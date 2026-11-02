"""データの取り口（差し替えポイント）の共通インターフェース。

ほかの部品（2段目の計算・3段目のカード作り）は、ここに書いた約束だけを使う。
取り口の中身（仮想データ・CSV・本物のAPI）は知らなくてよい。

約束：
- list_instruments() は銘柄の一覧（Instrument のリスト）を返す
- fetch_daily(code, start, end) は 1段目の生データを pandas.DataFrame で返す
    - 列は RAW_COLUMNS とまったく同じ順番
    - date は "YYYY-MM-DD" の文字列。昇順・重複なし
    - 価格は「その日に実際についた値段」（分割調整していない値）
    - 取引が成立しなかった日は open/high/low/close を空（NaN）、volume を 0 にする
    - split_ratio は「その日から株数が何倍になったか」（1→2 の分割なら 2.0）。普段は 1.0
    - 指定期間にデータが無ければ、列だけそろった空の DataFrame を返す
"""

from __future__ import annotations

import abc
import datetime as dt
import os
from dataclasses import dataclass
from typing import Optional

import pandas as pd

RAW_COLUMNS = ["date", "open", "high", "low", "close", "volume", "split_ratio"]

KIND_STOCK = "stock"
KIND_INDEX = "index"


@dataclass(frozen=True)
class Instrument:
    code: str            # 銘柄コード（文字列。"130A" のような英字入りも想定）
    name: str
    kind: str = KIND_STOCK  # "stock" または "index"（市場全体の指標）


def empty_raw_frame() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="object" if c == "date" else "float64")
                         for c in RAW_COLUMNS})


def read_secret(name: str) -> str:
    """APIキーなどの秘密を環境変数から読む（GitHub Actions ではシークレットを env に渡す）。

    コードやファイルに秘密を書かないための入口。無ければ分かりやすく止める。
    """
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"環境変数 {name} が設定されていません。"
            f"ローカルでは `export {name}=...`、GitHub Actions ではリポジトリのシークレットに登録して "
            f"`env: {name}: ${{{{ secrets.{name} }}}}` で渡してください。"
        )
    return value


class DataSource(abc.ABC):
    """データの取り口。本物のAPIに差し替えるときは、これを継承したクラスを1つ作る。"""

    #: カードの source 欄に入る短い名前（例: "virtual", "csv", "jquants"）
    name: str = "base"

    @abc.abstractmethod
    def list_instruments(self) -> list[Instrument]:
        """扱う銘柄の一覧。市場全体の指標を含めるなら kind="index" にする。"""

    @abc.abstractmethod
    def fetch_daily(self, code: str, start: Optional[dt.date] = None,
                    end: Optional[dt.date] = None) -> pd.DataFrame:
        """code の日足を start〜end（両端含む、None は制限なし）で返す。"""
