#!/usr/bin/env python3
"""
Stock Investment Evaluator v5.4 — Compact Risk-Adjusted Edition
Yahoo Finance via yfinance. Optimized for iPhone a-Shell.

v5.4.2 — the analysis-logic pass. These change which names surface, not just
which numbers are printed, so a scan from before is not comparable with one
after:
- trend_analysis() adds roa_trend, a GRADED direction (improving / flat /
  deteriorating / mixed) from the share of year-over-year steps that improved
  plus the overall change. roa_trend_consistent is kept and still reported, but
  nothing judges on it any more: demanding a non-decreasing ROA in every year
  means it fires more readily the more history a company has, so the same
  business read worse for having reported longer.
- divergence_pattern() counts CATEGORIES, not signals. Revenue-declining and
  net-income-declining were two of five votes and the trap threshold was two,
  so one soft year — revenue down, profit down with it — labelled a company a
  value trap on its own and cost it 35–50% of its position size through the
  conviction modifier. They are now one "latest year" category alongside
  profitability, cash generation and balance sheet.
- data_coverage() records what share of the scoring inputs a ticker actually
  had. score() returns a neutral 5.0 for anything missing, so a name with
  almost no data landed near 5.0 and read as a considered HOLD. It does not
  touch the composite; below LOW_COVERAGE it raises a "thin data" risk flag,
  which is how this codebase reduces size without disturbing the calibration.
- liquidity_check() takes an fx_rate and converts a stock's dollar volume into
  the account's currency before comparing. A TSX name's CAD volume measured
  against a USD account overstated its liquidity by the whole exchange rate.
  Without a rate it falls back to the old unconverted comparison and says so.
- dcf_scenarios() abstains on negative free cash flow instead of returning a
  large negative intrinsic value that build_scores read as "very expensive".
  A cash burn is not an overpriced company, and it is already scored through
  cash runway, FCF quality, growth and Piotroski.

v5.4.1 fixes (the first one DOES move composites — see below):
- PEG is read again. yfinance dropped `pegRatio` from .info in favour of
  `trailingPegRatio`, so m["peg"] had been None for every ticker: the report's
  PEG row was permanently N/A and build_scores() always took the peg-excluded
  weighting. Both key names are now read and m["peg_source"] records which one
  answered. Composites shift slightly for any name Yahoo has a trailing PEG
  for — that is the metric doing what it was always meant to do, but a scan
  from before this change is not directly comparable with one after it.

v5.4 changes vs v5.3 (all additive — the scoring math is untouched):
- Adds batch mode: evaluate_universe() scores the candidate universe that
  universe_screen.py writes, and writes data/scored_candidates.json plus a
  list of the tickers it had to skip and why. One bad ticker never stops the
  run. Reached from the CLI with --batch; plain `stock_evaluator.py TICKER`
  is unchanged.
- Batch fetches route through market_data.py (shared session, disk cache,
  exponential backoff) instead of raw yfinance, and rebuild the same pandas
  frames, so calc_metrics/piotroski/altman_z/magic_formula run as they are.
- Adds trend_analysis(): the year-over-year checks (ROA improving, debt
  decreasing) extended across every annual period Yahoo returns, reported as
  trend_years_available / roa_trend_consistent / fcf_positive_years /
  debt_trend. Under two years these read "insufficient history".
- Adds liquidity_check(): what share of average daily dollar volume one
  position would be, given an account size. Flags, never excludes.
- Adds divergence_pattern(): price_disconnect (price low, trend intact) vs
  trend_confirms_decline (price low, trend rolling over) vs neutral.
- Records financials_as_of and price_as_of per ticker: info is cached on the
  1-day price TTL, statements on the 7-day fundamentals TTL, so the two
  timestamps legitimately differ.

v5.3 changes vs v5.2:
- Adds value_screen(): a 52-week-low value flag. When a stock trades in the
  bottom 20% of its 52-week range, it uses the Health and Profitability
  dimension scores to distinguish a potential value entry from a value trap.
- Adds portfolio/batch mode: pass multiple tickers as CLI args to evaluate
  them sequentially, followed by a sector diversification summary that flags
  sector concentration risk.
- Both additions are informational only — they do NOT alter the composite
  score, same pattern as insider conviction and cash runway.

v5 changes vs v4:
- Adds R&D intensity (R&D / revenue), shown for relevant sectors.
- Adds insider conviction summary (6-month net buy/sell from Form 4 data).
- Adds cash runway, gated to cash-burning companies only.
- These three are informational/flags — they do NOT alter the composite
  score, to preserve the v3/v4 scoring calibration.
- New risk flag: heavy insider net selling.

v4 changes vs v3:
- Interest coverage capped at 30x for scoring (display unchanged)
- 52W high/low prefers info.fiftyTwoWeek* over historical
- Expanded sector list: 11 sectors + default
- Graduated valuation penalty when val_s < 6
- Beta-adjusted WACC in DCF (+/- 2% per beta point from 1.0)
- Gross margin warning for Basic Materials/Energy >55%
- Stale data check (annual statements >425d old)
- PEG excluded from scoring when extreme, not penalized
- EBIT normalized for cyclicals in Magic Formula
- CLI mode: python stock_evaluator.py TICKER

Setup: pip install yfinance
Usage: python stock_evaluator.py [TICKER [TICKER ...]]   # single-ticker, unchanged
       python stock_evaluator.py --batch                 # score data/candidates.json
       python stock_evaluator.py --batch --account-size 25000
"""

import math, os, sys
from datetime import datetime
from statistics import mean
import yfinance as yf

# ─── DISPLAY ───────────────────────────────────────────────────────────────
if sys.platform == "win32":
    os.system("color")

G, Y, R, C, B, X = "\033[92m", "\033[93m", "\033[91m", "\033[96m", "\033[1m", "\033[0m"

def colour(v, t=None):
    t = t if t is not None else f"{v:.2f}"
    return f"{G}{B}{t}{X}" if v >= 7.5 else f"{Y}{t}{X}" if v >= 5.0 else f"{R}{t}{X}"

def bar(s, w=14):
    s = max(0, min(10, s or 0))
    f = int(round((s / 10) * w))
    return "█" * f + "░" * (w - f)

# ─── HELPERS ───────────────────────────────────────────────────────────────
def rv(df, row, col=0):
    """Row value: safely fetch a cell from a yfinance DataFrame."""
    try:
        if df is None or df.empty or row not in df.index:
            return None
        v = df.loc[row].iloc[col]
        return None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)
    except Exception:
        return None

def rf(df, names, col=0):
    """Row first: try multiple row names, return first hit."""
    for n in names:
        v = rv(df, n, col)
        if v is not None:
            return v
    return None

def sd(a, b, default=None):
    try:
        return a / b if a is not None and b not in (None, 0) else default
    except Exception:
        return default

def num(x):
    """Coerce to float, returning None for None/NaN/non-numeric."""
    try:
        if x is None:
            return None
        x = float(x)
        return None if math.isnan(x) else x
    except Exception:
        return None

def avg(values):
    vs = [v for v in values if v is not None]
    return mean(vs) if vs else None

def clamp(x, lo, hi):
    return None if x is None else max(lo, min(hi, x))

def fmt_money(v):
    if v is None: return "N/A"
    a = abs(v)
    if a >= 1e12: return f"${v/1e12:.2f}T"
    if a >= 1e9:  return f"${v/1e9:.1f}B"
    if a >= 1e6:  return f"${v/1e6:.0f}M"
    return f"${v:,.0f}"

def fmt_sh(v):
    if v is None: return "N/A"
    a = abs(v)
    if a >= 1e9: return f"{v/1e9:.2f}B sh"
    if a >= 1e6: return f"{v/1e6:.2f}M sh"
    if a >= 1e3: return f"{v/1e3:.1f}K sh"
    return f"{v:,.0f} sh"

# ─── SECTOR RULES ──────────────────────────────────────────────────────────
# Weights: (valuation, profitability, growth, health, momentum, frameworks)
W_CYC  = (0.25, 0.20, 0.10, 0.25, 0.10, 0.10)  # cyclicals
W_GROW = (0.18, 0.24, 0.24, 0.18, 0.10, 0.06)  # growth/tech
W_DEF  = (0.20, 0.25, 0.20, 0.20, 0.10, 0.05)  # default/stable
W_FIN  = (0.25, 0.25, 0.10, 0.25, 0.10, 0.05)  # financials
W_REIT = (0.25, 0.15, 0.15, 0.25, 0.10, 0.10)  # real estate
W_UTIL = (0.25, 0.20, 0.10, 0.25, 0.10, 0.10)  # utilities

# Tuple: (pe_g, pe_b, peg_g, peg_b, ev_g, ev_b, ps_g, ps_b, pb_g, pb_b,
#         roe_g, roic_g, growth_cap, cyclical, weights)
SECTORS = {
    "Basic Materials":        (12, 35, 0.8, 4.0,  6, 18, 2.0, 10, 1.5, 5.0, 0.15, 0.12, 0.12, True,  W_CYC),
    "Energy":                 (10, 30, 0.8, 4.0,  5, 16, 1.0,  6, 1.2, 4.0, 0.15, 0.12, 0.10, True,  W_CYC),
    "Financial Services":     (10, 25, 0.8, 3.0,  8, 30, 2.0,  8, 1.0, 3.0, 0.12, 0.08, 0.12, True,  W_FIN),
    "Technology":             (25, 80, 1.0, 5.0, 15, 50, 5.0, 25, 4.0, 18,  0.20, 0.15, 0.25, False, W_GROW),
    "Healthcare":             (18, 50, 1.0, 4.0, 12, 35, 3.0, 15, 3.0, 12,  0.15, 0.12, 0.15, False, W_DEF),
    "Consumer Cyclical":      (15, 40, 1.0, 4.0, 10, 25, 1.5,  8, 2.0,  8,  0.15, 0.12, 0.15, True,  W_CYC),
    "Consumer Defensive":     (18, 35, 1.0, 3.0, 12, 25, 1.5,  6, 3.0,  8,  0.18, 0.12, 0.10, False, W_DEF),
    "Industrials":            (15, 35, 1.0, 4.0, 10, 25, 1.5,  6, 2.5,  8,  0.15, 0.12, 0.15, False, W_DEF),
    "Real Estate":            (20, 60, 1.5, 5.0, 15, 40, 4.0, 15, 1.5,  4,  0.08, 0.06, 0.08, True,  W_REIT),
    "Utilities":              (15, 25, 1.5, 4.0, 10, 18, 2.0,  6, 1.5,  4,  0.10, 0.07, 0.05, False, W_UTIL),
    "Communication Services": (18, 45, 1.0, 4.0, 10, 30, 2.0, 10, 2.0,  8,  0.15, 0.12, 0.15, False, W_DEF),
}
DEFAULT_S = (15, 60, 0.8, 4.0, 8, 30, 1.0, 15, 1.5, 6.0, 0.20, 0.15, 0.20, False, W_DEF)
S_KEYS = ["pe_g","pe_b","peg_g","peg_b","ev_g","ev_b","ps_g","ps_b","pb_g","pb_b",
          "roe_g","roic_g","gcap","cyc","weights"]

