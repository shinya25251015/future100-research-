"""Event Deduplication (§38)。

同じ出来事を複数媒体が報じた場合、それを 1 クラスタに束ね、代表イベント 1 件だけを
シグナル計数・レポートに流す。判定は次の順で行い、最初に一致した方法を採用する。

  1. canonical_url が一致        … 同一記事の再取得
  2. 同日 + content_hash が一致  … 転載・全文配信
  3. event_key が一致            … 主体 + 動作 + 発生日が同じ
  4. 同日 + 別ソース + 文面類似  … 表現違いの同一報道

**すべての判定を発生日で区切る。** 定例の公告（Sunshine Act Meetings、営業毎旬報告など）は
数か月後に一字一句同じ本文で再掲されるため、日付を見ない完全一致判定は別の出来事を
同一視してしまう。

判定がすべて発生日で閉じているため、**索引も発生日ごとに分割**して持つ
(data/index/clusters/YYYY-MM-DD.json)。1 枚の大きな索引を毎日書き換えると、
変更のない過去ぶんまで git に積み直され、観測履歴をリポジトリに残せなくなる。

**文面比較用のトークンは永続化しない。** 実行中のメモリにのみ持つ。トークンを貯めると
索引が日々数 MB 増えるため。この設計の代償として、同じ出来事の言い換え記事が
「別の日の実行」で届いた場合は統合されない（→ docs/ROADMAP.md の既知の制約）。
日次収集では全ソースを同一バッチで取得するため、実務上の大半は同一実行内で捕捉できる。
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
    """1 発生日ぶんのクラスタ索引。プロセス内で更新し、最後に save() で永続化する。"""

    day: str = ""
    by_url: dict[str, str] = field(default_factory=dict)
    by_hash: dict[str, str] = field(default_factory=dict)
    by_key: dict[str, str] = field(default_factory=dict)
    clusters: dict[str, dict] = field(default_factory=dict)
    # 実行中のみ保持する文面トークン（永続化しない）
    _tokens: dict[str, set[str]] = field(default_factory=dict, repr=False)

    @classmethod
    def for_date(cls, day: str) -> "ClusterIndex":
        raw = storage.load_index_partition(_INDEX_NAME, day)
        return cls(
            day=day,
            by_url=raw.get("by_url", {}),
            by_hash=raw.get("by_hash", {}),
            by_key=raw.get("by_key", {}),
            clusters=raw.get("clusters", {}),
        )

    def save(self) -> None:
        if not self.day:
            raise ValueError("ClusterIndex.day is required to persist the partition")
        storage.save_index_partition(
            _INDEX_NAME,
            self.day,
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
        #   1. 同日であること … 定例発表は日付だけが違い本文が酷似する
        #   2. 別ソースであること … 官報系の公告は同一発行元が定型文で別件を大量に出すため、
        #      同一ソース内の文面類似は「同じ出来事」を意味しない（実測で誤統合を確認）
        # 取りこぼしは件数を過大にするだけだが、誤統合は出来事を消してしまうため、
        # 迷う場合は統合しない側に倒す (§38)。
        probe = textnorm.tokens(text)
        best: Match | None = None
        for cluster_id, tokens in self._tokens.items():
            cluster = self.clusters.get(cluster_id, {})
            if set(cluster.get("source_ids", [])) <= {source_id}:
                continue
            score = textnorm.similarity(probe, tokens)
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
        if self.day and event_date != self.day:
            raise ValueError(f"cluster index for {self.day} cannot hold an event dated {event_date}")

        cluster = self.clusters.setdefault(
            cluster_id, {"primary_event_id": None, "event_ids": [], "source_ids": []}
        )
        if is_primary and cluster["primary_event_id"] is None:
            cluster["primary_event_id"] = event_id
        if event_id not in cluster["event_ids"]:
            cluster["event_ids"].append(event_id)
        if source_id not in cluster.setdefault("source_ids", []):
            cluster["source_ids"].append(source_id)

        merged = self._tokens.get(cluster_id, set()) | textnorm.tokens(text)
        self._tokens[cluster_id] = set(sorted(merged)[:_MAX_TOKENS_PER_CLUSTER])

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
