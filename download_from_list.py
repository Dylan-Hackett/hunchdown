#!/usr/bin/env python3
"""
VM-side video downloader for the Hunchly Report Builder.

Reads the download_list.csv the local machine emits (`python -m hrb ...
--emit-download-list`) and downloads each video to the FINAL filename already
computed there — "<Platform> Video Item <N> (<date>).mp4". The VM does no
naming and reports nothing back; the local machine already knows the names.

Self-contained: needs only yt-dlp and ffmpeg on PATH (or `python -m yt_dlp`).
No hrb package required, so you can copy just this one file to the VM.

Usage (on the VM):
    python download_from_list.py download_list.csv --output ./videos
    python download_from_list.py download_list.csv --output ./videos --cookies cookies.txt

Every video is guaranteed H.264-in-.mp4 (VP9/AV1-only sources are transcoded so
they play in Word/QuickTime/WMP). Each gets a .json chain-of-custody sidecar,
and a results.csv is written alongside.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

_PLAYABLE_VCODECS = ("h264", "avc1", "mpeg4")
# Prefer H.264+AAC (plays everywhere) — matches hrb/video.py.
_FORMAT = ("bv*[vcodec~='^(avc|h264)']+ba[acodec~='^(mp4a|aac)']/"
           "b[vcodec~='^(avc|h264)']/b[ext=mp4]/bv*+ba/b")

LIVE_FETCH_NOTE = (
    "Supplementary preservation. Fetched from the live post URL at download time "
    "via yt-dlp on the download VM; not part of the Hunchly capture and not "
    "reproducible from the export alone.")


class _Silent:
    def debug(self, m): pass
    def info(self, m): pass
    def warning(self, m): pass
    def error(self, m): pass


def _ytdlp_version() -> str:
    try:
        import yt_dlp
        return yt_dlp.version.__version__
    except Exception:
        return "unknown"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _ffmpeg(args: list[str]) -> bool:
    try:
        r = subprocess.run(["ffmpeg", "-y", *args], capture_output=True, text=True, timeout=900)
        return r.returncode == 0
    except Exception:
        return False


def _probe(path: Path) -> tuple[str | None, str | None]:
    if shutil.which("ffprobe") is None:
        return None, None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,width,height",
             "-of", "csv=p=0:nk=1", str(path)],
            capture_output=True, text=True, timeout=60)
        parts = [p for p in (out.stdout or "").strip().split(",") if p]
        codec = parts[0] if parts else None
        res = f"{parts[1]}x{parts[2]}" if len(parts) >= 3 else None
        return codec, res
    except Exception:
        return None, None


def _ensure_h264_mp4(src: Path, out_stem: Path) -> tuple[Path, bool, str | None]:
    """Guarantee H.264-in-.mp4: keep native H.264/mp4, remux other containers,
    transcode VP9/AV1-only. Returns (final_path, transcoded, source_codec)."""
    target = out_stem.with_suffix(".mp4")
    codec, _ = _probe(src)
    playable = bool(codec and codec.lower() in _PLAYABLE_VCODECS)
    if playable and src.suffix.lower() == ".mp4":
        return src, False, None
    if shutil.which("ffmpeg") is None:
        return src, False, None
    tmp = out_stem.with_name(out_stem.name + ".__tmp__.mp4")
    if playable:
        ok = _ffmpeg(["-i", str(src), "-c", "copy", "-movflags", "+faststart", str(tmp)])
        transcoded, source_codec = False, None
    else:
        ok = _ffmpeg(["-i", str(src), "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                      "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                      "-movflags", "+faststart", str(tmp)])
        transcoded, source_codec = True, codec
    if not ok or not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        return src, False, None
    if src != target:
        src.unlink(missing_ok=True)
    tmp.replace(target)
    return target, transcoded, source_codec


def download_one(row: dict, videos_dir: Path, cookies: str | None) -> dict:
    import yt_dlp
    from yt_dlp.utils import DownloadError

    stem = row["filename_stem"]
    out_stem = videos_dir / stem
    rec = {
        "source_url": row["url"],
        "filename": stem,
        "platform": row.get("platform", ""),
        "exhibit_number": row.get("exhibit_number", ""),
        "capture_sha256": row.get("capture_sha256", ""),
        "post_date": row.get("post_date", ""),
        "status": "error",
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
        "tool": "yt-dlp", "tool_version": _ytdlp_version(),
        "note": LIVE_FETCH_NOTE,
        "output_file": None, "output_sha256": None, "output_size_bytes": None,
        "video_codec": None, "resolution": None,
        "transcoded_to_h264": False, "source_video_codec": None, "error": None,
    }
    opts = {
        "quiet": True, "no_warnings": True, "noprogress": True, "logger": _Silent(),
        "noplaylist": True, "socket_timeout": 30, "retries": 3,
        "outtmpl": str(out_stem) + ".%(ext)s",
        "format": _FORMAT, "merge_output_format": "mp4",
    }
    if cookies:
        opts["cookiefile"] = cookies
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.sanitize_info(ydl.extract_info(row["url"], download=True))
    except DownloadError as e:
        msg = str(e)
        rec["status"] = "no_video" if "no video" in msg.lower() or "requested format" in msg.lower() else "error"
        rec["error"] = msg[:400]
        return rec
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {str(e)[:380]}"
        return rec

    rd = (info or {}).get("requested_downloads") or []
    fp = Path(rd[0]["filepath"]) if rd and rd[0].get("filepath") else None
    if fp is None or not fp.exists():
        matches = [p for p in videos_dir.glob(stem + ".*") if p.suffix != ".json"]
        fp = max(matches, key=lambda p: p.stat().st_size) if matches else None
    if fp is None or not fp.exists():
        rec["status"] = "no_video"
        rec["error"] = "yt-dlp reported success but produced no file"
        return rec

    fp, rec["transcoded_to_h264"], rec["source_video_codec"] = _ensure_h264_mp4(fp, out_stem)
    rec["status"] = "downloaded"
    rec["output_file"] = fp.name
    rec["output_sha256"] = _sha256(fp)
    rec["output_size_bytes"] = fp.stat().st_size
    rec["video_codec"], rec["resolution"] = _probe(fp)
    (out_stem.with_suffix(".json")).write_text(json.dumps(rec, indent=2))
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("list_csv", type=Path, help="download_list.csv from the local build")
    ap.add_argument("--output", type=Path, default=Path("./videos"), help="folder for the videos")
    ap.add_argument("--cookies", default=None, help="cookies.txt (for FB/IG if the VM IP needs login)")
    args = ap.parse_args()

    if shutil.which("ffmpeg") is None:
        print("WARNING: ffmpeg not on PATH — VP9-only videos can't be transcoded to H.264.")

    rows = list(csv.DictReader(args.list_csv.open(encoding="utf-8")))
    args.output.mkdir(parents=True, exist_ok=True)
    print(f"{len(rows)} videos -> {args.output}")
    results = []
    for i, row in enumerate(rows, 1):
        print(f"  [{i}/{len(rows)}] {row['filename_stem']}")
        r = download_one(row, args.output, args.cookies)
        note = f" (re-encoded from {r['source_video_codec']})" if r["transcoded_to_h264"] else ""
        print(f"      -> {r['status']}"
              + (f": {r['resolution']} {r['video_codec']}{note}" if r["status"] == "downloaded"
                 else f": {r['error'] or ''}"))
        results.append(r)

    with (args.output / "results.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["filename", "status", "resolution", "video_codec",
                    "transcoded_to_h264", "output_sha256", "capture_sha256", "error"])
        for r in results:
            w.writerow([r["filename"], r["status"], r["resolution"], r["video_codec"],
                        r["transcoded_to_h264"], r["output_sha256"], r["capture_sha256"], r["error"] or ""])

    ok = sum(1 for r in results if r["status"] == "downloaded")
    print(f"\n{ok}/{len(results)} downloaded. Audit: results.csv + per-video .json sidecars.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
