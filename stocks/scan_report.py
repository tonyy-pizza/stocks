#!/usr/bin/env python3
r"""
scan_report.py - run the whole scan and render the Tier 1 summary.

    universe_screen  ->  stock_evaluator --batch  ->  sentiment (survivors only)
                     ->  rmt_cluster  ->  position_sizer  ->  entry_timing
                     ->  holdings_exit  ->  this report

The report has two halves: BUY SIGNALS, the candidate list, each name tagged
with entry_timing's read on whether its price has actually turned; and SELL /
REVIEW SIGNALS, the holdings holdings_exit found an exit trigger on. Names
whose fundamentals cleared but whose price is still falling stay in the buy
list - they passed - but grouped below the ones timed for entry this week.

Each stage reads the previous stage's file out of C:\Users\joey\stocks\data\.
A stage is skipped when its output is still fresh by market_data.py's TTL
policy, so a second run in the same day re-renders from what is on disk
instead of re-scanning the market. --force re-runs everything.

Freshness cascades: if a stage does re-run, every stage after it re-runs too,
because their inputs just changed. A fresh file downstream of a stale one is
not actually fresh.

Failures cascade too. Each stage's exit code is checked, and a required stage
that fails - or that returns 0 without actually writing its output, which is
what universe_screen's abort path looks like from here - stops the run instead
of letting the next stage read yesterday's file and report it as today's. The
report is still rendered from what is on disk, with a note saying so, and the
process exits non-zero. Sentiment is the one optional stage: it fails routinely
when Reddit rate-limits, is reported, and does not halt anything.

Nothing here re-implements a stage. Each is invoked through its own entry
point (universe_screen.main, stock_evaluator.batch_main, rmt_cluster.main,
position_sizer.main, entry_timing.evaluate_timing, holdings_exit.main), and
--detail hands off to stock_evaluator's existing single-ticker report. Paths,
atomic JSON writes and number coercion come from stocks_common.py rather than
being copied in.

The two new stages are optional: neither is an input to anything else, so a
failure in either is reported and does not halt the scan or change the exit
code. entry_timing.py in particular may not be on disk at all, which is why it
is imported tolerantly and its absence reads as "no timing flags this run"
rather than as a broken pipeline.

Every completed run is snapshotted to data\archive\<timestamp>\, and the
newest --archive-keep of those are retained (30 by default). A snapshot is a
full copy of the run's JSON files and scored_candidates.json alone runs to tens
of megabytes on a few-thousand-name universe, so without a bound the archive
becomes the largest thing in the project. Only the most recent run is ever read
back, by position_sizer's exit review.

Setup:
    pip install yfinance curl_cffi requests numpy pandas

Usage:
    py scan_report.py                    # run what is stale, render the report
    py scan_report.py --force            # re-run every stage
    py scan_report.py --detail AAPL      # full single-ticker report instead
    py scan_report.py --include-canada --top 40
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import inspect
import json
import math
import os
import re
import shutil
import sys
import time
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
            return here
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    return here


SCRIPT_DIR = _add_project_dir_to_path()

import market_data as md                     # noqa: E402
import stocks_common as common               # noqa: E402
import universe_screen                       # noqa: E402
import stock_evaluator                       # noqa: E402
import rmt_cluster                           # noqa: E402
import position_sizer                        # noqa: E402
import holdings_exit                         # noqa: E402

# entry_timing.py is the one stage that may not be on disk. Every other import
# above is a hard dependency - the pipeline cannot run without it - but the
# timing stage only annotates a candidate list that is already complete, so its
# absence costs the tags and nothing else. Imported here rather than inside the
# stage so the failure is named once, at startup, instead of guessed at later.
try:
    import entry_timing                      # noqa: E402
except Exception as exc:                     # noqa: BLE001
    entry_timing, ENTRY_TIMING_IMPORT_ERROR = None, f"{type(exc).__name__}: {exc}"
else:
    ENTRY_TIMING_IMPORT_ERROR = None


# ─── DISPLAY ───────────────────────────────────────────────────────────────
# Same conventions as stock_evaluator.py / etf.py / op.py / ps2.py.
if sys.platform == "win32":
    os.system("color")

G, Y, R, C, B, D, X = ("\033[92m", "\033[93m", "\033[91m", "\033[96m",
                       "\033[1m", "\033[2m", "\033[0m")


def colour(v, t=None):
    """stock_evaluator.colour, thresholds taken from it rather than copied."""
    if v is None:
        return f"{D}  n/a{X}"
    t = t if t is not None else f"{v:.2f}"
    if v >= stock_evaluator.COLOUR_GOOD: return f"{G}{B}{t}{X}"
    return f"{Y}{t}{X}" if v >= stock_evaluator.COLOUR_FAIR else f"{R}{t}{X}"


def bar(s, w=14):
    """stock_evaluator.bar, same fill characters."""
    s = max(0, min(10, s or 0))
    f = int(round((s / 10) * w))
    return "█" * f + "░" * (w - f)


def rule(ch="═", width=76):
    print("  " + ch * width)


def h(text):
    print(f"  {B}{text}{X}")


# ─── CONFIG ────────────────────────────────────────────────────────────────

DATA_DIR = common.data_dir(md.BASE_DIR)

CANDIDATES = DATA_DIR / "candidates.json"
SCORED = DATA_DIR / "scored_candidates.json"
SENTIMENT = DATA_DIR / "sentiment.json"
CLUSTERED = DATA_DIR / "clustered.json"
SIZED = DATA_DIR / "sized_candidates.json"
TIMING = DATA_DIR / "timing_flags.json"
EXIT_SIGNALS = DATA_DIR / "exit_signals.json"
RUN_STATUS = DATA_DIR / "run_status.json"
ARCHIVE_DIR = DATA_DIR / "archive"

# How many survivors get a sentiment pull, and how hard that stage is allowed
# to push.
#
# Sentiment is the most fragile stage in the pipeline and the arithmetic is
# worth stating: ps2.py issues up to 12 subreddit targets x 4 queries = 48
# subreddit requests plus ~5-10 global searches per ticker, against an
# unauthenticated endpoint that rate-limits. At 25 names that is ~1,375
# requests in one run. 15 is a more honest default; raise it deliberately.
# Bump when this stage would produce a different answer from the same
# inputs - scan_report re-runs it when the stamp on its output does not
# match. See stocks_common.LOGIC_VERSION_KEY.
SENTIMENT_VERSION = "1.0"

SENTIMENT_TOP = 15

# Pause between tickers. The per-request sleeps inside ps2.py pace one
# ticker's queries; this paces the tickers themselves.
SENTIMENT_PAUSE = 2.0

# Stop the stage after this many consecutive failures. Once Reddit starts
# refusing, grinding through the rest of the list just adds traffic to an
# endpoint that has already said no.
SENTIMENT_MAX_CONSECUTIVE_FAILURES = 3

# Sector ETF suggested when a whole cluster scores alike - the industry call
# is then the decision, not the name.
SECTOR_ETF_US = {
    "Technology": "XLK", "Financial Services": "XLF", "Energy": "XLE",
    "Industrials": "XLI", "Healthcare": "XLV", "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP", "Utilities": "XLU", "Basic Materials": "XLB",
    "Real Estate": "XLRE", "Communication Services": "XLC",
}
SECTOR_ETF_CA = {
    "Technology": "XIT.TO", "Financial Services": "XFN.TO", "Energy": "XEG.TO",
    "Basic Materials": "XMA.TO", "Consumer Defensive": "XST.TO",
    "Utilities": "XUT.TO", "Real Estate": "XRE.TO",
}

DISCLAIMER = "⚠  For informational use only. Not financial advice."


_num = common.num


# ─── FRESHNESS ─────────────────────────────────────────────────────────────

read_json = common.read_json


def file_age_seconds(path):
    """Age from the file's own generated_at, falling back to its mtime.

    generated_at is when the data was produced; mtime only says when the file
    was last touched.
    """
    path = Path(path)
    if not path.exists():
        return None
    document = read_json(path)
    stamp = (document or {}).get("generated_at")
    if stamp:
        try:
            when = dt.datetime.fromisoformat(str(stamp))
            if when.tzinfo is not None:
                when = when.astimezone().replace(tzinfo=None)
            return (dt.datetime.now() - when).total_seconds()
        except Exception:
            pass
    try:
        return (dt.datetime.now()
                - dt.datetime.fromtimestamp(path.stat().st_mtime)).total_seconds()
    except OSError:
        return None


def is_fresh(path, ttl):
    age = file_age_seconds(path)
    return age is not None and age < ttl


def describe_age(seconds):
    if seconds is None:
        return "missing"
    if seconds < 90:
        return f"{seconds:.0f}s old"
    if seconds < 5400:
        return f"{seconds/60:.0f}m old"
    if seconds < 172800:
        return f"{seconds/3600:.0f}h old"
    return f"{seconds/86400:.1f}d old"


# ─── SENTIMENT (ps2.py, looped over survivors) ─────────────────────────────

def load_sentiment_module():
    """Import the sentiment evaluator from wherever it lives.

    Its folder name can contain a space, so it is loaded by file path rather
    than by module name.
    """
    names = ("ps2.py", "ps.py", "public_sentiment.py")
    roots = (SCRIPT_DIR, SCRIPT_DIR.parent, SCRIPT_DIR.parent / "evaluation scripts",
             SCRIPT_DIR / "evaluation scripts")
    for root in roots:
        for name in names:
            path = Path(root) / name
            if path.exists():
                spec = importlib.util.spec_from_file_location("sentiment_module", path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                if hasattr(module, "analyze_public_sentiment_yahoo_reddit"):
                    return module, path
    return None, None


def run_sentiment_stage(tickers, output_path=SENTIMENT, force_refresh=False, quiet=False):
    """Sentiment for the survivors, one ticker at a time, cached per ticker.

    The evaluator itself is reused as-is; results go through market_data's
    cache so a re-run inside the TTL does not hit Reddit again. One ticker
    failing is recorded and stepped over.
    """
    module, path = load_sentiment_module()
    document = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "source": str(path) if path else None,
        "sentiment": {},
        "skipped": [],
        "diagnostics": {},
        "blocked_count": 0,
    }

    if module is None:
        document["skipped"].append({"ticker": None,
                                    "reason": "sentiment evaluator not found "
                                              "(looked for ps2.py / ps.py)"})
        write_stage_json(document, output_path, SENTIMENT_VERSION)
        if not quiet:
            print(f"  {Y}sentiment evaluator not found - skipping that stage{X}")
        return document

    if not quiet:
        print(f"  sentiment: {len(tickers)} survivor(s) via {path.name} "
              f"(~{len(tickers) * 55} reddit requests worst case)")

    consecutive_failures = 0
    for i, ticker in enumerate(tickers, 1):
        def _pull(ticker=ticker):
            # The evaluator's own yfinance calls chatter about cookies/crumbs on
            # stderr; same muting risk.py uses around its downloads.
            devnull, saved = open(os.devnull, "w"), sys.stderr
            if not md.DEBUG:
                sys.stderr = devnull
            try:
                result = module.analyze_public_sentiment_yahoo_reddit(ticker)
            finally:
                sys.stderr = saved
                devnull.close()
            combined = (result or {}).get("combined") or {}
            if combined.get("overall") is None:
                raise ValueError(f"no combined sentiment for {ticker}")
            diagnostics = ((result or {}).get("reddit") or {}).get("diagnostics") or {}
            return {
                "overall": combined.get("overall"),
                "confidence": combined.get("confidence"),
                "sources": combined.get("contributing_sources"),
                "interpretation": combined.get("interpretation"),
                "_diagnostics": {
                    "requests_made": diagnostics.get("requests_made"),
                    "blocked": bool(diagnostics.get("blocked")),
                    "block_reason": diagnostics.get("block_reason"),
                    "queries_issued": len(diagnostics.get("queries_issued") or []),
                    "errors": (diagnostics.get("errors") or [])[:3],
                },
            }

        # max_retries=1 on purpose. Everywhere else in this project a retry is
        # one request; here a single "attempt" is ~55 Reddit requests, so an
        # outer retry multiplies load against an endpoint that is already
        # rate-limiting. ps2.py has its own per-request backoff and its own
        # blocked circuit breaker - that is the right place for retries.
        scores = md.cached_fetch(f"{ticker}_sentiment",
                                 lambda pull=_pull: md.fetch_with_backoff(pull, max_retries=1),
                                 md.TTL_PRICE, cache_type="screener",
                                 force_refresh=force_refresh)
        if scores is None:
            consecutive_failures += 1
            document["skipped"].append({"ticker": ticker, "reason": "sentiment fetch failed"})
            if not quiet:
                print(f"    [{i:>3}/{len(tickers)}] {ticker:<8} {Y}skipped{X}")
            if consecutive_failures >= SENTIMENT_MAX_CONSECUTIVE_FAILURES:
                remaining = tickers[i:]
                document["aborted"] = {
                    "after": ticker,
                    "reason": f"{consecutive_failures} consecutive failures - stopping "
                              f"rather than adding load to an endpoint that is refusing",
                    "not_attempted": remaining,
                }
                for skipped in remaining:
                    document["skipped"].append({"ticker": skipped,
                                                "reason": "not attempted - stage aborted"})
                if not quiet:
                    print(f"  {Y}stopping the sentiment stage: {consecutive_failures} "
                          f"consecutive failures, {len(remaining)} name(s) not attempted{X}")
                break
            continue

        consecutive_failures = 0
        diagnostics = scores.pop("_diagnostics", None) or {}
        if diagnostics:
            document["diagnostics"][ticker] = diagnostics
            if diagnostics.get("blocked"):
                document["blocked_count"] += 1
        document["sentiment"][ticker] = scores
        if not quiet:
            overall = scores.get("overall")
            requests_made = diagnostics.get("requests_made")
            trail = f"  {D}{requests_made} reddit req{X}" if requests_made else ""
            print(f"    [{i:>3}/{len(tickers)}] {ticker:<8} {colour(overall)}{trail}")

        if i < len(tickers) and not md.was_cache_hit():
            time.sleep(SENTIMENT_PAUSE)

    # What the endpoint actually did, so the volume question has an answer
    # after a real run instead of an estimate.
    totals = [d.get("requests_made") for d in document["diagnostics"].values()
              if isinstance(d.get("requests_made"), int)]
    document["request_summary"] = {
        "tickers_attempted": len(document["sentiment"]) + len(document["skipped"]),
        "reddit_requests_total": sum(totals) if totals else None,
        "reddit_requests_per_ticker": round(sum(totals) / len(totals), 1) if totals else None,
        "blocked_tickers": document["blocked_count"],
    }
    write_stage_json(document, output_path, SENTIMENT_VERSION)
    return document


# ─── ENTRY TIMING (entry_timing.py, over the buy candidates) ───────────────

def timing_entry_point():
    """(evaluate_timing, None), or (None, why it cannot be called)."""
    if entry_timing is None:
        return None, (ENTRY_TIMING_IMPORT_ERROR
                      or "entry_timing.py not found next to this script")
    function = getattr(entry_timing, "evaluate_timing", None)
    if not callable(function):
        return None, "entry_timing.py has no evaluate_timing()"
    return function, None


def timing_logic_version():
    """entry_timing's own logic version, when it publishes one.

    None means the stage is judged on its output's age alone - the same
    position every stage was in before versioning existed - rather than being
    re-run every time because a stamp this file invented never matches.
    """
    for name in ("TIMING_VERSION", "ENTRY_TIMING_VERSION", "LOGIC_VERSION"):
        version = getattr(entry_timing, name, None)
        if version:
            return str(version)
    return None


def timing_call_kwargs(function, tickers, output_path, force_refresh, quiet):
    """Offer evaluate_timing everything it might want; drop what it does not take.

    entry_timing.py is developed apart from this file, so its exact signature is
    not knowable from here. Guessing one and calling it would fail on any other,
    and a TypeError raised inside the function is indistinguishable from one
    raised by calling it wrongly. Reading the signature instead is the narrower
    assumption: the only thing assumed is the function's name.
    """
    offered = {
        "tickers": tickers, "candidates": tickers,
        "output_path": output_path, "output": output_path,
        "force_refresh": force_refresh, "refresh": force_refresh,
        "quiet": quiet,
    }
    canonical = ("tickers", "output_path", "force_refresh", "quiet")
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        return {}
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        # **kwargs swallows anything, so send one name per idea rather than
        # both spellings of it.
        return {name: offered[name] for name in canonical}
    return {name: offered[name] for name, p in parameters.items()
            if name in offered
            and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)}


def run_timing_stage(tickers, output_path=TIMING, force_refresh=False, quiet=False):
    """entry_timing.evaluate_timing() over this scan's buy candidates.

    Returns an exit code, like every other stage runner here. A module that
    writes its own output is left to it; one that returns the document instead
    has it written here, so either convention works.

    Raises when entry_timing.py is not there to be called: stage() records the
    message against an optional stage, and the report renders untagged and says
    so. The alternative - writing an empty timing_flags.json - would read as
    fresh on the next run and quietly suppress the tags for a day.
    """
    function, reason = timing_entry_point()
    if function is None:
        raise RuntimeError(reason)

    if not quiet:
        print(f"    entry_timing over {len(tickers)} candidate(s)")

    before = _output_stamp(output_path)
    result = function(**timing_call_kwargs(function, tickers, output_path,
                                           force_refresh, quiet))
    if isinstance(result, int):
        return result
    if isinstance(result, dict) and _output_stamp(output_path) == before:
        result.setdefault("generated_at", dt.datetime.now().isoformat(timespec="seconds"))
        version = timing_logic_version()
        if version:
            write_stage_json(result, output_path, version)
        else:
            write_json(result, output_path)
    return 0


# The flag values this report knows how to draw. Anything else entry_timing
# emits still shows, abbreviated, rather than being silently dropped - a flag
# this file has not been taught about is news, not noise.
TIMING_REVERSAL = "reversal_signal"
TIMING_STILL_FALLING = "still_falling"

_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_FLAG_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_NOT_A_TICKER = {"GENERATED_AT", "NOTE", "NOTES", "PARAMS", "COUNTS", "SOURCE",
                 "VERSION", "LOGIC_VERSION", "INPUTS", "EVALUATED", "TIMING",
                 "FLAGS", "TIMING_FLAGS", "CANDIDATES"}


def _flag_value(value):
    """The flag out of whatever entry_timing recorded against a ticker."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for key in ("timing_flag", "flag", "signal"):
            flag = value.get(key)
            if isinstance(flag, str) and flag.strip():
                return flag.strip()
    return None


