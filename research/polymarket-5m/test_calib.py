import math

import numpy as np
import pandas as pd
import pytest
from scipy.stats import t as student_t

import calib as C
import resolved as R

T0 = 1_780_000_020 // 60 * 60          # a whole minute
END = T0 + 6 * 3600                    # market end used by the synthetic checkpoints


# ------------------------------------------------------------------ helpers

def mk_frame(rows):
    """Minimal decided-markets frame (the columns rs.checkpoints / calib read)."""
    base = {"kind": "above", "type": "above", "excluded": "", "end": float(END), "tstar": float(END + 60),
            "we": float("nan"), "ws": float("nan"), "tsec": float("nan"), "level": float("nan"),
            "lo": float("nan"), "hi": float("nan"), "ref": float("nan"), "official": 1.0,
            "yes_token": "y", "no_token": "n"}
    return pd.DataFrame([{**base, **r} for r in rows])


def synth(markets):
    """markets: dicts cid, kind, check, p_yes, official, yes=[(dt, price, size)], no=[...].
    Returns (cp, trades, mk) shaped like the real ones."""
    cp, tr, mk = [], [], {}
    for m in markets:
        t = END - 60 * m["check"]
        cp.append({"cid": m["cid"], "kind": m.get("kind", "above"), "check": m["check"], "t": t, "anchor": END,
                   "p_yes": m["p_yes"]})
        mk[m["cid"]] = {"cid": m["cid"], "kind": m.get("kind", "above"), "official": m["official"]}
        for side in ("yes", "no"):
            for dt, price, size in m.get(side, []):
                tr.append({"cid": m["cid"], "ts": t + dt, "taker_buy": True, "is_yes": side == "yes",
                           "price": price, "size": size})
    trades = pd.DataFrame(tr, columns=["cid", "ts", "taker_buy", "is_yes", "price", "size"])
    return pd.DataFrame(cp), trades, mk_frame(list(mk.values()))


def rows_of(markets):
    cp, tr, mk = synth(markets)
    return C.side_rows(cp, C.window_trades(tr, cp), mk)


# ------------------------------------------------------------------ bucketing

def test_q_bucket_boundaries():
    q = [0.0, 0.0199, 0.02, 0.0499, 0.05, 0.15, 0.2999, 0.3, 0.5, 0.7, 0.85, 0.9499, 0.95, 0.9899, 0.99, 1.0, np.nan]
    want = [None, None, "0.02-0.05", "0.02-0.05", "0.05-0.15", "0.15-0.3", "0.15-0.3", "0.3-0.5", "0.5-0.7",
            "0.7-0.85", "0.85-0.95", "0.85-0.95", "0.95-0.99", "0.95-0.99", None, None, None]
    assert list(C.q_bucket(q)) == want


def test_time_bins_and_types():
    assert list(C.t_bin([240, 120, 60, 30, 10, 5, 1])) == [">=120", ">=120", "30-120", "30-120", "5-30", "5-30", "<5"]
    assert {C.TYPE_OF[k] for k in ("hit_daily", "hit_weekly", "hit_monthly")} == {"hit"}
    assert len(C.all_cells()) == C.NCELLS == 128
    assert C.CHECKS == (240, 120, 60, 30, 10, 5, 1)


def test_in_any_cell():
    assert list(C.in_any_cell([0.005, 0.01, 0.015, 0.5, 0.985, 0.99, 0.995])) == [False, False, True, True, True,
                                                                                   False, False]


# ------------------------------------------------------------------ both sides

def test_both_side_rows_window_and_weights():
    r = rows_of([{"cid": "a", "check": 60, "p_yes": 0.3, "official": 1.0,
                  "yes": [(0, 0.25, 10), (59, 0.35, 30), (60, 0.9, 100), (-1, 0.9, 100)],
                  "no": [(10, 0.70, 5)]}])
    assert len(r) == 2
    y = r[r["is_yes"]].iloc[0]
    n = r[~r["is_yes"]].iloc[0]
    assert y["q"] == pytest.approx(0.3) and n["q"] == pytest.approx(0.7)
    assert y["result"] == 1.0 and n["result"] == 0.0
    assert y["trades"] == 2 and y["shares"] == 40          # ts = t + 60 and t - 1 are outside [t, t + 60)
    assert y["p"] == pytest.approx((0.25 * 10 + 0.35 * 30) / 40)
    assert y["pnl"] == pytest.approx(1 - y["p"])
    assert y["pnl_taker"] == pytest.approx(1 - y["p"] - 0.07 * y["p"] * (1 - y["p"]))
    assert n["p"] == pytest.approx(0.70) and n["pnl"] == pytest.approx(-0.70)
    assert (y["qb"], n["qb"]) == ("0.3-0.5", "0.7-0.85")
    assert y["tb"] == n["tb"] == "30-120" and y["type"] == "above"
    assert y["gap"] == pytest.approx(0.3 - y["p"])


