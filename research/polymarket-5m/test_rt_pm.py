import math

import numpy as np
import pytest

import binary as bo
import roundtrip as rt
import rt_pm
from test_roundtrip import make_panel, random_panel, set_up_book, slow_reference

T, OFF = rt.T, rt.OFF


def fee(p):
    return float(bo.taker_fee(p))


def entries_list(e):
    return [(int(m), int(s), int(sd), round(float(t), 6)) for m, s, sd, t in zip(e.market, e.second, e.side, e.target)]


def variant(vs, name):
    (v,) = [v for v in vs if v.name == name]
    return v


# ---------------------------------------------------------------- signals: timing and conditions


def test_reversion_signal_fires_one_second_after_the_row_and_buys_the_side_that_dropped():
    p = make_panel()                                          # Up 0.49 / 0.50, Binance flat
    set_up_book(p, 0, np.arange(100, T), 0.44, 0.45)         # Up mid -5c in the row stamped 100
    e = rt_pm.pm_entries(p, w=2, m=0.05, kind="rev", b=0.5)
    assert entries_list(e) == [(0, 101, 1, 0.495)]           # row 100 usable from 101; target = pre-move mid
    assert len(rt_pm.pm_entries(p, w=2, m=0.08, kind="rev", b=0.5).market) == 0
    assert len(rt_pm.pm_entries(p, w=2, m=0.05, kind="mom", b=1.0).market) == 0   # Binance did not move
    p = make_panel()
    set_up_book(p, 0, np.arange(100, T), 0.55, 0.56)         # Up +6c = Down -6c: buy Down
    e = rt_pm.pm_entries(p, w=5, m=0.05, kind="rev", b=1.0)
    assert entries_list(e) == [(0, 101, -1, 0.505)]          # Down's own pre-move mid


def test_binance_filter_quiet_for_reversion_and_confirming_for_momentum():
    w, sig = 2, 1e-4
    unit = sig * math.sqrt(w)                                 # 1 sigma over the window
    for move, rev05, rev1, mom in [(-0.3 * unit, True, True, False), (-0.8 * unit, False, True, False),
                                   (-1.2 * unit, False, False, True), (+1.2 * unit, False, False, False)]:
        p = make_panel()
        set_up_book(p, 0, np.arange(100, T), 0.44, 0.45)     # Up dropped 5c = Down rose 5c
        p.x[0, OFF + 100:] = move                             # Binance move in the kline of second 100
        assert (len(rt_pm.pm_entries(p, w, 0.05, "rev", 0.5).market) == 1) == rev05
        assert (len(rt_pm.pm_entries(p, w, 0.05, "rev", 1.0).market) == 1) == rev1
        e = rt_pm.pm_entries(p, w, 0.05, "mom", 1.0)
        assert (len(e.market) == 1) == mom
        if mom:
            assert list(e.side) == [-1] and list(e.second) == [101]   # Down rose with Binance down
    # the Binance window is the klines of seconds d - w .. d - 1: a move in second 101 is not seen at 101
    p = make_panel()
    set_up_book(p, 0, np.arange(100, T), 0.44, 0.45)
    p.x[0, OFF + 101:] = 5 * unit
    assert entries_list(rt_pm.pm_entries(p, w, 0.05, "rev", 0.5)) == [(0, 101, 1, 0.495)]
    p.x[0, OFF + 100:] = 5 * unit
    assert len(rt_pm.pm_entries(p, w, 0.05, "rev", 0.5).market) == 0


def test_momentum_buys_the_side_that_rose_with_binance():
    p = make_panel()
    set_up_book(p, 0, np.arange(150, T), 0.57, 0.58)         # Up +8c in row 150
    p.x[0, OFF + 148:] = 4e-4                                # Binance up 4 bp > 1 sigma sqrt(10) = 3.2 bp
    e = rt_pm.pm_entries(p, w=10, m=0.08, kind="mom", b=1.0)
    assert list(e.second) == [151] and list(e.side) == [1]
    assert len(rt_pm.pm_entries(p, w=10, m=0.12, kind="mom", b=1.0).market) == 0
    p.x[0, OFF + 148:] = -4e-4                               # Binance against the book: nothing
    assert len(rt_pm.pm_entries(p, w=10, m=0.08, kind="mom", b=1.0).market) == 0


