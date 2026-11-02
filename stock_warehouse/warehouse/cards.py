"""3段目：2段目から判定用カード（1銘柄1枚の短いJSON）を作る。"""

from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from .features import MA_PERIODS
from .sources.base import KIND_INDEX, Instrument

SCHEMA_VERSION = "1.0"
CARD_MAX_BYTES = 8192
RECENT_DAYS = 10
SCHEMA_PATH = Path(__file__).parent / "schemas" / "card.schema.json"


class CardError(ValueError):
    pass


@lru_cache(maxsize=1)
def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def validate_card(card: dict) -> int:
    """スキーマに合うか・8KB以下かを確かめ、バイト数を返す。"""
    import jsonschema  # requirements.txt に入れてある

    try:
        jsonschema.validate(card, load_schema(), cls=jsonschema.Draft202012Validator)
    except jsonschema.ValidationError as exc:
        raise CardError(f"{card.get('code')}: スキーマに合いません: {exc.message}") from exc
    size = len(card_to_text(card).encode("utf-8"))
    if size > CARD_MAX_BYTES:
        raise CardError(f"{card.get('code')}: カードが {size} バイトで上限 {CARD_MAX_BYTES} を超えています")
    return size


def card_to_text(card: dict) -> str:
    return json.dumps(card, ensure_ascii=False, indent=1) + "\n"


# ---------------------------------------------------------------------- 値の変換
def _num(value: Any, digits: int = 2) -> Optional[float]:
    if value is None or value is pd.NA:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f):
        return None
    out = round(f, digits)
    return 0.0 if out == 0 else out


def _int(value: Any) -> Optional[int]:
    f = _num(value, 0)
    return None if f is None else int(f)


def _flag(value: Any) -> Optional[bool]:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return bool(value)


def _str(value: Any) -> Optional[str]:
    if value is None or value is pd.NA or (isinstance(value, float) and math.isnan(value)):
        return None
    return str(value)


def _pct_change(now: Any, before: Any) -> Optional[float]:
    a, b = _num(now, 6), _num(before, 6)
    if a is None or b is None or b == 0:
        return None
    return _num((a / b - 1) * 100)


def _count(series: pd.Series, window: int) -> Optional[int]:
    tail = series.tail(window)
    if len(tail) < window or tail.isna().any():
        return None
    return int(tail.astype(bool).sum())


# ---------------------------------------------------------------------- 本体
def market_summary(index_features: Optional[pd.DataFrame], code: str, as_of: str) -> Optional[dict]:
    if index_features is None:
        return None
    rows = index_features[(index_features["date"] <= as_of) & ~index_features["no_trade"].fillna(False).astype(bool)]
    if rows.empty:
        return {"code": code, "date": None, "change_pct": None, "change_5d_pct": None,
                "close_vs_ma25_pct": None, "close_vs_ma200_pct": None, "ma_alignment": None}
    last = rows.iloc[-1]
    before5 = rows.iloc[-6]["close_adj"] if len(rows) >= 6 else None
    return {
        "code": code,
        "date": last["date"],
        "change_pct": _num(last["change_pct"]),
        "change_5d_pct": _pct_change(last["close_adj"], before5),
        "close_vs_ma25_pct": _num(last["close_vs_ma25_pct"]),
        "close_vs_ma200_pct": _num(last["close_vs_ma200_pct"]),
        "ma_alignment": _str(last["ma_alignment"]),
    }


