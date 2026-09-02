r"""Read, validate and write data\holdings.json - the one file stock_view edits.

Everything else in this app is read-only over the pipeline's output.
holdings.json is different: it is not output, it is the hand-maintained INPUT
position_sizer.py and holdings_exit.py both read, and until now the only way to
change it was a text editor.

Two boundaries this module holds:

  - It touches holdings.json and nothing else. paper_portfolio.json belongs to
    paper_sim.py's separate simulated track and is never read or written here;
    the two ledgers exist precisely so that one cannot be mistaken for the
    other.
  - The document it writes is exactly the shape position_sizer.load_holdings()
    reads: a "holdings" list of {ticker, shares, cost_basis, currency,
    entry_date, entry_price}. Field names and types are taken from that
    function rather than invented here, because a rename on this side is a
    silent data loss on that one.

entry_date and entry_price are optional, deliberately. The pipeline treats a
missing one as unknown and skips the checks that need it - holdings_exit says
"insufficient entry data" rather than guessing - so a position whose entry is
genuinely not known can still be recorded. What is not allowed is a value that
is present and wrong: a negative price, or an entry date in the future.

Streamlit is not imported here. The validation rules are the part worth being
able to test without a browser.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional

# The schema, in the order it is written back to the file. Taken from
# position_sizer.load_holdings(); holdings_exit.py reads the same rows through
# it, so this list is the contract between all three.
FIELDS = ("ticker", "shares", "cost_basis", "currency", "entry_date", "entry_price")

# Written only when creating the file from nothing, so a hand-editor opening it
# later sees the same explanation position_sizer's own template carries.
NEW_FILE_COMMENT = (
    "Hand-edited holdings file. One entry per position you actually own. "
    "ticker: the Yahoo symbol (RY.TO for a TSX listing). "
    "shares: number of shares held. "
    "cost_basis: your average price per share, in the listing's own currency. "
    "currency: optional - the currency cost_basis is in (USD, CAD). "
    "entry_date: YYYY-MM-DD, the day the position was opened - optional, but "
    "holdings_exit.py needs it for the stop-loss and reassess checks. "
    "entry_price: what you paid per share that day - optional, and not the "
    "same number as cost_basis once a position has been added to."
)

BACKUP_SUFFIX = ".bak"


def backup_path(path) -> Path:
    """holdings.json -> holdings.json.bak, beside it."""
    path = Path(path)
    return path.with_name(path.name + BACKUP_SUFFIX)


def _is_missing(value) -> bool:
    """True for None, NaN and pandas' NaT.

    The editor hands back whatever the column type produced, and an empty date
    cell is NaT rather than None. NaN and NaT are the only values in Python
    that are not equal to themselves, which is the one test that catches both
    without importing pandas here.
    """
    if value is None:
        return True
    try:
        return bool(value != value)
    except Exception:                                  # noqa: BLE001
        return False


def _num(value) -> Optional[float]:
    """float, or None for None/NaN/blank/non-numeric. data_loader._num's rule."""
    if _is_missing(value):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) else out


def _text(value) -> str:
    if _is_missing(value):
        return ""
    return str(value).strip()


def date_text(value) -> str:
    """Anything a date column can hand back -> "YYYY-MM-DD", or "".

    st.column_config.DateColumn returns a date; a hand-written file carries a
    string; pandas turns an empty cell into NaT. All three arrive here.
    """
    if _is_missing(value):
        return ""
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    text = str(value).strip()
    if not text or text.lower() in ("nat", "none", "nan"):
        return ""
    return text[:10]


def parse_date(value) -> Optional[dt.date]:
    """The date behind a cell, or None when it is blank or unparseable."""
    text = date_text(value)
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        return None


# ── reading ───────────────────────────────────────────────────────────────

def read_document(path) -> tuple[Optional[dict], Optional[str]]:
    """The raw file, or (None, why not). Never raises."""
    path = Path(path)
    if not path.exists():
        return None, "not found"
    try:
        with open(path, "r", encoding="utf-8") as f:
            document = json.load(f)
    except json.JSONDecodeError as exc:
        return None, f"not valid JSON ({exc})"
    except OSError as exc:
        return None, f"could not be read ({exc})"
    if not isinstance(document, dict):
        return None, "unexpected shape (expected a JSON object)"
    return document, None


def rows_from_document(document: Optional[dict]) -> tuple[list, list]:
    """-> (editable rows, rows carried through untouched).

    The second list is everything position_sizer already ignores: entries
    marked _example, and entries with no ticker. They are inert to the whole
    pipeline, so a save leaves them exactly as they were rather than deleting
    something the person never touched.

    An editable row keeps any field this app does not know about under
    "_extra", so a note someone added by hand survives a round trip.
    """
    editable, passthrough = [], []
    for entry in (document or {}).get("holdings") or []:
        if not isinstance(entry, dict):
            passthrough.append(entry)
            continue
        ticker = _text(entry.get("ticker")).upper()
        if entry.get("_example") or not ticker or ticker == "...":
            passthrough.append(entry)
            continue
        editable.append({
            "ticker": ticker,
            "shares": _num(entry.get("shares")),
            "cost_basis": _num(entry.get("cost_basis")),
            "currency": _text(entry.get("currency")).upper(),
            "entry_date": date_text(entry.get("entry_date")),
            "entry_price": _num(entry.get("entry_price")),
            "_extra": {k: v for k, v in entry.items() if k not in FIELDS},
        })
    return editable, passthrough


# ── validation ────────────────────────────────────────────────────────────

