"""ROUNDTRIP.md, the independent check: the frozen round-trip variants of real/roundtrip-frozen.json
(selected on kacho.io's per-second books by roundtrip_run.py) evaluated ONCE on the Hugging Face
100 ms BTC 5m books (whodisidk/polymarket-btc-updown-exchange-data, 2026-05-25 .. 08-29), with a
0.5 s decision-to-fill latency on both legs, the same costs and size rules, and a Bonferroni test
over the k' frozen variants. Paper research on market data only, no orders. Runs where Hugging Face
is reachable (GitHub).

    python roundtrip_hf.py --workdir DIR [--days N] --frozen real/roundtrip-frozen.json \
        --out real/roundtrip-hf.md [--preds factors_preds.parquet] [--dataset whodisidk/...]

Verdict (ROUNDTRIP.md 判定, fixed before anything is run): each of the k' frozen variants passes if
its mean P&L per share is > 0 and its one-sided p (SE clustered by market) is < 0.05 / k', with at
least 30 markets (as on kacho). If the frozen list is empty there is no verdict: the script still
runs and reports each family's 10 best kacho-A variants (the JSON's `describe` lists) on these
books as a DESCRIPTION only. Those descriptive rows are always reported, in their own section, and
never enter the verdict.

Data per daily archive (downloaded, read and deleted one at a time; cross.fetch / cross.archives /
cross.read_day / cross.read_binance / cross.market_table, cross.py unchanged)
- Markets: horizon 5 (end - start = 300 s, start on a 300 s boundary) whose start falls on the
  archive's own UTC date, so no market is evaluated twice. Outcome: the official one, from
  cross.market_table on the market rows (one row per market first, as jump2s_hf.shrink_markets)
  and on the final resolution rows only (as jump2s_hf.final_resolution: sorted by revision and
  emission time; where consistency_check exists only "ok" rows or rows that already carry the
  settled 1 / 0 prices). Outcomes found in any later archive are used too; a market whose outcome
  is never known is left out of every variant (counted).
- Book (each token's own best bid / ask and the size at it, cross.read_day's first-level sizes,
  best-first). Snapshots de-duplicated per (market, timestamp_ms), keeping the last. The archive
  writes a row every 100 ms and repeats the last state when nothing arrives, so a row's time does
  not show the quote is current: a snapshot's "change age" at time q is q minus the time of the
  market's last change of any top-of-book price or size (cross.HEALTH_STATE, as cross.book_health).
  Rows whose lifecycle_state is not active / open / trading (missing counts as active) or that are
  halted are unusable.
- du / dd (bid-side depth within 5c of the best bid, in USDC: sum of price x size; 0 when the bid
  side is empty), only when a family-5 depth variant is evaluated, from a second lean pass over the
  same archive (read_bid_depth; cross.read_day does not keep the bid ladders).
- Binance: the archive's own BTCUSDT prints (cross.read_binance) by the recorder's receipt time,
  plus the previous archive's last 15 min (carried). The price known at time q is that of the print
  with the latest TRADE time among the prints received <= q; it is used only if that trade is at
  most 2 s old at q (the recorder's feed has multi-second gaps; a stale price would turn a gap into
  a one-second "jump"). The market's reference is the price known at the start (<= 5 s old).

The kacho variant, unchanged, on a per-second grid built from these books
- Signals are each family module's own functions run on an HF Panel with the kacho Panel's layout:
  book column c (0..299) = the book in effect at start + c + 1 s (so a decision at second d, which
  reads column d - 1, sees the book as of the decision moment start + d); `age` = floor(change
  age in s), and the column is NaN / age -1 when the change age is >= 6 s (kacho: rows more than
  5 s older than the column are dropped; rt_fair / rt_factors' own "age <= 4" then means < 5 s at
  the decision) or the row is unusable. x[OFF + s] = log price known at start + s + 1 minus the
  reference, NaN when the price is unknown or stale; sig = std of the 1 s log returns over the 600
  s ending there (>= 300 valid); fair = roundtrip.fair_from (the same driftless point-price model
  as on kacho, even after the 08-07 switch to TWAP settlement: the frozen variants are evaluated as
  frozen). vol / buy are NaN (no family uses them). Family 6 needs the walk-forward predictions
  (--preds: factors_study.pkl or a parquet / csv indexed by the 5-minute boundary T); without them
  its variants are reported as not evaluated (a frozen one then fails).
- Execution (ROUNDTRIP.md 执行: 决定后 0.5 秒按当时快照成交): an entry decided at d fills at the ASK of
  the snapshot in effect at start + d + 0.5 s; an exit decided at e at the BID of the snapshot in
  effect at start + e + 0.5 s. A fill needs that snapshot to be at most 1 s old with a change age
  <= 5 s, usable (active, not halted), neither token's book crossed or locked, the two asks
  summing to >= 0.99 (cross.book_health's sanity rule), price 0.02..0.98 and >= 5 shares at the
  touched level. Both legs pay binary.taker_fee. A failed entry fill drops the entry; a failed exit
  fill moves to the next second's fill moment, up to the one decided at 284 s (fill at 284.5 s,
  15.5 s before the end); if none passes, the position is valued at the official outcome
  ('fallback', reported). Hold-to-settlement variants pay no exit fee.
- Exits: hold h -> e = d + h (capped at 284: forced); converge / stop are checked at decisions
  e >= d + 1 (the entry fill at d + 0.5 is known then) on the side mid of the book in effect at the
  decision (column e - 1, NaN when unusable or either book crossed / locked; never crosses), against
  the model fair known at e, or the per-entry target; the stop's reference is the side mid of the
  entry fill snapshot. One open position per variant per market: the next entry needs a decision
  second after the previous exit's fill. Entry decisions 1..282 (as on kacho).

Statistics: per variant, trades, markets, mean P&L per share, SE clustered by market (CR1, as
roundtrip.cluster_se) and by UTC day, t, one-sided p, mean holding time, the share valued at
settlement because no bid was usable (fallback); descriptively also the means before and from
2026-08-07 (point-price vs TWAP settlement).

Where ROUNDTRIP.md and the task are silent, the conservative choice (written before running): the
1 s / 5 s freshness rules above, the 2 s Binance staleness rule, markets with no official outcome
left out entirely, the two asks >= 0.99 sanity rule on fills, a frozen variant that cannot be
evaluated counts as failed.
"""
from __future__ import annotations

import argparse
import io
import json
import math
import re
import tarfile
import time
import traceback
from pathlib import Path

import numpy as np
import pandas as pd

import binary as bo
import cross
import roundtrip as rt

