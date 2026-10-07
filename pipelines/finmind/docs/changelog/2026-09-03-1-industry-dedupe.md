# 2026-09-03 — 產業鏈維度表去重

`tw_industry` 的 processed 層改為依自然鍵 `(stock_id, industry, sub_industry)`
去重，`snapshot_date` 取 `max()`。`v_industry` 從 11,520 列回到 6,871 列，並修好
一個會隨每天擷取無限增長的重複問題。同時修好兩個壞掉的 Windows 排程工作。

## 為什麼

`TaiwanStockIndustryChain` 的 `date` 欄位是 FinMind 自己的**爬取時間戳**，不是
生效日——同一組對應關係只要被上游重爬一次就換一個戳記，內容一字不變。
[tw_industry.py](../../src/finmind_pipeline/ingestion/tw_industry.py) 的 docstring
原本就寫明了這點。

但 `process_industry()` 做的是 `SELECT DISTINCT *`，而 `*` 包含 `snapshot_date`，
且 `_read_all_snapshots()` 會聯集**所有**歷史 raw 快照。結果是同一筆事實在不同快照
裡帶著不同戳記，就被當成兩筆不同的列。

2330 是最乾淨的例子，內容完全相同，只有戳記動了：

| 來源 raw 檔 | 上游 `date` | industry | sub_industry |
|---|---|---|---|
| `2026-08-26.parquet` | 2026-08-26 | 半導體 | 晶圓製造 |
| `2026-09-03.parquet` | 2026-08-29 | 半導體 | 晶圓製造 |

兩個 raw 檔各自內部都沒有重複（實測皆為 0），重複 100% 來自跨快照聯集。

**觸發時間點：**2026-09-03 的 `daily_update` 是本專案史上第二次擷取 industry，
所以這天是重複第一次出現。（2026-08-30 的 weekly refresh 只重跑了 processing，
log 顯示 `tw_industry: 6867 rows`，沒有落新快照。）

