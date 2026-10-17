from __future__ import annotations

import gzip
import json
from decimal import ROUND_HALF_UP, Decimal

import common_mode_run as run
import pytest

BASE_MS = 1_000_000.0


def _write_raw(path, rows: list[tuple[int, dict[str, object]]]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        for receive_ns, payload in rows:
            stream.write(
                f"{receive_ns}\t{json.dumps(payload, separators=(',', ':'))}\n"
            )


def _lifecycle(kind: str, epoch: int, source: str = "deribit") -> dict[str, object]:
    return {
        "_recorder": {
            "schema": "recorder-lifecycle-v1",
            "kind": kind,
            "epoch": epoch,
            "source": source,
        }
    }


def _raw_index(price: float, timestamp: int) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "method": "subscription",
        "params": {
            "channel": "deribit_price_index.btc_usd",
            "data": {"price": price, "timestamp": timestamp},
        },
    }


def test_raw_deribit_index_frames_are_lifecycle_scoped_and_normalized(tmp_path) -> None:
    path = tmp_path / "deribit.20261011T00.txt.gz"
    epoch = 1_791_676_800_000_000_100
    _write_raw(path, [
        (epoch - 1, _raw_index(99.0, 1_791_676_799_999)),
        (epoch, _lifecycle("connection", epoch)),
        (epoch + 1, {"params": {"channel": "quote.BTC-PERPETUAL", "data": {}}}),
        (epoch + 2_000_000, _raw_index(100.25, 1_791_676_800_002)),
        (epoch + 3_000_000, _lifecycle("disconnect", epoch)),
        (epoch + 4_000_000, _raw_index(101.0, 1_791_676_800_004)),
    ])

    events = list(run.iter_deribit_index_events([path]))

    assert events == [{
        "kind": "deribit_index",
        "recv_ms": (epoch + 2_000_000) / 1_000_000.0,
        "seq": 0,
        "source_ts_ms": 1_791_676_800_002.0,
        "price": 100.25,
        "symbol": "BTC",
        "connection_epoch": epoch,
    }]


def test_raw_spot_bbo_frames_are_lifecycle_scoped_and_normalized(tmp_path) -> None:
    path = tmp_path / "bn_spot.20261011T00.txt.gz"
    epoch = 1_791_676_800_000_000_100
    before = {
        "stream": "btcusdt@bookTicker",
        "data": {"s": "BTCUSDT", "T": 1_791_676_799_999, "b": "99", "a": "101"},
    }
    valid = {
        "stream": "btcusdt@bookTicker",
        "data": {"s": "BTCUSDT", "T": 1_791_676_800_002, "b": "100", "a": "100.5"},
    }
    trade = {
        "stream": "btcusdt@trade",
        "data": {"e": "trade", "s": "BTCUSDT", "p": "100.2", "q": "1"},
    }
    _write_raw(path, [
        (epoch - 1, before),
        (epoch, _lifecycle("connection", epoch, "bn_spot")),
        (epoch + 1_000_000, trade),
        (epoch + 2_000_000, valid),
        (epoch + 3_000_000, _lifecycle("disconnect", epoch, "bn_spot")),
        (epoch + 4_000_000, valid),
    ])

    events = list(run.iter_binance_spot_bbo_events([path]))

    assert events == [
        {
            "kind": "spot_bbo_connection",
            "recv_ms": epoch / 1_000_000.0,
            "seq": 0,
            "source_ts_ms": None,
            "connection_epoch": epoch,
        },
        {
            "kind": "spot_bbo",
            "recv_ms": (epoch + 2_000_000) / 1_000_000.0,
            "seq": 1,
            "source_ts_ms": 1_791_676_800_002.0,
            "bid": 100.0,
            "ask": 100.5,
            "symbol": "BTC",
            "connection_epoch": epoch,
        },
        {
            "kind": "spot_bbo_disconnect",
            "recv_ms": (epoch + 3_000_000) / 1_000_000.0,
            "seq": 2,
            "source_ts_ms": None,
            "connection_epoch": epoch,
        },
    ]


