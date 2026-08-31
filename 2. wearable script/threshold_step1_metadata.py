#!/usr/bin/env python3
"""Create the threshold, status, SD and timestamp fields for HR, RR and SpO2.

Thresholds and their sources:
  Heart Rate        <60 / 60-100 / >100 bpm
                    American Heart Association, normal resting HR 60-100
  Respiratory Rate  <12 / 12-20 / >20 breaths/min
                    American Lung Association, normal adult resting RR 12-20
  SpO2              95-100 normal / 90-94 mild low / <90 marked low
                    WHO hypoxemia threshold

Data Sufficiency is a project design decision, not a clinical standard:
  0 valid readings -> No valid data, 1-2 -> Limited, 3+ -> Sufficient

WHAT THE AUDIT FOUND (C-01, H-02, H-07), and what changed
----------------------------------------------------------
1. H-02 as originally reported was WRONG, and this is corrected here.
   "Insufficient data" is defined in both the HR/RR and the SpO2 option set,
   and the concern was that creating options standalone and resolving them by
   a global name lookup would make the second set adopt the first set's
   option. Checked against the live server: DHIS2 2.44 holds them as two
   separate objects (jhiZJdtZDFy and bgaPE4PheLm) with the same name and the
   same code, and both sets are intact. The display names are therefore left
   as they were. Options are still created nested inside their set rather
   than standalone, which is more robust, and codes stay plain so they match
   what the instance already uses.

2. H-07: neither the create response nor the lookup result was checked, so a
   failed create produced None UIDs that were then written into a stage. Every
   create now verifies that what it asked for exists.

3. This module also exports the option set names, thresholds and field names
   that threshold_step2_backfill.py imports, so the metadata definition and
   the import can no longer drift apart.

USAGE
-----
    python3 "2. wearable script/threshold_step1_metadata.py"
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import dhis2  # noqa: E402
from common import metadata_uids as M  # noqa: E402

# ---------------------------------------------------------------------------
# Shared definitions. threshold_step2_backfill.py imports these.
# ---------------------------------------------------------------------------

SUFFICIENCY_SET = "Data Sufficiency"
HR_RR_STATUS_SET = "HR RR Hourly Threshold Status"
SPO2_STATUS_SET = "SpO2 Hourly Threshold Status"

SUFFICIENCY_OPTIONS = ["Sufficient", "Limited", "No valid data"]

# "Insufficient data" deliberately appears in both sets. Verified safe: the
# server holds them as two distinct options. See H-02 above.
HR_RR_STATUS_OPTIONS = [
    "Within range",
    "Low readings present",
    "High readings present",
    "Both low and high readings present",
    "Insufficient data",
]
SPO2_STATUS_OPTIONS = [
    "Expected range only",
    "Mild-low readings present",
    "Marked-low readings present",
    "Both mild-low and marked-low readings present",
    "Insufficient data",
]

HR_THRESHOLDS = (60, 100)
RR_THRESHOLDS = (12, 20)
SPO2_MILD_LOW = 95      # 90 to below 95 is mild low
SPO2_MARKED_LOW = 90    # below 90 is marked low

FIELD_NAMES = {
    "HR": {
        "sd": "HR Standard Deviation",
        "low_count": "HR Low Reading Count (<60)",
        "high_count": "HR High Reading Count (>100)",
        "status": "HR Hourly Status",
        "sufficiency": "HR Data Sufficiency",
        "low_ts": "HR Low Reading Timestamps",
        "high_ts": "HR High Reading Timestamps",
    },
    "RR": {
        "sd": "RR Standard Deviation",
        "low_count": "RR Low Reading Count (<12)",
        "high_count": "RR High Reading Count (>20)",
        "status": "RR Hourly Status",
        "sufficiency": "RR Data Sufficiency",
        "low_ts": "RR Low Reading Timestamps",
        "high_ts": "RR High Reading Timestamps",
    },
    "SPO2": {
        "sd": "SpO2 Standard Deviation",
        "mild_low_count": "SpO2 Mild Low Count (90-94)",
        "marked_low_count": "SpO2 Marked Low Count (<90)",
        "status": "SpO2 Hourly Status",
        "sufficiency": "SpO2 Data Sufficiency",
        "mild_low_ts": "SpO2 Mild Low Reading Timestamps",
        "marked_low_ts": "SpO2 Marked Low Reading Timestamps",
    },
}

VALUE_TYPES = {
    "sd": "NUMBER",
    "low_count": "INTEGER", "high_count": "INTEGER",
    "mild_low_count": "INTEGER", "marked_low_count": "INTEGER",
    "status": "TEXT", "sufficiency": "TEXT",
    "low_ts": "LONG_TEXT", "high_ts": "LONG_TEXT",
    "mild_low_ts": "LONG_TEXT", "marked_low_ts": "LONG_TEXT",
}


def field_names_for(metric):
    """{field key: data element name} for one metric."""
    return FIELD_NAMES[metric]


def main():
    session = dhis2.get_session()

    print("Creating option sets")
    sufficiency_uid = dhis2.create_option_set(session, SUFFICIENCY_SET, SUFFICIENCY_OPTIONS)
    hr_rr_uid = dhis2.create_option_set(session, HR_RR_STATUS_SET, HR_RR_STATUS_OPTIONS)
    spo2_uid = dhis2.create_option_set(session, SPO2_STATUS_SET, SPO2_STATUS_OPTIONS)
    print(f"  {SUFFICIENCY_SET}: {sufficiency_uid}")
    print(f"  {HR_RR_STATUS_SET}: {hr_rr_uid}")
    print(f"  {SPO2_STATUS_SET}: {spo2_uid}")

    option_set_for = {
        "status": {"HR": hr_rr_uid, "RR": hr_rr_uid, "SPO2": spo2_uid},
        "sufficiency": {"HR": sufficiency_uid, "RR": sufficiency_uid, "SPO2": sufficiency_uid},
    }

    for metric, stage_uid in M.THRESHOLD_STAGE_UIDS.items():
        print(f"\nCreating fields for {metric}")
        defs = []
        for key, name in FIELD_NAMES[metric].items():
            defs.append({
                "name": name,
                "shortName": name[:50],
                "valueType": VALUE_TYPES[key],
                "aggregationType": "SUM" if VALUE_TYPES[key] == "INTEGER" else "NONE",
                "optionSet": option_set_for.get(key, {}).get(metric),
            })
        uids = dhis2.create_data_elements(session, defs)
        for name, uid in uids.items():
            print(f"  {name}: {uid}")

        ordered = [uids[FIELD_NAMES[metric][k]] for k in FIELD_NAMES[metric]]
        added = dhis2.attach_data_elements(session, stage_uid, ordered)
        print(f"  attached {added} new field(s) to stage {stage_uid}")

    print("\nDone. threshold_step2_backfill.py resolves these by name, so there "
          "is nothing to paste anywhere.")


if __name__ == "__main__":
    main()
