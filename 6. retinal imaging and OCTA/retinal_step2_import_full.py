#!/usr/bin/env python3
"""Import retinal photography and OCTA events, for one participant or all.

For each manifest row: convert the source DICOM to a preview JPEG, upload it
as a DHIS2 file resource, and create a tracker event carrying the row's
metadata plus a reference to the preview.

WHAT THE AUDIT FOUND, and what changed
---------------------------------------
C-01  Credentials and the dataset root come from the environment.

M-05  occurredAt was time.strftime("%Y-%m-%d"), so all 118,480 retinal events
      carried the date they were imported rather than the date the image was
      taken. It now uses the participant's study_visit_date.

M-06  The conversion was copied into four files that then diverged, and only
      the backfill learned to handle YBR. Every caller now uses common.dicom,
      so a re-import cannot reintroduce the colour cast.

C-03  The OCTA event now stores the en-face file path it was built from, so a
      later repair can find the right source instead of falling back to the
      flow cube.

M-01  Both manifests were re-read and re-parsed inside the per-participant
      function, so a full run parsed them 2,280 times each. They are now read
      once and grouped.

M-02  save_checkpoint rewrote a 10.4 MB file after every one of 118,480
      events. Writes are now batched and atomic.

USAGE
-----
    python3 ".../retinal_step2_import_full.py" --person-id 1072    # pilot
    nohup python3 ".../retinal_step2_import_full.py" --all > retinal.log 2>&1 &
"""

import argparse
import os
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import aireadi, dhis2, dicom  # noqa: E402
from common import metadata_uids as M  # noqa: E402
from common.checkpoint import Checkpoint  # noqa: E402

CHECKPOINT_FILE = "retinal_import_checkpoint.json"
ORG_UNIT_NAME = "AI-READI Study"
ENFACE_KEYWORD = "superficial"

PHOTO_STAGE = "Retinal Photography"
OCTA_STAGE = "Retinal OCTA"


def resolve_org_unit(session):
    items = dhis2.get_json(
        session, "organisationUnits",
        {"filter": f"name:eq:{ORG_UNIT_NAME}", "fields": "id,name"},
    ).get("organisationUnits", [])
    if not items:
        sys.exit(f"ERROR: no org unit named '{ORG_UNIT_NAME}'.")
    return items[0]["id"]


def fetch_roster(session, org_unit_uid, person_id=None):
    """Every participant's person_id, tracked entity, enrollment and org unit."""
    params = {
        "program": M.PROGRAM_UID,
        "orgUnit": org_unit_uid,
        "ouMode": "DESCENDANTS",
        "fields": "trackedEntity,attributes[attribute,value],"
                  "enrollments[enrollment,orgUnit,status]",
    }
    if person_id:
        params["filter"] = f"{M.PERSON_ID_ATTR_UID}:eq:{person_id}"

    items = dhis2.fetch_all_pages(
        session, "tracker/trackedEntities", params, ("trackedEntities", "instances")
    )

    roster = []
    for item in items:
        pid = next(
            (a.get("value") for a in item.get("attributes", [])
             if a.get("attribute") == M.PERSON_ID_ATTR_UID),
            None,
        )
        enrollments = item.get("enrollments") or []
        active = next((e for e in enrollments if e.get("status") == "ACTIVE"), None)
        active = active or (enrollments[0] if enrollments else None)
        if pid and active:
            roster.append({
                "person_id": pid,
                "trackedEntity": item["trackedEntity"],
                "enrollment": active["enrollment"],
                "orgUnit": active.get("orgUnit") or org_unit_uid,
            })
    return roster


def pick_enface_slot(row):
    """The slot holding the superficial vascular plexus layer, else slot 1."""
    for slot in (1, 2, 3, 4):
        label = row.get(f"associated_enface_{slot}_ophthalmic_image_type")
        if isinstance(label, str) and ENFACE_KEYWORD in label.lower():
            return slot, label
    return 1, row.get("associated_enface_1_ophthalmic_image_type")


