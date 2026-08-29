#!/usr/bin/env python3
r"""
rmt_cluster.py - Stage 2: collapse the scored shortlist into correlation clusters.

Stage 1 scores every candidate on its own merits. It has no idea that eight of
its top names are the same trade. This finds the genuine correlation clusters
in the shortlist and, where the members' scores disagree, names the one worth
holding.

The correlation math is the Marchenko-Pastur noise filter from
rmt_etf_analysis (risk.py), reused rather than reimplemented - log_returns()
and marchenko_pastur_upper() below are that script's functions verbatim, and
the decomposition follows the same sequence (log returns -> correlation ->
np.linalg.eigh -> eigenvalues above the MP edge are signal, the rest is noise).

Two things had to be generalized to go from a fixed ETF list to a
variable-length stock shortlist:

  - Adaptive lookback. risk.py uses a fixed 3 years, which is fine for a dozen
    ETFs. MP needs T comfortably larger than N, so the window here scales with
    the shortlist and the achieved ratio is reported.

  - Market-mode adjustment. risk.py compares eigenvalues against the MP edge
    for sigma^2 = 1. That is right for a matrix of pure noise, but a real
    equity panel has a dominant market mode that absorbs much of the variance,
    leaving the residual noise band narrower than sigma^2 = 1 implies. Tested
    on synthetic panels with a market factor plus three planted sector blocks,
    the sigma^2 = 1 threshold recovered one signal of four; rescaling by
    sigma^2 = 1 - lambda_market / N recovered three. Both thresholds are
    reported so the difference stays visible.

All price fetches go through market_data.py (batched yf.download, TTL_PRICE
cache, shared session, backoff).

Setup:
    pip install yfinance curl_cffi requests numpy pandas

Usage:
    py rmt_cluster.py                      # cluster data\scored_candidates.json
    py rmt_cluster.py --top 60             # only the 60 highest composites
    py rmt_cluster.py --min-composite 6.5
    py rmt_cluster.py --lookback-years 3   # override the adaptive window
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import statistics
import sys
import tempfile
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd


# market_data.py sits next to this script (C:\Users\joey\stocks\).
def _import_market_data():
    here = Path(__file__).resolve().parent
    for candidate in (here, here.parent / "stocks", here.parent):
        if (candidate / "market_data.py").exists():
            if str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
            break
    import market_data
    return market_data


md = _import_market_data()


# -------------------------------------------------------------------------
# CONFIG
# -------------------------------------------------------------------------

DATA_DIR = Path(os.environ.get("STOCKS_DATA_DIR") or (Path(md.BASE_DIR) / "data"))
INPUT_PATH = DATA_DIR / "scored_candidates.json"
OUTPUT_PATH = DATA_DIR / "clustered.json"

# Below this many names there is nothing to cluster - a correlation matrix of
# four assets is noise wearing a hat.
MIN_NAMES_FOR_CLUSTERING = 5

# Adaptive lookback: aim for T ~ TARGET_Q * N trading days, clamped to a
# window Yahoo can actually serve.
TARGET_Q = 5.0
MIN_LOOKBACK_YEARS = 3.0    # risk.py's default; "2-5 recommended"
MAX_LOOKBACK_YEARS = 10.0
TRADING_DAYS_PER_YEAR = 252

# MP degenerates as T approaches N; below this ratio the result is not usable.
MIN_USABLE_Q = 1.5

# A name must cover this much of the best-covered ticker's history to enter the
# matrix. Short-history names otherwise drag the shared window down for everyone
# (risk.py calls this the bottleneck and warns about it interactively).
MIN_HISTORY_COVERAGE = 0.90
MIN_SHARED_ROWS = 60

# An eigenvector's members are the names loading above this multiple of the
# uniform level 1/sqrt(N) - i.e. names that load harder than they would if the
# mode were spread evenly over the whole shortlist. The average-correlation
# gate below, not this cutoff, is what keeps junk out.
LOADING_K = 1.0

# A candidate cluster has to show this much real average pairwise correlation.
MIN_CLUSTER_CORR = 0.30
MIN_CLUSTER_SIZE = 2

# When is one member of a cluster actually the pick, rather than the cluster
# being an industry call?
#
# This used to test stdev > 1.0, which is not size-stable: the same standout
# (one name 2.0 above an otherwise identical peer group) gives stdev 1.41 at
# n=2 and 0.71 at n=8, so an identical situation resolved differently
# depending on how many peers happened to be in the cluster. It also fired on
# a smooth gradient with no standout at all - scores 8.0/7.3/6.6/5.9/5.2 give
# stdev 1.11 and "pick_winner", while the top two names are 0.70 apart.
#
# Both tests below are size-independent, and they ask the question directly:
#   - a real winner beats the RUNNER-UP by more than score noise, and
#   - it stands above the BODY of the cluster, not just its nearest peer.
# On the live test clusters this keeps the airlines as pick_winner (DAL leads
# by 0.96) and correctly demotes the semis to industry_wide, where TSM 9.10 /
# MU 9.03 / NVDA 9.00 are a three-way tie the old rule resolved by 0.07.
#
# 0.5 is roughly the resolution of the composite: below that gap the ordering
# is not meaningful. stdev and range are still reported, as description.
MIN_WINNER_GAP = 0.5     # best vs runner-up
MIN_WINNER_LEAD = 1.0    # best vs the median of the rest
DISPERSION_THRESHOLD = MIN_WINNER_LEAD   # kept for callers that pass it in


def _num(value) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) else out


# -------------------------------------------------------------------------
# MATH - lifted from rmt_etf_analysis (risk.py)
# -------------------------------------------------------------------------

def log_returns(prices):
    """risk.py, verbatim."""
    return np.log(prices / prices.shift(1)).dropna()


def marchenko_pastur_upper(N, T):
    """risk.py, verbatim: the MP upper edge for sigma^2 = 1."""
    return (1 + 1.0 / np.sqrt(T / N)) ** 2


def market_adjusted_threshold(eigenvalues, N, T):
    """The same edge, rescaled by the variance the market mode leaves behind.

    The largest eigenvalue is the market: every name loads on it, and the
    variance it absorbs is not available to the noise band. Fitting the band to
    sigma^2 = 1 - lambda_market / N is the standard correction (Laloux et al.,
    Plerou et al.) and is what makes sector structure visible instead of being
    swallowed by the market mode.
    """
    plain = marchenko_pastur_upper(N, T)
    sigma2 = 1.0 - (float(eigenvalues[0]) / N)
    sigma2 = max(sigma2, 1.0 / N)          # never collapse the band to zero
    return plain * sigma2, sigma2, plain


def clean_correlation(decomp):
    """The MP-cleaned correlation matrix: signal eigenvalues kept, the noise
    band flattened to its own average, then rescaled to a unit diagonal.

    Textbook eigenvalue clipping (Laloux et al.). A sample correlation between
    two names is a noisy estimate; this keeps the part the MP edge says is real
    and averages the rest away, which is what you want before acting on a
    single pairwise number. The market mode stays in - two names moving
    together because the whole market moved are still moving together.
    """
    eigenvalues, eigvecs = decomp["eigenvalues"], decomp["eigvecs"]
    n_signal = max(1, decomp["n_signal"])

    cleaned = eigenvalues.copy()
    if n_signal < len(cleaned):
        cleaned[n_signal:] = float(cleaned[n_signal:].mean())

    matrix = eigvecs @ np.diag(cleaned) @ eigvecs.T
    scale = np.sqrt(np.diag(matrix))
    scale[scale == 0] = 1.0
    matrix = matrix / np.outer(scale, scale)
    np.fill_diagonal(matrix, 1.0)
    return pd.DataFrame(matrix, index=decomp["corr"].index, columns=decomp["corr"].columns)


# -------------------------------------------------------------------------
# INPUT
# -------------------------------------------------------------------------

def load_shortlist(input_path, top=None, min_composite=None):
    """Read scored_candidates.json -> (tickers, scores, names, volumes).

    Ordered by composite, best first, so --top takes the best names.
    """
    with open(input_path, "r", encoding="utf-8") as f:
        document = json.load(f)

    rows = []
    for record in document.get("scored") or []:
        ticker = str(record.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        composite = _num(record.get("composite"))
        if min_composite is not None and (composite is None or composite < min_composite):
            continue
        rows.append({
            "ticker": ticker,
            "composite": composite,
            "name": record.get("name"),
            "avg_volume": _num((record.get("metrics") or {}).get("avg_volume")),
        })

    rows.sort(key=lambda r: (r["composite"] is None, -(r["composite"] or 0), r["ticker"]))
    if top:
        rows = rows[:top]

    tickers = [r["ticker"] for r in rows]
    scores = {r["ticker"]: r["composite"] for r in rows}
    names = {r["ticker"]: r["name"] for r in rows if r["name"]}
    volumes = {r["ticker"]: r["avg_volume"] for r in rows if r["avg_volume"]}
    return tickers, scores, names, volumes


def adaptive_lookback(n_names):
    """Years of history to request for n_names, targeting T ~ TARGET_Q * N.

    A fixed window either starves MP on a long shortlist or wastes requests on
    a short one. Returns (years, yfinance period string).
    """
    wanted_days = TARGET_Q * max(1, n_names)
    years = wanted_days / TRADING_DAYS_PER_YEAR
    years = max(MIN_LOOKBACK_YEARS, min(MAX_LOOKBACK_YEARS, years))
    years = round(years, 2)
    period_years = max(1, int(math.ceil(years)))
    return years, f"{period_years}y"


def build_price_frame(tickers, period, force_refresh=False):
    """Batched fetch -> (price frame on the shared calendar, excluded reasons).

    Two ways a ticker drops out here: Yahoo returned nothing for it, or its
    history is too short to sit in the matrix without dragging the shared
    window down for every other name. Both are recorded, never silent.
    """
    excluded = {}
    payload = md.download_prices(tickers, period=period, force_refresh=force_refresh)

    series = {}
    for ticker in tickers:
        rows = payload.get(ticker)
        if not rows:
            excluded[ticker] = "no price history returned"
            continue
        series[ticker] = pd.Series({r["date"]: r["close"] for r in rows}, dtype="float64")

    if not series:
        return None, excluded

    frame = pd.DataFrame(series)
    frame.index = pd.to_datetime(frame.index)
    frame = frame.sort_index()

    counts = frame.count()
    best = int(counts.max()) if len(counts) else 0
    for ticker in list(frame.columns):
        rows = int(counts[ticker])
        if best and rows < MIN_HISTORY_COVERAGE * best:
            excluded[ticker] = (f"insufficient price history ({rows} of {best} rows, "
                                f"under {MIN_HISTORY_COVERAGE:.0%} coverage)")
            frame = frame.drop(columns=[ticker])

    if frame.empty or not len(frame.columns):
        return None, excluded

    return frame.dropna(), excluded


# -------------------------------------------------------------------------
# DECOMPOSITION  (risk.py's sequence, generalized)
# -------------------------------------------------------------------------

def rmt_decompose(prices):
    """Correlation matrix -> eigen spectrum -> MP signal/noise split."""
    N = len(prices.columns)
    T = len(prices) - 1

    returns = log_returns(prices)
    corr = returns.corr()
    eigenvalues, eigvecs = np.linalg.eigh(corr.values)

    order = np.argsort(eigenvalues)[::-1]
    eigenvalues = eigenvalues[order]
    eigvecs = eigvecs[:, order]

    adjusted, sigma2, plain = market_adjusted_threshold(eigenvalues, N, T)
    n_signal = int((eigenvalues > adjusted).sum())
    n_signal_plain = int((eigenvalues > plain).sum())

    signal_eigs = eigenvalues[:n_signal]
    effective_bets = (float(signal_eigs.sum()) ** 2 / float((signal_eigs ** 2).sum())
                      if n_signal else 1.0)

    return {
        "corr": corr,
        "eigenvalues": eigenvalues,
        "eigvecs": eigvecs,
        "N": N, "T": T, "q": T / N if N else 0.0,
        "threshold": float(adjusted),
        "threshold_plain": float(plain),
        "sigma2": float(sigma2),
        "n_signal": n_signal,
        "n_signal_plain": n_signal_plain,
        "effective_bets": float(effective_bets),
        "total_variance": float(eigenvalues.sum()),
    }


def extract_clusters(decomp, tickers, loading_k=LOADING_K, min_corr=MIN_CLUSTER_CORR):
    """Signal eigenvectors -> candidate clusters, validated against real correlation.

    Mode 0 is the market: every name loads on it, so it describes the whole
    shortlist rather than a cluster inside it, and is skipped. Each remaining
    signal mode contributes the names loading above loading_k / sqrt(N).

    Both signs of a mode are candidate clusters, not just the dominant one. A
    mode routinely holds two peer groups moving against each other - the banks
    on one side, the airlines on the other - and keeping only the side with the
    single largest loading throws a genuine cluster away.

    A name that loads on several modes goes to the one it loads on hardest, so
    clusters come out disjoint. Each candidate then has to earn its place with
    real average pairwise correlation.
    """
    eigenvalues, eigvecs = decomp["eigenvalues"], decomp["eigvecs"]
    corr, N = decomp["corr"], decomp["N"]
    cutoff = loading_k / math.sqrt(N)

    best_mode = {}
    for k in range(1, decomp["n_signal"]):
        vector = eigvecs[:, k]
        for i, ticker in enumerate(tickers):
            loading = float(vector[i])
            if abs(loading) > cutoff:
                side = 1 if loading > 0 else -1
                if abs(loading) > best_mode.get(ticker, (None, 0.0))[1]:
                    best_mode[ticker] = ((k, side), abs(loading))

    by_mode = {}
    for ticker, (key, _loading) in best_mode.items():
        by_mode.setdefault(key, []).append(ticker)

    clusters, rejected = [], {}
    for (mode, side), members in sorted(by_mode.items()):
        members = sorted(members)
        if len(members) < MIN_CLUSTER_SIZE:
            for ticker in members:
                rejected[ticker] = "only name loading on its correlation mode"
            continue
        pairs = [float(corr.loc[a, b])
                 for i, a in enumerate(members) for b in members[i + 1:]]
        avg_corr = sum(pairs) / len(pairs) if pairs else 0.0
        if avg_corr < min_corr:
            for ticker in members:
                rejected[ticker] = (f"correlation mode too weak to be a cluster "
                                    f"(avg pairwise {avg_corr:.2f} < {min_corr:.2f})")
            continue
        clusters.append({
            "members": members,
            "avg_correlation": round(avg_corr, 4),
            "mode_rank": mode,
            "mode_side": "positive" if side > 0 else "negative",
            "eigenvalue": round(float(eigenvalues[mode]), 4),
            "pct_variance": round(100 * float(eigenvalues[mode]) / decomp["total_variance"], 2),
        })

    clusters.sort(key=lambda c: (-len(c["members"]), -c["avg_correlation"]))
    return clusters, rejected


def resolve_cluster(cluster, scores):
    """Do the members deserve the same verdict, or is one of them the pick?

    Low score dispersion means the cluster is an industry call - the members
    are near-interchangeable and the decision is whether to own the theme at
    all. High dispersion means Stage 1 separated them, so the best-scoring name
    is the pick and the rest are demoted (kept in the output: a demoted peer is
    still a name you looked at, and may matter if the winner becomes untradeable).
    """
    members = cluster["members"]
    member_scores = {t: scores.get(t) for t in members}
    usable = {t: s for t, s in member_scores.items() if s is not None}

    cluster["scores"] = {t: (round(s, 2) if s is not None else None)
                         for t, s in member_scores.items()}

    if len(usable) < 2:
        cluster.update({"dispersion": None, "score_range": None,
                        "resolution": "industry_wide", "winner": None,
                        "demoted_peers": [],
                        "resolution_note": "not enough scored members to rank"})
        return cluster

    values = sorted(usable.values(), reverse=True)
    dispersion = statistics.stdev(values)
    gap = values[0] - values[1]
    lead = values[0] - statistics.median(values[1:])

    cluster["dispersion"] = round(dispersion, 4)          # descriptive only
    cluster["score_range"] = round(values[0] - values[-1], 4)
    cluster["winner_gap"] = round(gap, 4)
    cluster["winner_lead"] = round(lead, 4)

    if gap >= MIN_WINNER_GAP and lead >= MIN_WINNER_LEAD:
        winner = max(sorted(usable), key=lambda t: usable[t])
        cluster.update({
            "resolution": "pick_winner",
            "winner": winner,
            "demoted_peers": [t for t in members if t != winner],
            "resolution_note": (f"leads the runner-up by {gap:.2f} and the rest of the "
                                f"cluster by {lead:.2f}: a real standout"),
        })
    else:
        if gap < MIN_WINNER_GAP:
            why = (f"top two are {gap:.2f} apart, inside the {MIN_WINNER_GAP:.2f} "
                   f"resolution of the composite - no meaningful winner")
        else:
            why = (f"best name leads the rest by only {lead:.2f} "
                   f"(under {MIN_WINNER_LEAD:.2f}): members score alike")
        cluster.update({
            "resolution": "industry_wide",
            "winner": None,
            "demoted_peers": [],
            "resolution_note": why,
        })
    return cluster


# -------------------------------------------------------------------------
# OUTPUT
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


def cluster_shortlist(input_path=None, output_path=None, top=None, min_composite=None,
                      lookback_years=None, loading_k=LOADING_K, min_corr=MIN_CLUSTER_CORR,
                      dispersion_threshold=None, force_refresh=False, quiet=False):
    """Read the scored shortlist, cluster it, write data\\clustered.json."""
    global DISPERSION_THRESHOLD, MIN_WINNER_LEAD
    if dispersion_threshold is not None:
        DISPERSION_THRESHOLD = MIN_WINNER_LEAD = dispersion_threshold

    input_path = Path(input_path) if input_path else INPUT_PATH
    output_path = Path(output_path) if output_path else OUTPUT_PATH

    tickers, scores, names, volumes = load_shortlist(input_path, top=top,
                                                     min_composite=min_composite)
    if not quiet:
        print(f"\n  shortlist: {len(tickers)} name(s) from {input_path}")

    # Requirement: dedupe AGAIN. universe_screen already did, but a name can be
    # added back by hand afterwards, and a dual-class or cross-listed pair shows
    # up as a ~1.0 correlation that looks exactly like a real cluster.
    deduped, dropped = md.dedupe_tickers(tickers, names=names, volumes=volumes)
    if dropped and not quiet:
        print(f"  dedupe removed {len(dropped)} duplicate entit(y/ies) before correlating:")
        for row in dropped:
            print(f"    {row['ticker']:<10} -> kept {row['kept']:<10} ({row['reason']})")
    tickers = deduped

    params = {
        "input": str(input_path),
        "shortlist_size": len(tickers),
        "top": top,
        "min_composite": min_composite,
        "target_q": TARGET_Q,
        "loading_k": loading_k,
        "min_cluster_correlation": min_corr,
        "dispersion_threshold": DISPERSION_THRESHOLD,
        "min_names_for_clustering": MIN_NAMES_FOR_CLUSTERING,
    }
    standalone_notes = {}

    def bail(note):
        document = {
            "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "insufficient_data_for_clustering": True,
            "note": note,
            "params": params,
            "dropped_duplicates": dropped,
            "clusters": [],
            "standalone": sorted(tickers),
            "standalone_notes": standalone_notes,
        }
        write_json(document, output_path)
        if not quiet:
            print(f"\n  {note}")
            print(f"  every name treated as standalone ({len(tickers)})")
            print(f"  wrote: {output_path}\n")
        return document

    if len(tickers) < MIN_NAMES_FOR_CLUSTERING:
        for ticker in tickers:
            standalone_notes[ticker] = "shortlist too small for correlation analysis"
        return bail(f"shortlist of {len(tickers)} is below the {MIN_NAMES_FOR_CLUSTERING}-name "
                    f"minimum for meaningful correlation analysis")

    if lookback_years:
        years = float(lookback_years)
        period = f"{max(1, int(math.ceil(years)))}y"
    else:
        years, period = adaptive_lookback(len(tickers))
    params["lookback_years"] = years
    params["period"] = period
    if not quiet:
        print(f"  lookback: {years:.2f}y ({period}) for {len(tickers)} names "
              f"- targeting T/N ~ {TARGET_Q:.0f}")

    prices, excluded = build_price_frame(tickers, period, force_refresh=force_refresh)
    standalone_notes.update(excluded)
    if excluded and not quiet:
        print(f"  excluded from the matrix: {len(excluded)} name(s) "
              f"(kept as standalone with a note)")

    if prices is None or len(prices.columns) < MIN_NAMES_FOR_CLUSTERING:
        usable = 0 if prices is None else len(prices.columns)
        return bail(f"only {usable} name(s) had usable price history - below the "
                    f"{MIN_NAMES_FOR_CLUSTERING}-name minimum for correlation analysis")

    if len(prices) - 1 < MIN_SHARED_ROWS:
        return bail(f"shared price window is only {max(0, len(prices) - 1)} trading days "
                    f"(need {MIN_SHARED_ROWS}) - correlations would not be meaningful")

    usable_tickers = list(prices.columns)
    decomp = rmt_decompose(prices)
    params.update({
        "assets_in_matrix": decomp["N"],
        "trading_days": decomp["T"],
        "q_ratio": round(decomp["q"], 2),
        "mp_threshold": round(decomp["threshold"], 4),
        "mp_threshold_sigma1": round(decomp["threshold_plain"], 4),
        "noise_sigma2": round(decomp["sigma2"], 4),
        "signals_found": decomp["n_signal"],
        "signals_found_sigma1": decomp["n_signal_plain"],
        "effective_independent_bets": round(decomp["effective_bets"], 2),
    })

    if not quiet:
        print(f"\n  matrix: {decomp['N']} assets x {decomp['T']} trading days  "
              f"(T/N = {decomp['q']:.1f})")
        print(f"  MP edge: {decomp['threshold']:.3f} (market-adjusted, sigma^2="
              f"{decomp['sigma2']:.3f})  ·  {decomp['threshold_plain']:.3f} (sigma^2=1)")
        print(f"  signals above the noise floor: {decomp['n_signal']} "
              f"({decomp['n_signal_plain']} at sigma^2=1)  ·  "
              f"{decomp['effective_bets']:.1f} effective independent bets")

    if decomp["q"] < MIN_USABLE_Q:
        params["warning"] = (f"T/N = {decomp['q']:.2f} is below {MIN_USABLE_Q}; the "
                             f"correlation matrix is too short to filter reliably. "
                             f"Use --top to shorten the list.")
        if not quiet:
            print(f"  WARNING: {params['warning']}")

    clusters, rejected = extract_clusters(decomp, usable_tickers,
                                          loading_k=loading_k, min_corr=min_corr)
    clusters = [resolve_cluster(c, scores) for c in clusters]

    clustered = {t for c in clusters for t in c["members"]}
    standalone = sorted(set(tickers) - clustered)
    for ticker in standalone:
        if ticker not in standalone_notes:
            standalone_notes[ticker] = rejected.get(
                ticker, "no significant loading on any genuine correlation mode")

    document = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "insufficient_data_for_clustering": False,
        "params": params,
        "dropped_duplicates": dropped,
        "clusters": clusters,
        "standalone": standalone,
        "standalone_notes": standalone_notes,
    }
    write_json(document, output_path)

    if not quiet:
        print(f"\n  {len(clusters)} genuine cluster(s):")
        for c in clusters:
            head = f"    [{c['resolution']}] {len(c['members'])} names  "\
                   f"avg corr {c['avg_correlation']:.2f}  "\
                   f"dispersion {c['dispersion'] if c['dispersion'] is not None else 'n/a'}"
            print(head)
            if c["resolution"] == "pick_winner":
                print(f"       winner {c['winner']} ({c['scores'].get(c['winner'])})  "
                      f"demoted: {', '.join(c['demoted_peers'])}")
            else:
                print(f"       members: {', '.join(c['members'])}")
        print(f"\n  standalone: {len(standalone)}")
        print(f"  wrote: {output_path}\n")

    return document


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Stage 2: RMT correlation clustering over the scored shortlist.")
    parser.add_argument("--input", metavar="PATH",
                        help=f"scored candidates (default {INPUT_PATH})")
    parser.add_argument("--output", metavar="PATH",
                        help=f"output file (default {OUTPUT_PATH})")
    parser.add_argument("--top", type=int, metavar="N",
                        help="cluster only the N highest composites")
    parser.add_argument("--min-composite", type=float, metavar="SCORE",
                        help="drop candidates scoring below this before clustering")
    parser.add_argument("--lookback-years", type=float, metavar="YEARS",
                        help="override the adaptive window")
    parser.add_argument("--loading-k", type=float, default=LOADING_K, metavar="K",
                        help=f"cluster membership cutoff, K/sqrt(N) (default {LOADING_K})")
    parser.add_argument("--min-corr", type=float, default=MIN_CLUSTER_CORR, metavar="R",
                        help=f"minimum average pairwise correlation for a cluster "
                             f"(default {MIN_CLUSTER_CORR})")
    parser.add_argument("--dispersion", type=float, default=DISPERSION_THRESHOLD,
                        metavar="SD",
                        help=f"how far the best name must lead the rest of its cluster "
                             f"to become pick_winner (default {MIN_WINNER_LEAD})")
    parser.add_argument("--refresh", action="store_true", help="ignore cached prices")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="market_data cache/retry trace")
    args = parser.parse_args(argv)

    if args.verbose:
        md.DEBUG = True

    try:
        cluster_shortlist(input_path=args.input, output_path=args.output, top=args.top,
                          min_composite=args.min_composite,
                          lookback_years=args.lookback_years, loading_k=args.loading_k,
                          min_corr=args.min_corr, dispersion_threshold=args.dispersion,
                          force_refresh=args.refresh, quiet=args.quiet)
    except FileNotFoundError as e:
        print(f"\n  Scored shortlist not found: {e}")
        print("  Run stock_evaluator.py --batch first to build data/scored_candidates.json\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
