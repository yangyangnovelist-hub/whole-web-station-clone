"""Tests for the forward-testing kit (bot/). No network needed."""
import gzip
import json
import math
import time
from types import SimpleNamespace

import numpy as np
import pytest

import binary as bo
from bot import evaluate as ev
from bot import replay
from bot.book import Ladder
from bot.events import Book, Market, Quote, Spot, Tick, Trade, Twap, parse_clob
from bot.live_exec import gate_allows
from bot.paper import PaperBroker
from bot.recorder import Recorder
from bot.strategy import VARIANTS, Chainlink, Engine, Variant

UP, DN = "tok-up", "tok-down"
START = 1_800_000_000
END = START + 300


def market(**kw):
    return Market(slug=f"btc-updown-5m-{START}", start=START, end=END, up_token=UP, down_token=DN,
                  condition_id="0xabc", strike=100_000.0, **kw)


def ms_at(tau):
    return int((END - tau) * 1000)


# ------------------------------------------------------------------ book

def test_ladder_levels_trim_and_walk():
    b = Ladder()
    b.snapshot([(0.80, 50), (0.79, 10)], [(0.83, 5), (0.84, 20), (0.90, 100)], 0)
    assert (b.best_bid(), b.best_ask()) == (0.80, 0.83)
    b.level("SELL", 0.83, 0, 1)                 # level emptied
    b.level("BUY", 0.81, 7, 2)                  # new best bid
    assert (b.best_bid(), b.best_ask()) == (0.81, 0.84)
    assert b.take_asks(0.85, 30) == [(0.84, 20)]
    assert b.take_asks(0.95, 30) == [(0.84, 20), (0.90, 10)]
    b.trim(0.80, 0.90, 3)                       # quote says 0.84 ask and 0.81 bid are gone
    assert (b.best_bid(), b.best_ask()) == (0.80, 0.90)
    assert b.size_at("BUY", 0.80) == 50


def test_parse_clob_matches_sample_payloads():
    pc = {"event_type": "price_change", "market": "0xabc", "price_changes": [
        {"asset_id": UP, "side": "BUY", "price": "0.3", "size": "25", "best_bid": "0.53", "best_ask": "0.57"},
        {"asset_id": DN, "side": "SELL", "price": "0.7", "size": "0", "best_bid": "0.43", "best_ask": "0.47"}]}
    evs = parse_clob(pc, 5)
    assert [(e.token, e.side, e.price, e.size) for e in evs] == [(UP, "BUY", 0.3, 25.0), (DN, "SELL", 0.7, 0.0)]
    q = parse_clob({"event_type": "best_bid_ask", "asset_id": UP, "best_bid": "0.44", "best_ask": ""}, 1)[0]
    assert (q.bid, q.ask) == (0.44, None)
    t = parse_clob({"event_type": "last_trade_price", "asset_id": UP, "side": "SELL", "price": "0.9", "size": "3"}, 2)[0]
    assert isinstance(t, Trade) and t.side == "SELL" and t.size == 3.0
    assert parse_clob({"event_type": "tick_size_change"}, 0) == []


# ------------------------------------------------------------- chainlink

def test_chainlink_vol_forward_fills_and_matches_constant_drift():
    cl = Chainlink(halflife_s=60, k=15, warmup_s=10)
    step = 1e-4
    for s in range(0, 200):
        if s % 7 == 3:
            continue                            # gaps are forward-filled
        cl.add(s, 100_000 * math.exp(step * s))
    # 15s returns with forward-filled holes average close to 15*step
    assert cl.sigma_s() == pytest.approx(math.sqrt(15) * step, rel=0.15)
    cl.add(151, 1.0)                            # a late tick never rewrites history
    assert cl.px[151] == pytest.approx(math.log(100_000) + step * 151)


