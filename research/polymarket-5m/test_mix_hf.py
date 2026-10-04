"""mix_hf.py (MIX.md on the Hugging Face books) on synthetic archives and rows; no network."""
import io
import json
import tarfile

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import cross
import jump2s_hf as hf
import mix_hf as M

S = int(pd.Timestamp("2026-06-10 12:00", tz="UTC").timestamp())  # a 5m window open in A (point settlement)
MS = S * 1000
END = S + 300
JUMP = 100.0            # the +2.5 bp Binance step, exchange time, s after the open
LAG_MS = 100            # recorder receipt lag of every Binance print
T0 = S + JUMP + LAG_MS / 1000   # the jump's decision time (receipt) = S + 100.1
TE = T0 + M.LAT_S               # entry fill = S + 100.6
UP, DN = "tok-up", "tok-dn"


def fee(p):
    return 0.07 * p * (1 - p)


def binance(steps=((JUMP, 2.5),), noise=5e-7, seed=0, t_from=-1900.0, t_to=330.0):
    """Prints every 250 ms (exchange time), received LAG_MS later; log steps of `bp` at `at` s."""
    t = np.arange(t_from, t_to, 0.25)
    lp = np.log(60000.0) + np.cumsum(np.random.default_rng(seed).normal(0, noise, len(t)))
    for at, bp in steps:
        lp[t >= at - 1e-9] += bp * 1e-4
    ms = MS + np.round(t * 1000).astype(np.int64)
    return pd.DataFrame({"trade_ts_ms": ms, "recv_ts_ms": ms + LAG_MS, "price": np.exp(lp)})


def default_ask(rel):
    return np.where(rel <= 100.6 + 1e-9, 0.50, 0.95)


def default_bid(rel):
    return np.round(0.30 + np.round(rel * 10) / 10000, 6)  # 0.4006 at +100.6 s, 0.4061 at +106.1 s: one value per row


def book(up_ask=default_ask, up_bid=default_bid, ask_size=20.0, frozen=None, drop=None, market="m1", start=S):
    """100 ms snapshots from 60 s before the open to the end. Up ask 0.50 up to and including the
    +100.6 s snapshot (the jump row's entry), 0.95 after; Up bid 0.30 + rel / 1000 (each row its own
    value, so an exit price shows which snapshot was used); the Down book is the mirror image. The Up bid
    size alternates 10 / 11, so every snapshot is a recorded change, except where `frozen(rel)` is true."""
    k = np.arange(-600, 3001)
    rel = k / 10
    ua, ub = up_ask(rel), up_bid(rel)
    f = pd.DataFrame({"timestamp_ms": start * 1000 + 100 * k, "market_id": market, "lifecycle_state": "active",
                      "observed_halt_flag": False, "up_best_bid": ub, "up_best_ask": ua, "down_best_bid": 1 - ua,
                      "down_best_ask": 1 - ub, "up_ask_size": ask_size, "down_ask_size": 20.0,
                      "up_bid_size": 10.0 + (k % 2), "down_bid_size": 30.0})
    if frozen is not None:
        fz = np.asarray(frozen(rel), bool)
        grp = np.cumsum(~fz)
        cols = [c for c in hf.STATE_COLS if c in f]
        f.loc[:, cols] = f.loc[~fz, cols].iloc[grp - 1].to_numpy()
    if drop is not None:
        f = f[~drop(rel)]
    return f.reset_index(drop=True)


def markets(*specs):
    return pd.DataFrame([{"market_id": m, "start": s * 1000, "end": (s + 300) * 1000, "k": 60000.0, "up_won": w,
                          "horizon": 5, "up_token": f"{UP}-{m}", "down_token": f"{DN}-{m}"} for m, s, w in specs])


def prints(rows, market="m1"):
    """[(s after the open, 'up'|'dn', price, taker side)] -> Polymarket prints (receipt time)."""
    return pd.DataFrame({"recv_ts_ms": [MS + round(1000 * t) for t, *_ in rows],
                         "instrument": [f"{UP if tk == 'up' else DN}-{market}" for _, tk, *_ in rows],
                         "price": [p for *_, p, _ in rows], "size": 10.0,
                         "taker_side": [s for *_, s in rows]}, columns=["recv_ts_ms", "instrument", "price", "size",
                                                                         "taker_side"])


def day(f=None, b=None, pr=(), won=1.0, **kw):
    return M.day_rows(book() if f is None else f, markets(("m1", S, won)), binance() if b is None else b,
                      prints(list(pr)), **kw)


