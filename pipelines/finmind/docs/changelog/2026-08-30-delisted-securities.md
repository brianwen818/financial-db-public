# 2026-08-30 — 已下市證券與存活者偏誤

找回 767 檔已下市證券並回補行情，修掉未還原行情的存活者偏誤。還原行情的偏誤
因上游限制**無法**修復。同時修正三個會讓排程工作長期浪費或損壞資料的邏輯問題。

## 為什麼

`TaiwanStockInfo` 是當下狀態清單，只列出目前在市的證券。歷史回補照它逐檔抓，
所以在本專案第一份快照（2026-08-26）之前就下市的公司**從未被抓進來**。

實測影響：2010-09-15 的橫斷面（不含指數）應有 1,585 檔，缺了 145 檔，佔 9.1%。任何選股回測都
會系統性高估績效，因為「後來倒掉的贏家」全部不在資料裡。

驗證方式：對 2004-02-11 起每月抽一個交易日做全市場呼叫（271 次），把當日實際
交易的代碼與目前 master 比對。

## 抽樣密度的取捨

| 密度 | 呼叫數 | 找到的已下市普通股 |
|---|---|---|
| 每年一天 | 23 | 425 |
| 每月一天 | 271 | 441 |
| 每天（未執行） | 5,551 | — |

十二倍的呼叫只多找到 16 檔，其中只有 3 檔存活不到兩個月。曲線已經收斂，所以
停在月頻，不做逐日 sweep。

## 資料變更

| 項目 | 之前 | 之後 |
|---|---|---|
| `v_daily_prices` | 11,190,625 列 / 3,136 檔 | **11,707,723 列 / 3,602 檔** |
| `v_adj_daily_prices` | 11,183,723 列 / 3,136 檔 | 11,207,511 列 / 3,143 檔 |
| 完整 universe | 3,138 檔 | **3,905 檔** |
| processed 儲存 | — | daily-prices 381 MB / adj 660 MB |

已下市證券 767 檔的來源組成：`TaiwanStockDelisting` 獨有 320、抽樣獨有 296、
兩者都有 151。471 檔有名稱，296 檔只有代碼。

## Schema 變更

**新增 dataset**（`config/datasets.yml`）

- `tw_delisting` — `TaiwanStockDelisting`，723 筆
- `discovered_universe` — 全市場抽樣的觀測結果，非 FinMind 資料集

**新增 processed 表**

- `processed/tw-delisting/tw_delisting.parquet`
- `processed/delisted-securities/delisted_securities.parquet`

**新增 View**

- `v_delisted_securities` — 767 檔，含 `shape`、`first_seen`、`last_seen`、`source`
- `v_securities_all` — master + 已下市，3,905 檔，含 `is_delisted`、`delisted_date`

**變更 View**

- `v_daily_prices_enriched` 改 JOIN `v_securities_all`（原本是 `v_stock_info_latest`），
  **新增兩個欄位** `is_delisted`、`delisted_date`

## 對既有查詢的影響

**會變的：**

- `SELECT *` 從 `v_daily_prices` 會多出 466 檔證券的資料。任何未限定 `stock_id`
  的歷史聚合（每日成交總額、市場寬度、橫斷面排名）結果都會改變——這是修正，不是
  迴歸。
- `SELECT *` 從 `v_daily_prices_enriched` 會多兩個欄位。
- 依 `v_stock_info_latest` JOIN 行情的查詢**行為不變**，但現在會靜默丟掉已下市
  證券。回測請改用 `v_securities_all`。

**不會變的：**

- 所有指定 `stock_id` 的查詢。
- `v_stock_info_latest`、`v_securities`、`v_indices` 的內容。
- 兩個日期範圍 macro 的用法與分區裁剪行為。

## 排程與更新邏輯的修正

三個問題，第一個是這次改動造成的，後兩個原本就潛伏著。

**1. 缺口修復會洗掉已下市證券。** `daily_update.py` 會修復觀測範圍內任何歷史日
的缺口，並用 master 過濾全市場回應。加入已下市證券之前這個過濾是 no-op；之後只
要觸發修復，那一天的已下市證券就會被刪掉。
→ 修復用的 universe 改為 `load_universe(include_delisted=True)`。

**2. 未來的下市會變成永久 stale。** `load_universe()` 跨所有快照聯集，下市的股票
會永遠留在裡面，`last_seen` 凍結，每週被判 stale 並重抓不會再變的歷史。
→ 新增 `load_current_universe()`（只讀最新快照），對帳的 absent/stale 改用它。

