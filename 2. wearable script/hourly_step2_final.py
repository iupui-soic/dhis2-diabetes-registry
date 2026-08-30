"""
Final full-scale HOURLY AGGREGATED import, using:
  - shared Mean/Min/Max/Count fields for Heart Rate, Respiratory Rate,
    SpO2, Stress
  - dedicated fields for Sleep (duration-minutes), Activity (steps sum,
    now duration-split correctly), and Glucose (rich clinical metrics)

Run in the background:
    nohup python3 hourly_step2_final.py > hourly_import_log.txt 2>&1 &
    tail -f hourly_import_log.txt
"""

import json
import csv
import time
import os
import requests
from hourly_aggregation_logic_final import (
    aggregate_simple, aggregate_glucose, aggregate_activity,
    extract_heart_rate, extract_respiratory_rate, extract_oxygen_saturation,
    extract_stress, extract_sleep_segments,
)

# ---- CONFIG - fill in from hourly_step1_final.py's printed output ----
DHIS2_URL = 'https://t2d-registry.plhi.us'
ADMIN_USER = 'admin'
ADMIN_PASS = 'REPLACE_ME'
PROGRAM_UID = 'W3LSFZH3UDq'
PERSON_ID_ATTR_UID = 'oFbmOHnKYaX'
AI_READI_ROOT = '/home/jupyter-ainaperu/AI-READI-fixed'
BATCH_SIZE = 500
CHECKPOINT_FILE = 'hourly_checkpoint_final.json'

# Paste the full dict printed by hourly_step1_final.py here:
STAGE_UIDS = {
    'Wearable – Heart Rate': {'stage': 'REPLACE_ME', 'fields': {}},
    'Wearable – Respiratory Rate': {'stage': 'REPLACE_ME', 'fields': {}},
    'Wearable – SpO2': {'stage': 'REPLACE_ME', 'fields': {}},
    'Wearable – Stress': {'stage': 'REPLACE_ME', 'fields': {}},
    'Wearable – Sleep': {'stage': 'REPLACE_ME', 'fields': {}},
    'Wearable – Activity': {'stage': 'REPLACE_ME', 'fields': {}},
    'Wearable – Calories': {'stage': 'REPLACE_ME', 'fields': {}},
    'CGM – Glucose': {'stage': 'REPLACE_ME', 'fields': {}},
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


def make_event(stage_name, tei_info, hour, field_values):
    cfg = STAGE_UIDS[stage_name]
    data_values = []
    for field_name, value in field_values.items():
        if value is None:
            continue
        de_uid = cfg['fields'].get(field_name)
        if de_uid:
            data_values.append({'dataElement': de_uid, 'value': str(value)})
    return {
        'program': PROGRAM_UID, 'programStage': cfg['stage'],
        'trackedEntity': tei_info['trackedEntity'], 'enrollment': tei_info['enrollment'],
        'orgUnit': tei_info['orgUnit'], 'occurredAt': hour, 'status': 'COMPLETED',
        'dataValues': data_values,
    }


def build_events_for_participant(person_id, tei_info, wam_row, bg_row):
    events = []

    # 4 shared-field continuous metrics
    continuous = [
        ('Wearable – Heart Rate', 'heartrate_filepath', extract_heart_rate),
        ('Wearable – Respiratory Rate', 'respiratory_rate_filepath', extract_respiratory_rate),
        ('Wearable – SpO2', 'oxygen_saturation_filepath', extract_oxygen_saturation),
        ('Wearable – Stress', 'stress_level_filepath', extract_stress),
    ]
    for stage_name, col, extractor in continuous:
        fp_suffix = wam_row.get(col)
        if not fp_suffix:
            continue
        full_path = f"{AI_READI_ROOT}{fp_suffix}" if fp_suffix.startswith('/') else f"{AI_READI_ROOT}/{fp_suffix}"
        try:
            hourly = aggregate_simple(extractor(full_path))
        except Exception as e:
            print(f"  {person_id}/{stage_name}: error - {e}")
            continue
        for hour, stats in hourly.items():
            events.append(make_event(stage_name, tei_info, hour, stats))

    # Sleep - RAW SEGMENTS, one event per stage transition (not hourly)
    fp_suffix = wam_row.get('sleep_filepath')
    if fp_suffix:
        full_path = f"{AI_READI_ROOT}{fp_suffix}" if fp_suffix.startswith('/') else f"{AI_READI_ROOT}/{fp_suffix}"
        try:
            for start_ts, stage, duration_min in extract_sleep_segments(full_path):
                events.append(make_event('Wearable – Sleep', tei_info, start_ts,
                                          {'stage': stage, 'duration_minutes': duration_min}))
        except Exception as e:
            print(f"  {person_id}/Sleep: error - {e}")

    # Activity (now duration-split correctly)
    fp_suffix = wam_row.get('physical_activity_filepath')
    if fp_suffix:
        full_path = f"{AI_READI_ROOT}{fp_suffix}" if fp_suffix.startswith('/') else f"{AI_READI_ROOT}/{fp_suffix}"
        try:
            hourly = aggregate_activity(full_path)
            for hour, stats in hourly.items():
                events.append(make_event('Wearable – Activity', tei_info, hour, stats))
        except Exception as e:
            print(f"  {person_id}/Activity: error - {e}")

    # Calories (unchanged - point-in-time readings, no splitting needed)
    fp_suffix = wam_row.get('active_calories_filepath')
    if fp_suffix:
        full_path = f"{AI_READI_ROOT}{fp_suffix}" if fp_suffix.startswith('/') else f"{AI_READI_ROOT}/{fp_suffix}"
        try:
            with open(full_path) as f:
                data = json.load(f)
            from collections import defaultdict
            hourly_sum, hourly_count = defaultdict(float), defaultdict(int)
            for r in data['body']['activity']:
                if r.get('activity_name') == 'kcal_burned':
                    ts = r['effective_time_frame'].get('date_time') or r['effective_time_frame']['time_interval']['start_date_time']
                    from datetime import datetime
                    hour = datetime.fromisoformat(ts.replace('Z', '+00:00')).replace(minute=0, second=0, microsecond=0).isoformat()
                    hourly_sum[hour] += r['calories_value']['value']
                    hourly_count[hour] += 1
            for hour in hourly_sum:
                events.append(make_event('Wearable – Calories', tei_info, hour,
                                          {'sum': round(hourly_sum[hour], 1), 'count': hourly_count[hour]}))
        except Exception as e:
            print(f"  {person_id}/Calories: error - {e}")

    # Glucose
    fp_suffix = bg_row.get('glucose_filepath')
    if fp_suffix:
        full_path = f"{AI_READI_ROOT}{fp_suffix}" if fp_suffix.startswith('/') else f"{AI_READI_ROOT}/{fp_suffix}"
        try:
            hourly = aggregate_glucose(full_path)
            for hour, stats in hourly.items():
                events.append(make_event('CGM – Glucose', tei_info, hour, stats))
        except Exception as e:
            print(f"  {person_id}/Glucose: error - {e}")

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
        tei_info = get_tei_info(person_id)
        if tei_info is None:
            print(f"[{idx+1}/{len(remaining)}] {person_id}: no TEI, skipping")
            completed.add(person_id)
            checkpoint['completed'] = list(completed)
            save_checkpoint(checkpoint)
            continue

        wam_row = wam_manifest.get(person_id, {})
        bg_row = bg_manifest.get(person_id, {})
        events = build_events_for_participant(person_id, tei_info, wam_row, bg_row)
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
