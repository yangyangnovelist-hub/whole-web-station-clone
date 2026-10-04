"""Tests of roundtrip_hf.py on synthetic Hugging Face archives (the cross.read_day / read_binance
layout, as test_cross.py / test_jump2s_hf.py build them) and on hand-made panels."""
import io
import json
import tarfile

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import binary as bo
import cross
import roundtrip as rt
import roundtrip_hf as hf

DAY = "2026-06-10"
D0 = int(pd.Timestamp(DAY, tz="UTC").timestamp())          # seconds
S1 = D0 + 3600                                               # m1 starts 01:00 (jump at S1 + 100.2 s)
S2 = S1 + 300                                                # m2 starts 01:05 (no jump)
JUMP_MS = (S1 + 100) * 1000 + 200                            # trade time of the +10 bp print
LAG = 50                                                     # Binance receipt delay, ms
F1_ID = "f1_binance_momentum:w1-bp4-all-first-h10"
FEE = lambda p: 0.07 * p * (1 - p)


# ------------------------------------------------------------------ synthetic archive
def parquet_bytes(df):
    buf = io.BytesIO()
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), buf)
    return buf.getvalue()


def up_book(rel_ms, jump=True):
    """Up bid / ask at each snapshot time relative to the start (ms). With the jump (market m1):
    ask 0.51 -> 0.55 at 101.4 s -> 0.60 at 101.6 s; bid 0.49 -> 0.53 at 101.4 s -> 0.58 at
    111.3 s -> 0.45 at 111.6 s."""
    ub = np.full(len(rel_ms), 0.49)
    ua = np.full(len(rel_ms), 0.51)
    if jump:
        ua[rel_ms >= 101_400] = 0.55
        ua[rel_ms >= 101_600] = 0.60
        ub[rel_ms >= 101_400] = 0.53
        ub[rel_ms >= 111_300] = 0.58
        ub[rel_ms >= 111_600] = 0.45
    return ub, ua


def features(mid, start_s, jump=True, t_from=-5_000, t_to=300_000, ladders=True, mutate=None):
    rel = np.arange(t_from, t_to, 100)
    ub, ua = up_book(rel, jump)
    if mutate is not None:
        ub, ua = mutate(rel, ub, ua)
    n = len(rel)
    wiggle = 100.0 + (rel // 1000) % 2                       # the book changes every second
    f = pd.DataFrame({"timestamp_ms": start_s * 1000 + rel, "market_id": mid, "lifecycle_state": "active",
                      "up_best_bid": ub, "up_best_ask": ua, "down_best_bid": 1 - ua, "down_best_ask": 1 - ub,
                      "up_ask_sizes": [[w, 3.0] for w in wiggle], "down_ask_sizes": [[40.0]] * n,
                      "up_bid_sizes": [[40.0, 9.0]] * n, "down_bid_sizes": [[w] for w in wiggle],
                      "observed_halt_flag": False})
    if ladders:
        f["up_side_bids"] = [[b, b - 0.01, b - 0.07] for b in ub]
        f["up_bid_sizes"] = [[40.0, 9.0, 1000.0]] * n
        f["down_side_bids"] = [[1 - a] for a in ua]
    return f


def binance(t_from, t_to, jump=True, gap=None, seed=1):
    tt = np.arange(t_from * 1000, t_to * 1000, 200)
    rng = np.random.default_rng(seed)
    price = 80_000 * np.exp(np.cumsum(rng.normal(0, 2e-6, len(tt))))
    if jump:
        price[tt >= JUMP_MS] *= 1.001
    b = pd.DataFrame({"trade_ts_ms": tt, "recv_ts_ms": tt + LAG, "exchange": "binance", "price": price})
    if gap is not None:
        b = b[(b["recv_ts_ms"] < gap[0]) | (b["recv_ts_ms"] >= gap[1])]
    return b


def market_rows(rows):
    return pd.DataFrame([{"timestamp_ms": (s + 300) * 1000, "market_id": mid, "slug": f"btc-updown-{h}m-{s}",
                          "session_start_ts": s * 1000, "session_end_ts": (s + 60 * h) * 1000,
                          "chainlink_open_price": 80_000.0, "up_won": won, "lifecycle_state": "resolved"}
                         for mid, s, h, won in rows])


def write_archive(path, members):
    with tarfile.open(path, "w:gz") as tar:
        for name, df in members:
            raw = parquet_bytes(df)
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))
    return path


