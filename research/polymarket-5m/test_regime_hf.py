"""regime_hf.py (REGIME.md's per-window strategy P&L and environment) on synthetic books; no network."""
import io
import tarfile

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import cross
import jump2s_hf as hf
import regime_hf as R

S = int(pd.Timestamp("2026-06-10 12:00", tz="UTC").timestamp())  # a 5m window open in A (point settlement)
MS = S * 1000
END = S + 300
LAG_MS = 100            # recorder receipt lag of every Binance print
UP, DN = "tok-up", "tok-dn"


def fee(p):
    return 0.07 * p * (1 - p)


def binance(steps=(), noise=5e-7, seed=0, t_from=-1900.0, t_to=330.0, start=S):
    """Prints every 250 ms (exchange time, s after `start`), received LAG_MS later; log steps of `bp` at `at` s."""
    t = np.arange(t_from, t_to, 0.25)
    lp = np.log(60000.0) + np.cumsum(np.random.default_rng(seed).normal(0, noise, len(t)))
    for at, bp in steps:
        lp[t >= at - 1e-9] += bp * 1e-4
    ms = start * 1000 + np.round(t * 1000).astype(np.int64)
    return pd.DataFrame({"trade_ts_ms": ms, "recv_ts_ms": ms + LAG_MS, "price": np.exp(lp)})


def book(over=None, market="m1", start=S, state="active", t0=-60.0, t1=305.0):
    """100 ms snapshots, Up 0.49 / 0.51 and the Down book its mirror unless `over(rel, cols)` changes the
    columns in place. The Up bid size alternates 10 / 11, so every snapshot is a recorded change."""
    k = np.arange(int(round(t0 * 10)), int(round(t1 * 10)) + 1)
    rel = k / 10
    n = len(k)
    c = dict(up_best_bid=np.full(n, 0.49), up_best_ask=np.full(n, 0.51), up_bid_size=10.0 + (k % 2),
             up_ask_size=np.full(n, 20.0), down_bid_size=np.full(n, 30.0), down_ask_size=np.full(n, 20.0))
    if over is not None:
        over(rel, c)
    c.setdefault("down_best_bid", np.round(1 - c["up_best_ask"], 6))
    c.setdefault("down_best_ask", np.round(1 - c["up_best_bid"], 6))
    st = state(rel) if callable(state) else state
    return pd.DataFrame({"timestamp_ms": start * 1000 + 100 * k, "market_id": market, "lifecycle_state": st,
                         "observed_halt_flag": False, **c})


def markets(*specs):
    return pd.DataFrame([{"market_id": m, "start": s * 1000, "end": (s + 300) * 1000, "k": 60000.0, "up_won": w,
                          "horizon": 5, "up_token": f"{UP}-{m}", "down_token": f"{DN}-{m}"} for m, s, w in specs])


def prints(rows, market="m1", start=S):
    """[(s after the open, 'up'|'dn', price, taker side[, size])] -> Polymarket prints (receipt time)."""
    return pd.DataFrame({"recv_ts_ms": [start * 1000 + round(1000 * r[0]) for r in rows],
                         "instrument": [f"{UP if r[1] == 'up' else DN}-{market}" for r in rows],
                         "price": [r[2] for r in rows], "size": [r[4] if len(r) > 4 else 10.0 for r in rows],
                         "taker_side": [r[3] for r in rows]}, columns=["recv_ts_ms", "instrument", "price", "size",
                                                                       "taker_side"])


def day(f=None, b=None, pr=(), won=1.0, strats=R.STRATS, inputs=None, closes=None):
    return R.day_trades(book() if f is None else f, markets(("m1", S, won)), binance() if b is None else b,
                        prints(list(pr)), inputs=inputs or {}, closes=closes, strats=strats)


def trades_of(tr, strat):
    return tr[tr["strat"] == strat].sort_values("t").reset_index(drop=True)


def market_ctx(f, b, won=1.0, pr=()):
    starts = {"m1": float(S)}
    return R.Market("m1", S, END, won, R.Books(f, starts), R.Spot(b), R._prints_by(prints(list(pr)), {
        f"{UP}-m1": ("m1", 1), f"{DN}-m1": ("m1", -1)}))


# ------------------------------------------------------------------ H
def test_h_buys_the_stale_ask_half_a_second_later_once_per_two_seconds():
    """Jumps of +3 bp at +100.0, +101.0 and +102.5 s (received 0.1 s later). The first is sent at +100.1
    and filled at +100.6; every jump print received before +102.1 is skipped; +102.6 is the next send."""
    rows, tr, _ = day(b=binance(steps=((100.0, 3.0), (101.0, 3.0), (102.5, 3.0))), strats=("H",))
    h = trades_of(tr, "H")
    assert list(np.round(h["t"] - S, 3)) == [100.1, 102.6]
    assert list(np.round(h["te"] - h["t"], 6)) == [0.5, 0.5]
    assert (h["side"] == 1).all() and (h["price"] == 0.51).all() and (h["how"] == 0).all()
    assert h["fee_in"].iloc[0] == pytest.approx(fee(0.51)) and (h["fee_out"] == 0).all()
    assert h["pps"].iloc[0] == pytest.approx(1 - 0.51 - fee(0.51))
    assert h["t_exit"].iloc[0] == END and h["hold"].iloc[0] == pytest.approx(END - (S + 100.6))
    r = rows.iloc[0]
    assert r["trades_H"] == 2 and r["shares_H"] == 10 and r["sh20_H"] == 40  # 20 shares shown at the ask
    assert r["pnl_H"] == pytest.approx(10 * (1 - 0.51 - fee(0.51)))
    assert r["cost_H"] == pytest.approx(10 * (0.51 + fee(0.51)))
    assert r["caph_H"] == pytest.approx(5 * (0.51 + fee(0.51)) * ((END - S - 100.6) + (END - S - 103.1)) / 3600)
    assert r["pps_H"] == pytest.approx(1 - 0.51 - fee(0.51))
    lost, _, _ = day(b=binance(steps=((100.0, 3.0),)), won=0.0, strats=("H",))
    assert lost["pnl_H"].iloc[0] == pytest.approx(5 * (0 - 0.51 - fee(0.51)))


