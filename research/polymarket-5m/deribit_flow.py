"""Deribit BTC option flow -> dealer gamma, customer delta flow, put/call, ATM IV and pin proxies
on a 5-minute UTC grid (factor 8 of FACTORS.md; market data only, no keys, no orders).

Data. Every BTC option trade (inverse BTC-DDMMMYY-STRIKE-C/P options, currency=BTC kind=option)
from history.deribit.com's public get_last_trades_by_currency_and_time, 2026-02-28 .. 2026-09-30.
Pages of 1,000 trades, sorted ascending; the next page starts AT the last trade's timestamp (not
1 ms past it, so trades that share that millisecond are not lost) and pages are deduplicated by
trade_id; a page whose trades all sit on its start millisecond advances 1 ms. One parquet per UTC
day under <cache>/deribit/ is written atomically when the day is complete, so a rerun resumes at
the first missing day. Calls sleep between pages and retry with exponential backoff on HTTP 429,
5xx, Deribit's rate-limit error and connection errors.

Grid. known_at = every 5-minute UTC boundary T from 2026-03-07 00:00 (the first T with seven full
days of trades behind it) to 2026-10-01 00:00. Every value at T uses only trades with timestamp
< T (a trade stamped exactly T is not known at T).

Proxy assumptions (Deribit publishes no historical positions, so these are approximations):
- Who is the customer. The aggressor (Deribit's `direction`, the taker side) is the customer and
  the dealer is the passive side. A taker buy of 1 contract makes the dealer short 1 contract.
  Block trades (block_trade_id) and combo legs keep the direction Deribit reports for them; the
  column dealer_gamma_noblock_usd_1pct drops block trades as a variant.
- Starting positions. Deribit's historical open interest is not available, so a dealer position
  is the dealer side of the trailing 7 days of trades in that instrument only:
  pos_i(T) = - sum(sign x amount) over trades in [T - 7 d, T). Older trades are forgotten even if
  the position is still open, and positions closed by an opposite trade more than 7 days apart
  are double counted. An instrument is dropped at its expiry (08:00 UTC on its date); 1 contract
  = 1 BTC.
- Greeks. Black-Scholes with r = q = 0 on the Deribit index (the expiry's forward/basis is ignored),
  ACT/365 year. Dealer gamma at T uses the CURRENT index price S(T) (the index_price of the last
  trade before T, any instrument; S_age_s says how old, up to a few minutes in quiet spells), the
  instrument's last traded IV before T (looked up from the first grid time of T's UTC day minus
  7 days, so up to 8 days old) and tau = expiry - T
  (no floor; on the grid tau >= 5 min). Deribit reports IV 0 or 999 when it has none; IVs outside
  1%..400% are treated as missing. If an open instrument has no valid IV in the window, the last
  known ATM IV (below) is used, else the last valid IV of any trade. Dollar gamma per 1% move:
      dealer_gamma_usd_1pct = sum_i pos_i x Gamma_i x S^2 x 0.01
  i.e. the change in the dealers' dollar delta when BTC moves 1%. Negative = dealers short gamma
  (their hedging buys rallies and sells drops); positive = long gamma (they lean against moves).
  dealer_gamma_7d_usd_1pct keeps only expiries within 7 days of T; dealer_gamma_ex0dte_usd_1pct
  (an extra) drops expiries within 24 h of T, whose gamma explodes into each 08:00 expiry.
- Customer delta flow: sum of sign x amount x BS delta at trade time (trade's IV and index price,
  tau = expiry - trade time), in BTC, over [T - 1 h, T) and [T - 24 h, T). Trades without a valid
  IV are left out of the delta flow.
- pc_ratio_24h: put contracts / call contracts traded in [T - 24 h, T).
- atm_iv_1h: median IV (fraction, 0.55 = 55%) of trades in [T - 1 h, T) whose strike is within 2%
  of the trade's index price (|K / S_trade - 1| <= 0.02), all expiries; NaN if there are none.
- Pin proxies: pin_dist_1k_pct / pin_dist_5k_pct = |S - nearest multiple of 1,000 / 5,000| / S in
  %, the *_signed variants keep the sign (S above the strike is positive); hours_to_fri_expiry =
  hours from T to the next Friday 08:00 UTC strictly after T.

How good is the 7-day proxy? Against Deribit's live open interest on 2026-10-04 08:41 UTC (and
the trades up to then), the proxy's gross |positions| were 53.5k BTC = 15% of the 349k BTC of OI
(20-90% for expiries within a month, 4-11% for the Dec/Mar/Jun quarterlies), 22.5% of OI's dollar
gamma, Spearman +0.66 across instruments, and 13 instruments had |proxy| > OI (closing trades
counted as opening ones). It is a flow measure of the last week, not a full position book.

    python deribit_flow.py download [--cache DIR] [--start 2026-02-28] [--end 2026-09-30] [--workers 3]
    python deribit_flow.py coverage [--cache DIR]          # trades per day and intraday gaps
    python deribit_flow.py build    [--cache DIR] [--out DIR/deribit_5m.parquet]
    python deribit_flow.py check    [--cache DIR] [--klines DIR] [--dvol dvol.csv]  # periods A and B only
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.special import ndtr

URL = "https://history.deribit.com/api/v2/public/get_last_trades_by_currency_and_time"
SCRATCH = Path("/tmp/claude-0/-home-user-whole-web-station-clone/d12980d9-5808-53a6-9da0-aeb3bb8bdfb4/scratchpad")
CACHE = SCRATCH / "factors"
START_DAY, END_DAY = "2026-02-28", "2026-09-30"
GRID_START, GRID_END = "2026-03-07", "2026-10-01"  # known_at range, both ends included

MS_S = 1000
MS_H = 3_600_000
MS_DAY = 86_400_000
MS_YEAR = 365 * MS_DAY
GRID_MS = 300_000
WINDOW_MS = 7 * MS_DAY
FRI_0800_MS = (MS_DAY + 8 * MS_H)  # 1970-01-02 (a Friday) 08:00 UTC
WEEK_MS = 7 * MS_DAY
MONTHS = {m: i + 1 for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"))}
KEEP = ("trade_id", "timestamp", "instrument_name", "direction", "amount", "iv", "index_price",
        "price", "mark_price", "block_trade_id", "combo_id", "liquidation")


# ---------------------------------------------------------------- download

class RateLimited(Exception):
    pass


def fetch_page(session, start_ms, end_ms, count=1000, retries=8, base_sleep=1.0, timeout=60):
    """One page of BTC option trades in [start_ms, end_ms], ascending; returns (trades, has_more)."""
    params = {"currency": "BTC", "kind": "option", "start_timestamp": int(start_ms),
              "end_timestamp": int(end_ms), "count": count, "sorting": "asc"}
    err = None
    for k in range(retries):
        try:
            r = session.get(URL, params=params, timeout=timeout)
            if r.status_code == 429 or r.status_code >= 500:
                raise RateLimited(f"HTTP {r.status_code}")
            body = r.json()
            if "error" in body:
                code = body["error"].get("code")
                if code in (10028, 10040, 10041, 11098, -32000, 10047) or r.status_code >= 400:
                    raise RateLimited(f"deribit error {body['error']}")
            r.raise_for_status()
            res = body["result"]
            return res["trades"], bool(res.get("has_more"))
        except Exception as e:  # noqa: BLE001 - network, JSON and rate-limit errors are retried
            err = e
            time.sleep(base_sleep * 2 ** k)
    raise RuntimeError(f"page {start_ms}..{end_ms} failed after {retries} tries: {err}")


def fetch_range(fetch, start_ms, end_ms, sleep=0.15):
    """All trades in [start_ms, end_ms], paging forward and deduplicating by trade_id. `fetch`
    is (start, end) -> (trades, has_more). Returns (list of trade dicts sorted by time, pages)."""
    seen, out, pages, cur = set(), [], 0, start_ms
    while True:
        trades, more = fetch(cur, end_ms)
        pages += 1
        for t in trades:
            if t["trade_id"] not in seen:
                seen.add(t["trade_id"])
                out.append(t)
        if not trades or not more:
            break
        last = int(trades[-1]["timestamp"])
        # restart AT the last millisecond (more trades may share it); if the whole page sat on the
        # start millisecond, step 1 ms past it so the loop always moves forward
        cur = last if last > cur else cur + 1
        if cur > end_ms:
            break
        if sleep:
            time.sleep(sleep)
    out.sort(key=lambda t: (int(t["timestamp"]), int(t["trade_id"]) if str(t["trade_id"]).isdigit() else 0))
    return out, pages


def trades_frame(trades):
    rows = {k: [t.get(k) for t in trades] for k in KEEP}
    d = pd.DataFrame(rows)
    d["trade_id"] = pd.to_numeric(d["trade_id"], errors="coerce").astype("Int64")
    d["timestamp"] = d["timestamp"].astype("int64") if len(d) else d["timestamp"]
    for c in ("amount", "iv", "index_price", "price", "mark_price"):
        d[c] = pd.to_numeric(d[c], errors="coerce").astype(float)
    for c in ("instrument_name", "direction", "block_trade_id", "combo_id", "liquidation"):
        d[c] = d[c].astype("string")
    return d


def day_path(cache, day):
    return Path(cache) / "deribit" / f"{day}.parquet"


def download_day(day, cache=CACHE, fetch=None, sleep=0.15):
    """Download one UTC day unless its file exists; returns that day's summary dict."""
    p = day_path(cache, day)
    meta = p.with_suffix(".json")
    if p.exists() and meta.exists():
        return json.loads(meta.read_text())
    if fetch is None:
        import requests
        s = requests.Session()
        fetch = lambda a, b: fetch_page(s, a, b)  # noqa: E731
    t0 = int(pd.Timestamp(day, tz="UTC").value // 10**6)
    trades, pages = fetch_range(fetch, t0, t0 + MS_DAY - 1, sleep=sleep)
    d = trades_frame(trades)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    d.to_parquet(tmp, index=False)
    os.replace(tmp, p)
    ts = np.sort(d["timestamp"].to_numpy(np.int64)) if len(d) else np.array([], np.int64)
    edges = np.concatenate([[t0], ts, [t0 + MS_DAY]])
    gaps = np.diff(edges)
    m = {"day": day, "trades": int(len(d)), "pages": pages, "contracts": float(d["amount"].sum()),
         "block_trades": int(d["block_trade_id"].notna().sum()) if len(d) else 0,
         "max_gap_min": float(gaps.max() / 60000), "gaps_over_30min": int((gaps > 30 * 60000).sum())}
    meta.write_text(json.dumps(m))
    return m


def days_between(start, end):
    return [d.strftime("%Y-%m-%d") for d in pd.date_range(start, end, freq="D")]


def download(start=START_DAY, end=END_DAY, cache=CACHE, workers=3, sleep=0.15):
    days = days_between(start, end)
    lock = threading.Lock()
    out = []

    def one(day):
        m = download_day(day, cache, sleep=sleep)
        with lock:
            print(f"{day}: {m['trades']:>6} trades, {m['pages']:>3} pages, max gap {m['max_gap_min']:.1f} min",
                  flush=True)
        return m

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(one, d) for d in days]
        for f in as_completed(futs):
            out.append(f.result())
    return pd.DataFrame(out).sort_values("day").reset_index(drop=True)