def timing_flags(document):
    """timing_flags.json -> {TICKER: flag}.

    entry_timing.py owns this file and is written separately, so the reader
    takes the shapes it could reasonably have - a mapping under "timing",
    "timing_flags" or "flags", a list of records under "evaluated" or
    "candidates", or a bare ticker mapping - and a value that is either the
    flag itself or a record carrying one. A name it cannot read has no flag,
    which is what the report shows for a candidate entry_timing did not cover.
    """
    if not isinstance(document, dict):
        return {}

    flags = {}

    def record(ticker, value):
        ticker = str(ticker or "").strip().upper()
        flag = _flag_value(value)
        if ticker and flag:
            flags[ticker] = flag

    for key in ("timing", "timing_flags", "flags"):
        block = document.get(key)
        if isinstance(block, dict):
            for ticker, value in block.items():
                record(ticker, value)

    for key in ("evaluated", "candidates", "timing", "timing_flags", "flags"):
        block = document.get(key)
        if isinstance(block, list):
            for entry in block:
                if isinstance(entry, dict):
                    record(entry.get("ticker"), entry)

    if not flags:
        # A bare {"AAPL": "still_falling"} file. Guarded on both sides -
        # ticker-shaped key, flag-shaped value - so that "generated_at" and
        # friends cannot be read as positions.
        for ticker, value in document.items():
            key = str(ticker).strip().upper()
            if key in _NOT_A_TICKER or not _TICKER_RE.match(key):
                continue
            if isinstance(value, dict) or (isinstance(value, str)
                                           and _FLAG_RE.match(value.strip())):
                record(key, value)

    return flags


