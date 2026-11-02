"""コマンド1つで「取り口から読む→2段目を計算→3段目を作る」。

    python -m warehouse                          # 仮想データで全期間を作る
    python -m warehouse --date 2026-10-30        # その日の分だけ更新
    python -m warehouse --source csv --csv-dir my_csv   # CSVの取り口で作る
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path
from typing import Optional

from .pipeline import run
from .sources import SOURCE_NAMES, make_source

PROJECT_DIR = Path(__file__).resolve().parent.parent


def _date(text: str) -> dt.date:
    try:
        return dt.date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"日付は YYYY-MM-DD で指定してください: {text}") from exc


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m warehouse", description="日本株データの倉庫（1段目→2段目→3段目）")
    p.add_argument("--source", choices=SOURCE_NAMES, default="virtual", help="データの取り口（既定: virtual）")
    p.add_argument("--csv-dir", help="--source csv のとき、CSVを置いたフォルダ")
    p.add_argument("--seed", type=int, help="仮想データの乱数の種（既定: 20261102）")
    p.add_argument("--data-dir", help="出力先（既定: stock_warehouse/data/<取り口の名前>）")
    p.add_argument("--date", type=_date, help="この日の分だけ更新する（YYYY-MM-DD）")
    p.add_argument("--start", type=_date, help="全期間作り直しのとき、この日から")
    p.add_argument("--end", type=_date, help="全期間作り直しのとき、この日まで")
    p.add_argument("--quiet", action="store_true", help="進み具合を表示しない")
    args = p.parse_args(argv)
    if args.date and (args.start or args.end):
        p.error("--date と --start/--end は一緒に使えません")
    return args


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    source = make_source(args.source, seed=args.seed, csv_dir=args.csv_dir)
    data_dir = Path(args.data_dir) if args.data_dir else PROJECT_DIR / "data" / source.name
    result = run(source, data_dir, date=args.date, start=args.start, end=args.end, quiet=args.quiet)
    return 0 if result.cards else 1


if __name__ == "__main__":
    sys.exit(main())
