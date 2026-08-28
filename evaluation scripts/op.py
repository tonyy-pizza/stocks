#!/usr/bin/env python3
"""
Options Tier Screener — Clean Build
Cash-secured put / put-spread decision guide using Yahoo Finance via yfinance.

Purpose:
- Show whether Yahoo option-chain data is usable.
- Find available put strikes even when strict filters would reject them.
- List best LOW / MEDIUM / HIGH risk strikes.
- Separately label each tier as BUY/REVIEW, WATCHLIST, or NO TRADE.

Setup:
    pip install yfinance

Usage:
    python options_tier_screener_clean.py BYND
    python options_tier_screener_clean.py MU --allow-earnings
    python options_tier_screener_clean.py AAPL --account 25000 --stock-score 7.4
"""

import argparse
import math
import os
import sys
from datetime import datetime, date
from statistics import mean

import yfinance as yf


# ─── CONFIG ──────────────────────────────────────────────────────

MIN_DTE = 7
MAX_DTE = 60

# Strict table filters
STRICT_MIN_OI = 50
STRICT_MIN_VOL = 0
STRICT_MAX_SPREAD_PCT = 0.25
STRICT_DELTA_MIN = 0.05
STRICT_DELTA_MAX = 0.50

# Idea scan accepts wider data, then grades it.
IDEA_MAX_SPREAD_PCT = 0.90
IDEA_DELTA_MIN = 0.02
IDEA_DELTA_MAX = 0.70

EXEC_FILL_FRACTION = 0.25
CONTRACT_MULTIPLIER = 100
RFR_USD = 0.043
RFR_CAD = 0.030


# ─── DISPLAY ─────────────────────────────────────────────────────

if sys.platform == "win32":
    os.system("color")

G, Y, R, C, B, X = "\033[92m", "\033[93m", "\033[91m", "\033[96m", "\033[1m", "\033[0m"


def rule(ch="═", w=62):
    print("  " + ch * w)


def h(text):
    print(f"  {B}{text}{X}")


def fmt_money(v):
    if v is None:
        return "N/A"
    try:
        v = float(v)
    except Exception:
        return "N/A"
    a = abs(v)
    if a >= 1e12:
        return f"${v/1e12:.2f}T"
    if a >= 1e9:
        return f"${v/1e9:.1f}B"
    if a >= 1e6:
        return f"${v/1e6:.0f}M"
    return f"${v:,.0f}"


# ─── MATH ────────────────────────────────────────────────────────

def norm_cdf(x):
    return (1 + math.erf(x / math.sqrt(2))) / 2


def norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def bs_put(S, K, T, r, sigma):
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return None
    try:
        st = math.sqrt(T)
        d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * st)
        d2 = d1 - sigma * st
        price = K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)
        delta = norm_cdf(d1) - 1
        theta = (-(S * norm_pdf(d1) * sigma) / (2 * st) + r * K * math.exp(-r * T) * norm_cdf(-d2)) / 365
        vega = S * norm_pdf(d1) * st / 100
        p_above_strike = norm_cdf(d2)
        return price, delta, theta, vega, p_above_strike
    except Exception:
        return None


def prob_above_level(S, level, T, r, sigma):
    if S <= 0 or level <= 0 or T <= 0 or sigma <= 0:
        return None
    try:
        st = math.sqrt(T)
        d2 = (math.log(S / level) + (r - 0.5 * sigma * sigma) * T) / (sigma * st)
        return norm_cdf(d2)
    except Exception:
        return None


def calc_hv(prices, window=30):
    if len(prices) < window + 1:
        return None
    rets = []
    for i in range(1, len(prices)):
        if prices[i - 1] > 0 and prices[i] > 0:
            rets.append(math.log(prices[i] / prices[i - 1]))
    rets = rets[-window:]
    if len(rets) < 2:
        return None
    m = mean(rets)
    var = sum((x - m) ** 2 for x in rets) / (len(rets) - 1)
    return math.sqrt(var * 252)


