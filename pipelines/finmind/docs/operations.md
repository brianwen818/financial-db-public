# Operations runbook

## Scheduling with Windows Task Scheduler

Two tasks. Both wrappers `pushd` to the project directory themselves, so
"Start in" is optional, but setting it is harmless.

### Daily update

| Field | Value |
|---|---|
| Name | `FinMind daily update` |
| Trigger | Weekly, Mon–Sat, 18:30 |
| Action | Start a program |
| Program | `<repo>\pipelines\finmind\scripts\daily_update.bat` |
| Start in | `<repo>\pipelines\finmind` |

18:30 Taipei time is comfortably after the 13:30 close and FinMind's publish.

### Weekly refresh

| Field | Value |
|---|---|
| Name | `FinMind weekly refresh` |
| Trigger | Weekly, Sunday, 02:00 |
| Program | `...\scripts\weekly_refresh.bat` |

Runs about 40 minutes and makes ~3,100 API calls.

### Delisted-universe sweep — not scheduled

`scripts/discover_delisted.py` is deliberately **not** a scheduled task. It
recovers securities that delisted *before* this project's first security-master
snapshot (2026-08-26); that set is historical and does not grow.

Delistings from here on are handled by the two scheduled jobs on their own:

* the daily job lands a new master snapshot, and `load_universe()` unions all
  snapshots, so a security that drops out of the master keeps its place in the
  backfill universe and keeps its history;
* the weekly job re-ingests `TaiwanStockDelisting`, so the name and date arrive
  and `is_delisted` flips in `v_securities_all`;
* `load_current_universe()` — the newest snapshot only — is what the absent and
  stale checks use, so a delisting stops being re-fetched instead of becoming a
  permanent weekly cost.

Re-run the sweep only if the shape rules in `processing/universe.py` change, or
once a year as an audit. It is idempotent: snapshots accumulate in
`raw/discovered-universe/` and are merged, so a re-run can only add.

### Settings worth changing from the defaults

The PowerShell block below applies all but the first of these.

- **Run whether user is logged on or not** — otherwise it silently skips.
  Not applied by the block: it needs stored credentials or an S4U principal.
- **Wake the computer to run this task** — a sleeping machine misses the run.
  Not needed for correctness: the next run self-heals the gap.
- **Start the task only if the computer is on AC power** — clear this on a
  laptop, or the run is refused with `0x800710E0`.
- **Run task as soon as possible after a scheduled start is missed**
  (`-StartWhenAvailable`) — catches up after the machine was off.
- **If the task fails, restart every 15 minutes, up to 3 times.**
- Leave "Stop the task if it runs longer than" at 3 days or disable it; the
  weekly job legitimately runs for tens of minutes.

### Creating them from PowerShell

**Do not use `schtasks /TR` here.** The project path contains a space, and `/TR`
splits the command on it however you quote it: both tasks were registered with
`Execute = D:\Github` and failed silently with `0x80070002` (file not found)
from 2026-08-26 until 2026-09-03. Use the scheduler cmdlets, which take the
program and its working directory as separate arguments:

```powershell
$dir = "<repo>\pipelines\finmind"

foreach ($job in @(
    @{ Name = "FinMind daily update";   Bat = "daily_update.bat";
       Days = @("Monday","Tuesday","Wednesday","Thursday","Friday","Saturday"); At = "18:30" },
    @{ Name = "FinMind weekly refresh"; Bat = "weekly_refresh.bat";
       Days = @("Sunday"); At = "02:00" }
)) {
    $action  = New-ScheduledTaskAction -Execute "$dir\scripts\$($job.Bat)" -WorkingDirectory $dir
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $job.Days -At $job.At
    $set     = New-ScheduledTaskSettingsSet -StartWhenAvailable `
                 -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 15) `
                 -ExecutionTimeLimit (New-TimeSpan -Hours 72)
    $set.DisallowStartIfOnBatteries = $false
    $set.StopIfGoingOnBatteries     = $false
    Register-ScheduledTask -TaskName $job.Name -Action $action -Trigger $trigger `
                           -Settings $set -Force
}
```

`Set-ScheduledTask -TaskName <name> -Action $action -Settings $set` repairs an
existing task without touching its trigger or account.

Verify the registration itself, not just the exit code — a split path still
looks fine in the Task Scheduler UI:

```powershell
Get-ScheduledTask -TaskName "FinMind*" | ForEach-Object {
    $_.TaskName; $_.Actions | Format-List Execute, WorkingDirectory
}
Get-ScheduledTask -TaskName "FinMind*" | Get-ScheduledTaskInfo |
    Select-Object TaskName, LastRunTime, LastTaskResult, NextRunTime
