"""Security-code shape classification, and what counts as in scope.

The whole-market endpoint returns ~43,700 codes on a recent day, only ~2,800 of
which are securities this project keeps. There is no upstream field that says
what a code *is*: ``TaiwanStockInfo`` covers currently listed securities only,
so a delisted code arrives with no metadata whatsoever. Shape is the only
signal available, and it is a reliable one because the exchange allocates code
ranges by instrument type.

Measured over 271 whole-market days sampled monthly from 2004-02 to 2026-08,
unioned with the current security master — 138,937 distinct codes:

=============  =======  ===================================================
shape          count    what it is
=============  =======  ===================================================
``warrant``    108,406  權證 — ``######``, ``#####``, pre-2007 ``0###``/``0###P``
``tdr``         27,041  TDRs and beneficiary certificates, ``#####X``
``common``       2,973  ordinary shares, ``####``
``etf``            423  ETFs and closed-end funds, ``00`` + 2-4 digits
``preferred``       61  特別股 and convertible-bond series, ``####X``
``index``           32  alphabetic pseudo-tickers — ``TAIEX``, ``Textiles``
``other``            1  ``2887Z1``, which is in the master anyway
=============  =======  ===================================================

The ``00`` prefix turned out to be unambiguous: all 423 such codes are funds
and 417 of them are in the current master, so no six-digit warrant competes
for that shape.

Only ``common`` and ``etf`` are in scope. Warrants and TDRs are excluded
deliberately: including them would mean ~97M rows and 2-3 GB instead of ~12M
rows and ~300 MB (see ``docs/schema.md``). ``preferred`` is left out because
the shape cannot separate genuine 特別股 (``1101A``, ``2801A``) from the
convertible-bond series sharing it (``2418S`` through ``2418W``).

Widening the scope is a one-line change to :data:`IN_SCOPE_SHAPES` plus a
re-run of ``scripts/discover_delisted.py``. The discovery snapshots record
every shape they saw, so nothing has to be re-fetched from the API.

This classifier is applied only to codes *absent* from the security master.
Codes the master already carries are in the universe by virtue of being there,
whatever their shape — the master itself holds 226 warrant-shaped and 258
TDR-shaped ids, and reclassifying those would silently change existing scope.
"""

from __future__ import annotations

import re

# Order matters. The ETF patterns must be tried before the generic numeric
# ones (else 006202 reads as a warrant), and `0###` must be tried after them
# (else 0050 reads as a warrant).
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("index", re.compile(r"[A-Za-z]")),
    # Before every numeric rule: 00679B is an ETF, not a TDR, and 0050 is an
    # ETF, not a pre-2007 warrant.
    ("etf", re.compile(r"00\d{2,4}[A-Z]?$")),
    ("common", re.compile(r"[1-9]\d{3}$")),
    ("preferred", re.compile(r"[1-9]\d{3}[A-Z]$")),
    ("tdr", re.compile(r"\d{5}[A-Z]$")),
    # Pre-2007 warrants: 0347, and put warrants 0617P.
    ("warrant", re.compile(r"0\d{3}[A-Z]?$")),
    ("warrant", re.compile(r"\d{5}$")),
    ("warrant", re.compile(r"\d{6}$")),
)

IN_SCOPE_SHAPES = frozenset({"common", "etf"})


def classify_stock_id(stock_id: str) -> str:
    """Return the shape of a security code.

    Never raises and never returns None — an unrecognised code is ``other``,
    which keeps it out of scope without losing the record of having seen it.
    """
    if not stock_id:
        return "other"
    for shape, pattern in _PATTERNS:
        if pattern.match(stock_id):
            return shape
    return "other"


def in_scope(stock_id: str, shapes: frozenset[str] = IN_SCOPE_SHAPES) -> bool:
    return classify_stock_id(stock_id) in shapes
