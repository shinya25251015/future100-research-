#!/usr/bin/env python3
"""Phase 3-1: Early Signal Detection (§10)。

弱いシグナルの同時増加を集計し、data/signals/ に SignalWindow として保存する。
推論は行わない。ここで出るのは「どこで何が増えたか」まで。

  python3 scripts/detect_signals.py
  python3 scripts/detect_signals.py --window-days 14 --as-of 2026-08-13T00:00:00Z
  python3 scripts/detect_signals.py --topic "液浸冷却" --keywords immersion,液浸,液冷
  python3 scripts/detect_signals.py --dry-run --show-all
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from future100 import signals, timeutil  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--as-of", help="この時刻までに観測したイベントのみを使う (§37)。既定は現在時刻")
    parser.add_argument("--window-days", type=int, default=7, help="集計期間の長さ（既定 7 日）")
    parser.add_argument("--baseline-multiplier", type=int, default=4, help="比較する過去期間の倍率（既定 4）")
    parser.add_argument("--topic", help="既知セクターの代わりに任意の topic を集計する")
    parser.add_argument("--keywords", help="--topic のキーワード（カンマ区切り）")
    parser.add_argument("--dry-run", action="store_true", help="保存せず結果表示のみ")
    parser.add_argument("--show-all", action="store_true", help="しきい値未達の topic も表示する")
    args = parser.parse_args()

    if args.topic and not args.keywords:
        parser.error("--topic には --keywords が必要です")

    topics = None
    if args.topic:
        topics = [
            signals.Topic(
                label=args.topic,
                keywords=tuple(k.strip().lower() for k in args.keywords.split(",") if k.strip()),
                is_new_concept=True,
            )
        ]

    as_of = args.as_of or timeutil.now_str()
    windows = signals.run(
        as_of=as_of,
        window_days=args.window_days,
        baseline_multiplier=args.baseline_multiplier,
        topics=topics,
        save=not args.dry_run,
    )
    if not windows:
        print("no topics to evaluate")
        return 0

    period = windows[0]["window"]
    print(f"as_of={as_of}")
    print(f"window={period['start']}..{period['end']}  baseline={period['baseline_start']}..{period['baseline_end']}\n")
    print(f"{'topic':<28}{'types':>6}{'src':>5}{'reg':>5}{'score':>7}  {'cover':<7}signals")
    print("-" * 104)

    fired = 0
    held = 0
    for payload in sorted(windows, key=lambda w: w["topic"]["label"]):
        co = payload["co_occurrence"]
        covered = payload["coverage"]["baseline_covered"]
        if co["threshold_met"]:
            fired += 1
        elif not covered and co["distinct_signal_types"] >= 1:
            held += 1
        elif not args.show_all:
            continue
        detail = ", ".join(
            f"{c['signal_type']}={c['count']}(基準{c['baseline_count']:g})"
            for c in sorted(payload["counts"], key=lambda c: -c["count"])[:4]
        )
        mark = "*" if co["threshold_met"] else ("!" if not covered else " ")
        label = payload["topic"]["label"][:26]
        cover = "ok" if covered else "不足"
        print(
            f"{mark}{label:<27}{co['distinct_signal_types']:>6}{co['distinct_sources']:>5}"
            f"{co['distinct_regions']:>5}{co['score']:>7}  {cover:<7}{detail or '該当なし'}"
        )

    print(f"\n{fired}/{len(windows)} topics met the co-occurrence threshold "
          f"(rising types>={signals.MIN_RISING_TYPES}, sources>={signals.MIN_DISTINCT_SOURCES})")
    print("* = 構造評価 (Phase 3-2) の対象候補。集計層はセクターの成長を判断しない (§10, §44-46)。")
    if held:
        print(f"! = 観測期間が baseline に届かず判定保留（{held} 件）。取り込み開始による見かけの急増を"
              "シグナルとして扱わない。baseline 期間ぶん収集を続けると自動的に解消する。")
    if args.dry_run:
        print("dry-run: data/signals/ には保存していない")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
