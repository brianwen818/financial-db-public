"""Refresh TEJ workbooks by driving Excel headlessly.

There is no HTTP API here. The only thing that can repopulate one of these
workbooks is the TEJ Smart Wizard add-in, and the add-in lives inside Excel. So
"update without opening Excel" means Excel runs invisibly, in its own process,
driven over COM — nobody opens anything by hand, but a hidden Excel is still
doing the work. There is no way around that short of reimplementing TEJ's
protocol.

What the automation has to get right, and why:

**A separate Excel process.** ``DispatchEx`` always starts a new instance
rather than attaching to one the user already has open. Attaching would mean
changing ``EnableEvents`` and ``DisplayAlerts`` underneath a live session and
closing workbooks the user is editing.

**The add-in must be loaded explicitly.** Excel started by COM automation skips
XLSTART and does not load the add-ins listed under
``HKCU\\...\\Office\\16.0\\Excel\\Options``. The interactive Excel the user
knows loads ``excel03menu.xla`` from there; this one has to open it by hand or
``Application.Run`` finds no such macro.

**Events off while opening.** The add-in installs an application-level event
sink (``AppClass`` in its VBA project) which is what raises the "auto refresh?"
prompt on open. ``DisplayAlerts = False`` does not suppress a VBA ``MsgBox``,
but ``EnableEvents = False`` stops the handler running at all. The refresh is
then triggered deliberately rather than by a dialog nobody is there to answer.

**A watchdog on the process.** A synchronous COM call cannot be interrupted, so
if the add-in blocks — an expired TEJ session putting up a modal login is the
likely cause — the only way out is to terminate the Excel process. The watchdog
does that and the run is reported as a timeout rather than hanging for ever.

**A private desktop.** ``Application.Visible = False`` is not enough: the add-in
makes Excel's window visible during a refresh, and ``TejRefresh.exe`` puts its
own progress dialog dead centre on screen. Measured 2026-09-05, one refresh put
four windows on the user's desktop and stole focus. ``PrivateDesktop`` moves the
whole tree onto a desktop nobody is looking at, at no cost in time. See that
class for why switching this thread is sufficient.

The refresh macro is ``RefreshFile_Wait``, exported by ``Module1`` of
``excel03menu.xla``.  The menu's ``RefreshFile`` wrapper launches
``TEJRefresh.exe`` asynchronously, which is unsafe for automation because it
returns before the workbook has been populated.  The ``_Wait`` variant runs
the same Book refresh and blocks until that process exits.  It acts on the
active workbook and takes no arguments.
"""

from __future__ import annotations

import datetime as dt
import gc
import logging
import os
import shutil
import sys
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from tej_pipeline.settings import AddinConfig
from tej_pipeline.processing import schema as S
from tej_pipeline.utils.fsutil import replace_with_retry

log = logging.getLogger(__name__)

# Excel constants (xlcalculation / msoAutomationSecurity) used without the
# type library, which COM late binding does not give us.
_XL_CALC_MANUAL = -4135
_MSO_SECURITY_LOW = 1  # msoAutomationSecurityLow: enable macros without prompting


@dataclass(frozen=True)
class SeedContract:
    """How to tell whether TEJ answered a seeded prompt row.

    A seeded row has only columns A and B filled. Which further column proves
    TEJ populated it, and how many rows one seeded date can produce, differ
    between the two datasets -- everything else about seeding does not.
    """

    #: Index of the proof column inside the B..F window (B=0 .. F=4).
    populated_offset: int
    #: Upper bound on rows one seeded date can expand into.
    max_rows_per_date: int


#: waprcd1: one row per date, proved by a non-empty `close` in column F.
PRICE_SEED = SeedContract(populated_offset=4, max_rows_per_date=1)

#: widxs: one seeded date becomes a whole constituent set, proved by a
#: non-empty `成份股` in column C. Measured maxima are 51 (TWN50) and 102
#: (TM100); 120 leaves room for an index growing without this constant moving.
INDEX_SEED = SeedContract(populated_offset=1, max_rows_per_date=120)


