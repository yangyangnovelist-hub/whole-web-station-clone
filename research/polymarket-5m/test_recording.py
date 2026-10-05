import gzip
import json
import time

import numpy as np
import pandas as pd
import pytest

import binary as bo
import paper_trader as pt
import real_day as rd
import recording as rc
import strategy_zoo as sz

S0 = 1_788_825_600
P = pt.Params(taus=(30,), lo=0.80, hi=0.97, max_shares=100)


def gamma(start, up_won=None):
    return {"slug": f"btc-updown-5m-{start}", "outcomes": "[\"Up\", \"Down\"]",
            "clobTokenIds": f"[\"up{start}\", \"dn{start}\"]",
            "outcomePrices": "[\"0.5\", \"0.5\"]" if up_won is None else
            ("[\"1\", \"0\"]" if up_won else "[\"0\", \"1\"]"),
            "closed": up_won is not None, "feeSchedule": {"rate": 0.07},
            "cryptoMarketConfig": {"twapLookbackSeconds": 60}}


def book(token, bid, ask):
    return {"event_type": "book", "asset_id": token, "bids": [{"price": f"{bid:.2f}", "size": "40"}],
            "asks": [{"price": f"{ask:.2f}", "size": "150"}, {"price": f"{min(ask + 0.01, 1):.2f}", "size": "50"}]}


@pytest.fixture(scope="module")
def recorded(tmp_path_factory):
    """Two markets recorded through LiveTrader itself, so the test covers the real file format."""
    out = tmp_path_factory.mktemp("paper")
    rng = np.random.default_rng(5)
    secs = np.arange(S0 - 1200, S0 + 700)
    price = 80_000 * np.exp(np.cumsum(rng.normal(0, 1e-4, len(secs))))
    spot = dict(zip(secs, price))
    twap = {s: np.mean([spot[x] for x in range(s - 59, s + 1) if x in spot]) for s in (S0, S0 + 300, S0 + 600)}
    won = {start: bool(twap[start + 300] >= twap[start]) for start in (S0, S0 + 300)}
    def fetch(url):
        start = int(url.split("btc-updown-5m-")[1].split("&")[0])
        return [gamma(start, won[start])]

    trader = pt.LiveTrader(out, P, fetch=fetch)
    for s, v in zip(secs, price):
        trader.on_rtds(json.dumps({"topic": "crypto_prices_chainlink", "type": "update",
                                   "payload": {"symbol": "btc/usd", "timestamp": int(s) * 1000, "value": v}}),
                       int(s) * 1000 + 1500)
    trader.on_rtds(json.dumps({"topic": "crypto_prices_chainlink", "type": "update",
                               "payload": {"symbol": "eth/usd", "timestamp": S0 * 1000, "value": 3000.0}}), S0 * 1000)
    for start in (S0, S0 + 300):
        mk = gamma(start)
        trader.rec.write("markets", mk)
        trader.markets[mk["slug"]] = pt.parse_gamma_market(mk)
        trader.token_coin.update({f"up{start}": "btc", f"dn{start}": "btc"})
        up, dn = f"up{start}", f"dn{start}"
        for t in range(300):
            p = (0.97 if won[start] else 0.03) if t > 200 else 0.5
            bid, ask = bo.quote_book(p)
            ms = (start + t) * 1000 - 50
            trader.on_clob(json.dumps([book(up, bid, ask), book(dn, 1 - ask, 1 - bid)]), ms)
            if t == 100:  # a second update inside the same second: the snapshot must keep the last state
                trader.on_clob(json.dumps({"event_type": "price_change", "price_changes": [
                    {"asset_id": up, "price": "0.51", "size": "0", "side": "SELL"},
                    {"asset_id": up, "price": "0.53", "size": "7", "side": "SELL"}]}), ms + 20)
        trader.on_clob(json.dumps({"event_type": "last_trade_price", "asset_id": up, "price": "0.40", "side": "SELL",
                                   "size": "10", "timestamp": str((start + 70) * 1000)}), (start + 70) * 1000)
    trader.record_outcomes()
    trader.rec.close()
    return out, spot, won


