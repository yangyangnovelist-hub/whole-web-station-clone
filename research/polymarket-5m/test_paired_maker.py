from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import paired_maker as pm
import pytest

SLOT = 1_800_000_000
BASE_MS = (SLOT + 60) * 1_000.0
MARKET = "market-1"
UP = "up-token"
DOWN = "down-token"


def _market() -> dict:
    return {
        "kind": "market",
        "recv_ms": BASE_MS - 10,
        "market_id": MARKET,
        "slot": SLOT,
        "up_token_id": UP,
        "down_token_id": DOWN,
    }


def _snapshot(token: str, recv_ms: float, *, bid: float, bid_size: float, ask: float) -> dict:
    return {
        "kind": "snapshot",
        "recv_ms": recv_ms,
        "source_ts_ms": recv_ms - 2,
        "market_id": MARKET,
        "asset_id": token,
        "bids": [{"price": str(bid), "size": str(bid_size)}],
        "asks": [{"price": str(ask), "size": "20"}],
        "ambiguous_receipt": False,
    }


def _change(token: str, recv_ms: float, *, price: float, size: float) -> dict:
    return {
        "kind": "price_change",
        "recv_ms": recv_ms,
        "source_ts_ms": recv_ms - 2,
        "market_id": MARKET,
        "changes": [{
            "asset_id": token,
            "side": "BUY",
            "price": str(price),
            "size": str(size),
        }],
        "ambiguous_receipt": False,
    }


def _sell(
    token: str,
    recv_ms: float,
    *,
    price: float = 0.47,
    size: float = 5.0,
    side: str = "SELL",
) -> dict:
    return {
        "kind": "trade",
        "recv_ms": recv_ms,
        "source_ts_ms": recv_ms - 2,
        "market_id": MARKET,
        "asset_id": token,
        "price": price,
        "size": size,
        "side": side,
        "transaction_hash": f"tx-{recv_ms}-{token}",
        "ambiguous_receipt": False,
    }


def _ready(config: pm.PairedMakerConfig | None = None, *, down_ask: float = 0.50) -> pm.PairedMakerReplay:
    replay = pm.PairedMakerReplay(config)
    replay.on_event(_market())
    replay.on_event({"kind": "connection", "recv_ms": BASE_MS - 9, "epoch": 1})
    replay.on_event(_snapshot(UP, BASE_MS, bid=0.47, bid_size=10, ask=0.50))
    replay.on_event(_snapshot(DOWN, BASE_MS, bid=0.47, bid_size=8, ask=down_ask))
    replay.flush(BASE_MS + replay.config.placement_latency_ms)
    return replay


def test_equal_complementary_fills_lock_complete_set_profit() -> None:
    replay = _ready()
    replay.on_event(_sell(UP, BASE_MS + 40, size=15))
    replay.on_event(_sell(DOWN, BASE_MS + 41, size=13))

    row = replay.finish({MARKET: "Down"})[0]

    assert row["eligible"] is True
    assert row["up_filled"] == pytest.approx(5)
    assert row["down_filled"] == pytest.approx(5)
    assert row["paired_shares"] == pytest.approx(5)
    assert row["locked_edge_per_pair"] == pytest.approx(0.06)
    assert row["pnl"] == pytest.approx(0.30)


def test_maker_fill_events_preserve_exact_cross_transport_order() -> None:
    replay = _ready()
    first_ns = int((BASE_MS + 40) * 1_000_000)
    up = _sell(UP, BASE_MS + 40, size=15)
    down = _sell(DOWN, BASE_MS + 40, size=13)
    up["recv_ns"] = first_ns
    down["recv_ns"] = first_ns + 89

    replay.on_event(up)
    replay.on_event(down)
    row = replay.finish({MARKET: "Down"})[0]

    assert row["maker_fill_events"] == [
        {"recv_ns": first_ns, "sequence": 1, "observation_basis": "public_clob_trade_queue_inference", "side": "Up", "price": 0.47, "shares": 5.0},
        {"recv_ns": first_ns + 89, "sequence": 2, "observation_basis": "public_clob_trade_queue_inference", "side": "Down", "price": 0.47, "shares": 5.0},
    ]
    assert row["maker_fill_observation_basis"] == "public_clob_trade_queue_inference"


