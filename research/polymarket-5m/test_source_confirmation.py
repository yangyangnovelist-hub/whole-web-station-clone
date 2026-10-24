from __future__ import annotations

import copy
import json
from collections import Counter
from typing import Any

import h_replay
import h_replay_run
import pytest
import source_confirmation as confirmation


def _connected_window() -> confirmation.ConfirmationWindow:
    state = confirmation.ConfirmationWindow()
    state.connect("futures_trade", epoch=1, receive_ms=800.0)
    state.connect("deribit_quote", epoch=1, receive_ms=801.0)
    return state


def test_confirmation_window_is_strictly_prior_and_includes_exact_120ms_boundary() -> (
    None
):
    state = _connected_window()
    state.observe("futures_trade", direction=1, slot=0, receive_ms=880.0, epoch=1)

    decision = state.decide(direction=1, slot=0, receive_ms=1_000.0)

    assert decision.confirmed is True
    assert decision.reason == "confirmed"
    assert decision.same_sources == ("futures_trade",)
    assert decision.opposite_sources == ()

    expired = state.decide(direction=1, slot=0, receive_ms=1_000.001)
    assert expired.confirmed is False
    assert expired.reason == "no_same_direction_secondary"


def test_confirmation_window_vetoes_opposite_even_with_same_direction_candidate() -> (
    None
):
    state = _connected_window()
    state.observe("futures_trade", direction=1, slot=0, receive_ms=950.0, epoch=1)
    state.observe("deribit_quote", direction=-1, slot=0, receive_ms=960.0, epoch=1)

    decision = state.decide(direction=1, slot=0, receive_ms=1_000.0)

    assert decision.confirmed is False
    assert decision.reason == "opposite_secondary"
    assert decision.same_sources == ("futures_trade",)
    assert decision.opposite_sources == ("deribit_quote",)


def test_equal_receipt_secondary_is_ambiguous_and_future_secondary_cannot_look_back() -> (
    None
):
    equal = _connected_window()
    equal.observe("futures_trade", direction=1, slot=0, receive_ms=1_000.0, epoch=1)

    decision = equal.decide(direction=1, slot=0, receive_ms=1_000.0)

    assert decision.confirmed is False
    assert decision.reason == "equal_receipt_ambiguous"

    future = _connected_window()
    before = future.decide(direction=1, slot=0, receive_ms=1_000.0)
    future.observe("futures_trade", direction=1, slot=0, receive_ms=1_001.0, epoch=1)

    assert before.confirmed is False
    with pytest.raises(ValueError, match="receipt clock regressed"):
        future.decide(direction=1, slot=0, receive_ms=1_000.0)


def test_confirmation_lifecycle_clears_old_candidates_and_memory_is_bounded() -> None:
    state = _connected_window()
    for index in range(1_000):
        state.observe(
            "futures_trade",
            direction=1 if index % 2 else -1,
            slot=0,
            receive_ms=900.0 + index / 10_000.0,
            epoch=1,
        )

    assert state.memory_size <= 8

    state.disconnect("futures_trade", epoch=1, receive_ms=1_010.0)
    unavailable = state.decide(direction=1, slot=0, receive_ms=1_011.0)
    assert unavailable.reason == "secondary_source_unavailable"

    state.connect("futures_trade", epoch=2, receive_ms=1_012.0)
    after_reconnect = state.decide(direction=1, slot=0, receive_ms=1_013.0)
    assert after_reconnect.reason == "no_same_direction_secondary"


def test_confirmation_memory_bound_fails_closed_and_placebo_drains_without_a_primary() -> (
    None
):
    bounded = confirmation.ConfirmationWindow(max_candidates=2)
    bounded.connect("futures_trade", epoch=1, receive_ms=800.0)
    bounded.connect("deribit_quote", epoch=1, receive_ms=801.0)
    bounded.observe("futures_trade", direction=1, slot=0, receive_ms=900.0, epoch=1)
    bounded.observe("futures_trade", direction=-1, slot=0, receive_ms=901.0, epoch=1)

    with pytest.raises(ValueError, match="candidate bound exceeded"):
        bounded.observe("deribit_quote", direction=1, slot=0, receive_ms=902.0, epoch=1)

    shifted = confirmation.ShiftedConfirmationWindow(
        window_ms=120.0,
        shift_ms=500.0,
        max_pending=2,
    )
    shifted.connect("futures_trade", epoch=1, receive_ms=800.0)
    shifted.connect("deribit_quote", epoch=1, receive_ms=801.0)
    shifted.observe("futures_trade", direction=1, slot=0, receive_ms=900.0, epoch=1)
    shifted.advance(receive_ms=1_600.0)

    assert shifted.pending_size == 0
    assert shifted.memory_size == 0

    lifecycle_bounded = confirmation.ShiftedConfirmationWindow(
        window_ms=120.0,
        shift_ms=500.0,
        max_lifecycle_events=1,
    )
    lifecycle_bounded.connect("futures_trade", epoch=1, receive_ms=800.0)
    with pytest.raises(ValueError, match="lifecycle-event bound exceeded"):
        lifecycle_bounded.connect("deribit_quote", epoch=1, receive_ms=801.0)


