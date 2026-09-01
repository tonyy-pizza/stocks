#!/usr/bin/env python3
"""Local paper-trading simulator built on the existing stocks pipeline.

SIMULATION LIMITATION
---------------------
Every simulated fill uses the latest cached/fetched close from market_data.py
plus a flat slippage assumption (worse for both buys and sells). This does not
model a real order book, partial fills, spread changes, or intraday movement.
It is sufficient for validating decision logic and rough strategy performance;
it is not a substitute for testing real broker execution mechanics.

The simulator is intentionally isolated from the manual, real-money workflow.
It never reads or writes data/holdings.json. Its ledger, adapter, sized output,
trade log, and equity curve all have paper-specific filenames.
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import math
import traceback
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import entry_timing
import market_data as md
import position_sizer
import scan_report
import stocks_common as common


DATA_DIR = common.data_dir(md.BASE_DIR)
PAPER_PORTFOLIO_PATH = DATA_DIR / "paper_portfolio.json"
PAPER_TRADE_LOG_PATH = DATA_DIR / "paper_trade_log.jsonl"
PAPER_EQUITY_CURVE_PATH = DATA_DIR / "paper_equity_curve.jsonl"
PAPER_HOLDINGS_ADAPTER_PATH = DATA_DIR / "paper_holdings_adapter.json"
PAPER_SIZED_PATH = DATA_DIR / "paper_sized_candidates.json"
PAPER_EXIT_SIGNALS_PATH = DATA_DIR / "paper_exit_signals.json"

DEFAULT_STARTING_CASH = 100_000.0
DEFAULT_SLIPPAGE = 0.001
DEFAULT_MAX_POSITION_PCT = 0.10
DEFAULT_MAX_NEW_BUYS = 5
ACCOUNT_CURRENCY = "USD"

AUTO_SELL_TRIGGERS = {"thesis_broken", "stop_loss", "thesis_completed"}
EXIT_TRIGGER_PRIORITY = {
    "thesis_broken": 0,
    "stop_loss": 1,
    "thesis_completed": 2,
    "reassess": 3,
}
EXIT_FUNCTION_NAMES = (
    "evaluate_exit_triggers",
    "evaluate_exits",
    "check_exit_triggers",
    "check_exit_trigger",
    "check_exits",
    "evaluate_holdings",
    "evaluate_holding",
    "evaluate_exit",
    "evaluate_position",
)


class UpstreamStageError(RuntimeError):
    """A required signal stage did not produce a usable result."""


def _timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _number(value: Any) -> float | None:
    result = common.num(value)
    return float(result) if result is not None else None


def _round_money(value: float) -> float:
    return round(float(value), 6)


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """Append exactly one compact JSON object and newline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as destination:
        destination.write(json.dumps(
            record, ensure_ascii=False, default=common.json_default,
            separators=(",", ":"),
        ))
        destination.write("\n")


def _action(action: str, ticker: str | None = None, quantity: float = 0,
            simulated_fill_price: float | None = None,
            trigger: str | None = None, reason: str | None = None,
            **extra: Any) -> dict[str, Any]:
    record = {
        "timestamp": _timestamp(),
        "action": action,
        "ticker": ticker,
        "quantity": quantity,
        "simulated_fill_price": simulated_fill_price,
        "trigger": trigger,
        "reason": reason,
    }
    record.update(extra)
    return record


def load_or_initialize_portfolio(
    path: Path = PAPER_PORTFOLIO_PATH,
    starting_cash: float = DEFAULT_STARTING_CASH,
) -> dict[str, Any]:
    """Load the paper ledger, creating it only when it does not exist."""
    starting_cash = float(starting_cash)
    if not math.isfinite(starting_cash) or starting_cash <= 0:
        raise ValueError("starting cash must be a positive finite number")

    if not path.exists():
        portfolio = {
            "cash": starting_cash,
            "starting_cash": starting_cash,
            "positions": [],
            "realized_pnl_log": [],
        }
        common.write_json(portfolio, path)
        return portfolio

    with path.open("r", encoding="utf-8") as source:
        portfolio = json.load(source)
    if not isinstance(portfolio, dict):
        raise ValueError("paper portfolio must be a JSON object")

    cash = _number(portfolio.get("cash"))
    original_cash = _number(portfolio.get("starting_cash"))
    positions = portfolio.get("positions")
    realized = portfolio.get("realized_pnl_log")
    if cash is None or cash < 0:
        raise ValueError("paper portfolio cash must be a non-negative number")
    if original_cash is None or original_cash <= 0:
        raise ValueError("paper portfolio starting_cash must be positive")
    if not isinstance(positions, list) or not isinstance(realized, list):
        raise ValueError("paper portfolio positions and realized_pnl_log must be lists")

    normalized_positions = []
    seen = set()
    for position in positions:
        if not isinstance(position, dict):
            raise ValueError("each paper position must be a JSON object")
        ticker = str(position.get("ticker") or "").strip().upper()
        shares = _number(position.get("shares"))
        entry_price = _number(position.get("entry_price"))
        entry_date = str(position.get("entry_date") or "").strip()
        if not ticker or shares is None or shares <= 0:
            raise ValueError("each paper position needs a ticker and positive shares")
        if entry_price is None or entry_price <= 0 or not entry_date:
            raise ValueError(f"paper position {ticker} needs entry_date and entry_price")
        if ticker in seen:
            raise ValueError(f"duplicate paper position for {ticker}")
        seen.add(ticker)
        normalized_positions.append({
            "ticker": ticker,
            "shares": float(shares),
            "entry_date": entry_date,
            "entry_price": float(entry_price),
        })

    return {
        "cash": float(cash),
        "starting_cash": float(original_cash),
        "positions": normalized_positions,
        "realized_pnl_log": realized,
    }