def one(rows, kind, side, t=None):
    r = rows[(rows["kind"].astype(str) == kind) & (rows["side"] == side)]
    if t is not None:
        r = r[np.abs(r["t"] - t) < 1e-6]
    assert len(r) == 1, (kind, side, t, len(r))
    return r.iloc[0]


# ------------------------------------------------------------------ decision points and features
def test_decision_points_jumps_fixed_and_random():
    b = binance(steps=((10.0, 3.0), (JUMP, 2.5), (103.0, 2.5), (106.0, -3.0), (285.0, 3.0)))
    rows, info = day(b=b)
    assert info["jump"] == 2 and info["fixed"] == 4 and info["random"] == 2
    jt = sorted(rows.loc[rows["kind"] == "jump", "t"].unique() - S)
    # +10 s is 290 s before the end (> 285), +285 s is 15 s before it (< 20); +103 s is 2.9 s after a kept point
    assert jt == pytest.approx([100.1, 106.1])
    fx = sorted(rows.loc[rows["kind"] == "fixed", "t"].unique() - S)
    assert fx == pytest.approx([60, 120, 180, 240])
    u, d = one(rows, "jump", 1, T0), one(rows, "jump", -1, T0)
    assert u["jump_bp"] == pytest.approx(2.5, abs=0.05) and d["jump_bp"] == pytest.approx(-2.5, abs=0.05)
    assert u["dir"] == 1 and d["dir"] == -1 and u["is_jump"] == 1 and u["jdir"] == 1 and d["jdir"] == 1
    assert u["jump_z"] > 10 and u["tau"] == pytest.approx(300 - 100.1)
    assert u["ask"] == pytest.approx(0.50) and u["spread"] == pytest.approx(0.50 - 0.4001)
    assert d["ask"] == pytest.approx(1 - 0.4001) and u["imb"] == pytest.approx((11 - 20) / 31)
    # H fair: the Up mid 2 s before plus the Binance move -> Up's fair is near 1 (sigma is tiny)
    assert u["h_edge"] > 0.3 and d["h_edge"] < -0.5
    r = rows[rows["kind"] == "random"]
    assert len(r) == 2 and set(r["side"]) <= {-1, 1} and ((END - r["t"] >= 20) & (END - r["t"] <= 285)).all()
    again, _ = day(b=b)
    pd.testing.assert_frame_equal(rows, again)  # seeded per market


def test_no_feature_uses_data_after_the_decision_time():
    C = S + 130.0
    b = binance(steps=((JUMP, 2.5), (150.0, 4.0)))
    f = book()
    pr = [(101.0, "up", 0.52, "buy"), (140.0, "up", 0.70, "buy")]
    closes = pd.Series(60000.0 * np.exp(np.linspace(0, 0.01, 721)), index=S - 6 * 3600 + 60 * np.arange(721))
    base, _ = day(f=f, b=b, pr=pr, closes=closes)
    # everything after C changes: the books, the Binance prints, the Polymarket prints, the minute closes
    f2 = f.copy()
    late = f2["timestamp_ms"] > C * 1000
    f2.loc[late, ["up_best_ask", "up_best_bid"]] = [0.97, 0.02]
    f2.loc[late, ["down_best_ask", "down_best_bid"]] = [0.98, 0.03]
    f2.loc[late, ["up_ask_size", "up_bid_size"]] = [7.0, 900.0]
    b2 = b.copy()
    b2.loc[b2["recv_ts_ms"] > C * 1000, "price"] *= 1.004
    pr2 = [(101.0, "up", 0.52, "buy"), (140.0, "up", 0.20, "sell"), (131.0, "dn", 0.9, "buy")]
    c2 = closes.copy()
    c2[c2.index + 60 > C] *= 0.99
    mod, _ = day(f=f2, b=b2, pr=pr2, closes=c2)
    key = ["kind", "t", "side"]
    # (random control rows are left out: how many a market gets depends on its jumps, also later ones)
    a = base[(base["t"] <= C) & (base["kind"] != "random")].sort_values(key).reset_index(drop=True)
    z = mod[(mod["t"] <= C) & (mod["kind"] != "random")].sort_values(key).reset_index(drop=True)
    assert len(a) == len(z) == 6 and (a["kind"] == "jump").sum() == 2
    pd.testing.assert_frame_equal(a[key + M.FEATURES], z[key + M.FEATURES])
    assert np.isfinite(a["vr60"]).all() and np.isfinite(a["trend_side"]).all()
    # ... while the labels that reach past C do change
    assert one(base, "jump", 1, T0)["pnl_tk60"] != one(mod, "jump", 1, T0)["pnl_tk60"]
    # the snapshot just after t is never read: change only the +100.2 s row
    f3 = f.copy()
    f3.loc[f3["timestamp_ms"] == MS + 100_200, ["up_best_ask", "up_bid_size"]] = [0.60, 500.0]
    r3, _ = day(f=f3, b=b, pr=pr, closes=closes)
    assert one(r3, "jump", 1, T0)["ask"] == pytest.approx(0.50)
    assert one(r3, "jump", 1, T0)["imb"] == pytest.approx(one(base, "jump", 1, T0)["imb"])


