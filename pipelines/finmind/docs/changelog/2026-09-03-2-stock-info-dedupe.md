# 2026-09-03 (2) — 證券主檔依內容去重

`tw_stock_info` 的 processed 層改為依**內容**去重，`snapshot_date` 取 `min()`，
語意變成「這組內容第一次被看到的日期」。`v_stock_info_history` 從 7,633 列回到
4,321 列。同時在 `verify.py` 補上守門檢查，並更新四處已經漂掉的硬編數字。

這是 [2026-09-03 (1) 產業鏈維度表去重](2026-09-03-1-industry-dedupe.md) 的同類問題，
在盤點整個 pipeline 時發現的——同樣的缺陷，更大的表，而且沒有任何檢查在看它。

## 為什麼

`TaiwanStockInfo` 的 `date` 欄位在大多數列上是**爬取時間戳**：FinMind 每次重爬就
把它改成當天。`process_stock_info()` 原本做 `SELECT DISTINCT *`（`*` 含
`snapshot_date`），而 `_read_all_snapshots()` 聯集所有歷史快照，所以同一筆事實
每被重爬一次就多一列。

2026-09-03 這次擷取的實測拆解：

| | 列數 |
|---|---|
| 帶著 `2026-09-03` 戳記進來的列 | 3,317 |
| 其中內容與既有紀錄完全相同 | **3,305** |
| 真正新增或變更 | **12** |

**這比 industry 那個嚴重，因為它破壞的是這張表存在的理由。**
`process_stock_info()` 的 docstring 明講保留完整快照史是因為「那是產業重分類何時
發生的唯一紀錄」。每次擷取塞進 3,300 列假變更之後，真正的重分類就跟重爬雜訊無法
區分了。

2330 修正前有 4 列——兩筆事實各被戳兩次：

```
2026-08-26  台積電  電子工業  twse
2026-08-26  台積電  半導體業  twse
2026-09-03  台積電  電子工業  twse   ← 內容完全相同
2026-09-03  台積電  半導體業  twse   ← 內容完全相同
```

**增長速率：**每日工作（週一～六）加週日的 weekly refresh 共 7 次擷取／週，每次
約 +3,305 列。一年約 120 萬列（**推估值**），而這張表描述的只有 3,147 檔證券。

**為什麼一直沒被發現：**`verify.py` 有 industry 的唯一性檢查，卻沒有 stock_info 的。
更巧的是，唯一不會增長的正是那 32 檔指數——它們的 `date` 是字串 `"None"`、轉成
NULL 之後永遠不變，所以 `DISTINCT *` 收得掉。`stock_info NULL snapshot_date == 32`
這條檢查每次都 PASS，剛好把問題蓋住。

## 改了什麼

**去重規則**（`processing/dimensions.py` → `process_stock_info()`）

```diff
-SELECT DISTINCT * FROM t ORDER BY stock_id, snapshot_date NULLS FIRST
+SELECT min(snapshot_date) AS snapshot_date, stock_id, stock_name,
+       industry_category, industry_category_norm, type, is_index
+FROM t
+GROUP BY stock_id, stock_name, industry_category, industry_category_norm, type, is_index
+ORDER BY stock_id, snapshot_date NULLS FIRST
```

用 `min()` 而不是 industry 那邊的 `max()`：industry 沒有歷史，取最後看到的日期即可；
主檔的每一列都是一筆歷史事實，要的是它**第一次**成立的日期。取 `max()` 會讓所有列
都變成今天，等於把歷史刪掉。

**資料**

| 項目 | 之前 | 之後 |
|---|---|---|
| `v_stock_info_history` 列數 | 7,633 | **4,321** |
| 不重複內容組合 | 4,321 | 4,321 |
| 相異 `snapshot_date`（不含 NULL） | 266 | **263** |
| NULL `snapshot_date`（指數） | 32 | 32 |
| 帶 `2026-09-03` 戳記的列 | 3,317 | **12** |
| 2330 的列數 | 4 | 2 |

修正後帶著 2026-09-03 的那 12 列，正是 8/26 到 9/03 之間新上市的證券
（`00409A`、`00411A`、`009827`、`009828`、`00987D` 五檔 ETF，加 `3054`、`3718`、
`4530`、`4747`、`6950`、`7686`、`7932` 七檔個股）。這正是這張表該記的東西。

消失的三個日期是 `2026-08-27`(5 列)、`2026-08-31`(1 列)、`2026-09-02`(1 列)：它們
帶的內容在 8/26 就已經存在，`min()` 把它們歸回 8/26。

**Schema：**欄位沒有增減，`PROCESSED_STOCK_INFO_SCHEMA` 未動。改變的是
`snapshot_date` 的語意——從「某一次爬取的戳記」變成「這組內容第一次被看到的日期」。

**新增檢查**（`scripts/verify.py`）

```python
self.expect_zero(
    "stock_info has no content duplicates",
    "SELECT count(*) FROM (SELECT stock_id, stock_name, industry_category, "
    "type, is_index FROM v_stock_info_history GROUP BY 1,2,3,4,5 HAVING count(*) > 1)",
)
```

與既有的 industry 唯一性檢查對稱。那條檢查讓 industry 的問題在出現當天就被抓到；
這條是同一件事的守門員。verify 從 23 條變 24 條。

**文件**（四處硬編數字已漂掉，實測值回填）

