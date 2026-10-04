"""MIX.md on the Hugging Face books: rescue signals that carry information but lose it to costs, by
cheaper exits (resting sells, settlement) and by stacking weak signals into one score, trading only
its top few percent. Paper research on market data only (no orders, no keys).

Data: whodisidk/polymarket-btc-updown-exchange-data (BTC 5m, 2026-05-25 .. 08-29: 100 ms books of
both tokens, every Polymarket print with its taker side, every Binance BTCUSDT print with trade and
recorder times). Runs on GitHub as `python cross.py mix [N] --workdir DIR --out real/....md`
(ci/cross.request, the polymarket-cross lane). The factor-model predictions and the hourly DVOL it
needs are shipped in real/mix-inputs/ (built locally by `build_inputs`, see below).

Pipeline (MIX.md; every constant below was fixed before anything was run):
1. Per daily archive (downloaded, read and deleted one at a time; per-day rows written to
   <workdir>/mix-shards/<day>.parquet, then concatenated): BTC 5m markets with a final official
   outcome (jump2s_hf.final_resolution: provisional resolution rows dropped; shrink_markets first).
2. Decision points (times are the recorder's receipt clock, as cross.stale_trades / jump2s_hf):
   - "jump": a Binance print whose log price moved >= 2 bp from the last print at least 1 s and at
     most 5 s earlier (by trade time); decision time t = its receipt time; 285 >= end - t >= 20 s
     (inclusive); per market a point is kept only >= 5 s after the last kept one (greedy, in t order).
   - "fixed": t = end - 240, 180, 120, 60 s in every market.
   - "random" (cost-structure control only; never modelled or traded by a rule): one per jump point
     of the same market, t uniform in [end - 285, end - 20], side +-1 at random, seeded by
     zlib.crc32(market id) (so a market's draws do not depend on the other markets).
   Every jump / fixed point gives two rows, one per side (Up = +1, Down = -1).
3. Features at t, only data received at or before t (side-relative: "s" is the row's side):
   jump_bp = s x d (bp), jump_abs_bp = |d|, jump_z = s x d / sigma, dir = sign(s x d), is_jump,
   where d = log move of the last Binance print received by t against its 1-5 s reference (for a
   jump row the trigger itself) and sigma the per-second vol of the 600 s before (jump2s.Seconds,
   gaps up to 20 s forward filled, as jump2s_hf); h_edge = H fair value - the side's ask at t, where
   H fair (fill_rate.sends) = Phi(Phi^-1(Up mid 2 s earlier, clipped 0.005..0.995) + Binance log
   move over those 2 s / (sigma x f)) for Up, 1 - that for Down, f = sqrt(seconds left) for
   point-price markets, binary.twap_std_factor for the TWAP ones (rule window by
   pm_outcomes.rule_window: point before 08-07, 30 s 08-07 .. 08-13, 60 s from 08-14); dmid2, dmid10
   = the side's own mid now minus 2 s / 10 s earlier; imb = (bid size - ask size) / (sum) at the
   side's best; spread = ask - bid; price = the side's ask; fee = 0.07 p (1 - p); tau = seconds
   left; trend_agree = sign(d) x sign(ret4h) (is the move with the 4 h trend), trend_side =
   sign(s x ret4h); vr60 (jump2s.trend_features on Binance 1 m klines from data.binance.vision,
   minutes closed by the exchange time of the last print received by t); f_ridge5, f_hgb5,
   f_ridge15, f_hgb15 = s x (P(up) - 0.5) of the factor study's walk-forward out-of-sample
   predictions for the window starting at the market's open (FACTORS.md; known at the open);
   dvol_rv = DVOL / 100 (last hourly candle closed by t, known at its hour's end) - rv30
   (annualised std of the 1 s log returns of the 1,800 s before the second before the last print,
   at least 900 present), as factors.dvol_minus_rv30.
4. Execution labels, per share, for every row:
   - Entry: decided at t, filled at te = t + 0.5 s at the side's best ask in the last snapshot at
     or before te (<= 1 s old), which must be active, not halted, not crossed on that token
     (bid < ask), fresh (the market's top of book changed <= 1 s before te, as jump2s_hf), ask in
     0.02..0.98 with >= 5 shares at it. No such snapshot -> no row. Taker fee 0.07 p (1 - p).
   - "tk{h}" (h = 5, 10, 30, 60 s): sell decided at te + h, filled at tx = te + h + 0.5 s at the
     side's best bid in the snapshot at or before tx (same checks: active, fresh, not crossed, bid
     > 0 with >= 5 shares), taker fee on the bid; if tx is not before the market's end or there is
     no such bid, held to settlement.
   - "mk{x}_{h}" (x = 1, 2, 3, 5 c; h as above): a resting sell at L = min(entry price + x, 0.99),
     no fee. It counts as filled at the first moment in (tl, min(te + h, end)] when a Polymarket
     print on that token is a taker BUY at a price >= L, or a snapshot (active, not crossed) shows
     the token's best bid >= L; tl = te + 0.5 s is when the order is live (see below). Not filled
     by te + h: cancelled, and sold as in tk{h} (else settlement).
   - "settle": held to the official result.
   pnl = proceeds - entry price - every taker fee. Each row also keeps, per policy, the holding time
   (entry fill to exit fill, or to the market's end when held) and the 20-share size (see units).
5. After all days, segments by market start (UTC): A = 05-25 .. 07-15, B = 07-16 .. 08-15, C = 08-16
   .. 08-29. Models per policy (21 policies; rows = jump and fixed rows): ridge (alpha 10 on
   features standardised on the training rows, clipped at +-5 sd, NaN -> 0) and
   HistGradientBoostingRegressor (depth 3, 8 leaves, >= 400 per leaf, lr 0.05, 200 iterations,
   l2 1, no early stopping, random_state 0), both predicting the policy's per-share pnl. A
   out-of-sample: 7-day blocks from 05-25; each block scored by models fit on every A row of
   markets that ended before the block starts; the first 14 days only train (out-of-sample A =
   06-08 .. 07-15). Rules = (policy, model, top q in 20, 10, 5, 2 %), q cut = the score at the
   top q of A's out-of-sample scores. The best 3 by A out-of-sample per-share pnl (>= 300 A trades)
   are frozen to <prefix>frozen.json BEFORE anything of B or C is read (freeze() is given the A rows
   only and the file is read back from disk for the evaluation). Then models fit on all of A score
   B and C. B: mean > 0 and one-sided p < 0.05 / 3 (SE clustered by market). C, only for the rules
   that pass B, once: mean > 0 and p < 0.05.
   Single-signal controls: for each frozen rule, the same policy and q with the single feature
   (either direction) whose top q on the same A out-of-sample rows had the best per-share pnl
   (>= 300 trades), frozen with the rules and evaluated beside them.
6. Report (Chinese, with units) and the traded rows of the frozen rules and their controls.

Where MIX.md and the task are silent, the conservative choice made here:
- Resting sell latency: MIX.md says the sell is posted immediately after the buy fills; it is
  counted live 0.5 s after the entry fill (the same decision-to-match latency as every order
  here), not at the fill itself. If the book at that moment already shows the token's best bid >=
  L, the order would cross: it is filled at that bid as a taker (fee paid), not as a maker.
- A print counts for a maker fill only when its taker side is "buy" on the bought token (a sell of
  the other token, a merge, is not counted), price >= L, receipt time after the order is live.
- Crossed snapshots (bid >= ask on the token) are never used for an entry, an exit or a maker
  fill; at an exit they mean "no bid" (held to settlement).
- Book features at t use the last active snapshot at or before t (<= 1 s old) whatever its
  freshness (what a trader sees), NaN when crossed or missing; every price paid or received needs a
  fresh one.
- Each selected row is one trade (a market can be traded at several decision points, and both
  sides of one point can be selected); statistics are clustered by market.
- Units: 5 shares a trade (the ask shows >= 5), and min(20, ask size at entry, bid size at a taker
  exit) shares; a maker fill is assumed to fill the whole order at L. Capital is the entry cost
  (price + fee) x shares, tied from the entry fill to the exit fill, or to the market's end when
  held to settlement (the payout comes later; ignored). Days = UTC days with rows in the segment
  (for A out-of-sample: days in 06-08 .. 07-15).
- Single-signal controls: features whose top-q selection takes more than 1.5 q of the rows
  (ties: dir, is_jump, trend_agree, trend_side) are not candidates.
- Rows of markets that start outside 05-25 .. 08-29 are dropped; duplicate rows (a market in two
  archives) are dropped.
- A frozen file that already exists is not overwritten: it is used as is, and C is evaluated only
  when the A rows have the same fingerprint as when it was written (delete it to re-freeze, and say
  so). A run cut short by the time budget (BUDGET_S) analyses what it read, but evaluates C only
  if every archive of C was read.
- Factor predictions: the factor study's own walk-forward out-of-sample predictions (A + B of
  FACTORS.md, up to 08-15); from 08-16 the same walk-forward continued (each 7-day block of the
  factor study's C fit on rows whose target ended before the block, its ridge alpha and HGB
  settings), as factors.lockbox does for its frozen model. Missing -> NaN.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
import zlib
from pathlib import Path

import numpy as np
import pandas as pd

import jump2s as j2
import jump2s_hf as hf

HERE = Path(__file__).resolve().parent
INPUTS = HERE / "real" / "mix-inputs"

# --------------------------------------------------------------------------- MIX.md constants
JUMP_BP = 2.0
REF_MIN_S, REF_MAX_S = 1.0, 5.0
SPACING_S = 5.0
TAU_HI, TAU_LO = 285.0, 20.0
FIXED_TAUS = (240, 180, 120, 60)
LAT_S = 0.5                 # decision -> fill (entry and taker exit)
MAKER_LIVE_S = 0.5          # entry fill -> resting sell live
HOLDS = (5, 10, 30, 60)
MAKER_X = (1, 2, 3, 5)      # cents
MAKER_CAP = 0.99
FEE_RATE = 0.07
MIN_SHARES = 5.0
BAND = (0.02, 0.98)
BOOK_MAX_AGE_S = 1.0
PRICE_MAX_AGE_S = 5.0
H_ANCHOR_S = 2.0
RV_N, RV_MIN = 1800, 900
GAP_FILL_S = hf.GAP_FILL_S
CARRY_S = 1800
SEC_YEAR = 365 * 86400
T_EPS = 1e-6
DAY = 86400

SEGMENTS = (("A", "2026-05-25", "2026-07-16"), ("B", "2026-07-16", "2026-08-16"), ("C", "2026-08-16", "2026-08-30"))
BLOCK_DAYS, MIN_TRAIN_DAYS = 7, 14
QS = (0.20, 0.10, 0.05, 0.02)
N_PICK, MIN_A_TRADES = 3, 300
RIDGE_ALPHA = 10.0
HGB_PARAMS = dict(max_depth=3, max_leaf_nodes=8, min_samples_leaf=400, learning_rate=0.05, max_iter=200,
                  l2_regularization=1.0, early_stopping=False, random_state=0)
MODELS = ("ridge", "hgb")
B_ALPHA, C_ALPHA = 0.05 / 3, 0.05
TIE_MAX = 1.5               # a control's top-q may take at most 1.5 q of the rows
BUDGET_S = 285 * 60         # stop reading archives after this (the lane has 330 min)
FETCH_TRIES = 3
UNITS = (5, 20)

POLICIES = (["settle"] + [f"tk{h}" for h in HOLDS] + [f"mk{x}_{h}" for x in MAKER_X for h in HOLDS])
POLICY_NAMES = {"settle": "持有到结算", **{f"tk{h}": f"{h} 秒后吃单卖" for h in HOLDS},
                **{f"mk{x}_{h}": f"挂 +{x}¢ 卖、{h} 秒未成交吃单卖" for x in MAKER_X for h in HOLDS}}
FEATURES = ["jump_bp", "jump_abs_bp", "jump_z", "dir", "is_jump", "h_edge", "dmid2", "dmid10", "imb", "spread",
            "ask", "ask_fee", "tau", "trend_agree", "trend_side", "vr60", "f_ridge5", "f_hgb5", "f_ridge15",
            "f_hgb15", "dvol_rv"]
FEATURE_NAMES = {"jump_bp": "急动（这一方向，bp）", "jump_abs_bp": "急动大小（bp）", "jump_z": "急动 σ 倍数（这一方向）",
                 "dir": "急动方向是否这一方", "is_jump": "急动决策点", "h_edge": "H 公允价 − 卖一",
                 "dmid2": "中间价 2 秒变动", "dmid10": "中间价 10 秒变动", "imb": "买一卖一挂单量失衡",
                 "spread": "价差", "ask": "卖一价", "ask_fee": "卖一价的手续费", "tau": "剩余时间",
                 "trend_agree": "急动与 4 小时趋势同向", "trend_side": "这一方与 4 小时趋势同向", "vr60": "60 分钟方差比",
                 "f_ridge5": "多因子岭回归 5m", "f_hgb5": "多因子梯度提升 5m", "f_ridge15": "多因子岭回归 15m",
                 "f_hgb15": "多因子梯度提升 15m", "dvol_rv": "DVOL − 已实现波动"}
KINDS = ("jump", "fixed", "random")
META = ["day", "market", "start", "t", "kind", "side", "jdir", "price", "ask_size", "won"]


def fee(p):
    """Taker fee per share, 0.07 p (1 - p) (MIX.md)."""
    p = np.asarray(p, float)
    return FEE_RATE * p * (1.0 - p)


def ts(day):
    return int(pd.Timestamp(day, tz="UTC").timestamp())


def segment_of(start_s):
    """'A' / 'B' / 'C' by market start (s, UTC), '' outside."""
    s = np.asarray(start_s, float)
    out = np.full(s.shape, "", dtype=object)
    for name, a, b in SEGMENTS:
        out[(s >= ts(a)) & (s < ts(b))] = name
    return out


# --------------------------------------------------------------------------- inputs (factor preds, DVOL)
def build_inputs(cache=None, out_dir=INPUTS, start="2026-05-20", end="2026-09-01"):
    """real/mix-inputs/factor_preds_5m.csv.gz and dvol_1h.csv.gz from the factor study's cache (local only;
    the CI lane reads the files). Predictions: factors_study.pkl's walk-forward (out of sample, to
    08-15) and, from 08-16, the same walk-forward continued through the factor study's C blocks with
    its ridge alpha and HGB settings (factors.lockbox's procedure, for both models)."""
    import pickle

    import factors as F
    cache = Path(cache or F.CACHE)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(cache / "factors_study.pkl", "rb") as fh:
        R = pickle.load(fh)
    spec = json.loads((cache / "frozen.json").read_text())
    assert spec["features"] == F.FACTORS
    df = F.load_frame(cache)
    T = df.index.to_numpy()
    X = df[spec["features"]].to_numpy(float)

    def factory(name, alpha):
        return F.make_model(name, alpha, spec["hgb_params"])

    cols = {}
    c0 = F.ts(F.PERIODS["C"][0])
    for h in ("5m", "15m"):
        up = F.up_of(df[f"r_{h}"].to_numpy())
        for m in MODELS:
            ab = R["preds"][f"{m}_{h}"].reindex(df.index).to_numpy(float)
            blk = [(b0, b1) for b0, b1 in F.blocks(["C"]) if b0 < ts(end)]
            pc, _ = F.walk_forward(X, up, T, F.HMIN[h], m, blk, alpha=R["alpha"][h], factory=factory)
            cols[f"{m}_{h}"] = np.where(T < c0, ab, pc)
    p = pd.DataFrame(cols, index=pd.Index(T, name="T"))
    p = p[(p.index >= ts(start)) & (p.index < ts(end))].dropna(how="all")
    p.round(5).to_csv(out_dir / "factor_preds_5m.csv.gz")
    d = pd.read_csv(cache / "dvol_1h.csv")
    d = pd.DataFrame({"hour": d["t"].to_numpy(np.int64) // 1000, "dvol": d["close"].to_numpy(float)})
    d = d[(d["hour"] >= ts(start) - 7 * DAY) & (d["hour"] < ts(end))]
    d.to_csv(out_dir / "dvol_1h.csv.gz", index=False)
    return p, d


def load_inputs(inputs=INPUTS):
    """(factor predictions indexed by window start s, DVOL by hour start s); empty frames when missing."""
    inputs = Path(inputs)
    fp, dv = pd.DataFrame(), pd.DataFrame(columns=["hour", "dvol"])
    if (inputs / "factor_preds_5m.csv.gz").exists():
        fp = pd.read_csv(inputs / "factor_preds_5m.csv.gz", index_col=0)
        fp.index = fp.index.astype(np.int64)
    if (inputs / "dvol_1h.csv.gz").exists():
        dv = pd.read_csv(inputs / "dvol_1h.csv.gz").sort_values("hour")
    return fp, dv


def dvol_at(dv, t):
    """DVOL (percent) of the last hourly candle closed by t (known at its hour's end), NaN when none."""
    t = np.asarray(t, float)
    if dv is None or not len(dv):
        return np.full(t.shape, np.nan)
    known = dv["hour"].to_numpy(float) + 3600.0
    k = np.searchsorted(known, t + T_EPS, "right") - 1
    v = dv["dvol"].to_numpy(float)
    return np.where(k >= 0, v[np.maximum(k, 0)], np.nan)


# --------------------------------------------------------------------------- Binance
class Spot:
    """A day's Binance prints (with the carried tail of the previous archive): trade-time order (ts,
    lp, rv, d = move against the 1-5 s reference), receipt order, the per-second grid (sigma) and rv30."""

    def __init__(self, binance):
        b = binance[["trade_ts_ms", "recv_ts_ms", "price"]].apply(pd.to_numeric, errors="coerce")
        v = b.to_numpy(float)
        b = b[np.isfinite(v).all(axis=1) & (v[:, 2] > 0)].drop_duplicates()
        b = b.sort_values(["trade_ts_ms", "recv_ts_ms"], kind="stable")
        self.ts = b["trade_ts_ms"].to_numpy(float) / 1000.0
        self.rv = b["recv_ts_ms"].to_numpy(float) / 1000.0
        px = b["price"].to_numpy(float)
        self.lp = np.log(px)
        j = np.searchsorted(self.ts, self.ts - REF_MIN_S + T_EPS, "right") - 1
        jj = np.maximum(j, 0)
        ok = (j >= 0) & (self.ts - self.ts[jj] <= REF_MAX_S + T_EPS) if len(self.ts) else np.zeros(0, bool)
        self.d = np.where(ok, self.lp - self.lp[jj], np.nan) if len(self.ts) else np.zeros(0)
        self.order = np.argsort(self.rv, kind="stable")  # receipt order -> trade-sorted index
        self.rv_o = self.rv[self.order]
        self.sp = j2.Seconds(self.ts, px, ffill=GAP_FILL_S)
        if len(self.sp.lp):
            r = pd.Series(self.sp.lp).diff()
            self.rv30 = (r.rolling(RV_N, min_periods=RV_MIN).std() * math.sqrt(SEC_YEAR)).to_numpy(float)
        else:
            self.rv30 = np.array([])

    def last(self, t, max_age=np.inf):
        """Trade-sorted index of the last print received at or before t (-1 when none or older than max_age)."""
        t = np.asarray(t, float)
        if not len(self.rv_o):
            return np.full(t.shape, -1)
        k = np.searchsorted(self.rv_o, t + T_EPS, "right") - 1
        kk = np.maximum(k, 0)
        ok = (k >= 0) & (t - self.rv_o[kk] <= max_age + T_EPS)
        return np.where(ok, self.order[kk], -1)

    def at(self, arr, i):
        """arr[i] with NaN where i < 0."""
        return np.where(i >= 0, arr[np.maximum(i, 0)], np.nan) if len(arr) else np.full(np.shape(i), np.nan)


def jump_points(spot, win, own_from=-np.inf):
    """MIX.md jump decision points: prints (trade-time reference, received at t) with |d| >= 2 bp, 285 >=
    end - t >= 20 s, per market >= 5 s after the last kept point. Only prints with trade time >=
    own_from (the archive's own prints, not the carried tail) can be jumps.
    -> DataFrame market, t, i (trade-sorted index)."""
    if not len(spot.ts) or not len(win):
        return pd.DataFrame(columns=["market", "t", "i"])
    with np.errstate(invalid="ignore"):
        cand = np.flatnonzero((np.abs(spot.d) * 1e4 >= JUMP_BP - 1e-9) & (spot.ts >= own_from))
    ci = cand[np.argsort(spot.rv[cand], kind="stable")]
    co = spot.rv[ci]
    out = []
    for m, end in zip(win["market"], win["end"]):
        a = np.searchsorted(co, end - TAU_HI - T_EPS, "left")
        b = np.searchsorted(co, end - TAU_LO + T_EPS, "right")
        k, tt = 0, co[a:b]
        while k < len(tt):
            out.append((m, tt[k], ci[a + k]))
            k = max(k + 1, int(np.searchsorted(tt, tt[k] + SPACING_S - T_EPS, "left")))
    return pd.DataFrame(out, columns=["market", "t", "i"])


def plan(spot, win, own_from=-np.inf):
    """Decision points of a day: jump, fixed and random (one per jump point, seeded per market);
    columns market, start, end, t, kind, i (the print giving d: the trigger for jumps, the last received
    by t otherwise), side (random only; 0 = both sides)."""
    jp = jump_points(spot, win, own_from)
    st = dict(zip(win["market"], win["start"]))
    en = dict(zip(win["market"], win["end"]))
    parts = []
    if len(jp):
        parts.append(jp.assign(kind="jump", side=0))
    fx = [(m, e - tau) for m, e in zip(win["market"], win["end"]) for tau in FIXED_TAUS]
    if fx:
        f = pd.DataFrame(fx, columns=["market", "t"])
        parts.append(f.assign(kind="fixed", side=0, i=spot.last(f["t"].to_numpy(float), PRICE_MAX_AGE_S)))
    rnd = []
    for m, g in jp.groupby("market", sort=False) if len(jp) else []:
        rng = np.random.default_rng(zlib.crc32(str(m).encode()))
        t = rng.uniform(en[m] - TAU_HI, en[m] - TAU_LO, len(g))
        sd = rng.choice(np.array([-1, 1]), len(g))
        rnd.append(pd.DataFrame({"market": m, "t": t, "side": sd}))
    if rnd:
        r = pd.concat(rnd, ignore_index=True)
        parts.append(r.assign(kind="random", i=spot.last(r["t"].to_numpy(float), PRICE_MAX_AGE_S)))
    if not parts:
        return pd.DataFrame(columns=["market", "start", "end", "t", "kind", "i", "side"])
    p = pd.concat(parts, ignore_index=True)
    p["start"] = p["market"].map(st).astype(float)
    p["end"] = p["market"].map(en).astype(float)
    return p[["market", "start", "end", "t", "kind", "i", "side"]]


# --------------------------------------------------------------------------- books
class Books:
    """The day's 5m snapshots grouped by market (sorted, one row per timestamp), with the time of each
    market's last top-of-book change (jump2s_hf / cross.book_health)."""

    COLS = ("up_best_bid", "up_best_ask", "down_best_bid", "down_best_ask", "up_bid_size", "up_ask_size",
            "down_bid_size", "down_ask_size")

    def __init__(self, feat, markets):
        f = feat[feat["market_id"].astype(str).isin(set(markets))] if len(feat) else feat
        self.seg = {}
        if not len(f):
            return
        f = f.assign(market_id=f["market_id"].astype(str)).sort_values(["market_id", "timestamp_ms"], kind="stable")
        f = f.drop_duplicates(["market_id", "timestamp_ms"], keep="last")
        mk = f["market_id"].to_numpy()
        self.T = f["timestamp_ms"].to_numpy(float) / 1000.0
        self.c = {c: (f[c].to_numpy(float) if c in f else np.full(len(f), np.nan)) for c in self.COLS}
        state = f["lifecycle_state"].astype(str).str.lower().to_numpy() if "lifecycle_state" in f else \
            np.full(len(f), "active")
        halt = f["observed_halt_flag"].eq(True).to_numpy(bool) if "observed_halt_flag" in f else np.zeros(len(f), bool)
        self.live = (state == "active") & ~halt
        stv = f[[c for c in hf.STATE_COLS if c in f]].to_numpy(float)
        same = (stv[1:] == stv[:-1]) | (np.isnan(stv[1:]) & np.isnan(stv[:-1]))
        moved = np.r_[True, ~same.all(axis=1) | (mk[1:] != mk[:-1])]
        self.last_chg = pd.Series(np.where(moved, self.T, np.nan)).ffill().to_numpy(float)
        self.seg = hf._segments(mk)

    def side(self, g, s, ok):
        """(bid, ask, bid size, ask size) of side s at global rows g, NaN where not ok."""
        up = s > 0
        out = []
        for u, d in (("up_best_bid", "down_best_bid"), ("up_best_ask", "down_best_ask"),
                     ("up_bid_size", "down_bid_size"), ("up_ask_size", "down_ask_size")):
            out.append(np.where(ok, np.where(up, self.c[u][g], self.c[d][g]), np.nan))
        return out


def _asof(T, a, b, t, max_age=BOOK_MAX_AGE_S):
    """Global index of the last snapshot of segment [a, b) at or before t (<= max_age old), -1 when none."""
    k = j2.asof_idx(T[a:b], t, max_age)
    return np.where(k >= 0, k + a, -1)


def _quote(bk, g, s):
    """Side quote at global rows g (-1: none): (bid, ask, bid size, ask size, usable) where usable =
    active and not crossed (bid < ask when both are shown)."""
    has = g >= 0
    gg = np.maximum(g, 0)
    ok = has & bk.live[gg]
    bid, ask, bsz, asz = bk.side(gg, s, ok)
    with np.errstate(invalid="ignore"):
        crossed = np.isfinite(bid) & np.isfinite(ask) & (bid >= ask)
    ok &= ~crossed
    return np.where(ok, bid, np.nan), np.where(ok, ask, np.nan), np.where(ok, bsz, np.nan), \
        np.where(ok, asz, np.nan), ok


def _fresh(bk, g, t):
    gg = np.maximum(g, 0)
    with np.errstate(invalid="ignore"):
        return (g >= 0) & (t - bk.last_chg[gg] <= BOOK_MAX_AGE_S + T_EPS)


# --------------------------------------------------------------------------- one archive
def label_cols():
    cols = []
    for p in POLICIES:
        cols += [f"pnl_{p}", f"hold_{p}", f"how_{p}", f"sh20_{p}"]
    return cols


ROW_COLS = META + FEATURES + label_cols()


def _empty_rows():
    return pd.DataFrame({c: pd.Series(dtype="float32") for c in ROW_COLS})


def day_rows(feat, mkts, binance, prints, carry=None, factor=None, dvol=None, closes=None, day=""):
    """Rows (ROW_COLS) of one archive and a dict of counts.

    feat / mkts / binance / prints as from cross.read_day + cross.market_table, cross.read_binance and
    cross.read_poly_trades; carry: the previous archive's last Binance prints (lookups only);
    factor: load_inputs()[0]; dvol: load_inputs()[1]; closes: Binance minute closes (trend)."""
    info = dict(day=day, markets=0, jump=0, fixed=0, random=0, rows=0, no_entry=0, prints=len(binance))
    if mkts is None or mkts.empty or binance is None or binance.empty:
        return _empty_rows(), info
    m5 = mkts[(mkts["horizon"] == 5) & mkts["up_won"].notna()].copy()
    m5["market_id"] = m5["market_id"].astype(str)
    m5 = m5.drop_duplicates("market_id", keep="last")
    info["markets"] = len(m5)
    if m5.empty:
        return _empty_rows(), info
    win = pd.DataFrame({"market": m5["market_id"].to_numpy(), "start": m5["start"].to_numpy(float) / 1000.0,
                        "end": m5["end"].to_numpy(float) / 1000.0})
    own = pd.to_numeric(binance["trade_ts_ms"], errors="coerce")
    own_from = float(own.min()) / 1000.0 if own.notna().any() else -np.inf
    both = binance if carry is None or carry.empty else pd.concat([carry, binance], ignore_index=True)
    spot = Spot(both)
    p = plan(spot, win, own_from)
    for k in KINDS:
        info[k] = int((p["kind"] == k).sum())
    if p.empty:
        return _empty_rows(), info

    # ---- point-level Binance features (before the sides are split)
    i = p["i"].to_numpy(np.int64)
    t = p["t"].to_numpy(float)
    d = spot.at(spot.d, i)
    t_known = spot.at(spot.ts, spot.last(t))  # exchange time of the last print received by t
    known = np.isfinite(t_known)
    tk_safe = np.where(known, t_known, 0.0)
    sigma = np.where(known, spot.sp.sigma(tk_safe), np.nan)
    sec = np.floor(tk_safe).astype(np.int64) - 1
    rv30 = np.where(known, spot.sp._at(spot.rv30, sec), np.nan) if len(spot.rv30) else np.full(len(p), np.nan)
    lp_now = spot.at(spot.lp, spot.last(t, PRICE_MAX_AGE_S))
    lp_ago = spot.at(spot.lp, spot.last(t - H_ANCHOR_S, PRICE_MAX_AGE_S))
    if closes is not None and len(closes):
        ret4h, vr60 = j2.trend_features(closes, t_known)
    else:
        ret4h, vr60 = np.full(len(p), np.nan), np.full(len(p), np.nan)
    dv = dvol_at(dvol, t) / 100.0
    start = p["start"].to_numpy(float)
    fpred = {}
    for c, col in (("f_ridge5", "ridge_5m"), ("f_hgb5", "hgb_5m"), ("f_ridge15", "ridge_15m"), ("f_hgb15", "hgb_15m")):
        if factor is not None and len(factor) and col in factor:
            fpred[c] = factor[col].reindex(np.round(start).astype(np.int64)).to_numpy(float)
        else:
            fpred[c] = np.full(len(p), np.nan)
    w = j2.settle_twap(start)
    t_el = t - start
    import binary as bo
    fac = np.where(w > 0, bo.twap_std_factor(t_el, bo.WINDOW_S, np.maximum(w, 1)),
                   np.sqrt(np.maximum(bo.WINDOW_S - t_el, 1.0)))

    # ---- one row per side (random rows: their own side)
    kind = p["kind"].to_numpy()
    two = kind != "random"
    rep = np.where(two, 2, 1)
    idx = np.repeat(np.arange(len(p)), rep)
    side = np.empty(len(idx), np.int64)
    pos = np.cumsum(rep) - rep
    side[pos] = np.where(two, 1, p["side"].to_numpy(np.int64))
    side[pos[two] + 1] = -1
    n = len(idx)
    R = {"market": p["market"].to_numpy()[idx], "start": start[idx], "end": p["end"].to_numpy(float)[idx],
         "t": t[idx], "kind": kind[idx], "side": side}
    s = side.astype(float)
    dd = d[idx]
    with np.errstate(invalid="ignore", divide="ignore"):
        R["jump_bp"] = s * dd * 1e4
        R["jump_abs_bp"] = np.abs(dd) * 1e4
        R["jump_z"] = np.where(sigma[idx] > 0, s * dd / sigma[idx], np.nan)
        R["dir"] = np.sign(s * dd)
        R["jdir"] = np.nan_to_num(np.sign(dd)).astype(np.int8)
        R["is_jump"] = (R["kind"] == "jump").astype(float)
        R["tau"] = R["end"] - R["t"]
        r4 = ret4h[idx]
        R["trend_agree"] = np.where(np.isfinite(r4) & np.isfinite(dd), np.sign(dd) * np.sign(r4), np.nan)
        R["trend_side"] = np.where(np.isfinite(r4), np.sign(s * r4), np.nan)
        R["vr60"] = vr60[idx]
        for c, v in fpred.items():
            R[c] = s * (v[idx] - 0.5)
        R["dvol_rv"] = dv[idx] - rv30[idx]
    move = (lp_now - lp_ago)[idx]
    scale = (fac * sigma)[idx]

    # ---- book features, entry and exits, per market
    bk = Books(feat, win["market"])
    tok = {}
    for m, u, dn in zip(m5["market_id"], m5.get("up_token", [None] * len(m5)), m5.get("down_token", [None] * len(m5))):
        for tk, sd in ((u, 1), (dn, -1)):
            if tk is not None and str(tk) not in ("", "None", "nan"):
                tok[str(tk)] = (m, sd)
    pr_by = {}
    if prints is not None and len(prints) and tok:
        q = prints[prints["instrument"].astype(str).isin(tok)]
        q = q[q["taker_side"].astype(str).str.lower() == "buy"]
        if len(q):
            ins = q["instrument"].astype(str).to_numpy()
            key = [tok[x] for x in ins]
            q = q.assign(_m=[k[0] for k in key], _s=[k[1] for k in key])
            for (m, sd), g in q.groupby(["_m", "_s"], sort=False):
                g = g.sort_values("recv_ts_ms", kind="stable")
                pr_by[(m, sd)] = (g["recv_ts_ms"].to_numpy(float) / 1000.0, g["price"].to_numpy(float))
    F = {c: np.full(n, np.nan) for c in ("h_edge", "dmid2", "dmid10", "imb", "spread", "price_t")}
    price, asz_e = np.full(n, np.nan), np.full(n, np.nan)
    L = {f"{k}_{pol}": np.full(n, np.nan) for pol in POLICIES for k in ("pnl", "hold", "how", "sh20")}
    won_up = dict(zip(m5["market_id"], m5["up_won"].astype(float)))
    won = np.where(s > 0, np.array([won_up[m] for m in R["market"]]), 1 - np.array([won_up[m] for m in R["market"]]))
    order = pd.Series(np.arange(n)).groupby(R["market"], sort=False).indices
    for m, ix in order.items():
        if m not in bk.seg:
            continue
        a, b = bk.seg[m]
        tt, ss, end = R["t"][ix], s[ix], R["end"][ix]
        # features at t (any freshness), t - 2, t - 10
        g = _asof(bk.T, a, b, tt)
        bid, ask, bsz, asz, ok = _quote(bk, g, ss)
        mid = (bid + ask) / 2
        with np.errstate(invalid="ignore", divide="ignore"):
            F["spread"][ix] = ask - bid
            F["imb"][ix] = (bsz - asz) / (bsz + asz)
            F["price_t"][ix] = ask
            for lag, c in ((2.0, "dmid2"), (10.0, "dmid10")):
                b0, a0, _, _, _ = _quote(bk, _asof(bk.T, a, b, tt - lag), ss)
                F[c][ix] = mid - (b0 + a0) / 2
            ub, ua, _, _, _ = _quote(bk, _asof(bk.T, a, b, tt - H_ANCHOR_S), np.ones(len(ix)))
            prior = np.clip((ub + ua) / 2, 0.005, 0.995)
            from scipy.stats import norm
            p1 = norm.cdf(norm.ppf(prior) + move[ix] / scale[ix])
            fair = np.where(ss > 0, p1, 1 - p1)
            F["h_edge"][ix] = fair - ask
        # entry at t + 0.5
        te = tt + LAT_S
        ge = _asof(bk.T, a, b, te)
        _, pe, _, se, oke = _quote(bk, ge, ss)
        with np.errstate(invalid="ignore"):
            enter = oke & _fresh(bk, ge, te) & j2.price_ok(pe, se) & (te < end - T_EPS)
        if not enter.any():
            continue
        ix, te, ss, end, pe, se = ix[enter], te[enter], ss[enter], end[enter], pe[enter], se[enter]
        price[ix], asz_e[ix] = pe, se
        cost = pe + fee(pe)
        w_ = won[ix]
        settle = w_ - cost
        hold_end = end - te
        sh_e = np.minimum(20.0, se)
        L["pnl_settle"][ix], L["hold_settle"][ix], L["how_settle"][ix], L["sh20_settle"][ix] = settle, hold_end, 0, sh_e
        tk = {}
        for h in HOLDS:
            tx = te + h + LAT_S
            gx = _asof(bk.T, a, b, tx)
            bx, _, bsx, _, okx = _quote(bk, gx, ss)
            with np.errstate(invalid="ignore"):
                sold = okx & _fresh(bk, gx, tx) & (tx < end - T_EPS) & np.isfinite(bx) & (bx > 0) & \
                    np.isfinite(bsx) & (bsx >= MIN_SHARES - 1e-12)
            pnl = np.where(sold, bx - fee(bx) - cost, settle)
            hold = np.where(sold, tx - te, hold_end)
            sh = np.where(sold, np.minimum(sh_e, bsx), sh_e)
            tk[h] = (pnl, hold, sold.astype(float), sh)
            L[f"pnl_tk{h}"][ix], L[f"hold_tk{h}"][ix], L[f"how_tk{h}"][ix], L[f"sh20_tk{h}"][ix] = tk[h]
        # resting sells: live at tl; marketable on arrival -> taker at that bid (a fresh bid of >= 5 shares,
        # as any taker sale); never after the market's end
        tl = te + MAKER_LIVE_S
        gl = _asof(bk.T, a, b, tl)
        bl, _, bsl, _, okl = _quote(bk, gl, ss)
        with np.errstate(invalid="ignore"):
            cross_ok = okl & _fresh(bk, gl, tl) & (tl < end - T_EPS) & np.isfinite(bl) & (bl > 0) & \
                np.isfinite(bsl) & (bsl >= MIN_SHARES - 1e-12)
        for sd in (1, -1):
            sel = np.flatnonzero(ss == sd)
            if not len(sel):
                continue
            ev_t, ev_v = _events(bk, a, b, sd, pr_by.get((m, sd)))
            for x in MAKER_X:
                lim = np.minimum(np.round(pe[sel] + x / 100.0, 6), MAKER_CAP)
                first = _first_hit(ev_t, ev_v, tl[sel], np.minimum(te[sel] + max(HOLDS), end[sel]), lim)
                with np.errstate(invalid="ignore"):
                    mkt = cross_ok[sel] & (bl[sel] >= lim - 1e-9)
                for h in HOLDS:
                    pol = f"mk{x}_{h}"
                    filled = ~mkt & (first <= np.minimum(te[sel] + h, end[sel]) + T_EPS)
                    tp, th, tw, tsh = (v[sel] for v in tk[h])
                    pnl = np.where(mkt, bl[sel] - fee(bl[sel]) - cost[sel], np.where(filled, lim - cost[sel], tp))
                    hold = np.where(mkt, MAKER_LIVE_S, np.where(filled, first - te[sel], th))
                    how = np.where(mkt, 1.0, np.where(filled, 2.0, tw))
                    sh = np.where(mkt, np.minimum(sh_e[sel], bsl[sel]), np.where(filled, sh_e[sel], tsh))
                    ii = ix[sel]
                    L[f"pnl_{pol}"][ii], L[f"hold_{pol}"][ii], L[f"how_{pol}"][ii], L[f"sh20_{pol}"][ii] = \
                        pnl, hold, how, sh
    keep = np.isfinite(price)
    info["no_entry"] = int((~keep & (R["kind"] != "random")).sum())
    out = {"day": day, "market": R["market"], "start": np.round(R["start"]).astype(np.int64), "t": R["t"],
           "kind": R["kind"], "side": R["side"].astype(np.int8), "jdir": R["jdir"], "price": price,
           "ask_size": asz_e, "won": won}
    for c in FEATURES:
        if c == "ask":
            out[c] = F["price_t"]
        elif c == "ask_fee":
            out[c] = fee(F["price_t"])
        elif c in F:
            out[c] = F[c]
        else:
            out[c] = R[c]
    out.update(L)
    rows = pd.DataFrame(out)[keep].reset_index(drop=True)
    info["rows"] = len(rows)
    return compact(rows), info


def _events(bk, a, b, sd, pr):
    """Time-sorted (times, prices) at which a resting sell of side sd's token could be hit: active,
    uncrossed, fresh snapshots (top of book changed <= 1 s before) with that token's best bid, and
    taker BUY prints of the token (receipt time)."""
    T = bk.T[a:b]
    g = np.arange(a, b)
    bid, _, _, _, ok = _quote(bk, g, np.full(len(g), sd))
    v = np.where(ok & _fresh(bk, g, T) & np.isfinite(bid), bid, -np.inf)
    if pr is not None and len(pr[0]):
        T = np.concatenate([T, pr[0]])
        v = np.concatenate([v, pr[1]])
        o = np.argsort(T, kind="stable")
        T, v = T[o], v[o]
    return T, v


def _first_hit(ev_t, ev_v, t_from, t_to, lim):
    """First event time in (t_from, t_to] with value >= lim (inf when none), per row."""
    out = np.full(len(t_from), np.inf)
    lo = np.searchsorted(ev_t, t_from + T_EPS, "left")
    hi = np.searchsorted(ev_t, t_to + T_EPS, "right")
    for r in range(len(t_from)):
        if hi[r] > lo[r]:
            hit = ev_v[lo[r]:hi[r]] >= lim[r] - 1e-9
            if hit.any():
                out[r] = ev_t[lo[r] + int(np.argmax(hit))]
    return out


def compact(rows):
    """float32 / int8 / categorical columns (t stays float64: millisecond times)."""
    r = rows.copy()
    for c in r.columns:
        if c in ("day", "market", "kind"):
            r[c] = r[c].astype("category")
        elif c in ("start",):
            r[c] = r[c].astype(np.int64)
        elif c == "t":
            r[c] = r[c].astype(np.float64)
        elif c in ("side", "jdir", "won") or c.startswith("how_"):
            r[c] = pd.to_numeric(r[c]).fillna(-1).astype(np.int8)
        else:
            r[c] = pd.to_numeric(r[c]).astype(np.float32)
    return r


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
        with np.errstate(invalid="ignore"):
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


def a_blocks():
    """7-day blocks of A from its first day: (start, end, out-of-sample?)."""
    a0, a1 = ts(SEGMENTS[0][1]), ts(SEGMENTS[0][2])
    out, b = [], a0
    while b < a1:
        out.append((b, min(b + BLOCK_DAYS * DAY, a1), b - a0 >= MIN_TRAIN_DAYS * DAY))
        b += BLOCK_DAYS * DAY
    return out


def walk_forward(X, y, start, model):
    """A out-of-sample scores: each out-of-sample block scored by a model fit on the rows of markets
    that ended (start + 300 s) by the block's start; NaN elsewhere."""
    out = np.full(len(y), np.nan)
    end = start + 300
    for b0, b1, oos in a_blocks():
        if not oos:
            continue
        te = (start >= b0) & (start < b1)
        tr = end <= b0
        if not te.any() or tr.sum() < 2 * HGB_PARAMS["min_samples_leaf"]:
            continue
        out[te] = make_model(model).fit(X[tr], y[tr]).predict(X[te])
    return out


def top_cut(v, q):
    """Score at the top q of v (NaN ranks last): rows with v >= cut are the top q (ties included)."""
    v = np.asarray(v, float)
    f = np.sort(v[np.isfinite(v)])[::-1]
    k = max(1, int(math.ceil(q * len(v))))
    return float(f[k - 1]) if k <= len(f) else (float(f[-1]) if len(f) else np.nan)


# --------------------------------------------------------------------------- statistics
def cl_stat(pnl, cluster):
    """dict(mean, se, t, p (one-sided, mean > 0), n, k) with the SE clustered by `cluster`
    (jump2s.cl's formula)."""
    from scipy.stats import norm
    x = np.asarray(pnl, float)
    ok = np.isfinite(x)
    x, c = x[ok], np.asarray(cluster)[ok]
    if not len(x):
        return dict(mean=np.nan, se=np.nan, t=np.nan, p=np.nan, n=0, k=0)
    g = pd.DataFrame({"c": c, "x": x}).groupby("c", observed=True)["x"].agg(["sum", "count"])
    m = g["sum"].sum() / g["count"].sum()
    r = g["sum"] - m * g["count"]
    k = len(g)
    se = float(np.sqrt((r ** 2).sum() * k / max(k - 1, 1)) / g["count"].sum())
    t = m / se if se > 0 else np.nan
    return dict(mean=float(m), se=se, t=float(t), p=float(norm.sf(t)) if np.isfinite(t) else np.nan, n=int(len(x)),
                k=int(k))


def units(tr, pol, n_days):
    """Per-day units of a set of trades of policy `pol`: trades, shares (5 a trade and min(20, sizes)),
    entry cost $, profit $, capital tied up $ (time-weighted mean over the day; median and max of the
    daily peaks of concurrent positions, a day without a trade counting 0), all per day over n_days."""
    out = dict(days=n_days, trades=np.nan, sh5=np.nan, sh20=np.nan, cost5=np.nan, cost20=np.nan, profit5=np.nan,
               profit20=np.nan, cap5=np.nan, cap20=np.nan, peak5=np.nan, peak20=np.nan, pmax5=np.nan, pmax20=np.nan)
    if not n_days:
        return out
    if not len(tr):
        return {**out, **{k: 0.0 for k in out if k != "days"}}
    p = tr["price"].to_numpy(float)
    unit = p + fee(p)
    pnl = tr[f"pnl_{pol}"].to_numpy(float)
    hold = tr[f"hold_{pol}"].to_numpy(float)
    te = tr["t"].to_numpy(float) + LAT_S
    sh = {5: np.minimum(5.0, tr["ask_size"].to_numpy(float)), 20: tr[f"sh20_{pol}"].to_numpy(float)}
    out["trades"] = len(tr) / n_days
    day = np.floor(te / DAY)
    for u in UNITS:
        out[f"sh{u}"] = sh[u].sum() / n_days
        out[f"cost{u}"] = (sh[u] * unit).sum() / n_days
        out[f"profit{u}"] = (sh[u] * pnl).sum() / n_days
        out[f"cap{u}"] = (sh[u] * unit * hold).sum() / (n_days * DAY)
        peaks = []
        for dd in np.unique(day):
            k = day == dd
            ev = np.concatenate([te[k], te[k] + hold[k]])
            amt = np.concatenate([(sh[u] * unit)[k], -(sh[u] * unit)[k]])
            o = np.lexsort((amt, ev))  # at equal times an exit is counted before an entry
            peaks.append(float(np.max(np.cumsum(amt[o]))))
        peaks += [0.0] * max(n_days - len(peaks), 0)  # days of the segment without a trade
        out[f"peak{u}"] = float(np.median(peaks)) if peaks else 0.0
        out[f"pmax{u}"] = float(np.max(peaks)) if peaks else 0.0
    return out


def n_days(rows):
    """UTC days (of market start) present in rows."""
    return int(pd.Series(np.floor(rows["start"].to_numpy(float) / DAY)).nunique()) if len(rows) else 0


# --------------------------------------------------------------------------- selection, freeze, evaluation
def a_oos_mask(start):
    """Rows of A's out-of-sample blocks (by market start)."""
    s = np.asarray(start, float)
    return (s >= min(b0 for b0, _, oos in a_blocks() if oos)) & (s < ts(SEGMENTS[0][2]))


def fingerprint(rowsA):
    """sha256 of the A rows that the freeze depends on (keys, features, labels)."""
    if not len(rowsA):
        return "empty"
    r = rowsA.sort_values(["market", "t", "side", "kind"], kind="stable")
    h = hashlib.sha256()
    h.update("\n".join(r["market"].astype(str) + "|" + r["kind"].astype(str)).encode())
    h.update(np.round(r[["start", "t", "side"]].to_numpy(float), 3).tobytes())
    h.update(np.round(r[FEATURES + [f"pnl_{p}" for p in POLICIES]].to_numpy(float), 5).tobytes())
    return h.hexdigest()


def model_rows(rows):
    return rows[rows["kind"].astype(str) != "random"]


def candidates(A, log=print):
    """Every (policy, model, q) on A out of sample: cut, n, mean, se; plus the A oos scores."""
    X = A[FEATURES].to_numpy(float)
    st = A["start"].to_numpy(float)
    oos = a_oos_mask(st)
    mk = A["market"].astype(str).to_numpy()
    cand, scores = [], {}
    t0 = time.time()
    for pol in POLICIES:
        y = A[f"pnl_{pol}"].to_numpy(float)
        for model in MODELS:
            sc = walk_forward(X, y, st, model)
            scores[(pol, model)] = sc
            v = np.where(oos, sc, np.nan)
            for q in QS:
                cut = top_cut(v[oos], q)
                sel = oos & np.isfinite(v) & (v >= cut)
                cand.append(dict(policy=pol, model=model, q=q, cut=cut, **cl_stat(y[sel], mk[sel])))
        log(f"  walk-forward {pol}: {time.time() - t0:.0f} s")
    return pd.DataFrame(cand), scores


def single_control(A, pol, q):
    """Best single feature (either direction) at top q on the A out-of-sample rows: dict or None."""
    oos = a_oos_mask(A["start"].to_numpy(float))
    S = A[oos]
    y = S[f"pnl_{pol}"].to_numpy(float)
    mk = S["market"].astype(str).to_numpy()
    best = None
    for f in FEATURES:
        x = S[f].to_numpy(float)
        for sg in (1, -1):
            v = sg * x
            cut = top_cut(v, q)
            if not np.isfinite(cut):
                continue
            sel = np.isfinite(v) & (v >= cut)
            if sel.sum() > TIE_MAX * q * len(v) or sel.sum() < MIN_A_TRADES:
                continue
            st = cl_stat(y[sel], mk[sel])
            if best is None or st["mean"] > best["A"]["mean"]:
                best = dict(feature=f, sign=sg, cut=cut, A=st)
    return best


def freeze(A, path, log=print):
    """Pick and freeze the rules on A alone (A rows only are passed in). Writes `path` unless it
    exists; returns the spec read back from disk and whether it was written now."""
    path = Path(path)
    fp = fingerprint(A)
    if path.exists():
        spec = json.loads(path.read_text())
        log(f"{path} exists (made {spec.get('made')}); not re-frozen")
        return spec, False, None
    A = model_rows(A)
    cand, scores = candidates(A, log)
    ok = cand[cand["n"] >= MIN_A_TRADES].sort_values(["mean", "policy", "model", "q"], ascending=[False, True, True, False],
                                                     kind="stable")
    rules = []
    for k, r in enumerate(ok.head(N_PICK).itertuples(index=False)):
        ctl = single_control(A, r.policy, r.q)
        rules.append(dict(id=f"R{k + 1}", policy=r.policy, model=r.model, q=r.q, cut=r.cut,
                          A=dict(mean=r.mean, se=r.se, t=r.t, p=r.p, n=r.n, k=r.k), control=ctl))
    spec = dict(made=pd.Timestamp.now(tz="UTC").isoformat(), design="MIX.md", segments=SEGMENTS,
                a_oos_from=pd.Timestamp(min(b0 for b0, _, o in a_blocks() if o), unit="s").strftime("%Y-%m-%d"),
                features=FEATURES, policies=POLICIES, models={"ridge": {"alpha": RIDGE_ALPHA}, "hgb": HGB_PARAMS},
                qs=QS, min_a_trades=MIN_A_TRADES, b_alpha=B_ALPHA, c_alpha=C_ALPHA, a_rows=int(len(A)),
                a_fingerprint=fp, n_candidates=int(len(cand)), n_eligible=int(len(ok)), rules=rules,
                candidates=json.loads(cand.to_json(orient="records")))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(spec, indent=1, ensure_ascii=False, default=float), encoding="utf-8")
    log(f"frozen {len(rules)} rules -> {path}")
    return json.loads(path.read_text()), True, scores


def evaluate(rows, spec, complete_c=True, log=print):
    """B (and C for the rules that pass B) of the frozen rules and their controls, with A out of sample
    beside them. Returns (per rule dict, traded rows)."""
    seg = segment_of(rows["start"].to_numpy(float))
    M = model_rows(rows)
    mseg = segment_of(M["start"].to_numpy(float))
    A = M[mseg == "A"]
    fp_ok = spec.get("a_fingerprint") == fingerprint(rows[seg == "A"])
    Xa = A[FEATURES].to_numpy(float)
    oos = a_oos_mask(A["start"].to_numpy(float))
    days = {"A": n_days(A[oos]), "B": n_days(M[mseg == "B"]), "C": n_days(M[mseg == "C"])}
    res, traded = [], []
    for r in spec.get("rules", []):
        pol, model, cut = r["policy"], r["model"], r["cut"]
        y = A[f"pnl_{pol}"].to_numpy(float)
        sc_a = walk_forward(Xa, y, A["start"].to_numpy(float), model)
        final = make_model(model).fit(Xa, y)
        out = dict(rule=r, stats={}, units={}, ctl_stats={}, ctl_units={}, b_pass=False, c_pass=None, c_open=False)
        segs = {"A": (A[oos], sc_a[oos])}
        B = M[mseg == "B"]
        segs["B"] = (B, final.predict(B[FEATURES].to_numpy(float)) if len(B) else np.array([]))
        for name in ("A", "B", "C"):
            if name == "C":
                b = out["stats"].get("B", {})
                out["b_pass"] = bool(b.get("n", 0) and b["mean"] > 0 and b["p"] < B_ALPHA)
                if not (out["b_pass"] and complete_c and fp_ok):
                    break
                C = M[mseg == "C"]
                segs["C"] = (C, final.predict(C[FEATURES].to_numpy(float)) if len(C) else np.array([]))
                out["c_open"] = True
            S, sc = segs[name]
            sel = np.isfinite(sc) & (sc >= cut) if len(S) else np.zeros(0, bool)
            tr = S[sel]
            out["stats"][name] = cl_stat(tr[f"pnl_{pol}"].to_numpy(float), tr["market"].astype(str).to_numpy())
            out["units"][name] = units(tr, pol, days[name])
            traded.append(tr.assign(rule=r["id"], segment=name, score=sc[sel], policy=pol))
            ctl = r.get("control")
            if ctl:
                v = ctl["sign"] * S[ctl["feature"]].to_numpy(float)
                cs = np.isfinite(v) & (v >= ctl["cut"])
                ct = S[cs]
                out["ctl_stats"][name] = cl_stat(ct[f"pnl_{pol}"].to_numpy(float), ct["market"].astype(str).to_numpy())
                out["ctl_units"][name] = units(ct, pol, days[name])
                traded.append(ct.assign(rule=r["id"] + "-ctl", segment=name, score=v[cs], policy=pol))
        if out["c_open"]:
            c = out["stats"]["C"]
            out["c_pass"] = bool(c["n"] and c["mean"] > 0 and c["p"] < C_ALPHA)
        res.append(out)
        log(f"{r['id']} {pol} {model} q={r['q']}: " + ", ".join(f"{k} {v['mean']:+.4f} ({v['n']})"
                                                                  for k, v in out["stats"].items()))
    tr = pd.concat(traded, ignore_index=True) if traded else pd.DataFrame()
    return res, tr, dict(days=days, fp_ok=fp_ok)


# --------------------------------------------------------------------------- the run
def out_paths(out):
    """(report, frozen json, rows csv.gz) from --out: real/mix-hf.md -> real/mix-frozen.json, real/mix-rows.csv.gz."""
    out = Path(out)
    stem = out.stem
    prefix = stem[:-2] if stem.endswith("-hf") else stem + "-"
    return out, out.with_name(f"{prefix}frozen.json"), out.with_name(f"{prefix}rows.csv.gz")


def run(workdir, out, days=None, dataset=None, names=None, inputs=INPUTS, budget_s=BUDGET_S, log=None):
    """Every daily archive (the last `days`; or the archives in `names`), per-day shards in the workdir,
    then the freeze on A, B / C, the report and the traded rows."""
    import traceback

    import cross
    log = log or (lambda s: print(s, flush=True))
    if dataset and dataset != cross.DS:
        cross.set_dataset(dataset)
    workdir = Path(workdir)
    shards = workdir / "mix-shards"
    shards.mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    kl, missing = hf.klines(workdir)
    log(f"klines: {len(kl):,} minutes, missing months {missing or 'none'}")
    fp, dv = load_inputs(inputs)
    log(f"inputs: {len(fp):,} factor windows, {len(dv):,} DVOL hours")
    arcs = cross.archives(cross.fetch("MANIFEST.txt").decode())
    if names:
        arcs = [a for a in arcs if a[0] in set(names)]
    elif days:
        arcs = arcs[-int(days):]
    infos, failed, skipped, carry, own = [], [], [], None, []
    files = []
    for name, _ in arcs:
        day = name[15:25]
        if time.time() - t_start > budget_s:
            skipped.append(day)
            continue
        try:
            local = _fetch(cross, name, workdir / name, log)
            feat, mk, rs = cross.read_day(local)
            hf._release()
            rs, provisional = hf.final_resolution(rs)
            mt = cross.market_table(hf.shrink_markets(mk), rs)
            del mk, rs
            hf._release()
            binance = cross.read_binance(local)
            prints = cross.read_poly_trades(local)
            Path(local).unlink()
            if len(binance):
                tt = pd.to_numeric(binance["trade_ts_ms"], errors="coerce")
                own.append(j2.minute_closes(tt.to_numpy(float) / 1000.0, binance["price"].to_numpy(float)))
            own_c = pd.concat(own).groupby(level=0).last() if own else pd.Series(dtype=float)
            closes = hf.closes_for(kl, own_c, missing)
            rows, info = day_rows(feat, mt, binance, prints, carry=carry, factor=fp, dvol=dv, closes=closes, day=day)
            info["provisional"] = provisional
            del feat, prints, mt
            if len(binance):
                carry = binance[tt >= tt.max() - CARRY_S * 1000][["trade_ts_ms", "recv_ts_ms", "price"]].copy()
            else:
                carry = None
            del binance
            hf._release()
            f = shards / f"{day}.parquet"
            rows.to_parquet(f, index=False)
            files.append(f)
            infos.append(info)
            log(f"{name}: {info['markets']} markets, {info['jump']:,} jump / {info['fixed']:,} fixed / "
                f"{info['random']:,} random points, {info['rows']:,} rows ({info['no_entry']:,} without entry) "
                f"({time.time() - t_start:.0f} s)")
        except Exception:
            log(f"{name}: failed\n{traceback.format_exc()}")
            failed.append(day)
            carry = None
    rows = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True) if files else _empty_rows()
    return analyze(rows, out, infos, failed, skipped, len(arcs), missing, ds=cross.DS, log=log)


def _fetch(cross, name, dest, log=print, tries=FETCH_TRIES):
    """cross.fetch with a few retries (a C archive that cannot be read keeps the lockbox closed)."""
    for k in range(tries):
        try:
            return cross.fetch(name, dest)
        except OSError as e:
            if k + 1 == tries:
                raise
            log(f"{name}: download failed ({e!r}), retry {k + 1}")
            Path(dest).unlink(missing_ok=True)
            time.sleep(15 * (k + 1))


def analyze(rows, out, infos=(), failed=(), skipped=(), n_arcs=0, missing=(), ds=None, log=print):
    """Segments, freeze on A, B / C, report and traded rows (separate from run() for the tests)."""
    out, frozen_path, rows_path = out_paths(out)
    if len(rows):
        rows = rows.drop_duplicates(["market", "kind", "t", "side"]).reset_index(drop=True)
        seg = segment_of(rows["start"].to_numpy(float))
        rows = rows[seg != ""].reset_index(drop=True)
    seg = segment_of(rows["start"].to_numpy(float)) if len(rows) else np.array([], dtype=object)
    # C is opened only when every archive of C was read (none skipped for time, none failed)
    complete_c = not any(SEGMENTS[2][1] <= d < SEGMENTS[2][2] for d in list(skipped) + list(failed))
    A = rows[seg == "A"]
    spec, fresh, _ = freeze(A, frozen_path, log)  # A only: nothing of B or C reaches the freeze
    del A
    res, traded, meta = evaluate(rows, spec, complete_c=complete_c, log=log)
    meta["rows_note"] = write_traded(traded, rows_path)
    L = report(rows, spec, fresh, res, meta, infos, failed, skipped, n_arcs, missing, ds, frozen_path, rows_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    log("\n".join(L))
    return rows, spec, res


TRADED_COLS = ["rule", "segment", "policy", "market", "start", "t", "kind", "side", "jdir", "price", "ask_size", "won",
               "score", "pnl", "hold", "how", "sh20"]
ROWS_MAX_BYTES = 19_500_000


def write_traded(traded, path):
    """The rows the frozen rules and their controls traded (their own policy's pnl, holding time, exit kind
    and 20-share size) to path; under ROWS_MAX_BYTES, dropping the A rows of the controls and then all A
    rows when needed. Returns a note for the report."""
    path = Path(path)
    if not len(traded):
        pd.DataFrame(columns=TRADED_COLS).to_csv(path, index=False)
        return "没有交易行。"
    tr = traded.reset_index(drop=True)
    pol = tr["policy"].astype(str).to_numpy()
    for k in ("pnl", "hold", "how", "sh20"):
        tr[k] = np.nan
        for p_ in np.unique(pol):
            m = pol == p_
            tr.loc[m, k] = tr.loc[m, f"{k}_{p_}"].to_numpy(float)
    tr = tr[TRADED_COLS].copy()
    tr["t"] = tr["t"].round(3)
    for c in ("price", "ask_size", "score", "pnl", "hold", "sh20"):
        tr[c] = tr[c].astype(float).round(5)
    seg, ctl = tr["segment"].astype(str), tr["rule"].astype(str).str.endswith("-ctl")
    for note, keep in (("全部", np.ones(len(tr), bool)), ("去掉对照的 A 行", ~((seg == "A") & ctl)), ("去掉 A 行", seg != "A")):
        tr[keep].to_csv(path, index=False)
        if path.stat().st_size <= ROWS_MAX_BYTES:
            return f"{note}（{int(keep.sum()):,} 行）"
    return f"去掉 A 行后仍有 {path.stat().st_size / 1e6:.1f} MB"


# --------------------------------------------------------------------------- report
def _c(x, nd=2):
    return "–" if x is None or not np.isfinite(x) else f"{100 * x:+.{nd}f}¢"


def _st(s):
    if not s or not s.get("n"):
        return "–（0 笔）"
    return f"{_c(s['mean'])} ±{100 * s['se']:.2f}（t {s['t']:.1f}，{s['n']:,} 笔，{s['k']:,} 个市场）"


def _p(s):
    return "–" if not s or not np.isfinite(s.get("p", np.nan)) else f"{s['p']:.4f}"


def _usd(x):
    return "–" if x is None or not np.isfinite(x) else f"${x:,.2f}"


def _units_line(name, u, s):
    if not u or not u.get("days"):
        return f"| {name} | – | – | – | – | – | – | – | – |"
    return (f"| {name} | {u['days']} | {u['trades']:.1f} | {u['sh5']:,.0f} / {u['sh20']:,.0f} | "
            f"{_usd(u['cost5'])} / {_usd(u['cost20'])} | {_usd(u['profit5'])} / {_usd(u['profit20'])} | "
            f"{_usd(u['cap5'])} / {_usd(u['cap20'])} | {_usd(u['peak5'])}（{_usd(u['pmax5'])}） / "
            f"{_usd(u['peak20'])}（{_usd(u['pmax20'])}） | {_st(s)} |")


UNITS_HEAD = ["| 段 | 天数 | 笔/天 | 份/天（每笔 5 份 / 每笔 ≤ 20 份） | 买入花费 $/天（5 / 20） | 利润 $/天（5 / 20） | "
              "平均占用资金 $（5 / 20） | 当天峰值占用 $：中位（最大）（5 / 20） | 每份盈亏 ± 标准误 |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---|"]


def cost_table(rows, segs=("A", "B")):
    """Raw per-share pnl of every policy by row group (no model, no filter), A and B only."""
    seg = segment_of(rows["start"].to_numpy(float))
    kind = rows["kind"].astype(str).to_numpy()
    side, jdir = rows["side"].to_numpy(int), rows["jdir"].to_numpy(int)
    groups = (("急动这一方", (kind == "jump") & (side == jdir)), ("急动反方向", (kind == "jump") & (side == -jdir)),
              ("固定时点（两方）", kind == "fixed"), ("随机时点、随机方向", kind == "random"))
    mk = rows["market"].astype(str).to_numpy()
    L = []
    for sg in segs:
        L += ["", f"**{sg} 段**", "", "| 执行方式 | " + " | ".join(g for g, _ in groups) + " |", "|---|" + "---:|" * len(groups)]
        for pol in POLICIES:
            cells = []
            for _, m in groups:
                k = m & (seg == sg)
                st = cl_stat(rows[f"pnl_{pol}"].to_numpy(float)[k], mk[k])
                cells.append("–" if not st["n"] else f"{_c(st['mean'])} ±{100 * st['se']:.2f}（{st['n']:,}）")
            L.append(f"| {POLICY_NAMES[pol]} | " + " | ".join(cells) + " |")
        cells = []
        for _, m in groups:
            k = m & (seg == sg)
            if not k.any():
                cells.append("–")
                continue
            p = rows["price"].to_numpy(float)[k]
            sp = rows["spread"].to_numpy(float)[k]
            cells.append(f"{np.nanmean(p):.3f} / {100 * np.nanmean(sp):.2f}¢ / {100 * np.nanmean(fee(p)):.2f}¢")
        L.append("| 买入价 / 决策时价差 / 买入手续费（平均） | " + " | ".join(cells) + " |")
        if all(f"how_mk{x}_10" in rows for x in MAKER_X):
            cells = []
            for _, m in groups:
                k = m & (seg == sg)
                cells.append("–" if not k.any() else
                             " / ".join(f"{np.mean(rows[f'how_mk{x}_10'].to_numpy()[k] == 2):.0%}" for x in MAKER_X))
            L.append("| 挂单 10 秒内成交率（+1 / +2 / +3 / +5¢） | " + " | ".join(cells) + " |")
    return L


def report(rows, spec, fresh, res, meta, infos, failed, skipped, n_arcs, missing, ds, frozen_path, rows_path):
    days = [i["day"] for i in infos]
    seg = segment_of(rows["start"].to_numpy(float)) if len(rows) else np.array([], dtype=object)
    L = [f"# 成本侧改造 + 多信号叠加（MIX.md）：Hugging Face 5–8 月 BTC 5m 毫秒盘口（{ds or 'whodisidk'}）", "",
         "设计在 [`MIX.md`](../MIX.md)（跑之前写死并提交），代码 `mix_hf.py`。纸面研究，只用行情数据，不下单。"
         "所有价格是每份（1 份赢了付 $1），¢ = 美分；手续费 taker 0.07·p(1 − p)，挂单方不付。", "",
         f"数据：{n_arcs} 个日档里读到 {len(days)} 个（{days[0] if days else '–'} 至 {days[-1] if days else '–'}）"
         + (f"，读失败 {len(failed)} 个：{'、'.join(failed)}" if failed else "")
         + (f"，因时间预算没读 {len(skipped)} 个：{'、'.join(skipped)}" if skipped else "") + "。"
         + f"只用官方最终结果已知的 BTC 5m 市场（{sum(i['markets'] for i in infos):,} 个，按日档计）。", ""]
    if len(rows):
        kind = rows["kind"].astype(str).to_numpy()
        L += ["| 段 | 市场开盘日期 | 天数 | 市场 | 急动决策点行 | 固定时点行 | 随机对照行 |", "|---|---|---:|---:|---:|---:|---:|"]
        for name, a, b in SEGMENTS:
            k = seg == name
            L.append(f"| {name} | {a} – {(pd.Timestamp(b) - pd.Timedelta(days=1)).strftime('%Y-%m-%d')} | "
                     f"{n_days(rows[k])} | {rows.loc[k, 'market'].nunique():,} | {int((k & (kind == 'jump')).sum()):,} | "
                     f"{int((k & (kind == 'fixed')).sum()):,} | {int((k & (kind == 'random')).sum()):,} |")
        L += ["", "每个急动 / 固定决策点算两行（买 Up、买 Down 各一行）；只留下 0.5 秒后能按卖一买进的行"
              "（盘口 1 秒内变过、在交易状态、没交叉、0.02–0.98、卖一 ≥ 5 份）。", ""]
    L += ["## 规则（照 MIX.md）", "",
          f"- **决策点**（记录机收到时刻）：币安逐笔成交相对 1–5 秒前的最后一笔 ≥ {JUMP_BP:g} bp 的急动（剩 285–20 秒，同一市场间隔 ≥ 5 秒），"
          "加每个市场剩 240、180、120、60 秒的固定时点。",
          "- **特征**（只用决策时刻及之前收到的数据，按买的这一方取向）：" + "、".join(FEATURE_NAMES[f] for f in FEATURES) + "。",
          f"- **买入**：决策后 0.5 秒按这一方的卖一吃单（≥ 5 份）。**平仓**：① 买后 h = {'、'.join(map(str, HOLDS))} 秒决定按买一吃单卖"
          "（再晚 0.5 秒成交，买一 ≥ 5 份，否则持有到结算）；② 买后在 买入价 + x（x = "
          f"{'、'.join(map(str, MAKER_X))}¢，最高 0.99）挂卖单，0.5 秒后生效；之后这一方代币有吃单买入价 ≥ 挂单价的成交、"
          "或买一 ≥ 挂单价才算成交，到 h 秒还没成交就撤单按 ① 吃单卖；③ 持有到官方结算。共 21 种执行方式。",
          f"- **模型**：每种执行方式各拟合岭回归（α = {RIDGE_ALPHA:g}，特征标准化）和梯度提升树（深 3、8 叶、每叶 ≥ 400、"
          "学习率 0.05、200 轮），预测每份盈亏。A 内按周滚动（每周用开始前已结束的市场训练，前 14 天只训练），得到 A 的样本外分数。",
          f"- **挑选**：规则 = （执行方式，模型，样本外分数前 {'、'.join(f'{100 * q:g}%' for q in QS)}），门槛取 A 样本外分数的分位数；"
          f"只按 A 样本外每份盈亏挑最好的 {N_PICK} 个（至少 {MIN_A_TRADES} 笔），冻结在 `{frozen_path.name}`，然后才读 B、C。"
          f"B 和 C 用全部 A 训练的模型打分。B：每份 > 0 且单边 p < 0.05/3（按市场聚类）才进锁箱；C 只算一次，每份 > 0 且 p < 0.05 才通过。",
          "- **单信号对照**：同一执行方式、同一比例，只用 A 样本外最好的单个特征（正反两个方向都试）排名。", ""]
    rules = spec.get("rules", [])
    L += ["## 判定", ""]
    if not rules:
        L += [f"A 段没有一个候选规则达到 {MIN_A_TRADES} 笔样本外交易，没有可冻结的规则。"]
    frozen_note = ("本次运行冻结" if fresh else "冻结文件已存在，沿用（未重新冻结）") + f"：{spec.get('made', '–')}。"
    if rules:
        L += [frozen_note + ("" if meta.get("fp_ok", True) else
                             " **注意：A 段数据与冻结时不同（指纹不符），C 不计算。**"), "",
              "| 规则 | 执行方式 | 模型 | 前 q | A 样本外每份 | B 每份（p） | B 通过（p < 0.0167） | C 每份（p） | C 通过 |",
              "|---|---|---|---:|---|---|---|---|---|"]
        for o in res:
            r = o["rule"]
            b, c = o["stats"].get("B"), o["stats"].get("C")
            cells = [r["id"], POLICY_NAMES[r["policy"]], r["model"], f"{100 * r['q']:g}%", _st(o["stats"].get("A"))]
            cells += [f"{_st(b)}（p {_p(b)}）", "是" if o["b_pass"] else "否"]
            if o["c_open"]:
                cells += [f"{_st(c)}（p {_p(c)}）", "**通过，上前向**" if o["c_pass"] else "否"]
            else:
                cells += ["未开锁箱（B 未通过）" if not o["b_pass"] else "未开锁箱（C 数据不全或 A 数据与冻结时不同）", "–"]
            L.append("| " + " | ".join(cells) + " |")
        L += ["", "B、C 都用全部 A 训练出的模型打分，门槛是冻结时 A 样本外分数的分位数（所以 B、C 的成交比例不一定正好是 q）。"]
    L += ["", "## 每条规则每天的量（带单位）", "",
          "份数：每笔 5 份（卖一至少 5 份），或每笔 min(20, 买入时卖一数量, 吃单卖出时买一数量) 份；挂单成交按整单成交算。"
          "买入花费 = 份数 × (买入价 + 手续费)；利润 = 份数 × 每份盈亏；占用资金按买入成交到卖出成交（持有到结算的算到市场结束）"
          "的时间加权，峰值是同一天同时持仓花费之和的最大值。天数 = 该段有数据的 UTC 天数（A 只算样本外 "
          f"{spec.get('a_oos_from', '–')} 起）。", ""]
    for o in res:
        r = o["rule"]
        L += [f"### {r['id']}：{POLICY_NAMES[r['policy']]}，{r['model']}，前 {100 * r['q']:g}%（分数门槛 {r['cut']:+.4f}）", ""]
        L += UNITS_HEAD
        for name in ("A", "B", "C"):
            if name in o["units"]:
                L.append(_units_line(f"{name}{'（样本外）' if name == 'A' else ''}", o["units"][name], o["stats"][name]))
            else:
                L.append(f"| {name} | 未开锁箱 | – | – | – | – | – | – | – |")
        ctl = r.get("control")
        if ctl:
            L += ["", f"单信号对照：{FEATURE_NAMES[ctl['feature']]}（{'越大越好' if ctl['sign'] > 0 else '越小越好'}，"
                  f"门槛 {ctl['cut']:+.4f}）", ""] + UNITS_HEAD
            for name in ("A", "B", "C"):
                if name in o["ctl_units"]:
                    L.append(_units_line(name, o["ctl_units"][name], o["ctl_stats"][name]))
                else:
                    L.append(f"| {name} | 未开锁箱 | – | – | – | – | – | – | – |")
        else:
            L += ["", "单信号对照：没有单个特征在 A 样本外达到 300 笔。"]
        L.append("")
    cand = pd.DataFrame(spec.get("candidates", []))
    if len(cand):
        top = cand[cand["n"] >= MIN_A_TRADES].sort_values("mean", ascending=False).head(15)
        L += ["## A 样本外候选（前 15，挑选只看这一列）", "",
              f"{spec.get('n_candidates', len(cand))} 个候选，{spec.get('n_eligible', len(top))} 个达到 {MIN_A_TRADES} 笔。"
              "最好的几个是从很多候选里挑出来的最大值，A 上的数字偏乐观，所以要看 B。", "",
              "| 执行方式 | 模型 | 前 q | 每份 ± 标准误 |", "|---|---|---:|---|"]
        for r in top.itertuples(index=False):
            L.append(f"| {POLICY_NAMES[r.policy]} | {r.model} | {100 * r.q:g}% | {_st(r._asdict())} |")
        L.append("")
    if len(rows):
        L += ["## 成本结构：不经模型筛选的原始每份盈亏（A、B；C 是锁箱，不列）", "",
              "每格：每份 ± 按市场聚类的标准误（行数）。随机对照 = 同一市场 285–20 秒内随机时刻、随机方向（每个急动配一个）。"
              "“急动这一方”和随机行的差就是信号的毛信息；两者都为负说明成本（价差 + 两笔吃单费）吃掉了它。"]
        L += cost_table(rows)
        L.append("")
    L += ["## 数据覆盖", "", "<details><summary>每个日档</summary>", "",
          "| 日期 | 市场 | 只有临时结算（不用） | 急动点 | 固定点 | 随机点 | 行（能买进） | 决策点行买不进 |",
          "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for i in infos:
        L.append(f"| {i['day']} | {i['markets']} | {i.get('provisional', 0)} | {i['jump']:,} | {i['fixed']:,} | "
                 f"{i['random']:,} | {i['rows']:,} | {i['no_entry']:,} |")
    L += ["", "</details>", "",
          f"冻结文件 `{frozen_path.name}`，冻结规则（和对照）交易过的逐行数据在 `{rows_path.name}`"
          f"（{meta.get('rows_note', '')}；t = 决策时刻，记录机时钟，秒；pnl 是那条规则执行方式的每份盈亏，"
          "how：0 持有到结算、1 吃单卖、2 挂单成交）。"
          + (f" K 线缺 {'、'.join(missing)}，这些月份的分钟收盘改用日档里的币安逐笔推出。" if missing else "")]
    return L
