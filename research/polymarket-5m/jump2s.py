"""JUMP2S.md, the shared part: "buy the side of a >= 1.2 bp Binance jump 2 s later and hold to
settlement", checked on data the rule was not chosen on (paper research, market data only).

The design is fixed in JUMP2S.md (committed before anything was run). This module holds what does
not depend on the data source: jump detection, the entry plan with its controls, the 4 h / 1 h trend
features of the two side hypotheses, the driftless-model fair value for the price / outcome
decomposition, the statistics and decision rules, and the Chinese report tables. The lanes load the
data and price the plan: jump2s_hf.py (+ cross.py, Hugging Face May-Aug, run on GitHub) and
jump2s_local.py (kacho Mar-May books, Binance 1 s klines Mar-Sep).

Row schema shared by the lanes (one row per simulated 1-share buy):
  market, t0 (jump time, s), t_ex (exchange time of the jump print; = t0 without t_obs),
  kind in big | small | opposite | random, sign (+1 bought Up, -1 bought Down), price_type in
  book | trade, price, won (0/1), pnl (= won - price - fee); for the decomposition (big rows):
  mid0, mid2 (the bought side's Polymarket mid just before the jump / ENTRY_S after it),
  fair0, fair2 (model fair of the bought side then), cont_bp (outcome side); and ret4h, vr60, h1, h2.

Where JUMP2S.md is silent, the conservative choice made here:
- Jump reference (trades): the last print with ts <= t - 1 s and at most 5 s older than t (1e-6 s
  float tolerance on both); a print without such a reference is no candidate. Thresholds are
  inclusive: big |d| >= 1.2 bp, small 1.0 <= |d| < 1.2 bp (1e-9 bp tolerance for float noise).
- 1 s closes: d = log close(s) - log close(s - 1) only when both seconds are present (gaps are not
  bridged); t0 = s, the open time of the jump second (the move is known at s + 1).
- Window filters on the reported t0 (with `t_obs`, the recorder's receipt time): t0 - start >= 15 and
  end - t0 >= 12, both inclusive. Spacing is greedy: a candidate is kept when it is >= 10 s after the
  last kept jump of the same market and bucket. Big and small are separate rule sets, spaced
  separately. The big rule is exactly JUMP2S.md's. A small candidate (1.0..1.2 bp) is dropped
  before the small spacing when a same-sign big candidate (>= 1.2 bp, any print) becomes known in
  [t0, t0 + ENTRY_S]: that move was already a big jump when the buy happens. Without this, in trades
  mode most "small" rows were the first prints of big moves (a sharp move prints 1.1 bp a few ms
  before it prints 1.3 bp; 72% of the small jumps of 2026-05-27 had such a big twin, median 8 ms
  later), so the control was not independent of the big rows. With 1 s closes it drops 9-10%.
- Controls: "opposite" = every kept big jump, other side, same moment; "random" = one per kept big
  jump, uniform time in the same market's allowed span [start + 15, end - 12], random side (seeded).
- Trend features: minutes are Binance minute open times; a minute is usable at t0 only once it has
  closed (open + 60 <= t0). Pass the jump's exchange time (t_ex). Missing minutes are not filled: a
  feature that needs one is NaN, and NaN rows are left out of both an H subset and its complement.
  Variances are sample variances (ddof = 1), as in regime.py. H1 needs ret4h != 0.
- Model fair: binary.prob_up_european for point-price markets (opened before 2026-08-07),
  binary.prob_up for TWAP markets (window by pm_outcomes.rule_window). The strike and the settlement
  are the Binance proxies of pm_outcomes (point: the last 1 s close before the time; TWAP_w: the
  mean of the w 1 s closes before it), so no Chainlink basis enters. Inside a TWAP window the
  model uses whole seconds (the seconds before floor(t) are known). sigma: std of the 600 (>= 300)
  one-second log returns ending at the second before t (cross.stale_trades); pass the exchange time.
  A per-second price at time t is the close of second floor(t) - 1 (never a later one).
- Price side: ratio of sums, sum(mid2 - mid0) / sum(fair2 - fair0), not a mean of ratios; its SE by
  the clustered delta method. Outcome side: settlement proxy minus the Binance log price at entry,
  along the jump, in bp (cont_bp).
- Trade-price entries ("first print in [t0 + 2, t0 + 4]", either token, the other one as 1 - p) use the
  same 0.02..0.98 band as the book; there is no size rule for a print.
- Clustered SE: kacho_late.cl's formula. A difference of two subsets (markets can be in both) and
  a ratio use the same formula on per-market influence sums. Rows with NaN pnl are dropped.
- Days are UTC calendar days of t0. Daily figures and the 3-day windows count days with >= 20
  trades; a 3-day window is three consecutive calendar days that all qualify, scored by the pooled
  per-share mean (as the 547-trade +2.04c) against +2c.
- Decisions: t >= 2 and "at least two of the three month blocks" exactly as written; a block with no
  trades counts as not positive.
"""
from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

import binary as bo

# --------------------------------------------------------------------------- JUMP2S.md constants
JUMP_BP = 1.2            # big jump: |d| >= 1.2 bp
SMALL = (1.0, 1.2)       # small-jump control: 1.0 <= |d| < 1.2 bp
ENTRY_S = 2.0            # buy 2 s after the jump
SPACING_S = 10.0         # next jump of a market (same bucket) >= 10 s after the last kept one
OPEN_GAP_S = 15.0        # jump >= 15 s after the open
END_GAP_S = 12.0         # jump >= 12 s before the end (>= 10 s left at the buy)
REF_MIN_S = 1.0          # reference print at least 1 s earlier ...
REF_MAX_AGE_S = 5.0      # ... and at most 5 s earlier
BOOK_MAX_AGE_S = 1.0     # book snapshot at or before the entry, at most 1 s old
TRADE_WIN_S = (2.0, 4.0)  # first print 2.0..4.0 s after the jump
MIN_SHARES = 5.0
BAND = (0.02, 0.98)
SIGMA_N, SIGMA_MIN = 600, 300
RET4H_MIN = 240
VR_N, VR_Q = 60, 5
DAY_MIN_N = 20
HOT = 0.02               # "3 consecutive days >= +2c"
T_EPS, BP_EPS = 1e-6, 1e-9

