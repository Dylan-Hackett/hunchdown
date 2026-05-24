"""
Facebook MHTML post date extractor.

Modern FB renders text via single-char <span> elements scrambled in HTML order
and reassembled visually via CSS Flexbox `order:`. Half the spans are decoys
sharing a fixed multi-class suffix with no CSS rules.

Algorithm:
1. Load text/html + ALL text/css from the MHTML.
2. Build class -> {prop: value}.
3. Detect decoy class tail (frequent suffix on single-char spans whose
   classes have no `order` rule).
4. For each aria-labelled scramble container: skip decoy spans, sort the
   remaining by CSS order, concatenate.
5. Among decoded date blocks, pick the one immediately preceding the main
   post body text (use page_title or longest plain-text run as anchor).

Empirically verified against a 2019-02-08 Hunchly capture; cross-validated
when the date appears in both modal and timeline renderings.
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
    post_body_hint: str | None = None,
    reference_year: int | None = None,
) -> FBExtractResult:
    html, css = _load_parts(mhtml_bytes)
    if not html:
        return FBExtractResult(None, "failed", notes="no HTML in MHTML")

    class_props = _build_class_props(css)
    decoy_tail = _detect_decoy_tail(html, class_props)
    blocks = _unscramble_all(html, class_props, decoy_tail or "")

    if not blocks:
        return FBExtractResult(None, "failed", decoded_blocks=[], decoy_tail=decoy_tail,
                               notes="no scrambled-text blocks found")

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
        conf = "cross_validated"
        notes = f"{len(matching)} blocks decoded to the same date"
    elif body_positions:
        conf = "single"
        notes = "single matching block, anchored to post body position"
    else:
        conf = "no_body_anchor"
        notes = "no post body anchor; first dated block chosen by default"

    return FBExtractResult(post_date, conf, blocks, decoy_tail, notes=notes)
