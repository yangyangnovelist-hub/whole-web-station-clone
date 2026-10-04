import csv
import gzip
import json
import math
import sys

import pytest

import h_replay as replay
import h_replay_run as run


def test_signal_freeze_loads_deployed_bookticker_parameters():
    config = run.signal_config_from_freeze(replay.load_freeze())

    assert config.book_lag_ms == 0.0
    assert config.z0 == 2.0
    assert config.anchor_s == 2.0
    assert config.sigma_window_s == 600
    assert config.sigma_min_observations == 300


def test_fair_price_scales_endpoint_move_by_unknown_twap_fraction():
    t_rel = 270.0  # 30 of the 60 closing-TWAP seconds remain unknown.
    sigma = 0.01
    move = 0.01
    scale = run.twap_std_factor(t_rel) * sigma

    fair = run.fair_price(0.5, move, sigma, t_rel, 1)

    assert run.twap_move_factor(t_rel) == pytest.approx(0.5)
    assert fair == pytest.approx(run._NORMAL.cdf(move * 0.5 / scale))
    assert fair < run._NORMAL.cdf(move / scale)


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
    assert len(trigger.timestamps) == len(trigger.log_prices)


def test_timestamped_first_futures_uses_exchange_clock_and_spot_trade_sigma():
    config = run.SignalConfig(
        book_lag_ms=0.0,
        z0=5.0,
        tau_hi_s=300.0,
        tau_lo_s=0.0,
        sigma_window_s=10,
        sigma_min_observations=3,
    )
    trigger = run.TimestampedFirstTrigger(config)
    for second, price in enumerate((100.0, 100.01, 100.0, 100.01, 100.0, 100.01), 100):
        assert trigger.update_trade(second * 1_000, price) is None
    assert trigger.update_futures(104_000, 99.99, 100.01) is None
    assert trigger.update_futures(105_000, 99.99, 100.01) is None

    candidate = trigger.update_futures(106_000, 101.99, 102.01)

    assert candidate is not None
    assert candidate.timestamp_s == 106.0
    assert candidate.direction == 1
    assert math.isfinite(candidate.sigma)


def test_timestamped_first_rejects_a_regressed_source_clock():
    trigger = run.TimestampedFirstTrigger(run.SignalConfig(
        tau_hi_s=300.0, tau_lo_s=0.0, sigma_window_s=3, sigma_min_observations=2,
    ))

    assert trigger.update_source("deribit_quote", 2_000, 100.0) is None
    assert trigger.update_source("deribit_quote", 1_999, 101.0) is None
    assert trigger.source_timestamps["deribit_quote"] == [2.0]


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
    spot = [
        {"kind": "spot_connection", "recv_ms": 99_000.0, "source_ts_ms": None,
         "seq": 0, "stream": "spot", "stream_sequence": 0, "connection_epoch": 1},
        {"kind": "futures_connection", "recv_ms": 99_001.0, "source_ts_ms": None,
         "seq": 1, "stream": "futures", "stream_sequence": 0, "connection_epoch": 1},
    ]
    for sequence, (second, price) in enumerate(zip(
        range(100, 107),
        (100.0, 100.01, 100.0, 100.01, 100.0, 100.01, 100.0),
    ), 1):
        spot.append({
            "kind": "spot_trade",
            "recv_ms": second * 1_000 + 100,
            "source_ts_ms": second * 1_000,
            "price": price,
            "seq": 0,
            "stream": "spot", "stream_sequence": sequence, "connection_epoch": 1,
        })
    spot += [
        {"kind": "futures_bbo", "recv_ms": 104_106.0, "source_ts_ms": 104_000.0,
         "bid": 99.99, "ask": 100.01, "seq": 0, "stream": "futures",
         "stream_sequence": 1, "connection_epoch": 1},
        {"kind": "futures_bbo", "recv_ms": 105_106.0, "source_ts_ms": 105_000.0,
         "bid": 99.99, "ask": 100.01, "seq": 0, "stream": "futures",
         "stream_sequence": 2, "connection_epoch": 1},
        {"kind": "futures_bbo", "recv_ms": 106_106.0, "source_ts_ms": 106_000.0,
         "bid": 101.99, "ask": 102.01, "seq": 0, "stream": "futures",
         "stream_sequence": 3, "connection_epoch": 1},
    ]
    spot.sort(key=lambda row: row["recv_ms"])
    for sequence, row in enumerate(spot):
        row["seq"] = sequence
    clob = [
        {"kind": "clob_connection", "recv_ms": 103_000.0, "source_ts_ms": None, "seq": 0},
        {"kind": "clob_snapshot", "recv_ms": 103_010.0, "source_ts_ms": 103_000.0,
         "market_id": "m", "asset_id": "up", "bids": [{"price": "0.32", "size": "10"}],
         "asks": [{"price": "0.33", "size": "10"}], "seq": 1},
        {"kind": "clob_snapshot", "recv_ms": 103_010.0, "source_ts_ms": 103_000.0,
         "market_id": "m", "asset_id": "down", "bids": [{"price": "0.67", "size": "10"}],
         "asks": [{"price": "0.68", "size": "10"}], "seq": 2},
        {"kind": "clob_snapshot", "recv_ms": 105_950.0, "source_ts_ms": 105_900.0,
         "market_id": "m", "asset_id": "up", "bids": [{"price": "0.32", "size": "10"}],
         "asks": [{"price": "0.33", "size": "10"}], "seq": 3},
        {"kind": "clob_snapshot", "recv_ms": 105_950.0, "source_ts_ms": 105_900.0,
         "market_id": "m", "asset_id": "down", "bids": [{"price": "0.67", "size": "10"}],
         "asks": [{"price": "0.68", "size": "10"}], "seq": 4},
        {"kind": "clob_snapshot", "recv_ms": 106_290.0, "source_ts_ms": 106_280.0,
         "market_id": "m", "asset_id": "up", "bids": [{"price": "0.42", "size": "10"}],
         "asks": [{"price": "0.43", "size": "10"}], "seq": 5},
        {"kind": "clob_snapshot", "recv_ms": 106_290.0, "source_ts_ms": 106_280.0,
         "market_id": "m", "asset_id": "down", "bids": [{"price": "0.57", "size": "10"}],
         "asks": [{"price": "0.58", "size": "10"}], "seq": 6},
    ]
    execution = replay.ReplayConfig(evaluation_ms=(300.0,), max_order_usd=10.0,
                                    signal_time_basis="exchange_source_timestamp")
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
    assert report["fills_by_source"] == {"futures_book_ticker": 1}
    assert counters["candidate_source_futures_book_ticker"] == 1


