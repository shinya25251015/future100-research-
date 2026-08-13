"""Phase 3-2 セクター構造評価のテスト (§8-9, §20-34)。

この層で LLM を使うため、検査すべきは「生成させる前」と「生成させた後」の両方になる。
  - 前: 渡す根拠が as_of 以前に限られ、重複を含まないこと (§37-38)
  - 後: 存在しない根拠を引いた生成物を弾けること (§35-38)

  python3 tests/test_sector_analysis.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from future100 import sector_analysis  # noqa: E402

AS_OF = "2026-08-13T23:59:59Z"
SECTOR = "sec_power_grid"


def _event(event_id: str, *, title: str, observed_at: str, event_at: str,
           primary: bool = True, reliability: str = "A") -> dict:
    return {
        "event_id": event_id,
        "is_cluster_primary": primary,
        "title": title,
        "summary": title,
        "observed_at": observed_at,
        "event_at": event_at,
        "max_reliability": reliability,
        "sector_links": [{"sector_id": SECTOR, "relation": "unclassified", "confidence": "low"}],
        "sources": [{"source_id": "src_a", "reliability": reliability, "observed_at": observed_at}],
    }


VISIBLE = _event("evt_20260801_aaaaaaaaaa", title="変圧器の納期が延伸",
                 observed_at="2026-08-01T00:00:00Z", event_at="2026-08-01T00:00:00Z")
FUTURE = _event("evt_20260901_bbbbbbbbbb", title="将来の観測",
                observed_at="2026-09-01T00:00:00Z", event_at="2026-09-01T00:00:00Z")
DUPLICATE = _event("evt_20260801_cccccccccc", title="変圧器の納期が延伸（転載）",
                   observed_at="2026-08-01T00:00:00Z", event_at="2026-08-01T00:00:00Z", primary=False)


def _bundle(events=None):
    return sector_analysis.build_bundle(
        SECTOR, as_of=AS_OF, events=list(events if events is not None else [VISIBLE])
    )


# --- 入力の組み立て -------------------------------------------------------

def test_bundle_excludes_future_observations():
    """分析時点より後に観測した情報を根拠に含めない (§37)。"""
    bundle = _bundle([VISIBLE, FUTURE])
    assert bundle.event_ids == {VISIBLE["event_id"]}


def test_bundle_excludes_duplicate_events():
    """重複を根拠に混ぜると、同じ出来事が複数の証拠に見える (§38)。"""
    bundle = _bundle([VISIBLE, DUPLICATE])
    assert bundle.event_ids == {VISIBLE["event_id"]}


def test_bundle_respects_the_window():
    old = _event("evt_20250101_dddddddddd", title="古い出来事",
                 observed_at="2025-01-01T00:00:00Z", event_at="2025-01-01T00:00:00Z")
    assert old["event_id"] not in _bundle([VISIBLE, old]).event_ids


def test_prompts_are_separated_and_constrained():
    """コンセンサスと独自見解は別々の呼び出しにする (§34)。"""
    bundle = _bundle()
    consensus = sector_analysis.render_prompt(bundle, "consensus")
    independent = sector_analysis.render_prompt(bundle, "independent")

    assert "一般に語られている見方" in consensus
    assert "あなた自身の判断は書かない" in consensus
    assert "独自の見解" in independent
    assert "撤回" in independent, "撤回条件の要求 (§39)"
    for prompt in (consensus, independent):
        assert VISIBLE["event_id"] in prompt, "根拠は id つきで渡す"
        assert "銘柄" in prompt, "銘柄に言及させない指示 (§1, §44-46)"


# --- 生成物の検証 ---------------------------------------------------------

def _valid_analysis(basis_id: str) -> dict:
    return {
        "independent": {"summary": "供給制約が先に効く", "divergence": "需要側ではなく供給側",
                        "confidence": "medium", "basis": [basis_id]},
        "phase": {"phase": "scaling", "rationale": "更新需要が構造的に増える"},
        "growth": {
            "probability": {"value": 0.7, "confidence": "medium", "rationale": "複数経路"},
            "magnitude": {"value": "medium", "confidence": "low", "rationale": "規制下の資産"},
        },
        "scenarios": [
            {"scenario": s, "narrative": "…", "falsifier": "リードタイムが 18 か月を下回ったら撤回",
             "projections": [{"year": 2030, "market_size": {"amount": 0, "currency": "USD", "unit": "billion"}}]}
            for s in ("bear", "base", "bull")
        ],
    }


def _generator(basis_id: str):
    def generate(prompt: str, schema: dict) -> str:
        if "一般に語られている見方" in prompt:
            return json.dumps({"summary": "発電側が受益者とされる", "evidence": []})
        return json.dumps(_valid_analysis(basis_id))

    return generate


def test_draft_profile_passes_review():
    bundle = _bundle()
    profile = sector_analysis.draft_profile(bundle, generator=_generator(VISIBLE["event_id"]))
    assert sector_analysis.review(profile, bundle) == []
    assert profile["views"]["consensus"]["summary"]
    assert profile["views"]["independent"]["divergence"]
    assert profile["market_size"]["tam"]["amount"] == 0, "推計していない値を数字で埋めない"


def test_hallucinated_evidence_is_rejected():
    """存在しない event_id を引いた推論を通すと、以降の分析が架空の観測の上に積み上がる。"""
    bundle = _bundle()
    profile = sector_analysis.draft_profile(bundle, generator=_generator("evt_20260101_ffffffffff"))
    problems = sector_analysis.review(profile, bundle)
    assert any("evidence bundle" in p for p in problems), problems


def test_missing_falsifier_is_rejected():
    bundle = _bundle()

    def generate(prompt: str, schema: dict) -> str:
        if "一般に語られている見方" in prompt:
            return json.dumps({"summary": "…", "evidence": []})
        analysis = _valid_analysis(VISIBLE["event_id"])
        del analysis["scenarios"][1]["falsifier"]
        return json.dumps(analysis)

    profile = sector_analysis.draft_profile(bundle, generator=generate)
    assert any("§39" in p for p in sector_analysis.review(profile, bundle))


def test_non_json_output_is_rejected():
    bundle = _bundle()
    try:
        sector_analysis.draft_profile(bundle, generator=lambda prompt, schema: "承知しました。以下が分析です。")
    except ValueError as exc:
        assert "JSON" in str(exc)
        return
    raise AssertionError("non-JSON generator output must be rejected")


def test_generated_profile_matches_the_schema():
    try:
        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource
    except ImportError:
        print("    (skipped: jsonschema not installed)")
        return

    registry = Registry()
    for path in (ROOT / "schemas").glob("*.schema.json"):
        registry = registry.with_resource(path.name, Resource.from_contents(json.loads(path.read_text(encoding="utf-8"))))
    schema = json.loads((ROOT / "schemas/sector.schema.json").read_text(encoding="utf-8"))

    bundle = _bundle()
    profile = sector_analysis.draft_profile(bundle, generator=_generator(VISIBLE["event_id"]))
    errors = list(Draft202012Validator(schema, registry=registry).iter_errors(profile))
    assert not errors, "; ".join(f"{'/'.join(str(p) for p in e.path)}: {e.message}" for e in errors[:3])


def test_output_schemas_obey_structured_output_limits():
    """構造化出力は additionalProperties:false と全プロパティの required を要求し、
    数値範囲などの制約は使えない。スキーマ側で形を保証できないと、検証が後段に全部寄る。"""

    def walk(node, path="root"):
        if isinstance(node, dict) and node.get("type") == "object":
            assert node.get("additionalProperties") is False, f"{path}: additionalProperties"
            assert set(node.get("required", [])) == set(node.get("properties", {})), f"{path}: required"
            for key, value in node.get("properties", {}).items():
                walk(value, f"{path}.{key}")
        if isinstance(node, dict) and node.get("type") == "array":
            walk(node.get("items", {}), f"{path}[]")
        if isinstance(node, dict):
            for unsupported in ("minimum", "maximum", "minLength", "maxLength", "minItems"):
                assert unsupported not in node, f"{path}: {unsupported} は構造化出力で使えない"

    walk(sector_analysis.CONSENSUS_SCHEMA, "consensus")
    walk(sector_analysis.ANALYSIS_SCHEMA, "analysis")


def test_generator_reports_missing_credentials_clearly():
    """鍵が無い環境では、握りつぶさず対処法を添えて失敗する。"""
    import os

    from future100 import generators

    saved = {k: os.environ.pop(k) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN") if k in os.environ}
    try:
        generators.anthropic_generator()
    except generators.GeneratorUnavailable as exc:
        assert "ANTHROPIC_API_KEY" in str(exc) or "anthropic SDK" in str(exc)
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"想定外の例外: {type(exc).__name__}: {exc}") from exc
    else:
        raise AssertionError("認証情報が無い場合は GeneratorUnavailable を送出すべき")
    finally:
        os.environ.update(saved)


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
