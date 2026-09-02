r"""Holdings - holdings.json as a table, an editor for it, and the scan's exit review.

Two tabs over one file. **Positions** is the read-only view it has always been:
what the pipeline would act on, book value per currency, and position_sizer's
exit review beside it. **Edit** is the same file as an editable table, because
holdings.json is not pipeline output - it is the hand-maintained input
position_sizer.py and holdings_exit.py both read, and a text editor was the
only way to change it.

The edit tab is the one place in this whole app that writes anything. What it
writes is holdings.json and nothing else: paper_portfolio.json is paper_sim.py's
separate simulated ledger and is never touched here. The schema, the validation
and the backup all live in sv/holdings_store.py; this file is the tab around
them.

Current price and P&L are read from market_data's own cache and never fetched
on a page load - the dashboard's rule everywhere else, and the reason there is
a Refresh prices button rather than a spinner on arrival.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from .. import data_loader as dl
from .. import display as d
from .. import holdings_store as store
from ..pipeline import PIPELINE

VERDICT_COLOURS = {
    "exit_review": d.RED,
    "watch": d.YELLOW,
    "hold": d.GREEN,
    "unavailable": d.DIM,
}


def _verdict_css(value):
    return f"color: {VERDICT_COLOURS.get(value, d.DIM)}"


def _cost_basis_by_currency(rows, scan):
    """Cost basis totalled per currency, never across them.

    A single total over a mix of TSX and US names would be adding CAD to USD, so
    each currency is totalled separately. The currency comes from the holding's
    own `currency` field when it declares one, else from the scan's
    quote_currency, else from a Canadian ticker suffix - and stays "unknown"
    when none of those can say, rather than being folded into a total.
    """
    totals = {}
    for row in rows:
        shares, cost = row.get("shares"), row.get("cost_basis")
        currency = dl.infer_currency(row["ticker"], scan, row.get("currency")) or "unknown"
        entry = totals.setdefault(currency, {"value": 0.0, "names": [],
                                             "incomplete": []})
        entry["names"].append(row["ticker"])
        if shares is None or cost is None:
            entry["incomplete"].append(row["ticker"])
        else:
            entry["value"] += shares * cost
    return totals


def render(scan):
    st.subheader("Holdings")
    positions_tab, edit_tab = st.tabs(["Positions", "Edit"])
    with positions_tab:
        _render_positions(scan)
    with edit_tab:
        _render_editor(scan)


def _render_positions(scan):
    loaded = scan.holdings
    if not loaded.exists:
        st.warning(
            f"No holdings.json in {scan.directory}.\n\n"
            f"position_sizer.py writes a template there on its first run. Until it "
            f"has real positions in it, sizing is not correlation-adjusted at all — "
            f"which is the honest answer rather than assuming a correlation of zero.")
        if loaded.error and loaded.error != "not found":
            st.caption(f"({loaded.error})")
        return

    kept, ignored = dl.holding_entries(loaded.document)
    comment = (loaded.document or {}).get("_comment")

    st.caption(f"`{loaded.path}`  ·  {loaded.age_text}  ·  read-only here — "
               f"the **Edit** tab writes this file")

    if not kept:
        st.info(
            "holdings.json has no real positions in it — every entry is the example "
            "the template ships with, and the pipeline ignores those. Edit the file "
            "with your own positions and re-run scan_report.py to get "
            "correlation-adjusted sizing.")
        if comment:
            st.caption(comment)
        if ignored:
            st.dataframe(pd.DataFrame(ignored), width="stretch", hide_index=True)
        return

    rows = []
    for row in kept:
        ticker = row["ticker"]
        currency = dl.infer_currency(ticker, scan, row.get("currency"))
        shares, cost = row.get("shares"), row.get("cost_basis")
        candidate = next((c for c in scan.candidates if c.get("ticker") == ticker), None)
        rows.append({
            "Ticker": ticker,
            "Name": (candidate or {}).get("name"),
            "Sector": (candidate or {}).get("sector"),
            "Shares": shares,
            "Cost basis": cost,
            "Book value": None if (shares is None or cost is None) else shares * cost,
            "Cur": currency or "?",
            "In this scan": candidate is not None,
        })
    frame = pd.DataFrame(rows)
    st.dataframe(
        frame.style.format({"Shares": lambda v: "n/a" if pd.isna(v) else f"{v:g}",
                            "Cost basis": lambda v: "n/a" if pd.isna(v) else f"{v:,.2f}",
                            "Book value": lambda v: "n/a" if pd.isna(v) else f"{v:,.2f}"}),
        width="stretch", hide_index=True,
        column_config={
            "Cur": st.column_config.Column(
                "Cur", help="Inferred: holdings.json records no currency. Taken from "
                            "the scan's quote_currency where it knows the name, else "
                            "from the Yahoo suffix. '?' means neither applied."),
            "Book value": st.column_config.Column(
                help="shares × cost basis, in that listing's own currency"),
            "In this scan": st.column_config.CheckboxColumn(
                help="Whether this scan scored the holding, which is what the exit "
                     "review below needs"),
        })

    totals = _cost_basis_by_currency(kept, scan)
    st.markdown("**Book value at cost**")
    columns = st.columns(max(1, len(totals)))
    for column, (currency, entry) in zip(columns, sorted(totals.items())):
        suffix = ""
        if entry["incomplete"]:
            suffix = f" (excludes {', '.join(entry['incomplete'])} — missing shares/cost)"
        column.metric(f"{currency}", f"{entry['value']:,.2f}",
                      help=f"{', '.join(sorted(entry['names']))}{suffix}")
    if len(totals) > 1:
        st.warning(
            "**Mixed currencies — these are not added together.** " +
            "; ".join(f"{cur}: {', '.join(sorted(e['names']))}"
                      for cur, e in sorted(totals.items())) +
            ". A single portfolio total would need an FX rate, and the pipeline does "
            "not carry one.")
    if "unknown" in totals or "?" in totals:
        st.caption("'unknown' currency: the name is not in this scan and its suffix "
                   "does not identify a Canadian listing, so its currency was not "
                   "inferred.")

    if ignored:
        with st.expander(f"{len(ignored)} entry/entries the pipeline ignores"):
            st.caption("Entries marked _example, or without a ticker, are skipped "
                       "everywhere in the pipeline. stock_view skips them too.")
            st.dataframe(pd.DataFrame(ignored), width="stretch", hide_index=True)

    # ── exit review ───────────────────────────────────────────────────────
    reviews = scan.holdings_review
    st.divider()
    st.markdown("### Exit review")
    if not reviews:
        st.caption(
            "No exit review in this scan. position_sizer.py re-scores every holding "
            "through Stage 1 on each run unless it was given --no-exit-review.")
        return

    st.caption("position_sizer.py re-scores each holding through the same Stage 1 "
               "scoring a candidate gets, so a thesis that has quietly stopped being "
               "true shows up beside the new ideas.")
    order = {"exit_review": 0, "watch": 1, "unavailable": 2, "hold": 3}
    rows = []
    for review in sorted(reviews, key=lambda r: order.get(r.get("verdict"), 9)):
        reasons = review.get("reasons") or []
        rows.append({
            "Ticker": review.get("ticker"),
            "Verdict": review.get("verdict"),
            "Composite": d.num(review.get("composite")),
            "Δ since last scan": d.num(review.get("composite_delta")),
            "Rating": review.get("rating"),
            "ROA trend": review.get("roa_trend"),
            "Debt trend": review.get("debt_trend"),
            "Data": d.num(review.get("data_coverage")),
            "Why": reasons[0] if reasons else "",
        })
    review_frame = pd.DataFrame(rows)
    st.dataframe(
        review_frame.style
        .map(d.score_css, subset=["Composite"])
        .map(_verdict_css, subset=["Verdict"])
        .map(d.coverage_css, subset=["Data"])
        .format({"Composite": lambda v: "n/a" if pd.isna(v) else f"{v:.2f}",
                 "Δ since last scan": lambda v: "—" if pd.isna(v) else f"{v:+.2f}",
                 "Data": lambda v: "n/a" if pd.isna(v) else f"{v * 100:.0f}%"}),
        width="stretch", hide_index=True,
        column_config={
            "ROA trend": st.column_config.Column("ROA trend", help=d.TREND_HELP),
            "Data": st.column_config.Column("Data", help=d.COVERAGE_HELP),
        })

    flagged = [r for r in reviews if r.get("verdict") == "exit_review"]
    if flagged:
        st.error(f"{len(flagged)} holding(s) flagged for exit review: "
                 + ", ".join(r["ticker"] for r in flagged))

    st.divider()
    st.caption(
        "holdings.json is the one canonical file the rest of the pipeline reads. "
        "Change it in the **Edit** tab (or by hand), then re-run `py scan_report.py` "
        "for sizing and exit signals that reflect it.")


# ─────────────────────────────────────────────────────────────────────────
# EDIT TAB
# ─────────────────────────────────────────────────────────────────────────

# Editor column -> the schema field it writes. The read-only columns below are
# not in this map because they are derived, not stored: nothing computed from a
# price ever goes into holdings.json.
_EDITOR_COLUMNS = {
    "Ticker": "ticker",
    "Shares": "shares",
    "Cost basis": "cost_basis",
    "Cur": "currency",
    "Entry date": "entry_date",
    "Entry price": "entry_price",
}
_DERIVED_COLUMNS = ("Price", "P&L %", "Market value")
_ALL_COLUMNS = tuple(_EDITOR_COLUMNS) + _DERIVED_COLUMNS

# Short window: this is a "what is it worth now" lookup, not a chart. It is
# also the key market_data.get_price_history() writes under, so the button and
# the cache-only read below are talking about the same cache entry.
PRICE_PERIOD, PRICE_INTERVAL = "5d", "1d"

_EPOCH_KEY = "holdings_edit_epoch"       # bumped to re-seed the table from disk
_STAMP_KEY = "holdings_file_stamp"       # the file as it was when this table loaded
_RESULT_KEY = "holdings_save_result"     # survives the rerun a save triggers


# ── prices: market_data's cache, never a fetch on arrival ────────────────

def _cached_price(ticker):
    """(price, as_of) from market_data's cache, without fetching. (None, None) if absent.

    cached_fetch only calls its fetch function when the entry is missing or
    past the TTL, so passing one that returns None turns it into a pure read:
    a fresh entry comes back, a stale one comes back as well (cached_fetch
    prefers stale data to nothing), and an absent one is None. No request is
    made either way, which is what lets this run on every page load.
    """
    md = PIPELINE.md
    if md is None:
        return None, None
    key = f"{ticker}_{PRICE_PERIOD}_{PRICE_INTERVAL}"
    try:
        rows = md.cached_fetch(key, lambda: None, md.TTL_PRICE, cache_type="prices")
    except Exception:                                   # noqa: BLE001
        return None, None
    for row in reversed(rows or []):
        close = d.num(row.get("close")) if isinstance(row, dict) else None
        if close and close > 0:
            return close, md.cache_timestamp(key, "prices")
    return None, None


def _scan_price(ticker, scan):
    """The price the scan already recorded for a name it scored."""
    candidate = next((c for c in scan.candidates if c.get("ticker") == ticker), None)
    for record in (candidate, scan.scored_record(ticker)):
        if not record:
            continue
        price = d.num((record.get("metrics") or {}).get("price"))
        if price and price > 0:
            return price, record.get("price_as_of")
    return None, None


def _price_for(ticker, scan):
    """(price, as_of, source): market_data's cache first, then this scan's own."""
    price, as_of = _cached_price(ticker)
    if price:
        return price, as_of, "market_data cache"
    price, as_of = _scan_price(ticker, scan)
    if price:
        return price, as_of, "this scan"
    return None, None, None


def _refresh_prices(tickers):
    """The one network call this tab makes, and only when the button is pressed.

    One request per holding through market_data.get_price_history(), which
    writes the same cache entry _cached_price() reads - so the prices stay put
    for the rest of the TTL and across restarts, rather than living in this
    session.
    """
    md = PIPELINE.md
    if md is None:
        st.error("market_data is not importable, so prices cannot be refreshed. "
                 "See the sidebar for why.")
        return
    failed = []
    progress = st.progress(0.0, text="Refreshing prices…")
    for index, ticker in enumerate(tickers, start=1):
        progress.progress(index / len(tickers),
                          text=f"market_data.get_price_history({ticker!r}) "
                               f"[{index}/{len(tickers)}]")
        try:
            rows = md.get_price_history(ticker, period=PRICE_PERIOD,
                                        interval=PRICE_INTERVAL)
        except Exception as exc:                        # noqa: BLE001
            rows = None
            failed.append(f"{ticker} ({type(exc).__name__})")
            continue
        if not rows:
            failed.append(ticker)
    progress.empty()
    if failed:
        st.warning(f"No price came back for {', '.join(failed)}. market_data returns "
                   f"None on a failed fetch rather than raising, so this is a fetch "
                   f"that did not succeed, not a crash.")


# ── the table ─────────────────────────────────────────────────────────────

def _editor_frame(rows, scan):
    """The editable schema columns, plus the derived price columns beside them."""
    records = []
    sources = {}
    for row in rows:
        ticker = row["ticker"]
        price, as_of, source = _price_for(ticker, scan)
        if source:
            sources[source] = sources.get(source, 0) + 1
        cost = row.get("cost_basis")
        shares = row.get("shares")
        records.append({
            "Ticker": ticker,
            "Shares": shares,
            "Cost basis": cost,
            "Cur": row.get("currency") or "",
            "Entry date": row.get("entry_date") or None,
            "Entry price": row.get("entry_price"),
            "Price": price,
            "P&L %": None if not (price and cost) else (price - cost) / cost * 100.0,
            "Market value": None if not (price and shares) else price * shares,
        })

    frame = pd.DataFrame(records, columns=list(_ALL_COLUMNS))
    # float64 explicitly, not whatever the values infer to. A column of whole
    # numbers - or an empty one, on a file that does not exist yet - infers as
    # int64, and st.data_editor then treats it as an integer column and
    # truncates what is typed into it: 99.2 saved as 99, a fractional share
    # count silently rounded. Every one of these is a decimal quantity.
    for column in ("Shares", "Cost basis", "Entry price", "Price", "P&L %",
                   "Market value"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce").astype("float64")
    for column in ("Ticker", "Cur"):
        frame[column] = frame[column].astype("object").fillna("")
    frame["Entry date"] = pd.to_datetime(frame["Entry date"], errors="coerce")
    return frame, sources


def _rows_from_frame(frame, originals):
    """The editor's table back as schema rows, carrying unknown fields through."""
    extras = {row["ticker"]: row.get("_extra") or {} for row in originals}
    rows = []
    for _, record in frame.iterrows():
        row = {field: record.get(label) for label, field in _EDITOR_COLUMNS.items()}
        ticker = store._text(row.get("ticker")).upper()
        if ticker in extras:
            row["_extra"] = extras[ticker]
        rows.append(row)
    return rows


