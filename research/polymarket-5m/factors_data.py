"""A 1-minute panel of BTC spot, perpetual-futures positioning and implied volatility, every value
stamped by the moment it became known (data for the study written down in FACTORS.md; market data
only, no keys, no orders).

Sources (all public, read-only):
- Binance spot BTCUSDT 1 s klines (data.binance.vision, already downloaded; open time in ms before
  2025 and in us from 2025 on), aggregated here to 1 minute.
- Binance USD-M perpetual BTCUSDT, data.binance.vision (downloaded here, cached, resumable, retried,
  each zip checked against Binance's .CHECKSUM sha256):
    futures/um/daily/klines/BTCUSDT/1m/BTCUSDT-1m-YYYY-MM-DD.zip
    futures/um/daily/premiumIndexKlines/BTCUSDT/1m/BTCUSDT-1m-YYYY-MM-DD.zip
    futures/um/daily/metrics/BTCUSDT/BTCUSDT-metrics-YYYY-MM-DD.zip
    futures/um/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-YYYY-MM.zip
  Checked by downloading them (2026-10-04): klines and premium-index klines carry a header row
  (open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,
  taker_buy_quote_volume,ignore) and open_time in ms; metrics carry create_time as a UTC string
  'YYYY-MM-DD HH:MM:SS' every 5 minutes, rows can be missing (none are for 2026-03-14..09-30) and
  are not always in time order;
  fundingRate carries calc_time in ms (often a few ms after the hour), funding_interval_hours and
  last_funding_rate. Every timestamp goes through `to_ns`, which reads ms, us or ns by magnitude.
- Deribit DVOL: 1-minute candles (dvol.csv, columns t = candle open time in ms, close; Deribit only
  keeps about six months of 1-minute history, so the file starts 2026-04-01 07:15 and cannot be
  extended back) and, as an extension not in FACTORS.md, hourly candles fetched here from
  www.deribit.com/api/v2/public/get_volatility_index_data (resolution 3600), which reach back to
  March. The candle timestamp is its open: the live candle of the current minute/hour is returned
  with the current minute's/hour's timestamp (checked 2026-10-04 against the server clock).

The panel. Index `t`: every UTC minute from START 00:00 to END + 1 day 00:00 inclusive. Row T holds
what was known at the instant T (inclusive) and nothing later, so a factor for a decision at T may
use row T and earlier rows. A value carried forward is never carried past the stated maximum age;
past it the cell is NaN (causal: it does not look at when the next value arrives).

Column                        known at (and how it is carried)
spot_close                    close of the spot 1 s kline of the last second present in the minute
                              that opened at T - 60 s (= the last spot price before T); known at
                              minute open + 60 s; not carried (NaN if the minute has no 1 s kline)
spot_vol, spot_buy            base volume and taker-buy base volume summed over that minute
spot_trades, spot_nsec        number of trades, number of 1 s klines present (60 when complete)
fut_close                     perpetual 1 m kline with open time T - 60 s: close; known at open + 60 s
                              (its close_time is open + 59.999 s); not carried
fut_vol, fut_buy, fut_trades  its volume, taker_buy_volume (base) and count
premium                       premium-index 1 m kline with open time T - 60 s: close (Binance's
                              premium index, perpetual impact price against the spot index as a
                              fraction of the index, at the end of that minute); known at open + 60 s;
                              not carried
sum_open_interest             metrics row with create_time c; known at c + 5 min (FACTORS.md: only rows
sum_open_interest_value       stamped <= decision - 5 min); the latest known row is carried at most
count_toptrader_long_short_ratio  15 min past c + 5 min, so a gap between rows longer than 15 min is
sum_toptrader_long_short_ratio    never filled across (inside it the first 15 min still show the last
count_long_short_ratio            row, as a live reader would, then NaN). Binance meanings: OI in BTC
sum_taker_long_short_vol_ratio    and USDT; top-trader account L/S; top-trader position L/S; all-
                              account L/S; taker buy/sell volume ratio over [c, c + 5 min).
metrics_time, metrics_age_s   create_time of the row used; seconds since it became known (c + 5 min)
funding_rate                  last_funding_rate of the latest settlement with calc_time <= T (known
funding_interval_hours        from calc_time on, to the ms, so a settlement at 00:00:00.005 first
funding_time                  shows in the 00:01 row); carried until the next one, no cap.
                              funding_time is that calc_time.
dvol, dvol_age_s              DVOL 1 m candle with open time t: close, known at t + 60 s; carried at
                              most 15 min; age in seconds since known
dvol_1h, dvol_1h_age_s        DVOL hourly candle with open time t: close, known at t + 3600 s;
                              carried at most 75 min (one hour plus 15 min)

Kline-derived columns are left NaN where the source minute is missing (nothing is forward-filled, so
a gap is visible); a factor builder decides what to do with them.

Checked on the real data (2026-03-14..09-30, 2026-10-04):
- A metrics row stamped c describes the five minutes [c, c + 5 min): its sum_taker_long_short_vol_ratio
  equals taker-buy / taker-sell volume of the perpetual 1 m klines opening c .. c + 4 min (correlation
  0.999, median relative error 0.02%; the window [c - 5, c) gives 0.12), and sum_open_interest_value /
  sum_open_interest equals the perpetual price at c + 5 min (median error 0.15 bp, against 5 bp at c).
  So "known at c + 5 min" is the earliest instant the row could exist: right, but with no slack for
  Binance's publication delay. `--metrics-delay-min 6` (or more) gives a stricter panel.
- DVOL hourly close at hour end equals the 1-minute close at the same instant in all 4,383 hours where
  both are fresh, so both are stamped the same way; spot and perpetual 1 m returns line up at lag 0
  (correlation 0.988, about 0.01 at lags +-1).

    python factors_data.py --cache DIR --klines DIR [--dvol dvol.csv] [--start 2026-03-14]
                           [--end 2026-09-30] [--out DIR/panel_1m.parquet] [--no-download]
                           [--no-dvol-1h] [--metrics-delay-min 5]
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

START, END = "2026-03-14", "2026-09-30"
VISION = "https://data.binance.vision/data/futures/um"
DERIBIT = "https://www.deribit.com/api/v2/public/get_volatility_index_data"
MIN = 60 * 10**9  # one minute in ns
KLINE_HEADER = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "count",
                "taker_buy_volume", "taker_buy_quote_volume", "ignore"]
METRICS = ["sum_open_interest", "sum_open_interest_value", "count_toptrader_long_short_ratio",
           "sum_toptrader_long_short_ratio", "count_long_short_ratio", "sum_taker_long_short_vol_ratio"]
FUNDING_HEADER = ["calc_time", "funding_interval_hours", "last_funding_rate"]
# dataset -> (period, url template, local file name template); {d} is a day or a month
DATASETS = {
    "klines": ("daily", VISION + "/daily/klines/BTCUSDT/1m/BTCUSDT-1m-{d}.zip", "BTCUSDT-1m-{d}.zip"),
    "premium": ("daily", VISION + "/daily/premiumIndexKlines/BTCUSDT/1m/BTCUSDT-1m-{d}.zip", "BTCUSDT-1m-{d}.zip"),
    "metrics": ("daily", VISION + "/daily/metrics/BTCUSDT/BTCUSDT-metrics-{d}.zip", "BTCUSDT-metrics-{d}.zip"),
    "funding": ("monthly", VISION + "/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-{d}.zip",
                "BTCUSDT-fundingRate-{d}.zip"),
}
METRICS_DELAY = 5 * MIN
METRICS_MAX_AGE = 15 * MIN
DVOL_MAX_AGE = 15 * MIN
DVOL_1H_MAX_AGE = 75 * MIN


# ---------------------------------------------------------------- time helpers

def to_ns(t):
    """Epoch timestamps in ms, us or ns (told apart by magnitude, as regime.load_klines does) -> ns."""
    t = np.asarray(t, dtype=np.int64)
    return np.where(t < 10**14, t * 1_000_000, np.where(t < 10**17, t * 1_000, t))


def ts_ns(values):
    """Datetime-like values (strings are read as UTC) -> int64 ns, whatever resolution pandas picked."""
    return pd.to_datetime(pd.Series(values), utc=True).dt.as_unit("ns").astype("int64").to_numpy()


def days(start, end):
    return [d.strftime("%Y-%m-%d") for d in pd.date_range(start, end, freq="D")]


def months(start, end):
    return sorted({d[:7] for d in days(start, end)})


def minute_grid(start, end):
    """int64 ns of every minute from start 00:00 to end + 1 day 00:00, inclusive."""
    lo = ts_ns([start])[0]
    hi = ts_ns([end])[0] + 86400 * 10**9
    # integer arithmetic: np.arange sizes its output in floating point and drops the end point here
    return lo + MIN * np.arange((hi - lo) // MIN + 1, dtype=np.int64)


def asof_pos(grid, known, max_age=None):
    """For each T in `grid` (ns): the position (in `known`'s own order) of the latest event with
    known <= T, provided T - known <= max_age (ns; None = no cap), else -1; and that age in seconds
    (NaN where -1). Among events known at the same instant the last one listed wins."""
    grid = np.asarray(grid, dtype=np.int64)
    known = np.asarray(known, dtype=np.int64)
    if not len(known):
        return np.full(len(grid), -1), np.full(len(grid), np.nan)
    order = np.argsort(known, kind="stable")
    ks = known[order]
    i = np.searchsorted(ks, grid, side="right") - 1
    ok = i >= 0
    age = grid - ks[np.maximum(i, 0)]
    if max_age is not None:
        ok &= age <= max_age
    return np.where(ok, order[np.maximum(i, 0)], -1), np.where(ok, age / 1e9, np.nan)


def take(values, pos):
    """values[pos] as float, NaN where pos == -1."""
    v = np.asarray(values, dtype=float)
    return np.where(pos >= 0, v[np.maximum(pos, 0)] if len(v) else np.nan, np.nan)


def take_time(ns, pos):
    """int64 ns values[pos] as datetime64[ns, UTC], NaT where pos == -1."""
    ns = np.asarray(ns, dtype=np.int64)
    raw = np.where(pos >= 0, ns[np.maximum(pos, 0)] if len(ns) else 0, np.iinfo(np.int64).min)
    return pd.DatetimeIndex(raw.astype("datetime64[ns]")).tz_localize("UTC")


def asof(grid, known, frame, max_age=None):
    """`frame` (one row per event, known at `known` ns) on `grid`: every column as float, NaN where no
    event qualifies (see asof_pos). Returns (dict of column -> array, age in seconds)."""
    pos, age = asof_pos(grid, known, max_age)
    return {c: take(frame[c].to_numpy(), pos) for c in frame.columns}, age


# ---------------------------------------------------------------- downloading

class NotFound(Exception):
    pass


def http_get(url, timeout=60):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "research"}),
                                    timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise NotFound(url) from e
        raise


def zip_ok(data):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return z.testzip() is None and len(z.namelist()) == 1
    except zipfile.BadZipFile:
        return False


def fetch(url, dest, get=http_get, tries=6, backoff=2.0, checksum=True, sleep=time.sleep):
    """Download `url` to `dest` unless a valid zip is already there. Writes atomically (a .part file
    renamed into place), retries transient failures with exponential backoff, verifies the zip and,
    when Binance publishes one, the sha256 in `url`.CHECKSUM. Returns 'cached', 'downloaded' or
    'missing' (HTTP 404; not remembered, so a later run asks again)."""
    dest = Path(dest)
    if dest.exists() and zip_ok(dest.read_bytes()):
        return "cached"
    last = None
    for k in range(tries):
        try:
            data = get(url)
            if not zip_ok(data):
                raise ValueError(f"not a valid zip: {url}")
            if checksum:
                try:
                    want = get(url + ".CHECKSUM").split()[0].decode()
                except NotFound:
                    want = None
                if want is not None and hashlib.sha256(data).hexdigest() != want:
                    raise ValueError(f"sha256 mismatch: {url}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            part = dest.with_name(dest.name + ".part")
            part.write_bytes(data)
            os.replace(part, dest)
            return "downloaded"
        except NotFound:
            return "missing"
        except Exception as e:  # network, proxy, truncated transfer, bad checksum
            last = e
            if k + 1 < tries:
                sleep(backoff ** k)
    raise RuntimeError(f"giving up on {url} after {tries} tries: {last!r}")


def raw_path(cache, name, d):
    return Path(cache) / "raw" / name / DATASETS[name][2].format(d=d)


def download(cache, start=START, end=END, names=tuple(DATASETS), workers=8, get=http_get, log=print):
    """Fetch every file of every dataset for the range; returns {name: {day or month: status}}."""
    jobs = []
    for name in names:
        period, url, _ = DATASETS[name]
        for d in (days(start, end) if period == "daily" else months(start, end)):
            jobs.append((name, d, url.format(d=d), raw_path(cache, name, d)))
    def one(j):
        try:
            return fetch(j[2], j[3], get=get)
        except RuntimeError as e:
            log(str(e))
            return "failed"

    status = {n: {} for n in names}
    with ThreadPoolExecutor(workers) as ex:
        for (name, d, _, _), s in zip(jobs, ex.map(one, jobs)):
            status[name][d] = s
    for name in names:
        c = pd.Series(status[name]).value_counts().to_dict()
        log(f"{name}: " + ", ".join(f"{v} {k}" for k, v in sorted(c.items())))
    return status


def fetch_dvol_1h(cache, start=START, end=END, get=http_get, chunk_days=30):
    """Hourly DVOL candles (open time ms, close) for the range, cached as dvol_1h.csv; re-fetched only
    when the cached file does not cover the range."""
    path = Path(cache) / "dvol_1h.csv"
    lo = int(ts_ns([start])[0] // 10**6)
    hi = int(ts_ns([end])[0] // 10**6) + 86400_000
    if path.exists():
        d = pd.read_csv(path)
        if len(d) and d["t"].min() <= lo and d["t"].max() >= hi - 3600_000:
            return d
    rows = []
    a = lo
    while a < hi:
        b = min(a + chunk_days * 86400_000, hi)
        e = b
        while True:
            for k in range(6):
                try:
                    r = json.loads(get(f"{DERIBIT}?currency=BTC&start_timestamp={a}&end_timestamp={e}&resolution=3600"))
                    break
                except NotFound:
                    raise
                except Exception:
                    if k == 5:
                        raise
                    time.sleep(2.0 ** k)
            res = r["result"]
            rows += [(int(x[0]), float(x[4])) for x in res["data"]]
            if not res.get("continuation") or res["continuation"] <= a:
                break
            e = int(res["continuation"])
        a = b
    d = pd.DataFrame(rows, columns=["t", "close"]).drop_duplicates("t").sort_values("t")
    d = d[(d["t"] >= lo) & (d["t"] <= hi)].reset_index(drop=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    d.to_csv(path, index=False)
    return d


# ---------------------------------------------------------------- readers

def _csv_from_zip(path):
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if len(names) != 1:
            raise ValueError(f"{path}: expected one file in the zip, found {names}")
        return z.read(names[0])


def read_kline_zip(path):
    """A Binance 1 m kline or premium-index kline zip (with or without header) -> open_time (ns),
    close, volume, taker_buy_volume, count."""
    raw = _csv_from_zip(path)
    head = raw[:1].isdigit()
    d = pd.read_csv(io.BytesIO(raw), header=None if head else 0)
    if not head and list(d.columns) != KLINE_HEADER:
        raise ValueError(f"{path}: unexpected header {list(d.columns)}")
    if d.shape[1] != 12:
        raise ValueError(f"{path}: expected 12 columns, found {d.shape[1]}")
    d.columns = KLINE_HEADER
    out = pd.DataFrame({"open_time": to_ns(d["open_time"].to_numpy(np.int64)),
                        "close": d["close"].astype(float), "volume": d["volume"].astype(float),
                        "taker_buy_volume": d["taker_buy_volume"].astype(float),
                        "count": d["count"].astype(float)})
    close_ns = to_ns(d["close_time"].to_numpy(np.int64))
    if len(out) and not ((close_ns - out["open_time"].to_numpy()) < MIN).all():
        raise ValueError(f"{path}: not 1-minute klines")
    return out


def read_metrics_zip(path):
    """A Binance futures metrics zip -> create_time (ns) and the six METRICS columns, in time order."""
    d = pd.read_csv(io.BytesIO(_csv_from_zip(path)))
    miss = [c for c in ["create_time", *METRICS] if c not in d.columns]
    if miss:
        raise ValueError(f"{path}: missing columns {miss}; found {list(d.columns)}")
    ct = d["create_time"]
    t = to_ns(ct.to_numpy(np.int64)) if pd.api.types.is_numeric_dtype(ct) else ts_ns(ct)
    out = pd.DataFrame({"create_time": t, **{c: pd.to_numeric(d[c], errors="coerce").astype(float) for c in METRICS}})
    return out.sort_values("create_time", kind="stable").reset_index(drop=True)


def read_funding_zip(path):
    d = pd.read_csv(io.BytesIO(_csv_from_zip(path)))
    if list(d.columns) != FUNDING_HEADER:
        raise ValueError(f"{path}: unexpected header {list(d.columns)}")
    return pd.DataFrame({"calc_time": to_ns(d["calc_time"].to_numpy(np.int64)),
                         "funding_interval_hours": d["funding_interval_hours"].astype(float),
                         "funding_rate": d["last_funding_rate"].astype(float)})


def spot_minutes(path):
    """A Binance spot 1 s kline zip -> one row per minute that has any 1 s kline: open (ns), close of
    the last second present, summed volume, taker-buy volume and trades, and seconds present."""
    raw = _csv_from_zip(path)
    d = pd.read_csv(io.BytesIO(raw), header=None if raw[:1].isdigit() else 0, usecols=[0, 4, 5, 8, 9])
    d.columns = ["t", "close", "vol", "trades", "buy"]
    sec = to_ns(d["t"].to_numpy(np.int64)) // 10**9
    d = d.assign(sec=sec).sort_values("sec", kind="stable").drop_duplicates("sec", keep="last")
    d["m"] = (d["sec"] // 60) * 60 * 10**9
    g = d.groupby("m", sort=True).agg(close=("close", "last"), vol=("vol", "sum"), buy=("buy", "sum"),
                                       trades=("trades", "sum"), nsec=("sec", "size"))
    g = g.astype(float)
    g.index.name = "open"
    return g.reset_index()


def load_spot(klines_dir, cache, start=START, end=END):
    """Spot 1 m rows for the range from the 1 s zips, each day cached as parquet; plus the days
    whose 1 s file is absent."""
    parts, missing = [], []
    for d in days(start, end):
        src = Path(klines_dir) / f"BTCUSDT-1s-{d}.zip"
        dst = Path(cache) / "spot_1m" / f"{d}.parquet"
        if not src.exists():
            missing.append(d)
            continue
        if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
            parts.append(pd.read_parquet(dst))
            continue
        m = spot_minutes(src)
        dst.parent.mkdir(parents=True, exist_ok=True)
        m.to_parquet(dst, index=False)
        parts.append(m)
    cols = ["open", "close", "vol", "buy", "trades", "nsec"]
    out = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=cols)
    return out, missing


def read_dvol(path):
    d = pd.read_csv(path)
    return pd.DataFrame({"t": to_ns(d["t"].to_numpy(np.int64)), "close": d["close"].astype(float)})


def load_raw(cache, name, start=START, end=END):
    """Concatenate the downloaded files of one dataset; returns (frame, rows per day or month)."""
    period, _, _ = DATASETS[name]
    reader = {"klines": read_kline_zip, "premium": read_kline_zip, "metrics": read_metrics_zip,
              "funding": read_funding_zip}[name]
    parts, rows = [], {}
    for d in (days(start, end) if period == "daily" else months(start, end)):
        p = raw_path(cache, name, d)
        if p.exists():
            f = reader(p)
            parts.append(f)
            rows[d] = len(f)
        else:
            rows[d] = 0
    return (pd.concat(parts, ignore_index=True) if parts else None), rows


# ---------------------------------------------------------------- the panel

def build_panel(cache, klines_dir=None, dvol=None, dvol_1h=None, start=START, end=END, metrics_delay=METRICS_DELAY):
    """The 1-minute panel (see the module docstring for every column's known-at rule) and a dict of
    per-source details for the coverage report."""
    grid = minute_grid(start, end)
    cols, info = {}, {}

    if klines_dir is not None:
        s, info["spot_missing_days"] = load_spot(klines_dir, cache, start, end)
        v, _ = asof(grid, s["open"].to_numpy(np.int64) + MIN, s[["close", "vol", "buy", "trades", "nsec"]], max_age=0)
        cols.update({f"spot_{c}": x for c, x in v.items()})

    k, info["klines_rows"] = load_raw(cache, "klines", start, end)
    if k is not None:
        v, _ = asof(grid, k["open_time"].to_numpy() + MIN, k[["close", "volume", "taker_buy_volume", "count"]], max_age=0)
        cols.update(fut_close=v["close"], fut_vol=v["volume"], fut_buy=v["taker_buy_volume"], fut_trades=v["count"])

    p, info["premium_rows"] = load_raw(cache, "premium", start, end)
    if p is not None:
        v, _ = asof(grid, p["open_time"].to_numpy() + MIN, p[["close"]], max_age=0)
        cols["premium"] = v["close"]

    m, info["metrics_rows"] = load_raw(cache, "metrics", start, end)
    if m is not None:
        info["metrics_raw"] = m
        ct = m["create_time"].to_numpy(np.int64)
        pos, age = asof_pos(grid, ct + metrics_delay, METRICS_MAX_AGE)
        cols.update({c: take(m[c].to_numpy(), pos) for c in METRICS})
        cols["metrics_time"], cols["metrics_age_s"] = take_time(ct, pos), age

    f, info["funding_rows"] = load_raw(cache, "funding", start, end)
    if f is not None:
        info["funding_raw"] = f
        ft = f["calc_time"].to_numpy(np.int64)
        pos, _ = asof_pos(grid, ft)
        cols["funding_rate"] = take(f["funding_rate"].to_numpy(), pos)
        cols["funding_interval_hours"] = take(f["funding_interval_hours"].to_numpy(), pos)
        cols["funding_time"] = take_time(ft, pos)

    if dvol is not None:
        d = dvol if isinstance(dvol, pd.DataFrame) else read_dvol(dvol)
        v, cols["dvol_age_s"] = asof(grid, to_ns(d["t"].to_numpy(np.int64)) + MIN, d[["close"]], DVOL_MAX_AGE)
        cols["dvol"] = v["close"]
    if dvol_1h is not None:
        d = dvol_1h if isinstance(dvol_1h, pd.DataFrame) else pd.read_csv(dvol_1h)
        v, cols["dvol_1h_age_s"] = asof(grid, to_ns(d["t"].to_numpy(np.int64)) + 60 * MIN, d[["close"]], DVOL_1H_MAX_AGE)
        cols["dvol_1h"] = v["close"]

    order = ["spot_close", "spot_vol", "spot_buy", "spot_trades", "spot_nsec", "fut_close", "fut_vol", "fut_buy",
             "fut_trades", "premium", *METRICS, "metrics_time", "metrics_age_s", "funding_rate",
             "funding_interval_hours", "funding_time", "dvol", "dvol_age_s", "dvol_1h", "dvol_1h_age_s"]
    idx = pd.DatetimeIndex(grid.astype("datetime64[ns]"), name="t").tz_localize("UTC")
    return pd.DataFrame({c: cols[c] for c in order if c in cols}, index=idx), info


# ---------------------------------------------------------------- coverage

KEYS = (("spot 1 s -> 1 m", "spot_close"), ("perp 1 m klines", "fut_close"), ("premium index", "premium"),
        ("metrics (5 m)", "sum_open_interest"), ("funding", "funding_rate"), ("DVOL 1 m", "dvol"),
        ("DVOL 1 h", "dvol_1h"))


def nan_runs(mask):
    """(start position, length) of each run of True in a boolean array."""
    m = np.r_[False, np.asarray(mask, bool), False].astype(np.int8)
    d = np.diff(m)
    s, e = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
    return list(zip(s.tolist(), (e - s).tolist()))


def coverage(panel, info, status=None, top=8):
    """Plain-text coverage: rows, non-missing share per source, missing runs, and per-day file gaps."""
    idx = panel.index
    L = [f"panel: {len(panel):,} rows, {idx[0]:%Y-%m-%d %H:%M} -> {idx[-1]:%Y-%m-%d %H:%M} UTC, "
         f"{panel.shape[1]} columns"]
    for label, col in KEYS:
        if col not in panel:
            L.append(f"- {label}: not in the panel")
            continue
        x = panel[col].to_numpy(float)
        ok = np.isfinite(x)
        runs = nan_runs(~ok)
        first = idx[ok.argmax()] if ok.any() else None
        inner = [(s, n) for s, n in runs if first is not None and idx[s] > first]  # after the first value
        L.append(f"- {label} ({col}): {ok.sum():,} of {len(x):,} minutes ({100 * ok.mean():.2f}%); first value "
                 f"{first:%Y-%m-%d %H:%M}; {len(inner)} missing runs after it, {sum(n for _, n in inner):,} minutes"
                 if first is not None else f"- {label} ({col}): no values")
        for s, n in sorted(inner, key=lambda r: -r[1])[:top]:
            L.append(f"    {idx[s]:%Y-%m-%d %H:%M} .. {idx[s + n - 1]:%Y-%m-%d %H:%M} ({n} min)")
    if "spot_nsec" in panel:
        part = panel["spot_nsec"].between(1, 59).sum()
        L.append(f"- spot minutes with fewer than 60 one-second klines: {part:,}")
    for name, full in (("klines", 1440), ("premium", 1440), ("metrics", 288)):
        rows = info.get(f"{name}_rows") or {}
        absent = [d for d, n in rows.items() if n == 0]
        short = {d: n for d, n in rows.items() if 0 < n < full}
        st = (status or {}).get(name, {})
        n404 = [d for d, s in st.items() if s == "missing"]
        L.append(f"- {name} files: {len(rows) - len(absent)} of {len(rows)} days present"
                 + (f"; absent {absent}" if absent else "") + (f" (HTTP 404: {n404})" if n404 else "")
                 + (f"; {len(short)} short days: " + ", ".join(f"{d} {n}" for d, n in list(short.items())[:12])
                    + (" ..." if len(short) > 12 else "") if short else ""))
    if info.get("spot_missing_days"):
        L.append(f"- spot 1 s files absent: {info['spot_missing_days']}")
    m = info.get("metrics_raw")
    if m is not None and len(m):
        ct = np.unique(m["create_time"].to_numpy())
        dup = len(m) - len(ct)
        lo = ts_ns([idx[0].strftime("%Y-%m-%d")])[0]
        expect = lo + 5 * MIN * np.arange((idx[-1].value - lo - 1) // (5 * MIN) + 1, dtype=np.int64)  # to end 23:55
        miss = np.setdiff1d(expect, ct).size
        off = int((ct % (5 * MIN) != 0).sum())
        gaps = np.diff(ct) // MIN
        big = np.flatnonzero(gaps > 15)
        L.append(f"- metrics rows: {len(m):,} ({dup} duplicate create_times, {off} off the 5-minute grid); "
                 f"{miss:,} of {len(expect):,} five-minute stamps missing; {(gaps > 5).sum():,} gaps > 5 min, "
                 f"{len(big)} > 15 min")
        for i in big[np.argsort(-gaps[big])][:top]:
            L.append(f"    {pd.Timestamp(ct[i], tz='UTC'):%Y-%m-%d %H:%M} -> {pd.Timestamp(ct[i + 1], tz='UTC'):%Y-%m-%d %H:%M}"
                     f" ({gaps[i]} min)")
    f = info.get("funding_raw")
    if f is not None and len(f):
        ft = np.sort(f["calc_time"].to_numpy())
        lo, hi = idx[0].value - 86400 * 10**9, idx[-1].value
        sel = ft[(ft >= lo) & (ft <= hi)]
        g = np.diff(sel) / 3.6e12
        L.append(f"- funding settlements in range: {len(sel)}; spacing (h) "
                 + ", ".join(f"{k:g}: {v}" for k, v in pd.Series(np.round(g, 2)).value_counts().sort_index().items()))
    return L


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", required=True, help="where raw zips, per-day spot caches and the panel go")
    ap.add_argument("--klines", help="directory of spot BTCUSDT-1s-YYYY-MM-DD.zip")
    ap.add_argument("--dvol", help="DVOL 1-minute csv (t ms, close)")
    ap.add_argument("--start", default=START)
    ap.add_argument("--end", default=END)
    ap.add_argument("--out", help="default: CACHE/panel_1m.parquet")
    ap.add_argument("--no-download", action="store_true", help="use only files already in the cache")
    ap.add_argument("--no-dvol-1h", action="store_true", help="skip the hourly DVOL backfill from Deribit")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--metrics-delay-min", type=float, default=5.0,
                    help="a metrics row stamped c is known at c + this many minutes (FACTORS.md: 5)")
    a = ap.parse_args(argv)
    status = None if a.no_download else download(a.cache, a.start, a.end, workers=a.workers)
    d1h, p1h = None, Path(a.cache) / "dvol_1h.csv"
    if not a.no_dvol_1h:
        d1h = fetch_dvol_1h(a.cache, a.start, a.end) if not a.no_download else (pd.read_csv(p1h) if p1h.exists() else None)
    panel, info = build_panel(a.cache, a.klines, a.dvol, d1h, a.start, a.end, int(a.metrics_delay_min * MIN))
    out = Path(a.out or Path(a.cache) / "panel_1m.parquet")
    out.parent.mkdir(parents=True, exist_ok=True)
    panel.to_parquet(out)
    print(f"wrote {out}")
    print("\n".join(coverage(panel, info, status)))


if __name__ == "__main__":
    main()