def paper_positions_to_holdings(portfolio: dict[str, Any]) -> list[dict[str, Any]]:
    """Adapt the paper ledger to position_sizer/holdings_exit holding fields."""
    return [
        {
            "ticker": position["ticker"],
            "shares": position["shares"],
            "cost_basis": position["entry_price"],
            "entry_price": position["entry_price"],
            "entry_date": position["entry_date"],
            "currency": ACCOUNT_CURRENCY,
        }
        for position in portfolio.get("positions") or []
    ]


def _write_paper_holdings_adapter(portfolio: dict[str, Any]) -> list[dict[str, Any]]:
    holdings = paper_positions_to_holdings(portfolio)
    common.write_json({
        "_comment": "Generated from paper_portfolio.json; never the manual holdings file.",
        "holdings": holdings,
    }, PAPER_HOLDINGS_ADAPTER_PATH)
    # position_sizer.load_holdings() narrows each row to a fixed set of fields.
    # Return the same normalized view so a fresh paper sizing file is
    # recognized and does not get needlessly rebuilt - which is what happened
    # when entry_date and entry_price joined the schema and this view did not.
    # shares comes back through float(), so it is floated here too.
    return [
        {
            "ticker": holding["ticker"],
            "shares": float(holding["shares"]),
            "cost_basis": holding["cost_basis"],
            "currency": holding["currency"],
            "entry_date": holding["entry_date"],
            "entry_price": holding["entry_price"],
        }
        for holding in holdings
    ]


@contextmanager
def _paper_sizing_paths():
    """Redirect scan_report's holdings-aware stages away from manual-track files.

    scan_report.run_pipeline() now ends with an exit stage that reads
    holdings.json and writes exit_signals.json through holdings_exit. Left
    alone it would read the real portfolio and overwrite the real exit signals
    on every paper cycle, which is exactly the isolation this module promises
    not to break - so those two paths move to the paper track for the duration
    of the run, alongside sizing.
    """
    holdings_exit = importlib.import_module("holdings_exit")
    originals = {
        "scan_sized": scan_report.SIZED,
        "scan_exits": scan_report.EXIT_SIGNALS,
        "sizer_holdings": position_sizer.HOLDINGS_PATH,
        "sizer_output": position_sizer.OUTPUT_PATH,
        "exit_holdings": holdings_exit.HOLDINGS_PATH,
        "exit_output": holdings_exit.OUTPUT_PATH,
    }
    scan_report.SIZED = PAPER_SIZED_PATH
    scan_report.EXIT_SIGNALS = PAPER_EXIT_SIGNALS_PATH
    position_sizer.HOLDINGS_PATH = PAPER_HOLDINGS_ADAPTER_PATH
    position_sizer.OUTPUT_PATH = PAPER_SIZED_PATH
    holdings_exit.HOLDINGS_PATH = PAPER_HOLDINGS_ADAPTER_PATH
    holdings_exit.OUTPUT_PATH = PAPER_EXIT_SIGNALS_PATH
    try:
        yield
    finally:
        scan_report.SIZED = originals["scan_sized"]
        scan_report.EXIT_SIGNALS = originals["scan_exits"]
        position_sizer.HOLDINGS_PATH = originals["sizer_holdings"]
        position_sizer.OUTPUT_PATH = originals["sizer_output"]
        holdings_exit.HOLDINGS_PATH = originals["exit_holdings"]
        holdings_exit.OUTPUT_PATH = originals["exit_output"]


