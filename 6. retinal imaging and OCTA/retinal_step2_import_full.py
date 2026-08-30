#!/usr/bin/env python3
"""
retinal_step2_import.py

PILOT import for ONE participant (default: person_id 1072) into the two
new retinal imaging stages (Retinal Photography, Retinal OCTA) created by
retinal_step1_metadata.py.

For each manifest row for this participant:
  1. Convert the relevant DICOM file(s) to a resized preview JPEG
     (same normalization logic verified in the pilot QA step)
  2. Upload the JPEG as a DHIS2 file resource
  3. Build and POST a tracker event with all metadata fields + the
     preview image reference

Writes ADMIN-level data (creates events) -> requires an ADMIN account.

USAGE
-----
    export DHIS2_USERNAME="admin"
    export DHIS2_PASSWORD="..."
    python3 retinal_step2_import.py --person-id 1072

This is a PILOT for ONE participant only, per project convention: verify
in DHIS2 Capture before running any full-scale import. There is no
full-scale import in this script by design.
"""

import os
import sys
import json
import time
import argparse
from pathlib import Path
from io import BytesIO

import numpy as np
import pandas as pd
import requests
from requests.auth import HTTPBasicAuth
from PIL import Image

try:
    import pydicom
except ImportError:
    sys.exit("pydicom is required. Install with:\n    pip install pydicom --user\n")

BASE_URL = "https://t2d-registry.plhi.us/api"
PROGRAM_UID = "W3LSFZH3UDq"
PERSON_ID_ATTR_UID = "oFbmOHnKYaX"

AI_READI_ROOT = os.path.expanduser("~/AI-READI-fixed")
PHOTOGRAPHY_DIR = os.path.join(AI_READI_ROOT, "retinal_photography")
OCTA_DIR = os.path.join(AI_READI_ROOT, "retinal_octa")

METADATA_UID_FILE = "retinal_metadata_uids.json"
CHECKPOINT_FILE = "retinal_import_checkpoint.json"

MAX_DIMENSION = 800
JPEG_QUALITY = 85

RETRY_STATUS_CODES = {502, 503, 504}
MAX_RETRIES = 5
RETRY_BACKOFF_SECONDS = 3


# ---------------------------------------------------------------------------
# Session / retry helpers
# ---------------------------------------------------------------------------

def get_session():
    username = os.environ.get("DHIS2_USERNAME")
    password = os.environ.get("DHIS2_PASSWORD")
    if not username or not password:
        sys.exit(
            "ERROR: Set DHIS2_USERNAME and DHIS2_PASSWORD environment variables "
            "(an ADMIN account) before running this script."
        )
    session = requests.Session()
    session.auth = HTTPBasicAuth(username, password)
    return session


def request_with_retry(session, method, url, **kwargs):
    for attempt in range(1, MAX_RETRIES + 1):
        resp = session.request(method, url, timeout=120, **kwargs)
        if resp.status_code < 400:
            return resp
        if resp.status_code in RETRY_STATUS_CODES and attempt < MAX_RETRIES:
            wait = RETRY_BACKOFF_SECONDS * attempt
            print(f"    [warn] {resp.status_code}, retrying in {wait}s (attempt {attempt}/{MAX_RETRIES})...")
            time.sleep(wait)
            continue
        print(f"    [error] {method} {url} -> {resp.status_code}")
        print(f"    Response: {resp.text[:1500]}")
        resp.raise_for_status()
    raise RuntimeError(f"Failed after {MAX_RETRIES} retries: {url}")


# ---------------------------------------------------------------------------
# DICOM -> JPEG conversion (same logic verified in the pilot QA step)
# ---------------------------------------------------------------------------