def test_prob_up_uses_twap_window_sum():
    m = market()
    cl = Chainlink(warmup_s=0)
    for s in range(START - 1000, END - 20):     # flat at strike, then +$50 inside the TWAP window
        cl.add(s, 100_000.0 if s < END - 60 else 100_050.0)
    cl.var, cl.n = (0.3 / math.sqrt(bo.SECONDS_PER_YEAR)) ** 2, 10_000
    q = cl.prob_up(m)
    first = END - 59
    known = sum(cl.px[s] for s in range(first, END - 20))
    expect = bo.prob_up(cl.px[END - 21], math.log(100_000.0), cl.sigma_s(), 300 - 21, known)
    assert q == pytest.approx(float(expect)) and q > 0.99


# ------------------------------------------------------ engine + broker

class Sink(list):
    def __call__(self, rec):
        self.append(rec)


def engine_with(variants, latency_ms=0, **mk):
    sink = Sink()
    e = Engine(PaperBroker(sink, latency_ms=latency_ms), variants=variants)
    e.add_market(market(**mk))
    return e, sink


def book_events(t_ms, up_bids, up_asks):
    """Mirror an Up book into the Down book, as Polymarket does."""
    dn_bids = [(round(1 - p, 6), s) for p, s in up_asks]
    dn_asks = [(round(1 - p, 6), s) for p, s in up_bids]
    return [Book(t_ms, UP, up_bids, up_asks), Book(t_ms, DN, dn_bids, dn_asks)]


TAKER = Variant("t", "taker", 35, 25, 0.80, 0.97, size=10)


def test_taker_fills_at_ask_and_settles_with_fee():
    e, sink = engine_with([TAKER], up_won=True)
    for x in book_events(ms_at(40), [(0.86, 100)], [(0.88, 100)]):
        e.on_event(x)                           # tau 40: outside the window, nothing yet
    assert not e.broker.orders
    e.on_event(Tick(ms_at(34)))
    e.on_event(Tick(ms_at(33)))
    e.settle_ready(END * 1000 + 10_000)
    (r,) = sink
    fee = 10 * 0.07 * 0.88 * 0.12
    assert r["outcome"] == "Up" and r["filled"] == 10 and r["avg_price"] == 0.88
    assert r["pnl"] == pytest.approx(10 - 8.8 - fee)
    assert r["tau"] == pytest.approx(34)


def test_taker_misses_when_ask_moves_away_within_latency():
    e, sink = engine_with([TAKER], latency_ms=250, up_won=True)
    for x in book_events(ms_at(31), [(0.86, 100)], [(0.88, 100)]):
        e.on_event(x)
    for x in book_events(ms_at(31) + 100, [(0.90, 100)], [(0.92, 100)]):
        e.on_event(x)                           # ask jumps before our order lands
    e.on_event(Tick(ms_at(31) + 300))
    e.settle_ready(END * 1000 + 10_000)
    (r,) = sink
    assert r["status"] == "missed" and r["filled"] == 0 and r["pnl"] == 0


def test_down_favourite_and_band():
    e, sink = engine_with([TAKER], up_won=False)
    for x in book_events(ms_at(30), [(0.10, 100)], [(0.12, 100)]):   # Down ask 0.90
        e.on_event(x)
    e.on_event(Tick(ms_at(29)))
    e.settle_ready(END * 1000 + 10_000)
    (r,) = sink
    assert r["outcome"] == "Down" and r["avg_price"] == pytest.approx(0.90) and r["won"]


MAKER = Variant("m", "maker", 60, 10, 0.85, 0.97, size=10, cancel_tau=3)


