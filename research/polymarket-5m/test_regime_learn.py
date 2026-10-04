"""regime_learn.py (REGIME.md's learning and judging) on synthetic windows tables; no network."""
import json

import numpy as np
import pandas as pd
import pytest

import regime_learn as R

DAY = 86400


def ts(d):
    return int(pd.Timestamp(d, tz="UTC").timestamp())


def synth(seed=0, start="2026-05-18", end="2026-08-30", flip_b=False, edge=0.30):
    """One row per 5m window. vr60 is a persistent trend / range regime (AR(1) in log). Planted:
    'follow' trades 60 % of windows and makes +edge $ a trade when vr60 > 1, -edge when <= 1 (always
    on: about 0); with flip_b the sign flips from B on (the A rule then loses on B). 'H' has a tiny
    positive edge (the best always-on), 'revert' a small negative one; H is not evaluable in 2 %."""
    rng = np.random.default_rng(seed)
    st = np.arange(ts(start), ts(end), 300)
    n = len(st)
    e = rng.normal(0, 0.1, n)
    z = np.zeros(n)
    for i in range(1, n):
        z[i] = 0.97 * z[i - 1] + e[i]
    vr60 = np.exp(z)
    W = pd.DataFrame({"start": st, "market": [f"btc-updown-5m-{s}" for s in st], "vr60": vr60,
                      "rv5m": np.exp(rng.normal(-0.6, 0.3, n)), "rv30m": np.exp(rng.normal(-0.5, 0.3, n)),
                      "rv1h": np.exp(rng.normal(-0.5, 0.25, n)), "dvol_rv": rng.normal(0, 0.1, n),
                      "ret4h": rng.normal(0, 0.01, n), "vol_rel": np.exp(rng.normal(0, 0.4, n)),
                      "funding": rng.normal(1e-4, 5e-5, n), "basis": rng.normal(2, 1, n),
                      "pm_spread": rng.choice([0.01, 0.02, 0.03], n), "ask_size": np.exp(rng.normal(5, 1, n)),
                      "book_ups": np.exp(rng.normal(2, 0.5, n))})
    sign = np.where(vr60 > 1, 1.0, -1.0)
    if flip_b:
        sign = np.where(st >= ts("2026-07-16"), -sign, sign)

    def strat(name, p_trade, mean, sd, hold_h):
        tr = rng.random(n) < p_trade
        pnl = np.where(tr, mean + rng.normal(0, sd, n), 0.0)
        cost = np.where(tr, 5 * 0.52, 0.0)
        W[f"pnl_{name}"] = pnl
        W[f"trades_{name}"] = tr.astype(float)
        W[f"shares_{name}"] = 5.0 * tr
        W[f"cost_{name}"] = cost
        W[f"caph_{name}"] = cost * hold_h

    strat("H", 0.2, 0.02, 1.5, 150 / 3600)
    strat("follow", 0.6, edge * sign, 1.0, 30 / 3600)
    strat("revert", 0.3, -0.05, 1.0, 60 / 3600)
    bad = rng.random(n) < 0.02
    for q in R.QTYS:
        W.loc[bad, f"{q}_H"] = np.nan
    return W


@pytest.fixture
def fast_models(monkeypatch):
    """Fewer boosting iterations: the logic tests do not depend on the fixed parameters."""
    monkeypatch.setitem(R.HGB_PARAMS, "max_iter", 20)


@pytest.fixture(scope="module")
def planted(tmp_path_factory):
    """The full run with the fixed parameters on the planted table."""
    d = tmp_path_factory.mktemp("planted")
    W = synth()
    res, spec, text = R.run(W, d / "regime-learn.md", d / "regime-frozen.json", log=lambda *a: None)
    return dict(W=W, res=res, spec=spec, text=text, dir=d)


def quiet(*a):
    pass