def test_signals_use_only_rows_and_klines_stamped_before_the_decision():
    rng = np.random.default_rng(7)
    p, _ = random_panel(60, 21)
    p.x[:] = np.cumsum(rng.normal(0, 1e-4, (60, OFF + T)), axis=1).astype(np.float32)
    s0 = 160
    grid = [(w, m, kind, b) for w in rt_pm.PM_W for m in (0.03, 0.05) for kind, b in (("rev", 1.0), ("mom", 1.0))]
    base = [rt_pm.pm_entries(p, *g) for g in grid]
    assert sum(len(e.market) for e in base) > 100
    for c in rt.BOOK:                                         # change every row stamped >= s0
        p.a[c][:, s0:] = np.flip(p.a[c][:, s0:], axis=0)
    p.age[:, s0:] = np.flip(p.age[:, s0:], axis=0)
    p.x[:, OFF + s0:] += rng.normal(0, 1e-3, (60, T - s0)).astype(np.float32)
    changed = False
    for g, e0 in zip(grid, base):
        e1 = rt_pm.pm_entries(p, *g)
        k0, k1 = e0.second <= s0, e1.second <= s0             # decisions at <= s0 read stamps <= s0 - 1
        assert entries_list(rt.Entries(*(a[k0] for a in e0))) == entries_list(rt.Entries(*(a[k1] for a in e1)))
        changed |= entries_list(e0) != entries_list(e1)
    assert changed


def test_window_start_must_be_a_market_row_and_rows_must_be_usable():
    p = make_panel()
    set_up_book(p, 0, np.arange(200, T), 0.90, 0.91)         # a wrap from column -k would see 0.905
    for w in rt_pm.PM_W:
        e = rt_pm.pm_entries(p, w=w, m=0.03, kind="rev", b=1.0)
        assert e.second.min() == 201 and set(e.side) == {-1}     # only the real move at row 200
    p = make_panel()
    set_up_book(p, 0, np.arange(100, T), 0.44, 0.45)
    p.age[0, 100:106] = -1                                     # no fresh row at 100..105
    for c in rt.BOOK:
        p.a[c][0, 100:106] = np.nan
    assert list(rt_pm.pm_entries(p, w=10, m=0.05, kind="rev", b=1.0).second) == [107]
    p = make_panel()
    set_up_book(p, 0, np.arange(100, T), 0.44, 0.45)
    set_up_book(p, 0, [100], 0.46, 0.45)                     # crossed row: no mid
    assert list(rt_pm.pm_entries(p, w=2, m=0.05, kind="rev", b=1.0).second) == [102]


def test_unmirrored_books_where_both_sides_qualify_are_skipped():
    p = make_panel()
    set_up_book(p, 0, np.arange(100, T), 0.44, 0.45)
    p.bd[0, 100:], p.ad[0, 100:] = 0.44, 0.45                 # Down dropped too (not a mirror)
    assert len(rt_pm.pm_entries(p, w=2, m=0.05, kind="rev", b=1.0).market) == 0


