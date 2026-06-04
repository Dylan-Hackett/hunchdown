"""
Facebook detectors.

Two strategies, dispatched on `post_style`:

  * `single_post` (and similar): find the centered white modal post card
    floating over the dimmed feed (used for `/posts/pfbid…`, `/photo/?fbid=…`,
    `/videos/<id>` permalinks). See `_detect_modal_post`.

  * `main_account` (profile pages): detect named landmarks (cover_photo,
    avatar, tabs_row, lower_panel, intro_card) and compute the crop using
    tunable margins loaded from main_account_tuning.json. The crop is
    horizontally symmetric (Dylan's "equal sides" rule) and ends at the
    bottom of the intro card when visible, else at the image bottom.

Either path returns None on failure → caller falls back to the preset's
static crop %.
"""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

from ._common import BBox

_TUNING_PATH = Path(__file__).resolve().parent.parent.parent / "presets" / "facebook" / "main_account_tuning.json"


def _load_tuning() -> dict:
    """Read margin tuning live each call so tune-GUI saves take immediate effect."""
    try:
        return json.loads(_TUNING_PATH.read_text())
    except FileNotFoundError:
        return {
            "top_margin_below_cover_pct": 1.5,
            "bottom_margin_below_intro_pct": 1.5,
            "side_margin_around_panel_pct": 1.5,
        }


# ---------- Modal post card (single_post) ----------

_WHITE_THRESHOLD = 235
_MIN_SLAB_AREA_FRAC = 0.001
_CENTER_BAND = (0.15, 0.85)
_EDGE_MARGIN_PX = 5
_WIDTH_TOLERANCE_FRAC = 0.20
_X_TOLERANCE_PX = 60


def _detect_modal_post(img: np.ndarray, requested: list[str]) -> dict[str, BBox] | None:
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _, white = cv2.threshold(gray, _WHITE_THRESHOLD, 255, cv2.THRESH_BINARY)

    n, _, stats, _ = cv2.connectedComponentsWithStats(white, connectivity=8)
    slabs: list[tuple[int, int, int, int]] = []
    img_area = w * h
    for i in range(1, n):
        x, y, cw, ch, area = stats[i]
        if x < _EDGE_MARGIN_PX or y < _EDGE_MARGIN_PX:
            continue
        if x + cw > w - _EDGE_MARGIN_PX or y + ch > h - _EDGE_MARGIN_PX:
            continue
        if area < img_area * _MIN_SLAB_AREA_FRAC:
            continue
        cx = x + cw // 2
        if not (w * _CENTER_BAND[0] <= cx <= w * _CENTER_BAND[1]):
            continue
        slabs.append((x, y, cw, ch))

    if not slabs:
        return None

    widest = max(slabs, key=lambda s: s[2])
    cw_ref, x_ref = widest[2], widest[0]
    same_card = [
        s for s in slabs
        if abs(s[2] - cw_ref) <= cw_ref * _WIDTH_TOLERANCE_FRAC
        and abs(s[0] - x_ref) <= _X_TOLERANCE_PX
    ]
    if not same_card:
        return None

    left = min(s[0] for s in same_card)
    top = min(s[1] for s in same_card)
    right = max(s[0] + s[2] for s in same_card)
    bottom = max(s[1] + s[3] for s in same_card)
    return BBox(left, top, right, bottom)


# ---------- Profile / main account landmarks ----------

# FB chrome (blue search bar, nav icons) occupies the top ~4% of every capture.
_CHROME_SKIP_FRAC = 0.04

# Lower panel: light-gray fill containing the Intro card. Spans the full
# content width consistently — anchor for horizontal crop bounds.
_PANEL_GRAY_RANGE = (240, 250)
_PANEL_SEARCH_TOP_FRAC = 0.35
_PANEL_CLOSE_KERNEL = (20, 20)

# Intro card: leftmost white card in the lower panel.
_CARD_WHITE_THRESHOLD = 250
_CARD_MIN_AREA_FRAC = 0.005
_CARD_MIN_HEIGHT_PX = 100

# Avatar: Hough circle. Search band = lower half of the area between chrome
# and the lower panel (where the avatar+name strip sits).
_AVATAR_MIN_RADIUS_FRAC = 0.035
_AVATAR_MAX_RADIUS_FRAC = 0.09


def _find_lower_panel_bbox(gray: np.ndarray, w: int, h: int) -> BBox | None:
    lo, hi = _PANEL_GRAY_RANGE
    mask = ((gray >= lo) & (gray <= hi)).astype(np.uint8) * 255
    mask[: int(h * _PANEL_SEARCH_TOP_FRAC), :] = 0
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones(_PANEL_CLOSE_KERNEL[::-1], np.uint8))
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if n <= 1:
        return None
    idx = max(range(1, n), key=lambda i: int(stats[i, cv2.CC_STAT_AREA]))
    px = int(stats[idx, cv2.CC_STAT_LEFT])
    py = int(stats[idx, cv2.CC_STAT_TOP])
    pw = int(stats[idx, cv2.CC_STAT_WIDTH])
    ph = int(stats[idx, cv2.CC_STAT_HEIGHT])
    return BBox(px, py, px + pw, py + ph)


