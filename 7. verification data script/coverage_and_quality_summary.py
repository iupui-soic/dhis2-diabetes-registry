#!/usr/bin/env python3
"""Aggregate device coverage, data sufficiency, HbA1c and burden figures.

READ-ONLY against DHIS2. GET requests only. Writes only aggregate statistics
(counts, medians, percentiles), never a participant-level row, to
results/coverage_and_quality_<date>.json.

What it answers, and for whom:
  * How reliable are consumer-device data?  Share of hourly wearable events
    whose Data Sufficiency status is SUFFICIENT / LIMITED / NO_VALID_DATA, per
    stream; share of participants whose CGM hourly coverage over their wear
    span reaches 70% (the completeness threshold of Kok et al. 2026).
  * How much data does a participant yield without a CHW?  Distinct days with
    CGM data, hours with heart-rate data, activity and environment duration
    days, and the share of participants with >= 7 and >= 10 days on each.
  * What is the HbA1c spread?  SD overall and by diabetes severity group,
    for power calculations.

USAGE
    python3 "7. verification data script/coverage_and_quality_summary.py"
"""

import datetime as dt
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import dhis2  # noqa: E402
from common import metadata_uids as M  # noqa: E402

SUFFICIENCY = {
    "Wearable - Heart Rate": "HR Data Sufficiency",
    "Wearable - Respiratory Rate": "RR Data Sufficiency",
    "Wearable - SpO2": "SpO2 Data Sufficiency",
}
STATUSES = ("SUFFICIENT", "LIMITED", "NO_VALID_DATA")


def pct(part, whole):
    return round(100.0 * part / whole, 2) if whole else None


def quantiles(values):
    if not values:
        return None
    v = sorted(values)
    q = statistics.quantiles(v, n=4) if len(v) > 1 else [v[0]] * 3
    return {"n": len(v), "min": v[0], "q1": round(q[0], 2), "median": round(statistics.median(v), 2),
            "q3": round(q[2], 2), "max": v[-1], "mean": round(statistics.mean(v), 2),
            "sd": round(statistics.stdev(v), 3) if len(v) > 1 else None}


def count_events(session, stage_uid, extra=None):
    params = {"program": M.PROGRAM_UID, "programStage": stage_uid, "pageSize": 1, "totalPages": "true"}
    if extra:
        params.update(extra)
    payload = dhis2.get_json(session, "tracker/events", params)
    for path in (("page", "total"), ("pager", "total"), ("total",)):
        node = payload
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, int):
            return node
    raise RuntimeError("no total in count response")


