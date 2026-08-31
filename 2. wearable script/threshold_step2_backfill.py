#!/usr/bin/env python3
"""Backfill threshold, status, SD and timestamp fields onto HR, RR and SpO2 events.

WHAT THE AUDIT FOUND (C-01, H-01, H-04, H-05, H-06), and what changed
---------------------------------------------------------------------
1. H-01 was overstated, and this is corrected here. This script wrote the
   option display name while the metadata script generated codes. Checked
   against the live server: the stored values are codes (WITHIN_RANGE,
   SUFFICIENT), so DHIS2 2.44 resolved the name rather than rejecting it, and
   no data was lost. Values now go through dhis2.option_value, which reads
   the real code from the server, so the write no longer relies on that
   leniency and cannot drift from the metadata.

2. H-04: hours were matched by comparing event["occurredAt"][:19] against a
   re-formatted source string. That works only while every side happens to
   render UTC. Both sides now go through common.timeutil.hour_key, which
   compares instants.

3. H-05 and H-06: the paging loop returned [] on an HTTP error, and the
   participant was checkpointed complete regardless of whether anything was
   written. Both are fixed in common.

4. A tracker UPDATE replaces an event's data values, so every update now
   carries the event's existing values merged with the new ones.

USAGE
-----
    nohup python3 "2. wearable script/threshold_step2_backfill.py" > threshold.log 2>&1 &
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import aireadi, dhis2  # noqa: E402
from common import metadata_uids as M  # noqa: E402
from common.checkpoint import Checkpoint  # noqa: E402
from common.numeric import safe_round  # noqa: E402
from common.timeutil import hour_key, time_only  # noqa: E402

from hourly_aggregation_logic_final import (  # noqa: E402
    extract_heart_rate, extract_oxygen_saturation, extract_respiratory_rate,
)
from threshold_step1_metadata import (  # noqa: E402
    HR_RR_STATUS_SET, SPO2_STATUS_SET, SUFFICIENCY_SET,
    HR_THRESHOLDS, RR_THRESHOLDS, SPO2_MARKED_LOW, SPO2_MILD_LOW,
    field_names_for,
)

CHECKPOINT_FILE = "threshold_backfill_checkpoint.json"

METRICS = {
    "HR": ("heartrate_filepath", extract_heart_rate, M.WEARABLE_HEART_RATE_STAGE_UID),
    "RR": ("respiratory_rate_filepath", extract_respiratory_rate,
           M.WEARABLE_RESPIRATORY_RATE_STAGE_UID),
    "SPO2": ("oxygen_saturation_filepath", extract_oxygen_saturation,
             M.WEARABLE_SPO2_STAGE_UID),
}


def sufficiency(count):
    """Project-defined rule, not a clinical standard."""
    if count == 0:
        return "No valid data"
    if count <= 2:
        return "Limited"
    return "Sufficient"


def standard_deviation(values):
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return safe_round(variance ** 0.5, 2)


def compute_hr_rr(readings, low_threshold, high_threshold):
    values = [v for _, v in readings]
    low = [(ts, v) for ts, v in readings if v < low_threshold]
    high = [(ts, v) for ts, v in readings if v > high_threshold]

    if not values:
        status = "Insufficient data"
    elif low and high:
        status = "Both low and high readings present"
    elif low:
        status = "Low readings present"
    elif high:
        status = "High readings present"
    else:
        status = "Within range"

    return {
        "sd": standard_deviation(values),
        "low_count": len(low),
        "high_count": len(high),
        "status": dhis2.option_value(HR_RR_STATUS_SET, status),
        "sufficiency": dhis2.option_value(SUFFICIENCY_SET, sufficiency(len(values))),
        "low_ts": ", ".join(time_only(ts) for ts, _ in sorted(low)) or None,
        "high_ts": ", ".join(time_only(ts) for ts, _ in sorted(high)) or None,
    }


def compute_spo2(readings):
    values = [v for _, v in readings]
    mild = [(ts, v) for ts, v in readings if SPO2_MARKED_LOW <= v < SPO2_MILD_LOW]
    marked = [(ts, v) for ts, v in readings if v < SPO2_MARKED_LOW]

    if not values:
        status = "Insufficient data"
    elif mild and marked:
        status = "Both mild-low and marked-low readings present"
    elif marked:
        status = "Marked-low readings present"
    elif mild:
        status = "Mild-low readings present"
    else:
        status = "Expected range only"

    return {
        "sd": standard_deviation(values),
        "mild_low_count": len(mild),
        "marked_low_count": len(marked),
        "status": dhis2.option_value(SPO2_STATUS_SET, status),
        "sufficiency": dhis2.option_value(SUFFICIENCY_SET, sufficiency(len(values))),
        "mild_low_ts": ", ".join(time_only(ts) for ts, _ in sorted(mild)) or None,
        "marked_low_ts": ", ".join(time_only(ts) for ts, _ in sorted(marked)) or None,
    }


def group_by_hour(readings):
    grouped = {}
    for ts, value in readings:
        grouped.setdefault(hour_key(ts), []).append((ts, value))
    return grouped


def build_updates(session, registry, metric, tei_uid, wam_rows):
    column, extractor, stage_uid = METRICS[metric]
    field_uids = {
        key: registry.data_element(name)
        for key, name in field_names_for(metric).items()
    }

    # Every manifest row, not just the first: a participant can have more
    # than one wearable period.
    readings = []
    for row in wam_rows:
        path = aireadi.resolve("wearable_activity_monitor", row.get(column))
        if path:
            readings.extend(extractor(path))
    if not readings:
        return [], 0

    grouped = group_by_hour(readings)
    events = dhis2.fetch_events(session, M.PROGRAM_UID, stage_uid, tei_uid)

    updates, unmatched = [], 0
    for event in events:
        key = hour_key(event["occurredAt"])
        hour_readings = grouped.get(key)
        if hour_readings is None:
            # No source hour for this event. Do not write "no valid data"
            # over it, because that would be indistinguishable from a genuine
            # empty hour. Count it and let the caller decide.
            unmatched += 1
            continue

        computed = (compute_spo2(hour_readings) if metric == "SPO2"
                    else compute_hr_rr(hour_readings,
                                       *(HR_THRESHOLDS if metric == "HR" else RR_THRESHOLDS)))

        changes = {field_uids[k]: v for k, v in computed.items() if k in field_uids}
        updates.append(dhis2.event_update_payload(
            event, stage_uid, M.PROGRAM_UID,
            dhis2.merge_data_values(event, changes),
        ))

    return updates, unmatched


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--limit", type=int)
    parser.add_argument("--person-id")
    parser.add_argument("--max-unmatched", type=float, default=0.2,
                        help="abort a participant if more than this fraction of "
                             "their events have no matching source hour")
    args, _ = parser.parse_known_args()

    session = dhis2.get_session()
    registry = M.load(session)
    wam = aireadi.load_manifest("wearable_activity_monitor")
    all_ids = sorted(wam)

    with Checkpoint(CHECKPOINT_FILE, flush_every=5) as checkpoint:
        remaining = [args.person_id] if args.person_id else checkpoint.pending(all_ids)
        if args.limit:
            remaining = remaining[:args.limit]
        print(f"Total {len(all_ids)}, {checkpoint.summary()}, {len(remaining)} to process")
        start = time.time()

        for index, person_id in enumerate(remaining, start=1):
            t0 = time.time()
            try:
                context = dhis2.get_tei_context(
                    session, M.PROGRAM_UID, M.PERSON_ID_ATTR_UID, person_id
                )
                if context is None:
                    checkpoint.mark_done(person_id, note="no tracked entity")
                    continue

                total_updated, total_unmatched, total_events = 0, 0, 0
                for metric in METRICS:
                    updates, unmatched = build_updates(
                        session, registry, metric,
                        context["trackedEntity"], wam.get(person_id, []),
                    )
                    total_unmatched += unmatched
                    total_events += len(updates) + unmatched
                    if updates:
                        dhis2.send_events(session, updates, "UPDATE")
                        total_updated += len(updates)

                if total_events and total_unmatched / total_events > args.max_unmatched:
                    raise dhis2.Dhis2Error(
                        f"{total_unmatched} of {total_events} events had no matching "
                        f"source hour. That is above --max-unmatched, which usually "
                        f"means the hour keys are misaligned rather than that data is "
                        f"genuinely missing. Not marking this participant complete."
                    )

                checkpoint.mark_done(person_id)
                print(f"[{index}/{len(remaining)}] {person_id}: {total_updated} updated, "
                      f"{total_unmatched} unmatched, {time.time() - t0:.1f}s "
                      f"(total {(time.time() - start) / 3600:.2f}h)")

            except Exception as exc:
                checkpoint.mark_failed(person_id, exc)
                print(f"[{index}/{len(remaining)}] {person_id}: FAILED, {str(exc)[:300]}")

    print(f"\nRun complete. {checkpoint.summary()}")
    return 1 if checkpoint.failed else 0


if __name__ == "__main__":
    sys.exit(main())
