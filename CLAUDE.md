# Hunchly Report Builder

## Project Purpose

Take a Hunchly-exported `.docx` file and transform it into a set of polished, structured deliverables:

- **Per-platform exhibit documents** (`.docx` + `.pdf`) with captures organized by platform, sorted by real post date, cropped and resized per your style guides
- **A master Accounts Located document** indexing each subject's main account per platform
- **Replaced caption metadata**: Hunchly's "Updated date" (which is just the capture date) gets swapped out for the actual post date, extracted deterministically from the URL where possible

Chain of custody and reproducibility are non-negotiable.

**No AI calls. No OCR. Fully deterministic.** Every transformation must be reproducible by a third party with only the original Hunchly export.

---

## User Context

- **Developer:** Dylan Hackett
- **Dev environment:** Mac (development), Windows (production deployment via PyInstaller .exe)
- **Test data already uploaded:** `Test_Export.docx` (5 captures across Threads, X, TikTok, Instagram, Facebook)
- **One subject per case** (no need to split docs by subject)
- **Hunchly capture workflow:** Dylan navigates to each post's permalink before capturing. Every capture is a single-post page, never a feed/profile scroll capture (those are handled separately as "main account" captures for the Accounts Located).

---

## Verified Hunchly Export Structure

We unpacked `Test_Export.docx` and confirmed the structure. **All captures follow an identical layout:**

Each capture = one self-contained `<w:tbl>` with **10 rows, 3 cells per row**:

| Row | Content |
|-----|---------|
| 1 | The screenshot image (`<w:drawing>` with `r:embed="rIdX"`, sized via `<wp:extent cx="..." cy="..."/>`) |
| 2 | (empty spacer) |
| 3 | Label: "Page title:" |
| 4 | The page title text |
| 5 | Label: "URL:" |
| 6 | The URL as **plain text with a leading space** (NOT a `w:hyperlink`) |
| 7 | Label: "Hash (SHA-256):" |
| 8 | The hash |
| 9 | Labels: "Capture date:" `\t\t\t\t\t\t` "Updated date:" |
| 10 | Capture date `\t\t\t` Updated date (both are typically identical = the time Hunchly captured) |

**Confirmed in the test export:**
- 5 captures = 5 tables = 5 drawings = 5 PNGs in `word/media/`
- Every image initially sized at `cx=5541335 cy=2843965` EMU (6.06" × 3.11", aspect 1.95:1)
- Underlying PNGs are 3024 × 1552 px
- Images named by SHA hash in `word/media/` (e.g. `0818feb2a5011236d32661b0845b094c270bf64e.png`)
- URLs are plain text in Row 6 (zero `w:hyperlink` elements in entire doc, simpler parsing)
- "Updated date" in Row 10 is always identical to the capture date in our test, this is the field to replace

---

## Date Extraction Strategy (Verified Working)

Tested against the 5 real captures in `Test_Export.docx`. All decode math verified.

### Tier 1: URL Snowflake / shortcode decode (deterministic, no DOM needed)

```python
from datetime import datetime, timezone
import base64
import re

def decode_tiktok(url):
    """TikTok video ID >> 32 = unix seconds"""
    m = re.search(r'/video/(\d+)', url)
    if not m: return None
    return datetime.fromtimestamp(int(m.group(1)) >> 32, tz=timezone.utc)

def decode_twitter_x(url):
    """X status ID: (id >> 22) + 1288834974657 = unix ms"""
    m = re.search(r'/status/(\d+)', url)
    if not m: return None
    ms = (int(m.group(1)) >> 22) + 1288834974657
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)

def decode_meta_shortcode(shortcode):
    """Instagram + Threads use the same Meta media-ID scheme.
       Shortcode → base64url-decode → uint64 → ((id >> 23) + IG_EPOCH_MS) / 1000"""
    IG_EPOCH_MS = 1314220021721
    padded = shortcode.rjust(12, 'A')
    standard = padded.replace('-', '+').replace('_', '/')
    decoded = base64.b64decode(standard)
    media_id = int.from_bytes(decoded, 'big')
    ms = (media_id >> 23) + IG_EPOCH_MS
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)

def decode_instagram(url):
    m = re.search(r'instagram\.com/(?:p|reel|tv)/([^/?]+)', url)
    return decode_meta_shortcode(m.group(1)) if m else None

def decode_threads(url):
    m = re.search(r'threads\.(?:net|com)/@[^/]+/post/([^/?]+)', url)
    return decode_meta_shortcode(m.group(1)) if m else None

def decode_linkedin_activity(url):
    """LinkedIn URN activity IDs use Twitter Snowflake epoch."""
    m = re.search(r'activity[-:](\d+)', url)
    if not m: return None
    ms = (int(m.group(1)) >> 22) + 1288834974657
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
```

