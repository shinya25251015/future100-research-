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
    failures: list[tuple[str, str]] = []

    for source in sources:
        result = collect.base.collect(source)
        if result.error:
            failures.append((source["source_id"], result.error))
            print(f"  FAIL {source['source_id']}: {result.error}")
            continue

        new = 0
        for document in result.documents:
            total_seen += 1
            if args.dry_run:
                continue
            if storage.save_raw(document) is not None:
                new += 1
        total_new += new
        label = "fetched" if args.dry_run else "new"
        count = len(result.documents) if args.dry_run else new
        print(f"  OK   {source['source_id']}: {count} {label} / {len(result.documents)} items")

    print(f"\nstarted_at={started}  sources={len(sources)}  items={total_seen}  new_raw={total_new}  failed={len(failures)}")
    if failures:
        print("failed sources:", file=sys.stderr)
        for source_id, reason in failures:
            print(f"  {source_id}: {reason}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
