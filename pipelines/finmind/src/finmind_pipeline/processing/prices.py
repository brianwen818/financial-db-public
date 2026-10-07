"""Raw -> processed transform for the two price datasets.

Output layout is one Parquet file per trading day under
``year=YYYY/month=MM/YYYY-MM-DD.parquet``. That granularity is deliberate:

* the daily job only ever *creates* a file, so it is idempotent and an
  interrupted run cannot damage earlier days;
* every file holds exactly one date, so a trading day is either wholly present
  or wholly absent, which makes reconciliation a set comparison over dates.

Note that a ``date`` predicate alone does not prune files -- the Hive keys are
year and month, and DuckDB cannot infer them from ``date``. Queries should
constrain ``year`` too; the ``daily_prices_between()`` SQL macro does this.

The rebuild is driven by DuckDB rather than pandas. The raw layer is organised
by ticker (3,602 files) while the processed layer is organised by day (~8,100
files), so this is a genuine repartition of roughly 12M rows — well past the
point where materialising a single DataFrame is sensible. Work is chunked one
year at a time, which keeps peak memory at a few hundred thousand rows.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.compute as pc

from finmind_pipeline.processing.schema import (
    PROCESSED_PRICE_SCHEMA,
    RAW_PRICE_SCHEMA,
    records_to_raw_table,
)
from finmind_pipeline.settings import get_settings
from finmind_pipeline.utils.parquet_io import (
    StagedDirectory,
    atomic_write_table,
    day_partition_path,
)

log = logging.getLogger(__name__)

# Columns as they come out of the union query, matching PROCESSED_PRICE_SCHEMA
# minus ingested_at (stamped in Python so every row of a run shares one value).
_SELECT = """
    TRY_CAST(date AS DATE)            AS date,
    stock_id                          AS stock_id,
    CAST(open AS DOUBLE)              AS open,
    CAST("max" AS DOUBLE)             AS high,
    CAST("min" AS DOUBLE)             AS low,
    CAST(close AS DOUBLE)             AS close,
    CAST(spread AS DOUBLE)            AS spread,
    CAST(Trading_Volume AS BIGINT)    AS volume,
    CAST(Trading_money AS BIGINT)     AS turnover_value,
    CAST(Trading_turnover AS BIGINT)  AS transactions
