# 2026-10-04 — 還原價去重改為 ticker 優先

`adj_daily_prices` 的 `(stock_id, date)` 去重由「全市場日檔優先」改為「ticker 檔
優先」。未還原價維持全市場優先。`v_adj_daily_prices` 列數不變，但 2026-08-21 至
2026-09-11 之間有 319 檔、1,046 列的價格被修正。

## 為什麼

用 TEJ 還原價逐日比對報酬時，發現 `v_adj_daily_prices` 在某些股票的除權息日前
出現假性斷點，而 FinMind 自己的 ticker 檔沒有這個問題：

| 6669 緯穎 | ticker 檔 | 全市場日檔 | 修正前 processed | TEJ |
|---|---|---|---|---|
| 2026-08-20 | 2,120.49 | —（無日檔） | 2,120.49 | 2,120.49 |
| 2026-08-21 | 2,097.03 | 6,255.00 | **6,255.00** | 2,097.02 |
| 2026-08-28 | 2,413.84 | 7,200.00 | **7,200.00** | 2,413.84 |
| 2026-08-31 | 2,378.64 | 2,378.64 | 2,378.64 | 2,378.63 |

6669 在 2026-09-02 除權。processed 層因此在 08-20→08-21 出現 +195%、在
08-28→08-31 出現 −67% 的單日報酬，兩者都不存在於真實市場。

**原因是去重規則，不是上游資料。** `_union_sql()` 對兩個價格資料集一律讓
`market` 勝過 `ticker`。這對未還原價是對的（全市場日檔是收盤後的結算快照），
對還原價卻是錯的：

- 還原價的每個值只在「抓取當下的還原基準」下有意義，每次除權息都會讓整段歷史
  換基準。
- ticker 檔是單一基準下的完整歷史；全市場日檔只是某一天在抓取當下那個基準的
  快照，而且離開 3 天的 revision window 後就不會再重抓。
- 每週 `weekly_refresh.py` 重抓了正確的 ticker 全歷史，接著 `rebuild_from_raw()`
  又用舊基準的日檔把最近幾週蓋回去。所以這個錯**每週重抓也修不好**，而且日檔
  累積越多、受影響的範圍越大。

實測（raw 層，2026-10-04）：ticker 檔與全市場日檔同時存在的 44,984 列中，
1,046 列（319 檔）的收盤價相差超過 0.1%。

## 改了什麼

**資料**：`processed/adj-daily-prices/` 由 raw 全量重建（staged swap，未呼叫
API）。`raw/` 沒有動。

**設定**（`config/datasets.yml`）：`adj_daily_prices` 新增
`dedupe_prefer: "ticker"`。`daily_prices` 不設，預設為 `"market"`。

**程式**：
- `settings.py` — `DatasetConfig` 新增 `dedupe_prefer`（預設 `"market"`）
- `processing/prices.py` — 新增 `_source_rank()`；`_union_sql()` 的排序依
  `dedupe_prefer` 決定，未知值會 `ValueError`
- `scripts/verify.py` — 新增一條檢查：`v_adj_daily_prices` 中 `source = 'market'`
  的日數超過 7 天就 WARN
- `tests/test_processing.py` — 新增 4 條測試（ticker 優先、日檔仍補齊 ticker 檔
  之後的日子、兩個資料集各自的設定、未知設定值被拒絕）

**View / Schema / 排程**：無變更。

## 對既有查詢的影響

**`v_adj_daily_prices`、`v_prices_combined` 的列數與欄位不變**（11,252,462 列、
3,154 檔），但 2026-08-21 至 2026-09-11 之間，在這段期間有除權息的股票，
`open/high/low/close` 與 `adj_factor` 會變。跨過這段期間算報酬的結果會不同——
舊結果是錯的。

**`source` 欄的分布變了。** 修正前 `source = 'market'` 涵蓋 19 個交易日
（2026-08-21 起）；修正後只剩 6 天。用 `source` 當條件的查詢要重新檢查。

**未還原價完全不受影響。**

## 已知限制

- **最後一次 ticker 重抓之後的日子仍可能是不同基準。** 這些日子只有全市場日檔，
  若其間有除權息，除權息日之後的列會是新基準、之前的歷史仍是舊基準，要到下一次
  每週重抓才會一致。這是還原價「不能 append」的本質，窗口最長一週；
  `verify.py` 的新檢查在窗口超過 7 個交易日時會警告。
- **FinMind 排程自 2026-09-19 起因 token 降級而全數失敗**，目前資料停在
  2026-09-16，ticker 檔停在 2026-09-13 的重抓。恢復排程後第一次每週重抓會把
  基準對齊。
- **上游本身的還原缺口不在這次修正範圍內**：上市股 2003 年以前、上櫃股 2008 年
  以前的除權息，FinMind 還原價並未調整。這是上游行為，見
  `exports/grad-school-applications/outputs/tables/cmp_unadjusted_actions_by_market_year.csv`。

## 驗證

```
pytest tests -q                  210 passed（新增 4 條）
ruff check src scripts tests     All checks passed
scripts/build_db.py              19 views
scripts/verify.py                28 checks · 27 passed · 1 warning · 0 failures
```

`verify.py` 新檢查在修正前回報 `WARN 19 day(s)`，修正後 `PASS 6 day(s)`。
剩下的 1 個 warning 是歷史上失敗的排程紀錄（18 次，多數為 token 降級）。
