"""Round-trip family 6 (ROUNDTRIP.md 策略族 6): the FACTORS.md multi-factor model, bought after the
open of each BTC 5m market on kacho.io's per-second books and sold on the book before the end (or
held to settlement as the control). Paper research on market data only, no orders. Engine, costs,
timing and statistics are roundtrip.py's; this module only defines the signals and the grid.

    python roundtrip.py run --family rt_factors:variants   # -> SCRATCH/rt/stats_f6_factor_model.parquet
    python rt_factors.py [--workers 4]                      # same, plus coverage, an out-of-sample
                                                            # check of the predictions and a summary

Predictions (only out-of-sample ones)
- R["preds"] of SCRATCH/factors/factors_study.pkl (factors.study, the v2 study of 2026-10-04 10:15,
  written after features_5m.parquet): one P(up) per 5-minute UTC boundary T, columns
  "{ridge,hgb}_{5m,15m,1h,4h}". Every value is a walk-forward prediction (factors.walk_forward):
  the 7-day block holding T is predicted by a model fit only on rows T' >= 3/24 whose target ended
  by the block start (T' + h <= block start, expanding, purged); the first 14 days only train, so
  predictions start 2026-04-07 00:00. No in-sample fit is ever stored there. `verify_oos` re-runs
  that walk-forward on features_5m.parquet for every block that touches the kacho period and
  checks that the stored values are reproduced (main() does it before running the grid).
- Used here: ridge_5m, hgb_5m (the 5m-horizon models) and ridge_15m, hgb_15m (the 15m-horizon
  models' P(BTC up over [T, T + 15 min]) applied to the 5m market opening at T), the four the task
  lists. 1h / 4h are not used (not in the grid).
- Caveat, written before running: ridge's penalty (alpha = 3e4 for both 5m and 15m) was picked by
  factors.choose_alpha from a 10-value grid by the pooled Brier score of the walk-forward
  predictions of FACTORS.md's period A (3/24-6/30), which contains both of this study's splits; so
  ridge's coefficients are out of sample but one scalar hyper-parameter saw these months'
  outcomes. HGB's settings are fixed and untuned (factors.HGB_PARAMS). The FACTORS.md models learn
  Binance spot's direction; nothing in them was fitted to Polymarket prices.
- Market <-> prediction: the prediction stamped T is used for the market whose start is T (kacho
  starts are all multiples of 300 s). Markets with no prediction (A before 4/7) cannot trade.

Timing (the task's convention; roundtrip.Engine(fill_delay=1))
- The prediction at T uses only data known at T (factors.py: 1 s klines opening <= T - 1, 1 m
  rows closed by T, Binance metrics from create_time + 8 min, Deribit trades before T), so it is
  known at every decision second d >= 1 of the market.
- Entry: decision second d in (1, 5, 10, 30) ("first allowed row" = d = 1, entry delays 5 / 10 /
  30 s read as decision seconds 5 / 10 / 30); the entry fills at the ASK of the row stamped d + 1
  (>= 5 shares, 0.02..0.98, exact row, not crossed or locked). If that row fails the checks the
  market is skipped (the engine's rule; no retry at a later row; row 2 exists for 99.98 % of
  markets).
- Side: Up if P(up) > 0.5 + c, Down if P(up) < 0.5 - c (strict; c in 0, 2, 4, 6 c; P = 0.5 exactly
  never trades). One trade per market per variant.
- Disagreement filter ("only trades when the opening mid disagrees with the model by >= 2 / 5 c"):
  none, or gap g in (2 c, 5 c): the model's price of the traded side must exceed that side's book
  mid by at least g, i.e. P(up) - Up mid >= g for Up and (1 - P(up)) - Down mid >= g for Down
  (1e-6 tolerance). The mid is the one known at the decision: the panel row in use at column
  d - 1 (stamped in [d - 5, d - 1], i.e. age[d - 1] <= 4, so no row older than 5 s at use), with
  both books two-sided (0 < bid < ask < 1) and not crossed or locked; else no trade. For d = 1 that
  row is the row stamped 0, the opening mid; for the delayed entries the row stamped 0 is older than
  5 s at use, so the latest usable row stands in for it (written down before running).
- Exit: at 120 / 60 / 30 / 15 s left, the exit decision is e = 299 - L (rows stamped <= 298 - L
  known) and fills at the BID of the row stamped 300 - L (rows 180 / 240 / 270 / 285; Exit.hold
  with h = 299 - L - d). If that row fails the bid checks the engine sells at the next valid row up
  to 285, else values the position at the official outcome ('fallback', reported). Or hold to
  settlement (Exit.settle(); the control, flagged `control`, counted in N). Both legs pay
  binary.taker_fee; P&L per share. No stop-loss (none listed).

Grid (pre-registered by the task): 4 models x 4 c x 4 entry seconds x 3 filters = 192 signals,
x 5 exits = 960 variants (192 settle controls).
"""
from __future__ import annotations