T = rt.T
OFF = rt.OFF
FILL_LAT_MS = 500             # decision -> fill
FILL_ROW_MAX_AGE_MS = 1000    # the fill snapshot is at most this old
FILL_CHANGE_MAX_MS = 5000     # ... and its top of book changed at most this long ago
SIG_CHANGE_LIMIT_S = rt.MAX_AGE + 1   # signal columns: change age < 6 s (age 0..5)
PRICE_MAX_AGE_MS = 2000       # Binance: latest known trade at most 2 s old
REF_MAX_AGE_MS = 5000         # the market's reference price at the start
CARRY_S = 900                 # previous archive's Binance prints kept (sigma 600 s + 120 s before the start)
DEPTH_BAND = 0.05
ASK_SUM_MIN = 0.99
TWAP_FROM = "2026-08-07"
ALPHA = rt.ALPHA
MIN_MARKETS = rt.MIN_MARKETS
STATE = tuple(cross.HEALTH_STATE)
ACTIVE = ("active", "open", "trading")
F6 = "f6_factor_model"
DEPTH_MEASURES = ("depth", "depth_sh")
AGG = ("n", "book", "cost", "n_up", "n_dn", "hold", "fb", "forced", "stop", "conv")


# ----------------------------------------------------------------------------------------------
# Archive helpers (the official outcome; the bid ladders)


def _closed_01(fields, direction):
    """Whether a resolution row already carries Polymarket's settled result (closed, outcome prices
    exactly 1 / 0, direction agreeing); as jump2s_hf._closed_01."""
    v = fields
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return False
    if not isinstance(v, dict) or v.get("closed") is not True:
        return False
    try:
        px = [float(x) for x in json.loads(v.get("outcomePrices") or "null")]
    except (TypeError, ValueError):
        return False
    if len(px) != 2 or sorted(px) != [0.0, 1.0]:
        return False
    return str(direction).strip().lower() == ("up" if px[0] == 1.0 else "down")


def final_resolution(rs):
    """Resolution rows for cross.market_table: sorted by revision then emission time; where
    consistency_check exists only final rows ("ok" or already settled 1 / 0). As
    jump2s_hf.final_resolution."""
    if rs is None or rs.empty or "market_id" not in rs:
        return rs
    keys = [c for c in ("revision", "emitted_at_ts") if c in rs]
    r = rs.sort_values(keys, kind="stable") if keys else rs
    if "consistency_check" not in r:
        return r
    ok = r["consistency_check"].astype(str).str.lower().eq("ok").to_numpy().copy()
    if "final_outcome_fields" in r and "outcome_direction" in r:
        ok |= np.array([_closed_01(f, d) for f, d in zip(r["final_outcome_fields"], r["outcome_direction"])], bool)
    return r[ok]


def shrink_markets(mk):
    """One row per market (last non-null value of each column in file order), as
    jump2s_hf.shrink_markets, so cross.market_table does not copy every 100 ms market row."""
    if mk is None or mk.empty or "market_id" not in mk:
        return mk
    return mk.groupby("market_id", sort=False, dropna=False).last().reset_index()


def _release():
    """Hand freed Arrow memory back to the system between archives (as jump2s_hf._release)."""
    try:
        import pyarrow as pa
        pa.default_memory_pool().release_unused()
    except Exception:  # an older pyarrow: nothing to do
        pass


def _list_parts(col):
    """(offsets, values) of a pyarrow list column (chunks combined)."""
    import pyarrow as pa
    arr = col.combine_chunks() if isinstance(col, pa.ChunkedArray) else col
    off = arr.offsets.to_numpy().astype(np.int64)
    vals = arr.values.to_numpy(zero_copy_only=False).astype(float)
    valid = arr.is_valid().to_numpy(zero_copy_only=False)
    return off, vals, valid


def bid_depth(prices, sizes, band=DEPTH_BAND):
    """Per row: sum of price x size over the bid levels priced >= best bid - band (USDC); 0 when the
    bid side is empty; NaN when the price and size lists do not line up."""
    po, pv, pvalid = _list_parts(prices)
    so, sv, svalid = _list_parts(sizes)
    n = len(po) - 1
    lp, ls = np.diff(po), np.diff(so)
    lp = np.where(pvalid, lp, 0)
    ls = np.where(svalid, ls, 0)
    out = np.zeros(n)
    bad = lp != ls
    rows = np.flatnonzero((lp > 0) & ~bad)
    if rows.size:
        lens = lp[rows]
        before = np.r_[0, np.cumsum(lens)[:-1]]
        pi = np.repeat(po[rows] - before, lens) + np.arange(lens.sum())
        si = np.repeat(so[rows] - before, lens) + np.arange(lens.sum())
        p, s = pv[pi], sv[si]
        parent = np.repeat(np.arange(rows.size), lens)
        pm = np.where(np.isfinite(p), p, -np.inf)
        best = np.maximum.reduceat(pm, before)
        use = np.isfinite(p) & np.isfinite(s) & (p >= best[parent] - band - 1e-9) & (s > 0)
        out[rows] = np.bincount(parent, weights=np.where(use, p * s, 0.0), minlength=rows.size)
    out[bad] = np.nan
    return out


def read_bid_depth(path, market_ids, band=DEPTH_BAND):
    """(timestamp_ms, market_id, du, dd) of the polymarket_features_100ms rows of `market_ids`
    (bid depth within `band` of the best bid, USDC), de-duplicated per (market, timestamp) keeping
    the last row in archive order (as the book)."""
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq
    want = pa.array(sorted(map(str, market_ids)), pa.string())
    parts = []
    with tarfile.open(path, "r|gz") as tar:
        for m in tar:
            if not (m.isfile() and m.name.endswith(".parquet")):
                continue
            if (re.search(r"dataset=([^/]+)", m.name) or [None, ""])[1] != "polymarket_features_100ms":
                continue
            pf = pq.ParquetFile(io.BytesIO(tar.extractfile(m).read()))
            names = set(pf.schema_arrow.names)
            if not {"timestamp_ms", "market_id"} <= names:
                continue
            cols = [c for c in ("timestamp_ms", "market_id", "up_side_bids", "up_bid_sizes", "down_side_bids",
                                "down_bid_sizes") if c in names]
            t = pf.read(columns=cols)
            t = t.filter(pc.is_in(t["market_id"].cast(pa.string()), value_set=want))
            if not t.num_rows:
                continue
            d = {"timestamp_ms": t["timestamp_ms"].to_numpy().astype(np.int64),
                 "market_id": t["market_id"].cast(pa.string()).to_numpy(zero_copy_only=False).astype(str)}
            for side, k in (("up", "du"), ("down", "dd")):
                pcn, scn = f"{side}_side_bids", f"{side}_bid_sizes"
                d[k] = bid_depth(t[pcn], t[scn], band) if pcn in names and scn in names else np.full(t.num_rows, np.nan)
            parts.append(pd.DataFrame(d))
    if not parts:
        return pd.DataFrame(columns=["timestamp_ms", "market_id", "du", "dd"])
    out = pd.concat(parts, ignore_index=True)
    return out.drop_duplicates(["market_id", "timestamp_ms"], keep="last")


