"""Shared CV helpers: bbox math, image decode, projection profiles."""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass(frozen=True)
class BBox:
    """Pixel-coordinate bounding box. left/top inclusive, right/bottom exclusive."""
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def area(self) -> int:
        return max(0, self.width) * max(0, self.height)

    @property
    def aspect(self) -> float:
        return self.width / max(1, self.height)


@dataclass(frozen=True)
class CropResult:
    """Output of a per-capture detection: the crop percentages and how we got them."""
    crop_pct: dict     # {left_pct, top_pct, right_pct, bottom_pct}
    components: dict   # {component_id: BBox}  — for audit/debug


def decode_image(png_bytes: bytes) -> np.ndarray | None:
    """PNG/JPEG bytes -> BGR uint8 ndarray. Returns None on failure."""
    if not png_bytes:
        return None
    arr = np.frombuffer(png_bytes, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None or img.size == 0:
        return None
    return img


def union_bboxes(boxes: list[BBox]) -> BBox:
    """Smallest axis-aligned box containing every input box."""
    if not boxes:
        raise ValueError("union_bboxes: no boxes")
    return BBox(
        left=min(b.left for b in boxes),
        top=min(b.top for b in boxes),
        right=max(b.right for b in boxes),
        bottom=max(b.bottom for b in boxes),
    )


def bbox_to_crop_pct(bbox: BBox, img_w: int, img_h: int) -> dict:
    """Pixel bbox -> crop percentages matching presets.Preset.crop schema."""
    left_pct = max(0.0, bbox.left / img_w * 100)
    top_pct = max(0.0, bbox.top / img_h * 100)
    right_pct = max(0.0, (img_w - bbox.right) / img_w * 100)
    bottom_pct = max(0.0, (img_h - bbox.bottom) / img_h * 100)
    return {
        "left_pct": round(left_pct, 3),
        "top_pct": round(top_pct, 3),
        "right_pct": round(right_pct, 3),
        "bottom_pct": round(bottom_pct, 3),
    }


def column_variance(img: np.ndarray) -> np.ndarray:
    """Per-column std-dev across rows. Captures 'how much stuff is in this column'."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return gray.std(axis=0)


def row_variance(img: np.ndarray, x_slice: slice | None = None) -> np.ndarray:
    """Per-row std-dev within an optional x slice."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    region = gray[:, x_slice] if x_slice is not None else gray
    return region.std(axis=1)


def smooth_1d(arr: np.ndarray, window: int) -> np.ndarray:
    """Moving-average smoothing, same length as input."""
    if window <= 1:
        return arr
    kernel = np.ones(window) / window
    return np.convolve(arr, kernel, mode="same")


def find_high_band(profile: np.ndarray, threshold: float) -> tuple[int, int] | None:
    """Indices [start, end) of the largest contiguous run above `threshold`."""
    above = profile > threshold
    if not above.any():
        return None
    diff = np.diff(above.astype(np.int8))
    starts = list(np.where(diff == 1)[0] + 1)
    ends = list(np.where(diff == -1)[0] + 1)
    if above[0]:
        starts.insert(0, 0)
    if above[-1]:
        ends.append(len(profile))
    if not starts or not ends:
        return None
    best = max(zip(starts, ends), key=lambda se: se[1] - se[0])
    return int(best[0]), int(best[1])
