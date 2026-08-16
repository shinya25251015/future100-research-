#!/usr/bin/env python3
"""Phase 4: サプライチェーン・ボトルネック・波及分析 (§13-19)。

  python3 scripts/analyze_chain.py --sector sec_power_grid --show-prompt
  python3 scripts/analyze_chain.py --sector sec_power_grid --generate

セクタープロファイル (§20-34) が前提。連鎖分析は「そのセクターで何が起きるか」の
上に積む推論なので、構造評価の済んでいないセクターでは行わない。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from future100 import chain_analysis, timeutil  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sector", required=True)
    parser.add_argument("--as-of", help="分析時点。既定は現在時刻 (§37)")
    parser.add_argument("--kind", choices=["wave", "supply_chain", "both"], default="both")
    parser.add_argument("--show-prompt", action="store_true")
    parser.add_argument("--generate", action="store_true", help="Claude API で生成する（要 ANTHROPIC_API_KEY）")
    parser.add_argument("--replay", help="保存済み生成結果 {\"wave\": {...}, \"supply_chain\": {...}} を読み直して検証・保存する")
    parser.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    args = parser.parse_args()

    as_of = args.as_of or timeutil.now_str()
    try:
        bundle = chain_analysis.build_bundle(args.sector, as_of=as_of)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1

    kinds = ["wave", "supply_chain"] if args.kind == "both" else [args.kind]
    print(f"{bundle.sector_id} ({bundle.label}) 根拠 {len(bundle.event_ids)} 件  as_of={as_of}")

    if args.show_prompt:
        for kind in kinds:
            print(f"\n{'=' * 30} prompt: {kind} {'=' * 30}")
            print(chain_analysis.render_prompt(bundle, kind))

    if not (args.generate or args.replay):
        print("\n--generate で連鎖分析を生成する（要 ANTHROPIC_API_KEY）。")
        print("--replay で、別の場所で生成した結果を検証して取り込む（API を呼ばない）。")
        return 0

    from future100 import generators  # noqa: PLC0415

    try:
        generator = (
            generators.replay_generator(args.replay, keys=("wave", "supply_chain"),
                                        marker="波及連鎖を分析せよ", first_key="wave")
            if args.replay
            else generators.anthropic_generator(effort=args.effort)
        )
    except generators.GeneratorUnavailable as exc:
        print(f"\n生成器を用意できない: {exc}", file=sys.stderr)
        return 1

    failed = False
    for kind in kinds:
        print(f"\n生成中: {kind} (effort={args.effort}) ...")
        try:
            if kind == "wave":
                document = chain_analysis.draft_wave(bundle, generator=generator)
                problems = chain_analysis.review_wave(document, bundle)
                save = chain_analysis.save_wave
            else:
                document = chain_analysis.draft_supply_chain(bundle, generator=generator)
                problems = chain_analysis.review_supply_chain(document, bundle)
                save = chain_analysis.save_supply_chain
        except (generators.GenerationRefused, ValueError) as exc:
            print(f"  生成に失敗: {exc}", file=sys.stderr)
            failed = True
            continue

        if problems:
            failed = True
            print(f"  検査に通らなかったため保存しない（{len(problems)} 件）:", file=sys.stderr)
            for problem in problems:
                print(f"    {problem}", file=sys.stderr)
            continue
        print(f"  検査を通過。保存した: {save(document)}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