def test_record_outcomes_keeps_unresolved_markets(tmp_path):
    now = int(time.time())
    start = now // 300 * 300 - 600  # ended between 5 and 10 minutes ago
    trader = pt.LiveTrader(tmp_path, P, fetch=lambda url: [gamma(start)])
    trader.markets[f"btc-updown-5m-{start}"] = pt.parse_gamma_market(gamma(start))
    trader.record_outcomes()
    trader.rec.close()
    assert trader.markets[f"btc-updown-5m-{start}"]["retry_at"] > now
    assert not list(tmp_path.glob("raw/*/markets.jsonl*"))


def test_bundle_round_trip(recorded, tmp_path):
    out, spot, won = recorded
    counts = rc.build(out, tmp_path / "bundle")
    assert counts["markets"] == counts["resolved"] == counts["with_strike"] == 2
    assert counts["prices"] == len(spot) and counts["trade"] == 2
    markets = rd.load_markets(tmp_path / "bundle").set_index("start")
    for start in (S0, S0 + 300):
        assert bool(markets.loc[start, "up_won"]) == won[start]
        assert markets.loc[start, "strike"] == pytest.approx(np.mean([spot[s] for s in range(start - 59, start + 1)]),
                                                            rel=1e-12)
    # One book snapshot per token per second, holding the state after the last update of that second.
    with gzip.open(next((tmp_path / "bundle").rglob("*-book-*.jsonl.gz")), "rt") as f:
        snaps = pd.DataFrame([json.loads(line) for line in f])
    snaps["sec"] = snaps["recv_ms"] // 1000
    assert not snaps.duplicated(["asset_id", "sec"]).any()
    at_100 = snaps[(snaps["asset_id"] == f"up{S0}") & (snaps["sec"] == S0 + 99)].iloc[0]
    assert [a["price"] for a in at_100["payload"]["asks"]] == ["0.52", "0.53"]


def test_zoo_runs_on_recording(recorded, tmp_path):
    out, _, won = recorded
    rc.build(out, tmp_path / "bundle")
    markets = sz.build_markets(tmp_path / "bundle")
    assert [m.up_won for m in markets] == [won[S0], won[S0 + 300]]
    for m in markets:
        f = sz.late_favourite(30, 0.85, 0.99)(m)
        assert f is not None and (f["side"] == "Up") == m.up_won
    res, _ = sz.evaluate(markets, [sz.registry()[i - 1] for i in sz.PREREGISTERED], reps=100,
                         ids=sz.PREREGISTERED)
    assert res["id"].tolist() == list(sz.PREREGISTERED)
    assert "预注册候选的前向检验" in sz.report(res, markets, 100, confirm=True)


def test_fetch_outcomes_backfills_markets_a_run_missed(tmp_path):
    rec = pt.Recorder(tmp_path)
    rec.write("markets", gamma(S0))  # recorded while open, run stopped before it resolved
    rec.write("markets", gamma(S0 + 300, up_won=True))  # already resolved: no fetch needed
    rec.close()
    asked = []

    def fetch(url):
        asked.append(url)
        return [gamma(S0, up_won=False)]

    assert rc.fetch_outcomes(tmp_path, fetch=fetch) == 0
    assert asked == [pt.GAMMA_CLOSED.format(slug=f"btc-updown-5m-{S0}")]
    latest = list(rc.iter_jsonl(rc.raw_files(tmp_path, "markets")))
    assert latest[-1]["slug"] == f"btc-updown-5m-{S0}" and latest[-1]["closed"]


def test_diagnose_prints_decision_time_books(recorded, capsys):
    out, _, _ = recorded
    rc.diagnose(out)
    text = capsys.readouterr().out
    assert "event types:" in text and "example book:" in text and "tau=30:" in text
    assert f"btc-updown-5m-{S0}" in text


