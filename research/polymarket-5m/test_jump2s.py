import time

import numpy as np
import pandas as pd
import pytest

import binary as bo
import jump2s as j2
import kacho_late

S0 = 1_780_000_200  # a 5m window open (multiple of 300), 2026-05-28
X = np.log(60000.0)


def win(*starts):
    return pd.DataFrame({"market": [f"m{s}" for s in starts], "start": list(starts),
                         "end": [s + 300 for s in starts]})


def trades(pairs):
    """[(t, bp)] -> sorted ts, log prices X + bp * 1e-4."""
    pairs = sorted(pairs)
    return np.array([p[0] for p in pairs], float), X + 1e-4 * np.array([p[1] for p in pairs], float)


# ------------------------------------------------------------------ jumps
def test_threshold_buckets_and_sign():
    starts = [S0 + 300 * k for k in range(5)]
    moves = [0.99, 1.0, 1.19, 1.2, -1.3]
    pairs = []
    for s, d in zip(starts, moves):
        pairs += [(s + 100.0, 0.0), (s + 101.0, d), (s + 250.0, 0.0)]
    ts, lp = trades(pairs)
    j = j2.jumps(ts, lp, win(*starts))
    got = {r.market: (r.bucket, r.sign, round(r.size_bp, 6)) for r in j.itertuples()}
    assert got == {f"m{starts[1]}": ("small", 1, 1.0), f"m{starts[2]}": ("small", 1, 1.19),
                   f"m{starts[3]}": ("big", 1, 1.2), f"m{starts[4]}": ("big", -1, 1.3)}
    r = j[j["market"] == f"m{starts[4]}"].iloc[0]
    assert r.t0 == starts[4] + 101.0 and r.x1 - r.x0 == pytest.approx(-1.3e-4)


def test_reference_is_last_print_at_least_1s_and_at_most_5s_old():
    a, b, c, d = (S0 + 300 * k for k in range(4))
    ts, lp = trades([(a + 100.0, 0.0), (a + 105.0, 1.5),          # ref exactly 5 s old: a jump
                     (b + 100.0, 0.0), (b + 105.01, 1.5),         # 5.01 s: no reference
                     (c + 100.0, 0.0), (c + 100.5, 1.5),          # only 0.5 s earlier: no reference
                     (d + 100.0, 0.0), (d + 100.2, 1.5), (d + 101.1, 1.5)])  # ref of 101.1 is 100.0
    j = j2.jumps(ts, lp, win(a, b, c, d))
    assert list(j["market"]) == [f"m{a}", f"m{d}"]
    r = j.iloc[1]
    assert r.t0 == pytest.approx(d + 101.1) and ts[r.ref_i] == d + 100.0 and r.bucket == "big"


def test_spacing_is_per_bucket_and_inclusive():
    # one print a second; increments at the listed seconds after the open
    inc = {20: 1.5, 25: 1.5, 30: 1.5, 32: 1.1, 35: 1.1, 40: 1.3, 42: 1.05}
    secs = np.arange(0, 300)
    lv = np.cumsum([inc.get(int(s), 0.0) for s in secs])
    j = j2.jumps(S0 + secs.astype(float), X + 1e-4 * lv, win(S0))
    big = sorted(j.loc[j.bucket == "big", "t0"] - S0)
    small = sorted(j.loc[j.bucket == "small", "t0"] - S0)
    assert big == [20, 30, 40] and small == [32, 42]


def test_window_filters_inclusive():
    a, b = S0, S0 + 300
    ts, lp = trades([(a + 13.0, 0.0), (a + 14.0, 2.0),     # 14 s after the open: out
                     (a + 280.0, 0.0), (a + 288.0, 0.0), (a + 289.0, 2.0),  # 11 s before the end: out
                     (b + 14.0, 0.0), (b + 15.0, 2.0),     # 15 s: in
                     (b + 287.0, 0.0), (b + 288.0, -2.0)])  # 12 s before the end: in
    j = j2.jumps(ts, lp, win(a, b))
    assert list(j["t0"] - b) == [15.0, 288.0] and set(j["market"]) == {f"m{b}"}


