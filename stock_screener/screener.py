#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日本株ルールベース銘柄判定ツール (yfinance / CSV 出力版)

ウォッチリストの銘柄について yfinance 経由で日足を取得し、
移動平均・RSI・出来高比率・PER/PBR を計算して「買い候補 / 売り候補 / 様子見」を判定、
結果を CSV に書き出す単体スクリプト。

使い方:
    pip install -r requirements.txt
    python screener.py                       # watchlist.json の銘柄を判定して output/ に CSV 出力
    python screener.py --codes 7203 6758     # 銘柄コードを直接指定
    python screener.py --output result.csv   # 出力先を指定

注意:
    yfinance は Yahoo Finance の非公式ライブラリで、API キーは不要な代わりに
    - 株価は約 15 分遅延
    - 短時間に大量リクエストを送ると 429 (レート制限) で弾かれる
    という性質がある。呼び出し間隔 (REQUEST_INTERVAL_SEC) は余裕をもって設定すること。
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

# =============================================================================
# 判定パラメータ（ここだけ触れば判定ロジックを調整できる）
# =============================================================================

# --- 移動平均線 ---
MA_SHORT_PERIOD = 5       # 短期移動平均（日）
MA_MID_PERIOD = 25        # 中期移動平均（日）… クロス判定の基準線
MA_LONG_PERIOD = 75       # 長期移動平均（日）… 参考表示

# --- RSI ---
RSI_PERIOD = 14           # RSI 算出期間（日）

# --- 出来高 ---
VOLUME_AVG_PERIOD = 20            # 出来高平均の算出期間（日）
VOLUME_AVG_EXCLUDES_TODAY = True  # True: 当日を除いた過去 N 日平均と比較（急増検知向け）

# --- 買い候補の条件 ---
BUY_RSI_MIN = 30.0            # RSI 下限（これ以上）
BUY_RSI_MAX = 60.0            # RSI 上限（これ以下）
BUY_VOLUME_RATIO_MIN = 1.5    # 出来高比率（当日 / 過去平均）がこの倍率以上

# --- 売り候補の条件 ---
SELL_RSI_MIN = 70.0           # RSI がこれ以上

# --- 判定ラベル ---
LABEL_BUY = "買い候補"
LABEL_SELL = "売り候補"
LABEL_HOLD = "様子見"
LABEL_NO_DATA = "判定不可"

# --- 取引時間の扱い ---
JST = dt.timezone(dt.timedelta(hours=9), "JST")
MARKET_CLOSE_JST = dt.time(15, 30)     # 東証の大引け
EXCLUDE_INCOMPLETE_SESSION = True      # True: 大引け前は「当日の途中経過」を判定に使わない

# --- データ取得 ---
HISTORY_ROWS = 90          # 指標計算に使う日足の本数（営業日ベース）
FETCH_PERIOD = "1y"        # yfinance に渡す取得期間（上記本数を確保するための余裕込み）
TICKER_SUFFIX = ".T"       # 東証。他市場なら変更（名証 .N / 札証 .S / 福証 .F）
REQUEST_INTERVAL_SEC = 1.0 # 呼び出し間隔（429 回避。銘柄数が多いときは長めに）
MAX_RETRIES = 3            # 取得失敗時のリトライ回数
RETRY_BACKOFF_SEC = 3.0    # リトライ間隔（回数に応じて伸ばす）

# --- ウォッチリスト（watchlist.json が無い場合のフォールバック）---
DEFAULT_WATCHLIST = ["7203", "6758", "9984", "8306", "6501", "4063"]

# --- 出力 ---
DEFAULT_OUTPUT_DIR = "output"
CSV_COLUMNS = [
    "code",           # 銘柄コード（4桁）
    "name",           # 銘柄名
    "date",           # 判定基準日（取得できた最新営業日）
    "open",           # 始値
    "close",          # 終値
    "volume",         # 出来高
    "ma5",
    "ma25",
    "ma75",
    "rsi14",
    "volume_avg20",
    "volume_ratio",   # 当日出来高 / 過去平均出来高
    "per",
    "pbr",
    "judgement",      # 買い候補 / 売り候補 / 様子見 / 判定不可
    "reason",         # 判定理由
]


# =============================================================================
# 銘柄コードとティッカー
# =============================================================================

