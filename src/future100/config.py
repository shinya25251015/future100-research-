"""config/ の読み込み (§11, §48-51)。

設定は JSON で持つ（追加依存なしで読めることを優先）。
ソース定義は schemas/source.schema.json に準拠する。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from . import storage

SOURCES_FILE = storage.CONFIG_DIR / "sources.json"
SECTORS_FILE = storage.CONFIG_DIR / "known_sectors.json"
SIGNAL_RULES_FILE = storage.CONFIG_DIR / "signal_rules.json"


def load_sources(*, enabled_only: bool = True, tiers: set[str] | None = None) -> list[dict]:
    payload = storage.read_json(SOURCES_FILE)
    sources = payload["sources"]
    if enabled_only:
        sources = [s for s in sources if s.get("enabled", True)]
    if tiers:
        sources = [s for s in sources if s["tier"] in tiers]
    return sources


def get_source(source_id: str) -> dict:
    for source in load_sources(enabled_only=False):
        if source["source_id"] == source_id:
            return source
    raise KeyError(f"unknown source_id: {source_id}")


@lru_cache(maxsize=1)
def load_known_sectors() -> tuple[dict, ...]:
    """§11 常時監視セクター。ここに無いものが §12 の Emerging Sector 候補になる。"""
    return tuple(storage.read_json(SECTORS_FILE)["sectors"])


@lru_cache(maxsize=1)
def load_signal_rules() -> tuple[dict, ...]:
    """§10 弱いシグナルの種別を判定するキーワード規則。"""
    if not Path(SIGNAL_RULES_FILE).exists():
        return ()
    return tuple(storage.read_json(SIGNAL_RULES_FILE)["rules"])


def reload() -> None:
    """設定ファイルを編集した直後にキャッシュを捨てる（テスト・長時間プロセス用）。"""
    load_known_sectors.cache_clear()
    load_signal_rules.cache_clear()
