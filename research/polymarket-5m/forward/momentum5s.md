# Frozen five-second momentum forward test

Signal frozen at `2026-10-01T06:20:50Z` from zoo100 rule #27; executable protocol frozen at `2026-10-03T15:00:57Z`. Paper only; no orders are submitted.

| Local signal-to-match lag | Signals | Five-share fills | Days | Net EV/share | Exact fair-price p |
|---:|---:|---:|---:|---:|---:|
| 300 ms | 502 | 423 | 3 | -1.68¢ | 1 |
| 350 ms | 502 | 421 | 3 | -1.95¢ | 1 |
| 400 ms | 502 | 420 | 3 | -1.50¢ | 1 |
| 500 ms | 502 | 422 | 3 | -2.07¢ | 1 |

Primary 400 ms fresh stopping sample (markets after the executable-protocol freeze): 24/1,000 fills across 1 days; EV -6.53¢, one-sided 99% day lower bound +nan¢, exact p 1.
Status: `insufficient_fills`.

## Recording notes

- btc _temp: 44 candidates dropped because the CLOB socket closed between the quote and the order (102 disconnects in the recording; test C counts its judged lag only)
