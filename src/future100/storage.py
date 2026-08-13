"""data/ 配下の永続化規約 (§48-51)。

  data/raw/YYYY-MM-DD/<source_id>.jsonl.gz        取得スナップショット（追記のみ・不変）
  data/events/YYYY-MM-DD.jsonl                    正規化イベント（observed_at の日付で分割）
  data/signals/<window_id>.json                   Early Signal 集計
  data/sectors/<sector_id>.json                   セクタープロファイル
  data/index/*.json                               重複判定・進捗などの派生インデックス（再生成可能）

raw は「一度書いたら書き換えない」。再取得しても raw_id が同じなら既存を残し、
差分は新しい観測として events 側で表現する。可変な状態（正規化済みかどうか等）は
raw に持たせず data/index/ に置く。
"""
from __future__ import annotations

import gzip
import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from . import timeutil

ROOT = Path(os.environ.get("FUTURE100_ROOT", Path(__file__).resolve().parents[2]))
DATA = ROOT / "data"
RAW_DIR = DATA / "raw"
EVENTS_DIR = DATA / "events"
SIGNALS_DIR = DATA / "signals"
SECTORS_DIR = DATA / "sectors"
INDEX_DIR = DATA / "index"
REPORTS_DIR = ROOT / "reports"
CONFIG_DIR = ROOT / "config"
SCHEMAS_DIR = ROOT / "schemas"


