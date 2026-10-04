import pytest

import h_replay as hr


def test_live_fee_limit_is_frozen_at_decision():
    fair = 0.63

    fixed_limit = hr.limit_price(fair, theta=0.12)

    assert fixed_limit == 0.49
    assert fair - fixed_limit - hr.taker_fee(fixed_limit) >= 0.12
    assert fair - 0.50 - hr.taker_fee(0.50) < 0.12


def test_first_trigger_no_fill_is_not_replaced_by_later_executable_trigger():
    events = [
        {"kind": "snapshot", "market_id": "m", "receive_ms": 100, "source_ms": 100,
         "up_asks": [[0.33, 10]], "down_asks": [[0.67, 10]]},
        {"kind": "signal", "market_id": "m", "receive_ms": 106, "source_ms": 20,
         "direction": "Up", "fair": 0.63},
        {"kind": "snapshot", "market_id": "m", "receive_ms": 300, "source_ms": 300,
         "up_asks": [[0.50, 100]], "down_asks": [[0.50, 100]]},
        {"kind": "snapshot", "market_id": "m", "receive_ms": 2_105, "source_ms": 2_100,
         "up_asks": [[0.43, 100]], "down_asks": [[0.57, 100]]},
        {"kind": "signal", "market_id": "m", "receive_ms": 2_106, "source_ms": 50,
         "direction": "Up", "fair": 0.63},
    ]

    rows = hr.replay_events(events, hr.ReplayConfig(evaluation_ms=(300.0,)))

    assert len(rows) == 1
    assert rows[0]["signal_receive_ms"] == 106
    assert rows[0]["sent"] is True
    assert rows[0]["filled"] is False
    assert rows[0]["reason"] == "insufficient_effective_depth"


def test_partial_fak_fill_counts_as_fill_and_stops_same_market_reentry():
    events = [
        {"kind": "snapshot", "market_id": "m", "receive_ms": 90, "source_ms": 90,
         "up_asks": [[0.40, 3]], "down_asks": [[0.60, 10]]},
        {"kind": "signal", "market_id": "m", "receive_ms": 100, "source_ms": 100,
         "direction": "Up", "fair": 0.70},
        {"kind": "snapshot", "market_id": "m", "receive_ms": 450, "source_ms": 450,
         "up_asks": [[0.40, 3]], "down_asks": [[0.60, 10]]},
        {"kind": "signal", "market_id": "m", "receive_ms": 500, "source_ms": 500,
         "direction": "Down", "fair": 0.70},
    ]
    config = hr.ReplayConfig(evaluation_ms=(300.0,), local_path_ms=1.0,
                             retry_after_no_send_or_kill=True, reentry_enabled=False,
                             max_order_usd=10.0, signal_time_basis="exchange_source_timestamp")

    rows = hr.replay_events(events, config)

    assert len(rows) == 1
    assert rows[0]["reason"] == "partial_fill"
    assert rows[0]["filled"] is True
    assert rows[0]["filled_shares"] == pytest.approx(3.0)


def test_fixed_49_limit_accepts_cumulative_depth_but_rejects_50():
    events = [
        {"kind": "snapshot", "market_id": "enough", "receive_ms": 100,
         "up_asks": [[0.33, 10]], "down_asks": [[0.67, 10]]},
        {"kind": "snapshot", "market_id": "high", "receive_ms": 100,
         "up_asks": [[0.33, 10]], "down_asks": [[0.67, 10]]},
        {"kind": "signal", "market_id": "enough", "receive_ms": 106,
         "direction": "Up", "fair": 0.63},
        {"kind": "signal", "market_id": "high", "receive_ms": 106,
         "direction": "Up", "fair": 0.63},
        {"kind": "snapshot", "market_id": "enough", "receive_ms": 300,
         "up_asks": [[0.43, 2], [0.44, 3], [0.50, 100]], "down_asks": [[0.57, 100]]},
        {"kind": "snapshot", "market_id": "high", "receive_ms": 299,
         "up_asks": [[0.50, 100]], "down_asks": [[0.50, 100]]},
    ]

    rows = {row["market_id"]: row for row in hr.replay_events(
        events, hr.ReplayConfig(evaluation_ms=(300.0,))
    )}

    assert rows["enough"]["fixed_limit"] == 0.49
    assert rows["enough"]["eligible_depth"] == pytest.approx(5.0)
    assert rows["enough"]["match_price"] == 0.44
    assert rows["enough"]["filled"] is True
    assert rows["high"]["eligible_depth"] == 0.0
    assert rows["high"]["filled"] is False


