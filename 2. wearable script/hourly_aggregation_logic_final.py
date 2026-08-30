"""
Hourly aggregation functions - FINAL version incorporating:
  - Activity: duration-weighted splitting across hour boundaries (fixes
    the earlier start-time-only bucketing, which misattributed steps for
    segments up to 167 minutes long)
  - Glucose: added standard deviation and standard CGM clinical metrics
    (Time in Range / Time Above Range / Time Below Range, using the
    standard 70-180 mg/dL threshold), justified by real pilot data showing
    genuine variation across participants (0% to 94.6% TIR)
  - Sleep: unchanged - duration-weighted minutes per stage, justified by
    real data showing segments up to 111 minutes long (spanning hours)
"""

import json
from collections import defaultdict
from datetime import datetime, timedelta


def parse_hour_bucket(ts_str):
    dt = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
    return dt.replace(minute=0, second=0, microsecond=0).isoformat()


def get_ts(etf):
    if 'date_time' in etf:
        return etf['date_time']
    return etf['time_interval']['start_date_time']


# ---------------- generic point-in-time mean/min/max/count ----------------

def aggregate_simple(readings):
    """readings: list of (timestamp, value). Used for HR, Respiratory Rate,
    SpO2, Stress - all point-in-time readings, confirmed dense enough
    (17-60 readings/hour in pilot data) for meaningful stats."""
    buckets = defaultdict(list)
    for ts, val in readings:
        buckets[parse_hour_bucket(ts)].append(val)

    result = {}
    for hour, values in buckets.items():
        result[hour] = {
            'mean': round(sum(values) / len(values), 2),
            'min': min(values),
            'max': max(values),
            'count': len(values),
        }
    return result


# ---------------- per-metric extraction + cleaning (unchanged from pilot) ----------------

def extract_heart_rate(fp):
    with open(fp) as f:
        data = json.load(f)
    return [(get_ts(r['effective_time_frame']), r['heart_rate']['value'])
            for r in data['body']['heart_rate']
            if r['heart_rate']['value'] is not None and r['heart_rate']['value'] > 0]


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
    return [(get_ts(r['effective_time_frame']), r['oxygen_saturation']['value'])
            for r in data['body']['breathing'] if 'oxygen_saturation' in r]


def extract_stress(fp):
    with open(fp) as f:
        data = json.load(f)
    out = []
    for r in data['body']['stress']:
        v = r['stress']['value']
        if v is not None and v >= 0:
            out.append((get_ts(r['effective_time_frame']), v))
    return out


def extract_calories(fp):
    with open(fp) as f:
        data = json.load(f)
    return [(get_ts(r['effective_time_frame']), r['calories_value']['value'])
            for r in data['body']['activity'] if r.get('activity_name') == 'kcal_burned']


def extract_glucose(fp):
    with open(fp) as f:
        data = json.load(f)
    return [(get_ts(r['effective_time_frame']), r['blood_glucose']['value'])
            for r in data['body']['cgm']]


# ---------------- Sleep: RAW SEGMENTS, not hourly aggregated ----------------
# Per professor's recommendation - sleep segments are naturally low-volume
# (~50 segments/participant over ~18 days in pilot data), so keeping them
# raw preserves exact stage transitions at negligible cost to import scale.

def extract_sleep_segments(fp):
    """Returns list of (start_ts, stage, duration_minutes) - one per raw
    segment, no hourly bucketing."""
    with open(fp) as f:
        data = json.load(f)
    valid_stages = {'awake', 'light', 'deep', 'rem'}
    out = []
    for r in data['body']['sleep']:
        stage = r['sleep_stage_state']
        if stage not in valid_stages:
            continue
        tf = r['effective_time_frame']['time_interval']
        start = datetime.fromisoformat(tf['start_date_time'].replace('Z', '+00:00'))
        end = datetime.fromisoformat(tf['end_date_time'].replace('Z', '+00:00'))
        duration_min = round((end - start).total_seconds() / 60, 1)
        out.append((tf['start_date_time'], stage, duration_min))
    return out


# ---------------- Sleep: duration-weighted minutes per stage ----------------
# Justified by pilot data: 1 of 50 segments exceeded 60 minutes (max 111 min),
# confirming cross-hour splitting is necessary, not overengineering.