# Sectors where R&D intensity is a genuinely meaningful signal
RND_SECTORS = ("Technology", "Healthcare", "Communication Services", "Industrials")

def srules(sector):
    return dict(zip(S_KEYS, SECTORS.get(sector, DEFAULT_S)))

# ─── DATA FETCH ────────────────────────────────────────────────────────────
def get_data(ticker):
    print(f"\n  {C}Fetching data for {ticker.upper()}...{X}")
    try:
        t = yf.Ticker(ticker)
        info = t.info
        if not info or info.get("quoteType") is None or info.get("shortName") is None:
            print(f"\n  {R}Ticker '{ticker}' not found.{X}\n")
            return None

        # Insider data is optional — never let it break the run
        ins_purch = None
        try:
            ins_purch = t.insider_purchases
        except Exception:
            ins_purch = None

        return {"ticker": ticker.upper(), "info": info, "fin": t.financials,
                "bal": t.balance_sheet, "cf": t.cashflow,
                "hist": t.history(period="1y"), "insider_purchases": ins_purch}
    except Exception as e:
        print(f"\n  {R}Error fetching data: {e}{X}\n")
        return None

# ─── METRICS ───────────────────────────────────────────────────────────────
def calc_metrics(d):
    info, fin, bal, cf, hist = d["info"], d["fin"], d["bal"], d["cf"], d["hist"]
    m = {"warnings": [], "notes": []}

    # Identity
    m["name"] = info.get("longName") or info.get("shortName")
    m["sector"] = info.get("sector", "N/A")
    m["industry"] = info.get("industry", "N/A")
    m["exchange"] = info.get("exchange", "N/A")
    m["quote_currency"] = info.get("currency")
    m["financial_currency"] = info.get("financialCurrency")
    m["price"] = info.get("currentPrice") or info.get("regularMarketPrice")
    m["mktcap"] = info.get("marketCap")
    m["beta"] = info.get("beta")

    if m["quote_currency"] and m["financial_currency"] and m["quote_currency"] != m["financial_currency"]:
        m["warnings"].append(f"Currency mismatch: quotes in {m['quote_currency']}, financials in {m['financial_currency']}.")

    rules = srules(m["sector"])
    if rules["cyc"]:
        m["warnings"].append("Cyclical/commodity sector: growth metrics normalized.")

    # Stale data check
    try:
        if fin is not None and not fin.empty and hasattr(fin.columns[0], "to_pydatetime"):
            age = (datetime.now() - fin.columns[0].to_pydatetime()).days
            if age > 425:
                m["warnings"].append(f"Most recent annual statements are {age} days old.")
    except Exception:
        pass

    # Valuation
    m["pe"] = info.get("trailingPE")
    m["fwd_pe"] = info.get("forwardPE")
    # PEG: yfinance dropped `pegRatio` from .info and now exposes the trailing
    # figure as `trailingPegRatio`. Reading only the old key meant PEG was
    # silently None for every ticker, so the valuation blend always took the
    # peg-excluded branch and the report's PEG row was permanently N/A. Both
    # keys are read, newest name last, and which one answered is recorded so a
    # future rename is visible instead of quietly zeroing the metric again.
    m["peg"], m["peg_source"] = None, None
    for key in ("pegRatio", "trailingPegRatio"):
        value = num(info.get(key))
        if value is not None:
            m["peg"], m["peg_source"] = value, key
            break
    m["ps"] = info.get("priceToSalesTrailing12Months")
    m["pb"] = info.get("priceToBook")
    m["ev_ebitda"] = info.get("enterpriseToEbitda")

    if m.get("peg") is not None and (m["peg"] > 10 or m["peg"] < 0):
        m["warnings"].append(f"PEG {m['peg']:.2f} is unreliable; excluded from valuation score.")
        m["peg_excluded"] = True

    # Profitability
    m["gross_margin"] = info.get("grossMargins")
    m["op_margin"] = info.get("operatingMargins")
    m["net_margin"] = info.get("profitMargins")
    m["roe"] = info.get("returnOnEquity")
    m["roa"] = info.get("returnOnAssets")

    if m["sector"] in ("Basic Materials", "Energy") and m.get("gross_margin") and m["gross_margin"] > 0.55:
        m["warnings"].append(f"Gross margin {m['gross_margin']*100:.1f}% high for sector — likely excludes D&A/royalties.")

    # ROIC
    ebit = rf(fin, ["EBIT", "Operating Income"])
    tax_r = info.get("effectiveTaxRate")
    if tax_r is None or tax_r < 0 or tax_r > 0.6: tax_r = 0.21
    equity = rf(bal, ["Stockholders Equity", "Total Stockholders Equity", "Common Stock Equity"])
    debt = rf(bal, ["Total Debt", "Long Term Debt"]) or 0
    cash = rf(bal, ["Cash And Cash Equivalents", "Cash", "Cash Cash Equivalents And Short Term Investments"]) or 0
    inv_cap = (equity or 0) + debt - cash
    m["roic"] = sd(ebit * (1 - tax_r), inv_cap) if ebit and inv_cap > 0 else None
    m["ebit"] = ebit

    # Growth (raw values for display)
    rev0, rev1 = rv(fin, "Total Revenue", 0), rv(fin, "Total Revenue", 1)
    m["revenue"] = rev0
    m["rev_growth_raw"] = sd(rev0 - rev1, abs(rev1)) if rev0 and rev1 else None
    ni0, ni1 = rv(fin, "Net Income", 0), rv(fin, "Net Income", 1)
    m["ni_growth"] = sd(ni0 - ni1, abs(ni1)) if ni0 and ni1 else None
    m["eps"] = info.get("trailingEps")
    m["eps_growth_raw"] = info.get("earningsGrowth")

    # ─── R&D intensity (v5) ────────────────────────────────────────────────
    rnd = rf(fin, ["Research And Development", "Research Development"])
    m["rnd"] = rnd
    m["rnd_intensity"] = sd(rnd, rev0) if rnd and rev0 else None
    if m["rnd_intensity"] is None and m["sector"] in RND_SECTORS:
        m["notes"].append("R&D line not reported separately by Yahoo for this company.")

    # FCF normalization
    fcf_vals = [rv(cf, "Free Cash Flow", i) for i in range(4)]
    fcf0, fcf1 = fcf_vals[0], fcf_vals[1]
    m["fcf_latest"] = fcf0
    m["fcf_avg_3y"] = avg(fcf_vals[:3])
    if rules["cyc"] and m["fcf_avg_3y"] is not None and fcf0 is not None:
        m["fcf_normalized"] = min(fcf0, m["fcf_avg_3y"])
        m["notes"].append("DCF uses normalized FCF for cyclical sector.")
    else:
        m["fcf_normalized"] = fcf0
    m["fcf_growth_raw"] = sd(fcf0 - fcf1, abs(fcf1)) if fcf0 and fcf1 else None

    # Capped growth for cyclicals (used in scoring only)
    cap, cyc = rules["gcap"], rules["cyc"]
    m["rev_growth"] = clamp(m["rev_growth_raw"], -0.30, cap) if cyc else m["rev_growth_raw"]
    m["eps_growth"] = clamp(m["eps_growth_raw"], -0.30, cap) if cyc else m["eps_growth_raw"]
    m["fcf_growth"] = clamp(m["fcf_growth_raw"], -0.30, cap) if cyc else m["fcf_growth_raw"]

    # ─── Cash runway (v5) — only meaningful for cash-burning firms ──────────
    ocf = rf(cf, ["Operating Cash Flow", "Total Cash From Operating Activities",
                  "Cash Flow From Continuing Operating Activities"])
    cash_pos = rf(bal, ["Cash Cash Equivalents And Short Term Investments",
                        "Cash And Cash Equivalents", "Cash"])
    m["operating_cf"] = ocf
    m["cash_position"] = cash_pos
    burns = []
    if ocf is not None and ocf < 0:  burns.append(-ocf)
    if fcf0 is not None and fcf0 < 0: burns.append(-fcf0)
    annual_burn = max(burns) if burns else None  # most conservative (largest) burn
    m["annual_burn"] = annual_burn
    if annual_burn and cash_pos and cash_pos > 0:
        m["runway_years"] = cash_pos / annual_burn
        m["runway_months"] = m["runway_years"] * 12
        if m["runway_months"] < 12:
            m["warnings"].append(f"Short cash runway: ~{m['runway_months']:.0f} months at current burn rate.")
    else:
        m["runway_years"] = None
        m["runway_months"] = None
        if annual_burn and not cash_pos:
            m["notes"].append("Company is burning cash but cash position could not be read.")

    # Health
    m["curr_ratio"] = info.get("currentRatio")
    m["quick_ratio"] = info.get("quickRatio")
    m["debt_eq"] = info.get("debtToEquity")
    if m["debt_eq"] is not None: m["debt_eq"] /= 100
    int_exp = rv(fin, "Interest Expense")
    if int_exp and int_exp < 0: int_exp = abs(int_exp)
    m["int_coverage"] = sd(ebit, int_exp) if ebit and int_exp else None
    m["fcf_quality"] = sd(fcf0, ni0) if fcf0 and ni0 else None

    # 52W: prefer info.fiftyTwoWeek*, fallback to history
    m["52w_high"] = info.get("fiftyTwoWeekHigh")
    m["52w_low"] = info.get("fiftyTwoWeekLow")
    if (m["52w_high"] is None or m["52w_low"] is None) and hist is not None and not hist.empty:
        prices = hist["Close"].dropna().tolist()
        if prices:
            m["52w_high"] = m["52w_high"] or max(prices)
            m["52w_low"] = m["52w_low"] or min(prices)
    px = m["price"]
    if m["52w_high"] and m["52w_low"] and px and m["52w_high"] != m["52w_low"]:
        m["pos_52w"] = (px - m["52w_low"]) / (m["52w_high"] - m["52w_low"])
    else:
        m["pos_52w"] = None

    m["book_value"] = info.get("bookValue")
    m["shares"] = info.get("sharesOutstanding")
    m["net_income"] = ni0
    return m

# ─── VALUE SCREEN (v5.3) ───────────────────────────────────────────────────
def value_screen(m, dims):
    """
    52-week-low value flag. Triggers when a stock trades in the bottom 20% of
    its 52-week range, then uses the Health and Profitability dimension scores
    (from build_scores) to distinguish a potential value entry from a value
    trap.

    Informational only — NOT folded into the composite score, same pattern as
    insider_conviction and cash runway.
    """
    pos = m.get("pos_52w")
    if pos is not None and pos <= 0.20:
        if dims.get("Health", 0) >= 5.5 and dims.get("Profitability", 0) >= 5.5:
            verdict = "Near 52-week low — fundamentals intact, potential value entry"
        else:
            verdict = "Near 52-week low — check for value trap, fundamentals weak"
        return {"triggered": True, "verdict": verdict}
    return {"triggered": False}

