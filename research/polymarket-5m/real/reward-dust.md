# Family 31: Zero-fee dust-fill capacity audit

**Observed:** 2026-10-11T04:17:00.170956Z
**Verdict:** **INCOMPLETE**
**Evidence class:** public-row diagnostic only

## Scope and identification

The requested window starts 2026-09-11; 30 of 30 complete UTC days and 8,640 resolved BTC five-minute markets are currently included. The public taker-only feed contributed 12,151,186 rows. Each condition is read from its window open through one hour later, covering the five-minute trading window and delayed chain stamps.

A row is a theoretical zero-fee candidate only when `shares × 0.07 × price × (1-price) < 0.00001`. The inequality is strict. Public rows do not expose a unique trade id, parent order, match fragments, actual charged fee, or credited weighted volume. A larger public row can conceal several independently rounded match fragments. Transaction hashes and local occurrence counters therefore support overlap-safe row counting, not a complete upper bound on fee-free fragments or execution attribution.

## Capacity

| Metric | Directly visible public rows |
|---|---:|
| Theoretical zero-fee rows | 3,703 |
| Distinct transaction hashes | 3,703 |
| Markets with a candidate | 2,529 |
| Nominal weighted volume | 0.797220986 |
| Linear 30-day weighted volume at the observed daily rate | 0.7972209860000000000000000001 |
| Fraction of Bronze (2,000 wV) | 0.039861% |
| Daily capacity multiple needed inside 30 days | 2,508.7× |
| Fills to Bronze at the observed candidate mix | 9,289,771 |
| Candidate notional | $89.87732 |
| Hold-to-settlement PnL before any operational cost | $-0.21220 |
| Largest candidate-wallet share | 28.22% |

The canonical 0.01-share fill at price 0.01 contributes 0.0002277 wV, so even that reference case needs 8,783,488 fills to reach Bronze. Bronze was not reached in the directly visible sample. At the observed rate Bronze is unreachable within a rolling 30-day window.

## Fixed controls

Candidate taker sides: BUY: 4, SELL: 3,699.
Candidate outcomes: Down: 1,713, Up: 1,990.
Candidate prices: 0.001: 6, 0.002: 5, 0.003: 4, 0.004: 6, 0.005: 2, 0.006: 6, 0.007: 2, 0.008: 1, 0.009: 4, 0.01: 262, 0.011: 1, 0.012: 3, 0.013: 1, 0.986: 1, 0.987: 3, 0.988: 2, 0.989: 13, 0.99: 2,513, 0.991: 9, 0.992: 2, 0.993: 8, 0.994: 7, 0.995: 4, 0.996: 8, 0.997: 5, 0.998: 7, 0.999: 818.
Candidate sizes: 0.01: 3,046, 0.02: 15, 0.03: 3, 0.04: 3, 0.05: 24, 0.06: 12, 0.07: 2, 0.08: 2, 0.1: 572, 0.11: 9, 0.12: 11, 0.13: 2, 0.14: 2.
The fixed ordinary-fee control contains 50,035 other 0.01-share public taker rows; their hold-to-settlement PnL is $2.51897. These controls are descriptive only because neither arm is a controllable parent-order sample.

## Decision

The sample cannot promote a strategy. Directly visible dust rows miss Bronze by orders of magnitude, but omitted match-fragment boundaries mean this public-row scan cannot support formal capacity rejection. No public row proves that an account can choose the partial-fill size or receive nominal weighted-volume credit. The frozen 30-day scan remains a concentration diagnostic; rejection additionally requires a fragment-complete chain or authenticated-account bound.

## Sources

- https://docs.polymarket.com/trading/fees
- https://docs.polymarket.com/programs/taker-rebates
- https://docs.polymarket.com/api-reference/feeds/list-trades
- https://docs.polymarket.com/api-reference/trade/get-trades
