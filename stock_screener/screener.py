#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日本株ルールベース銘柄判定ツール (J-Quants API / CSV 出力版)

ウォッチリストの銘柄について J-Quants API から日足を取得し、
移動平均・RSI・出来高比率・PER/PBR を計算して「買い候補 / 売り候補 / 様子見」を判定、
結果を CSV に書き出す単体スクリプト。

使い方:
    export JQUANTS_MAIL_ADDRESS="you@example.com"
    export JQUANTS_PASSWORD="********"
    python screener.py                       # watchlist.json の銘柄を判定して output/ に CSV 出力
    python screener.py --codes 7203 6758     # 銘柄コードを直接指定
    python screener.py --output result.csv   # 出力先を指定

注意:
    J-Quants の無料プランはデータが約 12 週間遅延する。
    「当日」とは「取得できた最新営業日」を指す。
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Iterable, Optional

import requests

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

# --- データ取得 ---
HISTORY_ROWS = 90          # 指標計算に使う日足の本数（営業日ベース）
FETCH_CALENDAR_DAYS = 400  # 上記本数を確保するために遡る暦日数（休場日を考慮した余裕込み）
API_INTERVAL_SEC = 0.2     # API 連続呼び出しの間隔（無料プランへの配慮）
REQUEST_TIMEOUT_SEC = 30

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

JQUANTS_BASE_URL = os.environ.get("JQUANTS_BASE_URL", "https://api.jquants.com/v1")


# =============================================================================
# J-Quants API クライアント
# =============================================================================

class JQuantsError(RuntimeError):
    """J-Quants API 関連のエラー。"""


class JQuantsClient:
    """J-Quants API の薄いラッパー。

    認証は環境変数から読み込む（優先順）:
      1. JQUANTS_ID_TOKEN                          … ID トークン直指定（有効期間 24 時間）
      2. JQUANTS_REFRESH_TOKEN                     … リフレッシュトークン（有効期間 1 週間）
      3. JQUANTS_MAIL_ADDRESS + JQUANTS_PASSWORD   … メール/パスワードから自動取得
    """

    def __init__(self, id_token: str, interval_sec: float = API_INTERVAL_SEC) -> None:
        self._id_token = id_token
        self._interval_sec = interval_sec
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"Bearer {id_token}"})

    # ---- 認証 -------------------------------------------------------------

    @classmethod
    def from_env(cls, interval_sec: float = API_INTERVAL_SEC) -> "JQuantsClient":
        id_token = os.environ.get("JQUANTS_ID_TOKEN", "").strip()
        if id_token:
            return cls(id_token, interval_sec)

        refresh_token = os.environ.get("JQUANTS_REFRESH_TOKEN", "").strip()
        if not refresh_token:
            mail = os.environ.get("JQUANTS_MAIL_ADDRESS", "").strip()
            password = os.environ.get("JQUANTS_PASSWORD", "")
            if not mail or not password:
                raise JQuantsError(
                    "認証情報が見つかりません。以下のいずれかを環境変数に設定してください:\n"
                    "  - JQUANTS_ID_TOKEN\n"
                    "  - JQUANTS_REFRESH_TOKEN\n"
                    "  - JQUANTS_MAIL_ADDRESS と JQUANTS_PASSWORD"
                )
            refresh_token = cls._fetch_refresh_token(mail, password)

        return cls(cls._fetch_id_token(refresh_token), interval_sec)

    @staticmethod
    def _fetch_refresh_token(mail: str, password: str) -> str:
        url = f"{JQUANTS_BASE_URL}/token/auth_user"
        res = requests.post(
            url,
            data=json.dumps({"mailaddress": mail, "password": password}),
            timeout=REQUEST_TIMEOUT_SEC,
        )
        if res.status_code != 200:
            raise JQuantsError(f"リフレッシュトークンの取得に失敗しました ({res.status_code}): {res.text}")
        token = res.json().get("refreshToken")
        if not token:
            raise JQuantsError("レスポンスに refreshToken が含まれていません。")
        return token

    @staticmethod
    def _fetch_id_token(refresh_token: str) -> str:
        query = urllib.parse.urlencode({"refreshtoken": refresh_token})
        url = f"{JQUANTS_BASE_URL}/token/auth_refresh?{query}"
        res = requests.post(url, timeout=REQUEST_TIMEOUT_SEC)
        if res.status_code != 200:
            raise JQuantsError(f"ID トークンの取得に失敗しました ({res.status_code}): {res.text}")
        token = res.json().get("idToken")
        if not token:
            raise JQuantsError("レスポンスに idToken が含まれていません。")
        return token

    # ---- 低レベル GET -----------------------------------------------------

    def _get(self, path: str, params: dict[str, Any], collect_key: str) -> list[dict[str, Any]]:
        """ページネーションを解決しつつ GET し、collect_key の配列を結合して返す。"""
        rows: list[dict[str, Any]] = []
        params = dict(params)
        while True:
            time.sleep(self._interval_sec)
            res = self._session.get(
                f"{JQUANTS_BASE_URL}{path}", params=params, timeout=REQUEST_TIMEOUT_SEC
            )
            if res.status_code != 200:
                raise JQuantsError(f"{path} でエラー ({res.status_code}): {res.text}")
            payload = res.json()
            rows.extend(payload.get(collect_key, []))
            pagination_key = payload.get("pagination_key")
            if not pagination_key:
                return rows
            params["pagination_key"] = pagination_key

    # ---- 各エンドポイント -------------------------------------------------

    def get_daily_quotes(self, code: str, date_from: dt.date, date_to: dt.date) -> list[dict[str, Any]]:
        """日足（始値・終値・出来高など）を日付昇順で返す。"""
        rows = self._get(
            "/prices/daily_quotes",
            {
                "code": code,
                "from": date_from.strftime("%Y-%m-%d"),
                "to": date_to.strftime("%Y-%m-%d"),
            },
            "daily_quotes",
        )
        return sorted(rows, key=lambda r: r.get("Date", ""))

    def get_listed_info(self, code: str) -> dict[str, Any]:
        """銘柄基本情報（銘柄名など）。取得できなければ空 dict。"""
        try:
            rows = self._get("/listed/info", {"code": code}, "info")
        except JQuantsError:
            return {}
        return rows[-1] if rows else {}

    def get_statements(self, code: str) -> list[dict[str, Any]]:
        """財務諸表（EPS/BPS を含む）。取得できなければ空リスト。"""
        try:
            rows = self._get("/fins/statements", {"code": code}, "statements")
        except JQuantsError:
            return []
        return sorted(rows, key=lambda r: r.get("DisclosedDate", ""))


