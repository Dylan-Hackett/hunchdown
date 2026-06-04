"""
Instagram detectors.

Two strategies, dispatched on `post_style`:

  * `single_post` (and similar): find the post region (media + sidebar) on
    the dark page background. See `_detect_single_post`.

  * `main_account` (profile pages): find the action button row
    (Follow / Following / Message) below the bio and crop just below it,
    with equal left/right/bottom margins around the buttons. See
    `_detect_profile`.

Either returns None on failure → caller falls back to the preset's static crop %.
"""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

from ._common import BBox

_TUNING_PATH = Path(__file__).resolve().parent.parent.parent / "presets" / "instagram" / "main_account_tuning.json"


def _load_tuning() -> dict:
    """Read margin tuning (live each call, so the tune-GUI's saves take effect)."""
    try:
        return json.loads(_TUNING_PATH.read_text())
    except FileNotFoundError:
        return {
            "top_margin_above_avatar_pct": 2.3,
            "bottom_margin_below_buttons_pct": 1.3,
            "side_margin_around_buttons_pct": 1.0,
        }


# ---------- Single post (media + sidebar) ----------

_DARK_THRESHOLD = 30
_MIN_POST_AREA_FRAC = 0.05
_CENTER_BAND = (0.15, 0.85)
_EDGE_MARGIN_PX = 5
_MAX_WIDTH_FRAC = 0.95


def _detect_single_post(img: np.ndarray, requested: list[str]) -> dict[str, BBox] | None:
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _, mask = cv2.threshold(gray, _DARK_THRESHOLD, 255, cv2.THRESH_BINARY)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((40, 40), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((10, 10), np.uint8))

    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    img_area = w * h
    candidates: list[tuple[int, int, int, int]] = []
    for i in range(1, n):
        x, y, cw, ch, area = stats[i]
        if x < _EDGE_MARGIN_PX or (x + cw) > (w - _EDGE_MARGIN_PX):
            continue
        if area < img_area * _MIN_POST_AREA_FRAC:
            continue
        if cw > w * _MAX_WIDTH_FRAC:
            continue
        cx = x + cw // 2
        if not (w * _CENTER_BAND[0] <= cx <= w * _CENTER_BAND[1]):
            continue
        candidates.append((int(x), int(y), int(cw), int(ch)))

    if not candidates:
        return None
    candidates.sort(key=lambda s: s[2] * s[3], reverse=True)
    x, y, cw, ch = candidates[0]
    return BBox(x, y, x + cw, y + ch)


# ---------- Profile / main account ----------
#
# Landmark-anchored crop (tuned from ground-truth annotations).
#
# Two landmarks:
#   1. buttons_row — wide thin band of Follow/Following/Message buttons,
#      sits above the post grid.
#   2. avatar — the topmost large non-dark blob inside the same horizontal
#      column as the buttons_row.
#
# Crop rule (from 3 annotated IG profiles, all 3024×1558):
#   top    = avatar_top  − 2.3% of image height
#   bottom = buttons_row_bottom + 1.3% of image height
#   left   = buttons_row_left − 1.0% of image width
#   right  = buttons_row_right + 0.7% of image width

_PROFILE_DARK_THRESHOLD = 40

# buttons row geometry. Min width 40% filters out the narrower stats row
# ("X posts / Y followers / Z following") that sits just above the buttons.
_BTN_MIN_HEIGHT = 50
_BTN_MAX_HEIGHT = 150
_BTN_MIN_ASPECT = 8.0
_BTN_MAX_ASPECT = 25.0
_BTN_MIN_WIDTH_FRAC = 0.40
_BTN_CENTER_BAND = (0.20, 0.80)
_BTN_SEARCH_TOP_FRAC = 0.05
_BTN_SEARCH_BOT_FRAC = 0.70
# Bigger close kernel bridges gaps between individual buttons
# (Follow/Following/Message) so they merge into one wide band.
_BTN_CLOSE_KERNEL = (60, 30)

# avatar_top via horizontal-projection. IG desktop runs in DARK MODE in
# Dylan's captures — page background in the column band is uniformly dark
# (~black), avatar photo is the topmost feature with BRIGHT pixels in the
# column. Walk down, find first sustained run of rows with bright content.
_AVATAR_BRIGHT_THRESHOLD = 120      # gray > 120 → "bright" (avatar/content)
_AVATAR_MIN_BRIGHT_FRAC = 0.04      # ≥ 4% of row must be bright
_AVATAR_MIN_RUN_ROWS = 8            # need ≥ 8 consecutive rows above threshold

# Per-side margins are now loaded from main_account_tuning.json so the
# tune-GUI can update them without code changes. See _load_tuning().


