"""REGIME.md, the learning and the judging: from one row per BTC 5m window with each base strategy's
actual P&L in that window and the environment known at the window's open, learn when to switch
each strategy on, freeze the rule on A, then confirm it on B and (once) on C. Paper research on
market data only (no orders, no keys); it reads only the windows table.

    python regime_learn.py [--windows real/regime-windows.csv.gz] [--out real/regime-learn.md]
                           [--frozen real/regime-frozen.json] [--allow-partial-c]

The windows table (real/regime-windows.csv.gz) is written by regime_hf.py (the REGIME.md producer,
run on GitHub through cross.py). Expected columns, by their canonical names here (COLMAP below
maps the producer's names onto them, case-insensitively; the first alias present wins; every
mapping used is printed in the report):
- start: the window's open, UTC unix seconds (ms and date strings are converted). market (optional):
  the market's id. One row per window; a repeated start keeps the first row.
- per strategy s in H, follow, revert, direction, late, maker (REGIME.md's six base strategies;
  a strategy whose pnl column is absent is left out): pnl_s = the window's realised P&L in $ at 5
  shares a trade (after every fee), trades_s = trades, shares_s = shares bought, cost_s = $ spent
  buying (price + fee), caph_s = capital-hours ($ x hours tied up). NaN pnl = the strategy could not
  be evaluated in the window (no usable book): it is not a training row, and it counts as 0 (no
  trade) for every rule and control. 0 = evaluated, no trade.
- environment at the open (REGIME.md): rv5m, rv30m, rv1h (realised vol, any consistent scale), vr60
  (60-minute variance ratio, trend > 1 > range), dvol_rv (DVOL - realised vol), ret4h, vol_rel
  (Binance volume / its 24 h mean), funding, basis, hour (UTC), weekend (0 / 1), pm_spread
  (Polymarket opening spread), ask_size (shares at the best ask at the open), book_ups (book updates
  per second). Missing ones are NaN (said in the report); hour and weekend are derived from start
  when absent. Any other column is ignored (in particular, any trailing-performance column of the
  producer: the trailing features are rebuilt here).

What is done (REGIME.md, fixed 2026-10-04 before running; constants below):
1. Trailing features, per strategy s: hot12_s, hot48_s = the sum of pnl_s over the previous 12 / 48
   five-minute slots, NaN if none of those slots has a row. "Previous" skips the slot just ended
   (conservative: a window held to settlement resolves at or after the next window's open, so its
   P&L is not known at that open): for a window opening at t, the slots open in
   [t - (N + 1) x 300, t - 600]. Each strategy's model sees its own two (策略自身的冷热).
2. Models, per strategy: features = the environment columns (those not all NaN on A) + its own
   hot12, hot48. Two targets: the window's pnl (reward) and |pnl| (risk: the mean absolute P&L,
   robust to the fat tails of hold-to-settlement payoffs). Two models each, parameters fixed:
   ridge (alpha 10 on features standardised on the training rows, clipped at +-5 sd, NaN -> 0) and
   HistGradientBoostingRegressor (depth 3, 8 leaves, >= 200 rows a leaf, lr 0.05, 200 iterations,
   l2 1, no early stopping, random_state 0). Prediction = the mean of ridge and HGB (no choice of
   model on A: one less thing selected). Score = predicted pnl / max(predicted |pnl|, |predicted
   pnl|, $0.001), in [-1, 1] (predicted reward / risk, 预测盈亏比).
   A out of sample: 7-day blocks from 05-25; each block scored by models fit on the A rows of
   windows that ended at least one slot (300 s) before the block's start; the first 14 days only
   train (A out of sample = 06-08 .. 07-15); a fit needs >= 400 rows. B and C are scored by models
   fit on every A row (windows that ended >= 300 s before B's start), never refit on B or C.
3. Switch rule: in each window open every strategy whose score > threshold (several may be open);
   the threshold, one for all strategies, is the one of 0, 0.05, 0.1, 0.2 with the best mean daily
   P&L on the A out-of-sample days (ties: the higher threshold).
   Controls: each strategy always on; the hand trend / range switch (vr60 > 1: follow and direction
   on; vr60 <= 1: revert and maker on; H and late are off in it, NaN vr60: nothing on); the
   strategy with the best mean daily P&L over all of A, always on.
4. Freeze: the threshold, the best-on-A strategy, the feature list and every A candidate are written
   to real/regime-frozen.json by freeze(), which is given the A rows only (it refuses others),
   BEFORE any B or C row is scored; the evaluation reads the file back from disk. An existing frozen
   file is not overwritten: it is used as is, and C is opened only when the A rows have the same
   fingerprint as when it was written (delete it to re-freeze, and say so).
5. B: pass = mean daily P&L > 0, t >= 2 (days as clusters: t = mean / (sd / sqrt(days))), and the
   mean is above the best always-on control's mean daily P&L on B (the best of the six, picked on B
   itself: the harder bar). C (08-16 .. 08-29) is opened once, only if B passes and every C day is
   in the table with >= 144 windows (--allow-partial-c drops the second condition): pass = mean daily
   P&L > 0. While C is closed, nothing of C is reported (not even the base strategies).
6. Report (Chinese, units): per strategy and per regime state (trend / range x realised-vol tercile
   x sign of DVOL - RV): win rate, average win / average loss (盈亏比), per-share P&L, trades,
   shares, buying cost, P&L and capital tied up per day, max drawdown over the segment; then the
   switch against the controls per segment.

Where REGIME.md and the task are silent, the conservative choice made here:
- Segments by the window's open (UTC): A = 05-25 .. 07-15, B = 07-16 .. 08-15, C = 08-16 .. 08-29;
  rows outside are used only as history for the trailing features. Days = UTC days of the open
  with a row in the segment; a day on which a rule opens nothing counts as $0.
- Win rate and 盈亏比 are per traded window (trades > 0; without a trades column, shares > 0;
  without that, pnl != 0): a window's net P&L is one outcome.
- Per-share P&L = sum of pnl / sum of shares (cents a share). Capital tied up = sum of capital-hours
  / (24 x days): the time-averaged $ held. Max drawdown = the largest fall of the cumulative P&L
  (window by window, from $0 at the segment's start) below its running peak, in $.
- Regime states: vol terciles of rv30m with cut points from A, applied unchanged to B and C; a window
  with a NaN in vr60, rv30m or dvol_rv is in the state "缺数据".
- The A columns of the switch-vs-controls table are A's out-of-sample days (06-08 .. 07-15) for
  every rule (the switch has predictions only there); the best-on-A strategy is chosen on all of A.
- "Better than the best always-on control" compares the point estimates of the mean daily P&L; the
  paired difference (switch - that control, per day) and its t are reported beside it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
WINDOWS = HERE / "real" / "regime-windows.csv.gz"
OUT = HERE / "real" / "regime-learn.md"
FROZEN = HERE / "real" / "regime-frozen.json"

STRATS = ("H", "follow", "revert", "direction", "late", "maker")
QTYS = ("pnl", "trades", "shares", "cost", "caph")
ENV = ("rv5m", "rv30m", "rv1h", "vr60", "dvol_rv", "ret4h", "vol_rel", "funding", "basis", "hour", "weekend",
       "pm_spread", "ask_size", "book_ups")

_S_ALIAS = {"H": ("h", "stale", "hfair", "h_fair"), "follow": ("follow", "fol", "chase", "jump", "follow_scalp"),
            "revert": ("revert", "rev", "reversal", "reverse", "fade"),
            "direction": ("direction", "dir", "factor", "fac", "directional"),
            "late": ("late", "strong", "late_strong", "close", "nearcert"), "maker": ("maker", "mm", "make")}
_Q_ALIAS = {"pnl": ("pnl", "pnl5", "pnl_usd", "profit", "pnl_5"), "trades": ("trades", "n_trades", "ntrades", "n"),
            "shares": ("shares", "sh", "sh5", "shares5"), "cost": ("cost", "cost5", "cost_usd", "buy_cost"),
            "caph": ("caph", "caphr", "cap_h", "cap_hours", "capital_h", "capital_hours", "caphours", "cap_hr")}

# canonical name -> accepted column names (case-insensitive, first present wins)
COLMAP = {
    "start": ("start", "start_s", "window_start", "open_s", "t0", "ts", "start_ts"),
    "market": ("market", "market_id", "slug", "condition_id"),
    "rv5m": ("rv5m", "rv_5m", "rv5", "rv300"),
    "rv30m": ("rv30m", "rv_30m", "rv30", "rv1800"),
    "rv1h": ("rv1h", "rv_1h", "rv60m", "rv60", "rv3600"),
    "vr60": ("vr60", "vr_60", "vr60m", "vr"),
    "dvol_rv": ("dvol_rv", "dvol_minus_rv", "dvol_rv30", "ivrv", "iv_rv"),
    "ret4h": ("ret4h", "ret_4h", "r4h", "trend4h"),
    "vol_rel": ("vol_rel", "volume_rel", "vol_ratio", "volrel", "rel_volume", "vol24"),
    "funding": ("funding", "funding_rate", "fund"),
    "basis": ("basis", "basis_bp", "perp_basis"),
    "hour": ("hour", "hour_utc", "hod"),
    "weekend": ("weekend", "is_weekend", "wkend"),
    "pm_spread": ("pm_spread", "open_spread", "spread_open", "spread"),
    "ask_size": ("ask_size", "open_ask_size", "ask_sz", "ask_depth", "asksize"),
    "book_ups": ("book_ups", "book_rate", "updates_per_s", "book_hz", "upd_per_s", "ups"),
    **{f"{q}_{s}": tuple(dict.fromkeys([f"{q}_{s}"] + [f"{qa}_{sa}" for qa in _Q_ALIAS[q] for sa in _S_ALIAS[s]]
                                       + [f"{sa}_{qa}" for qa in _Q_ALIAS[q] for sa in _S_ALIAS[s]]))
       for s in STRATS for q in QTYS},
}

NAMES = {"H": "H（抢过期报价，持有到结算）", "follow": "跟随短打", "revert": "反转", "direction": "方向（开盘多因子）",
         "late": "收盘前强势方", "maker": "做市"}
TREND_ON, RANGE_ON = ("follow", "direction"), ("revert", "maker")

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
EPS_USD = 0.001
B_T = 2.0
C_MIN_WINDOWS = 144         # a C day must have at least this many windows to open the lockbox
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
def _to_seconds(x):
    x = pd.Series(x)
    if not pd.api.types.is_numeric_dtype(x):
        return (pd.to_datetime(x, utc=True).astype("int64") // 10 ** 9).astype(float).to_numpy()
    v = x.to_numpy(float)
    med = np.nanmedian(v) if np.isfinite(v).any() else 0.0
    return v / 1000.0 if med > 1e11 else v


def load_windows(src, log=print):
    """(windows frame with canonical columns sorted by start, mapping {canonical: source column},
    strategies present, missing environment columns). src: a path or a DataFrame."""
    raw = src.copy() if isinstance(src, pd.DataFrame) else pd.read_csv(src)
    lower = {}
    for c in raw.columns:
        lower.setdefault(str(c).lower(), c)
    mapping = {}
    for canon, aliases in COLMAP.items():
        for a in aliases:
            if a.lower() in lower:
                mapping[canon] = lower[a.lower()]
                break
    if "start" not in mapping:
        raise ValueError(f"no window-start column (tried {COLMAP['start']}); columns: {list(raw.columns)[:40]}")
    W = pd.DataFrame({"start": _to_seconds(raw[mapping["start"]])})
    W["market"] = raw[mapping["market"]].astype(str).to_numpy() if "market" in mapping else W["start"].astype(
        np.int64).astype(str)
    strats = [s for s in STRATS if f"pnl_{s}" in mapping]
    for s in strats:
        for q in QTYS:
            c = mapping.get(f"{q}_{s}")
            W[f"{q}_{s}"] = pd.to_numeric(raw[c], errors="coerce").to_numpy(float) if c else np.nan
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
        with np.errstate(invalid="ignore"), __import__("warnings").catch_warnings():
            __import__("warnings").simplefilter("ignore", RuntimeWarning)
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


class Scorer:
    """Ridge + HGB for pnl and for |pnl| of one strategy; score = reward / risk."""

    def fit(self, X, y):
        self.m = [make_model(k).fit(X, y) for k in MODELS]
        self.a = [make_model(k).fit(X, np.abs(y)) for k in MODELS]
        return self

    def parts(self, X):
        """{model: (pred pnl, pred |pnl|)} and the averaged pair."""
        out = {k: (m.predict(X), a.predict(X)) for k, m, a in zip(MODELS, self.m, self.a)}
        mean = np.mean([v[0] for v in out.values()], axis=0)
        absd = np.mean([v[1] for v in out.values()], axis=0)
        return out, mean, absd

    def score(self, X):
        return rr_score(*self.parts(X)[1:])


def rr_score(m, a):
    """Predicted pnl / max(predicted |pnl|, |predicted pnl|, EPS): in [-1, 1], NaN where m or a is."""
    m, a = np.asarray(m, float), np.asarray(a, float)
    den = np.fmax(np.fmax(a, np.abs(m)), EPS_USD)
    out = m / den
    out[~(np.isfinite(m) & np.isfinite(a))] = np.nan
    return out


def walk_forward(X, y, start):
    """A out-of-sample scores (and per-model parts): each out-of-sample block scored by models fit on
    the rows that ended >= GAP_S before the block's start; NaN elsewhere or when < MIN_TRAIN rows."""
    start = np.asarray(start, float)
    n = len(y)
    sc = np.full(n, np.nan)
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
        sc[te] = rr_score(m, a)
        for k in MODELS:
            parts[k][0][te], parts[k][1][te] = p[k]
    return sc, parts


