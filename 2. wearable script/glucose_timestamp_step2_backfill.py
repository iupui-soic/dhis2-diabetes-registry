#!/usr/bin/env python3
"""Backfill device-sentinel timestamps onto CGM glucose events.

SUPERSEDED. Prefer glucose_recount_step2_backfill.py.
------------------------------------------------------
This script writes only the device "High" and "Low" sentinel times into the
Above/Below Range Timestamp fields. glucose_recount_step2_backfill.py writes
the same two fields using the combined clinical definition, which is numeric
>180 or <70 mg/dL OR the device sentinel, and also maintains the matching
count fields.

Running this after the recount therefore REGRESSES those fields to the
narrower device-only meaning. It refuses to run without --i-know-this-is-
superseded for that reason.

WHAT THE AUDIT FOUND (C-01, H-04, H-05, H-06), and what changed
----------------------------------------------------------------
Credentials come from the environment; hours are matched on instants rather
than on the first 19 characters of a formatted string; the paging loop raises
instead of returning an empty list; updates carry the event's full data value
set; and a participant is only checkpointed once the write is confirmed.
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
from common.timeutil import hour_key, time_only  # noqa: E402

from hourly_aggregation_logic_final import get_ts  # noqa: E402

CHECKPOINT_FILE = "glucose_ts_checkpoint.json"


def extract_raw(path):
    with open(path) as fh:
        data = json.load(fh)
    return [
        (get_ts(r["effective_time_frame"]), r["blood_glucose"]["value"])
        for r in data["body"]["cgm"]
    ]


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

    events = dhis2.fetch_events(session, M.PROGRAM_UID, M.CGM_GLUCOSE_STAGE_UID, tei_uid)

    updates, unmatched = [], 0
    for event in events:
        hour_readings = grouped.get(hour_key(event["occurredAt"]))
        if hour_readings is None:
            unmatched += 1
            continue

        high = sorted(ts for ts, v in hour_readings if v == "High")
        low = sorted(ts for ts, v in hour_readings if v == "Low")
        changes = {
            M.GLUCOSE_FIELD_UIDS["above_ts"]: ", ".join(time_only(t) for t in high) or None,
            M.GLUCOSE_FIELD_UIDS["below_ts"]: ", ".join(time_only(t) for t in low) or None,
        }
        updates.append(dhis2.event_update_payload(
            event, M.CGM_GLUCOSE_STAGE_UID, M.PROGRAM_UID,
            dhis2.merge_data_values(event, changes),
        ))
    return updates, unmatched


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--i-know-this-is-superseded", action="store_true",
                        help="required, because this narrows fields that "
                             "glucose_recount_step2_backfill.py sets correctly")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--person-id")
    parser.add_argument("--max-unmatched", type=float, default=0.2)
    args, _ = parser.parse_known_args()

    if not getattr(args, "i_know_this_is_superseded"):
        print(__doc__)
        print("Refusing to run. Use glucose_recount_step2_backfill.py instead.")
        return 2

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
                        f"{unmatched} of {total} events had no matching source hour"
                    )
                if updates:
                    dhis2.send_events(session, updates, "UPDATE")
                checkpoint.mark_done(person_id)
                print(f"[{index}/{len(remaining)}] {person_id}: {len(updates)} updated, "
                      f"{unmatched} unmatched (total {(time.time() - start) / 3600:.2f}h)")
            except Exception as exc:
                checkpoint.mark_failed(person_id, exc)
                print(f"[{index}/{len(remaining)}] {person_id}: FAILED, {str(exc)[:300]}")

    print(f"\nRun complete. {checkpoint.summary()}")
    return 1 if checkpoint.failed else 0


if __name__ == "__main__":
    sys.exit(main())
