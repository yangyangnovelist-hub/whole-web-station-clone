import json
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from scipy import stats

import factors as F

DAY = 86400
T_START = F.ts("2026-03-14")


def synth(days=10, seed=0):
    """A synthetic panel (minute rows, every column FACTORS uses), Deribit 5-minute frame and 1 s
    closes, all starting 2026-03-14 00:00 UTC."""
    rng = np.random.default_rng(seed)
    n_s = days * DAY
    close = 70000 * np.exp(np.cumsum(rng.normal(0, 2e-4, n_s)))
    idx = T_START + 60 * np.arange(days * 1440 + 1)
    n = len(idx)
    p = pd.DataFrame(index=pd.Index(idx, name="t"))
    for src in ("spot", "fut"):
        p[f"{src}_vol"] = rng.gamma(2.0, 5.0, n)
        p[f"{src}_buy"] = p[f"{src}_vol"] * rng.uniform(0.2, 0.8, n)
    oi = 80000 * np.exp(np.cumsum(rng.normal(0, 1e-3, n // 5 + 1)))
    p["sum_open_interest"] = np.repeat(oi, 5)[:n]
    for c in ("count_toptrader_long_short_ratio", "sum_toptrader_long_short_ratio", "sum_taker_long_short_vol_ratio"):
        p[c] = np.repeat(np.exp(rng.normal(0, 0.2, n // 5 + 1)), 5)[:n]
    p["funding_rate"] = np.repeat(rng.normal(5e-5, 3e-5, n // 480 + 1), 480)[:n]
    p["premium"] = rng.normal(-4e-4, 1e-4, n)
    p["dvol"] = np.where(np.arange(n) < 2000, np.nan, rng.uniform(30, 60, n))
    p["dvol_1h"] = rng.uniform(30, 60, n)
    g = T_START + 300 * np.arange(days * 288 + 1)
    d = pd.DataFrame(index=pd.Index(g, name="known_at"))
    for c in ("dealer_gamma_usd_1pct", "dealer_gamma_7d_usd_1pct", "cust_delta_flow_1h", "cust_delta_flow_24h",
              "pin_dist_1k_signed_pct"):
        d[c] = rng.normal(0, 1e7 if "gamma" in c else 100, len(g))
    d["pin_dist_1k_pct"] = d["pin_dist_1k_signed_pct"].abs() / 100
    d["pin_dist_5k_pct"] = rng.uniform(0, 3, len(g))
    d["hours_to_fri_expiry"] = rng.uniform(0, 168, len(g))
    return p, d, T_START, close


def same(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    return bool(np.all((a == b) | (np.isnan(a) & np.isnan(b))))


def perturb_after(p, d, sec0, close, T0, seed=1):
    """Replace everything that becomes known after T0: panel rows t > T0, Deribit rows known_at >
    T0, and 1 s klines opening at or after T0 (their close is known at open + 1 s > T0)."""
    rng = np.random.default_rng(seed)
    p2, d2, c2 = p.copy(), d.copy(), close.copy()
    m = p2.index.to_numpy() > T0
    for c in p2.columns:
        if pd.api.types.is_float_dtype(p2[c].dtype):
            p2.loc[m, c] = p2.loc[m, c].to_numpy() * rng.uniform(0.5, 1.5, m.sum()) + rng.normal(0, 1e-4, m.sum())
    md = d2.index.to_numpy() > T0
    for c in d2.columns:
        if pd.api.types.is_float_dtype(d2[c].dtype):
            d2.loc[md, c] = d2.loc[md, c].to_numpy() * rng.uniform(-2, 2, md.sum())
    k = T0 - sec0
    c2[k:] = c2[k:] * np.exp(np.cumsum(rng.normal(0, 1e-3, len(c2) - k)))
    return p2, d2, c2


def test_no_lookahead_synthetic():
    p, d, sec0, close = synth()
    T = np.arange(T_START + 7 * DAY, T_START + 10 * DAY - 4 * 3600, 300, dtype=np.int64)
    T0 = int(T[len(T) // 2])
    f1 = F.build_features(p, d, sec0, close, T)
    p2, d2, c2 = perturb_after(p, d, sec0, close, T0)
    f2 = F.build_features(p2, d2, sec0, c2, T)
    before = f1.index <= T0
    for c in F.FACTORS + F.AUX:
        assert same(f1.loc[before, c], f2.loc[before, c]), c
    changed = [c for c in F.FACTORS if not same(f1.loc[~before, c], f2.loc[~before, c])]
    calendar = {"tod_sin", "tod_cos", "weekend", "h_to_funding"}
    assert set(F.FACTORS) - set(changed) == calendar  # the perturbation reaches every data factor
    # the targets do look ahead (they are not features): the 5 m target just before T0 changes
    t1, t2 = F.build_targets(sec0, close, T), F.build_targets(sec0, c2, T)
    assert not same(t1.loc[T0 - 300:T0, "r_5m"], t2.loc[T0 - 300:T0, "r_5m"])


def test_no_lookahead_one_second_boundary():
    """The 1 s kline opening at T - 1 is used at T, the one opening at T is not."""
    p, d, sec0, close = synth(days=9)
    T = np.array([T_START + 8 * DAY], dtype=np.int64)
    base = F.build_features(p, d, sec0, close, T)
    c2 = close.copy()
    c2[T[0] - sec0] *= 1.01  # opens at T
    assert same(base[F.FACTORS], F.build_features(p, d, sec0, c2, T)[F.FACTORS])
    c3 = close.copy()
    c3[T[0] - 1 - sec0] *= 1.01  # opens at T - 1, closes at T
    f3 = F.build_features(p, d, sec0, c3, T)
    assert abs(f3["ret_1m"].iloc[0] - base["ret_1m"].iloc[0] - np.log(1.01)) < 1e-12


def test_metrics_row_used_from_create_time_plus_5_min():
    """With a panel built like factors_data (row stamped c appears from c + 5 min), oi_chg_5m at T
    is log(OI[c = T - 5 min] / OI[c = T - 10 min])."""
    p, d, sec0, close = synth(days=9)
    rng = np.random.default_rng(3)
    c = T_START + 300 * np.arange(9 * 288)
    oi = pd.Series(80000 + rng.normal(0, 100, len(c)), index=c)
    known = oi.copy()
    known.index = known.index + 300
    p["sum_open_interest"] = known.reindex(p.index, method="ffill").to_numpy()
    T = np.arange(T_START + 8 * DAY, T_START + 8 * DAY + 3600, 300, dtype=np.int64)
    f = F.build_features(p, d, sec0, close, T)
    want = np.log(oi.reindex(T - 300).to_numpy() / oi.reindex(T - 600).to_numpy())
    assert np.allclose(f["oi_chg_5m"].to_numpy(), want)


def test_targets_and_up():
    rng = np.random.default_rng(0)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 1e-3, 3 * DAY)))
    sec0 = T_START
    T = np.array([T_START + DAY, T_START + DAY + 300], dtype=np.int64)
    t = F.build_targets(sec0, close, T)
    k = T - sec0
    assert np.allclose(t["r_5m"], np.log(close[k + 299] / close[k - 1]))
    assert np.allclose(t["r_4h"], np.log(close[k + 4 * 3600 - 1] / close[k - 1]))
    rv0 = np.sqrt(np.sum(np.diff(np.log(close[k[0] - 1:k[0] + 3600])) ** 2) * 24 * 365)
    assert abs(t["rv_fwd_1h"].iloc[0] - rv0) < 1e-9
    assert list(F.up_of([0.0, -1e-9, 1e-9, np.nan])[:3]) == [1.0, 0.0, 1.0]


def test_second_stats_match_regime():
    import regime as rg
    rng = np.random.default_rng(5)
    n = 4000
    close = 100 * np.exp(np.cumsum(rng.normal(0, 1e-4, n)))
    s = pd.DataFrame({"close": close, "vol": 1.0, "buy": 0.5}, index=np.arange(T_START, T_START + n))
    f = rg.features(s)
    T = np.array([T_START + 3000, T_START + 3900], dtype=np.int64)
    st = F._second_stats(T_START, close, T)
    assert np.allclose(st["rv30"], f.loc[T, "rv30"].to_numpy())
    assert np.allclose(st["vr10"], f.loc[T, "vr10"].to_numpy())


def test_spearman_nw():
    rng = np.random.default_rng(0)
    x = rng.normal(size=2000)
    y = 0.2 * x + rng.normal(size=2000)
    rho, t, p, n = F.spearman_nw(x, y, lags=0)
    assert abs(rho - stats.spearmanr(x, y).statistic) < 1e-12
    assert n == 2000 and 6 < t < 12
    # positively autocorrelated products widen the NW standard error
    xa = np.repeat(rng.normal(size=400), 5)
    ya = np.repeat(rng.normal(size=400), 5) + 0.3 * xa
    _, t0, _, _ = F.spearman_nw(xa, ya, lags=0)
    _, t4, _, _ = F.spearman_nw(xa, ya, lags=4)
    assert abs(t4) < abs(t0)
    assert F.auto_lags(28000) == 13


def test_cluster_and_ols():
    x = np.array([1.0, 1.0, -1.0, -1.0, 2.0, 2.0])
    g = np.array([0, 0, 1, 1, 2, 2])
    mu, se, n, G = F.cluster_mean(x, g)
    s = np.array([2 * (1 - mu), 2 * (-1 - mu), 2 * (2 - mu)])
    assert G == 3 and abs(se - np.sqrt(1.5 * (s ** 2).sum()) / 6) < 1e-12
    rng = np.random.default_rng(1)
    X = np.column_stack([np.ones(500), rng.normal(size=500)])
    y = 1 + 2 * X[:, 1] + rng.normal(size=500)
    b, se, n = F.ols_cluster(y, X, np.arange(500))
    assert abs(b[1] - 2) < 0.2 and n == 500


def test_walk_forward_is_purged():
    """Every training row's target ended by the start of the block it predicts."""
    T = np.arange(F.ts("2026-03-24"), F.ts("2026-08-16"), 300, dtype=np.int64)
    X = T[:, None].astype(float)
    up = (np.arange(len(T)) % 2).astype(float)
    seen = []

    class Rec:
        def __init__(self, *a):
            pass

        def fit(self, X, y):
            self.mx = X[:, 0].max()
            return self

        def predict_proba1(self, Xt):
            seen.append((self.mx, Xt[:, 0].min()))
            return np.full(len(Xt), 0.5)

    for h in (5, 240):
        seen.clear()
        p, fits = F.walk_forward(X, up, T, h, "ridge", F.blocks(["A", "B"]), factory=lambda *a: Rec())
        assert seen and all(mx + h * 60 <= b0 for mx, b0 in seen)
        assert np.isnan(p[T < F.ts("2026-04-07")]).all() and np.isfinite(p[T >= F.ts("2026-04-07")]).all()
    bl = F.blocks(["A", "B", "C"])
    assert bl[0] == (F.ts("2026-03-24"), F.ts("2026-03-31"))
    assert (F.ts("2026-07-01"), F.ts("2026-07-08")) in bl and (F.ts("2026-08-16"), F.ts("2026-08-23")) in bl
    assert all(b1 - b0 <= 7 * DAY for b0, b1 in bl) and bl[-1][1] == F.ts("2026-10-01")


def test_mask_c_hides_lockbox_targets():
    T = np.arange(F.ts("2026-08-15"), F.ts("2026-08-17"), 300, dtype=np.int64)
    df = pd.DataFrame({f"r_{h}": 1.0 for h in ("5m", "15m", "1h", "4h", "24h")} | {"rv_fwd_1h": 1.0}, index=T)
    m = F.mask_c(df)
    c0 = F.ts("2026-08-16")
    for h, hm in F.HORIZONS + (("24h", 1440),):
        ok = m[f"r_{h}"].notna().to_numpy()
        assert (ok == (T + hm * 60 <= c0)).all()


def test_trades_and_fee():
    T = np.arange(0, 300 * 6, 300, dtype=np.int64)
    p = np.array([0.53, 0.52, 0.47, 0.5, 0.44, 0.6])
    up = np.array([1.0, 1.0, 1.0, 0.0, 0.0, 0.0])
    t = F.trades(p, up, T, 5, 0.025)
    assert list(t["T"]) == [0, 600, 1200, 1500]
    fee = 0.07 * 0.51 * 0.49
    assert np.allclose(t["pnl"], [1 - 0.51 - fee, -0.51 - fee, 1 - 0.51 - fee, -0.51 - fee])
    t15 = F.trades(np.full(6, 0.6), up, T, 15, 0.025)
    assert list(t15["T"]) == [0, 900]  # only the 15 m window opens


def test_kacho_trades_use_opening_ask():
    k = pd.DataFrame({"S": [0, 300, 600, 900], "up_won": [True, False, True, True],
                      "au": [0.52, 0.50, 0.55, np.nan], "ad": [0.49, 0.51, 0.46, 0.5],
                      "sau": [10, 10, 3, 10], "sad": [10, 10, 10, 10]})
    p = pd.Series([0.6, 0.4, 0.6, 0.6], index=[0, 300, 600, 900])
    t = F.kacho_trades(k, p, 0.025)
    assert len(t) == 4
    assert np.isclose(t["ask"].iloc[0], 0.52) and np.isclose(t["ask"].iloc[1], 0.51)
    assert np.isnan(t["ask"].iloc[2]) and np.isnan(t["ask"].iloc[3])  # 3 shares; no ask
    assert np.isclose(t["pnl"].iloc[0], 1 - 0.52 - 0.07 * 0.52 * 0.48)
    assert np.isclose(t["pnl"].iloc[1], 1 - 0.51 - 0.07 * 0.51 * 0.49)  # bought down, up lost


def test_ridge_lpm_learns_and_is_standardised():
    rng = np.random.default_rng(2)
    X = rng.normal(size=(5000, 3)) * [1, 100, 0.01]
    y = (X[:, 0] + rng.normal(size=5000) > 0).astype(float)
    X[::7, 2] = np.nan
    m = F.RidgeLPM(10.0).fit(X, y)
    p = m.predict_proba1(X)
    assert stats.spearmanr(p, X[:, 0]).statistic > 0.95
    assert abs(p.mean() - y.mean()) < 0.01
    assert np.isfinite(p).all()


def test_lockbox_runs_once(tmp_path):
    (tmp_path / "lockbox.json").write_text(json.dumps({"horizons": {"5m": {"passed": False}}}))
    r = F.lockbox(cache=tmp_path, log=lambda *a: None)
    assert r["horizons"]["5m"]["passed"] is False


def test_factor_list_matches_design():
    assert len(F.FACTORS) == len(set(F.FACTORS)) == 55
    assert [fam for fam, _ in F.FAMILIES] == ["价格", "主动买卖", "成交量", "持仓量", "多空比", "资金费率和基差",
                                             "波动区间", "期权 gamma", "日历"]


# ---------------------------------------------------------------- on the real data (skipped without it)

REAL = (F.CACHE / "panel_1m.parquet").exists() and (F.CACHE / "deribit_5m.parquet").exists() \
    and (F.CACHE / "spot_1s_close.parquet").exists()


@pytest.mark.skipif(not REAL, reason="real panel / Deribit / 1 s cache not present")
def test_no_lookahead_real_data():
    lo, hi = F.ts("2026-05-01"), F.ts("2026-05-11")
    panel = F.load_panel(F.CACHE / "panel_1m.parquet")
    panel = panel[(panel.index >= lo) & (panel.index <= hi)]
    der = F.load_deribit(F.CACHE / "deribit_5m.parquet")
    der = der[(der.index >= lo) & (der.index <= hi)]
    s = pd.read_parquet(F.CACHE / "spot_1s_close.parquet")
    s = s[(s.index >= lo - 2 * DAY) & (s.index < hi)]
    sec0, close = int(s.index[0]), s["close"].to_numpy()
    T = np.arange(F.ts("2026-05-09"), F.ts("2026-05-10 12:00"), 300, dtype=np.int64)
    T0 = F.ts("2026-05-09 17:35")
    f1 = F.build_features(panel, der, sec0, close, T)
    p2, d2, c2 = perturb_after(panel, der, sec0, close, T0)
    f2 = F.build_features(p2, d2, sec0, c2, T)
    b = f1.index <= T0
    for c in F.FACTORS + F.AUX:
        assert same(f1.loc[b, c], f2.loc[b, c]), c
    assert f1.loc[b, F.FACTORS].notna().all().all()


@pytest.mark.skipif(not REAL, reason="real panel not present")
def test_real_panel_metrics_lag_and_raw_file():
    """Panel rows only carry metrics rows stamped <= t - 5 min, and oi_chg_5m at T matches the raw
    Binance metrics file: log(OI[c = T - 5 min] / OI[c = T - 10 min])."""
    panel = pd.read_parquet(F.CACHE / "panel_1m.parquet", columns=["sum_open_interest", "metrics_time"])
    ok = panel["metrics_time"].notna()
    lag = (panel.index[ok].to_series().reset_index(drop=True)
           - panel.loc[ok, "metrics_time"].reset_index(drop=True)).dt.total_seconds()
    assert lag.min() >= 300
    raw = F.CACHE / "raw" / "metrics" / "BTCUSDT-metrics-2026-05-02.zip"
    feats = F.CACHE / "features_5m.parquet"
    if not raw.exists() or not feats.exists():
        pytest.skip("raw metrics file or feature cache missing")
    with zipfile.ZipFile(raw) as z:
        m = pd.read_csv(z.open(z.namelist()[0]))
    m["c"] = F._secs(pd.to_datetime(m["create_time"], utc=True))
    oi = m.set_index("c")["sum_open_interest"].sort_index()
    f = pd.read_parquet(feats, columns=["oi_chg_5m"])
    T = np.arange(F.ts("2026-05-02 01:00"), F.ts("2026-05-02 23:00"), 300, dtype=np.int64)
    want = np.log(oi.reindex(T - 300).to_numpy() / oi.reindex(T - 600).to_numpy())
    assert np.allclose(f.loc[T, "oi_chg_5m"].to_numpy(), want, equal_nan=False)
