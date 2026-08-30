#!/usr/bin/env python3
"""
retinal_step1_metadata.py

Creates DHIS2 metadata for the two new retinal imaging program stages:
  - "Retinal Photography"
  - "Retinal OCTA"

Both are repeatable stages under the existing Diabetes Registry program
(PROGRAM_UID = W3LSFZH3UDq), following the same pattern already used for
Cardiac ECG and Diagnosis History.

WRITES metadata (data elements + program stages) -> requires an ADMIN
account, not the read-only Auditor account.

USAGE
-----
    export DHIS2_USERNAME="admin"
    export DHIS2_PASSWORD="..."
    python3 retinal_step1_metadata.py

Outputs a JSON file (retinal_metadata_uids.json) mapping field names to
their new DHIS2 data element UIDs and each stage's UID. step2 (the actual
event import script) will read this file rather than hardcoding UIDs.

Design decisions baked into this script (confirmed with professor):
  - Photography preview = converted IR/CFP/FAF image (already 2D)
  - OCTA preview        = converted "enface" image, fixed to the
                           "Superficial vascular plexus flow" layer as the
                           default representative layer for every record
  - flow_cube / segmentation stay as metadata + file-path reference only,
    NOT converted to preview images (raw volumetric / derived data, not
    independently viewable)
"""

import os
import sys
import json
import time

import requests
from requests.auth import HTTPBasicAuth

BASE_URL = "https://t2d-registry.plhi.us/api"
PROGRAM_UID = "W3LSFZH3UDq"

OUTPUT_FILE = "retinal_metadata_uids.json"

DEFAULT_ENFACE_LAYER = "Superficial vascular plexus flow"

RETRY_STATUS_CODES = {502, 503, 504}
MAX_RETRIES = 5
RETRY_BACKOFF_SECONDS = 3


# ---------------------------------------------------------------------------
# Field definitions
# ---------------------------------------------------------------------------
# valueType options used here follow DHIS2's standard set:
#   TEXT, LONG_TEXT, INTEGER, IMAGE

RETINAL_PHOTOGRAPHY_FIELDS = [
    ("Manufacturer", "TEXT"),
    ("Model", "TEXT"),
    ("Laterality", "TEXT"),
    ("Anatomic Region", "TEXT"),
    ("Imaging Type (IR/CFP/FAF)", "TEXT"),
    ("Image Height", "INTEGER"),
    ("Image Width", "INTEGER"),
    ("Color Channel Dimension", "INTEGER"),
    ("SOP Instance UID", "TEXT"),
    ("Original DICOM File Path", "LONG_TEXT"),
    ("Preview Image", "IMAGE"),
]

RETINAL_OCTA_FIELDS = [
    ("Manufacturer", "TEXT"),
    ("Model", "TEXT"),
    ("Laterality", "TEXT"),
    ("Anatomic Region", "TEXT"),
    ("Imaging Type (OCTA)", "TEXT"),
    ("Flow Cube Height", "INTEGER"),
    ("Flow Cube Width", "INTEGER"),
    ("Flow Cube Number of Frames", "INTEGER"),
    ("Flow Cube SOP Instance UID", "TEXT"),
    ("Flow Cube DICOM File Path", "LONG_TEXT"),
    ("Segmentation DICOM File Path", "LONG_TEXT"),
    ("Segmentation SOP Instance UID", "TEXT"),
    ("Segmentation Type", "TEXT"),
    ("En-face Type / Layer", "TEXT"),
    ("En-face SOP Instance UID", "TEXT"),
    ("Preview Image", "IMAGE"),
]

# Stage-name prefixes keep the two "Preview Image" / "SOP Instance UID" etc.
# data elements from colliding, since DHIS2 data element short names must
# be unique registry-wide.
STAGE_DEFS = [
    {
        "stage_name": "Retinal Photography",
        "short_prefix": "RetPhoto",
        "fields": RETINAL_PHOTOGRAPHY_FIELDS,
    },
    {
        "stage_name": "Retinal OCTA",
        "short_prefix": "RetOCTA",
        "fields": RETINAL_OCTA_FIELDS,
    },
]


def get_session():
    username = os.environ.get("DHIS2_USERNAME")
    password = os.environ.get("DHIS2_PASSWORD")
    if not username or not password:
        sys.exit(
            "ERROR: Set DHIS2_USERNAME and DHIS2_PASSWORD environment variables "
            "(an ADMIN account) before running this script. Do not hardcode credentials."
        )
    session = requests.Session()
    session.auth = HTTPBasicAuth(username, password)
    session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
    return session


def request_with_retry(session, method, url, **kwargs):
    for attempt in range(1, MAX_RETRIES + 1):
        resp = session.request(method, url, timeout=60, **kwargs)
        if resp.status_code < 400:
            return resp
        if resp.status_code in RETRY_STATUS_CODES and attempt < MAX_RETRIES:
            wait = RETRY_BACKOFF_SECONDS * attempt
            print(f"  [warn] {resp.status_code} from server, retrying in {wait}s "
                  f"(attempt {attempt}/{MAX_RETRIES})...")
            time.sleep(wait)
            continue
        print(f"  [error] {method} {url} -> {resp.status_code}")
        print(f"  Response: {resp.text[:1000]}")
        resp.raise_for_status()
    raise RuntimeError(f"Failed after {MAX_RETRIES} retries: {url}")