def test_shifted_placebo_preserves_candidate_but_cannot_confirm_original_primary() -> (
    None
):
    state = confirmation.ShiftedConfirmationWindow(window_ms=120.0, shift_ms=500.0)
    state.connect("futures_trade", epoch=1, receive_ms=800.0)
    state.connect("deribit_quote", epoch=1, receive_ms=801.0)
    state.observe("futures_trade", direction=1, slot=0, receive_ms=900.0, epoch=1)

    original = state.decide(direction=1, slot=0, receive_ms=1_000.0)
    equal_shifted = state.decide(direction=1, slot=0, receive_ms=1_400.0)
    shifted = state.decide(direction=1, slot=0, receive_ms=1_401.0)

    assert original.reason == "secondary_source_unavailable"
    assert equal_shifted.reason == "equal_receipt_ambiguous"
    assert shifted.confirmed is True
    assert state.pending_size == 0


def test_shifted_placebo_shifts_lifecycle_without_dropping_queued_candidate() -> None:
    state = confirmation.ShiftedConfirmationWindow(window_ms=120.0, shift_ms=500.0)
    state.connect("futures_trade", epoch=1, receive_ms=800.0)
    state.connect("deribit_quote", epoch=1, receive_ms=801.0)
    state.observe("futures_trade", direction=1, slot=0, receive_ms=900.0, epoch=1)
    state.disconnect("futures_trade", epoch=1, receive_ms=1_000.0)
    state.connect("futures_trade", epoch=2, receive_ms=1_100.0)

    before_shifted_disconnect = state.decide(
        direction=1,
        slot=0,
        receive_ms=1_401.0,
    )
    at_shifted_disconnect = state.decide(
        direction=1,
        slot=0,
        receive_ms=1_500.0,
    )
    after_shifted_reconnect = state.decide(
        direction=1,
        slot=0,
        receive_ms=1_601.0,
    )

    assert before_shifted_disconnect.confirmed is True
    assert at_shifted_disconnect.reason == "equal_receipt_ambiguous"
    assert after_shifted_reconnect.reason == "no_same_direction_secondary"
    assert state.pending_size == 0


def test_current_h_config_is_reused_with_only_frozen_confirmation_clock_changes() -> (
    None
):
    frozen = h_replay.load_freeze()
    untouched = copy.deepcopy(frozen)

    execution, signal = confirmation.configs_from_current_h(frozen)

    assert frozen == untouched
    assert execution.signal_time_basis == "local_receipt_timestamp"
    assert execution.evaluation_ms == (400.0, 500.0)
    assert execution.base_shares == 5.0
    assert execution.max_orders_per_min == 1
    assert execution.fee_rate == float(frozen["execution"]["fee_rate"])
    assert execution.theta == float(frozen["execution"]["theta"])
    assert signal.z0 == 2.0
    assert signal.source_z0 == {"deribit_quote": 3.0}


def _summary_row(
    *,
    arm: str,
    evaluation_ms: float,
    market_id: str,
    signal_ms: float,
    reason: str,
    sent: bool,
    filled_shares: float,
    pnl: float,
    won: bool | None,
    source: str = "futures_book_ticker",
    result_ms: float | None = None,
) -> dict[str, Any]:
    return {
        "arm": arm,
        "evaluation_ms": evaluation_ms,
        "evaluation_time_ms": signal_ms + evaluation_ms,
        "market_id": market_id,
        "signal_receive_ms": signal_ms,
        "day": "2026-10-12",
        "reason": reason,
        "sent": sent,
        "filled": filled_shares > 0,
        "full_fill": filled_shares >= 5.0,
        "filled_shares": filled_shares,
        "pnl": pnl,
        "won": won,
        "winner": "Up",
        "result_time_ms": result_ms,
        "all_in_cost": 0.60 if filled_shares else None,
        "signal_source": source,
        "same_secondary_sources": ("futures_trade",),
        "censored": False,
    }


