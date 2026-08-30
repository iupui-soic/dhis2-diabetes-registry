"""
Step 1:
  - Renames 4 existing glucose fields for clarity (device-only vs combined
    clinical threshold), WITHOUT touching any stored data - renaming a
    data element only changes its display label.
  - Creates 2 new fields: Above Range Count (>180) and Below Range Count
    (<70), to pair with the (already renamed) Above/Below Range Timestamps.

Run once. Prints the 2 new count field UIDs at the end.
"""

import requests
import json

DHIS2_URL = 'https://t2d-registry.plhi.us'
ADMIN_USER = 'admin'
ADMIN_PASS = 'REPLACE_ME'

GLUCOSE_STAGE_UID = 'SS7a20eCnBZ'

# Existing field UIDs to rename
RZpt03lifR6 = 'RZpt03lifR6'  # currently "High Reading Count"
hi1TAwCqN0f = 'hi1TAwCqN0f'  # currently "Low Reading Count"
Zu4iFxtthSU = 'Zu4iFxtthSU'  # currently "Glucose High Reading Timestamps"
LfzwHxQUotL = 'LfzwHxQUotL'  # currently "Glucose Low Reading Timestamps"

session = requests.Session()
session.auth = (ADMIN_USER, ADMIN_PASS)
headers = {'Content-Type': 'application/json'}


def rename_data_element(uid, new_name, new_short_name):
    de = session.get(f'{DHIS2_URL}/api/dataElements/{uid}', params={'fields': '*'}).json()
    de['name'] = new_name
    de['shortName'] = new_short_name[:50]
    resp = session.put(f'{DHIS2_URL}/api/dataElements/{uid}', headers=headers, data=json.dumps(de))
    print(f"  Renamed {uid} -> '{new_name}': {resp.status_code}")


print("=== Renaming existing fields ===")
rename_data_element(RZpt03lifR6, 'Device High Count', 'Device High Count')
rename_data_element(hi1TAwCqN0f, 'Device Low Count', 'Device Low Count')
rename_data_element(Zu4iFxtthSU, 'Above Range Timestamps (>180 mg/dL)', 'Above Range Timestamps')
rename_data_element(LfzwHxQUotL, 'Below Range Timestamps (<70 mg/dL)', 'Below Range Timestamps')

print("\n=== Creating 2 new count fields ===")
de_payload = {
    'dataElements': [
        {
            'name': 'Above Range Count (>180 mg/dL)',
            'shortName': 'Above Range Count',
            'domainType': 'TRACKER',
            'valueType': 'INTEGER',
            'aggregationType': 'SUM',
        },
        {
            'name': 'Below Range Count (<70 mg/dL)',
            'shortName': 'Below Range Count',
            'domainType': 'TRACKER',
            'valueType': 'INTEGER',
            'aggregationType': 'SUM',
        },
    ]
}
resp = session.post(f'{DHIS2_URL}/api/metadata', headers=headers, data=json.dumps(de_payload))
print("Data elements:", resp.status_code)
print(resp.text[:400])

lookup = session.get(
    f'{DHIS2_URL}/api/dataElements',
    params={'filter': 'name:in:[Above Range Count (>180 mg/dL),Below Range Count (<70 mg/dL)]',
            'fields': 'id,name'}
).json()
de_map = {de['name']: de['id'] for de in lookup.get('dataElements', [])}
print("New DE UIDs:", de_map)

ABOVE_COUNT_DE = de_map.get('Above Range Count (>180 mg/dL)')
BELOW_COUNT_DE = de_map.get('Below Range Count (<70 mg/dL)')

if not (ABOVE_COUNT_DE and BELOW_COUNT_DE):
    print("STOP - could not find both new data elements")
    exit(1)

stage_full = session.get(f'{DHIS2_URL}/api/programStages/{GLUCOSE_STAGE_UID}', params={'fields': '*'}).json()
existing_uids = {pde['dataElement']['id'] for pde in stage_full['programStageDataElements']}
max_sort = max((pde['sortOrder'] for pde in stage_full['programStageDataElements']), default=0)

added = 0
for uid in [ABOVE_COUNT_DE, BELOW_COUNT_DE]:
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

print(f"\n\n=== SAVE THESE ===\nABOVE_COUNT_DE = '{ABOVE_COUNT_DE}'\nBELOW_COUNT_DE = '{BELOW_COUNT_DE}'")