def test_closes_mode_needs_adjacent_seconds_and_t_obs_moves_the_filters():
    sec = np.array([S0 + 100, S0 + 101, S0 + 103, S0 + 104, S0 + 200, S0 + 201], float)
    lp = X + 1e-4 * np.array([0.0, 1.5, 3.0, 3.0, 3.0, 1.0])  # 101 jump; 103 vs 101 not adjacent
    j = j2.jumps(sec, lp, win(S0), mode="closes")
    assert list(j["t0"] - S0) == [101.0, 201.0] and list(j["bucket"]) == ["big", "big"]
    assert list(j["sign"]) == [1, -1]
    # trades: the move is on exchange time, filters and t0 on receipt time
    ts, lp = trades([(S0 + 286.0, 0.0), (S0 + 287.9, 2.0)])
    assert len(j2.jumps(ts, lp, win(S0))) == 1
    j = j2.jumps(ts, lp, win(S0), t_obs=ts + 0.2)  # received 288.1: 11.9 s before the end
    assert j.empty


def test_jumps_vectorised_on_a_million_prints():
    rng = np.random.default_rng(1)
    n = 1_000_000
    ts = np.sort(S0 + rng.uniform(0, 86_400, n))
    lp = X + np.cumsum(rng.normal(0, 0.15e-4, n))
    w = win(*(S0 + 300 * k for k in range(288)))
    t = time.time()
    j = j2.jumps(ts, lp, w)
    assert time.time() - t < 20
    assert len(j) > 100 and set(j["bucket"]) == {"big", "small"}
    for (m, b), g in j.groupby(["market", "bucket"]):
        assert (np.diff(np.sort(g["t0"].to_numpy())) >= 10 - 1e-6).all()


# ------------------------------------------------------------------ plan and entry helpers
def test_plan_kinds_controls_and_entry_time():
    secs = np.arange(0, 300)
    inc = {20: 1.5, 60: -1.4, 80: 1.1}
    lv = np.cumsum([inc.get(int(s), 0.0) for s in secs])
    sel = j2.jumps(S0 + secs.astype(float), X + 1e-4 * lv, win(S0))
    p = j2.plan(sel, win(S0), seed=3)
    assert p["kind"].value_counts().to_dict() == {"big": 2, "opposite": 2, "random": 2, "small": 1}
    big, opp = p[p.kind == "big"].sort_values("t0"), p[p.kind == "opposite"].sort_values("t0")
    assert list(opp["sign"]) == list(-big["sign"]) and list(opp["jump_sign"]) == list(big["sign"])
    rnd = p[p.kind == "random"]
    assert ((rnd.t0 >= S0 + 15) & (rnd.t0 <= S0 + 288)).all() and set(rnd["sign"]) <= {-1, 1}
    assert (p["t_entry"] == p["t0"] + 2.0).all()
    pd.testing.assert_frame_equal(p, j2.plan(sel, win(S0), seed=3))


def test_entry_helpers():
    snaps = np.array([10.0, 10.5, 13.0])
    assert list(j2.asof_idx(snaps, [9.9, 10.5, 11.4, 11.6, 13.0])) == [-1, 1, 1, -1, 2]
    pts = np.array([12.0, 12.5, 15.0])
    up = np.array([0.40, 0.45, 0.70])  # Up-equivalent prices (a Down print at 0.55 is 0.45)
    got = j2.first_print(pts, up, [12.0, 12.1, 13.0, 15.1], [14.0, 14.1, 15.0, 17.1], [1, -1, 1, 1])
    assert got[:3] == pytest.approx([0.40, 0.55, 0.70]) and np.isnan(got[3])
    assert list(j2.price_ok([0.01, 0.02, 0.5, 0.98, 0.99, np.nan])) == [False, True, True, True, False, False]
    assert list(j2.price_ok([0.5, 0.5], [4.99, 5.0])) == [False, True]
    assert j2.pnl(1.0, 0.6) == pytest.approx(0.4 - 0.07 * 0.6 * 0.4)


# ------------------------------------------------------------------ trend features
def _minutes(n=400, seed=0):
    rng = np.random.default_rng(seed)
    m = S0 - 60 * n + 60 * np.arange(n)
    return pd.Series(60000 * np.exp(np.cumsum(rng.normal(0, 5e-4, n))), index=m.astype(np.int64))


def test_trend_features_values_and_no_lookahead():
    c = _minutes()
    m_last = int(c.index[-30])           # a minute well inside the series
    t0 = m_last + 60 + 30.0              # half way through the next minute
    r4, vr = j2.trend_features(c, [t0])
    lc = np.log(c)
    assert r4[0] == pytest.approx(lc[m_last] - lc[m_last - 240 * 60])
    r = np.diff(lc.loc[m_last - 60 * 60:m_last].to_numpy())
    assert len(r) == 60
    s5 = np.convolve(r, np.ones(5), "valid")
    assert vr[0] == pytest.approx(np.var(s5, ddof=1) / (5 * np.var(r, ddof=1)))
    # the minute holding t0 (closes after t0) and every later minute never matter
    c2 = c.copy()
    c2.loc[m_last + 60:] *= 1.05
    assert np.allclose(j2.trend_features(c2, [t0]), (r4, vr))
    # a minute counts from the moment it has closed, not a millisecond earlier
    c3 = c.copy()
    c3.loc[m_last + 60] *= 1.01
    assert j2.trend_features(c3, [m_last + 120.0])[0][0] != pytest.approx(r4[0])
    assert j2.trend_features(c3, [m_last + 119.999])[0][0] == pytest.approx(r4[0])


