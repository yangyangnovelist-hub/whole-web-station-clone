"""ROUNDTRIP.md families 5 and 7 on kacho.io's per-second BTC 5m books: book imbalance (buy the side
whose own book has the heavy bids, sell h s later) and time x price buckets (at tau s left buy the
favourite or the underdog priced in a bucket, sell at tau2 s left or hold to settlement). Paper
research on market data only, no orders. Built on roundtrip.py (engine, timing, costs, stats,
selection) without changing it; the engine already had everything needed (Exit.hold / settle).

    python rt_book.py            # both families on the full panel -> SCRATCH/rt/stats_f5_*.parquet,
                                 # SCRATCH/rt/stats_f7_*.parquet; prints a family-only selection
    python roundtrip.py run --family rt_book:family5_variants --family rt_book:family7_variants
                                 # the same stats files through roundtrip's CLI
    (the pooled selection and the combined report are roundtrip.py's `select`, over all families.)

Data semantics used here (verified from btc_ticks, 2026-10-04, on top of roundtrip.py's notes)
- su / sau = shares at the Up best bid / best ask, sd / sad the same for Down; never 0, NaN when
  that side of the book is empty (then the bid is 0 or the ask 1). The two books mirror: su = sad,
  sd = sau, so the Up book's size imbalance is minus the Down book's.
- du / dd = bid-side depth within 5c in USDC, not shares: du >= su * bu on 99.9996 % of rows but
  du >= su on only 90.7 %. 0 when the bid side is empty. Because Up's bids sit at p and Down's at
  1 - p, the USDC ratio leans to the favourite by construction (median (du - dd) / (du + dd) is
  +0.71 when the Up mid is 0.8-0.9 and -0.70 at 0.1-0.2, on the first 1 M rows).

Timing (roundtrip.py's convention, unchanged): a decision at second d uses book rows stamped <= d - 1
only (the panel's column d - 1: forward-filled from the last row stamped <= d - 1 while <= 5 s old,
NaN otherwise); the entry fills at the ask of the row stamped d + 1, an exit decided at e at the
bid of the row stamped e + 1; fills need the exact row, >= 5 shares, 0.02..0.98; both legs pay
binary.taker_fee; one open position per variant per market; forced exit at row 285 (15 s before the
end); an unsellable position is valued at the official outcome ('fallback'). Neither family reads
Binance klines.

Family 5: book imbalance (ROUNDTRIP.md 策略族 5; the task's grid)
- Measures, each side read from its own book at column d - 1:
  'size'     (bid size - ask size) / (bid size + ask size) at the best level: Up (su - sau) /
             (su + sau), Down (sd - sad) / (sd + sad);
  'depth'    du / dd one-sidedness as recorded (USDC): Up (du - dd) / (du + dd), Down the negative;
  'depth_sh' ADDED beyond the task's list (counted in N like any variant): the same with the
             depths in approximate shares, du / bu and dd / bd (a lower bound on shares, since every
             level within 5c is <= the best bid), so the favourite lean of USDC depth is removed.
- Buy the side whose score is >= thr (thr in 0.5, 0.7, 0.9; ">=" with a 1e-6 tolerance, so 0.5 =
  bids >= 3x asks). A second at which both sides qualify (only possible on the rare unmirrored
  rows) is ambiguous and skipped. No signal when the row at d - 1 is missing / older than 5 s,
  crossed or locked on either book, a size is NaN, or the denominator is not > 0.
- Exits: hold h in (5, 10, 30, 60) s, plus the hold-to-settlement control (ROUNDTRIP.md 执行
  "另设持有到结算对照", as families 1, 3, 4); controls count in N and are flagged `control`.
- Decision seconds 1..282 (the engine's limit). The condition is level-triggered and persistent,
  so signals are thinned to >= 10 s apart in a market before the engine's one-position rule
  (family 1's "all10", families 3 / 4's only mode); no first-signal mode, no time-left window and
  no stop-loss (the task lists none).
- 3 measures x 3 thresholds x (4 holds + 1 settle) = 45 variants (9 controls): the task's listed
  grid ('size', 'depth') is 2 x 3 x 5 = 30, 'depth_sh' adds 15 (params `listed` = False).

Family 7: time x price buckets (ROUNDTRIP.md 策略族 7; the task's grid)
- tau in (240, 180, 120, 90, 60, 45, 30) s left -> one decision second d = 300 - tau per market,
  reading the side mids of the row at column d - 1 (each side's own (bid + ask) / 2; no trade in
  that market when the row is missing / stale / crossed / locked).
- Role and bucket refer to the price of the side bought: the favourite is the side whose mid is
  > 0.5, the underdog the side whose mid is < 0.5 (a 0.50 / 0.50 market has neither: skipped).
  Buckets on the bought side's mid, half-open so that Up and Down mirror exactly: underdog
  [lo, hi), favourite (lo, hi]; the 0.4-0.6 bucket is therefore [0.4, 0.5) for the underdog and
  (0.5, 0.6] for the favourite (1e-6 tolerance; mids sit on a 0.005 grid). A favourite cannot be
  priced in 0.05-0.2 or 0.2-0.4, nor an underdog in 0.6-0.8 or 0.8-0.95: those 4 of the 10
  role x bucket cells have no trades by definition and are not variants (they would only inflate
  N). The 6 cells: underdog 0.05-0.2, 0.2-0.4, 0.4-0.6; favourite 0.4-0.6, 0.6-0.8, 0.8-0.95.
- Exits: sell decided at tau2 s left (e = 300 - tau2, fill at the row stamped e + 1) for tau2 in
  (tau - 15, tau - 30, tau - 60), or "15 s left" = the forced exit (decision 284, fill row 285, the
  latest the design allows); a tau2 < 15 is dropped (would cross the forced exit) and a tau2 = 15
  is the same exit as "15 s left" (kept once); plus hold to settlement. Settlement variants are
  part of this family's grid ("或 settle") and are also the hold-to-settlement control, so they are
  flagged `control` like every other family's.
- Exits per tau: 240, 180, 120, 90: 4 + settle; 60: 3 + settle (tau - 60 = 0 dropped); 45: 2 +
  settle (tau - 30 = 15 is "15 s left"); 30: 1 + settle -> 29 per cell x 6 cells = 174 variants
  (42 controls).

Total: 45 + 174 = 219 variants (51 hold-to-settlement controls).
"""
from __future__ import annotations