def a_predictions(A, strats, env, log=print):
    """{s: (A out-of-sample score, per-model parts)} from A rows only."""
    if len(A) and (segment_of(A["start"].to_numpy(float)) != "A").any():
        raise ValueError("a_predictions is given rows outside A")
    out = {}
    t0 = time.time()
    for s in strats:
        X = A[features_for(s, env)].to_numpy(float)
        out[s] = walk_forward(X, A[f"pnl_{s}"].to_numpy(float), A["start"].to_numpy(float))
        log(f"  walk-forward {s}: {time.time() - t0:.0f} s")
    return out


def fit_final(A, strats, env):
    """{s: Scorer fit on every A row that ended >= GAP_S before B starts} (None when too few rows)."""
    b0 = ts(SEGMENTS[1][1])
    out = {}
    for s in strats:
        X = A[features_for(s, env)].to_numpy(float)
        y = A[f"pnl_{s}"].to_numpy(float)
        tr = trainable(A["start"].to_numpy(float), y, b0)
        out[s] = Scorer().fit(X[tr], y[tr]) if tr.sum() >= MIN_TRAIN else None
    return out


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
    t = m / se if se and np.isfinite(se) and se > 0 else np.nan
    return dict(mean=m, se=se, t=float(t), n=n, pos=float((x > 0).mean()))


