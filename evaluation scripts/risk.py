# =============================================================================
#  RMT ETF ANALYSIS TOOL
#  Random Matrix Theory noise filter for ETF portfolios
#  Supports US and Canadian (TSX) ETFs
#
#  SETUP (run once in your terminal):
#    pip install yfinance numpy pandas colorama
#
#  RUN:
#    python rmt_etf_analysis.py
# =============================================================================

YEARS_OF_DATA      = 3    # Years of history (2–5 recommended)
TOP_ETF_PER_SIGNAL = 5    # ETFs shown per signal

# =============================================================================
#  No need to edit below this line
# =============================================================================

import sys, os, warnings
warnings.filterwarnings("ignore")


def check_dependencies():
    missing = []
    for pkg in ["yfinance", "numpy", "pandas", "colorama"]:
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"\n  Missing packages. Run:\n\n    pip install {' '.join(missing)}\n")
        sys.exit(1)

check_dependencies()

import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from colorama import init as colorama_init, Fore, Style
colorama_init()  # Enables ANSI colors on Windows

# ── Color shortcuts ────────────────────────────────────────────────────────────
RED    = Fore.RED + Style.BRIGHT
YELLOW = Fore.YELLOW + Style.BRIGHT
GREEN  = Fore.GREEN + Style.BRIGHT
CYAN   = Fore.CYAN + Style.BRIGHT
WHITE  = Style.BRIGHT
RESET  = Style.RESET_ALL

def color_corr_padded(val, width=6):
    """Right-pad a correlation value to `width` chars, then wrap in color.
    Padding goes BEFORE the color code so invisible ANSI chars don't break alignment."""
    s = f"{val:.2f}"
    pad = " " * (width - len(s))
    if val > 0.75:
        return pad + RED   + s + RESET
    elif val > 0.40:
        return pad + YELLOW + s + RESET
    else:
        return pad + GREEN  + s + RESET


# ── Input parsing ──────────────────────────────────────────────────────────────

def parse_input(raw):
    """
    Accept tickers alone or with dollar amounts.
      'ZEQT, ZQQ, ZEA'                  — no amounts
      'ZEQT 20000, ZQQ 10000, ZEA 5000' — with amounts
    Returns: (tickers list, amounts dict keyed by UPPER ticker)
    """
    tickers = []
    amounts = {}
    for item in raw.split(","):
        parts = item.strip().upper().split()
        if not parts:
            continue
        ticker = parts[0]
        tickers.append(ticker)
        if len(parts) >= 2:
            try:
                amt = float(parts[1].replace("$", "").replace(",", ""))
                amounts[ticker] = amt
            except ValueError:
                pass
    return tickers, amounts


def get_usdcad_rate():
    """Fetch live USD/CAD rate. Falls back to a reasonable estimate if unavailable."""
    try:
        import yfinance as _yf
        data = _yf.download("USDCAD=X", period="2d", auto_adjust=True, progress=False)
        if not data.empty:
            rate = float(data["Close"].dropna().iloc[-1])
            print(f"  {WHITE}USD/CAD rate: {rate:.4f} (live){RESET}")
            return rate
    except Exception:
        pass
    fallback = 1.36
    print(f"  {YELLOW}Could not fetch live USD/CAD rate — using {fallback} as estimate.{RESET}")
    return fallback