def to_ticker(code: str) -> str:
    """銘柄コードを yfinance のティッカーに変換する（7203 -> 7203.T）。"""
    code = str(code).strip().upper()
    if "." in code:      # 既に 7203.T のような形式ならそのまま
        return code
    if len(code) == 5 and code.endswith("0"):
        code = code[:-1]  # J-Quants 形式の 5 桁コードにも一応対応（72030 -> 7203）
    return code + TICKER_SUFFIX


def display_code(code: str) -> str:
    """表示用の銘柄コードに整える（7203.T -> 7203）。"""
    code = str(code).strip().upper()
    if "." in code:
        code = code.split(".")[0]
    if len(code) == 5 and code.endswith("0"):
        code = code[:-1]
    return code


# =============================================================================
# データ取得（yfinance）
# =============================================================================

class DataSourceError(RuntimeError):
    """株価データ取得に関するエラー。"""


def _default_ticker_factory(ticker: str) -> Any:
    """yfinance の Ticker を生成する（インポートはここで行う）。

    モジュール読み込み時点では yfinance を必要としないため、
    テストや指標計算だけの利用では依存ライブラリなしで動く。
    """
    try:
        import yfinance  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - 環境依存
        raise DataSourceError(
            "yfinance がインストールされていません。`pip install -r requirements.txt` を実行してください。"
        ) from exc
    return yfinance.Ticker(ticker)


@dataclass
class Profile:
    """銘柄の基本情報とバリュエーション（取得できた分だけ）。"""

    name: str = ""
    per: Optional[float] = None
    pbr: Optional[float] = None


class YFinanceSource:
    """yfinance をラップしたデータ取得層。

    ticker_factory を差し替えればテスト用のダミーにも置き換えられる。
    """

    def __init__(self, ticker_factory: Optional[Callable[[str], Any]] = None,
                 interval_sec: float = REQUEST_INTERVAL_SEC,
                 max_retries: int = MAX_RETRIES,
                 backoff_sec: float = RETRY_BACKOFF_SEC) -> None:
        self._ticker_factory = ticker_factory or _default_ticker_factory
        self._interval_sec = interval_sec
        self._max_retries = max_retries
        self._backoff_sec = backoff_sec
        self._cache: dict[str, Any] = {}

    def _ticker(self, ticker: str) -> Any:
        """同じ銘柄では Ticker オブジェクトを使い回す（余計な通信を減らす）。"""
        if ticker not in self._cache:
            self._cache[ticker] = self._ticker_factory(ticker)
        return self._cache[ticker]

    def _call_with_retry(self, label: str, func: Callable[[], Any],
                         is_valid: Optional[Callable[[Any], bool]] = None) -> Any:
        """レート制限や一時的な通信エラーに備えてリトライする。

        yfinance はレート制限時に例外ではなく「空の結果」を返すことがあるため、
        is_valid を渡して「成功したが中身が無い」ケースもリトライ対象にできる。
        """
        last_error: Optional[str] = None
        for attempt in range(1, self._max_retries + 1):
            if self._interval_sec:
                time.sleep(self._interval_sec)
            try:
                value = func()
                if is_valid is None or is_valid(value):
                    return value
                last_error = "データが空でした（レート制限・コード誤り・上場廃止の可能性）"
            except Exception as exc:  # yfinance は多様な例外を投げるため広めに捕捉する
                last_error = str(exc)
            if attempt < self._max_retries:
                time.sleep(self._backoff_sec * attempt)
        raise DataSourceError(f"{label}の取得に失敗しました: {last_error}")

    # ---- 日足 -------------------------------------------------------------

    def fetch_bars(self, ticker: str, until: Optional[dt.date] = None) -> list[Bar]:
        """日足を古い順に返す。until を指定するとその日までに絞り込む。"""
        frame = self._call_with_retry(
            f"{ticker} の日足",
            lambda: self._ticker(ticker).history(
                period=FETCH_PERIOD, interval="1d", auto_adjust=True
            ),
            is_valid=lambda f: f is not None and len(f) > 0,
        )
        return drop_incomplete_session(bars_from_dataframe(frame, until=until))

    # ---- 銘柄情報・バリュエーション ---------------------------------------

    def fetch_profile(self, ticker: str) -> Profile:
        """銘柄名と PER/PBR を取得する。取れなければ空の Profile。"""
        try:
            info = self._call_with_retry(f"{ticker} の銘柄情報", lambda: self._ticker(ticker).info)
        except DataSourceError:
            return Profile()
        if not isinstance(info, dict):
            return Profile()
        return Profile(
            name=str(info.get("shortName") or info.get("longName") or ""),
            per=_positive(_to_float(info.get("trailingPE") or info.get("forwardPE"))),
            pbr=_positive(_to_float(info.get("priceToBook"))),
        )