def test_dvol_and_factor_inputs_are_known_before_t():
    dv = pd.DataFrame({"hour": [S - 7200, S - 3600, S], "dvol": [40.0, 50.0, 60.0]})
    assert M.dvol_at(dv, [S - 1, S, S + 3599, S + 3600]) == pytest.approx([40, 50, 50, 60])
    fp = pd.DataFrame({"ridge_5m": [0.6], "hgb_5m": [0.4], "ridge_15m": [0.55], "hgb_15m": [0.5]},
                      index=pd.Index([S], name="T"))
    rows, _ = day(factor=fp, dvol=dv)
    u, d = one(rows, "jump", 1, T0), one(rows, "jump", -1, T0)
    assert u["f_ridge5"] == pytest.approx(0.1) and d["f_ridge5"] == pytest.approx(-0.1)
    assert u["f_hgb5"] == pytest.approx(-0.1) and u["f_ridge15"] == pytest.approx(0.05)
    # DVOL 50 (the hour from S - 3600 closed at S; the one from S is not known yet) / 100 minus rv30
    # (annualised std of the 1 s returns: 4 prints a second, log noise 5e-7 each -> about 1e-6 a second)
    assert u["dvol_rv"] == pytest.approx(0.5 - 1e-6 * np.sqrt(365 * 86400), abs=1e-3)
    assert u["dvol_rv"] == d["dvol_rv"]


# ------------------------------------------------------------------ execution
def test_entry_at_t_plus_half_second_and_taker_exits():
    rows, _ = day()
    u = one(rows, "jump", 1, T0)
    assert u["price"] == pytest.approx(0.50) and u["ask_size"] == 20 and u["won"] == 1
    cost = 0.50 + fee(0.50)
    assert u["pnl_settle"] == pytest.approx(1 - cost) and u["hold_settle"] == pytest.approx(300 - 100.6)
    for h in M.HOLDS:
        bid = 0.30 + (100.6 + h + 0.5) / 1000  # the snapshot at te + h + 0.5 s, not the one after it
        assert u[f"pnl_tk{h}"] == pytest.approx(bid - fee(bid) - cost, abs=1e-6), h
        assert u[f"hold_tk{h}"] == pytest.approx(h + 0.5) and u[f"how_tk{h}"] == 1
        assert u[f"sh20_tk{h}"] == 10 or u[f"sh20_tk{h}"] == 11  # min(20, ask 20, bid size)
    # received 40 ms later: the entry (+100.64 s) still uses the +100.6 s snapshot
    b = binance()
    b["recv_ts_ms"] += 40
    assert one(day(b=b)[0], "jump", 1, T0 + 0.04)["price"] == pytest.approx(0.50)
    # the ask moving at the entry snapshot itself is seen
    r, _ = day(f=book(up_ask=lambda rel: np.where(rel < 100.6 - 1e-9, 0.50, 0.95)))
    assert one(r, "jump", 1, T0)["price"] == pytest.approx(0.95)


def test_exit_after_the_end_or_on_a_stale_book_holds_to_settlement():
    rows, _ = day(won=0.0)
    f = one(rows, "fixed", 1, END - 60)  # te = end - 59.5
    cost = f["price"] + fee(f["price"])
    assert f["how_tk60"] == 0 and f["pnl_tk60"] == pytest.approx(-cost) and f["hold_tk60"] == pytest.approx(59.5)
    assert f["how_tk30"] == 1 and f["hold_tk30"] == pytest.approx(30.5)
    # the book stops changing 1.5 s before the 5 s exit (+106.1 s): no bid, held
    r, _ = day(f=book(frozen=lambda rel: (rel > 104.6 + 1e-9) & (rel < 107)), won=0.0)
    u = one(r, "jump", 1, T0)
    assert u["how_tk5"] == 0 and u["pnl_tk5"] == pytest.approx(-(0.5 + fee(0.5)))
    assert u["how_tk10"] == 1