HF_BLOCKS = (("5月底+6月", "2026-05-25", "2026-07-01"), ("7月", "2026-07-01", "2026-08-01"),
             ("8月", "2026-08-01", "2026-09-01"))
KACHO_BLOCKS = (("3月", "2026-03-01", "2026-04-01"), ("4月", "2026-04-01", "2026-05-01"),
                ("5月", "2026-05-01", "2026-06-01"))
KLINE_BLOCKS = tuple((f"{m}月", f"2026-{m:02d}-01", f"2026-{m + 1:02d}-01") for m in range(3, 10))

KINDS = ("big", "small", "opposite", "random")
KIND_NAMES = {"big": "大跳（≥ 1.2 bp）", "small": "小跳（1.0–1.2 bp）", "opposite": "大跳同一时刻买反方向",
              "random": "随机时刻、随机方向"}
PRICE_NAMES = {"book": "盘口价", "trade": "成交价"}

JUMP_COLS = ["market", "t0", "t_ex", "sign", "size_bp", "bucket", "i", "ref_i", "x0", "x1"]


# --------------------------------------------------------------------------- jumps and the entry plan
def _windows(windows):
    w = pd.DataFrame(windows)
    return w["market"].to_numpy(), w["start"].to_numpy(float), w["end"].to_numpy(float)


def jumps(ts_s, logp, windows, mode="trades", t_obs=None):
    """Kept jumps (big and small) inside each market window [start, end).

    ts_s: sorted times in seconds (trade times, or 1 s kline open times for mode="closes"); logp:
    log prices. mode="trades": d_i = logp_i - logp of the last print at least REF_MIN_S earlier and at
    most REF_MAX_AGE_S earlier; mode="closes": d_i = logp_i - logp_{i-1} when ts_i - ts_{i-1} == 1.
    `t_obs` (optional, same length): the time each print became known (e.g. recorder receipt); the
    window filters, spacing and the reported t0 then use it, the move itself uses ts_s.
    windows: DataFrame (or dict) with market, start, end in seconds.
    big: |d| >= JUMP_BP; small: SMALL[0] <= |d| < JUMP_BP and no same-sign big candidate known in
    [t0, t0 + ENTRY_S] (the move had not become a big jump by the buy).

    Returns a DataFrame with JUMP_COLS: market, t0, t_ex (ts_s of the jump print), sign (+1/-1),
    size_bp (|d| in bp), bucket ("big" / "small"), i and ref_i (positions in the input arrays), x0, x1
    (log price of the reference and of the jump print), sorted by t0.
    """
    ts = np.asarray(ts_s, dtype=float)
    lp = np.asarray(logp, dtype=float)
    n = len(ts)
    mk, st, en = _windows(windows)
    if n == 0 or len(mk) == 0:
        return pd.DataFrame(columns=JUMP_COLS)
    if np.any(ts[1:] < ts[:-1]):
        raise ValueError("ts_s must be sorted")
    if mode == "trades":
        j = np.searchsorted(ts, ts - REF_MIN_S + T_EPS, "right") - 1
        jj = np.maximum(j, 0)
        ok = (j >= 0) & (ts - ts[jj] <= REF_MAX_AGE_S + T_EPS)
    elif mode == "closes":
        idx = np.arange(n)
        jj = np.maximum(idx - 1, 0)
        ok = (idx >= 1) & (np.abs(ts - ts[jj] - 1.0) <= T_EPS)
    else:
        raise ValueError(f"mode {mode!r}")
    with np.errstate(invalid="ignore"):
        d = np.where(ok, lp - lp[jj], np.nan)
        size = np.abs(d) * 1e4
        cand = np.flatnonzero(size >= SMALL[0] - BP_EPS)
    obs = ts if t_obs is None else np.asarray(t_obs, dtype=float)
    ci = cand[np.argsort(obs[cand], kind="stable")]
    co = obs[ci]
    big = size[ci] >= JUMP_BP - BP_EPS
    small = ~big & ~_grows_big(co, np.sign(d[ci]), big)
    lo = np.searchsorted(co, st + OPEN_GAP_S - T_EPS, "left")
    hi = np.searchsorted(co, en - END_GAP_S + T_EPS, "right")
    keep, mks, bks = [], [], []
    for m, a, b in zip(mk, lo, hi):
        if b <= a:
            continue
        for bucket, sel in (("big", big[a:b]), ("small", small[a:b])):
            idx, tt = ci[a:b][sel], co[a:b][sel]
            k, got = 0, []
            while k < len(tt):
                got.append(idx[k])
                k = max(k + 1, int(np.searchsorted(tt, tt[k] + SPACING_S - T_EPS, "left")))
            keep += got
            mks += [m] * len(got)
            bks += [bucket] * len(got)
    if not keep:
        return pd.DataFrame(columns=JUMP_COLS)
    keep = np.asarray(keep, dtype=np.int64)
    out = pd.DataFrame({"market": mks, "t0": obs[keep], "t_ex": ts[keep], "sign": np.sign(d[keep]).astype(int),
                        "size_bp": size[keep], "bucket": bks, "i": keep, "ref_i": jj[keep],
                        "x0": lp[jj[keep]], "x1": lp[keep]})
    return out.sort_values(["t0", "bucket"], kind="stable").reset_index(drop=True)


def _grows_big(co, sg, big):
    """For candidates sorted by known time `co` with signs `sg`: whether a same-sign big candidate
    is known in [co, co + ENTRY_S] (the small-jump control leaves those out)."""
    out = np.zeros(len(co), bool)
    for s in (-1.0, 1.0):
        tb = co[big & (sg == s)]
        if not len(tb):
            continue
        m = sg == s
        k = np.searchsorted(tb, co[m] - T_EPS, "left")
        out[m] = (k < len(tb)) & (tb[np.minimum(k, len(tb) - 1)] <= co[m] + ENTRY_S + T_EPS)
    return out


