#!/usr/bin/env python3
"""data/ の内容を JSON Schema と仕様書の不変条件で検査する。

  python3 scripts/validate_data.py
  python3 scripts/validate_data.py --as-of 2026-08-13T00:00:00Z

JSON Schema 検証は jsonschema パッケージがある場合のみ実行する（未導入なら明示的に skip と表示）。
仕様書の不変条件（§17-19 根拠必須 / §37 未来情報禁止 / §39 撤回条件 / §44-46 ランキング禁止）は
外部依存なしで常に検査する。違反があれば終了コード 1。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from future100 import invariants, storage  # noqa: E402

SCHEMA_BY_KIND = {
    "event": "event.schema.json",
    "sector": "sector.schema.json",
    "prediction": "prediction.schema.json",
    "daily_report": "daily_report.schema.json",
    "raw": "raw_document.schema.json",
    "signal": "signal.schema.json",
}


def load_validator(kind: str):
    """jsonschema があれば検証器を返す。無ければ None。"""
    try:
        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource
    except ImportError:
        return None

    registry = Registry()
    for path in storage.SCHEMAS_DIR.glob("*.schema.json"):
        registry = registry.with_resource(path.name, Resource.from_contents(storage.read_json(path)))
    schema = storage.read_json(storage.SCHEMAS_DIR / SCHEMA_BY_KIND[kind])
    return Draft202012Validator(schema, registry=registry)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--as-of", help="この時刻より後に観測されたデータを違反として扱う (§37)")
    args = parser.parse_args()

    problems: list[str] = []
    counts: dict[str, int] = {}

    def run(kind: str, records: list[tuple[str, dict]]) -> None:
        validator = load_validator(kind)
        counts[kind] = len(records)
        for label, record in records:
            if validator is not None:
                for error in validator.iter_errors(record):
                    problems.append(f"[schema/{kind}] {label}: {'/'.join(str(p) for p in error.path)}: {error.message}")
            check = invariants.CHECKS.get(kind)
            if check is None:
                continue
            kwargs = {"as_of": args.as_of} if kind == "event" and args.as_of else {}
            for issue in check(record, **kwargs):
                problems.append(f"[invariant/{kind}] {label}: {issue}")

    events = [(e["event_id"], e) for e in storage.iter_events()]
    run("event", events)
    run("sector", [(p.stem, storage.read_json(p)) for p in sorted(storage.SECTORS_DIR.glob("sec_*.json"))])
    run("prediction", [(p.stem, storage.read_json(p)) for p in sorted((storage.DATA / "predictions").glob("prd_*.json"))])
    run("daily_report", [(p.stem, storage.read_json(p)) for p in sorted((storage.REPORTS_DIR / "daily").glob("rpt_*.json"))])

    if load_validator("event") is None:
        print("note: jsonschema not installed — schema validation skipped, invariants still checked")

    summary = "  ".join(f"{kind}={count}" for kind, count in counts.items())
    print(f"checked: {summary}")
    if problems:
        print(f"\n{len(problems)} problem(s):", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print("no problems found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
