from __future__ import annotations

import importlib

import pytest


def test_asian_binary_delta_uses_only_remaining_twap_samples() -> None:
    hedge = importlib.import_module("external_delta_hedge")

    up = hedge.asian_binary_delta_btc(0.5, 100_000.0, 0.0001, 240.0, "Up")
    down = hedge.asian_binary_delta_btc(0.5, 100_000.0, 0.0001, 240.0, "Down")

    assert up == pytest.approx(0.008810564066636734, rel=1e-12)
    assert down == pytest.approx(-0.008810564066636734, rel=1e-12)


@pytest.mark.parametrize(
    "probability,spot,sigma,side",
    [
        (0.0, 100_000.0, 0.0001, "Up"),
        (1.0, 100_000.0, 0.0001, "Up"),
        (0.5, 0.0, 0.0001, "Up"),
        (0.5, 100_000.0, 0.0, "Up"),
        (0.5, 100_000.0, 0.0001, "Both"),
    ],
)
def test_asian_binary_delta_rejects_unhedgeable_inputs(
    probability: float,
    spot: float,
    sigma: float,
    side: str,
) -> None:
    hedge = importlib.import_module("external_delta_hedge")

    with pytest.raises(ValueError):
        hedge.asian_binary_delta_btc(probability, spot, sigma, 240.0, side)


def test_perpetual_round_trip_uses_bid_ask_and_charges_each_taker_fill() -> None:
    hedge = importlib.import_module("external_delta_hedge")

    pnl = hedge.perpetual_cashflow_pnl([
        (-0.01, 100.0),
        (0.01, 101.0),
    ], fee_rate=0.0005)

    assert pnl == pytest.approx(-0.011005)


def test_source_tape_uses_first_causally_valid_bbo_after_send() -> None:
    hedge = importlib.import_module("external_delta_hedge")
    due = 1_000_000_000
    tape = hedge.SourceTape(
        spot_states=[],
        futures_bbos=[
            _bbo(hedge, due - 1, due - 2_000_000, 99.0, 100.0),
            _bbo(hedge, due + 10_000_000, due - 300_000_000, 100.0, 101.0),
            _bbo(hedge, due + 20_000_000, due + 15_000_000, 101.0, 102.0),
            _bbo(hedge, due + 30_000_000, due + 25_000_000, 102.0, 103.0),
        ],
    )

    quote, reason = tape.execution_bbo(
        due,
        max_wait_ns=25_000_000,
        max_source_age_ns=250_000_000,
        future_clock_tolerance_ns=5_000_000,
    )

    assert reason is None
    assert quote is not None
    assert quote.recv_ns == due + 20_000_000


def test_source_tape_fails_closed_when_no_fresh_bbo_arrives() -> None:
    hedge = importlib.import_module("external_delta_hedge")
    due = 1_000_000_000
    tape = hedge.SourceTape(
        spot_states=[],
        futures_bbos=[
            _bbo(hedge, due + 5_000_000, due - 400_000_000, 100.0, 101.0),
            _bbo(hedge, due + 30_000_000, due + 25_000_000, 101.0, 102.0),
        ],
    )

    quote, reason = tape.execution_bbo(
        due,
        max_wait_ns=20_000_000,
        max_source_age_ns=250_000_000,
        future_clock_tolerance_ns=5_000_000,
    )

    assert quote is None
    assert reason == "no_fresh_bbo_before_deadline"


def test_source_tape_rejects_bbo_without_recorded_top_depth() -> None:
    hedge = importlib.import_module("external_delta_hedge")
    due = 1_000_000_000
    tape = hedge.SourceTape(
        spot_states=[],
        futures_bbos=[hedge.FuturesBBO(due, due - 1_000_000, 100.0, 101.0)],
    )

    quote, reason = tape.execution_bbo(
        due,
        max_wait_ns=20_000_000,
        max_source_age_ns=250_000_000,
        future_clock_tolerance_ns=5_000_000,
    )

    assert quote is None
    assert reason == "no_fresh_bbo_before_deadline"


def _maker_row(*fills: dict[str, float | int | str], pnl: float = 1.0) -> dict[str, object]:
    return {
        "market_id": "m",
        "slot": 0,
        "pnl": pnl,
        "maker_fill_events": [
            {"observation_basis": "public_clob_trade_queue_inference", **fill}
            for fill in fills
        ],
    }


def _spot(recv_ns: int) -> object:
    hedge = importlib.import_module("external_delta_hedge")
    return hedge.SpotState(recv_ns=recv_ns, price=100_000.0, sigma_s=0.0001)


def _bbo(hedge: object, recv_ns: int, exchange_ns: int, bid: float, ask: float) -> object:
    return hedge.FuturesBBO(
        recv_ns,
        exchange_ns,
        bid,
        ask,
        bid_depth=10.0,
        ask_depth=10.0,
    )


