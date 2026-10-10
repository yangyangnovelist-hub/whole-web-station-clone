"""REGIME.md, the learning and the judging: from one row per BTC 5m window with each base strategy's
actual P&L in that window and the environment known at the window's open, learn when to switch
each strategy on, freeze the rule on A, then confirm it on B and (once) on C. Paper research on
market data only (no orders, no keys); it reads only the windows table.

    python regime_learn.py [--windows real/cross-regime-windows.csv.gz] [--out real/regime-learn.md]
                           [--frozen real/regime-frozen.json] [--allow-partial-c]

Run it locally once the table is committed (not on the runner: the frozen rule and the ledger must
stay in the repository). The windows table (real/cross-regime-windows.csv.gz: the polymarket-cross
lane commits real/cross-* only) is written by regime_hf.py (the REGIME.md producer, run on GitHub as
`python cross.py regime --out real/cross-regime-windows.csv.gz`). Expected columns, by their canonical names here (COLMAP below
maps the producer's names onto them; names are compared lower-cased with everything but letters
and digits removed, so "pnl_H", "PNL-h" and "pnlH" are one name; the first alias present wins;
every mapping used is printed in the report):
- start: the window's open, UTC unix seconds (ms and date strings are converted). market (optional):
  the market's id. One row per window; a repeated start keeps the first row.
- per strategy s in H, follow, revert, direction, late, maker (REGIME.md's six base strategies 1-6,
  also accepted as s1 .. s6; a strategy whose pnl column is absent is left out): pnl_s = the
  window's realised P&L in $ at 5 shares a trade (after every fee), trades_s = trades, shares_s =
  shares bought, cost_s = $ spent buying (price + fee), caph_s = capital-hours ($ x hours tied up).
  NaN pnl = the strategy could not be evaluated in the window (no usable book): it is not a
  training row, and it counts as 0 (no trade) for every rule and control. 0 = evaluated, no trade.
- environment at the open (REGIME.md): rv5m, rv30m, rv1h (realised vol, any consistent scale), vr60
  (60-minute variance ratio, trend > 1 > range), dvol_rv (DVOL - realised vol), ret4h, vol_rel
  (Binance volume / its 24 h mean), funding, basis, hour (UTC), weekend (0 / 1), pm_spread
  (Polymarket opening spread), ask_size (shares at the best ask at the open), book_ups (book updates
  per second of the previous window) and book_ups_pre (of this market's book in the minute before
  its open; regime_hf.py measures REGIME.md's 盘口每秒更新次数 both ways, both are used). Missing ones
  are NaN (said in the report); hour and weekend are derived from start when absent. Any other
  column is ignored: in particular the producer's won (the outcome), book_cov / bin_cov (in-window
  coverage), dvol (the level; REGIME.md lists DVOL - RV only), pm_up and fac_up (not in REGIME.md's
  list), its 20-share columns (pnl20_s ...: the 5-share ones are used) and any trailing-performance
  column (the trailing features are rebuilt here, so their timing is checked here). The producer's
  maker sensitivity (pnl_makerbook, trades_makerbook, shares_makerbook: the maker when a snapshot's
  ask strictly below our bid also fills it) is shown beside the maker, never modelled or switched.

What is done (REGIME.md, fixed 2026-10-04 before running; constants below):
1. Trailing features, per strategy s: hot12_s, hot48_s = the sum of pnl_s over the previous 12 / 48
   five-minute slots, NaN if none of those slots has a row. "Previous" skips the slot just ended
   (conservative: a window held to settlement resolves at or after the next window's open, so its
   P&L is not known at that open): for a window opening at t, the slots open in
   [t - (N + 1) x 300, t - 600]. Each strategy's model sees its own two (策略自身的冷热).
2. Models, per strategy (REGIME.md: "预测它这个窗口的每份盈亏和波动"): features = the environment
   columns (those not all NaN on A) + its own hot12, hot48. Training rows = the windows in which
   the strategy traded. Target 1, the mean: the window's P&L a share, y = pnl_s / shares_s ($ a
   share). Target 2, the variance: the squared residual (y - fitted mean)^2 of those rows (in-sample
   residuals of the averaged mean model). Two models each, parameters fixed: ridge (alpha 10 on
   features standardised on the training rows, clipped at +-5 sd, NaN -> 0) and
   HistGradientBoostingRegressor (depth 3, 8 leaves, >= 200 rows a leaf, lr 0.05, 200 iterations;
   l2 1, no early stopping, random_state 0 as factors.py / mix_hf.py). Prediction = the mean of
   ridge and HGB (no choice of model on A: one less thing selected). Predicted volatility =
   sqrt(max(predicted variance, (0.5 x the training rows' sd of y)^2)): the variance model may claim
   at most a 4x lower variance than the strategy's own; fit on a few hundred rows of a fat-tailed
   payoff (late: +2-5c mostly, -95c sometimes), it otherwise finds regions without an in-sample loss
   and makes a small mean look large. Score = predicted mean / predicted
   volatility (预测盈亏比 = 预测均值 ÷ 预测波动; a per-trade Sharpe ratio, the same scale for a
   strategy that trades in 5% of the windows as for one that trades in 80%).
   A out of sample: 7-day blocks from 05-25; each block scored by models fit on the A rows of
   windows that ended at least one slot (300 s) before the block's start; the first 14 days only
   train (A out of sample = 06-08 .. 07-15); a fit needs >= 400 traded windows (else that strategy is
   closed there). B and C are scored by models fit on every A row (windows that ended >= 300 s
   before B's start), never refit on B or C.
3. Switch rule: in each window open every strategy whose score > threshold (several may be open);
   the threshold, one for all strategies, is the one of 0, 0.05, 0.1, 0.2 with the best mean daily
   P&L on the A out-of-sample days (ties: the higher threshold).
   Controls: each strategy always on; the hand trend / range switch (vr60 > 1: follow and direction
   on; vr60 <= 1: revert and maker on; NaN vr60: none of these four; H and late, which REGIME.md does
   not tie to trend or range, are on in every window, so the control differs from the switch only in
   how it switches); the strategy with the best mean daily P&L over all of A, always on.
4. Freeze: the threshold, the best-on-A strategy, the feature list and every A candidate are written
   to real/regime-frozen.json by freeze(), which is given the A rows only (it refuses others),
   BEFORE any B or C row is scored; the evaluation reads the file back from disk. An existing frozen
   file is not overwritten: it is used as is, and C is opened only when the A rows have the same
   fingerprint as when it was written (delete it to re-freeze, and say so).
5. B: pass = mean daily P&L > 0, t >= 2 (days as clusters: t = mean / (sd / sqrt(days))), and the
   mean is above the best always-on control's mean daily P&L on B (the best of the six, picked on B
   itself: the harder bar). C (08-16 .. 08-29) is opened only if B passes and every C day has at
   least one window in the table (C_MIN_WINDOWS = 1: every C archive was processed; a day missing
   means a failed or time-skipped archive, so rerun the producer; --allow-partial-c opens C anyway
   and the report says so): pass = mean daily P&L > 0. While C is closed, nothing of C is reported
   (not even the base strategies).
   Days, decided before any B result was seen, the same for B and C: every UTC day with at least one
   window in the segment is one cluster of equal weight, its P&L the sum over the windows present
   (a day the dataset only partly covers counts as a whole day). The dataset is thin there: the
   jump2s lane counted, per archive, 7 B days (07-17 243 markets, 07-18 214, 08-06 231, 08-12 141,
   08-13 122, 08-14 65, 08-15 77) and in C 08-16 63, 08-17 48, 08-18 19, 08-19 11, 08-20 109, then
   250-279 a day; the verdict lines list every B / C day with its window count.
   The ledger (regime-ledger.json beside the frozen file, whatever the frozen file is called):
   every B evaluation (frozen rule, A and B fingerprints, result) is appended, and the first opening
   of C is recorded once. A later run reports that first C result as the verdict; if the C rows
   have changed since, the new numbers are shown but marked as not a clean lockbox; a different
   frozen rule (re-frozen under any name) gets no C verdict. Earlier B looks are listed.
6. Report (Chinese, units): per strategy and per regime state (trend / range x realised-vol tercile
   x sign of DVOL - RV): win rate, average win / average loss (盈亏比), per-share P&L, trades,
   shares, buying cost, P&L and capital tied up per day, max drawdown over the segment; then the
   switch against the controls per segment; and how well the predictions ranked the windows.

Where REGIME.md and the task are silent, the conservative choice made here:
- Segments by the window's open (UTC): A = 05-25 .. 07-15, B = 07-16 .. 08-15, C = 08-16 .. 08-29;
  rows outside are used only as history for the trailing features. Days = UTC days of the open
  with a row in the segment; a day on which a rule opens nothing counts as $0.
- Win rate and 盈亏比 are per traded window (trades > 0; without a trades column, shares > 0;
  without that, pnl != 0): a window's net P&L is one outcome.
- Per-share P&L = sum of pnl / sum of shares (cents a share). Capital tied up, two ways: the
  time-average (sum of capital-hours / (24 x days)) and the peak = the largest $ bought in one
  window by the strategies open (positions are closed or settled by the window's end, so this
  bounds the capital needed at once; a payout delayed after resolution would add to it). Max
  drawdown = the largest fall of the cumulative P&L (window by window, from $0 at the segment's
  start) below its running peak, in $.
- Regime states: vol terciles of rv30m with cut points from A, applied unchanged to B and C; a window
  with a NaN in vr60, rv30m or dvol_rv is in the state "缺数据" (a component that is NaN on every
  A row is dropped from the state and the report says so).
- The A columns of the switch-vs-controls table are A's out-of-sample days (06-08 .. 07-15) for
  every rule (the switch has predictions only there); the best-on-A strategy is chosen on all of A.
  The threshold (1 of 4) and the best-on-A strategy are chosen on those same days, so the A rows
  are in-sample after that selection (their t is biased up); only B and C confirm. The report says so.
- Ceiling vs achievable: every taker fill is modelled 0.5 s after the decision on the recorder's
  clocks. The Hugging Face book timestamps appear to lag (on 06-02 H made +11.7c a share at this
  0.5 s, REGIME.md's 0.3 s number, not its 0.5 s +3-5c), so H's numbers, and the switch's as far as
  it opens H, are an upper bound; resting sells and the maker fill only strictly through (a touch
  never fills), which leans conservative. The report says so and adds "the switch without H"
  (display only, not part of any verdict).
- "Better than the best always-on control" compares the point estimates of the mean daily P&L; the
  paired difference (switch - that control, per day) and its t are reported beside it.
- The report goes to real/regime-learn.md, not real/regime.md: that name already holds the
  committed hedging-regime study (regime.py), which is left untouched.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import time
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
WINDOWS = HERE / "real" / "cross-regime-windows.csv.gz"  # the CI lane commits real/cross-* only
OUT = HERE / "real" / "regime-learn.md"
FROZEN = HERE / "real" / "regime-frozen.json"
LEDGER_NAME = "regime-ledger.json"  # beside the frozen file: every B look and the first opening of C

STRATS = ("H", "follow", "revert", "direction", "late", "maker")
QTYS = ("pnl", "trades", "shares", "cost", "caph")
ENV = ("rv5m", "rv30m", "rv1h", "vr60", "dvol_rv", "ret4h", "vol_rel", "funding", "basis", "hour", "weekend",
       "pm_spread", "ask_size", "book_ups", "book_ups_pre")

_S_ALIAS = {"H": ("h", "s1", "stale", "hfair", "h_fair", "snipe"),
            "follow": ("follow", "s2", "fol", "chase", "jump", "follow_scalp", "scalp"),
            "revert": ("revert", "s3", "rev", "reversal", "reverse", "fade", "reversion"),
            "direction": ("direction", "s4", "dir", "factor", "fac", "directional", "factors"),
            "late": ("late", "s5", "strong", "late_strong", "close", "nearcert", "fav", "favorite"),
            "maker": ("maker", "s6", "mm", "make", "making")}
_Q_ALIAS = {"pnl": ("pnl", "pnl5", "pnl_usd", "profit", "pnl_5"),
            "trades": ("trades", "n_trades", "ntrades", "n", "trade_count", "fills"),
            "shares": ("shares", "sh", "sh5", "shares5", "qty"),
            "cost": ("cost", "cost5", "cost_usd", "buy_cost", "spent", "buy_usd"),
            "caph": ("caph", "caphr", "cap_h", "cap_hours", "capital_h", "capital_hours", "caphours", "cap_hr",
                     "dollar_hours", "cap_usd_h")}

# canonical name -> accepted column names (compared by _norm; first present wins)
COLMAP = {
    "start": ("start", "start_s", "window_start", "open_s", "t0", "ts", "start_ts", "start_time", "open_time",
              "window_open", "t_open", "market_start"),
    "end": ("end", "end_s", "window_end", "end_ts", "end_time", "market_end"),
    "market": ("market", "market_id", "slug", "condition_id"),
    "rv5m": ("rv5m", "rv5", "rv300", "rv_5m_ann"),
    "rv30m": ("rv30m", "rv30", "rv1800", "rv_30m_ann"),
    "rv1h": ("rv1h", "rv60m", "rv60", "rv3600", "rv_1h_ann"),
    "vr60": ("vr60", "vr60m", "vr", "variance_ratio"),
    "dvol_rv": ("dvol_rv", "dvol_minus_rv", "dvol_rv30", "ivrv", "iv_rv", "dvol_minus_rv30"),
    "ret4h": ("ret4h", "r4h", "trend4h", "ret_240m"),
    "vol_rel": ("vol_rel", "volume_rel", "vol_ratio", "volrel", "rel_volume", "vol24", "vol_rel24", "volume_ratio"),
    "funding": ("funding", "funding_rate", "fund"),
    "basis": ("basis", "basis_bp", "perp_basis"),
    "hour": ("hour", "hour_utc", "hod"),
    "weekend": ("weekend", "is_weekend", "wkend"),
    "pm_spread": ("pm_spread", "open_spread", "spread_open", "spread", "pm_spread_open"),
    "ask_size": ("ask_size", "open_ask_size", "ask_sz", "ask_depth", "asksize", "ask_size_open"),
    "book_ups": ("book_ups", "book_rate", "updates_per_s", "book_hz", "upd_per_s", "ups", "book_updates_per_s",
                 "book_ups_s"),
    "book_ups_pre": ("book_ups_pre", "book_ups_pre_open", "pre_ups", "ups_pre"),
    **{f"{q}_{s}": tuple(dict.fromkeys([f"{q}_{s}"] + [f"{qa}_{sa}" for qa in _Q_ALIAS[q] for sa in _S_ALIAS[s]]
                                       + [f"{sa}_{qa}" for qa in _Q_ALIAS[q] for sa in _S_ALIAS[s]]))
       for s in STRATS for q in QTYS},
    **{f"{q}_makerbook": (f"{q}_makerbook", f"{q}_maker_book") for q in ("pnl", "trades", "shares")},
}

NAMES = {"H": "H（抢过期报价，持有到结算）", "follow": "跟随短打", "revert": "反转", "direction": "方向（开盘多因子）",
         "late": "收盘前强势方", "maker": "做市"}
TREND_ON, RANGE_ON = ("follow", "direction"), ("revert", "maker")
HAND_ALWAYS = ("H", "late")  # not tied to trend / range by REGIME.md: on in every window of the hand control
SENS = "makerbook"           # the producer's maker sensitivity: shown beside the maker, never modelled
UNIT = 5.0                   # shares a trade

SLOT = 300
DAY = 86400
TRAIL = (12, 48)
TRAIL_LAG = 1               # slots skipped before the trailing sums (the one just ended)
SEGMENTS = (("A", "2026-05-25", "2026-07-16"), ("B", "2026-07-16", "2026-08-16"), ("C", "2026-08-16", "2026-08-30"))
BLOCK_DAYS, MIN_TRAIN_DAYS = 7, 14
GAP_S = 300                 # a training window must have ended this long before the block it scores
THRESHOLDS = (0.0, 0.05, 0.1, 0.2)
RIDGE_ALPHA = 10.0
HGB_PARAMS = dict(max_depth=3, max_leaf_nodes=8, min_samples_leaf=200, learning_rate=0.05, max_iter=200,
                  l2_regularization=1.0, early_stopping=False, random_state=0)
MODELS = ("ridge", "hgb")
MIN_TRAIN = 2 * HGB_PARAMS["min_samples_leaf"]
VOL_FLOOR = 0.5            # predicted volatility >= this x the training rows' sd of the per-share P&L
EPS_SH = 1e-4               # $ a share: the volatility's absolute floor
B_T = 2.0
C_MIN_WINDOWS = 1           # every C day must have a window (its archive was processed) to open the lockbox
STATE_COLS = ("vr60", "rv30m", "dvol_rv")


def ts(day):
    return int(pd.Timestamp(day, tz="UTC").timestamp())


def segment_of(start_s):
    """'A' / 'B' / 'C' by window open (s, UTC), '' outside."""
    s = np.asarray(start_s, float)
    out = np.full(s.shape, "", dtype=object)
    for name, a, b in SEGMENTS:
        out[(s >= ts(a)) & (s < ts(b))] = name
    return out


# --------------------------------------------------------------------------- loading
def _norm(name):
    return re.sub(r"[^0-9a-z]", "", str(name).lower())


def _to_seconds(x):
    x = pd.Series(x)
    if not pd.api.types.is_numeric_dtype(x):
        return (pd.to_datetime(x, utc=True).astype("int64") // 10 ** 9).astype(float).to_numpy()
    v = x.to_numpy(float)
    med = np.nanmedian(v) if np.isfinite(v).any() else 0.0
    return v / 1000.0 if med > 1e11 else v


def map_columns(columns):
    """{canonical: source column} by COLMAP (first alias present wins)."""
    norm = {}
    for c in columns:
        norm.setdefault(_norm(c), c)
    mapping = {}
    for canon, aliases in COLMAP.items():
        for a in aliases:
            if _norm(a) in norm:
                mapping[canon] = norm[_norm(a)]
                break
    return mapping


LONG_KEYS = ("strategy", "strat", "policy", "base", "base_strategy")


def _strategy_of(value):
    v = _norm(value)
    for s in STRATS:
        if v in {_norm(a) for a in (s,) + _S_ALIAS[s]}:
            return s
    return None


def widen(raw, log=print):
    """A long table (one row per window x strategy: a strategy column, bare pnl / trades / shares /
    cost / caph columns, the environment repeated) -> one row per window with pnl_s etc.; None when
    `raw` is not such a table."""
    norm = {}
    for c in raw.columns:
        norm.setdefault(_norm(c), c)
    key = next((norm[_norm(k)] for k in LONG_KEYS if _norm(k) in norm), None)
    tcol = next((norm[_norm(a)] for a in COLMAP["start"] + COLMAP["end"] if _norm(a) in norm), None)
    if key is None or tcol is None:
        return None
    qcol = {q: next((norm[_norm(a)] for a in _Q_ALIAS[q] if _norm(a) in norm), None) for q in QTYS}
    if qcol["pnl"] is None:
        return None
    r = raw.copy()
    r["_s"] = [_strategy_of(v) for v in r[key]]
    unknown = sorted({str(v) for v, s in zip(r[key], r["_s"]) if s is None})
    if unknown:
        log(f"long table: strategies not in REGIME.md ignored: {unknown[:10]}")
    r = r[r["_s"].notna()]
    env = raw.drop(columns=[key] + [c for c in qcol.values() if c]).groupby(tcol, sort=False).first()
    out = [env]
    for q, c in qcol.items():
        if c is None:
            continue
        w = r.pivot_table(index=tcol, columns="_s", values=c, aggfunc="first", dropna=False)
        out.append(w.rename(columns={s: f"{q}_{s}" for s in w.columns}))
    return pd.concat(out, axis=1).reset_index()


def load_windows(src, log=print):
    """(windows frame with canonical columns sorted by start, mapping {canonical: source column},
    strategies present, missing environment columns). src: a path or a DataFrame (one row per
    window; a long table, one row per window x strategy, is widened first)."""
    raw = src.copy() if isinstance(src, pd.DataFrame) else pd.read_csv(src)
    mapping = map_columns(raw.columns)
    if not any(f"pnl_{s}" in mapping for s in STRATS):
        wide = widen(raw, log)
        if wide is not None:
            log("long table (one row per window x strategy) widened")
            raw, mapping = wide, map_columns(wide.columns)
    if "start" in mapping:
        start = _to_seconds(raw[mapping["start"]])
    elif "end" in mapping:
        start = _to_seconds(raw[mapping["end"]]) - SLOT
        log(f"no start column: start = {mapping['end']} - {SLOT} s")
    else:
        raise ValueError(f"no window-start column (tried {COLMAP['start']}); columns: {list(raw.columns)[:40]}")
    W = pd.DataFrame({"start": start})
    W["market"] = raw[mapping["market"]].astype(str).to_numpy() if "market" in mapping else \
        W["start"].round().astype("Int64").astype(str).to_numpy()
    strats = [s for s in STRATS if f"pnl_{s}" in mapping]
    for s in strats:
        for q in QTYS:
            c = mapping.get(f"{q}_{s}")
            W[f"{q}_{s}"] = pd.to_numeric(raw[c], errors="coerce").to_numpy(float) if c else np.nan
    for q in ("pnl", "trades", "shares"):
        if f"{q}_{SENS}" in mapping:
            W[f"{q}_{SENS}"] = pd.to_numeric(raw[mapping[f"{q}_{SENS}"]], errors="coerce").to_numpy(float)
    missing = []
    for e in ENV:
        if e in mapping:
            W[e] = pd.to_numeric(raw[mapping[e]], errors="coerce").to_numpy(float)
        else:
            W[e] = np.nan
            missing.append(e)
    st = W["start"].to_numpy(float)
    if "hour" in missing:
        W["hour"] = np.floor((st % DAY) / 3600)
        missing.remove("hour")
    if "weekend" in missing:
        W["weekend"] = (((np.floor(st / DAY) + 3) % 7) >= 5).astype(float)  # 1970-01-01 was a Thursday
        missing.remove("weekend")
    W = W[np.isfinite(W["start"].to_numpy(float))]
    n0 = len(W)
    W = W.sort_values("start", kind="stable").drop_duplicates("start", keep="first").reset_index(drop=True)
    if len(W) < n0:
        log(f"dropped {n0 - len(W)} repeated window starts")
    W["day"] = np.floor(W["start"].to_numpy(float) / DAY).astype(np.int64)
    W["segment"] = segment_of(W["start"].to_numpy(float))
    return W, mapping, strats, missing


def ensure_columns(W, strats, env):
    """NaN columns for any strategy quantity / environment column the frozen spec names but W lacks."""
    W = W.copy()
    for s in strats:
        for q in QTYS:
            if f"{q}_{s}" not in W:
                W[f"{q}_{s}"] = np.nan
    for e in env:
        if e not in W:
            W[e] = np.nan
    return W


def add_trailing(W, strats):
    """hot{N}_s: sum of pnl_s over the N slots open in [t - (N + TRAIL_LAG) x 300, t - (TRAIL_LAG + 1) x 300]
    (NaN when none of them has a row); only windows that ended a slot before t."""
    W = W.copy()
    st = W["start"].to_numpy(float)
    if not len(W):
        for s in strats:
            for n in TRAIL:
                W[f"hot{n}_{s}"] = np.nan
        return W
    s0 = st.min()
    slot = np.round((st - s0) / SLOT).astype(np.int64)
    grid = np.arange(slot.max() + 1)
    for s in strats:
        g = pd.Series(np.nan, index=grid)
        g.iloc[slot] = W[f"pnl_{s}"].to_numpy(float)
        for n in TRAIL:
            r = g.rolling(n, min_periods=1).sum().shift(1 + TRAIL_LAG)
            W[f"hot{n}_{s}"] = r.to_numpy()[slot]
    return W


# --------------------------------------------------------------------------- models
class Ridge:
    """Ridge regression on features standardised on the training rows (clipped at +-5 sd, NaN -> 0)."""

    def __init__(self, alpha=RIDGE_ALPHA):
        self.alpha = alpha

    def _z(self, X):
        Z = (X - self.mu) / self.sd
        return np.clip(np.where(np.isfinite(Z), Z, 0.0), -5, 5)

    def fit(self, X, y):
        X = np.asarray(X, float)
        with np.errstate(invalid="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            mu, sd = np.nanmean(X, axis=0), np.nanstd(X, axis=0)
        self.mu = np.where(np.isfinite(mu), mu, 0.0)
        self.sd = np.where(np.isfinite(sd) & (sd > 0), sd, 1.0)
        Z = self._z(X)
        self.zm = Z.mean(axis=0)
        Z = Z - self.zm
        self.ym = float(np.mean(y))
        self.coef = np.linalg.solve(Z.T @ Z + self.alpha * np.eye(Z.shape[1]), Z.T @ (np.asarray(y, float) - self.ym))
        return self

    def predict(self, X):
        return self.ym + (self._z(np.asarray(X, float)) - self.zm) @ self.coef


def make_model(name):
    if name == "ridge":
        return Ridge()
    from sklearn.ensemble import HistGradientBoostingRegressor
    return HistGradientBoostingRegressor(**HGB_PARAMS)


def features_for(s, env):
    return list(env) + [f"hot{n}_{s}" for n in TRAIL]


def env_features(A):
    """Environment columns not entirely NaN on A (decided on A alone)."""
    return [e for e in ENV if e in A and np.isfinite(A[e].to_numpy(float)).any()]


def a_blocks():
    """7-day blocks of A from its first day: (start, end, out-of-sample?)."""
    a0, a1 = ts(SEGMENTS[0][1]), ts(SEGMENTS[0][2])
    out, b = [], a0
    while b < a1:
        out.append((b, min(b + BLOCK_DAYS * DAY, a1), b - a0 >= MIN_TRAIN_DAYS * DAY))
        b += BLOCK_DAYS * DAY
    return out


def a_oos_from():
    return min(b0 for b0, _, oos in a_blocks() if oos)


def trainable(start, y, before):
    """Rows usable to fit a model applied from `before` on: label present, window ended >= GAP_S earlier."""
    return np.isfinite(y) & (np.asarray(start, float) + SLOT + GAP_S <= before)


def per_share(W, s):
    """The window's P&L a share ($) of strategy s: pnl / shares where shares > 0 (without a shares column:
    pnl / (5 x trades); without either: the window's pnl where it is not 0); NaN where it did not trade
    or could not be evaluated. The models' target (REGIME.md: 每份盈亏)."""
    pnl = W[f"pnl_{s}"].to_numpy(float) if f"pnl_{s}" in W else np.full(len(W), np.nan)
    for q, k in (("shares", 1.0), ("trades", UNIT)):
        v = W[f"{q}_{s}"].to_numpy(float) if f"{q}_{s}" in W else np.full(len(W), np.nan)
        if np.isfinite(v).any():
            with np.errstate(invalid="ignore", divide="ignore"):
                return np.where(np.isfinite(pnl) & np.isfinite(v) & (v > 0), pnl / (k * np.where(v > 0, v, 1.0)), np.nan)
    return np.where(np.isfinite(pnl) & (pnl != 0), pnl, np.nan)