# --------------------------------------------------------------------------- loading
def test_loader_maps_producer_names_and_derives_hour_and_weekend():
    st = np.array([ts("2026-05-30 13:05"), ts("2026-06-01 00:00"), ts("2026-05-30 13:05")])
    raw = pd.DataFrame({"Window_Start": st * 1000, "H_pnl": [0.1, -0.2, 9.0], "PNL-follow": [0.3, 0.0, 9.0],
                        "fade_pnl": [np.nan, 0.5, 9.0], "n_trades_h": [1, 2, 9], "VR60": [1.2, 0.8, 9.0],
                        "dvol_minus_rv30": [0.05, -0.02, 9.0], "open_spread": [0.01, 0.02, 9.0],
                        "cap_hours_follow": [0.4, 0.0, 9.0], "something_else": [1, 2, 3]})
    W, mapping, strats, missing = R.load_windows(raw, log=quiet)
    assert mapping["start"] == "Window_Start" and mapping["pnl_H"] == "H_pnl"
    assert mapping["pnl_follow"] == "PNL-follow" and mapping["pnl_revert"] == "fade_pnl"
    assert mapping["trades_H"] == "n_trades_h" and mapping["caph_follow"] == "cap_hours_follow"
    assert mapping["vr60"] == "VR60" and mapping["dvol_rv"] == "dvol_minus_rv30" and mapping["pm_spread"] == "open_spread"
    assert strats == ["H", "follow", "revert"]
    assert len(W) == 2                                   # the repeated start keeps the first row
    assert list(W["start"]) == sorted(st[:2]) and W["pnl_H"].tolist() == [0.1, -0.2]
    assert np.isnan(W["pnl_revert"].iloc[0]) and np.isnan(W["shares_H"]).all()
    assert W["hour"].tolist() == [13.0, 0.0]
    wk = [float(pd.Timestamp(s, unit="s").dayofweek >= 5) for s in W["start"]]
    assert W["weekend"].tolist() == wk == [1.0, 0.0]     # 05-30 is a Saturday, 06-01 a Monday
    assert "hour" not in missing and "rv5m" in missing and np.isnan(W["rv5m"]).all()
    assert list(W["segment"]) == ["A", "A"]


def test_loader_widens_a_long_table_and_takes_start_from_end():
    e = [ts("2026-06-01 00:05"), ts("2026-06-01 00:10")]
    raw = pd.DataFrame({"window_end": [e[0], e[0], e[0], e[1], e[1]],
                        "strategy": ["H", "follow", "made_up", "H", "fade"],
                        "pnl": [0.1, 0.2, 9.0, -0.3, 0.4], "shares": [5, 5, 9, 5, 0],
                        "vr60": [1.1, 1.1, 1.1, 0.9, 0.9]})
    W, mapping, strats, _ = R.load_windows(raw, log=quiet)
    assert strats == ["H", "follow", "revert"] and list(W["start"]) == [e[0] - 300, e[1] - 300]
    assert W["pnl_H"].tolist() == [0.1, -0.3] and W["shares_H"].tolist() == [5, 5]
    assert W["pnl_follow"].iloc[0] == 0.2 and np.isnan(W["pnl_follow"].iloc[1])
    assert np.isnan(W["pnl_revert"].iloc[0]) and W["pnl_revert"].iloc[1] == 0.4
    assert W["vr60"].tolist() == [1.1, 0.9] and np.isnan(W["trades_H"]).all()


def test_segments():
    s = [ts("2026-05-24 23:55"), ts("2026-05-25"), ts("2026-07-15 23:55"), ts("2026-07-16"), ts("2026-08-15 23:55"),
         ts("2026-08-16"), ts("2026-08-29 23:55"), ts("2026-08-30")]
    assert list(R.segment_of(s)) == ["", "A", "A", "B", "B", "C", "C", ""]
    assert pd.Timestamp(R.a_oos_from(), unit="s").strftime("%m-%d") == "06-08"


