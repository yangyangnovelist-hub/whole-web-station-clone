from __future__ import annotations

import gzip
import json
import math

import pytest

import settlement_proxy as sp


def _frame(source: str, payload: dict, receive_ms: float = 10_000.0):
    state = sp.FrameState()
    return sp.parse_raw_frame(source, receive_ms, json.dumps(payload), state)


@pytest.mark.parametrize(
    ("source", "payload", "expected"),
    [
        ("bn_spot", {"stream": "btcusdt@bookTicker", "data": {"b": "99", "a": "101"}}, 100.0),
        ("bn_spot", {"stream": "btcusdt@depth20@100ms", "data": {
            "bids": [["99", "1"]], "asks": [["101", "1"]]}}, 100.0),
        ("coinbase", {"type": "ticker", "product_id": "BTC-USD",
                      "best_bid": "99", "best_ask": "101"}, 100.0),
        ("bitstamp", {"channel": "order_book_btcusd", "event": "data", "data": {
            "bids": [["99", "1"]], "asks": [["101", "1"]]}}, 100.0),
        ("okx", {"arg": {"channel": "bbo-tbt", "instId": "BTC-USDT"}, "data": [{
            "bids": [["99", "1"]], "asks": [["101", "1"]]}]}, 100.0),
        ("bybit_spot", {"topic": "orderbook.1.BTCUSDT", "data": {
            "b": [["99", "1"]], "a": [["101", "1"]]}}, 100.0),
        ("deribit", {"params": {"channel": "deribit_price_index.btc_usd", "data": {
            "timestamp": 9_900, "price": 100.0}}}, 100.0),
    ],
)
def test_raw_price_parsers(source, payload, expected):
    prices, chainlink = _frame(source, payload)

    assert chainlink == []
    assert len(prices) == 1
    assert prices[0].source == ("deribit_index" if source == "deribit" else source)
    assert prices[0].value == expected
    assert prices[0].receive_ms == 10_000.0


def test_chainlink_parser_preserves_payload_push_and_receipt_clocks():
    payload = {
        "topic": "crypto_prices_chainlink",
        "timestamp": 11_000,
        "payload": {"symbol": "btc/usd", "timestamp": 10_000, "value": 100.25},
    }

    prices, chainlink = _frame("rtds_cl", payload, 11_250.0)

    assert prices == []
    assert chainlink == [sp.ChainlinkPoint(10_000.0, 11_000.0, 11_250.0, 100.25)]


def test_kraken_book_requires_a_snapshot_and_updates_causally():
    state = sp.FrameState()
    snapshot = {"channel": "book", "type": "snapshot", "data": [{
        "bids": [{"price": 99.0, "qty": 2.0}],
        "asks": [{"price": 101.0, "qty": 3.0}],
    }]}
    update = {"channel": "book", "type": "update", "data": [{
        "bids": [{"price": 100.0, "qty": 2.0}], "asks": [],
    }]}

    first, _ = sp.parse_raw_frame("kraken", 1_000.0, json.dumps(snapshot), state)
    second, _ = sp.parse_raw_frame("kraken", 1_001.0, json.dumps(update), state)

    assert first[0].value == 100.0
    assert second[0].value == 100.5


def test_step_series_never_reads_a_future_receipt():
    series = sp.StepSeries.from_points([
        sp.PricePoint("x", 100.0, 10.0),
        sp.PricePoint("x", 200.0, 20.0),
    ])

    assert series.at(199.999, max_age_ms=500.0) == 10.0
    assert series.at(200.0, max_age_ms=500.0) == 20.0
    assert series.at(701.0, max_age_ms=500.0) is None


def test_committed_freeze_hash_is_enforced(tmp_path):
    frozen = sp.load_freeze()
    assert sp.strategy_fingerprint(frozen) == sp.EXPECTED_FREEZE_SHA256

    frozen["proxy_config"]["timing_placebo_delay_ms"] = 251.0
    tampered = tmp_path / "freeze.json"
    tampered.write_text(json.dumps(frozen), encoding="utf-8")
    with pytest.raises(ValueError, match="fingerprint mismatch"):
        sp.load_freeze(tampered)


