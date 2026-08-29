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
from stock_evaluator import (position_guidance, score_candidate,      # noqa: E402
                             G, Y, R, X)                              # same palette


# -------------------------------------------------------------------------
# CONFIG
# -------------------------------------------------------------------------

DATA_DIR = Path(os.environ.get("STOCKS_DATA_DIR") or (Path(md.BASE_DIR) / "data"))
HOLDINGS_PATH = DATA_DIR / "holdings.json"
SCORED_PATH = DATA_DIR / "scored_candidates.json"
CLUSTERED_PATH = DATA_DIR / "clustered.json"
SENTIMENT_PATH = DATA_DIR / "sentiment.json"
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

# ── Sentiment x divergence ────────────────────────────────────────────────
# divergence_pattern says what the FINANCIALS think of a depressed price.
# Sentiment says what the PUBLIC thinks. They answer different halves of the
# same question - "is this decline external and temporary" - and a
# price_disconnect with a negative public narrative is a materially different
# situation from one nobody is talking about. ps2.py's composite is centred on
# 5.0 over 0-10, with its own 0-10 confidence.
SENTIMENT_NEGATIVE = 4.0     # below this the narrative is actively negative
SENTIMENT_POSITIVE = 6.0     # above this it is actively positive
SENTIMENT_MIN_CONFIDENCE = 4.0   # below this, sentiment does not get a vote

# How much size a contested read costs. Applied on top of, and reported
# separately from, the correlation scale so either can be reasoned about alone.
CONVICTION_SCALE = {
    "disconnect_supported": 1.00,
    "disconnect_unverified": 1.00,
    "disconnect_contested": 0.75,
    "trap_unverified": 0.65,
    "trap_contested": 0.60,
    "trap_confirmed": 0.50,
    "not_applicable": 1.00,
}

# ── Exit review ───────────────────────────────────────────────────────────
# A holding is re-scored through Stage 1 on every run: the entry pipeline is
# only half a strategy if nothing ever asks whether the thesis still holds.
EXIT_COMPOSITE = 4.0         # at or below this, the case for holding is gone
EXIT_WATCH_COMPOSITE = 5.5   # below this, worth a look
EXIT_DROP = 1.5              # composite fall since the last archived scan

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
# SENTIMENT x DIVERGENCE
# -------------------------------------------------------------------------

