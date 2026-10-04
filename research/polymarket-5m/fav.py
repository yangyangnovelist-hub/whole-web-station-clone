"""FAV.md: is the favourite side under-priced near expiry on markets the two passed BTC rules never used
(BTC hourly "above" ladders, BTC hourly up/down, ETH / SOL / XRP daily markets)? One confirmation test of
two rules fixed before the run, with frequency and capacity. Paper research on market data only: no
orders, no keys.

    python fav.py all        [--out real/fav.md]
    python fav.py discover | klines | decide | points | fetch | report      (the same steps one by one)

Reused by import, read-only (nothing in them is edited): resolved.py (Http cache / rate limit, Gamma
event lookup by slug, market rows, hit windows, official outcomes, rs.Spot = per-second klines with the
minute aggregation, trailing sigma and price-before, rs.decide = T* / computed outcome / noon candle
conventions, rs.checkpoints + rs.p_yes = the driftless model incl. reflection for hit markets,
rs.fetch_trades / rs.trade_frame), nearcert.py (rule-text signature nc.rule_sig / nc.rule_reason against
the 2026 BTC rules nc.EXPECTED, nc.kline_unit), calib.py (cb.fetch_windows = the trades of [t, t + 59]
of one checkpoint window, cb.side_rows = both-side rows with share-weighted price, cb.stats = cell
statistics with market-clustered SE and Student-t one-sided p, cb.one_sided_p).

Steps
1. discover (Gamma, closed events whose title day is 2025-09-15 .. 2026-09-30, ET):
   - BTC hourly "above" ladders (series bitcoin-multi-strikes-hourly) and BTC hourly up/down (series
     btc-up-or-down-hourly): every event of the series ending on each UTC day 2025-09-14 .. 2026-10-01
     (Gamma /events?series_id=..&end_date_min=..&end_date_max=.., all pages); every hour of the period
     without an event is then asked by slug in both spellings ("bitcoin-above-on-<month>-<d>[-<y>]-<h>
     <am|pm>-et", "bitcoin-up-or-down-<month>-<d>[-<y>]-<h><am|pm>-et"). The title gives the ET date
     and hour; the market end must be that hour (above: the candle ENDS at the title time) or one hour
     after it (up/down: the candle BEGINS at the title time), either fold on the DST fall-back day.
   - ETH / SOL / XRP daily markets, slugs in both spellings as resolved.py asks BTC's:
     "<coin>-above-on-<d>" (above), "<coin>-price-on-<d>" (range), "what-price-will-<coin>-hit-on-<d>"
     (hit_daily), "<coin>-up-or-down-on-<d>" (updown_day), coin = ethereum / solana / xrp. An event
     whose end is on another date (last year's short slug) is skipped; one on this date but not at
     12:00 ET (noon kinds) / 24:00 ET (hit) is kept and excluded as end_mismatch.
   Market rows come from rs.market_rows (strike / range / direction / window / official result).
2. rules: every market's description is parsed. Daily ETH / SOL / XRP: the coin's own pair (e.g.
   ETH/USDT) is required, no other coin's pair may appear, and the text with the pair written as
   BTC/USDT must give exactly the 2026 BTC signature of its kind (nc.rule_sig / nc.rule_reason with
   nc.EXPECTED: Binance 1m candle, Close at 12:00 ET (noon), above "higher than", range ties to the
   higher bracket, up/down vs the previous day's noon close with ties 50-50, hit = any 1m High / Low
   between 12:00 AM and 11:59 PM ET). BTC hourly: Binance BTC/USDT "1 hour candle", above = Close "higher
   than the price" of the candle that ENDS at the title time; up/down = "close price is greater than or
   equal to the open price" of the candle that BEGINS at the title time. Any difference excludes the
   market ("rule: <features>").
3. klines (data.binance.vision daily zips): 1 s and 1 m klines of ETHUSDT / SOLUSDT / XRPUSDT for UTC
   days 2025-09-14 .. 2026-10-01 (downloaded to <cache>/klines); BTCUSDT from the existing scratch caches
   (klines/, nearcert/klines, resolved/klines1s, resolved/klines1m, nearcert/klines1m), anything missing
   downloaded. Open-time unit per file (us / ms, nc.kline_unit) recorded. Prices are kept as integers
   in the pair's tick: 0.01 USD for BTC / ETH / SOL (cents, as resolved.py) and 0.0001 USD for XRP
   (cents would round XRP's price by up to 0.4 %); the largest |price * scale - integer| is reported.
   Official 1 m klines decide outcomes and touches (rs.Spot.set_minutes, as resolved.py); the 1 s
   aggregates are compared with them minute by minute. BTCUSDT monthly 1 h klines are downloaded only
   to check that the 1 h open / close used for the hourly markets (1 m open of the hour's first minute,
   1 m close of its last minute) are Binance's own.
4. decide: ETH / SOL / XRP daily markets by rs.decide, month by month (rs.Spot of that month's 1 s klines
   with two days around it), strikes passed as strike * scale / 100 because resolved.py multiplies by
   100 (cents); the noon candle convention (open12 / close12) is chosen per coin from ALL its months'
   official outcomes (fewest mismatches, ties open12, as rs.decide) and then applied to every month.
   BTC hourly markets from the official 1 m klines: above = Close of the 1 h candle ending at the end
   (1 m close of end - 60) > strike, the other reading (candle beginning at the end) is computed too
   and the reading with fewer mismatches is used (ties: the text's reading); up/down = Up iff 1 m close
   of end - 60 >= 1 m open of end - 3600. A computed result that differs from the official one, an
   unresolved market or missing klines excludes the market (listed).
5. points: the driftless model at the FAV.md checkpoints, rs.checkpoints on a frame where BTC hourly
   above is resolved.py's 'above' (decision time T* = candle close) and hourly up/down is 'updown_day'
   with the reference = the hour's 1 h open; S = close of the 1 s kline opening at t - 1, sigma = std of
   1 s log returns over [t - 3600, t) scaled by sqrt(time to the decision), hit markets by reflection and
   only while untouched (no 1 s crossing before t). Checkpoints: daily markets 60 / 30 / 10 / 5 / 1
   minutes before the end, hourly markets 30 / 10 / 5 / 1 (FAV.md), and only those inside the market's
   open period: t >= max(startDate, acceptingOrdersTimestamp) of the market (createdAt if both are
   missing).
6. fetch: from the model rows alone (no trade has been looked at), the windows [t, t + 60) at which some
   rule takes a side: rule 1 = a side with model probability in [0.95, 0.99) at a rule-1 checkpoint,
   rule 2 = an above-type market (BTC hourly above, ETH / SOL / XRP daily above) with a side in [0.85,
   0.95) at a rule-2 checkpoint (daily 60 / 30, hourly 30). If these windows exceed the budget (FETCH_BUDGET
   windows ~ 3 hours at 4 requests per second), days are sampled per group, uniformly at random with a
   fixed seed: groups in ascending order of their window count each get min(their windows, remaining
   budget / remaining groups); a group over its share keeps ceil(share / windows * active days) of its
   active ET days (random.Random(SEED + group index).sample), all windows of a kept day. The trades of
   each window are fetched with cb.fetch_windows (data-api start = t, end = t + 59 inclusive; every page
   cached under <cache>/http; <= 4 requests per second over all threads).
7. report (real/fav.md, Chinese): per rule x group, the rows of the in-band side (cb.side_rows: q = the
   side's model probability, p = share-weighted price of that side's trades in [t, t + 60), any taker
   side, result = official 1 / 0 / 0.5), statistics by cb.stats.

Verdict (FAV.md "判定"), per rule x group: pass iff maker pnl per share > 0, market-clustered one-sided
p < 0.05 / (rules x groups) = 0.05 / 10, >= 100 markets with trades, and both time halves > 0.

Conservative choices where FAV.md is silent (written down before the run):
- ETH / SOL / XRP = their DAILY markets only (FAV.md: 日内“高于”、区间、触及、按日涨跌): hit = the daily
  "what-price-will-<coin>-hit-on-<date>" events; the coins' weekly / monthly hit events and their hourly
  ladders are not in FAV.md's list and are not used. A group pools its four kinds for rule 1; rule 2 uses
  its above markets only.
- Rule 2 does not apply to BTC hourly up/down (not an above-type market); the Bonferroni count is still
  rules x groups = 2 x 5 = 10 as FAV.md writes it (threshold 0.005, stricter than 0.05 / 9).
- "Per share" in the verdict is the maker version (result - p, no fee), the quantity FAV.md's buy price
  defines; the taker version (- 0.07 p (1 - p), at the row's share-weighted p, cb.side_rows) is shown
  beside it, and a pass whose taker pnl is <= 0 is said so in the verdict line.
- One-sided p from Student t with G - 1 degrees of freedom (G = markets with trades) on the
  market-clustered t (cb.one_sided_p). ">= 100 markets" = markets WITH TRADES in the cell (clusters).
- A market contributes every checkpoint at which its side is in the band (pooled, clustered by market),
  as NEARCERT.md's history test and CALIB.md's V test did.
- Time halves: the cell's markets with trades are split at the (lower) median of their days (the ET date
  in the title; markets on the median day go to the first half); each half's share-weighted maker pnl
  must be > 0. The halves are the group's own (BTC hourly above exists only
  from 2026-03), not calendar halves of the whole period.
- Per-day figures (markets, shares, cost $, profit $) divide by the group's active ET days (days with at
  least one included market of the group) among the fetched days; for a sampled group that is the
  total over the sampled days / their number = (total / all active days) x (1 / fraction). Shares, cost
  and profit are over every trade printed, i.e. ceilings, not what one buyer could get. Worst
  single-market loss = the most negative net maker $ pnl of one market in the cell (0 if none lost);
  losing markets = markets with a negative net maker $ pnl in the cell.
- Rule check strict (any differing feature, a missing description, the wrong pair or a mention of another
  coin's pair excludes); end_mismatch excludes (the checkpoints are anchored on the end).
- Market open for the checkpoint filter is the later of startDate and acceptingOrdersTimestamp.
- Hourly up/down reference = the hour's 1 h candle open, i.e. the official 1 m open of the hour's first
  minute (known at every checkpoint); its decision time is the end (the candle's close).
- Day of a market: the ET date in its title (hourly above: the date of the end; hourly up/down: the date
  of the start; daily: the date).
- Markets whose computed outcome differs from the official one are excluded and listed, as resolved.py.
- Fetching: only windows with an in-band side are fetched; trades stamped outside [t, t + 60) are
  dropped; a window whose fetch failed after the retries is reported as missing (no trades).
"""
from __future__ import annotations

