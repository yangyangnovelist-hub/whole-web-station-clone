"""CALIB.md: a calibration map of BTC slow markets - where does the driftless model's probability q differ
systematically from the price p the market traded at, and who was right? Paper research on market data
only: no orders, no keys.

    python calib.py all      [--out real/calib.md]          (points D, points V, fetch, report)
    python calib.py points | fetch | report                 (the same steps one by one)

Data (reused by import, read-only; nothing in resolved.py / nearcert.py is edited):
- D = discovery segment 2026-03-14 .. 09-30: resolved.py's markets (<scratch>/resolved/decided.parquet,
  T*, official results, exclusions), its 1 s Binance klines (spot_1s.npz) and its trades
  (trades.parquet, fetched from min(T*, end - 3600) on, fetch_info.parquet).
- V = validation segment 2025-09-15 .. 2026-03-13: nearcert.py's (<scratch>/nearcert/..., markets
  rule-checked against the 2026 rules, end_mismatch excluded, same model and trade fetch).
- New here: checkpoints 240 and 120 minutes before the end. The model rows of all seven checkpoints are
  recomputed with rs.checkpoints (the 60 / 30 / 10 / 5 / 1 rows are checked to equal the cached
  checkpoints.parquet of each segment). The trades of [t, t + 60) of a checkpoint are taken from the
  cached trades when that market's cached fetch started at or before t; otherwise that one window is
  fetched from data-api (start = t, end = t + 59, inclusive; rs.fetch_trades / rs.Http: <= 4 requests
  per second over all threads, every page cached under <scratch>/calib/http). Gamma and the klines are
  not asked again (everything needed is cached).

Rows (CALIB.md "格子"):
- Model: rs.checkpoints - S = the close of the 1 s kline opening at t - 1, sigma = std of 1 s log returns
  of [t - 3600, t), scaled by sqrt(time to the decision), driftless; hit markets by reflection; hit markets
  already touched (first 1 s crossing before t) and creation windows not yet open are dropped.
- Per checkpoint and market, two rows: Yes with q = p_yes and result = official, No with q = 1 - p_yes and
  result = 1 - official (a 50-50 settlement is 0.5 on both sides). p = share-weighted price of that
  side's token's trades with t <= ts < t + 60 (any taker side); shares, cost = sum(price * size), trades.
- Cell = probability bucket of q x time bin x type: 8 x 4 x 4 = 128 cells.
  Probability buckets are left-closed, right-open: [0.02, 0.05), [0.05, 0.15), ..., [0.95, 0.99); a row
  with q < 0.02 or q >= 0.99 is in no cell. Time bin = the checkpoint's minutes before the end:
  240, 120 -> ">= 120"; 60, 30 -> "30-120"; 10, 5 -> "5-30"; 1 -> "< 5". Type: above, range,
  updown_day, hit (hit_daily + hit_weekly + hit_monthly pooled). updown_4h (Chainlink) is a control:
  same rows, its own table, never in a cell count, k or a verdict.
- Per cell (rows with trades only): per-share pnl maker = result - p (no fee), taker = result - p -
  0.07 p (1 - p), both share-weighted (row weight = its shares), cluster-robust SE by market
  (rs.clustered_mean); q - p share-weighted; markets = clusters with trades; losing markets = markets
  whose net maker dollar pnl in the cell is < 0; worst single-market loss = the most negative net maker
  dollar pnl of one market; shares / cost / profit per day = cell totals / the segment's ET days
  (D 201, V 180) - totals over every trade printed, i.e. ceilings, not what one buyer could get.

Verdicts (CALIB.md "判定"):
- D: a cell is a candidate iff maker pnl > 0, one-sided p < 0.05 / 128 and >= 30 markets with trades.
- V: only the candidates are computed on V; k = candidates outside the [0.95, 0.99) band; a candidate
  passes iff on V maker pnl > 0, one-sided p < 0.05 / k and >= 30 markets with trades.
- Gap rule (not split by cell): buy a row's side when q - p >= m, m in {2, 4, 6} cents; m chosen on D,
  tested on V once.

Conservative choices where CALIB.md is silent (written down before the run):
- "Per share pnl" in the verdicts is the maker version (result - p), the one CALIB.md defines as 每份盈亏
  and the one its forward plan (resting bids) would earn; the taker version is shown beside it and a
  pass whose taker pnl is <= 0 is said so.
- The taker fee is charged at the row's share-weighted p, 0.07 p (1 - p); by concavity this is >= the
  share-weighted fee of the single prints (never smaller).
- One-sided p-values come from Student t with G - 1 degrees of freedom (G = markets with trades) applied
  to the market-clustered t, not from the normal (larger p-values for the same t).
- ">= 30 markets" counts markets WITH TRADES in the cell (the clusters), not markets with checkpoints.
- All 16 cells of the [0.95, 0.99) band (every time bin, also >= 120 minutes, which NEARCERT.md did not
  use) count as "already used on V": they take part in D's Bonferroni (128) and can be D candidates,
  but they are not counted in k and cannot pass as new findings; their V numbers are shown marked.
- The 128 cells of V are NOT computed except D's candidates and the [0.95, 0.99) band (already used);
  the q - p map is D's.
- Gap rule universe: the four Binance-settled types, all seven checkpoints, rows with trades and q in
  [0.02, 0.99) (the cells' universe); a row is taken iff q - p >= m with p its share-weighted price.
  m is the one of {0.02, 0.04, 0.06} with the largest market-clustered t on D (ties: the larger m);
  the rule must also clear D (maker pnl > 0, one-sided p < 0.05 / 3, >= 30 markets) and then V once
  (maker pnl > 0, one-sided p < 0.05, >= 30 markets). The same rule without the [0.95, 0.99) band is
  reported beside it (description; the verdict line says whether a pass survives without it).
- Fetching 240 / 120 windows: only checkpoints with at least one side in a cell (0.01 < p_yes < 0.99)
  are fetched; the others cannot enter any cell or the gap rule.
- Inherited from resolved.py / nearcert.py unchanged: the market sets and exclusions, the noon candle
  convention (chosen there from the official outcomes; it moves the decision time, hence tau, by 60 s),
  risk-flagged markets kept.
- A day-clustered t (ET day of the checkpoint; strikes of one day share one BTC path) is shown for the
  candidates as a description only, never in a verdict.
"""
from __future__ import annotations

