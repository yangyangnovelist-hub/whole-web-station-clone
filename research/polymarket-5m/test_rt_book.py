import numpy as np
import pytest

import binary as bo
import roundtrip as rt
import rt_book as rb
from test_roundtrip import make_panel, random_panel, set_up_book, slow_reference

T, OFF = rt.T, rt.OFF


def fee(p):
    return float(bo.taker_fee(p))


def elist(e):
    return [(int(m), int(s), int(sd)) for m, s, sd in zip(e.market, e.second, e.side)]


def set_sizes(p, m, rows, up_bid, up_ask):
    """Up best-bid / best-ask sizes and their Down mirror (su = sad, sd = sau)."""
    p.su[m, rows], p.sad[m, rows] = up_bid, up_bid
    p.sau[m, rows], p.sd[m, rows] = up_ask, up_ask


def variant(vs, name):
    (v,) = [v for v in vs if v.name == name]
    return v


# ---------------------------------------------------------------- family 5 signals


def test_size_imbalance_fires_one_second_after_the_row_and_buys_the_heavy_bid_side():
    p = make_panel(M=2)                                      # sizes 100 / 100: imbalance 0
    set_sizes(p, 0, np.arange(100, T), 300.0, 100.0)         # Up (300 - 100) / 400 = 0.5 from row 100
    set_sizes(p, 1, np.arange(150, T), 100.0, 1900.0)        # Down (1900 - 100) / 2000 = 0.9 from row 150
    e = rb.imbalance_entries(p, "size", 0.5)
    first = {}
    for m, s, sd in elist(e):
        first.setdefault(m, (s, sd))
    assert first == {0: (101, 1), 1: (151, -1)}              # row s usable from s + 1
    assert [s for m, s, _ in elist(e) if m == 0] == list(range(101, 283, 10))   # >= 10 s apart, d <= 282
    e7 = rb.imbalance_entries(p, "size", 0.7)
    assert {m for m, _, _ in elist(e7)} == {1}
    assert elist(rb.imbalance_entries(p, "size", 0.9))[0] == (1, 151, -1)       # >= thr (0.9 exactly)


def test_depth_usdc_and_depth_in_shares():
    p = make_panel()                                         # rows < 100: 0.49 / 0.50, du = dd = 100
    set_up_book(p, 0, np.arange(100, T), 0.79, 0.80)         # Up favourite from row 100: Down bid 0.20
    p.du[0, 100:], p.dd[0, 100:] = 800.0, 200.0             # USDC: (800 - 200) / 1000 = 0.6 -> Up
    assert elist(rb.imbalance_entries(p, "depth", 0.5))[0] == (0, 101, 1)
    assert len(rb.imbalance_entries(p, "depth", 0.7).market) == 0
    # shares: 800 / 0.79 = 1012.7 vs 200 / 0.20 = 1000 -> 0.006: no signal
    assert len(rb.imbalance_entries(p, "depth_sh", 0.5).market) == 0
    p.dd[0, 100:] = 20.0                                     # Down 100 shares vs Up 1012.7: 0.82 -> Up
    sc = rb.heavy_bid_score(p, "depth_sh", [100])
    assert sc[0, 0, 0] == pytest.approx((800 / 0.79 - 100) / (800 / 0.79 + 100), rel=1e-5)
    assert sc[1, 0, 0] == pytest.approx(-sc[0, 0, 0])
    assert elist(rb.imbalance_entries(p, "depth_sh", 0.7))[0] == (0, 101, 1)
    p.du[0, 200:], p.dd[0, 200:] = 0.0, 50.0                 # empty Up bid side: Down 'depth' = 1
    assert (0, 201, -1) in elist(rb.imbalance_entries(p, "depth", 0.9))


def test_imbalance_skips_stale_crossed_and_ambiguous_rows():
    p = make_panel()
    set_sizes(p, 0, np.arange(T), 900.0, 100.0)              # Up 0.8 everywhere
    p.age[0, :50] = -1                                        # no fresh row in columns 0..49
    p.au[0, 50] = p.bu[0, 50]                                 # locked row at column 50
    assert elist(rb.imbalance_entries(p, "size", 0.5))[0] == (0, 52, 1)
    p = make_panel()
    p.su[0, :], p.sau[0, :] = 900.0, 100.0                   # Up 0.8 and (unmirrored) Down 0.8 too
    p.sd[0, :], p.sad[0, :] = 900.0, 100.0
    assert len(rb.imbalance_entries(p, "size", 0.5).market) == 0
    p.sad[0, :] = np.nan                                      # Down ask side empty: Down undefined
    assert elist(rb.imbalance_entries(p, "size", 0.5))[0] == (0, 1, 1)


