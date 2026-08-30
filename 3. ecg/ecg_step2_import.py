"""
Extracts fields from WFDB .hea files and imports one event per ECG
recording into the "Cardiac – 12-Lead ECG" stage.

Not hourly - single point-in-time clinical measurement. Handles
participants with 2 recordings naturally (repeatable stage).

Run:
    python3 ecg_step2_import.py
(small dataset, ~2257 events total - no background/checkpoint strictly
 needed, but included anyway for consistency and safety on reruns)
"""

import json
import csv
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
AI_READI_ROOT = Path('/home/jupyter-ainaperu/AI-READI-fixed')
CHECKPOINT_FILE = 'ecg_checkpoint.json'

# From ecg_step1_metadata.py's printed output:
STAGE_UID = 'REPLACE_ME'
FIELD_UIDS = {
    'ECG Study Visit Date': 'REPLACE_ME',
    'ECG Validation Date': 'REPLACE_ME',
    'ECG Recording Duration (sec)': 'REPLACE_ME',
    'ECG Heart Rate (bpm)': 'REPLACE_ME',
    'ECG Heart Rate Status': 'REPLACE_ME',
    'ECG PR Interval (ms)': 'REPLACE_ME',
    'ECG PR Interval Status': 'REPLACE_ME',
    'ECG QRS Duration (ms)': 'REPLACE_ME',
    'ECG QRS Duration Status': 'REPLACE_ME',
    'ECG QT Interval (ms)': 'REPLACE_ME',
    'ECG QTc Interval (ms)': 'REPLACE_ME',
    'ECG P Axis (deg)': 'REPLACE_ME',
    'ECG QRS Axis (deg)': 'REPLACE_ME',
    'ECG T Axis (deg)': 'REPLACE_ME',
    'ECG Participant Position': 'REPLACE_ME',
    'ECG Machine Interpretation Status': 'REPLACE_ME',
    'ECG Machine Interpretation Summary': 'REPLACE_ME',
    'ECG Finding 1': 'REPLACE_ME',
    'ECG Finding 1 Detail': 'REPLACE_ME',
    'ECG Finding 2': 'REPLACE_ME',
    'ECG Finding 2 Detail': 'REPLACE_ME',
    'ECG Finding 3': 'REPLACE_ME',
    'ECG Finding 3 Detail': 'REPLACE_ME',
    'ECG Device': 'REPLACE_ME',
    'ECG Sampling Frequency (Hz)': 'REPLACE_ME',
    'ECG Number of Leads': 'REPLACE_ME',
    'ECG Number of Samples': 'REPLACE_ME',
    'ECG Raw Header File Path': 'REPLACE_ME',
    'ECG Raw Data File Path': 'REPLACE_ME',
}

session = requests.Session()
session.auth = (ADMIN_USER, ADMIN_PASS)


def parse_hea_file(hea_path):
    result = {}
    with open(hea_path) as f:
        lines = f.readlines()
    header_parts = lines[0].split()
    result['n_leads'] = int(header_parts[1])
    result['sampling_freq'] = float(header_parts[2])
    result['n_samples'] = int(header_parts[3])
    result['duration_sec'] = round(result['n_samples'] / result['sampling_freq'], 2)
    for line in lines:
        line = line.strip()
        if line.startswith('#') and ':' in line:
            key, _, value = line[1:].partition(':')
            result[key.strip()] = value.strip()
    return result


def classify_range(value, low, high):
    if value is None:
        return None
    if value < low:
        return 'Below reference range'
    elif value > high:
        return 'Above reference range'
    return 'Within reference range'


def classify_qrs(value):
    if value is None:
        return None
    return ('Widened (meets bundle branch block QRS criteria)' if value >= 120
            else 'Within reference range')


