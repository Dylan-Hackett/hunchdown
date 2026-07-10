"""
Bulk-download the video for every capture in a Hunchly-exported .docx.

Given the same .docx the rest of hrb parses, this walks captures in
document order (hrb.parser.Capture.index — the closest deterministic
substitute for "page number", since Word doesn't store print-page
metadata in the XML) and tries to download each capture's URL as a
video via yt-dlp. Captures with no video track (plain photo posts,
Instagram carousels of stills, etc.) are skipped and logged, never
faked. TikTok photo/slideshow posts are downloaded via yt-dlp's native
slideshow support, which recreates the platform's own timed
image+audio rendering rather than fabricating new content.

No AI, no OCR — this is a thin deterministic wrapper around yt-dlp.

Usage:
    uv run python -m hrb.bulk_download --input Test_Export.docx --output ./videos
"""
from __future__ import annotations

import argparse
import csv
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from .parser import parse_docx

FILENAME_TEMPLATE = "Video_item_{n:03d}"


@dataclass
class DownloadResult:
    index: int              # 0-based capture index from the docx (our "page number")
    url: str
    status: str              # "downloaded" | "skipped" | "error"
    filename: str | None
    detail: str


def _download_one(url: str, out_dir: Path, stem: str) -> tuple[str, str | None, str]:
    """Try to download `url` as a video into out_dir/stem.mp4.

    Returns (status, filename, detail).
    """
    import yt_dlp

    outtmpl = str(out_dir / f"{stem}.%(ext)s")
    opts = {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ignoreerrors": False,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "No video formats found" in msg or "no video" in msg.lower():
            return "skipped", None, "no video track (photo-only post)"
        return "error", None, msg
    except Exception as e:  # noqa: BLE001 - surface any extractor failure per-URL
        return "error", None, str(e)

    if info is None:
        return "skipped", None, "no video track (photo-only post)"

    produced = out_dir / f"{stem}.mp4"
    if not produced.exists():
        # yt-dlp picked a different final extension than mp4 despite merge_output_format
        # (can happen if ffmpeg is missing); find whatever it actually wrote.
        matches = list(out_dir.glob(f"{stem}.*"))
        if not matches:
            return "error", None, "yt-dlp reported success but no output file found"
        produced = matches[0]

    return "downloaded", produced.name, "ok"


def bulk_download(input_docx: Path, output_dir: Path) -> list[DownloadResult]:
    if shutil.which("ffmpeg") is None:
        print(
            "warning: ffmpeg not found on PATH — downloads needing merge/remux to "
            "mp4 (most platforms) will fail or produce non-mp4 files.",
            file=sys.stderr,
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    parsed = parse_docx(input_docx)

    results: list[DownloadResult] = []
    for capture in parsed.captures:
        stem = FILENAME_TEMPLATE.format(n=capture.index + 1)
        status, filename, detail = _download_one(capture.url, output_dir, stem)
        results.append(DownloadResult(
            index=capture.index,
            url=capture.url,
            status=status,
            filename=filename,
            detail=detail,
        ))
        print(f"[{capture.index + 1:03d}] {status}: {capture.url} — {detail}")

    log_path = output_dir / "download_log.csv"
    with log_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["capture_index", "url", "status", "filename", "detail"])
        for r in results:
            writer.writerow([r.index + 1, r.url, r.status, r.filename or "", r.detail])

    downloaded = sum(1 for r in results if r.status == "downloaded")
    skipped = sum(1 for r in results if r.status == "skipped")
    errored = sum(1 for r in results if r.status == "error")
    print(f"\n{downloaded} downloaded, {skipped} skipped (no video), {errored} errored. "
          f"Log: {log_path}")

    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True, help="Hunchly-exported .docx")
    ap.add_argument("--output", required=True, help="folder to write videos + log into")
    args = ap.parse_args(argv)

    bulk_download(Path(args.input), Path(args.output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