def load_from_wealthsimple_csv(filepath):
    """
    Parse a Wealthsimple holdings CSV export.
    Returns (tickers list, amounts dict in CAD).
    """
    try:
        df = pd.read_csv(filepath)
    except Exception as e:
        print(f"\n  {RED}Could not read file: {e}{RESET}")
        return None, {}

    # Drop footer rows (Wealthsimple adds a timestamp row at the bottom)
    df = df[df["Symbol"].notna() & df["Market Value"].notna()].copy()
    df = df[df["Symbol"].astype(str).str.strip() != ""]

    # Normalise column names defensively
    df.columns = [c.strip() for c in df.columns]

    required = {"Symbol", "Market Value", "Market Value Currency"}
    if not required.issubset(set(df.columns)):
        print(f"\n  {RED}CSV is missing expected columns.{RESET}")
        print(f"  Found: {list(df.columns)}")
        return None, {}

    df["Symbol"]               = df["Symbol"].astype(str).str.strip().str.upper()
    df["Market Value"]         = pd.to_numeric(df["Market Value"], errors="coerce")
    df["Market Value Currency"]= df["Market Value Currency"].astype(str).str.strip().str.upper()
    df = df.dropna(subset=["Market Value"])

    # ── Account selection ─────────────────────────────────────────────────────
    account_col = None
    for col in ("Account Name", "Account Type", "Account"):
        if col in df.columns:
            account_col = col
            break

    if account_col:
        accounts = sorted(df[account_col].dropna().unique())
        print(f"\n  Found {len(accounts)} account(s) in the CSV:\n")
        for i, acc in enumerate(accounts, 1):
            total_cad = df[df[account_col] == acc]["Market Value"].sum()
            print(f"  {i}.  {acc:<20}  ~${total_cad:>10,.0f} CAD")
        print()
        print("  Which accounts to include? Enter numbers separated by commas,")
        print("  or press Enter to include all.")
        sel = input("  Selection: ").strip()
        if sel:
            try:
                indices  = [int(x.strip()) - 1 for x in sel.split(",")]
                selected = [accounts[i] for i in indices if 0 <= i < len(accounts)]
                df = df[df[account_col].isin(selected)]
            except (ValueError, IndexError):
                print(f"  {YELLOW}Invalid selection — using all accounts.{RESET}")

    # ── Currency conversion ───────────────────────────────────────────────────
    has_usd = (df["Market Value Currency"] == "USD").any()
    usdcad  = get_usdcad_rate() if has_usd else 1.0

    def to_cad(row):
        return row["Market Value"] * usdcad if row["Market Value Currency"] == "USD" \
               else row["Market Value"]

    df["Value CAD"] = df.apply(to_cad, axis=1)

    # ── Aggregate across accounts ─────────────────────────────────────────────
    aggregated = df.groupby("Symbol")["Value CAD"].sum().to_dict()

    # Remove very small positions (fractional shares, dust)
    min_value  = 5.0
    tiny       = {s: v for s, v in aggregated.items() if v < min_value}
    aggregated = {s: v for s, v in aggregated.items() if v >= min_value}

    if tiny:
        print(f"\n  {YELLOW}Skipped {len(tiny)} tiny position(s) under ${min_value:.0f} CAD:{RESET}")
        for sym, val in sorted(tiny.items(), key=lambda x: -x[1]):
            print(f"    · {sym}: ${val:.2f}")

    # ── Show what was loaded ──────────────────────────────────────────────────
    total = sum(aggregated.values())
    print(f"\n  {WHITE}Holdings loaded from CSV ({len(aggregated)} positions):{RESET}\n")
    print(f"  {'Ticker':<10}  {'Value (CAD)':>12}  {'Weight':>7}")
    print(f"  {'-'*10}  {'-'*12}  {'-'*7}")
    for sym, val in sorted(aggregated.items(), key=lambda x: -x[1]):
        bar = "█" * max(1, int(28 * val / total))
        print(f"  {sym:<10}  ${val:>11,.0f}  {100*val/total:>6.1f}%  {bar}")
    print(f"  {'-'*10}  {'-'*12}  {'-'*7}")
    print(f"  {'TOTAL':<10}  ${total:>11,.0f}")

    if len(aggregated) < 4:
        print(f"\n  {YELLOW}Only {len(aggregated)} position(s) — need at least 4 for RMT.{RESET}")
        return None, {}

    return list(aggregated.keys()), aggregated