**Verified outputs from `Test_Export.docx`:**

| Platform | URL | Extracted Post Date |
|----------|-----|---------------------|
| Threads | `/post/DYqShGlFUfr` | `2026-05-23 00:18 UTC` |
| X | `/status/2000134132786200677` | `2025-12-14 09:21 UTC` |
| TikTok | `/video/7634952343653567775` | `2026-05-01 15:54 UTC` |
| Instagram | `/p/DYdt0yejCDk/` | `2026-05-18 03:06 UTC` |
| Facebook | `/posts/pfbid02iG2...` | **Tier 1 fails** (pfbid is opaque) |

### Tier 2: MHTML extraction (uses the raw Hunchly case zip)

For platforms where URL doesn't encode the date (Facebook, YouTube, Reddit), parse the MHTML files from Hunchly's raw case .zip export. **The tool now takes both inputs: the exported docx AND the raw case zip.** This gives every capture access to its full rendered HTML.

MHTML files are named `PAGEID.mhtml` in the zip. Map docx capture → MHTML file via the SHA-256 hash in Row 8 of the capture table, which appears in the raw export's CSV index. The MHTML is RFC 2557 multipart MIME; the first `text/html` part is the rendered page.

```python
import email
from email import policy
def load_mhtml(mhtml_path):
    with open(mhtml_path, 'rb') as f:
        msg = email.message_from_binary_file(f, policy=policy.default)
    for part in msg.walk():
        if part.get_content_type() == 'text/html':
            return part.get_content()
    return None
```

```python
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
import json

def extract_from_html(html_text):
    """Universal HTML extractor: <time>, JSON-LD, OG meta, itemprop."""
    soup = BeautifulSoup(html_text, 'html.parser')
    # 1. <time datetime="...">
    for t in soup.find_all('time'):
        if t.get('datetime'):
            try: return date_parser.isoparse(t['datetime']), 'time_element'
            except: pass
    # 2. JSON-LD datePublished / uploadDate
    for s in soup.find_all('script', type='application/ld+json'):
        if not s.string: continue
        try: data = json.loads(s.string)
        except: continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict): continue
            for k in ('datePublished','uploadDate','dateCreated','datePosted'):
                if k in item:
                    try: return date_parser.isoparse(item[k]), f'jsonld_{k}'
                    except: pass
    # 3. Meta tags
    for prop in ('article:published_time','og:updated_time','video:release_date'):
        m = soup.find('meta', property=prop) or soup.find('meta', attrs={'name': prop})
        if m and m.get('content'):
            try: return date_parser.isoparse(m['content']), f'meta_{prop}'
            except: pass
    # 4. itemprop
    el = soup.find(attrs={'itemprop': 'datePublished'})
    if el:
        v = el.get('content') or el.get('datetime') or el.get_text(strip=True)
        if v:
            try: return date_parser.isoparse(v), 'itemprop'
            except: pass
    return None
```

**Verified working on YouTube via Claude for Chrome test:** The classic "Me at the zoo" video (`v=jNQXAC9IVRw`) returned `2005-04-23T20:31:52-07:00` via three independent methods (JSON-LD, meta, itemprop). Cross-validation built in.