def max_drawdown(pnl):
    """Largest fall ($) of the cumulative P&L below its running peak (from $0)."""
    c = np.concatenate([[0.0], np.cumsum(np.asarray(pnl, float))])
    return float(np.max(np.maximum.accumulate(c) - c))


def perf(f, days, caph_ok=True, shares_ok=True):
    """Units of a rule frame or a single strategy's frame over `days` (UTC day numbers)."""
    nd = len(days)
    f = f[f["day"].isin(days)].sort_values("start", kind="stable")
    tr = f[f["traded"].to_numpy(bool)]
    pnl = tr["pnl"].to_numpy(float)
    win, loss = pnl[pnl > 0], pnl[pnl < 0]
    sh = f["shares"].sum()
    out = dict(days=nd, windows=len(f), traded=len(tr), win=float((pnl > 0).mean()) if len(tr) else np.nan,
               avg_win=float(win.mean()) if len(win) else np.nan, avg_loss=float(loss.mean()) if len(loss) else np.nan,
               per_share=float(f["pnl"].sum() / sh) if shares_ok and sh > 0 else np.nan,
               trades=f["trades"].sum() / nd if nd else np.nan, shares=sh / nd if nd and shares_ok else np.nan,
               cost=f["cost"].sum() / nd if nd else np.nan, pnl_day=f["pnl"].sum() / nd if nd else np.nan,
               cap=f["caph"].sum() / (24 * nd) if nd and caph_ok else np.nan, mdd=max_drawdown(f["pnl"].to_numpy()))
    out["ratio"] = out["avg_win"] / -out["avg_loss"] if np.isfinite(out["avg_win"]) and np.isfinite(
        out["avg_loss"]) else np.nan
    out.update({f"d_{k}": v for k, v in day_t(daily(f, days).to_numpy()).items()})
    return out


