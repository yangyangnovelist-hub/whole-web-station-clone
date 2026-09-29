import json

import numpy as np
import pandas as pd
import pytest

import binary as bo
import strategy_zoo as sz
from test_real_day import S0, make_bundle, write_gz


@pytest.fixture(scope="module")
def markets(tmp_path_factory):
    root = make_bundle(tmp_path_factory.mktemp("zoo") / "polymarket-data-samples")
    # Market 0 quotes Up 0.50/0.51 until t=200. A taker sells Up at 0.40 at t=100
    # (through the 0.50 bid) and at exactly 0.50 at t=150 (not through it).
    prints = [("up0", (S0 + 100) * 1000, "SELL", 0.40), ("up0", (S0 + 150) * 1000, "SELL", 0.50)]
    write_gz(root / "data/polymarket/daily/last_trade_price/BTC-5m/BTC-5m-last_trade_price-2026-09-08.jsonl.gz",
             "\n".join(json.dumps({"asset_id": tok, "recv_ms": ms, "payload": {"side": side, "size": "10",
                                                                                "price": f"{px:.2f}"}})
                       for tok, ms, side, px in prints))
    return sz.build_markets(root)


def test_registry_has_100_distinct_strategies():
    reg = sz.registry()
    assert len(reg) == 100
    assert len({s.name for s in reg}) == 100
    assert len({s.family for s in reg}) == 10


def test_fair_price_pvalue_is_honest_on_all_win_samples():
    # 27 straight wins at an all-in cost of 0.919: luck does that 0.919**27 ~ 10% of the time.
    assert bo.fair_price_pvalue(np.full(27, 1 - 0.919), np.full(27, 0.919)) == pytest.approx(0.919**27, abs=0.01)
    # Trades drawn from the null itself should not look significant on average.
    rng = np.random.default_rng(1)
    cost = rng.uniform(0.1, 0.9, 400)
    ps = [bo.fair_price_pvalue((rng.random(400) < cost) - cost, cost, sims=2000, seed=k) for k in range(20)]
    assert 0.3 < np.mean(ps) < 0.7
    assert bo.fair_price_pvalue([], []) == 1.0


def test_benjamini_hochberg():
    passed = sz.benjamini_hochberg([0.001, 0.02, 0.04, 0.5], q=0.10)
    assert passed.tolist() == [True, True, True, False]
    assert not sz.benjamini_hochberg([0.2, 0.5, 0.9]).any()


def test_late_favourite_buys_the_quoted_winner(markets):
    trade = sz.late_favourite(30, 0.85, 0.99)
    fills = [trade(m) for m in markets]
    assert all(f is not None for f in fills)
    for m, f in zip(markets, fills):
        assert (f["side"] == "Up") == m.up_won and f["t"] == bo.WINDOW_S - 30
        assert f["fee"] == pytest.approx(bo.taker_fee(f["price"]))
    # Nothing is inside the band before the synthetic book moves at t=200.
    assert all(sz.late_favourite(180, 0.85, 0.99)(m) is None for m in markets)


def test_maker_fills_only_when_a_print_trades_through(markets):
    m0 = markets[0]
    assert m0.at(60)["bid_up"] == 0.50 and m0.at(60)["mid"] >= 0.5  # Up is the (tied) favourite
    fill = sz.maker_bid(240, "favourite")(m0)  # rests at t=60, the 0.40 print goes through
    assert fill == {"side": "Up", "price": 0.50, "fee": 0.0, "t": 60, "kind": "maker"}
    assert sz.maker_bid(160, "favourite")(m0) is None  # rests at t=140, only the 0.50 print follows
    assert sz.maker_bid(240, "favourite")(markets[1]) is None  # no prints at all