class Scorer:
    """Ridge + HGB for the per-share P&L of a traded window (the mean) and for its squared residual (the
    variance); score = predicted mean / predicted volatility."""

    def fit(self, X, y):
        y = np.asarray(y, float)
        self.m = [make_model(k).fit(X, y) for k in MODELS]
        mu = np.mean([m.predict(X) for m in self.m], axis=0)
        self.v = [make_model(k).fit(X, (y - mu) ** 2) for k in MODELS]
        self.floor = max(VOL_FLOOR * float(np.std(y)), EPS_SH)
        return self

    def parts(self, X):
        """({model: (pred mean, pred variance)}, averaged pred mean, predicted volatility)."""
        out = {k: (m.predict(X), v.predict(X)) for k, m, v in zip(MODELS, self.m, self.v)}
        mean = np.mean([o[0] for o in out.values()], axis=0)
        var = np.mean([o[1] for o in out.values()], axis=0)
        return out, mean, np.sqrt(np.fmax(var, self.floor ** 2))

    def score(self, X):
        return rr_score(*self.parts(X)[1:])


def rr_score(m, vol):
    """Predicted mean / predicted volatility (the volatility is floored by the Scorer; here at EPS_SH);
    NaN where either is."""
    m, vol = np.asarray(m, float), np.asarray(vol, float)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = m / np.fmax(vol, EPS_SH)
    out[~(np.isfinite(m) & np.isfinite(vol))] = np.nan
    return out


