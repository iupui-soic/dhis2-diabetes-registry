"""
Creates 8 Environment Program Stages. CORRECTED per review:

  - Every stage: Env Mean, Env Minimum, Env Maximum, Env Standard
    Deviation (added), Env Reading Count, Hour Start, Hour End
  - NOx: NO threshold fields. WHO's NO2 guideline does not apply to a
    sensor measuring combined NOx.
  - PM2.5 / PM10: NO per-hour threshold fields. WHO's PM guidelines are
    24-hour MEAN values; applying them to individual hourly readings
    misrepresents the guideline. A genuine 24-hour/daily summary with a
    proper exceedance flag is a planned SEPARATE later addition, built
    from consecutive hourly means once hourly data exists - not part of
    this script.
  - Humidity: Above 50% / Below 30% retained (EPA/ASHRAE indoor comfort
    target range - defensible as stated).
  - Temperature: retained but explicitly labelled "Study Comfort Range",
    not presented as a literal ASHRAE Standard 55 cutoff.
  - PM1, PM4, VOC: no threshold fields (no credible source, unchanged).

Run once. Prints all UIDs at the end - needed for the import script.
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
    payload = {
        'dataElements': [
            {'name': name, 'shortName': name[:50], 'domainType': 'TRACKER',
             'valueType': vtype, 'aggregationType': agg}
            for name, vtype, agg in defs
        ]
    }
    resp = session.post(f'{DHIS2_URL}/api/metadata', headers=headers, data=json.dumps(payload))
    print(f"  Data elements: {resp.status_code}")
    if resp.status_code not in (200, 201):
        print(f"  ERROR: {resp.text[:500]}")
    names = [d[0] for d in defs]
    lookup = session.get(
        f'{DHIS2_URL}/api/dataElements',
        params={'filter': f'name:in:[{",".join(names)}]', 'fields': 'id,name'}
    ).json()
    return {de['name']: de['id'] for de in lookup.get('dataElements', [])}


def create_stage(stage_name, field_uids_ordered):
    payload = {
        'programStages': [{
            'name': stage_name,
            'program': {'id': PROGRAM_UID},
            'repeatable': True,
            'featureType': 'NONE',
            'programStageDataElements': [
                {'dataElement': {'id': uid}, 'compulsory': False, 'sortOrder': i + 1}
                for i, (_, uid) in enumerate(field_uids_ordered) if uid
            ],
        }]
    }
    resp = session.post(f'{DHIS2_URL}/api/metadata', headers=headers, data=json.dumps(payload))
    print(f"  Stage created: {resp.status_code}")
    if resp.status_code not in (200, 201):
        print(f"  ERROR: {resp.text[:500]}")
    lookup = session.get(
        f'{DHIS2_URL}/api/programStages',
        params={'filter': f'name:eq:{stage_name}', 'fields': 'id,name'}
    ).json()
    return lookup['programStages'][0]['id'] if lookup.get('programStages') else None


all_uids = {}

# ============================================================
# Shared base fields (now includes Standard Deviation)
# ============================================================
print("=== Creating shared base fields ===")
base_defs = [
    ('Env Mean', 'NUMBER', 'AVERAGE'),
    ('Env Minimum', 'NUMBER', 'MIN'),
    ('Env Maximum', 'NUMBER', 'MAX'),
    ('Env Standard Deviation', 'NUMBER', 'NONE'),
    ('Env Reading Count', 'INTEGER', 'SUM'),
    ('Env Hour Start', 'TEXT', 'NONE'),
    ('Env Hour End', 'TEXT', 'NONE'),
]
base_uids = create_data_elements(base_defs)
print(f"  Base UIDs: {base_uids}\n")

base_field_order = [
    ('mean', base_uids.get('Env Mean')),
    ('min', base_uids.get('Env Minimum')),
    ('max', base_uids.get('Env Maximum')),
    ('sd', base_uids.get('Env Standard Deviation')),
    ('count', base_uids.get('Env Reading Count')),
    ('hour_start', base_uids.get('Env Hour Start')),
    ('hour_end', base_uids.get('Env Hour End')),
]

# ============================================================
# Per-variable stage definitions - PM2.5/PM10/NOx now have NO thresholds
# ============================================================
STAGE_SPECS = {
    'Environment – PM1': {'threshold': None},
    'Environment – PM2.5': {'threshold': None},
    'Environment – PM4': {'threshold': None},
    'Environment – PM10': {'threshold': None},
    'Environment – Humidity': {
        'threshold': 'both',
        'above_defs': [
            ('Humidity Above Comfort Range (>50%) - Count', 'INTEGER', 'SUM'),
            ('Humidity Above Comfort Range (>50%) - Timestamps', 'LONG_TEXT', 'NONE'),
        ],
        'below_defs': [
            ('Humidity Below Comfort Range (<30%) - Count', 'INTEGER', 'SUM'),
            ('Humidity Below Comfort Range (<30%) - Timestamps', 'LONG_TEXT', 'NONE'),
        ],
    },
    'Environment – Temperature': {
        'threshold': 'both',
        'above_defs': [
            ('Temperature Above Study Comfort Range (>24C) - Count', 'INTEGER', 'SUM'),
            ('Temperature Above Study Comfort Range (>24C) - Timestamps', 'LONG_TEXT', 'NONE'),
        ],
        'below_defs': [
            ('Temperature Below Study Comfort Range (<20C) - Count', 'INTEGER', 'SUM'),
            ('Temperature Below Study Comfort Range (<20C) - Timestamps', 'LONG_TEXT', 'NONE'),
        ],
    },
    'Environment – VOC': {'threshold': None},
    'Environment – NOx': {'threshold': None},
}

for stage_name, spec in STAGE_SPECS.items():
    print(f"=== Creating stage: {stage_name} ===")
    field_order = list(base_field_order)
    extra_uids = {}

    if spec['threshold'] in ('above_only', 'both'):
        above_uids = create_data_elements(spec['above_defs'])
        above_names = [d[0] for d in spec['above_defs']]
        field_order.append(('above_count', above_uids.get(above_names[0])))
        field_order.append(('above_ts', above_uids.get(above_names[1])))
        extra_uids['above_count'] = above_uids.get(above_names[0])
        extra_uids['above_ts'] = above_uids.get(above_names[1])

    if spec['threshold'] == 'both':
        below_uids = create_data_elements(spec['below_defs'])
        below_names = [d[0] for d in spec['below_defs']]
        field_order.append(('below_count', below_uids.get(below_names[0])))
        field_order.append(('below_ts', below_uids.get(below_names[1])))
        extra_uids['below_count'] = below_uids.get(below_names[0])
        extra_uids['below_ts'] = below_uids.get(below_names[1])

    stage_uid = create_stage(stage_name, field_order)
    print(f"  Stage UID: {stage_uid}\n")

    all_uids[stage_name] = {
        'stage': stage_uid,
        'fields': {
            'mean': base_uids.get('Env Mean'),
            'min': base_uids.get('Env Minimum'),
            'max': base_uids.get('Env Maximum'),
            'sd': base_uids.get('Env Standard Deviation'),
            'count': base_uids.get('Env Reading Count'),
            'hour_start': base_uids.get('Env Hour Start'),
            'hour_end': base_uids.get('Env Hour End'),
            **extra_uids,
        },
    }

print("\n\n=== ALL UIDS (save this - needed for the import script) ===")
print(json.dumps(all_uids, indent=2))
