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
    return out, spot, won


def test_record_outcomes_keeps_unresolved_markets(tmp_path):
    now = int(time.time())
    start = now // 300 * 300 - 600  # ended between 5 and 10 minutes ago
    trader = pt.LiveTrader(tmp_path, P, fetch=lambda url: [gamma(start)])
    trader.markets[f"btc-updown-5m-{start}"] = pt.parse_gamma_market(gamma(start))
    trader.record_outcomes()
    assert trader.markets[f"btc-updown-5m-{start}"]["retry_at"] > now
    assert not list(tmp_path.glob("raw/*/markets.jsonl"))


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
    asked = []

    def fetch(url):
        asked.append(url)
        return [gamma(S0, up_won=False)]

    assert rc.fetch_outcomes(tmp_path, fetch=fetch) == 0
    assert asked == [pt.GAMMA_CLOSED.format(slug=f"btc-updown-5m-{S0}")]
    latest = [json.loads(line) for line in next(tmp_path.glob("raw/*/markets.jsonl")).read_text().splitlines()]
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
    rec.write("clob", {"recv_ms": S0 * 1000, "msg": book(f"up{S0}", 0.4, 0.6)})
    rc.build(tmp_path, tmp_path / "bundle")
    assert sz.build_markets(tmp_path / "bundle") == []
    sz.main([str(tmp_path / "bundle"), "--confirm", "--trades-out", str(tmp_path / "t.csv")])
    assert pd.read_csv(tmp_path / "t.csv").empty