def write_json(document, output_path):
    return common.write_json(document, output_path, default=str)


def write_stage_json(document, output_path, version):
    """write_json, stamped with the logic version that produced it."""
    common.stamp_logic_version(document, version)
    return write_json(document, output_path)


# ─── PIPELINE ──────────────────────────────────────────────────────────────

def _output_stamp(path):
    """(mtime_ns, size) for a stage's output, or None when it is not there.

    Compared either side of a stage to tell "wrote a new file" from "left the
    old one alone", which is the difference between a stage that worked and
    one that quietly did not.
    """
    try:
        info = Path(path).stat()
        return (info.st_mtime_ns, info.st_size)
    except OSError:
        return None


def run_pipeline(args):
    """Run the stale stages in order. Returns a list of per-stage status rows.

    Every stage runner returns its entry point's exit code, and a required
    stage that fails stops the pipeline. That has to be explicit, because the
    failure modes here are quiet ones: universe_screen returns 1 and leaves
    yesterday's candidates.json in place, stock_evaluator returns 2 when its
    input is missing, and neither prints anything a later stage would notice.
    Running on regardless meant the report rendered yesterday's data under
    today's timestamp - the one outcome worse than an error.
    """
    status = []
    downstream_forced = bool(args.force)
    halted = False

    def stage(name, output, ttl, runner, label, required=True, version=None):
        """Run one stage unless its output is still fresh AND current.

        `runner` returns an exit code; anything non-zero, an exception, or a
        missing output file afterwards is a failure. A required stage failing
        halts everything after it, since those stages read what this one was
        supposed to write.

        `version` is the stage's current logic version. A TTL answers "is this
        data old", which is not the question after the code changes: the file
        can be minutes old and still have been produced by logic that no longer
        exists. That was a silent failure - the stage reported "fresh - not
        re-run" and the report re-rendered superseded numbers under today's
        timestamp for the rest of the TTL. A stamp that does not match the
        code on disk now counts as stale, whatever the file's age.

        An unstamped file (written before versioning, or by hand) reads as
        None and also counts as stale. Re-running a stage costs time; showing
        a number no current code would produce costs more.
        """
        nonlocal downstream_forced, halted

        if halted:
            status.append({"stage": name, "action": "not attempted",
                           "age": file_age_seconds(output), "output": str(output),
                           "ok": None})
            if not args.quiet:
                print(f"  {D}·{X} {label:<22} not attempted - an earlier stage failed")
            return False

        age = file_age_seconds(output)
        on_disk = common.output_logic_version(output) if version is not None else None
        outdated = version is not None and age is not None and on_disk != str(version)
        fresh = is_fresh(output, ttl) and not downstream_forced and not outdated
        if fresh:
            status.append({"stage": name, "action": "skipped (fresh)",
                           "age": age, "output": str(output), "ok": True,
                           "logic_version": on_disk})
            if not args.quiet:
                print(f"  {G}✓{X} {label:<22} fresh ({describe_age(age)}) - not re-run")
            return False

        if downstream_forced:
            reason = "forced"
        elif age is None:
            reason = "missing"
        elif outdated:
            reason = (f"logic changed ({on_disk or 'unstamped'} -> {version})")
        else:
            reason = "stale"
        if not args.quiet:
            print(f"  {C}→{X} {label:<22} {reason} - running")

        detail = None
        before = _output_stamp(output)
        try:
            code = runner()
        except Exception as exc:                      # noqa: BLE001
            code, detail = 1, f"{type(exc).__name__}: {exc}"
        else:
            if code not in (0, None):
                detail = f"exit code {code}"
            elif _output_stamp(output) == before:
                # A stage can return 0 and still leave its output untouched.
                # Checking only for existence misses the case that matters,
                # because the file usually DOES exist - it is the previous
                # run's, which universe_screen deliberately keeps when it
                # aborts. An unchanged output means this stage produced
                # nothing, whatever it returned.
                detail = (f"{Path(output).name} was not written "
                          f"(unchanged since before the stage ran)")

        if detail:
            status.append({"stage": name, "action": f"FAILED ({detail})",
                           "age": file_age_seconds(output), "output": str(output),
                           "ok": False, "required": required, "detail": detail})
            if not args.quiet:
                tint = R if required else Y
                print(f"  {tint}✗{X} {label:<22} failed - {detail}")
            if required:
                halted = True
                if not args.quiet:
                    print(f"  {R}stopping: every stage after this one reads what "
                          f"{label} was supposed to write.{X}")
            return False

        # Everything after this stage is now working from new inputs.
        downstream_forced = True
        status.append({"stage": name, "action": f"ran ({reason})",
                       "age": file_age_seconds(output), "output": str(output),
                       "ok": True,
                       "logic_version": common.output_logic_version(output)})
        return True

    if not args.quiet:
        print()
        h("PIPELINE")
        rule("─")

    # 1. universe screen
    def _universe():
        argv = []
        if args.include_canada:
            argv.append("--include-canada")
        if args.refresh_prices:
            argv.append("--refresh")
        return universe_screen.main(argv)
    stage("universe", CANDIDATES, md.TTL_SCREENER, _universe, "universe_screen",
          version=universe_screen.SCREEN_VERSION)

    # 2. evaluator (batch)
    def _evaluate():
        argv = ["--batch", "--quiet"]
        if args.evaluate_limit:
            argv += ["--limit", str(args.evaluate_limit)]
        if args.account_size:
            argv += ["--account-size", str(args.account_size),
                     "--account-currency", str(args.account_currency)]
        if args.workers:
            argv += ["--workers", str(args.workers)]
        if args.refresh_prices:
            argv.append("--refresh")
        return stock_evaluator.batch_main(argv)
    stage("evaluate", SCORED, md.TTL_PRICE, _evaluate, "stock_evaluator",
          version=stock_evaluator.EVALUATOR_VERSION)

    # 3. sentiment over the survivors only
    def _sentiment():
        scored = read_json(SCORED) or {}
        survivors = sorted((scored.get("scored") or []),
                           key=lambda r: -(_num(r.get("composite")) or 0))
        tickers = [r["ticker"] for r in survivors[:args.sentiment_top]]
        document = run_sentiment_stage(tickers, force_refresh=args.refresh_prices,
                                       quiet=args.quiet)
        # Sentiment is the one optional stage. It reports a failure so the run
        # is honest about it, but does not halt the pipeline: position_sizer
        # already has an answer for a name with no sentiment on file, and it is
        # *_unverified rather than a crash.
        return 0 if document.get("sentiment") or not tickers else 1
    stage("sentiment", SENTIMENT, md.TTL_PRICE, _sentiment, "sentiment",
          required=False, version=SENTIMENT_VERSION)

    # 4. correlation clusters
    def _cluster():
        argv = ["--quiet"]
        if args.top:
            argv += ["--top", str(args.top)]
        if args.min_composite is not None:
            argv += ["--min-composite", str(args.min_composite)]
        if args.refresh_prices:
            argv.append("--refresh")
        return rmt_cluster.main(argv)
    stage("cluster", CLUSTERED, md.TTL_PRICE, _cluster, "rmt_cluster",
          version=rmt_cluster.CLUSTER_VERSION)

    # 5. sizing against holdings
    def _size():
        argv = ["--quiet"]
        if args.top:
            argv += ["--top", str(args.top)]
        if args.refresh_prices:
            argv.append("--refresh")
        return position_sizer.main(argv)
    stage("size", SIZED, md.TTL_PRICE, _size, "position_sizer",
          version=position_sizer.SIZER_VERSION)

    # 6. entry timing over the names that got through
    #
    # Optional, like sentiment and for the same reason: nothing downstream
    # reads it. A candidate with no timing flag is still a candidate that
    # cleared the fundamentals, and the report says so rather than hiding it.
    def _timing():
        sized = read_json(SIZED) or {}
        tickers = [c["ticker"] for c in (sized.get("candidates") or [])
                   if c.get("ticker")]
        return run_timing_stage(tickers, force_refresh=args.refresh_prices,
                                quiet=args.quiet)
    stage("timing", TIMING, md.TTL_PRICE, _timing, "entry_timing",
          required=False, version=timing_logic_version())

    # 7. exit signals over the holdings
    #
    # Reads holdings.json and the archive rather than any file above it, so it
    # would run even on a scan that found nothing to buy - and should, since
    # the reason to look at a holding does not depend on this week's
    # candidates.
    def _exits():
        argv = ["--quiet"]
        if args.refresh_prices:
            argv.append("--refresh")
        return holdings_exit.main(argv)
    stage("exits", EXIT_SIGNALS, md.TTL_PRICE, _exits, "holdings_exit",
          required=False, version=holdings_exit.EXIT_VERSION)

    return status


