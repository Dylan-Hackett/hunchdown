"""Debug what the IG detector sees per image."""
from __future__ import annotations
import json
import sys
import zipfile
from io import BytesIO
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hrb.parser import parse_docx
from hrb.vision._common import decode_image
from hrb.vision.instagram import _find_buttons_row, _find_avatar_top, _AVATAR_BRIGHT_THRESHOLD as _AVATAR_DARK_THRESHOLD

parsed = parse_docx("530export.docx")
with zipfile.ZipFile(BytesIO(parsed.zip_bytes)) as zf:
    media = {n: zf.read(n) for n in zf.namelist() if n.startswith("word/media/")}

ann_list = json.loads(Path("ig_profile_annotations.json").read_text())

for ann in ann_list:
    if not ann["boxes"]:
        continue
    url = ann["url"]
    png = media.get(ann["image_media_path"])
    img = decode_image(png)
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    print(f"\n=== {url}  ({w}×{h}) ===")
    btn = _find_buttons_row(gray, w, h)
    print(f"  buttons_row: {btn}")
    gt = {b["label"]: b for b in ann["boxes"]}
    print(f"  GT avatar:    L={gt['avatar']['l_px']} T={gt['avatar']['t_px']}")
    print(f"  GT buttons:   L={gt['buttons_row']['l_px']} T={gt['buttons_row']['t_px']} "
          f"R={gt['buttons_row']['r_px']} B={gt['buttons_row']['b_px']}")

    if btn is None:
        continue
    bx, by, bw, bh = btn
    avatar_top = _find_avatar_top(gray, w, h, bx, bx + bw, by)
    print(f"  detected avatar_top: {avatar_top}  (gt: {gt['avatar']['t_px']})")

    # Per-row dark count in column band, first 200 rows
    band = gray[: by, bx:bx + bw]
    dark_per_row = (band < _AVATAR_DARK_THRESHOLD).sum(axis=1)
    col_w = bw
    threshold = col_w * 0.04
    print(f"  col band: cols {bx}..{bx+bw} ({col_w}px wide), threshold={threshold:.0f}")
    print(f"  rows 0-200 (dark count, * = above threshold):")
    for i in range(0, min(200, len(dark_per_row)), 5):
        flag = "*" if dark_per_row[i] >= threshold else " "
        print(f"    y={i:>3d}  dark={dark_per_row[i]:>5d}  {flag}")