def coverage(cache=CACHE, start=START_DAY, end=END_DAY):
    rows = []
    for day in days_between(start, end):
        meta = day_path(cache, day).with_suffix(".json")
        rows.append(json.loads(meta.read_text()) if meta.exists() else {"day": day, "trades": np.nan})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- trades -> arrays

IV_MIN, IV_MAX = 1.0, 400.0  # Deribit reports 0 and 999 when it has no implied vol; those are invalid


def parse_instruments(names):
    """BTC-DDMMMYY-STRIKE-C/P -> DataFrame(expiry_ms, strike, is_call); unparseable rows are NaN."""
    names = pd.Series(pd.unique(pd.Series(names, dtype="string")), dtype="string")
    m = names.str.extract(r"^BTC-(\d{1,2})([A-Z]{3})(\d{2})-(\d+(?:\.\d+)?)-([CP])$")
    ok = m.notna().all(axis=1) & m[1].isin(list(MONTHS))
    out = pd.DataFrame({"name": names, "expiry_ms": np.nan, "strike": np.nan, "is_call": False})
    if ok.any():
        mm = m[ok]
        date = np.array([f"{2000 + int(y):04d}-{MONTHS[mo]:02d}-{int(dd):02d}"
                         for dd, mo, y in zip(mm[0], mm[1], mm[2])], dtype="datetime64[ms]")
        out.loc[ok, "expiry_ms"] = date.astype(np.int64).astype(float) + 8 * MS_H  # 08:00 UTC
        out.loc[ok, "strike"] = mm[3].astype(float).to_numpy()
        out.loc[ok, "is_call"] = (mm[4] == "C").to_numpy()
    return out


