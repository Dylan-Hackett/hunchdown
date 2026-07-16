"""
Hunchly Report Builder CLI.

Usage:
    python -m hrb --input "Test Export.docx" --raw-zip Test.zip \\
                  --output ./output --case smith_v_jones

Outputs:
    output/<case>_<date>/
        Accounts Located.docx
        Facebook.docx
        Instagram.docx
        ...
        Review Required.docx        (only if any captures failed date extraction)
        New Presets Needed.json     (only if any URL had no specific preset match)
        manifest.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from .dates import DateResult, extract as extract_date
from .parser import parse_docx, Capture, ParsedDocx
from .pdf_export import ConverterUnavailable, export as export_pdfs
from .platforms import (
    PLATFORM_DISPLAY_NAMES,
    PLATFORM_ORDER,
    classify,
    extract_handle,
    is_main_account_url,
)
from .presets import Preset, PresetLibrary
from .raw_export import RawExport
from .video import VideoJob, download_all, is_video_post
from .writer import (
    ExhibitInput,
    _format_post_date,
    write_locator_docx,
    write_platform_docx,
)


def _platform_filename(platform: str, suffix: str = "docx") -> str:
    return f"{PLATFORM_DISPLAY_NAMES[platform]}.{suffix}"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _process_captures(
    parsed: ParsedDocx,
    raw: RawExport | None,
) -> tuple[
    dict[str, list[tuple[Capture, DateResult, str | None]]],  # platform -> (capture, date, note)
    list[Capture],                                  # main-account captures
    list[tuple[Capture, DateResult]],               # review queue
]:
    """Classify, extract dates, partition into platform / locator / review buckets."""
    posts_by_platform: dict[str, list[tuple[Capture, DateResult]]] = {}
    main_accounts: list[Capture] = []
    review_queue: list[tuple[Capture, DateResult]] = []

    for c in parsed.captures:
        platform = classify(c.url)

        if is_main_account_url(c.url, platform):
            main_accounts.append(c)
            continue

        mhtml_bytes = None
        note_text = None
        if raw is not None and c.sha256:
            mhtml_bytes = raw.read_mhtml(c.sha256)
            note_text = raw.get_note(c.sha256)

        year_match = re.search(r"\b(20\d{2})\b", c.capture_date_raw)
        reference_year = int(year_match.group(1)) if year_match else None

        dr = extract_date(
            url=c.url,
            platform=platform,
            mhtml_bytes=mhtml_bytes,
            note_text=note_text,
            post_body_hint=c.page_title or None,
            reference_year=reference_year,
        )

        if dr.post_date is None:
            review_queue.append((c, dr))
        else:
            posts_by_platform.setdefault(platform, []).append((c, dr, note_text))

    return posts_by_platform, main_accounts, review_queue


def _build_review_doc(
    parsed: ParsedDocx,
    review_queue: list[tuple[Capture, DateResult]],
    presets: PresetLibrary,
    output_path: Path,
) -> None:
    """Build REVIEW_REQUIRED.docx using the same table-clone path as exhibits."""
    if not review_queue:
        return

    placeholder_dt = datetime(1900, 1, 1, tzinfo=timezone.utc)
    exhibits: list[ExhibitInput] = []
    for i, (c, dr) in enumerate(review_queue, start=1):
        platform = classify(c.url)
        preset, _ = presets.match(c.url, platform)
        review_dr = DateResult(
            post_date=placeholder_dt,
            source="REVIEW_REQUIRED",
            confidence="needs_manual_entry",
            notes=dr.notes or "automatic date extraction failed",
        )
        exhibits.append(ExhibitInput(
            capture=c,
            date_result=review_dr,
            preset=preset,
            exhibit_number=i,
        ))

    write_platform_docx(parsed, "REVIEW REQUIRED — Manual Date Entry", exhibits, output_path)


def run(
    input_docx: Path,
    raw_zip: Path | None,
    output_root: Path,
    case_name: str,
    presets_dir: Path,
    no_pdf: bool = False,
    download_videos: bool = True,
) -> Path:
    parsed = parse_docx(input_docx)
    presets = PresetLibrary(presets_dir)

    raw_ctx = RawExport(raw_zip) if raw_zip else None
    try:
        posts_by_platform, main_accounts, review_queue = _process_captures(parsed, raw_ctx)
    finally:
        if raw_ctx is not None:
            raw_ctx.close()

    date_str = datetime.now().strftime("%Y-%m-%d")
    case_dir = output_root / f"{case_name}_{date_str}"
    case_dir.mkdir(parents=True, exist_ok=True)

    docx_outputs: list[Path] = []
    manifest_exhibits: list[dict] = []
    platform_exhibit_counts: dict[str, int] = {}
    platform_exhibit_filenames: dict[str, str] = {}
    video_jobs: list[VideoJob] = []

    for platform in PLATFORM_ORDER:
        items = posts_by_platform.get(platform, [])
        if not items:
            continue

        items_sorted = sorted(items, key=lambda x: x[1].post_date)
        exhibits: list[ExhibitInput] = []
        for i, (c, dr, note) in enumerate(items_sorted, start=1):
            preset, _ = presets.match(c.url, platform)
            ex = ExhibitInput(
                capture=c,
                date_result=dr,
                preset=preset,
                exhibit_number=i,
                note=note,
            )
            exhibits.append(ex)
            # Video filename mirrors the exhibit's slot in the deliverable doc:
            # "<Platform> Video Item <N> (<post-date>)", with the same
            # post-date string (and timezone handling) shown in the caption.
            if download_videos and is_video_post(c.url):
                video_jobs.append(VideoJob(
                    url=c.url,
                    platform=platform,
                    exhibit_number=i,
                    capture_sha256=c.sha256,
                    filename_stem=f"{PLATFORM_DISPLAY_NAMES[platform]} Video Item {i} ({_format_post_date(ex)})",
                ))

        fname = _platform_filename(platform, "docx")
        out_path = case_dir / fname
        decisions = write_platform_docx(parsed, PLATFORM_DISPLAY_NAMES[platform], exhibits, out_path)
        docx_outputs.append(out_path)

        platform_exhibit_counts[platform] = len(exhibits)
        platform_exhibit_filenames[platform] = _platform_filename(platform, "pdf")

        for ex, decision in zip(exhibits, decisions):
            manifest_exhibits.append({
                "platform": platform,
                "exhibit_number": ex.exhibit_number,
                "url": ex.capture.url,
                "page_title": ex.capture.page_title,
                "post_date": ex.date_result.post_date.isoformat(),
                "post_date_source": ex.date_result.source,
                "post_date_confidence": ex.date_result.confidence,
                "capture_date_raw": ex.capture.capture_date_raw,
                "sha256": ex.capture.sha256,
                "preset_used": ex.preset.source_path,
                "preset_post_style": ex.preset.post_style,
                "crop_source": decision["crop_source"],
                "crop_pct": decision["crop_pct"],
                "cv_components": decision["cv_components"],
            })

    locator_path = case_dir / "Accounts Located.docx"
    by_platform_accounts: dict[str, list[Capture]] = {}
    for c in main_accounts:
        by_platform_accounts.setdefault(classify(c.url), []).append(c)

    locator_sections: list[tuple[str, list[tuple[Capture, Preset]]]] = []
    locator_entries: list[dict] = []
    for platform in PLATFORM_ORDER:
        accounts = sorted(
            by_platform_accounts.get(platform, []),
            key=lambda c: c.capture_date_raw,
        )
        if not accounts:
            continue
        items: list[tuple[Capture, Preset]] = []
        for c in accounts:
            preset, _ = presets.match(c.url, platform)
            items.append((c, preset))
            locator_entries.append({
                "platform": platform,
                "handle": extract_handle(c.url, platform),
                "url": c.url,
                "capture_date_raw": c.capture_date_raw,
                "sha256": c.sha256,
                "exhibit_doc_filename": platform_exhibit_filenames.get(platform),
                "exhibit_count": platform_exhibit_counts.get(platform, 0),
            })
        locator_sections.append((PLATFORM_DISPLAY_NAMES[platform], items))

    if locator_sections:
        locator_decisions = write_locator_docx(
            parsed,
            sections=locator_sections,
            output_path=locator_path,
            doc_title=f"Accounts Located — {case_name}",
        )
        for entry in locator_entries:
            d = locator_decisions.get(entry["sha256"])
            if d:
                entry["crop_source"] = d["crop_source"]
                entry["crop_pct"] = d["crop_pct"]
                entry["cv_components"] = d["cv_components"]
        docx_outputs.insert(0, locator_path)

    review_path = case_dir / "Review Required.docx"
    if review_queue:
        _build_review_doc(parsed, review_queue, presets, review_path)
        docx_outputs.append(review_path)

    if presets.unmatched:
        (case_dir / "New Presets Needed.json").write_text(
            json.dumps(presets.unmatched, indent=2)
        )

    video_results = []
    if download_videos and video_jobs:
        print(f"Downloading {len(video_jobs)} video(s) from live post URLs "
              f"(supplementary preservation; see manifest chain-of-custody):")
        video_results = download_all(video_jobs, case_dir / "videos", case_dir)

    pdf_status = "skipped"
    if not no_pdf:
        try:
            export_pdfs(docx_outputs)
            pdf_status = "ok"
        except ConverterUnavailable as e:
            pdf_status = f"unavailable: {e}"

    manifest = {
        "case_name": case_name,
        "processed_at": datetime.now().astimezone().isoformat(),
        "source_docx": str(input_docx),
        "source_docx_sha256": _sha256_file(input_docx),
        "raw_zip": str(raw_zip) if raw_zip else None,
        "raw_zip_sha256": _sha256_file(raw_zip) if raw_zip else None,
        "captures_processed": len(parsed.captures),
        "exhibits_built": len(manifest_exhibits),
        "main_accounts_indexed": len(locator_entries),
        "review_required": len(review_queue),
        "platforms_found": sorted(posts_by_platform.keys()),
        "pdf_export_status": pdf_status,
        "videos_downloaded": sum(1 for r in video_results if r.status == "downloaded"),
        "video_downloads": [r.to_dict() for r in video_results],
        "exhibits": manifest_exhibits,
        "main_accounts": locator_entries,
        "review_queue": [
            {
                "url": c.url,
                "page_title": c.page_title,
                "sha256": c.sha256,
                "capture_date_raw": c.capture_date_raw,
                "failure_notes": dr.notes,
            }
            for c, dr in review_queue
        ],
        "unmatched_url_presets": presets.unmatched,
    }

    (case_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return case_dir


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="hrb", description="Hunchly Report Builder")
    p.add_argument("--input", required=True, type=Path, help="Hunchly-exported .docx")
    p.add_argument("--raw-zip", type=Path, default=None, help="Hunchly raw case .zip (for Tier 2 MHTML date extraction)")
    p.add_argument("--output", type=Path, default=Path("./output"), help="Output root directory")
    p.add_argument("--case", required=True, help="Case name (used in output folder + locator title)")
    p.add_argument("--presets", type=Path, default=Path("./presets"), help="Presets directory")
    p.add_argument("--no-pdf", action="store_true", help="Skip PDF export (docx only)")
    p.add_argument("--no-download-videos", action="store_true",
                   help="Skip downloading videos from live post URLs "
                        "(video download runs by default for every downloadable link)")

    args = p.parse_args(argv)

    case_dir = run(
        input_docx=args.input,
        raw_zip=args.raw_zip,
        output_root=args.output,
        case_name=args.case,
        presets_dir=args.presets,
        no_pdf=args.no_pdf,
        download_videos=not args.no_download_videos,
    )
    print(f"Done. Output: {case_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
