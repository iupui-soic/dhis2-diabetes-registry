#!/usr/bin/env python3
"""Create the glucose sentinel timestamp fields.

HISTORICAL. glucose_recount_step1_metadata.py renames the two fields this
script creates and adds the matching count fields, so on a fresh instance run
this first, then the recount step 1.

Captures the exact times the sensor reported "Low" or "High", meaning readings
outside its measurable range, to pair with the existing count fields.

WHAT THE AUDIT FOUND (C-01, H-07, M-04), and what changed
----------------------------------------------------------
Credentials come from the environment. Creation verifies its result rather
than trusting the status code, and attaching to the stage no longer round
trips the whole object through a bare fields=* PUT.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import dhis2  # noqa: E402
from common import metadata_uids as M  # noqa: E402

FIELDS = [
    {"name": "Glucose Low Reading Timestamps", "shortName": "Glucose Low Timestamps",
     "valueType": "LONG_TEXT", "aggregationType": "NONE"},
    {"name": "Glucose High Reading Timestamps", "shortName": "Glucose High Timestamps",
     "valueType": "LONG_TEXT", "aggregationType": "NONE"},
]


def main():
    session = dhis2.get_session()
    uids = dhis2.create_data_elements(session, FIELDS)
    for name, uid in uids.items():
        print(f"{name}: {uid}")
    added = dhis2.attach_data_elements(
        session, M.CGM_GLUCOSE_STAGE_UID, list(uids.values())
    )
    print(f"Attached {added} field(s) to the CGM glucose stage.")


if __name__ == "__main__":
    main()
