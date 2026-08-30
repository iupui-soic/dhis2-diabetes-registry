"""
Creates the final 8 hourly-summary Program Stages:
  - 4 continuous metrics (Heart Rate, Respiratory Rate, SpO2, Stress) share
    ONE generic set of data elements (Mean Value, Minimum Value, Maximum
    Value, Reading Count) - reduces metadata from ~16 to 4 elements for
    these, since all 4 are structurally identical (dense point-in-time
    readings, confirmed via pilot: 17-60 readings/hour).
  - Sleep and Activity keep their own dedicated, semantically-correct
    elements (duration-minutes and steps-sum respectively) rather than
    forcing mean/min/max/count onto data where that doesn't apply.
  - Glucose gets its own dedicated, clinically-richer element set
    (mean/min/max/count/SD/TIR/TAR/TBR/high/low) since it's structurally
    different from the others and needs standard CGM metrics.

Naming follows the cleaner "Wearable – X" / "CGM – X" convention.

Run once. Prints all UIDs at the end - SAVE THESE for the import script.
"""

import requests
import json

DHIS2_URL = 'https://t2d-registry.plhi.us'
ADMIN_USER = 'admin'
ADMIN_PASS = 'REPLACE_ME'
PROGRAM_UID = 'W3LSFZH3UDq'

session = requests.Session()
session.auth = (ADMIN_USER, ADMIN_PASS)
headers = {'Content-Type': 'application/json'}


def create_data_elements(defs):
    """defs: list of (name, valueType, aggregationType). Returns {name: uid}."""
    payload = {
        'dataElements': [
            {'name': name, 'shortName': name[:50], 'domainType': 'TRACKER',
             'valueType': vtype, 'aggregationType': agg}
            for name, vtype, agg in defs
        ]
    }
    resp = session.post(f'{DHIS2_URL}/api/metadata', headers=headers, data=json.dumps(payload))
    print(f"  Data elements created: {resp.status_code}")
    if resp.status_code not in (200, 201):
        print(f"  ERROR: {resp.text[:400]}")
        return {}
    names = [d[0] for d in defs]
    lookup = session.get(
        f'{DHIS2_URL}/api/dataElements',
        params={'filter': f'name:in:[{",".join(names)}]', 'fields': 'id,name'},
    ).json()
    return {de['name']: de['id'] for de in lookup.get('dataElements', [])}


def create_stage(stage_name, field_uids_ordered):
    """field_uids_ordered: list of (field_key, de_uid) tuples in display order."""
    payload = {
        'programStages': [{
            'name': stage_name,
            'program': {'id': PROGRAM_UID},
            'repeatable': True,
            'featureType': 'NONE',
            'programStageDataElements': [
                {'dataElement': {'id': uid}, 'compulsory': False, 'sortOrder': i + 1}
                for i, (_, uid) in enumerate(field_uids_ordered)
            ],
        }]
    }
    resp = session.post(f'{DHIS2_URL}/api/metadata', headers=headers, data=json.dumps(payload))
    print(f"  Stage created: {resp.status_code}")
    if resp.status_code not in (200, 201):
        print(f"  ERROR: {resp.text[:400]}")
        return None
    lookup = session.get(
        f'{DHIS2_URL}/api/programStages',
        params={'filter': f'name:eq:{stage_name}', 'fields': 'id,name'},
    ).json()
    return lookup['programStages'][0]['id']


all_uids = {}

# ---- Step 1: shared generic elements for the 4 continuous metrics ----
print("=== Creating shared generic elements (Mean/Min/Max/Count) ===")
shared_defs = [
    ('Mean Value', 'NUMBER', 'AVERAGE'),
    ('Minimum Value', 'NUMBER', 'MIN'),
    ('Maximum Value', 'NUMBER', 'MAX'),
    ('Reading Count', 'INTEGER', 'SUM'),
]
shared_uids = create_data_elements(shared_defs)
print(f"  Shared UIDs: {shared_uids}")

shared_field_order = [
    ('mean', shared_uids.get('Mean Value')),
    ('min', shared_uids.get('Minimum Value')),
    ('max', shared_uids.get('Maximum Value')),
    ('count', shared_uids.get('Reading Count')),
]

for stage_name in ['Wearable – Heart Rate', 'Wearable – Respiratory Rate',
                    'Wearable – SpO2', 'Wearable – Stress']:
    print(f"\n=== Creating stage: {stage_name} ===")
    stage_uid = create_stage(stage_name, shared_field_order)
    print(f"  Stage UID: {stage_uid}")
    all_uids[stage_name] = {
        'stage': stage_uid,
        'fields': {k: v for k, v in shared_field_order},
    }

