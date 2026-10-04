import gzip
import json

import numpy as np
import pandas as pd
import pytest

import binary as bo
import paper_trader as pt
from test_real_day import make_bundle

NAN = float("nan")
P = pt.Params(taus=(30,), lo=0.80, hi=0.97, max_shares=100)

# Shapes copied from the recorded market channel (outcometick sample payloads).
BOOK_UP = {"event_type": "book", "asset_id": "UP", "market": "0xabc", "timestamp": "1",
           "bids": [{"price": "0.88", "size": "40"}, {"price": "0.87", "size": "10"}],
           "asks": [{"price": "0.99", "size": "5000"}, {"price": "0.90", "size": "60"}]}
BOOK_DOWN = {"event_type": "book", "asset_id": "DOWN", "market": "0xabc", "timestamp": "1",
             "bids": [{"price": "0.09", "size": "70"}], "asks": [{"price": "0.12", "size": "80"}]}
PRICE_CHANGE = {"event_type": "price_change", "market": "0xabc", "timestamp": "2", "price_changes": [
    {"asset_id": "UP", "price": "0.90", "size": "0", "side": "SELL", "best_bid": "0.88", "best_ask": "0.91"},
    {"asset_id": "UP", "price": "0.91", "size": "25", "side": "SELL", "best_bid": "0.88", "best_ask": "0.91"}]}


def test_ladders_follow_book_and_price_changes():
    lad = {}
    pt.apply_clob_message(lad, [BOOK_UP, BOOK_DOWN], 1)
    assert (lad["UP"].bid, lad["UP"].ask, lad["UP"].size_at("ask", 0.90)) == (0.88, 0.90, 60)
    pt.apply_clob_message(lad, PRICE_CHANGE, 2)
    assert lad["UP"].ask == 0.91 and lad["UP"].size_at("ask", 0.91) == 25
    assert 0.90 not in lad["UP"].asks


def test_decide_buys_favourite_inside_band_capped_by_size():
    order = pt.decide((0.88, 0.91, 25, 40), (0.08, 0.12, 80, 70), 30, P)
    assert order["side"] == "Up" and order["ask"] == 0.91 and order["shares"] == 25
    assert order["cost"] == pytest.approx(25 * (0.91 + bo.taker_fee(0.91)))


def test_decide_uses_other_book_when_cheaper():
    # Down bid 0.11 means Up can be had at 0.89 by hitting it, cheaper than Up's own 0.95 ask.
    order = pt.decide((0.85, 0.95, 500, 40), (0.11, 0.14, 80, 30), 30, P)
    assert order["ask"] == 0.89 and order["shares"] == 30


def test_decide_skips_outside_band_and_empty_books():
    assert pt.decide((0.50, 0.52, 10, 10), (0.48, 0.50, 10, 10), 30, P)["skip"] == "ask outside band"
    assert pt.decide((0.98, 0.99, 10, 10), (0.01, 0.02, 10, 10), 30, P)["skip"] == "ask outside band"
    assert pt.decide((NAN, NAN, NAN, NAN), (NAN, NAN, NAN, NAN), 30, P)["skip"] == "no book"


def test_settle_and_summarize():
    order = pt.decide((0.88, 0.91, 25, 40), (0.08, 0.12, 80, 70), 30, P)
    win, loss = pt.settle(order, True), pt.settle(order, False)
    assert win["pnl"] == pytest.approx(25 - order["cost"]) and loss["pnl"] == pytest.approx(-order["cost"])
    ledger = pd.DataFrame([{**win, "end": 1}, {**loss, "end": 2}])
    text = pt.summarize(ledger)
    assert "| 30s | 2 | 50.0% |" in text


GAMMA = {"slug": "btc-updown-5m-1788825300", "outcomes": "[\"Up\", \"Down\"]",
         "clobTokenIds": "[\"UP\", \"DOWN\"]", "outcomePrices": "[\"0\", \"1\"]", "closed": True,
         "feeSchedule": {"rate": 0.07}}


def test_parse_gamma_market():
    m = pt.parse_gamma_market(GAMMA)
    assert (m["start"], m["end"], m["up_token"], m["down_token"], m["up_won"]) == \
        (1788825300, 1788825600, "UP", "DOWN", False)
    open_market = {**GAMMA, "closed": False, "outcomePrices": "[\"0.5\", \"0.5\"]"}
    assert pt.parse_gamma_market(open_market)["up_won"] is None


