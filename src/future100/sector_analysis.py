"""Phase 3-2: セクター構造評価 (§8-9, §20-34)。

この層で初めて LLM を使う。ただし LLM に任せるのは**推論だけ**で、
入力の組み立て・出力の検証・保存の可否判断はすべてこちら側が持つ。

  1. build_bundle()      as_of 以前のイベントだけを集める（保有銘柄は一切入れない §1, §37）
  2. render_prompt()     コンセンサスと独自見解を別々の呼び出しに分ける (§34)
  3. draft_profile()     生成結果を SectorProfile に組み立てる
  4. review()            スキーマ・不変条件・根拠の実在性を検査する

4 を通らない生成物は保存しない。特に「根拠の実在性」は、存在しない event_id を
引いた推論を弾くためのもので、この層で最も重要な検査になる。

生成部分は Generator（プロンプト → JSON 文字列）として差し替え可能にしてある。
LLM を設定していない環境でも、1・2・4 は単体で動かして検証できる。
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta

from . import config, invariants, signals, storage, timeutil

# 生成器は「プロンプト + 期待するスキーマ → JSON 文字列」。
# スキーマを渡すのは、生成側で構造化出力を使い形の逸脱を防ぐため。
# 形が合っていても仕様書の規律（根拠の実在性・撤回条件）は別途 review() で検査する。
Generator = Callable[[str, dict], str]

# 構造化出力の制約に合わせ、数値の範囲指定は使わず additionalProperties は false で固定する。
_EVIDENCE_SCHEMA = {
    "type": "object",
    "properties": {
        "source_id": {"type": "string"},
        "event_id": {"type": "string"},
        "reliability": {"type": "string", "enum": ["A", "B", "C", "D"]},
    },
    "required": ["source_id", "event_id", "reliability"],
    "additionalProperties": False,
}

CONSENSUS_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "evidence": {"type": "array", "items": _EVIDENCE_SCHEMA},
    },
    "required": ["summary", "evidence"],
    "additionalProperties": False,
}

_CONFIDENCE = {"type": "string", "enum": ["high", "medium", "low"]}

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "independent": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "divergence": {"type": "string"},
                "confidence": _CONFIDENCE,
                "basis": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["summary", "divergence", "confidence", "basis"],
            "additionalProperties": False,
        },
        "phase": {
            "type": "object",
            "properties": {
                "phase": {
                    "type": "string",
                    "enum": ["emerging", "validation", "early_growth", "scaling", "maturing", "mature"],
                },
                "rationale": {"type": "string"},
            },
            "required": ["phase", "rationale"],
            "additionalProperties": False,
        },
        "growth": {
            "type": "object",
            "properties": {
                "probability": {
                    "type": "object",
                    "properties": {
                        "value": {"type": "number"},
                        "confidence": _CONFIDENCE,
                        "rationale": {"type": "string"},
                    },
                    "required": ["value", "confidence", "rationale"],
                    "additionalProperties": False,
                },
                "magnitude": {
                    "type": "object",
                    "properties": {
                        "value": {
                            "type": "string",
                            "enum": ["very_low", "low", "medium", "high", "very_high"],
                        },
                        "confidence": _CONFIDENCE,
                        "rationale": {"type": "string"},
                    },
                    "required": ["value", "confidence", "rationale"],
                    "additionalProperties": False,
                },
            },
            "required": ["probability", "magnitude"],
            "additionalProperties": False,
        },
        "scenarios": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "scenario": {"type": "string", "enum": ["bear", "base", "bull"]},
                    "narrative": {"type": "string"},
                    "probability": {"type": "number"},
                    "falsifier": {"type": "string"},
                    "projections": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "year": {"type": "integer", "enum": [2030, 2035]},
                                "market_size": {
                                    "type": "object",
                                    "properties": {
                                        "amount": {"type": "number"},
                                        "currency": {"type": "string"},
                                        "unit": {
                                            "type": "string",
                                            "enum": ["one", "thousand", "million", "billion", "trillion"],
                                        },
                                    },
                                    "required": ["amount", "currency", "unit"],
                                    "additionalProperties": False,
                                },
                            },
                            "required": ["year", "market_size"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["scenario", "narrative", "probability", "falsifier", "projections"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["independent", "phase", "growth", "scenarios"],
    "additionalProperties": False,
}

DEFAULT_WINDOW_DAYS = 90
DEFAULT_MAX_EVENTS = 150
_RELIABILITY_RANK = {"A": 0, "B": 1, "C": 2, "D": 3}


@dataclass
class EvidenceBundle:
    """1 セクターぶんの評価入力。ここに入っているものだけが根拠として使える。"""

    sector_id: str
    label: str
    as_of: str
    window_start: str
    events: list[dict] = field(default_factory=list)

    @property
    def event_ids(self) -> set[str]:
        return {event["event_id"] for event in self.events}

    def reliability_counts(self) -> dict[str, int]:
        counts = {"A": 0, "B": 0, "C": 0, "D": 0}
        for event in self.events:
            counts[event.get("max_reliability", "D")] += 1
        return counts

    def summary_line(self) -> str:
        counts = self.reliability_counts()
        return (
            f"{self.sector_id} ({self.label}): {len(self.events)} events "
            f"[A={counts['A']} B={counts['B']} C={counts['C']} D={counts['D']}] "
            f"{self.window_start}..{self.as_of[:10]}"
        )


def build_bundle(
    sector_id: str,
    *,
    as_of: str | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    max_events: int = DEFAULT_MAX_EVENTS,
    events: list[dict] | None = None,
) -> EvidenceBundle:
    """評価に使うイベントを集める。

    - 可視性は observed_at <= as_of （§37）
    - 重複の代表イベントのみ（§38）
    - 信頼度の高い順・新しい順に上限まで。上限で切るのは入力量の都合であり、
      切り捨てた件数は summary に残す
    - 保有銘柄・ポートフォリオ情報は構造上ここに入り得ない（§1）
    """
    as_of = as_of or timeutil.now_str()
    known = {s["sector_id"]: s for s in config.load_known_sectors()}
    sector = known.get(sector_id)
    if sector is None:
        raise KeyError(f"unknown sector_id: {sector_id} (config/known_sectors.json)")

    topic = signals.Topic.from_known_sector(sector)
    start = timeutil.day_of(as_of) - timedelta(days=window_days - 1)
    start_str = f"{start:%Y-%m-%d}"

    source_events = (
        storage.iter_events(as_of=as_of, primary_only=True)
        if events is None
        else (
            e
            for e in events
            if e.get("is_cluster_primary", True) and timeutil.is_visible(e["observed_at"], as_of)
        )
    )
    matched = [
        event
        for event in source_events
        if event["event_at"][:10] >= start_str and topic.matches(event)
    ]
    # 信頼度の高い順、同じ信頼度なら新しい順（sort が安定なので 2 段階で書ける）
    matched.sort(key=lambda e: e["event_at"], reverse=True)
    matched.sort(key=lambda e: _RELIABILITY_RANK.get(e.get("max_reliability", "D"), 9))

    return EvidenceBundle(
        sector_id=sector_id,
        label=sector["name"],
        as_of=as_of,
        window_start=start_str,
        events=matched[:max_events],
    )


def render_prompt(bundle: EvidenceBundle, view_kind: str) -> str:
    """コンセンサスと独自見解で別々のプロンプトを作る。

    同一の呼び出しで両方書かせると独自見解がコンセンサスに引きずられるため、
    仕様書 §34 の分離は「文面を分ける」ではなく「呼び出しを分ける」で実現する。
    """
    if view_kind not in ("consensus", "independent"):
        raise ValueError(f"unknown view_kind: {view_kind}")

    evidence = "\n".join(
        f"- [{event['event_id']}] ({event.get('max_reliability', '?')}) "
        f"{event['event_at'][:10]} {event['title']}"
        for event in bundle.events
    ) or "- （該当期間に観測されたイベントは無い）"

    common = f"""あなたは産業構造の分析者である。対象セクター: {bundle.label} ({bundle.sector_id})
