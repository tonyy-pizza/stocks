"""Display conventions, matched to the pipeline's rather than invented.

scan_report.py and stock_evaluator.py agree on how a 0-10 score is coloured
(>= 7.5 green, >= 5.0 yellow, below that red) and on what the compact flag block
says. Those are the conventions here too - the same score has to read the same
way whether the person is looking at the terminal report or this dashboard.

Only the medium changes: ANSI escapes become CSS, and flag_cells()'s glyph
column becomes short text labels a table can sort on.
"""

from __future__ import annotations

import math
from typing import Optional

# scan_report.colour()'s thresholds, as colours a browser understands. The
# terminal palette is bright green / yellow / red; these are the readable
# equivalents on Streamlit's light and dark backgrounds.
GREEN = "#1a7f37"
YELLOW = "#9a6700"
RED = "#cf222e"
DIM = "#8b949e"

GOOD = 7.5      # stock_evaluator.colour: >= 7.5 is green and bold
FAIR = 5.0      # >= 5.0 is yellow, below is red


def num(value) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) else out


def score_colour(value) -> str:
    """scan_report.colour()'s thresholds, as a CSS colour."""
    value = num(value)
    if value is None:
        return DIM
    if value >= GOOD:
        return GREEN
    if value >= FAIR:
        return YELLOW
    return RED


def score_css(value) -> str:
    """A pandas Styler cell rule for a 0-10 score."""
    value = num(value)
    if value is None:
        return f"color: {DIM}"
    weight = "; font-weight: 600" if value >= GOOD else ""
    return f"color: {score_colour(value)}{weight}"


def score_html(value, digits: int = 2) -> str:
    value = num(value)
    if value is None:
        return f'<span style="color:{DIM}">n/a</span>'
    weight = "font-weight:600;" if value >= GOOD else ""
    return f'<span style="color:{score_colour(value)};{weight}">{value:.{digits}f}</span>'


def bar(value, width: int = 14) -> str:
    """stock_evaluator.bar(), same fill characters."""
    value = num(value)
    value = max(0.0, min(10.0, value or 0.0))
    filled = int(round((value / 10) * width))
    return "█" * filled + "░" * (width - filled)


# ── flag semantics, from scan_report.flag_cells() ─────────────────────────

# The conviction verdicts position_sizer assigns and what scan_report's legend
# calls them. Same words, so the two reports can be read side by side.
CONVICTION_LABELS = {
    "disconnect_supported": ("disconnect+", GREEN,
                             "price low, trend intact, public read agrees"),
    "disconnect_contested": ("disconnect?", YELLOW,
                             "price low and trend intact, but sentiment contests it"),
    "disconnect_unverified": ("disconnect·", GREEN,
                              "price low, trend intact, no sentiment on file"),
    "trap_confirmed": ("value-trap!", RED,
                       "trend and sentiment both negative"),
    "trap_contested": ("trap-hype", RED,
                       "trend down but sentiment high"),
    "trap_unverified": ("value-trap", RED,
                        "trend deteriorating, no sentiment on file"),
    "not_applicable": ("·", DIM, "divergence pattern is neutral"),
}

# The sizing consequence of each verdict, from position_sizer.CONVICTION_SCALE.
# Shown next to the label so a cut has a visible cause.
CONVICTION_HELP = (
    "Sentiment crossed with the price/fundamentals divergence, from "
    "position_sizer.py. disconnect+ / disconnect· keep full size; "
    "disconnect? costs 25%; value-trap 35-50%."
)


def conviction_label(candidate: dict) -> str:
    verdict = ((candidate.get("sizing") or {}).get("conviction") or "not_applicable")
    return CONVICTION_LABELS.get(verdict, ("·", DIM, ""))[0]


def conviction_colour(verdict: Optional[str]) -> str:
    return CONVICTION_LABELS.get(verdict or "not_applicable", ("", DIM, ""))[1]


def liquidity_label(candidate: dict) -> str:
    """flag_cells()'s liquidity cell: a flag means thin, not absent."""
    return "✗ thin" if candidate.get("liquidity_flag") else "✓ ok"


TREND_LABELS = {
    "improving": "✓ improving",
    "flat": "✓ flat",
    "deteriorating": "✗ declining",
    "mixed": "~ mixed",
}

TREND_HELP = (
    "Direction of multi-year ROA, from stock_evaluator.roa_trend: the share of "
    "year-over-year steps that improved plus the overall first-to-last change. "
    "'·' means too little history to say, which is not the same as a decline. "
    "This replaced roa_trend_consistent, which demanded a non-decreasing ROA in "
    "every single year and so read worse the more history a company had."
)


def trend_label(candidate: dict) -> str:
    """flag_cells()'s trend cell, on the graded roa_trend.

    The dot still means insufficient history rather than a failure, and there
    is now a fourth state: 'mixed', where the endpoints and the year-to-year
    steps disagree because one spike or trough is doing the work. Calling that
    either a rise or a decline would be overstating what the series says.
    """
    return TREND_LABELS.get(candidate.get("roa_trend"), "· trend")


def coverage_label(value) -> str:
    """Share of scoring inputs that were actually available, as a percentage."""
    value = num(value)
    return "n/a" if value is None else f"{value * 100:.0f}%"


# stock_evaluator.LOW_COVERAGE - below this a composite is mostly score()'s
# neutral 5.0 default rather than a reading of the company.
LOW_COVERAGE = 0.60

COVERAGE_HELP = (
    "Share of the 24-25 scoring inputs this ticker actually had. Missing inputs "
    "score a neutral 5.0, so a composite built from a handful of them is closer "
    "to a default than to a judgement. Below "
    f"{LOW_COVERAGE * 100:.0f}% stock_evaluator raises a 'thin data' risk flag, "
    "which costs the name its top position-size band."
)


def coverage_css(value) -> str:
    """Amber below the thin-data threshold, dim when unknown."""
    value = num(value)
    if value is None:
        return f"color: {DIM}"
    if value < LOW_COVERAGE:
        return f"color: {YELLOW}"
    return ""


DIVERGENCE_LABELS = {
    "price_disconnect": "price disconnect",
    "trend_confirms_decline": "trend confirms decline",
    "neutral": "neutral",
}


# ── formatting ────────────────────────────────────────────────────────────

def pct(value, digits: int = 1) -> str:
    value = num(value)
    return "n/a" if value is None else f"{value:.{digits}f}%"


def ratio(value, digits: int = 2) -> str:
    value = num(value)
    return "n/a" if value is None else f"{value:.{digits}f}"


def money(value) -> str:
    """stock_evaluator's compact money format."""
    value = num(value)
    if value is None:
        return "n/a"
    for limit, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= limit:
            return f"{value / limit:.2f}{suffix}"
    return f"{value:.2f}"


def guide_headline(guide: Optional[str]) -> str:
    """The first clause of a position_guidance string.

    "Core: 3%-5%; up to 8% with diversification." -> "Core: 3%-5%", which is
    what scan_report prints in its candidate rows.
    """
    if not guide:
        return "n/a"
    return guide.split(";")[0].strip()