def test_maker_queue_then_fill_from_both_books():
    e, sink = engine_with([MAKER], up_won=False)
    for x in book_events(ms_at(50), [(0.90, 30)], [(0.92, 50)]):
        e.on_event(x)                           # bid 0.90 on Up with 30 shares ahead of us
    e.on_event(Tick(ms_at(49)))
    e.on_event(Trade(ms_at(48), UP, "SELL", 0.90, 20))    # eats 20 of the 30 ahead
    e.on_event(Trade(ms_at(47), UP, "BUY", 0.92, 50))     # wrong side: ignored
    e.on_event(Trade(ms_at(46), DN, "BUY", 0.10, 15))     # complement buy at 0.10 == Up bid 0.90: 10 ahead, 5 to us
    (o,) = e.broker.orders[market().slug]
    assert o["filled"] == 5 and o["queue"] == 0
    e.on_event(Trade(ms_at(45), UP, "SELL", 0.89, 1))     # trades through: rest of ours fills
    assert o["filled"] == 10 and o["status"] == "filled"
    e.settle_ready(END * 1000 + 10_000)
    (r,) = sink
    assert r["fee"] == 0 and r["pnl"] == pytest.approx(-9.0) and r["queue_at_rest"] == 0


def test_maker_expires_unfilled():
    e, sink = engine_with([MAKER], up_won=True)
    for x in book_events(ms_at(50), [(0.90, 30)], [(0.92, 50)]):
        e.on_event(x)
    e.on_event(Tick(ms_at(49)))
    e.on_event(Tick(ms_at(2)))
    e.settle_ready(END * 1000 + 10_000)
    (r,) = sink
    assert r["status"] == "expired" and r["filled"] == 0


def test_halted_engine_places_nothing():
    e, sink = engine_with([TAKER], up_won=True)
    e.halted = True
    for x in book_events(ms_at(30), [(0.86, 100)], [(0.88, 100)]):
        e.on_event(x)
    e.on_event(Tick(ms_at(29)))
    assert not e.broker.orders


def test_strike_comes_from_twap_at_open():
    e, _ = engine_with([TAKER])
    m = e.markets[market().slug].meta
    m.strike = None
    e.on_event(Twap(0, START - 1, 99_000.0))
    e.on_event(Twap(0, START, 100_123.0))
    assert m.strike == 100_123.0


# --------------------------------------------------- recorder <-> replay

def test_recorder_roundtrip_through_replay(tmp_path):
    rec = Recorder(tmp_path)
    raw = {"slug": f"btc-updown-5m-{START}", "outcomes": '["Up", "Down"]', "clobTokenIds": json.dumps([UP, DN]),
           "conditionId": "0xabc", "closed": True, "outcomePrices": '["1", "0"]',
           "feeSchedule": {"rate": 0.07}, "orderMinSize": 5}
    m = market(up_won=True, raw=raw)
    rec.track(m)
    t0 = START * 1000
    rec.clob({"event_type": "book", "asset_id": UP, "market": "0xabc", "timestamp": str(t0),
              "bids": [{"price": "0.5", "size": "10"}], "asks": [{"price": "0.52", "size": "5"}]}, t0 + 5)
    rec.clob({"event_type": "price_change", "market": "0xabc", "timestamp": str(t0 + 10), "price_changes": [
        {"asset_id": UP, "side": "SELL", "price": "0.52", "size": "0", "best_bid": "0.5", "best_ask": ""}]}, t0 + 15)
    rec.price("spot", t0, 100_000.5, None, t0 + 900, t0 + 1600)
    rec.price("twap60", t0, 100_000.25, 100_000_250_000_000_000_000_000, None, t0 + 1700)
    rec.market(m, strike_full=100_000_250_000_000_000_000_000)
    rec.close()

    ms = replay.load_markets(tmp_path)
    (got,) = ms.values()
    assert got.up_token == UP and got.up_won is True and got.strike == pytest.approx(100_000.25)
    evs = [x for x in replay.events(tmp_path, tick_ms=10_000) if not isinstance(x, Tick)]
    kinds = [type(x).__name__ for x in evs]
    assert kinds == ["Book", "Level", "Spot", "Twap"]
    assert evs[1].token == UP and evs[1].size == 0
    assert evs[3].full == 100_000_250_000_000_000_000_000


