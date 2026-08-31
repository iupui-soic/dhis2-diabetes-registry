#!/usr/bin/env python3
"""Create the "Cardiac - 12-Lead ECG" program stage.

One event per recording. The stage is repeatable because six participants
have two recordings each.

Thresholds and their sources:
  Heart Rate    <60 / 60-100 / >100 bpm, American Heart Association
  PR Interval   <120 / 120-200 / >200 ms, StatPearls/NIH, LITFL, ACC/AHA
  QRS Duration  <120 normal, >=120 widened (bundle branch block), StatPearls
  QTc           deliberately not classified: the correction formula is not
                stated in the source data and QTc reference ranges are
                sex-specific, which the registry does not currently record
  P/QRS/T Axis  numeric only, since axis interpretation is context-dependent

WHAT THE AUDIT FOUND (C-01, H-02, H-07), and what changed
----------------------------------------------------------
H-02 as originally reported was WRONG, and this is corrected here.
"Within reference range" is defined in both the range set and the QRS
widening set, and the concern was that the second set would adopt the first
set's option. Checked against the live server: DHIS2 2.44 holds them as two
separate objects (hch4LaMZM4x and YYqlZx1OmoS) with the same name and code,
and both sets are intact. The wording is therefore left unchanged so existing
stored values stay valid. Options are still created nested inside their set
rather than standalone, which is more robust.

This module exports the option set names and field names that
ecg_step2_import.py imports, so the two cannot drift apart.

USAGE
-----
    python3 "3. ecg/ecg_step1_metadata.py"
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import dhis2  # noqa: E402
from common import metadata_uids as M  # noqa: E402

STAGE_NAME = "Cardiac - 12-Lead ECG"

RANGE_STATUS_SET = "ECG Reference Range Status"
RANGE_STATUS_OPTIONS = [
    "Below reference range",
    "Within reference range",
    "Above reference range",
]

QRS_STATUS_SET = "ECG QRS Widening Status"
QRS_STATUS_OPTIONS = [
    "Within reference range",
    "Widened (meets bundle branch block QRS criteria)",
]

HR_RANGE = (60, 100)
PR_RANGE = (120, 200)
QRS_WIDENED_AT = 120

FIELDS = [
    ("ECG Study Visit Date", "TEXT", None),
    ("ECG Validation Date", "TEXT", None),
    ("ECG Recording Duration (sec)", "NUMBER", None),
    ("ECG Heart Rate (bpm)", "INTEGER", None),
    ("ECG Heart Rate Status", "TEXT", RANGE_STATUS_SET),
    ("ECG PR Interval (ms)", "INTEGER", None),
    ("ECG PR Interval Status", "TEXT", RANGE_STATUS_SET),
    ("ECG QRS Duration (ms)", "INTEGER", None),
    ("ECG QRS Duration Status", "TEXT", QRS_STATUS_SET),
    ("ECG QT Interval (ms)", "INTEGER", None),
    ("ECG QTc Interval (ms)", "INTEGER", None),
    ("ECG P Axis (deg)", "INTEGER", None),
    ("ECG QRS Axis (deg)", "INTEGER", None),
    ("ECG T Axis (deg)", "INTEGER", None),
    ("ECG Participant Position", "TEXT", None),
    ("ECG Machine Interpretation Status", "TEXT", None),
    ("ECG Machine Interpretation Summary", "TEXT", None),
    ("ECG Finding 1", "TEXT", None),
    ("ECG Finding 1 Detail", "TEXT", None),
    ("ECG Finding 2", "TEXT", None),
    ("ECG Finding 2 Detail", "TEXT", None),
    ("ECG Finding 3", "TEXT", None),
    ("ECG Finding 3 Detail", "TEXT", None),
    ("ECG Device", "TEXT", None),
    ("ECG Sampling Frequency (Hz)", "NUMBER", None),
    ("ECG Number of Leads", "INTEGER", None),
    ("ECG Number of Samples", "INTEGER", None),
    ("ECG Raw Header File Path", "LONG_TEXT", None),
    ("ECG Raw Data File Path", "LONG_TEXT", None),
]


def main():
    session = dhis2.get_session()

    print("Creating option sets")
    range_uid = dhis2.create_option_set(session, RANGE_STATUS_SET, RANGE_STATUS_OPTIONS)
    qrs_uid = dhis2.create_option_set(session, QRS_STATUS_SET, QRS_STATUS_OPTIONS)
    print(f"  {RANGE_STATUS_SET}: {range_uid}")
    print(f"  {QRS_STATUS_SET}: {qrs_uid}")

    option_set_uids = {RANGE_STATUS_SET: range_uid, QRS_STATUS_SET: qrs_uid}

    print("\nCreating data elements")
    defs = [
        {"name": name, "shortName": name[:50], "valueType": vtype,
         "aggregationType": "NONE",
         "optionSet": option_set_uids.get(option_set)}
        for name, vtype, option_set in FIELDS
    ]
    uids = dhis2.create_data_elements(session, defs)
    for name, _, _ in FIELDS:
        print(f"  {name}: {uids[name]}")

    ordered = [uids[name] for name, _, _ in FIELDS]
    stage_uid = dhis2.create_program_stage(session, STAGE_NAME, M.PROGRAM_UID, ordered)
    print(f"\nStage {STAGE_NAME}: {stage_uid}")
    print("ecg_step2_import.py resolves these by name, so nothing needs pasting.")


if __name__ == "__main__":
    main()
