# financial-db — 台股研究級資料倉儲

> 從資料蒐集、分層建模到跨來源驗證。研究所備審資料（財務工程／資料科學）專題，2026 年 8–10 月。

兩條獨立的台股日資料管線——**FinMind API** 與 **TEJ Smart Wizard**（Excel 增益集，沒有 API）——
遵守同一套三層契約：`raw` 保留上游原貌、`processed` 是定型／去重／分區的 Parquet、
DuckDB 只存 view，可隨時刪除重建。有了兩個來源之後，再用第二個來源驗證第一個：
868 萬筆日報酬中 99.96% 相差在 10 bp 以內，其餘 10,008 個不一致日每一筆都歸因到具體原因，
過程中也抓到並修正了自己管線裡的一個去重錯誤。

## English abstract

A research-grade daily data warehouse for Taiwan equities, built from two independent sources:
the FinMind REST API (whole market, 1994–) and TEJ Smart Wizard (an Excel add-in with no API,
driven through COM automation; TWN50 / TM100 historical constituents and 2,453 securities with
survivorship-bias-free adjusted prices). Both pipelines follow one three-layer contract:
*raw* is stored untouched, *processed* is typed, de-duplicated, partitioned Parquet, and DuckDB
holds only views (39 in total) so the database file can be deleted and rebuilt at any time.
Correctness rests on 286 automated tests, 63 read-only health checks, and cross-validation
between the two sources on daily *returns* rather than price levels: 99.96% of 8.68 M
overlapping daily returns agree within 10 bp, and every one of the remaining 10,008
disagreeing days is attributed to a cause (unadjusted corporate actions in FinMind before
2003/2008, zero-coded no-trade days, a dedupe bug in this project's own pipeline that the
comparison exposed and that has since been fixed with regression tests).

| | |
|---|---|
| Rows | **33.9 M** (FinMind 11.75 M unadjusted + 11.25 M adjusted; TEJ 10.1 M adjusted + 0.84 M index membership) |
| Coverage | FinMind: ~3,900 securities incl. 767 recovered delisted ones, 1994-10 →; TEJ: 2,453 securities (462 no longer trading, full history kept), 2000-09 → |
| Views / macros | 39 views + 6 macros, zero tables; each `.duckdb` file is 268 KB |
| Tests / checks | 286 pytest cases, 63 read-only health checks run after every scheduled job |
| Cross-validation | 2,188 common securities, 9.4 M overlapping rows; 99.96 % of clean daily returns agree within 10 bp |
| Stack | Python 3.12 · DuckDB · PyArrow / Parquet · pandas · requests · pywin32 (Excel COM) · pytest · Windows Task Scheduler |

![架構圖](analysis/outputs/figures/architecture.png)

## 先看哪裡

| 想知道 | 看這裡 |
|---|---|
| 完整專案說明（23 頁） | [`docs/financial-db-專案說明書.pdf`](docs/financial-db-專案說明書.pdf) |
| 簡報版（29 頁） | [`docs/financial-db-專案簡報.pdf`](docs/financial-db-專案簡報.pdf) |
| 兩個來源哪裡不同、誰比較可信 | [`analysis/README.md`](analysis/README.md) → 程式 [`analysis/scripts/02_source_comparison.py`](analysis/scripts/02_source_comparison.py) |
| 資料庫怎麼選、view 與陷阱 | [`pipelines/README.md`](pipelines/README.md)、[`docs/data-map.md`](docs/data-map.md)、[`docs/schema-map.md`](docs/schema-map.md) |
| 管線程式碼 | [`pipelines/finmind/src/finmind_pipeline/`](pipelines/finmind/src/finmind_pipeline/)、[`pipelines/tej-wizard/src/tej_pipeline/`](pipelines/tej-wizard/src/tej_pipeline/) |
| SQL view 與 macro | [`pipelines/finmind/sql/`](pipelines/finmind/sql/)、[`pipelines/tej-wizard/sql/`](pipelines/tej-wizard/sql/) |
| 測試 | [`pipelines/finmind/tests/`](pipelines/finmind/tests/)（210）、[`pipelines/tej-wizard/tests/`](pipelines/tej-wizard/tests/)（76） |
| 設計取捨與上游缺陷的處理 | [`pipelines/finmind/docs/`](pipelines/finmind/docs/)、[`pipelines/tej-wizard/docs/`](pipelines/tej-wizard/docs/) |
| 只會 pandas 的使用教學 | [`pipelines/finmind/notebooks/database_usage_guide.ipynb`](pipelines/finmind/notebooks/database_usage_guide.ipynb) |

## 技術重點

**1. 三層契約，view-only DuckDB。** raw 一個字不改（FinMind 的欄名還是上游的 `max`、`min`、
`Trading_Volume`；TEJ 的 xlsx 原檔即唯一真相）。processed 才是查詢契約：定型、依自然鍵去重、
按 `year/month` 分區。DuckDB 檔只有 268 KB，因為它一張表都沒有，刪掉重跑 `build_db.py` 就回來。
日期範圍查詢走 macro 才會裁剪分區（實測 0.56 s → 0.13 s）。

**2. 存活者偏誤是量化過的，不是口頭提醒。** FinMind 的證券主檔只列現存證券；管線自 2004 年起
每月抽一天全市場掃描，找回 767 檔已下市證券。結果寫進 `v_securities_all`，README 明示
「無偏誤橫斷面最早到 2004-02-11」與「還原價只涵蓋 7/767 已下市檔，survivors-only」。
TEJ 一側則保留 462 檔已停止交易證券的完整還原歷史。