def make_archive(tmp_path, mutate=None, bin_kw=None, won=(1.0, 0.0)):
    f = pd.concat([features("m1", S1, mutate=mutate), features("m2", S2, jump=False),
                   features("m15", S1, jump=False, t_to=900_000, ladders=False)], ignore_index=True)
    mk = market_rows([("m1", S1, 5, won[0]), ("m2", S2, 5, won[1]), ("m15", S1, 15, 1.0),
                      ("mprev", D0 - 300, 5, 1.0)])
    b = binance(S1 - 1000, S2 + 320, **(bin_kw or {}))
    path = tmp_path / f"market_parquet_{DAY}.tar.gz"
    return write_archive(path, [(f"dataset=polymarket_features_100ms/date={DAY}/part-1.parquet", f),
                                (f"dataset=polymarket_market_100ms/date={DAY}/part-2.parquet", mk),
                                (f"dataset=trades/date={DAY}/part-3.parquet", b)])


def grid_entry(vid, A=0.01, B=0.02):
    import roundtrip_run as rr
    fams, _ = rr.all_variants(None)
    v = {x.id: x for vs in fams.values() for x in vs}[vid]
    return {"id": v.id, "family": v.family, "name": v.name, "params": json.loads(json.dumps(v.all_params())),
            "control": v.control, "A": {"mean": A}, "B": {"mean": B}}


def frozen_file(tmp_path, survivors, describe=None):
    p = tmp_path / "frozen.json"
    p.write_text(json.dumps({"frozen_at": "2026-10-04T00:00:00+00:00", "N": 8977, "survivors": survivors,
                             "describe": describe or {}}), encoding="utf-8")
    return p


def run_on(tmp_path, arch, survivors, describe=None, **kw):
    out = tmp_path / "out" / "roundtrip-hf.md"
    logs = []
    stats, info = hf.run([(arch.name, arch)], out, frozen_file(tmp_path, survivors, describe), log=logs.append, **kw)
    return stats, info, out.read_text(encoding="utf-8"), logs