def conviction_verdict(divergence, sentiment_scores):
    """Cross the price/fundamentals divergence with the public narrative.

    price_disconnect says the financials do not explain the price. That is
    only the "temporarily out of favour" case if the public story does not
    explain it either - if sentiment is actively negative, the market may be
    pricing something the last annual statements cannot show yet, and the
    right response is less size, not the same size.

    Returns (verdict, detail). Sentiment only votes when it is confident
    enough to be worth listening to.
    """
    detail = {"divergence_pattern": divergence, "sentiment": None,
              "confidence": None, "sentiment_counted": False}

    if divergence not in ("price_disconnect", "trend_confirms_decline"):
        detail["reason"] = "divergence pattern is neutral; nothing to cross"
        return "not_applicable", detail

    overall = _num((sentiment_scores or {}).get("overall"))
    confidence = _num((sentiment_scores or {}).get("confidence"))
    detail["sentiment"] = overall
    detail["confidence"] = confidence

    if overall is None:
        detail["reason"] = "no sentiment on file for this name"
        return ("disconnect_unverified" if divergence == "price_disconnect"
                else "trap_unverified"), detail
    if confidence is not None and confidence < SENTIMENT_MIN_CONFIDENCE:
        detail["reason"] = (f"sentiment confidence {confidence:.1f} below "
                            f"{SENTIMENT_MIN_CONFIDENCE:.1f}; not counted")
        return ("disconnect_unverified" if divergence == "price_disconnect"
                else "trap_unverified"), detail

    detail["sentiment_counted"] = True

    if divergence == "price_disconnect":
        if overall < SENTIMENT_NEGATIVE:
            detail["reason"] = (f"price low and trend intact, but the public read is "
                                f"negative ({overall:.1f}) - the market may know "
                                f"something the statements do not show yet")
            return "disconnect_contested", detail
        detail["reason"] = (f"price low, trend intact, and the public read is "
                            f"{overall:.1f} - neglect rather than news")
        return "disconnect_supported", detail

    # trend_confirms_decline
    if overall > SENTIMENT_POSITIVE:
        detail["reason"] = (f"fundamentals deteriorating while the public read is "
                            f"{overall:.1f} - enthusiasm running ahead of the numbers")
        return "trap_contested", detail
    detail["reason"] = (f"fundamentals deteriorating and the public read is "
                        f"{overall:.1f} - both agree")
    return "trap_confirmed", detail


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
                   flat=False, holdings_on_file=True, basis=CORRELATION_BASIS,
                   sentiment_scores=None, use_conviction=True):
    """Base guidance from stock_evaluator, then two independent modifiers.

    The correlation modifier asks "do I already own this trade". The
    conviction modifier asks "do the financials and the public story agree
    about why the price is where it is". They are computed and reported
    separately, then multiplied, so either can be read on its own.
    """
    metrics = record.get("metrics") or {}
    scores = {"composite": record.get("composite"),
              "dims": record.get("dims") or {}}
    guidance = position_guidance(metrics, scores, record.get("insider"))

    verdict, conviction_detail = conviction_verdict(
        record.get("divergence_pattern"), sentiment_scores)
    conviction_scale = CONVICTION_SCALE.get(verdict, 1.0) if use_conviction else 1.0

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
        "conviction": verdict,
        "conviction_detail": conviction_detail,
        "conviction_scale": conviction_scale,
    }

    def finalize(correlation_scale=1.0):
        """Combine the two modifiers into one adjusted range."""
        total = correlation_scale * conviction_scale
        sizing["scale"] = round(total, 4)
        sizing["reduction"] = round(1.0 - total, 4)
        sizing["adjusted_guide"] = (apply_reduction(guidance["guide"], total)
                                    if total != 1.0 else guidance["guide"])
        sizing["adjusted_low_pct"] = None if base_low is None else round(base_low * total, 2)
        sizing["adjusted_high_pct"] = None if base_high is None else round(base_high * total, 2)
        return guidance, sizing

    if holding is not None:
        sizing["note"] = "already held - not sized as a new position"
        return finalize()

    if not holdings_on_file:
        sizing["note"] = NO_HOLDINGS_NOTE
        return finalize()

    pairs = correlations.get(ticker) or {}
    sizing["correlations"] = pairs
    if not pairs:
        sizing["note"] = "no correlation to holdings available for this name"
        return finalize()

    worst = max(pairs, key=lambda h: pairs[h].get(basis, 0.0))
    worst_corr = pairs[worst].get(basis)
    sizing["max_correlation"] = worst_corr
    sizing["max_correlation_raw"] = pairs[worst].get("raw")
    sizing["max_correlation_cleaned"] = pairs[worst].get("cleaned")

    if worst_corr <= threshold:
        sizing["note"] = (f"no meaningful correlation to holdings (max {worst_corr:.2f} "
                          f"{basis} with {worst}, at or below {threshold:.2f})")
        return finalize()

    scale, reduction = sizing_scale(worst_corr, threshold, reduction_factor, flat=flat)
    sizing.update({
        "correlation_adjusted": True,
        "correlation_scale": round(scale, 4),
        "correlated_with": worst,
        "note": (f"correlation {worst_corr:.2f} ({basis}) with holding {worst} above "
                 f"{threshold:.2f}: size cut {reduction*100:.0f}%"),
    })
    return finalize(scale)


# -------------------------------------------------------------------------
# EXIT REVIEW - re-score what is already owned
# -------------------------------------------------------------------------