def test_same_nanosecond_maker_fills_preserve_observed_input_order() -> None:
    replay = _ready()
    recv_ns = int((BASE_MS + 40) * 1_000_000)
    down = _sell(DOWN, BASE_MS + 40, size=13)
    up = _sell(UP, BASE_MS + 40, size=15)
    down["recv_ns"] = recv_ns
    up["recv_ns"] = recv_ns

    replay.on_event(down)
    replay.on_event(up)
    row = replay.finish({MARKET: "Down"})[0]

    assert [event["side"] for event in row["maker_fill_events"]] == ["Down", "Up"]
    assert [event["sequence"] for event in row["maker_fill_events"]] == [1, 2]


def test_displayed_queue_cancellation_gives_no_priority_credit() -> None:
    replay = _ready()
    replay.on_event(_change(UP, BASE_MS + 31, price=0.47, size=2))
    replay.on_event(_sell(UP, BASE_MS + 40, size=5))
    replay.on_event(_sell(UP, BASE_MS + 41, size=5))
    assert replay.market(MARKET).up_order.filled == 0

    replay.on_event(_sell(UP, BASE_MS + 42, size=5))
    replay.flush(BASE_MS + 42)

    assert replay.market(MARKET).up_order.filled == pytest.approx(5)


def test_trade_side_must_confirm_sell_and_price_through_is_size_capped() -> None:
    replay = _ready()
    replay.on_event(_sell(UP, BASE_MS + 40, price=0.46, size=0.1, side="BUY"))
    replay.on_event(_sell(UP, BASE_MS + 41, price=0.46, size=0.1))
    replay.flush(BASE_MS + 41)

    assert replay.market(MARKET).up_order.filled == pytest.approx(0.1)

    duplicate = _sell(UP, BASE_MS + 42, price=0.46, size=0.1)
    duplicate["transaction_hash"] = "same-transaction"
    replay.on_event(duplicate)
    duplicate_later = dict(duplicate, recv_ms=BASE_MS + 43)
    replay.on_event(duplicate_later)
    replay.flush(BASE_MS + 43)
    assert replay.market(MARKET).up_order.filled == pytest.approx(0.2)


def test_queue_cancellation_credit_is_an_explicit_sensitivity_arm() -> None:
    strict = _ready()
    credited = _ready(replace(pm.PairedMakerConfig(), queue_cancellation_credit=True))
    for replay in (strict, credited):
        replay.on_event(_change(UP, BASE_MS + 31, price=0.47, size=2))
        replay.on_event(_sell(UP, BASE_MS + 32, size=7))
        replay.flush(BASE_MS + 32)

    assert strict.market(MARKET).up_order.filled == 0
    assert credited.market(MARKET).up_order.filled == pytest.approx(5)