import argparse
import gc
import json
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import t as student_t

import nearcert as nc
import resolved as rs

HERE = Path(__file__).resolve().parent
SCRATCH = rs.SCRATCH
CACHE = SCRATCH / "calib"
SEGS = {"D": {"src": SCRATCH / "resolved", "first": rs.FIRST, "last": rs.LAST},
        "V": {"src": SCRATCH / "nearcert", "first": nc.FIRST, "last": nc.LAST}}
CHECKS = (240, 120, 60, 30, 10, 5, 1)
OLD_CHECKS = (60, 30, 10, 5, 1)
QB = (0.02, 0.05, 0.15, 0.30, 0.50, 0.70, 0.85, 0.95, 0.99)
QLAB = ("0.02-0.05", "0.05-0.15", "0.15-0.3", "0.3-0.5", "0.5-0.7", "0.7-0.85", "0.85-0.95", "0.95-0.99")
USED = "0.95-0.99"                                   # the band NEARCERT.md already tested on V
TBINS = (">=120", "30-120", "5-30", "<5")
TYPES = ("above", "range", "updown_day", "hit")
CONTROL = "updown_4h"
TYPE_OF = {"above": "above", "range": "range", "updown_day": "updown_day", "hit_daily": "hit",
           "hit_weekly": "hit", "hit_monthly": "hit", CONTROL: CONTROL}
NCELLS = len(QLAB) * len(TBINS) * len(TYPES)        # 128
ALPHA = 0.05
MIN_MK = 30
GAPS = (0.02, 0.04, 0.06)
FEE = 0.07
RATE = 4.0
NAN = float("nan")
TYPE_CN = {"above": "高于", "range": "区间", "updown_day": "按日涨跌", "hit": "触及", CONTROL: "4 小时（对照）"}
TB_CN = {">=120": "≥120分", "30-120": "30–120分", "5-30": "5–30分", "<5": "<5分"}


# ===================================================================== cells

def q_bucket(q):
    """Probability bucket label of each q ([lo, hi) as in QB), None outside [0.02, 0.99)."""
    q = np.asarray(q, float)
    i = np.searchsorted(np.asarray(QB), q, side="right") - 1
    ok = np.isfinite(q) & (i >= 0) & (i < len(QLAB))
    return np.where(ok, np.array(QLAB + ("",), dtype=object)[np.where(ok, i, len(QLAB))], None)


def t_bin(check):
    """Time bin of a checkpoint (minutes before the end)."""
    c = np.asarray(check, float)
    return np.where(c >= 120, ">=120", np.where(c >= 30, "30-120", np.where(c >= 5, "5-30", "<5"))).astype(object)


def in_any_cell(p_yes):
    """At least one side of the checkpoint is in a probability bucket (else no row of it enters a cell)."""
    p = np.asarray(p_yes, float)
    return pd.notna(q_bucket(p)) | pd.notna(q_bucket(1.0 - p))


def all_cells(types=TYPES):
    return list(product(QLAB, TBINS, types))


# ===================================================================== model points

def model_points(mk, spot, checks=CHECKS):
    """The driftless model rows of resolved.py (rs.checkpoints) at these checkpoints: one row per market x
    checkpoint still undecided, using only klines before t (and, for hit markets, whether the first 1 s
    crossing happened before t)."""
    return rs.checkpoints(mk, spot, checks=checks)


def load_spot(seg):
    """The segment's cached Spot (1 s klines, as resolved.py / nearcert.py built it)."""
    src = SEGS[seg]["src"]
    if not (src / "spot_1s.npz").exists():
        raise FileNotFoundError(src / "spot_1s.npz")
    if seg == "D":
        return rs.build_spot(cache=src, http=None)[0]
    return nc.build_spot(src, http=None)[0]


def points(seg, out=CACHE, log=print):
    """Checkpoints 240 .. 1 of a segment (cached as <out>/<seg>/checkpoints.parquet) and the comparison of
    the 60 .. 1 rows with the segment's cached checkpoints."""
    d = Path(out) / seg
    d.mkdir(parents=True, exist_ok=True)
    path, chk_path = d / "checkpoints.parquet", d / "check_old.json"
    if path.exists() and chk_path.exists():
        return pd.read_parquet(path), json.loads(chk_path.read_text())
    src = SEGS[seg]["src"]
    mk = pd.read_parquet(src / "decided.parquet")
    t0 = time.time()
    spot = load_spot(seg)
    cp = model_points(mk, spot)
    del spot
    gc.collect()
    old = pd.read_parquet(src / "checkpoints.parquet")
    m = old.merge(cp[cp["check"].isin(OLD_CHECKS)], on=["cid", "check"], how="outer", suffixes=("_o", "_n"),
                  indicator=True)
    both = m[m["_merge"] == "both"]
    chk = {"old_rows": len(old), "new_rows_60_1": int(cp["check"].isin(OLD_CHECKS).sum()),
           "only_old": int((m["_merge"] == "left_only").sum()), "only_new": int((m["_merge"] == "right_only").sum()),
           "max_dq": float((both["p_yes_o"] - both["p_yes_n"]).abs().max()) if len(both) else NAN,
           "seconds": time.time() - t0}
    cp.to_parquet(path)
    chk_path.write_text(json.dumps(chk))
    log(f"{seg}: {len(cp):,} checkpoint rows {cp.groupby('check').size().to_dict()}; vs cached 60..1: {chk}")
    return cp, chk