import argparse
import functools
import json
import pickle
import time
from pathlib import Path

import numpy as np
import pandas as pd

import roundtrip as rt

F6 = "f6_factor_model"
F6_MODELS = ("ridge_5m", "hgb_5m", "ridge_15m", "hgb_15m")
F6_C = (0.0, 0.02, 0.04, 0.06)
F6_ENTRY = (1, 5, 10, 30)            # decision seconds (entry fills at row d + 1)
F6_GAPS = (0.0, 0.02, 0.05)          # 0: no disagreement filter
F6_LEFT = (120, 60, 30, 15)          # exit fill row 300 - L; None = hold to settlement
USE_AGE = 5                          # a row stamped s may be used at d only if d - s <= USE_AGE
TOL = rt.TOL
STUDY = rt.SCRATCH / "factors" / "factors_study.pkl"
FACTORS_CACHE = rt.SCRATCH / "factors"
VERIFY_FROM, VERIFY_UNTIL = "2026-03-24", "2026-05-19"   # the kacho period (A and B of ROUNDTRIP.md)

_STUDY_CACHE: dict = {}


# ----------------------------------------------------------------------------------------------
# Predictions


def load_study(path=STUDY):
    path = str(path)
    if path not in _STUDY_CACHE:
        with open(path, "rb") as fh:
            _STUDY_CACHE[path] = pickle.load(fh)
    return _STUDY_CACHE[path]


def load_preds(path=STUDY, models=F6_MODELS):
    """Walk-forward out-of-sample P(up) per 5-minute boundary T (index, int seconds)."""
    p = load_study(path)["preds"]
    missing = [m for m in models if m not in p.columns]
    if missing:
        raise KeyError(f"{path}: no predictions for {missing}")
    if not p.index.is_unique:
        raise ValueError("prediction index is not unique")
    return p[list(models)]


def pred_key(model):
    return f"f6_pred_{model}"


def attach_preds(panel, preds):
    """panel.a['f6_pred_<model>'] = (M,) P(up) stamped at each market's start (NaN if none)."""
    start = np.asarray(panel.start, np.int64)
    for model in preds.columns:
        panel.a[pred_key(model)] = preds[model].reindex(start).to_numpy(np.float64)
    return panel


def ensure_preds(panel, models=F6_MODELS, path=STUDY):
    if any(pred_key(m) not in panel.a for m in models):
        attach_preds(panel, load_preds(path, models))
    return panel


# ----------------------------------------------------------------------------------------------
# Signals


def decision_mid(panel, d, side):
    """(M,) mid of the `side` book (+1 Up, -1 Down) known at decision second d: the panel row in
    use at column d - 1, stamped in [d - USE_AGE, d - 1], both books two-sided (0 < bid < ask < 1);
    NaN otherwise."""
    c = int(d) - 1
    if c < 0:
        raise ValueError("decision second must be >= 1")
    age = panel.age[:, c].astype(np.int64)
    bu, au = panel.bu[:, c].astype(float), panel.au[:, c].astype(float)
    bd, ad = panel.bd[:, c].astype(float), panel.ad[:, c].astype(float)
    with np.errstate(invalid="ignore"):
        ok = ((age >= 0) & (age <= USE_AGE - 1) & (bu > 0) & (au > bu) & (au < 1)
              & (bd > 0) & (ad > bd) & (ad < 1))
    mid = np.where(np.asarray(side) < 0, (bd + ad) / 2, (bu + au) / 2)
    return np.where(ok, mid, np.nan)


def model_side(p, c):
    """+1 where P(up) > 0.5 + c, -1 where P(up) < 0.5 - c, else 0 (NaN -> 0)."""
    p = np.asarray(p, float)
    side = np.zeros(len(p), np.int8)
    with np.errstate(invalid="ignore"):
        side[p > 0.5 + c] = 1
        side[p < 0.5 - c] = -1
    return side