def test_entry_needs_a_fresh_book_five_shares_and_the_band():
    rows, info = day(f=book(ask_size=4.0))
    assert not ((rows["kind"] == "jump") & (rows["side"] == 1)).any() and info["no_entry"] >= 1
    assert len(rows[(rows["kind"] == "jump") & (rows["side"] == -1)]) == 1
    stuck = book(frozen=lambda rel: (rel > 99.5 + 1e-9) & (rel < 103))  # unchanged 1.1 s before the entry
    assert not (day(f=stuck)[0]["kind"] == "jump").any()
    ok = book(frozen=lambda rel: (rel > 99.7 + 1e-9) & (rel < 103))  # unchanged 0.9 s: fine
    assert (day(f=ok)[0]["kind"] == "jump").sum() == 2
    high = book(up_ask=lambda rel: np.where(rel <= 100.6 + 1e-9, 0.985, 0.99))
    assert not ((day(f=high)[0]["kind"] == "jump") & (day(f=high)[0]["side"] == 1)).any()


def test_maker_fill_print_or_bid_at_or_above_the_limit_only_after_the_order_is_live():
    # entry 0.50 at +100.6 s; the resting sell is live at +101.1 s
    bid = lambda rel: np.where(rel < 107.6 - 1e-9, 0.40, 0.52)  # noqa: E731  bid 0.52 from te + 7 s
    pr = [(100.9, "up", 0.60, "buy"),    # before the order is live
          (101.3, "up", 0.60, "sell"),   # a taker sell does not hit a resting sell
          (101.5, "dn", 0.60, "buy"),    # the other token
          (102.0, "up", 0.505, "buy"),   # below 0.51
          (103.6, "up", 0.515, "buy")]   # fills +1c (0.51) at te + 3 s
    rows, _ = day(f=book(up_bid=bid), pr=pr)
    u = one(rows, "jump", 1, T0)
    cost = 0.50 + fee(0.50)
    for h in M.HOLDS:
        assert u[f"how_mk1_{h}"] == 2 and u[f"pnl_mk1_{h}"] == pytest.approx(0.51 - cost)  # no maker fee
        assert u[f"hold_mk1_{h}"] == pytest.approx(3.0)
    # +2c (0.52): the bid reaches it at te + 7 s: not within 5 s (taker exit at te + 5.5 s), filled for h >= 10
    assert u["how_mk2_5"] == 1 and u["pnl_mk2_5"] == pytest.approx(0.40 - fee(0.40) - cost)
    assert u["pnl_mk2_5"] == pytest.approx(u["pnl_tk5"])
    for h in (10, 30, 60):
        assert u[f"how_mk2_{h}"] == 2 and u[f"pnl_mk2_{h}"] == pytest.approx(0.52 - cost)
        assert u[f"hold_mk2_{h}"] == pytest.approx(7.0)
    for x in (3, 5):  # never: the taker exit of the same h
        for h in M.HOLDS:
            assert u[f"pnl_mk{x}_{h}"] == pytest.approx(u[f"pnl_tk{h}"]) and u[f"how_mk{x}_{h}"] == u[f"how_tk{h}"]
    # without the +103.6 s print, +1c also waits for the bid at te + 7 s
    r2, _ = day(f=book(up_bid=bid), pr=pr[:-1])
    assert one(r2, "jump", 1, T0)["how_mk1_5"] == 1 and one(r2, "jump", 1, T0)["hold_mk1_10"] == pytest.approx(7.0)
    # the same 0.515 print 0.2 s before the order is live does not count
    r3, _ = day(f=book(up_bid=bid), pr=[(100.9, "up", 0.515, "buy")])
    assert one(r3, "jump", 1, T0)["how_mk1_5"] == 1