def prepare(raw):
    """Raw cached trades -> sorted, deduplicated frame with ts, inst (int code), expiry, strike,
    is_call, side (+1 taker buy / -1 taker sell), amount, iv (fraction, NaN if invalid), S, block."""
    d = raw.drop_duplicates("trade_id").copy()
    info = parse_instruments(d["instrument_name"])
    info = info.dropna(subset=["expiry_ms"]).reset_index(drop=True)
    code = pd.Series(np.arange(len(info)), index=info["name"].to_numpy())
    d = d[d["instrument_name"].isin(code.index)]
    inst = code.reindex(d["instrument_name"].to_numpy()).to_numpy(np.int64)
    iv = d["iv"].to_numpy(float)
    iv = np.where((iv >= IV_MIN) & (iv <= IV_MAX), iv / 100.0, np.nan)
    side = np.where(d["direction"].to_numpy(str) == "buy", 1.0, -1.0)
    t = pd.DataFrame({"ts": d["timestamp"].to_numpy(np.int64), "inst": inst,
                      "expiry": info["expiry_ms"].to_numpy(np.int64)[inst],
                      "strike": info["strike"].to_numpy(float)[inst],
                      "is_call": info["is_call"].to_numpy(bool)[inst],
                      "side": side, "amount": d["amount"].to_numpy(float), "iv": iv,
                      "S": d["index_price"].to_numpy(float),
                      "block": d["block_trade_id"].notna().to_numpy(bool),
                      "trade_id": d["trade_id"].to_numpy(np.int64)})
    t = t[np.isfinite(t["S"]) & (t["S"] > 0) & np.isfinite(t["amount"])]
    t = t.sort_values(["ts", "trade_id"], kind="stable").reset_index(drop=True)
    t.attrs["instruments"] = info
    return t


