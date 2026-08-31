#!/usr/bin/env python3
"""Rename the glucose sentinel fields and add the clinical range counts.

Renames, so the device-only concept is distinguishable from the clinical one:
  High Reading Count               -> Device High Count
  Low Reading Count                -> Device Low Count
  Glucose High Reading Timestamps  -> Above Range Timestamps (>180 mg/dL)
  Glucose Low Reading Timestamps   -> Below Range Timestamps (<70 mg/dL)

Creates:
  Above Range Count (>180 mg/dL)
  Below Range Count (<70 mg/dL)

Renaming a data element changes only its label. No stored value is touched.

WHAT THE AUDIT FOUND (C-01, H-07, M-04), and what changed
----------------------------------------------------------
Credentials come from the environment. The rename used GET fields=* followed
by a full PUT, which risks dropping any property the response omitted; it now
sends a targeted PATCH. Creation verifies its own result rather than trusting
the status code.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import dhis2  # noqa: E402
from common import metadata_uids as M  # noqa: E402

RENAMES = [
    ("RZpt03lifR6", "Device High Count", "Device High Count"),
    ("hi1TAwCqN0f", "Device Low Count", "Device Low Count"),
    ("Zu4iFxtthSU", "Above Range Timestamps (>180 mg/dL)", "Above Range Timestamps"),
    ("LfzwHxQUotL", "Below Range Timestamps (<70 mg/dL)", "Below Range Timestamps"),
]

NEW_FIELDS = [
    {"name": "Above Range Count (>180 mg/dL)", "shortName": "Above Range Count",
     "valueType": "INTEGER", "aggregationType": "SUM"},
    {"name": "Below Range Count (<70 mg/dL)", "shortName": "Below Range Count",
     "valueType": "INTEGER", "aggregationType": "SUM"},
]


def rename(session, uid, name, short_name):
    """PATCH just the two label fields, leaving everything else alone."""
    dhis2.request(
        session, "PATCH", f"{dhis2.api_url()}/dataElements/{uid}",
        headers={"Content-Type": "application/json-patch+json"},
        data=json.dumps([
            {"op": "replace", "path": "/name", "value": name},
            {"op": "replace", "path": "/shortName", "value": short_name[:50]},
        ]),
    )
    print(f"  {uid} -> {name}")


def main():
    session = dhis2.get_session()

    print("Renaming existing fields")
    for uid, name, short_name in RENAMES:
        rename(session, uid, name, short_name)

    print("\nCreating the clinical range count fields")
    uids = dhis2.create_data_elements(session, NEW_FIELDS)
    for name, uid in uids.items():
        print(f"  {name}: {uid}")

    added = dhis2.attach_data_elements(
        session, M.CGM_GLUCOSE_STAGE_UID, list(uids.values())
    )
    print(f"\nAttached {added} field(s) to the CGM glucose stage.")
    print("glucose_recount_step2_backfill.py resolves these by name.")


if __name__ == "__main__":
    main()
