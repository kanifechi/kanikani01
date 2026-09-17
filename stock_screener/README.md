# 日本株ルールベース銘柄判定ツール（CSV出力版）

J-Quants API から日足を取得し、移動平均・RSI・出来高比率・PER/PBR を計算して
**買い候補 / 売り候補 / 様子見** を判定、結果を CSV に書き出す単体スクリプトです。

> ⚠️ これは投資助言ではなく、あらかじめ決めたルールに機械的に当てはめるだけのスクリーニング補助ツールです。

## 構成

| ファイル | 役割 |
| --- | --- |
| `screener.py` | 本体（取得・計算・判定・CSV出力を1ファイルに集約） |
| `watchlist.json` | ウォッチリスト（銘柄コードの配列） |
| `requirements.txt` | 依存ライブラリ（`requests` のみ） |
| `tests/test_screener.py` | 指標計算・判定ロジックのテスト（API認証情報なしで実行可） |
| `output/` | CSV の既定出力先（実行時に自動生成） |

## セットアップ

```bash
pip install -r requirements.txt
```

[J-Quants](https://jpx-jquants.com/) に登録し、認証情報を環境変数に設定します。
以下のいずれか1組でOK（上から優先）:

| 環境変数 | 説明 |
| --- | --- |
| `JQUANTS_ID_TOKEN` | IDトークン直指定（有効期間 24時間） |
| `JQUANTS_REFRESH_TOKEN` | リフレッシュトークン（有効期間 1週間） |
| `JQUANTS_MAIL_ADDRESS` + `JQUANTS_PASSWORD` | 登録メール/パスワードからトークンを自動取得 |

```bash
export JQUANTS_MAIL_ADDRESS="you@example.com"
export JQUANTS_PASSWORD="********"
```

将来 GitHub Actions で毎朝実行する場合は、同じ名前で Repository secrets に登録し、
ジョブの `env:` に渡すだけで動きます（コード側の変更は不要）。

## 使い方

```bash
python screener.py                          # watchlist.json の銘柄を判定 → output/screening_YYYYMMDD.csv
python screener.py --codes 7203 6758        # 銘柄コードを直接指定
python screener.py --watchlist my_list.json # 別のウォッチリストを使う
python screener.py --output result.csv      # 出力先を指定
python screener.py --date 2025-06-30        # 基準日を指定（その日までのデータで判定）
python screener.py --no-fundamentals        # PER/PBR の取得をスキップ（API呼び出し削減）
python screener.py --verbose                # 取得本数などの詳細を表示
```

銘柄コードは4桁（`7203`）でも J-Quants 形式の5桁（`72030`）でも受け付けます。

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
VOLUME_AVG_EXCLUDES_TODAY = True
BUY_RSI_MIN, BUY_RSI_MAX = 30.0, 60.0
BUY_VOLUME_RATIO_MIN = 1.5
SELL_RSI_MIN = 70.0
HISTORY_ROWS = 90             # 指標計算に使う日足の本数
```

## 出力 CSV の列

| 列 | 内容 |
| --- | --- |
| `code` / `name` | 銘柄コード（4桁）/ 銘柄名 |
| `date` | 判定基準日（取得できた最新営業日） |
| `open` / `close` / `volume` | 始値 / 終値 / 出来高 |
| `ma5` / `ma25` / `ma75` | 移動平均線 |
| `rsi14` | RSI（Wilder方式） |
| `volume_avg20` / `volume_ratio` | 過去20日平均出来高 / 当日比率 |
| `per` / `pbr` | 直近開示の EPS・BPS と終値から算出（取得できない場合は空欄） |
| `judgement` | 買い候補 / 売り候補 / 様子見 / 判定不可 |
| `reason` | 判定理由 |

文字化け防止のため BOM 付き UTF-8 で出力しているので、Excel でもそのまま開けます。

## テスト

```bash
python -m unittest discover -s tests -v
```

API 認証情報なしで、移動平均・RSI・出来高比率・クロス検出・判定分岐・CSV出力を検証します。

## 仕様上の注意

- **無料プランのデータは約12週間遅延します。** したがって「当日」とは「取得できた最新営業日」を指します。
  リアルタイム判定が必要になった段階で有料プランへの切り替えを検討してください。
- 株式分割の影響を避けるため、調整後値（`AdjustmentClose` 等）を優先して使用します。
- J-Quants には PER/PBR そのものの API がないため、財務諸表の EPS・BPS と終値から算出しています
  （EPS/BPS が取得できない、または0以下の場合は空欄）。
- 1銘柄あたり最大3回（日足 / 銘柄情報 / 財務）API を呼びます。無料プランのレート制限に配慮して
  呼び出し間に 0.2 秒の間隔を入れています（`API_INTERVAL_SEC`）。

## 今後の拡張予定

1. Google Sheets API 連携（`write_csv()` と同じ `Result` を書き出し先だけ差し替える想定）
2. GitHub Actions による毎朝の定時実行（認証は環境変数のため対応済み）
