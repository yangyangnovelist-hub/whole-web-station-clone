# Theta30 favourite strategy

**Current verdict:** research-only, unproven, and not authorised for live trading. It remains a frozen hypothesis solely so the fresh-forward test can accept or reject it without changing parameters.

The raw win rate is not evidence of alpha. A contract bought at 0.80–0.97 is already priced to win most of the time. The relevant null is the exact probability implied by each executable entry price plus fees, not 50%. On that test the delayed-depth history is only p=0.0655, so it fails the 1% gate even before correcting for the wider strategy search.

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

## Post-freeze historical audit (not counted toward the gate)

The independent September 8 sample was rechecked against its full-depth snapshots after the strategy was frozen. Of 15 quotes that remained reachable at the fixed limit throughout the 150–350 ms window, 14 also had at least five executable shares throughout; all 14 won, for +7.77¢ per share after fees. Minimum executable depth was 19 shares and median depth was 505 shares. The one failed candidate is treated as unfilled. This removes the snapshot-capacity concern but, because the audit was performed after parameter selection and covers only one day, none of these observations count toward the fresh-forward stopping rule.

## Why the earlier option model was rejected

Using a Binance-derived opening average made `Asian180` appear to earn tens of cents per share. Replacing that proxy with each market's official Chainlink `priceToBeat` reduced the 150–350 ms result to +0.22¢ per share (p=0.48), and a further 200 ms delay made it negative. The apparent edge was an oracle-basis measurement error, not an implied-volatility opportunity.
