#!/usr/bin/env python3
"""Create the eight environment program stages.

Every stage carries Env Mean, Minimum, Maximum, Standard Deviation, Reading
Count, Hour Start and Hour End.

Threshold fields exist only where a defensible source supports them:
  Humidity     above 50% / below 30%, EPA and ASHRAE indoor comfort range
  Temperature  above 24C / below 20C, labelled a STUDY comfort range rather
               than presented as a literal ASHRAE Standard 55 cutoff, since
               ASHRAE 55 models comfort from temperature, humidity, air speed,
               clothing and metabolic rate rather than a fixed band
  PM2.5, PM10  no per-hour threshold. The WHO guidelines are 24-hour means,
               so comparing them to an hourly value misrepresents them
  NOx          no threshold. The WHO guideline is specific to NO2 and this
               sensor measures combined NOx
  PM1, PM4, VOC  no threshold, no credible source

WHAT THE AUDIT FOUND (C-01, H-07), and what changed
----------------------------------------------------
Credentials come from the environment. Creation verifies that every requested
object exists rather than trusting the status code, so a stage can no longer
be built from a null data element UID. The script is idempotent.

USAGE
-----
    python3 "4. envt/env_step1_metadata_v2.py"
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import dhis2  # noqa: E402
from common import metadata_uids as M  # noqa: E402

BASE_FIELDS = [
    {"name": "Env Mean", "valueType": "NUMBER", "aggregationType": "AVERAGE"},
    {"name": "Env Minimum", "valueType": "NUMBER", "aggregationType": "MIN"},
    {"name": "Env Maximum", "valueType": "NUMBER", "aggregationType": "MAX"},
    {"name": "Env Standard Deviation", "valueType": "NUMBER", "aggregationType": "NONE"},
    {"name": "Env Reading Count", "valueType": "INTEGER", "aggregationType": "SUM"},
    {"name": "Env Hour Start", "valueType": "TEXT", "aggregationType": "NONE"},
    {"name": "Env Hour End", "valueType": "TEXT", "aggregationType": "NONE"},
]

HUMIDITY_FIELDS = [
    {"name": "Humidity Above Comfort Range (>50%) - Count",
     "shortName": "Humidity Above Comfort Count",
     "valueType": "INTEGER", "aggregationType": "SUM"},
    {"name": "Humidity Above Comfort Range (>50%) - Timestamps",
     "shortName": "Humidity Above Comfort Timestamps",
     "valueType": "LONG_TEXT", "aggregationType": "NONE"},
    {"name": "Humidity Below Comfort Range (<30%) - Count",
     "shortName": "Humidity Below Comfort Count",
     "valueType": "INTEGER", "aggregationType": "SUM"},
    {"name": "Humidity Below Comfort Range (<30%) - Timestamps",
     "shortName": "Humidity Below Comfort Timestamps",
     "valueType": "LONG_TEXT", "aggregationType": "NONE"},
]

TEMPERATURE_FIELDS = [
    {"name": "Temperature Above Study Comfort Range (>24C) - Count",
     "shortName": "Temperature Above Comfort Count",
     "valueType": "INTEGER", "aggregationType": "SUM"},
    {"name": "Temperature Above Study Comfort Range (>24C) - Timestamps",
     "shortName": "Temperature Above Comfort Timestamps",
     "valueType": "LONG_TEXT", "aggregationType": "NONE"},
    {"name": "Temperature Below Study Comfort Range (<20C) - Count",
     "shortName": "Temperature Below Comfort Count",
     "valueType": "INTEGER", "aggregationType": "SUM"},
    {"name": "Temperature Below Study Comfort Range (<20C) - Timestamps",
     "shortName": "Temperature Below Comfort Timestamps",
     "valueType": "LONG_TEXT", "aggregationType": "NONE"},
]

STAGES = [
    ("Environment - PM1", None),
    ("Environment - PM2.5", None),
    ("Environment - PM4", None),
    ("Environment - PM10", None),
    ("Environment - Humidity", HUMIDITY_FIELDS),
    ("Environment - Temperature", TEMPERATURE_FIELDS),
    ("Environment - VOC", None),
    ("Environment - NOx", None),
]


def main():
    session = dhis2.get_session()

    print("Creating the shared base fields")
    base = dhis2.create_data_elements(session, BASE_FIELDS)
    base_order = [base[d["name"]] for d in BASE_FIELDS]
    for name, uid in base.items():
        print(f"  {name}: {uid}")

    for stage_name, extra_defs in STAGES:
        print(f"\nCreating {stage_name}")
        order = list(base_order)
        if extra_defs:
            extra = dhis2.create_data_elements(session, extra_defs)
            for name, uid in extra.items():
                print(f"  {name}: {uid}")
            order += [extra[d["name"]] for d in extra_defs]
        stage_uid = dhis2.create_program_stage(
            session, stage_name, M.PROGRAM_UID, order
        )
        print(f"  stage: {stage_uid}")

    print("\nDone. env_step2_import.py resolves these by name.")


if __name__ == "__main__":
    main()
