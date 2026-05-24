"""
Facebook MHTML post date extractor.

Empirically verified against a real Hunchly capture (`pages/1.mhtml`,
FB permalink to a 2019-02-08 post). The algorithm recovered
'February 8, 2019' from Facebook's character-scrambling defense, with
cross-validation across two matching blocks (modal + timeline rendering).

THE PROBLEM:
Modern Facebook renders visible text (including post dates) using
single-character <span> elements with CSS Flexbox `order:` properties
that reassemble them visually. The HTML contains the characters in
scrambled order, plus DECOY characters that are visible to scrapers
but hidden from rendering via a fixed "decoy class tail" suffix.
Standard date extraction methods (data-utime, JSON-LD, article:published_time,
<time>, creation_time) all return 0 on modern FB.

THE SOLUTION:
1. Load both text/html and ALL text/css parts from the MHTML.
2. Build a class -> CSS-properties map.
3. Detect the decoy class tail dynamically: a fixed multi-class suffix
   appearing on a substantial fraction of single-char spans, whose
   classes have no `order:` rule.
4. For each scrambled-text container (aria-labelledby + flex):
   - Skip spans whose class contains the decoy tail.
   - For remaining spans, look up CSS order, sort, concatenate.
5. Among multiple decoded date blocks (FB pages contain timeline +
   modal + sponsored + sibling-post headers), pick the date block
   immediately preceding the main post body text.

REAL CAVEATS:
- Decoy class names rotate per session/capture; algorithm detects
  dynamically rather than hard-coding.
- A few sponsored ads decode to text like "Sponsored ... Learn More"
  with no embedded date; the date pattern parser ignores those.
- If capture rendered incompletely or post is private (stub returned),
  algorithm returns None and caller should fall back to REVIEW_REQUIRED.
"""

from __future__ import annotations
import email
import re
from email import policy
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


# ---------- MHTML loading ----------

def load_mhtml_parts(mhtml_path: str) -> tuple[str, str]:
    """Return (main_html, combined_css) from an MHTML file."""
    with open(mhtml_path, 'rb') as f:
        msg = email.message_from_binary_file(f, policy=policy.default)

    html_parts, css_parts = [], []
    for part in msg.walk():
        ct = part.get_content_type()
        try:
            content = part.get_content()
        except Exception:
            try:
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                content = payload.decode('utf-8', errors='replace')
            except Exception:
                continue
        if ct == 'text/html':
            html_parts.append(content)
        elif ct == 'text/css':
            css_parts.append(content)

    main_html = max(html_parts, key=len) if html_parts else ''
    return main_html, '\n'.join(css_parts)


# ---------- CSS parsing ----------

def build_class_props(css: str) -> dict[str, dict[str, str]]:
    """Map class_name -> { property_name: value }."""
    out: dict[str, dict[str, str]] = {}
    for rule in re.finditer(r'\.([\w-]+)\s*\{([^}]*)\}', css):
        name = rule.group(1)
        body = rule.group(2)
        out.setdefault(name, {})
        for prop in re.finditer(r'([\w-]+)\s*:\s*([^;]+?)(?:;|$)', body):
            out[name][prop.group(1).strip()] = prop.group(2).strip()
    return out


# ---------- Decoy detection ----------

SINGLE_CHAR_SPAN_RE = re.compile(r'<span\s+class="([^"]+)"[^>]*>([^<])</span>')


def detect_decoy_tail(html: str, class_props: dict) -> Optional[str]:
    """
    Find the recurring multi-class suffix that marks decoy spans.

    Heuristic: among single-char spans, the suffix of >=4 consecutive
    class names which (a) appears on a substantial fraction of spans,
    (b) has no `order:` rule for any of its classes.
    """
    all_spans = SINGLE_CHAR_SPAN_RE.findall(html)
    if not all_spans:
        return None
    suffix_counter: Counter = Counter()
    for class_str, _ in all_spans:
        tokens = class_str.split()
        for length in range(4, min(13, len(tokens) + 1)):
            suffix_counter[' '.join(tokens[-length:])] += 1
    threshold = max(20, int(0.3 * len(all_spans)))
    best = None
    for suffix, count in suffix_counter.items():
        if count < threshold:
            continue
        tokens = suffix.split()
        # Reject if any class in the suffix has an `order:` rule
        if any(class_props.get(t, {}).get('order') for t in tokens):
            continue
        # Prefer longer suffixes (more specific), then more occurrences
        key = (len(tokens), count)
        if not best or key > best[0]:
            best = (key, suffix)
    return best[1] if best else None


# ---------- Block extraction ----------

SCRAMBLE_BLOCK_RE = re.compile(
    r'aria-labelledby="(_r_[^"]+_)"[^>]*>'
    r'<span[^>]*style="display: flex;"[^>]*>'
    r'(.*?)'
    r'</span></span>',
    re.DOTALL,
)


@dataclass
class UnscrambledBlock:
    label_id: str
    position: int
    text: str


def unscramble_one(inner_html: str, class_props: dict, decoy_tail: str) -> str:
    items: list[tuple[int, str]] = []
    for class_str, char in SINGLE_CHAR_SPAN_RE.findall(inner_html):
        if decoy_tail and decoy_tail in class_str:
            continue
        order = None
        for c in class_str.split():
            props = class_props.get(c)
            if props and 'order' in props:
                try:
                    order = int(props['order'])
                    break
                except ValueError:
                    pass
        items.append((order if order is not None else 9_999, char))
    items.sort(key=lambda x: x[0])
    return ''.join(c for _, c in items)


