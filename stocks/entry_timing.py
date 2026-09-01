#!/usr/bin/env python3
"""Standalone RSI/MACD entry-timing overlay for screened candidates.

This module intentionally does not participate in the fundamental composite
score.  It answers a narrower question: whether recent price action suggests
that this week may be a reasonable entry point for a candidate that has
already passed the fundamental screen.

Price history comes from market_data.download_prices(), which uses batched
yf.download() calls and the project's shared cache/backoff/session policy.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

import market_data as md
import stocks_common as common


DEFAULT_CANDIDATES_PATH = common.data_dir() / "scored_candidates.json"
TIMING_FLAGS_PATH = common.data_dir() / "timing_flags.json"

RSI_LENGTH = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
MINIMUM_HISTORY = max(RSI_LENGTH + 1, MACD_SLOW)


def _numeric_series(values: Any) -> pd.Series:
    """Return values as a float Series while preserving a supplied index."""
    if isinstance(values, pd.Series):
        series = values.copy()
    else:
        series = pd.Series(values)
    return pd.to_numeric(series, errors="coerce").astype(float)


def compute_rsi(close_series, length: int = 14) -> pd.Series:
    """Compute Wilder's Relative Strength Index for a close-price series.

    The first ``length`` observations are undefined because RSI needs that
    many price changes. A flat window is reported as neutral RSI 50; a window
    with gains but no losses is 100, and the converse is 0.
    """
    if length < 1:
        raise ValueError("RSI length must be at least 1")

    close = _numeric_series(close_series)
    delta = close.diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)

    # Wilder seeds each average with a simple mean, then applies his recursive
    # smoothing formula. This is equivalent to alpha=1/length after the seed,
    # but spelling out the seed avoids the initialization drift of a plain
    # pandas ewm() call.
    avg_gain = gains.rolling(window=length, min_periods=length).mean()
    avg_loss = losses.rolling(window=length, min_periods=length).mean()
    for position in range(length + 1, len(close)):
        previous_gain = avg_gain.iloc[position - 1]
        previous_loss = avg_loss.iloc[position - 1]
        current_gain = gains.iloc[position]
        current_loss = losses.iloc[position]
        if not pd.isna(previous_gain) and not pd.isna(current_gain):
            avg_gain.iloc[position] = (
                previous_gain * (length - 1) + current_gain
            ) / length
        if not pd.isna(previous_loss) and not pd.isna(current_loss):
            avg_loss.iloc[position] = (
                previous_loss * (length - 1) + current_loss
            ) / length

    relative_strength = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + relative_strength))

    both_flat = avg_gain.eq(0) & avg_loss.eq(0)
    only_gains = avg_gain.gt(0) & avg_loss.eq(0)
    only_losses = avg_gain.eq(0) & avg_loss.gt(0)
    rsi = rsi.mask(both_flat, 50.0)
    rsi = rsi.mask(only_gains, 100.0)
    rsi = rsi.mask(only_losses, 0.0)
    rsi.name = f"rsi_{length}"
    return rsi


def compute_macd(close_series, fast: int = 12, slow: int = 26,
                 signal: int = 9) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Return the MACD line, signal line, and histogram."""
    if min(fast, slow, signal) < 1:
        raise ValueError("MACD periods must all be at least 1")
    if fast >= slow:
        raise ValueError("MACD fast period must be shorter than slow period")

    close = _numeric_series(close_series)
    fast_ema = close.ewm(span=fast, adjust=False).mean()
    slow_ema = close.ewm(span=slow, adjust=False).mean()
    macd = fast_ema - slow_ema
    signal_line = macd.ewm(span=signal, adjust=False).mean()
    histogram = macd - signal_line

    macd.name = "macd"
    signal_line.name = "macd_signal"
    histogram.name = "macd_histogram"
    return macd, signal_line, histogram


def _history_for_ticker(ticker: str, price_history: Any) -> Any:
    """Accept either one ticker's rows or a batched market_data result."""
    if not isinstance(price_history, Mapping):
        return price_history

    symbol = str(ticker).strip().upper()
    for key, value in price_history.items():
        if str(key).strip().upper() == symbol:
            return value
    return None