def write_run_status(status, args, output_path=RUN_STATUS):
    """Record what each stage did, so a reader other than this terminal can see.

    run_pipeline knows when a stage failed and says so on stdout, but that is
    the only place it was ever said. stock_view reads this directory and had no
    way to tell a scan that completed from one that halted at stage two and
    left four stale files behind - which is exactly the situation where it most
    needs to stop presenting them as today's numbers.
    """
    failed = [row for row in status if row.get("ok") is False]
    blocking = [row for row in failed if row.get("required", True)]
    document = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "completed": not blocking,
        "ok": not failed,
        "stages": status,
        "failed_stages": [row["stage"] for row in failed],
        "blocking_stages": [row["stage"] for row in blocking],
        "invocation": {
            "force": bool(args.force),
            "render_only": bool(args.render_only),
            "include_canada": bool(args.include_canada),
            "top": args.top,
            "evaluate_limit": args.evaluate_limit,
            "sentiment_top": args.sentiment_top,
        },
    }
    return write_json(document, output_path)


# ─── ARCHIVE ───────────────────────────────────────────────────────────────

# Runs kept under data\archive\. Each is a full copy of the scan's five JSON
# files, and scored_candidates.json alone runs to tens of megabytes on a
# few-thousand-name universe, so an unbounded archive quietly becomes the
# largest thing in the project. The exit review only ever reads the most
# recent one; the rest are history, and a month of it is plenty.
DEFAULT_ARCHIVE_KEEP = 30