**3. FinMind 的 master 本身是髒的。** 315 檔「在市」證券的最後一根 K 超過 20 個
交易日，其中 250 檔已在 `TaiwanStockDelisting` 名單上（`9915` 2008 年下市，至今
仍被列為在市）。每週工作一直在重抓它們。
→ 每週多 1 次呼叫重抓下市清單，並用它覆寫 master 的判斷。若最後一根 K 晚於下市
日則視為復牌、繼續檢查。

**附帶：**對帳的 `missing` 檢查原本沒有排除 `v_known_empty_days`，導致 2026-07-10
那個颱風假每週被重抓一次。已排除，與 `v_missing_trading_days` 一致。

實測效果：

```
stale   315 → 65      absent   2 → 1      missing   1 → 0
```

每週固定省下約 250 次呼叫。

## 已知限制

**1. 還原行情仍有存活者偏誤，且修不了。**

767 檔中，未還原有 466 檔有資料，**還原只有 7 檔**。已逐檔向 API 驗證（`4103`
百略醫學、`5480` 統盟電子、`1787` 福盈科技、`4947` 昂寶電子等），
`TaiwanStockPriceAdj` 對已下市證券回空。這是上游缺口。

所以回測是二選一：無偏誤 universe（`v_daily_prices`，自行處理除權息），或現成
還原價（`v_adj_daily_prices`，survivors-only，須在結論標註）。

**2. 2004-02-11 是硬地板。**

全市場資料與已下市個股的逐檔歷史都從這天開始（`2311` 日月光 1989 年就上市，之前
仍無資料）。pre-2004 只有 919 檔有資料，全是存活者。無偏誤的橫斷面最早只到
2004-02-11。

**3. 767 檔中 301 檔完全沒有行情**，因為它們在 2004-02-11 之前就下市。

**4. 已下市證券沒有產業分類**，且 296 檔連名稱都沒有。產業分組分析要先用
`is_delisted` 篩掉。

**5. `is_delisted` 的定義是「不在最新一份 master 快照裡」。** FinMind 錯誤地繼續
列著的 251 檔（如 `9915`）會顯示為在市。這是繼承上游的錯誤，比自行猜測好。

## 程式變更

**新增**

- `src/finmind_pipeline/processing/universe.py` — 代碼形狀分類
- `src/finmind_pipeline/ingestion/universe_discovery.py` — 全市場抽樣 sweep
- `src/finmind_pipeline/ingestion/tw_delisting.py`
- `scripts/discover_delisted.py`
- `tests/test_universe.py`、`tests/test_reconcile_scope.py`

**修改**

- `tw_stock_info.py` — 新增 `load_current_universe()`、`load_delisted_universe()`；
  `load_universe()` 新增 `include_delisted` 參數（預設 `False`，維持既有呼叫端行為）
- `dimensions.py` — 新增 `process_delisting()`、`process_delisted_securities()`
- `backfill.py` — 新增 `--universe {all,listed,delisted}`
- `daily_update.py`、`weekly_refresh.py` — 見上方排程修正
- `verify.py` — scope 檢查改對 `v_securities_all`；新增一列已下市涵蓋率報告
- `sql/views/21_v_stock_info.sql`、`23_v_daily_prices.sql`

**兩個原本會壞掉的既有檢查**：`test_no_warrants_leaked_in` 與 `verify.py` 的 scope
檢查都斷言「行情中每個代碼都在 `v_stock_info_latest` 裡」——那正是把存活者偏誤
鎖進系統的斷言。兩者都改為對 `v_securities_all` 檢查。

## 執行紀錄

```
discover_delisted.py    272 calls    234s
backfill --universe delisted
  daily_prices          767 tickers → 466 with data, 517,098 rows, 0 failed
  adj_daily_prices      767 tickers → 7 with data
processed 全量重建       daily 8,054 days · adj 8,053 days
```

## 驗證

```
ruff       All checks passed
pytest     199 passed
verify.py  23 checks · 22 passed · 1 warning · 0 failures
```

唯一的 warning 是 2026-08-26 一次舊的 backfill 失敗紀錄，與本次無關。

## 重現方式

```powershell
.\.venv\Scripts\python.exe scripts\discover_delisted.py
.\.venv\Scripts\python.exe scripts\backfill.py --universe delisted
.\.venv\Scripts\python.exe scripts\build_db.py
```

`discover_delisted.py` 不需排程。它找回的是歷史集合，不會再長大；未來的下市由每日
與每週工作自動處理。約一年跑一次當稽核即可。