def test_missing_minute_gives_nan_and_minute_closes_takes_the_last_print():
    c = _minutes()
    m_last = int(c.index[-30])
    t0 = m_last + 90.0
    gap = c.drop(m_last - 30 * 60)
    r4, vr = j2.trend_features(gap, [t0, c.index[0] + 10.0])
    assert np.isfinite(r4[0]) and np.isnan(vr[0]) and np.isnan(r4[1]) and np.isnan(vr[1])
    ts = np.array([S0 + 1.0, S0 + 59.9, S0 + 30.0, S0 + 60.0, S0 + 61.0])
    px = np.array([1.0, 3.0, 2.0, 4.0, 5.0])
    mc = j2.minute_closes(ts, px)
    assert mc.to_dict() == {S0: 3.0, S0 + 60: 5.0}


def test_h1_h2_definitions():
    h1 = j2.h1([1, -1, 1, -1, 1], [0.01, -0.02, -0.01, 0.0, np.nan])
    assert list(h1[:4]) == [1.0, 1.0, 0.0, 0.0] and np.isnan(h1[4])
    h2 = j2.h2([1.2, 1.0, 0.8, np.nan])
    assert list(h2[:3]) == [1.0, 0.0, 0.0] and np.isnan(h2[3])
    rows = pd.DataFrame({"t_ex": [S0 - 600.0], "sign": [-1]})
    c = _minutes()
    out = j2.add_trend(rows, c)
    r4, vr = j2.trend_features(c, [S0 - 600.0])
    assert out["h1"].iloc[0] == float(-r4[0] > 0) and out["h2"].iloc[0] == float(vr[0] > 1)


# ------------------------------------------------------------------ model fair
def test_settle_rule_and_point_fair():
    days = ["2026-08-06 23:55", "2026-08-10 12:00", "2026-08-20 00:00"]
    starts = [pd.Timestamp(d, tz="UTC").timestamp() for d in days]
    assert list(j2.settle_twap(starts)) == [0, 30, 60]
    up = j2.fair_side(1, X, X, 1e-4, 150.0)
    assert float(up) == pytest.approx(0.5)
    hi = j2.fair_side([1, -1], X + 2e-4, X, 1e-4, 150.0)
    assert hi[0] + hi[1] == pytest.approx(1.0) and hi[0] > 0.5
    assert hi[0] == pytest.approx(float(bo.prob_up_european(X + 2e-4, X, 1e-4, 150.0)))


def test_seconds_model_fair_continuation_and_proxy():
    rng = np.random.default_rng(2)
    start = int(pd.Timestamp("2026-08-20", tz="UTC").timestamp())  # 60 s TWAP
    sec = np.arange(start - 1200, start + 400)
    lp = X + np.cumsum(rng.normal(0, 1e-4, len(sec)))
    sp = j2.Seconds(sec, np.exp(lp))
    i = lambda s: s - (start - 1200)
    # the price known at t is the close of second floor(t) - 1
    assert float(sp.price(start + 10.7)) == pytest.approx(lp[i(start + 9)])
    k = lp[i(start - 60):i(start)].mean()
    assert j2.ref_log(sp, start)[0] == pytest.approx(k)
    t = start + 270.4                      # inside the averaging window [start + 240, start + 300)
    known = lp[i(start + 240):i(start + 270)].sum()
    sg = np.std(np.diff(lp[i(start + 269) - 600:i(start + 269) + 1]), ddof=1)
    want = bo.prob_up(lp[i(start + 269)], k, sg, 270, known_sum=known, twap=60)
    assert j2.model_fair(sp, 1, start, t)[0] == pytest.approx(float(want))
    assert j2.model_fair(sp, -1, start, t)[0] == pytest.approx(1 - float(want))
    settle = lp[i(start + 240):i(start + 300)].mean()
    assert j2.continuation_bp(sp, -1, start, lp[i(start + 100)])[0] == \
        pytest.approx(-(settle - lp[i(start + 100)]) * 1e4)
    assert j2.proxy_up(sp, start)[0] == float(settle >= k)
    # point-price market (May): strike and settlement are the closes before the open and the end
    s2 = int(pd.Timestamp("2026-05-28", tz="UTC").timestamp())
    sp2 = j2.Seconds(sec - start + s2, np.exp(lp))
    i2 = lambda s: s - (s2 - 1200)
    f = j2.model_fair(sp2, 1, s2, s2 + 100.5)
    sg2 = np.std(np.diff(lp[i2(s2 + 99) - 600:i2(s2 + 99) + 1]), ddof=1)
    assert f[0] == pytest.approx(float(bo.prob_up_european(lp[i2(s2 + 99)], lp[i2(s2 - 1)], sg2, 199.5)))