def get_input():
    print()
    print(CYAN + "=" * 62 + RESET)
    print(CYAN + "  RMT ETF ANALYSIS TOOL" + RESET)
    print(CYAN + "=" * 62 + RESET)
    print()
    print("  How do you want to enter your holdings?")
    print()
    print(f"  {WHITE}1{RESET}  Type tickers manually")
    print(f"  {WHITE}2{RESET}  Load from Wealthsimple CSV export")
    print()
    choice = input("  Choice (1 or 2): ").strip()

    # ── Option 2: CSV ─────────────────────────────────────────────────────────
    if choice == "2":
        print()
        print("  Download your holdings CSV from Wealthsimple:")
        print("  Account → Holdings → Export (top-right corner)")
        print()
        filepath = input("  Path to CSV file: ").strip().strip('"').strip("'")
        if not filepath:
            return None, {}
        tickers, amounts = load_from_wealthsimple_csv(filepath)
        if tickers:
            print(f"\n  {GREEN}✓ Loaded {len(tickers)} tickers from CSV.{RESET}")
        return tickers, amounts

    # ── Option 1: Manual ──────────────────────────────────────────────────────
    print()
    print("  Enter tickers separated by commas.")
    print("  Optionally add a dollar amount after each ticker.")
    print()
    print(f"    No amounts :  {WHITE}ZEQT, ZQQ, ZEA, ZCN, XGD{RESET}")
    print(f"    With amounts: {WHITE}ZEQT 20000, ZQQ 10000, XGD 5000{RESET}")
    print()
    print("  Canadian ETFs: enter as-is — .TO is added automatically.")
    print()

    raw = input("  Tickers: ").strip()
    if not raw:
        return None, {}

    tickers, amounts = parse_input(raw)

    if len(tickers) < 4:
        print(f"\n  {RED}Need at least 4 tickers for a meaningful analysis.{RESET}")
        return None, {}

    print(f"\n  Got {len(tickers)} tickers: {', '.join(tickers)}")

    if amounts:
        total = sum(amounts.values())
        print(f"  Total entered: ${total:,.0f}")
        missing_amt = [t for t in tickers if t not in amounts]
        if missing_amt:
            print(f"  {YELLOW}No amount for: {', '.join(missing_amt)} — excluded from dollar sections.{RESET}")

    return tickers, amounts


# ── Data fetching ──────────────────────────────────────────────────────────────

def silent_download(ticker, start, end):
    devnull   = open(os.devnull, "w")
    old_err   = sys.stderr
    sys.stderr = devnull
    try:
        data = yf.download(ticker, start=start, end=end, auto_adjust=True, progress=False)
    finally:
        sys.stderr = old_err
        devnull.close()
    return data


def resolve_ticker(ticker, start, end):
    candidates = [ticker] + ([ticker + ".TO"] if "." not in ticker else [])
    for candidate in candidates:
        try:
            raw = silent_download(candidate, start, end)
            if isinstance(raw.columns, pd.MultiIndex):
                close = raw["Close"]
            else:
                close = raw[["Close"]]
                close.columns = [candidate]
            if not close.empty and len(close) > 10:
                display = ticker + " (TSX)" if candidate.endswith(".TO") else ticker
                return display, close[candidate]
        except Exception:
            continue
    return ticker, None