# ===================================================================== trades of the checkpoint windows

def fetch_cover(cp, info):
    """Per checkpoint row: the cached trade fetch of its market started at or before t (not truncated), so
    the cached trades hold every trade of [t, t + 60)."""
    if not len(info):
        return np.zeros(len(cp), bool)
    fs = info.drop_duplicates("cid", keep="last").set_index("cid")
    start = fs["fstart"].reindex(cp["cid"]).to_numpy(float)
    trunc = fs["truncated"].reindex(cp["cid"]).astype(object).fillna(True).to_numpy().astype(bool)
    return np.isfinite(start) & (start <= cp["t"].to_numpy(float)) & ~trunc


def fetch_plan(cp, info):
    """(covered mask, windows to fetch: cid, check, t) - only windows of checkpoints with a side in a cell."""
    cov = fetch_cover(cp, info)
    need = cp[~cov & in_any_cell(cp["p_yes"])][["cid", "kind", "check", "t"]].reset_index(drop=True)
    return cov, need


def fetch_windows(http, need, mk, out, workers=4, log=print, save_every=500):
    """Every trade of [t, t + 59] (data-api, end inclusive) of each window in `need`; kept in
    <out>/extra_trades.parquet with <out>/extra_info.parquet (one row per window: pages, truncated, n).
    Windows already in the info table are not asked again."""
    out = Path(out)
    tp, ip = out / "extra_trades.parquet", out / "extra_info.parquet"
    old_t = pd.read_parquet(tp) if tp.exists() else None
    old_i = pd.read_parquet(ip) if ip.exists() else pd.DataFrame(columns=["cid", "check", "t"])
    done = set(zip(old_i["cid"], old_i["check"].astype(int)))
    todo = need[[(c, int(k)) not in done for c, k in zip(need["cid"], need["check"])]]
    tok = mk.drop_duplicates("cid").set_index("cid")[["yes_token", "no_token"]]
    get = lambda url: http.get(url, slim=rs.slim_trades)
    log(f"  {len(todo):,} windows to fetch ({len(done):,} done)")
    frames = [old_t] if old_t is not None and len(old_t) else []
    infos = [old_i] if len(old_i) else []
    t0, lock, n = time.time(), threading.Lock(), [0]

    def one(r):
        try:
            recs, info = rs.fetch_trades(get, r.cid, int(r.t), end=int(r.t) + 59)
        except Exception as e:  # recorded as missing; a rerun asks again
            log(f"  {r.cid} {r.check}: {type(e).__name__}: {e}")
            return None
        y, no = tok.loc[r.cid, "yes_token"], tok.loc[r.cid, "no_token"]
        f = rs.trade_frame(r.cid, recs, y, no)
        f["check"] = int(r.check)
        with lock:
            n[0] += 1
            if n[0] % 500 == 0:
                log(f"  {n[0]:,}/{len(todo):,} windows, {http.calls:,} requests, {time.time() - t0:.0f} s")
        return f, {"cid": r.cid, "check": int(r.check), "t": int(r.t), "pages": info["pages"],
                   "truncated": bool(info["truncated"]), "n": len(recs)}

    def save():
        if infos:
            pd.concat(infos, ignore_index=True).to_parquet(ip)
            fr = [f for f in frames if len(f)]
            (pd.concat(fr, ignore_index=True) if fr else rs.trade_frame("", [], "", "").assign(check=0)).to_parquet(tp)

    rows = list(todo.itertuples(index=False))
    with ThreadPoolExecutor(workers) as ex:
        for k, res in enumerate(ex.map(one, rows), 1):
            if res is None:
                continue
            frames.append(res[0])
            infos.append(pd.DataFrame([res[1]]))
            if k % save_every == 0:
                save()
    save()
    tr = pd.read_parquet(tp) if tp.exists() else rs.trade_frame("", [], "", "").assign(check=0)
    inf = pd.read_parquet(ip) if ip.exists() else pd.DataFrame(columns=["cid", "check", "t", "pages", "truncated", "n"])
    return tr, inf


def window_trades(trades, cp):
    """Trades with t <= ts < t + 60 of each checkpoint row of cp (adds 'check'); the windows of one market
    do not overlap (checkpoints >= 1 minute apart), so check = ceil((anchor - ts) / 60)."""
    cols = ["cid", "ts", "taker_buy", "is_yes", "price", "size", "check"]
    if not len(cp) or not len(trades):
        return pd.DataFrame(columns=cols)
    anchor = cp.groupby("cid")["anchor"].first()
    tr = trades[trades["cid"].isin(anchor.index)]
    a = anchor.reindex(tr["cid"]).to_numpy(float)
    c = np.ceil((a - tr["ts"].to_numpy(float)) / 60.0)
    keep = np.isin(c, cp["check"].unique())
    tr = tr[keep].assign(check=c[keep].astype(int))
    j = tr.merge(cp[["cid", "check", "t"]], on=["cid", "check"], how="inner")
    j = j[(j["ts"] >= j["t"]) & (j["ts"] < j["t"] + 60)]
    return j[cols].reset_index(drop=True)