# ----------------------------------------------------------------------------------------------
# Per-second grids from the 100 ms data


MAX_RECV_DELAY_MS = None      # strict check: drop seconds whose latest print arrived > this late


def binance_grid(prints, t_lo, t_hi, max_age_ms=PRICE_MAX_AGE_MS, ref_age_ms=REF_MAX_AGE_MS):
    """Log price known at each second boundary t_lo..t_hi (seconds; the price of the print with the
    latest trade time among those received <= the boundary), NaN when that trade is older than
    max_age_ms; `lp_ref` the same with ref_age_ms; `sig` the std of the 1 s log returns (both ends
    known) over the 600 boundaries ending there (>= 300). Returns dict(t0, lp, lp_ref, sig)."""
    B = np.arange(int(t_lo), int(t_hi) + 1, dtype=np.int64)
    q = B * 1000
    out = {"t0": int(t_lo)}
    if prints is None or not len(prints):
        nan = np.full(len(B), np.nan)
        return {**out, "lp": nan, "lp_ref": nan.copy(), "sig": nan.copy()}
    b = prints[["trade_ts_ms", "recv_ts_ms", "price"]].apply(pd.to_numeric, errors="coerce").to_numpy(float)
    b = b[np.isfinite(b).all(axis=1) & (b[:, 2] > 0)]
    b = b[np.lexsort((b[:, 0], b[:, 1]))]               # by receipt, then trade time
    tt, recv, lp = b[:, 0], b[:, 1], np.log(b[:, 2])
    best = np.maximum.accumulate(tt) if len(tt) else tt
    idx = np.arange(len(tt))
    arg = np.maximum.accumulate(np.where(tt >= best, idx, -1)) if len(tt) else idx
    i = np.searchsorted(recv, q, "right") - 1
    ok = i >= 0
    ii = np.maximum(i, 0)
    age = np.where(ok, q - (best[ii] if len(tt) else 0), np.inf)
    val = np.where(ok, lp[arg[ii]] if len(tt) else np.nan, np.nan)
    if MAX_RECV_DELAY_MS is not None and len(tt):
        dly = np.where(ok, recv[arg[ii]] - tt[arg[ii]], np.inf)   # receipt delay of the print used
        ok = ok & (dly <= MAX_RECV_DELAY_MS)
    lpx = np.where(ok & (age <= max_age_ms), val, np.nan)
    lpr = np.where(ok & (age <= ref_age_ms), val, np.nan)
    r = np.r_[np.nan, np.diff(lpx)]
    sig = pd.Series(r).rolling(rt.SIGMA_WIN, min_periods=rt.SIGMA_MIN).std().to_numpy()
    return {**out, "lp": lpx, "lp_ref": lpr, "sig": sig}


def _market_rows(feat):
    """{market_id: positions} and the numeric columns of the features frame as arrays."""
    cols = {c: (feat[c].to_numpy(float) if c in feat else np.full(len(feat), np.nan))
            for c in STATE + ("du", "dd")}
    ts = feat["timestamp_ms"].to_numpy(np.int64)
    usable = np.ones(len(feat), bool)
    if "lifecycle_state" in feat:
        ls = feat["lifecycle_state"]
        usable &= (ls.isna() | ls.astype(str).str.lower().isin(ACTIVE)).to_numpy(bool)
    if "observed_halt_flag" in feat:
        usable &= ~feat["observed_halt_flag"].fillna(False).astype(bool).to_numpy()
    groups = feat.groupby(feat["market_id"].astype(str), sort=False).indices
    return groups, ts, cols, usable


PANEL_MAP = {"bu": "up_best_bid", "au": "up_best_ask", "bd": "down_best_bid", "ad": "down_best_ask",
             "su": "up_bid_size", "sau": "up_ask_size", "sd": "down_bid_size", "sad": "down_ask_size",
             "du": "du", "dd": "dd"}
FILL_MAP = {"bu": "up_best_bid", "au": "up_best_ask", "bd": "down_best_bid", "ad": "down_best_ask",
            "su": "up_bid_size", "sau": "up_ask_size", "sd": "down_bid_size", "sad": "down_ask_size"}


def market_book(ts, vals, usable, start_ms):
    """For one market's snapshots (any order): signal-panel columns (state at start + c + 1 s) and
    fill rows (state at start + r - 0.5 s). Returns (sig dict of (T,) arrays, age (T,), fill dict,
    fill_ok (T,))."""
    o = np.argsort(ts, kind="stable")
    ts = ts[o]
    keep = np.r_[ts[1:] != ts[:-1], True] if len(ts) else np.zeros(0, bool)   # last row per timestamp
    o, ts = o[keep], ts[keep]
    v = {k: a[o] for k, a in vals.items()}
    us = usable[o]
    st = np.stack([v[c] for c in STATE], axis=1) if len(ts) else np.zeros((0, len(STATE)))
    same = (st[1:] == st[:-1]) | (np.isnan(st[1:]) & np.isnan(st[:-1]))
    moved = np.r_[True, ~same.all(axis=1)] if len(ts) else np.zeros(0, bool)
    lc = np.maximum.accumulate(np.where(moved, ts, np.iinfo(np.int64).min)) if len(ts) else ts

    def at(q):
        i = np.searchsorted(ts, q, "right") - 1
        ok = i >= 0
        ii = np.maximum(i, 0)
        if not len(ts):
            return ok, ii, np.full(len(q), np.inf), np.full(len(q), np.inf)
        return ok, ii, np.where(ok, q - ts[ii], np.inf), np.where(ok, q - lc[ii], np.inf)

    c = np.arange(T, dtype=np.int64)
    ok, ii, row_age, ch_age = at(start_ms + (c + 1) * 1000)
    good = ok & (ch_age < SIG_CHANGE_LIMIT_S * 1000)
    if len(ts):
        good &= us[ii]
    age = np.where(good, np.floor(ch_age / 1000.0), -1).astype(np.int8)
    sig = {}
    for k, col in PANEL_MAP.items():
        a = v[col][ii] if len(ts) else np.full(T, np.nan)
        sig[k] = np.where(good, a, np.nan).astype(np.float32)
    ok, ii, row_age, ch_age = at(start_ms + c * 1000 - FILL_LAT_MS)
    fill = {}
    for k, col in FILL_MAP.items():
        a = v[col][ii] if len(ts) else np.full(T, np.nan)
        fill[k] = np.where(ok, a, np.nan).astype(np.float32)
    with np.errstate(invalid="ignore"):
        fok = (ok & (row_age <= FILL_ROW_MAX_AGE_MS) & (ch_age <= FILL_CHANGE_MAX_MS)
               & (fill["au"] > fill["bu"]) & (fill["ad"] > fill["bd"])
               & (fill["au"].astype(float) + fill["ad"].astype(float) >= ASK_SUM_MIN - rt.TOL))
    if len(ts):
        fok &= us[ii]
    return sig, age, fill, fok