def _column_config():
    return {
        "Ticker": st.column_config.TextColumn(
            "Ticker", required=True, width="small",
            help="The Yahoo symbol, exactly as the pipeline uses it — RY.TO for a "
                 "TSX listing. One row per position."),
        "Shares": st.column_config.NumberColumn(
            "Shares", min_value=0.0, format="%g",
            help="Number of shares held. Fractional shares are fine."),
        "Cost basis": st.column_config.NumberColumn(
            "Cost basis", min_value=0.0, format="%.4f",
            help="Your average price per share, in the listing's own currency. "
                 "This is what P&L is measured against."),
        "Cur": st.column_config.TextColumn(
            "Cur", width="small",
            help="Optional. The currency the cost basis is in (USD, CAD). Leave it "
                 "blank and the scan infers it from the listing."),
        "Entry date": st.column_config.DateColumn(
            "Entry date", format="YYYY-MM-DD",
            help="Optional. The day the position was opened — holdings_exit.py "
                 "measures the reassess horizon from it."),
        "Entry price": st.column_config.NumberColumn(
            "Entry price", min_value=0.0, format="%.4f",
            help="Optional. What you paid per share that day — holdings_exit.py "
                 "measures the stop-loss from it. Not the same as cost basis once "
                 "a position has been added to."),
        "Price": st.column_config.NumberColumn(
            "Price", format="%.2f", disabled=True,
            help="Read-only. The latest close market_data already has cached, or "
                 "the price this scan recorded. Press Refresh prices for a live one."),
        "P&L %": st.column_config.NumberColumn(
            "P&L %", format="%.1f", disabled=True,
            help="Read-only. (price − cost basis) ÷ cost basis. holdings_exit.py's "
                 "stop-loss measures from entry price instead, which can differ."),
        "Market value": st.column_config.NumberColumn(
            "Market value", format="%.2f", disabled=True,
            help="Read-only. shares × price, in the listing's own currency."),
    }


