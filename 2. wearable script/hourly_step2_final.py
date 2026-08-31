#!/usr/bin/env python3
"""Hourly aggregated import of the wearable and CGM modalities.

WHAT THE AUDIT FOUND (C-01, H-05, H-06, H-09), and what changed
---------------------------------------------------------------
1. Credentials were a REPLACE_ME constant, and the stage UIDs were a block
   pasted in by hand after running step 1. Both now resolve automatically:
   credentials from the environment, UIDs from common.metadata_uids.

2. send_batch returned nothing and only printed on failure, and the caller
   then checkpointed the participant unconditionally. A participant whose
   entire batch was rejected was recorded as done and skipped forever after.
   Writes now raise on rejection and the checkpoint records the failure.

3. The per-stage try/except caught every exception and moved on, so a
   systematic bug looked identical to a missing file. Failures are now
   attributed to the participant and re-tried on the next run.

USAGE
-----
    nohup python3 "2. wearable script/hourly_step2_final.py" > hourly_import.log 2>&1 &
    tail -f hourly_import.log
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import aireadi, dhis2  # noqa: E402
from common import metadata_uids as M  # noqa: E402
from common.checkpoint import Checkpoint  # noqa: E402

from hourly_aggregation_logic_final import (  # noqa: E402
    aggregate_activity, aggregate_calories, aggregate_glucose, aggregate_simple,
    extract_heart_rate, extract_oxygen_saturation, extract_respiratory_rate,
    extract_sleep_segments, extract_stress,
)

CHECKPOINT_FILE = "hourly_checkpoint_final.json"
BATCH_SIZE = 200

CONTINUOUS = [
    ("Wearable - Heart Rate", "heartrate_filepath", extract_heart_rate),
    ("Wearable - Respiratory Rate", "respiratory_rate_filepath", extract_respiratory_rate),
    ("Wearable - SpO2", "oxygen_saturation_filepath", extract_oxygen_saturation),
    ("Wearable - Stress", "stress_level_filepath", extract_stress),
]


def stage_config(registry):
    """Resolve every stage UID and its field UIDs once, up front."""
    shared = registry.wearable_shared_uids()
    config = {}
    for name, _, _ in CONTINUOUS:
        config[name] = {"stage": registry.stage(name), "fields": dict(shared)}
    config["Wearable - Sleep"] = {
        "stage": registry.stage("Wearable - Sleep"),
        "fields": {
            "stage": registry.data_element("Sleep Stage"),
            "duration_minutes": registry.data_element("Sleep Segment Duration Minutes"),
        },
    }
    config["Wearable - Activity"] = {
        "stage": registry.stage("Wearable - Activity"),
        "fields": {
            "sum": registry.data_element("Steps Sum"),
            "count": registry.data_element("Steps Reading Count"),
        },
    }
    config["Wearable - Calories"] = {
        "stage": registry.stage("Wearable - Calories"),
        "fields": {
            "sum": registry.data_element("Calories Sum"),
            "count": registry.data_element("Calories Reading Count"),
        },
    }
    config["CGM - Glucose"] = {
        "stage": registry.stage("CGM - Glucose"),
        "fields": dict(M.GLUCOSE_FIELD_UIDS),
    }
    return config


def make_event(config, stage_name, context, occurred_at, values):
    cfg = config[stage_name]
    data_values = []
    for key, value in values.items():
        entry = dhis2.data_value(cfg["fields"].get(key), value)
        if entry:
            data_values.append(entry)
    if not data_values:
        return None
    return {
        "program": M.PROGRAM_UID,
        "programStage": cfg["stage"],
        "trackedEntity": context["trackedEntity"],
        "enrollment": context["enrollment"],
        "orgUnit": context["orgUnit"],
        "occurredAt": occurred_at,
        "status": "COMPLETED",
        "dataValues": data_values,
    }


def build_events(config, context, wam_row, bg_row):
    """Build every event for one participant.

    Raises on a genuine failure rather than swallowing it, so the caller can
    record the participant as failed instead of complete.
    """
    events = []

    for stage_name, column, extractor in CONTINUOUS:
        path = aireadi.resolve("wearable_activity_monitor", wam_row.get(column))
        if not path:
            continue
        for hour, stats in aggregate_simple(extractor(path)).items():
            event = make_event(config, stage_name, context, hour, stats)
            if event:
                events.append(event)

    path = aireadi.resolve("wearable_activity_monitor", wam_row.get("sleep_filepath"))
    if path:
        for start_ts, sleep_stage, duration in extract_sleep_segments(path):
            event = make_event(config, "Wearable - Sleep", context, start_ts,
                               {"stage": sleep_stage, "duration_minutes": duration})
            if event:
                events.append(event)

    path = aireadi.resolve("wearable_activity_monitor", wam_row.get("physical_activity_filepath"))
    if path:
        for hour, stats in aggregate_activity(path).items():
            event = make_event(config, "Wearable - Activity", context, hour, stats)
            if event:
                events.append(event)

    path = aireadi.resolve("wearable_activity_monitor", wam_row.get("active_calories_filepath"))
    if path:
        for hour, stats in aggregate_calories(path).items():
            event = make_event(config, "Wearable - Calories", context, hour, stats)
            if event:
                events.append(event)

    path = aireadi.resolve("wearable_blood_glucose", bg_row.get("glucose_filepath"))
    if path:
        for hour, stats in aggregate_glucose(path).items():
            event = make_event(config, "CGM - Glucose", context, hour, stats)
            if event:
                events.append(event)

    return events


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--limit", type=int, help="process at most this many participants")
    parser.add_argument("--person-id", help="process a single participant")
    args, _ = parser.parse_known_args()

    session = dhis2.get_session()
    registry = M.load(session)
    config = stage_config(registry)

    wam = aireadi.load_manifest_first("wearable_activity_monitor")
    bg = aireadi.load_manifest_first("wearable_blood_glucose")
    all_ids = sorted(set(wam) | set(bg))

    with Checkpoint(CHECKPOINT_FILE, flush_every=5) as checkpoint:
        if args.person_id:
            remaining = [args.person_id]
        else:
            remaining = checkpoint.pending(all_ids)
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
                    print(f"[{index}/{len(remaining)}] {person_id}: no TEI, skipped")
                    continue

                events = build_events(
                    config, context, wam.get(person_id, {}), bg.get(person_id, {})
                )
                if events:
                    dhis2.send_events(session, events, "CREATE", batch_size=BATCH_SIZE)
                checkpoint.mark_done(person_id)
                print(f"[{index}/{len(remaining)}] {person_id}: {len(events)} events, "
                      f"{time.time() - t0:.1f}s (total {(time.time() - start) / 3600:.2f}h)")

            except Exception as exc:
                checkpoint.mark_failed(person_id, exc)
                print(f"[{index}/{len(remaining)}] {person_id}: FAILED, {str(exc)[:300]}")

    print(f"\nRun complete. {checkpoint.summary()}")
    if checkpoint.failed:
        print(f"{len(checkpoint.failed)} participant(s) failed and will be retried "
              f"on the next run. See {CHECKPOINT_FILE}.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
