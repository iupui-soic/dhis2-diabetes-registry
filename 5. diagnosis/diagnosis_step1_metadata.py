"""
Creates the "Diagnosis History" Program Stage.

One event per diagnosed condition per participant (repeatable stage).
Sourced from clinical_data/condition_occurrence.csv (OMOP CDM format).

Note: condition_source_value is truncated at 49 characters in the raw
AI-READI export for longer condition descriptions - a source-data
limitation, imported as-is rather than guessed/reconstructed.

condition_status_source_value and stop_reason were checked and found
100% blank across all 12,375 rows - excluded, no value to import.

Run once. Prints field UIDs at the end - needed for the import script.
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
    des = [
        {'name': name, 'shortName': name[:50], 'domainType': 'TRACKER',
         'valueType': vtype, 'aggregationType': 'NONE'}
        for name, vtype in defs
    ]
    resp = session.post(f'{DHIS2_URL}/api/metadata', headers=headers, data=json.dumps({'dataElements': des}))
    print(f"  Data elements: {resp.status_code}")
    if resp.status_code not in (200, 201):
        print(f"  ERROR: {resp.text[:500]}")
    names = [d[0] for d in defs]
    lookup = session.get(
        f'{DHIS2_URL}/api/dataElements',
        params={'filter': f'name:in:[{",".join(names)}]', 'fields': 'id,name'}
    ).json()
    return {de['name']: de['id'] for de in lookup.get('dataElements', [])}


print("=== Creating data elements ===")
defs = [
    ('Diagnosis Condition Code', 'TEXT'),
    ('Diagnosis Condition Label', 'LONG_TEXT'),
    ('Diagnosis Date', 'TEXT'),
]
field_uids = create_data_elements(defs)
print(f"Field UIDs: {field_uids}\n")

print("=== Creating stage: Diagnosis History ===")
field_order = [field_uids[name] for name, _ in defs]

stage_payload = {
    'programStages': [{
        'name': 'Diagnosis History',
        'program': {'id': PROGRAM_UID},
        'repeatable': True,
        'featureType': 'NONE',
        'programStageDataElements': [
            {'dataElement': {'id': uid}, 'compulsory': False, 'sortOrder': i + 1}
            for i, uid in enumerate(field_order)
        ],
    }]
}
resp = session.post(f'{DHIS2_URL}/api/metadata', headers=headers, data=json.dumps(stage_payload))
print(f"  Stage created: {resp.status_code}")
if resp.status_code not in (200, 201):
    print(f"  ERROR: {resp.text[:500]}")

lookup = session.get(
    f'{DHIS2_URL}/api/programStages',
    params={'filter': 'name:eq:Diagnosis History', 'fields': 'id,name'}
).json()
stage_uid = lookup['programStages'][0]['id'] if lookup.get('programStages') else None
print(f"  Stage UID: {stage_uid}")

print("\n\n=== SAVE THIS - needed for the import script ===")
print(f"STAGE_UID = '{stage_uid}'")
print(f"FIELD_UIDS = {json.dumps(field_uids, indent=2)}")