def _manual_calibration(bound_bp: float = 1.0) -> sp.ProxyCalibration:
    return sp.ProxyCalibration(
        calibration_end_ms=0.0,
        fit_end_ms=0.0,
        spot_query_offset_ms=-1_000.0,
        deribit_query_offset_ms=-500.0,
        venue_log_offsets={source: 0.0 for source in sp.SPOT_SOURCES},
        deribit_log_offset=0.0,
        residual_bound_bp=bound_bp,
        residual_quantile_bp=bound_bp,
        validation_windows=20,
        spot_validation_mae_bp=0.1,
        deribit_validation_mae_bp=0.1,
        combined_validation_mae_bp=0.1,
    )


def _constant_series(source: str, timestamps, value: float) -> sp.StepSeries:
    return sp.StepSeries.from_points(sp.PricePoint(source, timestamp, value) for timestamp in timestamps)


def test_exact_settlement_window_is_boundary_minus_62_through_minus_2():
    assert sp.settlement_payload_times(300_000.0) == tuple(
        float(value) for value in range(238_000, 299_000, 1_000)
    )
    assert len(sp.settlement_payload_times(300_000.0)) == 61


def test_proxy_window_requires_every_component_to_be_received_by_decision():
    boundary = 600_000.0
    payloads = sp.settlement_payload_times(boundary)
    series = {
        source: _constant_series(source, (payload - 1_000.0 for payload in payloads), 101.0)
        for source in sp.SPOT_SOURCES
    }
    series[sp.DERIBIT_SOURCE] = _constant_series(
        sp.DERIBIT_SOURCE, (payload - 500.0 for payload in payloads), 101.0,
    )

    estimate = sp.estimate_proxy_window(
        boundary, boundary - 1_000.0, series, _manual_calibration(), sp.ProxyConfig(),
    )
    too_early = sp.estimate_proxy_window(
        boundary, boundary - 3_000.0, series, _manual_calibration(), sp.ProxyConfig(),
    )

    assert estimate is not None
    assert estimate.combined == pytest.approx(101.0)
    assert too_early is None


def test_signal_generation_uses_exact_open_proxy_close_and_ignores_future_quotes():
    slot_s = 300
    open_boundary = slot_s * 1_000.0
    close_boundary = (slot_s + 300) * 1_000.0
    opening = sp.settlement_payload_times(open_boundary)
    closing = sp.settlement_payload_times(close_boundary)
    # No closing-window Chainlink values are present.  Signal creation must depend
    # only on the receipt-causal proxy; future settlement data is for scoring only.
    chainlink = [
        sp.ChainlinkPoint(payload, payload + 1_400.0, payload + 1_500.0, 100.0)
        for payload in opening
    ]
    series = {}
    for source in sp.SPOT_SOURCES:
        points = [sp.PricePoint(source, payload - 1_000.0, 101.0) for payload in closing]
        points.append(sp.PricePoint(source, close_boundary + 10_000.0, 50.0))
        series[source] = sp.StepSeries.from_points(points)
    series[sp.DERIBIT_SOURCE] = sp.StepSeries.from_points(
        [sp.PricePoint(sp.DERIBIT_SOURCE, payload - 500.0, 101.0) for payload in closing]
    )
    mapping = {"market_id": "m", "slot": slot_s, "up_token_id": "up", "down_token_id": "down"}

    signals, audit = sp.generate_signals(
        [mapping], series, chainlink, _manual_calibration(),
        holdout_start_ms=open_boundary, config=sp.ProxyConfig(), official_outcomes={"m": "Up"},
    )

    base = signals[sp.BASE_VARIANT]
    assert len(base) == 1
    assert base[0].direction == "Up"
    assert base[0].actual_winner == "Up"
    assert base[0].decision_ms == close_boundary - 1_000.0
    assert audit[sp.BASE_VARIANT]["signals"] == 1


