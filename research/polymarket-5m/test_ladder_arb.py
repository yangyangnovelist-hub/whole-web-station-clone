"""Tests for ladder_arb.py on synthetic books (no network)."""
import json
import urllib.error

import numpy as np
import pandas as pd
import pytest

import ladder_arb as la

NOON = 1791129600          # 2026-10-04 16:00 UTC = 12:00 ET (the above / range markets' end)
DAY0 = 1791086400          # 2026-10-04 04:00 UTC = 00:00 ET (the daily hit window's start)
T = NOON - 3600            # the synthetic recording starts an hour before noon
FEE = 0.07


def mk(cid, typ, lo=None, hi=None, end=NOON, start=None, ref=None, ref_ts=None, event="ev", day="2026-10-04"):
    return {"condition_id": cid, "slug": cid, "event_slug": event, "type": typ, "lo": lo, "hi": hi, "end_ts": end,
            "start_ts": start, "ref_ts": ref_ts, "ref_price": ref, "yes_token": cid + "Y", "no_token": cid + "N",
            "tick": 0.01, "fee_rate": FEE, "day": day, "neg_risk": typ == "range"}


def hit(cid, typ, level, start=DAY0, end=DAY0 + 86400):
    return mk(cid, typ, lo=level if typ == "hit_up" else None, hi=level if typ == "hit_down" else None,
              end=end, start=start, event="hit")


def side_rows(tok, pts):
    return [{"market_id": tok, "ts": float(t), "recv": float(t) + 0.01, "bid": b, "ask": a, "bid_size": bs,
             "ask_size": az, "tick": 0.01} for t, b, a, bs, az in pts]


def quotes(cid, pts):
    """Yes rows (t, bid, ask, bid_size, ask_size) and the mirrored No rows."""
    return side_rows(cid + "Y", pts) + side_rows(cid + "N", [(t, 1 - a, 1 - b, az, bs) for t, b, a, bs, az in pts])


def heartbeat(t0=T, t1=T + 60, step=1.0):
    return side_rows("hb", [(t, 0.5, 0.51, 10, 10) for t in np.arange(t0, t1 + step / 2, step)])


def books(*parts):
    return pd.DataFrame([r for p in parts for r in p])


def every(t0, t1, step, bid, ask, bs=100, az=100):
    return [(t, bid, ask, bs, az) for t in np.arange(t0, t1 + step / 2, step)]


def run(markets, bk, spot=None, closes=None, max_age=la.MAX_AGE):
    ms = la.prep_markets(markets, spot)
    bs = la.baskets(ms)
    summ, eps = la.scan_all(bs, la.Books(bk), la.Feed(bk, closes), max_age)
    return ms, bs, summ, eps


def find(bs, legs):
    want = {(c, s) for c, s in legs}
    got = [b for b in bs if {(m["condition_id"], s) for m, s in b.legs} == want]
    return got[0] if got else None


def eps_of(eps, legs):
    return [e for e in eps if set(e["tokens"]) == {c + s for c, s in legs}]


# ------------------------------------------------------------------ payoffs and baskets

def test_payoffs_follow_the_rules():
    a = la.prep_markets([mk("A", "above", lo=80000)])[0]
    r = la.prep_markets([mk("R", "range", lo=80000, hi=82000)])[0]
    top = la.prep_markets([mk("G", "range", lo=92000)])[0]
    x = np.array([79999.99, 80000.0, 80000.01, 82000.0, 92000.0])
    assert la.payoff(a, "Y", x).tolist() == [0, 0, 1, 1, 1]        # strictly above
    assert la.payoff(r, "Y", x).tolist() == [0, 1, 1, 0, 0]        # [a, b): the higher bracket on a bound
    assert la.payoff(top, "Y", x).tolist() == [0, 0, 0, 0, 1]      # ">92,000" is [92000, inf)
    u = la.prep_markets([mk("U", "updown_day", end=NOON, ref=80000.0, ref_ts=NOON - 86400)])[0]
    assert la.payoff(u, "Y", np.array([79999.0, 80000.0, 80001.0])).tolist() == [0, 0.5, 1]
    assert u["candle"] == NOON - 60                                  # the candle closing at noon