def _find_buttons_row(gray: np.ndarray, w: int, h: int) -> tuple[int, int, int, int] | None:
    _, m = cv2.threshold(gray, _PROFILE_DARK_THRESHOLD, 255, cv2.THRESH_BINARY)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones(_BTN_CLOSE_KERNEL[::-1], np.uint8))
    n, _, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)

    y_lo = int(h * _BTN_SEARCH_TOP_FRAC)
    y_hi = int(h * _BTN_SEARCH_BOT_FRAC)
    candidates: list[tuple[int, int, int, int]] = []
    for i in range(1, n):
        x = int(stats[i, cv2.CC_STAT_LEFT])
        y = int(stats[i, cv2.CC_STAT_TOP])
        cw = int(stats[i, cv2.CC_STAT_WIDTH])
        ch = int(stats[i, cv2.CC_STAT_HEIGHT])
        if ch < _BTN_MIN_HEIGHT or ch > _BTN_MAX_HEIGHT:
            continue
        if cw < w * _BTN_MIN_WIDTH_FRAC:
            continue
        aspect = cw / ch
        if aspect < _BTN_MIN_ASPECT or aspect > _BTN_MAX_ASPECT:
            continue
        if y < y_lo or y + ch > y_hi:
            continue
        cx = x + cw // 2
        if not (w * _BTN_CENTER_BAND[0] <= cx <= w * _BTN_CENTER_BAND[1]):
            continue
        candidates.append((x, y, cw, ch))
    if not candidates:
        return None
    # Prefer the WIDEST qualifying band — the Follow/Message buttons stretch
    # wider than any other wide-thin element (stats text, tabs, etc.).
    candidates.sort(key=lambda c: c[2], reverse=True)
    return candidates[0]


def _find_avatar_top(
    gray: np.ndarray, w: int, h: int,
    col_left: int, col_right: int, buttons_top: int,
) -> int | None:
    """First sustained row of bright content in the column band.

    Used as a HINT for the Hough avatar finder. Real top of profile
    content (the username text) may sit above this value; see
    _find_account_name_bbox.
    """
    if buttons_top < 80 or col_right - col_left < 100:
        return None
    band = gray[:buttons_top, col_left:col_right]
    bright_per_row = (band > _AVATAR_BRIGHT_THRESHOLD).sum(axis=1)
    col_w = col_right - col_left
    threshold = col_w * _AVATAR_MIN_BRIGHT_FRAC
    run = 0
    for i, s in enumerate(bright_per_row):
        if s >= threshold:
            run += 1
            if run >= _AVATAR_MIN_RUN_ROWS:
                start = i - run + 1
                loose = col_w * 0.005
                top = start
                while top > 0 and bright_per_row[top - 1] >= loose:
                    top -= 1
                return top
        else:
            run = 0
    return None


# content_top = first row in the column band with sustained bright content.
# This handles BOTH IG layouts:
#   - With bio: username text sits above avatar → content_top = username row
#   - No bio:   avatar moves up to top of card  → content_top = avatar row
# Either way, content_top is the topmost element to crop above.
_CONTENT_BRIGHT_FRAC_MIN = 0.005   # ≥ 0.5% of col width is bright
_CONTENT_MIN_RUN_ROWS = 3
_CONTENT_STRIP_HEIGHT = 20          # bbox we return is 20px tall for display


def _find_content_top(
    gray: np.ndarray, w: int, h: int,
    col_left: int, col_right: int, buttons_top: int,
) -> int | None:
    """Topmost row in the column band with sustained bright content."""
    if buttons_top < 30 or col_right - col_left < 100:
        return None
    band = gray[:buttons_top, col_left:col_right]
    bright_per_row = (band > _AVATAR_BRIGHT_THRESHOLD).sum(axis=1)
    col_w = col_right - col_left
    threshold = max(5, int(col_w * _CONTENT_BRIGHT_FRAC_MIN))
    run = 0
    for i, s in enumerate(bright_per_row):
        if s >= threshold:
            run += 1
            if run >= _CONTENT_MIN_RUN_ROWS:
                return i - run + 1
        else:
            run = 0
    return None


