"""BTC's own trend from derivatives positioning, taker flow, funding and dealer gamma: the study
written down in FACTORS.md (market data only, no keys, no orders).

Inputs (all built earlier, all stamped by the moment they became known):
- panel_1m.parquet (factors_data.py): Binance spot/perp 1 m klines, premium index, 5 m metrics
  (a row stamped c is in the panel from c + 5 min on, i.e. FACTORS.md's "timestamp <= decision
  - 5 min"), funding, DVOL. Row T holds only what was known at T.
- deribit_5m.parquet (deribit_flow.py): dealer gamma, customer delta flow, pin distances on the
  5-minute grid; a value at T uses only option trades stamped < T.
- Binance spot BTCUSDT 1 s klines (regime.load_klines; cached here as spot_1s_close.parquet).

Decision times, targets and periods (FACTORS.md):
- Decision times T: every 5-minute UTC boundary. Price at T = the close of the last 1 s kline
  before T (the kline that opens at T - 1 s, forward-filled).
- Targets: r_h = log P(T + h) - log P(T), h = 5 m, 15 m, 1 h, 4 h; "up" = r_h >= 0 (Polymarket's
  up/down markets resolve Up on >=; Binance spot is a proxy for their Chainlink price).
- A = 3/24-6/30, B = 7/1-8/15, C = 8/16-9/30 (by the date of T). Main statistics use the
  non-overlapping grid of each horizon (T a multiple of h); overlapping 5-minute-grid results use
  Newey-West with h/5 - 1 lags. In the A + B stage every target that ends after 8/16 00:00 is
  dropped, so nothing in C is touched before the freeze.

Factors (55; exact definitions in `build_features`; every one uses only data known at T):
 1 price      ret_1m, ret_5m, ret_15m, ret_60m, ret_240m, ret_24h (1 s closes)
 2 taker      {spot,fut}_tbr_{1,5,15,60}m = taker-buy share - 0.5; {spot,fut}_ntv_{1,5,15,60}m =
              net taker volume (buy - sell, BTC) over the same windows ("累计净主动成交量")
 3 volume     {spot,fut}_lvr_{5m,1h} = log(mean volume per minute over 5 m / 1 h over its 24 h mean)
 4 OI         oi_chg_{5,15,60,240}m (log change of sum_open_interest); oi_x_price_1h = sign(ret_60m)
              x oi_chg_60m; oi_z7d = (OI - 7 d mean) / 7 d std; liq_proxy = max(0, -z(oi_chg_15m)) x
              z(ret_15m), both z against their own trailing 7 days on the 5-minute grid
 5 long/short top_acct_ls, top_pos_ls (log top-trader account / position L/S) and their 1 h
              changes; taker_ls_5m = log(taker buy/sell volume ratio of the latest 5 m metrics row)
 6 funding    funding (last settled rate); premium, premium_1h_mean, premium_chg_1h (vs 1 h ago)
 7 vol regime rv30 (annualised std of 1 s log returns over the previous 1,800 s), dvol (Deribit 1 m
              DVOL; where it is missing - before 2026-04-01 07:16 and in its gaps - the hourly DVOL
              known at the end of its hour, an extension), dvol_minus_rv30, vr10 (var of 10 s
              returns / (10 var of 1 s returns), previous 1,800 s; as regime.features)
 8 gamma      dealer_gamma (Deribit 7-day dealer net gamma at the current price, $M per 1 %),
              cust_delta_1h, cust_delta_24h (customer net delta flow, BTC), pin_1k / pin_5k =
              distance to the nearest 1,000 / 5,000 strike (% of S) x hours to the next Friday 08:00
 9 calendar   tod_sin, tod_cos, weekend, h_to_funding (hours to the next 00/08/16 UTC settlement)

Tests:
- Univariate: Spearman IC per factor x horizon, A and B separately, Newey-West t (non-overlapping
  grid: floor(4 (n/100)^(2/9)) lags; overlapping 5 m grid: h/5 - 1 lags); Bonferroni over 55 x 4.
- Multi-factor: (a) ridge = linear-probability ridge on the up indicator with features standardised
  on the training rows (clipped at +-5 sd, NaN -> 0), alpha picked on A by the same walk-forward
  (pooled Brier score); (b) HistGradientBoostingClassifier with small, fixed, untuned settings
  (HGB_PARAMS below). Walk-forward: 7-day blocks starting at each period's first day; each block is
  predicted by a model fit on every 5-minute row from 3/24 whose target ended before the block
  starts (expanding, purged); the first 14 days of A only train, so out-of-sample A starts 4/7.
- Economics: at each non-overlapping window open, buy the predicted side at 0.51 when P(up) >=
  0.5 + theta (down when <= 0.5 - theta), theta in 0.025/0.04/0.06, taker fee 0.07 p (1 - p), held
  to the end; per-trade EV with standard errors clustered by UTC day. 5 m also on kacho.io's books
  (3/24-5/18; out-of-sample predictions exist from 4/7): the ask in kacho's row stamped at the
  window open (used from open + 1 s), at least 5 shares, against the official outcome.
- Freeze per horizon on A + B only: the (model, theta) with the highest B walk-forward EV among
  those with at least 300 B trades -> frozen.json; then C once (walk-forward, refits only on rows
  whose target ended before each C block), pass = EV > 0, one-sided day-clustered p < 0.05, >= 300.
  4 h has only 276 non-overlapping windows in B and in C, so 300 trades cannot be reached there:
  it is frozen on the highest B EV among candidates with at least 100 B trades and reported as
  unable to pass.

    python factors.py study   [--cache DIR] [--klines DIR] [--kacho DIR]  # A + B; writes frozen.json
    python factors.py lockbox [--cache DIR] [--force]                      # C, once
    python factors.py report  [--cache DIR] [--out real/factors.md]
    python factors.py all     ...                                          # the three in order
"""
from __future__ import annotations

import argparse
import json
import pickle
import time
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

SCRATCH = Path("/tmp/claude-0/-home-user-whole-web-station-clone/d12980d9-5808-53a6-9da0-aeb3bb8bdfb4/scratchpad")
CACHE = SCRATCH / "factors"
KLINES = SCRATCH / "klines"
KACHO = SCRATCH / "kacho"

PERIODS = {"A": ("2026-03-24", "2026-07-01"), "B": ("2026-07-01", "2026-08-16"), "C": ("2026-08-16", "2026-10-01")}
HORIZONS = (("5m", 5), ("15m", 15), ("1h", 60), ("4h", 240))
HMIN = dict(HORIZONS)
STEP = 300
DAY = 86400
TRAIN_START = "2026-03-24"
MIN_TRAIN_DAYS = 14
BLOCK_DAYS = 7
THETAS = (0.025, 0.04, 0.06)
PRICE = 0.51
FEE = 0.07
MIN_TRADES = 300
MIN_TRADES_FALLBACK = 100  # 4 h: B has only 276 windows, so 300 is out of reach (fixed before C was run)
METRICS_FEATURES = ["oi_chg_5m", "oi_chg_15m", "oi_chg_60m", "oi_chg_240m", "oi_x_price_1h", "oi_z7d", "liq_proxy",
                    "top_acct_ls", "top_acct_ls_chg1h", "top_pos_ls", "top_pos_ls_chg1h", "taker_ls_5m"]
SPOT_FEATURES = ["ret_1m", "ret_5m", "ret_15m", "ret_60m", "ret_240m", "ret_24h", "spot_tbr_1m", "spot_tbr_5m",
                 "spot_tbr_15m", "spot_tbr_60m", "spot_ntv_1m", "spot_ntv_5m", "spot_ntv_15m", "spot_ntv_60m",
                 "spot_lvr_5m", "spot_lvr_1h", "rv30", "vr10", "tod_sin", "tod_cos", "weekend"]
RIDGE_ALPHAS = (1.0, 10.0, 100.0, 1e3, 3e3, 1e4, 3e4, 1e5, 3e5, 1e6)
HGB_PARAMS = dict(learning_rate=0.05, max_iter=150, max_depth=3, max_leaf_nodes=8, min_samples_leaf=400,
           l2_regularization=1.0, max_bins=63, early_stopping=False, random_state=0)
MODELS = ("ridge", "hgb")
CAL_EDGES = (0.0, 0.44, 0.46, 0.475, 0.5, 0.525, 0.54, 0.56, 1.0)

FAMILIES = (
    ("价格", ["ret_1m", "ret_5m", "ret_15m", "ret_60m", "ret_240m", "ret_24h"]),
    ("主动买卖", [f"{s}_tbr_{k}m" for s in ("spot", "fut") for k in (1, 5, 15, 60)]
     + [f"{s}_ntv_{k}m" for s in ("spot", "fut") for k in (1, 5, 15, 60)]),
    ("成交量", ["spot_lvr_5m", "spot_lvr_1h", "fut_lvr_5m", "fut_lvr_1h"]),
    ("持仓量", ["oi_chg_5m", "oi_chg_15m", "oi_chg_60m", "oi_chg_240m", "oi_x_price_1h", "oi_z7d", "liq_proxy"]),
    ("多空比", ["top_acct_ls", "top_acct_ls_chg1h", "top_pos_ls", "top_pos_ls_chg1h", "taker_ls_5m"]),
    ("资金费率和基差", ["funding", "premium", "premium_1h_mean", "premium_chg_1h"]),
    ("波动区间", ["rv30", "dvol", "dvol_minus_rv30", "vr10"]),
    ("期权 gamma", ["dealer_gamma", "cust_delta_1h", "cust_delta_24h", "pin_1k", "pin_5k"]),
    ("日历", ["tod_sin", "tod_cos", "weekend", "h_to_funding"]),
)
FACTORS = [f for _, fs in FAMILIES for f in fs]
FAMILY = {f: fam for fam, fs in FAMILIES for f in fs}
AUX = ["zoi15", "zr15", "dealer_gamma_7d", "pin_dist_1k_signed", "pin_dist_1k", "hours_to_fri", "rv_past_1h",
       "dvol_src_1m"]


def ts(day):
    return int(pd.Timestamp(day, tz="UTC").timestamp())


def period_of(T):
    T = np.asarray(T, np.int64)
    out = np.full(len(T), "", dtype=object)
    for name, (lo, hi) in PERIODS.items():
        out[(T >= ts(lo)) & (T < ts(hi))] = name
    return out


# ---------------------------------------------------------------- loading

def _secs(index):
    return pd.DatetimeIndex(index).as_unit("ns").asi8 // 10**9


def load_panel(path):
    p = pd.read_parquet(path)
    p.index = pd.Index(_secs(p.index), name="t")
    return p


