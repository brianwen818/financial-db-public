# 跨來源驗證：FinMind 與 TEJ

有了兩個獨立來源，就能回答單一來源答不了的問題：**兩者哪裡不同？誰是對的？**
本目錄的所有數字都由程式產生，對應專案說明書第 7–8 章；數字為 2026-10-04 實測。

```
scripts/01_db_snapshot.py          兩個資料庫的規模、涵蓋、健康狀態   → outputs/tables/snapshot_*.csv
scripts/02_source_comparison.py    逐列比對、歸因、合理性檢驗           → outputs/tables/cmp_*.csv, case_*.csv
build/build_figures.py             → outputs/figures/*.png
build/build_pptx.py、build_docx.py → docs/ 的簡報與說明書（透過 build/facts.py 讀取上面的 CSV，沒有任何手打數字）
```

兩支分析腳本以 `READ_ONLY` 掛載兩個 DuckDB 檔，所有中間結果都是 TEMP table，不會寫入資料目錄。

## 方法

**比日報酬，不比價格水準。** 兩個來源都把歷史回溯調整到「最新」基準，只要重抓日期不同，整段
價格水準就差一個常數——逐列比還原收盤價只有 69.3 % 在 0.01 元以內，但那不是錯。同一檔、同一天
的報酬不受基準影響。

| | |
|---|---|
| 比對範圍 | 兩邊共有的 2,188 檔、941 萬列（2000-09-04 → 2026-09-16） |
| 乾淨樣本 | 上市櫃（TSE／OTC／TIB）、前後兩天都有成交的日報酬，868 萬筆；興櫃是議價、無漲跌幅，另計 |
| 一致門檻 | 10 bp（TEJ 四位小數、FinMind 六位的捨入雜訊）；超過 1 % 才算實質不一致 |
| 第三把尺 | FinMind **未還原**報酬。某天誰的報酬等於未還原報酬，誰那天就沒做調整 |
| 合理性檢驗 | 漲跌幅限制：2015-05-29 前 7 %、之後 10 %（+0.5 % 低價股 tick 寬限）。還原後仍超限的報酬不是真實事件就是沒調乾淨 |

## 結果

乾淨樣本中 **99.96 %** 的日報酬相差在 10 bp 以內（相關係數 0.9963）。相差超過 1 % 的 10,008 個
交易日，每一筆歸到下列原因之一（[`outputs/tables/cmp_disagreement_causes.csv`](outputs/tables/cmp_disagreement_causes.csv)）：

| 原因 | 交易日 | 證券 | 判定方式 |
|---|---:|---:|---|
| A 無成交日的記法不同 | 7,163 | 401 | FinMind 未還原收盤價為 0 的日子及其次日 |
| C FinMind 未還原除權息 | 2,806 | 864 | FinMind 報酬＝未還原報酬，TEJ ≠ |
| D FinMind 多調 | 20 | 20 | TEJ 報酬＝未還原報酬，FinMind ≠ |
| B 本專案管線：還原基準混用 | 10 | 10 | 落在最近一次每週重抓之後 |
| E 兩邊都調、幅度不同 | 9 | 9 | 兩邊都 ≠ 未還原報酬 |

![原因分類](outputs/figures/causes.png)

### 發現一：FinMind 的還原價，早期並沒有還原

逐年統計未還原價中超過漲跌幅的跳空（大致就是除權息日），再看還原後還剩多少。上市股到 2002 年、
上櫃股到 2007 年，FinMind 還原價的跳空與未還原價**完全相同**——沒有調整。拿這段 FinMind 還原價
回測，除權息會被當成單日暴跌。2008 年以後兩邊一致：2,074 檔中 1,935 檔整段累積報酬相差不到 1 %。

![殘留比例](outputs/figures/residual_share.png)

### 發現二：交叉驗證抓到自己管線的錯誤

有一類差異很特別：FinMind 自己的單檔歷史與 TEJ 一致，本專案 processed 層卻不同。6669 緯穎
2026-09-02 除權，修正前的 processed 層在 8/21 與 8/31 出現 +195 % 與 −67 % 的假報酬。

原因：同一個 `(stock_id, date)` 可能同時來自「單檔全歷史」與「全市場日檔」。原本去重一律讓
全市場日檔優先（收盤後結算快照），對未還原價是對的，對還原價是錯的——還原價只在抓取當下的
基準下有意義，舊基準的日檔每次重建都把正確的全歷史蓋回去，每週重抓也修不好。

修正：還原價改為單檔全歷史優先、日檔只補缺；去重優先序改為每個 dataset 各自設定；新增 4 個測試
與 1 條健康檢查（還原價中來自日檔的交易日數 > 7 就警告）；從 raw 重建，不需呼叫 API。
變更紀錄：[`2026-10-04-adj-dedupe-ticker-first.md`](../pipelines/finmind/docs/changelog/2026-10-04-adj-dedupe-ticker-first.md)。

| 指標 | 修正前 | 修正後 |
|---|---:|---:|
| 受舊基準影響的資料 | 319 檔、1,046 列 | 0 |
| 還原價中來自日檔的交易日數 | 19 | 6 |
| 來自本專案管線的不一致交易日 | 105 | 10 |

修正前的全部表格保留在 [`outputs/tables_before_fix/`](outputs/tables_before_fix/)，說明書引用它們作為「修正前」證據。

![6669 案例](outputs/figures/case_6669.png)

### 發現三：其他只有對照才看得到的缺陷

| 現象（FinMind 一側） | 規模 | 影響 |
|---|---|---|
| 無成交日收盤價記成 0 | 未還原價 387,319 列（3.3 %），其中 36,489 列仍有成交量 | 直接算報酬得到 −100 % |
| 成交量單位不一致 | 2000–2003 年有 15–27 % 的列恰為 TEJ 的千分之一 | 早期成交量不能跨年比較 |
| 上櫃資料缺漏 | 2007 年 1–4 月上櫃股幾乎整段沒有 | 該期間橫斷面不完整 |
| 減資後復牌未調整 | 818 個停牌後跳空 > 25 % 的事件：TEJ 消除 662、FinMind 405 | 413 個完全沒調 |
| 多餘的調整 | 20 個交易日未還原價無事件、還原價卻跳動 | 上游錯誤 |

### 誰比較可信

同一批 867 萬筆日報酬中，未還原價有 6,069 筆超過當時漲跌幅；還原後 FinMind 剩 2,141 筆、TEJ 剩
851 筆（多為真實事件，如新掛牌前五日無漲跌幅）。就「用於計算報酬與回測的還原價」而言 TEJ 較可信；
但 TEJ 也有待查之處（18 個超限還原報酬、2,788 列內建報酬欄與自算報酬相差 > 1 %、67,707 個存續期內
缺漏日），列在說明書第 8 章。

![超限筆數](outputs/figures/beyond_limit.png)

## 重跑

需要兩個 DuckDB 檔在 `<repo>/db/`（本 repo 未附資料，見根目錄 README）。

```powershell
cd analysis
python scripts\01_db_snapshot.py
python scripts\02_source_comparison.py
python build\build_figures.py
python build\build_pptx.py      # 需要 python-pptx；版面檢查用 build\office_render.py（Windows + Office）
python build\build_docx.py      # 需要 python-docx
```

不要在排程更新進行中執行：開著的讀取連線會擋住 view 重建與目錄換檔。
