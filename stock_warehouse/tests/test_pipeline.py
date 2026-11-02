"""通しのテスト：再現性・意地悪なデータ・カードのスキーマ・取り口の差し替え・日付指定の更新。"""

import contextlib
import copy
import datetime as dt
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from warehouse import calendar_jp  # noqa: E402
from warehouse.__main__ import main  # noqa: E402
from warehouse.cards import CARD_MAX_BYTES, CardError, card_to_text, validate_card  # noqa: E402
from warehouse.features import read_features_csv  # noqa: E402
from warehouse.pipeline import run  # noqa: E402
from warehouse.sources import CsvSource, DataSource, VirtualSource  # noqa: E402
from warehouse.sources.virtual import INDEX_CODE, SCENARIO, STOCK_CODES  # noqa: E402

LAST_DAY = "2026-10-30"


def tree_digest(folder: Path, sub: str = "") -> dict[str, str]:
    base = folder / sub if sub else folder
    return {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(base.rglob("*")) if p.is_file()}


def quiet_run(*args, **kwargs):
    kwargs.setdefault("quiet", True)
    return run(*args, **kwargs)


class FullRunTest(unittest.TestCase):
    """仮想データで全期間を1回作り、その結果をいろいろ確かめる。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name) / "a"
        cls.result = quiet_run(VirtualSource(), cls.dir)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def feat(self, code):
        return read_features_csv(self.dir / "features" / f"{code}.csv")

    def raw(self, code):
        return pd.read_csv(self.dir / "raw" / f"{code}.csv", dtype={"date": str})

    def card(self, code, day=LAST_DAY):
        return json.loads((self.dir / "cards" / day / f"{code}.json").read_text(encoding="utf-8"))

    # ---- 合格条件1：同じ種なら毎回まったく同じ
    def test_same_seed_same_bytes(self):
        with tempfile.TemporaryDirectory() as other:
            quiet_run(VirtualSource(), other)
            self.assertEqual(tree_digest(self.dir), tree_digest(Path(other)))

    def test_different_seed_differs(self):
        with tempfile.TemporaryDirectory() as other:
            quiet_run(VirtualSource(seed=1), other)
            self.assertNotEqual(tree_digest(self.dir, "raw"), tree_digest(Path(other), "raw"))

    def test_shape_of_output(self):
        self.assertEqual(self.result.as_of, LAST_DAY)
        self.assertEqual(len(list((self.dir / "raw").glob("99*.csv"))), 21)
        self.assertEqual(len(list((self.dir / "features").glob("*.csv"))), 21)
        # 9920 は 2025-12-26 で上場廃止 → 最終日のカードは無い
        self.assertEqual(sorted(self.result.cards), sorted([c for c in STOCK_CODES if c != "9920"] + [INDEX_CODE]))
        self.assertEqual(self.result.skipped, ["9920"])
        self.assertEqual(self.result.warnings, [])

    # ---- 合格条件2：意地悪なデータで落ちない・正しく扱う
    def test_no_rows_on_weekends_or_holidays(self):
        for code in STOCK_CODES + [INDEX_CODE]:
            days = [dt.date.fromisoformat(d) for d in self.raw(code)["date"]]
            self.assertTrue(all(calendar_jp.is_trading_day(d) for d in days), code)
        idx_days = set(self.raw(INDEX_CODE)["date"])
        for holiday in ["2025-01-13", "2025-11-24", "2026-09-22", "2025-12-31", "2026-01-02"]:
            self.assertNotIn(holiday, idx_days)
        self.assertGreater(len(idx_days), 470)  # 約2年分

    def test_splits_are_adjusted(self):
        for code, (day, ratio) in SCENARIO["splits"].items():
            raw, f = self.raw(code), self.feat(code)
            i = raw.index[raw["date"] == day][0]
            self.assertEqual(raw["split_ratio"].iloc[i], ratio)
            jump = raw["close"].iloc[i] / raw["close"].iloc[i - 1]
            self.assertLess(jump, 0.75, "生データでは分割で価格が飛んでいるはず")
            self.assertLess(abs(f["change_pct"].iloc[i]), 30.0, "調整後は飛ばない")
            self.assertAlmostEqual(f["close_adj"].iloc[i - 1], round(raw["close"].iloc[i - 1] / ratio, 4))
            card = self.card(code)
            self.assertEqual(card["data"]["last_split_date"], day)

    def test_limit_events_detected(self):
        for code, events in SCENARIO["limit_events"].items():
            f = self.feat(code).set_index("date")
            for day, kind in events:
                col = "limit_up" if kind == "up" else "limit_down"
                self.assertTrue(bool(f.loc[day, col]), f"{code} {day} {col}")
        self.assertTrue(self.card("9914")["recent"][-3]["limit_up"])  # 2026-10-28

    def test_no_trade_days(self):
        f = self.feat("9917").set_index("date")
        for day in SCENARIO["no_trade_days"]["9917"]:
            self.assertTrue(bool(f.loc[day, "no_trade"]))
            self.assertEqual(f.loc[day, "volume_adj"], 0.0)
            self.assertTrue(pd.isna(f.loc[day, "close_adj"]))
            self.assertTrue(pd.isna(f.loc[day, "ma5"]))
        recent = {r["date"]: r for r in self.card("9917")["recent"]}
        self.assertTrue(recent["2026-10-29"]["no_trade"])
        self.assertIsNone(recent["2026-10-29"]["close"])

    def test_no_trade_card_on_that_day(self):
        with tempfile.TemporaryDirectory() as other:
            quiet_run(VirtualSource(), other, end=dt.date(2026, 10, 29))
            card = json.loads((Path(other) / "cards" / "2026-10-29" / "9917.json").read_text(encoding="utf-8"))
            self.assertTrue(card["data"]["no_trade_today"])
            self.assertEqual(card["latest"]["date"], "2026-10-28")
            self.assertIsNotNone(card["latest"]["close"])
            validate_card(card)

    def test_listing_and_delisting(self):
        f19 = self.feat("9919")
        self.assertEqual(f19["date"].iloc[0], SCENARIO["listing"]["9919"])
        card = self.card("9919")
        self.assertIsNone(card["latest"]["ma200"]["value"])
        self.assertIsNone(card["latest"]["ma200"]["above"])
        self.assertIsNotNone(card["latest"]["ma75"]["value"])
        self.assertTrue(any("ma200" in n for n in card["data"]["notes"]))
        self.assertEqual(self.feat("9920")["date"].iloc[-1], SCENARIO["delisting"]["9920"])

    def test_null_until_enough_history(self):
        f = self.feat(INDEX_CODE)
        self.assertTrue(f["ma200"].iloc[:199].isna().all())
        self.assertTrue(f["ma200"].iloc[199:].notna().all())

    # ---- 合格条件2：カードがスキーマに合い、8KB以下
    def test_all_cards_match_schema_and_size(self):
        files = list((self.dir / "cards" / LAST_DAY).glob("*.json"))
        self.assertEqual(len(files), 20)
        for path in files:
            card = json.loads(path.read_text(encoding="utf-8"))
            size = validate_card(card)
            self.assertLessEqual(len(path.read_bytes()), CARD_MAX_BYTES, path.name)
            self.assertEqual(size, len(path.read_bytes()))

    def test_schema_rejects_bad_cards(self):
        card = self.card("9901")
        bad = copy.deepcopy(card)
        bad["latest"]["close"] = "高い"
        with self.assertRaises(CardError):
            validate_card(bad)
        bad = copy.deepcopy(card)
        bad["extra"] = 1
        with self.assertRaises(CardError):
            validate_card(bad)
        # スキーマの文字数上限ぎりぎりの notes を詰めて、8KB 超えを作る（形は正しいが大きすぎる）
        bad = copy.deepcopy(self.card("9905"))
        bad["data"]["notes"] = ["あ" * 120] * 12
        bad["name"] = "い" * 64
        self.assertGreater(len(card_to_text(bad).encode("utf-8")), CARD_MAX_BYTES)
        with self.assertRaises(CardError):
            validate_card(bad)

    def test_market_block_matches_index(self):
        card = self.card("9901")
        index_card = self.card(INDEX_CODE)
        self.assertEqual(card["market"]["code"], INDEX_CODE)
        self.assertEqual(card["market"]["change_pct"], index_card["latest"]["change_pct"])
        self.assertIsNone(index_card["market"])

    # ---- 合格条件3：取り口を CSV に替えても、ほかのコードはそのまま
    def test_csv_source_gives_same_result(self):
        with tempfile.TemporaryDirectory() as other:
            other = Path(other)
            quiet_run(CsvSource(self.dir / "raw"), other)
            self.assertEqual(tree_digest(self.dir, "raw"), tree_digest(other, "raw"))
            self.assertEqual(tree_digest(self.dir, "features"), tree_digest(other, "features"))
            for path in (self.dir / "cards" / LAST_DAY).glob("*.json"):
                a = json.loads(path.read_text(encoding="utf-8"))
                b = json.loads((other / "cards" / LAST_DAY / path.name).read_text(encoding="utf-8"))
                self.assertEqual(b["source"], "csv")
                a.pop("source"), b.pop("source")
                self.assertEqual(a, b, path.name)

    # ---- 日付指定の更新 = 全期間作り直しと同じ結果
    def test_update_one_day_equals_full_rebuild(self):
        with tempfile.TemporaryDirectory() as other:
            other = Path(other)
            quiet_run(VirtualSource(), other, end=dt.date(2026, 10, 29))
            res = quiet_run(VirtualSource(), other, date=dt.date(2026, 10, 30))
            self.assertEqual(res.as_of, LAST_DAY)
            self.assertEqual(tree_digest(self.dir, "raw"), tree_digest(other, "raw"))
            self.assertEqual(tree_digest(self.dir, "features"), tree_digest(other, "features"))
            self.assertEqual(tree_digest(self.dir, f"cards/{LAST_DAY}"), tree_digest(other, f"cards/{LAST_DAY}"))

    def test_update_across_split_day(self):
        # 分割の日を「その日だけ更新」で取り込んでも、過去の調整後価格が全期間作り直しと一致する
        with tempfile.TemporaryDirectory() as other:
            other = Path(other)
            quiet_run(VirtualSource(), other, end=dt.date(2026, 3, 31))
            quiet_run(VirtualSource(), other, date=dt.date(2026, 4, 1))
            with tempfile.TemporaryDirectory() as ref:
                quiet_run(VirtualSource(), ref, end=dt.date(2026, 4, 1))
                self.assertEqual(tree_digest(Path(ref), "features"), tree_digest(other, "features"))

    def test_update_on_holiday_makes_no_cards(self):
        with tempfile.TemporaryDirectory() as other:
            quiet_run(VirtualSource(), other, end=dt.date(2026, 9, 18))
            res = quiet_run(VirtualSource(), other, date=dt.date(2026, 9, 22))  # 国民の休日
            self.assertEqual(res.cards, [])


class HandMadeCsvTest(unittest.TestCase):
    """ボスが手で作りそうな CSV（BOM付き・スラッシュ日付・split_ratio なし・不成立日あり）。"""

    def test_hand_made_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in"
            src.mkdir()
            days = calendar_jp.trading_days(dt.date(2025, 1, 6), dt.date(2025, 3, 31))
            lines = ["Date,Open,High,Low,Close,Volume"]
            for i, d in enumerate(days):
                if i == 10:
                    lines.append(f"{d:%Y/%m/%d},,,,,0")
                else:
                    c = 1000 + i
                    lines.append(f"{d:%Y/%m/%d},{c},{c + 5},{c - 5},{c},{12000 + i}")
            (src / "130A.csv").write_text("﻿" + "\n".join(lines) + "\n", encoding="utf-8")
            (src / "instruments.csv").write_text("code,name,kind\n130A,手入力テスト,stock\n", encoding="utf-8")
            out = Path(tmp) / "out"
            res = quiet_run(CsvSource(src), out)
            self.assertEqual(res.cards, ["130A"])
            card = json.loads((out / "cards" / res.as_of / "130A.json").read_text(encoding="utf-8"))
            validate_card(card)
            self.assertEqual(card["name"], "手入力テスト")
            self.assertEqual(card["data"]["sessions"], len(days) - 1)
            self.assertIsNone(card["latest"]["ma75"]["value"])
            self.assertIsNotNone(card["latest"]["ma25"]["value"])
            self.assertIsNone(card["market"])

    def test_broken_csv_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)
            (src / "1111.csv").write_text("date,open,high,low,close\n2025-01-06,1,1,1,1\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                quiet_run(CsvSource(src), src / "out")


class SwapSourceTest(unittest.TestCase):
    """どんな DataSource でも pipeline は中身を知らずに動く（本物のAPIの代役）。"""

    def test_custom_source(self):
        class TinySource(DataSource):
            name = "tiny"

            def list_instruments(self):
                from warehouse.sources import Instrument
                return [Instrument("0001", "小さな銘柄")]

            def fetch_daily(self, code, start=None, end=None):
                frame = VirtualSource().fetch_daily("9901", start, end)
                return frame.tail(30).reset_index(drop=True)

        with tempfile.TemporaryDirectory() as tmp:
            res = quiet_run(TinySource(), tmp)
            self.assertEqual(res.cards, ["0001"])


class CliTest(unittest.TestCase):
    def test_cli_full_and_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(main(["--data-dir", tmp, "--end", "2026-10-29"]), 0)
                self.assertEqual(main(["--data-dir", tmp, "--date", "2026-10-30"]), 0)
            self.assertIn("2026-10-30", err.getvalue())
            self.assertTrue((Path(tmp) / "cards" / "2026-10-30" / "9901.json").exists())

    def test_cli_csv_needs_dir(self):
        with self.assertRaises(ValueError):
            main(["--source", "csv", "--quiet"])


if __name__ == "__main__":
    unittest.main()
