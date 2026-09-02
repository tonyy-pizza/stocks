# stock_view

An interactive local dashboard over the stocks scan pipeline's output.

```
universe_screen.py -> stock_evaluator.py --batch -> sentiment
                   -> rmt_cluster.py -> position_sizer.py -> scan_report.py
                   -> stock_view  (you are here)
```

**It never re-runs the pipeline.** Not on launch, not on refresh, not from any
button. It reads the JSON files the pipeline already wrote and works entirely
offline. When the data looks stale it says so and tells you to run
`scan_report.py` yourself — re-running the scan means hundreds of network
requests and a rate-limited sentiment stage, which is not something a dashboard
should be able to start by accident.

There is exactly one network call in the whole app: the price sparkline on the
drill-down's **Price history** tab, which only fires when you press its button.

This is a separate app from Dionysus. It does not touch the PyQt6 dashboard.

## Where it goes

`stock_view/` sits beside the pipeline scripts so it can import them as sibling
modules:

```
C:\Users\joey\stocks\
    market_data.py
    stock_evaluator.py
    rmt_cluster.py
    position_sizer.py
    scan_report.py
    data\                  <- the JSON this dashboard reads
    stock_view\            <- this folder
        stock_view.py
        launch.bat
        requirements.txt
        sv\
```