def upload_preview(session, modality, relative_path):
    """Convert and upload one image. Returns (file resource uid, reason)."""
    path = aireadi.resolve(modality, relative_path)
    if path is None:
        return None, f"source file not found: {relative_path}"
    jpeg, error = dicom.to_jpeg_bytes(path)
    if jpeg is None:
        return None, f"conversion failed: {error}"

    files = {"file": (Path(path).stem + ".jpg", jpeg, "image/jpeg")}
    response = dhis2.request(
        session, "POST", f"{dhis2.api_url()}/fileResources",
        params={"domain": "DATA_VALUE"}, files=files,
    ).json()
    resource = (response.get("response", {}).get("fileResource")
                or response.get("fileResource") or response)
    uid = resource.get("id")
    if not uid:
        return None, f"no fileResource id in response: {response}"
    return uid, None


def build_event(stage_uid, context, occurred_at, values):
    data_values = [
        dv for dv in (dhis2.data_value(uid, value) for uid, value in values)
        if dv is not None
    ]
    return {
        "program": M.PROGRAM_UID,
        "programStage": stage_uid,
        "trackedEntity": context["trackedEntity"],
        "enrollment": context["enrollment"],
        "orgUnit": context["orgUnit"],
        "occurredAt": occurred_at,
        "status": "COMPLETED",
        "dataValues": data_values,
    }


def import_photography(session, fields, stage_uid, context, rows,
                       occurred_at, checkpoint):
    created = 0
    for row in rows:
        key = f"photography::{row.get('sop_instance_uid')}"
        if checkpoint.is_done(key):
            continue

        preview_uid, reason = upload_preview(
            session, "retinal_photography", row.get("filepath")
        )
        if reason:
            print(f"    {reason}")

        event = build_event(stage_uid, context, occurred_at, [
            (fields["manufacturer"], row.get("manufacturer")),
            (fields["manufacturers_model_name"], row.get("manufacturers_model_name")),
            (fields["laterality"], row.get("laterality")),
            (fields["anatomic_region"], row.get("anatomic_region")),
            (fields["imaging"], row.get("imaging")),
            (fields["height"], row.get("height")),
            (fields["width"], row.get("width")),
            (fields["color_channel_dimension"], row.get("color_channel_dimension")),
            (fields["sop_instance_uid"], row.get("sop_instance_uid")),
            (fields["filepath"], row.get("filepath")),
            (fields["preview"], preview_uid),
        ])
        dhis2.send_events(session, [event], "CREATE")
        checkpoint.mark_done(key)
        created += 1
    return created