# ─── FRAMEWORKS ────────────────────────────────────────────────────────────
def piotroski(d):
    info, fin, bal, cf = d["info"], d["fin"], d["bal"], d["cf"]
    score, sigs = 0, {}
    def add(label, cond):
        nonlocal score
        sigs[label] = 1 if bool(cond) else 0
        score += sigs[label]

    roa0 = info.get("returnOnAssets")
    roa1 = sd(rv(fin, "Net Income", 1), rv(bal, "Total Assets", 1))
    fcf = rv(cf, "Free Cash Flow", 0)
    cfo = rf(cf, ["Operating Cash Flow", "Total Cash From Operating Activities", "Cash Flow From Continuing Operating Activities"])
    ni0 = rv(fin, "Net Income", 0)
    rev0, rev1 = rv(fin, "Total Revenue", 0), rv(fin, "Total Revenue", 1)
    gp0, gp1 = rv(fin, "Gross Profit", 0), rv(fin, "Gross Profit", 1)
    td0, td1 = rv(bal, "Total Debt", 0), rv(bal, "Total Debt", 1)
    cr0 = info.get("currentRatio")

    add("ROA > 0", roa0 and roa0 > 0)
    add("FCF > 0", fcf and fcf > 0)
    add("ROA improving", roa0 and roa1 and roa0 > roa1)
    add("CFO > Net Income", cfo and ni0 and cfo > ni0)
    add("Debt decreasing", td0 is not None and td1 is not None and td0 < td1)
    add("Current ratio > 1", cr0 and cr0 > 1)
    add("Positive net income", ni0 and ni0 > 0)
    add("Revenue growing", rev0 and rev1 and rev0 > rev1)
    add("Gross profit growing", gp0 and gp1 and gp0 > gp1)

    label = "Strong" if score >= 7 else "Neutral" if score >= 4 else "Weak"
    return {"score": score, "max": 9, "label": label, "signals": sigs}

def altman_z(d):
    info, fin, bal = d["info"], d["fin"], d["bal"]
    ta = rv(bal, "Total Assets", 0)
    if not ta: return None
    ca = rv(bal, "Current Assets", 0) or 0
    cl = rv(bal, "Current Liabilities", 0) or 0
    re = rv(bal, "Retained Earnings", 0) or 0
    ebit = rf(fin, ["EBIT", "Operating Income"]) or 0
    rev = rv(fin, "Total Revenue", 0) or 0
    tl = rf(bal, ["Total Liabilities Net Minority Interest", "Total Liabilities"]) or 0
    mc = info.get("marketCap") or 0
    z = (1.2*sd(ca-cl, ta, 0) + 1.4*sd(re, ta, 0) + 3.3*sd(ebit, ta, 0) +
         0.6*sd(mc, tl, 0) + sd(rev, ta, 0))
    zone = "Safe" if z > 3.0 else "Grey" if z > 1.8 else "Distress"
    warning = None
    if info.get("financialCurrency") and info.get("currency") and info.get("financialCurrency") != info.get("currency"):
        warning = "Z-score may need FX adjustment due to currency mismatch."
    return {"score": round(z, 2), "zone": zone, "warning": warning}

def graham_number(m):
    eps, bvps, px = m.get("eps"), m.get("book_value"), m.get("price")
    if not eps or not bvps or eps <= 0 or bvps <= 0: return None
    gn = math.sqrt(22.5 * eps * bvps)
    mos = sd(gn - px, px) if px else None
    return {"graham": round(gn, 2), "price": px, "mos": round(mos*100, 1) if mos is not None else None}

def magic_formula(d, m):
    """Magic Formula: EBIT/EV. EBIT normalized for cyclicals (min of latest vs 3y avg)."""
    info, fin, bal = d["info"], d["fin"], d["bal"]
    rules = srules(m.get("sector"))

    # EBIT normalization for cyclicals
    if rules["cyc"]:
        ebit_vals = [rf(fin, ["EBIT", "Operating Income"], i) for i in range(3)]
        ebit_avg = avg(ebit_vals)
        ebit_latest = ebit_vals[0] if ebit_vals else None
        ebit = min(ebit_latest, ebit_avg) if ebit_latest and ebit_avg else (ebit_latest or ebit_avg)
    else:
        ebit = m.get("ebit") or rf(fin, ["EBIT", "Operating Income"])

    market_cap = info.get("marketCap")
    debt = rf(bal, ["Total Debt", "Long Term Debt"]) or 0
    cash = rf(bal, ["Cash And Cash Equivalents", "Cash", "Cash Cash Equivalents And Short Term Investments"]) or 0
    roic = m.get("roic")

    if not ebit or not market_cap or not roic:
        ev_eb = m.get("ev_ebitda")
        if ev_eb and ev_eb > 0 and roic:
            return {"ey": round((1/ev_eb)*100, 2), "roic": round(roic*100, 2),
                    "combined": round(((1/ev_eb)+roic)*100, 2),
                    "warning": "EY approximated from 1/EV-EBITDA."}
        return None

    ev = market_cap + debt - cash
    ey = sd(ebit, ev)
    if not ey: return None
    warning = None
    if info.get("financialCurrency") and info.get("currency") and info.get("financialCurrency") != info.get("currency"):
        warning = "Magic Formula may need FX adjustment."
    return {"ey": round(ey*100, 2), "roic": round(roic*100, 2),
            "combined": round((ey+roic)*100, 2), "warning": warning}

def insider_conviction(d):
    """
    Summarize insider buying vs selling over the last 6 months from
    Yahoo's Form-4-derived `insider_purchases` table.

    Informational only — NOT folded into the composite score. Insider
    data is noisy: routine sales and pre-scheduled 10b5-1 plans inflate
    'selling' without signalling a real loss of conviction.
    """
    ip = d.get("insider_purchases")
    try:
        if ip is None or getattr(ip, "empty", True):
            return None
        cols = list(ip.columns)
        if not cols:
            return None
        label_col = cols[0]
        shares_col = cols[1] if len(cols) > 1 else None
        trans_col = cols[2] if len(cols) > 2 else None

        buy_sh = sell_sh = net_sh = pct_net = None
        buy_tx = sell_tx = None
        for _, r in ip.iterrows():
            lbl = str(r[label_col]).strip().lower()
            sh = num(r[shares_col]) if shares_col else None
            tx = num(r[trans_col]) if trans_col else None
            if lbl.startswith("purchase"):
                buy_sh, buy_tx = sh, tx
            elif lbl.startswith("sale"):
                sell_sh, sell_tx = sh, tx
            elif "net shares" in lbl and "%" not in lbl:
                net_sh = sh
            elif lbl.startswith("% net"):
                pct_net = sh

        # Derive net if not directly given
        if net_sh is None and buy_sh is not None and sell_sh is not None:
            net_sh = buy_sh - sell_sh

        # Nothing usable
        if buy_sh is None and sell_sh is None and net_sh is None:
            return None

        if net_sh is not None and net_sh > 0:
            verdict = "Net buying"
        elif net_sh is not None and net_sh < 0:
            verdict = "Net selling"
        else:
            verdict = "Neutral / minimal activity"

        return {"buy_sh": buy_sh, "sell_sh": sell_sh, "net_sh": net_sh,
                "buy_tx": buy_tx, "sell_tx": sell_tx,
                "pct_net": pct_net, "verdict": verdict}
    except Exception:
        return None

def dcf_scenarios(d, m):
    """Bear/base/bull DCF with normalized FCF and beta-adjusted WACC.

    Abstains when free cash flow is negative. Growing a negative number for
    five years and discounting it produces a negative intrinsic value and an
    upside near -100%, which build_scores then read as "extremely expensive"
    and folded into the frameworks average as a 1.0. That is a category error:
    a cash-burning company is not the same thing as an overpriced one, and the
    burn is already accounted for elsewhere - by the cash-runway warning, by
    the FCF-quality and growth scores, and by Piotroski's FCF > 0 test. Scoring
    it here as well charged the same fact twice, against the one metric that
    could not measure it.

    Returns {"not_applicable": True, "reason": ...}, which carries no "base"
    key and so drops out of the frameworks average rather than dragging it.
    """
    fcf0 = m.get("fcf_normalized") or m.get("fcf_latest")
    shares = m.get("shares")
    price = m.get("price")
    if not fcf0 or not shares: return None
    if fcf0 < 0:
        return {"not_applicable": True,
                "fcf_used": fcf0,
                "reason": ("free cash flow is negative; a discounted cash flow "
                           "of a cash burn is not a valuation. The burn is "
                           "scored through cash runway, FCF quality and growth "
                           "instead.")}

    rules = srules(m.get("sector"))
    cyc = rules["cyc"]
    beta = m.get("beta") or 1.0
    wacc_adj = (beta - 1) * 0.02  # +/- 2% per beta point from 1.0

    if cyc:
        scenarios = [("bear", 0.03, 0.015, 0.105, 0.75),
                     ("base", 0.06, 0.025, 0.095, 0.90),
                     ("bull", 0.10, 0.030, 0.090, 1.00)]
    else:
        scenarios = [("bear", 0.04, 0.020, 0.105, 0.85),
                     ("base", 0.08, 0.025, 0.095, 1.00),
                     ("bull", 0.13, 0.030, 0.090, 1.10)]

    results = {}
    for name, g, tg, dr, fcf_adj in scenarios:
        dr += wacc_adj
        if dr <= tg: continue
        adj_fcf = fcf0 * fcf_adj
        fwd = [adj_fcf * (1+g)**yr for yr in range(1, 6)]
        tv = fwd[-1] * (1+tg) / (dr-tg)
        pv = sum(f/(1+dr)**i for i, f in enumerate(fwd, 1)) + tv/(1+dr)**5
        iv = sd(pv, shares)
        upside = sd((iv or 0) - price, price) if price else None
        results[name] = {"iv": round(iv, 2) if iv else None,
                         "upside": round(upside*100, 1) if upside is not None else None,
                         "growth": round(g*100, 1), "terminal_growth": round(tg*100, 1),
                         "discount_rate": round(dr*100, 1), "fcf_adjustment": fcf_adj}
    return results

# ─── SCORING ───────────────────────────────────────────────────────────────
def score(val, good, bad, higher=True):
    if val is None: return 5.0
    if higher:
        if val >= good: return 10.0
        if val <= bad:  return 1.0
        return 1 + 9 * (val - bad) / (good - bad)
    else:
        if val <= good: return 10.0
        if val >= bad:  return 1.0
        return 1 + 9 * (bad - val) / (bad - good)

