# DuckDB Views 使用指南

本文件介紹 `finmind.duckdb` 提供的全部 19 個 View，以及三個日期範圍 Macro。
目標讀者是會使用 Python、Pandas 進行資料分析，但不一定熟悉 SQL 或資料庫的人。

互動式範例另見
[`notebooks/database_usage_guide.ipynb`](../notebooks/database_usage_guide.ipynb)。

## 1. 這個資料庫如何運作

這個專案的資料流是：

```text
FinMind API
    ↓
raw/ Parquet                    原始 API 資料
    ↓ 清洗、轉型、去重、分區
processed/ Parquet              正式資料，也是資料的唯一真相來源
    ↓
finmind.duckdb Views            提供一致的 SQL 查詢介面
    ↓
Python / Pandas / Jupyter / DBeaver
```

`finmind.duckdb` 主要保存 View 定義，不會再複製一份全部行情。查詢 View 時，
DuckDB 才會讀取 `db/finmind/processed/` 下的 Parquet。

View 可以理解成「預先定義好的唯讀 DataFrame」：

| 資料庫概念 | Pandas 類比 |
|---|---|
| View | DataFrame |
| Row | DataFrame 的一列 |
| Column | Series |
| SQL query | 對 DataFrame 選欄、篩列、排序與彙總的操作 |

使用 View 前，最重要的是先確認它的**資料粒度**：每一列代表什麼。
若把不同粒度的資料直接 JOIN，可能意外放大列數。

## 2. 連接資料庫

### Python

分析工作應一律使用唯讀連線，避免阻擋每日或每週更新：

```python
import duckdb

con = duckdb.connect(
    r"<repo>\db\finmind\finmind.duckdb",
    read_only=True,
)

df = con.execute("SELECT * FROM v_last_trading_day").df()
print(df)

con.close()
```

`con.execute(...).df()` 會將 SQL 結果直接轉成 Pandas DataFrame。大型資料應先在
DuckDB 內篩選，再轉成 DataFrame，不要先將全部千萬列行情載入記憶體。

### DBeaver

建立 DuckDB 連線至：

```text
<repo>\db\finmind\finmind.duckdb
```

在 Driver properties 將 `duckdb.read_only` 設為 `true`。

### 最少需要知道的 SQL

```sql
SELECT date, close             -- 選擇欄位
FROM v_daily_prices            -- 指定 View
WHERE stock_id = '2330'        -- 篩選列
ORDER BY date DESC             -- 排序
LIMIT 5;                       -- 最多回傳五列
```

股票代碼必須使用字串。請寫 `'0050'`，不要寫成數字 `50`，否則會失去前導零。

## 3. View 快速選擇表

| 想做的事 | 建議使用 |
|---|---|
| 查未還原 OHLCV | `v_daily_prices` |
| 查還原權息 OHLCV | `v_adj_daily_prices` |
| 比較還原與未還原價格 | `v_prices_combined` |
| 行情同時需要股票名稱與分類 | `v_daily_prices_enriched` |
| 查代碼目前名稱、分類與市場 | `v_stock_info_latest` |
| 取得非指數證券 universe | `v_securities` |
| **回測用的完整 universe(含已下市)** | **`v_securities_all`** |
| 查已下市證券清單 | `v_delisted_securities` |
| 查加權、櫃買或產業指數清單 | `v_indices` |
| 研究基本資料歷史變動 | `v_stock_info_history` |
| 查詳細產業鏈關係 | `v_industry` |
| 安全地將產業鏈 JOIN 到股票資料 | `v_industry_by_stock` |
| 取得交易日曆 | `v_trading_calendar` |
| 判斷行情最新日期 | `v_last_trading_day` |
| 查看每天資料量 | `v_daily_coverage` |
| 查看已確認的特殊休市日 | `v_known_empty_days` |
| 查看應有但缺少的交易日 | `v_missing_trading_days` |
| 查看資料量異常少的交易日 | `v_thin_trading_days` |
| 查看 pipeline 執行紀錄 | `v_ingestion_runs` |

## 4. 價格 Views

### 4.1 `v_daily_prices`

**用途：**未還原權息的台股日線 OHLCV。這是一般量價分析最主要的 View。