def test_above_ladder_basket_and_arb_after_fees():
    A1, A2 = mk("A1", "above", lo=80000), mk("A2", "above", lo=82000)
    # Yes(A1) ask 0.55, No(A2) ask = 1 - bid_yes(A2) = 0.40 -> 0.95 + fees 0.0341 < 1
    bk = books(heartbeat(), quotes("A1", every(T, T + 10, 1, 0.54, 0.55, 80, 50)),
               quotes("A2", every(T, T + 10, 1, 0.60, 0.61, 100, 100)),
               quotes("A2", every(T + 10.5, T + 60, 1, 0.56, 0.57)), quotes("A1", every(T + 10.5, T + 60, 1, 0.54, 0.55)))
    ms, bs, summ, eps = run([A1, A2], bk)
    b = find(bs, [("A1", "Y"), ("A2", "N")])
    assert b is not None and b.pay == 1 and not b.boundary and b.kind == "above-above"
    assert find(bs, [("A1", "N"), ("A2", "Y")]) is None              # the wrong direction is not a basket
    e = eps_of(eps, [("A1", "Y"), ("A2", "N")])
    assert len(e) == 1
    fee = FEE * (0.55 * 0.45 + 0.40 * 0.60)
    assert e[0]["edge"] == pytest.approx(1 - 0.95 - fee)
    assert e[0]["size"] == 50                                         # min(ask size of Yes A1, bid size of Yes A2)
    assert e[0]["take"] == pytest.approx(50 * (1 - 0.95 - fee))
    assert e[0]["later"] and e[0]["dur"] == pytest.approx(10.5, abs=0.02)   # gone when A2's bid drops at T + 10.5


def test_fees_remove_a_pre_fee_arb():
    A1, A2 = mk("A1", "above", lo=80000), mk("A2", "above", lo=82000)
    # 0.55 + 0.43 = 0.98 < 1 before fees, 1.0145 after
    bk = books(heartbeat(), quotes("A1", every(T, T + 60, 1, 0.54, 0.55)), quotes("A2", every(T, T + 60, 1, 0.57, 0.58)))
    _, bs, summ, eps = run([A1, A2], bk)
    assert not eps_of(eps, [("A1", "Y"), ("A2", "N")])
    best = summ.set_index("label").loc[find(bs, [("A1", "Y"), ("A2", "N")]).label(), "best"]
    assert best == pytest.approx(1 - 0.98 - FEE * (0.55 * 0.45 + 0.43 * 0.57))


def test_staleness_and_feed():
    A1, A2 = mk("A1", "above", lo=80000), mk("A2", "above", lo=82000)
    legs = quotes("A1", [(T, 0.54, 0.55, 50, 50)]) + quotes("A2", [(T, 0.60, 0.61, 100, 100)])   # one row each, never updated
    _, _, _, eps = run([A1, A2], books(heartbeat(T, T + 60), legs))
    e = eps_of(eps, [("A1", "Y"), ("A2", "N")])
    assert len(e) == 1 and e[0]["dur"] == pytest.approx(la.MAX_AGE, abs=0.05)   # stale after 5 s
    _, _, _, eps = run([A1, A2], books(heartbeat(T, T + 60), legs), max_age=np.inf)
    e = eps_of(eps, [("A1", "Y"), ("A2", "N")])
    assert len(e) == 1 and e[0]["dur"] == pytest.approx(60, abs=0.1)            # feed alive only: until the end
    # the feed goes quiet after T + 20: invalid QUIET s after the last row of any token
    _, _, _, eps = run([A1, A2], books(heartbeat(T, T + 20), legs, side_rows("hb", [(T + 60, 0.5, 0.51, 1, 1)])), max_age=np.inf)
    e = eps_of(eps, [("A1", "Y"), ("A2", "N")])
    assert e[0]["dur"] == pytest.approx(20 + la.QUIET, abs=0.1)
    # a CLOB disconnect after the legs' rows ends it too
    _, _, _, eps = run([A1, A2], books(heartbeat(T, T + 60), legs), closes=[T + 3.0], max_age=np.inf)
    e = eps_of(eps, [("A1", "Y"), ("A2", "N")])
    assert e[0]["dur"] == pytest.approx(3.0, abs=0.05)