def ma(values, n):
    vals = [v for v in values if v is not None]
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def executable_credit(bid, ask):
    if bid <= 0 or ask <= 0 or ask <= bid:
        return None
    return bid + EXEC_FILL_FRACTION * (ask - bid)


def get_rfr(ticker, currency):
    if ticker.upper().endswith(".TO") or (currency or "").upper() == "CAD":
        return RFR_CAD
    return RFR_USD




def median(values):
    vals = sorted([float(v) for v in values if v is not None])
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2


def option_chain_spot_proxy(t, reported_price, min_dte=MIN_DTE, max_dte=MAX_DTE):
    """
    Detects stale/split-adjusted Yahoo stock price mismatches.

    If reported stock price is far away from the option-chain strike grid,
    the script estimates a usable spot proxy from strikes near the chain's
    densest/median area. This keeps delta/probability calculations from
    breaking when Yahoo's price and option chain are out of sync.
    """
    today = date.today()
    strikes = []

    try:
        expiries = t.options or []
    except Exception:
        return reported_price, {
            "used_proxy": False,
            "reason": "could not fetch option expiries",
            "reported_price": reported_price,
            "proxy_price": None,
            "median_strike": None,
            "min_strike": None,
            "max_strike": None,
            "strike_count": 0,
        }

    # Use the nearest few expiries inside the normal DTE window.
    checked = 0
    for exp_str in expiries:
        try:
            exp = datetime.strptime(exp_str, "%Y-%m-%d").date()
        except Exception:
            continue

        dte = (exp - today).days
        if dte < min_dte or dte > max_dte:
            continue

        try:
            chain = t.option_chain(exp_str)
            if chain.puts is not None and not chain.puts.empty:
                strikes.extend([float(x) for x in chain.puts["strike"].dropna().tolist()])
            if chain.calls is not None and not chain.calls.empty:
                strikes.extend([float(x) for x in chain.calls["strike"].dropna().tolist()])
            checked += 1
        except Exception:
            continue

        if checked >= 4:
            break

    strikes = sorted(set([s for s in strikes if s > 0]))
    med = median(strikes)
    mn = min(strikes) if strikes else None
    mx = max(strikes) if strikes else None

    diag = {
        "used_proxy": False,
        "reason": "price and strike grid appear aligned",
        "reported_price": reported_price,
        "proxy_price": reported_price,
        "median_strike": med,
        "min_strike": mn,
        "max_strike": mx,
        "strike_count": len(strikes),
    }

    if not reported_price or not med:
        diag["reason"] = "insufficient price or strike data"
        return reported_price, diag

    # If reported spot is outside the available strike range by a lot, it is suspicious.
    outside_grid = reported_price > mx * 1.35 or reported_price < mn * 0.65

    # If reported spot is far from the median grid, it is suspicious.
    ratio = reported_price / med if med else 1
    far_from_grid = ratio > 3.0 or ratio < 1/3

    if not (outside_grid or far_from_grid):
        return reported_price, diag

    # Build a practical proxy:
    # Use the median strike grid as a conservative proxy. This is not a quote;
    # it is only a fallback for option math when Yahoo's stock price is broken.
    proxy = med

    diag.update({
        "used_proxy": True,
        "reason": "reported price is incompatible with option-chain strike grid",
        "proxy_price": proxy,
    })

    return proxy, diag


# ─── DATA ────────────────────────────────────────────────────────

def parse_earnings_date(t):
    try:
        cal = t.calendar
        raw = None
        if isinstance(cal, dict):
            ed = cal.get("Earnings Date", [])
            raw = ed[0] if isinstance(ed, list) and ed else ed
        elif hasattr(cal, "loc") and "Earnings Date" in cal.index:
            raw = cal.loc["Earnings Date"].iloc[0]
        if raw is None:
            return None
        if hasattr(raw, "to_pydatetime"):
            return raw.to_pydatetime().date()
        if isinstance(raw, datetime):
            return raw.date()
        if isinstance(raw, date):
            return raw
    except Exception:
        return None
    return None


