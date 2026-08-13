"""コレクタ共通基盤 (Phase 2)。

コレクタの責務は「取得して RawDocument を作る」ことだけに限定する。
解釈・分類・因果推論は後段（normalize / 分析）で行い、raw には手を入れない (§48-51)。
"""
from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Iterator

from .. import ids, textnorm, timeutil

USER_AGENT = "future100-research/0.1 (+research use; contact: repository owner)"
DEFAULT_TIMEOUT = 30


class FetchError(Exception):
    """取得失敗。1 ソースの失敗で日次収集全体を止めないため、呼び出し側で握る。"""

    def __init__(self, origin: str, reason: str) -> None:
        super().__init__(f"{origin}: {reason}")
        self.origin = origin
        self.reason = reason


@dataclass
class CollectResult:
    source_id: str
    documents: list[dict] = field(default_factory=list)
    error: str | None = None


def http_get(url: str, *, timeout: int = DEFAULT_TIMEOUT) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise FetchError(url, f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise FetchError(url, f"URL error: {exc.reason}") from exc
    except TimeoutError as exc:
        raise FetchError(url, "timeout") from exc


def make_raw_document(
    *,
    source: dict,
    url: str,
    title: str,
    body: str,
    published_at: str | None,
    method: str,
    collector: str,
    observed_at: str | None = None,
) -> dict:
    """RawDocument を組み立てる。observed_at は取得時刻であり、published_at と混同しない (§37)。"""
    canonical = textnorm.canonical_url(url)
    return {
        "raw_id": ids.raw_id(canonical),
        "source_id": source["source_id"],
        "observed_at": observed_at or timeutil.now_str(),
        **({"published_at": published_at} if published_at else {}),
        "fetch": {
            "url": url,
            "canonical_url": canonical,
            "method": method,
            "collector": collector,
        },
        "content": {
            "title": title.strip(),
            "body": body.strip(),
            **({"language": source["language"]} if source.get("language") else {}),
            "content_hash": textnorm.content_hash(title, body),
        },
        "processing": {"status": "pending", "event_ids": []},
    }


Collector = Callable[[dict], Iterator[dict]]
_REGISTRY: dict[str, Collector] = {}


def register(kind: str) -> Callable[[Collector], Collector]:
    def decorator(func: Collector) -> Collector:
        _REGISTRY[kind] = func
        return func

    return decorator


def get_collector(kind: str) -> Collector:
    if kind not in _REGISTRY:
        raise KeyError(f"no collector registered for kind={kind!r}; available: {sorted(_REGISTRY)}")
    return _REGISTRY[kind]


def collect(source: dict) -> CollectResult:
    """1 ソースを取得する。失敗は例外にせず CollectResult.error に載せて呼び出し側に返す。"""
    try:
        collector = get_collector(source["kind"])
        return CollectResult(source["source_id"], list(collector(source)))
    except FetchError as exc:
        return CollectResult(source["source_id"], [], error=exc.reason)
    except KeyError as exc:
        return CollectResult(source["source_id"], [], error=str(exc))
    except Exception as exc:  # コレクタ 1 本の失敗で日次収集全体を止めない
        return CollectResult(source["source_id"], [], error=f"{type(exc).__name__}: {exc}")