# =============================================================================
# 日足データの変換
# =============================================================================

@dataclass
class Bar:
    """日足 1 本分（分割・配当調整後の値）。"""

    date: str
    open: Optional[float]
    close: float
    volume: Optional[float]


def _to_float(value: Any) -> Optional[float]:
    """数値に変換する。NaN・None・空文字は None として扱う。"""
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


def _positive(value: Optional[float]) -> Optional[float]:
    """0 以下は「意味のある値ではない」とみなして None にする（赤字銘柄の PER など）。"""
    return value if value is not None and value > 0 else None


def _format_date(index_value: Any) -> str:
    strftime = getattr(index_value, "strftime", None)
    return strftime("%Y-%m-%d") if callable(strftime) else str(index_value)[:10]


def bars_from_dataframe(frame: Any, until: Optional[dt.date] = None) -> list[Bar]:
    """yfinance の DataFrame を Bar のリストに変換する。

    - 終値が欠損している行（売買停止など）はスキップする
    - until を指定すると、その日以前の行だけを残す（--date 用）
    """
    if frame is None or len(frame) == 0:
        return []

    bars: list[Bar] = []
    for index_value, row in frame.iterrows():
        if until is not None:
            row_date = getattr(index_value, "date", None)
            if callable(row_date) and row_date() > until:
                continue
        close = _to_float(row.get("Close"))
        if close is None:
            continue
        bars.append(Bar(
            date=_format_date(index_value),
            open=_to_float(row.get("Open")),
            close=close,
            volume=_to_float(row.get("Volume")),
        ))
    bars.sort(key=lambda b: b.date)
    return bars


def drop_incomplete_session(bars: list[Bar], now_jst: Optional[dt.datetime] = None,
                           enabled: bool = EXCLUDE_INCOMPLETE_SESSION) -> list[Bar]:
    """大引け前に取得した「当日の途中経過」の足を落とす。

    ザラ場中の当日足は、出来高がまだ一日分に達しておらず、終値も確定していない。
    そのまま使うと出来高比率が実態より小さく出て、買い候補を取りこぼす
    （寄り付き直後に実行すると全銘柄が 0.2 倍前後になる）。
    大引け後であれば当日足は確定値なのでそのまま使う。
    """
    if not enabled or not bars:
        return bars
    now = now_jst or dt.datetime.now(JST)
    if now.time() >= MARKET_CLOSE_JST:
        return bars
    if bars[-1].date == now.date().isoformat():
        return bars[:-1]
    return bars


# =============================================================================
# 指標計算
# =============================================================================

def sma(values: list[float], period: int, offset: int = 0) -> Optional[float]:
    """単純移動平均。offset=0 で末尾（最新）、offset=1 で 1 日前の値。"""
    end = len(values) - offset
    start = end - period
    if period <= 0 or start < 0:
        return None
    return sum(values[start:end]) / period


def rsi(values: list[float], period: int = RSI_PERIOD) -> Optional[float]:
    """RSI（Wilder の平滑化）。値が足りなければ None。"""
    if period <= 0 or len(values) < period + 1:
        return None

    gains: list[float] = []
    losses: list[float] = []
    for prev, cur in zip(values, values[1:]):
        diff = cur - prev
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def volume_ratio(volumes: list[Optional[float]], period: int = VOLUME_AVG_PERIOD,
                 exclude_today: bool = VOLUME_AVG_EXCLUDES_TODAY
                 ) -> tuple[Optional[float], Optional[float]]:
    """(過去平均出来高, 当日出来高 / 過去平均) を返す。"""
    clean = [v for v in volumes if v is not None]
    if not clean:
        return None, None
    today = clean[-1]
    window = clean[:-1] if exclude_today else clean
    if len(window) < period:
        return None, None
    avg = sum(window[-period:]) / period
    if avg <= 0:
        return avg, None
    return avg, today / avg