def test_external_warning_cancels_both_and_exact_tie_fills_first() -> None:
    replay = _ready()
    replay.on_event({
        "kind": "external_jump",
        "recv_ms": BASE_MS + 40,
        "slot": SLOT,
        "direction": 1,
        "source": "deribit_quote",
    })
    replay.on_event(_sell(UP, BASE_MS + 70, size=15))  # exact cancel-time tie
    replay.on_event(_sell(DOWN, BASE_MS + 71, size=13))

    row = replay.finish({MARKET: "Up"})[0]

    assert row["up_filled"] == pytest.approx(5)
    assert row["down_filled"] == pytest.approx(5)
    assert row["warning_source"] == "deribit_quote"
    assert row["warning_action"] == "preserve_inventory_hedge"
    assert row["cancel_effective_ms"] == pytest.approx(BASE_MS + 70)

    flat = _ready()
    flat.on_event({
        "kind": "external_jump",
        "recv_ms": BASE_MS + 40,
        "slot": SLOT,
        "direction": 1,
        "source": "deribit_quote",
    })
    flat.on_event(_sell(UP, BASE_MS + 71, size=15))
    flat.on_event(_sell(DOWN, BASE_MS + 72, size=13))
    flat_row = flat.finish({MARKET: "Up"})[0]
    assert flat_row["up_filled"] == 0
    assert flat_row["down_filled"] == 0
    assert flat_row["cancel_effective_ms"] == pytest.approx(BASE_MS + 70, rel=0, abs=1e-6)

    no_cancel = _ready(replace(pm.PairedMakerConfig(), external_cancel_delay_ms=None))
    no_cancel.on_event({
        "kind": "external_jump",
        "recv_ms": BASE_MS + 40,
        "slot": SLOT,
        "direction": 1,
        "source": "deribit_quote",
    })
    no_cancel.on_event(_sell(UP, BASE_MS + 71, size=15))
    no_cancel.on_event(_sell(DOWN, BASE_MS + 72, size=13))

    assert no_cancel.finish({MARKET: "Down"})[0]["paired_shares"] == pytest.approx(5)


def test_full_first_leg_reprices_opposite_only_within_locked_edge_cap() -> None:
    replay = _ready(down_ask=0.60)
    replay.on_event(_sell(UP, BASE_MS + 40, size=15))
    replay.flush(BASE_MS + 70)

    state = replay.market(MARKET)
    assert state.down_order.price == pytest.approx(0.51)
    assert state.down_order.queue_ahead == 0

    replay.on_event(_sell(DOWN, BASE_MS + 71, price=0.47, size=8))
    row = replay.finish({MARKET: "Up"})[0]

    assert row["down_fill_price"] == pytest.approx(0.51)
    assert row["locked_edge_per_pair"] == pytest.approx(0.02)
    assert row["pnl"] == pytest.approx(0.10)


def test_warning_after_inventory_preserves_and_reprices_the_hedge_leg() -> None:
    replay = _ready(down_ask=0.60)
    replay.on_event({
        "kind": "external_jump",
        "recv_ms": BASE_MS + 40,
        "slot": SLOT,
        "direction": -1,
        "source": "futures_trade",
    })
    replay.on_event(_sell(UP, BASE_MS + 60, size=15))
    replay.flush(BASE_MS + 90)

    state = replay.market(MARKET)
    assert state.down_order.active is True
    assert state.down_order.price == pytest.approx(0.51)
    assert state.warning_action == "preserve_inventory_hedge"

    replay.on_event(_sell(DOWN, BASE_MS + 91, price=0.47, size=8))
    row = replay.finish({MARKET: "Down"})[0]
    assert row["paired_shares"] == pytest.approx(5)
    assert row["pnl"] == pytest.approx(0.10)


def test_external_preserve_does_not_defer_an_already_due_end_cancel() -> None:
    replay = _ready()
    state = replay.market(MARKET)
    assert state.end_cancel_due_ms is not None
    end_effective_ms = state.end_cancel_due_ms + replay.config.cancel_latency_ms
    replay.on_event({
        "kind": "external_jump",
        "recv_ms": end_effective_ms - replay.config.external_cancel_delay_ms,
        "slot": SLOT,
        "direction": -1,
        "source": "futures_trade",
    })
    replay.on_event(_sell(UP, end_effective_ms - 20, size=15))

    replay.on_event(_sell(DOWN, end_effective_ms + 1, size=13))
    replay.flush(end_effective_ms + 1)

    assert state.warning_action == "preserve_inventory_hedge"
    assert state.down_order.active is False
    assert state.down_order.filled == 0


