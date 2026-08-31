#!/usr/bin/env python3
"""Add "En-face DICOM File Path" to the Retinal OCTA stage, and populate it.

WHY THIS EXISTS (C-03)
----------------------
The OCTA preview is meant to be the en-face image, fixed to the superficial
vascular plexus layer. The stage records En-face SOP Instance UID and En-face
Type / Layer but never recorded the en-face FILE PATH, so an event carries no
way back to the image it was built from.

That gap is what made the first backfill wrong: with no en-face path on the
event it reached for Flow Cube DICOM File Path instead and wrote a middle
slice of the OCT volume where an en-face image belongs.

This script closes the gap. It adds the data element, then backfills it from
the manifest by joining on flow_cube_sop_instance_uid, which is unique per
event. Run it before retinal_backfill_heidelberg_ybr.py.

USAGE
-----
    python3 "6. retinal imaging and OCTA/retinal_step3_add_enface_path.py" --dry-run
    python3 "6. retinal imaging and OCTA/retinal_step3_add_enface_path.py"
"""

import argparse
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import aireadi, dhis2  # noqa: E402
from common import metadata_uids as M  # noqa: E402

FIELD_NAME = "Retinal OCTA - En-face DICOM File Path"
STAGE_NAME = "Retinal OCTA"

ENFACE_KEYWORD = "superficial"


def pick_enface_slot(row):
    """The slot holding the superficial vascular plexus layer, else slot 1.

    Manufacturers word it differently, for example Zeiss says "Superficial
    retina vasculature flow", so match on the keyword.
    """
    for slot in (1, 2, 3, 4):
        label = row.get(f"associated_enface_{slot}_ophthalmic_image_type")
        if isinstance(label, str) and ENFACE_KEYWORD in label.lower():
            return slot
    return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true")
    args, _ = parser.parse_known_args()

    session = dhis2.get_session()

    print(f"Creating '{FIELD_NAME}'")
    uids = dhis2.create_data_elements(session, [
        {"name": FIELD_NAME, "shortName": "RetOCTA En-face File Path",
         "valueType": "LONG_TEXT", "aggregationType": "NONE"},
    ])
    enface_path_de = uids[FIELD_NAME]
    print(f"  {enface_path_de}")

    registry = M.load(session, refresh=True)
    stage_uid = registry.stage(STAGE_NAME)
    added = dhis2.attach_data_elements(session, stage_uid, [enface_path_de])
    print(f"  attached {added} field(s) to {STAGE_NAME} ({stage_uid})")

    print("\nBuilding the manifest index")
    manifest = pd.read_csv(aireadi.manifest_path("retinal_octa"), sep="\t")
    by_flow_cube = {}
    for row in manifest.to_dict("records"):
        key = row.get("flow_cube_sop_instance_uid")
        if not isinstance(key, str):
            continue
        slot = pick_enface_slot(row)
        path = row.get(f"associated_enface_{slot}_file_path")
        if isinstance(path, str) and path.strip() and path.strip().lower() != "not reported":
            by_flow_cube[key] = path
    print(f"  {len(by_flow_cube)} rows with a usable en-face path")

    flow_cube_de = M.RETINAL_OCTA_FIELD_UIDS["flow_cube_sop_instance_uid"]

    print("\nFetching OCTA events")
    events = dhis2.fetch_events(session, M.PROGRAM_UID, stage_uid)
    print(f"  {len(events)} events")

    updates, unmatched, already = [], 0, 0
    for event in events:
        values = {dv["dataElement"]: dv.get("value") for dv in event.get("dataValues", [])}
        if values.get(enface_path_de):
            already += 1
            continue
        path = by_flow_cube.get(values.get(flow_cube_de))
        if not path:
            unmatched += 1
            continue
        updates.append(dhis2.event_update_payload(
            event, stage_uid, M.PROGRAM_UID,
            dhis2.merge_data_values(event, {enface_path_de: path}),
        ))

    print(f"  to populate: {len(updates)}")
    print(f"  already set: {already}")
    print(f"  no manifest match: {unmatched}")

    if not updates:
        return 0
    if args.dry_run:
        print("\nDry run, nothing written.")
        return 0

    stats = dhis2.send_events(session, updates, "UPDATE", batch_size=200)
    print(f"\nPopulated {stats['updated']} event(s).")
    print("retinal_backfill_heidelberg_ybr.py can now rebuild OCTA previews "
          "from the correct source image.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
