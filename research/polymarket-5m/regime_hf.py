"""REGIME.md, the producer: for every BTC 5m window with a final official outcome on the Hugging Face
books, the realised P&L of each of the six base strategies in that window, and the environment known
at the window's open. One compact row per window; regime_learn.py learns and judges the switch from
the table. Paper research on market data only (no orders, no keys).

Data: whodisidk/polymarket-btc-updown-exchange-data (BTC 5m, 2026-05-25 .. 08-29: 100 ms books of
both tokens, every Polymarket print with its taker side, every Binance BTCUSDT print with trade and
recorder times). Runs on GitHub as

    python cross.py regime [N] --workdir DIR --out real/regime-windows.csv.gz

(ci/cross.request, the polymarket-cross lane), one daily archive at a time (downloaded, read, deleted;
per-day rows in <workdir>/regime-shards/<day>.parquet, concatenated at the end). The inputs that do
not come from the archives are small files in real/regime-inputs/ (built locally by build_inputs):
factor_preds_5m.csv.gz (the factor model's walk-forward out-of-sample P(up) per 5m window, copied
from real/mix-inputs/, see mix_hf.build_inputs), dvol_1h.csv.gz (Deribit DVOL hourly candles, same
copy) and binance_1m.csv.gz (per Binance minute, May 20 .. Aug 31, from the factor study's 1-minute
panel and Binance 1 s klines: spot volume, perpetual-spot basis, funding rate, and the sum of
squared 1 s log returns with their count).

Conventions shared with the other Hugging Face lanes (cross.py, jump2s_hf.py, mix_hf.py):
- Markets: BTC 5m (horizon 5) from cross.market_table on the archive's market rows (one row per
  market, jump2s_hf.shrink_markets) and the final resolution rows (jump2s_hf.final_resolution:
  provisional rows written before the Chainlink price are dropped). The market rows' own up_won
  column is dropped before market_table, so the outcome comes from a final resolution row only
  (it is empty in the archives checked; this makes "final outcomes only" hold whatever it holds).
- Times are the recorder's receipt clock: a Binance print is known at its receipt time, a book
  snapshot at its timestamp, a Polymarket print at its receipt time.
- Binance "jump" (急动): a print whose log price moved >= 2 bp from the last print at least 1 s and
  at most 5 s earlier by trade time (MIX.md); the decision time is its receipt time. sigma is the
  per-second vol of the 600 s before (jump2s.Seconds, gaps up to 20 s forward filled, as jump2s_hf).
- Book quote of a token at time t: the last snapshot at or before t, at most 1 s old; usable when the
  market is live (lifecycle "active"; see below for "pre_open"), not halted, and the token's book is
  not crossed (bid < ask). Fresh: the market's top of book (STATE_COLS) changed at most 1 s before
  t (jump2s_hf). Every price paid or received as a taker needs a fresh, usable quote.
- Taker entry decided at t: filled at te = t + 0.5 s at the side's best ask then, which must be in
  0.02 .. 0.98 with >= 5 shares, te before the end. Fee 0.07 p (1 - p) (binary.taker_fee); makers pay
  nothing. Taker exit decided at x: filled at x + 0.5 s at the best bid (fresh, > 0, >= 5 shares, before
  the end, taker fee); otherwise held to the official result.
- Resting sell (MIX.md): at L = min(entry + 2c, 0.99), live 0.5 s after the entry fill. If the book
  then already shows a fresh bid >= L with >= 5 shares, the order crosses and is sold there as a
  taker (fee). Otherwise it is filled at L (no fee) at the first moment in (live, entry + h] when
  a Polymarket print on that token with taker side BUY trades STRICTLY above L, or a usable snapshot
  shows the token's best bid STRICTLY above L, the print or the level showing >= 5 shares
  (conservative on queue priority: only a price through our level proves everyone at L was filled;
  a touch at L is never a fill). Not filled by entry + h: taker exit decided at entry + h. A bid >= L
  at the moment the order goes live that cannot be priced (stale book, < 5 shares) gives no resting
  fill at all (the taker exit at the timer is used).

The six base strategies (REGIME.md; every window, one realised P&L each, with units):
1. H ("H"): candidates = jump prints received with 240 >= seconds left >= 15 (the H rule's window,
   latency.TAUS[0], G_TAU_LO). At the receipt time t the H fair value of the jump's side
   (fill_rate.sends, anchor 2 s): Up fair = Phi(Phi^-1(Up mid 2 s earlier, clipped 0.005..0.995) +
   (x(t) - x(t - 2)) / (sigma f)), x = log price of the last print received by then (<= 5 s old),
   f = sqrt(seconds left) for point-price markets, binary.twap_std_factor for the TWAP ones (window by
   pm_outcomes.rule_window via jump2s.settle_twap); Down fair = 1 - Up fair. Sent when fair - ask(t) -
   fee >= 6c on the ask seen at t; a fill-and-kill limit at that edge: filled at t + 0.5 s only if the
   ask then still leaves fair - ask - fee >= 6c (the fair frozen at t). Per market the next candidate
   counts only >= 2 s after the last send (fill_rate.sends' `every`). Held to settlement.
2. Follow ("follow"): MIX.md jump points (285 >= seconds left >= 20, per market >= 5 s after the last
   kept point); buy the jump's side at t + 0.5 s; resting sell at +2c, h = 30 s, then taker exit.
3. Reversal ("revert"): at every top-of-book change t with 285 >= seconds left >= 20 where the Up mid
   moved >= 8c against the Up mid 10 s earlier (both usable quotes) while Binance moved less than
   1 bp over the same 10 s (x(t) - x(t - 10), both <= 5 s old; "几乎没动", half the jump threshold),
   buy the other side (Up mid up -> buy Down) at t + 0.5 s; per market >= 10 s after the last kept
   trigger (each trigger its own 10 s move); resting sell at +2c, h = 60 s, then taker exit.
4. Direction ("direction"): the factor model's P(up) for the window (ridge_5m, the model FACTORS.md
   froze for 5 m, with its 4c threshold) known at the open; |P - 0.5| >= 4c: buy that side at the open
   + 0.5 s; taker exit decided at 30 s left (filled at 29.5 s left), else settlement. NaN P: the
   window is not evaluable for this strategy.
5. Late strong side ("late"): at 60 s left the driftless model (jump2s.model_fair: Binance proxy of
   the price to beat, the point or TWAP rule, sigma of the 600 s before) on x = the last Binance print
   received by then; the side whose probability P is in [0.95, 0.99) is bought when its ask at that
   moment is <= P - 1c, by a limit at P - 1c filled at + 0.5 s only if the ask then is still <= P - 1c.
   Held to settlement.
6. Maker ("maker"): from 285 to 20 s before the end, resting BUY orders of 5 shares on both tokens,
   each at its own token's mid - 2c (the Down bid at the Down mid - 2c is selling Up at about the Up
   mid + 2c: "两边在中间价 ± 2¢"), floored to the cent, in 0.02 .. 0.98 (mid: see below). A side's order
   is placed or replaced when its price changes at a fresh snapshot with a usable mid (cancel
   effective 0.2 s after the decision, the new order live 0.5 s after it; an order a cancel overtakes
   is never live). Every Binance jump received at c cancels both (effective c + 0.2 s), and no order
   is placed before 1 s after the last such jump. Fill: a print on that token with taker side SELL
   STRICTLY below our price, or a usable snapshot with the token's best ask STRICTLY below it,
   showing >= 5 shares (as the resting sells above), at our price without fee. An order the book
   already crosses when it goes live (ask <= our price) is a taker buy at that ask (fee) when the ask
   is fresh, in 0.02 .. 0.98 with >= 5 shares, and no fill at all otherwise. At most one fill per token
   per window (that side then stops quoting). Held to settlement. Quotes come from the book only, so
   a fill on one side does not move the other side's quote. A trade's t is its order's decision time.
7. None: P&L 0 (pnl_none).

Per strategy s the columns (all $ after every fee; at 5 shares a trade and at the "20-share" size =
min(20, shares at the entry ask, at a taker exit bid, at the maker fill's print / level)):
trades_s, shares_s (= 5 x trades), sh20_s, cost_s / cost20_s (shares x (price + entry fee)), pnl_s /
pnl20_s, pps_s (pnl_s / shares_s, $ a share; NaN without a trade), caph_s / caph20_s (capital-hours:
cost x hours from the entry fill to the exit fill, or to the market's end when held).
0 = evaluated and nothing traded; NaN = not evaluable (see below). The per-share P&L of a trade is the
same at both sizes (the 20-share size follows the 5-share path).

Environment at the open (REGIME.md; only information available by the open; per-strategy trailing
P&L over the previous 12 / 48 windows is built in regime_learn.py from the table, not here):
- rv5m, rv30m, rv1h: annualised realised vol, sqrt(mean of squared Binance 1 s log returns x
  365 x 86400) over the 5 / 30 / 60 minutes before the open (complete 1 s klines, not the recorder's
  feed; >= half of the seconds present). The mean is not removed (realised vol); over these windows
  the difference to a sample std is below 0.1%.
- vr60, ret4h: jump2s.trend_features on Binance 1 m klines (data.binance.vision, cached in the
  workdir; jump2s_hf.klines / closes_for) at the open: minutes closed by the open.
- dvol (%), dvol_rv = DVOL / 100 - rv30m (as factors.dvol_minus_rv30): the last hourly DVOL candle
  closed by the open.
- vol_rel = log(mean Binance spot volume a minute over the last 60 minutes / its mean over the last
  24 h) (as factors' spot_lvr_1h; >= 30 and >= 720 minutes present).
- funding (bp): the last funding settlement with calc_time <= the open; basis (bp): mean of
  (perpetual close / spot close - 1) over the 60 minutes before the open (>= 30 present).
- hour (UTC), weekend (Saturday / Sunday UTC).
- pm_spread, ask_size, pm_up: the market's own book at its last snapshot strictly before the open
  (<= 1 s old, any lifecycle state, not halted, both tokens uncrossed): mean of the two tokens'
  spreads, mean of the two best-ask sizes (shares), Up mid. book_ups: top-of-book changes per second
  of the previous 5m market (the one ending at this open) over its window; book_ups_pre: of this
  market's own book over the 60 s before its open. fac_up: the factor P(up) used by "direction".
- Also kept (not features): day (archive), market, start (s), won (Up won, final), w (settlement
  TWAP window, s), book_cov, bin_cov (below), n_chg (live top-of-book changes in the window).

Where REGIME.md and the task are silent, the conservative choice made here:
- Evaluable: book_cov = share of the window's 300 seconds with a live top-of-book change, bin_cov =
  share with a Binance print received. A strategy is NaN (not evaluable, no usable data) when
  book_cov < 0.5, and for H, follow, revert, late and maker also when bin_cov < 0.25 (the recorder's
  Binance feed skips seconds every day: 73-74% of the seconds on whole days, 50-97% per window on
  06-02; a gap only removes trades), and for direction when its P(up) is missing. A market with no
  snapshot in its window in the archive (plus the carry) is skipped there.
- Mid (revert's signal, maker's quotes): a token's own (bid + ask) / 2, only where that token's
  spread is <= 5c. On 06-02, 22% of in-window Up snapshots had a wider spread (one level pulled for
  a snapshot or two, e.g. bid 0.44 -> 0.10 -> 0.44 within 0.3 s), and 77% of the 10 s "mid moves >= 8c"
  had such a hole at one end: a mid across a hole is not a price. Each token's own mid, because a
  Down quote derived from the Up mid crossed the Down book whenever an Up level flickered. H keeps
  its own rule's plain Up mid (fill_rate.sends), a convention of that rule.
- "pre_open": the recorder labels a 5m market "active" only 0.6-21.5 s after its start (median 11 s
  on 06-02), while its book trades before the start. A "pre_open" snapshot counts as live from the
  start to 30 s after it; before the start it is used for the features only.
- Days: a market whose window ends just before midnight is resolved in the next archive. The last
  15 minutes of the previous archive's 5m snapshots and Polymarket prints and its last 30 minutes of
  Binance prints are carried into the next one, so such a market is priced from its own window.
  A market priced in two archives keeps the row with more book changes (the first on ties).
- H's jump is REGIME.md's / MIX.md's >= 2 bp jump (the one quantified jump of REGIME.md), not the
  latency study's 2-sigma trigger. H's 6c is checked on both the ask seen at the decision and the
  ask at the fill (a fill-and-kill limit). "At most one trade per 2 s" counts from the last send.
- Late: "ask <= model - 1c" on the ask seen at the decision and at the fill, as H. The model is the
  repo's 5m model (600 s sigma), not NEARCERT.md's 1 h sigma (those are other markets).
- Follow, revert: each kept point is a trade (positions may overlap), as MIX.md's decision points.
- A resting order is treated as cancelled at its timer: a fill after it is ignored (the taker exit
  is used, which costs more), so the overlap of the cancel latency never helps.
- Maker 20-share size: min(20, the print's / level's size); its "position" is one fill per token.
- Factor P(up): FACTORS.md chose ridge and the 4c threshold on its B (2026-07-01 .. 08-15), which
  overlaps REGIME.md's A and B; the predictions themselves are walk-forward out of sample. Said here
  because the direction strategy's threshold is therefore not fresh on REGIME.md's B.
- Only the base strategies' P&L and the environment are produced here; nothing is selected.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

import numpy as np
import pandas as pd

import binary as bo
import jump2s as j2
import jump2s_hf as hf

HERE = Path(__file__).resolve().parent
INPUTS = HERE / "real" / "regime-inputs"
OUT = HERE / "real" / "regime-windows.csv.gz"
MIX_INPUTS = HERE / "real" / "mix-inputs"

# --------------------------------------------------------------------------- REGIME.md constants
LAT_S = 0.5                 # decision -> fill (entries, taker exits, a new resting order live)
CANCEL_S = 0.2              # maker: cancel effective after
MIN_SHARES = 5.0
UNIT, UNIT_MAX = 5.0, 20.0
BOOK_MAX_AGE_S = 1.0
FRESH_S = 1.0
PRICE_MAX_AGE_S = 5.0
PRE_OPEN_GRACE_S = 30.0
JUMP_BP = 2.0
REF_MIN_S, REF_MAX_S = 1.0, 5.0
H_TAU = (240.0, 15.0)
H_EDGE = 0.06
H_EVERY_S = 2.0
H_ANCHOR_S = 2.0
TAU = (285.0, 20.0)         # follow, revert, maker
FOLLOW_SPACING_S = 5.0
FOLLOW_HOLD_S = 30.0
REV_DMID = 0.08
REV_WIN_S = 10.0
REV_QUIET_BP = 1.0
REV_SPACING_S = 10.0
REV_HOLD_S = 60.0
MAKE_X = 0.02               # resting sell above the entry
MAKER_CAP = 0.99
DIR_THETA = 0.04
DIR_EXIT_TAU = 30.0
DIR_MODEL = "ridge_5m"
LATE_TAU = 60.0
LATE_BAND = (0.95, 0.99)
LATE_MARGIN = 0.01
MM_OFFSET = 0.02
MID_MAX_SPREAD = 0.05       # a mid across a wider hole in the book is not a price (revert signal, maker quotes)
MM_PAUSE_S = 1.0
COV_BOOK = 0.5              # share of the window's seconds with a live top-of-book change
COV_BIN = 0.25              # ... with a Binance print received (the recorder's feed: ~73% on whole days)
CARRY_BIN_S = 1800
CARRY_BOOK_S = 900
BUDGET_S = 285 * 60
SEC_YEAR = 365 * 86400
DAY = 86400
T_EPS = 1e-6

STRATS = ("H", "follow", "revert", "direction", "late", "maker")
NEEDS_BINANCE = ("H", "follow", "revert", "late", "maker")
QTY = ("trades", "shares", "sh20", "cost", "cost20", "pnl", "pnl20", "pps", "caph", "caph20")
ENV = ["rv5m", "rv30m", "rv1h", "vr60", "dvol", "dvol_rv", "ret4h", "vol_rel", "funding", "basis", "hour", "weekend",
       "pm_spread", "ask_size", "pm_up", "book_ups", "book_ups_pre", "fac_up"]
META = ["day", "market", "start", "won", "w", "book_cov", "bin_cov", "n_chg"]
STRAT_COLS = [f"{q}_{s}" for s in STRATS for q in QTY]
ROW_COLS = META + ENV + STRAT_COLS + ["pnl_none"]
TRADE_COLS = ["market", "strat", "t", "te", "side", "price", "fee_in", "how", "t_exit", "exit_px", "fee_out", "sh20",
              "pps", "hold"]
HOW = {0: "settle", 1: "taker exit", 2: "maker exit", 3: "crossed on arrival"}
ROUND = {"rv5m": 5, "rv30m": 5, "rv1h": 5, "vr60": 4, "dvol": 3, "dvol_rv": 5, "ret4h": 6, "vol_rel": 4,
         "funding": 4, "basis": 3, "pm_spread": 4, "ask_size": 2, "pm_up": 4, "book_ups": 3, "book_ups_pre": 3,
         "fac_up": 5, "book_cov": 3, "bin_cov": 3}
MAX_BYTES = 24_500_000


def fee(p):
    """Taker fee per share, 0.07 p (1 - p) (binary.taker_fee)."""
    return bo.taker_fee(np.asarray(p, float))


def ts(day):
    return int(pd.Timestamp(day, tz="UTC").timestamp())


# --------------------------------------------------------------------------- inputs
def build_inputs(cache=None, out_dir=INPUTS, start="2026-05-20", end="2026-09-01", mix_inputs=MIX_INPUTS):
    """real/regime-inputs/ from the factor study's cache (local only; the CI lane reads the files):
    factor_preds_5m.csv.gz and dvol_1h.csv.gz copied from real/mix-inputs/ (mix_hf.build_inputs: the
    factor study's walk-forward out-of-sample predictions, continued walk-forward from 08-16; hourly
    DVOL), and binance_1m.csv.gz: per minute m (open time, s; known at m + 60) in [start, end):
    vol = spot base volume, basis_bp = (perpetual close / spot close - 1) x 1e4, funding_bp = the last
    funding rate with calc_time <= m + 60, x 1e4 (factors_data panel row m + 60), rv2 = sum of squared
    1 s log returns of the seconds m .. m + 59 x 1e8 (bp^2), nr = how many."""
    if cache is None:
        import factors as F
        cache = F.CACHE
    cache, out_dir = Path(cache), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in ("factor_preds_5m.csv.gz", "dvol_1h.csv.gz"):
        src = Path(mix_inputs) / name
        if not src.exists():
            raise FileNotFoundError(f"{src}: run mix_hf.build_inputs first")
        shutil.copyfile(src, out_dir / name)
    a, b = ts(start), ts(end)
    p = pd.read_parquet(cache / "panel_1m.parquet", columns=["spot_vol", "spot_close", "fut_close", "funding_rate"])
    T = (((p.index - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(seconds=1)).to_numpy(np.int64)
         if isinstance(p.index, pd.DatetimeIndex) else p.index.to_numpy(np.int64))  # row T: known at T
    m = T - 60
    keep = (m >= a) & (m < b)
    p, m = p[keep], m[keep]
    with np.errstate(invalid="ignore", divide="ignore"):
        basis = (p["fut_close"].to_numpy(float) / p["spot_close"].to_numpy(float) - 1.0) * 1e4
    s = pd.read_parquet(cache / "spot_1s_close.parquet")
    sec = s.index.to_numpy(np.int64)
    lo = np.searchsorted(sec, a - 1)
    hi = np.searchsorted(sec, b)
    sec, lp = sec[lo:hi], np.log(s["close"].to_numpy(float)[lo:hi])
    del s
    r = np.full(len(lp), np.nan)
    one = np.r_[False, np.diff(sec) == 1]
    r[one] = np.diff(lp)[one[1:]]
    mm = (sec // 60) * 60
    g = pd.DataFrame({"m": mm, "r2": r * r * 1e8, "ok": np.isfinite(r)}).groupby("m")
    rv2 = g["r2"].sum(min_count=1).reindex(m)
    nr = g["ok"].sum().reindex(m).fillna(0).astype(np.int64)
    out = pd.DataFrame({"minute": m, "vol": np.round(p["spot_vol"].to_numpy(float), 4), "basis_bp": np.round(basis, 3),
                        "funding_bp": np.round(p["funding_rate"].to_numpy(float) * 1e4, 4),
                        "rv2": np.round(rv2.to_numpy(float), 3), "nr": nr.to_numpy()})
    out.to_csv(out_dir / "binance_1m.csv.gz", index=False)
    return out


class Minutes:
    """binance_1m.csv.gz on a complete minute grid with prefix sums (window sums before a time)."""

    COLS = ("vol", "basis_bp", "rv2", "nr")

    def __init__(self, df):
        self.ok = df is not None and len(df) > 0
        if not self.ok:
            return
        d = df.sort_values("minute").drop_duplicates("minute", keep="last")
        m = d["minute"].to_numpy(np.int64)
        self.m0 = int(m[0])
        grid = np.arange(self.m0, int(m[-1]) + 60, 60, dtype=np.int64)
        d = d.set_index("minute").reindex(grid)
        self.n = len(grid)
        self.fund = d["funding_bp"].to_numpy(float) if "funding_bp" in d else np.full(self.n, np.nan)
        self.cs, self.cn = {}, {}
        for c in self.COLS:
            v = d[c].to_numpy(float) if c in d else np.full(self.n, np.nan)
            f = np.isfinite(v)
            self.cs[c] = np.r_[0.0, np.cumsum(np.where(f, v, 0.0))]
            self.cn[c] = np.r_[0, np.cumsum(f)]

    def window(self, col, t, k):
        """(sum, count of present minutes) of col over the k minutes closed by t: opens [t - 60 k, t - 60]."""
        t = np.asarray(t, float)
        if not self.ok:
            return np.full(t.shape, np.nan), np.zeros(t.shape, np.int64)
        hi = np.floor((t - self.m0) / 60.0 + T_EPS).astype(np.int64)  # minutes with open + 60 <= t
        lo = hi - k
        ok = (lo >= 0) & (hi <= self.n)
        lo_, hi_ = np.clip(lo, 0, self.n), np.clip(hi, 0, self.n)
        s = np.where(ok, self.cs[col][hi_] - self.cs[col][lo_], np.nan)
        c = np.where(ok, self.cn[col][hi_] - self.cn[col][lo_], 0)
        return s, c

    def funding(self, t):
        """Funding (bp) of the last minute closed by t (known at its close: calc_time <= that close)."""
        t = np.asarray(t, float)
        if not self.ok:
            return np.full(t.shape, np.nan)
        i = np.floor((t - self.m0) / 60.0 + T_EPS).astype(np.int64) - 1
        ok = (i >= 0) & (i < self.n)
        return np.where(ok, self.fund[np.clip(i, 0, self.n - 1)], np.nan)


def load_inputs(inputs=INPUTS):
    """dict(factor = P(up) per window start (s), dvol = hourly candles, minutes = Minutes); empty when absent."""
    inputs = Path(inputs)
    out = dict(factor=pd.DataFrame(), dvol=pd.DataFrame(columns=["hour", "dvol"]), minutes=Minutes(None))
    if (inputs / "factor_preds_5m.csv.gz").exists():
        fp = pd.read_csv(inputs / "factor_preds_5m.csv.gz", index_col=0)
        fp.index = fp.index.astype(np.int64)
        out["factor"] = fp
    if (inputs / "dvol_1h.csv.gz").exists():
        out["dvol"] = pd.read_csv(inputs / "dvol_1h.csv.gz").sort_values("hour")
    if (inputs / "binance_1m.csv.gz").exists():
        out["minutes"] = Minutes(pd.read_csv(inputs / "binance_1m.csv.gz"))
    return out


def dvol_at(dv, t):
    """DVOL (%) of the last hourly candle closed by t (known at its hour's end), NaN when none."""
    t = np.asarray(t, float)
    if dv is None or not len(dv):
        return np.full(t.shape, np.nan)
    known = dv["hour"].to_numpy(float) + 3600.0
    k = np.searchsorted(known, t + T_EPS, "right") - 1
    v = dv["dvol"].to_numpy(float)
    return np.where(k >= 0, v[np.maximum(k, 0)], np.nan)


# --------------------------------------------------------------------------- Binance
class Spot:
    """The Binance prints of a day (with the carried tail): trade-time order (ts, lp, rv, d = move against
    the 1-5 s reference), receipt order, the per-second grid, and the jump prints in receipt order."""

    def __init__(self, binance):
        b = binance[["trade_ts_ms", "recv_ts_ms", "price"]].apply(pd.to_numeric, errors="coerce")
        v = b.to_numpy(float)
        b = b[np.isfinite(v).all(axis=1) & (v[:, 2] > 0)].drop_duplicates()
        b = b.sort_values(["trade_ts_ms", "recv_ts_ms"], kind="stable")
        self.ts = b["trade_ts_ms"].to_numpy(float) / 1000.0
        self.rv = b["recv_ts_ms"].to_numpy(float) / 1000.0
        px = b["price"].to_numpy(float)
        self.lp = np.log(px)
        n = len(self.ts)
        if n:
            j = np.searchsorted(self.ts, self.ts - REF_MIN_S + T_EPS, "right") - 1
            jj = np.maximum(j, 0)
            ok = (j >= 0) & (self.ts - self.ts[jj] <= REF_MAX_S + T_EPS)
            self.d = np.where(ok, self.lp - self.lp[jj], np.nan)
        else:
            self.d = np.zeros(0)
        self.order = np.argsort(self.rv, kind="stable")
        self.rv_o = self.rv[self.order]
        self.sec = j2.Seconds(self.ts, px, ffill=hf.GAP_FILL_S)
        with np.errstate(invalid="ignore"):
            cand = np.flatnonzero(np.abs(self.d) * 1e4 >= JUMP_BP - 1e-9)
        self.jc = cand[np.argsort(self.rv[cand], kind="stable")]
        self.jt = self.rv[self.jc]

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
        i = np.asarray(i)
        return np.where(i >= 0, arr[np.maximum(i, 0)], np.nan) if len(arr) else np.full(i.shape, np.nan)

    def x(self, t):
        """Log price of the last print received by t (<= 5 s old)."""
        return self.at(self.lp, self.last(t, PRICE_MAX_AGE_S))

    def jumps(self, lo, hi):
        """(receipt times, trade-sorted indices) of the jump prints received in [lo, hi]."""
        a = np.searchsorted(self.jt, lo - T_EPS, "left")
        b = np.searchsorted(self.jt, hi + T_EPS, "right")
        return self.jt[a:b], self.jc[a:b]

    def coverage(self, start, end):
        """Share of the seconds [start, end) with a print received in them."""
        a = np.searchsorted(self.rv_o, start - T_EPS, "left")
        b = np.searchsorted(self.rv_o, end - T_EPS, "left")
        if b <= a:
            return 0.0
        return len(np.unique(np.floor(self.rv_o[a:b] - start))) / max(end - start, 1.0)


# --------------------------------------------------------------------------- books
class Books:
    """The day's 5m snapshots grouped by market (sorted, one row per timestamp): quotes, live flag,
    top-of-book changes and the time of each market's last change."""

    COLS = ("up_best_bid", "up_best_ask", "down_best_bid", "down_best_ask", "up_bid_size", "up_ask_size",
            "down_bid_size", "down_ask_size")

    def __init__(self, feat, starts):
        """feat: snapshots (cross.read_day columns); starts: {market: window start, s}."""
        self.seg = {}
        f = feat[feat["market_id"].astype(str).isin(set(starts))] if len(feat) else feat
        if not len(f):
            return
        f = f.assign(market_id=f["market_id"].astype(str)).sort_values(["market_id", "timestamp_ms"], kind="stable")
        f = f.drop_duplicates(["market_id", "timestamp_ms"], keep="last")
        mk = f["market_id"].to_numpy()
        self.T = pd.to_numeric(f["timestamp_ms"]).to_numpy(float) / 1000.0
        self.c = {c: (pd.to_numeric(f[c], errors="coerce").to_numpy(float) if c in f else np.full(len(f), np.nan))
                  for c in self.COLS}
        state = f["lifecycle_state"].astype(str).str.lower().to_numpy() if "lifecycle_state" in f else \
            np.full(len(f), "active")
        self.halt = f["observed_halt_flag"].eq(True).to_numpy(bool) if "observed_halt_flag" in f else np.zeros(len(f), bool)
        st = pd.Series(mk).map(starts).to_numpy(float)
        with np.errstate(invalid="ignore"):
            pre = (state == "pre_open") & (self.T >= st - T_EPS) & (self.T < st + PRE_OPEN_GRACE_S - T_EPS)
        self.live = ((state == "active") | pre) & ~self.halt
        stv = f[[c for c in hf.STATE_COLS if c in f]].apply(pd.to_numeric, errors="coerce").to_numpy(float)
        same = (stv[1:] == stv[:-1]) | (np.isnan(stv[1:]) & np.isnan(stv[:-1]))
        first = np.r_[True, mk[1:] != mk[:-1]]
        self.moved = np.r_[True, ~same.all(axis=1)] | first
        self.change = self.moved & ~first          # a recorded change (the first row of a market is not one)
        self.last_chg = pd.Series(np.where(self.moved, self.T, np.nan)).ffill().to_numpy(float)
        self.seg = hf._segments(mk)

    def asof(self, m, t, max_age=BOOK_MAX_AGE_S):
        """Global index of market m's last snapshot at or before t (<= max_age old), -1 when none."""
        a, b = self.seg[m]
        k = j2.asof_idx(self.T[a:b], t, max_age)
        return np.where(k >= 0, k + a, -1)

    def quote(self, g, s, live=True):
        """Side s quote at global rows g (-1: none): (bid, ask, bid size, ask size, usable) where usable =
        live (or, with live=False, just not halted) and not crossed on that token."""
        g = np.asarray(g)
        has = g >= 0
        gg = np.maximum(g, 0)
        if not hasattr(self, "T"):
            nan = np.full(g.shape, np.nan)
            return nan, nan, nan, nan, np.zeros(g.shape, bool)
        ok = has & (self.live[gg] if live else ~self.halt[gg])
        up = np.broadcast_to(np.asarray(s) > 0, g.shape)
        bid = np.where(up, self.c["up_best_bid"][gg], self.c["down_best_bid"][gg])
        ask = np.where(up, self.c["up_best_ask"][gg], self.c["down_best_ask"][gg])
        bsz = np.where(up, self.c["up_bid_size"][gg], self.c["down_bid_size"][gg])
        asz = np.where(up, self.c["up_ask_size"][gg], self.c["down_ask_size"][gg])
        with np.errstate(invalid="ignore"):
            crossed = np.isfinite(bid) & np.isfinite(ask) & (bid >= ask)
        ok = ok & ~crossed
        return (np.where(ok, bid, np.nan), np.where(ok, ask, np.nan), np.where(ok, bsz, np.nan),
                np.where(ok, asz, np.nan), ok)

    def fresh(self, g, t):
        g = np.asarray(g)
        gg = np.maximum(g, 0)
        with np.errstate(invalid="ignore"):
            return (g >= 0) & (np.asarray(t, float) - self.last_chg[gg] <= FRESH_S + T_EPS)

    def up_mid(self, g, live=True):
        bid, ask, _, _, ok = self.quote(g, np.ones(np.shape(g)), live)
        return np.where(ok, (bid + ask) / 2, np.nan)

    def mid(self, g, s, max_spread=MID_MAX_SPREAD):
        """Side s's own mid at global rows g where its book is usable and its spread <= max_spread, else NaN."""
        bid, ask, _, _, ok = self.quote(g, np.broadcast_to(s, np.shape(g)))
        with np.errstate(invalid="ignore"):
            return np.where(ok & (ask - bid <= max_spread + 1e-9), (bid + ask) / 2, np.nan)


# --------------------------------------------------------------------------- execution helpers
def _first_hit(ev_t, ev_v, ev_s, t_from, t_to, lim):
    """Per row: (time, size) of the first event in (t_from, t_to] whose value is STRICTLY above lim and
    whose size is >= MIN_SHARES; (inf, NaN) when none."""
    t_from, t_to, lim = (np.atleast_1d(np.asarray(x, float)) for x in (t_from, t_to, lim))
    out_t, out_s = np.full(len(t_from), np.inf), np.full(len(t_from), np.nan)
    if not len(ev_t):
        return out_t, out_s
    lo = np.searchsorted(ev_t, t_from + T_EPS, "left")
    hi = np.searchsorted(ev_t, t_to + T_EPS, "right")
    for r in range(len(t_from)):
        if hi[r] > lo[r]:
            v, s = ev_v[lo[r]:hi[r]], ev_s[lo[r]:hi[r]]
            hit = (v > lim[r] + 1e-9) & (s >= MIN_SHARES - 1e-12)
            if hit.any():
                k = int(np.argmax(hit))
                out_t[r], out_s[r] = ev_t[lo[r] + k], s[k]
    return out_t, out_s


class Market:
    """One market's context: window, outcome, books, Binance, Polymarket prints (lazy hit events)."""

    def __init__(self, m, start, end, won_up, bk, spot, pr_by):
        self.m, self.start, self.end, self.won_up = m, float(start), float(end), float(won_up)
        self.bk, self.spot, self.pr_by = bk, spot, pr_by
        self.w = int(j2.settle_twap([self.start])[0])
        self._ev = {}

    def won(self, s):
        return np.where(np.asarray(s) > 0, self.won_up, 1.0 - self.won_up)

    def events(self, s, kind):
        """Hit events of a resting order on side s's token, time-sorted: kind 'sell' -> (bids of usable
        snapshots, taker-BUY prints) as is; kind 'buy' -> (asks, taker-SELL prints) negated, so that
        'strictly through' is always value > limit."""
        key = (s, kind)
        if key not in self._ev:
            a, b = self.bk.seg[self.m]
            g = np.arange(a, b)
            bid, ask, bsz, asz, ok = self.bk.quote(g, np.full(len(g), s))
            if kind == "sell":
                v, z = np.where(ok & np.isfinite(bid), bid, -np.inf), bsz
            else:
                v, z = np.where(ok & np.isfinite(ask), -ask, -np.inf), asz
            t = self.bk.T[a:b]
            p = self.pr_by.get((self.m, s, "buy" if kind == "sell" else "sell"))
            if p is not None and len(p[0]):
                t = np.concatenate([t, p[0]])
                v = np.concatenate([v, p[1] if kind == "sell" else -p[1]])
                z = np.concatenate([z, p[2]])
                o = np.argsort(t, kind="stable")
                t, v, z = t[o], v[o], z[o]
            self._ev[key] = (t, v, np.where(np.isfinite(z), z, 0.0))
        return self._ev[key]

    def entry(self, t, s, limit=None):
        """Taker buy decided at t: (te, ask, ask size, filled)."""
        te = np.asarray(t, float) + LAT_S
        g = self.bk.asof(self.m, te)
        _, ask, _, asz, ok = self.bk.quote(g, s)
        with np.errstate(invalid="ignore"):
            fill = ok & self.bk.fresh(g, te) & j2.price_ok(ask, asz) & (te < self.end - T_EPS)
            if limit is not None:
                fill &= ask <= np.asarray(limit, float) + 1e-9
        return te, ask, asz, fill

    def taker_exit(self, x, s):
        """Taker sell decided at x: (tx, bid, bid size, sold)."""
        tx = np.asarray(x, float) + LAT_S
        g = self.bk.asof(self.m, tx)
        bid, _, bsz, _, ok = self.bk.quote(g, s)
        with np.errstate(invalid="ignore"):
            sold = ok & self.bk.fresh(g, tx) & (tx < self.end - T_EPS) & np.isfinite(bid) & (bid > 0) & \
                np.isfinite(bsz) & (bsz >= MIN_SHARES - 1e-12)
        return tx, bid, bsz, sold

    def trades(self, strat, t, te, s, price, size, fee_in, how, t_exit, exit_px, fee_out, sh_exit=None):
        """Trade records (TRADE_COLS)."""
        n = len(te)
        sh20 = np.minimum(UNIT_MAX, np.asarray(size, float))
        if sh_exit is not None:
            sh20 = np.where(np.isfinite(sh_exit), np.minimum(sh20, sh_exit), sh20)
        pps = np.asarray(exit_px, float) - np.asarray(price, float) - np.asarray(fee_in, float) - np.asarray(fee_out, float)
        return pd.DataFrame({"market": self.m, "strat": strat, "t": np.broadcast_to(t, n), "te": te,
                             "side": np.broadcast_to(np.asarray(s, np.int64), n), "price": price, "fee_in": fee_in,
                             "how": np.asarray(how, np.int64), "t_exit": t_exit, "exit_px": exit_px,
                             "fee_out": fee_out, "sh20": sh20, "pps": pps, "hold": np.asarray(t_exit) - te})

    def settle_or_sell(self, strat, t, te, s, pe, se, x_dec):
        """Hold, or taker exit decided at x_dec (NaN: hold to settlement)."""
        won = self.won(s)
        tx, bx, bsx, sold = self.taker_exit(np.where(np.isfinite(x_dec), x_dec, self.end), s)
        sold &= np.isfinite(x_dec)
        return self.trades(strat, t, te, s, pe, se, fee(pe), np.where(sold, 1, 0), np.where(sold, tx, self.end),
                           np.where(sold, bx, won), np.where(sold, fee(np.where(sold, bx, 0.5)), 0.0),
                           np.where(sold, bsx, np.nan))

    def resting_exit(self, strat, t, te, s, pe, se, hold):
        """Entry at pe (te), resting sell at min(pe + 2c, 0.99) live at te + 0.5, else taker exit at te + hold."""
        out = []
        for sd in (1, -1):
            k = np.flatnonzero(np.asarray(s) == sd)
            if not len(k):
                continue
            tt, te_, pe_, se_ = np.asarray(t)[k], te[k], pe[k], se[k]
            L = np.minimum(np.round(pe_ + MAKE_X, 6), MAKER_CAP)
            tl = te_ + LAT_S
            gl = self.bk.asof(self.m, tl)
            bl, _, bsl, _, okl = self.bk.quote(gl, np.full(len(k), sd))
            with np.errstate(invalid="ignore"):
                at_l = okl & np.isfinite(bl) & (bl >= L - 1e-9)  # the book already crosses our price
                cross = at_l & self.bk.fresh(gl, tl) & (tl < self.end - T_EPS) & (bsl >= MIN_SHARES - 1e-12)
            ev_t, ev_v, ev_s = self.events(sd, "sell")
            ft, fs = _first_hit(ev_t, ev_v, ev_s, tl, np.minimum(te_ + hold, self.end), L)
            made = ~at_l & np.isfinite(ft)  # a crossing quote we cannot price (stale, < 5 shares): no resting fill
            tx, bx, bsx, sold = self.taker_exit(te_ + hold, np.full(len(k), sd))
            sold &= ~cross & ~made
            won = self.won(np.full(len(k), sd))
            how = np.where(cross, 3, np.where(made, 2, np.where(sold, 1, 0)))
            t_ex = np.where(cross, tl, np.where(made, ft, np.where(sold, tx, self.end)))
            px = np.where(cross, bl, np.where(made, L, np.where(sold, bx, won)))
            f_out = np.where(cross | sold, fee(np.where(cross | sold, px, 0.5)), 0.0)
            sh = np.where(cross, bsl, np.where(made, fs, np.where(sold, bsx, np.nan)))
            out.append(self.trades(strat, tt, te_, np.full(len(k), sd), pe_, se_, fee(pe_), how, t_ex, px, f_out, sh))
        return pd.concat(out, ignore_index=True) if out else None


def _greedy(t, gap):
    """Indices kept greedily in time order, each >= gap after the last kept one (inclusive)."""
    keep, last = [], -np.inf
    for i, x in enumerate(np.asarray(t, float)):
        if x >= last + gap - T_EPS:
            keep.append(i)
            last = x
    return np.asarray(keep, np.int64)


# --------------------------------------------------------------------------- the six strategies
def strat_H(mk):
    sp, end = mk.spot, mk.end
    t, i = sp.jumps(end - H_TAU[0], end - H_TAU[1])
    if not len(t):
        return None
    from scipy.stats import norm
    s = np.sign(sp.d[i]).astype(np.int64)
    xn, xa = sp.x(t), sp.x(t - H_ANCHOR_S)
    tk = sp.at(sp.ts, sp.last(t))
    sig = np.where(np.isfinite(tk), sp.sec.sigma(np.where(np.isfinite(tk), tk, 0.0)), np.nan)
    t_el = t - mk.start
    f = bo.twap_std_factor(t_el, bo.WINDOW_S, max(mk.w, 1)) if mk.w > 0 else np.sqrt(np.maximum(bo.WINDOW_S - t_el, 1.0))
    prior = np.clip(mk.bk.up_mid(mk.bk.asof(mk.m, t - H_ANCHOR_S)), 0.005, 0.995)
    with np.errstate(invalid="ignore", divide="ignore"):
        p1 = norm.cdf(norm.ppf(prior) + (xn - xa) / (f * sig))
    fair = np.where(s > 0, p1, 1 - p1)
    _, ask_t, _, _, _ = mk.bk.quote(mk.bk.asof(mk.m, t), s)
    with np.errstate(invalid="ignore"):
        send = np.flatnonzero(np.isfinite(fair) & np.isfinite(ask_t) & (fair - ask_t - fee(ask_t) >= H_EDGE - 1e-12))
    if not len(send):
        return None
    send = send[_greedy(t[send], H_EVERY_S)]
    t, s, fair = t[send], s[send], fair[send]
    te, pe, se, ok = mk.entry(t, s)
    with np.errstate(invalid="ignore"):
        ok &= fair - pe - fee(pe) >= H_EDGE - 1e-12
    if not ok.any():
        return None
    return mk.settle_or_sell("H", t[ok], te[ok], s[ok], pe[ok], se[ok], np.full(int(ok.sum()), np.nan))


def strat_follow(mk):
    t, i = mk.spot.jumps(mk.end - TAU[0], mk.end - TAU[1])
    if not len(t):
        return None
    k = _greedy(t, FOLLOW_SPACING_S)
    t, s = t[k], np.sign(mk.spot.d[i[k]]).astype(np.int64)
    te, pe, se, ok = mk.entry(t, s)
    if not ok.any():
        return None
    return mk.resting_exit("follow", t[ok], te[ok], s[ok], pe[ok], se[ok], FOLLOW_HOLD_S)


def revert_triggers(mk):
    """(times, sides) of the reversal triggers of a market."""
    bk = mk.bk
    a, b = bk.seg[mk.m]
    T = bk.T[a:b]
    lo, hi = np.searchsorted(T, mk.end - TAU[0] - T_EPS, "left"), np.searchsorted(T, mk.end - TAU[1] + T_EPS, "right")
    g = np.arange(a + lo, a + hi)
    g = g[bk.change[g]]
    if not len(g):
        return np.zeros(0), np.zeros(0, np.int64)
    t = bk.T[g]
    m_now = bk.mid(g, 1)
    m_ago = bk.mid(bk.asof(mk.m, t - REV_WIN_S), 1)
    dx = (mk.spot.x(t) - mk.spot.x(t - REV_WIN_S)) * 1e4
    with np.errstate(invalid="ignore"):
        dm = m_now - m_ago
        hit = np.flatnonzero((np.abs(dm) >= REV_DMID - 1e-9) & (np.abs(dx) < REV_QUIET_BP))
    if not len(hit):
        return np.zeros(0), np.zeros(0, np.int64)
    k = hit[_greedy(t[hit], REV_SPACING_S)]
    return t[k], -np.sign(dm[k]).astype(np.int64)


def strat_revert(mk):
    t, s = revert_triggers(mk)
    if not len(t):
        return None
    te, pe, se, ok = mk.entry(t, s)
    if not ok.any():
        return None
    return mk.resting_exit("revert", t[ok], te[ok], s[ok], pe[ok], se[ok], REV_HOLD_S)


def strat_direction(mk, p_up):
    if not np.isfinite(p_up) or abs(p_up - 0.5) < DIR_THETA - 1e-12:
        return None
    s = np.array([1 if p_up > 0.5 else -1])
    t = np.array([mk.start])
    te, pe, se, ok = mk.entry(t, s)
    if not ok.any():
        return None
    return mk.settle_or_sell("direction", t, te, s, pe, se, np.array([mk.end - DIR_EXIT_TAU]))


def late_model(mk):
    """(decision time, P(Up)) of the late strategy: the driftless model at 60 s left on the last Binance
    print received by then."""
    t = mk.end - LATE_TAU
    i = mk.spot.last(np.array([t]), PRICE_MAX_AGE_S)
    if i[0] < 0:
        return t, np.nan
    x, tk = mk.spot.lp[i], mk.spot.ts[i]
    return t, float(j2.model_fair(mk.spot.sec, np.array([1.0]), np.array([mk.start]), tk, x=x)[0])


def strat_late(mk):
    t, pu = late_model(mk)
    if not np.isfinite(pu):
        return None
    if LATE_BAND[0] - 1e-12 <= pu < LATE_BAND[1]:
        s, p = 1, pu
    elif LATE_BAND[0] - 1e-12 <= 1 - pu < LATE_BAND[1]:
        s, p = -1, 1 - pu
    else:
        return None
    lim = p - LATE_MARGIN
    _, ask_t, _, _, _ = mk.bk.quote(mk.bk.asof(mk.m, np.array([t])), np.array([s]))
    if not (np.isfinite(ask_t[0]) and ask_t[0] <= lim + 1e-9):
        return None
    te, pe, se, ok = mk.entry(np.array([t]), np.array([s]), limit=lim)
    if not ok.any():
        return None
    return mk.settle_or_sell("late", np.array([t]), te, np.array([s]), pe, se, np.array([np.nan]))


def maker_orders(mk, s):
    """The resting buy orders of side s: list of (price, live from, cancel effective)."""
    bk, sp = mk.bk, mk.spot
    a, b = bk.seg[mk.m]
    qa, qb = mk.end - TAU[0], mk.end - TAU[1]
    T = bk.T[a:b]
    lo, hi = np.searchsorted(T, qa - T_EPS, "left"), np.searchsorted(T, qb + T_EPS, "right")
    g = np.arange(a + lo, a + hi)
    mid = bk.mid(g, s)
    with np.errstate(invalid="ignore"):
        L = np.floor((mid - MM_OFFSET) * 100 + 1e-6) / 100
        ok = np.isfinite(L) & (L >= 0.02 - 1e-9) & (L <= 0.98 + 1e-9) & bk.fresh(g, bk.T[g])
    trig, _ = sp.jumps(qa - MM_PAUSE_S, qb)
    Tg, Lg, okg = bk.T[g].tolist(), np.round(L, 2).tolist(), ok.tolist()
    trig = trig.tolist()
    orders, cur, pause_until, j = [], None, -np.inf, 0
    for r in range(len(Tg)):
        tr = Tg[r]
        while j < len(trig) and trig[j] <= tr + T_EPS:
            c = trig[j]
            if cur is not None:
                orders.append((cur[0], cur[1], max(c + CANCEL_S, cur[1])))
                cur = None
            pause_until = max(pause_until, c + MM_PAUSE_S)
            j += 1
        if tr < pause_until - T_EPS or not okg[r]:
            continue
        if cur is None or Lg[r] != cur[0]:
            if cur is not None:
                orders.append((cur[0], cur[1], max(tr + CANCEL_S, cur[1])))
            cur = (Lg[r], tr + LAT_S)
    if cur is not None:
        c = next((x for x in trig[j:] if x <= qb + T_EPS), None)
        orders.append((cur[0], cur[1], max((qb if c is None else c) + CANCEL_S, cur[1])))
    return orders


def strat_maker(mk):
    out = []
    for s in (1, -1):
        orders = maker_orders(mk, s)
        if not orders:
            continue
        ev_t, ev_v, ev_s = mk.events(s, "buy")
        for L, live, cxl in orders:
            if cxl <= live + T_EPS:
                continue
            g = mk.bk.asof(mk.m, np.array([live]))
            _, ask, _, asz, okq = mk.bk.quote(g, np.array([s]))
            with np.errstate(invalid="ignore"):
                at_l = bool((okq & np.isfinite(ask) & (ask <= L + 1e-9))[0])
                cross = at_l and bool((mk.bk.fresh(g, live) & j2.price_ok(ask, asz))[0]) and live < mk.end - T_EPS
            if cross:
                p, f_in, t_f, size = float(ask[0]), float(fee(ask[0])), live, float(asz[0])
            elif at_l:
                continue  # crossed on arrival but not priceable (stale book, < 5 shares): no fill counted
            else:
                ft, fs = _first_hit(ev_t, ev_v, ev_s, [live], [cxl], [-L])
                if not np.isfinite(ft[0]):
                    continue
                p, f_in, t_f, size = L, 0.0, float(ft[0]), float(fs[0])
            won = float(mk.won(s))
            out.append(mk.trades("maker", np.array([live - LAT_S]), np.array([t_f]), np.array([s]), np.array([p]),
                                 np.array([size]), np.array([f_in]), [0], np.array([mk.end]), np.array([won]),
                                 np.array([0.0])))
            break  # one fill per token per window
    return pd.concat(out, ignore_index=True) if out else None


# --------------------------------------------------------------------------- one archive
def aggregate(tr, markets):
    """Per market and strategy: the QTY columns (0 when nothing traded)."""
    out = pd.DataFrame(0.0, index=pd.Index(markets, name="market"), columns=STRAT_COLS)
    if tr is None or not len(tr):
        for s in STRATS:
            out[f"pps_{s}"] = np.nan
        return out
    t = tr.copy()
    unit = t["price"] + t["fee_in"]
    t["cost"] = UNIT * unit
    t["cost20"] = t["sh20"] * unit
    t["pnl"] = UNIT * t["pps"]
    t["pnl20"] = t["sh20"] * t["pps"]
    t["caph"] = t["cost"] * t["hold"] / 3600.0
    t["caph20"] = t["cost20"] * t["hold"] / 3600.0
    t["trades"] = 1.0
    t["shares"] = UNIT
    g = t.groupby(["market", "strat"])[["trades", "shares", "sh20", "cost", "cost20", "pnl", "pnl20", "caph", "caph20"]].sum()
    for (m, s), r in g.iterrows():
        if m in out.index:
            for q, v in r.items():
                out.at[m, f"{q}_{s}"] = v
    for s in STRATS:
        sh = out[f"shares_{s}"]
        out[f"pps_{s}"] = np.where(sh > 0, out[f"pnl_{s}"] / sh.where(sh > 0, 1.0), np.nan)
    return out


def env_features(win, inp, closes, bk, starts_all):
    """Environment at each window's open (ENV columns, index = win.index)."""
    start = win["start"].to_numpy(float)
    n = len(win)
    E = {}
    M = inp.get("minutes") if inp else None
    M = M if M is not None else Minutes(None)
    for k, name in ((5, "rv5m"), (30, "rv30m"), (60, "rv1h")):
        s2, _ = M.window("rv2", start, k)
        nr, _ = M.window("nr", start, k)
        with np.errstate(invalid="ignore", divide="ignore"):
            E[name] = np.where(nr >= 0.5 * 60 * k, np.sqrt(s2 * 1e-8 / np.maximum(nr, 1) * SEC_YEAR), np.nan)
    v60, c60 = M.window("vol", start, 60)
    v24, c24 = M.window("vol", start, 1440)
    with np.errstate(invalid="ignore", divide="ignore"):
        E["vol_rel"] = np.where((c60 >= 30) & (c24 >= 720) & (v60 > 0) & (v24 > 0),
                                np.log((v60 / np.maximum(c60, 1)) / (v24 / np.maximum(c24, 1))), np.nan)
    bs, bc = M.window("basis_bp", start, 60)
    with np.errstate(invalid="ignore", divide="ignore"):
        E["basis"] = np.where(bc >= 30, bs / np.maximum(bc, 1), np.nan)
    E["funding"] = M.funding(start)
    E["dvol"] = dvol_at(inp.get("dvol") if inp else None, start)
    E["dvol_rv"] = E["dvol"] / 100.0 - E["rv30m"]
    if closes is not None and len(closes):
        E["ret4h"], E["vr60"] = j2.trend_features(closes, start)
    else:
        E["ret4h"], E["vr60"] = np.full(n, np.nan), np.full(n, np.nan)
    dt = pd.to_datetime(start, unit="s", utc=True)
    E["hour"] = dt.hour.to_numpy(float)
    E["weekend"] = (dt.dayofweek >= 5).astype(float)
    fp = inp.get("factor") if inp else None
    E["fac_up"] = (fp[DIR_MODEL].reindex(np.round(start).astype(np.int64)).to_numpy(float)
                   if fp is not None and len(fp) and DIR_MODEL in fp else np.full(n, np.nan))
    for c in ("pm_spread", "ask_size", "pm_up", "book_ups", "book_ups_pre"):
        E[c] = np.full(n, np.nan)
    by_start = {v: k for k, v in starts_all.items()}
    for r, (m, st) in enumerate(zip(win["market"], start)):
        if m in bk.seg:
            a, b = bk.seg[m]
            g = bk.asof(m, np.array([st - 0.001]))
            ub, ua, ubs, uas, uok = bk.quote(g, np.array([1]), live=False)
            db, da, dbs, das, dok = bk.quote(g, np.array([-1]), live=False)
            if uok[0] and dok[0]:
                E["pm_spread"][r] = ((ua - ub) + (da - db))[0] / 2
                E["ask_size"][r] = (uas + das)[0] / 2
                E["pm_up"][r] = (ua + ub)[0] / 2
            T = bk.T[a:b]
            lo, hi = np.searchsorted(T, st - 60 - T_EPS, "left"), np.searchsorted(T, st - T_EPS, "left")
            if hi > lo:
                E["book_ups_pre"][r] = bk.change[a + lo:a + hi].sum() / 60.0
        pm = by_start.get(st - 300)
        if pm is not None and pm in bk.seg:
            a, b = bk.seg[pm]
            T = bk.T[a:b]
            lo, hi = np.searchsorted(T, st - 300 - T_EPS, "left"), np.searchsorted(T, st - T_EPS, "left")
            if hi > lo:
                E["book_ups"][r] = bk.change[a + lo:a + hi].sum() / 300.0
    return pd.DataFrame({c: E[c] for c in ENV}, index=win.index)


def _prints_by(prints, tok):
    """{(market, side, taker side): (receipt s, price, size)} of the 5m tokens' prints, time-sorted."""
    out = {}
    if prints is None or not len(prints) or not tok:
        return out
    q = prints[prints["instrument"].astype(str).isin(tok)]
    if not len(q):
        return out
    ins = q["instrument"].astype(str).to_numpy()
    key = [tok[x] for x in ins]
    q = q.assign(_m=[k[0] for k in key], _s=[k[1] for k in key], _ts=q["taker_side"].astype(str).str.lower())
    for (m, sd, side), g in q.groupby(["_m", "_s", "_ts"], sort=False):
        g = g.sort_values("recv_ts_ms", kind="stable")
        out[(m, sd, side)] = (pd.to_numeric(g["recv_ts_ms"]).to_numpy(float) / 1000.0,
                              pd.to_numeric(g["price"], errors="coerce").to_numpy(float),
                              pd.to_numeric(g["size"], errors="coerce").to_numpy(float))
    return out


def _empty_rows():
    return pd.DataFrame({c: pd.Series(dtype="float64") for c in ROW_COLS})


def day_trades(feat, mkts, binance, prints, inputs=None, closes=None, day="", strats=STRATS):
    """(rows, trades, info) of one archive (plus carry, already concatenated by the caller).

    feat / mkts / binance / prints as from cross.read_day + cross.market_table, cross.read_binance and
    cross.read_poly_trades; inputs: load_inputs(); closes: Binance minute closes (trend features)."""
    info = dict(day=day, markets=0, no_book=0, prints=0 if binance is None else len(binance),
                **{f"n_{s}": 0 for s in STRATS}, **{f"na_{s}": 0 for s in STRATS})
    if mkts is None or mkts.empty:
        return _empty_rows(), pd.DataFrame(columns=TRADE_COLS), info
    all5 = mkts[mkts["horizon"] == 5].copy()
    all5["market_id"] = all5["market_id"].astype(str)
    all5 = all5.drop_duplicates("market_id", keep="last")
    starts_all = dict(zip(all5["market_id"], all5["start"].to_numpy(float) / 1000.0))
    m5 = all5[all5["up_won"].notna()]
    bk = Books(feat, starts_all)
    has = []
    for m, st in zip(m5["market_id"], m5["start"].to_numpy(float) / 1000.0):
        ok = m in bk.seg
        if ok:
            a, b = bk.seg[m]
            T = bk.T[a:b]
            ok = np.searchsorted(T, st + 300 - T_EPS, "left") > np.searchsorted(T, st - T_EPS, "left")
        has.append(ok)
    has = np.asarray(has, bool)
    info["no_book"] = int((~has).sum())
    m5 = m5[has]
    info["markets"] = len(m5)
    if m5.empty:
        return _empty_rows(), pd.DataFrame(columns=TRADE_COLS), info
    win = pd.DataFrame({"market": m5["market_id"].to_numpy(), "start": m5["start"].to_numpy(float) / 1000.0,
                        "end": m5["end"].to_numpy(float) / 1000.0, "won": m5["up_won"].to_numpy(float)})
    spot = Spot(binance if binance is not None else pd.DataFrame(columns=["trade_ts_ms", "recv_ts_ms", "price"]))
    tok = {}
    for m, u, dn in zip(all5["market_id"], all5.get("up_token", [None] * len(all5)),
                        all5.get("down_token", [None] * len(all5))):
        for tk, sd in ((u, 1), (dn, -1)):
            if tk is not None and str(tk) not in ("", "None", "nan"):
                tok[str(tk)] = (m, sd)
    pr_by = _prints_by(prints, tok)
    inputs = inputs or {}
    env = env_features(win, inputs, closes, bk, starts_all)
    fac = env["fac_up"].to_numpy(float)
    parts, cov = [], []
    for r, w in enumerate(win.itertuples(index=False)):
        mk = Market(w.market, w.start, w.end, w.won, bk, spot, pr_by)
        a, b = bk.seg[w.market]
        T = bk.T[a:b]
        lo, hi = np.searchsorted(T, w.start - T_EPS, "left"), np.searchsorted(T, w.end - T_EPS, "left")
        live_chg = bk.change[a + lo:a + hi] & bk.live[a + lo:a + hi]
        book_cov = len(np.unique(np.floor(T[lo:hi][live_chg] - w.start))) / 300.0
        bin_cov = spot.coverage(w.start, w.end)
        cov.append((book_cov, bin_cov, int(live_chg.sum()), mk.w))
        for s in strats:
            if book_cov < COV_BOOK or (s in NEEDS_BINANCE and bin_cov < COV_BIN) or \
                    (s == "direction" and not np.isfinite(fac[r])):
                continue
            f = {"H": strat_H, "follow": strat_follow, "revert": strat_revert, "late": strat_late,
                 "maker": strat_maker}.get(s)
            t = strat_direction(mk, fac[r]) if s == "direction" else f(mk)
            if t is not None and len(t):
                parts.append(t)
    tr = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=TRADE_COLS)
    agg = aggregate(tr, win["market"].to_numpy())
    cov = np.asarray(cov, float)
    rows = pd.DataFrame({"day": day, "market": win["market"].to_numpy(), "start": np.round(win["start"]).astype(np.int64),
                         "won": win["won"].to_numpy(float), "w": cov[:, 3], "book_cov": cov[:, 0], "bin_cov": cov[:, 1],
                         "n_chg": cov[:, 2]})
    rows = pd.concat([rows, env.reset_index(drop=True), agg.reset_index(drop=True)], axis=1)
    for s in STRATS:
        bad = (rows["book_cov"] < COV_BOOK) | ((rows["bin_cov"] < COV_BIN) if s in NEEDS_BINANCE else False)
        if s == "direction":
            bad |= ~np.isfinite(rows["fac_up"])
        if s not in strats:
            bad |= True
        rows.loc[bad, [f"{q}_{s}" for q in QTY]] = np.nan
        info[f"na_{s}"] = int(bad.sum())
        info[f"n_{s}"] = int(np.nansum(rows[f"trades_{s}"]))
    rows["pnl_none"] = 0.0
    return rows[ROW_COLS], tr, info


