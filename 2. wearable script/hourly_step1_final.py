#!/usr/bin/env python3
"""Create the eight wearable and CGM hourly-summary program stages.

Design:
  - Heart Rate, Respiratory Rate, SpO2 and Stress share one generic set of
    data elements (Mean/Minimum/Maximum/Reading Count). All four are dense
    point-in-time readings with the same shape, so four shared elements
    replace sixteen near-identical ones.
  - Sleep and Activity keep semantically correct elements of their own,
    duration-minutes and steps-sum, rather than having mean/min/max forced
    onto data where those do not apply.
  - Glucose gets the richer CGM element set: mean, min, max, count, SD, the
    three time-in-range percentages, and the device sentinel counts.

WHAT THE AUDIT FOUND (C-01, H-07), and what changed
----------------------------------------------------
Credentials were a REPLACE_ME constant and are now read from the environment.

More seriously, create_data_elements accepted any 200 or 201 and then resolved
UIDs by a name:in:[...] lookup whose result was never checked. DHIS2 can
answer 200 with status ERROR, so a failed create left {} and the stage was
then built from {'dataElement': {'id': None}}. Creation now verifies that
every requested object exists before a stage is created, and refuses to build
a stage from a null UID.

The script is idempotent, so re-running it is safe.

USAGE
-----
    python3 "2. wearable script/hourly_step1_final.py"
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import dhis2  # noqa: E402
from common import metadata_uids as M  # noqa: E402

SHARED_CONTINUOUS = [
    {"name": "Mean Value", "valueType": "NUMBER", "aggregationType": "AVERAGE"},
    {"name": "Minimum Value", "valueType": "NUMBER", "aggregationType": "MIN"},
    {"name": "Maximum Value", "valueType": "NUMBER", "aggregationType": "MAX"},
    {"name": "Reading Count", "valueType": "INTEGER", "aggregationType": "SUM"},
]

CONTINUOUS_STAGES = [
    "Wearable - Heart Rate",
    "Wearable - Respiratory Rate",
    "Wearable - SpO2",
    "Wearable - Stress",
]

DEDICATED_STAGES = [
    ("Wearable - Sleep", [
        {"name": "Sleep Stage", "valueType": "TEXT", "aggregationType": "NONE"},
        {"name": "Sleep Segment Duration Minutes", "valueType": "NUMBER",
         "aggregationType": "SUM"},
    ]),
    ("Wearable - Activity", [
        {"name": "Steps Sum", "valueType": "NUMBER", "aggregationType": "SUM"},
        {"name": "Steps Reading Count", "valueType": "INTEGER", "aggregationType": "SUM"},
    ]),
    ("Wearable - Calories", [
        {"name": "Calories Sum", "valueType": "NUMBER", "aggregationType": "SUM"},
        {"name": "Calories Reading Count", "valueType": "INTEGER", "aggregationType": "SUM"},
    ]),
    ("CGM - Glucose", [
        {"name": "Glucose Mean", "valueType": "NUMBER", "aggregationType": "AVERAGE"},
        {"name": "Glucose Minimum", "valueType": "NUMBER", "aggregationType": "MIN"},
        {"name": "Glucose Maximum", "valueType": "NUMBER", "aggregationType": "MAX"},
        {"name": "Glucose Reading Count", "valueType": "INTEGER", "aggregationType": "SUM"},
        {"name": "Glucose Standard Deviation", "valueType": "NUMBER", "aggregationType": "NONE"},
        {"name": "Time in Range Percent", "valueType": "NUMBER", "aggregationType": "AVERAGE"},
        {"name": "Time Above Range Percent", "valueType": "NUMBER", "aggregationType": "AVERAGE"},
        {"name": "Time Below Range Percent", "valueType": "NUMBER", "aggregationType": "AVERAGE"},
        {"name": "Device High Count", "valueType": "INTEGER", "aggregationType": "SUM"},
        {"name": "Device Low Count", "valueType": "INTEGER", "aggregationType": "SUM"},
    ]),
]


def main():
    session = dhis2.get_session()
    created = {}

    print("Creating the shared continuous-metric elements")
    shared = dhis2.create_data_elements(session, SHARED_CONTINUOUS)
    shared_order = [shared[d["name"]] for d in SHARED_CONTINUOUS]
    for name, uid in shared.items():
        print(f"  {name}: {uid}")

    for stage_name in CONTINUOUS_STAGES:
        uid = dhis2.create_program_stage(session, stage_name, M.PROGRAM_UID, shared_order)
        created[stage_name] = uid
        print(f"  stage {stage_name}: {uid}")

    for stage_name, defs in DEDICATED_STAGES:
        print(f"\nCreating {stage_name}")
        uids = dhis2.create_data_elements(session, defs)
        for name, uid in uids.items():
            print(f"  {name}: {uid}")
        ordered = [uids[d["name"]] for d in defs]
        stage_uid = dhis2.create_program_stage(session, stage_name, M.PROGRAM_UID, ordered)
        created[stage_name] = stage_uid
        print(f"  stage {stage_name}: {stage_uid}")

    print("\nDone. Nothing needs pasting into the import script: it resolves "
          "these by name through common.metadata_uids.")
    print("Refresh the cache with:  python3 -m common.metadata_uids --refresh")
    for name, uid in created.items():
        print(f"  {name}: {uid}")


if __name__ == "__main__":
    main()