def switch_open(scores, thr):
    """{s: score > thr} (NaN score: closed)."""
    return {s: np.isfinite(v) & (v > thr) for s, v in scores.items()}


def hand_open(W, strats):
    vr = W["vr60"].to_numpy(float)
    tr, rg = np.isfinite(vr) & (vr > 1), np.isfinite(vr) & (vr <= 1)
    return {s: tr if s in TREND_ON else (rg if s in RANGE_ON else np.zeros(len(W), bool)) for s in strats}


def days_of(W, mask=None):
    d = W["day"].to_numpy() if mask is None else W["day"].to_numpy()[mask]
    return np.unique(d)


# --------------------------------------------------------------------------- freeze and evaluation
def fingerprint(A, strats, env):
    """sha256 of what the freeze depends on: A's starts, features and labels."""
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
    vr = A["rv30m"].to_numpy(float) if "rv30m" in A else np.array([])
    vr = vr[np.isfinite(vr)]
    cuts = [float(np.quantile(vr, 1 / 3)), float(np.quantile(vr, 2 / 3))] if len(vr) else None
    spec = dict(made=pd.Timestamp.now(tz="UTC").isoformat(), design="REGIME.md", segments=SEGMENTS,
                a_oos_from=pd.Timestamp(a_oos_from(), unit="s").strftime("%Y-%m-%d"), strategies=list(strats),
                env_features=list(env), trailing=list(TRAIL), trailing_lag_slots=TRAIL_LAG, gap_s=GAP_S,
                models={"ridge": {"alpha": RIDGE_ALPHA}, "hgb": HGB_PARAMS}, score="mean(ridge,hgb) pnl / "
                "max(mean(ridge,hgb) |pnl|, |pnl pred|, 0.001)", thresholds=list(THRESHOLDS),
                threshold=None if best is None else best["threshold"], candidates=cand, best_on_a=best_s,
                always_on_a=always, vol_cuts=cuts, a_rows=int(len(A)), a_days=int(len(alld)),
                a_oos_days=int(len(days)), a_fingerprint=fingerprint(A, strats, env), b_t=B_T,
                c_min_windows=C_MIN_WINDOWS)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(spec, indent=1, ensure_ascii=False, default=float), encoding="utf-8")
    log(f"frozen threshold {spec['threshold']} best-on-A {best_s} -> {path}")
    return json.loads(path.read_text(encoding="utf-8")), True, preds


