"""Hourly aggregation for the wearable and CGM modalities.

Design notes carried forward from the original:
  - Activity: duration-weighted splitting across hour boundaries, because
    pilot data showed segments up to 167 minutes long.
  - Glucose: standard CGM clinical metrics using the 70-180 mg/dL consensus
    range, verified against pilot data to produce real variation.
  - Sleep: kept as raw segments, roughly 50 per participant, so exact stage
    transitions survive at negligible import cost.

WHAT THE AUDIT FOUND (H-09), and what changed
---------------------------------------------
extract_oxygen_saturation returned every reading unfiltered while its three
siblings guarded theirs, and the Garmin SpO2 exports contain NaN. NaN does not
raise, so nothing was caught. It propagated through aggregate_simple and made
the hour's mean NaN, which was then posted as the string "nan" to a NUMBER
data element. Measured over 93 participant files, 77 (83%) contain at least
one NaN; person 4680 had 16 corrupted buckets out of 230.

Every extractor now filters through common.numeric.clean_readings, and every
statistic returns None rather than a non-finite float.

All timestamps in these files are UTC. See common/timeutil.py for the check
that confirms the Z labels are genuine rather than mislabelled local time.
"""

import json
import os
import sys
from collections import defaultdict
from datetime import timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common.numeric import clean_readings, is_finite_number, safe_round  # noqa: E402
from common.timeutil import hour_bucket, parse_instant, to_iso  # noqa: E402

# Values at or below these thresholds are Garmin fill rather than measurements.
HEART_RATE_MIN = 0          # strictly greater than 0
NON_NEGATIVE = 0            # greater than or equal to 0

GLUCOSE_RANGE_LOW = 70
GLUCOSE_RANGE_HIGH = 180


def parse_hour_bucket(ts_str):
    """Hour bucket as an ISO string with an explicit UTC offset."""
    return to_iso(hour_bucket(ts_str))


def get_ts(etf):
    """Timestamp from either a point-in-time or an interval time frame."""
    if "date_time" in etf:
        return etf["date_time"]
    return etf["time_interval"]["start_date_time"]


def _load(fp):
    with open(fp) as fh:
        return json.load(fh)


# ---------------- generic point-in-time mean/min/max/count ----------------

def aggregate_simple(readings):
    """Hourly mean/min/max/count for dense point-in-time readings.

    Used for heart rate, respiratory rate, SpO2 and stress. Any non-finite
    value is dropped before aggregation, so a single NaN can no longer
    destroy an hour's mean while leaving min and max looking correct.
    """
    readings = clean_readings(readings)
    buckets = defaultdict(list)
    for ts, value in readings:
        buckets[parse_hour_bucket(ts)].append(value)

    result = {}
    for hour, values in buckets.items():
        if not values:
            continue
        result[hour] = {
            "mean": safe_round(sum(values) / len(values), 2),
            "min": min(values),
            "max": max(values),
            "count": len(values),
        }
    return result


# ---------------- per-metric extraction and cleaning ----------------

def extract_heart_rate(fp):
    data = _load(fp)
    raw = [
        (get_ts(r["effective_time_frame"]), r["heart_rate"]["value"])
        for r in data["body"]["heart_rate"]
    ]
    return clean_readings(raw, lambda v: v > HEART_RATE_MIN)


def extract_respiratory_rate(fp):
    data = _load(fp)
    raw = [
        (get_ts(r["effective_time_frame"]), r["respiratory_rate"]["value"])
        for r in data["body"]["breathing"] if "respiratory_rate" in r
    ]
    return clean_readings(raw, lambda v: v >= NON_NEGATIVE)


def extract_oxygen_saturation(fp):
    """SpO2 readings, with non-finite values removed.

    This is the H-09 fix. The previous version returned every reading as-is,
    including the NaN present in 83% of participant files.
    """
    data = _load(fp)
    raw = [
        (get_ts(r["effective_time_frame"]), r["oxygen_saturation"]["value"])
        for r in data["body"]["breathing"] if "oxygen_saturation" in r
    ]
    return clean_readings(raw, lambda v: v >= NON_NEGATIVE)


def extract_stress(fp):
    data = _load(fp)
    raw = [
        (get_ts(r["effective_time_frame"]), r["stress"]["value"])
        for r in data["body"]["stress"]
    ]
    return clean_readings(raw, lambda v: v >= NON_NEGATIVE)


def extract_calories(fp):
    data = _load(fp)
    raw = [
        (get_ts(r["effective_time_frame"]), r["calories_value"]["value"])
        for r in data["body"]["activity"] if r.get("activity_name") == "kcal_burned"
    ]
    return clean_readings(raw)


