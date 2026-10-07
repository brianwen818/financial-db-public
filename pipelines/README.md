# financial-db — data pipelines

個人財經資料庫的資料管線。兩個獨立的台股資料庫，同一套分層契約：

```
raw          來源給什麼就存什麼，不改一個字
processed    定型、去重、分區——這才是查詢契約
DuckDB       純 view 層，零 table，可拋棄重建
Task Sched   每日自動執行
```

程式碼在此 repo；**資料與 DuckDB 檔案在 `../db/`，不進 repo**（公開版未附資料，見[根目錄 README](../README.md)）。

## 視覺化總覽

| 頁面 | 內容 | 本地 Markdown 版 |
|---|---|---|
| [financial-db 資料地圖](https://brianwen818.github.io/financial-db-public/docs/data-map.html) | 整體資料流程圖、兩庫對照、處理步驟、更新排程時間軸、從零建置指令、查詢陷阱 | [`data-map.md`](../docs/data-map.md) |
| [financial-db Schema 圖](https://brianwen818.github.io/financial-db-public/docs/schema-map.html) | raw／processed／view 三層血緣圖，每張表的完整欄位與型別（含 TEJ xlsx 35 欄中文→英文對照），滑鼠移到表上會亮起上下游 | [`schema-map.md`](../docs/schema-map.md) |

> 兩份地圖的數字為 **2026-09-29** 實測（比本 README 的 09-03 快照新）。

> 以下所有數字實測於 **2026-09-03**（兩邊當日排程都跑完後）。數字會隨每日更新
> 變動，需要精確值請自己查，不要引用這裡的快照值。

---

## 先選對資料庫

| | **finmind** | **tej-wizard** |
|---|---|---|
| 來源 | FinMind API v4 | TEJ Smart Wizard 的 Excel 增益集 |
| 涵蓋 | 全市場 3,914 檔 | TWN50＋TM100 歷年成分股 365 檔 ＋ 0050／0051 兩支 ETF |
| 期間 | 1994-10-01 起，8,060 個交易日 | 2000-09-04 起，6,411 個交易日 |
| 列數 | 11,726,045（未還原）＋ 11,226,843（還原） | 1,887,747（還原）＋ 832,648（指數成分） |
| 還原價存活者偏誤 | **有，且修不了**（767 檔已下市只有 7 檔有還原價） | **無**（54 檔已停止交易皆有完整歷史） |
| 每列欄位 | 精簡（OHLCV + 成交值/筆數） | 豐富（40 欄：估值、權重、漲跌停、注意/處置旗標） |
| 未還原價 | 另一張表 `v_daily_prices` | 同一列的 `bid`／`next_ref_price` 等 |
| raw 可重製 | 可以，重抓就好 | **不行**，xlsx 是唯一真相，更新前先備份 |

**選 finmind**：需要全市場橫斷面、選股池、產業分類，或 2000 年以前的歷史。
**選 tej-wizard**：做 0050／台灣中型 100 成分股、需要無偏誤還原價、需要估值與
權重欄位，或需要**每日 point-in-time 指數成分**。

**兩者有一條相依**：tej-wizard 的每日更新會讀 finmind 的
`processed/expanded-tw-trading-dates/`，用來決定要往 xlsx 插入哪些缺漏交易日。
finmind 壞了，tej 也更新不了。

| 路徑 | 用途 |
|---|---|
| `finmind/` | FinMind（台股全市場）管線 |
| `tej-wizard/` | TEJ Smart Wizard（0050 成分股還原日行情）管線 |
| `other-sources/` | 未來其他資料來源 |

---

# Part 1 · finmind

**FinMind API v4 → Parquet → DuckDB view 層。**

資料在 `../db/finmind/`：

```
raw/            653 MB    API 回什麼就存什麼
processed/      1.1 GB    定型、去重、按日分區
finmind.duckdb  268 KB    19 個 view + 3 個 macro，零 table
```

`.duckdb` 只有 268 KB，因為它**一張表都沒有**，只是蓋在 Parquet 上的 view。
刪掉重跑 `scripts/build_db.py` 就回來。

## 一、抓進來什麼

**6 個 FinMind endpoint**，加 1 個自己做的掃描：

| 來源 | endpoint | 落地形狀 |
|---|---|---|
| 未還原行情 | `TaiwanStockPrice` | 3,602 個 ticker 檔 + 全市場日檔 |
| 還原行情 | `TaiwanStockPriceAdj` | 3,143 個 ticker 檔 + 全市場日檔 |
| 證券主檔 | `TaiwanStockInfo` | 每次抓一個快照檔 |
| 產業鏈 | `TaiwanStockIndustryChain` | 同上 |
| 官方交易日曆 | `TaiwanStockTradingDate` | 同上 |
| 下市公告 | `TaiwanStockDelisting` | 同上 |
| **已下市證券掃描** | 重用 `TaiwanStockPrice` | 2004 年起每月抽一天全市場，記下看過的所有代碼 |

**價格為什麼有兩種形狀**：回補時「一次呼叫拿一檔的全部歷史」最省配額
（3,602 次搞定 32 年）；每日更新時「一次呼叫拿全市場一天」最省（1 次搞定當天）。
兩種都原封不動落地——`raw/` 的欄位名還是上游的 `max`、`min`、`Trading_Volume`。

**最後那個掃描是為了修存活者偏誤。** `TaiwanStockInfo` 只列現在還在市的證券，
不掃就永遠不知道那 767 檔已下市證券存在過。

**沒有抓的**：財報、三大法人、融資券、股利明細、分價量表、盤中/分鐘資料。

## 二、怎麼處理

### 價格（最重的一段）

1. **改名 + 定型** — `max`→`high`、`min`→`low`、`Trading_Volume`→`volume`、
   `Trading_money`→`turnover_value`、`Trading_turnover`→`transactions`。
   日期字串轉真的 `DATE`，金額轉 `BIGINT`（`Trading_money` 會到 5.3×10¹⁰）。
   **`stock_id` 刻意留 `VARCHAR`**——`0050`、`00679B`、`TAIEX` 轉數字就壞了。
2. **過濾範圍** — 全市場單日回 43,727 檔，只留 **2,821 檔**。丟掉的是權證與 TDR
   （抽樣累積看到 108,406 個權證代碼、27,042 個 TDR 代碼）。
3. **重新分區** — 從「一檔一個檔案」翻轉成「一天一個檔案」，放進
   `year=YYYY/month=MM/`。這是整個查詢效能的基礎。
4. **去重** — 同一個 `(stock_id, date)` 可能同時來自 ticker 檔和全市場檔，
   用 `ROW_NUMBER()` 留一筆。未還原價**全市場優先**（它是收盤後的權威快照，
   ticker 檔可能是盤中抓的）；還原價**ticker 優先**（每次除權息都會改寫整段
   歷史，全市場日檔只是抓取當下那個基準的快照，讓它優先會把舊基準混進新基準）。

### 維度

每張表各有自己的**自然鍵**，不是 `SELECT DISTINCT *`：

| 表 | 去重鍵 | 日期語意 |
|---|---|---|
| `tw_stock_info` | 全部內容欄位 | `min()` = 首見日 |
| `tw_industry` | `(stock_id, industry, sub_industry)` | `max()` = 最後看到 |

**為什麼**：上游每次重爬都會把日期欄重新戳記，沒有鍵的概念就會讓同一筆事實
每爬一次多一列——實測第二次抓就把 `tw_industry` 6,871 列灌成 11,520 列。

### 三個是算出來的，不是抓來的

- **交易日曆的 1994–1998 段** — 官方只到 1999-01-05，早期靠觀測到的價格日期
  反推，**含週六**（當年有週六盤）。任何「週末必無盤」的假設都是 bug。
- **已下市清單** — 掃描 ∪ 下市公告 − 現有主檔，再依代碼形狀過濾。
- **確認休市日** — 日曆說有、實際沒交易的日子（目前 1 天：2026-07-10）。

### 順序不能亂換

```
維度 ingest → 價格 ingest → 價格 processing → 日曆 → 已下市清單 → build view
```

維度要先於價格（過濾範圍要用最新名單）；價格要先於日曆（早期日曆從價格反推）；
主檔要先於已下市清單（後者要減掉前者）。`process_all_dimensions()` 把順序寫死。

## 三、最後有什麼

```
daily-prices          8,060 檔  382 MB   11,726,045 列 / 3,612 檔 / 1994-10-01→2026-09-03
adj-daily-prices      8,060 檔  648 MB   11,226,843 列 / 3,152 檔 / 1994-10-01→2026-09-03
tw_stock_info                     4,321 列      tw_industry             6,871 列
delisted_securities                 767 列      expanded_trading_dates  8,141 列
tw_delisting                        723 列      known_empty_dates           1 列
```

**19 個 view**：

| 分組 | View |
|---|---|
| **價格** | `v_daily_prices`、`v_adj_daily_prices`、`v_prices_combined`、`v_daily_prices_enriched` |
| **證券主檔** | `v_securities_all`、`v_securities`、`v_indices`、`v_stock_info_latest`、`v_stock_info_history`、`v_delisted_securities` |
| **產業/日曆** | `v_industry`、`v_industry_by_stock`、`v_trading_calendar`、`v_last_trading_day` |
| **維運** | `v_daily_coverage`、`v_missing_trading_days`、`v_thin_trading_days`、`v_known_empty_days`、`v_ingestion_runs` |

**3 個 macro**：`daily_prices_between(lo, hi)`、`adj_daily_prices_between(lo, hi)`、
`prices_combined_between(lo, hi)`。

**`tw_delisting` 這 723 列沒有對外的 view**——它只是 `delisted_securities` 的原料。
要查下市日期用 `v_delisted_securities` 或 `v_securities_all`。

## 涵蓋範圍與已知缺口

```
v_securities_all   3,914  =  在市 3,147  +  已下市 767
v_daily_prices     3,612  =  在市 3,146  +  已下市 466
v_adj_daily_prices 3,152  =  在市 3,145  +  已下市   7
```

在市 3,147 檔的組成（依代碼形狀）：普通股 2,536、ETF 422、權證形狀 101、
特別股 39、TDR 形狀 16、指數 32。ETN（`020009`）、受益證券（`01009T`）、
存託憑證（`910069`）都有抓。

**未還原價含已下市證券，但只有 61%（466/767）。** 缺的 301 檔全部在 2004-02-11
之前下市，那是 FinMind 全市場端點的資料起點，更早的日期呼叫只回空。因此**無偏誤
的橫斷面最早只到 2004-02-11**；此後 universe 是完整的。

**還原價幾乎沒有已下市證券（7/767），且修不了。** 已逐檔向 API 驗證
（`4103`、`5480`、`1787`、`4947`），`TaiwanStockPriceAdj` 對已下市代碼一律回空，
是上游缺口而非漏抓。`v_adj_daily_prices` 仍是 survivors-only。

**在市的 3,147 檔中只有 1 檔完全無行情**：`1230`（FinMind master 停在 2024-12-03，
無任何價格）。按市場別（含 32 檔指數）`twse` 1,623/1,624、`tpex` 1,136/1,136、
`emerging` 387/387。

**這不等於「FinMind 上所有代碼」。** 保留權證與 TDR 估計會是 ~97M 列 / 2–3 GB。
要納入需移除 `config/datasets.yml` 的 `filter_to_universe`，並用約 8,100 次
全市場呼叫重補歷史。

## 陷阱（動手前必讀）

- **`stock_id` 是字串。** `'0050'` 不是 `50`。
- **日期範圍用 macro。** Parquet 依 `year/month` 分區，單寫 `WHERE date BETWEEN`
  不會裁剪分區（實測一季 0.56s vs 0.13s）。
- **回測 universe 用 `v_securities_all`，不是 `v_stock_info_latest`。**
  後者是當下狀態，不含已下市證券，會造成存活者偏誤。
- **最新一天可能不完整。** 用 `v_last_trading_day` 取最新日期，但先用
  `v_daily_coverage` 確認那天的 `row_count` 沒有異常偏低。
- **`v_daily_prices_enriched` 的產業是「現在」的分類**，不是歷史當時的。

| 需求 | 用什麼 | 代價 |
|---|---|---|
| 無存活者偏誤的 universe | `v_daily_prices` + `v_securities_all`，起點 2004-02-11 | 需自行處理除權息 |
| 現成還原價 | `v_adj_daily_prices` | survivors-only，**結論必須標註** |

## 指令

用專案 venv，不要用系統 python 或 conda。

```powershell
cd finmind
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
copy .env.example .env      # 填入 FINMIND_TOKEN

.\.venv\Scripts\python.exe scripts\backfill.py --tickers 0050,2330  # 試水溫，順便建起日曆
.\.venv\Scripts\python.exe scripts\discover_delisted.py             # 找回已下市證券
.\.venv\Scripts\python.exe scripts\backfill.py                      # 全量，~7,830 次呼叫 / ~85–95 分
.\.venv\Scripts\python.exe scripts\build_db.py
.\.venv\Scripts\python.exe scripts\verify.py
```

**順序不能換。** `discover_delisted.py` 會先讀交易日曆，空倉庫直接跑會
`FileNotFoundError`，所以要先有任何一次維度 ingest（第一行就夠）。
`backfill.py` 不帶參數等於 `--universe all`（主檔 + 已下市），所以第三行同時
補齊在市與已下市。`backfill.py` **不會自己重建 view**，最後兩行要自己跑。

```powershell
.\.venv\Scripts\python.exe scripts\daily_update.py     # 每日增量
.\.venv\Scripts\python.exe scripts\weekly_refresh.py   # 每週 adj 全量 + 對帳
.\.venv\Scripts\python.exe scripts\verify.py           # 唯讀健康檢查，28 項
.\.venv\Scripts\python.exe scripts\build_db.py         # 重建 view，不呼叫 API
.\.venv\Scripts\python.exe -m pytest tests -q          # 210 項
```

| 工作 | 時機 | 呼叫數 | 時間 |
|---|---|---|---|
| `daily_update.py` | Mon–Sat 18:30 | 3 維度 + 2 × 待補天數（實測 22） | 秒級 |
| `weekly_refresh.py` | Sun 02:00 | ~3,141 | ~40 分 |
| `discover_delisted.py` | **不排程**，一年一次稽核 | 272 | ~4 分 |
| `backfill.py` | 一次性 | ~7,830 | ~85–95 分 |

**每日**是三個集合的聯集：新日期 ∪ 最近 3 天重抓（FinMind 結算後會改當日成交量）
∪ 範圍內缺漏。超過 30 天會斷路，叫你去跑 backfill。
**每週**把還原價**全量重抓**——每次除權息都會改寫整條歷史，不能 append——
再跑對帳 A/B/C/D（缺日、稀薄日、零列 ticker、停更 ticker）。

---

# Part 2 · tej-wizard

**TEJ 增益集 → xlsx → Parquet → DuckDB。** 來源不是 API，而是 Excel 增益集：
更新靠 COM 驅動一個隱藏的 Excel 跑巨集。

**兩個 dataset**，同一套機制、不同契約：

| | `adj_daily_prices` | `index_constituents` |
|---|---|---|
| TEJ 表 | `waprcd1` 調整後日行情 | `widxs` 指數成分股 |
| 一個檔 | 一檔證券的全歷史 | 一個指數的一個年度 |
| 一列 | 證券 × 交易日 | 指數 × 交易日 × 成分股 |
| 鍵 | `(stock_id, date)` | `(index_id, date, stock_id)` |
| 主 view | `v_adj_daily_prices` | `v_index_constituents` |

資料在 `../db/tej-wizard/`：

```
Excel COM    先補缺漏交易日，再由 TEJ Smart Wizard 增益集更新 xlsx
             （跑在獨立的 Windows desktop 上，不會有視窗跳出來）
xlsx         raw 層，也是唯一真相（不可重製，更新前先備份）
Feather      0050 ETF review snapshot 權重與 ISIN（不再是 membership 來源）
Parquet      processed 層，兩個 dataset 各自依 year/month 分區
DuckDB       20 個 view + 3 個 macro
```

## 一、抓進來什麼

**這不是市場，是兩個指數的成分名單。**

`index_constituents` 是 48 份 index-year workbook：TWN50 從 2002-07-05、TM100 從
2004-11-30 起的每日官方成分，832,648 列、聯集 365 檔證券。**它就是 point-in-time
membership 的權威來源**，取代了原本要另外用 Pandas 讀的 Feather。

`adj_daily_prices` 則是這 365 檔各自一份 workbook 的還原日行情。原本只有 0050 的
120 檔，其餘是用 `create_workbooks.py` 從既有 workbook 的樣板重建出來的——TEJ 的
查詢定義不含證券代碼，代碼在每一列的 A 欄，所以改寫 A 欄再 refresh 就會得到新證券
的完整歷史（對照手動下載的檔實測 230,796 格全部相同）。

**退出者有保留。** 已停止交易的證券（日月光、矽品 2018 合併，華映 2019 下市，
晶電 2021 併入富采，新光金 2025-07 等）都保有完整還原歷史。

Feather（284,362 列）現在只剩兩個用途：ISIN，以及 0050 **ETF 自身**的 review
snapshot 權重——那和**指數**成分不是同一回事。本 pipeline 不產生也不更新它。

## 二、怎麼處理

**xlsx sheet 是固定的 35 欄（A..AI），367 份表頭完全一致。** 讀進來後：

1. **解析 + 定型** — `xlsx_reader.py` 依 `schema.py` 的契約讀取，
   欄名中文轉英文（`收盤價`→`close`、`次日參考價`→`next_ref_price` 等）。
2. **加上衍生與來源欄** — 補到 38 欄（含 `market`、`source_file`、`ingested_at`）。
3. **依 `year`/`month` 分區**寫成 Parquet，view 再暴露 `year`/`month`
   兩個分區鍵，`v_adj_daily_prices` 合計 **40 欄**。

**更新機制比 finmind 麻煩得多。** 每日 refresh 會先讀 finmind 的
`expanded_tw_trading_dates.parquet`，把活躍 workbook 從最新資料日至今天缺少的
交易日插入第 2 列起，再以同步的 `RefreshFile_Wait` 等 TEJ 填完後才存檔。
**TEJ 若沒填出新增日的收盤價，該檔不會存檔，整次 update 也會停止**——
寧可停下來，也不要寫進半筆資料。

## 三、最後有什麼

```
adj_daily_prices    1,887,747 列
securities                367  =  指數成分聯集 365  +  0050／0051 ETF
                               =  在市 313  +  已停止交易 54
trading days            6,411     2000-09-04 → 2026-09-04
每日列數中位數            305

index_constituents    832,648 列（TWN50 297,265／TM100 535,383）
indices                     2     TWN50 2002-07-05 起、TM100 2004-11-30 起
                                  皆至 2026-09-07（領先價格一個交易日）
每日成分數                        TWN50 46–51、TM100 98–102
```

實測大小：raw 366.7 MB ＋ 50.9 MB、processed 156.3 MB ＋ 8.7 MB、
`tej_wizard.duckdb` 268 KB（純 view，零 table）。

**20 個 view**：

| View | 用途 |
|---|---|
| `v_adj_daily_prices` | 價格主表，38 欄 + `year`/`month` 分區鍵 = 40 欄 |
| `v_securities` | 一檔一列：名稱、市場別、起訖日、交易日數、`is_current` |
| `v_trading_calendar` | 觀測到的交易日聯集（6,411 天） |
| `v_last_trading_day` | 最新一天 |
| `v_daily_coverage` | 每日列數與證券數 |
| `v_thin_trading_days` | 列數異常偏低的日子（目前 0） |
| `v_security_gaps` | 個股在自己存續期間內缺漏的交易日（目前 1,714 列／6 檔，全是停牌） |
| `v_workbooks`／`v_workbook_status` | raw xlsx 的檔案層狀態與對帳 |
| `v_index_constituents` | 成分股主表，14 欄 + 分區鍵 = 16 欄 |
| `v_index_membership` | 每（指數, 證券）的進出日期與在榜天數 |
| `v_index_universe` | 曾入選的 365 檔，含**本庫是否有還原價** |
| `v_index_calendar`／`v_index_last_trading_day` | 每指數的發布日 |
| `v_index_daily_coverage`／`v_thin_index_days` | 每日成分數與權重合計（異常日目前 0） |
| `v_index_price_gaps` | 成分股當日無法定價的列（目前 39 列／6 檔，全是併購停牌） |
| `v_index_workbooks`／`v_index_workbook_status` | 48 份 index-year 檔的狀態與對帳 |
| `v_ingestion_runs` | 每次執行的紀錄 |

Macro：`adj_daily_prices_between(lo, hi)`、`index_constituents_between(lo, hi)`、
`index_members_on(index_id, as_of)`。

`v_workbook_status` 是這條管線特有的獨立對帳層。refresh 本身會驗證新增日期是否
有非空 `close`，失敗時不存檔並停止 update；這個 view 則在重建後**再次**比對
workbook 列數、最新日期與查詢契約，用來發現仍然落後或內容不一致的檔案。

## 陷阱

**只有 OHLC 是還原價。** 同一列的 `bid`、`offer`、`next_ref_price`、
`next_limit_up`、`next_limit_down` 是**未還原**的原始報價：

```
2330  2000-09-04    close 27.9874（還原）    next_ref_price 133.50（未還原）
```

差 4.8 倍。混用必錯。

**日期範圍查詢用 macro。** Parquet 依 `year`/`month` 分區，DuckDB 無法從 `date`
述詞推出分區鍵：

```sql
SELECT * FROM adj_daily_prices_between('2024-01-01', '2024-03-31')
WHERE stock_id = '2330';
```

實測（暖快取、7 次取中位數）48.2 ms → 27.5 ms。

**回測歷史指數必須套用當時成分。** 不能把 365 檔聯集放進每一天——用
`index_members_on('TWN50', <date>)` 或 `v_index_constituents` 依 `date` 取當日成分。

## 指令與排程

這條管線用 **anaconda `py312`**，不是專案 venv（需要 `pywin32` 驅動 Excel COM）。

```powershell
cd tej-wizard
$python = "python"
& $python -m pip install -e ".[dev]"

& $python scripts\rebuild.py                # 兩個 dataset → Parquet → view（不碰 TEJ）
& $python scripts\rebuild.py --only index   # 只重建成分股層，約 35 秒
& $python scripts\verify.py                 # 35 項唯讀檢查
& $python -m pytest tests -q                # 76 項
```

```powershell
& $python scripts\refresh_workbooks.py --dataset index --dry-run
& $python scripts\refresh_workbooks.py --scope current-members --missing-only --dry-run
& $python scripts\refresh_workbooks.py --tickers 2330 --visible   # 第一次要監督
& $python scripts\create_workbooks.py --dry-run                   # 看缺哪些 workbook
& $python scripts\update.py                 # 每日排程
& $python scripts\weekly_refresh.py         # 每週全量
```

**Excel 跑在獨立的 Windows desktop 上**，更新時不會有視窗跳出來、也不會搶焦點。
`--visible` 會關掉隔離，用於監督執行。

**首次設定或 TEJ session 狀態不明時，先用 `--visible` 看著跑。** 增益集有自己的
TEJ session，過期時會跳登入視窗；隱藏的 Excel 會卡在那裡直到 watchdog 逾時砍掉它。

| 欄位 | 每日 | 每週 |
|---|---|---|
| 排程名稱 | `TEJ daily update` | `TEJ weekly refresh` |
| 觸發 | 每日 18:45 | 週日 03:00 |
| 程式 | `tej-wizard\scripts\update.bat` | `tej-wizard\scripts\weekly_refresh.bat` |
| 範圍 | 2 個指數檔 + 當期成分約 150 檔 | 全部 365 檔，含指數檔全量重抓 |
| 時間 | 約 55 分 | 約 2 小時 |

**為什麼要有每週全量？** 還原價不是 append-only——每次除權息都會重寫整段歷史，
所以「日期補齊」不等於「數字正確」。已退出指數的證券日期不再變動，
`v_workbook_status` 看不出它過期，只有重抓才知道。

**兩個時間都刻意錯開 FinMind。** 這條管線要讀 finmind 的交易日曆，而 finmind
每日 18:30、每週日 02:00 會用 staged swap 重寫它；錯開成 18:45 與 03:00，避免
邊寫邊讀，也避免兩個長時間工作搶同一顆硬碟。

**必須設「只在使用者登入時執行」。** 這是本工作與 FinMind 唯一不同的地方——
Excel 無法在 Session 0 啟動，設成「不論使用者是否登入」會每次都失敗。
漏跑不需手動補：下次執行會插入所有缺漏交易日，只 refresh 落後的活躍 workbook。

**註冊時不要用 `schtasks /TR`**，路徑裡的空格會被切開（FinMind 兩個工作就是這樣
靜默失敗一週）。用 `Register-ScheduledTask`,指令見
[`tej-wizard/docs/operations.md`](tej-wizard/docs/operations.md)。

**目前只註冊了每週工作(2026-09-05)。** 每日工作尚未建立,`update.py` 至今都是
手動執行;在建立之前,只有每週工作會推進 workbook。

**跨年度要手動建一次新的指數年度檔。** 1 月第一個交易日時 `2027.xlsx` 還不存在，
排程會大聲失敗而不是自己猜要複製哪個檔。步驟見
[`tej-wizard/docs/operations.md`](tej-wizard/docs/operations.md)。

---

## 查詢入口

```python
import duckdb
con = duckdb.connect(r"<repo>\db\finmind\finmind.duckdb",
                     read_only=True)          # 一律 read_only

con.execute("""
    SELECT date, stock_id, close, volume
    FROM daily_prices_between('2024-01-01', '2024-03-31')
    WHERE stock_id = '2330'
    ORDER BY date
""").df()
```

**一律 `read_only=True`。** 寫入連線會鎖住 `.duckdb`，擋掉排程更新，也會擋掉
你自己開著的 notebook；反過來也一樣。

不會 SQL 也沒關係，兩邊各有一份給只會 Pandas 的人的完整教學：
[`finmind/notebooks/database_usage_guide.ipynb`](finmind/notebooks/database_usage_guide.ipynb)
與 [`tej-wizard/notebooks/db_usage_guide.ipynb`](tej-wizard/notebooks/db_usage_guide.ipynb)。

## 文件地圖

| finmind | 內容 |
|---|---|
| [`docs/views-guide.md`](finmind/docs/views-guide.md) | 19 個 view 的完整中文說明 |
| [`docs/schema.md`](finmind/docs/schema.md) | 欄位型別、上游 API 實測行為、已處理的資料缺陷 |
| [`docs/architecture.md`](finmind/docs/architecture.md) | 分層契約與設計取捨 |
| [`docs/data-lineage.md`](finmind/docs/data-lineage.md) | 資料血緣與重建路徑 |
| [`docs/operations.md`](finmind/docs/operations.md) | 排程、runbook、成本模型 |
| [`docs/changelog/`](finmind/docs/changelog/) | 每次架構或資料變更的紀錄 |
| [`docs/BUILD_PLAN.md`](finmind/docs/BUILD_PLAN.md) | 原始建置計畫（**歷史紀錄，不再更新**） |

| tej-wizard | 內容 |
|---|---|
| [`docs/architecture.md`](tej-wizard/docs/architecture.md) | 分層契約、與 finmind 的差異、分區粒度實測、更新機制 |
| [`docs/schema.md`](tej-wizard/docs/schema.md) | 35 欄 xlsx → 40 欄 view 對照、實測資料事實、格式陷阱 |
| [`docs/operations.md`](tej-wizard/docs/operations.md) | 第一次監督執行、排程、runbook、壞掉時怎麼救 |