def test_reconstructed_settlement_is_checked_against_official_outcome():
    opening_boundary = 300_000.0
    closing_boundary = 600_000.0
    chainlink = [
        sp.ChainlinkPoint(payload, payload + 1_000, payload + 1_100, 100.0)
        for payload in sp.settlement_payload_times(opening_boundary)
    ] + [
        sp.ChainlinkPoint(payload, payload + 1_000, payload + 1_100, 101.0)
        for payload in sp.settlement_payload_times(closing_boundary)
    ]
    mappings = [{"market_id": "m", "slot": 300}]

    passing = sp.audit_official_outcomes(mappings, {"m": "Up"}, chainlink)
    failing = sp.audit_official_outcomes(mappings, {"m": "Down"}, chainlink)

    assert passing["passes"] is True
    assert passing["compared"] == 1
    assert failing["passes"] is False
    assert failing["mismatch_count"] == 1


def test_calibration_never_uses_chainlink_or_proxy_receipts_after_cutoff():
    config = sp.ProxyConfig(
        lag_grid_ms=(-1_000.0,),
        min_fit_points=10,
        min_validation_windows=2,
        calibration_fit_fraction=0.5,
        residual_safety_multiplier=1.0,
    )
    chainlink = []
    points = {source: [] for source in (*sp.SPOT_SOURCES, sp.DERIBIT_SOURCE)}
    for second in range(0, 1_500):
        payload = second * 1_000.0
        value = 100.0 + math.sin(second / 7.0)
        chainlink.append(sp.ChainlinkPoint(payload, payload + 1_000.0, payload + 1_100.0, value))
        for source in sp.SPOT_SOURCES:
            points[source].append(sp.PricePoint(source, payload - 1_000.0, value))
        points[sp.DERIBIT_SOURCE].append(sp.PricePoint(sp.DERIBIT_SOURCE, payload - 1_000.0, value))
    cutoff = 1_200_000.0
    # A Chainlink point received exactly at the cutoff belongs to the holdout.
    chainlink.append(sp.ChainlinkPoint(cutoff - 500.0, cutoff - 100.0, cutoff, 1_000_000.0))
    # A spectacular future price must not alter the frozen calibration.
    for source in points:
        points[source].append(sp.PricePoint(source, cutoff, 1_000_000.0))
        points[source].append(sp.PricePoint(source, cutoff + 1.0, 1_000_000.0))
    series = {source: sp.StepSeries.from_points(rows) for source, rows in points.items()}

    calibration = sp.fit_calibration(series, chainlink, cutoff, config)

    assert calibration.calibration_end_ms == cutoff
    assert calibration.spot_query_offset_ms == -1_000.0
    assert calibration.deribit_query_offset_ms == -1_000.0
    assert max(abs(value) for value in calibration.venue_log_offsets.values()) < 1e-9
    assert abs(calibration.deribit_log_offset) < 1e-9
    assert calibration.residual_bound_bp < 1e-6
    assert calibration.spot_validation_mae_bp < 1e-6
    assert calibration.deribit_validation_mae_bp < 1e-6
    assert calibration.combined_validation_mae_bp < 1e-6


def test_positive_lag_calibration_cannot_read_a_post_cutoff_proxy_quote():
    cutoff = 1_500.0
    config = sp.ProxyConfig(lag_grid_ms=(1_000.0,), min_fit_points=1)
    chainlink = [
        sp.ChainlinkPoint(0.0, 100.0, 100.0, 100.0),
        sp.ChainlinkPoint(1_000.0, 1_100.0, 1_100.0, 100.0),
    ]
    series = {
        source: sp.StepSeries.from_points([
            sp.PricePoint(source, 1_000.0, 100.0),
            sp.PricePoint(source, 2_000.0, 1_000_000.0),
        ])
        for source in sp.SPOT_SOURCES
    }

    _, offsets, _ = sp._fit_group(
        series, chainlink, sp.SPOT_SOURCES, config, latest_receive_ms=cutoff,
    )

    assert max(abs(value) for value in offsets.values()) < 1e-12