def _pipeline_args(*, current_equity: float, force: bool, refresh_prices: bool,
                   include_canada: bool, evaluate_limit: int | None,
                   top: int | None, min_composite: float | None,
                   sentiment_top: int, workers: int | None,
                   quiet: bool) -> SimpleNamespace:
    """Arguments consumed by scan_report.run_pipeline()."""
    return SimpleNamespace(
        force=force,
        refresh_prices=refresh_prices,
        include_canada=include_canada,
        evaluate_limit=evaluate_limit,
        account_size=current_equity,
        account_currency=ACCOUNT_CURRENCY,
        top=top,
        min_composite=min_composite,
        sentiment_top=sentiment_top,
        workers=workers,
        quiet=quiet,
        render_only=False,
    )


def _run_scan_pipeline(args: SimpleNamespace,
                       expected_holdings: list[dict[str, Any]]) -> tuple[list, dict]:
    """Run scan_report's orchestration with paper-only sizing inputs/outputs."""
    with _paper_sizing_paths():
        status = scan_report.run_pipeline(args)

        blocking = [
            row for row in status
            if row.get("ok") is False and row.get("required", True)
        ]
        if blocking:
            details = "; ".join(
                f"{row.get('stage')}: {row.get('detail') or row.get('action')}"
                for row in blocking
            )
            raise UpstreamStageError(details)

        universe = common.read_json(scan_report.CANDIDATES) or {}
        scored = common.read_json(scan_report.SCORED) or {}
        if not (universe.get("candidates") or []):
            raise UpstreamStageError("universe_screen produced no candidates")
        if not (scored.get("scored") or []):
            raise UpstreamStageError("stock_evaluator produced no scored candidates")

        sized = common.read_json(PAPER_SIZED_PATH) or {}
        params = sized.get("params") or {}
        sizing_matches = (
            (sized.get("holdings") or []) == expected_holdings
            and params.get("top") == args.top
            and params.get("min_composite") == args.min_composite
            and Path(params.get("holdings_input") or "") == PAPER_HOLDINGS_ADAPTER_PATH
        )
        if not sizing_matches:
            sized = position_sizer.size_shortlist(
                scored_path=scan_report.SCORED,
                clustered_path=scan_report.CLUSTERED,
                holdings_path=PAPER_HOLDINGS_ADAPTER_PATH,
                output_path=PAPER_SIZED_PATH,
                top=args.top,
                min_composite=args.min_composite,
                force_refresh=args.refresh_prices,
                quiet=True,
            )

    if not (sized.get("candidates") or []):
        raise UpstreamStageError("position_sizer produced no paper candidates")
    return status, sized


def _latest_prices(tickers: list[str], force_refresh: bool = False) -> dict[str, float]:
    """Fetch one batched price panel and return its latest usable closes."""
    symbols = sorted({str(ticker).strip().upper() for ticker in tickers if ticker})
    if not symbols:
        return {}
    histories = md.download_prices(
        symbols,
        period="1mo",
        interval="1d",
        cache_key="paper_sim",
        force_refresh=force_refresh,
    )
    prices = {}
    for ticker in symbols:
        rows = histories.get(ticker) or []
        for row in reversed(rows):
            value = _number(row.get("close") if isinstance(row, dict) else None)
            if value is not None and value > 0:
                prices[ticker] = value
                break
    return prices


def _portfolio_equity(portfolio: dict[str, Any], prices: dict[str, float]) -> float:
    """Cash plus marked positions; entry price is a stale fallback if needed."""
    total = float(portfolio["cash"])
    for position in portfolio.get("positions") or []:
        mark = prices.get(position["ticker"], position["entry_price"])
        total += float(position["shares"]) * float(mark)
    return _round_money(total)


def _find_exit_function(module):
    for name in EXIT_FUNCTION_NAMES:
        function = getattr(module, name, None)
        if callable(function):
            return function
    names = ", ".join(EXIT_FUNCTION_NAMES)
    raise UpstreamStageError(
        f"holdings_exit.py has no supported trigger function (expected one of {names})"
    )


@contextmanager
def _paper_exit_paths(module):
    """Redirect conventional holdings globals in holdings_exit, if present."""
    missing = object()
    replacements = {
        "HOLDINGS_PATH": PAPER_HOLDINGS_ADAPTER_PATH,
        "DEFAULT_HOLDINGS_PATH": PAPER_HOLDINGS_ADAPTER_PATH,
        "PORTFOLIO_PATH": PAPER_HOLDINGS_ADAPTER_PATH,
        "OUTPUT_PATH": PAPER_EXIT_SIGNALS_PATH,
        "EXIT_OUTPUT_PATH": PAPER_EXIT_SIGNALS_PATH,
    }
    originals = {name: getattr(module, name, missing) for name in replacements}
    for name, original in originals.items():
        if original is not missing:
            setattr(module, name, replacements[name])
    try:
        yield
    finally:
        for name, original in originals.items():
            if original is missing:
                continue
            setattr(module, name, original)


