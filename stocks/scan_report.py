#!/usr/bin/env python3
r"""
scan_report.py - run the whole scan and render the Tier 1 summary.

    universe_screen  ->  stock_evaluator --batch  ->  sentiment (survivors only)
                     ->  rmt_cluster  ->  position_sizer  ->  this report

Each stage reads the previous stage's file out of C:\Users\joey\stocks\data\.
A stage is skipped when its output is still fresh by market_data.py's TTL
policy, so a second run in the same day re-renders from what is on disk
instead of re-scanning the market. --force re-runs everything.

Freshness cascades: if a stage does re-run, every stage after it re-runs too,
because their inputs just changed. A fresh file downstream of a stale one is
not actually fresh.

Nothing here re-implements a stage. Each is invoked through its own entry
point (universe_screen.main, stock_evaluator.batch_main, rmt_cluster.main,
position_sizer.main), and --detail hands off to stock_evaluator's existing
single-ticker report.

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
import json
import math
import os
import sys
from pathlib import Path
from typing import Optional


def _add_script_dir_to_path():
    here = Path(__file__).resolve().parent
    for candidate in (here, here.parent / "stocks", here.parent):
        if (candidate / "market_data.py").exists():
            if str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
            return here
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    return here


SCRIPT_DIR = _add_script_dir_to_path()

import market_data as md                     # noqa: E402
import universe_screen                       # noqa: E402
import stock_evaluator                       # noqa: E402
import rmt_cluster                           # noqa: E402
import position_sizer                        # noqa: E402


# ─── DISPLAY ───────────────────────────────────────────────────────────────
# Same conventions as stock_evaluator.py / etf.py / op.py / ps2.py.
if sys.platform == "win32":
    os.system("color")

G, Y, R, C, B, D, X = ("\033[92m", "\033[93m", "\033[91m", "\033[96m",
                       "\033[1m", "\033[2m", "\033[0m")


def colour(v, t=None):
    """stock_evaluator.colour, same thresholds."""
    if v is None:
        return f"{D}  n/a{X}"
    t = t if t is not None else f"{v:.2f}"
    return f"{G}{B}{t}{X}" if v >= 7.5 else f"{Y}{t}{X}" if v >= 5.0 else f"{R}{t}{X}"


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

DATA_DIR = Path(os.environ.get("STOCKS_DATA_DIR") or (Path(md.BASE_DIR) / "data"))

CANDIDATES = DATA_DIR / "candidates.json"
SCORED = DATA_DIR / "scored_candidates.json"
SENTIMENT = DATA_DIR / "sentiment.json"
CLUSTERED = DATA_DIR / "clustered.json"
SIZED = DATA_DIR / "sized_candidates.json"

# How many survivors get a sentiment pull. Sentiment costs several requests
# per name (Yahoo news + Reddit search), so it runs on the top of the list.
SENTIMENT_TOP = 25

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


def _num(value) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) else out


# ─── FRESHNESS ─────────────────────────────────────────────────────────────

def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


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
    }

    if module is None:
        document["skipped"].append({"ticker": None,
                                    "reason": "sentiment evaluator not found "
                                              "(looked for ps2.py / ps.py)"})
        write_json(document, output_path)
        if not quiet:
            print(f"  {Y}sentiment evaluator not found - skipping that stage{X}")
        return document

    if not quiet:
        print(f"  sentiment: {len(tickers)} survivor(s) via {path.name}")

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
            return {
                "overall": combined.get("overall"),
                "confidence": combined.get("confidence"),
                "sources": combined.get("contributing_sources"),
                "interpretation": combined.get("interpretation"),
            }

        scores = md.cached_fetch(f"{ticker}_sentiment",
                                 lambda pull=_pull: md.fetch_with_backoff(pull, max_retries=2),
                                 md.TTL_PRICE, cache_type="screener",
                                 force_refresh=force_refresh)
        if scores is None:
            document["skipped"].append({"ticker": ticker, "reason": "sentiment fetch failed"})
            if not quiet:
                print(f"    [{i:>3}/{len(tickers)}] {ticker:<8} {Y}skipped{X}")
            continue
        document["sentiment"][ticker] = scores
        if not quiet:
            overall = scores.get("overall")
            print(f"    [{i:>3}/{len(tickers)}] {ticker:<8} {colour(overall)}")

    write_json(document, output_path)
    return document


def write_json(document, output_path):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(document, f, indent=2, ensure_ascii=False, default=str)
    os.replace(tmp, output_path)
    return output_path


# ─── PIPELINE ──────────────────────────────────────────────────────────────

def run_pipeline(args):
    """Run the stale stages in order. Returns a list of per-stage status rows."""
    status = []
    downstream_forced = bool(args.force)

    def stage(name, output, ttl, runner, label):
        nonlocal downstream_forced
        age = file_age_seconds(output)
        fresh = is_fresh(output, ttl) and not downstream_forced
        if fresh:
            status.append({"stage": name, "action": "skipped (fresh)",
                           "age": age, "output": str(output)})
            if not args.quiet:
                print(f"  {G}✓{X} {label:<22} fresh ({describe_age(age)}) - not re-run")
            return False
        reason = "forced" if downstream_forced else ("missing" if age is None else "stale")
        if not args.quiet:
            print(f"  {C}→{X} {label:<22} {reason} - running")
        runner()
        # Everything after this stage is now working from new inputs.
        downstream_forced = True
        status.append({"stage": name, "action": f"ran ({reason})",
                       "age": file_age_seconds(output), "output": str(output)})
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
        universe_screen.main(argv)
    stage("universe", CANDIDATES, md.TTL_SCREENER, _universe, "universe_screen")

    # 2. evaluator (batch)
    def _evaluate():
        argv = ["--batch", "--quiet"]
        if args.evaluate_limit:
            argv += ["--limit", str(args.evaluate_limit)]
        if args.account_size:
            argv += ["--account-size", str(args.account_size)]
        if args.refresh_prices:
            argv.append("--refresh")
        stock_evaluator.batch_main(argv)
    stage("evaluate", SCORED, md.TTL_PRICE, _evaluate, "stock_evaluator")

    # 3. sentiment over the survivors only
    def _sentiment():
        scored = read_json(SCORED) or {}
        survivors = sorted((scored.get("scored") or []),
                           key=lambda r: -(_num(r.get("composite")) or 0))
        tickers = [r["ticker"] for r in survivors[:args.sentiment_top]]
        run_sentiment_stage(tickers, force_refresh=args.refresh_prices, quiet=args.quiet)
    stage("sentiment", SENTIMENT, md.TTL_PRICE, _sentiment, "sentiment")

    # 4. correlation clusters
    def _cluster():
        argv = ["--quiet"]
        if args.top:
            argv += ["--top", str(args.top)]
        if args.min_composite is not None:
            argv += ["--min-composite", str(args.min_composite)]
        if args.refresh_prices:
            argv.append("--refresh")
        rmt_cluster.main(argv)
    stage("cluster", CLUSTERED, md.TTL_PRICE, _cluster, "rmt_cluster")

    # 5. sizing against holdings
    def _size():
        argv = ["--quiet"]
        if args.top:
            argv += ["--top", str(args.top)]
        if args.refresh_prices:
            argv.append("--refresh")
        position_sizer.main(argv)
    stage("size", SIZED, md.TTL_PRICE, _size, "position_sizer")

    return status


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

    trend = candidate.get("roa_trend_consistent")
    if trend is True:
        cells.append(f"{G}✓trend{X}")
    elif trend is False:
        cells.append(f"{R}✗trend{X}")
    else:
        cells.append(f"{D}·trend{X}")

    pattern = candidate.get("divergence_pattern")
    if pattern == "price_disconnect":
        cells.append(f"{G}disconnect{X}")
    elif pattern == "trend_confirms_decline":
        cells.append(f"{R}value-trap{X}")
    else:
        cells.append(f"{D}·         {X}")

    scores = (sentiment or {}).get(candidate["ticker"]) or {}
    overall = _num(scores.get("overall"))
    if overall is not None:
        cells.append("sent " + colour(overall, f"{overall:.1f}"))
    else:
        cells.append(f"{D}sent  n/a{X}")
    return "  ".join(cells)


def candidate_row(candidate, sentiment, indent=2):
    composite = _num(candidate.get("composite"))
    pad = " " * indent
    held = f" {C}[held]{X}" if candidate.get("already_held") else ""
    sizing = candidate.get("sizing") or {}
    guide = (sizing.get("adjusted_guide") or "").split(";")[0]
    cut = f" {Y}(cut {sizing['reduction']*100:.0f}%){X}" if sizing.get("correlation_adjusted") else ""
    print(f"  {pad}{candidate['ticker']:<9} {colour(composite)} {bar(composite)}  "
          f"{flag_cells(candidate, sentiment)}{held}")
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


def render_report(sized_doc, scored_doc, sentiment_doc, cluster_doc, status, args):
    candidates = sized_doc.get("candidates") or []
    by_ticker = {c["ticker"]: c for c in candidates}
    sentiment = (sentiment_doc or {}).get("sentiment") or {}

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

    # Counts, including anything that fell out along the way.
    scored_counts = (scored_doc or {}).get("counts") or {}
    skipped = len((scored_doc or {}).get("skipped") or [])
    sent_skipped = len((sentiment_doc or {}).get("skipped") or [])
    print(f"  {'Candidates':<18} {len(candidates)} shown  ·  "
          f"{scored_counts.get('scored', '?')} scored of "
          f"{scored_counts.get('candidates', '?')} screened")
    if skipped or sent_skipped:
        parts = []
        if skipped:
            parts.append(f"{skipped} ticker(s) skipped by the evaluator")
        if sent_skipped:
            parts.append(f"{sent_skipped} skipped by sentiment")
        print(f"  {'Partial data':<18} {Y}{'; '.join(parts)}{X}")
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
        print()
        rule("─")
        print(f"  {Y}{DISCLAIMER}{X}")
        rule()
        print()
        return

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
        if resolution == "pick_winner":
            winner_ticker = info.get("winner")
            winner = next((c for c in members if c["ticker"] == winner_ticker), members[0])
            candidate_row(winner, sentiment, indent=2)
            peers = [c for c in members if c["ticker"] != winner["ticker"]]
            if peers:
                print(f"    {D}demoted peers{X}")
                for peer in peers:
                    candidate_row(peer, sentiment, indent=6)
        else:
            for member in members:
                candidate_row(member, sentiment, indent=2)
            line = sector_etf_line([c["ticker"] for c in members], by_ticker)
            if line:
                print(f"    {D}→{X} {line}")

    if standalone:
        print()
        h("STANDALONE")
        rule("─")
        standalone.sort(key=lambda c: -(_num(c.get("composite")) or 0))
        for candidate in standalone:
            candidate_row(candidate, sentiment, indent=2)

    print()
    rule("─")
    h("LEGEND")
    print(f"  {G}✓liq{X}/{R}✗liq{X} liquidity · {G}✓trend{X}/{R}✗trend{X}/{D}·trend{X} "
          f"multi-year ROA (dot = insufficient history)")
    print(f"  {G}disconnect{X} price low, trend intact · {R}value-trap{X} price low, "
          f"trend deteriorating · sent = 0-10 sentiment")
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
    parser.add_argument("--top", type=int, metavar="N",
                        help="shortlist size for clustering and sizing")
    parser.add_argument("--min-composite", type=float, metavar="SCORE")
    parser.add_argument("--sentiment-top", type=int, default=SENTIMENT_TOP, metavar="N",
                        help=f"how many survivors get a sentiment pull "
                             f"(default {SENTIMENT_TOP})")
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

    sized_doc = read_json(SIZED)
    if sized_doc is None:
        print(f"\n  {R}No {SIZED.name} to render.{X}")
        print(f"  Run without --render-only to build it.\n")
        return 2

    render_report(sized_doc, read_json(SCORED), read_json(SENTIMENT),
                  read_json(CLUSTERED), status, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
