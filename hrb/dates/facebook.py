"""
Facebook MHTML post date extractor.

Modern FB renders text via single-char <span> elements scrambled in HTML order
and reassembled visually via CSS Flexbox `order:`. Half the spans are decoys
sharing a fixed multi-class suffix with no CSS rules.

Algorithm (primary path — modal-scoped):
1. Load text/html + ALL text/css from the MHTML.
2. Build class -> {prop: value}; detect decoy class tail.
3. Locate the post's modal: the <div role="dialog"> that contains the
   captured URL's pfbid token (or the largest dialog if none matches).
4. Unscramble ONLY the date blocks inside that modal — everything in the
   underlying profile timeline or sibling-post DOM is excluded by construction.
5. Take the first dated block in the modal (post headers come first in a
   post's DOM). Cross-validate if multiple blocks decoded to the same date.

Fallbacks (in order):
    a) pfbid-anchored: no modal, but URL has a pfbid → pick the date block
       immediately preceding the pfbid's position in full HTML.
    b) Legacy page-title anchoring (lowest confidence; original v0.1 path).

Empirically verified against a 2019-02-08 Hunchly capture; the page-title
fallback proved unreliable on real captures where the underlying profile
timeline rendered sibling posts in the same month/year as the modal.
"""
from __future__ import annotations

import email
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import policy



SINGLE_CHAR_SPAN_RE = re.compile(r'<span\s+class="([^"]+)"[^>]*>([^<])</span>')

SCRAMBLE_BLOCK_RE = re.compile(
    r'aria-labelledby="(_r_[^"]+_)"[^>]*>'
    r'<span[^>]*style="display: flex;"[^>]*>'
    r'(.*?)'
    r'</span></span>',
    re.DOTALL,
)

_MONTHS = (
    "January|February|March|April|May|June|"
    "July|August|September|October|November|December"
)

# "August 25, 2019" / "August252019" — explicit year
DATE_RE_FULL = re.compile(
    rf"({_MONTHS})\s*(\d{{1,2}})\s*,?\s*(\d{{4}})",
    re.IGNORECASE,
)

# "May 17 at 2:45 PM" / "May17at245PM" — FB shows this for current-year posts.
# Year is inferred from the capture date (with a future-date rollback to prev year).
DATE_RE_RECENT = re.compile(
    rf"({_MONTHS})\s*(\d{{1,2}})\s*at\s*\d",
    re.IGNORECASE,
)

# Legacy alias for back-compat with existing imports.
DATE_RE = DATE_RE_FULL


def _load_parts(mhtml_bytes: bytes) -> tuple[str, str]:
    msg = email.message_from_bytes(mhtml_bytes, policy=policy.default)
    html_parts, css_parts = [], []
    for part in msg.walk():
        ct = part.get_content_type()
        try:
            content = part.get_content()
        except Exception:
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            content = payload.decode("utf-8", errors="replace")
        if ct == "text/html":
            html_parts.append(content)
        elif ct == "text/css":
            css_parts.append(content)
    main_html = max(html_parts, key=len) if html_parts else ""
    return main_html, "\n".join(css_parts)


def _build_class_props(css: str) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for rule in re.finditer(r"\.([\w-]+)\s*\{([^}]*)\}", css):
        name = rule.group(1)
        body = rule.group(2)
        out.setdefault(name, {})
        for prop in re.finditer(r"([\w-]+)\s*:\s*([^;]+?)(?:;|$)", body):
            out[name][prop.group(1).strip()] = prop.group(2).strip()
    return out


def _detect_decoy_tail(html: str, class_props: dict) -> str | None:
    all_spans = SINGLE_CHAR_SPAN_RE.findall(html)
    if not all_spans:
        return None
    suffix_counter: Counter[str] = Counter()
    for class_str, _ in all_spans:
        tokens = class_str.split()
        for length in range(4, min(13, len(tokens) + 1)):
            suffix_counter[" ".join(tokens[-length:])] += 1
    threshold = max(20, int(0.3 * len(all_spans)))
    best: tuple[tuple[int, int], str] | None = None
    for suffix, count in suffix_counter.items():
        if count < threshold:
            continue
        tokens = suffix.split()
        if any(class_props.get(t, {}).get("order") for t in tokens):
            continue
        key = (len(tokens), count)
        if not best or key > best[0]:
            best = (key, suffix)
    return best[1] if best else None


@dataclass
class UnscrambledBlock:
    label_id: str
    position: int
    text: str


def _unscramble_one(inner_html: str, class_props: dict, decoy_tail: str) -> str:
    items: list[tuple[int, str]] = []
    for class_str, char in SINGLE_CHAR_SPAN_RE.findall(inner_html):
        if decoy_tail and decoy_tail in class_str:
            continue
        order: int | None = None
        for c in class_str.split():
            props = class_props.get(c)
            if props and "order" in props:
                try:
                    order = int(props["order"])
                    break
                except ValueError:
                    pass
        items.append((order if order is not None else 9_999, char))
    items.sort(key=lambda x: x[0])
    return "".join(c for _, c in items)