def _find_intro_card_bbox(gray: np.ndarray, w: int, h: int, panel: BBox) -> BBox | None:
    in_panel = np.zeros_like(gray)
    in_panel[panel.top:panel.bottom, panel.left:panel.right] = (
        gray[panel.top:panel.bottom, panel.left:panel.right]
    )
    _, cards_mask = cv2.threshold(in_panel, _CARD_WHITE_THRESHOLD, 255, cv2.THRESH_BINARY)
    n, _, stats, _ = cv2.connectedComponentsWithStats(cards_mask, connectivity=8)
    img_area = w * h
    cards = []
    for i in range(1, n):
        cx = int(stats[i, cv2.CC_STAT_LEFT])
        cy = int(stats[i, cv2.CC_STAT_TOP])
        cw = int(stats[i, cv2.CC_STAT_WIDTH])
        ch = int(stats[i, cv2.CC_STAT_HEIGHT])
        area = int(stats[i, cv2.CC_STAT_AREA])
        if area < img_area * _CARD_MIN_AREA_FRAC:
            continue
        if ch < _CARD_MIN_HEIGHT_PX:
            continue
        cards.append((cx, cy, cw, ch))
    if not cards:
        return None
    # Intro card = leftmost card.
    cards.sort(key=lambda c: c[0])
    cx, cy, cw, ch = cards[0]
    return BBox(cx, cy, cx + cw, cy + ch)


def _find_avatar_bbox(gray: np.ndarray, w: int, h: int, panel_top: int) -> BBox | None:
    """Hough circle for the FB profile avatar.

    The avatar sits in the lower half of the area between the chrome bar
    and the lower gray panel — that's where the avatar+name strip lives.
    """
    chrome_y = int(h * _CHROME_SKIP_FRAC)
    above_panel_height = panel_top - chrome_y
    if above_panel_height < 200:
        return None
    band_top = chrome_y + int(above_panel_height * 0.45)
    band_bot = panel_top + 20
    if band_bot - band_top < 100:
        return None

    band = gray[band_top:band_bot, :]
    blur = cv2.GaussianBlur(band, (9, 9), 2)
    min_r = max(60, int(w * _AVATAR_MIN_RADIUS_FRAC))
    max_r = max(min_r + 40, int(w * _AVATAR_MAX_RADIUS_FRAC))
    circles = cv2.HoughCircles(
        blur, cv2.HOUGH_GRADIENT, dp=1.4, minDist=max_r * 2,
        param1=120, param2=30, minRadius=min_r, maxRadius=max_r,
    )
    if circles is None:
        return None
    # Filter: circle must fit entirely within the band (no negative coords).
    valid = [
        c for c in circles[0]
        if c[1] - c[2] >= 0 and c[1] + c[2] <= band.shape[0]
    ]
    if not valid:
        return None
    cx, cy, r = (float(v) for v in sorted(valid, key=lambda c: c[0])[0])  # leftmost
    return BBox(
        int(cx - r),
        band_top + int(cy - r),
        int(cx + r),
        band_top + int(cy + r),
    )


def detect_profile_landmarks(img: np.ndarray) -> dict[str, BBox] | None:
    """Detect FB profile landmarks: avatar, lower_panel, intro_card."""
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    panel = _find_lower_panel_bbox(gray, w, h)
    if panel is None:
        return None
    avatar = _find_avatar_bbox(gray, w, h, panel.top)
    if avatar is None:
        return None
    intro = _find_intro_card_bbox(gray, w, h, panel)
    out = {
        "avatar": avatar,
        "lower_panel": panel,
    }
    if intro is not None:
        out["intro_card"] = intro
    return out


def _detect_profile(img: np.ndarray, requested: list[str]) -> dict[str, BBox] | None:
    h, w = img.shape[:2]
    landmarks = detect_profile_landmarks(img)
    if landmarks is None:
        return None

    avatar = landmarks["avatar"]
    panel = landmarks["lower_panel"]
    intro = landmarks.get("intro_card")

    t = _load_tuning()
    top_m = int(h * t["top_margin_above_avatar_pct"] / 100.0)
    bot_m = int(h * t["bottom_margin_below_intro_pct"] / 100.0)
    side_m = int(w * t["side_margin_around_panel_pct"] / 100.0)

    crop_top = max(0, avatar.top - top_m)
    if intro is not None:
        crop_bottom = min(h, intro.bottom + bot_m)
    else:
        crop_bottom = min(h, panel.bottom + bot_m)
    crop_left = max(0, panel.left - side_m)
    crop_right = min(w, panel.right + side_m)

    if crop_right - crop_left < w * 0.3 or crop_bottom - crop_top < h * 0.15:
        return None

    bbox = BBox(crop_left, crop_top, crop_right, crop_bottom)
    out: dict[str, BBox] = {}
    for c in requested:
        if c in ("post", "post_card", "profile", "main_account"):
            out[c] = bbox
        elif c == "avatar":
            out[c] = avatar
        elif c == "lower_panel":
            out[c] = panel
        elif c == "intro_card" and intro is not None:
            out[c] = intro
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

    card = _detect_modal_post(img, requested)
    if card is None:
        return None
    out: dict[str, BBox] = {}
    for c in requested:
        if c in ("post", "post_card"):
            out[c] = card
        else:
            return None
    return out
