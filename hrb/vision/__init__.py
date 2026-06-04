"""
Per-capture computer-vision crop detection.

A preset can list semantic `components` (e.g. ["video", "author", "caption"]).
At build time, the writer calls `detect_components(png_bytes, platform, ...)`
which dispatches to a per-platform classical-CV detector. The union of detected
component bboxes becomes the crop region.

On any failure (decode error, unsupported platform, low confidence, missing
component) the function returns None and the writer falls back to the preset's
static crop percentages.
"""
from __future__ import annotations

from ._common import BBox, CropResult, bbox_to_crop_pct, decode_image, union_bboxes
from . import facebook, instagram, tiktok


# platform_id -> callable(img: ndarray, post_style: str, requested: list[str]) -> dict[str, BBox] | None
_REGISTRY = {
    "facebook": facebook.detect,
    "instagram": instagram.detect,
    "tiktok": tiktok.detect,
}


def detect_components(
    png_bytes: bytes,
    platform: str,
    post_style: str,
    requested_components: list[str],
) -> dict[str, BBox] | None:
    """Return {component_id: BBox} for each requested component, or None on failure."""
    detector = _REGISTRY.get(platform)
    if detector is None:
        return None
    img = decode_image(png_bytes)
    if img is None:
        return None
    try:
        return detector(img, post_style, requested_components)
    except Exception:
        return None


__all__ = ["BBox", "CropResult", "bbox_to_crop_pct", "detect_components", "union_bboxes"]
