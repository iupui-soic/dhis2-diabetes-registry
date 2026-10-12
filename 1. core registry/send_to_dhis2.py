#!/usr/bin/env python3
"""Post import_payload.json to DHIS2 in batches.

WHAT THE AUDIT FOUND (C-01, M-14), and what changed
---------------------------------------------------
1. Credentials were hardcoded (admin/district against localhost). They now
   come from the environment.

2. A non-JSON response, such as a proxy error page, hit `break` and left the
   batch loop. The script then printed a FINAL RESULT summary and, unless some
   earlier batch happened to report ignored > 0, the message "No errors, every
   participant imported successfully" even though it had stopped partway. It
   now records the failure, continues with the remaining batches, and the
   closing message reflects what actually happened.

3. The success check compared the import report's top-level "created" count
   with the batch size. That count sums tracked entities, enrollments AND
   events, so a batch of 100 participants reported 400 to 500 created and
   every successful batch was written to import_errors.json as a failure.
   The check now reads the TRACKED_ENTITY breakdown.

4. The old closing message promised "CREATE will reject anything already
   imported". It would not: the payload carried no UIDs, so a re-run created
   2,280 duplicate participants. The payload now carries UIDs derived from
   person_id and is sent with CREATE_AND_UPDATE, and before sending anything
   this script looks up which person_ids already exist on the server and
   skips them, since participants imported before this change have UIDs
   that cannot be derived.

USAGE
-----
    python3 "1. core registry/send_to_dhis2.py" --payload import_payload.json
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import dhis2  # noqa: E402
from common.metadata_uids import PERSON_ID_ATTR_UID, PROGRAM_UID  # noqa: E402

BATCH_SIZE = 100


def person_id_of(tei):
    return next((a.get("value") for a in tei.get("attributes", [])
                 if a.get("attribute") == PERSON_ID_ATTR_UID), None)


def existing_person_ids(session):
    """person_id -> tracked entity UID for everyone already in the program."""
    items = dhis2.fetch_all_pages(
        session, "tracker/trackedEntities",
        {"program": PROGRAM_UID, "fields": "trackedEntity,attributes[attribute,value]"},
        ("trackedEntities", "instances"),
    )
    out = {}
    for item in items:
        pid = person_id_of(item)
        if pid:
            out.setdefault(pid, []).append(item["trackedEntity"])
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--payload", default="import_payload.json")
    parser.add_argument("--errors", default="import_errors.json")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--update-existing", action="store_true",
                        help="also send participants whose person_id is already on "
                             "the server. Only safe when those records were created "
                             "by this version of import_data.py, so their UIDs match.")
    args, _ = parser.parse_known_args()

    with open(args.payload) as fh:
        teis = json.load(fh)["trackedEntities"]
    print(f"Participants in payload: {len(teis)}")

    missing_uid = [t for t in teis if not t.get("trackedEntity")]
    if missing_uid:
        sys.exit(f"ERROR: {len(missing_uid)} records carry no trackedEntity UID. "
                 f"Rebuild the payload with the current import_data.py.")

    session = dhis2.get_session()

    existing = existing_person_ids(session)
    duplicates = {pid: uids for pid, uids in existing.items() if len(uids) > 1}
    if duplicates:
        print(f"WARNING: {len(duplicates)} person_id(s) already have more than one "
              f"tracked entity on the server, e.g. {list(duplicates.items())[:3]}. "
              f"Merge or delete those before importing anything else.")

    already = [t for t in teis if person_id_of(t) in existing]
    if already and not args.update_existing:
        clashing = [t for t in already
                    if existing[person_id_of(t)] != [t["trackedEntity"]]]
        print(f"Already on the server: {len(already)} participant(s), skipped. "
              f"({len(clashing)} of them have a UID that does not match this payload, "
              f"so --update-existing would create duplicates for them.)")
        teis = [t for t in teis if person_id_of(t) not in existing]
    elif already:
        clashing = [t for t in already
                    if existing[person_id_of(t)] != [t["trackedEntity"]]]
        if clashing:
            sys.exit(f"ERROR: --update-existing given, but {len(clashing)} participant(s) "
                     f"exist on the server under a different UID than this payload "
                     f"derives. Sending would create duplicates. Refusing.")

    print(f"To send: {len(teis)}")
    if not teis:
        print("Nothing to do.")
        return 0

    total_created = 0
    attempted = 0
    failures = []
    total_batches = (len(teis) + args.batch_size - 1) // args.batch_size

    for i in range(0, len(teis), args.batch_size):
        batch = teis[i:i + args.batch_size]
        number = i // args.batch_size + 1
        attempted += len(batch)
        try:
            stats = dhis2.import_tracker(
                session, {"trackedEntities": batch}, "CREATE_AND_UPDATE",
                expect="upserted", expect_count=len(batch),
                tracker_type="TRACKED_ENTITY",
            )
        except dhis2.Dhis2Error as exc:
            # Record and keep going. One bad batch is not a reason to abandon
            # the remaining participants.
            failures.append({
                "batch": number,
                "first_index": i,
                "size": len(batch),
                "error": str(exc),
            })
            print(f"Batch {number}/{total_batches}: FAILED, {str(exc)[:200]}")
            continue

        total_created += stats["created"] + stats["updated"]
        print(f"Batch {number}/{total_batches}: {stats['created']} created, "
              f"{stats['updated']} updated")

    failed_records = sum(f["size"] for f in failures)
    print()
    print(f"Attempted: {attempted} of {len(teis)}")
    print(f"Written:   {total_created}")
    print(f"Failed:    {failed_records} in {len(failures)} batch(es)")

    if failures:
        with open(args.errors, "w") as fh:
            json.dump(failures, fh, indent=2)
        print(f"\nDetails for {len(failures)} failed batch(es) written to {args.errors}.")
        print("Re-run after fixing the cause. Records carry derived UIDs and are sent "
              "with CREATE_AND_UPDATE, so re-sending a committed batch updates it in "
              "place rather than duplicating it.")
        return 1

    if total_created == len(teis):
        print("\nEvery participant imported successfully.")
        return 0

    print(f"\nNo batch errored, but {len(teis) - total_created} participants were "
          f"not written. Check the payload.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
