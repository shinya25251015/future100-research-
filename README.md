# Future100 Global Future Sector Intelligence System

世界の政策・地政学・マクロ経済・技術革新・設備投資・需給・資源制約を継続的に観測し、
**今後 5〜10 年で成長する可能性が高いセクター・産業・市場を早期に発見する**ための分析基盤。

仕様は [CLAUDE.md](CLAUDE.md)（Ver.4.0）が正。実装計画は [docs/ROADMAP.md](docs/ROADMAP.md)。

このシステムが**やらないこと**（仕様書 §1, §44-46）:

- 保有銘柄・積立銘柄を分析の起点にしない
- 銘柄推奨・銘柄ランキング・セクターランキングを出力しない
- 根拠のない数値、観測時点より後の情報に基づく評価を残さない

## 使い方

Python 3.11 以上。収集・正規化・検査は標準ライブラリのみで動く。

```bash
python3 scripts/init_structure.py          # ディレクトリ生成（冪等）
python3 scripts/collect.py --dry-run       # 全ソースの到達性を確認
python3 scripts/collect.py                 # data/raw/ にスナップショットを保存
python3 scripts/normalize.py               # raw → data/events/（重複統合込み）
python3 scripts/detect_signals.py          # 弱いシグナルの同時増加を集計 (§10)
python3 scripts/validate_data.py           # スキーマ + 仕様書不変条件の検査

python3 -m pytest tests -q                 # テスト（pytest が無ければ各ファイルを直接実行）
```

JSON Schema 検証まで行う場合のみ追加依存を入れる（未導入でも不変条件の検査は動く）:

```bash
pip install jsonschema referencing
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
  collect/  コレクタ（rss / atom / json_api）
  normalize.py  raw → Event
  dedup.py      同一ニュースの統合 (§38)
  signals.py    Early Signal Detection (§10)
  timeutil.py   UTC 統一と Look-ahead Bias ガード (§36-37)
  invariants.py 仕様書の規律をコードで強制 (§17-19, §39, §44-46)
scripts/    運用コマンド
data/       収集データ（git 管理外・再生成可能）
reports/    日次・月次レポート
backtest/   予測と実績の突き合わせ (§40-43)
```

## 設計の核

- **観測時刻と発生時刻を分離する。** 全レコードが `observed_at` を持ち、分析は `as_of` 以前のデータしか
  読まない。過去の分析を後から再現できる状態を保つ (§37)。
- **事実と推論を分離する。** `claims[].type` が `observed` か `inferred` か。推論には根拠が必須 (§17-19)。
- **raw は不変。** 解釈が変わっても取得データは書き換えず、events を作り直す (§48-51)。
- **迷ったら統合しない。** 重複統合の誤りは出来事を消す。取りこぼしは件数が増えるだけ (§38)。
- **観測開始を「変化」と読まない。** 収集を始めたばかりのソースは過去期間の件数が構造的に 0 になり、
  すべてが急増して見える。baseline 期間を観測できていない topic は判定を保留する (§10)。
