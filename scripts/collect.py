#!/usr/bin/env python3
"""Phase 2: 登録ソースを巡回して data/raw/ にスナップショットを保存する。

  python3 scripts/collect.py                 有効な全ソースを取得
  python3 scripts/collect.py --dry-run       取得はするが保存しない（到達性確認）
  python3 scripts/collect.py --source src_jp_meti
  python3 scripts/collect.py --tier tier1

失敗したソースは標準エラーに一覧表示し、終了コードには影響させない
（1 ソースの障害で日次収集を止めないため）。到達性の恒久的な失敗は
config/sources.json の enabled を false に落として運用する。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from future100 import collect, config, storage, timeutil  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", action="append", help="取得する source_id（複数指定可）")
    parser.add_argument("--tier", action="append", choices=["tier1", "tier2", "tier3", "tier4"])
    parser.add_argument("--dry-run", action="store_true", help="保存せず件数だけ報告する")
    parser.add_argument("--all", action="store_true", help="enabled=false のソースも対象にする")
    parser.add_argument("--force", action="store_true", help="poll_interval_minutes を無視して取りに行く")
    args = parser.parse_args()

    sources = config.load_sources(enabled_only=not args.all, tiers=set(args.tier) if args.tier else None)
    if args.source:
        wanted = set(args.source)
        sources = [s for s in sources if s["source_id"] in wanted]
    if not sources:
        print("no sources matched", file=sys.stderr)
        return 2

    started = timeutil.now_str()
    total_new = total_seen = 0
    skipped = 0
    failures: list[tuple[str, str]] = []
    # 取得間隔は source_id を明示したときと --force のときは見ない（手元で試すため）。
    last_polls = storage.load_index("last_poll")
    honor_interval = not args.source and not args.force

    for source in sources:
        if honor_interval and not config.is_due(source, last_polls.get(source["source_id"]), now=started):
            skipped += 1
            continue

        result = collect.base.collect(source)
        if not result.error and not args.dry_run:
            last_polls[source["source_id"]] = started
        if result.error:
            failures.append((source["source_id"], result.error))
            print(f"  FAIL {source['source_id']}: {result.error}")
            continue

        total_seen += len(result.documents)
        new = 0 if args.dry_run else storage.save_raw_batch(result.documents)
        total_new += new
        label = "fetched" if args.dry_run else "new"
        count = len(result.documents) if args.dry_run else new
        print(f"  OK   {source['source_id']}: {count} {label} / {len(result.documents)} items")

    if not args.dry_run:
        storage.save_index("last_poll", last_polls)

    print(
        f"\nstarted_at={started}  sources={len(sources)}  items={total_seen}  "
        f"new_raw={total_new}  skipped={skipped}  failed={len(failures)}"
    )
    if failures:
        print("failed sources:", file=sys.stderr)
        for source_id, reason in failures:
            print(f"  {source_id}: {reason}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