def test_queue_sensitivity_needs_a_print_above_the_limit():
    # +1c = 0.51: a print AT 0.51 at te + 1.9 s fills under MIX.md's rule; the strict sensitivity waits
    # for the print above it at te + 3 s; +2c (0.52) is reached by the bid at te + 7 s either way
    bid = lambda rel: np.where(rel < 107.6 - 1e-9, 0.40, 0.52)  # noqa: E731
    pr = [(102.5, "up", 0.51, "buy"), (103.6, "up", 0.515, "buy")]
    u = one(day(f=book(up_bid=bid), pr=pr)[0], "jump", 1, T0)
    cost = 0.50 + fee(0.50)
    assert u["how_mk1_5"] == 2 and u["hold_mk1_5"] == pytest.approx(1.9)
    assert u["fs1"] == pytest.approx(3.0) and u["fs2"] == pytest.approx(7.0) and np.isnan(u["fs5"])
    r = pd.DataFrame([u])
    assert M.strict_pnl(r, "mk1_5")[0] == pytest.approx(0.51 - cost)
    assert M.strict_pnl(r, "mk2_5")[0] == pytest.approx(u["pnl_tk5"])  # 7 s > 5 s: the taker exit
    assert M.strict_pnl(r, "mk2_10")[0] == pytest.approx(0.52 - cost) and M.strict_pnl(r, "tk5") is None
    # only the print at the limit: strictly, the +1c sell is never filled within 5 s
    u2 = one(day(f=book(up_bid=bid), pr=pr[:1])[0], "jump", 1, T0)
    assert u2["how_mk1_5"] == 2 and M.strict_pnl(pd.DataFrame([u2]), "mk1_5")[0] == pytest.approx(u2["pnl_tk5"])
    assert u2["fs1"] == pytest.approx(7.0)  # the bid reaches 0.52 >= 0.51 at te + 7 s


def test_resting_sell_that_would_cross_on_arrival_is_a_taker_sale():
    bid = lambda rel: np.where((rel >= 101.0 - 1e-9) & (rel < 101.2), 0.56, 0.40)  # noqa: E731
    rows, _ = day(f=book(up_bid=bid))
    u = one(rows, "jump", 1, T0)
    cost = 0.50 + fee(0.50)
    for x in M.MAKER_X:
        assert u[f"how_mk{x}_5"] == 3 and u[f"pnl_mk{x}_5"] == pytest.approx(0.56 - fee(0.56) - cost)
        assert u[f"hold_mk{x}_5"] == pytest.approx(0.5) and u[f"sh20_mk{x}_5"] == 11  # min(20, ask 20, bid 11)


def test_thin_bid_on_arrival_does_not_cross_and_nothing_counts_after_the_end():
    # the bid reaches 0.56 at +101.0 s with 3 shares: below the 5 a taker sale needs -> no crossing sale;
    # it is gone at +101.2 s, so the resting sells are never hit and fall back to the taker exit
    bid = lambda rel: np.where((rel >= 101.0 - 1e-9) & (rel < 101.2 - 1e-9), 0.56, 0.40)  # noqa: E731
    f = book(up_bid=bid)
    f.loc[(f["timestamp_ms"] >= MS + 101_000) & (f["timestamp_ms"] < MS + 101_200), "up_bid_size"] = 3.0
    u = one(day(f=f)[0], "jump", 1, T0)
    for x in M.MAKER_X:
        for h in M.HOLDS:
            assert u[f"how_mk{x}_{h}"] == u[f"how_tk{h}"] and u[f"pnl_mk{x}_{h}"] == pytest.approx(u[f"pnl_tk{h}"])
    # the fixed point 60 s before the end (te = end - 59.5 s): a print above +1c after the end does not
    # fill the 60 s order, which is then held (the taker exit would be after the end)
    rows, _ = day(pr=[(300.2, "up", 0.99, "buy")])
    g = one(rows, "fixed", 1, END - 60)
    assert g["how_mk1_60"] == 0 and g["pnl_mk1_60"] == pytest.approx(g["pnl_settle"])


def test_fee_per_leg():
    assert M.fee(0.5) == pytest.approx(0.0175) and M.fee(0.9) == pytest.approx(0.0063)
    rows, _ = day(won=1.0)
    u = one(rows, "jump", 1, T0)
    bid = 0.30 + 106.1 / 1000
    assert u["pnl_tk5"] == pytest.approx(bid - 0.07 * bid * (1 - bid) - 0.5 - 0.07 * 0.25)  # two taker legs
    assert u["pnl_settle"] == pytest.approx(1 - 0.5 - 0.07 * 0.25)  # one taker leg


