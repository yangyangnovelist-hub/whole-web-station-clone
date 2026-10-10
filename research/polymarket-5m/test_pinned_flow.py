from __future__ import annotations

import pinned_flow
import pytest

from absorption_run import replay_execution


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


def _snapshot(token: str, recv_ms: float, bids, asks, **extra) -> dict:
    return {
        "kind": "snapshot",
        "recv_ms": recv_ms,
        "source_ts_ms": recv_ms - 2,
        "epoch": 1,
        "market_id": MARKET,
        "asset_id": token,
        "bids": [{"price": str(price), "size": str(size)} for price, size in bids],
        "asks": [{"price": str(price), "size": str(size)} for price, size in asks],
        "ambiguous_receipt": False,
        **extra,
    }


def _change(token: str, recv_ms: float, side: str, price: float, size: float, **extra) -> dict:
    return {
        "kind": "price_change",
        "recv_ms": recv_ms,
        "source_ts_ms": recv_ms - 2,
        "epoch": 1,
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


def _trade(recv_ms: float, size: float, *, side: str = "BUY", token: str = UP,
           price: float = 0.50, **extra) -> dict:
    return {
        "kind": "trade",
        "recv_ms": recv_ms,
        "source_ts_ms": recv_ms - 2,
        "epoch": 1,
        "market_id": MARKET,
        "asset_id": token,
        "side": side,
        "price": price,
        "size": size,
        "ambiguous_receipt": False,
        **extra,
    }


def _ready() -> pinned_flow.PinnedFlowDetector:
    detector = pinned_flow.PinnedFlowDetector()
    detector.on_event(_market())
    detector.on_event({"kind": "connection", "recv_ms": BASE_MS - 9, "epoch": 1})
    detector.on_event(_snapshot(UP, BASE_MS - 8, [(0.49, 20)], [(0.50, 10), (0.51, 10)]))
    detector.on_event(_snapshot(DOWN, BASE_MS - 7, [(0.47, 20)], [(0.48, 3), (0.49, 10)]))
    return detector


def _pinned_signal(detector: pinned_flow.PinnedFlowDetector) -> dict:
    assert detector.on_event(_trade(BASE_MS, 5)) == []
    assert detector.on_event(_trade(BASE_MS + 100, 5)) == []
    assert detector.on_event(_trade(BASE_MS + 200, 5)) == []
    signals = detector.flush(BASE_MS + 500.001)
    assert len(signals) == 1
    return signals[0]


def test_high_buy_flow_with_no_visible_depletion_buys_complement_at_a_frozen_limit():
    signal = _pinned_signal(_ready())

    assert signal["variant"] == "base"
    assert signal["swept_side"] == "Up"
    assert signal["buy_side"] == "Down"
    assert signal["buy_asset_id"] == DOWN
    assert signal["decision_recv_ms"] == BASE_MS + 500
    assert signal["buy_shares"] == 15.0
    assert signal["trade_count"] == 3
    assert signal["buy_fraction"] == pytest.approx(1.0)
    assert signal["baseline_ask_depth"] == 10.0
    assert signal["trough_ask_depth"] == 10.0
    assert signal["mid_impact"] == pytest.approx(0.0)
    assert signal["fixed_limit"] == 0.49
    assert signal["decision_vwap"] == pytest.approx((0.48 * 3 + 0.49 * 2) / 5)
    assert signal["ordering_clock"] == "local_receipt_ms"


def test_visible_half_depletion_is_excluded_so_us018_and_pinned_flow_are_disjoint():
    detector = _ready()
    detector.on_event(_trade(BASE_MS, 5))
    detector.on_event(_trade(BASE_MS + 100, 5))
    detector.on_event(_change(UP, BASE_MS + 150, "SELL", 0.50, 5))
    detector.on_event(_trade(BASE_MS + 200, 5))

    assert detector.flush(BASE_MS + 500.001) == []
    assert detector.outcomes[-1]["reason"] == "visible_depletion"


def test_price_impact_and_two_sided_flow_each_fail_the_pinning_gate():
    moved = _ready()
    moved.on_event(_trade(BASE_MS, 5))
    moved.on_event(_trade(BASE_MS + 100, 5, price=0.51))
    moved.on_event(_trade(BASE_MS + 200, 5, price=0.52))
    assert moved.flush(BASE_MS + 500.001) == []
    assert moved.outcomes[-1]["reason"] == "price_not_pinned"

    mixed = _ready()
    mixed.on_event(_trade(BASE_MS, 5))
    mixed.on_event(_trade(BASE_MS + 100, 5))
    mixed.on_event(_trade(BASE_MS + 150, 10, side="SELL", price=0.49))
    mixed.on_event(_trade(BASE_MS + 200, 5))
    assert mixed.flush(BASE_MS + 500.001) == []
    assert mixed.outcomes[-1]["reason"] == "weak_trade_sign_imbalance"


def test_trade_direction_comes_from_the_pretrade_book_not_the_undocumented_side_field():
    detector = _ready()
    detector.on_event(_trade(BASE_MS, 5, side="SELL", price=0.50))
    detector.on_event(_trade(BASE_MS + 100, 5, side="SELL", price=0.50))
    detector.on_event(_trade(BASE_MS + 200, 5, side="SELL", price=0.50))
    assert len(detector.flush(BASE_MS + 500.001)) == 1

    in_spread = _ready()
    in_spread.on_event(_trade(BASE_MS, 100, side="BUY", price=0.495))
    assert in_spread.flush(BASE_MS + 500.001) == []
    assert in_spread.outcomes == []


def test_first_qualified_window_is_immutable_and_disconnect_or_ambiguity_fail_closed():
    detector = _ready()
    _pinned_signal(detector)
    detector.on_event(_trade(BASE_MS + 1_000, 100))
    assert detector.flush(BASE_MS + 1_600) == []
    assert detector.locked_markets == {MARKET}

    disconnected = _ready()
    disconnected.on_event({"kind": "disconnect", "recv_ms": BASE_MS - 1, "epoch": 1})
    assert disconnected.on_event(_trade(BASE_MS, 100)) == []

    ambiguous = _ready()
    assert ambiguous.on_event(_trade(BASE_MS, 100, ambiguous_receipt=True)) == []
    assert MARKET in ambiguous.tainted_markets


def test_existing_receipt_clock_executor_prices_400_and_500ms_from_direct_depth():
    detector = _ready()
    signal = _pinned_signal(detector)
    events = [
        _market(),
        {"kind": "connection", "recv_ms": BASE_MS - 9, "epoch": 1},
        _snapshot(UP, BASE_MS - 8, [(0.49, 20)], [(0.50, 10)]),
        _snapshot(DOWN, BASE_MS - 7, [(0.47, 20)], [(0.48, 3), (0.49, 10)]),
        _change(DOWN, BASE_MS + 850, "SELL", 0.48, 0),
        _change(DOWN, BASE_MS + 950, "SELL", 0.49, 4),
        _change(DOWN, BASE_MS + 5_950, "BUY", 0.47, 0),
        _change(DOWN, BASE_MS + 5_951, "BUY", 0.46, 20),
    ]
    rows = replay_execution(events, [signal], {MARKET: "Down"})

    assert [row["evaluation_ms"] for row in rows] == [400.0, 500.0]
    assert rows[0]["full_fill"] is True
    assert rows[1]["full_fill"] is False
    assert rows[1]["entry_reason"] == "partial_fill"
    assert rows[1]["entry_shares"] == pytest.approx(4.0)
    assert rows[0]["entry_book_recv_ms"] == BASE_MS + 850
    assert rows[0]["pnl"] is not None


@pytest.mark.parametrize("token", [UP, DOWN])
@pytest.mark.parametrize("fault", ["empty", "crossed", "malformed_change", "malformed_snapshot"])
def test_invalid_book_cannot_be_healed_into_a_continuously_pinned_window(token, fault):
    detector = _ready()
    detector.on_event(_trade(BASE_MS, 5))
    detector.on_event(_trade(BASE_MS + 100, 5))
    detector.on_event(_trade(BASE_MS + 200, 5))
    if fault == "empty":
        event = _snapshot(token, BASE_MS + 250, [(0.47, 20)], [])
    elif fault == "crossed":
        event = _change(token, BASE_MS + 250, "BUY", 0.60, 20)
    elif fault == "malformed_change":
        event = _change(token, BASE_MS + 250, "SELL", 0.50, 10)
        event["changes"][0]["size"] = "not-a-size"
    else:
        event = _snapshot(token, BASE_MS + 250, [(0.47, 20)], [(0.50, 10)])
        del event["asks"][0]["size"]
    detector.on_event(event)
    detector.on_event(_snapshot(UP, BASE_MS + 300, [(0.49, 20)], [(0.50, 10)]))
    detector.on_event(_snapshot(DOWN, BASE_MS + 301, [(0.47, 20)], [(0.48, 10)]))

    assert detector.flush(BASE_MS + 500.001) == []
    assert MARKET in detector.tainted_markets
    assert detector.outcomes[-1]["reason"] == "invalid_book_during_window"
    # The first market attempt stays invalid after apparent recovery.
    detector.on_event(_trade(BASE_MS + 1_000, 100))
    assert detector.flush(BASE_MS + 1_501) == []


def test_removing_the_entire_ask_then_restoring_snapshot_cannot_hide_depletion():
    detector = _ready()
    detector.on_event(_snapshot(UP, BASE_MS - 1, [(0.49, 20)], [(0.50, 10)]))
    for offset in (0, 100, 200):
        detector.on_event(_trade(BASE_MS + offset, 5))
    detector.on_event(_change(UP, BASE_MS + 250, "SELL", 0.50, 0))
    detector.on_event(_snapshot(UP, BASE_MS + 300, [(0.49, 20)], [(0.50, 10)]))

    assert detector.flush(BASE_MS + 500.001) == []
    assert detector.outcomes[-1]["reason"] == "invalid_book_during_window"