分析時点: {bundle.as_of}
観測期間: {bundle.window_start} 〜 {bundle.as_of[:10]}

## 守るべき規律
- 下記の観測イベント以外を根拠にしてはならない。event_id は下記に実在するものだけを引く。
- 事実（イベントに書いてあること）と推論（あなたの判断）を混ぜない。推論には必ず basis を付ける。
- 株式の銘柄・ティッカー・投資判断には一切言及しない。分析対象は産業構造であって銘柄ではない。
- 数値を書くときは根拠のイベントを添える。根拠が無ければ数値を書かない。
- 出力は JSON のみ。前後に説明文を付けない。

## 観測イベント
{evidence}
"""

    if view_kind == "consensus":
        return common + """
## 依頼
このセクターについて**市場で一般に語られている見方**を要約せよ。
あなた自身の判断は書かない。観測イベントから読み取れる一般的な受け止め方を書く。

出力形式:
{"summary": "...", "evidence": [{"source_id": "...", "event_id": "...", "reliability": "A|B|C|D"}]}
"""

    return common + """
## 依頼
構造分析にもとづく**独自の見解**を述べよ。市場の一般的見解に合わせにいかない。
需要側の変化だけでなく、供給側の制約（何が先に足りなくなるか）を必ず検討する。

各シナリオには「どうなったらこの見立てを撤回するか」を必ず書く。撤回条件を書けない
シナリオは書いてはならない。成長確率（起きるか）と拡大規模（起きたらどれだけか）は
別々に評価する。混ぜて 1 つのスコアにしない。

