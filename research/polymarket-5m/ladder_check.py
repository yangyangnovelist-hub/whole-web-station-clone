"""Stale quotes on Polymarket's undelayed BTC markets after a Binance jump (market data only: no
keys, no orders). Reads the directories ladder.py build writes.

    python ladder_check.py DIR [DIR ...] [--out real/ladder-check.md]

DIR is a built directory (books.csv.gz, markets.json, binance_trades.jsonl.gz) or any directory
above some; each recording is analysed on its own (its own Binance trades, sigma and books), then
pooled.

Jumps: the trigger family of latency.gated_trades, over the whole recording: a Binance print whose
log price moved more than 2 sigma from the last print 1..5 s earlier, sigma the std of 1 s log
returns over the trailing 600 s (latency.spot_grid), the first of each 2 s episode.

Model (b), driftless log-normal with that sigma (per second) over the time left tau:
- above K:  Phi(ln(S/K) / (sigma sqrt(tau)));  range [a, b): above(a) - above(b);
- hit_up K: 1 if the day's prints already reached K, else 2 (1 - Phi(ln(K/S) / (sigma sqrt(tau))))
  (reflection principle); hit_down the mirror image;
- updown_day / updown_4h: above(reference price), skipped while the reference is unknown.
The jump's fair-value change on a market is the model at the jump print minus the model at the
reference print 1..5 s before it (same sigma and tau); the token whose value rose is "favoured".

(a) reaction: per (jump, market) with a change of at least 0.5c, seconds until the favoured token's
    mid first moves one tick its way, and the share of its 5 s move already shown at +0.1..2 s
    (as recorder_check), by market type and by size of the change;
(c) taker markouts with no hold: where the change is >= 3c, buy the favoured token at its ask at
    t0 + L if fair - ask - fee >= theta, fair being the model ("model") or the token's mid 2 s
    before the print plus the model's change since ("anchored"); valued at the mid 30 s and 120 s
    after the print, minus ask and fee (a markout, not settlement P&L), standard errors clustered by
    token and by jump;
(d) barrier events: the first Binance print at or beyond a hit market's level while it is still
    open; the Yes ask over the next 5 s, how long it stays below 0.99 and what is offered there.
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

import binary as bo
import ladder as ld
import latency as lt

HS = (0.1, 0.2, 0.3, 0.5, 1.0, 2.0)
HORIZON, GAP, ANCHOR = 5.0, 2.0, 2.0
LAGS = (0.1, 0.15, 0.2, 0.3, 0.5, 1.0)
THETAS = (0.02, 0.05)
MIN_CHANGE = 0.03          # (c): the jump's fair-value change on the token
REACT_MIN = 0.005          # (a): smaller changes are counted, not traced
BUCKETS = ((0.005, 0.01), (0.01, 0.03), (0.03, 0.10), (0.10, 1.01))
MARKOUTS = (30.0, 120.0)
MIN_TAU = 60.0             # leave the last minute (the settlement candle) out
CLEAN = (10.0, MARKOUTS[-1] + 10.0)  # no CLOB disconnect in [t0 - 10 s, t0 + 130 s]
BARRIER_OFFS = (-1.0, 0.0, 0.1, 0.2, 0.3, 0.5, 1.0, 2.0, 5.0)
BARRIER_WAIT = 60.0
CAP = ld.DEPTH_CAP
FEE = bo.CRYPTO_FEE_RATE
UPDOWN = ("updown_day", "updown_4h")
TYPE_NAMES = {"above": "above（高于）", "range": "range（区间）", "hit_up": "hit_up（触及↑）",
              "hit_down": "hit_down（触及↓）", "updown_day": "updown_day（日涨跌）", "updown_4h": "updown_4h（4 小时）"}


# ------------------------------------------------------------------ model (b)

def p_above(S, K, sd):
    """P(S_T > K) for a driftless log-normal with median S and log std sd."""
    with np.errstate(divide="ignore", invalid="ignore"):
        return norm.cdf(np.log(np.asarray(S, float) / K) / np.asarray(sd, float))


def fair_yes(typ, S, sd, lo=np.nan, hi=np.nan, ref=np.nan):
    """Model value of the Yes (Up) token: see the module notes. S, sd may be arrays; lo / hi are
    the strike or range bounds (NaN for an open end), ref the up/down reference price."""
    S = np.asarray(S, float)
    if typ == "above":
        return p_above(S, lo, sd)
    if typ == "range":
        a = 1.0 if not np.isfinite(lo) else p_above(S, lo, sd)
        b = 0.0 if not np.isfinite(hi) else p_above(S, hi, sd)
        return np.asarray(a - b, float) * np.ones_like(S)
    if typ == "hit_up":
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(S >= lo, 1.0, np.minimum(1.0, 2.0 * norm.cdf(np.log(S / lo) / sd)))
    if typ == "hit_down":
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(S <= hi, 1.0, np.minimum(1.0, 2.0 * norm.cdf(np.log(hi / S) / sd)))
    if typ in UPDOWN:
        return p_above(S, ref, sd)
    raise ValueError(typ)


# ------------------------------------------------------------------ inputs

def jumps(spot, z0=lt.G_Z0, gap=GAP):
    """Binance jump prints (see the module notes): t0, the print's index i and price s0, the
    reference print's price s_ref, the log move dx and sigma (per second, latency.spot_grid)."""
    cols = ["t0", "i", "s0", "s_ref", "dx", "sigma"]
    if len(spot) < 2:
        return pd.DataFrame(columns=cols)
    _, sigma = lt.spot_grid(spot, max_gap=lt.C_MAX_GAP)
    ts = spot["trade_ts"].to_numpy(float)
    px = spot["price"].to_numpy(float)
    lp = np.log(px)
    j = np.searchsorted(ts, ts - 1.0, "right") - 1
    jj = np.maximum(j, 0)
    ok = (j >= 0) & (ts - ts[jj] <= lt.C_REF_AGE)
    dx = np.where(ok, lp - lp[jj], np.nan)
    sg = sigma.reindex(np.floor(ts).astype("int64") - 1).to_numpy()
    with np.errstate(invalid="ignore"):
        cand = np.flatnonzero(np.abs(dx) > z0 * sg)
    keep, nxt = [], -np.inf
    for i in cand:
        if ts[i] < nxt:
            continue
        nxt = ts[i] + gap
        keep.append(i)
    keep = np.asarray(keep, dtype=int)
    return pd.DataFrame({"t0": ts[keep], "i": keep, "s0": px[keep], "s_ref": px[jj[keep]], "dx": dx[keep],
                         "sigma": sg[keep]}, columns=cols)


