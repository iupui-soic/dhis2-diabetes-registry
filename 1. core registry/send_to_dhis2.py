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

BATCH_SIZE = 100


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--payload", default="import_payload.json")
    parser.add_argument("--errors", default="import_errors.json")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    args, _ = parser.parse_known_args()

    with open(args.payload) as fh:
        teis = json.load(fh)["trackedEntities"]
    print(f"Total participants to import: {len(teis)}")

    session = dhis2.get_session()

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
                session, {"trackedEntities": batch}, "CREATE",
                expect="created", expect_count=len(batch),
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

        total_created += stats["created"]
        print(f"Batch {number}/{total_batches}: {stats['created']} created")

    failed_records = sum(f["size"] for f in failures)
    print()
    print(f"Attempted: {attempted} of {len(teis)}")
    print(f"Created:   {total_created}")
    print(f"Failed:    {failed_records} in {len(failures)} batch(es)")

    if failures:
        with open(args.errors, "w") as fh:
            json.dump(failures, fh, indent=2)
        print(f"\nDetails for {len(failures)} failed batch(es) written to {args.errors}.")
        print("Re-run after fixing the cause. CREATE will reject anything already imported.")
        return 1

    if total_created == len(teis):
        print("\nEvery participant imported successfully.")
        return 0

    print(f"\nNo batch errored, but {len(teis) - total_created} participants were "
          f"not created. Check the payload.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
