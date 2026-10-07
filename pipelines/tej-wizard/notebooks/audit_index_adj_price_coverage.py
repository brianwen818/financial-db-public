"""Audit all supplied TM100/TWN50 workbooks against read-only TEJ prices.

Run with the py312 environment (duckdb, pandas, openpyxl required):
    python audit_index_adj_price_coverage.py --as-of 2026-09-05
Outputs are UTF-8 BOM CSVs, a Markdown report, and a JSON audit summary.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import re

import duckdb
import openpyxl
import pandas as pd


def audit(root: Path, as_of: date) -> dict:
    db_root = root / "db/tej-wizard"
    source = db_root / "raw/daily-index-constituents"
    output = db_root / "notebooks-outputs/tm100_twn50_adj_price_coverage"
    records, manifest = [], []
    for index in ("TM100", "TWN50"):
        files = sorted((source / index).rglob("*.xlsx"))
        if not files:
            raise ValueError(f"No source files for {index}")
        for path in files:
            if path.name.startswith("~$"):
                continue
            workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
            count, dates, ids, future = 0, set(), set(), 0
            try:
                for sheet in workbook:
                    rows = sheet.iter_rows(values_only=True)
                    header = next(rows)
                    if tuple(header[:3]) != ("公司代碼", "年月日", "成份股"):
                        raise ValueError(f"Unexpected header: {path}/{sheet.title}: {header}")
                    for rownum, row in enumerate(rows, 2):
                        if all(v is None for v in row):
                            continue
                        label, day, constituent = row[:3]
                        if str(label).split()[0] != index or not isinstance(day, datetime):
                            raise ValueError(f"Invalid index/date: {path}:{rownum}")
                        match = re.fullmatch(r"(\S+)\s+(.+)", str(constituent).strip())
                        if not match:
                            raise ValueError(f"Invalid constituent: {path}:{rownum}: {constituent}")
                        stock_id, name = match.groups()
                        day = day.date()
                        records.append((index, day, stock_id, name, path.relative_to(source).as_posix()))
                        count += 1
                        dates.add(day)
                        ids.add(stock_id)
                        future += day > as_of
            finally:
                workbook.close()
            manifest.append(dict(source_file=path.relative_to(source).as_posix(),
                                 sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                                 rows=count, securities=len(ids), first_date=min(dates),
                                 last_date=max(dates), future_rows=future))
            print(f"Read {index}/{path.name}: {count:,} rows", flush=True)

    raw = pd.DataFrame(records, columns=["index", "date", "stock_id", "stock_name", "source_file"])
    keys = ["index", "date", "stock_id"]
    conflicts = raw.groupby(keys).stock_name.nunique()
    if (conflicts > 1).any():
        raise ValueError("Conflicting constituent names on identical index/date/stock keys")
    members = raw.sort_values(keys + ["source_file"]).drop_duplicates(keys)
    historical = members[members.date <= as_of]
    with duckdb.connect(str(db_root / "tej_wizard.duckdb"), read_only=True) as con:
        existing = con.execute("""
            SELECT stock_id, last(stock_name ORDER BY date) AS db_stock_name,
                   min(date) AS price_first_date, max(date) AS price_last_date,
                   count(*) AS price_rows, count(close) AS valid_close_rows,
                   count(*) FILTER (WHERE open IS NOT NULL AND high IS NOT NULL
                       AND low IS NOT NULL AND close IS NOT NULL) AS valid_ohlc_rows
            FROM v_adj_daily_prices GROUP BY stock_id ORDER BY stock_id
        """).df()
    existing.stock_id = existing.stock_id.astype(str)
    raw_files = {}
    for path in sorted((db_root / "raw/adj-daily-prices-ticker").rglob("*.xlsx")):
        if not path.name.startswith("~$"):
            raw_files.setdefault(path.stem, []).append(path.relative_to(db_root).as_posix())

    rows = []
    for stock_id, group in members.groupby("stock_id", sort=True):
        ordered = group.sort_values(["date", "index"])
        item = dict(stock_id=stock_id, stock_name=ordered.iloc[-1].stock_name,
                    historical_names="; ".join(sorted(set(group.stock_name))),
                    indices="; ".join(sorted(set(group["index"]))),
                    in_TM100=bool((group["index"] == "TM100").any()),
                    in_TWN50=bool((group["index"] == "TWN50").any()),
                    first_membership_date=min(group.date), last_membership_date=max(group.date),
                    observed_by_as_of=bool((group.date <= as_of).any()),
                    raw_xlsx_present=stock_id in raw_files,
                    raw_xlsx_files="; ".join(raw_files.get(stock_id, [])))
        for index in ("TM100", "TWN50"):
            part = group[group["index"] == index]
            item[f"{index}_first_date"] = min(part.date) if len(part) else None
            item[f"{index}_last_date"] = max(part.date) if len(part) else None
        rows.append(item)
    coverage = pd.DataFrame(rows).merge(existing, on="stock_id", how="left", validate="one_to_one")
    coverage["has_db_price_rows"] = coverage.price_rows.fillna(0) > 0
    coverage["has_adj_price"] = coverage.valid_close_rows.fillna(0) > 0
    coverage["action"] = coverage.apply(lambda r: "已有還原價" if r.has_adj_price else
        ("已有Excel，需檢查並匯入" if r.raw_xlsx_present else "需蒐集Excel"), axis=1)
    missing = coverage[~coverage.has_adj_price].copy()
    to_collect = missing[~missing.raw_xlsx_present].copy()
    extra = existing[~existing.stock_id.isin(coverage.stock_id)].copy()
    by_index = []
    for index in ("TM100", "TWN50"):
        group = members[members["index"] == index]
        subset = coverage[coverage[f"in_{index}"]]
        by_index.append(dict(index=index, first_date=min(group.date), last_date=max(group.date),
                             historical_securities=len(subset), existing=int(subset.has_adj_price.sum()),
                             missing=int((~subset.has_adj_price).sum()), unique_membership_rows=len(group)))
    annual = raw[raw.source_file.str.match(r"(?:TM100|TWN50)/\d{4}\.xlsx$")]
    nonannual = raw[~raw.source_file.str.match(r"(?:TM100|TWN50)/\d{4}\.xlsx$")]
    annual_keys = set(map(tuple, annual[keys].itertuples(index=False, name=None)))
    other_keys = set(map(tuple, nonannual[keys].itertuples(index=False, name=None)))
    summary = dict(as_of=str(as_of), generated_at=datetime.now().astimezone().isoformat(),
                   source_root=str(source), database=str(db_root / "tej_wizard.duckdb"),
                   source_files=len(manifest), raw_membership_rows=len(raw),
                   unique_membership_rows=len(members), duplicate_membership_rows=len(raw)-len(members),
                   source_future_rows=int((raw.date > as_of).sum()),
                   future_only_stock_ids=sorted(set(members.stock_id)-set(historical.stock_id)),
                   nonannual_extra_membership_keys=len(other_keys-annual_keys),
                   nonannual_extra_stock_ids=sorted(set(nonannual.stock_id)-set(annual.stock_id)),
                   union_securities=len(coverage), existing_union_securities=int(coverage.has_adj_price.sum()),
                   missing_union_securities=len(missing), collect_excel_securities=len(to_collect),
                   db_total_securities=len(existing), db_extra_stock_ids=extra.stock_id.tolist(),
                   db_price_first_date=str(existing.price_first_date.min().date()),
                   db_price_last_date=str(existing.price_last_date.max().date()),
                   by_index=by_index)
    assert len(coverage) == int(coverage.has_adj_price.sum()) + len(missing)
    assert set(missing.stock_id).isdisjoint(set(existing.loc[existing.valid_close_rows > 0, "stock_id"]))
    output.mkdir(parents=True, exist_ok=True)
    tables = dict(all_historical_constituents=coverage, missing_adj_price=missing,
                  collect_excel=to_collect, existing_adj_price=existing, db_only_securities=extra,
                  index_summary=pd.DataFrame(by_index), source_files=pd.DataFrame(manifest))
    for name, frame in tables.items():
        frame.to_csv(output / f"{name}.csv", index=False, encoding="utf-8-sig", date_format="%Y-%m-%d")
    (output / "collect_stock_ids.txt").write_text("\n".join(to_collect.stock_id) + "\n", encoding="utf-8")
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    report = ["# TM100、TWN50 歷史成分股還原價盤點", "",
              f"盤點基準日：{as_of}。讀取來源 Excel {len(manifest)} 份，DuckDB 採唯讀連線。", "",
              "| 指數 | 來源起日 | 來源迄日 | 歷史成分股 | 已有還原價 | 缺少還原價 |",
              "|---|---|---|---:|---:|---:|"]
    for row in by_index:
        report.append(f"| {row['index']} | {row['first_date']} | {row['last_date']} | {row['historical_securities']} | {row['existing']} | {row['missing']} |")
    report += ["", f"兩指數聯集 {len(coverage)} 檔；已有還原價 {int(coverage.has_adj_price.sum())} 檔；缺少 {len(missing)} 檔，其中需蒐集 Excel {len(to_collect)} 檔。",
               f"資料庫共 {len(existing)} 檔，日期 {summary['db_price_first_date']} 至 {summary['db_price_last_date']}；聯集以外：{', '.join(extra.stock_id)}。", "",
               "## 判定口徑與來源檢查", "",
               "- 股票代碼以字串比對。名稱採來源中最後觀測名稱，另保留所有歷史名稱。",
               "- 已有還原價：v_adj_daily_prices 至少一筆 close 非空值；不代表每一天或整段歷史皆完整。價格起迄、有效 close/OHLC 筆數保留於 CSV。",
               "- raw Excel 是否存在依 adj-daily-prices-ticker 的檔名判斷；有檔但未入庫者另標示，檔案內容未在本次驗證。",
               "- 所有來源檔皆納入，以指數、日期、股票代碼去重；成分資料中的前日調整收盤價不當作已蒐集的日線還原價資料。",
               f"- 重複成分列 {summary['duplicate_membership_rows']:,} 筆；非年度檔新增成分鍵 {summary['nonannual_extra_membership_keys']} 筆、新增股票 {summary['nonannual_extra_stock_ids']}。",
               f"- 晚於盤點日的來源列 {summary['source_future_rows']:,} 筆；僅出現在未來日期的股票：{summary['future_only_stock_ids']}。聯集採所有提供資料，CSV 的 observed_by_as_of 可辨識歷史已觀測股票。",
               "- 歷史範圍僅限提供的檔案，不能据此斷言涵蓋指數自成立以來所有歷史。",
               "- source_files.csv 記錄每份來源檔的日期、筆數與 SHA-256，便於重跑核對。", "",
               "## 待補股票", "", "| 股票代碼 | 來源名稱 | 曾屬指數 | 處理方式 |", "|---|---|---|---|"]
    report.extend(f"| {r.stock_id} | {r.stock_name} | {r.indices} | {r.action} |" for r in missing.itertuples())
    (output / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    args = parser.parse_args()
    audit(args.root, args.as_of)
