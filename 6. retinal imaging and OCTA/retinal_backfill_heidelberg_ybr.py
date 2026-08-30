#!/usr/bin/env python3
"""
retinal_backfill_heidelberg_ybr.py

Targeted backfill for exactly two known, fixed issues:
  1. Heidelberg events with an EMPTY Preview Image (pylibjpeg dependency
     gap -- now installed). Applies to BOTH Retinal Photography and
     Retinal OCTA stages.
  2. Optomed / iCare Eidon events with a WRONG-COLORED (teal/pink)
     Preview Image (YBR colorspace bug -- now fixed). Applies to Retinal
     Photography only (OCTA enface files are all MONOCHROME2, confirmed
     unaffected).

Deliberately does NOT touch "broken data stream" corrupted-file events --
those are not fixable and are excluded by design.

For each affected event, this script:
  - Re-converts the correct source image using the fixed conversion logic
  - Uploads the new image as a DHIS2 file resource
  - UPDATES the EXISTING event's Preview Image field (importStrategy=UPDATE
    on the same event UID) -- this does NOT create a new event and does
    NOT duplicate anything in the registry
  - Deletes the OLD (defective) file resource after the new one is
    successfully attached, so the wrong image doesn't linger as orphaned,
    still-accessible storage

Checkpointed and resumable: safe to stop and re-run at any time without
reprocessing or duplicating already-fixed events.

USAGE
-----
    export DHIS2_USERNAME="admin"
    export DHIS2_PASSWORD="..."
    python3 retinal_backfill_heidelberg_ybr.py
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
    from pydicom.pixel_data_handlers.util import convert_color_space
except ImportError:
    sys.exit("pydicom is required. Install with:\n    pip install pydicom --user\n")

BASE_URL = "https://t2d-registry.plhi.us/api"
PROGRAM_UID = "W3LSFZH3UDq"
PERSON_ID_ATTR_UID = "oFbmOHnKYaX"
AI_READI_ROOT = os.path.expanduser("~/AI-READI-fixed")

METADATA_UID_FILE = "retinal_metadata_uids.json"
CHECKPOINT_FILE_FULL = "retinal_backfill_heidelberg_ybr_checkpoint.json"
CHECKPOINT_FILE_PILOT = "retinal_backfill_heidelberg_ybr_checkpoint_PILOT.json"

MAX_DIMENSION = 800
JPEG_QUALITY = 85

YBR_AFFECTED_MANUFACTURERS = {"Optomed", "iCare"}
HEIDELBERG_MANUFACTURER = "Heidelberg"

RETRY_STATUS_CODES = {502, 503, 504}
MAX_RETRIES = 5
RETRY_BACKOFF_SECONDS = 3


# ---------------------------------------------------------------------------
# Session / retry / response-key helpers
# ---------------------------------------------------------------------------

def get_session():
    username = os.environ.get("DHIS2_USERNAME")
    password = os.environ.get("DHIS2_PASSWORD")
    if not username or not password:
        sys.exit("ERROR: Set DHIS2_USERNAME and DHIS2_PASSWORD (ADMIN account) first.")
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
            print(f"    [warn] {resp.status_code}, retrying in {wait}s...")
            time.sleep(wait)
            continue
        print(f"    [error] {method} {url} -> {resp.status_code}: {resp.text[:600]}")
        resp.raise_for_status()
    raise RuntimeError(f"Failed after {MAX_RETRIES} retries: {url}")


def extract_items(response_json, *candidate_keys):
    for key in candidate_keys:
        if key in response_json:
            return response_json[key]
    return []


# ---------------------------------------------------------------------------
# DICOM -> JPEG conversion (fixed logic: pylibjpeg installed, YBR handled)
# ---------------------------------------------------------------------------

def dicom_to_preview_array(ds):
    pixel_array = ds.pixel_array
    if pixel_array.ndim >= 3 and int(getattr(ds, "NumberOfFrames", 1)) > 1:
        frame = pixel_array[pixel_array.shape[0] // 2]
    else:
        frame = pixel_array

    photometric = getattr(ds, "PhotometricInterpretation", "")
    if photometric.startswith("YBR"):
        frame = convert_color_space(frame, photometric, "RGB")

    frame = frame.astype(np.float64)
    slope = float(getattr(ds, "RescaleSlope", 1))
    intercept = float(getattr(ds, "RescaleIntercept", 0))
    frame = frame * slope + intercept

    fmin, fmax = frame.min(), frame.max()
    frame = (frame - fmin) / (fmax - fmin) * 255.0 if fmax > fmin else np.zeros_like(frame)
    return frame.astype(np.uint8)


def dicom_path_to_jpeg_bytes(rel_path):
    if pd.isna(rel_path) or str(rel_path).strip().lower() == "not reported":
        return None, "no file path recorded on event"
    full_path = os.path.join(AI_READI_ROOT, str(rel_path).lstrip("/"))
    if not os.path.exists(full_path):
        return None, f"file not found: {full_path}"
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
        return buf.getvalue(), None
    except Exception as e:
        return None, str(e)


# ---------------------------------------------------------------------------
# DHIS2 file resource + event helpers
# ---------------------------------------------------------------------------

def upload_file_resource(session, jpeg_bytes, filename):
    files = {"file": (filename, jpeg_bytes, "image/jpeg")}
    resp = request_with_retry(session, "POST", f"{BASE_URL}/fileResources",
                               params={"domain": "DATA_VALUE"}, files=files)
    data = resp.json()
    fr = data.get("response", {}).get("fileResource") or data.get("fileResource") or data
    fr_id = fr.get("id")
    if not fr_id:
        raise RuntimeError(f"Could not find fileResource id in response: {data}")
    return fr_id


def delete_file_resource(session, file_resource_id):
    """Best-effort delete of the old, defective file resource. Never raises --
    logs and continues, since a leftover orphaned file is a minor storage
    concern, not a correctness concern (nothing references it anymore)."""
    try:
        request_with_retry(session, "DELETE", f"{BASE_URL}/fileResources/{file_resource_id}")
        return True
    except Exception as e:
        print(f"    [warn] could not delete old file resource {file_resource_id}: {e}")
        return False


def update_event_preview(session, event_uid, program_stage, org_unit, enrollment,
                          occurred_at, status, preview_field_uid, new_file_resource_id):
    """UPDATE (not create) the existing event's Preview Image value only.
    Using importStrategy=UPDATE on the same event UID guarantees this does
    not create a duplicate event.

    IMPORTANT: DHIS2 tracker UPDATE requires the full event context
    (program, programStage, orgUnit, enrollment, occurredAt, status) even
    when only a single dataValue is being changed -- omitting any of these
    causes a 409 "Missing required event property" error."""
    payload = {
        "events": [{
            "event": event_uid,
            "program": PROGRAM_UID,
            "programStage": program_stage,
            "orgUnit": org_unit,
            "enrollment": enrollment,
            "occurredAt": occurred_at,
            "status": status,
            "dataValues": [{"dataElement": preview_field_uid, "value": new_file_resource_id}],
        }]
    }
    resp = request_with_retry(
        session, "POST", f"{BASE_URL}/tracker",
        params={"importStrategy": "UPDATE", "async": "false"},
        json=payload,
    )
    result = resp.json()
    stats = result.get("stats", {})
    if stats.get("updated", 0) != 1:
        raise RuntimeError(f"Event update did not report success: {json.dumps(result)[:800]}")


# ---------------------------------------------------------------------------
# Roster + event scanning
# ---------------------------------------------------------------------------

AI_READI_STUDY_ORG_UNIT_NAME = "AI-READI Study"


def resolve_org_unit_uid(session):
    resp = request_with_retry(
        session, "GET", f"{BASE_URL}/organisationUnits",
        params={"filter": f"name:eq:{AI_READI_STUDY_ORG_UNIT_NAME}", "fields": "id,name"},
    )
    results = resp.json().get("organisationUnits", [])
    if not results:
        sys.exit(f"ERROR: Could not find org unit named '{AI_READI_STUDY_ORG_UNIT_NAME}'.")
    return results[0]["id"]


def fetch_all_tracked_entities(session, org_unit_uid):
    te_uids = []
    page = 1
    page_size = 500
    while True:
        resp = request_with_retry(
            session, "GET", f"{BASE_URL}/tracker/trackedEntities",
            params={
                "program": PROGRAM_UID, "orgUnit": org_unit_uid, "ouMode": "DESCENDANTS",
                "fields": "trackedEntity", "page": page, "pageSize": page_size,
            },
        )
        instances = extract_items(resp.json(), "trackedEntities", "instances")
        if not instances:
            break
        te_uids.extend(inst["trackedEntity"] for inst in instances)
        if len(instances) < page_size:
            break
        page += 1
    return te_uids


def fetch_single_tracked_entity(session, org_unit_uid, person_id):
    """Resolve one participant's trackedEntity UID by their AI-READI person_id,
    for pilot-scoped runs (same proven filter pattern used in the main import)."""
    resp = request_with_retry(
        session, "GET", f"{BASE_URL}/tracker/trackedEntities",
        params={
            "program": PROGRAM_UID, "orgUnit": org_unit_uid, "ouMode": "DESCENDANTS",
            "filter": f"{PERSON_ID_ATTR_UID}:eq:{person_id}",
            "fields": "trackedEntity",
        },
    )
    instances = extract_items(resp.json(), "trackedEntities", "instances")
    if not instances:
        sys.exit(f"ERROR: No participant found for person_id={person_id}.")
    return [instances[0]["trackedEntity"]]


def find_events_for_stage(session, program_stage_uid, te_uid):
    resp = request_with_retry(
        session, "GET", f"{BASE_URL}/tracker/events",
        params={
            "program": PROGRAM_UID, "programStage": program_stage_uid, "trackedEntity": te_uid,
            "fields": "event,orgUnit,enrollment,occurredAt,status,dataValues[dataElement,value]",
            "pageSize": 200,
        },
    )
    return extract_items(resp.json(), "events", "instances")


def classify_and_collect(events, field_uids, path_field_name, stage_supports_ybr):
    """
    Return a list of {event, orgUnit, path, old_preview, reason} for events
    that need fixing, based on Manufacturer + current Preview Image state.
    """
    manufacturer_uid = field_uids["Manufacturer"]
    preview_uid = field_uids["Preview Image"]
    path_uid = field_uids[path_field_name]

    to_fix = []
    for ev in events:
        values = {dv["dataElement"]: dv.get("value") for dv in ev.get("dataValues", [])}
        manufacturer = values.get(manufacturer_uid, "")
        preview_value = values.get(preview_uid)
        path_value = values.get(path_uid)

        if not path_value:
            continue  # nothing to convert from

        if manufacturer == HEIDELBERG_MANUFACTURER and not preview_value:
            to_fix.append({"event": ev["event"], "orgUnit": ev["orgUnit"], "path": path_value,
                            "old_preview": preview_value, "reason": "heidelberg_missing",
                            "enrollment": ev.get("enrollment"), "occurredAt": ev.get("occurredAt"),
                            "status": ev.get("status", "COMPLETED")})
        elif stage_supports_ybr and manufacturer in YBR_AFFECTED_MANUFACTURERS:
            to_fix.append({"event": ev["event"], "orgUnit": ev["orgUnit"], "path": path_value,
                            "old_preview": preview_value, "reason": "ybr_miscolor",
                            "enrollment": ev.get("enrollment"), "occurredAt": ev.get("occurredAt"),
                            "status": ev.get("status", "COMPLETED")})

    return to_fix


def load_checkpoint(checkpoint_file):
    if os.path.exists(checkpoint_file):
        with open(checkpoint_file) as f:
            return json.load(f)
    return {"fixed_events": {}, "failed_events": {}}


def save_checkpoint(cp, checkpoint_file):
    with open(checkpoint_file, "w") as f:
        json.dump(cp, f, indent=2)


def process_stage(session, stage_name, stage_uid, field_uids, path_field_name,
                   stage_supports_ybr, te_uids, checkpoint, checkpoint_file):
    print(f"\n=== Scanning {stage_name} ({len(te_uids)} participants) ===")
    preview_uid = field_uids["Preview Image"]

    fixed = checkpoint["fixed_events"]
    failed = checkpoint["failed_events"]

    heidelberg_count = 0
    ybr_count = 0
    processed = 0

    for i, te_uid in enumerate(te_uids, start=1):
        events = find_events_for_stage(session, stage_uid, te_uid)
        candidates = classify_and_collect(events, field_uids, path_field_name, stage_supports_ybr)

        for item in candidates:
            event_uid = item["event"]
            if event_uid in fixed:
                continue

            if item["reason"] == "heidelberg_missing":
                heidelberg_count += 1
            else:
                ybr_count += 1

            jpeg_bytes, error = dicom_path_to_jpeg_bytes(item["path"])
            if jpeg_bytes is None:
                failed[event_uid] = f"conversion failed ({item['reason']}): {error}"
                save_checkpoint(checkpoint, checkpoint_file)
                continue

            try:
                filename = Path(item["path"]).stem + ".jpg"
                new_fr_id = upload_file_resource(session, jpeg_bytes, filename)
                update_event_preview(session, event_uid, stage_uid, item["orgUnit"],
                                      item["enrollment"], item["occurredAt"], item["status"],
                                      preview_uid, new_fr_id)

                # Remove the old defective image now that the new one is attached
                if item["old_preview"]:
                    delete_file_resource(session, item["old_preview"])

                fixed[event_uid] = {"reason": item["reason"], "new_file_resource": new_fr_id}
                failed.pop(event_uid, None)
            except Exception as e:
                failed[event_uid] = f"update failed ({item['reason']}): {e}"

            save_checkpoint(checkpoint, checkpoint_file)
            processed += 1

        if i % 200 == 0 or i == len(te_uids):
            print(f"  {i}/{len(te_uids)} participants scanned -- "
                  f"{heidelberg_count} Heidelberg + {ybr_count} YBR candidates found so far, "
                  f"{len(fixed)} fixed, {len(failed)} failed")

    print(f"\n{stage_name} complete. Heidelberg candidates: {heidelberg_count}, "
          f"YBR candidates: {ybr_count}")


def main():
    parser = argparse.ArgumentParser(description="Backfill Heidelberg + YBR affected preview images")
    parser.add_argument("--person-id", default=None,
                         help="If set, only process this one participant (pilot run). "
                              "Omit to process the full cohort.")
    args, unknown = parser.parse_known_args()
    if unknown:
        print(f"[info] Ignoring unrecognized arguments (likely Jupyter kernel args): {unknown}")

    if not os.path.exists(METADATA_UID_FILE):
        sys.exit(f"ERROR: {METADATA_UID_FILE} not found.")
    with open(METADATA_UID_FILE) as f:
        metadata = json.load(f)

    session = get_session()
    checkpoint_file = CHECKPOINT_FILE_PILOT if args.person_id else CHECKPOINT_FILE_FULL
    checkpoint = load_checkpoint(checkpoint_file)

    org_unit_uid = resolve_org_unit_uid(session)

    if args.person_id:
        print(f"PILOT MODE: scoping to person_id={args.person_id} only.")
        print(f"Checkpoint file: {checkpoint_file}\n")
        te_uids = fetch_single_tracked_entity(session, org_unit_uid, args.person_id)
    else:
        print("FULL COHORT MODE: processing all participants.")
        print(f"Checkpoint file: {checkpoint_file}\n")
        print("Fetching participant roster...")
        te_uids = fetch_all_tracked_entities(session, org_unit_uid)
        print(f"Found {len(te_uids)} participants.")

    photo = metadata["stages"]["Retinal Photography"]
    octa = metadata["stages"]["Retinal OCTA"]

    # Retinal Photography: both Heidelberg and YBR issues apply here
    process_stage(
        session, "Retinal Photography", photo["stage_uid"], photo["fields"],
        "Original DICOM File Path", stage_supports_ybr=True,
        te_uids=te_uids, checkpoint=checkpoint, checkpoint_file=checkpoint_file,
    )

    # Retinal OCTA: only Heidelberg applies (enface files are all MONOCHROME2)
    process_stage(
        session, "Retinal OCTA", octa["stage_uid"], octa["fields"],
        "Flow Cube DICOM File Path", stage_supports_ybr=False,
        te_uids=te_uids, checkpoint=checkpoint, checkpoint_file=checkpoint_file,
    )

    print(f"\n=== BACKFILL COMPLETE ===")
    print(f"Total events fixed: {len(checkpoint['fixed_events'])}")
    print(f"Total events still failing: {len(checkpoint['failed_events'])}")
    if checkpoint["failed_events"]:
        print(f"See {checkpoint_file} for per-event error details.")


if __name__ == "__main__":
    main()