def _close_series(ticker: str, price_history: Any) -> pd.Series:
    """Extract clean closes from market_data rows or a pandas object."""
    history = _history_for_ticker(ticker, price_history)
    if history is None:
        return pd.Series(dtype=float)

    if isinstance(history, pd.Series):
        closes = history
    elif isinstance(history, pd.DataFrame):
        if "close" in history.columns:
            closes = history["close"]
        elif "Close" in history.columns:
            closes = history["Close"]
        else:
            return pd.Series(dtype=float)
    elif isinstance(history, Sequence) and not isinstance(history, (str, bytes)):
        closes = pd.Series([
            row.get("close", row.get("Close")) if isinstance(row, Mapping) else row
            for row in history
        ])
    else:
        return pd.Series(dtype=float)

    return _numeric_series(closes).dropna().reset_index(drop=True)


def _macd_crossed_positive(histogram: pd.Series, sessions: int = 3) -> bool:
    """Whether a negative-to-positive histogram cross landed recently."""
    values = histogram.dropna()
    if len(values) < 2:
        return False

    first_transition = max(1, len(values) - sessions)
    return any(
        values.iloc[position - 1] < 0 < values.iloc[position]
        for position in range(first_transition, len(values))
    )


def timing_flag(ticker: str, price_history: Any) -> str:
    """Classify recent RSI/MACD action for one fundamentally screened ticker."""
    closes = _close_series(ticker, price_history)
    if len(closes) < MINIMUM_HISTORY:
        return "insufficient_history"

    rsi = compute_rsi(closes, RSI_LENGTH).dropna()
    if len(rsi) < 2:
        return "insufficient_history"

    _, _, histogram = compute_macd(
        closes, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL
    )
    macd_cross = _macd_crossed_positive(histogram, sessions=3)

    latest_rsi = float(rsi.iloc[-1])
    previous_rsi = float(rsi.iloc[-2])

    # Overbought is surfaced even though it does not disqualify a cheap stock.
    if latest_rsi > 70:
        return "overbought"

    rsi_turning_up = previous_rsi < 35 and latest_rsi > previous_rsi
    if rsi_turning_up or macd_cross:
        return "reversal_signal"

    rsi_still_falling = latest_rsi < 35 and latest_rsi < previous_rsi
    if rsi_still_falling and not macd_cross:
        return "still_falling"

    return "neutral"


def _candidate_tickers(document: Any) -> list[str]:
    """Read tickers from the scored-candidate document, preserving order."""
    if isinstance(document, Mapping):
        candidates = document.get("scored") or []
    elif isinstance(document, Sequence) and not isinstance(document, (str, bytes)):
        candidates = document
    else:
        raise ValueError("scored candidates must be a JSON object or list")

    tickers: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        raw_ticker = candidate.get("ticker") if isinstance(candidate, Mapping) else candidate
        ticker = str(raw_ticker or "").strip().upper()
        if ticker and ticker not in seen:
            seen.add(ticker)
            tickers.append(ticker)
    return tickers


def evaluate_timing(candidates_path=DEFAULT_CANDIDATES_PATH) -> dict[str, Any]:
    """Evaluate every scored candidate and write ``data/timing_flags.json``.

    All candidate histories are requested together through the shared batched
    downloader. A missing ticker or short recent-IPO history receives
    ``insufficient_history`` rather than stopping the run.
    """
    candidates_path = Path(candidates_path)
    with candidates_path.open("r", encoding="utf-8") as source:
        candidates = json.load(source)

    tickers = _candidate_tickers(candidates)
    price_history = md.download_prices(
        tickers,
        period="6mo",
        interval="1d",
        cache_key="entry_timing",
    )

    document = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "flags": {
            ticker: timing_flag(ticker, price_history)
            for ticker in tickers
        },
    }
    common.write_json(document, TIMING_FLAGS_PATH)
    return document


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compute standalone RSI/MACD timing flags for scored candidates"
    )
    parser.add_argument(
        "candidates_path",
        nargs="?",
        default=DEFAULT_CANDIDATES_PATH,
        help="path to scored_candidates.json",
    )
    args = parser.parse_args()

    result = evaluate_timing(args.candidates_path)
    print(f"Wrote {len(result['flags'])} timing flags to {TIMING_FLAGS_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