# data\archive\2026-08-31T02-19-07 - only folders this shape are ever pruned,
# so anything a person filed there by hand is left alone.
_ARCHIVE_STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}$")


def prune_archive(keep=DEFAULT_ARCHIVE_KEEP, quiet=False):
    """Delete all but the newest `keep` archived runs. Returns how many went.

    Names sort chronologically because the stamp is ISO-ordered, so the newest
    are simply the last ones. keep=0 or None disables pruning entirely.
    """
    if not keep or keep <= 0 or not ARCHIVE_DIR.is_dir():
        return 0
    runs = sorted(d for d in ARCHIVE_DIR.iterdir()
                  if d.is_dir() and _ARCHIVE_STAMP_RE.match(d.name))
    stale = runs[:-keep] if len(runs) > keep else []
    removed = 0
    for run in stale:
        try:
            shutil.rmtree(run)
            removed += 1
        except OSError as e:
            if not quiet:
                print(f"  {Y}could not prune archive/{run.name}: {e}{X}")
    if removed and not quiet:
        print(f"  {D}pruned {removed} archived run(s), keeping the newest {keep}{X}")
    return removed


def archive_run(status, keep=DEFAULT_ARCHIVE_KEEP, quiet=False):
    """Snapshot this run's outputs to data\\archive\\<timestamp>\\.

    Only when something actually re-ran - re-rendering yesterday's files does
    not create a new day's history. Without this there is no way to ask later
    how a scan's picks actually did, and the exit review has nothing to
    measure a holding's score against.

    And only when no REQUIRED stage failed. A halted run leaves a mix of new
    and stale files on disk; archiving that mix as one snapshot puts it at the
    front of the archive, where latest_archive_scores() picks it up and the
    exit review then reports "composite fell 2.1 since the last scan" against a
    scan that never finished.

    Sentiment is exempt because it is exempt everywhere: it fails routinely
    when Reddit rate-limits, and refusing to archive over it would starve the
    exit review of the history it needs for exactly the runs where the rest of
    the pipeline worked fine.
    """
    if not any(row["action"].startswith("ran") for row in status):
        return None
    failed = [row for row in status
              if row.get("ok") is False and row.get("required", True)]
    if failed:
        if not quiet:
            names = ", ".join(row["stage"] for row in failed)
            print(f"  {Y}not archiving: {names} failed, so this run's files are a "
                  f"mix of new and stale{X}")
        return None

    stamp = dt.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    target = ARCHIVE_DIR / stamp
    target.mkdir(parents=True, exist_ok=True)

    copied = []
    for path in (CANDIDATES, SCORED, SENTIMENT, CLUSTERED, SIZED, TIMING,
                 EXIT_SIGNALS, RUN_STATUS):
        if path.exists():
            try:
                shutil.copy2(path, target / path.name)
                copied.append(path.name)
            except OSError as e:
                if not quiet:
                    print(f"  {Y}could not archive {path.name}: {e}{X}")
    if not copied:
        try:
            target.rmdir()
        except OSError:
            pass
        return None
    if not quiet:
        print(f"  {D}archived {len(copied)} file(s) to archive/{stamp}{X}")
    prune_archive(keep=keep, quiet=quiet)
    return target


# ─── REPORT ────────────────────────────────────────────────────────────────

def timestamp_range(candidates):
    """Oldest and newest data-pull stamps across the candidates."""
    out = {}
    for field in ("financials_as_of", "price_as_of"):
        stamps = []
        for c in candidates:
            value = c.get(field)
            if not value:
                continue
            try:
                stamps.append(dt.datetime.fromisoformat(str(value)))
            except Exception:
                continue
        out[field] = (min(stamps), max(stamps)) if stamps else None
    return out


def flag_cells(candidate, sentiment):
    """The compact flag block: liquidity, trend, divergence, sentiment."""
    cells = []

    if candidate.get("liquidity_flag"):
        cells.append(f"{R}✗liq{X}")
    else:
        cells.append(f"{G}✓liq{X}")

    # roa_trend, not roa_trend_consistent. The strict flag demanded a
    # non-decreasing ROA in every year it had, so it went red on one soft year
    # in four and stayed green for a two-year-old listing that only had a
    # single step to clear - the same business reading worse for having
    # reported longer. The graded one says which way the series actually went.
    trend = candidate.get("roa_trend")
    if trend in ("improving", "flat"):
        cells.append(f"{G}✓trend{X}")
    elif trend == "deteriorating":
        cells.append(f"{R}✗trend{X}")
    elif trend == "mixed":
        cells.append(f"{Y}~trend{X}")
    else:
        cells.append(f"{D}·trend{X}")

    # The divergence pattern crossed with sentiment, when sentiment had a
    # confident read - a price_disconnect nobody is talking about and one the
    # public is actively negative on are not the same situation.
    conviction = ((candidate.get("sizing") or {}).get("conviction")
                  or "not_applicable")
    verdicts = {
        "disconnect_supported":  f"{G}disconnect+{X}",
        "disconnect_contested":  f"{Y}disconnect?{X}",
        "disconnect_unverified": f"{G}disconnect·{X}",
        "trap_confirmed":        f"{R}value-trap!{X}",
        "trap_contested":        f"{R}trap-hype  {X}",
        "trap_unverified":       f"{R}value-trap {X}",
        "not_applicable":        f"{D}·          {X}",
    }
    cells.append(verdicts.get(conviction, f"{D}·          {X}"))

    scores = (sentiment or {}).get(candidate["ticker"]) or {}
    overall = _num(scores.get("overall"))
    if overall is not None:
        cells.append("sent " + colour(overall, f"{overall:.1f}"))
    else:
        cells.append(f"{D}sent  n/a{X}")
    return "  ".join(cells)