def test_timing_placebo_delays_a_known_base_signal_instead_of_backdating_it():
    slot_s = 300
    open_boundary = slot_s * 1_000.0
    close_boundary = (slot_s + 300) * 1_000.0
    chainlink = [
        sp.ChainlinkPoint(payload, payload + 100.0, payload + 100.0, 100.0)
        for payload in sp.settlement_payload_times(open_boundary)
    ]
    series = {
        source: _constant_series(
            source,
            (payload - 1_000.0 for payload in sp.settlement_payload_times(close_boundary)),
            101.0,
        )
        for source in sp.SPOT_SOURCES
    }
    series[sp.DERIBIT_SOURCE] = _constant_series(
        sp.DERIBIT_SOURCE,
        (payload - 500.0 for payload in sp.settlement_payload_times(close_boundary)),
        101.0,
    )
    mapping = {"market_id": "m", "slot": slot_s, "up_token_id": "up", "down_token_id": "down"}

    signals, _ = sp.generate_signals(
        [mapping], series, chainlink, _manual_calibration(), open_boundary,
        sp.ProxyConfig(), {"m": "Up"},
    )

    base = signals[sp.BASE_VARIANT][0]
    delayed = signals[sp.TIMING_VARIANT][0]
    assert delayed.decision_ms == base.decision_ms + 250.0
    assert delayed.decision_ms + 500.0 < close_boundary
    assert delayed.direction == base.direction


def test_timing_placebo_that_crosses_market_close_is_rejected():
    with pytest.raises(ValueError, match="before market close"):
        sp.generate_signals(
            [], {}, [], _manual_calibration(), 0.0,
            sp.ProxyConfig(timing_placebo_delay_ms=500.0), {},
        )


def test_outcome_audit_counts_unresolved_markets_and_cannot_pass_them():
    boundary = 300_000.0
    chainlink = [
        sp.ChainlinkPoint(payload, payload + 100.0, payload + 100.0, 100.0)
        for payload in sp.settlement_payload_times(boundary)
    ] + [
        sp.ChainlinkPoint(payload, payload + 100.0, payload + 100.0, 101.0)
        for payload in sp.settlement_payload_times(boundary + 300_000.0)
    ]

    audit = sp.audit_official_outcomes([{"market_id": "pending", "slot": 300}], {}, chainlink)

    assert audit["unresolved_market_count"] == 1
    assert audit["passes"] is False


def test_first_crossing_sample_ignores_every_later_observation():
    def row(signal_ms, day, filled=True):
        return {"signal_ms": signal_ms, "market_id": str(signal_ms), "day": day,
                "qualified_fill": filled, "filled_shares": 5.0 if filled else 0.0,
                "winner": "Up", "pnl": 1.0 if filled else 0.0}

    sample, reached = sp._first_crossing_sample(
        [row(1.0, "2026-10-05"), row(2.0, "2026-10-06"), row(3.0, "2026-10-07")],
        min_fills=2, min_days=2,
    )

    assert reached is True
    assert [item["signal_ms"] for item in sample] == [1.0, 2.0]


def test_calibration_pin_cannot_be_created_before_every_source_covers_cutoff(tmp_path):
    cutoff = 2_000.0
    series = {
        source: sp.StepSeries.from_points([sp.PricePoint(source, 1_000.0, 100.0)])
        for source in (*sp.SPOT_SOURCES, sp.DERIBIT_SOURCE)
    }
    chainlink = [sp.ChainlinkPoint(0.0, 1_000.0, 1_500.0, 100.0)]

    with pytest.raises(ValueError, match="cutoff"):
        sp.load_or_pin_calibration(
            series, chainlink, cutoff, sp.ProxyConfig(min_fit_points=1),
            tmp_path / "calibration.json", "fingerprint",
        )


