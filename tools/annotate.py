"""
Annotate Hunchly capture PNGs with landmark + ideal_crop bounding boxes.

The output JSON feeds the CV detector tuner: for each capture you mark the
exact rectangle you want as the final crop (label = `ideal_crop`) plus any
landmarks you anchor that crop on mentally (`avatar`, `tabs_row`, `buttons_row`,
`intro_card`, etc — free-form labels). The detector can then be rewritten to
find the same landmarks at runtime and reproduce your crop.

Usage:
    python tools/annotate.py --docx 530export.docx \\
        --platform facebook --post-style main_account \\
        --out fb_profile_annotations.json

UI:
    1. Pick a label from the buttons up top.
    2. Click corner 1 on the canvas (cyan dot appears).
    3. Click corner 2 — a MAGENTA draft box appears.
    4. Reshape: click any corner of the draft box, then click new position.
       Repeat until the rectangle is right.
    5. Press Enter (or click the "accept" button) to commit the draft —
       it turns into a permanent yellow/green box with the active label.
    6. Press Esc at any time to discard the in-progress draft.

    Corners of permanent boxes are also clickable — click one then click
    a new position to fix an old box.

    Keys:
        Enter   accept draft
        Esc     discard draft / cancel grab
        u       undo last permanent box
        n / →   next image
        p / ←   previous image
        s       save JSON now (auto-saves on quit too)
        q       save + quit
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


PRESET_LABELS = [
    "ideal_crop",
    "avatar",
    "name",
    "tabs_row",
    "intro_card",
    "buttons_row",
    "bio",
    "cover_photo",
]


def _filter_captures(docx_path: Path, platforms, post_style: str):
    """platforms: a set/list of platform ids (or {"all"} for every platform)."""
    parsed = parse_docx(docx_path)
    with zipfile.ZipFile(BytesIO(parsed.zip_bytes)) as zf:
        media = {n: zf.read(n) for n in zf.namelist() if n.startswith("word/media/")}

    want = set(platforms)
    out = []
    for c in parsed.captures:
        platform = classify(c.url)
        if "all" not in want and platform not in want:
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


def run_annotator(captures, out_path: Path) -> None:
    import tkinter as tk
    from PIL import Image, ImageTk

    existing: dict = {}
    if out_path.exists():
        try:
            existing = {e["sha256"]: e for e in json.loads(out_path.read_text())}
        except Exception:
            existing = {}

    annotations: list[dict] = []
    for c, png in captures:
        prior = existing.get(c.sha256, {})
        annotations.append({
            "sha256": c.sha256,
            "url": c.url,
            "image_media_path": c.image_media_path,
            "image_size_px": None,
            "boxes": list(prior.get("boxes", [])),
        })

    # ─── state ─────────────────────────────────────────────────────────────
    # mode["state"] is one of:
    #   idle              — no in-progress action
    #   placed_first      — first corner of a new draft is set, awaiting second click
    #   draft             — a draft rect exists; user can grab its corners or accept
    #   draft_grabbing    — corner of the draft is grabbed; next click drops it
    #   perm_grabbing     — corner of a committed (perm) box is grabbed
    mode = {
        "state": "idle",
        "first_xy": None,            # canvas coords of the first click while drawing
        "draft": None,               # dict {l,t,r,b} in image-pixel coords
        "grab": None,                # ("draft", corner) or (box_idx, corner)
    }
    label_state = {"active": "ideal_crop", "buttons": {}}
    state = {"idx": 0, "image": None, "scale": 1.0}

    CORNER_PICK_PX = 14
    SCALE_MAX_W = 1400
    SCALE_MAX_H = 820

    root = tk.Tk()
    root.title("HRB landmark annotator")

    # ─── top status ────────────────────────────────────────────────────────
    info = tk.Label(root, text="", anchor="w", justify="left",
                    font=("TkDefaultFont", 11))
    info.pack(fill="x", padx=8, pady=(6, 2))

    # ─── label picker row ──────────────────────────────────────────────────
    label_bar = tk.Frame(root)
    label_bar.pack(fill="x", padx=8, pady=2)
    tk.Label(label_bar, text="label:", fg="#555").pack(side="left")

    def set_active_label(name: str):
        label_state["active"] = name
        for n, btn in label_state["buttons"].items():
            btn.config(relief="sunken" if n == name else "raised",
                       bg="#cfc" if n == name else "SystemButtonFace")
        refresh_status_line()

    for name in PRESET_LABELS:
        b = tk.Button(label_bar, text=name,
                      command=lambda n=name: set_active_label(n))
        b.pack(side="left", padx=2)
        label_state["buttons"][name] = b

    tk.Label(label_bar, text="  custom:", fg="#555").pack(side="left")
    custom_entry = tk.Entry(label_bar, width=14)
    custom_entry.pack(side="left", padx=2)

    def use_custom():
        v = custom_entry.get().strip()
        if v:
            set_active_label(v)
    tk.Button(label_bar, text="use", command=use_custom).pack(side="left")

    # ─── action buttons ────────────────────────────────────────────────────
    action_bar = tk.Frame(root)
    action_bar.pack(fill="x", padx=8, pady=2)
    accept_btn = tk.Button(action_bar, text="Accept draft (Enter)",
                           bg="#cfc", command=lambda: accept_draft())
    accept_btn.pack(side="left", padx=2)
    tk.Button(action_bar, text="Discard draft (Esc)",
              command=lambda: discard_draft()).pack(side="left", padx=2)
    tk.Button(action_bar, text="Undo last box (u)",
              command=lambda: undo()).pack(side="left", padx=2)
    tk.Button(action_bar, text="Save (s)",
              command=lambda: save_now()).pack(side="left", padx=8)
    tk.Button(action_bar, text="Prev (p / ←)",
              command=lambda: prev_img()).pack(side="left", padx=2)
    tk.Button(action_bar, text="Next (n / →)",
              command=lambda: next_img()).pack(side="left", padx=2)
    tk.Button(action_bar, text="Save + quit (q)",
              command=lambda: quit_()).pack(side="right", padx=2)

    # ─── status line under controls ────────────────────────────────────────
    status = tk.Label(root, text="", anchor="w", fg="#070",
                      font=("TkDefaultFont", 11, "bold"))
    status.pack(fill="x", padx=8)

    boxes_list = tk.Label(root, text="", anchor="w", justify="left", fg="#222")
    boxes_list.pack(fill="x", padx=8, pady=2)

    canvas = tk.Canvas(root, bg="#222", highlightthickness=0)
    canvas.pack(fill="both", expand=True)

    # ─── helpers ───────────────────────────────────────────────────────────
    def fit_scale(w: int, h: int) -> float:
        return min(SCALE_MAX_W / w, SCALE_MAX_H / h, 1.0)

    def save_json():
        out_path.write_text(json.dumps(annotations, indent=2))

    def refresh_status_line():
        s = mode["state"]
        active = label_state["active"]
        if s == "idle":
            status.config(text=f"→ click corner 1 to start a new box "
                               f"(label = {active})")
        elif s == "placed_first":
            status.config(text=f"corner 1 set for \"{active}\" — "
                               f"click corner 2 to make the draft")
        elif s == "draft":
            status.config(text=f"DRAFT for \"{active}\" — click a corner to "
                               f"grab/reshape, or press Enter to accept")
        elif s == "draft_grabbing":
            status.config(text=f"corner grabbed (draft) — click new position")
        elif s == "perm_grabbing":
            idx, corner = mode["grab"]
            lbl = annotations[state["idx"]]["boxes"][idx]["label"]
            status.config(text=f"corner {corner} of \"{lbl}\" grabbed — "
                               f"click new position")

    def render_all():
        canvas.delete("box")
        canvas.delete("draft")
        canvas.delete("marker")
        scale = state["scale"]
        ann = annotations[state["idx"]]

        # permanent boxes
        labels = []
        for b in ann["boxes"]:
            x0, y0 = b["l_px"] * scale, b["t_px"] * scale
            x1, y1 = b["r_px"] * scale, b["b_px"] * scale
            color = "#3f3" if b["label"] == "ideal_crop" else "#ff0"
            canvas.create_rectangle(x0, y0, x1, y1, outline=color, width=2,
                                    tags="box")
            for cx, cy in [(x0, y0), (x1, y0), (x0, y1), (x1, y1)]:
                canvas.create_rectangle(cx - 4, cy - 4, cx + 4, cy + 4,
                                        outline=color, fill=color, tags="box")
            canvas.create_text(x0 + 4, y0 + 4, anchor="nw", text=b["label"],
                               fill=color, font=("TkDefaultFont", 11, "bold"),
                               tags="box")
            labels.append(b["label"])
        boxes_list.config(text="committed boxes: " +
                          (", ".join(labels) if labels else "(none)"))

        # draft box
        if mode["draft"] is not None:
            d = mode["draft"]
            x0, y0 = d["l"] * scale, d["t"] * scale
            x1, y1 = d["r"] * scale, d["b"] * scale
            canvas.create_rectangle(x0, y0, x1, y1, outline="#f0f",
                                    width=2, dash=(6, 3), tags="draft")
            for cx, cy in [(x0, y0), (x1, y0), (x0, y1), (x1, y1)]:
                canvas.create_oval(cx - 7, cy - 7, cx + 7, cy + 7,
                                   outline="#f0f", width=2, tags="draft")

        # grab marker
        if mode["state"] in ("placed_first",) and mode["first_xy"]:
            x, y = mode["first_xy"]
            canvas.create_oval(x - 7, y - 7, x + 7, y + 7,
                               outline="#0ff", width=2, tags="marker")

        refresh_status_line()

    def load_image():
        c, png = captures[state["idx"]]
        pil = Image.open(BytesIO(png))
        w, h = pil.size
        annotations[state["idx"]]["image_size_px"] = [w, h]
        scale = fit_scale(w, h)
        disp = pil.resize((int(w * scale), int(h * scale)))
        tkimg = ImageTk.PhotoImage(disp)
        state["image"] = tkimg
        state["scale"] = scale
        canvas.config(width=disp.size[0], height=disp.size[1])
        canvas.delete("all")
        canvas.create_image(0, 0, anchor="nw", image=tkimg)
        info.config(text=f"[{state['idx']+1}/{len(captures)}]  {c.url}\n"
                         f"{w}×{h}px  sha256={c.sha256[:12]}…")
        mode["state"] = "idle"
        mode["first_xy"] = None
        mode["draft"] = None
        mode["grab"] = None
        render_all()

    # ─── corner pickers ────────────────────────────────────────────────────
    def nearest_perm_corner(x, y):
        ann = annotations[state["idx"]]
        scale = state["scale"]
        best, best_d = None, CORNER_PICK_PX + 1
        for i, b in enumerate(ann["boxes"]):
            corners = {
                "tl": (b["l_px"] * scale, b["t_px"] * scale),
                "tr": (b["r_px"] * scale, b["t_px"] * scale),
                "bl": (b["l_px"] * scale, b["b_px"] * scale),
                "br": (b["r_px"] * scale, b["b_px"] * scale),
            }
            for name, (cx, cy) in corners.items():
                d = max(abs(cx - x), abs(cy - y))
                if d < best_d:
                    best_d = d
                    best = (i, name)
        return best

    def nearest_draft_corner(x, y):
        d = mode["draft"]
        if d is None:
            return None
        scale = state["scale"]
        corners = {
            "tl": (d["l"] * scale, d["t"] * scale),
            "tr": (d["r"] * scale, d["t"] * scale),
            "bl": (d["l"] * scale, d["b"] * scale),
            "br": (d["r"] * scale, d["b"] * scale),
        }
        best, best_d = None, CORNER_PICK_PX + 1
        for name, (cx, cy) in corners.items():
            dist = max(abs(cx - x), abs(cy - y))
            if dist < best_d:
                best_d = dist
                best = name
        return best

    # ─── actions ───────────────────────────────────────────────────────────
    def accept_draft():
        if mode["state"] not in ("draft", "draft_grabbing"):
            status.config(text="(no draft to accept)")
            return
        d = mode["draft"]
        if abs(d["r"] - d["l"]) < 6 or abs(d["b"] - d["t"]) < 6:
            status.config(text="draft is too small — reshape or discard")
            return
        annotations[state["idx"]]["boxes"].append({
            "label": label_state["active"],
            "l_px": int(d["l"]),
            "t_px": int(d["t"]),
            "r_px": int(d["r"]),
            "b_px": int(d["b"]),
        })
        mode["state"] = "idle"
        mode["draft"] = None
        mode["grab"] = None
        render_all()

    def discard_draft():
        mode["state"] = "idle"
        mode["first_xy"] = None
        mode["draft"] = None
        mode["grab"] = None
        render_all()

    def undo(_=None):
        if annotations[state["idx"]]["boxes"]:
            annotations[state["idx"]]["boxes"].pop()
            render_all()

    def save_now(_=None):
        save_json()
        status.config(text="saved ✓")

    def next_img(_=None):
        if state["idx"] < len(captures) - 1:
            state["idx"] += 1
            load_image()

    def prev_img(_=None):
        if state["idx"] > 0:
            state["idx"] -= 1
            load_image()

    def quit_(_=None):
        save_json()
        root.destroy()

    # ─── click handler ─────────────────────────────────────────────────────
    def on_click(e):
        canvas.focus_set()
        scale = state["scale"]
        s = mode["state"]

        if s == "idle":
            # first try grabbing a draft corner (no draft exists here, so skip)
            # then try grabbing a perm corner
            pick = nearest_perm_corner(e.x, e.y)
            if pick is not None:
                mode["state"] = "perm_grabbing"
                mode["grab"] = pick
                render_all()
                return
            # otherwise start a new draft: this is corner 1
            mode["state"] = "placed_first"
            mode["first_xy"] = (e.x, e.y)
            render_all()
            return

        if s == "placed_first":
            x0, y0 = mode["first_xy"]
            x1, y1 = e.x, e.y
            mode["draft"] = {
                "l": min(x0, x1) / scale,
                "t": min(y0, y1) / scale,
                "r": max(x0, x1) / scale,
                "b": max(y0, y1) / scale,
            }
            mode["first_xy"] = None
            mode["state"] = "draft"
            render_all()
            return

        if s == "draft":
            # try grabbing a draft corner first (it sits on top)
            dc = nearest_draft_corner(e.x, e.y)
            if dc is not None:
                mode["state"] = "draft_grabbing"
                mode["grab"] = ("draft", dc)
                render_all()
                return
            # else: clicking outside the draft does nothing — user must Accept
            # or Esc. (Avoids surprising the user by starting a new draft and
            # losing the one in progress.)
            status.config(text="click a draft corner to reshape, or press Enter to accept "
                               "(Esc to discard)")
            return

        if s == "draft_grabbing":
            which, corner = mode["grab"]
            d = mode["draft"]
            nx, ny = e.x / scale, e.y / scale
            if corner == "tl":
                d["l"] = min(nx, d["r"] - 4)
                d["t"] = min(ny, d["b"] - 4)
            elif corner == "tr":
                d["r"] = max(nx, d["l"] + 4)
                d["t"] = min(ny, d["b"] - 4)
            elif corner == "bl":
                d["l"] = min(nx, d["r"] - 4)
                d["b"] = max(ny, d["t"] + 4)
            elif corner == "br":
                d["r"] = max(nx, d["l"] + 4)
                d["b"] = max(ny, d["t"] + 4)
            mode["state"] = "draft"
            mode["grab"] = None
            render_all()
            return

        if s == "perm_grabbing":
            idx, corner = mode["grab"]
            b = annotations[state["idx"]]["boxes"][idx]
            nx, ny = int(e.x / scale), int(e.y / scale)
            if corner == "tl":
                b["l_px"] = min(nx, b["r_px"] - 4)
                b["t_px"] = min(ny, b["b_px"] - 4)
            elif corner == "tr":
                b["r_px"] = max(nx, b["l_px"] + 4)
                b["t_px"] = min(ny, b["b_px"] - 4)
            elif corner == "bl":
                b["l_px"] = min(nx, b["r_px"] - 4)
                b["b_px"] = max(ny, b["t_px"] + 4)
            elif corner == "br":
                b["r_px"] = max(nx, b["l_px"] + 4)
                b["b_px"] = max(ny, b["t_px"] + 4)
            mode["state"] = "idle"
            mode["grab"] = None
            render_all()
            return

    # ─── bindings ──────────────────────────────────────────────────────────
    canvas.bind("<ButtonRelease-1>", on_click)
    canvas.bind("<Enter>", lambda _: canvas.focus_set())
    root.bind("<Return>", lambda _: accept_draft())
    root.bind("<KP_Enter>", lambda _: accept_draft())
    root.bind("<Escape>", lambda _: discard_draft())
    root.bind("n", next_img)
    root.bind("<Right>", next_img)
    root.bind("p", prev_img)
    root.bind("<Left>", prev_img)
    root.bind("u", undo)
    root.bind("s", save_now)
    root.bind("q", quit_)
    root.protocol("WM_DELETE_WINDOW", quit_)

    set_active_label("ideal_crop")
    load_image()
    canvas.focus_set()
    root.mainloop()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--docx", required=True, type=Path)
    p.add_argument("--platform", required=True,
                   help="platform id, comma-separated list, or 'all'")
    p.add_argument("--post-style", required=True,
                   choices=["main_account", "single_post"])
    p.add_argument("--out", required=True, type=Path)
    args = p.parse_args()

    platforms = [p.strip() for p in args.platform.split(",") if p.strip()]
    captures = _filter_captures(args.docx, platforms, args.post_style)
    if not captures:
        print(f"No {args.platform} {args.post_style} captures found in {args.docx}")
        return 1

    print(f"Annotating {len(captures)} captures → {args.out}")
    run_annotator(captures, args.out)
    print(f"Saved {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