def build_scores(m, pio, alt, gra, mag, dc):
    rules = srules(m.get("sector"))

    # Valuation: exclude PEG when extreme (renormalize weights)
    pe_s  = score(m.get("pe"),        rules["pe_g"],  rules["pe_b"], False)
    fpe_s = score(m.get("fwd_pe"),    rules["pe_g"],  rules["pe_b"], False)
    ev_s  = score(m.get("ev_ebitda"), rules["ev_g"],  rules["ev_b"], False)
    ps_s  = score(m.get("ps"),        rules["ps_g"],  rules["ps_b"], False)
    pb_s  = score(m.get("pb"),        rules["pb_g"],  rules["pb_b"], False)
    if m.get("peg_excluded") or m.get("peg") is None:
        val_s = pe_s*0.25 + fpe_s*0.20 + ev_s*0.25 + ps_s*0.15 + pb_s*0.15
    else:
        peg_s = score(m.get("peg"), rules["peg_g"], rules["peg_b"], False)
        val_s = pe_s*0.22 + fpe_s*0.18 + ev_s*0.22 + ps_s*0.13 + pb_s*0.15 + peg_s*0.10

    # Profitability
    gm_s   = score(m.get("gross_margin"), 0.45, 0.10, True)
    om_s   = score(m.get("op_margin"),    0.22, 0.00, True)
    roe_s  = score(m.get("roe"),  rules["roe_g"],  0.00, True)
    roa_s  = score(m.get("roa"),  0.08, 0.00, True)
    roic_s = score(m.get("roic"), rules["roic_g"], 0.00, True)
    prof_s = (gm_s + om_s + roe_s + roa_s + roic_s) / 5

    # Growth
    cap = rules["gcap"]
    rg_s = score(m.get("rev_growth"), cap, -0.05, True)
    eg_s = score(m.get("eps_growth"), cap, -0.10, True)
    fg_s = score(m.get("fcf_growth"), cap, -0.10, True)
    grow_s = (rg_s + eg_s + fg_s) / 3

    # Health (cap interest coverage at 30x for scoring purposes only)
    de_s = score(m.get("debt_eq"),    0.30, 2.50, False)
    cr_s = score(m.get("curr_ratio"), 2.50, 1.00, True)
    qr_s = score(m.get("quick_ratio"),1.50, 0.50, True)
    ic_capped = min(m["int_coverage"], 30) if m.get("int_coverage") else None
    ic_s = score(ic_capped, 10.0, 1.50, True)
    fq_s = score(m.get("fcf_quality"), 1.2, 0.30, True)
    health_s = (de_s + cr_s + qr_s + ic_s + fq_s) / 5

    # Momentum
    mom_s = score(m.get("pos_52w"), 0.80, 0.20, True)

    # Frameworks
    fw = []
    if pio: fw.append((pio["score"]/9) * 10)
    if alt: fw.append(score(alt["score"], 3.0, 1.8, True))
    if gra and gra["mos"] is not None: fw.append(score(gra["mos"], 40, -30, True))
    if mag: fw.append(score(mag["combined"], 25, 5, True))
    if dc and dc.get("base") and dc["base"].get("upside") is not None:
        fw.append(score(dc["base"]["upside"], 35, -20, True))
    fw_s = sum(fw)/len(fw) if fw else 5.0

    w = rules["weights"]
    composite = val_s*w[0] + prof_s*w[1] + grow_s*w[2] + health_s*w[3] + mom_s*w[4] + fw_s*w[5]

    # Graduated valuation penalty: subtract proportional amount when val_s < 6
    if val_s < 6.0:
        composite -= (6.0 - val_s) * 0.2

    composite = max(0, min(10, composite))

    dims = {"Valuation": round(val_s, 2), "Profitability": round(prof_s, 2),
            "Growth": round(grow_s, 2), "Health": round(health_s, 2),
            "Momentum": round(mom_s, 2), "Frameworks": round(fw_s, 2)}

    rating = rate(composite, dims)
    return {"composite": round(composite, 2), "dims": dims, "rating": rating}

# The inputs each dimension's score is actually built from, matching
# build_scores() line for line. Frameworks are counted separately below,
# because their availability is a framework returning something at all.
COVERAGE_INPUTS = {
    "Valuation":     ("pe", "fwd_pe", "ev_ebitda", "ps", "pb"),
    "Profitability": ("gross_margin", "op_margin", "roe", "roa", "roic"),
    "Growth":        ("rev_growth", "eps_growth", "fcf_growth"),
    "Health":        ("debt_eq", "curr_ratio", "quick_ratio", "int_coverage",
                      "fcf_quality"),
    "Momentum":      ("pos_52w",),
}

# Below this share of inputs, a composite is mostly score()'s neutral default
# rather than a reading of the company.
LOW_COVERAGE = 0.60


def data_coverage(m, pio=None, alt=None, gra=None, mag=None, dc=None):
    """What share of the scoring inputs this ticker actually had.

    score() returns a neutral 5.0 for anything it cannot read. That is the right
    default - it refuses to reward or punish a company for Yahoo's coverage of
    it - but it has a consequence nothing was recording: a ticker with almost no
    data lands near 5.0 and reads as a considered HOLD / WATCHLIST rather than
    as an absence of information. A 5.4 built from four inputs and a 5.4 built
    from twenty-four are not the same claim, and until now the file could not
    tell them apart.

    Informational only - it does not alter the composite, the same discipline
    insider conviction, cash runway and the value screen follow. It does earn a
    risk flag in position_guidance(), which is how this codebase says "size this
    smaller" without disturbing the v3/v4 scoring calibration.

    Writes m["data_coverage"] and m["coverage_detail"], and returns the detail.
    """
    by_dimension, available, total = {}, 0, 0

    for dimension, keys in COVERAGE_INPUTS.items():
        got = sum(1 for key in keys if num(m.get(key)) is not None)
        # PEG only counts where it actually participates: build_scores drops it
        # and renormalizes when it is missing or flagged unreliable.
        if dimension == "Valuation" and m.get("peg") is not None and not m.get("peg_excluded"):
            got, keys = got + 1, keys + ("peg",)
        by_dimension[dimension] = {"available": got, "total": len(keys),
                                   "share": round(got / len(keys), 4)}
        available += got
        total += len(keys)

    # Frameworks: the same five build_scores() averages over.
    frameworks = {
        "piotroski": bool(pio),
        "altman_z": bool(alt),
        "graham": bool(gra and gra.get("mos") is not None),
        "magic_formula": bool(mag),
        "dcf": bool(dc and dc.get("base") and dc["base"].get("upside") is not None),
    }
    got = sum(1 for present in frameworks.values() if present)
    by_dimension["Frameworks"] = {"available": got, "total": len(frameworks),
                                  "share": round(got / len(frameworks), 4),
                                  "which": frameworks}
    available += got
    total += len(frameworks)

    overall = round(available / total, 4) if total else 0.0
    detail = {"overall": overall, "available": available, "total": total,
              "low_threshold": LOW_COVERAGE, "by_dimension": by_dimension}

    m["data_coverage"] = overall
    m["coverage_detail"] = detail
    if overall < LOW_COVERAGE:
        m.setdefault("warnings", []).append(
            f"Thin data: {available} of {total} scoring inputs were available "
            f"({overall*100:.0f}%). Missing inputs score a neutral 5.0, so this "
            f"composite is closer to a default than to a reading.")
    return detail


def rate(comp, dims):
    if comp >= 8.0 and dims.get("Valuation", 10) < 6.0:
        return "QUALITY BUY, BUT VALUATION STRETCHED"
    if comp >= 8.5: return "HIGH-CONVICTION BUY"
    if comp >= 7.0: return "BUY / ACCUMULATE"
    if comp >= 5.5: return "HOLD / WATCHLIST"
    if comp >= 4.0: return "SPECULATIVE / WEAK"
    return "AVOID"

def position_guidance(m, sc, ins):
    comp = sc["composite"]
    flags = []
    if (m.get("beta") or 1.0) > 1.3: flags.append("high beta")
    if m.get("sector") in ("Basic Materials", "Energy"): flags.append("commodity/cyclical")
    if m.get("debt_eq") and m["debt_eq"] > 1.0: flags.append("elevated leverage")
    if m.get("pos_52w") and m["pos_52w"] > 0.85: flags.append("near 52-week high")
    if m.get("peg_excluded"): flags.append("unreliable PEG")
    if sc["dims"].get("Valuation", 10) < 5.5: flags.append("valuation not cheap")
    if m.get("runway_months") is not None and m["runway_months"] < 12:
        flags.append("short cash runway")
    if ins and ins.get("verdict") == "Net selling":
        flags.append("insider net selling")
    # A composite mostly made of score()'s neutral default is not a conviction,
    # and the top size band is reserved for names with at most one flag - which
    # is exactly the right place for "we barely have the data on this one".
    coverage = num(m.get("data_coverage"))
    if coverage is not None and coverage < LOW_COVERAGE:
        flags.append(f"thin data ({coverage*100:.0f}% of inputs)")

    if comp >= 8.5 and len(flags) <= 1:
        guide = "Core: 3%–5%; up to 8% with diversification."
    elif comp >= 7.0:
        guide = "Accumulate: 2%–4%; add on weakness."
    elif comp >= 5.5:
        guide = "Watchlist: 0%–2% only."
    else:
        guide = "Avoid; research only."
    return {"guide": guide, "risk_flags": flags}