def load_deribit(path):
    d = pd.read_parquet(path)
    d.index = pd.Index(_secs(d.index), name="known_at")
    return d


def load_spot(klines=KLINES, cache=CACHE):
    """(sec0, close): per-second close of the 1 s kline opening at sec0 + i (forward-filled)."""
    f = Path(cache) / "spot_1s_close.parquet"
    if f.exists():
        s = pd.read_parquet(f)
        idx = s.index.to_numpy(np.int64)
    else:
        import regime as rg
        k = rg.load_klines(klines)
        s = pd.DataFrame({"close": k["close"].to_numpy(np.float64)}, index=pd.Index(k.index.to_numpy(np.int64), name="sec"))
        Path(cache).mkdir(parents=True, exist_ok=True)
        s.to_parquet(f)
        idx = s.index.to_numpy(np.int64)
    assert (np.diff(idx) == 1).all()
    return int(idx[0]), s["close"].to_numpy(np.float64)


# ---------------------------------------------------------------- features and targets

def _lp_at(lp, sec0, t):
    """log of the last 1 s close before t (the kline opening at t - 1); NaN outside the data."""
    k = np.asarray(t, np.int64) - 1 - sec0
    out = np.full(len(k), np.nan)
    ok = (k >= 0) & (k < len(lp))
    out[ok] = lp[k[ok]]
    return out


def _window(c, sec0, n, a, b):
    """Sum over seconds [a, b) of a series whose prefix sums are c (c[k] = sum of the first k)."""
    ia, ib = a - sec0, b - sec0
    out = np.full(len(a), np.nan)
    ok = (ia >= 0) & (ib <= n) & (ib > ia)
    out[ok] = c[ib[ok]] - c[ia[ok]]
    return out