# ---- Step 2: Sleep (RAW SEGMENTS - one event per stage transition, not hourly) ----
print("\n=== Creating stage: Wearable – Sleep ===")
sleep_defs = [
    ('Sleep Stage', 'TEXT', 'NONE'),
    ('Sleep Segment Duration Minutes', 'NUMBER', 'SUM'),
]
sleep_uids = create_data_elements(sleep_defs)
sleep_field_order = [
    ('stage', sleep_uids.get('Sleep Stage')),
    ('duration_minutes', sleep_uids.get('Sleep Segment Duration Minutes')),
]
sleep_stage_uid = create_stage('Wearable – Sleep', sleep_field_order)
print(f"  Stage UID: {sleep_stage_uid}")
all_uids['Wearable – Sleep'] = {'stage': sleep_stage_uid, 'fields': dict(sleep_field_order)}

# ---- Step 3: Activity (dedicated, sum + count) ----
print("\n=== Creating stage: Wearable – Activity ===")
activity_defs = [
    ('Steps Sum', 'NUMBER', 'SUM'),
    ('Steps Reading Count', 'INTEGER', 'SUM'),
]
activity_uids = create_data_elements(activity_defs)
activity_field_order = [
    ('sum', activity_uids.get('Steps Sum')),
    ('count', activity_uids.get('Steps Reading Count')),
]
activity_stage_uid = create_stage('Wearable – Activity', activity_field_order)
print(f"  Stage UID: {activity_stage_uid}")
all_uids['Wearable – Activity'] = {'stage': activity_stage_uid, 'fields': dict(activity_field_order)}

# ---- Step 4: Calories (dedicated, sum + count) ----
print("\n=== Creating stage: Wearable – Calories ===")
calorie_defs = [
    ('Calories Sum', 'NUMBER', 'SUM'),
    ('Calories Reading Count', 'INTEGER', 'SUM'),
]
calorie_uids = create_data_elements(calorie_defs)
calorie_field_order = [
    ('sum', calorie_uids.get('Calories Sum')),
    ('count', calorie_uids.get('Calories Reading Count')),
]
calorie_stage_uid = create_stage('Wearable – Calories', calorie_field_order)
print(f"  Stage UID: {calorie_stage_uid}")
all_uids['Wearable – Calories'] = {'stage': calorie_stage_uid, 'fields': dict(calorie_field_order)}

# ---- Step 5: Glucose (dedicated, rich clinical metrics) ----
print("\n=== Creating stage: CGM – Glucose ===")
glucose_defs = [
    ('Glucose Mean', 'NUMBER', 'AVERAGE'),
    ('Glucose Minimum', 'NUMBER', 'MIN'),
    ('Glucose Maximum', 'NUMBER', 'MAX'),
    ('Glucose Reading Count', 'INTEGER', 'SUM'),
    ('Glucose Standard Deviation', 'NUMBER', 'NONE'),
    ('Time in Range Percent', 'NUMBER', 'AVERAGE'),
    ('Time Above Range Percent', 'NUMBER', 'AVERAGE'),
    ('Time Below Range Percent', 'NUMBER', 'AVERAGE'),
    ('High Reading Count', 'INTEGER', 'SUM'),
    ('Low Reading Count', 'INTEGER', 'SUM'),
]
glucose_uids = create_data_elements(glucose_defs)
glucose_field_order = [
    ('mean', glucose_uids.get('Glucose Mean')),
    ('min', glucose_uids.get('Glucose Minimum')),
    ('max', glucose_uids.get('Glucose Maximum')),
    ('count', glucose_uids.get('Glucose Reading Count')),
    ('sd', glucose_uids.get('Glucose Standard Deviation')),
    ('tir_pct', glucose_uids.get('Time in Range Percent')),
    ('tar_pct', glucose_uids.get('Time Above Range Percent')),
    ('tbr_pct', glucose_uids.get('Time Below Range Percent')),
    ('high_count', glucose_uids.get('High Reading Count')),
    ('low_count', glucose_uids.get('Low Reading Count')),
]
glucose_stage_uid = create_stage('CGM – Glucose', glucose_field_order)
print(f"  Stage UID: {glucose_stage_uid}")
all_uids['CGM – Glucose'] = {'stage': glucose_stage_uid, 'fields': dict(glucose_field_order)}

print("\n\n=== ALL UIDS (save this - needed for the import script) ===")
print(json.dumps(all_uids, indent=2))