def _call_with_context(function, context: dict[str, Any],
                       single_position: dict[str, Any] | None = None):
    """Call an upstream exit function without allowing an implicit holdings read."""
    signature = inspect.signature(function)
    args = []
    kwargs = {}
    local_context = dict(context)
    if single_position is not None:
        local_context["position"] = single_position
        local_context["holding"] = single_position

    for parameter in signature.parameters.values():
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            continue
        if parameter.name in local_context:
            value = local_context[parameter.name]
            if parameter.kind == parameter.POSITIONAL_ONLY:
                args.append(value)
            else:
                kwargs[parameter.name] = value
        elif parameter.default is parameter.empty:
            raise UpstreamStageError(
                f"unsupported required holdings_exit parameter: {parameter.name}"
            )
    if not args and not kwargs and signature.parameters:
        raise UpstreamStageError("refusing to let holdings_exit read implicit manual holdings")
    return function(*args, **kwargs)


def _run_exit_logic(portfolio: dict[str, Any], prices: dict[str, float],
                    scored_doc: dict[str, Any], sized_doc: dict[str, Any],
                    timing_doc: dict[str, Any], force_refresh: bool) -> Any:
    """Import and invoke holdings_exit's trigger logic against paper positions."""
    try:
        module = importlib.import_module("holdings_exit")
    except ModuleNotFoundError as exc:
        if exc.name == "holdings_exit":
            raise UpstreamStageError("holdings_exit.py is not available") from exc
        raise

    function = _find_exit_function(module)
    positions = paper_positions_to_holdings(portfolio)
    for position in positions:
        position["current_price"] = prices.get(position["ticker"])

    scored_records = {
        str(record.get("ticker") or "").strip().upper(): record
        for record in scored_doc.get("scored") or []
        if record.get("ticker")
    }
    context = {
        "positions": positions,
        "holdings": positions,
        "paper_positions": positions,
        "portfolio": {"positions": positions, "holdings": positions},
        "holdings_path": PAPER_HOLDINGS_ADAPTER_PATH,
        "portfolio_path": PAPER_HOLDINGS_ADAPTER_PATH,
        "output_path": PAPER_EXIT_SIGNALS_PATH,
        "scored_path": scan_report.SCORED,
        "sized_path": PAPER_SIZED_PATH,
        "timing_path": entry_timing.TIMING_FLAGS_PATH,
        "scored_doc": scored_doc,
        "scored_records": scored_records,
        "sized_doc": sized_doc,
        "timing_doc": timing_doc,
        "timing_flags": timing_doc.get("flags") or {},
        "price_history": prices,
        "prices": prices,
        "quiet": True,
        "force_refresh": force_refresh,
    }

    parameters = inspect.signature(function).parameters
    with _paper_exit_paths(module):
        if ("position" in parameters or "holding" in parameters) and not any(
            name in parameters for name in ("positions", "holdings", "paper_positions")
        ):
            results = []
            for position in positions:
                result = _call_with_context(
                    function, context, single_position=position
                )
                ticker = position["ticker"]
                if isinstance(result, str):
                    result = {"ticker": ticker, "trigger": result}
                elif isinstance(result, dict) and not (
                    result.get("ticker") or result.get("symbol")
                ):
                    result = {"ticker": ticker, **result}
                elif isinstance(result, (list, tuple)):
                    result = [
                        ({"ticker": ticker, **item}
                         if isinstance(item, dict)
                         and not (item.get("ticker") or item.get("symbol"))
                         else item)
                        for item in result
                    ]
                    results.extend(result)
                    continue
                results.append(result)
            return results
        return _call_with_context(function, context)


def _normalize_trigger_name(value: Any) -> str | None:
    if value is None:
        return None
    name = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    return name or None