def _bbo(kind: str, recv_ms: float, mid: float) -> dict[str, object]:
    return {
        "kind": kind,
        "recv_ms": recv_ms,
        "source_ts_ms": recv_ms,
        "bid": mid - 0.005,
        "ask": mid + 0.005,
        "symbol": "BTC",
    }


def _source_events(*tail: dict[str, object]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = [
        {"kind": "spot_bbo_connection", "recv_ms": BASE_MS - 100.0},
        {"kind": "futures_connection", "recv_ms": BASE_MS - 99.0},
        {"kind": "deribit_connection", "recv_ms": BASE_MS - 98.0},
        _bbo("deribit_quote", BASE_MS, 100.0),
        _bbo("spot_bbo", BASE_MS + 20.0, 100.0),
        _bbo("futures_bbo", BASE_MS + 21.0, 100.0),
        _bbo("spot_bbo", BASE_MS + 200.0, 100.02),
        _bbo("futures_bbo", BASE_MS + 210.0, 100.02),
        _bbo("deribit_quote", BASE_MS + 225.0, 100.0),
        {
            "kind": "spot_trade",
            "recv_ms": BASE_MS + 800.0,
            "source_ts_ms": BASE_MS + 800.0,
            "price": 100.02,
            "size": 1.0,
            "symbol": "BTC",
        },
        *tail,
    ]
    rows.sort(key=lambda row: float(row["recv_ms"]))
    for sequence, row in enumerate(rows):
        row["seq"] = sequence
    return rows


def _index_events(*extra: dict[str, object]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = [
        {
            "kind": "deribit_index",
            "recv_ms": BASE_MS + 1.0,
            "source_ts_ms": BASE_MS + 1.0,
            "price": 100.0,
            "symbol": "BTC",
        },
        {
            "kind": "deribit_index",
            "recv_ms": BASE_MS + 230.0,
            "source_ts_ms": BASE_MS + 230.0,
            "price": 100.0,
            "symbol": "BTC",
        },
        *extra,
    ]
    rows.sort(key=lambda row: float(row["recv_ms"]))
    for sequence, row in enumerate(rows):
        row["seq"] = sequence
    return rows


def _snapshot(
    recv_ms: float,
    token: str,
    bids: tuple[tuple[float, float], ...],
    asks: tuple[tuple[float, float], ...],
) -> dict[str, object]:
    return {
        "kind": "clob_snapshot",
        "recv_ms": recv_ms,
        "source_ts_ms": recv_ms,
        "market_id": "m",
        "asset_id": token,
        "bids": [{"price": price, "size": size} for price, size in bids],
        "asks": [{"price": price, "size": size} for price, size in asks],
    }


def _book_pair(
    recv_ms: float,
    *,
    up: tuple[float, float] = (0.49, 0.51),
    down: tuple[float, float] = (0.49, 0.51),
) -> list[dict[str, object]]:
    return [
        _snapshot(recv_ms, "up", ((up[0], 10.0),), ((up[1], 10.0),)),
        _snapshot(recv_ms, "down", ((down[0], 10.0),), ((down[1], 10.0),)),
    ]


def _clob_events(*extra: dict[str, object]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = [{
        "kind": "clob_connection",
        "recv_ms": BASE_MS - 97.0,
        "source_ts_ms": None,
    }]
    rows += _book_pair(BASE_MS + 10.0)
    rows += _book_pair(BASE_MS + 220.0, up=(0.52, 0.54), down=(0.43, 0.45))
    rows += list(extra)
    rows.sort(key=lambda row: float(row["recv_ms"]))
    for sequence, row in enumerate(rows):
        row["seq"] = sequence
    return rows


def _mapping() -> list[dict[str, object]]:
    return [{
        "market_id": "m",
        "slot": 900,
        "up_token_id": "up",
        "down_token_id": "down",
        "fee_rate": 0.07,
    }]


def _replay(
    *,
    source: list[dict[str, object]] | None = None,
    index: list[dict[str, object]] | None = None,
    clob: list[dict[str, object]] | None = None,
    outcomes: dict[str, str] | None = None,
    include_controls: bool = False,
):
    return run.replay_normalized(
        _source_events() if source is None else source,
        _index_events() if index is None else index,
        _clob_events() if clob is None else clob,
        _mapping(),
        {"m": "Down"} if outcomes is None else outcomes,
        include_controls=include_controls,
    )


def test_cross_stream_equal_receipt_is_censored_instead_of_ranked_optimistically() -> None:
    expensive = {
        "kind": "clob_price_change",
        "recv_ms": BASE_MS + 230.0,
        "source_ts_ms": BASE_MS + 230.0,
        "market_id": "m",
        "asset_id": "down",
        "side": "SELL",
        "price": 0.45,
        "size": 0.0,
    }
    replacement = {
        **expensive,
        "price": 0.60,
        "size": 10.0,
    }

    rows, counters = _replay(clob=_clob_events(expensive, replacement))

    assert rows == []
    assert counters["reset_ambiguous_receipt_tie"] == 1
    assert counters["ambiguous_receipt_tie_events"] == 2


def test_equal_receipt_across_source_transports_is_censored() -> None:
    source = _source_events()
    next(
        row for row in source
        if row["kind"] == "futures_bbo" and row["recv_ms"] == BASE_MS + 210.0
    )["recv_ms"] = BASE_MS + 200.0
    source.sort(key=lambda row: float(row["recv_ms"]))
    for sequence, row in enumerate(source):
        row["seq"] = sequence

    rows, counters = _replay(source=source)

    assert rows == []
    assert counters["reset_ambiguous_receipt_tie"] == 1


def test_disconnect_invalidates_both_pending_latency_branches() -> None:
    source = _source_events({
        "kind": "deribit_disconnect",
        "recv_ms": BASE_MS + 300.0,
        "source_ts_ms": None,
    })

    rows, counters = _replay(source=source)

    assert len(rows) == 2
    assert {row["reason"] for row in rows} == {"disconnect"}
    assert not any(row["filled"] for row in rows)
    assert not any(row["winner"] is not None for row in rows)
    assert counters["reset_deribit_disconnect"] == 1


def test_spot_bbo_sidecar_disconnect_invalidates_pending_branches() -> None:
    source = _source_events({
        "kind": "spot_bbo_disconnect",
        "recv_ms": BASE_MS + 300.0,
        "source_ts_ms": None,
    })

    rows, counters = _replay(source=source)

    assert len(rows) == 2
    assert {row["reason"] for row in rows} == {"disconnect"}
    assert counters["reset_spot_bbo_disconnect"] == 1


def test_receipt_regression_rejects_frame_and_keeps_prior_high_watermark() -> None:
    source = _source_events()
    tail_index = next(
        index for index, row in enumerate(source)
        if row["kind"] == "spot_trade"
    )
    source[tail_index:tail_index] = [
        _bbo("spot_bbo", BASE_MS + 300.0, 100.02),
        _bbo("futures_bbo", BASE_MS + 250.0, 100.02),
        _bbo("spot_bbo", BASE_MS + 275.0, 100.02),
    ]
    for sequence, row in enumerate(source):
        row["seq"] = sequence

    rows, counters = _replay(source=source)

    assert len(rows) == 2
    assert {row["reason"] for row in rows} == {"disconnect"}
    assert counters["reset_receipt_regression"] == 2
    assert counters["receipt_regressed_events"] == 2


def _ask_change(recv_ms: float, price: float, size: float) -> dict[str, object]:
    return {
        "kind": "clob_price_change",
        "recv_ms": recv_ms,
        "source_ts_ms": recv_ms,
        "market_id": "m",
        "asset_id": "down",
        "side": "SELL",
        "price": price,
        "size": size,
    }


def test_book_after_400ms_cannot_rescue_the_400ms_evaluation() -> None:
    clob = _clob_events(
        _ask_change(BASE_MS + 600.0, 0.45, 0.0),
        _ask_change(BASE_MS + 600.0, 0.60, 10.0),
        _ask_change(BASE_MS + 631.0, 0.60, 0.0),
        _ask_change(BASE_MS + 631.0, 0.44, 5.0),
    )

    rows, _ = _replay(clob=clob)
    by_latency = {int(row["evaluation_ms"]): row for row in rows}

    assert by_latency[400]["filled"] is False
    assert by_latency[400]["reason"] == "ask_above_frozen_limit"
    assert by_latency[400]["book_recv_ms"] == BASE_MS + 600.0
    assert by_latency[500]["filled"] is True
    assert by_latency[500]["fill_vwap"] == pytest.approx(0.44)
    assert by_latency[500]["book_recv_ms"] == BASE_MS + 631.0


def test_book_update_at_exact_evaluation_time_is_not_used_for_that_evaluation() -> None:
    clob = _clob_events(
        _ask_change(BASE_MS + 600.0, 0.45, 0.0),
        _ask_change(BASE_MS + 600.0, 0.60, 10.0),
        _ask_change(BASE_MS + 630.0, 0.60, 0.0),
        _ask_change(BASE_MS + 630.0, 0.44, 5.0),
    )

    rows, _ = _replay(clob=clob)
    by_latency = {int(row["evaluation_ms"]): row for row in rows}

    assert by_latency[400]["filled"] is False
    assert by_latency[400]["reason"] == "ask_above_frozen_limit"
    assert by_latency[400]["book_recv_ms"] == BASE_MS + 600.0
    assert by_latency[500]["filled"] is True
    assert by_latency[500]["fill_vwap"] == pytest.approx(0.44)


def test_later_received_removal_is_applied_even_if_exchange_clock_regresses() -> None:
    removal = _ask_change(BASE_MS + 600.0, 0.45, 0.0)
    replacement = _ask_change(BASE_MS + 600.0, 0.60, 10.0)
    removal["source_ts_ms"] = BASE_MS - 50.0
    replacement["source_ts_ms"] = BASE_MS - 50.0

    rows, _ = _replay(clob=_clob_events(removal, replacement))
    by_latency = {int(row["evaluation_ms"]): row for row in rows}

    assert by_latency[400]["filled"] is False
    assert by_latency[400]["reason"] == "ask_above_frozen_limit"


def test_exact_five_share_fee_and_settlement_pnl_are_auditable() -> None:
    depth = [
        _snapshot(
            BASE_MS + 600.0,
            "up",
            ((0.52, 10.0),),
            ((0.54, 10.0),),
        ),
        _snapshot(
            BASE_MS + 600.0,
            "down",
            ((0.42, 10.0),),
            ((0.43, 2.0), (0.44, 4.0)),
        ),
    ]

    rows, counters = _replay(clob=_clob_events(*depth))
    row = next(item for item in rows if item["evaluation_ms"] == 400.0)
    notional = 2.0 * 0.43 + 3.0 * 0.44
    raw_fee = (
        Decimal(2) * Decimal("0.07") * Decimal("0.43") * Decimal("0.57")
        + Decimal(3) * Decimal("0.07") * Decimal("0.44") * Decimal("0.56")
    )
    fee = float(raw_fee.quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP))

    assert row["filled"] is True
    assert row["full_fill"] is True
    assert row["filled_shares"] == 5.0
    assert row["fill_levels"] == [
        {"price": 0.43, "shares": 2.0},
        {"price": 0.44, "shares": 3.0},
    ]
    assert row["fee"] == pytest.approx(fee)
    assert row["total_cost"] == pytest.approx(notional + fee)
    assert row["winner"] == "Down"
    assert row["won"] is True
    assert row["pnl"] == pytest.approx(5.0 - notional - fee)
    assert counters["fills_400ms"] == 1


def test_us015_shadow_candidate_exclusion_propagates_from_frozen_detector() -> None:
    source = [
        {"kind": "spot_bbo_connection", "recv_ms": BASE_MS - 100.0},
        {"kind": "futures_connection", "recv_ms": BASE_MS - 99.0},
        {"kind": "deribit_connection", "recv_ms": BASE_MS - 98.0},
        _bbo("spot_bbo", BASE_MS, 100.0),
        _bbo("futures_bbo", BASE_MS, 100.0),
        _bbo("deribit_quote", BASE_MS, 100.0),
        _bbo("futures_bbo", BASE_MS + 100.0, 100.02),
        _bbo("spot_bbo", BASE_MS + 200.0, 100.02),
        _bbo("futures_bbo", BASE_MS + 210.0, 100.04),
        _bbo("deribit_quote", BASE_MS + 220.0, 100.0),
        {
            "kind": "spot_trade",
            "recv_ms": BASE_MS + 800.0,
            "source_ts_ms": BASE_MS + 800.0,
            "price": 100.02,
            "size": 1.0,
            "symbol": "BTC",
        },
    ]
    source.sort(key=lambda row: float(row["recv_ms"]))
    for sequence, row in enumerate(source):
        row["seq"] = sequence
    clob: list[dict[str, object]] = [{
        "kind": "clob_connection",
        "recv_ms": BASE_MS - 97.0,
        "source_ts_ms": None,
    }]
    clob += _book_pair(BASE_MS + 10.0)
    clob += _book_pair(
        BASE_MS + 240.0,
        up=(0.52, 0.54),
        down=(0.43, 0.45),
    )
    for sequence, row in enumerate(clob):
        row["seq"] = sequence

    rows, counters = _replay(source=source, clob=clob)

    assert rows == []
    assert counters.get("signals", 0) == 0


def test_frozen_controls_remain_separate_on_the_same_causal_inputs() -> None:
    base_rows, _ = _replay(include_controls=True)

    confirming_source = _source_events()
    next(
        row for row in confirming_source
        if row["kind"] == "deribit_quote" and row["recv_ms"] == BASE_MS + 225.0
    ).update({"bid": 100.001, "ask": 100.011})
    confirming_index = _index_events()
    next(
        row for row in confirming_index
        if row["recv_ms"] == BASE_MS + 230.0
    )["price"] = 100.006
    confirming_rows, _ = _replay(
        source=confirming_source,
        index=confirming_index,
        include_controls=True,
    )

    no_follow_clob: list[dict[str, object]] = [{
        "kind": "clob_connection",
        "recv_ms": BASE_MS - 97.0,
        "source_ts_ms": None,
    }]
    no_follow_clob += _book_pair(BASE_MS + 10.0)
    no_follow_clob += _book_pair(
        BASE_MS + 220.0,
        up=(0.50, 0.51),
        down=(0.43, 0.45),
    )
    for sequence, row in enumerate(no_follow_clob):
        row["seq"] = sequence
    no_follow_rows, _ = _replay(
        clob=no_follow_clob,
        include_controls=True,
    )

    assert {row["variant"] for row in base_rows} == {
        "base",
        "direction_reversal",
        "time_shift",
    }
    assert all(
        row["parent_signal_id"] is not None
        and row["comparison_design"] == "matched_base_arm"
        for row in base_rows
        if row["variant"] in {"direction_reversal", "time_shift"}
    )
    assert all(
        row["parent_signal_id"] is None and row["comparison_design"] == "base"
        for row in base_rows
        if row["variant"] == "base"
    )
    assert {row["variant"] for row in confirming_rows} == {
        "benchmark_confirmation",
    }
    assert {row["variant"] for row in no_follow_rows} == {"no_pm_follow"}
    assert all(
        row["parent_signal_id"] is None
        and row["comparison_design"] == "disjoint_negative_control_cohort"
        for row in (*confirming_rows, *no_follow_rows)
    )


def test_no_follow_control_cannot_reenter_after_overshooting_its_band() -> None:
    clob: list[dict[str, object]] = [{
        "kind": "clob_connection",
        "recv_ms": BASE_MS - 97.0,
        "source_ts_ms": None,
    }]
    clob += _book_pair(BASE_MS + 10.0)
    clob += _book_pair(
        BASE_MS + 220.0,
        up=(0.52, 0.54),
        down=(0.43, 0.45),
    )
    clob += _book_pair(
        BASE_MS + 240.0,
        up=(0.50, 0.51),
        down=(0.43, 0.45),
    )
    for sequence, row in enumerate(clob):
        row["seq"] = sequence

    rows, _ = _replay(clob=clob, include_controls=True)

    assert not any(row["variant"] == "no_pm_follow" for row in rows)
