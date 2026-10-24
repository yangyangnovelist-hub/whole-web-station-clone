import gzip
import json

import complement_queue_lag as lag
import pytest

UP = "up"
DOWN = "down"
MARKET = "m1"


def mapping():
    return [{
        "market_id": MARKET,
        "slot": 0,
        "up_token_id": UP,
        "down_token_id": DOWN,
        "fee_rate": 0.07,
    }]


def event(kind, recv_ms, seq, **fields):
    return {
        "kind": kind,
        "recv_ms": float(recv_ms),
        "source_ts_ms": float(fields.pop("source_ts_ms", recv_ms)),
        "connection_epoch": int(fields.pop("connection_epoch", 1)),
        "seq": seq,
        **fields,
    }


def snapshot(asset, recv_ms, seq, bids, asks):
    return event(
        "clob_snapshot", recv_ms, seq, market_id=MARKET, asset_id=asset,
        bids=[{"price": p, "size": s} for p, s in bids],
        asks=[{"price": p, "size": s} for p, s in asks],
    )


def change(asset, recv_ms, seq, side, price, size, **extra):
    return event(
        "clob_price_change", recv_ms, seq, market_id=MARKET, asset_id=asset,
        side=side, price=price, size=size, **extra,
    )


def base_events(*, confirmation_ms=100_100, include_same_batch_y=False):
    rows = [
        event("clob_connection", 0, 0),
        snapshot(UP, 1, 1, [(0.49, 5), (0.47, 10)], [(0.51, 10)]),
        snapshot(DOWN, 1, 2, [(0.46, 10)], [(0.48, 10)]),
        change(UP, 100_000, 3, "BUY", 0.49, 0),
        change(UP, 100_000, 4, "SELL", 0.51, 0),
        change(UP, 100_000, 5, "SELL", 0.49, 10),
    ]
    if include_same_batch_y:
        rows.append(change(DOWN, 100_000, 6, "SELL", 0.48, 10))
    else:
        rows.append(change(DOWN, confirmation_ms, 6, "SELL", 0.48, 10))
    return rows


def test_base_signal_uses_lagging_direct_token_and_exact_400_500ms_execution():
    rows, counters = lag.replay(
        base_events(), mapping(), {MARKET: "Down"}, segment_end_ms=101_000,
    )

    base = [row for row in rows if row["variant"] == "base"]
    assert [row["evaluation_ms"] for row in base] == [400.0, 500.0]
    assert all(row["buy_side"] == "Down" for row in base)
    assert all(row["fixed_limit"] == pytest.approx(0.48) for row in base)
    assert all(row["fair"] == pytest.approx(0.52) for row in base)
    assert all(row["filled_shares"] == pytest.approx(5.0) for row in base)
    assert all(row["pnl"] > 0 for row in base)
    assert counters["base_signals"] == 1


def test_signal_persists_exact_decision_depth_used_for_vwap_and_fee():
    events = base_events()
    events[2] = snapshot(
        DOWN,
        1,
        2,
        [(0.46, 10)],
        [(0.47, 2), (0.48, 3), (0.49, 10)],
    )
    events[-1] = change(DOWN, 100_100, 6, "SELL", 0.48, 3)

    rows, _ = lag.replay(
        events, mapping(), {MARKET: "Down"}, segment_end_ms=101_000,
    )

    base = [row for row in rows if row["variant"] == "base"]
    expected_levels = [
        {"price": 0.47, "shares": 2.0},
        {"price": 0.48, "shares": 3.0},
    ]
    assert base
    assert all(row.get("decision_levels") == expected_levels for row in base)
    assert all(row["decision_vwap"] == pytest.approx(0.476) for row in base)
    assert all(
        row["decision_fee"]
        == pytest.approx(lag._charged_fee([(0.47, 2.0), (0.48, 3.0)], 0.07))
        for row in base
    )


def test_signal_persists_source_clocks_for_artifact_binding():
    events = base_events()
    events[3]["source_ts_ms"] = 99_997.0
    events[4]["source_ts_ms"] = 99_998.0
    events[5]["source_ts_ms"] = 99_999.0
    events[6]["source_ts_ms"] = 100_050.0

    rows, _ = lag.replay(
        events, mapping(), {MARKET: "Down"}, segment_end_ms=101_000,
    )

    base = [row for row in rows if row["variant"] == "base"]
    assert base
    assert all(row.get("exhausted_source_ms") == 99_999.0 for row in base)
    assert all(row.get("decision_source_ms") == 100_050.0 for row in base)
    assert all(row.get("book_source_ms") == 100_050.0 for row in base)


