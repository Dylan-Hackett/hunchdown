"""
Live video preservation for video-post exhibits.

The Hunchly export NEVER contains the video stream — only poster/frame images.
To preserve the actual video as supplementary evidence, this module re-downloads
it from the LIVE post URL via yt-dlp, at analysis time, and records a full
chain-of-custody audit for each download.

This is deliberately a DISTINCT event from the original Hunchly capture:

  * The bytes come from the live URL at download time, NOT from the export.
  * The post may have been edited, re-encoded, or removed since it was captured,
    so a download reflects the state of the live URL *now*, not the captured
    state. The audit records the download timestamp, tool version, source URL,
    and the SHA-256 of what was actually fetched, and links back to the Hunchly
    capture's own hash so the two events stay traceable but never conflated.
  * It is therefore OUTSIDE the tool's "reproducible from the export alone"
    guarantee. It is opt-in (CLI --download-videos) and never runs by default.

Requires network access, and ffmpeg for the muxed-format merges some platforms
use (TikTok typically returns a single progressive MP4 and needs no merge).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

# Standard disclaimer attached to every download record so a third party reading
# the manifest understands what the file is (and is not).
LIVE_FETCH_NOTE = (
    "Supplementary preservation. Fetched from the live post URL at download "
    "time via yt-dlp; not part of the Hunchly capture and not reproducible "
    "from the export alone. Reflects the state of the live URL at "
    "downloaded_at, which may differ from the state when originally captured."
)

# URL shapes where a downloadable video is plausible. yt-dlp makes the final
# call per URL; this just avoids pointlessly hitting the network for URLs that
# are never videos (e.g. a plain profile page). Image-only posts that slip
# through (an Instagram /p/ photo) come back as status "no_video", not an error.
_VIDEO_URL_PATTERNS = [
    r"tiktok\.com/@[^/]+/video/\d+",
    # NOTE: tiktok /photo/ slideshows are intentionally excluded — yt-dlp
    # rejects them as "Unsupported URL", so there's no video to fetch. The
    # slideshow's stills are already preserved in the Hunchly capture image.
    r"instagram\.com/(?:reel|tv|p)/[^/?#]+",
    r"youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/",
    r"facebook\.com/(?:[^/]+/videos/|reel/|watch/?\?v=|watch/\?v=)",
    r"(?:twitter|x)\.com/[^/]+/status/\d+",
]
_VIDEO_URL_RE = re.compile("|".join(_VIDEO_URL_PATTERNS), re.IGNORECASE)


def is_video_post(url: str) -> bool:
    """True if the URL might hold a downloadable video (yt-dlp decides for real)."""
    return bool(_VIDEO_URL_RE.search(url or ""))


@dataclass
class VideoJob:
    """One capture to attempt a video download for."""
    url: str
    platform: str
    exhibit_number: int
    capture_sha256: str
    filename_stem: str          # e.g. "TikTok_Exhibit_01" (no extension)


@dataclass
class VideoDownloadResult:
    source_url: str
    platform: str
    exhibit_number: int
    capture_sha256: str                 # ties this download to the Hunchly capture
    status: str                         # "downloaded" | "no_video" | "error"
    downloaded_at: str                  # UTC ISO-8601
    tool: str
    tool_version: str
    note: str
    output_file: str | None = None      # path relative to the case dir
    output_sha256: str | None = None
    output_size_bytes: int | None = None
    video_id: str | None = None
    ext: str | None = None
    resolution: str | None = None
    video_codec: str | None = None      # codec of the delivered file
    transcoded_to_h264: bool = False    # True if we re-encoded for playability
    source_video_codec: str | None = None  # original codec, when transcoded
    duration_s: float | None = None
    # As REPORTED BY THE PLATFORM at download time — a cross-check only, not an
    # authoritative post date (that stays the deterministic URL/DOM extraction).
    reported_upload_date: str | None = None
    reported_timestamp: int | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class _SilentLogger:
    """Swallow yt-dlp's own console output; we surface status via our records."""
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def _ytdlp_version() -> str:
    try:
        import yt_dlp
        return yt_dlp.version.__version__
    except Exception:
        return "unknown"


