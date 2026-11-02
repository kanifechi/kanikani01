"""日本の祝日と東証の休場日（外部ライブラリなし）。

現行の祝日法（2020年改正以降の日付）で計算する。対象は 2022〜2099 年。
2019〜2021 年の特例（即位・五輪による移動）は扱わないので、範囲外の年は例外にする。

東証の休場日 = 土日 + 国民の祝日・休日 + 年末年始（12/31, 1/1〜1/3）。
臨時休場（システム障害など）は扱わない。本物のデータでは「データが無い日」として現れる。
"""

from __future__ import annotations

import datetime as dt
from functools import lru_cache

MIN_YEAR = 2022
MAX_YEAR = 2099


def _nth_monday(year: int, month: int, n: int) -> dt.date:
    first = dt.date(year, month, 1)
    offset = (7 - first.weekday()) % 7  # 最初の月曜まで
    return first + dt.timedelta(days=offset + 7 * (n - 1))


def _vernal_equinox_day(year: int) -> int:
    # 1980〜2099 年に有効な近似式（国立天文台の発表と一致する範囲）
    return int(20.8431 + 0.242194 * (year - 1980) - int((year - 1980) / 4))


def _autumnal_equinox_day(year: int) -> int:
    return int(23.2488 + 0.242194 * (year - 1980) - int((year - 1980) / 4))


@lru_cache(maxsize=None)
def japanese_holidays(year: int) -> frozenset[dt.date]:
    """その年の国民の祝日・振替休日・国民の休日。"""
    if not (MIN_YEAR <= year <= MAX_YEAR):
        raise ValueError(f"祝日計算の対象外の年です: {year}（{MIN_YEAR}〜{MAX_YEAR}）")
    base = {
        dt.date(year, 1, 1),                        # 元日
        _nth_monday(year, 1, 2),                    # 成人の日
        dt.date(year, 2, 11),                       # 建国記念の日
        dt.date(year, 2, 23),                       # 天皇誕生日
        dt.date(year, 3, _vernal_equinox_day(year)),  # 春分の日
        dt.date(year, 4, 29),                       # 昭和の日
        dt.date(year, 5, 3),                        # 憲法記念日
        dt.date(year, 5, 4),                        # みどりの日
        dt.date(year, 5, 5),                        # こどもの日
        _nth_monday(year, 7, 3),                    # 海の日
        dt.date(year, 8, 11),                       # 山の日
        _nth_monday(year, 9, 3),                    # 敬老の日
        dt.date(year, 9, _autumnal_equinox_day(year)),  # 秋分の日
        _nth_monday(year, 10, 2),                   # スポーツの日
        dt.date(year, 11, 3),                       # 文化の日
        dt.date(year, 11, 23),                      # 勤労感謝の日
    }
    holidays = set(base)
    # 振替休日：祝日が日曜なら、その後の最初の「祝日でない日」
    for day in sorted(base):
        if day.weekday() == 6:
            sub = day + dt.timedelta(days=1)
            while sub in holidays:
                sub += dt.timedelta(days=1)
            holidays.add(sub)
    # 国民の休日：前後が祝日に挟まれた平日
    for day in sorted(base):
        middle = day + dt.timedelta(days=1)
        after = day + dt.timedelta(days=2)
        if after in base and middle not in holidays and middle.weekday() != 6:
            holidays.add(middle)
    return frozenset(holidays)


def is_trading_day(day: dt.date) -> bool:
    """東証の取引日なら True。"""
    if day.weekday() >= 5:
        return False
    if (day.month, day.day) in {(12, 31), (1, 1), (1, 2), (1, 3)}:
        return False
    return day not in japanese_holidays(day.year)


def trading_days(start: dt.date, end: dt.date) -> list[dt.date]:
    """start〜end（両端含む）の取引日。"""
    days = []
    day = start
    while day <= end:
        if is_trading_day(day):
            days.append(day)
        day += dt.timedelta(days=1)
    return days
