"""2段目：1段目の生データから計算済みの数字を作る。

決めごと（README にも同じことを書いている）：
- 株式分割は「後ろから調整」する。分割より前の価格を倍率で割り、出来高を倍率で掛ける。
  → 最新日の調整後価格は、その日の実際の値段と同じになる。
  → 新しい分割が起きると過去の調整後価格が変わるので、2段目は毎回その銘柄の全期間を計算し直す。
- 取引不成立の日（四本値が空）は「価格の数字」をすべて空にする。前の日の値で埋めない。
  移動平均などの「N日」は、取引が成立した日をN回数える。
- 出来高の数字は取引不成立の日も含める（出来高0は本当の値なので）。
- データが足りない日（移動平均200日なら成立日が200日に満たない間）は空（null）。
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .market_rules import price_limit
from .sources.base import KIND_INDEX

MA_PERIODS = (5, 25, 75, 200)
ATR_PERIOD = 14
VOLUME_AVG_PERIOD = 20
HIGHLOW_PERIOD = 20
ROUND_DIGITS = 4

FEATURE_COLUMNS = (
    ["date", "code", "no_trade", "split_ratio", "adj_factor",
     "open_adj", "high_adj", "low_adj", "close_adj", "volume_adj", "sessions", "change_pct"]
    + [f"ma{n}" for n in MA_PERIODS]
    + [f"close_vs_ma{n}_pct" for n in MA_PERIODS]
    + [f"above_ma{n}" for n in MA_PERIODS]
    + ["ma_alignment",
       "volume_avg20_prev", "volume_ratio20",
       "high20_prev", "low20_prev", "new_high20", "new_low20",
       "tr", "atr14", "atr14_pct",
       "limit_base", "limit_width", "limit_up", "limit_down", "limit_up_touched", "limit_down_touched"]
)
BOOL_COLUMNS = (["no_trade"] + [f"above_ma{n}" for n in MA_PERIODS]
                + ["new_high20", "new_low20", "limit_up", "limit_down",
                   "limit_up_touched", "limit_down_touched"])


# ---------------------------------------------------------------------- 計算の部品
def sma(values: pd.Series, period: int) -> pd.Series:
    """単純移動平均。period 本そろわない間は空。"""
    return values.rolling(period, min_periods=period).mean()


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """真の値幅 = max(高値-安値, |高値-前日終値|, |安値-前日終値|)。前日終値が無い日は空。"""
    prev = close.shift(1)
    parts = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1)
    tr = parts.max(axis=1)
    tr[prev.isna()] = np.nan
    return tr


def wilder_atr(tr: pd.Series, period: int) -> pd.Series:
    """ATR（ワイルダー方式）。最初の値 = 最初の period 本の TR の単純平均、
    以降 ATR_t = (ATR_{t-1} * (period-1) + TR_t) / period。"""
    values = tr.to_numpy(dtype=float)
    out = np.full(len(values), np.nan)
    collected: list[float] = []
    prev = math.nan
    for i, v in enumerate(values):
        if math.isnan(v):
            if not math.isnan(prev):
                raise ValueError("ATRの計算途中にTRの欠けがあります")
            continue
        if math.isnan(prev):
            collected.append(v)
            if len(collected) == period:
                prev = sum(collected) / period
                out[i] = prev
        else:
            prev = (prev * (period - 1) + v) / period
            out[i] = prev
    return pd.Series(out, index=tr.index)


def adjustment_factor(split_ratio: pd.Series) -> pd.Series:
    """各日の「その日より後に起きた分割の倍率の積」。調整後 = 実際の値 ÷ これ。"""
    r = split_ratio.to_numpy(dtype=float)
    suffix = np.cumprod(r[::-1])[::-1]  # その日以降の積
    return pd.Series(suffix / r, index=split_ratio.index)


def _bool(series: pd.Series, valid: pd.Series) -> pd.Series:
    out = series.astype("boolean")
    out[~valid] = pd.NA
    return out


# ---------------------------------------------------------------------- 本体
def compute_features(raw: pd.DataFrame, code: str, kind: str) -> pd.DataFrame:
    raw = raw.reset_index(drop=True)
    out = pd.DataFrame(index=raw.index)
    out["date"] = raw["date"]
    out["code"] = code
    factor = adjustment_factor(raw["split_ratio"])
    no_trade = raw["close"].isna()
    out["no_trade"] = no_trade.astype("boolean")
    out["split_ratio"] = raw["split_ratio"]
    out["adj_factor"] = factor
    for col in ("open", "high", "low", "close"):
        out[f"{col}_adj"] = raw[col] / factor
    out["volume_adj"] = raw["volume"] * factor

    # 出来高（取引不成立の日も含めて数える）
    vol_avg = out["volume_adj"].shift(1).rolling(VOLUME_AVG_PERIOD, min_periods=VOLUME_AVG_PERIOD).mean()
    out["volume_avg20_prev"] = vol_avg
    out["volume_ratio20"] = (out["volume_adj"] / vol_avg).where(vol_avg > 0)

    # 価格の数字は取引が成立した日だけで計算し、あとで全日に戻す
    v = out[~no_trade].copy()
    close, high, low = v["close_adj"], v["high_adj"], v["low_adj"]
    v["sessions"] = np.arange(1, len(v) + 1, dtype=float)
    prev_close = close.shift(1)
    v["change_pct"] = (close / prev_close - 1) * 100
    for n in MA_PERIODS:
        ma = sma(close, n)
        v[f"ma{n}"] = ma
        v[f"close_vs_ma{n}_pct"] = (close / ma - 1) * 100
        v[f"above_ma{n}"] = _bool(close > ma, ma.notna())
    v["high20_prev"] = high.shift(1).rolling(HIGHLOW_PERIOD, min_periods=HIGHLOW_PERIOD).max()
    v["low20_prev"] = low.shift(1).rolling(HIGHLOW_PERIOD, min_periods=HIGHLOW_PERIOD).min()
    v["new_high20"] = _bool(high > v["high20_prev"], v["high20_prev"].notna())
    v["new_low20"] = _bool(low < v["low20_prev"], v["low20_prev"].notna())
    v["tr"] = true_range(high, low, close)
    v["atr14"] = wilder_atr(v["tr"], ATR_PERIOD)
    v["atr14_pct"] = v["atr14"] / close * 100

    # 値幅制限（実際の値段で判定。基準値段 = 前の成立日の終値を今日の株数に直したもの）
    if kind != KIND_INDEX:
        f_today = v["adj_factor"]
        f_prev = f_today.shift(1)
        base = prev_close * f_today
        split_between = (f_prev != f_today) & f_prev.notna()
        base = base.where(~split_between, np.floor(base + 0.5))  # 分割日は1円単位に丸める
        width = base.map(lambda b: price_limit(b) if pd.notna(b) else np.nan)
        upper = base + width
        lower = (base - width).clip(lower=1.0)
        close_raw, high_raw, low_raw = close * f_today, high * f_today, low * f_today
        has_base = base.notna()
        eps = 1e-6
        v["limit_base"] = base
        v["limit_width"] = width
        v["limit_up"] = _bool(close_raw >= upper - eps, has_base)
        v["limit_down"] = _bool(close_raw <= lower + eps, has_base)
        v["limit_up_touched"] = _bool(high_raw >= upper - eps, has_base)
        v["limit_down_touched"] = _bool(low_raw <= lower + eps, has_base)

    for col in FEATURE_COLUMNS:
        if col in out.columns:
            continue
        if col in v.columns:
            out[col] = v[col].reindex(out.index)
        else:
            out[col] = pd.Series(pd.NA, index=out.index, dtype="boolean") if col in BOOL_COLUMNS else np.nan
    for col in BOOL_COLUMNS:
        out[col] = out[col].astype("boolean")

    ma5, ma25, ma75 = out["ma5"], out["ma25"], out["ma75"]
    align = pd.Series(pd.NA, index=out.index, dtype="object")
    have = ma5.notna() & ma25.notna() & ma75.notna()
    align[have] = "mixed"
    align[have & (ma5 > ma25) & (ma25 > ma75)] = "bull"
    align[have & (ma5 < ma25) & (ma25 < ma75)] = "bear"
    out["ma_alignment"] = align

    out = out[FEATURE_COLUMNS]
    float_cols = [c for c in FEATURE_COLUMNS if c not in BOOL_COLUMNS and c not in ("date", "code", "ma_alignment")]
    out[float_cols] = out[float_cols].astype(float).round(ROUND_DIGITS)
    return out


def read_features_csv(path) -> pd.DataFrame:
    frame = pd.read_csv(path, dtype={"date": str, "code": str, "ma_alignment": str}, encoding="utf-8")
    for col in BOOL_COLUMNS:
        frame[col] = frame[col].astype("boolean")
    return frame
