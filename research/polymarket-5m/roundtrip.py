"""Round-trip (exit before settlement) strategy search on kacho.io's per-second BTC 5m books: the
engine, the stats / Bonferroni selection, and family 1 (Binance momentum scalps). Paper research
on market data only, no orders. The design is ROUNDTRIP.md (committed before anything was run).

    python roundtrip.py build                                   # panel -> SCRATCH/rt/panel.npz
    python roundtrip.py run --family roundtrip:family1_variants # stats -> SCRATCH/rt/stats_<family>.parquet
    python roundtrip.py select                                  # all stats files -> frozen.json + report

Data (verified from the files, 2026-10-04)
- btc_ticks: one row per market second, t = market_start + 0..299. bu/au and bd/ad are the Up and
  Down best bid/ask. The two books are one CLOB shown from both sides: bu + ad = 1 and su = sad,
  sd = sau in > 99.99 % / 96.6 % of rows (the rest are NaN). su/sd = size at the best bid,
  sau/sad = size at the best ask (shares, NaN when that side is empty; then the bid is 0 or the
  ask 1). du/dd = bid-side depth within 5c in USDC (0 when the bid side is empty). Prices are on a
  0.01 grid with 0.001 steps near the extremes. 5e-6 of rows are crossed (ask < bid), 3e-5 locked.
- Official outcome: btc_outcomes.csv (Gamma), all 15,682 markets; kacho's own inferred `outcome`
  disagrees on 0.2 % and is kept only as `up_inferred`.
- Binance BTCUSDT 1 s klines (open time in microseconds in these files; regime.load_klines
  handles us and ms). Seconds without a kline carry the last close.

Panel (one row per market, sorted by start; grid second s = 0..299 from market start)
- Book columns forward-filled from the last row stamped <= s while it is <= MAX_AGE = 5 s old
  (else NaN); `age` = s - stamp of the row in use (-1: none).
- Binance: x[m, OFF + s] = log close of the kline of second start + s minus the log close of the
  second before the market start (the reference/open price), s = -OFF..299; sig = std of 1 s log
  returns over the 600 s ending at that second (at least 300); vol/buy = base volume and taker-buy
  volume of that second. The kline of second s is usable from s + 1, so a decision at d reads
  column OFF + d - 1 and earlier. Real settlement in this period was Chainlink's price at the end
  vs at the start (point price, before 2026-08-07), not Binance; the reference here is Binance's.
- fair[m, d] = driftless point-price P(up) known at decision second d:
  binary.prob_up_european(x[d - 1], 0, sig[d - 1], tau = 300 - d) (the kacho period predates the
  TWAP settlement, as in jump2s.py).

Execution (the task's timing convention; ROUNDTRIP.md 执行)
- A decision at second d uses rows and klines stamped <= d - 1. The entry fills at the ASK of the
  row stamped d + 1; an exit decided at second e fills at the BID of the row stamped e + 1.
  ROUNDTRIP.md's text ("decide at s + 1 with data <= s, fill at the row stamped s + 1") is one row
  less delayed; the task's stricter reading is used for everything that is selected
  (Engine(fill_delay=1)); fill_delay=0 exists only as a sensitivity check and is never selected.
- Fill rows must be the exact row (age 0), not crossed or locked, price in 0.02..0.98 and >= 5
  shares at the touched level (ask size for the entry, bid size for the exit). Both legs pay
  binary.taker_fee (0.07 p (1 - p)).
- Entry decisions 1 <= d <= 282 (so that the earliest exit fill, row d + 3, is <= 285). An entry
  whose fill row fails the checks is dropped; it does not open a position.
- One open position per variant per market: candidate entries are taken in time order and a
  candidate is dropped unless its decision second is > the previous trade's exit fill row (the
  exit must have filled). Settled positions block the rest of the market.
- Exits: 'hold' h s -> decision e = d + h; 'converge' -> the first e >= d + 2 at which the side
  mid of row e - 1 is >= target(e) - eps, with a max hold; target 'fair' is the side's model fair
  known at e (1 - fair for Down), target 'entry' a per-entry side price (Entries.target); an
  optional stop -> the first e >= d + 2 at which the side mid of row e - 1 is <= (side mid of the
  entry fill row) - stop. Monitoring starts at d + 2, the first second the entry row is usable.
  Every exit decision is capped at e = 284 (fill row 285 = 15 s before the end: forced exit).
  'settle' holds to the official outcome (no exit fee).
- If the exit fill row fails the checks, the sale goes to the next row that passes them, up to
  row 285; if none does, the position is held and settled at the official outcome (counted as
  'fallback'; its share is reported). Conservative choices where ROUNDTRIP.md is silent: the
  0.02..0.98 range applies to the exit bid too, the stop's reference is the entry row's mid
  (not the ask), fills never use forward-filled rows, crossed or locked rows are unusable,
  comparisons on prices use a 1e-6 tolerance.

Statistics and selection (ROUNDTRIP.md 判定)
- Per variant and split (A = market start 2026-03-24..04-25 UTC, B = 04-26..05-18): trades,
  markets, mean P&L per share, SE clustered by market (CR1, as kacho_late.cl), one-sided normal
  p of mean > 0, and the SE clustered by UTC day as a check.
- A: p < 0.05 / N and mean > 0, N = all variants of all families (select() pools every stats
  file). B: of those k, p < 0.05 / k and mean > 0 -> frozen list (JSON, full params). A variant
  needs >= MIN_MARKETS = 30 distinct markets in a split to pass it (conservative; ROUNDTRIP.md is
  silent). Hold-to-settlement controls count in N and k like any variant and are flagged
  `control` in the frozen list.

Family modules
- A family is a callable panel -> list[Variant], addressed as "module:function" on the command
  line (e.g. rt_poly_revert:variants). A Variant is (family, name, params, signal, exit, key):
  `signal(panel) -> Entries(market, second, side[, target])`, `exit` an Exit; variants sharing a
  `key` share one signal call. run_variants() simulates, keeps stats (and the trades of the ids
  asked for); `python roundtrip.py run --family mod:fn` writes SCRATCH/rt/stats_<family>.parquet.

Family 1 (Binance momentum scalps; grid from ROUNDTRIP.md and the task)
- Return over the last w s (w in 1, 3, 5, 10, 30) up to the last usable kline: x[d - 1] - x[d - 1 - w].
  Signal when |r| > z sig sqrt(w) (z in 1.5, 2, 3; sig = sig[d - 1]) or |r| > b bp (1, 2, 4, 8);
  buy the side of the move.
- Exits: hold h in (2, 5, 10, 20, 30, 60, 120) x stop (none, 3c, 6c); converge to fair with eps
  (0, 1c) x max hold (60, 120) x stop (none, 3c, 6c); plus the hold-to-settlement control.
- Time left at the decision: all (d <= 282) or > 60 s (d <= 239). First signal per market, or
  every signal at least 10 s after the previous kept signal (thinned on signals, before the
  one-position rule).
- 5 x 7 x 34 x 2 x 2 = 4,760 variants.
"""
from __future__ import annotations