def build_day(feat, mk5, binance, carry=None):
    """(HF Panel, fill arrays) for the markets of mk5 (columns market_id, slug, start (ms), up_won).
    `binance` / `carry`: Binance prints (trade_ts_ms, recv_ts_ms, price)."""
    mk5 = mk5.sort_values("start").reset_index(drop=True)
    M = len(mk5)
    start_ms = mk5["start"].to_numpy(np.int64)
    start = start_ms // 1000
    book = {k: np.full((M, T), np.nan, np.float32) for k in PANEL_MAP}
    age = np.full((M, T), -1, np.int8)
    fill = {k: np.full((M, T), np.nan, np.float32) for k in FILL_MAP}
    fok = np.zeros((M, T), bool)
    if len(feat):
        groups, ts, cols, usable = _market_rows(feat)
        for j, mid in enumerate(mk5["market_id"].astype(str)):
            pos = groups.get(mid)
            if pos is None:
                continue
            w = pos[(ts[pos] >= start_ms[j] - 60_000) & (ts[pos] <= start_ms[j] + T * 1000)]
            s, a, f, ok = market_book(ts[w], {k: c[w] for k, c in cols.items()}, usable[w], start_ms[j])
            for k in book:
                book[k][j] = s[k]
            age[j] = a
            for k in fill:
                fill[k][j] = f[k]
            fok[j] = ok
    prints = binance if carry is None or not len(carry) else pd.concat([carry, binance], ignore_index=True)
    if M:
        g = binance_grid(prints, start.min() - OFF - rt.SIGMA_WIN - 5, start.max() + T + 1)
        cols_s = start[:, None] + np.arange(-OFF, T)[None, :] + 1 - g["t0"]
        lp = g["lp"][cols_s]
        ref = g["lp_ref"][start - g["t0"]]
        x = (lp - ref[:, None]).astype(np.float32)
        sg = g["sig"][cols_s].astype(np.float32)
    else:
        x = np.zeros((0, OFF + T), np.float32)
        sg = x.copy()
        ref = np.zeros(0)
    fair = rt.fair_from(x, sg, OFF) if M else np.zeros((0, T), np.float32)
    nanb = np.full(x.shape, np.nan, np.float32)
    won = mk5["up_won"].to_numpy(float) if "up_won" in mk5 else np.full(M, np.nan)
    p = rt.Panel(cid=mk5["market_id"].astype(str).to_numpy(), slug=mk5.get("slug", mk5["market_id"]).astype(str).to_numpy(),
                 start=start, up_won=np.where(np.isfinite(won), won, -1).astype(np.int8),
                 up_inferred=np.full(M, -1, np.int8), n_ticks=np.zeros(M, np.int16), split=np.zeros(M, np.int8),
                 day=(start // 86400).astype(np.int32), ref=np.exp(ref), age=age, fair=fair, x=x, sig=sg,
                 vol=nanb, buy=nanb.copy(), off=np.int64(OFF), **book)
    return p, {**fill, "ok": fok}


# ----------------------------------------------------------------------------------------------
# Engine: roundtrip.Engine's rules with the 0.5 s fills


class HFEngine(rt.Engine):
    """Fill row r = the snapshot in effect at start + r - 0.5 s (entry decided at d: row d + 1; exit
    decided at e: row e + 1); converge / stop read the signal panel's side mids (column e - 1 = the
    book at the decision e) from e = d + 1; the stop's reference is the entry fill snapshot's mid."""

    def __init__(self, panel, fill):
        self.p = panel
        self.D = 1
        self.last_decision = rt.FORCE_ROW - 3
        self.M = panel.M
        bid = np.stack([fill["bu"], fill["bd"]]).astype(np.float32)
        ask = np.stack([fill["au"], fill["ad"]]).astype(np.float32)
        bsz = np.stack([fill["su"], fill["sd"]]).astype(np.float32)
        asz = np.stack([fill["sau"], fill["sad"]]).astype(np.float32)
        ok = np.asarray(fill["ok"], bool)
        with np.errstate(invalid="ignore"):
            inr = lambda v: (v >= rt.PMIN - rt.TOL) & (v <= rt.PMAX + rt.TOL)
            self.entry_ok = ok[None] & inr(ask) & (asz >= rt.MIN_SIZE - rt.TOL)
            exit_ok = ok[None] & inr(bid) & (bsz >= rt.MIN_SIZE - rt.TOL)
        self.bid, self.ask = bid, ask
        fm = (bid + ask) / 2
        fm[:, ~ok] = np.nan
        self.fill_mid = fm
        self.nv = rt._next_true(exit_ok, rt.FORCE_ROW)
        with np.errstate(invalid="ignore"):
            good = (panel.age >= 0) & (panel.au > panel.bu) & (panel.ad > panel.bd)
        mid = np.stack([(panel.bu + panel.au) / 2, (panel.bd + panel.ad) / 2]).astype(np.float32)
        mid[:, ~good] = np.nan
        self.mid = mid
        self.won = np.full((2, self.M), -1, np.int8)   # outcomes are applied after all archives
        self._tab, self._nc = {}, {}

    def simulate(self, ent, ex):
        m = np.asarray(ent.market, np.int64)
        d = np.asarray(ent.second, np.int64)
        side = np.asarray(ent.side, np.int64)
        tgt = None if ent.target is None else np.asarray(ent.target, float)
        ok = (d >= 1) & (d <= self.last_decision) & (m >= 0) & (m < self.M) & (side != 0)
        m, d, side = m[ok], d[ok], side[ok]
        tgt = None if tgt is None else tgt[ok]
        k = (side < 0).astype(np.int64)
        r0 = d + 1
        ok = self.entry_ok[k, m, r0]
        m, d, k, r0 = m[ok], d[ok], k[ok], r0[ok]
        tgt = None if tgt is None else tgt[ok]
        o = np.lexsort((d, m))
        m, d, k, r0 = m[o], d[o], k[o], r0[o]
        tgt = None if tgt is None else tgt[o]
        n = len(m)
        e_cap = rt.FORCE_ROW - 1
        if ex.kind == "settle":
            X = np.full(n, -1, np.int64)
            reason = np.full(n, rt.SETTLE, np.int8)
            free = np.full(n, rt.KEY - 1, np.int64)
        else:
            if ex.kind not in ("hold", "converge"):
                raise ValueError(ex.kind)
            e = np.minimum(d + ex.h, e_cap)
            reason = np.where(d + ex.h > e_cap, rt.FORCED, rt.HOLD).astype(np.int8)
            first_check = d + 1
            if ex.kind == "converge":
                if ex.target == "entry":
                    if tgt is None:
                        raise ValueError("Exit(target='entry') needs Entries.target")
                    ec = self.first_cross(k, m, first_check - 1, e - 1, tgt - ex.eps - rt.TOL, below=False) + 1
                else:
                    ec = self.next_converge(ex.target, ex.eps)[k, m, first_check].astype(np.int64)
                hit = ec <= e
                reason[hit] = rt.CONVERGE
                e = np.where(hit, ec, e)
            if ex.stop > 0:
                ref = self.fill_mid[k, m, r0].astype(float)
                es = self.first_cross(k, m, first_check - 1, e - 1, ref - ex.stop + rt.TOL, below=True) + 1
                hit = es <= e
                reason[hit] = rt.STOP
                e = np.where(hit, es, e)
            X = self.nv[k, m, e + 1].astype(np.int64)
            fb = X >= rt.NONE
            reason[fb] = rt.FALLBACK
            X = np.where(fb, -1, X)
            free = np.where(fb, rt.KEY - 1, X)          # next decision >= X, after the fill at X - 0.5 s
        key = m * rt.KEY + d
        nxt = np.searchsorted(key, m * rt.KEY + free, "left")
        bad = nxt >= n
        nxt[bad] = 0
        nxt = np.where(bad | (m[nxt] != m), -1, nxt) if n else nxt
        t = rt.chain(m, nxt)
        m, d, k, r0, X, reason = m[t], d[t], k[t], r0[t], X[t], reason[t]
        ask = self.ask[k, m, r0].astype(float)
        settled = X < 0
        bid = np.where(settled, np.nan, self.bid[k, m, np.maximum(X, 0)].astype(float))
        hold = np.where(settled, T - (r0 - 0.5), (X - r0).astype(float))
        return {"market": m, "second": d, "side": np.where(k == 0, 1, -1).astype(np.int8), "entry_row": r0,
                "exit_row": X, "entry_px": ask, "exit_px": bid, "settled": settled, "hold": hold, "reason": reason}


def aggregate(tr, M):
    """Per market of one variant's trades: dict of (M,) sums (AGG); P&L needing the outcome is kept
    as counts of settled Up / Down shares and their cost."""
    m = tr["market"]
    st = tr["settled"]
    ask, bid = tr["entry_px"], tr["exit_px"]
    fee_in = bo.taker_fee(ask)
    book = np.where(st, 0.0, np.nan_to_num(bid) - bo.taker_fee(np.nan_to_num(bid)) - ask - fee_in)
    cost = np.where(st, -ask - fee_in, 0.0)
    r = tr["reason"]
    w = {"n": np.ones(len(m)), "book": book, "cost": cost, "n_up": st & (tr["side"] > 0), "n_dn": st & (tr["side"] < 0),
         "hold": tr["hold"], "fb": r == rt.FALLBACK, "forced": r == rt.FORCED, "stop": r == rt.STOP,
         "conv": r == rt.CONVERGE}
    return {k: np.bincount(m, weights=np.asarray(v, float), minlength=M) for k, v in w.items()}


# ----------------------------------------------------------------------------------------------
# Variants from the frozen JSON


def _norm(params):
    return json.dumps(json.loads(json.dumps(params, default=lambda o: o.item() if hasattr(o, "item") else str(o))),
                      sort_keys=True)


def load_frozen(path):
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    frozen = doc.get("survivors") or []
    describe = doc.get("describe") or {}
    return doc, frozen, describe


def resolve_variants(entries, variants=None):
    """[(entry, Variant)] for JSON entries, from the family modules' own grids; refuses an id that
    is not in the grids or whose parameters differ from the grid's."""
    if variants is None:
        import roundtrip_run as rr
        fams, _ = rr.all_variants(None)
        variants = [v for vs in fams.values() for v in vs]
    by_id = {v.id: v for v in variants}
    out = []
    for e in entries:
        v = by_id.get(e["id"])
        if v is None:
            raise KeyError(f"{e['id']} is not a variant of the family grids")
        if _norm(v.all_params()) != _norm(e["params"]):
            raise ValueError(f"{e['id']}: parameters differ from the grid's: {e['params']} vs {v.all_params()}")
        out.append((e, v))
    return out


def load_preds_file(path, models=None):
    """Family-6 predictions per 5-minute boundary T (seconds): factors_study.pkl (R['preds']) or a
    parquet / csv with T as index or a 'T' column."""
    p = Path(path)
    if p.suffix == ".pkl":
        import pickle
        with open(p, "rb") as fh:
            df = pickle.load(fh)["preds"]
    elif p.suffix == ".parquet":
        df = pd.read_parquet(p)
    else:
        df = pd.read_csv(p)
    if "T" in df.columns:
        df = df.set_index("T")
    df.index = df.index.astype(np.int64)
    if models:
        df = df[[m for m in models if m in df.columns]]
    return df


# ----------------------------------------------------------------------------------------------
# The run


def _day_of(name):
    m = re.search(r"(\d{4}-\d{2}-\d{2})", str(name))
    return m.group(1) if m else None


def markets_5m(mt, day=None):
    """5m markets of a cross.market_table frame (start / end in ms), starting on `day` (UTC) if
    given: columns market_id, slug, start, up_won."""
    if mt is None or not len(mt):
        return pd.DataFrame(columns=["market_id", "slug", "start", "up_won"])
    m = mt.copy()
    m["start"] = pd.to_numeric(m["start"], errors="coerce")
    m["end"] = pd.to_numeric(m["end"], errors="coerce")
    ok = (m["horizon"] == 5) & ((m["end"] - m["start"]) == T * 1000) & ((m["start"] % 300_000) == 0)
    m = m[ok.fillna(False).to_numpy(bool)].copy()
    if day:
        d = pd.to_datetime(m["start"], unit="ms", utc=True).dt.strftime("%Y-%m-%d")
        m = m[(d == day).to_numpy(bool)]
    m["market_id"] = m["market_id"].astype(str)
    m["start"] = m["start"].astype(np.int64)
    return m[["market_id", "slug", "start", "up_won"]].drop_duplicates("market_id").reset_index(drop=True)


INT_AGG = ("n", "n_up", "n_dn", "fb", "forced", "stop", "conv")


def evaluate_day(panel, fill, plan, preds=None, codes=None):
    """Per-market sums (one frame) for every variant of `plan` [(vid, Variant)] on one day's panel;
    `market` is the market id, or codes[i] (int32) when given (compact over many archives)."""
    if preds is not None and len(preds.columns):
        import rt_factors as rf
        rf.attach_preds(panel, preds)
    eng = HFEngine(panel, fill)
    groups = {}
    for vid, v in plan:
        groups.setdefault(v.key if v.key is not None else v.id, []).append((vid, v))
    parts = []
    M = panel.M
    for lst in groups.values():
        v0 = lst[0][1]
        if v0.family == F6 and f"f6_pred_{v0.params.get('model')}" not in panel.a:
            continue                                   # no predictions: not evaluated
        ent = v0.signal(panel)
        for vid, v in lst:
            agg = aggregate(eng.simulate(ent, v.exit), M)
            has = np.flatnonzero(agg["n"] > 0)
            if not has.size:
                continue
            parts.append(pd.DataFrame({"vid": np.full(has.size, vid, np.int32),
                                       "market": panel.cid[has] if codes is None else codes[has],
                                       **{k: agg[k][has].astype(np.int16 if k in INT_AGG else
                                                                np.float32 if k == "hold" else np.float64)
                                          for k in AGG}}))
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=["vid", "market", *AGG])