**資料粒度：**每列代表一個 `stock_id` 在一個 `date` 的行情。
`(stock_id, date)` 應該唯一。

**主要欄位：**

| 欄位 | 意義 |
|---|---|
| `date` | 交易日期，型別為 `DATE` |
| `stock_id` | 股票、ETF 或指數代碼，型別為字串 |
| `open`, `high`, `low`, `close` | 開盤、最高、最低、收盤價 |
| `spread` | 漲跌價差 |
| `volume` | 成交量 |
| `turnover_value` | 成交金額 |
| `transactions` | 成交筆數 |
| `source` | `ticker` 或 `market`，表示抓取方式 |
| `ingested_at` | processed partition 的寫入時間 |
| `year`, `month` | Parquet 分區欄位 |

查詢 2330 在 2024 年的行情：

```sql
SELECT date, open, high, low, close, volume
FROM v_daily_prices
WHERE stock_id = '2330'
  AND year = 2024
  AND date BETWEEN DATE '2024-01-01' AND DATE '2024-12-31'
ORDER BY date;
```

`year` 不只是方便顯示，也是實際的 Parquet 分區鍵。日期範圍查詢應同時限制
`year`，或使用後面的 `daily_prices_between()` Macro。

這裡的行情只涵蓋 security master universe；上游 API 中約四萬檔權證、TDR
及其他結構型商品已在 ingestion 階段排除。

### 4.2 `v_adj_daily_prices`

**用途：**後向還原權息日線。適合長期價格比較、報酬分析與回測。

**資料粒度：**每列代表一個 `stock_id` 在一個 `date` 的還原行情。

欄位結構與 `v_daily_prices` 相同，但 `open/high/low/close` 是還原後價格：

```sql
SELECT date, open, high, low, close
FROM v_adj_daily_prices
WHERE stock_id = '0050'
  AND year BETWEEN 2020 AND 2024
  AND date BETWEEN DATE '2020-01-01' AND DATE '2024-12-31'
ORDER BY date;
```

注意事項：

- 除權息或拆股可能改寫某檔股票的整段還原歷史。
- 專案因此每週全量刷新還原資料，而不是只追加新日期。
- 可查看 `ingested_at` 判斷還原基準的最後寫入時間。
- FinMind 還原資料的證券涵蓋範圍小於未還原資料：3,149 檔 對 3,612 檔。

**這個 View 仍有存活者偏誤。**已下市證券的 767 檔中，未還原有 466 檔有資料，
還原**只有 7 檔**——`TaiwanStockPriceAdj` 上游就沒有已下市證券的還原價（已逐檔
向 API 驗證）。選股回測若需要無偏誤的 universe，必須改用 `v_daily_prices` 並自
行處理除權息；若使用本 View，請明確接受偏誤並在結論中標註。

### 4.3 `v_prices_combined`

**用途：**在同一列比較未還原與還原價格，並觀察累積調整因子。

**資料粒度：**每列代表一個 `stock_id` 在一個 `date` 的行情。

**主要欄位：**

- `open/high/low/close`：未還原價格。
- `adj_open/adj_high/adj_low/adj_close`：還原後價格。
- `adj_factor`：`adj_close / close`。
- `volume/turnover_value/transactions`：未還原行情的交易量資訊。
- `year/month`：Hive 分區鍵，用來裁剪掃描範圍。

```sql
SELECT date, close, adj_close, adj_factor
FROM v_prices_combined
WHERE stock_id = '0050'
  AND date IN (DATE '2003-07-01', DATE '2015-07-01', DATE '2024-07-01')
ORDER BY date;
```

這個 View 以未還原行情為左表，因此某天沒有對應還原資料時，`adj_*` 會是 NULL
（實測 2026-09-03：全部 11,726,045 列中有 499,202 列）。`adj_factor` 另外在未還原
`close` 為 0 的停牌日也是 NULL（合計 784,674 列），這是刻意避開除以零。

大範圍查詢請用 `prices_combined_between()` Macro。這個 View 同時以
`(stock_id, date, year, month)` JOIN，就是為了讓 `year` 條件能同時裁剪兩邊的
Parquet；只加 `date` 條件會讓還原價那一側完整掃描（實測一季 1.31 秒 vs 0.29 秒）。

