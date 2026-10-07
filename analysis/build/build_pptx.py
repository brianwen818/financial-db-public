"""Build the project deck: data-pipelines/financial-db-專案簡報.pptx.

Written to be read without a presenter (an application portfolio), so each
slide carries its own sentence of explanation. Every number comes from
``facts.Facts`` -- re-run the analysis scripts, then this, to refresh them.
"""

from __future__ import annotations

from pptx.util import Inches

import figures
from build_figures import FINMIND_SCHEMA, TEJ_SCHEMA
from deck_kit import (
    BODY_W, LAYOUT_SECTION, LAYOUT_TITLE, MARGIN, SLIDE_W, box, bullets, card, content_slide, dot,
    new_presentation, stat, table, text,
)
from facts import CHECKS, DELIVERABLES, TESTS, VIEWS, Facts, n, pct, wan

X0 = MARGIN
LEAD_Y = 1.42
TOP = 2.08          # first row under a lead sentence
BOTTOM = 6.8        # last usable y above the footer


def lead(slide, sentence: str) -> None:
    text(slide, X0, LEAD_Y, BODY_W, 0.5, sentence, size=16, color="sub")


def divider(prs, number: str, title: str, sub: str) -> None:
    slide = prs.slides.add_slide(prs.slide_layouts[LAYOUT_SECTION])
    slide.shapes.title.text = f"{number}　{title}"
    slide.placeholders[1].text = sub


def columns(count: int, gap: float = 0.3, x: float = X0, w: float = BODY_W):
    cw = (w - (count - 1) * gap) / count
    return [x + i * (cw + gap) for i in range(count)], cw