@dataclass
class RefreshResult:
    stock_id: str
    path: Path
    ok: bool
    seconds: float
    message: str = ""
    bytes_before: int = 0
    bytes_after: int = 0
    seeded_dates: tuple[dt.date, ...] = ()
    latest_date_after: dt.date | None = None
    #: TEJ returned nothing at all for the seeded dates on a workbook that
    #: already held data -- the signature of a security that has stopped
    #: trading, not of a failed refresh. The file is left untouched.
    no_new_data: bool = False
    #: What the run log calls this workbook. For a price workbook that is the
    #: stock id; for an index-year workbook it is "TWN50/2026.xlsx", because
    #: the bare filename repeats across indices.
    label: str = ""

    @property
    def changed(self) -> bool:
        return self.ok and self.bytes_after != self.bytes_before


class ExcelUnavailableError(RuntimeError):
    """Excel or pywin32 is not usable on this machine."""


def _excel_pid(app) -> int | None:
    """Resolve the PID behind an Excel COM object, for the watchdog."""
    try:
        import win32process

        _thread_id, pid = win32process.GetWindowThreadProcessId(app.Hwnd)
        return int(pid)
    except Exception:  # noqa: BLE001 - diagnostics only; never fail a run for this
        return None


def _column_values(value2) -> tuple:
    """Flatten a one-column ``Range.Value2`` into a tuple of cell values.

    Excel returns a 2-D tuple for a multi-cell range but a bare scalar for a
    single cell, so a security with exactly one trading day would otherwise
    iterate over the characters of its own id.
    """
    if value2 is None:
        return ()
    if not isinstance(value2, tuple):
        return (value2,)
    return tuple(row[0] if isinstance(row, tuple) else row for row in value2)


def _kill(pid: int) -> None:
    import subprocess

    subprocess.run(
        ["taskkill", "/F", "/PID", str(pid)],
        capture_output=True,
        check=False,
    )


class PrivateDesktop:
    """A Windows desktop the user never sees, for Excel to run on.

    Without this a refresh puts windows on the user's screen even though
    ``Application.Visible`` is False. Measured 2026-09-05 by enumerating the
    Default desktop during one refresh: four appear -- ``TejRefresh.exe``'s
    WinForms progress dialog (twice, 480x128 dead centre), and Excel's own
    ``XLMAIN`` window, which the add-in makes visible despite the setting. They
    steal focus mid-refresh, which for a 2-hour scheduled job is the difference
    between "running in the background" and "unusable machine".

    ``DispatchEx`` starts Excel on the *calling thread's* desktop, and
    ``TejRefresh.exe`` inherits Excel's, so switching this thread before the
    Dispatch moves the whole tree. The same measurement with the switch in
    place saw nothing on the Default desktop, and the refresh took 12.4s
    against 13.0s without -- the isolation is free.

    pywin32 exports ``CreateDesktop`` but not ``SetThreadDesktop``, so both go
    through user32 directly.

    **Order matters, and this is the whole trick.** ``SetThreadDesktop`` fails
    if the calling thread already owns a window or hook, and ``import
    pythoncom`` creates one. Measured 2026-09-05: switching before the import
    succeeds, switching after it fails every time -- with ``pythoncom`` alone,
    with ``win32com.client``, and after ``CoInitialize()``. So the switch has
    to happen before COM is imported, which is why ``ExcelSession.__enter__``
    does it first and checks that nothing imported it earlier.

    (The Win32 error code reported in that state is not reliable: ``windll``
    does not preserve ``GetLastError`` across calls, so the number can be stale.
    The boolean return is what matters.)
    """

    _GENERIC_ALL = 0x10000000

    def __init__(self, name: str | None = None) -> None:
        # One desktop per process. Reusing a fixed name means inheriting
        # whatever an earlier run left bound to it, and CreateDesktop returns
        # a handle to the existing desktop rather than a fresh one.
        self.name = name or f"TejAutomation{os.getpid()}"
        self._u32 = None
        self._handle = None
        self._previous = None

    def __enter__(self) -> PrivateDesktop:
        import ctypes
        from ctypes import wintypes

        u32 = ctypes.windll.user32
        u32.CreateDesktopW.restype = wintypes.HANDLE
        u32.GetThreadDesktop.restype = wintypes.HANDLE
        u32.SetThreadDesktop.argtypes = [wintypes.HANDLE]
        self._u32 = u32

        if "pythoncom" in sys.modules:
            raise ExcelUnavailableError(
                "pythoncom was imported before the desktop switch, which makes "
                "SetThreadDesktop fail: importing it puts a window on the current "
                "desktop. Create the ExcelSession before anything imports COM, or "
                "pass isolate_desktop=False to run on the user's own desktop."
            )

        handle = u32.CreateDesktopW(self.name, None, None, 0, self._GENERIC_ALL, None)
        if not handle:
            raise ExcelUnavailableError(f"could not create desktop {self.name!r}")
        previous = u32.GetThreadDesktop(ctypes.windll.kernel32.GetCurrentThreadId())
        if not u32.SetThreadDesktop(handle):
            u32.CloseDesktop(handle)
            raise ExcelUnavailableError(
                f"could not switch to desktop {self.name!r}; the calling thread "
                "already owns a window or hook"
            )
        self._handle, self._previous = handle, previous
        log.info("Excel will run on private desktop %r", self.name)
        return self

    def __exit__(self, *_exc: object) -> None:
        # Restore first, then close: a desktop cannot be closed while a thread
        # is still bound to it, and the handle is useless afterwards anyway.
        if self._u32 is None:
            return
        if self._previous:
            self._u32.SetThreadDesktop(self._previous)
        if self._handle:
            self._u32.CloseDesktop(self._handle)
        self._handle = self._previous = None


