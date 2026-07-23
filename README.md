# Hunchly Report Builder

Transforms a Hunchly OSINT export into a clean set of per-platform exhibit
documents with real post dates substituted for Hunchly's capture-time "Updated
date" field, cropped/resized per style presets, and (optionally) the actual
post videos downloaded alongside.

**No AI. No OCR.** Date extraction and cropping are deterministic from the
export. Two features do make live network calls and are disclosed as such: the
video downloads, and the yt-dlp `creation_time` date (used only where the
export itself carries no date — see below).

---

## What it does

Given:
- A Hunchly-exported `.docx` (the "Report" file from a Hunchly case)
- The matching Hunchly raw case `.zip` (provides MHTML for date extraction)

Produces (in `output/<case>/`):
- **`Accounts Located.docx`** — master index of each subject's main account per
  platform, one capture per page.
- **`<Platform>.docx`** — one doc per platform (Facebook, Instagram, TikTok, X,
  …), each containing that platform's post captures sorted oldest-first by real
  post date, one exhibit per page, cropped + resized per the matched preset.
- **`Review Required.docx`** (when applicable) — captures whose post date could
  not be extracted, ready for a manual date entry.
- **`New Presets Needed.json`** (when applicable) — URLs that fell through to the
  platform default preset.
- **`manifest.json`** — full per-run audit: source hashes, per-exhibit
  extraction method + confidence, preset used, crop source, and video downloads.