def test_terminal_paper_verdict_is_pinned_once(tmp_path):
    def result(passed):
        row = {
            "stopping_sample_reached": True,
            "unresolved_market_count": 0,
            "fills": 1_000,
            "stopping_last_signal_ms": 123.0,
            "net_ev_per_share": 0.01 if passed else -0.01,
        }
        return {
            "execution": {sp.BASE_VARIANT: {"300": dict(row), "500": dict(row)}},
            "outcome_audit": {"unresolved_market_count": 0},
            "gate": {"paper_gate_passes": passed},
        }

    path = tmp_path / "verdict.json"
    first = sp.pin_verdict(result(True), path, "fingerprint")
    second = sp.pin_verdict(result(False), path, "fingerprint")

    assert first == second
    assert second["verdict"]["status"] == "paper_passed"


def test_controls_use_the_same_first_crossing_horizon_as_the_base():
    def signal(variant, market, clock):
        return sp.ProxySignal(
            variant=variant, market_id=market, slot=int(clock), decision_ms=clock,
            direction="Up", opening=100.0, projected_close=101.0,
            spot_close=101.0, deribit_close=101.0, margin=1.0, bound=0.01,
            actual_winner="Up",
        )

    def row(market, clock, day, protocol_slot=None):
        return {
            "market_id": market, "signal_ms": clock, "evaluation_ms": 300.0,
            "protocol_slot": clock if protocol_slot is None else protocol_slot,
            "day": day, "qualified_fill": True, "filled_shares": 5.0,
            "winner": "Up", "won": True, "all_in_cost": 0.5,
            "pnl": 2.5, "sent": True, "reason": "filled",
        }

    control = "omit_coinbase"
    signals = {
        sp.BASE_VARIANT: [signal(sp.BASE_VARIANT, f"m{i}", float(i)) for i in (1, 2, 3)],
        control: [signal(control, f"m{i}", float(i)) for i in (1, 2, 3)],
    }
    base_rows = [row("m1", 1.0, "2026-10-05"), row("m2", 2.0, "2026-10-06"),
                 row("m3", 3.0, "2026-10-07")]
    control_rows = [
        row("m1", 251.0, "2026-10-05", 1.0),
        row("m2", 252.0, "2026-10-06", 2.0),
        row("m3", 253.0, "2026-10-07", 3.0),
    ]

    report = sp.summarize_execution(
        {sp.BASE_VARIANT: base_rows, control: control_rows}, signals,
        min_stopping_fills=2, min_stopping_days=2,
    )

    assert report[sp.BASE_VARIANT]["300"]["signals"] == 2
    assert report[control]["300"]["signals"] == 2
    assert report[control]["300"]["stopping_last_signal_ms"] == 2.0
    assert report[control]["300"]["coverage_vs_base"] == 1.0


def test_control_only_unresolved_outcome_prevents_terminal_verdict():
    resolved = {
        "stopping_sample_reached": True,
        "unresolved_market_count": 0,
        "fills": 1_000,
        "stopping_last_signal_ms": 123.0,
        "net_ev_per_share": 0.01,
    }
    unresolved = {**resolved, "unresolved_market_count": 1}
    result = {
        "execution": {
            sp.BASE_VARIANT: {"300": dict(resolved), "500": dict(resolved)},
            "omit_coinbase": {"300": dict(unresolved), "500": dict(resolved)},
        },
        "outcome_audit": {"unresolved_market_count": 0},
        "gate": {"paper_gate_passes": True},
    }

    assert sp.paper_verdict(result)["status"] == "collecting"