```

`Execute` must be the full path to the `.bat`, with `Arguments` empty.
`LastTaskResult` `0x0` is success; `0x80070002` is the split-path failure above,
and `0x800710E0` means the task was refused — usually the battery settings that
the block above clears.

## Daily checks

```powershell
cd "<repo>\pipelines\finmind"
.\.venv\Scripts\python.exe scripts\verify.py
```

`verify.py` is read-only and exits non-zero on any failure, so it chains after a
scheduled job. It covers scale, dimension invariants, key uniqueness, scope
(no warrants leaked in), coverage gaps, and two golden values taken from real
FinMind responses.

To list the views and their row counts instead, use
`scripts\build_db.py --verify-only`.


```powershell
cd "<repo>\pipelines\finmind"
.\.venv\Scripts\python.exe scripts\build_db.py --verify-only
```

```sql
-- health at a glance
SELECT (SELECT count(*) FROM v_missing_trading_days) AS missing_days,
       (SELECT count(*) FROM v_thin_trading_days)    AS thin_days,
       (SELECT date     FROM v_last_trading_day)     AS latest_day;

-- last few runs
SELECT job, status, started_at, duration_seconds, error_count
FROM v_ingestion_runs ORDER BY started_at DESC LIMIT 10;
```

Logs: `db\finmind\logs\ingestion\<date>.log`,
`db\finmind\logs\processing\<date>.log`, and structured run records in
`db\finmind\logs\runs\<date>_<job>.jsonl`.

## Connecting for exploration

**Always connect read-only.** DuckDB allows a single writer, so an open
read-write handle from DBeaver will block the scheduled job.

DBeaver: create a DuckDB connection to
`<repo>\db\finmind\finmind.duckdb`, then in
*Driver properties* set `duckdb.read_only` to `true`.

Python:

```python
import duckdb
con = duckdb.connect(
    r"<repo>\db\finmind\finmind.duckdb", read_only=True
)
```

CLI: `duckdb -readonly "<repo>\db\finmind\finmind.duckdb"`

## Common situations

### The scheduled job did not run

Nothing to do. The next run detects the missing days and re-fetches them. To
catch up immediately:

```powershell
.\.venv\Scripts\python.exe scripts\daily_update.py
```

### "IO Error: database is locked"

Something holds a read-write handle — usually DBeaver. Close it, or reconnect
read-only. The scheduled jobs hold the file only for a second or two.

### HTTP 402 quota exhausted

The hourly budget is spent. The limiter normally prevents this; a 402 means
something else used the token, or the budget ratio is too high. Wait for the
hour to roll over, then:

```powershell
.\.venv\Scripts\python.exe scripts\backfill.py --resume
```

Lower `rate_budget_ratio` in `config/datasets.yml` if it recurs.

### A backfill was interrupted

```powershell
.\.venv\Scripts\python.exe scripts\backfill.py --resume
```

Progress is checkpointed every 50 tickers in
`db\finmind\state\backfill_checkpoint.json`. Delete that file to force a full
re-fetch.

**`backfill.py` does not rebuild the views**, unlike `daily_update.py` and
`weekly_refresh.py`, which both call `build_db.build()` at the end. Run it
yourself when the backfill finishes, or the warehouse keeps answering from the
pre-backfill Parquet set:

```powershell
.\.venv\Scripts\python.exe scripts\build_db.py
.\.venv\Scripts\python.exe scripts\verify.py
```

### Delisted securities are missing from a backtest universe

`v_delisted_securities` is empty, or `v_securities_all` equals
`v_stock_info_latest`. The discovery sweep has not run:

```powershell
.\.venv\Scripts\python.exe scripts\discover_delisted.py --dry-run
.\.venv\Scripts\python.exe scripts\discover_delisted.py
.\.venv\Scripts\python.exe scripts\backfill.py --universe delisted
.\.venv\Scripts\python.exe scripts\build_db.py
```

The sweep only lands the universe; the backfill is what fetches the prices.
Re-running the sweep later is cheap and safe — snapshots accumulate in
`raw/discovered-universe/` and `load_discovered()` merges them, so a narrower
re-run never shrinks what a wider earlier run found.

Roughly 300 of the 767 recovered codes return no price data at all: they
delisted before 2004-02-11, which is where FinMind's daily prices start. That
is expected, not a failure — the backfill records them as empty and moves on.

### The database looks wrong; rebuild everything

The Parquet layer is authoritative, so nothing here re-fetches from the API:

```powershell
.\.venv\Scripts\python.exe -c "from finmind_pipeline.processing import prices, dimensions; prices.rebuild_from_raw('daily_prices'); prices.rebuild_from_raw('adj_daily_prices'); dimensions.process_all_dimensions()"
.\.venv\Scripts\python.exe scripts\build_db.py
```

Deleting `finmind.duckdb` is always safe — `build_db.py` recreates it.

### Adjusted prices look stale

Expected between weekly runs. Every dividend or split rewrites a ticker's whole
adjusted history, and that is only picked up by the weekly full refresh. Force
one:

```powershell
.\.venv\Scripts\python.exe scripts\weekly_refresh.py --skip-reconcile
```

Check staleness with `ingested_at` on `v_adj_daily_prices`.

### Auditing without changing anything

```powershell
.\.venv\Scripts\python.exe scripts\weekly_refresh.py --dry-run
```

Reports checks A–D (missing days, thin days, absent tickers, stale tickers)
and makes no API calls beyond the security master.

## Cost model

| Job | API calls | Duration |
|---|---|---|
| Daily update | 3 dimensions + 2 datasets × (待補天數 + 3 天修正窗口)；實測 22（2026-09-03，補 9 天） | seconds |
| Weekly refresh | ~3,141 (+1 for the delisting log) | ~40 min |
| Delisted discovery | 272 (271 sampled days + delisting log) | ~4 min |
| Delisted backfill | ~1,530 (767 tickers × 2) | ~10 min |
| Full backfill | ~7,830 (3,914 tickers × 2) | ~85–95 min |

The weekly refresh deliberately does **not** grow with the delisted universe:
`load_universe()` defaults to `include_delisted=False`, because a delisted
security's history is immutable and its last bar is years old — including it
would mean 767 pointless calls a week and a permanent list of false staleness.

### How the limiter is sized

Two separate numbers, and conflating them is a real hazard:

- **rate** — sustained refill, 90% of the cap (5,400 of 6,000/hr)
- **burst** — bucket capacity, the *headroom* (600 = 6,000 − 5,400)

A token bucket refills while it is being drained, so the most it can admit over
a window `W` is `burst + rate × W`. Setting `burst = rate` — which looks
natural — therefore allows **twice** the intended hourly budget.

That is not hypothetical. The first full backfill here ran with
`burst = rate = 5,400`, emptied the bucket in about 20 minutes, kept drawing at
the refill rate, and hit HTTP 402 at 41 minutes with the provider reporting
`6002/6000` used. Sizing burst as the headroom bounds any rolling hour at
exactly 5,400 + 600 = 6,000.

Because a backfill now runs at the sustained rate throughout, expect the full
~6,280 calls to take about 70–80 minutes rather than finishing in a burst.

### If a 402 happens anyway

The client no longer dies on it. Each rejection drains the local bucket (the
provider's accounting wins) and retries after `quota_backoff_seconds`, giving
roughly 35 minutes of tolerance before the run gives up. Tune with
`quota_backoff_seconds` and `quota_retries` in `config/datasets.yml`.

## Adding a dataset

1. Add an entry to `config/datasets.yml`.
2. Add raw and processed schemas to `processing/schema.py`.
3. Write `ingestion/<name>.py` (follow `tw_industry.py` for a simple one).
4. Write the transform in `processing/`.
5. Add a view in `sql/views/` with a numeric prefix that orders after its
   dependencies.
6. Add tests, then `scripts/build_db.py`.