- **`videos/`** — the downloaded post videos, named to match their exhibit slot
  (`<Platform> Video Item <N> (<date>).mp4`), each with a JSON chain-of-custody
  sidecar. Or **`download_list.csv`** instead, if you're downloading on a
  separate machine (see [VM split](#downloading-on-a-separate-machine)).

The original PNGs in `word/media/` are **never modified** — all cropping is
display-only XML, so the underlying captured evidence stays byte-identical.

---

## Install

Requires Python 3.12+ and [uv](https://github.com/astral-sh/uv).

```bash
git clone <repo>
cd hunchdown
uv sync
```

- **PDF export** needs Microsoft Word (via `docx2pdf`). Without it, pass
  `--no-pdf` for docx only.
- **Video download** needs **ffmpeg** on PATH (transcodes VP9-only reels to
  H.264 and merges audio/video). Without it, pass `--no-download-videos`.

---

## Usage

Shortest form — a subject name resolves the matching docx + zip and names the
case (production files are named `<Subject> Hunchly.docx` / `.zip`):

```bash
uv run python -m hrb "John Smith"
```

Whole-folder batch (every `<name> hunchly.docx` + its zip in the directory):

```bash
uv run python -m hrb --batch
```

Explicit form:

```bash
uv run python -m hrb --input "Case.docx" --raw-zip "Case.zip" --case "John Smith"
```

Flags:

| flag | description |
|---|---|
| `target` (positional) | Subject name or `.docx` path; resolves the docx + zip + case name. |
| `--batch [DIR]` | Process every docx+zip pair in `DIR` (default `.`). |
| `--input` / `--raw-zip` / `--case` | Explicit inputs (use instead of a target/batch). |
| `--output` | Output root (default `./output`). Each case lands in `output/<case>/`. |
| `--presets` | Presets dir (default `./presets`). |
| `--no-pdf` | Skip PDF export (docx only). |
| `--no-download-videos` | Skip video downloads (build docx + dates only). |
| `--emit-download-list` | Don't download here; write `download_list.csv` for a VM to run. |

Output lands in `output/<case>/` — a **stable per-case folder** (no date stamp),
so re-running after peer review reuses it (see
[re-runs](#re-runs-after-peer-review)).

---

## Date extraction

Post dates come from a tiered cascade — the first tier that returns a date wins,
and the capture date is **never** used as a fallback. Every exhibit's method and
confidence is recorded in `manifest.json`.

| tier | source | notes |
|---|---|---|
| 0 | **Analyst note** | `Post Date: YYYY-MM-DD` typed into the Hunchly capture's note. Manual override; wins over everything. |
| 1 | **URL decode** | Snowflake / shortcode in the URL. Deterministic + reproducible. TikTok `(id>>32)`, X/LinkedIn Snowflake, Instagram/Threads Meta shortcode. |
| 2 | **yt-dlp `creation_time`** | The platform's real upload time, fetched live from the URL. **Primary for Facebook/Instagram videos** — modern FB/IG strip `creation_time` from the saved MHTML (Chrome's MHTML drops all `<script>` tags, where it lives), so reels often have *no* date in the export at all. Probed lazily, only when the URL didn't decode. Live fetch → disclosed, not reproducible from the export alone. |
| 3 | **MHTML parse** | From the raw zip. Universal cascade (`<time>` / JSON-LD / meta / itemprop) plus the **Facebook unscrambler** (defeats FB's CSS-Flexbox character-scramble of the visible date). |
| — | **REVIEW_REQUIRED** | If all fail, the capture goes to `Review Required.docx` for manual entry. |

For Facebook specifically: video reels get their date from tier 2 (yt-dlp), feed
posts from the tier-3 unscrambler, and `/photo/` viewer pages (no video, no
rendered date) fall to REVIEW_REQUIRED — handle those with a note.

---

## Video download

Every downloadable post (TikTok/IG/YouTube/Facebook/X) has its video pulled from
the live URL via yt-dlp and saved to `videos/`:

- **Named to its exhibit slot:** `<Platform> Video Item <N> (<post-date>).mp4`,
  where `N` is the exhibit number in that platform's deliverable doc.
- **Always H.264-in-.mp4** so it plays in Word/QuickTime/WMP. VP9/AV1-only
  sources (some FB/IG reels) are transcoded; native H.264 is kept as-is.
- **Chain of custody:** each video gets a `.json` sidecar (source URL, download
  timestamp, yt-dlp version, output SHA-256, and the originating capture's
  SHA-256). It's supplementary preservation, fetched live — never conflated with
  the Hunchly capture.
- Image-only posts record `no_video`; network/removed-post failures record
  `error`. Downloads never crash the run.

### Re-runs after peer review

Re-running on an updated export (reviewers added posts) is incremental: videos
already downloaded are **reused and renumbered** to their new slot (matched by
the capture SHA-256), and only genuinely-new posts are downloaded. Videos whose
post was removed move to `videos/_unused/` (never deleted). This is why the
output folder is stable per case.

### Downloading on a separate machine

If policy requires downloads on a different box (e.g. a VM), split it:

1. **Local:** `python -m hrb "John Smith" --emit-download-list` — builds the docx
   and writes `download_list.csv` (final filenames + dates already computed,
   including FB creation_times via the local metadata probe). No downloads here.
2. **VM:** copy `download_list.csv` + `download_from_list.py` over (the script is
   self-contained — only needs yt-dlp + ffmpeg), then:
   ```
   python download_from_list.py download_list.csv --output ./videos
   ```
   (add `--cookies cookies.txt` if the VM's IP needs login for FB/IG). Videos
   come out already correctly named.
3. Copy the VM's `videos/` folder back into `output/<case>/videos/`.

The local machine does all naming/numbering; the VM only downloads to the names
it's given — no round-trip.

---

## Deliverable formatting

- **Moderate margins** (1″ top/bottom, 0.75″ left/right) on every output doc.
- **Page header** reading `<Platform> <page number>` (or `Accounts Located`) in
  Times New Roman 12pt; **no footer**; no on-page heading.
- The Hunchly **"Page title:"** rows are dropped from each exhibit (usually just
  the post's own text/emoji) — kept only if the capture's note contains the word
  `page`.
- Row 10's "Updated date" is replaced with the real post date; the capture date
  and tab alignment are left intact.

---

## Preset system

Presets define the crop + display size per capture. First preset under the URL's
platform whose `url_patterns` matches wins; otherwise `_defaults.json` (and the
URL is logged in `New Presets Needed.json`).

```json
{
  "platform": "instagram",
  "post_style": "single_post",
  "url_patterns": ["instagram\\.com/(p|reel|tv)/[^/?#]+"],
  "components": ["post"],
  "crop": { "left_pct": 15.0, "top_pct": 10.0, "right_pct": 15.0, "bottom_pct": 8.0 },
  "size": { "mode": "fixed_width", "width_inches": 6.62 }
}
```

`size.mode`: `preserve` (keep extent), `fixed_width`, `fixed_height`, `fit_box`,
`fixed_both`. `width_inches: 6.62` fills the exhibit cell at Moderate margins.

**CV crop detection.** When a preset lists `components`, the crop is detected
per-capture by `hrb/vision` instead of using the static crop %; on any failure
it falls back to the preset's static crop. Detectors so far:
- **Instagram single post** — the modal card (media panel + white caption panel
  over the dimmed profile grid). Verified within ~5–10px of hand-annotated
  ideals; the white caption panel + a brightness-cap edge find handle it.
- **Profile / main-account** pages for several platforms.
- **TikTok single post** uses a near-full-frame static crop (the desktop viewer
  fills the frame) — no CV needed.

Annotate ideal crops with `tools/annotate.py` (GUI) and verify a detector
against them with `tools/verify_against_annotations.py`.

---

## Output structure

```
output/<case>/
├── Accounts Located.docx
├── Facebook.docx
├── Instagram.docx
├── TikTok.docx
├── X.docx  …
├── Review Required.docx        (if any captures need manual date entry)
├── New Presets Needed.json     (if any URLs hit the default preset)
├── manifest.json               (full audit trail)
├── download_list.csv           (only with --emit-download-list)
└── videos/
    ├── Facebook Video Item 9 (2026-06-22).mp4
    ├── Facebook Video Item 9 (2026-06-22).json
    └── _unused/                (videos whose post was removed in review)
```

---

## Architecture

```
hrb/
├── __main__.py     CLI (target / --batch / flags) + orchestration + manifest
├── parser.py       Hunchly docx → Capture records (10-row table layout)
├── raw_export.py   Hunchly raw zip → hash→MHTML index + analyst notes
├── platforms.py    URL → platform, handle, main-account detection
├── dates/
│   ├── __init__.py     tiered cascade + URL decoders + yt-dlp tier
│   ├── facebook.py     FB CSS-Flexbox character-scramble defeat
│   └── mhtml_universal.py
├── presets.py      preset library + crop/size math
├── vision/         per-platform classical-CV crop detection
├── writer.py       docx clone + XML surgery (crop, extent, caption, header, margins)
├── video.py        yt-dlp download, H.264 guarantee, incremental sync, list emit
└── pdf_export.py   optional docx → pdf via docx2pdf
download_from_list.py   self-contained VM-side downloader (yt-dlp + ffmpeg only)
```

The writer clones the source docx zip, replaces `word/document.xml`, and copies
`word/media/` through byte-identical.

---

## Limitations

- One subject per case.
- PDF export needs Microsoft Word.
- The yt-dlp date + video download are **live fetches** — they need network, the
  post must still be live, and social platforms (esp. Facebook/Instagram from
  datacenter IPs) may require cookies. They're disclosed as live-fetched in the
  audit; the deterministic tiers (note, URL decode, MHTML) are not.
- The Facebook unscrambler depends on FB's rendered-DOM scramble format (stable
  8+ years); on failure it routes to REVIEW_REQUIRED and the tool keeps working.

---

## License

Private. Not for redistribution.
