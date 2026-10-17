"""Polymarket BTC up/down markets: official outcomes, the settlement rule of each market, a Binance
proxy of that rule, and real taker entry prices at the window open (market data only, no keys, no
orders). Used by factors.py to score the Polymarket side of FACTORS.md.

Settlement rules (Gamma event descriptions and resolutionSource, checked 2026-10-04):
- 5m  btc-updown-5m-{T}, 15m btc-updown-15m-{T}, 4h btc-updown-4h-{T} (T = window open, epoch s;
  4 h windows open at 00/04/../20 UTC):
  * windows opening before 2026-08-07 00:00 UTC: Chainlink BTC/USD data stream; Up iff the price at
    the end >= the price at the start (point to point);
  * from 2026-08-07 00:00: Chainlink BTC/USD TWAP stream; Up iff the TWAP at the end >= the TWAP at
    the start. 5m used the 30 s TWAP stream for windows opening 08-07 .. 08-13 23:55 and the 60 s
    stream from 08-14 00:00; 15m and 4h the 60 s stream from 08-07 00:00.
- 1h  bitcoin-up-or-down-<month>-<day>-<year>-<h><am|pm>-et: Binance BTC/USDT 1 h candle that opens
  at T; Up iff close >= open (no change in the period).
The window length w of each fetched market is read from its own resolutionSource
(btc-usd-twap-30s / -60s -> 30 / 60, anything else -> 0 = point price); RULES is the fallback.

Official outcome: outcomePrices of the resolved market (Up = 1 or 0; NaN if not resolved).

Binance proxy (spot BTCUSDT 1 s klines, per-second closes forward-filled as in factors.load_spot):
point price at t = the close of the 1 s kline opening at t - 1 (the last price before t);
TWAP_w(t) = mean close of the 1 s klines opening t - w .. t - 1. Up iff proxy(T + h) >= proxy(T).

Real entry price (for the lock box only): the lowest price of the taker BUYs of the wanted outcome
in the first second that has any, within [T, T + ENTRY_WAIT[h]] (data-api.polymarket.com trades,
takerOnly, start/end filter), i.e. what a taker got at the open; NaN if nobody bought that side.

    python pm_outcomes.py fetch  [--cache DIR] [--start 2026-03-24] [--end 2026-10-01]
    python pm_outcomes.py check  [--cache DIR]      # rules by period and proxy agreement (A and B)
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

SCRATCH = Path("/tmp/claude-0/-home-user-whole-web-station-clone/d12980d9-5808-53a6-9da0-aeb3bb8bdfb4/scratchpad")
CACHE = SCRATCH / "factors"
GAMMA = "https://gamma-api.polymarket.com/events"
DATA_API = "https://data-api.polymarket.com/trades"
HMIN = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}
ENTRY_WAIT = {"5m": 5, "15m": 5, "1h": 60, "4h": 60}  # seconds after the open


def ts(day):
    return int(pd.Timestamp(day, tz="UTC").timestamp())


SWITCH = ts("2026-08-07")
SWITCH_5M_60 = ts("2026-08-14")


def rule_window(h, T):
    """Fallback TWAP window (s) of the market opening at T by the rules above; 0 = point price."""
    T = np.asarray(T, np.int64)
    if h == "1h":
        return np.zeros(len(T), np.int64)
    if h == "5m":
        return np.where(T < SWITCH, 0, np.where(T < SWITCH_5M_60, 30, 60)).astype(np.int64)
    return np.where(T < SWITCH, 0, 60).astype(np.int64)


def source_window(src):
    s = str(src or "")
    return 30 if "twap-30s" in s else 60 if "twap-60s" in s else 0


# ---------------------------------------------------------------- slugs

def slug(h, T):
    if h == "1h":
        et = datetime.fromtimestamp(int(T), timezone.utc) - timedelta(hours=4)  # EDT all of 03-08 .. 11-01
        hr = et.hour % 12 or 12
        return f"bitcoin-up-or-down-{et.strftime('%B').lower()}-{et.day}-{et.year}-{hr}{'am' if et.hour < 12 else 'pm'}-et"
    return f"btc-updown-{h}-{int(T)}"


def window_opens(h, start, end):
    """Window opens T with start <= T and T + h <= end (epoch s)."""
    step = HMIN[h] * 60
    lo = ((ts(start) + step - 1) // step) * step
    return np.arange(lo, ts(end) - step + 1, step, dtype=np.int64)


# ---------------------------------------------------------------- http

def http_json(url, tries=6, timeout=30, sleep=time.sleep):
    err = None
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "research"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (400, 404, 422):
                raise
            err = e
        except Exception as e:  # network, proxy, truncated transfer
            err = e
        sleep(1.5 * 2 ** k)
    raise RuntimeError(f"giving up on {url}: {err!r}")


def parse_event(ev):
    """One Gamma event -> dict(T, slug, up, condition_id, token_up, token_down, source, w)."""
    m = ev["markets"][0]
    outs = json.loads(m["outcomes"])
    prices = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")] or [np.nan, np.nan]
    toks = json.loads(m.get("clobTokenIds") or "[]")
    iu, idn = outs.index("Up"), outs.index("Down")
    up = prices[iu] if max(prices) > 0.99 and min(prices) < 0.01 else np.nan
    start = m.get("eventStartTime") or ev.get("startTime")
    src = m.get("resolutionSource") or ev.get("resolutionSource") or ""
    return dict(T=int(pd.Timestamp(start).timestamp()), slug=ev["slug"], up=float(up),
                condition_id=m.get("conditionId"), token_up=toks[iu] if toks else None,
                token_down=toks[idn] if toks else None, source=src, w=source_window(src))


def fetch_batch(slugs, get=http_json):
    q = "&".join("slug=" + urllib.parse.quote(s) for s in slugs) + f"&limit={len(slugs)}"
    return [parse_event(e) for e in get(f"{GAMMA}?{q}")]


def fetch_outcomes(cache=CACHE, horizons=tuple(HMIN), start="2026-03-24", end="2026-10-01", batch=100,
                   workers=4, get=http_json, log=print):
    """Official outcome of every window of every horizon in [start, end), cached in
    cache/pm_outcomes.parquet; windows already resolved in the cache are not asked again."""
    path = Path(cache) / "pm_outcomes.parquet"
    have = pd.read_parquet(path) if path.exists() else pd.DataFrame(columns=["h", "T", "up"])
    rows = [have]
    for h in horizons:
        Ts = window_opens(h, start, end)
        done = set(have.loc[(have["h"] == h) & have["up"].notna(), "T"].astype(np.int64))
        want = [int(T) for T in Ts if int(T) not in done]
        jobs = [[slug(h, T) for T in want[i:i + batch]] for i in range(0, len(want), batch)]
        with ThreadPoolExecutor(workers) as ex:
            got = [r for part in ex.map(lambda s: fetch_batch(s, get), jobs) for r in part]
        d = pd.DataFrame(got)
        if len(d):
            d.insert(0, "h", h)
            rows.append(d)
        log(f"{h}: {len(Ts):,} windows, {len(want):,} asked, {len(d):,} returned, "
            f"{int(d['up'].notna().sum()) if len(d) else 0:,} resolved")
    out = pd.concat([r for r in rows if len(r)], ignore_index=True)
    out["T"] = out["T"].astype(np.int64)
    out = out.sort_values(["h", "T"]).drop_duplicates(["h", "T"], keep="last").reset_index(drop=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(path)
    return out


def load_outcomes(cache=CACHE):
    path = Path(cache) / "pm_outcomes.parquet"
    return pd.read_parquet(path) if path.exists() else None


# ---------------------------------------------------------------- Binance proxy of the rule

def proxy_price(sec0, close, t, w):
    """Point price (w = 0: the close of the 1 s kline opening at t - 1) or TWAP_w (mean close of
    the klines opening t - w .. t - 1) at instants t; NaN outside the data. Known at t."""
    t, w = np.asarray(t, np.int64), np.broadcast_to(np.asarray(w, np.int64), np.shape(t))
    cs = np.concatenate([[0.0], np.cumsum(close)])
    n = len(close)
    out = np.full(len(t), np.nan)
    a = np.where(w > 0, t - w, t - 1) - sec0
    b = t - sec0
    ok = (a >= 0) & (b <= n)
    out[ok] = (cs[b[ok]] - cs[a[ok]]) / (b[ok] - a[ok])
    return out


def proxy_return(h, T, sec0, close, w=None):
    """log(proxy(T + h) / proxy(T)) under the rule of the market opening at T."""
    T = np.asarray(T, np.int64)
    w = rule_window(h, T) if w is None else np.asarray(w, np.int64)
    return np.log(proxy_price(sec0, close, T + HMIN[h] * 60, w) / proxy_price(sec0, close, T, w))


def settlement(h, T, sec0, close, outcomes=None):
    """(up, source, r) at window opens T: the official outcome where the cache has it, else the
    Binance proxy of the market's rule; source 'official' / 'proxy' / '' (neither); r = the
    proxy's log return under the market's rule (for rank statistics against the settlement)."""
    T = np.asarray(T, np.int64)
    w = rule_window(h, T)
    off = np.full(len(T), np.nan)
    if outcomes is not None and len(outcomes):
        o = outcomes[outcomes["h"] == h].drop_duplicates("T").set_index("T")
        off = o["up"].reindex(T).to_numpy(float)
        ww = o["w"].reindex(T).to_numpy(float)
        w = np.where(np.isfinite(ww), ww, w).astype(np.int64)
    r = proxy_return(h, T, sec0, close, w)
    prox = np.where(np.isfinite(r), (r >= 0).astype(float), np.nan)
    up = np.where(np.isfinite(off), off, prox)
    src = np.where(np.isfinite(off), "official", np.where(np.isfinite(prox), "proxy", ""))
    return up, src, r


# ---------------------------------------------------------------- real entry prices

def first_taker_buy(trades, outcome, T, wait):
    """Lowest price among the taker BUYs of `outcome` in the first second with any in [T, T + wait]."""
    b = [t for t in trades if t.get("side") == "BUY" and t.get("outcome") == outcome
         and T <= int(t["timestamp"]) <= T + wait]
    if not b:
        return np.nan, np.nan
    t0 = min(int(t["timestamp"]) for t in b)
    return float(min(float(t["price"]) for t in b if int(t["timestamp"]) == t0)), float(t0 - T)


def entry_prices(rows, cache=CACHE, workers=6, get=http_json, log=print):
    """rows: DataFrame with h, T, side_up, condition_id. Returns it with px (entry price of the
    side bought) and px_dt (seconds after the open); cached in cache/pm_entry.parquet by
    (h, T, side_up)."""
    path = Path(cache) / "pm_entry.parquet"
    have = pd.read_parquet(path) if path.exists() else pd.DataFrame(columns=["h", "T", "side_up", "px", "px_dt"])
    key = ["h", "T", "side_up"]
    rows = rows.copy()
    rows["T"] = rows["T"].astype(np.int64)
    rows["side_up"] = rows["side_up"].astype(bool)
    have = have.astype({"T": np.int64, "side_up": bool}) if len(have) else have
    todo = rows.merge(have[key], on=key, how="left", indicator=True)
    todo = todo[todo["_merge"] == "left_only"]

    def one(r):
        if not isinstance(r.condition_id, str):
            return dict(h=r.h, T=int(r.T), side_up=bool(r.side_up), px=np.nan, px_dt=np.nan)
        wait = ENTRY_WAIT[r.h]
        tr = get(f"{DATA_API}?market={r.condition_id}&limit=500&takerOnly=true&start={int(r.T)}&end={int(r.T) + wait}")
        px, dt = first_taker_buy(tr, "Up" if r.side_up else "Down", int(r.T), wait)
        return dict(h=r.h, T=int(r.T), side_up=bool(r.side_up), px=px, px_dt=dt)

    with ThreadPoolExecutor(workers) as ex:
        new = list(ex.map(one, list(todo.itertuples(index=False))))
    if new:
        have = pd.concat([have, pd.DataFrame(new)], ignore_index=True).drop_duplicates(key, keep="last")
        path.parent.mkdir(parents=True, exist_ok=True)
        have.to_parquet(path)
    log(f"entry prices: {len(rows):,} asked, {len(new):,} fetched")
    return rows.merge(have, on=key, how="left")


# ---------------------------------------------------------------- check (A and B only)

def check(cache=CACHE, end="2026-08-16"):
    """Rule windows by period and agreement of the proxy / point-to-point Binance label with the
    official outcome, windows ending before `end` (C is not looked at)."""
    import factors as F
    o = load_outcomes(cache)
    sec0, close = F.load_spot(cache=cache)
    L = []
    for h in HMIN:
        d = o[(o["h"] == h) & (o["T"] + HMIN[h] * 60 <= ts(end))]
        T = d["T"].to_numpy(np.int64)
        rw = rule_window(h, T)
        L.append(f"{h}: {len(d):,} windows, resolved {d['up'].notna().mean():.4f}; source window == rule: "
                 f"{np.mean(d['w'].to_numpy() == rw):.4f}")
        p2p = proxy_return(h, T, sec0, close, np.zeros(len(T), np.int64)) >= 0
        rule = proxy_return(h, T, sec0, close, d["w"].to_numpy(np.int64)) >= 0
        up = d["up"].to_numpy(float)
        for name, lo, hi in (("A", "2026-03-24", "2026-07-01"), ("B < 08-07", "2026-07-01", "2026-08-07"),
                             ("B >= 08-07", "2026-08-07", end)):
            m = (T >= ts(lo)) & (T < ts(hi)) & np.isfinite(up)
            if m.any():
                L.append(f"   {name:<10} n {m.sum():6,}  point-to-point {np.mean(p2p[m] == up[m]):.4f}  "
                         f"rule proxy {np.mean(rule[m] == up[m]):.4f}  up rate {up[m].mean():.3f}")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=("fetch", "check"))
    ap.add_argument("--cache", default=str(CACHE))
    ap.add_argument("--start", default="2026-03-24")
    ap.add_argument("--end", default="2026-10-01")
    a = ap.parse_args(argv)
    if a.cmd == "fetch":
        o = fetch_outcomes(a.cache, start=a.start, end=a.end)
        print(o.groupby("h").agg(n=("T", "size"), resolved=("up", lambda x: x.notna().mean()),
                                 first=("T", "min"), last=("T", "max")).to_string())
    else:
        print(check(a.cache))


if __name__ == "__main__":
    main()
