#!/usr/bin/env python3
"""Backfill "Hour Start" and "Hour End" onto every wearable and CGM event.

For Sleep, which stores raw segments rather than fixed buckets, Hour End is
the segment's own duration after Hour Start. For every other stage it is one
hour after Hour Start.

WHAT THE AUDIT FOUND (C-01, H-05, H-06), and what changed
----------------------------------------------------------
1. C-01: this file carried a live DHIS2 admin password in plain text. It now
   reads credentials from the environment.

2. H-06: the paging loop called resp.json().get("events", []) with no status
   check, so a 401 or a 500 produced an empty list, the loop broke on the
   first page, and the participant looked like they had nothing to update.

3. H-05: the participant was then checkpointed complete regardless. Combined
   with the above, a whole run could report success having written nothing.

4. The values are also now written as explicit UTC rather than a bare local
   looking string, since every source timestamp in this dataset is UTC.

USAGE
-----
    nohup python3 "2. wearable script/hour_backfill_step2.py" > hour_backfill.log 2>&1 &
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import aireadi, dhis2  # noqa: E402
from common import metadata_uids as M  # noqa: E402
from common.checkpoint import Checkpoint  # noqa: E402
from common.timeutil import format_display, parse_instant  # noqa: E402

from datetime import timedelta  # noqa: E402

CHECKPOINT_FILE = "hour_backfill_checkpoint.json"
SLEEP_STAGE_NAME = "Wearable - Sleep"


def build_update(registry, event, stage_name, stage_uid, hour_start_de, hour_end_de):
    start = parse_instant(event["occurredAt"])

    if stage_name == SLEEP_STAGE_NAME:
        duration_de = M.SLEEP_DURATION_DE
        raw = next(
            (dv.get("value") for dv in event.get("dataValues", [])
             if dv["dataElement"] == duration_de),
            None,
        )
        try:
            minutes = float(raw) if raw not in (None, "") else 60.0
        except (TypeError, ValueError):
            minutes = 60.0
        end = start + timedelta(minutes=minutes)
    else:
        end = start + timedelta(hours=1)

    changes = {
        hour_start_de: format_display(start),
        hour_end_de: format_display(end),
    }
    return dhis2.event_update_payload(
        event, stage_uid, M.PROGRAM_UID,
        dhis2.merge_data_values(event, changes),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--limit", type=int)
    parser.add_argument("--person-id")
    args, _ = parser.parse_known_args()

    session = dhis2.get_session()
    registry = M.load(session)
    hour_start_de = registry.data_element("Hour Start")
    hour_end_de = registry.data_element("Hour End")

    wam = aireadi.load_manifest_first("wearable_activity_monitor")
    bg = aireadi.load_manifest_first("wearable_blood_glucose")
    all_ids = sorted(set(wam) | set(bg))

    with Checkpoint(CHECKPOINT_FILE, flush_every=5) as checkpoint:
        remaining = [args.person_id] if args.person_id else checkpoint.pending(all_ids)
        if args.limit:
            remaining = remaining[:args.limit]
        print(f"Total {len(all_ids)}, {checkpoint.summary()}, {len(remaining)} to process")
        start_time = time.time()

        for index, person_id in enumerate(remaining, start=1):
            t0 = time.time()
            try:
                context = dhis2.get_tei_context(
                    session, M.PROGRAM_UID, M.PERSON_ID_ATTR_UID, person_id
                )
                if context is None:
                    checkpoint.mark_done(person_id, note="no tracked entity")
                    continue

                updated = 0
                for stage_name, stage_uid in M.WEARABLE_STAGE_UIDS.items():
                    events = dhis2.fetch_events(
                        session, M.PROGRAM_UID, stage_uid, context["trackedEntity"]
                    )
                    payloads = [
                        build_update(registry, event, stage_name, stage_uid,
                                     hour_start_de, hour_end_de)
                        for event in events
                    ]
                    if payloads:
                        dhis2.send_events(session, payloads, "UPDATE")
                        updated += len(payloads)

                checkpoint.mark_done(person_id)
                print(f"[{index}/{len(remaining)}] {person_id}: {updated} events updated, "
                      f"{time.time() - t0:.1f}s "
                      f"(total {(time.time() - start_time) / 3600:.2f}h)")

            except Exception as exc:
                checkpoint.mark_failed(person_id, exc)
                print(f"[{index}/{len(remaining)}] {person_id}: FAILED, {str(exc)[:300]}")

    print(f"\nRun complete. {checkpoint.summary()}")
    return 1 if checkpoint.failed else 0


if __name__ == "__main__":
    sys.exit(main())
