# Operations runbook

## Supervised smoke test

`refresh_workbooks.py` drives Excel and authenticates to TEJ as you. The live
round trip was validated on 2026-09-03: three supervised workbooks followed by
104 remaining active workbooks all refreshed successfully through
`RefreshFile_Wait`. It still consumes licensed TEJ quota and overwrites the raw
layer, so repeat the one-file visible smoke test after add-in, Excel or session
changes.

Run the smoke test on one file:

```powershell
cd "<repo>\pipelines\tej-wizard"
& "python" scripts\refresh_workbooks.py --missing-only --dry-run
& "python" scripts\refresh_workbooks.py --tickers 2330 --visible
```

`--visible` shows the Excel window so you can see what the add-in does. Watch
for three things:

1. **A login dialog.** The add-in holds its own TEJ session. If it has expired,
   a modal login appears and a hidden Excel would sit on it until the watchdog
   killed the process after `addin.timeout_seconds` (600s). Log in through the
   normal TEJ Smart Wizard UI once, then re-run.
2. **A prompt that still appears.** The automation sets `EnableEvents = False`
   while opening precisely to suppress the "auto refresh?" dialog. If one shows
   up anyway it is coming from somewhere else in the add-in, and the macro name
   or the event handling needs adjusting.
3. **The row count moving.** After it finishes, check that the workbook grew:

```powershell
& "python" scripts\rebuild.py
& "python" scripts\verify.py
```