def test_still_there_half_a_second_later():
    A1, A2 = mk("A1", "above", lo=80000), mk("A2", "above", lo=82000)
    base = heartbeat(T, T + 60, 0.1)
    gone = books(base, quotes("A1", every(T, T + 60, 1, 0.54, 0.55)), quotes("A2", [(T, 0.60, 0.61, 100, 100)]),
                 quotes("A2", every(T + 0.3, T + 60, 1, 0.50, 0.51)))
    _, _, _, eps = run([A1, A2], gone)
    e = eps_of(eps, [("A1", "Y"), ("A2", "N")])
    assert len(e) == 1 and not e[0]["later"] and e[0]["take5"] == 0
    stays = books(base, quotes("A1", every(T, T + 60, 0.2, 0.54, 0.55)), quotes("A2", every(T, T + 60, 0.2, 0.60, 0.61)))
    _, _, _, eps = run([A1, A2], stays)
    e = eps_of(eps, [("A1", "Y"), ("A2", "N")])
    assert len(e) == 1 and e[0]["later"] and e[0]["edge5"] == pytest.approx(e[0]["edge"])


def test_hit_ladders_and_touch():
    U1, U2 = hit("U1", "hit_up", 90000), hit("U2", "hit_up", 92000)
    D1, D2 = hit("D1", "hit_down", 80000), hit("D2", "hit_down", 78000)
    ms = la.prep_markets([U1, U2, D1, D2])
    bs = la.baskets(ms)
    assert find(bs, [("U1", "Y"), ("U2", "N")]).kind == "hit_up-hit_up"     # reaching 92k implies reaching 90k
    assert find(bs, [("U1", "N"), ("U2", "Y")]) is None
    assert find(bs, [("D1", "Y"), ("D2", "N")]).kind == "hit_down-hit_down"  # dipping to 78k implies 80k
    assert find(bs, [("D1", "N"), ("D2", "Y")]) is None
    # Yes(U1) 0.30 + No(U2) 0.65 -> 0.95 + fees 0.0306; Binance touches 90k at T + 5: U1 resolves, no longer used
    bk = books(heartbeat(), quotes("U1", every(T, T + 60, 1, 0.29, 0.30)), quotes("U2", every(T, T + 60, 1, 0.35, 0.36)))
    spot = pd.DataFrame({"trade_ts": [T - 1, T + 5.0, T + 6], "receive_ts": [T - 1, T + 5.0, T + 6],
                         "price": [89000.0, 90000.0, 89500.0]})
    ms, bs, summ, eps = run([U1, U2], bk, spot)
    assert next(m for m in ms if m["condition_id"] == "U1")["touch"] == T + 5.0
    e = eps_of(eps, [("U1", "Y"), ("U2", "N")])
    assert len(e) == 1 and e[0]["dur"] == pytest.approx(5.0, abs=0.05)
    assert e[0]["edge"] == pytest.approx(1 - 0.95 - FEE * (0.3 * 0.7 + 0.65 * 0.35))
    # a level the day's 1m high had reached before the recording is never used
    pre = dict(U1, pre_high=90500.0, pre_low=85000.0, pre_at=T - 100)
    ms = la.prep_markets([pre, U2])
    assert ms[0]["touch"] == -np.inf and not la.baskets(ms)


def test_above_vs_hit_needs_the_noon_candle_inside_the_window():
    A = mk("A", "above", lo=90000)
    day = hit("H", "hit_up", 89000)                          # 10-04 00:00 ET .. 10-05 00:00 ET contains noon
    bs = la.baskets(la.prep_markets([A, day]))
    b = find(bs, [("A", "N"), ("H", "Y")])                    # close > 90k at noon => High >= 89k that day
    assert b is not None and b.kind == "above-hit_up" and not b.boundary
    assert find(bs, [("A", "Y"), ("H", "Y")]) is None
    above_level = hit("H2", "hit_up", 91000)                  # a higher level is not implied
    assert find(la.baskets(la.prep_markets([A, above_level])), [("A", "N"), ("H2", "Y")]) is None
    late = hit("H3", "hit_up", 89000, start=NOON + 60, end=NOON + 86400)   # window opens after the candle
    assert not la.baskets(la.prep_markets([A, late]))
    edge = hit("H4", "hit_up", 89000, start=DAY0, end=NOON + 30)           # ends inside the candle
    assert not la.baskets(la.prep_markets([A, edge]))
    down = hit("L", "hit_down", 91000)                        # close <= 90k at noon => Low <= 91k that day
    b = find(la.baskets(la.prep_markets([A, down])), [("A", "Y"), ("L", "Y")])
    assert b is not None and b.kind == "above-hit_down"
    assert la.contains((DAY0, DAY0 + 86400), NOON) and not la.contains((NOON + 1, NOON + 86400), NOON)


