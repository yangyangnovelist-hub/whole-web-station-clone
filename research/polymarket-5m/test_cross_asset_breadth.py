from __future__ import annotations

import gzip
import json
from decimal import ROUND_DOWN, Decimal

import cross_asset_breadth as breadth
import pytest


def _trade(asset: str, recv_ms: float, price: float) -> dict[str, object]:
    return {"asset": asset, "recv_ms": recv_ms, "price": price}


def _signal_events(*, btc_jump: bool = False) -> list[dict[str, object]]:
    assets = ("btc", "eth", "sol", "xrp", "doge")
    prices = (100.0, 100.1, 100.0, 100.1)
    rows: list[dict[str, object]] = []
    for offset, price in enumerate(prices):
        recv_ms = 60_100.0 + offset * 1_000.0
        rows.extend(_trade(asset, recv_ms, price) for asset in assets)
    decision_ms = 64_100.0
    rows.extend([
        _trade("btc", decision_ms, 101.0 if btc_jump else 100.1),
        _trade("eth", decision_ms, 101.0),
        _trade("sol", decision_ms, 101.0),
        _trade("xrp", decision_ms, 100.1),
        _trade("doge", decision_ms, 100.1),
        _trade("xrp", decision_ms + 1.0, 101.0),
    ])
    return rows


def _development_config() -> breadth.BreadthConfig:
    return breadth.BreadthConfig(
        sigma_window_s=4,
        sigma_min_observations=2,
        latest_max_age_ms=250.0,
        reference_max_age_ms=250.0,
    )


def _strict_metadata(asset: str, event_count: int, start: float, end: float) -> dict[str, object]:
    return {
        "kind": "metadata",
        "schema": breadth.PRICE_SIDECAR_SCHEMA,
        "asset": asset,
        "run_id": "run-1",
        "collector_region": "test-region",
        "recorder_complete": True,
        "raw_event_count": event_count,
        "emitted_event_count": event_count,
        "raw_started_ms": start,
        "raw_ended_ms": end,
    }


def test_consensus_is_receipt_causal_atomic_and_first_signal_only() -> None:
    signals = breadth.detect_signals(_signal_events(), _development_config())

    assert len(signals) == 1
    signal = signals[0]
    assert signal.decision_recv_ms == 64_100.0
    assert signal.slot == 0
    assert signal.direction == 1
    assert signal.contributors == ("eth", "sol")
    assert abs(signal.z_scores["btc"]) <= 0.5
    assert abs(signal.z_scores["xrp"]) < 2.0


def test_btc_move_blocks_cross_asset_signal() -> None:
    assert breadth.detect_signals(
        _signal_events(btc_jump=True), _development_config(),
    ) == []


def test_strict_price_sidecar_parses_lifecycle_trade_and_bbo(tmp_path) -> None:
    path = tmp_path / "prices.jsonl.gz"
    rows = [
        {"kind": "connection", "recv_ms": 100.0, "connection_epoch": 1,
         "sequence": 0, "raw_sequence": 0},
        {"kind": "trade", "recv_ms": 101.0, "source_ts_ms": 90.0,
         "connection_epoch": 1, "sequence": 1, "raw_sequence": 1, "price": 99.0},
        {"kind": "bookTicker", "recv_ms": 102.0, "source_ts_ms": None,
         "connection_epoch": 1, "sequence": 2, "raw_sequence": 2,
         "bid": 100.0, "ask": 102.0,
         "mid": 101.0},
        {"kind": "disconnect", "recv_ms": 103.0, "connection_epoch": 1,
         "sequence": 3, "raw_sequence": 3},
        _strict_metadata("eth", 4, 100.0, 103.0),
    ]
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        stream.writelines(json.dumps(row) + "\n" for row in rows)

    parsed = list(breadth.iter_trade_file("eth", path))

    assert [row["kind"] for row in parsed] == [
        "connection", "price", "price", "disconnect",
    ]
    assert [row.get("price") for row in parsed] == [None, 99.0, 101.0, None]
    assert all(row["lifecycle_proven"] for row in parsed)


@pytest.mark.parametrize("fault", ["sequence", "epoch", "open_epoch"])
def test_strict_price_sidecar_rejects_broken_lifecycle(tmp_path, fault: str) -> None:
    path = tmp_path / "prices.jsonl.gz"
    rows = [
        {"kind": "connection", "asset": "eth", "recv_ms": 100.0,
         "connection_epoch": 1, "sequence": 0, "raw_sequence": 0},
        {"kind": "trade", "asset": "eth", "recv_ms": 101.0, "source_ts_ms": 90.0,
         "connection_epoch": 1, "sequence": 1, "raw_sequence": 1, "price": 99.0},
        {"kind": "disconnect", "asset": "eth", "recv_ms": 102.0,
         "connection_epoch": 1, "sequence": 2, "raw_sequence": 2},
        _strict_metadata("eth", 3, 100.0, 102.0),
    ]
    if fault == "sequence":
        rows[1]["sequence"] = 2
    elif fault == "epoch":
        rows[1]["connection_epoch"] = 2
    else:
        rows.pop(2)
        rows[-1] = _strict_metadata("eth", 2, 100.0, 101.0)
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        stream.writelines(json.dumps(row) + "\n" for row in rows)

    with pytest.raises(ValueError):
        list(breadth.iter_trade_file("eth", path))