def test_second_maker_leg_before_send_cancels_pending_futures_hedge() -> None:
    hedge = importlib.import_module("external_delta_hedge")
    row = _maker_row(
        {"recv_ns": 1_000_000_000, "side": "Up", "price": 0.40, "shares": 5.0},
        {"recv_ns": 1_100_000_000, "side": "Down", "price": 0.60, "shares": 5.0},
    )
    tape = hedge.SourceTape(
        spot_states=[_spot(900_000_000), _spot(1_050_000_000)],
        futures_bbos=[],
    )

    result = hedge.simulate_external_delta_hedge(
        row,
        tape,
        hedge.ExternalHedgeConfig(latency_ms=200.0),
    )

    assert result["futures_fills"] == []
    assert result["cancelled_before_send"] == 1
    assert result["combined_pnl"] == pytest.approx(1.0)


def test_exact_send_time_tie_executes_hedge_before_second_maker_fill() -> None:
    hedge = importlib.import_module("external_delta_hedge")
    row = _maker_row(
        {"recv_ns": 1_000_000_000, "side": "Up", "price": 0.40, "shares": 5.0},
        {"recv_ns": 1_200_000_000, "side": "Down", "price": 0.60, "shares": 5.0},
    )
    tape = hedge.SourceTape(
        spot_states=[_spot(900_000_000), _spot(1_100_000_000)],
        futures_bbos=[
            _bbo(hedge, 1_200_000_000, 1_199_000_000, 99_999.0, 100_001.0),
            _bbo(hedge, 1_400_000_000, 1_399_000_000, 99_998.0, 100_002.0),
        ],
    )

    result = hedge.simulate_external_delta_hedge(
        row,
        tape,
        hedge.ExternalHedgeConfig(latency_ms=200.0),
    )

    fills = result["futures_fills"]
    assert len(fills) == 2
    assert fills[0]["quantity_btc"] < 0.0
    assert fills[1]["quantity_btc"] > 0.0
    assert result["final_position_btc"] == pytest.approx(0.0, abs=1e-15)
    assert result["combined_pnl"] < 1.0


def test_missing_market_end_bbo_fails_closed_with_open_futures_position() -> None:
    hedge = importlib.import_module("external_delta_hedge")
    row = _maker_row(
        {"recv_ns": 1_000_000_000, "side": "Up", "price": 0.40, "shares": 5.0},
    )
    tape = hedge.SourceTape(
        spot_states=[_spot(900_000_000)],
        futures_bbos=[
            _bbo(hedge, 1_200_000_000, 1_199_000_000, 99_999.0, 100_001.0),
        ],
    )

    result = hedge.simulate_external_delta_hedge(
        row,
        tape,
        hedge.ExternalHedgeConfig(latency_ms=200.0, max_bbo_wait_ms=25.0),
    )

    assert result["final_position_btc"] != pytest.approx(0.0, abs=1e-15)
    assert result["combined_pnl"] is None
    assert result["failures"]["market_end_close:no_fresh_bbo_before_deadline"] == 1


def test_source_adapter_reuses_receipt_causal_sigma_and_direct_futures_bbo() -> None:
    hedge = importlib.import_module("external_delta_hedge")
    jump = importlib.import_module("jump_causal")
    second = 1_000_000_000
    events = [
        jump.MarketEvent(
            recv_ns=index * second + 10,
            exchange_ns=index * second,
            source="bn_spot",
            channel="bn_spot",
            kind="trade",
            price=price,
        )
        for index, price in enumerate((100.0, 101.0, 99.0, 102.0))
    ]
    events.append(jump.MarketEvent(
        recv_ns=3 * second + 20,
        exchange_ns=3 * second + 15,
        source="bn_fut_pub",
        channel="bn_perp",
        kind="book",
        bid=101.0,
        ask=102.0,
        bid_depth=3.0,
        ask_depth=4.0,
        book_kind="bbo",
    ))

    tape = hedge.build_source_tape(
        events,
        sigma_window_s=10,
        sigma_min_observations=2,
        forward_fill_s=1,
    )

    state = tape.spot_before(3 * second + 11)
    assert state is not None
    assert state.price == 102.0
    assert state.sigma_s > 0.0
    assert tape.futures_bbos == (
        hedge.FuturesBBO(
            3 * second + 20,
            3 * second + 15,
            101.0,
            102.0,
            bid_depth=3.0,
            ask_depth=4.0,
        ),
    )


def test_hedge_fails_closed_when_recorded_top_depth_cannot_fill_quantity() -> None:
    hedge = importlib.import_module("external_delta_hedge")
    row = _maker_row(
        {"recv_ns": 1_000_000_000, "side": "Up", "price": 0.40, "shares": 5.0},
    )
    tape = hedge.SourceTape(
        spot_states=[_spot(900_000_000)],
        futures_bbos=[
            hedge.FuturesBBO(
                1_200_000_000,
                1_199_000_000,
                99_999.0,
                100_001.0,
                bid_depth=0.000001,
                ask_depth=0.000001,
            ),
        ],
    )

    result = hedge.simulate_external_delta_hedge(
        row,
        tape,
        hedge.ExternalHedgeConfig(latency_ms=200.0),
    )

    assert result["futures_fills"] == []
    assert result["failures"]["maker_fill:insufficient_top_depth"] == 1
    assert result["combined_pnl"] is None