def needs_depth(plan):
    return any(v.family == "f5_book_imbalance" and v.params.get("measure") in DEPTH_MEASURES for _, v in plan)


def run(archives, out, frozen_path, preds_path=None, workdir=None, download=False, log=print):
    """archives: [(name, local path or None)]; with download, each is fetched into workdir and
    deleted after reading. Writes the report to `out` and returns (stats frame, info)."""
    t_start = time.time()
    doc, frozen, describe = load_frozen(frozen_path)
    import roundtrip_run as rr
    fams, _ = rr.all_variants(None)
    grid = [v for vs in fams.values() for v in vs]
    fz = resolve_variants(frozen, grid)
    ds = [e for es in describe.values() for e in es]
    dz = resolve_variants(ds, grid)
    plan, seen = [], {}
    role = []
    for tag, lst in (("frozen", fz), ("describe", dz)):
        for e, v in lst:
            if v.id in seen:
                role[seen[v.id]] = role[seen[v.id]] | {tag}
                continue
            seen[v.id] = len(plan)
            plan.append((len(plan), v))
            role.append({tag})
    preds = None
    if preds_path:
        try:
            preds = load_preds_file(preds_path, ("ridge_5m", "hgb_5m", "ridge_15m", "hgb_15m"))
        except Exception as e:
            log(f"predictions {preds_path}: {e!r}; family 6 not evaluated")
    depth = needs_depth(plan)
    log(f"variants: {len(fz)} frozen, {len(dz)} descriptive ({len(plan)} distinct); depth pass {'on' if depth else 'off'}; "
        f"family-6 predictions {'yes' if preds is not None else 'no'}")
    parts, infos, failed, outcomes, starts, carry = [], [], [], {}, {}, None
    registry = {}                                      # market id -> int code
    for name, local in archives:
        day = _day_of(name)
        t0 = time.time()
        try:
            if download:
                local = cross.fetch(name, Path(workdir) / name)
            feat, mk, rs = cross.read_day(local)
            _release()
            mt = cross.market_table(shrink_markets(mk), final_resolution(rs))
            del mk, rs
            _release()
            for r in markets_5m(mt).itertuples(index=False):   # every 5m outcome seen (also other days')
                if np.isfinite(r.up_won):
                    outcomes[r.market_id] = float(r.up_won)
            mk5 = markets_5m(mt, day)
            binance = cross.read_binance(local)
            if len(feat):
                feat = feat[feat["market_id"].astype(str).isin(set(mk5["market_id"]))]
            if depth and len(mk5):
                dep = read_bid_depth(local, set(mk5["market_id"]))
                if len(feat):
                    feat = feat.drop(columns=[c for c in ("du", "dd") if c in feat])
                    feat["market_id"] = feat["market_id"].astype(str)
                    feat["timestamp_ms"] = feat["timestamp_ms"].astype(np.int64)
                    feat = feat.merge(dep, on=["market_id", "timestamp_ms"], how="left", sort=False)
            if download:
                Path(local).unlink()
            panel, fill = build_day(feat, mk5, binance, carry)
            del feat
            _release()
            codes = np.array([registry.setdefault(mid, len(registry)) for mid in panel.cid], np.int32)
            for c, st in zip(codes, panel.start):
                starts[int(c)] = int(st)
            rows = evaluate_day(panel, fill, plan, preds, codes)
            parts.append(rows)
            info = {"day": day, "markets": int(panel.M), "prints": int(len(binance)),
                    "x_known": float(np.isfinite(panel.x[:, OFF:]).mean()) if panel.M else np.nan,
                    "book_known": float((panel.age >= 0).mean()) if panel.M else np.nan,
                    "fill_ok": float(fill["ok"][:, 1:rt.FORCE_ROW + 1].mean()) if panel.M else np.nan,
                    "rows": int(len(rows)), "s": time.time() - t0}
            infos.append(info)
            if len(binance):
                tt = pd.to_numeric(binance["recv_ts_ms"], errors="coerce")
                carry = binance[tt >= tt.max() - CARRY_S * 1000][["trade_ts_ms", "recv_ts_ms", "price"]].copy()
            else:
                carry = None
            log(f"{name}: {info['markets']} 5m markets, {info['prints']:,} Binance prints, Binance known "
                f"{100 * info['x_known']:.0f}% / book usable {100 * info['book_known']:.0f}% / fill ok "
                f"{100 * info['fill_ok']:.0f}% of seconds, {info['rows']:,} variant-market rows ({info['s']:.0f} s)")
        except Exception:
            log(f"{name}: failed\n{traceback.format_exc()}")
            failed.append(day or str(name))
            carry = None
            if download and local and Path(local).exists():
                Path(local).unlink()
    rows = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=["vid", "market", *AGG])
    stats, fin = finalize(rows, plan, {registry[i]: w for i, w in outcomes.items() if i in registry}, starts)
    info = {"archives": len(archives), "failed": failed, "days": infos, "runtime_s": time.time() - t_start,
            "preds": preds is not None, "depth": depth, **fin}
    L = report(stats, plan, role, fz, dz, doc, info)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text("\n".join(L) + "\n", encoding="utf-8")
    log("\n".join(L))
    return stats, info


