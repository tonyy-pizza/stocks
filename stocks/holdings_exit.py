#!/usr/bin/env python3
r"""
holdings_exit.py - the weekly exit pass over what you already own.

Stages 0-3 are all about getting in. This is the other half: once a week, put
every position in data\holdings.json back through the same Stage 1 scoring the
candidates get, and ask four questions the entry pipeline never asks.

  thesis_broken     the reason for owning it has stopped being true - the
                    divergence pattern has turned into trend_confirms_decline,
                    or the Piotroski F-Score has fallen below 5, or Altman Z
                    has dropped into the Distress zone since entry
  thesis_completed  the Valuation dimension has normalised to 6.5+ - the name
                    is no longer meaningfully cheap, so the quality-dip thesis
                    has played out and the position is now a judgement call
                    about a fairly priced business
  stop_loss         price is more than 15% below entry_price
  reassess          held longer than 8 weeks with nothing improving - a flag
                    to go and look, NOT a sell signal

The result goes to data\exit_signals.json and a summary to the terminal.

The four are evaluated independently and a position can trip more than one:
"down 18% and the Piotroski score collapsed" is a different situation from
"down 18% and the fundamentals are unchanged", and collapsing them into one
verdict would hide exactly that difference.

Nothing here re-implements scoring. score_candidate() from stock_evaluator.py
runs calc_metrics / piotroski / altman_z / build_scores / divergence_pattern
over market_data.py's cache and backoff, holdings.json is read through
position_sizer.load_holdings() so both files agree on the format, and the
"at entry" side of every comparison comes from data\archive\<run>\, the
snapshots scan_report.py already writes.

v5.5 can leave a dimension - or the whole composite - unmeasured rather than
scoring it a neutral 5.0. Nothing here reads an unmeasured number as a low
one: a trigger that needs it is skipped and says so, and the coverage behind
the Valuation score travels with it into the output.

entry_date and entry_price are optional in holdings.json. Without them a
position still gets its thesis_broken and thesis_completed checks - those are
about the business - but stop_loss and reassess are skipped and the position
is noted as "insufficient entry data" rather than being measured against a
guessed entry.

Setup:
    pip install yfinance curl_cffi requests numpy pandas

Usage:
    py holdings_exit.py                       # evaluate, print, write the JSON
    py holdings_exit.py --stop-loss 20 --reassess-weeks 12
    py holdings_exit.py --refresh             # ignore cached prices
    py holdings_exit.py --quiet
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path
from typing import Optional


# Identical in every stage, and not factorable into stocks_common: it is what
# makes stocks_common importable in the first place.
def _add_project_dir_to_path():
    here = Path(__file__).resolve().parent
    for candidate in (here, here.parent / "stocks", here.parent):
        if (candidate / "market_data.py").exists():
            if str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
            return


_add_project_dir_to_path()

import market_data as md                                        # noqa: E402
import stocks_common as common                                  # noqa: E402
import position_sizer as ps                                     # noqa: E402
from stock_evaluator import (score_candidate, DIVERGENCE_LOW_POS,  # noqa: E402
                             LOW_COVERAGE,                         # v5.5 coverage
                             G, Y, R, X)                          # same palette


# -------------------------------------------------------------------------
# CONFIG
# -------------------------------------------------------------------------

# Bump when this stage would produce a different answer from the same inputs -
# a threshold change, a new trigger, a changed default. See
# stocks_common.LOGIC_VERSION_KEY.
EXIT_VERSION = "1.0"

DATA_DIR = common.data_dir(md.BASE_DIR)
HOLDINGS_PATH = ps.HOLDINGS_PATH
SENTIMENT_PATH = ps.SENTIMENT_PATH
ARCHIVE_DIR = DATA_DIR / "archive"
OUTPUT_PATH = DATA_DIR / "exit_signals.json"

# How often this is meant to run. Nothing enforces it - a position does not
# stop deteriorating because you skipped a week - but the run reports how long
# it has been, because a "weekly" review that last ran in March is worth
# knowing about.
CADENCE_DAYS = 7

# ── Trigger thresholds ────────────────────────────────────────────────────
# piotroski() labels 0-3 Weak, 4-6 Neutral, 7+ Strong. Five is the middle of
# that neutral band: at four or fewer, more of the nine accounting signals are
# failing than passing. build_scores() already feeds the F-Score into the
# composite, but a holding can keep a respectable composite on momentum alone
# while this quietly halves, so it is checked in its own right.
PIOTROSKI_FLOOR = 5

# altman_z() zones: >3.0 Safe, >1.8 Grey, else Distress.
DISTRESS_ZONE = "Distress"

# The Valuation dimension is already sector-relative (mrules() sets the P/E,
# EV/EBITDA and P/B bands per sector), so 6.5 means "no longer cheap FOR THIS
# SECTOR" rather than cheap in the abstract. It sits above the 6.0 line
# build_scores() uses for its valuation penalty and below the 7.0 that would
# only trip on an outright expensive name.
#
# Left at 6.5 across the v5.5 rescale on purpose. RATING_BANDS moved because a
# COMPOSITE built from the same fundamentals lands ~10% lower now; a single
# dimension's score is read directly off score_opt(), where 6.5 still means
# "about two thirds of the way from the sector's bad band to its good one" -
# for a default sector, a P/E near 29. --valuation moves it.
VALUATION_NORMALISED = 6.5

# Down more than this from entry_price. A percentage, not a fraction.
STOP_LOSS_PCT = 15.0

# Held this long with nothing improving.
REASSESS_WEEKS = 8

# How much the public read has to move to count as improvement. The
# sentiment.json composite is 0-10 centred on 5.0 and is noisy week to week;
# anything smaller than this is not a change of narrative.
SENTIMENT_IMPROVEMENT = 0.5

# How far from the entry date an archived scan can be and still be described
# as the state "at entry" without comment. Scans are weekly at best, so a few
# weeks either side is normal; a baseline from three months away is still
# better than nothing, but it gets said out loud.
BASELINE_WINDOW_DAYS = 45

NO_HOLDINGS_NOTE = "no holdings on file"
INSUFFICIENT_ENTRY = "insufficient entry data"

TRIGGERS = ("thesis_broken", "thesis_completed", "stop_loss", "reassess")

TRIGGER_COLOUR = {"thesis_broken": R, "stop_loss": R,
                  "thesis_completed": G, "reassess": Y}


_num = common.num


def _date(value) -> Optional[dt.date]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text[:10])
    except ValueError:
        return None


# -------------------------------------------------------------------------
# WHAT THE PIPELINE THOUGHT AT ENTRY
# -------------------------------------------------------------------------

def archived_runs(archive_dir=None):
    """Every archived scan as (timestamp, directory), newest first.

    scan_report.py writes data\\archive\\<%Y-%m-%dT%H-%M-%S>\\ on any run that
    actually re-ran a stage, which is the only record of what a name looked
    like on the day it was bought.
    """
    archive_dir = Path(archive_dir or ARCHIVE_DIR)
    if not archive_dir.is_dir():
        return []
    runs = []
    for entry in archive_dir.iterdir():
        if not entry.is_dir():
            continue
        try:
            stamp = dt.datetime.strptime(entry.name, "%Y-%m-%dT%H-%M-%S")
        except ValueError:
            continue
        runs.append((stamp, entry))
    runs.sort(key=lambda pair: pair[0], reverse=True)
    return runs


def load_archived_run(directory, cache=None):
    """One archived run -> the three lookups a baseline can come from.

    A name that was still a candidate at the time appears in
    scored_candidates.json; one that was already owned by then appears only in
    sized_candidates.json's holdings_review, which carries fewer fields. Both
    are read, the fuller record first.
    """
    directory = Path(directory)
    if cache is not None and directory in cache:
        return cache[directory]

    scored = {}
    document = common.read_json(directory / "scored_candidates.json") or {}
    for record in document.get("scored") or []:
        ticker = str(record.get("ticker") or "").strip().upper()
        if ticker:
            scored[ticker] = record

    review = {}
    document = common.read_json(directory / "sized_candidates.json") or {}
    for record in document.get("holdings_review") or []:
        ticker = str(record.get("ticker") or "").strip().upper()
        if ticker:
            review[ticker] = record

    document = common.read_json(directory / "sentiment.json") or {}
    sentiment = document.get("sentiment") or {}

    run = {"scored": scored, "review": review, "sentiment": sentiment}
    if cache is not None:
        cache[directory] = run
    return run


def _baseline_fields(record) -> dict:
    """The comparable half of a scored record. Missing pieces stay None.

    holdings_review rows carry the divergence pattern and the composite but no
    frameworks or dimension scores, so those come back unknown rather than
    zero - "we do not know what the F-Score was at entry" is a different
    statement from "it was 0".
    """
    frameworks = record.get("frameworks") or {}
    piotroski = frameworks.get("piotroski") or {}
    altman = frameworks.get("altman_z") or {}
    dims = record.get("dims") or {}
    return {
        "divergence_pattern": record.get("divergence_pattern"),
        "divergence_detail": record.get("divergence_detail"),
        "piotroski": piotroski.get("score"),
        "altman_z": _num(altman.get("score")),
        "altman_zone": altman.get("zone"),
        "valuation": _num(dims.get("Valuation")),
        "composite": _num(record.get("composite")),
    }


def entry_baseline(ticker, entry_date, runs=None, cache=None):
    """What the pipeline thought of this name around the day it was bought.

    Prefers the last scan on or before entry_date; failing that, the first one
    after it, because a scan a fortnight late still describes the business you
    bought better than nothing does. Either way the offset is reported, so a
    baseline taken three months after entry can be discounted by whoever reads
    the file.

    Returns (baseline dict, note) - the baseline is None when there is nothing
    to compare against.
    """
    entry = _date(entry_date)
    if entry is None:
        return None, "entry_date unknown - no baseline to compare against"

    runs = archived_runs() if runs is None else runs
    if not runs:
        return None, "no archived scans on file - nothing to compare against"

    before = [(stamp, path) for stamp, path in runs if stamp.date() <= entry]
    after = [(stamp, path) for stamp, path in runs if stamp.date() > entry]
    ordered = before + list(reversed(after))     # newest before, then earliest after

    for stamp, path in ordered:
        run = load_archived_run(path, cache)
        record = run["scored"].get(ticker) or run["review"].get(ticker)
        if record is None:
            continue
        baseline = _baseline_fields(record)
        scores = run["sentiment"].get(ticker) or {}
        baseline["sentiment"] = _num(scores.get("overall"))
        baseline["sentiment_confidence"] = _num(scores.get("confidence"))
        baseline["run"] = path.name
        baseline["run_date"] = stamp.date().isoformat()
        offset = (stamp.date() - entry).days
        baseline["days_from_entry"] = offset
        baseline["source"] = "scored" if ticker in run["scored"] else "holdings_review"
        note = None
        if abs(offset) > BASELINE_WINDOW_DAYS:
            note = (f"entry baseline is the {path.name} scan, {abs(offset)} days "
                    f"{'before' if offset < 0 else 'after'} entry")
        return baseline, note

    return None, (f"{ticker} does not appear in any archived scan - "
                  f"no baseline to compare against")


# -------------------------------------------------------------------------
# CURRENT STATE
# -------------------------------------------------------------------------

def metrics_summary(record, sentiment_scores=None) -> dict:
    """The handful of numbers the triggers actually read, in one flat dict."""
    frameworks = record.get("frameworks") or {}
    piotroski = frameworks.get("piotroski") or {}
    altman = frameworks.get("altman_z") or {}
    dims = record.get("dims") or {}
    metrics = record.get("metrics") or {}
    scores = sentiment_scores or {}
    return {
        "composite": _num(record.get("composite")),
        "rating": record.get("rating"),
        "valuation": _num(dims.get("Valuation")),
        "dims": dims,
        # v5.5 can leave a dimension - or the whole composite - unmeasured
        # rather than scoring it a neutral 5.0, so how much of each was
        # actually read travels with the number.
        "coverage": _num(record.get("coverage")),
        "valuation_coverage": _num((record.get("dim_coverage") or {}).get("Valuation")),
        "piotroski": piotroski.get("score"),
        "piotroski_label": piotroski.get("label"),
        "altman_z": _num(altman.get("score")),
        "altman_zone": altman.get("zone"),
        "divergence_pattern": record.get("divergence_pattern"),
        "pos_52w": _num(metrics.get("pos_52w")),
        "price": _num(metrics.get("price")),
        "quote_currency": metrics.get("quote_currency"),
        "sentiment": _num(scores.get("overall")),
        "sentiment_confidence": _num(scores.get("confidence")),
        "financials_as_of": record.get("financials_as_of"),
        "price_as_of": record.get("price_as_of"),
        "warnings": record.get("warnings") or [],
    }


def _pattern_rank(pattern, detail) -> Optional[int]:
    """Order the divergence patterns from the point of view of an owner.

    For a candidate, price_disconnect is the interesting one - cheap with the
    fundamentals intact. For something already owned the ordering is different:
    the thesis working looks like the price leaving the bottom of its 52-week
    range, and above that low_pos line divergence_pattern() stops looking for a
    pattern and returns "neutral". So neutral only outranks a disconnect when the
    price really has recovered off the low; neutral while still pinned at the
    low is mixed signals, not progress, and neutral with no 52-week position at
    all says nothing either way.
    """
    if pattern == "trend_confirms_decline":
        return 0
    if pattern == "price_disconnect":
        return 1
    if pattern == "neutral":
        detail = detail or {}
        pos = _num(detail.get("pos_52w"))
        low = _num(detail.get("low_threshold"))
        low = DIVERGENCE_LOW_POS if low is None else low
        if pos is None:
            return None
        return 2 if pos > low else 1
    return None


def _divergence_improved(current, baseline):
    """(improved?, why) for the divergence pattern since entry."""
    now = _pattern_rank(current.get("divergence_pattern"),
                        current.get("divergence_detail"))
    then = _pattern_rank(baseline.get("divergence_pattern"),
                         baseline.get("divergence_detail"))
    if now is None or then is None:
        return None, "divergence pattern not comparable to entry"
    if now > then:
        return True, (f"divergence pattern improved "
                      f"{baseline.get('divergence_pattern')} -> "
                      f"{current.get('divergence_pattern')}")
    return False, (f"divergence pattern no better than entry "
                   f"({baseline.get('divergence_pattern')} -> "
                   f"{current.get('divergence_pattern')})")


def _sentiment_improved(summary, baseline):
    """(improved?, why) for the public read since entry.

    Sentiment only gets a vote when it is confident enough to be worth
    listening to - the same bar position_sizer.py applies before letting it
    change position size.
    """
    now = _num(summary.get("sentiment"))
    then = _num(baseline.get("sentiment"))
    if now is None or then is None:
        return None, "no sentiment on file for both dates"
    confidence = _num(summary.get("sentiment_confidence"))
    if confidence is not None and confidence < ps.SENTIMENT_MIN_CONFIDENCE:
        return None, (f"sentiment confidence {confidence:.1f} below "
                      f"{ps.SENTIMENT_MIN_CONFIDENCE:.1f}; not counted")
    delta = now - then
    if delta >= SENTIMENT_IMPROVEMENT:
        return True, f"sentiment improved {then:.1f} -> {now:.1f}"
    return False, f"sentiment {then:.1f} -> {now:.1f}, no material improvement"


# -------------------------------------------------------------------------
# THE FOUR TRIGGERS
# -------------------------------------------------------------------------

def check_thesis_broken(summary, baseline):
    """Has the reason for owning this stopped being true?

    Three independent ways in, any one of which is enough. Two of them are
    "since entry" comparisons: a name that was already a value trap or already
    in the Distress zone on the day it was bought has not broken since, and
    saying so is more useful than firing the same trigger every week forever.
    """
    reasons, notes = [], []
    baseline = baseline or {}

    pattern = summary.get("divergence_pattern")
    if pattern == "trend_confirms_decline":
        was = baseline.get("divergence_pattern")
        if was == "trend_confirms_decline":
            notes.append("divergence pattern was already trend_confirms_decline "
                         "at entry - not a change")
        elif was in ("price_disconnect", "neutral"):
            reasons.append(f"divergence pattern turned {was} -> "
                           f"trend_confirms_decline: price near its 52-week low "
                           f"and the multi-year trend is deteriorating")
        else:
            reasons.append("divergence pattern is trend_confirms_decline "
                           "(no entry baseline to compare against)")

    piotroski = summary.get("piotroski")
    if isinstance(piotroski, (int, float)) and piotroski < PIOTROSKI_FLOOR:
        was = baseline.get("piotroski")
        since = f" (was {was}/9 at entry)" if isinstance(was, (int, float)) else ""
        reasons.append(f"Piotroski F-Score {piotroski:.0f}/9 below "
                       f"{PIOTROSKI_FLOOR}{since}")

    if summary.get("altman_zone") == DISTRESS_ZONE:
        was = baseline.get("altman_zone")
        z = summary.get("altman_z")
        shown = "n/a" if z is None else f"{z:.2f}"
        if was == DISTRESS_ZONE:
            notes.append("Altman Z was already in the Distress zone at entry - "
                         "not a change")
        elif was:
            reasons.append(f"Altman Z {shown} moved {was} -> {DISTRESS_ZONE}")
        else:
            reasons.append(f"Altman Z {shown} in the {DISTRESS_ZONE} zone "
                           f"(no entry baseline to compare against)")

    return bool(reasons), reasons, notes


def check_thesis_completed(summary, baseline, valuation_normalised=VALUATION_NORMALISED):
    """Has the discount closed? Then the quality-dip thesis is done."""
    valuation = _num(summary.get("valuation"))
    if valuation is None:
        # v5.5 leaves a dimension unmeasured rather than scoring it 5.0, and
        # "we could not price it" is not "it is still cheap".
        return False, [], ["Valuation dimension unmeasured - thesis_completed "
                           "not evaluated"]
    if valuation < valuation_normalised:
        return False, [], []
    was = _num((baseline or {}).get("valuation"))
    since = f" (was {was:.2f} at entry)" if was is not None else ""
    thin = _num(summary.get("valuation_coverage"))
    thin = ("" if thin is None or thin >= LOW_COVERAGE
            else f", though only {thin*100:.0f}% of its inputs were available")
    return True, [f"Valuation dimension {valuation:.2f} at or above "
                  f"{valuation_normalised} - no longer meaningfully cheap for its "
                  f"sector{since}{thin}"], []


def check_stop_loss(summary, entry_price, stop_loss_pct=STOP_LOSS_PCT):
    """(triggered, reasons, notes, pct_change_from_entry).

    Only ever computed against a real entry_price. Cost basis is not a
    substitute: on a position that has been added to it is an average of
    several days, and a stop measured off an average is not a stop.
    """
    entry_price = _num(entry_price)
    price = _num(summary.get("price"))
    if entry_price is None or entry_price <= 0:
        return False, [], [f"{INSUFFICIENT_ENTRY}: entry_price unknown, "
                           f"stop_loss not evaluated"], None

    if price is None:
        return False, [], ["current price unavailable, stop_loss not evaluated"], None

    pct = (price - entry_price) / entry_price * 100.0
    pct = round(pct, 2)
    if pct < -stop_loss_pct:
        return True, [f"price {price:g} is {abs(pct):.1f}% below the entry price "
                      f"{entry_price:g} (limit {stop_loss_pct:.0f}%)"], [], pct
    return False, [], [], pct


def check_reassess(summary, baseline, days_held, reassess_weeks=REASSESS_WEEKS):
    """Long-held and nothing has improved - go and look at it yourself.

    Deliberately the weakest of the four: it does not say the thesis is wrong,
    only that it has had two months to start working and has not. Improvement
    in either the divergence pattern or the public read is enough to clear it.
    """
    if days_held is None:
        return False, [], [f"{INSUFFICIENT_ENTRY}: entry_date unknown, "
                           f"reassess not evaluated"]


    limit_days = reassess_weeks * 7
    if days_held <= limit_days:
        return False, [], []

    weeks = days_held / 7.0
    if not baseline:
        return True, [f"held {weeks:.0f} weeks (over {reassess_weeks}) and there is "
                      f"no entry baseline to show whether anything has improved"], []

    divergence_ok, divergence_why = _divergence_improved(
        {"divergence_pattern": summary.get("divergence_pattern"),
         "divergence_detail": summary.get("divergence_detail")}, baseline)
    sentiment_ok, sentiment_why = _sentiment_improved(summary, baseline)

    if divergence_ok or sentiment_ok:
        return False, [], [f"held {weeks:.0f} weeks; "
                           + (divergence_why if divergence_ok else sentiment_why)]

    return True, [f"held {weeks:.0f} weeks (over {reassess_weeks}) with no "
                  f"improvement since entry: {divergence_why}; {sentiment_why}"], []


# -------------------------------------------------------------------------
# ONE HOLDING
# -------------------------------------------------------------------------

def evaluate_holding(holding, record, baseline, baseline_note, sentiment_scores,
                     today=None, stop_loss_pct=STOP_LOSS_PCT,
                     reassess_weeks=REASSESS_WEEKS,
                     valuation_normalised=VALUATION_NORMALISED):
    """Run the four triggers over one holding and build its output row."""
    today = today or dt.date.today()
    ticker = holding["ticker"]
    entry_date = _date(holding.get("entry_date"))
    entry_price = _num(holding.get("entry_price"))
    days_held = None if entry_date is None else (today - entry_date).days

    summary = metrics_summary(record, sentiment_scores)
    # _divergence_improved() reads the detail off the same dict as the pattern.
    summary_for_checks = dict(summary)
    summary_for_checks["divergence_detail"] = record.get("divergence_detail")

    triggers, reasons, notes = [], {}, []
    if baseline_note:
        notes.append(baseline_note)

    fired, why, note = check_thesis_broken(summary_for_checks, baseline)
    notes.extend(note)
    if fired:
        triggers.append("thesis_broken")
        reasons["thesis_broken"] = why

    fired, why, note = check_thesis_completed(
        summary_for_checks, baseline, valuation_normalised=valuation_normalised)
    notes.extend(note)
    if fired:
        triggers.append("thesis_completed")
        reasons["thesis_completed"] = why

    fired, why, note, pct_change = check_stop_loss(
        summary_for_checks, entry_price, stop_loss_pct=stop_loss_pct)
    notes.extend(note)
    if fired:
        triggers.append("stop_loss")
        reasons["stop_loss"] = why

    fired, why, note = check_reassess(summary_for_checks, baseline, days_held,
                                      reassess_weeks=reassess_weeks)
    notes.extend(note)
    if fired:
        triggers.append("reassess")
        reasons["reassess"] = why

    return {
        "ticker": ticker,
        "status": "evaluated",
        "name": record.get("name"),
        "sector": record.get("sector"),
        "triggers": triggers,
        "trigger_reasons": reasons,
        "notes": notes,
        "current_metrics_summary": summary,
        "days_held": days_held,
        "pct_change_from_entry": pct_change,
        "entry": {
            "entry_date": holding.get("entry_date"),
            "entry_price": entry_price,
            "shares": holding.get("shares"),
            "cost_basis": holding.get("cost_basis"),
        },
        "entry_baseline": baseline,
    }


def unavailable_holding(holding, reason, today=None):
    """A holding that could not be re-scored still gets a row, and says why."""
    today = today or dt.date.today()
    entry_date = _date(holding.get("entry_date"))
    return {
        "ticker": holding["ticker"],
        "status": "unavailable",
        "name": None,
        "sector": None,
        "triggers": [],
        "trigger_reasons": {},
        "notes": [f"could not be re-scored: {reason}"],
        "current_metrics_summary": None,
        "days_held": None if entry_date is None else (today - entry_date).days,
        "pct_change_from_entry": None,
        "entry": {
            "entry_date": holding.get("entry_date"),
            "entry_price": _num(holding.get("entry_price")),
            "shares": holding.get("shares"),
            "cost_basis": holding.get("cost_basis"),
        },
        "entry_baseline": None,
    }


# -------------------------------------------------------------------------
# RUN
# -------------------------------------------------------------------------

def _write(document, output_path):
    """Stamp the logic version and write atomically, as every stage does."""
    common.stamp_logic_version(document, EXIT_VERSION)
    return common.write_json(document, output_path, default=common.json_default)


def _previous_run(output_path, today):
    """When this last ran, and how long ago - the cadence is the point."""
    document = common.read_json(output_path)
    if not document:
        return None
    generated = str(document.get("generated_at") or "")
    stamp = _date(generated)
    if stamp is None:
        return {"generated_at": generated or None, "days_ago": None}
    return {"generated_at": generated, "days_ago": (today - stamp).days}


def evaluate_holdings(holdings_path=None, output_path=None, sentiment_path=None,
                      archive_dir=None, stop_loss_pct=STOP_LOSS_PCT,
                      reassess_weeks=REASSESS_WEEKS,
                      valuation_normalised=VALUATION_NORMALISED, account_size=None,
                      force_refresh=False, quiet=False):
    """Score every holding, apply the four triggers, write exit_signals.json."""
    holdings_path = Path(holdings_path) if holdings_path else HOLDINGS_PATH
    output_path = Path(output_path) if output_path else OUTPUT_PATH
    sentiment_path = Path(sentiment_path) if sentiment_path else SENTIMENT_PATH
    today = dt.date.today()

    # create_template=False: a read-only review should not conjure the file it
    # is reviewing. Missing and empty both mean the same thing here.
    holdings, _ = ps.load_holdings(holdings_path, quiet=True, create_template=False)

    note = None
    if not holdings:
        note = NO_HOLDINGS_NOTE
        if holdings_path.exists() and common.read_json(holdings_path) is None:
            # An unreadable file is still "no holdings", but silently treating
            # a typo in the JSON as an empty portfolio would be a bad week.
            note = f"{NO_HOLDINGS_NOTE} - {holdings_path} could not be parsed"

    previous = _previous_run(output_path, today)
    document = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "note": note,
        "holdings_on_file": len(holdings),
        "params": {
            "holdings_input": str(holdings_path),
            "sentiment_input": str(sentiment_path) if sentiment_path.exists() else None,
            "archive_dir": str(Path(archive_dir or ARCHIVE_DIR)),
            "stop_loss_pct": stop_loss_pct,
            "reassess_weeks": reassess_weeks,
            "valuation_normalised": valuation_normalised,
            "piotroski_floor": PIOTROSKI_FLOOR,
            "distress_zone": DISTRESS_ZONE,
            "cadence_days": CADENCE_DAYS,
            "previous_run": previous,
        },
        "counts": {name: 0 for name in TRIGGERS},
        "evaluated": [],
    }

    if not holdings:
        _write(document, output_path)
        if not quiet:
            print(f"\n  {Y}{note}{X} - nothing to evaluate ({holdings_path})")
            print(f"  wrote: {output_path}\n")
        return document

    sentiment = (common.read_json(sentiment_path) or {}).get("sentiment") or {}
    runs = archived_runs(archive_dir)
    cache = {}

    if not quiet:
        print(f"\n  exit review: {len(holdings)} holding(s)  ·  "
              f"{len(runs)} archived scan(s) to compare against")
        if previous and previous.get("days_ago") is not None:
            days_ago = previous["days_ago"]
            overdue = ("" if days_ago <= CADENCE_DAYS
                       else f"  {Y}(past the {CADENCE_DAYS}-day cadence){X}")
            print(f"  last run {days_ago}d ago{overdue}")

    rows = []
    for holding in holdings:
        ticker = holding["ticker"]
        record, reason = score_candidate(ticker, account_size=account_size,
                                         force_refresh=force_refresh)
        if record is None:
            rows.append(unavailable_holding(holding, reason or "unknown error", today))
            if not quiet:
                print(f"    {ticker:<8} {Y}unavailable{X} - {reason}")
            continue

        baseline, baseline_note = entry_baseline(
            ticker, holding.get("entry_date"), runs, cache)
        row = evaluate_holding(holding, record, baseline, baseline_note,
                               sentiment.get(ticker), today=today,
                               stop_loss_pct=stop_loss_pct,
                               reassess_weeks=reassess_weeks,
                               valuation_normalised=valuation_normalised)
        rows.append(row)
        if not quiet:
            print_row(row)

    for row in rows:
        for name in row["triggers"]:
            document["counts"][name] += 1
    document["evaluated"] = rows
    _write(document, output_path)

    if not quiet:
        flagged = [r for r in rows if r["triggers"]]
        counts = "  ·  ".join(f"{name} {document['counts'][name]}"
                              for name in TRIGGERS)
        print(f"\n  {len(flagged)} of {len(rows)} position(s) flagged  ·  {counts}")
        print("  reassess is a flag to go and look, not a sell signal")
        print(f"\n  wrote: {output_path}\n")

    return document


def print_row(row):
    """One line per holding, then the reasons underneath it."""
    summary = row["current_metrics_summary"] or {}
    held = "     -" if row["days_held"] is None else f"{row['days_held']:>5}d"
    change = ("      -" if row["pct_change_from_entry"] is None
              else f"{row['pct_change_from_entry']:>+6.1f}%")
    composite = summary.get("composite")
    composite = "  -  " if composite is None else f"{composite:>5.2f}"
    if row["triggers"]:
        labels = ", ".join(f"{TRIGGER_COLOUR.get(name, '')}{name}{X}"
                           for name in row["triggers"])
    else:
        labels = f"{G}hold{X}"
    print(f"    {row['ticker']:<8} {composite}  {held}  {change}   {labels}")
    for name in row["triggers"]:
        for reason in row["trigger_reasons"].get(name, []):
            print(f"             {name}: {reason}")
    for note in row["notes"]:
        print(f"             note: {note}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Weekly exit review of the positions in holdings.json.")
    parser.add_argument("--holdings", metavar="PATH", help=f"default {HOLDINGS_PATH}")
    parser.add_argument("--output", metavar="PATH", help=f"default {OUTPUT_PATH}")
    parser.add_argument("--sentiment", metavar="PATH",
                        help=f"sentiment input (default {SENTIMENT_PATH})")
    parser.add_argument("--archive", metavar="PATH",
                        help=f"archived scans to take entry baselines from "
                             f"(default {ARCHIVE_DIR})")
    parser.add_argument("--stop-loss", type=float, default=STOP_LOSS_PCT,
                        metavar="PCT",
                        help=f"percent below entry_price that trips the stop "
                             f"(default {STOP_LOSS_PCT:.0f})")
    parser.add_argument("--reassess-weeks", type=float, default=REASSESS_WEEKS,
                        metavar="WEEKS",
                        help=f"weeks held with no improvement before a position is "
                             f"flagged for review (default {REASSESS_WEEKS})")
    parser.add_argument("--valuation", type=float, default=VALUATION_NORMALISED,
                        metavar="SCORE",
                        help=f"Valuation dimension at or above which the discount "
                             f"counts as closed (default {VALUATION_NORMALISED})")
    parser.add_argument("--account-size", type=float, metavar="AMOUNT",
                        help="account size for the liquidity check in the re-score")
    parser.add_argument("--refresh", action="store_true", help="ignore cached prices")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    if args.verbose:
        md.DEBUG = True

    evaluate_holdings(holdings_path=args.holdings, output_path=args.output,
                      sentiment_path=args.sentiment, archive_dir=args.archive,
                      stop_loss_pct=args.stop_loss,
                      reassess_weeks=args.reassess_weeks,
                      valuation_normalised=args.valuation,
                      account_size=args.account_size,
                      force_refresh=args.refresh, quiet=args.quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