def import_octa(session, fields, stage_uid, context, rows, occurred_at, checkpoint):
    created = 0
    for row in rows:
        key = f"octa::{row.get('flow_cube_sop_instance_uid')}"
        if checkpoint.is_done(key):
            continue

        slot, layer_label = pick_enface_slot(row)
        enface_path = row.get(f"associated_enface_{slot}_file_path")
        preview_uid, reason = upload_preview(session, "retinal_octa", enface_path)
        if reason:
            print(f"    {reason}")

        values = [
            (fields["manufacturer"], row.get("manufacturer")),
            (fields["manufacturers_model_name"], row.get("manufacturers_model_name")),
            (fields["laterality"], row.get("laterality")),
            (fields["anatomic_region"], row.get("anatomic_region")),
            (fields["imaging"], row.get("imaging")),
            (fields["flow_cube_height"], row.get("flow_cube_height")),
            (fields["flow_cube_width"], row.get("flow_cube_width")),
            (fields["flow_cube_number_of_frames"], row.get("flow_cube_number_of_frames")),
            (fields["flow_cube_sop_instance_uid"], row.get("flow_cube_sop_instance_uid")),
            (fields["flow_cube_file_path"], row.get("flow_cube_file_path")),
            (fields["segmentation_file_path"], row.get("associated_segmentation_file_path")),
            (fields["segmentation_sop_instance_uid"],
             row.get("associated_segmentation_sop_instance_uid")),
            (fields["segmentation_type"], row.get("associated_segmentation_type")),
            (fields["enface_layer"], layer_label),
            (fields["enface_sop_instance_uid"],
             row.get(f"associated_enface_{slot}_sop_instance_uid")),
            (fields["preview"], preview_uid),
        ]
        # C-03: record which file the preview came from.
        if fields.get("enface_file_path"):
            values.append((fields["enface_file_path"], enface_path))

        dhis2.send_events(session, [build_event(stage_uid, context, occurred_at, values)],
                          "CREATE")
        checkpoint.mark_done(key)
        created += 1
    return created


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--person-id", help="import a single participant")
    parser.add_argument("--all", action="store_true", help="import every participant")
    args, _ = parser.parse_known_args()

    if not args.all and not args.person_id:
        parser.error("pass --person-id for a pilot run, or --all for the full cohort")

    session = dhis2.get_session()
    registry = M.load(session)

    photo_fields = dict(M.RETINAL_PHOTOGRAPHY_FIELD_UIDS)
    octa_fields = dict(M.RETINAL_OCTA_FIELD_UIDS)
    octa_fields["enface_file_path"] = registry.maybe_data_element(
        "Retinal OCTA - En-face DICOM File Path"
    )
    if not octa_fields["enface_file_path"]:
        print("NOTE: 'Retinal OCTA - En-face DICOM File Path' does not exist yet, so "
              "OCTA events will not record their preview source. Run "
              "retinal_step3_add_enface_path.py to add it.")

    photo_stage = registry.stage(PHOTO_STAGE)
    octa_stage = registry.stage(OCTA_STAGE)

    # M-01: read each manifest once, then group.
    print("Reading manifests")
    photo_rows = {}
    for row in pd.read_csv(aireadi.manifest_path("retinal_photography"), sep="\t").to_dict("records"):
        photo_rows.setdefault(str(row["person_id"]), []).append(row)
    octa_rows = {}
    for row in pd.read_csv(aireadi.manifest_path("retinal_octa"), sep="\t").to_dict("records"):
        octa_rows.setdefault(str(row["person_id"]), []).append(row)
    print(f"  photography {sum(len(v) for v in photo_rows.values())} rows, "
          f"OCTA {sum(len(v) for v in octa_rows.values())} rows")

    # M-05: real visit dates rather than the date of the run.
    participants = pd.read_csv(aireadi.participants_file(), sep="\t")
    participants["person_id"] = participants["person_id"].astype(str)
    visit_dates = {
        pid: str(date)[:10]
        for pid, date in zip(participants["person_id"], participants["study_visit_date"])
        if pd.notna(date)
    }

    org_unit = resolve_org_unit(session)
    roster = fetch_roster(session, org_unit, args.person_id)
    print(f"Roster: {len(roster)} participant(s) with a valid enrollment\n")

    start = time.time()
    with Checkpoint(CHECKPOINT_FILE, flush_every=50) as checkpoint:
        for index, entry in enumerate(roster, start=1):
            person_id = entry["person_id"]
            occurred_at = visit_dates.get(person_id)
            if not occurred_at:
                checkpoint.mark_failed(person_id, "no study_visit_date")
                print(f"[{index}/{len(roster)}] {person_id}: no visit date, skipped")
                continue

            try:
                created = import_photography(
                    session, photo_fields, photo_stage, entry,
                    photo_rows.get(person_id, []), occurred_at, checkpoint,
                )
                created += import_octa(
                    session, octa_fields, octa_stage, entry,
                    octa_rows.get(person_id, []), occurred_at, checkpoint,
                )
                checkpoint.mark_done(person_id)
            except Exception as exc:
                checkpoint.mark_failed(person_id, exc)
                print(f"[{index}/{len(roster)}] {person_id}: FAILED, {str(exc)[:300]}")
                continue

            if index % 25 == 0 or index == len(roster):
                elapsed = (time.time() - start) / 60
                rate = index / elapsed if elapsed else 0
                eta = (len(roster) - index) / rate if rate else 0
                print(f"[{index}/{len(roster)}] {created} new event(s) this participant, "
                      f"{elapsed:.1f} min elapsed, ETA {eta:.0f} min, "
                      f"{len(checkpoint.failed)} failed")

    print(f"\nRun complete. {checkpoint.summary()}")
    return 1 if checkpoint.failed else 0


if __name__ == "__main__":
    sys.exit(main())