def day_rows(*args, **kw):
    """day_trades without the trade records."""
    rows, _, info = day_trades(*args, **kw)
    return rows, info


def dedupe(rows):
    """One row per market: the one with more in-window book changes (the first on ties)."""
    if not len(rows):
        return rows
    r = rows.reset_index(drop=True)
    r["_o"] = np.arange(len(r))
    r = r.sort_values(["market", "n_chg", "_o"], ascending=[True, False, True], kind="stable")
    r = r.drop_duplicates("market", keep="first").sort_values(["start", "_o"], kind="stable")
    return r.drop(columns="_o").reset_index(drop=True)


def write_table(rows, out):
    """rows -> out (csv.gz), rounded; under MAX_BYTES, dropping the 20-share columns, then pps, if needed.
    Returns the columns written."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    r = rows.copy()
    for c, nd in ROUND.items():
        if c in r:
            r[c] = r[c].astype(float).round(nd)
    for c in STRAT_COLS:
        q = c.split("_")[0]
        r[c] = r[c].astype(float).round(2 if q in ("sh20", "trades", "shares") else 5)
    drops = ([], [c for c in r if c.split("_")[0] in ("sh20", "cost20", "pnl20", "caph20")],
             [c for c in r if c.split("_")[0] in ("sh20", "cost20", "pnl20", "caph20", "pps")])
    for d in drops:
        w = r.drop(columns=d)
        w.to_csv(out, index=False, compression="gzip")
        if out.stat().st_size <= MAX_BYTES:
            return list(w.columns)
    return list(w.columns)


# --------------------------------------------------------------------------- the run
def _carry(df, col, keep_s, scale=1000.0):
    if df is None or not len(df):
        return None
    t = pd.to_numeric(df[col], errors="coerce")
    return df[t >= t.max() - keep_s * scale].copy()


def run(workdir, out=OUT, days=None, dataset=None, names=None, inputs=INPUTS, budget_s=BUDGET_S, log=None):
    """Every daily archive (the last `days`, or those in `names`), one at a time: per-day shards in
    <workdir>/regime-shards/, then the deduplicated table to `out`."""
    import traceback

    import cross
    log = log or (lambda s: print(s, flush=True))
    if dataset and dataset != cross.DS:
        cross.set_dataset(dataset)
    workdir = Path(workdir)
    shards = workdir / "regime-shards"
    shards.mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    kl, missing = hf.klines(workdir)
    log(f"klines: {len(kl):,} minutes, missing months {missing or 'none'}")
    inp = load_inputs(inputs)
    log(f"inputs: {len(inp['factor']):,} factor windows, {len(inp['dvol']):,} DVOL hours, "
        f"{inp['minutes'].n if inp['minutes'].ok else 0:,} Binance minutes")
    arcs = cross.archives(cross.fetch("MANIFEST.txt").decode())
    if names:
        arcs = [a for a in arcs if a[0] in set(names)]
    elif days:
        arcs = arcs[-int(days):]
    infos, failed, skipped, files, own, carry = [], [], [], [], [], None
    for name, _ in arcs:
        day = name[15:25]
        if time.time() - t_start > budget_s:
            skipped.append(day)
            continue
        local = workdir / name
        try:
            cross.fetch(name, local)
            rows, info, carry = process_archive(local, kl, missing, own, inp, carry, day)
            f = shards / f"{day}.parquet"
            rows.to_parquet(f, index=False)
            files.append(f)
            infos.append(info)
            log(f"{name}: {info['markets']} markets ({info['provisional']} provisional-only left out, {info['no_book']} "
                f"without a book here), trades " + ", ".join(f"{s} {info[f'n_{s}']:,}" for s in STRATS) +
                "; not evaluable " + ", ".join(f"{s} {info[f'na_{s}']}" for s in STRATS) +
                f" ({time.time() - t_start:.0f} s)")
        except Exception:
            log(f"{name}: failed\n{traceback.format_exc()}")
            failed.append(day)
            carry = None
        finally:
            if local.exists():
                local.unlink()
    rows = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True) if files else _empty_rows()
    rows = dedupe(rows)
    rows.to_parquet(workdir / "regime-windows.parquet", index=False)
    cols = write_table(rows, out)
    log(f"{len(rows):,} windows from {len(files)} of {len(arcs)} archives (failed {failed or 'none'}, skipped for "
        f"time {skipped or 'none'}) -> {out} ({Path(out).stat().st_size / 1e6:.1f} MB, {len(cols)} columns)")
    log(summary(rows))
    return rows


def process_archive(local, kl, missing, own, inp, carry=None, day=""):
    """Read one archive and price it with the previous archive's carry (dict: feat, binance, prints,
    markets, or None). `own` collects the archives' own Binance minute closes (the trend features'
    fallback for a kline month that did not download). Returns (rows, info, the next carry)."""
    import cross
    carry = carry or {}
    feat, mk, rs = cross.read_day(local)
    hf._release()
    rs, provisional = hf.final_resolution(rs)
    mk = hf.shrink_markets(mk)
    mt = cross.market_table(mk.drop(columns=["up_won"], errors="ignore"), rs)  # final resolution rows only
    del mk, rs
    m5_all = mt[mt["horizon"] == 5]
    feat = feat[feat["market_id"].astype(str).isin(set(m5_all["market_id"].astype(str)))] if len(feat) else feat
    hf._release()
    binance = cross.read_binance(local)
    prints = cross.read_poly_trades(local)
    toks = set()
    for c in ("up_token", "down_token"):
        if c in m5_all:
            toks |= set(m5_all[c].astype(str))
    prints = prints[prints["instrument"].astype(str).isin(toks)] if len(prints) else prints
    if len(binance):
        tt = pd.to_numeric(binance["trade_ts_ms"], errors="coerce")
        own.append(j2.minute_closes(tt.to_numpy(float) / 1000.0, binance["price"].to_numpy(float)))
    own_c = pd.concat(own).groupby(level=0).last() if own else pd.Series(dtype=float)
    closes = hf.closes_for(kl, own_c, missing)

    def cat(a, b):
        return b if a is None or not len(a) else pd.concat([a, b], ignore_index=True)

    prev = carry.get("markets")
    mt_all = mt if prev is None or not len(prev) else \
        pd.concat([mt, prev[~prev["market_id"].astype(str).isin(set(mt["market_id"].astype(str)))]], ignore_index=True)
    p_all = cat(carry.get("prints"), prints)
    if carry.get("prints") is not None:
        p_all = p_all.drop_duplicates()
    rows, _, info = day_trades(cat(carry.get("feat"), feat), mt_all, cat(carry.get("binance"), binance), p_all,
                               inputs=inp, closes=closes, day=day)
    info["provisional"] = provisional
    nf = _carry(feat, "timestamp_ms", CARRY_BOOK_S)
    keep = set(nf["market_id"].astype(str)) if nf is not None else set()
    nxt = dict(feat=nf, prints=_carry(prints, "recv_ts_ms", CARRY_BOOK_S),
               binance=_carry(binance[["trade_ts_ms", "recv_ts_ms", "price"]], "trade_ts_ms", CARRY_BIN_S)
               if len(binance) else None,
               markets=m5_all[m5_all["market_id"].astype(str).isin(keep)].assign(up_won=np.nan))  # never an outcome
    del feat, binance, prints, p_all
    hf._release()
    return rows, info, nxt


def summary(rows):
    """A short text summary per strategy (all rows): trades / day, $ / day, per share, windows evaluable."""
    if not len(rows):
        return "no rows"
    nd = max(pd.Series(np.floor(rows["start"].to_numpy(float) / DAY)).nunique(), 1)
    L = [f"{len(rows):,} windows over {nd} days"]
    for s in STRATS:
        ev = rows[f"pnl_{s}"].notna()
        tr, pnl, sh = (np.nansum(rows[f"{q}_{s}"]) for q in ("trades", "pnl", "shares"))
        L.append(f"  {s:9s}: evaluable {ev.mean():.0%}, {tr / nd:7.1f} trades/day, ${pnl / nd:+8.2f}/day at 5 shares, "
                 f"{100 * pnl / sh if sh else float('nan'):+.2f}c a share")
    return "\n".join(L)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("days", nargs="?", type=int)
    p.add_argument("--workdir", default="/tmp/cross")
    p.add_argument("--out", default=str(OUT))
    p = sub.add_parser("inputs")
    p.add_argument("--cache")
    p.add_argument("--out-dir", default=str(INPUTS))
    a = ap.parse_args(argv)
    if a.cmd == "run":
        run(a.workdir, a.out, a.days)
    else:
        d = build_inputs(a.cache, a.out_dir)
        print(f"{len(d):,} minutes -> {a.out_dir}")


if __name__ == "__main__":
    main()