### 4.4 `v_daily_prices_enriched`

**用途：**未還原行情已經 JOIN 最新股票基本資料，適合快速探索。

**資料粒度：**每列代表一個 `stock_id` 在一個 `date` 的行情。

除了 `v_daily_prices` 的全部欄位，還包含：

| 欄位 | 意義 |
|---|---|
| `stock_name` | 最新股票名稱 |
| `industry` | 最新正規化產業分類 |
| `type` | `twse`、`tpex` 或 `emerging` 等市場類型 |
| `is_index` | 是否為指數代碼 |
| `is_delisted` | 是否為已下市證券 |
| `delisted_date` | 下市日期，僅部分已下市證券有值 |

它 JOIN 的是 `v_securities_all`(含已下市)，不是 `v_stock_info_latest`。已下市
證券會保留名稱，但 `industry` 與 `type` 為 NULL——上游沒有它們的產業分類。做
產業分組時請先用 `is_delisted` 篩掉，不要讓它們掉進 NULL 群組。

```sql
SELECT date, stock_id, stock_name, industry, close, volume
FROM v_daily_prices_enriched
WHERE industry = '半導體業'
  AND year = 2024
  AND month = 12
ORDER BY turnover_value DESC
LIMIT 20;
```

它使用的是「目前最新」基本資料，不是每個歷史日期當時的分類。方便性高，但在
大型或效能敏感查詢中，優先使用 `v_daily_prices`，必要時再 JOIN 所需欄位。

## 5. 股票基本資料 Views

### 5.1 `v_stock_info_history`

**用途：**保存股票基本資料的累積歷史快照。

**資料粒度：**每列代表某代碼的一筆歷史基本資料狀態。同一 `stock_id` 可能有多列。

**欄位：**

| 欄位 | 意義 |
|---|---|
| `snapshot_date` | **這組內容第一次被看到的日期**；部分指數列為 NULL |
| `stock_id` | 證券或指數代碼 |
| `stock_name` | 名稱 |
| `industry_category` | FinMind 原始分類 |
| `industry_category_norm` | 經 alias 規則正規化的分類 |
| `type` | 市場類型 |
| `is_index` | 是否為指數 |

`snapshot_date` **不是爬取日**。FinMind 每次重爬都會把大多數列的日期改成當天，
所以 processed 層是依內容去重、日期取 `min()`。一列帶著某個日期，代表「這組
名稱／分類／市場別的組合是那天第一次出現」——也就是新上市或真正的重分類，
不是那天被重爬過。

研究分類名稱如何變動：

```sql
SELECT stock_id, stock_name, snapshot_date,
       industry_category, industry_category_norm
FROM v_stock_info_history
WHERE stock_id = '2330'
ORDER BY snapshot_date;
```

同一檔股票有多列的原因有兩種，兩者長得一樣但意義不同：**時間上的變動**
（分類被上游改名，日期不同），以及**同時掛在多個分類下**（如 2330 同時是
`電子工業` 與 `半導體業`，日期相同）。要判斷是哪一種，看日期是否相同。

這不是一檔一列的 current-state View。一般分析需要最新狀態時，使用
`v_stock_info_latest`。

### 5.2 `v_stock_info_latest`

**用途：**每個代碼目前最新的基本資料，是名稱與分類查詢的主要入口。

**資料粒度：**每個 `stock_id` 一列。

```sql
SELECT stock_id, stock_name, industry_category_norm, type, is_index
FROM v_stock_info_latest
WHERE stock_id IN ('0050', '2330', 'TAIEX')
ORDER BY stock_id;
```

用名稱搜尋：

```sql
SELECT stock_id, stock_name, industry_category_norm, type
FROM v_stock_info_latest
WHERE stock_name ILIKE '%台積%';
```

`industry_category_norm` 適合分組分析；`industry_category` 則保留上游原始詞彙。

### 5.3 `v_securities`

**用途：**目前 security master 中的非指數證券 universe。

**資料粒度：**每個非指數 `stock_id` 一列。

```sql
SELECT stock_id, stock_name, industry_category_norm, type
FROM v_securities
WHERE type = 'twse'
  AND industry_category_norm = '半導體業'
ORDER BY stock_id;
```

