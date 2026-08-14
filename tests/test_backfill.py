"""過去分の取り込みのテスト (§10, §37)。

過去分を取り込むと、Early Signal Detection の baseline を待たずに作れる。
そのぶん間違え方も増えるので、ここで固定するのは次の 4 点。

  - 観測時刻を発行日に書き換えない（過去時点の再現を壊さない, §37）
  - 期間を指定できないソースを「取り込んだ」ことにしない
  - 0 件の日で打ち切らない（arXiv は週末に announce が無い）
  - 穴の空いた期間を「観測できていた」と数えない

  python3 tests/test_backfill.py
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from future100 import backfill, config, signals  # noqa: E402
from future100.collect import base  # noqa: E402


def _source(**overrides) -> dict:
    source = {
        "source_id": "src_test_backfill",
        "name": "テスト",
        "tier": "tier1",
        "reliability": "A",
        "kind": "atom",
        "endpoint": "https://example.org/feed",
        "backfill": {"kind": "atom", "chunk_days": 1, "delay_seconds": 0,
                     "endpoint_template": "https://example.org/search?from={start_date}&to={end_date}"},
    }
    source.update(overrides)
    return source


# --- 区間の切り方 -----------------------------------------------------------

def test_chunks_cover_the_whole_range_oldest_first():
    spans = backfill.chunks(days=10, chunk_days=3, until=date(2026, 8, 14))
    assert spans[0][0] == date(2026, 8, 5), "10 日ぶんなら 8/5 から"
    assert spans[-1][1] == date(2026, 8, 14), "末尾は指定日を含む"
    assert [s for s, _ in spans] == sorted(s for s, _ in spans), "古い順に取る"
    # 区間に隙間も重なりも無いこと
    for (_, previous_end), (next_start, _) in zip(spans, spans[1:]):
        assert (next_start - previous_end).days == 1


# --- 取り込めないものを取り込んだことにしない -------------------------------

def test_source_without_a_date_range_is_not_backfillable():
    """最新フィードしか返さないソースを区間の数だけ取り直しても、過去は埋まらない。

    これを「取り込んだ」と記録すると、実際には空の baseline で増加を判定してしまう。
    """
    assert backfill.supported(_source())
    assert not backfill.supported(_source(backfill={"chunk_days": 7}))
    assert backfill.supported(
        _source(kind="json_api", http_method="POST", backfill={"chunk_days": 7})
    ), "期間を本文に書く POST は URL テンプレートが無くてもよい"


def test_post_body_uses_the_requested_range():
    """POST で取るソースは、遡及日数ではなく指定した期間を本文に書く。"""
    from future100.collect import json_api

    source = _source(kind="json_api", http_method="POST",
                     body={"filters": {"time_period": [{"start_date": "{{start_date}}",
                                                        "end_date": "{{end_date}}"}]}},
                     backfill={"chunk_days": 7})
    chunk = backfill._chunk_source(source, date(2026, 7, 1), date(2026, 7, 7))
    body = json_api._request_body(chunk).decode("utf-8")
    assert "2026-07-01" in body and "2026-07-07" in body


# --- 0 件の日で打ち切らない -------------------------------------------------

def _stub_collect(empty_days: set[str], failing_days: set[str] | None = None):
    """指定した日は 0 件、指定した日は失敗を返す取得。"""
    original = base.collect

    def collect(source: dict):
        day = source["endpoint"].split("from=")[1].split("&")[0]
        if day in (failing_days or set()):
            return base.CollectResult(source["source_id"], [], error="HTTP 500")
        if day in empty_days:
            return base.CollectResult(source["source_id"], [], error="feed contained no items", empty=True)
        return base.CollectResult(source["source_id"], [{"raw_id": f"raw_{day}"}])

    base.collect = collect
    return original


def test_empty_days_do_not_stop_the_backfill():
    """arXiv は週末に announce が無い。0 件を失敗として扱うと最初の週末で打ち切られる。"""
    original = _stub_collect(empty_days={"2026-08-08", "2026-08-09"})
    try:
        result = backfill.run(_source(), days=10, until=date(2026, 8, 14), dry_run=True)
    finally:
        base.collect = original

    assert all(chunk.error is None for chunk in result.chunks)
    assert result.covered_span() == ("2026-08-05", "2026-08-14")


def test_failed_chunk_truncates_the_recorded_span():
    """途中で落ちた区間から先は取り込めていない。穴を含む範囲を観測済みにしない。"""
    original = _stub_collect(empty_days=set(), failing_days={"2026-08-10"})
    try:
        result = backfill.run(_source(), days=10, until=date(2026, 8, 14), dry_run=True)
    finally:
        base.collect = original

    assert result.covered_span() == ("2026-08-05", "2026-08-09")


# --- 観測被覆への反映 -------------------------------------------------------

def _events(observed_at: str) -> list[dict]:
    return [{
        "event_id": "evt_20260814_aaaaaaaaaa",
        "observed_at": observed_at,
        "sources": [{"source_id": "src_test_backfill", "observed_at": observed_at}],
    }]


def test_backfill_extends_observation_coverage():
    """取り込んだ範囲が日次収集の開始に接していれば、そこまで遡れたことになる。"""
    events = _events("2026-08-14T00:00:00Z")
    spans = {"src_test_backfill": {"from": "2026-07-01", "to": "2026-08-13"}}
    coverage = signals.observation_coverage(events, spans)
    assert coverage["src_test_backfill"] == "2026-07-01T00:00:00Z"


def test_gap_between_backfill_and_live_collection_is_not_covered():
    """間が空いていれば、その期間の件数は欠測。baseline の比較は成立しない。"""
    events = _events("2026-08-14T00:00:00Z")
    spans = {"src_test_backfill": {"from": "2026-06-01", "to": "2026-07-01"}}
    coverage = signals.observation_coverage(events, spans)
    assert coverage["src_test_backfill"] == "2026-08-14T00:00:00Z", "穴があれば遡らせない"


def test_backfilled_documents_keep_todays_observed_at():
    """発行日を観測時刻に書き換えると、過去時点の再現が壊れる (§37)。

    取り込んだ過去分は「今日はじめて見た」ものであり、先月の as_of では見えない。
    """
    source = _source()
    chunk = backfill._chunk_source(source, date(2026, 7, 1), date(2026, 7, 1))
    assert "observed_at" not in chunk, "コレクタが取得時刻を入れる。区間定義で上書きしない"
    assert chunk["endpoint"].endswith("from=2026-07-01&to=2026-07-01")
    assert source["endpoint"] == "https://example.org/feed", "登録簿の定義は書き換えない"


def test_registered_backfill_sources_render_their_templates():
    sources = [s for s in config.load_sources(enabled_only=False) if s.get("backfill")]
    assert sources, "過去分を取れるソースが 1 つも登録されていない"
    for source in sources:
        assert backfill.supported(source), f"{source['source_id']}: 期間を指定する手段が無い"
        chunk = backfill._chunk_source(source, date(2026, 7, 1), date(2026, 7, 7))
        if source["backfill"].get("endpoint_template"):
            assert "2026-07-01" in chunk["endpoint"] or "20260701" in chunk["endpoint"]


def main() -> int:
    failures = 0
    for name, func in sorted(globals().items()):
        if not name.startswith("test_") or not callable(func):
            continue
        try:
            func()
            print(f"  PASS {name}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  FAIL {name}: {type(exc).__name__}: {exc}")
    print("all tests passed" if not failures else f"{failures} test(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