def c_complete(W):
    """Every C day present with >= C_MIN_WINDOWS windows."""
    c0, c1 = ts(SEGMENTS[2][1]), ts(SEGMENTS[2][2])
    want = np.arange(c0 // DAY, c1 // DAY)
    cnt = W[W["segment"] == "C"].groupby("day").size().reindex(want, fill_value=0)
    return bool((cnt >= C_MIN_WINDOWS).all())


def rules_for(S, strats, scores, spec):
    """{rule name: opened masks} on the rows S."""
    out = {"switch": switch_open(scores, spec["threshold"]) if spec.get("threshold") is not None
           else {s: np.zeros(len(S), bool) for s in strats},
           "hand": hand_open(S, strats)}
    if spec.get("best_on_a") in strats:
        out["best_a"] = {spec["best_on_a"]: np.ones(len(S), bool)}
    for s in strats:
        out[f"on_{s}"] = {s: np.ones(len(S), bool)}
    return out


def evaluate(W, spec, preds=None, allow_partial_c=False, log=print):
    """A out of sample, B, and C (once, if B passes) of the frozen switch and its controls."""
    strats, env = spec["strategies"], spec["env_features"]
    A = W[W["segment"] == "A"].reset_index(drop=True)
    fp_ok = spec.get("a_fingerprint") == fingerprint(A, strats, env)
    if preds is None or not fp_ok:
        preds = a_predictions(A, strats, env, log)
    final = fit_final(A, strats, env)
    caph_ok, shares_ok = has_qty(W, strats, "caph"), has_qty(W, strats, "shares")
    res = dict(segments={}, b_pass=False, c_open=False, c_pass=None, fp_ok=fp_ok, c_complete=c_complete(W))
    oos = A["start"].to_numpy(float) >= a_oos_from()
    for name in ("A", "B", "C"):
        if name == "A":
            S = A[oos].reset_index(drop=True)
            scores = {s: preds[s][0][oos] for s in strats}
        else:
            if name == "C":
                b = res["segments"].get("B")
                res["b_pass"] = bool(b and b["pass"])
                if not (res["b_pass"] and fp_ok and (res["c_complete"] or allow_partial_c)):
                    break
                res["c_open"] = True
            S = W[W["segment"] == name].reset_index(drop=True)
            scores = {}
            for s in strats:
                X = S[features_for(s, env)].to_numpy(float)
                scores[s] = final[s].score(X) if final[s] is not None and len(S) else np.full(len(S), np.nan)
        days = days_of(S)
        rules = rules_for(S, strats, scores, spec)
        frames = {k: rule_frame(S, v) for k, v in rules.items()}
        stats = {k: perf(f, days, caph_ok, shares_ok) for k, f in frames.items()}
        dd = {k: daily(f, days).to_numpy() for k, f in frames.items()}
        on = [f"on_{s}" for s in strats]
        best_on = max(on, key=lambda k: (stats[k]["d_mean"] if np.isfinite(stats[k]["d_mean"]) else -np.inf, k)) \
            if on else None
        diff = day_t(dd["switch"] - dd[best_on]) if best_on else day_t([])
        sw = stats["switch"]
        seg = dict(S=S, days=days, scores=scores, stats=stats, best_on=best_on, diff=diff, rules=rules,
                   frames=frames)
        if name == "B":
            seg["pass"] = bool(sw["d_n"] and sw["d_mean"] > 0 and np.isfinite(sw["d_t"]) and sw["d_t"] >= B_T
                               and (best_on is None or sw["d_mean"] > stats[best_on]["d_mean"]))
        if name == "C":
            res["c_pass"] = bool(sw["d_n"] and sw["d_mean"] > 0)
        res["segments"][name] = seg
        log(f"{name}: switch {sw['d_mean']:+.2f} $/day (t {sw['d_t']:.1f}, {sw['d_n']} days); best always-on "
            f"{best_on} {stats[best_on]['d_mean']:+.2f}" if best_on else f"{name}: switch {sw['d_mean']:+.2f} $/day")
    return res


# --------------------------------------------------------------------------- regime states
def state_labels(W, cuts):
    """趋势/震荡 x 波动三分位 x DVOL−RV 符号, '缺数据' when a component is NaN."""
    vr, rv, iv = (W[c].to_numpy(float) for c in STATE_COLS)
    ok = np.isfinite(vr) & np.isfinite(rv) & np.isfinite(iv) & (cuts is not None)
    tr = np.where(vr > 1, "趋势", "震荡")
    if cuts is None:
        terc = np.full(len(W), "")
    else:
        terc = np.where(rv <= cuts[0], "低波", np.where(rv <= cuts[1], "中波", "高波"))
    sg = np.where(iv > 0, "DVOL>RV", "DVOL≤RV")
    lab = np.char.add(np.char.add(np.char.add(np.char.add(tr.astype(str), "·"), terc.astype(str)), "·"),
                      sg.astype(str))
    return np.where(ok, lab, "缺数据")


STATE_ORDER = [f"{a}·{b}·{c}" for a in ("趋势", "震荡") for b in ("低波", "中波", "高波") for c in ("DVOL>RV", "DVOL≤RV")]


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
             "利润 $/天（± 标准误，按天 t） | 占用资金 $（时间平均） | 最大回撤 $ |")