# ─── REPORT ────────────────────────────────────────────────────────────────
def print_report(ticker, m, sc, pio, alt, gra, mag, dc, ins, pos, vs):
    W = 62
    comp = sc["composite"]
    dims = sc["dims"]

    def rule(ch="═"): print("  " + ch * W)
    def h(t): print(f"  {B}{t}{X}")
    def row(label, val, fmt=".2f", suffix=""):
        v = f"{format(val, fmt)}{suffix}" if val is not None else "N/A"
        print(f"  {label:<26}  {v}")

    print()
    rule()
    print(f"  {B}{C}{m['name']} ({ticker.upper()}){X}")
    print(f"  {m['sector']}  ·  {m['industry']}")
    px = f"${m['price']:.2f} {m.get('quote_currency') or ''}" if m.get("price") else "N/A"
    print(f"  {px}  ·  Cap: {fmt_money(m.get('mktcap'))}  ·  Beta: {m.get('beta') or 'N/A'}")
    if m.get("financial_currency") and m.get("quote_currency") and m["financial_currency"] != m["quote_currency"]:
        print(f"  Quote: {m['quote_currency']}  ·  Financials: {m['financial_currency']}")
    rule()

    print(f"\n  {B}COMPOSITE SCORE{X}   {colour(comp)} / 10   {bar(comp, 18)}")
    print(f"  Rating: {B}{sc['rating']}{X}")

    has_warn = m.get("warnings") or (alt and alt.get("warning")) or (mag and mag.get("warning"))
    if has_warn:
        print(); rule("─"); h("WARNINGS / DATA QUALITY"); rule("─")
        for w in m.get("warnings", []):
            print(f"  {Y}⚠{X}  {w}")
        if alt and alt.get("warning"): print(f"  {Y}⚠{X}  {alt['warning']}")
        if mag and mag.get("warning"): print(f"  {Y}⚠{X}  {mag['warning']}")

    print(); rule("─"); h("DIMENSION BREAKDOWN"); rule("─")
    for dim, val in dims.items():
        print(f"  {dim:<14}  {bar(val, 12)}  {colour(val)}")

    print(); rule("─"); h("NAMED FRAMEWORKS"); rule("─")
    if pio:
        sigs_yes = [k for k, v in pio["signals"].items() if v]
        sigs_no  = [k for k, v in pio["signals"].items() if not v]
        print(f"  Piotroski   {pio['score']}/9 — {pio['label']}")
        if sigs_yes: print(f"    {G}✓{X}  {' | '.join(sigs_yes)}")
        if sigs_no:  print(f"    {R}✗{X}  {' | '.join(sigs_no)}")
    if alt:
        zc = G if alt["zone"] == "Safe" else (Y if alt["zone"] == "Grey" else R)
        print(f"  Altman Z    {alt['score']}  [{zc}{alt['zone']}{X}]")
    if gra:
        arrow = "↑" if (gra["mos"] or 0) > 0 else "↓"
        print(f"  Graham      ${gra['graham']}  {arrow}  {gra['mos']}% MoS  (px ${gra['price']})")
    if mag:
        print(f"  Magic Formula  EY {mag['ey']}%  ·  ROIC {mag['roic']}%  ·  Combined {mag['combined']}%")
    if dc and dc.get("not_applicable"):
        print(f"  DCF Scenarios:  not applicable - {dc.get('reason')}")
    elif dc:
        print("  DCF Scenarios:")
        for name in ("bear", "base", "bull"):
            s = dc.get(name)
            if s:
                sign = "+" if (s.get("upside") or 0) > 0 else ""
                print(f"    {name.capitalize():<5} ${s['iv']:<8} ({sign}{s['upside']}%)  g={s['growth']}% WACC={s['discount_rate']}%")

    # ─── INSIDER ACTIVITY (v5) ─────────────────────────────────────────────
    print(); rule("─"); h("INSIDER ACTIVITY  (last 6 months)"); rule("─")
    if ins:
        vc = G if ins["verdict"] == "Net buying" else (R if ins["verdict"] == "Net selling" else Y)
        print(f"  Verdict: {vc}{ins['verdict']}{X}")
        if ins.get("buy_sh") is not None:
            tx = f" ({int(ins['buy_tx'])} trans)" if ins.get("buy_tx") else ""
            print(f"  Bought:  {fmt_sh(ins['buy_sh'])}{tx}")
        if ins.get("sell_sh") is not None:
            tx = f" ({int(ins['sell_tx'])} trans)" if ins.get("sell_tx") else ""
            print(f"  Sold:    {fmt_sh(ins['sell_sh'])}{tx}")
        if ins.get("net_sh") is not None:
            print(f"  Net:     {fmt_sh(ins['net_sh'])}")
        print(f"  {Y}Note: insider data is noisy — routine/scheduled (10b5-1){X}")
        print(f"  {Y}sales can show 'selling' without loss of conviction.{X}")
    else:
        print("  No insider transaction data available for this ticker.")

    # ─── CASH RUNWAY (v5) — shown only for cash-burning companies ──────────
    if m.get("annual_burn"):
        print(); rule("─"); h("CASH RUNWAY  (cash-burning company)"); rule("─")
        print(f"  Cash + ST investments   {fmt_money(m.get('cash_position'))}")
        print(f"  Annual cash burn        {fmt_money(-m['annual_burn'])}")
        if m.get("runway_months") is not None:
            rc = R if m["runway_months"] < 12 else (Y if m["runway_months"] < 24 else G)
            yrs = m["runway_years"]
            print(f"  Estimated runway        {rc}~{m['runway_months']:.0f} months ({yrs:.1f} yrs){X}")
        else:
            print("  Estimated runway        N/A (cash position unavailable)")
        print(f"  {Y}Burn = larger of negative operating CF / negative FCF.{X}")

    print(); rule("─"); h("POSITION GUIDANCE"); rule("─")
    print(f"  {pos['guide']}")
    if pos["risk_flags"]:
        print(f"  Risk flags: {', '.join(pos['risk_flags'])}")

    # ─── VALUE SCREEN (v5.3) — informational 52-week-low flag ──────────────
    if vs and vs.get("triggered"):
        print(); rule("─"); h("VALUE SCREEN"); rule("─")
        vc = G if "intact" in vs["verdict"] else Y
        print(f"  {vc}{vs['verdict']}{X}")

    print(); rule("─"); h("KEY METRICS"); rule("─")
    print(f"  {B}— Valuation —{X}")
    row("P/E (trailing)",   m.get("pe"),        ".1f")
    row("P/E (forward)",    m.get("fwd_pe"),    ".1f")
    row("PEG",              m.get("peg"),       ".2f")
    row("EV/EBITDA",        m.get("ev_ebitda"), ".1f")
    row("P/S",              m.get("ps"),        ".2f")
    row("P/B",              m.get("pb"),        ".2f")
    print(f"  {B}— Profitability —{X}")
    row("Gross Margin",     m.get("gross_margin"),".1%")
    row("Operating Margin", m.get("op_margin"),  ".1%")
    row("Net Margin",       m.get("net_margin"), ".1%")
    row("ROE",              m.get("roe"),         ".1%")
    row("ROA",              m.get("roa"),         ".1%")
    row("ROIC",             m.get("roic"),        ".1%")
    # R&D intensity — show for R&D-relevant sectors, or whenever a value exists
    if m.get("rnd_intensity") is not None or m.get("sector") in RND_SECTORS:
        print(f"  {B}— R&D —{X}")
        row("R&D Spend",      m.get("rnd"),           ",.0f")
        row("R&D Intensity",  m.get("rnd_intensity"), ".1%")
    print(f"  {B}— Growth (raw) —{X}")
    row("Revenue Growth",   m.get("rev_growth_raw"),".1%")
    row("EPS Growth",       m.get("eps_growth_raw"),".1%")
    row("FCF Growth",       m.get("fcf_growth_raw"),".1%")
    if m.get("fcf_normalized") != m.get("fcf_latest"):
        row("FCF (normalized)", m.get("fcf_normalized"), ",.0f")
    print(f"  {B}— Health —{X}")
    row("Debt/Equity",      m.get("debt_eq"),     ".2f")
    row("Current Ratio",    m.get("curr_ratio"),  ".2f")
    row("Quick Ratio",      m.get("quick_ratio"), ".2f")
    row("Interest Coverage",m.get("int_coverage"),".1f")
    row("FCF/Net Income",   m.get("fcf_quality"), ".2f")
    print(f"  {B}— Momentum —{X}")
    if m.get("52w_high") is not None: row("52W High",          m["52w_high"], ".2f", " $")
    if m.get("52w_low") is not None:  row("52W Low",           m["52w_low"],  ".2f", " $")
    if m.get("pos_52w") is not None:  row("Position in Range", m["pos_52w"],  ".1%")

    if m.get("notes"):
        print(); rule("─"); h("MODEL NOTES"); rule("─")
        for n in m["notes"]: print(f"  • {n}")

    print(); rule()
    print(f"  {Y}⚠  For informational use only. Not financial advice.{X}")
    rule(); print()

def print_diversification_summary(results):
    """Portfolio-mode sector diversification summary (v5.3). Only shown when
    2+ tickers were evaluated in a single batch run."""
    W = 62

    def rule(ch="═"): print("  " + ch * W)
    def h(t): print(f"  {B}{t}{X}")

    total = len(results)
    by_sector = {}
    for r in results:
        by_sector.setdefault(r["sector"] or "N/A", []).append(r)

    # Sort sectors by count descending
    ordered = sorted(by_sector.items(), key=lambda kv: len(kv[1]), reverse=True)

    print()
    rule()
    print(f"  {B}{C}PORTFOLIO DIVERSIFICATION SUMMARY{X}")
    print(f"  {total} tickers evaluated  ·  {len(by_sector)} sector(s)")
    rule()

    for sector, items in ordered:
        cnt = len(items)
        pct = cnt / total * 100
        print()
        print(f"  {B}{sector}{X}   {cnt}/{total}  ({pct:.0f}%)")
        for it in items:
            print(f"    {it['ticker']:<8}  {colour(it['composite'])} / 10")

    print(); rule("─")
    warned = False
    for sector, items in ordered:
        pct = len(items) / total * 100
        if pct > 40:
            print(f"  {Y}Concentration risk: {pct:.0f}% of candidates are in "
                  f"{sector}. Consider diversifying across sectors.{X}")
            warned = True
    if not warned:
        print(f"  {G}Sector spread looks reasonably diversified.{X}")
    rule(); print()

# ═══════════════════════════════════════════════════════════════════════════
# BATCH MODE  (v5.4)
#
# Everything below this line is additive. The single-ticker path above is
# untouched: it still calls get_data() and yfinance directly, and
# `py stock_evaluator.py TICKER` behaves exactly as it did.
#
# Batch mode instead routes every fetch through market_data.py, so a scan of
# a few thousand candidates reuses one session, hits the disk cache, and
# retries with backoff. The cached fetches rebuild the same pandas frames
# get_data() returns, so calc_metrics(), piotroski(), altman_z(),
# magic_formula() and dcf_scenarios() run unchanged on cached data.
# ═══════════════════════════════════════════════════════════════════════════

import json
from pathlib import Path

# market_data.py sits next to this script on the working machine
# (C:\Users\joey\stocks\), and in stocks/ when this repo is checked out.
def _import_market_data():
    here = Path(__file__).resolve().parent
    for candidate in (here, here.parent / "stocks", here.parent):
        if (candidate / "market_data.py").exists():
            if str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
            break
    import market_data
    return market_data

try:
    md = _import_market_data()
except Exception as _md_err:      # single-ticker mode must not care
    md = None
    _MD_IMPORT_ERROR = _md_err
else:
    _MD_IMPORT_ERROR = None

# Liquidity gate defaults: a 3% position that eats more than 1% of a day's
# dollar volume is worth flagging, not excluding.
DEFAULT_POSITION_PCT = 0.03
DEFAULT_MAX_ADV_PCT  = 0.01

# "Bottom of the 52-week range" for the divergence check.
DIVERGENCE_LOW_POS = 0.25

# How much ROA has to move across the whole window before the direction counts
# as a direction at all. ROA is a ratio, so this is 0.5 percentage points of
# return on assets - below that the series is flat, not improving or declining.
ROA_FLAT_BAND = 0.005

INSUFFICIENT = "insufficient history"


def data_dir():
    """<stocks>\\data — the same directory universe_screen.py writes to."""
    override = os.environ.get("STOCKS_DATA_DIR")
    if override:
        return Path(override)
    base = Path(md.BASE_DIR) if md is not None else Path(__file__).resolve().parent
    return base / "data"


def _require_market_data():
    if md is None:
        raise RuntimeError(
            "batch mode needs market_data.py next to this script "
            f"(import failed: {_MD_IMPORT_ERROR})")


