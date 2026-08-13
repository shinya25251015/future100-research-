"""URL / 本文の正規化と、重複判定のためのトークン化 (§38)。

日本語・英語が混在するため、形態素解析器に依存せず
「ラテン文字は単語、CJK は文字 2-gram」という方式でトークン化する。
外部依存なしで動くことを優先している。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# 内容に影響しない追跡パラメータ。canonical URL から除去する。
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src", "spm", "cmpid",
}

_WS = re.compile(r"\s+")
_URL_IN_TEXT = re.compile(r"https?://\S+")
_LATIN_WORD = re.compile(r"[a-z0-9]+(?:[.\-_][a-z0-9]+)*")
# ひらがな・カタカナ・CJK統合漢字(拡張A含む)・互換漢字
_CJK = re.compile("[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def canonical_url(url: str) -> str:
    """追跡パラメータ・フラグメント・末尾スラッシュを除いた正規化 URL。"""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower() or "https"
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    if (scheme == "https" and netloc.endswith(":443")) or (scheme == "http" and netloc.endswith(":80")):
        netloc = netloc.rsplit(":", 1)[0]
    query = urlencode(
        sorted(kv for kv in parse_qsl(parts.query, keep_blank_values=True) if kv[0].lower() not in _TRACKING_PARAMS)
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, query, ""))


def normalize_text(text: str) -> str:
    """全角/半角・記号・空白の揺れを吸収した比較用テキスト。"""
    text = unicodedata.normalize("NFKC", text)
    text = _URL_IN_TEXT.sub(" ", text)
    text = text.lower()
    text = _WS.sub(" ", text)
    return text.strip()


def content_hash(*parts: str) -> str:
    """正規化後の本文から求める SHA-256。完全一致重複の一次判定に使う。"""
    joined = "\n".join(normalize_text(p) for p in parts if p)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def tokens(text: str) -> set[str]:
    """近似重複判定用のトークン集合。ラテン語は単語、CJK は文字 2-gram。"""
    norm = normalize_text(text)
    out = {w for w in _LATIN_WORD.findall(norm) if len(w) > 1}

    run: list[str] = []
    for ch in norm:
        if _CJK.match(ch):
            run.append(ch)
            continue
        out.update(_bigrams(run))
        run = []
    out.update(_bigrams(run))
    return out


def _bigrams(chars: list[str]) -> set[str]:
    if len(chars) == 1:
        return {chars[0]}
    return {chars[i] + chars[i + 1] for i in range(len(chars) - 1)}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / len(a | b)


# 包含率を使う最小トークン数。これを下回る短文は、長文にたまたま含まれるだけで
# 高い包含率が出るため Jaccard に切り替える。
MIN_TOKENS_FOR_CONTAINMENT = 8


def similarity(a: set[str], b: set[str]) -> float:
    """同一報道の判定に使う類似度。

    媒体によって本文の長さが大きく違う（見出し＋数行 vs 全文）ため、Jaccard だけでは
    同じ出来事の記事を取り逃がす。短い側にどれだけ含まれているか（包含率）を主指標にし、
    どちらかが極端に短い場合のみ Jaccard に落とす。
    """
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    smaller = min(len(a), len(b))
    if smaller < MIN_TOKENS_FOR_CONTAINMENT:
        return inter / len(a | b)
    return inter / smaller


@lru_cache(maxsize=64)
def keyword_pattern(keywords: tuple[str, ...]) -> re.Pattern[str] | None:
    """キーワード群を語境界つきの正規表現にまとめる。

    単純な部分一致だと英語のキーワードが無関係な文に当たる。実測では
    "transformer"（電力の変圧器）が機械学習の Transformer 論文に、
    "space"（宇宙）が "latent space" に当たり、あるセクターの根拠の大半が
    無関係な論文で埋まっていた。

      - 英字を含む語 … 前後に語境界を要求し、末尾の複数形 (s / es) だけ許す
                       （"ai" が "aim" に当たらず、"patents" は "patent" に当たる）
      - 日本語       … 語境界の概念が無いのでそのまま部分一致
    """
    parts = []
    for keyword in keywords:
        escaped = re.escape(normalize_text(keyword))
        if re.search(r"[a-z]", keyword.lower()):
            parts.append(rf"\b{escaped}(?:e?s)?\b")
        else:
            parts.append(escaped)
    return re.compile("|".join(parts)) if parts else None


def short_hash(*parts: str, length: int = 10) -> str:
    joined = " ".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:length]
