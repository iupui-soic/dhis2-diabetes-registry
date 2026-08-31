"""DICOM to JPEG preview conversion.

One definition, imported everywhere. It was previously copied verbatim into
four files, and they diverged: only the backfill ever learned to handle the
YBR colorspace, so re-running either import script would have reintroduced
the teal and pink cast on the 37,374 iCare and Optomed images it had just
fixed. That is M-06.
"""

import sys
from io import BytesIO

import numpy as np
from PIL import Image

try:
    import pydicom
    from pydicom.pixel_data_handlers.util import convert_color_space
except ImportError:
    sys.exit(
        "pydicom is required.\n"
        "    pip install -r requirements.txt\n"
        "Note that pylibjpeg and pylibjpeg-libjpeg are also required: without "
        "them the Heidelberg Spectralis files fail to decode, which is what "
        "left 4,680 previews empty on the first full import."
    )

MAX_DIMENSION = 800
JPEG_QUALITY = 85


def to_preview_array(ds):
    """Normalised 8-bit array from a DICOM dataset.

    Handles multi-frame volumes by taking the middle frame, converts YBR to
    RGB, applies rescale slope and intercept, and stretches to 0-255.
    """
    pixels = ds.pixel_array

    frames = int(getattr(ds, "NumberOfFrames", 1) or 1)
    if pixels.ndim >= 3 and frames > 1:
        frame = pixels[pixels.shape[0] // 2]
    else:
        frame = pixels

    # iCare Eidon and Optomed Aurora store YBR_FULL_422. Treating that as RGB
    # is what produced the teal and pink cast.
    photometric = str(getattr(ds, "PhotometricInterpretation", ""))
    if photometric.startswith("YBR"):
        frame = convert_color_space(frame, photometric, "RGB")

    frame = frame.astype(np.float64)
    frame = frame * float(getattr(ds, "RescaleSlope", 1)) + float(
        getattr(ds, "RescaleIntercept", 0)
    )

    low, high = frame.min(), frame.max()
    if high > low:
        frame = (frame - low) / (high - low) * 255.0
    else:
        frame = np.zeros_like(frame)
    return frame.astype(np.uint8)


def to_image(ds):
    """PIL image, resized so the longest side is MAX_DIMENSION."""
    arr = to_preview_array(ds)
    if arr.ndim == 2:
        img = Image.fromarray(arr).convert("L")
    elif arr.ndim == 3 and arr.shape[-1] == 3:
        img = Image.fromarray(arr).convert("RGB")
    else:
        img = Image.fromarray(np.squeeze(arr)).convert("L")
    img.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.LANCZOS)
    return img


def to_jpeg_bytes(path):
    """Convert a DICOM file to JPEG bytes. Returns (bytes, None) or (None, reason)."""
    try:
        ds = pydicom.dcmread(path)
        buf = BytesIO()
        to_image(ds).save(buf, "JPEG", quality=JPEG_QUALITY)
        return buf.getvalue(), None
    except Exception as exc:
        return None, str(exc)