# --------------------------------------------------------------------------- leakage
def test_trailing_features_see_only_windows_that_ended_a_slot_before():
    rng = np.random.default_rng(1)
    st = ts("2026-06-01") + 300 * np.arange(200)
    keep = np.ones(200, bool)
    keep[[50, 51, 120]] = False                         # missing windows (no row)
    W = pd.DataFrame({"start": st[keep], "pnl_H": rng.normal(0, 1, keep.sum())})
    H = R.add_trailing(W, ["H"])
    pnl = pd.Series(np.nan, index=np.arange(200))
    pnl[np.flatnonzero(keep)] = W["pnl_H"].to_numpy()
    for k in (60, 130, 199):
        row = int(np.flatnonzero(H["start"].to_numpy() == st[k])[0])
        for n in (12, 48):
            want = pnl[max(0, k - n - 1):k - 1].sum(min_count=1)   # slots k-n-1 .. k-2
            assert H[f"hot{n}_H"].iloc[row] == pytest.approx(want)
        # changing the window itself, every later one, or the one just ended changes nothing
        W2 = W.copy()
        later = W2["start"].to_numpy() >= st[k - 1]
        W2.loc[later, "pnl_H"] = 1e6
        H2 = R.add_trailing(W2, ["H"])
        assert H2["hot12_H"].iloc[row] == H["hot12_H"].iloc[row]
        assert H2["hot48_H"].iloc[row] == H["hot48_H"].iloc[row]
        # the one ended two slots earlier is used
        W3 = W.copy()
        W3.loc[W3["start"] == st[k - 2], "pnl_H"] += 100.0
        assert R.add_trailing(W3, ["H"])["hot12_H"].iloc[row] == pytest.approx(H["hot12_H"].iloc[row] + 100.0)
    assert np.isnan(H["hot12_H"].iloc[0]) and np.isnan(H["hot12_H"].iloc[1])


def test_trainable_needs_the_window_ended_a_slot_before():
    b0 = ts("2026-06-08")
    start = np.array([b0 - 900, b0 - 600, b0 - 300, b0])
    assert list(R.trainable(start, np.ones(4), b0)) == [True, True, False, False]
    assert list(R.trainable(start, np.array([np.nan, 1, 1, 1]), b0)) == [False, True, False, False]


def test_walk_forward_never_sees_the_block_or_later(fast_models):
    W = R.add_trailing(synth(seed=3, start="2026-05-25", end="2026-07-16"), ["follow"])
    env = R.env_features(W)
    X = W[R.features_for("follow", env)].to_numpy(float)
    y = W["pnl_follow"].to_numpy(float)
    start = W["start"].to_numpy(float)
    sc, parts, pm = R.walk_forward(X, y, start)
    blocks = [(b0, b1) for b0, b1, oos in R.a_blocks() if oos]
    assert np.isnan(sc[start < blocks[0][0]]).all() and np.isfinite(sc[start >= blocks[0][0]]).all()
    assert np.nanmax(np.abs(sc)) <= 1.0
    rng = np.random.default_rng(0)
    for b0, b1 in (blocks[0], blocks[3]):
        te = (start >= b0) & (start < b1)
        y2, X2 = y.copy(), X.copy()
        y2[start >= b0 - 300] = rng.normal(0, 50, (start >= b0 - 300).sum())   # labels not known at b0
        X2[start >= b1] = rng.normal(0, 50, X2[start >= b1].shape)              # rows after the block
        sc2, _, pm2 = R.walk_forward(X2, y2, start)
        np.testing.assert_array_equal(sc2[te], sc[te])
        np.testing.assert_array_equal(pm2[te], pm[te])
        # whereas a label that was known (ended >= 300 s before b0) does matter
        y3 = y.copy()
        y3[start < b0 - 600] += 5.0
        assert not np.allclose(R.walk_forward(X, y3, start)[2][te], pm[te])


def test_freeze_and_final_fit_refuse_rows_outside_A(tmp_path, fast_models):
    W = R.add_trailing(R.load_windows(synth(seed=4), log=quiet)[0], ["H", "follow", "revert"])
    with pytest.raises(ValueError):
        R.freeze(W, tmp_path / "f.json", ["H"], ["vr60"], log=quiet)
    with pytest.raises(ValueError):
        R.fit_final(W[W["segment"] != "C"], ["H"], ["vr60"])
    assert not (tmp_path / "f.json").exists()