# ------------------------------------------------------------------ segments, freeze, units
def test_segments_by_market_start():
    def t(x):
        return int(pd.Timestamp(x, tz="UTC").timestamp())
    got = M.segment_of([t("2026-05-24 23:55"), t("2026-05-25 00:00"), t("2026-07-15 23:55"), t("2026-07-16 00:00"),
                        t("2026-08-15 23:55"), t("2026-08-16 00:00"), t("2026-08-29 23:55"), t("2026-08-30 00:00")])
    assert list(got) == ["", "A", "A", "B", "B", "C", "C", ""]
    blk = M.a_blocks()
    assert pd.Timestamp(blk[0][0], unit="s") == pd.Timestamp("2026-05-25") and not blk[0][2] and not blk[1][2]
    assert blk[2][2] and pd.Timestamp(blk[2][0], unit="s") == pd.Timestamp("2026-06-08")
    assert blk[-1][1] == t("2026-07-16")


def synth_rows(seed=0, flip_bc=False):
    """Model rows over A, B and C: the settle pnl rises with jump_bp (plus noise); 4 markets a day."""
    rng = np.random.default_rng(seed)
    parts = []
    for d in pd.date_range("2026-05-25", "2026-08-29", freq="D", tz="UTC"):
        for j in range(4):
            st = int(d.timestamp()) + 3600 * (j + 1)
            n = 60
            x = rng.normal(size=n)
            r = {c: rng.normal(size=n) for c in M.FEATURES}
            r["jump_bp"] = x
            seg = M.segment_of([st])[0]
            sgn = -1.0 if (flip_bc and seg in ("B", "C")) else 1.0
            pnl = sgn * 0.05 * x + rng.normal(0, 0.2, n)
            p = rng.uniform(0.3, 0.7, n)
            row = dict(day=d.strftime("%Y-%m-%d"), market=f"m{st}", start=st, t=st + np.sort(rng.uniform(20, 280, n)),
                       kind=np.where(np.arange(n) % 10 == 0, "fixed", "jump"), side=np.where(np.arange(n) % 2, 1, -1),
                       jdir=1, price=p, ask_size=50.0, won=(rng.random(n) < 0.5).astype(int), **r)
            for pol in M.POLICIES:
                row[f"pnl_{pol}"] = pnl if pol in ("settle", "tk5") else pnl - 0.05
                row[f"hold_{pol}"] = 10.0
                row[f"how_{pol}"] = 1
                row[f"sh20_{pol}"] = 20.0
            parts.append(pd.DataFrame(row))
    return M.compact(pd.concat(parts, ignore_index=True)[M.META + M.FEATURES + M.label_cols()])


@pytest.fixture
def fast_models(monkeypatch):
    monkeypatch.setattr(M, "MODELS", ("ridge",))
    monkeypatch.setattr(M, "POLICIES", ["settle", "tk5", "tk10"])
    monkeypatch.setattr(M, "QS", (0.2, 0.1))


def test_freeze_is_written_from_A_alone_before_B_and_C_are_read(tmp_path, monkeypatch, fast_models):
    rows = synth_rows()
    seen = []
    real_freeze, real_eval = M.freeze, M.evaluate

    def freeze(A, path, log=print):
        seen.append(("freeze", set(M.segment_of(A["start"].to_numpy(float)))))
        return real_freeze(A, path, log)

    def evaluate(rows_, spec, complete_c=True, log=print):
        seen.append(("evaluate", Path_exists(tmp_path)))
        return real_eval(rows_, spec, complete_c, log)

    def Path_exists(p):
        return sorted(x.name for x in p.rglob("*frozen.json"))

    monkeypatch.setattr(M, "freeze", freeze)
    monkeypatch.setattr(M, "evaluate", evaluate)
    _, spec, res = M.analyze(rows, tmp_path / "a" / "mix-hf.md", log=lambda s: None)
    assert seen[0] == ("freeze", {"A"}) and seen[1] == ("evaluate", ["mix-frozen.json"])
    assert len(spec["rules"]) == 3 and all(r["policy"] in ("settle", "tk5") for r in spec["rules"])
    assert res[0]["b_pass"] and res[0]["c_open"] and res[0]["c_pass"]
    # B and C pointing the other way change the B / C verdicts but not one byte of the freeze (bar its time)
    flipped = synth_rows(flip_bc=True)
    _, spec2, res2 = M.analyze(flipped, tmp_path / "b" / "mix-hf.md", log=lambda s: None)
    f1 = json.loads((tmp_path / "a" / "mix-frozen.json").read_text())
    f2 = json.loads((tmp_path / "b" / "mix-frozen.json").read_text())
    f1.pop("made"), f2.pop("made")
    assert f1 == f2
    assert not res2[0]["b_pass"] and not res2[0]["c_open"] and "C" not in res2[0]["stats"]
    # an existing freeze is not rewritten; with other A rows C stays closed
    other = synth_rows(seed=1)
    before = (tmp_path / "a" / "mix-frozen.json").read_text()
    _, spec3, res3 = M.analyze(other, tmp_path / "a" / "mix-hf.md", log=lambda s: None)
    assert (tmp_path / "a" / "mix-frozen.json").read_text() == before
    assert not any(o["c_open"] for o in res3)
    text = (tmp_path / "a" / "mix-hf.md").read_text(encoding="utf-8")
    assert "冻结文件已存在" in text and "指纹不符" in text
    assert (tmp_path / "a" / "mix-rows.csv.gz").exists()
    tr = pd.read_csv(tmp_path / "b" / "mix-rows.csv.gz")
    assert set(tr["segment"]) == {"A", "B"} and list(tr.columns) == M.TRADED_COLS