# ------------------------------------------------------------------ statistics
def _hand():
    return pd.DataFrame({"market": ["A", "A", "B", "C", "C", "C"], "pnl": [1.0, 0.0, 0.5, -1.0, 0.5, 0.2]})


def test_clustered_se_by_hand_and_kacho_late():
    t = _hand()
    m, se, n = j2.cl(t)
    # mean 0.2; per-market residual sums 0.6, 0.3, -0.9; sqrt(1.26 * 3/2) / 6
    assert (m, n) == (pytest.approx(0.2), 6) and se == pytest.approx(np.sqrt(1.26 * 1.5) / 6)
    assert (m, se, n) == pytest.approx(kacho_late.cl(t))
    s = j2.stat(t)
    assert s["t"] == pytest.approx(0.2 / se) and s["k"] == 3


def test_difference_and_ratio_se_match_the_cluster_sandwich():
    rng = np.random.default_rng(4)
    t = pd.DataFrame({"market": rng.integers(0, 15, 80), "flag": rng.integers(0, 2, 80).astype(float),
                      "pnl": rng.normal(0, 1, 80)})
    t.loc[3, "flag"] = np.nan
    d = j2.diff_stat(t, "flag")
    u = t.dropna(subset=["flag"])
    Xm = np.column_stack([np.ones(len(u)), u["flag"]])
    b = np.linalg.lstsq(Xm, u["pnl"].to_numpy(), rcond=None)[0]
    e = u["pnl"].to_numpy() - Xm @ b
    bread = np.linalg.inv(Xm.T @ Xm)
    meat = sum(np.outer(Xm[g].T @ e[g], Xm[g].T @ e[g]) for g in
               [np.flatnonzero(u["market"].to_numpy() == k) for k in np.unique(u["market"])])
    k = u["market"].nunique()
    V = bread @ meat @ bread * k / (k - 1)
    assert d["mean"] == pytest.approx(b[1]) and d["se"] == pytest.approx(np.sqrt(V[1, 1]))
    assert d["n1"] + d["n0"] == 79
    # ratio of sums with the delta-method SE
    t["num"], t["den"] = rng.normal(1, 1, 80), rng.uniform(0.5, 1.5, 80)
    r = j2.ratio_stat(t, "num", "den")
    R = t["num"].sum() / t["den"].sum()
    g = t.groupby("market")[["num", "den"]].sum()
    k = len(g)
    se = np.sqrt((((g["num"] - R * g["den"]) / t["den"].sum()) ** 2).sum() * k / (k - 1))
    assert r["ratio"] == pytest.approx(R) and r["se"] == pytest.approx(se)


def _rows(block_means, n_mk=60, per=4, sd=0.3, seed=0, flag_shift=None):
    """Big-jump rows, n_mk markets in each HF block, `per` trades a market."""
    rng = np.random.default_rng(seed)
    out = []
    for (name, lo, _), mu in zip(j2.HF_BLOCKS, block_means):
        t_lo = pd.Timestamp(lo, tz="UTC").timestamp() + 86400
        for m in range(n_mk):
            for q in range(per):
                f = float(q % 2)
                bump = 0.0 if flag_shift is None else (flag_shift if f else 0.0)
                out.append((f"{name}-{m}", t_lo + 3600 * m + 30 * q, mu + bump + rng.normal(0, sd), f))
    r = pd.DataFrame(out, columns=["market", "t0", "pnl", "h1"])
    return r.assign(kind="big", price_type="book")