def test_same_receipt_complement_update_is_diagnostic_and_never_traded():
    rows, counters = lag.replay(
        base_events(include_same_batch_y=True), mapping(), {MARKET: "Down"},
        segment_end_ms=101_000,
    )

    assert not [row for row in rows if row["variant"] == "base"]
    assert counters["same_batch_diagnostics"] == 1


def test_confirmation_after_frozen_250ms_window_does_not_signal():
    rows, counters = lag.replay(
        base_events(confirmation_ms=100_251), mapping(), {MARKET: "Down"},
        segment_end_ms=101_000,
    )

    assert not rows
    assert counters["confirmation_too_late"] == 1


def test_match_tied_with_a_book_update_fails_closed():
    events = base_events()
    events.append(change(DOWN, 100_500, 7, "SELL", 0.48, 0))
    events.append(change(DOWN, 100_500, 8, "SELL", 0.50, 10))
    rows, _ = lag.replay(events, mapping(), {MARKET: "Down"}, segment_end_ms=101_000)

    by_latency = {row["evaluation_ms"]: row for row in rows if row["variant"] == "base"}
    assert by_latency[400.0]["reason"] == "equal_receipt_ambiguous"
    assert by_latency[400.0]["filled"] is False
    assert by_latency[500.0]["reason"] == "ask_above_frozen_limit"


def test_source_clock_regression_invalidates_the_market():
    events = base_events()
    events[3]["source_ts_ms"] = 0.5
    rows, counters = lag.replay(
        events, mapping(), {MARKET: "Down"}, segment_end_ms=101_000,
    )

    assert not rows
    assert counters["source_time_regression"] == 1


def test_reverse_order_is_a_separate_control_not_a_base_signal():
    events = [
        event("clob_connection", 0, 0),
        snapshot(UP, 1, 1, [(0.49, 5), (0.47, 10)], [(0.51, 10)]),
        snapshot(DOWN, 1, 2, [(0.46, 10)], [(0.48, 10)]),
        change(DOWN, 99_900, 3, "SELL", 0.48, 10),
        change(UP, 100_000, 4, "BUY", 0.49, 0),
        change(UP, 100_000, 5, "SELL", 0.51, 0),
        change(UP, 100_000, 6, "SELL", 0.49, 10),
    ]
    rows, counters = lag.replay(events, mapping(), {MARKET: "Down"}, segment_end_ms=101_000)

    assert not [row for row in rows if row["variant"] == "base"]
    reverse = [row for row in rows if row["variant"] == "reverse_order"]
    assert len(reverse) == 2
    assert counters["reverse_order_signals"] == 1


def test_five_second_control_keeps_the_original_frozen_limit():
    events = base_events()
    events.extend([
        change(DOWN, 104_999, 7, "SELL", 0.48, 0),
        change(DOWN, 104_999, 8, "SELL", 0.47, 10),
    ])
    rows, counters = lag.replay(events, mapping(), {MARKET: "Down"}, segment_end_ms=106_000)

    delayed = [row for row in rows if row["variant"] == "delay_5s"]
    assert len(delayed) == 2
    assert all(row["fixed_limit"] == pytest.approx(0.48) for row in delayed)
    assert all(row["fill_vwap"] == pytest.approx(0.47) for row in delayed)
    assert all(row["parent_signal_id"] for row in delayed)
    assert counters["delay_5s_signals"] == 1


def test_right_censored_attempts_are_explicit_and_json_safe():
    rows, counters = lag.replay(
        base_events(), mapping(), {MARKET: "Down"}, segment_end_ms=100_200,
    )

    assert len(rows) == 4
    assert all(row["reason"] == "right_censored" for row in rows)
    assert all(row["sent"] is False for row in rows)
    assert counters["right_censored_execution"] == 4
    report = lag.summarize(rows, counters)
    assert report["variants"]["base"]["latencies"]["400"]["attempts"] == 0
    assert report["variants"]["base"]["latencies"]["400"]["right_censored"] == 1
    json.dumps(rows, allow_nan=False)


def test_distinct_physical_frames_at_same_float_clock_are_strictly_ordered():
    events = base_events(confirmation_ms=100_000)
    for row in events[:3]:
        row["physical_batch_id"] = f"setup-{row['seq']}"
    for row in events[3:6]:
        row["physical_batch_id"] = "x-frame"
    events[6]["physical_batch_id"] = "y-frame"

    rows, counters = lag.replay(
        events, mapping(), {MARKET: "Down"}, segment_end_ms=101_000,
    )

    assert len([row for row in rows if row["variant"] == "base"]) == 2
    assert counters["same_batch_diagnostics"] == 0


