"""2段目の計算を、手で計算した小さな例と突き合わせる。"""

import datetime as dt
import math
import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from warehouse import calendar_jp  # noqa: E402
from warehouse.features import adjustment_factor, compute_features, sma, true_range, wilder_atr  # noqa: E402
from warehouse.market_rules import price_limit  # noqa: E402
from warehouse.sources.base import RAW_COLUMNS  # noqa: E402
from warehouse.validate import RawDataError, validate_raw  # noqa: E402

NAN = float("nan")


def raw_frame(rows):
    """[(date, o, h, l, c, v, split), ...] → 1段目の形"""
    return pd.DataFrame(rows, columns=RAW_COLUMNS).astype({c: "float64" for c in RAW_COLUMNS[1:]})


def dates(n, start=dt.date(2025, 1, 6)):
    return [d.isoformat() for d in calendar_jp.trading_days(start, start + dt.timedelta(days=n * 2 + 10))][:n]


class MovingAverageTest(unittest.TestCase):
    def test_sma_hand_example(self):
        # 1〜6 の5日平均：5本目 (1+2+3+4+5)/5 = 3、6本目 (2+3+4+5+6)/5 = 4
        out = sma(pd.Series([1.0, 2, 3, 4, 5, 6]), 5)
        self.assertTrue(out.iloc[:4].isna().all())
        self.assertEqual(list(out.iloc[4:]), [3.0, 4.0])

    def test_ma_is_null_until_enough_sessions(self):
        n = 30
        rows = [(d, 100 + i, 101 + i, 99 + i, 100 + i, 1000, 1) for i, d in enumerate(dates(n))]
        f = compute_features(raw_frame(rows), "T", "stock")
        self.assertTrue(f["ma25"].iloc[:24].isna().all())
        self.assertAlmostEqual(f["ma25"].iloc[24], sum(100 + i for i in range(25)) / 25)
        self.assertTrue(f["ma75"].isna().all())
        self.assertTrue(f["ma200"].isna().all())
        self.assertTrue(f["above_ma200"].isna().all())
        self.assertTrue(f["ma_alignment"].isna().all())

    def test_ma5_and_change_in_pipeline_frame(self):
        closes = [100, 102, 101, 105, 110, 108]
        rows = [(d, c, c + 1, c - 1, c, 1000, 1) for d, c in zip(dates(6), closes)]
        f = compute_features(raw_frame(rows), "T", "stock")
        self.assertAlmostEqual(f["ma5"].iloc[4], (100 + 102 + 101 + 105 + 110) / 5)
        self.assertAlmostEqual(f["ma5"].iloc[5], (102 + 101 + 105 + 110 + 108) / 5)
        self.assertTrue(math.isnan(f["change_pct"].iloc[0]))
        self.assertAlmostEqual(f["change_pct"].iloc[1], 2.0)
        self.assertAlmostEqual(f["close_vs_ma5_pct"].iloc[5], round((108 / 105.2 - 1) * 100, 4))


class AtrTest(unittest.TestCase):
    # 高値・安値・終値と、手計算した TR（前日終値が要るので1日目は無し）
    H = [10.0, 11, 12, 11, 15]
    L = [8.0, 9, 9, 10, 11]
    C = [9.0, 10, 11, 10.5, 14]
    # d2: max(11-9, |11-9|, |9-9|) = 2
    # d3: max(12-9, |12-10|, |9-10|) = 3
    # d4: max(11-10, |11-11|, |10-11|) = 1
    # d5: max(15-11, |15-10.5|, |11-10.5|) = 4.5
    TR = [NAN, 2.0, 3.0, 1.0, 4.5]

    def test_true_range_hand_example(self):
        tr = true_range(pd.Series(self.H), pd.Series(self.L), pd.Series(self.C))
        self.assertTrue(math.isnan(tr.iloc[0]))
        self.assertEqual(list(tr.iloc[1:]), self.TR[1:])

    def test_wilder_atr_hand_example(self):
        # 期間3：最初 = (2+3+1)/3 = 2、次 = (2*2 + 4.5)/3 = 8.5/3
        atr = wilder_atr(pd.Series(self.TR), 3)
        self.assertTrue(atr.iloc[:3].isna().all())
        self.assertAlmostEqual(atr.iloc[3], 2.0)
        self.assertAlmostEqual(atr.iloc[4], 8.5 / 3)

    def test_atr14_in_features(self):
        n = 20
        rows = [(d, 100, 104, 98, 100 + (i % 2), 1000, 1) for i, d in enumerate(dates(n))]
        f = compute_features(raw_frame(rows), "T", "stock")
        # TR はいつも 104-98 = 6（前日終値 100/101 は高安の内側）→ ATR も 6
        self.assertTrue(f["atr14"].iloc[:14].isna().all())  # TR は2日目から、14本そろうのは15日目
        self.assertAlmostEqual(f["atr14"].iloc[14], 6.0)
        self.assertAlmostEqual(f["atr14"].iloc[-1], 6.0)