def load_trades(cache=CACHE, start=START_DAY, end=END_DAY):
    parts = [pd.read_parquet(day_path(cache, d)) for d in days_between(start, end) if day_path(cache, d).exists()]
    return prepare(pd.concat(parts, ignore_index=True))


def bs_greeks(S, K, tau, sigma, is_call):
    """Black-Scholes delta and gamma with r = q = 0 (tau in years, sigma a fraction). NaN where
    tau <= 0 or sigma is not positive."""
    S, K, tau, sigma = (np.asarray(x, float) for x in (S, K, tau, sigma))
    with np.errstate(divide="ignore", invalid="ignore"):
        sq = sigma * np.sqrt(tau)
        bad = ~((tau > 0) & (sigma > 0) & (S > 0) & (K > 0))
        d1 = (np.log(S / K) + 0.5 * sigma ** 2 * tau) / sq
        nd1 = ndtr(d1)
        delta = np.where(is_call, nd1, nd1 - 1.0)
        gamma = np.exp(-0.5 * d1 ** 2) / np.sqrt(2 * np.pi) / (S * sq)
    delta = np.where(bad, np.nan, delta)
    gamma = np.where(bad, np.nan, gamma)
    return delta, gamma


def hours_to_friday(t_ms):
    """Hours from t to the next Friday 08:00 UTC strictly after t."""
    h = (FRI_0800_MS - np.asarray(t_ms, np.int64)) % WEEK_MS
    h = np.where(h == 0, WEEK_MS, h)
    return h / MS_H


def pin_distance(S, step):
    """Signed distance of S from the nearest multiple of `step`, in % of S (above = positive)."""
    S = np.asarray(S, float)
    return 100.0 * (S - step * np.round(S / step)) / S


# ---------------------------------------------------------------- the 5-minute grid

