#!/usr/bin/env python3
"""Verify imported DHIS2 values against the AI-READI source files.

READ-ONLY against DHIS2. GET requests only, using the auditor account.

WHAT THE AUDIT FOUND, and what changed. Read this before quoting any number.
---------------------------------------------------------------------------
H-10  The sampling was not stratified. sample_participants was called with the
      same seed and the same population for all seven groups, so
      random.Random(42).sample(...) returned the identical 500 participants
      seven times. Effective coverage was 500 of 2,280, not seven draws. Each
      group now derives its own seed.

      The proportional allocation the old docstring described in detail,
      plan_sample_sizes, was never called: main overrode it with a fixed 500.
      Allocation is now genuinely proportional to live event counts, with
      --fixed-n available if the counting queries are too slow.

H-11  The match rate was inflated by construction. An event whose hour had no
      matching source hour was skipped with `continue`, so it counted neither
      as checked nor as a discrepancy and simply left the denominator. A
      systematic hour misalignment would have produced checked=0, printed as
      "n/a" rather than as a failure. Unmatched records are now counted as
      discrepancies and reported as their own category.

H-12  CORE_REGISTRY_CHECKED = 82302 and CORE_REGISTRY_MATCHED = 82302 were
      hard-coded and folded into the headline "combined exact-match rate",
      asserting 100% for 82,302 values this script never checks. The prior
      exhaustive result is now reported separately, with its provenance, and
      never merged into a measured rate.

H-13  metadata_uids was imported but never committed, so this script could not
      run at all. It now lives in common/metadata_uids.py.

H-14  NOx was excluded on the stated grounds that "this participant's raw
      source CSV has no nox column at all". That is not what the data shows:
      all 40 environment files sampled carry a nox column, and person 7427 has
      203,183 usable values in 203,185 rows. NOx is now verified like every
      other column.

M-03  The hour lookup was a linear scan that re-parsed every source hour for
      every event. Source hours are now indexed once.

M-11  Only rows[0] of each participant's manifest was used, ignoring a second
      wearable period or re-scan.

USAGE
-----
    python3 "7. verification data script/stratified_sample_verification.py"
    python3 "7. verification data script/stratified_sample_verification.py" --fixed-n 200
"""

import argparse
import hashlib
import json
import os
import random
import sys
import time

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "2. wearable script"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "4. envt"))

from common import aireadi, dhis2  # noqa: E402
from common import metadata_uids as M  # noqa: E402
from common.timeutil import hour_key  # noqa: E402

from hourly_aggregation_logic_final import (  # noqa: E402
    aggregate_glucose, aggregate_simple, extract_heart_rate,
    extract_oxygen_saturation, extract_respiratory_rate, extract_stress,
)
from env_aggregation_logic_v2 import (  # noqa: E402
    RELEVANT_COLUMNS, aggregate_env_column, read_env_csv,
)

CHECKPOINT_FILE = "stratified_sample_checkpoint.json"
BASE_SEED = 42
TOTAL_SAMPLE_TARGET = 2000

# The prior exhaustive check of the core registry. Reported separately and
# never merged into a measured rate. See H-12.
PRIOR_CORE_REGISTRY = {
    "values": 82302,
    "discrepancies": 0,
    "method": "exhaustive, all values, established in an earlier project phase",
    "verified_by_this_script": False,
}

CONTINUOUS_WEARABLE = {
    "heart_rate": ("heartrate_filepath", extract_heart_rate, "Wearable - Heart Rate"),
    "respiratory_rate": ("respiratory_rate_filepath", extract_respiratory_rate,
                         "Wearable - Respiratory Rate"),
    "spo2": ("oxygen_saturation_filepath", extract_oxygen_saturation, "Wearable - SpO2"),
    "stress": ("stress_level_filepath", extract_stress, "Wearable - Stress"),
}

GLUCOSE_COMPARED = ["mean", "min", "max", "count", "sd",
                    "tir_pct", "tar_pct", "tbr_pct", "high_count", "low_count"]

