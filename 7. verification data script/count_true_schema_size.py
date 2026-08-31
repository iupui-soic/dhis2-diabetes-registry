#!/usr/bin/env python3
"""Count the program's real schema size, from DHIS2 rather than a document.

Reports two different totals, because "152" and "220" may not be measuring
the same thing:

  PER-STAGE FIELD SLOTS   each stage's field count, summed. A shared field
                          such as "Hour Start", reused across nine stages,
                          counts once per stage.
  DISTINCT DATA ELEMENTS  every unique element, counted once no matter how
                          many stages reuse it.

READ-ONLY. A single GET.

WHAT THE AUDIT FOUND (C-01), and what changed
----------------------------------------------
Credentials come from the shared helper, which fails with one clear message
instead of two different half-configured paths.

USAGE
-----
    python3 "7. verification data script/count_true_schema_size.py"
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import dhis2  # noqa: E402
from common import metadata_uids as M  # noqa: E402


def main():
    session = dhis2.get_session(read_only=True)
    program = dhis2.get_json(session, f"programs/{M.PROGRAM_UID}", {
        "fields": "id,name,"
                  "programTrackedEntityAttributes[trackedEntityAttribute[id,name]],"
                  "programStages[id,name,programStageDataElements[dataElement[id,name]]]",
    })

    attributes = [
        p["trackedEntityAttribute"]["name"]
        for p in program.get("programTrackedEntityAttributes", [])
    ]
    print("=" * 70)
    print(f"TRACKED ENTITY ATTRIBUTES: {len(attributes)}")
    for name in attributes:
        print(f"  {name}")

    stages = program.get("programStages", [])
    print("\n" + "=" * 70)
    print(f"PROGRAM STAGES: {len(stages)}\n")

    slots = 0
    elements = {}
    usage = {}
    for stage in stages:
        psde = stage.get("programStageDataElements", [])
        slots += len(psde)
        print(f"  {stage['name']:<45} {len(psde):>3} fields")
        for item in psde:
            de = item["dataElement"]
            elements[de["id"]] = de["name"]
            usage[de["id"]] = usage.get(de["id"], 0) + 1

    print("\n" + "=" * 70)
    print("TOTALS")
    print(f"  Per-stage field slots across {len(stages)} stages: {slots}")
    print(f"  Distinct data elements:                     {len(elements)}")
    print("=" * 70)

    shared = {uid: n for uid, n in usage.items() if n > 1}
    if shared:
        print("\nElements used in more than one stage, which is the gap between "
              "the two totals:")
        for uid, count in sorted(shared.items(), key=lambda kv: -kv[1]):
            print(f"  {elements[uid]:<45} {count} stages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
