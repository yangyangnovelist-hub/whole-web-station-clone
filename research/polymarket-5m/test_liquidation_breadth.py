from __future__ import annotations

import gzip
import json
from pathlib import Path

import liquidation_breadth as lb
import pytest


SLOT = 1_800_000_000
BASE_MS = (SLOT + 60) * 1_000.0
MARKET = "market-1"
UP = "up-token"
DOWN = "down-token"


def _write_raw(path: Path, rows: list[tuple[int, dict]]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        for receive_ns, payload in rows:
            stream.write(f"{receive_ns}\t{json.dumps(payload, separators=(',', ':'))}\n")


def test_all_market_parser_is_receipt_clocked_um_only_and_excludes_btc_duplicate(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bn_fut_liq.20261010T03.txt.gz"
    epoch = 1_791_600_000_000_000_000
    def force(symbol: str, side: str, *, st: int = 1) -> dict:
        return {
            "stream": "!forceOrder@arr",
            "data": {
                "e": "forceOrder",
                "E": 1_791_600_000_050,
                "st": st,
                "ps": symbol.removesuffix("USDT") + "USDT",
                "o": {
                    "s": symbol,
                    "S": side,
                    "o": "LIMIT",
                    "f": "IOC",
                    "q": "10",
                    "p": "99",
                    "ap": "100",
                    "X": "FILLED",
                    "l": "8",
                    "z": "8",
                    "T": 1_791_599_999_000,
                },
            },
        }
    _write_raw(
        path,
        [
            (
                epoch,
                {"_recorder": {"schema": "recorder-lifecycle-v1", "kind": "connection", "epoch": epoch}},
            ),
            (epoch + 1_000_000, force("ETHUSDT", "SELL")),
            (epoch + 2_000_000, force("BTCUSDT", "SELL")),
            (epoch + 3_000_000, force("ETHUSD_PERP", "SELL", st=2)),
            (
                epoch + 4_000_000,
                {"stream": "btcusdt@forceOrder", "data": force("BTCUSDT", "SELL")["data"]},
            ),
            (
                epoch + 5_000_000,
                {"_recorder": {"schema": "recorder-lifecycle-v1", "kind": "disconnect", "epoch": epoch}},
            ),
        ],
    )

    diagnostics: dict[str, int] = {}
    rows = list(lb.iter_binance_liquidation_events([path], diagnostics=diagnostics))

    assert [row["kind"] for row in rows] == [
        "liquidation_connection",
        "liquidation",
        "liquidation_disconnect",
    ]
    event = rows[1]
    assert event["recv_ms"] == (epoch + 1_000_000) / 1_000_000
    assert event["symbol"] == "ETHUSDT"
    assert event["side"] == "SELL"
    assert event["filled_notional"] == 800.0
    assert event["sampled_cumulative_filled_notional"] == 800.0
    assert event["recv_ns"] == epoch + 1_000_000
    assert event["product_type"] == 1
    assert event["feed_semantics"] == "latest_per_symbol_per_1000ms"
    assert event["source_ts_ms"] == 1_791_600_000_050
    assert event["trade_ts_ms"] == 1_791_599_999_000
    assert event["ordering_clock"] == "local_receipt_ms"
    assert diagnostics["yielded_liquidations"] == 1
    assert diagnostics.get("legacy_nested_product_type_frames", 0) == 0


def test_legacy_nested_product_type_is_explicitly_audited(tmp_path: Path) -> None:
    path = tmp_path / "bn_fut_liq.20261010T03.txt.gz"
    epoch = 1_791_600_000_000_000_000
    payload = {
        "stream": "!forceOrder@arr",
        "data": {
            "e": "forceOrder",
            "E": 1_791_600_000_050,
            "o": {
                "s": "ETHUSDT",
                "S": "SELL",
                "ap": "100",
                "z": "8",
                "T": 1_791_599_999_000,
                "st": 1,
            },
        },
    }
    _write_raw(
        path,
        [
            (
                epoch,
                {"_recorder": {"schema": "recorder-lifecycle-v1", "kind": "connection", "epoch": epoch}},
            ),
            (epoch + 1_000_000, payload),
        ],
    )
    diagnostics: dict[str, int] = {}

    rows = list(lb.iter_binance_liquidation_events([path], diagnostics=diagnostics))

    assert rows[-1]["kind"] == "liquidation"
    assert diagnostics["legacy_nested_product_type_frames"] == 1


def _market() -> dict:
    return {
        "kind": "market",
        "recv_ms": BASE_MS - 10,
        "market_id": MARKET,
        "slot": SLOT,
        "up_token_id": UP,
        "down_token_id": DOWN,
    }


def _snapshot(token: str, recv_ms: float, bids, asks) -> dict:
    return {
        "kind": "snapshot",
        "recv_ms": recv_ms,
        "epoch": 1,
        "market_id": MARKET,
        "asset_id": token,
        "bids": [{"price": str(price), "size": str(size)} for price, size in bids],
        "asks": [{"price": str(price), "size": str(size)} for price, size in asks],
        "ambiguous_receipt": False,
    }


def _change(token: str, recv_ms: float, side: str, price: float, size: float) -> dict:
    return {
        "kind": "price_change",
        "recv_ms": recv_ms,
        "epoch": 1,
        "market_id": MARKET,
        "changes": [{
            "asset_id": token,
            "side": side,
            "price": str(price),
            "size": str(size),
        }],
        "ambiguous_receipt": False,
    }


def _liq(recv_ms: float, symbol: str, side: str, notional: float = 300.0) -> dict:
    return {
        "kind": "liquidation",
        "recv_ms": recv_ms,
        "source_ts_ms": recv_ms - 100,
        "trade_ts_ms": recv_ms - 1_100,
        "symbol": symbol,
        "side": side,
        "filled_notional": notional,
        "connection_epoch": 7,
        "ordering_clock": "local_receipt_ms",
    }


def _ready() -> lb.LiquidationBreadthDetector:
    detector = lb.LiquidationBreadthDetector()
    detector.on_event(_market())
    detector.on_event({"kind": "connection", "recv_ms": BASE_MS - 9, "epoch": 1})
    detector.on_event(
        {"kind": "liquidation_connection", "recv_ms": BASE_MS - 8, "connection_epoch": 7}
    )
    detector.on_event(_snapshot(UP, BASE_MS - 7, [(0.49, 20)], [(0.51, 2), (0.52, 10)]))
    detector.on_event(_snapshot(DOWN, BASE_MS - 6, [(0.47, 20)], [(0.48, 3), (0.49, 10)]))
    return detector


def _broad_sell(detector: lb.LiquidationBreadthDetector, start: float = BASE_MS) -> list[dict]:
    out: list[dict] = []
    for index, symbol in enumerate(("ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT")):
        out.extend(detector.on_event(_liq(start + index * 100, symbol, "SELL")))
    return out


def test_four_symbol_sell_breadth_freezes_down_limit_and_matched_reversal() -> None:
    signals = _broad_sell(_ready())

    assert [row["variant"] for row in signals] == ["base", "direction_reversal"]
    base, reversal = signals
    assert base["buy_side"] == "Down"
    assert base["buy_asset_id"] == DOWN
    assert base["fixed_limit"] == 0.49
    assert base["decision_vwap"] == pytest.approx((0.48 * 3 + 0.49 * 2) / 5)
    assert base["distinct_symbols"] == 4
    assert base["side_notional"] == 1_200.0
    assert base["side_fraction"] == 1.0
    assert base["decision_recv_ms"] == BASE_MS + 300
    assert base["source_age_ms"] == pytest.approx(100.0)
    assert base["trade_age_ms"] == pytest.approx(1_100.0)
    assert reversal["parent_signal_id"] == base["signal_id"]
    assert reversal["buy_side"] == "Up"
    assert reversal["fixed_limit"] == 0.52


def test_delayed_and_buy_breadth_controls_are_causal_and_independently_locked() -> None:
    detector = _ready()
    base = _broad_sell(detector)[0]
    detector.on_event(_change(DOWN, BASE_MS + 4_900, "SELL", 0.48, 0))
    detector.on_event(_change(DOWN, BASE_MS + 4_901, "SELL", 0.49, 6))
    due = base["decision_recv_ms"] + 5_000
    delayed = detector.on_event(_change(DOWN, due, "SELL", 0.49, 0))

    assert len(delayed) == 1
    assert delayed[0]["variant"] == "time_shift"
    assert delayed[0]["parent_signal_id"] == base["signal_id"]
    assert delayed[0]["decision_recv_ms"] == due
    assert delayed[0]["fixed_limit"] == 0.49

    opposite: list[dict] = []
    for index, symbol in enumerate(("ADAUSDT", "BNBUSDT", "LINKUSDT", "AVAXUSDT")):
        opposite.extend(
            detector.on_event(_liq(BASE_MS + 7_000 + index * 100, symbol, "BUY"))
        )
    assert [row["variant"] for row in opposite] == ["short_liquidation"]
    assert opposite[0]["buy_side"] == "Up"
    assert detector.on_event(_liq(BASE_MS + 7_500, "SUIUSDT", "BUY", 5_000)) == []


def test_mixed_flow_window_and_source_disconnect_fail_closed() -> None:
    mixed = _ready()
    assert mixed.on_event(_liq(BASE_MS, "ADAUSDT", "BUY", 1_000)) == []
    for index, symbol in enumerate(("ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT")):
        assert mixed.on_event(_liq(BASE_MS + 100 + index * 100, symbol, "SELL", 300)) == []
    assert mixed.outcomes[-1]["reason"] == "weak_side_fraction"

    disconnected = _ready()
    disconnected.on_event(_liq(BASE_MS, "ETHUSDT", "SELL"))
    disconnected.on_event(
        {"kind": "liquidation_disconnect", "recv_ms": BASE_MS + 50, "connection_epoch": 7}
    )
    assert disconnected.on_event(_liq(BASE_MS + 100, "SOLUSDT", "SELL")) == []
    assert disconnected.liquidations == []


def test_repeated_symbol_uses_only_latest_lossy_snapshot_notional() -> None:
    detector = _ready()
    assert detector.on_event(_liq(BASE_MS, "ETHUSDT", "SELL", 900)) == []
    assert detector.on_event(_liq(BASE_MS + 50, "ETHUSDT", "SELL", 1)) == []
    assert detector.on_event(_liq(BASE_MS + 100, "SOLUSDT", "SELL", 100)) == []
    assert detector.on_event(_liq(BASE_MS + 200, "XRPUSDT", "SELL", 100)) == []

    signals = detector.on_event(_liq(BASE_MS + 300, "DOGEUSDT", "SELL", 100))

    assert signals == []
    assert detector.outcomes[-1]["reason"] == "insufficient_side_notional"


def test_hold_to_settlement_execution_uses_frozen_limit_and_direct_depth() -> None:
    detector = _ready()
    base = _broad_sell(detector)[0]
    decision = base["decision_recv_ms"]
    events = [
        _market(),
        {"kind": "connection", "recv_ms": BASE_MS - 9, "epoch": 1},
        _snapshot(UP, BASE_MS - 7, [(0.49, 20)], [(0.51, 2), (0.52, 10)]),
        _snapshot(DOWN, BASE_MS - 6, [(0.47, 20)], [(0.48, 3), (0.49, 10)]),
        _change(DOWN, decision + 350, "SELL", 0.48, 0),
        _change(DOWN, decision + 450, "SELL", 0.49, 4),
    ]

    rows = lb.replay_execution(
        events, [base], {MARKET: "Down"}, recording_end_ms=decision + 600
    )

    assert [row["evaluation_ms"] for row in rows] == [400.0, 500.0]
    assert rows[0]["full_fill"] is True
    assert rows[0]["filled_shares"] == 5.0
    assert rows[0]["fill_vwap"] == pytest.approx(0.49)
    assert rows[0]["fee"] == pytest.approx(0.08747)
    assert rows[0]["pnl"] == pytest.approx(5 - 0.49 * 5 - 0.08747)
    assert rows[1]["full_fill"] is False
    assert rows[1]["reason"] == "partial_fill"
    assert rows[1]["filled_shares"] == 4.0


def test_cross_stream_equal_receipt_is_marked_ambiguous_and_cannot_trigger() -> None:
    detector = _ready()
    prior = [
        _liq(BASE_MS, "ETHUSDT", "SELL"),
        _liq(BASE_MS + 100, "SOLUSDT", "SELL"),
        _liq(BASE_MS + 200, "XRPUSDT", "SELL"),
    ]
    for event in prior:
        detector.on_event(event)
    tie_ms = BASE_MS + 300
    merged = list(
        lb.merge_receipt_streams(
            [_change(DOWN, tie_ms, "SELL", 0.49, 9)],
            [_liq(tie_ms, "DOGEUSDT", "SELL")],
        )
    )

    assert all(event["ambiguous_cross_stream_receipt_tie"] for event in merged)
    signals: list[dict] = []
    for event in merged:
        signals.extend(detector.on_event(event))
    assert signals == []
    assert MARKET in detector.tainted_markets