It finds the pipeline using the same `_add_project_dir_to_path()` search the
other scripts use, widened by one level because it sits in a subfolder: it looks
at `$STOCKS_DIR`, then its own folder, then the folder above it, then a `stocks\`
folder beside either. If you keep it somewhere else entirely, set `STOCKS_DIR`
to the folder holding `market_data.py` before launching.

Data comes from `$STOCKS_DATA_DIR`, else `market_data.BASE_DIR \ "data"` — the
same convention as the rest of the project. The resolved path is printed in the
sidebar.

## Launch

**Set it up once**, from the folder above this one:

```
powershell -ExecutionPolicy Bypass -File setup_shortcuts.ps1
```

That puts **Stock View** and **Run Stock Scan** on your Desktop and in the
Start Menu, and from then on it is a double-click. `-ExecutionPolicy Bypass`
applies to that one command only and changes no setting — Windows refuses to
run unsigned `.ps1` files by default, and refuses quietly enough to look like a
broken script.

The shortcuts do not go through the `.bat` file association. Their target is
`cmd.exe` with the script named as an argument, and an association only decides
what happens when Windows is asked to *open* a file. Naming the interpreter
outright skips that decision, so they still work on a machine where
double-clicking a `.bat` opens an editor or does nothing at all — which is the
usual reason a `.bat` "will not run" when the command inside it is fine.

Failing that, double-click **`launch.bat`** directly, or:

```
py -m pip install -r requirements.txt
py -m streamlit run stock_view.py
```

Use `py -m streamlit`, not bare `streamlit`. The `streamlit` command lives in
Python's `Scripts\` folder, which is very often not on `PATH`; that is the
`"streamlit is not recognized"` error, and it means the launcher is missing,
not the package. Going through `py -m` runs the same program via the
interpreter, which always works. `launch.bat` does it that way for the same
reason, and offers to `pip install` the requirements if they are not there yet.

Opening it before a scan has ever run is fine — it shows what is missing and
what to run, rather than an error.

## The views

| View | What it shows |
|---|---|
| **Overview** | One row per candidate — composite, rating, liquidity/trend/divergence/conviction flags, data coverage, sentiment, held marker. Sortable on any column; filter by sector, minimum composite, cluster resolution, held vs. not held, and data coverage. |
| **Cluster explorer** | The clusters from `clustered.json`, which is richer than the trimmed `cluster` field on each candidate: members, average correlation, dispersion, eigenvalue, share of variance, the resolution and why it resolved that way, plus the winner and its demoted peers. Each cluster gets a correlation heatmap. |
| **Position sizing simulator** | The core view. Move the correlation threshold, reduction factor, basis (raw/cleaned) and reduction mode (proportional/flat), and every not-yet-held candidate is re-sized live. Select a handful of names for a rollup: combined allocation, sector concentration, and which holdings are taking the most size out of the selection. |
| **Ticker drill-down** | The full blob behind one name — every metric group, Piotroski, Altman Z, Graham, Magic Formula, the DCF scenarios, the value screen, insider activity, warnings and notes. Plus the optional price sparkline. |
| **Holdings** | Two tabs. **Positions**: `holdings.json` as a table, with book value totalled **per currency**, and the exit review `position_sizer.py` runs over each holding — including its ROA trend and data coverage. **Edit**: the same file as an editable table — add, change and delete positions, with current price and P&L beside them, validated and written on an explicit save (previous file kept as `holdings.json.bak`). |

It also reads two files it does not put in a view: `candidates.json`, for
whether Stage 0's universe came back whole, and `run_status.json`, for whether
the last scan finished. Both go in the sidebar, and a halted run gets a banner
across the top of every view — see below.

## When the last scan did not finish

`scan_report.py` stops the pipeline when a required stage fails, rather than
letting the next stage read yesterday's file and report it as today's. It
records what happened in `run_status.json`, and this is the only thing that
reads it back.

That matters here more than in the terminal. A halted run leaves new files
beside stale ones, and **the ages will not look wrong** — the stages that ran
are fresh, and the ones that never ran are the problem. So the banner comes
before any staleness note: it names the stage that stopped the run, lists what
was never attempted, and says plainly that the views below are showing two
different scans at once.

The sidebar carries the matching Stage 0 line. `universe_screen.py` refuses to
overwrite a good `candidates.json` when more than half its region/sector
partitions fail, but a run under that bar still writes a thinner universe than
usual — and downstream, a smaller universe is indistinguishable from a tighter
market. Stage 0 records the counts; the sidebar is where they are read.

## How the live re-sizing works

`sized_candidates.json` already stores, per candidate, both the raw and the
MP-cleaned correlation to every holding it was compared against:

```json
"sizing": { "correlations": { "RY.TO": { "raw": 0.79, "cleaned": 0.71 } } }
```

The expensive part — fetching prices, building one correlation matrix over
candidates and holdings together, filtering it through Marchenko-Pastur — is
already done and on disk. Moving a slider is arithmetic over data already in
memory, so no refetch and no re-run is needed.

The arithmetic is not reimplemented here. `sizing_scale()`,
`apply_reduction()` and `worst_correlation()` are imported from
`position_sizer.py`, and `position_guidance()` from `stock_evaluator.py`, so the
simulator cannot drift from what the pipeline actually does.

`worst_correlation()` — which of a candidate's holdings drives its cut — used to
be a local copy here. The copy happened to be the correct one: it skipped
correlations stored as `null` or `NaN`, where `position_sizer` crashed on the
first and applied a full-size cut on the second. That is fixed at source now, so
the rule lives in one place and this imports it like the rest.

One detail worth knowing, because a threshold-and-reduction sketch misses it:
`position_sizer.size_candidate()` applies **two** independent modifiers and
multiplies them.

```
scale = correlation_scale x conviction_scale
```

The correlation modifier asks "do I already own this trade" — that is what the
sliders move. The conviction modifier asks "do the financials and the public
story agree about why the price is where it is" — it comes from the sentiment
stage, not from any slider, and is read back off the stored sizing block
unchanged. Ignoring it would show every reduced candidate as larger than
`position_sizer.py` sized it. The simulator's sidebar has an *Apply the
conviction modifier* tick-box, which is `position_sizer.py --no-conviction`: the
verdict is still reported, it just stops changing size.

The simulator carries an **integrity check** that proves the claim: it re-sizes
every candidate at the parameters the file was written with and compares against
what the file says. If a future change to `position_sizer.py` makes the two
disagree, that check goes red instead of the dashboard quietly showing numbers
the pipeline would not.

It compares the guide **sentence** as well as the percentages, and reports a
text-only difference separately from a numeric one. Those are genuinely
different findings, and one of them used to go unasked: `apply_reduction()` once
scaled only the first percentage in a guide, so a halved candidate read
`Core: 1.5%–2.5%; up to 8% with diversification` — correct numbers, and an 8%
ceiling quoted on a position just cut to 2.5%. A numbers-only check cannot see
that, which is why it survived as long as it did. A text-only mismatch now
usually just means the file predates the current `position_sizer.py`, so it is
reported as a note rather than as drift.

### What the heatmap can and cannot show

`position_sizer.py` builds one correlation matrix over candidates *and* holdings
together, but only writes out the candidate-to-holding part of it —
`sizing.correlations` is keyed by holding. The candidate-to-candidate block is
never persisted, and `clustered.json` keeps only each cluster's *average*
pairwise correlation as a single number.

So a members × members heatmap is not available offline, and the dashboard does
not fabricate one. What it draws instead is the pairwise structure that does
exist on disk: each cluster member against each current holding, which is the
relationship the sizing simulator actually acts on.

## What it does not do

- **Never runs** `universe_screen.py`, `stock_evaluator.py`, `rmt_cluster.py`,
  `position_sizer.py` or `scan_report.py`. Run `py scan_report.py` yourself and
  press ↻ Reload.
- **Never writes** to `sized_candidates.json`, `clustered.json` or
  `scored_candidates.json`. Those are pipeline outputs and are treated as
  read-only.
- **Writes exactly one file**, and only from the Holdings → Edit tab:
  `holdings.json`, the hand-maintained input `position_sizer.py` and
  `holdings_exit.py` read. Nothing is written until you press **Save changes**,
  the table is validated first (no duplicate tickers, positive numbers, a real
  entry date that is not in the future), and the previous file is copied to
  `holdings.json.bak` before the new one lands. `paper_portfolio.json` belongs
  to `paper_sim.py`'s separate simulated track and is never touched.
  Each entry takes an optional `"currency"`, and when it
  is there it wins: you know what you paid in better than any inference does.
  Without it the currency comes from the scan's `quote_currency`, then from a
  `.TO`/`.V`/`.CN`/`.NE` suffix, and stays *unknown* rather than being guessed
  into a total.
- **Never sums across currencies.** `quote_currency` is CAD or USD per ticker.
  Position guides are percentages of the account and so are comparable across
  both; cost-basis totals are not, so they are reported per currency with the
  mix named. A selection spanning currencies says so.

## Files

`sv/` deliberately does **not** import `stocks_common.py`, which the pipeline
scripts share for paths, atomic writes and number coercion. Every view that only
reads JSON has to work with nothing installed but streamlit, pandas and plotly,
and it cannot import a pipeline module to find out how to parse a number. The
two small `_num` copies in `sv/` are the price of that guarantee and are kept on
purpose; the sizing math, which genuinely must not drift, is imported.

```
stock_view.py            entry point: page setup, sidebar, view router
sv/pipeline.py           finds and imports the pipeline modules
sv/data_loader.py        reads the JSON, freshness, currency inference
sv/sizing.py             the live re-size, over position_sizer's own functions
sv/display.py            colour thresholds and flags, matched to scan_report.py
sv/views/overview.py     the candidate table
sv/views/clusters.py     the cluster explorer
sv/views/simulator.py    the sizing simulator
sv/views/drilldown.py    the per-ticker detail
sv/views/holdings.py     holdings and the exit review
```

Scores are coloured on `scan_report.colour()`'s own thresholds — green ≥ 7.5,
yellow ≥ 5.0, red below — and the flag column uses `flag_cells()`'s vocabulary,
so a name reads the same here as it does in the terminal report.

## Two columns worth reading carefully

**Trend** is `roa_trend`: the direction multi-year ROA actually went, from the
share of year-over-year steps that improved plus the overall first-to-last
change. It replaced `roa_trend_consistent`, which demanded a non-decreasing ROA
in *every* year and so went red on one soft year in four while staying green for
a two-year-old listing with a single step to clear — the same business reading
worse for having reported longer. `·` still means too little history to say, and
`~ mixed` means the endpoints and the year-to-year steps disagree because one
spike is doing the work.

**Data** is what share of the ~24 scoring inputs the ticker actually had.
`stock_evaluator.score()` returns a neutral 5.0 for anything it cannot read,
which is the right default — it refuses to punish a company for Yahoo's coverage
of it — but it means a name with almost no data lands near 5.0 and reads as a
considered HOLD rather than as an absence of information. A 5.4 built from four
inputs and a 5.4 built from twenty-four are not the same claim. Below 60% the
evaluator raises a *thin data* risk flag, which costs the name its top
position-size band, and the drill-down shows the breakdown per dimension next to
the score each one produced.

⚠ For informational use only. Not financial advice.
