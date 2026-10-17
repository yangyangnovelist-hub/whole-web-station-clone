import pytest

import adverse_reclaim as reclaim

BASE_PATH = [0.61] * 6 + [0.50] * 5 + [0.52, 0.55, 0.58, 0.60, 0.61]
SHALLOW_PATH = [0.61] * 14 + [0.60, 0.61]
QUIET_PATH = [0.30] * 16
MARKET = "m1"
UP = "up"
DOWN = "down"


def receipt_ms(row: dict[str, object]) -> float:
    value = row["recv_ms"]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("test event receipt must be numeric")
    return float(value)


def mapping(market: str = MARKET, slot: int = 0) -> dict[str, object]:
    return {
        "market_id": market,
        "slot": slot,
        "up_token_id": f"{market}-{UP}",
        "down_token_id": f"{market}-{DOWN}",
        "fee_rate": 0.07,
    }


def physical_events(
    up_path: list[float],
    *,
    down_path: list[float] | None = None,
    market: str = MARKET,
    slot: int = 0,
    decision_side: str = UP,
    decision_ask_size: float = 10.0,
    delay_ask: float | None = 0.61,
) -> list[dict[str, object]]:
    down_path = down_path or QUIET_PATH
    market_map = mapping(market, slot)
    origin = slot * 1_000
    raw: list[dict[str, object]] = [{
        "kind": "clob_connection",
        "recv_ms": float(origin),
        "source_ts_ms": None,
        "connection_epoch": 1,
    }]
    paths = (
        (DOWN, down_path, "down_token_id", 44_850),
        (UP, up_path, "up_token_id", 44_900),
    )
    for side, path, token_key, first_receive_ms in paths:
        for index, midpoint in enumerate(path):
            is_decision_book = side == decision_side and index == len(path) - 1
            ask_size = decision_ask_size if is_decision_book else 10.0
            receive_ms = float(origin + first_receive_ms + index * 1_000)
            raw.append({
                "kind": "clob_snapshot",
                "recv_ms": receive_ms,
                "source_ts_ms": receive_ms,
                "connection_epoch": 1,
                "market_id": market,
                "asset_id": market_map[token_key],
                "bids": [{"price": midpoint - 0.01, "size": 10.0}],
                "asks": [{"price": midpoint + 0.01, "size": ask_size}],
                "tick_size": "0.01",
            })
    if delay_ask is not None:
        token_key = (
            "up_token_id" if decision_side == UP else "down_token_id"
        )
        raw.append({
            "kind": "clob_snapshot",
            "recv_ms": float(origin + 65_300),
            "source_ts_ms": float(origin + 65_300),
            "connection_epoch": 1,
            "market_id": market,
            "asset_id": market_map[token_key],
            "bids": [{"price": delay_ask - 0.02, "size": 10.0}],
            "asks": [{"price": delay_ask, "size": 10.0}],
            "tick_size": "0.01",
        })
    raw.sort(key=receipt_ms)
    for sequence, row in enumerate(raw):
        row["seq"] = sequence
        row["physical_batch_id"] = f"batch-{sequence}"
    return raw


def test_direct_paths_are_classified_without_synthetic_complements() -> None:
    assert any(
        up + down != pytest.approx(1.0)
        for up, down in zip(QUIET_PATH, BASE_PATH, strict=True)
    )
    assert reclaim.classify_path(BASE_PATH, QUIET_PATH) == ("base", "Up")
    assert reclaim.classify_path(QUIET_PATH, BASE_PATH) == ("base", "Down")


