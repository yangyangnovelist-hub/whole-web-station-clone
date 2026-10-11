# Receipt-time development screen

This is a research-only, standard-library pipeline. It never submits an order,
changes production settings, or certifies an investment return. Existing frozen
evaluators are not modified.

The original registry contains 500 rule variants (50 templates, five remaining-
time windows, two strength thresholds). A further 220 variants are registered
before inspecting PnL to cover datasets without aggressor direction or verified
TWAP model inputs. Keep the original blocked rules in the denominator: the
combined registry has 720 variants, with a goal of evaluating 500 feature-
supported variants. These are overlapping families, not 720 independent alpha
mechanisms. Actual evaluated counts come from the output, not this goal.

## Commands

Run from this directory. Input data and outputs belong outside the repository.
The adapter requires a strict receipt archive with market registry, official
outcomes, manifest, and source/CLOB event streams. Use `--help` for the current
raw-market metadata and fixed-latency projection options. Never repair missing
aggressor labels by guessing from price changes. Never replace settlement with
external OHLC direction.

```bash
python3 mine500_adapter.py --help
python3 mine500_batch.py --input /private/normalized.jsonl.gz \
  --out /private/new-batch --sizes 5,10,20 --latencies 500,600,1000
python3 -m pytest -q test_mine500_catalog.py test_mine500_adapter.py \
  test_mine500_replay.py test_mine500_statistics.py
```

The batch refuses to overwrite an existing output directory. It records source,
input, catalog and execution-configuration hashes before running; writes every
candidate, attempted order, summary and statistics; and checks source stability
between sizes. Its output is always development evidence. A historical run does
not become a new holdout simply because this batch recorded a hash first.

`oct9_sizeN` output folder names are a convenience inherited from the first
development run; the authoritative dataset identity and time interval are in
the supplied input and registration. Do not infer dates from output names.

## Timing and execution

- Decisions use a fixed one-second receipt-time grid. Features carry their
  actual availability times; late source timestamps never advance knowledge.
- A fixed-latency projection, when requested, consumes every raw event and
  captures exact as-of state at the predeclared decision/matching instants.
  It retains each token's real receipt timestamp and only supports its declared
  grid and delays. It is not general-purpose downsampled market history.
- The primary run is five shares at 500 ms total local-decision-to-match delay.
  600/1000 ms and 10/20 shares are counterfactual stress runs. Total delay
  includes venue waiting; do not add venue waiting a second time.
- The first eligible signal consumes that candidate's market even if no order
  is sent or no fill occurs. The decision's best ask fixes the limit. At the
  actual matching instant, partial FAK can consume only displayed levels at or
  below that limit. Independent token freshness is enforced.
- Displayed depth is a fill/capacity upper bound. It does not reveal competing
  orders, queue position, hidden cancellations, or measured fill probability.
- Fee rate must be supplied with provenance. The formula is computed per filled
  price level; venue-specific rounding/minimum collection is not modeled.
- Enforce the recorded minimum order size. The primary five-share and 10/20-share
  stress sizes require a market minimum of no more than five shares; smaller
  one/two-share runs must not be represented as executable capacity.
- Each candidate/latency/size has its own $1,000 ledger. Cash is reserved when
  the simulated order is sent and released after recorded settlement knowledge.
  These separate ledgers must never be summed as a shared-capital portfolio.

## Evidence and rejection

Missing required features, missing execution inputs, no trigger, zero fills,
partial fills and unresolved inventory remain distinguishable. Missing evidence
is null, not an invented zero-PnL experiment. Statistics preserve the complete
registered denominator and all attempted orders.

Primary multiplicity includes all 720 registered variants. The fair-price
Poisson-binomial reference requires one entry per market, equal actual filled
shares and binary payouts, and still assumes independent market outcomes.
It is not a substitute for clustered uncertainty. A 99% day-cluster bootstrap
is unavailable with fewer than seven settled UTC days. Costs of another
0.25/0.5/1 cent per filled share are shown separately. Realized drawdown is
labeled as such and is not mark-to-market drawdown.

No development result from this pipeline qualifies as a large independent OOS
edge. Further qualification needs untouched later data, stable code/config,
market-specific rules and fees, calibrated execution failures, cluster-aware
uncertainty after multiple testing, and one shared-capital portfolio replay
including overlapping signals and settlement reuse. Maker and two-leg execution
are not validated by this single-leg taker engine.

