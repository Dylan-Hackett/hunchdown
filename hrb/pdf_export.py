"""
docx -> pdf via docx2pdf. Requires Microsoft Word installed (Mac or Windows).

On systems without Word, this raises a clean error and the rest of the run
(docx outputs, manifest, REVIEW_REQUIRED) is unaffected — the caller can
catch ConverterUnavailable and continue.
"""
from __future__ import annotations

from pathlib import Path


class ConverterUnavailable(RuntimeError):
    """Raised when docx2pdf can't find a Word installation to drive."""


def export(docx_paths: list[Path]) -> dict[Path, Path]:
    """
    Convert each docx in-place to a sibling .pdf. Returns {docx_path: pdf_path}.

    Skips quietly if `docx_paths` is empty.
    """
    if not docx_paths:
        return {}

    try:
        from docx2pdf import convert
    except ImportError as e:
        raise ConverterUnavailable("docx2pdf not installed") from e

    out: dict[Path, Path] = {}
    for docx_path in docx_paths:
        pdf_path = docx_path.with_suffix(".pdf")
        try:
            convert(str(docx_path), str(pdf_path))
        except Exception as e:
            raise ConverterUnavailable(f"docx2pdf failed on {docx_path}: {e}") from e
        out[docx_path] = pdf_path
    return out
