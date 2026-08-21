# Future100 Global Future Sector Intelligence System

世界の政策・地政学・マクロ経済・技術革新・設備投資・需給・資源制約を継続的に観測し、
**今後 5〜10 年で成長する可能性が高いセクター・産業・市場を早期に発見する**ための分析基盤。

仕様は [CLAUDE.md](CLAUDE.md)（Ver.4.0）が正。実装計画は [docs/ROADMAP.md](docs/ROADMAP.md)、
現状と残課題は [docs/HANDOVER.md](docs/HANDOVER.md)。

このシステムが**やらないこと**（仕様書 §1, §44-46）:

- 保有銘柄・積立銘柄を分析の起点にしない
- 銘柄推奨・銘柄ランキング・セクターランキングを出力しない
- 根拠のない数値、観測時点より後の情報に基づく評価を残さない

## 使い方

Python 3.11 以上。収集・正規化・検査は標準ライブラリのみで動く。

```bash
python3 scripts/daily.py                   # 収集 → 正規化 → シグナル集計 → 検査（日次サイクル）
```

個別に実行する場合:

```bash
python3 scripts/init_structure.py          # ディレクトリ生成（冪等）
python3 scripts/collect.py --dry-run       # 全ソースの到達性を確認
python3 scripts/collect.py                 # data/raw/ にスナップショットを保存
python3 scripts/collect.py --force         # poll_interval_minutes を無視して取り直す
python3 scripts/backfill.py --days 35      # 日付範囲で取れるソースの過去分を取り込む (§10)
python3 scripts/normalize.py               # raw → data/events/（重複統合込み）
python3 scripts/detect_signals.py          # 弱いシグナルの同時増加を集計 (§10)
python3 scripts/analyze_sector.py --list   # セクターごとの根拠件数を確認 (§20-34)
python3 scripts/build_report.py            # 日次レポート 16 項目を生成 (§47)
python3 scripts/analyze_chain.py --sector sec_power_grid  # 波及・ボトルネック (§13-19)
python3 scripts/backtest.py --summary      # 予測精度と外れた原因の内訳 (§40-43)
python3 scripts/validate_data.py           # スキーマ + 仕様書不変条件の検査

python3 -m pytest tests -q                 # テスト（pytest が無ければ各ファイルを直接実行）
```

日次サイクルは `.github/workflows/daily.yml` で毎日 21:30 UTC（日本時間 06:30）に実行され、
観測履歴を `data/` にコミットする。**観測履歴はこのシステムの資産**で、これが貯まらないと
Early Signal Detection は baseline を作れず判定を保留し続ける (§10)。

日付範囲を受け付ける API（arXiv・Federal Register・USAspending・統計）は、過去分を
`scripts/backfill.py` で先に取り込める。RSS しか出していないソース（企業広報・中央銀行・
専門メディア）は原理的に遡れないため、そのソースが寄与する topic の判定は日次収集が
baseline 期間に届くまで保留される。取り込めた範囲は `data/index/backfill.json` に
実行結果として記録し、シグナル判定の観測被覆に反映する。

過去分を取り込んでも `observed_at` は取り込んだ時刻のままで、発行日には書き換えない。
そのため今日より前の `as_of` で再生したときこの過去分は見えない（過去時点の再現を
壊さないため, §37）。

JSON Schema 検証まで行う場合のみ追加依存を入れる（未導入でも不変条件の検査は動く）:

```bash
pip install jsonschema referencing
```

## 資格情報

偽の連絡先・偽の鍵はリポジトリに置いていない。未設定の項目は、それを要求するソース
または生成処理だけが「未設定である」と明示して失敗し、他はそのまま動く。

| 環境変数 | 何が有効になるか | 取得先 |
| --- | --- | --- |
| `FUTURE100_CONTACT_EMAIL` | SEC EDGAR Form D（資金調達シグナル, §10）。利用条件として User-Agent に連絡先を要求される | 自分の連絡先 |
| `ANTHROPIC_API_KEY` | Phase 3-2 セクター構造評価 (§20-34) と Phase 4 波及・サプライチェーン (§13-19) の生成 | https://console.anthropic.com/ |
| `FUTURE100_EIA_API_KEY` | 米国の電力需給統計（月次）。設定後 `config/sources.json` の `src_us_eia_electricity` を `enabled: true` にする | https://www.eia.gov/opendata/register.php （無償） |
| `FUTURE100_BLS_API_KEY` | 米国の産業別雇用統計。キー無しでも取得できるが日次上限が共有 IP 単位で、実測では上限超過を HTTP 200 で返した | https://data.bls.gov/registrationEngine/ （無償） |

