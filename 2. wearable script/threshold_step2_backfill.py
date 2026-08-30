"""
Backfills threshold/status/SD/timestamp fields onto every existing Heart
Rate, Respiratory Rate, and SpO2 event. This is an UPDATE - existing
values (Mean, Min, Max, Count, Hour Start, Hour End) are preserved.

For each hourly event, re-reads the participant's RAW source file to
recompute which individual readings fall in that specific hour, then:
  - classifies each into low/within/high (or expected/mild-low/marked-low
    for SpO2)
  - computes standard deviation
  - builds the comma-separated timestamp lists for flagged readings
  - determines Hourly Status and Data Sufficiency

Run in the background - re-reads raw files for every participant again.

    nohup python3 threshold_step2_backfill.py > threshold_backfill_log.txt 2>&1 &
    tail -f threshold_backfill_log.txt
"""

import json
import csv
import os
import time
import math
import requests
from datetime import datetime, timedelta
from collections import defaultdict

DHIS2_URL = 'https://t2d-registry.plhi.us'
ADMIN_USER = 'admin'
ADMIN_PASS = 'REPLACE_ME'
PROGRAM_UID = 'W3LSFZH3UDq'
PERSON_ID_ATTR_UID = 'oFbmOHnKYaX'
AI_READI_ROOT = '/home/jupyter-ainaperu/AI-READI-fixed'
BATCH_SIZE = 500
CHECKPOINT_FILE = 'threshold_backfill_checkpoint.json'

STAGE_UID = {
    'HR': 'XB29GdXrNDb',
    'RR': 'ZHqSqHOv8is',
    'SPO2': 'QoigcBfYCcG',
}

# Paste from threshold_step1_metadata.py's printed output:
FIELD_UIDS = {
    'HR': {
        'sd': 'REPLACE_ME', 'low_count': 'REPLACE_ME', 'high_count': 'REPLACE_ME',
        'status': 'REPLACE_ME', 'sufficiency': 'REPLACE_ME',
        'low_ts': 'REPLACE_ME', 'high_ts': 'REPLACE_ME',
    },
    'RR': {
        'sd': 'REPLACE_ME', 'low_count': 'REPLACE_ME', 'high_count': 'REPLACE_ME',
        'status': 'REPLACE_ME', 'sufficiency': 'REPLACE_ME',
        'low_ts': 'REPLACE_ME', 'high_ts': 'REPLACE_ME',
    },
    'SPO2': {
        'sd': 'REPLACE_ME', 'mild_low_count': 'REPLACE_ME', 'marked_low_count': 'REPLACE_ME',
        'status': 'REPLACE_ME', 'sufficiency': 'REPLACE_ME',
        'mild_low_ts': 'REPLACE_ME', 'marked_low_ts': 'REPLACE_ME',
    },
}

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


def extract_heart_rate(fp):
    with open(fp) as f:
        data = json.load(f)
    out = []
    for r in data['body']['heart_rate']:
        v = r['heart_rate']['value']
        if v is not None and v > 0:
            out.append((get_ts(r['effective_time_frame']), v))
    return out


def extract_respiratory_rate(fp):
    with open(fp) as f:
        data = json.load(f)
    out = []
    for r in data['body']['breathing']:
        if 'respiratory_rate' in r:
            v = r['respiratory_rate']['value']
            if v is not None and v >= 0:
                out.append((get_ts(r['effective_time_frame']), v))
    return out


def extract_oxygen_saturation(fp):
    with open(fp) as f:
        data = json.load(f)
    out = []
    for r in data['body']['breathing']:
        if 'oxygen_saturation' in r:
            v = r['oxygen_saturation']['value']
            if v is not None and not (isinstance(v, float) and math.isnan(v)):
                out.append((get_ts(r['effective_time_frame']), v))
    return out


def group_by_hour(readings):
    """readings: list of (ts, value). Returns {hour_iso_no_tz: [(ts, value), ...]}"""
    grouped = defaultdict(list)
    for ts, val in readings:
        hour = parse_hour_bucket(ts)
        key = hour.strftime('%Y-%m-%dT%H:%M:%S')
        grouped[key].append((ts, val))
    return grouped


