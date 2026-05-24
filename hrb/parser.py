"""
Parse a Hunchly-exported .docx into structured Capture records.

Each Hunchly capture = one <w:tbl> with 10 rows (verified against test export):
    Row 1:  the screenshot image (<w:drawing> with r:embed="rIdN")
    Row 2:  spacer
    Row 3:  label "Page title:"
    Row 4:  page title text
    Row 5:  label "URL:"
    Row 6:  URL (plain text, leading space, trailing newline)
    Row 7:  label "Hash (SHA-256):"
    Row 8:  hash
    Row 9:  label "Capture date: \\t...\\t Updated date:"
    Row 10: capture date \\t\\t\\t updated date (tab-separated)
"""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

from lxml import etree

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
PIC_NS = "http://schemas.openxmlformats.org/drawingml/2006/picture"
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
NSMAP = {"w": W_NS, "r": R_NS, "a": A_NS, "pic": PIC_NS, "wp": WP_NS}


@dataclass
class Capture:
    """One Hunchly capture, source-of-truth fields plus references for surgery."""
    index: int                       # 0-based position in source doc
    page_title: str
    url: str
    sha256: str
    capture_date_raw: str            # e.g. "2026-05-23 18:02:00 PM GMT -04:00"
    updated_date_raw: str            # usually identical to capture_date_raw
    image_rid: str | None            # e.g. "rId7"
    image_media_path: str | None     # e.g. "word/media/abc...png"


@dataclass
class ParsedDocx:
    """Everything writer.py needs to clone + selectively keep tables."""
    captures: list[Capture]
    doc_xml: bytes                   # raw word/document.xml content
    rels_xml: bytes                  # raw word/_rels/document.xml.rels content
    zip_bytes: bytes                 # raw .docx (the full zip) for cloning


def _row_text(row: etree._Element) -> str:
    """Concatenate all <w:t> text in a row (preserves order)."""
    return "".join(t.text or "" for t in row.findall(f".//{{{W_NS}}}t"))


def _parse_rels(rels_xml: bytes) -> dict[str, str]:
    """Return {rId: target} from word/_rels/document.xml.rels."""
    tree = etree.fromstring(rels_xml)
    out: dict[str, str] = {}
    for rel in tree.findall("{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"):
        rid = rel.get("Id")
        target = rel.get("Target")
        if rid and target:
            out[rid] = target
    return out


_DATE_ROW_RE = re.compile(r"^(.*?)\s*\t+\s*(.*?)\s*$", re.DOTALL)


def _split_date_row(text: str) -> tuple[str, str]:
    """Row 10 holds 'capture_date \\t\\t\\t updated_date'. Split on the run of tabs."""
    m = _DATE_ROW_RE.match(text.strip())
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return text.strip(), text.strip()


def parse_docx(path: str | Path) -> ParsedDocx:
    """
    Load a Hunchly export. Returns ParsedDocx with one Capture per <w:tbl>.

    Raises ValueError if a table doesn't match the expected 10-row Hunchly layout
    (so a malformed export fails loudly instead of silently producing junk).
    """
    path = Path(path)
    zip_bytes = path.read_bytes()
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        doc_xml = zf.read("word/document.xml")
        rels_xml = zf.read("word/_rels/document.xml.rels")

    rid_to_target = _parse_rels(rels_xml)
    tree = etree.fromstring(doc_xml)
    tables = tree.findall(f".//{{{W_NS}}}tbl")

    captures: list[Capture] = []
    for i, tbl in enumerate(tables):
        rows = tbl.findall(f"{{{W_NS}}}tr")
        if len(rows) != 10:
            raise ValueError(
                f"Table {i+1} has {len(rows)} rows; expected 10 (Hunchly capture layout)."
            )

        label3 = _row_text(rows[2]).strip().lower()
        label5 = _row_text(rows[4]).strip().lower()
        label7 = _row_text(rows[6]).strip().lower()
        if not (label3.startswith("page title") and label5.startswith("url")
                and label7.startswith("hash")):
            raise ValueError(
                f"Table {i+1} labels don't match Hunchly layout: "
                f"row3={label3!r} row5={label5!r} row7={label7!r}"
            )

        page_title = _row_text(rows[3]).strip()
        url = _row_text(rows[5]).strip()
        sha256 = _row_text(rows[7]).strip()
        cap_raw, upd_raw = _split_date_row(_row_text(rows[9]))

        blip = rows[0].find(f".//{{{A_NS}}}blip")
        rid = blip.get(f"{{{R_NS}}}embed") if blip is not None else None
        media_target = rid_to_target.get(rid) if rid else None
        media_path = f"word/{media_target}" if media_target else None

        captures.append(Capture(
            index=i,
            page_title=page_title,
            url=url,
            sha256=sha256,
            capture_date_raw=cap_raw,
            updated_date_raw=upd_raw,
            image_rid=rid,
            image_media_path=media_path,
        ))

    return ParsedDocx(
        captures=captures,
        doc_xml=doc_xml,
        rels_xml=rels_xml,
        zip_bytes=zip_bytes,
    )