def test_summary_uses_all_primary_opportunities_for_itt_and_is_order_deterministic() -> (
    None
):
    rows = [
        _summary_row(
            arm="confirmation_veto",
            evaluation_ms=400.0,
            market_id="filled",
            signal_ms=100.0,
            reason="filled",
            sent=True,
            filled_shares=5.0,
            pnl=2.0,
            won=True,
        ),
        _summary_row(
            arm="confirmation_veto",
            evaluation_ms=400.0,
            market_id="veto",
            signal_ms=200.0,
            reason="confirmation_veto",
            sent=False,
            filled_shares=0.0,
            pnl=0.0,
            won=None,
        ),
        _summary_row(
            arm="confirmation_veto",
            evaluation_ms=400.0,
            market_id="rate",
            signal_ms=300.0,
            reason="rate",
            sent=False,
            filled_shares=0.0,
            pnl=0.0,
            won=None,
            source="spot_trade",
        ),
        _summary_row(
            arm="confirmation_veto",
            evaluation_ms=400.0,
            market_id="no-fill",
            signal_ms=400.0,
            reason="insufficient_effective_depth",
            sent=True,
            filled_shares=0.0,
            pnl=0.0,
            won=None,
        ),
    ]

    report = confirmation.summarize(rows, {"z": 1, "a": 2})
    reversed_report = confirmation.summarize(list(reversed(rows)), {"a": 2, "z": 1})
    metrics = report["arms"]["confirmation_veto"]["latencies"]["400"]

    assert report == reversed_report
    assert metrics["opportunities"] == 4
    assert metrics["sent"] == 2
    assert metrics["fills"] == 1
    assert metrics["full_fills"] == 1
    assert metrics["fill_rate_per_opportunity"] == pytest.approx(0.25)
    assert metrics["intention_to_trade_pnl"] == pytest.approx(2.0)
    assert metrics["intention_to_trade_ev_per_opportunity"] == pytest.approx(0.5)
    assert metrics["intention_to_trade_ev_per_requested_share"] == pytest.approx(0.1)
    assert metrics["net_ev_per_filled_share"] == pytest.approx(0.4)
    assert metrics["loss_rate"] == pytest.approx(0.0)
    assert metrics["opportunities_by_primary_source"] == {
        "futures_book_ticker": 3,
        "spot_trade": 1,
    }
    assert report["counters"] == {"a": 2, "z": 1}
    assert len(report["rows_sha256"]) == 64


def test_partial_fills_affect_ev_but_not_the_equal_size_exact_test(monkeypatch) -> None:
    observed: list[dict[str, Any]] = []

    def exact(rows: list[dict[str, Any]]) -> float:
        observed.extend(rows)
        return 0.25

    monkeypatch.setattr(confirmation.run, "_exact_pvalue", exact)
    rows = [
        _summary_row(
            arm="baseline",
            evaluation_ms=400.0,
            market_id="full",
            signal_ms=100.0,
            reason="filled",
            sent=True,
            filled_shares=5.0,
            pnl=2.0,
            won=True,
        ),
        _summary_row(
            arm="baseline",
            evaluation_ms=400.0,
            market_id="partial",
            signal_ms=200.0,
            reason="partial_fill",
            sent=True,
            filled_shares=1.0,
            pnl=-0.6,
            won=False,
        ),
    ]

    report = confirmation.summarize(rows, {})
    metrics = report["arms"]["baseline"]["latencies"]["400"]

    assert metrics["filled_shares"] == pytest.approx(6.0)
    assert metrics["intention_to_trade_pnl"] == pytest.approx(1.4)
    assert metrics["net_ev_per_filled_share"] == pytest.approx(1.4 / 6.0)
    assert metrics["exact_p"] == pytest.approx(0.25)
    assert [row["market_id"] for row in observed] == ["full"]


def test_common_stopping_timestamp_waits_for_every_arm_and_evaluation() -> None:
    rows = []
    for arm, timestamp in (
        ("baseline", 100.0),
        ("confirmation_veto", 110.0),
        ("receipt_time_shifted_placebo", 120.0),
    ):
        for latency in (400.0, 500.0):
            rows.append(
                _summary_row(
                    arm=arm,
                    evaluation_ms=latency,
                    market_id=f"{arm}-{latency:g}",
                    signal_ms=timestamp,
                    reason="filled",
                    sent=True,
                    filled_shares=5.0,
                    pnl=1.0,
                    won=True,
                    result_ms=timestamp + latency,
                )
            )

    stopping_ms = confirmation.common_earliest_stopping_timestamp(
        rows,
        minimum_full_fills=1,
        minimum_days=1,
    )

    assert stopping_ms is not None
    assert stopping_ms == pytest.approx(620.0)
    assert stopping_ms >= max(float(row["evaluation_time_ms"]) for row in rows)