def test_h_needs_the_edge_at_the_fill_and_the_two_seconds_count_from_the_send():
    """The ask is 0.51 at the decision +102.6 but 0.97 at its fill time +103.1: sent, not filled. The jump
    prints of +103.25 (received +103.35 .. +104.1, < 2 s after that send) are skipped even though nothing
    was bought; with the ask back at 0.51 from +104 they would have been bought."""
    def over(rel, c):
        c["up_best_ask"][(rel >= 103.0) & (rel < 104.0)] = 0.97
    b = binance(steps=((100.0, 3.0), (102.5, 3.0), (103.25, 3.0)))
    rows, tr, _ = day(f=book(over), b=b, strats=("H",))
    h = trades_of(tr, "H")
    assert list(np.round(h["t"] - S, 3)) == [100.1]
    _, tr1, _ = day(f=book(over), b=binance(steps=((100.0, 3.0), (103.25, 3.0))), strats=("H",))
    assert list(np.round(trades_of(tr1, "H")["t"] - S, 3)) == [100.1, 104.1]  # sent at +103.35 (unfilled), +104.1 >= 2 s
    # no jump at all: evaluated, nothing traded -> 0, not NaN
    rows0, tr0, _ = day(strats=("H",))
    assert rows0["trades_H"].iloc[0] == 0 and rows0["pnl_H"].iloc[0] == 0 and np.isnan(rows0["pps_H"].iloc[0])


def test_h_prior_is_the_up_mid_two_seconds_earlier():
    """Binance noise of 0.4 bp a second, so a -3 bp jump moves the fair by about half a sigma. Two seconds
    before every jump print the Up mid is 0.995: the Down fair stays near 0.02 and nothing is sent at the
    Down ask 0.51; with the usual 0.50 mid the Down fair is about 0.7 and it is."""
    def over(rel, c):
        hi = (rel >= 97.5) & (rel < 99.5)
        c["up_best_bid"][hi], c["up_best_ask"][hi] = 0.99, 1.0
    b = binance(steps=((100.0, -3.0),), noise=2e-5)
    _, tr, _ = day(f=book(over), b=b, strats=("H",))
    assert trades_of(tr, "H").empty
    _, tr2, _ = day(b=b, strats=("H",))
    h = trades_of(tr2, "H")
    assert len(h) == 1 and h["side"].iloc[0] == -1 and h["price"].iloc[0] == pytest.approx(0.51)


# ------------------------------------------------------------------ follow (maker exit, strictly through)
def test_follow_resting_sell_fills_only_strictly_through_with_five_shares():
    b = binance(steps=((100.0, 3.0),))
    pr = [(105.0, "up", 0.53, "buy"), (106.0, "up", 0.54, "buy", 3.0), (106.5, "dn", 0.60, "buy"),
          (106.7, "up", 0.40, "sell"), (107.0, "up", 0.54, "buy", 12.0)]
    rows, tr, _ = day(b=b, pr=pr, strats=("follow",))
    f = trades_of(tr, "follow")
    assert len(f) == 1  # the next jump points are < 5 s after +100.1
    r = f.iloc[0]
    assert r["t"] - S == pytest.approx(100.1) and r["te"] - S == pytest.approx(100.6) and r["price"] == 0.51
    assert r["how"] == 2 and r["t_exit"] - S == pytest.approx(107.0) and r["exit_px"] == pytest.approx(0.53)
    assert r["fee_out"] == 0 and r["pps"] == pytest.approx(0.53 - 0.51 - fee(0.51))
    assert r["sh20"] == pytest.approx(12.0)  # min(20 at the ask, 12 in the print through our level)
    assert rows["pnl20_follow"].iloc[0] == pytest.approx(12 * (0.53 - 0.51 - fee(0.51)))