After a code or add-in change, widen out with `--limit 5` before the daily job.
Backups of every workbook selected for that run are written to
`db\tej-wizard\temp\workbook-backup\<timestamp>\` before anything is touched,
and the last two sets are kept.

## Daily operation

```powershell
cd "<repo>\pipelines\tej-wizard"
& "python" scripts\update.py
```

`update.py` runs five stages and stops at the first failure:

1. **Refresh the index constituents** — two workbooks, the current year of
   TWN50 and TM100.
2. **Rebuild the index layer only** — no TEJ, ~35 s. This is what makes stage 3
   plan against *today's* membership rather than yesterday's.
3. **Refresh price workbooks for current index members** — ~150 of the 2,453.
   The other ~1,840 still-trading companies from the 2026-09-28 company-info
   expansion are **not** refreshed daily; only the weekly job moves them.
4. **Rebuild everything.**
5. **Verify.**

Before each workbook is refreshed, the pipeline reads
`db\finmind\processed\expanded-tw-trading-dates\expanded_tw_trading_dates.parquet`
and inserts the missing trading dates directly below the header, with the
identity in column A. It then calls `RefreshFile_Wait`, not the asynchronous
`RefreshFile`, so it waits for `TEJRefresh.exe` to finish. A workbook is saved
only after validation.

**Excel runs on a private Windows desktop**, so no window appears and nothing
steals focus while the job runs. Pass `--visible` to watch it instead.

The two datasets are seeded on different rules:

| | horizon | if TEJ has no data |
|---|---|---|
| Prices | **today** | the workbook is not saved and the run stops |
| Index constituents | **the next trading session** | dates past today are *optional*: the prompt row is dropped and the run succeeds |

The index horizon is one session ahead because a constituent list dated D is
published after D-1's close — on a Friday evening TEJ already has Monday's. A
horizon of today would leave the newest row permanently unfetched.

Price workbooks more than 20 trading sessions behind the modal raw latest date
are treated as inactive and are not seeded, so a security that stopped trading
does not receive thousands of artificial rows.

| Script | What it does | Cost | Touches TEJ? |
|---|---|---|---|
| `refresh_workbooks.py --dataset index` | The current year of each index | 2 files: ~10 s | **yes** |
| `refresh_workbooks.py --scope current-members` | Price workbooks for current index members | ~150 files: ~50 min | **yes** |
| `refresh_workbooks.py --scope all` | Every price workbook | 2,453 files: ~8 h (estimate) | **yes** |
| `list_company_tickers.py` | Writes the eligible company-info ids to `state/company_universe_tickers.txt` | ~5 s | no |
| `create_workbooks.py` | Builds workbooks for index members (or `--tickers-file` ids) that have none | 4–20 s each | **yes** |
| `rebuild.py` | xlsx → Parquet for both datasets, then views | ~16 min | no |
| `rebuild.py --only index` | Constituent layer only | ~35 s | no |
| `build_db.py` | Views only | ~1 s | no |
| `verify.py` | 35 read-only checks | ~3 s | no |
| `update.py` | The daily job: stages 1–5 above | ~55 min | **yes** |
| `weekly_refresh.py` | Full re-pull of everything, then rebuild + verify | ~8 h (estimate) | **yes** |

`rebuild.py`, `build_db.py` and `verify.py` are free and safe to run at any
time. Only the refresh and create scripts reach outside the machine.

## When a security stops trading

A security that has been delisted, merged or suspended returns **nothing** for
every seeded date. On the wire that is indistinguishable from a refresh that
failed, and the pipeline used to treat it as one: it refused to save, and the
whole daily run stopped.

That is what `2867 三商壽` did on 2026-09-05. Its last bar is 2026-08-31 —
FinMind has nothing for it after 2026-08-20 either, so the security genuinely
stopped — and it aborted the run after 107 of 108 workbooks had already been
refreshed, skipping the rebuild and verify stages. It would have done the same
every day for the ~20 sessions the workbook stays inside the active window.

The refresher now separates the two cases:

| What TEJ returned | Interpretation | What happens |
|---|---|---|
| Some seeded dates, not all | A partial refresh | Not saved; the run fails |
| **None**, on a workbook that already holds history | The security has stopped trading | Left untouched, reported as "no new data", run continues |
| None, on **every** workbook in the batch | TEJ is unreachable or the session expired | The run fails |

The last row is the safety net: one workbook returning nothing is a delisting,
all of them returning nothing is an outage, and only the batch can tell them
apart. A workbook that *should* have advanced but did not shows up in
`v_workbook_status` as `is_current = false` with a growing `days_behind`.

## Why there is a weekly job as well

**A back-adjusted history is not append-only.** Every ex-dividend and every
split rewrites the whole series, so a workbook whose *dates* are complete can
still hold a stale adjustment basis. The daily job only touches current index
members; a security that has left both indices stops moving, which means
`v_workbook_status` cannot tell you it went stale — only re-pulling can.

`weekly_refresh.py` re-pulls every workbook without seeding, including the
index-constituent years, because TEJ also restates constituent weights after
the fact and a seeded refresh would only ever add new rows.

## Starting a new index year

The index constituents are one workbook per index per calendar year, and
**nothing creates next year's file automatically**. On the first trading day of
a new year `refresh_workbooks.py --dataset index` stops with

```
ERROR: no 2027 workbook for TM100, TWN50.
```

That is deliberate. Guessing which file to copy is how a workbook ends up
holding two years, which `assert_years_do_not_overlap` then refuses to ingest.
Create each one once, by hand:

1. Open the previous year's workbook in Excel with the TEJ Smart Wizard add-in.
2. Re-run its query for the new year's date range.
3. Save it as `db\tej-wizard\raw\daily-index-constituents\<INDEX>\<YYYY>.xlsx`.
4. `python scripts\rebuild.py --only index` and check `v_index_workbook_status`.

Roughly ten minutes, once a year, and the scheduled job fails loudly until it
is done rather than silently falling behind.

## Adding a security that has no workbook

`v_index_universe.has_price_history` is false for any index member this
database cannot price. When a security joins TWN50 or TM100 for the first time,
that is the state it arrives in.

```powershell
& $python scripts\create_workbooks.py --dry-run       # what is missing
& $python scripts\create_workbooks.py --tickers 1234 --yes
& $python scripts\rebuild.py
```

`create_workbooks.py` copies the workbook with the longest history, rewrites
column A to the new id, clears the data and refreshes. TEJ resolves the bare id
back to `<id> <name>` and trims the date skeleton to the security's real life,
so a 2024 listing built from a 2000-2026 template comes back with only its own
rows. Verified against a workbook downloaded by hand from the add-in:
**230,796 of 230,796 cells identical**.

A code TEJ has no prices for does **not** shrink the skeleton: it comes back as
every template date with every field empty. The build therefore also requires
at least one non-empty `close` before it moves the file into `raw/`
(5513, 8702, 8706 and 8709 — all delisted 2001–2003 — came back this way on
2026-09-28, before the check existed; they were moved to
`temp\test\empty-builds-2026-09-29\`).

## Adding companies from company-info

`raw\company-info\company-info.xlsx` is TEJ's company master (3,555 companies on
2026-09-28). It is the source of the price universe beyond the index members:

```powershell
& $python scripts\list_company_tickers.py        # writes state\company_universe_tickers.txt
& $python scripts\create_workbooks.py --tickers-file "<repo>\db\tej-wizard\state\company_universe_tickers.txt" --dry-run
& $python scripts\create_workbooks.py --tickers-file "<same path>" --yes
& $python scripts\rebuild.py
& $python scripts\verify.py
```

The list keeps a company when it has ever listed on TSE, OTC or 創新板 and was
not delisted before 2000-09-04 (the template's first date). It deliberately
leaves out companies that only ever traded on 興櫃. The id is the part of
`公司簡稱` before the first space — **not** `證期會代碼`, which differs for
2854 寶來證 (`000970`). Existing workbooks are skipped, so re-running after a
company-info update builds only the new listings.

The first full run (2026-09-28, 2,090 workbooks) took about 6.5 hours. One
workbook, 2230 泰茂, hung for the full 600 s timeout and stopped the batch; the
TEJ session was fine and a retry succeeded. When the watchdog kills Excel, the
orphaned `TEJRefresh.exe` keeps `<id>.building.xlsx` locked — stop that
process, delete the `.building.xlsx`, and re-run; built files are skipped.

## Scheduling with Windows Task Scheduler

Two tasks:

| Field | Daily | Weekly |
|---|---|---|
| Name | `TEJ daily update` | `TEJ weekly refresh` |
| Trigger | Daily 18:45 | Sunday 03:00 |
| Program | `...\scripts\update.bat` | `...\scripts\weekly_refresh.bat` |
| Start in | `<repo>\pipelines\tej-wizard` | same |
| Runtime | ~55 min | ~2 h |
| Registered | **no — see below** | yes, 2026-09-05 |

**Both times are offset from the FinMind jobs on purpose.** This pipeline reads
FinMind's `processed/expanded-tw-trading-dates/` to decide which dates to seed,
and FinMind rewrites that directory through a staged swap. FinMind runs daily at
18:30 (seconds) and weekly at Sunday 02:00 (~40 min), so starting at 18:45 and
03:00 keeps the reader clear of the writer and stops two multi-hour jobs from
competing for the same disk.

**Do not use `schtasks /TR` here.** The project path contains a space, and `/TR`
splits the command on it however you quote it — that registered both FinMind
tasks with `Execute = D:\Github` and failed them silently with `0x80070002` for
a week (see [`../../finmind/docs/operations.md`](../../finmind/docs/operations.md)).
Use the scheduler cmdlets, which take the program and its working directory as
separate arguments:

```powershell
$dir = "<repo>\pipelines\tej-wizard"

