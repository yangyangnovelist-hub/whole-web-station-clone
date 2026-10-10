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