def _cluster(S, c):
    """mean, CR1 SE of per-cluster sums S / counts c (only clusters with c > 0)."""
    N = c.sum()
    if N <= 0:
        return np.nan, np.nan, 0
    G = int((c > 0).sum())
    mean = S.sum() / N
    if G < 2:
        return mean, np.nan, G
    r = S - mean * c
    return mean, math.sqrt((r @ r) * G / (G - 1)) / N, G


def finalize(rows, plan, outcomes, starts):
    """Per-variant stats from the per-(variant, market) sums; markets without an official outcome
    are left out of every variant."""
    all_mk = set(starts)
    unknown = {m for m in all_mk if m not in outcomes}
    fin = {"markets": len(all_mk), "markets_no_outcome": len(unknown)}
    out = []
    if len(rows):
        rows = rows[~rows["market"].isin(unknown)].copy()
        rows = rows.groupby(["vid", "market"], as_index=False)[list(AGG)].sum()
        for k in AGG:
            rows[k] = rows[k].astype(float)
        won = rows["market"].map(outcomes).to_numpy(float)
        rows["S"] = rows["book"] + rows["cost"] + rows["n_up"] * won + rows["n_dn"] * (1 - won)
        st = rows["market"].map(starts).to_numpy(np.int64)
        rows["day"] = st // 86400
        rows["late"] = st >= pd.Timestamp(TWAP_FROM, tz="UTC").timestamp()
    by_vid = dict(tuple(rows.groupby("vid"))) if len(rows) else {}
    for vid, v in plan:
        g = by_vid.get(vid)
        n = float(g["n"].sum()) if g is not None else 0.0
        r = {"vid": vid, "id": v.id, "family": v.family, "name": v.name, "control": bool(v.control), "n": int(n)}
        if n > 0:
            mean, se, G = _cluster(g["S"].to_numpy(float), g["n"].to_numpy(float))
            gd = g.groupby("day")[["S", "n"]].sum()
            _, sed, _ = _cluster(gd["S"].to_numpy(float), gd["n"].to_numpy(float))
            t = mean / se if se and se > 0 else np.nan
            r.update(mk=G, mean=mean, se=se, sed=sed, t=t, p=rt._p_one_sided(t), hold=g["hold"].sum() / n,
                     fb=g["fb"].sum() / n, forced=g["forced"].sum() / n, stop=g["stop"].sum() / n,
                     conv=g["conv"].sum() / n)
            for tag, sel in (("early", ~g["late"]), ("late", g["late"])):
                gg = g[sel.to_numpy(bool)]
                nn = gg["n"].sum()
                r[f"n_{tag}"] = int(nn)
                r[f"mean_{tag}"] = gg["S"].sum() / nn if nn else np.nan
        else:
            r.update(mk=0, mean=np.nan, se=np.nan, sed=np.nan, t=np.nan, p=1.0, hold=np.nan, fb=np.nan,
                     forced=np.nan, stop=np.nan, conv=np.nan, n_early=0, mean_early=np.nan, n_late=0, mean_late=np.nan)
        out.append(r)
    return pd.DataFrame(out), fin