def test_partial_opposite_fill_keeps_probability_of_residual_inventory_side() -> None:
    hedge = importlib.import_module("external_delta_hedge")
    row = _maker_row(
        {"recv_ns": 1_000_000_000, "side": "Up", "price": 0.40, "shares": 5.0},
        {"recv_ns": 1_300_000_000, "side": "Down", "price": 0.90, "shares": 1.0},
    )
    tape = hedge.SourceTape(
        spot_states=[_spot(900_000_000), _spot(1_250_000_000)],
        futures_bbos=[
            _bbo(hedge, 1_200_000_000, 1_199_000_000, 99_999.0, 100_001.0),
            _bbo(hedge, 1_500_000_000, 1_499_000_000, 99_999.0, 100_001.0),
            _bbo(hedge, 300_200_000_000, 300_199_000_000, 99_999.0, 100_001.0),
        ],
    )

    result = hedge.simulate_external_delta_hedge(
        row,
        tape,
        hedge.ExternalHedgeConfig(latency_ms=200.0),
    )

    expected = -4.0 * hedge.asian_binary_delta_btc(
        0.40,
        100_000.0,
        0.0001,
        1.3,
        "Up",
    )
    assert result["futures_fills"][1]["target_btc"] == pytest.approx(expected)


def test_future_depth_failure_is_not_known_until_quote_arrives() -> None:
    hedge = importlib.import_module("external_delta_hedge")
    row = _maker_row(
        {"recv_ns": 1_000_000_000, "side": "Up", "price": 0.40, "shares": 5.0},
        {"recv_ns": 1_300_000_000, "side": "Up", "price": 0.40, "shares": 5.0},
    )
    tape = hedge.SourceTape(
        spot_states=[_spot(900_000_000), _spot(1_250_000_000)],
        futures_bbos=[
            hedge.FuturesBBO(
                1_400_000_000,
                1_399_000_000,
                99_999.0,
                100_001.0,
                bid_depth=0.000001,
                ask_depth=0.000001,
            ),
            hedge.FuturesBBO(
                1_500_000_000,
                1_499_000_000,
                99_999.0,
                100_001.0,
                bid_depth=10.0,
                ask_depth=10.0,
            ),
        ],
    )

    result = hedge.simulate_external_delta_hedge(
        row,
        tape,
        hedge.ExternalHedgeConfig(latency_ms=200.0, max_bbo_wait_ms=250.0),
    )

    assert result["futures_fills"] == []
    assert result["cancelled_before_send"] == 1
    assert result["failures"]["maker_fill:insufficient_top_depth"] == 1
    assert result["combined_pnl"] is None


def test_summary_fails_closed_when_any_futures_residual_remains() -> None:
    hedge = importlib.import_module("external_delta_hedge")
    maker_rows = [
        {"maker_up_filled": 5.0, "maker_down_filled": 0.0, "pnl": 2.0},
        {"maker_up_filled": 0.0, "maker_down_filled": 5.0, "pnl": -1.0},
    ]
    hedge_rows = [
        {
            "combined_pnl": 1.5,
            "futures_pnl": -0.5,
            "futures_fills": [{"fee": 0.1}],
            "final_position_btc": 0.0,
            "peak_abs_btc": 0.01,
            "peak_notional_usdt": 1_000.0,
            "cancelled_before_send": 1,
            "failures": {},
        },
        {
            "combined_pnl": None,
            "futures_pnl": None,
            "futures_fills": [{"fee": 0.2}],
            "final_position_btc": 0.02,
            "peak_abs_btc": 0.02,
            "peak_notional_usdt": 2_000.0,
            "cancelled_before_send": 0,
            "failures": {"market_end_close:no_fresh_bbo_before_deadline": 1},
        },
    ]

    summary = hedge.summarize_hedge_rows(maker_rows, hedge_rows)

    assert summary["all_futures_positions_closed"] is False
    assert summary["combined_pnl"] is None
    assert summary["combined_pnl_closed_only"] == pytest.approx(1.5)
    assert summary["poly_pnl"] == pytest.approx(1.0)
    assert summary["futures_fees"] == pytest.approx(0.3)
    assert summary["peak_notional_usdt"] == pytest.approx(2_000.0)


def test_development_arms_freeze_primary_latency_and_fee_sensitivities() -> None:
    hedge = importlib.import_module("external_delta_hedge")

    arms = hedge.development_arms()

    assert set(arms) == {
        "latency100_fee5bps",
        "latency150_fee5bps",
        "latency200_fee5bps",
        "latency200_fee4bps",
    }
    assert arms["latency200_fee5bps"].latency_ms == 200.0
    assert arms["latency200_fee5bps"].fee_rate == pytest.approx(0.0005)