def test_boundary_is_rejected_when_both_direct_tokens_qualify() -> None:
    assert reclaim.classify_path(BASE_PATH, BASE_PATH) is None
    assert reclaim.classify_path(BASE_PATH, SHALLOW_PATH) is None

    events = physical_events(
        BASE_PATH,
        down_path=SHALLOW_PATH,
        delay_ask=None,
    )
    paths = ((DOWN, QUIET_PATH, 64_850.0), (UP, BASE_PATH, 64_900.0))
    for side, path, first_receive_ms in paths:
        for index, midpoint in enumerate(path):
            receive_ms = first_receive_ms + index * 1_000.0
            events.append({
                "kind": "clob_snapshot",
                "recv_ms": receive_ms,
                "source_ts_ms": receive_ms,
                "connection_epoch": 1,
                "market_id": MARKET,
                "asset_id": f"{MARKET}-{side}",
                "bids": [{"price": midpoint - 0.01, "size": 10.0}],
                "asks": [{"price": midpoint + 0.01, "size": 10.0}],
                "tick_size": "0.01",
            })
    events.sort(key=receipt_ms)
    for sequence, row in enumerate(events):
        row["seq"] = sequence
        row["physical_batch_id"] = f"dual-consume-{sequence}"

    rows, counters = reclaim.replay(
        events,
        [mapping()],
        {MARKET: "Up"},
        segment_end_ms=81_000,
    )

    assert rows == []
    assert counters["both_direct_qualified_rejected"] == 1
    assert counters["base_after_consumption"] >= 1


def test_shallow_control_keeps_the_crossing_but_removes_path_memory() -> None:
    assert SHALLOW_PATH[0] == BASE_PATH[0]
    assert SHALLOW_PATH[-1] == BASE_PATH[-1]
    assert reclaim.classify_path(SHALLOW_PATH, QUIET_PATH) == (
        "shallow",
        "Up",
    )


def test_base_replay_freezes_limit_and_runs_both_horizons_and_delay() -> None:
    rows, counters = reclaim.replay(
        physical_events(BASE_PATH),
        [mapping()],
        {MARKET: "Up"},
        segment_end_ms=66_000,
    )

    base = [row for row in rows if row["variant"] == "base"]
    delayed = [row for row in rows if row["variant"] == "delay_5s"]
    assert [row["evaluation_ms"] for row in base] == [400.0, 500.0]
    assert [row["evaluation_ms"] for row in delayed] == [400.0, 500.0]
    assert all(row["fixed_limit"] == pytest.approx(0.62) for row in base + delayed)
    assert all(row["fill_vwap"] == pytest.approx(0.62) for row in base)
    assert all(row["fill_vwap"] == pytest.approx(0.61) for row in delayed)
    assert all(row["fee"] == pytest.approx(0.08246) for row in base)
    assert all(row["pnl"] == pytest.approx(1.81754) for row in base)
    assert all(row["buy_side"] == "Up" for row in base + delayed)
    assert all(row["parent_signal_id"] for row in delayed)
    assert counters["base_signals"] == 1
    assert counters["delay_5s_signals"] == 1


def test_grid_equal_to_a_physical_batch_is_missing_not_optimistic() -> None:
    events = physical_events(BASE_PATH, delay_ask=None)
    final = next(row for row in events if row["recv_ms"] == 59_900.0)
    final["recv_ms"] = 60_000.0
    final["source_ts_ms"] = 60_000.0
    events.sort(key=receipt_ms)
    for sequence, row in enumerate(events):
        row["seq"] = sequence
        row["physical_batch_id"] = f"retimed-{sequence}"

    rows, counters = reclaim.replay(
        events,
        [mapping()],
        {MARKET: "Up"},
        segment_end_ms=61_000,
    )

    assert not [row for row in rows if row["variant"] == "base"]
    assert counters["grid_same_time_ambiguous"] == 1


def test_missing_one_second_observation_is_not_interpolated() -> None:
    events = [
        row
        for row in physical_events(BASE_PATH, delay_ask=None)
        if row["recv_ms"] != 54_900.0
    ]
    for sequence, row in enumerate(events):
        row["seq"] = sequence
        row["physical_batch_id"] = f"gap-{sequence}"

    rows, counters = reclaim.replay(
        events,
        [mapping()],
        {MARKET: "Up"},
        segment_end_ms=61_000,
    )

    assert not [row for row in rows if row["variant"] == "base"]
    assert counters["grid_missing"] >= 1


def test_missing_direct_down_observation_blocks_an_up_signal() -> None:
    events = [
        row
        for row in physical_events(BASE_PATH, delay_ask=None)
        if row["recv_ms"] != 54_850.0
    ]
    for sequence, row in enumerate(events):
        row["seq"] = sequence
        row["physical_batch_id"] = f"down-gap-{sequence}"

    rows, counters = reclaim.replay(
        events,
        [mapping()],
        {MARKET: "Up"},
        segment_end_ms=61_000,
    )

    assert not [row for row in rows if row["variant"] == "base"]
    assert counters["grid_missing"] >= 1


