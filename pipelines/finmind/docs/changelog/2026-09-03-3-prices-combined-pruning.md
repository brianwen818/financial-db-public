# 2026-09-03 (3) — `v_prices_combined` 可裁剪化，與三處靜默失效的修補

`v_prices_combined` 新增 `year` / `month` 欄位並改變 JOIN 鍵，新增
`prices_combined_between()` macro；另修 `v_missing_trading_days` 的 `NOT IN`
NULL 陷阱、view 載入順序的排序假設，並把 `api.max_retries` 接上。

## 為什麼

一次全面審視（實作 vs `docs/` vs notebook）發現四件事：

**1. 整個架構的效能敘事建立在分區裁剪上，但唯一推薦用來比較還原/未還原價的
view 完全無法裁剪。** `v_prices_combined` 沒有輸出 `year`/`month`，也沒有對應
macro。實測 2024Q1（141,940 列）：

| 查詢 | 時間 |
|---|---|
| `daily_prices_between('2024-01-01','2024-03-31')` | 0.16s |
| `v_prices_combined` 只加 `date` 條件 | 1.31s |

**只補欄位不夠**：`year` 條件只會裁剪未還原那一側，JOIN 仍會打開全部還原價
footer（8,060 個）。實測補欄位後仍要 0.80s。

**2. `v_missing_trading_days` 會靜默失效。** 原本寫
`c.date NOT IN (SELECT date FROM v_known_empty_days)`。只要那份 parquet 出現一個
NULL `date`，`NOT IN` 對每一列都回 UNKNOWN，整個 view 回 0 列——缺漏偵測會安靜
地關掉，而不是大聲壞掉。同一份 SQL 的 `21_v_stock_info.sql` 已經用了正確的
`NOT EXISTS`。

**3. view 載入順序的排序假設會在加目錄時失效。** `sql_files()` 用
`sorted(SQL_DIR.rglob("*.sql"))` 排完整路徑，**目錄名先比較**。日後加
`sql/marts/30_v_x.sql` 會排在 `sql/views/20_v_trading_calendar.sql` 之前，編號
前綴就失效。另外 `p.parent.name != "queries"` 只擋一層，
`sql/queries/adhoc/x.sql` 會被當 DDL 執行。

**4. `api.max_retries` 是死設定。** `config/datasets.yml:83` 有，`settings.py` 有讀，
但 `finmind_client.py` 用 `@retry(stop=stop_after_attempt(5))` 裝飾器硬寫。裝飾器
在 class 定義時就綁定，看不到 instance 設定，改 YAML 沒有任何效果。

## 改了什麼

**資料**：無。沒有重抓、沒有重建 processed 層。

**View**（`sql/views/24_v_adj_daily_prices.sql`）：
- `v_prices_combined` 新增 `year INTEGER`、`month INTEGER` 兩欄（14 → 16 欄）
- JOIN 鍵由 `USING (stock_id, date)` 改為 `USING (stock_id, date, year, month)`

多加的兩個鍵在建構上是冗餘的——`year`/`month` 就是 `day_partition_path()` 從
`date` 推出來的 Hive 鍵，日期相同必然年月相同。加上去純粹是讓 DuckDB 能把
`year` 條件傳播到兩側。**已全量驗證等價**：兩種寫法都是 11,726,045 列、
499,202 列 `adj_close` 為 NULL。

**View**（`sql/views/26_v_coverage.sql`）：`NOT IN` 改為 `NOT EXISTS`。

**Macro**（`sql/views/27_macros.sql`）：新增 `prices_combined_between(lo, hi)`。

**程式**：
- `database/build.py` — `sql_files()` 改用**檔名**排序；`queries/` 改為在路徑
  任一層都排除
- `utils/finmind_client.py` — `@retry` 裝飾器改為在 `__init__` 建 `Retrying`
  物件，`api.max_retries` 現在真的生效。`_request` 拆成重試前門 + `_request_once`
- `scripts/verify.py` — 新增三條 macro smoke test。`build.verify()` 只列舉
  `information_schema.tables` 的 VIEW，table macro 不在裡面，所以文件主推的查詢
  入口原本是唯一沒被測到的東西
- `tests/test_build_db.py` — 新檔。`database/build.py` 原本沒有任何測試覆蓋

**排程**：無變更。

## 對既有查詢的影響

**`SELECT *` 會多兩欄。** `v_prices_combined` 從 14 欄變 16 欄。用
`SELECT * FROM v_prices_combined` 再依位置取欄位的程式要檢查；依名稱取的不受影響。
notebook 第 23 格的 `DESCRIBE v_prices_combined` 現在回 16 列。

**列數與值完全不變**（已全量驗證，見上）。

**舊寫法不會壞，只是慢。** `v_prices_combined WHERE date BETWEEN ...` 仍然正確，
只是不裁剪。改用 `prices_combined_between()` 可從 1.31s 降到 0.29s。

**`v_missing_trading_days` 目前結果不變**（改動前後都是 0 列）——修的是未來的
失效模式，不是現在的錯誤。

## 已知限制

- **裁剪只到 4.5 倍，不是單表的完整效果。** JOIN 兩份資料集本身有成本，
  0.29s 對單表的 0.16s。
- **`v_thin_trading_days` 的視窗仍然包含被測的那一天**，會把自己的中位數拉低，
  偏向漏報（2026-08-26 以 ratio 0.5008 溜過）。刻意不動：偏保守是修復觸發器的
  安全方向。已在 SQL 註解寫明。
- **`v_ingestion_runs` 仍然脆弱**：`union_by_name` 讀 JSONL，`metrics`/`errors`
  無型別，任一份 manifest 型別不一致就會在讀取時爆掉；`logs/runs/` 也不會被清。
  只加了註解，沒有改結構。
- **`v_last_trading_day` / `v_daily_coverage` / `v_missing_trading_days` 仍是全表
  掃描**（實測分別 0.69s / 2.06s）。它們本質上要看全部日期，沒有便宜的寫法。

## 驗證

```
pytest tests -q          206 passed（新增 test_build_db.py 7 條）
ruff check src scripts tests   All checks passed
scripts/build_db.py      19 views
scripts/verify.py        27 checks · 26 passed · 1 warning · 0 failures
```

`verify.py` 的 1 個 warning 是 2026-08-26 那次 backfill 撞到 HTTP 402 的歷史紀錄，
與本次改動無關。

notebook 的全部 19 段 SQL 已逐一對資料庫執行，0 失敗。

實測基準（2026-09-03 當日 18:30 排程跑完後）：
`v_daily_prices` 11,726,045 列 / 3,612 檔；`v_adj_daily_prices` 11,226,843 列 /
3,152 檔；`v_prices_combined` 11,726,045 列，`adj_close` NULL 499,202 列、
`adj_factor` NULL 784,674 列（多出的 285,472 列是未還原 `close` 為 0 的停牌日）。