class Tokens:
    """Per-token book arrays (exchange time) with vectorised as-of lookups."""

    COLS = ("bid", "ask", "bid_size", "ask_size", "tick", "depth99", "edge99")

    def __init__(self, books):
        self.by = {}
        for tok, g in books.groupby("market_id", sort=False):
            a = {"ts": g["ts"].to_numpy(float)}
            for c in self.COLS:
                a[c] = g[c].to_numpy(float) if c in g else np.full(len(g), np.nan)
            a["mid"] = (a["bid"] + a["ask"]) / 2
            self.by[str(tok)] = a
        self.all_ts = np.sort(books["ts"].to_numpy(float)) if len(books) else np.array([])

    def asof(self, tok, t, col):
        a = self.by[tok]
        i = np.searchsorted(a["ts"], np.asarray(t, float), "right") - 1
        return np.where(i >= 0, a[col][np.maximum(i, 0)], np.nan)

    def first_move(self, tok, t0, base, step, horizon=HORIZON):
        """Seconds after each t0 until the mid first reaches base + step (NaN if not within horizon)."""
        a = self.by[tok]
        i0 = np.searchsorted(a["ts"], t0, "right")
        i1 = np.searchsorted(a["ts"], np.asarray(t0) + horizon, "right")
        out = np.full(len(t0), np.nan)
        for k in range(len(t0)):
            if not np.isfinite(base[k]):
                continue
            hit = np.flatnonzero(a["mid"][i0[k]:i1[k]] >= base[k] + step[k] - 1e-9)
            if len(hit):
                out[k] = a["ts"][i0[k] + hit[0]] - t0[k]
        return out


