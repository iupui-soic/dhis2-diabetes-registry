"""
Adds "Low Reading Timestamps" and "High Reading Timestamps" fields to the
existing CGM - Glucose stage, matching the pattern used for HR/RR/SpO2.

These capture the exact time(s) - with seconds - when the Dexcom sensor
reported "Low" or "High" (readings outside its measurable range), matching
the existing Low Reading Count / High Reading Count fields.

Run once. Prints the two new UIDs at the end - needed for the backfill script.
"""

import requests
import json

DHIS2_URL = 'https://t2d-registry.plhi.us'
ADMIN_USER = 'admin'
ADMIN_PASS = 'REPLACE_ME'

GLUCOSE_STAGE_UID = 'SS7a20eCnBZ'

session = requests.Session()
session.auth = (ADMIN_USER, ADMIN_PASS)
headers = {'Content-Type': 'application/json'}

de_payload = {
    'dataElements': [
        {
            'name': 'Glucose Low Reading Timestamps',
            'shortName': 'Glucose Low Timestamps',
            'domainType': 'TRACKER',
            'valueType': 'LONG_TEXT',
            'aggregationType': 'NONE',
        },
        {
            'name': 'Glucose High Reading Timestamps',
            'shortName': 'Glucose High Timestamps',
            'domainType': 'TRACKER',
            'valueType': 'LONG_TEXT',
            'aggregationType': 'NONE',
        },
    ]
}
resp = session.post(f'{DHIS2_URL}/api/metadata', headers=headers, data=json.dumps(de_payload))
print("Data elements:", resp.status_code)
print(resp.text[:400])

lookup = session.get(
    f'{DHIS2_URL}/api/dataElements',
    params={'filter': 'name:in:[Glucose Low Reading Timestamps,Glucose High Reading Timestamps]',
            'fields': 'id,name'}
).json()
de_map = {de['name']: de['id'] for de in lookup.get('dataElements', [])}
print("DE UIDs:", de_map)

LOW_TS_DE = de_map.get('Glucose Low Reading Timestamps')
HIGH_TS_DE = de_map.get('Glucose High Reading Timestamps')

if not (LOW_TS_DE and HIGH_TS_DE):
    print("STOP - could not find both new data elements")
    exit(1)

stage_full = session.get(f'{DHIS2_URL}/api/programStages/{GLUCOSE_STAGE_UID}', params={'fields': '*'}).json()
existing_uids = {pde['dataElement']['id'] for pde in stage_full['programStageDataElements']}
max_sort = max((pde['sortOrder'] for pde in stage_full['programStageDataElements']), default=0)

added = 0
for uid in [LOW_TS_DE, HIGH_TS_DE]:
    if uid not in existing_uids:
        max_sort += 1
        stage_full['programStageDataElements'].append({
            'dataElement': {'id': uid}, 'compulsory': False, 'sortOrder': max_sort,
        })
        added += 1

if added > 0:
    resp2 = session.put(f'{DHIS2_URL}/api/programStages/{GLUCOSE_STAGE_UID}',
                         headers=headers, data=json.dumps(stage_full))
    print(f"Attached {added} fields: {resp2.status_code}")
else:
    print("Already attached")

print(f"\n\n=== SAVE THESE ===\nLOW_TS_DE = '{LOW_TS_DE}'\nHIGH_TS_DE = '{HIGH_TS_DE}'")