def test_evaluate_and_report(markets, tmp_path):
    res, trades = sz.evaluate(markets, sz.registry(), reps=200)
    assert len(res) == 100 and set(trades["strategy"]) <= set(range(100))
    assert (trades.groupby(["strategy", "slug"]).size() == 1).all()  # at most one trade per market
    won = trades["won"].astype(float)
    assert np.allclose(trades["pnl"], won - trades["price"] - trades["fee"])
    # Two markets are far too few to test: every p is 1.
    assert (res["p"] == 1.0).all() and not res["bh"].any()
    text = sz.report(res, markets, 200)
    assert "## 全部 100 个" in text and "可检验的：0" in text


def test_preregistered_candidates_are_pinned():
    # The forward test is only honest if these never change meaning after forward data arrives.
    reg = sz.registry()
    assert {i: reg[i - 1].name for i in sz.PREREGISTERED} == {
        61: "Chainlink 动量 τ=240 回看10s z>1", 10: "强势方 τ=30 [0.80,0.97]", 9: "强势方 τ=30 [0.70,0.97]",
        11: "强势方 τ=30 [0.85,0.99]", 65: "mid 30s 变动>0.10 跟随 τ=60", 62: "Chainlink 动量 τ=240 回看10s z>2",
        80: "盘口失衡>0.6 τ=120"}


def test_split_holdout_selects_on_one_half_and_tests_on_the_other():
    rng = np.random.default_rng(0)
    rows, starts = [], [S0 + 300 * i for i in range(40)]
    for i, start in enumerate(starts):
        # Strategy 0 always wins at 0.50; strategy 1 is a fair coin at 0.50; strategy 2 wins only early.
        for j, won in ((0, True), (1, bool(rng.random() < 0.5)), (2, i < 20)):
            fee = float(bo.taker_fee(0.5))
            rows.append({"strategy": j, "start": start, "won": won, "price": 0.5, "fee": fee,
                         "pnl": float(won) - 0.5 - fee})
    markets = [sz.Market(str(s), s, s + 300, True, None, "u", "d", None, None, {}, {}) for s in starts]
    ho = sz.split_holdout(pd.DataFrame(rows), markets, 3, k=2, reps=2000)
    assert len(ho) == 4 and set(ho["direction"]) == {"前半选 → 后半验", "后半选 → 前半验"}
    assert ho[ho["id"] == 1]["passed"].all()
    early = ho[(ho["id"] == 3) & (ho["direction"] == "前半选 → 后半验")]
    assert len(early) == 1 and not early["passed"].iloc[0] and early["ev_test"].iloc[0] < 0


def test_maker_bid_respects_its_price_band(markets):
    m0 = markets[0]
    assert sz.maker_bid(240, "favourite", 0.60, 0.80)(m0) is None  # the 0.50 bid is outside the band
    assert sz.maker_bid(240, "favourite", 0.45, 0.55)(m0)["price"] == 0.50
    assert set(sz.EXTRA) == {101, 102, 103, 104, 105, 106, 107, 108}


def test_queue_maker_waits_for_the_size_ahead():
    start = 1_000_000
    rows = pd.DataFrame({"t": range(300), "mid": 0.70}).set_index("t")
    book_ms = np.array([(start + 250) * 1000])
    books = {"up": (book_ms, [([(0.69, 30.0), (0.68, 100.0)], [(0.71, 50.0)])])}

    def market(prints):
        trades = {"up": pd.DataFrame(prints, columns=["recv_ms", "side", "size", "price"])}
        return sz.Market("s", start, start + 300, True, None, "up", "down", rows, pd.Series(dtype=float), books, trades)

    rule = sz.maker_queue(45, "favourite", 0.60, 0.80)  # joins at t=255 behind 30 shares at 0.69
    at = lambda s: (start + s) * 1000
    assert rule(market([(at(260), "SELL", 20.0, 0.69)])) is None                      # 20 < 30 ahead
    assert rule(market([(at(260), "SELL", 20.0, 0.69), (at(270), "SELL", 15.0, 0.69)]))["price"] == 0.69
    assert rule(market([(at(260), "SELL", 1.0, 0.68)]))["kind"] == "maker"           # traded through
    assert rule(market([(at(260), "BUY", 50.0, 0.69)])) is None                       # buys do not fill a bid