import argparse
import io
import json
import math
import random
import re
import sys
import time
import urllib.parse
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import calib as cb
import ladder as lad
import nearcert as nc
import resolved as rs

HERE = Path(__file__).resolve().parent
SCRATCH = rs.SCRATCH
CACHE = SCRATCH / "fav"
FIRST, LAST = date(2025, 9, 15), date(2026, 9, 30)
K_FIRST, K_LAST = date(2025, 9, 14), date(2026, 10, 1)            # UTC days of klines
NAN = float("nan")
RATE = 4.0
FEE = 0.07
ALPHA = 0.05
MIN_MK = 100
SEED = 20261004
FETCH_BUDGET = 40_000                                              # windows (~1 request each), ~3 h at 4/s
VISION_D = "https://data.binance.vision/data/spot/daily/klines/{sym}/{iv}/{sym}-{iv}-{d}.zip"
VISION_M = "https://data.binance.vision/data/spot/monthly/klines/{sym}/{iv}/{sym}-{iv}-{m}.zip"

COINS = {"BTC": {"name": "bitcoin", "sym": "BTCUSDT", "scale": 100},
         "ETH": {"name": "ethereum", "sym": "ETHUSDT", "scale": 100},
         "SOL": {"name": "solana", "sym": "SOLUSDT", "scale": 100},
         "XRP": {"name": "xrp", "sym": "XRPUSDT", "scale": 10000}}
GROUPS = ("btc_above_1h", "btc_updown_1h", "eth", "sol", "xrp")
GROUP_CN = {"btc_above_1h": "BTC 每小时高于", "btc_updown_1h": "BTC 每小时涨跌", "eth": "ETH 日内",
            "sol": "SOL 日内", "xrp": "XRP 日内"}
GROUP_COIN = {"btc_above_1h": "BTC", "btc_updown_1h": "BTC", "eth": "ETH", "sol": "SOL", "xrp": "XRP"}
HOURLY = ("above_1h", "updown_1h")
SERIES = {"above_1h": "bitcoin-multi-strikes-hourly", "updown_1h": "btc-up-or-down-hourly"}
HOURLY_PREFIX = {"above_1h": "bitcoin-above-on", "updown_1h": "bitcoin-up-or-down"}
DAILY_KINDS = ("above", "range", "hit_daily", "updown_day")
DAILY_PREFIX = {"above": "{n}-above-on", "range": "{n}-price-on", "hit_daily": "what-price-will-{n}-hit-on",
                "updown_day": "{n}-up-or-down-on"}
ABOVE_KINDS = ("above", "above_1h")
RULES = {"R1": {"lo": 0.95, "hi": 0.99, "daily": (60, 30, 10, 5, 1), "hourly": (30, 10, 5, 1), "kinds": None},
         "R2": {"lo": 0.85, "hi": 0.95, "daily": (60, 30), "hourly": (30,), "kinds": ABOVE_KINDS}}
RULE_CN = {"R1": "规则 1（NEARCERT，[0.95, 0.99)）", "R2": "规则 2（CALIB，高于，[0.85, 0.95)）"}
N_TESTS = len(RULES) * len(GROUPS)                                  # Bonferroni count (10)
DAILY_CHECKS = RULES["R1"]["daily"]
HOURLY_CHECKS = RULES["R1"]["hourly"]
MONTHS = lad.MONTHS


def group_of(coin, kind):
    if coin == "BTC":
        return {"above_1h": "btc_above_1h", "updown_1h": "btc_updown_1h"}.get(kind)
    return coin.lower()


def rule_applies(rule, kind):
    k = RULES[rule]["kinds"]
    return k is None or kind in k


def rule_checks(rule, hourly):
    return RULES[rule]["hourly" if hourly else "daily"]


def bonferroni(alpha=ALPHA, n=N_TESTS):
    return alpha / n


# ===================================================================== klines

def kline_url(sym, iv, d):
    return VISION_D.format(sym=sym, iv=iv, d=d.isoformat())


def kline_file(coin, iv, d, cache=CACHE):
    """Local zip of one day (BTC: the existing scratch caches first)."""
    sym = COINS[coin]["sym"]
    name = f"{sym}-{iv}-{d.isoformat()}.zip"
    if coin == "BTC":
        cands = ([rs.KLINES / name, SCRATCH / "nearcert" / "klines" / name, SCRATCH / "resolved" / "klines1s" / name]
                 if iv == "1s" else
                 [SCRATCH / "resolved" / "klines1m" / name, SCRATCH / "nearcert" / "klines1m" / name])
        for c in cands:
            if c.exists() and c.stat().st_size > 0:
                return c
    return Path(cache) / "klines" / sym / iv / name


def download_klines(http, coins=tuple(COINS), ivs=("1s", "1m"), first=K_FIRST, last=K_LAST, cache=CACHE,
                    workers=4, log=print):
    """Every missing daily zip; returns {coin: [missing day iso, ...]} (a 404 is a missing day)."""
    jobs = [(c, iv, d) for c in coins for iv in ivs for d in rs.days(first, last)
            if not kline_file(c, iv, d, cache).exists()]
    log(f"klines: {len(jobs)} zips to download")
    missing = {}
    t0, n = time.time(), [0]

    def one(job):
        c, iv, d = job
        try:
            http.download(kline_url(COINS[c]["sym"], iv, d), kline_file(c, iv, d, cache))
        except Exception as e:
            return c, iv, d, f"{type(e).__name__}: {e}"
        n[0] += 1
        if n[0] % 100 == 0:
            log(f"  {n[0]}/{len(jobs)} zips, {time.time() - t0:.0f} s")
        return c, iv, d, ""

    with ThreadPoolExecutor(workers) as ex:
        for c, iv, d, err in ex.map(one, jobs):
            if err:
                missing.setdefault(f"{c}-{iv}", []).append(d.isoformat())
                log(f"  {c} {iv} {d}: {err}")
    return missing


def read_zip(path, scale):
    """(open time s, open, high, low, close as integers in 1 / scale USD, max |price * scale - integer|)
    from a Binance kline zip whose open times are in us or ms."""
    with zipfile.ZipFile(path) as z:
        raw = z.read(z.namelist()[0])
    d = pd.read_csv(io.BytesIO(raw), header=None, usecols=[0, 1, 2, 3, 4])
    if not np.issubdtype(d[0].dtype, np.number):  # a header row
        d = d.iloc[1:].astype(float)
    t = d[0].to_numpy(np.int64)
    t = np.where(t > 10 ** 14, t // 1_000_000, t // 1000)
    px = d[[1, 2, 3, 4]].to_numpy(float) * scale
    q = np.rint(px)
    dev = float(np.abs(px - q).max()) if len(q) else 0.0
    q = q.astype(np.int64)
    return t, q[:, 0], q[:, 1], q[:, 2], q[:, 3], dev


def sec_file(coin, d, cache=CACHE):
    return Path(cache) / "sec" / COINS[coin]["sym"] / f"{d.isoformat()}.npz"


def sec_day(coin, d, cache=CACHE):
    """(hi, lo, cl) int32 arrays of the 86400 seconds of UTC day d (0 = no kline), cached as npz; None if
    the zip is missing. Also returns {unit, dev, rows}."""
    f = sec_file(coin, d, cache)
    if f.exists():
        z = np.load(f)
        return z["hi"], z["lo"], z["cl"], json.loads(str(z["meta"]))
    zp = kline_file(coin, "1s", d, cache)
    if not zp.exists():
        return None
    t, _, h, l, c, dev = read_zip(zp, COINS[coin]["scale"])
    d0 = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())
    k = t - d0
    if ((k < 0) | (k >= 86400)).any():
        raise RuntimeError(f"{zp.name}: open times outside the day")
    if max(h.max(initial=0), c.max(initial=0)) >= 2 ** 31:
        raise RuntimeError(f"{zp.name}: price does not fit int32")
    hi, lo, cl = (np.zeros(86400, np.int32) for _ in range(3))
    hi[k], lo[k], cl[k] = h, l, c
    meta = {"unit": nc.kline_unit(zp), "dev": dev, "rows": int(len(t))}
    f.parent.mkdir(parents=True, exist_ok=True)
    np.savez(f, hi=hi, lo=lo, cl=cl, meta=np.array(json.dumps(meta)))
    return hi, lo, cl, meta


class Minutes:
    """Official 1 m klines of one coin (open time s, open / high / low / close integers in 1 / scale USD)."""

    def __init__(self, df):
        df = df.sort_values("t")
        self.t = df["t"].to_numpy(np.int64)
        self.o, self.h, self.l, self.c = (df[k].to_numpy(np.int64) for k in ("o", "h", "l", "c"))
        self.df = df

    def at(self, col, t_open):
        """Column `col` of the minutes opening at t_open (NaN where there is none)."""
        q = np.asarray(t_open, float)
        ok = np.isfinite(q)
        qi = np.where(ok, q, 0).astype(np.int64)
        if not len(self.t):
            return np.full(q.shape, NAN)
        i = np.clip(np.searchsorted(self.t, qi), 0, len(self.t) - 1)
        hit = ok & (self.t[i] == qi)
        return np.where(hit, getattr(self, col)[i].astype(float), NAN)

    def between(self, a, b):
        m = (self.t >= a) & (self.t < b)
        return self.t[m], self.h[m], self.l[m], self.c[m]


def minutes(coin, cache=CACHE, first=K_FIRST, last=K_LAST, log=print):
    """Minutes of every official 1 m zip of first .. last, cached as <cache>/min/<SYM>.parquet; also the
    open-time units and max tick deviation."""
    p = Path(cache) / "min" / f"{COINS[coin]['sym']}.parquet"
    if p.exists():
        return Minutes(pd.read_parquet(p))
    parts, meta = [], {"units": {}, "dev": 0.0, "missing": []}
    for d in rs.days(first, last):
        zp = kline_file(coin, "1m", d, cache)
        if not zp.exists():
            meta["missing"].append(d.isoformat())
            continue
        t, o, h, l, c, dev = read_zip(zp, COINS[coin]["scale"])
        u = nc.kline_unit(zp)
        meta["units"][u] = meta["units"].get(u, 0) + 1
        meta["dev"] = max(meta["dev"], dev)
        parts.append(pd.DataFrame({"t": t, "o": o, "h": h, "l": l, "c": c}))
    df = pd.concat(parts, ignore_index=True).drop_duplicates("t").sort_values("t").reset_index(drop=True)
    p.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(p)
    (p.parent / f"{COINS[coin]['sym']}.json").write_text(json.dumps(meta))
    log(f"{coin}: {len(df):,} official minutes, units {meta['units']}, max tick dev {meta['dev']:.2g}, missing {meta['missing']}")
    return Minutes(df)