def clean_jumps(t0, closes, all_ts, before=CLEAN[0], after=CLEAN[1]):
    """Jumps whose surroundings the recorder saw: no CLOB disconnect (recorder clock) within
    [t0 - before, t0 + after], a book row of any token within `before` s up to t0, and the recording
    still running at t0 + after - 10 (so the 120 s markout is a real quote)."""
    t0 = np.asarray(t0, float)
    ok = np.ones(len(t0), bool)
    if closes is not None and len(closes):
        c = np.sort(np.asarray(closes, float))
        ok &= np.searchsorted(c, t0 - before, "left") == np.searchsorted(c, t0 + after, "right")
    if not len(all_ts):
        return ok & False
    i = np.searchsorted(all_ts, t0, "right") - 1
    ok &= (i >= 0) & (t0 - all_ts[np.maximum(i, 0)] <= before)
    ok &= all_ts[-1] >= t0 + after - 10.0
    return ok


def _ref_from_spot(ts, px, ref_ts, max_age=5.0):
    """Last print at or before ref_ts (the 1m candle's close), if at most max_age s old."""
    i = np.searchsorted(ts, ref_ts, "right") - 1
    return float(px[i]) if i >= 0 and ref_ts - ts[i] <= max_age else np.nan


# ------------------------------------------------------------------ (a) and (c)

def pair_rows(js, markets, toks, spot, ok):
    """One row per (jump, market) whose model change is at least REACT_MIN, traced on the favoured
    token; plus the number of pairs below REACT_MIN per type."""
    ts = spot["trade_ts"].to_numpy(float)
    px = spot["price"].to_numpy(float)
    t0 = js["t0"].to_numpy(float)
    i0 = js["i"].to_numpy(int)
    sig = js["sigma"].to_numpy(float)
    ja = np.searchsorted(ts, t0 - ANCHOR, "right") - 1
    s_m2 = np.where((ja >= 0) & (t0 - ANCHOR - ts[np.maximum(ja, 0)] <= lt.C_REF_AGE), px[np.maximum(ja, 0)], np.nan)
    parts, small = [], {}
    for m in markets.itertuples(index=False):
        typ = m.type
        if typ not in ld.TYPES:
            continue
        tau = m.end_ts - t0
        sel = ok & (tau >= MIN_TAU) & np.isfinite(sig)
        ref = np.nan
        if typ in ("hit_up", "hit_down"):
            start = m.start_ts if np.isfinite(m.start_ts) else -np.inf
            a = np.searchsorted(ts, start, "left")
            sel &= (t0 >= start) & (i0 > a)
            seg = px[a:]
            ext = np.maximum.accumulate(seg) if typ == "hit_up" else np.minimum.accumulate(seg)
            before = np.where(i0 - 1 - a >= 0, ext[np.clip(i0 - 1 - a, 0, max(len(ext) - 1, 0))], np.nan) \
                if len(seg) else np.full(len(t0), np.nan)
            reached = before >= m.lo if typ == "hit_up" else before <= m.hi
            sel &= ~reached  # the day's prints had already settled it
        elif typ in UPDOWN:
            ref = m.ref_price if np.isfinite(m.ref_price) else _ref_from_spot(ts, px, m.ref_ts)
            if not np.isfinite(ref):
                continue
            sel &= t0 >= m.ref_ts
        if not sel.any():
            continue
        sd = sig * np.sqrt(np.maximum(tau, 0))
        f0 = fair_yes(typ, js["s_ref"].to_numpy(float), sd, m.lo, m.hi, ref)
        f1 = fair_yes(typ, js["s0"].to_numpy(float), sd, m.lo, m.hi, ref)
        f2 = fair_yes(typ, s_m2, sd, m.lo, m.hi, ref)
        ch = f1 - f0
        good = sel & np.isfinite(ch)
        small[typ] = small.get(typ, 0) + int((good & (np.abs(ch) < REACT_MIN)).sum())
        k = np.flatnonzero(good & (np.abs(ch) >= REACT_MIN))
        for side, tok, mask in (("Yes", m.yes_token, ch[k] > 0), ("No", m.no_token, ch[k] < 0)):
            kk = k[mask]
            if not len(kk) or tok not in toks.by:
                continue
            tt = t0[kk]
            flip = (lambda f: f) if side == "Yes" else (lambda f: 1.0 - f)
            row = {"jump": tt, "cid": m.condition_id, "type": typ, "side": side, "token": tok, "tau": tau[kk],
                   "change": np.abs(ch[kk]), "fair0": flip(f0[kk]), "fair1": flip(f1[kk]), "fair_m2": flip(f2[kk]),
                   "mid0": toks.asof(tok, tt, "mid"), "mid_m2": toks.asof(tok, tt - ANCHOR, "mid"),
                   "ask0": toks.asof(tok, tt, "ask"), "tick": toks.asof(tok, tt, "tick")}
            mids = {h: toks.asof(tok, tt + h, "mid") for h in HS + (HORIZON,) + MARKOUTS}
            for h in HS:
                row[f"d{h:g}"] = mids[h] - row["mid0"]
            row["d_end"] = mids[HORIZON] - row["mid0"]
            step = np.where(np.isfinite(row["tick"]) & (row["tick"] > 0), row["tick"], 0.01)
            row["first"] = toks.first_move(tok, tt, row["mid0"], step)
            for h in MARKOUTS:
                row[f"mid{h:g}"] = mids[h]
            for lag in LAGS:
                row[f"ask{lag:g}"] = toks.asof(tok, tt + lag, "ask")
                row[f"size{lag:g}"] = toks.asof(tok, tt + lag, "ask_size")
            parts.append(pd.DataFrame(row))
    pairs = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    return pairs, small


