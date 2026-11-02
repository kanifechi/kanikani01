"""データの取り口の一覧。新しい取り口を足すときは make_source に1行足す。"""

from __future__ import annotations

from .base import KIND_INDEX, KIND_STOCK, RAW_COLUMNS, DataSource, Instrument, read_secret
from .csv_source import CsvSource
from .virtual import VirtualSource

SOURCE_NAMES = ["virtual", "csv"]


def make_source(name: str, *, seed: int | None = None, csv_dir: str | None = None) -> DataSource:
    if name == "virtual":
        return VirtualSource() if seed is None else VirtualSource(seed=seed)
    if name == "csv":
        if not csv_dir:
            raise ValueError("CSVの取り口には --csv-dir（CSVを置いたフォルダ）が必要です")
        return CsvSource(csv_dir)
    # 本物のAPIの取り口を作ったら、ここに足す（README「本物のAPIに差し替える」参照）
    raise ValueError(f"知らない取り口です: {name}（使えるもの: {', '.join(SOURCE_NAMES)}）")


__all__ = [
    "DataSource", "Instrument", "RAW_COLUMNS", "KIND_STOCK", "KIND_INDEX", "read_secret",
    "CsvSource", "VirtualSource", "make_source", "SOURCE_NAMES",
]
