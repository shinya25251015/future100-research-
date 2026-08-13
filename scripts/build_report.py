#!/usr/bin/env python3
"""Phase 6: 日次 Global Sector Report を生成する (§47)。

  python3 scripts/build_report.py
  python3 scripts/build_report.py --as-of 2026-08-13T23:59:59Z
  python3 scripts/build_report.py --stdout        保存せず標準出力に表示

16 項目すべてを毎日出力する。該当が無い項目も「本日該当なし」と明記して省略しない。
§44-46 の禁止事項（銘柄推奨・ランキング）を検出した場合は保存せず終了コード 1 を返す。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from future100 import invariants, report as report_module, timeutil  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--as-of", help="この時刻までに観測したデータのみを使う (§37)")
    parser.add_argument("--date", help="レポート対象日 (既定: as-of の日付)")
    parser.add_argument("--stdout", action="store_true", help="保存せず Markdown を表示する")
    args = parser.parse_args()

    as_of = args.as_of or timeutil.now_str()
    try:
        report = report_module.build_daily_report(as_of=as_of, report_date=args.date)
    except report_module.ProhibitedOutput as exc:
        print(f"§44-46 違反のためレポートを生成しなかった: {exc}", file=sys.stderr)
        return 1

    problems = invariants.check_daily_report(report)
    if problems:
        print("不変条件に違反したためレポートを保存しない:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1

    if args.stdout:
        print(report_module.render_markdown(report))
        return 0

    json_path, md_path = report_module.save(report)
    coverage = report["coverage"]
    print(f"{report['report_id']}: 観測 {coverage['events_ingested']} 件 / "
          f"重複統合後 {coverage['events_after_dedup']} 件 / 16 項目")
    print(f"  {json_path.relative_to(Path.cwd()) if json_path.is_relative_to(Path.cwd()) else json_path}")
    print(f"  {md_path.relative_to(Path.cwd()) if md_path.is_relative_to(Path.cwd()) else md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