def test_family5_signals_are_causal():
    p, rng = random_panel(40, 3)
    for c in ("su", "sd", "sau", "sad"):
        p.a[c][:] = rng.lognormal(4, 1.5, (40, T)).astype(np.float32)
    p.du[:] = rng.lognormal(6, 1, (40, T)).astype(np.float32)
    p.dd[:] = rng.lognormal(6, 1, (40, T)).astype(np.float32)
    s0 = 140
    for measure in rb.F5_MEASURES:
        for thr in rb.F5_THR:
            base = rb.imbalance_entries(p, measure, thr)
            q = rt.Panel(**{k: np.array(v, copy=True) for k, v in p.a.items()})
            for c in rt.BOOK:                                 # change every row stamped >= s0 - 1
                q.a[c][:, s0 - 1:] = rng.permutation(q.a[c][:, s0 - 1:].ravel()).reshape(40, -1)
            q.age[:, s0 - 1:] = rng.choice([-1, 0, 3], (40, T - s0 + 1)).astype(np.int8)
            pert = rb.imbalance_entries(q, measure, thr)
            k0, k1 = base.second < s0, pert.second < s0       # decisions <= s0 - 1 read rows <= s0 - 2
            assert elist(rt.Entries(base.market[k0], base.second[k0], base.side[k0])) == \
                elist(rt.Entries(pert.market[k1], pert.second[k1], pert.side[k1]))
            assert len(base.market) > 0


# ---------------------------------------------------------------- family 7 signals


def test_bucket_entries_one_decision_second_reading_row_d_minus_1():
    p = make_panel(M=3)                                       # Up 0.49 / 0.50 -> mid 0.495
    set_up_book(p, 1, np.arange(T), 0.84, 0.86)               # Up 0.85 favourite
    set_up_book(p, 2, np.arange(T), 0.10, 0.12)               # Up 0.11 underdog
    e = rb.bucket_entries(p, 240, "dog", 0.4, 0.6)            # d = 60
    assert elist(e) == [(0, 60, 1)]                           # Up 0.495 < 0.5
    assert elist(rb.bucket_entries(p, 240, "fav", 0.4, 0.6)) == [(0, 60, -1)]   # Down 0.505
    assert elist(rb.bucket_entries(p, 120, "fav", 0.8, 0.95)) == [(1, 180, 1), (2, 180, -1)]   # Down 0.89
    assert elist(rb.bucket_entries(p, 30, "dog", 0.05, 0.2)) == [(1, 270, -1), (2, 270, 1)]
    assert len(rb.bucket_entries(p, 30, "dog", 0.2, 0.4).market) == 0
    # only column d - 1 matters
    q = make_panel()
    set_up_book(q, 0, np.arange(60, T), 0.84, 0.86)           # rows from 60 on: not usable at d = 60
    assert elist(rb.bucket_entries(q, 240, "dog", 0.4, 0.6)) == [(0, 60, 1)]
    set_up_book(q, 0, [59], 0.84, 0.86)
    assert len(rb.bucket_entries(q, 240, "dog", 0.4, 0.6).market) == 0
    assert elist(rb.bucket_entries(q, 240, "fav", 0.8, 0.95)) == [(0, 60, 1)]
    q.age[0, 59] = -1                                         # stale / missing row at d - 1: no trade
    assert len(rb.bucket_entries(q, 240, "fav", 0.8, 0.95).market) == 0


