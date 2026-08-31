"""
Reads raw AI-READI environmental sensor CSV files and aggregates readings
into hourly summaries for the 8 relevant columns.

CORRECTED per review:
  - NOx: no threshold at all. WHO's 25 ug/m3 guideline is defined for NO2
    specifically; the sensor measures combined NOx, so applying that
    guideline here would misrepresent what is being measured.
  - PM2.5 / PM10: no per-reading/per-hour threshold flagging. WHO's 15 and
    45 ug/m3 values are 24-HOUR MEAN guidelines, not thresholds for
    individual raw or hourly readings. Comparing an hourly average directly
    against a 24-hour guideline would misrepresent the guideline. Hourly
    Mean/Min/Max/SD/Count are still computed and stored; a genuine 24-hour
    (daily) summary derived from consecutive hourly means, with a proper
    guideline-exceeded flag, is planned as a SEPARATE later addition once
    hourly data exists - not built in this pass.
  - Humidity: Above 50% / Below 30% retained - EPA/ASHRAE do support this
    as a general indoor comfort target range.
  - Temperature: retained as a threshold, but explicitly labelled as a
    STUDY-DEFINED comfort range (20-24C), not presented as a direct,
    literal ASHRAE Standard 55 cutoff (ASHRAE 55 defines comfort via a
    model combining temperature, humidity, air speed, clothing, and
    metabolic rate - not a simple fixed range).
  - Standard deviation added for every column.
"""

import csv
import math
import os
import sys
from datetime import datetime, timezone
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common.numeric import safe_round  # noqa: E402
from common.timeutil import time_only  # noqa: E402

# The environment CSVs carry no timezone suffix, but they are UTC. Confirmed
# by diurnal check: for UW participants the daily indoor temperature maximum
# falls at 23:00-03:00 in the file's own labels, which is 16:00-20:00 local
# Pacific, the expected afternoon peak. Treating them as local time would put
# the peak in the middle of the night. Stamping UTC here means environment
# events serialise with an explicit offset like every other modality. (M-20)

RELEVANT_COLUMNS = ['pm1', 'pm2.5', 'pm4', 'pm10', 'hum', 'temp', 'voc', 'nox']

# Only humidity and temperature get threshold fields. See module docstring
# for why PM2.5, PM10, and NOx do not.
THRESHOLDS = {
    'hum': {'below': 30, 'above': 50},
    'temp': {'below': 20, 'above': 24},
}


def read_env_csv(filepath):
    """Returns list of dicts: [{'ts': datetime, 'pm1': float, ...}, ...]
    Skips a column's value (not the whole row) if it is missing/nan/inf/
    unparseable for that specific column."""
    rows = []
    with open(filepath) as f:
        header = None
        for line in f:
            if line.startswith('ts,'):
                header = line.strip().split(',')
                break
        if header is None:
            return rows

        col_idx = {name: header.index(name) for name in RELEVANT_COLUMNS if name in header}

        reader = csv.reader(f)
        for parts in reader:
            if not parts or len(parts) < len(header):
                continue
            try:
                ts = datetime.strptime(parts[0], '%Y-%m-%d %H:%M:%S').replace(
                    tzinfo=timezone.utc)
            except (ValueError, IndexError):
                continue

            row = {'ts': ts}
            for col_name, idx in col_idx.items():
                try:
                    val = float(parts[idx])
                    if math.isnan(val) or math.isinf(val):
                        continue
                    row[col_name] = val
                except (ValueError, IndexError):
                    continue
            rows.append(row)
    return rows


def parse_hour_bucket(dt):
    return dt.replace(minute=0, second=0, microsecond=0)


MAX_FLAGGED_TIMESTAMPS = 20


def summarise_timestamps(flagged):
    """Comma-separated flagged times, truncated so one value cannot balloon.

    M-19: the sensor samples roughly every five seconds, so an hour entirely
    outside the comfort range produced about 720 timestamps, near 6.5 KB, in
    a single LONG_TEXT value. The paired count field already carries the
    magnitude, so the list is capped and says how many it omitted.
    """
    if not flagged:
        return None
    times = [time_only(ts) for ts, _ in sorted(flagged)]
    if len(times) <= MAX_FLAGGED_TIMESTAMPS:
        return ', '.join(times)
    shown = ', '.join(times[:MAX_FLAGGED_TIMESTAMPS])
    return f"{shown}, ... and {len(times) - MAX_FLAGGED_TIMESTAMPS} more"


def aggregate_env_column(rows, column):
    """Groups one column's valid readings by hour: mean/min/max/SD/count,
    plus above/below threshold count+timestamps ONLY for columns listed
    in THRESHOLDS (currently just humidity and temperature)."""
    hourly = defaultdict(list)
    for row in rows:
        if column not in row:
            continue
        hour = parse_hour_bucket(row['ts'])
        hourly[hour].append((row['ts'], row[column]))

    thresholds = THRESHOLDS.get(column, {})
    result = {}
    for hour, readings in hourly.items():
        values = [v for ts, v in readings]
        mean = sum(values) / len(values)

        if len(values) > 1:
            variance = sum((v - mean) ** 2 for v in values) / len(values)
            sd = safe_round(variance ** 0.5, 3)
        else:
            sd = None

        entry = {
            'mean': safe_round(mean, 3),
            'min': min(values),
            'max': max(values),
            'sd': sd,
            'count': len(values),
        }

        if 'above' in thresholds:
            above = [(ts, v) for ts, v in readings if v > thresholds['above']]
            entry['above_count'] = len(above)
            entry['above_ts'] = summarise_timestamps(above)

        if 'below' in thresholds:
            below = [(ts, v) for ts, v in readings if v < thresholds['below']]
            entry['below_count'] = len(below)
            entry['below_ts'] = summarise_timestamps(below)

        result[hour] = entry

    return result