def dicom_to_preview_array(ds):
    pixel_array = ds.pixel_array
    if pixel_array.ndim >= 3 and int(getattr(ds, "NumberOfFrames", 1)) > 1:
        num_frames = pixel_array.shape[0]
        frame = pixel_array[num_frames // 2]
    else:
        frame = pixel_array

    frame = frame.astype(np.float64)
    slope = float(getattr(ds, "RescaleSlope", 1))
    intercept = float(getattr(ds, "RescaleIntercept", 0))
    frame = frame * slope + intercept

    fmin, fmax = frame.min(), frame.max()
    frame = (frame - fmin) / (fmax - fmin) * 255.0 if fmax > fmin else np.zeros_like(frame)
    return frame.astype(np.uint8)


def dicom_path_to_jpeg_bytes(rel_path):
    """Read a DICOM file (path relative to AI_READI_ROOT) and return JPEG bytes, or None on failure."""
    if pd.isna(rel_path) or str(rel_path).strip().lower() == "not reported":
        return None
    full_path = os.path.join(AI_READI_ROOT, str(rel_path).lstrip("/"))
    if not os.path.exists(full_path):
        print(f"    [warn] file not found: {full_path}")
        return None
    try:
        ds = pydicom.dcmread(full_path)
        arr = dicom_to_preview_array(ds)
        if arr.ndim == 2:
            img = Image.fromarray(arr).convert("L")
        elif arr.ndim == 3 and arr.shape[-1] == 3:
            img = Image.fromarray(arr).convert("RGB")
        else:
            img = Image.fromarray(np.squeeze(arr)).convert("L")
        img.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, "JPEG", quality=JPEG_QUALITY)
        return buf.getvalue()
    except Exception as e:
        print(f"    [error] conversion failed for {full_path}: {e}")
        return None


# ---------------------------------------------------------------------------
# DHIS2 helpers
# ---------------------------------------------------------------------------

def upload_file_resource(session, jpeg_bytes, filename):
    files = {"file": (filename, jpeg_bytes, "image/jpeg")}
    resp = request_with_retry(
        session, "POST", f"{BASE_URL}/fileResources",
        params={"domain": "DATA_VALUE"}, files=files,
    )
    data = resp.json()
    # DHIS2 response shape can vary slightly by version; check common paths.
    fr = data.get("response", {}).get("fileResource") or data.get("fileResource") or data
    fr_id = fr.get("id")
    if not fr_id:
        raise RuntimeError(f"Could not find fileResource id in response: {data}")
    return fr_id


AI_READI_STUDY_ORG_UNIT_NAME = "AI-READI Study"


def extract_items(response_json, *candidate_keys):
    """DHIS2 tracker responses key results by resource name on this version (e.g.
    'trackedEntities'), not a generic 'instances' key -- confirmed via diagnostic."""
    for key in candidate_keys:
        if key in response_json:
            return response_json[key]
    return []


def resolve_org_unit_uid(session):
    resp = request_with_retry(
        session, "GET", f"{BASE_URL}/organisationUnits",
        params={"filter": f"name:eq:{AI_READI_STUDY_ORG_UNIT_NAME}", "fields": "id,name"},
    )
    results = resp.json().get("organisationUnits", [])
    if not results:
        sys.exit(f"ERROR: Could not find org unit named '{AI_READI_STUDY_ORG_UNIT_NAME}'.")
    return results[0]["id"]


def fetch_full_roster(session, org_unit_uid):
    """Fetch every participant's person_id + trackedEntity + enrollment/orgUnit in one pass."""
    print("Fetching full participant roster (this may take a minute)...")
    roster = []
    page = 1
    page_size = 500
    while True:
        resp = request_with_retry(
            session, "GET", f"{BASE_URL}/tracker/trackedEntities",
            params={
                "program": PROGRAM_UID,
                "orgUnit": org_unit_uid,
                "ouMode": "DESCENDANTS",
                "fields": "trackedEntity,attributes[attribute,value],enrollments[enrollment,orgUnit,status]",
                "page": page,
                "pageSize": page_size,
            },
        )
        instances = extract_items(resp.json(), "trackedEntities", "instances")
        if not instances:
            break
        for inst in instances:
            person_id = None
            for attr in inst.get("attributes", []):
                if attr.get("attribute") == PERSON_ID_ATTR_UID:
                    person_id = attr.get("value")
                    break
            enrollments = inst.get("enrollments", [])
            active = next((e for e in enrollments if e.get("status") == "ACTIVE"), None) or \
                     (enrollments[0] if enrollments else None)
            if person_id and active:
                roster.append({
                    "person_id": person_id,
                    "trackedEntity": inst["trackedEntity"],
                    "enrollment": active["enrollment"],
                    "orgUnit": active["orgUnit"],
                })
        print(f"  page {page}: {len(instances)} participants (running total {len(roster)})")
        if len(instances) < page_size:
            break
        page += 1
    print(f"Roster complete: {len(roster)} participants with valid enrollment\n")
    return roster