def test_frozen_before_B_and_C_are_scored_and_independent_of_them(tmp_path, monkeypatch, fast_models):
    calls = []
    real = R.score_segment

    def spy(final, S, strats, env):
        segs = set(R.segment_of(S["start"].to_numpy(float)))
        calls.append((segs, (tmp_path / "a" / "frozen.json").exists()))
        return real(final, S, strats, env)

    monkeypatch.setattr(R, "score_segment", spy)
    W = synth(seed=5)
    res, spec, _ = R.run(W, tmp_path / "a" / "out.md", tmp_path / "a" / "frozen.json", log=quiet)
    assert calls and all(frozen for _, frozen in calls)
    assert {"B"} in [c[0] for c in calls]
    # B and C rows changed beyond recognition: the frozen rule is the same
    W2 = W.copy()
    bc = np.isin(R.segment_of(W2["start"].to_numpy(float)), ["B", "C"])
    late = W2["start"].to_numpy() >= ts("2026-07-16")
    assert (bc == late).all()
    rng = np.random.default_rng(9)
    for c in ("pnl_H", "pnl_follow", "pnl_revert", "vr60", "rv30m", "dvol_rv"):
        W2.loc[late, c] = rng.normal(0, 3, late.sum())
    _, spec2, _ = R.run(W2, tmp_path / "b" / "out.md", tmp_path / "b" / "frozen.json", log=quiet)
    drop = ("made",)
    assert {k: v for k, v in spec.items() if k not in drop} == {k: v for k, v in spec2.items() if k not in drop}


def test_C_scores_do_not_depend_on_C_labels_and_B_scores_not_on_later_ones(fast_models):
    W = R.add_trailing(R.load_windows(synth(seed=6), log=quiet)[0], ["H", "follow", "revert"])
    A = W[W["segment"] == "A"].reset_index(drop=True)
    env = R.env_features(A)
    final = R.fit_final(A, ["H", "follow", "revert"], env)
    B = W[W["segment"] == "B"].reset_index(drop=True)
    sc, _ = R.score_segment(final, B, ["follow"], env)
    W2 = W.copy()
    cut = ts("2026-08-01")
    W2.loc[W2["start"] >= cut - 300, "pnl_follow"] = 1e3        # the window ending at the cut and later
    W2 = R.add_trailing(W2[[c for c in W2.columns if not c.startswith("hot")]], ["H", "follow", "revert"])
    B2 = W2[W2["segment"] == "B"].reset_index(drop=True)
    sc2, _ = R.score_segment(final, B2, ["follow"], env)
    before = B["start"].to_numpy() < cut
    np.testing.assert_array_equal(sc["follow"][before], sc2["follow"][before])
    assert not np.allclose(np.nan_to_num(sc["follow"][~before]), np.nan_to_num(sc2["follow"][~before]))


# --------------------------------------------------------------------------- the planted regime
def test_planted_switch_beats_every_always_on_out_of_sample(planted):
    res, spec = planted["res"], planted["spec"]
    assert spec["threshold"] in R.THRESHOLDS
    assert spec["best_on_a"] == max(spec["always_on_a"], key=lambda s: spec["always_on_a"][s]["mean"])
    for seg in ("A", "B"):
        st = res["segments"][seg]["stats"]
        best_always = max(st[f"on_{s}"]["d_mean"] for s in spec["strategies"])
        assert st["switch"]["d_mean"] > best_always + 10.0      # $ a day
    b = res["segments"]["B"]
    assert b["stats"]["switch"]["d_t"] >= 2 and b["pass"] and res["b_pass"]
    assert b["diff"]["mean"] > 0 and b["diff"]["t"] >= 2
    # it opens follow in trend windows and keeps it closed in range windows
    S = b["S"]
    m = b["rules"]["switch"]["follow"]
    trend = S["vr60"].to_numpy() > 1
    assert m[trend].mean() > 0.8 and m[~trend].mean() < 0.2
    assert b["rules"]["switch"]["revert"].mean() < 0.2
    # C (complete, B passed): opened once, recorded, passes
    assert res["c_open"] and res["c_pass"] and res["c_clean"]
    rec = json.loads((planted["dir"] / "regime-frozen-c.json").read_text(encoding="utf-8"))
    assert rec["c_pass"] and rec["frozen_made"] == spec["made"]