def test_both_sides_without_trades_and_tie():
    r = rows_of([{"cid": "a", "check": 1, "p_yes": 0.6, "official": 0.5, "kind": "hit_weekly"}])
    assert len(r) == 2 and (r["shares"] == 0).all() and r["p"].isna().all()
    assert (r["result"] == 0.5).all() and (r["type"] == "hit").all() and (r["tb"] == "<5").all()
    s = C.stats(r, 10)
    assert s["markets"] == 0 and s["points"] == 2 and s["shares_day"] == 0


def test_windows_of_several_checkpoints_do_not_mix():
    m = [{"cid": "a", "check": c, "p_yes": 0.5, "official": 0.0, "yes": [(5, 0.1 * (i + 1), 1)]}
         for i, c in enumerate((240, 120, 60, 30, 10, 5, 1))]
    r = rows_of(m)
    y = r[r["is_yes"]].set_index("check")["p"]
    assert [round(y[c], 2) for c in (240, 120, 60, 30, 10, 5, 1)] == [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7]


def test_segment_trades_cached_vs_fetched():
    cp, tr, mk = synth([{"cid": "a", "check": 240, "p_yes": 0.5, "official": 1.0},
                        {"cid": "a", "check": 60, "p_yes": 0.5, "official": 1.0}])
    t240, t60 = END - 240 * 60, END - 3600
    cached = pd.DataFrame([{"cid": "a", "ts": t60 + 3, "taker_buy": True, "is_yes": True, "price": 0.4, "size": 2}])
    extra = pd.DataFrame([{"cid": "a", "ts": t240 + 7, "taker_buy": False, "is_yes": False, "price": 0.6, "size": 3,
                           "check": 240}])
    info = pd.DataFrame([{"cid": "a", "fstart": t60, "truncated": False}])
    assert list(C.fetch_cover(cp, info)) == [False, True]
    wt = C.segment_trades(cp, cached, info, extra)
    assert sorted(wt["check"]) == [60, 240] and len(wt) == 2


# ------------------------------------------------------------------ the model sees nothing after t

def spot_walk(seed=1, hours=8):
    rng = np.random.default_rng(seed)
    n = hours * 3600
    sec0 = END - 7 * 3600
    cl = np.rint(np.exp(np.log(6_000_000) + np.cumsum(rng.normal(0, 4e-5, n))))   # cents
    return sec0, cl


def make_spot(sec0, cl):
    return R.Spot(sec0, cl.copy(), cl.copy(), cl.copy())


@pytest.mark.parametrize("market", [
    {"cid": "ab", "kind": "above", "type": "above", "lo": 60_000.0},
    {"cid": "rg", "kind": "range", "type": "range", "lo": 59_500.0, "hi": 60_500.0},
    {"cid": "ud", "kind": "updown_day", "type": "updown_day", "ref": 60_100.0},
    {"cid": "hu", "kind": "hit_daily", "type": "hit_up", "level": 60_900.0, "ws": float(END - 20 * 3600),
     "we": float(END), "tstar": float("nan")},
])
def test_model_uses_no_data_after_checkpoint(market):
    sec0, cl = spot_walk()
    mk = mk_frame([market])
    base = C.model_points(mk, make_spot(sec0, cl)).set_index("check")["p_yes"]
    assert set(base.index) == set(C.CHECKS)
    for c in C.CHECKS:
        t = END - 60 * c
        k = t - sec0
        bad = cl.copy()
        bad[k:] = bad[k:] * 1.07                             # everything from t on is different
        got = C.model_points(mk, make_spot(sec0, bad)).set_index("check")["p_yes"]
        for c2 in C.CHECKS:
            if END - 60 * c2 <= t:                            # checkpoints at or before t: unchanged
                assert got[c2] == pytest.approx(base[c2], abs=0, rel=0)
        before = cl.copy()
        before[k - 1] = before[k - 1] * 1.07                 # the last second before t matters
        assert C.model_points(mk, make_spot(sec0, before)).set_index("check")["p_yes"][c] != base[c]


def test_hit_model_only_uses_whether_touched_before_t():
    sec0, cl = spot_walk(2)
    m = {"cid": "hu", "kind": "hit_daily", "type": "hit_up", "level": 60_900.0, "ws": float(END - 20 * 3600),
         "we": float(END), "tstar": float("nan")}
    spot = make_spot(sec0, cl)
    a = C.model_points(mk_frame([{**m, "tsec": float(END - 30)}]), spot)        # touched after every checkpoint
    b = C.model_points(mk_frame([{**m, "tsec": float("nan")}]), spot)           # never touched
    pd.testing.assert_frame_equal(a.reset_index(drop=True), b.reset_index(drop=True))
    c = C.model_points(mk_frame([{**m, "tsec": float(END - 90 * 60)}]), spot)   # touched 90 min before the end
    assert set(c["check"]) == {240, 120}