這個 View 只排除 `is_index = true` 的代碼；若你的分析還需要排除 ETF、興櫃或
其他類型，必須再依研究目的加條件，不要假設它等於「所有普通股」。

### 5.4 `v_indices`

**用途：**列出 security master 中的指數型代碼，例如加權、櫃買與產業指數。

**資料粒度：**每個指數 `stock_id` 一列。

```sql
SELECT stock_id, stock_name, industry_category_norm
FROM v_indices
ORDER BY stock_id;
```

這些指數代碼本身也有 OHLCV，可用 `stock_id` JOIN 或篩選 `v_daily_prices`：

```sql
SELECT p.date, p.stock_id, i.stock_name, p.close, p.spread
FROM v_daily_prices p
JOIN v_indices i USING (stock_id)
WHERE p.date = (SELECT date FROM v_last_trading_day)
ORDER BY p.spread DESC;
```

### 5.5 `v_delisted_securities`

**用途：**列出「曾經交易過、但已不在 security master」的證券。

**為什麼需要它：**`TaiwanStockInfo` 是當下狀態清單，本專案第一份快照是
2026-08-26，在那之前就下市的公司從未被記錄，逐檔回補也就從未去抓它們。少了這
些標的，選股回測會有存活者偏誤——2010-09-15 應有 1,585 檔（不含指數），其中 145
檔（9.1%）是缺的。

這些代碼是用「每月抽一個交易日做全市場呼叫」的方式找回來的，再與
`TaiwanStockDelisting` 聯集。

**資料粒度：**每個已下市 `stock_id` 一列，共 767 檔。

**欄位：**

| 欄位 | 意義 |
|---|---|
| `stock_id` | 證券代碼 |
| `stock_name` | 名稱；**僅 471 檔有值**，其餘為 NULL |
| `delisted_date` | 下市日期;同樣僅部分有值 |
| `shape` | `common` 或 `etf` |
| `first_seen` | **抽樣首次觀測到的日期，不是上市日** |
| `last_seen` | **抽樣最後觀測到的日期，不是下市日** |
| `source` | `discovered`(僅抽樣找到)、`delisting`(僅下市清單有)、`both` |

```sql
SELECT stock_id, stock_name, delisted_date, first_seen, last_seen
FROM v_delisted_securities
WHERE stock_name IS NOT NULL
ORDER BY delisted_date DESC
LIMIT 20;
```

`first_seen` / `last_seen` 是每月一次的抽樣結果，真實上市與下市日可能落在該區
間外最多一個月。需要精確日期時用 `delisted_date`，並接受它只有部分證券有值。

這些證券**沒有產業分類**，`v_industry` 與 `v_industry_by_stock` 查不到它們。

**行情涵蓋率：**767 檔中 466 檔有未還原行情；其餘 301 檔在 2004-02-11（FinMind
日線資料的起點）之前就已下市，上游沒有任何資料。**還原行情只有 7 檔**，詳見
4.2 節的警告。

### 5.6 `v_securities_all`

**用途：**master 加上已下市證券，是**回測應該使用的完整 universe**。

**資料粒度：**每個 `stock_id` 一列。

**欄位：**`v_stock_info_latest` 的全部欄位，外加 `is_delisted` 與 `delisted_date`。
已下市列的 `industry_category`、`industry_category_norm`、`type` 為 NULL。

把行情 JOIN 到 `v_stock_info_latest` 會**靜默地丟掉每一檔已下市證券**，這正是
存活者偏誤進入回測的途徑。JOIN 到這個 View 則不會:

```sql
-- 2010 年那一天實際存在的股票，含後來下市的
SELECT p.stock_id, s.stock_name, s.is_delisted, p.close, p.turnover_value
FROM v_daily_prices p
JOIN v_securities_all s USING (stock_id)
WHERE p.date = DATE '2010-09-15'
ORDER BY p.turnover_value DESC
LIMIT 20;
```

判斷某檔在某日是否仍在市:

```sql
SELECT count(*) AS alive
FROM v_securities_all
WHERE stock_id = '3452'
  AND (delisted_date IS NULL OR delisted_date > DATE '2015-01-01');
```