def test_common_stopping_timestamp_waits_for_match_and_result_availability() -> None:
    row = _summary_row(
        arm="baseline",
        evaluation_ms=400.0,
        market_id="m",
        signal_ms=100.0,
        reason="filled",
        sent=True,
        filled_shares=5.0,
        pnl=1.0,
        won=True,
    )
    row["match_time_ms"] = 550.0
    row["result_time_ms"] = 700.0

    stopping_ms = confirmation.common_earliest_stopping_timestamp(
        [row],
        minimum_full_fills=1,
        minimum_days=1,
        arms=("baseline",),
        evaluation_ms=(400.0,),
    )

    assert stopping_ms == pytest.approx(700.0)


def test_common_stopping_timestamp_rejects_unknown_result_availability() -> None:
    row = _summary_row(
        arm="baseline",
        evaluation_ms=400.0,
        market_id="m",
        signal_ms=100.0,
        reason="filled",
        sent=True,
        filled_shares=5.0,
        pnl=1.0,
        won=True,
    )

    with pytest.raises(ValueError, match="result availability timestamp"):
        confirmation.common_earliest_stopping_timestamp(
            [row],
            minimum_full_fills=1,
            minimum_days=1,
            arms=("baseline",),
            evaluation_ms=(400.0,),
        )


