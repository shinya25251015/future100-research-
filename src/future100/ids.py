"""ID 生成規則。ID は内容から決定的に導出し、再実行しても同じ値になるようにする。"""
from __future__ import annotations

import hashlib
import re

from . import textnorm, timeutil

SECTOR_ID_RE = re.compile(r"^sec_[a-z0-9_]+$")
SOURCE_ID_RE = re.compile(r"^src_[a-z0-9_]+$")


def raw_id(canonical_url: str) -> str:
    digest = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[:16]
    return f"raw_{digest}"


def event_id(observed_at: str, content_hash: str) -> str:
    day = timeutil.day_of(observed_at).strftime("%Y%m%d")
    return f"evt_{day}_{content_hash[:10]}"


def cluster_id(seed: str) -> str:
    """新規クラスタの ID。代表イベントの event_id から導出する。

    event_key から導出すると、「表題も日付も同じだが別の出来事」（同一発行元が同日に
    出す別々の契約公告など）が同じ ID に落ちてしまい、統合しないと判定したのに
    同じクラスタに入る。ID は一意性だけを担い、同一性の判断は dedup 側で行う。
    """
    return f"clu_{textnorm.short_hash(seed, length=10)}"


def sector_id(name: str) -> str:
    return f"sec_{slug(name)}"


def source_id(name: str) -> str:
    return f"src_{slug(name)}"


def node_id(name: str) -> str:
    return f"nd_{slug(name)}"


def slug(text: str) -> str:
    """英数字以外を _ に畳んだ ID 用スラグ。CJK のみの名称ではハッシュにフォールバックする。"""
    lowered = textnorm.normalize_text(text)
    out = re.sub(r"[^a-z0-9]+", "_", lowered).strip("_")
    if not out:
        return textnorm.short_hash(lowered, length=12)
    return out[:60]