def test_receipt_timestamp_group_is_atomic_around_activation_and_cancel() -> None:
    replay = pm.PairedMakerReplay()
    replay.on_event(_market())
    replay.on_event({"kind": "connection", "recv_ms": BASE_MS - 9, "epoch": 1})
    replay.on_event(_snapshot(UP, BASE_MS, bid=0.47, bid_size=1, ask=0.50))
    replay.on_event(_snapshot(DOWN, BASE_MS + 1, bid=0.47, bid_size=1, ask=0.50))
    activation_ms = BASE_MS + 1 + replay.config.placement_latency_ms
    replay.on_event(_sell(UP, activation_ms, size=6))
    replay.on_event({"kind": "noop", "recv_ms": activation_ms})
    replay.flush(activation_ms)
    assert replay.market(MARKET).up_order.filled == 0

    replay.on_event({
        "kind": "external_jump",
        "recv_ms": activation_ms + 10,
        "slot": SLOT,
        "direction": 1,
        "source": "deribit_quote",
    })
    cancel_ms = activation_ms + 10 + replay.config.external_cancel_delay_ms
    replay.on_event({"kind": "noop", "recv_ms": cancel_ms})
    replay.on_event(_sell(UP, cancel_ms, size=6))
    replay.flush(cancel_ms)
    assert replay.market(MARKET).up_order.filled == pytest.approx(5)


def test_warning_during_placement_is_not_ignored() -> None:
    replay = pm.PairedMakerReplay()
    replay.on_event(_market())
    replay.on_event({"kind": "connection", "recv_ms": BASE_MS - 9, "epoch": 1})
    replay.on_event(_snapshot(UP, BASE_MS, bid=0.47, bid_size=1, ask=0.50))
    replay.on_event(_snapshot(DOWN, BASE_MS + 1, bid=0.47, bid_size=1, ask=0.50))
    replay.on_event({
        "kind": "external_jump",
        "recv_ms": BASE_MS + 10,
        "slot": SLOT,
        "direction": 1,
        "source": "futures_trade",
    })
    replay.on_event(_sell(UP, BASE_MS + 41, size=6))
    replay.flush(BASE_MS + 41)

    state = replay.market(MARKET)
    assert state.warning_action == "cancel_flat_pair"
    assert state.up_order.filled == 0
    assert state.down_order.filled == 0


def test_partial_fill_protection_keeps_only_the_inventory_hedge_quantity() -> None:
    replay = _ready()
    replay.on_event({
        "kind": "external_jump",
        "recv_ms": BASE_MS + 40,
        "slot": SLOT,
        "direction": 1,
        "source": "deribit_quote",
    })
    replay.on_event(_sell(UP, BASE_MS + 50, size=11))  # ten ahead, one owned
    replay.on_event({"kind": "noop", "recv_ms": BASE_MS + 71})
    replay.flush(BASE_MS + 71)

    state = replay.market(MARKET)
    assert state.up_order.active is False
    assert state.up_order.filled == pytest.approx(1)
    assert state.down_order.active is True
    assert state.down_order.target_shares == pytest.approx(1)

    replay.on_event(_sell(DOWN, BASE_MS + 72, size=9))  # eight ahead, one owned
    row = replay.finish({MARKET: "Down"})[0]
    assert row["paired_shares"] == pytest.approx(1)
    assert row["unmatched_up"] == 0
    assert row["unmatched_down"] == 0


def test_connection_epoch_change_taints_exposed_orders() -> None:
    replay = _ready()
    replay.on_event({"kind": "connection", "recv_ms": BASE_MS + 35, "epoch": 2})
    replay.on_event(_sell(UP, BASE_MS + 40, size=15))

    row = replay.finish({MARKET: "Up"})[0]
    assert row["eligible"] is False
    assert row["taint_reason"] == "connection_epoch_change"
    assert row["up_filled"] == 0


