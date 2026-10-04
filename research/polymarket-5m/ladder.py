"""Record Polymarket BTC markets that have no taker delay and settle on Binance BTC/USDT 1-minute
candles, with Binance BTCUSDT trades beside them (market data only: no keys, no orders).

    python ladder.py discover [--out markets.json]
    python ladder.py record --out DIR --hours H
    python ladder.py build --src DIR --out DIR/built
    python ladder_check.py DIR [DIR ...] --out real/ladder-check.md

The 5m / 15m / hourly Up/Down markets and some short ladders hold every taker order for about
150 ms (GET clob.polymarket.com/clob-markets/<condition id> has ``"itode": true``).  This recorder
does not infer that property from a slug or Gamma's integer ``secondsDelay``: every candidate is
admitted only after the CLOB endpoint says ``itode`` is absent/false.

- above:      "bitcoin-above-on-<month>-<day>-<year>", 11 strikes ("86,000"), Yes if the Binance
              BTC/USDT 1m candle of 12:00 ET (noon) on that day closes above the strike;
- range:      "bitcoin-price-on-<month>-<day>-<year>", 11 neg-risk ranges ("84,000-86,000",
              "<74,000", ">92,000"), same candle;
- hit_up / hit_down: active daily, weekly or monthly "What price will Bitcoin hit ...?" events
              found through Gamma search ("↑ 86,000", "↓ 80,000"). Yes if any Binance BTC/USDT
              final 1m High/Low in the event window crosses the level;
- updown_day: "bitcoin-up-or-down-on-<month>-<day>-<year>", the candle closing at noon ET against
              the one closing at noon ET the day before;
- updown_4h:  "btc-updown-4h-<start>", the current and the next one (these settle on Chainlink's
              BTC/USD 60 s TWAP stream, not on Binance; see ladder_check.py).

discover lists the markets of today and the next two days (dates in ET), every active hit event
returned by Gamma search, and the 4-hour ones,
confirms on /clob-markets that none holds taker orders, and writes markets.json. record keeps the
CLOB market websocket subscribed to all their tokens (re-discovering every 30 minutes, so the next
day's markets join) and records Binance BTCUSDT aggregated trades from data-stream.binance.vision,
through paper_trader.LiveTrader's own websocket loops and file format (raw/<day>/clob-btc,
binance, markets, errors .jsonl.gz), so recording.py's readers parse it. build turns a recording
into per-token books stamped with the exchange's message time (recording.book_events' "xtop" rows,
plus the tick size and, for the hit markets' Yes tokens, the ask depth below 0.99), the CLOB's
last-trade prints, the Binance trades in latency.py's format, the CLOB disconnect times and the
markets table; load() reads them back.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import gzip
import json
import re
import ssl
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import binary as bo
import paper_trader as pt
import recording as rc

GAMMA_EVENT = "https://gamma-api.polymarket.com/events?slug={slug}"
GAMMA_SEARCH = "https://gamma-api.polymarket.com/public-search?q=bitcoin&limit_per_type=100"
CLOB_MARKET = "https://clob.polymarket.com/clob-markets/{cid}"
# Binance's market-data-only REST host (api.binance.com answers 451 on US runners).
KLINE = "https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=1m&startTime={ms}&limit=1"
MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september",
          "october", "november", "december")
DAILY = {"above": "bitcoin-above-on-{d}", "range": "bitcoin-price-on-{d}",
         "updown_day": "bitcoin-up-or-down-on-{d}"}
H4, H4_S = "btc-updown-4h-{ts}", 4 * 3600
TYPES = ("above", "range", "hit_up", "hit_down", "updown_day", "updown_4h")
REDISCOVER_S = 1800
DEPTH_CAP = 0.99  # hit markets: ask depth below this price (ladder_check barrier events)
NAN = float("nan")


def fetch_json(url, timeout=10):
    """HTTPS JSON fetch with the installed certifi trust store when available."""
    try:
        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:  # pragma: no cover - system Python normally has certifi
        context = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "polymarket-ladder-recorder/1.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=context) as r:
        return json.loads(r.read().decode())


def _fetch_many(fetch, urls, workers=4):
    """Fetch independent public endpoints concurrently, preserving input order and exceptions."""
    def one(url):
        last = None
        for _ in range(2):
            try:
                return fetch(url)
            except Exception as exc:
                last = exc
        return last  # caller records the exact failed URL and excludes it
    with ThreadPoolExecutor(max_workers=min(workers, max(1, len(urls)))) as pool:
        return list(pool.map(one, urls))


# ------------------------------------------------------------------ dates (ET)

def _us_dst(d, hour):
    """US Eastern daylight time on local date d at `hour` (second Sunday of March 02:00 to first
    Sunday of November 02:00); the fallback when the tz database is missing."""
    mar = date(d.year, 3, 8) + timedelta(days=(6 - date(d.year, 3, 8).weekday()) % 7)
    nov = date(d.year, 11, 1) + timedelta(days=(6 - date(d.year, 11, 1).weekday()) % 7)
    return (d > mar or (d == mar and hour >= 3)) and (d < nov or (d == nov and hour < 2))


def _tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/New_York")
    except Exception:  # no tz database on this machine
        return None


def et_to_utc(d, hour=0, minute=0):
    """Unix seconds of `hour:minute` ET (EDT or EST, whichever is in force) on date d."""
    tz = _tz()
    if tz is not None:
        return int(datetime(d.year, d.month, d.day, hour, minute, tzinfo=tz).timestamp())
    off = 4 if _us_dst(d, hour) else 5
    return int(datetime(d.year, d.month, d.day, hour, minute, tzinfo=timezone.utc).timestamp()) + off * 3600


def et_date(ts):
    """The ET calendar date at unix time ts."""
    tz = _tz()
    if tz is not None:
        return datetime.fromtimestamp(ts, tz).date()
    for off in (4, 5):
        local = datetime.fromtimestamp(ts - off * 3600, timezone.utc)
        if _us_dst(local.date(), local.hour) == (off == 4):
            return local.date()
    return datetime.fromtimestamp(ts - 5 * 3600, timezone.utc).date()


def noon_et(d):
    return et_to_utc(d, 12)


def day_slug(d):
    """'october-5-2026' for 2026-10-05, as the daily event slugs spell dates."""
    return f"{MONTHS[d.month - 1]}-{d.day}-{d.year}"


def h4_starts(now):
    """Start times of the current and the next 4-hour market: on 4-hour boundaries in UTC (as on
    2026-10-04, 04:00/08:00/12:00 UTC) and, should the series follow ET midnight in winter, ET."""
    out = set()
    for off in (0, et_to_utc(et_date(now), 0) % 86400):  # ET midnight's offset from UTC midnight
        base = (int(now) - off) // H4_S * H4_S + off
        out.update((base, base + H4_S))
    return sorted(out)


def iso_ts(s):
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


# ------------------------------------------------------------------ titles

def _num(s):
    s = s.strip().replace("$", "").replace(",", "").replace(" ", "")
    mult = 1000.0 if s.lower().endswith("k") else 1.0
    return float(s.rstrip("kK")) * mult


_NUM = r"\$?\s*[0-9][0-9,]*(?:\.[0-9]+)?\s*[kK]?"


def parse_title(title):
    """(shape, lo, hi) from a groupItemTitle: '86,000' -> ('level', 86000, None); '↑ 86,000' ->
    ('up', 86000, None); '↓ 80,000' -> ('down', None, 80000); '84,000-86,000' -> ('range', 84000,
    86000); '<74,000' -> ('range', None, 74000); '>92,000' -> ('range', 92000, None). None if the
    title has none of these shapes."""
    t = (title or "").strip().replace("–", "-").replace("—", "-")
    m = re.fullmatch(rf"(↑|↓)\s*({_NUM})", t)
    if m:
        v = _num(m.group(2))
        return ("up", v, None) if m.group(1) == "↑" else ("down", None, v)
    m = re.fullmatch(rf"(<|>|≤|≥)\s*({_NUM})", t)
    if m:
        v = _num(m.group(2))
        return ("range", None, v) if m.group(1) in "<≤" else ("range", v, None)
    m = re.fullmatch(rf"({_NUM})\s*-\s*({_NUM})", t)
    if m:
        return "range", _num(m.group(1)), _num(m.group(2))
    m = re.fullmatch(rf"({_NUM})", t)
    if m:
        return "level", _num(m.group(1)), None
    return None


# ------------------------------------------------------------------ discovery

def _list(x):
    return json.loads(x) if isinstance(x, str) else list(x or [])


def event_markets(ev, kind, d=None):
    """Our rows for one Gamma event of `kind` (a DAILY key or 'updown_4h') on ET date d: one per
    market, with its type, bounds, times and both tokens. Markets of an unexpected shape are left
    out."""
    rows = []
    for m in ev.get("markets") or []:
        outcomes, tokens = _list(m.get("outcomes")), _list(m.get("clobTokenIds"))
        if len(outcomes) != 2 or len(tokens) != 2 or not m.get("conditionId"):
            continue
        yes = next((i for i, o in enumerate(outcomes) if str(o).lower() in ("yes", "up")), None)
        if yes is None:
            continue
        title = m.get("groupItemTitle") or ""
        parsed = parse_title(title)
        end = iso_ts(m.get("endDate")) or iso_ts(ev.get("endDate"))
        lo = hi = ref_ts = start = expect = None
        if kind == "above":
            if not parsed or parsed[0] != "level":
                continue
            typ, lo = "above", parsed[1]
            expect = noon_et(d) if d else None
        elif kind == "range":
            if not parsed or parsed[0] not in ("range", "level"):
                continue
            typ, lo, hi = "range", parsed[1], parsed[2]
            expect = noon_et(d) if d else None
        elif kind == "hit":
            q = str(m.get("question") or "").lower()
            shape = parsed[0] if parsed else None
            if shape == "up" or (shape == "level" and "reach" in q):
                typ, lo = "hit_up", parsed[1]
            elif shape == "down" or (shape == "level" and "dip" in q):
                typ, hi = "hit_down", parsed[1] if shape == "level" else parsed[2]
            else:
                continue
            start = et_to_utc(d, 0) if d else (iso_ts(m.get("eventStartTime")) or
                                                iso_ts(ev.get("startDate")) or iso_ts(ev.get("startTime")))
            expect = et_to_utc(d + timedelta(days=1), 0) if d else end
        elif kind == "updown_day":
            typ = "updown_day"
            ref_ts = iso_ts(m.get("eventStartTime")) or iso_ts(ev.get("startTime")) or \
                (noon_et(d - timedelta(days=1)) if d else None)
            start, expect = ref_ts, noon_et(d) if d else None
        elif kind == "updown_4h":
            typ = "updown_4h"
            ref_ts = iso_ts(m.get("eventStartTime")) or iso_ts(ev.get("startTime")) or \
                int(str(ev.get("slug", "0")).rsplit("-", 1)[-1])
            start = ref_ts
            expect = ref_ts + H4_S if ref_ts else None
        else:
            raise ValueError(kind)
        rows.append({
            "slug": m.get("slug"), "event_slug": ev.get("slug"), "type": typ, "title": title,
            "question": m.get("question"), "lo": lo, "hi": hi, "end_ts": end, "start_ts": start,
            "ref_ts": ref_ts, "ref_price": None, "ref_source": None, "end_expected": expect,
            "end_ok": None if expect is None or end is None else end == expect,
            "condition_id": m["conditionId"], "yes_token": str(tokens[yes]), "no_token": str(tokens[1 - yes]),
            "yes_outcome": outcomes[yes], "no_outcome": outcomes[1 - yes],
            "tick": float(m["orderPriceMinTickSize"]) if m.get("orderPriceMinTickSize") else None,
            "neg_risk": bool(m.get("negRisk")), "closed": bool(m.get("closed")),
            "settle": "chainlink-twap" if typ == "updown_4h" else "binance-1m",
            "day": d.isoformat() if d else None})
    return rows


def _kline(fetch, open_ts):
    k = fetch(KLINE.format(ms=int(open_ts) * 1000))
    if k and int(k[0][0]) == int(open_ts) * 1000:
        return k[0]
    return None


def reference(m, fetch, now):
    """(price, source) of an up/down market's reference, if it has been fixed: the close of the
    Binance 1m candle closing at the day market's reference time, or, for the 4-hour market, the
    open of the Binance 1m candle at its start (an approximation of Chainlink's price then)."""
    if m["type"] == "updown_day" and m["ref_ts"] and now >= m["ref_ts"] + 5:
        k = _kline(fetch, m["ref_ts"] - 60)
        return (float(k[4]), "binance 1m close") if k else (None, None)
    if m["type"] == "updown_4h" and m["ref_ts"] and now >= m["ref_ts"] + 5:
        k = _kline(fetch, m["ref_ts"])
        return (float(k[1]), "binance 1m open (approximates chainlink)") if k else (None, None)
    return None, None


def discover(fetch=fetch_json, now=None, days=3, cache=None):
    """(markets, notes): every open market of the undelayed types ending after `now`, with the
    /clob-markets check ("itode" absent) and, for up/down, the reference price if fixed. Markets
    whose /clob-markets record holds taker orders are left out and named in the notes. `cache`
    (condition id -> /clob-markets record) saves lookups across re-discoveries."""
    now = time.time() if now is None else now
    cache = {} if cache is None else cache
    rows, notes = [], []
    today = et_date(now)
    requests = []
    for k in range(days):
        d = today + timedelta(days=k)
        for kind, pattern in DAILY.items():
            slug = pattern.format(d=day_slug(d))
            requests.append((slug, kind, d, GAMMA_EVENT.format(slug=slug)))
    for ts in h4_starts(now):
        slug = H4.format(ts=ts)
        requests.append((slug, "updown_4h", None, GAMMA_EVENT.format(slug=slug)))
    requests.append(("bitcoin public search", "search_hit", None, GAMMA_SEARCH))
    for (slug, kind, d, _), got in zip(requests, _fetch_many(fetch, [x[3] for x in requests])):
        if isinstance(got, Exception):
            notes.append(f"{slug}: {type(got).__name__}: {got}")
            continue
        if kind == "search_hit":
            events = [ev for ev in (got or {}).get("events", [])
                      if str(ev.get("slug", "")).startswith("what-price-will-bitcoin-hit")
                      and not ev.get("closed") and (iso_ts(ev.get("endDate")) or 0) > now]
            for ev in events:
                rows += event_markets(ev, "hit")
            if not events:
                notes.append("bitcoin public search: no active hit event")
            continue
        events = got or []
        if not events:
            notes.append(f"{slug}: not listed")
        for ev in events:
            rows += event_markets(ev, kind, d)
    out, seen = [], set()
    eligible = []
    for m in rows:
        if m["condition_id"] in seen or m["closed"] or not m["end_ts"] or m["end_ts"] <= now:
            continue
        seen.add(m["condition_id"])
        eligible.append(m)
    missing = [m for m in eligible if m["condition_id"] not in cache]
    urls = [CLOB_MARKET.format(cid=m["condition_id"]) for m in missing]
    attempted = {}
    for m, got in zip(missing, _fetch_many(fetch, urls)):
        attempted[m["condition_id"]] = got
        if not isinstance(got, Exception):
            cache[m["condition_id"]] = got
    for m in eligible:
        info = cache.get(m["condition_id"], attempted.get(m["condition_id"]))
        if isinstance(info, Exception):
            notes.append(f"{m['slug']}: /clob-markets {type(info).__name__}: {info}; left out")
            continue
        info = info or {}
        clob_tokens = {str(x.get("t")) for x in info.get("t", []) if isinstance(x, dict) and x.get("t")}
        if clob_tokens != {m["yes_token"], m["no_token"]} or not info.get("mts"):
            notes.append(f"{m['slug']}: incomplete /clob-markets record; left out")
            continue
        m["itode"] = bool(info.get("itode"))
        if m["itode"]:
            notes.append(f"{m['slug']}: itode (taker delay); left out")
            continue
        if info.get("mts"):
            m["tick"] = float(info["mts"])
        m["min_order_size"] = info.get("mos")
        m["fee_rate"] = (info.get("fd") or {}).get("r")
        if m["type"] in ("updown_day", "updown_4h"):
            try:
                m["ref_price"], m["ref_source"] = reference(m, fetch, now)
            except Exception as e:
                notes.append(f"{m['slug']}: reference {type(e).__name__}: {e}")
        out.append(m)
    out.sort(key=lambda m: (TYPES.index(m["type"]), m["end_ts"], m["lo"] or 0, m["hi"] or 0))
    return out, notes


def summary(markets, notes=()):
    """Plain-text counts per type and the /clob-markets check, for the log."""
    by = {}
    for m in markets:
        by.setdefault(m["type"], []).append(m)
    lines = [f"{len(markets)} undelayed markets ({2 * len(markets)} tokens); itode on none of them: "
             f"{not any(m.get('itode') for m in markets)}"]
    for t in TYPES:
        ms = by.get(t, [])
        if ms:
            ends = sorted({time.strftime('%m-%d %H:%M', time.gmtime(m['end_ts'])) for m in ms})
            bad = sum(m["end_ok"] is False for m in ms)
            refs = sum(m["ref_price"] is not None for m in ms) if t.startswith("updown") else None
            lines.append(f"  {t:<10} {len(ms):3d}  end (UTC) {', '.join(ends)}"
                         + (f"  end != computed ET time: {bad}" if bad else "")
                         + (f"  reference known: {refs}" if refs is not None else ""))
    lines += [f"  note: {n}" for n in notes if "not listed" not in n]
    missing = [n.split(":")[0] for n in notes if "not listed" in n]
    if missing:
        lines.append(f"  not listed yet: {', '.join(missing)}")
    return "\n".join(lines)


# ------------------------------------------------------------------ recording

class LadderRecorder(pt.LiveTrader):
    """paper_trader.LiveTrader's CLOB and Binance websocket loops and recorder, subscribed to the
    undelayed markets found by discover() instead of the 5m Up/Down series; paper-trades nothing."""

    def __init__(self, out, fetch=fetch_json, rediscover=REDISCOVER_S):
        super().__init__(out, pt.Params(), fetch=fetch, coins=("btc",), trade_coins=())
        self.rediscover = rediscover
        self.ladder = {}       # condition id -> market row
        self.clob_cache = {}   # condition id -> /clob-markets record

    def refresh(self, markets, now=None):
        """Take a discovery: new markets' tokens join the subscription (returned, to subscribe on
        the open socket), markets past their end leave the list used on the next reconnect."""
        now = time.time() if now is None else now
        new = []
        for m in markets:
            old = self.ladder.get(m["condition_id"])
            if old is not None and old.get("ref_price") is not None and m.get("ref_price") is None:
                m = {**m, "ref_price": old["ref_price"], "ref_source": old["ref_source"]}
            self.ladder[m["condition_id"]] = m
            for tok in (m["yes_token"], m["no_token"]):
                self.token_coin[tok] = "btc"
                if tok not in self.subscribed:
                    self.subscribed.add(tok)
                    new.append(tok)
        for cid, m in list(self.ladder.items()):
            if m["end_ts"] + 600 < now:
                self.subscribed.difference_update((m["yes_token"], m["no_token"]))
                del self.ladder[cid]
        return new

    async def discover_once(self):
        markets, notes = await asyncio.to_thread(discover, self.fetch, None, 3, self.clob_cache)
        self.rec.write("markets", {"at": pt.now_ms(), "markets": markets, "notes": notes})
        new = self.refresh(markets)
        (self.out / "markets.json").write_text(json.dumps({"at": pt.now_ms(), "markets": list(self.ladder.values()),
                                                           "notes": notes}, indent=1))
        print(time.strftime("%H:%M:%S"), summary(markets, notes), flush=True)
        if new and self.ws is not None:
            await self.ws.send(json.dumps({"assets_ids": new, "operation": "subscribe",
                                           "custom_feature_enabled": True}))
        return markets

    async def discover_loop(self, first_done=False):
        wait = self.rediscover if first_done else 0
        while True:
            await asyncio.sleep(wait)
            try:
                found = await self.discover_once()
                wait = self.rediscover if found else 60
            except Exception as e:
                self.rec.write("errors", {"at": pt.now_ms(), "where": "discover", "err": repr(e)})
                wait = 60

    async def run(self, hours):
        asyncio.get_running_loop().set_exception_handler(
            lambda loop, ctx: self.rec.write("errors", {"at": pt.now_ms(), "where": "loop",
                                                        "err": repr(ctx.get("exception") or ctx.get("message"))}))
        first = False
        for _ in range(10):  # subscribe with the first discovery rather than an empty list
            try:
                first = bool(await self.discover_once())
            except Exception as e:
                self.rec.write("errors", {"at": pt.now_ms(), "where": "discover", "err": repr(e)})
            if first:
                break
            await asyncio.sleep(30)
        tasks = [asyncio.create_task(c) for c in (self.discover_loop(first), self.clob_loop(), self.binance_loop())]
        try:
            await asyncio.sleep(hours * 3600)
        finally:
            for t in tasks:
                t.cancel()
            self.rec.close()


# ------------------------------------------------------------------ build

def ask_depth(asks, cap=DEPTH_CAP, rate=bo.CRYPTO_FEE_RATE):
    """(shares, dollars to gain if Yes) offered at asks below `cap`: sum of size, and of
    (1 - price - taker fee) * size."""
    sz = edge = 0.0
    for p, s in asks.items():
        if p < cap - 1e-9:
            sz += s
            edge += (1.0 - p - rate * p * (1.0 - p)) * s
    return round(sz, 6), round(edge, 6)


def book_rows(records, depth_tokens=(), ticks=None):
    """Yield ("top", row) and ("trade", row) from recorded CLOB messages in arrival order.
    "top" rows are recording.book_events' "xtop" rows (top of book and first-level sizes whenever
    they change, stamped with the exchange time of the message that changed them, one row per
    message; messages without a time are applied but not stamped), plus the token's tick size
    (from `ticks`, then tick_size_change messages) and, for `depth_tokens`, the ask depth below
    DEPTH_CAP (a change there also makes a row). "trade" rows are last_trade_price prints."""
    depth_tokens, tick = set(depth_tokens), dict(ticks or {})
    ladders, last = {}, {}
    for rec in records:
        ms, msg = int(rec["recv_ms"]), rec.get("msg")
        for m in msg if isinstance(msg, list) else [msg]:
            if not isinstance(m, dict):
                continue
            et = m.get("event_type")
            if et == "last_trade_price":
                try:
                    yield "trade", {"asset_id": m["asset_id"], "ts": int(m.get("timestamp") or ms) / 1000,
                                    "recv_ms": ms, "price": float(m["price"]), "size": float(m.get("size") or "nan"),
                                    "side": m.get("side")}
                except (KeyError, ValueError, TypeError):
                    pass
                continue
            if et == "tick_size_change":
                try:
                    tick[m["asset_id"]] = float(m["new_tick_size"])
                except (KeyError, ValueError, TypeError):
                    pass
                continue
            if et == "book":
                toks = [m["asset_id"]]
            elif et == "price_change":
                toks = list(dict.fromkeys(pc["asset_id"] for pc in m.get("price_changes", [])))
            else:
                continue
            pt.apply_clob_message(ladders, m, ms)
            stamp = m.get("timestamp")
            if not stamp:
                continue
            for tok in toks:
                lad = ladders[tok]
                x = (lad.bid, lad.ask, lad.size_at("bid", lad.bid), lad.size_at("ask", lad.ask))
                d = ask_depth(lad.asks) if tok in depth_tokens else (NAN, NAN)
                if (x, d) != last.get(tok):
                    last[tok] = (x, d)
                    yield "top", {"asset_id": tok, "ts": int(stamp) / 1000, "recv_ms": ms, "top": x,
                                  "tick": tick.get(tok, NAN), "depth": d}


def raw_markets(src):
    """Market rows from a recording: the latest discovery of each condition id (keeping a reference
    price an earlier one had), from raw/*/markets.jsonl.gz and, if present, <src>/markets.json."""
    out = {}

    def take(rows):
        for m in rows or []:
            if not isinstance(m, dict) or "condition_id" not in m:
                continue
            old = out.get(m["condition_id"])
            if old is not None and old.get("ref_price") is not None and m.get("ref_price") is None:
                m = {**m, "ref_price": old["ref_price"], "ref_source": old.get("ref_source")}
            out[m["condition_id"]] = m

    for snap in rc.iter_jsonl(rc.raw_files(src, "markets")):
        if isinstance(snap, dict):
            take(snap.get("markets"))
    f = Path(src) / "markets.json"
    if f.exists():
        try:
            take(json.loads(f.read_text()).get("markets"))
        except (ValueError, AttributeError):
            pass
    return sorted(out.values(), key=lambda m: (TYPES.index(m["type"]) if m.get("type") in TYPES else 99,
                                               m.get("end_ts") or 0, m.get("lo") or 0, m.get("hi") or 0))


def _f(x, nd=None):
    if x is None or not np.isfinite(x):
        return ""
    return f"{x:.{nd}f}" if nd is not None else f"{x:.10g}"


def build(src, out):
    """Write one recording's tables under `out`; returns counts for the log."""
    src, out = Path(src), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    markets = raw_markets(src)
    (out / "markets.json").write_text(json.dumps(markets, indent=1))
    depth = {m["yes_token"] for m in markets if m.get("type") in ("hit_up", "hit_down")}
    ticks = {tok: m["tick"] for m in markets if m.get("tick") for tok in (m["yes_token"], m["no_token"])}
    counts = {"markets": len(markets), "books": 0, "clob_trades": 0}
    with gzip.open(out / "books.csv.gz", "wt", newline="") as fb, gzip.open(out / "clob_trades.csv.gz", "wt",
                                                                            newline="") as ft:
        wb, wt = csv.writer(fb), csv.writer(ft)
        wb.writerow(["market_id", "ts", "recv", "bid", "ask", "bid_size", "ask_size", "tick", "depth99", "edge99"])
        wt.writerow(["asset_id", "ts", "recv", "price", "size", "side"])
        for kind, r in book_rows(rc.iter_jsonl(rc.clob_files(src, "btc")), depth, ticks):
            if kind == "top":
                bid, ask, bs, az = r["top"]
                wb.writerow([r["asset_id"], f"{r['ts']:.3f}", f"{r['recv_ms'] / 1000:.3f}", _f(bid), _f(ask), _f(bs),
                             _f(az), _f(r["tick"]), _f(r["depth"][0]), _f(r["depth"][1], 4)])
                counts["books"] += 1
            else:
                wt.writerow([r["asset_id"], f"{r['ts']:.3f}", f"{r['recv_ms'] / 1000:.3f}", _f(r["price"]),
                             _f(r["size"]), r["side"] or ""])
                counts["clob_trades"] += 1
    n = 0
    with gzip.open(out / "binance_trades.jsonl.gz", "wt") as f:  # latency.py's "binancews" format
        for ts, value, recv_ms in rc.binance_ws_ticks(rc.iter_jsonl(rc.raw_files(src, "binance")), "BTCUSDT"):
            f.write(json.dumps({"event": "BINANCE_WS_TRADE", "trade_ts": ts, "receive_ts": recv_ms / 1000,
                                "price": value}) + "\n")
            n += 1
    counts["binance"] = n
    closes = rc.clob_close_times(rc.iter_jsonl(rc.raw_files(src, "errors")))
    with gzip.open(out / "clob_closes.csv.gz", "wt", newline="") as f:  # latency.load_closes' format
        w = csv.writer(f)
        w.writerow(["at_s"])
        w.writerows([f"{x:.3f}"] for x in sorted(closes))
    counts["clob_closes"] = len(closes)
    (out / "counts.json").write_text(json.dumps(counts))
    return counts


def load(d):
    """(books, markets, spot, clob_trades, closes) of one built directory: books with latency.py's
    columns (market_id = token id, ts = exchange time, recv, bid, ask, bid_size, ask_size) plus tick,
    depth99 and edge99, sorted by token and time; the markets table; Binance trades (trade_ts,
    receive_ts, price); last-trade prints; CLOB disconnect times (recorder clock) or None."""
    import latency as lt
    d = Path(d)
    books = pd.read_csv(d / "books.csv.gz", dtype={"market_id": str})
    books = books.sort_values(["market_id", "ts"], kind="stable").reset_index(drop=True)
    markets = pd.DataFrame(json.loads((d / "markets.json").read_text()))
    for c in ("yes_token", "no_token", "condition_id"):
        if c in markets:
            markets[c] = markets[c].astype(str)
    for c in ("lo", "hi", "end_ts", "start_ts", "ref_ts", "ref_price", "tick"):
        if c in markets:
            markets[c] = pd.to_numeric(markets[c], errors="coerce")
    rows = lt._binance(d, "binancews")
    spot = pd.DataFrame(rows, columns=["trade_ts", "receive_ts", "price"]).drop_duplicates() \
        .sort_values("trade_ts", kind="stable").reset_index(drop=True)
    f = d / "clob_trades.csv.gz"
    trades = pd.read_csv(f, dtype={"asset_id": str}) if f.exists() else \
        pd.DataFrame(columns=["asset_id", "ts", "recv", "price", "size", "side"])
    return books, markets, spot, trades, lt.load_closes(d)


# ------------------------------------------------------------------ CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("discover", help="列出今天和之后两天的无延迟 BTC 市场，写 markets.json")
    p.add_argument("--out", default="markets.json")
    p.add_argument("--days", type=int, default=3)
    p = sub.add_parser("record", help="录制这些市场的盘口和币安 BTCUSDT 成交（不下单）")
    p.add_argument("--out", required=True)
    p.add_argument("--hours", type=float, required=True)
    p.add_argument("--rediscover", type=float, default=REDISCOVER_S, help="重新发现市场的间隔（秒）")
    p = sub.add_parser("build", help="把录制转换成逐代币盘口、币安成交和市场表")
    p.add_argument("--src", required=True)
    p.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "discover":
        markets, notes = discover(days=a.days)
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps({"at": pt.now_ms(), "markets": markets, "notes": notes}, indent=1))
        print(summary(markets, notes))
    elif a.cmd == "record":
        print(f"录制无延迟 BTC 市场 {a.hours} 小时，只记录、不下单。输出目录 {a.out}", flush=True)
        Path(a.out).mkdir(parents=True, exist_ok=True)
        asyncio.run(LadderRecorder(a.out, rediscover=a.rediscover).run(a.hours))
    else:
        print(" ".join(f"{k}={v}" for k, v in build(a.src, a.out).items()))


if __name__ == "__main__":
    main()
