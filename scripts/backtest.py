#!/usr/bin/env python3
"""Phase 7: 予測 → 実績 → 誤差 → 原因分析 → 改善 (§39-43)。

  python3 scripts/backtest.py --due                    判定期限を迎えた予測を一覧
  python3 scripts/backtest.py --summary                予測精度と原因内訳
  python3 scripts/backtest.py --review                 月次レビューを生成・保存
  python3 scripts/backtest.py --record prd_... --actual 26 --cause timing_lag \
      --narrative "リードタイム短縮が想定より早かった"

外れた予測を消さないこと。原因分類が改善の入口になる (§43)。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from future100 import backtest, timeutil  # noqa: E402

CAUSES = [
    "data_gap", "source_reliability", "timing_lag", "causal_link_wrong",
    "magnitude_overestimate", "magnitude_underestimate", "policy_reversal",
    "substitution", "demand_assumption", "supply_response_faster",
    "definition_mismatch", "correct_as_predicted",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--as-of", help="この時点で発行済みの予測のみを対象にする (§37)")
    parser.add_argument("--due", action="store_true", help="判定期限を迎えた未処理の予測を表示")
    parser.add_argument("--summary", action="store_true", help="予測精度と原因内訳を表示")
    parser.add_argument("--review", action="store_true", help="月次レビューを生成して保存")
    parser.add_argument("--record", help="結果を記録する対象の prediction_id")
    parser.add_argument("--actual", type=float, help="実績値。測定できなかった場合は省略する")
    parser.add_argument("--measured-at", help="実績の測定日 (YYYY-MM-DD)")
    parser.add_argument("--cause", choices=CAUSES, help="原因分類 (§43)")
    parser.add_argument("--narrative", help="なぜ外れたか / 当たったか")
    args = parser.parse_args()

    as_of = args.as_of or timeutil.now_str()

    if args.record:
        if not (args.cause and args.narrative):
            parser.error("--record には --cause と --narrative が必要（原因分析なしの記録は残さない）")
        predictions = {p["prediction_id"]: p for p in backtest.load_predictions()}
        prediction = predictions.get(args.record)
        if prediction is None:
            print(f"予測が見つからない: {args.record}", file=sys.stderr)
            return 1
        result = backtest.record_result(
            prediction=prediction,
            actual_value=args.actual,
            measured_at=args.measured_at or f"{timeutil.day_of(as_of):%Y-%m-%d}",
            cause_category=args.cause,
            narrative=args.narrative,
        )
        outcome = result["outcome"]
        print(f"{result['result_id']}: {outcome['verdict']} (実績 {outcome['actual']['value']})")
        return 0

    if args.due or not (args.summary or args.review):
        due = backtest.due_predictions(as_of=as_of)
        print(f"判定期限を迎えた未処理の予測: {len(due)} 件")
        for prediction in due:
            resolution = prediction["resolution"]
            print(f"  {prediction['prediction_id']} 期限 {resolution['due_date']} "
                  f"[{resolution['metric']} {resolution['criterion']['operator']} {resolution['criterion']['value']}]")
            print(f"    {prediction['statement'][:76]}")
        if not due:
            print("  該当なし")

    if args.summary:
        summary = backtest.accuracy(as_of=as_of)
        print(f"\n予測精度 (§41): 発行 {summary.total} / 判定済み {summary.resolved} "
              f"（的中 {summary.hit} / 外れ {summary.miss} / 部分 {summary.partial}）")
        print(f"  判定不能 {summary.unresolvable} / 未判定 {summary.pending}")
        print(f"  的中率: {summary.hit_rate if summary.hit_rate is not None else '判定済みの予測がまだ無い'}")
        causes = backtest.cause_breakdown(as_of=as_of)
        print("\n外れた原因の内訳 (§43):")
        for cause, count in causes or []:
            print(f"  {cause}: {count}")
        if not causes:
            print("  該当なし")

    if args.review:
        path = backtest.save_monthly_review(as_of=as_of)
        print(f"\n月次レビューを保存した: {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