def fetch_prices(tickers, years):
    end   = datetime.today()
    start = end - timedelta(days=int(365 * years))
    print(f"\n  Fetching {years} years of data...\n")

    series_dict, failed = {}, []
    for ticker in tickers:
        display, series = resolve_ticker(ticker, start, end)
        if series is not None:
            series_dict[display] = series
            print(f"    {GREEN}✓{RESET}  {display}")
        else:
            failed.append(ticker)
            print(f"    {RED}✗{RESET}  {ticker}  (not found — check the symbol)")

    if failed:
        print(f"\n  {YELLOW}Could not load: {', '.join(failed)}{RESET}")
    if len(series_dict) < 4:
        print(f"\n  {RED}Only {len(series_dict)} ETF(s) loaded. Need at least 4.{RESET}")
        return None

    # ── History diagnostic ────────────────────────────────────────────────────
    # Show each ETF's individual history so the user can see who is limiting the window.
    combined_raw = pd.DataFrame(series_dict)
    full_start   = combined_raw.apply(lambda s: s.first_valid_index()).max()   # latest first date
    shared_days  = len(combined_raw.dropna())

    rows = []
    for name, series in series_dict.items():
        etf_days = series.dropna().count()
        first    = series.first_valid_index()
        rows.append((name, etf_days, first))
    rows.sort(key=lambda x: x[1])   # shortest first so the bottleneck is obvious

    max_days = max(r[1] for r in rows)
    print(f"\n  {'ETF':<16}  {'Days':>5}  {'Starts':<12}  Note")
    print(f"  {'-'*16}  {'-'*5}  {'-'*12}  ----")
    for name, days, first in rows:
        first_str = first.strftime("%Y-%m-%d") if hasattr(first, "strftime") else str(first)[:10]
        if days == max_days:
            note = ""
        elif days <= shared_days + 5:
            note = f"{YELLOW}⚠ bottleneck — limits shared window{RESET}"
        else:
            note = f"{YELLOW}shorter than longest{RESET}"
        bar = "█" * int((days / max_days) * 20)
        print(f"  {name:<16}  {days:>5}  {first_str:<12}  {bar}  {note}")

    prices = combined_raw.dropna()
    print(f"\n  Shared window: {WHITE}{shared_days} trading days{RESET}", end="")

    # Warn clearly if shared window is much shorter than what any individual ETF has
    if shared_days < max_days * 0.5:
        shortest_name = rows[0][0]
        print(f"  {RED}(only {round(100*shared_days/max_days)}% of your longest ETF's history){RESET}")
        print(f"\n  {YELLOW}⚠  History bottleneck: {shortest_name} starts {rows[0][2].strftime('%Y-%m-%d') if hasattr(rows[0][2],'strftime') else rows[0][2]}.{RESET}")
        print(f"  {YELLOW}   Removing it would give ~{rows[1][1]} days of shared history instead of {shared_days}.{RESET}")
        print(f"  {YELLOW}   Consider dropping it from your list and re-running.{RESET}")
    else:
        print()

    print(f"\n  {len(prices.columns)} ETFs ready for analysis.")
    return prices


# ── Math ───────────────────────────────────────────────────────────────────────

def log_returns(prices):
    return np.log(prices / prices.shift(1)).dropna()

def marchenko_pastur_upper(N, T):
    return (1 + 1.0 / np.sqrt(T / N)) ** 2

def label_signal(rank, loadings):
    if rank == 0:
        return "MARKET MODE"
    def base(t):
        return t.replace(".TO","").replace(" (TSX)","").upper()
    top3 = [base(t) for t in loadings.abs().nlargest(3).index]
    theme_map = [
        ({"TLT","IEF","SHY","AGG","BND","ZAG","XBB","ZGB","CLF","XSB","VSB","VAB","ZSB"}, "BONDS / RATES"),
        ({"GLD","IAU","SGOL","CGL","MNT"},                                                  "GOLD"),
        ({"XGD","GDX","GDXJ","ZJG"},                                                        "GOLD MINERS"),
        ({"GSG","DJP","PDBC","BCM","ZCO"},                                                  "COMMODITIES"),
        ({"EEM","EFA","VEA","VWO","ZEM","XEM","ZEA","ACWI","ZDI","ZGQ","XEF","VIU"},       "INTERNATIONAL"),
        ({"VNQ","IYR","ZRE","XRE"},                                                          "REAL ESTATE"),
        ({"XLE","VDE","ZEO","XEG"},                                                          "ENERGY"),
        ({"XLK","VGT","QQQ","ZQQ","TXF","SMH"},                                             "TECHNOLOGY"),
        ({"XLF","VFH","ZEB","XFN"},                                                          "FINANCIALS"),
        ({"XLV","VHT","ZUH","XHC","IBB"},                                                   "HEALTHCARE"),
        ({"ZLB","XMV","ZMI","XMB","USMV","SPLV"},                                          "LOW VOLATILITY"),
        ({"ZDV","XDV","VIG","CDZ","PDC","ZDY"},                                             "DIVIDENDS"),
        ({"ZCN","XIC","VCN","XIU"},                                                          "CANADIAN EQUITY"),
        ({"ZEQT","XEQT","VGRO","VCNS","XBAL","ZGRO","VBAL","ZBAL"},                       "ASSET ALLOCATION"),
    ]
    for etf_set, name in theme_map:
        if any(b in etf_set for b in top3):
            return f"SIGNAL {rank+1}  ({name})"
    return f"SIGNAL {rank+1}"