def test_follow_bid_through_the_level_and_touch_and_timer():
    b = binance(steps=((100.0, 3.0),))

    def through(rel, c):
        c["up_best_ask"][(rel >= 108.0) & (rel < 111.0)] = 0.56
        c["up_best_bid"][(rel >= 108.0) & (rel < 109.0)] = 0.53   # a touch: not a fill
        c["up_best_bid"][(rel >= 110.0) & (rel < 111.0)] = 0.54   # through, 10-11 shares shown
    f = trades_of(day(f=book(through), b=b, strats=("follow",))[1], "follow").iloc[0]
    assert f["how"] == 2 and f["t_exit"] - S == pytest.approx(110.0) and f["exit_px"] == pytest.approx(0.53)
    # nothing through by te + 30 s: taker sell decided at +130.6, filled at +131.1 at the bid then (0.4911 here)
    def drift(rel, c):
        c["up_best_bid"] = np.where(rel > 120, np.round(0.48 + rel / 10000, 6), 0.49)
    g = trades_of(day(f=book(drift), b=b, strats=("follow",))[1], "follow").iloc[0]
    assert g["how"] == 1 and g["t_exit"] - S == pytest.approx(131.1) and g["exit_px"] == pytest.approx(0.48 + 0.01311)
    assert g["pps"] == pytest.approx(g["exit_px"] - fee(g["exit_px"]) - 0.51 - fee(0.51))
    assert g["hold"] == pytest.approx(30.5)
    # the book already bids above the level when the sell goes live (+101.1): sold there as a taker
    def cross_(rel, c):
        c["up_best_ask"][(rel >= 101.0) & (rel < 102.0)] = 0.57
        c["up_best_bid"][(rel >= 101.0) & (rel < 102.0)] = 0.55
    h = trades_of(day(f=book(cross_), b=b, strats=("follow",))[1], "follow").iloc[0]
    assert h["how"] == 3 and h["t_exit"] - S == pytest.approx(101.1) and h["exit_px"] == 0.55
    assert h["pps"] == pytest.approx(0.55 - fee(0.55) - 0.51 - fee(0.51))


# ------------------------------------------------------------------ revert
def jump_up_mid(rel, c, at=120.0, spread=0.02):
    m = rel >= at
    c["up_best_bid"][m], c["up_best_ask"][m] = 0.60 - spread / 2, 0.60 + spread / 2


def test_revert_buys_the_other_side_when_the_mid_moves_alone():
    rows, tr, _ = day(f=book(jump_up_mid), strats=("revert",))
    r = trades_of(tr, "revert")
    assert len(r) == 1
    r = r.iloc[0]
    assert r["t"] - S == pytest.approx(120.0) and r["side"] == -1 and r["price"] == pytest.approx(0.41)
    # no print through 0.43, no bid above it: taker exit decided at +180.5, filled at +181.0 at the Down bid 0.39
    assert r["how"] == 1 and r["t_exit"] - S == pytest.approx(181.0) and r["exit_px"] == pytest.approx(0.39)
    assert r["pps"] == pytest.approx(0.39 - fee(0.39) - 0.41 - fee(0.41))
    # a Down print through 0.43 fills the resting sell
    _, tr2, _ = day(f=book(jump_up_mid), pr=[(125.0, "dn", 0.44, "buy")], strats=("revert",))
    r2 = trades_of(tr2, "revert").iloc[0]
    assert r2["how"] == 2 and r2["exit_px"] == pytest.approx(0.43) and r2["t_exit"] - S == pytest.approx(125.0)


def test_revert_needs_binance_quiet_and_a_real_mid():
    # Binance moved 1.5 bp in the same 10 s (received +115.1): not "barely moved" until that print is more than
    # 10 s old; the mid is still 10c above its level 10 s earlier at +125.1, so the trigger moves there
    _, tr, _ = day(f=book(jump_up_mid), b=binance(steps=((115.0, 1.5),)), strats=("revert",))
    assert list(np.round(trades_of(tr, "revert")["t"] - S, 3)) == [125.1]
    _, tr1, _ = day(f=book(jump_up_mid), b=binance(steps=((115.0, 1.5), (122.0, 1.5))), strats=("revert",))
    assert trades_of(tr1, "revert").empty
    # the move is across a 10c hole in the book: no mid
    _, tr2, _ = day(f=book(lambda rel, c: jump_up_mid(rel, c, spread=0.10)), strats=("revert",))
    assert trades_of(tr2, "revert").empty
    # a 7c move is not enough
    def small(rel, c):
        m = rel >= 120
        c["up_best_bid"][m], c["up_best_ask"][m] = 0.56, 0.58
    assert trades_of(day(f=book(small), strats=("revert",))[1], "revert").empty


# ------------------------------------------------------------------ direction
def fac(p):
    return {"factor": pd.DataFrame({"ridge_5m": [p], "hgb_5m": [0.5]}, index=pd.Index([S], name="T"))}


def test_direction_buys_at_the_open_and_sells_with_thirty_seconds_left():
    pre = (lambda rel: np.where(rel < 11.0, "pre_open", "active"))  # the recorder's late "active" label
    rows, tr, _ = day(f=book(state=pre), inputs=fac(0.55), strats=("direction",))
    d = trades_of(tr, "direction").iloc[0]
    assert d["t"] == S and d["te"] - S == pytest.approx(0.5) and d["side"] == 1 and d["price"] == 0.51
    assert d["how"] == 1 and d["t_exit"] == pytest.approx(END - 29.5) and d["exit_px"] == 0.49
    assert d["pps"] == pytest.approx(0.49 - fee(0.49) - 0.51 - fee(0.51))
    assert rows["fac_up"].iloc[0] == pytest.approx(0.55)
    dn = trades_of(day(inputs=fac(0.44), strats=("direction",))[1], "direction").iloc[0]
    assert dn["side"] == -1 and dn["price"] == pytest.approx(0.51)
    # |P - 0.5| < 4c: evaluated, no trade; no prediction: not evaluable
    r0, _, _ = day(inputs=fac(0.53), strats=("direction",))
    assert r0["pnl_direction"].iloc[0] == 0 and r0["trades_direction"].iloc[0] == 0
    rn, _, _ = day(strats=("direction",))
    assert np.isnan(rn["pnl_direction"].iloc[0]) and np.isnan(rn["trades_direction"].iloc[0])
    # no bid with >= 5 shares at the exit: held to the official result
    def thin(rel, c):
        c["up_bid_size"][rel > 260] = 3.0 + (np.arange((rel > 260).sum()) % 2)
    h = trades_of(day(f=book(thin), inputs=fac(0.55), won=0.0, strats=("direction",))[1], "direction").iloc[0]
    assert h["how"] == 0 and h["pps"] == pytest.approx(0 - 0.51 - fee(0.51)) and h["t_exit"] == END


