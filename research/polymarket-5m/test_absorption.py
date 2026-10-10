from __future__ import annotations

import gzip
import json
from pathlib import Path

import absorption
import pytest

SLOT = 1_800_000_000
BASE_MS = (SLOT + 60) * 1_000.0
MARKET = "market-1"
UP = "up-token"
DOWN = "down-token"


def _market_event() -> dict:
    return {
        "kind": "market",
        "recv_ms": BASE_MS - 10,
        "market_id": MARKET,
        "slot": SLOT,
        "up_token_id": UP,
        "down_token_id": DOWN,
    }


def _snapshot(token: str, recv_ms: float, bids, asks, **extra) -> dict:
    return {
        "kind": "snapshot",
        "recv_ms": recv_ms,
        "source_ts_ms": recv_ms - 2,
        "market_id": MARKET,
        "asset_id": token,
        "bids": [{"price": str(price), "size": str(size)} for price, size in bids],
        "asks": [{"price": str(price), "size": str(size)} for price, size in asks],
        "ambiguous_receipt": False,
        **extra,
    }


def _change(token: str, recv_ms: float, side: str, price: float, size: float, **extra):
    return {
        "kind": "price_change",
        "recv_ms": recv_ms,
        "source_ts_ms": recv_ms - 2,
        "market_id": MARKET,
        "changes": [{
            "asset_id": token,
            "side": side,
            "price": str(price),
            "size": str(size),
        }],
        "ambiguous_receipt": False,
        **extra,
    }


def _trade(recv_ms: float, size: float, price: float = 0.5, **extra):
    return {
        "kind": "trade",
        "recv_ms": recv_ms,
        "source_ts_ms": recv_ms - 2,
        "market_id": MARKET,
        "asset_id": UP,
        "side": "BUY",
        "price": price,
        "size": size,
        "ambiguous_receipt": False,
        **extra,
    }


def _ready_detector() -> absorption.AbsorptionDetector:
    detector = absorption.AbsorptionDetector()
    detector.on_event(_market_event())
    detector.on_event({"kind": "connection", "recv_ms": BASE_MS - 9, "epoch": 1})
    detector.on_event(
        _snapshot(UP, BASE_MS - 8, [(0.49, 20)], [(0.5, 10), (0.51, 10)])
    )
    detector.on_event(
        _snapshot(DOWN, BASE_MS - 7, [(0.47, 20)], [(0.48, 6), (0.49, 10)])
    )
    return detector


def test_first_buy_burst_depletion_and_same_price_refill_freezes_direct_opposite_buy():
    detector = _ready_detector()

    assert detector.on_event(_trade(BASE_MS, 3)) == []
    assert detector.on_event(_trade(BASE_MS + 30, 2)) == []
    assert detector.on_event(_change(UP, BASE_MS + 45, "SELL", 0.5, 4)) == []
    assert detector.on_event(_change(UP, BASE_MS + 120, "SELL", 0.5, 9)) == []
    signals = detector.on_event(_change(DOWN, BASE_MS + 300, "SELL", 0.48, 0))

    assert len(signals) == 1
    signal = signals[0]
    assert signal["variant"] == "base"
    assert signal["swept_side"] == "Up"
    assert signal["buy_side"] == "Down"
    assert signal["burst_shares"] == 5.0
    assert signal["highest_burst_price"] == 0.5
    assert signal["baseline_depth"] == 10.0
    assert signal["trough_depth"] == 4.0
    assert signal["refill_shares"] == 5.0
    assert signal["decision_recv_ms"] == BASE_MS + 280
    assert signal["decision_vwap"] == pytest.approx(0.48)
    assert signal["fixed_limit"] == 0.48
    assert signal["fill_levels"] == [{"price": 0.48, "shares": 5.0}]


def test_depleted_burst_without_refill_emits_only_matched_no_refill_control():
    detector = _ready_detector()
    detector.on_event(_trade(BASE_MS, 5))
    detector.on_event(_change(UP, BASE_MS + 40, "SELL", 0.5, 2))

    assert detector.flush(BASE_MS + 250) == []
    signals = detector.flush(BASE_MS + 250.001)

    assert len(signals) == 1
    assert signals[0]["variant"] == "no_refill"
    assert signals[0]["decision_recv_ms"] == BASE_MS + 250
    assert signals[0]["parent_signal_id"].startswith("absorption:")


def test_quote_depletion_received_before_trade_print_uses_only_prior_book_history():
    detector = _ready_detector()
    detector.on_event(_change(UP, BASE_MS - 5, "SELL", 0.5, 4))
    detector.on_event(_trade(BASE_MS, 5))

    assert detector.on_event(_change(UP, BASE_MS + 80, "SELL", 0.5, 9)) == []
    signals = detector.flush(BASE_MS + 250.001)

    assert len(signals) == 1
    assert signals[0]["baseline_depth"] == 10.0
    assert signals[0]["trough_depth"] == 4.0
    assert signals[0]["refill_shares"] == 5.0
    assert signals[0]["decision_recv_ms"] == BASE_MS + 250


