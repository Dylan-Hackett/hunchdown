# Hunchly Report Builder

Transforms a Hunchly OSINT export into a clean set of per-platform exhibit
documents with real post dates substituted for Hunchly's capture-time "Updated
date" field.

**No AI calls. No OCR. Fully deterministic.** Every transformation is
reproducible by a third party with only the original Hunchly export.

---

## What it does

Given:
- A Hunchly-exported `.docx` (the "Report" file from a Hunchly case)
- The matching Hunchly raw case `.zip` (provides MHTML for Tier 2 date extraction)

Produces:
- **`00_Accounts_Located.docx`** — master index of each subject's main account
  per platform, using the full Hunchly capture table (image + URL + hash +
  dates), one capture per page.
- **`NN_<Platform>_Exhibits.docx`** — one doc per platform, each containing
  that platform's post captures sorted oldest-first by real post date, one
  exhibit per page, with `<a:srcRect>` crop + `<wp:extent>` resize applied
  per the matched preset.
- **`REVIEW_REQUIRED.docx`** (when applicable) — captures whose post date
  could not be deterministically extracted, ready for manual date entry.
- **`NEW_PRESETS_NEEDED.json`** (when applicable) — URLs that fell through to
  the platform default preset, so you know which post styles still need a
  proper preset defined.
- **`manifest.json`** — full per-run audit trail: source file hashes,
  per-exhibit extraction method + confidence, preset used, capture date,
  post date, and review queue.

The original PNGs in `word/media/` are **never modified**. All visual changes
are display-only XML transformations, so chain of custody on the underlying
evidence is intact.

---

## Install

