"""
Build per-platform exhibit docx files from a parsed Hunchly export.

For each platform, clone the source docx zip, replace word/document.xml with a
filtered + reordered body where:
  - Only that platform's capture tables remain, sorted oldest post-date first.
  - Each kept table has its Row 1 image XML modified (srcRect crop + extent
    resize) per the matched preset.
  - Each kept table has its Row 10 caption rewritten with the real post date
    and the extraction method.

The original PNGs in word/media/ are NEVER modified. All visual changes are
display-only XML transformations.
"""
from __future__ import annotations

import copy
import io
import shutil
import struct
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lxml import etree

from .dates import DateResult
from .parser import ParsedDocx, Capture
from .presets import Preset, calculate_display_dims
from . import vision


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
PIC_NS = "http://schemas.openxmlformats.org/drawingml/2006/picture"
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"


@dataclass
class ExhibitInput:
    """One capture to write into a platform doc, with all decisions resolved."""
    capture: Capture
    date_result: DateResult           # post_date must be set (sort key)
    preset: Preset
    exhibit_number: int               # 1-indexed within this platform doc
    note: str | None = None           # analyst's Hunchly note (gates page-title rows)


def _png_dims(data: bytes) -> tuple[int, int]:
    """Read width/height from a PNG IHDR chunk (bytes 16-23)."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _jpeg_dims(data: bytes) -> tuple[int, int]:
    """Read width/height from JPEG SOFn marker."""
    if data[:2] != b"\xff\xd8":
        raise ValueError("not a JPEG")
    i = 2
    n = len(data)
    while i < n - 9:
        if data[i] != 0xFF:
            raise ValueError("JPEG marker desync")
        marker = data[i + 1]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height = (data[i + 5] << 8) | data[i + 6]
            width = (data[i + 7] << 8) | data[i + 8]
            return width, height
        length = (data[i + 2] << 8) | data[i + 3]
        i += 2 + length
    raise ValueError("no SOFn marker in JPEG")


def _image_dims(data: bytes) -> tuple[int, int]:
    """Detect PNG vs JPEG (Hunchly names JPEGs as .png) and return (w, h)."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return _png_dims(data)
    if data[:2] == b"\xff\xd8":
        return _jpeg_dims(data)
    raise ValueError("unsupported image format (not PNG or JPEG)")


def _read_extent(tbl: etree._Element) -> tuple[int | None, int | None]:
    """Read the existing <wp:extent cx cy> from the table's drawing."""
    drawing = tbl.find(f".//{{{W_NS}}}drawing")
    if drawing is None:
        return None, None
    ext = drawing.find(f".//{{{WP_NS}}}extent")
    if ext is None:
        return None, None
    try:
        return int(ext.get("cx") or "0"), int(ext.get("cy") or "0")
    except ValueError:
        return None, None


def _set_image_xml(tbl: etree._Element, cx_emu: int, cy_emu: int, src_rect: dict) -> None:
    """Inject srcRect + update both extents on the table's first <w:drawing>."""
    drawing = tbl.find(f".//{{{W_NS}}}drawing")
    if drawing is None:
        return

    for ext in drawing.iter(f"{{{WP_NS}}}extent"):
        ext.set("cx", str(cx_emu))
        ext.set("cy", str(cy_emu))

    for sp in drawing.iter(f"{{{PIC_NS}}}spPr"):
        for ext in sp.iter(f"{{{A_NS}}}ext"):
            ext.set("cx", str(cx_emu))
            ext.set("cy", str(cy_emu))

    needs_crop = any(src_rect[k] > 0 for k in ("l", "t", "r", "b"))
    if not needs_crop:
        return

    for blip_fill in drawing.iter(f"{{{PIC_NS}}}blipFill"):
        existing = blip_fill.find(f"{{{A_NS}}}srcRect")
        if existing is not None:
            blip_fill.remove(existing)
        blip = blip_fill.find(f"{{{A_NS}}}blip")
        src = etree.SubElement(blip_fill, f"{{{A_NS}}}srcRect")
        for k in ("l", "t", "r", "b"):
            if src_rect[k] > 0:
                src.set(k, str(src_rect[k]))
        if blip is not None:
            blip.addnext(src)


