"""JSON API コレクタ（標準ライブラリのみ）。

RSS を出していない一次情報源（Federal Register API など）を、
ソース定義の `mapping` だけで取り込めるようにする汎用コレクタ。

  "mapping": {
    "items_path": "results",              項目配列へのドット区切りパス（省略時はトップレベルが配列）
    "url": "html_url",
    "title": "title",
    "body": ["abstract", "action"],       複数指定すると連結する
    "published_at": "publication_date"    日付のみの場合は 00:00:00Z を補って day 精度として扱う
  }
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

from .. import timeutil
from . import base

COLLECTOR = "json_api@1"
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@base.register("json_api")
def collect_json(source: dict) -> Iterator[dict]:
    url = source.get("endpoint") or source["url"]
    mapping = source.get("mapping") or {}
    payload = base.http_get(url, timeout=source.get("timeout", base.DEFAULT_TIMEOUT))
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise base.FetchError(source["source_id"], f"invalid JSON: {exc}") from exc

    items = _dig(document, mapping.get("items_path")) if mapping.get("items_path") else document
    if not isinstance(items, list):
        raise base.FetchError(source["source_id"], f"items_path did not resolve to a list: {mapping.get('items_path')!r}")

    observed_at = timeutil.now_str()
    for item in items:
        link = _first_str(item, mapping.get("url", "url"))
        title = _first_str(item, mapping.get("title", "title"))
        if not link or not title:
            continue
        yield base.make_raw_document(
            source=source,
            url=link,
            title=title,
            body=_join(item, mapping.get("body", [])),
            published_at=_published(item, mapping.get("published_at")),
            method="json_api",
            collector=COLLECTOR,
            observed_at=observed_at,
        )


def _dig(payload: Any, path: str | None) -> Any:
    if not path:
        return payload
    current = payload
    for key in path.split("."):
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return None
    return current


def _first_str(item: dict, paths: str | list[str]) -> str:
    for path in [paths] if isinstance(paths, str) else paths:
        value = _dig(item, path)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _join(item: dict, paths: str | list[str]) -> str:
    values = []
    for path in [paths] if isinstance(paths, str) else paths:
        value = _dig(item, path)
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
    return "\n\n".join(values)


def _published(item: dict, path: str | None) -> str | None:
    """公開時刻。日付のみの API では 00:00:00Z を補う（時刻不明を day 精度として扱う規約）。"""
    if not path:
        return None
    value = _dig(item, path)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if _DATE_ONLY.match(text):
        return f"{text}T00:00:00Z"
    try:
        return timeutil.fmt(timeutil.parse(text))
    except ValueError:
        return None