class VolumeAndHighLowTest(unittest.TestCase):
    def test_volume_ratio_excludes_today(self):
        vols = [1000] * 20 + [3000]
        rows = [(d, 100, 101, 99, 100, v, 1) for d, v in zip(dates(21), vols)]
        f = compute_features(raw_frame(rows), "T", "stock")
        self.assertTrue(f["volume_ratio20"].iloc[:20].isna().all())
        self.assertAlmostEqual(f["volume_ratio20"].iloc[20], 3.0)

    def test_new_high_low_20(self):
        highs = [110] * 20 + [111, 109]
        lows = [90] * 20 + [91, 89]
        rows = [(d, 100, h, l, 100, 1000, 1) for d, h, l in zip(dates(22), highs, lows)]
        f = compute_features(raw_frame(rows), "T", "stock")
        self.assertTrue(f["new_high20"].iloc[:20].isna().all())
        self.assertEqual(bool(f["new_high20"].iloc[20]), True)
        self.assertEqual(bool(f["new_high20"].iloc[21]), False)
        self.assertEqual(bool(f["new_low20"].iloc[20]), False)
        self.assertEqual(bool(f["new_low20"].iloc[21]), True)


class SplitTest(unittest.TestCase):
    def test_adjustment_factor(self):
        self.assertEqual(list(adjustment_factor(pd.Series([1.0, 1, 2, 1, 3, 1]))), [6.0, 6, 3, 3, 1, 1])

    def test_split_is_adjusted_backward(self):
        d = dates(4)
        rows = [(d[0], 1000, 1000, 1000, 1000, 100, 1), (d[1], 1000, 1000, 1000, 1000, 100, 1),
                (d[2], 500, 510, 500, 510, 200, 2), (d[3], 510, 510, 510, 510, 200, 1)]
        f = compute_features(raw_frame(rows), "T", "stock")
        self.assertEqual(list(f["close_adj"]), [500.0, 500.0, 510.0, 510.0])
        self.assertEqual(list(f["volume_adj"]), [200.0, 200.0, 200.0, 200.0])
        self.assertAlmostEqual(f["change_pct"].iloc[2], 2.0)  # 分割で -49% にならない
        self.assertEqual(f["limit_base"].iloc[2], 500.0)       # 前日1000円 → 分割後の基準 500円


class LimitTest(unittest.TestCase):
    def test_price_limit_table(self):
        self.assertEqual(price_limit(99), 30)
        self.assertEqual(price_limit(100), 50)
        self.assertEqual(price_limit(499), 80)
        self.assertEqual(price_limit(500), 100)
        self.assertEqual(price_limit(2999), 500)
        self.assertEqual(price_limit(3000), 700)

    def test_limit_up_and_down_detection(self):
        d = dates(4)
        rows = [(d[0], 500, 500, 500, 500, 100, 1),
                (d[1], 550, 600, 550, 600, 100, 1),   # 基準500・幅100 → 600 でストップ高
                (d[2], 590, 600, 500, 500, 100, 1),   # 基準600・幅100 → 500 でストップ安（高値600は700に届かない）
                (d[3], 520, 599, 500, 520, 100, 1)]   # 基準500：高値599 はストップ高(600)に届かない
        f = compute_features(raw_frame(rows), "T", "stock")
        self.assertTrue(f["limit_up"].iloc[0] is pd.NA)
        self.assertEqual([bool(x) for x in f["limit_up"].iloc[1:]], [True, False, False])
        self.assertEqual([bool(x) for x in f["limit_down"].iloc[1:]], [False, True, False])
        self.assertEqual([bool(x) for x in f["limit_up_touched"].iloc[1:]], [True, False, False])

    def test_index_has_no_limits(self):
        rows = [(d, 100, 200, 50, 200, 0, 1) for d in dates(3)]
        f = compute_features(raw_frame(rows), "IDX", "index")
        self.assertTrue(f["limit_up"].isna().all())