def test_direct_l2_execution_uses_fixed_097_limit_five_shares_and_real_fee():
    decision = 599_000.0
    signals = {sp.BASE_VARIANT: [sp.ProxySignal(
        variant=sp.BASE_VARIANT,
        market_id="m",
        slot=300,
        decision_ms=decision,
        direction="Up",
        opening=100.0,
        projected_close=101.0,
        spot_close=101.0,
        deribit_close=101.0,
        margin=1.0,
        bound=0.01,
        actual_winner="Up",
    )]}
    mappings = [{"market_id": "m", "slot": 300, "up_token_id": "up", "down_token_id": "down"}]
    clob = [
        {"kind": "clob_connection", "recv_ms": decision - 10, "source_ts_ms": None, "seq": 0},
    ]
    seq = 1
    for timestamp, up_ask in ((decision - 5, 0.95), (decision + 310, 0.96), (decision + 510, 0.96),
                              (decision + 700, 0.96)):
        for token, bid, ask in (("up", up_ask - 0.01, up_ask), ("down", 0.03, 0.04)):
            clob.append({
                "kind": "clob_snapshot", "recv_ms": timestamp + 1, "source_ts_ms": timestamp,
                "market_id": "m", "asset_id": token,
                "bids": [{"price": str(bid), "size": "10"}],
                "asks": [{"price": str(ask), "size": "10"}], "seq": seq,
            })
            seq += 1

    rows = sp.replay_execution(signals, clob, mappings, sp.execution_config())
    report = sp.summarize_execution(rows, signals)[sp.BASE_VARIANT]

    assert {row["evaluation_ms"] for row in rows[sp.BASE_VARIANT]} == {300.0, 500.0}
    assert all(row["fixed_limit"] == 0.97 for row in rows[sp.BASE_VARIANT])
    assert all(row["filled_shares"] >= 5.0 for row in rows[sp.BASE_VARIANT])
    assert all(row["fill_fee"] > 0 for row in rows[sp.BASE_VARIANT])
    assert report["300"]["fills"] == 1
    assert report["500"]["net_ev_per_share"] > 0


def test_late_received_clob_update_cannot_rewrite_an_earlier_match_book():
    decision = 599_000.0
    signals = {sp.BASE_VARIANT: [sp.ProxySignal(
        variant=sp.BASE_VARIANT, market_id="m", slot=300, decision_ms=decision,
        direction="Up", opening=100.0, projected_close=101.0, spot_close=101.0,
        deribit_close=101.0, margin=1.0, bound=0.01, actual_winner="Up",
    )]}
    mappings = [{"market_id": "m", "slot": 300, "up_token_id": "up", "down_token_id": "down"}]
    clob = [{"kind": "clob_connection", "recv_ms": decision - 20,
             "source_ts_ms": None, "seq": 0}]
    seq = 1
    for receive, source, up_ask in (
        (decision - 10, decision - 11, 0.95),
        # Exchange timestamp precedes the 300 ms target, but this update was not
        # received until afterwards and therefore cannot rewrite that checkpoint.
        (decision + 350, decision + 100, 0.99),
        (decision + 650, decision + 600, 0.99),
    ):
        for token, bid, ask in (("up", up_ask - 0.01, up_ask), ("down", 0.03, 0.04)):
            clob.append({
                "kind": "clob_snapshot", "recv_ms": receive, "source_ts_ms": source,
                "market_id": "m", "asset_id": token,
                "bids": [{"price": str(bid), "size": "10"}],
                "asks": [{"price": str(ask), "size": "10"}], "seq": seq,
            })
            seq += 1

    rows = sp.replay_execution(signals, clob, mappings, sp.execution_config())[sp.BASE_VARIANT]
    by_latency = {row["evaluation_ms"]: row for row in rows}

    assert by_latency[300.0]["qualified_fill"] is True
    assert by_latency[300.0]["fill_price"] == pytest.approx(0.95)
    assert by_latency[500.0]["qualified_fill"] is False
    assert all(row["match_book_clock"] == "receipt_ms" for row in rows)


