#!/usr/bin/env python3
"""Pilot import of retinal imaging for one participant.

This file used to be a near-copy of retinal_step2_import_full.py. The two
diverged, which is how the YBR colour fix ended up in only one of them
(M-06). It is now a thin wrapper so there is a single implementation.

USAGE
-----
    python3 "6. retinal imaging and OCTA/retinal_step2_import.py" --person-id 1072
"""

import os
import runpy
import sys

if __name__ == "__main__":
    if not any(a.startswith("--person-id") for a in sys.argv[1:]):
        sys.argv += ["--person-id", "1072"]
    target = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "retinal_step2_import_full.py")
    sys.argv[0] = target
    runpy.run_path(target, run_name="__main__")
