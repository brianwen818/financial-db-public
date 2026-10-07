"""Draw every figure on its own slide and export it as a PNG for the report.

The slides are small (8.6 x 4.4 in) so that text set at the report's sizes is
still legible once the image is scaled to the page width.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops

import figures
from deck_kit import blank_slide, new_presentation, text
from facts import OUT, Facts
from office_render import pptx_to_png

W, H = 8.6, 4.4
PAD = 0.1
FIG_DIR = OUT / "figures"

FINMIND_SCHEMA = [
    ("raw", "API 回什麼就存什麼", [
        "date（字串）", "stock_id", "open / max / min / close", "spread", "Trading_Volume", "Trading_money",
        "Trading_turnover",
    ]),
    ("processed", "查詢契約：定型、改名、去重", [
        "date（DATE）・stock_id　← 鍵", "open / high / low / close", "spread", "volume", "turnover_value",
        "transactions", "source・ingested_at", "year / month　← 分區鍵",
    ]),
    ("view", "v_daily_prices 等 19 個 view", [
        "v_daily_prices：同 processed 14 欄", "v_prices_combined：加上還原價與還原因子",
        "v_daily_prices_enriched：加上名稱、產業、下市日", "*_between(lo, hi)：自動裁剪分區",
    ]),
]

TEJ_SCHEMA = [
    ("raw", "xlsx 35 欄，中文表頭逐字驗證", [
        "證券代碼（代碼＋名稱同格）", "年月日（Excel 序號）", "開盤價 / 最高價 / 最低價 / 收盤價",
        "成交量(千股)・成交值(千元)", "本益比・股價淨值比・殖利率", "最後揭示買價 / 賣價", "注意・處置・全額交割旗標",
    ]),
    ("processed", "38 欄：英文欄名、真正的型別", [
        "stock_id・date　← 鍵（斷言唯一）", "stock_name（由代碼欄切出）", "open / high / low / close（還原）",
        "bid / offer / next_ref_price（未還原）", "volume_k_shares・turnover_k", "pe_* / pbr_* / div_yield_*",
        "source_file・ingested_at",
    ]),
    ("view", "v_adj_daily_prices 等 20 個 view", [
        "價格主表 40 欄（＋year / month）", "v_index_constituents：每日指數成分", "index_members_on(指數, 日期)",
        "v_workbook_status：活頁簿對帳", "v_security_gaps：存續期內缺漏日",
    ]),
]

FIGURES = [
    ("architecture", lambda s, f: figures.architecture(s, PAD, PAD, W - 2 * PAD, 3.3, fs=10.5, f=f)),
    ("finmind_order", lambda s, f: figures.finmind_order(s, PAD, PAD, W - 2 * PAD, 1.5, fs=10, f=f)),
    ("tej_refresh", lambda s, f: figures.tej_refresh(s, PAD, PAD, W - 2 * PAD, 1.7, fs=10, f=f)),
    ("schedule", lambda s, f: figures.schedule(s, PAD, PAD, W - 2 * PAD, 2.9, fs=10, f=f)),
    ("schema_finmind", lambda s, f: figures.schema(s, PAD, PAD, W - 2 * PAD, 2.6, FINMIND_SCHEMA, fs=10, role="fm")),
    ("schema_tej", lambda s, f: figures.schema(s, PAD, PAD, W - 2 * PAD, 2.6, TEJ_SCHEMA, fs=10, role="tej")),
    ("rows_per_year", lambda s, f: figures.rows_per_year(s, PAD, PAD, W - 2 * PAD, 3.6, fs=10, f=f)),
    ("coverage", lambda s, f: figures.coverage(s, PAD, PAD, W - 2 * PAD, 3.6, fs=10, f=f)),
    ("residual_share", lambda s, f: _residual_pair(s, f)),
    ("causes", lambda s, f: figures.causes(s, PAD, PAD, W - 2 * PAD, 2.6, fs=10, f=f)),
    ("beyond_limit", lambda s, f: figures.beyond_limit(s, PAD + 1.2, PAD, W - 2 * PAD - 2.4, 3.4, fs=10, f=f)),
    ("case_6669", lambda s, f: figures.case_6669(s, PAD, PAD, W - 2 * PAD, 3.8, fs=10, f=f)),
    ("zero_price", lambda s, f: figures.zero_price(s, PAD, PAD, W - 2 * PAD, 3.2, fs=10, f=f)),
]


def _residual_pair(slide, f):
    half = (W - 2 * PAD - 0.3) / 2
    for i, (market, label) in enumerate((("tse", "上市"), ("otc", "上櫃"))):
        x = PAD + i * (half + 0.3)
        text(slide, x, PAD, half, 0.25, label, size=10, bold=True, color="ink")
        figures.residual_share(slide, x, PAD + 0.25, half, 3.4, market, fs=9, f=f)


def trim(path: Path, margin: int = 16) -> None:
    """Crop the white border left around a figure smaller than its slide."""
    image = Image.open(path).convert("RGB")
    diff = ImageChops.difference(image, Image.new("RGB", image.size, (255, 255, 255)))
    bbox = diff.getbbox()
    if bbox:
        left, top, right, bottom = bbox
        image.crop((max(0, left - margin), max(0, top - margin), min(image.width, right + margin),
                    min(image.height, bottom + margin))).save(path)


def main() -> None:
    facts = Facts()
    prs = new_presentation(W, H)
    for _, draw in FIGURES:
        draw(blank_slide(prs), facts)
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    deck = FIG_DIR / "_figures.pptx"
    prs.save(deck)
    for (name, _), png in zip(FIGURES, pptx_to_png(deck, FIG_DIR, width=2580, prefix="_fig"), strict=True):
        target = FIG_DIR / f"{name}.png"
        target.unlink(missing_ok=True)
        png.rename(target)
        trim(target)
        print(target)


if __name__ == "__main__":
    main()
