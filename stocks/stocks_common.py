#!/usr/bin/env python3
r"""
stocks_common.py - the small things every script in this project needs.

Four things had been copied into every stage instead of shared: where the
project lives, where its data goes, how to coerce a value to a float, and how
to write JSON without a reader ever seeing half a file. Five copies of each,
and they had already begun to drift - stock_evaluator's path search had no
final fallback where position_sizer's did, and scan_report's atomic write used
a different temp-file scheme from the other three. None of that drift was
deliberate and none of it was visible from any one file.

This module owns those four. It imports nothing but the standard library and
nothing from this project, so it sits at the bottom of the import graph and
market_data.py can build on it rather than the other way round.

    BASE_DIR                     the folder holding these scripts
    data_dir()                   <base>\data, honouring $STOCKS_DATA_DIR
    account_currency()           what the account settles in ($STOCKS_ACCOUNT_CURRENCY)
    preferred_suffixes()         which listing that currency would rather hold
    is_cad_listing(ticker)       whether a symbol is a Canadian listing
    add_project_dir_to_path()    make the sibling modules importable
    num(value)                   float, or None for None/NaN/non-numeric
    read_json(path)              parsed JSON, or None - never raises
    write_json(doc, path)        atomic write (tmp file + replace)
    stamp_logic_version(doc, v)  record which version of a stage wrote a file
    output_logic_version(path)   read that back, or None

Deliberately NOT used by stock_view\sv\. That package's contract is that every
view which only reads JSON works with nothing installed but streamlit, pandas
and plotly, and it cannot import a pipeline module to find out how to parse a
number. Its two small copies of num() are the price of that guarantee, and are
kept on purpose.
"""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional

# Project root. On the machine this was written for that resolves to
# C:\Users\joey\stocks, because these files live there. market_data.py re-
# exports this as market_data.BASE_DIR, which is the name the rest of the
# project and stock_view already ask for.
BASE_DIR = Path(os.environ.get("STOCKS_DIR") or Path(__file__).resolve().parent)


# ─── WHERE THE ACCOUNT SETTLES ─────────────────────────────────────────────
# Every stage that compares money to money needs to know this, and until it was
# named here each one simply assumed USD: the screener pulled US listings only,
# the liquidity gate converted into USD, the dedupe kept whichever listing
# traded most (always the US one for a cross-listed name), and the paper
# simulator refused to buy anything quoted in anything else.
#
# Set STOCKS_ACCOUNT_CURRENCY=CAD once and those defaults follow the account
# instead. Every one of them still takes an explicit flag that wins over this.
DEFAULT_ACCOUNT_CURRENCY = "USD"

# Yahoo's suffixes for the Canadian venues: Toronto, TSX Venture, the CSE, and
# Cboe Canada - which is where the CDRs trade, the CAD-hedged depositary
# receipts over US megacaps that a Canadian account buys instead of the US line.
CAD_SUFFIXES = (".TO", ".V", ".CN", ".NE")

# Which listing an account in a given currency would rather hold. "" means a
# symbol with no suffix at all, which is how Yahoo writes a US listing.
PREFERRED_SUFFIXES = {
    "CAD": CAD_SUFFIXES,
    "USD": ("",),
}


def account_currency(default: Optional[str] = None) -> str:
    """The currency the account settles in. $STOCKS_ACCOUNT_CURRENCY, else USD."""
    value = os.environ.get("STOCKS_ACCOUNT_CURRENCY")
    if value and value.strip():
        return value.strip().upper()
    return (default or DEFAULT_ACCOUNT_CURRENCY).upper()


def preferred_suffixes(currency: Optional[str] = None) -> tuple:
    """The listing suffixes an account in this currency should keep. () if unknown.

    An empty tuple rather than a guess matters: with no preference the dedupe
    falls back to keeping the most traded listing, which is a defensible answer
    for a currency nothing here knows about. A wrong preference silently drops
    the listing the person can actually buy.
    """
    return PREFERRED_SUFFIXES.get((currency or account_currency()).upper(), ())


def is_cad_listing(ticker: Optional[str]) -> bool:
    """True for a Toronto / Venture / CSE / Cboe Canada symbol."""
    symbol = str(ticker or "").strip().upper()
    return any(symbol.endswith(suffix) for suffix in CAD_SUFFIXES)


def data_dir(base: Optional[Path] = None) -> Path:
    """<base>\\data, or $STOCKS_DATA_DIR when it is set.

    Every stage reads and writes here, and every stage had its own copy of this
    one-liner. The override wins over `base` so that pointing the environment
    variable somewhere moves the whole pipeline at once.
    """
    override = os.environ.get("STOCKS_DATA_DIR")
    if override:
        return Path(override)
    return Path(base or BASE_DIR) / "data"


def add_project_dir_to_path() -> Optional[Path]:
    """Put the folder holding these scripts on sys.path. Returns it, or None.

    Needed when a module here is imported from somewhere else - stock_view
    importing position_sizer, say - because then sys.path[0] is the importer's
    folder, not this one.

    Each script still carries a four-line copy of this search inline, and that
    is not an oversight: a script cannot call this function until it can import
    this module, which is the very thing the search exists to arrange. The
    inline copies are identical to each other, which is the part that matters -
    what drifted before was five subtly different searches, not the fact that
    there were five.
    """
    here = Path(__file__).resolve().parent
    for candidate in (here, here.parent / "stocks", here.parent):
        try:
            if (candidate / "market_data.py").exists():
                resolved = str(candidate.resolve())
                if resolved not in sys.path:
                    sys.path.insert(0, resolved)
                return candidate.resolve()
        except OSError:
            continue
    return None