def _strict_fixture(
    *,
    secondary_recv_ms: float = 106_200.0,
    secondary_direction: int = 1,
    primary_recv_ms: float = 106_300.0,
    second_primary_recv_ms: float | None = None,
    decision_depth: float = 10.0,
    disconnect_secondary_ms: float | None = None,
    equal_primary_clob: bool = False,
    equal_evaluation_clob: bool = False,
    reconnect_clob: bool = False,
    reconnect_primary: bool = False,
    reconnect_secondary: bool = False,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, str],
    h_replay.ReplayConfig,
    h_replay_run.SignalConfig,
]:
    source: list[dict[str, Any]] = []
    stream_sequences: Counter[str] = Counter()

    def add_source(
        kind: str,
        receive_ms: float,
        stream: str,
        *,
        source_ms: float | None = None,
        epoch: int = 1,
        **values: Any,
    ) -> None:
        sequence = stream_sequences[stream]
        stream_sequences[stream] += 1
        source.append(
            {
                "kind": kind,
                "recv_ms": receive_ms,
                "source_ts_ms": source_ms,
                "stream": stream,
                "stream_sequence": sequence,
                "connection_epoch": epoch,
                **values,
            }
        )

    for offset, stream in enumerate(("spot", "futures", "futures_trade", "deribit")):
        add_source(f"{stream}_connection", 99_000.0 + offset, stream)
    for second, price in zip(
        range(100, 107),
        (100.0, 100.01, 100.0, 100.01, 100.0, 100.01, 100.0),
    ):
        add_source(
            "spot_trade",
            second * 1_000.0 + 100.0,
            "spot",
            source_ms=second * 1_000.0,
            price=price,
            size=1.0,
        )
    for source_ms, receive_ms in ((104_000.0, 104_050.0), (105_000.0, 105_050.0)):
        add_source(
            "futures_bbo",
            receive_ms,
            "futures",
            source_ms=source_ms,
            bid=99.99,
            ask=100.01,
        )
    primary_epoch = 1
    if reconnect_primary:
        add_source("futures_disconnect", 105_100.0, "futures")
        add_source("futures_connection", 105_110.0, "futures", epoch=2)
        primary_epoch = 2
    for source_ms, receive_ms in ((104_000.0, 104_060.0), (105_000.0, 105_060.0)):
        add_source(
            "futures_trade",
            receive_ms,
            "futures_trade",
            source_ms=source_ms,
            price=100.0,
            size=1.0,
        )
    secondary_price = 102.0 if secondary_direction > 0 else 98.0
    add_source(
        "futures_trade",
        secondary_recv_ms,
        "futures_trade",
        source_ms=106_000.0,
        price=secondary_price,
        size=1.0,
    )
    if disconnect_secondary_ms is not None:
        add_source(
            "futures_trade_disconnect",
            disconnect_secondary_ms,
            "futures_trade",
        )
        if reconnect_secondary:
            add_source(
                "futures_trade_connection",
                disconnect_secondary_ms,
                "futures_trade",
                epoch=2,
            )
    add_source(
        "futures_bbo",
        primary_recv_ms,
        "futures",
        source_ms=106_000.0,
        epoch=primary_epoch,
        bid=101.99,
        ask=102.01,
    )
    if second_primary_recv_ms is not None:
        add_source(
            "futures_bbo",
            second_primary_recv_ms,
            "futures",
            source_ms=106_200.0,
            epoch=primary_epoch,
            bid=102.99,
            ask=103.01,
        )
    source.sort(key=lambda row: float(row["recv_ms"]))
    for sequence, row in enumerate(source):
        row["seq"] = sequence

    mappings: list[dict[str, Any]] = [
        {
            "market_id": "m",
            "slot": 0,
            "up_token_id": "up",
            "down_token_id": "down",
        }
    ]

    clob: list[dict[str, Any]] = [
        {
            "kind": "clob_connection",
            "recv_ms": 99_010.0,
            "source_ts_ms": None,
            "connection_epoch": 1,
        }
    ]

    def add_book(
        receive_ms: float,
        source_ms: float,
        ask: float,
        depth: float,
        *,
        epoch: int = 1,
    ) -> None:
        clob.extend(
            [
                {
                    "kind": "clob_snapshot",
                    "recv_ms": receive_ms,
                    "source_ts_ms": source_ms,
                    "market_id": "m",
                    "asset_id": "up",
                    "bids": [{"price": f"{ask - 0.01:.2f}", "size": str(depth)}],
                    "asks": [{"price": f"{ask:.2f}", "size": str(depth)}],
                    "connection_epoch": epoch,
                },
                {
                    "kind": "clob_snapshot",
                    "recv_ms": receive_ms,
                    "source_ts_ms": source_ms,
                    "market_id": "m",
                    "asset_id": "down",
                    "bids": [{"price": f"{1.0 - ask:.2f}", "size": str(depth)}],
                    "asks": [{"price": f"{1.01 - ask:.2f}", "size": str(depth)}],
                    "connection_epoch": epoch,
                },
            ]
        )

    add_book(103_010.0, 103_000.0, 0.33, decision_depth)
    clob_epoch = 1
    if reconnect_clob:
        clob.extend(
            [
                {
                    "kind": "clob_error",
                    "recv_ms": 105_500.0,
                    "source_ts_ms": None,
                    "connection_epoch": 1,
                    "error": "closed",
                },
                {
                    "kind": "clob_connection",
                    "recv_ms": 105_600.0,
                    "source_ts_ms": None,
                    "connection_epoch": 2,
                },
            ]
        )
        clob_epoch = 2
    add_book(105_950.0, 105_900.0, 0.33, decision_depth, epoch=clob_epoch)
    if equal_primary_clob:
        add_book(primary_recv_ms, 106_100.0, 0.33, decision_depth, epoch=clob_epoch)
    add_book(106_650.0, 106_640.0, 0.40, 10.0, epoch=clob_epoch)
    if equal_evaluation_clob:
        add_book(primary_recv_ms + 400.0, 106_690.0, 0.40, 10.0, epoch=clob_epoch)
    add_book(106_900.0, 106_890.0, 0.40, 10.0, epoch=clob_epoch)
    clob.sort(key=lambda row: float(row["recv_ms"]))
    for sequence, row in enumerate(clob):
        row["seq"] = sequence

    execution = h_replay.ReplayConfig(
        local_path_ms=0.9,
        order_wire_ms=15.0,
        venue_hold_ms=150.0,
        evaluation_ms=(400.0, 500.0),
        theta=0.12,
        tick=0.01,
        fee_rate=0.07,
        base_shares=5.0,
        max_order_usd=10.0,
        signal_time_basis="local_receipt_timestamp",
        retry_after_no_send_or_kill=True,
        reentry_enabled=False,
        max_orders_per_min=1,
    )
    signal = h_replay_run.SignalConfig(
        z0=2.0,
        source_z0={"deribit_quote": 3.0},
        tau_hi_s=300.0,
        tau_lo_s=0.0,
        sigma_window_s=10,
        sigma_min_observations=3,
    )
    return source, clob, mappings, {"m": "Up"}, execution, signal


def _run_fixture(**changes: Any) -> tuple[list[dict[str, Any]], dict[str, int]]:
    source, clob, mappings, outcomes, execution, signal = _strict_fixture(**changes)
    return confirmation.replay_normalized(
        source,
        clob,
        mappings,
        outcomes,
        execution,
        signal,
    )


def _arm_row(
    rows: list[dict[str, Any]],
    arm: str,
    latency: float = 400.0,
) -> dict[str, Any]:
    return next(
        row
        for row in rows
        if row["arm"] == arm and float(row["evaluation_ms"]) == latency
    )