def walk_forward(X, y, start):
    """A out-of-sample (score, {model: (pred mean, pred variance)}, mean pred) from y = the per-share P&L
    (NaN = no trade): each out-of-sample block scored by models fit on the traded rows that ended >=
    GAP_S before the block's start; NaN elsewhere or when < MIN_TRAIN such rows."""
    start = np.asarray(start, float)
    y = np.asarray(y, float)
    n = len(y)
    sc, pm = np.full(n, np.nan), np.full(n, np.nan)
    parts = {k: (np.full(n, np.nan), np.full(n, np.nan)) for k in MODELS}
    for b0, b1, oos in a_blocks():
        if not oos:
            continue
        te = (start >= b0) & (start < b1)
        tr = trainable(start, y, b0)
        if not te.any() or tr.sum() < MIN_TRAIN:
            continue
        f = Scorer().fit(X[tr], y[tr])
        p, m, a = f.parts(X[te])
        sc[te], pm[te] = rr_score(m, a), m
        for k in MODELS:
            parts[k][0][te], parts[k][1][te] = p[k]
    return sc, parts, pm


def a_predictions(A, strats, env, log=print):
    """{s: (A out-of-sample score, per-model parts, mean pred pnl)} from A rows only."""
    if len(A) and (segment_of(A["start"].to_numpy(float)) != "A").any():
        raise ValueError("a_predictions is given rows outside A")
    out = {}
    t0 = time.time()
    for s in strats:
        X = A[features_for(s, env)].to_numpy(float)
        out[s] = walk_forward(X, per_share(A, s), A["start"].to_numpy(float))
        log(f"  walk-forward {s}: {time.time() - t0:.0f} s")
    return out