def latest_archive_scores(data_dir=None):
    """Composites for every name in the most recent archived scan.

    Gives the exit review a "since when" - a holding that scored 7.8 last
    month and 5.9 today is a different situation from one that has been 5.9
    all along, and only the archive can tell them apart.
    """
    archive = Path(data_dir or DATA_DIR) / "archive"
    if not archive.is_dir():
        return {}, None
    runs = sorted((d for d in archive.iterdir() if d.is_dir()), reverse=True)
    for run in runs:
        document = None
        path = run / "sized_candidates.json"
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    document = json.load(f)
            except Exception:
                document = None
        if not document:
            continue
        scores = {}
        for entry in document.get("candidates") or []:
            scores[entry["ticker"]] = _num(entry.get("composite"))
        for entry in document.get("holdings_review") or []:
            if entry.get("composite") is not None:
                scores[entry["ticker"]] = _num(entry.get("composite"))
        if scores:
            return scores, run.name
    return {}, None


def review_holdings(holdings, scored_records=None, previous=None,
                    account_size=None, force_refresh=False, quiet=False):
    """Run every holding back through Stage 1 and ask whether to keep it.

    The entry pipeline is only half a strategy. This is the other half: the
    same scoring a candidate gets, applied to what is already owned, so a
    thesis that has quietly stopped being true shows up on the same page as
    the new ideas.

    A holding already scored in this run is reused rather than refetched.
    """
    previous = previous or {}
    scored_records = scored_records or {}
    reviews = []

    for holding in holdings:
        ticker = holding["ticker"]
        record = scored_records.get(ticker)
        reason = None
        if record is None:
            record, reason = score_candidate(ticker, account_size=account_size,
                                             force_refresh=force_refresh)

        if record is None:
            reviews.append({"ticker": ticker, "verdict": "unavailable",
                            "reason": reason or "could not be re-scored",
                            "composite": None, "shares": holding.get("shares"),
                            "cost_basis": holding.get("cost_basis")})
            if not quiet:
                print(f"    {ticker:<8} {Y}unavailable{X} - {reason}")
            continue

        composite = _num(record.get("composite"))
        prior = _num(previous.get(ticker))
        delta = None if (composite is None or prior is None) else round(composite - prior, 2)

        reasons = []
        verdict = "hold"
        if composite is not None and composite <= EXIT_COMPOSITE:
            verdict = "exit_review"
            reasons.append(f"composite {composite:.2f} at or below {EXIT_COMPOSITE:.1f}")
        if record.get("divergence_pattern") == "trend_confirms_decline":
            verdict = "exit_review"
            reasons.append("price low and the multi-year trend is deteriorating")
        if delta is not None and delta <= -EXIT_DROP:
            verdict = "exit_review"
            reasons.append(f"composite fell {abs(delta):.2f} since the last archived scan")
        if verdict == "hold":
            if composite is not None and composite < EXIT_WATCH_COMPOSITE:
                verdict = "watch"
                reasons.append(f"composite {composite:.2f} under {EXIT_WATCH_COMPOSITE:.1f}")
            else:
                # roa_trend_consistent alone is far too sensitive to use here:
                # it demands a non-decreasing ROA in every single year, so one
                # down year in four makes it False and it reads as "watch" on
                # names scoring 9+. Real deterioration is rising debt AND weak
                # cash generation together.
                years = (record.get("trend_detail") or {}).get("fcf_years_available") or 0
                positive = record.get("fcf_positive_years")
                weak_fcf = isinstance(positive, int) and years and positive * 2 <= years
                if record.get("debt_trend") == "increasing" and weak_fcf:
                    verdict = "watch"
                    reasons.append(f"debt rising over the window and FCF positive in only "
                                   f"{positive} of {years} years")
        if not reasons:
            reasons.append("fundamentals still support the position")

        reviews.append({
            "ticker": ticker,
            "shares": holding.get("shares"),
            "cost_basis": holding.get("cost_basis"),
            "composite": composite,
            "previous_composite": prior,
            "composite_delta": delta,
            "rating": record.get("rating"),
            "verdict": verdict,
            "reasons": reasons,
            "divergence_pattern": record.get("divergence_pattern"),
            "roa_trend_consistent": record.get("roa_trend_consistent"),
            "debt_trend": record.get("debt_trend"),
            "warnings": record.get("warnings") or [],
        })
        if not quiet:
            colour = {"exit_review": R, "watch": Y}.get(verdict, G)
            shift = "" if delta is None else f"  ({delta:+.2f} since last scan)"
            print(f"    {ticker:<8} {composite if composite is None else f'{composite:.2f}':<6} "
                  f"{colour}{verdict}{X}{shift}  {reasons[0]}")

    return reviews


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
                   lookback_years=None, slim=False, sentiment_path=None,
                   use_conviction=True, review_existing=True,
                   force_refresh=False, quiet=False):
    """Combine scored + clustered + sizing into data\\sized_candidates.json."""
    scored_path = Path(scored_path) if scored_path else SCORED_PATH
    clustered_path = Path(clustered_path) if clustered_path else CLUSTERED_PATH
    output_path = Path(output_path) if output_path else OUTPUT_PATH

    scored_doc, records = load_scored(scored_path)
    cluster_view, shortlist, clustered_doc = load_clusters(clustered_path)

    sentiment_path = Path(sentiment_path) if sentiment_path else SENTIMENT_PATH
    sentiment = {}
    if sentiment_path.exists():
        try:
            with open(sentiment_path, "r", encoding="utf-8") as f:
                sentiment = (json.load(f) or {}).get("sentiment") or {}
        except Exception:
            sentiment = {}

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
            basis=correlation_basis, sentiment_scores=sentiment.get(ticker),
            use_conviction=use_conviction)

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

    holdings_review = []
    if review_existing and holdings:
        previous, previous_run = latest_archive_scores()
        if not quiet:
            print(f"\n  exit review: re-scoring {len(holdings)} holding(s)"
                  + (f" against the {previous_run} scan" if previous_run else ""))
        holdings_review = review_holdings(
            holdings, scored_records=records, previous=previous,
            account_size=None, force_refresh=force_refresh, quiet=quiet)

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
            "conviction_sizing": use_conviction,
            "conviction_scale": CONVICTION_SCALE,
            "sentiment_input": str(sentiment_path) if sentiment else None,
            "sentiment_names": len(sentiment),
        },
        "holdings": holdings,
        "holdings_review": holdings_review,
        "candidates": out_candidates,
    }
    write_json(document, output_path)

    if not quiet:
        held = [c for c in out_candidates if c["already_held"]]
        cut = [c for c in out_candidates if c["sizing"]["correlation_adjusted"]]
        contested = [c for c in out_candidates
                     if c["sizing"]["conviction"] in ("disconnect_contested", "trap_unverified",
                                                      "trap_contested", "trap_confirmed")]
        if contested:
            print(f"\n  conviction modifier applied to {len(contested)}:")
            for c in contested:
                s = c["sizing"]
                print(f"    {c['ticker']:<8} {s['conviction']:<22} "
                      f"x{s['conviction_scale']:.2f}  {s['conviction_detail'].get('reason','')[:60]}")
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
    parser.add_argument("--sentiment", metavar="PATH",
                        help=f"sentiment input (default {SENTIMENT_PATH})")
    parser.add_argument("--no-conviction", action="store_true",
                        help="report the sentiment/divergence verdict but do not let "
                             "it change position size")
    parser.add_argument("--no-exit-review", action="store_true",
                        help="skip re-scoring existing holdings")
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
                       sentiment_path=args.sentiment,
                       use_conviction=not args.no_conviction,
                       review_existing=not args.no_exit_review,
                       force_refresh=args.refresh, quiet=args.quiet)
    except FileNotFoundError as e:
        print(f"\n  Missing input: {e}")
        print("  Run stock_evaluator.py --batch (and rmt_cluster.py) first.\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