def test_first_signal_consumes_market_globally_even_when_unsent() -> None:
    events = physical_events(
        SHALLOW_PATH,
        decision_ask_size=4.0,
        delay_ask=None,
    )
    paths = ((DOWN, QUIET_PATH, 64_850.0), (UP, BASE_PATH, 64_900.0))
    for side, path, first_receive_ms in paths:
        for index, midpoint in enumerate(path):
            receive_ms = first_receive_ms + index * 1_000.0
            events.append({
                "kind": "clob_snapshot",
                "recv_ms": receive_ms,
                "source_ts_ms": receive_ms,
                "connection_epoch": 1,
                "market_id": MARKET,
                "asset_id": f"{MARKET}-{side}",
                "bids": [{"price": midpoint - 0.01, "size": 10.0}],
                "asks": [{"price": midpoint + 0.01, "size": 10.0}],
                "tick_size": "0.01",
            })
    events.sort(key=receipt_ms)
    for sequence, row in enumerate(events):
        row["seq"] = sequence
        row["physical_batch_id"] = f"consume-{sequence}"

    rows, counters = reclaim.replay(
        events,
        [mapping()],
        {MARKET: "Up"},
        segment_end_ms=81_000,
    )

    assert [row["variant"] for row in rows] == ["shallow", "shallow"]
    assert all(row["reason"] == "insufficient_decision_depth" for row in rows)
    assert all(row["signal_id"] for row in rows)
    assert all(row["fixed_limit"] is None for row in rows)
    assert counters["shallow_triggers"] == 1
    assert counters["base_triggers"] == 0
    assert counters["base_after_consumption"] >= 1
    assert counters["qualifying_after_consumption"] >= 1


def test_shallow_excursion_is_a_separate_executable_control() -> None:
    rows, counters = reclaim.replay(
        physical_events(SHALLOW_PATH, delay_ask=None),
        [mapping()],
        {MARKET: "Up"},
        segment_end_ms=61_000,
    )

    assert [row["variant"] for row in rows] == ["shallow", "shallow"]
    assert [row["evaluation_ms"] for row in rows] == [400.0, 500.0]
    assert counters["shallow_signals"] == 1
    assert counters["delay_5s_signals"] == 0


def test_unresolved_fills_censor_primary_intention_to_trade_pnl() -> None:
    rows, counters = reclaim.replay(
        physical_events(BASE_PATH),
        [mapping()],
        {},
        segment_end_ms=66_000,
    )

    report = reclaim.summarize(rows, counters)
    metrics = report["variants"]["base"]["latencies"]["400"]
    assert metrics["unresolved_fills"] == 1
    assert metrics["intention_to_trade_pnl"] is None
    assert metrics["intention_to_trade_ev_per_requested_share"] is None
    assert report["primary_metric"] == "intention_to_trade_pnl"
    assert report["promotion_eligible"] is False
    assert report["formal_pooled_fresh_gate_implemented"] is False
    assert report["correction_denominator"] >= 873
    assert report["effective_split"] == "development"


def test_no_attempts_are_missing_evidence_not_zero_edge() -> None:
    report = reclaim.summarize([], {})
    metrics = report["variants"]["base"]["latencies"]["400"]

    assert metrics["intents"] == 0
    assert metrics["fill_rate_per_intent"] is None
    assert metrics["intention_to_trade_pnl"] is None
    assert metrics["filled_share_ev"] is None


def test_execution_past_recording_watermark_is_censored_not_zero_pnl() -> None:
    rows, counters = reclaim.replay(
        physical_events(BASE_PATH, delay_ask=None),
        [mapping()],
        {MARKET: "Up"},
        segment_end_ms=60_200,
    )

    assert len(rows) == 4
    assert all(row["reason"] == "right_censored" for row in rows)
    assert all(row["pnl"] is None for row in rows)
    report = reclaim.summarize(rows, counters)
    base_400 = report["variants"]["base"]["latencies"]["400"]
    assert base_400["censored"] == 1
    assert base_400["intention_to_trade_pnl"] is None


