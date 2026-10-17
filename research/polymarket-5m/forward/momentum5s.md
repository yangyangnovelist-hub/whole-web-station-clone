# Frozen five-second momentum forward test

Signal frozen at `2026-10-01T06:20:50Z` from zoo100 rule #27; executable protocol frozen at `2026-10-03T15:00:57Z`. Paper only; no orders are submitted.

| Local signal-to-match lag | Signals | Five-share fills | Days | Net EV/share | Exact fair-price p |
|---:|---:|---:|---:|---:|---:|
| 300 ms | 973 | 808 | 7 | -0.50¢ | 1 |
| 350 ms | 973 | 811 | 7 | -1.28¢ | 1 |
| 400 ms | 973 | 806 | 7 | -0.97¢ | 1 |
| 500 ms | 973 | 809 | 7 | -1.26¢ | 1 |

Primary 400 ms fresh stopping sample (markets after the executable-protocol freeze): 410/1,000 fills across 5 days; EV -0.76¢, one-sided 99% day lower bound +nan¢, exact p 1.
Status: `insufficient_fills`.