class ExcelSession:
    """A hidden Excel instance with the TEJ add-in loaded.

    Use as a context manager; the Excel process is quit on exit even if the
    body raised, and force-killed if ``Quit()`` itself hangs.
    """

    def __init__(
        self,
        addin: AddinConfig,
        visible: bool = False,
        isolate_desktop: bool | None = None,
    ) -> None:
        self.addin = addin
        self.visible = visible
        # Isolation and visibility are opposites: --visible exists so a person
        # can watch the run, and a private desktop is precisely where they
        # cannot. Default to isolating whenever the run is not being watched.
        self.isolate_desktop = (not visible) if isolate_desktop is None else isolate_desktop
        self.app = None
        self.pid: int | None = None
        self._addin_wb = None
        self._desktop: PrivateDesktop | None = None

    def __enter__(self) -> ExcelSession:
        # The desktop switch has to come first, before COM is imported:
        # importing pythoncom puts a window on the current desktop, and
        # SetThreadDesktop refuses to move a thread that owns one. It also has
        # to come before the Dispatch, because Excel is placed on whatever
        # desktop this thread is bound to at creation time. Both constraints
        # point at the same place -- the very top of this method.
        if self.isolate_desktop:
            self._desktop = PrivateDesktop().__enter__()

        try:
            import pythoncom
            import win32com.client
        except ImportError as exc:  # pragma: no cover - platform dependent
            if self._desktop is not None:
                self._desktop.__exit__()
                self._desktop = None
            raise ExcelUnavailableError(
                "pywin32 is required to drive Excel; install with pip install pywin32"
            ) from exc

        pythoncom.CoInitialize()
        try:
            # DispatchEx, not Dispatch: always a fresh process, never the
            # user's own session.
            self.app = win32com.client.DispatchEx("Excel.Application")
        except Exception as exc:  # noqa: BLE001
            raise ExcelUnavailableError(f"could not start Excel: {exc}") from exc

        self.pid = _excel_pid(self.app)
        self.app.Visible = bool(self.visible)
        self.app.DisplayAlerts = False
        self.app.AskToUpdateLinks = False
        self.app.ScreenUpdating = bool(self.visible)
        # Nothing here depends on recalculation, and the workbooks hold no
        # formulas at all; manual calc keeps a 6,400-row rewrite cheap.
        # Some Excel builds reject changing Calculation before any workbook is
        # open.  This is only a performance optimisation, so do not make the
        # refresh fail when the property is unavailable.
        try:
            self.app.Calculation = _XL_CALC_MANUAL
        except Exception as exc:  # noqa: BLE001 - Excel COM is version-dependent
            log.warning("could not set Excel calculation mode to manual: %s", exc)
        try:
            self.app.AutomationSecurity = _MSO_SECURITY_LOW
        except Exception:  # noqa: BLE001 - not settable on every build
            pass

        self._load_addin()
        return self

    def _load_addin(self) -> None:
        xla = Path(self.addin.xla_path)
        if not xla.exists():
            raise ExcelUnavailableError(
                f"TEJ add-in not found at {xla}. Check addin.xla_path in config/datasets.yml"
            )
        log.info("loading add-in %s", xla.name)
        # Events stay on for this one open: the add-in builds its toolbar and
        # initialises its session in Auto_Open / Workbook_Open.
        self.app.EnableEvents = True
        self._addin_wb = self.app.Workbooks.Open(str(xla), ReadOnly=True)

    @staticmethod
    def _seed_missing_dates(
        wb,
        stock_id: str,
        dates: tuple[dt.date, ...],
        identity: str | None = None,
    ) -> None:
        """Insert prompt rows below the header for Smart Wizard to populate.

        Column A carries the identity string and column B the date; TEJ fills
        the rest. For the price contract one seeded row yields one data row.
        For the index contract one seeded row yields the *whole constituent
        set* -- measured 2026-09-05 on TM100/26-test.xlsx, one row for
        2026-09-07 became 100 (16,302 -> 16,401 rows in 4.8s). Same mechanism
        either way, so the seeding code does not need to know which.
        """
        if not dates:
            return
        sheet = wb.Worksheets(1)
        count = len(dates)
        # Preserve the exact identity string TEJ originally wrote (for example
        # ``2330 台積電``, or ``TWN50 台灣50指數``) rather than replacing it
        # with a bare id.
        identity = identity or sheet.Cells(2, 1).Value2 or stock_id
        # Whole-row insertion lets Excel expand XX_TEJ1 and copies the existing
        # data-row formats, including column B's date number format.
        sheet.Rows(f"2:{count + 1}").Insert(Shift=-4121, CopyOrigin=1)
        newest_first = sorted(dates, reverse=True)
        values = tuple(
            (identity, (date - S.EXCEL_EPOCH).days)
            for date in newest_first
        )
        # Text format on the id column, or Excel silently turns '0050' into the
        # number 50 on write. Never bites when the identity is "<id> <name>",
        # but does the moment a bare ticker is written.
        sheet.Range(f"A2:A{count + 1}").NumberFormat = "@"
        sheet.Range(f"A2:B{count + 1}").Value2 = values

    @staticmethod
    def _populated_seed_dates(
        wb,
        count: int,
        contract: SeedContract = PRICE_SEED,
    ) -> tuple[set[dt.date], dt.date | None]:
        """Return the seeded dates TEJ actually populated, and the newest date.

        Reads columns B..F of the top rows. ``contract.populated_offset`` picks
        the column inside that window whose emptiness means "TEJ returned
        nothing for this row" -- ``close`` for the price contract, ``成份股``
        for the index one.

        The scan window is ``count * contract.max_rows_per_date`` because one
        seeded index date expands into a whole constituent set, so its rows are
        not one-per-date the way the price contract's are.
        """
        sheet = wb.Worksheets(1)
        last = 1 + max(1, count) * contract.max_rows_per_date
        rows = sheet.Range(f"B2:F{last}").Value2
        populated: set[dt.date] = set()
        latest: dt.date | None = None
        for row in rows:
            raw_date, value = row[0], row[contract.populated_offset]
            if raw_date in (None, ""):
                continue
            date = S.excel_serial_to_date(raw_date)
            latest = date if latest is None else max(latest, date)
            if value not in (None, ""):
                populated.add(date)
        return populated, latest

    @staticmethod
    def _delete_unpopulated_rows(
        wb,
        dates: set[dt.date],
        count: int,
        contract: SeedContract,
    ) -> int:
        """Drop the prompt rows TEJ left empty for ``dates``. Returns how many.

        Used only for optional seeds -- dates past today, which TEJ may not
        have published yet. Leaving the row would put an observation-shaped
        hole in the raw layer; the reader drops it anyway, but the workbook is
        the source of truth and should not carry a prompt that was never
        answered.
        """
        if not dates:
            return 0
        sheet = wb.Worksheets(1)
        last = 1 + max(1, count) * contract.max_rows_per_date
        rows = sheet.Range(f"B2:F{last}").Value2
        doomed = [
            i + 2
            for i, row in enumerate(rows)
            if row[0] not in (None, "")
            and row[contract.populated_offset] in (None, "")
            and S.excel_serial_to_date(row[0]) in dates
        ]
        # Bottom-up, or each delete shifts the rows below it.
        for row_number in sorted(doomed, reverse=True):
            sheet.Rows(f"{row_number}:{row_number}").Delete()
        return len(doomed)

    def refresh(
        self,
        path: Path,
        timeout: float,
        seed_dates: tuple[dt.date, ...] = (),
        optional_dates: tuple[dt.date, ...] = (),
        contract: SeedContract = PRICE_SEED,
        identity: str | None = None,
        label: str | None = None,
    ) -> RefreshResult:
        """Open one workbook, run the TEJ refresh macro, save and close.

        ``optional_dates`` must be a subset of ``seed_dates``. Those are
        allowed to come back empty: their rows are removed and the workbook is
        still saved. Every other seeded date going unfilled aborts the save,
        because a half-populated workbook is worse than a stale one.
        """
        path = Path(path)
        size_before = path.stat().st_size
        t0 = time.monotonic()

        timed_out = threading.Event()
        no_new_data = False

        def watchdog() -> None:
            if not done.wait(timeout) and self.pid:
                timed_out.set()
                log.error(
                    "refresh of %s exceeded %.0fs; terminating Excel (pid %d)",
                    path.name,
                    timeout,
                    self.pid,
                )
                _kill(self.pid)

        done = threading.Event()
        watcher = threading.Thread(target=watchdog, daemon=True)
        watcher.start()

        wb = None
        try:
            # Events off: the add-in's open handler is what raises the
            # "auto refresh?" prompt, and there is nobody to answer it.
            self.app.EnableEvents = False
            wb = self.app.Workbooks.Open(str(path), UpdateLinks=0)
            wb.Activate()
            try:
                rows_before = wb.Names.Item("XX_TEJ1").RefersToRange.Rows.Count
            except Exception:  # noqa: BLE001 - diagnostic only
                rows_before = 0
            self._seed_missing_dates(wb, path.stem, seed_dates, identity=identity)
            if seed_dates:
                try:
                    tej_range = wb.Names.Item("XX_TEJ1").RefersToRange.Address
                    log.info("%s TEJ range after seeding: %s", path.name, tej_range)
                except Exception as exc:  # noqa: BLE001 - diagnostic only
                    log.warning("could not inspect %s TEJ range: %s", path.name, exc)
            self.app.EnableEvents = True
            self.app.Run(self.addin.refresh_macro)
            count = max(1, len(seed_dates))
            populated, latest_after = self._populated_seed_dates(wb, count, contract)
            missing_after = set(seed_dates) - populated
            # An optional seed is a date past today: the constituent list for
            # the next session, which TEJ publishes after the previous close.
            # Not having it yet is normal, so the row is dropped and the run
            # still counts as a success. A settled date going unfilled is not.
            unfilled_optional = missing_after & set(optional_dates)
            missing_required = missing_after - unfilled_optional
            # A security that has stopped trading is indistinguishable from a
            # failed refresh on any single date -- both leave the prompt row
            # empty. What separates them is that a stopped security returns
            # NOTHING, on a workbook that already holds a full history.
            # Aborting there blocks the whole daily job for the ~20 sessions
            # the workbook stays inside the active window, which is what
            # 2867 三商壽 did on 2026-09-05: last bar 2026-08-31, and FinMind
            # has nothing after 2026-08-20 either.
            #
            # So this is reported rather than raised. The file is left exactly
            # as it was, and `v_workbook_status` is where a workbook that
            # should have advanced becomes visible.
            if missing_required and missing_required == set(seed_dates) and rows_before > 1:
                ok = True
                no_new_data = True
                message = (
                    "TEJ has no data for "
                    + ", ".join(d.isoformat() for d in sorted(missing_required))
                    + "; workbook left unchanged"
                )
            elif missing_required:
                top_values = wb.Worksheets(1).Range(f"A2:F{count + 1}").Value2
                log.error("%s top rows after TEJ refresh: %r", path.name, top_values)
                missing_text = ", ".join(d.isoformat() for d in sorted(missing_required))
                ok, message = False, f"TEJ did not populate seeded date(s): {missing_text}"
            else:
                if unfilled_optional:
                    dropped = self._delete_unpopulated_rows(
                        wb, unfilled_optional, count, contract
                    )
                    log.info(
                        "%s: TEJ has no data yet for %s; dropped %d prompt row(s)",
                        path.name,
                        ", ".join(d.isoformat() for d in sorted(unfilled_optional)),
                        dropped,
                    )
                wb.Save()
                ok, message = True, ""
        except Exception as exc:  # noqa: BLE001 - COM raises bare Exception
            ok = False
            message = "timed out" if timed_out.is_set() else f"{type(exc).__name__}: {exc}"
        finally:
            done.set()
            if wb is not None and not timed_out.is_set():
                try:
                    wb.Close(SaveChanges=False)
                except Exception:  # noqa: BLE001
                    pass
            # Release the workbook proxy before the application is quit.  A
            # dangling proxy can make COM start a replacement hidden Excel
            # process later, leaving an otherwise stale ~$ lock file behind.
            wb = None
            gc.collect()

        size_after = path.stat().st_size if path.exists() else 0
        return RefreshResult(
            stock_id=path.stem,
            path=path,
            label=label or path.stem,
            ok=ok,
            seconds=round(time.monotonic() - t0, 1),
            message=message,
            bytes_before=size_before,
            bytes_after=size_after,
            seeded_dates=seed_dates,
            latest_date_after=latest_after if 'latest_after' in locals() else None,
            no_new_data=no_new_data,
        )

    def create_from_template(
        self,
        template: Path,
        out_path: Path,
        stock_id: str,
        extra_dates: tuple[dt.date, ...],
        timeout: float,
    ) -> RefreshResult:
        """Build a new security's workbook by re-pointing an existing one.

        There is no way to *compose* a TEJ query from outside the add-in -- the
        query definition lives in the workbook's A1 comment. But the definition
        does not name the security: the identity is in column A of each data
        row. So an existing workbook, with its column A rewritten and its data
        cleared, is a valid query for a different security.

        Verified 2026-09-05 against a workbook downloaded by hand from the
        add-in: rebuilding 1103 from 1101's workbook produced 6,411 rows over
        2000-09-04..2026-09-04 and **230,796 of 230,796 cells identical**, with
        the same key set in both directions. TEJ also resolves the bare id back
        to ``1103 嘉泥`` and trims the skeleton to the security's real life --
        7799, listed 2024-11-27, came back as 431 rows from a 6,410-date
        template, with ``XX_TEJ1`` shrunk to match. So the caller does not need
        to know a listing date.

        The build happens on a temporary path and is moved into place only
        after validation, because the raw layer is the source of truth and a
        half-built workbook there is indistinguishable from a real one.
        """
        template, out_path = Path(template), Path(out_path)
        t0 = time.monotonic()
        building = out_path.with_name(out_path.stem + ".building.xlsx")
        shutil.copy2(template, building)

        timed_out = threading.Event()
        done = threading.Event()

        def watchdog() -> None:
            if not done.wait(timeout) and self.pid:
                timed_out.set()
                log.error(
                    "build of %s exceeded %.0fs; terminating Excel (pid %d)",
                    out_path.name,
                    timeout,
                    self.pid,
                )
                _kill(self.pid)

        threading.Thread(target=watchdog, daemon=True).start()

        wb = None
        latest_after: dt.date | None = None
        try:
            self.app.EnableEvents = False
            wb = self.app.Workbooks.Open(str(building), UpdateLinks=0)
            wb.Activate()
            sheet = wb.Worksheets(1)
            block_rows = wb.Names.Item("XX_TEJ1").RefersToRange.Rows.Count
            last_row = block_rows  # header included, so this is the last data row
            if last_row < 2:
                raise ValueError(f"{template.name}: template has no data rows to reuse")

            # Text format first, or '0050' is written as the number 50.
            sheet.Range(f"A2:A{last_row}").NumberFormat = "@"
            sheet.Range(f"A2:A{last_row}").Value2 = tuple(
                (stock_id,) for _ in range(last_row - 1)
            )
            # Blank every data field, so a value left over from the template
            # cannot be mistaken for one TEJ returned.
            sheet.Range(f"C2:AI{last_row}").ClearContents()
            self._seed_missing_dates(wb, stock_id, extra_dates, identity=stock_id)

            self.app.EnableEvents = True
            self.app.Run(self.addin.refresh_macro)

            block_rows = wb.Names.Item("XX_TEJ1").RefersToRange.Rows.Count
            data_rows = block_rows - 1
            if data_rows < 1:
                raise ValueError(f"TEJ returned no rows for {stock_id}")

            identities = _column_values(sheet.Range(f"A2:A{block_rows}").Value2)
            found = {str(v).split(" ")[0] for v in identities if v not in (None, "")}
            if found != {stock_id}:
                raise ValueError(
                    f"expected only {stock_id} in column A, found {sorted(found)}"
                )
            # A code TEJ has no prices for does not shrink the block: it comes
            # back as the template's whole date skeleton with every field empty
            # (5513/8702/8706/8709 on 2026-09-28, all delisted 2001-2003). The
            # identity check above passes on that, so require a real close.
            closes = _column_values(sheet.Range(f"F2:F{block_rows}").Value2)
            if not any(v not in (None, "") for v in closes):
                raise ValueError(f"TEJ returned no prices for {stock_id}")
            dates = _column_values(sheet.Range(f"B2:B{block_rows}").Value2)
            latest_after = max(
                S.excel_serial_to_date(v) for v in dates if v not in (None, "")
            )

            # The template's surplus rows are cleared but still present in the
            # sheet, which inflates the file. Drop everything below the block.
            used_rows = sheet.UsedRange.Rows.Count
            if used_rows > block_rows:
                sheet.Rows(f"{block_rows + 1}:{used_rows}").Delete()

            wb.Save()
            ok, message = True, ""
        except Exception as exc:  # noqa: BLE001 - COM raises bare Exception
            ok = False
            message = "timed out" if timed_out.is_set() else f"{type(exc).__name__}: {exc}"
            data_rows = 0
        finally:
            done.set()
            if wb is not None and not timed_out.is_set():
                try:
                    wb.Close(SaveChanges=False)
                except Exception:  # noqa: BLE001
                    pass
            wb = None
            gc.collect()

        if ok:
            replace_with_retry(building, out_path)
        else:
            building.unlink(missing_ok=True)

        return RefreshResult(
            stock_id=stock_id,
            path=out_path,
            ok=ok,
            seconds=round(time.monotonic() - t0, 1),
            message=message,
            bytes_before=0,
            bytes_after=out_path.stat().st_size if ok and out_path.exists() else 0,
            seeded_dates=extra_dates,
            latest_date_after=latest_after,
            label=stock_id,
        )

    def __exit__(self, *_exc: object) -> None:
        app = self.app
        addin_wb = self._addin_wb
        self.app = None
        self._addin_wb = None
        try:
            if addin_wb is not None:
                try:
                    addin_wb.Close(SaveChanges=False)
                except Exception:  # noqa: BLE001
                    pass
            if app is not None:
                app.DisplayAlerts = False
                app.Quit()
        except Exception:  # noqa: BLE001
            pass
        finally:
            addin_wb = None
            app = None
            gc.collect()
            try:
                import pythoncom

                pythoncom.CoFreeUnusedLibraries()
            except Exception:  # noqa: BLE001
                pass
            # Quit() can leave the process alive if the add-in holds a
            # reference; the automation instance is ours alone, so killing it
            # cannot disturb a session the user has open.
            if self.pid:
                time.sleep(0.5)
                _kill(self.pid)
            try:
                import pythoncom

                pythoncom.CoUninitialize()
            except Exception:  # noqa: BLE001
                pass
            # Last, so the desktop outlives every Excel handle that lived on it.
            if self._desktop is not None:
                self._desktop.__exit__()
                self._desktop = None


