"""
Backfills "Hour Start" and "Hour End" onto every existing event across all
8 stages and all participants. This is an UPDATE, not a re-import -
existing field values are preserved; only the two new fields are added.

For Sleep (raw segments, not fixed hourly buckets): Hour End = Hour Start +
that segment's actual duration_minutes value.
For all other stages (fixed hourly buckets): Hour End = Hour Start + 1 hour.

Run in the background - this touches ~3.8 million existing records, so
expect this to take a while, likely longer than the original ~45 minute
import since each event must be fetched AND updated.

    nohup python3 hour_backfill_step2.py > hour_backfill_log.txt 2>&1 &
    tail -f hour_backfill_log.txt
    cat hour_backfill_checkpoint.json
"""

import json
import csv
import os
import time
import requests
from datetime import datetime, timedelta

DHIS2_URL = 'https://t2d-registry.plhi.us'
ADMIN_USER = 'admin'
ADMIN_PASS = 'Londonbridge@2026'
PROGRAM_UID = 'W3LSFZH3UDq'
PERSON_ID_ATTR_UID = 'oFbmOHnKYaX'
AI_READI_ROOT = '/home/jupyter-ainaperu/AI-READI-fixed'
BATCH_SIZE = 500
CHECKPOINT_FILE = 'hour_backfill_checkpoint.json'

# From hour_backfill_step1_metadata.py's printed output:
HOUR_START_DE = 'Ef2A6W8ouAq'
HOUR_END_DE = 'jSZPIXD5WmW'

# Paste your existing STAGE_UIDS dict from hourly_step2_final.py here.
# The 'duration_field' UID is needed specifically for Sleep.
STAGE_UIDS = {
    'Wearable – Heart Rate': {'stage': 'XB29GdXrNDb'},
    'Wearable – Respiratory Rate': {'stage': 'ZHqSqHOv8is'},
    'Wearable – SpO2': {'stage': 'QoigcBfYCcG'},
    'Wearable – Stress': {'stage': 'g803i2FH8bF'},
    'Wearable – Sleep': {'stage': 'aR9APTYYiEe', 'duration_field': 'Z7fmggiTvKu'},
    'Wearable – Activity': {'stage': 'uASeLWkCtRB'},
    'Wearable – Calories': {'stage': 'xfNfR1XEwM8'},
    'CGM – Glucose': {'stage': 'SS7a20eCnBZ'},
}

session = requests.Session()
session.auth = (ADMIN_USER, ADMIN_PASS)


def fmt(dt):
    return dt.strftime('%Y-%m-%d %H:%M')


def load_manifest(path):
    with open(path) as f:
        return {row['person_id']: row for row in csv.DictReader(f, delimiter='\t')}


def get_tei_info(person_id):
    resp = session.get(
        f'{DHIS2_URL}/api/tracker/trackedEntities',
        params={'program': PROGRAM_UID, 'filter': f'{PERSON_ID_ATTR_UID}:eq:{person_id}',
                'fields': 'trackedEntity'},
    )
    resp.raise_for_status()
    instances = resp.json().get('trackedEntities', [])
    return instances[0]['trackedEntity'] if instances else None


def fetch_all_events(tei, stage_uid):
    """Fetch every event for this participant+stage, with full dataValues."""
    all_events = []
    page = 1
    while True:
        resp = session.get(
            f'{DHIS2_URL}/api/tracker/events',
            params={
                'program': PROGRAM_UID, 'programStage': stage_uid, 'trackedEntity': tei,
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


def build_update_payload(event, stage_name, cfg):
    occurred = datetime.fromisoformat(event['occurredAt'].replace('Z', '+00:00'))
    hour_start = occurred

    if stage_name == 'Wearable – Sleep':
        duration_field = cfg.get('duration_field')
        duration_val = next(
            (dv['value'] for dv in event['dataValues'] if dv['dataElement'] == duration_field), None
        )
        try:
            minutes = float(duration_val) if duration_val is not None else 60.0
        except ValueError:
            minutes = 60.0
        hour_end = hour_start + timedelta(minutes=minutes)
    else:
        hour_end = hour_start + timedelta(hours=1)

    existing_dvs = list(event['dataValues'])
    existing_de_ids = {dv['dataElement'] for dv in existing_dvs}
    if HOUR_START_DE not in existing_de_ids:
        existing_dvs.append({'dataElement': HOUR_START_DE, 'value': fmt(hour_start)})
    if HOUR_END_DE not in existing_de_ids:
        existing_dvs.append({'dataElement': HOUR_END_DE, 'value': fmt(hour_end)})

    return {
        'event': event['event'],
        'program': PROGRAM_UID,
        'programStage': cfg['stage'],
        'orgUnit': event['orgUnit'],
        'enrollment': event.get('enrollment'),
        'occurredAt': event['occurredAt'],
        'status': event.get('status', 'COMPLETED'),
        'dataValues': existing_dvs,
    }


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


def load_checkpoint():
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE) as f:
            return json.load(f)
    return {'completed': []}


def save_checkpoint(cp):
    with open(CHECKPOINT_FILE, 'w') as f:
        json.dump(cp, f)


def main():
    wam_manifest = load_manifest(f'{AI_READI_ROOT}/wearable_activity_monitor/manifest.tsv')
    bg_manifest = load_manifest(f'{AI_READI_ROOT}/wearable_blood_glucose/manifest.tsv')
    all_person_ids = sorted(set(wam_manifest.keys()) | set(bg_manifest.keys()))

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

        total_updated = 0
        for stage_name, cfg in STAGE_UIDS.items():
            events = fetch_all_events(tei, cfg['stage'])
            if not events:
                continue
            payloads = [build_update_payload(e, stage_name, cfg) for e in events]
            send_update_batch(payloads)
            total_updated += len(payloads)

        elapsed = time.time() - t0
        completed.add(person_id)
        checkpoint['completed'] = list(completed)
        save_checkpoint(checkpoint)

        total_elapsed = time.time() - overall_start
        print(f"[{idx+1}/{len(remaining)}] {person_id}: {total_updated} events updated, "
              f"{elapsed:.1f}s (total: {total_elapsed/3600:.2f}h)")

    print("\nALL PARTICIPANTS COMPLETE")


if __name__ == '__main__':
    main()