import argparse
import functools
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

import roundtrip as rt

F5 = "f5_book_imbalance"
F7 = "f7_time_price_bucket"
TOL = rt.TOL
T = rt.T

F5_MEASURES = ("size", "depth", "depth_sh")
F5_THR = (0.5, 0.7, 0.9)
F5_HOLDS = (5, 10, 30, 60)
SPACING = 10

F7_TAUS = (240, 180, 120, 90, 60, 45, 30)
F7_BUCKETS = ((0.05, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 0.95))
F7_ROLES = ("fav", "dog")
F7_EXIT_OFFSETS = (15, 30, 60)       # tau2 = tau - offset
F7_END_LEFT = 15                     # "15 s left": the forced exit (fill row 285)


# ----------------------------------------------------------------------------------------------
# Shared: usable book rows


def book_ok(panel, cols):
    """(M, len(cols)) bool: the row in use at these columns is fresh (<= 5 s old) and neither book
    is crossed or locked."""
    cols = np.asarray(cols, np.int64)
    if cols.size and cols.min() < 0:
        raise ValueError("negative book column")
    with np.errstate(invalid="ignore"):
        return ((panel.age[:, cols] >= 0) & (panel.au[:, cols] > panel.bu[:, cols])
                & (panel.ad[:, cols] > panel.bd[:, cols]))


def side_mids(panel, cols):
    """(2, M, len(cols)) side mids (0 Up, 1 Down), each from its own book; NaN where not book_ok."""
    cols = np.asarray(cols, np.int64)
    ok = book_ok(panel, cols)
    up = (panel.bu[:, cols].astype(float) + panel.au[:, cols].astype(float)) / 2
    dn = (panel.bd[:, cols].astype(float) + panel.ad[:, cols].astype(float)) / 2
    out = np.stack([up, dn])
    out[:, ~ok] = np.nan
    return out


# ----------------------------------------------------------------------------------------------
# Family 5: book imbalance


def _ratio(a, b):
    """(a - b) / (a + b), NaN where a or b is NaN or a + b <= 0."""
    s = a + b
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(s > 0, (a - b) / np.where(s > 0, s, 1.0), np.nan)


