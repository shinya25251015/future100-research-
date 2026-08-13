"""Phase 7: 予測 → 実績 → 誤差 → 原因分析 → 改善 (§39-43)。

仕様書が求めるループを閉じる層。ここが動かないと、システムは「当たったことにする」
方向にいくらでも漂う。設計は 3 つの規律で固定する。

  1. **発行時に判定方法を固定する。** 期限・指標・判定式・測定ソースを予測と同時に
     決め、あとから緩められないようにする (§41)。判定式を欠く予測は保存しない。
  2. **予測は不変。** 訂正は新しい予測を発行して supersedes で繋ぐ。上書きすると
     「予測が実績に近づいていく」ことが起こる (§39)。
  3. **外れた予測こそ残す。** verdict を hit に丸めず、原因分類を改善アクションに繋ぐ。
     判定不能 (unresolvable) も独立した結果として扱う (§42-43)。
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from . import ids, invariants, storage, textnorm, timeutil

PREDICTIONS_DIR = storage.DATA / "predictions"
RESULTS_DIR = storage.ROOT / "backtest" / "outcomes"

_OPERATORS = {
    ">=": lambda actual, value, upper: actual >= value,
    ">": lambda actual, value, upper: actual > value,
    "<=": lambda actual, value, upper: actual <= value,
    "<": lambda actual, value, upper: actual < value,
    "==": lambda actual, value, upper: actual == value,
    "between": lambda actual, value, upper: upper is not None and value <= actual <= upper,
}


@dataclass
class Accuracy:
    """§41 Sector Forecast Accuracy。件数が少ないうちは率を読まない。"""

    total: int = 0
    hit: int = 0
    miss: int = 0
    partial: int = 0
    unresolvable: int = 0
    pending: int = 0

    @property
    def resolved(self) -> int:
        return self.hit + self.miss + self.partial

    @property
    def hit_rate(self) -> float | None:
        """判定済みに対する的中率。判定不能と未判定は分母に入れない。"""
        return round(self.hit / self.resolved, 3) if self.resolved else None


# --- 予測の発行 -----------------------------------------------------------

def create_prediction(
    *,
    statement: str,
    subject: dict,
    resolution: dict,
    falsifier: str,
    confidence: str = "medium",
    issued_at: str | None = None,
    **extra,
) -> dict:
    """予測を作って保存する。検証できない予測は保存しない (§41)。"""
    issued_at = issued_at or timeutil.now_str()
    prediction = {
        "prediction_id": (
            f"prd_{timeutil.day_of(issued_at):%Y%m%d}_"
            f"{textnorm.short_hash(statement, issued_at, length=8)}"
        ),
        "issued_at": issued_at,
        "subject": subject,
        "statement": statement,
        "resolution": resolution,
        "confidence": confidence,
        "falsifier": falsifier,
        "schema_version": "1.0",
        **extra,
    }

    problems = invariants.check_prediction(prediction)
    if problems:
        raise ValueError("検証できない予測は保存しない: " + "; ".join(problems))

    storage.write_json(PREDICTIONS_DIR / f"{prediction['prediction_id']}.json", prediction)
    return prediction


def load_predictions(*, as_of: str | None = None) -> list[dict]:
    """発行済みの予測。as_of を渡すと、その時点で発行済みのものだけを返す (§37)。"""
    rows = [storage.read_json(path) for path in sorted(PREDICTIONS_DIR.glob("prd_*.json"))]
    if as_of:
        rows = [p for p in rows if timeutil.is_visible(p["issued_at"], as_of)]
    return rows


def load_results() -> dict[str, dict]:
    """prediction_id → BacktestResult。"""
    results = {}
    for path in sorted(RESULTS_DIR.glob("bt_*.json")):
        result = storage.read_json(path)
        results[result["prediction_id"]] = result
    return results


def due_predictions(*, as_of: str | None = None) -> list[dict]:
    """判定期限を迎え、まだ結果が記録されていない予測。"""
    as_of = as_of or timeutil.now_str()
    today = f"{timeutil.day_of(as_of):%Y-%m-%d}"
    recorded = load_results()
    return [
        prediction
        for prediction in load_predictions(as_of=as_of)
        if prediction["resolution"]["due_date"] <= today
        and prediction["prediction_id"] not in recorded
    ]


# --- 判定 -----------------------------------------------------------------

def judge(prediction: dict, actual_value: float | None) -> tuple[str, dict]:
    """実績値から verdict と誤差を求める。

    測定できなかった場合は unresolvable とし、hit にも miss にも寄せない。
    「測れなかった」を「外れた」と混ぜると、改善対象が分からなくなる (§42)。
    """
    if actual_value is None:
        return "unresolvable", {}

    criterion = prediction["resolution"]["criterion"]
    operator = criterion["operator"]
    expected = criterion["value"]
    upper = criterion.get("value_upper")

    verdict = "hit" if _OPERATORS[operator](actual_value, expected, upper) else "miss"
    error = {"absolute": round(actual_value - expected, 6)}
    if expected:
        error["relative"] = round((actual_value - expected) / expected, 6)
    return verdict, error


def record_result(
    *,
    prediction: dict,
    actual_value: float | None,
    measured_at: str,
    cause_category: str,
    narrative: str,
    unit: str | None = None,
    evidence: list[dict] | None = None,
    verdict: str | None = None,
    improvement: dict | None = None,
    missed_signals: list[str] | None = None,
    evaluated_at: str | None = None,
) -> dict:
    """実績と原因分析を記録する (§42-43)。

    見落としたシグナルは「発行時点で観測できていたもの」に限る。発行後に観測した
    情報を『見落とし』に数えると、あとから見れば分かることを反省し続けることになる (§37)。
    """
    evaluated_at = evaluated_at or timeutil.now_str()
    computed_verdict, error = judge(prediction, actual_value)
    verdict = verdict or computed_verdict

    checked_signals = []
    for event_id in missed_signals or []:
        if _observable_at(event_id, prediction["issued_at"]):
            checked_signals.append(event_id)

    result = {
        "result_id": (
            f"bt_{timeutil.day_of(evaluated_at):%Y%m%d}_"
            f"{textnorm.short_hash(prediction['prediction_id'], evaluated_at, length=8)}"
        ),
        "prediction_id": prediction["prediction_id"],
        "evaluated_at": evaluated_at,
        "outcome": {
            "verdict": verdict,
            "actual": {
                "value": actual_value,
                "measured_at": measured_at,
                **({"unit": unit} if unit else {}),
                **({"evidence": evidence} if evidence else {}),
            },
            **({"error": error} if error else {}),
        },
        "error_analysis": {
            "cause_category": cause_category,
            "narrative": narrative,
            **({"missed_signals": checked_signals} if checked_signals else {}),
        },
        **({"improvement": improvement} if improvement else {}),
        "schema_version": "1.0",
    }

    storage.write_json(RESULTS_DIR / f"{result['result_id']}.json", result)
    return result


def _observable_at(event_id: str, issued_at: str) -> bool:
    """そのイベントが予測発行時点で観測済みだったか。"""
    for event in storage.iter_events(as_of=issued_at):
        if event["event_id"] == event_id:
            return True
    return False


# --- 集計 (§41, §43) ------------------------------------------------------

def accuracy(*, as_of: str | None = None, sector_id: str | None = None) -> Accuracy:
    """予測精度。セクター間の順位付けには使わない (§44-46)。"""
    results = load_results()
    summary = Accuracy()
    for prediction in load_predictions(as_of=as_of):
        if sector_id and prediction["subject"].get("sector_id") != sector_id:
            continue
        summary.total += 1
        result = results.get(prediction["prediction_id"])
        if result is None:
            summary.pending += 1
            continue
        verdict = result["outcome"]["verdict"]
        setattr(summary, verdict, getattr(summary, verdict) + 1)
    return summary


def cause_breakdown(*, as_of: str | None = None) -> list[tuple[str, int]]:
    """外れた原因の内訳。改善アクションの優先順位づけに使う (§43)。"""
    visible = {p["prediction_id"] for p in load_predictions(as_of=as_of)}
    causes = Counter(
        result["error_analysis"]["cause_category"]
        for prediction_id, result in load_results().items()
        if prediction_id in visible and result["outcome"]["verdict"] in ("miss", "partial")
    )
    return causes.most_common()


def open_improvements() -> list[dict]:
    """未着手の改善アクション。閉じるまで残す (§43)。"""
    return [
        {"result_id": result["result_id"], **result["improvement"]}
        for result in load_results().values()
        if result.get("improvement", {}).get("status") in ("open", "in_progress")
    ]


def render_monthly_review(*, as_of: str | None = None) -> str:
    """月次の自己分析 (§43)。予測が無い月も「無い」と書いて残す。"""
    as_of = as_of or timeutil.now_str()
    summary = accuracy(as_of=as_of)
    causes = cause_breakdown(as_of=as_of)
    improvements = open_improvements()
    due = due_predictions(as_of=as_of)

    lines = [
        f"# 予測レビュー {timeutil.day_of(as_of):%Y-%m}",
        "",
        f"as_of: {as_of}",
        "",
        "## 予測精度 (§41)",
        "",
        f"- 発行済み {summary.total} / 判定済み {summary.resolved}"
        f"（的中 {summary.hit} / 外れ {summary.miss} / 部分 {summary.partial}）",
        f"- 判定不能 {summary.unresolvable} / 未判定 {summary.pending}",
        f"- 的中率: {summary.hit_rate if summary.hit_rate is not None else '判定済みの予測がまだ無い'}",
        "",
        "件数が少ないうちは率を読まない。的中率はセクターの優劣ではなく、本システムの誤差の測定値である。",
        "",
        "## 外れた原因の内訳 (§43)",
        "",
    ]
    lines += [f"- {cause}: {count} 件" for cause, count in causes] or ["- 該当なし"]
    lines += ["", "## 判定期限を迎えた未処理の予測", ""]
    lines += [
        f"- {p['prediction_id']} 期限 {p['resolution']['due_date']}: {p['statement']}" for p in due
    ] or ["- 該当なし"]
    lines += ["", "## 未着手の改善アクション (§43)", ""]
    lines += [
        f"- [{item['status']}] {item.get('target', '-')}: {item.get('action', '')}" for item in improvements
    ] or ["- 該当なし"]
    return "\n".join(lines) + "\n"


def save_monthly_review(*, as_of: str | None = None):
    as_of = as_of or timeutil.now_str()
    path = storage.REPORTS_DIR / "monthly" / f"review_{timeutil.day_of(as_of):%Y-%m}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_monthly_review(as_of=as_of), encoding="utf-8")
    return path


__all__ = [
    "Accuracy",
    "accuracy",
    "cause_breakdown",
    "create_prediction",
    "due_predictions",
    "ids",
    "judge",
    "load_predictions",
    "load_results",
    "open_improvements",
    "record_result",
    "render_monthly_review",
    "save_monthly_review",
]