def segment_trades(cp, cached, info, extra):
    """Trades of every checkpoint window: from the segment's cached trades where its fetch covered the
    window, else from the windows fetched here (a window in neither has no trades: it was not needed)."""
    cov = fetch_cover(cp, info)
    a = window_trades(cached, cp[cov])
    if extra is not None and len(extra):
        b = window_trades(extra.drop(columns=["check"], errors="ignore"), cp[~cov])
    else:
        b = a.iloc[:0]
    return pd.concat([a, b], ignore_index=True)


# ===================================================================== rows

def side_rows(cp, wt, mk, fee=FEE):
    """Two rows per checkpoint (Yes: q = p_yes, result = official; No: q = 1 - p_yes, result = 1 - official),
    with that side's trades of [t, t + 60): trades, shares, cost, p = cost / shares (share-weighted), and
    the cell keys qb / tb / type, pnl (maker, result - p), pnl_taker (- fee p (1 - p)), gap = q - p, the
    ET day of t."""
    off = mk.drop_duplicates("cid").set_index("cid")["official"]
    g = (wt.assign(cost=wt["price"] * wt["size"])
         .groupby(["cid", "check", "is_yes"]).agg(trades=("size", "size"), shares=("size", "sum"), cost=("cost", "sum"))
         .reset_index()) if len(wt) else pd.DataFrame(columns=["cid", "check", "is_yes", "trades", "shares", "cost"])
    parts = []
    for side in (True, False):
        r = cp[["cid", "kind", "check", "t", "p_yes"]].copy()
        o = off.reindex(r["cid"]).to_numpy(float)
        r["is_yes"] = side
        r["q"] = r["p_yes"] if side else 1.0 - r["p_yes"]
        r["result"] = o if side else 1.0 - o
        parts.append(r)
    rows = pd.concat(parts, ignore_index=True)
    g = g.astype({"check": int, "is_yes": bool})
    rows = rows.merge(g, on=["cid", "check", "is_yes"], how="left")
    rows[["trades", "shares", "cost"]] = rows[["trades", "shares", "cost"]].fillna(0.0)
    rows["trades"] = rows["trades"].astype(int)
    with np.errstate(all="ignore"):
        rows["p"] = np.where(rows["shares"] > 0, rows["cost"] / rows["shares"].where(rows["shares"] > 0), NAN)
    rows["qb"] = q_bucket(rows["q"])
    rows["tb"] = t_bin(rows["check"])
    rows["type"] = rows["kind"].map(TYPE_OF)
    rows["pnl"] = rows["result"] - rows["p"]
    rows["pnl_taker"] = rows["pnl"] - fee * rows["p"] * (1.0 - rows["p"])
    rows["gap"] = rows["q"] - rows["p"]
    rows["day"] = rs.et_day(pd.Series(rows["t"].to_numpy(np.int64))).to_numpy() if len(rows) else []
    return rows


# ===================================================================== statistics

def one_sided_p(t, G):
    """P(T > t) for Student t with G - 1 degrees of freedom (NaN without a t or with < 2 clusters)."""
    if not (np.isfinite(t) and np.isfinite(G)) or G < 2:
        return NAN
    return float(student_t.sf(t, G - 1))


def stats(r, days):
    """Cell statistics of rows r (only rows with trades count), per CALIB.md, with units:
    shares / cost $ / profit $ per day = totals / days (ceilings), pnl in $ per share."""
    s = r[r["shares"] > 0]
    mu, se, t, G = rs.clustered_mean(s["pnl"], s["shares"], s["cid"])
    mu_t, _, t_t, _ = rs.clustered_mean(s["pnl_taker"], s["shares"], s["cid"])
    _, _, t_d, Gd = rs.clustered_mean(s["pnl"], s["shares"], s["day"])
    W = float(s["shares"].sum())
    per_mk = (s["pnl"] * s["shares"]).groupby(s["cid"]).sum()
    wavg = lambda x: float((s[x] * s["shares"]).sum() / W) if W > 0 else NAN
    return {"points": len(r), "mk_points": int(r["cid"].nunique()), "rows_tr": len(s), "markets": int(G),
            "losing": int((per_mk < 0).sum()), "trades": int(s["trades"].sum()), "shares": W,
            "shares_day": W / days, "cost_day": float(s["cost"].sum()) / days,
            "profit_day": float((s["pnl"] * s["shares"]).sum()) / days,
            "profit_taker_day": float((s["pnl_taker"] * s["shares"]).sum()) / days,
            "pnl": mu, "se": se, "t": t, "p1": one_sided_p(t, G), "pnl_taker": mu_t, "t_taker": t_t,
            "worst": float(per_mk.min()) if len(per_mk) else NAN, "t_day": t_d, "days_cl": int(Gd),
            "q": wavg("q"), "p": wavg("p"), "result": wavg("result"), "gap": wavg("gap")}


def cell_table(rows, days, cells=None, types=TYPES):
    """One row of statistics per cell (qb, tb, type); every requested cell, empty ones too."""
    cells = all_cells(types) if cells is None else list(cells)
    r = rows[rows["qb"].notna() & rows["type"].isin({c[2] for c in cells})]
    groups = {k: g for k, g in r.groupby(["qb", "tb", "type"])}
    empty = r.iloc[:0]
    out = []
    for qb, tb, ty in cells:
        out.append({"qb": qb, "tb": tb, "type": ty, **stats(groups.get((qb, tb, ty), empty), days)})
    return pd.DataFrame(out)


