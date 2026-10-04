"""ROUNDTRIP.md families 3 and 4 on kacho.io's per-second BTC 5m books: Polymarket's own mean
reversion (the book moved, Binance did not: buy the side that dropped) and Polymarket's own
momentum (the book moved with Binance: buy the side that rose). Paper research on market data
only, no orders. Built on roundtrip.py (engine, timing, costs, stats, selection) without changing
it; the engine already had everything needed (Entries.target + Exit.converge(target='entry')).

    python roundtrip.py run --family rt_pm:family3_variants --family rt_pm:family4_variants
        -> SCRATCH/rt/stats_f3_pm_reversion.parquet, SCRATCH/rt/stats_f4_pm_momentum.parquet
    (selection and the combined report are roundtrip.py's `select`, over all families' stats.)

Pre-registered grid (the task's, from ROUNDTRIP.md 策略族 3 and 4)
- Book move: side mid moved by >= m over w s, m in (3, 5, 8, 12, 15) c, w in (2, 5, 10, 30).
- Binance over the same w s: reversion (family 3) needs |r| < b sigma, b in (0.5, 1), and buys
  the side whose mid dropped; momentum (family 4) needs r >= 1 sigma in the direction of the side
  whose mid rose, and buys that side.
- Exits: hold h in (5, 10, 30, 60, 120) s x stop-loss (none, 5c); reversion also: converge to the
  pre-move mid, else sell after h (same h grid) x stop (none, 5c). Signals thinned to >= 10 s
  apart; time-left windows as family 1 (all: d <= 282; gt60: d <= 239).
- Family 3: 4 w x 5 m x 2 b x 2 windows x (10 hold + 10 converge + 1 settle) = 1,680 variants;
  family 4: 4 x 5 x 2 windows x (10 hold + 1 settle) = 440 variants; 2,120 in all, of which 120
  are hold-to-settlement controls.

Timing (roundtrip.py's convention, unchanged): a decision at second d uses book rows and Binance
klines stamped <= d - 1 only; the entry fills at the ask of the row stamped d + 1, an exit decided
at e at the bid of the row stamped e + 1; fills need the exact row, >= 5 shares, 0.02..0.98; both
legs pay binary.taker_fee; one open position per variant per market; forced exit at row 285 (15 s
before the end); an unsellable position is valued at settlement ('fallback').

Choices where ROUNDTRIP.md and the task are silent (the conservative or the family-1 one)
- The side mid at column s is the panel's (forward-filled from the last row stamped <= s while it
  is <= 5 s old) (bid + ask) / 2 of that side's own book, NaN when there is no fresh row or the
  row is crossed or locked (the engine's `mid` mask). The move is the NET change between columns
  d - 1 and d - 1 - w (the newest usable row and the row w s before it), not the largest swing
  inside the window. Both ends must be usable; d >= w + 1 so that the window start is a market
  row (no index wrap). "Moved >= m" compares with a 1e-6 tolerance (mids sit on a 0.005 grid).
- Each side's condition is read from its own book. Up and Down mids mirror (Up + Down = 1) on all
  but 3.6e-5 of rows; a second at which both sides qualify is ambiguous and skipped.
- Binance: r = x[d - 1] - x[d - 1 - w], the log return over the klines of seconds d - w .. d - 1
  (the same seconds as the book window); "sigma" is the trailing 1 s volatility sig[d - 1] scaled
  to the window, sig sqrt(w) (family 1's convention). Reversion: |r| < b sig sqrt(w) (strict);
  momentum: r >= sig sqrt(w) for Up, r <= -sig sqrt(w) for Down. Missing sigma or klines -> no
  signal.
- The converge target ("pre-move mid") is the bought side's mid at column d - 1 - w, carried as
  Entries.target; converge when the side mid of row e - 1 is >= that target (eps 0), checked from
  e = d + 2 (the entry row), else sold at the max hold h. ROUNDTRIP.md "回归或 h 秒后卖" -> the max
  hold takes the same h grid as the fixed holds. Momentum has no converge exit (task: converge is
  for reversion only).
- The stop is the engine's: side mid of row e - 1 <= side mid of the entry fill row - 5c.
- Spacing: every signal (either side) at least 10 s after the previous kept one in the market,
  thinned before the engine's one-position rule (family 1's "all10"); there is no first-signal
  mode, since the task lists only the 10 s spacing.
- A hold-to-settlement control per signal set (ROUNDTRIP.md 执行 "另设持有到结算对照", as family 1);
  controls count in N and are flagged `control`.
"""
from __future__ import annotations

import functools
import math

import numpy as np

import roundtrip as rt