def fit_final(A, strats, env):
    """{s: Scorer fit on every A row that ended >= GAP_S before B starts} (None when too few rows)."""
    if len(A) and (segment_of(A["start"].to_numpy(float)) != "A").any():
        raise ValueError("fit_final is given rows outside A")
    b0 = ts(SEGMENTS[1][1])
    out = {}
    for s in strats:
        X = A[features_for(s, env)].to_numpy(float)
        y = per_share(A, s)
        tr = trainable(A["start"].to_numpy(float), y, b0)
        out[s] = Scorer().fit(X[tr], y[tr]) if tr.sum() >= MIN_TRAIN else None
    return out


def score_segment(final, S, strats, env):
    """B / C scores by the models fit on A: {s: score}, {s: mean pred pnl}."""
    sc, pm = {}, {}
    for s in strats:
        if final.get(s) is None or not len(S):
            sc[s], pm[s] = np.full(len(S), np.nan), np.full(len(S), np.nan)
            continue
        _, m, a = final[s].parts(S[features_for(s, env)].to_numpy(float))
        sc[s], pm[s] = rr_score(m, a), m
    return sc, pm


# --------------------------------------------------------------------------- rules and statistics
def qty(W, s, q):
    """A strategy's per-window quantity with NaN -> 0 (not evaluated = not traded)."""
    v = W[f"{q}_{s}"].to_numpy(float) if f"{q}_{s}" in W else np.full(len(W), np.nan)
    return np.where(np.isfinite(v), v, 0.0)


def traded_mask(W, s):
    for q in ("trades", "shares"):
        v = W[f"{q}_{s}"].to_numpy(float) if f"{q}_{s}" in W else np.full(len(W), np.nan)
        if np.isfinite(v).any():
            return np.where(np.isfinite(v), v, 0.0) > 0
    return qty(W, s, "pnl") != 0


def has_qty(W, strats, q):
    return any(f"{q}_{s}" in W and np.isfinite(W[f"{q}_{s}"].to_numpy(float)).any() for s in strats)


def rule_frame(W, opened):
    """Per window, summed over the strategies opened ({s: bool mask}): pnl, trades, shares, cost, caph,
    traded (any opened strategy traded)."""
    n = len(W)
    out = {q: np.zeros(n) for q in QTYS}
    tr = np.zeros(n, bool)
    for s, m in opened.items():
        m = np.asarray(m, bool)
        for q in QTYS:
            out[q] += np.where(m, qty(W, s, q), 0.0)
        tr |= m & traded_mask(W, s)
    f = pd.DataFrame(out)
    f["traded"] = tr
    f["start"] = W["start"].to_numpy(float)
    f["day"] = W["day"].to_numpy()
    return f


def daily(f, days):
    """Daily P&L ($) on every day of `days` (0 when nothing)."""
    return f.groupby("day")["pnl"].sum().reindex(days, fill_value=0.0)


def day_t(x):
    """dict(mean, se, t, n, pos) of a daily series (days as clusters)."""
    x = np.asarray(x, float)
    n = len(x)
    if not n:
        return dict(mean=np.nan, se=np.nan, t=np.nan, n=0, pos=np.nan)
    m = float(x.mean())
    se = float(x.std(ddof=1) / math.sqrt(n)) if n > 1 else np.nan
    t = m / se if np.isfinite(se) and se > 1e-9 * max(1.0, abs(m)) else np.nan   # identical days: no t
    return dict(mean=m, se=se, t=float(t), n=n, pos=float((x > 0).mean()))


def max_drawdown(pnl):
    """Largest fall ($) of the cumulative P&L below its running peak (from $0)."""
    c = np.concatenate([[0.0], np.cumsum(np.asarray(pnl, float))])
    return float(np.max(np.maximum.accumulate(c) - c))


def perf(f, days, ok=None):
    """Units of a rule frame over `days` (UTC day numbers). ok: {quantity: column present in the
    table?}; an absent quantity is reported as NaN, not 0."""
    ok = ok or {}
    has = {q: ok.get(q, True) for q in QTYS}
    nd = len(days)
    f = f[f["day"].isin(days)].sort_values("start", kind="stable")
    tr = f[f["traded"].to_numpy(bool)]
    pnl = tr["pnl"].to_numpy(float)
    win, loss = pnl[pnl > 0], pnl[pnl < 0]
    sh = f["shares"].sum()

    def per_day(q, scale=1.0):
        return f[q].sum() / (scale * nd) if nd and has[q] else np.nan

    out = dict(days=nd, windows=len(f), traded=len(tr), win=float((pnl > 0).mean()) if len(tr) else np.nan,
               avg_win=float(win.mean()) if len(win) else np.nan, avg_loss=float(loss.mean()) if len(loss) else np.nan,
               per_share=float(f["pnl"].sum() / sh) if has["shares"] and sh > 0 else np.nan,
               trades=per_day("trades"), shares=per_day("shares"), cost=per_day("cost"), pnl_day=per_day("pnl"),
               cap=per_day("caph", 24.0), cap_peak=float(f["cost"].max()) if len(f) and has["cost"] else np.nan,
               mdd=max_drawdown(f["pnl"].to_numpy()))
    out["ratio"] = out["avg_win"] / -out["avg_loss"] if np.isfinite(out["avg_win"]) and np.isfinite(
        out["avg_loss"]) else np.nan
    out.update({f"d_{k}": v for k, v in day_t(daily(f, days).to_numpy()).items()})
    return out