def select_candidates(tab_d, n_cells=NCELLS, alpha=ALPHA, min_mk=MIN_MK):
    """D's candidates: maker pnl > 0, one-sided p < alpha / n_cells (Bonferroni), >= min_mk markets with
    trades. Uses D's table only."""
    ok = (tab_d["pnl"] > 0) & (tab_d["p1"] < alpha / n_cells) & (tab_d["markets"] >= min_mk)
    c = tab_d[ok.fillna(False).astype(bool)].copy()
    c["used"] = c["qb"] == USED
    return c.reset_index(drop=True)


def evaluate_v(rows_v, days_v, cand, alpha=ALPHA, min_mk=MIN_MK):
    """(V table of D's candidate cells only, k): k = candidates outside the already-used band; a new cell
    passes iff maker pnl > 0, one-sided p < alpha / k and >= min_mk markets with trades on V. Used-band
    candidates are computed and marked, never a pass."""
    used = cand["qb"] == USED if len(cand) else pd.Series(dtype=bool)
    k = int((~used).sum()) if len(cand) else 0
    if not len(cand):
        return pd.DataFrame(columns=["qb", "tb", "type", "used", "pass"]), 0
    tab = cell_table(rows_v, days_v, cells=list(zip(cand["qb"], cand["tb"], cand["type"])),
                     types=tuple(cand["type"].unique()))
    tab["used"] = tab["qb"] == USED
    thr = alpha / k if k else NAN
    ok = (tab["pnl"] > 0) & (tab["p1"] < thr) & (tab["markets"] >= min_mk) & ~tab["used"]
    tab["pass"] = ok.fillna(False).astype(bool)
    tab["thr"] = thr
    return tab, k


# ===================================================================== gap rule

def gap_universe(rows, drop_used=False):
    u = rows[rows["type"].isin(TYPES) & rows["qb"].notna() & (rows["shares"] > 0)]
    return u[u["qb"] != USED] if drop_used else u


def gap_rows(rows, m, drop_used=False):
    """Rows of the cells' universe whose side the model puts at least m above its price: q - p >= m."""
    u = gap_universe(rows, drop_used)
    return u[u["q"] - u["p"] >= m]


def choose_m(rows_d, days_d, gaps=GAPS, alpha=ALPHA, min_mk=MIN_MK):
    """(m, D table of every m, D gate passed): m = the largest market-clustered t on D (ties: larger m).
    The D gate: maker pnl > 0, one-sided p < alpha / len(gaps), >= min_mk markets. D rows only."""
    tab = pd.DataFrame([{"m": m, **stats(gap_rows(rows_d, m), days_d)} for m in gaps])
    key = [(tt if np.isfinite(tt) else -np.inf, m) for tt, m in zip(tab["t"], tab["m"])]
    m = max(key)[1]
    r = tab[tab["m"] == m].iloc[0]
    ok = bool(r["pnl"] > 0 and r["p1"] < alpha / len(gaps) and r["markets"] >= min_mk)
    return float(m), tab, ok


def gap_test(rows_v, days_v, m, alpha=ALPHA, min_mk=MIN_MK, drop_used=False):
    """V once at D's m: (stats, passed) with maker pnl > 0, one-sided p < alpha, >= min_mk markets."""
    s = stats(gap_rows(rows_v, m, drop_used), days_v)
    ok = bool(np.isfinite(s["pnl"]) and s["pnl"] > 0 and np.isfinite(s["p1"]) and s["p1"] < alpha
              and s["markets"] >= min_mk)
    return s, ok


# ===================================================================== segment assembly

def segment_rows(seg, cp, http=None, out=CACHE, log=print):
    """(rows, info dict) of a segment: fetch the windows not covered by the cached trades (if http), join
    trades, build both-side rows."""
    src, d = SEGS[seg]["src"], Path(out) / seg
    mk = pd.read_parquet(src / "decided.parquet")
    info = pd.read_parquet(src / "fetch_info.parquet")
    cov, need = fetch_plan(cp, info)
    if http is not None:
        extra, einf = fetch_windows(http, need, mk, d, log=log)
    else:
        tp, ip = d / "extra_trades.parquet", d / "extra_info.parquet"
        extra = pd.read_parquet(tp) if tp.exists() else None
        einf = pd.read_parquet(ip) if ip.exists() else pd.DataFrame(columns=["cid", "check", "truncated", "n"])
    got = set(zip(einf["cid"], einf["check"].astype(int))) if len(einf) else set()
    missing = sum((c, int(k)) not in got for c, k in zip(need["cid"], need["check"]))
    cached = pd.read_parquet(src / "trades.parquet", columns=["cid", "ts", "taker_buy", "is_yes", "price", "size"])
    wt = segment_trades(cp, cached, info, extra)
    del cached
    gc.collect()
    rows = side_rows(cp, wt, mk)
    meta = {"markets": int(mk["excluded"].eq("").sum()), "cp_rows": len(cp), "covered": int(cov.sum()),
            "need": len(need), "fetched": len(got), "missing": int(missing),
            "trunc": int(einf["truncated"].astype(bool).sum()) if len(einf) else 0,
            "trunc_cached": int(info["truncated"].astype(bool).sum()),
            "window_trades": len(wt), "days": len(rs.days(SEGS[seg]["first"], SEGS[seg]["last"]))}
    return rows, meta


# ===================================================================== report

def _c(x, nd=2):
    """Dollars per share as cents."""
    return "–" if x is None or not np.isfinite(x) else f"{100 * x:+.{nd}f}"


