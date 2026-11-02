"""1段目の生データの点検。どの取り口から来たデータにも同じ点検をかける。

本物のAPIに差し替えたとき、形の違うデータが2段目に流れ込んで静かに壊れるのを防ぐ。
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from .calendar_jp import is_trading_day
from .sources.base import RAW_COLUMNS


class RawDataError(ValueError):
    pass


def validate_raw(frame: pd.DataFrame, code: str) -> list[str]:
    """おかしな点があれば RawDataError。注意で済むものは文字列のリストで返す。"""
    problems: list[str] = []
    warnings: list[str] = []
    if list(frame.columns) != RAW_COLUMNS:
        raise RawDataError(f"{code}: 列が約束と違います: {list(frame.columns)}（期待: {RAW_COLUMNS}）")
    if frame.empty:
        return warnings

    dates = frame["date"]
    try:
        parsed = [dt.date.fromisoformat(d) for d in dates]
    except (TypeError, ValueError) as exc:
        raise RawDataError(f"{code}: 日付が YYYY-MM-DD ではありません: {exc}") from exc
    if dates.duplicated().any():
        problems.append(f"同じ日付が重複しています: {sorted(set(dates[dates.duplicated()]))[:5]}")
    if not dates.is_monotonic_increasing:
        problems.append("日付が昇順になっていません")

    prices = frame[["open", "high", "low", "close"]]
    all_missing = prices.isna().all(axis=1)
    partly_missing = prices.isna().any(axis=1) & ~all_missing
    if partly_missing.any():
        problems.append(f"四本値の一部だけが空の日があります: {list(dates[partly_missing])[:5]}")
    vol = frame["volume"]
    if vol.isna().any():
        problems.append(f"出来高が空の日があります: {list(dates[vol.isna()])[:5]}")
    if (vol < 0).any():
        problems.append("出来高がマイナスの日があります")
    bad_no_trade = all_missing & (vol.fillna(0) != 0)
    if bad_no_trade.any():
        problems.append(f"四本値が空なのに出来高がある日があります: {list(dates[bad_no_trade])[:5]}")

    ok = ~all_missing
    p = prices[ok]
    if (p <= 0).any().any():
        problems.append("価格が0以下の日があります")
    tol = 1e-9
    if (p["high"] + tol < p[["open", "close", "low"]].max(axis=1)).any():
        problems.append("高値が始値・終値・安値より低い日があります")
    if (p["low"] - tol > p[["open", "close", "high"]].min(axis=1)).any():
        problems.append("安値が始値・終値・高値より高い日があります")
    ratio = frame["split_ratio"]
    if ratio.isna().any() or (ratio <= 0).any():
        problems.append("split_ratio が空か0以下の日があります")

    if problems:
        raise RawDataError(f"{code}: " + " / ".join(problems))

    non_trading = [d for d, day in zip(dates, parsed) if not is_trading_day(day)]
    if non_trading:
        warnings.append(f"{code}: 東証の休場日にデータがあります（臨時営業・カレンダー違いの可能性）: {non_trading[:5]}")
    return warnings