def _normalize_exit_results(result: Any) -> list[dict[str, Any]]:
    """Normalize common holdings_exit return envelopes without recreating logic."""
    if result is None:
        return []
    if isinstance(result, tuple):
        result = result[0] if result else None
    if isinstance(result, dict):
        # "evaluated" is the envelope holdings_exit actually writes. Without it
        # the fallback below walked the document's own metadata keys and read
        # generated_at, counts and evaluated as if they were tickers, so no
        # real ticker ever produced a signal and nothing was ever sold.
        for key in ("evaluated", "triggers", "exits", "actions", "reviews", "results"):
            if isinstance(result.get(key), list):
                result = result[key]
                break
        else:
            if any(key in result for key in ("ticker", "symbol", "trigger", "verdict")):
                result = [result]
            else:
                expanded = []
                for ticker, value in result.items():
                    if isinstance(value, dict):
                        expanded.append({"ticker": ticker, **value})
                    elif isinstance(value, (list, tuple, set)):
                        expanded.extend({"ticker": ticker, "trigger": item} for item in value)
                    else:
                        expanded.append({"ticker": ticker, "trigger": value})
                result = expanded
    if not isinstance(result, list):
        result = [result]

    normalized = []
    for item in result:
        if item is None:
            continue
        if isinstance(item, str):
            continue
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker") or item.get("symbol") or "").strip().upper()
        trigger_values = item.get("triggers")
        if not isinstance(trigger_values, (list, tuple, set)):
            trigger = (
                item.get("trigger") or item.get("exit_trigger")
                or item.get("verdict") or item.get("action")
            )
            if trigger is None:
                trigger_values = [
                    name for name in (*AUTO_SELL_TRIGGERS, "reassess")
                    if item.get(name) is True
                ]
            else:
                trigger_values = [trigger]
        reason = item.get("reason") or item.get("detail") or item.get("message")
        if reason is None and isinstance(item.get("reasons"), list):
            reason = "; ".join(str(value) for value in item["reasons"])
        # holdings_exit records its reasons per trigger rather than per row.
        per_trigger = item.get("trigger_reasons")
        per_trigger = per_trigger if isinstance(per_trigger, dict) else {}
        for trigger in trigger_values:
            trigger_name = _normalize_trigger_name(trigger)
            if ticker and trigger_name:
                specific = per_trigger.get(trigger_name) or per_trigger.get(trigger)
                if isinstance(specific, (list, tuple)):
                    specific = "; ".join(str(value) for value in specific)
                normalized.append({
                    "ticker": ticker,
                    "trigger": trigger_name,
                    "reason": str(specific or reason or trigger_name),
                })
    return normalized


def _exit_signals_by_ticker(result: Any) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for signal in _normalize_exit_results(result):
        grouped.setdefault(signal["ticker"], []).append(signal)
    for signals in grouped.values():
        signals.sort(key=lambda row: EXIT_TRIGGER_PRIORITY.get(row["trigger"], 99))
    return grouped


def _target_weight(candidate: dict[str, Any]) -> float | None:
    sizing = candidate.get("sizing") or {}
    percent = _number(sizing.get("adjusted_high_pct"))
    if percent is None:
        percent = _number(sizing.get("base_high_pct"))
    if percent is None or percent <= 0:
        return None
    return percent / 100.0


def _persist_actions(portfolio: dict[str, Any], actions: list[dict[str, Any]]) -> None:
    common.write_json(portfolio, PAPER_PORTFOLIO_PATH)
    for record in actions:
        _append_jsonl(PAPER_TRADE_LOG_PATH, record)


def _append_equity(total_equity: float) -> None:
    _append_jsonl(PAPER_EQUITY_CURVE_PATH, {
        "timestamp": _timestamp(),
        "total_portfolio_value": _round_money(total_equity),
    })


def _skipped_run(portfolio: dict[str, Any], reason: str,
                 prices: dict[str, float] | None = None) -> dict[str, Any]:
    actions = [_action("run_skipped", reason=reason)]
    _persist_actions(portfolio, actions)
    equity = _portfolio_equity(portfolio, prices or {})
    _append_equity(equity)
    return {
        "ok": False,
        "status": "skipped",
        "actions_taken": 0,
        "trades": 0,
        "reassessments": 0,
        "skipped": 1,
        "total_equity": equity,
        "cash": portfolio["cash"],
        "reason": reason,
    }


