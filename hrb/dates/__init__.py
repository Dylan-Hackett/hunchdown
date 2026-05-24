"""
Post-date extraction. Three-tier cascade:
    1. URL Snowflake / shortcode decode (deterministic, no DOM needed).
    2. MHTML parse from the raw case zip (generic + FB unscramble).
    3. REVIEW_REQUIRED — return None so the caller can route the capture.

Never silently fall back to the capture date. If nothing decodes, return None.
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from . import facebook as fb_dates
from . import mhtml_universal


IG_EPOCH_MS = 1314220021721
TWITTER_EPOCH_MS = 1288834974657


@dataclass
class DateResult:
    """One extraction attempt's outcome."""
    post_date: datetime | None
    source: str                       # e.g. "url_snowflake_x", "mhtml_time_element"
    confidence: str                   # "authoritative" | "cross_validated" | "single" | "failed"
    notes: str = ""


# ---------- Tier 1: URL decoders ----------

def decode_tiktok(url: str) -> datetime | None:
    m = re.search(r"/video/(\d+)", url)
    if not m:
        return None
    return datetime.fromtimestamp(int(m.group(1)) >> 32, tz=timezone.utc)


def decode_x(url: str) -> datetime | None:
    m = re.search(r"/status/(\d+)", url)
    if not m:
        return None
    ms = (int(m.group(1)) >> 22) + TWITTER_EPOCH_MS
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _decode_meta_shortcode(shortcode: str) -> datetime | None:
    try:
        padded = shortcode.rjust(12, "A")
        standard = padded.replace("-", "+").replace("_", "/")
        decoded = base64.b64decode(standard)
        media_id = int.from_bytes(decoded, "big")
        ms = (media_id >> 23) + IG_EPOCH_MS
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except Exception:
        return None


def decode_instagram(url: str) -> datetime | None:
    m = re.search(r"instagram\.com/(?:p|reel|reels|tv)/([^/?#]+)", url)
    return _decode_meta_shortcode(m.group(1)) if m else None


def decode_threads(url: str) -> datetime | None:
    m = re.search(r"threads\.(?:net|com)/@[^/]+/post/([^/?#]+)", url)
    return _decode_meta_shortcode(m.group(1)) if m else None


def decode_linkedin(url: str) -> datetime | None:
    m = re.search(r"activity[-:](\d+)", url)
    if not m:
        return None
    ms = (int(m.group(1)) >> 22) + TWITTER_EPOCH_MS
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


_URL_DECODERS: dict[str, tuple[callable, str]] = {
    "tiktok":    (decode_tiktok,    "url_snowflake_tiktok"),
    "x":         (decode_x,         "url_snowflake_x"),
    "instagram": (decode_instagram, "url_shortcode_instagram"),
    "threads":   (decode_threads,   "url_shortcode_threads"),
    "linkedin":  (decode_linkedin,  "url_snowflake_linkedin"),
}


def try_url(url: str, platform: str) -> DateResult | None:
    """Tier 1. Returns None if no decoder for this platform OR decode failed."""
    entry = _URL_DECODERS.get(platform)
    if not entry:
        return None
    fn, source = entry
    dt = fn(url)
    if not dt:
        return None
    return DateResult(dt, source, "authoritative", notes="decoded from URL ID/shortcode")


# ---------- Tier 2: MHTML ----------

def try_mhtml(
    mhtml_bytes: bytes,
    platform: str,
    post_body_hint: str | None = None,
    reference_year: int | None = None,
) -> DateResult | None:
    """Tier 2. Platform-aware: FB → unscrambler, everything else → universal."""
    if not mhtml_bytes:
        return None

    if platform == "facebook":
        r = fb_dates.extract_from_bytes(mhtml_bytes, post_body_hint=post_body_hint, reference_year=reference_year)
        if not r.post_date:
            return None
        return DateResult(
            r.post_date,
            "mhtml_fb_unscramble",
            r.confidence,
            notes=r.notes,
        )

    dt, source = mhtml_universal.extract_from_bytes(mhtml_bytes)
    if not dt:
        return None
    return DateResult(dt, f"mhtml_{source}", "single", notes="universal MHTML extractor")


# ---------- Orchestrator ----------

def extract(
    url: str,
    platform: str,
    mhtml_bytes: bytes | None = None,
    post_body_hint: str | None = None,
    reference_year: int | None = None,
) -> DateResult:
    """
    Run the full cascade. Always returns a DateResult; post_date is None on total failure
    (caller routes that capture to REVIEW_REQUIRED).
    """
    r = try_url(url, platform)
    if r:
        return r

    if mhtml_bytes:
        r = try_mhtml(mhtml_bytes, platform, post_body_hint=post_body_hint, reference_year=reference_year)
        if r:
            return r

    return DateResult(None, "none", "failed", notes="no extractor succeeded")