@pytest.mark.parametrize("bid,ask,cells", [
    (0.19, 0.21, {("dog", 0.2, 0.4): 1, ("fav", 0.6, 0.8): -1}),        # Up 0.20 / Down 0.80
    (0.39, 0.41, {("dog", 0.4, 0.6): 1, ("fav", 0.4, 0.6): -1}),        # Up 0.40 / Down 0.60
    (0.49, 0.51, {}),                                                    # 0.50 / 0.50: neither
    (0.04, 0.06, {("dog", 0.05, 0.2): 1, ("fav", 0.8, 0.95): -1}),      # Up 0.05 / Down 0.95
    (0.03, 0.05, {}),                                                    # 0.04: below every bucket
])
def test_bucket_boundaries_mirror_up_and_down(bid, ask, cells):
    p = make_panel()
    set_up_book(p, 0, np.arange(T), bid, ask)
    got = {}
    for role, lo, hi in rb.f7_cells():
        e = rb.bucket_entries(p, 90, role, lo, hi)
        if len(e.market):
            got[(role, lo, hi)] = int(e.side[0])
    assert got == cells


def test_bucket_signals_are_causal():
    p, rng = random_panel(60, 5)
    for tau in rb.F7_TAUS:
        d = T - tau
        q = rt.Panel(**{k: np.array(v, copy=True) for k, v in p.a.items()})
        for c in rt.BOOK:
            q.a[c][:, d:] = rng.permutation(q.a[c][:, d:].ravel()).reshape(60, -1)
        for role, lo, hi in rb.f7_cells():
            assert elist(rb.bucket_entries(p, tau, role, lo, hi)) == elist(rb.bucket_entries(q, tau, role, lo, hi))


# ---------------------------------------------------------------- exits


def test_f7_exits_sell_at_tau2_left_or_15_s_left_or_settle():
    hs = lambda tau: [(t2, ex.kind, ex.h) for t2, ex in rb.f7_exits(tau)]
    assert hs(240) == [(225, "hold", 15), (210, "hold", 30), (180, "hold", 60), (15, "hold", 224), (None, "settle", 0)]
    assert hs(60) == [(45, "hold", 15), (30, "hold", 30), (15, "hold", 44), (None, "settle", 0)]
    assert hs(45) == [(30, "hold", 15), (15, "hold", 29), (None, "settle", 0)]   # tau - 30 = 15 kept once
    assert hs(30) == [(15, "hold", 14), (None, "settle", 0)]


def test_f7_trades_fill_at_the_right_rows():
    p = make_panel()
    s = np.arange(T)
    set_up_book(p, 0, s, 0.30 + 0.001 * s, 0.31 + 0.001 * s)  # Up 0.3.. underdog, rising
    vs = rb.family7_variants(p)
    eng = rt.Engine(p)
    tr = eng.simulate(rb.bucket_entries(p, 240, "dog", 0.2, 0.4), variant(vs, "t240-dog20-40-x225").exit)
    assert tr["second"][0] == 60 and tr["entry_row"][0] == 61 and tr["exit_row"][0] == 76   # e = 75
    assert tr["entry_px"][0] == pytest.approx(0.371, abs=1e-6) and tr["exit_px"][0] == pytest.approx(0.376, abs=1e-6)
    assert tr["pnl"][0] == pytest.approx(0.376 - fee(0.376) - 0.371 - fee(0.371), abs=1e-6)
    tr = eng.simulate(rb.bucket_entries(p, 240, "dog", 0.2, 0.4), variant(vs, "t240-dog20-40-x15").exit)
    assert tr["exit_row"][0] == 285 and rt.REASONS[tr["reason"][0]] == "hold"   # 15 s left
    tr = eng.simulate(rb.bucket_entries(p, 240, "dog", 0.2, 0.4), variant(vs, "t240-dog20-40-settle").exit)
    assert tr["exit_row"][0] == -1 and tr["pnl"][0] == pytest.approx(1 - 0.371 - fee(0.371), abs=1e-6)
    # tau 30 (d = 270): column 269 has Up 0.569 / 0.579, mid 0.574 = favourite in (0.5, 0.6];
    # Down 0.426 is the underdog in [0.4, 0.5)
    assert elist(rb.bucket_entries(p, 30, "dog", 0.4, 0.6)) == [(0, 270, -1)]
    assert len(rb.bucket_entries(p, 30, "fav", 0.6, 0.8).market) == 0
    tr = eng.simulate(rb.bucket_entries(p, 30, "fav", 0.4, 0.6), variant(vs, "t30-fav40-60-x15").exit)
    assert tr["entry_row"][0] == 271 and tr["exit_row"][0] == 285
    assert tr["entry_px"][0] == pytest.approx(0.581, abs=1e-6) and tr["exit_px"][0] == pytest.approx(0.585, abs=1e-6)