def test_decision_functions():
    ok = j2.decide_main(_rows([0.08, 0.08, 0.08]), j2.HF_BLOCKS)
    assert ok["passed"] and ok["npos"] == 3 and ok["t"] >= 2
    one_block = j2.decide_main(_rows([0.4, -0.01, -0.01], sd=0.05), j2.HF_BLOCKS)
    assert one_block["mean"] > 0 and one_block["t"] >= 2 and one_block["npos"] == 1 and not one_block["passed"]
    weak = j2.decide_main(_rows([0.01, 0.01, 0.01], sd=1.0), j2.HF_BLOCKS)
    assert not weak["passed"] and weak["t"] < 2
    # H: the subset is better than its complement in every block
    h = j2.decide_h(_rows([0.0, 0.0, 0.0], flag_shift=0.15, sd=0.2), "h1", j2.HF_BLOCKS)
    assert h["passed"] and h["npos"] == 3 and h["diff"]["t"] >= 2 and h["sub"]["t"] >= 2
    # subset positive but no better than the complement: not a hypothesis that holds
    same = j2.decide_h(_rows([0.1, 0.1, 0.1], sd=0.2), "h1", j2.HF_BLOCKS)
    assert same["sub"]["t"] >= 2 and not same["passed"]
    assert j2.decide_main(_rows([0.08] * 3).iloc[:0], j2.HF_BLOCKS)["passed"] is False


def _days(spec):
    """{day: (n, mean)} -> rows, one market per trade pair."""
    out = []
    for day, (n, mu) in spec.items():
        t = pd.Timestamp(day, tz="UTC").timestamp() + 3600
        out += [(f"{day}-{k // 2}", t + 10 * k, mu) for k in range(n)]
    return pd.DataFrame(out, columns=["market", "t0", "pnl"]).assign(kind="big", price_type="book")


def test_three_day_window_counter():
    r = _days({"2026-06-01": (20, 0.03), "2026-06-02": (20, 0.03), "2026-06-03": (20, 0.0),
               "2026-06-04": (19, 0.5), "2026-06-05": (20, 0.05), "2026-06-06": (20, 0.05),
               "2026-06-07": (20, -0.05)})
    w = j2.three_day(r)
    # 06-01..03 pooled +2.0c (a hit, inclusive); 06-04 has 19 trades so 02-04, 03-05, 04-06 are no
    # windows; 05..07 pooled +1.67c
    assert (w["windows"], w["hits"], w["starts"]) == (2, 1, ["2026-06-01"])
    # pooled per share, not a mean of daily means: (0 * 40 + 0.06 * 20 + 0 * 20) / 80 = 1.5c
    p = j2.three_day(_days({"2026-07-01": (40, 0.0), "2026-07-02": (20, 0.06), "2026-07-03": (20, 0.0)}))
    assert (p["windows"], p["hits"]) == (1, 0)
    s = j2.day_summary(r)
    assert s["days"] == 6 and s["pos"] == 4 and s["best"][0] == "2026-06-05" and s["worst"][0] == "2026-06-07"


# ------------------------------------------------------------------ report helpers
def test_report_tables():
    r = _rows([0.05, 0.02, -0.01], seed=7)
    rows = []
    for kind in j2.KINDS:
        for pt in ("book", "trade"):
            rows.append(r.assign(kind=kind, price_type=pt))
    rows = pd.concat(rows, ignore_index=True)
    rows["mid0"], rows["mid2"], rows["fair0"], rows["fair2"] = 0.5, 0.54, 0.5, 0.55
    rows["cont_bp"] = 0.5
    rows["h2"] = rows["h1"]
    L = j2.main_table(rows)
    assert len(L) == 2 + 4 and "盘口价" in L[0] and "成交价" in L[0] and L[2].startswith("| 大跳")
    assert len(j2.block_table(rows, j2.HF_BLOCKS)) == 2 + 3
    D = j2.decomp_table(rows, j2.HF_BLOCKS)
    assert len(D) == 2 + 4 and "80% ±0" in D[-1] and "+0.50 ±0.00" in D[-1]
    H = j2.h_table(rows, "h1", j2.HF_BLOCKS)
    assert len(H) == 2 + 4
    d = j2.daily_lines(rows, detail=True)
    assert d[2].startswith("| ") and any("| 日期 |" in x for x in d)
    big = rows[(rows.kind == "big") & (rows.price_type == "book")]
    lines = j2.decision_lines(j2.decide_main(big, j2.HF_BLOCKS), j2.decide_h(big, "h1", j2.HF_BLOCKS),
                              j2.decide_h(big, "h2", j2.HF_BLOCKS))
    assert len(lines) == 3 and lines[0].startswith("- 主规则") and ("成立" in lines[1])
    # a lane without book mids or trade prices still gets its tables
    bare = rows[rows.price_type == "book"].drop(columns=["mid0", "mid2", "fair0", "fair2"])
    assert len(j2.main_table(bare)) == 2 + 4 and "–" in j2.decomp_table(bare, j2.HF_BLOCKS)[-1]
