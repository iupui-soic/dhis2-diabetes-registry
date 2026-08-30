"""
Full-scale environment sensor import across all participants, using the
hourly aggregation approach already validated for wearable/CGM data.

Run in the background:
    nohup python3 env_step2_import.py > env_import_log.txt 2>&1 &
    tail -f env_import_log.txt
    cat env_checkpoint.json
"""

import json
import csv
import os
import time
from datetime import timedelta
import requests
from env_aggregation_logic_v2 import read_env_csv, aggregate_env_column, RELEVANT_COLUMNS

DHIS2_URL = 'https://t2d-registry.plhi.us'
ADMIN_USER = 'admin'
ADMIN_PASS = 'REPLACE_ME'
PROGRAM_UID = 'W3LSFZH3UDq'
PERSON_ID_ATTR_UID = 'oFbmOHnKYaX'
AI_READI_ROOT = '/home/jupyter-ainaperu/AI-READI-fixed'
BATCH_SIZE = 500
CHECKPOINT_FILE = 'env_checkpoint.json'

# Paste the full dict printed by env_step1_metadata.py here:
STAGE_UIDS = {
    'Environment – PM1': {'stage': 'REPLACE_ME', 'fields': {}},
    'Environment – PM2.5': {'stage': 'REPLACE_ME', 'fields': {}},
    'Environment – PM4': {'stage': 'REPLACE_ME', 'fields': {}},
    'Environment – PM10': {'stage': 'REPLACE_ME', 'fields': {}},
    'Environment – Humidity': {'stage': 'REPLACE_ME', 'fields': {}},
    'Environment – Temperature': {'stage': 'REPLACE_ME', 'fields': {}},
    'Environment – VOC': {'stage': 'REPLACE_ME', 'fields': {}},
    'Environment – NOx': {'stage': 'REPLACE_ME', 'fields': {}},
}

COLUMN_TO_STAGE = {
    'pm1': 'Environment – PM1',
    'pm2.5': 'Environment – PM2.5',
    'pm4': 'Environment – PM4',
    'pm10': 'Environment – PM10',
    'hum': 'Environment – Humidity',
    'temp': 'Environment – Temperature',
    'voc': 'Environment – VOC',
    'nox': 'Environment – NOx',
}

session = requests.Session()
session.auth = (ADMIN_USER, ADMIN_PASS)


def load_manifest(path):
    with open(path) as f:
        return {row['person_id']: row for row in csv.DictReader(f, delimiter='\t')}


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


def fmt_dt(dt):
    return dt.strftime('%Y-%m-%d %H:%M')


def build_events_for_participant(person_id, tei_info, env_row):
    fp_suffix = env_row.get('env_sensor_filepath')
    if not fp_suffix or fp_suffix == 'None':
        return []

    full_path = f"{AI_READI_ROOT}{fp_suffix}" if fp_suffix.startswith('/') else f"{AI_READI_ROOT}/{fp_suffix}"
    try:
        rows = read_env_csv(full_path)
    except Exception as e:
        print(f"  {person_id}: read error - {e}")
        return []

    events = []
    for column in RELEVANT_COLUMNS:
        stage_name = COLUMN_TO_STAGE[column]
        cfg = STAGE_UIDS[stage_name]
        hourly = aggregate_env_column(rows, column)

        for hour, stats in hourly.items():
            data_values = []
            for field_key in ['mean', 'min', 'max', 'sd', 'count', 'above_count', 'above_ts',
                               'below_count', 'below_ts']:
                de_uid = cfg['fields'].get(field_key)
                value = stats.get(field_key)
                if de_uid and value is not None:
                    data_values.append({'dataElement': de_uid, 'value': str(value)})

            hour_start_de = cfg['fields'].get('hour_start')
            hour_end_de = cfg['fields'].get('hour_end')
            if hour_start_de:
                data_values.append({'dataElement': hour_start_de, 'value': fmt_dt(hour)})
            if hour_end_de:
                data_values.append({'dataElement': hour_end_de, 'value': fmt_dt(hour + timedelta(hours=1))})

            events.append({
                'program': PROGRAM_UID,
                'programStage': cfg['stage'],
                'trackedEntity': tei_info['trackedEntity'],
                'enrollment': tei_info['enrollment'],
                'orgUnit': tei_info['orgUnit'],
                'occurredAt': hour.isoformat(),
                'status': 'COMPLETED',
                'dataValues': data_values,
            })

    return events


def send_batch(events):
    for i in range(0, len(events), BATCH_SIZE):
        batch = events[i:i + BATCH_SIZE]
        resp = session.post(f'{DHIS2_URL}/api/tracker',
                             params={'async': 'false', 'importStrategy': 'CREATE'},
                             json={'events': batch})
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
    env_manifest = load_manifest(f'{AI_READI_ROOT}/environment/manifest.tsv')
    all_person_ids = sorted(env_manifest.keys())

    checkpoint = load_checkpoint()
    completed = set(checkpoint['completed'])
    remaining = [pid for pid in all_person_ids if pid not in completed]
    print(f"Total: {len(all_person_ids)}, done: {len(completed)}, remaining: {len(remaining)}")

    overall_start = time.time()
    for idx, person_id in enumerate(remaining):
        t0 = time.time()
        tei_info = get_tei_info(person_id)
        if tei_info is None:
            print(f"[{idx+1}/{len(remaining)}] {person_id}: no TEI, skipping")
            completed.add(person_id)
            checkpoint['completed'] = list(completed)
            save_checkpoint(checkpoint)
            continue

        env_row = env_manifest.get(person_id, {})
        events = build_events_for_participant(person_id, tei_info, env_row)
        send_batch(events)

        elapsed = time.time() - t0
        completed.add(person_id)
        checkpoint['completed'] = list(completed)
        save_checkpoint(checkpoint)

        total_elapsed = time.time() - overall_start
        print(f"[{idx+1}/{len(remaining)}] {person_id}: {len(events)} events, "
              f"{elapsed:.1f}s (total: {total_elapsed/3600:.2f}h)")

    print("\nALL PARTICIPANTS COMPLETE")


if __name__ == '__main__':
    main()