def test_confirmed_primary_keeps_send_time_and_uses_existing_fak_fee_and_outcome() -> (
    None
):
    rows, _ = _run_fixture()
    baseline = _arm_row(rows, "baseline")
    confirmed = _arm_row(rows, "confirmation_veto")
    placebo = _arm_row(rows, "receipt_time_shifted_placebo")

    assert baseline["sent"] is True and confirmed["sent"] is True
    assert confirmed["confirmation_reason"] == "confirmed"
    assert confirmed["same_secondary_sources"] == ("futures_trade",)
    assert confirmed["signal_receive_ms"] == baseline["signal_receive_ms"]
    assert confirmed["send_ms"] == baseline["send_ms"] == pytest.approx(106_300.9)
    assert confirmed["fixed_limit"] == baseline["fixed_limit"]
    assert confirmed["filled"] is True
    assert float(confirmed["filled_shares"]) >= 5.0
    assert confirmed["fill_fee"] == pytest.approx(
        float(confirmed["filled_shares"])
        * h_replay.taker_fee(float(confirmed["fill_price"]), 0.07)
    )
    assert confirmed["pnl"] == pytest.approx(
        float(confirmed["filled_shares"])
        - float(confirmed["filled_shares"]) * float(confirmed["fill_price"])
        - float(confirmed["fill_fee"])
    )
    assert placebo["sent"] is False
    assert placebo["pnl"] == 0.0


def test_first_primary_is_consumed_when_vetoed_and_later_confirmed_primary_cannot_replace_it() -> (
    None
):
    rows, _ = _run_fixture(
        secondary_recv_ms=106_350.0,
        second_primary_recv_ms=106_450.0,
    )
    candidate = [row for row in rows if row["arm"] == "confirmation_veto"]

    assert len(candidate) == 2
    assert {row["signal_receive_ms"] for row in candidate} == {106_300.0}
    assert {row["reason"] for row in candidate} == {"no_same_direction_secondary"}
    assert all(row["sent"] is False and row["pnl"] == 0.0 for row in candidate)


def test_equal_receipt_and_disconnected_secondary_fail_closed() -> None:
    equal_rows, _ = _run_fixture(secondary_recv_ms=106_300.0)
    disconnected_rows, _ = _run_fixture(disconnect_secondary_ms=106_250.0)

    assert (
        _arm_row(equal_rows, "confirmation_veto")["reason"] == "equal_receipt_ambiguous"
    )
    assert (
        _arm_row(disconnected_rows, "confirmation_veto")["reason"]
        == "secondary_source_unavailable"
    )


def test_equal_receipt_candidate_disconnect_reconnect_preserves_stream_order() -> None:
    rows, _ = _run_fixture(
        secondary_recv_ms=106_200.0,
        disconnect_secondary_ms=106_200.0,
        reconnect_secondary=True,
    )

    confirmation_row = _arm_row(rows, "confirmation_veto")
    placebo_row = _arm_row(rows, "receipt_time_shifted_placebo")
    assert confirmation_row["reason"] == "no_same_direction_secondary"
    assert placebo_row["reason"] == "no_same_direction_secondary"


def test_equal_receipt_clob_is_ambiguous_at_the_primary_decision() -> None:
    rows, _ = _run_fixture(equal_primary_clob=True)

    assert len(rows) == 6
    assert {row["reason"] for row in rows} == {"equal_receipt_ambiguous"}
    assert all(row["sent"] is False and row["pnl"] == 0.0 for row in rows)


def test_equal_receipt_clob_is_ambiguous_at_the_evaluation_horizon() -> None:
    rows, _ = _run_fixture(equal_evaluation_clob=True)

    for arm in ("baseline", "confirmation_veto"):
        tied = _arm_row(rows, arm, latency=400.0)
        later = _arm_row(rows, arm, latency=500.0)
        assert tied["sent"] is True
        assert tied["reason"] == "equal_receipt_ambiguous"
        assert tied["filled"] is False and tied["pnl"] == 0.0
        assert later["reason"] == "filled"


def test_source_and_clob_reconnects_cannot_reuse_pre_disconnect_references() -> None:
    source_rows, _ = _run_fixture(reconnect_primary=True)
    clob_rows, clob_counters = _run_fixture(reconnect_clob=True)

    assert source_rows == []
    assert clob_rows == []
    assert clob_counters["primary_no_anchor"] == 1


