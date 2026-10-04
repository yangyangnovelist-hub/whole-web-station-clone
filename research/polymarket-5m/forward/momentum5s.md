# Frozen five-second momentum forward test

Signal frozen at `2026-10-01T06:20:50Z` from zoo100 rule #27; executable protocol frozen at `2026-10-03T15:00:57Z`. Paper only; no orders are submitted.

| Local signal-to-match lag | Signals | Five-share fills | Days | Net EV/share | Exact fair-price p |
|---:|---:|---:|---:|---:|---:|
| 300 ms | 584 | 484 | 4 | -0.73¢ | 1 |
| 350 ms | 584 | 483 | 4 | -0.92¢ | 1 |
| 400 ms | 584 | 482 | 4 | -0.53¢ | 1 |
| 500 ms | 584 | 482 | 4 | -1.14¢ | 1 |

Primary 400 ms fresh stopping sample (markets after the executable-protocol freeze): 86/1,000 fills across 2 days; EV +2.49¢, one-sided 99% day lower bound +nan¢, exact p 0.3195.
Status: `insufficient_fills`.

## Recording notes

- btc _temp: 20 candidates dropped because the CLOB socket closed between the quote and the order (70 disconnects in the recording; test C counts its judged lag only)