F3 = "f3_pm_reversion"
F4 = "f4_pm_momentum"
PM_W = (2, 5, 10, 30)
PM_M = (0.03, 0.05, 0.08, 0.12, 0.15)
F3_B = (0.5, 1.0)          # reversion: Binance |r| < b sigma
F4_Z = 1.0                 # momentum: Binance r >= 1 sigma toward the bought side
PM_HOLDS = (5, 10, 30, 60, 120)
PM_STOPS = (0.0, 0.05)
PM_WINDOWS = ("all", "gt60")
SPACING = 10
TOL = rt.TOL


def side_mids(panel, cols):
    """(2, M, len(cols)) side mids (0 Up, 1 Down) at panel columns `cols` (all >= 0), NaN where
    the column has no fresh row or the row is crossed / locked (as Engine.mid)."""
    cols = np.asarray(cols, np.int64)
    if cols.size and cols.min() < 0:
        raise ValueError("negative book column")
    bu, au = panel.bu[:, cols], panel.au[:, cols]
    good = (panel.age[:, cols] >= 0) & (au > bu)
    up = (bu + au) / 2
    dn = (panel.bd[:, cols] + panel.ad[:, cols]) / 2
    out = np.stack([up, dn]).astype(np.float32)
    out[:, ~good] = np.nan
    return out


def pm_entries(panel, w, m, kind, b, window="all", spacing=SPACING):
    """Entries for a book move of >= m over w s with Binance quiet (kind 'rev', |r| < b sigma,
    buy the side that dropped, target = its pre-move mid) or confirming (kind 'mom', r >= b sigma
    toward the side that rose, buy it; target = its pre-move mid too, unused by its exits)."""
    d = rt.decision_seconds(window)
    d = d[d - 1 - w >= 0]
    now = side_mids(panel, d - 1).astype(float)
    pre = side_mids(panel, d - 1 - w).astype(float)
    dm = now - pre                                           # (2, M, nd)
    c = panel.off + d - 1
    r = panel.x[:, c].astype(float) - panel.x[:, c - w].astype(float)
    s = panel.sig[:, c].astype(float) * math.sqrt(w)
    with np.errstate(invalid="ignore"):
        if kind == "rev":
            hit = (dm <= -m + TOL) & (np.abs(r) < b * s)[None]
        elif kind == "mom":
            hit = (dm >= m - TOL) & np.stack([r >= b * s, r <= -b * s])
        else:
            raise ValueError(kind)
    one = hit[0] ^ hit[1]                                   # both sides qualifying: ambiguous, skip
    mi, j = np.nonzero(one)                                 # sorted by (market, second)
    sec = d[j]
    k = np.where(hit[0][mi, j], 0, 1)
    keep = rt.thin(mi, sec, spacing)
    mi, sec, k, j = mi[keep], sec[keep], k[keep], j[keep]
    return rt.Entries(mi.astype(np.int64), sec.astype(np.int64), np.where(k == 0, 1, -1).astype(np.int8),
                      pre[k, mi, j].astype(float))


def f3_exits():
    ex = [rt.Exit.hold(h, s) for h in PM_HOLDS for s in PM_STOPS]
    ex += [rt.Exit.converge(h, 0.0, s, target="entry") for h in PM_HOLDS for s in PM_STOPS]
    return ex + [rt.Exit.settle()]


def f4_exits():
    return [rt.Exit.hold(h, s) for h in PM_HOLDS for s in PM_STOPS] + [rt.Exit.settle()]


def _label(ex):
    return ex.label().replace("cv-entry-", "cv-premid-")


def family3_variants(panel=None):
    out = []
    for w in PM_W:
        for m in PM_M:
            for b in F3_B:
                for window in PM_WINDOWS:
                    sp = {"w": w, "m": m, "binance": "quiet", "b_sigma": b, "window": window,
                          "spacing": SPACING, "buy": "side that dropped", "converge_target": "pre-move side mid"}
                    key = (F3, w, m, b, window)
                    sig = functools.partial(pm_entries, w=w, m=m, kind="rev", b=b, window=window)
                    tag = f"w{w}-m{round(m * 100)}-b{b:g}-{window}"
                    for ex in f3_exits():
                        out.append(rt.Variant(F3, f"{tag}-{_label(ex)}", sp, sig, ex, key, ex.kind == "settle"))
    return out


def family4_variants(panel=None):
    out = []
    for w in PM_W:
        for m in PM_M:
            for window in PM_WINDOWS:
                sp = {"w": w, "m": m, "binance": "confirm", "z_sigma": F4_Z, "window": window,
                      "spacing": SPACING, "buy": "side that rose"}
                key = (F4, w, m, F4_Z, window)
                sig = functools.partial(pm_entries, w=w, m=m, kind="mom", b=F4_Z, window=window)
                tag = f"w{w}-m{round(m * 100)}-z{F4_Z:g}-{window}"
                for ex in f4_exits():
                    out.append(rt.Variant(F4, f"{tag}-{_label(ex)}", sp, sig, ex, key, ex.kind == "settle"))
    return out


def variants(panel=None):
    return family3_variants(panel) + family4_variants(panel)
