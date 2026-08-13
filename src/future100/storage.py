"""data/ 配下の永続化規約 (§48-51)。

  data/raw/YYYY/MM/DD/<source_id>/<raw_id>.json   取得スナップショット（不変）
  data/events/YYYY-MM-DD.jsonl                    正規化イベント（observed_at の日付で分割）
  data/signals/<window_id>.json                   Early Signal 集計
  data/sectors/<sector_id>.json                   セクタープロファイル
  data/index/*.json                               重複判定などの派生インデックス（再生成可能）

raw は「一度書いたら書き換えない」。再取得しても raw_id が同じなら既存を残し、
差分は新しい観測として events 側で表現する。
"""
from __future__ import annotations

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

def raw_path(source_id: str, raw_id: str, observed_at: str) -> Path:
    day = timeutil.day_of(observed_at)
    return RAW_DIR / f"{day:%Y/%m/%d}" / source_id / f"{raw_id}.json"


def raw_exists(source_id: str, raw_id: str, observed_at: str) -> bool:
    if raw_path(source_id, raw_id, observed_at).exists():
        return True
    # 過去日に取得済みの可能性があるため raw_id で全期間を探す
    return next(RAW_DIR.rglob(f"{raw_id}.json"), None) is not None


def save_raw(document: dict) -> Path | None:
    """未取得なら保存してパスを返す。既に同じ raw_id があれば None を返す（上書きしない）。"""
    source_id = document["source_id"]
    raw_id = document["raw_id"]
    observed_at = document["observed_at"]
    if raw_exists(source_id, raw_id, observed_at):
        return None
    path = raw_path(source_id, raw_id, observed_at)
    write_json(path, document)
    return path


def iter_raw(as_of: str | None = None) -> Iterator[dict]:
    """raw を observed_at 昇順で読み出す。as_of 指定時は未来の観測を除外する (§37)。"""
    docs = []
    for path in sorted(RAW_DIR.rglob("raw_*.json")):
        doc = read_json(path)
        if as_of and not timeutil.is_visible(doc["observed_at"], as_of):
            continue
        docs.append(doc)
    docs.sort(key=lambda d: d["observed_at"])
    yield from docs


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
