"""BTC markets whose result is fixed before they settle: what still trades below 1 after the result
is known, and how 'near-certain' sides are priced (RESOLVED.md; market data only, no orders, no keys).

    python resolved.py all      [--cache DIR] [--klines DIR] [--out real/resolved.md]
    python resolved.py discover | spot | decide | trades | report     (the same steps one by one)

Steps
1. discover: Gamma events of 2026-03-14 .. 2026-09-30 (dates in ET), all closed:
   - daily  "bitcoin-above-on-<d>" (above), "bitcoin-price-on-<d>" (range), "bitcoin-up-or-down-on-<d>"
     (updown_day) and "what-price-will-bitcoin-hit-on-<d>" (hit_daily); every slug is asked in both
     spellings Gamma used this year ("june-3" and "june-3-2026");
   - weekly / monthly "what-price-will-bitcoin-hit-..." events: Gamma public-search ("what price will
     bitcoin hit", closed, every page until hasMore is false) plus the slugs of every Monday-Sunday
     week and every month in both spellings; an event is kept when its last ET day is in the period;
   - "btc-updown-4h-<epoch>" for every 4-hour boundary (UTC) in the period.
   Per market: condition id, tokens, strike / range / direction, window, endDate, closedTime
   (settlement), outcomePrices (official), rewards fields, fee fields.
2. spot: Binance BTCUSDT 1 s klines (the shared kline folder, 03-14 .. 09-30; 03-01 .. 03-13 and
   10-01 downloaded from data.binance.vision into the cache for the windows that start earlier /
   end later). Open times in us or ms (as regime.load_klines). Prices kept in integer cents (BTCUSDT
   tick 0.01) so 'High >= level' is exact. 1-minute High / Low / Close by OPEN time; a minute's
   close is known at open + 60 s. 20 random days are compared with data.binance.vision 1m klines
   as asked; because the 1 s aggregates turned out WIDER than Binance's own 1m High / Low in ~0.3%
   of minutes (by 1 cent up to a few dollars; closes always equal), every day's 1m klines are
   downloaded too and used for T* and outcomes (they are the settlement source); 1 s klines are
   used only for t_sec, the price before t and sigma.
3. decide: T* per market (RESOLVED.md):
   - hit: window from the market description (daily: 00:00-23:59 ET of the date; weekly: 00:00 ET
     of the first date to 23:59 ET of the last; monthly: the month; "from the creation of this
     market": from createdAt rounded UP to the next whole minute, conservative - a candle that
     straddles creation is not used). The candles of the window are those OPENING in [start, end).
     T* = open + 60 s of the first candle with High >= level (up) / Low <= level (down); if none,
     the result (No) is fixed at the window end: T* = end. t_sec = the first 1 s kline in the window
     that crosses (the earliest one could know, reported only, with the trades in [t_sec + L, T*)).
   - above (close > strike), range (lo <= close < hi; a close exactly on a boundary goes to the
     higher bracket as the rules say), updown_day (today's noon close vs yesterday's; equal = 50-50):
     the "1 minute candle for 12:00 ET". Two conventions are computed for every market: "open12"
     (the candle opening 12:00:00 ET, closing 12:01:00; Binance labels candles by open time) and
     "close12" (the candle opening 11:59, closing at 12:00, as ladder.py's updown reference). The one
     that agrees with every official outcome is used (both are reported); T* = its close time.
   - updown_4h (Chainlink, control only): T* = window end; official outcome used as the truth; the
     Binance proxy (close before end vs close before start) is reported but not used to exclude.
   Every market's computed outcome is compared with outcomePrices; a mismatch means the rule or the
   data is wrong for that market: it is excluded and listed. Hit markets touched (by the written
   window) before they were created are kept but counted and flagged in the report: one such market
   was resolved "from creation" against its description, so their resolution practice is a risk. Unresolved markets and markets whose
   window lacks klines are excluded and counted.
4. trades: data-api.polymarket.com/trades?market=<conditionId> (taker records only, the API
   default: each match once, side = the taker's side, asset = the token the taker traded), from
   min(T*, end - 3600 s) on. Pages of 1000 by offset; the API refuses offsets above 10000, so when
   a page at the offset cap is still full the next request window ends at the oldest timestamp seen
   (end is inclusive; repeated records are merged by multiplicity) and paging restarts; a market is
   'truncated' only if more than 11000 trades share one second (then reported).
5. measure:
   - decided-but-unsettled: trades of the winning token at p < 1 with ts >= T* + L (L = 0, 1, 5, 30,
     60 s; ts in whole seconds, T* on a whole second; the data-api timestamp is the on-chain time,
     2-4 s after the match (README section 16), so only L >= 5 is clean), any taker side (evidence of
     a price; the record's price is the taker's average fill price), and the
     taker-SELL subset (what a resting buyer could have received). Per share 1 - p as maker, minus
     0.07 p (1 - p) as taker. Shares, notional p * size, profit (1 - p) * size, price buckets, hours
     to closedTime, per ET day dollars, by kind and month. Losing-token trades at p > 0 after T*, by
     taker side. Markets settled 50-50 have no winner and are left out here. Trades stamped after
     closedTime are already settled: counted apart, not as 'unsettled'. The "all" rows, the per-day
     frame and the verdict use the six Binance-settled kinds only (updown_4h is the Chainlink
     control); per-day dollars are over the ET days FIRST .. LAST, empty days as 0 (trades on days
     outside the period are reported apart), with means and medians. Markets 'decided' only under a
     reading the official result settled later (noon markets whose two candle conventions disagree;
     hit markets touched before creation) carry a risk flag and are reported with and without.
   - near-certain: at 60, 30, 10, 5, 1 minutes before the market end (endDate; for hit markets the
     window end), still undecided (hit: no 1 s High / Low across the level yet; creation windows:
     already started), the driftless model: S = close of the 1 s kline opening at t - 1, sigma = std
     of 1 s log returns over [t - 3600, t), tau = seconds to the decision (noon: T* of the chosen
     convention; hit: window end; 4h: end), above: Phi(ln(S/K) / (sigma sqrt(tau))), range: the
     difference of two such, up/down: same with the reference close, hit: 2 Phi(-|ln(K/S)| /
     (sigma sqrt(tau))) (reflection, capped at 1). The favoured side (model >= 0.5) in buckets
     >= 0.99 and [0.95, 0.99); its token's trades with ts in [t, t + 60): price, official result,
     pnl per share = result - p (and minus the taker fee). Means are share-weighted; t statistics
     are clustered by market (cluster-robust variance of the weighted mean).
6. report: real/resolved.md (Chinese) and real/resolved-trades.csv.gz (post-T* winning-token trades).

Everything fetched is cached under the cache folder (one gzip JSON per URL, trade pages stored slim),
so a rerun does not ask again. Requests are limited to 4 per second over all threads, with retries
and exponential backoff.
"""
from __future__ import annotations

import argparse
import calendar
import gzip
import hashlib
import io
import json
import math
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

import ladder as lad

HERE = Path(__file__).resolve().parent
SCRATCH = Path("/tmp/claude-0/-home-user-whole-web-station-clone/d12980d9-5808-53a6-9da0-aeb3bb8bdfb4/scratchpad")
CACHE = SCRATCH / "resolved"
KLINES = SCRATCH / "klines"
GAMMA = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com/trades"
VISION = "https://data.binance.vision/data/spot/daily/klines/BTCUSDT/{iv}/BTCUSDT-{iv}-{d}.zip"
FIRST, LAST = date(2026, 3, 14), date(2026, 9, 30)
SPOT_FIRST, SPOT_LAST = date(2026, 3, 1), date(2026, 10, 1)   # 1 s klines needed (UTC days)
LAGS = (0, 1, 5, 30, 60)
CHECKS = (60, 30, 10, 5, 1)
FEE = 0.07
PAGE, MAX_OFFSET = 1000, 10000
RATE = 4.0
H4_S = 4 * 3600
KINDS = ("hit_daily", "hit_weekly", "hit_monthly", "above", "range", "updown_day", "updown_4h")
CONTROL = "updown_4h"                                   # Chainlink-settled: a control, never in a verdict
BINANCE_KINDS = tuple(k for k in KINDS if k != CONTROL)
MIN_G = 20                                              # fewer clusters than this: no t statistic printed
KIND_LABEL = {"all": "全部币安结算", CONTROL: f"{CONTROL}（对照）"}
NOON_KINDS = ("above", "range", "updown_day")
HIT_KINDS = ("hit_daily", "hit_weekly", "hit_monthly")
CONVS = ("open12", "close12")
PREFIX = {"above": "bitcoin-above-on", "range": "bitcoin-price-on", "updown_day": "bitcoin-up-or-down-on",
          "hit_daily": "what-price-will-bitcoin-hit-on"}
HIT = "what-price-will-bitcoin-hit"
MONTHS = lad.MONTHS
PBUCKETS = (0.0, 0.98, 0.99, 0.995, 0.999, 1.0)
NAN = float("nan")


# ===================================================================== HTTP (cached, rate limited)

def _urlopen(url, timeout=60, binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": "research-resolved/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return raw if binary else json.loads(raw)


class Http:
    """GET with a per-URL gzip JSON cache, a global request rate and retries with backoff. `opener`
    (url -> parsed JSON) is replaceable for tests. `slim` (url, data) -> data may shrink what is
    cached (and returned)."""

    def __init__(self, cache, rate=RATE, opener=_urlopen, sleep=time.sleep, tries=7, clock=time.monotonic):
        self.dir = Path(cache) / "http"
        self.rate, self.opener, self.sleep, self.tries, self.clock = rate, opener, sleep, tries, clock
        self.lock = threading.Lock()
        self.next_t = 0.0
        self.calls = self.hits = 0

    def _path(self, url):
        h = hashlib.sha1(url.encode()).hexdigest()
        return self.dir / h[:2] / f"{h}.json.gz"

    def _wait(self):
        with self.lock:
            now = self.clock()
            t = max(now, self.next_t)
            self.next_t = t + 1.0 / self.rate
        if t > now:
            self.sleep(t - now)

    def get(self, url, slim=None, cache=True):
        path = self._path(url)
        if cache and path.exists():
            self.hits += 1
            return json.loads(gzip.decompress(path.read_bytes()))
        err = None
        for k in range(self.tries):
            self._wait()
            try:
                data = self.opener(url)
                self.calls += 1
                break
            except urllib.error.HTTPError as e:
                if e.code in (400, 401, 403, 404, 422):
                    raise
                err = e
            except Exception as e:  # network, proxy, truncated transfer
                err = e
            self.sleep(min(60.0, 1.5 * 2 ** k))
        else:
            raise RuntimeError(f"giving up on {url}: {err!r}")
        if slim is not None:
            data = slim(url, data)
        if cache:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".tmp{threading.get_ident()}")
            tmp.write_bytes(gzip.compress(json.dumps(data, separators=(",", ":")).encode()))
            tmp.replace(path)
        return data

    def download(self, url, path):
        """Binary file to `path` (kept; not fetched again)."""
        path = Path(path)
        if path.exists() and path.stat().st_size > 0:
            return path
        err = None
        for k in range(self.tries):
            self._wait()
            try:
                raw = _urlopen(url, binary=True)
                self.calls += 1
                break
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise
                err = e
            except Exception as e:
                err = e
            self.sleep(min(60.0, 1.5 * 2 ** k))
        else:
            raise RuntimeError(f"giving up on {url}: {err!r}")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(raw)
        tmp.replace(path)
        return path


# ===================================================================== time helpers

def ts_of(s):
    """Unix seconds (float) of a Gamma time string ('2026-07-01T16:20:20Z', '2026-07-01 16:13:41+00',
    '2026-06-04T15:01:58.669Z'); NaN if missing or unreadable."""
    if s is None or s == "" or (isinstance(s, float) and math.isnan(s)):
        return NAN
    x = str(s).strip().replace(" ", "T")
    if x.endswith("Z"):
        x = x[:-1] + "+00:00"
    if re.search(r"[+-]\d\d$", x):
        x += ":00"
    try:
        d = datetime.fromisoformat(x)
    except ValueError:
        try:
            return pd.Timestamp(s).timestamp()
        except Exception:
            return NAN
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def et_midnight(d):
    return lad.et_to_utc(d, 0, 0)


def days(a, b):
    out, d = [], a
    while d <= b:
        out.append(d)
        d += timedelta(days=1)
    return out


def month_end(y, m):
    return date(y, m, calendar.monthrange(y, m)[1])


def _month(name):
    name = name.lower().strip(". ")
    for i, m in enumerate(MONTHS):
        if name == m or (len(name) >= 3 and m.startswith(name)):
            return i + 1
    return None


# ===================================================================== discovery

def daily_slugs(d):
    """kind -> both spellings of the daily slugs of ET date d."""
    short = f"{MONTHS[d.month - 1]}-{d.day}"
    return {k: [f"{p}-{short}-{d.year}", f"{p}-{short}"] for k, p in PREFIX.items()}