def switch_open(scores, thr):
    """{s: score > thr} (NaN score: closed)."""
    return {s: np.isfinite(v) & (v > thr) for s, v in scores.items()}


def hand_open(W, strats):
    """The hand trend / range control: follow + direction when vr60 > 1, revert + maker when <= 1, H and
    late in every window."""
    vr = W["vr60"].to_numpy(float)
    tr, rg = np.isfinite(vr) & (vr > 1), np.isfinite(vr) & (vr <= 1)
    return {s: tr if s in TREND_ON else (rg if s in RANGE_ON else np.ones(len(W), bool)) for s in strats}


def days_of(W, mask=None):
    d = W["day"].to_numpy() if mask is None else W["day"].to_numpy()[mask]
    return np.unique(d)


# --------------------------------------------------------------------------- freeze and evaluation
def fingerprint(S, strats, env):
    """sha256 of what a judgement depends on: the rows' starts, features and labels."""
    if not len(S):
        return "empty"
    h = hashlib.sha256()
    cols = sorted(set(env) | {f"hot{n}_{s}" for s in strats for n in TRAIL} | {f"{q}_{s}" for s in strats
                                                                               for q in QTYS})
    cols = [c for c in cols if c in S]
    h.update(json.dumps([list(strats), list(env), cols]).encode())
    h.update(np.round(S["start"].to_numpy(float), 3).tobytes())
    h.update(np.round(S[cols].to_numpy(float), 6).tobytes())
    return h.hexdigest()


def a_fingerprint(A, strats, env):
    """The freeze's fingerprint: A's starts, features and pnl labels."""
    if not len(A):
        return "empty"
    h = hashlib.sha256()
    cols = sorted(set(env) | {f"hot{n}_{s}" for s in strats for n in TRAIL} | {f"pnl_{s}" for s in strats})
    h.update(json.dumps([list(strats), list(env)]).encode())
    h.update(np.round(A["start"].to_numpy(float), 3).tobytes())
    h.update(np.round(A[cols].to_numpy(float), 6).tobytes())
    return h.hexdigest()


def freeze(A, path, strats, env, preds=None, log=print):
    """Choose the threshold and the best-on-A strategy on A alone (only A rows may be passed) and write
    `path` unless it exists. Returns (spec read back from disk, written now?, A predictions)."""
    path = Path(path)
    if len(A) and (segment_of(A["start"].to_numpy(float)) != "A").any():
        raise ValueError("freeze() is given rows outside A")
    if preds is None:
        preds = a_predictions(A, strats, env, log)
    if path.exists():
        spec = json.loads(path.read_text(encoding="utf-8"))
        log(f"{path} exists (made {spec.get('made')}); not re-frozen")
        return spec, False, preds
    oos = A["start"].to_numpy(float) >= a_oos_from()
    Ao = A[oos].reset_index(drop=True)
    days = days_of(Ao)
    cand = []
    for thr in THRESHOLDS:
        f = rule_frame(Ao, switch_open({s: preds[s][0][oos] for s in strats}, thr))
        st = day_t(daily(f, days).to_numpy())
        cand.append(dict(threshold=thr, **st, pnl_total=float(f["pnl"].sum()), traded=int(f["traded"].sum())))
    ok = [c for c in cand if np.isfinite(c["mean"])]
    best = max(ok, key=lambda c: (c["mean"], c["threshold"])) if ok else None
    alld = days_of(A)
    always = {s: day_t(daily(rule_frame(A, {s: np.ones(len(A), bool)}), alld).to_numpy()) for s in strats}
    best_s = max(strats, key=lambda s: (always[s]["mean"] if np.isfinite(always[s]["mean"]) else -np.inf, s)) \
        if strats else None
    rv = A["rv30m"].to_numpy(float) if "rv30m" in A else np.array([])
    rv = rv[np.isfinite(rv)]
    cuts = [float(np.quantile(rv, 1 / 3)), float(np.quantile(rv, 2 / 3))] if len(rv) else None
    state_cols = [c for c in STATE_COLS if c in A and np.isfinite(A[c].to_numpy(float)).any()]
    spec = dict(made=pd.Timestamp.now(tz="UTC").isoformat(), design="REGIME.md", segments=SEGMENTS,
                a_oos_from=pd.Timestamp(a_oos_from(), unit="s").strftime("%Y-%m-%d"), strategies=list(strats),
                env_features=list(env), trailing=list(TRAIL), trailing_lag_slots=TRAIL_LAG, gap_s=GAP_S,
                models={"ridge": {"alpha": RIDGE_ALPHA}, "hgb": HGB_PARAMS}, score="mean(ridge,hgb) per-share pnl / "
                "sqrt(max(mean(ridge,hgb) squared residual, (vol_floor x sd)^2)), traded windows only",
                vol_floor=VOL_FLOOR, thresholds=list(THRESHOLDS),
                threshold=None if best is None else best["threshold"], candidates=cand, best_on_a=best_s,
                always_on_a=always, vol_cuts=cuts, state_cols=state_cols, a_rows=int(len(A)),
                a_days=int(len(alld)), a_oos_days=int(len(days)), a_fingerprint=a_fingerprint(A, strats, env),
                b_t=B_T, c_min_windows=C_MIN_WINDOWS)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(spec, indent=1, ensure_ascii=False, default=float), encoding="utf-8")
    log(f"frozen threshold {spec['threshold']} best-on-A {best_s} -> {path}")
    return json.loads(path.read_text(encoding="utf-8")), True, preds


def c_missing(W):
    """The C days (UTC day numbers) with fewer than C_MIN_WINDOWS windows."""
    c0, c1 = ts(SEGMENTS[2][1]), ts(SEGMENTS[2][2])
    want = np.arange(c0 // DAY, c1 // DAY)
    cnt = W[W["segment"] == "C"].groupby("day").size().reindex(want, fill_value=0)
    return [int(d) for d in cnt.index[cnt.to_numpy() < C_MIN_WINDOWS]]


def c_complete(W):
    """Every C day has >= C_MIN_WINDOWS windows (its archive was processed)."""
    return not c_missing(W)


def ledger_path(frozen):
    """The ledger beside the frozen file (one per directory, whatever the frozen file is called)."""
    return Path(frozen).with_name(LEDGER_NAME)


def read_ledger(path):
    d = json.loads(Path(path).read_text(encoding="utf-8")) if path is not None and Path(path).exists() else {}
    d.setdefault("b_looks", [])
    d.setdefault("c", None)
    return d


def write_ledger(path, d):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(d, indent=1, ensure_ascii=False, default=float), encoding="utf-8")


def day_counts(S):
    """'MM-DD (n)' for every day of S."""
    if not len(S):
        return "无"
    c = S.groupby("day").size()
    return "、".join(f"{pd.Timestamp(int(d) * DAY, unit='s').strftime('%m-%d')}（{n}）" for d, n in c.items())


def rules_for(S, strats, scores, spec):
    """{rule name: opened masks} on the rows S."""
    sw = switch_open(scores, spec["threshold"]) if spec.get("threshold") is not None \
        else {s: np.zeros(len(S), bool) for s in strats}
    out = {"switch": sw}
    if "H" in sw and len(sw) > 1:
        out["switch_noH"] = {s: m for s, m in sw.items() if s != "H"}   # display only (H is a ceiling)
    out["hand"] = hand_open(S, strats)
    if spec.get("best_on_a") in strats:
        out["best_a"] = {spec["best_on_a"]: np.ones(len(S), bool)}
    for s in strats:
        out[f"on_{s}"] = {s: np.ones(len(S), bool)}
    return out


