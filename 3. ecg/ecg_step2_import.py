#!/usr/bin/env python3
"""Import one event per ECG recording from the WFDB .hea headers.

WHAT THE AUDIT FOUND (C-01, H-01, H-05, M-07), and what changed
----------------------------------------------------------------
1. H-01 was overstated, and this is corrected here. Status fields were
   written with the option display name, for example "Below reference range".
   Checked against the live server: the stored values are codes
   (WITHIN_REFERENCE_RANGE, BELOW_REFERENCE_RANGE), so DHIS2 2.44 resolved
   the name rather than rejecting it, and no data was lost. Values now go
   through dhis2.option_value, which reads the real code from the server, so
   the write no longer depends on that leniency.

2. M-07: occurredAt fell back to parsed["validation_date"], which is the raw
   20241014 form straight out of the header. That is exactly the format
   ecg_validation_date_fix.py exists to repair, and putting it in occurredAt
   repeats the mistake in a harder place to fix. It is now normalised, and a
   record with no usable date is skipped rather than given a fabricated
   2023-01-01.

3. H-05: the participant was checkpointed complete whether or not the import
   succeeded.

4. C-01: credentials and the dataset root come from the environment.

Verified against the real headers: every key read here is present, for example
"# Rate: 78", "# QRSD: 83", "# validation_date: 20241014".

USAGE
-----
    python3 "3. ecg/ecg_step2_import.py"
"""

import argparse
import os
import sys
import time
from datetime import datetime

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import aireadi, dhis2  # noqa: E402
from common import metadata_uids as M  # noqa: E402
from common.checkpoint import Checkpoint  # noqa: E402

from ecg_step1_metadata import (  # noqa: E402
    HR_RANGE, PR_RANGE, QRS_STATUS_SET, QRS_WIDENED_AT,
    RANGE_STATUS_SET, STAGE_NAME,
)

CHECKPOINT_FILE = "ecg_checkpoint.json"


def parse_hea(path):
    """Read a WFDB header: the signal line plus the '# key: value' comments."""
    with open(path) as fh:
        lines = fh.readlines()

    parts = lines[0].split()
    result = {
        "n_leads": int(parts[1]),
        "sampling_freq": float(parts[2]),
        "n_samples": int(parts[3]),
    }
    if result["sampling_freq"] > 0:
        result["duration_sec"] = round(result["n_samples"] / result["sampling_freq"], 2)

    for line in lines:
        line = line.strip()
        if line.startswith("#") and ":" in line:
            key, _, value = line[1:].partition(":")
            key = key.strip()
            # Never let a comment overwrite a value parsed from the signal line.
            if key not in ("n_leads", "sampling_freq", "n_samples", "duration_sec"):
                result[key] = value.strip()
    return result


def normalise_date(raw):
    """Turn 20241014 or 2024-10-14 into 2024-10-14. None when unusable."""
    if not raw:
        return None
    text = str(raw).strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def classify_range(value, low, high):
    if value is None:
        return None
    if value < low:
        name = "Below reference range"
    elif value > high:
        name = "Above reference range"
    else:
        name = "Within reference range"
    return dhis2.option_value(RANGE_STATUS_SET, name)


def classify_qrs(value):
    if value is None:
        return None
    name = ("Widened (meets bundle branch block QRS criteria)"
            if value >= QRS_WIDENED_AT else "Within reference range")
    return dhis2.option_value(QRS_STATUS_SET, name)