def _t(t, G):
    return "–" if not (np.isfinite(t) and G >= rs.MIN_G) else f"{t:.2f}"


def _n(x, nd=0):
    return "–" if x is None or not np.isfinite(x) else f"{x:,.{nd}f}"


def _p(p):
    if p is None or not np.isfinite(p):
        return "–"
    return f"{p:.1e}" if p < 1e-3 else f"{p:.3f}"


def cell_name(qb, tb, ty):
    return f"{TYPE_CN[ty]} · {TB_CN[tb]} · q {qb}"


STAT_HEADS = ["段", "有成交市场", "输的市场", "笔数", "份/天", "花费 $/天", "利润 $/天", "挂单 ¢/份", "t", "吃单 ¢/份", "t",
              "q−p ¢", "最大单市场亏损 $", "单侧 p", "日聚类 t"]


def stat_cells(seg, r):
    return [seg, f"{int(r['markets'])}", f"{int(r['losing'])}", f"{int(r['trades']):,}", _n(r["shares_day"]),
            _n(r["cost_day"]), _n(r["profit_day"], 1), _c(r["pnl"]), _t(r["t"], r["markets"]), _c(r["pnl_taker"]),
            _t(r["t_taker"], r["markets"]), _c(r["gap"], 1), _n(r["worst"]), _p(r["p1"]), _t(r["t_day"], r["days_cl"])]


def md(heads, body):
    return "\n".join(["| " + " | ".join(heads) + " |", "|" + "---|" * len(heads)]
                     + ["| " + " | ".join(str(x) for x in b) + " |" for b in body])


def gap_map(tab, cand):
    """q - p (cents, share-weighted) of every D cell: rows = type x time bin, columns = probability bucket;
    '*' = D candidate, '(…)' = fewer than MIN_MK markets with trades, '·' = no trades."""
    cset = set(zip(cand["qb"], cand["tb"], cand["type"])) if len(cand) else set()
    t = tab.set_index(["qb", "tb", "type"])
    body = []
    for ty, tb in product(TYPES, TBINS):
        line = [TYPE_CN[ty], TB_CN[tb]]
        for qb in QLAB:
            r = t.loc[(qb, tb, ty)]
            if r["markets"] == 0:
                line.append("·")
                continue
            v = f"{100 * r['gap']:+.1f}"
            v = v + "*" if (qb, tb, ty) in cset else v
            line.append(v if r["markets"] >= MIN_MK else f"({v})")
        body.append(line)
    return md(["类型", "剩余"] + list(QLAB), body)