def test_source_regression_requires_a_new_snapshot():
    book = lag.DirectBook()
    assert book.replace(
        [{"price": 0.4, "size": 5}], [{"price": 0.6, "size": 5}], 1, 10,
    )
    assert book.ready
    assert not book.change("BUY", 0.4, 0, 2, 9)
    assert book.snapshot_required and not book.ready
    assert book.change("BUY", 0.39, 5, 3, 11)
    assert book.snapshot_required and not book.ready
    assert book.replace(
        [{"price": 0.39, "size": 5}], [{"price": 0.61, "size": 5}], 4, 12,
    )
    assert book.ready and not book.snapshot_required


def test_crossed_book_requires_a_new_snapshot_before_recovery():
    book = lag.DirectBook()
    book.replace(
        [{"price": 0.4, "size": 5}], [{"price": 0.6, "size": 5}], 1, 10,
    )

    assert book.change("BUY", 0.7, 5, 2, 11)
    assert book.snapshot_required and not book.ready
    assert book.change("BUY", 0.3, 5, 3, 12)
    assert book.snapshot_required and not book.ready


def test_venue_bbo_mismatch_invalidates_ghost_depth():
    events = base_events()
    events[-1]["best_ask"] = 0.50

    rows, counters = lag.replay(
        events, mapping(), {MARKET: "Down"}, segment_end_ms=101_000,
    )

    assert not [row for row in rows if row.get("variant") == "base"]
    assert counters["venue_bbo_mismatch"] == 1


def test_exhausted_side_may_update_again_without_post_selection():
    events = base_events()
    events.insert(-1, change(UP, 100_050, 6, "SELL", 0.60, 1))
    events[-1]["seq"] = 7

    rows, counters = lag.replay(
        events, mapping(), {MARKET: "Down"}, segment_end_ms=101_000,
    )

    assert len([row for row in rows if row["variant"] == "base"]) == 2
    assert counters["base_signals"] == 1


def test_historical_fee_fallback_uses_market_start_time():
    old_mapping = mapping()
    old_mapping[0]["fee_rate"] = ""
    rows, _ = lag.replay(
        base_events(), old_mapping, {MARKET: "Down"}, segment_end_ms=101_000,
    )

    base = [row for row in rows if row["variant"] == "base"]
    assert base and all(row["fee_rate"] == pytest.approx(0.072) for row in base)


def test_subcent_direct_ask_is_rejected_by_conservative_limit_grid():
    events = base_events()
    events[2] = snapshot(DOWN, 1, 2, [(0.46, 10)], [(0.485, 10)])
    events[-1] = change(DOWN, 100_100, 6, "SELL", 0.485, 10)

    rows, counters = lag.replay(
        events, mapping(), {MARKET: "Down"}, segment_end_ms=101_000,
    )

    assert not rows
    assert counters["invalid_limit_precision"] == 1


def test_epoch_scale_receipts_do_not_expand_equal_time_tolerance():
    origin = 1_790_000_000_000.0
    events = base_events()
    for row in events:
        row["recv_ms"] += origin
        row["source_ts_ms"] += origin
    events.append(change(UP, origin + 100_200, 7, "SELL", 0.60, 1))
    market = mapping()
    market[0]["slot"] = int(origin / 1_000)

    rows, _ = lag.replay(
        events, market, {MARKET: "Down"}, segment_end_ms=origin + 101_000,
    )

    base = [row for row in rows if row["variant"] == "base"]
    assert len(base) == 2
    assert all(row["reason"] == "filled" for row in base)


def test_physical_reader_preserves_frames_and_rejects_sequence_gaps(tmp_path):
    path = tmp_path / "clob.jsonl.gz"
    physical = [
        {"kind": "clob_connection", "recv_ms": 1.0, "source_ts_ms": None,
         "connection_epoch": 1, "seq": 0},
        {"kind": "clob_price_change_batch", "recv_ms": 2.0,
         "source_ts_ms": 2.0, "connection_epoch": 1, "seq": 1,
         "market_id": MARKET, "changes": [
             {"asset_id": UP, "price": 0.4, "size": 5, "side": "BUY"},
             {"asset_id": DOWN, "price": 0.6, "size": 5, "side": "SELL"},
         ]},
        {"kind": "clob_error", "recv_ms": 3.0, "source_ts_ms": None,
         "connection_epoch": 1, "seq": 3, "error": "closed"},
    ]
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        for row in physical:
            stream.write(json.dumps(row) + "\n")

    logical = list(lag.iter_physical_clob_events(path))
    assert [row["seq"] for row in logical] == [0, 1, 2, 3]
    assert logical[1]["physical_batch_id"] == logical[2]["physical_batch_id"]
    assert logical[0]["physical_batch_id"] != logical[1]["physical_batch_id"]

    physical[-1]["seq"] = 4
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        for row in physical:
            stream.write(json.dumps(row) + "\n")
    with pytest.raises(ValueError, match="non-contiguous strict sequence"):
        list(lag.iter_physical_clob_events(path))