def _run_paper_cycle(
    *,
    starting_cash: float,
    slippage: float,
    max_position_pct: float,
    max_new_buys: int,
    force: bool,
    refresh_prices: bool,
    include_canada: bool,
    evaluate_limit: int | None,
    top: int | None,
    min_composite: float | None,
    sentiment_top: int,
    workers: int | None,
    quiet: bool,
) -> dict[str, Any]:
    if not 0 <= slippage < 1:
        raise ValueError("slippage must be a fraction from 0 up to (but not including) 1")
    if not 0 < max_position_pct <= 1:
        raise ValueError("max position percentage must be a fraction in (0, 1]")
    if max_new_buys < 0:
        raise ValueError("max new buys must be zero or greater")

    portfolio = load_or_initialize_portfolio(starting_cash=starting_cash)
    initial_prices = _latest_prices(
        [position["ticker"] for position in portfolio["positions"]],
        force_refresh=refresh_prices,
    )
    initial_equity = _portfolio_equity(portfolio, initial_prices)
    adapted_holdings = _write_paper_holdings_adapter(portfolio)
    args = _pipeline_args(
        current_equity=initial_equity,
        force=force,
        refresh_prices=refresh_prices,
        include_canada=include_canada,
        evaluate_limit=evaluate_limit,
        top=top,
        min_composite=min_composite,
        sentiment_top=sentiment_top,
        workers=workers,
        quiet=quiet,
    )

    try:
        _, sized_doc = _run_scan_pipeline(args, adapted_holdings)
        timing_doc = entry_timing.evaluate_timing(scan_report.SCORED)
        if not isinstance(timing_doc.get("flags"), dict):
            raise UpstreamStageError("entry_timing produced no timing flags")

        scored_doc = common.read_json(scan_report.SCORED) or {}
        candidates = sized_doc.get("candidates") or []
        reversal_tickers = [
            str(candidate.get("ticker") or "").strip().upper()
            for candidate in candidates
            if (timing_doc["flags"].get(
                str(candidate.get("ticker") or "").strip().upper()
            ) == "reversal_signal")
        ]
        held_tickers = [position["ticker"] for position in portfolio["positions"]]
        prices = _latest_prices(
            held_tickers + reversal_tickers,
            force_refresh=refresh_prices,
        )
        exit_result = _run_exit_logic(
            portfolio, prices, scored_doc, sized_doc, timing_doc, refresh_prices
        )
        exit_signals = _exit_signals_by_ticker(exit_result)
    except UpstreamStageError as exc:
        return _skipped_run(portfolio, str(exc), initial_prices)

    actions: list[dict[str, Any]] = []
    sold_this_run: set[str] = set()
    trades = 0
    reassessments = 0
    skipped = 0

    # Exits execute before entries so buys see the portfolio's current cash and
    # equity after any thesis-driven sales.
    remaining_positions = []
    for position in portfolio["positions"]:
        ticker = position["ticker"]
        signals = exit_signals.get(ticker) or []
        actionable = next(
            (signal for signal in signals if signal["trigger"] in AUTO_SELL_TRIGGERS),
            None,
        )
        reassess = next(
            (signal for signal in signals if signal["trigger"] == "reassess"),
            None,
        )

        if actionable is None:
            remaining_positions.append(position)
            if reassess is not None:
                reassessments += 1
                actions.append(_action(
                    "reassess",
                    ticker=ticker,
                    quantity=position["shares"],
                    trigger="reassess",
                    reason=reassess["reason"],
                ))
            continue

        quote = prices.get(ticker)
        if quote is None:
            remaining_positions.append(position)
            skipped += 1
            actions.append(_action(
                "sell_skipped",
                ticker=ticker,
                quantity=position["shares"],
                trigger=actionable["trigger"],
                reason=f"{actionable['reason']}; latest price unavailable",
            ))
            continue

        fill = quote * (1.0 - slippage)
        quantity = float(position["shares"])
        proceeds = quantity * fill
        realized_pnl = quantity * (fill - float(position["entry_price"]))
        portfolio["cash"] = _round_money(float(portfolio["cash"]) + proceeds)
        realized_entry = {
            "timestamp": _timestamp(),
            "ticker": ticker,
            "shares": quantity,
            "entry_date": position["entry_date"],
            "entry_price": position["entry_price"],
            "exit_date": datetime.now().astimezone().date().isoformat(),
            "exit_price": _round_money(fill),
            "proceeds": _round_money(proceeds),
            "realized_pnl": _round_money(realized_pnl),
            "trigger": actionable["trigger"],
            "reason": actionable["reason"],
        }
        portfolio["realized_pnl_log"].append(realized_entry)
        sold_this_run.add(ticker)
        trades += 1
        actions.append(_action(
            "sell",
            ticker=ticker,
            quantity=quantity,
            simulated_fill_price=_round_money(fill),
            trigger=actionable["trigger"],
            reason=actionable["reason"],
            market_price=_round_money(quote),
            slippage=slippage,
            proceeds=_round_money(proceeds),
            realized_pnl=_round_money(realized_pnl),
        ))
    portfolio["positions"] = remaining_positions

    buys = 0
    for candidate in candidates:
        ticker = str(candidate.get("ticker") or "").strip().upper()
        if not ticker or timing_doc["flags"].get(ticker) != "reversal_signal":
            continue

        if ticker in sold_this_run:
            skipped += 1
            actions.append(_action(
                "buy_skipped", ticker=ticker, trigger="reversal_signal",
                reason="sold by an exit trigger in this run; no same-cycle re-entry",
            ))
            continue

        target_weight = _target_weight(candidate)
        existing = next(
            (position for position in portfolio["positions"] if position["ticker"] == ticker),
            None,
        )
        current_equity = _portfolio_equity(portfolio, prices)
        if existing is not None:
            mark = prices.get(ticker, existing["entry_price"])
            current_weight = existing["shares"] * mark / current_equity if current_equity else 0
            reason = "already held; paper simulator only opens new positions"
            if target_weight is not None and current_weight >= target_weight:
                reason = (
                    f"already held at {current_weight:.2%}, at or above "
                    f"the {target_weight:.2%} target"
                )
            skipped += 1
            actions.append(_action(
                "buy_skipped", ticker=ticker, trigger="reversal_signal", reason=reason,
                current_weight=round(current_weight, 6), target_weight=target_weight,
            ))
            continue

        if buys >= max_new_buys:
            skipped += 1
            actions.append(_action(
                "buy_skipped", ticker=ticker, trigger="reversal_signal",
                reason=f"maximum of {max_new_buys} new buys reached for this run",
            ))
            continue
        if target_weight is None:
            skipped += 1
            actions.append(_action(
                "buy_skipped", ticker=ticker, trigger="reversal_signal",
                reason="position_sizer returned no usable target percentage",
            ))
            continue

        quote_currency = str(candidate.get("quote_currency") or ACCOUNT_CURRENCY).upper()
        if quote_currency != ACCOUNT_CURRENCY:
            skipped += 1
            actions.append(_action(
                "buy_skipped", ticker=ticker, trigger="reversal_signal",
                reason=(f"paper cash is {ACCOUNT_CURRENCY}; candidate quote currency "
                        f"is {quote_currency}"),
            ))
            continue

        quote = prices.get(ticker)
        if quote is None:
            skipped += 1
            actions.append(_action(
                "buy_skipped", ticker=ticker, trigger="reversal_signal",
                reason="latest price unavailable",
            ))
            continue

        capped_weight = min(target_weight, max_position_pct)
        desired_cost = current_equity * capped_weight
        if desired_cost > float(portfolio["cash"]) + 1e-9:
            skipped += 1
            actions.append(_action(
                "buy_skipped", ticker=ticker, trigger="reversal_signal",
                reason=(f"insufficient paper cash: need {desired_cost:.2f}, "
                        f"have {portfolio['cash']:.2f}"),
                target_weight=target_weight,
                capped_weight=capped_weight,
            ))
            continue

        fill = quote * (1.0 + slippage)
        # Fractional shares keep the simulator focused on signal and sizing
        # behavior rather than introducing an arbitrary whole-share rule.
        quantity = math.floor((desired_cost / fill) * 1_000_000) / 1_000_000
        cost = quantity * fill
        if quantity <= 0 or cost <= 0:
            skipped += 1
            actions.append(_action(
                "buy_skipped", ticker=ticker, trigger="reversal_signal",
                reason="target allocation is too small to produce a positive quantity",
            ))
            continue

        portfolio["cash"] = _round_money(float(portfolio["cash"]) - cost)
        portfolio["positions"].append({
            "ticker": ticker,
            "shares": quantity,
            "entry_date": datetime.now().astimezone().date().isoformat(),
            "entry_price": _round_money(fill),
        })
        buys += 1
        trades += 1
        cap_note = (
            f"; capped from {target_weight:.2%} to {capped_weight:.2%}"
            if capped_weight < target_weight else ""
        )
        actions.append(_action(
            "buy",
            ticker=ticker,
            quantity=quantity,
            simulated_fill_price=_round_money(fill),
            trigger="reversal_signal",
            reason=f"entry timing reversal; position_sizer target {target_weight:.2%}{cap_note}",
            market_price=_round_money(quote),
            slippage=slippage,
            cost=_round_money(cost),
            target_weight=target_weight,
            capped_weight=capped_weight,
        ))

    _persist_actions(portfolio, actions)
    final_equity = _portfolio_equity(portfolio, prices)
    _append_equity(final_equity)
    return {
        "ok": True,
        "status": "completed",
        "actions_taken": trades + reassessments,
        "trades": trades,
        "buys": buys,
        "sells": len(sold_this_run),
        "reassessments": reassessments,
        "skipped": skipped,
        "total_equity": final_equity,
        "cash": portfolio["cash"],
    }