def backup_workbooks(
    paths: Iterable[Path],
    backup_root: Path,
    keep: int = 2,
    relative_to: Path | None = None,
) -> Path:
    """Copy workbooks aside before refreshing, keeping the last ``keep`` sets.

    The raw layer is the source of truth for this pipeline — unlike the FinMind
    side there is no API to re-fetch from, so a workbook damaged by an
    interrupted save is unrecoverable without downloading it from TEJ again.

    ``relative_to`` preserves the directory structure under it. The index
    workbooks need it: every index has a ``2026.xlsx``, so a flat backup would
    silently keep only the last one copied.
    """
    backup_root = Path(backup_root)
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    target = backup_root / stamp
    target.mkdir(parents=True, exist_ok=True)
    copied = 0
    for p in paths:
        dest = target / (p.relative_to(relative_to) if relative_to else p.name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dest)
        copied += 1

    sets = sorted((d for d in backup_root.iterdir() if d.is_dir()), reverse=True)
    for old in sets[keep:]:
        shutil.rmtree(old, ignore_errors=True)
    log.info("backed up %d workbooks to %s", copied, target)
    return target


@dataclass(frozen=True)
class RefreshJob:
    """One workbook to refresh, and everything specific to it.

    ``label`` is what the run log calls it -- a stock id for the price
    workbooks, ``TWN50/2026.xlsx`` for an index-year one, where the bare
    filename repeats across indices.
    """

    path: Path
    label: str
    seed_dates: tuple[dt.date, ...] = ()
    optional_dates: tuple[dt.date, ...] = ()
    contract: SeedContract = PRICE_SEED
    identity: str | None = None