def num(value) -> Optional[float]:
    """Coerce to float. None for None, NaN, or anything non-numeric.

    NaN is folded into None on purpose. It is the value that compares False
    against every threshold, so letting one through means it survives an
    "is this big enough to act on" test and is then acted on - which is exactly
    how a NaN correlation once drove a full-size position cut.
    """
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) else out


def read_json(path) -> Optional[dict]:
    """Parse a JSON file, or return None. Never raises.

    A missing or half-written file is an ordinary state in this pipeline - a
    stage may not have run yet - so callers get None and decide what it means.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ─── STAGE LOGIC VERSIONS ──────────────────────────────────────────────────
# scan_report skips a stage whose output is still fresh by TTL. That is a
# statement about the DATA's age and says nothing about the CODE that produced
# it, so after a scoring change the pipeline would happily re-render yesterday's
# numbers under today's timestamp for the rest of the TTL - the failure looks
# exactly like success. Each stage stamps the version of its own logic into its
# output; scan_report treats a mismatch as stale, whatever the file's age.
#
# Bump a stage's version whenever it would produce a DIFFERENT ANSWER from the
# same inputs. A refactor that cannot change the output does not need a bump; a
# threshold change, a new score term, or a changed default does.
LOGIC_VERSION_KEY = "logic_version"


def stamp_logic_version(document: Any, version: str) -> Any:
    """Record which version of a stage's logic produced this document."""
    if isinstance(document, dict):
        document[LOGIC_VERSION_KEY] = str(version)
    return document


def output_logic_version(path) -> Optional[str]:
    """The logic version stamped in a stage's output, or None.

    None covers both "file is not there" and "file predates versioning", and
    both mean the same thing to a caller deciding whether to re-run: this
    output cannot be shown to have come from the code that is on disk now.
    """
    document = read_json(path)
    if not isinstance(document, dict):
        return None
    version = document.get(LOGIC_VERSION_KEY)
    return None if version is None else str(version)


# Windows refuses os.replace with "Access is denied" while anything else holds
# either file open, and something usually does for a moment: a real-time
# antivirus scanner opens every file the instant it is created, and OneDrive
# does the same. The hold is milliseconds, so a few short retries turn a failed
# cache write - and with it a refetch, and with enough of those a rate limit -
# back into a write that simply happened a moment later. POSIX never takes this
# path.
_REPLACE_ATTEMPTS = 4
_REPLACE_DELAY = 0.1


def _replace_with_retry(tmp_path, output_path) -> None:
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(tmp_path, output_path)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_DELAY * (2 ** attempt))


def write_json(document: Any,
               output_path,
               default: Optional[Callable[[Any], Any]] = None,
               indent: int = 2) -> Path:
    """Write JSON atomically: temp file in the same folder, then os.replace().

    Same folder because os.replace is only atomic within a filesystem. The temp
    file is uniquely named, so two runs writing the same output cannot collide
    on it, and it is removed if the write fails - a reader of this pipeline's
    output should never see a partial file, and never a stray .tmp either.

    `default` is json.dump's fallback encoder; the stages pass their own
    (str, or one that unwraps numpy and pandas scalars).
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(output_path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(document, f, indent=indent, ensure_ascii=False, default=default)
        _replace_with_retry(tmp_path, output_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return output_path


def json_default(obj):
    """Last-resort encoder: datetimes, sets, numpy scalars, pandas objects.

    market_data and stock_evaluator both grew a version of this for the same
    reason - a cache entry or a scan record can hold a numpy float that
    json.dump refuses - so it lives here with them.
    """
    import datetime as dt

    if isinstance(obj, (dt.datetime, dt.date, dt.time)):
        return obj.isoformat()
    if isinstance(obj, (set, frozenset, tuple)):
        return list(obj)
    item = getattr(obj, "item", None)          # numpy scalar
    if callable(item):
        try:
            return item()
        except Exception:
            pass
    to_dict = getattr(obj, "to_dict", None)    # pandas Series/DataFrame
    if callable(to_dict):
        try:
            return to_dict()
        except Exception:
            pass
    return str(obj)


def _self_test() -> int:
    cases = [
        ("num(None)", num(None), None),
        ("num('')", num(""), None),
        ("num('3.5')", num("3.5"), 3.5),
        ("num(nan)", num(float("nan")), None),
        ("num(inf)", num(float("inf")), float("inf")),
        ("num(True)", num(True), 1.0),
    ]
    ok = True
    print("-- num --")
    for label, got, want in cases:
        good = got == want
        ok &= good
        print(f"  {'ok  ' if good else 'FAIL'} {label:<14} -> {got!r}")

    print("\n-- write_json is atomic and leaves no temp file --")
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "nested" / "out.json"
        write_json({"a": 1}, target, default=json_default)
        strays = [p.name for p in target.parent.glob("*.tmp")]
        round_trip = read_json(target)
        good = round_trip == {"a": 1} and not strays
        ok &= good
        print(f"  {'ok  ' if good else 'FAIL'} wrote {target.name}, "
              f"read back {round_trip}, {len(strays)} stray temp file(s)")

        print("\n-- read_json never raises --")
        bad = Path(tmp) / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        for label, path in (("malformed", bad), ("missing", Path(tmp) / "nope.json")):
            got = read_json(path)
            ok &= got is None
            print(f"  {'ok  ' if got is None else 'FAIL'} {label:<10} -> {got!r}")

    print(f"\nBASE_DIR:  {BASE_DIR}")
    print(f"data_dir(): {data_dir()}")
    print(f"\nself-test: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_self_test())
