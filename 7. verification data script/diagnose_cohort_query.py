#!/usr/bin/env python3
"""
diagnose_cohort_query.py

Diagnostic only — read-only, GET requests only, same safety guarantees
as every other script. Prints the RAW event JSON for one participant's
Retinal Photography events and CGM-Glucose events, so we can see the
actual field names this DHIS2 build uses, instead of assuming they
match standard documentation (the same class of issue found in
Phase 20 — the "trackedEntities" vs "instances" key mismatch).

USAGE
-------
Run this, then paste the FULL printed output back — don't summarize
it, the exact key names matter.
"""

import os
import json
import requests

import metadata_uids as M

BASE_URL = "https://t2d-registry.plhi.us"

AUDITOR_USER = os.environ["DHIS2_AUDITOR_USER"]
AUDITOR_PASS = os.environ["DHIS2_AUDITOR_PASS"]

SESSION = requests.Session()
SESSION.auth = (AUDITOR_USER, AUDITOR_PASS)


def get_one_participant():
    r = SESSION.get(
        f"{BASE_URL}/api/tracker/trackedEntities",
        params={"program": M.PROGRAM_UID, "fields": "trackedEntity", "pageSize": 1},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    print("Raw trackedEntities response (first participant lookup):")
    print(json.dumps(data, indent=2)[:1000])
    print()
    return data["trackedEntities"][0]["trackedEntity"]


def dump_events(person_uid, stage_uid, label):
    r = SESSION.get(
        f"{BASE_URL}/api/tracker/events",
        params={
            "program": M.PROGRAM_UID,
            "programStage": stage_uid,
            "trackedEntity": person_uid,
        },
        timeout=30,
    )
    print(f"--- {label} events for {person_uid} ---")
    print(f"HTTP status: {r.status_code}")
    r.raise_for_status()
    data = r.json()
    events = data.get("events", [])
    print(f"Number of events returned: {len(events)}")
    if events:
        print("Full JSON of first event:")
        print(json.dumps(events[0], indent=2))
    else:
        print("No events returned for this participant/stage at all.")
    print()
    return events


def main():
    person_uid = get_one_participant()
    print(f"Using participant: {person_uid}\n")

    dump_events(person_uid, M.RETINAL_PHOTOGRAPHY_STAGE_UID, "Retinal Photography")
    dump_events(person_uid, M.CGM_GLUCOSE_STAGE_UID, "CGM-Glucose")


if __name__ == "__main__":
    main()