def _unscramble_all(html: str, class_props: dict, decoy_tail: str) -> list[UnscrambledBlock]:
    out: list[UnscrambledBlock] = []
    for m in SCRAMBLE_BLOCK_RE.finditer(html):
        out.append(UnscrambledBlock(
            label_id=m.group(1),
            position=m.start(),
            text=_unscramble_one(m.group(2), class_props, decoy_tail),
        ))
    return out


def _parse_date(text: str, reference_year: int | None = None) -> datetime | None:
    """
    Parse a date out of a (possibly space-stripped) unscrambled block.

    Tries explicit "Month D, YYYY" first; falls back to "Month D at ..." with
    `reference_year` (or current UTC year). If that yields a future date
    relative to `reference_year`, rolls back to the previous year.
    """
    m = DATE_RE_FULL.search(text)
    if m:
        try:
            return datetime.strptime(
                f"{m.group(1).capitalize()} {int(m.group(2))} {int(m.group(3))}",
                "%B %d %Y",
            )
        except ValueError:
            pass

    m = DATE_RE_RECENT.search(text)
    if m:
        year = reference_year or datetime.now(timezone.utc).year
        try:
            dt = datetime.strptime(
                f"{m.group(1).capitalize()} {int(m.group(2))} {year}",
                "%B %d %Y",
            )
        except ValueError:
            return None
        if reference_year is not None:
            ref = datetime(reference_year, 12, 31)
            if dt > ref:
                dt = dt.replace(year=year - 1)
        return dt

    return None


PFBID_RE = re.compile(r"pfbid[0-9A-Za-z]+")
# Matches fbid=<digits> (photo viewer) and v=<digits> (/watch/?v= share URL)
_QUERY_NUMERIC_ID_RE = re.compile(r"[?&](?:fbid|v)=(\d{8,})")
_PATH_NUMERIC_ID_RE = re.compile(r"/(?:videos|posts|photos|photo|reel|reels|watch)/(\d{8,})")


def _extract_pfbid(url: str | None) -> str | None:
    """Legacy alias retained so callers using only pfbids keep working."""
    if not url:
        return None
    m = PFBID_RE.search(url)
    return m.group(0) if m else None


def _extract_post_id(url: str | None) -> str | None:
    """
    Pull the FB post identifier from any of the common URL shapes:
        - /posts/pfbid... or /videos/pfbid...   → pfbid token
        - /photo/?fbid=<numeric>                 → numeric fbid
        - /<user>/videos/<numeric>               → numeric video ID
        - /<user>/posts/<numeric>                → legacy numeric post ID

    Returned token is what we look for inside `<a href="...">` upstream of a
    date block to claim that block as the captured post's date.
    """
    if not url:
        return None
    m = PFBID_RE.search(url)
    if m:
        return m.group(0)
    m = _QUERY_NUMERIC_ID_RE.search(url)
    if m:
        return m.group(1)
    m = _PATH_NUMERIC_ID_RE.search(url)
    if m:
        return m.group(1)
    return None


# How many bytes before a date block to scan for an <a href> permalink.
# The post's timestamp is rendered as a clickable link, so the anchor
# opening tag sits a few hundred bytes upstream of the scrambled spans.
ID_ANCHOR_LOOKBACK = 1000

_HREF_RE = re.compile(r'<a[^>]*href="([^"]+)"')


def _is_id_anchored(html: str, block_pos: int, post_id: str) -> bool:
    """
    True if a <a href="..."> whose href contains `post_id` appears in the
    `ID_ANCHOR_LOOKBACK` bytes preceding `block_pos`.

    FB renders the captured post's visible date as a permalink anchor
    pointing at the post (containing its pfbid, fbid, or numeric ID).
    Sibling posts on the same page have different IDs in their date
    anchors, so this filter cleanly separates the captured post's date
    from sibling/recommended-post dates.
    """
    ctx = html[max(0, block_pos - ID_ANCHOR_LOOKBACK):block_pos]
    return any(post_id in href for href in _HREF_RE.findall(ctx))


def _find_post_body_positions(html: str, hint: str | None) -> list[int]:
    if hint:
        positions = [m.start() for m in re.finditer(re.escape(hint), html)]
        if positions:
            return positions
    runs = re.findall(r">([^<>{}]{60,500})<", html)
    runs.sort(key=len, reverse=True)
    for run in runs[:20]:
        if re.search(r"[A-Z][a-z]+ [a-z]+ [a-z]+ [a-z]+", run):
            positions = [m.start() for m in re.finditer(re.escape(run[:40]), html)]
            if positions:
                return positions
    return []


