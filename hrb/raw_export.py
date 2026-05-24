"""
Index Hunchly's raw case .zip so we can look up the MHTML for any capture.

The bridge from docx capture → MHTML is the SHA-256 hash:
    - docx Row 8 = capture hash
    - case_data/pages.csv 'Page Hash' column = same hash
    - case_data/pages.csv 'Page ID' column → pages/<id>.mhtml in the zip
"""
from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass
from pathlib import Path


@dataclass
class PageEntry:
    page_id: str
    url: str
    page_title: str
    sha256: str
    timestamp_created: str
    mhtml_path: str                  # e.g. "pages/14.mhtml"


class RawExport:
    """Lazy reader over a Hunchly raw case .zip."""

    def __init__(self, zip_path: str | Path):
        self.zip_path = Path(zip_path)
        self._zip = zipfile.ZipFile(self.zip_path)
        self._by_hash: dict[str, PageEntry] = {}
        self._load_index()

    def _load_index(self) -> None:
        try:
            raw = self._zip.read("case_data/pages.csv").decode("utf-8")
        except KeyError as e:
            raise ValueError(
                f"{self.zip_path}: missing case_data/pages.csv — is this a Hunchly raw case zip?"
            ) from e
        names = set(self._zip.namelist())
        reader = csv.DictReader(io.StringIO(raw))
        for row in reader:
            page_id = (row.get("Page ID") or "").strip()
            sha = (row.get("Page Hash") or "").strip()
            url = (row.get("Page URL") or "").strip()
            if not page_id or not sha:
                continue
            mhtml_path = f"pages/{page_id}.mhtml"
            if mhtml_path not in names:
                continue
            self._by_hash[sha] = PageEntry(
                page_id=page_id,
                url=url,
                page_title=(row.get("Page Title") or "").strip(),
                sha256=sha,
                timestamp_created=(row.get("Timestamp Created") or "").strip(),
                mhtml_path=mhtml_path,
            )

    def lookup(self, sha256: str) -> PageEntry | None:
        return self._by_hash.get(sha256)

    def read_mhtml(self, sha256: str) -> bytes | None:
        entry = self.lookup(sha256)
        if entry is None:
            return None
        return self._zip.read(entry.mhtml_path)

    def close(self) -> None:
        self._zip.close()

    def __enter__(self) -> "RawExport":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