def test_zoo_handles_a_recording_without_resolved_markets(tmp_path):
    rec = pt.Recorder(tmp_path)
    rec.write("markets", gamma(S0))
    rec.write("rtds", {"recv_ms": S0 * 1000, "msg": {"topic": "crypto_prices_chainlink", "payload": {
        "symbol": "btc/usd", "timestamp": S0 * 1000, "value": 1.0}}})
    rec.write("clob", {"recv_ms": S0 * 1000, "msg": book(f"up{S0}", 0.4, 0.6)})  # the old single-file name
    rec.close()
    rc.build(tmp_path, tmp_path / "bundle")
    assert sz.build_markets(tmp_path / "bundle") == []
    sz.main([str(tmp_path / "bundle"), "--confirm", "--trades-out", str(tmp_path / "t.csv")])
    assert pd.read_csv(tmp_path / "t.csv").empty


def test_two_coins_record_and_build_separately(tmp_path):
    trader = pt.LiveTrader(tmp_path, P, fetch=lambda url: [], coins=("btc", "eth"))
    btc, eth = gamma(S0), {**gamma(S0), "slug": f"eth-updown-5m-{S0}", "clobTokenIds": "[\"ue\", \"de\"]"}
    for m, coin in ((btc, "btc"), (eth, "eth")):
        trader.rec.write("markets", m)
        mk = pt.parse_gamma_market(m)
        trader.markets[mk["slug"]] = mk
        trader.token_coin.update({mk["up_token"]: coin, mk["down_token"]: coin})
    for s in range(S0 - 120, S0 + 10):
        for sym, v in (("btc/usd", 80_000.0), ("eth/usd", 3_000.0)):
            trader.on_rtds(json.dumps({"topic": "crypto_prices_chainlink", "type": "update",
                                       "payload": {"symbol": sym, "timestamp": s * 1000, "value": v}}), s * 1000 + 500)
    trader.on_clob(json.dumps([book(f"up{S0}", 0.4, 0.6), book("ue", 0.3, 0.7)]), S0 * 1000)
    trader.rec.close()
    assert {p.name for p in tmp_path.glob("raw/*/clob-*.jsonl.gz")} == {"clob-btc.jsonl.gz", "clob-eth.jsonl.gz"}
    c_btc = rc.build(tmp_path, tmp_path / "b-btc", "btc")
    c_eth = rc.build(tmp_path, tmp_path / "b-eth", "eth")
    assert c_btc["markets"] == c_eth["markets"] == 1 and c_btc["book"] == c_eth["book"] == 1
    assert next((tmp_path / "b-eth").rglob("ETHUSD-prices-*.csv.gz"))
    assert rd.load_markets(tmp_path / "b-eth")["strike"].iloc[0] == pytest.approx(3_000.0)


def test_latency_files_use_exchange_time(tmp_path):
    """The Up ask is 0.50 until the exchange stamps 0.70 at start+100.4; messages arrive 0.2 s later.
    latency.py must see the exchange's times and Polymarket's relay of Binance."""
    import latency as lt
    trader = pt.LiveTrader(tmp_path, P, fetch=lambda url: [])
    mk = gamma(S0, up_won=True)
    trader.rec.write("markets", mk)
    trader.markets[mk["slug"]] = pt.parse_gamma_market(mk)
    trader.token_coin.update({f"up{S0}": "btc", f"dn{S0}": "btc"})
    rng = np.random.default_rng(8)
    price = 80_000.0
    for s in range(S0 - 900, S0 + 300):
        price *= np.exp(rng.normal(0, 1e-5)) * (1.004 if s == S0 + 100 else 1.0)
        trader.on_rtds(json.dumps({"topic": "crypto_prices", "type": "update", "payload": {
            "symbol": "btcusdt", "timestamp": s * 1000 + 50, "value": price}}), s * 1000 + 350, "rtds-binance")
    for s in range(S0 + 90, S0 + 110):  # Coinbase prints, exchange time as ISO text
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(s)) + ".250000Z"
        trader.on_coinbase(json.dumps({"type": "match", "product_id": "BTC-USD", "time": stamp,
                                       "price": "80000.5", "size": "0.01", "side": "sell"}), s * 1000 + 400)
    trader.on_coinbase(json.dumps({"type": "subscriptions", "channels": []}), S0 * 1000)
    for t_ms, ask in ((S0 * 1000, 0.50), ((S0 + 100) * 1000 + 400, 0.70)):
        trader.on_clob(json.dumps([{**book(f"up{S0}", ask - 0.01, ask), "timestamp": str(t_ms)}]), t_ms + 200)
    trader.rec.close()
    assert (tmp_path / "raw").glob("*/rtds-binance.jsonl.gz")
    counts = rc.build(tmp_path, tmp_path / "bundle")
    assert counts["latency_markets"] == 1 and counts["latency_books"] == 2 and counts["latency_binance"] == 1200
    assert counts["latency_coinbase"] == 20
    cb = lt.load(tmp_path / "bundle" / "latency", spot="coinbase")[2]
    assert cb["trade_ts"].iloc[0] == pytest.approx(S0 + 90.25) and cb["receive_ts"].iloc[0] == pytest.approx(S0 + 90.4)
    books, markets, binance = lt.load(tmp_path / "bundle" / "latency")
    assert list(books["ts"]) == [S0, S0 + 100.4] and list(markets["winner"]) == ["Up"]
    grid, sigma = lt.spot_grid(binance)
    trig = lt.triggers(markets, binance, sigma, 3.0)
    assert list(trig["t"]) == [pytest.approx(S0 + 100.05)]
    book_ = lt.Book(books)
    assert list(lt.trade(trig, book_, 0.2, False)["price"]) == [0.50]
    assert list(lt.trade(trig, book_, 1.0, False)["price"]) == [0.70]