def test_updown_day_uses_the_candle_closing_at_noon():
    U = mk("U", "updown_day", end=NOON, ref=88000.0, ref_ts=NOON - 86400)
    A = mk("A", "above", lo=87000)
    H = hit("H", "hit_up", 87500)
    bs = la.baskets(la.prep_markets([U, A, H]))
    assert not any({m["type"] for m, _ in b.legs} == {"above", "updown_day"} for b in bs)   # different candles
    b = find(bs, [("U", "N"), ("H", "Y")])                     # Up (close > 88k, 50-50 on a tie) => High >= 87.5k
    assert b is not None and b.kind == "hit_up-updown_day"
    no_ref = mk("V", "updown_day", end=NOON + 86400, ref=None, ref_ts=NOON)
    assert la.prep_markets([no_ref])[0]["candle"] is None      # reference unknown: no relation


def test_range_sums_and_partition_check():
    R = [mk("R1", "range", hi=80000), mk("R2", "range", lo=80000, hi=82000), mk("R3", "range", lo=82000)]
    bs = la.baskets(la.prep_markets(R))
    y = [b for b in bs if b.kind == "range_sum_yes"]
    n = [b for b in bs if b.kind == "range_sum_no"]
    assert len(y) == 1 and y[0].pay == 1 and len(n) == 1 and n[0].pay == 2
    assert not [b for b in la.baskets(la.prep_markets([R[0], R[2]])) if b.kind.startswith("range_sum")]   # a hole
    # all Yes at 0.30: 0.90 + 3 fees < 1; all No at 0.60 (Yes bids 0.40, sum 1.2 > 1): 1.80 + fees < 2
    bk = books(heartbeat(), *[quotes(c, every(T, T + 60, 1, 0.40, 0.30 + 0.0 * i)) for i, c in enumerate(("R1", "R2", "R3"))])
    _, _, summ, eps = run(R, bk)
    ey = eps_of(eps, [(c, "Y") for c in ("R1", "R2", "R3")])
    en = eps_of(eps, [(c, "N") for c in ("R1", "R2", "R3")])
    assert ey and ey[0]["edge"] == pytest.approx(1 - 0.9 - 3 * FEE * 0.3 * 0.7)
    assert en and en[0]["edge"] == pytest.approx(2 - 1.8 - 3 * FEE * 0.6 * 0.4)
    assert np.isnan(en[0]["apy"])                             # converted at once, no lock-up
    bk = books(heartbeat(), *[quotes(c, every(T, T + 60, 1, 0.30, 0.35)) for c in ("R1", "R2", "R3")])
    _, _, _, eps = run(R, bk)
    assert not eps_of(eps, [(c, "Y") for c in ("R1", "R2", "R3")]) and not eps_of(eps, [(c, "N") for c in ("R1", "R2", "R3")])


