import numpy as np
import pandas as pd

import momentum5s as m5


def test_signal_uses_first_available_five_second_move():
    idx = pd.Index(range(900, 1301))
    grid = pd.Series(10.0, index=idx)
    grid.loc[1100:] = 10.01
    sigma = pd.Series(0.001, index=idx)
    markets = pd.DataFrame([{"market_id": "m", "start_ts": 1000.0, "winner": "Up"}])

    got = m5.signals_from_grid(markets, grid, sigma)

    assert len(got) == 1
    assert got.iloc[0]["t"] == 1100
    assert got.iloc[0]["side"] == "Up"
    assert np.isclose(got.iloc[0]["z5"], 0.01 / (0.001 * np.sqrt(5)))


def test_received_grid_cannot_use_a_trade_before_it_arrives():
    spot = pd.DataFrame({
        "trade_ts": [1000.0, 1001.0],
        "receive_ts": [1000.1, 1006.1],
        "price": [100.0, 101.0],
    })

    grid, _ = m5.received_grid(spot, min_periods=1)

    assert grid.loc[1006] == np.log(100.0)
    assert grid.loc[1007] == np.log(101.0)


class FakeBook:
    def __init__(self):
        self.cut = 0

    def at(self, market_id, t, max_age=None):
        return (0.5, 0.6, 10.0, 10.0)

    def across_close(self, market_id, t0, t1):
        return False

    def side_ask(self, market_id, t, side):
        return (0.61, 8.0) if t < 1100.4 else (0.66, 4.0)


def test_execution_requires_five_shares_and_charges_fee():
    signals = pd.DataFrame([{
        "market_id": "m", "start_ts": m5.FREEZE_TS + 1, "t": 1100.0,
        "side": "Up", "winner": "Up", "z5": 3.0,
    }])

    got = m5.execute(signals, FakeBook(), lags=(0.3, 0.4), min_size=5.0).set_index("lag")

    assert bool(got.loc[0.3, "filled"])
    assert not bool(got.loc[0.4, "filled"])
    assert got.loc[0.3, "pnl"] == 1.0 - 0.61 - m5.bo.taker_fee(0.61)
    assert np.isnan(got.loc[0.4, "pnl"])


def test_combine_keeps_first_observation_per_market_and_lag():
    old = pd.DataFrame([{
        "market_id": "m", "lag": 0.4, "start_ts": m5.FREEZE_TS + 1,
        "t": m5.FREEZE_TS + 3, "price": 0.5,
    }])
    new = pd.DataFrame([
        {"market_id": "m", "lag": 0.4, "start_ts": m5.FREEZE_TS + 1, "t": m5.FREEZE_TS + 2, "price": 0.6},
        {"market_id": "n", "lag": 0.4, "start_ts": m5.FREEZE_TS - 1, "t": m5.FREEZE_TS + 4, "price": 0.7},
    ])

    got = m5.combine(old, new)

    assert len(got) == 1
    assert got.iloc[0]["price"] == 0.5


def test_missing_winner_never_becomes_a_loss():
    idx = pd.Index(range(900, 1301))
    grid = pd.Series(10.0, index=idx)
    grid.loc[1100:] = 10.01
    sigma = pd.Series(0.001, index=idx)
    markets = pd.DataFrame([{"market_id": "m", "start_ts": 1000.0, "winner": np.nan}])
    assert m5.signals_from_grid(markets, grid, sigma).empty


def test_empty_report_is_valid():
    text = m5.report(pd.DataFrame(columns=m5.RESULT_COLS))
    assert "0/1,000 fills" in text


def test_exact_poisson_binomial_tail():
    fills = pd.DataFrame({"price": [0.5, 0.5], "fee": [0.0, 0.0], "won": [1.0, 1.0]})
    assert m5.exact_fair_price_pvalue(fills) == 0.25


def test_freeze_manifest_matches_executable_strategy():
    manifest = m5.load_manifest(m5.Path(m5.__file__).parent / "forward/momentum5s-freeze.json")
    assert manifest["strategy_fingerprint"] == m5.strategy_fingerprint()


def test_stopping_sample_is_pinned_once(tmp_path, monkeypatch):
    monkeypatch.setattr(m5, "STOP_N", 2)
    rows = pd.DataFrame([
        {
            "market_id": f"m{i}", "start_ts": m5.PROTOCOL_FREEZE_TS + i, "t": m5.PROTOCOL_FREEZE_TS + i,
            "day": f"2026-10-0{i + 4}", "side": "Up", "z5": 3.0, "lag": 0.4, "won": 1.0,
            "price": 0.5, "fee": 0.0, "size": 10.0, "filled": True, "pnl": 0.5, "reason": "filled",
        }
        for i in range(2)
    ])
    path = tmp_path / "verdict.json"

    first, payload = m5.pin_verdict(rows, path)
    second, loaded = m5.pin_verdict(rows.assign(pnl=-0.5, won=0.0), path)

    assert payload is not None
    assert second["p"] == first["p"]
    assert second["ev"] == first["ev"]
    assert loaded["sample"] == payload["sample"]