def get_stock(ticker):
    t = yf.Ticker(ticker)
    info = t.info
    if not info or not info.get("shortName"):
        return None

    hist = t.history(period="1y")
    prices = hist["Close"].dropna().tolist() if hist is not None and not hist.empty else []
    price = info.get("currentPrice") or info.get("regularMarketPrice")
    if not price and prices:
        price = prices[-1]

    calc_price, spot_diag = option_chain_spot_proxy(t, price)

    return {
        "ticker": ticker.upper(),
        "yf": t,
        "name": info.get("longName") or info.get("shortName"),
        "sector": info.get("sector", "N/A"),
        "price": calc_price,
        "reported_price": price,
        "spot_diag": spot_diag,
        "currency": info.get("currency", ""),
        "mktcap": info.get("marketCap"),
        "beta": info.get("beta") or 1.0,
        "pe": info.get("trailingPE"),
        "fwd_pe": info.get("forwardPE"),
        "earnings": parse_earnings_date(t),
        "hv20": calc_hv(prices, 20),
        "hv30": calc_hv(prices, 30),
        "prices": prices,
    }


def expiry_crosses_earnings(exp, earnings):
    return bool(earnings and isinstance(earnings, date) and earnings <= exp)


# ─── OPTION SCAN ─────────────────────────────────────────────────

