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

import numpy as np
import pandas as pd
from PIL import Image

try:
    import pydicom
except ImportError:
    sys.exit(
        "pydicom is required. Install it with:\n"
        "    pip install pydicom --user\n"
    )

AI_READI_ROOT = os.path.expanduser("~/AI-READI-fixed")
PHOTOGRAPHY_DIR = os.path.join(AI_READI_ROOT, "retinal_photography")
OCTA_DIR = os.path.join(AI_READI_ROOT, "retinal_octa")

MAX_DIMENSION = 800  # resize longest side to this many pixels
JPEG_QUALITY = 85


def dicom_to_preview_array(ds):
    """
    Extract a normalized 8-bit 2D array from a pydicom Dataset, handling:
      - single-frame vs multi-frame (volumetric) DICOM -> take middle frame
      - rescale slope/intercept if present
      - normalization to 0-255 for JPEG export
    """
    pixel_array = ds.pixel_array

    # Multi-frame (volumetric) DICOM: pixel_array shape is (frames, H, W) or
    # (frames, H, W, channels). Take the middle frame as the representative slice.
    if pixel_array.ndim >= 3 and getattr(ds, "NumberOfFrames", 1) and int(getattr(ds, "NumberOfFrames", 1)) > 1:
        num_frames = pixel_array.shape[0]
        middle_idx = num_frames // 2
        frame = pixel_array[middle_idx]
    else:
        frame = pixel_array

    frame = frame.astype(np.float64)

    # Apply DICOM rescale slope/intercept if present (common for OCT/OCTA)
    slope = float(getattr(ds, "RescaleSlope", 1))
    intercept = float(getattr(ds, "RescaleIntercept", 0))
    frame = frame * slope + intercept

    # Normalize to 0-255 based on this frame's own min/max
    fmin, fmax = frame.min(), frame.max()
    if fmax > fmin:
        frame = (frame - fmin) / (fmax - fmin) * 255.0
    else:
        frame = np.zeros_like(frame)

    frame = frame.astype(np.uint8)
    return frame


def save_preview(ds, out_path):
    arr = dicom_to_preview_array(ds)

    if arr.ndim == 2:
        img = Image.fromarray(arr).convert("L")  # grayscale
    elif arr.ndim == 3 and arr.shape[-1] == 3:
        img = Image.fromarray(arr).convert("RGB")
    else:
        # Fallback: squeeze/convert whatever shape shows up
        img = Image.fromarray(np.squeeze(arr)).convert("L")

    # Resize keeping aspect ratio, longest side = MAX_DIMENSION
    img.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.LANCZOS)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "JPEG", quality=JPEG_QUALITY)
    return out_path, img.size


def process_manifest(manifest_path, filepath_col, person_id, out_dir, label, limit=None):
    print(f"\n=== {label} ===")
    if not os.path.exists(manifest_path):
        print(f"  [skip] manifest not found: {manifest_path}")
        return

    df = pd.read_csv(manifest_path, sep="\t")
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
        full_path = os.path.join(AI_READI_ROOT, rel_path.lstrip("/"))
        if not os.path.exists(full_path):
            print(f"  [warn] file not found, skipping: {full_path}")
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
        manifest_path=os.path.join(PHOTOGRAPHY_DIR, "manifest.tsv"),
        filepath_col="filepath",
        person_id=args.person_id,
        out_dir=args.out_dir,
        label="retinal_photography",
        limit=args.limit_per_category,
    )

    process_manifest(
        manifest_path=os.path.join(OCTA_DIR, "manifest.tsv"),
        filepath_col="associated_enface_1_file_path",
        person_id=args.person_id,
        out_dir=args.out_dir,
        label="retinal_octa_enface",
        limit=args.limit_per_category,
    )

    print("\nAll done. Open the images in the output directory and visually confirm:")
    print("  - photography images look like normal retinal photos (not black/washed out)")
    print("  - OCTA flow_cube middle-slice previews look like a plausible single frame")
    print("If they look right, we'll design the DHIS2 stages/data elements next.")


if __name__ == "__main__":
    main()