def test_boxes_and_range_in_above_with_boundaries():
    A, R, B = mk("A", "above", lo=80000), mk("R", "range", lo=80000, hi=82000), mk("B", "above", lo=82000)
    low = mk("L", "above", lo=79000)
    bs = la.baskets(la.prep_markets([A, R, B, low]))
    long_ = [b for b in bs if b.kind == "box_long"][0]
    short = [b for b in bs if b.kind == "box_short"][0]
    assert long_.pay == 1 and long_.boundary          # a close of exactly 82,000.00 pays 0
    assert short.pay == 2 and short.boundary          # exactly 80,000.00 pays 1
    assert la.min_pay([(m, s) for m, s in long_.legs]) == (1.0, 0.0)
    b = find(bs, [("R", "N"), ("L", "Y")])            # [80k, 82k) inside above 79k: exact
    assert b is not None and b.kind == "above-range" and not b.boundary
    b = find(bs, [("R", "N"), ("A", "Y")])            # inside above 80k except a close of exactly 80k
    assert b is not None and b.boundary
    b = find(bs, [("R", "N"), ("B", "N")])            # disjoint from above 82k: exact
    assert b is not None and not b.boundary
    # box long priced to an arb: No(A) 0.30 + Yes(R) 0.30 + Yes(B) 0.30
    bk = books(heartbeat(), quotes("A", every(T, T + 60, 1, 0.70, 0.71)), quotes("R", every(T, T + 60, 1, 0.29, 0.30)),
               quotes("B", every(T, T + 60, 1, 0.29, 0.30)))
    _, _, _, eps = run([A, R, B], bk)
    e = eps_of(eps, [("A", "N"), ("R", "Y"), ("B", "Y")])
    assert e and e[0]["boundary"] and e[0]["edge"] == pytest.approx(1 - 0.9 - 3 * FEE * 0.21)


def test_dedupe_counts_a_stale_quote_once():
    e1 = {"start": 0.0, "end": 10.0, "take": 2.0, "tokens": ["a", "b"]}
    e2 = {"start": 1.0, "end": 5.0, "take": 3.0, "tokens": ["b", "c"]}
    e3 = {"start": 11.0, "end": 12.0, "take": 1.0, "tokens": ["b", "d"]}
    kept = la.dedupe([e1, e2, e3])
    assert kept == [e1, e3]


# ------------------------------------------------------------------ rewards

def test_scoring_matches_the_documented_example():
    # docs: midpoint 0.50, v = 3 c: 100 bid @0.49, 200 bid @0.48 on m, 100 ask @0.51 on m'
    # (the m' ask shows on the Yes book as a bid at 0.49) -> Q_one = (2/3)^2 100 + (1/3)^2 200 + (2/3)^2 100
    q1, q2 = la.book_q({0.49: 200.0, 0.48: 200.0}, {}, 0.50, 3.0, 0.0)
    assert q1 == pytest.approx(4 / 9 * 100 + 1 / 9 * 200 + 4 / 9 * 100) and q2 == 0
    assert float(la.score(3.0, 3.0)) == 0 and float(la.score(3.0, -1.0)) == pytest.approx(4 / 9)
    assert float(la.q_min(90.0, 30.0, 0.5)) == pytest.approx(max(30, 90 / 3))       # single-sided allowance
    assert float(la.q_min(90.0, 0.0, 0.5)) == pytest.approx(30.0)
    assert float(la.q_min(90.0, 0.0, 0.95)) == 0.0                                  # must be two-sided near 0 / 1
    assert la.book_q({0.49: 40.0}, {0.51: 60.0}, 0.50, 3.0, 50.0) == (0.0, pytest.approx(4 / 9 * 60))   # min size


def test_adjusted_mid_and_quotes():
    assert la.adjusted_mid({0.50: 10.0, 0.49: 100.0}, {0.52: 200.0}, 50.0) == (0.50, 0.52, pytest.approx(0.505))
    assert la.our_quote(0.5, 0.49, 0.51, 4.5, 0.01, "edge") == (0.46, 0.54)
    assert la.our_quote(0.5, 0.49, 0.51, 4.5, 0.001, "edge") == (0.456, 0.544)   # strictly inside 4.5 c
    assert la.our_quote(0.5, 0.49, 0.51, 4.5, 0.01, "half") == (0.47, 0.53)
    assert la.our_quote(0.5, 0.49, 0.51, 4.5, 0.01, "touch") == (0.49, 0.51)
    pb, pa = la.our_quote(0.985, 0.98, 0.99, 4.5, 0.01, "edge")
    assert pb == 0.95 and np.isnan(pa)                                           # no ask above 0.99


def test_quote_share_on_a_toy_book():
    # others: Q_one = Q_two = 100; ours: 50 shares at 2 c from 0.50 with v = 3 c -> 50/9 each side
    sh, pb, pa = la.quote_share(0.50, 0.49, 0.51, 100.0, 100.0, 3.0, 50.0, 0.01, "edge")
    assert (pb, pa) == (0.48, 0.52)
    assert sh == pytest.approx((50 / 9) / (50 / 9 + 100))
    sh, _, _ = la.quote_share(0.95, 0.94, 0.96, 0.0, 0.0, 3.0, 50.0, 0.01, "edge")
    assert sh == pytest.approx(1.0)                                               # alone in the book