def test_f5_hold_exit_and_one_position_rule():
    p = make_panel()
    set_sizes(p, 0, np.arange(T), 300.0, 100.0)              # Up 0.5 everywhere: signals 1, 11, 21, ...
    vs = rb.family5_variants(p)
    v = variant(vs, "size-i50-h30")
    tr = rt.Engine(p).simulate(v.signal(p), v.exit)
    # first trade: decision 1, entry row 2, exit decided 31 -> row 32; next kept signal >= 33 is 41
    assert list(tr["second"][:3]) == [1, 41, 81] and list(tr["entry_row"][:2]) == [2, 42]
    assert list(tr["exit_row"][:2]) == [32, 72]
    assert np.allclose(tr["pnl"], 0.49 - fee(0.49) - 0.50 - fee(0.50))
    v = variant(vs, "size-i50-settle")
    tr = rt.Engine(p).simulate(v.signal(p), v.exit)
    assert len(tr["market"]) == 1 and tr["pnl"][0] == pytest.approx(1 - 0.5 - fee(0.5))


@pytest.mark.parametrize("name", ["size-i50-h5", "depth-i70-h30", "depth_sh-i50-h60"])
def test_family5_engine_matches_slow_reference(name):
    p, rng = random_panel(30, 11)
    p.su[:] = rng.lognormal(4, 1.5, (30, T)).astype(np.float32)
    p.sau[:] = rng.lognormal(4, 1.5, (30, T)).astype(np.float32)
    p.du[:] = rng.lognormal(6, 1, (30, T)).astype(np.float32)
    p.dd[:] = rng.lognormal(6, 1, (30, T)).astype(np.float32)
    v = variant(rb.family5_variants(p), name)
    e = v.signal(p)
    tr = rt.Engine(p).simulate(e, v.exit)
    ref = slow_reference(p, e, v.exit)
    assert len(ref) == len(tr["market"]) > 0
    assert np.allclose([r[4] for r in ref], tr["pnl"], atol=1e-9)
    assert [r[3] for r in ref] == list(tr["exit_row"])


@pytest.mark.parametrize("name", ["t240-dog40-60-x210", "t90-fav60-80-x15", "t45-dog20-40-x30", "t120-fav40-60-settle"])
def test_family7_engine_matches_slow_reference(name):
    p, _ = random_panel(200, 13)
    v = variant(rb.family7_variants(p), name)
    e = v.signal(p)
    tr = rt.Engine(p).simulate(e, v.exit)
    ref = slow_reference(p, e, v.exit)
    assert len(ref) == len(tr["market"]) > 0
    assert np.allclose([r[4] for r in ref], tr["pnl"], atol=1e-9)


# ---------------------------------------------------------------- grids and end to end


def test_grids():
    v5, v7 = rb.family5_variants(), rb.family7_variants()
    assert len(v5) == 3 * 3 * 5 == 45 and sum(v.control for v in v5) == 9
    assert sum(v.params["listed"] for v in v5) == 30
    assert len(rb.f7_cells()) == 6
    assert {(r, lo) for r, lo, _ in rb.f7_cells()} == {("dog", 0.05), ("dog", 0.2), ("dog", 0.4),
                                                       ("fav", 0.4), ("fav", 0.6), ("fav", 0.8)}
    assert len(v7) == 6 * (5 + 5 + 5 + 5 + 4 + 3 + 2) == 174 and sum(v.control for v in v7) == 42
    ids = [v.id for v in v5 + v7]
    assert len(set(ids)) == len(ids) == 219
    assert all(v.exit.kind == "settle" for v in v5 + v7 if v.control)
    for v in v7:                                              # every sale is filled by row 285
        if v.exit.kind == "hold":
            assert T - v.params["tau"] + v.exit.h + 1 <= rt.FORCE_ROW
            assert v.params["tau2"] >= 15


def test_end_to_end_run_variants_and_selection():
    p, rng = random_panel(80, 21)
    p.split[40:] = 1
    vs = rb.variants(p)
    st, kept = rt.run_variants(p, vs, keep=[vs[0].id], workers=1, progress=False)
    assert len(st) == len(vs) and set(st["family"]) == {rb.F5, rb.F7}
    assert (st["n_A"] + st["n_B"] > 0).any()
    sel, info = rt.select(st)
    assert info["N"] == 219
    assert vs[0].id in kept
