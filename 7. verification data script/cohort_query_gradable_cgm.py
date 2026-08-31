#!/usr/bin/env python3
"""Count participants with gradable retinal photography AND >=7 days of CGM.

Fills the abstract's [C] cohort size and [t] query time placeholders.

READ-ONLY. GET requests only, using the auditor account.

WHAT THE AUDIT FOUND (C-01, H-06, H-13), and what changed
----------------------------------------------------------
Credentials come from the environment rather than being read from two
undocumented variables. metadata_uids, which this script imported and which
was never committed, now lives in common/. The paging loops raise on an HTTP
error instead of returning an empty list that reads as "this participant has
no events", which would have quietly shrunk the cohort.

The original pagination bug this file's own header describes, judging seven
distinct days from whatever happened to be on page one, is fixed by
dhis2.fetch_all_pages.

USAGE
-----
    python3 "7. verification data script/cohort_query_gradable_cgm.py"
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import dhis2  # noqa: E402
from common import metadata_uids as M  # noqa: E402

MIN_CGM_DAYS = 7


def has_gradable_photography(session, registry, tei_uid):
    preview_de = M.RETINAL_PHOTOGRAPHY_FIELD_UIDS["preview"]
    events = dhis2.fetch_events(
        session, M.PROGRAM_UID, registry.stage("Retinal Photography"), tei_uid,
        fields="event,dataValues[dataElement,value]",
    )
    for event in events:
        for dv in event.get("dataValues", []):
            if dv.get("dataElement") == preview_de and dv.get("value"):
                return True
    return False


def cgm_day_count(session, registry, tei_uid):
    events = dhis2.fetch_events(
        session, M.PROGRAM_UID, registry.stage("CGM - Glucose"), tei_uid,
        fields="event,occurredAt",
    )
    return len({e["occurredAt"][:10] for e in events if e.get("occurredAt")})


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--min-cgm-days", type=int, default=MIN_CGM_DAYS)
    args, _ = parser.parse_known_args()

    session = dhis2.get_session(read_only=True)
    registry = M.load(session)

    print(f"Assembling cohort: gradable photography AND >={args.min_cgm_days} "
          f"days of CGM coverage")
    start = time.perf_counter()

    participants = dhis2.fetch_all_pages(
        session, "tracker/trackedEntities",
        {"program": M.PROGRAM_UID, "fields": "trackedEntity"},
        ("trackedEntities", "instances"),
    )
    print(f"  participants in the registry: {len(participants)}")

    matching = 0
    for index, item in enumerate(participants, start=1):
        tei_uid = item["trackedEntity"]
        if (has_gradable_photography(session, registry, tei_uid)
                and cgm_day_count(session, registry, tei_uid) >= args.min_cgm_days):
            matching += 1
        if index % 100 == 0:
            print(f"  checked {index}/{len(participants)}, matches {matching}")

    elapsed = time.perf_counter() - start
    print("\n" + "=" * 60)
    print("Fill these into the abstract:")
    print(f"  Cohort size (C): {matching} participants")
    print(f"  Query time (t):  {elapsed:.1f} s")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