import re as _re


def _make_para(text_lines: list[str], bold_first: bool = False) -> etree._Element:
    """Build a <w:p> with each line in its own <w:r>, separated by <w:br/>."""
    p = etree.Element(f"{{{W_NS}}}p")
    for i, line in enumerate(text_lines):
        r = etree.SubElement(p, f"{{{W_NS}}}r")
        if bold_first and i == 0:
            rpr = etree.SubElement(r, f"{{{W_NS}}}rPr")
            etree.SubElement(rpr, f"{{{W_NS}}}b")
        if i > 0:
            etree.SubElement(r, f"{{{W_NS}}}br")
        t = etree.SubElement(r, f"{{{W_NS}}}t")
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        t.text = line
    return p


_POST_TAB_RE = _re.compile(r"(\t[\s\t]*)(\S.*)$", _re.DOTALL)


def _replace_after_last_tab(cell: etree._Element, replacement: str) -> bool:
    """
    In Hunchly's Row 10, the cell text looks like:
        '<capture_date> \\t\\t\\t <updated_date>'
    all inside a single <w:t>. Replace the post-tab portion with `replacement`,
    leaving the capture-date side and the tab spacing alone. Returns True on hit.
    """
    t_els = cell.findall(f".//{{{W_NS}}}t")
    last_with_tab = None
    for t in t_els:
        if t.text and "\t" in t.text:
            last_with_tab = t

    if last_with_tab is not None:
        new_text, n = _POST_TAB_RE.subn(
            lambda m: m.group(1) + replacement, last_with_tab.text
        )
        if n:
            last_with_tab.text = new_text
            last_with_tab.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            return True
    return False


def _content_cell(row: etree._Element) -> etree._Element | None:
    """
    Return the cell in `row` that actually holds the content. Hunchly's table
    has 3 cells per row but only the middle one has width=100%; the other two
    are empty spacer slivers. Picking the wrong cell makes text wrap one char
    per line. Prefer (in order): the cell that already contains text, then the
    cell with the largest declared width, then the first cell.
    """
    cells = row.findall(f"{{{W_NS}}}tc")
    if not cells:
        return None

    for cell in cells:
        if any((t.text or "").strip() for t in cell.findall(f".//{{{W_NS}}}t")):
            return cell

    best = cells[0]
    best_w = -1
    for cell in cells:
        tc_w = cell.find(f".//{{{W_NS}}}tcW")
        if tc_w is None:
            continue
        try:
            w = int(tc_w.get(f"{{{W_NS}}}w") or "0")
        except ValueError:
            w = 0
        if tc_w.get(f"{{{W_NS}}}type") == "pct":
            w *= 100
        if w > best_w:
            best_w, best = w, cell
    return best


def _tidy_url_row(tbl: etree._Element) -> None:
    """
    Hunchly's Row 6 stores the URL as " https://...\\n" — leading space, trailing
    newline. For long URLs (e.g. Facebook pfbid permalinks) the leading space
    becomes a soft-wrap point, pushing the URL onto a second line and leaving a
    visible gap below the "URL:" label. Strip the surrounding whitespace; the
    URL string itself is unchanged.
    """
    rows = tbl.findall(f"{{{W_NS}}}tr")
    if len(rows) < 6:
        return
    cell = _content_cell(rows[5])
    if cell is None:
        return
    for t in cell.findall(f".//{{{W_NS}}}t"):
        if t.text:
            t.text = t.text.strip()


def _relabel_updated_date(tbl: etree._Element) -> None:
    """
    Row 9 holds the labels 'Capture date: \\t...\\t Updated date:'. Hunchly's
    'Updated date' is really the capture time; we substitute the real post date
    in Row 10, so the label must read 'Post date:' to match. Replace the label
    text in place, preserving tab alignment.
    """
    rows = tbl.findall(f"{{{W_NS}}}tr")
    if len(rows) < 9:
        return
    cell = _content_cell(rows[8])
    if cell is None:
        return
    for t in cell.findall(f".//{{{W_NS}}}t"):
        if t.text and "updated date" in t.text.lower():
            t.text = _re.sub(r"(?i)updated date", "Post date", t.text)


