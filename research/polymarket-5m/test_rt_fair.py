import math

import numpy as np
import pandas as pd
import pytest

import binary as bo
import roundtrip as rt
import rt_fair as rf

T, OFF = rt.T, rt.OFF
START0 = 1_775_001_600  # 2026-04-01 00:00 UTC (split A)


def make_panel(M=1, bid=0.49, ask=0.50, size=100.0, up_won=1, fair=0.5, sig=1e-4):
    f = lambda v: np.full((M, T), v, np.float32)
    start = START0 + 300 * np.arange(M, dtype=np.int64)
    return rt.Panel(cid=np.array([f"c{i}" for i in range(M)]), slug=np.array([f"s{i}" for i in range(M)]),
                    start=start, up_won=np.full(M, up_won, np.int8), up_inferred=np.full(M, up_won, np.int8),
                    n_ticks=np.full(M, T, np.int16), split=np.zeros(M, np.int8),
                    day=(start // 86400).astype(np.int32), ref=np.full(M, 70000.0), age=np.zeros((M, T), np.int8),
                    fair=f(fair), x=np.zeros((M, OFF + T), np.float32), sig=np.full((M, OFF + T), sig, np.float32),
                    vol=np.zeros((M, OFF + T), np.float32), buy=np.zeros((M, OFF + T), np.float32),
                    off=np.int64(OFF), bu=f(bid), au=f(ask), bd=f(1 - ask), ad=f(1 - bid), su=f(size),
                    sd=f(size), sau=f(size), sad=f(size), du=f(100.0), dd=f(100.0))


def set_up_book(p, m, rows, bid, ask):
    """Set the Up book (and its Down mirror) on rows."""
    p.bu[m, rows], p.au[m, rows] = bid, ask
    p.bd[m, rows], p.ad[m, rows] = 1 - np.asarray(ask), 1 - np.asarray(bid)


def sig_list(e):
    return list(zip(e.market.tolist(), e.second.tolist(), e.side.tolist()))


def variant(name):
    return next(v for v in rf.variants() if v.name == name)


def fee(x):
    return float(bo.taker_fee(x))


# ---------------------------------------------------------------- grid


def test_grid_is_the_preregistered_one():
    vs = rf.variants()
    assert len(vs) == 6 * 3 * 3 * 17 == 918
    assert len({v.id for v in vs}) == len(vs)
    assert len({v.key for v in vs}) == 54
    assert sum(v.control for v in vs) == 54
    assert all(v.family == rf.F2 for v in vs)
    ex = rf.f2_exits()
    cv = [e for e in ex if e.kind == "converge"]
    assert sorted({(e.eps, e.h) for e in cv}) == sorted((e, h) for e in (0.0, 0.01, 0.02) for h in (10, 30, 60, 120))
    assert all(e.target == "fair" and e.stop == 0 for e in cv)
    assert sorted(e.h for e in ex if e.kind == "hold") == [5, 10, 30, 60]
    assert all(e.stop == 0 for e in ex) and sum(e.kind == "settle" for e in ex) == 1
    assert {v.params["g"] for v in vs} == {0.02, 0.03, 0.04, 0.06, 0.08, 0.10}
    assert {v.params["move_w"] for v in vs} == {0, 1, 3}
    assert {v.params["window"] for v in vs} == {"all", "240-15", "120-15"}


def test_time_left_windows():
    p = make_panel(bid=0.44, ask=0.45)                     # 5c gap on every row
    for window, first in (("all", 1), ("240-15", 60), ("120-15", 180)):
        e = rf.gap_entries(p, 0.04, 0, window)
        assert e.second.tolist() == list(range(first, 283))
        assert set(e.side.tolist()) == {1}


# ---------------------------------------------------------------- signal timing and data use


def test_gap_uses_row_stamped_d_minus_1_and_fair_known_at_d():
    p = make_panel()
    set_up_book(p, 0, [100], 0.44, 0.45)                  # Up ask 5c under fair 0.5 on row 100 only
    set_up_book(p, 0, [200], 0.55, 0.56)                  # Down ask = 0.45 on row 200 only
    assert sig_list(rf.gap_entries(p, 0.04)) == [(0, 101, 1), (0, 201, -1)]
    assert sig_list(rf.gap_entries(p, 0.05)) == [(0, 101, 1), (0, 201, -1)]   # gap >= g inclusive
    assert sig_list(rf.gap_entries(p, 0.06)) == []
    # the fair price at d comes from the kline of d - 1: a jump in the kline of 150 opens the gap at 151
    q = make_panel()
    q.x[0, OFF + 150:] = 5e-3
    q.fair[:] = rt.fair_from(q.x, q.sig)
    e = rf.gap_entries(q, 0.10)
    assert e.second[0] == 151 and e.side[0] == 1
    assert q.fair[0, 150] == pytest.approx(0.5) and q.fair[0, 151] > 0.99


def test_rows_older_than_5_s_at_the_decision_are_skipped():
    raw = {c: np.full((1, T), v, np.float32) for c, v in
           (("bu", 0.49), ("au", 0.50), ("bd", 0.50), ("ad", 0.51), ("su", 100.0), ("sd", 100.0),
            ("sau", 100.0), ("sad", 100.0), ("du", 100.0), ("dd", 100.0))}
    raw["bu"][0, 99], raw["au"][0, 99] = 0.44, 0.45      # gap row stamped 99
    raw["bd"][0, 99], raw["ad"][0, 99] = 0.55, 0.56
    present = np.ones((1, T), bool)
    present[0, 100:111] = False                           # no rows stamped 100..110
    book, age = rt.ffill_rows(raw, present)
    p = make_panel()
    p.a.update(book)
    p.a["age"] = age
    # row 99 is usable from 100; at d = 104 it is 5 s old (ok), at 105 6 s old (skipped)
    assert rf.gap_entries(p, 0.04).second.tolist() == [100, 101, 102, 103, 104]


def test_crossed_or_locked_rows_give_no_signal():
    p = make_panel()
    set_up_book(p, 0, [100], 0.45, 0.45)                  # locked, ask 5c under fair
    set_up_book(p, 0, [150], 0.46, 0.45)                  # crossed
    set_up_book(p, 0, [200], 0.44, 0.45)                  # fine
    p.bd[0, 250], p.ad[0, 250] = 0.56, 0.56               # Down book locked (books not mirrors)
    p.bu[0, 250], p.au[0, 250] = 0.44, 0.45
    assert rf.gap_entries(p, 0.04).second.tolist() == [201]


def test_both_sides_on_one_row_are_skipped():
    p = make_panel()
    p.bu[0, 100], p.au[0, 100] = 0.40, 0.45               # Up ask 5c under 0.5
    p.bd[0, 100], p.ad[0, 100] = 0.40, 0.45               # Down ask 5c under 0.5 (non-mirrored books)
    assert rf.gap_entries(p, 0.04).second.tolist() == []


def test_binance_move_filter_uses_klines_before_the_decision():
    p = make_panel(bid=0.44, ask=0.45)                    # Up gap everywhere, fair fixed at 0.5
    p.x[0, OFF + 150:] = 2e-4                             # +2 bp in the kline of 150 (sigma 1 bp)
    assert rf.gap_entries(p, 0.04, 1).second.tolist() == [151]
    assert rf.gap_entries(p, 0.04, 3).second.tolist() == [151, 152, 153]      # 2 > sqrt(3) bp
    p.x[0, OFF + 150:] = 1.5e-4
    assert rf.gap_entries(p, 0.04, 1).second.tolist() == [151]
    assert rf.gap_entries(p, 0.04, 3).second.tolist() == []                   # 1.5 < 1.73 bp
    p.x[0, OFF + 150:] = 0.9e-4                           # below one sigma
    assert rf.gap_entries(p, 0.04, 1).second.tolist() == []
    p.x[0, OFF + 150:] = -2e-4                            # wrong direction for Up
    assert rf.gap_entries(p, 0.04, 1).second.tolist() == []
    q = make_panel(bid=0.55, ask=0.56)                    # Down gap everywhere
    q.x[0, OFF + 150:] = -2e-4
    e = rf.gap_entries(q, 0.04, 1)
    assert sig_list(e) == [(0, 151, -1)]
    q.x[0, OFF + 150:] = 2e-4
    assert len(rf.gap_entries(q, 0.04, 1).second) == 0


def random_raw_panel(M, seed):
    """A panel built from raw rows (some missing, some crossed) through rt.ffill_rows, plus the raw
    rows and their presence mask for the slow reference."""
    rng = np.random.default_rng(seed)
    mid = np.clip(0.5 + np.cumsum(rng.choice([-0.01, 0, 0.01], (M, T), p=[0.25, 0.5, 0.25]), axis=1), 0.03, 0.97)
    spread = rng.choice([0.01, 0.02, 0.03], (M, T), p=[0.6, 0.3, 0.1])
    bid = np.round(mid - spread / 2, 2)
    ask = np.round(bid + spread, 2)
    cross = rng.random((M, T)) < 0.02
    ask[cross] = bid[cross] - rng.choice([0.0, 0.01], cross.sum())
    raw = {"bu": bid, "au": ask, "bd": 1 - ask, "ad": 1 - bid}
    for c in ("su", "sd", "sau", "sad", "du", "dd"):
        raw[c] = np.full((M, T), 50.0)
    raw = {k: v.astype(np.float32) for k, v in raw.items()}
    present = rng.random((M, T)) > 0.15
    for i in range(M):                                    # a few long outages
        s = rng.integers(0, T - 12)
        present[i, s:s + rng.integers(3, 12)] = False
    book, age = rt.ffill_rows(raw, present)
    p = make_panel(M=M)
    p.a.update(book)
    p.a["age"] = age
    p.x[:] = np.cumsum(rng.normal(0, 1e-4, (M, OFF + T)), axis=1).astype(np.float32)
    p.sig[:] = rng.uniform(0.5e-4, 1.5e-4, (M, OFF + T)).astype(np.float32)
    p.fair[:] = rt.fair_from(p.x, p.sig) * 0.5 + 0.5 * np.clip(mid + rng.normal(0, 0.05, (M, T)), 0, 1)
    return p, raw, present


def slow_signals(p, raw, present, g, w, window):
    """Per (market, d) loop from the raw rows: latest row stamped <= d - 1, at most 5 s old at d."""
    out = []
    first = {"all": 1, "240-15": 60, "120-15": 180}[window]
    for m in range(p.M):
        for d in range(first, 283):
            stamps = [s for s in range(0, d) if present[m, s]]
            if not stamps or d - stamps[-1] > 5:
                continue
            s = stamps[-1]
            bu, au, bd, ad = (float(raw[k][m, s]) for k in ("bu", "au", "bd", "ad"))
            if not (au > bu and ad > bd):
                continue
            f = float(p.fair[m, d])
            up = f - au >= g - 1e-6
            dn = (1 - f) - ad >= g - 1e-6
            if up and dn:
                continue
            if w:
                r = float(p.x[m, OFF + d - 1]) - float(p.x[m, OFF + d - 1 - w])
                thr = float(p.sig[m, OFF + d - 1]) * math.sqrt(w)
                up = up and r > thr
                dn = dn and r < -thr
            if up or dn:
                out.append((m, d, 1 if up else -1))
    return out


@pytest.mark.parametrize("g,w,window", [(0.02, 0, "all"), (0.04, 1, "240-15"), (0.03, 3, "all"),
                                        (0.10, 0, "120-15"), (0.06, 3, "120-15")])
def test_signals_match_slow_reference_on_raw_rows(g, w, window):
    p, raw, present = random_raw_panel(12, 7)
    e = rf.gap_entries(p, g, w, window)
    ref = slow_signals(p, raw, present, g, w, window)
    assert sig_list(e) == ref and len(ref) >= 20


def test_signals_are_causal_under_perturbation_of_later_data():
    p, _, _ = random_raw_panel(30, 9)
    p.fair[:] = rt.fair_from(p.x, p.sig)
    s0 = 150
    base = {(g, w): sig_list(rf.gap_entries(p, g, w)) for g in (0.02, 0.06) for w in (0, 1, 3)}
    rng = np.random.default_rng(2)
    for c in ("bu", "au", "bd", "ad"):
        p.a[c][:, s0:] = np.clip(p.a[c][:, s0:] + rng.choice([-0.05, 0.05], (30, T - s0)), 0.01, 0.99)
    p.x[:, OFF + s0:] += rng.normal(0, 1e-3, (30, T - s0)).astype(np.float32)
    p.fair[:] = rt.fair_from(p.x, p.sig)
    changed = False
    for (g, w), b in base.items():
        new = sig_list(rf.gap_entries(p, g, w))
        assert [s for s in b if s[1] <= s0] == [s for s in new if s[1] <= s0]
        changed |= [s for s in b if s[1] > s0] != [s for s in new if s[1] > s0]
    assert changed


# ---------------------------------------------------------------- exits through the engine


def gap_then_close_panel():
    """Up ask 0.45 (bid 0.44) on rows 100..129, fair 0.50; from row 130 the book is 0.49 / 0.50."""
    p = make_panel()
    set_up_book(p, 0, np.arange(100, 130), 0.44, 0.45)
    return p


def run(p, name):
    v = variant(name)
    return rt.Engine(p).simulate(v.signal(p), v.exit)


def test_converge_exit_on_side_mid_back_to_fair_minus_eps():
    p = gap_then_close_panel()
    tr = run(p, "g4-mv0-all-cv-fair-e1-mx30")
    # enter at d = 101 (row 100 seen), ask of row 102; mid 0.495 >= 0.50 - 0.01 first on row 130,
    # seen at e = 131, sold at the bid of row 132; the gap is gone afterwards: one trade
    assert tr["second"].tolist() == [101] and tr["entry_row"].tolist() == [102]
    assert tr["exit_row"].tolist() == [132] and rt.REASONS[tr["reason"][0]] == "converge"
    assert tr["pnl"][0] == pytest.approx(0.49 - fee(0.49) - 0.45 - fee(0.45), abs=1e-6)
    tr = run(p, "g4-mv0-all-cv-fair-e2-mx10")             # max hold 10 hits first
    assert tr["exit_row"][0] == 112 and rt.REASONS[tr["reason"][0]] == "hold"
    tr = run(p, "g4-mv0-all-cv-fair-e0-mx30")             # mid 0.495 never reaches 0.50: max hold
    assert tr["exit_row"].tolist() == [132] and rt.REASONS[tr["reason"][0]] == "hold"


def test_fixed_hold_re_enters_while_the_gap_persists():
    p = gap_then_close_panel()
    tr = run(p, "g4-mv0-all-h10")
    assert tr["second"].tolist() == [101, 113, 125]
    assert tr["exit_row"].tolist() == [112, 124, 136]
    assert tr["exit_px"] == pytest.approx([0.44, 0.44, 0.49], abs=1e-6)
    assert len(run(p, "g6-mv0-all-h10")["pnl"]) == 0       # 5c gap < 6c


def test_settle_control_trades_once_per_market():
    p = gap_then_close_panel()
    p.up_won[0] = 0
    v = variant("g4-mv0-all-settle")
    assert v.control
    tr = rt.Engine(p).simulate(v.signal(p), v.exit)
    assert tr["second"].tolist() == [101]
    assert tr["pnl"][0] == pytest.approx(0 - 0.45 - fee(0.45), abs=1e-6)


def test_down_side_converges_to_one_minus_fair():
    p = make_panel(fair=0.40)                              # Down fair 0.60
    set_up_book(p, 0, np.arange(0, T), 0.45, 0.46)         # Down ask 0.55: 5c under
    set_up_book(p, 0, np.arange(200, T), 0.39, 0.40)       # Down mid 0.605 >= 0.60 from row 200
    tr = run(p, "g4-mv0-120-15-cv-fair-e0-mx60")
    assert tr["second"][0] == 180 and tr["side"][0] == -1
    assert tr["entry_px"][0] == pytest.approx(0.55, abs=1e-6)
    assert tr["exit_row"][0] == 202 and rt.REASONS[tr["reason"][0]] == "converge"
    assert tr["exit_px"][0] == pytest.approx(0.60, abs=1e-6)


def test_family_runs_through_the_engine_and_selection():
    p, _, _ = random_raw_panel(40, 3)
    p.split[20:] = 1
    p.up_won[:] = np.random.default_rng(1).integers(0, 2, 40)
    vs = [v for v in rf.variants() if v.params["g"] == 0.04 and v.params["window"] == "all"]
    st, kept = rt.run_variants(p, vs, keep={vs[0].id}, progress=False)
    assert len(st) == len(vs) == 51
    one = rt.split_stats(p, kept[vs[0].id])
    assert st.loc[0, "n_A"] == one["n_A"] > 0 and st.loc[0, "mean_A"] == pytest.approx(one["mean_A"])
    st2, _ = rt.run_variants(p, vs, workers=2, progress=False)
    pd.testing.assert_frame_equal(st, st2)
    sel, info = rt.select(st)
    assert info["N"] == 51 and "pass_A" in sel
