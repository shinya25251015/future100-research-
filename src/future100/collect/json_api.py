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
from datetime import timedelta
from typing import Any

from .. import timeutil
from . import base

COLLECTOR = "json_api@1"
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@base.register("json_api")
def collect_json(source: dict) -> Iterator[dict]:
    url = source.get("endpoint") or source["url"]
    mapping = source.get("mapping") or {}
    payload = base.http_get(
        url,
        timeout=source.get("timeout", base.DEFAULT_TIMEOUT),
        user_agent=base.resolve_user_agent(source),
        body=_request_body(source),
    )
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise base.FetchError(source["source_id"], f"invalid JSON: {exc}") from exc

    items = _dig(document, mapping.get("items_path")) if mapping.get("items_path") else document
    if not isinstance(items, list):
        raise base.FetchError(source["source_id"], f"items_path did not resolve to a list: {mapping.get('items_path')!r}")

    observed_at = timeutil.now_str()
    for item in items:
        link = _link(item, mapping)
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


def _request_body(source: dict) -> bytes | None:
    """POST 用の JSON ボディ。日付テンプレートは取得時刻を基準に置換する。

    テンプレートを使うのは、検索 API が対象期間の明示を要求するため。
    ここで埋めるのは「いま取りに行く期間」であって、観測時刻 (observed_at) とは別物である。
    """
    if source.get("http_method", "GET").upper() != "POST":
        return None
    body = source.get("body")
    if body is None:
        raise base.FetchError(source["source_id"], "http_method=POST requires a body")

    # 過去分の取り込みは対象期間を明示して渡す（遡及日数では任意の過去区間を指せない）
    date_range = source.get("date_range")
    if date_range:
        rendered = json.dumps(body, ensure_ascii=False)
        rendered = rendered.replace("{{start_date}}", date_range["start"]).replace("{{end_date}}", date_range["end"])
        return rendered.encode("utf-8")

    end = timeutil.now().date()
    start = end - timedelta(days=source.get("lookback_days", 7))
    rendered = json.dumps(body, ensure_ascii=False)
    rendered = rendered.replace("{{start_date}}", f"{start:%Y-%m-%d}").replace("{{end_date}}", f"{end:%Y-%m-%d}")
    return rendered.encode("utf-8")


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


def _link(item: dict, mapping: dict) -> str:
    """項目の URL。URL を返さない API 用に、ID から組み立てるテンプレートも受ける。

    URL は raw_id の素になるため、同じ対象が常に同じ文字列になることが重要
    （そうでないと再取得のたびに新しい観測として増える）。
    """
    template = mapping.get("url_template")
    if template:
        try:
            return template.format(**item)
        except (KeyError, IndexError):
            return ""
    return _first_str(item, mapping.get("url", "url"))


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
    """公開時刻。

    タイムゾーンを明示しない API が多いため、規約を固定する。
      - 日付のみ ("2026-08-11")            → 00:00:00Z を補う（day 精度）
      - タイムゾーン無しの日時             → 日付部分だけを採り 00:00:00Z（時刻を勝手に UTC と見なさない）
      - タイムゾーン付き                   → そのまま UTC に変換する
    推測した時刻を書かないことを優先する (§36)。
    """
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
        pass
    if _DATE_ONLY.match(text[:10]):
        return f"{text[:10]}T00:00:00Z"
    return None