def _swap_updated_date(tbl: etree._Element, post_date_str: str) -> None:
    """
    Replace the 'Updated date' value in Row 10 with the post date, preserving
    the cell/paragraph/run structure and the tab alignment exactly as Hunchly
    laid it out. Capture-date side is untouched. Also relabels the Row 9 header
    from 'Updated date:' to 'Post date:'.
    """
    rows = tbl.findall(f"{{{W_NS}}}tr")
    if len(rows) < 10:
        return
    cell = _content_cell(rows[9])
    if cell is None:
        return
    _replace_after_last_tab(cell, post_date_str)
    _relabel_updated_date(tbl)


def _parse_capture_tz(capture_date_raw: str):
    """
    Pull the UTC offset out of Hunchly's capture-date string, e.g.
    '2026-05-23 18:01:16 PM GMT -04:00' -> timezone(-4h). Returns None if absent.
    """
    m = _re.search(r"([+-])(\d{2}):?(\d{2})\s*$", capture_date_raw.strip())
    if not m:
        return None
    sign = 1 if m.group(1) == "+" else -1
    return timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3))))


def _format_post_date(ex: ExhibitInput) -> str:
    """
    Render the post date for Row 10 as the calendar day only (YYYY-MM-DD).

    The post date is still resolved in the SAME timezone as the capture date
    before the time is dropped, because the local-day can differ from the UTC
    day (e.g. 03:06 UTC on the 18th is 23:06 on the 17th at GMT -04:00).
    Timezone-naive post dates (e.g. the Facebook unscrambler) are shown as-is.
    """
    dt = ex.date_result.post_date
    if dt.tzinfo is None:
        return dt.strftime("%Y-%m-%d")

    tz = _parse_capture_tz(ex.capture.capture_date_raw)
    local = dt.astimezone(tz) if tz is not None else dt.astimezone(timezone.utc)
    return local.strftime("%Y-%m-%d")


def _section_heading(text: str) -> etree._Element:
    """A single bold paragraph used as the doc header."""
    return _make_para([text], bold_first=True)


def _make_page_break() -> etree._Element:
    """A paragraph containing a single hard page break."""
    p = etree.Element(f"{{{W_NS}}}p")
    r = etree.SubElement(p, f"{{{W_NS}}}r")
    br = etree.SubElement(r, f"{{{W_NS}}}br")
    br.set(f"{{{W_NS}}}type", "page")
    return p


def _resize_table_image(
    tbl: etree._Element,
    capture: Capture,
    preset: Preset,
    media_lookup,
) -> dict:
    """
    Apply crop + resize XML to a cloned capture table.

    Returns a dict describing how the crop was decided, for the manifest:
        {"crop_source": "cv_detected" | "static_preset" | "no_image",
         "crop_pct": {left_pct, top_pct, right_pct, bottom_pct},
         "cv_components": {component_id: (left,top,right,bottom)} or None}
    """
    media_path = capture.image_media_path
    png_bytes = media_lookup(media_path) if media_path else None
    if png_bytes:
        orig_w, orig_h = _image_dims(png_bytes)
    else:
        orig_w, orig_h = 3024, 1552

    crop = preset.crop
    crop_source = "static_preset"
    cv_components: dict | None = None
    if preset.components and png_bytes:
        detected = vision.detect_components(
            png_bytes,
            preset.platform,
            preset.post_style,
            preset.components,
        )
        if detected:
            union = vision.union_bboxes(list(detected.values()))
            crop = vision.bbox_to_crop_pct(union, orig_w, orig_h)
            crop_source = "cv_detected"
            cv_components = {
                k: (int(b.left), int(b.top), int(b.right), int(b.bottom))
                for k, b in detected.items()
            }

    current_cx, current_cy = _read_extent(tbl)
    cx, cy, src_rect = calculate_display_dims(
        orig_w, orig_h, crop, preset.size,
        current_cx_emu=current_cx, current_cy_emu=current_cy,
    )
    _set_image_xml(tbl, cx, cy, src_rect)

    return {
        "crop_source": crop_source if png_bytes else "no_image",
        "crop_pct": dict(crop),
        "cv_components": cv_components,
    }