# ── save ──────────────────────────────────────────────────────────────────

def _render_save_result(result):
    summary = result["summary"]
    st.success(f"Saved {result['written']} position(s) to `{result['path']}`."
               + (" The file was created." if result["created"] else ""))

    lines = []
    if summary["added"]:
        lines.append(f"**Added** {', '.join(summary['added'])}")
    if summary["removed"]:
        lines.append(f"**Removed** {', '.join(summary['removed'])}")
    for ticker, differences in summary["changed"]:
        lines.append(f"**{ticker}** — {'; '.join(differences)}")
    if not lines:
        lines.append("No values changed — the file was rewritten as it was.")
    if summary["unchanged"]:
        lines.append(f"{summary['unchanged']} position(s) untouched.")
    st.markdown("\n".join(f"- {line}" for line in lines))

    if result["carried_through"]:
        st.caption(f"{result['carried_through']} entry/entries the pipeline ignores "
                   f"(the template's example rows) were left in the file untouched.")
    if result["backup"]:
        st.caption(f"The previous file was copied to `{result['backup']}` first.")
    for warning in result["warnings"]:
        st.caption(f"⚠ {warning}")
    st.caption("Re-run `py scan_report.py` for sizing and exit signals that reflect "
               "this, then press ↻ Reload from disk.")