# ── CSV export ─────────────────────────────────────────────────────────────────

def save_csv(available, raw_corr_df, eigenvalues, eigvecs, n_signal,
             total_var, display_amounts, threshold, T, years, script_dir):

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename  = os.path.join(script_dir, f"rmt_analysis_{timestamp}.csv")
    rows      = []

    def blank():
        rows.append([])

    def header(text):
        rows.append([text])

    # ── Run info ──
    header("RMT ETF ANALYSIS")
    rows.append(["Date",             datetime.now().strftime("%Y-%m-%d %H:%M")])
    rows.append(["ETFs",             ", ".join(available)])
    rows.append(["Years of data",    years])
    rows.append(["Trading days",     T])
    rows.append(["Noise threshold",  round(threshold, 4)])
    rows.append(["Genuine signals",  n_signal])
    blank()

    # ── Correlation matrix ──
    header("CORRELATION MATRIX")
    rows.append([""] + available)
    for t1 in available:
        row = [t1]
        for t2 in available:
            row.append(1.0 if t1 == t2 else round(raw_corr_df.loc[t1, t2], 4))
        rows.append(row)
    blank()

    # ── Pairs ──
    header("CORRELATION PAIRS  (HIGH = red, MODERATE = yellow, LOW = green when charted)")
    rows.append(["ETF 1", "ETF 2", "Correlation", "Tier"])
    for i, t1 in enumerate(available):
        for j, t2 in enumerate(available):
            if j <= i:
                continue
            c    = raw_corr_df.loc[t1, t2]
            tier = "HIGH" if c > 0.75 else ("MODERATE" if c > 0.40 else "LOW")
            rows.append([t1, t2, round(c, 4), tier])
    blank()

    # ── Eigenvalues (scree plot data) ──
    header("EIGENVALUES  —  chart this as a bar chart with a horizontal line at the threshold to see signal vs noise")
    rows.append(["Rank", "Eigenvalue", "Signal or Noise", "Noise Threshold (flat line)"])
    for i, ev in enumerate(eigenvalues):
        label = "Signal" if ev > threshold else "Noise"
        rows.append([i + 1, round(ev, 4), label, round(threshold, 4) if i == 0 else ""])
    blank()

    # ── Signals ──
    header("GENUINE SIGNALS")
    rows.append(["Signal", "Eigenvalue", "% of Variance", "Top ETFs (loading)"])
    for i in range(n_signal):
        loadings = pd.Series(eigvecs[:, i], index=available)
        abs_load = loadings.abs().sort_values(ascending=False)
        pct_var  = 100 * eigenvalues[i] / total_var
        title    = label_signal(i, loadings)
        top_str  = "  |  ".join(
            [f"{t}: {loadings[t]:+.3f}" for t in abs_load.head(TOP_ETF_PER_SIGNAL).index]
        )
        rows.append([title, round(eigenvalues[i], 4), round(pct_var, 2), top_str])
    blank()

    # ── Portfolio breakdown ──
    if display_amounts:
        total_inv = sum(display_amounts.values())
        header("PORTFOLIO BREAKDOWN")
        rows.append(["ETF", "Amount ($)", "Weight (%)"])
        for t in available:
            amt = display_amounts.get(t, None)
            if amt is not None:
                rows.append([t, amt, round(100 * amt / total_inv, 2)])
            else:
                rows.append([t, "not entered", ""])
        rows.append(["TOTAL", total_inv, 100.0])
        blank()

        header("DOUBLING UP — high-correlation pairs and combined dollar exposure")
        rows.append(["ETF 1", "Amount ($)", "ETF 2", "Amount ($)", "Combined ($)", "Correlation"])
        for i, t1 in enumerate(available):
            for j, t2 in enumerate(available):
                if j <= i:
                    continue
                c = raw_corr_df.loc[t1, t2]
                if c > 0.75:
                    a1 = display_amounts.get(t1, 0)
                    a2 = display_amounts.get(t2, 0)
                    rows.append([t1, a1, t2, a2, a1 + a2, round(c, 4)])
        blank()

    pd.DataFrame(rows).to_csv(filename, index=False, header=False)
    return filename


