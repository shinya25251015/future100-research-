#!/usr/bin/env python3
"""過去分の取り込み (§10, §37)。

  python3 scripts/backfill.py --days 35 --dry-run    取れる件数だけ確認する
  python3 scripts/backfill.py --days 35              取り込む
  python3 scripts/backfill.py --days 90 --source src_arxiv_cs_ai

日次収集は今日から先しか積み上がらないが、日付範囲を指定できる API は過去の同じ
データを今からでも取りに行ける。Early Signal Detection の baseline を待たずに作るための
コマンド。RSS しか出していないソースは原理的に遡れないので対象外になる。

observed_at は取り込んだ今の時刻であり、発行日には書き換えない。したがって今日より前の
as_of で再生したときこの過去分は見えない（過去時点の再現を壊さないため, §37）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from future100 import backfill, config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=35,
                        help="遡る日数。既定 35（直近 7 日 + baseline 28 日）")
    parser.add_argument("--source", action="append", help="対象 source_id（複数指定可）")
    parser.add_argument("--dry-run", action="store_true", help="取得はするが保存しない")
    parser.add_argument("--delay", type=float, help="区間ごとの待機秒数（既定はソース定義）")
    args = parser.parse_args()

    sources = [s for s in config.load_sources(enabled_only=False) if backfill.supported(s)]
    if args.source:
        wanted = set(args.source)
        unsupported = wanted - {s["source_id"] for s in sources}
        sources = [s for s in sources if s["source_id"] in wanted]
        for source_id in sorted(unsupported):
            print(f"  SKIP {source_id}: 日付範囲での取得に対応していない（RSS のみのソースは遡れない）",
                  file=sys.stderr)
    if not sources:
        print("過去分を取れるソースが無い", file=sys.stderr)
        return 2

    total_stored = total_fetched = 0
    failures = 0
    for source in sources:
        print(f"\n{source['source_id']} ({source['name']}): 過去 {args.days} 日")
        result = backfill.run(source, days=args.days, delay_seconds=args.delay, dry_run=args.dry_run)
        for chunk in result.chunks:
            if chunk.error:
                print(f"  FAIL {chunk.start}..{chunk.end}: {chunk.error}", file=sys.stderr)
            else:
                print(f"  OK   {chunk.start}..{chunk.end}: {chunk.stored} new / {chunk.fetched} items")
        failures += sum(1 for c in result.chunks if c.error)
        total_stored += result.stored
        total_fetched += result.fetched

        span = result.covered_span()
        print(f"  取り込めた範囲: {span[0]}..{span[1]}" if span else "  取り込めた範囲: なし")

    print(f"\nfetched={total_fetched}  stored={total_stored}  failed_chunks={failures}")
    if not args.dry_run and total_stored:
        print("次に: python3 scripts/normalize.py && python3 scripts/detect_signals.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