# =============================================================================
# 指標計算
# =============================================================================

@dataclass
class Bar:
    """日足 1 本分（調整後の値を優先して保持する）。"""

    date: str
    open: Optional[float]
    close: float
    volume: Optional[float]


def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_bars(rows: Iterable[dict[str, Any]]) -> list[Bar]:
    """J-Quants の daily_quotes を Bar のリストに変換する。

    株式分割の影響を避けるため Adjustment* （調整後）を優先し、無ければ生値を使う。
    終値が無い日（売買停止など）はスキップする。
    """
    bars: list[Bar] = []
    for row in rows:
        close = _to_float(row.get("AdjustmentClose"))
        if close is None:
            close = _to_float(row.get("Close"))
        if close is None:
            continue
        open_ = _to_float(row.get("AdjustmentOpen"))
        if open_ is None:
            open_ = _to_float(row.get("Open"))
        volume = _to_float(row.get("AdjustmentVolume"))
        if volume is None:
            volume = _to_float(row.get("Volume"))
        bars.append(Bar(date=str(row.get("Date", "")), open=open_, close=close, volume=volume))
    return bars


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
# PER / PBR（取得できた場合のみ）
# =============================================================================

def _pick_number(statement: dict[str, Any], keys: list[str]) -> Optional[float]:
    for key in keys:
        value = _to_float(statement.get(key))
        if value is not None:
            return value
    return None


def calc_valuation(statements: list[dict[str, Any]], close: float
                   ) -> tuple[Optional[float], Optional[float]]:
    """直近の開示から EPS / BPS を拾って PER・PBR を算出する。

    J-Quants の財務諸表には PER/PBR そのものが無いため、終値から計算する。
    EPS/BPS が取れない、または 0 以下の場合は None（＝出力は空欄）。
    """
    per = pbr = None
    for statement in reversed(statements):  # 新しい開示から順に探す
        if per is None:
            eps = _pick_number(statement, [
                "EarningsPerShare",
                "ForecastEarningsPerShare",
                "NextYearForecastEarningsPerShare",
            ])
            if eps is not None and eps > 0:
                per = close / eps
        if pbr is None:
            bps = _pick_number(statement, ["BookValuePerShare"])
            if bps is not None and bps > 0:
                pbr = close / bps
        if per is not None and pbr is not None:
            break
    return per, pbr


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
    """日足リストから各指標を計算する。データ不足なら None。"""
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
        per=None,
        pbr=None,
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
# 銘柄コードとウォッチリスト
# =============================================================================

