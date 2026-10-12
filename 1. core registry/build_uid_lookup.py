#!/usr/bin/env python3
"""Write uid_lookup.json for import_data.py from the live server.

WHY THIS EXISTS
---------------
import_data.py needs the program, tracked entity type, attribute and stage
UIDs, plus a data element UID for every CSV column it imports. That file was
built by hand on a workstation that no longer exists and was never committed,
so step 0 of the run order could not be reproduced. This script rebuilds it
by resolving every object by name against the server.

It does not CREATE the program or its stages. Those were built in the
Maintenance app before any script existed. If a name below does not resolve,
the script lists what it found so the mapping can be corrected, and refuses
to write a partial file.

USAGE
-----
    python3 "1. core registry/build_uid_lookup.py" --inventory   # list what the program has
    python3 "1. core registry/build_uid_lookup.py"               # write uid_lookup.json
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import dhis2  # noqa: E402
from common import metadata_uids as M  # noqa: E402

from import_data import STAGE_FIELDS  # noqa: E402

# CSV column -> tracked entity attribute display name, confirmed against the
# live program inventory. person_id is written separately by import_data.py
# and "Recommended ML Train/Val/Test Split" is not imported from the CSV.
ATTRIBUTE_NAMES = {
    "year_of_birth": "Year of Birth",
    "cl_maristat": "Marital Status",
    "study_group": "Diabetes Severity Group",
    "mhterm_dm2": "Type 2 Diabetes Status (self-reported)",
    "mhterm_predm": "Pre-Diabetes Status",
    "fh_dm2pt": "Family History - Parent T2D",
    "fh_dm2sb": "Family History - Sibling T2D",
    "clinical_site": "Clinical Recruitment Site",
}

# Stage key used by import_data.STAGE_FIELDS -> stage name on the server.
# Confirmed against the live program inventory on 2026-10-12. import_data.py
# loads only these three stages; the other core stages (Clinical Laboratory &
# Assessment Measurements, Medications, Device & Sensor Metadata) were
# populated by "adding raw reading metadata 1027.ipynb" and are not part of
# step 0.
STAGE_NAMES = {
    "vitals": "Vitals & Measurements",
    "survey": "Health & Lifestyle Survey",
    "cgm": "CGM Summary",
}

# CSV column -> data element name, confirmed against the inventory and against
# the values stored for a loaded participant. The survey stage also carries
# raw-named elements (pxfi1, sualckncf, dmlact, ...) added later by the raw
# metadata notebook; import_data.py writes the curated names below, so those
# raw elements must not be mapped here.
DATA_ELEMENT_NAMES = {
    "bp1_sysbp_vsorres": "Systolic Blood Pressure",
    "bp1_diabp_vsorres": "Diastolic Blood Pressure",
    "pulse_vsorres": "Heart Rate",
    "height_vsorres": "Height (cm)",
    "weight_vsorres": "Weight (kg)",
    "bmi_vsorres": "BMI",
    "waist_vsorres": "Waist Circumference (cm)",
    "hip_vsorres": "Hip Circumference (cm)",
    "whr_vsorres": "Waist-to-Hip Ratio",
    "mhoccur_hbp": "High Blood Pressure History",
    "mhoccur_clsh": "High Cholesterol History",
    "mhoccur_obs": "Obesity History",
    "mhoccur_mi": "Heart Attack History",
    "mhoccur_strk": "Stroke History",
    "mhoccur_circ": "Circulation Problems",
    "mhoccur_pdr": "Diabetic Retinopathy",
    "dmlact": "Typical Activity Level",
    "sualckncf": "Ever Consumed Alcohol",
    "susmkncf": "Smoked 100+ Cigarettes Lifetime",
    "susmkcdur": "Currently Smokes",
    "pxfi1": "Food Security Q1",
    "pxfi2": "Food Security Q2",
    "pxfi3": "Food Security Q3",
    "pxfi4": "Food Security Q4",
    "pxfi5": "Food Security Q5",
    "average_glucose_level_mg_dl": "Average Glucose Level (mg/dL)",
    "glucose_level_record_count": "Glucose Reading Count",
    "glucose_sensor_sampling_duration_days": "Sensor Wear Duration (days)",
}


def fetch_program(session):
    return dhis2.get_json(session, f"programs/{M.PROGRAM_UID}", {
        "fields": "id,name,trackedEntityType[id,name],"
                  "programTrackedEntityAttributes[trackedEntityAttribute[id,name,unique]],"
                  "programStages[id,name,programStageDataElements[dataElement[id,name]]]",
    })


def print_inventory(program):
    print(f"Program {program['id']}: {program['name']}")
    tet = program.get("trackedEntityType") or {}
    print(f"Tracked entity type: {tet.get('id')} {tet.get('name')}\n")
    print("Attributes:")
    for item in program.get("programTrackedEntityAttributes", []):
        attr = item["trackedEntityAttribute"]
        flag = "  (unique)" if attr.get("unique") else ""
        print(f"  {attr['id']}  {attr['name']}{flag}")
    print("\nStages:")
    for stage in program.get("programStages", []):
        print(f"  {stage['id']}  {stage['name']}")
        for psde in stage.get("programStageDataElements", []):
            de = psde["dataElement"]
            print(f"      {de['id']}  {de['name']}")


def resolve(program):
    """Build the lookup, returning (lookup, problems, warnings)."""
    problems, warnings = [], []
    norm = dhis2.normalise_name

    attrs = {norm(i["trackedEntityAttribute"]["name"]): i["trackedEntityAttribute"]
             for i in program.get("programTrackedEntityAttributes", [])}
    stages = {norm(s["name"]): s for s in program.get("programStages", [])}

    person = next((a for a in attrs.values() if a["id"] == M.PERSON_ID_ATTR_UID), None)
    if person is None:
        problems.append(f"person_id attribute {M.PERSON_ID_ATTR_UID} is not on the program")
    elif not person.get("unique"):
        # A server setting, not a mapping error, so it does not block the
        # file. send_to_dhis2.py and get_tei_context guard against duplicates
        # in code, but the server itself only refuses them once this is set.
        warnings.append(
            f"attribute {person['name']!r} is not flagged unique. Without that the "
            f"server accepts two participants with the same person_id. Set it in "
            f"Maintenance > Tracked entity attribute > AI-READI Person ID > Unique."
        )

    attribute_uid_by_field = {}
    for field, name in ATTRIBUTE_NAMES.items():
        attr = attrs.get(norm(name))
        if attr is None:
            problems.append(f"attribute for {field}: no attribute named {name!r}")
        else:
            attribute_uid_by_field[field] = attr["id"]

    stage_uids, de_by_stage = {}, {}
    for key, name in STAGE_NAMES.items():
        stage = stages.get(norm(name))
        if stage is None:
            problems.append(f"stage {key}: no stage named {name!r}")
            continue
        stage_uids[key] = stage["id"]
        de_by_stage[key] = {
            norm(p["dataElement"]["name"]): p["dataElement"]["id"]
            for p in stage.get("programStageDataElements", [])
        }

    data_element_uid_by_field = {}
    for key, fields in STAGE_FIELDS.items():
        for field in fields:
            name = DATA_ELEMENT_NAMES.get(field)
            if name is None:
                problems.append(f"data element for {field}: not in DATA_ELEMENT_NAMES")
                continue
            uid = de_by_stage.get(key, {}).get(norm(name))
            if uid is None:
                problems.append(f"data element for {field}: {name!r} is not on stage {key}")
            else:
                data_element_uid_by_field[field] = uid

    lookup = {
        "program_uid": program["id"],
        "tracked_entity_type_uid": (program.get("trackedEntityType") or {}).get("id"),
        "person_id_attribute_uid": M.PERSON_ID_ATTR_UID,
        "stage_uids": stage_uids,
        "attribute_uid_by_field": attribute_uid_by_field,
        "data_element_uid_by_field": data_element_uid_by_field,
    }
    if not lookup["tracked_entity_type_uid"]:
        problems.append("program has no tracked entity type")
    return lookup, problems, warnings


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", default="uid_lookup.json")
    parser.add_argument("--inventory", action="store_true",
                        help="print the program's attributes, stages and data elements")
    args, _ = parser.parse_known_args()

    session = dhis2.get_session(read_only=True)
    program = fetch_program(session)

    if args.inventory:
        print_inventory(program)
        return 0

    lookup, problems, warnings = resolve(program)
    for line in warnings:
        print(f"WARNING: {line}\n")
    if problems:
        print(f"{len(problems)} name(s) did not resolve. Nothing written.\n")
        for line in problems:
            print(f"  {line}")
        print("\nRun with --inventory to see the server's names, then fix the "
              "mapping tables at the top of this script.")
        return 1

    with open(args.out, "w") as fh:
        json.dump(lookup, fh, indent=2)
    print(f"Wrote {args.out}: {len(lookup['attribute_uid_by_field'])} attributes, "
          f"{len(lookup['stage_uids'])} stages, "
          f"{len(lookup['data_element_uid_by_field'])} data elements.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