def model_entries(panel, model, c, d, gap=0.0):
    """One entry per market at decision second d on the side the model favours by more than c;
    with gap > 0 only where the model's side price exceeds that side's mid at d by >= gap."""
    ensure_preds(panel, (model,))
    p = np.asarray(panel.a[pred_key(model)], float)
    side = model_side(p, c)
    if gap > 0:
        mid = decision_mid(panel, d, side)
        pside = np.where(side > 0, p, 1 - p)
        with np.errstate(invalid="ignore"):
            ok = np.isfinite(mid) & (pside - mid >= gap - TOL)
        side[~ok] = 0
    m = np.flatnonzero(side != 0)
    return rt.Entries(m.astype(np.int64), np.full(len(m), int(d), np.int64), side[m])


def exit_for(d, left):
    """Exit filling at the bid of row 300 - left (decision 299 - left), or settle (left None)."""
    if left is None:
        return rt.Exit.settle()
    h = rt.T - 1 - int(left) - int(d)
    if h < 2 or rt.T - 1 - left > rt.FORCE_ROW - 1:
        raise ValueError(f"exit at {left} s left is not reachable from decision {d}")
    return rt.Exit.hold(h)


def _tag(model, c, d, gap, left):
    g = "all" if gap == 0 else f"gap{round(gap * 100)}"
    x = "settle" if left is None else f"left{left}"
    return f"{model}-c{round(c * 100)}-d{d}-{g}-{x}"


def variants(panel=None):
    if panel is not None:
        ensure_preds(panel)
    out = []
    for model in F6_MODELS:
        for c in F6_C:
            for d in F6_ENTRY:
                for gap in F6_GAPS:
                    key = (F6, model, c, d, gap)
                    sig = functools.partial(model_entries, model=model, c=c, d=d, gap=gap)
                    for left in F6_LEFT + (None,):
                        sp = {"model": model, "horizon": model.split("_")[1], "c": c, "entry_second": d,
                              "gap": gap, "exit_left": left}
                        out.append(rt.Variant(F6, _tag(model, c, d, gap, left), sp, sig, exit_for(d, left),
                                              key, left is None))
    return out


# ----------------------------------------------------------------------------------------------
# Out-of-sample check and coverage


def oos_check(X, up, T, h_min, model, blks, alpha, stored, lo=None, hi=None):
    """Re-run factors.walk_forward on (X, up, T) and compare with the `stored` predictions on rows
    lo <= T < hi: equal (to 1e-9) means the stored values are the purged walk-forward ones."""
    import factors as fx
    p, fits = fx.walk_forward(X, up, T, h_min, model, blks, alpha=alpha)
    T = np.asarray(T, np.int64)
    w = np.ones(len(T), bool)
    if lo is not None:
        w &= T >= lo
    if hi is not None:
        w &= T < hi
    a, b = p[w], np.asarray(stored, float)[w]
    both = np.isfinite(a) & np.isfinite(b)
    nan_mismatch = int((np.isfinite(a) != np.isfinite(b)).sum())
    diff = float(np.max(np.abs(a[both] - b[both]))) if both.any() else 0.0
    t0 = fx.ts(fx.TRAIN_START)
    ok_y = np.isfinite(up)
    purge = []
    for b0, _ in fits:   # latest training target end vs the block start, per fitted block
        tr = (T >= t0) & (T + h_min * 60 <= b0) & ok_y
        purge.append(int((T[tr] + h_min * 60).max() - b0) if tr.any() else None)
    return {"n": int(both.sum()), "nan_mismatch": nan_mismatch, "max_abs_diff": diff,
            "blocks": len(fits), "max_train_end_minus_block_start_s": max(v for v in purge if v is not None)
            if any(v is not None for v in purge) else None,
            "reproduced": bool(nan_mismatch == 0 and diff <= 1e-9 and both.any())}


def verify_oos(models=F6_MODELS, study=STUDY, cache=FACTORS_CACHE, lo=VERIFY_FROM, hi=VERIFY_UNTIL, log=print):
    """Recompute the walk-forward predictions of every block that touches [lo, hi) from
    features_5m.parquet and compare with the stored ones."""
    import factors as fx
    R = load_study(study)
    preds = R["preds"]
    df = fx.load_frame(cache)
    T = df.index.to_numpy(np.int64)
    X = df[fx.FACTORS].to_numpy(float)
    lo_s, hi_s = fx.ts(lo), fx.ts(hi)
    blks = [b for b in fx.blocks(["A", "B"]) if b[1] > lo_s and b[0] < hi_s]
    out = {}
    for model in models:
        name, h = model.split("_")
        up = fx.up_of(df[f"r_{h}"].to_numpy())
        t0 = time.time()
        out[model] = oos_check(X, up, T, fx.HMIN[h], name, blks, R["alpha"][h] if name == "ridge" else None,
                               preds[model].reindex(T).to_numpy(float), lo_s, hi_s)
        log(f"  oos check {model}: {out[model]} ({time.time() - t0:.0f} s)")
    return out


