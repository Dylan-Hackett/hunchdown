"""
Config-driven profile-page (main_account) crop detection.

Profile layouts vary a lot per platform, but cluster into a few families.
Rather than a bespoke module per platform, each platform supplies a small
config (family + params) and a tuning JSON of margins. This module
implements the families and the per-platform dispatch.

Families implemented here:
  * ``centered_card`` — white-background pages where the profile identity
    block (avatar / name / handle / action buttons) sits in a centered
    column, with secondary content (pin grid, app-store buttons, feed)
    separated below by a whitespace gap. We crop the first content block.

Facebook and Instagram keep their own dedicated modules (richer landmark
logic); everything else routes through here.

Each family returns a BBox (the crop) or None on failure → caller falls
back to the preset's static crop %.
"""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

from ._common import BBox

_PRESETS_DIR = Path(__file__).resolve().parent.parent.parent / "presets"


def load_tuning(platform: str) -> dict:
    """Per-platform margin tuning, live-reloaded so the tuner GUI takes effect."""
    p = _PRESETS_DIR / platform / "main_account_tuning.json"
    try:
        return json.loads(p.read_text())
    except FileNotFoundError:
        return {}


# --------------------------------------------------------------------------
# centered_card family
# --------------------------------------------------------------------------

def _content_rows(content: np.ndarray, min_count: int) -> np.ndarray:
    """Boolean per-row: does this row have >= min_count content pixels."""
    return content.sum(axis=1) // 255 >= min_count


def _first_content_block(
    row_has: np.ndarray, max_gap: int, min_block: int
) -> tuple[int, int] | None:
    """Top/bottom of the first content block tall enough to be the profile.

    Splits rows into gap-separated blocks (gaps >= max_gap rows), then returns
    the FIRST block whose height >= min_block. This skips short leading blocks
    such as a top nav/icon row, while still stopping before secondary content
    (pin grid, feed) that sits below a gap.
    """
    n = len(row_has)
    i = 0
    while i < n:
        # advance to next content row
        while i < n and not row_has[i]:
            i += 1
        if i >= n:
            return None
        top = i
        bottom = i
        gap = 0
        while i < n:
            if row_has[i]:
                bottom = i
                gap = 0
            else:
                gap += 1
                if gap >= max_gap:
                    break
            i += 1
        if bottom - top >= min_block:
            return top, bottom
        # else: too short (nav row etc.) — continue to the next block
    return None


def _bg_value(gray: np.ndarray) -> int:
    """Median brightness of the four corners = page background."""
    h, w = gray.shape[:2]
    s = 8
    corners = [gray[:s, :s], gray[:s, -s:], gray[-s:, :s], gray[-s:, -s:]]
    return int(np.median(np.concatenate([c.ravel() for c in corners])))


def _content_blocks(
    row_has: np.ndarray, max_gap: int, min_block: int, limit: int
) -> list[tuple[int, int]]:
    """Up to `limit` content blocks (each >= min_block tall), top to bottom."""
    n = len(row_has)
    out: list[tuple[int, int]] = []
    i = 0
    while i < n and len(out) < limit:
        while i < n and not row_has[i]:
            i += 1
        if i >= n:
            break
        top = i
        bottom = i
        gap = 0
        while i < n:
            if row_has[i]:
                bottom = i
                gap = 0
            else:
                gap += 1
                if gap >= max_gap:
                    break
            i += 1
        if bottom - top >= min_block:
            out.append((top, bottom))
    return out


def _is_page_gray(g: np.ndarray, page: int = 242, tol: int = 5) -> np.ndarray:
    return np.abs(g.astype(np.int16) - page) <= tol