def test_replay_survives_truncated_gzip(tmp_path):
    p = tmp_path / "polymarket/daily/best_bid_ask/BTC-5m/BTC-5m-best_bid_ask-2027-01-15.jsonl.gz"
    p.parent.mkdir(parents=True)
    line = json.dumps({"recv_ms": 1, "payload": {"event_type": "best_bid_ask", "asset_id": UP,
                                                 "best_bid": "0.4", "best_ask": "0.6"}}) + "\n"
    blob = gzip.compress((line * 2000).encode())
    p.write_bytes(blob[: len(blob) - 40])      # killed mid-write
    got = [x for x in replay.events(tmp_path) if isinstance(x, Quote)]
    assert 0 < len(got) <= 2000


# -------------------------------------------------------------- evaluate

def test_exact_p_is_calibrated_under_h0_and_sharp_under_edge():
    rng = np.random.default_rng(0)
    ps = []
    for _ in range(60):
        cost = rng.uniform(0.8, 0.97, 50)
        won = rng.random(50) < cost
        ps.append(ev.exact_p(np.ones(50), cost, won, sims=4000))
    assert 0.3 < np.mean(ps) < 0.7                     # roughly uniform under H0
    cost = np.full(300, 0.88)
    assert ev.exact_p(np.ones(300), cost, np.ones(300, bool), sims=20000) < 1e-3
    # 25 straight wins at ~0.915 is *not* significant, whatever a t-value says
    assert ev.exact_p(np.ones(25), np.full(25, 0.915 + 0.0055), np.ones(25, bool), sims=50000) > 0.05


def test_trades_needed():
    assert ev.trades_needed(0.88, 0.90, 0.01) == math.inf
    n = ev.trades_needed(0.95, 0.92, 0.01)
    assert 400 < n < 900


def _rows(n_days, per_day, win_p, cost, variant="fav_taker_30"):
    rng = np.random.default_rng(1)
    rows = []
    for d in range(n_days):
        for i in range(per_day):
            won = bool(rng.random() < win_p)
            rows.append(dict(variant=variant, slug=f"s{d}-{i}", day=f"2027-01-{d + 1:02d}", end=d * 1000 + i,
                             filled=10.0, cost=10 * cost, fee=0.0, won=won))
    return rows


def test_gate_go_and_no_go(tmp_path):
    good = tmp_path / "good.jsonl"
    good.write_text("\n".join(json.dumps(r) for r in _rows(10, 40, 0.97, 0.88)))
    res = ev.evaluate([str(good)])
    assert res["variants"]["fav_taker_30"]["verdict"] == "GO"
    assert res["variants"]["fav_taker_60"]["verdict"] == "NO-GO"      # no data
    fair = tmp_path / "fair.jsonl"
    fair.write_text("\n".join(json.dumps(r) for r in _rows(10, 40, 0.88, 0.88)))
    assert ev.evaluate([str(fair)])["variants"]["fav_taker_30"]["verdict"] == "NO-GO"
    # the in-sample day never counts unless asked
    ins = tmp_path / "ins.jsonl"
    ins.write_text("\n".join(json.dumps(dict(r, day="2026-09-08")) for r in _rows(1, 40, 0.97, 0.88)))
    assert ev.evaluate([str(ins)])["days"] == []

    gate = tmp_path / "gate.json"
    gate.write_text(json.dumps(res))
    assert gate_allows(gate, "fav_taker_30")[0]
    assert not gate_allows(gate, "fav_taker_60")[0]
    assert not gate_allows(gate, "fav_taker_30", now=time.time() + 30 * 86400)[0]
    gate.write_text(json.dumps(dict(res, include_in_sample=True)))
    assert not gate_allows(gate, "fav_taker_30")[0]


def test_variants_are_preregistered():
    names = [v.name for v in VARIANTS]
    assert len(names) == len(set(names)) == 5


# ----------------------------------------------------------- live parse