def test_stale_price_change_does_not_refresh_the_execution_book():
    class Machine:
        def __init__(self):
            self.events = []

        def feed(self, event):
            self.events.append(event)

    market = sp.h_replay_run.DirectMarketBook("m", 300, "up", "down")
    tokens = {"up": (market, "Up"), "down": (market, "Down")}
    machine = Machine()
    machines = {sp.BASE_VARIANT: machine}
    connected = sp._direct_clob_batch({
        "recv_ms": 900.0,
        "events": [{"kind": "clob_connection", "connection_epoch": 1}],
    }, machines, tokens, False)
    connected = sp._direct_clob_batch({
        "recv_ms": 1_010.0,
        "events": [
            {"kind": "clob_snapshot", "asset_id": "up", "source_ts_ms": 1_000.0,
             "bids": [{"price": "0.39", "size": "10"}],
             "asks": [{"price": "0.40", "size": "10"}]},
            {"kind": "clob_snapshot", "asset_id": "down", "source_ts_ms": 1_000.0,
             "bids": [{"price": "0.59", "size": "10"}],
             "asks": [{"price": "0.60", "size": "10"}]},
        ],
    }, machines, tokens, connected)
    assert any(event["kind"] == "snapshot" for event in machine.events)

    machine.events.clear()
    sp._direct_clob_batch({
        "recv_ms": 2_000.0,
        "events": [{
            "kind": "clob_price_change", "asset_id": "up", "source_ts_ms": 999.0,
            "side": "SELL", "price": "0.40", "size": "9",
            "best_bid": "0.39", "best_ask": "0.40",
        }],
    }, machines, tokens, connected)

    assert not any(event["kind"] in {"snapshot", "invalidate"} for event in machine.events)
    assert market.up.asks == {0.40: 10.0}


def test_partial_fak_loss_is_included_in_strategy_pnl():
    decision = 599_000.0
    signals = {sp.BASE_VARIANT: [sp.ProxySignal(
        variant=sp.BASE_VARIANT, market_id="m", slot=300, decision_ms=decision,
        direction="Up", opening=100.0, projected_close=101.0, spot_close=101.0,
        deribit_close=101.0, margin=1.0, bound=0.01, actual_winner="Down",
    )]}
    mappings = [{"market_id": "m", "slot": 300, "up_token_id": "up", "down_token_id": "down"}]
    clob = [{"kind": "clob_connection", "recv_ms": decision - 20,
             "source_ts_ms": None, "seq": 0}]
    seq = 1
    for timestamp in (decision - 10, decision + 310, decision + 510, decision + 650):
        for token, bid, ask, size in (
            ("up", 0.94, 0.95, 2.0), ("down", 0.04, 0.05, 10.0),
        ):
            clob.append({
                "kind": "clob_snapshot", "recv_ms": timestamp, "source_ts_ms": timestamp,
                "market_id": "m", "asset_id": token,
                "bids": [{"price": str(bid), "size": str(size)}],
                "asks": [{"price": str(ask), "size": str(size)}], "seq": seq,
            })
            seq += 1

    rows = sp.replay_execution(signals, clob, mappings, sp.execution_config())
    report = sp.summarize_execution(rows, signals)[sp.BASE_VARIANT]["300"]

    assert report["fills"] == 0
    assert report["partial_fills"] == 1
    assert report["executions_including_partials"] == 1
    assert report["shares"] == 2.0
    assert report["pnl"] < 0
    assert report["net_ev_per_share"] < 0


def test_generate_signals_rejects_overlapping_calibration_and_holdout():
    with pytest.raises(ValueError, match="holdout start"):
        sp.generate_signals([], {}, [], _manual_calibration(), -1.0)


def test_stateful_source_is_reset_after_a_receipt_gap(tmp_path):
    path = tmp_path / "kraken.20261005T00.txt.gz"
    snapshot = {"channel": "book", "type": "snapshot", "data": [{
        "bids": [{"price": 99.0, "qty": 2.0}],
        "asks": [{"price": 101.0, "qty": 2.0}],
    }]}
    one_sided_update = {"channel": "book", "type": "update", "data": [{
        "bids": [{"price": 100.0, "qty": 2.0}], "asks": [],
    }]}
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        stream.write(f"{1_000_000_000}\t{json.dumps(snapshot)}\n")
        stream.write(f"{4_000_000_000}\t{json.dumps(one_sided_update)}\n")

    series, raw_count = sp._load_source(tmp_path, "kraken", sp.ProxyConfig())

    assert raw_count == 1
    assert len(series) == 1
