"""Phase 2 パイプラインと仕様書不変条件のテスト。

  python3 -m pytest tests -q      （pytest がある場合）
  python3 tests/test_pipeline.py  （無い場合はこれで全件実行される）
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from future100 import dedup, ids, invariants, textnorm, timeutil  # noqa: E402
from future100.collect import rss  # noqa: E402


# --- 正規化 ---------------------------------------------------------------

def test_canonical_url_strips_tracking_and_case():
    assert textnorm.canonical_url("https://WWW.Example.com/a/?utm_source=x&b=1#frag") == "https://example.com/a?b=1"
    assert textnorm.canonical_url("http://example.com:80/a/b/") == "http://example.com/a/b"


def test_tokens_handle_japanese_without_tokenizer():
    a = textnorm.tokens("半導体の輸出規制を強化")
    b = textnorm.tokens("半導体の輸出規制が強化された")
    assert textnorm.jaccard(a, b) > 0.4
    assert textnorm.jaccard(a, textnorm.tokens("原子力発電所の再稼働")) < 0.15


def test_ids_are_deterministic():
    assert ids.raw_id("https://example.com/a") == ids.raw_id("https://example.com/a")
    assert ids.sector_id("Power Grid") == "sec_power_grid"


# --- 時刻と Look-ahead Bias (§36-37) ---------------------------------------

def test_naive_datetime_is_rejected():
    from datetime import datetime

    try:
        timeutil.fmt(datetime(2026, 1, 1))
    except ValueError:
        return
    raise AssertionError("naive datetime must be rejected")


def test_look_ahead_guard():
    assert timeutil.is_visible("2026-08-01T00:00:00Z", "2026-08-13T00:00:00Z")
    assert not timeutil.is_visible("2026-08-14T00:00:00Z", "2026-08-13T00:00:00Z")
    try:
        timeutil.assert_visible("2026-08-14T00:00:00Z", "2026-08-13T00:00:00Z")
    except timeutil.LookAheadError:
        return
    raise AssertionError("future observation must raise LookAheadError")


# --- 重複統合 (§38) --------------------------------------------------------

def _assign(index, *, url, title, body, date, event_id, source_id="src_a"):
    return dedup.assign(
        index,
        canonical_url=url,
        content_hash=textnorm.content_hash(title, body),
        event_key=dedup.build_event_key(actors=["X"], action=title, event_at=date),
        text=f"{title}\n{body}",
        event_id=event_id,
        event_date=date,
        source_id=source_id,
    )


def test_same_story_from_two_outlets_is_merged():
    index = dedup.ClusterIndex(day="2026-08-13")
    first = _assign(
        index,
        url="https://a.example/1",
        title="政府が半導体工場に補助金を決定",
        body="経済産業省は先端半導体工場の建設に対する補助金の交付を決定した。",
        date="2026-08-13",
        event_id="evt_20260813_aaaaaaaaaa",
    )
    second = _assign(
        index,
        url="https://b.example/9",
        title="半導体工場への補助金交付が決定",
        body="経済産業省が先端半導体工場の建設に対し補助金を交付すると決定した。",
        date="2026-08-13",
        event_id="evt_20260813_bbbbbbbbbb",
        source_id="src_b",
    )
    assert first[1] is True, "最初のイベントは代表になる"
    assert second[1] is False, "同一事象は代表にならない"
    assert first[0] == second[0], "同じクラスタに束ねられる"


def test_recurring_report_on_different_days_is_not_merged():
    """定例発表は本文が酷似するが別の出来事。発生日が違えば統合してはならない。"""
    body = "営業毎旬報告。当該旬末の営業状況を公表する。"
    first = _assign(dedup.ClusterIndex(day="2026-07-11"), url="https://c.example/1",
                    title="営業毎旬報告（7月10日現在）", body=body,
                    date="2026-07-11", event_id="evt_20260711_cccccccccc")
    second = _assign(dedup.ClusterIndex(day="2026-07-21"), url="https://c.example/2",
                     title="営業毎旬報告（7月20日現在）", body=body,
                     date="2026-07-21", event_id="evt_20260721_dddddddddd")
    assert first[0] != second[0]
    assert second[1] is True


def test_identical_text_on_different_days_is_not_merged():
    """定例公告は数か月後に一字一句同じ本文で再掲される。完全一致でも発生日が違えば別の出来事。

    日付の分離は索引そのものが発生日ごとに分かれていることで担保される。
    """
    title, body = "日本銀行が保有する国債の銘柄別残高", "月次の保有残高を公表する。"
    july = dedup.ClusterIndex(day="2026-07-14")
    august = dedup.ClusterIndex(day="2026-08-13")
    first = _assign(july, url="https://boj.example/mei260710.xlsx", title=title, body=body,
                    date="2026-07-14", event_id="evt_20260714_eeeeeeeeee", source_id="src_boj")
    second = _assign(august, url="https://boj.example/mei260810.xlsx", title=title, body=body,
                     date="2026-08-13", event_id="evt_20260813_ffffffffff", source_id="src_boj")
    assert first[0] != second[0], "同一本文でも発生日が違えば別クラスタ"
    assert second[1] is True


def test_identical_text_on_the_same_day_is_merged():
    index = dedup.ClusterIndex(day="2026-08-13")
    title, body = "Sunshine Act Meetings", "Notice of meeting."
    first = _assign(index, url="https://fr.example/16519", title=title, body=body,
                    date="2026-08-13", event_id="evt_20260813_7777777777", source_id="src_fr")
    second = _assign(index, url="https://fr.example/16543", title=title, body=body,
                     date="2026-08-13", event_id="evt_20260813_8888888888", source_id="src_fr")
    assert first[0] == second[0]
    assert second[1] is False
    assert second[2]["method"] == "content_hash"


def test_partition_rejects_an_event_from_another_day():
    """取り違えて別日のイベントを入れると、日付で閉じている前提が壊れる。"""
    index = dedup.ClusterIndex(day="2026-08-13")
    try:
        _assign(index, url="https://a.example/1", title="別の日の出来事", body="本文",
                date="2026-07-01", event_id="evt_20260701_0000000001")
    except ValueError:
        return
    raise AssertionError("mismatched event_date must be rejected")


def test_index_does_not_persist_comparison_tokens():
    """トークンを永続化すると索引が日々数 MB 増え、観測履歴をリポジトリに残せなくなる。"""
    index = dedup.ClusterIndex(day="2026-08-13")
    _assign(index, url="https://a.example/1", title="送電網の増強", body="変圧器の調達を進める。",
            date="2026-08-13", event_id="evt_20260813_9999999999")
    cluster = next(iter(index.clusters.values()))
    assert "tokens" not in cluster
    assert index._tokens, "比較用トークンは実行中のメモリには載っている"


def test_boilerplate_from_same_source_is_not_merged():
    """官報系の公告は定型文が大半で別件でも文面が酷似する。同一ソース内では統合しない。"""
    index = dedup.ClusterIndex(day="2026-08-13")
    body = (
        "Agency Information Collection Activities; Submission to the Office of Management and Budget "
        "for Review and Approval; Comment Request; notice of submission."
    )
    first = _assign(index, url="https://fr.example/1", title="Federal Reserve Board announces approval of the application by First Bank",
                    body=body, date="2026-08-13", event_id="evt_20260813_1111111111", source_id="src_fr")
    second = _assign(index, url="https://fr.example/2", title="Federal Reserve Board announces approval of the application by Citizens Bank",
                     body=body, date="2026-08-13", event_id="evt_20260813_2222222222", source_id="src_fr")
    assert first[0] != second[0]
    assert second[1] is True


# --- 不変条件 (§17-19, §39, §44-46, §47) -----------------------------------

def test_inferred_claim_without_basis_is_a_violation():
    event = {
        "event_id": "evt_20260813_0000000000",
        "observed_at": "2026-08-13T00:00:00Z",
        "event_at": "2026-08-13T00:00:00Z",
        "sources": [{"source_id": "src_x", "reliability": "A"}],
        "claims": [{"claim_id": "c1", "type": "inferred", "statement": "電力需要が増える", "confidence": "medium"}],
    }
    problems = invariants.check_event(event)
    assert any("§17-19" in p for p in problems)

    event["claims"][0]["basis"] = ["evt_20260812_0000000000#c1"]
    assert invariants.check_event(event) == []


def test_sector_requires_three_scenarios_with_falsifiers():
    profile = {
        "as_of": "2026-08-13T00:00:00Z",
        "growth": {"probability": {}, "magnitude": {}},
        "views": {"consensus": {}, "independent": {}},
        "scenarios": [
            {"scenario": "base", "falsifier": "x"},
            {"scenario": "bull", "falsifier": "y"},
        ],
    }
    problems = invariants.check_sector(profile)
    assert any("bear/base/bull" in p for p in problems)

    profile["scenarios"].append({"scenario": "bear"})
    assert any("§39" in p for p in invariants.check_sector(profile))


def test_score_must_be_internal_only():
    profile = {
        "as_of": "2026-08-13T00:00:00Z",
        "growth": {"probability": {}, "magnitude": {}},
        "views": {"consensus": {}, "independent": {}},
        "scenarios": [{"scenario": s, "falsifier": "x"} for s in ("bear", "base", "bull")],
        "future_sector_score": {"total": 80, "internal_only": False, "components": {}},
    }
    assert any("§44-46" in p for p in invariants.check_sector(profile))


def test_daily_report_template_satisfies_invariants():
    import json

    template = json.loads((ROOT / "reports/templates/daily_report_template.json").read_text(encoding="utf-8"))
    assert invariants.check_daily_report(template) == []


# --- raw アーカイブ (§48-51) -----------------------------------------------

def test_raw_archive_roundtrip_and_idempotency(tmp_root=None):
    """raw は追記のみ。同じ raw_id を二度書かず、読み戻して同一内容になる。"""
    import importlib
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["FUTURE100_ROOT"] = tmp
        from future100 import storage as storage_module

        storage = importlib.reload(storage_module)
        docs = [
            {
                "raw_id": f"raw_{i:016x}",
                "source_id": "src_a",
                "observed_at": "2026-08-13T00:00:00Z",
                "fetch": {"url": f"https://a.example/{i}", "canonical_url": f"https://a.example/{i}", "method": "rss"},
                "content": {"title": f"t{i}", "body": "b", "content_hash": "0" * 64},
            }
            for i in range(3)
        ]
        assert storage.save_raw_batch(docs) == 3
        assert storage.save_raw_batch(docs) == 0, "同じ raw_id は二度書かない"

        loaded = list(storage.iter_raw())
        assert [d["raw_id"] for d in loaded] == [d["raw_id"] for d in docs]
        assert storage.rebuild_raw_index().keys() == {d["raw_id"] for d in docs}

        archive = storage.raw_archive_path("src_a", "2026-08-13T00:00:00Z")
        assert archive.exists() and archive.name.endswith(".jsonl.gz")

    del os.environ["FUTURE100_ROOT"]
    importlib.reload(storage_module)


# --- コレクタ -------------------------------------------------------------

def test_rss_parses_minimal_feed():
    xml = b"""<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>Test &amp; Title</title><link>https://example.com/x?utm_source=a</link>
      <description>&lt;p&gt;body text&lt;/p&gt;</description>
      <pubDate>Mon, 10 Aug 2026 12:00:00 GMT</pubDate></item>
    </channel></rss>"""
    import xml.etree.ElementTree as ET

    entry = ET.fromstring(xml).find(".//item")
    link, title, body, published = rss._parse_entry(entry)
    assert title == "Test & Title"
    assert body == "body text"
    assert published == "2026-08-10T12:00:00Z"
    assert link.startswith("https://example.com/x")


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