## 6. 產業鏈 Views

### 6.1 `v_industry`

**用途：**FinMind 產業鏈的詳細多對多關係。

**資料粒度：**每列代表 `(stock_id, industry, sub_industry)` 的一組關係。
一檔股票可能有 1 到 61 列。

**欄位：**`stock_id`、`industry`、`sub_industry`、`snapshot_date`。

`snapshot_date` 是「這組關係最後一次在上游被看到的日期」，**不是生效日**。
FinMind 每次重新爬取都會重蓋時間戳，內容不變，所以 processed 層已依自然鍵
去重並取 `max()`。這裡**沒有產業分類的歷史**：拿 `snapshot_date` 筛只會讓結果
變少，不會回到某一天的產業分類狀態。

```sql
SELECT stock_id, industry, sub_industry, snapshot_date
FROM v_industry
WHERE stock_id = '2330'
ORDER BY industry, sub_industry;
```

不要直接把它以 `stock_id` JOIN 每日行情，否則一筆行情會按產業關係數被複製：

```sql
-- 不建議：可能放大行情列數
SELECT *
FROM v_daily_prices p
JOIN v_industry i USING (stock_id);
```

若需要詳細關係，先彙總到每檔一列，或使用 `v_industry_by_stock`。

### 6.2 `v_industry_by_stock`

**用途：**將一檔股票的所有產業與次產業收成 list，方便安全地 1:1 JOIN。

**資料粒度：**每個 `stock_id` 一列。

**欄位：**

- `industries`：產業名稱 list。
- `sub_industries`：次產業名稱 list。
- `last_seen`：該股票產業資料的最新快照日期。

```sql
SELECT stock_id, industries, sub_industries, last_seen
FROM v_industry_by_stock
WHERE stock_id = '2330';
```

安全地加入最新行情：

```sql
SELECT p.stock_id, s.stock_name, p.close,
       i.industries, i.sub_industries
FROM v_daily_prices p
LEFT JOIN v_stock_info_latest s USING (stock_id)
LEFT JOIN v_industry_by_stock i USING (stock_id)
WHERE p.date = (SELECT date FROM v_last_trading_day)
ORDER BY p.turnover_value DESC
LIMIT 20;
```

## 7. 交易日曆與涵蓋率 Views

### 7.1 `v_trading_calendar`

**用途：**台股交易日曆。

**資料粒度：**每個交易日期一列。

**欄位：**

| 欄位 | 意義 |
|---|---|
| `date` | 交易日期 |
| `source` | `finmind` 或 `derived` |
| `year`, `month` | 年、月 |
| `day_of_week` | DuckDB 星期編號 |
| `is_weekend` | 是否落在星期六或星期日 |

```sql
SELECT *
FROM v_trading_calendar
WHERE date BETWEEN DATE '2024-01-01' AND DATE '2024-01-31'
ORDER BY date;
```

FinMind 官方日曆從 1999 年開始；1994–1998 的日期由實際行情推導，因此
`source = 'derived'`。早期台股曾在星期六交易，不可用現代週一至週五規則重建。

此 View 也可能包含已公告的未來日期。判斷「資料更新到哪天」必須使用
`v_last_trading_day`，不能使用這裡的 `max(date)`。

### 7.2 `v_last_trading_day`

**用途：**回傳未還原行情實際擁有資料的最新日期。

**資料粒度：**固定一列、一欄。

```sql
SELECT date AS latest_data_date
FROM v_last_trading_day;
```

取得最新一日市場行情：

```sql
SELECT *
FROM v_daily_prices
WHERE date = (SELECT date FROM v_last_trading_day)
ORDER BY turnover_value DESC;
```

### 7.3 `v_daily_coverage`

**用途：**彙總每天的資料涵蓋情況。

**資料粒度：**每個已有行情的日期一列。

**欄位：**

| 欄位 | 意義 |
|---|---|
| `date` | 交易日期 |
| `row_count` | 該日行情列數 |
| `securities` | 該日不同代碼數 |
| `last_ingested` | 該日資料最後寫入時間 |

```sql
SELECT date, row_count, securities, last_ingested
FROM v_daily_coverage
ORDER BY date DESC
LIMIT 20;
```