import argparse
import functools
import importlib
import json
import math
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Hashable, NamedTuple

import numpy as np
import pandas as pd

import binary as bo

SCRATCH = Path("/tmp/claude-0/-home-user-whole-web-station-clone/d12980d9-5808-53a6-9da0-aeb3bb8bdfb4/scratchpad")
KACHO = SCRATCH / "kacho"
KLINES = SCRATCH / "klines"
RT = SCRATCH / "rt"
PANEL = RT / "panel.npz"

T = 300               # grid seconds per market
OFF = 120             # Binance columns before the market start
MAX_AGE = 5           # a book row older than this (s) is not used
FORCE_ROW = 285       # latest exit fill row (15 s before the end)
PMIN, PMAX = 0.02, 0.98
MIN_SIZE = 5.0
TOL = 1e-6
SIGMA_WIN, SIGMA_MIN = 600, 300
MIN_MARKETS = 30
ALPHA = 0.05
SPLITS = (("A", "2026-03-24", "2026-04-26"), ("B", "2026-04-26", "2026-05-19"))
SPLIT_CODE = {"A": 0, "B": 1}
KEY = 4096            # (market, second) sort key stride
NONE = 10_000         # "no row" in the int16 next-row tables

BOOK = ("bu", "au", "bd", "ad", "su", "sd", "sau", "sad", "du", "dd")
BIN = ("x", "sig", "vol", "buy")
REASONS = ("hold", "forced", "converge", "stop", "settle", "fallback")
HOLD, FORCED, CONVERGE, STOP, SETTLE, FALLBACK = range(6)


# ----------------------------------------------------------------------------------------------
# Panel


class Panel:
    """Dense per-market arrays. Book arrays are (M, T); Binance arrays (M, OFF + T) with column
    OFF + s for second s; per-market vectors (M,). Extra (M, T) arrays may be added with
    `panel.a[name] = arr` (e.g. a converge target in P(up) units)."""

    def __init__(self, **arrays):
        self.a = dict(arrays)
        self.off = int(self.a.get("off", OFF))
        self.M = len(self.a["start"])

    def __getattr__(self, name):
        a = self.__dict__.get("a")
        if a is not None and name in a:
            return a[name]
        raise AttributeError(name)

    def __getitem__(self, name):
        return self.a[name]

    def mid(self, side):
        b, a = (self.bu, self.au) if side > 0 else (self.bd, self.ad)
        return (b + a) / 2

    def nbytes(self):
        return sum(np.asarray(v).nbytes for v in self.a.values())

    def save(self, path=PANEL):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        np.savez(path, **self.a)

    @classmethod
    def load(cls, path=PANEL):
        with np.load(path, allow_pickle=False) as z:
            return cls(**{k: z[k] for k in z.files})


def _split_code(start):
    day = pd.to_datetime(start, unit="s", utc=True).strftime("%Y-%m-%d")
    out = np.full(len(start), -1, np.int8)
    for name, lo, hi in SPLITS:
        out[(day >= lo) & (day < hi)] = SPLIT_CODE[name]
    return out


def ffill_rows(raw, present, max_age=MAX_AGE):
    """Forward-fill (M, T) arrays along seconds from the last present row while it is <= max_age
    old. Returns (dict of filled arrays, age) with NaN / -1 where no fresh row."""
    cols = np.arange(raw[next(iter(raw))].shape[1])
    stamp = np.where(present, cols, -1)
    last = np.maximum.accumulate(stamp, axis=1)
    age = cols - last
    fresh = (last >= 0) & (age <= max_age)
    rows = np.arange(present.shape[0])[:, None]
    src = np.maximum(last, 0)
    out = {}
    for k, v in raw.items():
        f = v[rows, src]
        f[~fresh] = np.nan
        out[k] = f
    return out, np.where(fresh, age, -1).astype(np.int8)