def grid_times(start=GRID_START, end=GRID_END):
    a = int(pd.Timestamp(start, tz="UTC").value // 10**6)
    b = int(pd.Timestamp(end, tz="UTC").value // 10**6)
    return np.arange(a, b + 1, GRID_MS, dtype=np.int64)


def _window_sums(bucket, values, T, width_ms, b0):
    """Sum of `values` over trades with bucket in [T - width, T) for every grid time T (all on
    the 5 min grid); `bucket` is ts // GRID_MS, b0 its origin."""
    nb = int(T.max() // GRID_MS - b0) + 1
    s = np.bincount(bucket - b0, weights=values, minlength=nb)[:nb]
    c = np.concatenate([[0.0], np.cumsum(s)])
    j = T // GRID_MS - b0
    lo = np.clip(j - width_ms // GRID_MS, 0, None)
    return c[np.clip(j, 0, nb)] - c[lo]


def _last_before(ts, values, T):
    """values of the last trade with ts < T, and its age in ms (NaN if none)."""
    i = np.searchsorted(ts, T, side="left") - 1
    ok = i >= 0
    v = np.full(len(T), np.nan)
    age = np.full(len(T), np.nan)
    v[ok] = values[i[ok]]
    age[ok] = T[ok] - ts[i[ok]]
    return v, age


def _rolling_median(ts, values, T, width_ms):
    lo = np.searchsorted(ts, T - width_ms, side="left")
    hi = np.searchsorted(ts, T, side="left")
    out = np.full(len(T), np.nan)
    n = hi - lo
    for k in np.flatnonzero(n > 0):
        out[k] = np.median(values[lo[k]:hi[k]])
    return out, n


def dealer_gamma(tr, T, S_T, iv_fallback, window_ms=WINDOW_MS, near_ms=7 * MS_DAY):
    """Dealer dollar gamma per 1% at each grid time T (see the module docstring), processed one
    UTC day of grid times at a time. Returns a dict of arrays: all expiries, expiries within
    `near_ms`, expiries more than a day away, block trades excluded, open instruments, gross and
    net dealer contracts, and the number of open instruments that needed the IV fallback."""
    ts = tr["ts"].to_numpy(np.int64)
    inst = tr["inst"].to_numpy(np.int64)
    expiry = tr["expiry"].to_numpy(np.int64)
    flow = (tr["side"] * tr["amount"]).to_numpy(float)  # customer signed contracts
    noblock = ~tr["block"].to_numpy(bool)
    iv = tr["iv"].to_numpy(float)
    info_strike = tr.groupby("inst")["strike"].first()
    info_call = tr.groupby("inst")["is_call"].first()
    info_exp = tr.groupby("inst")["expiry"].first()
    keys = ("all", "near", "ex0dte", "noblock", "n_open", "gross", "n_fallback", "net_contracts")
    out = {k: np.full(len(T), np.nan) for k in keys}
    nwin = window_ms // GRID_MS
    day_of = T // MS_DAY
    for day in np.unique(day_of):
        kk = np.flatnonzero(day_of == day)
        Tk = T[kk]
        lo_ms = Tk[0] - window_ms
        a = np.searchsorted(ts, lo_ms, side="left")
        b = np.searchsorted(ts, Tk[-1], side="left")
        sel = np.arange(a, b)
        sel = sel[expiry[sel] > Tk[0]]
        if not len(sel):
            for k in keys:
                out[k][kk] = 0.0
            continue
        u, ii = np.unique(inst[sel], return_inverse=True)
        b0 = lo_ms // GRID_MS
        nb = int((Tk[-1] - lo_ms) // GRID_MS)
        bk = (ts[sel] // GRID_MS - b0).astype(np.int64)
        cell = ii * nb + bk
        F = np.bincount(cell, weights=flow[sel], minlength=len(u) * nb).reshape(len(u), nb)
        Fn = np.bincount(cell, weights=flow[sel] * noblock[sel], minlength=len(u) * nb).reshape(len(u), nb)
        C = np.concatenate([np.zeros((len(u), 1)), np.cumsum(F, axis=1)], axis=1)
        Cn = np.concatenate([np.zeros((len(u), 1)), np.cumsum(Fn, axis=1)], axis=1)
        # last valid IV per instrument at the end of each bucket (the latest trade in the cell;
        # `sel` is in time order), carried forward
        L = np.full(len(u) * nb, np.nan)
        v = np.flatnonzero(np.isfinite(iv[sel]))
        rev = v[::-1]
        _, first = np.unique(cell[rev], return_index=True)
        last = rev[first]
        L[cell[last]] = iv[sel][last]
        L = L.reshape(len(u), nb)
        idx = np.where(np.isfinite(L), np.arange(nb)[None, :], -1)
        np.maximum.accumulate(idx, axis=1, out=idx)
        Lf = np.where(idx >= 0, np.take_along_axis(L, np.maximum(idx, 0), axis=1), np.nan)
        K = info_strike.reindex(u).to_numpy(float)
        call = info_call.reindex(u).to_numpy(bool)
        ex = info_exp.reindex(u).to_numpy(np.int64)
        for m, t in zip(kk, Tk):
            j = int((t - lo_ms) // GRID_MS)
            cust = C[:, j] - C[:, j - nwin]
            cust_nb = Cn[:, j] - Cn[:, j - nwin]
            live = (ex > t) & ((np.abs(cust) > 1e-9) | (np.abs(cust_nb) > 1e-9))
            sig = Lf[live, j - 1] if j >= 1 else np.full(live.sum(), np.nan)
            fb = ~np.isfinite(sig)
            sig = np.where(fb, iv_fallback[m], sig)
            tau = (ex[live] - t) / MS_YEAR
            _, g = bs_greeks(S_T[m], K[live], tau, sig, call[live])
            g = np.nan_to_num(g)
            dollar = g * S_T[m] ** 2 * 0.01
            dpos = -cust[live]
            out["all"][m] = np.sum(dpos * dollar)
            out["near"][m] = np.sum(np.where(ex[live] - t <= near_ms, dpos * dollar, 0.0))
            out["ex0dte"][m] = np.sum(np.where(ex[live] - t > MS_DAY, dpos * dollar, 0.0))
            out["noblock"][m] = np.sum(-cust_nb[live] * dollar)
            out["n_open"][m] = int(np.count_nonzero(np.abs(cust[live]) > 1e-9))
            out["gross"][m] = float(np.abs(cust[live]).sum())
            out["net_contracts"][m] = float(dpos.sum())
            out["n_fallback"][m] = int(np.count_nonzero(fb & (np.abs(cust[live]) > 1e-9)))
    return out


def build(tr, T=None):
    """The 5-minute factor frame, indexed by known_at (UTC); see the module docstring."""
    T = grid_times() if T is None else np.asarray(T, np.int64)
    ts = tr["ts"].to_numpy(np.int64)
    S_T, S_age = _last_before(ts, tr["S"].to_numpy(float), T)
    # customer delta flow at trade time
    tau_tr = (tr["expiry"].to_numpy(np.int64) - ts) / MS_YEAR
    delta, _ = bs_greeks(tr["S"], tr["strike"], tau_tr, tr["iv"], tr["is_call"].to_numpy(bool))
    dflow = np.nan_to_num(tr["side"].to_numpy() * tr["amount"].to_numpy() * delta)
    bucket = ts // GRID_MS
    b0 = int(min(bucket.min(), T.min() // GRID_MS - 300))
    amt = tr["amount"].to_numpy(float)
    call = tr["is_call"].to_numpy(bool)
    f = pd.DataFrame(index=pd.DatetimeIndex(pd.to_datetime(T, unit="ms", utc=True), name="known_at"))
    f["S"] = S_T
    f["S_age_s"] = S_age / MS_S
    # ATM IV over the past hour
    mny = tr["strike"].to_numpy(float) / tr["S"].to_numpy(float) - 1.0
    atm = (np.abs(mny) <= 0.02) & np.isfinite(tr["iv"].to_numpy(float))
    atm_iv, n_atm = _rolling_median(ts[atm], tr["iv"].to_numpy(float)[atm], T, MS_H)
    f["atm_iv_1h"] = atm_iv
    f["n_atm_1h"] = n_atm
    # IV fallback for open instruments without a valid IV in the window: the last ATM IV, else
    # the last valid IV of any trade before T, else 50% (all causal)
    ivv = np.isfinite(tr["iv"].to_numpy(float))
    last_iv, _ = _last_before(ts[ivv], tr["iv"].to_numpy(float)[ivv], T)
    fallback = pd.Series(atm_iv).ffill().to_numpy()
    fallback = np.where(np.isfinite(fallback), fallback, np.where(np.isfinite(last_iv), last_iv, 0.5))
    g = dealer_gamma(tr, T, S_T, fallback)
    f["dealer_gamma_usd_1pct"] = g["all"]
    f["dealer_gamma_7d_usd_1pct"] = g["near"]
    f["dealer_gamma_ex0dte_usd_1pct"] = g["ex0dte"]
    f["dealer_gamma_noblock_usd_1pct"] = g["noblock"]
    f["dealer_net_contracts"] = g["net_contracts"]
    f["dealer_gross_contracts"] = g["gross"]
    f["n_open_instruments"] = g["n_open"]
    f["n_iv_fallback"] = g["n_fallback"]
    f["cust_delta_flow_1h"] = _window_sums(bucket, dflow, T, MS_H, b0)
    f["cust_delta_flow_24h"] = _window_sums(bucket, dflow, T, MS_DAY, b0)
    cv = _window_sums(bucket, np.where(call, amt, 0.0), T, MS_DAY, b0)
    pv = _window_sums(bucket, np.where(call, 0.0, amt), T, MS_DAY, b0)
    f["call_contracts_24h"] = cv
    f["put_contracts_24h"] = pv
    with np.errstate(divide="ignore", invalid="ignore"):
        f["pc_ratio_24h"] = np.where(cv > 0, pv / cv, np.nan)
    f["n_trades_1h"] = _window_sums(bucket, np.ones(len(ts)), T, MS_H, b0)
    f["n_trades_24h"] = _window_sums(bucket, np.ones(len(ts)), T, MS_DAY, b0)
    for step, name in ((1000, "1k"), (5000, "5k")):
        d = pin_distance(S_T, step)
        f[f"pin_dist_{name}_pct"] = np.abs(d)
        f[f"pin_dist_{name}_signed_pct"] = d
    f["hours_to_fri_expiry"] = hours_to_friday(T)
    return f


# ---------------------------------------------------------------- sanity checks (A + B only)

# FACTORS.md periods; C (2026-08-16 .. 09-30) is the lock box and is not looked at here
PERIODS = (("A", "2026-03-24", "2026-07-01"), ("B", "2026-07-01", "2026-08-16"))


def realised(klines, T):
    """From Binance 1 s klines (regime.load_klines, indexed by open second, close known one second
    later): past/forward 1 h log return and realised vol (annualised) around each T (ms)."""
    lp = np.log(klines["close"].to_numpy(float))
    r = np.diff(lp, prepend=np.nan)
    r2 = np.concatenate([[0.0], np.cumsum(np.nan_to_num(r) ** 2)])  # r2[k] = sum r^2 of seconds < k
    sec0 = int(klines.index[0])
    k = T // 1000 - sec0  # index of the kline that opens at T
    n = len(lp)
    ok = (k - 3601 >= 0) & (k + 3600 <= n)
    out = pd.DataFrame(index=np.arange(len(T)))
    ann = np.sqrt(24 * 365)
    for name, a, b in (("rv_past_1h", -3600, 0), ("rv_fwd_1h", 0, 3600)):
        v = np.full(len(T), np.nan)
        v[ok] = np.sqrt(r2[k[ok] + b] - r2[k[ok] + a]) * ann
        out[name] = v
    for name, a, b in (("ret_past_1h", -3601, -1), ("ret_fwd_1h", -1, 3599)):
        v = np.full(len(T), np.nan)
        v[ok] = lp[k[ok] + b] - lp[k[ok] + a]
        out[name] = v
    return out


def _period(idx):
    day = idx.strftime("%Y-%m-%d")
    out = np.full(len(day), "", dtype=object)
    for name, lo, hi in PERIODS:
        out[(day >= lo) & (day < hi)] = name
    return out


def _spearman(x, y):
    from scipy.stats import spearmanr
    m = np.isfinite(x) & np.isfinite(y)
    if m.sum() < 10:
        return np.nan, np.nan, int(m.sum())
    r = spearmanr(x[m], y[m])
    return float(r.statistic), float(r.pvalue), int(m.sum())


def _resid(y, xs):
    m = np.isfinite(y) & np.all([np.isfinite(x) for x in xs], axis=0)
    X = np.column_stack([np.ones(m.sum())] + [x[m] for x in xs])
    b, *_ = np.linalg.lstsq(X, y[m], rcond=None)
    out = np.full(len(y), np.nan)
    out[m] = y[m] - X @ b
    return out


def check(f, klines, dvol=None):
    """Exploratory sanity table on non-overlapping hourly samples (T at :00) in periods A and B."""
    T = (f.index.astype("int64") // 10**6).to_numpy(np.int64) if f.index.dtype.unit == "ns" else \
        (f.index.as_unit("ms").astype("int64")).to_numpy(np.int64)
    x = pd.concat([f.reset_index(), realised(klines, T)], axis=1).set_index("known_at")
    if dvol is not None:
        d = pd.read_csv(dvol)
        known = d["t"].to_numpy(np.int64) + 60_000  # a 1 min candle is known when it closes
        v = pd.Series(d["close"].to_numpy(float) / 100, index=known).sort_index()
        v = v[~v.index.duplicated()]
        x["dvol"] = v.reindex(T, method="ffill").to_numpy()
    else:
        x["dvol"] = np.nan
    x["period"] = _period(x.index)
    h = x[(x.index.minute == 0) & (x["period"] != "")].copy()
    L = []
    L.append("Dealer gamma ($ per 1% move, millions), hourly samples:")
    for p in ("A", "B"):
        g = h.loc[h["period"] == p]
        for c in ("dealer_gamma_usd_1pct", "dealer_gamma_7d_usd_1pct", "dealer_gamma_noblock_usd_1pct"):
            q = g[c] / 1e6
            L.append(f"  {p} {c:<32} median {q.median():+8.2f}  p10 {q.quantile(.1):+8.2f}  "
                     f"p90 {q.quantile(.9):+8.2f}  share<0 {np.mean(q < 0):.2f}  n {q.notna().sum()}")
    L.append("Spearman with realised vol over the NEXT hour (raw; and residual of log next-1h RV after"
             " log past-1h RV, log DVOL and hour-of-day dummies); and with past-1h RV. Exploratory: the"
             " series are persistent hourly samples, so no p-values are printed (factors.py tests them"
             " with Newey-West):")
    for p in ("A", "B"):
        g = h.loc[h["period"] == p]
        hours = [(g.index.hour == k).astype(float) for k in range(1, 24)]
        res = _resid(np.log(g["rv_fwd_1h"].to_numpy()),
                     [np.log(g["rv_past_1h"].to_numpy())] + hours
                     + ([np.log(g["dvol"].to_numpy())] if g["dvol"].notna().any() else []))
        for c in ("dealer_gamma_usd_1pct", "dealer_gamma_7d_usd_1pct", "dealer_gamma_noblock_usd_1pct",
                  "cust_delta_flow_24h", "pc_ratio_24h", "pin_dist_1k_pct"):
            a = _spearman(g[c].to_numpy(), g["rv_fwd_1h"].to_numpy())
            b = _spearman(g[c].to_numpy(), res)
            c0 = _spearman(g[c].to_numpy(), g["rv_past_1h"].to_numpy())
            L.append(f"  {p} {c:<32} fwdRV {a[0]:+.3f}  resid {b[0]:+.3f}  pastRV {c0[0]:+.3f}  n {a[2]}")
    L.append("By dealer-gamma quintile (cut points from A): mean next-1h RV, mean past-1h RV, "
             "trend continuation = mean sign(past 1h ret) x next 1h ret (bp), Spearman(past, next 1h ret):")
    for c in ("dealer_gamma_usd_1pct", "dealer_gamma_7d_usd_1pct"):
        cut = np.nanquantile(h.loc[h["period"] == "A", c], [.2, .4, .6, .8])
        for p in ("A", "B"):
            g = h.loc[h["period"] == p]
            q = np.searchsorted(cut, g[c].to_numpy(), side="right")
            for k in range(5):
                gg = g[q == k]
                cont = np.sign(gg["ret_past_1h"]) * gg["ret_fwd_1h"] * 1e4
                sp = _spearman(gg["ret_past_1h"].to_numpy(), gg["ret_fwd_1h"].to_numpy())
                lo = "-inf" if k == 0 else f"{cut[k - 1] / 1e6:+.1f}"
                hi = "inf" if k == 4 else f"{cut[k] / 1e6:+.1f}"
                L.append(f"  {c[:-9]:<24} {p} Q{k + 1} ({lo}..{hi} $M)  fwdRV {gg['rv_fwd_1h'].mean():.3f}  "
                         f"pastRV {gg['rv_past_1h'].mean():.3f}  cont {cont.mean():+6.1f} +/-{cont.std() / np.sqrt(max(len(cont), 1)):.1f} bp  "
                         f"rho {sp[0]:+.3f}  n {len(gg)}")
    L.append("Customer net delta flow vs the next-hour return (Spearman; exploratory, no p-values):")
    for p in ("A", "B"):
        g = h.loc[h["period"] == p]
        for c in ("cust_delta_flow_1h", "cust_delta_flow_24h"):
            a = _spearman(g[c].to_numpy(), g["ret_fwd_1h"].to_numpy())
            b = _spearman(g[c].to_numpy(), g["ret_past_1h"].to_numpy())
            L.append(f"  {p} {c:<20} next 1h ret {a[0]:+.3f}  past 1h ret {b[0]:+.3f}  n {a[2]}")
    return "\n".join(L), x


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=("download", "build", "check", "coverage"))
    ap.add_argument("--cache", default=str(CACHE))
    ap.add_argument("--start", default=START_DAY)
    ap.add_argument("--end", default=END_DAY)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--out")
    ap.add_argument("--klines", default=str(SCRATCH / "klines"))
    ap.add_argument("--dvol", default=str(SCRATCH / "dvol.csv"))
    a = ap.parse_args(argv)
    out = Path(a.out) if a.out else Path(a.cache) / "deribit_5m.parquet"
    if a.cmd == "download":
        c = download(a.start, a.end, a.cache, a.workers)
        print(c.to_string())
    elif a.cmd == "coverage":
        c = coverage(a.cache, a.start, a.end)
        print(c.to_string())
        print(c["trades"].describe())
    elif a.cmd == "build":
        tr = load_trades(a.cache, a.start, a.end)
        print(f"{len(tr):,} trades, {tr['inst'].nunique():,} instruments, "
              f"{np.isnan(tr['iv']).mean():.2%} without a valid IV", flush=True)
        f = build(tr)
        f.to_parquet(out)
        print(f.describe().T.to_string())
        print(f"wrote {out} ({len(f):,} rows)")
    else:
        import regime as rg
        f = pd.read_parquet(out)
        text, _ = check(f, rg.load_klines(a.klines), a.dvol if a.dvol and Path(a.dvol).exists() else None)
        print(text)


if __name__ == "__main__":
    sys.exit(main())
