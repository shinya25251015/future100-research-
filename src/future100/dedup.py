"""Event Deduplication (§38)。

同じ出来事を複数媒体が報じた場合、それを 1 クラスタに束ね、代表イベント 1 件だけを
シグナル計数・レポートに流す。判定は次の順で行い、最初に一致した方法を採用する。

  1. canonical_url が一致       … 同一記事の再取得
  2. content_hash が一致        … 転載・全文配信
  3. event_key が一致           … 主体 + 動作 + 発生日が同じ
  4. トークン類似度 >= しきい値 … 表現違いの同一報道

クラスタ索引は data/index/clusters.json に置く（events から再生成可能な派生データ）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import ids, storage, textnorm

# textnorm.similarity（包含率ベース）に対するしきい値。
# 実測: 言い換えられた同一報道 0.64-0.79 / 無関係な記事 0.05-0.08。
SIMILARITY_THRESHOLD = 0.6
_MAX_TOKENS_PER_CLUSTER = 400
_INDEX_NAME = "clusters"


@dataclass
class Match:
    cluster_id: str
    method: str
    similarity: float
    primary_event_id: str | None = None


@dataclass
class ClusterIndex:
    """クラスタ索引。プロセス内で更新し、最後に save() で永続化する。"""

    by_url: dict[str, str] = field(default_factory=dict)
    by_hash: dict[str, str] = field(default_factory=dict)
    by_key: dict[str, str] = field(default_factory=dict)
    clusters: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def load(cls) -> "ClusterIndex":
        raw = storage.load_index(_INDEX_NAME, {})
        return cls(
            by_url=raw.get("by_url", {}),
            by_hash=raw.get("by_hash", {}),
            by_key=raw.get("by_key", {}),
            clusters=raw.get("clusters", {}),
        )

    def save(self) -> None:
        storage.save_index(
            _INDEX_NAME,
            {
                "by_url": self.by_url,
                "by_hash": self.by_hash,
                "by_key": self.by_key,
                "clusters": self.clusters,
            },
        )

    # -- lookup ------------------------------------------------------------

    def find(
        self,
        *,
        canonical_url: str,
        content_hash: str,
        event_key: str,
        text: str,
        event_date: str,
        source_id: str,
    ) -> Match | None:
        if canonical_url in self.by_url:
            return self._match(self.by_url[canonical_url], "canonical_url", 1.0)
        if content_hash in self.by_hash:
            return self._match(self.by_hash[content_hash], "content_hash", 1.0)
        if event_key in self.by_key:
            return self._match(self.by_key[event_key], "event_key", 1.0)

        # 文面類似による統合には 2 つの条件を課す。
        #   1. 同日であること … 定例発表（毎旬報告・月次統計）は日付だけが違い本文が酷似する
        #   2. 別ソースであること … 官報系の公告は同一発行元が定型文で別件を大量に出すため、
        #      同一ソース内の文面類似は「同じ出来事」を意味しない（実測で誤統合を確認）
        # 同一ソース内の真の重複は canonical_url / content_hash / event_key で捕捉できる。
        # 取りこぼし（統合漏れ）は件数を過大にするだけだが、誤統合は出来事を消してしまうため、
        # 迷う場合は統合しない側に倒す (§38)。
        probe = textnorm.tokens(text)
        best: Match | None = None
        for cluster_id, cluster in self.clusters.items():
            if event_date not in cluster.get("dates", []):
                continue
            if set(cluster.get("source_ids", [])) <= {source_id}:
                continue
            score = textnorm.similarity(probe, set(cluster.get("tokens", [])))
            if score >= SIMILARITY_THRESHOLD and (best is None or score > best.similarity):
                best = self._match(cluster_id, "token_similarity", round(score, 4))
        return best

    def _match(self, cluster_id: str, method: str, similarity: float) -> Match:
        cluster = self.clusters.get(cluster_id, {})
        return Match(cluster_id, method, similarity, cluster.get("primary_event_id"))

    # -- update ------------------------------------------------------------

    def register(
        self,
        *,
        cluster_id: str,
        event_id: str,
        canonical_url: str,
        content_hash: str,
        event_key: str,
        text: str,
        event_date: str,
        source_id: str,
        is_primary: bool,
    ) -> None:
        cluster = self.clusters.setdefault(
            cluster_id,
            {"primary_event_id": None, "event_ids": [], "tokens": [], "dates": [], "source_ids": []},
        )
        if is_primary and cluster["primary_event_id"] is None:
            cluster["primary_event_id"] = event_id
        if event_id not in cluster["event_ids"]:
            cluster["event_ids"].append(event_id)
        if event_date not in cluster.setdefault("dates", []):
            cluster["dates"].append(event_date)
        if source_id not in cluster.setdefault("source_ids", []):
            cluster["source_ids"].append(source_id)

        merged = set(cluster["tokens"]) | textnorm.tokens(text)
        cluster["tokens"] = sorted(merged)[:_MAX_TOKENS_PER_CLUSTER]

        if canonical_url:
            self.by_url.setdefault(canonical_url, cluster_id)
        self.by_hash.setdefault(content_hash, cluster_id)
        self.by_key.setdefault(event_key, cluster_id)


def build_event_key(*, actors: list[str], action: str, event_at: str) -> str:
    """主体 + 動作 + 発生日から作る正規化キー。表記揺れは normalize_text で吸収する。"""
    actor_part = "|".join(sorted(textnorm.normalize_text(a) for a in actors if a))
    return f"{actor_part}::{textnorm.normalize_text(action)}::{event_at[:10]}"


def assign(
    index: ClusterIndex,
    *,
    canonical_url: str,
    content_hash: str,
    event_key: str,
    text: str,
    event_id: str,
    event_date: str,
    source_id: str,
) -> tuple[str, bool, dict]:
    """クラスタ ID を決定し、(cluster_id, is_primary, dedup メタ) を返す。

    既存クラスタに一致した場合 is_primary=False となり、そのイベントは
    シグナル計数・レポートから除外される（記録自体は残す）。
    """
    match = index.find(
        canonical_url=canonical_url,
        content_hash=content_hash,
        event_key=event_key,
        text=text,
        event_date=event_date,
        source_id=source_id,
    )
    if match is None:
        cluster_id = ids.cluster_id(event_key)
        is_primary = True
        meta = {"content_hash": content_hash, "event_key": event_key, "method": "event_key", "similarity": 1.0}
    else:
        cluster_id = match.cluster_id
        is_primary = False
        meta = {
            "content_hash": content_hash,
            "event_key": event_key,
            "method": match.method,
            "similarity": match.similarity,
        }
        if match.primary_event_id:
            meta["duplicate_of"] = match.primary_event_id

    index.register(
        cluster_id=cluster_id,
        event_id=event_id,
        canonical_url=canonical_url,
        content_hash=content_hash,
        event_key=event_key,
        text=text,
        event_date=event_date,
        source_id=source_id,
        is_primary=is_primary,
    )
    return cluster_id, is_primary, meta
