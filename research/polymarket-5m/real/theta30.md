# Theta30 favourite strategy

**Current verdict:** this is the only candidate promoted to a frozen fresh-forward test. It is not yet proven live-profitable.

At 30 seconds before settlement, buy the current favourite only when its ask is 0.80–0.97. The FAK limit is fixed before submission at no more than one cent above that ask, the order is five shares, and the crypto taker fee is charged. Historical fill proxies require five shares of executable depth to remain available from 150 through 350 ms after the decision. There is no routine hedge; an opposite claim is allowed only after the first fill is confirmed and the two all-in costs lock a positive payout.

## Evidence available before the freeze

| Dataset | Fills | Wins | Days | Net EV/share | Notes |
|---|---:|---:|---:|---:|---|
| 2026-09-08 independent sample | 25 | 25 | 1 | +7.96¢ | Original time/price-band hypothesis; snapshot fills only |
| 2026-09-13–15 Dublin top-five | 27 | 27 | 3 | +5.24¢ | 150–350 ms persistent executable depth |
| 2026-09-29–10-02 Dublin top-five | 41 | 40 | 4 | +4.46¢ | 150–350 ms persistent executable depth |
| Two delayed-depth datasets combined | 68 | 67 | 7 | +4.77¢ | Exact fair-price p=0.0655; one-sided day-cluster 99% lower bound +0.58¢ |

Every delayed-depth day was positive. Adding 450 ms to the execution window reduced fills but left the later dataset positive (+6.84¢ per filled share, 31 fills), so this candidate is not a stale-quote speed race.

The exact fair-price test has not crossed the frozen 1% threshold. The first 100 post-freeze persistent-depth fills, spanning at least seven days, are judged once. They must have both a positive one-sided 99% day-cluster lower bound and exact p below 0.01. Real exchange fills are then required separately.

## Why the earlier option model was rejected

Using a Binance-derived opening average made `Asian180` appear to earn tens of cents per share. Replacing that proxy with each market's official Chainlink `priceToBeat` reduced the 150–350 ms result to +0.22¢ per share (p=0.48), and a further 200 ms delay made it negative. The apparent edge was an oracle-basis measurement error, not an implied-volatility opportunity.