def test_locked_hedge_rescue_completes_first_leg_with_real_taker_fee() -> None:
    replay = _ready(replace(
        pm.PairedMakerConfig(),
        rescue_mode="hedge",
        rescue_latency_ms=300.0,
    ))
    replay.on_event(_sell(UP, BASE_MS + 40, size=15))
    replay.on_event(_change(DOWN, BASE_MS + 100, price=0.50, size=0))
    replay.on_event({
        "kind": "price_change",
        "recv_ms": BASE_MS + 101,
        "market_id": MARKET,
        "changes": [{
            "asset_id": DOWN,
            "side": "SELL",
            "price": "0.49",
            "size": "5",
        }],
    })
    replay.flush(BASE_MS + 370)

    row = replay.finish({MARKET: "Down"})[0]
    fee = 0.08747  # venue fee rounded half-up to five decimal places
    assert row["maker_up_filled"] == pytest.approx(5)
    assert row["maker_down_filled"] == 0
    assert row["rescue_kind"] == "hedge"
    assert row["rescue_filled"] == pytest.approx(5)
    assert row["rescue_fee"] == pytest.approx(fee)
    assert row["up_filled"] == pytest.approx(5)
    assert row["down_filled"] == pytest.approx(5)
    assert row["pnl"] == pytest.approx(5 - 5 * 0.47 - 5 * 0.49 - fee)


def test_hedge_rescue_never_crosses_the_locked_edge_cap() -> None:
    replay = _ready(replace(
        pm.PairedMakerConfig(),
        rescue_mode="hedge",
        rescue_latency_ms=300.0,
    ))
    replay.on_event(_sell(UP, BASE_MS + 40, size=15))
    replay.flush(BASE_MS + 370)

    row = replay.finish({MARKET: "Down"})[0]
    assert row["rescue_limit"] == pytest.approx(0.49)
    assert row["rescue_filled"] == 0
    assert row["rescue_reason"] == "ask_above_frozen_limit"
    assert row["pnl"] == pytest.approx(-5 * 0.47)


def test_bounded_unwind_rescue_flattens_at_frozen_bid_and_charges_fee() -> None:
    replay = _ready(replace(
        pm.PairedMakerConfig(),
        rescue_mode="unwind",
        rescue_latency_ms=300.0,
    ))
    replay.on_event(_sell(UP, BASE_MS + 40, size=15))
    replay.flush(BASE_MS + 370)

    row = replay.finish({MARKET: "Down"})[0]
    fee = 0.08719  # venue fee rounded half-up to five decimal places
    assert row["rescue_kind"] == "unwind"
    assert row["rescue_limit"] == pytest.approx(0.47)
    assert row["rescue_filled"] == pytest.approx(5)
    assert row["up_filled"] == 0
    assert row["down_filled"] == 0
    assert row["pnl"] == pytest.approx(-fee)


def test_unwind_rescue_does_not_chase_a_bid_below_the_cancel_time_floor() -> None:
    replay = _ready(replace(
        pm.PairedMakerConfig(),
        rescue_mode="unwind",
        rescue_latency_ms=300.0,
    ))
    replay.on_event(_sell(UP, BASE_MS + 40, size=15))
    replay.on_event(_change(UP, BASE_MS + 100, price=0.47, size=0))
    replay.on_event(_snapshot(UP, BASE_MS + 101, bid=0.46, bid_size=5, ask=0.50))
    replay.flush(BASE_MS + 370)

    row = replay.finish({MARKET: "Down"})[0]
    assert row["rescue_limit"] == pytest.approx(0.47)
    assert row["rescue_filled"] == 0
    assert row["rescue_reason"] == "bid_below_frozen_limit"
    assert row["pnl"] == pytest.approx(-5 * 0.47)


def test_rescue_cancel_transit_allows_existing_hedge_to_finish_without_overhedge() -> None:
    replay = _ready(replace(
        pm.PairedMakerConfig(),
        rescue_mode="hedge",
        rescue_latency_ms=300.0,
    ))
    replay.on_event(_sell(UP, BASE_MS + 40, size=15))
    replay.on_event(_sell(DOWN, BASE_MS + 60, size=13))
    replay.flush(BASE_MS + 370)

    row = replay.finish({MARKET: "Up"})[0]
    assert row["paired_shares"] == pytest.approx(5)
    assert row["rescue_kind"] is None
    assert row["rescue_filled"] == 0


