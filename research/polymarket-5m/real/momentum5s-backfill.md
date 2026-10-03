# Frozen five-second momentum forward test

Signal frozen at `2026-10-01T06:20:50Z` from zoo100 rule #27; executable protocol frozen at `2026-10-03T15:00:57Z`. Paper only; no orders are submitted.

| Local signal-to-match lag | Signals | Five-share fills | Days | Net EV/share | Exact fair-price p |
|---:|---:|---:|---:|---:|---:|
| 300 ms | 465 | 400 | 3 | -1.55¢ | 1 |
| 350 ms | 465 | 397 | 3 | -1.67¢ | 1 |
| 400 ms | 465 | 396 | 3 | -1.19¢ | 1 |
| 500 ms | 465 | 399 | 3 | -1.64¢ | 1 |

Primary 400 ms fresh stopping sample (markets after the executable-protocol freeze): 0/1,000 fills across 0 days; EV +nan¢, one-sided 99% day lower bound +nan¢, exact p 1.
Status: `insufficient_fills`.

## Recording notes

- btc 37002779006: 24 candidates dropped because the CLOB socket closed between the quote and the order (107 disconnects in the recording; test C counts its judged lag only)
- btc 37041041140: 16 candidates dropped because the CLOB socket closed between the quote and the order (70 disconnects in the recording; test C counts its judged lag only)
- btc 37075141917: 28 candidates dropped because the CLOB socket closed between the quote and the order (80 disconnects in the recording; test C counts its judged lag only)
- btc 37095301195: 32 candidates dropped because the CLOB socket closed between the quote and the order (76 disconnects in the recording; test C counts its judged lag only)
- btc 37111395430: 29 candidates dropped because the CLOB socket closed between the quote and the order (77 disconnects in the recording; test C counts its judged lag only)
