import dataclasses

import pytest

import h_fill_gate as gate
import h_replay as replay


def _config():
    return replay.ReplayConfig(
        local_path_ms=0.0,
        order_wire_ms=0.0,
        venue_hold_ms=0.0,
        evaluation_ms=(300.0,),
        signal_time_basis="local_receipt_timestamp",
        retry_after_no_send_or_kill=True,
        reentry_enabled=False,
        max_orders_per_min=0,
        max_order_usd=5.0,
    )


def _snapshot(receive_ms, up_ask=0.40, market_id="m"):
    return {
        "kind": "snapshot",
        "market_id": market_id,
        "receive_ms": float(receive_ms),
        "source_ms": float(receive_ms),
        "up_asks": ((up_ask, 10.0),),
        "down_asks": ((1.0 - up_ask + 0.01, 10.0),),
        "levels_normalized": True,
    }


def _signal(receive_ms, fair=0.70, market_id="m"):
    return {
        "kind": "signal",
        "market_id": market_id,
        "receive_ms": float(receive_ms),
        "source_ms": float(receive_ms - 5),
        "signal_source": "spot_trade",
        "direction": "Up",
        "fair": fair,
        "trigger_reason": "trigger",
    }


def _run(machine, events):
    for event in events:
        machine.feed(event)
    machine.finish(force=False)
    return machine.records


def test_allow_all_is_byte_for_byte_identical_to_canonical_h_replay():
    events = [_snapshot(0), _signal(100), _snapshot(400)]
    expected = _run(replay.HReplay(_config()), events)
    wrapped = gate.HFillGateReplay(_config(), lambda _: gate.GateVerdict.pass_order(score=0.9))

    actual = _run(wrapped, events)

    assert actual == expected
    assert len(wrapped.gate_decisions) == 1
    assert wrapped.gate_decisions[0].verdict.allow


def test_veto_consumes_no_h_state_and_allows_the_next_original_h_signal():
    calls = []

    def policy(decision):
        calls.append(decision)
        return gate.GateVerdict(allow=len(calls) > 1, score=0.1 * len(calls), reason="test")

    wrapped = gate.HFillGateReplay(_config(), policy)
    rows = _run(wrapped, [_snapshot(0), _signal(100), _signal(110), _snapshot(410)])

    assert len(calls) == 2
    assert [item.verdict.allow for item in wrapped.gate_decisions] == [False, True]
    assert len(rows) == 1
    assert rows[0]["signal_receive_ms"] == 110.0
    assert rows[0]["filled"]


def test_gate_observes_a_branch_released_by_the_canonical_pre_signal_drain():
    calls = []

    def policy(decision):
        calls.append(decision.signal_receive_ms)
        return gate.GateVerdict(allow=len(calls) == 1, score=1.0, reason="test")

    wrapped = gate.HFillGateReplay(_config(), policy)
    wrapped.feed(_snapshot(0))
    wrapped.feed(_signal(100))
    empty = _snapshot(400)
    empty["up_asks"] = ((0.40, 0.0),)
    wrapped.feed(empty)
    released = wrapped.feed(_signal(401))

    assert calls == [100.0, 401.0]
    assert len(released) == 1
    assert released[0]["signal_receive_ms"] == 100.0
    assert not wrapped.gate_decisions[-1].verdict.allow
    wrapped.finish(force=False)
    assert len(wrapped.records) == 1


def test_gate_sees_only_frozen_decision_fields_and_exact_h_limit():
    captured = []
    wrapped = gate.HFillGateReplay(
        _config(), lambda decision: captured.append(decision) or gate.GateVerdict.pass_order(),
    )

    _run(wrapped, [_snapshot(0), _signal(100), _snapshot(400)])

    decision = captured[0]
    assert dataclasses.is_dataclass(decision)
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.fair = 0.1
    assert decision.market_id == "m"
    assert decision.side == "Up"
    assert decision.fair == pytest.approx(0.70)
    assert decision.decision_ask == pytest.approx(0.40)
    assert decision.fixed_limit == replay.limit_price(0.70, _config().theta)
    assert decision.net_edge == pytest.approx(
        0.70 - 0.40 - replay.taker_fee(0.40, _config().fee_rate)
    )
    forbidden = {"winner", "won", "filled", "fill_price", "match_price", "pnl", "up_won"}
    assert forbidden.isdisjoint(field.name for field in dataclasses.fields(decision))


def test_gate_is_not_called_when_h_itself_cannot_send():
    calls = []
    wrapped = gate.HFillGateReplay(
        _config(), lambda decision: calls.append(decision) or gate.GateVerdict.pass_order(),
    )

    rows = _run(wrapped, [_snapshot(0, up_ask=0.65), _signal(100), _snapshot(400, up_ask=0.65)])

    assert not calls
    assert len(rows) == 1
    assert not rows[0]["sent"]
    assert rows[0]["decision_reason"] == "ask_above_limit"


def test_nonfinite_decision_ask_cannot_reach_the_gate():
    calls = []
    wrapped = gate.HFillGateReplay(
        _config(), lambda decision: calls.append(decision) or gate.GateVerdict.pass_order(),
    )
    broken = _snapshot(0)
    broken["up_asks"] = ((float("nan"), 10.0),)

    rows = _run(wrapped, [broken, _signal(100), _snapshot(400)])

    assert not calls
    assert len(rows) == 1
    assert not rows[0]["sent"]


