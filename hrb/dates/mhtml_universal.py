"""
Generic MHTML date extractor: 4-method cascade.

Returns the first hit from:
    1. <time datetime="...">
    2. JSON-LD datePublished / uploadDate / dateCreated / datePosted
    3. <meta property="article:published_time" | "og:updated_time" | "video:release_date">
    4. itemprop="datePublished"
"""
from __future__ import annotations

import email
import json
from email import policy
from datetime import datetime

from bs4 import BeautifulSoup
from dateutil import parser as date_parser


def _load_html_from_mhtml(mhtml_bytes: bytes) -> str:
    msg = email.message_from_bytes(mhtml_bytes, policy=policy.default)
    html_parts: list[str] = []
    for part in msg.walk():
        if part.get_content_type() != "text/html":
            continue
        try:
            content = part.get_content()
        except Exception:
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            content = payload.decode("utf-8", errors="replace")
        html_parts.append(content)
    return max(html_parts, key=len) if html_parts else ""


def extract_from_html(html_text: str) -> tuple[datetime | None, str | None]:
    if not html_text:
        return None, None
    soup = BeautifulSoup(html_text, "html.parser")

    for t in soup.find_all("time"):
        dt_attr = t.get("datetime")
        if dt_attr:
            try:
                return date_parser.isoparse(dt_attr), "time_element"
            except (ValueError, TypeError):
                pass

    for s in soup.find_all("script", type="application/ld+json"):
        if not s.string:
            continue
        try:
            data = json.loads(s.string)
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            for k in ("datePublished", "uploadDate", "dateCreated", "datePosted"):
                if k in item and isinstance(item[k], str):
                    try:
                        return date_parser.isoparse(item[k]), f"jsonld_{k}"
                    except (ValueError, TypeError):
                        pass

    for prop in ("article:published_time", "og:updated_time", "video:release_date"):
        m = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
        if m and m.get("content"):
            try:
                return date_parser.isoparse(m["content"]), f"meta_{prop}"
            except (ValueError, TypeError):
                pass

    el = soup.find(attrs={"itemprop": "datePublished"})
    if el:
        v = el.get("content") or el.get("datetime") or el.get_text(strip=True)
        if v:
            try:
                return date_parser.isoparse(v), "itemprop"
            except (ValueError, TypeError):
                pass

    return None, None


def extract_from_bytes(mhtml_bytes: bytes) -> tuple[datetime | None, str | None]:
    return extract_from_html(_load_html_from_mhtml(mhtml_bytes))
