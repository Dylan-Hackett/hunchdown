"""
Visual tuner for the auto-detected-landmark crop rules.

For a given platform + post_style, this:
  1. Detects the platform's landmarks on each capture image (e.g. IG avatar
     and buttons_row).
  2. Shows them outlined on the canvas (blue = avatar, orange = buttons_row).
  3. Renders the resulting CROP rectangle (green dashed) using the currently
     configured margins.
  4. Lets you tweak three sliders: top margin (above avatar), bottom margin
     (below buttons row), and a SINGLE horizontal margin applied equally to
     left + right (so the crop is automatically symmetric around the
     buttons row).
  5. Saves to the platform's tuning JSON when you press "Save". The CV
     detector re-reads the tuning on every call so the runtime picks up
     your edits next time the pipeline runs.

Usage:
    python tools/tune_crop.py --docx 530export.docx \\
        --platform instagram --post-style main_account
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
from hrb.platforms import classify, is_main_account_url
from hrb.vision._common import decode_image


_PRESETS_DIR = Path(__file__).resolve().parent.parent / "presets"

TUNING_PATHS = {
    ("instagram", "main_account"): _PRESETS_DIR / "instagram" / "main_account_tuning.json",
    ("facebook", "main_account"):  _PRESETS_DIR / "facebook"  / "main_account_tuning.json",
}

# Per-platform slider config: list of (json_key, slider label, min, max).
SLIDER_CONFIG = {
    ("instagram", "main_account"): [
        ("top_margin_above_avatar_pct",      "top margin (above name/avatar topmost, % of h)",      0.0, 6.0),
        ("bottom_margin_below_buttons_pct",  "bottom margin (below buttons, % of h)",               0.0, 6.0),
        ("side_margin_around_buttons_pct",   "side margin (equidistant L/R around buttons, % of w)", 0.0, 5.0),
    ],
    ("facebook", "main_account"): [
        ("top_margin_above_avatar_pct",      "top margin (above avatar, into cover photo, % of h)", 0.0, 18.0),
        ("left_margin_pct",                  "left margin (blue left of content, % of w)",          0.0, 5.0),
        ("right_margin_pct",                 "right margin (blue right of content, % of w)",        0.0, 5.0),
        ("bottom_margin_pct",                "bottom margin (blue below intro, capped to gap, % of w)", 0.0, 5.0),
    ],
}

# Per-platform crop-compute hooks (so each platform's geometry stays local).
def _compute_crop(platform: str, post_style: str, img_w: int, img_h: int,
                  landmarks: dict, t: dict) -> dict:
    if platform == "instagram" and post_style == "main_account":
        ct = landmarks.get("content_top")
        av = landmarks["avatar"]
        btn = landmarks["buttons_row"]
        top_m = int(img_h * t["top_margin_above_avatar_pct"] / 100.0)
        bot_m = int(img_h * t["bottom_margin_below_buttons_pct"] / 100.0)
        side_m = int(img_w * t["side_margin_around_buttons_pct"] / 100.0)
        top_anchor = min(ct.top, av.top) if ct is not None else av.top
        return {
            "left": max(0, btn.left - side_m),
            "top": max(0, top_anchor - top_m),
            "right": min(img_w, btn.right + side_m),
            "bottom": min(img_h, btn.bottom + bot_m),
            "margins_px": {
                "top_margin_above_avatar_pct": top_m,
                "bottom_margin_below_buttons_pct": bot_m,
                "side_margin_around_buttons_pct": side_m,
            },
        }
    if platform == "facebook" and post_style == "main_account":
        from hrb.vision.facebook import profile_crop_from_landmarks
        bb = profile_crop_from_landmarks(landmarks, img_w, img_h, t)
        avatar = landmarks["avatar"]
        intro = landmarks.get("intro_card")
        column = landmarks["content_column"]
        top_m = avatar.top - bb.top
        # Left is anchored on the avatar (see facebook.profile_crop_from_landmarks).
        left_anchor = avatar.left if avatar.left >= column.left else column.left
        left_m = left_anchor - bb.left
        right_m = bb.right - column.right
        bot_m = (bb.bottom - intro.bottom) if intro is not None else 0
        return {
            "left": bb.left, "top": bb.top, "right": bb.right, "bottom": bb.bottom,
            "margins_px": {
                "top_margin_above_avatar_pct": top_m,
                "left_margin_pct": left_m,
                "right_margin_pct": right_m,
                "bottom_margin_pct": bot_m,
            },
        }
    raise ValueError(f"no crop hook for {platform}/{post_style}")


# Landmark-detector hooks: must return dict[str, BBox] or None.
def _landmark_detector(platform: str, post_style: str):
    if platform == "instagram" and post_style == "main_account":
        from hrb.vision.instagram import detect_profile_landmarks
        return detect_profile_landmarks
    if platform == "facebook" and post_style == "main_account":
        from hrb.vision.facebook import detect_profile_landmarks
        return detect_profile_landmarks
    return None


# Color-coding for landmark overlays in the GUI (rendered in stable order).
_LANDMARK_COLORS = {
    "content_top":    "#f3f",   # magenta
    "avatar":         "#3af",   # blue
    "buttons_row":    "#fa3",   # orange
    "content_column": "#9c9",   # pale green (FB body column)
    "intro_card":     "#fa3",   # orange
    "next_card":      "#f55",   # red (the card below intro — crop must not show it)
}


def _filter_captures(docx_path: Path, platform: str, post_style: str):
    parsed = parse_docx(docx_path)
    with zipfile.ZipFile(BytesIO(parsed.zip_bytes)) as zf:
        media = {n: zf.read(n) for n in zf.namelist() if n.startswith("word/media/")}
    out = []
    for c in parsed.captures:
        if classify(c.url) != platform:
            continue
        is_main = is_main_account_url(c.url, platform)
        if post_style == "main_account" and not is_main:
            continue
        if post_style == "single_post" and is_main:
            continue
        if not c.image_media_path:
            continue
        png = media.get(c.image_media_path)
        if not png:
            continue
        out.append((c, png))
    return out


def run(platform: str, post_style: str, captures, tuning_path: Path) -> None:
    import tkinter as tk
    from PIL import Image, ImageTk

    detect = _landmark_detector(platform, post_style)
    if detect is None:
        raise SystemExit(f"No landmark detector for {platform}/{post_style}")

    tuning = json.loads(tuning_path.read_text())

    # Per-image: decode + detect once, cache landmarks
    cache = []
    for c, png in captures:
        img = decode_image(png)
        lms = detect(img) if img is not None else None
        cache.append({"capture": c, "png": png, "img": img, "landmarks": lms})

    state = {"idx": 0, "tkimg": None, "scale": 1.0}

    SCALE_MAX_W = 1300
    SCALE_MAX_H = 720

    root = tk.Tk()
    root.title(f"HRB crop tuner — {platform}/{post_style}")

    top = tk.Frame(root)
    top.pack(fill="x", padx=8, pady=4)
    info = tk.Label(top, text="", anchor="w", justify="left",
                    font=("TkDefaultFont", 11))
    info.pack(side="left", fill="x", expand=True)
    tk.Button(top, text="Prev (p / ←)", command=lambda: prev_img()).pack(side="left", padx=2)
    tk.Button(top, text="Next (n / →)", command=lambda: next_img()).pack(side="left", padx=2)
    tk.Button(top, text="Save (s)", command=lambda: save_now(), bg="#cfc").pack(side="left", padx=8)
    tk.Button(top, text="Quit (q)", command=lambda: quit_()).pack(side="left", padx=2)

    legend = tk.Label(
        root,
        text=f"landmark overlays + green dashed = resulting crop  "
             f"(platform: {platform}/{post_style})",
        anchor="w", fg="#555",
    )
    legend.pack(fill="x", padx=8)

    body = tk.Frame(root)
    body.pack(fill="both", expand=True)

    canvas = tk.Canvas(body, bg="#222", highlightthickness=0)
    canvas.pack(side="left", fill="both", expand=True)

    # Sliders panel
    panel = tk.Frame(body, padx=10, pady=10)
    panel.pack(side="right", fill="y")

    tk.Label(panel, text="Margins", font=("TkDefaultFont", 13, "bold")).pack(pady=(0, 8))

    sliders = {}
    px_readouts = {}

    def make_slider(key, label, from_, to_, step=0.05):
        f = tk.Frame(panel)
        f.pack(fill="x", pady=4)
        tk.Label(f, text=label, anchor="w").pack(fill="x")
        row = tk.Frame(f)
        row.pack(fill="x")
        sv = tk.DoubleVar(value=tuning.get(key, 1.0))
        s = tk.Scale(row, from_=from_, to_=to_, resolution=step,
                     orient="horizontal", variable=sv, length=240,
                     command=lambda _v: on_slider_change())
        s.pack(side="left")
        px = tk.Label(row, text="", width=10, anchor="w", fg="#555")
        px.pack(side="left", padx=4)
        sliders[key] = sv
        px_readouts[key] = px

    for key, label, lo, hi in SLIDER_CONFIG[(platform, post_style)]:
        make_slider(key, label, lo, hi)

    status = tk.Label(panel, text="", anchor="w", justify="left", fg="#070",
                      font=("TkDefaultFont", 10))
    status.pack(fill="x", pady=(10, 0))

    # ── render ────────────────────────────────────────────────────────────
    def fit_scale(w, h):
        return min(SCALE_MAX_W / w, SCALE_MAX_H / h, 1.0)

    def current_tuning():
        return {k: float(v.get()) for k, v in sliders.items()}

    def compute_crop(img_w, img_h, landmarks, t):
        return _compute_crop(platform, post_style, img_w, img_h, landmarks, t)

    def render():
        entry = cache[state["idx"]]
        c = entry["capture"]
        img = entry["img"]
        lms = entry["landmarks"]

        canvas.delete("all")

        if img is None:
            info.config(text=f"[{state['idx']+1}/{len(cache)}]  {c.url}\n  (image decode failed)")
            return

        h, w = img.shape[:2]
        scale = fit_scale(w, h)
        state["scale"] = scale

        pil = Image.fromarray(img[:, :, ::-1])  # BGR → RGB
        disp = pil.resize((int(w * scale), int(h * scale)))
        tkimg = ImageTk.PhotoImage(disp)
        state["tkimg"] = tkimg
        canvas.config(width=disp.size[0], height=disp.size[1])
        canvas.create_image(0, 0, anchor="nw", image=tkimg)

        if lms is None:
            info.config(text=f"[{state['idx']+1}/{len(cache)}]  {c.url}  "
                             f"({w}×{h})   ⚠ landmark detection FAILED")
            return

        t = current_tuning()
        crop = compute_crop(w, h, lms, t)

        def draw_box(bb, color, dash=None):
            canvas.create_rectangle(
                bb.left * scale, bb.top * scale,
                bb.right * scale, bb.bottom * scale,
                outline=color, width=2,
                dash=dash if dash else None,
            )

        for name, bb in lms.items():
            color = _LANDMARK_COLORS.get(name, "#999")
            draw_box(bb, color)
        # crop rect
        canvas.create_rectangle(
            crop["left"] * scale, crop["top"] * scale,
            crop["right"] * scale, crop["bottom"] * scale,
            outline="#3f3", width=3, dash=(8, 4),
        )

        landmark_summary = "  ".join(
            f"{k}=({bb.left},{bb.top})–({bb.right},{bb.bottom})"
            for k, bb in lms.items()
        )
        info.config(text=f"[{state['idx']+1}/{len(cache)}]  {c.url}  ({w}×{h})\n"
                         f"{landmark_summary}\n"
                         f"crop=({crop['left']},{crop['top']})–({crop['right']},{crop['bottom']})")

        # readouts: each slider's margin in pixels, keyed by its json key.
        for key, px in crop.get("margins_px", {}).items():
            if key in px_readouts:
                px_readouts[key].config(text=f"= {px:>4d}px")

    def on_slider_change():
        render()

    def prev_img(_=None):
        if state["idx"] > 0:
            state["idx"] -= 1
            render()

    def next_img(_=None):
        if state["idx"] < len(cache) - 1:
            state["idx"] += 1
            render()

    def save_now(_=None):
        t = current_tuning()
        # preserve any "comment" or unknown keys already in the file
        existing = json.loads(tuning_path.read_text())
        existing.update(t)
        tuning_path.write_text(json.dumps(existing, indent=2))
        status.config(text=f"✓ saved → {tuning_path.name}")

    def quit_(_=None):
        root.destroy()

    root.bind("<Left>", prev_img)
    root.bind("p", prev_img)
    root.bind("<Right>", next_img)
    root.bind("n", next_img)
    root.bind("s", save_now)
    root.bind("q", quit_)
    root.protocol("WM_DELETE_WINDOW", quit_)

    render()
    root.mainloop()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--docx", required=True, type=Path)
    p.add_argument("--platform", required=True)
    p.add_argument("--post-style", required=True)
    args = p.parse_args()

    key = (args.platform, args.post_style)
    if key not in TUNING_PATHS:
        raise SystemExit(f"No tuning config for {key}. Supported: {list(TUNING_PATHS)}")

    captures = _filter_captures(args.docx, args.platform, args.post_style)
    if not captures:
        print(f"No matching captures in {args.docx}")
        return 1

    print(f"Tuning {len(captures)} captures, config = {TUNING_PATHS[key]}")
    run(args.platform, args.post_style, captures, TUNING_PATHS[key])
    return 0


if __name__ == "__main__":
    sys.exit(main())