def test_match_boundary_includes_book_lag_local_path_and_venue_hold():
    rows = hr.replay_events([
        {"kind": "snapshot", "market_id": "m", "receive_ms": 100,
         "up_asks": [[0.33, 10]], "down_asks": [[0.67, 10]]},
        {"kind": "signal", "market_id": "m", "receive_ms": 106,
         "direction": "Up", "fair": 0.63},
    ], hr.ReplayConfig(local_path_ms=0.86, evaluation_ms=(256.859, 256.86)))

    before, at = rows
    assert before["signal_ms"] == 0.0
    assert before["signal_to_send_ms"] == pytest.approx(106.86)
    assert before["reason"] == "venue_hold"
    assert at["evaluation_time_ms"] == pytest.approx(at["earliest_match_ms"])
    assert at["filled"] is True


def test_disconnect_invalidates_pending_and_reconnect_requires_full_snapshot():
    initial = {"up_asks": [[0.33, 10]], "down_asks": [[0.67, 10]]}
    executable = {"up_asks": [[0.43, 10]], "down_asks": [[0.57, 10]]}
    events = []
    for market_id in ("crossed", "partial", "half", "fresh"):
        events.append({"kind": "snapshot", "market_id": market_id, "receive_ms": 100, **initial})
    events += [
        {"kind": "signal", "market_id": "crossed", "receive_ms": 106,
         "direction": "Up", "fair": 0.63},
        {"kind": "disconnect", "market_id": "partial", "receive_ms": 101},
        {"kind": "reconnect", "market_id": "partial", "receive_ms": 102},
        {"kind": "update", "market_id": "partial", "receive_ms": 103,
         "direction": "Up", "asks": [[0.43, 10]]},
        {"kind": "signal", "market_id": "partial", "receive_ms": 106,
         "direction": "Up", "fair": 0.63},
        {"kind": "disconnect", "market_id": "half", "receive_ms": 101},
        {"kind": "reconnect", "market_id": "half", "receive_ms": 102},
        {"kind": "snapshot", "market_id": "half", "receive_ms": 103,
         "up_asks": [[0.43, 10]]},
        {"kind": "signal", "market_id": "half", "receive_ms": 106,
         "direction": "Up", "fair": 0.63},
        {"kind": "disconnect", "market_id": "fresh", "receive_ms": 101},
        {"kind": "reconnect", "market_id": "fresh", "receive_ms": 102},
        {"kind": "snapshot", "market_id": "fresh", "receive_ms": 103, **executable},
        {"kind": "signal", "market_id": "fresh", "receive_ms": 106,
         "direction": "Up", "fair": 0.63},
        {"kind": "disconnect", "market_id": "crossed", "receive_ms": 200},
        {"kind": "reconnect", "market_id": "crossed", "receive_ms": 210},
        {"kind": "snapshot", "market_id": "crossed", "receive_ms": 220, **executable},
    ]

    rows = {row["market_id"]: row for row in hr.replay_events(
        events, hr.ReplayConfig(evaluation_ms=(300.0,))
    )}

    assert rows["crossed"]["reason"] == "disconnect"
    assert rows["partial"]["reason"] == "book_unavailable_at_decision"
    assert rows["half"]["reason"] == "book_unavailable_at_decision"
    assert rows["fresh"]["filled"] is True