# Six visible characters, fixed, so the flag block after it stays in its
# column. Short because this report is read in an 80-column console: the words
# are in the legend, and the still-falling names get their own heading anyway.
TIMING_CELLS = {
    TIMING_REVERSAL:      (G, "[rev] "),
    TIMING_STILL_FALLING: (Y, "[fall]"),
}


def timing_cell(flag):
    """The timing tag, or a dim placeholder for a name entry_timing missed."""
    if not flag:
        return f"{D}  ·   {X}"
    tint, text = TIMING_CELLS.get(flag, (D, f"[{flag[:4]}]"))
    return f"{tint}{text:<6}{X}"


def candidate_row(candidate, sentiment, indent=2, flag=None):
    composite = _num(candidate.get("composite"))
    pad = " " * indent
    held = f" {C}[held]{X}" if candidate.get("already_held") else ""
    sizing = candidate.get("sizing") or {}
    guide = (sizing.get("adjusted_guide") or "").split(";")[0]
    cut = f" {Y}(cut {sizing['reduction']*100:.0f}%){X}" if sizing.get("correlation_adjusted") else ""
    print(f"  {pad}{candidate['ticker']:<9} {colour(composite)} {bar(composite)} "
          f"{timing_cell(flag)} {flag_cells(candidate, sentiment)}{held}")
    if guide:
        print(f"  {pad}{'':<9} {D}{guide}{X}{cut}")


def sector_etf_line(members, candidates_by_ticker):
    """Suggest the sector ETF for a cluster whose members score alike."""
    sectors, cad = {}, 0
    for ticker in members:
        candidate = candidates_by_ticker.get(ticker) or {}
        sector = candidate.get("sector")
        if sector:
            sectors[sector] = sectors.get(sector, 0) + 1
        if (candidate.get("quote_currency") or "").upper() == "CAD":
            cad += 1
    if not sectors:
        return None
    sector = max(sectors, key=lambda s: sectors[s])
    table = SECTOR_ETF_CA if cad > len(members) / 2 else SECTOR_ETF_US
    etf = table.get(sector)
    if not etf:
        return None
    return (f"members score alike - the {sector} call is the decision, "
            f"not the name. Sector ETF: {B}{etf}{X}")


def render_candidate_groups(candidates, sentiment, flags, by_ticker):
    """The candidate list as it has always been drawn: clusters, then singles.

    Lifted out of render_report so the buy section can draw it twice, once for
    the names timed for entry and once for the ones still falling. One thing
    changed on the way out: a cluster can now arrive part-shown, because the
    timing split does not respect cluster membership, so the count is stated
    and a pick_winner cluster missing its winner is drawn flat.
    """
    # Group by cluster; demoted peers nest under their winner.
    clusters, standalone = {}, []
    for candidate in candidates:
        cluster = candidate.get("cluster")
        if cluster:
            clusters.setdefault(cluster["cluster_index"], []).append(candidate)
        else:
            standalone.append(candidate)

    ordered = sorted(clusters.items(),
                     key=lambda kv: -max((_num(c.get("composite")) or 0) for c in kv[1]))

    if ordered:
        print()
        h("CORRELATION CLUSTERS")
        rule("─")

    for index, members in ordered:
        info = members[0]["cluster"]
        resolution = info.get("resolution")
        corr = info.get("avg_correlation")
        tag = f"{Y}pick_winner{X}" if resolution == "pick_winner" else f"{C}industry_wide{X}"
        print()
        print(f"  {tag}  {len(info.get('members') or members)} names  ·  "
              f"avg corr {corr:.2f}" if corr is not None else f"  {tag}")

        members.sort(key=lambda c: -(_num(c.get("composite")) or 0))
        winner_ticker = info.get("winner")
        winner = next((c for c in members if c["ticker"] == winner_ticker), None)
        if resolution == "pick_winner" and winner is not None:
            candidate_row(winner, sentiment, indent=2, flag=flags.get(winner["ticker"]))
            peers = [c for c in members if c["ticker"] != winner["ticker"]]
            if peers:
                print(f"    {D}demoted peers{X}")
                for peer in peers:
                    candidate_row(peer, sentiment, indent=6,
                                  flag=flags.get(peer["ticker"]))
            continue

        # Splitting the candidates by timing can leave a cluster part-shown.
        # Saying so beats a header that reads "3 names" over two rows - and for
        # a pick_winner cluster whose winner is in the other group, beats
        # promoting the best of what is left and calling it the winner, which
        # is what filtering a cluster does silently.
        total = len(info.get("members") or members)
        if len(members) < total:
            note = (f"{len(members)} of {total} members here - the rest are in "
                    f"the other timing group")
            if resolution == "pick_winner" and winner is None and winner_ticker:
                note += f", the winner {winner_ticker} among them"
            print(f"    {D}{note}{X}")
        for member in members:
            candidate_row(member, sentiment, indent=2, flag=flags.get(member["ticker"]))
        if resolution != "pick_winner":
            line = sector_etf_line([c["ticker"] for c in members], by_ticker)
            if line:
                print(f"    {D}→{X} {line}")

    if standalone:
        print()
        h("STANDALONE")
        rule("─")
        standalone.sort(key=lambda c: -(_num(c.get("composite")) or 0))
        for candidate in standalone:
            candidate_row(candidate, sentiment, indent=2,
                          flag=flags.get(candidate["ticker"]))


def render_buy_signals(candidates, sentiment, flags, by_ticker):
    """BUY SIGNALS, split by whether the price has actually turned yet.

    A still_falling name passed the same fundamentals as everything above it
    and stays in the list for that reason. It sits below because the question
    this section answers is what to buy THIS WEEK, and a business worth owning
    whose price is still falling is a worse answer to that question than the
    same business after it stops - without being a worse business.
    """
    print()
    h("BUY SIGNALS — candidates that cleared the fundamentals")
    rule("─")

    falling_tickers = {c["ticker"] for c in candidates
                       if flags.get(c["ticker"]) == TIMING_STILL_FALLING}
    falling = [c for c in candidates if c["ticker"] in falling_tickers]
    timed = [c for c in candidates if c["ticker"] not in falling_tickers]

    if timed:
        render_candidate_groups(timed, sentiment, flags, by_ticker)
    else:
        print(f"  {Y}every candidate is still falling - none timed for entry "
              f"this week{X}")

    if falling:
        print()
        print(f"  {D}── still falling — fundamentals cleared, the price has not "
              f"turned yet ──{X}")
        falling.sort(key=lambda c: -(_num(c.get("composite")) or 0))
        for candidate in falling:
            candidate_row(candidate, sentiment, indent=2,
                          flag=flags.get(candidate["ticker"]))


# Red is act on it, yellow is look at it, green is the discount closed - the
# same convention the rest of the report uses. thesis_completed is green
# because it is the exit the thesis was written for, not a failure.
EXIT_TINTS = {"thesis_broken": R, "stop_loss": R, "reassess": Y,
              "thesis_completed": G}
# Most urgent first, both for the row order and within a row's trigger list.
EXIT_RANK = {"thesis_broken": 0, "stop_loss": 1, "thesis_completed": 2,
             "reassess": 3}


