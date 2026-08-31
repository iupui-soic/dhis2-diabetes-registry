#!/usr/bin/env python3
"""Repair ECG Validation Date values stored as 20241014 instead of 2024-10-14.

Converts every affected event in place and touches no other field.

Only needed for events imported before ecg_step2_import.py started
normalising the date. New imports do not need this script.

WHAT THE AUDIT FOUND (C-01, H-05, H-06), and what changed
----------------------------------------------------------
The paging loop called resp.json().get("events", []) with no status check, so
any HTTP error produced an empty list and the script printed "Total events
fetched: 0", "Events needing fix: 0" and "DONE" having done nothing. It now
raises. Credentials come from the environment, and updates carry the event's
full data value set because a tracker UPDATE replaces them.

USAGE
-----
    python3 "3. ecg/ecg_validation_date_fix.py" --dry-run
    python3 "3. ecg/ecg_validation_date_fix.py"
"""

import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import dhis2  # noqa: E402
from common import metadata_uids as M  # noqa: E402

from ecg_step1_metadata import STAGE_NAME  # noqa: E402


def reformat(raw):
    """20241014 -> 2024-10-14. Returns None when it is not that shape."""
    if raw and len(raw) == 8 and raw.isdigit():
        try:
            return datetime.strptime(raw, "%Y%m%d").strftime("%Y-%m-%d")
        except ValueError:
            return None
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true")
    args, _ = parser.parse_known_args()

    session = dhis2.get_session()
    registry = M.load(session)
    stage_uid = registry.stage(STAGE_NAME)
    date_de = registry.data_element("ECG Validation Date")

    print("Fetching ECG events...")
    events = dhis2.fetch_events(session, M.PROGRAM_UID, stage_uid)
    print(f"Total events fetched: {len(events)}")

    updates = []
    already_ok = 0
    for event in events:
        current = next(
            (dv.get("value") for dv in event.get("dataValues", [])
             if dv["dataElement"] == date_de),
            None,
        )
        fixed = reformat(current)
        if fixed is None or fixed == current:
            already_ok += 1
            continue
        updates.append(dhis2.event_update_payload(
            event, stage_uid, M.PROGRAM_UID,
            dhis2.merge_data_values(event, {date_de: fixed}),
        ))

    print(f"Events needing a fix: {len(updates)}")
    print(f"Already correct, or missing the field: {already_ok}")

    if not updates:
        return 0
    if args.dry_run:
        print("Dry run, nothing written.")
        return 0

    stats = dhis2.send_events(session, updates, "UPDATE", batch_size=50)
    print(f"Updated {stats['updated']} event(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