def report(ctx, out_md):
    D, V = ctx["D"], ctx["V"]
    tab, cand, vtab, k = ctx["tab_d"], ctx["cand"], ctx["vtab"], ctx["k"]
    L = ["# BTC 慢盘校准地图（CALIB.md）\n"]
    # ---------------------------------------------------------------- verdict
    new = cand[~cand["used"]] if len(cand) else cand
    used_c = cand[cand["used"]] if len(cand) else cand
    passed = vtab[vtab["pass"]] if len(vtab) else vtab
    L.append("## 判定\n")
    L.append(f"- **D 候选**（每份 > 0、单侧 p < 0.05/128 = {ALPHA / NCELLS:.1e}、≥ {MIN_MK} 个有成交市场）："
             f"{len(cand)} 格" + (f"，其中 {len(used_c)} 格在 0.95–0.99 档（已在 V 上用过，不计入 k）" if len(used_c) else "")
             + f"；新候选 k = {k}。D 上 ≥ {MIN_MK} 个有成交市场的格子 {int((tab['markets'] >= MIN_MK).sum())} / 128。")
    if k == 0:
        L.append("- **V**：没有新候选，V 上无可检验的格子 → **没有格子通过**。")
    else:
        names = "；".join(f"{cell_name(r.qb, r.tb, r.type)}（V 每份 {_c(r.pnl)}¢，单侧 p {_p(r.p1)}，{int(r.markets)} 个市场）"
                         for r in vtab[~vtab["used"]].itertuples(index=False))
        L.append(f"- **V**（每份 > 0、单侧 p < 0.05/{k} = {ALPHA / k:.4f}、≥ {MIN_MK} 个有成交市场）：通过 {len(passed)} / {k} 格"
                 + (" → **" + "、".join(cell_name(r.qb, r.tb, r.type) for r in passed.itertuples(index=False)) + " 通过**"
                    if len(passed) else " → **没有格子通过**") + f"。{names}")
        for r in passed.itertuples(index=False):
            if not (np.isfinite(r.pnl_taker) and r.pnl_taker > 0):
                L.append(f"  - {cell_name(r.qb, r.tb, r.type)}：按吃单扣费后 V 每份 {_c(r.pnl_taker)}¢ ≤ 0。")
    g = ctx["gap"]
    L.append(f"- **差规则**（q − p ≥ m，不分格）：D 选 m = {100 * g['m']:.0f}¢（D 上 t 最大）；D：每份 {_c(g['d']['pnl'])}¢，"
             f"t {_t(g['d']['t'], g['d']['markets'])}，单侧 p {_p(g['d']['p1'])}，{g['d']['markets']} 个市场 → "
             + ("过 D 关" if g["d_ok"] else "**D 关不过**（需每份 > 0、p < 0.05/3、≥ 30 个市场）")
             + f"；V 检验一次：每份 {_c(g['v']['pnl'])}¢，t {_t(g['v']['t'], g['v']['markets'])}，单侧 p {_p(g['v']['p1'])}，"
             f"{g['v']['markets']} 个市场 → " + ("**通过**" if g["d_ok"] and g["v_ok"] else "**不通过**")
             + (f"；去掉 0.95–0.99 档后 V 每份 {_c(g['v_x']['pnl'])}¢、单侧 p {_p(g['v_x']['p1'])} → "
                + ("仍通过" if g["v_x_ok"] else "不再通过") if g["d_ok"] and g["v_ok"] else "") + "。")
    L.append("- **0.95–0.99 档**：NEARCERT.md 已在 V 上检验过，这里只列出、标“已用过”，不算新发现。")
    if len(passed) or (g["d_ok"] and g["v_ok"]):
        L.append("- 通过的格子 / 规则 → 按 CALIB.md 做前向（阶梯录制，挂单在 q − 安全边际，成交价穿过才算）；是否小额实盘由用户决定。")
    L.append("")
    L.append(f"数据：D = 2026-03-14–09-30（{D['days']} 个 ET 日，resolved.py 的 {D['markets']:,} 个市场），"
             f"V = 2025-09-15–2026-03-13（{V['days']} 天，nearcert.py 的 {V['markets']:,} 个市场）。"
             f"检查点 240/120/60/30/10/5/1 分钟（D {D['cp_rows']:,}、V {V['cp_rows']:,} 个），每个两边各一行；"
             f"之后 60 秒内该边成交（任何主动方）按份数加权得 p。60–1 分钟的模型和两段缓存逐行一致"
             f"（最大 |Δq| D {ctx['chk']['D']['max_dq']:.1e}、V {ctx['chk']['V']['max_dq']:.1e}）。"
             f"240/120 分钟的窗口新抓 D {D['fetched']:,}、V {V['fetched']:,} 个（缺 {D['missing'] + V['missing']}，"
             f"截断 {D['trunc'] + V['trunc']}）。本次运行 {ctx['runtime'] / 60:.1f} 分钟；首次运行另有：重算检查点 D "
             f"{ctx['chk']['D']['seconds']:.0f} 秒、V {ctx['chk']['V']['seconds']:.0f} 秒"
             + (f"，抓取 {ctx['timing']['fetch_s'] / 60:.0f} 分钟、{ctx['timing']['requests']:,} 次请求（≤ 4 次/秒）"
                if ctx.get("timing") else "") + "。")
    L.append("")
    L.append("单位：份/天、花费 $/天（Σ p·份 ÷ 天数）、利润 $/天（Σ(结果 − p)·份 ÷ 天数，挂单无费）都是这一格**全市场合计，"
             "是上限**，不是一个买家能拿到的；每份 ¢ = 按份数加权的 结果 − p（吃单再扣 0.07·p(1 − p)）；t 按市场聚类"
             f"（少于 {rs.MIN_G} 个市场记“–”），单侧 p 用 G − 1 自由度的 t 分布；最大单市场亏损 = 一个市场在这一格的挂单净亏损（$）；"
             "日聚类 t 只作描述。\n")
    # ---------------------------------------------------------------- candidates
    L.append("## 候选格（D 选出，V 只算这些）\n")
    if not len(cand):
        L.append("D 上没有格子进入候选。D 上 t 最大的 5 格（都未过 Bonferroni / 市场数）：\n")
        top = tab[tab["markets"] > 0].sort_values("t", ascending=False).head(5)
        body = [[cell_name(r["qb"], r["tb"], r["type"])] + stat_cells("D", r) for _, r in top.iterrows()]
        L.append(md(["格子"] + STAT_HEADS, body))
    else:
        vt = vtab.set_index(["qb", "tb", "type"]) if len(vtab) else None
        body = []
        for _, r in cand.iterrows():
            name = cell_name(r["qb"], r["tb"], r["type"]) + ("（0.95–0.99：已在 V 上用过，不计入 k）" if r["used"] else "")
            body.append([name] + stat_cells("D", r) + ["候选"])
            v = vt.loc[(r["qb"], r["tb"], r["type"])]
            verdict = "已用过" if v["used"] else ("**通过**" if v["pass"] else "不通过")
            body.append([""] + stat_cells("V", v) + [verdict])
        L.append(md(["格子"] + STAT_HEADS + ["结果"], body))
        rest = tab[(tab["markets"] > 0) & ~tab.set_index(["qb", "tb", "type"]).index.isin(
            list(zip(cand["qb"], cand["tb"], cand["type"])))].sort_values("t", ascending=False).head(3)
        L.append("\nD 上未入选、t 最大的 3 格：" + "；".join(
            f"{cell_name(r.qb, r.tb, r.type)} 每份 {_c(r.pnl)}¢、t {_t(r.t, r.markets)}、单侧 p {_p(r.p1)}、{int(r.markets)} 个市场"
            for r in rest.itertuples(index=False)) + "。")
    L.append("")
    # ---------------------------------------------------------------- gap rule
    L.append("## 差规则 q − p ≥ m（不分格，四类币安结算、七个时点、q ∈ [0.02, 0.99)）\n")
    body = [[f"{100 * r['m']:.0f}¢" + ("（选中）" if r["m"] == g["m"] else "")] + stat_cells("D", r)
            for _, r in g["d_tab"].iterrows()]
    body.append([f"{100 * g['m']:.0f}¢"] + stat_cells("V", g["v"]))
    body.append([f"{100 * g['m']:.0f}¢，去掉 0.95–0.99"] + stat_cells("D", g["d_x"]))
    body.append([f"{100 * g['m']:.0f}¢，去掉 0.95–0.99"] + stat_cells("V", g["v_x"]))
    L.append(md(["m"] + STAT_HEADS, body))
    L.append("")
    # ---------------------------------------------------------------- used band
    L.append("## 0.95–0.99 档（已在 V 上用过，只列出）\n")
    body = []
    for name, key in (("60–1 分钟（NEARCERT.md 的定义）", "old"), ("七个时点", "all")):
        body.append([name] + stat_cells("D", ctx["used"]["D"][key]))
        body.append([""] + stat_cells("V", ctx["used"]["V"][key]))
    L.append(md(["0.95–0.99，四类合并"] + STAT_HEADS, body))
    L.append("")
    # ---------------------------------------------------------------- map
    L.append("## 校准地图：q − p（D，¢/份，按份数加权）\n")
    L.append("q = 模型概率，p = 之后 60 秒的成交均价；正 = 模型认为这一边比市场价更可能赢。`*` = D 候选，"
             f"括号 = 有成交市场少于 {MIN_MK} 个，`·` = 无成交。V 只算候选格，所以这里只有 D。\n")
    L.append(gap_map(tab, cand))
    L.append("")
    L.append("同一张表的 结果 − p（D，挂单 ¢/份，谁对了）：\n")
    t2 = tab.copy()
    t2["gap"] = t2["pnl"]
    L.append(gap_map(t2, cand))
    L.append("")
    # ---------------------------------------------------------------- control
    L.append("## 对照：4 小时涨跌（Chainlink 结算，不进任何判定；各时点合并）\n")
    body = []
    for qb in QLAB:
        line = [qb]
        for seg in ("D", "V"):
            r = ctx["control"][seg].set_index("qb").loc[qb]
            line += [f"{int(r['markets'])}", _c(r["gap"], 1), _c(r["pnl"]), _t(r["t"], r["markets"])]
        body.append(line)
    L.append(md(["q 档", "D 市场", "D q−p ¢", "D 每份 ¢", "D t", "V 市场", "V q−p ¢", "V 每份 ¢", "V t"], body))
    text = "\n".join(L) + "\n"
    Path(out_md).parent.mkdir(parents=True, exist_ok=True)
    Path(out_md).write_text(text)
    return text