PERF_SEP = "|---:|---:|---:|---|---:|---:|---:|---:|---|---:|---:|"


def _perf_cells(p):
    ratio = f"{_usd(p['avg_win'])} / {_usd(p['avg_loss'])}（{_num(p['ratio'], 2)}）"
    pl = f"{_usd(p['pnl_day'])}（±{_num(p['d_se'], 2)}，t {_t(p['d_t'])}）"
    return (f"{p['days']} | {p['traded']:,} | {_pct(p['win'])} | {ratio} | {_cents(p['per_share'])} | "
            f"{_num(p['trades'])} | {_num(p['shares'], 0)} | {_usd(p['cost'], False)} | {pl} | "
            f"{_usd(p['cap'], False)} | {_usd(p['mdd'], False)} |")


RULE_NAMES = {"switch": "**学习切换**", "hand": "手工趋势/震荡切换", "best_a": "A 段最好的一个一直开"}


def _rule_name(k, spec):
    if k == "best_a":
        return f"{RULE_NAMES[k]}（{NAMES.get(spec.get('best_on_a'), spec.get('best_on_a'))}）"
    if k.startswith("on_"):
        return f"一直开：{NAMES.get(k[3:], k[3:])}"
    return RULE_NAMES.get(k, k)


def report(W, spec, res, mapping, missing, written, paths, log=print):
    strats = spec["strategies"]
    L = ["# 按状态学会何时开哪个策略（REGIME.md 的学习和判定）", ""]
    segs = res["segments"]
    n_seg = {k: int((W["segment"] == k).sum()) for k in "ABC"}
    L += [f"数据：`{paths['windows']}`，每个 5 分钟窗口一行；A {n_seg['A']:,} 个窗口，B {n_seg['B']:,} 个，"
          f"C {'%s 个' % format(n_seg['C'], ',') if res['c_open'] else '（锁箱未开，不报告）'}。"
          f"金额都是每笔 5 份时的美元（含两腿手续费），每份盈亏用美分。", ""]
    L += [f"策略：{'、'.join(NAMES[s] for s in strats)}" + (f"；缺：{'、'.join(NAMES[s] for s in STRATS if s not in strats)}"
                                                         if len(strats) < len(STRATS) else "") + "。",
          f"环境特征（用于模型）：{', '.join(spec['env_features'])}" + (f"；表里没有：{', '.join(missing)}" if missing else "")
          + f"；另加每个策略自己过去 {TRAIL[0]}、{TRAIL[1]} 个窗口（跳过刚结束的那个）的实际盈亏。", ""]
    # verdict
    b = segs.get("B")
    L += ["## 判定", ""]
    if b is not None:
        sw, bo = b["stats"]["switch"], b["stats"][b["best_on"]] if b["best_on"] else None
        L += [f"- B（{SEGMENTS[1][1][5:]} – 08-15）：学习切换每天 {_usd(sw['d_mean'])}（t {_t(sw['d_t'])}，{sw['d_n']} 天），"
              f"最好的一直开（{NAMES.get(b['best_on'][3:], '') if b['best_on'] else '–'}）每天 "
              f"{_usd(bo['d_mean']) if bo else '–'}；差 {_usd(b['diff']['mean'])}/天（t {_t(b['diff']['t'])}）。"
              f"要求：> 0、t ≥ {B_T:g}、好于最好的一直开 → **{'通过' if res['b_pass'] else '未通过'}**。"]
    if res["c_open"]:
        c = segs["C"]["stats"]["switch"]
        L += [f"- C（08-16 – 08-29，只算一次）：每天 {_usd(c['d_mean'])}（t {_t(c['d_t'])}，{c['d_n']} 天）→ "
              f"**{'通过' if res['c_pass'] else '未通过'}**。"]
    else:
        why = ("B 未通过" if not res["b_pass"] else ("A 段数据和冻结时不同（指纹不符）" if not res["fp_ok"]
                                                  else "C 段不完整（有的天少于 %d 个窗口）" % C_MIN_WINDOWS))
        L += [f"- C：锁箱没开（{why}）。"]
    L += [""]
    # frozen rule
    L += ["## 冻结的规则（只用 A，写入后才算 B / C）", "",
          f"冻结文件 `{paths['frozen']}`，{'本次写入' if written else '沿用已有文件'}（{spec.get('made', '')[:19]} UTC）。"
          f"门槛 = **{spec.get('threshold')}**（预测盈亏比 = 预测每窗口盈亏 ÷ 预测每窗口 |盈亏|，岭回归和梯度提升树平均）；"
          f"A 段最好的单个策略：{NAMES.get(spec.get('best_on_a'), '–')}。", "",
          "| 门槛 | A 样本外每天盈亏 $ | ± 标准误 | t | 盈利天占比 | 天数 | 有成交的窗口 |", "|---:|---:|---:|---:|---:|---:|---:|"]
    for c in spec.get("candidates", []):
        L += [f"| {c['threshold']:g} | {_usd(c['mean'])} | {_num(c['se'], 2)} | {_t(c['t'])} | {_pct(c['pos'])} | "
              f"{c['n']} | {c['traded']:,} |"]
    L += ["", "A 全段每个策略一直开（挑“A 段最好的一个”用）：" + "；".join(
        f"{NAMES[s]} {_usd(v['mean'])}/天" for s, v in spec.get("always_on_a", {}).items()) + "。", ""]
    # switch vs controls
    L += ["## 学习切换 vs 对照（每段）", "",
          f"A = A 段样本外的天（{spec['a_oos_from'][5:]} – 07-15，模型按周滚动）；B / C 用在全部 A 上拟合的模型。", ""]
    for name, seg in segs.items():
        L += [f"### {name} 段", "", "| 规则 " + PERF_HEAD, "|---" + PERF_SEP]
        for k, p in seg["stats"].items():
            L += [f"| {_rule_name(k, spec)} | " + _perf_cells(p)]
        L += [""]
    # what the switch opened
    L += ["## 学习切换打开了什么（每个策略）", "",
          "| 段 | 策略 | 打开的窗口占比 | 打开时 利润 $/天 | 没打开时 利润 $/天 | 打开时 每份盈亏 | 没打开时 每份盈亏 |",
          "|---|---|---:|---:|---:|---:|---:|"]
    for name, seg in segs.items():
        S, days = seg["S"], seg["days"]
        for s in strats:
            m = seg["rules"]["switch"][s]
            po = perf(rule_frame(S, {s: m}), days)
            pc = perf(rule_frame(S, {s: ~m}), days)
            L += [f"| {name} | {NAMES[s]} | {_pct(m.mean() if len(m) else np.nan)} | {_usd(po['pnl_day'])} | "
                  f"{_usd(pc['pnl_day'])} | {_cents(po['per_share'])} | {_cents(pc['per_share'])} |"]
    L += [""]
    # per strategy overall
    show = [k for k in ("A", "B", "C") if k in segs]
    full = {k: W[W["segment"] == k].reset_index(drop=True) for k in show}
    L += ["## 每个策略（全段一直开）", "", f"A 这里是 A 全段（{SEGMENTS[0][1][5:]} – 07-15）。", "",
          "| 策略 | 段 " + PERF_HEAD, "|---|---" + PERF_SEP]
    for s in strats:
        for k in show:
            S = full[k]
            L += [f"| {NAMES[s]} | {k} | " + _perf_cells(perf(rule_frame(S, {s: np.ones(len(S), bool)}), days_of(S)))]
    L += [""]
    # states
    cuts = spec.get("vol_cuts")
    L += ["## 每个状态下的每个策略", "",
          "状态 = 趋势（过去 60 分钟方差比 VR60 > 1）或震荡（≤ 1）× 30 分钟已实现波动三分位（切点来自 A："
          + (f"{cuts[0]:.4g}、{cuts[1]:.4g}" if cuts else "无") + "）× DVOL − 已实现波动的符号。"
          "每天的量 = 这个状态里的总量 ÷ 该段全部天数（即这个状态每天贡献多少）。", ""]
    labs = {k: state_labels(full[k], cuts) for k in show}
    order = STATE_ORDER + ["缺数据"]
    L += ["### 利润 $/天 一览（行 = 状态，列 = 策略）", "",
          "| 状态 | 段 | 窗口占比 | " + " | ".join(NAMES[s] for s in strats) + " |",
          "|---|---|---:|" + "---:|" * len(strats)]
    for st in order:
        for k in show:
            m = labs[k] == st
            if not m.any():
                continue
            S, days = full[k], days_of(full[k])
            cells = [_usd(perf(rule_frame(S, {s: m}), days)["pnl_day"]) for s in strats]
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
                L += [f"| {st} | {k} | " + _perf_cells(perf(rule_frame(S, {s: m}), days_of(S)))]
        L += [""]
    L += ["## 口径", "",
          "- 一行 = 一个 5 分钟窗口；每个策略在这个窗口里的实际盈亏（每笔 5 份，含手续费，持有到结算的按官方结果）。",
          "- 胜率、平均赢 / 平均亏（盈亏比）：按有成交的窗口算，一个窗口的净盈亏算一次。",
          "- 每份盈亏 = 总盈亏 ÷ 总份数。占用资金 = 资金 × 小时 的总和 ÷（24 × 天数），即时间平均占用的美元。",
          "- 最大回撤：按窗口时间顺序累计盈亏，从该段开头的 $0 起，低于此前最高点的最大跌幅（美元）。",
          "- 按天 t：每天的盈亏当一个样本，t = 平均 ÷（标准差 ÷ √天数）；没开任何策略的天算 $0。",
          f"- 模型：每个策略两个目标（盈亏、|盈亏|）× 岭回归（alpha {RIDGE_ALPHA:g}，标准化）和梯度提升树"
          f"（深 {HGB_PARAMS['max_depth']}、{HGB_PARAMS['max_leaf_nodes']} 叶、每叶 ≥ {HGB_PARAMS['min_samples_leaf']}、"
          f"学习率 {HGB_PARAMS['learning_rate']}、{HGB_PARAMS['max_iter']} 轮），两种模型取平均；训练只用在预测时刻前"
          f" ≥ {GAP_S} 秒已结束的窗口。",
          "- 列名映射（标准名 ← 表里的列）：" + "，".join(f"{k} ← {v}" for k, v in mapping.items() if k != v) + "。"
          if any(k != v for k, v in mapping.items()) else "- 列名与标准名一致。", ""]
    return "\n".join(L)