def test_reverse2_requires_an_actual_fill_and_two_seconds_from_previous_fill():
    events = [
        {"kind": "snapshot", "market_id": "m", "receive_ms": 100,
         "up_asks": [[0.33, 20]], "down_asks": [[0.67, 20]]},
        {"kind": "signal", "market_id": "m", "receive_ms": 106,
         "direction": "Up", "fair": 0.63},
        {"kind": "snapshot", "market_id": "m", "receive_ms": 300,
         "up_asks": [[0.43, 20]], "down_asks": [[0.57, 20]]},
        {"kind": "signal", "market_id": "m", "receive_ms": 1_106,
         "direction": "Down", "fair": 0.63},
        {"kind": "snapshot", "market_id": "m", "receive_ms": 2_104,
         "up_asks": [[0.57, 20]], "down_asks": [[0.43, 20]]},
        {"kind": "signal", "market_id": "m", "receive_ms": 2_105,
         "direction": "Down", "fair": 0.63},
        {"kind": "signal", "market_id": "m", "receive_ms": 2_106,
         "direction": "Down", "fair": 0.63},
        {"kind": "snapshot", "market_id": "m", "receive_ms": 4_104,
         "up_asks": [[0.43, 20]], "down_asks": [[0.57, 20]]},
        {"kind": "signal", "market_id": "m", "receive_ms": 4_106,
         "direction": "Up", "fair": 0.63},
    ]

    rows = hr.replay_events(
        events,
        hr.ReplayConfig(evaluation_ms=(300.0,), max_order_usd=10.0),
    )

    assert len(rows) == 3
    first, reverse, same = rows
    assert first["filled"] is True
    assert reverse["signal_ms"] == 2_000.0
    assert reverse["first_fill_direction"] == "Up"
    assert reverse["is_reverse"] is True
    assert reverse["size_multiplier"] == 2.0
    assert reverse["target_shares"] == 10.0
    assert reverse["filled"] is True
    assert same["is_reverse"] is False
    assert same["size_multiplier"] == 1.0
    assert same["target_shares"] == 5.0


def test_down_signal_uses_only_the_direct_down_ladder():
    rows = hr.replay_events([
        {"kind": "snapshot", "market_id": "m", "receive_ms": 100,
         "up_asks": [[0.10, 100]], "down_asks": [[0.41, 10]]},
        {"kind": "signal", "market_id": "m", "receive_ms": 106,
         "direction": "Down", "fair": 0.63},
        {"kind": "snapshot", "market_id": "m", "receive_ms": 300,
         "up_asks": [[0.10, 100]], "down_asks": [[0.44, 5], [0.50, 100]]},
    ], hr.ReplayConfig(evaluation_ms=(300.0,)))

    assert rows[0]["direct_token"] == "Down"
    assert rows[0]["decision_ask"] == 0.41
    assert rows[0]["match_price"] == 0.44
    assert rows[0]["eligible_depth"] == 5.0
    assert rows[0]["filled"] is True


def test_receive_time_is_causal_but_match_book_is_looked_up_on_source_clock():
    events = [
        {"kind": "snapshot", "market_id": "m", "receive_ms": 100, "source_ms": 100,
         "up_asks": [[0.33, 10]], "down_asks": [[0.67, 10]]},
        {"kind": "signal", "market_id": "m", "receive_ms": 106, "source_ms": 0,
         "direction": "Up", "fair": 0.63},
        {"kind": "snapshot", "market_id": "m", "receive_ms": 350, "source_ms": 250,
         "up_asks": [[0.43, 10]], "down_asks": [[0.57, 10]]},
        {"kind": "snapshot", "market_id": "m", "receive_ms": 360, "source_ms": 301,
         "up_asks": [[0.50, 100]], "down_asks": [[0.50, 100]]},
    ]

    row = hr.replay_events(events, hr.ReplayConfig(evaluation_ms=(300.0,)))[0]

    assert row["ordering_clock"] == "receive_ms"
    assert row["match_book_clock"] == "source_ms"
    assert row["signal_source_ms"] == 0
    assert row["book_receive_ms"] == 350
    assert row["book_source_ms"] == 250
    assert row["filled"] is True


