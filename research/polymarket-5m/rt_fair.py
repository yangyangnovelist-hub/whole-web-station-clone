"""Round-trip family 2 (ROUNDTRIP.md 策略族 2): fair-value gap convergence on kacho.io's per-second
BTC 5m books. Buy the side whose ask sits at least g below the model's fair price, sell when the
side mid comes back to fair - eps, after a fixed hold, or hold to settlement (control). Paper
research on market data only, no orders. Engine, costs, timing and statistics are roundtrip.py's;
this module only defines the signals and the variant grid.

    python roundtrip.py run --family rt_fair:variants   # -> SCRATCH/rt/stats_f2_fair_gap.parquet
    python rt_fair.py [--workers 4]                     # same, plus a family-only summary

Signal (decision second d; only rows and klines stamped <= d - 1)
- Book: the panel's row in use at second d - 1 (forward-filled from the last row stamped
  <= d - 1). The row must be at most 5 s old *at the decision*, d - stamp <= 5, i.e. panel
  age[d - 1] <= 4 (the task's "rows older than 5 s at use are not used"; stricter by one second
  than the engine's own MAX_AGE, which counts age at column d - 1). It must not be crossed or
  locked in either book (au > bu and ad > bd).
- Fair: panel.fair[m, d], the driftless point-price P(up) from the kline of second d - 1
  (roundtrip.fair_from). Up fair = fair, Down fair = 1 - fair.
- Gap: buy Up when fair - au >= g, buy Down when (1 - fair) - ad >= g (1e-6 tolerance), with
  g in 2, 3, 4, 6, 8, 10 c. If both hold on one row (only possible when the two books are not
  mirrors) the second is skipped.
- Binance move (optional; ROUNDTRIP.md / the task say "requiring a Binance move in the last
  1 / 3 s" without a size; fixed here before anything was run): the Binance return over the last
  w s up to the last usable kline, r = x[d - 1] - x[d - 1 - w], must be in the side's direction
  and larger than one standard deviation, |r| > 1 * sig[d - 1] * sqrt(w) (same sigma as family
  1). Options: none, w = 1, w = 3.
- Time left at the decision, 300 - d: all (d 1..282), 240..15 (d 60..282), 120..15
  (d 180..282). d <= 282 is the engine's last decision (entry row d + 1, earliest exit row d + 3
  <= 285 = 15 s left).
- Every second at which the condition holds is a candidate; no thinning (the grid lists no
  signal mode). The engine's one-position rule keeps the first candidate, then the first one
  decided after the previous exit filled, so a persistent gap is re-entered after each exit;
  settle variants trade once per market.

Exits (Exit objects of roundtrip.py, nothing new in the engine)
- converge: the first e >= d + 2 with side mid of row e - 1 >= side fair(e) - eps, eps in
  0, 1, 2 c, max hold 10, 30, 60, 120 s (Exit.converge(h, eps, target='fair'));
- fixed hold 5, 10, 30, 60 s (Exit.hold(h));
- hold to settlement (Exit.settle(), the control, flagged `control`).
- No stop-loss (none listed for this family). Forced exit at row 285, unsellable -> settled and
  counted as fallback, both legs pay binary.taker_fee: all as in roundtrip.Engine.

Grid: 6 gaps x 3 move filters x 3 time-left windows = 54 signals; x 17 exits = 918 variants
(54 settle controls).
"""
from __future__ import annotations

import argparse
import functools
import math
import time
from pathlib import Path

import numpy as np
import pandas as pd

import roundtrip as rt

F2 = "f2_fair_gap"
F2_GAPS = (0.02, 0.03, 0.04, 0.06, 0.08, 0.10)
F2_EPS = (0.0, 0.01, 0.02)
F2_MAXHOLD = (10, 30, 60, 120)
F2_HOLDS = (5, 10, 30, 60)
F2_MOVES = (0, 1, 3)                 # 0: no Binance-move requirement; else w seconds
F2_WINDOWS = ("all", "240-15", "120-15")
MOVE_Z = 1.0                         # move must exceed MOVE_Z * sigma * sqrt(w), in the side's direction
USE_AGE = 5                          # a row stamped s may be used at d only if d - s <= USE_AGE
LAST_DECISION = rt.FORCE_ROW - 3     # 282, the engine's last decision with fill_delay = 1
WINDOW_FIRST = {"all": 1, "240-15": rt.T - 240, "120-15": rt.T - 120}