def main():
    session = dhis2.get_session(read_only=True)
    registry = M.load(session)
    start = time.time()
    out = {"run_date": dt.date.today().isoformat(), "method": "read-only tracker queries; aggregate statistics only"}

    # 1. Data sufficiency per stream, by server-side count with a data value filter.
    suff = {}
    for stage_name, de_name in SUFFICIENCY.items():
        stage_uid = registry.stage(stage_name)
        de_uid = registry.data_element(de_name)
        total = count_events(session, stage_uid)
        counts = {s: count_events(session, stage_uid, {"filter": f"{de_uid}:eq:{s}"}) for s in STATUSES}
        suff[stage_name] = {"hourly_events": total, **{s: counts[s] for s in STATUSES},
                            **{f"{s}_pct": pct(counts[s], total) for s in STATUSES},
                            "unlabelled": total - sum(counts.values())}
        print(stage_name, suff[stage_name], flush=True)
    out["data_sufficiency_by_stream"] = suff

    # 2. Per-participant coverage from the summary stages (small, one fetch each).
    def stage_values(stage_name, de_names):
        uids = {n: registry.data_element(n) for n in de_names}
        events = dhis2.fetch_all_pages(
            session, "tracker/events",
            {"program": M.PROGRAM_UID, "programStage": registry.stage(stage_name),
             "fields": "trackedEntity,dataValues[dataElement,value]"},
            ("events", "instances"))
        rows = {}
        for ev in events:
            vals = {dv["dataElement"]: dv.get("value") for dv in ev.get("dataValues", [])}
            rows[ev["trackedEntity"]] = {n: vals.get(u) for n, u in uids.items()}
        return rows

    def numeric(rows, key):
        vals = []
        for r in rows.values():
            try:
                vals.append(float(r[key]))
            except (TypeError, ValueError):
                pass
        return vals

    cgm_summary = stage_values("CGM Summary", ["Sensor Wear Duration (days)"])
    wear_days = numeric(cgm_summary, "Sensor Wear Duration (days)")
    device_meta = stage_values("Device & Sensor Metadata", ["act_duration_days", "env_duration_days"])
    act_days = numeric(device_meta, "act_duration_days")
    env_days = numeric(device_meta, "env_duration_days")
    out["cgm_sensor_wear_days"] = {**quantiles(wear_days),
                                   "ge_7_days_pct": pct(sum(d >= 7 for d in wear_days), len(wear_days)),
                                   "ge_10_days_pct": pct(sum(d >= 10 for d in wear_days), len(wear_days))}
    out["activity_monitor_days"] = {**quantiles(act_days),
                                    "ge_7_days_pct": pct(sum(d >= 7 for d in act_days), len(act_days)),
                                    "ge_10_days_pct": pct(sum(d >= 10 for d in act_days), len(act_days))}
    out["environment_sensor_days"] = {**quantiles(env_days),
                                      "ge_7_days_pct": pct(sum(d >= 7 for d in env_days), len(env_days)),
                                      "ge_10_days_pct": pct(sum(d >= 10 for d in env_days), len(env_days))}
    print("summary stages done", flush=True)

    # 3. HbA1c spread, overall and by severity group (attribute on the tracked entity).
    tes = dhis2.fetch_all_pages(
        session, "tracker/trackedEntities",
        {"program": M.PROGRAM_UID, "fields": "trackedEntity,attributes[displayName,value]"},
        ("trackedEntities", "instances"))
    severity = {}
    for te in tes:
        for a in te.get("attributes", []):
            if "severity" in a.get("displayName", "").lower():
                severity[te["trackedEntity"]] = a.get("value")
    labs = stage_values("Clinical Laboratory & Assessment Measurements", ["import_hba1c"])
    hba1c_all, by_group = [], {}
    for tei, r in labs.items():
        try:
            v = float(r["import_hba1c"])
        except (TypeError, ValueError):
            continue
        if not (3.0 <= v <= 20.0):
            continue
        hba1c_all.append(v)
        by_group.setdefault(severity.get(tei, "unknown"), []).append(v)
    out["hba1c_percent"] = {"overall": quantiles(hba1c_all),
                            "by_severity_group": {g: quantiles(v) for g, v in sorted(by_group.items())}}
    print("hba1c done", out["hba1c_percent"]["overall"], flush=True)

    # 4. Per-participant CGM and heart-rate coverage from the hourly stages.
    cgm_stage = registry.stage("CGM - Glucose")
    hr_stage = registry.stage("Wearable - Heart Rate")
    cgm_days, cgm_completeness, hr_hours, hr_days = [], [], [], []
    all_three = 0
    per_te_days = {}
    for i, te in enumerate(tes, start=1):
        tei = te["trackedEntity"]
        ev = dhis2.fetch_events(session, M.PROGRAM_UID, cgm_stage, tei, fields="occurredAt")
        hours = sorted({e["occurredAt"][:13] for e in ev if e.get("occurredAt")})
        days = {h[:10] for h in hours}
        if hours:
            first = dt.datetime.strptime(hours[0][:10], "%Y-%m-%d")
            last = dt.datetime.strptime(hours[-1][:10], "%Y-%m-%d")
            span_hours = ((last - first).days + 1) * 24
            cgm_days.append(len(days))
            cgm_completeness.append(100.0 * len(hours) / span_hours)
        ev = dhis2.fetch_events(session, M.PROGRAM_UID, hr_stage, tei, fields="occurredAt")
        hrh = {e["occurredAt"][:13] for e in ev if e.get("occurredAt")}
        if hrh:
            hr_hours.append(len(hrh))
            hr_days.append(len({h[:10] for h in hrh}))
        per_te_days[tei] = (len(days), len({h[:10] for h in hrh}))
        if i % 200 == 0:
            print(f"  coverage {i}/{len(tes)}", flush=True)
    env_by_te = {tei: r for tei, r in device_meta.items()}
    for tei, (cd, hd) in per_te_days.items():
        try:
            ed = float(env_by_te.get(tei, {}).get("env_duration_days"))
        except (TypeError, ValueError):
            ed = 0
        if cd >= 7 and hd >= 7 and ed >= 7:
            all_three += 1
    out["cgm_hourly_coverage"] = {
        "participants_with_cgm": len(cgm_days),
        "distinct_days_with_data": quantiles(cgm_days),
        "ge_7_days_pct": pct(sum(d >= 7 for d in cgm_days), len(cgm_days)),
        "ge_10_days_pct": pct(sum(d >= 10 for d in cgm_days), len(cgm_days)),
        "hourly_completeness_over_span_pct": quantiles(cgm_completeness),
        "ge_70pct_complete_pct": pct(sum(c >= 70 for c in cgm_completeness), len(cgm_completeness)),
    }
    out["heart_rate_hourly_coverage"] = {
        "participants_with_hr": len(hr_hours),
        "hours_with_data": quantiles(hr_hours),
        "distinct_days_with_data": quantiles(hr_days),
        "ge_7_days_pct": pct(sum(d >= 7 for d in hr_days), len(hr_days)),
    }
    out["burden_all_three_streams_ge_7_days"] = {
        "participants": all_three, "of": len(tes), "pct": pct(all_three, len(tes)),
        "definition": ">= 7 distinct days with CGM hourly events, >= 7 with heart-rate hourly events, env_duration_days >= 7"}
    out["elapsed_seconds"] = round(time.time() - start)

    os.makedirs("results", exist_ok=True)
    path = os.path.join("results", f"coverage_and_quality_{out['run_date']}.json")
    with open(path, "w") as fh:
        json.dump(out, fh, indent=1)
    print(json.dumps(out, indent=1))
    print("written", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