def minute_meta(coin, cache=CACHE):
    p = Path(cache) / "min" / f"{COINS[coin]['sym']}.json"
    return json.loads(p.read_text()) if p.exists() else {}


def spot_range(coin, d0, d1, mins=None, cache=CACHE):
    """rs.Spot of the 1 s klines of UTC days d0 .. d1 (clipped to the kline period), with the official 1 m
    klines of those days set as its minutes (as resolved.py does); None without any 1 s day."""
    d0, d1 = max(d0, K_FIRST), min(d1, K_LAST)
    ds = rs.days(d0, d1)
    n = len(ds) * 86400
    hi, lo, cl = (np.zeros(n, np.int32) for _ in range(3))
    got = 0
    for i, d in enumerate(ds):
        x = sec_day(coin, d, cache)
        if x is None:
            continue
        got += 1
        hi[i * 86400:(i + 1) * 86400], lo[i * 86400:(i + 1) * 86400], cl[i * 86400:(i + 1) * 86400] = x[:3]
    if not got:
        return None
    sec0 = int(datetime(d0.year, d0.month, d0.day, tzinfo=timezone.utc).timestamp())
    f = lambda a: np.where(a > 0, a.astype(float), np.nan)
    spot = rs.Spot(sec0, f(hi), f(lo), f(cl))
    del hi, lo, cl
    if mins is not None:
        t, h, l, c = mins.between(sec0, sec0 + n)
        spot.set_minutes(t, h, l, c)
    return spot


def sec_check(coin, mins, cache=CACHE, first=K_FIRST, last=K_LAST):
    """1 s aggregates against the official 1 m klines, every minute of every day: minutes compared, 1 s
    High above / Low below the official one, Close different, the largest difference (USD), the 1 s
    open-time units and the largest tick deviation."""
    out = {"days": 0, "minutes": 0, "h_wider": 0, "l_wider": 0, "h_narrower": 0, "l_narrower": 0, "c_diff": 0,
           "max_diff_usd": 0.0, "units": {}, "dev": 0.0, "missing": []}
    sc = COINS[coin]["scale"]
    for d in rs.days(first, last):
        x = sec_day(coin, d, cache)
        if x is None:
            out["missing"].append(d.isoformat())
            continue
        hi, lo, cl, meta = x
        out["days"] += 1
        out["units"][meta["unit"]] = out["units"].get(meta["unit"], 0) + 1
        out["dev"] = max(out["dev"], meta["dev"])
        d0 = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())
        H = np.where(hi > 0, hi, np.iinfo(np.int32).min).reshape(1440, 60).max(axis=1)
        L = np.where(lo > 0, lo, np.iinfo(np.int32).max).reshape(1440, 60).min(axis=1)
        have = (cl > 0).reshape(1440, 60)
        last_i = np.where(have.any(axis=1), 59 - np.argmax(have[:, ::-1], axis=1), -1)
        C = np.where(last_i >= 0, cl.reshape(1440, 60)[np.arange(1440), np.maximum(last_i, 0)], 0)
        tm = d0 + 60 * np.arange(1440)
        oh, ol, oc = mins.at("h", tm), mins.at("l", tm), mins.at("c", tm)
        ok = np.isfinite(oc) & (last_i >= 0)
        out["minutes"] += int(ok.sum())
        out["h_wider"] += int((H[ok] > oh[ok]).sum())
        out["l_wider"] += int((L[ok] < ol[ok]).sum())
        out["h_narrower"] += int((H[ok] < oh[ok]).sum())
        out["l_narrower"] += int((L[ok] > ol[ok]).sum())
        out["c_diff"] += int((C[ok] != oc[ok]).sum())
        if ok.any():
            md = max(np.abs(H[ok] - oh[ok]).max(), np.abs(L[ok] - ol[ok]).max(), np.abs(C[ok] - oc[ok]).max())
            out["max_diff_usd"] = max(out["max_diff_usd"], float(md) / sc)
    return out


