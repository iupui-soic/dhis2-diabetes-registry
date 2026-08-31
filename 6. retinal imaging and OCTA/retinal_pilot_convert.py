#!/usr/bin/env python3
"""
retinal_pilot_convert.py

STEP 0 of the retinal imaging integration: convert ONE participant's DICOM
images (retinal_photography + retinal_octa) into resized preview JPEGs, so
you can visually confirm the conversion looks correct BEFORE we design the
DHIS2 stages/data elements around it.

This does NOT touch DHIS2 at all. Pure local file conversion + QA.

Follows the project's established pattern: always test on one participant,
verify, THEN design the full pipeline.

USAGE (on JupyterHub, in a terminal or notebook):

    pip install pydicom pillow numpy --user   # if not already installed
    python3 retinal_pilot_convert.py --person-id 1072 --out-dir ./pilot_preview

Then look at the images in ./pilot_preview and confirm:
  - photography images (ir/cfp/faf) look like normal retinal photos, not
    washed-out/black/inverted
  - octa enface images look like reasonable 2D scans
  - octa flow_cube/segmentation "middle slice" previews look like a sane
    single frame from the volume, not noise
"""

import os
import sys
import argparse
from pathlib import Path

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import aireadi, dicom  # noqa: E402

import pydicom  # noqa: E402  (common.dicom already reported a clear error if absent)

# M-06: the conversion lives in common.dicom now. This file used to carry its
# own copy, which never learned to handle the YBR colorspace, so previews
# generated here would have looked different from the ones the import wrote.

MAX_DIMENSION = dicom.MAX_DIMENSION
JPEG_QUALITY = dicom.JPEG_QUALITY


def save_preview(ds, out_path):
    """Convert and write one preview, using the shared conversion."""
    img = dicom.to_image(ds)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "JPEG", quality=JPEG_QUALITY)
    return out_path, img.size


def process_manifest(manifest_path, filepath_col, person_id, out_dir, label, modality, limit=None):
    print(f"\n=== {label} ===")
    try:
        resolved_manifest = aireadi.manifest_path(manifest_path)
    except FileNotFoundError as exc:
        print(f"  [skip] {exc}")
        return

    df = pd.read_csv(resolved_manifest, sep="\t")
    df["person_id"] = df["person_id"].astype(str)
    rows = df[df["person_id"] == str(person_id)]
    print(f"  {len(rows)} rows for person_id={person_id}")

    if limit:
        rows = rows.head(limit)

    converted = 0
    failed = 0
    skipped = 0
    for i, row in rows.iterrows():
        rel_path = row[filepath_col]
        if pd.isna(rel_path) or str(rel_path).strip().lower() == "not reported":
            skipped += 1
            continue
        # manifest paths are stored with a leading slash relative to AI_READI_ROOT
        full_path = aireadi.resolve(modality, rel_path)
        if full_path is None:
            print(f"  [warn] file not found, skipping: {rel_path}")
            failed += 1
            continue
        try:
            ds = pydicom.dcmread(full_path)
            out_name = Path(full_path).stem + ".jpg"
            out_path = Path(out_dir) / label / out_name
            saved_path, size = save_preview(ds, out_path)
            converted += 1
            if converted <= 5:
                print(f"  [ok] {Path(full_path).name} -> {saved_path.name} ({size[0]}x{size[1]})")
        except Exception as e:
            print(f"  [error] {Path(full_path).name}: {e}")
            failed += 1

    print(f"  Done: {converted} converted, {failed} failed, {skipped} skipped (no file reported), out of {len(rows)}")


def main():
    parser = argparse.ArgumentParser(description="Pilot DICOM->JPEG preview conversion for one participant")
    parser.add_argument("--person-id", required=True, help="AI-READI person_id, e.g. 1072")
    parser.add_argument("--out-dir", default="./pilot_preview")
    parser.add_argument("--limit-per-category", type=int, default=None,
                         help="Optional cap on number of files converted per manifest, for a quick smoke test")
    args, unknown = parser.parse_known_args()  # tolerate Jupyter's injected -f kernel arg
    if unknown:
        print(f"[info] Ignoring unrecognized arguments (likely Jupyter kernel args): {unknown}")

    print(f"Pilot conversion for person_id={args.person_id}")
    print(f"Output directory: {args.out_dir}")

    process_manifest(
        manifest_path="retinal_photography",
        filepath_col="filepath",
        person_id=args.person_id,
        out_dir=args.out_dir,
        label="retinal_photography",
        modality="retinal_photography",
        limit=args.limit_per_category,
    )

    process_manifest(
        manifest_path="retinal_octa",
        filepath_col="associated_enface_1_file_path",
        person_id=args.person_id,
        out_dir=args.out_dir,
        label="retinal_octa_enface",
        modality="retinal_octa",
        limit=args.limit_per_category,
    )

    print("\nAll done. Open the images in the output directory and visually confirm:")
    print("  - photography images look like normal retinal photos (not black/washed out)")
    print("  - OCTA flow_cube middle-slice previews look like a plausible single frame")
    print("If they look right, we'll design the DHIS2 stages/data elements next.")


if __name__ == "__main__":
    main()
