import numpy as np
import pandas as pd
import pytest

import binary as bo
import factors as fx
import roundtrip as rt
import rt_factors as rf

T, OFF = rt.T, rt.OFF
START0 = 1_775_001_600  # 2026-04-01 00:00 UTC (split A)


def make_panel(M=1, bid=0.49, ask=0.50, size=100.0, up_won=1, split=0):
    f = lambda v: np.full((M, T), v, np.float32)
    start = START0 + 300 * np.arange(M, dtype=np.int64)
    return rt.Panel(cid=np.array([f"c{i}" for i in range(M)]), slug=np.array([f"s{i}" for i in range(M)]),
                    start=start, up_won=np.full(M, up_won, np.int8), up_inferred=np.full(M, up_won, np.int8),
                    n_ticks=np.full(M, T, np.int16), split=np.full(M, split, np.int8),
                    day=(start // 86400).astype(np.int32), ref=np.full(M, 70000.0), age=np.zeros((M, T), np.int8),
                    fair=f(0.5), x=np.zeros((M, OFF + T), np.float32), sig=np.full((M, OFF + T), 1e-4, np.float32),
                    vol=np.zeros((M, OFF + T), np.float32), buy=np.zeros((M, OFF + T), np.float32),
                    off=np.int64(OFF), bu=f(bid), au=f(ask), bd=f(1 - ask), ad=f(1 - bid), su=f(size),
                    sd=f(size), sau=f(size), sad=f(size), du=f(100.0), dd=f(100.0))


def set_up_book(p, m, rows, bid, ask):
    """Set the Up book (and its Down mirror) on rows."""
    p.bu[m, rows], p.au[m, rows] = bid, ask
    p.bd[m, rows], p.ad[m, rows] = 1 - np.asarray(ask), 1 - np.asarray(bid)


def set_pred(p, values, models=rf.F6_MODELS):
    v = np.asarray(values, float)
    for model in models:
        p.a[rf.pred_key(model)] = v.copy()


def copy_panel(p):
    return rt.Panel(**{k: np.array(v, copy=True) for k, v in p.a.items()})


def sig_list(e):
    return list(zip(e.market.tolist(), e.second.tolist(), e.side.tolist()))


def variant(name):
    return next(v for v in rf.variants() if v.name == name)


def fee(x):
    return float(bo.taker_fee(x))


# ---------------------------------------------------------------- grid


def test_grid_is_the_preregistered_one():
    vs = rf.variants()
    assert len(vs) == 4 * 4 * 4 * 3 * 5 == 960
    assert len({v.id for v in vs}) == len(vs)
    assert len({v.key for v in vs}) == 192
    assert sum(v.control for v in vs) == 192
    assert all(v.family == rf.F6 for v in vs)
    assert {v.params["model"] for v in vs} == {"ridge_5m", "hgb_5m", "ridge_15m", "hgb_15m"}
    assert {v.params["c"] for v in vs} == {0.0, 0.02, 0.04, 0.06}
    assert {v.params["entry_second"] for v in vs} == {1, 5, 10, 30}
    assert {v.params["gap"] for v in vs} == {0.0, 0.02, 0.05}
    assert {v.params["exit_left"] for v in vs} == {120, 60, 30, 15, None}
    for v in vs:
        L, d = v.params["exit_left"], v.params["entry_second"]
        if L is None:
            assert v.exit.kind == "settle" and v.control
        else:
            assert v.exit.kind == "hold" and v.exit.stop == 0 and not v.control
            assert d + v.exit.h == 299 - L          # decision e = 299 - L -> fill row 300 - L
            assert d + v.exit.h <= rt.FORCE_ROW - 1


def test_variants_with_a_panel_do_not_reload_attached_predictions(monkeypatch):
    p = make_panel(M=2)
    set_pred(p, [0.6, 0.4])
    monkeypatch.setattr(rf, "load_preds", lambda *a, **k: (_ for _ in ()).throw(AssertionError("loaded")))
    assert len(rf.variants(p)) == 960


# ---------------------------------------------------------------- predictions -> markets


def test_prediction_is_the_one_stamped_at_the_market_start():
    p = make_panel(M=2)
    S = p.start
    idx = np.array([S[0] - 300, S[0], S[1], S[1] + 300], np.int64)
    pr = pd.DataFrame({m: [0.9, 0.56, 0.44, 0.1] for m in rf.F6_MODELS}, index=pd.Index(idx, name="T"))
    rf.attach_preds(p, pr)
    for m in rf.F6_MODELS:
        assert p.a[rf.pred_key(m)].tolist() == [0.56, 0.44]
    pr2 = pr.copy()
    pr2.loc[S[0] - 300] = 0.0                       # the previous and next boundaries are not read
    pr2.loc[S[1] + 300] = 1.0
    q = rf.attach_preds(make_panel(M=2), pr2)
    assert q.a[rf.pred_key("ridge_5m")].tolist() == [0.56, 0.44]
    r = rf.attach_preds(make_panel(M=2), pr.drop(index=S[1]))
    assert np.isnan(r.a[rf.pred_key("hgb_5m")][1])
    assert sig_list(rf.model_entries(r, "hgb_5m", 0.0, 1)) == [(0, 1, 1)]   # no prediction: no trade


def test_side_thresholds_are_strict_and_symmetric():
    p = np.array([0.5, 0.5001, 0.4999, 0.52, 0.48, 0.5201, 0.4799, 0.61, np.nan])
    assert rf.model_side(p, 0.0).tolist() == [0, 1, -1, 1, -1, 1, -1, 1, 0]
    assert rf.model_side(p, 0.02).tolist() == [0, 0, 0, 0, 0, 1, -1, 1, 0]
    assert rf.model_side(p, 0.06).tolist() == [0, 0, 0, 0, 0, 0, 0, 1, 0]
    pan = make_panel(M=9)
    set_pred(pan, p)
    e = rf.model_entries(pan, "ridge_15m", 0.02, 5)
    assert sig_list(e) == [(5, 5, 1), (6, 5, -1), (7, 5, 1)]


# ---------------------------------------------------------------- signals use only usable data


def random_panel(M=60, seed=0):
    rng = np.random.default_rng(seed)
    p = make_panel(M=M)
    mid = np.clip(0.5 + 0.08 * rng.standard_normal((M, T)), 0.05, 0.95)
    spr = rng.choice([0.01, 0.02, 0.03], size=(M, T))
    for m in range(M):
        set_up_book(p, m, np.arange(T), (mid[m] - spr[m] / 2).astype(np.float32), (mid[m] + spr[m] / 2).astype(np.float32))
    p.age[:] = rng.choice([0, 0, 0, 1, 2, 4, 5, -1], size=(M, T)).astype(np.int8)
    p.x[:] = (1e-4 * rng.standard_normal((M, OFF + T))).cumsum(axis=1)
    set_pred(p, 0.5 + 0.06 * rng.standard_normal(M))
    return p


def entries_by_key(p):
    out = {}
    for v in rf.variants():
        if v.key not in out:
            out[v.key] = sig_list(v.signal(p))
    return out


@pytest.mark.parametrize("d", rf.F6_ENTRY)
def test_signals_ignore_rows_stamped_at_or_after_the_decision_and_all_klines(d):
    p = random_panel()
    base = {k: v for k, v in entries_by_key(p).items() if k[3] == d}
    assert sum(len(v) for v in base.values()) > 0
    assert any(len(v) for k, v in base.items() if k[4] > 0)
    q = copy_panel(p)
    rng = np.random.default_rng(1)
    for c in rt.BOOK:
        q.a[c][:, d:] = rng.uniform(0, 1, size=q.a[c][:, d:].shape)   # rows stamped >= d (and ffill there)
    q.age[:, d:] = rng.integers(-1, 6, size=q.age[:, d:].shape)
    for c in ("x", "sig", "vol", "buy", "fair"):
        q.a[c][:] = rng.uniform(-1, 1, size=q.a[c].shape)                # klines and fair are not read at all
    after = {k: v for k, v in entries_by_key(q).items() if k[3] == d}
    assert after == base


def test_filter_reads_the_row_in_use_at_d_minus_1():
    p = random_panel(seed=3)
    d = 10
    base = sig_list(rf.model_entries(p, "hgb_5m", 0.0, d, 0.02))
    q = copy_panel(p)
    for m in range(q.M):                               # move every mid at column d - 1 against the model
        side = 1 if q.a[rf.pred_key("hgb_5m")][m] > 0.5 else -1
        set_up_book(q, m, d - 1, 0.97 if side > 0 else 0.02, 0.98 if side > 0 else 0.03)
    assert base and not sig_list(rf.model_entries(q, "hgb_5m", 0.0, d, 0.02))


def test_opening_mid_at_d1_is_row_0_and_delayed_entries_use_the_latest_usable_row():
    p = make_panel(M=1)
    set_pred(p, [0.56])
    set_up_book(p, 0, 0, 0.50, 0.51)                   # opening mid 0.505: model ahead by 5.5 c
    set_up_book(p, 0, np.arange(1, T), 0.54, 0.55)     # later mid 0.545: ahead by only 1.5 c
    assert sig_list(rf.model_entries(p, "ridge_5m", 0.0, 1, 0.05)) == [(0, 1, 1)]
    assert sig_list(rf.model_entries(p, "ridge_5m", 0.0, 1, 0.02)) == [(0, 1, 1)]
    for d in (5, 10, 30):
        assert sig_list(rf.model_entries(p, "ridge_5m", 0.0, d, 0.02)) == []
        assert sig_list(rf.model_entries(p, "ridge_5m", 0.0, d, 0.0)) == [(0, d, 1)]
    p.age[0, 0] = -1                                   # no row stamped 0: no opening mid
    assert sig_list(rf.model_entries(p, "ridge_5m", 0.0, 1, 0.02)) == []
    assert sig_list(rf.model_entries(p, "ridge_5m", 0.0, 1, 0.0)) == [(0, 1, 1)]


def test_filter_down_side_and_the_tolerance():
    p = make_panel(M=3)
    set_pred(p, [0.44, 0.44, 0.47])
    set_up_book(p, 0, np.arange(T), 0.50, 0.51)        # Down mid 0.495, model Down 0.56: ahead 6.5 c
    set_up_book(p, 1, np.arange(T), 0.48, 0.50)        # Down mid 0.51: ahead 5.0 c (exactly)
    set_up_book(p, 2, np.arange(T), 0.50, 0.51)        # model Down 0.53: ahead 3.5 c
    assert sig_list(rf.model_entries(p, "hgb_15m", 0.0, 1, 0.05)) == [(0, 1, -1), (1, 1, -1)]
    assert sig_list(rf.model_entries(p, "hgb_15m", 0.0, 1, 0.02)) == [(0, 1, -1), (1, 1, -1), (2, 1, -1)]
    assert sig_list(rf.model_entries(p, "hgb_15m", 0.04, 1, 0.02)) == [(0, 1, -1), (1, 1, -1)]


def test_filter_skips_stale_one_sided_crossed_and_locked_rows():
    p = make_panel(M=5)
    set_pred(p, [0.60] * 5)
    d = 10
    p.age[0, d - 1] = 4                                # row stamped d - 5: usable
    p.age[1, d - 1] = 5                                # row stamped d - 6: older than 5 s at use
    p.bu[2, d - 1], p.sau[2, d - 1] = 0.0, np.nan      # empty Up bid (bid 0)
    p.ad[2, d - 1] = 1.0
    set_up_book(p, 3, d - 1, 0.50, 0.49)               # crossed
    set_up_book(p, 4, d - 1, 0.50, 0.50)               # locked
    assert sig_list(rf.model_entries(p, "ridge_5m", 0.0, d, 0.02)) == [(0, d, 1)]
    assert len(rf.model_entries(p, "ridge_5m", 0.0, d, 0.0).market) == 5


def test_decision_mid_values():
    p = make_panel(M=2)
    set_up_book(p, 0, 4, 0.40, 0.42)
    p.bd[1, 4], p.ad[1, 4] = 0.30, 0.34                # Down book not a mirror: its own mid is used
    assert rf.decision_mid(p, 5, np.array([1, -1])).tolist() == pytest.approx([0.41, 0.32])
    with pytest.raises(ValueError):
        rf.decision_mid(p, 0, np.array([1, 1]))


# ---------------------------------------------------------------- exits through the engine


@pytest.mark.parametrize("d", rf.F6_ENTRY)
@pytest.mark.parametrize("left", rf.F6_LEFT)
def test_entry_at_ask_of_row_d_plus_1_and_exit_at_bid_of_row_300_minus_left(d, left):
    p = make_panel(M=2, up_won=0)
    s = np.arange(T)
    for m in range(2):
        set_up_book(p, m, s, (0.30 + 0.001 * s).astype(np.float32), (0.31 + 0.001 * s).astype(np.float32))
    set_pred(p, [0.6, 0.4])
    v = variant(f"ridge_5m-c2-d{d}-all-left{left}")
    tr = rt.Engine(p).simulate(v.signal(p), v.exit)
    assert tr["market"].tolist() == [0, 1] and tr["side"].tolist() == [1, -1]
    assert tr["entry_row"].tolist() == [d + 1, d + 1]
    assert tr["exit_row"].tolist() == [300 - left] * 2
    up_ask, up_bid = 0.31 + 0.001 * (d + 1), 0.30 + 0.001 * (300 - left)
    dn_ask, dn_bid = 1 - (0.30 + 0.001 * (d + 1)), 1 - (0.31 + 0.001 * (300 - left))
    assert tr["entry_px"] == pytest.approx([up_ask, dn_ask], abs=1e-6)
    assert tr["exit_px"] == pytest.approx([up_bid, dn_bid], abs=1e-6)
    exp = [b - fee(b) - a - fee(a) for a, b in ((up_ask, up_bid), (dn_ask, dn_bid))]
    assert tr["pnl"] == pytest.approx(exp, abs=1e-6)
    assert [rt.REASONS[r] for r in tr["reason"]] == ["hold", "hold"]


def test_settle_control_pays_the_official_outcome_with_one_fee():
    p = make_panel(M=2, up_won=1)
    set_pred(p, [0.55, 0.43])
    v = variant("hgb_5m-c6-d1-all-settle")
    assert v.control
    tr = rt.Engine(p).simulate(v.signal(p), v.exit)
    assert tr["market"].tolist() == [1] and tr["side"].tolist() == [-1]  # 0.55 is not > 0.56
    a = 1 - 0.49
    assert tr["pnl"][0] == pytest.approx(0 - a - fee(a), abs=1e-6)
    assert rt.REASONS[tr["reason"][0]] == "settle"


def test_unsellable_exit_row_moves_on_then_falls_back_to_settlement():
    p = make_panel(M=2, up_won=1)
    set_pred(p, [0.6, 0.6])
    p.su[0, 180] = 2.0                                 # bid size < 5 at row 180: sold at row 181
    p.su[1, 180:] = 1.0                                # never sellable: valued at settlement
    v = variant("ridge_15m-c0-d1-all-left120")
    tr = rt.Engine(p).simulate(v.signal(p), v.exit)
    assert tr["exit_row"].tolist() == [181, -1]
    assert [rt.REASONS[r] for r in tr["reason"]] == ["hold", "fallback"]
    assert tr["pnl"][1] == pytest.approx(1 - 0.50 - fee(0.50), abs=1e-6)


def test_entry_row_must_pass_the_checks():
    p = make_panel(M=3)
    set_pred(p, [0.6, 0.6, 0.6])
    p.sau[0, 2] = 4.0                                  # < 5 shares at the ask of row 2
    p.age[1, 2] = 1                                    # row 2 missing (forward-filled): not a fill row
    v = variant("hgb_5m-c0-d1-all-left15")
    tr = rt.Engine(p).simulate(v.signal(p), v.exit)
    assert tr["market"].tolist() == [2] and tr["exit_row"].tolist() == [285]


def test_run_variants_end_to_end_and_splits():
    p = make_panel(M=4)
    p.split[:] = [0, 0, 1, 1]
    s = np.arange(T)
    for m in range(4):
        set_up_book(p, m, s, (0.40 + 0.0005 * s).astype(np.float32), (0.41 + 0.0005 * s).astype(np.float32))
    set_pred(p, [0.6, 0.62, 0.65, 0.5])
    vs = [v for v in rf.variants() if v.key == (rf.F6, "ridge_5m", 0.02, 1, 0.0)]
    st, kept = rt.run_variants(p, vs, keep=[vs[0].id], progress=False)
    assert len(st) == 5 and st["n_signals"].tolist() == [3] * 5
    r = st.set_index("name").loc["ridge_5m-c2-d1-all-left120"]
    assert (r["n_A"], r["n_B"]) == (2, 1)              # 0.5 never trades
    assert vs[0].name.endswith("left120") and kept[vs[0].id]["exit_row"].tolist() == [180, 180, 180]


# ---------------------------------------------------------------- out-of-sample check


def synthetic_frame(days=28, seed=0):
    rng = np.random.default_rng(seed)
    T_ = np.arange(fx.ts("2026-03-24"), fx.ts("2026-03-24") + days * 86400, 300, dtype=np.int64)
    X = rng.standard_normal((len(T_), 3))
    up = (X[:, 0] + 2 * rng.standard_normal(len(T_)) > 0).astype(float)
    return X, up, T_


def test_oos_check_reproduces_walk_forward_and_flags_in_sample_fits():
    X, up, T_ = synthetic_frame()
    blks = [(fx.ts("2026-04-07"), fx.ts("2026-04-14")), (fx.ts("2026-04-14"), fx.ts("2026-04-21"))]
    wf, _ = fx.walk_forward(X, up, T_, 5, "ridge", blks, alpha=10.0)
    assert np.isfinite(wf).sum() == 2 * 7 * 288
    ok = rf.oos_check(X, up, T_, 5, "ridge", blks, 10.0, wf)
    assert ok["reproduced"] and ok["max_abs_diff"] == 0.0 and ok["n"] == 2 * 7 * 288
    assert ok["max_train_end_minus_block_start_s"] <= 0      # purged: every training target ended by the block start
    ins = np.full(len(T_), np.nan)
    sel = np.isfinite(wf)
    ins[sel] = fx.make_model("ridge", 10.0).fit(X, up).predict_proba1(X[sel])   # fit on everything
    bad = rf.oos_check(X, up, T_, 5, "ridge", blks, 10.0, ins)
    assert not bad["reproduced"] and bad["max_abs_diff"] > 1e-6
    miss = wf.copy()
    miss[sel.nonzero()[0][:3]] = np.nan
    assert not rf.oos_check(X, up, T_, 5, "ridge", blks, 10.0, miss)["reproduced"]


def test_coverage_counts_predictions_per_split():
    p = make_panel(M=3)
    p.split[:] = [0, 0, 1]
    S = p.start
    pr = pd.DataFrame({m: [0.55, np.nan, 0.45] for m in rf.F6_MODELS}, index=pd.Index(S, name="T"))
    rf.attach_preds(p, pr)
    cov = rf.coverage(p, pr)
    assert cov["A"]["markets"] == 2 and cov["A"]["mk_pred_ridge_5m"] == 1
    assert cov["B"]["markets"] == 1 and cov["B"]["mk_pred_hgb_15m"] == 1
    assert cov["A"]["T_pred_ridge_5m"] == 2 and cov["B"]["T_pred_ridge_5m"] == 0  # all three stamps are 4/01 (A)