_KEEP_PAGE_TITLE_RE = _re.compile(r"\bpage\b", _re.IGNORECASE)


def _modify_table_for_exhibit(
    tbl: etree._Element,
    ex: ExhibitInput,
    media_bytes_lookup,
) -> dict:
    """Apply image XML surgery + caption rewrite in place. Returns the crop decision."""
    decision = _resize_table_image(tbl, ex.capture, ex.preset, media_bytes_lookup)
    _tidy_url_row(tbl)
    _swap_updated_date(tbl, _format_post_date(ex))
    # Drop the Page Title label + value rows (indices 2, 3) — they're usually
    # noise (post text / emoji). Keep them only when the analyst's note mentions
    # "page" (i.e. the page title is meaningful here). Must run LAST: the ops
    # above address rows by their original index.
    if not _KEEP_PAGE_TITLE_RE.search(ex.note or ""):
        _remove_table_rows(tbl, [2, 3])
    return decision


def _strip_after_first_tab(cell: etree._Element) -> bool:
    """
    Truncate the cell's content at the first tab character, dropping everything
    after. Used in the Accounts Located doc to strip the second column from
    Hunchly's two-column rows (Capture date \t Post date, etc.), leaving only
    the capture-date side. Handles both inline `\\t` chars in <w:t> text and
    the separate <w:tab/> element representation.
    """
    found = False
    for t in cell.iter(f"{{{W_NS}}}t"):
        if found:
            t.text = ""
        elif t.text and "\t" in t.text:
            t.text = t.text.split("\t", 1)[0]
            found = True
    for tab in list(cell.iter(f"{{{W_NS}}}tab")):
        parent = tab.getparent()
        if parent is not None:
            parent.remove(tab)
            found = True
    return found


def _remove_table_rows(tbl: etree._Element, indices: list[int]) -> None:
    """Remove rows at the given 0-based indices. Iterates in reverse to keep
    earlier indices valid as we delete."""
    rows = tbl.findall(f"{{{W_NS}}}tr")
    for i in sorted(set(indices), reverse=True):
        if 0 <= i < len(rows):
            tbl.remove(rows[i])


def _modify_table_for_locator(
    tbl: etree._Element,
    capture: Capture,
    preset: Preset,
    media_bytes_lookup,
) -> dict:
    """
    Apply the exhibit treatment to a main-account capture, MINUS the post-date
    swap (profile/landing pages have no single post date). Also drops the
    Page Title rows entirely and strips the "Post date" column from the
    capture-date rows — profile pages don't have a single post date and
    Hunchly's page title isn't useful here.

    Returns the crop decision so the caller can record it in the manifest.

    Resulting layout: image, URL, hash, capture date. Nothing else.
    """
    decision = _resize_table_image(tbl, capture, preset, media_bytes_lookup)
    _tidy_url_row(tbl)

    # Strip the post-date column from Row 9 (label) and Row 10 (value).
    # These are 0-indexed positions 8 and 9 in the original Hunchly layout.
    rows = tbl.findall(f"{{{W_NS}}}tr")
    for idx in (8, 9):
        if idx < len(rows):
            cell = _content_cell(rows[idx])
            if cell is not None:
                _strip_after_first_tab(cell)

    # Drop the Page Title rows (Row 3 label + Row 4 value = indices 2, 3).
    _remove_table_rows(tbl, [2, 3])
    return decision


def _set_moderate_margins(sect_pr: etree._Element) -> None:
    """Set Word's 'Moderate' page margins on this section: 1" top/bottom,
    0.75" left/right. Values are twips (1 inch = 1440 twips). Header/footer/
    gutter are preserved if already present, else given Word's defaults."""
    pg = sect_pr.find(f"{{{W_NS}}}pgMar")
    if pg is None:
        pg = etree.Element(f"{{{W_NS}}}pgMar")
        pg_sz = sect_pr.find(f"{{{W_NS}}}pgSz")   # pgMar must follow pgSz in the schema
        if pg_sz is not None:
            pg_sz.addnext(pg)
        else:
            sect_pr.insert(0, pg)
    pg.set(f"{{{W_NS}}}top", "1440")
    pg.set(f"{{{W_NS}}}bottom", "1440")
    pg.set(f"{{{W_NS}}}left", "1080")
    pg.set(f"{{{W_NS}}}right", "1080")
    for attr, default in (("header", "708"), ("footer", "708"), ("gutter", "0")):
        if pg.get(f"{{{W_NS}}}{attr}") is None:
            pg.set(f"{{{W_NS}}}{attr}", default)


