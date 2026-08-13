#!/usr/bin/env python3
"""Phase 2: data/raw/ の未処理スナップショットを Event に正規化する (§38 重複統合込み)。

  python3 scripts/normalize.py
  python3 scripts/normalize.py --limit 50
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from future100 import normalize  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, help="処理する raw の最大件数")
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="data/events と重複索引を捨てて raw から全件作り直す（正規化規則を変えたとき）",
    )
    args = parser.parse_args()

    stats = normalize.rebuild() if args.rebuild else normalize.run(limit=args.limit)
    print(
        f"processed={stats.processed}  new_events={stats.created}  "
        f"duplicates_merged={stats.duplicates}  skipped={stats.skipped}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
