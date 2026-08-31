#!/usr/bin/env python3
"""
diagnose_heidelberg_ir_artifact.py

Investigates a specific Heidelberg Spectralis IR file that converted
"successfully" (no error logged) but rendered as an odd, noisy, web-like
texture rather than a normal fundus photo -- checking for windowing tags
and pixel value distribution that a plain min-max stretch could be
mishandling.
"""

import os
import sys
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import aireadi, dicom  # noqa: E402  (reports a clear error if pydicom is absent)

import pydicom  # noqa: E402


TARGET_FILE = "retinal_photography/ir/heidelberg_spectralis/1158/1158_spectralis_ppol_mac_hr_oct_ir_l_1.3.6.1.4.1.33437.11.4.9341142.116784484712349.29141.4.0.0.dcm"

full_path = aireadi.resolve("retinal_photography", TARGET_FILE)

if full_path is None:
    print(f"File not found: {TARGET_FILE}")
else:
    ds = pydicom.dcmread(full_path)
    print(f"File: {full_path}\n")

    print("=== Relevant DICOM tags ===")
    for tag in ["PhotometricInterpretation", "SamplesPerPixel", "BitsAllocated",
                "BitsStored", "HighBit", "PixelRepresentation",
                "RescaleSlope", "RescaleIntercept",
                "WindowCenter", "WindowWidth", "NumberOfFrames"]:
        print(f"  {tag}: {getattr(ds, tag, 'NOT SET')}")

    arr = ds.pixel_array
    print(f"\n=== Pixel array stats ===")
    print(f"  shape: {arr.shape}, dtype: {arr.dtype}")
    print(f"  min: {arr.min()}, max: {arr.max()}, mean: {arr.mean():.2f}, std: {arr.std():.2f}")

    # Check distribution: is most of the data clustered in a narrow range
    # with a few extreme outliers? (this would explain why simple min-max
    # stretching produces a harsh, noisy result -- outliers dominate the
    # stretch and compress everything else into a narrow visible band)
    p1, p50, p99 = np.percentile(arr, [1, 50, 99])
    print(f"  1st percentile: {p1:.1f}, median: {p50:.1f}, 99th percentile: {p99:.1f}")
    print(f"  (if max is much higher than the 99th percentile, a few outlier")
    print(f"   pixels are likely stretching/compressing the visible contrast)")