- `docs/schema.md` — `4,308 rows / 3,137 securities` → `4,321 / 3,147 / 263 scrape dates`，
  並新增 `snapshot_date` 是首見日的說明
- `sql/views/21_v_stock_info.sql` — 同上（View 的 SQL 未改，只有註解）
- `src/finmind_pipeline/ingestion/tw_stock_info.py` — 模組 docstring
- `src/finmind_pipeline/processing/prices.py` — raw ticker 檔案數 `3,137` → `3,602`
  （8/30 加入 767 檔已下市證券後就不對了）

`docs/schema.md` 開頭那張「2026-08-26 對 live API 實測」的表格**刻意不動**：它是
有註明量測日期的觀測紀錄，改成今天的數字反而是竄改。

## 對既有查詢的影響

**會變的：**

- `v_stock_info_history` 少掉 3,312 列（43%）。任何對它做 `count(*)`、或想找「某檔
  股票被重分類過幾次」的查詢，之前的答案都被重爬雜訊灌水，**現在才是對的**。
- `WHERE snapshot_date = '...'` 篩 `v_stock_info_history` 的結果會變。修正後篩到的
  是「這天第一次出現的內容」——也就是真正的新上市與重分類。
- `2026-08-27`、`2026-08-31`、`2026-09-02` 這三個日期不再出現在這張表裡。

**不會變的（皆為實測）：**

| View | 列數 |
|---|---|
| `v_stock_info_latest` | 3,147（修正前後相同） |
| `v_securities` | 3,115 |
| `v_indices` | 32 |
| `v_securities_all` | 3,914 |

`v_stock_info_latest` 用 `ROW_NUMBER()` 收成每檔一列，所以下游全部免疫；
`v_daily_prices_enriched` 在 2026-09-02 是 2,822 列，等於 `v_daily_prices` 的 2,822
列，**修正前後都沒有 fan out**。價格層完全不受這次改動影響。

## 已知限制

**1. 分類改回舊值的邊界情況沒有處理。**
某檔股票若從 A 類改成 B 類、之後又改回 A 類，A 那列的 `min()` 停在最早的日期，
`v_stock_info_latest` 的 `ORDER BY snapshot_date DESC` 就會選到 B。正解是把
`snapshot_date` 拆成 `first_seen` / `last_seen` 兩欄、改用 `max(last_seen)` 選現況，
但那是 schema 變更，會動到 `v_stock_info_history` 的欄位與所有 `SELECT *`。
本次選擇不改 schema；真的遇到再升級，raw 層保有全部戳記，隨時能重建。

**2. 只覆蓋 API 有回傳的欄位。**
去重鍵是 `stock_id, stock_name, industry_category, type, is_index`，也就是
`TaiwanStockInfo` 除了 `date` 以外的全部內容。上游若日後新增欄位，鍵要跟著加。

**3. `process_delisting()` 仍使用 `SELECT DISTINCT *`。**
目前是安全的（723 列 / 723 檔），因為那裡的 `date` 是真正的下市日事件，不是戳記。
但這依賴「上游不會修正名稱或日期」；真的修正了就會靜默變成兩列。好消息是
`process_delisted_securities()` 已明確處理「一檔股票多次下市」並取最新事件，會優雅
降級。本次未改。

**4. `daily_update.py` 仍每天擷取 `tw_industry`。**
每日流程沒有任何東西需要它——universe 過濾器只由 `tw_stock_info` 決定。搬到週更可
省 1 call/天。去重修好之後這只是 raw 目錄每天多一個 36 KB 的檔案，無實質傷害，本次
未改。

## 根因（給未來的自己）

四個維度轉換有三個用 `SELECT DISTINCT *` 當去重手段。**`DISTINCT *` 沒有鍵的概念**
——只要任何一欄是時間戳，「不重複的列」就不等於「不重複的事實」。

對照組是價格層：`processing/prices.py` 的 `_union_sql()` 明確宣告
`PARTITION BY stock_id, date` 並定義了 market 勝過 ticker 的優先序，所以它對這類
bug 完全免疫。

新增維度時的規則：**先寫下自然鍵，再寫 `GROUP BY`；不要用 `DISTINCT *`。**

| 轉換 | 去重方式 | 狀態 |
|---|---|---|
| `process_stock_info` | 內容鍵 + `min()` | ✅ 本次 |
| `process_industry` | 自然鍵 + `max()` | ✅ 2026-09-03 (1) |
| `process_delisting` | `DISTINCT *` | 目前安全，`date` 是事件日 |
| `process_expanded_trading_dates` | 以 `date` 為鍵 | ✅ 本來就對 |

## 驗證

```
ruff       src / scripts / tests  All checks passed
pytest     199 passed
verify.py  24 checks · 23 passed · 1 warning · 0 failures
```

新增的 `stock_info has no content duplicates` 為 PASS(0)。唯一的 warning 是
2026-08-26 一次舊的 backfill 失敗紀錄，與本次無關。

## 重現方式

```powershell
.\.venv\Scripts\python.exe -c "from finmind_pipeline.processing import dimensions; dimensions.process_stock_info(); dimensions.process_delisted_securities()"
.\.venv\Scripts\python.exe scripts\build_db.py
.\.venv\Scripts\python.exe scripts\verify.py
```

不需要呼叫 API：raw 層是權威，這次只是重算 processed 層。
`process_delisted_securities()` 要跟著跑，因為它讀主檔判斷「哪些代碼不在 master 裡」。
