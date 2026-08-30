"""
Adds threshold/status/timestamp fields to the existing Heart Rate,
Respiratory Rate, and SpO2 stages.

Thresholds used (cited sources):
  - Heart Rate: <60 / 60-100 / >100 bpm
      American Heart Association - normal resting HR is 60-100 bpm
      https://www.heart.org/en/health-topics/high-blood-pressure/the-facts-about-high-blood-pressure/all-about-heart-rate-pulse
  - Respiratory Rate: <12 / 12-20 / >20 breaths/min
      American Lung Association - normal adult resting RR is 12-20 breaths/min
      https://www.lung.org/blog/respiratory-rate-vital-signs
  - SpO2: 95-100% (normal) / 90-94% (mild low) / <90% (marked low, WHO
    hypoxemia threshold, widely cited in respiratory research)

Data Sufficiency rule (project-design decision, not a clinical standard):
  0 valid readings -> No valid data
  1-2 valid readings -> Limited
  3+ valid readings -> Sufficient

Run once. Prints all new field UIDs at the end - needed for the backfill script.
"""

import requests
import json

DHIS2_URL = 'https://t2d-registry.plhi.us'
ADMIN_USER = 'admin'
ADMIN_PASS = 'REPLACE_ME'

# Existing stage UIDs (from hourly_step2_final.py)
STAGE_UIDS = {
    'Wearable – Heart Rate': 'XB29GdXrNDb',
    'Wearable – Respiratory Rate': 'ZHqSqHOv8is',
    'Wearable – SpO2': 'QoigcBfYCcG',
}

session = requests.Session()
session.auth = (ADMIN_USER, ADMIN_PASS)
headers = {'Content-Type': 'application/json'}


def create_option_set(name, options):
    """Creates an option set with the given list of option names. Returns its UID."""
    option_payload = {
        'options': [
            {'name': opt, 'code': opt.upper().replace(' ', '_').replace('-', '_')}
            for opt in options
        ]
    }
    resp = session.post(f'{DHIS2_URL}/api/metadata', headers=headers,
                         data=json.dumps(option_payload))
    print(f"  Options for '{name}': {resp.status_code}")

    option_lookup = session.get(
        f'{DHIS2_URL}/api/options',
        params={'filter': f'name:in:[{",".join(options)}]', 'fields': 'id,name'}
    ).json()
    option_ids = {o['name']: o['id'] for o in option_lookup.get('options', [])}

    optionset_payload = {
        'optionSets': [{
            'name': name,
            'valueType': 'TEXT',
            'options': [{'id': option_ids[opt]} for opt in options if opt in option_ids],
        }]
    }
    resp2 = session.post(f'{DHIS2_URL}/api/metadata', headers=headers,
                          data=json.dumps(optionset_payload))
    print(f"  Option set '{name}': {resp2.status_code}")

    os_lookup = session.get(
        f'{DHIS2_URL}/api/optionSets',
        params={'filter': f'name:eq:{name}', 'fields': 'id,name'}
    ).json()
    os_id = os_lookup['optionSets'][0]['id'] if os_lookup.get('optionSets') else None
    print(f"  Option set UID: {os_id}")
    return os_id


def create_data_elements(defs):
    """defs: list of (name, valueType, aggregationType, optionSetUid_or_None). Returns {name: uid}."""
    des = []
    for name, vtype, agg, os_uid in defs:
        de = {
            'name': name, 'shortName': name[:50], 'domainType': 'TRACKER',
            'valueType': vtype, 'aggregationType': agg,
        }
        if os_uid:
            de['optionSet'] = {'id': os_uid}
        des.append(de)

    resp = session.post(f'{DHIS2_URL}/api/metadata', headers=headers,
                         data=json.dumps({'dataElements': des}))
    print(f"  Data elements: {resp.status_code}")
    if resp.status_code not in (200, 201):
        print(f"  ERROR: {resp.text[:500]}")

    names = [d[0] for d in defs]
    lookup = session.get(
        f'{DHIS2_URL}/api/dataElements',
        params={'filter': f'name:in:[{",".join(names)}]', 'fields': 'id,name'}
    ).json()
    return {de['name']: de['id'] for de in lookup.get('dataElements', [])}