可用它觀察市場 universe 隨時間變化，也可快速發現某日資料量突然下跌。

### 7.4 `v_known_empty_days`

**用途：**保存官方日曆列為交易日，但經 API 確認沒有任何交易資料的特殊休市日。

**資料粒度：**每個已確認特殊休市日一列。

**欄位：**`date`、`confirmed_at`。

```sql
SELECT *
FROM v_known_empty_days
ORDER BY date;
```

這些日期會從缺口報告排除，避免每日或每週工作永遠重抓同一個不存在的交易日。

### 7.5 `v_missing_trading_days`

**用途：**列出行情觀測範圍內，交易日曆認為應存在、但未還原行情完全缺少的日期。

**資料粒度：**每個缺失日期一列。

```sql
SELECT *
FROM v_missing_trading_days
ORDER BY date;
```

理想狀態是零列。每日更新會偵測歷史缺口；每週 reconciliation 也會檢查並修復。
這個 View 不會把最新資料之後尚未發布的日期算成缺口，也會排除
`v_known_empty_days`。

### 7.6 `v_thin_trading_days`

**用途：**找出行情列數明顯低於附近交易日的可疑日期，常代表部分寫入或不完整回應。

**資料粒度：**每個可疑日期一列。

**欄位：**

| 欄位 | 意義 |
|---|---|
| `date` | 可疑日期 |
| `row_count` | 當日實際列數 |
| `neighbour_median` | 前後鄰近日的列數中位數 |
| `ratio` | `row_count / neighbour_median` |

```sql
SELECT *
FROM v_thin_trading_days
ORDER BY date;
```

目前判定條件是當日列數低於鄰近日中位數的 50%。零列代表沒有偵測到異常稀薄日。

## 8. Pipeline 執行紀錄 View

### 8.1 `v_ingestion_runs`

**用途：**用 SQL 查詢每日更新、每週刷新、backfill 與其他 pipeline 工作的執行紀錄。

**資料粒度：**每次工作執行一列。

**欄位：**

| 欄位 | 意義 |
|---|---|
| `job` | 工作名稱 |
| `status` | `success` 或 `failed` |
| `started_at`, `finished_at` | 開始與完成時間 |
| `duration_seconds` | 執行秒數 |
| `host` | 執行主機 |
| `metrics` | 工作寫入的結構化指標 |
| `errors` | 錯誤 list |
| `error_count` | 錯誤數量 |

```sql
SELECT job, status, started_at, duration_seconds, error_count
FROM v_ingestion_runs
ORDER BY started_at DESC
LIMIT 20;
```

只看失敗工作：

```sql
SELECT job, started_at, duration_seconds, errors
FROM v_ingestion_runs
WHERE status = 'failed'
ORDER BY started_at DESC;
```

此 View 直接讀取 `db/finmind/logs/runs/*.jsonl`。詳細文字日誌則位於
`db/finmind/logs/ingestion/` 和 `db/finmind/logs/processing/`。

## 9. 日期範圍 Macros

Macro 不是 View，也不是另一份資料。它是可以帶參數的預先定義查詢。

### 9.1 `daily_prices_between(lo, hi)`

**用途：**有效率地查詢日期區間內的未還原行情。

```sql
SELECT date, stock_id, close, volume
FROM daily_prices_between('2024-01-01', '2024-03-31')
WHERE stock_id IN ('0050', '2330')
ORDER BY stock_id, date;
```

### 9.2 `adj_daily_prices_between(lo, hi)`

**用途：**有效率地查詢日期區間內的還原行情。

```sql
SELECT date, stock_id, close
FROM adj_daily_prices_between('2020-01-01', '2024-12-31')
WHERE stock_id = '0050'
ORDER BY date;
```

### 9.3 `prices_combined_between(lo, hi)`

**用途：**有效率地查詢日期區間內的還原/未還原並排比較。

```sql
SELECT date, stock_id, close, adj_close, adj_factor
FROM prices_combined_between('2024-01-01', '2024-03-31')
WHERE stock_id = '0050'
ORDER BY date;
```

為什麼推薦 Macro：價格 Parquet 以 `year/month` 分區。單純寫：

```sql
WHERE date BETWEEN DATE '2024-01-01' AND DATE '2024-03-31'
```