**3. 沒有 API 的來源也能自動化。** TEJ 資料只能透過 Excel 增益集取得。
[`excel_refresh.py`](pipelines/tej-wizard/src/tej_pipeline/ingestion/excel_refresh.py) 以 COM 驅動
隱藏的 Excel 跑在獨立的 Windows desktop 上（不搶焦點），先依 FinMind 的交易日曆往工作表插入缺漏
交易日、再同步等增益集填完，watchdog 逾時就砍掉；新增日期沒填出收盤價就不存檔、整次更新停止。

**4. 跨行程限流、原子寫入、可重跑排程。** API 配額用跨行程 token bucket 管理
（[`rate_limiter.py`](pipelines/finmind/src/finmind_pipeline/utils/rate_limiter.py)）；Parquet 一律
寫暫存目錄再換名（[`parquet_io.py`](pipelines/finmind/src/finmind_pipeline/utils/parquet_io.py)）；
每日更新是「新日期 ∪ 最近 3 天重抓 ∪ 範圍內缺漏」的聯集，漏跑不必手動補；每次執行寫 JSONL，
以 `v_ingestion_runs` 查詢。

**5. 用第二個來源驗證第一個。** 比的是日報酬不是價格水準（兩邊還原基準日不同，水準會差一個常數）。
以 FinMind 未還原報酬當第三把尺判定「那天誰做了調整」，以漲跌幅限制（7 %／10 %）做合理性檢驗。
結論：FinMind 還原價在上市股 2003 年以前、上櫃股 2008 年以前**沒有還原除權息**（2,806 日／864 檔）；
無成交日記成 0（387,319 列）；2000–2003 年成交量單位不一致。詳見 [`analysis/`](analysis/README.md)。

**6. 抓到自己的錯誤並留下紀錄。** 同一比對發現 processed 層的還原價被舊基準的全市場日檔覆蓋
（6669 緯穎除權前後出現 +195 % / −67 % 的假報酬）。修正去重優先序、新增 4 個測試與 1 條健康檢查、
從 raw 重建，寫成 [changelog](pipelines/finmind/docs/changelog/2026-10-04-adj-dedupe-ticker-first.md)；
修正前的分析結果保留在 `analysis/outputs/tables_before_fix/` 作為對照。

<p align="center">
<img src="analysis/outputs/figures/residual_share.png" width="48%" alt="每年未還原跳空中，還原後仍殘留的比例">
<img src="analysis/outputs/figures/case_6669.png" width="48%" alt="6669 緯穎除權前後三條序列">
</p>

## Repo 結構

```
financial-db-public/
├── README.md                 本文
├── docs/                     專案說明書、簡報（PDF + 原檔）、資料地圖、Schema 圖
├── pipelines/
│   ├── README.md             兩個資料庫的選用指南、處理步驟、view 清單、陷阱、指令
│   ├── finmind/              FinMind API → Parquet → DuckDB   (src / sql / scripts / tests / docs / notebooks)
│   └── tej-wizard/           TEJ 增益集 → xlsx → Parquet → DuckDB（同上）
└── analysis/
    ├── README.md             跨來源驗證的方法與發現
    ├── scripts/              01_db_snapshot.py、02_source_comparison.py（唯讀查詢兩個 DuckDB）
    ├── build/                由分析結果產生圖表、簡報與說明書的程式（python-pptx / python-docx）
    └── outputs/              figures/ 13 張圖、tables/ 彙總 CSV、tables_before_fix/ 修正前對照
```

## 資料未隨附

本 repo 只有程式碼、文件與彙總結果，**不含任何資料檔**（`db/` 約 6 GB）：

- **TEJ** 是授權資料，不能再散布。
- **FinMind** 可自行到 [finmindtrade.com](https://finmindtrade.com/) 申請 token 後重建，
  步驟見 [`pipelines/README.md`](pipelines/README.md#指令)；資料根目錄預設為 `<repo>/db/`，
  可用 `FINMIND_DB_ROOT`／`TEJ_DB_ROOT` 環境變數覆寫。
- `analysis/outputs/tables/` 只放彙總統計（逐年計數、原因分類、Top-N 摘要），不含逐日價格序列。
- 測試：純單元測試不需要資料，在乾淨環境實測 finmind 166 項、tej-wizard 61 項通過；其餘是對真實資料庫的
  整合測試與 golden-value 測試，需要 token 與已建好的倉儲才能跑。

文件中的 `<repo>` 代表本 repo 的根目錄；原始環境的絕對路徑已全部改寫。

## 已知限制

- FinMind token 自 2026-09-19 起降為免費方案，FinMind 一側資料停在 2026-09-16。
- TEJ 每日排程尚未註冊，指數成分以外的證券僅靠每週全量更新。
- TEJ 一側仍有 18 個超過漲跌幅的還原報酬、2,788 列內建報酬欄位差異與 67,707 個存續期內缺漏日未逐筆查證。
- 只有日頻價量與指數成分，沒有財報、法人與融資券資料。

## 關於

作者：[brianwen818](https://github.com/brianwen818)。本專案為畢業專題的一部分，2026-08-25 起開發；
程式碼與文件由作者撰寫，開發過程中使用 AI 輔助工具（Claude Code）協助。
程式碼以 [MIT License](LICENSE) 釋出；資料來源各依其授權條款，不在授權範圍內。