```bash
export FUTURE100_CONTACT_EMAIL=you@example.com
export ANTHROPIC_API_KEY=sk-ant-...
python3 scripts/analyze_sector.py --sector sec_power_grid --generate
```

GitHub Actions で動かす場合は同名の Secret を登録する。生成は日次サイクルに含めず、
`.github/workflows/analyze.yml` を手動実行する（構造評価は毎日回すものではなく、
毎日生成し直すと結論が揺れて予測精度の測定 (§40-43) が濁るため）。

### API キーを使わずに分析を生成する

`ANTHROPIC_API_KEY` は API の従量課金が発生する。課金せずに済ませるなら、生成だけを
別の場所（Claude Code セッションや claude.ai）で行い、結果を `--replay` で取り込む。
**検査（根拠の実在・撤回条件の有無・銘柄への言及・波が進むほど確信度が上がっていないか）
は生成元に関係なく同じように走る**ので、取り込み経路が変わっても出力の規律は変わらない。

```bash
# 1. プロンプトを出す（分析時点を固定する。根拠の集合がずれると id が合わなくなる）
AS_OF=$(python3 -c "import sys;sys.path.insert(0,'src');from future100 import timeutil;print(timeutil.now_str())")
python3 scripts/analyze_sector.py --sector sec_ai_compute --as-of "$AS_OF" --show-prompt

# 2. 応答を JSON で保存する
#    {"consensus": {…}, "independent": {…ANALYSIS_SCHEMA 全体…}}
# 3. 検証して保存（API を呼ばない）
python3 scripts/analyze_sector.py --sector sec_ai_compute --as-of "$AS_OF" --replay out.json

# 連鎖分析も同じ。{"wave": {…}, "supply_chain": {…}}
python3 scripts/analyze_chain.py --sector sec_ai_compute --as-of "$AS_OF" --replay chain.json
```

正規化規則（`config/signal_rules.json` / `config/known_sectors.json`）を変えたときは、
raw を保ったまま作り直す:

```bash
python3 scripts/normalize.py --rebuild
```

## 構成

```
config/     情報ソース登録簿・監視セクター・シグナル判定規則
schemas/    正準データモデル（JSON Schema）と説明用の例
src/future100/
  collect/  コレクタ（rss / atom / json_api / json_series）
  normalize.py  raw → Event
  dedup.py      同一ニュースの統合 (§38)
  signals.py    Early Signal Detection (§10)
  sector_analysis.py  セクター構造評価の入力組み立てと出力検証 (§20-34)
  generators.py       Claude API 接続（構造化出力・拒否処理）
  chain_analysis.py   サプライチェーン・ボトルネック・波及 (§13-19)
  backtest.py         予測 → 実績 → 誤差 → 改善 (§40-43)
  report.py           日次レポート 16 項目 (§47)
  timeutil.py   UTC 統一と Look-ahead Bias ガード (§36-37)
  invariants.py 仕様書の規律をコードで強制 (§17-19, §39, §44-46)
scripts/    運用コマンド
data/       観測履歴（git 追跡下。raw は日×ソースの gzip JSONL、索引は発生日で分割）
reports/    日次・月次レポート
backtest/   予測と実績の突き合わせ (§40-43)
```

## 設計の核

- **観測時刻と発生時刻を分離する。** 全レコードが `observed_at` を持ち、分析は `as_of` 以前のデータしか
  読まない。過去の分析を後から再現できる状態を保つ (§37)。
- **事実と推論を分離する。** `claims[].type` が `observed` か `inferred` か。推論には根拠が必須 (§17-19)。
- **raw は不変。** 解釈が変わっても取得データは書き換えず、events を作り直す (§48-51)。
- **迷ったら統合しない。** 重複統合の誤りは出来事を消す。取りこぼしは件数が増えるだけ (§38)。
- **統計は数値のまま取り込む。** 一次統計は 1 データ点 1 観測として、単位と対象期間を伴って
  `quantities` に入る。本文に書いた数字を後から拾い直すと単位が失われ、市場規模の根拠に使えない (§9)。
- **観測開始を「変化」と読まない。** 収集を始めたばかりのソースは過去期間の件数が構造的に 0 になり、
  すべてが急増して見える。baseline 期間を観測できていない topic は判定を保留する (§10)。