def detect_linkedin_card(img: np.ndarray, cfg: dict, tuning: dict) -> BBox | None:
    """LinkedIn main profile card: equal gray margin off the white card edges.

    The card is white on a light-gray page. Its LEFT edge is read from the
    banner (gray->image transition; the banner spans the full card width and
    isn't broken by the avatar/text the way the white body is). Its RIGHT
    edge is read from the white card body (clean white->gray transition). The
    crop frames the card with an equal gray margin on both sides, the top
    just above the banner, and a capped bottom (the lower sections get cut
    off — they vary and don't need to be precise).
    """
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # --- card LEFT + banner TOP, from the banner band ---
    btop_frac = cfg.get("banner_top_frac", 0.10)
    bbot_frac = cfg.get("banner_bot_frac", 0.20)
    band = gray[int(h * btop_frac):int(h * bbot_frac), :]
    ng_cols = (~_is_page_gray(band)).mean(axis=0) > 0.5
    xs = np.where(ng_cols[:int(w * 0.6)])[0]
    if xs.size == 0:
        return None
    card_left = int(xs.min())

    # banner top = top edge of the cover photo. The page has a fixed global
    # nav + search bar (white, non-gray) at the very top, THEN a gray gap
    # (page background), THEN the cover photo. So we can't take the first
    # non-gray row (that's the search bar). Instead: skip the nav by walking
    # down to the gray gap, then through it, and stop at the cover photo's
    # first non-gray row.
    nav_skip = int(h * cfg.get("nav_skip_frac", 0.02))
    limit = int(h * 0.4)
    col_lo, col_hi = card_left, min(w, card_left + int(w * 0.4))
    rows_ng = (~_is_page_gray(gray[:, col_lo:col_hi])).mean(axis=1)
    y = nav_skip
    while y < limit and rows_ng[y] > 0.5:   # skip the nav / search bar
        y += 1
    while y < limit and rows_ng[y] <= 0.5:  # skip the gray gap below the nav
        y += 1
    banner_top = y if y < limit else nav_skip

    # --- card RIGHT, from the white card body ---
    yb = gray[int(h * 0.28):int(h * 0.55), :]
    wf = cv2.blur((yb > 249).mean(axis=0).astype(np.float32).reshape(1, -1), (1, 15)).ravel()
    is_white = wf > 0.4
    runs = []
    s = None
    for x in range(w):
        if is_white[x] and s is None:
            s = x
        elif not is_white[x] and s is not None:
            runs.append((s, x))
            s = None
    if s is not None:
        runs.append((s, w))
    runs = [r for r in runs if r[1] - r[0] > w * 0.15]
    if not runs:
        return None
    card_right = runs[0][1]

    # --- frame: equal gray margin off the card edges ---
    # The margin is one pixel value used on all framed sides (left, right,
    # top, and below the About section) so the gray reads equal everywhere.
    # It's derived from the image WIDTH; using height for the top/bottom
    # would make those margins ~half the side gray since the capture is
    # wider than it is tall.
    margin = int(w * tuning.get("side_margin_pct", cfg.get("side_margin_pct", 0.6)) / 100.0)
    crop_left = max(0, card_left - margin)
    crop_right = min(w, card_right + margin)
    crop_top = max(0, banner_top - margin)

    # --- bottom: cut just after the About section, with gray below ---
    # The profile is a stack of white cards (intro, About, then more
    # sections) separated by gray gaps. Find those gaps below the cover,
    # skip the small header sub-gaps near the top, and cut at the gap that
    # follows the About section (the Nth card down; intro + About = 2). The
    # gray below is min(margin, gap height): inter-section gaps run only
    # ~9-30px, so on a tight gap we take what fits rather than bleed the
    # next section into frame.
    gap_frac = _is_page_gray(gray[:, col_lo:col_hi]).mean(axis=1)
    min_gap_len = max(3, int(h * cfg.get("min_gap_frac", 0.004)))
    section_skip = banner_top + int(h * cfg.get("intro_min_frac", 0.25))
    gaps: list[tuple[int, int]] = []
    in_gap = False
    g_start = 0
    for yy in range(section_skip, h):
        if gap_frac[yy] > 0.8:
            if not in_gap:
                g_start, in_gap = yy, True
        elif in_gap:
            if yy - g_start >= min_gap_len:
                gaps.append((g_start, yy - 1))
            in_gap = False
    if in_gap and h - g_start >= min_gap_len:
        gaps.append((g_start, h - 1))

    if gaps:
        n = cfg.get("sections_below_cover", 2)  # intro + About
        gap = gaps[n - 1] if len(gaps) >= n else gaps[-1]
        card_bottom, gap_end = gap
        crop_bottom = min(h, card_bottom + min(margin, gap_end - card_bottom + 1))
    else:
        cap = tuning.get("max_height_frac", cfg.get("max_height_frac", 0.88))
        crop_bottom = min(h, crop_top + int(h * cap))

    if crop_right - crop_left < w * 0.2:
        return None
    return BBox(crop_left, crop_top, crop_right, crop_bottom)


