"""Phase 4: サプライチェーン・ボトルネック・波及分析 (§13-19)。

Phase 3-2 と同じ構成（入力の組み立て → 呼び出し → 検証）を、連鎖の分析に適用する。
この層に固有の規律は 3 つある。

  1. **観察と推論を分ける。** 各波及段は claim_type が observed か inferred か。
     inferred には根拠と撤回条件を要求する (§17-19)。
  2. **波が進むほど確信度は上がらない。** 第三波の因果を第一波より強く主張するのは、
     推論を重ねるほど確からしくなると言っているのと同じ。検査で弾く (§19)。
  3. **ボトルネックの受益者は類型で書く。** 個別銘柄を書かせない (§1, §44-46)。

セクタープロファイル (§20-34) を入力に取るため、Phase 3-2 の成果物が前提になる。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import ids, invariants, sector_analysis, storage, timeutil

Generator = sector_analysis.Generator

_CONFIDENCE = {"type": "string", "enum": ["high", "medium", "low"]}
_LEVEL = {"type": "string", "enum": ["very_low", "low", "medium", "high", "very_high"]}

WAVE_SCHEMA = {
    "type": "object",
    "properties": {
        "trigger": {
            "type": "object",
            "properties": {"description": {"type": "string"}},
            "required": ["description"],
            "additionalProperties": False,
        },
        "links": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "wave": {"type": "integer", "enum": [1, 2, 3]},
                    "from": {"type": "string"},
                    "to": {"type": "string"},
                    "mechanism": {"type": "string"},
                    "claim_type": {"type": "string", "enum": ["observed", "inferred"]},
                    "causal_confidence": _CONFIDENCE,
                    "falsifier": {"type": "string"},
                    "basis": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "wave", "from", "to", "mechanism", "claim_type", "causal_confidence",
                    "falsifier", "basis",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["trigger", "links"],
    "additionalProperties": False,
}

SUPPLY_CHAIN_SCHEMA = {
    "type": "object",
    "properties": {
        "nodes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "tier_position": {
                        "type": "string",
                        "enum": [
                            "raw_material", "processing", "component", "equipment", "subsystem",
                            "integration", "infrastructure", "service", "end_demand",
                        ],
                    },
                    "is_bottleneck": {"type": "boolean"},
                    "trigger_condition": {"type": "string"},
                    "pricing_power": _LEVEL,
                    "concentration": _LEVEL,
                    "monetizer_type": {"type": "string"},
                    "resolution_path": {"type": "string"},
                    "confidence": _CONFIDENCE,
                    "basis": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "name", "tier_position", "is_bottleneck", "trigger_condition",
                    "pricing_power", "concentration", "monetizer_type", "resolution_path",
                    "confidence", "basis",
                ],
                "additionalProperties": False,
            },
        },
        "edges": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "from": {"type": "string"},
                    "to": {"type": "string"},
                    "flow": {"type": "string"},
                    "substitutability": _LEVEL,
                    "confidence": _CONFIDENCE,
                },
                "required": ["from", "to", "flow", "substitutability", "confidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["nodes", "edges"],
    "additionalProperties": False,
}

_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}
# 個別銘柄を書かせないための検査。ティッカー表記と証券コードを弾く (§1, §44-46)。
_TICKER_HINTS = ("NASDAQ:", "NYSE:", "TYO:", "証券コード", "$AAPL")


@dataclass
class ChainBundle:
    """連鎖分析の入力。セクタープロファイルと、その根拠イベント。"""

    sector_id: str
    label: str
    as_of: str
    profile: dict
    evidence: sector_analysis.EvidenceBundle = field(repr=False, default=None)

    @property
    def event_ids(self) -> set[str]:
        return self.evidence.event_ids if self.evidence else set()


def build_bundle(sector_id: str, *, as_of: str | None = None, **kwargs) -> ChainBundle:
    """セクタープロファイルと根拠を集める。プロファイルが無ければ分析しない。

    連鎖分析は「そのセクターで何が起きるか」の上に積む推論なので、
    構造評価 (§20-34) が済んでいないセクターでは行わない。
    """
    as_of = as_of or timeutil.now_str()
    path = storage.sector_path(sector_id)
    if not path.exists():
        raise FileNotFoundError(
            f"{sector_id} のセクタープロファイルが無い。先に Phase 3-2 の評価を作成する "
            f"(scripts/analyze_sector.py --sector {sector_id} --generate)"
        )
    profile = storage.read_json(path)
    evidence = sector_analysis.build_bundle(sector_id, as_of=as_of, **kwargs)
    return ChainBundle(
        sector_id=sector_id, label=profile["name"], as_of=as_of, profile=profile, evidence=evidence
    )


def render_prompt(bundle: ChainBundle, kind: str) -> str:
    """波及連鎖 / サプライチェーンのプロンプト。"""
    if kind not in ("wave", "supply_chain"):
        raise ValueError(f"unknown kind: {kind}")

    events = "\n".join(
        f"- [{e['event_id']}] ({e.get('max_reliability', '?')}) {e['event_at'][:10]} {e['title']}"
        for e in (bundle.evidence.events if bundle.evidence else [])
    ) or "- （観測イベントなし）"
    independent = bundle.profile["views"]["independent"]

    common = f"""対象セクター: {bundle.label} ({bundle.sector_id})
分析時点: {bundle.as_of}