class NoTradeTest(unittest.TestCase):
    def test_no_trade_day_is_null_and_skipped_in_ma(self):
        d = dates(7)
        rows = [(d[i], c, c, c, c, 100, 1) for i, c in enumerate([10, 11, 12, 13])]
        rows.append((d[4], NAN, NAN, NAN, NAN, 0, 1))
        rows += [(d[5], 14, 14, 14, 14, 100, 1), (d[6], 15, 15, 15, 15, 100, 1)]
        f = compute_features(raw_frame(rows), "T", "stock")
        self.assertTrue(bool(f["no_trade"].iloc[4]))
        self.assertTrue(math.isnan(f["close_adj"].iloc[4]))
        self.assertTrue(math.isnan(f["ma5"].iloc[4]))
        self.assertEqual(f["volume_adj"].iloc[4], 0.0)
        # 5日平均は「成立した5日」: 10,11,12,13,14 → 12
        self.assertAlmostEqual(f["ma5"].iloc[5], 12.0)
        # 不成立日の翌日の前日比は、最後の成立日（13）と比べる
        self.assertAlmostEqual(f["change_pct"].iloc[5], round((14 / 13 - 1) * 100, 4))


class ValidateTest(unittest.TestCase):
    def test_rejects_bad_rows(self):
        d = dates(2)
        with self.assertRaises(RawDataError):
            validate_raw(raw_frame([(d[0], 100, 90, 95, 100, 10, 1)]), "T")      # 高値 < 安値
        with self.assertRaises(RawDataError):
            validate_raw(raw_frame([(d[0], NAN, NAN, NAN, NAN, 10, 1)]), "T")   # 四本値なしで出来高あり
        with self.assertRaises(RawDataError):
            validate_raw(raw_frame([(d[1], 1, 1, 1, 1, 1, 1), (d[0], 1, 1, 1, 1, 1, 1)]), "T")  # 降順
        with self.assertRaises(RawDataError):
            validate_raw(raw_frame([(d[0], 1, 1, 1, 1, 1, 1)])[RAW_COLUMNS[:-1]], "T")  # 列不足

    def test_warns_on_holiday_rows(self):
        warnings = validate_raw(raw_frame([("2025-01-01", 1, 1, 1, 1, 1, 1)]), "T")
        self.assertEqual(len(warnings), 1)


class CalendarTest(unittest.TestCase):
    def test_known_holidays(self):
        closed = ["2025-01-01", "2025-01-02", "2025-01-03", "2025-01-13", "2025-02-11", "2025-02-24",
                  "2025-03-20", "2025-05-06", "2025-07-21", "2025-09-15", "2025-09-23", "2025-10-13",
                  "2025-11-03", "2025-11-24", "2025-12-31", "2026-01-12", "2026-05-06",
                  "2026-09-21", "2026-09-22", "2026-09-23", "2024-11-04", "2024-11-23"]
        for text in closed:
            self.assertFalse(calendar_jp.is_trading_day(dt.date.fromisoformat(text)), text)
        open_days = ["2025-01-06", "2025-02-25", "2025-05-07", "2026-09-24", "2026-10-30"]
        for text in open_days:
            self.assertTrue(calendar_jp.is_trading_day(dt.date.fromisoformat(text)), text)

    def test_out_of_range_year(self):
        with self.assertRaises(ValueError):
            calendar_jp.japanese_holidays(2019)


if __name__ == "__main__":
    unittest.main()
