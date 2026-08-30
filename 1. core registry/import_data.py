import pandas as pd
import json
import requests
from datetime import date

# ---- Configuration ----
DHIS2_URL = "http://localhost:8080"
DHIS2_USER = "admin"
DHIS2_PASSWORD = "district"

PROJECT_DIR = "/home/ainaperu/diabetes_registry_project"
CSV_PATH = f"{PROJECT_DIR}/registry_master_v3.csv"
LOOKUP_PATH = f"{PROJECT_DIR}/uid_lookup.json"

ORG_UNIT_MAP = {
    "UAB": "jZmnwTrmXnR",
    "UCSD": "mPQv3n3GQDK",
    "UW": "dFnKYalCXGc",
}
PLACEHOLDER_DATE = "2023-06-01"

BOOLEAN_FIELDS = {
    "mhterm_dm2", "mhterm_predm", "fh_dm2pt", "fh_dm2sb",
    "mhoccur_hbp", "mhoccur_clsh", "mhoccur_obs", "mhoccur_mi",
    "mhoccur_strk", "mhoccur_circ", "mhoccur_pdr",
    "sualckncf", "susmkncf", "susmkcdur",
}

with open(LOOKUP_PATH) as f:
    lookup = json.load(f)

df = pd.read_csv(CSV_PATH)
print(f"Loaded {len(df)} participants, {len(df.columns)} columns")

TET_UID = lookup["tracked_entity_type_uid"]
PROGRAM_UID = lookup["program_uid"]
STAGE_UIDS = lookup["stage_uids"]
ATTR_UIDS = lookup["attribute_uid_by_field"]
DE_UIDS = lookup["data_element_uid_by_field"]

def clean_value(field, val):
    if pd.isna(val):
        return None
    if field in BOOLEAN_FIELDS:
        return "true" if float(val) == 1.0 else "false"
    if isinstance(val, float) and val.is_integer():
        return str(int(val))
    return str(val)

def build_tei_payload(row):
    org_unit = ORG_UNIT_MAP.get(row.get("clinical_site"))
    if org_unit is None:
        return None

    attributes = []
    attributes.append({"attribute": "oFbmOHnKYaX", "value": str(int(row["person_id"]))})
    for field, uid in ATTR_UIDS.items():
        val = clean_value(field, row.get(field))
        if val is not None:
            attributes.append({"attribute": uid, "value": val})

    stage_fields = {
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

    events = []
    for stage_key, fields in stage_fields.items():
        data_values = []
        for field in fields:
            val = clean_value(field, row.get(field))
            if val is not None:
                data_values.append({"dataElement": DE_UIDS[field], "value": val})
        if data_values:
            events.append({
                "programStage": STAGE_UIDS[stage_key],
                "orgUnit": org_unit,
                "occurredAt": PLACEHOLDER_DATE,
                "status": "COMPLETED",
                "dataValues": data_values,
            })

    return {
        "trackedEntityType": TET_UID,
        "orgUnit": org_unit,
        "attributes": attributes,
        "enrollments": [{
            "orgUnit": org_unit,
            "program": PROGRAM_UID,
            "enrolledAt": PLACEHOLDER_DATE,
            "occurredAt": PLACEHOLDER_DATE,
            "status": "COMPLETED",
            "events": events,
        }],
    }

payloads = []
skipped = 0
for _, row in df.iterrows():
    tei = build_tei_payload(row)
    if tei is None:
        skipped += 1
    else:
        payloads.append(tei)

print(f"Built {len(payloads)} participant records, skipped {skipped} (no recognized site)")

with open(f"{PROJECT_DIR}/import_payload.json", "w") as f:
    json.dump({"trackedEntities": payloads}, f, indent=2)

print("Saved import_payload.json - inspect it before running the actual import step")