def test_opposite_confirmation_vetoes_and_direct_depth_below_five_never_sends() -> None:
    opposite_rows, _ = _run_fixture(secondary_direction=-1)
    shallow_rows, _ = _run_fixture(decision_depth=4.999)

    opposite = _arm_row(opposite_rows, "confirmation_veto")
    shallow = _arm_row(shallow_rows, "confirmation_veto")
    assert opposite["reason"] == "opposite_secondary"
    assert opposite["sent"] is False and opposite["pnl"] == 0.0
    assert shallow["reason"] == "insufficient_decision_depth"
    assert shallow["decision_direct_depth"] == pytest.approx(4.999)
    assert shallow["sent"] is False and shallow["pnl"] == 0.0


def test_each_arm_reuses_the_execution_engines_global_rate_limit() -> None:
    execution = h_replay.ReplayConfig(
        local_path_ms=0.9,
        order_wire_ms=15.0,
        venue_hold_ms=150.0,
        evaluation_ms=(400.0, 500.0),
        theta=0.12,
        tick=0.01,
        fee_rate=0.07,
        base_shares=5.0,
        max_order_usd=10.0,
        signal_time_basis="local_receipt_timestamp",
        retry_after_no_send_or_kill=True,
        reentry_enabled=False,
        max_orders_per_min=1,
    )
    mappings = [
        {
            "market_id": "first",
            "slot": 0,
            "up_token_id": "first-up",
            "down_token_id": "first-down",
        },
        {
            "market_id": "second",
            "slot": 300,
            "up_token_id": "second-up",
            "down_token_id": "second-down",
        },
    ]
    markets, tokens = h_replay_run._mapping_dict(mappings)
    for source_ms, market in zip((900.0, 1_900.0), markets.values(), strict=True):
        market.up.replace(
            [{"price": "0.30", "size": "10"}],
            [{"price": "0.40", "size": "10"}],
            source_ms,
        )
        market.down.replace(
            [{"price": "0.60", "size": "10"}],
            [{"price": "0.70", "size": "10"}],
            source_ms,
        )
    arm = confirmation._ArmState(
        "baseline",
        h_replay.HReplay(execution),
        markets,
        tokens,
        connected=True,
    )
    accepted = confirmation.ConfirmationDecision(True, "primary_control")
    outcomes = {"first": "Up", "second": "Up"}

    for market_id, slot, receive_ms in (
        ("first", 0, 1_000.0),
        ("second", 300, 2_000.0),
    ):
        confirmation._dispatch_opportunity(
            arm,
            confirmation._Opportunity(
                opportunity_id=f"{market_id}:primary",
                market_id=market_id,
                slot=slot,
                receive_ms=receive_ms,
                source_ms=receive_ms,
                source="futures_book_ticker",
                direction=1,
                fair=0.95,
            ),
            accepted,
            outcomes,
            execution,
        )
    arm.machine.feed(
        {
            "kind": "watermark",
            "receive_ms": 3_000.0,
            "source_ms": 3_000.0,
        }
    )
    rows = [
        confirmation._score_machine_row(row, arm, outcomes, set())
        for row in arm.machine.records
    ]

    first = [row for row in rows if row["market_id"] == "first"]
    second = [row for row in rows if row["market_id"] == "second"]
    assert len(first) == len(second) == 2
    assert all(row["sent"] is True for row in first)
    assert {row["reason"] for row in second} == {"rate"}
    assert all(row["sent"] is False and row["pnl"] == 0.0 for row in second)


def test_strict_sequence_gap_is_rejected() -> None:
    source, clob, mappings, outcomes, execution, signal = _strict_fixture()
    source[5]["seq"] = int(source[5]["seq"]) + 1

    with pytest.raises(ValueError, match="non-contiguous source sequence"):
        confirmation.replay_normalized(
            source,
            clob,
            mappings,
            outcomes,
            execution,
            signal,
        )


def test_replay_and_summary_are_deterministic() -> None:
    first_rows, first_counters = _run_fixture()
    second_rows, second_counters = _run_fixture()

    assert first_rows == second_rows
    assert first_counters == second_counters
    assert confirmation.summarize(first_rows, first_counters) == confirmation.summarize(
        second_rows,
        second_counters,
    )