def _find_avatar_bbox(
    gray: np.ndarray, w: int, h: int,
    col_left: int, col_right: int, avatar_top_hint: int, buttons_top: int,
) -> BBox | None:
    """Find the IG avatar circle via Hough transform.

    The IG profile avatar is a reliably-circular bright disk sitting in
    the upper-left of the header card. Hough circle detection finds it
    directly and gives a tight bounding box, including the actual top of
    the circle (which the projection-based avatar_top often under-shoots
    because the very top of the curve has few bright pixels per row).
    """
    # Search slightly above the projection-based avatar_top hint so we
    # catch the true top edge of the circle.
    y_lo = max(0, avatar_top_hint - 60)
    y_hi = min(buttons_top, avatar_top_hint + int(h * 0.35))
    if y_hi - y_lo < 40 or col_right - col_left < 80:
        return None

    band = gray[y_lo:y_hi, col_left:col_right]
    # Smooth to suppress noise; Hough is sensitive to grain.
    blur = cv2.GaussianBlur(band, (9, 9), 2)

    # Radius range: IG avatar typically ~ 5%–12% of image width.
    min_r = max(30, int(w * 0.04))
    max_r = max(min_r + 10, int(w * 0.13))

    circles = cv2.HoughCircles(
        blur,
        cv2.HOUGH_GRADIENT,
        dp=1.4,
        minDist=max_r * 2,   # avatars aren't stacked vertically
        param1=120,
        param2=30,
        minRadius=min_r,
        maxRadius=max_r,
    )
    if circles is None:
        return None
    detected = circles[0]
    # Prefer the leftmost circle (avatar sits at left of header card).
    detected = sorted(detected, key=lambda c: c[0])
    cx, cy, r = detected[0]
    cx, cy, r = float(cx), float(cy), float(r)
    return BBox(
        col_left + int(cx - r),
        y_lo + int(cy - r),
        col_left + int(cx + r),
        y_lo + int(cy + r),
    )


def detect_profile_landmarks(img: np.ndarray) -> dict[str, BBox] | None:
    """Detect IG profile landmarks.

    Returns:
        content_top:  thin bbox marking the topmost row of profile content
                      in the column band (= username OR avatar, whichever
                      is topmost in this layout).
        avatar:       Hough-detected circle bbox.
        buttons_row:  Follow/Following/Message row bbox.
    """
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    btn = _find_buttons_row(gray, w, h)
    if btn is None:
        return None
    bx, by, bw, bh = btn
    buttons_row = BBox(bx, by, bx + bw, by + bh)

    avatar_top_hint = _find_avatar_top(gray, w, h, bx, bx + bw, by)
    if avatar_top_hint is None:
        return None
    avatar = _find_avatar_bbox(gray, w, h, bx, bx + bw, avatar_top_hint, by)
    if avatar is None:
        return None

    content_top_y = _find_content_top(gray, w, h, bx, bx + bw, by)
    if content_top_y is None:
        # Fall back to avatar top if projection finds nothing.
        content_top_y = avatar.top

    # content_top "bbox" is a thin horizontal strip for tuner display.
    content_top = BBox(
        bx, content_top_y,
        bx + bw, content_top_y + _CONTENT_STRIP_HEIGHT,
    )

    return {
        "content_top": content_top,
        "avatar": avatar,
        "buttons_row": buttons_row,
    }


def _detect_profile(img: np.ndarray, requested: list[str]) -> dict[str, BBox] | None:
    h, w = img.shape[:2]
    landmarks = detect_profile_landmarks(img)
    if landmarks is None:
        return None
    ct = landmarks["content_top"]
    av = landmarks["avatar"]
    btn = landmarks["buttons_row"]

    t = _load_tuning()
    top_m = int(h * t["top_margin_above_avatar_pct"] / 100.0)
    bot_m = int(h * t["bottom_margin_below_buttons_pct"] / 100.0)
    side_m = int(w * t["side_margin_around_buttons_pct"] / 100.0)

    # content_top.top already represents whichever is topmost (name or avatar);
    # min() with avatar.top is a defensive belt-and-suspenders in case the
    # projection-based content_top missed something.
    top_anchor = min(ct.top, av.top)
    crop_top = max(0, top_anchor - top_m)
    crop_bottom = min(h, btn.bottom + bot_m)
    crop_left = max(0, btn.left - side_m)
    crop_right = min(w, btn.right + side_m)

    if crop_right - crop_left < w * 0.3 or crop_bottom - crop_top < h * 0.15:
        return None

    bbox = BBox(crop_left, crop_top, crop_right, crop_bottom)
    out: dict[str, BBox] = {}
    for c in requested:
        if c in ("post", "post_card", "profile", "main_account"):
            out[c] = bbox
        elif c == "avatar":
            out[c] = av
        elif c == "buttons_row":
            out[c] = btn
        elif c == "content_top":
            out[c] = ct
        else:
            return None
    return out


# ---------- Dispatch ----------

def detect(
    img: np.ndarray,
    post_style: str,
    requested: list[str],
) -> dict[str, BBox] | None:
    if post_style == "main_account":
        return _detect_profile(img, requested)

    post = _detect_single_post(img, requested)
    if post is None:
        return None
    out: dict[str, BBox] = {}
    for c in requested:
        if c in ("post", "post_card"):
            out[c] = post
        else:
            return None
    return out