# ── Main analysis ──────────────────────────────────────────────────────────────

def run_analysis():

    tickers, amounts = get_input()
    if tickers is None:
        return

    prices = fetch_prices(tickers, YEARS_OF_DATA)
    if prices is None:
        return

    available = list(prices.columns)

    # Map user-entered ticker names to display names (e.g. ZEQT → ZEQT (TSX))
    display_amounts = {}
    for display in available:
        base = display.replace(" (TSX)", "").strip()
        if base in amounts:
            display_amounts[display] = amounts[base]

    N = len(available)
    T = len(prices) - 1

    returns          = log_returns(prices)
    corr_matrix      = returns.corr().values
    eigenvalues, eigvecs = np.linalg.eigh(corr_matrix)

    order        = np.argsort(eigenvalues)[::-1]
    eigenvalues  = eigenvalues[order]
    eigvecs      = eigvecs[:, order]

    threshold   = marchenko_pastur_upper(N, T)
    signal_mask = eigenvalues > threshold
    n_signal    = int(signal_mask.sum())
    n_noise     = N - n_signal
    pct_noise   = 100 * n_noise / N

    sig_eigs = eigenvalues[:n_signal]
    pr = (sig_eigs.sum() ** 2) / ((sig_eigs ** 2).sum()) if n_signal > 0 else 1.0

    raw_corr_df = pd.DataFrame(corr_matrix, index=available, columns=available)
    total_var   = eigenvalues.sum()

    div = CYAN + "=" * 62 + RESET

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n\n{div}")
    print(CYAN + "  RESULTS — SUMMARY" + RESET)
    print(div)
    print(f"  ETFs analyzed          : {N}")
    print(f"  Trading days used      : {T}  ({YEARS_OF_DATA} years)")
    print(f"  Noise threshold        : {threshold:.3f}")
    print(f"  Genuine signals found  : {WHITE}{n_signal}{RESET}")
    print(f"  Noise fraction         : {n_noise} of {N} ({pct_noise:.0f}%) are noise")
    print(f"  Effective positions    : {WHITE}{pr:.1f}{RESET}  independent bets")
    print()

    if   pr / N >= 0.6:  q_col, quality = GREEN,  "GOOD — your list covers distinct themes well."
    elif pr / N >= 0.35: q_col, quality = YELLOW, "MODERATE — some redundancy present."
    else:                q_col, quality = RED,    "LOW — most ETFs are driven by the same factor."
    print(f"  Diversification quality: {q_col}{quality}{RESET}")

    if n_signal == 1:
        print(f"""
  {YELLOW}NOTE: Only 1 genuine signal found.{RESET}
  All your ETFs are effectively driven by one factor: global equities.
  Geography and style differences exist, but they all rise and fall
  together. To get a 2nd genuine signal, add a different asset class:

    {GREEN}Bonds       {RESET}ZAG, XBB, TLT
    {GREEN}Gold        {RESET}CGL, GLD
    {GREEN}Real estate {RESET}ZRE, VNQ
    {GREEN}Commodities {RESET}ZCO, GSG
""")

    # ── Portfolio breakdown ────────────────────────────────────────────────────
    if display_amounts:
        total_inv = sum(display_amounts.values())
        print(f"\n{div}")
        print(CYAN + "  PORTFOLIO BREAKDOWN" + RESET)
        print(div)
        print(f"\n  {'ETF':<16}  {'Amount':>12}  {'Weight':>8}")
        print(f"  {'-'*16}  {'-'*12}  {'-'*8}")
        for t in available:
            amt = display_amounts.get(t)
            if amt is not None:
                print(f"  {t:<16}  ${amt:>11,.0f}  {100*amt/total_inv:>7.1f}%")
            else:
                print(f"  {t:<16}  {'—':>12}  {'—':>8}")
        print(f"  {'-'*16}  {'-'*12}  {'-'*8}")
        print(f"  {'TOTAL':<16}  ${total_inv:>11,.0f}  {'100.0%':>8}")

    # ── Signals ───────────────────────────────────────────────────────────────
    print(f"\n{div}")
    print(CYAN + "  GENUINE SIGNALS  (what actually drives your portfolio)" + RESET)
    print(div)

    for i in range(n_signal):
        loadings = pd.Series(eigvecs[:, i], index=available)
        abs_load = loadings.abs().sort_values(ascending=False)
        top_etfs = abs_load.head(TOP_ETF_PER_SIGNAL)
        pct_var  = 100 * eigenvalues[i] / total_var
        title    = label_signal(i, loadings)

        print(f"\n  {WHITE}{title}{RESET}")
        print(f"  Strength: {eigenvalues[i]:.2f}  |  Explains {pct_var:.1f}% of total variance")

        if display_amounts:
            exposure  = sum(display_amounts.get(t, 0) * abs(loadings[t]) for t in available)
            total_inv = sum(display_amounts.values())
            pct_exp   = 100 * exposure / total_inv if total_inv > 0 else 0
            print(f"  Dollar exposure: ~${exposure:,.0f}  ({pct_exp:.0f}% of portfolio)")

        print(f"  {'ETF':<16}  {'Influence':^22}  Loading")
        print(f"  {'-'*16}  {'-'*22}  -------")
        for ticker in top_etfs.index:
            raw_load = loadings[ticker]
            bar      = "█" * max(1, int(abs(raw_load) * 28))
            print(f"  {ticker:<16}  {bar:<22}  {raw_load:+.3f}")

    # ── Correlations ──────────────────────────────────────────────────────────
    print(f"\n{div}")
    print(CYAN + "  ACTUAL CORRELATIONS" + RESET)
    print(div)
    print("  " + RED + "RED > 0.75 (high)" + RESET
          + "  |  " + YELLOW + "YELLOW 0.40–0.75 (moderate)" + RESET
          + "  |  " + GREEN + "GREEN < 0.40 (low)" + RESET)

    high_pairs, mid_pairs, low_pairs = [], [], []
    for i, t1 in enumerate(available):
        for j, t2 in enumerate(available):
            if j <= i:
                continue
            c = raw_corr_df.loc[t1, t2]
            if   c > 0.75: high_pairs.append((t1, t2, c))
            elif c > 0.40: mid_pairs.append((t1, t2, c))
            else:          low_pairs.append((t1, t2, c))

    # HIGH
    print(f"\n  {RED}HIGH (> 0.75) — moving together, limited diversification:{RESET}\n")
    if high_pairs:
        for t1, t2, c in sorted(high_pairs, key=lambda x: -x[2]):
            bar = "█" * int(c * 18)
            dollar_str = ""
            if t1 in display_amounts and t2 in display_amounts:
                combined   = display_amounts[t1] + display_amounts[t2]
                dollar_str = f"   ${combined:,.0f} combined"
            print(f"    {t1} ↔ {t2:<16}  {RED}{bar:<18}  {c:.2f}{RESET}{dollar_str}")
    else:
        print(f"    {GREEN}None — no strongly redundant pairs.{RESET}")

    # MODERATE
    print(f"\n  {YELLOW}MODERATE (0.40–0.75) — related but not identical:{RESET}\n")
    if mid_pairs:
        for t1, t2, c in sorted(mid_pairs, key=lambda x: -x[2]):
            bar = "█" * int(c * 18)
            print(f"    {t1} ↔ {t2:<16}  {YELLOW}{bar:<18}  {c:.2f}{RESET}")
    else:
        print(f"    {YELLOW}None in this range.{RESET}")

    # LOW
    print(f"\n  {GREEN}LOW (< 0.40) — genuinely independent:{RESET}\n")
    if low_pairs:
        for t1, t2, c in sorted(low_pairs, key=lambda x: abs(x[2])):
            print(f"    {t1} ↔ {t2:<16}  {GREEN}{c:+.2f}{RESET}")
    else:
        print(f"    {YELLOW}None — all ETFs share meaningful common exposure.{RESET}")

    # ── Doubling up ────────────────────────────────────────────────────────────
    covered = [(t1, t2, c) for t1, t2, c in high_pairs
               if t1 in display_amounts and t2 in display_amounts]
    if covered:
        print(f"\n{div}")
        print(CYAN + "  DOUBLING UP — HIGH CORRELATION PAIRS WITH DOLLAR EXPOSURE" + RESET)
        print(div)
        print("\n  These pairs move together. Holding both concentrates that")
        print("  combined dollar amount into essentially one bet.\n")
        print(f"  {'Pair':<36}  {'Combined':>12}  {'Corr':>6}")
        print(f"  {'-'*36}  {'-'*12}  {'-'*6}")
        for t1, t2, c in sorted(covered, key=lambda x: -(display_amounts.get(x[0],0) + display_amounts.get(x[1],0))):
            combined = display_amounts[t1] + display_amounts[t2]
            pair_str = f"{t1} ↔ {t2}"
            print(f"  {RED}{pair_str:<36}  ${combined:>11,.0f}  {c:.2f}{RESET}")

    # ── Full correlation table ─────────────────────────────────────────────────
    print(f"\n{div}")
    print(CYAN + "  FULL CORRELATION TABLE" + RESET)
    print(div)
    print()

    col_w  = 8
    labels = [t.replace(" (TSX)", "")[:col_w] for t in available]

    # Header
    hdr = " " * 14
    for lbl in labels:
        hdr += "  " + lbl.rjust(col_w)
    print("  " + hdr)

    # Rows — pad BEFORE color so ANSI doesn't break column width
    for i, t1 in enumerate(available):
        short1 = labels[i][:14]
        row = f"  {short1:<14}"
        for j, t2 in enumerate(available):
            if t1 == t2:
                row += "  " + "—".rjust(col_w)
            else:
                c    = raw_corr_df.loc[t1, t2]
                row += "  " + color_corr_padded(c, col_w)
        print(row)

    # ── CSV ────────────────────────────────────────────────────────────────────
    script_dir = os.path.dirname(os.path.abspath(__file__))
    csv_path   = save_csv(
        available, raw_corr_df, eigenvalues, eigvecs,
        n_signal, total_var, display_amounts,
        threshold, T, YEARS_OF_DATA, script_dir
    )

    print(f"\n{div}")
    print(f"  {GREEN}CSV saved →{RESET} {csv_path}")
    print(f"  Open in Excel for charts. See the EIGENVALUES section")
    print(f"  for a scree plot (bar chart + horizontal threshold line).")
    print(div)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    while True:
        run_analysis()
        print()
        again = input("  Run another analysis? (y / n): ").strip().lower()
        if again not in ("y", "yes"):
            break
    print(f"\n  Goodbye.\n")