def test_pre_open_snapshots_are_live_only_in_the_first_30_seconds():
    f = book(state="pre_open")
    bk = R.Books(f, {"m1": float(S)})
    a, b = bk.seg["m1"]
    rel = bk.T[a:b] - S
    live = bk.live[a:b]
    assert not live[rel < 0].any() and live[(rel >= 0) & (rel < 29.95)].all() and not live[rel >= 30].any()
    # a book that says pre_open all window long (as the damaged 08-19 archive) is live 30 s of 300: not evaluable
    rows, tr, _ = day(f=f, inputs=fac(0.55), strats=("direction",))
    assert rows["book_cov"].iloc[0] == pytest.approx(0.1) and np.isnan(rows["pnl_direction"].iloc[0]) and tr.empty


# ------------------------------------------------------------------ late
def late_setup(ask_at=None, ask_te=None):
    """A Binance step that puts the driftless model's Up at 95-99% with 60 s left; Up ask set from it."""
    for bp in np.arange(1.0, 40.0, 0.25):
        b = binance(steps=((50.0, bp),), noise=2e-5, seed=3)
        _, pu = R.late_model(market_ctx(book(), b))
        if 0.955 <= pu < 0.985:
            break
    else:
        raise AssertionError("no step gives 95-99%")
    a0 = round(pu - 0.02, 2) if ask_at is None else ask_at(pu)
    a1 = a0 if ask_te is None else ask_te(pu)

    def over(rel, c):
        c["up_best_ask"][rel >= 200] = a0
        c["up_best_bid"][rel >= 200] = a0 - 0.01
        c["up_best_ask"][rel >= 240.3] = a1
    return book(over), b, pu, a0, a1


def test_late_buys_the_95_99_side_at_most_model_minus_one_cent():
    f, b, pu, a0, _ = late_setup()
    rows, tr, _ = day(f=f, b=b, strats=("late",))
    t = trades_of(tr, "late")
    assert len(t) == 1 and t["t"].iloc[0] == END - 60 and t["te"].iloc[0] == END - 59.5
    assert t["side"].iloc[0] == 1 and t["price"].iloc[0] == pytest.approx(a0) and t["how"].iloc[0] == 0
    assert t["pps"].iloc[0] == pytest.approx(1 - a0 - fee(a0))
    # the ask is above model - 1c at the decision, or moves above it before the fill: nothing bought
    f2, b2, *_ = late_setup(ask_at=lambda p: round(p - 0.005, 3))
    assert trades_of(day(f=f2, b=b2, strats=("late",))[1], "late").empty
    f3, b3, *_ = late_setup(ask_te=lambda p: round(p + 0.005, 3))
    r3, tr3, _ = day(f=f3, b=b3, strats=("late",))
    assert trades_of(tr3, "late").empty and r3["pnl_late"].iloc[0] == 0


# ------------------------------------------------------------------ maker
def test_maker_fills_only_when_a_print_goes_strictly_through_our_bid():
    pr = [(50.0, "up", 0.48, "sell"), (55.0, "up", 0.47, "sell", 3.0), (58.0, "up", 0.47, "buy"),
          (60.0, "up", 0.47, "sell"), (70.0, "up", 0.40, "sell")]
    rows, tr, _ = day(pr=pr, won=0.0, strats=("maker",))
    m = trades_of(tr, "maker")
    assert len(m) == 1  # one fill per token; Down never traded through 0.48
    r = m.iloc[0]
    assert r["side"] == 1 and r["price"] == pytest.approx(0.48) and r["fee_in"] == 0
    assert r["te"] - S == pytest.approx(60.0) and r["t"] - S == pytest.approx(15.0)  # quoted at 285 s left
    assert r["how"] == 0 and r["pps"] == pytest.approx(0 - 0.48) and r["t_exit"] == END
    assert r["sh20"] == pytest.approx(10.0)
    assert rows["pnl_maker"].iloc[0] == pytest.approx(5 * -0.48) and rows["trades_maker"].iloc[0] == 1