def test_xtop_rows_are_stamped_per_message():
    """One websocket frame with two price changes stamped 100 ms apart gives two rows, each with
    its own exchange time, rather than one row stamped with the frame's last time."""
    frame = [{**book("u", 0.40, 0.60), "timestamp": "1000000"},
             {"event_type": "price_change", "timestamp": "1000100", "price_changes": [
                 {"asset_id": "u", "price": "0.55", "size": "10", "side": "SELL"}]},
             {"event_type": "price_change", "timestamp": "1000200", "price_changes": [
                 {"asset_id": "u", "price": "0.45", "size": "5", "side": "BUY"}]}]
    rows = [r for k, r in rc.book_events([{"recv_ms": 1_000_300, "msg": frame}]) if k == "xtop"]
    assert [r["ts"] for r in rows] == [1000.0, 1000.1, 1000.2]
    assert [r["top"][:2] for r in rows] == [(0.40, 0.60), (0.40, 0.55), (0.45, 0.55)]


def test_record_outcomes_uses_prefetched_records_and_unsubscribes(tmp_path):
    """With lookups done off the event loop, record_outcomes only uses what was fetched (a failed
    lookup is retried later, a slug not fetched waits), and a resolved market's tokens leave the
    resubscription list."""
    now = int(time.time())
    a, b, c = (now // 300 * 300 - 600 - 300 * i for i in range(3))
    calls = []
    trader = pt.LiveTrader(tmp_path, P, fetch=lambda url: calls.append(url) or [])
    for s in (a, b, c):
        mk = pt.parse_gamma_market(gamma(s))
        trader.markets[mk["slug"]] = mk
        trader.subscribed.update((mk["up_token"], mk["down_token"]))
    assert sorted(trader.due_outcomes()) == sorted(f"btc-updown-5m-{s}" for s in (a, b, c))
    trader.record_outcomes({f"btc-updown-5m-{a}": gamma(a, True), f"btc-updown-5m-{b}": TimeoutError("gamma")})
    trader.rec.close()
    assert not calls
    assert f"btc-updown-5m-{a}" not in trader.markets and not {f"up{a}", f"dn{a}"} & trader.subscribed
    assert trader.markets[f"btc-updown-5m-{b}"]["retry_at"] > now and f"up{b}" in trader.subscribed
    assert "retry_at" not in trader.markets[f"btc-updown-5m-{c}"]


def test_latency_files_keep_the_clob_disconnects(recorded, tmp_path):
    import gzip as gz
    import shutil
    out = tmp_path / "paper"
    shutil.copytree(recorded[0], out)  # the fixture is shared: add the disconnects to a copy
    day = sorted((out / "raw").iterdir())[0]
    with gz.open(day / "errors.jsonl.gz", "at") as f:
        f.write(json.dumps({"at": S0 * 1000 + 150_250, "where": "clob", "err": "ConnectionClosedError(1013)"}) + "\n")
        f.write(json.dumps({"at": S0 * 1000 + 152_900, "where": "clob-open", "tokens": 4}) + "\n")
    counts = rc.build(out, tmp_path / "bundle")
    assert counts["latency_clob_closes"] == 1
    import latency as lt
    assert lt.load_closes(tmp_path / "bundle" / "latency").tolist() == [S0 + 150.25]


def test_binance_trades_reach_the_latency_files(recorded, tmp_path):
    import shutil
    out = tmp_path / "paper"
    shutil.copytree(recorded[0], out)
    trader = pt.LiveTrader(out, P, fetch=lambda url: [])
    for i, (sym, px) in enumerate((("BTCUSDT", 80000.5), ("ETHUSDT", 3000.1), ("BTCUSDT", 80001.0))):
        trader.on_binance(json.dumps({"stream": f"{sym.lower()}@aggTrade", "data": {
            "e": "aggTrade", "s": sym, "p": str(px), "q": "0.01", "T": (S0 + 100) * 1000 + i, "m": False}}),
            (S0 + 100) * 1000 + 50 + i)
    trader.rec.close()
    counts = rc.build(out, tmp_path / "bundle")
    assert counts["latency_binance_ws"] == 2
    import gzip as gz
    rows = [json.loads(line) for line in gz.open(
        tmp_path / "bundle" / "latency" / "binance_trades.jsonl.gz", "rt",
    )]
    assert [r["price"] for r in rows] == [80000.5, 80001.0] and rows[0]["trade_ts"] == S0 + 100


def test_strict_bundle_keeps_bookticker_dual_token_l2_and_connection_epochs(tmp_path):
    import h_replay_archive as archive

    trader = pt.LiveTrader(tmp_path, P, fetch=lambda url: [])
    market = {**gamma(S0, up_won=True), "conditionId": "0xstrict"}
    parsed = pt.parse_gamma_market(market)
    trader.rec.write("markets", market)
    trader.markets[parsed["slug"]] = parsed
    trader.token_coin.update({parsed["up_token"]: "btc", parsed["down_token"]: "btc"})
    trader.subscribed.update((parsed["up_token"], parsed["down_token"]))

    trader.record_clob_open(S0 * 1000)
    trader.on_clob(json.dumps([
        {**book(parsed["up_token"], 0.32, 0.33), "market": "0xstrict", "timestamp": str(S0 * 1000 + 10)},
        {**book(parsed["down_token"], 0.66, 0.67), "market": "0xstrict", "timestamp": str(S0 * 1000 + 11)},
    ]), S0 * 1000 + 20.25)
    trader.on_clob(json.dumps({
        "event_type": "price_change", "market": "0xstrict", "timestamp": str(S0 * 1000 + 30),
        "price_changes": [
            {"asset_id": parsed["up_token"], "price": "0.34", "size": "7", "side": "SELL",
             "best_bid": "0.32", "best_ask": "0.33"},
            {"asset_id": parsed["down_token"], "price": "0.65", "size": "6", "side": "BUY",
             "best_bid": "0.66", "best_ask": "0.67"},
        ],
    }), S0 * 1000 + 40.5)
    trader.record_clob_close("closed 1000", S0 * 1000 + 50)
    trader.record_binance_open("spot", S0 * 1000 + 4)
    trader.record_binance_open("spot", S0 * 1000 + 4.1, conn_id=1)
    trader.record_binance_open("spot", S0 * 1000 + 4.2, conn_id=2)
    trader.on_binance(json.dumps({"stream": "btcusdt@trade", "data": {
        "e": "trade", "s": "BTCUSDT", "T": S0 * 1000 + 5, "p": "80000.5", "q": "0.01", "m": False,
    }}), S0 * 1000 + 6.25)
    trader.on_binance(json.dumps({"stream": "btcusdt@bookTicker", "data": {
        "u": 9, "s": "BTCUSDT", "b": "80000.4", "B": "2", "a": "80000.6", "A": "3",
    }}), S0 * 1000 + 7.5)
    trader.record_binance_close("spot", "closed 1000", S0 * 1000 + 7.8, conn_id=1)
    trader.record_binance_close("spot", "closed 1000", S0 * 1000 + 7.9, conn_id=2)
    trader.record_binance_close("spot", "closed 1000", S0 * 1000 + 8)
    trader.record_binance_open("futures", S0 * 1000 + 8.5)
    trader.record_binance_open("futures", S0 * 1000 + 8.6, conn_id=1)
    trader.on_binance_futures(json.dumps({
        "e": "bookTicker", "E": S0 * 1000 + 6, "T": S0 * 1000 + 5, "u": 10, "s": "BTCUSDT",
        "b": "80000.45", "B": "2", "a": "80000.55", "A": "3",
    }), S0 * 1000 + 9.25)
    trader.record_binance_close("futures", "closed 1000", S0 * 1000 + 9.9, conn_id=1)
    trader.record_binance_close("futures", "closed 1000", S0 * 1000 + 10)
    trader.record_binance_open("futures_trade", S0 * 1000 + 10.5)
    trader.record_binance_open("futures_trade", S0 * 1000 + 10.6, conn_id=1)
    trader.on_binance_futures_trade(json.dumps({
        "e": "trade", "E": S0 * 1000 + 8, "T": S0 * 1000 + 7, "t": 11,
        "s": "BTCUSDT", "p": "80000.5", "q": "0.02", "m": False,
    }), S0 * 1000 + 11.25)
    trader.record_binance_close("futures_trade", "closed 1000", S0 * 1000 + 11.9, conn_id=1)
    trader.record_binance_close("futures_trade", "closed 1000", S0 * 1000 + 12)
    trader.record_binance_open("deribit", S0 * 1000 + 12.5)
    trader.on_deribit_quote(json.dumps({
        "jsonrpc": "2.0", "method": "subscription", "params": {
            "channel": "quote.BTC-PERPETUAL", "data": {
                "timestamp": S0 * 1000 + 9, "best_bid_price": 80000.4,
                "best_ask_price": 80000.6, "best_bid_amount": 4, "best_ask_amount": 5,
            },
        },
    }), S0 * 1000 + 13.25)
    trader.on_deribit_quote(json.dumps({
        "jsonrpc": "2.0", "method": "subscription", "params": {
            "channel": "deribit_volatility_index.btc_usd", "data": {
                "timestamp": S0 * 1000 + 10, "volatility": 58.25,
                "index_name": "btc_usd",
            },
        },
    }), S0 * 1000 + 13.5)
    trader.record_binance_close("deribit", "closed 1000", S0 * 1000 + 14)
    trader.rec.close()

    incomplete = rc.build(tmp_path, tmp_path / "bundle")
    assert incomplete["strict_ready"] is False
    assert incomplete["strict_failure_reasons"] == ["recorder_complete"]
    assert incomplete["receipt_race_failure_reasons"] == ["strict_complete"]
    trader.record_complete(S0 * 1000 + 60)
    trader.rec.close()
    counts = rc.build(tmp_path, tmp_path / "bundle")
    strict = tmp_path / "bundle" / "strict"
    assert counts["strict_ready"] is True
    manifest = json.loads((strict / "manifest.json").read_text())
    assert manifest["schema"] == "polymarket-5m-strict-replay-v2"
    assert manifest["complete"] is True
    assert manifest["receipt_race_ready"] is True
    assert manifest["failure_reasons"] == []
    assert manifest["receipt_race_failure_reasons"] == []
    assert all(manifest["completeness_checks"].values())
    assert all(manifest["receipt_race_checks"].values())
    assert counts["receipt_race_ready"] is True
    assert manifest["counts"]["spot_bbo"] == 1
    assert manifest["counts"]["futures_bbo"] == 1
    assert manifest["counts"]["futures_trade"] == 1
    assert manifest["counts"]["deribit_quote"] == 1
    assert manifest["counts"]["deribit_dvol"] == 1
    assert manifest["counts"]["clob_snapshot"] == 2
    assert manifest["counts"]["clob_price_change"] == 2
    assert archive.validate_standard_artifact(strict)["counts"] == manifest["counts"]

    source = list(archive.iter_normalized_events(strict / "source_events.jsonl.gz", family="source"))
    assert [row["kind"] for row in source] == [
        "spot_connection", "spot_trade", "spot_bbo", "spot_disconnect",
        "futures_connection", "futures_bbo", "futures_disconnect",
        "futures_trade_connection", "futures_trade", "futures_trade_disconnect",
        "deribit_connection", "deribit_quote", "deribit_dvol", "deribit_disconnect",
    ]
    assert source[2]["source_ts_ms"] is None and source[2]["bid"] == 80000.4
    assert source[5]["source_ts_ms"] == S0 * 1000 + 5
    assert source[8]["source_ts_ms"] == S0 * 1000 + 7
    assert source[11]["source_ts_ms"] == S0 * 1000 + 9
    assert source[12]["source_ts_ms"] == S0 * 1000 + 10
    assert source[12]["volatility"] == 58.25

    clob = list(archive.iter_normalized_events(strict / "clob_events.jsonl.gz", family="clob"))
    assert [row["kind"] for row in clob] == [
        "clob_connection", "clob_snapshot", "clob_snapshot",
        "clob_price_change", "clob_price_change", "clob_error",
    ]
    assert {row["asset_id"] for row in clob if "asset_id" in row} == {
        parsed["up_token"], parsed["down_token"],
    }
    assert {row["connection_epoch"] for row in clob} == {1}
    snapshots = [row for row in clob if row["kind"] == "clob_snapshot"]
    assert snapshots[0]["asks"][0]["size"] == "150"

    mappings = list(archive.iter_market_mappings(strict / "market_registry.csv.gz"))
    outcomes = list(archive.iter_outcomes(strict / "market_outcomes.csv.gz"))
    assert mappings[0]["market_id"] == outcomes[0]["market_id"] == "0xstrict"
    assert mappings[0]["up_token_id"] == parsed["up_token"] and outcomes[0]["winner"] == "Up"
    assert mappings[0]["fee_rate"] == 0.07


def test_strict_json_reader_rejects_a_partial_line(tmp_path):
    path = tmp_path / "broken.jsonl.gz"
    with gzip.open(path, "wt") as stream:
        stream.write('{"ok":1}\n{"partial":')
    with pytest.raises(ValueError, match="invalid JSON"):
        list(rc.iter_jsonl_strict([path]))


def test_strict_streams_reject_missing_receipt_timestamps(tmp_path):
    with pytest.raises(ValueError, match="missing recv_ms"):
        list(rc._strict_spot_events([{"s": "BTCUSDT", "kind": "bookTicker", "b": "1", "a": "2"}],
                                    "BTCUSDT"))

    day = tmp_path / "raw" / "2026-10-04"
    day.mkdir(parents=True)
    with gzip.open(day / "clob-btc.jsonl.gz", "wt") as stream:
        stream.write(json.dumps({"sequence": 1, "connection_epoch": 1, "msg": {}}) + "\n")
    with pytest.raises(ValueError, match="missing recv_ms"):
        list(rc._strict_clob_inputs(tmp_path, "btc"))


def test_strict_clob_integrity_rejects_duplicate_source_sequence(tmp_path):
    day = tmp_path / "raw" / "2026-10-04"
    day.mkdir(parents=True)
    with gzip.open(day / "errors.jsonl.gz", "wt") as stream:
        stream.write(json.dumps({"where": "clob-open", "at": 1, "sequence": 1,
                                 "connection_epoch": 1}) + "\n")
        stream.write(json.dumps({"where": "clob", "at": 4, "sequence": 3,
                                 "connection_epoch": 1, "connection_active": True}) + "\n")
    with gzip.open(day / "clob-btc.jsonl.gz", "wt") as stream:
        stream.write(json.dumps({"recv_ms": 2, "sequence": 1, "connection_epoch": 1,
                                 "msg": {"event_type": "ignored"}}) + "\n")
    rows = list(rc._strict_clob_events(tmp_path, "btc", {}))
    assert rows[-1][1]["source_sequence_regressions"] == 1