$action  = New-ScheduledTaskAction -Execute "$dir\scripts\weekly_refresh.bat" `
             -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At "03:00"
$set     = New-ScheduledTaskSettingsSet -StartWhenAvailable `
             -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 15) `
             -ExecutionTimeLimit (New-TimeSpan -Hours 72)
$set.DisallowStartIfOnBatteries = $false
$set.StopIfGoingOnBatteries     = $false
# Interactive is the one place this differs from the FinMind tasks: Excel
# cannot start in Session 0, so "whether user is logged on or not" fails always.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
               -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName "TEJ weekly refresh" -Action $action `
  -Trigger $trigger -Settings $set -Principal $principal -Force
```

Swap `weekly_refresh.bat` / `-Weekly -DaysOfWeek Sunday -At "03:00"` for
`update.bat` / `-Daily -At "18:45"` to register the daily job.

Verify the registration itself, not just the exit code — a split path still
looks correct in the Task Scheduler UI:

```powershell
Get-ScheduledTask -TaskName "TEJ*" | ForEach-Object {
    $_.TaskName; $_.Actions | Format-List Execute, Arguments, WorkingDirectory
    $_.Principal.LogonType
}
```

`Execute` must be the full path to the `.bat` with `Arguments` empty, and
`LogonType` must be `Interactive`.

**The daily task is not registered.** As of 2026-09-05 only `TEJ weekly refresh`
exists; `update.py` has only ever been run by hand. Until the daily task is
created, the weekly job is the only thing that advances the workbooks, and
`v_workbook_status.days_behind` will grow through the week.

### Settings that matter here

- **Run only when user is logged on.** This is not optional and is the one
  place this job differs from the FinMind tasks. Excel cannot start in Session
  0, so "run whether user is logged on or not" makes the task fail every time.
- **Clear "Start the task only if the computer is on AC power"** on a laptop.
- **If the task fails, restart every 15 minutes, up to 3 times.**
- Leave the run-time limit generous: about 55 minutes for the daily job and
  two hours for the weekly one.
- The window isolation means neither job interrupts you, but the weekly one
  still holds an Excel process and a TEJ session for two hours.

A missed run needs no manual catch-up. The next run inserts every missing
trading date and refreshes only active workbooks that are behind.

## Daily checks

```powershell
& "python" scripts\verify.py
```

Read-only, exits non-zero on any failure, so it chains after a scheduled job.

The check that matters most for this pipeline is **whether the refresh actually
landed**. The refresh stage rejects seeded dates with an empty `close`, while
`v_workbook_status` independently catches workbooks that remain behind or no
longer agree with the processed layer:

```sql
-- workbooks that did not move, and are not simply delisted
SELECT stock_id, stock_name, modified_at, last_date, days_behind
FROM v_workbook_status
WHERE NOT is_current
ORDER BY days_behind;
```

461 securities sit permanently behind because they stopped trading (see
`docs/schema.md`) — that count matches `v_securities` exactly. Since the
2026-09-28 expansion, though, **most still-trading companies are also behind
between weekly runs**, because the daily job only refreshes current index
members. Read `days_behind` for index members, and for everything else compare
against the date of the last weekly refresh.

```sql
-- health at a glance
SELECT (SELECT date  FROM v_last_trading_day)      AS latest_day,
       (SELECT count(*) FROM v_securities)         AS securities,
       (SELECT count(*) FROM v_security_gaps)      AS gaps,
       (SELECT count(*) FROM v_thin_trading_days)  AS thin_days;