def to_int(val):
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def build_event(person_id, tei_info, row, visit_date):
    hea_path = AI_READI_ROOT / row['wfdb_hea_filepath'].lstrip('/')

    try:
        parsed = parse_hea_file(hea_path)
    except Exception as e:
        print(f"  {person_id}: parse error - {e}")
        return None

    rate = to_int(parsed.get('Rate'))
    pr = to_int(parsed.get('PR'))
    qrsd = to_int(parsed.get('QRSD'))
    qt = to_int(parsed.get('QT'))
    qtc = to_int(parsed.get('QTc'))
    p_axis = to_int(parsed.get('P'))
    qrs_axis = to_int(parsed.get('QRS'))
    t_axis = to_int(parsed.get('T'))

    values = {
        'ECG Study Visit Date': visit_date,
        'ECG Validation Date': parsed.get('validation_date'),
        'ECG Recording Duration (sec)': parsed.get('duration_sec'),
        'ECG Heart Rate (bpm)': rate,
        'ECG Heart Rate Status': classify_range(rate, 60, 100),
        'ECG PR Interval (ms)': pr,
        'ECG PR Interval Status': classify_range(pr, 120, 200),
        'ECG QRS Duration (ms)': qrsd,
        'ECG QRS Duration Status': classify_qrs(qrsd),
        'ECG QT Interval (ms)': qt,
        'ECG QTc Interval (ms)': qtc,
        'ECG P Axis (deg)': p_axis,
        'ECG QRS Axis (deg)': qrs_axis,
        'ECG T Axis (deg)': t_axis,
        'ECG Participant Position': parsed.get('participant_position'),
        'ECG Machine Interpretation Status': parsed.get('interpretation_comment_1'),
        'ECG Machine Interpretation Summary': parsed.get('interpretation_comment_2'),
        'ECG Finding 1': parsed.get('comment_1_key'),
        'ECG Finding 1 Detail': parsed.get('comment_1_val'),
        'ECG Finding 2': parsed.get('comment_2_key'),
        'ECG Finding 2 Detail': parsed.get('comment_2_val'),
        'ECG Finding 3': parsed.get('comment_3_key'),
        'ECG Finding 3 Detail': parsed.get('comment_3_val'),
        'ECG Device': parsed.get('device_model'),
        'ECG Sampling Frequency (Hz)': parsed.get('sampling_freq'),
        'ECG Number of Leads': parsed.get('n_leads'),
        'ECG Number of Samples': parsed.get('n_samples'),
        'ECG Raw Header File Path': row['wfdb_hea_filepath'],
        'ECG Raw Data File Path': row['wfdb_dat_filepath'],
    }

    data_values = []
    for field_name, value in values.items():
        de_uid = FIELD_UIDS.get(field_name)
        if de_uid and value is not None:
            data_values.append({'dataElement': de_uid, 'value': str(value)})

    occurred_at = visit_date if isinstance(visit_date, str) and visit_date else parsed.get('validation_date', '2023-01-01')

    return {
        'program': PROGRAM_UID,
        'programStage': STAGE_UID,
        'trackedEntity': tei_info['trackedEntity'],
        'enrollment': tei_info['enrollment'],
        'orgUnit': tei_info['orgUnit'],
        'occurredAt': occurred_at,
        'status': 'COMPLETED',
        'dataValues': data_values,
    }


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


def send_batch(events, batch_size=100):
    for i in range(0, len(events), batch_size):
        batch = events[i:i + batch_size]
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
    manifest = pd.read_csv(AI_READI_ROOT / "cardiac_ecg" / "manifest.tsv", sep="\t")
    manifest['person_id'] = manifest['person_id'].astype(str)

    participants = pd.read_csv(AI_READI_ROOT / "participants.tsv", sep="\t")
    participants['person_id'] = participants['person_id'].astype(str)
    visit_dates = dict(zip(participants['person_id'], participants['study_visit_date']))

    checkpoint = load_checkpoint()
    completed = set(checkpoint['completed'])

    all_person_ids = manifest['person_id'].unique().tolist()
    remaining = [pid for pid in all_person_ids if pid not in completed]
    print(f"Total: {len(all_person_ids)}, done: {len(completed)}, remaining: {len(remaining)}")

    start = time.time()
    for idx, person_id in enumerate(remaining):
        tei_info = get_tei_info(person_id)
        if tei_info is None:
            print(f"[{idx+1}/{len(remaining)}] {person_id}: no TEI, skipping")
            completed.add(person_id)
            checkpoint['completed'] = list(completed)
            save_checkpoint(checkpoint)
            continue

        rows = manifest[manifest['person_id'] == person_id]
        visit_date = visit_dates.get(person_id)

        events = []
        for _, row in rows.iterrows():
            event = build_event(person_id, tei_info, row, visit_date)
            if event:
                events.append(event)

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