def test_same_receive_batch_advances_source_watermark_only_after_all_books_are_applied():
    machine = hr.HReplay(hr.ReplayConfig(evaluation_ms=(300.0,)))
    for market_id in ("other", "target"):
        machine.feed({
            "kind": "snapshot",
            "market_id": market_id,
            "receive_ms": 100,
            "source_ms": 100,
            "up_asks": [[0.33, 10]],
            "down_asks": [[0.67, 10]],
        })
    machine.feed({
        "kind": "signal",
        "market_id": "target",
        "receive_ms": 106,
        "direction": "Up",
        "fair": 0.63,
    })

    assert machine.feed({
        "kind": "snapshot",
        "market_id": "other",
        "receive_ms": 350,
        "source_ms": 301,
        "up_asks": [[0.50, 10]],
        "down_asks": [[0.50, 10]],
        "advance_watermark": False,
    }) == []
    assert machine.feed({
        "kind": "snapshot",
        "market_id": "target",
        "receive_ms": 350,
        "source_ms": 250,
        "up_asks": [[0.43, 10]],
        "down_asks": [[0.57, 10]],
        "advance_watermark": False,
    }) == []

    rows = machine.feed({
        "kind": "watermark",
        "receive_ms": 350,
        "source_ms": 301,
    })

    assert len(rows) == 1
    assert rows[0]["book_source_ms"] == 250
    assert rows[0]["filled"] is True


def test_frozen_manifest_preserves_golden_live_evidence_without_edge_backfill():
    frozen = hr.load_freeze()
    config = hr.config_from_freeze(frozen)
    evidence = frozen["golden_live_evidence"]

    assert config.book_lag_ms == 0.0
    assert config.local_path_ms == 0.9
    assert config.order_wire_ms == 15.0
    assert config.venue_hold_ms == 150.0
    assert config.evaluation_ms == (300.0, 350.0, 400.0, 500.0)
    assert frozen["semantics"]["candidate_sources"] == [
        "binance_spot_trade", "binance_futures_bookTicker",
    ]
    assert frozen["semantics"]["retry_after_no_send_or_kill"] is True
    assert config.signal_time_basis == "exchange_source_timestamp"
    assert config.max_order_usd == 5.0
    assert config.max_orders_per_min == 1
    assert config.reentry_enabled is False
    assert frozen["semantics"]["fill_qualification"] == "all_positive_FAK_partial_and_full_fills_count"
    assert evidence["decision_ask"] == 0.33
    assert evidence["fixed_limit"] == 0.49
    assert evidence["filled_shares"] == 5.697675
    assert evidence["fill_price"] == 0.43
    assert evidence["signal_to_match_ms"] == 481.75
    assert evidence["trial_edge"] is None


def test_timestamped_current_h_uses_source_clock_and_rate_limits_retries():
    frozen = hr.load_freeze()
    config = hr.config_from_freeze(frozen)
    config = hr.ReplayConfig(**{**config.__dict__, "evaluation_ms": (300.0,)})
    events = [
        {"kind": "snapshot", "market_id": "first", "receive_ms": 100, "source_ms": 0,
         "up_asks": [[0.33, 10]], "down_asks": [[0.67, 10]]},
        {"kind": "snapshot", "market_id": "second", "receive_ms": 100, "source_ms": 0,
         "up_asks": [[0.33, 10]], "down_asks": [[0.67, 10]]},
        {"kind": "signal", "market_id": "first", "receive_ms": 106, "source_ms": 0,
         "direction": "Up", "fair": 0.63},
        {"kind": "signal", "market_id": "second", "receive_ms": 1_106, "source_ms": 1_000,
         "direction": "Up", "fair": 0.63},
    ]

    first, second = hr.replay_events(events, config)

    assert first["signal_ms"] == 0
    assert first["signal_to_send_ms"] == pytest.approx(106.9)
    assert first["sent"] is True
    assert second["sent"] is False and second["reason"] == "rate"


