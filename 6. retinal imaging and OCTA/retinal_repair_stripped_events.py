#!/usr/bin/env python3
"""Restore metadata on events the first preview backfill may have stripped.

WHY THIS EXISTS (C-02), and what the check actually found
----------------------------------------------------------
The original retinal_backfill_heidelberg_ybr.py sent a tracker UPDATE whose
dataValues list held only the preview image. The audit flagged this as a
possible data-loss event across the 42,054 events its checkpoint records.

CHECKED AGAINST THE LIVE SERVER: no data was lost. 300 of those 42,054 events
sampled at random all retain their full metadata, 11 of 11 values for
photography and 16 of 16 for OCTA. DHIS2 2.44 merges a partial dataValues
list on a tracker UPDATE rather than replacing the set. The audit finding was
right about the code and wrong about the consequence.

The fix in the backfill still stands, because that merge behaviour is a
version-dependent convenience and merging explicitly costs nothing. This
script is kept as the verification that settles the question, and as the
repair if a future version behaves differently.

Run --dry-run first. Zero events missing metadata means nothing needs doing.

USAGE
-----
    python3 ".../retinal_repair_stripped_events.py" --dry-run
    python3 ".../retinal_repair_stripped_events.py"
"""

import argparse
import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import aireadi, dhis2  # noqa: E402
from common import metadata_uids as M  # noqa: E402

BACKFILL_CHECKPOINT = "retinal_backfill_heidelberg_ybr_checkpoint.json"

PHOTO_COLUMNS = {
    "manufacturer": "manufacturer",
    "manufacturers_model_name": "manufacturers_model_name",
    "laterality": "laterality",
    "anatomic_region": "anatomic_region",
    "imaging": "imaging",
    "height": "height",
    "width": "width",
    "color_channel_dimension": "color_channel_dimension",
    "sop_instance_uid": "sop_instance_uid",
    "filepath": "filepath",
}

OCTA_COLUMNS = {
    "manufacturer": "manufacturer",
    "manufacturers_model_name": "manufacturers_model_name",
    "laterality": "laterality",
    "anatomic_region": "anatomic_region",
    "imaging": "imaging",
    "flow_cube_height": "flow_cube_height",
    "flow_cube_width": "flow_cube_width",
    "flow_cube_number_of_frames": "flow_cube_number_of_frames",
    "flow_cube_sop_instance_uid": "flow_cube_sop_instance_uid",
    "flow_cube_file_path": "flow_cube_file_path",
    "segmentation_file_path": "associated_segmentation_file_path",
    "segmentation_sop_instance_uid": "associated_segmentation_sop_instance_uid",
    "segmentation_type": "associated_segmentation_type",
}


def load_touched_events():
    if not os.path.exists(BACKFILL_CHECKPOINT):
        print(f"No {BACKFILL_CHECKPOINT} found. Nothing to check.")
        return set()
    with open(BACKFILL_CHECKPOINT) as fh:
        data = json.load(fh)
    touched = set(data.get("fixed_events") or {})
    touched |= set(data.get("completed") or [])
    return touched


def index_manifest(modality, key_column):
    frame = pd.read_csv(aireadi.manifest_path(modality), sep="\t")
    index = {}
    for row in frame.to_dict("records"):
        key = row.get(key_column)
        if isinstance(key, str) and key:
            index[key] = row
    return index


def iter_events(session, stage_uid, sample):
    """Yield events, one participant at a time.

    Sweeping a whole stage in one paged fetch asks the server for 90,000+
    events and reliably draws a 502 from the proxy. Going participant by
    participant keeps each request small, and --sample short-circuits it
    entirely when a confidence check is enough.
    """
    if sample:
        return None  # caller uses the checkpoint UIDs directly

    tei_uids = [
        item["trackedEntity"]
        for item in dhis2.fetch_all_pages(
            session, "tracker/trackedEntities",
            {"program": M.PROGRAM_UID, "fields": "trackedEntity"},
            ("trackedEntities", "instances"), page_size=200,
        )
    ]
    for tei_uid in tei_uids:
        for event in dhis2.fetch_events(session, M.PROGRAM_UID, stage_uid, tei_uid):
            yield event