def test_maker_snapshot_through_and_quotes_follow_each_tokens_mid():
    def over(rel, c):
        dip = (rel >= 80) & (rel < 80.3)  # the Down book drops 4c for 0.3 s: its ask goes through our 0.48
        c["down_best_ask"] = np.where(dip, 0.46, np.round(1 - c["up_best_bid"], 6))
        c["down_best_bid"] = np.where(dip, 0.45, np.round(1 - c["up_best_ask"], 6))
    rows, tr, _ = day(f=book(over), strats=("maker",))
    m = trades_of(tr, "maker")
    assert len(m) == 1 and m["side"].iloc[0] == -1 and m["te"].iloc[0] - S == pytest.approx(80.0)
    assert m["price"].iloc[0] == pytest.approx(0.48) and m["sh20"].iloc[0] == 20
    # the Up mid moves to 0.55 at +100: the Up bid is replaced at 0.53 (live +100.5); a print at 0.52 fills it
    def move(rel, c):
        up = rel >= 100
        c["up_best_bid"][up], c["up_best_ask"][up] = 0.54, 0.56
    orders = R.maker_orders(market_ctx(book(move), binance()), 1)
    assert [(o[0], round(o[1] - S, 3), round(o[2] - S, 3)) for o in orders] == [(0.48, 15.5, 100.2), (0.53, 100.5, 280.2)]
    _, tr2, _ = day(f=book(move), pr=[(130.0, "up", 0.52, "sell")], strats=("maker",))
    m2 = trades_of(tr2, "maker").set_index("side")
    # the same 5c move took the Down book's ask (0.46) through our Down bid 0.48 in one snapshot: filled, adverse
    assert m2.loc[-1, "price"] == pytest.approx(0.48) and m2.loc[-1, "te"] - S == pytest.approx(100.0)
    assert m2.loc[-1, "pps"] == pytest.approx(-0.48)
    assert m2.loc[1, "price"] == pytest.approx(0.53) and m2.loc[1, "te"] - S == pytest.approx(130.0)
    assert m2.loc[1, "pps"] == pytest.approx(1 - 0.53)


def test_maker_cancels_on_a_binance_jump_with_latency_and_pauses_a_second():
    # the +3 bp step's jump prints are received at +100.1 .. +100.85: the first cancels (effective +100.3), the
    # last holds quoting until +101.85; the next order is decided at the +101.9 snapshot and live at +102.4
    b = binance(steps=((100.0, 3.0),))
    orders = R.maker_orders(market_ctx(book(), b), -1)
    assert [(o[0], round(o[1] - S, 3), round(o[2] - S, 3)) for o in orders] == [(0.48, 15.5, 100.3), (0.48, 102.4, 280.2)]
    early = trades_of(day(b=b, pr=[(100.2, "dn", 0.47, "sell")], strats=("maker",))[1], "maker")
    assert len(early) == 1 and early["te"].iloc[0] - S == pytest.approx(100.2)  # still live during the cancel
    late = day(b=b, pr=[(100.4, "dn", 0.47, "sell"), (102.3, "dn", 0.47, "sell")], strats=("maker",))[1]
    assert trades_of(late, "maker").empty
    again = trades_of(day(b=b, pr=[(102.5, "dn", 0.47, "sell")], strats=("maker",))[1], "maker")
    assert len(again) == 1 and again["te"].iloc[0] - S == pytest.approx(102.5) and again["t"].iloc[0] - S == pytest.approx(101.9)


def test_maker_order_the_book_already_crosses_is_a_taker_buy():
    def over(rel, c):
        dip = rel >= 15.5
        c["up_best_bid"][dip], c["up_best_ask"][dip] = 0.45, 0.47
    _, tr, _ = day(f=book(over), strats=("maker",))
    m = trades_of(tr, "maker")
    up = m[m["side"] == 1].iloc[0]
    assert up["te"] - S == pytest.approx(15.5) and up["price"] == 0.47 and up["fee_in"] == pytest.approx(fee(0.47))