def build() -> None:
    f = Facts()
    prs = new_presentation(footer="financial-db｜台股研究級資料倉儲")
    total_wan = f"{f.total_rows / 1e4:,.0f} 萬"
    tests, checks, views = sum(TESTS.values()), sum(CHECKS.values()), sum(VIEWS.values())
    disagreements = int(f.causes["days"].sum())
    zero_rows = int(f.zero["close_is_zero"].sum())
    zero_with_volume = int(f.zero["zero_price_with_volume"].sum())

    # ---------------------------------------------------------------- title
    s = prs.slides.add_slide(prs.slide_layouts[LAYOUT_TITLE])
    s.shapes.title.text = "financial-db\n台股研究級資料倉儲"
    s.placeholders[1].text = "以 Parquet＋DuckDB 建置兩條可重現的資料管線，並用跨來源驗證找出資料與程式的錯誤"
    for i, (label, role) in enumerate((("FinMind", "fm"), ("TEJ", "tej"), ("DuckDB", "ok"))):
        dot(s, MARGIN + 0.2 + i * 1.75, 1.32, 0.2, role)
        text(s, MARGIN + 0.5 + i * 1.75, 1.25, 1.3, 0.35, label, size=15, color="white", bold=True)
    text(s, MARGIN + 0.2, 6.3, 9, 0.35, "研究所備審資料｜專題說明｜2026 年 10 月", size=13, color="muted")

    # -------------------------------------------------------------- summary
    s = content_slide(prs, "一頁看懂：建了什麼、驗證了什麼")
    xs, cw = columns(2, gap=0.3, w=5.9)
    tiles = [
        (total_wan, "列日資料：兩個來源的行情與指數成分，最早自 1994 年 10 月", "ink"),
        ("2 條管線", f"FinMind API 與 TEJ Excel 增益集，共用三層契約，對外提供 {views} 個 view", "tej"),
        (wan(f.clean.n), "筆日報酬做跨來源比對，" + pct(f.clean.agree_within_10bp_pct, 2) + " 相差在 10bp 以內", "ok"),
        (f"{tests}＋{checks}", "個自動化測試與健康檢查，每次改動後全部重跑", "fm"),
    ]
    for i, (value, label, role) in enumerate(tiles):
        stat(s, xs[i % 2], 1.6 + (i // 2) * 2.6, cw, 2.35, value, label, role=role, value_size=36, label_size=14)
    cards = [
        ("建了什麼", "把上游資料原樣落地（raw），整理成有型別、去重、分區的 Parquet（processed），"
                     "再用一張資料表都沒有的 DuckDB view 提供查詢。", "ink"),
        ("怎麼確保正確", "每一層都能由上一層重建；文件與斷言裡的數字一律實測；"
                         "用對帳 view、健康檢查與第二個資料來源交叉驗證。", "ok"),
        ("發現了什麼", "FinMind 的還原價在上市股 2003 年前、上櫃股 2008 年前其實沒有還原；"
                       "同一套比對也抓到並修正了自己管線的一個去重錯誤。", "fm"),
    ]
    for i, (title, body, role) in enumerate(cards):
        card(s, 6.85, 1.6 + i * 1.72, SLIDE_W - MARGIN - 6.85, 1.55, title, body, role=role, size=14)

    # ----------------------------------------------------------- motivation
    s = content_slide(prs, "為什麼不直接呼叫 API：回測資料的三個陷阱")
    lead(s, "直接拿公開資料做回測，結果常常好得不真實。這個專案把三個常見問題變成設計需求。")
    xs, cw = columns(3)
    traps = [
        ("存活者偏誤", "fm",
         "證券主檔只列現在還在的股票，已下市的公司會從歷史橫斷面中消失，報酬因此被高估。",
         f"每月抽一天掃描全市場，找回 {n(f.delisted.delisted_recovered)} 檔已下市證券；"
         f"TEJ 側保留 {n(f.tej_stopped)} 檔已停止交易公司的完整歷史。"),
        ("還原價會被改寫", "tej",
         "每次除權息都會重寫整段還原歷史。只會往後追加的資料庫，舊資料會悄悄過期。",
         "每週全量重抓還原價，並把「這筆資料屬於哪個還原基準」納入去重規則。"),
        ("單一來源無法自證", "ok",
         "單位錯誤、缺漏、沒調整到的除權息——來源自己的錯，只看這個來源是看不出來的。",
         "兩個獨立來源、同一套契約，逐日比對報酬，再用漲跌幅限制檢驗合理性。"),
    ]
    for cx, (title, role, problem, answer) in zip(xs, traps, strict=True):
        card(s, cx, TOP, cw, BOTTOM - TOP, title, [
            {"runs": [("問題", {"bold": True, "color": "sub"})], "after": 4, "size": 13},
            {"runs": [problem], "after": 22},
            {"runs": [("設計回應", {"bold": True, "color": "sub"})], "after": 4, "size": 13},
            {"runs": [answer]},
        ], role=role, size=16, title_size=22, pad=0.32)

    # ================================================================ part 1
    divider(prs, "一", "架構與資料", "三層契約、Parquet＋DuckDB 的取捨、資料盤點")

    s = content_slide(prs, "三層契約：兩條管線，同一個心智模型")
    lead(s, "raw 保留上游原貌，processed 才是查詢契約，DuckDB 只是蓋在 Parquet 上的 view。")
    figures.architecture(s, X0, TOP, BODY_W, BOTTOM - TOP, fs=15, f=f)

    s = content_slide(prs, "為什麼是 Parquet＋DuckDB：每個取捨都有實測數字")
    choices = [
        ("Parquet 是唯一真相", "欄式、壓縮、任何工具都讀得到。DuckDB 檔裡一張資料表都沒有，"
                               "刪掉重跑一支程式就回來，也避開了單一寫入者的檔案鎖。"),
        ("分區粒度由更新方式決定", "FinMind 每天只新增一個檔，所以一天一檔、寫入天然冪等；"
                                   "TEJ 每次全量重建，改用一月一檔。"),
        ("日期查詢一律走 macro", "分區鍵是 year／month，只寫日期條件不會裁剪檔案。"
                                 "*_between(lo, hi) macro 會自動補上 year 條件。"),
    ]
    for i, (title, body) in enumerate(choices):
        card(s, X0, 1.6 + i * 1.75, 6.5, 1.58, title, body, role="ok", size=14)
    xs, cw = columns(2, gap=0.3, x=7.4, w=SLIDE_W - MARGIN - 7.4)
    measured = [
        ("268 KB", "兩個 .duckdb 檔各自的大小：純 view、零 table"),
        ("4.3×", "補上分區條件後，查一季資料由 0.56 秒降到 0.13 秒"),
        ("15×", "TEJ 月分區相對日分區的全表掃描（624 ms → 41 ms）"),
        ("2.8×", "同一份資料，月分區的檔案較小（188.9 MB → 67.1 MB）"),
    ]
    for i, (value, label) in enumerate(measured):
        stat(s, xs[i % 2], 1.6 + (i // 2) * 2.63, cw, 2.45, value, label, role="ok", value_size=36, label_size=14)

    s = content_slide(prs, f"有哪些資料：{total_wan}列、五類資料集")
    span = lambda r: f"{str(r.first_date)[:7]} → {str(r.last_date)[:7]}"  # noqa: E731
    rows = [
        ["資料集", "來源", "列數", "證券數", "期間"],
        ["未還原日行情\n開高低收、成交量值、筆數", ("FinMind", "fm"), n(f.fm_raw.n_rows), n(f.fm_raw.securities), span(f.fm_raw)],
        ["還原日行情", ("FinMind", "fm"), n(f.fm_adj.n_rows), n(f.fm_adj.securities), span(f.fm_adj)],
        ["還原日行情\n40 欄：估值、權重、處置旗標", ("TEJ", "tej"), n(f.tej_adj.n_rows), n(f.tej_adj.securities), span(f.tej_adj)],
        ["指數每日成分\nTWN50、TM100", ("TEJ", "tej"), n(f.tej_idx.n_rows), n(f.tej_idx.securities), span(f.tej_idx)],
        ["維度：證券主檔、產業鏈、\n交易日曆、已下市清單", ("FinMind", "fm"),
         f"{n(f.fm_universe)} 檔證券\n{n(f.health.fm_calendar_days)} 個交易日", "—", "1994-10 起"],
    ]
    table(s, X0, 1.6, [2.6, 0.9, 1.35, 0.85, 2.05], rows, size=12,
          heights=[0.42, 0.68, 0.48, 0.68, 0.68, 0.68], align="llrrl")
    text(s, X0, 5.45, 7.75, 1.2, [
        {"runs": [("沒有抓的：", {"bold": True}), "財報、三大法人、融資券、股利明細、盤中與分鐘資料。"], "after": 6},
        {"runs": ["FinMind 的價格範圍刻意排除權證與 TDR：全市場一天約 4.4 萬檔，只保留約 2,800 檔。"]},
    ], size=13, color="sub")
    text(s, 8.75, 1.6, 3.98, 0.3, "每年列數（千列）", size=13, bold=True, color="ink")
    figures.rows_per_year(s, 8.65, 1.9, SLIDE_W - MARGIN - 8.65, 4.2, fs=11, f=f)
    text(s, 8.75, 6.15, 3.98, 0.5, "未滿一年的 2026 年不列入。", size=11, color="sub")

    s = content_slide(prs, "兩個來源各有所長：先選對資料庫")
    rows = [
        ["面向", "FinMind", "TEJ"],
        ["來源與取得", "FinMind API v4（HTTP＋token），隨時可以重抓", "TEJ Smart Wizard Excel 增益集；授權資料，raw 無法重製"],
        ["涵蓋", f"全市場 {n(f.fm_universe)} 檔：上市、上櫃、興櫃、ETF、指數",
         f"公司池 {n(f.tej_adj.securities)} 檔，加上 TWN50／TM100 每日成分"],
        ["期間", "1994-10-01 起", "2000-09-04 起"],
        ["已下市公司的還原價",
         f"找回的 {n(f.delisted.delisted_recovered)} 檔中只有 {n(f.delisted.with_adjusted)} 檔有（上游缺口）",
         f"{n(f.tej_stopped)} 檔已停止交易者全數保留"],
        ["每列欄位", "精簡：開高低收、成交量值、筆數", "40 欄：估值、殖利率、市值比重、漲跌停與處置旗標"],
        ["適合", "全市場橫斷面、選股池、產業、2000 年以前", "無偏誤的還原報酬、估值因子、指數的歷史成分"],
    ]
    table(s, X0, 1.65, [2.5, 4.8, 4.83], rows, size=13, row_h=0.68, bold_first_col=True,
          heights=[0.45] + [0.72] * 6)

    # ================================================================ part 2
    divider(prs, "二", "蒐集、處理與更新", "API 與 Excel 兩種截然不同的來源，如何收斂到同一套流程")

    s = content_slide(prs, "FinMind：用最少的 API 呼叫抓完 32 年")
    figures.finmind_order(s, X0, 1.6, BODY_W, 2.05, fs=14, f=f)
    xs, cw = columns(3)
    notes = [
        ("兩種抓取形狀", "回補時一次呼叫拿一檔的全部歷史，約 3,600 次呼叫補完 32 年；"
                         "每日更新時一次呼叫拿全市場一天。兩種都原樣落地，raw 只增不改。"),
        ("跨行程限流", "自寫 token-bucket 限流器，狀態存在檔案裡，排程與手動執行共用同一份配額"
                       "（方案上限的 90%）。配額用盡與伺服器錯誤各有退避重試。"),
        ("找回已下市證券", "主檔只列現存證券。自 2004 年起每月抽一天掃描全市場（271 次呼叫），"
                           "再與下市公告聯集：只靠掃描找到 296 檔、只靠公告 320 檔、兩者都有 151 檔。"),
    ]
    for cx, (title, body) in zip(xs, notes, strict=True):
        card(s, cx, 3.9, cw, BOTTOM - 3.9, title, body, role="fm", size=14, title_size=16)

    s = content_slide(prs, "TEJ：沒有 API，就把 Excel 變成可排程的資料來源")
    figures.tej_refresh(s, X0, 1.6, BODY_W, 2.2, fs=14, f=f)
    notes = [
        ("繞不掉的 Excel", "唯一能更新資料的是住在 Excel 裡的增益集。程式以 COM 啟動獨立的 Excel、"
                           "自行載入增益集並呼叫同步巨集；卡住時由看門狗結束該行程。"),
        ("不打擾使用者", "增益集更新時會把視窗翻成可見並搶走焦點。把執行緒綁到獨立的 Windows 桌面後，"
                         "使用者桌面上的視窗從 4 個變成 0 個，耗時 12.4 秒對 13.0 秒。"),
        ("從樣板長出活頁簿", "查詢定義不含證券代碼，改寫代碼欄再更新，就得到另一檔的完整歷史。"
                             "2,300 多本由此自動建立；與手動下載的檔逐格比對，230,796 格全部相同。"),
    ]
    for cx, (title, body) in zip(xs, notes, strict=True):
        card(s, cx, 4.05, cw, BOTTOM - 4.05, title, body, role="tej", size=14, title_size=16)

    s = content_slide(prs, "raw → processed：把上游的資料變成查詢契約")
    xs, cw = columns(2, gap=0.3, w=7.7)
    steps = [
        ("1　改名與定型", "max→high、min→low；日期字串轉 DATE、金額轉 BIGINT（成交值會超過 int32）。"
                          "stock_id 刻意保留字串：0050、00679B。"),
        ("2　過濾範圍", "全市場一天回 43,727 檔，其中約 4 萬檔是權證與 TDR。"
                        "依代碼形狀只留普通股與 ETF，共 2,821 檔。"),
        ("3　重新分區", "把「一檔一個檔案」翻成「一天一個檔案」。約 1,200 萬列，"
                        "交給 DuckDB 逐年分塊處理，記憶體只需幾十萬列。"),
        ("4　去重", "每張表先寫下自然鍵。未還原價以全市場日檔為準；"
                    "還原價以 ticker 全歷史為準（原因見第三部分）。"),
    ]
    for i, (title, body) in enumerate(steps):
        card(s, xs[i % 2], 1.6 + (i // 2) * 2.63, cw, 2.45, title, body, role="fm", size=14, title_size=16)
    rx, rw = 8.6, SLIDE_W - MARGIN - 8.6
    box(s, rx, 1.6, rw, BOTTOM - 1.6, fill="tint")
    text(s, rx + 0.25, 1.8, rw - 0.5, 0.4, "為什麼不用 SELECT DISTINCT *", size=16, bold=True, color="ink")
    text(s, rx + 0.25, 2.3, rw - 0.5, 1.1, "上游每次重爬都會把日期欄重新蓋章。沒有鍵的去重會把同一筆事實每爬一次多存一列。",
         size=14)
    text(s, rx + 0.25, 3.45, rw - 0.5, 0.6, "6,871 → 11,520", size=28, bold=True, color="ink")
    text(s, rx + 0.25, 4.1, rw - 0.5, 0.4, "產業表在第二次抓取後的列數（修正前）", size=12, color="sub")
    text(s, rx + 0.25, 4.65, rw - 0.5, 2.0,
         "改以（證券、產業、子產業）為鍵後回到 6,871 列，並在健康檢查裡加上守門規則。"
         "TEJ 側則不去重，直接斷言鍵唯一，違反就讓重建失敗。", size=14)

    s = content_slide(prs, "Schema（FinMind 價格）：三層各長什麼樣")
    lead(s, "raw 保留上游欄名；processed 是唯一該查的一層；view 只做組合與分區裁剪。")
    figures.schema(s, X0, TOP, BODY_W, BOTTOM - TOP, FINMIND_SCHEMA, fs=16, role="fm")

    s = content_slide(prs, "Schema（TEJ 還原價）：35 欄中文表頭 → 40 欄 view")
    lead(s, "同一列只有開高低收是還原價，買賣報價與次日參考價是未還原的原始報價，混用必錯。")
    figures.schema(s, X0, TOP, BODY_W, BOTTOM - TOP, TEJ_SCHEMA, fs=16, role="tej")

    s = content_slide(prs, f"view 層：{views} 個 view，把「怎麼查才對」寫進資料庫")
    rows = [
        ["用途", f"FinMind（{VIEWS['finmind']} view＋3 macro）", f"TEJ（{VIEWS['tej']} view＋3 macro）"],
        ["價格", "v_daily_prices、v_adj_daily_prices、\nv_prices_combined、v_daily_prices_enriched",
         "v_adj_daily_prices（40 欄）、v_securities"],
        ["回測用的證券範圍", "v_securities_all（含已下市）、\nv_delisted_securities、v_stock_info_latest",
         "v_index_constituents、v_index_membership、\nv_index_universe"],
        ["產業與日曆", "v_industry_by_stock、v_trading_calendar、\nv_last_trading_day",
         "v_trading_calendar、v_index_calendar"],
        ["涵蓋率與對帳", "v_daily_coverage、v_missing_trading_days、\nv_thin_trading_days、v_known_empty_days",
         "v_workbook_status、v_security_gaps、\nv_index_price_gaps、v_thin_index_days"],
        ["維運與入口", "v_ingestion_runs；daily_prices_between(lo, hi)", "v_ingestion_runs；index_members_on(指數, 日期)"],
    ]
    table(s, X0, 1.6, [2.3, 4.9, 4.93], rows, size=12, bold_first_col=True, heights=[0.45] + [0.74] * 4 + [0.5])
    text(s, X0, 5.75, BODY_W, 0.9,
         "涵蓋率與對帳本身就是 view：資料健不健康，用一句 SQL 就能問。回測歷史指數時，"
         "用 index_members_on() 取當日成分，而不是把所有曾入選的股票放進每一天。", size=14, color="sub")

    s = content_slide(prs, "如何更新：每日增量、每週全量、漏跑自動補")
    figures.schedule(s, X0, 1.6, 7.7, 3.55, fs=13, f=f)
    box(s, X0, 5.3, 7.7, BOTTOM - 5.3, fill="tint")
    dot(s, X0 + 0.2, 5.52, 0.14, "ink")
    text(s, X0 + 0.45, 5.43, 7.05, 1.3, [
        {"runs": [("現況（2026-10-04）", {"bold": True, "color": "ink"})], "after": 3},
        {"runs": ["FinMind 的 token 自 9 月 19 日降為免費方案，排程失敗、資料停在 9 月 16 日；"
                  "每次失敗都留在 v_ingestion_runs。"]},
    ], size=13)
    rx, rw = 8.6, SLIDE_W - MARGIN - 8.6
    rules = [
        ("每日抓三個集合", "新日期 ∪ 最近 3 天重抓 ∪ 範圍內缺漏。上游收盤後會修正當日成交量，漏跑的日子下次自動補齊。"),
        ("還原價每週全量重抓", "除權息會改寫整段歷史，日期補齊不等於數字正確。"),
        ("寧可停下，不寫半筆", "TEJ 新增的日期沒有收盤價就不存檔並中止；重建先寫暫存目錄，再原子換檔。"),
    ]
    for i, (title, body) in enumerate(rules):
        card(s, rx, 1.6 + i * 1.77, rw, 1.62, title, body, role="ok", size=13, title_size=15)

    s = content_slide(prs, "把「資料正確」做成可以重跑的檢查")
    xs, cw = columns(4)
    quality = [
        (str(tests), f"個自動化測試（FinMind {TESTS['finmind']}、TEJ {TESTS['tej']}）", "ok"),
        (str(checks), f"項唯讀健康檢查（{CHECKS['finmind']}＋{CHECKS['tej']}），排程後自動執行", "ok"),
        ("4 類", "每週對帳：缺漏日、稀薄日、沒有資料的證券、停更的證券", "ok"),
        ("0", "次手改資料：資料只由程式產生，要改資料就改程式再重跑", "ok"),
    ]
    for cx, (value, label, role) in zip(xs, quality, strict=True):
        stat(s, cx, 1.6, cw, 1.95, value, label, role=role, value_size=30, label_size=12)
    rows = [
        ["處理過的上游缺陷（各有對應測試）", "處理方式"],
        ["日期欄是字串「None」（32 列）", "轉成真正的 NULL，否則轉型會直接失敗"],
        ["1999 年以前有週六盤（176 天）", "不寫任何「週末必無交易」的邏輯"],
        ["官方日曆列了實際休市的日子（2026-07-10）", "向 API 確認一次後記錄下來，不再當成缺漏反覆重抓"],
        ["收盤後成交量被修正（0050：39,946,000 → 43,382,378）", "每日重抓最近 3 個交易日"],
        ["產業名稱飄移（創新板／創新版等）", "別名表正規化，原值另存"],
    ]
    table(s, X0, 3.8, [5.9, 6.23], rows, size=13, row_h=0.49)

    # ================================================================ part 3
    divider(prs, "三", "跨來源驗證", "FinMind 與 TEJ 哪裡不同、誰是對的、各自哪裡不可信")

    s = content_slide(prs, "存活者偏誤：兩個來源保留了多少已下市公司")
    lead(s, "回測只用現存公司，報酬會被高估。下圖是 TEJ 上市櫃資料列中，FinMind 也有的比例。")
    figures.coverage(s, X0 - 0.1, TOP, 7.7, BOTTOM - TOP, fs=12, f=f)
    rx, rw = 8.6, SLIDE_W - MARGIN - 8.6
    tiles = [
        (f"{n(f.delisted.with_adjusted)}／{n(f.delisted.delisted_recovered)}",
         f"FinMind 找回的已下市證券中有還原價的檔數（未還原價有 {n(f.delisted.with_unadjusted)} 檔）", "fm"),
        (n(f.tej_stopped), f"TEJ 已停止交易、仍保有完整還原歷史的檔數；FinMind 還原價只涵蓋其中 "
                           f"{n(f.overlap.stopped_in_fm_adjusted)} 檔", "tej"),
        ("2004-02-11", "FinMind 能做無偏誤橫斷面的最早日期（全市場端點的資料起點）", "fm"),
    ]
    for i, (value, label, role) in enumerate(tiles):
        stat(s, rx, TOP + i * 1.6, rw, 1.48, value, label, role=role, value_size=24, label_size=12)

    s = content_slide(prs, "驗證方法：比報酬，不比價格水準")
    points = [
        ("1　為什麼不比水準", f"兩邊都把歷史回溯到最新基準，只要重抓的日期不同，整段水準就差一個常數。"
                              f"逐列比收盤價，只有 {f.level_within_1c:.1f}% 相差在 0.01 元以內——但那不代表有錯。"),
        ("2　改比日報酬", "同一檔、同一天的報酬不受基準影響。再拿未還原報酬當第三把尺："
                          "誰的報酬等於未還原報酬，誰那天就沒有調整。"),
        ("3　用交易制度當檢驗", "台股有漲跌幅限制（2015 年 6 月前 7%，之後 10%）。"
                                "還原後仍超過限制的報酬，不是真實事件，就是沒有調乾淨。"),
    ]
    for i, (title, body) in enumerate(points):
        card(s, X0, 1.6 + i * 1.75, 6.9, 1.58, title, body, role="ok", size=14, title_size=16)
    xs, cw = columns(2, gap=0.3, x=7.8, w=SLIDE_W - MARGIN - 7.8)
    tiles = [
        (f"{n(f.overlap_size.securities)} 檔", f"兩邊共有的證券，交集 {wan(f.overlap_size.overlap_rows)}列"),
        (wan(f.clean.n), "筆上市櫃、且前後兩天都有成交的日報酬"),
        (pct(f.clean.agree_within_10bp_pct, 2), "的日報酬，兩邊相差在 10bp 以內"),
        (n(disagreements), "個交易日相差超過 1%，全部逐筆歸因"),
    ]
    for i, (value, label) in enumerate(tiles):
        stat(s, xs[i % 2], 1.6 + (i // 2) * 2.63, cw, 2.45, value, label, role="ok", value_size=32, label_size=14)

    s = content_slide(prs, f"{n(disagreements)} 個不一致的交易日，每一筆都找到原因")
    text(s, X0, 1.6, 7.3, 0.3, "兩邊日報酬相差超過 1% 的交易日數（上市櫃）", size=13, bold=True, color="ink")
    figures.causes(s, X0 - 0.1, 1.95, 7.5, 3.9, fs=13, f=f)
    text(s, X0, 5.95, 7.3, 0.8,
         f"本專案管線造成的差異：修正前 {f.pipeline_days_before} 天，修正後 {f.cause_days('B')} 天"
         "（都落在最近一次每週重抓之後，下次重抓即對齊）。", size=13, color="sub")
    rx, rw = 8.3, SLIDE_W - MARGIN - 8.3
    explain = [
        ("fm", "FinMind 未還原除權息",
         f"{n(f.cause_days('C'))} 天、{n(f.cause.loc['C', 'securities'])} 檔，幾乎都在 2007 年以前。"),
        ("muted", "無成交日的記法不同",
         f"FinMind 把無成交日記成 0、還原價沿用前一日；TEJ 記參考價，其中 {n(f.no_trade.tej_at_limit)} 天正好落在漲跌停價。"),
        ("fm", "FinMind 多調",
         f"{n(f.cause_days('D'))} 天：未還原價沒有事件，還原價卻跳動，例如 6949 在 2024-03-11 為 −95%。"),
        ("muted", "兩邊都調、幅度不同", f"{n(f.cause_days('E'))} 天，尚待逐筆查證。"),
    ]
    for i, (role, title, body) in enumerate(explain):
        y = 1.6 + i * 1.32
        dot(s, rx, y + 0.08, 0.16, role)
        text(s, rx + 0.3, y, rw - 0.3, 1.25, [
            {"runs": [title], "bold": True, "color": "ink", "size": 16, "after": 3},
            {"runs": [body]},
        ], size=14)

    s = content_slide(prs, "發現一：FinMind 的還原價，早期其實沒有還原")
    lead(s, "每年「未還原價超過漲跌幅」的跳空中，還原後仍然留著的比例。100% 表示完全沒有調整。")
    xs, cw = columns(2, gap=0.4)
    for cx, (market, label) in zip(xs, (("tse", "上市"), ("otc", "上櫃")), strict=True):
        text(s, cx, 1.98, cw, 0.3, label, size=14, bold=True, color="ink")
        figures.residual_share(s, cx - 0.1, 2.25, cw + 0.1, 3.55, market, fs=11, f=f)
    box(s, X0, 5.92, BODY_W, BOTTOM - 5.92, fill="tint")
    text(s, X0 + 0.25, 5.92, BODY_W - 0.5, BOTTOM - 5.92,
         f"上市股到 2002 年、上櫃股到 2007 年，FinMind 的還原價留著全部的除權息跳空；之後兩條線重合。"
         f"共 {n(f.cause_days('C'))} 個交易日、{n(f.cause.loc['C', 'securities'])} 檔——拿這段期間做回測，除權息會被當成暴跌。",
         size=14, anchor="m")

    s = content_slide(prs, "發現二：交叉驗證抓到自己管線的錯誤")
    lead(s, "6669 緯穎在 2026-09-02 除權。修正前，本專案的還原價在前一週出現 +195% 與 −67% 的假報酬。")
    figures.case_6669(s, X0 - 0.1, TOP, 7.6, BOTTOM - TOP, fs=11, f=f)
    rx, rw = 8.35, SLIDE_W - MARGIN - 8.35
    card(s, rx, TOP, rw, 1.95, "原因",
         "去重時讓「全市場日檔」蓋過「ticker 全歷史」。對未還原價這是對的；"
         "但還原價的日檔只是抓取當下那個基準的快照，每週重抓的正確歷史反而被舊快照蓋回去。",
         role="fm", size=13, title_size=15)
    card(s, rx, TOP + 2.08, rw, 1.3, "修正",
         "還原價改為 ticker 優先，日檔只補 ticker 檔尚未涵蓋的日子；新增 4 個測試與 1 條健康檢查。",
         role="ok", size=13, title_size=15)
    xs, cw = columns(2, gap=0.2, x=rx, w=rw)
    stat(s, xs[0], TOP + 3.51, cw, BOTTOM - TOP - 3.51, f"{n(f.stale.securities_affected)} 檔",
         f"{n(f.stale.rows_market_differs)} 列被舊基準污染", role="fm", value_size=20, label_size=11)
    stat(s, xs[1], TOP + 3.51, cw, BOTTOM - TOP - 3.51, "19 → 6 天", "混合基準的交易日數", role="ok",
         value_size=20, label_size=11)

    s = content_slide(prs, "發現三：只有對照第二個來源才看得到的缺陷")
    rows = [
        ["現象", "規模（實測）", "影響"],
        ["無成交日的收盤價記成 0",
         f"FinMind 未還原價 {n(zero_rows)} 列（{100 * zero_rows / f.fm_raw.n_rows:.1f}%），其中 {n(zero_with_volume)} 列仍有成交量",
         "直接算報酬會得到 −100%；查詢時要排除"],
        ["成交量單位不一致", "2000–2003 年有 15–27% 的列，成交量恰為 TEJ 的千分之一", "早期成交量不能直接跨年比較"],
        ["上櫃資料缺漏", "2007 年 1–4 月，上櫃股幾乎整段沒有資料", "這段期間的橫斷面不完整"],
        ["減資後復牌未調整",
         f"{n(f.halts.unadjusted_jump_gt_25pct)} 個停牌後跳空超過 25% 的事件：TEJ 消除 {n(f.halts.tej_removed_jump)} 個，"
         f"FinMind {n(f.halts.fm_removed_jump)} 個",
         f"FinMind 有 {n(f.halts.fm_left_unadjusted)} 個完全沒有調整"],
        ["多餘的調整", f"{n(f.cause_days('D'))} 個交易日：未還原價沒有事件，還原價卻跳動", "屬上游錯誤，已列出清單"],
    ]
    table(s, X0, 1.65, [2.9, 5.5, 3.73], rows, size=13, bold_first_col=True, heights=[0.45] + [0.82] * 5)
    text(s, X0, 6.3, BODY_W, 0.45, "以上都出在 FinMind 一側；TEJ 一側的疑點見「兩個來源各自不能直接相信的地方」。", size=13, color="sub")

    s = content_slide(prs, "誰比較可信：用漲跌幅限制檢驗還原後的報酬")
    text(s, X0, 1.6, 6.4, 0.6, f"同一批 {wan(f.limit.n)}筆日報酬中，超過當時漲跌幅限制的筆數",
         size=13, bold=True, color="ink")
    figures.beyond_limit(s, X0 - 0.1, 2.0, 6.5, BOTTOM - 2.0, fs=12, f=f)
    rx, rw = 7.5, SLIDE_W - MARGIN - 7.5
    gap_all = f.gap_all.loc["fm ticker vs tej"]
    text(s, rx, 1.6, rw, 3.0, bullets([
        [("TEJ 留下的較少。", {"bold": True}),
         f"還原後 TEJ 剩 {n(f.limit.tej_beyond_limit)} 筆、FinMind 剩 {n(f.limit.fm_ticker_beyond_limit)} 筆；"
         "剩下的多是真實事件，例如新掛牌前五日沒有漲跌幅限制。"],
        [("2008 年以後兩者幾乎相同。", {"bold": True}),
         f"{n(f.gap_2008.securities)} 檔中有 {n(f.gap_2008.within_1pct)} 檔，整段累積報酬相差不到 1%。"],
        [("全期間差距來自早期。", {"bold": True}),
         f"{n(gap_all.securities)} 檔中有 {n(gap_all.gap_10_to_50pct + gap_all.gap_over_50pct)} 檔累積報酬相差超過 10%，"
         "來源是 2008 年前沒有還原的除權息。"],
    ], after=10), size=14)
    card(s, rx, 5.0, rw, BOTTOM - 5.0, "結論",
         "算報酬、做回測用 TEJ；要全市場廣度、真實成交價與 2000 年以前的歷史，用 FinMind 的未還原價。",
         role="ok", size=14, title_size=16)

    s = content_slide(prs, "兩個來源各自不能直接相信的地方")
    xs, cw = columns(2, gap=0.35)
    cautions = [
        ("FinMind", "fm", [
            "還原價：上市股 2003 年前、上櫃股 2008 年前沒有還原；已下市公司幾乎沒有還原價",
            "未還原價：無成交日記成 0；2000–2003 年成交量單位不一致；2007 年初上櫃缺漏",
            "2004-02-11 以前只有現存公司；官方交易日曆含實際休市的日子",
            "維度表每次重爬都會改日期戳記，產業分類沒有歷史版本",
        ]),
        ("TEJ", "tej", [
            "同一列混有兩種尺度：只有開高低收是還原價",
            f"{n(f.tej_suspicious.only_tej_breaches)} 個交易日的還原報酬超過漲跌幅，而未還原價與 FinMind 都沒有，原因待查",
            f"內建的報酬率欄位與還原收盤價算出的報酬，有 {n(f.tej_internal.loc['listed', 'differs_gt_1pct'])} 列相差超過 1%",
            f"{n(f.tej_gaps)} 筆存續期內的缺漏日尚未逐檔查證",
            "2000-09-04 以前下市的公司不在內；raw 無法重製",
        ]),
    ]
    for cx, (title, role, items) in zip(xs, cautions, strict=True):
        box(s, cx, 1.6, cw, BOTTOM - 1.6, fill="tint")
        dot(s, cx + 0.28, 1.9, 0.18, role)
        text(s, cx + 0.58, 1.82, cw - 0.9, 0.4, title, size=18, bold=True, color="ink")
        text(s, cx + 0.28, 2.5, cw - 0.56, BOTTOM - 2.65, bullets(items, after=14, bullet_color=role), size=16)

    # ================================================================ part 4
    divider(prs, "四", "限制與收穫", "還沒做到的事，以及這個專案練到的能力")

    s = content_slide(prs, "限制與下一步")
    sides = [
        ("目前的限制", "ink", [
            "FinMind 排程自 2026-09-19 起因 token 降級而停擺，資料停在 9 月 16 日",
            "TEJ 每日排程尚未註冊，指數成分以外的股票最多落後一週",
            "TEJ 一側的異常報酬與報酬欄位差異，還沒有找到解釋",
            "只有日頻價量與指數成分，沒有財報、法人與融資券資料",
        ]),
        ("下一步", "ok", [
            "用未還原價與除權息事件自行計算還原因子，補上 FinMind 缺的早期還原與已下市公司",
            "把跨來源比對變成每週自動執行的檢查，差異直接進對帳 view",
            "逐檔查證 TEJ 的缺漏日與異常報酬",
            "加入財報與籌碼資料，支援因子研究",
        ]),
    ]
    for cx, (title, role, items) in zip(xs, sides, strict=True):
        box(s, cx, 1.6, cw, BOTTOM - 1.6, fill="tint")
        dot(s, cx + 0.28, 1.9, 0.18, role)
        text(s, cx + 0.58, 1.82, cw - 0.9, 0.4, title, size=18, bold=True, color="ink")
        text(s, cx + 0.28, 2.5, cw - 0.56, BOTTOM - 2.65, bullets(items, after=16, bullet_color=role), size=16)

    # -------------------------------------------------------------- closing
    s = prs.slides.add_slide(prs.slide_layouts[LAYOUT_SECTION])
    s.shapes.title.text = "資料先對，模型才有意義"
    s.placeholders[1].text = "這個專案練到的三種能力"
    # A placeholder that overrides its position must state all four values.
    for shape, top, height in ((s.shapes.title, 0.9, 1.0), (s.placeholders[1], 2.0, 0.6)):
        shape.left, shape.top = Inches(MARGIN + 0.2), Inches(top)
        shape.width, shape.height = Inches(BODY_W - 0.4), Inches(height)
    xs, cw = columns(3, gap=0.5, x=MARGIN + 0.2, w=BODY_W - 0.4)
    skills = [
        ("資料工程", "fm", "分層契約、原子寫入與暫存目錄換檔、跨行程限流、以 COM 自動化一個沒有 API 的來源、"
                           "可重跑的排程與自我修復。"),
        ("統計與驗證", "tej", "以報酬而非水準比較兩個來源、用交易制度做合理性檢驗、"
                              "把一萬個差異逐筆歸因、量化存活者偏誤。"),
        ("研究紀律", "ok", "數字一律實測、可以重現；找到自己的錯誤就修正並留下紀錄；"
                           "每個結論都寫明適用範圍與限制。"),
    ]
    for cx, (title, role, body) in zip(xs, skills, strict=True):
        dot(s, cx, 3.37, 0.2, role)
        text(s, cx + 0.34, 3.28, cw - 0.34, 0.45, title, size=20, bold=True, color="white")
        text(s, cx, 3.95, cw, 2.4, body, size=15, color="line", spacing=1.25)
    text(s, MARGIN + 0.2, 6.55, BODY_W - 0.4, 0.35,
         "完整說明見同資料夾的《financial-db 專案說明書》；互動版資料地圖與 Schema 圖的連結在 README.md。",
         size=12, color="muted")

    target = DELIVERABLES / "financial-db-專案簡報.pptx"
    prs.core_properties.title = "financial-db：台股研究級資料倉儲"
    prs.core_properties.subject = "專案簡報"
    prs.save(target)
    print(target, len(prs.slides), "slides")


if __name__ == "__main__":
    build()