def test_live_trader_offline(tmp_path, monkeypatch):
    trader = pt.LiveTrader(tmp_path, P, fetch=lambda url: [GAMMA])
    trader.token_coin.update({"UP": "btc", "DOWN": "btc"})
    trader.on_clob(json.dumps([BOOK_UP, BOOK_DOWN]), 1)
    trader.on_clob(json.dumps({"event_type": "new_market", "market": "0xother"}), 1)  # not recorded
    trader.on_clob(json.dumps(PRICE_CHANGE), 2)
    trader.on_clob("PONG", 3)
    trader.on_rtds(json.dumps({"topic": "crypto_prices_chainlink", "type": "update",
                               "payload": {"symbol": "btc/usd", "timestamp": 5, "value": 79100.5}}), 4)
    assert trader.last_btc == (5, 79100.5)
    mk = pt.parse_gamma_market(GAMMA)
    rec = trader.evaluate(mk, 30)
    assert rec["side"] == "Up" and rec["shares"] == 25 and len(trader.pending) == 1
    trader.resolve_pending()  # market ended long ago and Gamma says Down won
    assert trader.pending == []
    ledger = pd.read_csv(tmp_path / "ledger.csv")
    assert len(ledger) == 1 and not ledger["won"].iloc[0]
    assert "已结算 1 笔" in pt.write_report(tmp_path)
    trader.rec.close()
    raw = list(tmp_path.glob("raw/*/*.jsonl.gz"))
    assert {p.name.split(".")[0] for p in raw} >= {"clob-btc", "rtds", "decisions", "markets"}
    clob = [json.loads(line) for line in gzip.open(next(tmp_path.glob("raw/*/clob-btc.jsonl.gz")), "rt")]
    assert all(ev.get("event_type") != "new_market" for rec in clob for ev in rec["msg"])


