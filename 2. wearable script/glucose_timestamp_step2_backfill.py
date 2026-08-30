"""
Backfills "Low Reading Timestamps" and "High Reading Timestamps" onto every
existing CGM - Glucose event. Preserves all existing field values - this
is an UPDATE, only the two new fields are added.

Run in the background:
    nohup python3 glucose_timestamp_step2_backfill.py > glucose_ts_log.txt 2>&1 &
    tail -f glucose_ts_log.txt
"""

import json
import csv
import os
import time
import requests
from datetime import datetime

DHIS2_URL = 'https://t2d-registry.plhi.us'
ADMIN_USER = 'admin'
ADMIN_PASS = 'REPLACE_ME'
PROGRAM_UID = 'W3LSFZH3UDq'
PERSON_ID_ATTR_UID = 'oFbmOHnKYaX'
AI_READI_ROOT = '/home/jupyter-ainaperu/AI-READI-fixed'
GLUCOSE_STAGE_UID = 'SS7a20eCnBZ'
BATCH_SIZE = 500
CHECKPOINT_FILE = 'glucose_ts_checkpoint.json'

# From glucose_timestamp_step1_metadata.py's printed output:
LOW_TS_DE = 'REPLACE_ME'
HIGH_TS_DE = 'REPLACE_ME'

session = requests.Session()
session.auth = (ADMIN_USER, ADMIN_PASS)


def load_manifest(path):
    with open(path) as f:
        return {row['person_id']: row for row in csv.DictReader(f, delimiter='\t')}


def get_ts(effective_time_frame):
    if 'date_time' in effective_time_frame:
        return effective_time_frame['date_time']
    return effective_time_frame['time_interval']['start_date_time']


def parse_hour_bucket(ts_str):
    dt = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
    return dt.replace(minute=0, second=0, microsecond=0)


def time_only(ts_str):
    dt = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
    return dt.strftime('%H:%M:%S')


def extract_glucose_raw(fp):
    with open(fp) as f:
        data = json.load(f)
    out = []
    for r in data['body']['cgm']:
        v = r['blood_glucose']['value']
        ts = get_ts(r['effective_time_frame'])
        out.append((ts, v))
    return out


def group_by_hour(readings):
    from collections import defaultdict
    grouped = defaultdict(list)
    for ts, val in readings:
        hour = parse_hour_bucket(ts)
        key = hour.strftime('%Y-%m-%dT%H:%M:%S')
        grouped[key].append((ts, val))
    return grouped


def get_tei_info(person_id):
    resp = session.get(
        f'{DHIS2_URL}/api/tracker/trackedEntities',
        params={'program': PROGRAM_UID, 'filter': f'{PERSON_ID_ATTR_UID}:eq:{person_id}',
                'fields': 'trackedEntity'},
    )
    resp.raise_for_status()
    instances = resp.json().get('trackedEntities', [])
    return instances[0]['trackedEntity'] if instances else None


def fetch_all_events(tei):
    all_events = []
    page = 1
    while True:
        resp = session.get(
            f'{DHIS2_URL}/api/tracker/events',
            params={
                'program': PROGRAM_UID, 'programStage': GLUCOSE_STAGE_UID, 'trackedEntity': tei,
                'pageSize': 1000, 'page': page,
                'fields': 'event,orgUnit,enrollment,occurredAt,status,dataValues[dataElement,value]',
            },
        )
        events = resp.json().get('events', [])
        if not events:
            break
        all_events.extend(events)
        page += 1
    return all_events


def send_update_batch(events):
    for i in range(0, len(events), BATCH_SIZE):
        batch = events[i:i + BATCH_SIZE]
        resp = session.post(
            f'{DHIS2_URL}/api/tracker',
            params={'importStrategy': 'UPDATE', 'async': 'false'},
            json={'events': batch},
        )
        try:
            status = resp.json().get('status', 'UNKNOWN')
        except Exception:
            status = f"HTTP {resp.status_code}"
        if status not in ('OK', 'SUCCESS'):
            print(f"    batch error: {resp.text[:500]}")


def build_updates(tei, bg_row):
    glu_suffix = bg_row.get('glucose_filepath')
    if not glu_suffix or glu_suffix == 'None':
        return []

    full_path = f"{AI_READI_ROOT}{glu_suffix}" if glu_suffix.startswith('/') else f"{AI_READI_ROOT}/{glu_suffix}"
    try:
        readings = extract_glucose_raw(full_path)
    except Exception as e:
        print(f"    extraction error: {e}")
        return []

    grouped = group_by_hour(readings)
    existing_events = fetch_all_events(tei)

    updates = []
    for event in existing_events:
        occurred_key = event['occurredAt'][:19]
        hour_readings = grouped.get(occurred_key, [])

        low_readings = [ts for ts, v in hour_readings if v == 'Low']
        high_readings = [ts for ts, v in hour_readings if v == 'High']

        low_ts_str = ', '.join(time_only(ts) for ts in sorted(low_readings))
        high_ts_str = ', '.join(time_only(ts) for ts in sorted(high_readings))

        existing_dvs = list(event['dataValues'])
        existing_de_ids = {dv['dataElement'] for dv in existing_dvs}

        if LOW_TS_DE not in existing_de_ids and low_ts_str:
            existing_dvs.append({'dataElement': LOW_TS_DE, 'value': low_ts_str})
        if HIGH_TS_DE not in existing_de_ids and high_ts_str:
            existing_dvs.append({'dataElement': HIGH_TS_DE, 'value': high_ts_str})

        updates.append({
            'event': event['event'],
            'program': PROGRAM_UID,
            'programStage': GLUCOSE_STAGE_UID,
            'orgUnit': event['orgUnit'],
            'enrollment': event.get('enrollment'),
            'occurredAt': event['occurredAt'],
            'status': event.get('status', 'COMPLETED'),
            'dataValues': existing_dvs,
        })

    return updates


def load_checkpoint():
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE) as f:
            return json.load(f)
    return {'completed': []}


def save_checkpoint(cp):
    with open(CHECKPOINT_FILE, 'w') as f:
        json.dump(cp, f)


def main():
    bg_manifest = load_manifest(f'{AI_READI_ROOT}/wearable_blood_glucose/manifest.tsv')
    all_person_ids = sorted(bg_manifest.keys())

    checkpoint = load_checkpoint()
    completed = set(checkpoint['completed'])
    remaining = [pid for pid in all_person_ids if pid not in completed]
    print(f"Total: {len(all_person_ids)}, done: {len(completed)}, remaining: {len(remaining)}")

    overall_start = time.time()
    for idx, person_id in enumerate(remaining):
        t0 = time.time()
        tei = get_tei_info(person_id)
        if tei is None:
            completed.add(person_id)
            checkpoint['completed'] = list(completed)
            save_checkpoint(checkpoint)
            continue

        bg_row = bg_manifest.get(person_id, {})
        updates = build_updates(tei, bg_row)
        if updates:
            send_update_batch(updates)

        elapsed = time.time() - t0
        completed.add(person_id)
        checkpoint['completed'] = list(completed)
        save_checkpoint(checkpoint)

        total_elapsed = time.time() - overall_start
        print(f"[{idx+1}/{len(remaining)}] {person_id}: {len(updates)} events updated, "
              f"{elapsed:.1f}s (total: {total_elapsed/3600:.2f}h)")

    print("\nALL PARTICIPANTS COMPLETE")


if __name__ == '__main__':
    main()
