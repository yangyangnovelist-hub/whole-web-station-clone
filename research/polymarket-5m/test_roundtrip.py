import json

import numpy as np
import pandas as pd
import pytest

import binary as bo
import kacho_late as kl
import roundtrip as rt

T, OFF = rt.T, rt.OFF
START0 = 1_775_001_600  # 2026-04-01 00:00 UTC (split A)


def make_panel(M=1, bid=0.49, ask=0.50, size=100.0, up_won=1, split=0):
    f = lambda v: np.full((M, T), v, np.float32)
    start = START0 + 300 * np.arange(M, dtype=np.int64)
    p = rt.Panel(cid=np.array([f"c{i}" for i in range(M)]), slug=np.array([f"s{i}" for i in range(M)]),
                 start=start, up_won=np.full(M, up_won, np.int8), up_inferred=np.full(M, up_won, np.int8),
                 n_ticks=np.full(M, T, np.int16), split=np.full(M, split, np.int8),
                 day=(start // 86400).astype(np.int32), ref=np.full(M, 70000.0), age=np.zeros((M, T), np.int8),
                 fair=f(0.5), x=np.zeros((M, OFF + T), np.float32), sig=np.full((M, OFF + T), 1e-4, np.float32),
                 vol=np.zeros((M, OFF + T), np.float32), buy=np.zeros((M, OFF + T), np.float32), off=np.int64(OFF),
                 bu=f(bid), au=f(ask), bd=f(1 - ask), ad=f(1 - bid), su=f(size), sd=f(size), sau=f(size),
                 sad=f(size), du=f(100.0), dd=f(100.0))
    return p


def set_up_book(p, m, rows, bid, ask):
    """Set the Up book (and its Down mirror) on rows."""
    p.bu[m, rows], p.au[m, rows] = bid, ask
    p.bd[m, rows], p.ad[m, rows] = 1 - np.asarray(ask), 1 - np.asarray(bid)


def ent(m, d, side, target=None):
    a = lambda v: np.atleast_1d(np.asarray(v, np.int64))
    return rt.Entries(a(m), a(d), a(side), None if target is None else np.atleast_1d(np.asarray(target, float)))


def fee(p):
    return float(bo.taker_fee(p))


# ---------------------------------------------------------------- timing convention


def test_entry_at_ask_of_row_d_plus_1_and_exit_at_bid_of_row_e_plus_1():
    p = make_panel()
    s = np.arange(T)
    set_up_book(p, 0, s, 0.30 + 0.001 * s, 0.31 + 0.001 * s)
    tr = rt.Engine(p).simulate(ent(0, 50, 1), rt.Exit.hold(10))
    assert tr["entry_row"][0] == 51 and tr["exit_row"][0] == 61
    assert tr["entry_px"][0] == pytest.approx(0.31 + 0.051, abs=1e-6)
    assert tr["exit_px"][0] == pytest.approx(0.30 + 0.061, abs=1e-6)
    assert tr["hold"][0] == 10 and rt.REASONS[tr["reason"][0]] == "hold"


def test_down_side_trades_the_down_book():
    p = make_panel()
    s = np.arange(T)
    set_up_book(p, 0, s, 0.30 + 0.001 * s, 0.31 + 0.001 * s)
    tr = rt.Engine(p).simulate(ent(0, 50, -1), rt.Exit.hold(10))
    assert tr["entry_px"][0] == pytest.approx(p.ad[0, 51], abs=1e-6)
    assert tr["exit_px"][0] == pytest.approx(p.bd[0, 61], abs=1e-6)
    assert tr["side"][0] == -1


def test_momentum_signal_uses_only_klines_stamped_before_the_decision():
    p = make_panel(M=2)
    p.x[0, OFF + 100:] = 2e-4          # a 2 bp jump in the kline of second 100 (usable from 101)
    p.x[1, OFF + 200:] = -2e-4
    e = rt.momentum_entries(p, w=1, kind="bp", thr=1.0, window="all", mode="first")
    assert list(e.market) == [0, 1] and list(e.second) == [101, 201] and list(e.side) == [1, -1]
    e = rt.momentum_entries(p, w=1, kind="z", thr=1.5, window="gt60", mode="first")
    assert list(e.second) == [101, 201]                      # 2 bp > 1.5 sigma sqrt(1) = 1.5 bp
    assert len(rt.momentum_entries(p, w=1, kind="z", thr=2.0, window="all", mode="first").market) == 0
    assert list(rt.momentum_entries(p, w=3, kind="bp", thr=1.0, window="all", mode="all10").second) == [101, 201]
    p2 = make_panel()
    p2.x[0, OFF + 245:] = 5e-4         # usable at 246: 54 s left, outside the >60 s window
    assert len(rt.momentum_entries(p2, w=1, kind="bp", thr=1.0, window="gt60", mode="first").market) == 0
    assert list(rt.momentum_entries(p2, w=1, kind="bp", thr=1.0, window="all", mode="first").second) == [246]


def test_signals_and_fair_are_causal():
    rng = np.random.default_rng(1)
    p = make_panel(M=50)
    p.x[:] = np.cumsum(rng.normal(0, 1e-4, (50, OFF + T)), axis=1).astype(np.float32)
    s0 = 150
    base = rt.momentum_entries(p, w=3, kind="z", thr=1.5, window="all", mode="all10")
    fair0 = rt.fair_from(p.x, p.sig)
    p.x[:, OFF + s0:] += rng.normal(0, 1e-3, (50, T - s0)).astype(np.float32)   # change klines >= s0
    pert = rt.momentum_entries(p, w=3, kind="z", thr=1.5, window="all", mode="all10")
    fair1 = rt.fair_from(p.x, p.sig)
    k0 = base.second <= s0
    k1 = pert.second <= s0
    assert np.array_equal(base.market[k0], pert.market[k1]) and np.array_equal(base.second[k0], pert.second[k1])
    assert np.array_equal(fair0[:, :s0 + 1], fair1[:, :s0 + 1])
    assert not np.array_equal(fair0[:, s0 + 1:], fair1[:, s0 + 1:])


def test_fair_is_the_point_price_model_from_the_previous_kline():
    p = make_panel()
    p.x[0, OFF + 99] = 3e-4
    f = rt.fair_from(p.x, p.sig)
    assert f[0, 100] == pytest.approx(float(bo.prob_up_european(3e-4, 0.0, 1e-4, 200)), abs=1e-6)
    assert f[0, 0] == pytest.approx(0.5)


def test_converge_check_at_e_reads_row_e_minus_1():
    p = make_panel()
    p.fair[:] = 0.60
    rows = np.arange(80, T)
    set_up_book(p, 0, rows, 0.60 + 0.001 * (rows - 80), 0.61 + 0.001 * (rows - 80))   # mid >= 0.6 from row 80
    tr = rt.Engine(p).simulate(ent(0, 50, 1), rt.Exit.converge(120, eps=0.0))
    # decision 81 sees row 80 (first mid >= fair), fills at row 82
    assert tr["exit_row"][0] == 82 and rt.REASONS[tr["reason"][0]] == "converge"
    assert tr["exit_px"][0] == pytest.approx(0.602, abs=1e-6)
    tr = rt.Engine(p).simulate(ent(0, 50, 1), rt.Exit.converge(120, eps=0.11))   # already converged
    assert tr["exit_row"][0] == 53                                                # first check at d + 2


def test_converge_max_hold_and_entry_target():
    p = make_panel()
    p.fair[:] = 0.90
    tr = rt.Engine(p).simulate(ent(0, 50, 1), rt.Exit.converge(60, eps=0.01))
    assert tr["exit_row"][0] == 111 and rt.REASONS[tr["reason"][0]] == "hold"
    rows = np.arange(90, T)
    set_up_book(p, 0, rows, 0.52, 0.53)
    tr = rt.Engine(p).simulate(ent(0, 50, 1, target=0.525), rt.Exit.converge(60, target="entry"))
    assert tr["exit_row"][0] == 92 and rt.REASONS[tr["reason"][0]] == "converge"


def test_stop_loss_uses_entry_row_mid():
    p = make_panel()
    set_up_book(p, 0, np.arange(70, T), 0.46, 0.47)        # mid 0.465 = 0.495 - 0.03
    tr = rt.Engine(p).simulate(ent(0, 50, 1), rt.Exit.hold(120, stop=0.03))
    assert tr["exit_row"][0] == 72 and rt.REASONS[tr["reason"][0]] == "stop"
    tr = rt.Engine(p).simulate(ent(0, 50, 1), rt.Exit.hold(120, stop=0.06))
    assert tr["exit_row"][0] == 171 and rt.REASONS[tr["reason"][0]] == "hold"
    # whichever of stop and converge comes first decides
    p = make_panel()
    p.fair[:] = 0.60
    set_up_book(p, 0, np.arange(60, 80), 0.45, 0.46)        # stop seen at decision 61
    set_up_book(p, 0, np.arange(80, T), 0.60, 0.61)         # converge seen at decision 81
    tr = rt.Engine(p).simulate(ent(0, 50, 1), rt.Exit.converge(120, eps=0.0, stop=0.03))
    assert rt.REASONS[tr["reason"][0]] == "stop" and tr["exit_row"][0] == 62
    tr = rt.Engine(p).simulate(ent(0, 50, 1), rt.Exit.converge(120, eps=0.0, stop=0.06))
    assert rt.REASONS[tr["reason"][0]] == "converge" and tr["exit_row"][0] == 82


def test_costs_both_legs():
    p = make_panel(bid=0.62, ask=0.63)
    tr = rt.Engine(p).simulate(ent(0, 10, 1), rt.Exit.hold(5))
    assert tr["pnl"][0] == pytest.approx(0.62 - fee(0.62) - 0.63 - fee(0.63), abs=1e-6)


# ---------------------------------------------------------------- positions and exits


def test_one_position_per_market():
    p = make_panel(M=2)
    tr = rt.Engine(p).simulate(ent([0, 0, 0, 0, 1], [50, 55, 61, 62, 55], [1, -1, 1, 1, 1]), rt.Exit.hold(10))
    assert list(zip(tr["market"], tr["second"])) == [(0, 50), (0, 62), (1, 55)]
    # the first trade exits at row 61: a decision at 61 is still blocked, 62 is not


def test_forced_exit_15_s_before_the_end():
    p = make_panel()
    tr = rt.Engine(p).simulate(ent([0], [270], [1]), rt.Exit.hold(60))
    assert tr["exit_row"][0] == rt.FORCE_ROW == 285 and rt.REASONS[tr["reason"][0]] == "forced"
    tr = rt.Engine(p).simulate(ent([0], [282], [1]), rt.Exit.hold(2))
    assert tr["exit_row"][0] == 285 and rt.REASONS[tr["reason"][0]] == "hold"
    assert len(rt.Engine(p).simulate(ent([0], [283], [1]), rt.Exit.hold(2))["pnl"]) == 0
    assert len(rt.Engine(p).simulate(ent([0], [0], [1]), rt.Exit.hold(2))["pnl"]) == 0


def test_settle_variant_holds_to_the_official_outcome():
    p = make_panel(M=2, bid=0.39, ask=0.40)
    p.up_won[1] = 0
    tr = rt.Engine(p).simulate(ent([0, 0, 1], [20, 100, 20], [1, 1, -1]), rt.Exit.settle())
    assert list(tr["second"]) == [20, 20]                      # one position per market
    assert tr["pnl"][0] == pytest.approx(1 - 0.40 - fee(0.40), abs=1e-6)
    assert tr["pnl"][1] == pytest.approx(1 - 0.61 - fee(0.61), abs=1e-6)   # Down ask = 1 - 0.39


def test_size_price_and_staleness_checks():
    p = make_panel()
    p.sau[0, 51] = 4.0                                        # entry row: too few shares at the ask
    assert len(rt.Engine(p).simulate(ent(0, 50, 1), rt.Exit.hold(10))["pnl"]) == 0
    p = make_panel()
    set_up_book(p, 0, [51], 0.985, 0.99)                      # ask outside 0.02..0.98
    assert len(rt.Engine(p).simulate(ent(0, 50, 1), rt.Exit.hold(10))["pnl"]) == 0
    p = make_panel()
    p.age[0, 51] = 1                                          # fill rows must be the exact row
    assert len(rt.Engine(p).simulate(ent(0, 50, 1), rt.Exit.hold(10))["pnl"]) == 0
    p = make_panel()
    set_up_book(p, 0, [51], 0.50, 0.49)                       # crossed row is unusable
    assert len(rt.Engine(p).simulate(ent(0, 50, 1), rt.Exit.hold(10))["pnl"]) == 0
    p = make_panel()
    p.su[0, 61:64] = 3.0                                      # exit bid too thin: sell at the next good row
    tr = rt.Engine(p).simulate(ent(0, 50, 1), rt.Exit.hold(10))
    assert tr["exit_row"][0] == 64
    p = make_panel(up_won=0)
    p.su[0, 61:] = 3.0                                        # never sellable through 285: settle
    tr = rt.Engine(p).simulate(ent(0, 50, 1), rt.Exit.hold(10))
    assert rt.REASONS[tr["reason"][0]] == "fallback" and tr["exit_row"][0] == -1
    assert tr["pnl"][0] == pytest.approx(0 - 0.50 - fee(0.50), abs=1e-6)
    tr = rt.Engine(p).simulate(ent([0, 0], [50, 120], [1, 1]), rt.Exit.hold(10))
    assert len(tr["pnl"]) == 1                                # a settled position blocks the market


def test_ffill_rows_respects_max_age():
    raw = {"v": np.arange(T, dtype=np.float32)[None, :].copy()}
    present = np.ones((1, T), bool)
    present[0, 10:21] = False                                 # rows 10..20 missing
    out, age = rt.ffill_rows(raw, present)
    assert out["v"][0, 14] == 9 and age[0, 14] == 5
    assert np.isnan(out["v"][0, 15]) and age[0, 15] == -1
    assert out["v"][0, 21] == 21 and age[0, 21] == 0


def test_fill_delay_zero_is_the_literal_roundtrip_text():
    p = make_panel()
    s = np.arange(T)
    set_up_book(p, 0, s, 0.30 + 0.001 * s, 0.31 + 0.001 * s)
    tr = rt.Engine(p, fill_delay=0).simulate(ent(0, 50, 1), rt.Exit.hold(10))
    assert tr["entry_row"][0] == 50 and tr["exit_row"][0] == 60


# ---------------------------------------------------------------- signals, stats, selection


def test_thin_and_first_per_market():
    m = np.array([0, 0, 0, 0, 1, 1])
    s = np.array([5, 9, 15, 24, 3, 30])
    assert list(rt.thin(m, s, 10)) == [0, 2, 4, 5]
    assert list(rt.first_per_market(m)) == [0, 4]


def test_cluster_se_matches_kacho_late():
    rng = np.random.default_rng(3)
    mk = rng.integers(0, 40, 500)
    pnl = rng.normal(0.01, 0.2, 500) + 0.05 * (mk % 3)
    mean, se, G = rt.cluster_se(pnl, mk, 40)
    m2, se2, _ = kl.cl(pd.DataFrame({"market": mk, "pnl": pnl}))
    assert mean == pytest.approx(m2) and se == pytest.approx(se2)


def test_bonferroni_selection():
    base = dict(mk_A=100, mk_B=100, n_A=200, n_B=200)
    rows = [dict(id="a", mean_A=0.01, p_A=1e-4, mean_B=0.01, p_B=0.02, **base),     # passes both (k = 2)
            dict(id="b", mean_A=0.01, p_A=4e-4, mean_B=0.01, p_B=0.03, **base),     # passes A, fails B (0.03 > 0.025)
            dict(id="c", mean_A=0.01, p_A=6e-4, mean_B=0.01, p_B=1e-9, **base),     # fails A (6e-4 > 5e-4)
            dict(id="d", mean_A=-0.01, p_A=1e-9, mean_B=0.01, p_B=1e-9, **base),    # negative mean
            dict(id="e", mean_A=0.01, p_A=1e-9, mean_B=0.01, p_B=1e-9, **{**base, "mk_A": 10})]  # too few markets
    st = pd.DataFrame(rows)
    sel, info = rt.select(st, n_total=100)
    assert list(sel["pass_A"]) == [True, True, False, False, False]
    assert info["k"] == 2 and info["thr_B"] == pytest.approx(0.025)
    assert list(sel["pass_B"]) == [True, False, False, False, False]


def test_family1_grid_and_end_to_end(tmp_path):
    vs = rt.family1_variants()
    assert len(vs) == 5 * 7 * 34 * 2 * 2 == 4760
    assert len({v.id for v in vs}) == len(vs)
    assert sum(v.control for v in vs) == 5 * 7 * 2 * 2
    rng = np.random.default_rng(5)
    M = 60
    p = make_panel(M=M)
    p.split[M // 2:] = 1
    p.x[:] = np.cumsum(rng.normal(0, 1e-4, (M, OFF + T)), axis=1).astype(np.float32)
    mid = np.clip(0.5 + np.cumsum(rng.normal(0, 0.01, (M, T)), axis=1), 0.05, 0.95).round(2)
    set_up_book(p, slice(None), slice(None), (mid - 0.01).astype(np.float32), mid.astype(np.float32))
    p.fair[:] = rt.fair_from(p.x, p.sig)
    sub = [v for v in vs if v.key == vs[0].key] + [v for v in vs if v.name.startswith("w30-bp8-gt60-all10")]
    st, kept = rt.run_variants(p, sub, keep={sub[0].id}, progress=False)
    assert len(st) == len(sub) and sub[0].id in kept
    tr = kept[sub[0].id]
    one = rt.split_stats(p, tr)
    assert st.loc[0, "n_A"] == one["n_A"] and st.loc[0, "mean_A"] == pytest.approx(one["mean_A"], nan_ok=True)
    st2, _ = rt.run_variants(p, sub, workers=2, progress=False)
    pd.testing.assert_frame_equal(st, st2)
    sel, info = rt.select(st)
    doc = rt.frozen_json(sel, info, tmp_path / "frozen.json")
    assert json.loads((tmp_path / "frozen.json").read_text())["N"] == len(sub) == doc["N"]
    md = rt.report_md(sel, info)
    assert "各族汇总" in md and rt.F1 in md
    f = rt.trades_frame(p, tr, sub[0].id)
    assert {"variant", "start", "entry_px", "exit_px", "pnl", "hold", "reason"} <= set(f.columns)


def test_panel_roundtrips_through_npz(tmp_path):
    p = make_panel(M=3)
    p.save(tmp_path / "p.npz")
    q = rt.Panel.load(tmp_path / "p.npz")
    assert q.M == 3 and np.array_equal(q.au, p.au) and q.off == OFF and q.slug[2] == "s2"


# ---------------------------------------------------------------- independent slow reference


def slow_reference(p, entries, ex):
    """Per-trade loop written from the stated rules, not from the engine."""
    TOLR = 1e-6

    def book(k, r):
        if k == 0:
            return p.bu[m, r], p.au[m, r], p.su[m, r], p.sau[m, r]
        return p.bd[m, r], p.ad[m, r], p.sd[m, r], p.sad[m, r]

    def usable(r):
        return p.age[m, r] >= 0 and p.au[m, r] > p.bu[m, r]

    def mid(k, r):
        if not usable(r):
            return np.nan
        b, a, _, _ = book(k, r)
        return (float(b) + float(a)) / 2

    def entry_ok(k, r):
        b, a, bs, as_ = book(k, r)
        return usable(r) and p.age[m, r] == 0 and 0.02 - TOLR <= a <= 0.98 + TOLR and as_ >= 5 - TOLR

    def exit_ok(k, r):
        b, a, bs, as_ = book(k, r)
        return usable(r) and p.age[m, r] == 0 and 0.02 - TOLR <= b <= 0.98 + TOLR and bs >= 5 - TOLR

    out = []
    order = sorted(range(len(entries.market)), key=lambda i: (entries.market[i], entries.second[i], i))
    free = {}
    for i in order:
        m, d, sd = int(entries.market[i]), int(entries.second[i]), int(entries.side[i])
        k = 0 if sd > 0 else 1
        if d < 1 or d > 282 or d < free.get(m, -1):
            continue
        r0 = d + 1
        if not entry_ok(k, r0):
            continue
        ask = float(book(k, r0)[1])
        won = p.up_won[m] if k == 0 else 1 - p.up_won[m]
        if ex.kind == "settle":
            out.append((m, d, r0, -1, won - ask - fee(ask), "settle"))
            free[m] = 10**9
            continue
        e_end = min(d + ex.h, 284)
        reason = "forced" if d + ex.h > 284 else "hold"
        ref = mid(k, r0)
        e_exit = e_end
        for e in range(d + 2, e_end + 1):
            mp = mid(k, e - 1)
            if ex.stop > 0 and np.isfinite(mp) and mp <= ref - ex.stop + TOLR:
                reason, e_exit = "stop", e
                break
            if ex.kind == "converge":
                if ex.target == "entry":
                    tg = float(entries.target[i])
                else:
                    tg = float(p.a[ex.target][m, e]) if k == 0 else 1 - float(p.a[ex.target][m, e])
                if np.isfinite(mp) and mp >= tg - ex.eps - TOLR:
                    reason, e_exit = "converge", e
                    break
        r = e_exit + 1
        while r <= 285 and not exit_ok(k, r):
            r += 1
        if r > 285:
            out.append((m, d, r0, -1, won - ask - fee(ask), "fallback"))
            free[m] = 10**9
        else:
            bid = float(book(k, r)[0])
            out.append((m, d, r0, r, bid - fee(bid) - ask - fee(ask), reason))
            free[m] = r + 1
    return out


def random_panel(M, seed):
    rng = np.random.default_rng(seed)
    p = make_panel(M=M)
    mid = np.clip(0.5 + np.cumsum(rng.choice([-0.01, 0, 0.01], (M, T), p=[0.2, 0.6, 0.2]), axis=1), 0.01, 0.99)
    spread = rng.choice([0.01, 0.02, 0.03], (M, T), p=[0.7, 0.2, 0.1])
    bid = np.round(mid - spread / 2, 2)
    ask = np.round(bid + spread, 2)
    cross = rng.random((M, T)) < 0.01
    ask[cross] = bid[cross] - 0.01
    set_up_book(p, slice(None), slice(None), bid.astype(np.float32), ask.astype(np.float32))
    for c in ("su", "sd", "sau", "sad"):
        p.a[c][:] = rng.choice([3.0, 5.0, 50.0], (M, T), p=[0.1, 0.2, 0.7])
    stale = rng.random((M, T)) < 0.03
    p.age[stale] = rng.integers(1, 6, stale.sum())
    gone = rng.random((M, T)) < 0.02
    p.age[gone] = -1
    for c in rt.BOOK:
        p.a[c][gone] = np.nan
    p.fair[:] = np.clip(mid + rng.normal(0, 0.03, (M, T)), 0.01, 0.99)
    p.up_won[:] = rng.integers(0, 2, M)
    return p, rng


@pytest.mark.parametrize("ex", [rt.Exit.hold(5), rt.Exit.hold(30, 0.03), rt.Exit.hold(120, 0.06),
                                rt.Exit.converge(60, 0.0), rt.Exit.converge(120, 0.01, 0.03),
                                rt.Exit.converge(60, 0.01, 0.03, target="entry"), rt.Exit.settle()],
                         ids=lambda e: e.label())
def test_engine_matches_slow_reference(ex):
    p, rng = random_panel(40, 11)
    n = 600
    e = rt.Entries(rng.integers(0, 40, n), rng.integers(-2, 290, n), rng.choice([-1, 1], n),
                   rng.uniform(0.3, 0.7, n))
    tr = rt.Engine(p).simulate(e, ex)
    ref = slow_reference(p, e, ex)
    got = list(zip(tr["market"], tr["second"], tr["entry_row"], tr["exit_row"], tr["pnl"],
                   np.asarray(rt.REASONS)[tr["reason"]]))
    assert len(got) == len(ref) >= 35
    for g, r in zip(got, ref):
        assert g[:4] == r[:4] and g[5] == r[5]
        assert g[4] == pytest.approx(r[4], abs=1e-6)