# ------------------------------------------------------------------ the environment, evaluability, units
def minutes(start=S - 2 * 86400, n=4 * 1440, seed=1):
    rng = np.random.default_rng(seed)
    m = start + 60 * np.arange(n)
    return pd.DataFrame({"minute": m, "vol": rng.uniform(5, 15, n), "basis_bp": rng.normal(-4, 1, n),
                         "funding_bp": np.repeat(rng.uniform(0, 1, n // 480 + 1), 480)[:n],
                         "rv2": rng.uniform(10, 30, n), "nr": 60})


def full_inputs():
    dv = pd.DataFrame({"hour": S - 3 * 86400 + 3600 * np.arange(24 * 5), "dvol": np.linspace(30, 50, 24 * 5)})
    return dict(fac(0.55), dvol=dv, minutes=R.Minutes(minutes()))


def closes(seed=2):
    k = S - 8 * 3600 + 60 * np.arange(16 * 60)
    return pd.Series(60000 * np.exp(np.cumsum(np.random.default_rng(seed).normal(0, 3e-4, len(k)))), index=k)


def two_markets(f_prev=None, f_cur=None):
    prev = book(market="m0", start=S - 300) if f_prev is None else f_prev
    cur = book() if f_cur is None else f_cur
    return pd.concat([prev, cur], ignore_index=True)


def env_of(f, b, pr, inp, cl):
    mk = markets(("m0", S - 300, np.nan), ("m1", S, 1.0))
    rows, _, _ = R.day_trades(f, mk, b, pr, inputs=inp, closes=cl)
    assert list(rows["market"]) == ["m1"]  # m0 has no final outcome
    return rows


def test_environment_values():
    inp, cl = full_inputs(), closes()
    rows = env_of(two_markets(), binance(), prints([]), inp, cl)
    r = rows.iloc[0]
    M = minutes().set_index("minute")
    before = M.loc[S - 1800:S - 60]
    assert len(before) == 30
    assert r["rv30m"] == pytest.approx(np.sqrt(before["rv2"].sum() * 1e-8 / 1800 * 365 * 86400), rel=1e-9)
    assert r["vol_rel"] == pytest.approx(np.log(M.loc[S - 3600:S - 60, "vol"].mean() / M.loc[S - 86400:S - 60, "vol"].mean()))
    assert r["basis"] == pytest.approx(M.loc[S - 3600:S - 60, "basis_bp"].mean())
    assert r["funding"] == pytest.approx(M.loc[S - 60, "funding_bp"])
    dv = inp["dvol"]
    assert r["dvol"] == pytest.approx(dv.loc[dv["hour"] + 3600 <= S, "dvol"].iloc[-1])
    assert r["dvol_rv"] == pytest.approx(r["dvol"] / 100 - r["rv30m"])
    assert r["hour"] == 12 and r["weekend"] == 0  # 2026-06-10 is a Wednesday
    assert r["pm_spread"] == pytest.approx(0.02) and r["ask_size"] == pytest.approx(20) and r["pm_up"] == pytest.approx(0.5)
    assert r["book_ups"] == pytest.approx(2999 / 300, abs=0.01) and r["book_ups_pre"] == pytest.approx(10, abs=0.02)
    assert np.isfinite(r["vr60"]) and np.isfinite(r["ret4h"]) and r["fac_up"] == 0.55
    assert r["w"] == 0 and r["book_cov"] == 1 and r["bin_cov"] == 1 and r["won"] == 1


def test_no_feature_uses_data_after_the_open():
    inp, cl = full_inputs(), closes()
    f, b, pr = two_markets(), binance(steps=((100.0, 3.0),)), prints([(10.0, "up", 0.6, "buy"), (-5.0, "up", 0.5, "buy")])
    base = env_of(f, b, pr, inp, cl)
    # change everything at or after the open: books, Binance, Polymarket prints, minutes, DVOL, klines
    f2 = f.copy()
    late = f2["timestamp_ms"] >= MS
    f2.loc[late, ["up_best_bid", "up_best_ask", "down_best_bid", "down_best_ask"]] = [0.10, 0.12, 0.88, 0.90]
    f2.loc[late, ["up_ask_size", "down_ask_size", "up_bid_size"]] = [1.0, 2.0, 3.0]
    b2 = b.copy()
    b2.loc[b2["trade_ts_ms"] >= MS, "price"] *= 1.01
    pr2 = prints([(10.0, "up", 0.1, "sell"), (-5.0, "up", 0.5, "buy")])
    m2 = minutes()
    m2.loc[m2["minute"] + 60 > S, ["vol", "basis_bp", "funding_bp", "rv2"]] = [1e6, 99.0, 9.0, 1e5]
    dv2 = inp["dvol"].copy()
    dv2.loc[dv2["hour"] + 3600 > S, "dvol"] = 500.0
    cl2 = cl.copy()
    cl2[cl2.index + 60 > S] *= 1.05
    inp2 = dict(inp, minutes=R.Minutes(m2), dvol=dv2)
    mod = env_of(f2, b2, pr2, inp2, cl2)
    cols = [c for c in R.ENV]
    pd.testing.assert_frame_equal(base[cols], mod[cols])
    assert base["pnl_H"].iloc[0] != mod["pnl_H"].iloc[0]  # the strategies do see the window
    # and the features do move with data just before the open
    m3 = minutes()
    m3.loc[m3["minute"] == S - 60, "rv2"] = 1e4
    f3 = f.copy()
    f3.loc[(f3["timestamp_ms"] < MS) & (f3["market_id"] == "m1"), "up_best_ask"] = 0.55
    pre = env_of(f3, b, pr, dict(inp, minutes=R.Minutes(m3)), cl)
    assert pre["rv5m"].iloc[0] > base["rv5m"].iloc[0] and pre["pm_spread"].iloc[0] > base["pm_spread"].iloc[0]


def test_evaluability_nan_vs_zero():
    # Binance feed dead for the window: Binance strategies NaN, direction still evaluated
    b = binance()
    b = b[(b["recv_ts_ms"] < MS) | (b["recv_ts_ms"] > MS + 300_000)]
    rows, _, info = day(b=b, inputs=fac(0.55))
    r = rows.iloc[0]
    for s in R.NEEDS_BINANCE:
        assert np.isnan(r[f"pnl_{s}"]) and info[f"na_{s}"] == 1
    assert r["trades_direction"] == 1 and r["bin_cov"] == 0
    # the book stops changing after +100 s: book_cov < 0.5, nothing evaluable
    def frozen(rel, c):
        c["up_bid_size"] = np.where(rel > 100, 10.0, c["up_bid_size"])
    rows2, _, _ = day(f=book(frozen), inputs=fac(0.55))
    assert rows2["book_cov"].iloc[0] < 0.5 and rows2[[f"pnl_{s}" for s in R.STRATS]].isna().all(axis=1).iloc[0]
    assert rows2["pnl_none"].iloc[0] == 0


def test_only_markets_with_a_final_outcome_and_a_book():
    mk = markets(("m1", S, 1.0), ("m2", S, np.nan), ("m3", S - 86400, 0.0))
    rows, _, info = R.day_trades(book(), mk, binance(), prints([]), inputs={})
    assert list(rows["market"]) == ["m1"] and info["no_book"] == 1 and info["markets"] == 1


def test_dedupe_and_write_table(tmp_path):
    rows, _, _ = day(b=binance(steps=((100.0, 3.0),)), inputs=fac(0.55))
    two = pd.concat([rows.assign(n_chg=5.0, day="a"), rows.assign(day="b")], ignore_index=True)
    d = R.dedupe(two)
    assert len(d) == 1 and d["day"].iloc[0] == "b"
    cols = R.write_table(d, tmp_path / "w.csv.gz")
    back = pd.read_csv(tmp_path / "w.csv.gz")
    assert cols == R.ROW_COLS and list(back.columns) == R.ROW_COLS
    assert back["pnl_H"].iloc[0] == pytest.approx(rows["pnl_H"].iloc[0], abs=1e-5)
    import regime_learn as RL
    W, mapping, strats, missing = RL.load_windows(back, log=lambda s: None)
    assert list(strats) == list(R.STRATS)
    assert not set(missing) & {"rv5m", "rv30m", "rv1h", "dvol_rv", "vol_rel", "pm_spread", "ask_size", "book_ups"}
    assert mapping["pnl_H"] == "pnl_H" and mapping["caph_maker"] == "caph_maker" and mapping["start"] == "start"


# ------------------------------------------------------------------ archives, final outcomes, the subcommand
def parquet_bytes(df):
    buf = io.BytesIO()
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), buf)
    return buf.getvalue()