def _clip(text, width=70):
    text = str(text or "")
    return text if len(text) <= width else text[:width - 1] + "…"


def render_exit_signals(exit_doc):
    """SELL / REVIEW SIGNALS - only the holdings with something to act on.

    A holding with no trigger is not listed. It was still evaluated, and the
    count of those says so, but a review section that lists everything is a
    list nobody reads twice.
    """
    if not isinstance(exit_doc, dict):
        return
    evaluated = exit_doc.get("evaluated") or []
    if not evaluated:
        return

    flagged = [row for row in evaluated if row.get("triggers")]
    unavailable = [row for row in evaluated if row.get("status") == "unavailable"]
    clean = len(evaluated) - len(flagged) - len(unavailable)

    print()
    h("SELL / REVIEW SIGNALS — holdings with an exit trigger")
    rule("─")

    if not flagged:
        # `clean` rather than len(evaluated): a holding that could not be
        # re-scored was not reviewed, and the trailer below names those.
        print(f"    {G}nothing to act on{X}  {D}· {clean} holding(s) reviewed, "
              f"no triggers{X}")

    def severity(row):
        return min((EXIT_RANK.get(t, 9) for t in row["triggers"]), default=9)

    for row in sorted(flagged, key=lambda r: (severity(r), r["ticker"])):
        triggers = sorted(row["triggers"], key=lambda t: EXIT_RANK.get(t, 9))
        tint = EXIT_TINTS.get(triggers[0], Y)
        labels = "  ".join(f"{EXIT_TINTS.get(t, Y)}{t}{X}" for t in triggers)

        pct = _num(row.get("pct_change_from_entry"))
        days = row.get("days_held")
        numbers = [f"{pct:+.1f}% since entry" if pct is not None
                   else "no entry price on file",
                   f"held {days}d" if days is not None else "no entry date on file"]
        summary = row.get("current_metrics_summary") or {}
        composite = _num(summary.get("composite"))
        if composite is not None:
            numbers.append(f"composite {composite:.2f}")

        print(f"    {tint}{row['ticker']:<9}{X} {labels}")
        print(f"    {'':<9} {D}{' · '.join(numbers)}{X}")
        reasons = row.get("trigger_reasons") or {}
        for trigger in triggers:
            for reason in (reasons.get(trigger) or [])[:1]:
                print(f"    {'':<9} {D}{trigger}: {_clip(reason)}{X}")

    trail = []
    if flagged and clean > 0:
        trail.append(f"{clean} other holding(s) reviewed, no triggers")
    if unavailable:
        names = ", ".join(row["ticker"] for row in unavailable[:6])
        more = "" if len(unavailable) <= 6 else f" +{len(unavailable) - 6} more"
        trail.append(f"{len(unavailable)} could not be re-scored: {names}{more}")
    if trail:
        print(f"    {D}{' · '.join(trail)}{X}")


def render_report(sized_doc, scored_doc, sentiment_doc, cluster_doc, status, args,
                  timing_doc=None, exit_doc=None):
    candidates = sized_doc.get("candidates") or []
    by_ticker = {c["ticker"]: c for c in candidates}
    sentiment = (sentiment_doc or {}).get("sentiment") or {}
    flags = timing_flags(timing_doc)

    print()
    rule()
    print(f"  {B}{C}SCAN REPORT — TIER 1 SUMMARY{X}")

    stamps = timestamp_range(candidates)
    for field, label in (("financials_as_of", "Financials pulled"),
                         ("price_as_of", "Prices pulled")):
        window = stamps.get(field)
        if window:
            oldest, newest = window
            same = oldest.strftime("%Y-%m-%d %H:%M") == newest.strftime("%Y-%m-%d %H:%M")
            when = (newest.strftime("%Y-%m-%d %H:%M") if same
                    else f"{oldest.strftime('%Y-%m-%d %H:%M')} → {newest.strftime('%Y-%m-%d %H:%M')}")
            print(f"  {label:<18} {when}")
        else:
            print(f"  {label:<18} {D}unknown{X}")

    # Which scoring logic produced these numbers. Worth a line because the
    # scores on screen can outlive the code that made them: a stale
    # scored_candidates.json used to be re-rendered under a fresh header with
    # nothing on the page saying so. A mismatch here now cannot survive a run
    # - stage() re-runs on it - so this line reads as confirmation rather than
    # as a warning, which is exactly what was missing before.
    scored_version = (scored_doc or {}).get(common.LOGIC_VERSION_KEY)
    current = stock_evaluator.EVALUATOR_VERSION
    if scored_version is None:
        print(f"  {'Scored by':<18} {Y}unstamped (pre-versioning file){X}")
    elif str(scored_version) != str(current):
        print(f"  {'Scored by':<18} {R}evaluator v{scored_version}, "
              f"but the code here is v{current}{X}")
    else:
        print(f"  {'Scored by':<18} {D}evaluator v{scored_version}{X}")

    # Counts, including anything that fell out along the way.
    scored_counts = (scored_doc or {}).get("counts") or {}
    skipped = len((scored_doc or {}).get("skipped") or [])
    sent_skipped = len((sentiment_doc or {}).get("skipped") or [])
    print(f"  {'Candidates':<18} {len(candidates)} shown  ·  "
          f"{scored_counts.get('scored', '?')} scored of "
          f"{scored_counts.get('candidates', '?')} screened")
    if flags:
        reversal = sum(1 for c in candidates
                       if flags.get(c["ticker"]) == TIMING_REVERSAL)
        falling = sum(1 for c in candidates
                      if flags.get(c["ticker"]) == TIMING_STILL_FALLING)
        print(f"  {'Entry timing':<18} {D}{reversal} reversal signal  ·  "
              f"{falling} still falling  ·  "
              f"{len(candidates) - reversal - falling} unflagged{X}")
    else:
        print(f"  {'Entry timing':<18} {Y}no timing flags on file - candidates "
              f"are untagged{X}")
    if skipped or sent_skipped:
        parts = []
        if skipped:
            parts.append(f"{skipped} ticker(s) skipped by the evaluator")
        if sent_skipped:
            parts.append(f"{sent_skipped} skipped by sentiment")
        print(f"  {'Partial data':<18} {Y}{'; '.join(parts)}{X}")
    summary = (sentiment_doc or {}).get("request_summary") or {}
    if summary.get("reddit_requests_total"):
        blocked = summary.get("blocked_tickers") or 0
        line = (f"{summary['reddit_requests_total']} reddit requests, "
                f"{summary['reddit_requests_per_ticker']}/ticker")
        if blocked:
            line += f", {blocked} ticker(s) rate-limited"
        print(f"  {'Sentiment cost':<18} {line}")
    if (sentiment_doc or {}).get("aborted"):
        print(f"  {'Sentiment':<18} {Y}{sentiment_doc['aborted']['reason']}{X}")
    if (cluster_doc or {}).get("insufficient_data_for_clustering"):
        print(f"  {'Clustering':<18} {Y}{cluster_doc.get('note')}{X}")
    if sized_doc.get("note"):
        print(f"  {'Sizing':<18} {Y}{sized_doc['note']}{X}")
    rule()

    if not candidates:
        print()
        print(f"  {Y}No candidates cleared the bar this scan — this can be a "
              f"legitimate market{X}")
        print(f"  {Y}condition, not an error.{X}")
        # The holdings still get their section. Nothing about whether to sell
        # what you own depends on this week's buy list being non-empty, and a
        # scan that found nothing to buy is exactly when that gets forgotten.
        render_exit_signals(exit_doc)
        print()
        rule("─")
        print(f"  {Y}{DISCLAIMER}{X}")
        rule()
        print()
        return

    render_buy_signals(candidates, sentiment, flags, by_ticker)
    render_exit_signals(exit_doc)

    reviews = sized_doc.get("holdings_review") or []
    if reviews:
        print()
        h("EXIT REVIEW — current holdings, re-scored")
        rule("─")
        order = {"exit_review": 0, "watch": 1, "unavailable": 2, "hold": 3}
        for review in sorted(reviews, key=lambda r: order.get(r["verdict"], 9)):
            composite = _num(review.get("composite"))
            tint = {"exit_review": R, "watch": Y, "unavailable": D}.get(review["verdict"], G)
            delta = review.get("composite_delta")
            shift = "" if delta is None else f"  {delta:+.2f} since last scan"
            print(f"    {review['ticker']:<9} {colour(composite)} {bar(composite)}  "
                  f"{tint}{review['verdict']}{X}{shift}")
            print(f"    {'':<9} {D}{review['reasons'][0]}{X}")

    print()
    rule("─")
    h("LEGEND")
    print(f"  {G}✓liq{X}/{R}✗liq{X} liquidity · {G}✓trend{X} ROA improving or flat · "
          f"{R}✗trend{X} declining · {Y}~trend{X} mixed · {D}·trend{X} too little history")
    print(f"  {G}disconnect+{X} price low, trend intact, public read agrees · "
          f"{Y}disconnect?{X} sentiment contests it · {G}disconnect·{X} no sentiment")
    print(f"  {R}value-trap!{X} trend and sentiment both negative · {R}trap-hype{X} "
          f"trend down but sentiment high · sent = 0-10 sentiment")
    print(f"  {G}[rev]{X} reversal signal, timed for entry · {Y}[fall]{X} still "
          f"falling, fundamentals cleared but the price has not · {D}·{X} no flag")
    print(f"  exits: {R}thesis_broken{X}/{R}stop_loss{X} act · {Y}reassess{X} look "
          f"· {G}thesis_completed{X} the discount closed - the exit it was for")
    if args.show_stages and status:
        print()
        h("STAGES")
        for row in status:
            print(f"  {row['stage']:<12} {row['action']:<20} {describe_age(row['age'])}")
    print()
    rule("─")
    print(f"  {Y}{DISCLAIMER}{X}")
    rule()
    print()