def fair_from(x, sig, off=OFF):
    """(M, T) driftless point-price P(up) known at decision second d: from the kline of second
    d - 1 (column off + d - 1), tau = T - d."""
    d = np.arange(T)
    xm, sm = x[:, off + d - 1].astype(float), sig[:, off + d - 1].astype(float)
    fair = bo.prob_up_european(xm, 0.0, sm, (T - d)[None, :]).astype(np.float32)
    fair[~np.isfinite(xm) | ~np.isfinite(sm)] = np.nan
    return fair


def _klines_series(klines, first_day, last_day):
    """regime.load_klines on the needed days only (symlinks in a scratch dir)."""
    import regime as rg
    sel = RT / "_klines_sel"
    if sel.exists():
        shutil.rmtree(sel)
    sel.mkdir(parents=True)
    for d in pd.date_range(first_day, last_day, freq="D"):
        f = Path(klines) / f"BTCUSDT-1s-{d:%Y-%m-%d}.zip"
        if not f.exists():
            raise FileNotFoundError(f)
        (sel / f.name).symlink_to(f)
    s = rg.load_klines(sel)
    shutil.rmtree(sel)
    return s


def build_panel(kacho=KACHO, klines=KLINES, out=PANEL):
    t0 = time.time()
    kacho = Path(kacho)
    m = pd.read_parquet(kacho / "btc_markets.parquet",
                        columns=["condition_id", "slug", "market_start", "outcome", "n_ticks"])
    o = pd.read_csv(kacho / "btc_outcomes.csv")
    m = m.merge(o, on="slug", how="left")
    m["start"] = pd.to_datetime(m["market_start"], utc=True).astype("int64") // 10**9
    m = m.sort_values("start").reset_index(drop=True)
    if m["start"].duplicated().any():
        raise ValueError("two markets share a start")
    M = len(m)
    start = m["start"].to_numpy(np.int64)
    up_won = np.where(m["up_won"].isna(), -1, m["up_won"].fillna(False).astype(bool)).astype(np.int8)
    up_inf = np.where(m["outcome"].isna(), -1, (m["outcome"] == "Up")).astype(np.int8)

    t = pd.read_parquet(kacho / "btc_ticks.parquet", columns=["condition_id", "t", *BOOK])
    mi = pd.Series(np.arange(M), index=m["condition_id"]).reindex(t["condition_id"]).to_numpy()
    ok = ~np.isnan(mi)
    mi = mi[ok].astype(np.int64)
    rel = t["t"].to_numpy(np.int64)[ok] - start[mi]
    inside = (rel >= 0) & (rel < T)
    mi, rel = mi[inside], rel[inside]
    present = np.zeros((M, T), bool)
    present[mi, rel] = True
    raw = {}
    for c in BOOK:
        a = np.full((M, T), np.nan, np.float32)
        a[mi, rel] = t[c].to_numpy(np.float32)[ok][inside]
        raw[c] = a
    del t
    book, age = ffill_rows(raw, present)
    del raw

    first = pd.to_datetime(start.min() - OFF - SIGMA_WIN - 60, unit="s", utc=True).normalize()
    last = pd.to_datetime(start.max() + T, unit="s", utc=True).normalize()
    s = _klines_series(klines, first.tz_localize(None), last.tz_localize(None))
    lp = np.log(s["close"].to_numpy(float))
    r = np.diff(lp, prepend=np.nan)
    sig = pd.Series(r).rolling(SIGMA_WIN, min_periods=SIGMA_MIN).std().to_numpy()
    s0 = int(s.index[0])
    secs = start[:, None] + np.arange(-OFF, T)[None, :] - s0
    if secs.min() < 1 or secs.max() >= len(lp):
        raise ValueError("klines do not cover the markets")
    ref = lp[start - 1 - s0]
    x = (lp[secs] - ref[:, None]).astype(np.float32)
    sg = sig[secs].astype(np.float32)
    vol = s["vol"].to_numpy(np.float32)[secs]
    buy = s["buy"].to_numpy(np.float32)[secs]
    fair = fair_from(x, sg, OFF)

    p = Panel(cid=m["condition_id"].to_numpy(str), slug=m["slug"].to_numpy(str), start=start,
              up_won=up_won, up_inferred=up_inf, n_ticks=m["n_ticks"].to_numpy(np.int16),
              split=_split_code(start), day=(start // 86400).astype(np.int32), ref=np.exp(ref),
              age=age, fair=fair, x=x, sig=sg, vol=vol, buy=buy, off=np.int64(OFF), **book)
    if out:
        p.save(out)
    p.a["_build_s"] = np.float64(time.time() - t0)
    return p


def load_panel(path=PANEL, build=True):
    if not Path(path).exists():
        if not build:
            raise FileNotFoundError(path)
        return build_panel(out=path)
    return Panel.load(path)


# ----------------------------------------------------------------------------------------------
# Variant API


class Entries(NamedTuple):
    market: np.ndarray            # market index into the panel
    second: np.ndarray            # decision second d (uses rows / klines stamped <= d - 1)
    side: np.ndarray              # +1 Up, -1 Down
    target: np.ndarray | None = None   # per-entry side-price target for Exit(target='entry')


@dataclass(frozen=True)
class Exit:
    kind: str                     # 'hold' | 'converge' | 'settle'
    h: int = 0                    # hold seconds, or the max hold for 'converge'
    eps: float = 0.0              # converge when side mid >= target - eps
    stop: float = 0.0             # 0: no stop; else exit when side mid <= entry mid - stop
    target: str = "fair"          # 'fair' or another panel (M, T) P(up) array, or 'entry'

    @classmethod
    def hold(cls, h, stop=0.0):
        return cls("hold", int(h), 0.0, float(stop))

    @classmethod
    def converge(cls, maxhold, eps=0.0, stop=0.0, target="fair"):
        return cls("converge", int(maxhold), float(eps), float(stop), target)

    @classmethod
    def settle(cls):
        return cls("settle")

    def params(self):
        if self.kind == "settle":
            return {"exit": "settle"}
        p = {"exit": self.kind, "h": self.h, "stop": self.stop}
        if self.kind == "converge":
            p.update(eps=self.eps, target=self.target)
        return p

    def label(self):
        if self.kind == "settle":
            return "settle"
        s = f"_sl{round(self.stop * 100)}" if self.stop else ""
        if self.kind == "hold":
            return f"h{self.h}{s}"
        return f"cv-{self.target}-e{round(self.eps * 100)}-mx{self.h}{s}"


@dataclass
class Variant:
    family: str
    name: str
    params: dict
    signal: Callable[[Panel], Entries]
    exit: Exit
    key: Hashable = None
    control: bool = False

    @property
    def id(self):
        return f"{self.family}:{self.name}"

    def all_params(self):
        return {**self.params, **self.exit.params()}


def chain(market, nxt):
    """Indices reached from each market's first element by following `nxt` (-1 ends).
    `market` must be sorted."""
    n = len(market)
    if n == 0:
        return np.zeros(0, np.int64)
    cur = np.flatnonzero(np.r_[True, market[1:] != market[:-1]])
    taken = []
    while cur.size:
        taken.append(cur)
        cur = nxt[cur]
        cur = cur[cur >= 0]
    return np.sort(np.concatenate(taken))


def thin(market, second, spacing):
    """Keep signals at least `spacing` s after the previous kept one in the same market.
    Inputs sorted by (market, second); returns kept indices."""
    market = np.asarray(market, np.int64)
    key = market * KEY + np.asarray(second, np.int64)
    nxt = np.searchsorted(key, key + spacing, "left")
    bad = nxt >= len(key)
    nxt[bad] = 0
    nxt = np.where(bad | (market[nxt] != market), -1, nxt)
    return chain(market, nxt)


def first_per_market(market):
    market = np.asarray(market)
    if not len(market):
        return np.zeros(0, np.int64)
    return np.flatnonzero(np.r_[True, market[1:] != market[:-1]])


# ----------------------------------------------------------------------------------------------
# Engine


def _next_true(ok, limit=None):
    """For a (..., T) bool array: the first column >= c that is True (and <= limit), else NONE;
    returns (..., T + 2) int16 so that c may run to T + 1."""
    sh = ok.shape[:-1] + (ok.shape[-1] + 2,)
    idx = np.full(sh, NONE, np.int16)
    cols = np.arange(ok.shape[-1], dtype=np.int16)
    good = ok if limit is None else ok & (cols <= limit)
    idx[..., :-2] = np.where(good, cols, NONE)
    return np.minimum.accumulate(idx[..., ::-1], axis=-1)[..., ::-1]


class Engine:
    """Executes entries under the timing convention. Precomputes per side (0 Up, 1 Down):
    entry / exit validity per row, the next valid exit row, side mids and (lazily) sparse min/max
    tables of the mids and next-converge tables."""

    def __init__(self, panel: Panel, fill_delay=1):
        self.p = panel
        self.D = int(fill_delay)
        self.last_decision = FORCE_ROW - 2 * self.D - 1
        p = panel
        M = p.M
        bid = np.stack([p.bu, p.bd]).astype(np.float32)
        ask = np.stack([p.au, p.ad]).astype(np.float32)
        bsz = np.stack([p.su, p.sd]).astype(np.float32)
        asz = np.stack([p.sau, p.sad]).astype(np.float32)
        good = (p.age >= 0) & (p.au > p.bu)          # fresh, not crossed or locked (Up view)
        exact = good & (p.age == 0)
        with np.errstate(invalid="ignore"):
            inr = lambda v: (v >= PMIN - TOL) & (v <= PMAX + TOL)
            self.entry_ok = exact[None] & inr(ask) & (asz >= MIN_SIZE - TOL)
            exit_ok = exact[None] & inr(bid) & (bsz >= MIN_SIZE - TOL)
        self.bid, self.ask = bid, ask
        mid = (bid + ask) / 2
        mid[:, ~good] = np.nan
        self.mid = mid
        self.nv = _next_true(exit_ok, FORCE_ROW)
        self.won = np.stack([p.up_won, np.where(p.up_won >= 0, 1 - p.up_won, -1)]).astype(np.int8)
        self._tab = {}
        self._nc = {}
        self.M = M

    # sparse tables -----------------------------------------------------------------------
    def _table(self, kind):
        if kind not in self._tab:
            fill = np.inf if kind == "min" else -np.inf
            op = np.minimum if kind == "min" else np.maximum
            base = np.where(np.isnan(self.mid), fill, self.mid).astype(np.float32)
            levels = [base]
            step = 1
            while 2 * step <= T:
                prev = levels[-1]
                nxt = np.full_like(prev, fill)
                nxt[..., :T - step] = op(prev[..., :T - step], prev[..., step:])
                levels.append(nxt)
                step *= 2
            self._tab[kind] = np.stack(levels)        # (L, 2, M, T)
        return self._tab[kind]

    def first_cross(self, k, m, lo, hi, thr, below):
        """First row i in [lo, hi] with side mid <= thr (below) or >= thr (above); hi + 1 if none.
        NaN mids never cross."""
        tab = self._table("min" if below else "max")
        pos = lo.astype(np.int64).copy()
        for lvl in range(tab.shape[0] - 1, -1, -1):
            step = 1 << lvl
            can = pos + step - 1 <= hi
            if not can.any():
                continue
            v = tab[lvl, k, m, np.minimum(pos, T - 1)]
            skip = can & ((v > thr) if below else (v < thr))
            pos += step * skip
        return np.minimum(pos, hi + 1)

    def next_converge(self, target, eps):
        """(2, M, T + 2) int16: the first decision second e >= c with side mid(row e - 1) >=
        side target(e) - eps."""
        key = (target, round(eps, 9))
        if key not in self._nc:
            tg = np.asarray(self.p.a[target], np.float32)
            st = np.stack([tg, 1 - tg])
            c = np.zeros((2, self.M, T), bool)
            with np.errstate(invalid="ignore"):
                c[..., 1:] = self.mid[..., :-1] >= st[..., 1:] - eps - TOL
            self._nc[key] = _next_true(c)
        return self._nc[key]

    # simulation ----------------------------------------------------------------------------
    def simulate(self, ent: Entries, ex: Exit):
        """Trades of one variant: dict of arrays sorted by (market, decision second)."""
        D = self.D
        m = np.asarray(ent.market, np.int64)
        d = np.asarray(ent.second, np.int64)
        side = np.asarray(ent.side, np.int64)
        tgt = None if ent.target is None else np.asarray(ent.target, float)
        ok = (d >= 1) & (d <= self.last_decision) & (m >= 0) & (m < self.M) & (side != 0)
        m, d, side = m[ok], d[ok], side[ok]
        tgt = None if tgt is None else tgt[ok]
        k = (side < 0).astype(np.int64)
        r0 = d + D
        ok = self.entry_ok[k, m, r0]
        if ex.kind == "settle":
            ok &= self.won[k, m] >= 0
        m, d, k, r0 = m[ok], d[ok], k[ok], r0[ok]
        tgt = None if tgt is None else tgt[ok]
        o = np.lexsort((d, m))
        m, d, k, r0 = m[o], d[o], k[o], r0[o]
        tgt = None if tgt is None else tgt[o]
        n = len(m)
        e_cap = FORCE_ROW - D
        if ex.kind == "settle":
            X = np.full(n, -1, np.int64)
            reason = np.full(n, SETTLE, np.int8)
            free = np.full(n, KEY - 1, np.int64)
        else:
            if ex.kind not in ("hold", "converge"):
                raise ValueError(ex.kind)
            e = np.minimum(d + ex.h, e_cap)
            reason = np.where(d + ex.h > e_cap, FORCED, HOLD).astype(np.int8)
            first_check = r0 + 1                      # first e whose row e - 1 is the entry row
            if ex.kind == "converge":
                if ex.target == "entry":
                    if tgt is None:
                        raise ValueError("Exit(target='entry') needs Entries.target")
                    i = self.first_cross(k, m, first_check - 1, e - 1, tgt - ex.eps - TOL, below=False)
                    ec = i + 1
                else:
                    ec = self.next_converge(ex.target, ex.eps)[k, m, first_check].astype(np.int64)
                hit = ec <= e
                reason[hit] = CONVERGE
                e = np.where(hit, ec, e)
            if ex.stop > 0:
                ref = self.mid[k, m, r0].astype(float)
                i = self.first_cross(k, m, first_check - 1, e - 1, ref - ex.stop + TOL, below=True)
                es = i + 1
                hit = es <= e
                reason[hit] = STOP
                e = np.where(hit, es, e)
            X = self.nv[k, m, e + D].astype(np.int64)
            fb = X >= NONE
            reason[fb] = FALLBACK
            if fb.any() and (self.won[k[fb], m[fb]] < 0).any():
                drop = fb & (self.won[k, m] < 0)    # unknown outcome: cannot value
                keep = ~drop
                m, d, k, r0, X, reason, fb = m[keep], d[keep], k[keep], r0[keep], X[keep], reason[keep], fb[keep]
                n = len(m)
            X = np.where(fb, -1, X)
            free = np.where(fb, KEY - 1, X + 1)
        # one position at a time: next candidate whose decision second >= free
        key = m * KEY + d
        nxt = np.searchsorted(key, m * KEY + free, "left")
        bad = nxt >= n
        nxt[bad] = 0
        nxt = np.where(bad | (m[nxt] != m), -1, nxt) if n else nxt
        t = chain(m, nxt)
        m, d, k, r0, X, reason = m[t], d[t], k[t], r0[t], X[t], reason[t]
        ask = self.ask[k, m, r0].astype(float)
        settled = X < 0
        bid = np.where(settled, np.nan, self.bid[k, m, np.maximum(X, 0)].astype(float))
        won = self.won[k, m].astype(float)
        fee_in = bo.taker_fee(ask)
        pnl = np.where(settled, won - ask - fee_in, bid - bo.taker_fee(np.nan_to_num(bid)) - ask - fee_in)
        hold = np.where(settled, T - r0, X - r0)
        return {"market": m, "second": d, "side": np.where(k == 0, 1, -1).astype(np.int8),
                "entry_row": r0, "exit_row": X, "entry_px": ask, "exit_px": bid, "pnl": pnl,
                "hold": hold, "reason": reason}


def trades_frame(panel, tr, variant_id=None):
    f = pd.DataFrame(tr)
    f["reason"] = np.asarray(REASONS)[f["reason"].to_numpy()]
    f.insert(0, "start", pd.to_datetime(panel.start[f["market"].to_numpy()], unit="s", utc=True))
    f.insert(1, "slug", panel.slug[f["market"].to_numpy()])
    if variant_id is not None:
        f.insert(0, "variant", variant_id)
    return f


# ----------------------------------------------------------------------------------------------
# Statistics


def _p_one_sided(t):
    return 0.5 * math.erfc(t / math.sqrt(2)) if np.isfinite(t) else 1.0


def cluster_se(pnl, groups, n_groups):
    """Mean and its CR1 standard error clustered by `groups` (ints < n_groups)."""
    N = len(pnl)
    if N == 0:
        return np.nan, np.nan, 0
    S = np.bincount(groups, weights=pnl, minlength=n_groups)
    c = np.bincount(groups, minlength=n_groups)
    G = int((c > 0).sum())
    mean = S.sum() / N
    if G < 2:
        return mean, np.nan, G
    r = S - mean * c
    return mean, math.sqrt((r @ r) * G / (G - 1)) / N, G


def split_stats(panel, tr):
    """Per split: n, markets, mean, se (market-clustered), se_day, t, p, hold, fallback/forced/
    stop/converge shares."""
    out = {}
    m = tr["market"]
    sp = panel.split[m]
    day0 = int(panel.day.min())
    nd = int(panel.day.max()) - day0 + 1
    for name, code in SPLIT_CODE.items():
        sel = sp == code
        pnl = tr["pnl"][sel]
        mm = m[sel]
        mean, se, G = cluster_se(pnl, mm, panel.M)
        _, se_d, nday = cluster_se(pnl, panel.day[mm] - day0, nd)
        t = mean / se if se and se > 0 else np.nan
        rs = tr["reason"][sel]
        n = len(pnl)
        out.update({f"n_{name}": n, f"mk_{name}": G, f"mean_{name}": mean, f"se_{name}": se,
                    f"sed_{name}": se_d, f"t_{name}": t, f"p_{name}": _p_one_sided(t),
                    f"hold_{name}": float(tr["hold"][sel].mean()) if n else np.nan,
                    f"fb_{name}": float((rs == FALLBACK).mean()) if n else np.nan,
                    f"forced_{name}": float((rs == FORCED).mean()) if n else np.nan,
                    f"stopx_{name}": float((rs == STOP).mean()) if n else np.nan,
                    f"conv_{name}": float((rs == CONVERGE).mean()) if n else np.nan})
    return out


# ----------------------------------------------------------------------------------------------
# Running many variants

_G = {}


def _run_group(args):
    """Worker: one signal key's variants. Uses the engine in _G (inherited on fork)."""
    eng, keep = _G["engine"], _G["keep"]
    variants = args
    rows, kept = [], {}
    ent = variants[0].signal(eng.p)
    for v in variants:
        tr = eng.simulate(ent, v.exit)
        st = split_stats(eng.p, tr)
        rows.append({"id": v.id, "family": v.family, "name": v.name,
                     "params": json.dumps(v.all_params(), sort_keys=True), "control": bool(v.control),
                     "n_signals": int(len(ent.market)), **st})
        if v.id in keep:
            kept[v.id] = tr
    return rows, kept


def group_by_key(variants):
    groups = {}
    for v in variants:
        groups.setdefault(v.key if v.key is not None else v.id, []).append(v)
    return list(groups.values())


def run_variants(panel_or_engine, variants, keep=(), workers=1, progress=True):
    """Simulate every variant; returns (stats DataFrame, {id: trades} for ids in `keep`)."""
    eng = panel_or_engine if isinstance(panel_or_engine, Engine) else Engine(panel_or_engine)
    if any(v.exit.stop > 0 or v.exit.target == "entry" for v in variants):
        eng._table("min"), eng._table("max")
    for v in variants:
        if v.exit.kind == "converge" and v.exit.target != "entry":
            eng.next_converge(v.exit.target, v.exit.eps)
    _G["engine"], _G["keep"] = eng, set(keep)
    groups = group_by_key(variants)
    rows, kept = [], {}
    t0 = time.time()
    done = 0
    if workers > 1:
        import multiprocessing as mp
        with mp.get_context("fork").Pool(workers) as pool:
            for r, k in pool.imap_unordered(_run_group, groups):
                rows += r
                kept.update(k)
                done += len(r)
                if progress and done % 500 < len(r):
                    print(f"  {done}/{len(variants)} variants, {time.time() - t0:.0f} s", flush=True)
    else:
        for g in groups:
            r, k = _run_group(g)
            rows += r
            kept.update(k)
            done += len(r)
            if progress and done % 500 < len(r):
                print(f"  {done}/{len(variants)} variants, {time.time() - t0:.0f} s", flush=True)
    order = {v.id: i for i, v in enumerate(variants)}
    df = pd.DataFrame(rows)
    if len(df):
        df = df.sort_values("id", key=lambda s: s.map(order)).reset_index(drop=True)
    return df, kept


# ----------------------------------------------------------------------------------------------
# Selection


def select(stats, alpha=ALPHA, n_total=None, min_markets=MIN_MARKETS):
    """ROUNDTRIP.md: pass A if mean_A > 0 and p_A < alpha / N (N = all variants); of those k,
    pass B if mean_B > 0 and p_B < alpha / k. Returns (stats with pass_A / pass_B, info)."""
    s = stats.copy()
    N = int(n_total if n_total is not None else len(s))
    pa = (s["mean_A"] > 0) & (s["p_A"] < alpha / max(N, 1)) & (s["mk_A"] >= min_markets)
    k = int(pa.sum())
    pb = pa & (s["mean_B"] > 0) & (s["p_B"] < alpha / max(k, 1)) & (s["mk_B"] >= min_markets)
    s["pass_A"], s["pass_B"] = pa.to_numpy(), pb.to_numpy()
    return s, {"N": N, "k": k, "k_B": int(pb.sum()), "alpha": alpha, "thr_A": alpha / max(N, 1),
               "thr_B": alpha / max(k, 1), "min_markets": min_markets}


def _num(v):
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        return None if not np.isfinite(v) else float(v)
    return v


def frozen_json(sel, info, path=None):
    surv = []
    cols = ("n", "mk", "mean", "se", "sed", "t", "p", "hold", "fb")
    for r in sel[sel["pass_B"]].itertuples(index=False):
        r = r._asdict()
        surv.append({"id": r["id"], "family": r["family"], "name": r["name"],
                     "params": json.loads(r["params"]), "control": bool(r["control"]),
                     **{sp: {c: _num(r[f"{c}_{sp}"]) for c in cols} for sp in ("A", "B")}})
    doc = {"design": "research/polymarket-5m/ROUNDTRIP.md", "engine": "research/polymarket-5m/roundtrip.py",
           "data": "kacho.io BTC 5m per-second books 2026-03-24..05-18; A = starts 03-24..04-25, B = 04-26..05-18",
           "timing": "decision d uses rows/klines stamped <= d-1; entry at the ask of row d+1, exit decided at e at the bid of row e+1",
           **{k: _num(v) for k, v in info.items()}, "survivors": surv}
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return doc


# ----------------------------------------------------------------------------------------------
# Report (Chinese markdown)


def _cell(r, sp):
    n = r[f"n_{sp}"]
    if not n:
        return "–"
    return (f"{100 * r[f'mean_{sp}']:+.2f}¢ ±{100 * r[f'se_{sp}']:.2f}（日 ±{100 * r[f'sed_{sp}']:.2f}），"
            f"t {r[f't_{sp}']:+.2f}，{n:,} 笔 / {r[f'mk_{sp}']:,} 市场")


def report_md(sel, info, top=10, title=None, extra=None):
    L = [title or "# 不持有到结算：kacho 每秒盘口上的大规模买入—卖出搜索（BTC 5m）", "",
         "> 设计见 ROUNDTRIP.md（跑之前提交）。只用行情数据，纸面研究，不下单。",
         "> 决定在 d 秒，只用标为 ≤ d − 1 的盘口行和币安 K 线；按标为 d + 1 的行的卖一买入，"
         "在 e 秒决定平仓、按标为 e + 1 的行的买一卖出；两腿各付 0.07·p(1 − p)；两腿都要求该价位至少 5 份、"
         "价格 0.02–0.98；同一变体同一市场最多一笔持仓；最晚标为 285 的行（剩 15 秒）平仓；"
         "平不掉则持有到官方结算（记为“兜底结算”）。每份盈亏 ± 按市场聚类的标准误（括号内按日聚类）。", "",
         f"- 分段：A = 3/24–4/25 开盘的市场（挑选），B = 4/26–5/18（确认）。",
         f"- 变体总数 N = {info['N']:,}；A 段门槛 p < 0.05 / N = {info['thr_A']:.2e} 且每份 > 0；"
         f"过 A 的 k = {info['k']}；B 段门槛 p < 0.05 / k = {info['thr_B']:.2e} 且每份 > 0；"
         f"冻结 {info['k_B']} 个。每段至少 {info['min_markets']} 个市场才可能通过。", ""]
    if extra:
        L += extra + [""]
    L += ["## 各族汇总", "", "| 族 | 变体数 | 过 A | 过 B |", "|---|---:|---:|---:|"]
    for fam, g in sel.groupby("family", sort=False):
        L.append(f"| {fam} | {len(g):,} | {int(g['pass_A'].sum())} | {int(g['pass_B'].sum())} |")
    for fam, g in sel.groupby("family", sort=False):
        g = g.sort_values("t_A", ascending=False, na_position="last").head(top)
        L += ["", f"## {fam}：A 段 t 值最高的 {len(g)} 个（只作描述，不论是否通过）", "",
              "| 变体 | A 段 | B 段 | 平均持有（秒，A） | 兜底结算占比（A） | 过 A | 过 B |",
              "|---|---|---|---:|---:|:-:|:-:|"]
        for r in g.to_dict("records"):
            L.append(f"| `{r['name']}` | {_cell(r, 'A')} | {_cell(r, 'B')} | {r['hold_A']:.0f} | "
                     f"{100 * r['fb_A']:.1f}% | {'✓' if r['pass_A'] else ''} | {'✓' if r['pass_B'] else ''} |")
    fz = sel[sel["pass_B"]]
    L += ["", "## 冻结名单", ""]
    if not len(fz):
        L.append("没有变体通过 A 和 B 两段。")
    else:
        L += ["| 变体 | 参数 | A 段 | B 段 | 对照 |", "|---|---|---|---|:-:|"]
        for r in fz.to_dict("records"):
            L.append(f"| `{r['id']}` | `{r['params']}` | {_cell(r, 'A')} | {_cell(r, 'B')} | "
                     f"{'持有到结算' if r['control'] else ''} |")
    return "\n".join(L) + "\n"


# ----------------------------------------------------------------------------------------------
# Family 1: Binance momentum scalps

F1 = "f1_binance_momentum"
F1_W = (1, 3, 5, 10, 30)
F1_THR = (("z", 1.5), ("z", 2.0), ("z", 3.0), ("bp", 1.0), ("bp", 2.0), ("bp", 4.0), ("bp", 8.0))
F1_HOLDS = (2, 5, 10, 20, 30, 60, 120)
F1_STOPS = (0.0, 0.03, 0.06)
F1_EPS = (0.0, 0.01)
F1_MAXHOLD = (60, 120)
F1_WINDOWS = ("all", "gt60")
F1_MODES = ("first", "all10")
SPACING = 10


def decision_seconds(window, last=FORCE_ROW - 3):
    d = np.arange(1, last + 1)
    if window == "gt60":
        d = d[T - d > 60]
    elif window != "all":
        raise ValueError(window)
    return d


def select_signals(hit, value, d, mode, spacing=SPACING):
    """From a (M, len(d)) bool `hit` and signed `value`: Entries for the first signal per market
    ('first') or every signal >= spacing s after the previous kept one ('all10')."""
    mi, j = np.nonzero(hit)
    sec = d[j]
    side = np.sign(value[mi, j]).astype(np.int8)
    good = side != 0
    mi, sec, side = mi[good], sec[good], side[good]
    keep = first_per_market(mi) if mode == "first" else thin(mi, sec, spacing)
    return Entries(mi[keep].astype(np.int64), sec[keep].astype(np.int64), side[keep])


def momentum_entries(panel, w, kind, thr, window="all", mode="first", spacing=SPACING):
    """Binance return over the last w s ending at the last usable kline (d - 1)."""
    d = decision_seconds(window)
    c = panel.off + d - 1
    x = panel.x
    r = x[:, c].astype(float) - x[:, c - w].astype(float)
    with np.errstate(invalid="ignore"):
        if kind == "z":
            hit = np.abs(r) > thr * panel.sig[:, c].astype(float) * math.sqrt(w)
        elif kind == "bp":
            hit = np.abs(r) > thr * 1e-4
        else:
            raise ValueError(kind)
    hit &= np.isfinite(r)
    return select_signals(hit, r, d, mode, spacing)


def f1_exits():
    ex = [Exit.hold(h, s) for h in F1_HOLDS for s in F1_STOPS]
    ex += [Exit.converge(mx, e, s) for e in F1_EPS for mx in F1_MAXHOLD for s in F1_STOPS]
    return ex + [Exit.settle()]


def family1_variants(panel=None):
    out = []
    for w in F1_W:
        for kind, thr in F1_THR:
            for window in F1_WINDOWS:
                for mode in F1_MODES:
                    sp = {"w": w, "kind": kind, "thr": thr, "window": window, "mode": mode}
                    key = (F1, w, kind, thr, window, mode)
                    sig = functools.partial(momentum_entries, w=w, kind=kind, thr=thr, window=window, mode=mode)
                    tag = f"w{w}-{kind}{thr:g}-{window}-{mode}"
                    for ex in f1_exits():
                        out.append(Variant(F1, f"{tag}-{ex.label()}", sp, sig, ex, key, ex.kind == "settle"))
    return out


# ----------------------------------------------------------------------------------------------
# CLI


def resolve(spec):
    mod, _, fn = spec.partition(":")
    return getattr(importlib.import_module(mod), fn or "variants")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--kacho", default=str(KACHO))
    b.add_argument("--klines", default=str(KLINES))
    b.add_argument("--out", default=str(PANEL))
    r = sub.add_parser("run")
    r.add_argument("--family", action="append", required=True, help="module:function returning variants")
    r.add_argument("--panel", default=str(PANEL))
    r.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    r.add_argument("--out-dir", default=str(RT))
    s = sub.add_parser("select")
    s.add_argument("--stats", nargs="*", default=None, help="stats parquet files (default: all in rt/)")
    s.add_argument("--frozen", default=str(RT / "frozen.json"))
    s.add_argument("--report", default=str(RT / "roundtrip.md"))
    a = ap.parse_args(argv)
    if a.cmd == "build":
        p = build_panel(a.kacho, a.klines, a.out)
        print(f"panel: {p.M} markets, {p.nbytes() / 1e6:.0f} MB in memory, "
              f"{Path(a.out).stat().st_size / 1e6:.0f} MB on disk, {float(p.a['_build_s']):.0f} s")
    elif a.cmd == "run":
        panel = load_panel(a.panel)
        eng = Engine(panel)
        for spec in a.family:
            vs = resolve(spec)(panel)
            t0 = time.time()
            st, _ = run_variants(eng, vs, workers=a.workers)
            fam = vs[0].family if vs else spec.replace(":", "_")
            outp = Path(a.out_dir) / f"stats_{fam}.parquet"
            st.to_parquet(outp)
            print(f"{fam}: {len(vs):,} variants in {time.time() - t0:.0f} s -> {outp}")
    elif a.cmd == "select":
        files = a.stats or sorted(str(f) for f in RT.glob("stats_*.parquet"))
        st = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
        sel, info = select(st)
        frozen_json(sel, info, a.frozen)
        Path(a.report).write_text(report_md(sel, info), encoding="utf-8")
        print(f"N = {info['N']}, pass A = {info['k']}, pass B = {info['k_B']} -> {a.frozen}, {a.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