def taker_rows(pairs, lags=LAGS, thetas=THETAS, min_change=MIN_CHANGE):
    """(c): one row per (pair, variant, theta, lag) where the favoured token's ask at t0 + lag was at
    least theta below the fair value after the taker fee."""
    if pairs.empty:
        return pd.DataFrame(columns=["variant", "theta", "lag", "jump", "token", "type", "ask", "size", "fee",
                                     "fair", "mark30", "mark120"])
    p = pairs[pairs["change"] >= min_change - 1e-12]
    out = []
    for variant in ("model", "anchored"):
        fair = p["fair1"] if variant == "model" else (p["mid_m2"] + p["fair1"] - p["fair_m2"]).clip(0.001, 0.999)
        for lag in lags:
            ask = p[f"ask{lag:g}"]
            fee = FEE * ask * (1 - ask)
            for theta in thetas:
                take = np.isfinite(ask) & (ask > 0) & (ask < 1) & np.isfinite(fair) & (fair - ask - fee >= theta - 1e-12)
                if not take.any():
                    continue
                q = p[take]
                out.append(pd.DataFrame({
                    "variant": variant, "theta": theta, "lag": lag, "jump": q["jump"], "token": q["token"],
                    "type": q["type"], "ask": ask[take], "size": q[f"size{lag:g}"], "fee": fee[take],
                    "fair": fair[take], **{f"mark{h:g}": q[f"mid{h:g}"] - ask[take] - fee[take] for h in MARKOUTS}}))
    if not out:
        return taker_rows(pd.DataFrame())
    return pd.concat(out, ignore_index=True)


# ------------------------------------------------------------------ (d)

def barrier_rows(markets, toks, spot, closes, trades):
    """(d): for each hit market, the first print at or beyond its level inside its window (not the
    window's first print in the recording, which may have been beyond it already): the Yes ask at
    BARRIER_OFFS, how long it stayed below CAP, the depth and dollars below CAP at the print, and
    the Yes token's trades below CAP within 5 s."""
    ts = spot["trade_ts"].to_numpy(float)
    px = spot["price"].to_numpy(float)
    rows = []
    for m in markets.itertuples(index=False):
        if m.type not in ("hit_up", "hit_down") or m.yes_token not in toks.by:
            continue
        start = m.start_ts if np.isfinite(m.start_ts) else -np.inf
        a, b = np.searchsorted(ts, [start, m.end_ts], "left")
        seg = px[a:b]
        hit = np.flatnonzero(seg >= m.lo) if m.type == "hit_up" else np.flatnonzero(seg <= m.hi)
        if not len(hit) or hit[0] == 0:
            continue
        t = float(ts[a + hit[0]])
        if closes is not None and len(closes) and \
                np.searchsorted(closes, t - 10, "left") != np.searchsorted(closes, t + 10, "right"):
            continue
        tok, arr = m.yes_token, toks.by[m.yes_token]
        i = np.searchsorted(arr["ts"], t, "right") - 1
        if i < 0:
            continue
        asks = toks.asof(tok, t + np.array(BARRIER_OFFS), "ask")
        a0 = arr["ask"][i]
        if not (np.isfinite(a0) and a0 < CAP - 1e-9):
            below = 0.0
        else:
            later = np.flatnonzero(~(arr["ask"][i + 1:] < CAP - 1e-9) & (arr["ts"][i + 1:] <= t + BARRIER_WAIT))
            below = float(arr["ts"][i + 1 + later[0]] - t) if len(later) else np.inf
        tr = trades[(trades["asset_id"] == tok) & (trades["ts"] >= t) & (trades["ts"] <= t + HORIZON)
                    & (trades["price"] < CAP - 1e-9)] if len(trades) else trades
        rows.append({"cid": m.condition_id, "type": m.type, "level": m.lo if m.type == "hit_up" else m.hi,
                     "t": t, "price": float(px[a + hit[0]]),
                     **{f"ask{o:+g}": v for o, v in zip(BARRIER_OFFS, asks)},
                     "below": below, "depth": arr["depth99"][i], "edge": arr["edge99"][i],
                     "trades": len(tr), "traded": float(tr["size"].sum()) if len(tr) else 0.0,
                     "first_trade": float(tr["ts"].min() - t) if len(tr) else np.nan})
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ one recording