## このセクターについての既存の構造評価
{independent['summary']}
コンセンサスとの差分: {independent['divergence']}

## 観測イベント
{events}

## 守るべき規律
- 観察された事実 (observed) と、あなたが推論した因果 (inferred) を必ず分ける。
- inferred には basis（上記の event_id）と falsifier（その因果が成立していないと
  判定できる観測条件）を必ず付ける。
- 推論を重ねるほど確信度が上がることはない。波が進むほど causal_confidence は
  同じか低くなる。
- 株式の銘柄・ティッカー・証券コードには一切言及しない。利益を回収する主体は
  「重電機器メーカー」「電磁鋼板の供給者」のような**類型**で書く。
- 出力は JSON のみ。
"""

    if kind == "wave":
        return common + """
## 依頼
このセクターを起点とする波及連鎖を分析せよ (§17-19)。
wave=1 が直接需要、2 が第二波、3 が第三波。各段について、なぜ波及するのかの
物理的・経済的な機序 (mechanism) を書く。「関連がある」ではなく「何が何を強制するか」を書く。
"""

    return common + """
## 依頼
川上から最終需要までのサプライチェーンを分解せよ (§13-16)。
そのうえで「需要が急増した場合に最初に足りなくなるノード」を特定し (§13-14)、
そのノードについて、誰がその不足から利益を回収できるかを類型で示せ (§15)。
増設や代替による解消経路と、そこに要する時間も書く。
"""


def draft_wave(bundle: ChainBundle, *, generator: Generator) -> dict:
    """波及連鎖を組み立てる（wave.schema.json 準拠）。"""
    payload = _parse(generator(render_prompt(bundle, "wave"), WAVE_SCHEMA))
    links = []
    for link in payload["links"]:
        entry = {
            "wave": link["wave"],
            "from": link["from"],
            "to": link["to"],
            "mechanism": link["mechanism"],
            "claim_type": link["claim_type"],
            "causal_confidence": link["causal_confidence"],
            "falsifier": link["falsifier"],
            "evidence": [
                {"source_id": "derived", "event_id": event_id, "reliability": "B"}
                for event_id in link["basis"]
            ],
        }
        links.append(entry)

    return {
        "chain_id": f"wav_{bundle.sector_id.removeprefix('sec_')}",
        "as_of": bundle.as_of,
        "trigger": {"description": payload["trigger"]["description"], "sector_id": bundle.sector_id},
        "links": links,
        "schema_version": "1.0",
    }


def draft_supply_chain(bundle: ChainBundle, *, generator: Generator) -> dict:
    """サプライチェーンとボトルネックを組み立てる（supply_chain.schema.json 準拠）。"""
    payload = _parse(generator(render_prompt(bundle, "supply_chain"), SUPPLY_CHAIN_SCHEMA))
    name_to_id = {node["name"]: ids.node_id(node["name"]) for node in payload["nodes"]}

    nodes = []
    for node in payload["nodes"]:
        entry = {
            "node_id": name_to_id[node["name"]],
            "name": node["name"],
            "tier_position": node["tier_position"],
        }
        if node["is_bottleneck"]:
            entry["bottleneck"] = {
                "is_bottleneck": True,
                "trigger_condition": node["trigger_condition"],
                "pricing_power": node["pricing_power"],
                "concentration": node["concentration"],
                "monetizer_type": node["monetizer_type"],
                "resolution_path": node["resolution_path"],
                "confidence": node["confidence"],
            }
        nodes.append(entry)

    edges = [
        {
            "from": name_to_id.get(edge["from"], ids.node_id(edge["from"])),
            "to": name_to_id.get(edge["to"], ids.node_id(edge["to"])),
            "flow": edge["flow"],
            "substitutability": edge["substitutability"],
            "confidence": edge["confidence"],
        }
        for edge in payload["edges"]
    ]

    return {
        "map_id": f"scm_{bundle.sector_id.removeprefix('sec_')}",
        "sector_id": bundle.sector_id,
        "as_of": bundle.as_of,
        "nodes": nodes,
        "edges": edges,
        "schema_version": "1.0",
    }


# --- 検証 -----------------------------------------------------------------

def review_wave(chain: dict, bundle: ChainBundle) -> list[str]:
    problems = invariants.check_wave(chain)
    problems.extend(sector_analysis.check_grounding(chain, bundle.evidence))
    problems.extend(_check_no_tickers(chain, "wave"))
    return problems


def review_supply_chain(chain_map: dict, bundle: ChainBundle) -> list[str]:
    problems = invariants.check_supply_chain(chain_map)
    problems.extend(_check_no_tickers(chain_map, "supply_chain"))
    return problems


def _check_no_tickers(payload: dict, label: str) -> list[str]:
    text = json.dumps(payload, ensure_ascii=False)
    return [
        f"§1/§44-46: {label} に銘柄表記が含まれる: {hint}"
        for hint in _TICKER_HINTS
        if hint in text
    ]


def _parse(payload: str) -> dict:
    text = payload.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"generator did not return valid JSON: {exc}") from exc
    return parsed


def save_wave(chain: dict):
    path = storage.DATA / "waves" / f"{chain['chain_id']}.json"
    storage.write_json(path, chain)
    return path


def save_supply_chain(chain_map: dict):
    path = storage.DATA / "supply_chain" / f"{chain_map['map_id']}.json"
    storage.write_json(path, chain_map)
    return path