def write_json(path: Path, payload: Any) -> None:
    """同一ディレクトリの一時ファイル経由で原子的に書き込む（途中終了で壊れたJSONを残さない）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


# --- raw ------------------------------------------------------------------
#
# raw は「日 × ソース」単位の gzip 圧縮 JSONL アーカイブに追記する。
#
#   data/raw/2026-08-13/src_arxiv_cs_ai.jsonl.gz
#
# 1 文書 1 ファイルにすると年 20 万ファイルに達し、リポジトリに観測履歴を
# 残せなくなる。観測履歴が残らなければ baseline が貯まらず、Early Signal
# Detection は永久に判定を保留し続ける (§10)。
#
# 追記のみで、書いたレコードは決して書き換えない。正規化の進捗などの可変な状態は
# data/index/ 側に置き、raw を不変に保つ (§48-51)。

RAW_SUFFIX = ".jsonl.gz"
_RAW_INDEX = "raw_ids"


def raw_archive_path(source_id: str, observed_at: str) -> Path:
    return RAW_DIR / f"{timeutil.day_of(observed_at):%Y-%m-%d}" / f"{source_id}{RAW_SUFFIX}"


def load_raw_index() -> dict[str, str]:
    """raw_id → "YYYY-MM-DD/source_id"。取得済み判定を O(1) にするための索引。

    アーカイブから再生成できる派生データなので、壊れても rebuild_raw_index() で作り直せる。
    """
    return load_partitioned_index(_RAW_INDEX)


def rebuild_raw_index() -> dict[str, str]:
    partitions: dict[str, dict[str, str]] = {}
    for path in sorted(RAW_DIR.rglob(f"*{RAW_SUFFIX}")):
        day = path.parent.name
        key = f"{day}/{path.name[: -len(RAW_SUFFIX)]}"
        for document in _read_archive(path):
            partitions.setdefault(day, {})[document["raw_id"]] = key
    clear_partitioned_index(_RAW_INDEX)
    merged: dict[str, str] = {}
    for day, mapping in partitions.items():
        save_index_partition(_RAW_INDEX, day, mapping)
        merged.update(mapping)
    return merged


def save_raw_batch(documents: list[dict]) -> int:
    """未取得の文書だけをアーカイブに追記し、追記件数を返す。

    同一 raw_id を二度書かない（同じ URL の再取得は既存の観測をそのまま残す）。
    """
    if not documents:
        return 0
    index = load_raw_index()
    grouped: dict[Path, list[dict]] = {}
    added_by_day: dict[str, dict[str, str]] = {}
    seen: set[str] = set()

    for document in documents:
        raw_id = document["raw_id"]
        if raw_id in index or raw_id in seen:
            continue
        seen.add(raw_id)
        path = raw_archive_path(document["source_id"], document["observed_at"])
        grouped.setdefault(path, []).append(document)
        day = path.parent.name
        added_by_day.setdefault(day, {})[raw_id] = f"{day}/{path.name[: -len(RAW_SUFFIX)]}"

    for path, batch in grouped.items():
        _append_archive(path, batch)
    for day, mapping in added_by_day.items():
        merged = load_index_partition(_RAW_INDEX, day)
        merged.update(mapping)
        save_index_partition(_RAW_INDEX, day, merged)
    return len(seen)


def iter_raw(as_of: str | None = None, *, days: list[str] | None = None) -> Iterator[dict]:
    """raw を observed_at 昇順で読み出す。as_of 指定時は未来の観測を除外する (§37)。"""
    documents: list[dict] = []
    for path in sorted(RAW_DIR.rglob(f"*{RAW_SUFFIX}")):
        if days is not None and path.parent.name not in days:
            continue
        for document in _read_archive(path):
            if as_of and not timeutil.is_visible(document["observed_at"], as_of):
                continue
            documents.append(document)
    documents.sort(key=lambda d: (d["observed_at"], d["raw_id"]))
    yield from documents


def _append_archive(path: Path, documents: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # gzip の連結ストリームは 1 つの gzip ファイルとして読めるため、追記で済む
    with gzip.open(path, "at", encoding="utf-8") as fh:
        for document in documents:
            fh.write(json.dumps(document, ensure_ascii=False) + "\n")


def _read_archive(path: Path) -> Iterator[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


# --- 正規化の進捗 (raw を書き換えずに持つ) ---------------------------------

_NORMALIZED_INDEX = "normalized"


def load_normalized_index() -> dict[str, dict]:
    """raw_id → {event_ids, normalized_at}。raw を不変に保つため状態は外に置く。"""
    return load_partitioned_index(_NORMALIZED_INDEX)


def save_normalized_partition(day: str, mapping: dict[str, dict]) -> None:
    save_index_partition(_NORMALIZED_INDEX, day, mapping)


def load_normalized_partition(day: str) -> dict[str, dict]:
    return load_index_partition(_NORMALIZED_INDEX, day)


def clear_normalized_index() -> None:
    clear_partitioned_index(_NORMALIZED_INDEX)


# --- events ---------------------------------------------------------------

def events_path(observed_at: str) -> Path:
    return EVENTS_DIR / f"{timeutil.day_of(observed_at):%Y-%m-%d}.jsonl"


def append_event(event: dict) -> Path:
    path = events_path(event["observed_at"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    return path


def iter_events(as_of: str | None = None, *, primary_only: bool = False) -> Iterator[dict]:
    """イベントを observed_at 昇順で読み出す。

    as_of を渡した場合、その時点で観測済みのイベントのみを返す。
    分析コードは必ず as_of を渡すこと (§37)。
    """
    for path in sorted(EVENTS_DIR.glob("*.jsonl")):
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                event = json.loads(line)
                if as_of and not timeutil.is_visible(event["observed_at"], as_of):
                    continue
                if primary_only and not event.get("is_cluster_primary", True):
                    continue
                yield event


# --- sectors / signals ----------------------------------------------------

def sector_path(sector_id: str) -> Path:
    return SECTORS_DIR / f"{sector_id}.json"


def save_sector(profile: dict) -> Path:
    path = sector_path(profile["sector_id"])
    write_json(path, profile)
    return path


def load_sector(sector_id: str) -> dict:
    return read_json(sector_path(sector_id))


def save_signal_window(window: dict) -> Path:
    path = SIGNALS_DIR / f"{window['window_id']}.json"
    write_json(path, window)
    return path


# --- index ----------------------------------------------------------------

def load_index(name: str, default: Any = None) -> Any:
    path = INDEX_DIR / f"{name}.json"
    if not path.exists():
        return {} if default is None else default
    return read_json(path)


def save_index(name: str, payload: Any) -> Path:
    path = INDEX_DIR / f"{name}.json"
    write_json(path, payload)
    return path


# 索引は日付で分割する。1 枚の大きな JSON を毎日書き換えると、
# 変更のない過去ぶんまで含めて git に新しい blob が積まれ、観測履歴を残せなくなる。
# 判定がすべて発生日で区切られているため、日付ごとに閉じた索引にできる。

def index_partition_path(name: str, day: str) -> Path:
    return INDEX_DIR / name / f"{day}.json"


def load_index_partition(name: str, day: str) -> dict:
    path = index_partition_path(name, day)
    return read_json(path) if path.exists() else {}


def save_index_partition(name: str, day: str, payload: dict) -> Path:
    path = index_partition_path(name, day)
    write_json(path, payload)
    return path


def index_partition_days(name: str) -> list[str]:
    directory = INDEX_DIR / name
    if not directory.is_dir():
        return []
    return sorted(p.stem for p in directory.glob("*.json"))


def load_partitioned_index(name: str) -> dict:
    merged: dict = {}
    for day in index_partition_days(name):
        merged.update(load_index_partition(name, day))
    return merged


def clear_partitioned_index(name: str) -> None:
    directory = INDEX_DIR / name
    if not directory.is_dir():
        return
    for path in directory.glob("*.json"):
        path.unlink()
