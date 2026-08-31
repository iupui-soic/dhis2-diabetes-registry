#!/usr/bin/env python3
"""Print raw tracker JSON for one participant, to inspect real field names.

Diagnostic only. READ-ONLY, GET requests only.

WHAT THE AUDIT FOUND (C-01, H-13), and what changed
----------------------------------------------------
Credentials come from the environment, and metadata_uids is now committed in
common/ so this script can actually run.

USAGE
-----
    python3 "7. verification data script/diagnose_cohort_query.py"
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import dhis2  # noqa: E402
from common import metadata_uids as M  # noqa: E402


def main():
    session = dhis2.get_session(read_only=True)
    registry = M.load(session)

    payload = dhis2.get_json(session, "tracker/trackedEntities", {
        "program": M.PROGRAM_UID, "fields": "trackedEntity", "pageSize": 1,
    })
    print("Raw trackedEntities response:")
    print(json.dumps(payload, indent=2)[:1000])

    items = dhis2.extract_items(payload, "trackedEntities", "instances")
    if not items:
        print("\nNo participants returned.")
        return 1
    tei_uid = items[0]["trackedEntity"]
    print(f"\nUsing participant: {tei_uid}\n")

    for label, stage_name in (("Retinal Photography", "Retinal Photography"),
                              ("CGM - Glucose", "CGM - Glucose")):
        events = dhis2.fetch_events(
            session, M.PROGRAM_UID, registry.stage(stage_name), tei_uid
        )
        print(f"--- {label}: {len(events)} events ---")
        if events:
            print(json.dumps(events[0], indent=2))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