def hour_check(http, mins, cache=CACHE, first=K_FIRST, last=K_LAST):
    """BTCUSDT 1 h klines (monthly zips) against the 1 h open / close built from the official 1 m klines
    (open of the hour's first minute, close of its last minute): hours compared, open and close
    mismatches."""
    out = {"hours": 0, "open_diff": 0, "close_diff": 0, "months": 0, "missing": []}
    y, m = first.year, first.month
    while (y, m) <= (last.year, last.month):
        tag = f"{y}-{m:02d}"
        p = Path(cache) / "klines" / "BTCUSDT" / "1h" / f"BTCUSDT-1h-{tag}.zip"
        try:
            http.download(VISION_M.format(sym="BTCUSDT", iv="1h", m=tag), p)
            t, o, _, _, c, _ = read_zip(p, COINS["BTC"]["scale"])
            mo, mc = mins.at("o", t), mins.at("c", t + 3540)
            ok = np.isfinite(mo) & np.isfinite(mc)
            out["hours"] += int(ok.sum())
            out["open_diff"] += int((mo[ok] != o[ok]).sum())
            out["close_diff"] += int((mc[ok] != c[ok]).sum())
            out["months"] += 1
        except Exception as e:
            out["missing"].append(f"{tag}: {type(e).__name__}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


# ===================================================================== discovery

_HOUR = re.compile(r"([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{1,2})\s*(am|pm)\s*et\b", re.I)


def et_ts(d, hour=0, fold=0):
    """Unix seconds of hour:00 ET on date d (fold = 1: the second 1 AM of the fall-back day)."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
    except Exception:  # no tz database: ladder.py's rule
        return lad.et_to_utc(d, hour)
    return int(datetime(d.year, d.month, d.day, hour, tzinfo=tz, fold=fold).timestamp())


def hourly_title(title, end_ts, kind):
    """(ET date, hour 0-23) named in an hourly event title ('..., September 15, 4PM ET'); the year is the
    one of the title time implied by the end (above: the end; up/down: one hour before it)."""
    m = _HOUR.search(str(title or ""))
    if not m or not np.isfinite(end_ts):
        return None
    mon = rs._month(m.group(1))
    if mon is None:
        return None
    h = int(m.group(3)) % 12 + (12 if m.group(4).lower() == "pm" else 0)
    ref = int(end_ts) - (3600 if kind == "updown_1h" else 0)
    y = lad.et_date(ref).year
    try:
        d = date(y, mon, int(m.group(2)))
    except ValueError:
        return None
    return d, h


def hourly_expected_end(kind, d, h):
    """The end(s) an hourly market of title date d, hour h must have (both folds)."""
    base = {et_ts(d, h, 0), et_ts(d, h, 1)}
    return sorted(b + (3600 if kind == "updown_1h" else 0) for b in base)


def hour_slugs(kind, end):
    """Both spellings of the hourly slug of a market ending at `end`."""
    t = end - (3600 if kind == "updown_1h" else 0)
    d = lad.et_date(t)
    from zoneinfo import ZoneInfo
    h = datetime.fromtimestamp(t, ZoneInfo("America/New_York")).hour
    hh = f"{h % 12 or 12}{'am' if h < 12 else 'pm'}"
    p, mon = HOURLY_PREFIX[kind], MONTHS[d.month - 1]
    return [f"{p}-{mon}-{d.day}-{d.year}-{hh}-et", f"{p}-{mon}-{d.day}-{hh}-et"]


def series_id(http, slug):
    r = http.get(f"{rs.GAMMA}/series?slug={urllib.parse.quote(slug)}") or []
    return int(r[0]["id"]) if r else None


def series_events_day(http, sid, d):
    """Events of series sid whose end is on UTC day d (every page)."""
    out, off = [], 0
    while True:
        url = f"{rs.GAMMA}/events?" + urllib.parse.urlencode(
            {"series_id": sid, "end_date_min": f"{d.isoformat()}T00:00:00Z", "end_date_max": f"{d.isoformat()}T23:59:59Z",
             "limit": 100, "offset": off, "order": "endDate", "ascending": "true"})
        page = http.get(url) or []
        out += page
        if len(page) < 100:
            return out
        off += 100


def _raw_markets(ev):
    return {m.get("conditionId"): m for m in ev.get("markets") or [] if m.get("conditionId")}


def rows_of(ev, coin, kind, d, source):
    """resolved.py's market rows of one event plus what FAV needs: coin, group, the market's open time,
    the event title / series, how it was found."""
    rk = {"hit_daily": "hit", "above_1h": "above", "updown_1h": "updown_day"}.get(kind, kind)
    raw = _raw_markets(ev)
    out = []
    for r in rs.market_rows(ev, rk, d):
        m = raw.get(r["cid"], {})
        if kind in HOURLY:
            r["kind"] = kind
        acc = rs.ts_of(m.get("acceptingOrdersTimestamp"))
        st = rs.ts_of(m.get("startDate"))
        both = [x for x in (st, acc) if np.isfinite(x)]
        r.update({"coin": coin, "group": group_of(coin, r["kind"]), "accepting_ts": acc,
                  "open_ts": max(both) if both else r.get("created", NAN), "ev_title": ev.get("title") or "",
                  "series": ",".join(s.get("slug") or "" for s in ev.get("series") or []), "found_by": source,
                  "hour": NAN, "end_ok": True})
        out.append(r)
    return out


def hourly_rows(ev, kind, src, first, last, bad_title):
    """Rows of one hourly event whose title day is in first .. last (end checked against the title)."""
    end = rs.ts_of(ev.get("endDate"))
    tt = hourly_title(ev.get("title"), end, kind)
    if tt is None:
        bad_title.append(ev.get("slug"))
        return []
    d, h = tt
    if not (first <= d <= last):
        return []
    exp = hourly_expected_end(kind, d, h)
    out = rows_of(ev, "BTC", kind, d, src)
    for r in out:
        r["hour"] = h
        r["end_ok"] = bool(r["end"] in exp)
        r["day"] = d.isoformat()
        r["description"] = sys.intern(str(r["description"] or ""))
    return out


def discover_hourly(http, first=FIRST, last=LAST, log=print):
    """BTC hourly above / up-down rows of title days first .. last and coverage notes. Events are turned
    into rows as they arrive (an hourly above day is ~1.5 MB of JSON)."""
    rows, notes = [], {}
    for kind, slug in SERIES.items():
        sid = series_id(http, slug)
        notes[f"{kind}_series_id"] = sid
        seen, have_end, bad_title, n_series = set(), set(), [], 0
        for d in rs.days(first - timedelta(days=1), last + timedelta(days=1)):
            for ev in series_events_day(http, sid, d) if sid else []:
                eid = str(ev.get("id"))
                if eid in seen:
                    continue
                seen.add(eid)
                n_series += 1
                have_end.add(rs.ts_of(ev.get("endDate")))
                rows += hourly_rows(ev, kind, "series", first, last, bad_title)
        # every hour of the period without an event: ask both slug spellings
        a = et_ts(first, 0) + (0 if kind == "updown_1h" else 3600)
        b = et_ts(last + timedelta(days=1), 0) + (3600 if kind == "updown_1h" else 0)
        want = [e for e in range(a, b + 1, 3600) if float(e) not in have_end]
        slugs = [s for e in want for s in hour_slugs(kind, e)]
        n_slug = 0
        for ev in rs.events_by_slug(http, slugs) if slugs else []:
            if str(ev.get("id")) not in seen:
                seen.add(str(ev.get("id")))
                rows += hourly_rows(ev, kind, "slug", first, last, bad_title)
                n_slug += 1
        notes[f"{kind}_series_events"] = n_series
        notes[f"{kind}_slug_asked"] = len(slugs)
        notes[f"{kind}_slug_found"] = n_slug
        notes[f"{kind}_title_unparsed"] = bad_title[:20]
        notes[f"{kind}_title_unparsed_n"] = len(bad_title)
        log(f"{kind}: {n_series + n_slug:,} events ({n_slug} found only by slug, {len(slugs):,} slugs asked)")
    return rows, notes


def daily_slugs_coin(name, d):
    short = f"{MONTHS[d.month - 1]}-{d.day}"
    return {k: [f"{p.format(n=name)}-{short}-{d.year}", f"{p.format(n=name)}-{short}"] for k, p in DAILY_PREFIX.items()}


def discover_daily(http, coins=("ETH", "SOL", "XRP"), first=FIRST, last=LAST, log=print):
    rows, notes = [], {"skipped_events": []}
    for coin in coins:
        want, kind_of = [], {}
        for d in rs.days(first, last):
            for kind, ss in daily_slugs_coin(COINS[coin]["name"], d).items():
                for s in ss:
                    want.append(s)
                    kind_of[s] = (kind, d)
        evs = rs.events_by_slug(http, want)
        n = 0
        for ev in evs:
            kind, d = kind_of.get(ev.get("slug"), (None, None))
            if kind is None:
                continue
            end = rs.ts_of(ev.get("endDate"))
            expect = rs.et_midnight(d + timedelta(days=1)) if kind == "hit_daily" else lad.noon_et(d)
            if not np.isfinite(end) or lad.et_date(int(end) - 1) != d:
                notes["skipped_events"].append(f"{ev.get('slug')}: endDate {ev.get('endDate')} is not this date's")
                continue
            try:
                rr = rows_of(ev, coin, kind, d, "slug")
            except Exception as e:  # one malformed event leaves out its markets only
                notes["skipped_events"].append(f"{ev.get('slug')}: {type(e).__name__}: {e}")
                continue
            for r in rr:
                r["end_ok"] = bool(r["end"] == expect)
                r["day"] = d.isoformat()
                if r["kind"] != "hit_daily" and kind == "hit_daily":
                    r["window_note"] = (r.get("window_note") or "") + f" period {r['kind']}"
            rows += rr
            n += 1
        log(f"{coin}: {len(set(want)):,} slugs asked, {n:,} events kept")
    return rows, notes


def discover(http, log=print):
    h, nh = discover_hourly(http, log=log)
    d, nd = discover_daily(http, log=log)
    mk = pd.DataFrame(h + d)
    mk = mk.drop_duplicates("cid").reset_index(drop=True)
    return mk, {**nh, **nd}


# ===================================================================== resolution rules

EXPECTED_1H = {
    "above_1h": {"source": "binance", "pair": "btc/usdt", "candle": "1h", "field": "close", "cmp": ">", "window": "ends"},
    "updown_1h": {"source": "binance", "pair": "btc/usdt", "candle": "1h", "cmp": "up>=open", "window": "begins"},
}


def as_btc(desc, coin):
    """(text with the coin's pair written as btc/usdt, own pair found, other coins' pairs found)."""
    d = nc._norm(desc)
    t = coin.lower()
    own = bool(re.search(rf"\b{t}[/_ -]?usdt\b", d))
    other = [c for c in COINS if c != coin and re.search(rf"\b{c.lower()}[/_ -]?usdt?\b", d)]
    return re.sub(rf"\b{t}[/_ -]?usdt\b", "btc/usdt", d), own, other


def hourly_sig(kind, desc):
    d = nc._norm(desc)
    s = nc.rule_sig("above" if kind == "above_1h" else "updown_day", d)
    w = re.findall(r"1 hour candle that (ends|begins) on the time and date specified", d)
    s["window"] = "+".join(sorted(set(w)))
    if kind == "updown_1h":
        s["cmp"] = "up>=open" if "close price is greater than or equal to the open price" in d else ""
        s["ref"] = ""
    s["time"] = ""
    return s


def rule_one(coin, kind, desc, typ=None, day=None):
    """(signature text, reason): reason '' = the rule the model assumes, else the differing features."""
    if not str(desc or "").strip():
        return "(none)", "no description"
    if kind in HOURLY:
        s = hourly_sig(kind, desc)
        bad = [f"{k}={s.get(k) or '?'}" for k, v in EXPECTED_1H[kind].items() if s.get(k, "") != v]
        return nc.sig_text(s), ";".join(bad)
    if kind not in nc.EXPECTED:
        return "(none)", f"kind={kind}"
    text, own, other = as_btc(desc, coin)
    s = nc.rule_sig(kind, text, typ)
    why = nc.rule_reason(kind, s, typ, day)
    extra = ([] if own else [f"pair!={coin.lower()}/usdt"]) + [f"other_pair={c}" for c in other]
    return nc.sig_text(s).replace("btc/usdt", f"{coin.lower()}/usdt"), ";".join([x for x in [why] + extra if x])


def rule_check(mk):
    mk = mk.copy()
    out = [rule_one(r.coin, r.kind, r.description, r.type, r.day) for r in mk.itertuples(index=False)]
    mk["sig"] = [s for s, _ in out]
    mk["rule"] = [w for _, w in out]
    return mk


# ===================================================================== decide

def scaled(mk, coin):
    """The frame resolved.py's functions get: strikes in units / 100 (they multiply by 100)."""
    f = COINS[coin]["scale"] / 100.0
    x = mk.copy()
    for c in ("lo", "hi", "level"):
        x[c] = x[c].astype(float) * f
    return x


def month_chunks(days_iso):
    """{YYYY-MM: (first day, last day)} of these ISO days."""
    s = pd.Series(sorted(set(days_iso)))
    return {m: (date.fromisoformat(g.iloc[0]), date.fromisoformat(g.iloc[-1])) for m, g in s.groupby(s.str[:7])}


def finalize_noon(dec, conv):
    """rs.decide's last step with a given noon convention (the per-convention columns it returns)."""
    dec = dec.copy()
    noon = dec["kind"].isin(rs.NOON_KINDS)
    dec["tstar"] = np.where(noon, dec[f"tstar_{conv}"], dec["tstar_hit"])
    dec["comp"] = np.where(noon, dec[f"comp_{conv}"], dec["comp_hit"])
    dec["ref"] = np.where(dec["kind"] == "updown_day", dec[f"ref_{conv}"], NAN)
    why = np.full(len(dec), "", dtype=object)
    why[dec["official"].isna().to_numpy()] = "unresolved"
    why[dec["comp"].isna().to_numpy() & (why == "")] = "no klines / window"
    bad = (dec["comp"] != dec["official"]).to_numpy() & dec["comp"].notna().to_numpy() & (why == "")
    why[bad] = "mismatch"
    dec["excluded"] = why
    return dec


def decide_daily(mk, coin, mins, log=print):
    """ETH / SOL / XRP daily markets (rule-checked ones only): rs.decide month by month, the noon
    convention chosen over all months (fewest mismatches with the official outcomes, ties open12).
    Returns (decided frame in USD strikes with lo_rs / hi_rs / level_rs / ref in resolved.py's units,
    convention, mismatch counts)."""
    parts = []
    for mo, (a, b) in month_chunks(mk["day"]).items():
        sub = mk[(mk["day"] >= a.isoformat()) & (mk["day"] <= b.isoformat())]
        spot = spot_range(coin, a - timedelta(days=2), b + timedelta(days=2), mins)
        if spot is None:
            x = sub.copy()
            x["excluded"] = "no klines / window"
            parts.append(x)
            continue
        x = scaled(sub, coin).reset_index(drop=True)
        dec, _, _ = rs.decide(x, spot)
        dec["lo_rs"], dec["hi_rs"], dec["level_rs"] = dec["lo"], dec["hi"], dec["level"]
        for c in ("lo", "hi", "level"):
            dec[c] = sub[c].to_numpy(float)
        parts.append(dec)
        del spot
    dec = pd.concat(parts, ignore_index=True)
    noon = dec["kind"].isin(rs.NOON_KINDS) & dec["official"].notna()
    mism = {c: int(((dec[f"comp_{c}"] != dec["official"]) & dec[f"comp_{c}"].notna() & noon).sum()) for c in rs.CONVS}
    conv = min(rs.CONVS, key=lambda c: (mism[c], rs.CONVS.index(c)))
    dec = finalize_noon(dec, conv)
    log(f"{coin}: noon mismatches {mism} -> {conv}; excluded {dec['excluded'].value_counts().to_dict()}")
    return dec, conv, mism


def decide_hourly(mk, mins):
    """BTC hourly markets from the official 1 m klines. above: 'ends' = Close of the 1 h candle ending at
    the market end (1 m close of end - 60) > strike, 'begins' = of the candle beginning at the end; the
    reading with fewer mismatches is used (ties: 'ends', the text's). up/down: Up iff 1 m close of
    end - 60 >= 1 m open of end - 3600 (the hour's open); 'prevclose' (the previous hour's close as the
    open) is computed for comparison only. Returns (frame, above reading, mismatch counts)."""
    dec = mk.copy()
    end = dec["end"].to_numpy(float)
    sc = COINS["BTC"]["scale"]
    above = (dec["kind"] == "above_1h").to_numpy()
    up = (dec["kind"] == "updown_1h").to_numpy()
    strike = dec["lo"].to_numpy(float) * sc
    off = dec["official"].to_numpy(float)
    c_end, c_beg = mins.at("c", end - 60), mins.at("c", end + 3540)
    o_hr, c_prev = mins.at("o", end - 3600), mins.at("c", end - 3660)
    cmpf = lambda ok, x: np.where(ok, x.astype(float), NAN)
    comp = {"ends": cmpf(np.isfinite(c_end), c_end > strike), "begins": cmpf(np.isfinite(c_beg), c_beg > strike),
            "open": cmpf(np.isfinite(c_end) & np.isfinite(o_hr), c_end >= o_hr),
            "prevclose": cmpf(np.isfinite(c_end) & np.isfinite(c_prev), c_end >= c_prev)}
    mm = lambda sel, c: int((sel & np.isfinite(off) & np.isfinite(c) & (c != off)).sum())
    mism = {"above_ends": mm(above, comp["ends"]), "above_begins": mm(above, comp["begins"]),
            "updown_open": mm(up, comp["open"]), "updown_prevclose": mm(up, comp["prevclose"])}
    conv = "ends" if mism["above_ends"] <= mism["above_begins"] else "begins"
    dec["comp"] = np.where(above, comp[conv], np.where(up, comp["open"], NAN))
    dec["tstar"] = np.where(above & (conv == "begins"), end + 3600, end)
    dec["close_u"] = np.where(above & (conv == "begins"), c_beg, c_end)
    dec["ref_u"] = np.where(up, o_hr, NAN)
    dec["ref"] = dec["ref_u"] / 100.0                       # resolved.py's units (it multiplies by 100)
    dec["lo_rs"], dec["hi_rs"], dec["level_rs"] = dec["lo"], dec["hi"], dec["level"]
    for c in ("tsec", "ws", "we"):
        dec[c] = NAN
    why = np.full(len(dec), "", dtype=object)
    why[~np.isfinite(off)] = "unresolved"
    why[~np.isfinite(dec["comp"].to_numpy(float)) & (why == "")] = "no klines / window"
    bad = np.isfinite(dec["comp"].to_numpy(float)) & (dec["comp"].to_numpy(float) != off) & (why == "")
    why[bad] = "mismatch"
    dec["excluded"] = why
    return dec, conv, mism


def decide_all(mk, log=print):
    """Every market: rule / end exclusions first, then the outcome checks. Returns (frame, notes)."""
    mk = mk.copy()
    pre = np.where(mk["rule"] != "", "rule: " + mk["rule"], np.where(~mk["end_ok"].astype(bool), "end_mismatch", ""))
    if "kind" in mk:
        pre = np.where((pre == "") & ~mk["kind"].isin(DAILY_KINDS + HOURLY), "kind: " + mk["kind"], pre)
    mk["pre"] = pre
    parts, notes = [], {"noon": {}, "hourly": {}}
    rest = mk[mk["pre"] != ""].copy()
    rest["excluded"] = rest["pre"]
    parts.append(rest)
    ok = mk[mk["pre"] == ""]
    for coin in COINS:
        sub = ok[ok["coin"] == coin]
        if not len(sub):
            continue
        mins = minutes(coin, log=log)
        if coin == "BTC":
            dec, conv, mism = decide_hourly(sub, mins)
            notes["hourly"] = {"above_reading": conv, "mismatches": mism}
            log(f"BTC hourly: above reading {conv}, mismatches {mism}; excluded {dec['excluded'].value_counts().to_dict()}")
        else:
            dec, conv, mism = decide_daily(sub, coin, mins, log=log)
            notes["noon"][coin] = {"conv": conv, "mismatches": mism}
        parts.append(dec)
    out = pd.concat(parts, ignore_index=True)
    return out, notes


# ===================================================================== model points

def rs_frame(dec):
    """The columns rs.checkpoints reads, hourly kinds mapped to resolved.py's: hourly above = 'above'
    (decision at T* = the candle close), hourly up/down = 'updown_day' (reference = the hour's open)."""
    fr = pd.DataFrame({"cid": dec["cid"], "excluded": dec["excluded"],
                       "kind": dec["kind"].map({"above_1h": "above", "updown_1h": "updown_day"}).fillna(dec["kind"]),
                       "type": dec["type"], "end": dec["end"].astype(float), "tstar": dec["tstar"].astype(float),
                       "ws": dec["ws"].astype(float), "we": dec["we"].astype(float), "tsec": dec["tsec"].astype(float),
                       "level": dec["level_rs"].astype(float), "lo": dec["lo_rs"].astype(float),
                       "hi": dec["hi_rs"].astype(float), "ref": dec["ref"].astype(float)})
    return fr.reset_index(drop=True)


def points_spot(m, spot):
    """Model rows of the included markets m at the FAV.md checkpoints (rs.checkpoints: hourly markets 30 /
    10 / 5 / 1, daily 60 / 30 / 10 / 5 / 1 minutes before the end), only those at or after the market's
    open; adds kind / group / day / hourly / open_ts."""
    m = m[m["excluded"] == ""]
    parts = []
    for hourly, checks in ((True, HOURLY_CHECKS), (False, DAILY_CHECKS)):
        s = m[m["kind"].isin(HOURLY) == hourly]
        if len(s):
            cp = rs.checkpoints(rs_frame(s), spot, checks=checks)
            if len(cp):
                parts.append(cp)
    if not parts:
        return pd.DataFrame()
    cp = pd.concat(parts, ignore_index=True)
    meta = m.drop_duplicates("cid").set_index("cid")
    cp["kind"] = meta["kind"].reindex(cp["cid"]).to_numpy()
    cp["group"] = meta["group"].reindex(cp["cid"]).to_numpy()
    cp["day"] = meta["day"].reindex(cp["cid"]).to_numpy()
    cp["hourly"] = cp["kind"].isin(HOURLY).to_numpy()
    cp["open_ts"] = meta["open_ts"].reindex(cp["cid"]).to_numpy(float)
    return cp[~(cp["t"] < cp["open_ts"])].reset_index(drop=True)


def points_coin(dec, coin, log=print):
    """points_spot month by month (rs.Spot of the month's 1 s klines with two days around it)."""
    m = dec[(dec["coin"] == coin) & (dec["excluded"] == "")]
    if not len(m):
        return pd.DataFrame()
    mins = minutes(coin, log=log)
    parts = []
    for mo, (a, b) in month_chunks(m["day"]).items():
        sub = m[(m["day"] >= a.isoformat()) & (m["day"] <= b.isoformat())]
        spot = spot_range(coin, a - timedelta(days=2), b + timedelta(days=2), mins)
        if spot is None:
            continue
        cp = points_spot(sub, spot)
        if len(cp):
            parts.append(cp)
        del spot
    cp = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    log(f"{coin}: {len(cp):,} model rows")
    return cp


# ===================================================================== rules -> windows, sampling

def in_band(q, rule):
    q = np.asarray(q, float)
    return (q >= RULES[rule]["lo"]) & (q < RULES[rule]["hi"])


def rule_mask(cp, rule, q=None):
    """Rows of cp (one side each: q, default the favoured side's p_fav) that rule `rule` takes."""
    q = cp["p_fav"].to_numpy(float) if q is None else np.asarray(q, float)
    hourly = cp["hourly"].to_numpy(bool)
    chk = cp["check"].to_numpy()
    ok_c = np.where(hourly, np.isin(chk, rule_checks(rule, True)), np.isin(chk, rule_checks(rule, False)))
    ok_k = np.array([rule_applies(rule, k) for k in cp["kind"]], bool) if len(cp) else np.zeros(0, bool)
    return in_band(q, rule) & ok_c & ok_k


def windows(cp):
    """The checkpoint windows to fetch: every (cid, check) at which some rule takes a side, with the rules."""
    if not len(cp):
        return pd.DataFrame(columns=["cid", "kind", "group", "day", "check", "t", "rules"])
    masks = {r: rule_mask(cp, r) for r in RULES}
    anym = np.zeros(len(cp), bool)
    for m in masks.values():
        anym |= m
    w = cp.loc[anym, ["cid", "kind", "group", "day", "check", "t"]].copy()
    w["rules"] = ["+".join(r for r in RULES if masks[r][i]) for i in np.flatnonzero(anym)]
    return w.drop_duplicates(["cid", "check"]).reset_index(drop=True)


def sample_plan(win, active_days, budget=FETCH_BUDGET, seed=SEED, groups=GROUPS):
    """{group: {"windows", "days", "kept_days" (sorted ISO list), "fraction", "kept_windows"}}: all days if
    the windows fit the budget, else water-filling over groups in ascending order of their window count;
    a group over its share keeps ceil(share / windows * active days) active days, sampled uniformly with
    random.Random(seed + group index)."""
    n = {g: int((win["group"] == g).sum()) for g in groups}
    plan = {}
    total = sum(n.values())
    left, todo = budget, sorted(groups, key=lambda g: (n[g], groups.index(g)))
    for k, g in enumerate(todo):
        days_g = sorted(active_days.get(g, []))
        share = left / (len(todo) - k)
        if total <= budget or n[g] <= share:
            kept = days_g
        else:
            want = min(len(days_g), math.ceil(share / n[g] * len(days_g)))
            kept = sorted(random.Random(seed + groups.index(g)).sample(days_g, want))
        kw = int(((win["group"] == g) & win["day"].isin(set(kept))).sum())
        plan[g] = {"windows": n[g], "days": len(days_g), "kept_days": kept, "kept_windows": kw,
                   "fraction": len(kept) / len(days_g) if days_g else NAN}
        left -= kw
    return plan


def fetch(http, win, dec, plan, cache=CACHE, batch=2000, log=print):
    """Trades of the planned windows (cb.fetch_windows, in batches so its files are appended in pieces)."""
    kept = pd.concat([win[(win["group"] == g) & win["day"].isin(set(p["kept_days"]))] for g, p in plan.items()],
                     ignore_index=True)
    toks = dec.drop_duplicates("cid")[["cid", "yes_token", "no_token"]]
    tr, inf = None, None
    for i in range(0, max(len(kept), 1), batch):
        part = kept.iloc[i:i + batch]
        tr, inf = cb.fetch_windows(http, part[["cid", "kind", "check", "t"]], toks, cache, log=log)
        log(f"fetch: {min(i + batch, len(kept)):,}/{len(kept):,} windows; {http.calls:,} requests")
    return tr, inf, kept


# ===================================================================== rows and statistics

def window_trades(tr, kept):
    """Fetched trades inside their window [t, t + 60) (defensive: the API's start / end are inclusive)."""
    cols = ["cid", "ts", "taker_buy", "is_yes", "price", "size", "check"]
    if tr is None or not len(tr) or not len(kept):
        return pd.DataFrame(columns=cols)
    j = tr.merge(kept[["cid", "check", "t"]], on=["cid", "check"], how="inner")
    j = j[(j["ts"] >= j["t"]) & (j["ts"] < j["t"] + 60)]
    return j[cols].reset_index(drop=True)


def side_rows(cp, wt, dec):
    """cb.side_rows (both sides of every checkpoint, the side's trades of [t, t + 60): shares, cost,
    share-weighted p, pnl = result - p, taker pnl) plus group / hourly / the market's day (mday)."""
    if not len(cp):
        return pd.DataFrame()
    rows = cb.side_rows(cp[["cid", "kind", "check", "t", "p_yes"]], wt, dec)
    meta = cp.drop_duplicates("cid").set_index("cid")
    for c, src in (("group", "group"), ("hourly", "hourly"), ("mday", "day")):
        rows[c] = meta[src].reindex(rows["cid"]).to_numpy()
    rows["hourly"] = rows["hourly"].astype(bool)
    return rows


def cell_rows(rows, rule, group, kept_days=None):
    """The rows rule `rule` takes in group `group` (the in-band side at the rule's checkpoints), on the
    fetched (kept) days."""
    r = rows[rows["group"] == group]
    if kept_days is not None:
        r = r[r["mday"].isin(set(kept_days))]
    if not len(r):
        return r
    return r[rule_mask(r, rule, q=r["q"].to_numpy(float))]


def halves(r, days_):
    """(first, second) cb.stats of the cell's rows split at the lower median of the markets' days."""
    s = r[r["shares"] > 0]
    if not len(s):
        return None, None, None
    md = s.groupby("cid")["mday"].first().sort_values()
    med = md.iloc[(len(md) - 1) // 2]
    a = set(md.index[md <= med])
    st = lambda x: cb.stats(x, days_) if len(x) else cb.stats(r.iloc[:0], days_)
    return st(r[r["cid"].isin(a)]), st(r[~r["cid"].isin(a)]), med


def cell(rows, rule, group, days_kept, n_tests=N_TESTS, alpha=ALPHA, min_mk=MIN_MK):
    """Statistics and verdict of one rule x group cell (FAV.md); None if the rule does not apply."""
    kinds = rows.loc[rows["group"] == group, "kind"].unique() if len(rows) else []
    if not any(rule_applies(rule, k) for k in kinds):
        return {"rule": rule, "group": group, "applies": False}
    r = cell_rows(rows, rule, group, days_kept)
    nd = max(len(days_kept), 1)
    st = cb.stats(r, nd)
    h1, h2, med = halves(r, nd)
    thr = alpha / n_tests
    ok = {"pnl": bool(np.isfinite(st["pnl"]) and st["pnl"] > 0),
          "p": bool(np.isfinite(st["p1"]) and st["p1"] < thr),
          "n": st["markets"] >= min_mk,
          "halves": bool(h1 is not None and np.isfinite(h1["pnl"]) and h1["pnl"] > 0
                         and np.isfinite(h2["pnl"]) and h2["pnl"] > 0)}
    tr = r[r["shares"] > 0]
    out = {"rule": rule, "group": group, "applies": True, **st, "thr": thr, "days": nd,
           "mk_inband": int(r["cid"].nunique()), "mk_inband_day": r["cid"].nunique() / nd,
           "mk_tr_day": st["markets"] / nd, "p1_taker": cb.one_sided_p(st["t_taker"], st["markets"]),
           "q_pts": float(r["q"].mean()) if len(r) else NAN, "win_pts": float(r["result"].mean()) if len(r) else NAN,
           "win_tr": float((tr["result"] * tr["shares"]).sum() / tr["shares"].sum()) if len(tr) else NAN,
           "h1": h1, "h2": h2, "median_day": med, "ok": ok, "pass": all(ok.values())}
    return out


def by_kind(rows, rule, group, days_kept):
    """Description: the cell split by market kind and by checkpoint (pnl, t, markets with trades)."""
    r = cell_rows(rows, rule, group, days_kept)
    out = []
    for col in ("kind", "check"):
        for k, g in r.groupby(col):
            st = cb.stats(g, max(len(days_kept), 1))
            out.append({"by": col, "key": k, "markets": st["markets"], "pnl": st["pnl"], "t": st["t"],
                        "losing": st["losing"], "profit_day": st["profit_day"], "p": st["p"]})
    return pd.DataFrame(out)


# ===================================================================== report

def _c(x, nd=2):
    return "–" if x is None or not np.isfinite(x) else f"{100 * x:+.{nd}f}"


def _n(x, nd=0):
    return "–" if x is None or not np.isfinite(x) else f"{x:,.{nd}f}"


def _pv(p):
    if p is None or not np.isfinite(p):
        return "–"
    return f"{p:.1e}" if p < 0.001 else f"{p:.3f}"


def _t(t, G):
    return "–" if not (np.isfinite(t) and G >= rs.MIN_G) else f"{t:.2f}"


def verdict_line(c):
    name = f"{RULE_CN[c['rule']]} × {GROUP_CN[c['group']]}"
    if not c["applies"]:
        return f"- {name}：不适用（规则 2 只用于“高于”类）。"
    why = [x for x, ok in ((f"每份 {_c(c['pnl'])}¢ ≤ 0", c["ok"]["pnl"]),
                           (f"单侧 p {_pv(c['p1'])} ≥ {c['thr']:.3g}", c["ok"]["p"]),
                           (f"有成交市场 {c['markets']} < {MIN_MK}", c["ok"]["n"]),
                           ("前后两半不都为正", c["ok"]["halves"])) if not ok]
    h1, h2 = c["h1"], c["h2"]
    hs = (f"前半 {_c(h1['pnl'])}¢（{h1['markets']} 个）/ 后半 {_c(h2['pnl'])}¢（{h2['markets']} 个）"
          if h1 is not None else "无成交")
    s = (f"- {name}：每份 {_c(c['pnl'])}¢（挂单），t {_t(c['t'], c['markets'])}，单侧 p {_pv(c['p1'])}，"
         f"有成交市场 {c['markets']}，{hs}，利润上限 ${_n(c['profit_day'], 1)}/天 → "
         + ("**通过**" if c["pass"] else "**不通过**（" + "；".join(why) + "）"))
    if c["pass"] and not (np.isfinite(c["pnl_taker"]) and c["pnl_taker"] > 0):
        s += f"；但吃单每份 {_c(c['pnl_taker'])}¢"
    return s


def md(heads, body):
    return "\n".join(["| " + " | ".join(heads) + " |", "|" + "---|" * len(heads)] +
                     ["| " + " | ".join(str(x) for x in r) + " |" for r in body])


def cell_table(cells, plan):
    heads = ["规则", "组", "抽样比例", "天数", "在区间市场/天", "有成交市场", "有成交市场/天", "输的市场", "笔数",
             "份/天", "花费 $/天", "利润 $/天", "挂单 ¢/份", "t", "单侧 p", "吃单 ¢/份", "t", "前半 ¢/份（市场）",
             "后半 ¢/份（市场）", "最大单市场亏损 $", "模型 q", "成交价 p", "成交加权胜率", "判定"]
    body = []
    for c in cells:
        if not c["applies"]:
            body.append([c["rule"], GROUP_CN[c["group"]]] + ["–"] * (len(heads) - 3) + ["不适用"])
            continue
        h1, h2 = c["h1"], c["h2"]
        hc = lambda h: "–" if h is None else f"{_c(h['pnl'])}（{h['markets']}）"
        body.append([c["rule"], GROUP_CN[c["group"]], f"{plan[c['group']]['fraction']:.2f}", c["days"],
                     _n(c["mk_inband_day"], 2), c["markets"], _n(c["mk_tr_day"], 2), c["losing"], _n(c["trades"]),
                     _n(c["shares_day"]), _n(c["cost_day"]), _n(c["profit_day"], 1), _c(c["pnl"]),
                     _t(c["t"], c["markets"]), _pv(c["p1"]), _c(c["pnl_taker"]), _t(c["t_taker"], c["markets"]),
                     hc(h1), hc(h2), _n(min(c["worst"], 0.0) if np.isfinite(c["worst"]) else NAN),
                     _n(c["q"], 4), _n(c["p"], 4), _n(c["win_tr"], 4), "**通过**" if c["pass"] else "不通过"])
    return md(heads, body)


KIND_CN = {"above": "高于", "range": "区间", "hit_daily": "触及", "updown_day": "按日涨跌", "above_1h": "每小时高于",
           "updown_1h": "每小时涨跌"}


def coverage(dec):
    rows = []
    for (g, k), x in dec.groupby(["group", "kind"]):
        ex = x["excluded"].fillna("")
        rows.append({"group": g, "kind": k, "events": x["event_slug"].nunique(), "markets": len(x),
                     "days": x["day"].nunique(), "first": x["day"].min(), "last": x["day"].max(),
                     "rule": int(ex.str.startswith("rule").sum()), "end": int((ex == "end_mismatch").sum()),
                     "unres": int((ex == "unresolved").sum()), "nok": int((ex == "no klines / window").sum()),
                     "mism": int((ex == "mismatch").sum()), "ok": int((ex == "").sum())})
    return pd.DataFrame(rows)


def rule_evidence(dec, k=2):
    """Per group x kind: the most common signature (markets), the differing ones (count, example slug,
    the description excerpt of the first)."""
    out = []
    for (g, kind), x in dec.groupby(["group", "kind"]):
        top = x["sig"].value_counts()
        bad = x[x["rule"] != ""]
        ex = []
        for why, n in bad["rule"].value_counts().iloc[:k].items():
            r0 = bad[bad["rule"] == why].iloc[0]
            ex.append({"why": why, "n": int(n), "slug": r0["slug"], "text": re.sub(r"\s+", " ", str(r0["description"]))[:260]})
        out.append({"group": g, "kind": kind, "sig": top.index[0], "sig_n": int(top.iloc[0]), "n": len(x),
                    "bad": ex, "bad_n": len(bad),
                    "sample": re.sub(r"\s+", " ", str(x[x["rule"] == ""]["description"].iloc[0]))[:330]
                    if (x["rule"] == "").any() else ""})
    return out


def report(ctx, out_md):
    cells, plan, dec = ctx["cells"], ctx["plan"], ctx["dec"]
    thr = bonferroni()
    L = ["# 临近到期的热门一边：同一规则铺到更多市场（FAV.md）", ""]
    L.append(f"**判定**（每格：挂单每份 > 0、按市场聚类单侧 p < 0.05/{N_TESTS} = {thr:.3g}（规则数 × 市场组数 = 2 × 5）、"
             f"≥ {MIN_MK} 个有成交市场、前后两半都 > 0）：")
    L += [verdict_line(c) for c in cells]
    npass = sum(1 for c in cells if c["applies"] and c["pass"])
    L.append(f"- 通过 {npass} / {sum(1 for c in cells if c['applies'])} 格。"
             + ("通过的组按 FAV.md 上前向（需要把阶梯录制扩展到这些市场，单独写规则）。" if npass else "没有格子通过，不上前向。"))
    L.append("")
    L.append(f"数据：Gamma 已结束事件（标题日期 {FIRST}–{LAST}，ET）、data-api 逐笔成交、data.binance.vision 的 1 秒和 1 分钟 K 线"
             f"（BTCUSDT、ETHUSDT、SOLUSDT、XRPUSDT，UTC {K_FIRST}–{K_LAST}）。模型、结算、成交窗口和统计全部从 resolved.py / nearcert.py / "
             f"calib.py 导入（见 `fav.py` 文档）。本次运行 {ctx['runtime'] / 60:.1f} 分钟；首次运行各步：{ctx['timing_text']}。")
    L.append("")
    L.append("单位：¢/份 = 按份数加权的（结果 − 成交价）×100，挂单无费，吃单再扣 0.07·p(1 − p)；t 按市场聚类（少于 20 个市场记“–”），"
             "单侧 p 用 G − 1 自由度的 t 分布；份/天、花费 $/天（Σ p·份）、利润 $/天（Σ(结果 − p)·份）是这一格**全部成交的合计，是上限**，"
             "不是一个买家能拿到的；天数 = 该组有市场的 ET 日（抽样的组只算抽中的日，等于全期合计 ÷ 天数 × 1/抽样比例）；"
             "在区间市场/天 = 有一边落在规则区间（任一时点）的市场；最大单市场亏损 = 一个市场在这一格的挂单净亏损。")
    L.append("")
    L.append("## 每格结果")
    L.append("")
    L.append(cell_table(cells, plan))
    L.append("")
    if ctx.get("bykind") is not None and len(ctx["bykind"]):
        L.append("按类型 / 时点拆开（描述，不判定；¢/份 挂单，市场 = 有成交市场）：")
        L.append("")
        bk = ctx["bykind"]
        body = []
        for (rule, g), x in bk.groupby(["rule", "group"], sort=False):
            parts = []
            for r in x.itertuples():
                lab = KIND_CN.get(r.key, r.key) if r.by == "kind" else f"{r.key}分"
                parts.append(f"{lab} {_c(r.pnl)}（{r.markets}，t {_t(r.t, r.markets)}）")
            body.append([rule, GROUP_CN[g], "；".join(parts)])
        L.append(md(["规则", "组", "类型 / 时点：¢/份（有成交市场，t）"], body))
        L.append("")
    L.append("## 覆盖与剔除")
    L.append("")
    cov = ctx["coverage"]
    L.append(md(["组", "类型", "事件", "市场", "有市场的天", "第一天", "最后一天", "规则不同", "结束时间不对", "未结算",
                 "缺 K 线", "结果不符", "纳入"],
                [[GROUP_CN[r.group], KIND_CN.get(r.kind, r.kind), r.events, r.markets, r.days, r.first, r.last, r.rule, r.end,
                  r.unres, r.nok, r.mism, r.ok] for r in cov.itertuples()]))
    L.append("")
    L += ctx["coverage_notes"]
    L.append("")
    L.append("规则文字（每组每类：最常见的解析签名；纳入的市场描述节选；签名不同而剔除的，附原文）：")
    L.append("")
    for e in ctx["evidence"]:
        L.append(f"- {GROUP_CN[e['group']]} · {KIND_CN.get(e['kind'], e['kind'])}：`{e['sig']}`（{e['sig_n']}/{e['n']}）。"
                 + (f"例：“{e['sample']}…”" if e["sample"] else ""))
        for b in e["bad"]:
            L.append(f"  - 剔除 {b['n']} 个（{b['why']}），如 `{b['slug']}`：“{b['text']}…”")
    L.append("")
    L.append("## K 线与结算口径")
    L.append("")
    L += ctx["kline_notes"]
    L.append("")
    L.append("## 抓取与抽样")
    L.append("")
    L += ctx["fetch_notes"]
    L.append("")
    L.append("文件：`fav.py`（代码，含全部预先写定的保守选择），`test_fav.py`（测试）。")
    Path(out_md).write_text("\n".join(L) + "\n")
    return "\n".join(L)


# ===================================================================== notes for the report

def coverage_notes(dec, notes):
    L = []
    h = dec[dec["kind"].isin(HOURLY)]
    for kind in HOURLY:
        x = h[h["kind"] == kind]
        if not len(x):
            continue
        per_day = x.groupby("day")["event_slug"].nunique()
        full = 24
        short = per_day[per_day < full]
        L.append(f"- {KIND_CN[kind]}：系列 `{SERIES[kind]}`（id {notes.get(f'{kind}_series_id')}），按结束日逐日列出 "
                 f"{notes.get(f'{kind}_series_events', 0):,} 个事件；缺的整点按两种 slug 拼法补查 {notes.get(f'{kind}_slug_asked', 0):,} 个，"
                 f"补到 {notes.get(f'{kind}_slug_found', 0)} 个；标题解析不了 {notes.get(f'{kind}_title_unparsed_n', 0)} 个。"
                 f"有事件的 {len(per_day)} 天里每天事件数中位数 {per_day.median():.0f}，少于 24 个的 {len(short)} 天"
                 + (f"（如 {', '.join(f'{d} {n}' for d, n in short.iloc[:6].items())}）" if len(short) else "") + "。")
        if kind == "above_1h":
            spe = x.groupby("event_slug").size()
            mon = x.assign(m=x["day"].str[:7]).groupby("m")["event_slug"].agg(lambda s: s.value_counts().median())
            L.append(f"  - 每个事件的价位数：中位数 {spe.median():.0f}（最少 {spe.min()}、最多 {spe.max()}）；按月中位数 "
                     + "，".join(f"{m} {v:.0f}" for m, v in mon.items()) + "。")
            gap = x.sort_values("lo").groupby("event_slug")["lo"].apply(lambda s: s.diff().median()).median()
            L.append(f"  - 相邻价位间隔中位数 ${gap:,.0f}；结束 = 标题时刻（1 小时 K 线收盘），市场开放（startDate/acceptingOrders）在结束前"
                     f"中位数 {((x['end'] - x['open_ts']) / 60).median():.0f} 分钟（p10 {((x['end'] - x['open_ts']) / 60).quantile(0.1):.0f}）。")
        else:
            L.append(f"  - 结束 = 标题时刻 + 1 小时；市场开放在结束前中位数 {((x['end'] - x['open_ts']) / 3600).median():.0f} 小时。")
        for dd in ("2025-11-02", "2026-03-08"):
            y = x[x["day"] == dd].drop_duplicates("event_slug")
            if len(y):
                hrs = sorted(int(v) for v in y["hour"].dropna())
                L.append(f"  - 夏令时切换日 {dd}：{len(y)} 个事件，标题小时 {hrs}，结束时间不对（剔除）{int((~y['end_ok'].astype(bool)).sum())} 个。")
    d = dec[~dec["kind"].isin(HOURLY)]
    alld = {x.isoformat() for x in rs.days(FIRST, LAST)}
    for coin in ("ETH", "SOL", "XRP"):
        parts = []
        for k in DAILY_KINDS:
            have = set(d[(d["coin"] == coin) & (d["kind"] == k)]["day"])
            miss = sorted(alld - have)
            first = min(have) if have else "–"
            inside = [m for m in miss if have and m > min(have)]
            parts.append(f"{KIND_CN[k]} 从 {first} 起，之后缺 {len(inside)} 天" + (f"（{', '.join(inside[:4])}{' …' if len(inside) > 4 else ''}）" if inside else ""))
        L.append(f"- {coin}：" + "；".join(parts) + "。")
    sk = notes.get("skipped_events") or []
    L.append(f"- 短 slug 命中去年同日的事件（跳过）{sum('is not this date' in s for s in sk)} 个；其他跳过 {sum('is not this date' not in s for s in sk)} 个。")
    mm = dec[dec["excluded"] == "mismatch"]
    if len(mm):
        L.append(f"- 算出的结果与官方不符（剔除）{len(mm)} 个：" + "，".join(f"`{s}`" for s in mm["slug"].iloc[:8]) + ("…" if len(mm) > 8 else ""))
    return L


def kline_notes(kn, dn):
    L = []
    for coin in COINS:
        k = kn.get(coin) or {}
        s, m = k.get("sec") or {}, k.get("min") or {}
        if not s:
            continue
        tick = 1.0 / COINS[coin]["scale"]
        L.append(f"- {COINS[coin]['sym']}：1 秒 {s['days']} 天（时间戳单位 {s['units']}），1 分钟 {sum((m.get('units') or {}).values())} 天"
                 f"（{m.get('units')}）；价格按 {tick:g} 美元的整数存（最大取整误差 {max(s['dev'], m.get('dev', 0)):.1e} 个最小单位）。"
                 f"逐分钟核对 {s['minutes']:,} 分钟：1 秒聚合最高价高于官方 {s['h_wider']}、低于 {s['h_narrower']}，最低价低于官方 {s['l_wider']}、"
                 f"高于 {s['l_narrower']}，收盘价不同 {s['c_diff']}（最大差 ${s['max_diff_usd']:.4g}）→ 结果和 T* 用官方 1 分钟，1 秒只用于 σ 和秒级越过。"
                 + (f" 缺 1 秒 {s['missing']}" if s.get("missing") else ""))
    miss = kn.get("missing") or {}
    if any(miss.values()):
        L.append(f"- 下载失败的日：{miss}")
    for coin, v in (dn.get("noon") or {}).items():
        L.append(f"- {coin} 中午 K 线约定：与官方不符 open12（12:00 开盘、12:01 收盘）= {v['mismatches']['open12']}，"
                 f"close12（11:59 开盘）= {v['mismatches']['close12']} → 用 **{v['conv']}**（和 BTC 日内相同的规则文字）。")
    hh = dn.get("hourly") or {}
    if hh:
        mm = hh["mismatches"]
        L.append(f"- BTC 每小时高于：“结束于标题时刻的 1 小时 K 线收盘”（= 结束前最后一根 1 分钟 K 线收盘）与官方不符 {mm['above_ends']}，"
                 f"另一种读法（开始于标题时刻）{mm['above_begins']} → 用 **{hh['above_reading']}**。BTC 每小时涨跌：收盘 ≥ 本小时开盘"
                 f"（第一根 1 分钟 K 线开盘）与官方不符 {mm['updown_open']}（若用上一小时收盘当开盘：{mm['updown_prevclose']}）。")
    hc = kn.get("hour_check") or {}
    if hc:
        L.append(f"- 币安自己的 BTCUSDT 1 小时 K 线（{hc['months']} 个月度文件）对 1 分钟拼出的开盘/收盘：{hc['hours']:,} 小时，开盘不同 "
                 f"{hc['open_diff']}、收盘不同 {hc['close_diff']}。")
    return L


def fetch_notes(win, plan, inf, kept, fstat):
    L = []
    by = win.assign(R1=win["rules"].str.contains("R1"), R2=win["rules"].str.contains("R2")).groupby("group")
    rows = []
    for g in GROUPS:
        p = plan.get(g, {})
        x = win[win["group"] == g]
        rows.append([GROUP_CN[g], len(x), int(x["rules"].str.contains("R1").sum()), int(x["rules"].str.contains("R2").sum()),
                     p.get("days", 0), len(p.get("kept_days", [])), f"{p.get('fraction', NAN):.3f}", p.get("kept_windows", 0)])
    L.append(f"先只用 K 线算模型（不看任何成交），列出有一边落在规则区间的时点窗口，再只抓这些窗口 [t, t+60) 的成交。预算 {FETCH_BUDGET:,} 个窗口"
             f"（约 3 小时 × 4 次/秒）。")
    L.append("")
    L.append(md(["组", "窗口", "规则 1", "规则 2", "有市场的天", "抽中的天", "抽样比例", "抓的窗口"], rows))
    L.append("")
    tot = sum(r[1] for r in rows)
    L.append(f"- 共 {tot:,} 个窗口，{'在预算内，全部抓取（抽样比例 1）' if tot <= FETCH_BUDGET else '超过预算，按组均匀抽日（固定种子 ' + str(SEED) + '）'}；"
             f"抓到 {len(inf):,} 个窗口、{int(inf['pages'].sum()) if len(inf) else 0:,} 页，截断 {int(inf['truncated'].astype(bool).sum()) if len(inf) else 0}，"
             f"缺 {fstat.get('missing', 0)}；有成交的窗口 {int((inf['n'] > 0).sum()) if len(inf) else 0:,}。"
             + (f" 首次抓取 {fstat['fetch_s'] / 60:.0f} 分钟、{fstat['requests']:,} 次请求。" if fstat.get("requests") else ""))
    sampled = [GROUP_CN[g] for g, p in plan.items() if p.get("fraction", 1) < 1]
    if sampled:
        L.append(f"- 抽样的组（{', '.join(sampled)}）：每天的份数、花费、利润 = 抽中日合计 ÷ 抽中的天数，即全期合计要乘 1/抽样比例才可比；"
                 "市场数、t、p 只来自抽中的日。")
    return L


# ===================================================================== main

def _timing_text(tm):
    lab = {"discover": "发现", "klines": "K 线", "decide": "结算核对", "points": "模型", "fetch": "抓成交"}
    return "，".join(f"{lab.get(k, k)} {v / 60:.1f} 分钟" for k, v in tm.items() if k in lab)


def run(args, log=print):
    t_start = time.time()
    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)
    http = rs.Http(cache, rate=args.rate)
    tp = cache / "timing.json"
    timing = json.loads(tp.read_text()) if tp.exists() else {}
    save_t = lambda: tp.write_text(json.dumps(timing, indent=1))

    mp, np_ = cache / "markets.parquet", cache / "discover_notes.json"
    if mp.exists() and not args.refresh:
        mk, notes = pd.read_parquet(mp), json.loads(np_.read_text())
    else:
        t0 = time.time()
        mk, notes = discover(http, log=log)
        mk = rule_check(mk)
        mk.to_parquet(mp)
        np_.write_text(json.dumps(notes, indent=1))
        timing["discover"] = time.time() - t0
        save_t()
    log(mk.groupby(["group", "kind"]).agg(markets=("cid", "size"), rule_bad=("rule", lambda s: int((s != "").sum()))).to_string())
    if args.step == "discover":
        return

    kp = cache / "klines_notes.json"
    if kp.exists():
        kn = json.loads(kp.read_text())
    else:
        t0 = time.time()
        kn = {"missing": download_klines(http, log=log)}
        for coin in COINS:
            mins = minutes(coin, log=log)
            kn[coin] = {"min": minute_meta(coin), "sec": sec_check(coin, mins)}
            log(f"{coin}: {kn[coin]['sec']}")
        kn["hour_check"] = hour_check(http, minutes("BTC"))
        kp.write_text(json.dumps(kn, indent=1))
        timing["klines"] = time.time() - t0
        save_t()
    if args.step == "klines":
        return

    dp, dnp = cache / "decided.parquet", cache / "decide_notes.json"
    if dp.exists():
        dec, dn = pd.read_parquet(dp), json.loads(dnp.read_text())
    else:
        t0 = time.time()
        dec, dn = decide_all(mk, log=log)
        dec.to_parquet(dp)
        dnp.write_text(json.dumps(dn, indent=1))
        timing["decide"] = time.time() - t0
        save_t()
    if args.step == "decide":
        return

    cpp = cache / "checkpoints.parquet"
    if cpp.exists():
        cp = pd.read_parquet(cpp)
    else:
        t0 = time.time()
        cp = pd.concat([points_coin(dec, c, log=log) for c in COINS], ignore_index=True)
        cp.to_parquet(cpp)
        timing["points"] = time.time() - t0
        save_t()
    win = windows(cp)
    active = {g: sorted(dec[(dec["group"] == g) & (dec["excluded"] == "")]["day"].unique()) for g in GROUPS}
    pp = cache / "plan.json"
    if pp.exists():
        plan = json.loads(pp.read_text())
    else:
        plan = sample_plan(win, active)
        pp.write_text(json.dumps(plan, indent=1))
    log({g: (p["windows"], p["kept_windows"], round(p["fraction"], 3)) for g, p in plan.items()})
    if args.step == "points":
        return

    t0, calls0 = time.time(), http.calls
    tr, inf, kept = fetch(http, win, dec, plan, cache, log=log)
    if http.calls - calls0 > 100:
        timing["fetch"] = timing.get("fetch", 0.0) + time.time() - t0
        timing["requests"] = timing.get("requests", 0) + http.calls - calls0
        save_t()
    got = set(zip(inf["cid"], inf["check"].astype(int))) if len(inf) else set()
    fstat = {"missing": int(sum((c, int(k)) not in got for c, k in zip(kept["cid"], kept["check"]))),
             "fetch_s": timing.get("fetch", 0.0), "requests": timing.get("requests", 0)}
    if args.step == "fetch":
        return

    kd = {g: p["kept_days"] for g, p in plan.items()}
    cpk = cp[[d in set(kd.get(g, [])) for g, d in zip(cp["group"], cp["day"])]]
    wt = window_trades(tr, kept)
    rows = side_rows(cpk, wt, dec)
    rows.to_parquet(cache / "rows.parquet")
    cells, bk = [], []
    for rule in RULES:
        for g in GROUPS:
            c = cell(rows, rule, g, kd[g])
            cells.append(c)
            if c["applies"]:
                bk.append(by_kind(rows, rule, g, kd[g]).assign(rule=rule, group=g))
    (cache / "cells.json").write_text(json.dumps(cells, indent=1, default=lambda o: o if not isinstance(o, (np.generic,)) else o.item()))
    ctx = {"cells": cells, "plan": plan, "dec": dec, "bykind": pd.concat(bk, ignore_index=True) if bk else None,
           "coverage": coverage(dec), "coverage_notes": coverage_notes(dec, notes), "evidence": rule_evidence(dec),
           "kline_notes": kline_notes(kn, dn), "fetch_notes": fetch_notes(win, plan, inf, kept, fstat),
           "runtime": time.time() - t_start, "timing_text": _timing_text(timing)}
    text = report(ctx, args.out)
    log(text)
    log(f"runtime {time.time() - t_start:.0f} s; requests {http.calls:,} (cache hits {http.hits:,})")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", nargs="?", default="all", choices=["all", "discover", "klines", "decide", "points", "fetch", "report"])
    ap.add_argument("--cache", default=str(CACHE))
    ap.add_argument("--out", default=str(HERE / "real" / "fav.md"))
    ap.add_argument("--rate", type=float, default=RATE)
    ap.add_argument("--refresh", action="store_true", help="rediscover markets")
    args = ap.parse_args(argv)
    run(args)


if __name__ == "__main__":
    main()
