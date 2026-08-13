"""Early Signal Detection (§10) のテスト。

検出器に対して確かめたいのは 2 方向ある。
  - 撃つべきときに撃つ（複数種別が複数ソースで同時に増えた）
  - 撃つべきでないときに撃たない（単独種別 / 単独ソース / 観測を始めたばかり）

特に最後の「観測開始によるみかけの急増」は、実データで最初に踏んだ罠なので必ず押さえる。

  python3 tests/test_signals.py
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from future100 import signals  # noqa: E402

AS_OF = "2026-08-13T23:59:59Z"
WINDOW = signals.Window.ending(date(2026, 8, 13), window_days=7, baseline_multiplier=4)
OLD_OBSERVATION = "2026-01-01T00:00:00Z"  # baseline より前から観測しているソース
TOPIC = signals.Topic(label="電力・送配電", keywords=("送電", "grid"), sector_id="sec_power_grid")


def _event(
    event_id: str,
    *,
    event_at: str,
    signal_types: list[str],
    source_id: str = "src_a",
    observed_at: str = OLD_OBSERVATION,
    region: str = "US",
) -> dict:
    return {
        "event_id": event_id,
        "observed_at": observed_at,
        "event_at": f"{event_at}T00:00:00Z",
        "title": "送電網の増強計画",
        "summary": "grid investment",
        "signal_types": signal_types,
        "regions": [region],
        "sector_links": [{"sector_id": "sec_power_grid", "relation": "unclassified", "confidence": "low"}],
        "sources": [{"source_id": source_id, "reliability": "A", "observed_at": observed_at}],
    }


def test_co_occurrence_fires_when_multiple_types_rise():
    events = [
        # baseline 期間: 各種別 1 件ずつ
        _event("evt_b1", event_at="2026-07-15", signal_types=["government_budget"]),
        _event("evt_b2", event_at="2026-07-20", signal_types=["new_facility"], source_id="src_b"),
        # window 期間: 両種別が増加
        _event("evt_w1", event_at="2026-08-10", signal_types=["government_budget"]),
        _event("evt_w2", event_at="2026-08-11", signal_types=["government_budget"]),
        _event("evt_w3", event_at="2026-08-12", signal_types=["new_facility"], source_id="src_b"),
        _event("evt_w4", event_at="2026-08-12", signal_types=["new_facility"], source_id="src_b"),
    ]
    payload = signals.build_window(TOPIC, WINDOW, as_of=AS_OF, events=events)
    co = payload["co_occurrence"]
    assert payload["coverage"]["baseline_covered"] is True
    assert co["distinct_signal_types"] == 2
    assert co["distinct_sources"] == 2
    assert co["threshold_met"] is True


def test_single_signal_type_does_not_fire():
    """1 種別だけの増加は、その媒体が多く報じただけであることが多い。"""
    events = [
        _event("evt_b1", event_at="2026-07-15", signal_types=["paper"]),
        *[
            _event(f"evt_w{i}", event_at="2026-08-10", signal_types=["paper"])
            for i in range(20)
        ],
    ]
    payload = signals.build_window(TOPIC, WINDOW, as_of=AS_OF, events=events)
    assert payload["co_occurrence"]["distinct_signal_types"] == 1
    assert payload["co_occurrence"]["threshold_met"] is False


def test_single_source_does_not_fire():
    events = [
        _event("evt_b1", event_at="2026-07-15", signal_types=["government_budget"]),
        _event("evt_w1", event_at="2026-08-10", signal_types=["government_budget"]),
        _event("evt_w2", event_at="2026-08-10", signal_types=["government_budget"]),
        _event("evt_w3", event_at="2026-08-11", signal_types=["new_facility"]),
        _event("evt_w4", event_at="2026-08-11", signal_types=["new_facility"]),
    ]
    payload = signals.build_window(TOPIC, WINDOW, as_of=AS_OF, events=events)
    assert payload["co_occurrence"]["distinct_signal_types"] == 2
    assert payload["co_occurrence"]["distinct_sources"] == 1
    assert payload["co_occurrence"]["threshold_met"] is False


def test_cold_start_does_not_fire():
    """観測を今日始めたソースは baseline が構造的に 0 になる。これは急増ではない。"""
    fresh = "2026-08-13T00:00:00Z"
    events = [
        _event("evt_w1", event_at="2026-08-13", signal_types=["paper"], observed_at=fresh),
        _event("evt_w2", event_at="2026-08-13", signal_types=["patent"], observed_at=fresh, source_id="src_b"),
        _event("evt_w3", event_at="2026-08-13", signal_types=["patent"], observed_at=fresh, source_id="src_b"),
        _event("evt_w4", event_at="2026-08-13", signal_types=["paper"], observed_at=fresh),
    ]
    payload = signals.build_window(TOPIC, WINDOW, as_of=AS_OF, events=events)
    assert payload["coverage"]["baseline_covered"] is False
    assert payload["co_occurrence"]["distinct_signal_types"] == 2, "件数としては増加している"
    assert payload["co_occurrence"]["threshold_met"] is False, "が、観測開始が原因なので発火してはならない"
    assert "取り込み開始" in payload["promotion"]["rationale"]


def test_baseline_is_scaled_to_window_length():
    """baseline は window の 4 倍の長さ。件数は window 長に合わせて割り戻す。"""
    events = [
        _event(f"evt_b{i}", event_at=day, signal_types=["paper"])
        for i, day in enumerate(["2026-07-11", "2026-07-18", "2026-07-25", "2026-08-01"])
    ]
    payload = signals.build_window(TOPIC, WINDOW, as_of=AS_OF, events=events)
    paper = next(c for c in payload["counts"] if c["signal_type"] == "paper")
    assert paper["count"] == 0
    assert paper["baseline_count"] == 1.0, "28日間に4件 → 7日あたり1件"


def test_delta_ratio_omitted_when_baseline_is_zero():
    """0 除算を大きな数で誤魔化さない。基準がなければ増加率を定義しない。"""
    events = [_event("evt_w1", event_at="2026-08-10", signal_types=["paper"])]
    payload = signals.build_window(TOPIC, WINDOW, as_of=AS_OF, events=events)
    paper = next(c for c in payload["counts"] if c["signal_type"] == "paper")
    assert paper["baseline_count"] == 0
    assert "delta_ratio" not in paper


def test_topic_matches_by_keyword_without_sector_link():
    """まだセクターに紐づかない新概念でも集計できる (§12)。"""
    topic = signals.Topic(label="液浸冷却", keywords=("immersion", "液浸"), is_new_concept=True)
    event = _event("evt_x", event_at="2026-08-10", signal_types=["patent"])
    event["sector_links"] = []
    event["title"] = "液浸冷却の新方式を発表"
    assert topic.matches(event) is True
    assert topic.slug() == "immersion" or topic.slug().startswith("sec_") is False


def test_window_id_matches_schema_pattern():
    import re

    payload = signals.build_window(TOPIC, WINDOW, as_of=AS_OF, events=[])
    assert re.match(r"^sig_[a-z0-9_]+_\d{8}$", payload["window_id"]), payload["window_id"]


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
