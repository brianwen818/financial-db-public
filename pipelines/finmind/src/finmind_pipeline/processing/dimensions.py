"""Raw -> processed transforms for the three dimension datasets.

All three are small enough to handle in a single Arrow table, unlike the price
datasets which need DuckDB-driven chunking.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from finmind_pipeline.processing.schema import (
    PROCESSED_DELISTED_SECURITY_SCHEMA,
    PROCESSED_DELISTING_SCHEMA,
    PROCESSED_INDUSTRY_SCHEMA,
    PROCESSED_STOCK_INFO_SCHEMA,
    PROCESSED_TRADING_DATE_SCHEMA,
    parse_date,
)
from finmind_pipeline.settings import get_settings
from finmind_pipeline.utils.parquet_io import atomic_write_table

log = logging.getLogger(__name__)


def _glob(path: Path) -> str:
    return str(path).replace("\\", "/")


def _read_all_snapshots(raw_dir: Path) -> pa.Table:
    files = sorted(raw_dir.glob("*.parquet"))
    if not files:
        raise FileNotFoundError(f"no raw snapshots in {raw_dir}")
    return pa.concat_tables([pq.read_table(f) for f in files], promote_options="default")


# ---------------------------------------------------------------------------
# TaiwanStockInfo
# ---------------------------------------------------------------------------


def process_stock_info() -> Path:
    """Build the security master.

    Kept as the full snapshot history rather than a current-state table: the
    same stock legitimately appears under several ``industry_category`` values
    across scrape dates, and collapsing that would throw away the only record
    of when a reclassification happened. ``v_stock_info_latest`` in SQL picks
    the current row when that is what a query wants.

    **History means content changes, not re-crawls.** Upstream re-stamps most
    rows with the crawl date on every pull -- the 2026-09-03 snapshot brought
    3,317 rows of which 3,305 were byte-identical to something already
    recorded and only 12 were real. Deduping on the whole row (``date``
    included) therefore added ~3,300 rows per run and buried the genuine
    reclassifications it exists to record. So rows collapse on *content* and
    ``snapshot_date`` becomes ``min()`` -- the date this exact content was
    first seen. The per-crawl stamps stay in ``raw/tw-stock-info/``.

    Two source quirks handled here:

    * ``date`` arrives as the literal string ``"None"`` for the 32 index
      pseudo-tickers. It becomes a real NULL ``snapshot_date``.
    * ``industry_category`` has drifted over time (``創新板股票`` vs the
      upstream typo ``創新版股票``, and six ``X``/``X類``/``X業`` pairs), so a
      normalised column is added alongside the verbatim one.
    """
    settings = get_settings()
    ds = settings.dataset("tw_stock_info")
    raw = _read_all_snapshots(settings.path(ds.raw))

    category = raw.column("industry_category").to_pylist()
    rows = {
        "snapshot_date": [parse_date(d) for d in raw.column("date").to_pylist()],
        "stock_id": raw.column("stock_id").to_pylist(),
        "stock_name": raw.column("stock_name").to_pylist(),
        "industry_category": category,
        "industry_category_norm": [settings.normalise_industry(c) for c in category],
        "type": raw.column("type").to_pylist(),
        "is_index": [settings.is_index_category(c) for c in category],
    }
    table = pa.table(rows, schema=PROCESSED_STOCK_INFO_SCHEMA)

    # Snapshots overlap heavily across days; keep one row per distinct fact,
    # stamped with the first date that fact was observed.
    con = duckdb.connect()
    try:
        con.register("t", table)
        table = con.execute(
            "SELECT min(snapshot_date) AS snapshot_date, stock_id, stock_name, "
            "industry_category, industry_category_norm, type, is_index "
            "FROM t "
            "GROUP BY stock_id, stock_name, industry_category, industry_category_norm, "
            "type, is_index "
            "ORDER BY stock_id, snapshot_date NULLS FIRST"
        ).to_arrow_table()
    finally:
        con.close()

    target = settings.path(ds.processed) / "tw_stock_info.parquet"
    atomic_write_table(table.cast(PROCESSED_STOCK_INFO_SCHEMA), target)

    nulls = sum(1 for v in table.column("snapshot_date").to_pylist() if v is None)
    idx = sum(1 for v in table.column("is_index").to_pylist() if v)
    log.info(
        "tw_stock_info: %d rows, %d distinct stock_id, %d NULL snapshot_date, %d index rows -> %s",
        table.num_rows,
        len(set(table.column("stock_id").to_pylist())),
        nulls,
        idx,
        target.name,
    )
    return target


# ---------------------------------------------------------------------------
# TaiwanStockIndustryChain
# ---------------------------------------------------------------------------


def process_industry() -> Path:
    """Build the industry-chain mapping.

    Many-to-many by nature: the key is ``(stock_id, industry, sub_industry)``,
    not ``stock_id`` and not ``(stock_id, date)``.

    Upstream's ``date`` is a per-stock *scrape stamp*, not an effective date --
    FinMind re-stamps a row whenever it re-crawls it, with the mapping itself
    unchanged. Snapshots are unioned here, so carrying that stamp into the
    dedupe key would emit one row per stamp and break the natural key: the
    second-ever ingest (2026-09-03) turned 6,871 facts into 11,520 rows, and
    every later run would have added ~4,600 more. So the rows are collapsed on
    the natural key and ``snapshot_date`` becomes ``max()`` -- the date this
    mapping was last seen upstream. The individual stamps stay in
    ``raw/tw-industry/``.
    """
    settings = get_settings()
    ds = settings.dataset("tw_industry")
    raw = _read_all_snapshots(settings.path(ds.raw))

    table = pa.table(
        {
            "stock_id": raw.column("stock_id").to_pylist(),
            "industry": raw.column("industry").to_pylist(),
            "sub_industry": raw.column("sub_industry").to_pylist(),
            "snapshot_date": [parse_date(d) for d in raw.column("date").to_pylist()],
        },
        schema=PROCESSED_INDUSTRY_SCHEMA,
    )

    con = duckdb.connect()
    try:
        con.register("t", table)
        table = con.execute(
            "SELECT stock_id, industry, sub_industry, max(snapshot_date) AS snapshot_date "
            "FROM t GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"
        ).to_arrow_table()
    finally:
        con.close()

    target = settings.path(ds.processed) / "tw_industry.parquet"
    atomic_write_table(table.cast(PROCESSED_INDUSTRY_SCHEMA), target)
    log.info(
        "tw_industry: %d rows, %d distinct stock_id -> %s",
        table.num_rows,
        len(set(table.column("stock_id").to_pylist())),
        target.name,
    )
    return target


# ---------------------------------------------------------------------------
# TaiwanStockDelisting
# ---------------------------------------------------------------------------


def process_delisting() -> Path | None:
    """Build the delisting log: one row per (stock_id, name, delisted_date).

    Returns None when the dataset has never been ingested, so that an existing
    installation keeps building its other dimensions instead of failing on a
    directory that is not there yet.
    """
    settings = get_settings()
    ds = settings.dataset("tw_delisting")
    raw_dir = settings.path(ds.raw)
    if not any(raw_dir.glob("*.parquet")):
        log.info("tw_delisting: no raw snapshots yet, skipping")
        return None

    raw = _read_all_snapshots(raw_dir)
    table = pa.table(
        {
            "stock_id": raw.column("stock_id").to_pylist(),
            "stock_name": raw.column("stock_name").to_pylist(),
            "delisted_date": [parse_date(d) for d in raw.column("date").to_pylist()],
        },
        schema=PROCESSED_DELISTING_SCHEMA,
    )

    con = duckdb.connect()
    try:
        con.register("t", table)
        table = con.execute(
            "SELECT DISTINCT * FROM t WHERE stock_id IS NOT NULL ORDER BY delisted_date, stock_id"
        ).to_arrow_table()
    finally:
        con.close()

    target = settings.path(ds.processed) / "tw_delisting.parquet"
    atomic_write_table(table.cast(PROCESSED_DELISTING_SCHEMA), target)
    log.info(
        "tw_delisting: %d rows, %d distinct stock_id -> %s",
        table.num_rows,
        len(set(table.column("stock_id").to_pylist())),
        target.name,
    )
    return target


# ---------------------------------------------------------------------------
# delisted securities — the universe the security master cannot see
# ---------------------------------------------------------------------------


def process_delisted_securities() -> Path:
    """Combine the discovery sweep and the delisting log into one table.

    Membership is ``(discovered | delisting) - master``, restricted to in-scope
    shapes. Two sources are needed because neither is complete on its own: the
    sweep misses securities that never traded on a sampled day (``1204`` traded
    for 145 days in 2005), and the delisting log misses two thirds of what the
    sweep finds, recent delistings included.

    ``stock_name`` and ``delisted_date`` stay NULL for codes only the sweep
    found. There is no upstream source of a name for those, and inventing one
    would be worse than admitting the gap.

    Always writes, even with zero rows: ``v_daily_prices_enriched`` joins this
    table, so its absence would break a view that has nothing to do with
    delisting.
    """
    from finmind_pipeline.ingestion import universe_discovery
    from finmind_pipeline.processing.universe import IN_SCOPE_SHAPES, classify_stock_id

    settings = get_settings()
    ds = settings.dataset("discovered_universe")

    discovered = universe_discovery.load_discovered()
    delisting_path = settings.path(settings.dataset("tw_delisting").processed) / (
        "tw_delisting.parquet"
    )
    delisting: dict[str, dict] = {}
    if delisting_path.exists():
        for row in pq.read_table(delisting_path).to_pylist():
            prev = delisting.get(row["stock_id"])
            # A code can delist more than once (relisted, then delisted again);
            # the most recent event is the one that describes its final state.
            if prev is None or (
                row["delisted_date"]
                and prev["delisted_date"]
                and row["delisted_date"] > prev["delisted_date"]
            ):
                delisting[row["stock_id"]] = row

    if not discovered and not delisting:
        log.info("delisted securities: no discovery sweep or delisting log yet, writing empty")

    # Membership is decided against the *current* snapshot, not the cumulative
    # log. The cumulative log keeps a security forever, so a delisting from
    # here on would never be recognised as one; the newest snapshot is the only
    # thing that says what is listed today.
    #
    # This means "delisted" is defined as "absent from the current security
    # master". A code FinMind wrongly keeps listing after it delisted — 9915 is
    # one, gone since 2008 and still in TaiwanStockInfo — reads as listed here.
    # That is upstream's error, and inheriting it beats guessing.
    from finmind_pipeline.ingestion import tw_stock_info

    try:
        master = tw_stock_info.load_current_universe(include_index=True)
    except FileNotFoundError:
        master = set()

    rows = []
    for stock_id in sorted(set(discovered) | set(delisting)):
        if stock_id in master:
            continue
        shape = classify_stock_id(stock_id)
        if shape not in IN_SCOPE_SHAPES:
            continue
        seen = discovered.get(stock_id)
        gone = delisting.get(stock_id)
        rows.append(
            {
                "stock_id": stock_id,
                "stock_name": gone["stock_name"] if gone else None,
                "delisted_date": gone["delisted_date"] if gone else None,
                "shape": shape,
                "first_seen": seen["first_seen"] if seen else None,
                "last_seen": seen["last_seen"] if seen else None,
                "source": "both" if (seen and gone) else ("discovered" if seen else "delisting"),
            }
        )

    table = pa.Table.from_pylist(rows, schema=PROCESSED_DELISTED_SECURITY_SCHEMA)
    target = settings.path(ds.processed) / "delisted_securities.parquet"
    atomic_write_table(table, target)

    sources: dict[str, int] = {}
    for s in table.column("source").to_pylist():
        sources[s] = sources.get(s, 0) + 1
    named = sum(1 for n in table.column("stock_name").to_pylist() if n)
    log.info(
        "delisted securities: %d codes (%s), %d with a name -> %s",
        table.num_rows,
        ", ".join(f"{k}={v}" for k, v in sorted(sources.items())),
        named,
        target.name,
    )
    return target


# ---------------------------------------------------------------------------
# expanded trading calendar
# ---------------------------------------------------------------------------


def process_expanded_trading_dates() -> Path:
    """Union the official calendar with every date observed in the price data.

    ``TaiwanStockTradingDate`` starts at 1999-01-05, but price history reaches
    back to 1994-10-01. The derived dates fill that gap — and they include
    **Saturdays**, because the exchange traded on Saturdays until 1998. Any
    downstream weekday-only assumption is therefore wrong.
    """
    settings = get_settings()
    ds = settings.dataset("tw_trading_dates")
    official_glob = _glob(settings.path(ds.raw) / "*.parquet")

    price_globs = [
        _glob(settings.path(settings.dataset(k).processed) / "year=*/month=*/*.parquet")
        for k in ("daily_prices", "adj_daily_prices")
        if any(settings.path(settings.dataset(k).processed).rglob("*.parquet"))
    ]

    con = duckdb.connect()
    try:
        derived = "\nUNION\n".join(
            f"SELECT DISTINCT date FROM read_parquet('{g}', hive_partitioning=1)"
            for g in price_globs
        )
        derived_cte = derived if derived else "SELECT NULL::DATE AS date WHERE false"
        table = con.execute(
            f"""
            WITH official AS (
                SELECT DISTINCT TRY_CAST(date AS DATE) AS date
                FROM read_parquet('{official_glob}')
            ),
            derived AS ({derived_cte}),
            all_dates AS (
                SELECT date FROM official
                UNION
                SELECT date FROM derived
            )
            SELECT a.date,
                   CASE WHEN o.date IS NOT NULL THEN 'finmind' ELSE 'derived' END AS source
            FROM all_dates a
            LEFT JOIN official o USING (date)
            WHERE a.date IS NOT NULL
            ORDER BY a.date
            """
        ).to_arrow_table()
    finally:
        con.close()

    target = settings.path(ds.processed) / "expanded_tw_trading_dates.parquet"
    atomic_write_table(table.cast(PROCESSED_TRADING_DATE_SCHEMA), target)

    dates = table.column("date").to_pylist()
    sources = table.column("source").to_pylist()
    n_derived = sum(1 for s in sources if s == "derived")
    saturdays = sum(
        1 for d, s in zip(dates, sources, strict=True) if s == "derived" and d.weekday() == 5
    )
    log.info(
        "expanded calendar: %d dates (%s .. %s), %d derived of which %d Saturdays -> %s",
        len(dates),
        min(dates) if dates else None,
        max(dates) if dates else None,
        n_derived,
        saturdays,
        target.name,
    )
    return target


def process_all_dimensions() -> dict[str, Path]:
    from finmind_pipeline.processing import empty_dates

    empty_dates.ensure_exists()
    built: dict[str, Path] = {
        "tw_stock_info": process_stock_info(),
        "tw_industry": process_industry(),
        "expanded_trading_dates": process_expanded_trading_dates(),
    }
    # Order matters: the delisted-security list reads the delisting log this
    # step writes. The log itself is skipped until the dataset is first
    # ingested; the list is always written, empty if it has no sources.
    delisting = process_delisting()
    if delisting is not None:
        built["tw_delisting"] = delisting
    built["delisted_securities"] = process_delisted_securities()
    return built


def latest_trading_day(today: dt.date | None = None) -> dt.date | None:
    """Most recent calendar day that is not in the future."""
    from finmind_pipeline.utils.trading_calendar import (
        last_closed_trading_day,
        load_calendar,
    )

    settings = get_settings()
    path = (
        settings.path(settings.dataset("tw_trading_dates").processed)
        / "expanded_tw_trading_dates.parquet"
    )
    return last_closed_trading_day(load_calendar(path), today)