def test_report_units_and_sections(planted):
    text = planted["text"]
    for s in ("## 判定", "## 冻结的规则", "### A 段", "### B 段", "### C 段", "## 每个状态下的每个策略",
              "胜率", "盈亏比", "每份盈亏", "份/天", "买入花费 $/天", "利润 $/天", "占用资金 $", "最大回撤 $",
              "趋势·低波·DVOL>RV", "震荡·高波·DVOL≤RV", "手工趋势/震荡切换", "A 段最好的一个一直开", "**通过**"):
        assert s in text, s
    assert "¢" in text and "$" in text
    written = (planted["dir"] / "regime-learn.md").read_text(encoding="utf-8")
    assert written == text


def test_C_is_judged_once(tmp_path, fast_models):
    W = synth(seed=7)
    fz = tmp_path / "frozen.json"
    res1, spec1, _ = R.run(W, tmp_path / "o.md", fz, log=quiet)
    assert res1["b_pass"] and res1["c_open"] and res1["c_clean"]
    rec1 = json.loads(R.c_log_path(fz).read_text(encoding="utf-8"))
    # the same data again: the same verdict, the first record is kept
    res2, spec2, _ = R.run(W, tmp_path / "o.md", fz, log=quiet)
    assert spec2["made"] == spec1["made"] and res2["c_pass"] == res1["c_pass"] and res2["c_clean"]
    assert json.loads(R.c_log_path(fz).read_text(encoding="utf-8")) == rec1
    # C rows changed afterwards: the first opening stays the verdict, the new numbers are flagged
    W3 = W.copy()
    c = W3["start"].to_numpy() >= ts("2026-08-16")
    W3.loc[c, "pnl_follow"] = -1.0
    res3, _, text3 = R.run(W3, tmp_path / "o.md", fz, log=quiet)
    assert res3["c_clean"] is False and res3["c_pass"] == rec1["c_pass"]
    assert "以首次为准" in text3
    # a new freeze cannot spend C again cleanly
    fz.unlink()
    res4, _, text4 = R.run(W, tmp_path / "o.md", fz, log=quiet)
    assert res4["c_pass"] is None and "不是干净的锁箱" in text4


def test_C_stays_closed_when_B_fails(tmp_path, fast_models):
    res, spec, text = R.run(synth(seed=8, flip_b=True), tmp_path / "o.md", tmp_path / "f.json", log=quiet)
    assert not res["b_pass"] and not res["c_open"] and "C" not in res["segments"]
    assert "锁箱没开（B 未通过）" in text and "### C 段" not in text and "| C |" not in text
    assert not R.c_log_path(tmp_path / "f.json").exists()


def test_C_stays_closed_when_incomplete_or_A_changed(tmp_path, fast_models):
    W = synth(seed=7)
    gap = (W["start"] >= ts("2026-08-20")) & (W["start"] < ts("2026-08-20 14:00"))
    res, _, text = R.run(W[~gap], tmp_path / "o.md", tmp_path / "f.json", log=quiet)
    assert res["b_pass"] and not res["c_open"] and "C 段不完整" in text
    # A changed after the freeze: the old frozen file is used, C stays closed
    W2 = W.copy()
    W2.loc[W2["start"] < ts("2026-06-01"), "pnl_H"] += 0.5
    res2, _, text2 = R.run(W2, tmp_path / "o.md", tmp_path / "f.json", log=quiet)
    assert not res2["fp_ok"] and not res2["c_open"] and "指纹不符" in text2