# ------------------------------------------------------------------ clustered SE

def manual_cluster(x, w, g):
    x, w = np.asarray(x, float), np.asarray(w, float)
    W = w.sum()
    mu = (w * x).sum() / W
    s = pd.Series(w * (x - mu)).groupby(np.asarray(g)).sum().to_numpy()
    G = len(s)
    se = math.sqrt((s ** 2).sum() * G / (G - 1)) / W
    return mu, se, mu / se, G


def test_clustered_se_and_one_sided_p():
    rng = np.random.default_rng(3)
    m = []
    for i in range(25):
        off = float(rng.random() < 0.7)
        m.append({"cid": f"m{i}", "check": 60, "p_yes": 0.6, "official": off,
                  "yes": [(1, 0.55 + 0.01 * (i % 5), 5 + i), (2, 0.6, 3)]})
        m.append({"cid": f"m{i}", "check": 30, "p_yes": 0.62, "official": off, "yes": [(4, 0.58, 7)]})
    r = rows_of(m)
    s = C.stats(r[r["is_yes"]], 10)
    y = r[r["is_yes"] & (r["shares"] > 0)]
    mu, se, t, G = manual_cluster(y["result"] - y["p"], y["shares"], y["cid"])
    assert (s["pnl"], s["se"], s["t"], s["markets"]) == (pytest.approx(mu), pytest.approx(se), pytest.approx(t), G)
    assert s["p1"] == pytest.approx(student_t.sf(t, G - 1))
    assert s["p1"] > 1 - __import__("scipy").stats.norm.cdf(t)       # t with G - 1 df: never smaller than normal
    # the two checkpoints of a market share one result: clustering by market is wider than by row here
    _, se_rows, _, _ = manual_cluster(y["result"] - y["p"], y["shares"], np.arange(len(y)))
    assert se > se_rows


def test_cell_dollars_losing_and_worst():
    m = [{"cid": "w", "check": 10, "p_yes": 0.9, "official": 1.0, "yes": [(0, 0.8, 100)]},
         {"cid": "l", "check": 5, "p_yes": 0.9, "official": 0.0, "yes": [(0, 0.85, 40), (1, 0.95, 20)]}]
    r = rows_of(m)
    s = C.stats(r[r["is_yes"]], days=4)
    assert s["markets"] == 2 and s["losing"] == 1 and s["trades"] == 3
    assert s["shares_day"] == pytest.approx(160 / 4)
    assert s["cost_day"] == pytest.approx((80 + 34 + 19) / 4)
    assert s["profit_day"] == pytest.approx((20 - 53) / 4)
    assert s["worst"] == pytest.approx(-53)
    assert s["pnl"] == pytest.approx((20 - 53) / 160)


# ------------------------------------------------------------------ Bonferroni on D, V only for candidates

def cell_rows(cid_prefix, n, q, price, wins, check=60, kind="above"):
    """n markets, Yes side at q bought at price, the first `wins` win."""
    return [{"cid": f"{cid_prefix}{i}", "check": check, "kind": kind, "p_yes": q, "official": float(i < wins),
             "yes": [(1, price, 10)]} for i in range(n)]


def test_select_candidates_uses_bonferroni_and_min_markets():
    tab = pd.DataFrame([
        {"qb": "0.5-0.7", "tb": "5-30", "type": "above", "pnl": 0.05, "p1": 1e-5, "markets": 30},     # in
        {"qb": "0.5-0.7", "tb": "<5", "type": "above", "pnl": 0.05, "p1": 0.001, "markets": 300},     # p < 0.05 only
        {"qb": "0.7-0.85", "tb": "<5", "type": "range", "pnl": 0.05, "p1": 1e-9, "markets": 29},      # too few
        {"qb": "0.7-0.85", "tb": "<5", "type": "hit", "pnl": -0.05, "p1": 1e-9, "markets": 100},      # pnl <= 0
        {"qb": "0.7-0.85", "tb": "5-30", "type": "hit", "pnl": 0.05, "p1": float("nan"), "markets": 100},
        {"qb": "0.95-0.99", "tb": "<5", "type": "hit", "pnl": 0.01, "p1": 1e-6, "markets": 40},       # in, used
    ])
    c = C.select_candidates(tab)
    assert list(zip(c["qb"], c["tb"], c["type"])) == [("0.5-0.7", "5-30", "above"), ("0.95-0.99", "<5", "hit")]
    assert list(c["used"]) == [False, True]
    assert 0.05 / 128 < 0.001                       # the second row would pass an unadjusted test


