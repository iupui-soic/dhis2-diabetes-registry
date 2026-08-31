"""Locating and reading the AI-READI dataset.

The dataset root comes from AIREADI_ROOT and is never hardcoded, because it
has moved twice already (~/AI-READI, ~/AI-READI-fixed, /data0/AI-READI).

Layout note: the published tree nests each modality one level deeper than the
manifest paths imply, so a manifest entry of

    /wearable_activity_monitor/heart_rate/garmin_vivosmart5/1023/1023_heartrate.json

lives at

    <root>/wearable_activity_monitor/wearable_activity_monitor/heart_rate/...

Some local copies have been flattened. resolve() handles both so a script does
not care which copy it is pointed at.
"""

import csv
import os
import sys

from common import dotenv

MODALITIES = (
    "wearable_activity_monitor",
    "wearable_blood_glucose",
    "environment",
    "cardiac_ecg",
    "retinal_photography",
    "retinal_octa",
    "clinical_data",
)


def root():
    dotenv.load_once()
    path = os.environ.get("AIREADI_ROOT")
    if not path:
        sys.exit(
            "ERROR: AIREADI_ROOT is not set.\n"
            "  On the JupyterHub server:  export AIREADI_ROOT=/data0/AI-READI/full_subset"
        )
    path = os.path.expanduser(path)
    if not os.path.isdir(path):
        sys.exit(f"ERROR: AIREADI_ROOT does not exist: {path}")
    return path


def manifest_path(modality):
    """Path to a modality's manifest.tsv, whichever layout is present."""
    base = root()
    for candidate in (
        os.path.join(base, modality, modality, "manifest.tsv"),
        os.path.join(base, modality, "manifest.tsv"),
    ):
        if os.path.exists(candidate):
            return candidate
    raise FileNotFoundError(f"no manifest.tsv for modality '{modality}' under {base}")


def resolve(modality, relative_path):
    """Turn a manifest filepath into an absolute path, or None if unusable."""
    if not relative_path or str(relative_path).strip() in ("", "None", "nan"):
        return None
    rel = str(relative_path).lstrip("/")
    base = root()
    for candidate in (
        os.path.join(base, modality, rel),
        os.path.join(base, rel),
    ):
        if os.path.exists(candidate):
            return candidate
    return None


def clinical_file(name):
    """Path to a file under clinical_data, whichever layout is present."""
    base = root()
    for candidate in (
        os.path.join(base, "clinical_data", "clinical_data", name),
        os.path.join(base, "clinical_data", name),
    ):
        if os.path.exists(candidate):
            return candidate
    raise FileNotFoundError(f"no clinical_data/{name} under {base}")


def participants_file():
    base = root()
    candidate = os.path.join(base, "participants.tsv")
    if not os.path.exists(candidate):
        raise FileNotFoundError(f"no participants.tsv under {base}")
    return candidate


def load_manifest(modality):
    """Read a manifest as {person_id: [row, ...]}.

    Returns every row per participant, not just the first. Several scripts
    previously took rows[0] and silently ignored a participant's second
    wearable period or re-scan.
    """
    out = {}
    with open(manifest_path(modality)) as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            out.setdefault(row["person_id"], []).append(row)
    return out


def load_manifest_first(modality):
    """Read a manifest as {person_id: row}, keeping only the first row.

    Only for modalities that genuinely have one row per participant, such as
    wearable_blood_glucose and environment. Prefer load_manifest elsewhere.
    """
    return {pid: rows[0] for pid, rows in load_manifest(modality).items()}