def test_fills_from_both_tokens_and_markouts():
    m = la.prep_markets([mk("A", "above", lo=80000)])[0]
    bk = books(heartbeat(T, T + 200), quotes("A", every(T, T + 200, 1, 0.49, 0.51)))
    trades = pd.DataFrame([
        {"asset_id": "AY", "ts": T + 10, "price": 0.45, "size": 80, "side": "SELL"},   # through our 0.46 bid
        {"asset_id": "AN", "ts": T + 20, "price": 0.55, "size": 30, "side": "BUY"},    # = Yes SELL at 0.45
        {"asset_id": "AY", "ts": T + 20, "price": 0.45, "size": 30, "side": "SELL"},   # the same print on Yes
        {"asset_id": "AY", "ts": T + 30, "price": 0.50, "size": 30, "side": "SELL"},   # above our bid: no fill
        {"asset_id": "AY", "ts": T + 40, "price": 0.55, "size": 10, "side": "BUY"}])   # through our 0.54 ask
    tr = la.yes_trades(trades, [m])
    assert len(tr) == 4
    prm = {"min_size": 50.0, "max_spread": 4.5, "rate": 0.0}
    f = pd.DataFrame(la.maker_fills(m, prm, la.Books(bk), la.Feed(bk), tr, "edge", settle=1.0))
    assert f["price"].tolist() == [0.46, 0.46, 0.54] and f["qty"].tolist() == [50, 30, 10]
    assert f["m30"].tolist() == pytest.approx([0.04, 0.04, 0.04])       # bought 0.46 / sold 0.54, mid 0.50 later
    assert f["msettle"].tolist() == pytest.approx([0.54, 0.54, -0.46])
    assert f["rebate"].iloc[0] == pytest.approx(0.2 * FEE * 0.46 * 0.54)


def test_rewards_samples_and_end_to_end_report():
    A = mk("A", "above", lo=80000)
    bk = books(heartbeat(T, T + 600, 5), quotes("A", every(T, T + 600, 5, 0.49, 0.51, 200, 200)))
    prm = {"A": {"rate": 14.4, "min_size": 50.0, "max_spread": 4.5, "src": "test", "sponsored": np.nan}}
    spot = pd.DataFrame({"trade_ts": [T, T + 600.0], "receive_ts": [T, T + 600.0], "price": [80500.0, 80600.0]})
    res = la.analyze(bk, pd.DataFrame([A]), spot, None, pd.DataFrame(columns=["asset_id", "ts", "price", "size", "side"]),
                     src=None, prm=prm)
    s = res["samples"]
    assert res["depth"] == "L1" and len(s) == 10
    # others: 200 @ 0.49 and 0.51 (1 c from 0.50, v 4.5): ((3.5)/4.5)^2 * 200; ours at 0.46 / 0.54: (0.5/4.5)^2 * 50
    oth, us = (3.5 / 4.5) ** 2 * 200, (0.5 / 4.5) ** 2 * 50
    assert s["share_edge"].iloc[0] == pytest.approx(us / (us + oth))
    assert s["usd_edge"].sum() == pytest.approx(10 * 14.4 / 1440 * us / (us + oth))
    text = la.report([res], ["syn"], note="test")
    assert "## 1. 吃单套利" in text and "## 3. 流动性奖励（估计）" in text and "above" in text
    assert "样本只有" in text


# ------------------------------------------------------------------ fetching

