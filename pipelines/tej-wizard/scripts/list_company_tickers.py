"""List every company in company-info.xlsx that a price workbook can be built for.

``raw/company-info/company-info.xlsx`` is TEJ's company master: 3,555 companies
on 2026-09-28, far wider than the price universe. Most of them have no
``waprcd1`` history this pipeline can fetch, so the list is filtered:

* the id is the part of ``公司簡稱`` before the first space. ``證期會代碼`` is not
  the same thing -- 2854 寶來證 carries ``000970`` there;
* the company must have listed on TSE, OTC or 創新板 at some point. ``上市別``
  is *today's* status and cannot be used: 2311 日月光 reads ``UNPUB`` but has a
  full price history. Companies that only ever traded on 興櫃 are excluded by
  choice -- negotiated prices, not an exchange auction;
* a company delisted before 2000-09-04 is excluded, because the template's date
  skeleton starts there and TEJ trims dates but cannot invent them.

Writes one id per line to ``state/company_universe_tickers.txt``, the input to
``create_workbooks.py --tickers-file``.

Usage::

    python scripts/list_company_tickers.py
"""

from __future__ import annotations

import datetime as dt
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import openpyxl  # noqa: E402

from tej_pipeline.processing.prices import raw_workbooks  # noqa: E402
from tej_pipeline.settings import get_settings  # noqa: E402

SKELETON_START = dt.datetime(2000, 9, 4)
LISTING_COLUMNS = ("TSE上市日", "OTC上市日", "創新版上市日")


def main() -> int:
    settings = get_settings()
    source = settings.raw_dir / "company-info" / "company-info.xlsx"
    out = settings.state_dir / "company_universe_tickers.txt"

    wb = openpyxl.load_workbook(source, read_only=True, data_only=True)
    rows = wb.worksheets[0].iter_rows(values_only=True)
    col = {name: i for i, name in enumerate(next(rows))}

    total, kept = Counter(), Counter()
    tickers: list[str] = []
    for row in rows:
        if not row[col["公司簡稱"]]:
            continue
        board = row[col["上市別"]]
        total[board] += 1
        listed = any(row[col[c]] for c in LISTING_COLUMNS)
        delisted = row[col["下市日期"]]
        if not listed or (delisted and delisted < SKELETON_START):
            continue
        kept[board] += 1
        tickers.append(str(row[col["公司簡稱"]]).strip().split(" ")[0])
    wb.close()

    existing = {p.stem for p in raw_workbooks(settings.raw_dir_for("adj_daily_prices"))}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(tickers) + "\n", encoding="utf-8")

    print(f"{sum(total.values())} companies in {source.name}")
    for board in sorted(total, key=lambda b: -total[b]):
        print(f"  {board:<6} {kept[board]:>5} of {total[board]:>5} eligible")
    print(f"{len(tickers)} eligible, {len(set(tickers) - existing)} without a workbook")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
