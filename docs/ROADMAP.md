# 実装ロードマップ

CLAUDE.md Ver.4.0 §52-57 の「既存環境の確認 → データモデル設計 → 段階的実装（Phase 1〜7）」に対する実装計画。
Phase の区切りは仕様書 §3 の分析フローに対応させている。

| Phase | 内容 | 仕様書 | 状態 |
|---|---|---|---|
| 1 | 環境・ディレクトリ・データモデル | §48-57 | **完了** |
| 2 | 情報収集（GLOBAL DATA の取り込み） | §4-7, §35-38, §48-51 | **骨格完了 / 拡張中** |
| 3 | セクター分析（Early Signal → 構造評価） | §8-12, §20-34 | **3-1 完了 / 3-2 未着手** |
| 4 | サプライチェーン・ボトルネック・波及分析 | §13-19 | スキーマのみ |
| 5 | 市場規模・シナリオ・スコア | §9, §39, §44-46 | スキーマのみ |
| 6 | 日次 Global Sector Report 生成 | §47 | テンプレートのみ |
| 7 | 予測 → 実績 → 誤差 → 原因分析 → 改善 | §40-43 | スキーマのみ |

---

## Phase 1（完了）

- `scripts/init_structure.py` … ディレクトリの冪等生成。`--check` で CI からも検査できる。
- `schemas/*.schema.json` … 正準データモデル（JSON Schema draft 2020-12）。
- `src/future100/invariants.py` … JSON Schema では表現しきれない仕様書の規律をコードで強制。

データモデルの設計方針は 4 点に集約される。

1. **観測時刻と発生時刻を分ける** — `observed_at`（本システムが見た時刻）と `event_at`（起きた時刻）を
   全レコードに持たせ、分析側は必ず `as_of` を渡して読む。これが Look-ahead Bias 防止の唯一の仕組み (§37)。
2. **事実と推論を分ける** — `claims[].type` が `observed` か `inferred` か。`inferred` は `basis` 必須 (§17-19)。
3. **値と根拠を対で持つ** — 構造指標も市場規模も `{ value, rationale, evidence }` の形。根拠のない数値は書けない (§35-38)。
4. **raw は不変** — 正規化規則を変えたら `normalize.py --rebuild` で作り直す。当時観測できた情報だけで
   何度でも再現できる状態を保つ (§48-51)。

## Phase 2（情報収集）— 現状と残作業

### 動いているもの

```
scripts/collect.py    登録ソースを巡回 → data/raw/ に不変スナップショット
scripts/normalize.py  raw → Event（分類・シグナル判定・重複統合） → data/events/
scripts/validate_data.py  スキーマ検証 + 仕様書不変条件の検査
```

実測（2026-08-13、有効 9 ソース）: 580 件取得 → 576 件保存 → 570 イベント、重複統合 6 件、検査違反 0。

### 残作業（優先順）

1. **ソース網羅の拡張 (§48-51)** — 現状 9 ソース。仕様書が要求する範囲に対して次が欠けている。
   - 日本の政策一次情報（経産省が実行環境から HTTP 403。国内 IP 経由か e-Gov / 官報の代替経路が必要）
   - 中国・EU の規制当局、各国の予算・調達データベース
   - 特許（USPTO / JPO / EPO の公開 API）、VC 投資、求人 — §10 のシグナル種別に直接対応する
   - 企業 IR / 決算（Capex 追跡 §7）。`json_api` コレクタの mapping で大半は追加できる。
2. **HTML コレクタ** — フィードも API も無い一次情報源（BIS のプレスリリース等）。
   `src/future100/collect/html.py` に CSS セレクタ指定型で実装し、`kind: "html"` を登録する。
3. **重複統合の精度 (§38)** — 現在は「同日 + 別ソース + 包含率 0.6 以上」。
   実データで官報系の定型文による誤統合を確認したため、同一ソース内の文面類似は統合しない設計にしてある。
   規模が増えたら (a) 全クラスタ総当たり O(n²) を転置インデックスで置き換える、
   (b) IDF 重み付けで定型文の寄与を下げる、の 2 点が必要。
4. **カテゴリ分類の精度** — 現在は「ソース定義の既定カテゴリ」。1 ソースが複数領域を出す場合に粗い。
   本文からの分類（規則 → LLM）に置き換える。ここで初めて LLM を使う。
5. **収集の定期実行** — cron / GitHub Actions で日次実行し、失敗ソースを検知する。

### 次に書くコード（Phase 2 の続き）

```
src/future100/collect/html.py        CSS セレクタ指定の HTML コレクタ
src/future100/enrich.py              本文からの category / actors / quantities 抽出
scripts/check_sources.py             全ソースの到達性を検査し、失敗が続くソースを報告
```