def run_paper_cycle(
    *,
    starting_cash: float = DEFAULT_STARTING_CASH,
    slippage: float = DEFAULT_SLIPPAGE,
    max_position_pct: float = DEFAULT_MAX_POSITION_PCT,
    max_new_buys: int = DEFAULT_MAX_NEW_BUYS,
    force: bool = False,
    refresh_prices: bool = False,
    include_canada: bool = False,
    evaluate_limit: int | None = None,
    top: int | None = None,
    min_composite: float | None = None,
    sentiment_top: int = scan_report.SENTIMENT_TOP,
    workers: int | None = None,
    quiet: bool = True,
) -> dict[str, Any]:
    """Run one guarded paper cycle; unexpected failures are logged, not hidden."""
    try:
        return _run_paper_cycle(
            starting_cash=starting_cash,
            slippage=slippage,
            max_position_pct=max_position_pct,
            max_new_buys=max_new_buys,
            force=force,
            refresh_prices=refresh_prices,
            include_canada=include_canada,
            evaluate_limit=evaluate_limit,
            top=top,
            min_composite=min_composite,
            sentiment_top=sentiment_top,
            workers=workers,
            quiet=quiet,
        )
    except Exception as exc:  # noqa: BLE001 - unattended top-level safety net
        reason = f"{type(exc).__name__}: {exc}"
        failure = _action(
            "run_failed",
            reason=reason,
            traceback=traceback.format_exc(limit=12),
        )
        try:
            _append_jsonl(PAPER_TRADE_LOG_PATH, failure)
        except Exception:
            pass

        cash = None
        equity = None
        try:
            portfolio = load_or_initialize_portfolio(starting_cash=starting_cash)
            cash = portfolio["cash"]
            equity = _portfolio_equity(portfolio, {})
            _append_equity(equity)
        except Exception:
            pass
        return {
            "ok": False,
            "status": "failed",
            "actions_taken": 0,
            "trades": 0,
            "reassessments": 0,
            "skipped": 0,
            "total_equity": equity,
            "cash": cash,
            "reason": reason,
        }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one isolated local paper-trading cycle."
    )
    parser.add_argument("--starting-cash", type=float, default=DEFAULT_STARTING_CASH,
                        help="initial cash when the paper ledger is first created")
    parser.add_argument("--slippage", type=float, default=DEFAULT_SLIPPAGE,
                        help="fraction applied against fills (default 0.001 = 0.1%%)")
    parser.add_argument("--max-position-pct", type=float,
                        default=DEFAULT_MAX_POSITION_PCT,
                        help="maximum fraction of current equity per new buy")
    parser.add_argument("--max-new-buys", type=int, default=DEFAULT_MAX_NEW_BUYS,
                        help="maximum new positions opened in one run")
    parser.add_argument("--force", action="store_true",
                        help="force scan stages even when outputs are fresh")
    parser.add_argument("--refresh-prices", action="store_true",
                        help="bypass the market_data price cache")
    parser.add_argument("--include-canada", action="store_true")
    parser.add_argument("--evaluate-limit", type=int, metavar="N")
    parser.add_argument("--top", type=int, metavar="N")
    parser.add_argument("--min-composite", type=float, metavar="SCORE")
    parser.add_argument("--sentiment-top", type=int,
                        default=scan_report.SENTIMENT_TOP, metavar="N")
    parser.add_argument("--workers", type=int, metavar="N")
    parser.add_argument("--show-progress", action="store_true",
                        help="show the existing scan stages while they run")
    args = parser.parse_args(argv)

    result = run_paper_cycle(
        starting_cash=args.starting_cash,
        slippage=args.slippage,
        max_position_pct=args.max_position_pct,
        max_new_buys=args.max_new_buys,
        force=args.force,
        refresh_prices=args.refresh_prices,
        include_canada=args.include_canada,
        evaluate_limit=args.evaluate_limit,
        top=args.top,
        min_composite=args.min_composite,
        sentiment_top=args.sentiment_top,
        workers=args.workers,
        quiet=not args.show_progress,
    )

    equity = "unavailable" if result["total_equity"] is None else f"${result['total_equity']:,.2f}"
    cash = "unavailable" if result["cash"] is None else f"${result['cash']:,.2f}"
    print(
        f"Paper cycle {result['status']}: {result['actions_taken']} action(s), "
        f"equity {equity}, cash {cash}."
    )
    if result.get("reason"):
        print(f"Reason: {result['reason']}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
