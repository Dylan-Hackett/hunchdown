"""
Facebook detectors.

Two strategies, dispatched on `post_style`:

  * `single_post` (and similar): find the centered white modal post card
    floating over the dimmed feed (used for `/posts/pfbid…`, `/photo/?fbid=…`,
    `/videos/<id>` permalinks). See `_detect_modal_post`.

  * `main_account` (profile pages): detect named landmarks (avatar,
    content_column, intro_card) and compute the crop using tunable margins
    loaded from main_account_tuning.json. The crop:
      - top    = avatar.top − top margin (reaches up into the cover photo)
      - bottom = intro card bottom + border (capture "from Personal details up")
      - sides  = content column ± border, forced symmetric about the image
                 center
    The same `border` pixel value is applied to the left, right, and bottom
    so the light-blue lower-panel background frames the content equally on
    all three sides (Dylan's rule). `border` is `border_pct` × image width.
    The content column is the union of the white cards in the lower gray
    panel (intro card on the left through the composer/right card). When no
    cards are visible the column is derived by mirroring the avatar's left
    edge about the image center.

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
            "top_margin_above_avatar_pct": 13.15,
            "left_margin_pct": 1.2,
            "right_margin_pct": 1.6,
            "bottom_margin_pct": 1.0,
        }


# ---------- Modal post card (single_post) ----------

_WHITE_THRESHOLD = 235
_MIN_SLAB_AREA_FRAC = 0.001
_CENTER_BAND = (0.15, 0.85)
_EDGE_MARGIN_PX = 5
_WIDTH_TOLERANCE_FRAC = 0.20
_X_TOLERANCE_PX = 60
# A real "open as post" card is a large centered region (~46% wide, ~39%
# area in testing). Anything well below this is a misfire on a non-feed
# surface (photo lightbox, video player) — reject so the writer falls back
# to the static preset crop instead of emitting a garbage sliver.
_CARD_MIN_WIDTH_FRAC = 0.25
_CARD_MIN_AREA_FRAC = 0.08


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

    # Sanity guard: reject implausibly small cards (non-feed surfaces) so the
    # caller falls back to the static preset crop.
    if (right - left) < w * _CARD_MIN_WIDTH_FRAC:
        return None
    if (right - left) * (bottom - top) < img_area * _CARD_MIN_AREA_FRAC:
        return None
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


def _white_card_top_below(gray: np.ndarray, box: BBox, max_scan: int = 220) -> int | None:
    """First row below `box` (within its x-range) that starts another white card.

    Returns the y of the next card's top edge, or None if no card within
    `max_scan` px. Used to keep the crop's bottom from spilling into the
    card stacked below the intro/Personal-details card.
    """
    h = gray.shape[0]
    y0 = box.bottom
    y1 = min(h, box.bottom + max_scan)
    if y1 - y0 < 5 or box.width < 50:
        return None
    strip = gray[y0:y1, box.left:box.right]
    white_per_row = (strip > _CARD_WHITE_THRESHOLD).sum(axis=1)
    thr = box.width * 0.5
    run = 0
    for i, v in enumerate(white_per_row):
        if v >= thr:
            run += 1
            if run >= 3:
                return y0 + i - run + 1
        else:
            run = 0
    return None


def _find_cards(gray: np.ndarray, w: int, h: int, panel: BBox) -> list[tuple[int, int, int, int]]:
    """All qualifying white cards inside the lower gray panel, as (x,y,w,h)."""
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
    cards.sort(key=lambda c: c[0])  # left → right
    return cards


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
    # The avatar sits in the left portion of the header. Its top edge often
    # extends slightly above the search band (it straddles the cover-photo
    # boundary), so we only require the circle CENTER to be on the left half
    # and let the bbox clamp to the image. (An earlier "fully inside band"
    # filter wrongly rejected avatars whose top crossed the band edge.)
    valid = [c for c in circles[0] if c[0] < w * 0.6]
    if not valid:
        return None
    cx, cy, r = (float(v) for v in sorted(valid, key=lambda c: c[0])[0])  # leftmost
    return BBox(
        max(0, int(cx - r)),
        max(0, band_top + int(cy - r)),
        int(cx + r),
        band_top + int(cy + r),
    )


def detect_profile_landmarks(img: np.ndarray) -> dict[str, BBox] | None:
    """Detect FB profile landmarks: avatar, content_column, intro_card.

    - avatar:         Hough circle of the profile picture.
    - content_column: the centered body column (union of the white cards in
                      the lower panel). When no cards are visible, derived by
                      mirroring the avatar's left edge about the image center.
                      Reported as a full-height strip for the tuner overlay.
    - intro_card:     leftmost white card (only present when cards detected);
                      its bottom is the crop's bottom anchor.
    """
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    panel = _find_lower_panel_bbox(gray, w, h)
    if panel is None:
        return None
    avatar = _find_avatar_bbox(gray, w, h, panel.top)
    if avatar is None:
        return None

    cards = _find_cards(gray, w, h, panel)
    next_card_bbox = None
    if cards:
        col_left = min(c[0] for c in cards)
        col_right = max(c[0] + c[2] for c in cards)
        intro = cards[0]  # leftmost
        intro_bbox = BBox(intro[0], intro[1], intro[0] + intro[2], intro[1] + intro[3])
        # The card stacked directly below the intro (e.g. Friends/Photos),
        # which may be too short / cut off to register as its own card.
        nxt = _white_card_top_below(gray, intro_bbox)
        if nxt is not None:
            next_card_bbox = BBox(intro_bbox.left, nxt, intro_bbox.right, min(h, nxt + 10))
    else:
        # No intro card in viewport — mirror the avatar's left edge about the
        # image center to recover the (centered) content column.
        col_left = avatar.left
        col_right = w - avatar.left
        intro_bbox = None

    content_column = BBox(col_left, 0, col_right, h)

    out: dict[str, BBox] = {
        "avatar": avatar,
        "content_column": content_column,
    }
    if intro_bbox is not None:
        out["intro_card"] = intro_bbox
    if next_card_bbox is not None:
        out["next_card"] = next_card_bbox
    return out


def profile_crop_from_landmarks(landmarks: dict, w: int, h: int, tuning: dict) -> BBox:
    """Compute the FB profile crop bbox from detected landmarks + tuning.

    Shared by the runtime detector and the tuner GUI so they never drift.

      - top    = avatar.top − top margin (reaches into the cover photo)
      - left   = content column left − left margin
      - right  = content column right + right margin
      - bottom = intro card bottom + bottom margin, capped to the gap above
                 the next card so that card never pokes in.
    Left and right are independent because the dark post images on the right
    read as visually "heavier" than the airy cards on the left, so a slightly
    larger right margin makes the blue look balanced even though it isn't
    pixel-equal. All margins are a percentage of image WIDTH (top of HEIGHT).
    """
    avatar = landmarks["avatar"]
    column = landmarks["content_column"]
    intro = landmarks.get("intro_card")
    next_card = landmarks.get("next_card")

    top_m = int(h * tuning["top_margin_above_avatar_pct"] / 100.0)
    left_m = int(w * tuning["left_margin_pct"] / 100.0)
    right_m = int(w * tuning["right_margin_pct"] / 100.0)
    bot_m = int(w * tuning["bottom_margin_pct"] / 100.0)

    # Cap the BOTTOM margin to the blue gap before the next card so it never
    # pokes into the frame. Sides are unaffected.
    if intro is not None and next_card is not None:
        gap = next_card.top - intro.bottom
        bot_m = max(0, min(bot_m, gap - 2))

    crop_top = max(0, avatar.top - top_m)
    if intro is not None:
        crop_bottom = min(h, intro.bottom + bot_m)
    else:
        crop_bottom = h
    crop_left = max(0, column.left - left_m)
    crop_right = min(w, column.right + right_m)
    return BBox(crop_left, crop_top, crop_right, crop_bottom)


def _detect_profile(img: np.ndarray, requested: list[str]) -> dict[str, BBox] | None:
    h, w = img.shape[:2]
    landmarks = detect_profile_landmarks(img)
    if landmarks is None:
        return None

    avatar = landmarks["avatar"]
    column = landmarks["content_column"]
    intro = landmarks.get("intro_card")

    bbox = profile_crop_from_landmarks(landmarks, w, h, _load_tuning())
    crop_left, crop_top, crop_right, crop_bottom = bbox.left, bbox.top, bbox.right, bbox.bottom

    if crop_right - crop_left < w * 0.3 or crop_bottom - crop_top < h * 0.15:
        return None
    out: dict[str, BBox] = {}
    for c in requested:
        if c in ("post", "post_card", "profile", "main_account"):
            out[c] = bbox
        elif c == "avatar":
            out[c] = avatar
        elif c == "content_column":
            out[c] = column
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
