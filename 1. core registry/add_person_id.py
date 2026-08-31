#!/usr/bin/env python3
"""Backfill person_id onto tracked entities that were imported without it.

Only needed for entities created before import_data.py started writing the
person_id attribute directly. New imports do not need this script.

WHAT THE AUDIT FOUND (C-04), and what changed
---------------------------------------------
1. The join key could never match. The CSV side was built with
   str(row["fh_dm2pt"]), giving "1.0"/"0.0", while import_data.py had written
   those same fields to DHIS2 as "true"/"false". Both sides now encode through
   common.registry_fields.clean_value, so they cannot diverge again.

2. The key is not unique. The six demographic columns yield only 1,766
   distinct combinations across 2,280 participants, so 514 rows collide. The
   old code kept the last writer and would have written one participant's
   person_id onto another's record. Colliding keys are now excluded from
   matching entirely and reported, and the script refuses to write unless
   --allow-partial is given.

3. The participant fetch used a hardcoded pageSize of 2,280 with no paging
   and no error check, so a short read looked like a clean run. It now pages.

USAGE
-----
    python3 "1. core registry/add_person_id.py" --csv registry_master_v3.csv --dry-run
    python3 "1. core registry/add_person_id.py" --csv registry_master_v3.csv
"""

import argparse
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import dhis2, registry_fields  # noqa: E402
from common.metadata_uids import PERSON_ID_ATTR_UID, PROGRAM_UID  # noqa: E402

# The demographic columns used to identify a participant, paired with the
# attribute display names that hold the same values in DHIS2. Order matters:
# the two lists are zipped into a comparison key.
CSV_KEY_FIELDS = [
    "year_of_birth", "study_group", "cl_maristat",
    "clinical_site", "fh_dm2pt", "fh_dm2sb",
]
DHIS2_KEY_ATTRIBUTES = [
    "Year of Birth", "Diabetes Severity Group", "Marital Status",
    "Clinical Recruitment Site", "Family History - Parent T2D",
    "Family History - Sibling T2D",
]

BATCH = 100


def build_csv_index(df):
    """Map key -> person_id, keeping only keys that identify exactly one row."""
    by_key = {}
    for _, row in df.iterrows():
        key = registry_fields.attribute_key(row, CSV_KEY_FIELDS)
        by_key.setdefault(key, []).append(int(row["person_id"]))

    unique = {k: v[0] for k, v in by_key.items() if len(v) == 1}
    ambiguous = {k: v for k, v in by_key.items() if len(v) > 1}
    return unique, ambiguous


def fetch_entities(session):
    return dhis2.fetch_all_pages(
        session, "tracker/trackedEntities",
        {"program": PROGRAM_UID, "fields": "trackedEntity,attributes[displayName,value]"},
        ("trackedEntities", "instances"),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--csv", default="registry_master_v3.csv")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change without writing")
    parser.add_argument("--allow-partial", action="store_true",
                        help="write the unambiguous matches even though some "
                             "participants cannot be identified uniquely")
    args, _ = parser.parse_known_args()

    df = pd.read_csv(args.csv)
    unique, ambiguous = build_csv_index(df)
    ambiguous_rows = sum(len(v) for v in ambiguous.values())

    print(f"CSV rows:                     {len(df)}")
    print(f"Uniquely identifiable:        {len(unique)}")
    print(f"Sharing a key with another:   {ambiguous_rows} rows in {len(ambiguous)} groups")

    session = dhis2.get_session()
    entities = fetch_entities(session)
    print(f"Tracked entities fetched:     {len(entities)}")

    updates, unmatched, hit_ambiguous = [], 0, 0
    for entity in entities:
        attrs = {a["displayName"]: a["value"] for a in entity.get("attributes", [])}
        key = registry_fields.dhis2_key(attrs, DHIS2_KEY_ATTRIBUTES)
        if key in unique:
            updates.append({
                "trackedEntity": entity["trackedEntity"],
                "attributes": [
                    {"attribute": PERSON_ID_ATTR_UID, "value": str(unique[key])}
                ],
            })
        elif key in ambiguous:
            hit_ambiguous += 1
        else:
            unmatched += 1

    print()
    print(f"Matched uniquely:             {len(updates)}")
    print(f"Matched an ambiguous key:     {hit_ambiguous}  (skipped, cannot be identified)")
    print(f"No match at all:              {unmatched}")

    if hit_ambiguous or unmatched:
        print()
        print("Some participants cannot be safely identified from these six")
        print("attributes. Writing anyway would risk assigning one participant's")
        print("person_id to another. Re-run with --allow-partial only if you")
        print("accept that the skipped participants keep no person_id.")
        if not args.allow_partial:
            return 1

    if not updates:
        print("\nNothing to write.")
        return 0

    if args.dry_run:
        print(f"\nDry run: would update {len(updates)} tracked entities.")
        return 0

    written = 0
    for i in range(0, len(updates), BATCH):
        batch = updates[i:i + BATCH]
        stats = dhis2.import_tracker(
            session, {"trackedEntities": batch}, "UPDATE",
            expect="updated", expect_count=len(batch),
        )
        written += stats["updated"]
        print(f"  batch {i // BATCH + 1}: updated={stats['updated']}")

    print(f"\nDone. person_id written to {written} tracked entities.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
