"""仮想データの取り口。

乱数の種（seed）を固定すると、毎回まったく同じデータになる。
本物のデータで起きる「意地悪な事情」をわざと混ぜてある（下の SCENARIO を参照）：

- 土日・祝日・年末年始は取引なし（calendar_jp で判定）
- 株式分割：9905（2025-07-01 から 1→2）、9912（2026-04-01 から 1→3）
- ストップ高・ストップ安：9903（2025-03-10, 03-11 に連続ストップ高）、
  9908（2026-02-16 ストップ安）、9914（2026-10-28 ストップ高）。ほかに自然に当たる日もある
- 出来高0（取引不成立）：9917（2025-05-14, 2025-12-03, 2026-10-29）。この日は四本値が空
- 上場が途中から：9919（2026-03-02 上場）／途中で終わる：9920（2025-12-26 が最終日）
- 市場全体の指標役：9999（出来高は全銘柄の合計）

イベントの日付は種に関係なく固定。値動きの細かいところだけが種で変わる。
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Optional

import numpy as np
import pandas as pd

from ..calendar_jp import is_trading_day, trading_days
from ..market_rules import price_limit
from .base import KIND_INDEX, KIND_STOCK, RAW_COLUMNS, DataSource, Instrument, empty_raw_frame

DEFAULT_SEED = 20261102
DEFAULT_START = dt.date(2024, 11, 1)
DEFAULT_END = dt.date(2026, 10, 30)

INDEX_CODE = "9999"
STOCK_CODES = [str(c) for c in range(9901, 9921)]

SCENARIO = {
    "splits": {            # code: (分割の効力発生日, 倍率)
        "9905": ("2025-07-01", 2.0),
        "9912": ("2026-04-01", 3.0),
    },
    "limit_events": {      # code: [(日付, "up" or "down"), ...]
        "9903": [("2025-03-10", "up"), ("2025-03-11", "up")],
        "9908": [("2026-02-16", "down")],
        "9914": [("2026-10-28", "up")],
    },
    "no_trade_days": {     # code: [日付, ...]（出来高0・四本値なし）
        "9917": ["2025-05-14", "2025-12-03", "2026-10-29"],
    },
    "listing": {"9919": "2026-03-02"},   # この日から上場
    "delisting": {"9920": "2025-12-26"},  # この日が最終売買日
    "overrides": {         # 個別の性格づけ（初値, 1日の基準出来高）
        "9917": {"price0": 180.0, "base_volume": 2_000.0},
    },
}


def _d(text: str) -> dt.date:
    return dt.date.fromisoformat(text)


class VirtualSource(DataSource):
    name = "virtual"

    def __init__(self, seed: int = DEFAULT_SEED, start: dt.date = DEFAULT_START,
                 end: dt.date = DEFAULT_END):
        self.seed = seed
        self.start = start
        self.end = end
        self._frames: Optional[dict[str, pd.DataFrame]] = None
        self._check_scenario()

    # ------------------------------------------------------------------ 公開
    def list_instruments(self) -> list[Instrument]:
        items = [Instrument(code=c, name=f"架空銘柄{c[-2:]}", kind=KIND_STOCK) for c in STOCK_CODES]
        items.append(Instrument(code=INDEX_CODE, name="架空市場指数", kind=KIND_INDEX))
        return items

    def fetch_daily(self, code: str, start: Optional[dt.date] = None,
                    end: Optional[dt.date] = None) -> pd.DataFrame:
        frames = self._generate()
        if code not in frames:
            raise KeyError(f"仮想データに無い銘柄コードです: {code}")
        frame = frames[code]
        mask = pd.Series(True, index=frame.index)
        if start is not None:
            mask &= frame["date"] >= start.isoformat()
        if end is not None:
            mask &= frame["date"] <= end.isoformat()
        out = frame[mask].reset_index(drop=True)
        return out.copy() if len(out) else empty_raw_frame()

    # ------------------------------------------------------------------ 中身
    def _check_scenario(self) -> None:
        dates = []
        for d, _ in SCENARIO["splits"].values():
            dates.append(d)
        for events in SCENARIO["limit_events"].values():
            dates += [d for d, _ in events]
        for days in SCENARIO["no_trade_days"].values():
            dates += days
        dates += list(SCENARIO["listing"].values()) + list(SCENARIO["delisting"].values())
        for text in dates:
            if not is_trading_day(_d(text)):
                raise ValueError(f"シナリオの日付が取引日ではありません: {text}")

    def _generate(self) -> dict[str, pd.DataFrame]:
        if self._frames is not None:
            return self._frames
        rng = np.random.default_rng(self.seed)
        days = trading_days(self.start, self.end)
        n = len(days)

        # 市場全体の動き（指標の日次リターン）
        market_ret = rng.normal(0.0003, 0.009, size=n)

        frames: dict[str, pd.DataFrame] = {}
        total_volume = np.zeros(n)
        for code in STOCK_CODES:
            frame, volumes = self._generate_stock(code, days, market_ret, rng)
            frames[code] = frame
            total_volume += volumes
        frames[INDEX_CODE] = self._generate_index(days, market_ret, total_volume, rng)
        self._frames = frames
        return frames

    def _generate_index(self, days, market_ret, total_volume, rng) -> pd.DataFrame:
        rows = []
        prev = 2600.0
        for i, day in enumerate(days):
            close = prev * (1 + market_ret[i])
            open_ = prev * (1 + 0.3 * market_ret[i] + rng.normal(0, 0.002))
            high = max(open_, close) * (1 + abs(rng.normal(0, 0.003)))
            low = min(open_, close) * (1 - abs(rng.normal(0, 0.003)))
            rows.append([day.isoformat(), round(open_, 2), round(high, 2), round(low, 2),
                         round(close, 2), float(total_volume[i]), 1.0])
            prev = close
        return pd.DataFrame(rows, columns=RAW_COLUMNS)

    def _generate_stock(self, code, days, market_ret, rng):
        # 銘柄の性格（種で決まる）。乱数を引く順番を固定するため、上書きがあっても必ず引く
        price0 = float(round(math.exp(rng.uniform(math.log(200), math.log(9000)))))
        beta = rng.uniform(0.6, 1.4)
        vol = rng.uniform(0.012, 0.03)
        base_volume = float(round(10 ** rng.uniform(4.5, 6.5), -2))
        override = SCENARIO["overrides"].get(code, {})
        price0 = override.get("price0", price0)
        base_volume = override.get("base_volume", base_volume)

        split_date, split_ratio = SCENARIO["splits"].get(code, (None, 1.0))
        limit_events = {d: kind for d, kind in SCENARIO["limit_events"].get(code, [])}
        no_trade = set(SCENARIO["no_trade_days"].get(code, []))
        listing = SCENARIO["listing"].get(code)
        delisting = SCENARIO["delisting"].get(code)

        rows = []
        volumes = np.zeros(len(days))
        prev_close: Optional[float] = None
        share_mult = 1.0
        for i, day in enumerate(days):
            iso = day.isoformat()
            # 乱数は毎日同じ数だけ引く（上場前・廃止後でも）→ 銘柄間・日付間でずれない
            noise = rng.normal(0, 1, size=4)
            jump = rng.random() < 0.01
            vol_noise = rng.lognormal(0, 0.4)
            if listing and iso < listing:
                continue
            if delisting and iso > delisting:
                continue

            ratio = split_ratio if iso == split_date else 1.0
            share_mult *= ratio
            if iso in no_trade:
                rows.append([iso, np.nan, np.nan, np.nan, np.nan, 0.0, ratio])
                if prev_close is not None and ratio != 1.0:
                    prev_close = math.floor(prev_close / ratio + 0.5)
                continue

            ret = beta * market_ret[i] + noise[0] * vol * (4.0 if jump else 1.0)
            if prev_close is None:  # 上場初日（またはデータの初日）：値幅制限なし
                base = price0
                lo_lim, hi_lim = 1.0, float("inf")
            else:
                base = prev_close / ratio
                if ratio != 1.0:  # 分割日の基準値段は1円単位に丸める（features と同じ決めごと）
                    base = math.floor(base + 0.5)
                width = price_limit(base)
                lo_lim, hi_lim = math.ceil(max(base - width, 1.0)), math.floor(base + width)

            close = base * (1 + ret)
            open_ = base * (1 + 0.4 * ret + noise[1] * vol * 0.4)
            forced = limit_events.get(iso)
            if forced == "up":
                close = hi_lim
            elif forced == "down":
                close = lo_lim

            def clip(x: float) -> float:
                return float(min(max(round(x), lo_lim), hi_lim))

            close = clip(close)
            open_ = clip(open_)
            high = clip(max(open_, close) * (1 + abs(noise[2]) * vol * 0.5))
            low = clip(min(open_, close) * (1 - abs(noise[3]) * vol * 0.5))
            if forced == "up":
                high = close
            if forced == "down":
                low = close
            high = max(high, open_, close)
            low = min(low, open_, close)

            volume = base_volume * share_mult * vol_noise * (1 + 8 * abs(ret))
            volume = float(max(100.0, round(volume, -2)))
            rows.append([iso, open_, high, low, close, volume, ratio])
            volumes[i] = volume
            prev_close = close
        return pd.DataFrame(rows, columns=RAW_COLUMNS), volumes
