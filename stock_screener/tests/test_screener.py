#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""指標計算と判定ロジックのテスト（API 認証情報なしで実行できる）。

実行:
    python -m unittest discover -s tests -v
"""

import contextlib
import csv
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import screener as sc  # noqa: E402


# 最終日にゴールデンクロス / デッドクロスが成立する検証用の終値系列
GOLDEN_CROSS_CLOSES = [100.0 - i * 0.3 for i in range(80)] + [140.0]
DEAD_CROSS_CLOSES = [100.0 + i * 1.0 for i in range(80)] + [100.0]

# 「下落トレンドが底打ちし、緩やかに戻して最終日にゴールデンクロス」という
# 買い候補の3条件（GC / RSI 30〜60 / 出来高1.5倍以上）が揃う現実的な値動き
BUY_SIGNAL_CLOSES = (
    [100.0 - 0.5 * i for i in range(85)]
    + [100.0 - 0.5 * 84 + 0.5 * j for j in range(1, 10)]
)


def build_bars(closes, volumes=None):
    """終値（と出来高）のリストから Bar のリストを作るテスト用ヘルパー。"""
    volumes = volumes if volumes is not None else [1000.0] * len(closes)
    return [
        sc.Bar(date=f"2025-01-{i + 1:02d}", open=c, close=c, volume=v)
        for i, (c, v) in enumerate(zip(closes, volumes))
    ]


class TestSma(unittest.TestCase):
    def test_latest_and_previous(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        self.assertAlmostEqual(sc.sma(values, 5), 3.0)
        self.assertAlmostEqual(sc.sma(values, 2), 4.5)          # (4+5)/2
        self.assertAlmostEqual(sc.sma(values, 2, offset=1), 3.5)  # (3+4)/2

    def test_returns_none_when_not_enough_data(self):
        self.assertIsNone(sc.sma([1.0, 2.0], 5))
        self.assertIsNone(sc.sma([1.0, 2.0, 3.0], 3, offset=1))


class TestRsi(unittest.TestCase):
    def test_all_up_days_is_100(self):
        self.assertAlmostEqual(sc.rsi([float(i) for i in range(1, 30)], 14), 100.0)

    def test_all_down_days_is_0(self):
        self.assertAlmostEqual(sc.rsi([float(i) for i in range(30, 1, -1)], 14), 0.0)

    def test_known_value(self):
        # 手計算で検証できるケース（period=2）
        # 値動き: +1, -1, +1, -1
        #   初期  : 平均上昇 0.5 / 平均下落 0.5
        #   3本目 : (0.5+1)/2 = 0.75 / (0.5+0)/2 = 0.25
        #   4本目 : (0.75+0)/2 = 0.375 / (0.25+1)/2 = 0.625
        #   RS = 0.6 -> RSI = 100 - 100/1.6 = 37.5
        self.assertAlmostEqual(sc.rsi([10.0, 11.0, 10.0, 11.0, 10.0], 2), 37.5)

    def test_alternating_series_is_near_50(self):
        values = []
        price = 100.0
        for i in range(40):
            price += 1.0 if i % 2 == 0 else -1.0
            values.append(price)
        self.assertAlmostEqual(sc.rsi(values, 14), 50.0, delta=5.0)

    def test_returns_none_when_not_enough_data(self):
        self.assertIsNone(sc.rsi([1.0] * 10, 14))


class TestVolumeRatio(unittest.TestCase):
    def test_ratio_excludes_today(self):
        volumes = [100.0] * 20 + [300.0]
        avg, ratio = sc.volume_ratio(volumes, period=20, exclude_today=True)
        self.assertAlmostEqual(avg, 100.0)
        self.assertAlmostEqual(ratio, 3.0)

    def test_ratio_includes_today(self):
        volumes = [100.0] * 19 + [300.0]
        avg, ratio = sc.volume_ratio(volumes, period=20, exclude_today=False)
        self.assertAlmostEqual(avg, 110.0)
        self.assertAlmostEqual(ratio, 300.0 / 110.0)

    def test_none_when_not_enough_data(self):
        self.assertEqual(sc.volume_ratio([100.0] * 5, period=20), (None, None))


class TestCrossState(unittest.TestCase):
    def test_golden_cross(self):
        # 下降トレンドの最終日だけ急騰させ、5日線が 25日線を上抜けた瞬間を作る
        closes = GOLDEN_CROSS_CLOSES
        self.assertEqual(sc.cross_state(closes, 5, 25), "golden")
        # 前日時点ではまだクロスしていない（＝「上抜けた当日」だけを拾う）
        self.assertIsNone(sc.cross_state(closes[:-1], 5, 25))

    def test_dead_cross(self):
        closes = DEAD_CROSS_CLOSES
        self.assertEqual(sc.cross_state(closes, 5, 25), "dead")
        self.assertIsNone(sc.cross_state(closes[:-1], 5, 25))

    def test_no_cross(self):
        closes = [100.0 + i * 0.5 for i in range(40)]
        self.assertIsNone(sc.cross_state(closes, 5, 25))

    def test_none_when_not_enough_data(self):
        self.assertIsNone(sc.cross_state([100.0] * 10, 5, 25))


def make_indicators(cross=None, rsi_value=45.0, ratio=2.0, close=1000.0):
    """判定ロジック検証用に指標を直接組み立てる。"""
    return sc.Indicators(
        date="2025-01-31", open=close, close=close, volume=3000.0,
        ma_short=1010.0, ma_mid=1000.0, ma_long=980.0,
        rsi=rsi_value, volume_avg=1500.0, volume_ratio=ratio, cross=cross,
    )


class TestJudge(unittest.TestCase):
    """買い/売り/様子見の分岐と、判定理由の中身を確認する。"""

    def test_buy_candidate(self):
        judgement, reason = sc.judge(make_indicators(cross="golden", rsi_value=45.0, ratio=1.8))
        self.assertEqual(judgement, sc.LABEL_BUY)
        self.assertIn("ゴールデンクロス", reason)
        self.assertIn("RSI 45.0", reason)
        self.assertIn("1.80倍", reason)

    def test_buy_rsi_boundaries_are_inclusive(self):
        for rsi_value in (sc.BUY_RSI_MIN, sc.BUY_RSI_MAX):
            with self.subTest(rsi=rsi_value):
                judgement, _ = sc.judge(make_indicators(cross="golden", rsi_value=rsi_value))
                self.assertEqual(judgement, sc.LABEL_BUY)

    def test_buy_volume_boundary_is_inclusive(self):
        judgement, _ = sc.judge(
            make_indicators(cross="golden", ratio=sc.BUY_VOLUME_RATIO_MIN))
        self.assertEqual(judgement, sc.LABEL_BUY)

    def test_hold_when_rsi_is_too_high(self):
        judgement, reason = sc.judge(make_indicators(cross="golden", rsi_value=65.0))
        self.assertEqual(judgement, sc.LABEL_HOLD)
        self.assertIn("範囲外", reason)

    def test_hold_when_volume_is_not_enough(self):
        judgement, reason = sc.judge(make_indicators(cross="golden", ratio=1.1))
        self.assertEqual(judgement, sc.LABEL_HOLD)
        self.assertIn("1.5倍未満", reason)

    def test_hold_when_no_cross(self):
        judgement, reason = sc.judge(make_indicators(cross=None))
        self.assertEqual(judgement, sc.LABEL_HOLD)
        self.assertIn("ゴールデンクロスなし", reason)
        self.assertIn("デッドクロスなし", reason)

    def test_sell_candidate(self):
        judgement, reason = sc.judge(make_indicators(cross="dead", rsi_value=75.0))
        self.assertEqual(judgement, sc.LABEL_SELL)
        self.assertIn("デッドクロス", reason)
        self.assertIn("RSI 75.0", reason)

    def test_sell_rsi_boundary_is_inclusive(self):
        judgement, _ = sc.judge(make_indicators(cross="dead", rsi_value=sc.SELL_RSI_MIN))
        self.assertEqual(judgement, sc.LABEL_SELL)

    def test_hold_when_dead_cross_but_rsi_is_low(self):
        judgement, reason = sc.judge(make_indicators(cross="dead", rsi_value=40.0))
        self.assertEqual(judgement, sc.LABEL_HOLD)
        self.assertIn("未満", reason)

    def test_no_data(self):
        judgement, reason = sc.judge(None)
        self.assertEqual(judgement, sc.LABEL_NO_DATA)
        self.assertIn("取得できません", reason)

    def test_insufficient_history(self):
        ind = sc.build_indicators(build_bars([100.0] * 10))
        judgement, reason = sc.judge(ind)
        self.assertEqual(judgement, sc.LABEL_NO_DATA)
        self.assertIn("データ不足", reason)


class TestBuildIndicators(unittest.TestCase):
    """日足リストから各指標が正しく組み上がるかを確認する。"""

    def test_indicators_from_bars(self):
        closes = GOLDEN_CROSS_CLOSES
        volumes = [1000.0] * (len(closes) - 1) + [3000.0]
        ind = sc.build_indicators(build_bars(closes, volumes))

        self.assertEqual(ind.date, f"2025-01-{len(closes):02d}")
        self.assertEqual(ind.close, closes[-1])
        self.assertAlmostEqual(ind.ma_short, sum(closes[-5:]) / 5)
        self.assertAlmostEqual(ind.ma_mid, sum(closes[-25:]) / 25)
        self.assertAlmostEqual(ind.ma_long, sum(closes[-75:]) / 75)
        self.assertAlmostEqual(ind.volume_avg, 1000.0)
        self.assertAlmostEqual(ind.volume_ratio, 3.0)
        self.assertEqual(ind.cross, "golden")
        self.assertIsNotNone(ind.rsi)

    def test_returns_none_for_empty_bars(self):
        self.assertIsNone(sc.build_indicators([]))


class TestBarsFromDataFrame(unittest.TestCase):
    """yfinance の DataFrame -> Bar 変換。"""

    @staticmethod
    def frame(rows):
        import pandas as pd
        index = pd.to_datetime([r[0] for r in rows])
        return pd.DataFrame(
            {"Open": [r[1] for r in rows],
             "Close": [r[2] for r in rows],
             "Volume": [r[3] for r in rows]},
            index=index,
        )

    def test_converts_rows(self):
        bars = sc.bars_from_dataframe(self.frame([
            ("2025-06-02", 100.0, 110.0, 5000.0),
            ("2025-06-03", 111.0, 115.0, 6000.0),
        ]))
        self.assertEqual([b.date for b in bars], ["2025-06-02", "2025-06-03"])
        self.assertEqual((bars[0].open, bars[0].close, bars[0].volume), (100.0, 110.0, 5000.0))

    def test_skips_rows_with_nan_close(self):
        bars = sc.bars_from_dataframe(self.frame([
            ("2025-06-02", 100.0, float("nan"), 5000.0),
            ("2025-06-03", 111.0, 115.0, 6000.0),
        ]))
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].date, "2025-06-03")

    def test_until_filters_future_rows(self):
        import datetime as dt
        bars = sc.bars_from_dataframe(self.frame([
            ("2025-06-02", 100.0, 110.0, 5000.0),
            ("2025-06-03", 111.0, 115.0, 6000.0),
            ("2025-06-04", 116.0, 120.0, 7000.0),
        ]), until=dt.date(2025, 6, 3))
        self.assertEqual([b.date for b in bars], ["2025-06-02", "2025-06-03"])

    def test_empty_frame(self):
        self.assertEqual(sc.bars_from_dataframe(None), [])
        self.assertEqual(sc.bars_from_dataframe(self.frame([])), [])


class TestTicker(unittest.TestCase):
    def test_to_ticker(self):
        self.assertEqual(sc.to_ticker("7203"), "7203.T")
        self.assertEqual(sc.to_ticker(" 6758 "), "6758.T")
        self.assertEqual(sc.to_ticker("7203.T"), "7203.T")
        self.assertEqual(sc.to_ticker("130a"), "130A.T")
        self.assertEqual(sc.to_ticker("72030"), "7203.T")  # J-Quants形式の5桁にも対応

    def test_display_code(self):
        self.assertEqual(sc.display_code("7203.T"), "7203")
        self.assertEqual(sc.display_code("7203"), "7203")
        self.assertEqual(sc.display_code("72030"), "7203")


class TestCsvOutput(unittest.TestCase):
    def test_writes_all_columns(self):
        ind = make_indicators(cross="golden", rsi_value=45.0, ratio=3.0)
        ind.per, ind.pbr = 15.0, 1.2
        judgement, reason = sc.judge(ind)
        result = sc.Result(code="7203", name="トヨタ自動車", indicators=ind,
                           judgement=judgement, reason=reason)

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sub", "out.csv")
            sc.write_csv([result], path)
            with open(path, encoding="utf-8-sig", newline="") as fp:
                rows = list(csv.DictReader(fp))

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(list(row.keys()), sc.CSV_COLUMNS)
        self.assertEqual(row["code"], "7203")
        self.assertEqual(row["name"], "トヨタ自動車")
        self.assertEqual(row["judgement"], sc.LABEL_BUY)
        self.assertEqual(row["per"], "15.00")
        self.assertEqual(row["volume_ratio"], "3.00")

    def test_missing_values_are_blank(self):
        result = sc.Result(code="9999", judgement=sc.LABEL_NO_DATA, reason="データなし")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.csv")
            sc.write_csv([result], path)
            with open(path, encoding="utf-8-sig", newline="") as fp:
                row = list(csv.DictReader(fp))[0]
        self.assertEqual(row["close"], "")
        self.assertEqual(row["rsi14"], "")
        self.assertEqual(row["judgement"], sc.LABEL_NO_DATA)


class TestWatchlist(unittest.TestCase):
    def test_reads_codes_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "w.json")
            with open(path, "w", encoding="utf-8") as fp:
                fp.write('{"codes": ["7203", "6758"]}')
            self.assertEqual(sc.load_watchlist(path), ["7203", "6758"])

    def test_reads_plain_array(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "w.json")
            with open(path, "w", encoding="utf-8") as fp:
                fp.write('["7203", 6758]')
            self.assertEqual(sc.load_watchlist(path), ["7203", "6758"])

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            sc.load_watchlist("/nonexistent/watchlist.json")


class FakeTicker:
    """yfinance.Ticker のダミー。指定した終値・出来高から日足フレームを作る。"""

    def __init__(self, closes, volumes=None, info=None, fail_times=0, empty=False):
        self.closes = closes
        self.volumes = volumes if volumes is not None else [1000.0] * len(closes)
        self._info = info if info is not None else {}
        self.fail_times = fail_times
        self.empty = empty
        self.history_calls = 0
        self.info_calls = 0

    def history(self, **kwargs):
        import pandas as pd
        self.history_calls += 1
        if self.history_calls <= self.fail_times:
            raise RuntimeError("429 Too Many Requests")
        if self.empty:
            return pd.DataFrame()
        import datetime as dt
        base = dt.date(2025, 1, 6)
        index = pd.to_datetime([base + dt.timedelta(days=i) for i in range(len(self.closes))])
        return pd.DataFrame(
            {"Open": [c - 1 for c in self.closes],
             "Close": list(self.closes),
             "Volume": list(self.volumes)},
            index=index,
        )

    @property
    def info(self):
        self.info_calls += 1
        return self._info


class TestYFinanceSource(unittest.TestCase):
    """取得層（リトライ・キャッシュ・PER/PBR 抽出）。"""

    def make_source(self, tickers):
        return sc.YFinanceSource(ticker_factory=lambda t: tickers[t],
                                 interval_sec=0, max_retries=3, backoff_sec=0)

    def test_fetch_bars(self):
        fake = FakeTicker([100.0, 101.0, 102.0])
        bars = self.make_source({"7203.T": fake}).fetch_bars("7203.T")
        self.assertEqual([b.close for b in bars], [100.0, 101.0, 102.0])

    def test_ticker_object_is_reused(self):
        fake = FakeTicker([100.0, 101.0])
        source = self.make_source({"7203.T": fake})
        source.fetch_bars("7203.T")
        source.fetch_profile("7203.T")
        self.assertEqual(len(source._cache), 1)

    def test_retries_then_succeeds(self):
        fake = FakeTicker([100.0, 101.0], fail_times=2)
        bars = self.make_source({"7203.T": fake}).fetch_bars("7203.T")
        self.assertEqual(fake.history_calls, 3)
        self.assertEqual(len(bars), 2)

    def test_empty_result_is_retried_then_reported(self):
        # yfinance はレート制限時に例外ではなく空フレームを返すことがある
        fake = FakeTicker([], empty=True)
        with self.assertRaises(sc.DataSourceError) as ctx:
            self.make_source({"7203.T": fake}).fetch_bars("7203.T")
        self.assertEqual(fake.history_calls, 3)
        self.assertIn("データが空でした", str(ctx.exception))

    def test_raises_after_max_retries(self):
        fake = FakeTicker([100.0], fail_times=99)
        with self.assertRaises(sc.DataSourceError):
            self.make_source({"7203.T": fake}).fetch_bars("7203.T")

    def test_profile_extracts_name_per_pbr(self):
        fake = FakeTicker([100.0], info={"shortName": "TOYOTA MOTOR",
                                         "trailingPE": 12.5, "priceToBook": 1.3})
        profile = self.make_source({"7203.T": fake}).fetch_profile("7203.T")
        self.assertEqual(profile.name, "TOYOTA MOTOR")
        self.assertAlmostEqual(profile.per, 12.5)
        self.assertAlmostEqual(profile.pbr, 1.3)

    def test_profile_falls_back_to_forward_pe(self):
        fake = FakeTicker([100.0], info={"longName": "Sony Group", "forwardPE": 20.0})
        profile = self.make_source({"7203.T": fake}).fetch_profile("7203.T")
        self.assertEqual(profile.name, "Sony Group")
        self.assertAlmostEqual(profile.per, 20.0)
        self.assertIsNone(profile.pbr)

    def test_profile_drops_non_positive_values(self):
        # 赤字銘柄の PER などは空欄にする
        fake = FakeTicker([100.0], info={"shortName": "X", "trailingPE": -5.0, "priceToBook": 0})
        profile = self.make_source({"7203.T": fake}).fetch_profile("7203.T")
        self.assertIsNone(profile.per)
        self.assertIsNone(profile.pbr)

    def test_profile_is_empty_when_fetch_fails(self):
        class Broken:
            @property
            def info(self):
                raise RuntimeError("no info")
        source = sc.YFinanceSource(ticker_factory=lambda t: Broken(),
                                   interval_sec=0, max_retries=1, backoff_sec=0)
        profile = source.fetch_profile("7203.T")
        self.assertEqual(profile, sc.Profile())


class TestScreenCode(unittest.TestCase):
    def test_returns_judgement_and_name(self):
        fake = FakeTicker(BUY_SIGNAL_CLOSES,
                          volumes=[1000.0] * (len(BUY_SIGNAL_CLOSES) - 1) + [3000.0],
                          info={"shortName": "テスト商事", "trailingPE": 10.0, "priceToBook": 1.1})
        source = sc.YFinanceSource(ticker_factory=lambda t: fake, interval_sec=0)
        result = sc.screen_code(source, "7203")
        self.assertEqual(result.code, "7203")
        self.assertEqual(result.name, "テスト商事")
        self.assertEqual(result.judgement, sc.LABEL_BUY, result.reason)
        self.assertEqual(result.indicators.cross, "golden")
        self.assertAlmostEqual(result.indicators.per, 10.0)
        self.assertEqual(result.error, "")

    def test_no_fundamentals_skips_info_call(self):
        fake = FakeTicker(GOLDEN_CROSS_CLOSES)
        source = sc.YFinanceSource(ticker_factory=lambda t: fake, interval_sec=0)
        result = sc.screen_code(source, "7203", with_fundamentals=False)
        self.assertEqual(fake.info_calls, 0)
        self.assertEqual(result.name, "")

    def test_records_error_when_source_fails(self):
        fake = FakeTicker([100.0], fail_times=99)
        source = sc.YFinanceSource(ticker_factory=lambda t: fake,
                                   interval_sec=0, max_retries=2, backoff_sec=0)
        result = sc.screen_code(source, "7203")
        self.assertEqual(result.judgement, sc.LABEL_NO_DATA)
        self.assertIn("取得エラー", result.reason)
        self.assertTrue(result.error)

    def test_empty_history_is_not_fatal(self):
        fake = FakeTicker([], empty=True)
        source = sc.YFinanceSource(ticker_factory=lambda t: fake, interval_sec=0,
                                   max_retries=1, backoff_sec=0)
        result = sc.screen_code(source, "9999", with_fundamentals=False)
        self.assertEqual(result.judgement, sc.LABEL_NO_DATA)
        self.assertIn("取得エラー", result.reason)
        self.assertTrue(result.error)


class TestMainEndToEnd(unittest.TestCase):
    """CLI から CSV 出力まで、ダミーのデータソースで通しで動かす。"""

    def test_main_writes_csv(self):
        buy_closes = BUY_SIGNAL_CLOSES
        volumes = [1000.0] * (len(buy_closes) - 1) + [3000.0]
        tickers = {
            "7203.T": FakeTicker(buy_closes, volumes,
                                 info={"shortName": "買い候補商事", "trailingPE": 10.0,
                                       "priceToBook": 1.1}),
            "9999.T": FakeTicker([], empty=True, info={"shortName": "データなし物産"}),
        }
        source = sc.YFinanceSource(ticker_factory=lambda t: tickers[t], interval_sec=0,
                                   max_retries=1, backoff_sec=0)

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.csv")
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                code = sc.main(["--codes", "7203", "9999", "--output", path], source=source)
            self.assertEqual(code, 0)
            with open(path, encoding="utf-8-sig", newline="") as fp:
                rows = {r["code"]: r for r in csv.DictReader(fp)}

        self.assertEqual(rows["7203"]["judgement"], sc.LABEL_BUY)
        self.assertEqual(rows["7203"]["name"], "買い候補商事")
        self.assertEqual(rows["7203"]["per"], "10.00")
        self.assertEqual(rows["7203"]["volume_ratio"], "3.00")
        self.assertIn("ゴールデンクロス", rows["7203"]["reason"])
        self.assertEqual(rows["9999"]["judgement"], sc.LABEL_NO_DATA)
        self.assertEqual(rows["9999"]["close"], "")

    def test_main_rejects_bad_date(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(sc.main(["--codes", "7203", "--date", "2025/06/30"]), 2)


if __name__ == "__main__":
    unittest.main()