def short_name_for(prefix, field_name, max_len=50):
    """
    DHIS2 dataElement shortName has a max length (50 chars) and must be
    unique. Build a compact, deterministic short name from the stage
    prefix + field name.
    """
    raw = f"{prefix} {field_name}"
    # collapse whitespace, strip characters that tend to cause issues
    raw = " ".join(raw.split())
    return raw[:max_len]


def create_data_element(session, stage_name, field_name, short_name, value_type):
    full_name = f"{stage_name} - {field_name}"

    # Idempotency: check if this exact data element already exists before creating.
    existing = find_existing_data_element(session, full_name)
    if existing:
        print(f"  [skip] dataElement '{full_name}' already exists -> {existing}")
        return existing

    payload = {
        "name": full_name,
        "shortName": short_name,
        "domainType": "TRACKER",
        "valueType": value_type,
        "aggregationType": "NONE",
    }
    resp = request_with_retry(session, "POST", f"{BASE_URL}/dataElements", json=payload)
    uid = resp.json()["response"]["uid"]
    print(f"  [ok] dataElement '{full_name}' ({value_type}) -> {uid}")
    return uid


def find_existing_data_element(session, full_name):
    resp = request_with_retry(
        session, "GET", f"{BASE_URL}/dataElements",
        params={"filter": f"name:eq:{full_name}", "fields": "id"},
    )
    results = resp.json().get("dataElements", [])
    return results[0]["id"] if results else None


def find_existing_program_stage(session, stage_name):
    resp = request_with_retry(
        session, "GET", f"{BASE_URL}/programStages",
        params={"filter": f"name:eq:{stage_name}", "fields": "id"},
    )
    results = resp.json().get("programStages", [])
    return results[0]["id"] if results else None


def create_program_stage(session, stage_name, data_element_uids):
    """
    Create a repeatable program stage attached to PROGRAM_UID, with all
    given data elements attached via programStageDataElements.
    Idempotent: if a stage with this name already exists, reuse it.
    """
    existing = find_existing_program_stage(session, stage_name)
    if existing:
        print(f"[skip] programStage '{stage_name}' already exists -> {existing}")
        return existing

    program_stage_data_elements = [
        {"dataElement": {"id": uid}, "compulsory": False, "sortOrder": i}
        for i, uid in enumerate(data_element_uids)
    ]

    payload = {
        "name": stage_name,
        "program": {"id": PROGRAM_UID},
        "repeatable": True,
        "programStageDataElements": program_stage_data_elements,
        "featureType": "NONE",
        "autoGenerateEvent": False,
        "openAfterEnrollment": False,
    }
    resp = request_with_retry(session, "POST", f"{BASE_URL}/programStages", json=payload)
    uid = resp.json()["response"]["uid"]
    print(f"[ok] programStage '{stage_name}' -> {uid}")
    return uid


def build_stage(session, stage_def):
    print(f"\n=== Creating data elements for stage: {stage_def['stage_name']} ===")
    field_uid_map = {}
    ordered_uids = []
    for field_name, value_type in stage_def["fields"]:
        short_name = short_name_for(stage_def["short_prefix"], field_name)
        uid = create_data_element(session, stage_def["stage_name"], field_name, short_name, value_type)
        field_uid_map[field_name] = uid
        ordered_uids.append(uid)

    print(f"\n=== Creating program stage: {stage_def['stage_name']} ===")
    stage_uid = create_program_stage(session, stage_def["stage_name"], ordered_uids)

    return {
        "stage_uid": stage_uid,
        "fields": field_uid_map,
    }


def main():
    session = get_session()

    print(f"Target program: {PROGRAM_UID} (Diabetes Registry)")
    print(f"Default OCTA preview enface layer: '{DEFAULT_ENFACE_LAYER}'")

    # Resume support: load any previously-saved progress rather than starting blank.
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE) as f:
            results = json.load(f)
        print(f"[info] Loaded existing progress from {OUTPUT_FILE}")
    else:
        results = {
            "program_uid": PROGRAM_UID,
            "default_enface_layer": DEFAULT_ENFACE_LAYER,
            "stages": {},
        }

    for stage_def in STAGE_DEFS:
        stage_result = build_stage(session, stage_def)
        results["stages"][stage_def["stage_name"]] = stage_result

        # Save immediately after each stage completes, so a failure on a later
        # stage doesn't lose progress already made on an earlier one.
        with open(OUTPUT_FILE, "w") as f:
            json.dump(results, f, indent=2)
        print(f"[info] Progress saved to {OUTPUT_FILE}")

    print(f"\nAll metadata created. UID mapping written to: {OUTPUT_FILE}")
    print("\nNEXT STEPS:")
    print("  1. In the Metadata Management app, verify the two new stages under")
    print("     Diabetes Registry look correct (field order, value types).")
    print("  2. Set sharing on both new program stages (Data: View only, Metadata:")
    print("     View only for the Auditor role) -- same as the existing 19 stages,")
    print("     since new stages do NOT inherit sharing automatically.")
    print("  3. Test the full pipeline (convert -> upload -> verify) on ONE")
    print("     participant before any full-scale import, per project convention.")


if __name__ == "__main__":
    main()
