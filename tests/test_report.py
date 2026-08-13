"""日次 Global Sector Report のテスト (§47, §44-46)。

  python3 tests/test_report.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from future100 import invariants, report  # noqa: E402

AS_OF = "2026-08-13T23:59:59Z"


def _report():
    return report.build_daily_report(as_of=AS_OF, report_date="2026-08-13")


def test_report_always_has_sixteen_sections():
    """該当が無い日も項目を省略しない。省略すると観測されなかったのか
    見落としたのかが後から区別できなくなる (§47)。"""
    payload = _report()
    assert len(payload["sections"]) == report.SECTION_COUNT
    assert [s["no"] for s in payload["sections"]] == list(range(1, 17))
    assert all(s["body"].strip() for s in payload["sections"])
    assert invariants.check_daily_report(payload) == []


def test_empty_sections_say_so_explicitly():
    payload = _report()
    empty = [s for s in payload["sections"] if "本日該当なし" in s["body"] or "未実装" in s["body"]]
    assert empty, "該当なしの項目は明示される"


def test_ticker_mention_stops_the_report():
    """§44-46: 銘柄の記載は生成時点で止める。"""
    try:
        report._section(1, "executive_summary", "t", "半導体セクターでは (NASDAQ: NVDA) が注目される。")
    except report.ProhibitedOutput as exc:
        assert "銘柄" in str(exc)
    else:
        raise AssertionError("ticker mention must be rejected")

    try:
        report._section(1, "executive_summary", "t", "$AAPL の動向に注目。")
    except report.ProhibitedOutput:
        return
    raise AssertionError("$TICKER form must be rejected")


def test_ranking_expression_stops_the_report():
    for text in ("本日の有望セクターランキングを示す。", "成長性の第 1 位は電力である。", "Top 5 sectors today."):
        try:
            report._section(8, "known_sector_monitor", "t", text)
        except report.ProhibitedOutput:
            continue
        raise AssertionError(f"ranking expression must be rejected: {text}")


def test_quoted_source_headlines_are_not_treated_as_our_words():
    """一次情報の見出しに "Best CEOs" が含まれるだけでレポート全体を止めない。
    検査対象は本システムが書いた文に限る。"""
    section = report._section(
        5, "global_capex", "t", "本日 1 件を観測。",
        ["[A] 2026-08-12 NVIDIA CEO Tops Glassdoor’s 2026 List of Best CEOs (evt_x)"],
    )
    assert "Best CEOs" in section["body"]


def test_sector_monitor_states_that_order_is_not_a_ranking():
    section = next(s for s in _report()["sections"] if s["key"] == "known_sector_monitor")
    assert "順位ではない" in section["body"]


def test_markdown_renders_all_sections():
    payload = _report()
    text = report.render_markdown(payload)
    for section in payload["sections"]:
        assert f"## {section['no']}. {section['title']}" in text
    assert "銘柄推奨・ランキングを含まない" in text


def test_report_matches_the_schema():
    try:
        import json

        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource
    except ImportError:
        print("    (skipped: jsonschema not installed)")
        return

    registry = Registry()
    for path in (ROOT / "schemas").glob("*.schema.json"):
        registry = registry.with_resource(path.name, Resource.from_contents(json.loads(path.read_text(encoding="utf-8"))))
    schema = json.loads((ROOT / "schemas/daily_report.schema.json").read_text(encoding="utf-8"))
    errors = list(Draft202012Validator(schema, registry=registry).iter_errors(_report()))
    assert not errors, "; ".join(f"{'/'.join(str(p) for p in e.path)}: {e.message}" for e in errors[:3])


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