def scan_puts(stock, allow_earnings=False):
    t = stock["yf"]
    S = stock["price"]
    ticker = stock["ticker"]
    r = get_rfr(ticker, stock["currency"])
    today = date.today()
    earnings = stock.get("earnings")

    diag = {
        "expiries_found": 0,
        "expiries_in_dte": 0,
        "earnings_blocked_expiries": 0,
        "put_rows": 0,
        "call_rows": 0,
        "valid_bidask": 0,
        "positive_credit": 0,
        "valid_iv": 0,
        "valid_delta": 0,
        "inside_idea_delta": 0,
        "inside_strict_delta": 0,
        "strict_accepted": 0,
        "idea_accepted": 0,
        "rejects": {},
        "samples": [],
    }

    def reject(reason):
        diag["rejects"][reason] = diag["rejects"].get(reason, 0) + 1

    strict = []
    ideas = []

    try:
        expiries = t.options or []
    except Exception as e:
        reject(f"no expiries: {e}")
        return strict, ideas, diag

    diag["expiries_found"] = len(expiries)

    for exp_str in expiries:
        try:
            exp = datetime.strptime(exp_str, "%Y-%m-%d").date()
        except Exception:
            reject("bad expiry format")
            continue

        dte = (exp - today).days
        if dte < MIN_DTE or dte > MAX_DTE:
            continue

        diag["expiries_in_dte"] += 1
        crosses_earnings = expiry_crosses_earnings(exp, earnings)
        if crosses_earnings:
            diag["earnings_blocked_expiries"] += 1

        try:
            chain = t.option_chain(exp_str)
            puts = chain.puts
            calls = chain.calls
        except Exception:
            reject("chain fetch error")
            continue

        if calls is not None:
            diag["call_rows"] += len(calls)
        if puts is None or puts.empty:
            reject("no puts")
            continue

        diag["put_rows"] += len(puts)
        T = dte / 365.0

        for _, row in puts.iterrows():
            try:
                K = float(row.get("strike") or 0)
                bid = float(row.get("bid") or 0)
                ask = float(row.get("ask") or 0)
                oi = int(row.get("openInterest") or 0)
                vol = int(row.get("volume") or 0)
                iv = float(row.get("impliedVolatility") or 0)
            except Exception:
                reject("row parse fail")
                continue

            if not (bid > 0 and ask > 0 and ask > bid):
                reject("bad bid/ask")
                continue
            diag["valid_bidask"] += 1

            mid = (bid + ask) / 2
            spread = (ask - bid) / mid if mid > 0 else 9
            credit = executable_credit(bid, ask)
            if not credit or credit <= 0:
                reject("no positive credit")
                continue
            diag["positive_credit"] += 1

            if not (0.03 <= iv <= 4.0):
                reject("invalid IV")
                continue
            diag["valid_iv"] += 1

            bs = bs_put(S, K, T, r, iv)
            if not bs:
                reject("BS failure")
                continue

            _, delta, theta_long, vega_long, p_above_strike = bs
            abs_delta = abs(delta)
            diag["valid_delta"] += 1

            if not (IDEA_DELTA_MIN <= abs_delta <= IDEA_DELTA_MAX):
                reject("delta outside idea range")
                continue
            diag["inside_idea_delta"] += 1

            breakeven = K - credit
            p_profit = prob_above_level(S, breakeven, T, r, iv)
            ann_yield = (credit / K) * (365 / dte) if K and dte else 0
            mos = max(0, (S - breakeven) / S) if S else 0
            liq_score = 0
            if oi >= 100:
                liq_score += 0.35
            elif oi >= 10:
                liq_score += 0.15
            if vol > 0:
                liq_score += 0.15
            if spread <= 0.20:
                liq_score += 0.35
            elif spread <= 0.50:
                liq_score += 0.15

            score = (
                0.25 * (p_profit or 0)
                + 0.20 * ann_yield
                + 0.20 * min(liq_score, 1)
                + 0.15 * mos
                + 0.10 * max(0, 1 - abs(abs_delta - 0.25) / 0.40)
                + 0.10 * (1 if 14 <= dte <= 45 else 0.6)
            )

            c = {
                "expiry": exp_str,
                "expiry_date": exp,
                "dte": dte,
                "strike": K,
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "exec_credit": credit,
                "iv": iv,
                "delta": delta,
                "abs_delta": abs_delta,
                "theta_short": -theta_long,
                "vega_short": -vega_long,
                "p_above_strike": p_above_strike,
                "p_profit_expiry": p_profit,
                "breakeven": breakeven,
                "ann_yield_exec": ann_yield,
                "margin_of_safety": mos,
                "spread": spread,
                "oi": oi,
                "volume": vol,
                "crosses_earnings": crosses_earnings,
                "score": score,
            }

            ideas.append(c)
            diag["idea_accepted"] += 1

            if len(diag["samples"]) < 8:
                diag["samples"].append(c)

            strict_ok = (
                (allow_earnings or not crosses_earnings)
                and STRICT_DELTA_MIN <= abs_delta <= STRICT_DELTA_MAX
                and oi >= STRICT_MIN_OI
                and vol >= STRICT_MIN_VOL
                and spread <= STRICT_MAX_SPREAD_PCT
            )

            if strict_ok:
                strict.append(c)
                diag["strict_accepted"] += 1
                diag["inside_strict_delta"] += 1
            elif STRICT_DELTA_MIN <= abs_delta <= STRICT_DELTA_MAX:
                diag["inside_strict_delta"] += 1

    strict.sort(key=lambda x: (x["expiry"], x["strike"]))
    ideas.sort(key=lambda x: (x["expiry"], x["strike"]))
    return strict, ideas, diag


# ─── TIERING / DECISION ──────────────────────────────────────────