def analyze(books, markets, spot, closes=None, trades=None):
    """Everything for one recording: dict with jumps, pairs (a), small (pairs below REACT_MIN by
    type), takers (c) and barriers (d)."""
    trades = pd.DataFrame(columns=["asset_id", "ts", "price", "size"]) if trades is None else trades
    toks = Tokens(books)
    js = jumps(spot)
    ok = clean_jumps(js["t0"].to_numpy(float), closes, toks.all_ts)
    pairs, small = pair_rows(js, markets, toks, spot, ok)
    return {"jumps": js, "clean": int(ok.sum()), "pairs": pairs, "small": small, "takers": taker_rows(pairs),
            "barriers": barrier_rows(markets, toks, spot, None if closes is None else np.sort(closes), trades),
            "markets": markets, "span": (float(spot["trade_ts"].iloc[0]), float(spot["trade_ts"].iloc[-1]))
            if len(spot) else (np.nan, np.nan)}


# ------------------------------------------------------------------ report

def reaction_stats(r):
    """Pairs, share that moved a tick within HORIZON, first-move median / 90% (s), response share at
    each h in HS (sum of the move by h over the sum of the 5 s move)."""
    r = r[np.isfinite(r["mid0"])] if len(r) else r  # a token with both sides quoted at the jump
    out = {"n": len(r)}
    if not len(r):
        return out
    out["moved"] = float(r["first"].notna().mean())
    f = r["first"].dropna()
    out["f50"], out["f90"] = (f.quantile(0.5), f.quantile(0.9)) if len(f) else (np.nan, np.nan)
    d = r[r["d_end"].notna()]
    den = d["d_end"].sum()
    for h in HS:
        out[f"r{h:g}"] = d[f"d{h:g}"].fillna(0).sum() / den if den > 0 else np.nan
    return out


def _pct(x):
    return f"{100 * x:.0f}%" if x is not None and np.isfinite(x) else "–"


def _reaction_line(name, s):
    if s["n"] == 0:
        return f"| {name} | 0 |" + " – |" * (2 + len(HS))
    first = f"{1000 * s['f50']:.0f} / {1000 * s['f90']:.0f}" if np.isfinite(s["f50"]) else "–"
    return f"| {name} | {s['n']:,} | {_pct(s['moved'])} | {first} | " + " | ".join(_pct(s[f"r{h:g}"]) for h in HS) + " |"


def _se_cell(t, col, by):
    """Mean (cents a share) of column `col` and its standard error clustered by column `by`."""
    x = pd.DataFrame({"pnl": t[col].to_numpy(float), "market_id": t[by].astype(str).to_numpy()}).dropna()
    if x.empty:
        return np.nan, np.nan, 0
    mean, se, _, k = lt.clustered(x, pd.Series(1.0, index=x.index))
    return mean, se, len(x)


