# Changelog

每次**改動資料庫架構**或**加入新資料**時，在這個資料夾新增一份文件。

## 什麼情況要寫

- 新增或移除資料集（新的 FinMind dataset、新的資料來源）
- 新增、刪除或改變既有 View 的欄位
- 改變 processed 層的 schema、分區方式或去重規則
- 改變 universe 的定義（哪些證券算在範圍內）
- 改變排程工作的行為或成本

單純修 bug、改文字、加測試不用寫。判準是：**三個月後的你（或一個 AI agent）
如果不知道這件事，會不會誤讀資料或寫錯查詢？** 會的話就寫。

## 檔名

```
YYYY-MM-DD-短標題.md
```

例：`2026-08-30-delisted-securities.md`。同一天有多份就加序號。

## 每份文件要有

| 段落 | 內容 |
|---|---|
| 日期與一句話摘要 | 改了什麼 |
| 為什麼 | 觸發這次改動的問題，附上證據或數字 |
| 改了什麼 | 資料、schema、View、程式、排程，分開列 |
| 對既有查詢的影響 | **最重要**——舊的查詢會不會壞、會不會結果變了 |
| 已知限制 | 這次沒解決的部分，以及為什麼 |
| 驗證 | 測試、verify.py 的結果 |

數字一律寫實測值，不要寫估計值。若當下無法驗證，明說那是估計。

## 索引

| 日期 | 變更 | 影響 |
|---|---|---|
| 2026-08-30 | [已下市證券與存活者偏誤](2026-08-30-delisted-securities.md) | 新增 767 檔已下市證券、2 個 View；`v_daily_prices` 列數 +4.6% |
| 2026-09-03 | [產業鏈維度表去重](2026-09-03-1-industry-dedupe.md) | `v_industry` 11,520 → 6,871 列；`snapshot_date` 語意改為「最後一次被看到」 |
| 2026-09-03 | [證券主檔依內容去重](2026-09-03-2-stock-info-dedupe.md) | `v_stock_info_history` 7,633 → 4,321 列；`snapshot_date` 語意改為「首見日」；verify 新增一條檢查 |
| 2026-09-03 | [`v_prices_combined` 可裁剪化](2026-09-03-3-prices-combined-pruning.md) | 該 view 新增 `year`/`month`（14 → 16 欄）並改 JOIN 鍵；新增 `prices_combined_between()` macro；修 `NOT IN` NULL 陷阱與 view 載入順序 |
| 2026-10-04 | [還原價去重改為 ticker 優先](2026-10-04-adj-dedupe-ticker-first.md) | `v_adj_daily_prices` 列數不變，但 2026-08-21..09-11 有 319 檔、1,046 列被修正（原本混入舊還原基準）；verify 新增一條檢查 |
