#!/usr/bin/env python3
"""Create the "Retinal Photography" and "Retinal OCTA" program stages.

Both are repeatable stages under the Diabetes Registry program, following the
same pattern as Cardiac ECG and Diagnosis History. Needs a write account.

Design decisions (confirmed with professor):
  - Photography preview = converted IR/CFP/FAF image (already 2D)
  - OCTA preview        = converted "enface" image, fixed to the
                           "Superficial vascular plexus flow" layer as the
                           default representative layer for every record
  - flow_cube / segmentation stay as metadata + file-path reference only,
    NOT converted to preview images (raw volumetric / derived data, not
    independently viewable)

WHAT THE AUDIT FOUND, and what changed
---------------------------------------
This was the one metadata script that did not go through common.dhis2. It
built its own session without reading .env, hardcoded the base URL, posted
to /api/dataElements one at a time and trusted the HTTP status, which DHIS2
answers with 200 even when the import report says ERROR. It now uses the
shared helpers, so it reads credentials the same way as everything else,
fails loudly on a soft error, matches existing objects dash-insensitively,
and verifies every UID it hands back.

The UIDs are no longer written to retinal_metadata_uids.json. The import
scripts resolve them by name through common.metadata_uids; run
`python3 -m common.metadata_uids --refresh` after this.

USAGE
-----
    python3 "6. retinal imaging and OCTA/retinal_step1_metadata.py"
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import dhis2  # noqa: E402
from common import metadata_uids as M  # noqa: E402

DEFAULT_ENFACE_LAYER = "Superficial vascular plexus flow"

RETINAL_PHOTOGRAPHY_FIELDS = [
    ("Manufacturer", "TEXT"),
    ("Model", "TEXT"),
    ("Laterality", "TEXT"),
    ("Anatomic Region", "TEXT"),
    ("Imaging Type (IR/CFP/FAF)", "TEXT"),
    ("Image Height", "INTEGER"),
    ("Image Width", "INTEGER"),
    ("Color Channel Dimension", "INTEGER"),
    ("SOP Instance UID", "TEXT"),
    ("Original DICOM File Path", "LONG_TEXT"),
    ("Preview Image", "IMAGE"),
]

RETINAL_OCTA_FIELDS = [
    ("Manufacturer", "TEXT"),
    ("Model", "TEXT"),
    ("Laterality", "TEXT"),
    ("Anatomic Region", "TEXT"),
    ("Imaging Type (OCTA)", "TEXT"),
    ("Flow Cube Height", "INTEGER"),
    ("Flow Cube Width", "INTEGER"),
    ("Flow Cube Number of Frames", "INTEGER"),
    ("Flow Cube SOP Instance UID", "TEXT"),
    ("Flow Cube DICOM File Path", "LONG_TEXT"),
    ("Segmentation DICOM File Path", "LONG_TEXT"),
    ("Segmentation SOP Instance UID", "TEXT"),
    ("Segmentation Type", "TEXT"),
    ("En-face Type / Layer", "TEXT"),
    ("En-face SOP Instance UID", "TEXT"),
    ("Preview Image", "IMAGE"),
]

# Data element names are prefixed with the stage so the two "Preview Image"
# and "SOP Instance UID" fields do not collide, and short names must be
# unique registry-wide, hence the short prefix.
STAGE_DEFS = [
    {"stage_name": "Retinal Photography", "short_prefix": "RetPhoto",
     "fields": RETINAL_PHOTOGRAPHY_FIELDS},
    {"stage_name": "Retinal OCTA", "short_prefix": "RetOCTA",
     "fields": RETINAL_OCTA_FIELDS},
]


def short_name_for(prefix, field_name, max_len=50):
    return " ".join(f"{prefix} {field_name}".split())[:max_len]


def build_stage(session, stage_def):
    stage_name = stage_def["stage_name"]
    print(f"\n{stage_name}")
    defs = [
        {"name": f"{stage_name} - {field}",
         "shortName": short_name_for(stage_def["short_prefix"], field),
         "valueType": value_type, "aggregationType": "NONE"}
        for field, value_type in stage_def["fields"]
    ]
    uids = dhis2.create_data_elements(session, defs)
    for d in defs:
        print(f"  {d['name']}: {uids[d['name']]}")

    ordered = [uids[d["name"]] for d in defs]
    stage_uid = dhis2.create_program_stage(session, stage_name, M.PROGRAM_UID, ordered)
    print(f"  stage: {stage_uid}")
    return stage_uid


def main():
    session = dhis2.get_session()
    print(f"Target program: {M.PROGRAM_UID} (Diabetes Registry)")
    print(f"Default OCTA preview enface layer: '{DEFAULT_ENFACE_LAYER}'")

    for stage_def in STAGE_DEFS:
        build_stage(session, stage_def)

    print("\nDone. Next:")
    print("  1. python3 -m common.metadata_uids --refresh")
    print("  2. In the Maintenance app, set sharing on both new stages (Data: View,")
    print("     Metadata: View for the Auditor role). New stages do not inherit it.")
    print("  3. Pilot one participant with retinal_step2_import.py before --all.")


if __name__ == "__main__":
    main()
