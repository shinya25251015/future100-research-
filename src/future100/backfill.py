"""過去分の取り込み (§10, §37)。

日次収集は「今日から先」しか積み上がらない。一方で、日付範囲を指定できる API は
過去の同じデータを今からでも取りに行ける。取れるものを先に取っておけば、
Early Signal Detection の baseline を待たずに作れる。

守る規律:

  - **observed_at は今。** 過去分を取り込んでも「そのとき見ていた」ことにはしない。
    観測時刻を発行日に書き換えると、過去時点の再現 (§37) が壊れる。
    結果として、今日より前の as_of で再生したときこの過去分は見えない。これが正しい。
  - **event_at は発行日。** 出来事が起きた日は文書のとおりに入るので、
    baseline の件数は正しい期間に落ちる。
  - **取り込めた範囲を記録する。** どの期間を実際に取り込めたかを data/index/backfill.json
    に残し、シグナル判定の観測被覆に反映する。「取り込んだつもり」で baseline を
    有効化しないため、記録は設定ではなく実行結果から作る。

過去分を取れるのは日付範囲を受け付けるソースだけで、RSS しか出していないソース
（企業広報・中央銀行の発表ページなど）は原理的に遡れない。遡れないものは遡れないまま
記録され、そのソースが寄与する topic の判定は従来どおり保留される。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import storage, timeutil
from .collect import base

INDEX_NAME = "backfill"
DEFAULT_DELAY_SECONDS = 3.0


@dataclass
class ChunkResult:
    start: str
    end: str
    fetched: int = 0
    stored: int = 0
    error: str | None = None


@dataclass
class BackfillResult:
    source_id: str
    chunks: list[ChunkResult] = field(default_factory=list)

    @property
    def stored(self) -> int:
        return sum(chunk.stored for chunk in self.chunks)

    @property
    def fetched(self) -> int:
        return sum(chunk.fetched for chunk in self.chunks)

    def covered_span(self) -> tuple[str, str] | None:
        """実際に取り込めた連続範囲。

        途中で失敗した chunk があるなら、そこで途切れたものとして扱う。穴の空いた期間を
        「取り込み済み」と記録すると、過小な baseline で増加を判定してしまう。
        """
        ok = []
        for chunk in sorted(self.chunks, key=lambda c: c.start):
            if chunk.error:
                break
            ok.append(chunk)
        if not ok:
            return None
        return ok[0].start, ok[-1].end


def supported(source: dict) -> bool:
    """過去分を実際に取り分けられるか。

    期間を指定する手段（URL テンプレート、または期間を本文に書く POST）が無いのに
    backfill を設定してあるものは対象外にする。同じ最新フィードを区間の数だけ取り直して
    「過去分を取り込んだ」と記録すると、実際には空の baseline で増加を判定してしまう。
    """
    spec = source.get("backfill")
    if not spec:
        return False
    return bool(spec.get("endpoint_template")) or source.get("http_method", "GET").upper() == "POST"


def chunks(*, days: int, chunk_days: int, until: date) -> list[tuple[date, date]]:
    """古い順に [start, end] の区間を作る。until は含む。"""
    if days < 1 or chunk_days < 1:
        raise ValueError("days と chunk_days は 1 以上")
    first = until - timedelta(days=days - 1)
    spans: list[tuple[date, date]] = []
    cursor = first
    while cursor <= until:
        end = min(cursor + timedelta(days=chunk_days - 1), until)
        spans.append((cursor, end))
        cursor = end + timedelta(days=1)
    return spans


def _chunk_source(source: dict, start: date, end: date) -> dict:
    """1 区間ぶんの取得に使う一時的なソース定義。

    登録簿の定義そのものは書き換えない。kind と endpoint だけを差し替えた写しを渡す。
    """
    spec = source["backfill"]
    fields = {"start_date": f"{start:%Y-%m-%d}", "end_date": f"{end:%Y-%m-%d}",
              "start_compact": f"{start:%Y%m%d}", "end_compact": f"{end:%Y%m%d}"}
    chunk = dict(source)
    chunk["kind"] = spec.get("kind", source["kind"])
    if spec.get("endpoint_template"):
        chunk["endpoint"] = spec["endpoint_template"].format(**fields)
    # POST 本文を使うソース（検索 API）は、遡及日数ではなく明示した範囲で取りに行く
    chunk["date_range"] = {"start": fields["start_date"], "end": fields["end_date"]}
    return chunk


def run(source: dict, *, days: int, until: date | None = None,
        delay_seconds: float | None = None, dry_run: bool = False) -> BackfillResult:
    """1 ソースの過去 days 日ぶんを古い順に取り込む。"""
    if not supported(source):
        raise ValueError(f"{source['source_id']}: 期間を指定して取得する手段が無い（backfill 対象外）")

    spec = source["backfill"]
    until = until or timeutil.now().date()
    delay = spec.get("delay_seconds", DEFAULT_DELAY_SECONDS) if delay_seconds is None else delay_seconds
    result = BackfillResult(source["source_id"])

    for index, (start, end) in enumerate(chunks(days=days, chunk_days=spec.get("chunk_days", 1), until=until)):
        if index and delay:
            time.sleep(delay)  # 提供元の利用条件（arXiv は 3 秒間隔）を守る
        chunk = ChunkResult(f"{start:%Y-%m-%d}", f"{end:%Y-%m-%d}")
        collected = base.collect(_chunk_source(source, start, end))
        # 期間を指定した取得では 0 件は正常な答えでありうる（arXiv は週末に announce が無い）。
        # これを失敗として扱うと、35 日ぶんの取り込みが最初の週末で打ち切られる。
        if collected.error and not collected.empty:
            chunk.error = collected.error
        else:
            chunk.fetched = len(collected.documents)
            chunk.stored = 0 if dry_run else storage.save_raw_batch(collected.documents)
        result.chunks.append(chunk)

    if not dry_run:
        record(result)
    return result


def record(result: BackfillResult) -> None:
    """取り込めた範囲を残す。既存の記録とは連続しているときだけ広げる。"""
    span = result.covered_span()
    if span is None:
        return
    start, end = span
    index = storage.load_index(INDEX_NAME)
    previous = index.get(result.source_id)
    if previous:
        # 既存範囲と離れていれば穴が空く。広げずに新しい範囲で置き換える。
        contiguous = start <= _next_day(previous["to"]) and end >= _previous_day(previous["from"])
        if contiguous:
            start = min(start, previous["from"])
            end = max(end, previous["to"])
    index[result.source_id] = {"from": start, "to": end, "recorded_at": timeutil.now_str()}
    storage.save_index(INDEX_NAME, index)


def load_spans() -> dict[str, dict]:
    return storage.load_index(INDEX_NAME)


def _next_day(day: str) -> str:
    return f"{date.fromisoformat(day) + timedelta(days=1):%Y-%m-%d}"


def _previous_day(day: str) -> str:
    return f"{date.fromisoformat(day) - timedelta(days=1):%Y-%m-%d}"
