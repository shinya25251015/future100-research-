"""コレクタ共通基盤 (Phase 2)。

コレクタの責務は「取得して RawDocument を作る」ことだけに限定する。
解釈・分類・因果推論は後段（normalize / 分析）で行い、raw には手を入れない (§48-51)。
"""
from __future__ import annotations

import os
import re
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


_ENV_REF = re.compile(r"\$\{([A-Z0-9_]+)\}")


def expand_env(template: str, *, origin: str, purpose: str) -> str:
    """文字列中の ${ENV_VAR} を環境変数で埋める。未設定なら対処法を添えて失敗させる。

    連絡先アドレスや API キーをリポジトリに置くわけにはいかない。かといって
    偽の値を埋めて 403 を「取得失敗」として片付けると、設定漏れが恒久的に隠れる。
    未設定であることをそのまま失敗理由として表に出す。
    """
    missing: list[str] = []

    def substitute(match: re.Match[str]) -> str:
        value = os.environ.get(match.group(1), "")
        if not value:
            missing.append(match.group(1))
        return value

    resolved = _ENV_REF.sub(substitute, template)
    if missing:
        raise FetchError(
            origin,
            f"環境変数 {', '.join(missing)} が未設定です（{purpose}）。"
            f"例: export {missing[0]}=...",
        )
    return resolved


def resolve_user_agent(source: dict) -> str | None:
    """ソース定義の User-Agent 内の ${ENV_VAR} を環境変数で埋める。

    SEC EDGAR のように「連絡先アドレスを名乗ること」を利用条件にしている提供元がある。
    """
    template = source.get("user_agent")
    if not template:
        return None
    return expand_env(
        template, origin=source["source_id"], purpose="このソースは User-Agent に連絡先を要求します"
    )


def http_get(
    url: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    user_agent: str | None = None,
    body: bytes | None = None,
) -> bytes:
    """GET（body を渡した場合は JSON の POST）。

    User-Agent を差し替えられるようにしてあるのは、SEC EDGAR のように
    連絡先入りの UA を要求し、既定 UA では 403 を返す提供元があるため。
    """
    headers = {"User-Agent": user_agent or USER_AGENT, "Accept": "*/*"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, headers=headers, data=body)
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
    identity: str | None = None,
    quantities: list[dict] | None = None,
) -> dict:
    """RawDocument を組み立てる。observed_at は取得時刻であり、published_at と混同しない (§37)。

    identity は「同じ観測かどうか」を決める文字列で、既定は canonical_url。
    統計のように同じ URL の値が改訂されうるものは、値まで含めた identity を渡して
    改訂を別の観測として記録する（既定のままだと改訂が既取得として捨てられる）。
    """
    canonical = textnorm.canonical_url(url)
    return {
        "raw_id": ids.raw_id(identity or canonical),
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
            **({"quantities": quantities} if quantities else {}),
        },
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
