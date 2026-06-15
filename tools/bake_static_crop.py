"""
Bake `ideal_crop` annotations into per-platform static main_account presets.

Reads an annotations JSON produced by tools/annotate.py (each entry has a
url, image_size_px, and boxes incl. one labelled `ideal_crop`), groups by
platform, averages the ideal_crop across that platform's captures, converts
to crop percentages, and writes/updates presets/<platform>/main_account.json
with NO `components` field (so it uses the static crop path).

Usage:
    python tools/bake_static_crop.py static_profiles.json
    python tools/bake_static_crop.py static_profiles.json --only snapchat,venmo
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hrb.platforms import classify

_PRESETS_DIR = Path(__file__).resolve().parent.parent / "presets"

# URL patterns for presets we may need to create. Profile/landing pages only.
_URL_PATTERNS = {
    "snapchat":  [r"snapchat\.com/@[^/?#]+"],
    "cashapp":   [r"cash\.app/\$[^/?#]+"],
    "venmo":     [r"venmo\.com/u/[^/?#]+", r"account\.venmo\.com/u/[^/?#]+"],
    "pinterest": [r"pinterest\.com/[^/?#]+/?(?:$|[?#])"],
    "yelp":      [r"yelp\.com/user_details\?userid="],
    "linkedin":  [r"linkedin\.com/in/[^/?#]+"],
    "youtube":   [r"youtube\.com/(?:user/|channel/|c/|@)[^/?#]+"],
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("annotations", type=Path)
    ap.add_argument("--only", default="", help="comma-separated platforms to bake (default: all present)")
    ap.add_argument("--width-inches", type=float, default=6.0)
    args = ap.parse_args()

    only = {p.strip() for p in args.only.split(",") if p.strip()}
    entries = json.loads(args.annotations.read_text())

    # platform -> list of crop_pct dicts
    by_platform: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        ideal = next((b for b in e.get("boxes", []) if b["label"] == "ideal_crop"), None)
        if ideal is None or not e.get("image_size_px"):
            continue
        w, h = e["image_size_px"]
        platform = classify(e["url"])
        if only and platform not in only:
            continue
        by_platform[platform].append({
            "left_pct":   round(max(0.0, ideal["l_px"]) / w * 100, 3),
            "top_pct":    round(max(0.0, ideal["t_px"]) / h * 100, 3),
            "right_pct":  round(max(0.0, (w - ideal["r_px"])) / w * 100, 3),
            "bottom_pct": round(max(0.0, (h - ideal["b_px"])) / h * 100, 3),
        })

    if not by_platform:
        print("No ideal_crop annotations found (with image sizes).")
        return 1

    for platform, crops in sorted(by_platform.items()):
        avg = {
            k: round(sum(c[k] for c in crops) / len(crops), 3)
            for k in ("left_pct", "top_pct", "right_pct", "bottom_pct")
        }
        out_dir = _PRESETS_DIR / platform
        out_dir.mkdir(exist_ok=True)
        out_path = out_dir / "main_account.json"

        if out_path.exists():
            preset = json.loads(out_path.read_text())
        else:
            preset = {
                "platform": platform,
                "post_style": "main_account",
                "url_patterns": _URL_PATTERNS.get(platform, []),
                "size": {"mode": "fixed_width", "width_inches": args.width_inches},
                "created_by": "Dylan Hackett",
            }
        preset["crop"] = avg
        preset.pop("components", None)  # static path
        preset["notes"] = f"Static profile crop, baked from {len(crops)} annotated capture(s)."
        if not preset.get("url_patterns"):
            preset["url_patterns"] = _URL_PATTERNS.get(platform, [])

        out_path.write_text(json.dumps(preset, indent=2) + "\n")
        print(f"{platform:10s} {len(crops)} cap  crop={avg}  -> {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
