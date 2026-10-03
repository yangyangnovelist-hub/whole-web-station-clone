import math

import h_replay as replay
import h_replay_run as run


def test_signal_freeze_loads_deployed_bookticker_parameters():
    config = run.signal_config_from_freeze(replay.load_freeze())

    assert config.book_lag_ms == 106.0
    assert config.z0 == 2.0
    assert config.anchor_s == 2.0
    assert config.sigma_window_s == 600
    assert config.sigma_min_observations == 300


def test_bookticker_clock_is_receipt_minus_106ms_and_uses_trade_sigma():
    config = run.SignalConfig(
        book_lag_ms=106.0,
        z0=5.0,
        tau_hi_s=300.0,
        tau_lo_s=0.0,
        sigma_window_s=10,
        sigma_min_observations=3,
    )
    trigger = run.BookTickerTrigger(config)
    for second, price in enumerate((100.0, 100.01, 100.0, 100.01, 100.0, 100.01), 100):
        trigger.update_trade(second * 1_000, price)
    assert trigger.update_bbo(104_106.0, 99.99, 100.01) is None
    assert trigger.update_bbo(105_106.0, 99.99, 100.01) is None

    candidate = trigger.update_bbo(106_106.0, 101.99, 102.01)

    assert candidate is not None
    assert candidate.timestamp_s == 106.0
    assert candidate.slot == 0
    assert candidate.direction == 1
    assert math.isfinite(candidate.sigma)


def test_direct_token_book_quarantines_an_unreproducible_venue_bba():
    token = run.TokenBook()
    token.replace(
        [{"price": "0.30", "size": "5"}],
        [{"price": "0.40", "size": "5"}],
        1_000,
    )

    token.change("SELL", 0.41, 2.0, 1_001, 0.30, 0.39)

    assert token.ready is False


def test_strict_normalized_replay_generates_and_scores_one_executable_signal():
    mappings = [{
        "market_id": "m",
        "slot": 0,
        "up_token_id": "up",
        "down_token_id": "down",
    }]
    spot = []
    for sequence, (second, price) in enumerate(zip(
        range(100, 107),
        (100.0, 100.01, 100.0, 100.01, 100.0, 100.01, 100.0),
    )):
        spot.append({
            "kind": "spot_trade",
            "recv_ms": second * 1_000 + 100,
            "source_ts_ms": second * 1_000,
            "price": price,
            "seq": sequence,
        })
    spot += [
        {"kind": "spot_bbo", "recv_ms": 104_106.0, "source_ts_ms": None,
         "bid": 99.99, "ask": 100.01, "seq": 20},
        {"kind": "spot_bbo", "recv_ms": 105_106.0, "source_ts_ms": None,
         "bid": 99.99, "ask": 100.01, "seq": 21},
        {"kind": "spot_bbo", "recv_ms": 106_106.0, "source_ts_ms": None,
         "bid": 101.99, "ask": 102.01, "seq": 22},
    ]
    clob = [
        {"kind": "clob_connection", "recv_ms": 103_000.0, "source_ts_ms": None, "seq": 0},
        {"kind": "clob_snapshot", "recv_ms": 103_010.0, "source_ts_ms": 103_000.0,
         "market_id": "m", "asset_id": "up", "bids": [{"price": "0.32", "size": "10"}],
         "asks": [{"price": "0.33", "size": "10"}], "seq": 1},
        {"kind": "clob_snapshot", "recv_ms": 103_010.0, "source_ts_ms": 103_000.0,
         "market_id": "m", "asset_id": "down", "bids": [{"price": "0.66", "size": "10"}],
         "asks": [{"price": "0.67", "size": "10"}], "seq": 2},
        {"kind": "clob_snapshot", "recv_ms": 105_950.0, "source_ts_ms": 105_900.0,
         "market_id": "m", "asset_id": "up", "bids": [{"price": "0.32", "size": "10"}],
         "asks": [{"price": "0.33", "size": "10"}], "seq": 3},
        {"kind": "clob_snapshot", "recv_ms": 105_950.0, "source_ts_ms": 105_900.0,
         "market_id": "m", "asset_id": "down", "bids": [{"price": "0.66", "size": "10"}],
         "asks": [{"price": "0.67", "size": "10"}], "seq": 4},
        {"kind": "clob_snapshot", "recv_ms": 106_290.0, "source_ts_ms": 106_280.0,
         "market_id": "m", "asset_id": "up", "bids": [{"price": "0.42", "size": "10"}],
         "asks": [{"price": "0.43", "size": "10"}], "seq": 5},
        {"kind": "clob_snapshot", "recv_ms": 106_290.0, "source_ts_ms": 106_280.0,
         "market_id": "m", "asset_id": "down", "bids": [{"price": "0.56", "size": "10"}],
         "asks": [{"price": "0.57", "size": "10"}], "seq": 6},
    ]
    execution = replay.ReplayConfig(evaluation_ms=(300.0,), max_order_usd=10.0)
    signal = run.SignalConfig(
        book_lag_ms=106.0,
        z0=5.0,
        tau_hi_s=300.0,
        tau_lo_s=0.0,
        sigma_window_s=10,
        sigma_min_observations=3,
    )

    rows, counters = run.replay_normalized(spot, clob, mappings, {"m": "Up"}, execution, signal)
    report = run.summarize(rows, counters)["latencies"]["300"]

    assert report["signals"] == 1
    assert report["sent"] == 1
    assert report["fills"] == 1
    assert report["net_ev_per_share"] > 0
    assert report["exact_p"] < 1


def test_same_receipt_millisecond_processes_spot_before_clob():
    spot = [{"kind": "spot_bbo", "recv_ms": 100.0, "seq": 1}]
    clob = [{"kind": "clob_connection", "recv_ms": 100.0, "seq": 0}]

    assert [event["kind"] for event in run._merged_events(spot, clob)] == ["spot_bbo", "clob_batch"]
