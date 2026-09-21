# 日本株ルールベース銘柄判定ツール（CSV出力版）

yfinance 経由で日足を取得し、移動平均・RSI・出来高比率・PER/PBR を計算して
**買い候補 / 売り候補 / 様子見** を判定、結果を CSV に書き出す単体スクリプトです。

> ⚠️ これは投資助言ではなく、あらかじめ決めたルールに機械的に当てはめるだけのスクリーニング補助ツールです。

## 構成

| ファイル | 役割 |
| --- | --- |
| `screener.py` | 本体（取得・計算・判定・CSV出力を1ファイルに集約） |
| `watchlist.json` | ウォッチリスト（銘柄コードの配列） |
| `requirements.txt` | 依存ライブラリ（`yfinance` / `pandas`） |
| `tests/test_screener.py` | 指標計算・判定・取得層・CLI通しのテスト（ネットワーク不要） |
| `output/` | CSV の既定出力先（実行時に自動生成） |

## セットアップ

```bash
pip install -r requirements.txt
```

**APIキーの登録は不要です。** yfinance は Yahoo Finance から直接取得するため、
アカウント登録もトークン管理もありません（GitHub Actions でも Secrets 設定が不要）。

## 使い方

```bash
python screener.py                          # watchlist.json の銘柄を判定 → output/screening_YYYYMMDD.csv
python screener.py --codes 7203 6758        # 銘柄コードを直接指定
python screener.py --watchlist my_list.json # 別のウォッチリストを使う
python screener.py --output result.csv      # 出力先を指定
python screener.py --date 2025-06-30        # 基準日を指定（その日までのデータで判定）
python screener.py --no-fundamentals        # 銘柄名・PER/PBR をスキップ（通信量を半減）
python screener.py --interval 2.0           # 呼び出し間隔を延ばす（429が出るとき）
python screener.py --verbose                # 取得本数などの詳細を表示
```

銘柄コードは4桁（`7203`）で指定すれば自動で `7203.T` に変換されます。
`7203.T` 形式での直接指定も可能です（東証以外は `TICKER_SUFFIX` を変更）。

### ウォッチリスト

`watchlist.json` の `codes` に並べるだけです。

```json
{ "codes": ["7203", "6758", "9984"] }
```

## 判定ルール

| 判定 | 条件（すべて満たしたとき） |
| --- | --- |
| 買い候補 | ゴールデンクロス（5日線が25日線を上抜け） **かつ** RSI 30〜60 **かつ** 出来高比率 1.5倍以上 |
| 売り候補 | デッドクロス（5日線が25日線を下抜け） **かつ** RSI 70以上 |
| 様子見 | 上記以外 |
| 判定不可 | データ不足・取得エラー |

- クロスは「**前日はクロスしておらず、当日クロスした**」場合のみ検出します（上抜け/下抜けの瞬間を拾う）。
- **大引け（15:30）前に実行した場合、当日の足は判定に使いません。**
  ザラ場中は出来高がまだ一日分に達しておらず終値も確定していないため、そのまま使うと
  出来高比率が実態より小さく出て買い候補を取りこぼします（寄り付き直後は全銘柄が0.2倍前後になる）。
  この挙動は `EXCLUDE_INCOMPLETE_SESSION` で切り替えられます。
- 出来高比率は「当日出来高 ÷ **当日を除く**過去20日平均」です（急増検知のため）。
- 判定理由には、合致した条件（買い/売り）または外れた条件（様子見）を具体的な数値付きで記録します。

### 閾値の調整

`screener.py` 冒頭の定数ブロックだけを編集すれば調整できます。

```python
MA_SHORT_PERIOD = 5           # 短期移動平均
MA_MID_PERIOD = 25            # 中期移動平均（クロス判定の基準線）
MA_LONG_PERIOD = 75           # 長期移動平均（参考表示）
RSI_PERIOD = 14
VOLUME_AVG_PERIOD = 20
EXCLUDE_INCOMPLETE_SESSION = True  # 大引け前は当日の途中経過を使わない
MARKET_CLOSE_JST = time(15, 30)    # 東証の大引け
VOLUME_AVG_EXCLUDES_TODAY = True
BUY_RSI_MIN, BUY_RSI_MAX = 30.0, 60.0
BUY_VOLUME_RATIO_MIN = 1.5
SELL_RSI_MIN = 70.0
HISTORY_ROWS = 90             # 指標計算に使う日足の本数
REQUEST_INTERVAL_SEC = 1.0    # 呼び出し間隔（429対策）
```

## 出力 CSV の列