def test_fetcher_caches_retries_and_respects_ttl(tmp_path):
    calls, sleeps = [], []

    def opener(url):
        calls.append(url)
        if len(calls) == 1:
            raise urllib.error.HTTPError(url, 503, "busy", None, None)
        return {"n": len(calls)}

    f = la.Fetcher(tmp_path, opener=opener, sleep=sleeps.append)
    assert f.get("https://x/a") == {"n": 2} and len(calls) == 2 and sleeps    # one retry after backoff
    assert f.get("https://x/a") == {"n": 2} and len(calls) == 2               # cached forever (ttl None)
    rec = json.loads(f.path("https://x/a").read_text())
    rec["at"] -= 10_000
    f.path("https://x/a").write_text(json.dumps(rec))
    assert f.get("https://x/a", ttl=3600) == {"n": 3}                         # expired: refetched
    assert la.Fetcher(tmp_path, offline=True).get("https://x/a", ttl=1) == {"n": 3}
    with pytest.raises(LookupError):
        la.Fetcher(tmp_path, offline=True).get("https://x/missing")
    g = la.Fetcher(tmp_path, opener=lambda u: (_ for _ in ()).throw(urllib.error.HTTPError(u, 404, "no", None, None)),
                   sleep=lambda s: None)
    with pytest.raises(urllib.error.HTTPError):
        g.get("https://x/404")


def test_reward_params_keep_rates_seen_in_earlier_snapshots(tmp_path):
    A = la.prep_markets([mk("0xa", "above", lo=80000)])[0]
    listed = {"value": True}

    def opener(url):
        if "gamma-api" in url:
            return [{"conditionId": "0xa", "rewardsMinSize": 50, "rewardsMaxSpread": 4.5, "clobRewards": None}]
        if "rewards/markets/current" in url:
            row = {"condition_id": "0xa", "total_daily_rate": 12.5, "sponsored_daily_rate": 12.5,
                   "rewards_min_size": 50, "rewards_max_spread": 4.5}
            return {"data": [row] if listed["value"] else [], "next_cursor": "LTE="}
        raise AssertionError(url)

    notes = []
    p = la.reward_params([A], la.Fetcher(tmp_path, opener=opener, sleep=lambda s: None), notes, now=1_000)
    assert p["0xa"]["rate"] == 12.5 and p["0xa"]["max_spread"] == 4.5 and not notes
    listed["value"] = False                     # the market has ended and left the list
    for f in tmp_path.glob("*.json"):
        f.unlink()                              # force refetching the pages
    p = la.reward_params([A], la.Fetcher(tmp_path, opener=opener, sleep=lambda s: None), notes, now=2_000)
    assert p["0xa"]["rate"] == 12.5             # from the earlier snapshot


def test_hit_window_with_unknown_start_is_not_assumed_to_contain_anything():
    A = mk("A", "above", lo=90000)
    U1 = dict(hit("U1", "hit_up", 89000), start_ts=None, event_slug="week")
    U2 = dict(hit("U2", "hit_up", 91000), start_ts=None, event_slug="week")
    U3 = dict(hit("U3", "hit_up", 91000), start_ts=None, event_slug="month")
    bs = la.baskets(la.prep_markets([A, U1, U2, U3]))
    assert find(bs, [("A", "N"), ("U1", "Y")]) is None                 # the candle may be before the window
    assert find(bs, [("U1", "Y"), ("U2", "N")]) is not None            # same event: same window
    assert find(bs, [("U1", "Y"), ("U3", "N")]) is None                # different events, windows unknown


def test_hit_fill_marked_at_one_after_the_touch():
    H = hit("H", "hit_up", 90000)
    bk = books(heartbeat(T, T + 200), quotes("H", every(T, T + 200, 1, 0.49, 0.51)))
    spot = pd.DataFrame({"trade_ts": [T, T + 100.0], "receive_ts": [T, T + 100.0], "price": [89000.0, 90000.0]})
    m = la.prep_markets([H], spot)[0]
    tr = la.yes_trades(pd.DataFrame([{"asset_id": "HY", "ts": T + 40, "price": 0.55, "size": 10, "side": "BUY"}]), [m])
    f = la.maker_fills(m, {"min_size": 50.0, "max_spread": 4.5}, la.Books(bk), la.Feed(bk), tr, "edge")
    assert len(f) == 1 and f[0]["price"] == 0.54
    assert f[0]["m30"] == pytest.approx(0.54 - 0.50)          # +30 s: before the touch, the mid
    assert f[0]["m120"] == pytest.approx(0.54 - 1.0)          # +120 s: resolved Yes at the touch (T + 100)
    assert f[0]["msettle"] == pytest.approx(0.54 - 1.0)