def test_live_recorder_keeps_bookticker_and_explicit_clob_epoch(tmp_path):
    trader = pt.LiveTrader(tmp_path, P, fetch=lambda url: [])
    trader.token_coin.update({"UP": "btc", "DOWN": "btc"})
    trader.subscribed.update({"UP", "DOWN"})

    assert trader.record_clob_open(1_000) == 1
    trader.on_clob(json.dumps([BOOK_UP, BOOK_DOWN]), 1_001.25)
    trader.record_clob_close("closed 1000", 1_002)
    trader.record_binance_open("spot", 1_003)
    trader.record_binance_open("spot", 1_003.1, conn_id=1)
    trader.record_binance_open("spot", 1_003.2, conn_id=2)
    trader.on_binance(json.dumps({"stream": "btcusdt@bookTicker", "data": {
        "u": 7, "s": "BTCUSDT", "b": "80000.1", "B": "2", "a": "80000.2", "A": "3",
    }}), 1_003.5)
    trader.on_binance(json.dumps({"stream": "btcusdt@bookTicker", "data": {
        "u": 8, "s": "BTCUSDT", "b": "80000.1", "B": "20", "a": "80000.2", "A": "30",
    }}), 1_004.5)  # size-only updates are not frozen-H trigger events
    trader.record_binance_close("spot", "closed 1000", 1_004.7, conn_id=1)
    trader.record_binance_close("spot", "closed 1000", 1_004.8, conn_id=2)
    trader.record_binance_close("spot", "closed 1000", 1_005)

    trader.record_binance_open("futures", 1_006)
    trader.record_binance_open("futures", 1_006.1, conn_id=1)
    trader.on_binance_futures(json.dumps({
        "e": "bookTicker", "E": 900, "T": 899, "u": 11, "s": "BTCUSDT",
        "b": "80000.1", "B": "2", "a": "80000.2", "A": "3",
    }), 1_006.5)
    trader.on_binance_futures(json.dumps({
        "e": "bookTicker", "E": 901, "T": 900, "u": 12, "s": "BTCUSDT",
        "b": "80000.1", "B": "20", "a": "80000.2", "A": "30",
    }), 1_007.5)  # production consumes every futures frame, including size-only changes
    trader.record_binance_close("futures", "closed 1000", 1_007.8, conn_id=1)
    trader.record_binance_close("futures", "closed 1000", 1_008)

    trader.record_binance_open("futures_trade", 1_009)
    trader.record_binance_open("futures_trade", 1_009.1, conn_id=1)
    future_trade = json.dumps({"e": "trade", "E": 902, "T": 901, "t": 13, "s": "BTCUSDT",
                               "p": "80000.2", "q": "0.4", "m": False})
    trader.on_binance_futures_trade(future_trade, 1_009.5, conn_id=0)
    trader.on_binance_futures_trade(future_trade, 1_009.6, conn_id=1)
    trader.record_binance_close("futures_trade", "closed 1000", 1_009.8, conn_id=1)
    trader.record_binance_close("futures_trade", "closed 1000", 1_010)

    trader.record_binance_open("deribit", 1_011)
    quote = json.dumps({"jsonrpc": "2.0", "method": "subscription", "params": {
        "channel": "quote.BTC-PERPETUAL", "data": {
            "timestamp": 903, "best_bid_price": 80000.1, "best_ask_price": 80000.3,
            "best_bid_amount": 2, "best_ask_amount": 3,
        },
    }})
    trader.on_deribit_quote(quote, 1_011.5)
    trader.on_deribit_quote(quote, 1_011.6)
    trader.record_binance_close("deribit", "closed 1000", 1_012)
    trader.rec.close()

    clob = [json.loads(line) for line in gzip.open(next(tmp_path.glob("raw/*/clob-btc.jsonl.gz")), "rt")]
    assert clob[0]["connection_epoch"] == 1 and clob[0]["sequence"] == 1
    errors = [json.loads(line) for line in gzip.open(next(tmp_path.glob("raw/*/errors.jsonl.gz")), "rt")]
    assert [(row["where"], row["connection_epoch"], row["sequence"]) for row in errors] == [
        ("clob-open", 1, 0), ("clob", 1, 2),
    ]
    binance = [json.loads(line) for line in gzip.open(next(tmp_path.glob("raw/*/binance-strict.jsonl.gz")), "rt")]
    assert [row["kind"] for row in binance] == ["connection", "bookTicker", "disconnect"]
    assert {key: binance[1][key] for key in ("kind", "recv_ms", "s", "E", "u", "b", "B", "a", "A",
                                                   "connection_epoch", "sequence", "active_connections",
                                                   "max_active_connections", "conn_id")} == {
        "kind": "bookTicker", "recv_ms": 1_003.5, "s": "BTCUSDT", "E": None, "u": 7,
        "b": "80000.1", "B": "2", "a": "80000.2", "A": "3", "connection_epoch": 1,
        "sequence": 1, "active_connections": 3, "max_active_connections": 3, "conn_id": 0,
    }
    futures = [json.loads(line) for line in
               gzip.open(next(tmp_path.glob("raw/*/binance-futures-strict.jsonl.gz")), "rt")]
    assert [row["kind"] for row in futures] == ["connection", "bookTicker", "bookTicker", "disconnect"]
    assert [(row.get("T"), row.get("E")) for row in futures[1:3]] == [(899, 900), (900, 901)]
    assert all(row["connection_epoch"] == 1 and row["sequence"] == index
               for index, row in enumerate(futures))
    futures_trades = [json.loads(line) for line in
                      gzip.open(next(tmp_path.glob("raw/*/binance-futures-trade-strict.jsonl.gz")), "rt")]
    assert [row["kind"] for row in futures_trades] == ["connection", "trade", "disconnect"]
    assert futures_trades[1]["t"] == 13 and futures_trades[1]["T"] == 901
    deribit = [json.loads(line) for line in
               gzip.open(next(tmp_path.glob("raw/*/deribit-strict.jsonl.gz")), "rt")]
    assert [row["kind"] for row in deribit] == ["connection", "quote", "disconnect"]
    assert deribit[1]["timestamp"] == 903 and deribit[1]["bid"] == 80000.1
    assert pt.binance_stream_names(("btc",)) == (
        "btcusdt@aggTrade", "btcusdt@trade", "btcusdt@bookTicker",
    )


def test_replay_on_synthetic_bundle(tmp_path):
    root = make_bundle(tmp_path / "polymarket-data-samples")
    ledger = pt.replay(root, pt.Params(taus=(60, 30), lo=0.80, hi=0.99, max_shares=100))
    # The synthetic book quotes the winner at 0.97 in the last 100s, with 150 shares at the ask.
    assert len(ledger) > 0
    assert ledger["won"].all() and (ledger["ask"] == 0.98).all() and (ledger["shares"] == 100).all()


def test_book_messages_with_buys_and_sells():
    # Older market-channel payloads name the sides buys/sells; they must not wipe the ladder.
    lad = {}
    pt.apply_clob_message(lad, {**BOOK_UP, "bids": None, "asks": None}, 1)
    old = {k: v for k, v in BOOK_UP.items() if k not in ("bids", "asks")}
    pt.apply_clob_message(lad, {**old, "buys": BOOK_UP["bids"], "sells": BOOK_UP["asks"]}, 2)
    assert (lad["UP"].bid, lad["UP"].ask) == (0.88, 0.90)


def test_fetch_market_asks_for_closed_markets_first():
    asked = []

    def fetch(url):
        asked.append(url)
        return [GAMMA] if "closed=true" in url else []

    assert pt.fetch_market(fetch, GAMMA["slug"]) == GAMMA and len(asked) == 1
    assert pt.fetch_market(lambda url: [], GAMMA["slug"]) is None
