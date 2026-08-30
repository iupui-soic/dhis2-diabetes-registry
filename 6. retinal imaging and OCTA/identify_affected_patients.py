#!/usr/bin/env python3
"""
identify_affected_patients.py

Produces three separate lists for reporting purposes:
  1. heidelberg_affected_patients.csv  -- patients with missing preview
     images due to the pylibjpeg dependency gap (now fixed, pending backfill)
  2. corrupted_file_patients.csv       -- patients with genuinely corrupted
     source DICOM files ("broken data stream"), not fixable by us
  3. ybr_affected_patients.csv         -- patients with Optomed Aurora or
     iCare Eidon images showing the teal/pink color cast (fix ready,
     pending backfill)

Sources:
  - Heidelberg + corrupted-file lists are parsed directly from the import
    log (retinal_full_import.log), since both showed up as [error] lines.
  - The YBR list is NOT derivable from the log, because those conversions
    did not error -- they "succeeded" with the wrong colors. This list is
    built instead by scanning the manifest for the affected manufacturers.

USAGE:
    python3 identify_affected_patients.py
    (run from the same folder as retinal_full_import.log and the AI-READI
    manifests -- adjust paths below if needed)
"""

import os
import re
import csv
from collections import defaultdict

import pandas as pd

LOG_FILE = "retinal_full_import.log"
AI_READI_ROOT = os.path.expanduser("~/AI-READI-fixed")
PHOTOGRAPHY_MANIFEST = os.path.join(AI_READI_ROOT, "retinal_photography", "manifest.tsv")

# Manufacturers confirmed via survey_color_encoding.py to use YBR_FULL_422
YBR_AFFECTED_MANUFACTURERS = {"Optomed", "iCare"}


def extract_person_id_from_path(file_path):
    """
    File paths look like:
    .../retinal_photography/ir/heidelberg_spectralis/7767/7767_spectralis_....dcm
    The person_id is the folder name right before the filename.
    """
    match = re.search(r"/(\d+)/[^/]+\.dcm", file_path)
    return match.group(1) if match else None


def parse_log_for_errors(log_path):
    heidelberg_hits = defaultdict(set)  # person_id -> set of file paths
    corrupted_hits = defaultdict(set)
    other_errors = defaultdict(list)

    if not os.path.exists(log_path):
        print(f"WARNING: {log_path} not found. Skipping log-based detection.")
        return heidelberg_hits, corrupted_hits, other_errors

    with open(log_path) as f:
        for line in f:
            if "[error] conversion failed for" not in line:
                continue

            match = re.search(r"conversion failed for (\S+): (.+)", line)
            if not match:
                continue
            file_path, error_msg = match.group(1), match.group(2).strip()
            person_id = extract_person_id_from_path(file_path)
            if not person_id:
                continue

            if "GDCM" in error_msg and "pylibjpeg" in error_msg:
                heidelberg_hits[person_id].add(file_path)
            elif "broken data stream" in error_msg:
                corrupted_hits[person_id].add(file_path)
            else:
                other_errors[person_id].append((file_path, error_msg))

    return heidelberg_hits, corrupted_hits, other_errors


def find_ybr_affected_patients(manifest_path):
    df = pd.read_csv(manifest_path, sep="\t")
    affected = df[df["manufacturer"].isin(YBR_AFFECTED_MANUFACTURERS)]
    # person_id -> count of affected files for that patient
    counts = affected.groupby("person_id").size().to_dict()
    return counts


def write_csv(filename, rows, fieldnames):
    with open(filename, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"  Wrote {len(rows)} rows -> {filename}")


def main():
    print("=== Parsing import log for Heidelberg and corrupted-file errors ===")
    heidelberg_hits, corrupted_hits, other_errors = parse_log_for_errors(LOG_FILE)

    print(f"\nHeidelberg (missing preview, pylibjpeg gap): "
          f"{len(heidelberg_hits)} patients, "
          f"{sum(len(v) for v in heidelberg_hits.values())} files")
    write_csv(
        "heidelberg_affected_patients.csv",
        [{"person_id": pid, "affected_file_count": len(files)}
         for pid, files in sorted(heidelberg_hits.items())],
        ["person_id", "affected_file_count"],
    )

    print(f"\nCorrupted source files (broken data stream, not fixable): "
          f"{len(corrupted_hits)} patients, "
          f"{sum(len(v) for v in corrupted_hits.values())} files")
    write_csv(
        "corrupted_file_patients.csv",
        [{"person_id": pid, "affected_file_count": len(files)}
         for pid, files in sorted(corrupted_hits.items())],
        ["person_id", "affected_file_count"],
    )

    if other_errors:
        print(f"\n[note] {len(other_errors)} patients had OTHER unrecognized error "
              f"types not matching Heidelberg or corrupted-file patterns -- "
              f"worth reviewing manually:")
        for pid, errs in list(other_errors.items())[:5]:
            print(f"    person_id={pid}: {errs[0][1][:100]}")

    print("\n=== Scanning manifest for YBR-affected patients (Optomed + iCare Eidon) ===")
    ybr_counts = find_ybr_affected_patients(PHOTOGRAPHY_MANIFEST)
    print(f"\nYBR color-cast affected: {len(ybr_counts)} patients, "
          f"{sum(ybr_counts.values())} files")
    write_csv(
        "ybr_affected_patients.csv",
        [{"person_id": pid, "affected_file_count": count}
         for pid, count in sorted(ybr_counts.items())],
        ["person_id", "affected_file_count"],
    )

    print("\n=== Summary ===")
    print(f"Heidelberg affected patients:  {len(heidelberg_hits)}")
    print(f"Corrupted-file patients:       {len(corrupted_hits)}")
    print(f"YBR-affected patients:         {len(ybr_counts)}")
    print("\nThree CSV files written to the current directory.")


if __name__ == "__main__":
    main()
