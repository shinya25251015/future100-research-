"""Phase 4 サプライチェーン・ボトルネック・波及分析のテスト (§13-19)。

この層で守りたいのは、推論が事実の顔をしないこと。
  - 推論した因果には根拠と撤回条件が要る
  - 波が進むほど確信度が上がることはない
  - ボトルネックの受益者は類型で書き、銘柄を書かない

  python3 tests/test_chain_analysis.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from future100 import chain_analysis, invariants, sector_analysis  # noqa: E402

AS_OF = "2026-08-13T23:59:59Z"
EVENT_ID = "evt_20260801_aaaaaaaaaa"


def _bundle():
    evidence = sector_analysis.EvidenceBundle(
        sector_id="sec_power_grid", label="電力・送配電", as_of=AS_OF,
        window_start="2026-05-16",
        events=[{
            "event_id": EVENT_ID, "title": "変圧器の納期が延伸", "summary": "",
            "observed_at": "2026-08-01T00:00:00Z", "event_at": "2026-08-01T00:00:00Z",
            "max_reliability": "A", "sources": [{"source_id": "src_a", "reliability": "A"}],
        }],
    )
    profile = {
        "name": "電力・送配電",
        "views": {"independent": {"summary": "供給制約が先に効く", "divergence": "需要側ではなく供給側"}},
    }
    return chain_analysis.ChainBundle(
        sector_id="sec_power_grid", label="電力・送配電", as_of=AS_OF,
        profile=profile, evidence=evidence,
    )


def _wave_payload(**overrides):
    payload = {
        "trigger": {"description": "AI 計算需要の増加"},
        "links": [
            {"wave": 1, "from": "AI 計算需要", "to": "アクセラレータ出荷",
             "mechanism": "計算量の増加が調達数量に転写される", "claim_type": "observed",
             "causal_confidence": "high", "falsifier": "出荷が 2 四半期横ばいなら不成立",
             "basis": [EVENT_ID]},
            {"wave": 2, "from": "アクセラレータ出荷", "to": "送配電設備投資",
             "mechanism": "既存系統の余力を超える負荷は追加投資なしに接続できない",
             "claim_type": "inferred", "causal_confidence": "medium",
             "falsifier": "系統接続待ちが減少に転じること", "basis": [EVENT_ID]},
        ],
    }
    payload.update(overrides)
    return payload


def _generator(payload):
    return lambda prompt, schema: json.dumps(payload, ensure_ascii=False)


# --- 波及連鎖 (§17-19) ------------------------------------------------------

def test_wave_passes_review():
    bundle = _bundle()
    chain = chain_analysis.draft_wave(bundle, generator=_generator(_wave_payload()))
    assert chain_analysis.review_wave(chain, bundle) == []
    assert chain["links"][1]["claim_type"] == "inferred"


def test_inferred_link_without_basis_is_rejected():
    payload = _wave_payload()
    payload["links"][1]["basis"] = []
    bundle = _bundle()
    chain = chain_analysis.draft_wave(bundle, generator=_generator(payload))
    assert any("§17-19" in p for p in chain_analysis.review_wave(chain, bundle))


def test_inferred_link_without_falsifier_is_rejected():
    payload = _wave_payload()
    payload["links"][1]["falsifier"] = ""
    bundle = _bundle()
    chain = chain_analysis.draft_wave(bundle, generator=_generator(payload))
    assert any("§39" in p for p in chain_analysis.review_wave(chain, bundle))


def test_confidence_cannot_rise_with_wave_depth():
    """第二波の因果を第一波より強く主張するのは、推論を重ねるほど
    確からしくなると言っているのと同じ (§19)。"""
    payload = _wave_payload()
    payload["links"][0]["causal_confidence"] = "medium"
    payload["links"][1]["causal_confidence"] = "high"
    bundle = _bundle()
    chain = chain_analysis.draft_wave(bundle, generator=_generator(payload))
    assert any("§19" in p for p in chain_analysis.review_wave(chain, bundle))


def test_hallucinated_basis_is_rejected():
    payload = _wave_payload()
    payload["links"][1]["basis"] = ["evt_20260101_ffffffffff"]
    bundle = _bundle()
    chain = chain_analysis.draft_wave(bundle, generator=_generator(payload))
    assert any("evidence bundle" in p for p in chain_analysis.review_wave(chain, bundle))


# --- サプライチェーン (§13-16) ---------------------------------------------

def _supply_payload(**node_overrides):
    node = {
        "name": "大型電力変圧器", "tier_position": "equipment", "is_bottleneck": True,
        "trigger_condition": "年率 10% を超える需要増で不足する",
        "pricing_power": "high", "concentration": "high",
        "monetizer_type": "重電機器メーカーおよび電磁鋼板の供給者",
        "resolution_path": "増設に 3-5 年", "confidence": "medium", "basis": [EVENT_ID],
    }
    node.update(node_overrides)
    return {
        "nodes": [
            node,
            {"name": "電磁鋼板", "tier_position": "raw_material", "is_bottleneck": False,
             "trigger_condition": "", "pricing_power": "medium", "concentration": "medium",
             "monetizer_type": "", "resolution_path": "", "confidence": "low", "basis": []},
        ],
        "edges": [{"from": "電磁鋼板", "to": "大型電力変圧器", "flow": "素材",
                   "substitutability": "low", "confidence": "medium"}],
    }


def test_supply_chain_passes_review():
    bundle = _bundle()
    chain_map = chain_analysis.draft_supply_chain(bundle, generator=_generator(_supply_payload()))
    assert chain_analysis.review_supply_chain(chain_map, bundle) == []
    assert chain_map["nodes"][0]["bottleneck"]["is_bottleneck"] is True


def test_bottleneck_without_monetizer_is_rejected():
    """不足を特定しても、誰が回収するかを書かなければ §15 を満たさない。"""
    bundle = _bundle()
    chain_map = chain_analysis.draft_supply_chain(
        bundle, generator=_generator(_supply_payload(monetizer_type=""))
    )
    assert any("§15" in p for p in chain_analysis.review_supply_chain(chain_map, bundle))


def test_bottleneck_without_trigger_condition_is_rejected():
    bundle = _bundle()
    chain_map = chain_analysis.draft_supply_chain(
        bundle, generator=_generator(_supply_payload(trigger_condition=""))
    )
    assert any("§14" in p for p in chain_analysis.review_supply_chain(chain_map, bundle))


def test_ticker_mention_is_rejected():
    """受益者は類型で書く。個別銘柄は書かせない (§1, §44-46)。"""
    bundle = _bundle()
    chain_map = chain_analysis.draft_supply_chain(
        bundle, generator=_generator(_supply_payload(monetizer_type="日立 (TYO: 6501)"))
    )
    assert any("§1/§44-46" in p for p in chain_analysis.review_supply_chain(chain_map, bundle))


def test_edges_must_reference_existing_nodes():
    payload = _supply_payload()
    payload["edges"][0]["from"] = "存在しないノード"
    bundle = _bundle()
    chain_map = chain_analysis.draft_supply_chain(bundle, generator=_generator(payload))
    # 未知の名前からも node_id は作られるが、nodes に無いので検査で落ちる
    assert any("§16" in p for p in invariants.check_supply_chain(chain_map))


def test_missing_profile_blocks_analysis():
    """構造評価の済んでいないセクターでは連鎖分析を始めない。"""
    try:
        chain_analysis.build_bundle("sec_does_not_exist", as_of=AS_OF)
    except (FileNotFoundError, KeyError) as exc:
        assert "sec_does_not_exist" in str(exc)
        return
    raise AssertionError("プロファイルが無ければ分析を始めない")


def test_generated_documents_match_their_schemas():
    try:
        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource
    except ImportError:
        print("    (skipped: jsonschema not installed)")
        return

    registry = Registry()
    for path in (ROOT / "schemas").glob("*.schema.json"):
        registry = registry.with_resource(path.name, Resource.from_contents(json.loads(path.read_text(encoding="utf-8"))))

    bundle = _bundle()
    cases = [
        (chain_analysis.draft_wave(bundle, generator=_generator(_wave_payload())), "wave"),
        (chain_analysis.draft_supply_chain(bundle, generator=_generator(_supply_payload())), "supply_chain"),
    ]
    for payload, schema_name in cases:
        schema = json.loads((ROOT / f"schemas/{schema_name}.schema.json").read_text(encoding="utf-8"))
        errors = list(Draft202012Validator(schema, registry=registry).iter_errors(payload))
        assert not errors, f"{schema_name}: {errors[0].message}"


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