-- last few runs
SELECT job, status, started_at, duration_seconds, error_count
FROM v_ingestion_runs ORDER BY started_at DESC LIMIT 10;

-- did the parse and the workbooks agree?
SELECT * FROM v_workbook_status WHERE rows_delta <> 0;
```

Logs: `db\tej-wizard\logs\ingestion\<date>.log`,
`db\tej-wizard\logs\processing\<date>.log`, and structured run records in
`db\tej-wizard\logs\runs\<date>_<job>.jsonl`.

## Adding a security by hand

`create_workbooks.py` (above) is the normal route: index members come from
`v_index_universe`, everything else from company-info via
`list_company_tickers.py`. Download by hand only for a security neither covers
(an ETF other than 0050/0051, for example) — the pipeline will keep its workbook
refreshed, but nothing will ever assert that it should exist.

Use the same TEJ Smart Wizard query as the others — `waprcd1`, same 33 fields,
DAY frequency — and save it as
`db\tej-wizard\raw\adj-daily-prices-ticker\<stock_id>.xlsx`. Then:

```powershell
& "python" scripts\rebuild.py
```

The filename must be the stock id: `verify.py` checks that `source_file`
matches `stock_id || '.xlsx'`, and the scan rejects any workbook whose query
does not match the contract, so a file built from a different TEJ table is
caught rather than silently blended in.

## Connecting for exploration

**Always connect read-only.** DuckDB allows a single writer, so an open
read-write handle from DBeaver will block the scheduled job.

DBeaver: create a DuckDB connection to
`<repo>\db\tej-wizard\tej_wizard.duckdb`, then in
*Driver properties* set `duckdb.read_only` to `true`.

```python
import duckdb
con = duckdb.connect(r"<repo>\db\tej-wizard\tej_wizard.duckdb",
                     read_only=True)
con.execute("SELECT * FROM adj_daily_prices_between('2024-01-01','2024-03-31')").df()
```

## Recovering from a bad refresh

The raw workbooks are the source of truth and there is no API to re-fetch them
from, so this is the one failure mode worth rehearsing.

1. The processed layer still holds the last good rebuild — a failed refresh does
   not touch it, and `rebuild.py` swaps directories atomically.
2. Backups are in `db\tej-wizard\temp\workbook-backup\<timestamp>\`. Copy the
   workbooks back over `raw\adj-daily-prices-ticker\` and re-run `rebuild.py`.
3. Only if both are gone does the workbook have to be downloaded from TEJ again.

## Costs

| Operation | Time | Notes |
|---|---|---|
| Parse 2,453 price workbooks | ~15 min | streaming XML, no Excel; whole rebuild 986 s |
| Parse 48 index workbooks | ~35 s | |
| Write 313 + 291 monthly partitions | ~20 s | |
| Build views | ~1 s | |
| Build one workbook from a template | 4–20 s | `create_workbooks.py`, per security; short histories are fastest |
| Refresh one workbook | 10–15 s | whole history re-pulled every time |

Measured on 2026-09-29 after the company-info expansion. Storage: 1.9 GB of
price xlsx (raw), 662 MB of price Parquet (processed), and 268 KiB of DuckDB.