def evaluate(W, spec, preds=None, allow_partial_c=False, ledger=None, log=print):
    """A out of sample, B, and C (once, if B passes) of the frozen switch and its controls. ledger: the
    ledger file (every B look is appended, the first C opening recorded); None: nothing recorded."""
    strats, env = spec["strategies"], spec["env_features"]
    A = W[W["segment"] == "A"].reset_index(drop=True)
    fp_ok = spec.get("a_fingerprint") == a_fingerprint(A, strats, env)
    if preds is None or not fp_ok:
        preds = a_predictions(A, strats, env, log)
    ok = {q: has_qty(W, strats, q) for q in QTYS}
    res = dict(segments={}, b_pass=False, c_open=False, c_pass=None, fp_ok=fp_ok, c_complete=c_complete(W),
               c_missing=c_missing(W), c_partial=False, c_record=None, c_clean=None, ok=ok, b_prior=[])
    oos = A["start"].to_numpy(float) >= a_oos_from()
    final = None
    for name in ("A", "B", "C"):
        if name == "A":
            S = A[oos].reset_index(drop=True)
            scores = {s: preds[s][0][oos] for s in strats}
            pm = {s: preds[s][2][oos] for s in strats}
        else:
            if name == "C":
                b = res["segments"].get("B")
                res["b_pass"] = bool(b and b["pass"])
                if not (res["b_pass"] and fp_ok and (res["c_complete"] or allow_partial_c)):
                    break
                res["c_open"], res["c_partial"] = True, not res["c_complete"]
            if final is None:
                final = fit_final(A, strats, env)
            S = W[W["segment"] == name].reset_index(drop=True)
            scores, pm = score_segment(final, S, strats, env)
        days = days_of(S)
        rules = rules_for(S, strats, scores, spec)
        frames = {k: rule_frame(S, v) for k, v in rules.items()}
        stats = {k: perf(f, days, ok) for k, f in frames.items()}
        dd = {k: daily(f, days).to_numpy() for k, f in frames.items()}
        on = [f"on_{s}" for s in strats]
        best_on = max(on, key=lambda k: (stats[k]["d_mean"] if np.isfinite(stats[k]["d_mean"]) else -np.inf, k)) \
            if on else None
        diff = day_t(dd["switch"] - dd[best_on]) if best_on else day_t([])
        sw = stats["switch"]
        seg = dict(S=S, days=days, scores=scores, pm=pm, stats=stats, best_on=best_on, diff=diff, rules=rules,
                   frames=frames)
        if name == "B":
            seg["pass"] = bool(sw["d_n"] and sw["d_mean"] > 0 and np.isfinite(sw["d_t"]) and sw["d_t"] >= B_T
                               and (best_on is None or sw["d_mean"] > stats[best_on]["d_mean"]))
            if ledger is not None:
                led = read_ledger(ledger)
                look = dict(at=pd.Timestamp.now(tz="UTC").isoformat(), frozen_made=spec.get("made"),
                            a_fingerprint=spec.get("a_fingerprint"), b_fingerprint=fingerprint(S, strats, env),
                            threshold=spec.get("threshold"), mean=sw["d_mean"], t=sw["d_t"], n=sw["d_n"],
                            passed=seg["pass"])
                key = ("frozen_made", "a_fingerprint", "b_fingerprint")
                same = [x for x in led["b_looks"] if all(x.get(k) == look[k] for k in key)]
                res["b_prior"] = [x for x in led["b_looks"] if not all(x.get(k) == look[k] for k in key)]
                if not same:
                    led["b_looks"].append(look)
                    write_ledger(ledger, led)
        if name == "C":
            res["c_pass"] = bool(sw["d_n"] and sw["d_mean"] > 0)
            now = dict(opened=pd.Timestamp.now(tz="UTC").isoformat(), frozen_made=spec.get("made"),
                       a_fingerprint=spec.get("a_fingerprint"), c_fingerprint=fingerprint(S, strats, env),
                       mean=sw["d_mean"], se=sw["d_se"], t=sw["d_t"], n=sw["d_n"], c_pass=res["c_pass"])
            if ledger is not None:
                led = read_ledger(ledger)
                rec = led["c"]
                if rec is not None:
                    res["c_record"] = rec
                    res["c_clean"] = bool(rec.get("frozen_made") == now["frozen_made"]
                                          and rec.get("a_fingerprint") == now["a_fingerprint"]
                                          and rec.get("c_fingerprint") == now["c_fingerprint"])
                    if rec.get("frozen_made") == now["frozen_made"] and rec.get("a_fingerprint") == now["a_fingerprint"]:
                        res["c_pass"] = bool(rec["c_pass"])     # the first opening is the verdict
                    else:
                        res["c_pass"] = None                    # C was spent on another frozen rule
                    log(f"C was first opened {rec.get('opened')} (pass {rec.get('c_pass')}); clean now: {res['c_clean']}")
                else:
                    now["partial"] = res["c_partial"]
                    led["c"] = now
                    write_ledger(ledger, led)
                    res["c_record"], res["c_clean"] = now, True
        res["segments"][name] = seg
        log(f"{name}: switch {sw['d_mean']:+.2f} $/day (t {sw['d_t']:.1f}, {sw['d_n']} days); best always-on "
            f"{best_on} {stats[best_on]['d_mean']:+.2f}" if best_on else f"{name}: switch {sw['d_mean']:+.2f} $/day")
    return res


# --------------------------------------------------------------------------- regime states
def state_labels(W, cuts, cols=STATE_COLS):
    """趋势/震荡 x 波动三分位 x DVOL−RV 符号 over the components in `cols`; '缺数据' when one is NaN."""
    n = len(W)
    parts, ok = [], np.ones(n, bool)
    if "vr60" in cols:
        vr = W["vr60"].to_numpy(float)
        ok &= np.isfinite(vr)
        parts.append(np.where(vr > 1, "趋势", "震荡"))
    if "rv30m" in cols and cuts is not None:
        rv = W["rv30m"].to_numpy(float)
        ok &= np.isfinite(rv)
        parts.append(np.where(rv <= cuts[0], "低波", np.where(rv <= cuts[1], "中波", "高波")))
    if "dvol_rv" in cols:
        iv = W["dvol_rv"].to_numpy(float)
        ok &= np.isfinite(iv)
        parts.append(np.where(iv > 0, "DVOL>RV", "DVOL≤RV"))
    if not parts:
        return np.full(n, "缺数据", dtype=object)
    lab = pd.Series(parts[0].astype(str))
    for p in parts[1:]:
        lab = lab + "·" + pd.Series(p.astype(str))
    return np.where(ok, lab.to_numpy(object), "缺数据")


def state_order(cols, cuts):
    lists = []
    if "vr60" in cols:
        lists.append(("趋势", "震荡"))
    if "rv30m" in cols and cuts is not None:
        lists.append(("低波", "中波", "高波"))
    if "dvol_rv" in cols:
        lists.append(("DVOL>RV", "DVOL≤RV"))
    out = [""]
    for lst in lists:
        out = [f"{a}·{b}" if a else b for a in out for b in lst]
    return [o for o in out if o] + ["缺数据"]


# --------------------------------------------------------------------------- report
def _usd(x, sign=True):
    if x is None or not np.isfinite(x):
        return "–"
    return f"{'+' if sign and x >= 0 else ('-' if x < 0 else '')}${abs(x):,.2f}"


def _cents(x):
    return "–" if x is None or not np.isfinite(x) else f"{100 * x:+.2f}¢"


def _pct(x):
    return "–" if x is None or not np.isfinite(x) else f"{100 * x:.1f}%"


def _num(x, nd=1):
    return "–" if x is None or not np.isfinite(x) else f"{x:,.{nd}f}"


def _t(x):
    return "–" if x is None or not np.isfinite(x) else f"{x:+.2f}"


PERF_HEAD = ("| 天数 | 有成交的窗口 | 胜率 | 平均赢 / 平均亏 $（盈亏比） | 每份盈亏 | 笔/天 | 份/天 | 买入花费 $/天 | "
             "利润 $/天（± 标准误，按天 t） | 占用资金 $（时间平均） | 峰值占用 $（单窗口最大买入） | 最大回撤 $ |")
PERF_SEP = "|---:|---:|---:|---|---:|---:|---:|---:|---|---:|---:|---:|"


def _perf_cells(p):
    ratio = f"{_usd(p['avg_win'])} / {_usd(p['avg_loss'])}（{_num(p['ratio'], 2)}）"
    pl = f"{_usd(p['pnl_day'])}（±{_num(p['d_se'], 2)}，t {_t(p['d_t'])}）"
    return (f"{p['days']} | {p['traded']:,} | {_pct(p['win'])} | {ratio} | {_cents(p['per_share'])} | "
            f"{_num(p['trades'])} | {_num(p['shares'], 0)} | {_usd(p['cost'], False)} | {pl} | "
            f"{_usd(p['cap'], False)} | {_usd(p.get('cap_peak'), False)} | {_usd(p['mdd'], False)} |")


RULE_NAMES = {"switch": "**学习切换**", "switch_noH": "学习切换（不含 H，H 是上限；只作参考，不参与判定）",
              "hand": "手工趋势/震荡切换（VR60 > 1 开跟随+方向，≤ 1 开反转+做市；H、收盘前强势方一直开）",
              "best_a": "A 段最好的一个一直开"}


def _rule_name(k, spec):
    if k == "best_a":
        return f"{RULE_NAMES[k]}（{NAMES.get(spec.get('best_on_a'), spec.get('best_on_a'))}）"
    if k.startswith("on_"):
        return f"一直开：{NAMES.get(k[3:], k[3:])}"
    return RULE_NAMES.get(k, k)