def random_controls(big, windows, seed=0):
    """One random-time, random-side row per kept big jump, in the same market: t0 uniform in
    [start + OPEN_GAP_S, end - END_GAP_S], sign +1/-1 with equal odds (seeded, in t0 order)."""
    mk, st, en = _windows(windows)
    span = {m: (s, e) for m, s, e in zip(mk, st, en)}
    b = big.sort_values("t0", kind="stable")
    rng = np.random.default_rng(seed)
    lo = np.array([span[m][0] for m in b["market"]], dtype=float) + OPEN_GAP_S
    hi = np.array([span[m][1] for m in b["market"]], dtype=float) - END_GAP_S
    t = rng.uniform(lo, hi) if len(b) else np.array([], float)
    sg = rng.choice(np.array([-1, 1]), len(b)) if len(b) else np.array([], int)
    return pd.DataFrame({"market": b["market"].to_numpy(), "t0": t, "t_ex": t, "sign": sg,
                         "jump_t0": b["t0"].to_numpy()})


def plan(sel, windows, seed=0):
    """Every 1-share buy to price, as rows of kind big | small | opposite | random, with
    jump_sign (the jump's own sign; NaN for random) and t_entry = t0 + ENTRY_S. `sel` is jumps()."""
    big, small = sel[sel["bucket"] == "big"], sel[sel["bucket"] == "small"]
    parts = [big.assign(kind="big", jump_sign=big["sign"]),
             small.assign(kind="small", jump_sign=small["sign"]),
             big.assign(kind="opposite", jump_sign=big["sign"], sign=-big["sign"])]
    rnd = random_controls(big, windows, seed)
    parts.append(rnd.assign(kind="random", jump_sign=np.nan))
    out = pd.concat([p for p in parts if len(p)], ignore_index=True) if any(len(p) for p in parts) else \
        pd.DataFrame(columns=JUMP_COLS + ["kind", "jump_sign"])
    out["t_entry"] = out["t0"].astype(float) + ENTRY_S
    return out.drop(columns=["bucket"], errors="ignore")


# --------------------------------------------------------------------------- entry helpers
def asof_idx(ts, t, max_age=BOOK_MAX_AGE_S):
    """Index of the last snapshot at or before each t, -1 when none or older than max_age."""
    ts, t = np.asarray(ts, float), np.asarray(t, float)
    if not len(ts):
        return np.full(np.shape(t), -1)
    k = np.searchsorted(ts, t + T_EPS, "right") - 1
    ok = (k >= 0) & (t - ts[np.maximum(k, 0)] <= max_age + T_EPS)
    return np.where(ok, k, -1)


def first_print(ts, up_px, t_lo, t_hi, sign):
    """Price of the bought side at the first print (either token) in [t_lo, t_hi]: `up_px` is each
    print's Up-equivalent price (a Down-token print at p is 1 - p); NaN when there is none."""
    ts, up_px = np.asarray(ts, float), np.asarray(up_px, float)
    t_lo, t_hi, sign = np.asarray(t_lo, float), np.asarray(t_hi, float), np.asarray(sign)
    k = np.searchsorted(ts, t_lo - T_EPS, "left")
    kk = np.minimum(k, max(len(ts) - 1, 0))
    ok = (k < len(ts)) & (ts[kk] <= t_hi + T_EPS) if len(ts) else np.zeros(np.shape(t_lo), bool)
    p = np.where(ok, up_px[kk] if len(ts) else np.nan, np.nan)
    return np.where(sign > 0, p, 1.0 - p)


def price_ok(price, size=None):
    """In the 0.02..0.98 band (and, for a book quote, at least MIN_SHARES shown)."""
    p = np.asarray(price, float)
    with np.errstate(invalid="ignore"):
        ok = np.isfinite(p) & (p >= BAND[0] - 1e-12) & (p <= BAND[1] + 1e-12)
        if size is not None:
            s = np.asarray(size, float)
            ok &= np.isfinite(s) & (s >= MIN_SHARES - 1e-12)
    return ok


def pnl(won, price):
    """Per share, taker fee 0.07 p (1 - p) (binary.taker_fee), held to settlement."""
    return bo.long_binary_pnl(won, price)


# --------------------------------------------------------------------------- trend features (H1, H2)
def minute_closes(ts_s, price):
    """1-minute closes indexed by minute open time (int s): the last price with ts in [m, m + 60).
    Works for prints and for 1 s klines (open times; a kline opening at m + 59 closes the minute)."""
    ts, px = np.asarray(ts_s, float), np.asarray(price, float)
    good = np.isfinite(ts) & np.isfinite(px)
    ts, px = ts[good], px[good]
    o = np.argsort(ts, kind="stable")
    m = (np.floor(ts[o] / 60.0) * 60).astype(np.int64)
    return pd.Series(px[o], index=m).groupby(level=0, sort=True).last()