def cross_state(closes: list[float], short_period: int = MA_SHORT_PERIOD,
                mid_period: int = MA_MID_PERIOD) -> Optional[str]:
    """最新日のクロス状態を返す: 'golden' / 'dead' / None。

    前日の短期線 <= 中期線 かつ 当日の短期線 > 中期線 でゴールデンクロス（逆はデッドクロス）。
    """
    short_now = sma(closes, short_period, offset=0)
    short_prev = sma(closes, short_period, offset=1)
    mid_now = sma(closes, mid_period, offset=0)
    mid_prev = sma(closes, mid_period, offset=1)
    if None in (short_now, short_prev, mid_now, mid_prev):
        return None
    if short_prev <= mid_prev and short_now > mid_now:
        return "golden"
    if short_prev >= mid_prev and short_now < mid_now:
        return "dead"
    return None


# =============================================================================
# 判定ロジック
# =============================================================================

@dataclass
class Indicators:
    date: str = ""
    open: Optional[float] = None
    close: Optional[float] = None
    volume: Optional[float] = None
    ma_short: Optional[float] = None
    ma_mid: Optional[float] = None
    ma_long: Optional[float] = None
    rsi: Optional[float] = None
    volume_avg: Optional[float] = None
    volume_ratio: Optional[float] = None
    cross: Optional[str] = None
    per: Optional[float] = None
    pbr: Optional[float] = None


def build_indicators(bars: list[Bar]) -> Optional[Indicators]:
    """日足リストから各指標を計算する。データが無ければ None。"""
    if not bars:
        return None
    closes = [b.close for b in bars]
    volumes = [b.volume for b in bars]
    latest = bars[-1]
    avg_volume, ratio = volume_ratio(volumes)
    return Indicators(
        date=latest.date,
        open=latest.open,
        close=latest.close,
        volume=latest.volume,
        ma_short=sma(closes, MA_SHORT_PERIOD),
        ma_mid=sma(closes, MA_MID_PERIOD),
        ma_long=sma(closes, MA_LONG_PERIOD),
        rsi=rsi(closes, RSI_PERIOD),
        volume_avg=avg_volume,
        volume_ratio=ratio,
        cross=cross_state(closes, MA_SHORT_PERIOD, MA_MID_PERIOD),
    )


