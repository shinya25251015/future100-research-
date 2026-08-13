"""RSS 2.0 / Atom コレクタ（標準ライブラリのみ）。

政府機関・中央銀行・IR・専門メディアの多くがフィードを提供しており、
Phase 2 の最初の情報経路としてはこれで十分な範囲を覆える。
"""
from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from email.utils import parsedate_to_datetime

from .. import timeutil
from . import base

COLLECTOR = "rss@1"
_TAG = re.compile(r"<[^>]+>")
_NS = {"atom": "http://www.w3.org/2005/Atom", "dc": "http://purl.org/dc/elements/1.1/"}


@base.register("rss")
@base.register("atom")
def collect_feed(source: dict) -> Iterator[dict]:
    url = source.get("endpoint") or source["url"]
    payload = base.http_get(url, timeout=source.get("timeout", base.DEFAULT_TIMEOUT))
    if payload.lstrip()[:200].lower().startswith((b"<!doctype html", b"<html")):
        # bot 対策ページやリダイレクト先の HTML を「壊れたフィード」と誤報告しない
        raise base.FetchError(source["source_id"], "received HTML instead of a feed (bot challenge or moved endpoint)")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise base.FetchError(source["source_id"], f"feed parse error: {exc}") from exc

    observed_at = timeutil.now_str()
    entries = root.findall(".//item") or root.findall(".//atom:entry", _NS)
    if not entries:
        raise base.FetchError(source["source_id"], "feed contained no items")

    for entry in entries:
        parsed = _parse_entry(entry)
        if parsed is None:
            continue
        link, title, body, published_at = parsed
        yield base.make_raw_document(
            source=source,
            url=link,
            title=title,
            body=body,
            published_at=published_at,
            method=source["kind"],
            collector=COLLECTOR,
            observed_at=observed_at,
        )


def _parse_entry(entry: ET.Element) -> tuple[str, str, str, str | None] | None:
    link = _link(entry)
    title = _text(entry, "title") or _text(entry, "atom:title")
    if not link or not title:
        return None
    body = (
        _text(entry, "description")
        or _text(entry, "atom:summary")
        or _text(entry, "atom:content")
        or _text(entry, "{http://purl.org/rss/1.0/modules/content/}encoded")
        or ""
    )
    return link, title, body, _published(entry)


def _text(entry: ET.Element, tag: str) -> str:
    node = entry.find(tag, _NS) if ":" in tag else entry.find(tag)
    if node is None or node.text is None:
        return ""
    return _strip_markup(node.text)


def _strip_markup(text: str) -> str:
    return html.unescape(_TAG.sub(" ", text)).strip()


def _link(entry: ET.Element) -> str:
    node = entry.find("link")
    if node is not None and node.text and node.text.strip():
        return node.text.strip()
    for node in entry.findall("atom:link", _NS):
        rel = node.get("rel", "alternate")
        if rel == "alternate" and node.get("href"):
            return node.get("href", "").strip()
    guid = entry.find("guid")
    if guid is not None and guid.text and guid.text.startswith("http"):
        return guid.text.strip()
    return ""


def _published(entry: ET.Element) -> str | None:
    """フィード上の公開時刻。読めない場合は推測せず None を返す（偽の時刻を作らない §36）。"""
    for tag in ("pubDate", "atom:published", "atom:updated", "dc:date"):
        value = entry.find(tag, _NS) if ":" in tag else entry.find(tag)
        if value is None or not value.text:
            continue
        text = value.text.strip()
        try:
            return timeutil.fmt(parsedate_to_datetime(text))
        except (TypeError, ValueError):
            pass
        try:
            return timeutil.fmt(timeutil.parse(text))
        except ValueError:
            continue
    return None
