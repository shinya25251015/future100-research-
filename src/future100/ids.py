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


def cluster_id(event_key: str) -> str:
    return f"clu_{textnorm.short_hash(event_key, length=10)}"


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
