"""
Imports one event per diagnosed condition per participant into the
"Diagnosis History" stage, from clinical_data/condition_occurrence.csv.

condition_source_value format: "code, Label text" - split on the first comma.

Run:
    python3 diagnosis_step2_import.py
(small-to-medium dataset, ~12,375 events total)
"""

import json
import os
import time
import requests
import pandas as pd
from pathlib import Path

DHIS2_URL = 'https://t2d-registry.plhi.us'
ADMIN_USER = 'admin'
ADMIN_PASS = 'REPLACE_ME'
PROGRAM_UID = 'W3LSFZH3UDq'
PERSON_ID_ATTR_UID = 'oFbmOHnKYaX'
AI_READI_ROOT = Path(os.path.expanduser('~/AI-READI'))
CHECKPOINT_FILE = 'diagnosis_checkpoint.json'
BATCH_SIZE = 100

# From diagnosis_step1_metadata.py's printed output:
STAGE_UID = 'REPLACE_ME'
FIELD_UIDS = {
    'Diagnosis Condition Code': 'REPLACE_ME',
    'Diagnosis Condition Label': 'REPLACE_ME',
    'Diagnosis Date': 'REPLACE_ME',
}

session = requests.Session()
session.auth = (ADMIN_USER, ADMIN_PASS)


def get_tei_info(person_id):
    resp = session.get(
        f'{DHIS2_URL}/api/tracker/trackedEntities',
        params={'program': PROGRAM_UID, 'filter': f'{PERSON_ID_ATTR_UID}:eq:{person_id}',
                'fields': 'trackedEntity,orgUnit,enrollments[enrollment]'},
    )
    resp.raise_for_status()
    instances = resp.json().get('trackedEntities', [])
    if not instances:
        return None
    tei = instances[0]
    enrollment = tei['enrollments'][0]['enrollment'] if tei.get('enrollments') else None
    return {'trackedEntity': tei['trackedEntity'], 'orgUnit': tei['orgUnit'], 'enrollment': enrollment}


def parse_condition_value(raw):
    """'mhoccur_ad, Dementia (Examples...' -> ('mhoccur_ad', 'Dementia (Examples...')"""
    if not isinstance(raw, str) or ',' not in raw:
        return raw, raw
    code, _, label = raw.partition(',')
    return code.strip(), label.strip()


def build_events_for_participant(person_id, tei_info, rows):
    events = []
    for _, row in rows.iterrows():
        code, label = parse_condition_value(row['condition_source_value'])
        date = row['condition_start_date']

        data_values = [
            {'dataElement': FIELD_UIDS['Diagnosis Condition Code'], 'value': code},
            {'dataElement': FIELD_UIDS['Diagnosis Condition Label'], 'value': label},
            {'dataElement': FIELD_UIDS['Diagnosis Date'], 'value': str(date)},
        ]

        events.append({
            'program': PROGRAM_UID,
            'programStage': STAGE_UID,
            'trackedEntity': tei_info['trackedEntity'],
            'enrollment': tei_info['enrollment'],
            'orgUnit': tei_info['orgUnit'],
            'occurredAt': str(date),
            'status': 'COMPLETED',
            'dataValues': data_values,
        })
    return events


def send_batch(events):
    for i in range(0, len(events), BATCH_SIZE):
        batch = events[i:i + BATCH_SIZE]
        resp = session.post(
            f'{DHIS2_URL}/api/tracker',
            params={'importStrategy': 'CREATE', 'async': 'false'},
            json={'events': batch},
        )
        try:
            status = resp.json().get('status', 'UNKNOWN')
        except Exception:
            status = f"HTTP {resp.status_code}"
        if status not in ('OK', 'SUCCESS'):
            print(f"    batch error: {resp.text[:500]}")


def load_checkpoint():
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE) as f:
            return json.load(f)
    return {'completed': []}


def save_checkpoint(cp):
    with open(CHECKPOINT_FILE, 'w') as f:
        json.dump(cp, f)


def main():
    cond = pd.read_csv(AI_READI_ROOT / "clinical_data" / "clinical_data" / "condition_occurrence.csv")
    cond['person_id'] = cond['person_id'].astype(str)

    checkpoint = load_checkpoint()
    completed = set(checkpoint['completed'])

    all_person_ids = cond['person_id'].unique().tolist()
    remaining = [pid for pid in all_person_ids if pid not in completed]
    print(f"Total: {len(all_person_ids)}, done: {len(completed)}, remaining: {len(remaining)}")

    start = time.time()
    for idx, person_id in enumerate(remaining):
        tei_info = get_tei_info(person_id)
        if tei_info is None:
            print(f"[{idx+1}/{len(remaining)}] {person_id}: no TEI, skipping "
                  f"(this OMOP person_id may not match the registry's person_id scheme - verify)")
            completed.add(person_id)
            checkpoint['completed'] = list(completed)
            save_checkpoint(checkpoint)
            continue

        rows = cond[cond['person_id'] == person_id]
        events = build_events_for_participant(person_id, tei_info, rows)
        if events:
            send_batch(events)

        completed.add(person_id)
        checkpoint['completed'] = list(completed)
        save_checkpoint(checkpoint)

        if (idx + 1) % 100 == 0:
            elapsed = time.time() - start
            print(f"[{idx+1}/{len(remaining)}] processed, {elapsed:.0f}s elapsed")

    print("\nALL PARTICIPANTS COMPLETE")


if __name__ == '__main__':
    main()