def compute_hr_rr_fields(readings_this_hour, low_thresh, high_thresh):
    values = [v for ts, v in readings_this_hour]
    count = len(values)

    if count == 0:
        sufficiency = 'No valid data'
    elif count <= 2:
        sufficiency = 'Limited'
    else:
        sufficiency = 'Sufficient'

    low_readings = [(ts, v) for ts, v in readings_this_hour if v < low_thresh]
    high_readings = [(ts, v) for ts, v in readings_this_hour if v > high_thresh]

    sd = None
    if count > 1:
        mean = sum(values) / count
        variance = sum((v - mean) ** 2 for v in values) / count
        sd = round(variance ** 0.5, 2)

    if count == 0:
        status = 'Insufficient data'
    elif low_readings and high_readings:
        status = 'Both low and high readings present'
    elif low_readings:
        status = 'Low readings present'
    elif high_readings:
        status = 'High readings present'
    else:
        status = 'Within range'

    low_ts_str = ', '.join(time_only(ts) for ts, v in sorted(low_readings, key=lambda x: x[0]))
    high_ts_str = ', '.join(time_only(ts) for ts, v in sorted(high_readings, key=lambda x: x[0]))

    return {
        'sd': sd,
        'low_count': len(low_readings),
        'high_count': len(high_readings),
        'status': status,
        'sufficiency': sufficiency,
        'low_ts': low_ts_str if low_ts_str else None,
        'high_ts': high_ts_str if high_ts_str else None,
    }


def compute_spo2_fields(readings_this_hour):
    values = [v for ts, v in readings_this_hour]
    count = len(values)

    if count == 0:
        sufficiency = 'No valid data'
    elif count <= 2:
        sufficiency = 'Limited'
    else:
        sufficiency = 'Sufficient'

    mild_low = [(ts, v) for ts, v in readings_this_hour if 90 <= v < 95]
    marked_low = [(ts, v) for ts, v in readings_this_hour if v < 90]

    sd = None
    if count > 1:
        mean = sum(values) / count
        variance = sum((v - mean) ** 2 for v in values) / count
        sd = round(variance ** 0.5, 2)

    if count == 0:
        status = 'Insufficient data'
    elif mild_low and marked_low:
        status = 'Both mild-low and marked-low readings present'
    elif marked_low:
        status = 'Marked-low readings present'
    elif mild_low:
        status = 'Mild-low readings present'
    else:
        status = 'Expected range only'

    mild_ts_str = ', '.join(time_only(ts) for ts, v in sorted(mild_low, key=lambda x: x[0]))
    marked_ts_str = ', '.join(time_only(ts) for ts, v in sorted(marked_low, key=lambda x: x[0]))

    return {
        'sd': sd,
        'mild_low_count': len(mild_low),
        'marked_low_count': len(marked_low),
        'status': status,
        'sufficiency': sufficiency,
        'mild_low_ts': mild_ts_str if mild_ts_str else None,
        'marked_low_ts': marked_ts_str if marked_ts_str else None,
    }


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


def build_update_for_stage(metric_key, tei, wam_row, stage_uid):
    """Returns list of event update payloads for one metric/stage/participant."""
    col_map = {
        'HR': ('heartrate_filepath', extract_heart_rate),
        'RR': ('respiratory_rate_filepath', extract_respiratory_rate),
        'SPO2': ('oxygen_saturation_filepath', extract_oxygen_saturation),
    }
    col, extractor = col_map[metric_key]
    fp_suffix = wam_row.get(col)
    if not fp_suffix or fp_suffix == 'None':
        return []

    full_path = f"{AI_READI_ROOT}{fp_suffix}" if fp_suffix.startswith('/') else f"{AI_READI_ROOT}/{fp_suffix}"
    try:
        readings = extractor(full_path)
    except Exception as e:
        print(f"    extraction error: {e}")
        return []

    grouped = group_by_hour(readings)

    existing_events = fetch_all_events(tei, stage_uid)

    updates = []
    for event in existing_events:
        occurred_key = event['occurredAt'][:19]  # strip ms/tz
        hour_readings = grouped.get(occurred_key, [])

        if metric_key == 'SPO2':
            computed = compute_spo2_fields(hour_readings)
        else:
            thresholds = {'HR': (60, 100), 'RR': (12, 20)}[metric_key]
            computed = compute_hr_rr_fields(hour_readings, *thresholds)

        fields = FIELD_UIDS[metric_key]
        existing_dvs = list(event['dataValues'])
        existing_de_ids = {dv['dataElement'] for dv in existing_dvs}

        for field_key, value in computed.items():
            de_uid = fields.get(field_key)
            if de_uid and de_uid not in existing_de_ids and value is not None:
                existing_dvs.append({'dataElement': de_uid, 'value': str(value)})

        updates.append({
            'event': event['event'],
            'program': PROGRAM_UID,
            'programStage': stage_uid,
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
    wam_manifest = load_manifest(f'{AI_READI_ROOT}/wearable_activity_monitor/manifest.tsv')
    all_person_ids = sorted(wam_manifest.keys())

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

        wam_row = wam_manifest.get(person_id, {})
        total_updated = 0
        for metric_key in ['HR', 'RR', 'SPO2']:
            updates = build_update_for_stage(metric_key, tei, wam_row, STAGE_UID[metric_key])
            if updates:
                send_update_batch(updates)
                total_updated += len(updates)

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
