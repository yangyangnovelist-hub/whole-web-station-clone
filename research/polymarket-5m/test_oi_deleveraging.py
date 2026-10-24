from __future__ import annotations

import gzip
import json
from pathlib import Path

import oi_deleveraging as od
import pytest


SLOT = 1_800_000_000
BASE_MS = (SLOT + 60) * 1_000.0
MARKET = "market-1"
UP = "up-token"
DOWN = "down-token"


def _write(path: Path, rows: list[tuple[int, dict]]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        for receive_ns, payload in rows:
            stream.write(f"{receive_ns}\t{json.dumps(payload, separators=(',', ':'))}\n")


def _marker(epoch: int, kind: str) -> dict:
    return {
        "_recorder": {
            "schema": "recorder-lifecycle-v1",
            "kind": kind,
            "epoch": epoch,
        }
    }


def test_normalizes_relative_oi_units_and_binance_reference_price(tmp_path: Path) -> None:
    epoch = 1_800_000_000_000_000_000
    deribit = tmp_path / "deribit_vol.20261011T00.txt.gz"
    okx = tmp_path / "okx_liq.20261011T00.txt.gz"
    bybit = tmp_path / "bybit_liq.20261011T00.txt.gz"
    binance = tmp_path / "bn_fut_mark.20261011T00.txt.gz"
    _write(deribit, [
        (epoch, _marker(epoch, "connection")),
        (epoch + 1, {"params": {"channel": "ticker.BTC-PERPETUAL.agg2", "data": {
            "timestamp": 1_800_000_000_001, "instrument_name": "BTC-PERPETUAL",
            "open_interest": 800_000_000, "index_price": 80_000,
        }}}),
    ])
    _write(okx, [
        (epoch + 2, _marker(epoch + 2, "connection")),
        (epoch + 3, {"arg": {"channel": "open-interest", "instId": "BTC-USDT-SWAP"}, "data": [{
            "instId": "BTC-USDT-SWAP", "oiCcy": "30000", "ts": "1800000000002",
        }]}),
    ])
    _write(bybit, [
        (epoch + 4, _marker(epoch + 4, "connection")),
        (epoch + 5, {"topic": "tickers.BTCUSDT", "type": "delta", "ts": 1_800_000_000_003,
                     "data": {"symbol": "BTCUSDT", "markPrice": "80000",
                              "openInterestValue": "4800000000"}}),
    ])
    _write(binance, [
        (epoch + 6, _marker(epoch + 6, "connection")),
        (epoch + 7, {"stream": "btcusdt@markPrice@1s", "data": {
            "e": "markPriceUpdate", "E": 1_800_000_000_004, "s": "BTCUSDT",
            "p": "80010", "i": "80000",
        }}),
    ])

    rows = list(od.merge_source_events({
        "deribit": [deribit], "okx": [okx], "bybit": [bybit], "binance": [binance],
    }))
    observations = [row for row in rows if row["kind"] in {"oi", "reference_price"}]

    assert [(row["kind"], row["source"]) for row in observations] == [
        ("oi", "deribit"), ("oi", "okx"), ("oi", "bybit"),
        ("reference_price", "binance"),
    ]
    assert observations[0]["oi_level"] == 800_000_000
    assert observations[0]["unit"] == "usd_contracts"
    assert observations[1]["oi_level"] == 30_000
    assert observations[1]["unit"] == "btc"
    assert observations[2]["oi_level"] == pytest.approx(60_000)
    assert observations[2]["unit"] == "btc_derived_from_value_over_mark"
    assert observations[3]["price"] == 80_000
    assert all(row["ordering_clock"] == "local_receipt_ms" for row in observations)


def _market() -> dict:
    return {
        "kind": "market", "recv_ms": BASE_MS - 20, "market_id": MARKET,
        "slot": SLOT, "up_token_id": UP, "down_token_id": DOWN,
    }


def _snapshot(token: str, recv_ms: float, asks: list[tuple[float, float]]) -> dict:
    return {
        "kind": "snapshot", "recv_ms": recv_ms, "epoch": 1,
        "market_id": MARKET, "asset_id": token,
        "bids": [{"price": "0.40", "size": "20"}],
        "asks": [{"price": str(price), "size": str(size)} for price, size in asks],
        "ambiguous_receipt": False,
    }


def _source(source: str, recv_ms: float, *, oi: float | None = None,
            price: float | None = None, epoch: int = 10) -> dict:
    row = {
        "kind": "oi" if oi is not None else "reference_price",
        "source": source, "recv_ms": recv_ms, "source_ts_ms": recv_ms - 10,
        "connection_epoch": epoch, "ordering_clock": "local_receipt_ms",
    }
    if oi is not None:
        row["oi_level"] = oi
    else:
        row["price"] = price
    return row


def _ready_detector() -> od.OiDeleveragingDetector:
    detector = od.OiDeleveragingDetector()
    detector.on_event(_market())
    detector.on_event({"kind": "connection", "recv_ms": BASE_MS - 19, "epoch": 1})
    for offset, source in enumerate(("deribit", "okx", "bybit", "binance")):
        detector.on_event({
            "kind": "source_connection", "source": source,
            "recv_ms": BASE_MS - 18 + offset, "connection_epoch": 10,
        })
    detector.on_event(_snapshot(UP, BASE_MS - 10, [(0.51, 2), (0.52, 10)]))
    detector.on_event(_snapshot(DOWN, BASE_MS - 9, [(0.47, 3), (0.48, 10)]))
    return detector


def test_two_venue_oi_drop_with_price_rise_emits_reversal_and_direction_control() -> None:
    detector = _ready_detector()
    detector.on_event(_source("deribit", BASE_MS, oi=800_000_000))
    detector.on_event(_source("okx", BASE_MS + 1, oi=30_000))
    detector.on_event(_source("bybit", BASE_MS + 2, oi=60_000))
    detector.on_event(_source("binance", BASE_MS, price=80_000))
    detector.on_event(_snapshot(UP, BASE_MS + 9_500, [(0.51, 2), (0.52, 10)]))
    detector.on_event(_snapshot(DOWN, BASE_MS + 9_501, [(0.47, 3), (0.48, 10)]))
    detector.on_event(_source("deribit", BASE_MS + 9_800, oi=799_520_000))
    detector.on_event(_source("binance", BASE_MS + 9_900, price=80_016))

    signals = detector.on_event(_source("okx", BASE_MS + 10_001, oi=29_982))

    assert [row["variant"] for row in signals] == ["base", "direction_control"]
    base, control = signals
    assert base["buy_side"] == "Down"
    assert base["buy_asset_id"] == DOWN
    assert base["fixed_limit"] == 0.48
    assert base["decision_vwap"] == pytest.approx((0.47 * 3 + 0.48 * 2) / 5)
    assert base["qualifying_venues"] == ["deribit", "okx"]
    assert base["price_return"] == pytest.approx(0.0002)
    assert base["oi_returns"]["deribit"] == pytest.approx(-0.0006)
    assert base["oi_returns"]["okx"] == pytest.approx(-0.0006)
    assert control["buy_side"] == "Up"
    assert control["parent_signal_id"] == base["signal_id"]


def test_five_second_control_refreshes_book_without_reusing_old_limit() -> None:
    detector = _ready_detector()
    detector.on_event(_source("deribit", BASE_MS, oi=800_000_000))
    detector.on_event(_source("okx", BASE_MS + 1, oi=30_000))
    detector.on_event(_source("binance", BASE_MS, price=80_000))
    detector.on_event(_snapshot(DOWN, BASE_MS + 9_500, [(0.47, 3), (0.48, 10)]))
    detector.on_event(_source("deribit", BASE_MS + 9_800, oi=799_520_000))
    detector.on_event(_source("binance", BASE_MS + 9_900, price=80_016))
    base = detector.on_event(_source("okx", BASE_MS + 10_001, oi=29_982))[0]
    due = base["decision_recv_ms"] + 5_000
    detector.on_event(_snapshot(DOWN, due - 1, [(0.49, 1), (0.50, 10)]))

    shifted = detector.on_event(_source("binance", due, price=80_010))

    assert len(shifted) == 1
    assert shifted[0]["variant"] == "time_shift"
    assert shifted[0]["parent_signal_id"] == base["signal_id"]
    assert shifted[0]["fixed_limit"] == 0.50
    assert shifted[0]["decision_vwap"] == pytest.approx((0.49 + 0.50 * 4) / 5)


def test_reused_executor_enforces_frozen_limit_and_direct_depth() -> None:
    decision = BASE_MS + 10_000
    signal = {
        "signal_id": "oi:base:1", "parent_signal_id": None, "variant": "base",
        "market_id": MARKET, "buy_asset_id": DOWN, "buy_side": "Down",
        "decision_recv_ms": decision, "fixed_limit": 0.48,
        "target_shares": 5.0, "sent": True, "reason": "eligible",
    }
    events = [
        _market(),
        {"kind": "connection", "recv_ms": decision - 10, "epoch": 1},
        _snapshot(DOWN, decision - 9, [(0.47, 3), (0.48, 10)]),
        _snapshot(DOWN, decision + 450, [(0.48, 4), (0.49, 10)]),
    ]

    rows = od.replay_execution(
        events, [signal], {MARKET: "Down"}, recording_end_ms=decision + 600
    )

    assert [row["evaluation_ms"] for row in rows] == [400.0, 500.0]
    assert rows[0]["full_fill"] is True
    assert rows[0]["filled_shares"] == 5.0
    assert rows[1]["full_fill"] is False
    assert rows[1]["reason"] == "partial_fill"
    assert rows[1]["filled_shares"] == 4.0


def test_source_disconnect_and_equal_receipt_ambiguity_fail_closed() -> None:
    disconnected = _ready_detector()
    disconnected.on_event(_source("deribit", BASE_MS, oi=800_000_000))
    disconnected.on_event(_source("okx", BASE_MS + 1, oi=30_000))
    disconnected.on_event(_source("binance", BASE_MS + 2, price=80_000))
    disconnected.on_event({
        "kind": "source_disconnect", "source": "okx",
        "recv_ms": BASE_MS + 5_000, "connection_epoch": 10,
    })
    disconnected.on_event(_source("deribit", BASE_MS + 9_800, oi=799_520_000))
    disconnected.on_event(_source("binance", BASE_MS + 9_900, price=80_016))
    assert disconnected.on_event(_source("okx", BASE_MS + 10_001, oi=29_982)) == []

    ambiguous = _ready_detector()
    ambiguous.on_event({
        **_source("deribit", BASE_MS, oi=800_000_000),
        "ambiguous_cross_source_receipt_tie": True,
    })
    assert MARKET in ambiguous.tainted_markets


def test_okx_plain_pong_control_frame_is_explicitly_ignored(tmp_path: Path) -> None:
    path = tmp_path / "okx_liq.20261011T00.txt.gz"
    epoch = 1_800_000_000_000_000_000
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        stream.write(f"{epoch}\t{json.dumps(_marker(epoch, 'connection'))}\n")
        stream.write(f"{epoch + 1}\tpong\n")
        stream.write(
            f"{epoch + 2}\t"
            + json.dumps({
                "arg": {"channel": "open-interest", "instId": "BTC-USDT-SWAP"},
                "data": [{"instId": "BTC-USDT-SWAP", "oiCcy": "30000", "ts": "1800000000002"}],
            })
            + "\n"
        )

    rows = list(od.iter_source_events("okx", [path]))

    assert [row["kind"] for row in rows] == ["source_connection", "oi"]


def test_less_than_ten_seconds_of_post_reconnect_history_cannot_trigger() -> None:
    detector = _ready_detector()
    detector.on_event(_source("deribit", BASE_MS + 5_000, oi=800_000_000))
    detector.on_event(_source("okx", BASE_MS + 5_001, oi=30_000))
    detector.on_event(_source("bybit", BASE_MS + 5_002, oi=60_000))
    detector.on_event(_source("binance", BASE_MS + 5_003, price=80_000))
    detector.on_event(_snapshot(UP, BASE_MS + 9_500, [(0.51, 2), (0.52, 10)]))
    detector.on_event(_snapshot(DOWN, BASE_MS + 9_501, [(0.47, 3), (0.48, 10)]))
    detector.on_event(_source("deribit", BASE_MS + 9_800, oi=799_520_000))
    detector.on_event(_source("binance", BASE_MS + 9_900, price=80_016))

    signals = detector.on_event(_source("okx", BASE_MS + 10_001, oi=29_982))

    assert signals == []


def test_reconnect_clears_parent_linked_delay_and_ambiguous_disconnect_still_applies() -> None:
    detector = _ready_detector()
    detector.on_event(_source("deribit", BASE_MS, oi=800_000_000))
    detector.on_event(_source("okx", BASE_MS + 1, oi=30_000))
    detector.on_event(_source("binance", BASE_MS, price=80_000))
    detector.on_event(_snapshot(DOWN, BASE_MS + 9_500, [(0.47, 3), (0.48, 10)]))
    detector.on_event(_source("deribit", BASE_MS + 9_800, oi=799_520_000))
    detector.on_event(_source("binance", BASE_MS + 9_900, price=80_016))
    detector.on_event(_source("okx", BASE_MS + 10_001, oi=29_982))
    assert len(detector.delayed) == 1

    detector.on_event({
        "kind": "source_connection", "source": "okx",
        "recv_ms": BASE_MS + 10_100, "connection_epoch": 11,
    })
    assert detector.delayed == []

    detector.on_event({
        "kind": "source_disconnect", "source": "deribit",
        "recv_ms": BASE_MS + 10_200, "connection_epoch": 10,
        "ambiguous_cross_stream_receipt_tie": True,
    })
    assert "deribit" not in detector.source_epochs