# ===================================================================== main

def control_table(rows, days):
    r = rows[rows["type"] == CONTROL].assign(tb="all")
    return pd.DataFrame([{"qb": qb, **stats(r[r["qb"] == qb], days)} for qb in QLAB])


def used_band(rows, days):
    u = rows[rows["type"].isin(TYPES) & (rows["qb"] == USED)]
    return {"old": stats(u[u["check"].isin(OLD_CHECKS)], days), "all": stats(u, days)}


def run(args, log=print):
    t0 = time.time()
    out = Path(args.cache)
    out.mkdir(parents=True, exist_ok=True)
    cps, chk = {}, {}
    for seg in ("D", "V"):          # one segment's 1 s klines in memory at a time
        cps[seg], chk[seg] = points(seg, out, log=log)
        gc.collect()
    if args.step == "points":
        return
    http = rs.Http(out, rate=args.rate) if args.step in ("all", "fetch") else None
    t_fetch = time.time()
    rows, meta = {}, {}
    for seg in ("D", "V"):
        rows[seg], meta[seg] = segment_rows(seg, cps[seg], http=http, out=out, log=log)
        log(f"{seg}: {meta[seg]}")
    if http is not None:
        log(f"requests {http.calls:,} (cache hits {http.hits:,})")
        if http.calls > 50:
            (out / "timing.json").write_text(json.dumps({"fetch_s": time.time() - t_fetch, "requests": http.calls}))
    if args.step == "fetch":
        return
    dD, dV = meta["D"]["days"], meta["V"]["days"]
    tab_d = cell_table(rows["D"], dD)
    cand = select_candidates(tab_d)
    vtab, k = evaluate_v(rows["V"], dV, cand)
    m, d_tab, d_ok = choose_m(rows["D"], dD)
    v, v_ok = gap_test(rows["V"], dV, m)
    v_x, v_x_ok = gap_test(rows["V"], dV, m, drop_used=True)
    gap = {"m": m, "d_tab": d_tab, "d_ok": d_ok, "d": d_tab[d_tab["m"] == m].iloc[0].to_dict(), "v": v, "v_ok": v_ok,
           "d_x": stats(gap_rows(rows["D"], m, drop_used=True), dD), "v_x": v_x, "v_x_ok": v_x_ok}
    ctx = {"D": meta["D"], "V": meta["V"], "chk": chk, "tab_d": tab_d, "cand": cand, "vtab": vtab, "k": k, "gap": gap,
           "used": {s: used_band(rows[s], meta[s]["days"]) for s in ("D", "V")},
           "control": {s: control_table(rows[s], meta[s]["days"]) for s in ("D", "V")}}
    tab_d.to_parquet(out / "cells_D.parquet")
    if len(vtab):
        vtab.to_parquet(out / "cells_V_candidates.parquet")
    ctx["runtime"] = time.time() - t0
    tj = out / "timing.json"
    ctx["timing"] = json.loads(tj.read_text()) if tj.exists() else None
    text = report(ctx, args.out)
    log(text)
    log(f"runtime {time.time() - t0:.0f} s")
    return ctx


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", nargs="?", default="all", choices=["all", "points", "fetch", "report"])
    ap.add_argument("--cache", default=str(CACHE))
    ap.add_argument("--out", default=str(HERE / "real" / "calib.md"))
    ap.add_argument("--rate", type=float, default=RATE)
    args = ap.parse_args(argv)
    run(args)


if __name__ == "__main__":
    main()
