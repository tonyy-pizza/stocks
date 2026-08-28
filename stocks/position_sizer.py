#!/usr/bin/env python3
r"""
position_sizer.py - Stage 3: size the shortlist against what you already own.

Stages 0-2 judge candidates in isolation, then against each other. Neither
knows what is already in your account. A name that scores 8.5 and moves with a
position you already hold is not a new bet, it is more of an existing one, and
should be sized accordingly.

What this does:

  - reads data\holdings.json (hand-edited; a template is written on first run)
  - reads data\scored_candidates.json and data\clustered.json
  - builds ONE correlation matrix over candidates AND holdings together, cleaned
    with the same Marchenko-Pastur filter rmt_cluster.py uses, reporting both
    the raw and the filtered correlation for every candidate/holding pair
  - calls stock_evaluator.position_guidance() for the base sizing, then shrinks
    it where a candidate duplicates a holding, naming the holding responsible

Nothing here re-implements sizing or correlation logic: position_guidance()
comes from stock_evaluator.py and the MP math from rmt_cluster.py, both
imported.

With no holdings on file the correlation modifier is skipped entirely and the
base guidance passes through untouched, which is the honest answer rather than
a correlation of zero.

Setup:
    pip install yfinance curl_cffi requests numpy pandas

Usage:
    py position_sizer.py                      # size the Stage 2 shortlist
    py position_sizer.py --top 40
    py position_sizer.py --corr-threshold 0.6 --reduction-factor 0.6
    py position_sizer.py --flat-reduction     # fixed cut instead of proportional
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd


def _add_script_dir_to_path():
    here = Path(__file__).resolve().parent
    for candidate in (here, here.parent / "stocks", here.parent):
        if (candidate / "market_data.py").exists():
            if str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
            return
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))


_add_script_dir_to_path()

import market_data as md                                   # noqa: E402
import rmt_cluster as rc                                    # noqa: E402
from stock_evaluator import position_guidance               # noqa: E402


# -------------------------------------------------------------------------
# CONFIG
# -------------------------------------------------------------------------

DATA_DIR = Path(os.environ.get("STOCKS_DATA_DIR") or (Path(md.BASE_DIR) / "data"))
HOLDINGS_PATH = DATA_DIR / "holdings.json"
SCORED_PATH = DATA_DIR / "scored_candidates.json"
CLUSTERED_PATH = DATA_DIR / "clustered.json"
OUTPUT_PATH = DATA_DIR / "sized_candidates.json"

# Above this correlation with a holding, a candidate is treated as partly the
# same position.
CORR_THRESHOLD = 0.70

# Which number the threshold is applied to. Both are computed and reported for
# every pair: "raw" is the sample correlation, "cleaned" is the same matrix put
# through the MP filter, which pulls each pair toward the factor structure -
# shrinking pairs whose correlation was mostly noise and reinforcing pairs a
# real factor explains. On a 27-name panel the banks sat at 0.71-0.76 raw and
# 0.65-0.69 cleaned, so a 0.70 bar on cleaned values quietly behaves like a
# 0.75 bar. "Correlated above 0.70" conventionally means the sample
# correlation, so that is what gates the reduction; --correlation-basis cleaned
# switches it to the filtered estimate.
CORRELATION_BASIS = "raw"

# How much of the base size a fully-correlated (1.0) duplicate loses.
REDUCTION_FACTOR = 0.50

NO_HOLDINGS_NOTE = "no holdings on file - sizing not correlation-adjusted"

HOLDINGS_TEMPLATE = {
    "_comment": (
        "Hand-edited holdings file. One entry per position you actually own. "
        "ticker: the Yahoo symbol (RY.TO for a TSX listing). "
        "shares: number of shares held. "
        "cost_basis: your average price per share, in the listing's own currency. "
        "Delete the example entry below - entries marked _example are ignored."
    ),
    "holdings": [
        {"ticker": "AAPL", "shares": 10, "cost_basis": 150.00, "_example": True}
    ],
}

# "Core: 3%–5%; up to 8% with diversification." -> (3.0, 5.0)
_PCT_RANGE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*[–—-]\s*(\d+(?:\.\d+)?)\s*%")


def _num(value) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) else out


# -------------------------------------------------------------------------
# HOLDINGS
# -------------------------------------------------------------------------

def load_holdings(holdings_path=None, quiet=False):
    """Read holdings.json, writing the template if it does not exist yet.

    Returns (holdings list, created_template flag). Entries marked _example are
    skipped, so an untouched template counts as no holdings rather than
    silently pretending you own the example.
    """
    holdings_path = Path(holdings_path) if holdings_path else HOLDINGS_PATH
    created = False

    if not holdings_path.exists():
        holdings_path.parent.mkdir(parents=True, exist_ok=True)
        with open(holdings_path, "w", encoding="utf-8") as f:
            json.dump(HOLDINGS_TEMPLATE, f, indent=2, ensure_ascii=False)
        created = True
        if not quiet:
            print(f"  created holdings template: {holdings_path}")
            print("  edit it with your real positions, then re-run to get "
                  "correlation-adjusted sizing")

    try:
        with open(holdings_path, "r", encoding="utf-8") as f:
            document = json.load(f)
    except Exception as e:
        if not quiet:
            print(f"  could not read {holdings_path}: {e}")
        return [], created

    holdings = []
    for entry in (document.get("holdings") or []):
        if not isinstance(entry, dict) or entry.get("_example"):
            continue
        ticker = str(entry.get("ticker") or "").strip().upper()
        if not ticker or ticker == "...":
            continue
        holdings.append({
            "ticker": ticker,
            "shares": _num(entry.get("shares")),
            "cost_basis": _num(entry.get("cost_basis")),
        })
    return holdings, created


# -------------------------------------------------------------------------
# INPUT
# -------------------------------------------------------------------------

def load_scored(scored_path):
    with open(scored_path, "r", encoding="utf-8") as f:
        document = json.load(f)
    records = {}
    for record in document.get("scored") or []:
        ticker = str(record.get("ticker") or "").strip().upper()
        if ticker:
            records[ticker] = record
    return document, records


def load_clusters(clustered_path):
    """clustered.json -> (per-ticker cluster view, shortlist, raw document).

    The shortlist is whatever Stage 2 actually looked at (clustered members
    plus standalone names), so Stage 3 sizes exactly that set.
    """
    path = Path(clustered_path)
    if not path.exists():
        return {}, [], None

    with open(path, "r", encoding="utf-8") as f:
        document = json.load(f)

    view, shortlist = {}, []
    for index, cluster in enumerate(document.get("clusters") or []):
        members = cluster.get("members") or []
        for ticker in members:
            view[ticker] = {
                "cluster_index": index,
                "members": members,
                "avg_correlation": cluster.get("avg_correlation"),
                "dispersion": cluster.get("dispersion"),
                "resolution": cluster.get("resolution"),
                "winner": cluster.get("winner"),
                "is_winner": cluster.get("winner") == ticker,
                "demoted": ticker in (cluster.get("demoted_peers") or []),
            }
            shortlist.append(ticker)
    for ticker in document.get("standalone") or []:
        view.setdefault(ticker, None)
        shortlist.append(ticker)

    return view, sorted(set(shortlist)), document


# -------------------------------------------------------------------------
# CORRELATION  (rmt_cluster's MP filter, over candidates + holdings)
# -------------------------------------------------------------------------

def correlation_to_holdings(candidates, holding_tickers, lookback_years=None,
                            force_refresh=False, quiet=False):
    """One cleaned correlation matrix over candidates AND holdings together.

    Both sets go into the same matrix on purpose: a correlation estimated on
    one panel is not comparable with one estimated on another, and the MP
    threshold depends on the shape of the panel it was fitted to.

    Returns (correlations, meta) where correlations is
    {candidate: {holding: {"cleaned": x, "raw": y}}} for the names that made it
    in. Both bases are reported: cleaned is the better estimate, raw is what a
    correlation threshold is usually quoted against.
    """
    universe = sorted(set(candidates) | set(holding_tickers))
    meta = {"assets_requested": len(universe), "excluded": {}}

    if len(universe) < 3:
        meta["note"] = "too few names to estimate a correlation matrix"
        return {}, meta

    if lookback_years:
        years = float(lookback_years)
        period = f"{max(1, int(math.ceil(years)))}y"
    else:
        years, period = rc.adaptive_lookback(len(universe))
    meta.update({"lookback_years": years, "period": period})
    if not quiet:
        print(f"  correlation panel: {len(universe)} names over {years:.2f}y ({period})")

    prices, excluded = rc.build_price_frame(universe, period, force_refresh=force_refresh)
    meta["excluded"] = excluded
    if prices is None or len(prices.columns) < 3 or len(prices) - 1 < rc.MIN_SHARED_ROWS:
        meta["note"] = "not enough shared price history to estimate correlations"
        return {}, meta

    decomp = rc.rmt_decompose(prices)
    cleaned = rc.clean_correlation(decomp)
    raw = decomp["corr"]

    meta.update({
        "assets_in_matrix": decomp["N"],
        "trading_days": decomp["T"],
        "q_ratio": round(decomp["q"], 2),
        "mp_threshold": round(decomp["threshold"], 4),
        "noise_sigma2": round(decomp["sigma2"], 4),
        "signals_found": decomp["n_signal"],
    })
    if decomp["q"] < rc.MIN_USABLE_Q:
        meta["warning"] = (f"T/N = {decomp['q']:.2f} is below {rc.MIN_USABLE_Q}; "
                           f"correlations are unreliable. Use --top to shorten the list.")
    if not quiet:
        print(f"  matrix: {decomp['N']} assets x {decomp['T']} days (T/N = {decomp['q']:.1f})"
              f"  ·  {decomp['n_signal']} signal modes kept, noise band flattened")

    usable = set(cleaned.columns)
    held_in_matrix = [t for t in holding_tickers if t in usable]
    meta["holdings_in_matrix"] = held_in_matrix
    meta["holdings_missing"] = [t for t in holding_tickers if t not in usable]

    correlations = {}
    for candidate in candidates:
        if candidate not in usable:
            continue
        correlations[candidate] = {
            holding: {"cleaned": round(float(cleaned.loc[candidate, holding]), 4),
                      "raw": round(float(raw.loc[candidate, holding]), 4)}
            for holding in held_in_matrix if holding != candidate
        }
    return correlations, meta


# -------------------------------------------------------------------------
# SIZING
# -------------------------------------------------------------------------

def parse_guide_range(guide):
    """Pull the percentage range out of a position_guidance() string."""
    match = _PCT_RANGE_RE.search(guide or "")
    if not match:
        return None, None
    return float(match.group(1)), float(match.group(2))


def apply_reduction(guide, scale):
    """Rewrite the guide string with the reduced range, keeping its wording."""
    def _sub(match):
        low = float(match.group(1)) * scale
        high = float(match.group(2)) * scale
        return f"{low:.1f}%–{high:.1f}%"
    return _PCT_RANGE_RE.sub(_sub, guide, count=1)


def sizing_scale(correlation, threshold, reduction_factor, flat=False):
    """How much of the base size survives, given the worst correlation.

    Proportional by default: nothing is taken away at the threshold and the
    full reduction_factor applies at a correlation of 1.0, so a name at 0.71
    is barely touched while a near-duplicate is halved. --flat-reduction
    applies the whole factor the moment the threshold is crossed.
    """
    if correlation is None or correlation <= threshold:
        return 1.0, 0.0
    if flat:
        reduction = reduction_factor
    else:
        span = max(1e-9, 1.0 - threshold)
        reduction = reduction_factor * (correlation - threshold) / span
    reduction = max(0.0, min(reduction_factor, reduction))
    return 1.0 - reduction, reduction


def size_candidate(ticker, record, cluster, holding, correlations,
                   threshold=CORR_THRESHOLD, reduction_factor=REDUCTION_FACTOR,
                   flat=False, holdings_on_file=True, basis=CORRELATION_BASIS):
    """Base guidance from stock_evaluator, then the correlation modifier."""
    metrics = record.get("metrics") or {}
    scores = {"composite": record.get("composite"),
              "dims": record.get("dims") or {}}
    guidance = position_guidance(metrics, scores, record.get("insider"))

    base_low, base_high = parse_guide_range(guidance["guide"])
    sizing = {
        "base_guide": guidance["guide"],
        "base_low_pct": base_low,
        "base_high_pct": base_high,
        "risk_flags": guidance["risk_flags"],
        "correlation_adjusted": False,
        "scale": 1.0,
        "reduction": 0.0,
        "adjusted_guide": guidance["guide"],
        "adjusted_low_pct": base_low,
        "adjusted_high_pct": base_high,
        "max_correlation": None,
        "max_correlation_raw": None,
        "max_correlation_cleaned": None,
        "correlation_basis": basis,
        "correlated_with": None,
        "correlations": {},
    }

    if holding is not None:
        sizing["note"] = "already held - not sized as a new position"
        return guidance, sizing

    if not holdings_on_file:
        sizing["note"] = NO_HOLDINGS_NOTE
        return guidance, sizing

    pairs = correlations.get(ticker) or {}
    sizing["correlations"] = pairs
    if not pairs:
        sizing["note"] = "no correlation to holdings available for this name"
        return guidance, sizing

    worst = max(pairs, key=lambda h: pairs[h].get(basis, 0.0))
    worst_corr = pairs[worst].get(basis)
    sizing["max_correlation"] = worst_corr
    sizing["max_correlation_raw"] = pairs[worst].get("raw")
    sizing["max_correlation_cleaned"] = pairs[worst].get("cleaned")

    if worst_corr <= threshold:
        sizing["note"] = (f"no meaningful correlation to holdings (max {worst_corr:.2f} "
                          f"{basis} with {worst}, at or below {threshold:.2f})")
        return guidance, sizing

    scale, reduction = sizing_scale(worst_corr, threshold, reduction_factor, flat=flat)
    sizing.update({
        "correlation_adjusted": True,
        "scale": round(scale, 4),
        "reduction": round(reduction, 4),
        "correlated_with": worst,
        "adjusted_guide": apply_reduction(guidance["guide"], scale),
        "adjusted_low_pct": None if base_low is None else round(base_low * scale, 2),
        "adjusted_high_pct": None if base_high is None else round(base_high * scale, 2),
        "note": (f"correlation {worst_corr:.2f} ({basis}) with holding {worst} above "
                 f"{threshold:.2f}: size cut {reduction*100:.0f}%"),
    })
    return guidance, sizing


# -------------------------------------------------------------------------
# RUN
# -------------------------------------------------------------------------

def write_json(document, output_path):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(output_path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(document, f, indent=2, ensure_ascii=False, default=str)
        os.replace(tmp_path, output_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return output_path


def size_shortlist(scored_path=None, clustered_path=None, holdings_path=None,
                   output_path=None, top=None, min_composite=None,
                   corr_threshold=CORR_THRESHOLD, reduction_factor=REDUCTION_FACTOR,
                   flat_reduction=False, correlation_basis=CORRELATION_BASIS,
                   lookback_years=None, slim=False,
                   force_refresh=False, quiet=False):
    """Combine scored + clustered + sizing into data\\sized_candidates.json."""
    scored_path = Path(scored_path) if scored_path else SCORED_PATH
    clustered_path = Path(clustered_path) if clustered_path else CLUSTERED_PATH
    output_path = Path(output_path) if output_path else OUTPUT_PATH

    scored_doc, records = load_scored(scored_path)
    cluster_view, shortlist, clustered_doc = load_clusters(clustered_path)

    # Stage 2's shortlist is the set to size; without it, everything scored.
    tickers = [t for t in shortlist if t in records] or sorted(records)
    if min_composite is not None:
        tickers = [t for t in tickers
                   if (_num(records[t].get("composite")) or -1) >= min_composite]
    tickers.sort(key=lambda t: -(_num(records[t].get("composite")) or 0))
    if top:
        tickers = tickers[:top]

    holdings, created_template = load_holdings(holdings_path, quiet=quiet)
    by_ticker = {h["ticker"]: h for h in holdings}
    holdings_on_file = bool(holdings)

    if not quiet:
        print(f"\n  candidates: {len(tickers)}  ·  holdings: {len(holdings)}")

    correlations, corr_meta = {}, {}
    if holdings_on_file:
        correlations, corr_meta = correlation_to_holdings(
            tickers, list(by_ticker), lookback_years=lookback_years,
            force_refresh=force_refresh, quiet=quiet)
    elif not quiet:
        print(f"  {NO_HOLDINGS_NOTE}")

    out_candidates = []
    for ticker in tickers:
        record = records[ticker]
        holding = by_ticker.get(ticker)
        cluster = cluster_view.get(ticker)
        guidance, sizing = size_candidate(
            ticker, record, cluster, holding, correlations,
            threshold=corr_threshold, reduction_factor=reduction_factor,
            flat=flat_reduction, holdings_on_file=holdings_on_file,
            basis=correlation_basis)

        entry = {
            "ticker": ticker,
            "name": record.get("name"),
            "sector": record.get("sector"),
            "quote_currency": record.get("quote_currency"),
            "composite": record.get("composite"),
            "rating": record.get("rating"),
            "dims": record.get("dims"),
            "already_held": holding is not None,
            "holding": holding,
            "cluster": cluster,
            "position_guidance": guidance,
            "sizing": sizing,
            "divergence_pattern": record.get("divergence_pattern"),
            "liquidity_flag": record.get("liquidity_flag"),
            "trend_years_available": record.get("trend_years_available"),
            "roa_trend_consistent": record.get("roa_trend_consistent"),
            "fcf_positive_years": record.get("fcf_positive_years"),
            "debt_trend": record.get("debt_trend"),
            "financials_as_of": record.get("financials_as_of"),
            "price_as_of": record.get("price_as_of"),
            "warnings": record.get("warnings") or [],
        }
        if not slim:
            entry["metrics"] = record.get("metrics")
            entry["frameworks"] = record.get("frameworks")
            entry["value_screen"] = record.get("value_screen")
            entry["insider"] = record.get("insider")
        out_candidates.append(entry)

    document = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "holdings_on_file": len(holdings),
        "note": None if holdings_on_file else NO_HOLDINGS_NOTE,
        "params": {
            "scored_input": str(scored_path),
            "clustered_input": str(clustered_path) if clustered_doc else None,
            "holdings_input": str(Path(holdings_path) if holdings_path else HOLDINGS_PATH),
            "holdings_template_created": created_template,
            "correlation_threshold": corr_threshold,
            "reduction_factor": reduction_factor,
            "reduction_mode": "flat" if flat_reduction else "proportional",
            "correlation_basis": correlation_basis,
            "top": top,
            "min_composite": min_composite,
            "slim": slim,
            "correlation_panel": corr_meta or None,
        },
        "holdings": holdings,
        "candidates": out_candidates,
    }
    write_json(document, output_path)

    if not quiet:
        held = [c for c in out_candidates if c["already_held"]]
        cut = [c for c in out_candidates if c["sizing"]["correlation_adjusted"]]
        print(f"\n  sized {len(out_candidates)}  ·  already held {len(held)}  "
              f"·  correlation-reduced {len(cut)}")
        for c in sorted(cut, key=lambda c: -(c["sizing"]["max_correlation"] or 0))[:12]:
            s = c["sizing"]
            print(f"    {c['ticker']:<8} {s['base_guide'].split(';')[0]:<28} -> "
                  f"{s['adjusted_guide'].split(';')[0]:<28} "
                  f"(corr {s['max_correlation_raw']:.2f} raw / "
                  f"{s['max_correlation_cleaned']:.2f} cleaned with {s['correlated_with']})")
        for c in held:
            print(f"    {c['ticker']:<8} already held "
                  f"({c['holding'].get('shares')} sh @ {c['holding'].get('cost_basis')})")
        print(f"\n  wrote: {output_path}\n")

    return document


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Stage 3: size the shortlist against current holdings.")
    parser.add_argument("--scored", metavar="PATH", help=f"default {SCORED_PATH}")
    parser.add_argument("--clustered", metavar="PATH", help=f"default {CLUSTERED_PATH}")
    parser.add_argument("--holdings", metavar="PATH", help=f"default {HOLDINGS_PATH}")
    parser.add_argument("--output", metavar="PATH", help=f"default {OUTPUT_PATH}")
    parser.add_argument("--top", type=int, metavar="N",
                        help="size only the N highest composites")
    parser.add_argument("--min-composite", type=float, metavar="SCORE")
    parser.add_argument("--corr-threshold", type=float, default=CORR_THRESHOLD,
                        metavar="R",
                        help=f"correlation with a holding above which size is cut "
                             f"(default {CORR_THRESHOLD})")
    parser.add_argument("--reduction-factor", type=float, default=REDUCTION_FACTOR,
                        metavar="F",
                        help=f"size removed at a correlation of 1.0 "
                             f"(default {REDUCTION_FACTOR})")
    parser.add_argument("--flat-reduction", action="store_true",
                        help="apply the full reduction as soon as the threshold is "
                             "crossed, instead of scaling with correlation")
    parser.add_argument("--correlation-basis", choices=("cleaned", "raw"),
                        default=CORRELATION_BASIS,
                        help=f"apply the threshold to the raw sample correlation or the "
                             f"MP-filtered estimate (default {CORRELATION_BASIS})")
    parser.add_argument("--lookback-years", type=float, metavar="YEARS",
                        help="override the adaptive correlation window")
    parser.add_argument("--slim", action="store_true",
                        help="drop the heavy metrics/frameworks blobs from the output")
    parser.add_argument("--refresh", action="store_true", help="ignore cached prices")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    if args.verbose:
        md.DEBUG = True

    try:
        size_shortlist(scored_path=args.scored, clustered_path=args.clustered,
                       holdings_path=args.holdings, output_path=args.output,
                       top=args.top, min_composite=args.min_composite,
                       corr_threshold=args.corr_threshold,
                       reduction_factor=args.reduction_factor,
                       flat_reduction=args.flat_reduction,
                       correlation_basis=args.correlation_basis,
                       lookback_years=args.lookback_years, slim=args.slim,
                       force_refresh=args.refresh, quiet=args.quiet)
    except FileNotFoundError as e:
        print(f"\n  Missing input: {e}")
        print("  Run stock_evaluator.py --batch (and rmt_cluster.py) first.\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