def attach_to_stage(stage_uid, field_uids_ordered):
    """field_uids_ordered: list of (fieldKey, uid) - appends any not already present."""
    stage_full = session.get(f'{DHIS2_URL}/api/programStages/{stage_uid}', params={'fields': '*'}).json()
    existing_uids = {pde['dataElement']['id'] for pde in stage_full['programStageDataElements']}
    max_sort = max((pde['sortOrder'] for pde in stage_full['programStageDataElements']), default=0)

    added = 0
    for _, uid in field_uids_ordered:
        if uid and uid not in existing_uids:
            max_sort += 1
            stage_full['programStageDataElements'].append({
                'dataElement': {'id': uid}, 'compulsory': False, 'sortOrder': max_sort,
            })
            added += 1

    if added == 0:
        print("  all fields already attached, skipping")
        return

    resp = session.put(f'{DHIS2_URL}/api/programStages/{stage_uid}',
                        headers=headers, data=json.dumps(stage_full))
    print(f"  Attached {added} fields: {resp.status_code}")


# ============================================================
# Step 1: shared option sets
# ============================================================
print("=== Creating Data Sufficiency option set (shared across all 3) ===")
sufficiency_os = create_option_set(
    'Data Sufficiency',
    ['Sufficient', 'Limited', 'No valid data']
)

print("\n=== Creating HR/RR Hourly Status option set (shared) ===")
hr_rr_status_os = create_option_set(
    'HR RR Hourly Threshold Status',
    ['Within range', 'Low readings present', 'High readings present',
     'Both low and high readings present', 'Insufficient data']
)

print("\n=== Creating SpO2 Hourly Status option set ===")
spo2_status_os = create_option_set(
    'SpO2 Hourly Threshold Status',
    ['Expected range only', 'Mild-low readings present', 'Marked-low readings present',
     'Both mild-low and marked-low readings present', 'Insufficient data']
)

# ============================================================
# Step 2: Heart Rate fields
# ============================================================
print("\n=== Creating Heart Rate fields ===")
hr_defs = [
    ('HR Standard Deviation', 'NUMBER', 'NONE', None),
    ('HR Low Reading Count (<60)', 'INTEGER', 'SUM', None),
    ('HR High Reading Count (>100)', 'INTEGER', 'SUM', None),
    ('HR Hourly Status', 'TEXT', 'NONE', hr_rr_status_os),
    ('HR Data Sufficiency', 'TEXT', 'NONE', sufficiency_os),
    ('HR Low Reading Timestamps', 'LONG_TEXT', 'NONE', None),
    ('HR High Reading Timestamps', 'LONG_TEXT', 'NONE', None),
]
hr_uids = create_data_elements(hr_defs)
print(f"HR field UIDs: {hr_uids}")

attach_to_stage(STAGE_UIDS['Wearable – Heart Rate'], list(hr_uids.items()))

# ============================================================
# Step 3: Respiratory Rate fields
# ============================================================
print("\n=== Creating Respiratory Rate fields ===")
rr_defs = [
    ('RR Standard Deviation', 'NUMBER', 'NONE', None),
    ('RR Low Reading Count (<12)', 'INTEGER', 'SUM', None),
    ('RR High Reading Count (>20)', 'INTEGER', 'SUM', None),
    ('RR Hourly Status', 'TEXT', 'NONE', hr_rr_status_os),
    ('RR Data Sufficiency', 'TEXT', 'NONE', sufficiency_os),
    ('RR Low Reading Timestamps', 'LONG_TEXT', 'NONE', None),
    ('RR High Reading Timestamps', 'LONG_TEXT', 'NONE', None),
]
rr_uids = create_data_elements(rr_defs)
print(f"RR field UIDs: {rr_uids}")

attach_to_stage(STAGE_UIDS['Wearable – Respiratory Rate'], list(rr_uids.items()))

# ============================================================
# Step 4: SpO2 fields
# ============================================================
print("\n=== Creating SpO2 fields ===")
spo2_defs = [
    ('SpO2 Standard Deviation', 'NUMBER', 'NONE', None),
    ('SpO2 Mild Low Count (90-94)', 'INTEGER', 'SUM', None),
    ('SpO2 Marked Low Count (<90)', 'INTEGER', 'SUM', None),
    ('SpO2 Hourly Status', 'TEXT', 'NONE', spo2_status_os),
    ('SpO2 Data Sufficiency', 'TEXT', 'NONE', sufficiency_os),
    ('SpO2 Mild Low Reading Timestamps', 'LONG_TEXT', 'NONE', None),
    ('SpO2 Marked Low Reading Timestamps', 'LONG_TEXT', 'NONE', None),
]
spo2_uids = create_data_elements(spo2_defs)
print(f"SpO2 field UIDs: {spo2_uids}")

attach_to_stage(STAGE_UIDS['Wearable – SpO2'], list(spo2_uids.items()))

# ============================================================
# Final summary
# ============================================================
print("\n\n=== ALL FIELD UIDS (save this for the backfill script) ===")
print(json.dumps({
    'Heart Rate': hr_uids,
    'Respiratory Rate': rr_uids,
    'SpO2': spo2_uids,
}, indent=2))
