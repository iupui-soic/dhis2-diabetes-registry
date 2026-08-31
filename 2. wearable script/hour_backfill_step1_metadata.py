#!/usr/bin/env python3
"""Add "Hour Start" and "Hour End" to all eight wearable and CGM stages.

Run once. Idempotent, so re-running is safe.

WHAT THE AUDIT FOUND (C-01, H-07, M-04), and what changed
----------------------------------------------------------
1. C-01: this file carried a live DHIS2 admin password in plain text. It now
   reads credentials from the environment and there is nothing to leak.

2. H-07: the metadata create response was printed but never checked, so a
   soft failure would have produced null UIDs.

3. M-04: the stage round trip used GET fields=* followed by a full PUT.
   A bare fields=* can omit associations, and PUT replaces the object, so
   anything missing from the response would have been dropped from the stage.
   common.dhis2.attach_data_elements uses fields=:owner instead.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import dhis2  # noqa: E402
from common import metadata_uids as M  # noqa: E402

HOUR_FIELDS = [
    {"name": "Hour Start", "valueType": "TEXT", "aggregationType": "NONE"},
    {"name": "Hour End", "valueType": "TEXT", "aggregationType": "NONE"},
]


def main():
    session = dhis2.get_session()

    uids = dhis2.create_data_elements(session, HOUR_FIELDS)
    ordered = [uids["Hour Start"], uids["Hour End"]]
    for name, uid in uids.items():
        print(f"{name}: {uid}")

    for stage_name, stage_uid in M.WEARABLE_STAGE_UIDS.items():
        added = dhis2.attach_data_elements(session, stage_uid, ordered)
        print(f"  {stage_name}: attached {added} field(s)")

    print("\nDone. hour_backfill_step2.py resolves these by name.")


if __name__ == "__main__":
    main()