def test_deadline_rescue_preserves_the_maker_hedge_until_tau10_cancel() -> None:
    replay = _ready(replace(
        pm.PairedMakerConfig(),
        rescue_mode="unwind",
        rescue_trigger="end_cancel",
        rescue_latency_ms=300.0,
    ))
    replay.on_event(_sell(UP, BASE_MS + 40, size=15))
    replay.flush(BASE_MS + 370)

    state = replay.market(MARKET)
    assert state.down_order.active is True
    assert state.rescue_cancel_due_ms is None
    assert state.rescue_order is None


def test_deadline_unwind_rescues_only_inventory_still_unmatched_at_tau10() -> None:
    replay = _ready(replace(
        pm.PairedMakerConfig(),
        rescue_mode="unwind",
        rescue_trigger="end_cancel",
        rescue_latency_ms=300.0,
    ))
    replay.on_event(_sell(UP, BASE_MS + 40, size=15))
    cancel_ms = (SLOT + 300) * 1_000.0 - 10_000.0 + 30.0
    replay.on_event(_snapshot(UP, cancel_ms, bid=0.46, bid_size=10, ask=0.50))
    replay.on_event(_snapshot(UP, cancel_ms + 300, bid=0.46, bid_size=10, ask=0.50))
    replay.flush(cancel_ms + 300)

    row = replay.finish({MARKET: "Down"})[0]
    assert row["rescue_kind"] == "unwind"
    assert row["rescue_limit"] == pytest.approx(0.46)
    assert row["rescue_filled"] == pytest.approx(5)
    assert row["up_filled"] == 0
    assert row["pnl"] == pytest.approx(-0.05 - 0.08694)


def test_deadline_rescue_is_not_scheduled_after_the_maker_pair_completes() -> None:
    replay = _ready(replace(
        pm.PairedMakerConfig(),
        rescue_mode="unwind",
        rescue_trigger="end_cancel",
        rescue_latency_ms=300.0,
    ))
    replay.on_event(_sell(UP, BASE_MS + 40, size=15))
    replay.on_event(_sell(DOWN, BASE_MS + 41, size=13))
    cancel_ms = (SLOT + 300) * 1_000.0 - 10_000.0 + 30.0
    replay.on_event(_snapshot(UP, cancel_ms, bid=0.46, bid_size=10, ask=0.50))
    replay.flush(cancel_ms + 300)

    row = replay.finish({MARKET: "Up"})[0]
    assert row["completed_pair"] is True
    assert row["rescue_kind"] is None
    assert row["pnl"] == pytest.approx(0.30)


def test_unmatched_inventory_is_scored_to_outcome_and_disconnect_is_ineligible() -> None:
    replay = _ready()
    replay.on_event(_sell(UP, BASE_MS + 40, size=15))
    won = replay.finish({MARKET: "Up"})[0]
    assert won["pnl"] == pytest.approx(5 * (1 - 0.47))

    lost_replay = _ready()
    lost_replay.on_event(_sell(UP, BASE_MS + 40, size=15))
    lost = lost_replay.finish({MARKET: "Down"})[0]
    assert lost["pnl"] == pytest.approx(-5 * 0.47)

    tainted = _ready()
    tainted.on_event(_sell(UP, BASE_MS + 34, size=15))
    tainted.on_event({"kind": "disconnect", "recv_ms": BASE_MS + 35, "epoch": 1})
    row = tainted.finish({MARKET: "Up"})[0]
    assert row["eligible"] is True
    assert row["taint_reason"] == "disconnect"
    assert row["pnl"] == pytest.approx(5 * (1 - 0.47))
    assert row["unknown_live_order_shares"] == pytest.approx(5)

    missing = _ready()
    missing.on_event(_sell(UP, BASE_MS + 40, size=15))
    with pytest.raises(ValueError, match="missing official outcome"):
        missing.finish({})


