#!/usr/bin/env python3
"""
count_true_schema_size.py

asking DHIS2's own metadata API
directly, rather than trusting either a stale document figure or our
own hand-built field lists in metadata_uids.py.

SAFETY: A single GET request. 

WHY TWO DIFFERENT TOTALS ARE REPORTED
------------------------------------------
"152" and "220" might not even be measuring the same thing:

  - PER-STAGE FIELD SLOTS: sum of each stage's field count. A shared
    field (e.g., "Hour Start", reused across 9 stages) gets counted
    once per stage it appears in. This is what our own manual count
    (220) does.

  - DISTINCT DATA ELEMENTS: every unique data element that exists in
    DHIS2, counted once no matter how many stages reuse it. This
    could be much smaller, since several fields are deliberately
    shared (Hour Start/End across 9 stages; Mean/Min/Max/SD/Count
    across 7 of 8 environmental stages).

Both numbers are printed, along with the breakdown, so whichever
definition "152" was originally meant to describe, you can check it
against real, current DHIS2 metadata rather than a document that may
be several project-phases out of date.
"""

import os
import requests

BASE_URL = "https://t2d-registry.plhi.us"
PROGRAM_UID = "W3LSFZH3UDq"

DHIS2_USER = os.environ.get("DHIS2_AUDITOR_USER") or os.environ.get("DHIS2_USER")
DHIS2_PASS = os.environ.get("DHIS2_AUDITOR_PASS") or os.environ.get("DHIS2_PASS")
if not DHIS2_USER or not DHIS2_PASS:
    raise SystemExit("Set DHIS2_AUDITOR_USER and DHIS2_AUDITOR_PASS env vars first.")

SESSION = requests.Session()
SESSION.auth = (DHIS2_USER, DHIS2_PASS)


def main():
    r = SESSION.get(
        f"{BASE_URL}/api/programs/{PROGRAM_UID}.json",
        params={
            "fields": "id,name,"
                      "programTrackedEntityAttributes[trackedEntityAttribute[id,name]],"
                      "programStages[id,name,programStageDataElements[dataElement[id,name]]]"
        },
        timeout=60,
    )
    r.raise_for_status()
    program = r.json()

    # --- Tracked Entity Attributes ---
    ptea = program.get("programTrackedEntityAttributes", [])
    attribute_names = [p["trackedEntityAttribute"]["name"] for p in ptea]
    print("=" * 70)
    print(f"TRACKED ENTITY ATTRIBUTES: {len(attribute_names)}")
    for name in attribute_names:
        print(f"  - {name}")

    # --- Program Stages and their Data Elements ---
    stages = program.get("programStages", [])
    print("\n" + "=" * 70)
    print(f"PROGRAM STAGES: {len(stages)}\n")

    total_slots = 0
    all_element_uids = {}  # uid -> name, for distinct counting

    for stage in stages:
        psde = stage.get("programStageDataElements", [])
        stage_field_count = len(psde)
        total_slots += stage_field_count
        print(f"  {stage['name']:<45} {stage_field_count:>3} fields")
        for item in psde:
            de = item["dataElement"]
            all_element_uids[de["id"]] = de["name"]

    distinct_count = len(all_element_uids)

    print("\n" + "=" * 70)
    print("FINAL COUNTS - compare both against the document's '152':")
    print(f"  Per-stage field slots (sum across all {len(stages)} stages): {total_slots}")
    print(f"  Distinct data elements (deduplicated, whole program):       {distinct_count}")
    print("=" * 70)

    # Show which elements are actually shared, for transparency
    print("\nElements appearing in MORE than one stage (the source of any gap")
    print("between the two totals above):")
    element_stage_count = {}
    for stage in stages:
        for item in stage.get("programStageDataElements", []):
            uid = item["dataElement"]["id"]
            element_stage_count[uid] = element_stage_count.get(uid, 0) + 1
    shared = {uid: count for uid, count in element_stage_count.items() if count > 1}
    for uid, count in sorted(shared.items(), key=lambda x: -x[1]):
        print(f"  {all_element_uids[uid]:<40} used in {count} stages")


if __name__ == "__main__":
    main()