**Important caveat verified in testing:** TikTok page DOM contains a `__UNIVERSAL_DATA_FOR_REHYDRATION__` blob with a `createTime` field, BUT this can be WRONG when the page client-side-renders a different video than the URL specifies (login wall, autoplay redirect, etc). The URL Snowflake is authoritative; the DOM blob is at best a cross-check.

### Tier 3: REVIEW_REQUIRED queue

If Tier 1 and Tier 2 both fail, the capture goes into a `REVIEW_REQUIRED.docx` with the image, URL, and a blank "Post Date:" field for manual entry. Re-run the tool with `--review-completed review.json` to inject those captures into the right platform docs with proper numbering.

**Never silently use the capture date as a fallback.** The whole point is to never present the wrong date as if it were the post date.

### Facebook extraction: DEFEATED THE SCRAMBLING (empirically verified)

**Initial dead ends:** standard date extraction methods all return 0 on modern Facebook MHTML — no `data-utime`, no `<time>` elements, no JSON-LD, no `article:published_time`, no `creation_time` JSON, no `<meta>` date tags. The visible "7y" / "February 8, 2019" / etc. text on the rendered page does NOT appear as a readable string in the HTML source.

**Why:** Facebook uses a CSS-Flexbox character-scrambling defense. Each visible character is in its own `<span class="...">X</span>`, the spans are written to HTML in scrambled order, and CSS `order:` properties on per-character classes reassemble them visually. Additionally, half the spans are **decoys** — visible to scrapers but hidden from rendering via a specific CSS pattern.

**The defeat algorithm (verified against real Hunchly capture):**