def load_minute_klines(paths, start_s=None, end_s=None):
    """Binance 1m kline zips (data.binance.vision; a directory or a list of files) -> minute closes
    indexed by minute open time (s). Open times in ms or us.  With bounds, rows are streamed in
    timestamp order and parsing stops before the first row at ``end_s``; values after the bound are
    never decoded (used by the A-only legacy migration)."""
    if isinstance(paths, (str, Path)) and Path(paths).is_dir():
        paths = sorted(Path(paths).glob("*-1m-*.zip"))
    if start_s is not None or end_s is not None:
        start_s = -np.inf if start_s is None else float(start_s)
        end_s = np.inf if end_s is None else float(end_s)
        if not start_s < end_s:
            raise ValueError("invalid kline bounds")
        times, closes, last = [], [], -np.inf
        finished = False
        for f in paths:
            if finished:
                break
            with zipfile.ZipFile(f) as z, z.open(z.namelist()[0]) as raw:
                reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8", newline=""))
                for line_no, row in enumerate(reader):
                    if not row:
                        continue
                    if len(row) < 5:
                        raise ValueError("malformed bounded kline row")
                    try:
                        stamp = int(row[0])
                    except (TypeError, ValueError) as error:
                        if line_no == 0 and row[0].strip().lower() in ("open_time", "open time"):
                            continue
                        raise ValueError("invalid bounded kline timestamp") from error
                    t = stamp // 1_000_000 if stamp > 10**14 else stamp // 1000
                    if t <= last:
                        raise ValueError("kline timestamps are not strictly monotonic")
                    last = t
                    if t >= end_s:
                        finished = True
                        break
                    if t < start_s:
                        continue
                    times.append(t)
                    closes.append(float(row[4]))
        s = pd.Series(closes, index=np.asarray(times, dtype=np.int64), dtype=float)
        return s[~s.index.duplicated(keep="last")].sort_index()
    parts = []
    for f in paths:
        with zipfile.ZipFile(f) as z:
            raw = z.read(z.namelist()[0])
        d = pd.read_csv(io.BytesIO(raw), header=None, usecols=[0, 4])
        if not np.issubdtype(d[0].dtype, np.number):  # a header row
            d = d.iloc[1:].astype(float)
        parts.append(d)
    if not parts:
        return pd.Series(dtype=float)
    d = pd.concat(parts, ignore_index=True)
    t = d[0].to_numpy(np.int64)
    t = np.where(t > 10**14, t // 1_000_000, t // 1000)
    s = pd.Series(d[4].to_numpy(float), index=t)
    return s[~s.index.duplicated(keep="last")].sort_index()


def trend_features(closes, t0):
    """(ret4h, vr60) at each t0 (exchange seconds) from minute closes (minute_closes /
    load_minute_klines). m = the last minute closed at or before t0 (m + 60 <= t0).
    ret4h = log close(m) - log close(m - 240 min).
    vr60 = Var(overlapping 5-min sums of the 60 one-minute log returns ending at m)
           / (5 Var(those 60 returns)), ddof = 1. NaN when a needed minute is missing."""
    t0 = np.atleast_1d(np.asarray(t0, float))
    ret4h, vr60 = np.full(len(t0), np.nan), np.full(len(t0), np.nan)
    c = pd.Series(closes)
    c.index = np.asarray(c.index, dtype=np.int64)
    c = c.sort_index()
    c = c[~c.index.duplicated(keep="last")]
    if c.empty or not len(t0):
        return ret4h, vr60
    m0 = int(c.index[0])
    grid = np.arange(m0, int(c.index[-1]) + 60, 60, dtype=np.int64)
    with np.errstate(divide="ignore", invalid="ignore"):
        lc = pd.Series(np.log(c.reindex(grid).to_numpy(float)), index=grid)
    r = lc.diff()
    ret = (lc - lc.shift(RET4H_MIN)).to_numpy()
    v1 = r.rolling(VR_N, min_periods=VR_N).var().to_numpy()
    s5 = r.rolling(VR_Q, min_periods=VR_Q).sum()
    v5 = s5.rolling(VR_N - VR_Q + 1, min_periods=VR_N - VR_Q + 1).var().to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        vr = np.where(v1 > 0, v5 / (VR_Q * v1), np.nan)
    ok = np.isfinite(t0)
    m_last = np.floor((np.where(ok, t0, 0.0) - 60.0) / 60.0) * 60.0
    pos = ((m_last - m0) // 60).astype(np.int64)
    ok &= (pos >= 0) & (pos < len(grid))
    ret4h[ok], vr60[ok] = ret[pos[ok]], vr[pos[ok]]
    return ret4h, vr60


def h1(sign, ret4h):
    """1.0 when the jump has the sign of the 4 h return (0 return -> 0.0), NaN when unknown."""
    r = np.asarray(ret4h, float)
    with np.errstate(invalid="ignore"):
        return np.where(np.isfinite(r), (np.asarray(sign, float) * r > 0).astype(float), np.nan)


def h2(vr60):
    """1.0 when VR60 > 1 (trending), NaN when unknown."""
    v = np.asarray(vr60, float)
    with np.errstate(invalid="ignore"):
        return np.where(np.isfinite(v), (v > 1.0).astype(float), np.nan)


def add_trend(rows, closes, t_col="t_ex"):
    """rows + ret4h, vr60, h1, h2 (features at rows[t_col], H1 with the row's own sign)."""
    r4, vr = trend_features(closes, rows[t_col].to_numpy(float))
    return rows.assign(ret4h=r4, vr60=vr, h1=h1(rows["sign"].to_numpy(float), r4), h2=h2(vr))


# --------------------------------------------------------------------------- model fair (decomposition)
def settle_twap(start):
    """Settlement averaging window (s) of 5m markets opening at `start`: 0 = point price (before
    2026-08-07), 30 (08-07 .. 08-13), 60 (from 08-14); pm_outcomes.rule_window."""
    import pm_outcomes as po
    st = np.floor(np.atleast_1d(np.asarray(start, float))).astype(np.int64)
    return np.asarray(po.rule_window("5m", st), dtype=np.int64)


class Seconds:
    """Binance per-second log prices on a complete 1 s grid: lp[s] = log of the last price in
    [s, s + 1) (= the close of the 1 s kline opening at s), forward filled over gaps of at most
    `ffill` s; sigma[s] = std of the SIGMA_N (>= SIGMA_MIN) one-second log returns ending at s."""

    def __init__(self, ts_s, price, ffill=10):
        ts, px = np.asarray(ts_s, float), np.asarray(price, float)
        good = np.isfinite(ts) & np.isfinite(px) & (px > 0)
        o = np.argsort(ts[good], kind="stable")
        sec = np.floor(ts[good][o]).astype(np.int64)
        s = pd.Series(np.log(px[good][o]), index=sec).groupby(level=0, sort=True).last()
        if s.empty:
            self.s0, self.lp, self.sig = 0, np.array([]), np.array([])
        else:
            g = s.reindex(pd.RangeIndex(int(s.index[0]), int(s.index[-1]) + 1)).ffill(limit=ffill)
            self.s0 = int(s.index[0])
            self.lp = g.to_numpy(float)
            self.sig = g.diff().rolling(SIGMA_N, min_periods=SIGMA_MIN).std().to_numpy(float)
        fin = np.isfinite(self.lp)
        self.csum = np.concatenate([[0.0], np.cumsum(np.where(fin, self.lp, 0.0))])
        self.cnt = np.concatenate([[0], np.cumsum(fin)])

    def _at(self, arr, s):
        s = np.asarray(s, np.int64) - self.s0
        ok = (s >= 0) & (s < len(arr))
        return np.where(ok, arr[np.clip(s, 0, max(len(arr) - 1, 0))] if len(arr) else np.nan, np.nan)

    def price(self, t):
        """Log price known at time t: the close of second floor(t) - 1."""
        return self._at(self.lp, np.floor(np.asarray(t, float)).astype(np.int64) - 1)

    def sigma(self, t):
        """Per-second vol from the returns ending at the second before t."""
        return self._at(self.sig, np.floor(np.asarray(t, float)).astype(np.int64) - 1)

    def total(self, a, b):
        """(sum, count of present seconds) of lp over the seconds [a, b) (integers, b >= a)."""
        a = np.asarray(a, np.int64) - self.s0
        b = np.asarray(b, np.int64) - self.s0
        ok = (a >= 0) & (b <= len(self.lp)) & (b >= a)
        a_, b_ = np.clip(a, 0, len(self.lp)), np.clip(b, 0, len(self.lp))
        return (np.where(ok, self.csum[b_] - self.csum[a_], np.nan),
                np.where(ok, self.cnt[b_] - self.cnt[a_], -1))

    def mean(self, a, b):
        """Mean of lp over [a, b); NaN unless every second is present."""
        s, c = self.total(a, b)
        need = np.asarray(b, np.int64) - np.asarray(a, np.int64)
        with np.errstate(invalid="ignore", divide="ignore"):
            return np.where((c == need) & (need > 0), s / np.maximum(need, 1), np.nan)


def ref_log(sp, start, twap=None):
    """Binance proxy of the price to beat: point -> price at the open; TWAP_w -> mean of the w 1 s
    closes before the open."""
    start = np.atleast_1d(np.asarray(start, float))
    w = settle_twap(start) if twap is None else np.broadcast_to(np.asarray(twap, np.int64), start.shape)
    st = np.floor(start).astype(np.int64)
    return np.where(w > 0, sp.mean(st - w, st), sp.price(st))


def settle_log(sp, start, twap=None):
    """Binance proxy of the settlement price of the market opening at `start`."""
    start = np.atleast_1d(np.asarray(start, float))
    return ref_log(sp, start + bo.WINDOW_S, settle_twap(start) if twap is None else twap)


def fair_side(sign, x, k, sigma, t_el, twap=0, known=0.0):
    """P(the side of `sign` wins) under binary.py's driftless model: x the log price now, k the log
    price to beat, sigma per second, t_el seconds since the open; point settlement (twap = 0):
    binary.prob_up_european with tau = WINDOW_S - t_el; TWAP_w: binary.prob_up with the log prices
    already inside the averaging window summed in `known`."""
    sign, x, k, sigma, t_el, twap, known = np.broadcast_arrays(
        np.asarray(sign, float), np.asarray(x, float), np.asarray(k, float), np.asarray(sigma, float),
        np.asarray(t_el, float), np.asarray(twap, float), np.asarray(known, float))
    with np.errstate(invalid="ignore", divide="ignore"):
        eu = bo.prob_up_european(x, k, sigma, bo.WINDOW_S - t_el)
        w = np.where(twap > 0, twap, bo.TWAP_S)
        tw = bo.prob_up(x, k, sigma, t_el, known_sum=known, twap=w)
    up = np.where(twap > 0, tw, eu)
    up = np.where(np.isfinite(x) & np.isfinite(k) & np.isfinite(sigma) & (sigma > 0), up, np.nan)
    return np.where(sign > 0, up, 1.0 - up)


def model_fair(sp, sign, start, t, x=None, twap=None):
    """fair_side at time t of the market opening at `start` from a Seconds series: x defaults to
    sp.price(t), sigma = sp.sigma(t), strike = ref_log; inside a TWAP window the seconds
    [end - w, floor(t)) are known and t_el = floor(t) - start."""
    start = np.atleast_1d(np.asarray(start, float))
    t = np.broadcast_to(np.asarray(t, float), start.shape)
    w = settle_twap(start) if twap is None else np.broadcast_to(np.asarray(twap, np.int64), start.shape)
    st = np.floor(start).astype(np.int64)
    ti = np.floor(t).astype(np.int64)
    end = st + bo.WINDOW_S
    x = sp.price(t) if x is None else np.broadcast_to(np.asarray(x, float), start.shape)
    a = end - w
    need = np.maximum(ti, a) - a
    s, c = sp.total(a, a + need)
    known = np.where((w > 0) & (need > 0), s, 0.0)
    full = (w == 0) | (need == 0) | (c == need)
    t_el = np.where(w > 0, ti - st, t - start)
    f = fair_side(sign, x, ref_log(sp, start, w), sp.sigma(t), t_el, w, known)
    return np.where(full, f, np.nan)


def continuation_bp(sp, sign, start, x_entry, twap=None):
    """Outcome side: how far BTC (settlement proxy) went from the entry price along the jump, bp."""
    return np.asarray(sign, float) * (settle_log(sp, start, twap) - np.asarray(x_entry, float)) * 1e4


def proxy_up(sp, start, twap=None):
    """1.0 if the Binance settlement proxy >= the proxy price to beat, NaN when unknown."""
    a, b = settle_log(sp, start, twap), ref_log(sp, start, twap)
    with np.errstate(invalid="ignore"):
        return np.where(np.isfinite(a) & np.isfinite(b), (a >= b).astype(float), np.nan)


# --------------------------------------------------------------------------- statistics
def _num(t, col):
    return pd.to_numeric(t[col], errors="coerce") if col in t else pd.Series(np.nan, index=t.index, dtype=float)


def _clean(t, col):
    """Rows with a finite `col` (none when the column is missing)."""
    return t[np.isfinite(_num(t, col).to_numpy(float))]


def cl(t, col="pnl", cluster="market"):
    """(mean, SE clustered by market, n): kacho_late.cl's formula."""
    t = _clean(t, col)
    if not len(t):
        return np.nan, np.nan, 0
    g = t.groupby(cluster)[col].agg(["sum", "count"])
    m = g["sum"].sum() / g["count"].sum()
    r = g["sum"] - m * g["count"]
    k = len(g)
    return m, np.sqrt((r ** 2).sum() * k / max(k - 1, 1)) / g["count"].sum(), len(t)


def stat(t, col="pnl", cluster="market"):
    """dict(mean, se, t, n, k) with the clustered SE."""
    m, se, n = cl(t, col, cluster)
    k = int(_clean(t, col)[cluster].nunique()) if n else 0
    tt = m / se if n and se > 0 else np.nan
    return dict(mean=m, se=se, t=tt, n=n, k=k)


def _psi(t, col, cluster, m, n):
    g = t.groupby(cluster)[col].agg(["sum", "count"])
    return (g["sum"] - m * g["count"]) / n


def diff_stat(t, flag, col="pnl", cluster="market"):
    """Subset (flag == 1) minus complement (flag == 0), rows with NaN flag left out; SE clustered
    by market on the per-market influence sums (a market may be in both). dict(mean, se, t, n1, n0,
    m1, m0)."""
    d = _clean(t, col)
    f = pd.to_numeric(d[flag], errors="coerce") if len(d) else pd.Series(dtype=float)
    a, b = d[f == 1], d[f == 0]
    n1, n0 = len(a), len(b)
    out = dict(mean=np.nan, se=np.nan, t=np.nan, n1=n1, n0=n0, m1=np.nan, m0=np.nan)
    if not n1 or not n0:
        if n1:
            out["m1"] = a[col].mean()
        if n0:
            out["m0"] = b[col].mean()
        return out
    m1, m0 = a[col].mean(), b[col].mean()
    psi = _psi(a, col, cluster, m1, n1).sub(_psi(b, col, cluster, m0, n0), fill_value=0.0)
    k = len(psi)
    se = float(np.sqrt((psi ** 2).sum() * k / max(k - 1, 1)))
    out.update(mean=m1 - m0, se=se, t=(m1 - m0) / se if se > 0 else np.nan, m1=m1, m0=m0)
    return out


def ratio_stat(t, num, den, cluster="market"):
    """sum(num) / sum(den) with the clustered delta-method SE. dict(ratio, se, n)."""
    if len(t):
        a = pd.to_numeric(t[num], errors="coerce").to_numpy(float)
        b = pd.to_numeric(t[den], errors="coerce").to_numpy(float)
        t = t[np.isfinite(a) & np.isfinite(b)]
    if not len(t) or t[den].sum() == 0:
        return dict(ratio=np.nan, se=np.nan, n=len(t))
    B = t[den].sum()
    R = t[num].sum() / B
    g = t.groupby(cluster)[[num, den]].sum()
    psi = (g[num] - R * g[den]) / B
    k = len(g)
    return dict(ratio=R, se=float(np.sqrt((psi ** 2).sum() * k / max(k - 1, 1))), n=len(t))


def day_of(t0):
    """UTC calendar day (YYYY-MM-DD) of epoch seconds."""
    return pd.to_datetime(np.asarray(t0, float), unit="s", utc=True).strftime("%Y-%m-%d").to_numpy()


def block_of(t0, blocks):
    """Label of the (label, first_day, end_day_exclusive) block holding each t0; '' if none."""
    day = day_of(t0)
    out = np.full(len(day), "", dtype=object)
    for name, lo, hi in blocks:
        out[(day >= lo) & (day < hi)] = name
    return out


def by_block(t, blocks, col="pnl", cluster="market"):
    """[(label, stat)] for each block."""
    b = block_of(t["t0"].to_numpy(float), blocks) if len(t) else np.array([], dtype=object)
    return [(name, stat(t[b == name], col, cluster)) for name, _, _ in blocks]


def with_day(t):
    """t plus a 'day' column (UTC day of t0), so stat / diff_stat can cluster by day."""
    return t.assign(day=day_of(t["t0"].to_numpy(float)) if len(t) else np.array([], dtype=object))


def day_cluster_table(t, flags=("h1", "h2"), col="pnl", bp=False):
    """Descriptive, not in JUMP2S.md: the main rows and the H subsets / differences with the SE
    clustered by market (the design) and by UTC day (pnl of different markets on one day moves
    together, so the market-clustered t overstates certainty). Markdown lines."""
    t = with_day(t)

    def val(m):
        return "–" if not np.isfinite(m) else (f"{m:+.2f} bp" if bp else _c(m))

    def se(s, k=None, sub=None):
        days = sub["day"].nunique() if sub is not None and len(sub) else 0
        if not np.isfinite(s.get("se", np.nan)) or (sub is not None and days < 2):  # one day: no day SE
            return "–"
        tt = f"{s['t']:+.1f}" if np.isfinite(s["t"]) else "–"
        x = f"{s['se']:.2f}" if bp else _c(s["se"], sign=False)[:-1]
        return f"±{x}（t {tt}" + (f"，{k} 天）" if k is not None else "）")

    L = ["| 项目 | 均值 | ± 按市场聚类（设计） | ± 按天聚类（描述） |", "|---|---:|---:|---:|"]
    t = _clean(t, col)
    a, b = stat(t, col), stat(t, col, cluster="day")
    L.append(f"| 全部 | {val(a['mean'])} | {se(a)} | {se(b, b['k'], t)} |")
    for fl in flags:
        if fl not in t:
            continue
        f = pd.to_numeric(t[fl], errors="coerce")
        a, b = stat(t[f == 1], col), stat(t[f == 1], col, cluster="day")
        L.append(f"| {fl.upper()} 子集 | {val(a['mean'])} | {se(a)} | {se(b, b['k'], t[f == 1])} |")
        a, b = diff_stat(t, fl, col), diff_stat(t, fl, col, cluster="day")
        L.append(f"| {fl.upper()} 子集 − 补集 | {val(a['mean'])} | {se(a)} | {se(b, None, t[f.notna()])} |")
    return L


def daily(t, col="pnl", min_n=DAY_MIN_N):
    """Per UTC day with >= min_n trades: n, sum and mean of `col`."""
    t = _clean(t, col)
    if not len(t):
        return pd.DataFrame(columns=["day", "n", "sum", "mean"])
    g = t.assign(day=day_of(t["t0"].to_numpy(float))).groupby("day")[col].agg(n="count", sum="sum")
    g = g[g["n"] >= min_n]
    return g.assign(mean=g["sum"] / g["n"]).reset_index()


def three_day(t, col="pnl", min_n=DAY_MIN_N, thr=HOT):
    """Windows of three consecutive calendar days, each with >= min_n trades, and how many have a
    pooled per-share mean >= thr. dict(windows, hits, rate, starts=[first day of each hit])."""
    d = daily(t, col, min_n).set_index("day")
    days = set(d.index)
    W, starts = 0, []
    for day in sorted(days):
        nxt = [(pd.Timestamp(day) + pd.Timedelta(days=k)).strftime("%Y-%m-%d") for k in (1, 2)]
        if not all(x in days for x in nxt):
            continue
        W += 1
        sel = d.loc[[day] + nxt]
        if sel["sum"].sum() / sel["n"].sum() >= thr - 1e-12:
            starts.append(day)
    return dict(windows=W, hits=len(starts), rate=len(starts) / W if W else np.nan, starts=starts)


def day_summary(t, col="pnl", min_n=DAY_MIN_N, thr=HOT):
    """dict(days, pos, pos_share, median, best, worst (day, mean), three=three_day(...))."""
    d = daily(t, col, min_n)
    out = dict(days=len(d), pos=int((d["mean"] > 0).sum()) if len(d) else 0, three=three_day(t, col, min_n, thr))
    out["pos_share"] = out["pos"] / out["days"] if out["days"] else np.nan
    out["median"] = float(d["mean"].median()) if len(d) else np.nan
    out["best"] = tuple(d.loc[d["mean"].idxmax(), ["day", "mean"]]) if len(d) else ("", np.nan)
    out["worst"] = tuple(d.loc[d["mean"].idxmin(), ["day", "mean"]]) if len(d) else ("", np.nan)
    return out


def decide_main(t, blocks, col="pnl", need=2):
    """Main rule (big jumps, book price): mean > 0 and t >= 2 and >= `need` blocks with mean > 0.
    dict(passed, mean, se, t, n, k, blocks=[(label, stat)], npos, need)."""
    s = stat(t, col)
    bl = by_block(t, blocks, col)
    npos = sum(1 for _, b in bl if b["n"] and b["mean"] > 0)
    ok = bool(s["n"] and s["mean"] > 0 and s["t"] >= 2 and npos >= need)
    return dict(passed=ok, **s, blocks=bl, npos=npos, need=need)


def decide_h(t, flag, blocks, col="pnl", need=2):
    """H1 / H2: subset mean > 0 with t >= 2, subset - complement > 0 with t >= 2, and >= `need`
    blocks with subset - complement > 0. dict(passed, sub, comp, diff, blocks=[(label, diff)], npos,
    need, unknown)."""
    f = pd.to_numeric(t[flag], errors="coerce") if len(t) else pd.Series(dtype=float)
    sub, comp, dif = stat(t[f == 1], col), stat(t[f == 0], col), diff_stat(t, flag, col)
    b = block_of(t["t0"].to_numpy(float), blocks) if len(t) else np.array([], dtype=object)
    bl = [(name, diff_stat(t[b == name], flag, col)) for name, _, _ in blocks]
    npos = sum(1 for _, d in bl if np.isfinite(d["mean"]) and d["mean"] > 0)
    ok = bool(sub["n"] and sub["mean"] > 0 and sub["t"] >= 2 and dif["mean"] > 0 and dif["t"] >= 2
              and npos >= need)
    return dict(passed=ok, sub=sub, comp=comp, diff=dif, blocks=bl, npos=npos, need=need,
                unknown=int(f.isna().sum()))


# --------------------------------------------------------------------------- Chinese report tables
def _c(x, nd=2, sign=True):
    return "–" if not np.isfinite(x) else (f"{100 * x:+.{nd}f}¢" if sign else f"{100 * x:.{nd}f}¢")


def fmt(s):
    """'+1.23¢ ±0.45（t +2.7，1,234）' from a stat dict; '–' when empty."""
    if not s.get("n"):
        return "–"
    tt = f"{s['t']:+.1f}" if np.isfinite(s["t"]) else "–"
    return f"{_c(s['mean'])} ±{_c(s['se'], sign=False)[:-1]}（t {tt}，{s['n']:,}）"


def fmt_diff(d):
    if not np.isfinite(d["mean"]):
        return "–"
    tt = f"{d['t']:+.1f}" if np.isfinite(d["t"]) else "–"
    return f"{_c(d['mean'])} ±{_c(d['se'], sign=False)[:-1]}（t {tt}）"


def _sel(rows, kind=None, price_type=None):
    m = np.ones(len(rows), bool)
    if kind is not None:
        m &= (rows["kind"] == kind).to_numpy()
    if price_type is not None and "price_type" in rows:
        m &= (rows["price_type"] == price_type).to_numpy()
    return rows[m]


def _types(rows, price_types):
    have = set(rows["price_type"]) if "price_type" in rows and len(rows) else set()
    return [p for p in price_types if p in have]


def main_table(rows, price_types=("book", "trade")):
    """| 买法 | 盘口价 | 成交价 |: per share ± clustered SE (t, n) for big, small, opposite, random."""
    pts = _types(rows, price_types)
    L = ["| 买法 | " + " | ".join(PRICE_NAMES[p] for p in pts) + " |", "|---|" + "---:|" * len(pts)]
    for k in KINDS:
        L.append(f"| {KIND_NAMES[k]} | " + " | ".join(fmt(stat(_sel(rows, k, p))) for p in pts) + " |")
    return L


def block_table(rows, blocks, price_type="book"):
    """| 月份段 | big | small | opposite | random | for one entry-price type."""
    L = ["| 月份段 | " + " | ".join(KIND_NAMES[k] for k in KINDS) + " |", "|---|" + "---:|" * len(KINDS)]
    r = _sel(rows, None, price_type)
    b = block_of(r["t0"].to_numpy(float), blocks) if len(r) else np.array([], dtype=object)
    for name, _, _ in blocks:
        L.append(f"| {name} | " + " | ".join(fmt(stat(_sel(r[b == name], k))) for k in KINDS) + " |")
    return L


def decomposition(t):
    """dict(n, dmid, dfair (mean side-oriented changes), ratio, ratio_se, cont (stat of cont_bp, bp),
    pnl (stat)) for big-jump rows carrying mid0, mid2, fair0, fair2, cont_bp."""
    t = t.assign(dmid=_num(t, "mid2") - _num(t, "mid0"), dfair=_num(t, "fair2") - _num(t, "fair0"))
    r = ratio_stat(t, "dmid", "dfair")
    both = _clean(_clean(t, "dmid"), "dfair")
    return dict(n=len(t), dmid=both["dmid"].mean() if len(both) else np.nan,
                dfair=both["dfair"].mean() if len(both) else np.nan, ratio=r["ratio"], ratio_se=r["se"],
                ratio_n=r["n"], cont=stat(t, "cont_bp"), pnl=stat(t))


def decomp_table(rows, blocks, price_type="book"):
    """Big jumps: | 段 | 笔数 | 中间价 2 秒变动 | 模型应变动 | 价格端跟上比例 | 结果端（bp） | 每份 |."""
    r = _sel(rows, "big", price_type)
    L = ["| 段 | 笔数 | 中间价 2 秒变动 | 模型应变动 | 价格端跟上比例 | 结果端：买后到结算顺跳变（bp） | 每份 |",
         "|---|---:|---:|---:|---:|---:|---:|"]
    b = block_of(r["t0"].to_numpy(float), blocks) if len(r) else np.array([], dtype=object)
    for name, sub in [(name, r[b == name]) for name, _, _ in blocks] + [("合计", r)]:
        d = decomposition(sub)
        ratio = "–" if not np.isfinite(d["ratio"]) else f"{100 * d['ratio']:.0f}% ±{100 * d['ratio_se']:.0f}"
        c = d["cont"]
        cont = "–" if not c["n"] else f"{c['mean']:+.2f} ±{c['se']:.2f}"
        L.append(f"| {name} | {d['n']:,} | {_c(d['dmid'])} | {_c(d['dfair'])} | {ratio} | {cont} | {fmt(d['pnl'])} |")
    return L


def h_table(rows, flag, blocks, price_type="book"):
    """Big jumps split by `flag` (h1 / h2): | 段 | 子集 | 补集 | 子集 − 补集 |, total then blocks."""
    r = _sel(rows, "big", price_type)
    f = pd.to_numeric(r[flag], errors="coerce") if len(r) else pd.Series(dtype=float)
    L = ["| 段 | 子集 | 补集 | 子集 − 补集 |", "|---|---:|---:|---:|"]
    b = block_of(r["t0"].to_numpy(float), blocks) if len(r) else np.array([], dtype=object)
    for name, m in [("合计", np.ones(len(r), bool))] + [(name, b == name) for name, _, _ in blocks]:
        s = r[m]
        fs = f[m]
        L.append(f"| {name} | {fmt(stat(s[fs == 1]))} | {fmt(stat(s[fs == 0]))} | {fmt_diff(diff_stat(s, flag))} |")
    unknown = int(f.isna().sum())
    if unknown:
        L += ["", f"特征缺失（不进子集也不进补集）：{unknown:,} 笔。"]
    return L


def daily_lines(rows, price_type="book", kind="big", min_n=DAY_MIN_N, thr=HOT, detail=False):
    """Daily summary of one kind / price type (and, with detail, one row per day)."""
    r = _sel(rows, kind, price_type)
    s = day_summary(r, min_n=min_n, thr=thr)
    th = s["three"]
    rate = "–" if not th["windows"] else f"{th['hits']} / {th['windows']}（{th['rate']:.0%}）"
    share = "–" if not s["days"] else f"{s['pos']} / {s['days']}（{s['pos_share']:.0%}）"
    best = "–" if not s["days"] else f"{s['best'][0]} {_c(s['best'][1])}"
    worst = "–" if not s["days"] else f"{s['worst'][0]} {_c(s['worst'][1])}"
    L = [f"| 天数（≥ {min_n} 笔） | 正收益天数 | 日均值中位数 | 最好一天 | 最差一天 | 连续 3 天合计 ≥ {_c(thr, 0)} 的窗口 |",
         "|---:|---:|---:|---|---|---:|",
         f"| {s['days']} | {share} | {_c(s['median'])} | {best} | {worst} | {rate} |"]
    if th["starts"]:
        L += ["", "≥ 阈值的 3 天窗口起点：" + "、".join(th["starts"])]
    if detail:
        d = daily(r, min_n=min_n)
        L += ["", "| 日期 | 笔数 | 每份 |", "|---|---:|---:|"]
        L += [f"| {x.day} | {x.n:,} | {_c(x.mean)} |" for x in d.itertuples()]
    return L


def _yes(ok):
    return "**成立**" if ok else "**不成立**"


def decision_lines(main=None, h1_=None, h2_=None, scope="5–8 月"):
    """Decision lines from decide_main / decide_h results (None to skip)."""
    L = []
    if main is not None:
        bl = "，".join(f"{n} {_c(b['mean']) if b['n'] else '–'}" for n, b in main["blocks"])
        L.append(f"- 主规则（≥ 1.2 bp、盘口价）在 {scope}：{_yes(main['passed'])}。每份 {fmt(main)}；"
                 f"为正的月份段 {main['npos']} / {len(main['blocks'])}（需 ≥ {main['need']}：{bl}）。")
    for name, h in (("H1 顺势", h1_), ("H2 趋势型", h2_)):
        if h is None:
            continue
        bl = "，".join(f"{n} {_c(d['mean'])}" for n, d in h["blocks"])
        L.append(f"- {name}：{_yes(h['passed'])}。子集 {fmt(h['sub'])}；补集 {fmt(h['comp'])}；"
                 f"子集 − 补集 {fmt_diff(h['diff'])}；差为正的月份段 {h['npos']} / {len(h['blocks'])}"
                 f"（需 ≥ {h['need']}：{bl}）。")
    return L
