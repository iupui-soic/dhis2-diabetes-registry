#!/usr/bin/env python3
"""
survey_color_encoding.py

Read-only survey: samples DICOM files across every manufacturer/imaging
type combination in both manifests and tallies PhotometricInterpretation
values, to see how widespread the YBR colorspace issue is (vs isolated to
one manufacturer like iCare Eidon).

SAFE to run WHILE retinal_step2_import_full.py is running -- this script
only reads local files, never touches DHIS2, never writes anything.

USAGE:
    python3 survey_color_encoding.py
"""

import os
import sys
from collections import Counter, defaultdict

import pandas as pd
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import aireadi, dicom  # noqa: E402  (reports a clear error if pydicom is absent)

import pydicom  # noqa: E402


SAMPLES_PER_GROUP = 3  # how many files to check per manufacturer+model+imaging combo


def sample_and_check(df, filepath_col, label, modality):
    print(f"\n{'=' * 70}")
    print(f"{label}")
    print('=' * 70)

    if "manufacturer" not in df.columns:
        print("  [skip] no 'manufacturer' column in this manifest")
        return

    group_cols = [c for c in ["manufacturer", "manufacturers_model_name", "imaging"] if c in df.columns]
    grouped = df.groupby(group_cols)

    results = defaultdict(Counter)

    for group_key, group_df in grouped:
        sample = group_df.head(SAMPLES_PER_GROUP)
        photometric_counts = Counter()
        checked = 0
        for _, row in sample.iterrows():
            rel_path = row.get(filepath_col)
            if pd.isna(rel_path) or str(rel_path).strip().lower() == "not reported":
                continue
            full_path = aireadi.resolve(modality, rel_path)
            if full_path is None:
                continue
            try:
                ds = pydicom.dcmread(full_path, stop_before_pixels=True)
                pi = getattr(ds, "PhotometricInterpretation", "UNKNOWN")
                photometric_counts[pi] += 1
                checked += 1
            except Exception as e:
                photometric_counts[f"ERROR: {e}"] += 1

        group_label = " / ".join(str(g) for g in group_key)
        total_in_group = len(group_df)
        print(f"\n  {group_label}  ({total_in_group} total files, {checked} sampled)")
        for pi, count in photometric_counts.items():
            flag = "  <-- YBR (likely needs colorspace fix)" if "YBR" in str(pi) else ""
            print(f"    {pi}: {count}{flag}")


print("Reading manifests (read-only, safe alongside the running import)...")

photo_df = pd.read_csv(aireadi.manifest_path("retinal_photography"), sep="\t")
sample_and_check(photo_df, "filepath", "RETINAL PHOTOGRAPHY", "retinal_photography")

octa_df = pd.read_csv(aireadi.manifest_path("retinal_octa"), sep="\t")
sample_and_check(octa_df, "associated_enface_1_file_path", "OCTA ENFACE (slot 1 sample)", "retinal_octa")

print("\nDone. Any 'YBR*' PhotometricInterpretation values above need a colorspace")
print("fix in the conversion script. If only iCare Eidon shows YBR, the fix is")
print("scoped to that manufacturer; if multiple manufacturers show it, the fix")
print("should apply broadly (e.g. always convert YBR->RGB when detected).")