def week_slugs(monday):
    """Candidate slugs of the hit event of the week monday .. monday + 6 (both spellings)."""
    sun = monday + timedelta(days=6)
    m1, m2 = MONTHS[monday.month - 1], MONTHS[sun.month - 1]
    core = f"{m1}-{monday.day}-{sun.day}" if monday.month == sun.month else f"{m1}-{monday.day}-{m2}-{sun.day}"
    return [f"{HIT}-{core}-{sun.year}", f"{HIT}-{core}"]


def month_slugs(y, m):
    return [f"{HIT}-in-{MONTHS[m - 1]}-{y}", f"{HIT}-in-{MONTHS[m - 1]}"]


def h4_slugs(first=FIRST, last=LAST):
    """4-hour windows (on 4-hour UTC boundaries) STARTING on ET days first .. last."""
    a = et_midnight(first) // H4_S * H4_S
    b = et_midnight(last + timedelta(days=1))
    return [f"btc-updown-4h-{t}" for t in range(a, b, H4_S) if t >= et_midnight(first)]


def events_by_slug(http, slugs, batch=40):
    """Gamma events for these slugs (missing slugs are simply absent), deduplicated by id."""
    out = {}
    slugs = list(dict.fromkeys(slugs))
    for i in range(0, len(slugs), batch):
        part = slugs[i:i + batch]
        q = "&".join("slug=" + urllib.parse.quote(s) for s in part) + f"&limit={len(part)}"
        for ev in http.get(f"{GAMMA}/events?{q}") or []:
            out[str(ev.get("id"))] = ev
    return list(out.values())


def search_hit_slugs(http, q="what price will bitcoin hit"):
    """Every closed event slug public-search returns for q (all pages)."""
    slugs, page, pages = [], 1, 0
    while True:
        url = f"{GAMMA}/public-search?" + urllib.parse.urlencode(
            {"q": q, "limit_per_type": 50, "events_status": "closed", "page": page})
        r = http.get(url) or {}
        pages += 1
        slugs += [e.get("slug") for e in r.get("events") or [] if e.get("slug")]
        if not (r.get("pagination") or {}).get("hasMore") or page > 60:
            break
        page += 1
    return [s for s in slugs if s.startswith(HIT)], pages


_DATE = r"([A-Za-z]+)\.?\s+(\d{1,2})"


def hit_window(ev, m):
    """(period, from_creation, start, end, note) of a hit market's window in unix seconds: candles
    OPENING in [start, end). period (daily / weekly / monthly) and its dates come from the event title
    ("on June 3", "June 8-14", "August 31-September 6", "in June"), the year from the event's end;
    the market description must use the same period's wording, and if it says "from the creation of
    this market" the window starts at createdAt rounded up to the next whole minute (conservative)."""
    desc = str(m.get("description") or ev.get("description") or "").lower()
    title = str(ev.get("title") or m.get("question") or "").replace("–", "-").replace("—", "-")
    end_ts = ts_of(m.get("endDate") or ev.get("endDate"))
    if not np.isfinite(end_ts):
        return None, False, NAN, NAN, "no endDate"
    y_end = lad.et_date(int(end_ts) - 1).year
    period = first = last = None
    mm = re.search(rf"{_DATE}\s*-\s*(?:([A-Za-z]+)\.?\s+)?(\d{{1,2}})\b", title)
    if mm and _month(mm.group(1)):
        m1, d1 = _month(mm.group(1)), int(mm.group(2))
        m2 = _month(mm.group(3)) if mm.group(3) else m1
        if m2:
            period = "weekly"
            last = date(y_end, m2, int(mm.group(4)))
            first = date(y_end - (1 if m1 > m2 else 0), m1, d1)
    if period is None:
        mm = re.search(rf"\bon\s+{_DATE}", title)
        if mm and _month(mm.group(1)):
            period = "daily"
            first = last = date(y_end, _month(mm.group(1)), int(mm.group(2)))
    if period is None:
        mm = re.search(r"\bin\s+([A-Za-z]+)", title)
        if mm and _month(mm.group(1)):
            period = "monthly"
            mon = _month(mm.group(1))
            first, last = date(y_end, mon, 1), month_end(y_end, mon)
    if period is None:
        return None, False, NAN, NAN, f"period not found in {title!r}"
    words = {"daily": ("on the date specified", "between 12:00 am et and 11:59 pm et"),
             "weekly": ("date range",), "monthly": ("month specified", "during the month")}[period]
    note = "" if any(w_ in desc for w_ in words) else f"description does not read as {period}"
    start, end = et_midnight(first), et_midnight(last + timedelta(days=1))
    creation = "creation of this market" in desc
    if creation:
        created = ts_of(m.get("createdAt"))
        start = max(start, math.ceil(created / 60.0) * 60) if np.isfinite(created) else NAN
    return period, creation, float(start), float(end), note


def _list(x):
    if x is None:
        return []
    return json.loads(x) if isinstance(x, str) else list(x)


def official_yes(m, yes_idx):
    """1 / 0 / 0.5 from outcomePrices for the Yes (Up) outcome; NaN if not a resolved price pair."""
    try:
        p = [float(v) for v in _list(m.get("outcomePrices"))]
    except (TypeError, ValueError):
        return NAN
    if len(p) != 2:
        return NAN
    y = p[yes_idx]
    if abs(p[0] - p[1]) < 1e-9 and abs(y - 0.5) < 1e-9:
        return 0.5
    if max(p) > 0.99 and min(p) < 0.01:
        return 1.0 if y > 0.5 else 0.0
    return NAN


def market_rows(ev, kind, d=None):
    """Our rows for one Gamma event of `kind` (KINDS; hit events as 'hit') on ET date d."""
    rows = []
    for m in ev.get("markets") or []:
        outcomes, tokens = _list(m.get("outcomes")), _list(m.get("clobTokenIds"))
        if len(outcomes) != 2 or len(tokens) != 2 or not m.get("conditionId"):
            continue
        yes = next((i for i, o in enumerate(outcomes) if str(o).lower() in ("yes", "up")), None)
        if yes is None:
            continue
        title = m.get("groupItemTitle") or ""
        parsed = lad.parse_title(title)
        q = str(m.get("question") or "").lower()
        typ, lo, hi, level, scope, ws, we, note, creation = kind, NAN, NAN, NAN, "", NAN, NAN, "", False
        if kind == "above":
            if not parsed or parsed[0] != "level":
                continue
            lo = level = parsed[1]
        elif kind == "range":
            if not parsed or parsed[0] not in ("range", "level"):
                continue
            lo = parsed[1] if parsed[1] is not None else NAN
            hi = parsed[2] if parsed[2] is not None else NAN
        elif kind == "hit":
            shape = parsed[0] if parsed else None
            if shape == "up" or (shape == "level" and "reach" in q):
                typ, level = "hit_up", parsed[1]
            elif shape == "down" or (shape == "level" and "dip" in q):
                typ, level = "hit_down", parsed[1] if shape == "level" else parsed[2]
            else:
                mm = re.search(r"(reach|dip to)\s+\$?([0-9][0-9,]*(?:\.[0-9]+)?k?)", q)
                if not mm:
                    continue
                typ = "hit_up" if mm.group(1) == "reach" else "hit_down"
                level = lad._num(mm.group(2))
            scope, creation, ws, we, note = hit_window(ev, m)
        elif kind == "updown_day":
            pass
        elif kind == "updown_4h":
            ws = ts_of(m.get("eventStartTime") or ev.get("startTime"))
            if not np.isfinite(ws):
                ws = float(int(str(ev.get("slug", "0")).rsplit("-", 1)[-1]))
            we = ws + H4_S
        else:
            raise ValueError(kind)
        end = ts_of(m.get("endDate") or ev.get("endDate"))
        rew = m.get("clobRewards")
        fs = m.get("feeSchedule") or {}
        rows.append({
            "kind": kind if kind != "hit" else {"daily": "hit_daily", "weekly": "hit_weekly",
                                                "monthly": "hit_monthly"}.get(scope, "hit_unknown"),
            "type": typ, "from_creation": creation, "event_slug": ev.get("slug"), "event_id": str(ev.get("id")),
            "slug": m.get("slug"), "title": title, "question": m.get("question"),
            "cid": m["conditionId"], "yes_token": str(tokens[yes]), "no_token": str(tokens[1 - yes]),
            "level": float(level) if level is not None else NAN, "lo": float(lo), "hi": float(hi),
            "day": d.isoformat() if d else None, "ws": float(ws), "we": float(we), "window_note": note,
            "end": end, "closed_ts": ts_of(m.get("closedTime")) if m.get("closedTime") else ts_of(ev.get("closedTime")),
            "created": ts_of(m.get("createdAt")), "start_date": ts_of(m.get("startDate")),
            "ref_start": ts_of(m.get("eventStartTime") or ev.get("startTime")),
            "closed": bool(m.get("closed")), "uma": m.get("umaResolutionStatus"),
            "official": official_yes(m, yes), "outcome_prices": m.get("outcomePrices"),
            "neg_risk": bool(m.get("negRisk")), "volume": float(m.get("volumeNum") or m.get("volume") or 0.0),
            "rewards_min_size": float(m["rewardsMinSize"]) if m.get("rewardsMinSize") is not None else NAN,
            "rewards_max_spread": float(m["rewardsMaxSpread"]) if m.get("rewardsMaxSpread") is not None else NAN,
            "clob_rewards": json.dumps(rew) if rew else "",
            "rewards_daily": float(sum(float(r.get("rewardsDailyRate") or 0) for r in rew)) if isinstance(rew, list) else 0.0,
            "holding_rewards": bool(m.get("holdingRewardsEnabled")),
            "fees_enabled": bool(m.get("feesEnabled")), "fee_rate": float(fs.get("rate")) if fs.get("rate") is not None else NAN,
            "description": m.get("description") or ev.get("description") or ""})
    return rows


def discover(http, first=FIRST, last=LAST, log=print):
    """(markets DataFrame, coverage notes dict)."""
    want, kind_of = [], {}
    for d in days(first, last):
        for kind, ss in daily_slugs(d).items():
            for s in ss:
                want.append(s)
                kind_of[s] = (kind, d)
    mon = first - timedelta(days=first.weekday())
    while mon <= last:
        for s in week_slugs(mon):
            want.append(s)
            kind_of[s] = ("hit", None)
        mon += timedelta(days=7)
    y, m = first.year, first.month
    while (y, m) <= (last.year, last.month):
        for s in month_slugs(y, m):
            want.append(s)
            kind_of[s] = ("hit", None)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    found, pages = search_hit_slugs(http)
    for s in found:
        kind_of.setdefault(s, ("hit", None))
        want.append(s)
    for s in h4_slugs(first, last):
        want.append(s)
        kind_of[s] = ("updown_4h", None)
    evs = events_by_slug(http, want)
    log(f"gamma: {len(set(want)):,} slugs asked, {len(evs):,} events returned; search: {len(found)} hit slugs in {pages} pages")
    rows, notes = [], {"search_hit_slugs": len(found), "search_pages": pages, "events": len(evs), "skipped_events": []}
    lo_ts, hi_ts = et_midnight(first), et_midnight(last + timedelta(days=1))
    for ev in evs:
        slug = ev.get("slug")
        kind, d = kind_of.get(slug, (None, None))
        if kind is None:
            continue
        if d is not None:  # a daily slug: the short spelling can be last year's event
            expect = et_midnight(d + timedelta(days=1)) if kind == "hit_daily" else lad.noon_et(d)
            if ts_of(ev.get("endDate")) != expect:
                notes["skipped_events"].append(f"{slug}: endDate {ev.get('endDate')} is not this date's")
                continue
        if kind == "hit_daily":
            kind = "hit"
        if kind == "hit":
            end = ts_of(ev.get("endDate"))
            if not slug.startswith(HIT) or not (np.isfinite(end) and lo_ts < end <= hi_ts):
                continue  # e.g. 'before-2027' or an event of another period found by the search
            if not ev.get("closed"):
                notes["skipped_events"].append(f"{slug}: not closed")
                continue
        try:
            rows += market_rows(ev, kind, d)
        except Exception as e:  # one malformed event leaves out its markets only
            notes["skipped_events"].append(f"{slug}: {type(e).__name__}: {e}")
    mk = pd.DataFrame(rows)
    if len(mk):
        mk = mk.drop_duplicates("cid").reset_index(drop=True)
        hit = mk["kind"].str.startswith("hit")
        mk.loc[hit, "day"] = [lad.et_date(int(e) - 1).isoformat() if np.isfinite(e) else None for e in mk.loc[hit, "end"]]
        h4 = mk["kind"] == "updown_4h"
        mk.loc[h4, "day"] = [lad.et_date(int(w)).isoformat() for w in mk.loc[h4, "ws"]]
        bad = mk["kind"] == "hit_unknown"
        notes["hit_window_unparsed"] = mk.loc[bad, "slug"].tolist()
        mk = mk[~bad].reset_index(drop=True)
        noon = mk["kind"].isin(NOON_KINDS)
        notes["noon_desc_without_1200"] = int((~mk.loc[noon, "description"].str.contains("12:00")).sum())
        notes["hit_window_ne_end"] = mk.loc[mk["kind"].isin(HIT_KINDS) & (mk["we"] != mk["end"]), "slug"].tolist()
        notes["h4_window_ne_end"] = int((h4 & (mk["we"] != mk["end"])).sum())
    return mk, notes