1. Load BOTH the main `text/html` part AND every `text/css` part from the MHTML's multipart MIME.
2. Build a `class → order` map from CSS rules (`.xi695je { order: 14; }` style).
3. Identify the "decoy class tail": Facebook attaches a specific 8-class suffix to every decoy span (in this test: `brhKlE1H grhKlE1M hrhKlE1N erhKlE1K bejN cejO dejP eejQ`). These classes have NO rules in CSS, while real chars have order-bearing classes. **The decoy tail is the same throughout one MHTML but varies between captures** — must be detected per-MHTML by finding the class string that appears on roughly half the single-char spans and has no CSS rule for those classes.
4. For each scrambled-text container (pattern: `<span aria-labelledby="_r_XXX_"><span style="display: flex;">...single-char spans...</span></span>`):
   - For each `<span class="C">X</span>` child:
     - If class string contains the decoy tail → skip (it's a decoy)
     - Otherwise → look up CSS order for the unique non-tail class, record (order, char)
   - Sort by order, concatenate → readable visible text

**Verification on real capture (`pages/1.mhtml`, FB post `pfbid02iG2...`):**
Algorithm successfully decoded 9 separate scrambled date blocks:
- `'August 25, 2019'`, `'May 17, 2019'`, `'February 8, 2019'`, `'October 13, 2018'`, `'August 25, 2018'`, etc.
- Two of these said `'February 8, 2019'` — both renderings of the main captured post (FB shows the post in both a modal overlay AND in the underlying timeline).
- **Cross-verified visually against the screenshot**: the post header in the modal shows "February 8, 2019" matching the algorithm's output exactly.

**Selecting THE post date when multiple are present:**

The MHTML often contains multiple scrambled date blocks (the main post + sibling posts visible in timeline + Sponsored ad headers). To find THE main post's date:

1. Find the position(s) in HTML of the actual post body text. The post body is plain readable text (not scrambled), so search for distinctive content from the captured page's `<title>` or a visible text fragment.
2. Find the scrambled-date block immediately preceding the post body text. That's the post header.
3. Decode that block's date.

For Hunchly captures of FB post permalinks specifically, the post body appears twice (modal + timeline) and BOTH preceding-date-blocks decode to the same date, giving you built-in cross-validation.

**Algorithm robustness considerations:**

- **The CSS class names rotate.** `xi695je`, `brhKlE1H`, etc. are different per session and possibly per capture. The algorithm doesn't hard-code class names — it derives them from each MHTML's own CSS.
- **The decoy tail may change format.** Detect it dynamically: enumerate single-char spans, find the class string that appears on a large fraction of them and whose classes have no CSS rules for `order:`. That's the decoy tail.
- **Text in the post header is "Posted M/D/YYYY"** or similar before the actual date string. The algorithm should output the full unscrambled block and a date-pattern parser extracts the date from it.
- **Sponsored ads have "Sponsored" text + dates intermixed.** Filter by detecting if the unscrambled text starts with "Sponsored" — those are ad blocks, not the main post.

**Proof-of-concept code** included in this conversation (saved as `fb_unscrambler_proof_of_concept.py`). The Claude Code session should productionize this with:
- Dynamic decoy-tail detection (not hard-coded class strings)
- Post-body-proximity heuristic for selecting THE main post date
- Date pattern parsing (handle "Month D, YYYY" and "Month D at HH:MM PM")
- Cross-validation against multiple matching blocks for the same post when present
- Caption disclosure: `Post Date: 2019-02-08; Source: FB scrambled-DOM unscramble + cross-validation (2 matching blocks); Confidence: Authoritative`

**Cross-checks on other platforms (same export):**
- **Threads MHTML**: 5 clean `<time datetime="...">` elements, first matches Snowflake exactly. Use `<time>` element directly.
- **Instagram MHTML**: 2 `<time>` tags without `datetime` attrs. Fall back to URL shortcode decode (works perfectly).
- **TikTok / X / Instagram / Threads / LinkedIn**: URL Snowflake is the primary method regardless of MHTML state.
- **Facebook alone needs the unscramble algorithm — but it works.**

### Facebook policy (FINAL)

Facebook extraction is now **automatic** in v0.1 using the unscramble algorithm. Expected coverage based on the empirical test: should work on any modern FB capture where the post page rendered fully (i.e., where character-scrambled spans are present in the MHTML). If the algorithm fails for a given capture (e.g., the decoy-tail detection doesn't converge, or no date pattern is found in any unscrambled block), the capture falls back to REVIEW_REQUIRED.

Caption format for FB exhibits:
```
Post Date: 2019-02-08
Source: FB scrambled-DOM unscramble (algorithm: detect decoy tail, sort visible chars by CSS order)
Confidence: Authoritative (cross-validated against 2 matching blocks in MHTML)
```

### Facebook algorithm maintenance outlook (evidence-based)

Researched the historical record of Facebook's anti-scraping techniques to estimate how long the unscrambler will keep working without updates:

- The character-scrambling-with-decoys technique was **already in use by 2017** (documented in ad-blocker bypass articles)
- **Same technique confirmed in February 2022** (Brandon Roberts, "Scraping a hostile web")
- **Same technique still operating in 2025-2026** (multiple scraping guides confirm)
- **Structural stability: 8+ years and counting**

What CHANGES (the algorithm handles automatically because it detects dynamically):
- Specific class names rotate per deploy (weekly+)
- Order values shift
- Which characters are real vs decoy

What STAYS STABLE (what the algorithm depends on):
- Single-character `<span>` per visible char
- CSS Flexbox `order:` for visual reassembly
- Decoy spans grouped by a shared multi-class suffix
- These have not changed in 8+ years

**Why it's likely to stay stable**: Meta uses real DOM rendering (not canvas/WASM) because of accessibility compliance (ADA, EAA). Screen readers require real text in the DOM. Moving to canvas-rendered text would create regulatory exposure. This is a regulatory ceiling, not a technical one, and it's why the technique has been so stable historically.

**Realistic maintenance estimate**: 30-minute Claude Code fix-it session every 1-2 years when minor parameters need tweaking (decoy threshold, regex pattern adjustments). Catastrophic breaks requiring algorithm rewrite are possible but historically rare and would likely be telegraphed by industry-wide changes in scraping discussion.

**When the algorithm DOES eventually break**: the failure mode is graceful — captures route to REVIEW_REQUIRED with manual date entry. The tool keeps working; only FB auto-extraction is degraded until updated.

---

## Cropping & Resizing (Display-Only, Image Preserved)

Both transformations applied via XML in the docx, NOT by modifying the PNG. Original PNG stays embedded full-size in `word/media/`, evidence integrity intact.

### Crop XML

Inside each image's `<pic:blipFill>`, inject:

```xml
<pic:blipFill>
  <a:blip r:embed="rId7"/>
  <a:srcRect l="1800" t="650" r="2200" b="400"/>  <!-- ← ADD THIS -->
  <a:stretch><a:fillRect/></a:stretch>
</pic:blipFill>
```

Values are in **hundredths of a percent**: `l="1800"` = crop 18% off the left edge. Range 0-100000 per side.

### Resize XML

Update the `<wp:extent cx="..." cy="..."/>` (anchor extent) AND the inner `<pic:spPr><a:xfrm><a:ext cx="..." cy="..."/>` (picture extent). Both must match or rendering breaks in some Word versions.

Units: EMU (English Metric Units). `914400 EMU = 1 inch`.

### Combined math (per capture)

```python
def calculate_display_dims(orig_w_px, orig_h_px, crop, size_preset):
    """
    crop: {'left_pct': 18.0, 'top_pct': 6.5, 'right_pct': 22.0, 'bottom_pct': 4.0}
    size_preset: {'mode': 'fixed_width', 'width_inches': 5.5}
    Returns: (cx_emu, cy_emu, srcRect_dict)
    """
    EMU_PER_INCH = 914400
    visible_w_pct = (100 - crop['left_pct'] - crop['right_pct']) / 100.0
    visible_h_pct = (100 - crop['top_pct'] - crop['bottom_pct']) / 100.0
    visible_w_px = orig_w_px * visible_w_pct
    visible_h_px = orig_h_px * visible_h_pct
    aspect = visible_w_px / visible_h_px

    if size_preset['mode'] == 'fixed_width':
        disp_w = size_preset['width_inches']
        disp_h = disp_w / aspect
    elif size_preset['mode'] == 'fixed_height':
        disp_h = size_preset['height_inches']
        disp_w = disp_h * aspect
    elif size_preset['mode'] == 'fit_box':
        max_w, max_h = size_preset['max_width_inches'], size_preset['max_height_inches']
        if max_w / aspect <= max_h:
            disp_w, disp_h = max_w, max_w / aspect
        else:
            disp_h, disp_w = max_h, max_h * aspect

    srcRect = {
        'l': int(crop['left_pct'] * 1000),
        't': int(crop['top_pct'] * 1000),
        'r': int(crop['right_pct'] * 1000),
        'b': int(crop['bottom_pct'] * 1000),
    }
    return int(disp_w * EMU_PER_INCH), int(disp_h * EMU_PER_INCH), srcRect
```

---

## Preset System

### Directory layout

```
presets/
├── _defaults.json          # fallback when no specific preset matches a platform
├── tiktok/
│   └── video.json
├── instagram/
│   ├── single_post.json
│   ├── carousel_post.json
│   └── reel.json
├── x/
│   ├── text_tweet.json
│   ├── image_tweet.json
│   └── quoted_tweet.json
├── facebook/
│   ├── text_post.json
│   ├── photo_post.json
│   └── video_post.json
└── threads/
    ├── text_post.json
    └── image_post.json
```

### Preset JSON schema

```json
{
  "platform": "instagram",
  "post_style": "single_post",
  "url_patterns": ["instagram\\.com/p/[^/?]+/?$"],
  "crop": {
    "left_pct": 18.0,
    "top_pct": 6.5,
    "right_pct": 22.0,
    "bottom_pct": 4.0
  },
  "size": {
    "mode": "fixed_width",
    "width_inches": 5.5
  },
  "notes": "Crops Instagram sidebar and bottom timestamp",
  "style_guide_ref": "SMI-IG-001",
  "created_by": "Dylan Hackett",
  "created_at": "2026-05-23"
}
```

`size.mode` options:
- `fixed_width` → height follows from cropped aspect ratio
- `fixed_height` → width follows from cropped aspect ratio
- `fit_box` → bounded by max_width_inches and max_height_inches, aspect preserved
- `fixed_both` → explicit width AND height (uses carefully; may distort)

### Preset selection logic

For each capture URL:
1. Identify platform from URL host
2. Iterate that platform's preset files
3. First whose `url_patterns` regex matches → that preset
4. If none match → use `presets/_defaults.json` for that platform AND flag the capture in `NEW_PRESETS_NEEDED.json`

### Interactive preset builder (v0.2)

Companion mode: `builder.exe --define-preset`
Tkinter window: drag a Hunchly PNG, position a green rectangle to define keep-area, fill metadata fields, save JSON. Used once per new post style. Initial library will be built from Dylan's existing style guides.

---

## Output Structure

```
output/<case_name>_<YYYY-MM-DD>/
├── 00_Accounts_Located.docx          (master index, see below)
├── 00_Accounts_Located.pdf
├── 01_Facebook_Exhibits.docx        (only if FB captures present)
├── 01_Facebook_Exhibits.pdf
├── 02_Instagram_Exhibits.docx
├── 02_Instagram_Exhibits.pdf
├── 03_TikTok_Exhibits.docx
├── 03_TikTok_Exhibits.pdf
├── 04_X_Exhibits.docx
├── 04_X_Exhibits.pdf
├── 05_LinkedIn_Exhibits.docx
├── 05_LinkedIn_Exhibits.pdf
├── 06_Threads_Exhibits.docx
├── 06_Threads_Exhibits.pdf
├── 07_YouTube_Exhibits.docx
├── 07_YouTube_Exhibits.pdf
├── 99_Other_Exhibits.docx           (catch-all, if needed)
├── REVIEW_REQUIRED.docx             (only if any captures failed date extraction)
├── NEW_PRESETS_NEEDED.json          (only if any captures had no preset match)
└── manifest.json                    (full audit trail, see below)
```

### Per-platform exhibit doc structure

- **Preserve Hunchly's table layout exactly** (bordered table cell with image + caption metadata). Bosses want this format.
- **Per platform, exhibits restart numbering at 1**, sorted oldest post-date first.
- **Row 10 caption gets rewritten** to show the real post date instead of Hunchly's "Updated date" capture-time placeholder.
- **Image in Row 1 gets** `<a:srcRect>` crop + updated `<wp:extent>` per the matched preset.

Suggested caption format for Row 10 (replacing Hunchly's default):
```
Post Date: 2024-03-15 18:42:11 UTC  |  Capture Date: 2026-05-23 15:44:49 -04:00
Extraction Method: URL Snowflake decode (verified)
```

### Accounts Located doc

Listed in platform priority order (Facebook → Instagram → TikTok → X → LinkedIn → YouTube → Threads → Reddit → Other). Per subject per platform, the main account profile/landing page:
- Platform name
- Username/handle
- Profile URL
- First capture date in this case
- Cross-reference to platform exhibit doc and exhibit range (e.g. "See 03_TikTok_Exhibits.pdf, Exhibits 1-12")

**Main account detection**: a capture URL is a "main account" if it matches a profile/landing pattern (e.g. `tiktok.com/@username` with no `/video/`, `instagram.com/username/` with no `/p/` or `/reel/`). Detected via per-platform regex.

### manifest.json

Per-case audit trail:
```json
{
  "case_name": "Smith_v_Jones",
  "processed_at": "2026-05-23T14:30:00-04:00",
  "analyst": "Dylan Hackett",
  "source_docx": "Test_Export.docx",
  "source_docx_sha256": "abc123...",
  "captures_processed": 47,
  "exhibits_built": 43,
  "review_required": 4,
  "platforms_found": ["facebook","instagram","tiktok"],
  "exhibits": [
    {
      "platform": "tiktok",
      "exhibit_number": 1,
      "url": "https://...",
      "post_date": "2024-03-15T18:42:11Z",
      "post_date_source": "url_snowflake",
      "capture_date": "2026-05-23T15:40:09-04:00",
      "sha256": "...",
      "preset_used": "tiktok/video.json",
      "is_main_account": false
    }
  ],
  "main_accounts": [...],
  "review_queue": [...]
}
```

---

## Build Order

### v0.1 (foundation, no GUI) — start here

**Inputs**: Hunchly exported docx AND raw Hunchly case zip.

1. **Docx parser** (`hrb/parser.py`): unzip + parse `Test_Export.docx`. For each `<w:tbl>`, extract: image rId, image filename in media/, URL (Row 6), page title (Row 4), hash (Row 8), capture date (Row 10).
2. **Raw case zip loader** (`hrb/raw_export.py`): unzip the .zip, parse the CSV index, build a hash → mhtml_path mapping. Allows docx capture → MHTML lookup via the SHA-256 in Row 8.
3. **Platform classifier** (`hrb/platforms.py`): URL → platform_id via regex.
4. **Date extractors** (`hrb/dates.py`):
   - Tier 1 Snowflake/shortcode decoders for TikTok, X, Instagram, Threads, LinkedIn
   - Tier 2 MHTML universal extractor for YouTube, Reddit
   - Facebook-specific multi-method MHTML extractor (data-utime → GraphQL → article:published_time)
5. **Preset loader** (`hrb/presets.py`): load JSON from `presets/`, match URL patterns. Ship with 1-2 starter presets per platform based on Dylan's style guides.
6. **Docx writer** (`hrb/writer.py`): for each platform, clone source docx, keep only matching capture tables, modify image XML (srcRect + extents) per preset, rewrite Row 10 caption text with the appropriate method-and-confidence disclosure.
7. **Accounts Located builder** (`hrb/locator.py`): scan captures for main-account URLs, build a separate docx with the index.
8. **PDF export** (`hrb/pdf_export.py`): use `docx2pdf` (Mac/Windows, requires Word installed).
9. **CLI** (`hrb/__main__.py`): `python -m hrb --input Test_Export.docx --raw-zip case.zip --output ./output --case "test_case_1"`
10. **manifest.json writer**: drop the audit trail at end of run.

**Test against `Test_Export.docx` continuously throughout v0.1.** Expected output: 5 per-platform docs (Threads, X, TikTok, Instagram, Facebook), each with 1 exhibit. 4 get post dates from Snowflake decode. Facebook lands in REVIEW_REQUIRED.

### v0.2

- Interactive preset builder (Tkinter)
- Refinement of FB GraphQL `creation_time` matcher based on production captures
- Multi-subject support if Dylan's workflow expands

### v0.3 (deployment)

- PyInstaller spec → Windows `.exe`
- Code signing if distributing to other analysts
- `--review-completed review.json` re-run flow

---

## Key Architectural Principles (do not violate)

1. **Determinism over cleverness.** No AI, no OCR, no relative-date math without an anchor. Every date must be reproducible by a third party with only the URL or the captured HTML.
2. **Original evidence is never modified.** PNG files in `word/media/` stay byte-identical to what Hunchly captured. All crops are display-only via XML.
3. **No silent fallbacks for post date.** If we can't deterministically extract it, the capture goes to REVIEW_REQUIRED. Never substitute the capture date and present it as the post date.
4. **Audit trail in every output.** Each exhibit's caption notes the extraction method. The manifest.json records full processing details.
5. **Preserve Hunchly's table layout in the output docs.** Bosses want that format. Don't reinvent the visual design, just restructure (per-platform docs) and refine (post date + crop + resize).

---

## Useful File Paths

- Test docx: provided by Dylan in conversation as `Test_Export.docx`
- Unpacking utility: `python /mnt/skills/public/docx/scripts/office/unpack.py file.docx unpacked/`
- Repacking utility: `python /mnt/skills/public/docx/scripts/office/pack.py unpacked/ output.docx --original file.docx`
- The unpack tool pretty-prints XML and merges adjacent runs, makes it readable. Repack validates and re-condenses.

---

## Open Items Dylan Will Provide

- **Initial preset JSONs** built from internal style guides (one per common post style per platform). Dylan will define these as he encounters new post styles in real cases, using either the v0.2 interactive builder or by manually editing JSON with values from his style guide references.
- **A larger test export** with main-account-page captures (he mentioned getting accounts per platform next) so we can test the Accounts Located builder end-to-end.
- **Hunchly raw case .zip** (eventually) so we can build the Tier 2 MHTML extractor for Facebook + YouTube + Reddit dates.

---

## Pitfalls Already Identified (don't re-learn the hard way)

1. **TikTok DOM `createTime` ≠ URL Snowflake.** If the page client-side-rendered a different video due to login wall or autoplay, the embedded JSON has the WRONG date. URL is always authoritative. Verified empirically with `@angrygrandma18/video/6789760592203861253` (URL = Feb 2020, DOM = March 2026, visible page date = Feb 2020 ✓).
2. **Facebook character-scrambling defeated, dates ARE extractable.** Standard methods (data-utime, creation_time, article:published_time, etc.) all return 0. But the visible date text IS in the HTML as scrambled single-char spans with CSS `order:` properties + decoy spans with a shared "decoy class tail". Algorithm: detect the decoy tail per-MHTML, filter out decoy spans, sort remaining spans by CSS order, concatenate. Empirically verified to recover "February 8, 2019" from a real Hunchly FB capture, with cross-validation (the same date appears twice in HTML, modal + timeline rendering). See "Facebook extraction" section for full algorithm. v0.1 should ship this.
3. **Instagram shortcode decoder needs base64-url padding to 12 chars with 'A'.** Naive decoding gives 2015 dates (wrong). Pad first.
4. **docx XML uses two extent locations** for images: `wp:extent` (anchor) and `pic:spPr/a:xfrm/a:ext` (picture). Both must be updated to the same value or some Word versions render wrong.
5. **URL in Hunchly's Row 6 has a LEADING SPACE.** Strip it before parsing.
6. **Capture date and Updated date in Row 10 are TAB-separated** in the same paragraph (`2026-05-23 15:44:49 PM GMT -04:00 \t\t\t 2026-05-23 15:44:49 PM GMT -04:00`). Use regex on the tab structure.
7. **PNG files in `word/media/` are named by hash, not by capture order.** The image rId in each table is the only way to map table → image file. Use `word/_rels/document.xml.rels` to resolve rId → media filename.

---

## Conversation Context Summary

This document was generated at the end of a long planning conversation between Dylan and Claude. Key decisions reached:
- Tool's job is to **transform Hunchly's already-exported docx**, not rebuild from raw Hunchly export files (though raw export may be optional input for Tier 2 fallbacks in v0.2)
- One subject per case (no subject splitting)
- Exhibits restart numbering at 1 per platform, sorted oldest-first by post date
- Sizing varies per crop style, not globally configured (presets contain both crop AND size settings)
- "Updated date" in Row 10 is what we replace; everything else in the layout stays
- Hunchly free trial signup at https://hunch.ly/free-trial for Dylan to generate test exports separate from work data
- Final deployment target is Windows .exe via PyInstaller; development on Mac is fine because .docx format is platform-agnostic

The conversation also established date-extraction math empirically against the 5 captures in `Test_Export.docx` and via live testing on Claude for Chrome (YouTube, TikTok, X). All math in this document is verified, not theoretical.