"""


@dataclass
class RebuildStats:
    dates_written: int = 0
    rows_written: int = 0
    rows_deduped: int = 0
    min_date: dt.date | None = None
    max_date: dt.date | None = None
    years: list[int] = field(default_factory=list)


def _glob(path: Path) -> str:
    """DuckDB glob literal. Forward slashes work on Windows and avoid escaping."""
    return str(path).replace("\\", "/")


def _source_rank(prefer: str) -> str:
    """ORDER BY expression that ranks the preferred fetch unit first."""
    if prefer not in ("market", "ticker"):
        raise ValueError(f"dedupe_prefer must be 'market' or 'ticker', not {prefer!r}")
    return f"CASE source WHEN '{prefer}' THEN 2 ELSE 1 END DESC"


def _union_sql(dataset_key: str) -> str:
    """Union raw ticker and market files, keeping one row per (stock_id, date).

    ``source`` records which fetch unit produced the row. Which one wins when
    both hold the same day is ``dedupe_prefer`` in config/datasets.yml, and the
    two price datasets need opposite answers:

    * unadjusted -- the whole-market pull wins. It is the settled end-of-day
      snapshot, whereas a ticker file may have been fetched mid-session.
    * adjusted -- the ticker file wins. A back-adjusted value is only
      meaningful on the basis it was fetched under, and every ex-date moves
      that basis for the whole history. A ticker file is one consistent
      vintage; a market-day file is a snapshot of whatever basis applied when
      that day was pulled. Measured 2026-10-04: with the market pull winning,
      1,046 rows across 319 securities between 2026-08-21 and 2026-09-11 sat on
      an older basis than the history around them (6669 read 7,200 on
      2026-08-28 against 2,413.84 in its own ticker file), leaving a false
      -67% break that the weekly re-fetch could not repair.

    Market rows still fill the days a ticker file does not reach yet.
    """
    settings = get_settings()
    ds = settings.dataset(dataset_key)
    ticker_glob = _glob(settings.path(ds.raw_ticker) / "*.parquet")
    market_glob = _glob(settings.path(ds.raw_market) / "*.parquet")

    parts = []
    if any(settings.path(ds.raw_ticker).glob("*.parquet")):
        parts.append(f"SELECT {_SELECT}, 'ticker' AS source FROM read_parquet('{ticker_glob}')")
    if any(settings.path(ds.raw_market).glob("*.parquet")):
        parts.append(f"SELECT {_SELECT}, 'market' AS source FROM read_parquet('{market_glob}')")
    if not parts:
        raise FileNotFoundError(f"no raw Parquet files for {dataset_key}")

    union = "\nUNION ALL\n".join(parts)
    return f"""
        WITH combined AS (
            {union}
        ),
        ranked AS (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY stock_id, date
                ORDER BY {_source_rank(ds.dedupe_prefer)}
            ) AS rn
            FROM combined
            WHERE date IS NOT NULL AND stock_id IS NOT NULL
        )
        SELECT date, stock_id, open, high, low, close, spread,
               volume, turnover_value, transactions, source
        FROM ranked WHERE rn = 1
    """


def _finalise(table: pa.Table, ingested_at: dt.datetime) -> pa.Table:
    """Attach ingested_at and cast to the processed contract."""
    stamped = table.append_column(
        "ingested_at",
        pa.array([ingested_at] * table.num_rows, type=pa.timestamp("us", tz="UTC")),
    )
    return stamped.select([f.name for f in PROCESSED_PRICE_SCHEMA]).cast(PROCESSED_PRICE_SCHEMA)


def _write_by_date(table: pa.Table, root: Path, ingested_at: dt.datetime) -> tuple[int, int]:
    """Split an Arrow table by date and write one Parquet per day."""
    dates = pc.unique(table.column("date")).to_pylist()
    files = rows = 0
    for d in sorted(x for x in dates if x is not None):
        mask = pc.equal(table.column("date"), pa.scalar(d, type=pa.date32()))
        day = table.filter(mask)
        if day.num_rows == 0:
            continue
        rows += atomic_write_table(_finalise(day, ingested_at), day_partition_path(root, d))
        files += 1
    return files, rows


def rebuild_from_raw(dataset_key: str, staged: bool = True) -> RebuildStats:
    """Regenerate every processed partition for ``dataset_key`` from raw.

    With ``staged=True`` the new dataset is built in a sibling directory and
    swapped in at the end, so the live data stays queryable and complete for
    the whole rebuild. That is the right mode for the weekly adjusted-price
    refresh, which rewrites all ~8,100 partitions.
    """
    settings = get_settings()
    processed_root = settings.path(settings.dataset(dataset_key).processed)
    ingested_at = dt.datetime.now(dt.UTC)
    stats = RebuildStats()

    con = duckdb.connect()
    try:
        con.execute("PRAGMA threads=4")
        con.execute(f"CREATE TEMP VIEW src AS {_union_sql(dataset_key)}")

        raw_rows = con.execute("SELECT count(*) FROM src").fetchone()[0]
        years = [
            r[0]
            for r in con.execute("SELECT DISTINCT year(date) AS y FROM src ORDER BY y").fetchall()
        ]
        if not years:
            raise RuntimeError(f"{dataset_key}: raw data produced zero usable rows")
        stats.years = years
        log.info(
            "%s rebuild: %s deduped rows across %d years (%d..%d)",
            dataset_key,
            f"{raw_rows:,}",
            len(years),
            years[0],
            years[-1],
        )

        def build_into(root: Path) -> None:
            for year in years:
                chunk = con.execute(
                    "SELECT * FROM src WHERE year(date) = ? ORDER BY date, stock_id",
                    [year],
                ).to_arrow_table()
                if chunk.num_rows == 0:
                    continue
                files, rows = _write_by_date(chunk, root, ingested_at)
                stats.dates_written += files
                stats.rows_written += rows
                log.info("  %s %d: %d days, %s rows", dataset_key, year, files, f"{rows:,}")

        if staged:
            with StagedDirectory(processed_root) as stage:
                build_into(stage)
        else:
            build_into(processed_root)

        bounds = con.execute("SELECT min(date), max(date) FROM src").fetchone()
        stats.min_date, stats.max_date = bounds
        stats.rows_deduped = raw_rows
    finally:
        con.close()

    log.info(
        "%s rebuild complete: %s rows over %d days (%s .. %s)",
        dataset_key,
        f"{stats.rows_written:,}",
        stats.dates_written,
        stats.min_date,
        stats.max_date,
    )
    return stats


def process_market_day(
    dataset_key: str, date: dt.date | str, records: list[dict] | None = None
) -> Path | None:
    """Transform one raw whole-market day into its processed partition.

    Used by the daily job and by gap repair. Safe to overwrite the target: the
    whole-market pull is the complete, authoritative picture for that date, so
    it never needs merging with per-ticker rows.
    """
    settings = get_settings()
    ds = settings.dataset(dataset_key)
    if isinstance(date, str):
        date = dt.date.fromisoformat(date)

    if records is not None:
        raw = records_to_raw_table(records, RAW_PRICE_SCHEMA)
    else:
        src = settings.path(ds.raw_market) / f"date={date.isoformat()}.parquet"
        if not src.exists():
            log.warning("%s: no raw market file for %s", dataset_key, date)
            return None
        import pyarrow.parquet as pq

        raw = pq.read_table(src)

    if raw.num_rows == 0:
        return None

    con = duckdb.connect()
    try:
        con.register("raw", raw)
        arrow = con.execute(
            f"SELECT {_SELECT}, 'market' AS source FROM raw "
            "WHERE date IS NOT NULL AND stock_id IS NOT NULL "
            "ORDER BY stock_id"
        ).to_arrow_table()
    finally:
        con.close()

    target = day_partition_path(settings.path(ds.processed), date)
    atomic_write_table(_finalise(arrow, dt.datetime.now(dt.UTC)), target)
    log.info("%s %s: %d rows -> %s", dataset_key, date, arrow.num_rows, target.name)
    return target


def processed_dates(dataset_key: str) -> set[dt.date]:
    """Trading days already present in the processed layer."""
    settings = get_settings()
    root = settings.path(settings.dataset(dataset_key).processed)
    out: set[dt.date] = set()
    for path in root.rglob("*.parquet"):
        try:
            out.add(dt.date.fromisoformat(path.stem))
        except ValueError:
            log.warning("unexpected processed filename: %s", path)
    return out