def archive(path, dday, feats, mks, res, binance_, prints_):
    def to_arc(f):
        f = f.drop(columns=["up_ask_size", "down_ask_size", "up_bid_size", "down_bid_size"]).copy()
        f["up_ask_sizes"] = [[20.0, 7.0]] * len(f)
        f["down_ask_sizes"] = [[20.0]] * len(f)
        f["up_bid_sizes"] = [[10.0 + (i % 2), 3.0] for i in range(len(f))]
        f["down_bid_sizes"] = [[30.0]] * len(f)
        return f
    b = binance_.assign(exchange="binance", instrument="BTCUSDT", size=0.01, taker_side="buy")
    p = prints_.assign(exchange="polymarket", trade_ts_ms=lambda d: d["recv_ts_ms"])
    tr = pd.concat([b, p], ignore_index=True)[["trade_ts_ms", "recv_ts_ms", "exchange", "instrument", "price", "size",
                                               "taker_side"]]
    parts = [(f"dataset=polymarket_features_100ms/date={dday}/part-1.parquet", to_arc(feats)),
             (f"dataset=polymarket_market_100ms/date={dday}/part-2.parquet", mks),
             (f"dataset=trades/date={dday}/part-3.parquet", tr)]
    if res is not None:
        parts.append((f"dataset=resolution/date={dday}/part-4.parquet", res))
    with tarfile.open(path, "w:gz") as tar:
        for name, df in parts:
            raw = parquet_bytes(df)
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))
    return path


def mrow(m, start, won=None):
    return {"timestamp_ms": (start + 300) * 1000, "market_id": m, "slug": f"btc-updown-5m-{start}",
            "session_start_ts": start * 1000, "session_end_ts": (start + 300) * 1000, "chainlink_open_price": 60000.0,
            "up_won": won, "lifecycle_state": "resolved", "up_token_id": f"{UP}-{m}", "down_token_id": f"{DN}-{m}"}


def rrow(m, direction, check, rev=2):
    return {"market_id": m, "horizon": "5m", "outcome_direction": direction, "consistency_check": check,
            "revision": rev, "emitted_at_ts": 10 * rev}


def test_subcommand_end_to_end_final_outcomes_and_carry(tmp_path, monkeypatch):
    """Day 1: m1 (final DOWN, while its market rows say up_won = 1: the market rows' outcome is never used),
    m2 (provisional only: left out) and m3, whose window ends at midnight and which is resolved in day 2's
    archive: priced there from the carried snapshots, Binance and Polymarket prints of day 1."""
    d1, d2 = "2026-06-10", "2026-06-11"
    S3 = int(pd.Timestamp(d2, tz="UTC").timestamp()) - 300
    f1 = pd.concat([book(market="m1"), book(market="m2"), book(market="m3", start=S3, t1=299.9)], ignore_index=True)
    b1 = pd.concat([binance(steps=((100.0, 3.0),)), binance(steps=((200.0, 3.0),), start=S3, t_from=-700, t_to=299.9)])
    p1 = pd.concat([prints([(60.0, "up", 0.47, "sell")]), prints([(60.0, "up", 0.47, "sell")], market="m3", start=S3)])
    mk1 = pd.DataFrame([mrow("m1", S, 1.0), mrow("m2", S), mrow("m3", S3)])
    rs1 = pd.DataFrame([rrow("m1", "DOWN", "ok"), rrow("m2", "UP", "chainlink_resolution_price_missing", 1)])
    f2 = book(market="m3", start=S3, t0=300.0, t1=320.0)  # only the market's rows after its end
    b2 = binance(start=S3, t_from=300.0, t_to=900.0)
    mk2 = pd.DataFrame([mrow("m3", S3)])
    rs2 = pd.DataFrame([rrow("m3", "UP", "ok")])
    n1, n2 = f"market_parquet_{d1}.tar.gz", f"market_parquet_{d2}.tar.gz"
    srcs = {n1: archive(tmp_path / "a1.tar.gz", d1, f1, mk1, rs1, b1, p1),
            n2: archive(tmp_path / "a2.tar.gz", d2, f2, mk2, rs2, b2, prints([], market="m3", start=S3))}
    wd = tmp_path / "wd"
    fetched = []

    def fetch(n, dest=None):
        fetched.append(n)
        if n == "MANIFEST.txt":
            return "".join(f"{k}  abc  {v.stat().st_size}\n" for k, v in srcs.items()).encode()
        dest.write_bytes(srcs[n].read_bytes())
        return dest

    def download(url, dest):
        raise OSError("offline")

    monkeypatch.setattr(cross, "fetch", fetch)
    monkeypatch.setattr(hf, "download", download)
    out = tmp_path / "real" / "regime-windows.csv.gz"
    cross.main(["regime", "--workdir", str(wd), "--out", str(out)])
    assert fetched == ["MANIFEST.txt", n1, n2] and not (wd / n1).exists() and not (wd / n2).exists()
    assert (wd / "regime-shards" / f"{d1}.parquet").exists() and (wd / "regime-shards" / f"{d2}.parquet").exists()
    rows = pd.read_csv(out, dtype={"market": str})
    assert list(rows.columns) == R.ROW_COLS
    assert list(rows["market"]) == ["m1", "m3"] and list(rows["day"]) == [d1, d2]
    r1, r3 = rows.iloc[0], rows.iloc[1]
    assert r1["won"] == 0 and r3["won"] == 1
    assert r1["trades_H"] == 1 and r1["pnl_H"] == pytest.approx(5 * (0 - 0.51 - fee(0.51)), abs=1e-4)
    assert r3["book_cov"] > 0.9 and r3["bin_cov"] > 0.9  # day 1's rows, carried
    assert r3["trades_H"] == 1 and r3["pnl_H"] == pytest.approx(5 * (1 - 0.51 - fee(0.51)), abs=1e-4)
    assert r3["trades_maker"] == 1 and r3["pnl_maker"] == pytest.approx(5 * (1 - 0.48), abs=1e-4)  # carried print
    assert np.isfinite(r1["fac_up"]) and np.isfinite(r1["rv30m"]) and np.isfinite(r1["dvol_rv"])  # real/regime-inputs


