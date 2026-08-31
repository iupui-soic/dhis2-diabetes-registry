#!/usr/bin/env python3
"""Import one event per diagnosed condition from condition_occurrence.csv.

condition_source_value has the form "code, Label text" and is split on the
first comma. Verified across the dataset: all 12,375 values contain a comma.

WHAT THE AUDIT FOUND (C-01, H-05, H-08), and what changed
----------------------------------------------------------
H-08: parse_condition_value returned its argument unchanged when it was not a
string, so a missing condition_source_value came back as a float NaN for both
code and label. The data value was then built as {'value': code} with no
str(), and json.dumps writes a bare NaN token, which is not valid JSON, so
DHIS2 would reject the whole 100-event batch. The same applied to
occurredAt: str(date), which became the string "nan".

That path is unreachable on the current export, which has zero nulls in
either column, but it is one upstream change away from rejecting entire
batches. Rows missing a code or a date are now skipped and counted, and every
value goes through the shared null and NaN guard.

H-05: the participant was checkpointed complete regardless of the outcome.

USAGE
-----
    python3 "5. diagnosis/diagnosis_step2_import.py"
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

from diagnosis_step1_metadata import STAGE_NAME  # noqa: E402

CHECKPOINT_FILE = "diagnosis_checkpoint.json"
BATCH_SIZE = 100


def parse_condition_value(raw):
    """'mhoccur_ad, Dementia' -> ('mhoccur_ad', 'Dementia').

    Returns (None, None) for anything that is not usable text, so a NaN can
    never reach the payload.
    """
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None, None
    text = str(raw).strip()
    if not text or text.lower() == "nan":
        return None, None
    if "," not in text:
        return text, ""
    code, _, label = text.partition(",")
    return code.strip(), label.strip()


def parse_date(raw):
    """Normalise a condition_start_date. None when unusable."""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    text = str(raw).strip()
    if not text or text.lower() in ("nan", "nat"):
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(text[:10], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def build_events(field_uids, stage_uid, context, rows):
    events, skipped = [], {}
    for row in rows.itertuples(index=False):
        code, label = parse_condition_value(getattr(row, "condition_source_value", None))
        date = parse_date(getattr(row, "condition_start_date", None))

        if code is None:
            skipped["no condition code"] = skipped.get("no condition code", 0) + 1
            continue
        if date is None:
            skipped["no usable start date"] = skipped.get("no usable start date", 0) + 1
            continue

        data_values = []
        for uid, value in (
            (field_uids["code"], code),
            (field_uids["label"], label),
            (field_uids["date"], date),
        ):
            entry = dhis2.data_value(uid, value)
            if entry:
                data_values.append(entry)

        events.append({
            "program": M.PROGRAM_UID,
            "programStage": stage_uid,
            "trackedEntity": context["trackedEntity"],
            "enrollment": context["enrollment"],
            "orgUnit": context["orgUnit"],
            "occurredAt": date,
            "status": "COMPLETED",
            "dataValues": data_values,
        })
    return events, skipped


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--limit", type=int)
    parser.add_argument("--person-id")
    args, _ = parser.parse_known_args()

    session = dhis2.get_session()
    registry = M.load(session)
    stage_uid = registry.stage(STAGE_NAME)
    field_uids = {
        "code": registry.data_element("Diagnosis Condition Code"),
        "label": registry.data_element("Diagnosis Condition Label"),
        "date": registry.data_element("Diagnosis Date"),
    }

    conditions = pd.read_csv(aireadi.clinical_file("condition_occurrence.csv"))
    conditions["person_id"] = conditions["person_id"].astype(str)
    by_person = dict(tuple(conditions.groupby("person_id")))
    all_ids = sorted(by_person)

    with Checkpoint(CHECKPOINT_FILE, flush_every=20) as checkpoint:
        remaining = [args.person_id] if args.person_id else checkpoint.pending(all_ids)
        if args.limit:
            remaining = remaining[:args.limit]
        print(f"Total {len(all_ids)}, {checkpoint.summary()}, {len(remaining)} to process")
        start = time.time()
        skipped_total = {}

        for index, person_id in enumerate(remaining, start=1):
            try:
                context = dhis2.get_tei_context(
                    session, M.PROGRAM_UID, M.PERSON_ID_ATTR_UID, person_id
                )
                if context is None:
                    checkpoint.mark_done(
                        person_id,
                        note="no tracked entity; this OMOP person_id may not match "
                             "the registry person_id scheme",
                    )
                    continue

                events, skipped = build_events(
                    field_uids, stage_uid, context, by_person[person_id]
                )
                for reason, count in skipped.items():
                    skipped_total[reason] = skipped_total.get(reason, 0) + count

                if events:
                    dhis2.send_events(session, events, "CREATE", batch_size=BATCH_SIZE)
                checkpoint.mark_done(person_id)

            except Exception as exc:
                checkpoint.mark_failed(person_id, exc)
                print(f"[{index}/{len(remaining)}] {person_id}: FAILED, {str(exc)[:300]}")

            if index % 100 == 0:
                print(f"[{index}/{len(remaining)}] {time.time() - start:.0f}s elapsed")

    print(f"\nRun complete. {checkpoint.summary()}")
    for reason, count in skipped_total.items():
        print(f"  skipped {count} row(s): {reason}")
    return 1 if checkpoint.failed else 0


if __name__ == "__main__":
    sys.exit(main())