# Video codecs that decode out-of-the-box in the players a deliverable lands in
# (QuickTime/Preview, Word & PowerPoint embeds, Windows Media Player).
_PLAYABLE_VCODECS = ("h264", "avc1", "mpeg4")


def _probe_video(path: Path) -> tuple[str | None, str | None]:
    """(codec, WxH) of the first video stream via ffprobe; (None, None) if absent.

    Read from the downloaded file itself so both are accurate even when yt-dlp's
    info dict omits them (e.g. Facebook's progressive sd/hd formats)."""
    import shutil, subprocess
    if shutil.which("ffprobe") is None:
        return None, None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,width,height", "-of", "csv=p=0:nk=1",
             str(path)],
            capture_output=True, text=True, timeout=30,
        )
        parts = [p for p in (out.stdout or "").strip().split(",") if p]
        codec = parts[0] if parts else None
        res = f"{parts[1]}x{parts[2]}" if len(parts) >= 3 else None
        return codec, res
    except Exception:
        return None, None


def _transcode_to_h264(path: Path) -> bool:
    """Re-encode `path` in place to H.264/AAC mp4 for universal playback.

    Used only as a last resort when a platform offered no H.264 variant (some
    Facebook/Instagram reels are VP9-only). Writes to a temp file, then atomically
    replaces the original. Returns True on success. This changes the bytes — the
    caller records it as a compatibility re-encode in the audit.
    """
    import shutil, subprocess
    if shutil.which("ffmpeg") is None:
        return False
    tmp = path.with_suffix(".h264.mp4")
    try:
        out = subprocess.run(
            ["ffmpeg", "-y", "-i", str(path),
             "-c:v", "libx264", "-preset", "medium", "-crf", "18",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
             "-movflags", "+faststart", str(tmp)],
            capture_output=True, text=True, timeout=600,
        )
        if out.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            tmp.unlink(missing_ok=True)
            return False
        tmp.replace(path)
        return True
    except Exception:
        tmp.unlink(missing_ok=True)
        return False


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download_video(job: VideoJob, videos_dir: Path, case_dir: Path) -> VideoDownloadResult:
    """Download one video, hash it, and return a chain-of-custody record.

    Writes the media file to ``videos_dir`` and a sidecar ``<stem>.json`` audit
    beside it. Never raises — failures (no video, network, geo-block, removed
    post) are captured in the returned record's status/error.
    """
    import yt_dlp
    from yt_dlp.utils import DownloadError

    now = datetime.now(timezone.utc).isoformat()
    base = VideoDownloadResult(
        source_url=job.url,
        platform=job.platform,
        exhibit_number=job.exhibit_number,
        capture_sha256=job.capture_sha256,
        status="error",
        downloaded_at=now,
        tool="yt-dlp",
        tool_version=_ytdlp_version(),
        note=LIVE_FETCH_NOTE,
    )

    videos_dir.mkdir(parents=True, exist_ok=True)
    out_stem = videos_dir / job.filename_stem
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": _SilentLogger(),
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 3,
        "outtmpl": str(out_stem) + ".%(ext)s",
        # Prefer H.264 video + AAC audio. This is the codec that actually plays
        # everywhere the deliverable goes (QuickTime/Preview, Word/PowerPoint
        # embeds, Windows Media Player). Selecting by container alone ([ext=mp4])
        # is NOT enough: Facebook serves VP9-in-mp4 for its adaptive streams,
        # which those players can't decode — so we match the CODEC, then fall
        # back to a progressive mp4 (FB's sd/hd, also H.264), then anything.
        "format": (
            "bv*[vcodec~='^(avc|h264)']+ba[acodec~='^(mp4a|aac)']/"
            "b[vcodec~='^(avc|h264)']/"
            "b[ext=mp4]/"
            "bv*+ba/b"
        ),
        "merge_output_format": "mp4",
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(job.url, download=True)
            info = ydl.sanitize_info(info)
    except DownloadError as e:
        msg = str(e)
        base.status = "no_video" if re.search(r"no video|requested format|unsupported url", msg, re.I) else "error"
        base.error = msg[:400]
        _write_sidecar(out_stem, base)
        return base
    except Exception as e:  # network, geo-block, auth, etc.
        base.error = f"{type(e).__name__}: {str(e)[:380]}"
        _write_sidecar(out_stem, base)
        return base

    # Resolve the file that landed on disk.
    filepath: Path | None = None
    rd = (info or {}).get("requested_downloads") or []
    if rd and rd[0].get("filepath"):
        filepath = Path(rd[0]["filepath"])
    if filepath is None or not filepath.exists():
        matches = [p for p in videos_dir.glob(job.filename_stem + ".*") if p.suffix != ".json"]
        filepath = max(matches, key=lambda p: p.stat().st_size) if matches else None
    if filepath is None or not filepath.exists():
        base.status = "no_video"
        base.error = "yt-dlp reported success but produced no output file"
        _write_sidecar(out_stem, base)
        return base

    base.status = "downloaded"
    base.video_id = info.get("id")
    base.duration_s = info.get("duration")
    base.reported_upload_date = info.get("upload_date")
    base.reported_timestamp = info.get("timestamp")

    codec, _ = _probe_video(filepath)
    # Last resort: the platform offered no H.264 variant (VP9/AV1-only). Re-encode
    # so the file actually plays in the deliverable's viewers. Recorded as a
    # compatibility transcode; the authoritative evidence remains the Hunchly
    # capture + the source URL.
    if codec and codec.lower() not in _PLAYABLE_VCODECS and _transcode_to_h264(filepath):
        base.transcoded_to_h264 = True
        base.source_video_codec = codec

    base.output_file = str(filepath.relative_to(case_dir)) if case_dir in filepath.parents else str(filepath)
    base.output_sha256 = _sha256_file(filepath)
    base.output_size_bytes = filepath.stat().st_size
    base.ext = filepath.suffix.lstrip(".")
    base.video_codec, probed_res = _probe_video(filepath)
    base.resolution = probed_res or info.get("resolution") or (
        f"{info.get('width')}x{info.get('height')}" if info.get("width") else None
    )
    _write_sidecar(out_stem, base)
    return base