def classify_bias(stock, stock_score=None):
    S = stock["price"]
    prices = stock.get("prices") or []
    ma20 = ma(prices, 20)
    ma50 = ma(prices, 50)
    pts = 0
    reasons = []

    if stock_score is not None:
        if stock_score >= 7.5:
            pts += 2
        elif stock_score >= 6.5:
            pts += 1
        elif stock_score < 5.5:
            pts -= 2
        reasons.append(f"stock score {stock_score:.1f}/10")

    if S and ma20:
        pts += 1 if S > ma20 else -1
        reasons.append(f"price {'above' if S > ma20 else 'below'} 20d MA")
    if S and ma50:
        pts += 1 if S > ma50 else -1
        reasons.append(f"price {'above' if S > ma50 else 'below'} 50d MA")
    if stock.get("beta", 1) >= 1.6:
        pts -= 1
        reasons.append("high beta")
    if stock.get("hv30") and stock["hv30"] >= 0.60:
        pts -= 1
        reasons.append("high realized volatility")

    if pts >= 3:
        bias = "BULLISH"
    elif pts >= 1:
        bias = "CAUTIOUS BULLISH"
    elif pts <= -3:
        bias = "BEARISH / WEAK"
    elif pts <= -1:
        bias = "CAUTIOUS / WEAK"
    else:
        bias = "NEUTRAL"

    if not reasons:
        reasons.append("limited trend/fundamental inputs")

    return bias, pts, reasons, ma20, ma50


def select_tiers(ideas):
    if not ideas:
        return {"LOW": None, "MEDIUM": None, "HIGH": None}

    def choose(lo, hi, target):
        pool = [c for c in ideas if lo <= c["abs_delta"] <= hi]
        if not pool:
            pool = ideas
        return max(pool, key=lambda c: (-abs(c["abs_delta"] - target), c["score"], c["p_profit_expiry"] or 0, -c["spread"]))

    return {
        "LOW": choose(0.02, 0.18, 0.12),
        "MEDIUM": choose(0.18, 0.35, 0.25),
        "HIGH": choose(0.35, 0.70, 0.45),
    }


def decision(stock, c, tier, allow_earnings=False, stock_score=None):
    if not c:
        return "NO TRADE", ["no strike available"], -99

    score = 0
    why = []

    pbe = c.get("p_profit_expiry") or 0
    spread = c.get("spread") or 9
    oi = c.get("oi") or 0
    ann = c.get("ann_yield_exec") or 0
    dte = c.get("dte") or 0

    if pbe >= 0.75:
        score += 2
    elif pbe >= 0.65:
        score += 1
    else:
        score -= 1
        why.append("lower probability above breakeven")

    if spread <= 0.20:
        score += 1
    elif spread <= 0.50:
        why.append("wide but usable spread")
    else:
        score -= 1
        why.append("very wide spread")

    if oi >= 100:
        score += 1
    elif oi >= 10:
        why.append("low open interest")
    else:
        score -= 1
        why.append("very low open interest")

    if 14 <= dte <= 45:
        score += 1
    else:
        why.append("less ideal DTE")

    if ann >= 0.20:
        score += 1
    elif ann < 0.05:
        score -= 1
        why.append("low premium/yield")

    if c.get("crosses_earnings"):
        if allow_earnings:
            score -= 1
            why.append("crosses earnings")
        else:
            score -= 2
            why.append("crosses earnings / conservative no-trade")

    if stock.get("hv30") and stock["hv30"] >= 0.90:
        score -= 2
        why.append("extreme realized volatility")
    elif stock.get("hv30") and stock["hv30"] >= 0.60:
        score -= 1
        why.append("high realized volatility")

    if stock.get("beta", 1) >= 2:
        score -= 1
        why.append("very high beta")

    if stock_score is not None:
        if stock_score >= 7.5:
            score += 1
        elif stock_score < 5.5:
            score -= 2
            why.append("weak stock evaluator score")

    if tier == "LOW" and c["abs_delta"] > 0.25:
        score -= 1
        why.append("too close to money for low-risk tier")

    if score >= 4:
        rec = "BUY / REVIEW"
    elif score >= 2:
        rec = "WATCHLIST / SMALL SIZE"
    else:
        rec = "NO TRADE"

    if not why:
        why.append("passes core option-quality checks")

    return rec, why, score


# ─── REPORT ──────────────────────────────────────────────────────