# --------------------------------------------------------------------------- rules and units
def test_hand_switch_and_switch_masks():
    W = pd.DataFrame({"vr60": [1.5, 1.0, 0.7, np.nan]})
    m = R.hand_open(W, list(R.STRATS))
    assert list(m["follow"]) == list(m["direction"]) == [True, False, False, False]
    assert list(m["revert"]) == list(m["maker"]) == [False, True, True, False]
    assert not m["H"].any() and not m["late"].any()
    o = R.switch_open({"H": np.array([0.1, 0.05, np.nan, -0.2])}, 0.05)
    assert list(o["H"]) == [True, False, False, False]


def test_score_is_reward_over_risk_and_bounded():
    s = R.rr_score([0.1, -0.3, 0.5, 0.0, np.nan], [0.5, 0.2, 0.4, 0.0, 1.0])
    assert s[0] == pytest.approx(0.2) and s[1] == pytest.approx(-1.0) and s[2] == pytest.approx(1.0)
    assert s[3] == 0.0 and np.isnan(s[4])


def test_threshold_is_the_best_A_out_of_sample_daily_mean(planted):
    spec = planted["spec"]
    c = spec["candidates"]
    best = max(c, key=lambda x: (x["mean"], x["threshold"]))
    assert spec["threshold"] == best["threshold"] and [x["threshold"] for x in c] == list(R.THRESHOLDS)
    assert all(x["n"] == spec["a_oos_days"] for x in c) and spec["a_oos_days"] == 38   # 06-08 .. 07-15


def test_perf_units():
    d0 = ts("2026-07-20") // DAY
    S = pd.DataFrame({"start": [ts("2026-07-20 00:00"), ts("2026-07-20 00:05"), ts("2026-07-21 00:00"),
                                ts("2026-07-22 00:00")],
                      "pnl_H": [1.0, -0.5, 2.0, 0.0], "trades_H": [1, 1, 2, 0], "shares_H": [5, 5, 10, 0],
                      "cost_H": [2.5, 3.0, 5.0, 0.0], "caph_H": [24.0, 0.0, 24.0, 0.0]})
    S["day"] = (S["start"] // DAY).astype(np.int64)
    days = np.array([d0, d0 + 1, d0 + 2])
    p = R.perf(R.rule_frame(S, {"H": np.ones(4, bool)}), days)
    assert p["traded"] == 3 and p["win"] == pytest.approx(2 / 3)
    assert p["avg_win"] == pytest.approx(1.5) and p["avg_loss"] == pytest.approx(-0.5) and p["ratio"] == pytest.approx(3)
    assert p["per_share"] == pytest.approx(2.5 / 20)
    assert p["trades"] == pytest.approx(4 / 3) and p["shares"] == pytest.approx(20 / 3)
    assert p["cost"] == pytest.approx(10.5 / 3) and p["pnl_day"] == pytest.approx(2.5 / 3)
    assert p["cap"] == pytest.approx(48 / 72)            # $-hours / (24 x days)
    assert p["mdd"] == pytest.approx(0.5)
    assert p["d_n"] == 3 and p["d_mean"] == pytest.approx(2.5 / 3)
    q = R.perf(R.rule_frame(S, {"H": np.ones(4, bool)}), days, ok={"caph": False})
    assert np.isnan(q["cap"])
    assert R.max_drawdown([1, -2, 0.5, -1, 3]) == pytest.approx(2.5)


def test_state_labels():
    W = pd.DataFrame({"vr60": [1.2, 0.9, 1.1, np.nan], "rv30m": [0.1, 0.5, 0.9, 0.5], "dvol_rv": [0.1, -0.1, 0.0, 0]})
    lab = R.state_labels(W, [0.3, 0.6])
    assert list(lab) == ["趋势·低波·DVOL>RV", "震荡·中波·DVOL≤RV", "趋势·高波·DVOL≤RV", "缺数据"]
    assert list(R.state_labels(W, [0.3, 0.6], ("vr60", "rv30m"))) == ["趋势·低波", "震荡·中波", "趋势·高波", "缺数据"]
    assert len(R.state_order(R.STATE_COLS, [0.3, 0.6])) == 13