def test_right_censored_tail_is_excluded_without_erasing_known_intents() -> None:
    known = {
        "sent": True,
        "censored": False,
        "filled": True,
        "full_fill": True,
        "filled_shares": 5.0,
        "pnl": 0.5,
        "reason": "filled",
    }
    censored = {
        "sent": False,
        "censored": True,
        "filled": False,
        "full_fill": False,
        "filled_shares": 0.0,
        "pnl": None,
        "reason": "right_censored",
    }

    metrics = reclaim._metrics([known, censored], 5.0)

    assert metrics["intents"] == 1
    assert metrics["censored"] == 1
    assert metrics["intention_to_trade_pnl"] == pytest.approx(0.5)
    assert metrics["intention_to_trade_ev_per_requested_share"] == pytest.approx(0.1)


def test_partial_fak_is_fee_charged_and_settlement_scored() -> None:
    events = physical_events(BASE_PATH, delay_ask=None)
    events.append({
        "kind": "clob_snapshot",
        "recv_ms": 60_200.0,
        "source_ts_ms": 60_200.0,
        "connection_epoch": 1,
        "market_id": MARKET,
        "asset_id": f"{MARKET}-{UP}",
        "bids": [{"price": 0.60, "size": 10.0}],
        "asks": [{"price": 0.62, "size": 2.0}],
        "tick_size": "0.01",
    })
    events.sort(key=receipt_ms)
    for sequence, row in enumerate(events):
        row["seq"] = sequence
        row["physical_batch_id"] = f"partial-{sequence}"

    rows, _counters = reclaim.replay(
        events,
        [mapping()],
        {MARKET: "Up"},
        segment_end_ms=61_000,
    )

    base = [row for row in rows if row["variant"] == "base"]
    assert all(row["reason"] == "partial_fill" for row in base)
    assert all(row["filled_shares"] == pytest.approx(2.0) for row in base)
    assert all(row["fee"] == pytest.approx(0.03298) for row in base)
    assert all(row["pnl"] == pytest.approx(0.72702) for row in base)


def test_decision_uses_the_recorded_historical_tick_not_a_one_cent_proxy() -> None:
    events = physical_events(BASE_PATH, delay_ask=None)
    final = next(row for row in events if row["recv_ms"] == 59_900.0)
    final["bids"] = [{"price": 0.605, "size": 10.0}]
    final["asks"] = [{"price": 0.615, "size": 10.0}]
    final["tick_size"] = "0.001"

    rows, _counters = reclaim.replay(
        events,
        [mapping()],
        {MARKET: "Up"},
        segment_end_ms=61_000,
    )

    base = [row for row in rows if row["variant"] == "base"]
    assert len(base) == 2
    assert all(row["sent"] is True for row in base)
    assert all(row["fixed_limit"] == pytest.approx(0.615) for row in base)
    assert all(row["tick_size"] == pytest.approx(0.001) for row in base)


def test_direct_down_path_drives_down_execution_without_complementing_up() -> None:
    rows, counters = reclaim.replay(
        physical_events(
            QUIET_PATH,
            down_path=BASE_PATH,
            decision_side=DOWN,
        ),
        [mapping()],
        {MARKET: "Down"},
        segment_end_ms=66_000,
    )

    base = [row for row in rows if row["variant"] == "base"]
    assert len(base) == 2
    assert all(row["buy_side"] == "Down" for row in base)
    assert all(row["path_start"] == pytest.approx(0.61) for row in base)
    assert all(row["adverse_excursion"] == pytest.approx(0.11) for row in base)
    assert all(row["fixed_limit"] == pytest.approx(0.62) for row in base)
    assert all(row["reason"] == "filled" for row in base)
    assert counters["base_signals"] == 1