def aggregate_calories(fp):
    """Hourly calorie sum and reading count."""
    hourly_sum = defaultdict(float)
    hourly_count = defaultdict(int)
    for ts, value in extract_calories(fp):
        hour = parse_hour_bucket(ts)
        hourly_sum[hour] += value
        hourly_count[hour] += 1
    return {
        hour: {"sum": safe_round(total, 1), "count": hourly_count[hour]}
        for hour, total in hourly_sum.items()
    }


# ---------------- Sleep: raw segments, not hourly aggregated ----------------

def extract_sleep_segments(fp):
    """One (start_ts, stage, duration_minutes) tuple per raw sleep segment."""
    data = _load(fp)
    valid_stages = {"awake", "light", "deep", "rem"}
    out = []
    for r in data["body"]["sleep"]:
        stage = r.get("sleep_stage_state")
        if stage not in valid_stages:
            continue
        tf = r["effective_time_frame"]["time_interval"]
        start = parse_instant(tf["start_date_time"])
        end = parse_instant(tf["end_date_time"])
        duration = (end - start).total_seconds() / 60
        if not is_finite_number(duration) or duration < 0:
            continue
        out.append((tf["start_date_time"], stage, round(duration, 1)))
    return out


# ---------------- Activity: duration-weighted splitting ----------------

def aggregate_activity(fp):
    """Hourly step sum, prorated across hour boundaries by time overlap.

    Note on 'count': a segment spanning three hours increments the count in
    each of them, so counts are segments-touching-hour and do not sum to the
    participant's total segment count. That is deliberate, but the data
    element is named "Steps Reading Count", which reads as if it were a plain
    total. Tracked separately in the audit as M-09.
    """
    data = _load(fp)
    hourly_sum = defaultdict(float)
    hourly_count = defaultdict(int)

    for r in data["body"]["activity"]:
        value = r.get("base_movement_quantity", {}).get("value")
        if not is_finite_number(value):
            continue
        tf = r["effective_time_frame"]["time_interval"]
        start = parse_instant(tf["start_date_time"])
        end = parse_instant(tf["end_date_time"])
        total_seconds = (end - start).total_seconds()

        if total_seconds <= 0:
            hour = to_iso(hour_bucket(start))
            hourly_sum[hour] += value
            hourly_count[hour] += 1
            continue

        cursor = start
        while cursor < end:
            bucket = hour_bucket(cursor)
            segment_end = min(end, bucket + timedelta(hours=1))
            fraction = (segment_end - cursor).total_seconds() / total_seconds
            key = to_iso(bucket)
            hourly_sum[key] += value * fraction
            hourly_count[key] += 1
            cursor = segment_end

    return {
        hour: {"sum": safe_round(total, 1), "count": hourly_count[hour]}
        for hour, total in hourly_sum.items()
    }


# ---------------- Glucose: clinical CGM metrics ----------------

def aggregate_glucose(fp):
    """Hourly CGM metrics including TIR/TAR/TBR.

    The Dexcom exports use the strings "High" and "Low" for readings outside
    the sensor's measurable range. Those are counted separately and folded
    into the range percentages, which is why the percentages use a larger
    denominator than 'count'. See M-09 in the audit: 'count' is the number of
    numeric readings, and 'count_total' is now returned alongside it so a
    consumer can reconstruct the denominator.
    """
    data = _load(fp)

    buckets = defaultdict(lambda: {"values": [], "high": 0, "low": 0})
    for r in data["body"]["cgm"]:
        value = r["blood_glucose"]["value"]
        hour = parse_hour_bucket(get_ts(r["effective_time_frame"]))
        if value == "High":
            buckets[hour]["high"] += 1
        elif value == "Low":
            buckets[hour]["low"] += 1
        elif is_finite_number(value):
            buckets[hour]["values"].append(value)

    result = {}
    for hour, bucket in buckets.items():
        values = bucket["values"]
        total = len(values) + bucket["high"] + bucket["low"]
        if not total:
            continue

        mean = sum(values) / len(values) if values else None
        sd = None
        if len(values) > 1:
            variance = sum((v - mean) ** 2 for v in values) / len(values)
            sd = safe_round(variance ** 0.5, 2)

        in_range = sum(1 for v in values if GLUCOSE_RANGE_LOW <= v <= GLUCOSE_RANGE_HIGH)
        above = sum(1 for v in values if v > GLUCOSE_RANGE_HIGH) + bucket["high"]
        below = sum(1 for v in values if v < GLUCOSE_RANGE_LOW) + bucket["low"]

        result[hour] = {
            "mean": safe_round(mean, 2) if mean is not None else None,
            "min": min(values) if values else None,
            "max": max(values) if values else None,
            "count": len(values),
            "count_total": total,
            "sd": sd,
            "tir_pct": safe_round(100 * in_range / total, 1),
            "tar_pct": safe_round(100 * above / total, 1),
            "tbr_pct": safe_round(100 * below / total, 1),
            "high_count": bucket["high"],
            "low_count": bucket["low"],
        }
    return result