def test_strict_source_bundle_must_match_archive_identity_and_interval() -> None:
    audits = {
        asset: {
            "mode": "strict",
            "metadata": _strict_metadata(asset, 4, 100.0, 200.0),
        }
        for asset in breadth.ASSETS
    }
    manifest = {
        "run_id": "run-1",
        "collector_region": "test-region",
        "started_ms": 120.0,
        "ended_ms": 180.0,
    }

    assert breadth._source_bundle_proven(audits, manifest) is True
    audits["doge"]["metadata"]["run_id"] = "another-run"
    with pytest.raises(ValueError, match="one recorder run"):
        breadth._source_bundle_proven(audits, manifest)


def test_strict_source_bundle_must_cover_the_clob_interval() -> None:
    audits = {
        asset: {
            "mode": "strict",
            "metadata": _strict_metadata(asset, 4, 100.0, 200.0),
        }
        for asset in breadth.ASSETS
    }
    manifest = {
        "run_id": "run-1",
        "collector_region": "test-region",
        "started_ms": 199.0,
        "ended_ms": 10_000.0,
    }

    with pytest.raises(ValueError, match="cover the CLOB"):
        breadth._source_bundle_proven(audits, manifest)


def test_source_disconnect_resets_causal_sigma() -> None:
    rows = _signal_events()
    rows.extend([
        {"asset": "sol", "kind": "connection", "recv_ms": 64_098.0,
         "connection_epoch": 1},
        {"asset": "sol", "kind": "disconnect", "recv_ms": 64_099.0,
         "connection_epoch": 1},
        {"asset": "sol", "kind": "connection", "recv_ms": 64_099.5,
         "connection_epoch": 2},
    ])
    rows.sort(key=lambda row: float(row["recv_ms"]))

    assert breadth.detect_signals(rows, _development_config()) == []


def _mapping() -> list[dict[str, object]]:
    return [{
        "market_id": "m",
        "slot": 0,
        "up_token_id": "up",
        "down_token_id": "down",
        "fee_rate": 0.07,
    }]


def _snapshot(
    recv_ms: float,
    asset_id: str,
    bid: float,
    ask: float,
    ask_size: float = 10.0,
    *,
    seq: int,
) -> dict[str, object]:
    return {
        "kind": "clob_snapshot",
        "recv_ms": recv_ms,
        "source_ts_ms": recv_ms - 10.0,
        "connection_epoch": 1,
        "seq": seq,
        "market_id": "m",
        "asset_id": asset_id,
        "bids": [{"price": bid, "size": 10.0}],
        "asks": [{"price": ask, "size": ask_size}],
    }


def _candidate(decision_ms: float = 1_000.0) -> breadth.BreadthSignal:
    return breadth.BreadthSignal(
        decision_recv_ms=decision_ms,
        slot=0,
        direction=1,
        contributors=("eth", "sol"),
        z_scores={"btc": 0.1, "eth": 3.0, "sol": 2.5, "xrp": 0.2, "doge": -0.1},
    )