def build_card(features: pd.DataFrame, instrument: Instrument, as_of: str, source_name: str,
               market: Optional[dict]) -> Optional[dict]:
    """as_of の日に行が無い銘柄（上場前・廃止後・休場日）は None。"""
    hist = features[features["date"] <= as_of]
    if hist.empty or hist.iloc[-1]["date"] != as_of:
        return None
    today = hist.iloc[-1]
    no_trade = hist["no_trade"].fillna(False).astype(bool)
    valid = hist[~no_trade]
    notes: list[str] = []

    if valid.empty:
        raise CardError(f"{instrument.code}: {as_of} までに取引が成立した日がありません")
    last = valid.iloc[-1]
    if bool(today["no_trade"]):
        notes.append(f"{as_of} は取引不成立（出来高0）。latest は最後の成立日 {last['date']} の値")
    sessions = int(len(valid))
    for n in MA_PERIODS:
        if sessions < n:
            notes.append(f"ma{n} は成立日が{n}日に満たないため null（{sessions}日）")
    first_date = hist.iloc[0]["date"]
    splits = hist[hist["split_ratio"] != 1.0]
    last_split = splits.iloc[-1]["date"] if len(splits) else None
    if last_split:
        ratio = splits.iloc[-1]["split_ratio"]
        notes.append(f"{last_split} に1→{ratio:g}の株式分割。過去の価格・出来高は分割調整済み")
    no_trade_20 = int(no_trade.tail(20).sum())
    if no_trade_20:
        notes.append(f"直近20営業日に取引不成立の日が{no_trade_20}日")

    def ma_block(n: int) -> dict:
        return {"value": _num(last[f"ma{n}"]), "close_vs_pct": _num(last[f"close_vs_ma{n}_pct"]),
                "above": _flag(last[f"above_ma{n}"])}

    latest = {
        "date": last["date"],
        "open": _num(last["open_adj"]), "high": _num(last["high_adj"]),
        "low": _num(last["low_adj"]), "close": _num(last["close_adj"]),
        "volume": _int(last["volume_adj"]),
        "change_pct": _num(last["change_pct"]),
        **{f"ma{n}": ma_block(n) for n in MA_PERIODS},
        "ma_alignment": _str(last["ma_alignment"]),
        "volume_ratio20": _num(last["volume_ratio20"]),
        "high20_prev": _num(last["high20_prev"]), "low20_prev": _num(last["low20_prev"]),
        "new_high20": _flag(last["new_high20"]), "new_low20": _flag(last["new_low20"]),
        "atr14": _num(last["atr14"]), "atr14_pct": _num(last["atr14_pct"]),
        "limit_up": _flag(last["limit_up"]), "limit_down": _flag(last["limit_down"]),
        "limit_up_touched": _flag(last["limit_up_touched"]),
        "limit_down_touched": _flag(last["limit_down_touched"]),
    }

    recent = [
        {
            "date": row["date"], "close": _num(row["close_adj"]), "change_pct": _num(row["change_pct"]),
            "volume_ratio20": _num(row["volume_ratio20"]), "no_trade": bool(row["no_trade"]),
            "limit_up": _flag(row["limit_up"]), "limit_down": _flag(row["limit_down"]),
            "new_high20": _flag(row["new_high20"]), "new_low20": _flag(row["new_low20"]),
        }
        for _, row in hist.tail(RECENT_DAYS).iterrows()
    ]

    closes = valid["close_adj"]
    ma25 = valid["ma25"]
    diffs = closes.diff().tail(5)
    have5 = len(valid) >= 6
    trend = {
        "change_5d_pct": _pct_change(closes.iloc[-1], closes.iloc[-6]) if have5 else None,
        "change_20d_pct": _pct_change(closes.iloc[-1], closes.iloc[-21]) if len(valid) >= 21 else None,
        "ma25_slope_5d_pct": _pct_change(ma25.iloc[-1], ma25.iloc[-6]) if have5 else None,
        "up_days_5": int((diffs > 0).sum()) if have5 else None,
        "down_days_5": int((diffs < 0).sum()) if have5 else None,
        "limit_up_days_20": _count(valid["limit_up"], 20) if instrument.kind != KIND_INDEX else None,
        "limit_down_days_20": _count(valid["limit_down"], 20) if instrument.kind != KIND_INDEX else None,
        "new_high20_days_20": _count(valid["new_high20"], 20),
        "new_low20_days_20": _count(valid["new_low20"], 20),
    }

    card = {
        "schema_version": SCHEMA_VERSION,
        "code": instrument.code,
        "name": instrument.name,
        "kind": instrument.kind,
        "as_of": as_of,
        "source": source_name,
        "data": {"first_date": first_date, "sessions": sessions, "no_trade_today": bool(today["no_trade"]),
                 "last_split_date": last_split, "notes": notes[:12]},
        "latest": latest,
        "recent": recent,
        "trend": trend,
        "market": market,
    }
    validate_card(card)
    return card
