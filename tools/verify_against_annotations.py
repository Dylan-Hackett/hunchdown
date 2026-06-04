"""
Compare CV detector output against ground-truth annotations.

Usage:
    python tools/verify_against_annotations.py \\
        --docx 530export.docx \\
        --annotations ig_profile_annotations.json \\
        --platform instagram \\
        --post-style main_account
"""
from __future__ import annotations
import argparse
import json
import sys
import zipfile
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hrb.parser import parse_docx
from hrb.vision import detect_components


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--docx", required=True, type=Path)
    p.add_argument("--annotations", required=True, type=Path)
    p.add_argument("--platform", required=True)
    p.add_argument("--post-style", required=True)
    args = p.parse_args()

    parsed = parse_docx(args.docx)
    with zipfile.ZipFile(BytesIO(parsed.zip_bytes)) as zf:
        media = {n: zf.read(n) for n in zf.namelist() if n.startswith("word/media/")}
    by_sha = {c.sha256: c for c in parsed.captures}

    ann_list = json.loads(args.annotations.read_text())
    print(f"{'profile':<40s} {'side':<7s} {'gt':>6s} {'cv':>6s} {'Δpx':>5s}")
    print("-" * 75)

    abs_errors = []
    for ann in ann_list:
        sha = ann["sha256"]
        url = ann["url"]
        gt = {b["label"]: b for b in ann["boxes"]}
        if "ideal_crop" not in gt:
            continue
        png = media.get(ann["image_media_path"])
        if not png:
            continue
        det = detect_components(png, args.platform, args.post_style, ["profile"])
        if not det or "profile" not in det:
            print(f"{url[:40]:<40s}  DETECTOR FAILED")
            continue
        bb = det["profile"]
        ic = gt["ideal_crop"]
        comparisons = [
            ("L", ic["l_px"], bb.left),
            ("T", ic["t_px"], bb.top),
            ("R", ic["r_px"], bb.right),
            ("B", ic["b_px"], bb.bottom),
        ]
        for i, (side, gv, cv) in enumerate(comparisons):
            label = url.split("/")[-2] if i == 0 else ""
            delta = cv - gv
            abs_errors.append(abs(delta))
            print(f"{label:<40s} {side:<7s} {gv:>6d} {cv:>6d} {delta:>+5d}")
        print()

    if abs_errors:
        mean_err = sum(abs_errors) / len(abs_errors)
        max_err = max(abs_errors)
        print(f"\nMean |error|: {mean_err:.1f}px   Max: {max_err}px")


if __name__ == "__main__":
    main()