def coverage(panel, preds=None):
    """Per split: 5-minute boundaries in its calendar, those with a prediction, panel markets, and
    panel markets with a prediction (per model)."""
    preds = preds if preds is not None else load_preds()
    T = preds.index.to_numpy(np.int64)
    day = pd.to_datetime(T, unit="s", utc=True).strftime("%Y-%m-%d")
    out = {}
    for name, lo, hi in rt.SPLITS:
        inside = (day >= lo) & (day < hi)
        sp = panel.split == rt.SPLIT_CODE[name]
        row = {"grid_T": int(inside.sum()), "markets": int(sp.sum())}
        for model in preds.columns:
            row[f"T_pred_{model}"] = int(np.isfinite(preds[model].to_numpy(float)[inside]).sum())
            v = np.asarray(panel.a[pred_key(model)], float)
            row[f"mk_pred_{model}"] = int(np.isfinite(v[sp]).sum())
            row[f"up_share_{model}"] = float(np.mean(v[sp & np.isfinite(v)] > 0.5)) if np.isfinite(v[sp]).any() else None
        has = inside & np.isfinite(preds.to_numpy(float)).all(axis=1)
        row["first_pred"] = pd.to_datetime(T[has].min(), unit="s", utc=True).isoformat() if has.any() else None
        out[name] = row
    return out


# ----------------------------------------------------------------------------------------------
# CLI


def _fmt(r, sp):
    if not r[f"n_{sp}"]:
        return "-"
    return (f"{100 * r[f'mean_{sp}']:+.2f}c +-{100 * r[f'se_{sp}']:.2f} t {r[f't_{sp}']:+.2f} p {r[f'p_{sp}']:.1e} "
            f"({r[f'n_{sp}']:,} tr / {r[f'mk_{sp}']:,} mk)")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--panel", default=str(rt.PANEL))
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default=str(rt.RT / f"stats_{F6}.parquet"))
    ap.add_argument("--summary", default=str(rt.RT / f"summary_{F6}.json"))
    ap.add_argument("--no-verify", action="store_true")
    a = ap.parse_args(argv)
    t_all = time.time()
    panel = rt.load_panel(a.panel, build=False)
    preds = load_preds()
    attach_preds(panel, preds)
    cov = coverage(panel, preds)
    print("coverage:", json.dumps(cov, indent=1))
    ver = None
    if not a.no_verify:
        ver = verify_oos()
        if not all(v["reproduced"] for v in ver.values()):
            raise SystemExit("stored predictions are not the walk-forward ones; refusing to run")
    vs = variants(panel)
    t0 = time.time()
    st, _ = rt.run_variants(rt.Engine(panel), vs, workers=a.workers)
    dt = time.time() - t0
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    st.to_parquet(a.out)
    sel, info = rt.select(st)
    print(f"{F6}: {len(vs):,} variants in {dt:.0f} s -> {a.out}")
    print(f"family-only N = {info['N']}, thr_A = {info['thr_A']:.2e}, pass A = {info['k']}, "
          f"thr_B = {info['thr_B']:.2e}, pass B = {info['k_B']} "
          f"(controls passing B: {int((sel['pass_B'] & sel['control']).sum())})")
    top = sel.sort_values("t_A", ascending=False, na_position="last").head(10)
    for r in top.to_dict("records"):
        print(f"  {r['name']}: A {_fmt(r, 'A')} | B {_fmt(r, 'B')} | hold {r['hold_A']:.0f} s, "
              f"fb {100 * r['fb_A']:.1f}%, pass {r['pass_A']:d}/{r['pass_B']:d}")
    summ = {"family": F6, "variants": len(vs), "signals": len({v.key for v in vs}),
            "controls": int(sum(v.control for v in vs)), "runtime_s": dt, "total_s": time.time() - t_all,
            "coverage": cov, "oos_check": ver, "select_family_only": {k: rt._num(v) for k, v in info.items()},
            "top10_by_tA": [{c: rt._num(r[c]) for c in ("id", "n_A", "mk_A", "mean_A", "se_A", "t_A", "p_A", "n_B",
                                                       "mk_B", "mean_B", "se_B", "t_B", "p_B", "fb_A", "pass_A", "pass_B")}
                            for r in top.to_dict("records")]}
    Path(a.summary).write_text(json.dumps(summ, indent=1, default=lambda o: o.item() if hasattr(o, "item") else str(o)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