RETINAL_PHOTOGRAPHY_COLUMNS = [
    "manufacturer", "manufacturers_model_name", "laterality", "anatomic_region",
    "imaging", "height", "width", "color_channel_dimension", "filepath",
]


class Tally:
    """Match counts that distinguish a mismatch from a record never compared.

    H-11: a source hour with no event, or an event with no source hour, is a
    real finding. It is counted here rather than dropped.
    """

    def __init__(self):
        self.matched = 0
        self.mismatched = 0
        self.unmatched_events = 0
        self.unmatched_source = 0

    @property
    def checked(self):
        return self.matched + self.mismatched

    @property
    def discrepancies(self):
        return self.mismatched + self.unmatched_events + self.unmatched_source

    @property
    def total_compared(self):
        return self.checked + self.unmatched_events + self.unmatched_source

    def compare(self, dhis2_value, source_value):
        if close_enough(dhis2_value, source_value):
            self.matched += 1
        else:
            self.mismatched += 1

    def as_dict(self):
        rate = (100 * self.matched / self.total_compared) if self.total_compared else None
        return {
            "values_compared": self.total_compared,
            "matched": self.matched,
            "mismatched": self.mismatched,
            "events_with_no_source_hour": self.unmatched_events,
            "source_hours_with_no_event": self.unmatched_source,
            "discrepancies": self.discrepancies,
            "match_rate_pct": round(rate, 4) if rate is not None else None,
        }


def close_enough(a, b, tol=0.01):
    """Numeric comparison with a tolerance, falling back to exact strings.

    A missing value on either side is never a match. Two blanks are not
    evidence of correctness.
    """
    if a is None or b is None:
        return False
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return str(a).strip() == str(b).strip()


def group_seed(group):
    """A distinct, reproducible seed per group. See H-10."""
    digest = hashlib.sha256(f"{BASE_SEED}:{group}".encode()).hexdigest()
    return int(digest[:8], 16)


def sample_participants(group, n, participants):
    rng = random.Random(group_seed(group))
    valid = [(tei, pid) for tei, pid in participants if pid]
    return rng.sample(valid, min(n, len(valid)))


def get_participants(session):
    items = dhis2.fetch_all_pages(
        session, "tracker/trackedEntities",
        {"program": M.PROGRAM_UID, "fields": "trackedEntity,attributes[attribute,value]"},
        ("trackedEntities", "instances"),
    )
    out = []
    for item in items:
        pid = next(
            (a.get("value") for a in item.get("attributes", [])
             if a.get("attribute") == M.PERSON_ID_ATTR_UID),
            None,
        )
        out.append((item["trackedEntity"], pid))
    return out


def event_value(event, de_uid):
    for dv in event.get("dataValues", []):
        if dv.get("dataElement") == de_uid:
            return dv.get("value")
    return None


def index_by_hour(hourly):
    """{hour_key: stats}. M-03: built once instead of scanned per event."""
    return {hour_key(k): v for k, v in hourly.items()}


def compare_hourly(tally, events, source_hourly, field_uids):
    """Compare one stage's events against one aggregated source series."""
    indexed = index_by_hour(source_hourly)
    seen = set()

    for event in events:
        occurred = event.get("occurredAt")
        if not occurred:
            tally.unmatched_events += 1
            continue
        key = hour_key(occurred)
        stats = indexed.get(key)
        if stats is None:
            tally.unmatched_events += 1
            continue
        seen.add(key)
        for field, de_uid in field_uids.items():
            tally.compare(event_value(event, de_uid), stats.get(field))

    # H-11: a source hour that produced no event is also a discrepancy.
    tally.unmatched_source += len(set(indexed) - seen)


# ---------------------------------------------------------------------------
# Per-domain verification
# ---------------------------------------------------------------------------