def decision_seconds(window, last=LAST_DECISION):
    """Decision seconds d whose time left 300 - d lies in the window (and d <= last)."""
    if window not in WINDOW_FIRST:
        raise ValueError(window)
    return np.arange(WINDOW_FIRST[window], last + 1)


def gap_entries(panel, g, move_w=0, window="all", z=MOVE_Z):
    """Entries at every decision second where the side's ask is >= g below its model fair."""
    d = decision_seconds(window)
    c = d - 1                                            # rows stamped <= d - 1 (panel column d - 1)
    a = panel.a
    age = a["age"][:, c]
    bu, au, bd, ad = (a[k][:, c].astype(float) for k in ("bu", "au", "bd", "ad"))
    fair = a["fair"][:, d].astype(float)                 # from the kline of d - 1
    with np.errstate(invalid="ignore"):
        ok = (age >= 0) & (age <= USE_AGE - 1) & (au > bu) & (bd < ad)
        up = ok & (fair - au >= g - rt.TOL)
        dn = ok & ((1.0 - fair) - ad >= g - rt.TOL)
        both = up & dn
        up &= ~both
        dn &= ~both
        if move_w:
            k = panel.off + d - 1                        # last usable kline
            r = a["x"][:, k].astype(float) - a["x"][:, k - move_w].astype(float)
            thr = z * a["sig"][:, k].astype(float) * math.sqrt(move_w)
            up &= r > thr
            dn &= r < -thr
    mi, j = np.nonzero(up | dn)
    side = np.where(up[mi, j], 1, -1).astype(np.int8)
    return rt.Entries(mi.astype(np.int64), d[j].astype(np.int64), side)


def f2_exits():
    ex = [rt.Exit.converge(mx, e) for e in F2_EPS for mx in F2_MAXHOLD]
    ex += [rt.Exit.hold(h) for h in F2_HOLDS]
    return ex + [rt.Exit.settle()]


def variants(panel=None):
    out = []
    for g in F2_GAPS:
        for mv in F2_MOVES:
            for window in F2_WINDOWS:
                sp = {"g": g, "move_w": mv, "move_z": MOVE_Z if mv else 0.0, "window": window}
                key = (F2, g, mv, window)
                sig = functools.partial(gap_entries, g=g, move_w=mv, window=window)
                tag = f"g{round(g * 100)}-mv{mv}-{window}"
                for ex in f2_exits():
                    out.append(rt.Variant(F2, f"{tag}-{ex.label()}", sp, sig, ex, key, ex.kind == "settle"))
    return out


def _fmt(r, sp):
    return (f"{100 * r[f'mean_{sp}']:+.2f}c +-{100 * r[f'se_{sp}']:.2f} t {r[f't_{sp}']:+.1f} "
            f"({r[f'n_{sp}']:,} tr / {r[f'mk_{sp}']:,} mk)")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--panel", default=str(rt.PANEL))
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default=str(rt.RT / f"stats_{F2}.parquet"))
    a = ap.parse_args(argv)
    panel = rt.load_panel(a.panel, build=False)
    vs = variants(panel)
    t0 = time.time()
    st, _ = rt.run_variants(rt.Engine(panel), vs, workers=a.workers)
    dt = time.time() - t0
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    st.to_parquet(a.out)
    sel, info = rt.select(st)
    print(f"{F2}: {len(vs):,} variants in {dt:.0f} s -> {a.out}")
    print(f"family-only N = {info['N']}, thr_A = {info['thr_A']:.2e}, pass A = {info['k']}, "
          f"thr_B = {info['thr_B']:.2e}, pass B = {info['k_B']} "
          f"(controls passing B: {int((sel['pass_B'] & sel['control']).sum())})")
    for r in sel.sort_values("t_A", ascending=False).head(10).to_dict("records"):
        print(f"  {r['name']}: A {_fmt(r, 'A')} | B {_fmt(r, 'B')} | hold {r['hold_A']:.0f} s, "
              f"fb {100 * r['fb_A']:.1f}%, conv {100 * r['conv_A']:.0f}%, pass {r['pass_A']:d}/{r['pass_B']:d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