def test_archive_adapter_filters_pre_cutoff_and_uses_observable_result_time(
    tmp_path, monkeypatch
) -> None:
    strict = tmp_path / "strict"
    strict.mkdir()
    (strict / "manifest.json").write_text("{}\n", encoding="utf-8")
    manifest = {
        "schema": "polymarket-5m-strict-replay-v2",
        "complete": True,
        "run_id": "run-family36",
        "collector_region": "eu-west-1",
        "receipt_race_ready": True,
        "started_ms": 1_000.0,
        "ended_ms": 500_000.0,
    }
    identity = {
        "run_id": "run-family36",
        "collector_region": "eu-west-1",
        "receipt_race_ready": True,
        "started_ms": 1_000.0,
        "ended_ms": 500_000.0,
        "files": {"manifest.json": "sha"},
    }
    artifact_stats = {"manifest.json": (3, 4, 5)}
    mappings = [{
        "market_id": "m",
        "slot": 100,
        "up_token_id": "up",
        "down_token_id": "down",
    }]
    outcomes = [{
        "market_id": "m",
        "winner": "Up",
        "recv_ms": 399_000.0,
        "source_ts_ms": 300_000.0,
    }]
    before = _summary_row(
        arm="baseline",
        evaluation_ms=400.0,
        market_id="m",
        signal_ms=999.0,
        reason="filled",
        sent=True,
        filled_shares=5.0,
        pnl=1.0,
        won=True,
    )
    after = copy.deepcopy(before)
    after["signal_receive_ms"] = 1_000.0
    after["evaluation_time_ms"] = 1_400.0
    no_fill = copy.deepcopy(after)
    no_fill.update(
        signal_receive_ms=1_001.0,
        evaluation_time_ms=1_401.0,
        filled=False,
        full_fill=False,
        filled_shares=0.0,
        pnl=0.0,
        won=None,
        reason="killed",
    )

    monkeypatch.setattr(
        confirmation.source_race,
        "_capture_artifact_identity",
        lambda _: (identity, artifact_stats, strict),
    )
    monkeypatch.setattr(
        confirmation.source_race,
        "_artifact_stats",
        lambda _: artifact_stats,
    )
    monkeypatch.setattr(
        confirmation.archive,
        "validate_standard_artifact",
        lambda _: {"manifest": manifest, "counts": {}},
    )
    monkeypatch.setattr(
        confirmation.archive,
        "iter_market_mappings",
        lambda _: iter(mappings),
    )
    monkeypatch.setattr(
        confirmation.archive,
        "iter_outcomes",
        lambda _: iter(outcomes),
    )
    monkeypatch.setattr(
        confirmation.archive,
        "iter_normalized_events",
        lambda *_args, **_kwargs: iter(()),
    )
    monkeypatch.setattr(
        confirmation,
        "replay_normalized",
        lambda *_args, **_kwargs: ([before, after, no_fill], {"events": 1}),
    )

    rows_out = tmp_path / "family36.jsonl"
    result = confirmation.replay_archive(
        tmp_path,
        rows_out=rows_out,
        prospective_cutoff_ms=1_000.0,
    )

    persisted = [
        json.loads(line)
        for line in rows_out.read_text(encoding="utf-8").splitlines()
    ]
    assert len(persisted) == 2
    filled, killed = persisted
    assert filled["signal_receive_ms"] == 1_000.0
    assert filled["result_time_ms"] == 400_000.0
    assert killed.get("result_time_ms") is None
    assert filled["observation_run_id"] == "run-family36"
    assert filled["family"] == 36
    assert filled["artifact_started_ms"] == 1_000.0
    assert filled["artifact_ended_ms"] == 500_000.0
    assert filled["artifact_collector_region"] == "eu-west-1"
    assert filled["artifact_receipt_race_ready"] is True
    assert filled["artifact_files_sha256"] == result["dataset"]["artifact_files_sha256"]
    assert filled["artifact_row_count"] == 2
    assert killed["artifact_row_count"] == 2
    assert filled["artifact_rows_sha256"] == result["dataset"]["artifact_rows_sha256"]
    unbound = [
        {
            key: value
            for key, value in row.items()
            if key not in {"artifact_row_count", "artifact_rows_sha256"}
        }
        for row in persisted
    ]
    assert confirmation.source_race._canonical_digest(unbound) == filled[
        "artifact_rows_sha256"
    ]
    assert result["dataset"]["excluded_pre_cutoff_rows"] == 1
    assert result["dataset"]["eligible_rows"] == 2
    assert result["dataset"]["paper_gate_eligible"] is True
    assert result["promotion_eligible"] is False


def test_archive_adapter_rejects_non_receipt_race_artifact(tmp_path, monkeypatch) -> None:
    strict = tmp_path / "strict"
    strict.mkdir()
    (strict / "manifest.json").write_text("{}\n", encoding="utf-8")
    identity = {
        "run_id": "bad",
        "collector_region": "eu-west-1",
        "receipt_race_ready": False,
        "files": {},
    }
    monkeypatch.setattr(
        confirmation.source_race,
        "_capture_artifact_identity",
        lambda _: (identity, {}, strict),
    )

    with pytest.raises(ValueError, match="receipt-race-ready"):
        confirmation.replay_archive(tmp_path)