# ─── CACHED FETCH (frames in, frames out) ──────────────────────────────────
def _frame_to_payload(df):
    """DataFrame -> JSON-safe dict. None for an empty/missing statement."""
    if df is None or getattr(df, "empty", True):
        return None
    columns = [c.isoformat() if hasattr(c, "isoformat") else str(c) for c in df.columns]
    rows = []
    for _, row in df.iterrows():
        rows.append([num(v) for v in row.tolist()])
    return {"index": [str(i) for i in df.index], "columns": columns, "data": rows}


def _payload_to_frame(payload):
    """Rebuild the frame get_data() would have handed to calc_metrics().

    Columns come back as Timestamps so the stale-statement check
    (fin.columns[0].to_pydatetime()) keeps working.
    """
    if not payload:
        return None
    import pandas as pd
    columns = pd.to_datetime(payload["columns"], errors="coerce")
    if columns.isna().all():
        columns = payload["columns"]
    return pd.DataFrame(payload["data"], index=payload["index"], columns=columns)


def _history_frame(rows):
    """market_data price rows -> the OHLCV frame calc_metrics() expects."""
    if not rows:
        return None
    import pandas as pd
    index = pd.to_datetime([r.get("date") for r in rows], errors="coerce", utc=True)
    return pd.DataFrame({
        "Open":   [r.get("open") for r in rows],
        "High":   [r.get("high") for r in rows],
        "Low":    [r.get("low") for r in rows],
        "Close":  [r.get("close") for r in rows],
        "Volume": [r.get("volume") for r in rows],
    }, index=index)


def fetch_statements(ticker, ttl=None, force_refresh=False):
    """Annual income statement / balance sheet / cash flow, cached 7 days.

    One cache entry per ticker holding all three, so a ticker costs one
    request instead of three.
    """
    _require_market_data()
    ttl = md.TTL_FINANCIALS if ttl is None else ttl

    def _pull():
        t = md.get_ticker(ticker)
        payload = {"fin": _frame_to_payload(t.financials),
                   "bal": _frame_to_payload(t.balance_sheet),
                   "cf":  _frame_to_payload(t.cashflow)}
        if all(v is None for v in payload.values()):
            raise ValueError(f"no annual statements returned for {ticker}")
        return payload

    return md.cached_fetch(f"{ticker}_statements",
                           lambda: md.fetch_with_backoff(_pull),
                           ttl, cache_type="financials", force_refresh=force_refresh)


def fetch_insider(ticker, ttl=None, force_refresh=False):
    """Form-4 summary table. Optional and noisy - failure is not an error."""
    _require_market_data()
    ttl = md.TTL_FINANCIALS if ttl is None else ttl

    def _pull():
        table = md.get_ticker(ticker).insider_purchases
        payload = _frame_to_payload(table)
        if payload is None:
            raise ValueError(f"no insider table for {ticker}")
        return payload

    return md.cached_fetch(f"{ticker}_insider",
                           lambda: md.fetch_with_backoff(_pull, max_retries=2),
                           ttl, cache_type="financials", force_refresh=force_refresh)


def get_data_cached(ticker, include_insider=False, force_refresh=False):
    """get_data() for batch mode: same dict shape, fetched through market_data.

    info carries the live price and the 52-week range, so it is cached on the
    price TTL (1 day) rather than the fundamentals TTL (7 days) - that is why
    financials_as_of and price_as_of can differ.

    Returns (data, None) or (None, reason). Never raises for a fetch failure.
    """
    _require_market_data()

    info = md.get_info(ticker, ttl=md.TTL_PRICE, force_refresh=force_refresh)
    if not isinstance(info, dict) or not info:
        return None, "info unavailable after retries"
    if info.get("quoteType") is None or info.get("shortName") is None:
        return None, "ticker not found (no quoteType/shortName)"

    statements = fetch_statements(ticker, force_refresh=force_refresh)
    if not statements:
        return None, "annual statements unavailable after retries"

    insider = None
    if include_insider:
        payload = fetch_insider(ticker, force_refresh=force_refresh)
        insider = _payload_to_frame(payload) if payload else None

    history = _history_frame(md.get_price_history(ticker, period="1y"))

    return {
        "ticker": ticker.upper(),
        "info": info,
        "fin": _payload_to_frame(statements.get("fin")),
        "bal": _payload_to_frame(statements.get("bal")),
        "cf":  _payload_to_frame(statements.get("cf")),
        "hist": history,
        "insider_purchases": insider,
        # When each input was actually fetched, read off the cache entries.
        "financials_as_of": md.cache_timestamp(f"{ticker}_statements", "financials"),
        "price_as_of": md.cache_timestamp(f"{ticker}_info", "financials"),
    }, None


# ─── MULTI-YEAR TRENDS (v5.4) ──────────────────────────────────────────────
def _n_periods(df):
    try:
        return 0 if df is None or df.empty else len(df.columns)
    except Exception:
        return 0


def trend_analysis(d):
    """Multi-year view of the signals Piotroski only checks year-over-year.

    Piotroski asks "is ROA better than last year". This asks "has ROA held up
    across every year Yahoo returned" - typically four, sometimes two, and for
    a recent IPO sometimes one. Below two years nothing is computed: a
    one-point trend is not a trend, and reporting one would be misleading.

    Returns the four fields plus the underlying series, oldest year first.
    """
    fin, bal, cf = d.get("fin"), d.get("bal"), d.get("cf")

    # Widest statement wins: rv() returns None past a narrower frame's end.
    # Yahoo pads the oldest column with an all-empty period, so the column
    # count is not the number of years of data - see the trim below.
    counts = [n for n in (_n_periods(fin), _n_periods(bal), _n_periods(cf)) if n]
    periods = max(counts) if counts else 0

    # yfinance returns newest-first; reverse everything to oldest-first.
    roa_series, fcf_series, debt_series = [], [], []
    for i in reversed(range(periods)):
        ni = rv(fin, "Net Income", i)
        ta = rv(bal, "Total Assets", i)
        roa_series.append(sd(ni, ta))

        fcf = rv(cf, "Free Cash Flow", i)
        if fcf is None:
            ocf = rf(cf, ["Operating Cash Flow", "Total Cash From Operating Activities",
                          "Cash Flow From Continuing Operating Activities"], i)
            capex = rf(cf, ["Capital Expenditure", "Capital Expenditures"], i)
            if ocf is not None and capex is not None:
                fcf = ocf + capex        # capex is reported negative
        fcf_series.append(fcf)

        debt_series.append(rf(bal, ["Total Debt", "Long Term Debt"], i))

    # Drop periods that carry no data at all - an empty padding column is not
    # a year of history, and counting it would understate every "N of M".
    keep = [i for i in range(periods)
            if any(s[i] is not None for s in (roa_series, fcf_series, debt_series))]
    roa_series = [roa_series[i] for i in keep]
    fcf_series = [fcf_series[i] for i in keep]
    debt_series = [debt_series[i] for i in keep]
    years = len(keep)

    if years < 2:
        return {
            "trend_years_available": years,
            "roa_trend_consistent": INSUFFICIENT,
            "roa_trend": INSUFFICIENT,
            "fcf_positive_years": INSUFFICIENT,
            "debt_trend": INSUFFICIENT,
            "trend_detail": {"roa_by_year": [], "fcf_by_year": [], "debt_by_year": [],
                             "note": "fewer than 2 annual periods with data"},
        }

    roa_valid = [v for v in roa_series if v is not None]
    fcf_valid = [v for v in fcf_series if v is not None]
    debt_valid = [v for v in debt_series if v is not None]

    # Two readings of the same series, and the difference matters.
    #
    # roa_trend_consistent is the strict one: non-decreasing in EVERY year. It
    # is kept because it is a real, precise statement, but it must not be the
    # one anything judges on, because it is not comparable between names. It
    # demands more the more history a company has - one down year in four makes
    # it False, while a two-year-old listing only has to clear a single step -
    # so the same business looks worse purely for having reported longer.
    # position_sizer's exit review already says as much in a comment and works
    # around it; divergence_pattern did not, and voted on it.
    #
    # roa_trend is the graded one: which direction the series actually went,
    # from the share of year-over-year steps that improved and the overall
    # first-to-last change. One down year inside a rising series reads
    # "improving", which is what it is.
    roa_consistent = INSUFFICIENT
    roa_trend = INSUFFICIENT
    roa_shape = {}
    if len(roa_valid) >= 2:
        roa_consistent = all(b >= a for a, b in zip(roa_valid, roa_valid[1:]))
        steps = list(zip(roa_valid, roa_valid[1:]))
        up = sum(1 for a, b in steps if b > a)
        down = sum(1 for a, b in steps if b < a)
        change = roa_valid[-1] - roa_valid[0]
        roa_shape = {"steps": len(steps), "steps_up": up, "steps_down": down,
                     "first": round(roa_valid[0], 6), "last": round(roa_valid[-1], 6),
                     "change": round(change, 6), "flat_band": ROA_FLAT_BAND}
        if abs(change) < ROA_FLAT_BAND:
            roa_trend = "flat"
        elif change > 0 and up >= down:
            roa_trend = "improving"
        elif change < 0 and down >= up:
            roa_trend = "deteriorating"
        else:
            # The endpoints and the steps disagree - a spike or a trough is
            # doing the work. Neither direction is a fair summary.
            roa_trend = "mixed"

    fcf_positive = len([v for v in fcf_valid if v > 0]) if fcf_valid else INSUFFICIENT

    if len(debt_valid) >= 2:
        first, last = debt_valid[0], debt_valid[-1]
        change = sd(last - first, abs(first))
        if change is None:
            debt_trend = "flat" if last == first else "unavailable"
        elif change < -0.05:
            debt_trend = "decreasing"
        elif change > 0.05:
            debt_trend = "increasing"
        else:
            debt_trend = "flat"
    else:
        debt_trend = INSUFFICIENT

    def _round(values):
        return [None if v is None else round(v, 6) for v in values]

    return {
        "trend_years_available": years,
        "roa_trend_consistent": roa_consistent,
        "roa_trend": roa_trend,
        "fcf_positive_years": fcf_positive,
        "debt_trend": debt_trend,
        "trend_detail": {
            "roa_by_year": _round(roa_series),
            "fcf_by_year": _round(fcf_series),
            "debt_by_year": _round(debt_series),
            # Per-series counts: a series can be shorter than the window when
            # Yahoo omits a line item, so "N of M" is stated per series.
            "roa_shape": roa_shape,
            "roa_years_available": len(roa_valid),
            "fcf_years_available": len(fcf_valid),
            "debt_years_available": len(debt_valid),
            "order": "oldest to newest",
        },
    }


