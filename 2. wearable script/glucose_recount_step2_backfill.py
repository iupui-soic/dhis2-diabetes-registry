"""
Step 2: recomputes and updates, for every existing CGM - Glucose event:
  - Above Range Timestamps (>180 mg/dL, combined: numeric >180 OR device "High")
  - Below Range Timestamps (<70 mg/dL, combined: numeric <70 OR device "Low")
  - Above Range Count (matches the timestamp list length)
  - Below Range Count (matches the timestamp list length)

Device High Count / Device Low Count are NOT touched here - they were
already correctly populated in the original import and represent a
different, narrower concept (sensor's own measurable-range limit).

If Above/Below Range Timestamps were already populated by the earlier
(device-only) backfill, this OVERWRITES them with the corrected,
combined-threshold version.

Run in the background:
    nohup python3 glucose_recount_step2_backfill.py > glucose_recount_log.txt 2>&1 &
    tail -f glucose_recount_log.txt
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
CHECKPOINT_FILE = 'glucose_recount_checkpoint.json'

# Existing timestamp field UIDs (already created, now renamed)
ABOVE_TS_DE = 'Zu4iFxtthSU'
BELOW_TS_DE = 'LfzwHxQUotL'

# From glucose_recount_step1_metadata.py's printed output:
ABOVE_COUNT_DE = 'REPLACE_ME'
BELOW_COUNT_DE = 'REPLACE_ME'

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

        below_readings = [ts for ts, v in hour_readings
                           if v == 'Low' or (not isinstance(v, str) and v < 70)]
        above_readings = [ts for ts, v in hour_readings
                           if v == 'High' or (not isinstance(v, str) and v > 180)]

        below_ts_str = ', '.join(time_only(ts) for ts in sorted(below_readings))
        above_ts_str = ', '.join(time_only(ts) for ts in sorted(above_readings))

        existing_dvs = [dv for dv in event['dataValues']
                         if dv['dataElement'] not in (ABOVE_TS_DE, BELOW_TS_DE, ABOVE_COUNT_DE, BELOW_COUNT_DE)]

        if above_ts_str:
            existing_dvs.append({'dataElement': ABOVE_TS_DE, 'value': above_ts_str})
        if below_ts_str:
            existing_dvs.append({'dataElement': BELOW_TS_DE, 'value': below_ts_str})
        existing_dvs.append({'dataElement': ABOVE_COUNT_DE, 'value': str(len(above_readings))})
        existing_dvs.append({'dataElement': BELOW_COUNT_DE, 'value': str(len(below_readings))})

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