def _select_post_date(blocks: list[UnscrambledBlock], body_positions: list[int], reference_year: int | None = None) -> UnscrambledBlock | None:
    dated = [b for b in blocks if _parse_date(b.text, reference_year)]
    if not dated:
        return None
    if not body_positions:
        return dated[0]
    for bp in body_positions:
        preceding = [b for b in dated if b.position < bp]
        if preceding:
            return preceding[-1]
    return dated[0]


@dataclass
class FBExtractResult:
    post_date: datetime | None
    confidence: str = "failed"
    decoded_blocks: list[UnscrambledBlock] = field(default_factory=list)
    decoy_tail: str | None = None
    notes: str = ""


def extract_from_bytes(
    mhtml_bytes: bytes,
    post_url: str | None = None,
    post_body_hint: str | None = None,
    reference_year: int | None = None,
) -> FBExtractResult:
    html, css = _load_parts(mhtml_bytes)
    if not html:
        return FBExtractResult(None, "failed", notes="no HTML in MHTML")

    class_props = _build_class_props(css)
    decoy_tail = _detect_decoy_tail(html, class_props)
    post_id = _extract_post_id(post_url)

    blocks = _unscramble_all(html, class_props, decoy_tail or "")
    if not blocks:
        return FBExtractResult(None, "failed", decoded_blocks=[], decoy_tail=decoy_tail,
                               notes="no scrambled-text blocks found")

    dated: list[tuple[UnscrambledBlock, datetime]] = [
        (b, d) for b in blocks
        if (d := _parse_date(b.text, reference_year)) is not None
    ]
    if not dated:
        return FBExtractResult(None, "failed", decoded_blocks=blocks, decoy_tail=decoy_tail,
                               notes="no decoded block contained a parseable date")

    # --- Primary: post-ID-anchored selection ------------------------------
    # FB renders the captured post's visible date as a permalink anchor
    # whose href contains the URL's post identifier (pfbid token, fbid
    # query value, or numeric video/post/photo ID). Sibling posts on the
    # same page carry their OWN identifiers, so filtering by "this post's
    # ID appears in an <a href> just upstream of the date block" cleanly
    # excludes sibling/recommended posts.
    #
    # When the captured post is rendered multiple times on the page (e.g.
    # share-dialog preview, breadcrumb, then the actual post body), all
    # renders carry the same pfbid in their anchors. Empirically the LATER
    # render in DOM order is the authoritative one — earlier renders are
    # preview cards that sometimes decode to slightly different dates due
    # to decoy contamination in the abbreviated markup. So we take the
    # LAST id-anchored block, not the first.
    if post_id:
        anchored = [(b, d) for b, d in dated if _is_id_anchored(html, b.position, post_id)]
        if anchored:
            anchored.sort(key=lambda x: x[0].position)
            chosen_block, post_date = anchored[-1]
            same_date_anchored = [d for _, d in anchored if d == post_date]
            if len(same_date_anchored) >= 2:
                conf = "id_cross_validated"
                notes = (
                    f"post-ID-anchored selection; "
                    f"{len(same_date_anchored)}/{len(anchored)} ID-anchored blocks agreed"
                )
            else:
                conf = "id_anchored"
                notes = (
                    f"post-ID-anchored selection; "
                    f"first of {len(anchored)} ID-anchored block(s)"
                )
            return FBExtractResult(post_date, conf, blocks, decoy_tail, notes=notes)

    # --- Fallback: legacy page-title anchoring (lowest confidence) --------
    # Reached when no post-ID was extractable from the URL, or no ID-anchor
    # was found upstream of any dated block. These are genuinely ambiguous —
    # caller may prefer to route to REVIEW_REQUIRED.
    body_positions = _find_post_body_positions(html, post_body_hint)
    chosen = _select_post_date(blocks, body_positions, reference_year)
    if not chosen:
        return FBExtractResult(None, "failed", decoded_blocks=blocks, decoy_tail=decoy_tail,
                               notes="no decoded block contained a parseable date")
    post_date = _parse_date(chosen.text, reference_year)
    if not post_date:
        return FBExtractResult(None, "failed", decoded_blocks=blocks, decoy_tail=decoy_tail,
                               notes="selected block had no parseable date")

    matching = [b for b in blocks if _parse_date(b.text, reference_year) == post_date]
    if len(matching) >= 2:
        conf, notes = (
            "legacy_cross_validated",
            f"legacy page-title fallback; {len(matching)} blocks decoded to same date",
        )
    elif body_positions:
        conf, notes = "legacy_single", "legacy page-title fallback; single matching block"
    else:
        conf, notes = "legacy_no_anchor", "legacy fallback with no anchor (lowest confidence)"

    return FBExtractResult(post_date, conf, blocks, decoy_tail, notes=notes)