def test_gate_verdict_rejects_nonfinite_scores():
    with pytest.raises(ValueError, match="finite"):
        gate.GateVerdict(allow=True, score=float("nan"))


def test_gate_verdict_requires_a_real_boolean_and_reason():
    with pytest.raises(TypeError, match="bool"):
        gate.GateVerdict(allow=1)
    with pytest.raises(ValueError, match="reason"):
        gate.GateVerdict(allow=True, reason="")


@pytest.mark.parametrize("bad_policy", [lambda _: object(), lambda _: 1 / 0])
def test_policy_contract_failure_permanently_latches_the_replay(bad_policy):
    wrapped = gate.HFillGateReplay(_config(), bad_policy)
    wrapped.feed(_snapshot(0))

    with pytest.raises(gate.GatePolicyError, match="cannot continue"):
        wrapped.feed(_signal(100))
    with pytest.raises(gate.GatePolicyError, match="permanently failed"):
        wrapped.feed(_snapshot(400))
    with pytest.raises(gate.GatePolicyError, match="permanently failed"):
        wrapped.finish()


def test_multi_horizon_freeze_is_split_into_independent_gate_branches():
    config = dataclasses.replace(_config(), evaluation_ms=(300.0, 350.0, 400.0, 500.0))
    with pytest.raises(ValueError, match="one independent"):
        gate.HFillGateReplay(config, lambda _: gate.GateVerdict.pass_order())
    primary = gate.single_horizon_config(config, 500.0)
    assert primary.evaluation_ms == (500.0,)
    assert dataclasses.replace(primary, evaluation_ms=config.evaluation_ms) == config
    with pytest.raises(ValueError, match="frozen H"):
        gate.single_horizon_config(config, 450.0)


def test_single_500ms_branch_is_the_exact_projection_of_canonical_multi_horizon_h():
    multi = dataclasses.replace(_config(), evaluation_ms=(300.0, 350.0, 400.0, 500.0))
    single = gate.single_horizon_config(multi, 500.0)
    events = [
        _snapshot(0), _signal(100), _snapshot(400), _snapshot(450),
        _snapshot(500), _snapshot(600), _snapshot(601),
    ]

    multi_rows = [row for row in _run(replay.HReplay(multi), events) if row["evaluation_ms"] == 500.0]
    single_rows = _run(replay.HReplay(single), events)

    assert single_rows == multi_rows


def test_allow_all_matches_exchange_source_clock_with_delayed_receipts():
    config = dataclasses.replace(_config(), signal_time_basis="exchange_source_timestamp")
    first = _snapshot(90)
    first["source_ms"] = 80.0
    signal = _signal(100)
    signal["source_ms"] = 95.0
    later = _snapshot(420)
    later["source_ms"] = 396.0
    events = [first, signal, later]

    expected = _run(replay.HReplay(config), events)
    wrapped = gate.HFillGateReplay(config, lambda _: gate.GateVerdict.pass_order())
    actual = _run(wrapped, events)

    assert actual == expected
    assert actual[0]["match_book_clock"] == "source_ms"
    assert wrapped.gate_decisions[0].decision.signal_ms == 95.0


def test_source_clock_veto_preserves_canonical_post_signal_drain_for_other_market():
    config = dataclasses.replace(_config(), signal_time_basis="exchange_source_timestamp")
    wrapped = gate.HFillGateReplay(config, lambda _: gate.GateVerdict.pass_order())
    first = _signal(100, market_id="a")
    first["source_ms"] = 100.0
    wrapped.feed(_snapshot(90, market_id="a"))
    wrapped.feed(_snapshot(90, market_id="b"))
    wrapped.feed(first)
    wrapped.feed({"kind": "watermark", "receive_ms": 399.0, "source_ms": 401.0})
    wrapped.policy = lambda _: gate.GateVerdict(False, 0.1, "veto")
    second = _signal(400, market_id="b")
    second["source_ms"] = 200.0

    drained = wrapped.feed(second)

    assert len(drained) == 1
    assert drained[0]["market_id"] == "a"
    assert not wrapped.pending_evaluations()


def test_rate_blocked_and_disconnected_signals_bypass_the_gate_but_keep_h_records():
    calls = []
    config = dataclasses.replace(_config(), max_orders_per_min=1)
    wrapped = gate.HFillGateReplay(
        config, lambda decision: calls.append(decision) or gate.GateVerdict.pass_order(),
    )
    empty = _snapshot(400)
    empty["up_asks"] = ((0.40, 0.0),)
    events = [
        _snapshot(0), _signal(100), empty, _signal(1_000), _snapshot(1_300),
        {"kind": "disconnect", "market_id": "m", "receive_ms": 1_301, "source_ms": 1_301},
        _signal(1_302), _snapshot(1_603),
    ]

    rows = _run(wrapped, events)

    assert len(calls) == 1
    assert [row["decision_reason"] for row in rows] == ["sent", "rate", "book_unavailable_at_decision"]
    assert [row["reason"] for row in rows] == [
        "insufficient_effective_depth", "rate", "book_unavailable_at_decision",
    ]


@pytest.mark.parametrize(
    "changes",
    [
        {"retry_after_no_send_or_kill": False},
        {"reentry_enabled": True},
    ],
)
def test_wrapper_fails_closed_outside_the_frozen_current_h_branch_semantics(changes):
    config = dataclasses.replace(_config(), **changes)
    with pytest.raises(ValueError, match="current-H veto"):
        gate.HFillGateReplay(config, lambda _: gate.GateVerdict.pass_order())
