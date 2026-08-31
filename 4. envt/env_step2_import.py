#!/usr/bin/env python3
"""Import hourly environment sensor summaries for every participant.

WHAT THE AUDIT FOUND (C-01, H-05, H-06, M-20), and what changed
----------------------------------------------------------------
1. C-01: credentials and the dataset root come from the environment, and the
   stage UIDs resolve by name instead of being pasted in from step 1.

2. H-05: send_batch returned nothing and the participant was checkpointed
   complete regardless of whether the import succeeded.

3. M-20: occurredAt was a naive isoformat while every other modality sent an
   explicit +00:00. The underlying data agrees, since the environment CSVs
   are UTC despite carrying no suffix, but a naive value is interpreted in the
   server's timezone. read_env_csv now stamps UTC, so all modalities
   serialise identically.

USAGE
-----
    nohup python3 "4. envt/env_step2_import.py" > env_import.log 2>&1 &
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import aireadi, dhis2  # noqa: E402
from common import metadata_uids as M  # noqa: E402
from common.checkpoint import Checkpoint  # noqa: E402
from common.timeutil import format_display, hour_end, to_iso  # noqa: E402

from env_aggregation_logic_v2 import (  # noqa: E402
    RELEVANT_COLUMNS, aggregate_env_column, read_env_csv,
)

CHECKPOINT_FILE = "env_checkpoint.json"
BATCH_SIZE = 200

# Only humidity has extra threshold fields whose UIDs this repo recorded.
# Temperature's are resolved by name alongside everything else.
EXTRA_FIELD_NAMES = {
    "hum": {
        "above_count": "Humidity Above Comfort Range (>50%) - Count",
        "above_ts": "Humidity Above Comfort Range (>50%) - Timestamps",
        "below_count": "Humidity Below Comfort Range (<30%) - Count",
        "below_ts": "Humidity Below Comfort Range (<30%) - Timestamps",
    },
    "temp": {
        "above_count": "Temperature Above Study Comfort Range (>24C) - Count",
        "above_ts": "Temperature Above Study Comfort Range (>24C) - Timestamps",
        "below_count": "Temperature Below Study Comfort Range (<20C) - Count",
        "below_ts": "Temperature Below Study Comfort Range (<20C) - Timestamps",
    },
}


def build_field_map(registry):
    """Resolve every stage and field UID once, before any participant runs."""
    base = registry.env_field_uids()
    config = {}
    for column in RELEVANT_COLUMNS:
        fields = dict(base)
        for key, name in EXTRA_FIELD_NAMES.get(column, {}).items():
            uid = registry.maybe_data_element(name)
            if uid:
                fields[key] = uid
        config[column] = {
            "stage": registry.environment_stage(column),
            "fields": fields,
        }
    return config


def build_events(config, context, rows):
    events = []
    for column in RELEVANT_COLUMNS:
        cfg = config[column]
        for hour, stats in aggregate_env_column(rows, column).items():
            data_values = []
            for key in ("mean", "min", "max", "sd", "count",
                        "above_count", "above_ts", "below_count", "below_ts"):
                entry = dhis2.data_value(cfg["fields"].get(key), stats.get(key))
                if entry:
                    data_values.append(entry)

            for key, value in (("hour_start", format_display(hour)),
                               ("hour_end", format_display(hour_end(hour)))):
                entry = dhis2.data_value(cfg["fields"].get(key), value)
                if entry:
                    data_values.append(entry)

            if not data_values:
                continue
            events.append({
                "program": M.PROGRAM_UID,
                "programStage": cfg["stage"],
                "trackedEntity": context["trackedEntity"],
                "enrollment": context["enrollment"],
                "orgUnit": context["orgUnit"],
                "occurredAt": to_iso(hour),
                "status": "COMPLETED",
                "dataValues": data_values,
            })
    return events


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--limit", type=int)
    parser.add_argument("--person-id")
    args, _ = parser.parse_known_args()

    session = dhis2.get_session()
    registry = M.load(session)
    config = build_field_map(registry)

    manifest = aireadi.load_manifest("environment")
    all_ids = sorted(manifest)

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

                rows = []
                for row in manifest.get(person_id, []):
                    path = aireadi.resolve("environment", row.get("env_sensor_filepath"))
                    if path:
                        rows.extend(read_env_csv(path))

                events = build_events(config, context, rows) if rows else []
                if events:
                    dhis2.send_events(session, events, "CREATE", batch_size=BATCH_SIZE)
                checkpoint.mark_done(person_id)
                print(f"[{index}/{len(remaining)}] {person_id}: {len(events)} events, "
                      f"{time.time() - t0:.1f}s (total {(time.time() - start) / 3600:.2f}h)")

            except Exception as exc:
                checkpoint.mark_failed(person_id, exc)
                print(f"[{index}/{len(remaining)}] {person_id}: FAILED, {str(exc)[:300]}")

    print(f"\nRun complete. {checkpoint.summary()}")
    return 1 if checkpoint.failed else 0


if __name__ == "__main__":
    sys.exit(main())