# ─── MAIN ──────────────────────────────────────────────────────────────────

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run the scan pipeline and render the Tier 1 summary.")
    parser.add_argument("--detail", metavar="TICKER",
                        help="print stock_evaluator's full single-ticker report instead")
    parser.add_argument("--force", action="store_true",
                        help="re-run every stage, ignoring output freshness")
    parser.add_argument("--refresh-prices", action="store_true",
                        help="also bypass market_data's own price/statement cache")
    parser.add_argument("--render-only", action="store_true",
                        help="never run a stage; render from whatever is on disk")
    parser.add_argument("--include-canada", action="store_true",
                        help="pass through to universe_screen")
    parser.add_argument("--evaluate-limit", type=int, metavar="N",
                        help="cap how many screened candidates get scored")
    parser.add_argument("--account-size", type=float, metavar="AMOUNT",
                        help="account size for the evaluator's liquidity gate")
    parser.add_argument("--account-currency", default="USD", metavar="CODE",
                        help="currency the account size is in (default USD). The "
                             "liquidity gate converts each name's dollar volume "
                             "into this before comparing.")
    parser.add_argument("--top", type=int, metavar="N",
                        help="shortlist size for clustering and sizing")
    parser.add_argument("--min-composite", type=float, metavar="SCORE")
    parser.add_argument("--sentiment-top", type=int, default=SENTIMENT_TOP, metavar="N",
                        help=f"how many survivors get a sentiment pull "
                             f"(default {SENTIMENT_TOP})")
    parser.add_argument("--no-archive", action="store_true",
                        help="do not snapshot this run to data/archive/<timestamp>/")
    parser.add_argument("--archive-keep", type=int, default=DEFAULT_ARCHIVE_KEEP,
                        metavar="N",
                        help=f"how many archived runs to keep (default "
                             f"{DEFAULT_ARCHIVE_KEEP}; 0 keeps every one)")
    parser.add_argument("--workers", type=int, metavar="N",
                        help="score N tickers at once in the evaluator stage "
                             "(default 1; Yahoo rate-limits, so raise it carefully)")
    parser.add_argument("--show-stages", action="store_true",
                        help="list each stage's action in the report footer")
    parser.add_argument("--quiet", action="store_true", help="suppress stage progress")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="market_data cache/retry trace")
    args = parser.parse_args(argv)

    if args.verbose:
        md.DEBUG = True

    # Drill-down: hand straight to the existing single-ticker CLI report.
    if args.detail:
        stock_evaluator.evaluate(args.detail.strip().upper())
        return 0

    status = []
    if args.render_only:
        if not args.quiet:
            print(f"\n  {D}--render-only: using whatever is already in "
                  f"{DATA_DIR}{X}")
    else:
        status = run_pipeline(args)
        # Before archiving, so the snapshot carries the run's own verdict.
        write_run_status(status, args)
        if not args.no_archive:
            archive_run(status, keep=args.archive_keep, quiet=args.quiet)

    sized_doc = read_json(SIZED)
    if sized_doc is None:
        print(f"\n  {R}No {SIZED.name} to render.{X}")
        print(f"  Run without --render-only to build it.\n")
        return 2

    render_report(sized_doc, read_json(SCORED), read_json(SENTIMENT),
                  read_json(CLUSTERED), status, args,
                  timing_doc=read_json(TIMING), exit_doc=read_json(EXIT_SIGNALS))

    # A halted run still renders - what is on disk is the last good scan and is
    # worth reading - but it says which stage stopped it, and exits non-zero
    # when a required one did, so a scheduled run fails visibly instead of
    # reporting stale data as today's. A failed sentiment stage is reported and
    # does not change the exit code: the scan itself completed.
    failed = [row for row in status if row.get("ok") is False]
    if not failed:
        return 0

    blocking = [row for row in failed if row.get("required", True)]
    print(f"  {R if blocking else Y}This run did not complete"
          f"{'' if blocking else ' in full'}.{X}")
    for row in failed:
        tint = R if row.get("required", True) else Y
        optional = "" if row.get("required", True) else " (optional stage)"
        print(f"    {tint}✗{X} {row['stage']}: {row.get('detail') or row['action']}"
              f"{optional}")
    skipped = [row for row in status if row["action"] == "not attempted"]
    if skipped:
        print(f"    {D}not attempted: "
              f"{', '.join(row['stage'] for row in skipped)}{X}")
    if blocking:
        print(f"  {Y}The report above is rendered from the files already on disk, "
              f"which are older than this run.{X}\n")
        return 1
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
