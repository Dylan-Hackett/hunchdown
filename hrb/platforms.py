"""
Classify a URL into a platform_id and detect whether it points to a main-account
profile/landing page (as opposed to a single post).
"""
from __future__ import annotations

import re
from urllib.parse import unquote, urlparse


PLATFORM_ORDER = [
    "facebook", "instagram", "snapchat", "linkedin", "venmo",
    "tiktok", "x", "youtube", "threads", "reddit",
    "yelp", "cashapp", "paypal", "spotify", "soundcloud",
    "twitch", "pinterest", "vimeo", "medium", "bluesky",
    "telegram", "github", "letterboxd", "onlyfans", "vsco",
    "whatsapp", "other",
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
    "yelp": "Yelp",
    "cashapp": "Cash App",
    "venmo": "Venmo",
    "paypal": "PayPal",
    "spotify": "Spotify",
    "soundcloud": "SoundCloud",
    "twitch": "Twitch",
    "pinterest": "Pinterest",
    "vimeo": "Vimeo",
    "medium": "Medium",
    "bluesky": "Bluesky",
    "telegram": "Telegram",
    "github": "GitHub",
    "letterboxd": "Letterboxd",
    "onlyfans": "OnlyFans",
    "vsco": "VSCO",
    "whatsapp": "WhatsApp",
    "snapchat": "Snapchat",
    "other": "Other",
}


_HOST_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("facebook",   re.compile(r"(?:^|\.)facebook\.com$|(?:^|\.)fb\.com$|(?:^|\.)fb\.watch$", re.I)),
    ("instagram",  re.compile(r"(?:^|\.)instagram\.com$", re.I)),
    ("tiktok",     re.compile(r"(?:^|\.)tiktok\.com$", re.I)),
    ("x",          re.compile(r"(?:^|\.)x\.com$|(?:^|\.)twitter\.com$", re.I)),
    ("linkedin",   re.compile(r"(?:^|\.)linkedin\.com$", re.I)),
    ("youtube",    re.compile(r"(?:^|\.)youtube\.com$|(?:^|\.)youtu\.be$", re.I)),
    ("threads",    re.compile(r"(?:^|\.)threads\.net$|(?:^|\.)threads\.com$", re.I)),
    ("reddit",     re.compile(r"(?:^|\.)reddit\.com$", re.I)),
    ("yelp",       re.compile(r"(?:^|\.)yelp\.com$", re.I)),
    ("cashapp",    re.compile(r"(?:^|\.)cash\.app$", re.I)),
    ("venmo",      re.compile(r"(?:^|\.)venmo\.com$", re.I)),
    ("paypal",     re.compile(r"(?:^|\.)paypal\.me$|(?:^|\.)paypal\.com$", re.I)),
    ("spotify",    re.compile(r"(?:^|\.)spotify\.com$", re.I)),
    ("soundcloud", re.compile(r"(?:^|\.)soundcloud\.com$", re.I)),
    ("twitch",     re.compile(r"(?:^|\.)twitch\.tv$", re.I)),
    ("pinterest",  re.compile(r"(?:^|\.)pinterest\.com$", re.I)),
    ("vimeo",      re.compile(r"(?:^|\.)vimeo\.com$", re.I)),
    ("medium",     re.compile(r"(?:^|\.)medium\.com$", re.I)),
    ("bluesky",    re.compile(r"(?:^|\.)bsky\.app$", re.I)),
    ("telegram",   re.compile(r"(?:^|\.)t\.me$|(?:^|\.)telegram\.me$", re.I)),
    ("github",     re.compile(r"(?:^|\.)github\.com$", re.I)),
    ("letterboxd", re.compile(r"(?:^|\.)letterboxd\.com$", re.I)),
    ("onlyfans",   re.compile(r"(?:^|\.)onlyfans\.com$", re.I)),
    ("vsco",       re.compile(r"(?:^|\.)vsco\.co$", re.I)),
    ("whatsapp",   re.compile(r"(?:^|\.)wa\.me$|(?:^|\.)whatsapp\.com$", re.I)),
    ("snapchat",   re.compile(r"(?:^|\.)snapchat\.com$", re.I)),
]


def classify(url: str) -> str:
    """Return platform_id for the URL. Falls back to 'other'."""
    host = (urlparse(url).hostname or "").lower()
    for platform, pat in _HOST_PATTERNS:
        if pat.search(host):
            return platform
    return "other"