def build_event(registry, stage_uid, context, row, visit_date):
    path = aireadi.resolve("cardiac_ecg", row["wfdb_hea_filepath"])
    if path is None:
        return None, "header file not found"

    parsed = parse_hea(path)
    validation_date = normalise_date(parsed.get("validation_date"))
    occurred = normalise_date(visit_date) or validation_date
    if occurred is None:
        # Better to skip than to invent a date. A fabricated occurredAt is
        # indistinguishable from a real one once it is in the registry.
        return None, "no usable study visit or validation date"

    rate = to_int(parsed.get("Rate"))
    pr = to_int(parsed.get("PR"))
    qrsd = to_int(parsed.get("QRSD"))

    values = {
        "ECG Study Visit Date": normalise_date(visit_date),
        "ECG Validation Date": validation_date,
        "ECG Recording Duration (sec)": parsed.get("duration_sec"),
        "ECG Heart Rate (bpm)": rate,
        "ECG Heart Rate Status": classify_range(rate, *HR_RANGE),
        "ECG PR Interval (ms)": pr,
        "ECG PR Interval Status": classify_range(pr, *PR_RANGE),
        "ECG QRS Duration (ms)": qrsd,
        "ECG QRS Duration Status": classify_qrs(qrsd),
        "ECG QT Interval (ms)": to_int(parsed.get("QT")),
        "ECG QTc Interval (ms)": to_int(parsed.get("QTc")),
        "ECG P Axis (deg)": to_int(parsed.get("P")),
        "ECG QRS Axis (deg)": to_int(parsed.get("QRS")),
        "ECG T Axis (deg)": to_int(parsed.get("T")),
        "ECG Participant Position": parsed.get("participant_position"),
        "ECG Machine Interpretation Status": parsed.get("interpretation_comment_1"),
        "ECG Machine Interpretation Summary": parsed.get("interpretation_comment_2"),
        "ECG Finding 1": parsed.get("comment_1_key"),
        "ECG Finding 1 Detail": parsed.get("comment_1_val"),
        "ECG Finding 2": parsed.get("comment_2_key"),
        "ECG Finding 2 Detail": parsed.get("comment_2_val"),
        "ECG Finding 3": parsed.get("comment_3_key"),
        "ECG Finding 3 Detail": parsed.get("comment_3_val"),
        "ECG Device": parsed.get("device_model"),
        "ECG Sampling Frequency (Hz)": parsed.get("sampling_freq"),
        "ECG Number of Leads": parsed.get("n_leads"),
        "ECG Number of Samples": parsed.get("n_samples"),
        "ECG Raw Header File Path": row["wfdb_hea_filepath"],
        "ECG Raw Data File Path": row.get("wfdb_dat_filepath"),
    }

    data_values = []
    for name, value in values.items():
        entry = dhis2.data_value(registry.maybe_data_element(name), value)
        if entry:
            data_values.append(entry)

    return {
        "program": M.PROGRAM_UID,
        "programStage": stage_uid,
        "trackedEntity": context["trackedEntity"],
        "enrollment": context["enrollment"],
        "orgUnit": context["orgUnit"],
        "occurredAt": occurred,
        "status": "COMPLETED",
        "dataValues": data_values,
    }, None


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--limit", type=int)
    parser.add_argument("--person-id")
    args, _ = parser.parse_known_args()

    session = dhis2.get_session()
    registry = M.load(session)
    stage_uid = registry.stage(STAGE_NAME)

    manifest = aireadi.load_manifest("cardiac_ecg")
    participants = pd.read_csv(aireadi.participants_file(), sep="\t")
    participants["person_id"] = participants["person_id"].astype(str)
    visit_dates = dict(zip(participants["person_id"], participants["study_visit_date"]))

    all_ids = sorted(manifest)
    with Checkpoint(CHECKPOINT_FILE, flush_every=20) as checkpoint:
        remaining = [args.person_id] if args.person_id else checkpoint.pending(all_ids)
        if args.limit:
            remaining = remaining[:args.limit]
        print(f"Total {len(all_ids)}, {checkpoint.summary()}, {len(remaining)} to process")
        start = time.time()
        skipped = {}

        for index, person_id in enumerate(remaining, start=1):
            try:
                context = dhis2.get_tei_context(
                    session, M.PROGRAM_UID, M.PERSON_ID_ATTR_UID, person_id
                )
                if context is None:
                    checkpoint.mark_done(person_id, note="no tracked entity")
                    continue

                visit_date = visit_dates.get(person_id)
                events = []
                for row in manifest[person_id]:
                    event, reason = build_event(
                        registry, stage_uid, context, row, visit_date
                    )
                    if event:
                        events.append(event)
                    else:
                        skipped[reason] = skipped.get(reason, 0) + 1

                if events:
                    dhis2.send_events(session, events, "CREATE", batch_size=100)
                checkpoint.mark_done(person_id)

            except Exception as exc:
                checkpoint.mark_failed(person_id, exc)
                print(f"[{index}/{len(remaining)}] {person_id}: FAILED, {str(exc)[:300]}")

            if index % 100 == 0:
                print(f"[{index}/{len(remaining)}] {time.time() - start:.0f}s elapsed")

    print(f"\nRun complete. {checkpoint.summary()}")
    for reason, count in skipped.items():
        print(f"  skipped {count} recording(s): {reason}")
    return 1 if checkpoint.failed else 0


if __name__ == "__main__":
    sys.exit(main())