def test_golden_fill_replays_fixed_maker_amount_at_price_improvement():
    row = hr.replay_events([
        {"kind": "snapshot", "market_id": "golden", "receive_ms": 100,
         "up_asks": [[0.33, 20]], "down_asks": [[0.67, 20]]},
        {"kind": "signal", "market_id": "golden", "receive_ms": 106,
         "direction": "Up", "fair": 0.63, "trial_edge": None},
        {"kind": "snapshot", "market_id": "golden", "receive_ms": 481.75,
         "up_asks": [[0.43, 20]], "down_asks": [[0.57, 20]]},
    ], hr.ReplayConfig(evaluation_ms=(481.75,)))[0]

    assert row["decision_ask"] == 0.33
    assert row["fixed_limit"] == 0.49
    assert row["maker_usd"] == 2.45
    assert round(row["filled_shares"], 6) == 5.697675
    assert row["fill_price"] == pytest.approx(0.43)
    assert row["evaluation_time_ms"] == 481.75
    assert row["trial_edge"] is None


def test_low_price_pilot_preserves_one_dollar_venue_minimum():
    row = hr.replay_events([
        {"kind": "snapshot", "market_id": "low", "receive_ms": 100,
         "up_asks": [[0.05, 100]], "down_asks": [[0.95, 100]]},
        {"kind": "signal", "market_id": "low", "receive_ms": 106,
         "direction": "Up", "fair": 0.18},
    ], hr.ReplayConfig(evaluation_ms=(300.0,)))[0]

    assert row["fixed_limit"] == 0.05
    assert row["target_shares"] == 5.0
    assert row["signed_size"] == 21.0
    assert row["maker_usd"] == 1.05


def test_pilot_order_cap_blocks_five_shares_above_78_cents():
    row = hr.replay_events([
        {"kind": "snapshot", "market_id": "high", "receive_ms": 100,
         "up_asks": [[0.80, 100]], "down_asks": [[0.20, 100]]},
        {"kind": "signal", "market_id": "high", "receive_ms": 106,
         "direction": "Up", "fair": 0.98},
    ], hr.ReplayConfig(evaluation_ms=(300.0,)))[0]

    assert row["fixed_limit"] == 0.85
    assert row["reason"] == "order_size_cap"
    assert row["filled"] is False


def test_evaluations_do_not_use_future_health_or_late_books():
    events = []
    for market_id in ("filled", "late"):
        events += [
            {"kind": "snapshot", "market_id": market_id, "receive_ms": 100,
             "up_asks": [[0.33, 10]], "down_asks": [[0.67, 10]]},
            {"kind": "signal", "market_id": market_id, "receive_ms": 106,
             "direction": "Up", "fair": 0.63},
        ]
    events += [
        {"kind": "snapshot", "market_id": "filled", "receive_ms": 299, "source_ms": 299,
         "up_asks": [[0.43, 10]], "down_asks": [[0.57, 10]]},
        {"kind": "snapshot", "market_id": "late", "receive_ms": 299, "source_ms": 299,
         "up_asks": [[0.50, 10]], "down_asks": [[0.50, 10]]},
        {"kind": "disconnect", "market_id": "filled", "receive_ms": 301},
        {"kind": "snapshot", "market_id": "late", "receive_ms": 301, "source_ms": 250,
         "up_asks": [[0.43, 10]], "down_asks": [[0.57, 10]]},
    ]

    rows = {row["market_id"]: row for row in hr.replay_events(
        events, hr.ReplayConfig(evaluation_ms=(300.0,))
    )}

    assert rows["filled"]["filled"] is True
    assert rows["filled"]["future_health_checked"] is False
    assert rows["late"]["filled"] is False
    assert rows["late"]["book_receive_ms"] == 299


def test_feed_disconnect_without_market_id_clears_every_market():
    events = []
    for market_id in ("a", "b"):
        events += [
            {"kind": "snapshot", "market_id": market_id, "receive_ms": 100,
             "up_asks": [[0.33, 10]], "down_asks": [[0.67, 10]]},
            {"kind": "signal", "market_id": market_id, "receive_ms": 106,
             "direction": "Up", "fair": 0.63},
        ]
    events.append({"kind": "disconnect", "receive_ms": 200})

    rows = hr.replay_events(events, hr.ReplayConfig(evaluation_ms=(300.0,)))

    assert {row["market_id"] for row in rows} == {"a", "b"}
    assert {row["reason"] for row in rows} == {"disconnect"}
