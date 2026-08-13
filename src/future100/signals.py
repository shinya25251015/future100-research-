"""Early Signal Detection (§10)。

「政府予算・特許・論文・VC投資・求人・新工場建設・新規格などの弱いシグナルが
**同時に**増加していること」から Emerging Sector 候補を見つける層。

この層の判定は集計のみで行い、推論は一切しない。ここで出るのは「どこで何が増えたか」
までであり、「なぜ増えたか」「成長するか」は Phase 3-2 の構造評価に委ねる。

設計上の要点:

  - 単独シグナルの絶対数では発火させない。1 種別だけが増えるのは、その媒体が
    たまたま多く報じただけであることが多い。複数種別・複数ソース・複数地域での
    同時増加を要求する (co_occurrence)。
  - 集計対象は is_cluster_primary=true のイベントのみ。同一ニュースの重複を
    数えるとシグナルが水増しされる (§38)。
  - 可視性は observed_at <= as_of で判定し、期間の集計は event_at で行う。
    「いつ起きたか」で数え、「いつ知ったか」で見えるものを絞る (§37)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from . import config, storage, textnorm, timeutil

# co_occurrence.score の配点（内部指標。セクター間の順位付けには使わない §44-46）
_SCORE_WEIGHT_TYPES = 50
_SCORE_WEIGHT_SOURCES = 30
_SCORE_WEIGHT_REGIONS = 20
_SCORE_CAP_TYPES = 4
_SCORE_CAP_SOURCES = 4
_SCORE_CAP_REGIONS = 3

# 発火条件: 「複数種別が」「複数ソースで」同時に増えていること
MIN_RISING_TYPES = 2
MIN_DISTINCT_SOURCES = 2
# baseline から増えたとみなす下限。1 件が 0 件になっただけで発火させない。
MIN_COUNT_TO_RISE = 2


@dataclass
class Topic:
    """集計単位。既知セクターにも、まだ名前のない概念にも使える (§11-12)。"""

    label: str
    keywords: tuple[str, ...] = ()
    sector_id: str | None = None
    is_new_concept: bool = False

    @classmethod
    def from_known_sector(cls, sector: dict) -> "Topic":
        return cls(
            label=sector["name"],
            keywords=tuple(sector.get("keywords", ())),
            sector_id=sector["sector_id"],
        )

    def slug(self) -> str:
        if self.sector_id:
            return self.sector_id.removeprefix("sec_")
        from . import ids

        return ids.slug(self.label)

    def matches(self, event: dict) -> bool:
        """セクター紐付け、またはキーワード一致で対象とみなす。"""
        if self.sector_id and any(link["sector_id"] == self.sector_id for link in event.get("sector_links", [])):
            return True
        if not self.keywords:
            return False
        haystack = textnorm.normalize_text(f"{event['title']}\n{event.get('summary', '')}")
        return any(keyword in haystack for keyword in self.keywords)


@dataclass
class Window:
    start: date
    end: date
    baseline_start: date
    baseline_end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def baseline_days(self) -> int:
        return (self.baseline_end - self.baseline_start).days + 1

    @classmethod
    def ending(cls, end: date, *, window_days: int, baseline_multiplier: int) -> "Window":
        start = end - timedelta(days=window_days - 1)
        baseline_end = start - timedelta(days=1)
        baseline_start = baseline_end - timedelta(days=window_days * baseline_multiplier - 1)
        return cls(start=start, end=end, baseline_start=baseline_start, baseline_end=baseline_end)


@dataclass
class _Bucket:
    """1 シグナル種別ぶんの集計。"""

    count: int = 0
    baseline_raw: int = 0
    event_ids: list[str] = field(default_factory=list)
    sources: set[str] = field(default_factory=set)
    regions: set[str] = field(default_factory=set)


def observation_coverage(events: list[dict]) -> dict[str, str]:
    """ソースごとの観測開始時刻（そのソースを最初に見た observed_at）。

    収集を始めたばかりのソースは baseline 期間の件数が構造的に 0 になるため、
    「増えていないもの」が急増して見える。その誤検出を防ぐために必要 (§10)。
    """
    starts: dict[str, str] = {}
    for event in events:
        observed_at = event["observed_at"]
        for source in event.get("sources", []):
            source_id = source["source_id"]
            if source_id not in starts or observed_at < starts[source_id]:
                starts[source_id] = observed_at
    return starts


def build_window(topic: Topic, window: Window, *, as_of: str, events: list[dict] | None = None) -> dict:
    """1 topic ぶんの SignalWindow を組み立てる（signal.schema.json 準拠）。"""
    if events is None:
        events = list(storage.iter_events(as_of=as_of, primary_only=True))
    coverage_starts = observation_coverage(events)

    buckets: dict[str, _Bucket] = {}
    for event in events:
        if not topic.matches(event):
            continue
        event_day = timeutil.day_of(event["event_at"])
        in_window = window.start <= event_day <= window.end
        in_baseline = window.baseline_start <= event_day <= window.baseline_end
        if not (in_window or in_baseline):
            continue

        for signal_type in event.get("signal_types", []):
            bucket = buckets.setdefault(signal_type, _Bucket())
            if in_window:
                bucket.count += 1
                bucket.event_ids.append(event["event_id"])
                bucket.sources.update(source["source_id"] for source in event.get("sources", []))
                bucket.regions.update(event.get("regions", []))
            else:
                bucket.baseline_raw += 1

    scale = window.days / window.baseline_days if window.baseline_days else 0.0
    counts = []
    rising_types = 0
    sources: set[str] = set()
    regions: set[str] = set()

    for signal_type in sorted(buckets):
        bucket = buckets[signal_type]
        baseline_count = round(bucket.baseline_raw * scale, 3)
        entry = {
            "signal_type": signal_type,
            "count": bucket.count,
            "baseline_count": baseline_count,
            "event_ids": bucket.event_ids,
        }
        # baseline が 0 のときは増加率を定義しない（0 除算を大きな数で誤魔化さない）
        if baseline_count > 0:
            entry["delta_ratio"] = round(bucket.count / baseline_count - 1, 3)
        counts.append(entry)

        if bucket.count >= MIN_COUNT_TO_RISE and bucket.count > baseline_count:
            rising_types += 1
            sources.update(bucket.sources)
            regions.update(bucket.regions)

    # 寄与ソースを baseline 期間の開始時点から観測できていたかを検査する。
    # 1 つでも「観測を後から始めたソース」があれば baseline は過小で、比較が成立しない。
    baseline_start_ts = f"{window.baseline_start:%Y-%m-%d}T00:00:00Z"
    coverage_rows = []
    for source_id in sorted(sources):
        observation_start = coverage_starts.get(source_id, as_of)
        coverage_rows.append(
            {
                "source_id": source_id,
                "observation_start": observation_start,
                "covers_baseline": observation_start <= baseline_start_ts,
            }
        )
    baseline_covered = bool(coverage_rows) and all(row["covers_baseline"] for row in coverage_rows)

    coverage = {
        "baseline_covered": baseline_covered,
        "sources": coverage_rows,
    }
    if not baseline_covered:
        coverage["note"] = (
            "baseline 期間より後に観測を開始したソースが含まれる。増加はこのソースの取り込み開始に"
            "由来する可能性があり、シグナルとして扱わない。"
        )

    co_occurrence = {
        "distinct_signal_types": rising_types,
        "distinct_sources": len(sources),
        "distinct_regions": len(regions),
        "score": _score(rising_types, len(sources), len(regions)),
        "threshold_met": (
            baseline_covered and rising_types >= MIN_RISING_TYPES and len(sources) >= MIN_DISTINCT_SOURCES
        ),
    }

    payload = {
        "window_id": f"sig_{topic.slug()}_{window.end:%Y%m%d}",
        "as_of": as_of,
        "window": {
            "start": f"{window.start:%Y-%m-%d}",
            "end": f"{window.end:%Y-%m-%d}",
            "baseline_start": f"{window.baseline_start:%Y-%m-%d}",
            "baseline_end": f"{window.baseline_end:%Y-%m-%d}",
        },
        "topic": {
            "label": topic.label,
            "keywords": list(topic.keywords),
            "is_new_concept": topic.is_new_concept,
        },
        "counts": counts,
        "coverage": coverage,
        "co_occurrence": co_occurrence,
        "promotion": {
            "promoted": False,
            "rationale": _promotion_rationale(co_occurrence["threshold_met"], baseline_covered),
        },
        "schema_version": "1.0",
    }
    if topic.sector_id:
        payload["topic"]["sector_id"] = topic.sector_id
    return payload


def _promotion_rationale(threshold_met: bool, baseline_covered: bool) -> str:
    if not baseline_covered:
        return "観測期間が baseline に届いていないため判定を保留する（増加が実体か取り込み開始かを区別できない）。"
    if threshold_met:
        return "集計層では昇格を判断しない。同時増加のしきい値を満たしたため構造評価の対象とする。"
    return "同時増加のしきい値を満たさない。"


def _score(rising_types: int, sources: int, regions: int) -> float:
    """内部指標。しきい値判定の補助であり、topic 間の順位付けには使わない (§44-46)。"""
    value = (
        min(rising_types, _SCORE_CAP_TYPES) / _SCORE_CAP_TYPES * _SCORE_WEIGHT_TYPES
        + min(sources, _SCORE_CAP_SOURCES) / _SCORE_CAP_SOURCES * _SCORE_WEIGHT_SOURCES
        + min(regions, _SCORE_CAP_REGIONS) / _SCORE_CAP_REGIONS * _SCORE_WEIGHT_REGIONS
    )
    return round(value, 1)


def run(
    *,
    as_of: str | None = None,
    window_days: int = 7,
    baseline_multiplier: int = 4,
    topics: list[Topic] | None = None,
    save: bool = True,
) -> list[dict]:
    """全 topic の SignalWindow を作る。既定では §11 の常時監視セクターを対象にする。"""
    as_of = as_of or timeutil.now_str()
    window = Window.ending(
        timeutil.day_of(as_of), window_days=window_days, baseline_multiplier=baseline_multiplier
    )
    if topics is None:
        topics = [Topic.from_known_sector(sector) for sector in config.load_known_sectors()]

    events = list(storage.iter_events(as_of=as_of, primary_only=True))
    windows = [build_window(topic, window, as_of=as_of, events=events) for topic in topics]
    if save:
        for payload in windows:
            storage.save_signal_window(payload)
    return windows