def test_physical_reader_rejects_unmarked_logical_price_change(tmp_path):
    path = tmp_path / "logical.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        stream.write(json.dumps({
            "kind": "clob_price_change", "recv_ms": 2.0, "source_ts_ms": 2.0,
            "connection_epoch": 1, "seq": 0, "market_id": MARKET,
            "asset_id": UP, "price": 0.4, "size": 5, "side": "BUY",
        }) + "\n")

    with pytest.raises(ValueError, match="missing physical_batch_id"):
        list(lag.iter_physical_clob_events(path))


@pytest.mark.parametrize(
    "protected",
    [
        "kind",
        "recv_ms",
        "source_ts_ms",
        "connection_epoch",
        "seq",
        "connection_id",
        "market_id",
        "physical_batch_id",
    ],
)
def test_physical_reader_rejects_nested_batch_identity_overrides(
    tmp_path, protected,
):
    path = tmp_path / "malicious-batch.jsonl.gz"
    change_row = {
        "asset_id": UP,
        "price": 0.4,
        "size": 5,
        "side": "BUY",
        protected: "forged",
    }
    physical = {
        "kind": "clob_price_change_batch",
        "recv_ms": 2.0,
        "source_ts_ms": 2.0,
        "connection_epoch": 1,
        "seq": 0,
        "connection_id": 7,
        "market_id": MARKET,
        "physical_batch_id": "real-frame",
        "changes": [change_row],
    }
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        stream.write(json.dumps(physical) + "\n")

    with pytest.raises(ValueError, match="protected batch field"):
        list(lag.iter_physical_clob_events(path))


def test_below_one_dollar_decision_is_rejected_even_with_positive_edge():
    events = base_events()
    events[2] = snapshot(DOWN, 1, 2, [(0.16, 10)], [(0.18, 10)])
    events[-1] = change(DOWN, 100_100, 6, "SELL", 0.18, 10)
    rows, counters = lag.replay(events, mapping(), {MARKET: "Down"}, segment_end_ms=101_000)

    assert not rows
    assert counters["below_venue_minimum"] == 1


def test_summary_keeps_controls_and_null_ev_separate():
    rows, counters = lag.replay(
        base_events(), mapping(), {MARKET: "Down"}, segment_end_ms=101_000,
    )

    report = lag.summarize(rows, counters)

    assert report["family"] == 34
    assert report["variants"]["base"]["latencies"]["400"]["full_fills"] == 1
    assert report["variants"]["base"]["latencies"]["400"]["net_ev_per_share"] > 0
    assert report["variants"]["reverse_order"]["latencies"]["400"]["net_ev_per_share"] is None
    assert report["control_matching"]["outcome_blind"] is True
    assert report["promotion_eligible"] is False


def test_unresolved_fill_suppresses_claimed_ev():
    rows, counters = lag.replay(
        base_events(), mapping(), {}, segment_end_ms=101_000,
    )

    report = lag.summarize(rows, counters)
    metrics = report["variants"]["base"]["latencies"]["400"]

    assert metrics["unresolved_fills"] == 1
    assert metrics["net_ev_per_share"] is None
    assert metrics["pnl"] is None


def test_archive_adapter_writes_atomic_rows_for_the_workflow(tmp_path, monkeypatch):
    monkeypatch.setattr(
        lag.archive,
        "validate_standard_artifact",
        lambda _root: {"manifest": {
            "run_id": "run-1",
            "collector_region": "eu-west-1",
            "started_ms": 0.0,
            "ended_ms": 101_000.0,
            "receipt_race_ready": True,
        }},
    )
    monkeypatch.setattr(lag.archive, "iter_market_mappings", lambda _path: iter(mapping()))
    monkeypatch.setattr(
        lag.archive,
        "iter_outcomes",
        lambda _path: iter([{"market_id": MARKET, "winner": "Down"}]),
    )
    monkeypatch.setattr(lag, "iter_physical_clob_events", lambda _path: iter(base_events()))
    rows_path = tmp_path / "rows.jsonl"

    result = lag.replay_archive(tmp_path, rows_out=rows_path)

    assert result["record_count"] == 4
    assert result["dataset"]["run_id"] == "run-1"
    assert rows_path.read_text().count("\n") == 4
    assert not rows_path.with_name("rows.jsonl.tmp").exists()