# --------------------------------------------------------------------------- the run
def run(windows=WINDOWS, out=OUT, frozen=FROZEN, allow_partial_c=False, log=print):
    """Load, add trailing features, freeze on A, evaluate B (and C once), write the report."""
    W, mapping, strats, missing = load_windows(windows, log)
    if not strats:
        raise ValueError("no strategy pnl column found (tried e.g. " + ", ".join(COLMAP["pnl_H"][:4]) + ")")
    log(f"{len(W):,} windows; strategies {strats}; missing env {missing}")
    W = add_trailing(W, strats)
    A = W[W["segment"] == "A"].reset_index(drop=True)
    env = env_features(A)
    spec, written, preds = freeze(A, frozen, strats, env, log=log)
    spec = json.loads(Path(frozen).read_text(encoding="utf-8"))   # B / C use the file on disk only
    if spec["strategies"] != strats or spec["env_features"] != env:
        log("frozen file's strategies / features differ from this table: using the frozen ones")
        strats = [s for s in spec["strategies"] if f"pnl_{s}" in W]
        missing_cols = [e for e in spec["env_features"] if e not in W]
        for e in missing_cols:
            W[e] = np.nan
        preds = None
    res = evaluate(W, spec, preds if spec["strategies"] == strats and spec["env_features"] == env else None,
                   allow_partial_c=allow_partial_c, log=log)
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
    ap.add_argument("--allow-partial-c", action="store_true", help="open C even if a C day has < 144 windows")
    a = ap.parse_args(argv)
    run(a.windows, a.out, a.frozen, allow_partial_c=a.allow_partial_c)


if __name__ == "__main__":
    main()