def get_enrollment_context(session, person_id, org_unit_uid):
    resp = request_with_retry(
        session, "GET", f"{BASE_URL}/tracker/trackedEntities",
        params={
            "program": PROGRAM_UID,
            "orgUnit": org_unit_uid,
            "ouMode": "DESCENDANTS",
            "filter": f"{PERSON_ID_ATTR_UID}:eq:{person_id}",
            "fields": "trackedEntity,enrollments[enrollment,orgUnit,status]",
        },
    )
    instances = extract_items(resp.json(), "trackedEntities", "instances")
    if not instances:
        sys.exit(
            f"ERROR: No tracked entity found for person_id={person_id}. "
            f"Check the ID and that this account has read access to the org unit "
            f"the participant belongs to."
        )
    inst = instances[0]
    enrollments = inst.get("enrollments", [])
    active = next((e for e in enrollments if e.get("status") == "ACTIVE"), None) or \
             (enrollments[0] if enrollments else None)
    if not active:
        sys.exit(f"ERROR: No enrollment found for person_id={person_id} in this program.")
    return {
        "trackedEntity": inst["trackedEntity"],
        "enrollment": active["enrollment"],
        "orgUnit": active["orgUnit"],
    }


def post_event(session, program_stage, enrollment_ctx, data_values, checkpoint_key, checkpoint):
    if checkpoint_key in checkpoint.get("completed_events", {}):
        print(f"    [skip] already imported -> {checkpoint['completed_events'][checkpoint_key]}")
        return checkpoint["completed_events"][checkpoint_key]

    payload = {
        "events": [{
            "program": PROGRAM_UID,
            "programStage": program_stage,
            "orgUnit": enrollment_ctx["orgUnit"],
            "enrollment": enrollment_ctx["enrollment"],
            "trackedEntity": enrollment_ctx["trackedEntity"],
            "status": "COMPLETED",
            "occurredAt": time.strftime("%Y-%m-%d"),
            "dataValues": data_values,
        }]
    }
    resp = request_with_retry(
        session, "POST", f"{BASE_URL}/tracker",
        params={"importStrategy": "CREATE", "async": "false"},
        json=payload,
    )
    result = resp.json()
    stats = result.get("stats", {})
    if stats.get("created", 0) != 1:
        raise RuntimeError(f"Event creation did not report success: {json.dumps(result)[:1500]}")

    # Pull the created event UID out of the bundle report for the checkpoint record.
    try:
        event_uid = result["bundleReport"]["typeReportMap"]["EVENT"]["objectReports"][0]["uid"]
    except (KeyError, IndexError):
        event_uid = "UNKNOWN"

    checkpoint.setdefault("completed_events", {})[checkpoint_key] = event_uid
    save_checkpoint(checkpoint)
    return event_uid


def load_checkpoint():
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE) as f:
            return json.load(f)
    return {"completed_events": {}}


def save_checkpoint(checkpoint):
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump(checkpoint, f, indent=2)


def dv(uid, value):
    """Build a dataValue entry, skipping None/NaN values entirely."""
    if value is None:
        return None
    if isinstance(value, float) and np.isnan(value):
        return None
    return {"dataElement": uid, "value": str(value)}


def clean_data_values(entries):
    return [e for e in entries if e is not None]


# ---------------------------------------------------------------------------
# Photography import
# ---------------------------------------------------------------------------

def import_photography(session, person_id, field_uids, enrollment_ctx, program_stage, checkpoint):
    print("\n=== Importing Retinal Photography events ===")
    manifest_path = os.path.join(PHOTOGRAPHY_DIR, "manifest.tsv")
    df = pd.read_csv(manifest_path, sep="\t")
    df["person_id"] = df["person_id"].astype(str)
    rows = df[df["person_id"] == str(person_id)]
    print(f"{len(rows)} rows for person_id={person_id}")

    for i, row in rows.iterrows():
        checkpoint_key = f"photography::{row['sop_instance_uid']}"
        print(f"  [{i}] {row['imaging']} {row['laterality']} {row['anatomic_region']}")

        jpeg_bytes = dicom_path_to_jpeg_bytes(row["filepath"])
        preview_uid = None
        if jpeg_bytes:
            filename = Path(str(row["filepath"])).stem + ".jpg"
            preview_uid = upload_file_resource(session, jpeg_bytes, filename)

        data_values = clean_data_values([
            dv(field_uids["Manufacturer"], row.get("manufacturer")),
            dv(field_uids["Model"], row.get("manufacturers_model_name")),
            dv(field_uids["Laterality"], row.get("laterality")),
            dv(field_uids["Anatomic Region"], row.get("anatomic_region")),
            dv(field_uids["Imaging Type (IR/CFP/FAF)"], row.get("imaging")),
            dv(field_uids["Image Height"], row.get("height")),
            dv(field_uids["Image Width"], row.get("width")),
            dv(field_uids["Color Channel Dimension"], row.get("color_channel_dimension")),
            dv(field_uids["SOP Instance UID"], row.get("sop_instance_uid")),
            dv(field_uids["Original DICOM File Path"], row.get("filepath")),
            dv(field_uids["Preview Image"], preview_uid),
        ])

        event_uid = post_event(session, program_stage, enrollment_ctx, data_values, checkpoint_key, checkpoint)
        print(f"    -> event {event_uid}")