def run_refresh_jobs(
    jobs: list[RefreshJob],
    addin: AddinConfig,
    visible: bool = False,
) -> list[RefreshResult]:
    """Refresh each job in turn through one hidden Excel session."""
    results: list[RefreshResult] = []
    with ExcelSession(addin, visible=visible) as session:
        for i, job in enumerate(jobs, 1):
            path = job.path
            log.info("[%d/%d] refreshing %s", i, len(jobs), job.label)
            if job.seed_dates:
                optional = set(job.optional_dates)
                log.info(
                    "[%d/%d] seeding %d missing trading date(s): %s",
                    i,
                    len(jobs),
                    len(job.seed_dates),
                    ", ".join(
                        d.isoformat() + (" (optional)" if d in optional else "")
                        for d in job.seed_dates
                    ),
                )
            result = session.refresh(
                path,
                timeout=addin.timeout_seconds,
                seed_dates=job.seed_dates,
                optional_dates=job.optional_dates,
                contract=job.contract,
                identity=job.identity,
                label=job.label,
            )
            results.append(result)
            if result.no_new_data:
                log.info("[%d/%d] %s: %s", i, len(jobs), job.label, result.message)
            elif result.ok:
                log.info(
                    "[%d/%d] %s done in %.1fs (%+d bytes)",
                    i,
                    len(jobs),
                    job.label,
                    result.seconds,
                    result.bytes_after - result.bytes_before,
                )
            else:
                log.error("[%d/%d] %s FAILED: %s", i, len(jobs), job.label, result.message)
                # A timeout killed the Excel process; the session is gone and
                # every later file would fail the same way.
                if result.message == "timed out":
                    break
            if addin.pause_seconds and i < len(jobs):
                time.sleep(addin.pause_seconds)
    return results


def refresh_workbooks(
    paths: list[Path],
    addin: AddinConfig,
    visible: bool = False,
    seed_dates_by_stock: dict[str, tuple[dt.date, ...]] | None = None,
) -> list[RefreshResult]:
    """Refresh price workbooks. Thin wrapper over ``run_refresh_jobs``."""
    jobs = [
        RefreshJob(
            path=path,
            label=path.stem,
            seed_dates=(seed_dates_by_stock or {}).get(path.stem, ()),
        )
        for path in paths
    ]
    return run_refresh_jobs(jobs, addin, visible=visible)
