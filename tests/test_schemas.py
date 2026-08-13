"""schemas/ 自体と、schemas/examples/ の例が整合していることを検証する。

jsonschema が未導入の環境ではスキーマ検証を skip し、JSON として読めることだけ確認する。

  python3 tests/test_schemas.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

SCHEMAS = ROOT / "schemas"

# 例ファイル → 対応スキーマ
CASES = {
    "examples/example_sector.json": "sector.schema.json",
    "examples/example_wave.json": "wave.schema.json",
    "examples/example_prediction.json": "prediction.schema.json",
    "../reports/templates/daily_report_template.json": "daily_report.schema.json",
    "../config/sources.json": None,  # 個々の source を source.schema.json で検証する
}


def _validator(schema_name: str):
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    registry = Registry()
    for path in SCHEMAS.glob("*.schema.json"):
        registry = registry.with_resource(path.name, Resource.from_contents(json.loads(path.read_text(encoding="utf-8"))))
    schema = json.loads((SCHEMAS / schema_name).read_text(encoding="utf-8"))
    return Draft202012Validator(schema, registry=registry)


def _has_jsonschema() -> bool:
    try:
        import jsonschema  # noqa: F401
        import referencing  # noqa: F401
    except ImportError:
        return False
    return True


def test_all_schemas_are_valid_json():
    for path in SCHEMAS.glob("*.schema.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["$schema"].endswith("2020-12/schema"), path.name
        assert payload["$id"].endswith(path.name), path.name


def test_examples_match_their_schemas():
    if not _has_jsonschema():
        print("    (skipped: jsonschema not installed)")
        return
    for rel, schema_name in CASES.items():
        if schema_name is None:
            continue
        record = json.loads((SCHEMAS / rel).read_text(encoding="utf-8"))
        errors = sorted(_validator(schema_name).iter_errors(record), key=lambda e: list(e.path))
        assert not errors, f"{rel}: " + "; ".join(f"{'/'.join(str(p) for p in e.path)}: {e.message}" for e in errors[:5])


def test_source_registry_matches_source_schema():
    payload = json.loads((ROOT / "config/sources.json").read_text(encoding="utf-8"))
    source_ids = [s["source_id"] for s in payload["sources"]]
    assert len(source_ids) == len(set(source_ids)), "duplicate source_id in registry"
    if not _has_jsonschema():
        print("    (schema check skipped: jsonschema not installed)")
        return
    validator = _validator("source.schema.json")
    for source in payload["sources"]:
        errors = list(validator.iter_errors(source))
        assert not errors, f"{source['source_id']}: {errors[0].message}"


def test_known_sectors_have_unique_ids():
    payload = json.loads((ROOT / "config/known_sectors.json").read_text(encoding="utf-8"))
    ids_ = [s["sector_id"] for s in payload["sectors"]]
    assert len(ids_) == len(set(ids_))
    assert all(i.startswith("sec_") for i in ids_)


def main() -> int:
    failures = 0
    for name, func in sorted(globals().items()):
        if not name.startswith("test_") or not callable(func):
            continue
        try:
            func()
            print(f"  PASS {name}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  FAIL {name}: {type(exc).__name__}: {exc}")
    print("all tests passed" if not failures else f"{failures} test(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