def _fmt(value: Optional[float], digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def judge(ind: Optional[Indicators]) -> tuple[str, str]:
    """(判定結果, 判定理由) を返す。"""
    if ind is None or ind.close is None:
        return LABEL_NO_DATA, "日足データを取得できませんでした"

    missing: list[str] = []
    if ind.ma_short is None or ind.ma_mid is None:
        missing.append(f"移動平均({MA_SHORT_PERIOD}/{MA_MID_PERIOD}日)")
    if ind.rsi is None:
        missing.append(f"RSI({RSI_PERIOD}日)")
    if ind.volume_ratio is None:
        missing.append(f"出来高平均({VOLUME_AVG_PERIOD}日)")
    if missing:
        return LABEL_NO_DATA, "データ不足のため判定できません: " + ", ".join(missing)

    # --- 買い候補 ---
    buy_checks = [
        (ind.cross == "golden",
         f"ゴールデンクロス({MA_SHORT_PERIOD}日線が{MA_MID_PERIOD}日線を上抜け)",
         f"ゴールデンクロスなし({MA_SHORT_PERIOD}日線{_fmt(ind.ma_short)} / {MA_MID_PERIOD}日線{_fmt(ind.ma_mid)})"),
        (BUY_RSI_MIN <= ind.rsi <= BUY_RSI_MAX,
         f"RSI {_fmt(ind.rsi, 1)} が {BUY_RSI_MIN:g}〜{BUY_RSI_MAX:g} の範囲内",
         f"RSI {_fmt(ind.rsi, 1)} が {BUY_RSI_MIN:g}〜{BUY_RSI_MAX:g} の範囲外"),
        (ind.volume_ratio >= BUY_VOLUME_RATIO_MIN,
         f"出来高比率 {_fmt(ind.volume_ratio)}倍 が {BUY_VOLUME_RATIO_MIN:g}倍以上",
         f"出来高比率 {_fmt(ind.volume_ratio)}倍 が {BUY_VOLUME_RATIO_MIN:g}倍未満"),
    ]
    if all(ok for ok, _, _ in buy_checks):
        return LABEL_BUY, " / ".join(hit for _, hit, _ in buy_checks)

    # --- 売り候補 ---
    sell_checks = [
        (ind.cross == "dead",
         f"デッドクロス({MA_SHORT_PERIOD}日線が{MA_MID_PERIOD}日線を下抜け)",
         f"デッドクロスなし({MA_SHORT_PERIOD}日線{_fmt(ind.ma_short)} / {MA_MID_PERIOD}日線{_fmt(ind.ma_mid)})"),
        (ind.rsi >= SELL_RSI_MIN,
         f"RSI {_fmt(ind.rsi, 1)} が {SELL_RSI_MIN:g} 以上",
         f"RSI {_fmt(ind.rsi, 1)} が {SELL_RSI_MIN:g} 未満"),
    ]
    if all(ok for ok, _, _ in sell_checks):
        return LABEL_SELL, " / ".join(hit for _, hit, _ in sell_checks)

    # --- 様子見（どの条件が外れたかを残す）---
    buy_misses = [miss for ok, _, miss in buy_checks if not ok]
    sell_misses = [miss for ok, _, miss in sell_checks if not ok]
    return LABEL_HOLD, f"買い条件: {', '.join(buy_misses)} ／ 売り条件: {', '.join(sell_misses)}"


# =============================================================================
# ウォッチリスト
# =============================================================================

def load_watchlist(path: Optional[str]) -> list[str]:
    """watchlist.json（{"codes": [...]} または [...]）を読む。無ければ既定値。"""
    candidate = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlist.json")
    if not os.path.exists(candidate):
        if path:
            raise FileNotFoundError(f"ウォッチリストが見つかりません: {path}")
        return list(DEFAULT_WATCHLIST)
    with open(candidate, encoding="utf-8") as fp:
        data = json.load(fp)
    codes = data.get("codes", []) if isinstance(data, dict) else data
    if not codes:
        raise ValueError(f"ウォッチリストに銘柄コードがありません: {candidate}")
    return [str(c) for c in codes]


# =============================================================================
# 1 銘柄分の処理
# =============================================================================

@dataclass
class Result:
    code: str
    name: str = ""
    indicators: Optional[Indicators] = None
    judgement: str = LABEL_NO_DATA
    reason: str = ""
    error: str = ""

    def to_row(self) -> dict[str, Any]:
        ind = self.indicators or Indicators()
        return {
            "code": self.code,
            "name": self.name,
            "date": ind.date,
            "open": _csv_num(ind.open),
            "close": _csv_num(ind.close),
            "volume": _csv_num(ind.volume, 0),
            "ma5": _csv_num(ind.ma_short),
            "ma25": _csv_num(ind.ma_mid),
            "ma75": _csv_num(ind.ma_long),
            "rsi14": _csv_num(ind.rsi, 1),
            "volume_avg20": _csv_num(ind.volume_avg, 0),
            "volume_ratio": _csv_num(ind.volume_ratio),
            "per": _csv_num(ind.per),
            "pbr": _csv_num(ind.pbr),
            "judgement": self.judgement,
            "reason": self.reason,
        }


def _csv_num(value: Optional[float], digits: int = 2) -> str:
    """CSV 用の数値整形。None は空欄にする（Excel/スプレッドシートで扱いやすい）。"""
    if value is None:
        return ""
    return f"{value:.{digits}f}" if digits > 0 else f"{value:.0f}"


def screen_code(source: YFinanceSource, raw_code: str, until: Optional[dt.date] = None,
                with_fundamentals: bool = True, verbose: bool = False) -> Result:
    """1 銘柄分の取得・計算・判定をまとめて行う。"""
    ticker = to_ticker(raw_code)
    result = Result(code=display_code(ticker))

    try:
        bars = source.fetch_bars(ticker, until=until)[-HISTORY_ROWS:]
        if verbose:
            print(f"  [{result.code}] 日足 {len(bars)} 本を取得", file=sys.stderr)

        indicators = build_indicators(bars)
        if with_fundamentals:
            profile = source.fetch_profile(ticker)
            result.name = profile.name
            if indicators is not None:
                indicators.per, indicators.pbr = profile.per, profile.pbr

        result.indicators = indicators
        result.judgement, result.reason = judge(indicators)
    except DataSourceError as exc:
        result.error = str(exc)
        result.judgement = LABEL_NO_DATA
        result.reason = f"取得エラー: {exc}"
    return result


# =============================================================================
# 出力
# =============================================================================

def write_csv(results: Iterable[Result], path: str) -> str:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    # Excel で開いても文字化けしないよう BOM 付き UTF-8 で出力する
    with open(path, "w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for result in results:
            writer.writerow(result.to_row())
    return path


def print_summary(results: list[Result]) -> None:
    order = {LABEL_BUY: 0, LABEL_SELL: 1, LABEL_HOLD: 2, LABEL_NO_DATA: 3}
    print("")
    print(f"{'コード':<8}{'銘柄名':<24}{'判定':<8}理由")
    print("-" * 100)
    for result in sorted(results, key=lambda r: (order.get(r.judgement, 9), r.code)):
        name = (result.name or "")[:20]
        print(f"{result.code:<8}{name:<24}{result.judgement:<8}{result.reason}")
    counts = {label: sum(1 for r in results if r.judgement == label) for label in order}
    print("-" * 100)
    print(" / ".join(f"{label}: {counts[label]}件" for label in order))


# =============================================================================
# CLI
# =============================================================================

def default_output_path(today: dt.date) -> str:
    base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, DEFAULT_OUTPUT_DIR, f"screening_{today:%Y%m%d}.csv")


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="yfinance を使った日本株のルールベース銘柄判定ツール",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--codes", nargs="+", metavar="CODE",
                        help="銘柄コードを直接指定（例: --codes 7203 6758）。省略時はウォッチリストを使用")
    parser.add_argument("--watchlist", metavar="PATH", help="ウォッチリスト JSON のパス")
    parser.add_argument("--output", metavar="PATH", help="CSV の出力先（省略時は output/screening_YYYYMMDD.csv）")
    parser.add_argument("--date", metavar="YYYY-MM-DD",
                        help="基準日（この日までのデータで判定する）。省略時は取得できた最新営業日")
    parser.add_argument("--no-fundamentals", action="store_true",
                        help="銘柄名・PER/PBR の取得をスキップする（通信量を半減できる）")
    parser.add_argument("--interval", type=float, default=REQUEST_INTERVAL_SEC, metavar="SEC",
                        help=f"API 呼び出し間隔の秒数（既定: {REQUEST_INTERVAL_SEC}）。429 が出るときは長くする")
    parser.add_argument("--verbose", "-v", action="store_true", help="処理の詳細を表示する")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None, source: Optional[YFinanceSource] = None) -> int:
    args = parse_args(argv)

    until: Optional[dt.date] = None
    if args.date:
        try:
            until = dt.date.fromisoformat(args.date)
        except ValueError:
            print(f"日付の形式が不正です: {args.date} (YYYY-MM-DD で指定してください)", file=sys.stderr)
            return 2

    try:
        codes = args.codes if args.codes else load_watchlist(args.watchlist)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ウォッチリストの読み込みに失敗しました: {exc}", file=sys.stderr)
        return 2

    source = source or YFinanceSource(interval_sec=args.interval)

    label = f"{until:%Y-%m-%d} まで" if until else "最新営業日"
    print(f"基準日: {label} / 対象 {len(codes)} 銘柄", file=sys.stderr)
    results: list[Result] = []
    for index, code in enumerate(codes, start=1):
        print(f"[{index}/{len(codes)}] {code} を処理中...", file=sys.stderr)
        results.append(
            screen_code(source, code, until=until,
                        with_fundamentals=not args.no_fundamentals,
                        verbose=args.verbose)
        )

    output_path = args.output or default_output_path(dt.date.today())
    write_csv(results, output_path)
    print_summary(results)
    print(f"\nCSV を出力しました: {output_path}")

    errors = [r for r in results if r.error]
    if errors:
        print(f"{len(errors)} 銘柄でエラーが発生しました（CSV には判定不可として記録済み）", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