def verdict(stats, frozen_ids, alpha=ALPHA, min_markets=MIN_MARKETS):
    """k' = number of frozen variants; pass: mean > 0, p < alpha / k', >= min_markets markets."""
    k = len(frozen_ids)
    s = stats.set_index("id").reindex(list(frozen_ids))
    thr = alpha / k if k else np.nan
    ok = (s["mean"] > 0) & (s["p"] < thr) & (s["mk"] >= min_markets)
    return k, thr, ok.fillna(False).to_numpy(bool)


# ----------------------------------------------------------------------------------------------
# Report (Chinese)


def _cents(v, se=None):
    if v is None or not np.isfinite(v):
        return "–"
    return f"{100 * v:+.2f}¢" + (f" ±{100 * se:.2f}" if se is not None and np.isfinite(se) else "")


def _row(r, kacho=None):
    if not r["n"]:
        return "0 / 0 | – | – | – | –"
    t = f"t {r['t']:+.1f}" if np.isfinite(r["t"]) else "t –"
    s = (f"{int(r['n']):,} / {int(r['mk']):,} | {_cents(r['mean'], r['se'])}（{t}，p {r['p']:.1e}） | "
         f"{r['hold']:.0f} | {100 * r['fb']:.0f}% | {_cents(r['mean_early'])} / {_cents(r['mean_late'])}")
    return s