def _positive(value, label, where, errors) -> Optional[float]:
    number = _num(value)
    if number is None:
        errors.append(f"{where}: {label} is required and must be a number.")
        return None
    if number <= 0:
        errors.append(f"{where}: {label} must be greater than zero (got {number:g}).")
        return None
    return number


def validate(rows, today: Optional[dt.date] = None) -> tuple[list, list, list]:
    """-> (entries ready to write, errors, warnings).

    Errors block the save; warnings do not. A blank entry_date or entry_price
    is a warning, not an error, because the pipeline has an answer for an
    unknown entry and no answer at all for a wrong one.
    """
    today = today or dt.date.today()
    entries, errors, warnings = [], [], []
    seen: dict[str, str] = {}

    for index, row in enumerate(rows, start=1):
        ticker = _text(row.get("ticker")).upper()
        where = f"Row {index}" + (f" ({ticker})" if ticker else "")

        if not ticker or ticker == "...":
            # A row with nothing in it at all is an artifact of adding a row and
            # changing your mind; say so plainly rather than as a type error.
            if not any(_text(row.get(field)) for field in FIELDS[1:]):
                errors.append(f"Row {index}: empty row - fill it in or delete it.")
            else:
                errors.append(f"Row {index}: ticker is required.")
            continue
        if ticker in seen:
            errors.append(f"{where}: duplicate of row {seen[ticker]}. One row per "
                          f"position - add to the shares instead.")
            continue
        seen[ticker] = str(index)

        shares = _positive(row.get("shares"), "shares", where, errors)
        cost_basis = _positive(row.get("cost_basis"), "cost basis", where, errors)

        entry_price = None
        if _text(row.get("entry_price")):
            entry_price = _positive(row.get("entry_price"), "entry price", where, errors)
        else:
            warnings.append(f"{where}: no entry price - holdings_exit.py will skip "
                            f"the stop-loss check for this position.")

        entry_date = None
        raw_date = date_text(row.get("entry_date"))
        if raw_date:
            entry_date = parse_date(raw_date)
            if entry_date is None:
                errors.append(f"{where}: entry date {raw_date!r} is not a valid "
                              f"YYYY-MM-DD date.")
            elif entry_date > today:
                errors.append(f"{where}: entry date {entry_date.isoformat()} is in "
                              f"the future.")
                entry_date = None
        else:
            warnings.append(f"{where}: no entry date - holdings_exit.py will skip "
                            f"the reassess check for this position.")

        currency = _text(row.get("currency")).upper()
        if currency and not (currency.isalpha() and 3 <= len(currency) <= 4):
            errors.append(f"{where}: currency {currency!r} is not a currency code "
                          f"like USD or CAD. Leave it blank to let the scan infer it.")
            currency = ""

        entry = {"ticker": ticker, "shares": shares, "cost_basis": cost_basis}
        if currency:
            entry["currency"] = currency
        if entry_date is not None:
            entry["entry_date"] = entry_date.isoformat()
        if entry_price is not None:
            entry["entry_price"] = entry_price
        extra = row.get("_extra") or {}
        for key, value in extra.items():
            entry.setdefault(key, value)
        entries.append(entry)

    if errors:
        return [], errors, warnings
    return entries, errors, warnings


# ── what changed ──────────────────────────────────────────────────────────

_LABELS = {"shares": "shares", "cost_basis": "cost basis", "currency": "currency",
           "entry_date": "entry date", "entry_price": "entry price"}


def _shown(value) -> str:
    if value in (None, ""):
        return "—"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def summarize(before: list, after: list) -> dict:
    """Rows added, removed and changed, by ticker, for the save confirmation."""
    old = {row["ticker"]: row for row in before}
    new = {entry["ticker"]: entry for entry in after}

    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = []
    for ticker in sorted(set(old) & set(new)):
        differences = []
        for field in ("shares", "cost_basis", "currency", "entry_date", "entry_price"):
            was, now = old[ticker].get(field), new[ticker].get(field)
            was = None if was in ("", None) else was
            now = None if now in ("", None) else now
            if isinstance(was, float) and isinstance(now, float):
                if math.isclose(was, now, rel_tol=1e-9, abs_tol=1e-9):
                    continue
            elif was == now:
                continue
            differences.append(f"{_LABELS[field]} {_shown(was)} → {_shown(now)}")
        if differences:
            changed.append((ticker, differences))
    return {"added": added, "removed": removed, "changed": changed,
            "unchanged": len(set(old) & set(new)) - len(changed)}


# ── writing ───────────────────────────────────────────────────────────────

def file_stamp(path) -> tuple:
    """(mtime_ns, size), or (None, None). Compared either side of an edit.

    The pipeline can rewrite holdings.json while this tab is open -
    position_sizer writes a template when the file is missing - and a save that
    overwrote that blindly would lose it. Cheap insurance for a file with one
    author and several writers.
    """
    try:
        info = Path(path).stat()
        return (info.st_mtime_ns, info.st_size)
    except OSError:
        return (None, None)


def save(path, entries: list, passthrough: Optional[list] = None,
         comment: Optional[str] = None) -> dict:
    """Back the file up, then write it atomically. Returns what it did.

    The backup is a plain copy to holdings.json.bak, taken before the write and
    only when there is something to back up. It is the only copy of this file
    that exists anywhere - no stage archives it - so a bad edit has to be
    recoverable from the file itself.
    """
    path = Path(path)
    existed = path.exists()
    backup = None
    if existed:
        backup = backup_path(path)
        shutil.copy2(path, backup)

    document = {}
    if comment:
        document["_comment"] = comment
    document["holdings"] = list(entries) + list(passthrough or [])

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(document, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return {"path": path, "backup": backup, "created": not existed,
            "written": len(entries), "carried_through": len(passthrough or [])}