def test_same_receipt_millisecond_processes_spot_before_clob():
    spot = [{"kind": "spot_bbo", "recv_ms": 100.0, "seq": 1}]
    clob = [{"kind": "clob_connection", "recv_ms": 100.0, "seq": 0}]

    assert [event["kind"] for event in run._merged_events(spot, clob)] == ["spot_bbo", "clob_batch"]


def test_strict_replay_dispatches_optional_race_sources_without_enabling_them_by_default():
    source = [
        {"kind": "futures_trade_connection", "recv_ms": 1.0, "source_ts_ms": None, "seq": 0,
         "stream": "futures_trade", "stream_sequence": 0, "connection_epoch": 1},
        {"kind": "futures_trade", "recv_ms": 2.0, "source_ts_ms": 1.0, "seq": 1,
         "stream": "futures_trade", "stream_sequence": 1, "connection_epoch": 1,
         "price": 100.0, "size": 1.0},
        {"kind": "futures_trade_disconnect", "recv_ms": 3.0, "source_ts_ms": None, "seq": 2,
         "stream": "futures_trade", "stream_sequence": 2, "connection_epoch": 1},
        {"kind": "deribit_connection", "recv_ms": 4.0, "source_ts_ms": None, "seq": 3,
         "stream": "deribit", "stream_sequence": 0, "connection_epoch": 1},
        {"kind": "deribit_quote", "recv_ms": 5.0, "source_ts_ms": 4.0, "seq": 4,
         "stream": "deribit", "stream_sequence": 1, "connection_epoch": 1,
         "bid": 99.0, "ask": 101.0},
        {"kind": "deribit_disconnect", "recv_ms": 6.0, "source_ts_ms": None, "seq": 5,
         "stream": "deribit", "stream_sequence": 2, "connection_epoch": 1},
    ]

    rows, counters = run.replay_normalized(
        source, [], [], {}, replay.ReplayConfig(), run.SignalConfig(),
        trigger_sources={"spot_trade", "futures_book_ticker"},
    )

    assert rows == []
    assert counters["futures_trades"] == 1
    assert counters["deribit_quotes"] == 1


def test_effective_book_uses_mirrored_opposite_bid_without_double_counting():
    market = run.DirectMarketBook("m", 0, "up", "down")
    market.up.replace([{"price": "0.30", "size": "4"}],
                      [{"price": "0.40", "size": "2"}], 1.0)
    market.down.replace([{"price": "0.60", "size": "7"}],
                        [{"price": "0.70", "size": "5"}], 1.0)

    assert market.effective_asks("Up") == [[0.4, 7.0]]
    assert market.effective_asks("Down") == [[0.7, 5.0]]
    assert market.midpoint() == pytest.approx(0.35)