# ─── LIQUIDITY GATE (v5.4) ─────────────────────────────────────────────────
def liquidity_check(m, account_size=None,
                    position_pct=DEFAULT_POSITION_PCT,
                    max_adv_pct=DEFAULT_MAX_ADV_PCT,
                    account_currency=None,
                    avg_volume_hint=None,
                    fx_rate=None):
    """How much of a normal trading day one position would be.

    A flag, never an exclusion: thin names stay in the scan and are marked so
    the reason is visible downstream instead of silently disappearing.

    The two sides of this ratio are quoted in different currencies. Dollar
    volume is price x shares in the STOCK's currency; the position is a share
    of the ACCOUNT, in the account's currency. Dividing one by the other
    unconverted is a real error, not a rounding one: a TSX name's volume in CAD
    measured against a USD account overstated its liquidity by the whole
    exchange rate, so thin Canadian names read as tradeable in a US account and
    the flag they should have raised never fired.

    `fx_rate` converts the quote currency into the account currency. When it is
    not supplied and the two differ, the comparison falls back to the old
    unconverted one and says so rather than inventing a rate of 1.0.
    """
    result = {"evaluated": False, "account_size": account_size,
              "position_pct": position_pct, "max_adv_pct": max_adv_pct}

    if not account_size or account_size <= 0:
        result["note"] = "no account size supplied; liquidity not evaluated"
        return False, result

    price = num(m.get("price"))
    volume = num(m.get("avg_volume")) or num(avg_volume_hint)
    if not price or not volume:
        result["note"] = "price or average volume unavailable"
        return False, result

    quote_currency = m.get("quote_currency")
    adv_value = price * volume                       # in the quote currency
    position_value = account_size * position_pct     # in the account currency

    rate = num(fx_rate)
    same_currency = bool(account_currency and quote_currency
                         and str(account_currency).upper() == str(quote_currency).upper())
    if same_currency:
        rate = 1.0
    adv_in_account = adv_value if rate is None else adv_value * rate

    share = sd(position_value, adv_in_account)
    if share is None:
        result["note"] = "average daily dollar volume is zero"
        return False, result

    result.update({
        "evaluated": True,
        "avg_daily_dollar_volume": round(adv_value, 2),
        "avg_daily_dollar_volume_account": round(adv_in_account, 2),
        "position_value": round(position_value, 2),
        "position_pct_of_adv": round(share, 6),
        "quote_currency": quote_currency,
        "account_currency": account_currency,
        "fx_rate": rate,
        "fx_adjusted": bool(rate is not None and not same_currency),
    })
    if not same_currency and account_currency and quote_currency:
        if rate is None:
            result["fx_note"] = (f"dollar volume in {quote_currency}, account in "
                                 f"{account_currency}; no rate available, so this "
                                 f"comparison is NOT FX-adjusted")
        else:
            result["fx_note"] = (f"dollar volume converted {quote_currency}->"
                                 f"{account_currency} at {rate:.4f}")

    return bool(share > max_adv_pct), result


# ─── PRICE / FUNDAMENTALS DIVERGENCE (v5.4) ────────────────────────────────
def divergence_pattern(m, trend, low_pos=DIVERGENCE_LOW_POS):
    """Is a beaten-down price contradicted or confirmed by the trend data?

    Only meaningful near the bottom of the 52-week range. There the question
    is whether the multi-year fundamentals agree with the price:

      price_disconnect       - price low, nothing deteriorating (out of favour)
      trend_confirms_decline - price low and the trend is rolling over (trap)
      neutral                - mixed signals, or price is not near its low
    """
    pos = num(m.get("pos_52w"))
    detail = {"pos_52w": pos, "low_threshold": low_pos, "deterioration": []}

    if pos is None:
        detail["reason"] = "52-week position unavailable"
        return "neutral", detail
    if pos > low_pos:
        detail["reason"] = "price is not in the bottom of its 52-week range"
        return "neutral", detail

    deterioration = detail["deterioration"]
    holding_up = detail["holding_up"] = []

    # Signals are grouped into four INDEPENDENT categories, and the trap
    # threshold counts categories rather than signals.
    #
    # It used to count signals, with revenue-declining and net-income-declining
    # as two of the five. Those are not two findings, they are one bad year
    # seen twice - revenue falls and profit falls with it - so a company whose
    # only problem was a single soft year hit the two-signal threshold on its
    # own and was labelled a value trap, which then cost it 35-50% of its
    # position size through the conviction modifier. That is precisely the
    # fundamentals-intact/price-depressed case the pipeline exists to find.
    #
    # Grouped, one soft year is one category and reads neutral; a soft year
    # ALONGSIDE rising debt, or thin cash generation, still reads as a trap.
    categories = {
        "profitability": None,   # multi-year ROA direction
        "cash_generation": None, # how often FCF was positive
        "balance_sheet": None,   # where debt went over the window
        "latest_year": None,     # revenue / net income in the most recent year
    }

    roa_trend = trend.get("roa_trend")
    if roa_trend == "deteriorating":
        categories["profitability"] = False
        deterioration.append("ROA declining across available years")
    elif roa_trend in ("improving", "flat"):
        categories["profitability"] = True
        holding_up.append(f"ROA {roa_trend} across available years")

    if isinstance(trend.get("fcf_positive_years"), int):
        fcf_years = (trend.get("trend_detail") or {}).get("fcf_years_available") or 0
        if fcf_years and trend["fcf_positive_years"] * 2 <= fcf_years:
            categories["cash_generation"] = False
            deterioration.append(
                f"FCF positive in only {trend['fcf_positive_years']} of {fcf_years} years")
        elif fcf_years:
            categories["cash_generation"] = True
            holding_up.append(
                f"FCF positive in {trend['fcf_positive_years']} of {fcf_years} years")

    if trend.get("debt_trend") == "increasing":
        categories["balance_sheet"] = False
        deterioration.append("debt rising over the full window")
    elif trend.get("debt_trend") in ("decreasing", "flat"):
        categories["balance_sheet"] = True
        holding_up.append(f"debt {trend['debt_trend']} over the full window")

    # One category for the latest year, however many of its lines moved.
    rev_growth = num(m.get("rev_growth_raw"))
    ni_growth = num(m.get("ni_growth"))
    latest = [(label, value) for label, value in
              (("revenue", rev_growth), ("net income", ni_growth)) if value is not None]
    if latest:
        falling = [label for label, value in latest if value < 0]
        rising = [label for label, value in latest if value >= 0]
        if falling:
            categories["latest_year"] = False
            deterioration.append(f"{' and '.join(falling)} declining year over year")
            if rising:
                holding_up.append(f"{' and '.join(rising)} still growing year over year")
        else:
            categories["latest_year"] = True
            holding_up.append(f"{' and '.join(rising)} growing year over year")

    failing = sorted(k for k, v in categories.items() if v is False)
    passing = sorted(k for k, v in categories.items() if v is True)
    detail["categories"] = categories
    detail["deteriorating_categories"] = failing
    detail["holding_up_categories"] = passing

    if len(failing) >= 2:
        detail["reason"] = (f"{len(failing)} independent categories deteriorating "
                            f"({', '.join(failing)})")
        return "trend_confirms_decline", detail
    if not failing and passing:
        detail["reason"] = (f"nothing deteriorating across {len(passing)} category "
                            f"({', '.join(passing)})" if len(passing) == 1 else
                            f"nothing deteriorating across {len(passing)} categories "
                            f"({', '.join(passing)})")
        return "price_disconnect", detail

    # No deterioration but nothing holding up either: a recent IPO with no
    # usable history is not evidence that the business is fine.
    detail["reason"] = (f"only {failing[0]} is deteriorating; one category is not "
                        f"enough to call a trap"
                        if failing else
                        "no usable trend data to confirm the price move")
    return "neutral", detail


# ─── SCORING ONE CANDIDATE ─────────────────────────────────────────────────
def fx_rate_for(quote_currency, account_currency, cache=None):
    """Rate converting a stock's quote currency into the account's, memoized.

    Rates are per currency PAIR, not per ticker, so a whole batch of Canadian
    names costs one lookup - and market_data caches that on disk for the day,
    so a re-run costs none. `cache` is an ordinary dict the caller keeps for
    the length of a run.
    """
    if not quote_currency or not account_currency:
        return None
    quote = str(quote_currency).strip().upper()
    account = str(account_currency).strip().upper()
    if not quote or not account:
        return None
    if quote == account:
        return 1.0
    if md is None:
        return None
    key = (quote, account)
    if cache is None:
        return md.get_fx_rate(quote, account)
    if key not in cache:
        cache[key] = md.get_fx_rate(quote, account)
    return cache[key]


def score_candidate(ticker, account_size=None, position_pct=DEFAULT_POSITION_PCT,
                    max_adv_pct=DEFAULT_MAX_ADV_PCT, account_currency=None,
                    avg_volume_hint=None, include_insider=False, force_refresh=False,
                    fx_cache=None):
    """Run one ticker through the existing pipeline, quietly.

    Same calls evaluate() makes, minus print_report(), plus the v5.4 fields.
    Returns (record, None) or (None, reason) - it never raises for bad data.
    """
    try:
        data, reason = get_data_cached(ticker, include_insider=include_insider,
                                       force_refresh=force_refresh)
        if data is None:
            return None, reason

        m = calc_metrics(data)
        # Average volume for the liquidity gate: prefer the info payload (same
        # 1-day cache as the price), fall back to the screener's figure.
        m["avg_volume"] = (num(data["info"].get("averageDailyVolume3Month"))
                           or num(data["info"].get("averageVolume"))
                           or num(avg_volume_hint))
        pio = piotroski(data)
        alt = altman_z(data)
        gra = graham_number(m)
        mag = magic_formula(data, m)
        ins = insider_conviction(data) if include_insider else None
        dc = dcf_scenarios(data, m)
        sc = build_scores(m, pio, alt, gra, mag, dc)
        # Before position_guidance: it reads m["data_coverage"] for its
        # thin-data risk flag.
        coverage = data_coverage(m, pio, alt, gra, mag, dc)
        pos = position_guidance(m, sc, ins)
        vs = value_screen(m, sc["dims"])

        trend = trend_analysis(data)
        liquidity_flag, liquidity = liquidity_check(
            m, account_size=account_size, position_pct=position_pct,
            max_adv_pct=max_adv_pct, account_currency=account_currency,
            avg_volume_hint=avg_volume_hint,
            fx_rate=fx_rate_for(m.get("quote_currency"), account_currency, fx_cache))
        pattern, divergence = divergence_pattern(m, trend)

        if liquidity_flag:
            m["warnings"].append(
                f"Thin liquidity: a {position_pct*100:.0f}% position would be "
                f"{liquidity['position_pct_of_adv']*100:.2f}% of average daily dollar "
                f"volume (flag above {max_adv_pct*100:.0f}%).")
        if pattern == "trend_confirms_decline":
            m["warnings"].append(
                "Value-trap pattern: price near its 52-week low AND the multi-year "
                "trend is deteriorating - higher risk.")

        record = {
            "ticker": data["ticker"],
            "name": m.get("name"),
            "sector": m.get("sector"),
            "industry": m.get("industry"),
            "quote_currency": m.get("quote_currency"),
            "composite": sc["composite"],
            "rating": sc["rating"],
            "dims": sc["dims"],
            "metrics": m,
            "frameworks": {"piotroski": pio, "altman_z": alt, "graham": gra,
                           "magic_formula": mag, "dcf": dc},
            "position_guidance": pos,
            "value_screen": vs,
            "insider": ins,
            "data_coverage": coverage["overall"],
            "coverage_detail": coverage,
            # v5.4 additions
            "trend_years_available": trend["trend_years_available"],
            "roa_trend_consistent": trend["roa_trend_consistent"],
            "roa_trend": trend["roa_trend"],
            "fcf_positive_years": trend["fcf_positive_years"],
            "debt_trend": trend["debt_trend"],
            "trend_detail": trend["trend_detail"],
            "liquidity_flag": liquidity_flag,
            "liquidity": liquidity,
            "divergence_pattern": pattern,
            "divergence_detail": divergence,
            "financials_as_of": data.get("financials_as_of"),
            "price_as_of": data.get("price_as_of"),
            "warnings": m.get("warnings", []),
            "notes": m.get("notes", []),
        }
        return record, None
    except Exception as e:
        # One malformed ticker must never take the batch down with it.
        return None, f"{type(e).__name__}: {e}"