_POST_PATTERNS: dict[str, re.Pattern[str]] = {
    # Includes /photo/?fbid=... (singular path, fbid in query) used by FB's
    # modern photo viewer, plus *.php variants for legacy permalinks.
    "facebook":  re.compile(
        r"/(?:posts|videos|photos?|reel|reels|permalink|watch)/"
        r"|/(?:photo|story|video)\.php"
        r"|[?&](?:story_)?fbid=",
        re.I,
    ),
    "instagram":  re.compile(r"/(p|reel|reels|tv|stories)/", re.I),
    "tiktok":     re.compile(r"/video/|/photo/", re.I),
    "x":          re.compile(r"/status/", re.I),
    "linkedin":   re.compile(r"/posts/|/feed/update/|activity[-:]", re.I),
    "youtube":    re.compile(r"/watch|/shorts/|youtu\.be/", re.I),
    "threads":    re.compile(r"/post/", re.I),
    "reddit":     re.compile(r"/comments/|/r/[^/]+/s/", re.I),
    # `/playlist/`, `/track/`, `/album/`, `/episode/`, `/show/`, and Spotify
    # artist URLs are not main accounts.
    "spotify":    re.compile(r"/(?:playlist|track|album|episode|show|artist)/", re.I),
    # SoundCloud track URL is `/<user>/<slug>` — second path segment present.
    # Handled via the account regex (anchored to single-segment).
    "soundcloud": re.compile(r"/sets/|/discover|/stream", re.I),
    # Vimeo videos are numeric-only paths.
    "vimeo":      re.compile(r"^/\d+(?:/|$)", re.I),
    # Pinterest pins live under `/pin/<id>/`.
    "pinterest":  re.compile(r"/pin/", re.I),
    # Twitch videos and clips.
    "twitch":     re.compile(r"^/videos/|/clip/|^/directory(?:/|$)", re.I),
    # Medium posts: `/@user/post-slug-<id>` or `/p/<id>`.
    "medium":     re.compile(r"^/p/|^/@[^/]+/[^/]+", re.I),
    # Bluesky posts: `/profile/<handle>/post/<rkey>`.
    "bluesky":    re.compile(r"/post/", re.I),
    # GitHub repos / gists / orgs sub-pages: anything beyond `/<user>`.
    "github":     re.compile(r"^/(?:orgs|gist|topics|search|marketplace|explore|trending)(?:/|$)|^/[^/]+/[^/]+", re.I),
    # Letterboxd film/list/review URLs.
    "letterboxd": re.compile(r"^/film/|^/list/|/film/|/list/|/review/", re.I),
}

_ACCOUNT_PATTERNS: dict[str, re.Pattern[str]] = {
    "facebook":   re.compile(r"^/[A-Za-z0-9.\-_]+/?$", re.I),
    "instagram":  re.compile(r"^/[A-Za-z0-9._]+/?$", re.I),
    "tiktok":     re.compile(r"^/@[A-Za-z0-9._]+/?$", re.I),
    "x":          re.compile(r"^/[A-Za-z0-9_]+/?$", re.I),
    "linkedin":   re.compile(r"^/in/[^/]+/?$|^/company/[^/]+/?$", re.I),
    "youtube":    re.compile(r"^/@[A-Za-z0-9._\-]+/?$|^/c/[^/]+/?$|^/channel/[^/]+/?$|^/user/[^/]+/?$", re.I),
    "threads":    re.compile(r"^/@[A-Za-z0-9._]+/?$", re.I),
    "reddit":     re.compile(r"^/(?:r|user|u)/[A-Za-z0-9._\-]+/?$", re.I),
    # Yelp matches against path+query, so both `/biz/<slug>` and
    # `/user_details?userid=<id>` resolve to main accounts.
    "yelp":       re.compile(r"^/biz/[A-Za-z0-9.\-_]+/?$|^/user_details\?userid=[A-Za-z0-9._\-]+", re.I),
    "cashapp":    re.compile(r"^/\$[A-Za-z0-9._\-]+/?$", re.I),
    "venmo":      re.compile(r"^/u/[A-Za-z0-9._\-]+/?$|^/[A-Za-z0-9._\-]+/?$", re.I),
    "paypal":     re.compile(r"^/[A-Za-z0-9._\-]+/?$", re.I),
    # Spotify users only (no artist/playlist/track) — opaque base62 ID after /user/.
    "spotify":    re.compile(r"^/user/[A-Za-z0-9._\-]+/?$", re.I),
    # SoundCloud single-segment path = user/listener profile.
    "soundcloud": re.compile(r"^/[A-Za-z0-9._\-]+/?$", re.I),
    "twitch":     re.compile(r"^/[A-Za-z0-9_]+/?$", re.I),
    "pinterest":  re.compile(r"^/[A-Za-z0-9._\-]+/?$", re.I),
    # Vimeo user profile: `/<username>` (non-numeric — numeric is a video, caught by POST pattern).
    "vimeo":      re.compile(r"^/[A-Za-z][A-Za-z0-9._\-]*/?$", re.I),
    "medium":     re.compile(r"^/@[A-Za-z0-9._\-]+/?$", re.I),
    "bluesky":    re.compile(r"^/profile/[A-Za-z0-9._\-]+/?$", re.I),
    "telegram":   re.compile(r"^/[A-Za-z][A-Za-z0-9_]+/?$", re.I),
    # GitHub single-segment path = user or org.
    "github":     re.compile(r"^/[A-Za-z0-9][A-Za-z0-9\-]*/?$", re.I),
    "letterboxd": re.compile(r"^/[A-Za-z0-9_]+/?$", re.I),
    "onlyfans":   re.compile(r"^/[A-Za-z0-9._\-]+/?$", re.I),
    # VSCO: `/<user>` and `/<user>/gallery|journal|collection|images`.
    "vsco":       re.compile(r"^/[A-Za-z0-9._\-]+(?:/(?:gallery|journal|collection|images|spaces))?/?$", re.I),
    # WhatsApp click-to-chat: phone number is the identity.
    # `wa.me/<phone>` (path) or `api.whatsapp.com/send?phone=<phone>` (query,
    # possibly URL-encoded as `%2B`).
    "whatsapp":   re.compile(r"^/\+?\d+/?$|^/send/?\?.*phone=(?:\+|%2B)?\d+", re.I),
    # Snapchat profile: `snapchat.com/add/<username>` (also `/@<username>`).
    "snapchat":   re.compile(r"^/add/[A-Za-z0-9._\-]+/?$|^/@[A-Za-z0-9._\-]+/?$", re.I),
}


