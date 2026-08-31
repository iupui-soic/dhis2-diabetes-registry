#!/usr/bin/env python3
"""Recompute the clinical above/below range fields on every CGM glucose event.

Writes, per hourly event:
  Above Range Timestamps and Count  (numeric >180 mg/dL, or device "High")
  Below Range Timestamps and Count  (numeric <70 mg/dL,  or device "Low")

Device High Count and Device Low Count are left alone. Those are the sensor's
own out-of-measurable-range flags, which is a narrower concept.

WHAT THE AUDIT FOUND (C-01, H-04, H-05, H-06), and what changed
----------------------------------------------------------------
H-04 was destructive here, and this is the most important change in the file.
Hours were matched by comparing event["occurredAt"][:19] to a re-formatted
source string. On a miss the old code still ran: it filtered the existing
timestamp values out of the payload, only added them back when the recomputed
string was non-empty, and wrote the counts unconditionally. A systematic key
mismatch would therefore have cleared every timestamp field and stamped "0"
into every count.

Now hours are matched on instants through common.timeutil.hour_key, an event
with no matching source hour is left untouched rather than zeroed, and a run
that fails to match most of a participant's events aborts instead of writing.

USAGE
-----
    nohup python3 "2. wearable script/glucose_recount_step2_backfill.py" > recount.log 2>&1 &
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import aireadi, dhis2  # noqa: E402
from common import metadata_uids as M  # noqa: E402
from common.checkpoint import Checkpoint  # noqa: E402
from common.numeric import is_finite_number  # noqa: E402
from common.timeutil import hour_key, time_only  # noqa: E402

from hourly_aggregation_logic_final import (  # noqa: E402
    GLUCOSE_RANGE_HIGH, GLUCOSE_RANGE_LOW, get_ts,
)

CHECKPOINT_FILE = "glucose_recount_checkpoint.json"


def extract_raw(path):
    with open(path) as fh:
        data = json.load(fh)
    return [
        (get_ts(r["effective_time_frame"]), r["blood_glucose"]["value"])
        for r in data["body"]["cgm"]
    ]


def classify(readings):
    """Split one hour's readings into above and below the clinical range.

    Handles the device sentinels and ignores anything non-numeric that is not
    a recognised sentinel, rather than comparing it to a number.
    """
    above, below = [], []
    for ts, value in readings:
        if value == "High":
            above.append(ts)
        elif value == "Low":
            below.append(ts)
        elif is_finite_number(value):
            if value > GLUCOSE_RANGE_HIGH:
                above.append(ts)
            elif value < GLUCOSE_RANGE_LOW:
                below.append(ts)
    return above, below


def build_updates(session, tei_uid, bg_rows):
    readings = []
    for row in bg_rows:
        path = aireadi.resolve("wearable_blood_glucose", row.get("glucose_filepath"))
        if path:
            readings.extend(extract_raw(path))
    if not readings:
        return [], 0

    grouped = {}
    for ts, value in readings:
        grouped.setdefault(hour_key(ts), []).append((ts, value))

    events = dhis2.fetch_events(
        session, M.PROGRAM_UID, M.CGM_GLUCOSE_STAGE_UID, tei_uid
    )

    updates, unmatched = [], 0
    for event in events:
        hour_readings = grouped.get(hour_key(event["occurredAt"]))
        if hour_readings is None:
            # Leave the event exactly as it is. Writing zeroes here is what
            # made the original version destructive.
            unmatched += 1
            continue

        above, below = classify(hour_readings)
        changes = {
            M.GLUCOSE_FIELD_UIDS["above_ts"]: ", ".join(time_only(t) for t in sorted(above)) or None,
            M.GLUCOSE_FIELD_UIDS["below_ts"]: ", ".join(time_only(t) for t in sorted(below)) or None,
            M.GLUCOSE_FIELD_UIDS["above_count"]: len(above),
            M.GLUCOSE_FIELD_UIDS["below_count"]: len(below),
        }
        updates.append(dhis2.event_update_payload(
            event, M.CGM_GLUCOSE_STAGE_UID, M.PROGRAM_UID,
            dhis2.merge_data_values(event, changes),
        ))

    return updates, unmatched


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--limit", type=int)
    parser.add_argument("--person-id")
    parser.add_argument("--max-unmatched", type=float, default=0.2)
    args, _ = parser.parse_known_args()

    session = dhis2.get_session()
    bg = aireadi.load_manifest("wearable_blood_glucose")
    all_ids = sorted(bg)

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

                updates, unmatched = build_updates(
                    session, context["trackedEntity"], bg.get(person_id, [])
                )
                total = len(updates) + unmatched
                if total and unmatched / total > args.max_unmatched:
                    raise dhis2.Dhis2Error(
                        f"{unmatched} of {total} events had no matching source hour, "
                        f"which is above --max-unmatched. Refusing to write, because "
                        f"this usually means the hour keys are misaligned."
                    )

                if updates:
                    dhis2.send_events(session, updates, "UPDATE")
                checkpoint.mark_done(person_id)
                print(f"[{index}/{len(remaining)}] {person_id}: {len(updates)} updated, "
                      f"{unmatched} unmatched, {time.time() - t0:.1f}s "
                      f"(total {(time.time() - start) / 3600:.2f}h)")

            except Exception as exc:
                checkpoint.mark_failed(person_id, exc)
                print(f"[{index}/{len(remaining)}] {person_id}: FAILED, {str(exc)[:300]}")

    print(f"\nRun complete. {checkpoint.summary()}")
    return 1 if checkpoint.failed else 0


if __name__ == "__main__":
    sys.exit(main())