出力形式:
{
  "independent": {"summary": "...", "divergence": "コンセンサスとどこでなぜ異なるか",
                  "confidence": "high|medium|low", "basis": ["evt_..."]},
  "phase": {"phase": "emerging|validation|early_growth|scaling|maturing|mature", "rationale": "..."},
  "growth": {
    "probability": {"value": 0.0-1.0, "confidence": "high|medium|low", "rationale": "..."},
    "magnitude": {"value": "very_low|low|medium|high|very_high", "confidence": "...", "rationale": "..."}
  },
  "scenarios": [
    {"scenario": "bear|base|bull", "narrative": "...", "probability": 0.0-1.0,
     "projections": [{"year": 2030, "market_size": {"amount": 0, "currency": "USD", "unit": "billion"}}],
     "falsifier": "この見立てを撤回する観測条件"}
  ]
}
scenarios は bear / base / bull の 3 本すべてを返すこと。
"""


def draft_profile(
    bundle: EvidenceBundle,
    *,
    generator: Generator,
    origin: str = "known_monitor",
) -> dict:
    """生成結果を SectorProfile に組み立てる。検証はここでは行わず review() に任せる。"""
    consensus = _parse(generator(render_prompt(bundle, "consensus"), CONSENSUS_SCHEMA), "consensus")
    analysis = _parse(generator(render_prompt(bundle, "independent"), ANALYSIS_SCHEMA), "independent")

    counts = bundle.reliability_counts()
    profile = {
        "sector_id": bundle.sector_id,
        "name": bundle.label,
        "as_of": bundle.as_of,
        "origin": origin,
        "phase": analysis["phase"],
        "growth": analysis["growth"],
        "market_size": {
            "base_year": int(bundle.as_of[:4]),
            "tam": {"amount": 0, "currency": "USD", "unit": "billion"},
            "method": "未推計。一次統計を取得するまで 0 のまま置く（推測値で埋めない §35-38）。",
            "evidence": [],
        },
        "scenarios": analysis["scenarios"],
        "views": {
            "consensus": consensus,
            "independent": analysis["independent"],
        },
        "evidence_summary": {
            "event_ids": sorted(bundle.event_ids),
            "reliability_counts": counts,
            **_evidence_span(bundle),
        },
        "schema_version": "1.0",
    }
    if "structure" in analysis:
        profile["structure"] = analysis["structure"]
    if "technology_stage" in analysis:
        profile["technology_stage"] = analysis["technology_stage"]
    return profile


def review(profile: dict, bundle: EvidenceBundle) -> list[str]:
    """保存してよいかを判定する。空リストなら保存可。

    仕様書の不変条件に加え、根拠の実在性を検査する。存在しない event_id を引いた
    推論を通すと、以降のすべての分析が架空の観測の上に積み上がる。
    """
    problems = invariants.check_sector(profile)
    problems.extend(check_grounding(profile, bundle))
    return problems


def check_grounding(profile: dict, bundle: EvidenceBundle) -> list[str]:
    """profile 内のすべての event_id 参照が、渡した bundle に実在するかを確かめる。"""
    known = bundle.event_ids
    problems = []
    for path, event_id in _iter_event_refs(profile):
        if event_id not in known:
            problems.append(f"§35-38: {path} references an event not in the evidence bundle: {event_id}")
    return problems


def _iter_event_refs(node, path: str = "") -> list[tuple[str, str]]:
    """profile 内の event_id 参照をすべて拾う（basis の文字列と evidence_ref の両方）。"""
    found: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "event_ids" and path.startswith("evidence_summary"):
                continue  # 自分自身の一覧なので照合対象にしない
            if key == "event_id" and isinstance(value, str):
                found.append((f"{path}.{key}", value))
            else:
                found.extend(_iter_event_refs(value, f"{path}.{key}" if path else key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_iter_event_refs(value, f"{path}[{index}]"))
    elif isinstance(node, str) and node.startswith("evt_"):
        found.append((path, node.split("#")[0]))
    return found


def _parse(payload: str, label: str) -> dict:
    text = payload.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} view: generator did not return valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} view: expected a JSON object, got {type(parsed).__name__}")
    return parsed


def _evidence_span(bundle: EvidenceBundle) -> dict:
    if not bundle.events:
        return {}
    observed = sorted(event["observed_at"] for event in bundle.events)
    return {"oldest_evidence_at": observed[0], "newest_evidence_at": observed[-1]}