def heavy_bid_score(panel, measure, cols):
    """(2, M, len(cols)) one-sidedness of each side's own book towards its bids, in [-1, 1]
    (0 Up, 1 Down), from the rows in use at panel columns `cols`; NaN where unusable."""
    cols = np.asarray(cols, np.int64)
    ok = book_ok(panel, cols)
    g = lambda name: panel.a[name][:, cols].astype(float)
    if measure == "size":
        up = _ratio(g("su"), g("sau"))
        dn = _ratio(g("sd"), g("sad"))
    elif measure in ("depth", "depth_sh"):
        du, dd = g("du"), g("dd")
        if measure == "depth_sh":
            bu, bd = g("bu"), g("bd")
            with np.errstate(invalid="ignore", divide="ignore"):
                du = np.where(bu > 0, du / np.where(bu > 0, bu, 1.0), np.where(du == 0, 0.0, np.nan))
                dd = np.where(bd > 0, dd / np.where(bd > 0, bd, 1.0), np.where(dd == 0, 0.0, np.nan))
        up = _ratio(du, dd)
        dn = -up
    else:
        raise ValueError(measure)
    out = np.stack([up, dn])
    out[:, ~ok] = np.nan
    return out


def imbalance_entries(panel, measure, thr, spacing=SPACING):
    """Entries: buy the side whose own book's heavy-bid score at column d - 1 is >= thr; every
    signal >= `spacing` s after the previous kept one in the market (d = 1..282)."""
    d = rt.decision_seconds("all")
    sc = heavy_bid_score(panel, measure, d - 1)
    with np.errstate(invalid="ignore"):
        hit = sc >= thr - TOL
    one = hit[0] ^ hit[1]                                   # both sides qualifying: ambiguous, skip
    mi, j = np.nonzero(one)                                 # sorted by (market, second)
    sec = d[j]
    k = np.where(hit[0][mi, j], 0, 1)
    keep = rt.thin(mi, sec, spacing)
    mi, sec, k = mi[keep], sec[keep], k[keep]
    return rt.Entries(mi.astype(np.int64), sec.astype(np.int64), np.where(k == 0, 1, -1).astype(np.int8))


def f5_exits():
    return [rt.Exit.hold(h) for h in F5_HOLDS] + [rt.Exit.settle()]


def family5_variants(panel=None):
    out = []
    for measure in F5_MEASURES:
        for thr in F5_THR:
            sp = {"measure": measure, "thr": thr, "spacing": SPACING, "window": "all",
                  "buy": "side whose own book has the heavy bids", "listed": measure != "depth_sh"}
            key = (F5, measure, thr)
            sig = functools.partial(imbalance_entries, measure=measure, thr=thr)
            tag = f"{measure}-i{round(thr * 100)}"
            for ex in f5_exits():
                out.append(rt.Variant(F5, f"{tag}-{ex.label()}", sp, sig, ex, key, ex.kind == "settle"))
    return out


# ----------------------------------------------------------------------------------------------
# Family 7: time x price buckets


def f7_cells():
    """The feasible (role, lo, hi) cells: a favourite is priced > 0.5, an underdog < 0.5."""
    return [(role, lo, hi) for role in F7_ROLES for lo, hi in F7_BUCKETS
            if (role == "fav" and hi > 0.5) or (role == "dog" and lo < 0.5)]


def in_cell(mid, role, lo, hi):
    """Bool mask: the side mid is the `role` side and priced in the bucket (underdog [lo, hi),
    favourite (lo, hi]; 1e-6 tolerance). NaN -> False."""
    with np.errstate(invalid="ignore"):
        if role == "dog":
            return (mid < 0.5 - TOL) & (mid >= lo - TOL) & (mid < hi - TOL)
        if role == "fav":
            return (mid > 0.5 + TOL) & (mid > lo + TOL) & (mid <= hi + TOL)
    raise ValueError(role)


def bucket_entries(panel, tau, role, lo, hi):
    """Entries at the single decision second d = 300 - tau: buy the side that is the `role` side
    priced in [lo, hi] per in_cell(), read from the row at column d - 1."""
    d = T - int(tau)
    mids = side_mids(panel, [d - 1])[:, :, 0]               # (2, M)
    hit = in_cell(mids, role, lo, hi)
    one = hit[0] ^ hit[1]
    mi = np.flatnonzero(one)
    k = np.where(hit[0][mi], 0, 1)
    return rt.Entries(mi.astype(np.int64), np.full(len(mi), d, np.int64), np.where(k == 0, 1, -1).astype(np.int8))


