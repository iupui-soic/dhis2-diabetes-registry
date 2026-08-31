#!/usr/bin/env python3
"""Rebuild preview images for the two known conversion failures.

  1. Heidelberg events with an EMPTY preview, caused by pylibjpeg not being
     installed on the first full import. Applies to both retinal stages.
  2. Optomed and iCare events with a WRONG-COLOURED preview, caused by
     YBR_FULL_422 pixel data being treated as RGB. Photography only, since
     the OCTA en-face files are all MONOCHROME2.

Events whose source file is genuinely corrupt, reported as "broken data
stream", are deliberately left alone.

WHAT THE AUDIT FOUND, and what changed
---------------------------------------
C-02, the serious one. The update payload carried a single data value:

    "dataValues": [{"dataElement": preview_uid, "value": new_file_resource_id}]

A tracker UPDATE treats the submitted event as authoritative, so every other
value on the event was at risk: Manufacturer, Model, Laterality, Anatomic
Region, SOP Instance UID and the source file path. Those are also the fields
this script reads to decide what needs fixing, so a second run would no
longer find them. Every other backfill in this repository merges with the
event's existing values; this one now does too, via dhis2.merge_data_values.

C-03. For OCTA the script converted Flow Cube DICOM File Path, which is the
volumetric OCT cube, and wrote its middle slice where an en-face image
belongs. It now reads the En-face DICOM File Path field added by
retinal_step3_add_enface_path.py, and refuses to process OCTA at all if that
field is missing rather than silently reaching for the wrong source.

H-03. find_events_for_stage requested pageSize 200 and returned the first
page with no loop, so any participant with more than 200 events in a stage
silently kept their broken previews. It now pages.

USAGE
-----
    python3 ".../retinal_step3_add_enface_path.py"          # once, first
    python3 ".../retinal_backfill_heidelberg_ybr.py" --person-id 1072   # pilot
    nohup python3 ".../retinal_backfill_heidelberg_ybr.py" > backfill.log 2>&1 &
"""

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import aireadi, dhis2, dicom  # noqa: E402
from common import metadata_uids as M  # noqa: E402
from common.checkpoint import Checkpoint  # noqa: E402

CHECKPOINT_FULL = "retinal_backfill_heidelberg_ybr_checkpoint.json"
CHECKPOINT_PILOT = "retinal_backfill_heidelberg_ybr_checkpoint_PILOT.json"

YBR_MANUFACTURERS = {"Optomed", "iCare"}
HEIDELBERG = "Heidelberg"

ORG_UNIT_NAME = "AI-READI Study"


def resolve_org_unit(session):
    items = dhis2.get_json(
        session, "organisationUnits",
        {"filter": f"name:eq:{ORG_UNIT_NAME}", "fields": "id,name"},
    ).get("organisationUnits", [])
    if not items:
        sys.exit(f"ERROR: no org unit named '{ORG_UNIT_NAME}'.")
    return items[0]["id"]


def fetch_tracked_entities(session, org_unit_uid, person_id=None):
    params = {
        "program": M.PROGRAM_UID,
        "orgUnit": org_unit_uid,
        "ouMode": "DESCENDANTS",
        "fields": "trackedEntity",
    }
    if person_id:
        params["filter"] = f"{M.PERSON_ID_ATTR_UID}:eq:{person_id}"
        items = dhis2.extract_items(
            dhis2.get_json(session, "tracker/trackedEntities", params),
            "trackedEntities", "instances",
        )
        if not items:
            sys.exit(f"ERROR: no participant with person_id={person_id}.")
        return [items[0]["trackedEntity"]]

    return [
        item["trackedEntity"]
        for item in dhis2.fetch_all_pages(
            session, "tracker/trackedEntities", params,
            ("trackedEntities", "instances"),
        )
    ]


def classify(events, field_uids, path_key, allow_ybr):
    """Events needing a rebuilt preview, with the source path to rebuild from."""
    manufacturer_de = field_uids["manufacturer"]
    preview_de = field_uids["preview"]
    path_de = field_uids[path_key]

    out = []
    for event in events:
        values = {dv["dataElement"]: dv.get("value") for dv in event.get("dataValues", [])}
        source_path = values.get(path_de)
        if not source_path:
            continue

        manufacturer = values.get(manufacturer_de, "")
        preview = values.get(preview_de)

        if manufacturer == HEIDELBERG and not preview:
            reason = "heidelberg_missing"
        elif allow_ybr and manufacturer in YBR_MANUFACTURERS:
            reason = "ybr_miscolor"
        else:
            continue

        out.append({
            "event": event,
            "path": source_path,
            "old_preview": preview,
            "reason": reason,
        })
    return out


