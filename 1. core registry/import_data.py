#!/usr/bin/env python3
"""Build the tracked entity import payload from registry_master_v3.csv.

Writes import_payload.json for send_to_dhis2.py to post. Inspect the payload
before importing.

Credentials are not needed here: this step only reads the CSV and the UID
lookup, and writes a file.

NOTE ON DATES
-------------
registry_master_v3.csv carries no visit date, so every enrollment and event
was dated with a placeholder. That placeholder is now sourced from
participants.tsv where AIREADI_ROOT is available, and falls back to the old
constant only with a warning, so nobody mistakes a synthetic date for a real
one.
"""

import argparse
import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import registry_fields  # noqa: E402

ORG_UNIT_MAP = {
    "UAB": "jZmnwTrmXnR",
    "UCSD": "mPQv3n3GQDK",
    "UW": "dFnKYalCXGc",
}

FALLBACK_DATE = "2023-06-01"
PERSON_ID_ATTR_UID = "oFbmOHnKYaX"

STAGE_FIELDS = {
    "vitals": ["bp1_sysbp_vsorres", "bp1_diabp_vsorres", "pulse_vsorres",
               "height_vsorres", "weight_vsorres", "bmi_vsorres",
               "waist_vsorres", "hip_vsorres", "whr_vsorres"],
    "survey": ["mhoccur_hbp", "mhoccur_clsh", "mhoccur_obs", "mhoccur_mi",
               "mhoccur_strk", "mhoccur_circ", "mhoccur_pdr", "dmlact",
               "sualckncf", "susmkncf", "susmkcdur",
               "pxfi1", "pxfi2", "pxfi3", "pxfi4", "pxfi5"],
    "cgm": ["average_glucose_level_mg_dl", "glucose_level_record_count",
            "glucose_sensor_sampling_duration_days"],
}


def load_visit_dates():
    """Real study visit dates from participants.tsv, when the dataset is reachable."""
    if not os.environ.get("AIREADI_ROOT"):
        return {}
    try:
        from common import aireadi
        participants = pd.read_csv(aireadi.participants_file(), sep="\t")
    except (SystemExit, OSError, FileNotFoundError):
        return {}
    if "study_visit_date" not in participants.columns:
        return {}
    participants["person_id"] = participants["person_id"].astype(str)
    return {
        pid: str(date)
        for pid, date in zip(participants["person_id"], participants["study_visit_date"])
        if pd.notna(date)
    }


def build_tei(row, lookup, visit_dates):
    org_unit = ORG_UNIT_MAP.get(row.get("clinical_site"))
    if org_unit is None:
        return None, "no recognised clinical site"

    person_id = str(int(row["person_id"]))
    occurred = visit_dates.get(person_id, FALLBACK_DATE)

    attributes = [{"attribute": PERSON_ID_ATTR_UID, "value": person_id}]
    for field, uid in lookup["attribute_uid_by_field"].items():
        value = registry_fields.clean_value(field, row.get(field))
        if value is not None:
            attributes.append({"attribute": uid, "value": value})

    de_uids = lookup["data_element_uid_by_field"]
    events = []
    for stage_key, fields in STAGE_FIELDS.items():
        data_values = []
        for field in fields:
            uid = de_uids.get(field)
            if uid is None:
                continue
            value = registry_fields.clean_value(field, row.get(field))
            if value is not None:
                data_values.append({"dataElement": uid, "value": value})
        if data_values:
            events.append({
                "programStage": lookup["stage_uids"][stage_key],
                "orgUnit": org_unit,
                "occurredAt": occurred,
                "status": "COMPLETED",
                "dataValues": data_values,
            })

    return {
        "trackedEntityType": lookup["tracked_entity_type_uid"],
        "orgUnit": org_unit,
        "attributes": attributes,
        "enrollments": [{
            "orgUnit": org_unit,
            "program": lookup["program_uid"],
            "enrolledAt": occurred,
            "occurredAt": occurred,
            "status": "COMPLETED",
            "events": events,
        }],
    }, None


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--csv", default="registry_master_v3.csv")
    parser.add_argument("--lookup", default="uid_lookup.json")
    parser.add_argument("--out", default="import_payload.json")
    args, _ = parser.parse_known_args()

    with open(args.lookup) as fh:
        lookup = json.load(fh)

    missing = [f for f in sum(STAGE_FIELDS.values(), [])
               if f not in lookup["data_element_uid_by_field"]]
    if missing:
        print(f"WARNING: {len(missing)} fields have no data element UID and will be "
              f"skipped: {missing}")

    df = pd.read_csv(args.csv)
    visit_dates = load_visit_dates()
    if visit_dates:
        print(f"Loaded {len(visit_dates)} real study visit dates from participants.tsv")
    else:
        print(f"WARNING: no participants.tsv available, so every enrollment and event "
              f"will carry the placeholder date {FALLBACK_DATE}. Set AIREADI_ROOT to "
              f"use real visit dates.")

    print(f"Loaded {len(df)} participants, {len(df.columns)} columns")

    payloads, skipped = [], {}
    for _, row in df.iterrows():
        tei, reason = build_tei(row, lookup, visit_dates)
        if tei is None:
            skipped[reason] = skipped.get(reason, 0) + 1
        else:
            payloads.append(tei)

    print(f"Built {len(payloads)} participant records")
    for reason, count in skipped.items():
        print(f"  skipped {count}: {reason}")

    with open(args.out, "w") as fh:
        json.dump({"trackedEntities": payloads}, fh, indent=2)
    print(f"\nWrote {args.out}. Inspect it before running send_to_dhis2.py.")


if __name__ == "__main__":
    main()