def detect_centered_card(img: np.ndarray, cfg: dict, tuning: dict) -> BBox | None:
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # Content = pixels that differ from the page background by more than a
    # delta. Works on light *and* dark (TikTok) pages. For card layouts whose
    # body is near-bg (e.g. white card on light-gray page), set bg_override to
    # the PAGE color + a small delta so the whole card reads as one block (its
    # internal whitespace is card-colored, not page-colored).
    bg = cfg.get("bg_override", _bg_value(gray))
    delta = cfg.get("content_delta", 22)
    content = (np.abs(gray.astype(np.int16) - bg) > delta).astype(np.uint8) * 255

    # Restrict horizontally to a central band so side nav / right rails don't
    # pollute the content bounding box.
    sl = int(w * cfg.get("search_left_frac", 0.20))
    sr = int(w * cfg.get("search_right_frac", 0.85))
    band = np.zeros_like(content)
    band[:, sl:sr] = content[:, sl:sr]

    # Optionally skip a top strip (page-title header / chrome above the card).
    st = int(h * cfg.get("search_top_frac", 0.0))
    if st > 0:
        band[:st, :] = 0

    # Small morphological close so text glyphs merge into solid rows.
    band = cv2.morphologyEx(band, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))

    min_row_px = int((sr - sl) * cfg.get("row_content_frac", 0.01))
    row_has = _content_rows(band, max(8, min_row_px))

    max_gap = int(h * cfg.get("block_gap_frac", 0.03))
    min_block = int(h * cfg.get("min_block_frac", 0.08))
    n_blocks = cfg.get("n_blocks", 1)
    if n_blocks > 1:
        # Union the first N qualifying content blocks (e.g. banner + header),
        # stopping before later blocks (tabs / grid / feed). Keeps min_block
        # small so the banner and header each register as their own block.
        blks = _content_blocks(row_has, max_gap, min_block, n_blocks)
        if not blks:
            return None
        top = min(b[0] for b in blks)
        bottom = max(b[1] for b in blks)
    else:
        blk = _first_content_block(row_has, max_gap=max_gap, min_block=min_block)
        if blk is None:
            return None
        top, bottom = blk

    # Horizontal extent of content within the block.
    sub = band[top:bottom + 1, :]
    col_has = (sub.sum(axis=0) // 255) >= max(3, int((bottom - top) * 0.02))
    xs = np.where(col_has)[0]
    if xs.size == 0:
        return None
    left, right = int(xs.min()), int(xs.max())

    # Margins (% of width; top of height).
    side_m = int(w * tuning.get("side_margin_pct", cfg.get("side_margin_pct", 2.0)) / 100.0)
    top_m = int(h * tuning.get("top_margin_pct", cfg.get("top_margin_pct", 2.0)) / 100.0)
    bot_m = int(h * tuning.get("bottom_margin_pct", cfg.get("bottom_margin_pct", 2.0)) / 100.0)

    if cfg.get("independent_margins"):
        # Track the actual content edges with small independent margins
        # (captures may be at different zoom, so the frame can't be fixed and
        # symmetric centering would make one side swing with the other).
        crop_left = max(0, left - side_m)
        crop_right = min(w, right + side_m)
        crop_top = max(0, top - top_m)
        crop_bottom = min(h, bottom + bot_m)
    else:
        # Symmetric sides about the content center.
        cx = (left + right) / 2.0
        half = (right - left) / 2.0 + side_m
        crop_left = max(0, int(round(cx - half)))
        crop_right = min(w, int(round(cx + half)))
        crop_top = max(0, top - top_m)
        crop_bottom = min(h, bottom + bot_m)

    # Optional height cap ("cut it off if it runs too long").
    max_h_frac = cfg.get("max_height_frac")
    if max_h_frac is not None:
        crop_bottom = min(crop_bottom, crop_top + int(h * max_h_frac))

    if crop_right - crop_left < w * 0.15 or crop_bottom - crop_top < h * 0.08:
        return None
    return BBox(crop_left, crop_top, crop_right, crop_bottom)


# --------------------------------------------------------------------------
# Per-platform config + dispatch
# --------------------------------------------------------------------------

PROFILE_CONFIGS: dict[str, dict] = {
    # CV (content length varies). centered_card = "crop the first content
    # block in the central column"; works for headers + cards alike.
    "threads": {
        "family": "centered_card",
        "search_left_frac": 0.30, "search_right_frac": 0.72,
        "search_top_frac": 0.085, "block_gap_frac": 0.06,
        "bottom_margin_pct": 3.5,
    },
    "tiktok": {
        "family": "centered_card",
        "search_left_frac": 0.165, "search_right_frac": 0.64,
        "block_gap_frac": 0.035, "side_margin_pct": 1.2,
    },
    "x": {
        "family": "centered_card",
        # center column sits between the left nav (gutter ~x0.242w) and the
        # right "you might like" sidebar (gutter ~x0.66w).
        "search_left_frac": 0.245, "search_right_frac": 0.655,
        "search_top_frac": 0.062, "block_gap_frac": 0.035,
        "side_margin_pct": 1.0,
    },
    "youtube": {
        "family": "centered_card",
        # left nav gutter ~0.16w; single wide main column, no right sidebar.
        # block1 = banner, block2 = name/desc/Subscribe, then tabs + grid.
        # Union the first two blocks so the variable banner->header gap (which
        # differs light vs dark) doesn't matter; the tabs/grid stay excluded.
        "search_left_frac": 0.165, "search_right_frac": 0.93,
        "search_top_frac": 0.085, "block_gap_frac": 0.018,
        "n_blocks": 2, "min_block_frac": 0.05, "side_margin_pct": 1.0,
    },
    "linkedin": {
        "family": "linkedin_card",
        # Equal gray margin off the white card edges: left from the banner,
        # right from the white body, top above the cover photo, and below the
        # About section. side_margin_pct is that single margin (in pixels off
        # the width) used on all four framed sides so the gray reads equal.
        "nav_skip_frac": 0.02, "banner_top_frac": 0.10, "banner_bot_frac": 0.20,
        "side_margin_pct": 0.6,
        # Bottom: cut after the Nth card down (intro + About = 2). Skip header
        # sub-gaps within intro_min_frac of the cover; a gap must be at least
        # min_gap_frac tall to count. max_height_frac is the no-gaps fallback.
        "sections_below_cover": 2, "intro_min_frac": 0.25,
        "min_gap_frac": 0.004, "max_height_frac": 0.88,
    },
    # Snapchat / Cash App / Venmo / Pinterest / Yelp are FIXED-layout pages
    # and use pixel-exact static crops (more accurate + robust than CV for a
    # non-varying layout).
}

_FAMILY_FUNCS = {
    "centered_card": detect_centered_card,
    "linkedin_card": detect_linkedin_card,
}


def detect_profile(img: np.ndarray, platform: str) -> BBox | None:
    cfg = PROFILE_CONFIGS.get(platform)
    if cfg is None:
        return None
    fn = _FAMILY_FUNCS.get(cfg["family"])
    if fn is None:
        return None
    return fn(img, cfg, load_tuning(platform))
