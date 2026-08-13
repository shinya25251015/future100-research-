"""RawDocument → Event の正規化 (Phase 2)。

ここで行うのは「事実の構造化」までで、成長判断・因果推論は行わない。
規則ベースで付けるのは次の 3 つだけ:

  - category      … ソース定義の既定カテゴリ
  - signal_types  … config/signal_rules.json のキーワード一致 (§10)
  - sector_links  … config/known_sectors.json のキーワード一致。方向は unclassified のまま (§11)

影響方向・因果・確信度の引き上げは Phase 3 の分析側で行う (§17-19)。
claims には必ず observed（文書に書いてあること）だけを置き、inferred はここで作らない。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from . import config, dedup, ids, storage, textnorm, timeutil

# Source Reliability → その文書に書いてある事実そのものの確信度 (§35-38)
_RELIABILITY_TO_CONFIDENCE = {"A": "high", "B": "high", "C": "medium", "D": "low"}
_SUMMARY_MAX = 600


@dataclass
class NormalizeStats:
    processed: int = 0
    created: int = 0
    duplicates: int = 0
    skipped: int = 0


def normalize_document(document: dict, source: dict, index: dedup.ClusterIndex) -> dict:
    """1 件の raw から Event を作る。重複でもイベント自体は作り、代表フラグで区別する (§38)。"""
    title = document["content"]["title"]
    body = document["content"]["body"]
    text = f"{title}\n{body}"
    observed_at = document["observed_at"]
    published_at = document.get("published_at")
    event_at = published_at or observed_at

    content_hash = document["content"]["content_hash"]
    event_id = ids.event_id(observed_at, content_hash)
    event_key = dedup.build_event_key(
        actors=[source.get("publisher") or source["name"]],
        action=title,
        event_at=event_at,
    )
    cluster_id, is_primary, dedup_meta = dedup.assign(
        index,
        canonical_url=document["fetch"]["canonical_url"],
        content_hash=content_hash,
        event_key=event_key,
        text=text,
        event_id=event_id,
        event_date=event_at[:10],
        source_id=source["source_id"],
    )

    reliability = source["reliability"]
    signal_types = detect_signal_types(text, source, keywords=source["kind"] != "json_series")
    event = {
        "event_id": event_id,
        "cluster_id": cluster_id,
        "is_cluster_primary": is_primary,
        "category": _category(source, signal_types),
        "title": title,
        "summary": _summarize(body or title),
        "observed_at": observed_at,
        "event_at": event_at,
        "event_at_precision": "day" if published_at else "day",
        "regions": [source["country"]] if source.get("country") else [],
        "actors": [
            {
                "name": source.get("publisher") or source["name"],
                "kind": _actor_kind(source),
                **({"country": source["country"]} if source.get("country") else {}),
                "role": "initiator",
            }
        ],
        "signal_types": signal_types,
        "sector_links": detect_sector_links(text, declared=document.get("sector_ids")),
        "sources": [
            {
                "source_id": source["source_id"],
                "raw_id": document["raw_id"],
                "url": document["fetch"]["canonical_url"],
                "reliability": reliability,
                "observed_at": observed_at,
            }
        ],
        "max_reliability": reliability,
        "claims": [
            {
                "claim_id": f"{event_id}#c1",
                "type": "observed",
                "statement": title,
                "confidence": _RELIABILITY_TO_CONFIDENCE[reliability],
            }
        ],
        "dedup": dedup_meta,
        "schema_version": "1.0",
    }
    if published_at:
        event["published_at"] = published_at
    # 統計コレクタが構造のまま取り込んだ数値はそのまま持ち上げる。
    # 本文から数字を拾い直すと単位と対象期間が失われ、市場規模の根拠に使えない (§9, §35-38)。
    if document["content"].get("quantities"):
        event["quantities"] = document["content"]["quantities"]
    return event


@lru_cache(maxsize=1)
def _compiled_rules() -> tuple[tuple[str, re.Pattern[str]], ...]:
    """キーワード規則を語境界つきの正規表現に変換する。

    単純な部分一致では "series a" が "series **a**nd" に当たるなど、英語の
    キーワードが無関係な文に一致してしまう（実測で Samsung の新製品記事が
    資金調達シグナルとして数えられていた）。

      - 複数語のフレーズ … 前後とも語境界を要求する
      - 単語 1 つ         … 先頭のみ語境界（patent が patents にも当たるように）
      - 日本語            … 語境界の概念が無いのでそのまま部分一致
    """
    compiled = []
    for rule in config.load_signal_rules():
        parts = []
        for keyword in rule["any"]:
            escaped = re.escape(keyword)
            if not re.search(r"[a-z]", keyword):
                parts.append(escaped)
            elif " " in keyword.strip():
                parts.append(rf"\b{escaped}\b")
            else:
                parts.append(rf"\b{escaped}")
        compiled.append((rule["signal_type"], re.compile("|".join(parts))))
    return tuple(compiled)


def detect_signal_types(text: str, source: dict | None = None, *, keywords: bool = True) -> list[str]:
    """キーワード規則で判定した種別と、ソース単位で自明な種別の和集合 (§10)。

    Form D の届出はすべて資金調達、契約公告はすべて調達というように、
    ソースの性質だけで決まる種別がある。本文にその語が現れなくても数える。

    keywords=False は統計時系列用。弱いシグナルとは「1 件の出来事が起きたこと」であり、
    集計値はその出来事ではない。「特許出願件数」という系列名にキーワード判定をかけると、
    1 本の年次統計を取り込んだだけで特許シグナルが 12 件立ち上がる（実測）。
    """
    norm = textnorm.normalize_text(text)
    hits = {signal_type for signal_type, pattern in _compiled_rules() if pattern.search(norm)} if keywords else set()
    if source:
        hits.update(source.get("default_signal_types", []))
    return sorted(hits)


@lru_cache(maxsize=1)
def _sector_patterns() -> tuple[tuple[str, re.Pattern[str]], ...]:
    return tuple(
        (sector["sector_id"], pattern)
        for sector in config.load_known_sectors()
        if (pattern := textnorm.keyword_pattern(tuple(sector["keywords"]))) is not None
    )


def detect_sector_links(text: str, *, declared: list[str] | None = None) -> list[dict]:
    """既知セクターへの一次紐付け。方向は付けず、Phase 3 の評価に委ねる。

    declared はソース登録簿で宣言された対象セクター。統計の系列名にはセクター名が
    現れないため（"Production in industry, C27" が送配電に効くことは文面から読めない）、
    キーワード判定だけでは統計が分析側に届かない。
    """
    norm = textnorm.normalize_text(text)
    matched = [sector_id for sector_id, pattern in _sector_patterns() if pattern.search(norm)]
    for sector_id in declared or []:
        if sector_id not in matched:
            matched.append(sector_id)
    return [
        {"sector_id": sector_id, "relation": "unclassified", "confidence": "low"}
        for sector_id in matched
    ]


def rebuild() -> NormalizeStats:
    """events とクラスタ索引を捨てて raw から作り直す。

    正規化規則（signal_rules / known_sectors）を変えたときに使う。raw を不変に保つのは
    この再構築を、当時観測できた情報だけで何度でもやり直せるようにするためである (§37, §48-51)。
    """
    for path in storage.EVENTS_DIR.glob("*.jsonl"):
        path.unlink()
    storage.clear_partitioned_index("clusters")
    storage.clear_normalized_index()
    return run()


def run(*, limit: int | None = None) -> NormalizeStats:
    """未処理の raw をすべて正規化して data/events/ に追記する。

    処理済みかどうかは data/index/normalized/ で管理し、raw には触れない。
    重複判定の索引は発生日ごとに分かれているため、発生日でまとめて処理する。
    """
    stats = NormalizeStats()
    normalized = storage.load_normalized_index()
    sources = {s["source_id"]: s for s in config.load_sources(enabled_only=False)}

    pending: dict[str, list[dict]] = {}
    for document in storage.iter_raw():
        if document["raw_id"] in normalized:
            continue
        if limit is not None and sum(len(v) for v in pending.values()) >= limit:
            break
        event_at = document.get("published_at") or document["observed_at"]
        pending.setdefault(event_at[:10], []).append(document)

    touched_days: dict[str, dict[str, dict]] = {}

    for event_date in sorted(pending):
        index = dedup.ClusterIndex.for_date(event_date)
        for document in pending[event_date]:
            stats.processed += 1
            raw_id = document["raw_id"]
            observed_day = document["observed_at"][:10]
            partition = touched_days.setdefault(observed_day, storage.load_normalized_partition(observed_day))

            source = sources.get(document["source_id"])
            if source is None:
                partition[raw_id] = {
                    "status": "skipped",
                    "skip_reason": "source not in registry",
                    "normalized_at": timeutil.now_str(),
                }
                stats.skipped += 1
                continue

            event = normalize_document(document, source, index)
            storage.append_event(event)
            if event["is_cluster_primary"]:
                stats.created += 1
            else:
                stats.duplicates += 1

            partition[raw_id] = {
                "status": "normalized",
                "event_ids": [event["event_id"]],
                "normalized_at": timeutil.now_str(),
            }
        index.save()

    for day, partition in touched_days.items():
        storage.save_normalized_partition(day, partition)
    return stats


# シグナル種別から導けるカテゴリ。上にあるものを優先する。
# 1 ソースが複数領域を出すため（企業広報は新製品も設備投資も同じフィードに流す）、
# ソース単位の既定値だけでは「§7 世界の設備投資」に新製品ニュースが並ぶ。
_SIGNAL_TO_CATEGORY = (
    # 論文・特許は、本文にどんな語が出てこようとまず論文・特許である。
    # これを後ろに置くと、要旨に "construction of" を含む論文が設備投資に分類される。
    (("patent", "paper"), "technology"),
    (("capex_guidance", "new_facility"), "capex"),
    (("export_control", "procurement", "subsidy", "government_budget"), "policy_regulation"),
    (("supply_contract",), "supply_demand"),
    (("new_standard",), "standard"),
    (("vc_investment",), "corporate_action"),
)


def _category(source: dict, signal_types: list[str] | None = None) -> str:
    """イベントの分類。シグナルから導ければそれを優先し、無ければソースの既定値。"""
    for types, category in _SIGNAL_TO_CATEGORY:
        if any(t in (signal_types or ()) for t in types):
            return category
    categories = source.get("categories") or []
    return categories[0] if categories else "market_data"


def _actor_kind(source: dict) -> str:
    if source["tier"] == "tier1" and source.get("reliability") == "A":
        return "government" if _looks_governmental(source) else "company"
    return "institution"


def _looks_governmental(source: dict) -> bool:
    name = f"{source.get('publisher', '')} {source['name']}".lower()
    return any(k in name for k in ("ministry", "department", "commission", "federal", "bank", "agency", "省", "庁", "日本銀行"))


def _summarize(body: str) -> str:
    text = " ".join(body.split())
    if len(text) <= _SUMMARY_MAX:
        return text
    return text[:_SUMMARY_MAX].rstrip() + "…"