def unscramble_all(html: str, class_props: dict, decoy_tail: str) -> list[UnscrambledBlock]:
    out = []
    for m in SCRAMBLE_BLOCK_RE.finditer(html):
        out.append(UnscrambledBlock(
            label_id=m.group(1),
            position=m.start(),
            text=unscramble_one(m.group(2), class_props, decoy_tail),
        ))
    return out


# ---------- Date parsing ----------

MONTHS = ('January February March April June July May August September '
          'October November December').split()
DATE_RE = re.compile(
    r'(January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s*(\d{1,2})\s*,?\s*(\d{4})',
    re.IGNORECASE,
)


def parse_date(text: str) -> Optional[datetime]:
    m = DATE_RE.search(text)
    if not m:
        return None
    try:
        return datetime.strptime(
            f"{m.group(1).capitalize()} {int(m.group(2))} {int(m.group(3))}",
            "%B %d %Y",
        )
    except ValueError:
        return None


# ---------- Main post selection ----------

def find_post_body_positions(html: str, hint: Optional[str]) -> list[int]:
    """Return positions of the main post body in HTML."""
    if hint:
        positions = [m.start() for m in re.finditer(re.escape(hint), html)]
        if positions:
            return positions
    # Fallback: longest plain-text run
    runs = re.findall(r'>([^<>{}]{60,500})<', html)
    runs.sort(key=len, reverse=True)
    for run in runs[:20]:
        if re.search(r'[A-Z][a-z]+ [a-z]+ [a-z]+ [a-z]+', run):
            positions = [m.start() for m in re.finditer(re.escape(run[:40]), html)]
            if positions:
                return positions
    return []


def select_post_date(blocks: list[UnscrambledBlock], body_positions: list[int]) -> Optional[UnscrambledBlock]:
    """Pick the date block immediately preceding the main post body."""
    blocks_with_dates = [b for b in blocks if parse_date(b.text)]
    if not blocks_with_dates:
        return None
    if not body_positions:
        # No body anchor; just return the first dated block
        return blocks_with_dates[0]
    # Find preceding-date block for each body position
    chosen_per_body = []
    for bp in body_positions:
        preceding = [b for b in blocks_with_dates if b.position < bp]
        if preceding:
            chosen_per_body.append(preceding[-1])
    if not chosen_per_body:
        return blocks_with_dates[0]
    return chosen_per_body[0]


# ---------- Public API ----------

@dataclass
class FBExtractResult:
    post_date: Optional[datetime]
    confidence: str  # 'cross_validated' | 'single' | 'no_body_anchor' | 'failed'
    decoded_blocks: list[UnscrambledBlock]
    decoy_tail: Optional[str]
    notes: str = ''


def extract_facebook_post_date(
    mhtml_path: str,
    post_body_hint: Optional[str] = None,
) -> FBExtractResult:
    """
    Extract the post date from a Facebook MHTML capture.

    post_body_hint: a distinctive phrase from the post body
        (e.g. derived from Hunchly's page title field). Optional but
        improves main-post anchoring when the page contains multiple
        posts (timeline view, sponsored ads).
    """
    html, css = load_mhtml_parts(mhtml_path)
    if not html:
        return FBExtractResult(None, 'failed', [], None, notes='no HTML in MHTML')

    class_props = build_class_props(css)
    decoy_tail = detect_decoy_tail(html, class_props)
    blocks = unscramble_all(html, class_props, decoy_tail or '')

    if not blocks:
        return FBExtractResult(None, 'failed', [], decoy_tail, notes='no scrambled-text blocks found')

    body_positions = find_post_body_positions(html, post_body_hint)
    chosen = select_post_date(blocks, body_positions)
    if not chosen:
        return FBExtractResult(None, 'failed', blocks, decoy_tail, notes='no decoded block contained a parseable date')

    post_date = parse_date(chosen.text)
    if not post_date:
        return FBExtractResult(None, 'failed', blocks, decoy_tail, notes='selected block had no parseable date')

    # Confidence
    matching = [b for b in blocks if parse_date(b.text) == post_date]
    if len(matching) >= 2:
        conf = 'cross_validated'
        notes = f'{len(matching)} blocks decoded to the same date'
    elif body_positions:
        conf = 'single'
        notes = 'single matching block, anchored to post body position'
    else:
        conf = 'no_body_anchor'
        notes = 'no post body anchor; first dated block chosen by default'

    return FBExtractResult(post_date, conf, blocks, decoy_tail, notes=notes)


# ---------- CLI demo ----------

if __name__ == '__main__':
    import sys
    mhtml = sys.argv[1] if len(sys.argv) > 1 else 'pages/1.mhtml'
    hint = sys.argv[2] if len(sys.argv) > 2 else None

    r = extract_facebook_post_date(mhtml, post_body_hint=hint)
    print(f"\nMHTML: {mhtml}")
    print(f"Decoy tail detected: {r.decoy_tail!r}")
    print(f"\nDecoded blocks ({len(r.decoded_blocks)}):")
    for b in r.decoded_blocks:
        d = parse_date(b.text)
        ds = d.strftime('%Y-%m-%d') if d else '(no date)'
        print(f"  {b.label_id} @ {b.position:>8,}  date={ds:<12}  text={b.text!r}")
    print(f"\nSelected post date: {r.post_date}")
    print(f"Confidence: {r.confidence}")
    print(f"Notes: {r.notes}")