def _clob_events(*extra: dict[str, object]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = [{
        "kind": "clob_connection",
        "recv_ms": 100.0,
        "source_ts_ms": None,
        "connection_epoch": 1,
        "seq": 0,
    }]
    rows.extend([
        _snapshot(900.0, "up", 0.39, 0.40, seq=1),
        _snapshot(900.0, "down", 0.59, 0.60, seq=2),
        *extra,
    ])
    rows.sort(key=lambda row: (float(row["recv_ms"]), int(row["seq"])))
    return rows


def _fee(price: float, shares: float = 5.0) -> float:
    raw = Decimal(str(shares)) * Decimal("0.07") * Decimal(str(price)) * (
        Decimal(1) - Decimal(str(price))
    )
    return float(raw.quantize(Decimal("0.00001"), rounding=ROUND_DOWN))


def test_execution_uses_exact_due_direct_book_frozen_limit_and_fee() -> None:
    events = _clob_events(
        _snapshot(1_399.9, "up", 0.40, 0.41, 5.0, seq=3),
        _snapshot(1_399.9, "down", 0.59, 0.61, 5.0, seq=4),
        _snapshot(1_400.1, "up", 0.38, 0.39, 5.0, seq=5),
        _snapshot(1_499.9, "up", 0.41, 0.42, 5.0, seq=6),
        _snapshot(1_500.1, "down", 0.59, 0.61, 5.0, seq=7),
    )

    rows, _ = breadth.replay_execution(
        events,
        [_candidate()],
        _mapping(),
        {"m": "Up"},
        include_controls=False,
    )
    by_delay = {row["evaluation_ms"]: row for row in rows}

    filled = by_delay[400.0]
    assert filled["fixed_limit"] == pytest.approx(0.41)
    assert filled["fill_vwap"] == pytest.approx(0.41)
    assert filled["book_recv_ms"] == 1_399.9
    assert filled["filled_shares"] == 5.0
    assert filled["fee"] == pytest.approx(_fee(0.41))
    assert filled["pnl"] == pytest.approx(5.0 - 5.0 * 0.41 - _fee(0.41))
    assert by_delay[500.0]["reason"] == "ask_above_frozen_limit"


def test_controls_use_opposite_direct_token_and_refreeze_after_five_seconds() -> None:
    events = _clob_events(
        _snapshot(1_399.9, "up", 0.39, 0.40, 5.0, seq=3),
        _snapshot(1_399.9, "down", 0.59, 0.60, 5.0, seq=4),
        _snapshot(5_999.9, "up", 0.29, 0.30, 5.0, seq=5),
        _snapshot(5_999.9, "down", 0.69, 0.70, 5.0, seq=6),
        _snapshot(6_399.9, "up", 0.30, 0.31, 5.0, seq=7),
        _snapshot(6_399.9, "down", 0.70, 0.71, 5.0, seq=8),
        _snapshot(6_400.1, "up", 0.30, 0.31, 5.0, seq=9),
    )

    rows, _ = breadth.replay_execution(
        events,
        [_candidate()],
        _mapping(),
        {"m": "Up"},
        evaluation_ms=(400.0,),
        include_controls=True,
    )
    by_variant = {row["variant"]: row for row in rows}

    reverse = by_variant["direction_reversal"]
    assert reverse["buy_side"] == "Down"
    assert reverse["buy_asset_id"] == "down"
    assert reverse["fixed_limit"] == pytest.approx(0.61)
    assert reverse["fill_vwap"] == pytest.approx(0.60)
    shifted = by_variant["time_shift"]
    assert shifted["parent_signal_id"] == by_variant["base"]["signal_id"]
    assert shifted["decision_recv_ms"] == 6_000.0
    assert shifted["fixed_limit"] == pytest.approx(0.31)
    assert shifted["fill_vwap"] == pytest.approx(0.31)


def test_disconnect_before_match_fails_closed() -> None:
    rows, _ = breadth.replay_execution(
        _clob_events({
            "kind": "clob_error",
            "recv_ms": 1_200.0,
            "source_ts_ms": None,
            "connection_epoch": 1,
            "seq": 3,
        }),
        [_candidate()],
        _mapping(),
        {"m": "Up"},
        evaluation_ms=(400.0,),
        include_controls=False,
    )

    assert rows[0]["filled"] is False
    assert rows[0]["reason"] == "disconnect"


def test_independent_clob_tie_at_decision_is_not_ordered_optimistically() -> None:
    rows, _ = breadth.replay_execution(
        _clob_events(_snapshot(1_000.0, "up", 0.20, 0.21, seq=3)),
        [_candidate()],
        _mapping(),
        {"m": "Up"},
        evaluation_ms=(400.0,),
        include_controls=False,
    )

    assert rows[0]["sent"] is False
    assert rows[0]["reason"] == "ambiguous_cross_stream_receipt_tie"


def test_independent_clob_tie_at_match_cannot_create_a_fill() -> None:
    rows, _ = breadth.replay_execution(
        _clob_events(_snapshot(1_400.0, "up", 0.20, 0.21, 5.0, seq=3)),
        [_candidate()],
        _mapping(),
        {"m": "Up"},
        evaluation_ms=(400.0,),
        include_controls=False,
    )

    assert rows[0]["filled"] is False
    assert rows[0]["reason"] == "ambiguous_cross_stream_receipt_tie"


def test_partial_fak_execution_keeps_fee_and_settlement_risk() -> None:
    rows, _ = breadth.replay_execution(
        _clob_events(
            _snapshot(1_399.9, "up", 0.39, 0.40, 3.0, seq=3),
            _snapshot(1_400.1, "down", 0.59, 0.60, 5.0, seq=4),
        ),
        [_candidate()],
        _mapping(),
        {"m": "Down"},
        evaluation_ms=(400.0,),
        include_controls=False,
    )
    row = rows[0]

    assert row["full_fill"] is False
    assert row["executed"] is True
    assert row["reason"] == "partial_fill"
    assert row["filled_shares"] == pytest.approx(3.0)
    assert row["fee"] == pytest.approx(_fee(0.40, 3.0))
    assert row["pnl"] == pytest.approx(-3.0 * 0.40 - _fee(0.40, 3.0))
