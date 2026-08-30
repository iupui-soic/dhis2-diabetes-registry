"""
Adds two new shared data elements - "Hour Start" and "Hour End" - and
attaches them to all 8 existing wearable/CGM Program Stages.

Run this once. It prints the two new UIDs at the end - needed for the
backfill script (hour_backfill_step2.py).
"""

import requests
import json

DHIS2_URL = 'https://t2d-registry.plhi.us'
ADMIN_USER = 'admin'
ADMIN_PASS = 'Londonbridge@2026'

session = requests.Session()
session.auth = (ADMIN_USER, ADMIN_PASS)
headers = {'Content-Type': 'application/json'}

# Import STAGE_UIDS from your existing hourly_step2_final.py, or paste it here
STAGE_UIDS = {
    'Wearable – Heart Rate': {'stage': 'XB29GdXrNDb'},
    'Wearable – Respiratory Rate': {'stage': 'ZHqSqHOv8is'},
    'Wearable – SpO2': {'stage': 'QoigcBfYCcG'},
    'Wearable – Stress': {'stage': 'g803i2FH8bF'},
    'Wearable – Sleep': {'stage': 'aR9APTYYiEe'},
    'Wearable – Activity': {'stage': 'uASeLWkCtRB'},
    'Wearable – Calories': {'stage': 'xfNfR1XEwM8'},
    'CGM – Glucose': {'stage': 'SS7a20eCnBZ'},
}

# --- Step 1: create the two shared data elements ---
de_payload = {
    'dataElements': [
        {
            'name': 'Hour Start',
            'shortName': 'Hour Start',
            'domainType': 'TRACKER',
            'valueType': 'TEXT',
            'aggregationType': 'NONE',
        },
        {
            'name': 'Hour End',
            'shortName': 'Hour End',
            'domainType': 'TRACKER',
            'valueType': 'TEXT',
            'aggregationType': 'NONE',
        },
    ]
}
resp = session.post(f'{DHIS2_URL}/api/metadata', headers=headers, data=json.dumps(de_payload))
print("Data elements:", resp.status_code)
print(resp.text[:400])

lookup = session.get(
    f'{DHIS2_URL}/api/dataElements',
    params={'filter': 'name:in:[Hour Start,Hour End]', 'fields': 'id,name'}
).json()
de_map = {de['name']: de['id'] for de in lookup.get('dataElements', [])}
print("DE UIDs:", de_map)

HOUR_START_DE = de_map.get('Hour Start')
HOUR_END_DE = de_map.get('Hour End')

if not (HOUR_START_DE and HOUR_END_DE):
    print("STOP - could not find both new data elements, check Maintenance app")
    exit(1)

# --- Step 2: attach both fields to all 8 existing stages ---
for stage_name, cfg in STAGE_UIDS.items():
    stage_uid = cfg['stage']
    print(f"\n=== Attaching to: {stage_name} ===")

    stage_full = session.get(f'{DHIS2_URL}/api/programStages/{stage_uid}', params={'fields': '*'}).json()
    existing_uids = {pde['dataElement']['id'] for pde in stage_full['programStageDataElements']}

    if HOUR_START_DE in existing_uids and HOUR_END_DE in existing_uids:
        print("  already attached, skipping")
        continue

    max_sort = max((pde['sortOrder'] for pde in stage_full['programStageDataElements']), default=0)
    if HOUR_START_DE not in existing_uids:
        stage_full['programStageDataElements'].append({
            'dataElement': {'id': HOUR_START_DE}, 'compulsory': False, 'sortOrder': max_sort + 1,
        })
        max_sort += 1
    if HOUR_END_DE not in existing_uids:
        stage_full['programStageDataElements'].append({
            'dataElement': {'id': HOUR_END_DE}, 'compulsory': False, 'sortOrder': max_sort + 1,
        })

    resp = session.put(
        f'{DHIS2_URL}/api/programStages/{stage_uid}',
        headers=headers, data=json.dumps(stage_full)
    )
    print(f"  {resp.status_code} - {resp.text[:200]}")

print(f"\n\n=== SAVE THESE ===\nHOUR_START_DE = '{HOUR_START_DE}'\nHOUR_END_DE = '{HOUR_END_DE}'")