| 列 | 内容 |
| --- | --- |
| `code` / `name` | 銘柄コード（4桁）/ 銘柄名 |
| `date` | 判定基準日（取得できた最新営業日） |
| `open` / `close` / `volume` | 始値 / 終値 / 出来高（分割・配当調整後） |
| `ma5` / `ma25` / `ma75` | 移動平均線 |
| `rsi14` | RSI（Wilder方式） |
| `volume_avg20` / `volume_ratio` | 過去20日平均出来高 / 当日比率 |
| `per` / `pbr` | `trailingPE` / `priceToBook`（取得できない場合や0以下は空欄） |
| `judgement` | 買い候補 / 売り候補 / 様子見 / 判定不可 |
| `reason` | 判定理由 |

文字化け防止のため BOM 付き UTF-8 で出力しているので、Excel でもそのまま開けます。

## テスト

```bash
python -m unittest discover -s tests -v
```

ネットワークに接続せず（ダミーのデータソースを差し込んで）、移動平均・RSI・出来高比率・
クロス検出・判定分岐・リトライ・CSV出力・CLI通しまでを検証します。

## yfinance を使ううえでの注意

- **非公式ライブラリです。** Yahoo Finance 側の仕様変更で突然動かなくなる可能性があります。
  「取れない日がある」前提で運用してください（取得失敗は `判定不可` として CSV に残ります）。
- **株価は約15分遅延**します。ザラ場中のリアルタイム判定には向きません。
- **レート制限に注意。** 短時間に大量リクエストを送ると 429 で弾かれ、IPが一時ブロックされることがあります。
  銘柄数が増えたら `--interval` を長めに（2〜3秒）してください。
- yfinance はレート制限時に**例外ではなく空データを返す**ことがあるため、空データもリトライ対象にしています
  （`MAX_RETRIES` / `RETRY_BACKOFF_SEC`）。
- 1銘柄あたり最大2回（日足 / 銘柄情報）通信します。`--no-fundamentals` で1回に減らせます。
- yfinance は研究・個人利用が想定範囲です。取得データの再配布は想定外の用途にあたります。

## データソースを差し替えたくなったら

取得層は `YFinanceSource` に閉じ込めてあり、`fetch_bars()` / `fetch_profile()` の2つだけ
実装すれば他のデータソースに差し替えられます（`ticker_factory` を渡せばテスト用ダミーにも置換可能）。

JPX公式の J-Quants API は無料プランだと**約12週間のデータ遅延**と**5リクエスト/分**の制限があるため、
毎朝の運用には向きませんが、正確な財務データが必要になった場合の候補になります
（V2 から `x-api-key` ヘッダーによるAPIキー方式。V1は2026年6月1日に終了済み）。

## GitHub Actions で毎朝自動実行する

`.github/workflows/screener.yml` を用意してあります。**APIキーが不要なので Secrets の設定もありません。**

| 項目 | 内容 |
| --- | --- |
| 実行時刻 | 平日 8:12 JST（cron は UTC 表記で `12 23 * * 0-4`） |
| 結果の保存先 | `stock_screener/results/screening_YYYYMMDD.csv` をリポジトリに自動コミット |
| 最新版 | `stock_screener/results/latest.csv` が常に最新 |
| サマリー | Actions の実行結果ページに判定一覧が表形式で表示される |
| 手動実行 | Actions タブ →「銘柄判定（毎朝）」→ Run workflow |

### 有効にする手順

1. **このワークフローを `main`（デフォルトブランチ）にマージする**
   GitHub の仕様上、**スケジュール実行はデフォルトブランチにあるワークフローしか動きません。**
   作業ブランチに置いたままだと、手動実行はできても毎朝の自動実行は始まりません。
2. Actions タブから一度「Run workflow」で手動実行し、結果を確認する
3. 以降は平日朝に自動実行され、`results/` に CSV が貯まっていく

### 運用上の注意

- **祝日も実行されます。** 東証の休場日は前営業日と同じ判定結果になるため、CSV に変化がなければコミットはスキップされます。
- **全銘柄が `判定不可` になった場合、ワークフローを意図的に失敗させます。**
  yfinance の仕様変更やレート制限に気づけるようにするためで、GitHub から通知メールが届きます。
- 共有ランナーの IP はレート制限に当たりやすいため、Actions 上では `--interval 3`（3秒間隔）で実行しています。
  銘柄数を増やして時間がかかるようなら `timeout-minutes` と併せて調整してください。
- **スケジュール実行はベストエフォートです。** GitHub 側の混雑により、実行が遅れたり
  スキップされることがあります（特にリクエストが集中する毎時 0 分）。このため実行時刻を
  8 時 12 分にずらしています。それでも実行されない日があれば、Actions タブから手動実行してください。
- 自動実行を止めたいときは Actions タブからワークフローを Disable できます。

## 今後の拡張予定

1. Google Sheets API 連携（`write_csv()` と同じ `Result` を書き出し先だけ差し替える想定）
2. 判定結果の通知（Slack / メール / LINE など）