def test_lockbox_stays_closed_when_an_archive_of_C_was_not_read(tmp_path, fast_models):
    rows = synth_rows()
    _, _, res = M.analyze(rows, tmp_path / "f" / "mix-hf.md", failed=["2026-08-20"], log=lambda s: None)
    assert res[0]["b_pass"] and not any(o["c_open"] for o in res)
    # a failed A or B archive does not keep C closed
    _, _, res2 = M.analyze(rows, tmp_path / "g" / "mix-hf.md", failed=["2026-06-20"], skipped=["2026-08-30"],
                           log=lambda s: None)
    assert res2[0]["c_open"]


def test_q_cut_comes_from_the_A_out_of_sample_scores(fast_models):
    rows = synth_rows()
    A = M.model_rows(rows[M.segment_of(rows["start"].to_numpy(float)) == "A"])
    cand, scores = M.candidates(A, log=lambda s: None)
    oos = M.a_oos_mask(A["start"].to_numpy(float))
    sc = scores[("settle", "ridge")]
    assert np.isfinite(sc[oos]).all() and np.isnan(sc[~oos]).all()
    c = cand[(cand["policy"] == "settle") & (cand["q"] == 0.2)].iloc[0]
    assert c["cut"] == pytest.approx(np.sort(sc[oos])[::-1][int(np.ceil(0.2 * oos.sum())) - 1])
    assert c["n"] == (sc[oos] >= c["cut"]).sum()
    assert M.top_cut([1, 2, 3, 4, np.nan], 0.4) == 3.0


def test_per_day_units():
    t0 = S + 50.0
    tr = pd.DataFrame({"t": [t0, t0 + 5, t0 + 3 * 86400], "price": [0.40, 0.60, 0.50], "ask_size": [50.0, 8.0, 5.0],
                       "pnl_tk5": [0.10, -0.05, 0.02], "hold_tk5": [10.0, 30.0, 5.5], "sh20_tk5": [20.0, 8.0, 5.0]})
    u = M.units(tr, "tk5", 2)
    c = np.array([0.40 + fee(0.40), 0.60 + fee(0.60), 0.50 + fee(0.50)])
    assert u["trades"] == 1.5 and u["sh5"] == 7.5 and u["sh20"] == pytest.approx(16.5)
    assert u["cost5"] == pytest.approx(5 * c.sum() / 2) and u["cost20"] == pytest.approx((20 * c[0] + 8 * c[1] + 5 * c[2]) / 2)
    assert u["profit5"] == pytest.approx(5 * (0.10 - 0.05 + 0.02) / 2)
    assert u["profit20"] == pytest.approx((20 * 0.10 - 8 * 0.05 + 5 * 0.02) / 2)
    assert u["cap5"] == pytest.approx(5 * (c[0] * 10 + c[1] * 30 + c[2] * 5.5) / (2 * 86400))
    # day 1: the two positions overlap (5 s apart, held 10 and 30 s) -> peak = both; day 2: one
    assert u["peak5"] == pytest.approx(np.median([5 * (c[0] + c[1]), 5 * c[2]]))
    assert u["pmax5"] == pytest.approx(5 * (c[0] + c[1])) and u["pmax20"] == pytest.approx(20 * c[0] + 8 * c[1])
    # over 5 days (3 without a trade) the median day ties up nothing
    u5 = M.units(tr, "tk5", 5)
    assert u5["peak5"] == 0 and u5["pmax5"] == pytest.approx(5 * (c[0] + c[1])) and u5["trades"] == pytest.approx(0.6)
    assert M.units(tr.iloc[:0], "tk5", 3)["profit5"] == 0 and np.isnan(M.units(tr, "tk5", 0)["trades"])