# ─── BATCH RUN ─────────────────────────────────────────────────────────────
def _json_default(o):
    if isinstance(o, datetime):
        return o.isoformat()
    item = getattr(o, "item", None)
    if callable(item):
        try:
            return item()
        except Exception:
            pass
    return str(o)


def _write_json(document, output_path):
    """Atomic write, so a reader never sees a half-written scan."""
    import tempfile
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(output_path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(document, f, indent=2, ensure_ascii=False, default=_json_default)
        os.replace(tmp_path, output_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return output_path


def evaluate_universe(candidates_path=None, output_path=None, account_size=None,
                      position_pct=DEFAULT_POSITION_PCT, max_adv_pct=DEFAULT_MAX_ADV_PCT,
                      account_currency="USD", limit=None, sectors=None,
                      include_insider=False, force_refresh=False, quiet=False):
    """Score every candidate from Stage 0 into data\\scored_candidates.json.

    Reads the candidates.json universe_screen.py writes, runs each ticker
    through the existing scoring pipeline over market_data's cache, and
    writes every successful record plus a "skipped" list of the tickers that
    failed and why. A ticker that cannot be fetched or scored is logged and
    stepped over - the batch always finishes.
    """
    _require_market_data()

    candidates_path = Path(candidates_path) if candidates_path else data_dir() / "candidates.json"
    output_path = Path(output_path) if output_path else data_dir() / "scored_candidates.json"

    with open(candidates_path, "r", encoding="utf-8") as f:
        universe = json.load(f)
    candidates = universe.get("candidates") or []

    if sectors:
        wanted = {s.strip().lower() for s in sectors}
        candidates = [c for c in candidates if (c.get("sector") or "").lower() in wanted]
    if limit:
        candidates = candidates[:limit]

    scored, skipped = [], []
    total = len(candidates)
    started = datetime.now()
    fx_cache = {}          # one entry per currency pair for the whole batch

    if not quiet:
        print(f"\n  {B}{C}Batch scoring {total} candidate(s){X}")
        print(f"  source: {candidates_path}")
        if account_size:
            print(f"  liquidity gate: {position_pct*100:.0f}% of "
                  f"{account_size:,.0f} {account_currency}, flag above "
                  f"{max_adv_pct*100:.0f}% of average daily dollar volume")
        print()

    for i, candidate in enumerate(candidates, 1):
        ticker = str(candidate.get("ticker") or "").strip().upper()
        if not ticker:
            skipped.append({"ticker": None, "reason": "candidate row has no ticker"})
            continue

        record, reason = score_candidate(
            ticker,
            account_size=account_size, position_pct=position_pct,
            max_adv_pct=max_adv_pct, account_currency=account_currency,
            avg_volume_hint=candidate.get("avg_volume"),
            include_insider=include_insider, force_refresh=force_refresh,
            fx_cache=fx_cache)

        if record is None:
            skipped.append({"ticker": ticker, "reason": reason})
            if not quiet:
                print(f"  [{i:>4}/{total}] {ticker:<10} {Y}skipped{X} - {reason}")
            continue

        scored.append(record)
        if not quiet:
            flags = []
            if record["liquidity_flag"]:
                flags.append("thin")
            if record["divergence_pattern"] != "neutral":
                flags.append(record["divergence_pattern"])
            suffix = f"  [{', '.join(flags)}]" if flags else ""
            print(f"  [{i:>4}/{total}] {ticker:<10} {colour(record['composite'])} / 10{suffix}")

    document = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": str(candidates_path),
        "params": {
            "account_size": account_size,
            "account_currency": account_currency,
            "position_pct": position_pct,
            "max_adv_pct": max_adv_pct,
            "divergence_low_pos": DIVERGENCE_LOW_POS,
            "sectors": sorted(sectors) if sectors else None,
            "limit": limit,
            "include_insider": include_insider,
            # Which rates the liquidity gate actually converted at, so a run is
            # reproducible and an un-convertible currency is visible as a null
            # rather than as a silently unconverted comparison.
            "fx_rates": {f"{quote}->{account}": rate
                         for (quote, account), rate in sorted(fx_cache.items())},
            "evaluator_version": "5.4",
        },
        "counts": {"candidates": total, "scored": len(scored), "skipped": len(skipped)},
        "scored": scored,
        "skipped": skipped,
    }
    _write_json(document, output_path)

    if not quiet:
        elapsed = (datetime.now() - started).total_seconds()
        print(f"\n  {B}scored:{X}  {len(scored)}")
        print(f"  {B}skipped:{X} {len(skipped)}")
        if scored:
            flagged = sum(1 for r in scored if r["liquidity_flag"])
            traps = sum(1 for r in scored if r["divergence_pattern"] == "trend_confirms_decline")
            disconnects = sum(1 for r in scored if r["divergence_pattern"] == "price_disconnect")
            thin_trend = sum(1 for r in scored if r["trend_years_available"] < 2)
            print(f"  liquidity flags: {flagged}  ·  price_disconnect: {disconnects}  "
                  f"·  trend_confirms_decline: {traps}")
            print(f"  insufficient trend history: {thin_trend}")
        print(f"  {B}wrote:{X}   {output_path}  ({elapsed:.0f}s)\n")

    return document


def batch_main(argv):
    """CLI for batch mode. Reached only when a flag is passed, so the plain
    `py stock_evaluator.py TICKER [TICKER ...]` path never comes through here."""
    import argparse
    parser = argparse.ArgumentParser(
        prog="stock_evaluator.py",
        description="Batch-score the Stage 0 candidate universe "
                    "(no flags = the original single-ticker mode).")
    parser.add_argument("--batch", "--universe", action="store_true", dest="batch",
                        help="score every candidate from candidates.json")
    parser.add_argument("--candidates", metavar="PATH",
                        help="input universe (default: <stocks>/data/candidates.json)")
    parser.add_argument("--output", metavar="PATH",
                        help="output file (default: <stocks>/data/scored_candidates.json)")
    parser.add_argument("--account-size", type=float, metavar="AMOUNT",
                        help="account size for the liquidity gate; omitted = gate off")
    parser.add_argument("--account-currency", default="USD",
                        help="currency the account size is in (default USD)")
    parser.add_argument("--position-pct", type=float, default=DEFAULT_POSITION_PCT,
                        metavar="FRACTION",
                        help=f"hypothetical position as a fraction of the account "
                             f"(default {DEFAULT_POSITION_PCT})")
    parser.add_argument("--max-adv-pct", type=float, default=DEFAULT_MAX_ADV_PCT,
                        metavar="FRACTION",
                        help=f"flag above this share of average daily dollar volume "
                             f"(default {DEFAULT_MAX_ADV_PCT})")
    parser.add_argument("--sector", action="append", dest="sectors", metavar="NAME",
                        help="only score this sector (repeatable)")
    parser.add_argument("--limit", type=int, help="score only the first N candidates")
    parser.add_argument("--insider", action="store_true",
                        help="also pull insider transactions (one extra request per ticker)")
    parser.add_argument("--refresh", action="store_true", help="ignore cached data and refetch")
    parser.add_argument("--quiet", action="store_true", help="only print the summary")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="market_data cache/retry trace")
    args = parser.parse_args(argv)

    if md is None:
        print(f"\n  {R}Batch mode needs market_data.py next to this script.{X}")
        print(f"  Import failed: {_MD_IMPORT_ERROR}\n")
        return 2
    if args.verbose:
        md.DEBUG = True

    try:
        evaluate_universe(candidates_path=args.candidates, output_path=args.output,
                          account_size=args.account_size, position_pct=args.position_pct,
                          max_adv_pct=args.max_adv_pct, account_currency=args.account_currency,
                          limit=args.limit, sectors=args.sectors,
                          include_insider=args.insider, force_refresh=args.refresh,
                          quiet=args.quiet)
    except FileNotFoundError as e:
        print(f"\n  {R}Candidate universe not found: {e}{X}")
        print("  Run universe_screen.py first to build data/candidates.json.\n")
        return 2
    return 0


# ─── MAIN ──────────────────────────────────────────────────────────────────
def evaluate(ticker):
    data = get_data(ticker)
    if not data: return None
    print(f"  {C}Calculating...{X}")
    m = calc_metrics(data)
    pio = piotroski(data)
    alt = altman_z(data)
    gra = graham_number(m)
    mag = magic_formula(data, m)
    ins = insider_conviction(data)
    dc  = dcf_scenarios(data, m)
    sc  = build_scores(m, pio, alt, gra, mag, dc)
    data_coverage(m, pio, alt, gra, mag, dc)
    pos = position_guidance(m, sc, ins)
    vs  = value_screen(m, sc["dims"])
    print_report(ticker, m, sc, pio, alt, gra, mag, dc, ins, pos, vs)
    return {"ticker": ticker, "sector": m["sector"], "composite": sc["composite"]}

def main():
    # Batch mode (v5.4) is opt-in via flags. Tickers never start with "-", so
    # the original single-ticker path below is reached exactly as before.
    if any(arg.startswith("-") for arg in sys.argv[1:]):
        return batch_main(sys.argv[1:])

    print(f"\n  {B}{C}╔═════════════════════════════════════╗{X}")
    print(f"  {B}{C}║  Stock Evaluator v5.4 Risk-Adjusted ║{X}")
    print(f"  {B}{C}╚═════════════════════════════════════╝{X}")

    # CLI mode: python stock_evaluator.py TICKER [TICKER ...]
    if len(sys.argv) > 1:
        tickers = [t.upper() for t in sys.argv[1:]]
        results = []
        for tk in tickers:
            r = evaluate(tk)
            if r:
                results.append(r)
        if len(results) >= 2:
            print_diversification_summary(results)
        return

    while True:
        try:
            ticker = input(f"\n  {B}Enter ticker (or 'q' to quit): {X}").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n  Goodbye.\n"); break
        if ticker.lower() in ("q", "quit", "exit"):
            print("  Goodbye.\n"); break
        if not ticker: continue
        evaluate(ticker.upper())

if __name__ == "__main__":
    sys.exit(main() or 0)
