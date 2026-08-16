"""Phase 3-2 の生成器（Claude API 接続）。

sector_analysis は「プロンプト + 期待するスキーマ → JSON 文字列」という関数だけを
必要とする。ここではそれを Claude API で実装する。生成結果の検証は呼び出し側
(sector_analysis.review) が行うので、この層の責務は次の 3 つに限る。

  1. 構造化出力でスキーマに沿った JSON を返させる（プロンプトで JSON を"お願い"しない）
  2. 拒否 (stop_reason=refusal) を content の読み出し前に処理する
  3. 鍵が無い・SDK が無い場合に、対処法を添えて失敗する

依存は任意。未導入の環境でも sector_analysis の他の機能は動く。

  pip install anthropic
  export ANTHROPIC_API_KEY=...
"""
from __future__ import annotations

import json
import os
from typing import Any

MODEL = "claude-opus-5"
MAX_TOKENS = 16000
DEFAULT_EFFORT = "high"


class GeneratorUnavailable(RuntimeError):
    """SDK または認証情報が無い。対処法をメッセージに含める。"""


class GenerationRefused(RuntimeError):
    """安全性の判定により生成が拒否された。内容を読み出す前に検出する。"""


def anthropic_generator(*, model: str = MODEL, effort: str = DEFAULT_EFFORT, max_tokens: int = MAX_TOKENS):
    """Claude API を呼ぶ生成器を返す。

    構造化出力 (output_config.format) を使い、スキーマ適合の JSON を保証する。
    それでも sector_analysis.review() は通す。スキーマが保証するのは「形」であって、
    根拠の実在性や撤回条件の有無という仕様書の規律ではないため (§35-39)。
    """
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - 環境依存
        raise GeneratorUnavailable(
            "anthropic SDK が未導入です。`pip install anthropic` を実行してください。"
        ) from exc

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise GeneratorUnavailable(
            "認証情報が見つかりません。`export ANTHROPIC_API_KEY=...` を設定するか、"
            "`ant auth login` でプロファイルを作成してください。"
        )

    client = anthropic.Anthropic()

    def generate(prompt: str, schema: dict[str, Any]) -> str:
        response = client.beta.messages.create(
            model=model,
            max_tokens=max_tokens,
            # 安全性判定で拒否された場合に別モデルで再実行する。
            # 拒否のカテゴリに応じた既定の退避先が選ばれる。
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            thinking={"type": "adaptive"},
            output_config={
                "effort": effort,
                "format": {"type": "json_schema", "schema": schema},
            },
            messages=[{"role": "user", "content": prompt}],
        )

        # content を読む前に拒否を判定する（拒否時 content は空または途中まで）
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise GenerationRefused(f"生成が拒否された (category={category})。プロンプトの内容を確認する。")

        text = next((block.text for block in response.content if block.type == "text"), "")
        if not text:
            raise RuntimeError(f"生成器が本文を返さなかった (stop_reason={response.stop_reason})")
        return text

    return generate


def replay_generator(path: str, *, keys: tuple[str, str] = ("consensus", "independent"),
                     marker: str = "一般に語られている見方", first_key: str = "consensus"):
    """保存済みの生成結果を読み直す生成器。

    ファイルは呼び出し 1 回ぶんの応答をキーに割り当てた形にする。

      セクター評価: {"consensus": {…CONSENSUS_SCHEMA…},
                     "independent": {…ANALYSIS_SCHEMA 全体…}}
      連鎖分析:     {"wave": {…WAVE_SCHEMA…}, "supply_chain": {…SUPPLY_CHAIN_SCHEMA…}}

    2 番目のキーが持つのは「2 回目の呼び出しの応答全体」であって、独自見解の部分だけ
    ではない。ANALYSIS_SCHEMA は independent / phase / growth / scenarios を含むので、
    "independent" キーの中にもう一段 "independent" が入る形になる。

    用途は 2 つある。1 つは再現・デバッグ。もう 1 つは、API 以外の場所で生成した
    結果を取り込むこと。検査（根拠の実在・撤回条件・銘柄への言及）は生成元に関係なく
    同じように走るので、取り込み経路が変わっても出力の規律は変わらない。

    marker はプロンプトに現れる語で、どちらの依頼かを見分けるために使う。
    """
    with open(path, encoding="utf-8") as handle:
        saved = json.load(handle)

    missing = [key for key in keys if key not in saved]
    if missing:
        raise GeneratorUnavailable(f"保存済み生成結果に {', '.join(missing)} が無い: {path}")

    def generate(prompt: str, schema: dict[str, Any]) -> str:
        key = first_key if marker in prompt else next(k for k in keys if k != first_key)
        return json.dumps(saved[key], ensure_ascii=False)

    return generate