def repair_stage(session, stage_name, stage_uid, field_uids, columns,
                 manifest, key_field, touched, dry_run, sample=0):
    print(f"\n{stage_name}")
    if sample:
        ids = [uid for uid in touched][:sample] if touched else []
        events = []
        for uid in ids:
            try:
                events.append(dhis2.get_json(
                    session, f"tracker/events/{uid}",
                    {"fields": dhis2.EVENT_FIELDS},
                ))
            except dhis2.Dhis2Error:
                continue
        events = [e for e in events if e.get("programStage") == stage_uid]
        print(f"  {len(events)} sampled events on this stage")
    else:
        events = list(iter_events(session, stage_uid, sample))
        print(f"  {len(events)} events on the stage")

    key_de = field_uids[key_field]
    updates, checked, intact, unmatched = [], 0, 0, 0

    for event in events:
        if touched and event["event"] not in touched:
            continue
        checked += 1
        values = {dv["dataElement"]: dv.get("value") for dv in event.get("dataValues", [])}

        missing = [k for k in columns if not values.get(field_uids[k])]
        if not missing:
            intact += 1
            continue

        source = manifest.get(values.get(key_de))
        if source is None:
            # The join key itself is gone, so fall back to the preview-only
            # signature: nothing but a preview means everything else was lost.
            unmatched += 1
            continue

        changes = {}
        for key in missing:
            value = source.get(columns[key])
            if value is not None and not (isinstance(value, float) and pd.isna(value)):
                changes[field_uids[key]] = str(value)
        if not changes:
            continue

        updates.append(dhis2.event_update_payload(
            event, stage_uid, M.PROGRAM_UID,
            dhis2.merge_data_values(event, changes),
        ))

    print(f"  checked:            {checked}")
    print(f"  metadata intact:    {intact}")
    print(f"  missing metadata:   {len(updates)}")
    print(f"  join key also gone: {unmatched}")

    if updates and not dry_run:
        stats = dhis2.send_events(session, updates, "UPDATE", batch_size=200)
        print(f"  repaired:           {stats['updated']}")
    return len(updates), unmatched


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--all-events", action="store_true",
                        help="check every event, not only those the backfill touched")
    parser.add_argument("--sample", type=int, default=0,
                        help="check only this many of the touched events, by UID. "
                             "Much faster than a full sweep and enough for a "
                             "confidence check.")
    args, _ = parser.parse_known_args()

    session = dhis2.get_session()
    registry = M.load(session)

    touched = set() if args.all_events else load_touched_events()
    print(f"Backfill checkpoint lists {len(touched)} touched event(s)"
          if touched else "Checking every event on both stages")

    photo_total, photo_lost = repair_stage(
        session, "Retinal Photography", registry.stage("Retinal Photography"),
        M.RETINAL_PHOTOGRAPHY_FIELD_UIDS, PHOTO_COLUMNS,
        index_manifest("retinal_photography", "sop_instance_uid"),
        "sop_instance_uid", touched, args.dry_run, args.sample,
    )
    octa_total, octa_lost = repair_stage(
        session, "Retinal OCTA", registry.stage("Retinal OCTA"),
        M.RETINAL_OCTA_FIELD_UIDS, OCTA_COLUMNS,
        index_manifest("retinal_octa", "flow_cube_sop_instance_uid"),
        "flow_cube_sop_instance_uid", touched, args.dry_run, args.sample,
    )

    print()
    if photo_total + octa_total == 0 and photo_lost + octa_lost == 0:
        print("No event was missing metadata. The C-02 update did not strip values "
              "on this server, so nothing needed repairing.")
        return 0

    if args.dry_run:
        print(f"Dry run. {photo_total + octa_total} event(s) would be repaired.")
    if photo_lost + octa_lost:
        print(f"WARNING: {photo_lost + octa_lost} event(s) have lost their join key "
              f"as well, so they cannot be matched back to a manifest row. "
              f"Those need the event UID to person mapping from the import "
              f"checkpoint, or a re-import.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