def _second_stats(sec0, close, T):
    """rv30, vr10 (previous 1,800 s) and rv over the previous hour at T, from 1 s closes; second i's
    return is lp[i] - lp[i - 1] (known at i + 1), so the window [T - 1800, T) is known at T."""
    lp = np.log(close)
    n = len(lp)
    r = np.diff(lp, prepend=np.nan)
    x10 = np.full(n, np.nan)
    x10[10:] = lp[10:] - lp[:-10]

    def pre(v):
        ok = np.isfinite(v)
        return (np.concatenate([[0.0], np.cumsum(np.where(ok, v, 0.0))]),
                np.concatenate([[0.0], np.cumsum(np.where(ok, v * v, 0.0))]),
                np.concatenate([[0], np.cumsum(ok)]))

    out = {}
    a, b = T - 1800, T
    var = {}
    for name, v in (("r", r), ("x10", x10)):
        c1, c2, cn = pre(v)
        s1, s2, m = (_window(c, sec0, n, a, b) for c in (c1, c2, cn))
        with np.errstate(invalid="ignore", divide="ignore"):
            var[name] = np.where(m >= 900, (s2 - s1 * s1 / m) / (m - 1), np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        out["rv30"] = np.sqrt(var["r"]) * np.sqrt(DAY * 365)
        out["vr10"] = var["x10"] / (10 * var["r"])
    c1, c2, cn = pre(r)
    out["rv_past_1h"] = np.sqrt(_window(c2, sec0, n, T - 3600, T)) * np.sqrt(24 * 365)
    return out


def _panel_series(p):
    """Per-minute series, each value at row T using rows <= T only (trailing windows, shifts)."""
    idx = p.index.to_numpy()
    assert (np.diff(idx) == 60).all(), "panel must be a complete minute grid"
    f = pd.DataFrame(index=p.index)
    for src in ("spot", "fut"):
        vol, buy = p[f"{src}_vol"], p[f"{src}_buy"]
        for k in (1, 5, 15, 60):
            v = vol.rolling(k, min_periods=k).sum()
            bb = buy.rolling(k, min_periods=k).sum()
            f[f"{src}_tbr_{k}m"] = bb / v.where(v > 0) - 0.5
            f[f"{src}_ntv_{k}m"] = 2 * bb - v
        v24 = vol.rolling(1440, min_periods=1440).mean()
        with np.errstate(divide="ignore", invalid="ignore"):
            f[f"{src}_lvr_5m"] = np.log(vol.rolling(5, min_periods=5).mean() / v24)
            f[f"{src}_lvr_1h"] = np.log(vol.rolling(60, min_periods=60).mean() / v24)
    oi = p["sum_open_interest"]
    for k in (5, 15, 60, 240):
        f[f"oi_chg_{k}m"] = np.log(oi / oi.shift(k))
    f["oi_z7d"] = (oi - oi.rolling(7 * 1440, min_periods=3 * 1440).mean()) / oi.rolling(7 * 1440, min_periods=3 * 1440).std()
    for col, name in (("count_toptrader_long_short_ratio", "top_acct_ls"), ("sum_toptrader_long_short_ratio", "top_pos_ls")):
        x = np.log(p[col])
        f[name] = x
        f[f"{name}_chg1h"] = x - x.shift(60)
    f["taker_ls_5m"] = np.log(p["sum_taker_long_short_vol_ratio"])
    f["funding"] = p["funding_rate"]
    f["premium"] = p["premium"]
    f["premium_1h_mean"] = p["premium"].rolling(60, min_periods=60).mean()
    f["premium_chg_1h"] = p["premium"] - p["premium"].shift(60)
    f["dvol"] = p["dvol"].fillna(p["dvol_1h"]) / 100
    f["dvol_src_1m"] = p["dvol"].notna().astype(float)
    return f


def build_features(panel, deribit, sec0, close, T):
    """Factor values (FACTORS) and auxiliary columns (AUX) at decision times T (int seconds,
    multiples of 300). Uses panel rows <= T, Deribit rows known_at <= T and 1 s klines opening
    before T only; computed on the full contiguous 5-minute grid up to max(T), then sampled."""
    T = np.asarray(T, np.int64)
    p0 = int(panel.index[0])
    G = np.arange(((p0 + STEP - 1) // STEP) * STEP, int(T.max()) + 1, STEP, dtype=np.int64)
    lp = np.log(close)
    f = pd.DataFrame(index=pd.Index(G, name="T"))
    l0 = _lp_at(lp, sec0, G)
    for k, name in ((1, "ret_1m"), (5, "ret_5m"), (15, "ret_15m"), (60, "ret_60m"), (240, "ret_240m"), (1440, "ret_24h")):
        f[name] = l0 - _lp_at(lp, sec0, G - k * 60)
    ps = _panel_series(panel).reindex(G)
    for c in ps.columns:
        f[c] = ps[c].to_numpy()
    f["oi_x_price_1h"] = np.sign(f["ret_60m"]) * f["oi_chg_60m"]
    w = 7 * DAY // STEP
    f["zoi15"] = f["oi_chg_15m"] / f["oi_chg_15m"].rolling(w, min_periods=w * 3 // 7).std()
    f["zr15"] = f["ret_15m"] / f["ret_15m"].rolling(w, min_periods=w * 3 // 7).std()
    f["liq_proxy"] = np.maximum(-f["zoi15"], 0.0) * f["zr15"]
    st = _second_stats(sec0, close, G)
    for k, v in st.items():
        f[k] = v
    f["dvol_minus_rv30"] = f["dvol"] - f["rv30"]
    d = deribit.reindex(G)
    f["dealer_gamma"] = d["dealer_gamma_usd_1pct"].to_numpy() / 1e6
    f["dealer_gamma_7d"] = d["dealer_gamma_7d_usd_1pct"].to_numpy() / 1e6
    f["cust_delta_1h"] = d["cust_delta_flow_1h"].to_numpy()
    f["cust_delta_24h"] = d["cust_delta_flow_24h"].to_numpy()
    f["pin_1k"] = (d["pin_dist_1k_pct"] * d["hours_to_fri_expiry"]).to_numpy()
    f["pin_5k"] = (d["pin_dist_5k_pct"] * d["hours_to_fri_expiry"]).to_numpy()
    f["pin_dist_1k_signed"] = d["pin_dist_1k_signed_pct"].to_numpy()
    f["pin_dist_1k"] = d["pin_dist_1k_pct"].to_numpy()
    f["hours_to_fri"] = d["hours_to_fri_expiry"].to_numpy()
    tod = (G % DAY) / DAY
    f["tod_sin"] = np.sin(2 * np.pi * tod)
    f["tod_cos"] = np.cos(2 * np.pi * tod)
    f["weekend"] = (((G // DAY) + 3) % 7 >= 5).astype(float)  # 1970-01-01 was a Thursday
    f["h_to_funding"] = ((-G) % (8 * 3600)) / 3600
    return f.reindex(T)[FACTORS + AUX]


def build_targets(sec0, close, T):
    T = np.asarray(T, np.int64)
    lp = np.log(close)
    l0 = _lp_at(lp, sec0, T)
    out = pd.DataFrame(index=pd.Index(T, name="T"))
    for name, h in HORIZONS + (("24h", 1440),):
        r = _lp_at(lp, sec0, T + h * 60) - l0
        out[f"r_{name}"] = r
    r = np.diff(lp, prepend=np.nan)
    c2 = np.concatenate([[0.0], np.cumsum(np.where(np.isfinite(r), r * r, 0.0))])
    out["rv_fwd_1h"] = np.sqrt(_window(c2, sec0, len(lp), T, T + 3600)) * np.sqrt(24 * 365)
    return out


def up_of(r):
    r = np.asarray(r, float)
    return np.where(np.isfinite(r), (r >= 0).astype(float), np.nan)


# ---------------------------------------------------------------- statistics

def auto_lags(n):
    return int(np.floor(4 * (max(n, 1) / 100) ** (2 / 9)))


def nw_lrv(w, lags):
    w = np.asarray(w, float) - np.mean(w)
    n = len(w)
    v = w @ w / n
    for k in range(1, min(lags, n - 1) + 1):
        v += 2 * (1 - k / (lags + 1)) * (w[k:] @ w[:-k]) / n
    return v


def spearman_nw(x, y, lags=None):
    """Spearman rho = mean(u v) of standardised ranks; t from the Newey-West variance of u v
    (rows in time order). Returns (rho, t, p two-sided, n)."""
    x, y = np.asarray(x, float), np.asarray(y, float)
    m = np.isfinite(x) & np.isfinite(y)
    n = int(m.sum())
    if n < 30:
        return np.nan, np.nan, np.nan, n
    u, v = stats.rankdata(x[m]), stats.rankdata(y[m])
    if u.std() == 0 or v.std() == 0:
        return np.nan, np.nan, np.nan, n
    u, v = (u - u.mean()) / u.std(), (v - v.mean()) / v.std()
    w = u * v
    rho = float(w.mean())
    se = np.sqrt(max(nw_lrv(w, auto_lags(n) if lags is None else lags), 1e-300) / n)
    t = rho / se
    return rho, float(t), float(2 * stats.norm.sf(abs(t))), n


def hac_mean(x, lags):
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 5:
        return np.nan, np.nan, n
    return float(x.mean()), float(np.sqrt(nw_lrv(x, lags) / n)), n


def cluster_mean(x, g):
    """Mean of x and its standard error clustered by g; (mean, se, n, clusters)."""
    x, g = np.asarray(x, float), np.asarray(g)
    m = np.isfinite(x)
    x, g = x[m], g[m]
    n = len(x)
    if n == 0:
        return np.nan, np.nan, 0, 0
    mu = float(x.mean())
    s = pd.Series(x - mu).groupby(g).sum()
    G = len(s)
    se = float(np.sqrt(G / (G - 1) * (s ** 2).sum()) / n) if G > 1 else np.nan
    return mu, se, n, G


def ols_cluster(y, X, g):
    """OLS with standard errors clustered by g; rows with any NaN dropped. (coef, se, n)."""
    y, X, g = np.asarray(y, float), np.asarray(X, float), np.asarray(g)
    m = np.isfinite(y) & np.isfinite(X).all(axis=1)
    y, X, g = y[m], X[m], g[m]
    n, k = X.shape
    if n <= k + 2:
        return np.full(k, np.nan), np.full(k, np.nan), n
    xtx_inv = np.linalg.pinv(X.T @ X)
    b = xtx_inv @ X.T @ y
    e = y - X @ b
    sc = pd.DataFrame(X * e[:, None]).groupby(g).sum().to_numpy()
    G = len(sc)
    V = xtx_inv @ (sc.T @ sc) @ xtx_inv * G / max(G - 1, 1)
    return b, np.sqrt(np.diag(V)), n


def grid_mask(T, h_min):
    return (np.asarray(T) % (h_min * 60)) == 0


# ---------------------------------------------------------------- univariate

def univariate(df, factors=FACTORS, periods=("A", "B")):
    """Spearman IC per factor x horizon x period on the non-overlapping grid ('main') and the
    5-minute grid with h/5 - 1 Newey-West lags ('overlap')."""
    rows = []
    T = df.index.to_numpy()
    per = period_of(T)
    for hname, h in HORIZONS:
        y = df[f"r_{hname}"].to_numpy()
        gm = grid_mask(T, h)
        for pname in periods:
            sel = per == pname
            for f in factors:
                x = df[f].to_numpy()
                for grid, m, lags in (("main", sel & gm, None), ("overlap", sel, h // 5 - 1)):
                    rho, t, p, n = spearman_nw(x[m], y[m], lags)
                    rows.append((f, FAMILY[f], hname, pname, grid, rho, t, p, n))
    return pd.DataFrame(rows, columns=["factor", "family", "h", "period", "grid", "ic", "t", "p", "n"])


def consistent(ic, grid="main", alpha=0.05):
    """Factors x horizons significant (Bonferroni over factors x horizons) in A and in B, same sign."""
    m = len(FACTORS) * len(HORIZONS)
    g = ic[ic["grid"] == grid].pivot_table(index=["factor", "h"], columns="period", values=["ic", "p"])
    ok = (g[("p", "A")] < alpha / m) & (g[("p", "B")] < alpha / m) & (np.sign(g[("ic", "A")]) == np.sign(g[("ic", "B")]))
    return g[ok]


# ---------------------------------------------------------------- models

class RidgeLPM:
    """Linear-probability ridge on the up indicator; features standardised on the training rows,
    clipped at +-5 sd, NaN -> 0 (the training mean)."""

    def __init__(self, alpha):
        self.alpha = alpha

    def _z(self, X):
        Z = (X - self.mu) / self.sd
        return np.clip(np.where(np.isfinite(Z), Z, 0.0), -5, 5) - self.zm

    def fit(self, X, y):
        self.mu = np.nanmean(X, axis=0)
        sd = np.nanstd(X, axis=0)
        self.mu = np.where(np.isfinite(self.mu), self.mu, 0.0)
        self.sd = np.where(np.isfinite(sd) & (sd > 0), sd, 1.0)
        self.zm = 0.0
        Z0 = self._z(X)
        self.zm = Z0.mean(axis=0)
        Z = Z0 - self.zm
        self.ym = float(y.mean())
        self.coef = np.linalg.solve(Z.T @ Z + self.alpha * np.eye(Z.shape[1]), Z.T @ (y - self.ym))
        return self

    def predict_proba1(self, X):
        return np.clip(self.ym + self._z(X) @ self.coef, 0.001, 0.999)


class HGB:
    def __init__(self, **kw):
        from sklearn.ensemble import HistGradientBoostingClassifier
        self.m = HistGradientBoostingClassifier(**{**HGB_PARAMS, **kw})

    def fit(self, X, y):
        self.m.fit(X, y.astype(int))
        return self

    def predict_proba1(self, X):
        return self.m.predict_proba(X)[:, 1]


def make_model(name, alpha=None):
    return RidgeLPM(alpha) if name == "ridge" else HGB()


def blocks(names):
    out = []
    for nm in names:
        lo, hi = ts(PERIODS[nm][0]), ts(PERIODS[nm][1])
        b = lo
        while b < hi:
            out.append((b, min(b + BLOCK_DAYS * DAY, hi)))
            b += BLOCK_DAYS * DAY
    return out


def walk_forward(X, up, T, h_min, model, blks, alpha=None, train_start=TRAIN_START, min_train_days=MIN_TRAIN_DAYS,
                 factory=None):
    """Out-of-sample P(up) for the rows of each block from a model fit on rows T >= train_start
    whose target ended by the block start (T + h <= block start). Returns (pred, fitted models)."""
    pred = np.full(len(T), np.nan)
    fits = []
    t0 = ts(train_start)
    ok_y = np.isfinite(up)
    for b0, b1 in blks:
        if b0 - t0 < min_train_days * DAY:
            continue
        tr = (T >= t0) & (T + h_min * 60 <= b0) & ok_y
        te = (T >= b0) & (T < b1)
        if tr.sum() < 500 or not te.any():
            continue
        m = (factory or make_model)(model, alpha).fit(X[tr], up[tr])
        pred[te] = m.predict_proba1(X[te])
        fits.append((b0, m))
    return pred, fits


def choose_alpha(X, up, T, h_min):
    """Ridge alpha by walk-forward inside A (pooled Brier score of the out-of-sample A rows)."""
    blk = blocks(["A"])
    res = {}
    for a in RIDGE_ALPHAS:
        p, _ = walk_forward(X, up, T, h_min, "ridge", blk, alpha=a)
        m = np.isfinite(p) & np.isfinite(up)
        res[a] = float(np.mean((p[m] - up[m]) ** 2))
    best = min(res, key=res.get)
    return best, res


# ---------------------------------------------------------------- evaluation

def trades(p, up, T, h_min, theta, price=PRICE):
    """Non-overlapping window opens where |P(up) - 0.5| >= theta: side, won, pnl at `price`."""
    m = grid_mask(T, h_min) & np.isfinite(p) & np.isfinite(up)
    buy_up = m & (p >= 0.5 + theta)
    buy_dn = m & (p <= 0.5 - theta)
    sel = buy_up | buy_dn
    won = np.where(buy_up, up, 1 - up)[sel]
    pnl = won - price - FEE * price * (1 - price)
    return pd.DataFrame({"T": T[sel], "side_up": buy_up[sel], "won": won, "pnl": pnl, "p": p[sel]})


def econ_stats(t):
    if not len(t):
        return dict(ev=np.nan, se=np.nan, n=0, days=0, t=np.nan, p1=np.nan, win=np.nan)
    mu, se, n, G = cluster_mean(t["pnl"].to_numpy(), t["T"].to_numpy() // DAY)
    tt = mu / se if se and se > 0 else np.nan
    p1 = float(stats.t.sf(tt, G - 1)) if np.isfinite(tt) and G > 1 else np.nan
    return dict(ev=mu, se=se, n=n, days=G, t=tt, p1=p1, win=float(t["won"].mean()))


def oos_stats(p, r, T, h_min):
    up = up_of(r)
    gm = grid_mask(T, h_min)
    m = gm & np.isfinite(p) & np.isfinite(r)
    ic = spearman_nw(p[m], r[m])
    ico = spearman_nw(p[np.isfinite(p)], r[np.isfinite(p)], h_min // 5 - 1)
    hit = ((p[m] >= 0.5) == (up[m] == 1)).astype(float)
    acc = cluster_mean(hit, T[m] // DAY)
    return dict(ic=ic[0], t=ic[1], n=ic[3], ic_ov=ico[0], t_ov=ico[1], n_ov=ico[3], acc=acc[0], acc_se=acc[1],
                brier=float(np.mean((p[m] - up[m]) ** 2)), up_rate=float(np.mean(up[m])), p_sd=float(np.std(p[m])))


def calibration(p, r, T, h_min, edges=CAL_EDGES):
    up = up_of(r)
    m = grid_mask(T, h_min) & np.isfinite(p) & np.isfinite(up)
    b = np.digitize(p[m], edges[1:-1])
    rows = []
    for k in range(len(edges) - 1):
        s = b == k
        rows.append((f"{edges[k]:.3f}–{edges[k + 1]:.3f}", float(p[m][s].mean()) if s.any() else np.nan,
                     float(up[m][s].mean()) if s.any() else np.nan, int(s.sum())))
    return pd.DataFrame(rows, columns=["bin", "p_mean", "up_rate", "n"])


# ---------------------------------------------------------------- kacho

def kacho_open(kacho=KACHO):
    """Per kacho market: open S, official up_won and the book row stamped S (used from S + 1 s)."""
    m = pd.read_parquet(Path(kacho) / "btc_markets.parquet", columns=["condition_id", "slug", "market_start"])
    o = pd.read_csv(Path(kacho) / "btc_outcomes.csv")
    m = m.merge(o, on="slug")
    m["S"] = _secs(m["market_start"])
    t = pd.read_parquet(Path(kacho) / "btc_ticks.parquet", columns=["condition_id", "t", "au", "ad", "sau", "sad"])
    t = t.merge(m[["condition_id", "S"]], on="condition_id")
    t = t[t["t"] == t["S"]].drop_duplicates("condition_id")
    out = m[["S", "up_won"]].merge(t[["S", "au", "ad", "sau", "sad"]], on="S", how="left")
    return out.sort_values("S").drop_duplicates("S").reset_index(drop=True)


def kacho_trades(k, p_at, theta):
    """Trades at kacho's opening ask (row stamped S, >= 5 shares, 0.02..0.98) against the
    official outcome; p_at maps S -> P(up)."""
    p = p_at.reindex(k["S"]).to_numpy()
    rows = []
    for (S, upw, au, ad, sau, sad), pp in zip(k[["S", "up_won", "au", "ad", "sau", "sad"]].itertuples(index=False), p):
        if not np.isfinite(pp) or abs(pp - 0.5) < theta:
            continue
        side_up = pp >= 0.5
        ask, size = (au, sau) if side_up else (ad, sad)
        won = float(upw) if side_up else 1.0 - float(upw)
        ok = np.isfinite(ask) and 0.02 <= ask <= 0.98 and np.isfinite(size) and size >= 5
        rows.append((S, side_up, ask if ok else np.nan, won, pp))
    t = pd.DataFrame(rows, columns=["T", "side_up", "ask", "won", "p"])
    t["pnl_051"] = t["won"] - PRICE - FEE * PRICE * (1 - PRICE)
    t["pnl"] = t["won"] - t["ask"] - FEE * t["ask"] * (1 - t["ask"])
    return t


# ---------------------------------------------------------------- hypothesis checks (A and B)

def _per(df):
    return period_of(df.index.to_numpy())


def hyp_oi_price(df):
    rows = []
    T = df.index.to_numpy()
    per = _per(df)
    s = np.sign(df["ret_60m"].to_numpy())
    same = (np.sign(df["oi_chg_60m"].to_numpy()) == s) & (s != 0)
    opp = (np.sign(df["oi_chg_60m"].to_numpy()) == -s) & (s != 0)
    for hname in ("5m", "15m", "1h"):
        cont = s * df[f"r_{hname}"].to_numpy() * 1e4
        gm = grid_mask(T, HMIN[hname])
        for pn in ("A", "B"):
            m = gm & (per == pn)
            a = cluster_mean(cont[m & same], T[m & same] // DAY)
            b = cluster_mean(cont[m & opp], T[m & opp] // DAY)
            mm = m & (same | opp)
            X = np.column_stack([np.ones(mm.sum()), same[mm].astype(float)])
            coef, se, _ = ols_cluster(cont[mm], X, T[mm] // DAY)
            rows.append((hname, pn, a[0], a[1], a[2], b[0], b[1], b[2], coef[1], se[1]))
    return pd.DataFrame(rows, columns=["h", "period", "same", "same_se", "same_n", "opp", "opp_se", "opp_n", "diff", "diff_se"])


def liq_events(df, z_oi=-2.0, z_r=2.0, gap=3600):
    T = df.index.to_numpy()
    ev = (df["zoi15"].to_numpy() <= z_oi) & (np.abs(df["zr15"].to_numpy()) >= z_r)
    keep, last = [], -np.inf
    for i in np.flatnonzero(ev):
        if T[i] - last >= gap:
            keep.append(i)
            last = T[i]
    return np.array(keep, dtype=int)


def hyp_liq(df):
    T = df.index.to_numpy()
    per = _per(df)
    k = liq_events(df)
    s = np.sign(df["ret_15m"].to_numpy())
    rows = []
    for pn in ("A", "B"):
        kk = k[per[k] == pn]
        row = [pn, len(kk)]
        for hname, _ in HORIZONS:
            x = s[kk] * df[f"r_{hname}"].to_numpy()[kk] * 1e4
            mu, se, n, _ = cluster_mean(x, T[kk] // DAY)
            row += [mu, se]
        rows.append(row)
    cols = ["period", "n"] + [f"{c}_{h}" for h, _ in HORIZONS for c in ("m", "se")]
    return pd.DataFrame(rows, columns=cols)


def hyp_gamma(df):
    """Momentum x dealer gamma (hourly grid, cluster by day) and gamma vs next-hour realised vol."""
    T = df.index.to_numpy()
    per = _per(df)
    hr = grid_mask(T, 60)
    g = df["dealer_gamma"].to_numpy()
    sdA = np.nanstd(g[(per == "A") & hr])
    out = {"mom": [], "rv": [], "q": []}
    cuts = np.nanquantile(g[(per == "A") & hr], [0.2, 0.4, 0.6, 0.8])
    for pn in ("A", "B"):
        m = hr & (per == pn)
        for hname, past in (("1h", "ret_60m"), ("15m", "ret_15m")):
            mm = m & grid_mask(T, HMIN[hname])
            y = df[f"r_{hname}"].to_numpy()[mm] * 1e4
            x = df[past].to_numpy()[mm] * 1e4
            gz = g[mm] / sdA
            X = np.column_stack([np.ones(len(x)), x, x * gz, gz])
            coef, se, n = ols_cluster(y, X, T[mm] // DAY)
            s = np.sign(x)
            neg = cluster_mean((s * y)[gz < 0], (T[mm] // DAY)[gz < 0])
            pos = cluster_mean((s * y)[gz >= 0], (T[mm] // DAY)[gz >= 0])
            out["mom"].append((pn, hname, coef[1], se[1], coef[2], se[2], n, neg[0], neg[1], neg[2], pos[0], pos[1], pos[2]))
        q = np.searchsorted(cuts, g[m], side="right")
        cont = np.sign(df["ret_60m"].to_numpy()[m]) * df["r_1h"].to_numpy()[m] * 1e4
        for k in range(5):
            s_ = q == k
            c = cluster_mean(cont[s_], (T[m] // DAY)[s_])
            rvf = np.nanmean(df["rv_fwd_1h"].to_numpy()[m][s_])
            out["q"].append((pn, k + 1, c[0], c[1], c[2], rvf))
        # gamma vs next-hour RV: raw and after log past-1h RV, log DVOL and hour-of-day dummies
        d = df[m]
        ylog = np.log(d["rv_fwd_1h"].to_numpy())
        hours = (d.index.to_numpy() % DAY) // 3600
        X = np.column_stack([np.ones(len(d)), np.log(d["rv_past_1h"].to_numpy()), np.log(d["dvol"].to_numpy())]
                            + [(hours == k).astype(float) for k in range(1, 24)])
        ok = np.isfinite(ylog) & np.isfinite(X).all(axis=1)
        res = np.full(len(d), np.nan)
        b, *_ = np.linalg.lstsq(X[ok], ylog[ok], rcond=None)
        res[ok] = ylog[ok] - X[ok] @ b
        for col in ("dealer_gamma", "dealer_gamma_7d"):
            raw = spearman_nw(d[col].to_numpy(), d["rv_fwd_1h"].to_numpy())
            ctl = spearman_nw(d[col].to_numpy(), res)
            past = spearman_nw(d[col].to_numpy(), d["rv_past_1h"].to_numpy())
            out["rv"].append((pn, col, raw[0], raw[1], ctl[0], ctl[1], past[0], ctl[3]))
    return (pd.DataFrame(out["mom"], columns=["period", "h", "mom", "mom_se", "mom_x_gz", "mom_x_gz_se", "n",
                                              "cont_neg", "cont_neg_se", "n_neg", "cont_pos", "cont_pos_se", "n_pos"]),
            pd.DataFrame(out["q"], columns=["period", "q", "cont", "cont_se", "n", "rv_fwd"]),
            pd.DataFrame(out["rv"], columns=["period", "gamma", "raw", "raw_t", "ctl", "ctl_t", "past", "n"]))


def hyp_pin(df):
    """Pull toward the nearest 1,000 strike over the next hour (bp; + = toward it), hourly grid,
    in the last 24 h before the Friday 08:00 expiry vs the rest, split by dealer gamma sign; and
    next-hour realised vol by closeness to the strike."""
    T = df.index.to_numpy()
    per = _per(df)
    hr = grid_mask(T, 60)
    pull = -np.sign(df["pin_dist_1k_signed"].to_numpy()) * df["r_1h"].to_numpy() * 1e4
    near = df["hours_to_fri"].to_numpy() <= 24
    g = df["dealer_gamma"].to_numpy()
    close_ = df["pin_dist_1k"].to_numpy() <= 0.2
    rows = []
    for pn in ("A", "B"):
        m = hr & (per == pn)
        for name, s in (("周五到期前 24 小时，gamma > 0", near & (g > 0)), ("周五到期前 24 小时，gamma ≤ 0", near & (g <= 0)),
                        ("周五到期前 24 小时，全部", near), ("其他时间", ~near)):
            mm = m & s
            c = cluster_mean(pull[mm], T[mm] // DAY)
            rvc = np.nanmean(df["rv_fwd_1h"].to_numpy()[mm & close_])
            rvf = np.nanmean(df["rv_fwd_1h"].to_numpy()[mm & ~close_])
            rows.append((pn, name, c[0], c[1], c[2], rvc, rvf))
    return pd.DataFrame(rows, columns=["period", "group", "pull", "pull_se", "n", "rv_close", "rv_far"])


def hyp_funding(df):
    T = df.index.to_numpy()
    per = _per(df)
    g4 = grid_mask(T, 240)
    f = df["funding"].to_numpy()
    lo, hi = np.nanquantile(f[(per == "A") & g4], [0.1, 0.9])
    rows = []
    for pn in ("A", "B"):
        m = g4 & (per == pn)
        for name, s in ((f"最高 10%（≥ {hi * 1e4:.2f} bp）", f >= hi), (f"最低 10%（≤ {lo * 1e4:.2f} bp）", f <= lo),
                        ("中间 80%", (f > lo) & (f < hi))):
            mm = m & s
            a = cluster_mean(df["r_4h"].to_numpy()[mm] * 1e4, T[mm] // DAY)
            b = cluster_mean(df["r_24h"].to_numpy()[mm] * 1e4, T[mm] // (7 * DAY))
            rows.append((pn, name, a[0], a[1], b[0], b[1], a[2]))
    return pd.DataFrame(rows, columns=["period", "group", "r4h", "r4h_se", "r24h", "r24h_se", "n"])


# ---------------------------------------------------------------- pipeline

def load_frame(cache=CACHE, klines=KLINES, rebuild=False):
    """Features + targets on the 5-minute grid 3/14 .. 9/30 23:55 (cached)."""
    f = Path(cache) / "features_5m.parquet"
    if f.exists() and not rebuild:
        return pd.read_parquet(f)
    panel = load_panel(Path(cache) / "panel_1m.parquet")
    der = load_deribit(Path(cache) / "deribit_5m.parquet")
    sec0, close = load_spot(klines, cache)
    T = np.arange(ts("2026-03-15"), ts("2026-10-01"), STEP, dtype=np.int64)
    df = build_features(panel, der, sec0, close, T).join(build_targets(sec0, close, T))
    df.to_parquet(f)
    return df


def mask_c(df):
    """Copy with every target that ends after the start of C set to NaN (A + B stage)."""
    d = df.copy()
    c0 = ts(PERIODS["C"][0])
    T = d.index.to_numpy()
    for hname, h in HORIZONS + (("24h", 1440),):
        d.loc[T + h * 60 > c0, f"r_{hname}"] = np.nan
    d.loc[T + 3600 > c0, "rv_fwd_1h"] = np.nan
    return d


def study(cache=CACHE, klines=KLINES, kacho=KACHO, rebuild=False, log=print):
    t0 = time.time()
    full = load_frame(cache, klines, rebuild)
    df = mask_c(full)
    T = df.index.to_numpy()
    per = period_of(T)
    X = df[FACTORS].to_numpy(float)
    R = {"made": pd.Timestamp.now(tz="UTC").isoformat(), "n_rows": len(df)}
    log(f"features {len(df):,} rows, {len(FACTORS)} factors ({time.time() - t0:.0f}s)")
    R["coverage"] = {f: {p: float(np.isfinite(df[f].to_numpy()[per == p]).mean()) for p in ("A", "B", "C")} for f in FACTORS}
    R["up_rate"] = {h: {p: float(np.nanmean(up_of(df[f"r_{h}"].to_numpy())[(per == p) & grid_mask(T, hm)]))
                        for p in ("A", "B")} for h, hm in HORIZONS}
    ic = univariate(df)
    R["ic"] = ic
    log(f"univariate done ({time.time() - t0:.0f}s)")
    preds = pd.DataFrame(index=df.index)
    R["alpha"], R["alpha_cv"], R["oos"], R["cal"], R["econ"], R["coef"], R["perm"] = {}, {}, [], {}, [], {}, {}
    blk = blocks(["A", "B"])
    for hname, h in HORIZONS:
        r = df[f"r_{hname}"].to_numpy()
        up = up_of(r)
        a, cv = choose_alpha(X, up, T, h)
        R["alpha"][hname], R["alpha_cv"][hname] = a, cv
        for model in MODELS:
            p, fits = walk_forward(X, up, T, h, model, blk, alpha=a)
            preds[f"{model}_{hname}"] = p
            for pn in ("A", "B"):
                sel = per == pn
                st = oos_stats(p[sel], r[sel], T[sel], h)
                R["oos"].append(dict(h=hname, model=model, period=pn, **st))
                for th in THETAS:
                    R["econ"].append(dict(h=hname, model=model, period=pn, theta=th,
                                          **econ_stats(trades(p[sel], up[sel], T[sel], h, th))))
            selB = per == "B"
            R["cal"][(hname, model)] = calibration(p[selB], r[selB], T[selB], h)
            if model == "ridge":
                bf = [m.coef for b0, m in fits if b0 >= ts(PERIODS["B"][0])]
                cm = np.mean(bf, axis=0)
                R["coef"][hname] = pd.DataFrame({"factor": FACTORS, "coef": cm,
                                                 "same_sign": np.mean(np.sign(bf) == np.sign(cm), axis=0)})
            else:
                R["perm"][hname] = permutation(X, up, T, h, per)
            log(f"  {hname} {model}: alpha {a:g}, B {R['oos'][-1]} ({time.time() - t0:.0f}s)")
    R["oos"] = pd.DataFrame(R["oos"])
    R["econ"] = pd.DataFrame(R["econ"])
    R["preds"] = preds
    # kacho (5 m, A out-of-sample)
    k = kacho_open(kacho)
    R["kacho_books"] = dict(markets=len(k), with_row=int(k["au"].notna().sum()),
                            au_med=float(k["au"].median()), ad_med=float(k["ad"].median()),
                            au_mean=float(k["au"].mean()), ad_mean=float(k["ad"].mean()),
                            sum_mean=float((k["au"] + k["ad"]).mean()))
    kk = []
    rb = up_of(df["r_5m"].to_numpy())
    bin_up = pd.Series(rb, index=df.index)
    for model in MODELS:
        p_at = preds[f"{model}_5m"]
        for th in THETAS:
            t = kacho_trades(k, p_at, th)
            ok = t["ask"].notna()
            st = econ_stats(t[ok][["T", "won", "pnl"]])
            st051 = econ_stats(t[["T", "won", "pnl_051"]].rename(columns={"pnl_051": "pnl"}))
            side_ask = t.loc[ok, "ask"]
            agree = float(np.mean(bin_up.reindex(t["T"]).to_numpy() == np.where(t["side_up"], t["won"], 1 - t["won"])))
            kk.append(dict(model=model, theta=th, n_signal=len(t), n=st["n"], ev=st["ev"], se=st["se"], p1=st["p1"],
                           win=st["win"], ev051=st051["ev"], se051=st051["se"], ask_mean=float(side_ask.mean()),
                           ask_med=float(side_ask.median()), ask_p10=float(side_ask.quantile(0.1)),
                           ask_p90=float(side_ask.quantile(0.9)), gap=float(side_ask.mean() - PRICE), agree=agree))
    R["kacho"] = pd.DataFrame(kk)
    mid = (k["au"] + (1 - k["ad"])) / 2
    R["kacho_mid_rho"] = {}
    for model in MODELS:
        pk = preds[f"{model}_5m"].reindex(k["S"]).to_numpy()
        ok = np.isfinite(pk) & np.isfinite(mid.to_numpy())
        rho = stats.spearmanr(pk[ok], mid.to_numpy()[ok]).statistic
        # the model's edge over the opening mid: Spearman(P(up) - mid, official up)
        ex = stats.spearmanr(pk[ok] - mid.to_numpy()[ok], k["up_won"].astype(float).to_numpy()[ok]).statistic
        raw = stats.spearmanr(pk[ok], k["up_won"].astype(float).to_numpy()[ok]).statistic
        mrho = stats.spearmanr(mid.to_numpy()[ok], k["up_won"].astype(float).to_numpy()[ok]).statistic
        R["kacho_mid_rho"][model] = dict(rho_mid=float(rho), rho_up=float(raw), rho_mid_up=float(mrho),
                                         rho_excess_up=float(ex), n=int(ok.sum()))
    m_k = np.isin(T, k["S"].to_numpy()) & np.isfinite(rb)
    kb = k.set_index("S")["up_won"].reindex(T[m_k]).astype(float).to_numpy()
    R["kacho_agree_all"] = float(np.mean(kb == rb[m_k]))
    log(f"kacho done ({time.time() - t0:.0f}s)")
    R["robust"] = robustness(df, R["alpha"])
    log(f"robustness done ({time.time() - t0:.0f}s)")
    # hypothesis checks
    R["h_oi"] = hyp_oi_price(df)
    R["h_liq"] = hyp_liq(df)
    R["h_gamma"] = hyp_gamma(df)
    R["h_pin"] = hyp_pin(df)
    R["h_fund"] = hyp_funding(df)
    # freeze
    R["frozen"] = freeze(R, cache)
    with open(Path(cache) / "factors_study.pkl", "wb") as fh:
        pickle.dump(R, fh)
    log(f"study done ({time.time() - t0:.0f}s)")
    log(json.dumps(R["frozen"], indent=1, ensure_ascii=False))
    return R


def robustness(df, alphas):
    """Ridge walk-forward on A + B with feature subsets: all but the 5-minute-metrics factors (their
    timing has no slack), and spot-only (1 s klines / spot 1 m only). B IC and theta = 0.04 EV."""
    T = df.index.to_numpy()
    per = period_of(T)
    rows = []
    for name, feats in (("全部 55 个", FACTORS), ("去掉 5 分钟指标（12 个）", [f for f in FACTORS if f not in METRICS_FEATURES]),
                        ("只用现货（21 个）", SPOT_FEATURES)):
        X = df[feats].to_numpy(float)
        for hname, h in HORIZONS[:2]:
            r = df[f"r_{hname}"].to_numpy()
            up = up_of(r)
            p, _ = walk_forward(X, up, T, h, "ridge", blocks(["A", "B"]), alpha=alphas[hname])
            sel = per == "B"
            st = oos_stats(p[sel], r[sel], T[sel], h)
            ec = econ_stats(trades(p[sel], up[sel], T[sel], h, 0.04))
            rows.append(dict(features=name, h=hname, ic=st["ic"], t=st["t"], ev=ec["ev"], se=ec["se"], n=ec["n"]))
    return pd.DataFrame(rows)


def permutation(X, up, T, h_min, per, n_repeats=5):
    """HGB fit on all of A (targets ended before B), permutation importance (ROC AUC drop) on B's
    5-minute rows."""
    from sklearn.inspection import permutation_importance
    t0, b0 = ts(TRAIN_START), ts(PERIODS["B"][0])
    tr = (T >= t0) & (T + h_min * 60 <= b0) & np.isfinite(up)
    te = (per == "B") & np.isfinite(up)
    m = HGB().fit(X[tr], up[tr])
    r = permutation_importance(m.m, X[te], up[te].astype(int), scoring="roc_auc", n_repeats=n_repeats, random_state=0)
    from sklearn.metrics import roc_auc_score
    auc = roc_auc_score(up[te].astype(int), m.predict_proba1(X[te]))
    d = pd.DataFrame({"factor": FACTORS, "drop": r.importances_mean, "sd": r.importances_std, "auc": float(auc)})
    return d.sort_values("drop", ascending=False).reset_index(drop=True)


def freeze(R, cache=CACHE):
    """Per horizon: the (model, theta) with the highest B walk-forward EV among those with >= 300
    B trades; if none has 300 (4 h: only 276 windows in B), the highest B EV among those with >= 100,
    flagged as unable to pass."""
    e = R["econ"]
    spec = {"made": pd.Timestamp.now(tz="UTC").isoformat(), "rule": "highest B walk-forward EV with >= 300 B trades",
            "price": PRICE, "fee": f"{FEE} p (1 - p)", "train_start": TRAIN_START, "block_days": BLOCK_DAYS,
            "refit": "walk-forward: each C block predicted by a model fit on rows T >= train_start with T + h <= block start",
            "features": FACTORS, "hgb_params": HGB_PARAMS, "horizons": {}}
    for hname, h in HORIZONS:
        b = e[(e["h"] == hname) & (e["period"] == "B")].copy()
        ok = b[b["n"] >= MIN_TRADES]
        meets = len(ok) > 0
        fb = b[b["n"] >= MIN_TRADES_FALLBACK]
        pick = (ok if meets else fb if len(fb) else b.sort_values("n", ascending=False).head(1)).sort_values("ev", ascending=False).iloc[0]
        spec["horizons"][hname] = dict(model=pick["model"], theta=float(pick["theta"]),
                                       alpha=float(R["alpha"][hname]) if pick["model"] == "ridge" else None,
                                       meets_rule=bool(meets), B_ev=float(pick["ev"]), B_se=float(pick["se"]),
                                       B_n=int(pick["n"]), max_windows_C=int(46 * 1440 // h),
                                       note="" if meets else f"no candidate reached {MIN_TRADES} B trades; highest B EV among "
                                                             f"those with >= {MIN_TRADES_FALLBACK}; C has fewer than "
                                                             f"{MIN_TRADES} windows, so it cannot pass")
    with open(Path(cache) / "frozen.json", "w") as fh:
        json.dump(spec, fh, indent=1, ensure_ascii=False)
    return spec


def lockbox(cache=CACHE, klines=KLINES, force=False, log=print):
    out = Path(cache) / "lockbox.json"
    if out.exists() and not force:
        log(f"{out} exists; the lock box is evaluated once (use --force to recompute)")
        return json.loads(out.read_text())
    spec = json.loads((Path(cache) / "frozen.json").read_text())
    assert spec["features"] == FACTORS, "feature list changed since the freeze"
    df = load_frame(cache, klines)
    T = df.index.to_numpy()
    per = period_of(T)
    X = df[spec["features"]].to_numpy(float)
    res = {"frozen_made": spec["made"], "run": pd.Timestamp.now(tz="UTC").isoformat(), "horizons": {}}
    for hname, h in HORIZONS:
        s = spec["horizons"][hname]
        r = df[f"r_{hname}"].to_numpy()
        up = up_of(r)
        p, _ = walk_forward(X, up, T, h, s["model"], blocks(["C"]), alpha=s["alpha"])
        sel = per == "C"
        t = trades(p[sel], up[sel], T[sel], h, s["theta"])
        st = econ_stats(t)
        oo = oos_stats(p[sel], r[sel], T[sel], h)
        passed = bool(st["n"] >= MIN_TRADES and st["ev"] > 0 and st["p1"] < 0.05)
        res["horizons"][hname] = dict(model=s["model"], theta=s["theta"], ev=st["ev"], se=st["se"], n=st["n"],
                                      days=st["days"], p1=st["p1"], win=st["win"], passed=passed,
                                      ic=oo["ic"], ic_t=oo["t"], acc=oo["acc"], up_rate=oo["up_rate"])
        log(f"C {hname}: {res['horizons'][hname]}")
    out.write_text(json.dumps(res, indent=1, ensure_ascii=False))
    return res


# ---------------------------------------------------------------- report

def _f(x, d=3, sign=True):
    if x is None or not np.isfinite(x):
        return "–"
    return f"{x:+.{d}f}" if sign else f"{x:.{d}f}"


def report(cache=CACHE, out="real/factors.md"):
    with open(Path(cache) / "factors_study.pkl", "rb") as fh:
        R = pickle.load(fh)
    lb_path = Path(cache) / "lockbox.json"
    LB = json.loads(lb_path.read_text()) if lb_path.exists() else None
    ic = R["ic"]
    mtests = len(FACTORS) * len(HORIZONS)
    tcrit = stats.norm.isf(0.05 / mtests / 2)
    L = ["# BTC 自身趋势的多因子检验（FACTORS.md 的执行结果）", "",
         f"设计见 FACTORS.md（跑之前写定）。代码 `factors.py`，测试 `test_factors.py`。数据：币安现货 1 秒 K 线、永续 1 分钟 K 线/溢价指数/5 分钟指标/资金费率、"
         f"Deribit DVOL 和期权逐笔（7 天做市商 gamma 代理）、kacho.io 5 分钟盘口。决策时刻每 5 分钟；A 3/24–6/30，B 7/1–8/15，C 8/16–9/30（锁箱）。",
         "", "## 和 FACTORS.md 不完全一样的地方", "",
         "| 项目 | 做法 |", "|---|---|",
         "| DVOL | 1 分钟 DVOL 4/1 07:16 才开始；之前和缺口用小时 DVOL（整点收盘后才算已知） |",
         "| 5 分钟指标 | 时间戳 c 的那一行从 c + 5 分钟起可用（正好是 FACTORS.md 的规则，没有给币安发布延迟留余量） |",
         "| 净主动成交量 | 现货、永续各 1/5/15/60 分钟的（主动买 − 主动卖），单位 BTC |",
         "| 钉住代理 | 按原文：离最近 1000/5000 行权价的距离（%）× 到周五 08:00 的小时数（无方向）；带方向的“向行权价靠拢”只在假设检验里用 |",
         "| 爆仓代理 | max(0, −z(15 分钟持仓变化)) × z(15 分钟收益)，z 相对过去 7 天；事件 = z(持仓) ≤ −2 且 z(收益) 的绝对值 ≥ 2，60 分钟内只取第一次 |",
         "| 岭回归 | 对“涨”的 0/1 做线性概率岭回归；α 在 A 内用同样的滚动方式按 Brier 选（所以岭回归的 A 样本外对 α 不是干净的，B、C 是） |",
         "| HGB | 参数事先固定、不调：深度 3、8 叶、每叶 ≥ 400、学习率 0.05、150 轮、L2 = 1 |",
         "| 滚动 | 每段从第一天起每 7 天一块；每块用 3/24 起、目标在块开始前已结束的全部 5 分钟样本重拟合；A 前 14 天只训练，样本外从 4/7 起 |",
         "| NW 滞后 | 不重叠网格用 floor(4(n/100)^(2/9))；5 分钟重叠网格用 h/5 − 1 |",
         "| C 隔离 | A + B 阶段把所有在 8/16 00:00 之后才结束的目标置空 |",
         "| 4 小时 | B、C 各只有 276 个不重叠的 4 小时窗口，到不了 300 笔：在 B 至少 100 笔的组合里按每份最高冻结（跑 C 之前定），注定不能“通过” |",
         "| kacho | 只有 4/7–5/18 有样本外预测（3/24–4/6 是训练期） |",
         "| 涨跌 | 涨 = 收益 ≥ 0（Polymarket 的规则）；币安现货是 Chainlink 价格的代理 |", ""]
    # univariate
    L += ["## 1. 单因子 Spearman IC（不重叠网格，NW t）", "",
          f"共 {len(FACTORS)} 个因子 × 4 个 h = {mtests} 个检验，Bonferroni 门槛 |t| > {tcrit:.2f}（双侧 0.05/{mtests}）。"
          "格内为 A 的 IC（t）/ B 的 IC（t）；** = A、B 都过门槛且同号。全表（含重叠网格）在 `real/factors-ic.csv`。", "",
          "| 族 | 因子 | " + " | ".join(h for h, _ in HORIZONS) + " |", "|---|---|" + "---|" * len(HORIZONS)]
    main = ic[ic["grid"] == "main"].set_index(["factor", "h", "period"])
    good = consistent(ic, "main")
    good_ov = consistent(ic, "overlap")
    for f in FACTORS:
        cells = []
        for h, _ in HORIZONS:
            a, b = main.loc[(f, h, "A")], main.loc[(f, h, "B")]
            star = " **" if (f, h) in good.index else ""
            cells.append(f"{_f(a.ic)} ({_f(a.t, 1)}) / {_f(b.ic)} ({_f(b.t, 1)}){star}")
        L.append(f"| {FAMILY[f]} | {f} | " + " | ".join(cells) + " |")
    L += ["", "A、B 都显著且同号（不重叠网格）：" + ("、".join(f"{f}@{h}" for f, h in good.index) if len(good) else "**没有**"),
          "", "A、B 都显著且同号（5 分钟重叠网格，NW 滞后 h/5 − 1）：" + ("、".join(f"{f}@{h}" for f, h in good_ov.index) if len(good_ov) else "**没有**"), ""]
    # also: how many pass in A alone and B alone
    nA = int(((main.xs("A", level="period")["p"]) < 0.05 / mtests).sum())
    nB = int(((main.xs("B", level="period")["p"]) < 0.05 / mtests).sum())
    L += [f"单看一段：A 段过门槛的有 {nA} 个，B 段 {nB} 个（因子 × h，不论另一段）。", ""]
    # multi-factor
    o = R["oos"]
    L += ["## 2. 多因子（每周滚动，样本外）", "",
          "| h | 模型 | α | A 样本外 IC (t) | B IC (t) | B 重叠网格 IC (t) | B 方向准确率 | B 上涨占比 | B 预测概率的标准差 |",
          "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for h, _ in HORIZONS:
        for model in MODELS:
            a = o[(o.h == h) & (o.model == model) & (o.period == "A")].iloc[0]
            b = o[(o.h == h) & (o.model == model) & (o.period == "B")].iloc[0]
            al = f"{R['alpha'][h]:g}" if model == "ridge" else "–"
            L.append(f"| {h} | {model} | {al} | {_f(a.ic)} ({_f(a.t, 1)}) | {_f(b.ic)} ({_f(b.t, 1)}) | {_f(b.ic_ov)} ({_f(b.t_ov, 1)}) | "
                     f"{100 * b.acc:.1f}% ±{100 * b.acc_se:.1f} | {100 * b.up_rate:.1f}% | {b.p_sd:.3f} |")
    L += ["", "### B 段概率校准（不重叠网格；格内：平均预测 / 实际上涨比例 / 样本数）", "",
          "| P(涨) 区间 | " + " | ".join(f"{h} {m}" for h, _ in HORIZONS for m in MODELS) + " |",
          "|---|" + "---|" * (len(HORIZONS) * len(MODELS))]
    bins = R["cal"][(HORIZONS[0][0], MODELS[0])]["bin"].tolist()
    for i, bn in enumerate(bins):
        cells = []
        for h, _ in HORIZONS:
            for m in MODELS:
                c = R["cal"][(h, m)].iloc[i]
                cells.append(f"{_f(c.p_mean, 3, False)} / {_f(c.up_rate, 3, False)} / {c.n}" if c.n else "–")
        L.append(f"| {bn} | " + " | ".join(cells) + " |")
    L += ["", "### 重要性", "",
          "| h | 岭回归 B 各次重拟合平均系数（每标准差的概率，前 6，括号内为同号比例） | HGB 置换重要性（A 训练、B 上 AUC 下降 ×1000，前 6） |",
          "|---|---|---|"]
    for h, _ in HORIZONS:
        c = R["coef"][h].reindex(R["coef"][h]["coef"].abs().sort_values(ascending=False).index).head(6)
        pr = R["perm"][h].head(6)
        L.append(f"| {h} | " + "，".join(f"{r.factor} {100 * r.coef:+.2f}% ({r.same_sign:.0%})" for r in c.itertuples())
                 + f" | AUC {R['perm'][h]['auc'].iloc[0]:.3f}：" + "，".join(f"{r.factor} {1000 * r.drop:.1f}±{1000 * r.sd:.1f}" for r in pr.itertuples()) + " |")
    L += ["", "### 稳健性：岭回归换特征子集（B 样本外；θ = 0.04、0.51 吃单）", "",
          "| 特征 | h | B IC (t) | B 每份（笔） |", "|---|---|---:|---:|"]
    for r in R["robust"].itertuples():
        L.append(f"| {r.features} | {r.h} | {_f(r.ic)} ({_f(r.t, 1)}) | {_f(100 * r.ev, 2)}¢ ±{_f(100 * r.se, 2, False)}（{r.n}） |")
    # economics
    e = R["econ"]
    L += ["", "## 3. 换成 Polymarket 的钱（0.51 吃单 + 0.07·p(1−p) 手续费，盈亏平衡胜率 52.75%）", "",
          "每份 ¢ ± 按天聚类标准误（笔数）。B 是干净的样本外；A 样本外对岭回归的 α 不干净。", "",
          "| h | 模型 | θ | A 样本外 | B 样本外 | B 胜率 |", "|---|---|---:|---:|---:|---:|"]
    for h, _ in HORIZONS:
        for model in MODELS:
            for th in THETAS:
                a = e[(e.h == h) & (e.model == model) & (e.period == "A") & (e.theta == th)].iloc[0]
                b = e[(e.h == h) & (e.model == model) & (e.period == "B") & (e.theta == th)].iloc[0]
                L.append(f"| {h} | {model} | {th} | {_f(100 * a.ev, 2)} ±{_f(100 * a.se, 2, False)}（{a.n}） | "
                         f"{_f(100 * b.ev, 2)} ±{_f(100 * b.se, 2, False)}（{b.n}） | {_f(100 * b.win, 1, False)}% |")
    kb = R["kacho_books"]
    L += ["", "### 5 分钟在 kacho 真实盘口上（4/7–5/18，样本外；开盘那一行的卖一，≥ 5 份，官方结算）", "",
          f"kacho 共 {kb['markets']:,} 个市场，{kb['with_row']:,} 个有开盘那一秒的盘口。开盘卖一中位数：涨 {kb['au_med']:.3f}、跌 {kb['ad_med']:.3f}；"
          f"均值：涨 {kb['au_mean']:.3f}、跌 {kb['ad_mean']:.3f}，两边相加平均 {kb['sum_mean']:.3f}。币安现货方向和官方结算一致的比例：{100 * R['kacho_agree_all']:.1f}%。", "",
          "| 模型 | θ | 信号数 | 可成交 | 真实卖一每份 | 同样信号按 0.51 每份 | 胜率 | 所买一边的卖一：均值 / 中位 / 10%–90% | 比 0.51 高 |",
          "|---|---:|---:|---:|---:|---:|---:|---|---:|"]
    for r in R["kacho"].itertuples():
        L.append(f"| {r.model} | {r.theta} | {r.n_signal} | {r.n} | {_f(100 * r.ev, 2)} ±{_f(100 * r.se, 2, False)} | "
                 f"{_f(100 * r.ev051, 2)} ±{_f(100 * r.se051, 2, False)} | {_f(100 * r.win, 1, False)}% | "
                 f"{_f(r.ask_mean, 3, False)} / {_f(r.ask_med, 3, False)} / {_f(r.ask_p10, 2, False)}–{_f(r.ask_p90, 2, False)} | {_f(100 * r.gap, 1)}¢ |")
    L += ["", "盘口是否已经计入模型的信号（kacho 4/7–5/18 全部市场，Spearman）：", "",
          "| 模型 | P(涨) 与开盘中间价 | P(涨) 与官方结果 | 开盘中间价与官方结果 | (P(涨) − 中间价) 与官方结果 | n |", "|---|---:|---:|---:|---:|---:|"]
    for model, d in R["kacho_mid_rho"].items():
        L.append(f"| {model} | {_f(d['rho_mid'])} | {_f(d['rho_up'])} | {_f(d['rho_mid_up'])} | {_f(d['rho_excess_up'])} | {d['n']:,} |")
    # frozen + lockbox
    fz = R["frozen"]
    L += ["", "## 4. 冻结（只用 A + B）和锁箱 C（只算一次）", "",
          "规则：每个 h 在 B 滚动样本外每份最高、且 B 至少 300 笔的（模型, θ）。冻结文件 `frozen.json`。", "",
          "| h | 冻结 | 合规则 | B 每份（笔） | C 每份 ± SE | C 笔数 | C 单侧 p | C 胜率 | C IC (t) | 通过？ |",
          "|---|---|---|---:|---:|---:|---:|---:|---:|---|"]
    for h, _ in HORIZONS:
        s = fz["horizons"][h]
        name = f"{s['model']}，θ = {s['theta']}" + (f"，α = {s['alpha']:g}" if s["alpha"] else "")
        c = LB["horizons"][h] if LB else None
        if c:
            L.append(f"| {h} | {name} | {'是' if s['meets_rule'] else '否（不足 300 笔）'} | {_f(100 * s['B_ev'], 2)}¢（{s['B_n']}） | "
                     f"{_f(100 * c['ev'], 2)}¢ ±{_f(100 * c['se'], 2, False)} | {c['n']} | {_f(c['p1'], 3, False)} | {_f(100 * c['win'], 1, False)}% | "
                     f"{_f(c['ic'])} ({_f(c['ic_t'], 1)}) | {'**通过**' if c['passed'] else '不通过'} |")
        else:
            L.append(f"| {h} | {name} | {'是' if s['meets_rule'] else '否'} | {_f(100 * s['B_ev'], 2)}¢（{s['B_n']}） | 未跑 | | | | | |")
    # hypotheses
    vlines, vres = verdicts(R, main)
    L += ["", "## 5. 事先写下的假设", ""] + vlines + ["", "### 持仓量 × 价格（过去 1 小时同向/反向之后，朝过去 1 小时方向的延续，bp）", "",
          "| h | 段 | 同向 | 反向 | 差（同 − 反） |", "|---|---|---:|---:|---:|"]
    for r in R["h_oi"].itertuples():
        L.append(f"| {r.h} | {r.period} | {_f(r.same, 2)} ±{_f(r.same_se, 2, False)}（{r.same_n}） | {_f(r.opp, 2)} ±{_f(r.opp_se, 2, False)}（{r.opp_n}） | "
                 f"{_f(r.diff, 2)} ±{_f(r.diff_se, 2, False)} |")
    L += ["", "### 持仓骤降 + 价格大动（爆仓代理）之后，朝那次大动方向的收益（bp）", "",
          "| 段 | 事件数 | " + " | ".join(f"+{h}" for h, _ in HORIZONS) + " |", "|---|---:|" + "---:|" * len(HORIZONS)]
    for r in R["h_liq"].itertuples(index=False):
        d = r._asdict()
        L.append(f"| {d['period']} | {d['n']} | " + " | ".join(f"{_f(d[f'm_{h}'], 1)} ±{_f(d[f'se_{h}'], 1, False)}" for h, _ in HORIZONS) + " |")
    L += ["", "### 主动买卖失衡（单因子 IC，不重叠网格）", "", "| 因子 | 5m A | 5m B | 15m A | 15m B |", "|---|---:|---:|---:|---:|"]
    for f in ("spot_tbr_1m", "spot_tbr_5m", "fut_tbr_1m", "fut_tbr_5m", "spot_ntv_5m", "fut_ntv_5m", "taker_ls_5m"):
        cells = [f"{_f(main.loc[(f, h, p)].ic)} ({_f(main.loc[(f, h, p)].t, 1)})" for h in ("5m", "15m") for p in ("A", "B")]
        L.append(f"| {f} | " + " | ".join(cells) + " |")
    mom, q, rv = R["h_gamma"]
    L += ["", "### 做市商 gamma × 动量（小时网格；gz = gamma / A 段标准差）", "",
          "假设：gamma 越负动量越强 → 交互项为负；gamma < 0 时的延续 > gamma ≥ 0 时。", "",
          "| 段 | h（过去收益窗口） | 动量系数 | 动量 × gz | gamma < 0 时延续 bp（n） | gamma ≥ 0 时延续 bp（n） |", "|---|---|---:|---:|---:|---:|"]
    for r in mom.itertuples():
        past = "1h（过去 1h）" if r.h == "1h" else "15m（过去 15m）"
        L.append(f"| {r.period} | {past} | {_f(r.mom, 4)} ±{_f(r.mom_se, 4, False)} | {_f(r.mom_x_gz, 4)} ±{_f(r.mom_x_gz_se, 4, False)} | "
                 f"{_f(r.cont_neg, 1)} ±{_f(r.cont_neg_se, 1, False)}（{r.n_neg}） | {_f(r.cont_pos, 1)} ±{_f(r.cont_pos_se, 1, False)}（{r.n_pos}） |")
    L += ["", "| gamma 五分位（A 切点） | A 延续 bp | A 下一小时 RV | B 延续 bp | B 下一小时 RV |", "|---|---:|---:|---:|---:|"]
    for k in range(1, 6):
        a, b = q[(q.period == "A") & (q.q == k)].iloc[0], q[(q.period == "B") & (q.q == k)].iloc[0]
        L.append(f"| Q{k} | {_f(a.cont, 1)} ±{_f(a.cont_se, 1, False)} | {_f(a.rv_fwd, 3, False)} | {_f(b.cont, 1)} ±{_f(b.cont_se, 1, False)} | {_f(b.rv_fwd, 3, False)} |")
    L += ["", "gamma 和下一小时已实现波动的 Spearman（假设：负相关）。控制 = 过去 1 小时 RV、DVOL、小时哑变量：", "",
          "| 段 | gamma | 原始 (t) | 控制后 (t) | 和过去 1 小时 RV | n |", "|---|---|---:|---:|---:|---:|"]
    for r in rv.itertuples():
        L.append(f"| {r.period} | {r.gamma} | {_f(r.raw)} ({_f(r.raw_t, 1)}) | {_f(r.ctl)} ({_f(r.ctl_t, 1)}) | {_f(r.past)} | {r.n} |")
    L += ["", "### 钉住效应（小时网格；下一小时朝最近 1000 整数行权价方向的收益 bp，+ = 靠拢）", "",
          "| 段 | 组 | 靠拢 bp（n） | 下一小时 RV：离行权价 ≤ 0.2% | > 0.2% |", "|---|---|---:|---:|---:|"]
    for r in R["h_pin"].itertuples():
        L.append(f"| {r.period} | {r.group} | {_f(r.pull, 1)} ±{_f(r.pull_se, 1, False)}（{r.n}） | {_f(r.rv_close, 3, False)} | {_f(r.rv_far, 3, False)} |")
    L += ["", "### 资金费率极端（4 小时网格；分位切点来自 A；24 小时按周聚类）", "",
          "| 段 | 资金费率 | 之后 4 小时 bp | 之后 24 小时 bp | n |", "|---|---|---:|---:|---:|"]
    for r in R["h_fund"].itertuples():
        L.append(f"| {r.period} | {r.group} | {_f(r.r4h, 1)} ±{_f(r.r4h_se, 1, False)} | {_f(r.r24h, 1)} ±{_f(r.r24h_se, 1, False)} | {r.n} |")
    L += ["", "## 6. 结论", ""] + conclusions(R, LB, good, good_ov, vres)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text("\n".join(L) + "\n", encoding="utf-8")
    ic.to_csv(Path(out).parent / "factors-ic.csv", index=False, float_format="%.5g")
    return "\n".join(L)


def _verdict(a, b, sign):
    """a, b = (estimate, t) in A and B; sign = the hypothesis' sign. 成立 needs both |t| >= 1.96 with
    that sign; 相反 both with the other sign."""
    if all(np.isfinite(x[1]) and abs(x[1]) >= 1.96 and np.sign(x[0]) == sign for x in (a, b)):
        return "成立"
    if all(np.isfinite(x[1]) and abs(x[1]) >= 1.96 and np.sign(x[0]) == -sign for x in (a, b)):
        return "**相反**"
    return "不成立"


def verdicts(R, main):
    """One row per FACTORS.md hypothesis: the pre-registered test statistic in A and B and a verdict."""
    rows = []

    def t_(m, se):
        return (m, m / se if se and np.isfinite(se) and se > 0 else np.nan)

    o = R["h_oi"].set_index(["h", "period"])
    for h in ("5m", "15m", "1h"):
        a, b = o.loc[(h, "A")], o.loc[(h, "B")]
        rows.append((f"持仓与价格同向 → {h} 内延续更强（同向 − 反向，bp）", t_(a["diff"], a.diff_se), t_(b["diff"], b.diff_se), 1))
    lq = R["h_liq"].set_index("period")
    for h, sg, txt in (("5m", 1, "先同向"), ("15m", 1, "先同向"), ("4h", -1, "随后回吐")):
        rows.append((f"爆仓代理之后 +{h}：{txt}（bp）", t_(lq.loc["A", f"m_{h}"], lq.loc["A", f"se_{h}"]),
                     t_(lq.loc["B", f"m_{h}"], lq.loc["B", f"se_{h}"]), sg))
    for f in ("spot_tbr_5m", "fut_tbr_5m"):
        a, b = main.loc[(f, "5m", "A")], main.loc[(f, "5m", "B")]
        rows.append((f"主动买卖失衡 {f} → 下 5 分钟同向（IC）", (a.ic, a.t), (b.ic, b.t), 1))
    mom, _, rv = R["h_gamma"]
    for h in ("1h", "15m"):
        a, b = mom[(mom.period == "A") & (mom.h == h)].iloc[0], mom[(mom.period == "B") & (mom.h == h)].iloc[0]
        rows.append((f"gamma 越负动量越强（{h}，动量 × gz 系数）", t_(a.mom_x_gz, a.mom_x_gz_se), t_(b.mom_x_gz, b.mom_x_gz_se), -1))
    for g in ("dealer_gamma", "dealer_gamma_7d"):
        a, b = rv[(rv.period == "A") & (rv.gamma == g)].iloc[0], rv[(rv.period == "B") & (rv.gamma == g)].iloc[0]
        rows.append((f"{g} 越负下一小时波动越大（控制后 Spearman）", (a.ctl, a.ctl_t), (b.ctl, b.ctl_t), -1))
    pn = R["h_pin"].set_index(["period", "group"])
    for gname in ("周五到期前 24 小时，gamma > 0", "周五到期前 24 小时，全部"):
        a, b = pn.loc[("A", gname)], pn.loc[("B", gname)]
        rows.append((f"钉住：{gname}，下一小时向 1000 行权价靠拢（bp）", t_(a.pull, a.pull_se), t_(b.pull, b.pull_se), 1))
    fu = R["h_fund"]
    for prefix, sg, txt in (("最高", -1, "资金费率最高 10% → 4 小时反转（bp）"), ("最低", 1, "资金费率最低 10% → 4 小时反转（bp）")):
        a = fu[(fu.period == "A") & fu.group.str.startswith(prefix)].iloc[0]
        b = fu[(fu.period == "B") & fu.group.str.startswith(prefix)].iloc[0]
        rows.append((txt, t_(a.r4h, a.r4h_se), t_(b.r4h, b.r4h_se), sg))
    L = ["判定：A、B 两段都 |t| ≥ 1.96 且符号和假设一致才算“成立”；两段都显著但符号相反记“相反”。", "",
         "| 假设 | 预期符号 | A 估计 (t) | B 估计 (t) | 判定 |", "|---|---:|---:|---:|---|"]
    out = []
    for name, a, b, sg in rows:
        v = _verdict(a, b, sg)
        out.append((name, v))
        L.append(f"| {name} | {'+' if sg > 0 else '−'} | {_f(a[0], 3)} ({_f(a[1], 1)}) | {_f(b[0], 3)} ({_f(b[1], 1)}) | {v} |")
    return L, out


def conclusions(R, LB, good, good_ov, vres=()):
    """Plain statements generated from the numbers."""
    L = []
    ic = R["ic"]
    neg = [f"{f}@{h}" for f, h in good.index if ic[(ic.grid == "main") & (ic.factor == f) & (ic.h == h) & (ic.period == "A")].ic.iloc[0] < 0]
    L.append(f"- 单因子：A、B 都过 Bonferroni 且同号的（不重叠网格）{len(good)} 个："
             + ("、".join(f"{f}@{h}" for f, h in good.index) if len(good) else "无")
             + (f"；其中 IC 为负（反转）的 {len(neg)} 个" if len(good) else "") + "。"
             + "没有一个过的族：" + "、".join(fam for fam, fs in FAMILIES if not any(f in fs for f, _ in good.index)) + "。")
    o = R["oos"]
    b = o[o.period == "B"]
    cells = "；".join(f"{r.h} {r.model} {r.ic:+.3f}（t {r.t:+.1f}）" for r in b.itertuples())
    L.append(f"- 多因子 B 样本外 IC：{cells}。5 分钟、15 分钟的排序能力在 A、B 都稳定，1 小时、4 小时不稳定。")
    L.append("- 方向：模型主要学到的是“过去几分钟涨、主动买多 → 接下来 5–15 分钟回落”的短期反转，和“趋势延续”的设想相反。")
    e = R["econ"]
    eb = e[(e.period == "B") & (e.n >= 1)]
    pos = eb[(eb.ev > 0) & (eb.ev / eb.se > 1.645)]
    L.append(f"- 0.51 吃单：B 段 24 个组合里每份为正且单侧显著（t > 1.645）的 {len(pos)} 个"
             + ("：" + "、".join(f"{r.h} {r.model} θ{r.theta}（{100 * r.ev:+.2f}¢，{r.n} 笔）" for r in pos.itertuples()) if len(pos) else "") + "。")
    k = R["kacho"]
    km = R["kacho_mid_rho"]["ridge"]
    L.append(f"- 但 0.51 不是真实价格：kacho 上模型所选一边的开盘卖一平均比 0.51 高 {100 * k.gap.min():.1f}–{100 * k.gap.max():.1f}¢，"
             f"岭回归的 P(涨) 和开盘中间价的秩相关 {km['rho_mid']:+.2f}，开盘中间价本身对结果的秩相关（{km['rho_mid_up']:+.3f}）比模型（{km['rho_up']:+.3f}）还高。"
             f"也就是 Polymarket 开盘报价已经计入了这个反转；按真实卖一，每份 {100 * k.ev.min():+.2f} 到 {100 * k.ev.max():+.2f}¢，"
             f"单侧显著为正（t > 1.645）的 {int(((k.ev / k.se) > 1.645).sum())} 个（共 {len(k)} 个）。")
    if LB:
        ps = [h for h, c in LB["horizons"].items() if c["passed"]]
        c5, c15 = LB["horizons"]["5m"], LB["horizons"]["15m"]
        L.append("- 锁箱 C（只算一次）：" + ("通过的：" + "、".join(ps) if ps else "**没有一个 h 通过**")
                 + f"。5 分钟的排序能力还在（C IC {c5['ic']:+.3f}，t {c5['ic_t']:+.1f}），但 0.51 下每份只有 {100 * c5['ev']:+.2f}¢（p {c5['p1']:.2f}）；"
                 f"15 分钟 {100 * c15['ev']:+.2f}¢（{c15['n']} 笔，p {c15['p1']:.3f}），差一点但不过；1 小时、4 小时为负。")
    held = [n for n, v in vres if v == "成立"]
    flipped = [n for n, v in vres if "相反" in v]
    L.append(f"- 事先写下的假设（{len(vres)} 个检验）：成立的 {len(held)} 个" + ("（" + "；".join(held) + "）" if held else "")
             + f"；两段都显著但方向相反的 {len(flipped)} 个" + ("（" + "；".join(flipped) + "）" if flipped else "")
             + "；其余不显著或 A、B 不一致。gamma 和下一小时波动：A 段控制后约为 0，B 段为正（与假设相反）。")
    return L


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=("study", "lockbox", "report", "all"))
    ap.add_argument("--cache", default=str(CACHE))
    ap.add_argument("--klines", default=str(KLINES))
    ap.add_argument("--kacho", default=str(KACHO))
    ap.add_argument("--out", default="real/factors.md")
    ap.add_argument("--rebuild", action="store_true", help="recompute the feature cache")
    ap.add_argument("--force", action="store_true", help="recompute the lock box even if it was evaluated")
    a = ap.parse_args(argv)
    if a.cmd in ("study", "all"):
        study(a.cache, a.klines, a.kacho, a.rebuild)
    if a.cmd in ("lockbox", "all"):
        lockbox(a.cache, a.klines, a.force)
    if a.cmd in ("report", "all"):
        print(report(a.cache, a.out))


if __name__ == "__main__":
    main()