_HEADER_PART = "word/header_hrb.xml"
_HEADER_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"
_HEADER_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/header"


def _build_header_xml(label: str) -> bytes:
    """Header part: '<label> ' followed by a live PAGE field, Times New Roman
    12pt (sz is half-points, so 24).

    The page number uses the explicit fldChar begin/separate/end construction
    (not fldSimple) with the font on EVERY run — including the result run — so
    Word keeps the 12pt when it re-evaluates the field on open/print."""
    esc = label.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    rpr = ('<w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" '
           'w:cs="Times New Roman"/><w:sz w:val="24"/><w:szCs w:val="24"/>')

    def run(inner: str) -> str:
        return f'<w:r><w:rPr>{rpr}</w:rPr>{inner}</w:r>'

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f'<w:p><w:pPr><w:rPr>{rpr}</w:rPr></w:pPr>'
        + run(f'<w:t xml:space="preserve">{esc} </w:t>')
        + run('<w:fldChar w:fldCharType="begin"/>')
        + run('<w:instrText xml:space="preserve"> PAGE </w:instrText>')
        + run('<w:fldChar w:fldCharType="separate"/>')
        + run('<w:t>1</w:t>')
        + run('<w:fldChar w:fldCharType="end"/>')
        + '</w:p></w:hdr>'
    ).encode("utf-8")


def _next_rid(rels_xml: bytes) -> str:
    ids = [int(m) for m in _re.findall(rb'Id="rId(\d+)"', rels_xml)]
    return f"rId{(max(ids) + 1) if ids else 1}"


def _write_docx_with_body(
    parsed: ParsedDocx,
    body_elements: list[etree._Element],
    output_path: Path,
    header_label: str | None = None,
) -> None:
    """Clone the source zip, replace word/document.xml with a body built from
    `body_elements`. When `header_label` is given, strip any footer and add a
    header showing '<label> <page number>' (Times New Roman 12pt)."""
    tree = etree.fromstring(parsed.doc_xml)
    body = tree.find(f"{{{W_NS}}}body")
    if body is None:
        raise ValueError("source docx has no <w:body>")

    sect_pr = body.find(f"{{{W_NS}}}sectPr")
    for child in list(body):
        body.remove(child)
    for el in body_elements:
        body.append(el)
    if sect_pr is None:
        sect_pr = etree.Element(f"{{{W_NS}}}sectPr")
    _set_moderate_margins(sect_pr)

    header_xml = new_rels = new_ct = None
    if header_label is not None:
        # drop the footer entirely
        for fr in sect_pr.findall(f"{{{W_NS}}}footerReference"):
            sect_pr.remove(fr)
        with zipfile.ZipFile(io.BytesIO(parsed.zip_bytes)) as zf:
            rels_xml = zf.read("word/_rels/document.xml.rels")
            ct_xml = zf.read("[Content_Types].xml")
        rid = _next_rid(rels_xml)
        hr = etree.Element(f"{{{W_NS}}}headerReference")
        hr.set(f"{{{W_NS}}}type", "default")
        hr.set(f"{{{R_NS}}}id", rid)
        sect_pr.insert(0, hr)   # header/footer refs lead the sectPr
        header_xml = _build_header_xml(header_label)
        rel = (f'<Relationship Id="{rid}" Type="{_HEADER_REL_TYPE}" '
               f'Target="header_hrb.xml"/>').encode("utf-8")
        new_rels = rels_xml.replace(b"</Relationships>", rel + b"</Relationships>")
        override = (f'<Override PartName="/{_HEADER_PART}" '
                    f'ContentType="{_HEADER_CT}"/>').encode("utf-8")
        new_ct = ct_xml.replace(b"</Types>", override + b"</Types>")

    body.append(sect_pr)
    new_doc_xml = etree.tostring(tree, xml_declaration=True, encoding="UTF-8", standalone=True)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(parsed.zip_bytes)) as zf:
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as out:
            for item in zf.infolist():
                fn = item.filename
                if fn == "word/document.xml":
                    out.writestr(item, new_doc_xml)
                elif header_label is not None and fn == "word/_rels/document.xml.rels":
                    out.writestr(item, new_rels)
                elif header_label is not None and fn == "[Content_Types].xml":
                    out.writestr(item, new_ct)
                else:
                    out.writestr(item, zf.read(fn))
            if header_xml is not None:
                out.writestr(_HEADER_PART, header_xml)