def verify_wearable(session, registry, n, participants):
    manifest = aireadi.load_manifest("wearable_activity_monitor")
    shared = registry.wearable_shared_uids()
    tally = Tally()

    for tei, pid in sample_participants("wearable", n, participants):
        rows = manifest.get(pid, [])
        for _, (column, extractor, stage_name) in CONTINUOUS_WEARABLE.items():
            # M-11: every manifest row, not just the first.
            readings = []
            for row in rows:
                path = aireadi.resolve("wearable_activity_monitor", row.get(column))
                if path:
                    readings.extend(extractor(path))
            if not readings:
                continue
            events = dhis2.fetch_events(
                session, M.PROGRAM_UID, registry.stage(stage_name), tei
            )
            compare_hourly(tally, events, aggregate_simple(readings), shared)
    return tally


def verify_glucose(session, registry, n, participants):
    manifest = aireadi.load_manifest("wearable_blood_glucose")
    field_uids = {k: M.GLUCOSE_FIELD_UIDS[k] for k in GLUCOSE_COMPARED}
    stage_uid = registry.stage("CGM - Glucose")
    tally = Tally()

    for tei, pid in sample_participants("glucose", n, participants):
        for row in manifest.get(pid, []):
            path = aireadi.resolve("wearable_blood_glucose", row.get("glucose_filepath"))
            if not path:
                continue
            events = dhis2.fetch_events(session, M.PROGRAM_UID, stage_uid, tei)
            compare_hourly(tally, events, aggregate_glucose(path), field_uids)
    return tally


def verify_environmental(session, registry, n, participants):
    manifest = aireadi.load_manifest("environment")
    base = registry.env_field_uids()
    compared = {k: base[k] for k in ("mean", "min", "max", "sd", "count")}
    tally = Tally()

    for tei, pid in sample_participants("environmental", n, participants):
        rows = []
        for row in manifest.get(pid, []):
            path = aireadi.resolve("environment", row.get("env_sensor_filepath"))
            if path:
                rows.extend(read_env_csv(path))
        if not rows:
            continue
        # H-14: every column including nox. A participant whose nox column is
        # all nan simply produces no buckets, which read_env_csv handles.
        for column in RELEVANT_COLUMNS:
            events = dhis2.fetch_events(
                session, M.PROGRAM_UID, registry.environment_stage(column), tei
            )
            compare_hourly(tally, events, aggregate_env_column(rows, column), compared)
    return tally


def verify_diagnosis(session, registry, n, participants):
    conditions = pd.read_csv(aireadi.clinical_file("condition_occurrence.csv"))
    conditions["person_id"] = conditions["person_id"].astype(str)
    by_person = dict(tuple(conditions.groupby("person_id")))

    stage_uid = registry.stage("Diagnosis History")
    code_de = registry.data_element("Diagnosis Condition Code")
    label_de = registry.data_element("Diagnosis Condition Label")
    date_de = registry.data_element("Diagnosis Date")
    tally = Tally()

    for tei, pid in sample_participants("diagnosis", n, participants):
        source_rows = by_person.get(pid)
        if source_rows is None:
            continue
        by_code = {}
        for row in source_rows.to_dict("records"):
            raw = str(row.get("condition_source_value", ""))
            code, _, label = raw.partition(",")
            by_code.setdefault(code.strip(), []).append(
                (label.strip(), str(row.get("condition_start_date", ""))[:10])
            )

        events = dhis2.fetch_events(session, M.PROGRAM_UID, stage_uid, tei)
        matched_codes = set()
        for event in events:
            code = event_value(event, code_de)
            candidates = by_code.get(code)
            if not candidates:
                tally.unmatched_events += 1
                continue
            matched_codes.add(code)
            label, date = candidates[0]
            tally.compare(code, code)
            tally.compare(event_value(event, label_de), label)
            tally.compare(event_value(event, date_de), date)
        tally.unmatched_source += len(set(by_code) - matched_codes)
    return tally