def test_spacing_and_time_left_windows():
    p = make_panel(M=2)
    rows = np.arange(T)
    step = (rows // 3) % 2 == 1                               # Up flips 0.49/0.50 <-> 0.43/0.44 every 3 s
    set_up_book(p, 0, rows[step], 0.43, 0.44)
    set_up_book(p, 1, np.arange(245, T), 0.44, 0.45)         # usable at 246: 54 s left
    e = rt_pm.pm_entries(p, w=2, m=0.05, kind="rev", b=1.0)
    s = e.second[e.market == 0]
    assert len(s) > 5 and np.all(np.diff(s) >= rt_pm.SPACING) and set(e.side[e.market == 0]) == {-1, 1}
    assert list(e.second[e.market == 1]) == [246]
    g = rt_pm.pm_entries(p, w=2, m=0.05, kind="rev", b=1.0, window="gt60")
    assert len(g.second[g.market == 1]) == 0 and g.second.max() <= 239


def slow_signals(p, w, m, kind, b, window="all"):
    """Per-(market, second) loop written from the rules in the module docstring."""
    out = []
    last = 239 if window == "gt60" else 282
    for mk in range(p.M):
        kept = -10**9
        for d in range(1, last + 1):
            a, z = d - 1 - w, d - 1
            if a < 0:
                continue

            def mid(k, s):
                if p.age[mk, s] < 0 or not p.au[mk, s] > p.bu[mk, s]:
                    return np.nan
                return float(np.float32((p.bu[mk, s] + p.au[mk, s]) / 2 if k == 0 else (p.bd[mk, s] + p.ad[mk, s]) / 2))

            r = float(p.x[mk, OFF + z]) - float(p.x[mk, OFF + a])
            sg = float(p.sig[mk, OFF + z]) * math.sqrt(w)
            hits = []
            for k in (0, 1):
                dm = mid(k, z) - mid(k, a)
                if not np.isfinite(dm) or not np.isfinite(r) or not np.isfinite(sg):
                    continue
                if kind == "rev" and dm <= -m + 1e-6 and abs(r) < b * sg:
                    hits.append(k)
                if kind == "mom" and dm >= m - 1e-6 and (r >= b * sg if k == 0 else r <= -b * sg):
                    hits.append(k)
            if len(hits) != 1 or d < kept + 10:
                continue
            kept = d
            k = hits[0]
            out.append((mk, d, 1 if k == 0 else -1, round(mid(k, a), 6)))
    return out


@pytest.mark.parametrize("w,m,kind,b,window", [(2, 0.03, "rev", 0.5, "all"), (5, 0.03, "rev", 1.0, "gt60"),
                                               (10, 0.03, "mom", 1.0, "all"), (30, 0.05, "mom", 1.0, "gt60"),
                                               (30, 0.08, "rev", 1.0, "all")])
def test_vectorised_signals_match_a_slow_reference(w, m, kind, b, window):
    rng = np.random.default_rng(9)
    p, _ = random_panel(40, 13)
    p.x[:] = np.cumsum(rng.normal(0, 1e-4, (40, OFF + T)), axis=1).astype(np.float32)
    p.sig[:] = rng.uniform(0.5e-4, 1.5e-4, (40, OFF + T)).astype(np.float32)
    p.sig[3, OFF + 50:OFF + 80] = np.nan
    e = rt_pm.pm_entries(p, w, m, kind, b, window)
    ref = slow_signals(p, w, m, kind, b, window)
    assert len(ref) >= 15
    assert entries_list(e) == ref


# ---------------------------------------------------------------- exits through the engine


def test_reversion_exits_converge_hold_and_stop():
    vs = rt_pm.family3_variants()
    p = make_panel()
    set_up_book(p, 0, np.arange(100, T), 0.44, 0.45)         # drop seen at 101, entry ask row 102
    set_up_book(p, 0, np.arange(120, T), 0.49, 0.50)         # back to the pre-move mid at row 120
    eng = rt.Engine(p)
    v = variant(vs, "w2-m5-b0.5-all-cv-premid-e0-mx30")
    tr = eng.simulate(v.signal(p), v.exit)
    assert tr["second"][0] == 101 and tr["entry_row"][0] == 102 and tr["entry_px"][0] == pytest.approx(0.45)
    assert rt.REASONS[tr["reason"][0]] == "converge" and tr["exit_row"][0] == 122   # decided 121 from row 120
    assert tr["pnl"][0] == pytest.approx(0.49 - fee(0.49) - 0.45 - fee(0.45), abs=1e-6)
    v = variant(vs, "w2-m5-b0.5-all-cv-premid-e0-mx10")       # not back within 10 s: sell at d + 10
    tr = eng.simulate(v.signal(p), v.exit)
    assert rt.REASONS[tr["reason"][0]] == "hold" and tr["exit_row"][0] == 112
    v = variant(vs, "w2-m5-b0.5-all-h5")
    tr = eng.simulate(v.signal(p), v.exit)
    assert tr["exit_row"][0] == 107 and tr["exit_px"][0] == pytest.approx(0.44)
    # stop 5c below the entry row's mid (0.445): the mid 0.395 at row 110 is seen at 111, sold at 112
    set_up_book(p, 0, np.arange(110, 120), 0.39, 0.40)
    v = variant(vs, "w2-m5-b0.5-all-cv-premid-e0-mx60_sl5")
    tr = rt.Engine(p).simulate(v.signal(p), v.exit)
    assert rt.REASONS[tr["reason"][0]] == "stop" and tr["exit_row"][0] == 112
    v = variant(vs, "w2-m5-b0.5-all-cv-premid-e0-mx60")
    tr = rt.Engine(p).simulate(v.signal(p), v.exit)
    assert rt.REASONS[tr["reason"][0]] == "converge" and tr["exit_row"][0] == 122
    v = variant(vs, "w2-m5-b0.5-all-settle")
    tr = rt.Engine(p).simulate(v.signal(p), v.exit)
    assert rt.REASONS[tr["reason"][0]] == "settle" and tr["pnl"][0] == pytest.approx(1 - 0.45 - fee(0.45))


def test_momentum_exits_hold_forced_and_stop():
    vs = rt_pm.family4_variants()
    p = make_panel()
    set_up_book(p, 0, np.arange(200, T), 0.57, 0.58)
    p.x[0, OFF + 199:] = 4e-4
    v = variant(vs, "w2-m8-z1-all-h30")
    tr = rt.Engine(p).simulate(v.signal(p), v.exit)
    assert tr["second"][0] == 201 and tr["entry_px"][0] == pytest.approx(0.58) and tr["exit_row"][0] == 232
    v = variant(vs, "w2-m8-z1-all-h120")
    tr = rt.Engine(p).simulate(v.signal(p), v.exit)
    assert rt.REASONS[tr["reason"][0]] == "forced" and tr["exit_row"][0] == rt.FORCE_ROW
    set_up_book(p, 0, np.arange(210, T), 0.52, 0.53)         # mid 0.525 = 0.575 - 5c
    v = variant(vs, "w2-m8-z1-all-h60_sl5")
    tr = rt.Engine(p).simulate(v.signal(p), v.exit)
    assert rt.REASONS[tr["reason"][0]] == "stop" and tr["exit_row"][0] == 212


@pytest.mark.parametrize("ex", rt_pm.f3_exits(), ids=lambda e: e.label())
def test_engine_on_pm_signals_matches_the_slow_trade_reference(ex):
    rng = np.random.default_rng(4)
    p, _ = random_panel(40, 17)
    p.x[:] = np.cumsum(rng.normal(0, 1e-4, (40, OFF + T)), axis=1).astype(np.float32)
    e = rt_pm.pm_entries(p, w=5, m=0.03, kind="rev", b=1.0)
    tr = rt.Engine(p).simulate(e, ex)
    ref = slow_reference(p, e, ex)
    got = list(zip(tr["market"], tr["second"], tr["entry_row"], tr["exit_row"], tr["pnl"],
                   np.asarray(rt.REASONS)[tr["reason"]]))
    assert len(got) == len(ref) >= 30
    for g, r in zip(got, ref):
        assert g[:4] == r[:4] and g[5] == r[5]
        assert g[4] == pytest.approx(r[4], abs=1e-6)


# ---------------------------------------------------------------- grid and end to end


def test_grids_are_the_pre_registered_ones():
    f3, f4 = rt_pm.family3_variants(), rt_pm.family4_variants()
    assert len(f3) == 4 * 5 * 2 * 2 * 21 == 1680 and len(f4) == 4 * 5 * 2 * 11 == 440
    allv = rt_pm.variants()
    assert len(allv) == 2120 and len({v.id for v in allv}) == 2120
    assert sum(v.control for v in f3) == 80 and sum(v.control for v in f4) == 40
    assert len(rt.group_by_key(f3)) == 80 and len(rt.group_by_key(f4)) == 40
    assert {v.exit.h for v in allv if v.exit.kind != "settle"} == {5, 10, 30, 60, 120}
    assert {v.exit.stop for v in allv} == {0.0, 0.05}
    assert {v.exit.target for v in f3 if v.exit.kind == "converge"} == {"entry"}
    assert not any(v.exit.kind == "converge" for v in f4)
    assert {v.params["m"] for v in allv} == {0.03, 0.05, 0.08, 0.12, 0.15}
    assert {v.params["b_sigma"] for v in f3} == {0.5, 1.0} and {v.params["w"] for v in allv} == {2, 5, 10, 30}


def test_end_to_end_run_and_select():
    rng = np.random.default_rng(5)
    M = 60
    p, _ = random_panel(M, 23)
    p.split[M // 2:] = 1
    p.x[:] = np.cumsum(rng.normal(0, 1e-4, (M, OFF + T)), axis=1).astype(np.float32)
    vs = [v for v in rt_pm.variants() if v.params["w"] == 5 and v.params["m"] == 0.03]
    st, kept = rt.run_variants(p, vs, keep={vs[0].id}, progress=False)
    assert len(st) == len(vs) == 2 * 2 * 21 + 2 * 11
    assert set(st["family"]) == {rt_pm.F3, rt_pm.F4}
    one = rt.split_stats(p, kept[vs[0].id])
    assert st.loc[0, "n_A"] == one["n_A"] > 0 and st.loc[0, "mean_A"] == pytest.approx(one["mean_A"])
    st2, _ = rt.run_variants(p, vs, workers=2, progress=False)
    import pandas as pd
    pd.testing.assert_frame_equal(st, st2)
    sel, info = rt.select(st)
    assert info["N"] == len(vs)