def _corr(x, y):
    x, y = np.asarray(x, float), np.asarray(y, float)
    m = np.isfinite(x) & np.isfinite(y)
    if m.sum() < 3 or np.std(x[m]) == 0 or np.std(y[m]) == 0:
        return np.nan
    return float(np.corrcoef(x[m], y[m])[0, 1])


def report(W, spec, res, mapping, missing, written, paths, log=print):
    strats = spec["strategies"]
    ok = res.get("ok", {})
    L = ["# 按状态学会何时开哪个策略（REGIME.md 的学习和判定）", ""]
    segs = res["segments"]
    n_seg = {k: int((W["segment"] == k).sum()) for k in "ABC"}
    L += [f"数据：`{paths['windows']}`，每个 5 分钟窗口一行；A {n_seg['A']:,} 个窗口，B {n_seg['B']:,} 个，"
          f"C {'%s 个' % format(n_seg['C'], ',') if res['c_open'] else '（锁箱未开，不报告）'}。"
          f"金额都是每笔 5 份时的美元（含两腿手续费），每份盈亏用美分。纸面研究，只用行情数据。", ""]
    L += [f"策略：{'、'.join(NAMES[s] for s in strats)}" + (f"；缺：{'、'.join(NAMES[s] for s in STRATS if s not in strats)}"
                                                         if len(strats) < len(STRATS) else "") + "。",
          f"环境特征（用于模型）：{', '.join(spec['env_features'])}" + (f"；表里没有：{', '.join(missing)}" if missing else "")
          + f"；另加每个策略自己过去 {TRAIL[0]}、{TRAIL[1]} 个窗口（跳过刚结束的那个）的实际盈亏。", ""]
    nq = [q for q in QTYS if not ok.get(q, True)]
    if nq:
        L += [f"表里没有这些量（报告里显示为 –）：{', '.join(nq)}。", ""]
    # verdict
    b = segs.get("B")
    L += ["## 判定", ""]
    if not res["fp_ok"]:
        L += ["- **注意**：冻结文件是用另一份 A 段数据写的（指纹不符），B 的结果只作参考，C 不开。"]
    if b is not None:
        sw, bo = b["stats"]["switch"], (b["stats"][b["best_on"]] if b["best_on"] else None)
        L += [f"- B（{SEGMENTS[1][1][5:]} – 08-15）：学习切换每天 {_usd(sw['d_mean'])}（t {_t(sw['d_t'])}，{sw['d_n']} 天），"
              f"最好的一直开（{NAMES.get(b['best_on'][3:], '') if b['best_on'] else '–'}）每天 "
              f"{_usd(bo['d_mean']) if bo else '–'}；差 {_usd(b['diff']['mean'])}/天（配对 t {_t(b['diff']['t'])}）。"
              f"要求：> 0、t ≥ {B_T:g}、好于最好的一直开 → **{'通过' if res['b_pass'] else '未通过'}**。",
              f"  B 段的天（窗口数；每天一个等权的聚类，不完整的天也算一整天）：{day_counts(b['S'])}。"]
        if "switch_noH" in b["stats"]:
            nh = b["stats"]["switch_noH"]
            L += [f"  H 的数字是上限（见口径）：不含 H 的学习切换在 B 上每天 {_usd(nh['d_mean'])}（t {_t(nh['d_t'])}），只作参考。"]
        if res.get("b_prior"):
            L += [f"  B 段此前还被看过 {len(res['b_prior'])} 次（别的冻结规则或别的 B 数据，记在 ledger 里）："
                  + "；".join(f"{str(x.get('at', ''))[:16]} 冻结于 {str(x.get('frozen_made', ''))[:16]}，门槛 "
                             f"{x.get('threshold')}，每天 {_usd(x.get('mean'))}（t {_t(x.get('t'))}）→ "
                             f"{'通过' if x.get('passed') else '未通过'}" for x in res["b_prior"]) + "。"]
    if res["c_open"]:
        c = segs["C"]["stats"]["switch"]
        rec = res.get("c_record") or {}
        L += [f"- C 段的天（窗口数）：{day_counts(segs['C']['S'])}。"
              + ("**C 段有的天没有窗口，用 --allow-partial-c 强行打开的**。" if res.get("c_partial") else "")]
        if res.get("c_pass") is None:
            L += [f"- C（08-16 – 08-29）：锁箱已在 {str(rec.get('opened', ''))[:19]} UTC 用另一个冻结规则打开过，"
                  f"这次的 C 不是干净的锁箱，不作判定（本次每天 {_usd(c['d_mean'])}，t {_t(c['d_t'])}）。"]
        else:
            first = (f"首次打开 {str(rec.get('opened', ''))[:19]} UTC：每天 {_usd(rec.get('mean'))}"
                     f"（t {_t(rec.get('t'))}，{rec.get('n')} 天）" if rec else "")
            same = "" if res.get("c_clean") in (None, True) else \
                f"；C 段数据此后变了，本次每天 {_usd(c['d_mean'])}（t {_t(c['d_t'])}），以首次为准"
            L += [f"- C（08-16 – 08-29，只算一次）：{first or '每天 ' + _usd(c['d_mean'])}{same} → "
                  f"**{'通过' if res['c_pass'] else '未通过'}**（要求每天盈亏 > 0）。"]
    else:
        miss = "、".join(pd.Timestamp(d * DAY, unit="s").strftime("%m-%d") for d in res.get("c_missing", []))
        why = ("B 未通过" if not res["b_pass"] else ("A 段数据和冻结时不同（指纹不符）" if not res["fp_ok"]
                                                  else f"C 段不完整：这些天没有窗口（存档没处理成功？重跑生产者）：{miss}"))
        L += [f"- C：锁箱没开（{why}）。"]
    L += [""]
    # frozen rule
    L += ["## 冻结的规则（只用 A，写入后才算 B / C）", "",
          f"冻结文件 `{paths['frozen']}`，{'本次写入' if written else '沿用已有文件'}（{spec.get('made', '')[:19]} UTC）。"
          f"门槛 = **{spec.get('threshold')}**（预测盈亏比 = 预测每份盈亏 ÷ 预测每份盈亏的波动，只用有成交的窗口训练，"
          f"岭回归和梯度提升树平均）；A 段最好的单个策略：{NAMES.get(spec.get('best_on_a'), '–')}。", "",
          f"门槛是在下面这些天上从 {len(THRESHOLDS)} 个候选里挑出的，所以被选中那一行是挑选后的样本内结果（t 偏高），"
          "只有 B / C 是确认。", "",
          "| 门槛 | A 样本外每天盈亏 $ | ± 标准误 $ | t | 盈利天占比 | 天数 | 有成交的窗口 |", "|---:|---:|---:|---:|---:|---:|---:|"]
    for c in spec.get("candidates", []):
        L += [f"| {c['threshold']:g} | {_usd(c['mean'])} | {_num(c['se'], 2)} | {_t(c['t'])} | {_pct(c['pos'])} | "
              f"{c['n']} | {c['traded']:,} |"]
    L += ["", "A 全段每个策略一直开（挑“A 段最好的一个”用）：" + "；".join(
        f"{NAMES.get(s, s)} {_usd(v['mean'])}/天" for s, v in spec.get("always_on_a", {}).items()) + "。", ""]
    # switch vs controls
    L += ["## 学习切换 vs 对照（每段）", "",
          f"A = A 段样本外的天（{spec['a_oos_from'][5:]} – 07-15，模型按周滚动）；B / C 用在全部 A 上拟合的模型。"
          "几个策略同时开时，各项按窗口相加。"
          f"门槛（{len(THRESHOLDS)} 个里选 1）和 A 段最好的一个都在这些天上挑出，A 行是样本内选择后的结果；只有 B / C 是确认。", ""]
    for name, seg in segs.items():
        L += [f"### {name} 段", "", "| 规则 " + PERF_HEAD, "|---" + PERF_SEP]
        for k, p in seg["stats"].items():
            L += [f"| {_rule_name(k, spec)} | " + _perf_cells(p)]
        d = seg["diff"]
        L += ["", f"学习切换 − 本段最好的一直开（{NAMES.get((seg['best_on'] or '___')[3:], '–')}）：每天 "
                  f"{_usd(d['mean'])}（± {_num(d['se'], 2)}，配对 t {_t(d['t'])}，{d['n']} 天）。", ""]
    # what the switch opened, and how well the predictions ranked the windows
    L += ["## 学习切换打开了什么（每个策略）", "",
          "预测相关 = 预测的每份盈亏（两种模型平均）与实际每份盈亏的相关系数（只算这个策略有成交的窗口）。", "",
          "| 段 | 策略 | 打开的窗口占比 | 打开时 利润 $/天 | 没打开时 利润 $/天 | 打开时 每份盈亏 | 没打开时 每份盈亏 | 预测相关 |",
          "|---|---|---:|---:|---:|---:|---:|---:|"]
    for name, seg in segs.items():
        S, days = seg["S"], seg["days"]
        for s in strats:
            m = seg["rules"]["switch"][s]
            po = perf(rule_frame(S, {s: m}), days, ok)
            pc = perf(rule_frame(S, {s: ~m}), days, ok)
            L += [f"| {name} | {NAMES[s]} | {_pct(m.mean() if len(m) else np.nan)} | {_usd(po['pnl_day'])} | "
                  f"{_usd(pc['pnl_day'])} | {_cents(po['per_share'])} | {_cents(pc['per_share'])} | "
                  f"{_num(_corr(seg['pm'][s], per_share(S, s)), 3)} |"]
    L += [""]
    # per strategy overall
    show = [k for k in ("A", "B", "C") if k in segs]
    full = {k: W[W["segment"] == k].reset_index(drop=True) for k in show}
    L += ["## 每个策略（全段一直开）", "", f"A 这里是 A 全段（{SEGMENTS[0][1][5:]} – 07-15）。", "",
          "| 策略 | 段 " + PERF_HEAD, "|---|---" + PERF_SEP]
    for s in strats:
        for k in show:
            S = full[k]
            L += [f"| {NAMES[s]} | {k} | " + _perf_cells(perf(rule_frame(S, {s: np.ones(len(S), bool)}), days_of(S), ok))]
        if s == "maker" and f"pnl_{SENS}" in W:
            for k in show:
                S = full[k]
                L += [f"| 做市（敏感性：快照里卖一严格低于我们的买价也算成交） | {k} | " + _perf_cells(perf(rule_frame(
                    S, {SENS: np.ones(len(S), bool)}), days_of(S), dict(ok, cost=False, caph=False)))]
    L += [""]
    # states
    cuts = spec.get("vol_cuts")
    cols = tuple(spec.get("state_cols", STATE_COLS))
    dropped = [c for c in STATE_COLS if c not in cols]
    L += ["## 每个状态下的每个策略", "",
          "状态 = 趋势（过去 60 分钟方差比 VR60 > 1）或震荡（≤ 1）× 30 分钟已实现波动三分位（切点来自 A："
          + (f"{cuts[0]:.4g}、{cuts[1]:.4g}" if cuts else "无") + "）× DVOL − 已实现波动的符号"
          + (f"（表里整段没有 {', '.join(dropped)}，这一维去掉）" if dropped else "") + "。"
          "每天的量 = 这个状态里的总量 ÷ 该段全部天数（即这个状态每天贡献多少）；回撤只算这个状态的窗口。", ""]
    labs = {k: state_labels(full[k], cuts, cols) for k in show}
    order = state_order(cols, cuts)
    L += ["### 利润 $/天 一览（行 = 状态，列 = 策略）", "",
          "| 状态 | 段 | 窗口占比 | " + " | ".join(NAMES[s] for s in strats) + " |",
          "|---|---|---:|" + "---:|" * len(strats)]
    for st in order:
        for k in show:
            m = labs[k] == st
            if not m.any():
                continue
            S, days = full[k], days_of(full[k])
            cells = [_usd(perf(rule_frame(S, {s: m}), days, ok)["pnl_day"]) for s in strats]
            L += [f"| {st} | {k} | {_pct(m.mean())} | " + " | ".join(cells) + " |"]
    L += [""]
    for s in strats:
        L += [f"### {NAMES[s]}", "", "| 状态 | 段 " + PERF_HEAD, "|---|---" + PERF_SEP]
        for st in order:
            for k in show:
                m = labs[k] == st
                if not m.any():
                    continue
                S = full[k]
                L += [f"| {st} | {k} | " + _perf_cells(perf(rule_frame(S, {s: m}), days_of(S), ok))]
        L += [""]
    L += ["## 口径", "",
          "- 一行 = 一个 5 分钟窗口；每个策略在这个窗口里的实际盈亏（每笔 5 份，含手续费，持有到结算的按官方结果）。"
          "策略在某窗口无法评估（没有可用盘口）时算没开、盈亏 $0，也不进训练。",
          "- 胜率、平均赢 / 平均亏（盈亏比）：按有成交的窗口算，一个窗口的净盈亏算一次。",
          "- 每份盈亏 = 总盈亏 ÷ 总份数。占用资金 = 资金 × 小时 的总和 ÷（24 × 天数），即时间平均占用的美元；"
          "峰值占用 = 开着的策略在单个窗口里买入花费的最大值（仓位在窗口结束时平掉或结算，所以这是同时需要的资金的上界；"
          "结算后赎回若有延迟，还要更多）。",
          "- 上限与可实现：所有吃单按录制时钟决策后 0.5 秒成交。Hugging Face 盘口的时间戳看起来滞后（06-02 试跑里 H 在这里的"
          " 0.5 秒是每份 +11.7¢，相当于 REGIME.md 里 0.3 秒的 +10–12¢，而不是 0.5 秒的 +3–5¢），所以 H 的数字、以及学习切换里"
          "打开 H 的部分，是上限；“学习切换（不含 H）”一行只作参考。挂单卖和做市只按严格穿价成交（碰到不算），偏保守；"
          "做市按 REGIME.md 只认成交价穿过，另列“快照里卖一低于我们的买价也算成交”的敏感性；生产者在币安数据断流超过 1 秒时撤掉做市报价。",
          "- 最大回撤：按窗口时间顺序累计盈亏，从该段开头的 $0 起，低于此前最高点的最大跌幅（美元）。",
          "- 按天 t：每天的盈亏当一个样本，t = 平均 ÷（标准差 ÷ √天数）；没开任何策略的天算 $0。",
          f"- 冷热特征：过去 {TRAIL[0]}、{TRAIL[1]} 个 5 分钟窗口的盈亏和，跳过刚结束的那个窗口（它在本窗口开盘时可能还没结算）。",
          f"- 模型：每个策略在它有成交的窗口上，两个目标（每份盈亏；其残差平方，即方差，预测波动不低于该策略每份盈亏标准差的"
          f" {VOL_FLOOR:g} 倍）× 岭回归（alpha {RIDGE_ALPHA:g}，标准化）和梯度提升树"
          f"（深 {HGB_PARAMS['max_depth']}、{HGB_PARAMS['max_leaf_nodes']} 叶、每叶 ≥ {HGB_PARAMS['min_samples_leaf']}、"
          f"学习率 {HGB_PARAMS['learning_rate']}、{HGB_PARAMS['max_iter']} 轮），两种模型取平均；训练只用在预测时刻前"
          f" ≥ {GAP_S} 秒已结束的窗口。",
          ("- 列名映射（标准名 ← 表里的列）：" + "，".join(f"{k} ← {v}" for k, v in mapping.items() if k != v) + "。")
          if any(k != v for k, v in mapping.items()) else "- 列名与标准名一致。", ""]
    return "\n".join(L)