def f7_exits(tau, fill_delay=1):
    """[(tau2, Exit)] for a decision at d = 300 - tau: sell decided at tau2 s left (e = 300 - tau2,
    fill row e + 1), tau2 in (tau - 15, tau - 30, tau - 60) or 15 (= the forced exit, fill row
    285); tau2 < 15 dropped, duplicates kept once; then (None, settle)."""
    d = T - int(tau)
    e_cap = rt.FORCE_ROW - fill_delay
    out, seen = [], set()
    for tau2 in [tau - o for o in F7_EXIT_OFFSETS] + [F7_END_LEFT]:
        if tau2 < F7_END_LEFT:
            continue
        e = min(T - tau2, e_cap)
        h = e - d
        if h <= 0 or h in seen:
            continue
        seen.add(h)
        out.append((tau2, rt.Exit.hold(h)))
    return out + [(None, rt.Exit.settle())]


def _b(v):
    return f"{round(v * 100):02d}"


def family7_variants(panel=None):
    out = []
    for tau in F7_TAUS:
        for role, lo, hi in f7_cells():
            sp = {"tau": tau, "decision_second": T - tau, "role": role, "bucket": [lo, hi],
                  "price": "bought side mid at the last usable row"}
            key = (F7, tau, role, lo, hi)
            sig = functools.partial(bucket_entries, tau=tau, role=role, lo=lo, hi=hi)
            tag = f"t{tau}-{role}{_b(lo)}-{_b(hi)}"
            for tau2, ex in f7_exits(tau):
                if ex.kind == "settle":
                    name, vp = f"{tag}-settle", {**sp, "tau2": None}
                else:
                    name, vp = f"{tag}-x{tau2}", {**sp, "tau2": tau2}
                out.append(rt.Variant(F7, name, vp, sig, ex, key, ex.kind == "settle"))
    return out


def variants(panel=None):
    return family5_variants(panel) + family7_variants(panel)


# ----------------------------------------------------------------------------------------------
# Run (family-only selection: provisional; the pooled one is roundtrip.py select)


def _fmt(r, sp):
    if not r[f"n_{sp}"]:
        return "-"
    return (f"{100 * r[f'mean_{sp}']:+.2f}c +/-{100 * r[f'se_{sp}']:.2f} (day +/-{100 * r[f'sed_{sp}']:.2f}) "
            f"t {r[f't_{sp}']:+.2f} p {r[f'p_{sp}']:.1e} n {r[f'n_{sp}']:,}/{r[f'mk_{sp}']:,} mk")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--panel", default=str(rt.PANEL))
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out-dir", default=str(rt.RT))
    ap.add_argument("--top", type=int, default=5)
    a = ap.parse_args(argv)
    t0 = time.time()
    panel = rt.load_panel(a.panel)
    eng = rt.Engine(panel)
    print(f"panel + engine: {panel.M} markets, {time.time() - t0:.1f} s", flush=True)
    stats = []
    for fn in (family5_variants, family7_variants):
        vs = fn(panel)
        t1 = time.time()
        st, _ = rt.run_variants(eng, vs, workers=a.workers, progress=False)
        outp = Path(a.out_dir) / f"stats_{vs[0].family}.parquet"
        st.to_parquet(outp)
        print(f"{vs[0].family}: {len(vs)} variants in {time.time() - t1:.1f} s -> {outp}", flush=True)
        stats.append(st)
    st = pd.concat(stats, ignore_index=True)
    sel, info = rt.select(st)
    print(json.dumps(info))
    for fam, g in sel.groupby("family", sort=False):
        _, fi = rt.select(g)
        print(f"{fam}: N {len(g)}, pass A {int(g['pass_A'].sum())}, pass B {int(g['pass_B'].sum())} "
              f"(pooled f5+f7 N); alone: pass A {fi['k']}, pass B {fi['k_B']}")
    top = sel.sort_values("t_A", ascending=False, na_position="last").head(a.top)
    for r in top.to_dict("records"):
        print(f"{r['id']}: A {_fmt(r, 'A')} | B {_fmt(r, 'B')} | hold {r['hold_A']:.0f} fb {100 * r['fb_A']:.1f}% "
              f"passA {r['pass_A']} passB {r['pass_B']}")
    print(f"total {time.time() - t0:.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