def test_shallow_control_matching_uses_frozen_outcome_blind_strata() -> None:
    base_rows, _ = reclaim.replay(
        physical_events(BASE_PATH),
        [mapping()],
        {MARKET: "Up"},
        segment_end_ms=66_000,
    )
    shallow_rows, _ = reclaim.replay(
        physical_events(SHALLOW_PATH, market="m2", delay_ask=None),
        [mapping("m2")],
        {"m2": "Up"},
        segment_end_ms=61_000,
    )

    report = reclaim.summarize(base_rows + shallow_rows, {})
    matching = report["control_matching"]
    assert matching["outcome_blind"] is True
    assert matching["without_replacement"] is True
    assert matching["different_market_required"] is True
    assert matching["latencies"]["400"]["pairs"] == 1
    assert matching["latencies"]["500"]["pairs"] == 1


def test_future_book_update_cannot_rewrite_the_frozen_signal() -> None:
    baseline_rows, _ = reclaim.replay(
        physical_events(BASE_PATH, delay_ask=None),
        [mapping()],
        {MARKET: "Up"},
        segment_end_ms=61_000,
    )
    events = physical_events(BASE_PATH, delay_ask=None)
    events.append({
        "kind": "clob_snapshot",
        "recv_ms": 60_100.0,
        "source_ts_ms": 60_100.0,
        "connection_epoch": 1,
        "market_id": MARKET,
        "asset_id": f"{MARKET}-{UP}",
        "bids": [{"price": 0.79, "size": 10.0}],
        "asks": [{"price": 0.81, "size": 10.0}],
        "tick_size": "0.01",
    })
    events.sort(key=receipt_ms)
    for sequence, row in enumerate(events):
        row["seq"] = sequence
        row["physical_batch_id"] = f"future-{sequence}"
    future_rows, _ = reclaim.replay(
        events,
        [mapping()],
        {MARKET: "Up"},
        segment_end_ms=61_000,
    )

    baseline = next(row for row in baseline_rows if row["variant"] == "base")
    future = next(row for row in future_rows if row["variant"] == "base")
    frozen_fields = (
        "signal_ms",
        "decision_ms",
        "buy_side",
        "path_start",
        "path_end",
        "adverse_excursion",
        "fixed_limit",
    )
    assert {name: baseline[name] for name in frozen_fields} == {
        name: future[name] for name in frozen_fields
    }
    assert baseline["reason"] == "filled"
    assert future["reason"] == "ask_above_frozen_limit"


def test_path_cannot_span_connection_epochs() -> None:
    events = physical_events(BASE_PATH, delay_ask=None)
    events.extend([
        {
            "kind": "clob_error",
            "recv_ms": 52_100.0,
            "source_ts_ms": None,
            "connection_epoch": 1,
            "error": "closed",
        },
        {
            "kind": "clob_connection",
            "recv_ms": 52_200.0,
            "source_ts_ms": None,
            "connection_epoch": 2,
        },
    ])
    for row in events:
        if receipt_ms(row) > 52_200.0:
            row["connection_epoch"] = 2
    events.sort(key=receipt_ms)
    for sequence, row in enumerate(events):
        row["seq"] = sequence
        row["physical_batch_id"] = f"epoch-{sequence}"

    rows, _counters = reclaim.replay(
        events,
        [mapping()],
        {MARKET: "Up"},
        segment_end_ms=61_000,
    )

    assert not [row for row in rows if row["variant"] == "base"]


def test_archive_entrypoint_reuses_strict_physical_adapter(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        reclaim.queue_lag.archive,
        "validate_standard_artifact",
        lambda _root: {"manifest": {
            "run_id": "run-1",
            "collector_region": "eu-west-1",
            "started_ms": 0.0,
            "ended_ms": 66_000.0,
            "receipt_race_ready": True,
        }},
    )
    monkeypatch.setattr(
        reclaim.queue_lag.archive,
        "iter_market_mappings",
        lambda _path: iter([mapping()]),
    )
    monkeypatch.setattr(
        reclaim.queue_lag.archive,
        "iter_outcomes",
        lambda _path: iter([{"market_id": MARKET, "winner": "Up"}]),
    )
    monkeypatch.setattr(
        reclaim.queue_lag,
        "iter_physical_clob_events",
        lambda _path: iter(physical_events(BASE_PATH)),
    )

    result = reclaim.replay_archive(tmp_path)

    assert result["dataset"]["run_id"] == "run-1"
    assert result["record_count"] == 4
    assert result["family"] == 35
