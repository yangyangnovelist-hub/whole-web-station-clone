# Family 30: Reward-subsidy standalone audit

**Observed:** 2026-10-11T01:46:30Z
**Verdict:** **INCOMPLETE**

## Frozen mechanism

Before rounding, crypto weighted volume is `shares × p × (1-p) × 2.3`
and the fee is `shares × 0.07 × p × (1-p)`, so nominal fee/wV is
price-invariant at `0.07 / 2.3 = 3.043478%`. That identity is not used
as an executable lower bound because Polymarket truncates fees to five
decimal places and charges zero below the 0.00001 quantum.

Using each checked market's live tick and minimum-order settings,
and allowing a minimum order to fragment into two-decimal partial
fills, exhaustive enumeration finds the most favourable rule-derived
fee/wV ratio is `0.000000%`: a
0.01-share fill at 0.01 has raw
fee 0.00000693, rounded fee 0.00000.
This is a theoretical arithmetic witness, not evidence that a
minimum taker order can repeatedly obtain that fragmentation.
The optimistic arithmetic screen sets spread, slippage, adverse
selection, and market risk to zero; it does not claim those costs
are zero in trading.

The first tier receives no rebate. For each later tier, the already-
unlocked prior-tier rebate is optimistically applied to the next wV
tranche; no rebate is backfilled onto earlier qualifying volume.

## Tier economics

| Tier | Required wV | Tier rebate | Optimistic fees to qualify | Cumulative bonus | Subsidy net | Future residual fee / wV |
|---|---:|---:|---:|---:|---:|---:|
| Bronze | 2,000 | 3% | $0.00 | $10.00 | $10.00 | 0.0000% |
| Silver | 20,000 | 8% | $0.00 | $60.00 | $60.00 | 0.0000% |
| Gold | 200,000 | 18% | $0.00 | $310.00 | $310.00 | 0.0000% |
| Platinum | 1,000,000 | 32% | $0.00 | $1,810.00 | $1,810.00 | 0.0000% |
| Diamond | 4,000,000 | 44% | $0.00 | $9,310.00 | $9,310.00 | 0.0000% |
| Obsidian | 10,000,000 | 50% | $0.00 | $34,310.00 | $34,310.00 | 0.0000% |

The arithmetic screen cannot reject subsidy alpha: the enumerated
price/size grid under the venue precision rules contains a raw fee
below one quantum, so repeated
fee-free fragments would leave the one-time bonuses positive.
The maker rebate was already tested in Family 24 and is not counted again here.

## Live reward-configuration check

Both the native and `sponsored=true` views of the official raw endpoint
were read. Nested configurations were counted only when their dates covered
the observation instant and `rate_per_day` was positive.

| Start (Unix s) | Market | Min order | Tick | Native active | Active with sponsored |
|---:|---|---:|---:|---:|---:|
| 1791683100 | `btc-updown-5m-1791683100` | 5 | 0.01 | 0 | 0 |
| 1791683400 | `btc-updown-5m-1791683400` | 5 | 0.01 | 0 | 0 |
| 1791683700 | `btc-updown-5m-1791683700` | 5 | 0.01 | 0 | 0 |
| 1791684000 | `btc-updown-5m-1791684000` | 5 | 0.01 | 0 | 0 |

## Decision

Family 30 exposes a zero-fee dust-fill candidate but does not yet prove
a strategy. A 0.01-share partial fill at a one-cent outcome price is
below the documented fee quantum, while submitted orders still require
at least five shares. The next audit must establish how much genuine,
non-self-matched weighted volume a taker can deliberately obtain through
such fragments and charge spread, adverse selection, settlement loss,
and operational limits. The arithmetic witness alone is not evidence of executable capacity.

## Sources

- https://docs.polymarket.com/programs/taker-rebates
- https://docs.polymarket.com/programs/maker-rebates
- https://docs.polymarket.com/programs/liquidity-rewards
- https://docs.polymarket.com/trading/fees
- https://docs.polymarket.com/trading/place-orders
- https://docs.polymarket.com/api-reference/rewards/get-raw-rewards-for-a-specific-market
