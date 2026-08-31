#!/usr/bin/env python3
"""Create the "Diagnosis History" program stage.

One event per diagnosed condition per participant, sourced from
clinical_data/condition_occurrence.csv in OMOP CDM format.

Verified against the dataset (12,375 rows, 2,189 participants):
  - condition_source_value is truncated at 49 characters in the AI-READI
    export for longer descriptions. 3,388 rows sit at exactly 49. That is a
    source limitation, imported as-is rather than reconstructed.
  - condition_status_source_value and stop_reason are blank on every row, so
    both are excluded.

WHAT THE AUDIT FOUND (C-01, H-07), and what changed
----------------------------------------------------
Credentials come from the environment, and creation verifies its own result.

USAGE
-----
    python3 "5. diagnosis/diagnosis_step1_metadata.py"
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import dhis2  # noqa: E402
from common import metadata_uids as M  # noqa: E402

STAGE_NAME = "Diagnosis History"

FIELDS = [
    {"name": "Diagnosis Condition Code", "valueType": "TEXT"},
    {"name": "Diagnosis Condition Label", "valueType": "LONG_TEXT"},
    {"name": "Diagnosis Date", "valueType": "TEXT"},
]


def main():
    session = dhis2.get_session()
    uids = dhis2.create_data_elements(session, FIELDS)
    for name, uid in uids.items():
        print(f"{name}: {uid}")
    ordered = [uids[d["name"]] for d in FIELDS]
    stage_uid = dhis2.create_program_stage(session, STAGE_NAME, M.PROGRAM_UID, ordered)
    print(f"\nStage {STAGE_NAME}: {stage_uid}")
    print("diagnosis_step2_import.py resolves these by name.")


if __name__ == "__main__":
    main()