Keep private tapes, order ledgers, research results, download links and artifact
identifiers out of this public repository. Persist them separately with source
commit and content hashes so the research can be resumed.

## Future source-field contract

`mine500_source_contract.py` is a code-only gate for later recordings; it does
not alter the frozen October evaluator or its confirmation protocol. External
trade direction is accepted only from an explicit provider field
(`aggressor_side`, documented trade `side`, or buyer-is-maker boolean).
Conflicts fail closed and missing direction remains missing, so price ticks and
book changes can never manufacture 15/60-second signed-flow features.

The contract preserves source-event and local-receipt clocks but orders and
cuts features only by local receipt. It also accepts settlement-tail samples
only when the record explicitly identifies Chainlink Data Streams, BTC/USD,
an exact 60-second window, both boundaries, both clocks, sequence, and raw
provenance. Binance OHLC is not an admissible substitute. These checks make the
220 currently blocked variants executable only after a future recorder really
captures the missing fields; they do not turn the existing archive's nulls into
data and do not create new OOS evidence.

## Fixed-ledger missed-fill sensitivity

`mine500_fragility.py --inputs /private/size5/replay.json
/private/size10/replay.json /private/size20/replay.json --out /private/fragility`
analyzes existing development ledgers without changing the signal, execution,
or future-confirmation code. It records input hashes and configuration before
computing results. This is post-development analysis, not a blind experiment.

The fixed stress grid removes up to floor(10/25/50 percent of filled orders),
starting with the largest positive contributions, and keeps losing fills.
The extra-cost grid is zero or one cent per retained share. Removed orders lose
both their acquisition cost and their settlement payout; partial fills retain
their recorded sizes. No replacement order or cash-redeployment is assumed.
This hindsight-based adverse-selection envelope is not a measured failure
probability, a feasible trading selector, or a dynamic portfolio simulation.

Report the integer number actually deleted: fractional budgets can delete zero
orders from short ledgers. The worst-single-market result and the minimum count
of lost winners needed to erase profit expose that small-sample weakness.
Identical fill signatures reveal exact duplicates; distinct signatures do not
establish distinct economic mechanisms. Missing/no-fill rows remain explicitly
ineligible, and every registered candidate stays in the reported denominator.
Keep the existing frozen confirmation commit pinned when adding this module.

## One shared cash account

`mine500_shared_portfolio.py` combines complete attempt ledgers without replaying
or changing their signals. A plan must pin input and catalog hashes, panel IDs,
latencies, sizes, extra costs and the global market policy before the combined
PnL is computed. The output inherits the input split and can never promote
development attempts to OOS.

The conservative default consumes each market at its earliest panel signal.
The candidate ID breaks exact-time ties. Later signals are suppressed even if
the first order is rejected or unfilled. Opposite sides are not stacked or
netted. Thus displayed depth is used at most once per market and strategy PnLs
are never added. At an equal timestamp, a new decision is processed before a
match releases reservation and before a settlement payout releases capital.

The engine rebuilds reservation, match cost, settlement reuse, minimum
available cash and realized drawdown on one account. Extra costs apply only to
filled shares and are reserved against the requested size. Results report
suppressed signals, opposite-side conflicts and shared-cash rejections.
Selection methods must state whether outcomes/PnL were used. A coverage-selected
panel is an engineering baseline; a development-winner panel is explicitly
post-hoc. Neither establishes multiple independent mechanisms, real queue fills,
or a deployable portfolio. Future confirmation requires a separately frozen
plan and untouched inputs; it must not rewrite the existing candidate protocol.

## Private FAK execution calibration

`mine500_execution_calibration.py` is the fail-closed bridge from displayed-depth
paper fills to future private FAK receipts. It accepts only an explicit private
order response, preserves decision/send/match/response clocks, treats the
measured match timestamp as already containing the venue hold, separates partial
fill, kill, reject and unresolved outcomes, and reports exact 99% fill-realization
bounds plus outcome-dependent fill diagnostics. It creates no signal and cannot
change the frozen rules. Until prospective private receipts are supplied,
displayed depth remains only a capacity upper bound and the calibration result is
null.