def verify_retinal_photography(session, registry, n, participants):
    frame = pd.read_csv(aireadi.manifest_path("retinal_photography"), sep="\t")
    frame["person_id"] = frame["person_id"].astype(str)
    by_person = {}
    for row in frame.to_dict("records"):
        by_person.setdefault(row["person_id"], []).append(row)

    stage_uid = registry.stage("Retinal Photography")
    fields = M.RETINAL_PHOTOGRAPHY_FIELD_UIDS
    sop_de = fields["sop_instance_uid"]
    tally = Tally()

    for tei, pid in sample_participants("retinal_photography", n, participants):
        rows = by_person.get(pid, [])
        if not rows:
            continue
        events = dhis2.fetch_events(session, M.PROGRAM_UID, stage_uid, tei)
        by_sop = {}
        for event in events:
            sop = event_value(event, sop_de)
            if sop:
                by_sop[sop] = event

        for row in rows:
            event = by_sop.get(row.get("sop_instance_uid"))
            if event is None:
                # H-11: a manifest row with no event is a real gap.
                tally.unmatched_source += 1
                continue
            for column in RETINAL_PHOTOGRAPHY_COLUMNS:
                tally.compare(event_value(event, fields[column]), row.get(column))
        tally.unmatched_events += len(set(by_sop) - {
            r.get("sop_instance_uid") for r in rows
        })
    return tally


def verify_event_counts(session, registry, stage_name, modality, n, participants, group):
    """Count reconciliation for stages without a traced field mapping."""
    manifest = aireadi.load_manifest(modality)
    stage_uid = registry.stage(stage_name)
    tally = Tally()

    for tei, pid in sample_participants(group, n, participants):
        expected = len(manifest.get(pid, []))
        actual = len(dhis2.fetch_events(
            session, M.PROGRAM_UID, stage_uid, tei, fields="event"
        ))
        tally.compare(actual, expected)
    return tally


# ---------------------------------------------------------------------------
# Allocation
# ---------------------------------------------------------------------------

def stage_event_count(session, stage_uid):
    """Live event count for a stage. Returns None rather than a silent zero."""
    try:
        payload = dhis2.get_json(session, "tracker/events", {
            "program": M.PROGRAM_UID, "programStage": stage_uid,
            "pageSize": 1, "totalPages": "true",
        })
    except dhis2.Dhis2Error as exc:
        print(f"    count failed for {stage_uid}: {exc}")
        return None
    for path in (("page", "total"), ("pager", "total"), ("total",)):
        node = payload
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, int):
            return node
    print(f"    could not find a total in the count response for {stage_uid}")
    return None


