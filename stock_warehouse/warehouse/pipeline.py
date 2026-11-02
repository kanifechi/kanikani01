"""取り口から読む → 2段目を計算 → 3段目を作る、の通し。

この部品は DataSource の約束（sources/base.py）しか使わない。取り口の中身は知らない。

置き場所（data_dir の下）：
    raw/instruments.csv      銘柄一覧（code,name,kind）
    raw/<code>.csv           1段目（このフォルダはそのまま CSV の取り口に読ませられる）
    features/<code>.csv      2段目
    cards/<YYYY-MM-DD>/<code>.json  3段目
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pandas as pd

from .cards import build_card, card_to_text, market_summary
from .features import compute_features, read_features_csv
from .sources.base import KIND_INDEX, RAW_COLUMNS, DataSource, Instrument
from .validate import validate_raw


@dataclass
class RunResult:
    as_of: Optional[str]
    raw_rows: int = 0
    codes_with_data: list[str] = field(default_factory=list)
    cards: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _paths(data_dir: Path) -> tuple[Path, Path, Path]:
    return data_dir / "raw", data_dir / "features", data_dir / "cards"


def _write_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, encoding="utf-8", lineterminator="\n")


def _read_raw(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, dtype={"date": str}, encoding="utf-8")
    return frame[RAW_COLUMNS].astype({c: "float64" for c in RAW_COLUMNS[1:]})


def _write_instruments(instruments: list[Instrument], raw_dir: Path) -> None:
    table = pd.DataFrame([{"code": i.code, "name": i.name, "kind": i.kind} for i in instruments])
    _write_csv(table, raw_dir / "instruments.csv")


def _log(message: str, quiet: bool) -> None:
    if not quiet:
        print(message, file=sys.stderr)


def run(source: DataSource, data_dir: str | Path, *, date: Optional[dt.date] = None,
        start: Optional[dt.date] = None, end: Optional[dt.date] = None,
        quiet: bool = False) -> RunResult:
    """date を渡すと「その日の分だけ更新」、渡さないと全期間を作り直す。"""
    data_dir = Path(data_dir)
    raw_dir, feat_dir, card_dir = _paths(data_dir)
    instruments = source.list_instruments()
    if not instruments:
        raise ValueError("取り口が銘柄を1つも返しませんでした")
    codes = [i.code for i in instruments]
    if len(set(codes)) != len(codes):
        raise ValueError("銘柄コードが重複しています")

    result = RunResult(as_of=None)
    if date is None:
        # 全期間の作り直し：前の結果が混ざらないよう、1・2段目の古いファイルを消す
        for folder in (raw_dir, feat_dir):
            if folder.exists():
                for old in folder.glob("*.csv"):
                    old.unlink()
    raw_dir.mkdir(parents=True, exist_ok=True)
    _write_instruments(instruments, raw_dir)

    # ---- 1段目
    raws: dict[str, pd.DataFrame] = {}
    for inst in instruments:
        if date is None:
            frame = source.fetch_daily(inst.code, start, end)
        else:
            new = source.fetch_daily(inst.code, date, date)
            path = raw_dir / f"{inst.code}.csv"
            old = _read_raw(path) if path.exists() else pd.DataFrame(columns=RAW_COLUMNS)
            old = old[old["date"] != date.isoformat()]
            if new.empty and old.empty:
                continue
            frame = pd.concat([df for df in (old, new) if len(df)], ignore_index=True)
            frame = frame.sort_values("date").reset_index(drop=True)
            if new.empty:
                raws[inst.code] = frame  # 当日分なし（上場前・廃止後など）。既存の分だけ持つ
                continue
        frame = frame[RAW_COLUMNS].reset_index(drop=True)
        if frame.empty:
            continue
        result.warnings += validate_raw(frame, inst.code)
        _write_csv(frame, raw_dir / f"{inst.code}.csv")
        raws[inst.code] = frame
        result.raw_rows += len(frame)

    if not raws:
        result.warnings.append("どの銘柄にもデータがありませんでした")
        for w in result.warnings:
            _log(f"注意: {w}", quiet)
        return result

    # ---- 2段目（分割の後ろ調整があるので、毎回その銘柄の全期間を計算し直す）
    feats: dict[str, pd.DataFrame] = {}
    for inst in instruments:
        if inst.code not in raws:
            continue
        frame = compute_features(raws[inst.code], inst.code, inst.kind)
        _write_csv(frame, feat_dir / f"{inst.code}.csv")
        feats[inst.code] = read_features_csv(feat_dir / f"{inst.code}.csv")  # 保存した物と同じ値でカードを作る

    # ---- 3段目
    if date is not None:
        as_of = date.isoformat()
    else:
        as_of = max(f["date"].max() for f in feats.values())
    result.as_of = as_of
    result.codes_with_data = sorted(c for c, f in feats.items() if (f["date"] == as_of).any())
    if not result.codes_with_data:
        result.warnings.append(f"{as_of} のデータがある銘柄がありません（休場日の可能性）")

    index_inst = next((i for i in instruments if i.kind == KIND_INDEX), None)
    index_feat = feats.get(index_inst.code) if index_inst else None
    market = market_summary(index_feat, index_inst.code, as_of) if index_inst else None

    out_dir = card_dir / as_of
    if out_dir.exists():
        for old in out_dir.glob("*.json"):
            old.unlink()
    for inst in instruments:
        if inst.code not in feats:
            result.skipped.append(inst.code)
            continue
        card = build_card(feats[inst.code], inst, as_of, source.name,
                          None if inst.kind == KIND_INDEX else market)
        if card is None:
            result.skipped.append(inst.code)
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{inst.code}.json").write_text(card_to_text(card), encoding="utf-8")
        result.cards.append(inst.code)

    for w in result.warnings:
        _log(f"注意: {w}", quiet)
    _log(f"基準日 {as_of}: 1段目 {len(raws)}銘柄 / 2段目 {len(feats)}銘柄 / "
         f"カード {len(result.cards)}枚 → {out_dir}"
         + (f"（カードなし: {', '.join(result.skipped)}）" if result.skipped else ""), quiet)
    return result


def summary_json(result: RunResult) -> str:
    return json.dumps({"as_of": result.as_of, "cards": len(result.cards), "skipped": result.skipped,
                       "warnings": result.warnings}, ensure_ascii=False)