def test_d_selection_then_v_only_candidates():
    # D: a strong cell (above, 30-120, 0.5-0.7: q 0.6, price 0.4, 80 % win) and a weak one (range, <5)
    d = rows_of(cell_rows("a", 40, 0.6, 0.40, 32) + cell_rows("r", 40, 0.6, 0.55, 26, check=1, kind="range"))
    tab = C.cell_table(d, 100)
    assert len(tab) == 128
    cand = C.select_candidates(tab)
    assert list(zip(cand["qb"], cand["tb"], cand["type"])) == [("0.5-0.7", "30-120", "above")]
    # V: the candidate is weaker, another cell is very strong - only the candidate is computed
    v = rows_of(cell_rows("va", 40, 0.6, 0.45, 26) + cell_rows("vr", 40, 0.6, 0.10, 39, check=1, kind="range"))
    vt, k = C.evaluate_v(v, 100, cand)
    assert k == 1 and len(vt) == 1 and vt.iloc[0]["type"] == "above"
    assert vt.iloc[0]["thr"] == pytest.approx(0.05)
    # the selection never looks at V: the same D gives the same candidates whatever V is
    assert C.select_candidates(C.cell_table(d, 100)).equals(cand)


def test_v_threshold_is_alpha_over_k_and_used_band_not_counted():
    cand = pd.DataFrame([{"qb": "0.5-0.7", "tb": "30-120", "type": "above"},
                         {"qb": "0.3-0.5", "tb": "30-120", "type": "above"},
                         {"qb": "0.95-0.99", "tb": "30-120", "type": "above"}])
    cand["used"] = cand["qb"] == C.USED
    v = rows_of(cell_rows("x", 40, 0.6, 0.50, 26) + cell_rows("u", 40, 0.97, 0.80, 40))
    vt, k = C.evaluate_v(v, 100, cand)
    assert k == 2 and set(vt["thr"]) == {0.025}
    r = vt.set_index("qb")
    assert 0.025 < r.loc["0.5-0.7", "p1"] < 0.05 and not r.loc["0.5-0.7", "pass"]   # passes 0.05, not 0.05 / 2
    assert r.loc["0.95-0.99", "used"] and not r.loc["0.95-0.99", "pass"]           # strong but already used
    vt1, k1 = C.evaluate_v(v, 100, cand.iloc[[0, 2]])
    assert k1 == 1 and vt1.set_index("qb").loc["0.5-0.7", "pass"]


# ------------------------------------------------------------------ gap rule

def gap_markets(prefix, wins_a, wins_b, wins_c, n=40, price=0.55):
    """Three groups of n markets, Yes bought at price with q - p = 0.03 (A), 0.05 (B), 0.07 (C)."""
    out = []
    for g, q, wins in (("A", 0.58, wins_a), ("B", 0.60, wins_b), ("C", 0.62, wins_c)):
        out += cell_rows(f"{prefix}{g}", n, q, price, wins)
    return out


def test_gap_rule_m_chosen_on_d_only_and_tested_once_on_v():
    d = rows_of(gap_markets("d", 8, 36, 22))       # A loses, B wins, C even: m = 4 cents is best on D
    v = rows_of(gap_markets("v", 38, 20, 20))      # on V only A wins: m = 2 cents would be best there
    sel = C.gap_rows(d, 0.04)
    assert sel["q"].sub(sel["p"]).min() >= 0.04 and len(sel) == 80
    m, tab, ok = C.choose_m(d, 100)
    assert m == 0.04 and ok
    assert C.choose_m(v, 100)[0] == 0.02            # what V alone would have chosen
    s, v_ok = C.gap_test(v, 100, m)
    assert s["markets"] == 80 and not v_ok          # tested at D's m only
    # D's choice does not depend on V
    assert C.choose_m(d, 100)[0] == C.choose_m(rows_of(gap_markets("d", 8, 36, 22)), 100)[0]


def test_gap_universe_excludes_out_of_cell_and_control():
    r = rows_of([{"cid": "a", "check": 60, "p_yes": 0.995, "official": 1.0, "yes": [(1, 0.9, 10)]},
                 {"cid": "b", "check": 60, "kind": "updown_4h", "p_yes": 0.6, "official": 1.0, "yes": [(1, 0.4, 10)]},
                 {"cid": "c", "check": 60, "p_yes": 0.97, "official": 1.0, "yes": [(1, 0.9, 10)]}])
    u = C.gap_rows(r, 0.02)
    assert list(u["cid"]) == ["c"]
    assert not len(C.gap_rows(r, 0.02, drop_used=True))