def _write_sidecar(out_stem: Path, result: VideoDownloadResult) -> None:
    import json
    out_stem.parent.mkdir(parents=True, exist_ok=True)
    (out_stem.with_suffix(".json")).write_text(json.dumps(result.to_dict(), indent=2))


def download_all(jobs: list[VideoJob], videos_dir: Path, case_dir: Path,
                 log=print) -> list[VideoDownloadResult]:
    """Download every job, logging progress. Returns all records for the manifest."""
    import shutil
    if shutil.which("ffmpeg") is None:
        log("  warning: ffmpeg not found on PATH — downloads that need an "
            "audio+video merge or mp4 remux may fail or land as non-mp4.")
    results: list[VideoDownloadResult] = []
    for i, job in enumerate(jobs, start=1):
        log(f"  [{i}/{len(jobs)}] {job.filename_stem}  {job.url}")
        res = download_video(job, videos_dir, case_dir)
        if res.status == "downloaded":
            note = ""
            if res.transcoded_to_h264:
                note = f"  (re-encoded from {res.source_video_codec} for playback)"
            elif res.video_codec and res.video_codec.lower() not in _PLAYABLE_VCODECS:
                note = (f"  ** WARNING: codec {res.video_codec} may not play and could "
                        f"not be transcoded (ffmpeg missing?) **")
            log(f"      -> {res.output_file}  ({res.resolution} {res.video_codec}, "
                f"{res.output_size_bytes:,} bytes){note}")
        else:
            log(f"      -> {res.status}: {res.error or ''}")
        results.append(res)
    return results