DuckDB 不一定能從 `date` 自動推導要讀哪些 `year` 目錄。Macro 會同時加入單純的
`year BETWEEN ...` 比較，實測可將三個月查詢從約 0.56 秒降至約 0.13 秒；
`v_prices_combined` 要掃兩份資料集，差距是 1.31 秒對 0.29 秒。

## 10. 常見組合範例

### 最新交易日成交金額前 20 名

```sql
SELECT p.stock_id, s.stock_name, s.industry_category_norm,
       p.close, p.volume, p.turnover_value
FROM v_daily_prices p
LEFT JOIN v_stock_info_latest s USING (stock_id)
WHERE p.date = (SELECT date FROM v_last_trading_day)
ORDER BY p.turnover_value DESC
LIMIT 20;
```

### 計算單一股票日報酬

```sql
SELECT date, close,
       close / lag(close) OVER (ORDER BY date) - 1 AS daily_return
FROM daily_prices_between('2024-01-01', '2024-12-31')
WHERE stock_id = '2330'
ORDER BY date;
```

### 每月最後收盤價

```sql
SELECT year, month, last(close ORDER BY date) AS month_end_close
FROM v_daily_prices
WHERE stock_id = '2330'
  AND year BETWEEN 2020 AND 2024
GROUP BY year, month
ORDER BY year, month;
```

### 由 Python 取得 DataFrame

```python
import datetime as dt
import duckdb

con = duckdb.connect(
    r"<repo>\db\finmind\finmind.duckdb",
    read_only=True,
)

stock_id = "2330"
start = dt.date(2024, 1, 1)
end = dt.date(2024, 12, 31)

prices = con.execute(
    """
    SELECT date, stock_id, open, high, low, close, volume
    FROM v_daily_prices
    WHERE stock_id = ?
      AND year BETWEEN ? AND ?
      AND date BETWEEN ? AND ?
    ORDER BY date
    """,
    [stock_id, start.year, end.year, start, end],
).df()

prices["daily_return"] = prices["close"].pct_change()
prices["ma20"] = prices["close"].rolling(20).mean()

con.close()
```

## 11. 使用原則與常見陷阱

1. 分析工具一律使用 `read_only=True`，避免阻擋排程更新。
2. 股票代碼一律當字串，保留前導零與英文字尾。
3. 先在 DuckDB 篩選日期與股票，再轉成 Pandas DataFrame。
4. 日期範圍查詢使用 Macro，或同時限制 `year` 與 `date`。
5. 長期報酬通常使用 `v_adj_daily_prices`；當時實際報價使用 `v_daily_prices`。
6. 最新行情日期使用 `v_last_trading_day`，不要使用交易日曆的最大日期。
7. `v_industry` 是多對多資料；一般 JOIN 使用 `v_industry_by_stock`。
8. `v_daily_prices_enriched` 使用目前最新分類，不代表歷史日期當時的分類。
9. 不要手動修改 Raw 或 Processed Parquet；資料應由 pipeline 腳本更新。
10. 查詢到 NULL 不一定是錯誤，例如某日沒有對應還原行情，或指數的 `snapshot_date` 為 NULL。
11. 選股回測的 universe 使用 `v_securities_all`，不要使用 `v_stock_info_latest`——
    後者不含已下市證券，會造成存活者偏誤。無偏誤的橫斷面最早只到 2004-02-11。

## 12. 查看資料健康狀態

```sql
SELECT
    (SELECT date FROM v_last_trading_day) AS latest_day,
    (SELECT count(*) FROM v_missing_trading_days) AS missing_days,
    (SELECT count(*) FROM v_thin_trading_days) AS thin_days,
    (SELECT count(*) FROM v_known_empty_days) AS known_empty_days;
```

也可以在專案目錄執行完整唯讀檢查：

```powershell
.\.venv\Scripts\python.exe scripts\verify.py
```

若只想確認全部 View 能否查詢並查看列數：

```powershell
.\.venv\Scripts\python.exe scripts\build_db.py --verify-only
```

資料更新與排程操作請參考 [`operations.md`](operations.md)，欄位型別與資料品質規則
請參考 [`schema.md`](schema.md)。
