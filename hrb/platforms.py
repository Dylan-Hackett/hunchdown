"""
Classify a URL into a platform_id and detect whether it points to a main-account
profile/landing page (as opposed to a single post).
"""
from __future__ import annotations

import re
from urllib.parse import urlparse


PLATFORM_ORDER = [
    "facebook", "instagram", "tiktok", "x", "linkedin",
    "youtube", "threads", "reddit", "other",
]

PLATFORM_DISPLAY_NAMES = {
    "facebook": "Facebook",
    "instagram": "Instagram",
    "tiktok": "TikTok",
    "x": "X",
    "linkedin": "LinkedIn",
    "youtube": "YouTube",
    "threads": "Threads",
    "reddit": "Reddit",
    "other": "Other",
}


_HOST_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("facebook",  re.compile(r"(?:^|\.)facebook\.com$|(?:^|\.)fb\.com$|(?:^|\.)fb\.watch$", re.I)),
    ("instagram", re.compile(r"(?:^|\.)instagram\.com$", re.I)),
    ("tiktok",    re.compile(r"(?:^|\.)tiktok\.com$", re.I)),
    ("x",         re.compile(r"(?:^|\.)x\.com$|(?:^|\.)twitter\.com$", re.I)),
    ("linkedin",  re.compile(r"(?:^|\.)linkedin\.com$", re.I)),
    ("youtube",   re.compile(r"(?:^|\.)youtube\.com$|(?:^|\.)youtu\.be$", re.I)),
    ("threads",   re.compile(r"(?:^|\.)threads\.net$|(?:^|\.)threads\.com$", re.I)),
    ("reddit",    re.compile(r"(?:^|\.)reddit\.com$", re.I)),
]


def classify(url: str) -> str:
    """Return platform_id for the URL. Falls back to 'other'."""
    host = (urlparse(url).hostname or "").lower()
    for platform, pat in _HOST_PATTERNS:
        if pat.search(host):
            return platform
    return "other"


_POST_PATTERNS: dict[str, re.Pattern[str]] = {
    "facebook":  re.compile(r"/(posts|videos|photos|reel|permalink|story\.php|watch)/|story_fbid=", re.I),
    "instagram": re.compile(r"/(p|reel|reels|tv|stories)/", re.I),
    "tiktok":    re.compile(r"/video/|/photo/", re.I),
    "x":         re.compile(r"/status/", re.I),
    "linkedin":  re.compile(r"/posts/|/feed/update/|activity[-:]", re.I),
    "youtube":   re.compile(r"/watch|/shorts/|youtu\.be/", re.I),
    "threads":   re.compile(r"/post/", re.I),
    "reddit":    re.compile(r"/comments/|/r/[^/]+/s/", re.I),
}

_ACCOUNT_PATTERNS: dict[str, re.Pattern[str]] = {
    "facebook":  re.compile(r"^/[A-Za-z0-9.\-_]+/?$", re.I),
    "instagram": re.compile(r"^/[A-Za-z0-9._]+/?$", re.I),
    "tiktok":    re.compile(r"^/@[A-Za-z0-9._]+/?$", re.I),
    "x":         re.compile(r"^/[A-Za-z0-9_]+/?$", re.I),
    "linkedin":  re.compile(r"^/in/[^/]+/?$|^/company/[^/]+/?$", re.I),
    "youtube":   re.compile(r"^/@[A-Za-z0-9._\-]+/?$|^/c/[^/]+/?$|^/channel/[^/]+/?$|^/user/[^/]+/?$", re.I),
    "threads":   re.compile(r"^/@[A-Za-z0-9._]+/?$", re.I),
    "reddit":    re.compile(r"^/(?:r|user|u)/[A-Za-z0-9._\-]+/?$", re.I),
}


def is_main_account_url(url: str, platform: str | None = None) -> bool:
    """True if the URL looks like a profile/landing page for its platform."""
    if platform is None:
        platform = classify(url)
    if platform == "other":
        return False
    parsed = urlparse(url)
    path = parsed.path or "/"
    post_pat = _POST_PATTERNS.get(platform)
    if post_pat and post_pat.search(parsed.path + ("?" + parsed.query if parsed.query else "")):
        return False
    account_pat = _ACCOUNT_PATTERNS.get(platform)
    if account_pat and account_pat.match(path):
        return True
    return False


def extract_handle(url: str, platform: str | None = None) -> str | None:
    """Best-effort handle/username extraction for the Accounts Located doc."""
    if platform is None:
        platform = classify(url)
    path = (urlparse(url).path or "").strip("/")
    if not path:
        return None
    first = path.split("/")[0]
    if platform in ("tiktok", "threads") and first.startswith("@"):
        return first
    if platform == "linkedin":
        parts = path.split("/")
        if len(parts) >= 2 and parts[0] in ("in", "company"):
            return parts[1]
    if platform == "youtube" and first.startswith("@"):
        return first
    if platform == "reddit":
        parts = path.split("/")
        if len(parts) >= 2 and parts[0] in ("r", "user", "u"):
            return f"{parts[0]}/{parts[1]}"
    if platform in ("facebook", "instagram", "x"):
        return first
    return first