# ---------------------------------------------------------------------------
# OCTA import
# ---------------------------------------------------------------------------

DEFAULT_ENFACE_LAYER_KEYWORD = "superficial"


def pick_enface_slot(row):
    """
    Find which of the 4 enface slots (1-4) matches the default preview
    layer ('Superficial vascular plexus flow'). Manufacturers phrase this
    slightly differently (e.g. Zeiss: 'Superficial retina vasculature
    flow'), so match on the keyword 'superficial' rather than an exact
    string. Falls back to slot 1 with a warning if no match is found.
    """
    for slot in [1, 2, 3, 4]:
        label = row.get(f"associated_enface_{slot}_ophthalmic_image_type")
        if isinstance(label, str) and DEFAULT_ENFACE_LAYER_KEYWORD in label.lower():
            return slot, label
    print("    [warn] no enface slot matched 'superficial' keyword; falling back to slot 1")
    return 1, row.get("associated_enface_1_ophthalmic_image_type")


def import_octa(session, person_id, field_uids, enrollment_ctx, program_stage, checkpoint):
    print("\n=== Importing Retinal OCTA events ===")
    manifest_path = os.path.join(OCTA_DIR, "manifest.tsv")
    df = pd.read_csv(manifest_path, sep="\t")
    df["person_id"] = df["person_id"].astype(str)
    rows = df[df["person_id"] == str(person_id)]
    print(f"{len(rows)} rows for person_id={person_id}")

    for i, row in rows.iterrows():
        checkpoint_key = f"octa::{row['flow_cube_sop_instance_uid']}"
        print(f"  [{i}] {row['manufacturer']} {row['laterality']} {row['anatomic_region']}")

        slot, layer_label = pick_enface_slot(row)
        enface_path = row.get(f"associated_enface_{slot}_file_path")
        enface_sop_uid = row.get(f"associated_enface_{slot}_sop_instance_uid")

        jpeg_bytes = dicom_path_to_jpeg_bytes(enface_path)
        preview_uid = None
        if jpeg_bytes:
            filename = Path(str(enface_path)).stem + ".jpg"
            preview_uid = upload_file_resource(session, jpeg_bytes, filename)
        else:
            print(f"    [warn] no usable enface image for this row (slot {slot}); preview will be empty")

        data_values = clean_data_values([
            dv(field_uids["Manufacturer"], row.get("manufacturer")),
            dv(field_uids["Model"], row.get("manufacturers_model_name")),
            dv(field_uids["Laterality"], row.get("laterality")),
            dv(field_uids["Anatomic Region"], row.get("anatomic_region")),
            dv(field_uids["Imaging Type (OCTA)"], row.get("imaging")),
            dv(field_uids["Flow Cube Height"], row.get("flow_cube_height")),
            dv(field_uids["Flow Cube Width"], row.get("flow_cube_width")),
            dv(field_uids["Flow Cube Number of Frames"], row.get("flow_cube_number_of_frames")),
            dv(field_uids["Flow Cube SOP Instance UID"], row.get("flow_cube_sop_instance_uid")),
            dv(field_uids["Flow Cube DICOM File Path"], row.get("flow_cube_file_path")),
            dv(field_uids["Segmentation DICOM File Path"], row.get("associated_segmentation_file_path")),
            dv(field_uids["Segmentation SOP Instance UID"], row.get("associated_segmentation_sop_instance_uid")),
            dv(field_uids["Segmentation Type"], row.get("associated_segmentation_type")),
            dv(field_uids["En-face Type / Layer"], layer_label),
            dv(field_uids["En-face SOP Instance UID"], enface_sop_uid),
            dv(field_uids["Preview Image"], preview_uid),
        ])

        event_uid = post_event(session, program_stage, enrollment_ctx, data_values, checkpoint_key, checkpoint)
        print(f"    -> event {event_uid}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def process_one_participant(session, person_id, enrollment_ctx, photo_stage, octa_stage, checkpoint):
    """Run both imports for one participant. Returns (success: bool, error: str or None)."""
    try:
        import_photography(
            session, person_id, photo_stage["fields"], enrollment_ctx,
            photo_stage["stage_uid"], checkpoint,
        )
        import_octa(
            session, person_id, octa_stage["fields"], enrollment_ctx,
            octa_stage["stage_uid"], checkpoint,
        )
        return True, None
    except Exception as e:
        return False, str(e)


def main():
    parser = argparse.ArgumentParser(description="Import retinal imaging into DHIS2")
    parser.add_argument("--person-id", default="1072",
                         help="Single participant to import (ignored if --all is set)")
    parser.add_argument("--all", action="store_true",
                         help="Import ALL participants in the roster, not just one")
    args, unknown = parser.parse_known_args()
    if unknown:
        print(f"[info] Ignoring unrecognized arguments (likely Jupyter kernel args): {unknown}")

    if not os.path.exists(METADATA_UID_FILE):
        sys.exit(f"ERROR: {METADATA_UID_FILE} not found. Run retinal_step1_metadata.py first.")
    with open(METADATA_UID_FILE) as f:
        metadata = json.load(f)

    session = get_session()
    checkpoint = load_checkpoint()
    org_unit_uid = resolve_org_unit_uid(session)

    photo_stage = metadata["stages"]["Retinal Photography"]
    octa_stage = metadata["stages"]["Retinal OCTA"]

    if not args.all:
        print(f"Pilot import for person_id={args.person_id}")
        enrollment_ctx = get_enrollment_context(session, args.person_id, org_unit_uid)
        print(f"Tracked entity: {enrollment_ctx['trackedEntity']}, "
              f"enrollment: {enrollment_ctx['enrollment']}, org unit: {enrollment_ctx['orgUnit']}")
        process_one_participant(session, args.person_id, enrollment_ctx, photo_stage, octa_stage, checkpoint)
        print("\nPilot import complete.")
        return

    # --- Full cohort import ---
    roster = fetch_full_roster(session, org_unit_uid)
    total = len(roster)
    print(f"Starting FULL COHORT import for {total} participants.\n")

    failed_participants = checkpoint.setdefault("failed_participants", {})
    done_participants = checkpoint.setdefault("done_participants", [])

    start_time = time.time()
    for i, entry in enumerate(roster, start=1):
        person_id = entry["person_id"]
        if person_id in done_participants:
            continue  # already fully completed in a prior run

        enrollment_ctx = {
            "trackedEntity": entry["trackedEntity"],
            "enrollment": entry["enrollment"],
            "orgUnit": entry["orgUnit"],
        }

        success, error = process_one_participant(
            session, person_id, enrollment_ctx, photo_stage, octa_stage, checkpoint
        )

        if success:
            done_participants.append(person_id)
            failed_participants.pop(person_id, None)
        else:
            print(f"  [FAILED] person_id={person_id}: {error}")
            failed_participants[person_id] = error

        save_checkpoint(checkpoint)

        if i % 25 == 0 or i == total:
            elapsed_min = (time.time() - start_time) / 60
            rate = i / elapsed_min if elapsed_min > 0 else 0
            remaining = total - i
            eta_min = remaining / rate if rate > 0 else float("inf")
            print(f"\n[progress] {i}/{total} participants processed "
                  f"({len(failed_participants)} failed so far). "
                  f"Elapsed: {elapsed_min:.1f} min, ETA: {eta_min:.0f} min\n")

    print("\n=== FULL COHORT IMPORT COMPLETE ===")
    print(f"Succeeded: {len(done_participants)} / {total}")
    print(f"Failed: {len(failed_participants)}")
    if failed_participants:
        print("Failed participant IDs (see retinal_import_checkpoint.json for error details):")
        for pid in list(failed_participants.keys())[:20]:
            print(f"  {pid}")
        if len(failed_participants) > 20:
            print(f"  ... and {len(failed_participants) - 20} more")


if __name__ == "__main__":
    main()
