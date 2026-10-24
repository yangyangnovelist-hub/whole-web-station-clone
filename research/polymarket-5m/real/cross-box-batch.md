# Family 29: same-expiry 5m-15m box with independent batch legs

Archives: 67. Evidence tier: `aligned_100ms_snapshot_upper_bound`.

The lower opening strike's Up and the higher opening strike's Down pay at least $1 only when expiry and resolution source match. Two FOK entries in one Polymarket batch are processed independently, so paired fills and orphan legs are reported separately.

Eligible source-verified pairs: 0. Economic candidate pairs: 0. Minimum-order blocks: 0.
Diagnostic rows: 880. Status counts: `{'settlement_authority_conflict': 880}`.

**Historical screen verdict:** INCOMPLETE: B/C evidence at both 400 ms and 500 ms is unavailable.

| Period | Latency | Signals | Paired | Lower/upper orphans | Invalid | Paired rate | Gross floor/set | Fees | Bonus payout | Net EV/share | 99% day lower | Corrected p | Net PnL | Max DD | Worst orphan | Peak capital | Attempted notional |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| – | – | 0 | 0 | 0/0 | 0 | – | – | – | – | – | – | – | – | – | – | – | – |

This historical screen cannot prove a live fill. A positive result only qualifies the frozen rule for direct receipt-ordered L2 forward validation; it does not authorise production.