def process_stage(session, stage_name, stage_uid, field_uids, path_key,
                  allow_ybr, modality, tei_uids, checkpoint):
    print(f"\nScanning {stage_name} across {len(tei_uids)} participant(s)")
    preview_de = field_uids["preview"]
    counts = {"heidelberg_missing": 0, "ybr_miscolor": 0}
    fixed = 0

    for index, tei_uid in enumerate(tei_uids, start=1):
        # H-03: paged, so participants with more than 200 events are not
        # silently truncated.
        events = dhis2.fetch_events(session, M.PROGRAM_UID, stage_uid, tei_uid)

        for item in classify(events, field_uids, path_key, allow_ybr):
            event_uid = item["event"]["event"]
            if checkpoint.is_done(event_uid):
                continue
            counts[item["reason"]] += 1

            path = aireadi.resolve(modality, item["path"])
            if path is None:
                checkpoint.mark_failed(event_uid, f"source file not found: {item['path']}")
                continue

            jpeg, error = dicom.to_jpeg_bytes(path)
            if jpeg is None:
                checkpoint.mark_failed(event_uid, f"conversion failed: {error}")
                continue

            try:
                files = {"file": (Path(path).stem + ".jpg", jpeg, "image/jpeg")}
                response = dhis2.request(
                    session, "POST", f"{dhis2.api_url()}/fileResources",
                    params={"domain": "DATA_VALUE"}, files=files,
                ).json()
                resource = (response.get("response", {}).get("fileResource")
                            or response.get("fileResource") or response)
                new_uid = resource.get("id")
                if not new_uid:
                    raise dhis2.Dhis2Error(f"no fileResource id in {response}")

                # C-02: merge, never replace. Sending only the preview would
                # put every other value on this event at risk.
                dhis2.send_events(
                    session,
                    [dhis2.event_update_payload(
                        item["event"], stage_uid, M.PROGRAM_UID,
                        dhis2.merge_data_values(item["event"], {preview_de: new_uid}),
                    )],
                    "UPDATE",
                )

                if item["old_preview"]:
                    try:
                        dhis2.request(
                            session, "DELETE",
                            f"{dhis2.api_url()}/fileResources/{item['old_preview']}",
                        )
                    except dhis2.Dhis2Error as exc:
                        # An orphaned file is a storage concern, not a
                        # correctness one. Nothing references it any more.
                        print(f"    could not delete old file resource: {exc}")

                checkpoint.mark_done(event_uid, note=item["reason"])
                fixed += 1

            except Exception as exc:
                checkpoint.mark_failed(event_uid, exc)

        if index % 200 == 0 or index == len(tei_uids):
            print(f"  {index}/{len(tei_uids)} scanned, "
                  f"{counts['heidelberg_missing']} Heidelberg + "
                  f"{counts['ybr_miscolor']} YBR found, {fixed} fixed, "
                  f"{len(checkpoint.failed)} failed")

    print(f"{stage_name} complete: {fixed} preview(s) rebuilt")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--person-id", help="pilot run against one participant")
    parser.add_argument("--skip-octa", action="store_true",
                        help="photography only, if the en-face path field is absent")
    args, _ = parser.parse_known_args()

    session = dhis2.get_session()
    registry = M.load(session)

    photo_stage = registry.stage("Retinal Photography")
    octa_stage = registry.stage("Retinal OCTA")

    photo_fields = dict(M.RETINAL_PHOTOGRAPHY_FIELD_UIDS)
    octa_fields = dict(M.RETINAL_OCTA_FIELD_UIDS)
    octa_fields["enface_file_path"] = registry.maybe_data_element(
        "Retinal OCTA - En-face DICOM File Path"
    )

    org_unit = resolve_org_unit(session)
    tei_uids = fetch_tracked_entities(session, org_unit, args.person_id)
    checkpoint_file = CHECKPOINT_PILOT if args.person_id else CHECKPOINT_FULL
    print(f"{'PILOT' if args.person_id else 'FULL COHORT'} mode, "
          f"{len(tei_uids)} participant(s), checkpoint {checkpoint_file}")

    start = time.time()
    with Checkpoint(checkpoint_file, flush_every=25) as checkpoint:
        process_stage(
            session, "Retinal Photography", photo_stage, photo_fields,
            "filepath", True, "retinal_photography", tei_uids, checkpoint,
        )

        if args.skip_octa:
            print("\nSkipping Retinal OCTA as requested.")
        elif not octa_fields["enface_file_path"]:
            # C-03: without the en-face path there is no correct source for
            # an OCTA preview. Refuse rather than convert the flow cube.
            print("\nSkipping Retinal OCTA: the 'Retinal OCTA - En-face DICOM "
                  "File Path' data element does not exist yet.")
            print("Run retinal_step3_add_enface_path.py first. Converting the "
                  "flow cube instead would write a slice of the OCT volume "
                  "where an en-face image belongs, which is the C-03 defect.")
        else:
            process_stage(
                session, "Retinal OCTA", octa_stage, octa_fields,
                "enface_file_path", False, "retinal_octa", tei_uids, checkpoint,
            )

    print(f"\nDone in {(time.time() - start) / 60:.1f} min. {checkpoint.summary()}")
    return 1 if checkpoint.failed else 0


if __name__ == "__main__":
    sys.exit(main())