def aggregate_sleep(fp):
    with open(fp) as f:
        data = json.load(f)

    hourly = defaultdict(lambda: defaultdict(float))
    valid_stages = {'awake', 'light', 'deep', 'rem'}

    for r in data['body']['sleep']:
        stage = r['sleep_stage_state']
        if stage not in valid_stages:
            continue
        tf = r['effective_time_frame']['time_interval']
        start = datetime.fromisoformat(tf['start_date_time'].replace('Z', '+00:00'))
        end = datetime.fromisoformat(tf['end_date_time'].replace('Z', '+00:00'))

        cur = start
        while cur < end:
            hour_start = cur.replace(minute=0, second=0, microsecond=0)
            hour_end = hour_start + timedelta(hours=1)
            segment_end = min(end, hour_end)
            minutes = (segment_end - cur).total_seconds() / 60
            hourly[hour_start.isoformat()][stage] += minutes
            cur = segment_end

    result = {}
    for hour, stages in hourly.items():
        result[hour] = {
            'awake_minutes': round(stages.get('awake', 0), 1),
            'light_minutes': round(stages.get('light', 0), 1),
            'deep_minutes': round(stages.get('deep', 0), 1),
            'rem_minutes': round(stages.get('rem', 0), 1),
        }
    return result


# ---------------- Activity: FIXED with duration-weighted splitting ----------------
# Pilot data showed segments up to 167 minutes long (avg 8.2 segments/hour),
# meaning the original start-hour-only bucketing misattributed steps for any
# segment spanning multiple hours. Now prorates steps by time overlap,
# same technique as Sleep.

def aggregate_activity(fp):
    with open(fp) as f:
        data = json.load(f)

    hourly_sum = defaultdict(float)
    hourly_count = defaultdict(int)

    for r in data['body']['activity']:
        v = r['base_movement_quantity']['value']
        if v == '' or v is None:
            continue
        tf = r['effective_time_frame']['time_interval']
        start = datetime.fromisoformat(tf['start_date_time'].replace('Z', '+00:00'))
        end = datetime.fromisoformat(tf['end_date_time'].replace('Z', '+00:00'))
        total_seconds = (end - start).total_seconds()

        if total_seconds <= 0:
            # zero-duration reading - attribute fully to its start hour
            hour = start.replace(minute=0, second=0, microsecond=0).isoformat()
            hourly_sum[hour] += v
            hourly_count[hour] += 1
            continue

        cur = start
        while cur < end:
            hour_start = cur.replace(minute=0, second=0, microsecond=0)
            hour_end = hour_start + timedelta(hours=1)
            segment_end = min(end, hour_end)
            frac = (segment_end - cur).total_seconds() / total_seconds
            hourly_sum[hour_start.isoformat()] += v * frac
            hourly_count[hour_start.isoformat()] += 1  # segment touched this hour
            cur = segment_end

    result = {}
    for hour, total in hourly_sum.items():
        result[hour] = {'sum': round(total, 1), 'count': hourly_count[hour]}
    return result


# ---------------- Glucose: mean/min/max/count/SD + standard CGM clinical metrics ----------------
# TIR/TAR/TBR use the standard single-tier threshold (70-180 mg/dL), the
# most widely used CGM consensus metric. Verified against real pilot data:
# person 1027 showed 0% TIR / 100% TAR (severe, sustained hyperglycemia,
# consistent with insulin_dependent severity), person 1023 showed 94.6% TIR
# (normal range) - confirms the calculation produces real, meaningful
# variation, not a degenerate result.

def aggregate_glucose(fp):
    with open(fp) as f:
        data = json.load(f)

    buckets = defaultdict(lambda: {'values': [], 'high': 0, 'low': 0})
    for r in data['body']['cgm']:
        val = r['blood_glucose']['value']
        ts = get_ts(r['effective_time_frame'])
        hour = parse_hour_bucket(ts)
        if val == 'High':
            buckets[hour]['high'] += 1
        elif val == 'Low':
            buckets[hour]['low'] += 1
        else:
            buckets[hour]['values'].append(val)

    result = {}
    for hour, d in buckets.items():
        vals = d['values']
        total = len(vals) + d['high'] + d['low']
        if not total:
            continue

        mean = sum(vals) / len(vals) if vals else None
        if vals and len(vals) > 1:
            variance = sum((v - mean) ** 2 for v in vals) / len(vals)
            sd = variance ** 0.5
        else:
            sd = None

        in_range = sum(1 for v in vals if 70 <= v <= 180)
        above_range = sum(1 for v in vals if v > 180) + d['high']
        below_range = sum(1 for v in vals if v < 70) + d['low']

        result[hour] = {
            'mean': round(mean, 2) if mean is not None else None,
            'min': min(vals) if vals else None,
            'max': max(vals) if vals else None,
            'count': len(vals),
            'sd': round(sd, 2) if sd is not None else None,
            'tir_pct': round(100 * in_range / total, 1),
            'tar_pct': round(100 * above_range / total, 1),
            'tbr_pct': round(100 * below_range / total, 1),
            'high_count': d['high'],
            'low_count': d['low'],
        }
    return result
