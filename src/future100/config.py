"""config/ の読み込み (§11, §48-51)。

設定は JSON で持つ（追加依存なしで読めることを優先）。
ソース定義は schemas/source.schema.json に準拠する。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from . import storage, timeutil

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


def is_due(source: dict, last_poll: str | None, *, now: str | None = None) -> bool:
    """このソースを今回取りに行くか。

    poll_interval_minutes は「少なくともこの頻度で」の意味に取る。1 日以上の間隔は
    経過時間ではなく日付の差で数える。日次ジョブの起動時刻は数十分から数時間ずれ、
    手動実行が挟まることもあるため、経過時間で締め切ると「23 時間しか経っていない」
    という理由で日次ソースを丸ごと落とす日が出る。落とした日は観測履歴に穴が開き、
    baseline の充足がその日数ぶん後ろにずれる (§10)。

    1 日未満の間隔は日次サイクルでは常に満たされるため、毎回取りに行く。
    """
    if not last_poll:
        return True
    interval_days = source.get("poll_interval_minutes", 1440) // 1440
    if interval_days < 1:
        return True
    elapsed_days = (timeutil.day_of(now or timeutil.now_str()) - timeutil.day_of(last_poll)).days
    return elapsed_days >= interval_days


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