def test_build_inputs_minute_file(tmp_path):
    """Panel row T (known at T) becomes minute T - 60; rv2 sums the squared 1 s log returns of that minute's
    seconds (bp^2), the first one against the previous minute's last close."""
    cache, mix = tmp_path / "cache", tmp_path / "mix"
    cache.mkdir()
    mix.mkdir()
    for name in ("factor_preds_5m.csv.gz", "dvol_1h.csv.gz"):
        (mix / name).write_bytes(b"x")
    t0 = int(pd.Timestamp("2026-05-20", tz="UTC").timestamp())
    idx = pd.date_range("2026-05-19 23:59", periods=6, freq="1min", tz="UTC", name="t")  # 23:59 .. 00:04
    panel = pd.DataFrame({"spot_vol": [2.0, 3.0, 4.0, 5.0, 6.0, 7.0], "spot_close": 100.0,
                          "fut_close": [100.0, 100.0, 100.05, 99.9, 100.0, 100.0],
                          "funding_rate": [1e-4, 1e-4, 1e-4, 2e-4, 2e-4, 2e-4]}, index=idx)
    panel.to_parquet(cache / "panel_1m.parquet")
    sec = np.arange(t0 - 120, t0 + 300)
    close = 100.0 * np.exp(np.where(sec >= t0 + 30, 2e-4, 0.0) + np.where(sec >= t0 + 70, -1e-4, 0.0))
    pd.DataFrame({"close": close}, index=pd.Index(sec, name="sec")).to_parquet(cache / "spot_1s_close.parquet")
    out = R.build_inputs(cache, tmp_path / "inp", start="2026-05-20", end="2026-05-21", mix_inputs=mix)
    assert list(out["minute"]) == [t0, t0 + 60, t0 + 120, t0 + 180]  # panel rows 00:01 .. 00:04
    assert list(out["vol"]) == [4.0, 5.0, 6.0, 7.0] and list(out["nr"]) == [60, 60, 60, 60]
    assert list(out["basis_bp"]) == pytest.approx([5.0, -10.0, 0.0, 0.0])
    assert list(out["funding_bp"]) == pytest.approx([1.0, 2.0, 2.0, 2.0])
    assert list(out["rv2"]) == pytest.approx([4.0, 1.0, 0.0, 0.0], abs=1e-3)
    assert (tmp_path / "inp" / "binance_1m.csv.gz").exists() and (tmp_path / "inp" / "dvol_1h.csv.gz").exists()
    M = R.Minutes(pd.read_csv(tmp_path / "inp" / "binance_1m.csv.gz"))
    s, c = M.window("rv2", np.array([t0 + 120.0]), 2)
    assert s[0] == pytest.approx(5.0, abs=1e-3) and c[0] == 2  # minutes t0, t0 + 60 (closed by t0 + 120)
    assert M.funding(np.array([t0 + 120.0]))[0] == pytest.approx(2.0)


def test_twap_era_market_runs_every_strategy():
    """A window opening 2026-08-20 settles on the 60 s TWAP (w = 60): H's fair uses binary.twap_std_factor and
    the late model the TWAP rule; both still trade on a clear move."""
    s2 = int(pd.Timestamp("2026-08-20 12:00", tz="UTC").timestamp())
    f = book(start=s2)
    b = binance(steps=((100.0, 3.0),), start=s2)
    rows, tr, info = R.day_trades(f, markets(("m1", s2, 1.0)), b, prints([], start=s2), inputs={})
    assert rows["w"].iloc[0] == 60 and info["n_H"] == 1
    h = trades_of(tr, "H").iloc[0]
    assert h["t"] - s2 == pytest.approx(100.1) and h["price"] == 0.51
    mk = R.Market("m1", s2, s2 + 300, 1.0, R.Books(f, {"m1": float(s2)}), R.Spot(b), {})
    assert mk.w == 60 and np.isfinite(R.late_model(mk)[1])