def test_external_candidates_keep_receipt_clock_and_cross_transport_ties_fail_closed() -> None:
    candidate = SimpleNamespace(
        source="deribit_quote",
        recv_ns=int((BASE_MS + 40) * 1_000_000),
        source_ns=int((BASE_MS - 30) * 1_000_000),
        slot=SLOT,
        direction=1,
        z_score=3.2,
    )
    warning = next(iter(pm.external_jump_events([candidate])))
    assert warning["recv_ms"] == pytest.approx(BASE_MS + 40)
    assert warning["source_ts_ms"] == pytest.approx(BASE_MS - 30)

    replay = _ready()
    trade = _sell(UP, BASE_MS + 40, size=15)
    for event in pm.merge_replay_events([trade], [warning]):
        replay.on_event(event)

    row = replay.finish({MARKET: "Up"})[0]
    assert row["taint_reason"] == "equal_cross_transport_receipt"
    assert row["pnl"] == pytest.approx(5 * (1 - 0.47))
    assert row["unknown_live_order_shares"] == pytest.approx(5)

    events = [
        _market(),
        {"kind": "connection", "recv_ms": BASE_MS - 9, "epoch": 1},
        _snapshot(UP, BASE_MS, bid=0.47, bid_size=10, ask=0.50),
        _snapshot(DOWN, BASE_MS, bid=0.47, bid_size=8, ask=0.50),
        trade,
    ]
    rows = pm.replay_variants(
        events,
        [warning],
        {MARKET: "Up"},
        {
            "protected": pm.PairedMakerConfig(),
            "no_cancel": replace(pm.PairedMakerConfig(), external_cancel_delay_ms=None),
        },
    )
    assert rows["protected"][0]["taint_reason"] == "equal_cross_transport_receipt"
    assert rows["no_cancel"][0]["taint_reason"] == "equal_cross_transport_receipt"


def test_raw_nanosecond_receipts_do_not_collapse_into_a_false_transport_tie() -> None:
    receive_ns = int((BASE_MS + 40) * 1_000_000)
    candidate = SimpleNamespace(
        source="spot_trade",
        recv_ns=receive_ns,
        source_ns=receive_ns - 100_000_000,
        slot=SLOT,
        direction=1,
        z_score=2.2,
    )
    warning = next(iter(pm.external_jump_events([candidate])))
    trade = _sell(UP, warning["recv_ms"], size=15)
    trade["recv_ns"] = receive_ns + 89

    replay = _ready()
    for event in pm.merge_replay_events([trade], [warning]):
        replay.on_event(event)
    row = replay.finish({MARKET: "Up"})[0]

    assert warning["recv_ns"] == receive_ns
    assert trade["recv_ms"] == warning["recv_ms"]
    assert row["taint_reason"] is None
    assert row["up_filled"] == pytest.approx(5)


def test_matched_variants_share_one_stream_but_apply_different_cancel_rules() -> None:
    base = _ready().config
    events = [
        _market(),
        {"kind": "connection", "recv_ms": BASE_MS - 9, "epoch": 1},
        _snapshot(UP, BASE_MS, bid=0.47, bid_size=10, ask=0.50),
        _snapshot(DOWN, BASE_MS, bid=0.47, bid_size=8, ask=0.50),
        _sell(UP, BASE_MS + 80, size=15),
        _sell(DOWN, BASE_MS + 81, size=13),
    ]
    warning = [{
        "kind": "external_jump",
        "recv_ms": BASE_MS + 40,
        "slot": SLOT,
        "direction": 1,
        "source": "futures_trade",
    }]

    result = pm.replay_variants(
        events,
        warning,
        {MARKET: "Down"},
        {
            "protected": base,
            "no_cancel": replace(base, external_cancel_delay_ms=None),
        },
    )

    assert result["protected"][0]["paired_shares"] == 0
    assert result["no_cancel"][0]["paired_shares"] == pytest.approx(5)
    assert pm.summarize(result["no_cancel"])["completed_pairs"] == 1


def test_development_variant_selection_is_explicit_and_rejects_unknown_names() -> None:
    selected = pm.development_configs([
        "place30_cancel30",
        "place30_deadline_unwind300",
    ])
    assert tuple(selected) == (
        "place30_cancel30",
        "place30_deadline_unwind300",
    )
    with pytest.raises(ValueError, match="unknown development variants"):
        pm.development_configs(["missing-arm"])