def test_first_qualifying_burst_is_immutable_even_if_later_burst_would_refill():
    detector = _ready_detector()
    detector.on_event(_trade(BASE_MS, 5))
    detector.flush(BASE_MS + 250.001)

    detector.on_event(_trade(BASE_MS + 1_000, 5))
    detector.on_event(_change(UP, BASE_MS + 1_020, "SELL", 0.5, 2))
    signals = detector.on_event(_change(UP, BASE_MS + 1_100, "SELL", 0.5, 10))

    assert signals == []
    assert detector.locked_markets == {MARKET}


def test_disconnect_ambiguous_receipt_and_stale_books_fail_closed():
    disconnected = _ready_detector()
    disconnected.on_event({"kind": "disconnect", "recv_ms": BASE_MS - 1, "epoch": 1})
    assert disconnected.on_event(_trade(BASE_MS, 5)) == []

    ambiguous = _ready_detector()
    assert ambiguous.on_event(_trade(BASE_MS, 5, ambiguous_receipt=True)) == []
    assert MARKET in ambiguous.tainted_markets

    stale = _ready_detector()
    assert stale.on_event(_trade(BASE_MS + 2_100, 5)) == []
    assert stale.outcomes[-1]["reason"] == "stale_or_invalid_book"


def test_adapter_preserves_trade_receipt_order_and_rejects_equal_receipts(tmp_path: Path):
    path = tmp_path / "poly_clob.20261010T00.jsonl.gz"
    rows = [
        {"rn": 1_000_000_000, "c": -1, "k": "meta", "m": {
            "meta": f"btc-updown-5m-{SLOT}", "cid": MARKET,
            "tokens": json.dumps([UP, DOWN]), "outcomes": json.dumps(["Up", "Down"]),
        }},
        {"rn": 1_000_000_001, "c": 0, "e": 6, "s": 40, "k": "message", "m": {
            "event_type": "last_trade_price", "asset_id": UP, "market": MARKET,
            "timestamp": "998", "price": "0.49", "size": "99", "side": "BUY",
        }},
        {"rn": 1_000_000_002, "c": 0, "e": 7, "s": 0, "k": "connection", "assets": [UP, DOWN]},
        {"rn": 1_000_000_003, "c": 0, "e": 6, "s": 41, "k": "message", "m": {
            "event_type": "last_trade_price", "asset_id": UP, "market": MARKET,
            "timestamp": "998", "price": "0.49", "size": "99", "side": "BUY",
        }},
        {"rn": 1_000_000_004, "c": 0, "e": 7, "s": 1, "k": "message", "m": {
            "event_type": "book", "asset_id": UP, "market": MARKET, "timestamp": "999",
            "bids": [{"price": "0.49", "size": "5"}],
            "asks": [{"price": "0.50", "size": "8"}],
        }},
        {"rn": 1_000_000_005, "c": 0, "e": 7, "s": 2, "k": "message", "m": {
            "event_type": "last_trade_price", "asset_id": UP, "market": MARKET,
            "timestamp": "1000", "price": "0.50", "size": "5.76", "side": "BUY",
        }},
        {"rn": 1_000_000_006, "c": 0, "e": 7, "s": 3, "k": "disconnect"},
    ]
    with gzip.open(path, "wt") as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")

    events = list(absorption.iter_raw_clob_events([path], segment_end_ms=1_001.0))

    assert [event["kind"] for event in events] == [
        "market", "connection", "snapshot", "trade", "disconnect",
    ]
    assert events[3]["recv_ms"] == pytest.approx(1_000.000005)
    assert events[3]["source_ts_ms"] == 1_000.0
    assert events[3]["size"] == 5.76
    assert events[3]["side"] == "BUY"

    rows[5]["rn"] = rows[4]["rn"]
    with gzip.open(path, "wt") as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")
    with pytest.raises(ValueError, match="equal or regressed"):
        list(absorption.iter_raw_clob_events([path], segment_end_ms=1_001.0))


def test_same_frame_dual_snapshot_is_atomic_but_trade_quote_mix_is_ambiguous():
    markets = {MARKET: {"market_id": MARKET}}
    tokens = {UP: MARKET, DOWN: MARKET}
    up_snapshot = {
        "event_type": "book", "asset_id": UP, "timestamp": "1000",
        "bids": [{"price": "0.49", "size": "5"}],
        "asks": [{"price": "0.50", "size": "5"}],
    }
    down_snapshot = {
        "event_type": "book", "asset_id": DOWN, "timestamp": "1000",
        "bids": [{"price": "0.49", "size": "5"}],
        "asks": [{"price": "0.50", "size": "5"}],
    }
    atomic = absorption._normalized_messages(
        [up_snapshot, down_snapshot],
        receive_ms=1_001,
        epoch=1,
        markets=markets,
        tokens=tokens,
    )
    assert not any(event["ambiguous_receipt"] for event in atomic)

    trade = {
        "event_type": "last_trade_price", "asset_id": UP, "timestamp": "1001",
        "price": "0.50", "size": "5", "side": "BUY",
    }
    mixed = absorption._normalized_messages(
        [up_snapshot, trade],
        receive_ms=1_002,
        epoch=1,
        markets=markets,
        tokens=tokens,
    )
    assert all(event["ambiguous_receipt"] for event in mixed)
