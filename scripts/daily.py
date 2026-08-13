#!/usr/bin/env python3
"""日次サイクル: 収集 → 正規化 → シグナル集計 → 検査。

  python3 scripts/daily.py
  python3 scripts/daily.py --skip-collect      既存 raw だけで再実行
  python3 scripts/daily.py --as-of 2026-08-13T23:59:59Z

このスクリプトの目的は観測履歴を毎日 1 日ぶんずつ積むことにある。
Early Signal Detection は baseline 期間ぶんの観測履歴が無いと判定を保留し続けるため
(§10)、まず「毎日走り続けること」自体が要件になる。

途中でソース取得に失敗しても止めない（1 ソースの障害で当日の観測を失わない）。
検査で違反が出た場合のみ終了コード 1 を返す。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from future100 import timeutil  # noqa: E402


def step(title: str, argv: list[str]) -> int:
    print(f"\n=== {title} ===", flush=True)
    result = subprocess.run([sys.executable, *argv], cwd=ROOT)
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--as-of", help="シグナル集計の基準時刻。既定は現在時刻")
    parser.add_argument("--skip-collect", action="store_true", help="収集を行わず既存 raw のみ処理する")
    parser.add_argument("--window-days", type=int, default=7)
    args = parser.parse_args()

    as_of = args.as_of or timeutil.now_str()
    print(f"future100 daily cycle  as_of={as_of}")

    if not args.skip_collect:
        # 収集の失敗は当日の観測を減らすだけで、後続の処理は続ける
        step("1/4 collect", ["scripts/collect.py"])
    else:
        print("\n=== 1/4 collect (skipped) ===")

    if step("2/4 normalize", ["scripts/normalize.py"]) != 0:
        print("normalize failed", file=sys.stderr)
        return 1

    if step("3/4 detect signals", ["scripts/detect_signals.py", "--as-of", as_of,
                                   "--window-days", str(args.window_days)]) != 0:
        print("signal detection failed", file=sys.stderr)
        return 1

    code = step("4/4 validate", ["scripts/validate_data.py", "--as-of", as_of])
    print(f"\ndaily cycle finished with exit code {code}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
