"""UTC 時刻の統一と Look-ahead Bias ガード (§36-37)。

本システムでは時刻を必ず UTC の RFC3339 文字列 (末尾 Z) で保持する。
ローカル時刻・naive datetime をそのまま書き込むことは禁止する。
"""
from __future__ import annotations

from datetime import date, datetime, timezone

TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class LookAheadError(RuntimeError):
    """分析時点 as_of より後に観測された情報を参照しようとしたときに送出する (§37)。"""


def now() -> datetime:
    return datetime.now(timezone.utc)


def to_utc(dt: datetime) -> datetime:
    """naive datetime は UTC とみなさず拒否する（暗黙のタイムゾーン推定を防ぐ）。"""
    if dt.tzinfo is None:
        raise ValueError("naive datetime is not allowed; attach an explicit tzinfo")
    return dt.astimezone(timezone.utc)


def fmt(dt: datetime) -> str:
    return to_utc(dt).strftime(TS_FORMAT)


def now_str() -> str:
    return fmt(now())


def parse(value: str) -> datetime:
    """RFC3339 文字列を UTC datetime に変換する。'Z' と数値オフセットの両方を受ける。"""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise ValueError(f"timestamp without timezone: {value!r}")
    return dt.astimezone(timezone.utc)


def day_of(value: str | datetime) -> date:
    dt = parse(value) if isinstance(value, str) else to_utc(value)
    return dt.date()


def assert_visible(observed_at: str, as_of: str, *, what: str = "record") -> None:
    """observed_at が as_of 以前であることを保証する (§37)。

    バックテストと日次レポート生成の両方で、データを読み出す境界で必ず呼ぶ。
    """
    if parse(observed_at) > parse(as_of):
        raise LookAheadError(
            f"{what}: observed_at={observed_at} is after as_of={as_of}; "
            "future information must not enter past analysis (spec §37)"
        )


def is_visible(observed_at: str, as_of: str) -> bool:
    return parse(observed_at) <= parse(as_of)
