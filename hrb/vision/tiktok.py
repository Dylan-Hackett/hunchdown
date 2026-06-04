"""
TikTok single-post detector.

Strategy (classical CV, no model):
  1. Find the video card: it's a tall ~9:16 rectangle in the central column,
     visually distinct from the white UI background. Edge-detect + contour-find,
     pick the largest contour whose bounding rect has TikTok-video aspect.
  2. Find the author+caption block: starts immediately below the video bottom,
     same horizontal extent. Detect the run of high-variance rows below the
     video; its bottom is where the visible post content ends and the
     comments / suggestions begin.
  3. (Future: separate `author` / `caption` / `comments` components. v1 lumps
     author+caption together because Dylan always wants both.)

Returns None on any failure — caller falls back to the preset's static crop %.
"""
from __future__ import annotations

import cv2
import numpy as np

from ._common import BBox, find_high_band, row_variance, smooth_1d

# Known TikTok video aspect is 9:16 = 0.5625. Allow some slack for rounded
# corners / desktop chrome around the player.
_VIDEO_ASPECT_MIN = 0.40
_VIDEO_ASPECT_MAX = 0.85

# Video has to be a meaningful chunk of the screenshot, otherwise we're
# probably looking at a UI icon, not the post.
_MIN_VIDEO_AREA_FRAC = 0.04
_MIN_VIDEO_WIDTH_PX = 200
_MIN_VIDEO_HEIGHT_PX = 300

# Caption/author band: scan this far below the video for text rows.
_CAPTION_SCAN_HEIGHT_FRAC = 0.6   # of video height

# A row counts as "content" if its grayscale std-dev exceeds this multiple of
# the median std-dev across the scan window.
_CAPTION_VARIANCE_MULT = 1.4


def detect(
    img: np.ndarray,
    post_style: str,
    requested: list[str],
) -> dict[str, BBox] | None:
    """Detect requested TikTok components. Returns None if anything required is missing."""
    h, w = img.shape[:2]

    video = _find_video_bbox(img)
    if video is None:
        return None

    out: dict[str, BBox] = {}
    if "video" in requested:
        out["video"] = video

    needs_caption = "caption" in requested or "author" in requested
    if needs_caption:
        cap = _find_caption_bbox(img, video)
        if cap is None:
            return None
        if "caption" in requested:
            out["caption"] = cap
        if "author" in requested:
            # v1: author and caption are detected as one band — same bbox.
            out["author"] = cap

    # Any requested component we didn't produce → caller can't honor the preset.
    for c in requested:
        if c not in out:
            return None

    return out


def _find_video_bbox(img: np.ndarray) -> BBox | None:
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    # Strong Canny edges, then dilate so the video's outer border closes into
    # a continuous contour rather than dotted segments.
    edges = cv2.Canny(gray, 60, 180)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=2)

    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    candidates: list[tuple[int, BBox]] = []
    img_area = w * h
    for cnt in contours:
        x, y, cw, ch = cv2.boundingRect(cnt)
        if cw < _MIN_VIDEO_WIDTH_PX or ch < _MIN_VIDEO_HEIGHT_PX:
            continue
        if cw * ch < img_area * _MIN_VIDEO_AREA_FRAC:
            continue
        aspect = cw / ch
        if not (_VIDEO_ASPECT_MIN <= aspect <= _VIDEO_ASPECT_MAX):
            continue
        # Must be roughly horizontally centered: bbox center within middle 60%.
        cx = x + cw // 2
        if not (w * 0.20 <= cx <= w * 0.80):
            continue
        candidates.append((cw * ch, BBox(x, y, x + cw, y + ch)))

    if not candidates:
        return None
    candidates.sort(reverse=True, key=lambda t: t[0])
    return candidates[0][1]


def _find_caption_bbox(img: np.ndarray, video: BBox) -> BBox | None:
    """Find the author/caption block directly below the video, same x range."""
    h, w = img.shape[:2]

    scan_top = video.bottom
    scan_bottom = min(h, video.bottom + int(video.height * _CAPTION_SCAN_HEIGHT_FRAC))
    if scan_bottom - scan_top < 20:
        return None

    region = img[scan_top:scan_bottom, video.left:video.right]
    if region.size == 0:
        return None

    profile = row_variance(region)
    profile = smooth_1d(profile, window=5)

    threshold = float(np.median(profile)) * _CAPTION_VARIANCE_MULT
    band = find_high_band(profile, threshold)
    if band is None:
        return None

    band_start_abs = scan_top + band[0]
    band_end_abs = scan_top + band[1]
    if band_end_abs <= band_start_abs:
        return None

    return BBox(
        left=video.left,
        top=band_start_abs,
        right=video.right,
        bottom=band_end_abs,
    )