def test_out_paths():
    a = M.out_paths("real/mix-hf.md")
    assert [str(x) for x in a] == ["real/mix-hf.md", "real/mix-frozen.json", "real/mix-rows.csv.gz"]
    b = M.out_paths("real/cross-mix-hf.md")
    assert [x.name for x in b] == ["cross-mix-hf.md", "cross-mix-frozen.json", "cross-mix-rows.csv.gz"]


# ------------------------------------------------------------------ end to end
def parquet_bytes(df):
    buf = io.BytesIO()
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), buf)
    return buf.getvalue()


def archive(path, dday):
    f = book().drop(columns=["up_ask_size", "down_ask_size", "up_bid_size", "down_bid_size"])
    f["up_ask_sizes"] = [[20.0, 7.0]] * len(f)
    f["down_ask_sizes"] = [[20.0]] * len(f)
    f["up_bid_sizes"] = [[10.0 + (i % 2), 3.0] for i in range(len(f))]
    f["down_bid_sizes"] = [[30.0]] * len(f)
    mk = pd.DataFrame({"timestamp_ms": [MS + 300_000], "market_id": ["m1"], "slug": [f"btc-updown-5m-{S}"],
                       "session_start_ts": [MS], "session_end_ts": [MS + 300_000], "chainlink_open_price": [60000.0],
                       "up_won": [1.0], "lifecycle_state": ["resolved"], "up_token_id": [f"{UP}-m1"],
                       "down_token_id": [f"{DN}-m1"]})
    b = binance().assign(exchange="binance", instrument="BTCUSDT", size=0.01, taker_side="buy")
    p = prints([(103.6, "up", 0.515, "buy")]).assign(exchange="polymarket", trade_ts_ms=lambda d: d["recv_ts_ms"])
    tr = pd.concat([b, p], ignore_index=True)[["trade_ts_ms", "recv_ts_ms", "exchange", "instrument", "price", "size",
                                               "taker_side"]]
    with tarfile.open(path, "w:gz") as tar:
        for name, df in ((f"dataset=polymarket_features_100ms/date={dday}/part-1.parquet", f),
                         (f"dataset=polymarket_market_100ms/date={dday}/part-2.parquet", mk),
                         (f"dataset=trades/date={dday}/part-3.parquet", tr)):
            raw = parquet_bytes(df)
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))
    return path


def test_subcommand_end_to_end(tmp_path, monkeypatch):
    dday = "2026-06-10"
    name = f"market_parquet_{dday}.tar.gz"
    src = archive(tmp_path / "src.tar.gz", dday)
    wd = tmp_path / "wd"
    fetched = []

    def fetch(n, dest=None):
        fetched.append(n)
        if n == "MANIFEST.txt":
            return f"# file  sha256  bytes\n{name}  abc  {src.stat().st_size}\n".encode()
        dest.write_bytes(src.read_bytes())
        return dest

    def download(url, dest):
        raise OSError("offline")

    monkeypatch.setattr(cross, "fetch", fetch)
    monkeypatch.setattr(hf, "download", download)
    out = tmp_path / "real" / "mix-hf.md"
    cross.main(["mix", "--workdir", str(wd), "--out", str(out)])
    assert fetched == ["MANIFEST.txt", name] and not (wd / name).exists()
    assert (wd / "mix-shards" / f"{dday}.parquet").exists()
    shard = pd.read_parquet(wd / "mix-shards" / f"{dday}.parquet")
    u = shard[(shard["kind"] == "jump") & (shard["side"] == 1)].iloc[0]
    assert u["price"] == pytest.approx(0.50) and u["how_mk1_5"] == 2 and u["pnl_mk1_5"] == pytest.approx(0.51 - 0.5 - 0.0175)
    assert shard["f_ridge5"].notna().all() and shard["dvol_rv"].notna().all()  # real/mix-inputs cover 06-10
    spec = json.loads((tmp_path / "real" / "mix-frozen.json").read_text())
    assert spec["rules"] == [] and spec["a_rows"] == (shard["kind"] != "random").sum() == 10
    text = out.read_text(encoding="utf-8")
    for s in ("## 判定", "没有一个候选规则", "## 成本结构", "| 2026-06-10 | 1 | 0 |", "K 线缺"):
        assert s in text, s
    assert (tmp_path / "real" / "mix-rows.csv.gz").exists()