def taker_table(t):
    L = ["| 公允价 | θ | 延迟 | 笔数 | 急动 | 代币 | 平均卖一 | 卖一量中位（份） | 30 s 标记 ¢（SE 代币 / 急动） | 120 s 标记 ¢（SE 代币 / 急动） |",
         "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for variant, vname in (("model", "模型"), ("anchored", "锚定中价")):
        for theta in THETAS:
            for lag in LAGS:
                q = t[(t["variant"] == variant) & (t["theta"] == theta) & (t["lag"] == lag)]
                head = f"| {vname} | {100 * theta:.0f}¢ | {1000 * lag:.0f} ms | {len(q):,} |"
                if q.empty:
                    L.append(head + " 0 | 0 | – | – | – | – |")
                    continue
                cells = []
                for h in MARKOUTS:
                    mt, st, _ = _se_cell(q, f"mark{h:g}", "token")
                    _, sj, _ = _se_cell(q, f"mark{h:g}", "jump")
                    cells.append(f"{mt:+.2f}（{st:.2f} / {sj:.2f}）" if np.isfinite(mt) else "–")
                L.append(head + f" {q['jump'].nunique():,} | {q['token'].nunique():,} | {q['ask'].mean():.3f} | "
                         f"{q['size'].median():,.0f} | " + " | ".join(cells) + " |")
    return L


def _secs(x):
    if not np.isfinite(x):
        return f"> {BARRIER_WAIT:.0f} s" if x == np.inf else "–"
    return f"{1000 * x:.0f} ms"


def barrier_table(b, last=20):
    cols = " | ".join(f"{o:+g}" for o in BARRIER_OFFS)
    L = [f"| 市场 | 触及（UTC） | 币安价 | Yes 卖一 @ {cols} s | 卖一 < {CAP:.2f} 持续 | < {CAP:.2f} 挂单（份） | 全吃可得 $ | 5 s 内 < {CAP:.2f} 成交（笔 / 份） |",
         "|---|---|---:|---|---:|---:|---:|---:|"]
    for d in b.sort_values("t").tail(last).to_dict("records"):
        path = " / ".join(f"{d[f'ask{o:+g}']:.3f}" if np.isfinite(d[f"ask{o:+g}"]) else "–" for o in BARRIER_OFFS)
        arrow = "↑" if d["type"] == "hit_up" else "↓"
        when = pd.Timestamp(d["t"], unit="s", tz="UTC").strftime("%m-%d %H:%M:%S.%f")[:-3]
        L.append(f"| {arrow} {d['level']:,.0f} | {when} | {d['price']:,.2f} | {path} | {_secs(d['below'])} | "
                 + (f"{d['depth']:,.0f}" if np.isfinite(d["depth"]) else "–") + " | "
                 + (f"{d['edge']:,.0f}" if np.isfinite(d["edge"]) else "–") + f" | {d['trades']} / {d['traded']:,.0f} |")
    return L


def report(results, names, notes=()):
    """Chinese markdown report over every recording's analyze() result."""
    pairs = pd.concat([r["pairs"].assign(run=n) for r, n in zip(results, names) if not r["pairs"].empty],
                      ignore_index=True) if any(not r["pairs"].empty for r in results) else pd.DataFrame()
    takers = pd.concat([r["takers"].assign(run=n) for r, n in zip(results, names)], ignore_index=True) \
        if results else taker_rows(pd.DataFrame())
    barriers = pd.concat([r["barriers"].assign(run=n) for r, n in zip(results, names) if not r["barriers"].empty],
                         ignore_index=True) if any(not r["barriers"].empty for r in results) else pd.DataFrame()
    small = {}
    for r in results:
        for k, v in r["small"].items():
            small[k] = small.get(k, 0) + v
    markets = pd.concat([r["markets"] for r in results], ignore_index=True).drop_duplicates("condition_id") \
        if results else pd.DataFrame(columns=["condition_id", "type"])
    hours = sum((r["span"][1] - r["span"][0]) / 3600 for r in results if np.isfinite(r["span"][0]))
    spans = [r["span"] for r in results if np.isfinite(r["span"][0])]
    when = (f"{pd.Timestamp(min(s[0] for s in spans), unit='s', tz='UTC'):%Y-%m-%d %H:%M} → "
            f"{pd.Timestamp(max(s[1] for s in spans), unit='s', tz='UTC'):%Y-%m-%d %H:%M} UTC") if spans else "–"
    nj = sum(len(r["jumps"]) for r in results)
    sig = pd.concat([r["jumps"]["sigma"] for r in results], ignore_index=True).dropna() if nj else pd.Series(dtype=float)
    vol = f"{100 * sig.median() * np.sqrt(365 * 86400):.0f}%" if len(sig) else "–"
    nc = sum(r["clean"] for r in results)
    by_type = markets["type"].value_counts() if len(markets) else pd.Series(dtype=int)
    mk = "，".join(f"{t} {by_type.get(t, 0)}" for t in ld.TYPES if by_type.get(t, 0))
    L = [f"# 无延迟 BTC 市场：币安急动之后的报价（{time.strftime('%Y-%m-%d %H:%M', time.gmtime())} UTC）", "",
         "只用市场数据，不下单。市场：每日 above / 价格区间 / 触及价 / 日涨跌（按币安 BTC/USDT 1 分钟 K 线结算）和 4 小时涨跌，"
         "都没有 150 ms 吃单延迟（/clob-markets 没有 itode）。", "",
         f"**样本**：录制段 {len(results)} 个，共 {hours:.1f} 小时（{when}）；市场 {len(markets)} 个（{mk or '–'}）；"
         f"币安 2σ 急动 {nj:,} 个（数据干净的 {nc:,} 个；急动时模型 σ 年化中位 {vol}）；急动 × 市场对（|Δ公允| ≥ {100 * REACT_MIN:.1f}¢）{len(pairs):,} 个，"
         f"另有 {sum(small.values()):,} 对 < {100 * REACT_MIN:.1f}¢ 只计数；触及事件 {len(barriers)} 个。"
         "样本小的格子只作参考。", ""]
    L += ["## (a) 反应：被看好代币的中间价多快朝模型方向动", "",
          f"首次变动 = 中间价第一次朝模型方向动 ≥ 1 跳（tick）的时间（{HORIZON:.0f} 秒内动了的那些，中位 / 90%）；"
          f"+h = 到 h 时已走完的 {HORIZON:.0f} 秒总变动的比例（对所有对求和后相除）。时间都是交易所时间戳。", "",
          "| 类型（\\|Δ公允\\| ≥ 1¢） | 对数 | 5 s 内动 1 跳 | 首次变动 ms | " + " | ".join(f"+{1000 * h:.0f} ms" for h in HS) + " |",
          "|---|---:|---:|---:|" + "---:|" * len(HS)]
    big = pairs[pairs["change"] >= 0.01] if len(pairs) else pairs
    for t in ld.TYPES:
        q = big[big["type"] == t] if len(big) else big
        if len(q):
            L.append(_reaction_line(TYPE_NAMES[t], reaction_stats(q)))
    if len(big):
        L.append(_reaction_line("合计", reaction_stats(big)))
    else:
        L.append("| （还没有） | 0 |" + " – |" * (2 + len(HS)))
    L += ["", "| \\|Δ公允\\| | 对数 | 5 s 内动 1 跳 | 首次变动 ms | " + " | ".join(f"+{1000 * h:.0f} ms" for h in HS) + " |",
          "|---|---:|---:|---:|" + "---:|" * len(HS)]
    L.append(f"| < {100 * REACT_MIN:.1f}¢ | {sum(small.values()):,} | 不追踪 |" + " |" * (1 + len(HS)))
    for lo, hi in BUCKETS:
        q = pairs[(pairs["change"] >= lo) & (pairs["change"] < hi)] if len(pairs) else pairs
        name = f"{100 * lo:g}–{100 * hi:g}¢" if hi < 1 else f"≥ {100 * lo:g}¢"
        L.append(_reaction_line(name, reaction_stats(q)))
    L += ["", f"## (c) 无持有期吃单：|Δ公允| ≥ {100 * MIN_CHANGE:.0f}¢ 时在 t0 + 延迟按卖一买入被看好的代币", "",
          "条件：公允价 − 卖一 − 吃单费 ≥ θ（费 = 0.07·p·(1−p)）。公允价「模型」= 急动那笔成交价下的模型值；"
          "「锚定中价」= 急动前 2 秒该代币的中间价 + 模型从那时到急动的变化。标记 = t0 + 30 s / 120 s 的中间价 − 卖一 − 费，"
          "单位 ¢/份；**标记不是结算盈亏**。括号里是按代币、按急动聚类的标准误（同一急动下各市场一起动，按急动聚类更保守）。", ""]
    L += taker_table(takers)
    L += ["", f"## (d) 触及事件：币安成交价第一次到达触及市场的价位（市场还没结算）", "",
          f"Yes 卖一从触及时刻起的路径（−1 s 到 +5 s）、卖一保持在 {CAP:.2f} 以下多久、触及时 {CAP:.2f} 以下挂了多少份、"
          f"全部吃下且按 Yes 结算可得多少（扣吃单费）、5 秒内有人在 {CAP:.2f} 以下成交了多少。最多列最近 20 个。", ""]
    if len(barriers):
        L += barrier_table(barriers)
        fin = barriers["below"].replace(np.inf, np.nan).dropna()
        L += ["", f"合计 {len(barriers)} 个；卖一 < {CAP:.2f} 持续中位 "
              + (f"{1000 * fin.median():.0f} ms" if len(fin) else "–")
              + f"，{(barriers['below'] == np.inf).sum()} 个超过 {BARRIER_WAIT:.0f} 秒；"
              f"触及时 < {CAP:.2f} 挂单中位 {barriers['depth'].median():,.0f} 份，可得中位 ${barriers['edge'].median():,.0f}。"]
    else:
        L.append("还没有触及事件。")
    L += ["", "## 近似和注意事项", "",
          "- 模型：无漂移对数正态 P = Φ(ln(S/K)/(σ√τ))，不含 −σ²/2 修正；σ = 最近 600 秒 1 秒对数收益的标准差，"
          "把几分钟的日内波动外推到几小时到几天（隔夜、周末、事件前后都会偏），所以「模型」公允价的绝对水平只作参考，"
          "「锚定中价」只用模型的变化。",
          "- 结算价是 12:00 ET 那根币安 1 分钟 K 线的收盘价（最后一笔成交）；这里用急动那笔成交价做 S，τ 算到 Gamma 的 endDate"
          "（夏令时 16:00 UTC，冬令时 17:00 UTC，与 K 线收盘差 ≤ 1 分钟），最后 60 秒不算。",
          "- ET ↔ UTC 用 America/New_York 时区（含夏令时切换），discover 用它核对每个市场的 endDate。",
          "- 触及市场按 1 分钟 K 线 High/Low 判定，等于全部成交的最高/最低价，这里用逐笔成交（aggTrade）判断；"
          "模型用反射原理（连续路径）。币安数据流断线时会漏掉触及。",
          "- updown_day 的参考价是前一天 12:00 ET 收盘的那根 K 线收盘价（币安 K 线接口取，或录制里那一刻之前的最后一笔）；"
          "updown_4h 实际按 Chainlink BTC/USD 60 秒 TWAP 结算，这里用币安价格近似，没有参考价时跳过。",
          f"- 数据卫生：急动前 {CLEAN[0]:.0f} 秒到后 {CLEAN[1]:.0f} 秒内 CLOB 断线（录制机时钟）的急动不用；"
          f"录制要在 t0 + {MARKOUTS[-1]:.0f} 秒后仍在运行；同一急动下多个市场同时计入（彼此相关）。"]
    if notes:
        L += ["", "备注：", ""] + [f"- {n}" for n in notes]
    return "\n".join(L) + "\n"


def built_dirs(roots):
    out = []
    for r in roots:
        r = Path(r)
        if (r / "books.csv.gz").exists():
            out.append(r)
        else:
            out += sorted(p.parent for p in r.glob("**/books.csv.gz"))
    return sorted(set(out))


def _name(d):
    d = Path(d)
    return d.parent.name if d.name == "built" else d.name


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dirs", nargs="+")
    ap.add_argument("--out", default="real/ladder-check.md")
    a = ap.parse_args(argv)
    results, names, notes = [], [], []
    for d in built_dirs(a.dirs):
        try:
            books, markets, spot, trades, closes = ld.load(d)
        except Exception as e:
            notes.append(f"{_name(d)}: 跳过（{type(e).__name__}: {e}）")
            continue
        if books.empty or markets.empty or len(spot) < 2:
            notes.append(f"{_name(d)}: 没有盘口、市场或币安成交")
            continue
        results.append(analyze(books, markets, spot, closes, trades))
        names.append(_name(d))
    text = report(results, names, notes)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