Requires Python 3.12+ and [uv](https://github.com/astral-sh/uv).

```bash
git clone <repo>
cd hunchdown
uv sync
```

PDF export requires Microsoft Word installed (via `docx2pdf`). On a machine
without Word, pass `--no-pdf` and only the `.docx` files are produced.

---

## Usage

```bash
uv run python -m hrb \
    --input "Case_Export.docx" \
    --raw-zip "Case_Raw.zip" \
    --output ./output \
    --case smith_v_jones
```

Flags:

| flag | required | description |
|---|---|---|
| `--input` | yes | Path to the Hunchly-exported `.docx` |
| `--raw-zip` | no | Path to the Hunchly raw case `.zip`. Required for Tier 2 date extraction (Facebook + any platform whose URL doesn't carry a post ID). |
| `--output` | no | Output root dir (default `./output`) |
| `--case` | yes | Case slug; used in output folder name + doc title |
| `--presets` | no | Presets dir (default `./presets`) |
| `--no-pdf` | no | Skip PDF export (docx only) |

Output lands in `<output>/<case>_<YYYY-MM-DD>/`.

---

## Date extraction (three tiers)

Post dates are extracted via a three-tier cascade. The first tier that
returns a date wins; no fallback to capture date is ever used.

**Tier 1 — URL Snowflake / shortcode decode (no DOM, deterministic):**

| platform | encoding | method |
|---|---|---|
| TikTok | video ID Snowflake | `(id >> 32)` = unix seconds |
| X / Twitter | status ID Snowflake | `((id >> 22) + 1288834974657)` = unix ms |
| Instagram | shortcode base64 | Meta media-ID scheme |
| Threads | shortcode base64 | Meta media-ID scheme |
| LinkedIn | activity URN | Twitter Snowflake epoch |

**Tier 2 — MHTML parse (uses raw case zip):**

- Universal cascade: `<time datetime="...">` → JSON-LD `datePublished` → `<meta property="article:published_time">` → `itemprop="datePublished"`.
- **Facebook unscrambler:** modern FB scrambles dates as per-character `<span>`s
  reassembled via CSS Flexbox `order:`, with decoy spans sharing a fixed
  multi-class suffix. The extractor reads HTML + all CSS parts from the
  MHTML, detects the decoy tail dynamically, filters decoys, sorts visible
  spans by their CSS `order` value, and concatenates. Handles both
  "Month D, YYYY" and "Month D at HH:MM PM" (year inferred from capture
  date, with future-date rollback to previous year).

**Tier 3 — REVIEW_REQUIRED queue:** if both tiers fail, the capture lands in
`REVIEW_REQUIRED.docx` with the image + URL and a placeholder date for
manual entry.

Every exhibit's extraction method + confidence is recorded in `manifest.json`.

---

## Preset system

Presets define the crop + display size applied to each capture's image. They
live in `presets/`:

```
presets/
├── _defaults.json                  # fallback per platform (preserve, no crop)
├── facebook/
│   └── main_account.json
├── instagram/
│   ├── main_account.json
│   └── single_post.json
├── tiktok/
│   └── main_account.json
├── x/
│   └── main_account.json
└── threads/
    └── main_account.json
```

Each preset JSON:

```json
{
  "platform": "instagram",
  "post_style": "single_post",
  "url_patterns": ["instagram\\.com/p/[^/?#]+/?"],
  "crop": {
    "left_pct": 15.0,
    "top_pct": 10.0,
    "right_pct": 15.0,
    "bottom_pct": 8.0
  },
  "size": {
    "mode": "preserve",
    "width_inches": 6.0
  }
}
```

`size.mode` options:

| mode | behavior |
|---|---|
| `preserve` | Keep the table's existing extent. With a non-zero crop, width is kept and height is recomputed for the cropped aspect ratio. |
| `fixed_width` | Scale to `width_inches`; height follows the cropped aspect. |
| `fixed_height` | Scale to `height_inches`; width follows the cropped aspect. |
| `fit_box` | Bounded by `max_width_inches` + `max_height_inches`, aspect preserved. |
| `fixed_both` | Explicit width AND height (may distort if combined with a crop that changes aspect). |

Selection: for each capture URL, the first preset under that platform's
directory whose `url_patterns` regex matches wins. If none match, the URL
falls through to `_defaults.json` and is logged in `NEW_PRESETS_NEEDED.json`
so you know which post style still needs a preset.

### Building a preset the easy way

You don't have to compute crop percentages by hand:

1. Run the tool with a default preset to produce a draft exhibit doc.
2. Open the doc in Word, select the image, **Picture Format → Crop**, drag
   handles to your preferred framing, hit Enter, then resize via
   **Picture Format → Size** to your preferred display dimensions.
3. Save the file.
4. Read the `<a:srcRect>` and `<wp:extent>` values out of the resulting
   XML — these are exactly what the preset JSON needs.

Word stores its crop the same way OOXML stores ours, so cropping visually in
Word is equivalent to writing the JSON by hand.

---

## Output structure

```
output/<case>_<YYYY-MM-DD>/
├── 00_Accounts_Located.docx       (master index, one capture per page)
├── 00_Accounts_Located.pdf
├── 01_Facebook_Exhibits.docx
├── 01_Facebook_Exhibits.pdf
├── 02_Instagram_Exhibits.docx
├── 02_Instagram_Exhibits.pdf
├── 03_TikTok_Exhibits.docx
├── 03_TikTok_Exhibits.pdf
├── 04_X_Exhibits.docx
├── 04_X_Exhibits.pdf
├── 05_LinkedIn_Exhibits.docx
├── 06_Threads_Exhibits.docx
├── 07_YouTube_Exhibits.docx
├── REVIEW_REQUIRED.docx           (if any captures need manual date entry)
├── NEW_PRESETS_NEEDED.json        (if any URLs hit the default preset)
└── manifest.json                  (full audit trail)
```

Within each platform doc:

- Exhibits are sorted **oldest-first by real post date**.
- Each exhibit gets its own page (hard page break between exhibits).
- The original Hunchly capture table layout is preserved exactly.
- Row 10 (the date row) has its "Updated date" value replaced with the real
  post date; the capture date is left untouched. Tab alignment is preserved.

---

## Architecture

```
hrb/
├── __main__.py        CLI + manifest writer + orchestration
├── parser.py          Hunchly docx → Capture records (10-row table layout)
├── raw_export.py      Hunchly raw case zip → hash-to-MHTML index
├── platforms.py       URL → platform_id, handle extraction, main-account detection
├── dates/
│   ├── __init__.py    3-tier cascade + URL Snowflake/shortcode decoders
│   ├── facebook.py    FB CSS-Flexbox character-scramble defeat
│   └── mhtml_universal.py   <time> / JSON-LD / meta / itemprop cascade
├── presets.py         Preset library + crop/size math
├── writer.py          Docx clone + XML surgery (srcRect, extent, caption row)
└── pdf_export.py      Optional docx → pdf via docx2pdf
```

Key principle: **the source docx is the template.** The writer clones the
source zip, replaces `word/document.xml` with a new body containing only the
filtered + reordered tables (with image XML modified in place), and writes
the result. The PNGs in `word/media/` are copied through byte-identical.

---

## Limitations

- Currently assumes **one subject per case**. Multi-subject support is on
  the roadmap.
- PDF export depends on Microsoft Word being installed (Mac or Windows).
- Tier 2 MHTML extraction requires the raw case `.zip` from Hunchly. Without
  it, Facebook and any non-ID-bearing URLs land in `REVIEW_REQUIRED.docx`.
- Facebook unscrambler depends on FB continuing to use real DOM with CSS
  Flexbox `order:` (stable for 8+ years, but could change). On failure it
  routes captures to REVIEW_REQUIRED — the tool keeps working.

---

## License

Private. Not for redistribution.