def _media_lookup_for(parsed: ParsedDocx):
    """Build a cached media reader bound to the source zip."""
    cache: dict[str, bytes] = {}
    zf = zipfile.ZipFile(io.BytesIO(parsed.zip_bytes))

    def _read(path: str) -> bytes | None:
        if path in cache:
            return cache[path]
        try:
            data = zf.read(path)
        except KeyError:
            return None
        cache[path] = data
        return data

    return _read


def write_platform_docx(
    parsed: ParsedDocx,
    platform_display: str,
    exhibits: list[ExhibitInput],
    output_path: Path,
) -> list[dict]:
    """
    Build a new docx containing only `exhibits`, ordered by their list position
    (caller already sorted by post date). One exhibit per page.

    Returns a list of crop-decision dicts parallel to `exhibits` so the caller
    can record `crop_source` / `crop_pct` in the manifest.
    """
    tree = etree.fromstring(parsed.doc_xml)
    src_tables = tree.find(f"{{{W_NS}}}body").findall(f"{{{W_NS}}}tbl")
    media_lookup = _media_lookup_for(parsed)

    # No on-page heading — the platform name lives in the page header instead.
    elements: list[etree._Element] = []
    decisions: list[dict] = []
    for i, ex in enumerate(exhibits):
        cloned = copy.deepcopy(src_tables[ex.capture.index])
        decisions.append(_modify_table_for_exhibit(cloned, ex, media_lookup))
        elements.append(cloned)
        if i < len(exhibits) - 1:
            elements.append(_make_page_break())

    _write_docx_with_body(parsed, elements, output_path, header_label=platform_display)
    return decisions


def write_locator_docx(
    parsed: ParsedDocx,
    sections: list[tuple[str, list[tuple[Capture, Preset]]]],
    output_path: Path,
    doc_title: str,
) -> dict[str, dict]:
    """
    Build the Accounts Located doc using the same per-capture layout as a
    platform exhibit doc — full Hunchly table (image + title + URL + hash +
    dates), one capture per page — minus the post-date swap.

    `sections` is a list of (platform_display, [(capture, preset), ...]).
    Sections are emitted in list order, each with its own platform heading,
    and every capture lives on its own page.

    Returns {capture.sha256: crop_decision} so the caller can annotate the
    Accounts Located entries in the manifest with how the crop was decided.
    """
    tree = etree.fromstring(parsed.doc_xml)
    src_tables = tree.find(f"{{{W_NS}}}body").findall(f"{{{W_NS}}}tbl")
    media_lookup = _media_lookup_for(parsed)

    # No on-page title — "Accounts Located" lives in the page header instead.
    # Per-platform section headings inside the body stay (they organize the index).
    elements: list[etree._Element] = []

    flat: list[tuple[str | None, Capture, Preset]] = []
    for platform_display, items in sections:
        if not items:
            continue
        first = True
        for capture, preset in items:
            flat.append((platform_display if first else None, capture, preset))
            first = False

    decisions: dict[str, dict] = {}
    for i, (heading, capture, preset) in enumerate(flat):
        if heading:
            if i > 0:
                elements.append(_make_page_break())
            elements.append(_section_heading(heading))
        cloned = copy.deepcopy(src_tables[capture.index])
        decisions[capture.sha256] = _modify_table_for_locator(cloned, capture, preset, media_lookup)
        elements.append(cloned)
        is_last = i == len(flat) - 1
        next_has_heading = (not is_last) and flat[i + 1][0] is not None
        if not is_last and not next_has_heading:
            elements.append(_make_page_break())

    _write_docx_with_body(parsed, elements, output_path, header_label="Accounts Located")
    return decisions