## Phase 3（セクター分析）— 設計

Phase 3 は「イベントの山」から「構造の評価」に変換する層で、2 段構えにする。

### 3-1. Early Signal Detection (§10) — 完了

```
src/future100/signals.py       build_window(topic, window, as_of) -> SignalWindow
scripts/detect_signals.py      集計と data/signals/ への保存
```

- `data/events/` を `as_of` で絞り（`is_cluster_primary=true` のみ）、`signal_types` × 期間で集計する。
  可視性は `observed_at`、期間の割り当ては `event_at`。「いつ起きたか」で数え「いつ知ったか」で絞る (§37)。
- 判定は件数の絶対値ではなく **同時増加**。既定は直近 7 日を、その前 28 日を 7 日ぶんに割り戻した
  baseline と比較し、2 種別以上が 2 ソース以上で増えたときだけ発火させる。
- baseline が 0 のときは増加率を定義しない（0 除算を大きな数で誤魔化さない）。

**観測開始による偽シグナルの遮断（実データで最初に踏んだ罠）**

初回実行で「AI / 計算基盤: paper=338 対 baseline 0.25、score 93.3」という強烈なシグナルが出たが、
これは arXiv を*その日に初めて取得した*ことによるもので、世界の変化ではない。
収集開始直後はどのソースも baseline 期間の件数が構造的に 0 になり、**すべてが急増して見える**。

対策として `coverage` を全 SignalWindow に持たせた。寄与ソースごとの観測開始時刻を記録し、
baseline 期間の開始より後に観測を始めたソースが 1 つでもあれば `baseline_covered=false` とし、
`threshold_met` を立てない。`invariants.check_signal_window()` がこの規律を強制する。
baseline 期間ぶん（既定 28 日）収集を続ければ自動的に解消する。

現状の実行結果は **0/16 発火・14 件が判定保留**。これが観測 1 日目の正しい答えである。

- しきい値を超えた topic のうち、既知セクターに紐づかないものが §12 Emerging Sector 候補になる。
- この層に LLM は要らない。**なぜ増えたか**を説明するのが次の層。

### 3-2. セクター構造評価 (§8-9, §20-34) — LLM を使う部分

```
src/future100/sector_analysis.py
    draft_profile(sector_id, as_of) -> SectorProfile
```

- 入力は「as_of 以前のイベント + 既存プロファイル」のみ。プロンプトに保有銘柄情報を一切入れない (§1)。
- 出力は `sector.schema.json` に適合する JSON。生成後に必ず
  `invariants.check_sector()` を通し、違反があれば採用しない（3 シナリオ・撤回条件・根拠必須）。
- LLM に**書かせてよいのは推論だけ**で、事実は必ず event 由来の `evidence` を伴う。
  `claim_type=inferred` には `basis` として event_id / claim_id を必ず引かせる。
- Consensus View と Independent View は**別々の呼び出し**で生成する。
  同一プロンプトで両方書かせると独自見解がコンセンサスに引きずられるため (§34)。

### Phase 3 の実装順

1. ~~`signals.py` + `scripts/detect_signals.py`（規則ベース、LLM 不要）~~ **完了**
2. **日次収集の定常化** — Phase 3-2 に進む前にこれが要る。observation coverage が baseline に届くまで
   （既定設定で 28 日）シグナル判定は保留され続けるため、まず毎日走らせる仕組みを作る。
3. `sector_analysis.py` の骨格 — 入力の組み立てと出力検証だけ先に作り、生成部分は差し替え可能にする
4. 既知 16 セクター (§11) のプロファイル初版を作成し、`validate_data.py` を全件通す
5. §12 Emerging Sector Discovery — signals の結果から新概念を起こす

## 技術的負債・既知の制約

| 項目 | 内容 | 影響 |
|---|---|---|
| 経産省 / IEA が 403 | 実行環境からアクセス拒否 | 日本の政策一次情報が欠落中（Phase 2 の最優先課題） |
| 重複判定 O(n²) | 全クラスタ総当たり | 1 万イベント程度から実用速度を割る |
| 分類が規則ベース | category はソース既定値、sector_link は方向未評価 | Phase 3 の評価精度に上限を作る |
| 日本語トークン化 | 形態素解析なしの文字 2-gram | 精度は実用範囲だが、専門用語の切り出しは弱い |
| 市場規模データなし | TAM/SAM/SOM の一次統計を未取得 | §9 の定量評価が Phase 5 まで空欄 |
| 観測履歴が 1 日ぶん | baseline 期間の被覆が不足 | シグナル判定が全件保留中。日次収集を 28 日続ければ解消 |
| 16 項目レポートの構成 | 要約版仕様書から §47 の 16 項目を再構成した | 完全版 Ver.4.0 の定義と突き合わせが必要 |