# --------------------------------------------------------------------------- the run
def run(windows=WINDOWS, out=OUT, frozen=FROZEN, allow_partial_c=False, log=print, ledger=None):
    """Load, add trailing features, freeze on A, evaluate B (and C once), write the report. ledger: default
    regime-ledger.json beside `frozen`."""
    W, mapping, strats, missing = load_windows(windows, log)
    if not strats:
        raise ValueError("no strategy pnl column found (tried e.g. " + ", ".join(COLMAP["pnl_H"][:4]) + ")")
    log(f"{len(W):,} windows; strategies {strats}; missing env {missing}; mapping {mapping}")
    W = add_trailing(W, strats)
    A = W[W["segment"] == "A"].reset_index(drop=True)
    env = env_features(A)
    spec, written, preds = freeze(A, frozen, strats, env, log=log)
    spec = json.loads(Path(frozen).read_text(encoding="utf-8"))   # B / C use the file on disk only
    if spec["strategies"] != strats or spec["env_features"] != env:
        log("frozen file's strategies / features differ from this table: using the frozen ones")
        W = add_trailing(ensure_columns(W, spec["strategies"], spec["env_features"]), spec["strategies"])
        preds = None
    res = evaluate(W, spec, preds, allow_partial_c=allow_partial_c,
                   ledger=ledger_path(frozen) if ledger is None else ledger, log=log)
    text = report(W, spec, res, mapping, missing, written,
                  dict(windows=_rel(windows), frozen=_rel(frozen)), log)
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(text, encoding="utf-8")
        log(f"report -> {out}")
    return res, spec, text


def _rel(p):
    if isinstance(p, pd.DataFrame):
        return "（内存中的表）"
    p = Path(p)
    try:
        return str(p.resolve().relative_to(HERE))
    except ValueError:
        return str(p)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--windows", default=str(WINDOWS))
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--frozen", default=str(FROZEN))
    ap.add_argument("--allow-partial-c", action="store_true", help="open C even if a C day has no window")
    a = ap.parse_args(argv)
    run(a.windows, a.out, a.frozen, allow_partial_c=a.allow_partial_c)


if __name__ == "__main__":
    main()
