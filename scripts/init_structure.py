#!/usr/bin/env python3
"""仕様書 §57 に基づくディレクトリ構造の自動生成（冪等）。

  python3 scripts/init_structure.py [--check]

--check を付けると生成せず、不足しているディレクトリの一覧のみを出力し、
不足があれば終了コード 1 を返す（CI 用）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (相対パス, 用途) — 用途は各ディレクトリの README.md に書き出される
DIRECTORIES: list[tuple[str, str]] = [
    ("config", "情報ソース定義・監視セクター定義・実行設定 (§11, §48-51)"),
    ("data/raw", "取得した一次データの不変スナップショット。加工・上書き禁止 (§48-51)"),
    ("data/events", "正規化済みイベント。日次 JSONL (§48-51, §35-38)"),
    ("data/signals", "Early Signal Detection の弱いシグナル (§10)"),
    ("data/sectors", "セクタープロファイル。1 セクター 1 ファイル (§11-12, §20-32)"),
    ("data/supply_chain", "サプライチェーンマップとボトルネック (§13-16)"),
    ("data/waves", "第二波・第三波の因果連鎖 (§17-19)"),
    ("data/predictions", "予測レコード。発行後は不変 (§39-43)"),
    ("data/index", "重複判定・クラスタ索引などの派生インデックス (§38)"),
    ("reports/daily", "日次 Global Sector Report (§47)"),
    ("reports/monthly", "月次の予測誤差・自己改善レポート (§40-43)"),
    ("reports/templates", "レポート雛形"),
    ("backtest/predictions", "検証対象として凍結した予測のスナップショット (§41)"),
    ("backtest/outcomes", "実績データ (§42)"),
    ("backtest/error_analysis", "誤差の原因分析と改善履歴 (§43)"),
    ("schemas", "JSON Schema (draft 2020-12) による正準データモデル (§53)"),
    ("src/future100", "実装本体"),
    ("src/future100/collect", "Phase 2 情報収集コレクタ"),
    ("scripts", "運用スクリプト"),
    ("tests", "テスト"),
    ("docs", "設計文書・ロードマップ"),
]

# README.md を置かない（＝.gitkeep のみ）ディレクトリ
_CODE_DIRS = {"src/future100", "src/future100/collect", "scripts", "tests", "docs", "schemas"}


def missing(root: Path) -> list[str]:
    return [rel for rel, _ in DIRECTORIES if not (root / rel).is_dir()]


def create(root: Path) -> list[str]:
    created: list[str] = []
    for rel, purpose in DIRECTORIES:
        path = root / rel
        if not path.is_dir():
            path.mkdir(parents=True, exist_ok=True)
            created.append(rel)
        if rel in _CODE_DIRS:
            # 中身が入ったディレクトリに空ファイルを残さない
            keep = path / ".gitkeep"
            if not any(p for p in path.iterdir() if p.name != ".gitkeep"):
                keep.touch(exist_ok=True)
            elif keep.exists():
                keep.unlink()
            continue
        readme = path / "README.md"
        if not readme.exists():
            readme.write_text(f"# {rel}\n\n{purpose}\n", encoding="utf-8")
    return created


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="生成せず不足のみ報告する")
    args = parser.parse_args()

    if args.check:
        gaps = missing(ROOT)
        for rel in gaps:
            print(f"missing: {rel}")
        if gaps:
            print(f"{len(gaps)} directories missing", file=sys.stderr)
            return 1
        print("all directories present")
        return 0

    created = create(ROOT)
    for rel in created:
        print(f"created: {rel}")
    print(f"{len(created)} created, {len(DIRECTORIES) - len(created)} already present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