def test_live_message_handlers(tmp_path):
    from bot.live import Live

    args = SimpleNamespace(data=str(tmp_path / "data"), log=str(tmp_path / "paper.jsonl"), latency_ms=0)
    live = Live(args, "paper")
    seen = []
    live.engine.on_event = seen.append
    live.on_rtds(json.dumps({"topic": "crypto_prices_chainlink", "type": "update", "timestamp": 5,
                             "payload": {"symbol": "btc/usd", "timestamp": 1_800_000_001_000, "value": 100_000.5}}), 7)
    live.on_rtds(json.dumps({"topic": "crypto_prices_chainlink", "type": "subscribe", "timestamp": 5,
                             "payload": {"symbol": "btc/usd", "data": [{"timestamp": 1_800_000_000_000, "value": 99_999}]}}), 7)
    live.on_rtds(json.dumps({"topic": "crypto_prices_chainlink", "type": "update", "timestamp": 5,
                             "payload": {"symbol": "eth/usd", "timestamp": 1_800_000_001_000, "value": 3000}}), 7)
    live.on_rtds(json.dumps({"topic": "crypto_prices_twap_sixty", "type": "update", "timestamp": 5,
                             "payload": {"symbol": "btc/usd", "timestamp": 1_800_000_002_000, "value": "100000.25",
                                         "window_s": 60, "full_accuracy_value": "100000250000000000000000"}}), 8)
    live.on_clob(json.dumps([{"event_type": "best_bid_ask", "asset_id": UP, "best_bid": "0.4", "best_ask": "0.6"}]), 9)
    live.on_clob("not json", 9)
    assert [type(x).__name__ for x in seen] == ["Spot", "Spot", "Twap", "Quote"]
    assert seen[0].sec == 1_800_000_001 and seen[1].value == 99_999
    assert live.twap_full[1_800_000_002] == 100_000_250_000_000_000_000_000
    live.rec.close()
    live.sink.close()


def test_live_broker_guards_and_order_args(tmp_path):
    import asyncio

    from bot.live_exec import LiveBroker

    gate = tmp_path / "gate.json"
    gate.write_text(json.dumps(ev.evaluate([])))            # everything NO-GO
    kill = tmp_path / "STOP"
    args = SimpleNamespace(variant=["t"], gate=str(gate), log=str(tmp_path / "paper.jsonl"), max_shares=20,
                           max_daily_loss=5, max_orders_per_day=2, kill_file=str(kill))
    sent = []

    class FakeClient:
        async def place_market_order(self, **kw):
            sent.append(kw)
            return SimpleNamespace(ok=True, status="matched", order_id="o1",
                                   taking_amount="10", making_amount="8.8")

    async def scenario():
        b = LiveBroker(Sink(), args, latency_ms=0)
        b.client = FakeClient()
        e = Engine(b, variants=[TAKER])
        e.add_market(market(up_won=False))
        v, ms = TAKER, e.markets[market().slug]
        b._refresh_gate()
        assert b._blocked(v, 10) == "gate"
        b.allowed["t"] = True                                 # pretend the gate said GO
        kill.write_text("")
        assert b._blocked(v, 10) == "kill-file"
        kill.unlink()
        assert b._blocked(v, 21) == "max-shares"
        b.taker(ms, v, UP, 0.9, 10, ms_at(30), {"tau": 30})
        await asyncio.sleep(0)
        await asyncio.gather(*b.inflight)
        assert sent == [dict(token_id=UP, side="BUY", amount=9.0, max_price=0.9, order_type="FAK")]
        b.settle(ms)                                          # Up lost: -8.8 realised
        assert b.day_pnl == pytest.approx(-8.8)
        assert b._blocked(v, 10) == "max-daily-loss"
        b.real_sink.close()

    asyncio.run(scenario())
    lines = [json.loads(l) for l in (tmp_path / "real_orders.jsonl").read_text().splitlines()]
    assert [l["status"] for l in lines] == ["matched", "settled"] and lines[-1]["pnl"] == pytest.approx(-8.8)