**增長速率：**這一次擷取就多出 4,653 列。[daily_update.py:78](../../scripts/daily_update.py#L78)
是**每天**擷取 industry 的，所以未過濾的話約每天多 4,600 列廢列，而事實一筆都沒增加。

## 改了什麼

**去重規則**（`processing/dimensions.py` → `process_industry()`）

```diff
-SELECT DISTINCT * FROM t ORDER BY stock_id, industry, sub_industry
+SELECT stock_id, industry, sub_industry, max(snapshot_date) AS snapshot_date
+FROM t GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
```

**資料**

| 項目 | 之前 | 之後 |
|---|---|---|
| `v_industry` 列數 | 11,520 | **6,871** |
| 不重複自然鍵 | 6,871 | 6,871 |
| 重複鍵數量 | 4,649（各 2 份） | **0** |
| `v_industry_by_stock` 列數 | 2,356 | 2,356（不變） |
| 2330 的 `snapshot_date` | 2026-08-26 與 2026-08-29 兩列 | 一列，2026-08-29 |

**Schema：**欄位沒有增減，`PROCESSED_INDUSTRY_SCHEMA` 未動。改變的是
`snapshot_date` 的**語意**：從「某一次爬取的戳記」變成「這組對應關係最後一次
在上游被看到的日期」。

**文件**

- `docs/schema.md` — `tw_industry` 段落新增「One row per key, enforced」
- `docs/views-guide.md` §6.1、`docs/agent_guide.md` §4.3 — 說明 `snapshot_date`
  不是生效日、產業分類沒有 point-in-time 歷史
- `sql/views/22_v_industry.sql` — 註解補上同一件事（View 本身的 SQL 未改）

## 對既有查詢的影響

**會變的：**

- 直接查 `v_industry` 的列數會少掉 4,649 列。今天到這次修正之間，任何
  `COUNT(*)`、`SUM()`、或未加 `DISTINCT` 的產業分組統計，有 68% 的對應關係被
  算成兩份——**現在的數字才是對的**。
- 用 `WHERE snapshot_date = '...'` 篩 `v_industry` 的查詢結果會變。這種寫法本來
  就取不到 point-in-time 結果，只是把「最後被重爬的時間剛好落在那天」的列篩出來。
- 把 `v_industry` 直接 JOIN 行情的查詢，fan out 倍數會回到只由多對多本身決定。

**不會變的：**

- `v_industry_by_stock` 的內容。它用 `list_distinct(list(...))`，重複本來就被
  吸收掉了——這也是為什麼問題是被 `verify.py` 抓到，而不是被使用者踩到。照
  `agent_guide.md` 建議走 `v_industry_by_stock` 的查詢完全不受影響。
- 所有價格、日曆、證券主檔相關的 View。
- API 呼叫數與排程成本。

## 排程工作修正（非資料變更，但會影響資料新鮮度）

兩個 Windows 排程工作都沒有真的在跑：

| 工作 | 最後結果 | 原因 |
|---|---|---|
| FinMind daily update | `0x80070002` | 註冊的 `Execute` 是 `D:\Github`，路徑在空格處被切斷 |
| FinMind weekly refresh | `0x800710E0` | 同樣的路徑問題，加上 `DisallowStartIfOnBatteries` |

所以 `v_daily_prices` 從 2026-08-26 停在原地直到 2026-09-03 手動補跑。已用
`Set-ScheduledTask` 修正（觸發時間與帳號不變）：

- `Execute` 改為完整路徑，`WorkingDirectory` 指向 finmind 專案目錄
- `DisallowStartIfOnBatteries`、`StopIfGoingOnBatteries` → `False`
- 新增 `StartWhenAvailable`（錯過的排程開機後補跑）與失敗重試 3 次 / 間隔 15 分鐘

`operations.md` 裡用 `schtasks /TR` 建立工作的那段指令，就是這次路徑被切斷的來源；
要重建工作時建議改用 `New-ScheduledTaskAction -Execute <bat> -WorkingDirectory <dir>`。

## 已知限制

**1. 產業分類沒有歷史，這次也沒有加。**
processed 層現在是「所有快照聯集後去重」，只保留最後看到的日期。真正的歷史戳記
都還在 `raw/tw-industry/*.parquet`（目前 2 個檔，上游 `date` 橫跨 2025-03-30 至
2026-09-03，45 與 50 個相異日期）。若日後要做 point-in-time 產業歸屬，得從 raw
重建一張有生效區間的表——但上游給的是爬取戳記，不是生效日，這件事本質上做不準。

**2. 上游移除的對應關係會永遠留著。**
聯集語意代表某檔股票若被上游拿掉某個產業標籤，那筆對應仍在表中，只是
`snapshot_date` 停在舊日期。這與本專案在下市證券上的處理哲學一致（聯集、保留
歷史）。不能拿「`snapshot_date` 很舊」當成「已被移除」的證據，因為戳記只在上游
重爬時才更新。

**3. 已下市證券仍然沒有產業分類**（見 2026-08-30 那份），本次未變。

## 驗證

```
ruff       src / scripts / tests  All checks passed
           （notebooks 有 3 個既有錯誤，與本次無關）
pytest     199 passed
verify.py  23 checks · 22 passed · 1 warning · 0 failures
```

`industry key (stock_id, industry, sub_industry) unique` 由 FAIL(4,649) 轉為 PASS，
`verify.py` 與 `tests/test_warehouse.py::test_industry_key_is_unique` 兩條斷言
**都沒有改動**——它們本來就是對的，錯的是資料。

唯一的 warning 是 2026-08-26 一次舊的 backfill 失敗紀錄，與本次無關。

## 重現方式

```powershell
.\.venv\Scripts\python.exe -c "from finmind_pipeline.processing import dimensions; dimensions.process_industry()"
.\.venv\Scripts\python.exe scripts\build_db.py
.\.venv\Scripts\python.exe scripts\verify.py
```

不需要呼叫 API：raw 層是權威，這次只是重算 processed 層。