def print_chain_debug(diag):
    print()
    rule("─")
    h("RAW OPTION CHAIN DEBUG")
    rule("─")
    print(f"  Expiries found:              {diag['expiries_found']}")
    print(f"  Expiries in DTE range:        {diag['expiries_in_dte']}")
    print(f"  Earnings-blocked expiries:    {diag['earnings_blocked_expiries']}")
    print(f"  Put rows reviewed:            {diag['put_rows']}")
    print(f"  Call rows seen:               {diag['call_rows']}")
    print(f"  Rows with valid bid/ask:      {diag['valid_bidask']}")
    print(f"  Rows with positive credit:    {diag['positive_credit']}")
    print(f"  Rows with valid IV:           {diag['valid_iv']}")
    print(f"  Rows with valid delta:        {diag['valid_delta']}")
    print(f"  Inside strict delta range:    {diag['inside_strict_delta']}")
    print(f"  Inside idea delta range:      {diag['inside_idea_delta']}")
    print(f"  Strict accepted candidates:   {diag['strict_accepted']}")
    print(f"  Idea accepted candidates:     {diag['idea_accepted']}")

    if diag["rejects"]:
        print("  Reject summary:")
        for k, v in sorted(diag["rejects"].items(), key=lambda kv: kv[1], reverse=True):
            print(f"    - {k:<24} {v}")

    if diag["samples"]:
        print()
        print("  Sample usable puts before strict filters:")
        print(f"  {'Expiry':<12}{'DTE':>4}{'Strike':>8}{'Bid':>7}{'Ask':>7}{'IV':>7}{'Delta':>8}{'OI':>7}{'Vol':>7}")
        for p in diag["samples"]:
            print(
                f"  {p['expiry']:<12}{p['dte']:>4}{p['strike']:>8.2f}"
                f"{p['bid']:>7.2f}{p['ask']:>7.2f}{p['iv']*100:>6.1f}%"
                f"{p['delta']:>8.2f}{p['oi']:>7,}{p['volume']:>7,}"
            )


