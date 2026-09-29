"""Forward-testing kit for Polymarket BTC 5m Up/Down markets.

One strategy engine (`strategy.Engine`) fed by two drivers:

- `replay`: recorded days in the outcometick layout (the free sample or what
  `record` writes), for backtests with book depth and trade prints;
- `live`: the CLOB market channel and the RTDS Chainlink feeds, for recording,
  paper trading and (gated) live trading.

See `python -m bot --help` and the README section "前向检验".
"""
