"""
Creates the "Cardiac – 12-Lead ECG" Program Stage.

One event per ECG recording (repeatable stage, since 6 participants have 2
recordings each). Not hourly - this is a single point-in-time clinical
measurement, same pattern as Vitals & Measurements / Health & Lifestyle Survey.

Thresholds used (cited sources):
  - Heart Rate: <60 / 60-100 / >100 bpm - American Heart Association
  - PR Interval: <120 / 120-200 / >200 ms - StatPearls/NIH, LITFL, ACC/AHA
  - QRS Duration: <120 ms normal / >=120 ms widened (bundle branch block
    criteria) - StatPearls/NIH
  - QTc: NOT included - correction formula not specified in source data,
    and QTc reference ranges are sex-specific while sex is not currently
    a registry attribute (see project notes)
  - P/QRS/T Axis: numeric only, no status - axis interpretation is
    genuinely context-dependent, no simple defensible cutoff

Run once. Prints all field UIDs at the end - needed for the import script.
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


def create_option_set(name, options):
    option_payload = {
        'options': [
            {'name': opt, 'code': opt.upper().replace(' ', '_').replace('-', '_').replace('(', '').replace(')', '')}
            for opt in options
        ]
    }
    resp = session.post(f'{DHIS2_URL}/api/metadata', headers=headers, data=json.dumps(option_payload))
    print(f"  Options for '{name}': {resp.status_code}")

    option_lookup = session.get(
        f'{DHIS2_URL}/api/options',
        params={'filter': f'name:in:[{",".join(options)}]', 'fields': 'id,name'}
    ).json()
    option_ids = {o['name']: o['id'] for o in option_lookup.get('options', [])}

    optionset_payload = {
        'optionSets': [{
            'name': name, 'valueType': 'TEXT',
            'options': [{'id': option_ids[opt]} for opt in options if opt in option_ids],
        }]
    }
    resp2 = session.post(f'{DHIS2_URL}/api/metadata', headers=headers, data=json.dumps(optionset_payload))
    print(f"  Option set '{name}': {resp2.status_code}")

    os_lookup = session.get(
        f'{DHIS2_URL}/api/optionSets',
        params={'filter': f'name:eq:{name}', 'fields': 'id,name'}
    ).json()
    return os_lookup['optionSets'][0]['id'] if os_lookup.get('optionSets') else None


def create_data_elements(defs):
    """defs: list of (name, valueType, optionSetUid_or_None)"""
    des = []
    for name, vtype, os_uid in defs:
        de = {'name': name, 'shortName': name[:50], 'domainType': 'TRACKER',
              'valueType': vtype, 'aggregationType': 'NONE'}
        if os_uid:
            de['optionSet'] = {'id': os_uid}
        des.append(de)

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


# ============================================================
# Step 1: shared status option set (reused across HR/PR/QRS)
# ============================================================
print("=== Creating status option sets ===")
range_status_os = create_option_set(
    'ECG Reference Range Status',
    ['Below reference range', 'Within reference range', 'Above reference range']
)
qrs_status_os = create_option_set(
    'ECG QRS Widening Status',
    ['Within reference range', 'Widened (meets bundle branch block QRS criteria)']
)

# ============================================================
# Step 2: all data elements
# ============================================================
print("\n=== Creating data elements ===")
defs = [
    ('ECG Study Visit Date', 'TEXT', None),
    ('ECG Validation Date', 'TEXT', None),
    ('ECG Recording Duration (sec)', 'NUMBER', None),
    ('ECG Heart Rate (bpm)', 'INTEGER', None),
    ('ECG Heart Rate Status', 'TEXT', range_status_os),
    ('ECG PR Interval (ms)', 'INTEGER', None),
    ('ECG PR Interval Status', 'TEXT', range_status_os),
    ('ECG QRS Duration (ms)', 'INTEGER', None),
    ('ECG QRS Duration Status', 'TEXT', qrs_status_os),
    ('ECG QT Interval (ms)', 'INTEGER', None),
    ('ECG QTc Interval (ms)', 'INTEGER', None),
    ('ECG P Axis (deg)', 'INTEGER', None),
    ('ECG QRS Axis (deg)', 'INTEGER', None),
    ('ECG T Axis (deg)', 'INTEGER', None),
    ('ECG Participant Position', 'TEXT', None),
    ('ECG Machine Interpretation Status', 'TEXT', None),
    ('ECG Machine Interpretation Summary', 'TEXT', None),
    ('ECG Finding 1', 'TEXT', None),
    ('ECG Finding 1 Detail', 'TEXT', None),
    ('ECG Finding 2', 'TEXT', None),
    ('ECG Finding 2 Detail', 'TEXT', None),
    ('ECG Finding 3', 'TEXT', None),
    ('ECG Finding 3 Detail', 'TEXT', None),
    ('ECG Device', 'TEXT', None),
    ('ECG Sampling Frequency (Hz)', 'NUMBER', None),
    ('ECG Number of Leads', 'INTEGER', None),
    ('ECG Number of Samples', 'INTEGER', None),
    ('ECG Raw Header File Path', 'LONG_TEXT', None),
    ('ECG Raw Data File Path', 'LONG_TEXT', None),
]
field_uids = create_data_elements(defs)
print(f"\nField UIDs: {field_uids}")

# ============================================================
# Step 3: create the stage
# ============================================================
print("\n=== Creating stage: Cardiac – 12-Lead ECG ===")
field_order = [uid for name, _, _ in defs if (uid := field_uids.get(name))]

stage_payload = {
    'programStages': [{
        'name': 'Cardiac – 12-Lead ECG',
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
    params={'filter': 'name:eq:Cardiac – 12-Lead ECG', 'fields': 'id,name'}
).json()
stage_uid = lookup['programStages'][0]['id'] if lookup.get('programStages') else None
print(f"  Stage UID: {stage_uid}")

print("\n\n=== SAVE THIS - needed for the import script ===")
print(f"STAGE_UID = '{stage_uid}'")
print(f"FIELD_UIDS = {json.dumps(field_uids, indent=2)}")