def test_replay_archive_auto_detects_standard_forward_contract(tmp_path, monkeypatch):
    strict = tmp_path / "strict"
    strict.mkdir()
    with gzip.open(strict / "market_registry.csv.gz", "wt", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=[
            "market_id", "start_ts", "up_token_id", "down_token_id", "updated_at",
        ])
        writer.writeheader()
        writer.writerow({"market_id": "m", "start_ts": 0, "up_token_id": "up",
                         "down_token_id": "down", "updated_at": 0})
    with gzip.open(strict / "market_outcomes.csv.gz", "wt", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=[
            "market_id", "winner", "resolution_ts", "source", "recorded_at",
        ])
        writer.writeheader()
        writer.writerow({"market_id": "m", "winner": "Up", "resolution_ts": 300,
                         "source": "gamma", "recorded_at": 301})
    source_rows = [
        {"kind": "spot_connection", "recv_ms": 80.0, "source_ts_ms": None, "seq": 0,
         "stream": "spot", "stream_sequence": 0, "connection_epoch": 1},
        {"kind": "spot_trade", "recv_ms": 81.0, "source_ts_ms": 80.0, "seq": 1,
         "stream": "spot", "stream_sequence": 1, "connection_epoch": 1, "price": 1.0, "size": 1.0},
        {"kind": "spot_disconnect", "recv_ms": 82.0, "source_ts_ms": None, "seq": 2,
         "stream": "spot", "stream_sequence": 2, "connection_epoch": 1},
        {"kind": "futures_connection", "recv_ms": 83.0, "source_ts_ms": None, "seq": 3,
         "stream": "futures", "stream_sequence": 0, "connection_epoch": 1},
        {"kind": "futures_bbo", "recv_ms": 84.0, "source_ts_ms": 83.0, "seq": 4,
         "stream": "futures", "stream_sequence": 1, "connection_epoch": 1, "bid": 1.0, "ask": 2.0},
        {"kind": "futures_disconnect", "recv_ms": 85.0, "source_ts_ms": None, "seq": 5,
         "stream": "futures", "stream_sequence": 2, "connection_epoch": 1},
    ]
    with gzip.open(strict / "source_events.jsonl.gz", "wt") as stream:
        stream.writelines(json.dumps(row) + "\n" for row in source_rows)
    with gzip.open(strict / "clob_events.jsonl.gz", "wt") as stream:
        stream.write(json.dumps({"kind": "clob_connection", "recv_ms": 90.0, "source_ts_ms": None,
                                 "seq": 0, "connection_epoch": 1, "token_count": 2}) + "\n")
        stream.write(json.dumps({"kind": "clob_error", "recv_ms": 91.0, "source_ts_ms": None,
                                 "seq": 1, "connection_epoch": 1, "error": "closed"}) + "\n")
    (strict / "manifest.json").write_text(json.dumps({
        "schema": "polymarket-5m-strict-replay-v2", "complete": True,
        "run_id": "run-123",
        "collector_region": "eu-west-1",
        "recorder_complete": True, "missing_resolved_market_ids": [],
        "counts": {"markets": 1, "outcomes": 1, "spot_connection": 1, "spot_trade": 1,
                   "spot_disconnect": 1, "futures_connection": 1, "futures_bbo": 1,
                   "futures_disconnect": 1,
                   "clob_connection": 1, "clob_snapshot": 0,
                   "clob_price_change": 0, "clob_error": 1},
    }))
    seen = {}

    def fake(spot, clob, mappings, outcomes, execution, signal):
        seen["spot"] = list(spot)
        seen["clob"] = list(clob)
        seen["mappings"] = list(mappings)
        seen["outcomes"] = outcomes
        return [{"market_id": "m", "winner": "Up"}], {}

    monkeypatch.setattr(run, "replay_normalized", fake)
    monkeypatch.setattr(run, "summarize", lambda *_: {})
    rows_out = tmp_path / "rows.jsonl"
    result = run.replay_archive(tmp_path, rows_out=rows_out)

    assert result["dataset"]["sample_scope"] == "standard_forward_artifact"
    assert result["dataset"]["paper_gate_eligible"] is True
    assert seen["spot"][0]["kind"] == "spot_connection"
    assert seen["clob"][0]["connection_epoch"] == 1
    assert seen["mappings"][0]["up_token_id"] == "up" and seen["outcomes"] == {"m": "Up"}
    assert json.loads(rows_out.read_text())["observation_run_id"] == "run-123"


def test_cli_require_paper_gate_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(run, "replay_archive", lambda *_: {
        "dataset": {"paper_gate_eligible": False, "pending_signal_market_outcomes": ["m"]},
    })
    monkeypatch.setattr(sys, "argv", ["h_replay_run.py", "--archive", "unused", "--require-paper-gate"])

    with pytest.raises(SystemExit, match="not paper-gate eligible"):
        run.main()
    assert '"paper_gate_eligible": false' in capsys.readouterr().out
