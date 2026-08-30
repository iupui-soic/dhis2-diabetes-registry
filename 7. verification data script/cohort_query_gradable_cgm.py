#!/usr/bin/env python3
"""
cohort_query_gradable_cgm.py  (v3 — fixes a pagination bug)

Fills the abstract's [C] and [t] placeholders.

BUG FOUND AND FIXED IN THIS VERSION
---------------------------------------
The diagnostic dump showed CGM-Glucose returning exactly 50 events for
a participant known to have months of hourly CGM data — that's DHIS2's
default page size, not a true total. The original has_min_cgm_days()
never paginated, so it silently judged "at least 7 distinct days" from
whatever arbitrary slice of events happened to be on page 1 — almost
certainly why every participant came back as failing the CGM-coverage
check, producing a cohort of 0. This version paginates fully for both
checks before deciding.

SAFETY / READ-ONLY GUARANTEE
-----------------------------
GET requests only. No writes. Same as every other script in this set.
"""

import os
import time
import requests

import metadata_uids as M

BASE_URL = "https://t2d-registry.plhi.us"

AUDITOR_USER = os.environ["DHIS2_AUDITOR_USER"]
AUDITOR_PASS = os.environ["DHIS2_AUDITOR_PASS"]

SESSION = requests.Session()
SESSION.auth = (AUDITOR_USER, AUDITOR_PASS)

MIN_CGM_DAYS = 7
PAGE_SIZE = 200  # explicit, rather than trusting the server default


def get_all_participants():
    participants, page = [], 1
    while True:
        r = SESSION.get(
            f"{BASE_URL}/api/tracker/trackedEntities",
            params={"program": M.PROGRAM_UID, "fields": "trackedEntity",
                    "page": page, "pageSize": PAGE_SIZE},
            timeout=30,
        )
        r.raise_for_status()
        batch = r.json().get("trackedEntities", [])
        if not batch:
            break
        participants.extend(t["trackedEntity"] for t in batch)
        page += 1
    return participants


def get_all_events_for_participant(person_uid, stage_uid, fields):
    """Paginates fully — do not trust a single unpaginated call, as
    confirmed necessary by the diagnostic dump (CGM-Glucose silently
    truncated to 50 events without this)."""
    all_events = []
    page = 1
    while True:
        r = SESSION.get(
            f"{BASE_URL}/api/tracker/events",
            params={
                "program": M.PROGRAM_UID,
                "programStage": stage_uid,
                "trackedEntity": person_uid,
                "fields": fields,
                "page": page,
                "pageSize": PAGE_SIZE,
            },
            timeout=30,
        )
        r.raise_for_status()
        batch = r.json().get("events", [])
        if not batch:
            break
        all_events.extend(batch)
        if len(batch) < PAGE_SIZE:
            break  # last page
        page += 1
    return all_events


def has_gradable_photography(person_uid):
    events = get_all_events_for_participant(
        person_uid, M.RETINAL_PHOTOGRAPHY_STAGE_UID, "dataValues"
    )
    for event in events:
        for dv in event.get("dataValues", []):
            if dv.get("dataElement") == M.RETINAL_PHOTOGRAPHY_PREVIEW_DE and dv.get("value"):
                return True
    return False


def has_min_cgm_days(person_uid, min_days=MIN_CGM_DAYS):
    events = get_all_events_for_participant(
        person_uid, M.CGM_GLUCOSE_STAGE_UID, "occurredAt"
    )
    distinct_days = {e["occurredAt"][:10] for e in events if e.get("occurredAt")}
    return len(distinct_days) >= min_days


def main():
    print("Assembling cohort: gradable photography AND >=7 days CGM coverage...")
    print(f"(paginating fully, pageSize={PAGE_SIZE}, per participant per stage)\n")
    start = time.perf_counter()

    participants = get_all_participants()
    print(f"  Total participants in registry: {len(participants)}")

    matching = []
    for i, person_uid in enumerate(participants):
        if has_gradable_photography(person_uid) and has_min_cgm_days(person_uid):
            matching.append(person_uid)
        if (i + 1) % 100 == 0:
            print(f"  ...checked {i + 1}/{len(participants)}  (matches so far: {len(matching)})")
        time.sleep(0.02)

    elapsed = time.perf_counter() - start

    print("\n" + "=" * 60)
    print("FINAL SUMMARY — fill these directly into the abstract:")
    print(f"  Cohort size (C): {len(matching)} participants")
    print(f"  Query time (t):  {elapsed:.1f} s")
    print("=" * 60)


if __name__ == "__main__":
    main()
