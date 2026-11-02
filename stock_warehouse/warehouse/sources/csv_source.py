"""CSVの取り口。ボスが本物のデータを手で入れて試すための入口。

フォルダの形：
    <フォルダ>/
        instruments.csv   … 任意。列: code,name,kind（kind は stock か index）
        7203.csv          … 1銘柄1ファイル。ファイル名（拡張子を除く）が銘柄コード
        ...

各銘柄CSVの列（1段目と同じ。順番は自由、余分な列は無視）：
    date,open,high,low,close,volume[,split_ratio]
- date は YYYY-MM-DD（YYYY/MM/DD も可）
- 価格は分割調整していない「その日の実際の値段」
- 取引不成立の日は open/high/low/close を空欄、volume を 0
- split_ratio は省略可（省略すると全日 1.0 = 分割なし）
- 文字コードは UTF-8（BOM 付きでも可）
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Optional

import pandas as pd

from .base import KIND_INDEX, KIND_STOCK, RAW_COLUMNS, DataSource, Instrument, empty_raw_frame

REQUIRED_COLUMNS = ["date", "open", "high", "low", "close", "volume"]


class CsvSource(DataSource):
    name = "csv"

    def __init__(self, folder: str | Path):
        self.folder = Path(folder)
        if not self.folder.is_dir():
            raise FileNotFoundError(f"CSVのフォルダが見つかりません: {self.folder}")

    def list_instruments(self) -> list[Instrument]:
        meta: dict[str, Instrument] = {}
        meta_path = self.folder / "instruments.csv"
        if meta_path.exists():
            table = pd.read_csv(meta_path, dtype=str, encoding="utf-8-sig").fillna("")
            for _, row in table.iterrows():
                code = row["code"].strip()
                kind = (row.get("kind", "") or KIND_STOCK).strip() or KIND_STOCK
                if kind not in (KIND_STOCK, KIND_INDEX):
                    raise ValueError(f"instruments.csv の kind が不正です: {code} → {kind}")
                meta[code] = Instrument(code=code, name=(row.get("name", "") or code).strip(), kind=kind)
        codes = sorted(p.stem for p in self.folder.glob("*.csv") if p.name != "instruments.csv")
        return [meta.get(code, Instrument(code=code, name=code, kind=KIND_STOCK)) for code in codes]

    def fetch_daily(self, code: str, start: Optional[dt.date] = None,
                    end: Optional[dt.date] = None) -> pd.DataFrame:
        path = self.folder / f"{code}.csv"
        if not path.exists():
            raise FileNotFoundError(f"銘柄のCSVがありません: {path}")
        table = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
        table.columns = [c.strip().lower() for c in table.columns]
        missing = [c for c in REQUIRED_COLUMNS if c not in table.columns]
        if missing:
            raise ValueError(f"{path.name}: 必要な列がありません: {missing}")
        if "split_ratio" not in table.columns:
            table["split_ratio"] = "1"
        table = table[RAW_COLUMNS].copy()
        table["date"] = pd.to_datetime(table["date"].str.strip(), format="mixed").dt.strftime("%Y-%m-%d")
        for col in RAW_COLUMNS[1:]:
            table[col] = pd.to_numeric(table[col].str.strip().replace("", None), errors="raise")
        table["split_ratio"] = table["split_ratio"].fillna(1.0)
        table = table.astype({c: "float64" for c in RAW_COLUMNS[1:]})
        table = table.sort_values("date").reset_index(drop=True)
        if start is not None:
            table = table[table["date"] >= start.isoformat()]
        if end is not None:
            table = table[table["date"] <= end.isoformat()]
        table = table.reset_index(drop=True)
        return table if len(table) else empty_raw_frame()
