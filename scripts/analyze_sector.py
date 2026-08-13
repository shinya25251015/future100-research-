#!/usr/bin/env python3
"""Phase 3-2: セクター構造評価 (§8-9, §20-34)。

  python3 scripts/analyze_sector.py --sector sec_power_grid            入力の内訳を表示
  python3 scripts/analyze_sector.py --sector sec_power_grid --show-prompt
  python3 scripts/analyze_sector.py --list

LLM の生成部分はまだ接続していない。このコマンドで確認できるのは
「何を根拠として渡すことになるか」までで、生成結果の検証は
future100.sector_analysis.review() が担う。

生成を接続する際は sector_analysis.draft_profile(generator=...) に
プロンプト → JSON 文字列 の関数を渡す。review() が空リストを返した場合のみ保存する。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from future100 import config, sector_analysis, timeutil  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sector", help="対象セクター (config/known_sectors.json の sector_id)")
    parser.add_argument("--as-of", help="分析時点。既定は現在時刻 (§37)")
    parser.add_argument("--window-days", type=int, default=sector_analysis.DEFAULT_WINDOW_DAYS)
    parser.add_argument("--max-events", type=int, default=sector_analysis.DEFAULT_MAX_EVENTS)
    parser.add_argument("--show-prompt", action="store_true", help="生成に渡すプロンプトを表示する")
    parser.add_argument("--list", action="store_true", help="全セクターの根拠件数を一覧する")
    parser.add_argument("--generate", action="store_true", help="Claude API で評価を生成する（要 ANTHROPIC_API_KEY）")
    parser.add_argument("--replay", help="保存済み生成結果を読み直して検証のみ行う")
    parser.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    args = parser.parse_args()

    as_of = args.as_of or timeutil.now_str()

    if args.list or not args.sector:
        print(f"as_of={as_of}  window={args.window_days}d\n")
        print(f"{'sector_id':<26}{'events':>7}{'A':>5}{'B':>5}{'C':>5}{'D':>5}  name")
        print("-" * 78)
        for sector in config.load_known_sectors():
            bundle = sector_analysis.build_bundle(
                sector["sector_id"], as_of=as_of, window_days=args.window_days, max_events=args.max_events
            )
            counts = bundle.reliability_counts()
            print(
                f"{sector['sector_id']:<26}{len(bundle.events):>7}{counts['A']:>5}{counts['B']:>5}"
                f"{counts['C']:>5}{counts['D']:>5}  {sector['name']}"
            )
        print("\n根拠が 0 件のセクターは評価しない（根拠なしの評価を作らない §35-38）。")
        return 0

    bundle = sector_analysis.build_bundle(
        args.sector, as_of=as_of, window_days=args.window_days, max_events=args.max_events
    )
    print(bundle.summary_line())
    if not bundle.events:
        print("\n根拠となるイベントが無い。収集を進めてから評価する。")
        return 0

    print("\n最新の根拠 (上位 10 件):")
    for event in bundle.events[:10]:
        print(f"  [{event.get('max_reliability', '?')}] {event['event_at'][:10]} {event['title'][:66]}")

    if args.show_prompt:
        for view_kind in ("consensus", "independent"):
            print(f"\n{'=' * 30} prompt: {view_kind} {'=' * 30}")
            print(sector_analysis.render_prompt(bundle, view_kind))

    if not (args.generate or args.replay):
        print("\n--generate で評価を生成する（要 ANTHROPIC_API_KEY）。")
        print("生成結果は review() を通し、違反が無い場合のみ data/sectors/ に保存する。")
        return 0

    from future100 import generators, storage  # noqa: PLC0415

    try:
        generator = (
            generators.replay_generator(args.replay)
            if args.replay
            else generators.anthropic_generator(effort=args.effort)
        )
    except generators.GeneratorUnavailable as exc:
        print(f"\n生成器を用意できない: {exc}", file=sys.stderr)
        return 1

    print(f"\n生成中 (effort={args.effort}) ...")
    try:
        profile = sector_analysis.draft_profile(bundle, generator=generator)
    except generators.GenerationRefused as exc:
        print(f"生成が拒否された: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"生成結果を解釈できない: {exc}", file=sys.stderr)
        return 1

    problems = sector_analysis.review(profile, bundle)
    if problems:
        print(f"\n検査に通らなかったため保存しない（{len(problems)} 件）:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1

    path = storage.save_sector(profile)
    print(f"検査を通過。保存した: {path}")
    print(f"  フェーズ {profile['phase']['phase']} / 成長確率 {profile['growth']['probability']['value']} / "
          f"拡大規模 {profile['growth']['magnitude']['value']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