def plan_sample_sizes(session, registry, groups, target_total):
    """Allocate the sample proportionally to live event counts.

    This is the allocation the old docstring described. It was never called,
    because main overrode it with a fixed 500 per group. It is called now, and
    a group whose count cannot be established is reported rather than being
    silently allocated zero.
    """
    counts, unknown = {}, []
    for group, stage_names in groups.items():
        total = 0
        for name in stage_names:
            count = stage_event_count(session, registry.stage(name))
            if count is None:
                unknown.append(group)
                break
            total += count
        else:
            counts[group] = total
            print(f"  {group}: {total} events")

    if unknown:
        return None, counts, unknown

    grand = sum(counts.values())
    if not grand:
        return None, counts, list(groups)
    sizes = {g: max(1, round(target_total * c / grand)) for g, c in counts.items()}
    return sizes, counts, []


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--fixed-n", type=int,
                        help="participants per group, skipping the live count queries")
    parser.add_argument("--target-total", type=int, default=TOTAL_SAMPLE_TARGET)
    parser.add_argument("--fresh", action="store_true", help="ignore the checkpoint")
    args, _ = parser.parse_known_args()

    session = dhis2.get_session(read_only=True)
    registry = M.load(session)

    groups = {
        "wearable": [s for _, _, s in CONTINUOUS_WEARABLE.values()],
        "glucose": ["CGM - Glucose"],
        "environmental": [
            "Environment - PM1", "Environment - PM2.5", "Environment - PM4",
            "Environment - PM10", "Environment - Humidity",
            "Environment - Temperature", "Environment - VOC", "Environment - NOx",
        ],
        "diagnosis": ["Diagnosis History"],
        "retinal_photography": ["Retinal Photography"],
        "retinal_octa": ["Retinal OCTA"],
        "ecg": ["Cardiac - 12-Lead ECG"],
    }

    participants = get_participants(session)
    print(f"Participants in the registry: {len(participants)}")

    if args.fixed_n:
        sizes = {g: args.fixed_n for g in groups}
        allocation = f"fixed {args.fixed_n} participants per group"
        counts, unknown = {}, []
    else:
        print("\nCounting live events per group for proportional allocation")
        sizes, counts, unknown = plan_sample_sizes(
            session, registry, groups, args.target_total
        )
        if sizes is None:
            fallback = max(1, args.target_total // len(groups))
            print(f"  counts unavailable for {unknown}, falling back to "
                  f"{fallback} per group")
            sizes = {g: fallback for g in groups}
            allocation = f"fixed {fallback} per group, live counts unavailable"
        else:
            allocation = (f"proportional to live event counts, "
                          f"target {args.target_total} participants overall")

    checkpoint = {"results": {}}
    if os.path.exists(CHECKPOINT_FILE) and not args.fresh:
        with open(CHECKPOINT_FILE) as fh:
            checkpoint = json.load(fh)
        checkpoint.setdefault("results", {})

    verifiers = {
        "wearable": lambda n: verify_wearable(session, registry, n, participants),
        "glucose": lambda n: verify_glucose(session, registry, n, participants),
        "environmental": lambda n: verify_environmental(session, registry, n, participants),
        "diagnosis": lambda n: verify_diagnosis(session, registry, n, participants),
        "retinal_photography": lambda n: verify_retinal_photography(
            session, registry, n, participants),
        "retinal_octa": lambda n: verify_event_counts(
            session, registry, "Retinal OCTA", "retinal_octa", n,
            participants, "retinal_octa"),
        "ecg": lambda n: verify_event_counts(
            session, registry, "Cardiac - 12-Lead ECG", "cardiac_ecg", n,
            participants, "ecg"),
    }

    start = time.time()
    for group, size in sizes.items():
        if group in checkpoint["results"]:
            print(f"[resume] {group} already done")
            continue
        print(f"\n[run] {group}, n={size}")
        tally = verifiers[group](size)
        result = tally.as_dict()
        result["participants_sampled"] = size
        result["seed"] = group_seed(group)
        checkpoint["results"][group] = result
        with open(CHECKPOINT_FILE, "w") as fh:
            json.dump(checkpoint, fh, indent=2)
        print(f"[done] {group}: {result}")

    results = checkpoint["results"]
    compared = sum(r["values_compared"] for r in results.values())
    matched = sum(r["matched"] for r in results.values())
    discrepancies = sum(r["discrepancies"] for r in results.values())

    print("\n" + "=" * 72)
    print("SAMPLED VERIFICATION")
    print(f"  method: stratified random sampling without replacement, "
          f"{allocation},")
    print(f"          independent seed per group derived from base seed {BASE_SEED}")
    print(f"  elapsed: {time.time() - start:.0f}s")
    print()
    print(f"  {'group':<22}{'compared':>10}{'matched':>10}{'mismatch':>10}"
          f"{'no source':>11}{'no event':>10}{'rate':>9}")
    for group, r in results.items():
        rate = f"{r['match_rate_pct']:.2f}%" if r["match_rate_pct"] is not None else "n/a"
        print(f"  {group:<22}{r['values_compared']:>10}{r['matched']:>10}"
              f"{r['mismatched']:>10}{r['events_with_no_source_hour']:>11}"
              f"{r['source_hours_with_no_event']:>10}{rate:>9}")
    print()
    print(f"  Values compared: {compared}")
    print(f"  Discrepancies:   {discrepancies}")
    if compared:
        print(f"  Match rate:      {100 * matched / compared:.4f}%")

    # H-12: reported next to the sampled result, never merged into it.
    print("\nPRIOR CORE REGISTRY RESULT, not re-verified by this run")
    print(f"  values: {PRIOR_CORE_REGISTRY['values']}")
    print(f"  discrepancies: {PRIOR_CORE_REGISTRY['discrepancies']}")
    print(f"  method: {PRIOR_CORE_REGISTRY['method']}")
    print("  This figure comes from an earlier phase. Do not add it to the "
          "sampled totals above: doing so folds an assumed 100% into a "
          "measured rate.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