def report(stats, plan, role, fz, dz, doc, info):
    days = info["days"]
    fz_ids = [v.id for _, v in fz]
    k, thr, ok = verdict(stats, fz_ids)
    by_id = stats.set_index("id")
    L = ["# 不持有到结算：冻结名单在 Hugging Face 100 毫秒盘口上的独立复核（BTC 5m）", "",
         "> 设计见 [ROUNDTRIP.md](../ROUNDTRIP.md)；冻结名单 "
         f"[roundtrip-frozen.json](roundtrip-frozen.json)（冻结于 {doc.get('frozen_at', '?')}，kacho 上 N = {doc.get('N', '?')}）；"
         "脚本 roundtrip_hf.py。只用行情数据，纸面研究，不下单。",
         "> 每个变体的信号、出场和参数与 kacho 上完全相同（各族模块自己的函数），只把盘口换成 100 毫秒快照、"
         "按秒取当时的状态：在 d 秒决定，只用 d 秒及以前收到的盘口和币安成交；决定后 0.5 秒按那一刻的快照成交"
         "（买入按卖一，卖出按买一，两腿都是）。成交快照不超过 1 秒、盘口 5 秒内变过、两本书都不交叉、两边卖一之和 ≥ 0.99、"
         "价格 0.02–0.98、该价位 ≥ 5 份；两腿各付 0.07·p(1 − p)；最晚 284.5 秒（剩 15.5 秒）卖出，卖不掉按官方结算算。"
         "每份盈亏 ± 按市场聚类的标准误。", ""]
    ok_days = [d["day"] for d in days]
    L += [f"- 数据：{info['archives']} 个日档，成功 {len(ok_days)} 个"
          + (f"（{ok_days[0]} – {ok_days[-1]}）" if ok_days else "") + f"，失败 {len(info['failed'])} 个"
          + (f"（{', '.join(info['failed'])}）" if info["failed"] else "") + "。",
          f"- 市场：{info['markets']:,} 个 5m 市场；找不到官方结果的 {info['markets_no_outcome']:,} 个不计入任何变体。"]
    if days:
        xk = np.nanmean([d["x_known"] for d in days])
        bk = np.nanmean([d["book_known"] for d in days])
        fk = np.nanmean([d["fill_ok"] for d in days])
        L.append(f"- 覆盖：币安价格可用的秒 {100 * xk:.0f}%，盘口可用的秒 {100 * bk:.0f}%，可成交的快照 {100 * fk:.0f}%（各日平均）。")
    L.append(f"- 第 6 族的样本外预测：{'已提供' if info['preds'] else '没有提供（第 6 族变体未评估）'}；"
             f"第 5 族深度：{'从买单阶梯另算' if info['depth'] else '不需要'}。运行 {info['runtime_s']:.0f} 秒。")
    L += ["", "## 判定（事先写死）", ""]
    if k == 0:
        L.append("**冻结名单为空，没有可判定的变体。** 下面各族的结果只作描述，不下任何结论。")
    else:
        npass = int(ok.sum())
        L.append(f"冻结名单 k′ = {k} 个变体；每个变体每份 > 0 且单边 p < 0.05 / k′ = {thr:.2e}（至少 {MIN_MARKETS} 个市场）才算通过。"
                 f"**通过 {npass} 个。**" + ("通过的要再在前向数据上按同样规则检验。" if npass else ""))
        rows = []
        for (e, v), passed in zip(fz, ok):
            r = by_id.loc[v.id]
            rows.append((r["t"] if np.isfinite(r["t"]) else -np.inf, v, r, e, passed))
        rows.sort(key=lambda x: -x[0])
        table = ["| 变体 | 笔数 / 市场 | 每份 | 持有 | 卖不掉 | 8/07 前 / 起 | kacho A / B | 通过 |",
                 "|---|---:|---|---:|---:|---|---|:-:|"]
        for _, v, r, e, passed in rows:
            ka = _cents((e.get("A") or {}).get("mean"))
            kb = _cents((e.get("B") or {}).get("mean"))
            table.append(f"| `{v.id}`{'（持有到结算对照）' if v.control else ''} | {_row(r)} | {ka} / {kb} | {'✓' if passed else '✗'} |")
        fam = pd.Series([v.family for _, v in fz])
        L += ["", "| 族 | 冻结 | 通过 |", "|---|---:|---:|"]
        for f in fam.unique():
            sel = (fam == f).to_numpy()
            L.append(f"| {f} | {int(sel.sum())} | {int(ok[sel].sum())} |")
        L += ["", "列：笔数 / 市场 | 每份 ± 标准误（t，p） | 平均持有秒 | 卖不掉、按结算算的占比 | 8/07 前 / 起的每份"
                  "（点价 / TWAP 结算） | kacho A / B 每份 | 通过", ""]
        means = np.array([r["mean"] for _, _, r, _, _ in rows], float)
        ev = np.isfinite(means)
        L[len(L) - 3:len(L) - 3] = ["", f"评估到的 {int(ev.sum())} 个冻结变体中，每份 > 0 的 {int((means[ev] > 0).sum())} 个；"
                                       f"每份中位数 {_cents(float(np.median(means[ev])) if ev.any() else np.nan)}。"]
        if len(table) > 32:
            L += [f"t 最高的 20 个：", ""] + table[:22] + ["", f"<details><summary>全部 {k} 个冻结变体（按 t 排序）</summary>",
                                                        ""] + table + ["", "</details>"]
        else:
            L += table
    L += ["", "## 各族 kacho A 段最好的 10 个在这里的表现（只作描述，不参与判定）", "",
          "这些变体按 kacho A 段 t 值挑出（不论是否通过），在这里的数字不能当作检验；"
          "冻结名单为空时这是唯一的结果，同样不下结论。", "",
          "列同上（没有“通过”列）。"]
    by_fam = {}
    for e, v in dz:
        by_fam.setdefault(v.family, []).append((e, v))
    for fam, lst in by_fam.items():
        L += ["", f"### {fam}", "", "| 变体 | 笔数 / 市场 | 每份 | 持有 | 卖不掉 | 8/07 前 / 起 | kacho A / B |",
              "|---|---:|---|---:|---:|---|---|"]
        for e, v in lst:
            r = by_id.loc[v.id]
            ka, kb = _cents((e.get("A") or {}).get("mean")), _cents((e.get("B") or {}).get("mean"))
            note = "（持有到结算对照）" if v.control else ""
            if not r["n"] and v.family == F6 and not info["preds"]:
                L.append(f"| `{v.name}`{note} | 没有预测，未评估 | | | | | {ka} / {kb} |")
            else:
                L.append(f"| `{v.name}`{note} | {_row(r)} | {ka} / {kb} |")
    return L


# ----------------------------------------------------------------------------------------------
# CLI


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--days", type=int, default=None, help="only the last N daily archives")
    ap.add_argument("--frozen", default="real/roundtrip-frozen.json")
    ap.add_argument("--out", default="real/roundtrip-hf.md")
    ap.add_argument("--preds", default=None, help="family-6 predictions (factors_study.pkl / parquet / csv)")
    ap.add_argument("--dataset", default=None)
    ap.add_argument("--local", nargs="*", default=None, help="daily archives already on disk (not downloaded or deleted)")
    ap.add_argument("--fill-change-ms", type=int, default=None,
                    help="strict check: the fill snapshot's top of book changed at most this long ago (default 5000)")
    ap.add_argument("--max-recv-delay-ms", type=int, default=None,
                    help="strict check: ignore Binance seconds whose latest print reached the recorder later than this")
    a = ap.parse_args(argv)
    global FILL_CHANGE_MAX_MS, MAX_RECV_DELAY_MS
    if a.fill_change_ms is not None:
        FILL_CHANGE_MAX_MS = a.fill_change_ms
    if a.max_recv_delay_ms is not None:
        MAX_RECV_DELAY_MS = a.max_recv_delay_ms
    workdir = Path(a.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    if a.dataset and a.dataset != cross.DS:
        cross.set_dataset(a.dataset)
    if a.local is not None:
        arcs = [(Path(p).name, p) for p in sorted(a.local)]
        download = False
    else:
        arcs = [(name, None) for name, _ in cross.archives(cross.fetch("MANIFEST.txt").decode())]
        download = True
    if a.days:
        arcs = arcs[-int(a.days):]
    run(arcs, a.out, a.frozen, preds_path=a.preds, workdir=workdir, download=download,
        log=lambda s: print(s, flush=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