# ------------------------------------------------------------------ the day panel
def day_panel(tmp_path, mutate=None, bin_kw=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    arch = make_archive(tmp_path, mutate=mutate, bin_kw=bin_kw)
    feat, mk, rs = cross.read_day(arch)
    mt = cross.market_table(hf.shrink_markets(mk), hf.final_resolution(rs))
    mk5 = hf.markets_5m(mt, DAY)
    feat = feat[feat["market_id"].astype(str).isin(set(mk5["market_id"]))]
    return hf.build_day(feat, mk5, cross.read_binance(arch))


def test_markets_5m_keeps_the_days_5m_markets(tmp_path):
    arch = make_archive(tmp_path)
    feat, mk, rs = cross.read_day(arch)
    mt = cross.market_table(hf.shrink_markets(mk), hf.final_resolution(rs))
    assert set(hf.markets_5m(mt)["market_id"]) == {"m1", "m2", "mprev"}
    m = hf.markets_5m(mt, DAY)
    assert list(m["market_id"]) == ["m1", "m2"] and list(m["start"]) == [S1 * 1000, S2 * 1000]


def test_panel_columns_are_the_book_at_the_decision(tmp_path):
    p, fill = day_panel(tmp_path)
    assert list(p.cid) == ["m1", "m2"] and list(p.start) == [S1, S2]
    # column c = the book in effect at start + c + 1 s: column 100 (decision 101) is before the 101.4 s change
    assert p.au[0, 100] == pytest.approx(0.51) and p.au[0, 101] == pytest.approx(0.60)
    assert p.su[0, 0] == 40.0 and p.sau[0, 0] in (100.0, 101.0) and p.sd[0, 0] == p.sau[0, 0]
    assert (p.age >= 0).all() and p.age[:, :299].max() == 0             # changes every second
    # fill row r = the snapshot in effect at start + r - 0.5 s
    assert fill["au"][0, 102] == pytest.approx(0.55) and fill["au"][0, 101] == pytest.approx(0.51)
    assert fill["bu"][0, 112] == pytest.approx(0.58) and fill["bu"][0, 113] == pytest.approx(0.45)
    assert fill["ok"][:, 1:286].all()
    # Binance: x[OFF + s] = price known at start + s + 1 minus the price known at the start
    assert p.x[0, rt.OFF - 1] == pytest.approx(0.0, abs=1e-12)
    assert abs(p.x[0, rt.OFF + 99]) < 2e-4 and p.x[0, rt.OFF + 100] - p.x[0, rt.OFF + 99] == pytest.approx(
        np.log(1.001), abs=2e-5)
    assert np.isfinite(p.sig[0, rt.OFF:]).all() and np.isfinite(p.fair[0, 1:]).all()


def test_depth_from_the_bid_ladder(tmp_path):
    arch = make_archive(tmp_path)
    dep = hf.read_bid_depth(arch, {"m1"})
    assert set(dep["market_id"]) == {"m1"}
    r = dep.sort_values("timestamp_ms").iloc[0]                         # Up bids 0.49 x 40, 0.48 x 9, 0.42 x 1000
    assert r["du"] == pytest.approx(0.49 * 40 + 0.48 * 9)
    assert r["dd"] == pytest.approx(0.49 * 101.0)                      # Down bids 0.49 x 101 at -5 s
    col = pa.chunked_array([pa.array([[0.5, 0.45, 0.44], None, [], [0.3]], pa.list_(pa.float64()))])
    sz = pa.chunked_array([pa.array([[10.0, 20.0, 30.0], None, [], [1.0, 2.0]], pa.list_(pa.float64()))])
    d = hf.bid_depth(col, sz)
    assert d[0] == pytest.approx(0.5 * 10 + 0.45 * 20) and d[1] == 0.0 and d[2] == 0.0 and np.isnan(d[3])


def test_binance_grid_latest_trade_and_staleness():
    # prints received out of trade order; a gap of 3 s
    b = pd.DataFrame({"trade_ts_ms": [10_000, 10_900, 10_500, 11_900, 16_000],
                      "recv_ts_ms": [10_050, 10_950, 11_000, 11_950, 16_050],
                      "price": [100.0, 101.0, 99.0, 102.0, 103.0]})
    g = hf.binance_grid(b, 10, 17)
    lp = dict(zip(range(10, 18), g["lp"]))
    assert np.isnan(lp[10])                                            # nothing received by 10.000
    assert lp[11] == pytest.approx(np.log(101.0))                      # latest TRADE among received (10.9 s), not 99
    assert lp[12] == pytest.approx(np.log(102.0)) and lp[13] == pytest.approx(np.log(102.0))
    assert np.isnan(lp[14]) and np.isnan(lp[15])                       # > 2 s since the latest trade
    lpr = dict(zip(range(10, 18), g["lp_ref"]))
    assert lpr[14] == pytest.approx(np.log(102.0)) and lpr[16] == pytest.approx(np.log(102.0))
    assert np.isnan(lp[16]) and lp[17] == pytest.approx(np.log(103.0))


def test_stale_book_is_not_used(tmp_path):
    arch_feat = features("m1", S1)
    rel = arch_feat["timestamp_ms"].to_numpy() - S1 * 1000
    keep = (rel < 150_000) | (rel >= 170_000)                          # recorder writes nothing 150..170 s
    f = arch_feat[keep].copy()
    for c in ("up_ask_sizes", "down_ask_sizes", "up_bid_sizes", "down_bid_sizes"):   # as cross.read_day
        f[c.replace("_sizes", "_size")] = [v[0] for v in f[c]]
    mk5 = pd.DataFrame({"market_id": ["m1"], "slug": ["x"], "start": [S1 * 1000], "up_won": [1.0]})
    p, fill = hf.build_day(f, mk5, binance(S1 - 1000, S1 + 320))
    # the last snapshot is at 149.9 s, the last change at 149.0 s: usable while the change age at the
    # column's moment (start + c + 1 s) is < 6 s
    assert p.age[0, 148] == 0 and p.age[0, 153] == 5 and p.age[0, 154] == -1 and np.isnan(p.au[0, 160])
    assert p.age[0, 169] == 0
    assert fill["ok"][0, 150] and not fill["ok"][0, 152]                # fill snapshot at most 1 s old


# ------------------------------------------------------------------ the engine on hand-made arrays
def flat_panel(M=1, ub=0.49, ua=0.51):
    a = {k: np.full((M, rt.T), v, np.float32) for k, v in
         (("bu", ub), ("au", ua), ("bd", 1 - ua), ("ad", 1 - ub), ("su", 50.0), ("sd", 50.0), ("sau", 50.0),
          ("sad", 50.0), ("du", 10.0), ("dd", 10.0))}
    p = rt.Panel(cid=np.array([f"m{i}" for i in range(M)]), slug=np.array(["s"] * M), start=np.arange(M) * 300 + D0,
                 up_won=np.full(M, -1, np.int8), up_inferred=np.full(M, -1, np.int8), n_ticks=np.zeros(M, np.int16),
                 split=np.zeros(M, np.int8), day=np.zeros(M, np.int32), ref=np.ones(M), age=np.zeros((M, rt.T), np.int8),
                 fair=np.full((M, rt.T), 0.5, np.float32), x=np.zeros((M, rt.OFF + rt.T), np.float32),
                 sig=np.full((M, rt.OFF + rt.T), 1e-4, np.float32), vol=np.zeros((M, rt.OFF + rt.T), np.float32),
                 buy=np.zeros((M, rt.OFF + rt.T), np.float32), off=np.int64(rt.OFF), **a)
    fill = {k: a[k].copy() for k in hf.FILL_MAP}
    fill["ok"] = np.ones((M, rt.T), bool)
    return p, fill


def E(d, side=1, target=None):
    return rt.Entries(np.zeros(len(d), np.int64), np.asarray(d, np.int64), np.full(len(d), side, np.int8),
                      None if target is None else np.asarray(target, float))


def test_engine_hold_fills_half_a_second_later():
    p, fill = flat_panel()
    fill["au"][0, 51] = 0.40                                            # snapshot at 50.5 s
    fill["bu"][0, 61] = 0.70                                            # snapshot at 60.5 s
    tr = hf.HFEngine(p, fill).simulate(E([50]), rt.Exit.hold(10))
    assert tr["entry_row"].tolist() == [51] and tr["exit_row"].tolist() == [61]
    assert tr["entry_px"][0] == pytest.approx(0.40) and tr["exit_px"][0] == pytest.approx(0.70)
    agg = hf.aggregate(tr, 1)
    assert agg["book"][0] == pytest.approx(0.70 - FEE(0.70) - 0.40 - FEE(0.40), abs=1e-6) and agg["hold"][0] == 10


def test_engine_forced_fallback_and_one_position():
    p, fill = flat_panel()
    tr = hf.HFEngine(p, fill).simulate(E([270, 275, 280]), rt.Exit.hold(60))
    assert tr["second"].tolist() == [270] and tr["exit_row"].tolist() == [285] and tr["reason"][0] == rt.FORCED
    tr = hf.HFEngine(p, fill).simulate(E([10, 15, 20, 21]), rt.Exit.hold(10))
    assert tr["second"].tolist() == [10, 21]                            # exit fill at 20.5 s: next decision 21
    fill2 = {k: v.copy() for k, v in fill.items()}
    fill2["bu"][0, 30:] = 0.99                                          # no sellable bid from 29.5 s on
    tr = hf.HFEngine(p, fill2).simulate(E([20]), rt.Exit.hold(10))
    assert tr["settled"][0] and tr["reason"][0] == rt.FALLBACK
    fill3 = {k: v.copy() for k, v in fill.items()}
    fill3["ok"][0, 31:35] = False                                       # the 30.5 .. 33.5 s snapshots fail
    tr = hf.HFEngine(p, fill3).simulate(E([20]), rt.Exit.hold(10))
    assert tr["exit_row"].tolist() == [35]
    tr = hf.HFEngine(p, fill).simulate(E([20, 40]), rt.Exit.settle())
    assert tr["second"].tolist() == [20] and tr["settled"][0]


def test_engine_converge_and_stop_read_the_decision_book():
    p, fill = flat_panel()
    p.a["bu"][0, 31:] = 0.60                                            # column 31 = book at 32 s
    p.a["au"][0, 31:] = 0.62
    p.a["fair"][0, :] = 0.55
    tr = hf.HFEngine(p, fill).simulate(E([20]), rt.Exit.converge(60))
    assert tr["reason"][0] == rt.CONVERGE and tr["exit_row"][0] == 33   # decision 32, fill at 32.5 s
    # checked from d + 1: a book already at fair converges at decision d + 1
    p2, fill2 = flat_panel()
    p2.a["fair"][0, :] = 0.40
    tr = hf.HFEngine(p2, fill2).simulate(E([20]), rt.Exit.converge(60))
    assert tr["exit_row"][0] == 22
    # stop: reference = mid of the entry fill snapshot (0.50); the decision book drops 3c at column 40
    p3, fill3 = flat_panel()
    p3.a["bu"][0, 40:] = 0.46
    p3.a["au"][0, 40:] = 0.48
    tr = hf.HFEngine(p3, fill3).simulate(E([20]), rt.Exit.hold(60, stop=0.03))
    assert tr["reason"][0] == rt.STOP and tr["exit_row"][0] == 42      # decision 41 reads column 40
    # entry target (family 3): converge when the side mid reaches the target
    p4, fill4 = flat_panel()
    p4.a["bu"][0, 25:] = 0.54
    p4.a["au"][0, 25:] = 0.56
    tr = hf.HFEngine(p4, fill4).simulate(E([20], target=[0.55]), rt.Exit.converge(30, target="entry"))
    assert tr["exit_row"][0] == 27


def test_down_side_uses_the_down_book():
    p, fill = flat_panel()
    fill["ad"][0, 51] = 0.30
    fill["bd"][0, 56] = 0.35
    tr = hf.HFEngine(p, fill).simulate(E([50], side=-1), rt.Exit.hold(5))
    assert tr["side"][0] == -1 and tr["entry_px"][0] == pytest.approx(0.30) and tr["exit_px"][0] == pytest.approx(0.35)


# ------------------------------------------------------------------ end to end
def test_end_to_end_frozen_variant(tmp_path):
    arch = make_archive(tmp_path)
    stats, info, text, logs = run_on(tmp_path, arch, [grid_entry(F1_ID)])
    r = stats.set_index("id").loc[F1_ID]
    # decision 101 (the jump print is known at 101 s), entry at the 101.5 s ask 0.55, exit decided 111 at
    # the 111.5 s bid 0.58
    assert r["n"] == 1 and r["mk"] == 1
    assert r["mean"] == pytest.approx(0.58 - FEE(0.58) - 0.55 - FEE(0.55), abs=1e-6)
    assert r["hold"] == pytest.approx(10.0) and r["fb"] == 0
    assert info["markets"] == 2 and info["markets_no_outcome"] == 0 and not info["failed"]
    assert "冻结名单 k′ = 1" in text and "通过 0 个" in text and F1_ID in text   # 1 market < 30: no pass
    assert any("2 5m markets" in s for s in logs)


def test_no_lookahead(tmp_path):
    def later(rel, ub, ua):                                             # change everything after the 101.5 s fill
        ua = np.where(rel > 101_500, 0.70, ua)
        ub = np.where(rel > 101_500, 0.10, ub)
        return ub, ua
    base, _ = day_panel(tmp_path / "a")
    alt, fill = day_panel(tmp_path / "b", mutate=later)
    import roundtrip_run as rr
    fams, _ = rr.all_variants(None)
    v = {x.id: x for vs in fams.values() for x in vs}[F1_ID]
    e0, e1 = v.signal(base), v.signal(alt)
    assert e0.second.tolist() == e1.second.tolist() == [101] and e0.side.tolist() == [1]
    tr = hf.HFEngine(alt, fill).simulate(e1, rt.Exit.settle())
    assert tr["entry_px"][0] == pytest.approx(0.55)
    # Binance prints received after the decision moment do not move the signal
    late = day_panel(tmp_path / "c", bin_kw={"gap": ((S1 + 101) * 1000 + 1, (S1 + 140) * 1000)})[0]
    assert v.signal(late).second.tolist() == [101]
    # a feed gap: prints received 99..103 s missing -> the price at 100..103 s is stale, no jump at 101
    gap = day_panel(tmp_path / "d", bin_kw={"gap": ((S1 + 98) * 1000, (S1 + 103) * 1000)})[0]
    assert np.isnan(gap.x[0, rt.OFF + 100]) and 101 not in v.signal(gap).second.tolist()


def preds_file(tmp_path):
    preds = pd.DataFrame({"T": [S1, S2], "ridge_5m": [0.60, 0.40], "hgb_5m": [0.5, 0.5], "ridge_15m": [0.5, 0.5],
                          "hgb_15m": [0.5, 0.5]})
    pp = tmp_path / "preds.csv"
    preds.to_csv(pp, index=False)
    return pp


F6_ID = "f6_factor_model:ridge_5m-c4-d10-all-settle"


def test_outcomes_and_unknown_markets(tmp_path):
    arch = make_archive(tmp_path, won=(0.0, np.nan))
    stats, info, text, _ = run_on(tmp_path, arch, [grid_entry(F1_ID)],
                                  {"f6_factor_model": [grid_entry(F6_ID)],
                                   "f7_time_price_bucket": [grid_entry("f7_time_price_bucket:t120-fav40-60-settle")]},
                                  preds_path=preds_file(tmp_path))
    assert info["markets_no_outcome"] == 1                              # m2 never gets an outcome
    s = stats.set_index("id")
    assert s.loc[F1_ID, "mean"] == pytest.approx(0.58 - FEE(0.58) - 0.55 - FEE(0.55), abs=1e-6)   # sold: outcome irrelevant
    f6 = s.loc[F6_ID]                                                   # m1 Up (lost); m2 Down left out
    assert f6["n"] == 1 and f6["mk"] == 1 and f6["mean"] == pytest.approx(-0.51 - FEE(0.51), abs=1e-6)
    f7 = s.loc["f7_time_price_bucket:t120-fav40-60-settle"]             # m1 Up mid 0.525 at 180 s
    assert f7["n"] == 1 and f7["mean"] == pytest.approx(-0.60 - FEE(0.60), abs=1e-6)


def test_empty_frozen_list_is_descriptive(tmp_path):
    arch = make_archive(tmp_path)
    describe = {"f1_binance_momentum": [grid_entry(F1_ID)],
                "f5_book_imbalance": [grid_entry("f5_book_imbalance:depth_sh-i50-h10"),
                                      grid_entry("f5_book_imbalance:size-i50-h10")],
                "f6_factor_model": [grid_entry("f6_factor_model:ridge_5m-c4-d10-all-settle")]}
    stats, info, text, logs = run_on(tmp_path, arch, [], describe)
    assert "冻结名单为空，没有可判定的变体" in text and "只作描述，不参与判定" in text
    assert "冻结名单 k′" not in text and "没有预测，未评估" in text and info["depth"]
    assert stats.set_index("id").loc[F1_ID, "n"] == 1


def test_family6_with_predictions(tmp_path):
    arch = make_archive(tmp_path)
    stats, info, text, _ = run_on(tmp_path, arch, [], {"f6_factor_model": [grid_entry(F6_ID)]},
                                  preds_path=preds_file(tmp_path))
    r = stats.set_index("id").loc[F6_ID]
    # m1 Up at the 10.5 s ask 0.51 (won), m2 Down at 1 - 0.49 = 0.51 (won)
    assert r["n"] == 2 and r["mean"] == pytest.approx(1 - 0.51 - FEE(0.51), abs=1e-6)


def test_param_mismatch_is_refused(tmp_path):
    e = grid_entry(F1_ID)
    e["params"]["thr"] = 5.0
    with pytest.raises(ValueError):
        hf.resolve_variants([e])
    with pytest.raises(KeyError):
        hf.resolve_variants([{**grid_entry(F1_ID), "id": "f1_binance_momentum:nope"}])


def test_finalize_statistics():
    class V:
        def __init__(self, i):
            self.id, self.family, self.name, self.control = f"f:v{i}", "f", f"v{i}", False
    rows = pd.DataFrame({"vid": [0, 0, 0, 1], "market": ["a", "b", "c", "a"], "n": [2.0, 1.0, 1.0, 1.0],
                         "book": [0.10, -0.02, 0.0, 0.0], "cost": [0.0, 0.0, -0.6, -0.6], "n_up": [0, 0, 1, 1],
                         "n_dn": [0, 0, 0, 0], "hold": [20.0, 5.0, 100.0, 100.0], "fb": [0, 0, 1, 0],
                         "forced": [0, 0, 0, 0], "stop": [0, 0, 0, 0], "conv": [0, 0, 0, 0]})
    starts = {"a": D0, "b": D0 + 86400, "c": int(pd.Timestamp("2026-08-08", tz="UTC").timestamp())}
    st, fin = hf.finalize(rows, [(0, V(0)), (1, V(1))], {"a": 1.0, "b": 0.0, "c": 1.0}, starts)
    r = st.iloc[0]
    S = np.array([0.10, -0.02, 0.4])
    c = np.array([2.0, 1.0, 1.0])
    mean = S.sum() / c.sum()
    rr = S - mean * c
    se = np.sqrt(rr @ rr * 3 / 2) / c.sum()
    assert r["mean"] == pytest.approx(mean) and r["se"] == pytest.approx(se) and r["mk"] == 3
    assert r["fb"] == pytest.approx(0.25) and r["hold"] == pytest.approx(125 / 4)
    assert r["mean_late"] == pytest.approx(0.4) and r["mean_early"] == pytest.approx(0.08 / 3)
    k, thr, ok = hf.verdict(st, ["f:v0", "f:v1"])
    assert k == 2 and thr == pytest.approx(0.025) and not ok.any()     # < 30 markets


def test_cli_downloads_reads_and_deletes(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    src = make_archive(tmp_path / "src")
    name = f"market_parquet_{DAY}.tar.gz"
    fetched = []

    def fetch(n, dest=None):
        fetched.append(n)
        if n == "MANIFEST.txt":
            return f"# file sha256 bytes\n{name}  abc  {src.stat().st_size}\n".encode()
        dest.write_bytes(src.read_bytes())
        return dest

    monkeypatch.setattr(cross, "fetch", fetch)
    wd = tmp_path / "wd"
    out = tmp_path / "real" / "roundtrip-hf.md"
    fr = frozen_file(tmp_path, [grid_entry(F1_ID)])
    assert hf.main(["--workdir", str(wd), "--days", "1", "--frozen", str(fr), "--out", str(out)]) == 0
    assert fetched == ["MANIFEST.txt", name] and not (wd / name).exists()
    assert "通过 0 个" in out.read_text(encoding="utf-8")


def test_hf_engine_matches_the_kacho_engine_on_kacho_like_rows():
    """With fill row r = the kacho row stamped r (exact rows only) and the signal columns = the kacho
    columns, single hold / settle trades are the kacho Engine's (only converge / stop timing and the
    re-entry gap differ by design)."""
    rng = np.random.default_rng(7)
    M = 40
    p, _ = flat_panel(M)
    up_b = np.round(rng.uniform(0.02, 0.95, (M, rt.T)), 2).astype(np.float32)
    up_a = (up_b + np.round(rng.uniform(0.01, 0.04, (M, rt.T)), 2)).astype(np.float32)
    p.a.update(bu=up_b, au=up_a, bd=(1 - up_a).astype(np.float32), ad=(1 - up_b).astype(np.float32),
               su=rng.choice([3.0, 50.0], (M, rt.T)).astype(np.float32), sau=rng.choice([3.0, 50.0], (M, rt.T)).astype(np.float32))
    p.a["sd"], p.a["sad"] = p.a["sau"].copy(), p.a["su"].copy()
    p.a["age"] = rng.choice([0, 0, 0, 1, 2], (M, rt.T)).astype(np.int8)
    p.a["up_won"] = rng.integers(0, 2, M).astype(np.int8)
    fill = {k: p.a[k].copy() for k in hf.FILL_MAP}
    fill["ok"] = p.a["age"] == 0
    kacho = rt.Engine(p)
    ours = hf.HFEngine(p, fill)
    ent = rt.Entries(np.arange(M, dtype=np.int64), rng.integers(1, 283, M).astype(np.int64),
                     rng.choice([-1, 1], M).astype(np.int8))
    for ex in (rt.Exit.hold(5), rt.Exit.hold(30), rt.Exit.hold(200), rt.Exit.settle()):
        a, b = kacho.simulate(ent, ex), ours.simulate(ent, ex)
        assert a["market"].tolist() == b["market"].tolist() and a["exit_row"].tolist() == b["exit_row"].tolist()
        np.testing.assert_allclose(a["entry_px"], b["entry_px"])
        agg = hf.aggregate(b, M)
        won = np.where(b["side"] > 0, p.up_won[b["market"]], 1 - p.up_won[b["market"]])
        pnl = agg["book"][b["market"]] + agg["cost"][b["market"]] + np.where(b["settled"], won, 0)
        np.testing.assert_allclose(pnl, a["pnl"], atol=1e-9)