def normalize_code(code: str) -> str:
    """J-Quants の 5 桁コードに正規化する（7203 -> 72030）。"""
    code = str(code).strip().upper()
    if len(code) == 4:
        return code + "0"
    return code


def display_code(code: str) -> str:
    """表示用の 4 桁コードに戻す（72030 -> 7203）。"""
    code = str(code).strip().upper()
    if len(code) == 5 and code.endswith("0"):
        return code[:-1]
    return code


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


def screen_code(client: JQuantsClient, raw_code: str, today: dt.date,
                with_fundamentals: bool = True, verbose: bool = False) -> Result:
    """1 銘柄分の取得・計算・判定をまとめて行う。"""
    code = normalize_code(raw_code)
    result = Result(code=display_code(code))

    try:
        date_from = today - dt.timedelta(days=FETCH_CALENDAR_DAYS)
        rows = client.get_daily_quotes(code, date_from, today)
        bars = parse_bars(rows)[-HISTORY_ROWS:]
        if verbose:
            print(f"  [{result.code}] 日足 {len(bars)} 本を取得", file=sys.stderr)

        info = client.get_listed_info(code)
        result.name = info.get("CompanyName") or info.get("CompanyNameEnglish") or ""

        indicators = build_indicators(bars)
        if indicators is not None and with_fundamentals and indicators.close is not None:
            statements = client.get_statements(code)
            indicators.per, indicators.pbr = calc_valuation(statements, indicators.close)

        result.indicators = indicators
        result.judgement, result.reason = judge(indicators)
    except JQuantsError as exc:
        result.error = str(exc)
        result.judgement = LABEL_NO_DATA
        result.reason = f"取得エラー: {exc}"
    return result


# =============================================================================
# 出力
# =============================================================================

def write_csv(results: list[Result], path: str) -> str:
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
        description="J-Quants API を使った日本株のルールベース銘柄判定ツール",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--codes", nargs="+", metavar="CODE",
                        help="銘柄コードを直接指定（例: --codes 7203 6758）。省略時はウォッチリストを使用")
    parser.add_argument("--watchlist", metavar="PATH", help="ウォッチリスト JSON のパス")
    parser.add_argument("--output", metavar="PATH", help="CSV の出力先（省略時は output/screening_YYYYMMDD.csv）")
    parser.add_argument("--date", metavar="YYYY-MM-DD",
                        help="基準日（この日までのデータで判定する）。省略時は本日")
    parser.add_argument("--no-fundamentals", action="store_true",
                        help="PER/PBR の取得をスキップする（API 呼び出しを減らせる）")
    parser.add_argument("--verbose", "-v", action="store_true", help="処理の詳細を表示する")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    try:
        today = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    except ValueError:
        print(f"日付の形式が不正です: {args.date} (YYYY-MM-DD で指定してください)", file=sys.stderr)
        return 2

    try:
        codes = args.codes if args.codes else load_watchlist(args.watchlist)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ウォッチリストの読み込みに失敗しました: {exc}", file=sys.stderr)
        return 2

    try:
        client = JQuantsClient.from_env()
    except JQuantsError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except requests.RequestException as exc:
        print(f"認証時に通信エラーが発生しました: {exc}", file=sys.stderr)
        return 1

    print(f"基準日: {today:%Y-%m-%d} / 対象 {len(codes)} 銘柄", file=sys.stderr)
    results: list[Result] = []
    for index, code in enumerate(codes, start=1):
        print(f"[{index}/{len(codes)}] {code} を処理中...", file=sys.stderr)
        try:
            results.append(
                screen_code(client, code, today,
                            with_fundamentals=not args.no_fundamentals,
                            verbose=args.verbose)
            )
        except requests.RequestException as exc:
            results.append(Result(code=display_code(normalize_code(code)),
                                  judgement=LABEL_NO_DATA,
                                  reason=f"通信エラー: {exc}",
                                  error=str(exc)))

    output_path = args.output or default_output_path(today)
    write_csv(results, output_path)
    print_summary(results)
    print(f"\nCSV を出力しました: {output_path}")

    errors = [r for r in results if r.error]
    if errors:
        print(f"{len(errors)} 銘柄でエラーが発生しました（CSV には判定不可として記録済み）", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
