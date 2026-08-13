"""Phase 7 予測 → 実績 → 誤差 → 改善のテスト (§39-43)。

この層で守りたいのは「都合よく当たったことにできない」こと。
判定方法は発行時に固定され、外れは外れとして残り、測れなかったものは
的中にも失敗にも寄せない。

  python3 tests/test_backtest.py
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def _fresh_modules(root: str):
    """一時ディレクトリを FUTURE100_ROOT にして storage / backtest を読み直す。"""
    os.environ["FUTURE100_ROOT"] = root
    from future100 import storage as storage_module

    importlib.reload(storage_module)
    from future100 import backtest as backtest_module

    return importlib.reload(backtest_module)


def _prediction(backtest, **overrides):
    payload = {
        "statement": "大型変圧器の標準リードタイムは 2027-06-30 時点で 24 か月以上を維持する",
        "subject": {"kind": "kpi", "sector_id": "sec_power_grid", "kpi_id": "kpi_lead_time"},
        "resolution": {
            "due_date": "2027-06-30",
            "metric": "大型変圧器の標準リードタイム",
            "unit": "months",
            "criterion": {"operator": ">=", "value": 24},
            "measurement_source_id": "src_us_doe_news",
        },
        "falsifier": "リードタイムが 18 か月を下回ったら撤回する",
        "issued_at": "2026-08-13T00:00:00Z",
    }
    payload.update(overrides)
    return backtest.create_prediction(**payload)


def _run(check):
    with tempfile.TemporaryDirectory() as tmp:
        backtest = _fresh_modules(tmp)
        try:
            check(backtest)
        finally:
            del os.environ["FUTURE100_ROOT"]
            from future100 import storage as storage_module

            importlib.reload(storage_module)
            importlib.reload(backtest)


def test_unverifiable_prediction_is_not_saved():
    """期限・指標・判定式のどれかを欠く予測は保存しない (§41)。"""

    def check(backtest):
        try:
            _prediction(backtest, resolution={"metric": "リードタイム"})
        except ValueError as exc:
            assert "検証できない" in str(exc)
            assert not list(backtest.PREDICTIONS_DIR.glob("prd_*.json"))
            return
        raise AssertionError("判定方法を欠く予測は拒否すべき")

    _run(check)


def test_prediction_without_falsifier_is_not_saved():
    def check(backtest):
        try:
            _prediction(backtest, falsifier="")
        except ValueError as exc:
            assert "§39" in str(exc)
            return
        raise AssertionError("撤回条件の無い予測は拒否すべき")

    _run(check)


def test_verdict_follows_the_criterion_fixed_at_issue_time():
    def check(backtest):
        prediction = _prediction(backtest)
        assert backtest.judge(prediction, 26)[0] == "hit"
        assert backtest.judge(prediction, 24)[0] == "hit", "境界値は判定式どおり >= で当たり"
        assert backtest.judge(prediction, 17)[0] == "miss"

    _run(check)


def test_unmeasurable_outcome_is_not_a_miss():
    """測れなかったことと外れたことを混ぜると、改善対象が分からなくなる (§42)。"""

    def check(backtest):
        prediction = _prediction(backtest)
        verdict, error = backtest.judge(prediction, None)
        assert verdict == "unresolvable"
        assert error == {}

        backtest.record_result(
            prediction=prediction, actual_value=None, measured_at="2027-07-01",
            cause_category="data_gap", narrative="測定ソースが公表を停止した",
        )
        summary = backtest.accuracy()
        assert summary.unresolvable == 1
        assert summary.miss == 0
        assert summary.hit_rate is None, "判定不能だけでは的中率を出さない"

    _run(check)


def test_missed_signals_must_have_been_observable_at_issue_time():
    """発行後に観測した情報を『見落とし』に数えない (§37)。"""

    def check(backtest):
        prediction = _prediction(backtest)
        result = backtest.record_result(
            prediction=prediction, actual_value=17, measured_at="2027-07-01",
            cause_category="supply_response_faster", narrative="増産が想定より早かった",
            missed_signals=["evt_20270101_ffffffffff"],  # 発行後のイベント（存在もしない）
        )
        assert "missed_signals" not in result["error_analysis"]

    _run(check)


def test_accuracy_and_cause_breakdown():
    def check(backtest):
        hit = _prediction(backtest)
        miss = _prediction(backtest, statement="別の予測: 24 か月以上を維持する")
        backtest.record_result(prediction=hit, actual_value=26, measured_at="2027-07-01",
                               cause_category="correct_as_predicted", narrative="想定どおり")
        backtest.record_result(prediction=miss, actual_value=17, measured_at="2027-07-01",
                               cause_category="supply_response_faster", narrative="増産が早かった")

        summary = backtest.accuracy()
        assert (summary.total, summary.hit, summary.miss) == (2, 1, 1)
        assert summary.hit_rate == 0.5
        assert backtest.cause_breakdown() == [("supply_response_faster", 1)]

    _run(check)


def test_due_predictions_exclude_recorded_ones():
    def check(backtest):
        prediction = _prediction(backtest)
        assert len(backtest.due_predictions(as_of="2027-07-01T00:00:00Z")) == 1
        assert backtest.due_predictions(as_of="2026-09-01T00:00:00Z") == [], "期限前は対象外"

        backtest.record_result(prediction=prediction, actual_value=26, measured_at="2027-07-01",
                               cause_category="correct_as_predicted", narrative="想定どおり")
        assert backtest.due_predictions(as_of="2027-07-01T00:00:00Z") == []

    _run(check)


def test_predictions_issued_after_as_of_are_invisible():
    """§37: 分析時点より後に発行した予測を、その時点の精度に混ぜない。"""

    def check(backtest):
        _prediction(backtest, issued_at="2026-12-01T00:00:00Z")
        assert backtest.accuracy(as_of="2026-08-13T00:00:00Z").total == 0
        assert backtest.accuracy(as_of="2027-01-01T00:00:00Z").total == 1

    _run(check)


def test_monthly_review_states_absence_explicitly():
    def check(backtest):
        text = backtest.render_monthly_review(as_of="2026-08-13T00:00:00Z")
        assert "該当なし" in text
        assert "判定済みの予測がまだ無い" in text
        assert "セクターの優劣ではなく" in text, "§44-46: 精度は順位づけではない"

    _run(check)


def test_result_matches_the_schema():
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

    def check(backtest):
        prediction = _prediction(backtest)
        result = backtest.record_result(prediction=prediction, actual_value=17, measured_at="2027-07-01",
                                        cause_category="timing_lag", narrative="時期がずれた")
        for payload, schema_name in ((prediction, "prediction"), (result, "backtest_result")):
            schema = json.loads((ROOT / f"schemas/{schema_name}.schema.json").read_text(encoding="utf-8"))
            errors = list(Draft202012Validator(schema, registry=registry).iter_errors(payload))
            assert not errors, f"{schema_name}: {errors[0].message}"

    _run(check)


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
