"""
Preset loader. Each preset = crop + size for a specific URL pattern.

Directory layout:
    presets/
        _defaults.json              # platform -> default preset (fallback)
        <platform>/<style>.json     # one preset per post style

Each preset JSON has:
    platform, post_style, url_patterns (list of regex), crop, size, ...

Selection: first preset for the URL's platform whose url_patterns regex matches.
If none match, fall back to _defaults.json[platform] and flag the URL.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


EMU_PER_INCH = 914400


@dataclass
class Preset:
    platform: str
    post_style: str
    crop: dict                       # {left_pct, top_pct, right_pct, bottom_pct}
    size: dict                       # {mode, ...}
    url_patterns: list[re.Pattern] = field(default_factory=list)
    source_path: str = ""
    raw: dict = field(default_factory=dict)


class PresetLibrary:
    def __init__(self, presets_dir: str | Path):
        self.presets_dir = Path(presets_dir)
        self._by_platform: dict[str, list[Preset]] = {}
        self._defaults: dict[str, Preset] = {}
        self.unmatched: list[dict] = []   # collected mismatches for NEW_PRESETS_NEEDED.json
        self._load()

    def _load(self) -> None:
        defaults_path = self.presets_dir / "_defaults.json"
        if defaults_path.exists():
            data = json.loads(defaults_path.read_text())
            for key, val in data.items():
                if key.startswith("_"):
                    continue
                self._defaults[key] = self._make(val, str(defaults_path))

        for sub in sorted(self.presets_dir.iterdir()):
            if not sub.is_dir():
                continue
            platform = sub.name
            for jf in sorted(sub.glob("*.json")):
                try:
                    data = json.loads(jf.read_text())
                except json.JSONDecodeError as e:
                    raise ValueError(f"{jf}: invalid JSON ({e})") from e
                preset = self._make(data, str(jf), default_platform=platform)
                self._by_platform.setdefault(platform, []).append(preset)

    @staticmethod
    def _make(data: dict, source: str, default_platform: str | None = None) -> Preset:
        crop = data.get("crop") or {"left_pct": 0, "top_pct": 0, "right_pct": 0, "bottom_pct": 0}
        size = data.get("size") or {"mode": "fixed_width", "width_inches": 5.5}
        patterns = [re.compile(p, re.I) for p in (data.get("url_patterns") or [])]
        return Preset(
            platform=data.get("platform") or default_platform or "other",
            post_style=data.get("post_style", "_default"),
            crop=crop,
            size=size,
            url_patterns=patterns,
            source_path=source,
            raw=data,
        )

    def match(self, url: str, platform: str) -> tuple[Preset, bool]:
        """
        Return (preset, matched_specific).
        matched_specific=False means we fell back to the platform default and
        the URL is logged in `unmatched`.
        """
        for p in self._by_platform.get(platform, []):
            for pat in p.url_patterns:
                if pat.search(url):
                    return p, True
        fallback = self._defaults.get(platform) or self._defaults.get("other")
        if fallback is None:
            fallback = Preset(
                platform=platform,
                post_style="_hardcoded_default",
                crop={"left_pct": 0, "top_pct": 0, "right_pct": 0, "bottom_pct": 0},
                size={"mode": "fixed_width", "width_inches": 5.5},
            )
        self.unmatched.append({
            "url": url,
            "platform": platform,
            "fallback_preset": fallback.source_path,
        })
        return fallback, False


def calculate_display_dims(
    orig_w_px: int,
    orig_h_px: int,
    crop: dict,
    size: dict,
    current_cx_emu: int | None = None,
    current_cy_emu: int | None = None,
) -> tuple[int, int, dict]:
    """
    Returns (cx_emu, cy_emu, srcRect dict ready for XML).

    crop pct values are 0-100. srcRect uses hundredths of a percent (0-100000).

    size["mode"]:
        - "preserve":     keep the table's existing <wp:extent> (no resize).
                          With a non-zero crop the existing display width is
                          retained and height is recomputed for the cropped
                          aspect ratio.
        - "fixed_width":  scale to size["width_inches"], height follows aspect.
        - "fixed_height": scale to size["height_inches"], width follows aspect.
        - "fit_box":      bounded by max_width_inches + max_height_inches.
        - "fixed_both":   explicit width AND height (may distort if crop set).
    """
    visible_w_pct = (100 - crop["left_pct"] - crop["right_pct"]) / 100.0
    visible_h_pct = (100 - crop["top_pct"] - crop["bottom_pct"]) / 100.0
    if visible_w_pct <= 0 or visible_h_pct <= 0:
        raise ValueError(f"Crop leaves no visible area: {crop}")

    visible_w_px = orig_w_px * visible_w_pct
    visible_h_px = orig_h_px * visible_h_pct
    aspect = visible_w_px / visible_h_px

    mode = size.get("mode", "preserve")
    if mode == "preserve":
        if current_cx_emu is None or current_cy_emu is None:
            raise ValueError("size.mode=preserve requires current extents")
        no_crop = all(crop[k] == 0 for k in ("left_pct", "top_pct", "right_pct", "bottom_pct"))
        if no_crop:
            cx_emu, cy_emu = current_cx_emu, current_cy_emu
        else:
            cx_emu = current_cx_emu
            cy_emu = int(current_cx_emu / aspect)
    elif mode == "fixed_width":
        disp_w = size["width_inches"]
        cx_emu = int(disp_w * EMU_PER_INCH)
        cy_emu = int((disp_w / aspect) * EMU_PER_INCH)
    elif mode == "fixed_height":
        disp_h = size["height_inches"]
        cx_emu = int((disp_h * aspect) * EMU_PER_INCH)
        cy_emu = int(disp_h * EMU_PER_INCH)
    elif mode == "fit_box":
        max_w = size["max_width_inches"]
        max_h = size["max_height_inches"]
        if max_w / aspect <= max_h:
            disp_w, disp_h = max_w, max_w / aspect
        else:
            disp_h, disp_w = max_h, max_h * aspect
        cx_emu = int(disp_w * EMU_PER_INCH)
        cy_emu = int(disp_h * EMU_PER_INCH)
    elif mode == "fixed_both":
        cx_emu = int(size["width_inches"] * EMU_PER_INCH)
        cy_emu = int(size["height_inches"] * EMU_PER_INCH)
    else:
        raise ValueError(f"Unknown size mode: {mode}")

    src_rect = {
        "l": int(crop["left_pct"] * 1000),
        "t": int(crop["top_pct"] * 1000),
        "r": int(crop["right_pct"] * 1000),
        "b": int(crop["bottom_pct"] * 1000),
    }
    return cx_emu, cy_emu, src_rect