def print_report(stock, strict, ideas, diag, args):
    S = stock["price"]
    ticker = stock["ticker"]

    print()
    rule()
    print(f"  {B}{C}{stock['name']} ({ticker}){X}")
    print(f"  {stock['sector']} · Cap: {fmt_money(stock['mktcap'])}")
    print(f"  ${S:.2f} {stock['currency']} · Beta: {stock.get('beta')}")
    sdg = stock.get("spot_diag") or {}
    if sdg.get("used_proxy"):
        print(f"  {Y}⚠ Yahoo reported price ${sdg.get('reported_price'):.2f}, but option strikes suggest ~${sdg.get('proxy_price'):.2f}.{X}")
        print(f"  {Y}  Using option-chain spot proxy for option math only.{X}")
    if stock.get("pe"):
        print(f"  P/E: {stock['pe']:.1f} · Fwd P/E: {stock.get('fwd_pe') or 'N/A'}")
    if stock.get("earnings"):
        print(f"  Earnings date: {stock['earnings']}")
    rule()

    print()
    h("VOLATILITY")
    rule("─")
    print(f"  HV20: {stock['hv20']*100:.1f}%" if stock.get("hv20") else "  HV20: N/A")
    print(f"  HV30: {stock['hv30']*100:.1f}%" if stock.get("hv30") else "  HV30: N/A")

    print_chain_debug(diag)

    print()
    rule("─")
    h("STRICT CSP CANDIDATES")
    rule("─")
    if strict:
        print(f"  {'Expiry':<12}{'DTE':>4}{'Strike':>8}{'Cr':>7}{'Delta':>8}{'P>BE':>7}{'Ann%':>8}{'OI':>7}")
        for c in strict[:20]:
            pbe = (c["p_profit_expiry"] or 0) * 100
            print(f"  {c['expiry']:<12}{c['dte']:>4}{c['strike']:>8.2f}{c['exec_credit']:>7.2f}{c['delta']:>8.2f}{pbe:>6.0f}%{c['ann_yield_exec']*100:>7.1f}%{c['oi']:>7,}")
    else:
        print(f"  {R}No contracts passed strict CSP filters.{X}")
        print("  The tier guide below still lists the best available strikes from the relaxed idea scan.")

    bias, pts, reasons, ma20, ma50 = classify_bias(stock, args.stock_score)
    tiers = select_tiers(ideas)

    print()
    rule("─")
    h("RISK-LEVEL OPTION IDEA GUIDE")
    rule("─")
    print(f"  Market bias: {B}{bias}{X} (score {pts})")
    print(f"  Basis: {', '.join(reasons)}")
    if ma20:
        print(f"  MA20: ${ma20:.2f}" + (f" · MA50: ${ma50:.2f}" if ma50 else ""))
    print(f"  {Y}Best strike is shown first; recommendation is separate.{X}")

    for tier in ("LOW", "MEDIUM", "HIGH"):
        c = tiers[tier]
        rec, why, q = decision(stock, c, tier, args.allow_earnings, args.stock_score)

        print(f"\n  {B}{tier} RISK{X}")
        if not c:
            print("  Best strike:     No strike available")
            print("  Recommendation:  NO TRADE")
            print("  Why:             no usable option-chain data")
            continue

        print(f"  Best strike:     Sell {c['expiry']} ${c['strike']:.2f} put")
        print(f"  Recommendation:  {rec}")
        print(f"  Metrics:         delta {abs(c['delta']):.2f}, credit ${c['exec_credit']:.2f}, breakeven ${c['breakeven']:.2f}, P>BE {(c['p_profit_expiry'] or 0)*100:.1f}%")
        print(f"  Liquidity:       OI {c['oi']:,}, volume {c['volume']:,}, spread {c['spread']*100:.1f}%, DTE {c['dte']}")
        print(f"  Why:             {', '.join(why[:5])}")

    print()
    rule()
    print(f"  {Y}⚠ Informational only. Not financial advice. Verify quotes and risk before trading.{X}")
    rule()
    print()


# ─── MAIN ────────────────────────────────────────────────────────

def evaluate(ticker, args):
    ticker = ticker.upper()
    print(f"\n  {C}Fetching stock/options data for {ticker}...{X}")
    stock = get_stock(ticker)
    if not stock or not stock.get("price"):
        print(f"  {R}Could not load stock data for {ticker}.{X}")
        return

    strict, ideas, diag = scan_puts(stock, allow_earnings=args.allow_earnings)
    print_report(stock, strict, ideas, diag, args)


def parse_args(argv):
    p = argparse.ArgumentParser(description="Clean options tier screener.")
    p.add_argument("ticker", nargs="?", help="Ticker, e.g. BYND, MU, AAPL, AEM.TO")
    p.add_argument("--allow-earnings", action="store_true", help="Do not penalize/filter earnings as heavily.")
    p.add_argument("--account", type=float, default=None, help="Optional account size.")
    p.add_argument("--stock-score", type=float, default=None, help="Score from stock evaluator, 0-10.")
    return p.parse_args(argv)


def main():
    print(f"\n  {B}{C}╔════════════════════════════════════╗{X}")
    print(f"  {B}{C}║   Options Tier Screener - Clean   ║{X}")
    print(f"  {B}{C}╚════════════════════════════════════╝{X}")

    args = parse_args(sys.argv[1:])
    if args.ticker:
        evaluate(args.ticker, args)
        return

    while True:
        try:
            ticker = input(f"\n  {B}Enter ticker (or 'q' to quit): {X}").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n  Goodbye.\n")
            break
        if ticker.lower() in ("q", "quit", "exit"):
            print("  Goodbye.\n")
            break
        if ticker:
            evaluate(ticker, args)


if __name__ == "__main__":
    main()