def is_main_account_url(url: str, platform: str | None = None) -> bool:
    """True if the URL looks like a profile/landing page for its platform."""
    if platform is None:
        platform = classify(url)
    if platform == "other":
        return False
    parsed = urlparse(url)
    path = parsed.path or "/"
    path_and_query = path + ("?" + parsed.query if parsed.query else "")
    post_pat = _POST_PATTERNS.get(platform)
    if post_pat and post_pat.search(path_and_query):
        return False
    account_pat = _ACCOUNT_PATTERNS.get(platform)
    if account_pat and account_pat.match(path_and_query):
        return True
    return False


def extract_handle(url: str, platform: str | None = None) -> str | None:
    """Best-effort handle/username extraction for the Accounts Located doc."""
    if platform is None:
        platform = classify(url)
    parsed = urlparse(url)
    path = (parsed.path or "").strip("/")
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
    if platform == "yelp":
        parts = path.split("/")
        if len(parts) >= 2 and parts[0].lower() == "biz":
            return parts[1]
        if first.lower() == "user_details":
            m = re.search(r"userid=([A-Za-z0-9._\-]+)", parsed.query or "")
            if m:
                return m.group(1)
        return first
    if platform == "cashapp":
        return first if first.startswith("$") else f"${first}"
    if platform == "venmo":
        parts = path.split("/")
        if len(parts) >= 2 and parts[0] == "u":
            return parts[1]
        return first
    if platform == "spotify":
        parts = path.split("/")
        if len(parts) >= 2 and parts[0] == "user":
            return parts[1]
        return first
    if platform == "bluesky":
        parts = path.split("/")
        if len(parts) >= 2 and parts[0] == "profile":
            return parts[1]
        return first
    if platform == "medium" and first.startswith("@"):
        return first
    if platform == "snapchat":
        parts = path.split("/")
        if len(parts) >= 2 and parts[0].lower() == "add":
            return parts[1]
        if first.startswith("@"):
            return first
        return first
    if platform == "whatsapp":
        # query-form first: `api.whatsapp.com/send?phone=<phone>`
        m = re.search(r"(?:^|&)phone=(\+?\d+)", unquote(parsed.query or ""))
        if m:
            num = m.group(1)
            return num if num.startswith("+") else f"+{num}"
        # path-form: `wa.me/<phone>` → first segment is the number.
        if first and re.match(r"^\+?\d+$", first):
            return first if first.startswith("+") else f"+{first}"
        return first
    if platform in ("facebook", "instagram", "x"):
        return first
    return first