def _render_editor(scan):
    path = scan.holdings.path
    document, error = store.read_document(path)
    if error and error != "not found":
        st.error(f"`{path}` could not be read: {error}")
        st.caption("Fix the file by hand first. This tab will not offer to overwrite "
                   "a file it cannot read, because the overwrite is what would "
                   "destroy what is in there.")
        return

    rows, passthrough = store.rows_from_document(document)
    epoch = st.session_state.setdefault(_EPOCH_KEY, 0)

    # The file as it was when this generation of the table was seeded. Compared
    # again at save time: position_sizer writes a template when the file is
    # missing, and a person can edit it by hand while this tab is open.
    stamp = st.session_state.get(_STAMP_KEY)
    if not stamp or stamp.get("epoch") != epoch:
        stamp = {"epoch": epoch, "stamp": store.file_stamp(path)}
        st.session_state[_STAMP_KEY] = stamp

    result = st.session_state.pop(_RESULT_KEY, None)
    if result:
        _render_save_result(result)

    if document is None:
        st.info(f"No `holdings.json` in `{scan.directory}` yet. Add your positions "
                f"below and press **Save changes** — the file is created on the "
                f"first save, in the shape position_sizer.py and holdings_exit.py "
                f"already read.")
    st.caption(f"`{path}` — the only file stock_view writes. paper_sim.py's "
               f"`paper_portfolio.json` is a separate simulated ledger and is never "
               f"touched here.")

    frame, sources = _editor_frame(rows, scan)
    edited = st.data_editor(
        frame, key=f"holdings_editor_{epoch}", num_rows="dynamic",
        width="stretch", hide_index=True, column_config=_column_config(),
        disabled=list(_DERIVED_COLUMNS))

    if sources:
        parts = [f"{count} from {source}" for source, count in sorted(sources.items())]
        missing = len(rows) - sum(sources.values())
        if missing:
            parts.append(f"{missing} with none cached yet")
        st.caption("Prices: " + ", ".join(parts) + f" — read from cache on the "
                   f"{PRICE_PERIOD} window, never fetched by opening this tab.")
    elif rows:
        st.caption("No cached price for any holding yet. **Refresh prices** fetches "
                   "them once through market_data and they stay cached for a day.")

    left, middle, right, spacer = st.columns([1, 1, 1, 3])
    save = left.button("💾 Save changes", type="primary", width="stretch",
                       help="Validate the table and write holdings.json, after "
                            "copying the current file to holdings.json.bak.")
    refresh = middle.button("↻ Refresh prices", width="stretch",
                            disabled=not rows or PIPELINE.md is None,
                            help=f"{len(rows)} request(s) through market_data. "
                                 f"Cached for a day afterwards.")
    revert = right.button("Discard edits", width="stretch",
                          help="Throw away every unsaved change and re-read the file.")
    spacer.caption("Edits are not saved until you press Save changes.")

    if refresh:
        _refresh_prices([row["ticker"] for row in rows])
        st.rerun()

    if revert:
        st.session_state[_EPOCH_KEY] = epoch + 1
        st.rerun()

    if not save:
        return

    if store.file_stamp(path) != stamp["stamp"]:
        st.error(
            "`holdings.json` changed on disk since this table was loaded, so saving "
            "now would overwrite that change with what was on screen before it. "
            "Press **Discard edits** to reload the file, then make the change again.")
        return

    proposed = _rows_from_frame(edited, rows)
    entries, errors, warnings = store.validate(proposed)
    if errors:
        st.error("**Not saved.** " + ("Fix this first:" if len(errors) == 1
                                      else f"Fix these {len(errors)} problems first:"))
        st.markdown("\n".join(f"- {message}" for message in errors))
        if warnings:
            st.caption("Also, but not blocking: " + " ".join(warnings))
        return

    comment = (document or {}).get("_comment") or (store.NEW_FILE_COMMENT
                                                   if document is None else None)
    try:
        written = store.save(path, entries, passthrough, comment)
    except OSError as exc:
        st.error(f"Could not write `{path}`: {exc}")
        return

    written["summary"] = store.summarize(rows, entries)
    written["warnings"] = warnings
    st.session_state[_RESULT_KEY] = written
    st.session_state[_EPOCH_KEY] = epoch + 1
    dl.clear_cache()
    st.rerun()