def coverage(mk, first=FIRST, last=LAST):
    """Per kind: events, markets, days with an event, missing days (daily kinds), resolved markets."""
    out, missing = [], {}
    all_days = [d.isoformat() for d in days(first, last)]
    for k in KINDS:
        s = mk[mk["kind"] == k]
        ev_days = set(s["day"].dropna())
        row = {"kind": k, "events": s["event_slug"].nunique(), "markets": len(s),
               "resolved": int(s["official"].notna().sum()), "days": len(ev_days & set(all_days))}
        if k in ("hit_daily", "above", "range", "updown_day"):
            missing[k] = [d for d in all_days if d not in ev_days]
        elif k == "updown_4h":
            per = s.groupby("day").size()
            missing[k] = [d for d in all_days if per.get(d, 0) < 6]
        out.append(row)
    return pd.DataFrame(out), missing


# ===================================================================== Binance spot

def read_kline_zip(path):
    """(open time s, high, low, close in integer cents) from a Binance kline zip (us or ms)."""
    with zipfile.ZipFile(path) as z:
        raw = z.read(z.namelist()[0])
    d = pd.read_csv(io.BytesIO(raw), header=None, usecols=[0, 2, 3, 4])
    if not np.issubdtype(d[0].dtype, np.number):  # a header row
        d = d.iloc[1:].astype(float)
    t = d[0].to_numpy(np.int64)
    t = np.where(t > 10 ** 14, t // 1_000_000, t // 1000)
    cents = lambda c: np.rint(d[c].to_numpy(float) * 100).astype(np.int64)
    return t, cents(2), cents(3), cents(4)


class Spot:
    """Per-second Binance BTCUSDT high / low / close (cents, NaN where no 1 s kline) from sec0, the
    forward-filled close, 1-minute High / Low / Close by open time, and trailing realised sigma."""

    def __init__(self, sec0, hi, lo, cl):
        assert sec0 % 60 == 0
        self.sec0 = int(sec0)
        self.hi, self.lo, self.cl = (np.asarray(x, float) for x in (hi, lo, cl))
        self.n = len(self.hi)
        have = np.isfinite(self.cl)
        idx = np.where(have, np.arange(self.n), -1)
        np.maximum.accumulate(idx, out=idx)
        self.ff = np.where(idx >= 0, self.cl[np.maximum(idx, 0)], np.nan)
        nm = self.n // 60
        self.m0 = self.sec0 // 60
        with np.errstate(all="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            H = np.nanmax(self.hi[:nm * 60].reshape(nm, 60), axis=1)
            L = np.nanmin(self.lo[:nm * 60].reshape(nm, 60), axis=1)
        C = self.ff[:nm * 60].reshape(nm, 60)[:, 59]
        self.mhave = have[:nm * 60].reshape(nm, 60).sum(axis=1)
        self.H = np.where(np.isfinite(H), H, C)  # a minute without trades: a flat candle at the last close
        self.L = np.where(np.isfinite(L), L, C)
        self.C = C
        self.Hs, self.Ls, self.Cs = self.H, self.L, self.C  # the 1 s aggregates, kept for comparison
        self.source = "1s"
        lr = np.zeros(self.n)
        with np.errstate(all="ignore"):
            lr[1:] = np.diff(np.log(self.ff))
        ok = np.isfinite(lr)
        lr[~ok] = 0.0
        self.c1 = np.concatenate([[0.0], np.cumsum(lr)])
        self.c2 = np.concatenate([[0.0], np.cumsum(lr * lr)])
        self.cn = np.concatenate([[0], np.cumsum(ok & (np.arange(self.n) > 0))])

    @classmethod
    def from_arrays(cls, t, hi, lo, cl):
        t = np.asarray(t, np.int64)
        sec0 = int(t.min()) // 60 * 60
        n = (int(t.max()) - sec0) // 60 * 60 + 60
        H, L, C = (np.full(n, np.nan) for _ in range(3))
        k = t - sec0
        H[k], L[k], C[k] = hi, lo, cl
        return cls(sec0, H, L, C)

    def set_minutes(self, t_open, H, L, C):
        """Use Binance's own 1-minute klines (the settlement source) for High / Low / Close where
        given; minutes without one keep the 1 s aggregate."""
        i = np.asarray(t_open, np.int64) // 60 - self.m0
        ok = (i >= 0) & (i < len(self.C))
        self.H, self.L, self.C = self.Hs.copy(), self.Ls.copy(), self.Cs.copy()
        self.H[i[ok]], self.L[i[ok]], self.C[i[ok]] = np.asarray(H, float)[ok], np.asarray(L, float)[ok], np.asarray(C, float)[ok]
        self.m1 = np.zeros(len(self.C), bool)
        self.m1[i[ok]] = True
        self.source = "1m"

    def covers(self, a, b):
        """Every minute opening in [a, b) has at least one 1 s kline."""
        i, j = int(a // 60 - self.m0), int(math.ceil(b / 60) - self.m0)
        if i < 0 or j > len(self.C) or j <= i:
            return False
        return bool((self.mhave[i:j] > 0).all())

    def minute(self, open_ts):
        i = int(open_ts // 60 - self.m0)
        if i < 0 or i >= len(self.C) or self.mhave[i] == 0:
            return None
        return i

    def close_of(self, open_ts):
        i = self.minute(open_ts)
        return NAN if i is None else float(self.C[i])

    def price_before(self, t):
        k = int(t) - 1 - self.sec0
        return float(self.ff[k]) if 0 <= k < self.n else NAN

    def sigma(self, t, w=3600):
        """Std of 1 s log returns of the seconds t - w .. t - 1 (NaN if fewer than w / 2 returns)."""
        a, b = int(t) - w - self.sec0, int(t) - self.sec0
        if a < 1 or b > self.n:
            return NAN
        n = self.cn[b] - self.cn[a]
        if n < w // 2:
            return NAN
        s1, s2 = self.c1[b] - self.c1[a], self.c2[b] - self.c2[a]
        var = (s2 - s1 * s1 / n) / max(n - 1, 1)
        return math.sqrt(max(var, 0.0))


def first_touch(spot, ws, we, level, up, with_flag=False):
    """(T*, t_sec) for a barrier: T* = close time (open + 60) of the first 1-minute candle opening in
    [ws, we) whose High >= level (up) / Low <= level (down), t_sec = the first 1 s kline in that minute
    that crosses; (we, NaN) if none; (NaN, NaN) if the window is not covered by the klines. level in
    cents."""
    if not (np.isfinite(ws) and np.isfinite(we)) or not spot.covers(ws, we):
        return (NAN, NAN, NAN) if with_flag else (NAN, NAN)
    i, j = int(ws // 60 - spot.m0), int(math.ceil(we / 60) - spot.m0)
    seg = spot.H[i:j] >= level if up else spot.L[i:j] <= level
    hit = np.flatnonzero(seg)
    if not len(hit):
        return (float(we), NAN, 0.0) if with_flag else (float(we), NAN)
    t_open = (spot.m0 + i + int(hit[0])) * 60
    out = float(t_open + 60), first_second(spot, ws, t_open + 60, level, up)
    return (*out, 1.0) if with_flag else out


def first_second(spot, ws, we, level, up):
    """Open time of the first 1 s kline in [ws, we) whose high >= level (up) / low <= level (down)."""
    a, b = int(ws) - spot.sec0, int(we) - spot.sec0
    if a < 0 or b > spot.n:
        return NAN
    sec = spot.hi[a:b] >= level if up else spot.lo[a:b] <= level
    s = np.flatnonzero(sec)
    return float(ws + int(s[0])) if len(s) else NAN


def noon_open(d, conv):
    """Open time of the '12:00 ET' candle of ET date d under a convention."""
    t = lad.noon_et(d)
    return t if conv == "open12" else t - 60


def rule_outcome(kind, close, lo=NAN, hi=NAN, ref=NAN):
    """Computed Yes (Up) result: above close > lo; range lo <= close < hi (NaN bound = open end);
    updown_day close vs ref (equal = 0.5). Prices in the same unit. NaN if an input is missing."""
    if not np.isfinite(close):
        return NAN
    if kind == "above":
        return float(close > lo)
    if kind == "range":
        ok = (not np.isfinite(lo) or close >= lo) and (not np.isfinite(hi) or close < hi)
        return float(ok)
    if kind == "updown_day":
        if not np.isfinite(ref):
            return NAN
        return 0.5 if close == ref else float(close > ref)
    raise ValueError(kind)


def decide(mk, spot):
    """Add T*, t_sec, computed outcome per convention, status, exclusion reason and the chosen
    noon convention (returned with per-convention mismatch counts)."""
    mk = mk.copy()
    n = len(mk)
    out = {c: np.full(n, NAN) for c in ("tstar_open12", "tstar_close12", "comp_open12", "comp_close12",
                                         "tstar_hit", "tsec", "comp_hit", "close_open12", "close_close12",
                                         "ref_open12", "ref_close12")}
    for i, r in enumerate(mk.itertuples(index=False)):
        if r.kind in NOON_KINDS:
            d = date.fromisoformat(r.day)
            for c in CONVS:
                o = noon_open(d, c)
                close = spot.close_of(o)
                ref = spot.close_of(noon_open(d - timedelta(days=1), c)) if r.kind == "updown_day" else NAN
                out[f"close_{c}"][i] = close / 100
                out[f"ref_{c}"][i] = ref / 100
                out[f"tstar_{c}"][i] = o + 60
                out[f"comp_{c}"][i] = rule_outcome(r.kind, close, r.lo * 100, r.hi * 100, ref)
        elif r.kind in HIT_KINDS:
            out["tstar_hit"][i], out["tsec"][i], out["comp_hit"][i] = first_touch(
                spot, r.ws, r.we, r.level * 100, r.type == "hit_up", with_flag=True)
        elif r.kind == "updown_4h":
            a, b = spot.price_before(r.ws), spot.price_before(r.we)
            out["tstar_hit"][i] = r.we
            out["comp_hit"][i] = NAN if not (np.isfinite(a) and np.isfinite(b)) else float(b >= a)
    for k, v in out.items():
        mk[k] = v
    noon = mk["kind"].isin(NOON_KINDS) & mk["official"].notna()
    mism = {c: int(((mk[f"comp_{c}"] != mk["official"]) & mk[f"comp_{c}"].notna() & noon).sum()) for c in CONVS}
    conv = min(CONVS, key=lambda c: (mism[c], CONVS.index(c)))
    mk["tstar"] = np.where(mk["kind"].isin(NOON_KINDS), mk[f"tstar_{conv}"], mk["tstar_hit"])
    mk["comp"] = np.where(mk["kind"].isin(NOON_KINDS), mk[f"comp_{conv}"], mk["comp_hit"])
    mk["tsec"] = np.where(mk["kind"].isin(HIT_KINDS), mk["tsec"], NAN)
    why = np.full(n, "", dtype=object)
    why[mk["official"].isna().to_numpy()] = "unresolved"
    nodata = mk["comp"].isna().to_numpy() & (why == "") & (mk["kind"] != "updown_4h").to_numpy()
    why[nodata] = "no klines / window"
    bad = (mk["comp"] != mk["official"]).to_numpy() & mk["comp"].notna().to_numpy() & (why == "") & \
        (mk["kind"] != "updown_4h").to_numpy()
    why[bad] = "mismatch"
    mk["excluded"] = why
    mk["proxy_disagrees"] = (mk["kind"] == "updown_4h") & mk["comp"].notna() & mk["official"].notna() & \
        (mk["comp"] != mk["official"])
    return mk, conv, mism


# ===================================================================== trades

TRADE_FIELDS = ("timestamp", "side", "asset", "price", "size", "outcomeIndex", "transactionHash", "proxyWallet")


def slim_trades(url, data):
    """Keep only what is used from a data-api trades page (as lists, in API order)."""
    if not isinstance(data, list):
        return data
    return [[r.get(k) for k in TRADE_FIELDS] for r in data]


def fetch_trades(get, cid, start, page=PAGE, max_offset=MAX_OFFSET, end=None):
    """(records, info): every taker trade of market `cid` with start <= timestamp (<= end if given). get(url) returns a
    slim page (lists in TRADE_FIELDS order, newest first). Offset paging up to max_offset; past it,
    the next window ends at the oldest timestamp seen (inclusive) and paging restarts; records are
    merged by key with their largest multiplicity in any one page. info: pages, windows, truncated."""
    counts, recs = Counter(), {}
    info = {"pages": 0, "windows": 0, "truncated": False, "note": ""}
    while True:
        info["windows"] += 1
        oldest, capped, offset = None, False, 0
        while True:
            url = f"{DATA_API}?market={cid}&limit={page}&offset={offset}&start={int(start)}"
            if end is not None:
                url += f"&end={int(end)}"
            rows = get(url)
            if not isinstance(rows, list):
                raise RuntimeError(f"unexpected trades page for {cid}: {str(rows)[:200]}")
            info["pages"] += 1
            c = Counter(tuple(r) for r in rows)
            for k, v in c.items():
                if v > counts[k]:
                    counts[k] = v
                    recs[k] = k
            if rows:
                lo_ts = min(int(r[0]) for r in rows)
                oldest = lo_ts if oldest is None else min(oldest, lo_ts)
            if len(rows) < page:
                break
            if offset + page > max_offset:
                capped = True
                break
            offset += page
        if not capped:
            break
        if end is not None and oldest is not None and oldest >= end:
            # the whole capped window is one second: what is past the cap there is out of reach
            info["truncated"] = True
            info["note"] += f"{max_offset + page}+ trades at ts {end}; "
            end = oldest - 1
            continue
        end = oldest
    out = []
    for k, v in counts.items():
        out += [k] * v
    return out, info


def typed_trades(t):
    return t.astype({"cid": str, "ts": np.int64, "taker_buy": bool, "is_yes": bool, "price": float, "size": float})


def trade_frame(cid, records, yes_token, no_token):
    if not records:
        return typed_trades(pd.DataFrame(columns=["cid", "ts", "taker_buy", "is_yes", "price", "size"]))
    a = np.array(records, dtype=object)
    asset = a[:, 2].astype(str)
    keep = (asset == yes_token) | (asset == no_token)
    return pd.DataFrame({"cid": cid, "ts": a[keep, 0].astype(np.int64),
                         "taker_buy": a[keep, 1] == "BUY", "is_yes": asset[keep] == yes_token,
                         "price": a[keep, 3].astype(float), "size": a[keep, 4].astype(float)})


def fetch_all_trades(http, mk, cache, workers=4, log=print):
    """Trades of every non-excluded market from min(T*, end - 3600) on; cached as trades.parquet with a
    per-market info table (fetch_info.parquet)."""
    tp, ip = Path(cache) / "trades.parquet", Path(cache) / "fetch_info.parquet"
    todo = mk[mk["excluded"] == ""].copy()
    anchor = np.fmin(todo["end"], todo["we"]) if "we" in todo else todo["end"]
    todo["fstart"] = np.minimum(todo["tstar"], anchor - 3600).astype(np.int64)
    have_i = pd.read_parquet(ip) if ip.exists() else pd.DataFrame(columns=["cid", "fstart"])
    done = set(zip(have_i["cid"], have_i["fstart"].astype(np.int64))) if len(have_i) else set()
    todo = todo[[(c, int(s)) not in done for c, s in zip(todo["cid"], todo["fstart"])]]
    get = lambda url: http.get(url, slim=slim_trades)
    log(f"trades: {len(todo):,} markets to fetch ({len(done):,} cached)")
    frames, infos = [], []
    t0, lock, k = time.time(), threading.Lock(), [0]

    def one(r):
        try:
            recs, info = fetch_trades(get, r.cid, r.fstart)
        except Exception as e:  # one market's failure is recorded, not fatal; a rerun asks again
            log(f"  {r.cid}: {type(e).__name__}: {e}")
            return None, None
        with lock:
            k[0] += 1
            if k[0] % 500 == 0:
                log(f"  {k[0]:,}/{len(todo):,} markets, {http.calls:,} requests, {time.time() - t0:.0f} s")
        return trade_frame(r.cid, recs, r.yes_token, r.no_token), {"cid": r.cid, "fstart": int(r.fstart), **info,
                                                                    "n": len(recs)}

    with ThreadPoolExecutor(workers) as ex:
        for f, info in ex.map(one, list(todo.itertuples(index=False))):
            if f is not None:
                frames.append(f)
                infos.append(info)
    if frames:
        old = pd.read_parquet(tp) if tp.exists() else None
        new_cids = {i["cid"] for i in infos}
        parts = ([old[~old["cid"].isin(new_cids)]] if old is not None else []) + [f for f in frames if len(f)]
        allt = pd.concat(parts, ignore_index=True) if parts else frames[0]
        allt.to_parquet(tp)
        info_df = pd.concat([have_i[~have_i["cid"].isin(new_cids)], pd.DataFrame(infos)], ignore_index=True)
        info_df.to_parquet(ip)
    trades = pd.read_parquet(tp) if tp.exists() else pd.DataFrame(columns=["cid", "ts", "taker_buy", "is_yes", "price", "size"])
    info_df = pd.read_parquet(ip) if ip.exists() else pd.DataFrame()
    return trades, info_df


def fetch_pre_tstar(http, mk, cache, log=print):
    """Hit markets settled Yes: the trades between the first 1 s crossing and T* (the touch candle's
    close), which the main fetch (from min(T*, end - 3600)) may not cover. Cached as pre_trades.parquet."""
    path = Path(cache) / "pre_trades.parquet"
    if path.exists():
        return typed_trades(pd.read_parquet(path))
    m = mk[mk["kind"].isin(HIT_KINDS) & (mk["excluded"] == "") & (mk["official"] == 1.0) & mk["tsec"].notna()]
    get = lambda url: http.get(url, slim=slim_trades)
    frames = []
    for r in m.itertuples(index=False):
        try:
            recs, _ = fetch_trades(get, r.cid, int(r.tsec), end=int(r.tstar) - 1)
        except Exception as e:
            log(f"  pre-T* {r.cid}: {type(e).__name__}: {e}")
            continue
        frames.append(trade_frame(r.cid, recs, r.yes_token, r.no_token))
    out = typed_trades(pd.concat(frames, ignore_index=True)) if frames else trade_frame("", [], "", "")
    out.to_parquet(path)
    return out


def second_level(pre, mk, spot, lags=(0, 1, 5)):
    """Hit markets: winning-token trades at p < 1 in [t_sec + L, T*) (the touch already shows in the
    1 s klines, the 1-minute candle has not closed), and how often a 1 s crossing inside the window
    was NOT a touch on Binance's 1-minute candles (markets settled No whose 1 s High / Low crossed)."""
    m = mk[mk["kind"].isin(HIT_KINDS) & (mk["excluded"] == "") & (mk["official"] == 1.0) & mk["tsec"].notna()]
    t = pre.merge(m[["cid", "kind", "tsec", "tstar"]], on="cid")
    t = t[t["is_yes"] & (t["price"] < 1) & (t["ts"] < t["tstar"])]
    rows = []
    for L in lags:
        s = t[t["ts"] >= t["tsec"] + L]
        rows.append({"L": L, "markets": len(m), "mk_tr": s["cid"].nunique(), "trades": len(s),
                     "notional": (s["price"] * s["size"]).sum(), "profit": ((1 - s["price"]) * s["size"]).sum(),
                     "vwap": (s["price"] * s["size"]).sum() / s["size"].sum() if len(s) else NAN,
                     "sell_profit": ((1 - s["price"]) * s["size"])[~s["taker_buy"]].sum()})
    no = mk[mk["kind"].isin(HIT_KINDS) & (mk["excluded"] == "") & (mk["official"] == 0.0)]
    false_cross = sum(np.isfinite(first_second(spot, r.ws, r.we, r.level * 100, r.type == "hit_up"))
                      for r in no.itertuples(index=False) if np.isfinite(r.ws) and np.isfinite(r.we))
    return pd.DataFrame(rows), int(false_cross), len(no)


# ===================================================================== near-certain model

def p_yes(typ, S, K_lo, K_hi, sd, ref=NAN):
    """Driftless model probability of Yes (Up). sd = sigma * sqrt(tau) (log units); S, bounds, ref in
    the same price unit; NaN bound = open end."""
    if not (np.isfinite(S) and np.isfinite(sd)) or sd <= 0:
        return NAN
    above = lambda K: float(norm.cdf(math.log(S / K) / sd))
    if typ == "above":
        return above(K_lo)
    if typ == "range":
        a = 1.0 if not np.isfinite(K_lo) else above(K_lo)
        b = 0.0 if not np.isfinite(K_hi) else above(K_hi)
        return max(0.0, a - b)
    if typ in ("updown_day", "updown_4h"):
        return above(ref) if np.isfinite(ref) else NAN
    if typ == "hit_up":
        return 1.0 if S >= K_lo else min(1.0, 2.0 * float(norm.cdf(math.log(S / K_lo) / sd)))
    if typ == "hit_down":
        return 1.0 if S <= K_lo else min(1.0, 2.0 * float(norm.cdf(math.log(K_lo / S) / sd)))
    raise ValueError(typ)


def checkpoints(mk, spot, checks=CHECKS):
    """One row per (market, minutes before end) still undecided: model p_yes, favoured side, its prob."""
    rows = []
    for r in mk[mk["excluded"] == ""].itertuples(index=False):
        if r.kind in NOON_KINDS:
            end, tdec = r.end, r.tstar
        elif r.kind in HIT_KINDS:
            end, tdec = r.we, r.we
        else:
            end, tdec = r.we, r.we
        for c in checks:
            t = int(end - 60 * c)
            if r.kind in HIT_KINDS:
                if not (np.isfinite(r.ws) and t > r.ws):
                    continue
                if np.isfinite(r.tsec) and r.tsec < t:
                    continue  # already touched: decided
            S, sig = spot.price_before(t), spot.sigma(t)
            tau = tdec - t
            if not (np.isfinite(S) and np.isfinite(sig)) or tau <= 0:
                continue
            sd = sig * math.sqrt(tau)
            if r.kind in HIT_KINDS:
                p = p_yes(r.type, S, r.level * 100, NAN, sd)
            elif r.kind == "above":
                p = p_yes("above", S, r.lo * 100, NAN, sd)
            elif r.kind == "range":
                p = p_yes("range", S, r.lo * 100, r.hi * 100, sd)
            elif r.kind == "updown_day":
                p = p_yes("updown_day", S, NAN, NAN, sd, ref=r.ref * 100)
            else:
                p = p_yes("updown_4h", S, NAN, NAN, sd, ref=spot.price_before(r.ws))
            if not np.isfinite(p):
                continue
            fav_yes = p >= 0.5
            rows.append({"cid": r.cid, "kind": r.kind, "check": c, "t": t, "anchor": int(end), "p_yes": p,
                         "fav_yes": fav_yes, "p_fav": p if fav_yes else 1 - p, "sigma": sig, "tau": tau})
    return pd.DataFrame(rows)


# ===================================================================== measurements

def pbucket(p):
    labels = ["<0.98", "0.98-0.99", "0.99-0.995", "0.995-0.999", "0.999-1"]
    return pd.cut(p, PBUCKETS, right=False, labels=labels)


def risk_flags(mk):
    """Per market, why its result at T* was decided only under a reading the official result settled
    later ("" if none): 'noon_conv' = a noon market whose two candle conventions give different results
    (the convention is chosen ex post, from the official outcomes); 'pre_created' = a hit market kept
    as touched by its written window before it was created (one such market was resolved 'from
    creation' instead and is excluded)."""
    out = pd.Series("", index=mk.index, dtype=object)
    if "comp_open12" in mk and "comp_close12" in mk:
        a, b = mk["comp_open12"], mk["comp_close12"]
        out[mk["kind"].isin(NOON_KINDS) & a.notna() & b.notna() & (a != b)] = "noon_conv"
    if "created" in mk and "comp" in mk:
        pre = mk["kind"].isin(HIT_KINDS) & (mk["comp"] == 1.0) & (mk["tstar"] - 60 < mk["created"])
        out[pre] = "pre_created"
    if "excluded" in mk:
        out[mk["excluded"] != ""] = ""          # excluded markets are not in any measurement
    return out


def post_tstar(trades, mk, first=FIRST, last=LAST):
    """Trades after T* of markets with a winner, with win flag, dt, notional, profit, hours to settle,
    after_close (stamped after closedTime: already settled, not 'unsettled'), in_period (ET day in
    first .. last) and the market's resolution-risk flag."""
    m = mk[(mk["excluded"] == "") & mk["official"].isin([0.0, 1.0])].copy()
    if "risk" not in m:
        m["risk"] = ""
    m = m[["cid", "kind", "type", "tstar", "tsec", "official", "closed_ts", "end", "slug", "risk"]]
    t = trades.merge(m, on="cid", how="inner")
    t = t[t["ts"] >= t["tstar"]].copy()
    t["win"] = t["is_yes"] == (t["official"] == 1.0)
    t["dt"] = t["ts"] - t["tstar"]
    t["notional"] = t["price"] * t["size"]
    t["profit"] = (1 - t["price"]) * t["size"]
    t["fee"] = FEE * t["price"] * (1 - t["price"]) * t["size"]
    t["hours"] = (t["closed_ts"] - t["ts"]) / 3600
    t["after_close"] = np.isfinite(t["closed_ts"].to_numpy(float)) & (t["ts"] > t["closed_ts"])
    t["day"] = et_day(t["ts"])
    t["month"] = t["day"].str[:7]
    t["in_period"] = (t["day"] >= first.isoformat()) & (t["day"] <= last.isoformat())
    return t


def et_day(ts):
    """ISO ET dates of unix seconds (vectorised)."""
    d = pd.to_datetime(pd.Series(np.asarray(ts, np.int64)), unit="s", utc=True).dt.tz_convert("America/New_York")
    return pd.Series(d.dt.strftime("%Y-%m-%d").to_numpy(), index=getattr(ts, "index", None))


def lag_table(w, mk, lags=LAGS, by="kind"):
    """Winning-token trades at p < 1 after T* + L: per kind x L, and 'all' = the Binance-settled kinds
    (the Chainlink-settled control is its own row only)."""
    rows = []
    tot = mk[(mk["excluded"] == "") & mk["official"].isin([0.0, 1.0])].groupby("kind").size()
    for k in list(KINDS) + ["all"]:
        s0 = w[w[by].isin(BINANCE_KINDS)] if k == "all" else w[w[by] == k]
        nmk = int(tot.reindex(BINANCE_KINDS).fillna(0).sum()) if k == "all" else int(tot.get(k, 0))
        for L in lags:
            s = s0[s0["dt"] >= L]
            ts = s[~s["taker_buy"]]
            rows.append({"kind": k, "L": L, "mk": nmk, "mk_tr": s["cid"].nunique(), "trades": len(s),
                         "shares": s["size"].sum(), "notional": s["notional"].sum(), "profit": s["profit"].sum(),
                         "taker_profit": (s["profit"] - s["fee"]).sum(),
                         "vwap": s["notional"].sum() / s["size"].sum() if len(s) else NAN,
                         "n995": s.loc[s["price"] <= 0.995, "notional"].sum(),
                         "sell_notional": ts["notional"].sum(), "sell_profit": ts["profit"].sum(),
                         "sell_n995": ts.loc[ts["price"] <= 0.995, "notional"].sum(),
                         "med_h": float(np.median(s["hours"])) if len(s) else NAN})
    return pd.DataFrame(rows)


def clustered_mean(x, w, g):
    """Share-weighted mean of x with a cluster-robust standard error (clusters g) and t."""
    x, w, g = np.asarray(x, float), np.asarray(w, float), np.asarray(g)
    W = w.sum()
    if W <= 0:
        return NAN, NAN, NAN, 0
    mu = (w * x).sum() / W
    s = pd.Series(w * (x - mu)).groupby(g).sum().to_numpy()
    G = len(s)
    if G < 2:
        return mu, NAN, NAN, G
    se = math.sqrt((s * s).sum() * G / (G - 1)) / W
    return mu, se, mu / se if se > 0 else NAN, G


def near_table(cp, trades, mk):
    """Near-certain buckets: trades of the favoured token with ts in [t, t + 60) after each checkpoint.
    Returns (table by check x bucket with a pooled 'all' row, table by kind x bucket pooled, the
    joined trades)."""
    if not len(cp):
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    cp = cp.copy()
    cp["bucket"] = np.where(cp["p_fav"] >= 0.99, ">=0.99", np.where(cp["p_fav"] >= 0.95, "0.95-0.99", ""))
    cp = cp[cp["bucket"] != ""]
    off = mk.set_index("cid")["official"].reindex(cp["cid"]).to_numpy()
    cp["won"] = np.where(cp["fav_yes"], off, 1 - off)
    anchor = cp.groupby("cid")["anchor"].first()
    tr = trades[trades["cid"].isin(anchor.index)].copy()
    a = anchor.reindex(tr["cid"]).to_numpy()
    # windows [anchor - 60 c, anchor - 60 c + 60) do not overlap: c = ceil((anchor - ts) / 60)
    tr["check"] = np.ceil((a - tr["ts"].to_numpy()) / 60.0)
    tr = tr[tr["check"].isin(CHECKS)]
    tr["check"] = tr["check"].astype(int)
    j = cp.merge(tr, on=["cid", "check"], how="inner")
    j = j[(j["ts"] >= j["t"]) & (j["ts"] < j["t"] + 60) & (j["is_yes"] == j["fav_yes"])].copy()
    j["pnl"] = j["won"] - j["price"]
    j["pnl_taker"] = j["pnl"] - FEE * j["price"] * (1 - j["price"])

    def row(name, c, s):
        mu, se, t, G = clustered_mean(s["pnl"], s["size"], s["cid"])
        mu_t, _, t_t, _ = clustered_mean(s["pnl_taker"], s["size"], s["cid"])
        ts = s[~s["taker_buy"]]
        mu_s, _, t_s, G_s = clustered_mean(ts["pnl"], ts["size"], ts["cid"])
        lost = lambda x: int((x.groupby("cid")["won"].min() < 1).sum()) if len(x) else 0
        return {"group": name, "points": len(c), "markets": c["cid"].nunique(), "model": c["p_fav"].mean(),
                "win_all": c["won"].mean(), "pts_tr": s[["cid", "check"]].drop_duplicates().shape[0], "mk_tr": G,
                "trades": len(s), "shares": s["size"].sum(),
                "win_tr": (s["won"] * s["size"]).sum() / s["size"].sum() if len(s) else NAN,
                "vwap": (s["price"] * s["size"]).sum() / s["size"].sum() if len(s) else NAN,
                "pnl": mu, "se": se, "t": t, "pnl_taker": mu_t, "t_taker": t_t, "pnl_sell": mu_s, "t_sell": t_s,
                "mk_sell": G_s, "losses_sell": lost(ts),
                "losses": lost(s), "loss_pts": int((c["won"] < 1).sum()),
                "exp_fail": float((1 - c["p_fav"]).sum())}

    by_check, by_kind = [], []
    for b in (">=0.99", "0.95-0.99"):
        cb, jb = cp[cp["bucket"] == b], j[j["bucket"] == b]
        for k in KINDS:
            c, s = cb[cb["kind"] == k], jb[jb["kind"] == k]
            if len(c):
                by_kind.append({"bucket": b, "kind": k, **row(k, c, s)})
        # the pooled rows (and the verdict) are the Binance-settled kinds; the Chainlink control is
        # its own by-kind row
        cb, jb = cb[cb["kind"].isin(BINANCE_KINDS)], jb[jb["kind"].isin(BINANCE_KINDS)]
        for name in ["all"] + list(CHECKS):
            c = cb if name == "all" else cb[cb["check"] == name]
            s = jb if name == "all" else jb[jb["check"] == name]
            by_check.append({"bucket": b, "check": str(name), **row(str(name), c, s)})
    return pd.DataFrame(by_check), pd.DataFrame(by_kind), j


def t_cell(t, G, losses):
    """A clustered t for the report: '–' with fewer than MIN_G clusters (the cluster-robust SE is
    meaningless then); '†' when no cluster lost (the SE reflects price dispersion only, nothing about
    the tail)."""
    if not (np.isfinite(t) and np.isfinite(G)) or G < MIN_G:
        return "–"
    return f"{t:.2f}" + ("†" if losses == 0 else "")


def calibration(cp, mk, kinds=BINANCE_KINDS, bucket=0.99):
    """Model >= bucket checkpoints of these kinds: points, expected failures sum(1 - p_fav), observed
    failures as checkpoints, distinct markets, events and ET days (the same market repeats across
    checkpoints and strikes of one day share one BTC path, so the checkpoints are not independent)."""
    c = cp[(cp["p_fav"] >= bucket) & cp["kind"].isin(kinds)]
    if not len(c):
        return {"points": 0, "expected": NAN, "fail_pts": 0, "fail_mk": 0, "fail_ev": 0, "fail_days": 0, "days": 0}
    m = mk.set_index("cid")
    off = m["official"].reindex(c["cid"]).to_numpy()
    won = np.where(c["fav_yes"], off, 1 - off)
    lost = c[won < 1]
    ev = m["event_slug"].reindex(lost["cid"]) if "event_slug" in m else pd.Series(dtype=object)
    day = lambda x: et_day(pd.Series(x["anchor"].to_numpy(np.int64) - 1, index=x.index))
    return {"points": len(c), "expected": float((1 - c["p_fav"]).sum()), "fail_pts": len(lost),
            "fail_mk": lost["cid"].nunique(), "fail_ev": ev.nunique(), "fail_days": day(lost).nunique() if len(lost) else 0,
            "days": day(c).nunique()}


# ===================================================================== spot build / check

def spot_files(klines=KLINES, cache=CACHE, http=None, first=SPOT_FIRST, last=SPOT_LAST, log=print):
    files, missing = [], []
    for d in days(first, last):
        f = Path(klines) / f"BTCUSDT-1s-{d}.zip"
        if not f.exists():
            f = Path(cache) / "klines1s" / f"BTCUSDT-1s-{d}.zip"
            if not f.exists() and http is not None:
                try:
                    http.download(VISION.format(iv="1s", d=d), f)
                except Exception as e:
                    log(f"  1 s klines {d}: {type(e).__name__}: {e}")
        (files if f.exists() else missing).append(f if f.exists() else d.isoformat())
    return files, missing


def build_spot(klines=KLINES, cache=CACHE, http=None, log=print):
    path = Path(cache) / "spot_1s.npz"
    if path.exists():
        z = np.load(path)
        f = lambda a: np.where(a > 0, a.astype(float), np.nan)
        return Spot(int(z["sec0"]), f(z["hi"]), f(z["lo"]), f(z["cl"])), list(z["missing"])
    files, missing = spot_files(klines, cache, http, log=log)
    sec0 = int(datetime(SPOT_FIRST.year, SPOT_FIRST.month, SPOT_FIRST.day, tzinfo=timezone.utc).timestamp())
    n = (len(days(SPOT_FIRST, SPOT_LAST))) * 86400
    hi, lo, cl = (np.zeros(n, np.int32) for _ in range(3))
    for f in files:
        t, h, l, c = read_kline_zip(f)
        k = t - sec0
        ok = (k >= 0) & (k < n)
        hi[k[ok]], lo[k[ok]], cl[k[ok]] = h[ok], l[ok], c[ok]
    np.savez(path, sec0=sec0, hi=hi, lo=lo, cl=cl, missing=np.array(missing, dtype=str))
    f = lambda a: np.where(a > 0, a.astype(float), np.nan)
    log(f"spot: {len(files)} days of 1 s klines, missing {missing}")
    return Spot(sec0, f(hi), f(lo), f(cl)), missing


def attach_1m(spot, http, cache=CACHE, first=SPOT_FIRST, last=SPOT_LAST, log=print):
    """Download Binance's 1m klines for every day, compare them with the 1 s aggregates minute by
    minute and use them for High / Low / Close. Returns the per-day comparison."""
    ts, hs, ls, cs, rows = [], [], [], [], []
    for d in days(first, last):
        f = Path(cache) / "klines1m" / f"BTCUSDT-1m-{d}.zip"
        try:
            http.download(VISION.format(iv="1m", d=d), f)
        except Exception as e:
            rows.append({"day": d.isoformat(), "minutes": 0, "bad_h": -1, "bad_l": -1, "bad_c": -1, "note": repr(e)})
            continue
        t, h, l, c = read_kline_zip(f)
        i = t // 60 - spot.m0
        ok = (i >= 0) & (i < len(spot.Cs))
        i = i[ok]
        rows.append({"day": d.isoformat(), "minutes": int(ok.sum()),
                     "bad_h": int((spot.Hs[i] != h[ok]).sum()), "bad_l": int((spot.Ls[i] != l[ok]).sum()),
                     "bad_c": int((spot.Cs[i] != c[ok]).sum()),
                     "h_wider": int((spot.Hs[i] > h[ok]).sum()), "l_wider": int((spot.Ls[i] < l[ok]).sum()),
                     "max_diff_cents": float(max(np.abs(spot.Hs[i] - h[ok]).max(), np.abs(spot.Ls[i] - l[ok]).max())),
                     "note": ""})
        ts.append(t[ok]); hs.append(h[ok]); ls.append(l[ok]); cs.append(c[ok])
    spot.set_minutes(np.concatenate(ts), np.concatenate(hs), np.concatenate(ls), np.concatenate(cs))
    return pd.DataFrame(rows)


def check_minutes(spot, http, cache=CACHE, n=20, seed=20261004, log=print):
    """Our 1-minute H/L/C against data.binance.vision 1m klines on n random days."""
    rng = random.Random(seed)
    ds = sorted(rng.sample(days(FIRST, LAST), n))
    rows = []
    for d in ds:
        f = Path(cache) / "klines1m" / f"BTCUSDT-1m-{d}.zip"
        try:
            http.download(VISION.format(iv="1m", d=d), f)
        except Exception as e:
            rows.append({"day": d.isoformat(), "minutes": 0, "bad_h": -1, "bad_l": -1, "bad_c": -1, "note": repr(e)})
            continue
        t, h, l, c = read_kline_zip(f)
        i = t // 60 - spot.m0
        ok = (i >= 0) & (i < len(spot.C))
        i = i[ok]
        rows.append({"day": d.isoformat(), "minutes": int(ok.sum()),
                     "bad_h": int((spot.Hs[i] != h[ok]).sum()), "bad_l": int((spot.Ls[i] != l[ok]).sum()),
                     "bad_c": int((spot.Cs[i] != c[ok]).sum()), "note": ""})
    return pd.DataFrame(rows)


# ===================================================================== report

def _f(x, nd=0):
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "–"
    return f"{x:,.{nd}f}"


def md_table(df, cols, heads=None):
    heads = heads or cols
    out = ["| " + " | ".join(heads) + " |", "|" + "---|" * len(cols)]
    for r in df.itertuples(index=False):
        d = r._asdict()
        out.append("| " + " | ".join(str(d[c]) for c in cols) + " |")
    return "\n".join(out)


def daily_dollars(w, L=5, cap=0.995, first=FIRST, last=LAST, kinds=BINANCE_KINDS):
    """Per ET day of first .. last (every day, empty ones as 0): notional and profit of the winning-token
    trades at dt >= L, p <= cap, of these kinds, not after closedTime; and the taker-sell / taker-buy
    parts. Trades on ET days outside the period are left out here (they are reported apart)."""
    alld = [d.isoformat() for d in days(first, last)]
    s = w[(w["dt"] >= L) & (w["price"] <= cap) & w["kind"].isin(kinds) & w["day"].isin(set(alld))]
    if "after_close" in s:
        s = s[~s["after_close"]]
    agg = lambda x, p: x.groupby("day").agg(**{f"{p}notional": ("notional", "sum"), f"{p}profit": ("profit", "sum")}) \
        .reindex(alld, fill_value=0.0)
    return agg(s, "").join(agg(s[~s["taker_buy"]], "sell_")).join(agg(s[s["taker_buy"]], "buy_"))


def day_stats(dd, col, bar=50.0):
    """mean, median and days >= bar of one daily_dollars column."""
    x = dd[col]
    return {"mean": float(x.mean()) if len(x) else NAN, "median": float(x.median()) if len(x) else NAN,
            "ge": int((x >= bar).sum()), "n": len(x)}


def lag_md(lt, a, b, title):
    """kind x L table, cells 'a / b' (dollars)."""
    rows = []
    for k in list(BINANCE_KINDS) + ["all", CONTROL]:
        s = lt[lt["kind"] == k]
        if not len(s):
            continue
        r = {"kind": KIND_LABEL.get(k, k), "mk": int(s["mk"].iloc[0])}
        for L in LAGS:
            x = s[s["L"] == L]
            r[f"L{L}"] = f"{_f(x[a].iloc[0])} / {_f(x[b].iloc[0])}" if len(x) else "–"
        rows.append(r)
    df = pd.DataFrame(rows)
    return f"{title}\n\n" + md_table(df, ["kind", "mk"] + [f"L{L}" for L in LAGS],
                                       ["类型", "市场"] + [f"T*+{L}s" for L in LAGS])


def report(mk, cov, missing, conv, mism, near_kind, minutes_chk, trades, info, w, lose, cp, near, notes, runtime,
           out_md, out_csv, spot_missing, mall=None, sl=None, after=None):
    """w: winning-token trades at p < 1 after T* and not after closedTime; after: the same stamped after
    closedTime (already settled; counted apart). mk needs the 'risk' column (risk_flags)."""
    L = []
    nmk = len(mk)
    if "risk" not in mk:
        mk = mk.assign(risk=risk_flags(mk))
    excl = mk["excluded"].value_counts()
    L.append("# BTC 市场“结果已定、还没结算”的成交（RESOLVED.md 的测量）\n")
    tj = Path(CACHE) / "timing.json"
    tinfo = json.loads(tj.read_text()) if tj.exists() else {}
    L.append(f"数据：Gamma 已结束事件（{FIRST}–{LAST}，日期按 ET）、data-api 逐笔成交（taker 记录，每笔撮合一次）、"
             f"币安 BTCUSDT 1 秒 K 线和官方 1 分钟 K 线。本次运行 {runtime / 60:.1f} 分钟"
             + (f"；首次完整运行（含抓取成交）{tinfo['fetch_s'] / 60:.0f} 分钟、{tinfo['requests']:,} 次请求（≤ 4 次/秒）" if tinfo else "")
             + "。\n")
    L.append("## 覆盖\n")
    c2 = cov.copy()
    c2["missing_days"] = [len(missing.get(k, [])) if k in missing else "–" for k in c2["kind"]]
    ex = mk.groupby("kind")["excluded"].agg(lambda s: int((s == "").sum()))
    c2["used"] = [int(ex.get(k, 0)) for k in c2["kind"]]
    L.append(md_table(c2, ["kind", "events", "markets", "resolved", "days", "missing_days", "used"],
                      ["类型", "事件", "市场", "已结算", "有事件的天数", "缺的天", "纳入"]))
    for k, v in missing.items():
        if v:
            L.append(f"\n- {k} 缺的日期（{len(v)}）：{', '.join(v[:40])}{' …' if len(v) > 40 else ''}")
    ex_d = {k: int(v) for k, v in excl.drop("", errors="ignore").items()}
    L.append(f"\n- 排除：{ex_d}；共 {nmk:,} 个市场。")
    L.append(f"- 搜索：public-search 共 {notes.get('search_pages')} 页、{notes.get('search_hit_slugs')} 个 hit 事件 slug；"
             f"周、月按 slug 规律补查。")
    rew = mk.groupby("kind").agg(min_size=("rewards_min_size", "median"), max_spread=("rewards_max_spread", "median"),
                                 with_clob=("clob_rewards", lambda s: int((s != "").sum())),
                                 daily=("rewards_daily", "sum"), dmax=("rewards_daily", "max"), fee=("fee_rate", "median"))
    L.append("- 奖励字段：rewardsMinSize / rewardsMaxSpread 中位数、clobRewards 非空的市场数（rewardsDailyRate 合计 / 最大）、费率："
             + "；".join(f"{k} {_f(r.min_size)} / {_f(r.max_spread, 1)}、{int(r.with_clob)} 个（{r.daily:,.3f} / {r.dmax:,.3f}）、"
                        f"{_f(r.fee, 3)}" for k, r in rew.iterrows()) + "。")
    if spot_missing:
        L.append(f"- 缺 1 秒 K 线的天：{spot_missing}")
    L.append(f"- 1 分钟 K 线核对（{len(minutes_chk)} 个随机日，data.binance.vision 1m）：比较 {int(minutes_chk['minutes'].sum()):,} 分钟，"
             f"最高价不一致 {int(minutes_chk['bad_h'].clip(lower=0).sum())}、最低价 {int(minutes_chk['bad_l'].clip(lower=0).sum())}、"
             f"收盘价 {int(minutes_chk['bad_c'].clip(lower=0).sum())}。")
    if mall is not None and len(mall):
        L.append(f"- 全部 {len(mall)} 天逐分钟核对：{int(mall['minutes'].sum()):,} 分钟里 1 秒聚合的最高价比官方 1m 高的 "
                 f"{int(mall['h_wider'].sum())} 分钟、最低价更低的 {int(mall['l_wider'].sum())} 分钟（从不反向，收盘价全部一致；"
                 f"最大差 {mall['max_diff_cents'].max() / 100:.2f} 美元）。所以 T* 和结果用 data.binance.vision 的 1m K 线（结算源），"
                 f"1 秒 K 线只用于秒级时刻和波动率。")
    L.append("\n## 规则核对\n")
    L.append(f"- 中午 K 线：两种约定与官方结果不符的市场数 open12（12:00 开盘、12:01 收盘）= {mism['open12']}，"
             f"close12（11:59 开盘、12:00 收盘）= {mism['close12']}；采用 **{conv}**，T* = 该 K 线收盘时刻。")
    bad = mk[mk["excluded"] == "mismatch"]
    L.append(f"- 规则算出的结果与官方不符（已排除）：{len(bad)} 个。")
    if len(bad):
        b = bad.copy()
        b["lvl"] = [f"{r.level:,.0f}" if np.isfinite(r.level) else f"{_f(r.lo)}–{_f(r.hi)}" for r in b.itertuples()]
        b["comp_s"], b["off_s"] = b["comp"].map(lambda x: _f(x, 1)), b["official"].map(lambda x: _f(x, 1))
        utc = lambda x: pd.Timestamp(x, unit="s").strftime("%m-%d %H:%M") if np.isfinite(x) else "–"
        b["t_s"], b["c_s"], b["w_s"] = b["tstar"].map(utc), b["created"].map(utc), b["ws"].map(utc)
        L.append(md_table(b.head(60), ["kind", "slug", "lvl", "w_s", "t_s", "c_s", "comp_s", "off_s"],
                          ["类型", "市场", "档位", "窗口开始(UTC)", "T*(UTC)", "createdAt(UTC)", "算出", "官方"]))
    h4 = mk[mk["kind"] == "updown_4h"]
    L.append(f"- 4 小时（Chainlink 结算，只作对照）：币安代理（结束前最后价 vs 开始前最后价）与官方不同的 {int(h4['proxy_disagrees'].sum())}/"
             f"{int(h4['comp'].notna().sum())}，不据此排除。")
    hits = mk[mk["kind"].isin(HIT_KINDS) & (mk["excluded"] == "") & (mk["official"] == 1.0)]
    lag = (hits["tstar"] - hits["tsec"]).dropna()
    if len(lag):
        L.append(f"- 触及类：秒级首次越过到所在分钟 K 线收盘的间隔 中位数 {lag.median():.0f} 秒（p10 {lag.quantile(.1):.0f}，p90 {lag.quantile(.9):.0f}）。"
                 f"触及市场的结算（closedTime）在 T* 之后 中位数 {((hits['closed_ts'] - hits['tstar']) / 60).median():.1f} 分钟。")
    noon = mk[mk["kind"].isin(NOON_KINDS) & (mk["excluded"] == "")]
    if len(noon):
        L.append(f"- 中午类：closedTime 在 T* 之后 中位数 {((noon['closed_ts'] - noon['tstar']) / 60).median():.1f} 分钟"
                 f"（p90 {((noon['closed_ts'] - noon['tstar']) / 60).quantile(.9):.1f}）。")
    if len(info):
        tr = info[info["truncated"]]
        L.append(f"- 成交抓取：{len(info):,} 个市场、{int(info['pages'].sum()):,} 页，超过 offset 上限改用时间窗的 "
                 f"{int((info['windows'] > 1).sum())} 个，仍被截断的 {len(tr)} 个{('：' + ', '.join(tr['cid'].str[:10])) if len(tr) else ''}。")
    hits_all = mk[mk["kind"].isin(HIT_KINDS) & (mk["comp"] == 1.0)]
    pre_c = hits_all[hits_all["tstar"] - 60 < hits_all["created"]]
    pc_profit = w.loc[w["cid"].isin(set(pre_c["cid"])) & (w["dt"] >= 5), "profit"].sum() if len(w) else 0.0
    L.append(f"- 市场创建之前就已触及的触及市场：{len(pre_c)} 个，其中官方按“窗口起点”判 Yes 的 "
             f"{int((pre_c['official'] == 1.0).sum())} 个、按“创建之后”判 No 的 {int((pre_c['official'] == 0.0).sum())} 个"
             f"（即上面排除的不符）。保留的这些市场 T*+5 秒后赢方收益 ${pc_profit:,.0f}，结算口径不一致是它们的额外风险。")
    L.append(f"- 结算口径风险：T* 时的“已定”依赖一个之后才由官方结果确定的读法的市场共 {int((mk['risk'] != '').sum()) if 'risk' in mk else 0} 个——"
             f"两种中午 K 线约定结果不同的中午市场 {int((mk.get('risk', pd.Series(dtype=object)) == 'noon_conv').sum())} 个（约定是事后按官方结果选的），"
             f"和上面创建前已触及、按书面窗口保留的触及市场 {int((mk.get('risk', pd.Series(dtype=object)) == 'pre_created').sum())} 个"
             "（同类的另一个市场官方按“创建之后”判了反方向）。它们不是无风险的，下面单独列出“去掉它们”的数字。")
    L.append("- 范围（按 RESOLVED.md 预先登记）：不含按 1 小时 K 线结算的币安 BTC 市场——每小时涨跌（系列 btc-up-or-down-hourly）和"
             "每小时“高于”阶梯（系列 bitcoin-multi-strikes-hourly）。它们同样在 K 线收盘时就已确定，所以这里“结果已定、未结算”的金额只是 BTC 市场的下限。")
    wb = w[w["kind"].isin(BINANCE_KINDS)]
    L.append("\n## 结果已定、未结算：赢的一边在 T*+L 之后、结算之前以 p<1 成交\n")
    na = after if after is not None and len(after) else pd.DataFrame(columns=["dt", "profit", "kind", "notional"])
    na5 = na[na["dt"] >= 5]
    L.append("任何主动方都算：成交只证明“当时有人以这个价成交过”，不证明我们能拿到同样的量。“卖方主动”= taker 卖出赢方代币，接到它的是"
             "**已经挂在那里的**买单；我们要接到，得在同一价位排在他们前面，或者挂更高的价（收益更少），所以这一列是上限，不是我们挂单能拿到的量。"
             "美元：名义 = p×份数，收益 = (1−p)×份数（挂单无费）；吃单收益再扣 0.07·p(1−p)。data-api 的时间戳是链上时间，比撮合晚"
             "（README §16 量过：中位 2.2 秒、99% 3.6 秒），所以 L = 0、1 秒里有 T* 之前撮合的成交，L ≥ 5 秒才干净。"
             "成交价是 taker 这一笔的平均成交价（和 takerOnly=false 的挂单方记录核对过）。"
             f"“全部币安结算”= 前六类；{CONTROL} 按 Chainlink 结算，T* 时结果并没有确定（币安代理与官方不同的见上），只作对照，不计入判定。"
             f"时间戳晚于 closedTime（已结算）的成交不算“未结算”，不在下面任何表里：赢方 p<1 共 {len(na):,} 笔，L ≥ 5 秒的 {len(na5):,} 笔、"
             f"名义 ${na5['notional'].sum():,.0f}、收益 ${na5['profit'].sum():,.0f}（其中 {CONTROL} {int((na5['kind'] == CONTROL).sum())} 笔）。\n")
    lt = lag_table(w, mk)
    L.append(lag_md(lt, "notional", "profit", "任何主动方：名义$ / 收益$"))
    L.append("")
    L.append(lag_md(lt, "sell_notional", "sell_profit",
                    "其中卖方主动（taker 卖出赢方代币，已有的挂单买方接到的；我们要接到须排在队首或挂更高的价——上限）：名义$ / 收益$"))
    order = {k: i for i, k in enumerate(list(BINANCE_KINDS) + ["all", CONTROL])}
    d5 = lt[lt["L"] == 5].copy().sort_values("kind", key=lambda s: s.map(order))
    d5["mkx"] = [f"{a}/{b}" for a, b in zip(d5["mk_tr"], d5["mk"])]
    d5["kind"] = d5["kind"].map(lambda k: KIND_LABEL.get(k, k))
    for c in ("shares", "taker_profit", "n995", "sell_n995"):
        d5[c] = d5[c].map(lambda x: _f(x))
    d5["vwap"] = d5["vwap"].map(lambda x: _f(x, 4))
    d5["med_h"] = d5["med_h"].map(lambda x: _f(x, 2))
    L.append("\nL = 5 秒的细节：\n")
    L.append(md_table(d5, ["kind", "mkx", "trades", "shares", "vwap", "taker_profit", "n995", "sell_n995", "med_h"],
                      ["类型", "有成交市场/市场", "笔数", "份数", "均价", "吃单收益$", "名义$(p≤0.995)",
                       "卖方主动名义$(p≤0.995)", "到结算小时(中位)"]))
    s5m = wb[wb["dt"] >= 5]
    tot = s5m["profit"].sum()
    if len(s5m):
        tm = s5m.groupby(["slug", "kind"]).agg(trades=("size", "size"), notional=("notional", "sum"),
                                               profit=("profit", "sum"), pmin=("price", "min"),
                                               settle=("hours", "max")).sort_values("profit", ascending=False)
        top = tm.head(10).reset_index()
        mi = mk.set_index("slug")

        def note(sl):
            r = mi.loc[sl]
            risk = {"noon_conv": "，两种约定结果不同", "pre_created": "创建前已触及"}.get(r.get("risk", ""), "")
            if r["kind"] in NOON_KINDS:
                k = f"{r['lo']:,.0f}" if r["kind"] == "above" else ""
                return f"{k} 12:00 收盘 {r['close_open12']:,.2f}（11:59 收盘 {r['close_close12']:,.2f}）{risk}".strip()
            return risk
        top["note"] = [note(x) for x in top["slug"]]
        L.append(f"\n收益最集中的 10 个市场（币安结算，L = 5 秒，占这些类型全部收益 {top['profit'].sum() / tot:.0%}；“结算”= 第一笔到 closedTime 的小时数）：\n")
        for c in ("notional", "profit"):
            top[c] = top[c].map(lambda x: _f(x))
        top["pmin"] = top["pmin"].map(lambda x: _f(x, 3))
        top["settle"] = top["settle"].map(lambda x: _f(x, 1))
        L.append(md_table(top, ["slug", "kind", "trades", "notional", "profit", "pmin", "settle", "note"],
                          ["市场", "类型", "笔数", "名义$", "收益$", "最低价", "结算(小时)", "说明"]))
        rest = s5m[~s5m["slug"].isin(set(tm.index.get_level_values(0)[:10]))]
        r999 = rest[rest["price"] >= 0.999]
        L.append(f"\n去掉最集中的 10 个市场后，L = 5 秒收益 ${rest['profit'].sum():,.0f}，其中价格 ≥ 0.999（每份 0.1¢，只靠排队）的成交占 "
                 f"{r999['profit'].sum() / max(rest['profit'].sum(), 1e-9):.0%}（${r999['profit'].sum():,.0f}）；全部 L = 5 秒成交里 ≥ 0.999 的占份数 "
                 f"{s5m.loc[s5m['price'] >= 0.999, 'size'].sum() / s5m['size'].sum():.0%}。")
    if sl is not None:
        st, false_cross, n_no = sl
        st2 = st.copy()
        for c in ("notional", "profit", "sell_profit"):
            st2[c] = st2[c].map(lambda x: _f(x, 1))
        st2["vwap"] = st2["vwap"].map(lambda x: _f(x, 4))
        L.append("\n触及类“秒级上限”：1 秒 K 线已越过、所在 1 分钟 K 线还没收盘（[t_sec + L, T*)）时赢方 p<1 的成交：\n")
        L.append(md_table(st2, ["L", "markets", "mk_tr", "trades", "notional", "vwap", "profit", "sell_profit"],
                          ["L 秒", "触及市场", "有成交", "笔数", "名义$", "均价", "收益$", "卖方主动收益$"]))
        L.append(f"\n官方 No 的触及市场里，1 秒 K 线在窗口内越过档位的有 {false_cross}/{n_no} 个"
                 + ("：只看秒级就动手会在这些市场买错。" if false_cross else
                    (f"（1 秒聚合在 {(mall['h_wider'].sum() + mall['l_wider'].sum()) / max(1, mall['minutes'].sum()):.2%} 的分钟"
                     f"端点上比 1m 宽，最多 {mall['max_diff_cents'].max() / 100:.2f} 美元，理论上可能买错，这段样本里没发生）。"
                     if mall is not None and len(mall) else "。"))
                 + "这一段在 T* 之前，不计入判定。")
    s5 = s5m
    if len(s5):
        pb = s5.assign(b=pbucket(s5["price"])).groupby("b", observed=False).agg(
            trades=("size", "size"), shares=("size", "sum"), notional=("notional", "sum"), profit=("profit", "sum"),
            sell_profit=("profit", lambda x: x[~s5.loc[x.index, "taker_buy"]].sum())).reset_index()
        for c in ("shares", "notional", "profit", "sell_profit"):
            pb[c] = pb[c].map(lambda x: _f(x))
        L.append("\n价格分布（币安结算，L = 5 秒）：\n")
        L.append(md_table(pb, ["b", "trades", "shares", "notional", "profit", "sell_profit"],
                          ["价格", "笔数", "份数", "名义$", "收益$", "其中卖方主动收益$"]))
        bm = w[w["dt"] >= 5].groupby(["month", "kind"]).agg(profit=("profit", "sum")).reset_index()
        piv = bm.pivot(index="month", columns="kind", values="profit").fillna(0.0)
        piv["all"] = piv[[k for k in BINANCE_KINDS if k in piv.columns]].sum(axis=1)
        piv = piv.reset_index()
        cols = ["month"] + [k for k in BINANCE_KINDS if k in piv.columns] + ["all"] + ([CONTROL] if CONTROL in piv.columns else [])
        for c in cols[1:]:
            piv[c] = piv[c].map(lambda x: _f(x))
        L.append("\n按月、按类型的收益$（L = 5 秒，任何主动方；按成交的 ET 日期）：\n")
        L.append(md_table(piv, cols, ["month"] + [KIND_LABEL.get(c, c) for c in cols[1:]]))
        ann = (1 / s5["price"] - 1) * 8760 / s5["hours"].clip(lower=1 / 60)
        L.append(f"\n资金占用（币安结算，L = 5 秒）：成交到结算的小时数 中位数 {s5['hours'].median():.2f}、p90 {s5['hours'].quantile(.9):.2f}；"
                 f"逐笔单利年化（仅描述，不能滚动复制）中位数 {100 * np.median(ann):,.0f}%。")
    # per day (the verdict's frame): Binance-settled kinds, ET days FIRST .. LAST, before closedTime
    dd = daily_dollars(w)
    sN, sP = day_stats(dd, "notional"), day_stats(dd, "profit")
    sSN, sSP = day_stats(dd, "sell_notional"), day_stats(dd, "sell_profit")
    late = dd[dd.index >= "2026-08-01"]
    lP, lSP = day_stats(late, "profit"), day_stats(late, "sell_profit")
    buy_share = dd["buy_profit"].sum() / max(dd["profit"].sum(), 1e-9)
    s5all = wb[(wb["dt"] >= 5) & wb["in_period"]]
    sell = s5all[~s5all["taker_buy"]]
    sell999 = sell.loc[sell["price"] >= 0.999, "profit"].sum() / max(sell["profit"].sum(), 1e-9)
    dc = daily_dollars(w, kinds=(CONTROL,))
    cN, cP = day_stats(dc, "notional"), day_stats(dc, "profit")
    outp = wb[(wb["dt"] >= 5) & (wb["price"] <= 0.995) & ~wb["in_period"]]
    L.append(f"\n每天（币安结算，{FIRST}–{LAST} 的 {sN['n']} 个 ET 日，没有成交的天算 0；L = 5 秒、p ≤ 0.995）：\n")
    L.append("| 口径 | 名义$/天 平均 | 中位数 | ≥ $50 的天 | 收益$/天 平均 | 中位数 | 08–09 月收益$/天 平均 / 中位数 |\n|---|---|---|---|---|---|---|")
    L.append(f"| 任何主动方 | {sN['mean']:,.1f} | {sN['median']:,.1f} | {sN['ge']}/{sN['n']} | {sP['mean']:,.2f} | {sP['median']:,.2f} | "
             f"{lP['mean']:,.2f} / {lP['median']:,.2f} |")
    L.append(f"| 卖方主动（上限） | {sSN['mean']:,.1f} | {sSN['median']:,.1f} | {sSN['ge']}/{sSN['n']} | {sSP['mean']:,.2f} | {sSP['median']:,.2f} | "
             f"{lSP['mean']:,.2f} / {lSP['median']:,.2f} |")
    L.append(f"| {CONTROL}（对照，不计入） | {cN['mean']:,.1f} | {cN['median']:,.1f} | {cN['ge']}/{cN['n']} | {cP['mean']:,.2f} | {cP['median']:,.2f} | – |")
    L.append(f"\n- p ≤ 0.995 的收益里 {buy_share:.0%} 来自 taker 买入（有人吃掉还挂着的过时卖单：要抢速度，不是挂单能接到的）；"
             f"卖方主动收益（全部价格 < 1）里 {sell999:.0%} 在 0.999（每份 0.1¢，只取决于排队位置）。")
    if len(outp):
        L.append(f"- 期间外 ET 日（{', '.join(sorted(outp['day'].unique()))}）的成交不计入每天的数字：{len(outp)} 笔、名义 ${outp['notional'].sum():,.0f}、"
                 f"收益 ${outp['profit'].sum():,.0f}（3 月月度、3/9–15 周度市场在期间开始前的成交）。")
    # resolution risk: with / without the markets decided only under a reading settled later
    rk = []
    for name, sub in (("全部币安结算", wb), ("去掉结算口径有风险的", wb[wb["risk"] == ""]),
                      ("其中：中午两种约定不同", wb[wb["risk"] == "noon_conv"]), ("其中：创建前已触及", wb[wb["risk"] == "pre_created"])):
        x = sub[sub["dt"] >= 5]
        dsub = daily_dollars(sub)
        a, b = day_stats(dsub, "notional"), day_stats(dsub, "profit")
        rk.append({"g": name, "mk": x["cid"].nunique(), "trades": len(x), "notional": _f(x["notional"].sum()),
                   "profit": _f(x["profit"].sum()), "dn": f"{a['mean']:,.1f} / {a['median']:,.1f}",
                   "dp": f"{b['mean']:,.2f} / {b['median']:,.2f}"})
    L.append("\n结算口径风险（L = 5 秒；每天 = p ≤ 0.995、期间内）：\n")
    L.append(md_table(pd.DataFrame(rk), ["g", "mk", "trades", "notional", "profit", "dn", "dp"],
                      ["市场", "有成交市场", "笔数", "名义$", "收益$", "名义$/天 平均 / 中位数", "收益$/天 平均 / 中位数"]))
    alld = s5all.groupby("day").agg(notional=("notional", "sum"), profit=("profit", "sum"),
                                    trades=("size", "size")).sort_values("profit", ascending=False).head(10).reset_index()
    if len(alld):
        top = alld.copy()
        for c in ("notional", "profit"):
            top[c] = top[c].map(lambda x: _f(x, 1))
        L.append("\n收益最多的 10 天（币安结算，期间内，L = 5 秒，全部价格 < 1）：\n")
        L.append(md_table(top, ["day", "trades", "notional", "profit"], ["日期", "笔数", "名义$", "收益$"]))
    if len(lose):
        lg = lose.groupby(["kind", "taker_buy"]).agg(trades=("size", "size"), shares=("size", "sum"),
                                                    notional=("notional", "sum")).reset_index()
        lg["side"] = np.where(lg["taker_buy"], "taker 买入输方", "taker 卖出输方")
        for c in ("shares", "notional"):
            lg[c] = lg[c].map(lambda x: _f(x))
        L.append("\nT* 之后输的一边以 p > 0 成交（taker 买入 = 付钱买作废代币的一方；taker 卖出 = 挂单方在买作废代币）：\n")
        L.append(md_table(lg, ["kind", "side", "trades", "shares", "notional"], ["类型", "方向", "笔数", "份数", "名义$"]))
    L.append("\n## 几乎确定：结束前 60/30/10/5/1 分钟模型 ≥ 99%（及 95–99%）的一边，之后 60 秒内的成交\n")
    L.append("无漂移模型，σ = 过去 1 小时 1 秒对数收益的标准差，按剩余时间放大；每份盈亏 = 结果 − p（挂单无费；吃单再扣费），按份数加权，t 按市场聚类。"
             f"按分钟分的表（含“全部”行，即判定口径）只含币安结算的六类；{CONTROL} 只在按类型的表里作对照。“全部”行把各时点合在一起（同一市场多次出现，仍按市场聚类）。"
             "“卖方主动每份”= 只算 taker 卖出该边（已有挂单买方接到）的成交，是事后拆分，不是 RESOLVED.md 的判定口径。"
             f"聚类少于 {MIN_G} 个时不给 t（“–”）；† = 没有一个输的市场，这时 t 只反映价格的离散，不说明尾部风险。\n")
    if len(near):
        cols = ["bucket", "group", "points", "model", "win_all", "loss_pts", "exp_fail", "pts_tr", "mk_tr", "trades", "shares",
                "vwap", "win_tr", "losses", "pnl", "t", "pnl_taker", "t_taker", "mk_sell", "pnl_sell", "t_sell"]
        heads = ["模型", "分钟前/类型", "时点", "模型均值", "实际胜率", "输的时点", "模型预期输的时点", "有成交时点", "有成交市场", "笔数", "份数",
                 "均价", "成交加权胜率", "有成交且输的市场", "每份盈亏", "t", "吃单每份", "t", "卖方主动市场", "卖方主动每份", "t"]
        for tab in (near, near_kind):
            n2 = tab.copy()
            n2["t"] = [t_cell(t, g, l) for t, g, l in zip(tab["t"], tab["mk_tr"], tab["losses"])]
            n2["t_taker"] = [t_cell(t, g, l) for t, g, l in zip(tab["t_taker"], tab["mk_tr"], tab["losses"])]
            n2["t_sell"] = [t_cell(t, g, l) for t, g, l in zip(tab["t_sell"], tab["mk_sell"], tab["losses_sell"])]
            for c in ("model", "win_all", "win_tr", "vwap"):
                n2[c] = n2[c].map(lambda x: _f(x, 4))
            for c in ("pnl", "pnl_taker", "pnl_sell"):
                n2[c] = n2[c].map(lambda x: _f(x, 5))
            n2["exp_fail"] = n2["exp_fail"].map(lambda x: _f(x, 1))
            n2["shares"] = n2["shares"].map(lambda x: _f(x))
            L.append(md_table(n2, cols, heads))
            L.append("")
    cal, cal4 = calibration(cp, mk), calibration(cp, mk, kinds=(CONTROL,))
    # decision lines
    ok1, ok1s = sN["mean"] >= 50, sSN["mean"] >= 50
    a = near[(near["group"] == "all") & (near["bucket"] == ">=0.99")] if len(near) else pd.DataFrame()
    ok2 = bool(len(a) and a["pnl"].iloc[0] > 0 and a["t"].iloc[0] >= 2)
    s5c = w[(w["dt"] >= 5) & (w["price"] <= 0.995) & w["kind"].isin(BINANCE_KINDS) & w["in_period"]]
    tmn = s5c.groupby("cid")["notional"].sum().sort_values(ascending=False)
    dd2 = daily_dollars(s5c[~s5c["cid"].isin(set(tmn.index[:10]))])
    r2N, r2SN = day_stats(dd2, "notional"), day_stats(dd2, "sell_notional")
    ddr = daily_dollars(w[w["risk"] == ""])
    rN, rP = day_stats(ddr, "notional"), day_stats(ddr, "profit")
    L.append("\n## 判定（RESOLVED.md）\n")
    L.append(f"- 结果已定部分（币安结算的六类，{FIRST}–{LAST} 的 {sN['n']} 个 ET 日，T*+5 秒之后、closedTime 之前、p ≤ 0.995 的赢方成交）："
             f"每天名义平均 ${sN['mean']:,.1f}（任何主动方）、${sSN['mean']:,.1f}（卖方主动）；门槛 $50/天（按平均）→ "
             f"{'达到' if ok1 else '未达到'}（任何主动方）/{'达到' if ok1s else '未达到'}（卖方主动）。"
             f"{'按预先登记的口径值得做前向和实盘准备，' if ok1 else '不值得为它做前向和实盘准备。'}"
             + (f"但典型的一天达不到：名义中位数 ${sN['median']:,.1f}/天（卖方主动 ${sSN['median']:,.1f}），≥ $50 的天只有 {sN['ge']}/{sN['n']}"
                f"（卖方主动 {sSN['ge']}/{sSN['n']}）。" if ok1 and sN["median"] < 50 else
                (f"名义中位数 ${sN['median']:,.1f}/天，≥ $50 的天 {sN['ge']}/{sN['n']}。" if ok1 else "")))
    L.append(f"- 能赚到的钱很少、很集中：收益平均 ${sP['mean']:,.2f}/天、中位数 ${sP['median']:,.2f}/天；08–09 月平均 ${lP['mean']:,.2f}/天、"
             f"中位数 ${lP['median']:,.2f}/天。其中 {buy_share:.0%} 是 taker 买入（吃过时卖单，要抢速度）；挂单能接到的上限（卖方主动）收益平均 "
             f"${sSP['mean']:,.2f}/天、中位数 ${sSP['median']:,.2f}/天，而且卖方主动收益的 {sell999:.0%} 在 0.999（只靠排队）。"
             "成交只证明有过这个价，不证明我们能拿到这个量。")
    L.append(f"- 去掉名义金额最大的 10 个市场后每天名义平均 ${r2N['mean']:,.1f}、中位数 ${r2N['median']:,.1f}（卖方主动平均 ${r2SN['mean']:,.1f}）；"
             f"去掉结算口径有风险的 {int((mk['risk'] != '').sum())} 个市场后名义平均 ${rN['mean']:,.1f}、中位数 ${rN['median']:,.1f}，"
             f"收益平均 ${rP['mean']:,.2f}、中位数 ${rP['median']:,.2f}。")
    L.append(f"- {CONTROL}（Chainlink，对照，不计入）：名义平均 ${cN['mean']:,.1f}/天、中位数 ${cN['median']:,.1f}。"
             "不含每小时涨跌和每小时“高于”阶梯（不在预先登记的范围内），所以对 BTC 市场整体是下限。")
    if cal["points"]:
        L.append(f"- 模型校准（≥ 99% 的时点，币安结算）：{cal['points']:,} 个时点（{cal['days']} 天），模型预期失败 Σ(1−p) = {cal['expected']:.1f} 次，"
                 f"实际失败 {cal['fail_pts']} 个时点 = {cal['fail_mk']} 个市场、{cal['fail_ev']} 个事件、{cal['fail_days']} 天"
                 f"（同一市场在几个时点重复、同一天的档位共用一条价格路径，所以独立的失败大约按天数算）→ 模型过于自信，但倍数只能粗略地说。"
                 + (f"{CONTROL}（币安代理对 Chainlink 结算）：{cal4['points']:,} 个时点，预期 {cal4['expected']:.1f}，实际 {cal4['fail_pts']} 个时点 / "
                    f"{cal4['fail_mk']} 个市场。" if cal4["points"] else ""))
    if len(a):
        r0 = a.iloc[0]
        k4 = near_kind[(near_kind["kind"] == CONTROL) & (near_kind["bucket"] == ">=0.99")] if len(near_kind) else pd.DataFrame()
        L.append(f"- 几乎确定部分（≥ 99%，币安结算，各时点合并）：每份 {r0['pnl']:+.5f}，t = {_f(r0['t'], 2)}（{int(r0['mk_tr'])} 个市场聚类，"
                 f"其中有成交且输的 {int(r0['losses'])} 个）→ "
                 f"{'满足“每份 > 0 且 t ≥ 2”，可以写前向规则。' if ok2 else '不满足“每份 > 0 且 t ≥ 2”，不写前向规则。'}"
                 + (f"（{CONTROL} 对照：每份 {k4['pnl'].iloc[0]:+.5f}，t = {_f(k4['t'].iloc[0], 2)}。）" if len(k4) else ""))
    L.append("\n文件：`real/resolved-trades.csv.gz`（T* 之后、结算之前赢方 p<1 的全部成交，含 updown_4h 对照；risk 列为结算口径风险）。"
             "代码 `resolved.py`，测试 `test_resolved.py`。")
    Path(out_md).write_text("\n".join(L) + "\n")
    cols = ["kind", "slug", "day", "ts", "dt", "taker_buy", "price", "size", "hours", "risk"]
    c = w[cols].copy()
    c["taker_buy"] = c["taker_buy"].astype(int)
    c["hours"] = c["hours"].round(3)
    c = c.sort_values(["ts", "slug"])
    c.to_csv(out_csv, index=False, compression="gzip")
    return "\n".join(L)


# ===================================================================== main

def run(args, log=print):
    t_start = time.time()
    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)
    http = Http(cache, rate=args.rate)
    mk_path = cache / "markets.parquet"
    if mk_path.exists() and not args.refresh:
        mk = pd.read_parquet(mk_path)
        notes = json.loads((cache / "discover_notes.json").read_text())
    else:
        mk, notes = discover(http, log=log)
        mk.to_parquet(mk_path)
        (cache / "discover_notes.json").write_text(json.dumps(notes, indent=1))
    cov, missing = coverage(mk)
    log(cov.to_string(index=False))
    if args.step == "discover":
        return
    spot, spot_missing = build_spot(args.klines, cache, http, log=log)
    mchk_path = cache / "minute_check.parquet"
    if mchk_path.exists():
        mchk = pd.read_parquet(mchk_path)
    else:
        mchk = check_minutes(spot, http, cache, log=log)
        mchk.to_parquet(mchk_path)
    log(f"1m check: {int(mchk['minutes'].sum())} minutes, bad H/L/C {int(mchk['bad_h'].sum())}/{int(mchk['bad_l'].sum())}/{int(mchk['bad_c'].sum())}")
    mall = attach_1m(spot, http, cache, log=log)
    mall.to_parquet(cache / "minute_check_all.parquet")
    log(f"1m all days: {int(mall['minutes'].sum())} minutes, bad H/L/C {int(mall['bad_h'].sum())}/{int(mall['bad_l'].sum())}/"
        f"{int(mall['bad_c'].sum())}; 1 s wider H/L {int(mall['h_wider'].sum())}/{int(mall['l_wider'].sum())}")
    if args.step == "spot":
        return
    mk, conv, mism = decide(mk, spot)
    mk["ref"] = np.where(mk["kind"] == "updown_day", mk[f"ref_{conv}"], NAN)
    mk["risk"] = risk_flags(mk)
    log(f"noon convention mismatches {mism}; using {conv}; excluded {mk['excluded'].value_counts().to_dict()}")
    mk.to_parquet(cache / "decided.parquet")
    if args.step == "decide":
        return
    if args.limit:
        mk = mk[mk.groupby("kind").cumcount() < args.limit]
    t_fetch, calls0 = time.time(), http.calls
    trades, info = fetch_all_trades(http, mk, cache, workers=args.workers, log=log)
    if http.calls - calls0 > 100 and not args.limit:
        (cache / "timing.json").write_text(json.dumps({"fetch_s": time.time() - t_start, "requests": http.calls}))
    trades = trades[trades["cid"].isin(set(mk["cid"]))]
    pre = fetch_pre_tstar(http, mk, cache, log=log) if not args.limit else trade_frame("", [], "", "")
    sl = second_level(pre, mk, spot)
    if args.step == "trades":
        return
    pt = post_tstar(trades, mk)
    win = pt[pt["win"] & (pt["price"] < 1)]
    w, after = win[~win["after_close"]].copy(), win[win["after_close"]].copy()   # settled already: not 'unsettled'
    lose = pt[~pt["win"] & (pt["price"] > 0)].copy()
    cp = checkpoints(mk, spot)
    near, near_kind, nj = near_table(cp, trades, mk)
    cp.to_parquet(cache / "checkpoints.parquet")
    rt = time.time() - t_start
    text = report(mk, cov, missing, conv, mism, near_kind, mchk, trades, info[info["cid"].isin(set(mk["cid"]))] if len(info) else info,
                  w, lose, cp, near, notes, rt, args.out, args.csv, spot_missing, mall=mall, sl=sl, after=after)
    log(text)
    log(f"runtime {rt:.0f} s; requests {http.calls:,} (cache hits {http.hits:,})")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", nargs="?", default="all", choices=["all", "discover", "spot", "decide", "trades", "report"])
    ap.add_argument("--cache", default=str(CACHE))
    ap.add_argument("--klines", default=str(KLINES))
    ap.add_argument("--out", default=str(HERE / "real" / "resolved.md"))
    ap.add_argument("--csv", default=str(HERE / "real" / "resolved-trades.csv.gz"))
    ap.add_argument("--rate", type=float, default=RATE)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0, help="markets per kind (smoke runs)")
    ap.add_argument("--refresh", action="store_true", help="rediscover markets")
    args = ap.parse_args(argv)
    run(args)


if __name__ == "__main__":
    main()
