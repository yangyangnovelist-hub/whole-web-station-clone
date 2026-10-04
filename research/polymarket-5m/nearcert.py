"""NEARCERT.md test 1 (history): is the side a driftless model puts at 95-99 % under-priced, on the half
year resolved.py never used (2025-09-15 .. 2026-03-13, ET dates)? Market data only; no orders, no keys.

    python nearcert.py all      [--cache DIR] [--out real/nearcert-history.md]
    python nearcert.py discover | rules | spot | decide | trades | report     (the same steps one by one)

Everything that can be is imported from resolved.py (read-only): Gamma discovery (rs.discover with this
period), the market rows, the Http cache / rate limit, the kline reader and Spot, T* / outcome logic
(rs.decide, which also picks the noon candle convention from THIS period's official outcomes), the
trade fetch (rs.fetch_all_trades), the checkpoints and model (rs.checkpoints, rs.p_yes), the
near-certain table (rs.near_table, rs.clustered_mean) and the decided-but-unsettled tables
(rs.post_tstar, rs.lag_table, rs.daily_dollars). What is new here:

0. discovery extras: rs.h4_slugs asks for 4-hour windows on UTC boundaries, but in EST (2025-11-02 ..
   2026-03-08) the series started at 01:00 / 05:00 / ... UTC (ET midnight + 4 h k), so those slugs are
   asked too (control only); and hit events whose window rs.hit_window cannot read (the yearly "What
   price will Bitcoin hit in 2025?", which the monthly series ran instead of a December-2025 event) are
   kept as kind 'hit_other' only so that the rule check can name and exclude them.
1. rules: every market's description (Gamma, fetched with the events) is parsed into a resolution
   signature: source (Binance / Chainlink / ...), pair (BTC/USDT, BTC/USD), candle (1m, 1h, ...), price
   field (Close / High / Low), time (noon = "12:00 in the ET timezone"), comparison / tie rule, the hit
   window (day / week / month / from creation) and, for daily up/down, that the reference is the
   previous day's noon candle. Each kind's signature in resolved.py's 2026 rules is written down here
   (EXPECTED) and checked first on a Gamma sample of 2026 events (one date / week / month per kind and
   month, 2026-03 .. 2026-09). A market of this period whose signature differs from its kind's in ANY
   feature (or that has no description) is excluded with the reason; a kind with all markets excluded
   is named as excluded. The computed-vs-official check of rs.decide then excludes single mismatches.
2. spot: Binance BTCUSDT 1 s klines 2025-09-01 .. 2026-03-14 (UTC days; windows of the monthly hit
   event of September start 09-01, the updown reference needs the day before, 4-hour windows end on
   03-14) from data.binance.vision daily zips into <cache>/klines. The raw open-time unit of every
   file is recorded (ms or us; rs.read_kline_zip converts both). Every day's official 1m klines are
   downloaded too, compared minute by minute with the 1 s aggregates and used for T* and outcomes
   (rs.attach_1m), exactly as resolved.py does.
3. near-certain: rs.checkpoints (60 / 30 / 10 / 5 / 1 minutes before the end, still undecided,
   S = last 1 s close, sigma = std of 1 s log returns of the past hour scaled by sqrt(time left),
   reflection for hit markets) and rs.near_table (buckets >= 0.99 and [0.95, 0.99); the favoured
   token's trades in [t, t + 60), any taker side, share-weighted, pnl = official result - price,
   clustered by market; the taker-SELL-only column), by checkpoint and by kind, plus a month table
   (ET month of the checkpoint) computed with the same rows.
4. decided-but-unsettled (description only): resolved.md's lag tables for this period.

Conservative choices where NEARCERT.md is silent (written down before the run):
- The period is the events whose last ET day is in 2025-09-15 .. 2026-03-13 (resolved.py's own rule for
  its period), so the week "March 9-15, 2026" and the month March 2026 stay with resolved.py.
- Rule check is strict: any feature differing from the 2026 rule excludes the market (a missing or
  unparseable description too); the rule table lists signatures by kind and month.
- The noon candle convention is NOT carried over from resolved.py; rs.decide picks it again from this
  period's official outcomes and both mismatch counts are reported.
- Verdict (NEARCERT.md): the pooled row of [0.95, 0.99), all five checkpoints, Binance-settled kinds
  only (the 4-hour Chainlink markets are a separate control row), pnl per share = result - price with
  no fee, any taker side, share-weighted (resolved.py's 'pnl' column), t clustered by market; it passes
  only if pnl > 0 AND t >= 2 AND the number of markets WITH TRADES in the bucket (clusters) is >= 200.
  The taker-fee and taker-sell-only columns are reported beside it; they are not the verdict, but a
  pass whose taker-fee pnl is <= 0 is said so in the verdict line.
- t cells with fewer than 20 clusters are shown as '–' (resolved.py's t_cell); '†' marks a t where no
  cluster lost (the SE then says nothing about the tail).
"""
from __future__ import annotations

import argparse
import inspect
import io
import json
import math
import re
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import ladder as lad
import resolved as rs

HERE = Path(__file__).resolve().parent
SCRATCH = Path("/tmp/claude-0/-home-user-whole-web-station-clone/d12980d9-5808-53a6-9da0-aeb3bb8bdfb4/scratchpad")
CACHE = SCRATCH / "nearcert"
FIRST, LAST = date(2025, 9, 15), date(2026, 3, 13)
SPOT_FIRST, SPOT_LAST = date(2025, 9, 1), date(2026, 3, 14)       # UTC days of 1 s klines needed
REF_MONTHS = [(2026, m) for m in range(3, 10)]                      # 2026 rule samples (resolved.py's period)
RATE = 4.0
TEST, REF = "0.95-0.99", ">=0.99"
MIN_MARKETS, MIN_T = 200, 2.0
NAN = float("nan")
KINDS = rs.KINDS
CONTROL = getattr(rs, "CONTROL", "updown_4h")
BINANCE_KINDS = getattr(rs, "BINANCE_KINDS", tuple(k for k in KINDS if k != CONTROL))
NOON_KINDS, HIT_KINDS = rs.NOON_KINDS, rs.HIT_KINDS


def _call(fn, *args, **kw):
    """fn(*args, **kw) with the keyword arguments fn does not take dropped (resolved.py is being edited
    concurrently; older / newer signatures differ only in optional keywords)."""
    try:
        ps = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return fn(*args, **kw)
    if any(p.kind == p.VAR_KEYWORD for p in ps.values()):
        return fn(*args, **kw)
    return fn(*args, **{k: v for k, v in kw.items() if k in ps})


# ===================================================================== discovery

def h4_slugs_et(first=FIRST, last=LAST):
    """4-hour windows on ET-midnight boundaries for ET days first .. last. In EDT they coincide with
    rs.h4_slugs (00:00 / 04:00 / ... UTC); in EST (2025-11-02 .. 2026-03-08) the series started at
    01:00 / 05:00 / ... UTC, which rs.h4_slugs (UTC boundaries) does not ask for."""
    out = []
    for d in rs.days(first, last):
        a = rs.et_midnight(d)
        out += [f"btc-updown-4h-{a + k * rs.H4_S}" for k in range(6)]
    return out


def discover(http, first=FIRST, last=LAST, log=print):
    """rs.discover for this period, plus (a) 4-hour markets on ET boundaries (EST months) and (b) the hit
    events whose window resolved.py cannot read (e.g. the yearly 'What price will Bitcoin hit in 2025?',
    which stood in the monthly series for December 2025) as kind 'hit_other' - kept only to be named
    and excluded by the rule check."""
    mk, notes = rs.discover(http, first=first, last=last, log=log)
    rows = []
    lo_ts, hi_ts = rs.et_midnight(first), rs.et_midnight(last + timedelta(days=1))
    have = set(mk["event_slug"]) if len(mk) else set()
    extra = [s for s in h4_slugs_et(first, last) if s not in have]
    for ev in rs.events_by_slug(http, extra):
        rows += rs.market_rows(ev, "updown_4h")
    n4 = len(rows)
    slugs, _ = rs.search_hit_slugs(http)
    for ev in rs.events_by_slug(http, [s for s in slugs if s not in have]):
        end = rs.ts_of(ev.get("endDate"))
        if not (ev.get("closed") and np.isfinite(end) and lo_ts < end <= hi_ts):
            continue
        for r in rs.market_rows(ev, "hit"):
            if r["kind"] == "hit_unknown":
                r["kind"] = "hit_other"
                rows.append(r)
    if rows:
        ex = pd.DataFrame(rows)
        h4 = ex["kind"] == "updown_4h"
        ex.loc[h4, "day"] = [lad.et_date(int(w)).isoformat() for w in ex.loc[h4, "ws"]]
        ex.loc[~h4, "day"] = [lad.et_date(int(e) - 1).isoformat() for e in ex.loc[~h4, "end"]]
        ex = ex[(ex["day"] >= first.isoformat()) & (ex["day"] <= last.isoformat())]
        mk = pd.concat([mk, ex], ignore_index=True).drop_duplicates("cid").reset_index(drop=True)
    notes["h4_et_markets_added"] = int(n4)
    notes["hit_other_markets"] = int((mk["kind"] == "hit_other").sum())
    log(f"extra: {n4} 4-hour markets on ET boundaries, {notes['hit_other_markets']} hit markets of other windows")
    return mk, notes


# ===================================================================== resolution rules

SOURCES = ("binance", "chainlink", "coinbase", "kraken", "bitstamp", "pyth", "coingecko", "coinmarketcap", "cme",
           "uma")
FIELDS = ("close", "high", "low", "open")
EXPECTED = {   # resolved.py's 2026 rules (its docstring, step 3), one signature per kind
    "above": {"source": "binance", "pair": "btc/usdt", "candle": "1m", "field": "close", "time": "noon", "cmp": ">"},
    "range": {"source": "binance", "pair": "btc/usdt", "candle": "1m", "field": "close", "time": "noon",
              "cmp": "tie_up"},
    "updown_day": {"source": "binance", "pair": "btc/usdt", "candle": "1m", "field": "close", "time": "noon",
                   "cmp": "up>ref,tie50", "ref": "prev_noon"},
    "hit_daily": {"source": "binance", "pair": "btc/usdt", "candle": "1m", "cmp": "touch>=", "window": "day"},
    "hit_weekly": {"source": "binance", "pair": "btc/usdt", "candle": "1m", "cmp": "touch>=", "window": "week"},
    "hit_monthly": {"source": "binance", "pair": "btc/usdt", "candle": "1m", "cmp": "touch>=", "window": "month"},
    "updown_4h": {"source": "chainlink", "pair": "btc/usd", "cmp": "up>=start"},
}
_MON3 = {m[:3]: i + 1 for i, m in enumerate(lad.MONTHS)}


def _norm(desc):
    d = str(desc or "").lower()
    for a, b in (("“", '"'), ("”", '"'), ("‘", "'"), ("’", "'"), ("–", "-"), ("—", "-")):
        d = d.replace(a, b)
    d = re.sub(r"(\d)-(minute|hour|day)", r"\1 \2", d)
    return re.sub(r"\s+", " ", d).strip()


def rule_sig(kind, desc, typ=None):
    """Resolution signature of one market description (dict of short strings; '' = not found)."""
    d = _norm(desc)
    s = {"source": "", "pair": "", "candle": "", "field": "", "time": "", "cmp": "", "window": "", "ref": ""}
    if not d:
        return s
    s["source"] = "+".join(x for x in SOURCES if re.search(rf"\b{x}\b", d) and not (x == "uma" and "binance" in d))
    pairs = []
    if re.search(r"btc[/_ -]?usdt\b", d):
        pairs.append("btc/usdt")
    if re.search(r"btc[/_ -]?usd\b", d):
        pairs.append("btc/usd")
    s["pair"] = "+".join(pairs)
    cand = set()
    for n, u in re.findall(r"\b(\d+) (minute|hour|day)s? (?:candle|kline|bar)", d):
        cand.add(f"{n}{u[0]}")
    for n, u in re.findall(r'"(\d+)(m|h|d)"', d):
        cand.add(f"{n}{u}")
    s["candle"] = "+".join(sorted(cand))
    fl = set(re.findall(r'"(close|high|low|open)"', d)) | set(re.findall(r"\b(close|high|low|open) prices?\b", d))
    s["field"] = "+".join(f for f in FIELDS if f in fl)
    if "12:00 in the et timezone (noon)" in d:
        s["time"] = "noon"
    else:
        m = re.search(r"\b(\d{1,2}:\d\d) ?(am|pm)? (?:in the )?(et|utc)\b", d)
        s["time"] = " ".join(x for x in m.groups() if x) if m else ""
    if kind == "above":
        if re.search(r"higher than or equal|equal to or (?:higher|greater)|at or above", d):
            s["cmp"] = ">="
        elif re.search(r"(?:higher|greater) than the price", d):
            s["cmp"] = ">"
    elif kind == "range":
        s["cmp"] = "tie_up" if "higher range bracket" in d else ("tie_down" if "lower range bracket" in d else "")
    elif kind == "updown_day":
        up = re.search(r'resolve to "up" if the "close" price for the binance 1 minute candle for btc/usdt (.+?) is lower '
                       r'than the final "close" price for the (.+?) candle', d)
        s["cmp"] = ("up>ref" if up else "") + (",tie50" if "50-50" in d else "")
        # 'Sep 14 '25 12:00' and '3 Nov '25 12:00' both occur
        dates = [(a or d_, b_ or c_, y) for a, b_, c_, d_, y in
                 re.findall(r"\b(?:([a-z]{3})[a-z]* (\d{1,2})|(\d{1,2}) ([a-z]{3})[a-z]*) '(\d\d) 12:00\b", d)]
        if up and len(dates) >= 2:
            try:
                (m1, d1, y1), (m2, d2, y2) = dates[0], dates[1]
                a = date(2000 + int(y1), _MON3[m1[:3]], int(d1))
                b = date(2000 + int(y2), _MON3[m2[:3]], int(d2))
                s["ref"] = "prev_noon" if (b - a).days == 1 else f"gap{(b - a).days}d"
                s["ref_day"] = b.isoformat()
            except (KeyError, ValueError):
                s["ref"] = "?"
    elif kind in HIT_KINDS or kind == "hit_other":
        hi = re.search(r'"?high"? price (?:is )?equal to or (?:greater|higher) than', d)
        lo = re.search(r'"?low"? price (?:is )?equal to or (?:lower|less) than', d)
        both = re.search(r"price equal to or beyond \(above for .{0,4}high prices?, below for .{0,4}low prices?\)", d)
        s["cmp"] = "touch>=" if (hi or lo or both) else ""
        s["dir"] = "by_title" if both else ("up" if hi and not lo else ("down" if lo and not hi else ""))
        w = []
        if "on the date specified in the title" in d and "between 12:00 am et and 11:59 pm et" in d:
            w.append("day")
        if "date range specified in the title" in d and "11:59 pm et on the last" in d:
            w.append("week")
        if "month specified in the title" in d:
            w.append("month")
        s["window"] = "+".join(w) + ("+creation" if "creation of this market" in d else "")
        if not w:
            m = re.search(r"between (.{3,40}?) and (.{3,40}?)(?:,? has\b|,? in the et\b| \()", d)
            if m:
                s["window"] = f"{m.group(1)}..{m.group(2)}"
    elif kind == CONTROL:
        s["cmp"] = "twap>=start" if ("time-weighted average" in d or "twap" in d) else \
            "up>=start" if re.search(r"end of the time range .* greater than or equal to the price at the "
                                            r"beginning", d) else ""
    return s


def sig_text(s):
    keys = ("source", "pair", "candle", "field", "time", "cmp", "window", "ref")
    return " ".join(f"{s[k]}" for k in keys if s.get(k)) or "(none)"


def rule_reason(kind, sig, typ=None, day=None):
    """'' if the signature is the kind's 2026 rule, else the features that differ ('feature=value')."""
    exp = EXPECTED.get(kind)
    if exp is None:
        return f"window={sig.get('window') or '?'} (no such kind in resolved.py)"
    if not any(sig.get(k) for k in ("source", "pair", "candle", "field", "cmp")):
        return "no description"
    bad = []
    for k, v in exp.items():
        got = sig.get(k, "")
        if k == "window":
            base = got.replace("+creation", "")
            if base != v:
                bad.append(f"window={got or '?'}")
        elif got != v:
            bad.append(f"{k}={got or '?'}")
    if kind in HIT_KINDS:
        want_field = {"hit_up": "high", "hit_down": "low"}.get(typ)
        by_title = sig.get("dir") == "by_title" and sig.get("field") == "high+low"   # 'High or Low ... beyond'
        if want_field and sig.get("field") != want_field and not by_title:
            bad.append(f"field={sig.get('field') or '?'}")
        if want_field and sig.get("dir") != ("up" if want_field == "high" else "down") and not by_title:
            bad.append(f"dir={sig.get('dir') or '?'}")
    if kind == "updown_day" and day and sig.get("ref_day") and sig["ref_day"] != str(day):
        bad.append(f"date={sig['ref_day']}")
    return ";".join(bad)


def rule_check(mk):
    """mk with 'sig' (text) and 'rule' ('' = the 2026 rule of its kind, else the differing features)."""
    mk = mk.copy()
    sigs, why = [], []
    for r in mk.itertuples(index=False):
        s = rule_sig(r.kind, r.description, getattr(r, "type", None))
        sigs.append(sig_text(s))
        why.append(rule_reason(r.kind, s, getattr(r, "type", None), getattr(r, "day", None)))
    mk["sig"], mk["rule"] = sigs, why
    return mk


def _other(bad, k=2):
    """'reason (n, e.g. slug)' for the k most common differing signatures, '+ m more' for the rest."""
    if not len(bad):
        return "–"
    vc = bad["rule"].value_counts()
    out = [f"{why} ({n}, e.g. {bad.loc[bad['rule'] == why, 'slug'].iloc[0][:60]})" for why, n in vc.iloc[:k].items()]
    if len(vc) > k:
        out.append(f"+ {len(vc) - k} more ({int(vc.iloc[k:].sum())})")
    return "; ".join(out)


def rule_table(mk, period_col="month"):
    """kind x month: events, markets, markets with the 2026 rule, other signatures (count) and a sample
    slug of each differing one."""
    rows = []
    for (k, mo), g in mk.groupby(["kind", period_col]):
        bad = g[g["rule"] != ""]
        rows.append({"kind": k, "month": mo, "events": g["event_slug"].nunique(), "markets": len(g),
                     "ok": int((g["rule"] == "").sum()),
                     "other": _other(bad)})
    return pd.DataFrame(rows)


def ref_sample_slugs(months=REF_MONTHS):
    """Slugs (kind, slug) of one 2026 event per kind and month: the 15th for daily kinds, the week that
    holds the 15th, the month itself and the 4-hour window starting at 16:00 UTC on the 15th."""
    out = []
    for y, m in months:
        d = date(y, m, 15)
        for kind, ss in rs.daily_slugs(d).items():
            out += [(kind if kind != "hit_daily" else "hit", s) for s in ss]
        mon = d - timedelta(days=d.weekday())
        out += [("hit", s) for s in rs.week_slugs(mon)]
        out += [("hit", s) for s in rs.month_slugs(y, m)]
        t = int(datetime(y, m, 15, 16, tzinfo=timezone.utc).timestamp())
        out.append(("updown_4h", f"btc-updown-4h-{t}"))
    return out


def reference_rules(http, months=REF_MONTHS):
    """2026 sample: (kind, month, slug, signature, reason) for every market of the sampled events."""
    pairs = ref_sample_slugs(months)
    kind_of = dict((s, k) for k, s in pairs)
    evs = rs.events_by_slug(http, [s for _, s in pairs])
    rows = []
    for ev in evs:
        kind = kind_of.get(ev.get("slug"))
        if kind is None:
            continue
        end = rs.ts_of(ev.get("endDate"))
        if not np.isfinite(end) or lad.et_date(int(end) - 1).year != 2026:
            continue
        d = lad.et_date(int(end) - 1) if kind in ("above", "range", "updown_day") else None
        for r in rs.market_rows(ev, kind, d):
            s = rule_sig(r["kind"], r["description"], r["type"])
            rows.append({"kind": r["kind"], "month": lad.et_date(int(end) - 1).isoformat()[:7], "slug": r["slug"],
                         "event_slug": ev.get("slug"), "sig": sig_text(s),
                         "rule": rule_reason(r["kind"], s, r["type"], r["day"])})
    return pd.DataFrame(rows)


# ===================================================================== Binance spot for this period

def kline_unit(path):
    """'us' or 'ms': the raw open-time unit of a Binance kline zip (first data row)."""
    with zipfile.ZipFile(path) as z:
        with z.open(z.namelist()[0]) as f:
            line = f.readline().decode().strip()
            if not line[:1].isdigit():
                line = f.readline().decode().strip()
    t = int(line.split(",")[0])
    return "us" if t > 10 ** 14 else "ms"


def build_spot(cache, http=None, first=SPOT_FIRST, last=SPOT_LAST, log=print):
    """(Spot of 1 s klines first .. last, missing days, {unit: n files}) - rs.build_spot for this range,
    zips in <cache>/klines, arrays cached as <cache>/spot_1s.npz (integer cents)."""
    cache = Path(cache)
    path = cache / "spot_1s.npz"
    f = lambda a: np.where(a > 0, a.astype(float), np.nan)
    if path.exists():
        z = np.load(path)
        units = json.loads(str(z["units"])) if "units" in z else {}
        return rs.Spot(int(z["sec0"]), f(z["hi"]), f(z["lo"]), f(z["cl"])), list(z["missing"]), units
    sec0 = int(datetime(first.year, first.month, first.day, tzinfo=timezone.utc).timestamp())
    n = len(rs.days(first, last)) * 86400
    hi, lo, cl = (np.zeros(n, np.int32) for _ in range(3))
    missing, units = [], {}
    for d in rs.days(first, last):
        fz = cache / "klines" / f"BTCUSDT-1s-{d}.zip"
        if not fz.exists() and http is not None:
            try:
                http.download(rs.VISION.format(iv="1s", d=d), fz)
            except Exception as e:
                log(f"  1 s klines {d}: {type(e).__name__}: {e}")
        if not fz.exists():
            missing.append(d.isoformat())
            continue
        u = kline_unit(fz)
        units[u] = units.get(u, 0) + 1
        t, h, l, c = rs.read_kline_zip(fz)
        bad = (t < int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())) | \
              (t >= int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()) + 86400)
        if bad.any():
            raise RuntimeError(f"{fz.name}: {int(bad.sum())} open times outside the day (unit {u})")
        k = t - sec0
        hi[k], lo[k], cl[k] = h, l, c
    np.savez(path, sec0=sec0, hi=hi, lo=lo, cl=cl, missing=np.array(missing, dtype=str),
             units=np.array(json.dumps(units)))
    log(f"spot: {len(rs.days(first, last)) - len(missing)} days of 1 s klines, units {units}, missing {missing}")
    return rs.Spot(sec0, f(hi), f(lo), f(cl)), missing, units


# ===================================================================== decide / measure

def decide_checked(mk, spot):
    """rs.decide on the markets with their kind's 2026 rule only (so the noon convention is chosen from
    them); the others are kept with excluded = 'rule: <differences>'. Adds 'ref' as resolved.run does."""
    mk = rule_check(mk) if "rule" not in mk else mk
    ok = (mk["rule"] == "").to_numpy()
    if ok.any():
        dec, conv, mism = rs.decide(mk[ok].reset_index(drop=True), spot)
    else:
        dec, conv, mism = mk.iloc[:0].copy(), "open12", {c: 0 for c in rs.CONVS}
    rest = mk[~ok].copy()
    rest["excluded"] = "rule: " + rest["rule"]
    out = pd.concat([dec, rest], ignore_index=True) if len(rest) else dec
    out["ref"] = np.where(out["kind"] == "updown_day", out[f"ref_{conv}"], NAN) if f"ref_{conv}" in out else NAN
    return out, conv, mism


def measure(mk, spot, trades):
    """(checkpoints, by checkpoint, by kind, joined trades, by month) - rs.checkpoints + rs.near_table."""
    cp = rs.checkpoints(mk, spot)
    near, near_kind, j, mon = near_tables(cp, trades, mk)
    return cp, near, near_kind, j, mon


def tables(mk, spot, trades, first=FIRST, last=LAST):
    """Every table of the report that depends on trades: near-certain (by checkpoint / kind / month)
    and decided-but-unsettled (lag table, per-day dollars, per-month profit)."""
    cp, near, near_kind, j, mon = measure(mk, spot, trades)
    pt = _call(rs.post_tstar, trades, mk, first=first, last=last)
    w = pt[pt["win"] & (pt["price"] < 1)].copy()
    return {"cp": cp, "near": near, "near_kind": near_kind, "j": j, "mon": mon, "lag": rs.lag_table(w, mk),
            "dd": _call(rs.daily_dollars, w, first=first, last=last), "ymon": yield_month(w), "robust": robustness(j),
            "ytop": yield_top(w)}


# ===================================================================== near-certain (resolved.py's tables)

def bucket_points(cp, mk):
    """Checkpoints with resolved.py's bucket and won flag (as rs.near_table sets them)."""
    cp = cp.copy()
    cp["bucket"] = np.where(cp["p_fav"] >= 0.99, REF, np.where(cp["p_fav"] >= 0.95, TEST, ""))
    cp = cp[cp["bucket"] != ""]
    off = mk.set_index("cid")["official"].reindex(cp["cid"]).to_numpy()
    cp["won"] = np.where(cp["fav_yes"], off, 1 - off)
    cp["month"] = rs.et_day(pd.Series(cp["t"].to_numpy(np.int64), index=cp.index)).str[:7]
    return cp


def stat_row(c, s):
    """One table row from checkpoints c and their joined trades s (the columns of rs.near_table)."""
    mu, se, t, G = rs.clustered_mean(s["pnl"], s["size"], s["cid"])
    mu_t, _, t_t, _ = rs.clustered_mean(s["pnl_taker"], s["size"], s["cid"])
    ts = s[~s["taker_buy"]]
    mu_s, _, t_s, G_s = rs.clustered_mean(ts["pnl"], ts["size"], ts["cid"])
    lost = lambda x: int((x.groupby("cid")["won"].min() < 1).sum()) if len(x) else 0
    W = s["size"].sum()
    return {"points": len(c), "markets": c["cid"].nunique(), "model": c["p_fav"].mean() if len(c) else NAN,
            "win_all": c["won"].mean() if len(c) else NAN,
            "pts_tr": s[["cid", "check"]].drop_duplicates().shape[0], "mk_tr": G, "trades": len(s), "shares": W,
            "win_tr": (s["won"] * s["size"]).sum() / W if W > 0 else NAN,
            "vwap": (s["price"] * s["size"]).sum() / W if W > 0 else NAN,
            "pnl": mu, "se": se, "t": t, "pnl_taker": mu_t, "t_taker": t_t, "pnl_sell": mu_s, "t_sell": t_s,
            "mk_sell": G_s, "losses_sell": lost(ts), "losses": lost(s), "loss_pts": int((c["won"] < 1).sum()),
            "exp_fail": float((1 - c["p_fav"]).sum())}


def month_table(cp, j, mk, kinds=BINANCE_KINDS):
    """Per bucket x ET month of the checkpoint (all checkpoints pooled, these kinds), the same row as
    rs.near_table's 'all' row, plus an 'all' month."""
    cpb = bucket_points(cp, mk)
    cpb = cpb[cpb["kind"].isin(kinds)]
    jj = j[j["kind"].isin(kinds)].copy() if len(j) else j
    if len(jj):
        mon = cpb.set_index(["cid", "check"])["month"]
        jj["month"] = mon.reindex(pd.MultiIndex.from_frame(jj[["cid", "check"]])).to_numpy()
    rows = []
    for b in (TEST, REF):
        cb = cpb[cpb["bucket"] == b]
        jb = jj[jj["bucket"] == b] if len(jj) else jj
        for mo in sorted(cb["month"].unique()) + ["all"]:
            c = cb if mo == "all" else cb[cb["month"] == mo]
            s = jb if mo == "all" else jb[jb["month"] == mo]
            rows.append({"bucket": b, "month": mo, **stat_row(c, s)})
    return pd.DataFrame(rows)


def near_tables(cp, trades, mk):
    """(by checkpoint, by kind, joined trades, by month) - the first three are rs.near_table's own."""
    near, near_kind, j = rs.near_table(cp, trades, mk)
    mon = month_table(cp, j, mk) if len(cp) else pd.DataFrame()
    return near, near_kind, j, mon


def robustness(j, bucket=TEST, kinds=BINANCE_KINDS, top=10):
    """Descriptions only (not the verdict): the bucket's pooled pnl with markets equally weighted (t of
    the per-market means), without the `top` markets with the most shares, and clustered by ET day of
    the checkpoint (strikes of one day share one BTC path)."""
    s = j[(j["bucket"] == bucket) & j["kind"].isin(kinds)] if len(j) else j
    if not len(s):
        return {}
    g = s.assign(w=s["pnl"] * s["size"]).groupby("cid").agg(w=("w", "sum"), sh=("size", "sum"))
    per = g["w"] / g["sh"]
    n = len(per)
    eq_t = per.mean() / (per.std(ddof=1) / math.sqrt(n)) if n > 1 and per.std(ddof=1) > 0 else NAN
    rest = s[~s["cid"].isin(set(g["sh"].sort_values(ascending=False).index[:top]))]
    mu_x, _, t_x, G_x = rs.clustered_mean(rest["pnl"], rest["size"], rest["cid"])
    day = rs.et_day(pd.Series(s["t"].to_numpy(np.int64), index=s.index)).to_numpy()
    mu_d, _, t_d, G_d = rs.clustered_mean(s["pnl"], s["size"], day)
    return {"eq": per.mean(), "eq_t": eq_t, "eq_med": per.median(), "n": n,
            "top_share": g["sh"].sort_values(ascending=False).iloc[:top].sum() / g["sh"].sum(),
            "ex_top": mu_x, "ex_top_t": t_x, "day_t": t_d, "days": G_d}


def verdict(near, min_markets=MIN_MARKETS, min_t=MIN_T):
    """(passed, row, text) for NEARCERT.md test 1 on the pooled [0.95, 0.99) row."""
    a = near[(near["bucket"] == TEST) & (near["group"] == "all")] if len(near) else pd.DataFrame()
    if not len(a):
        return False, None, "没有 [0.95, 0.99) 的时点 → 不通过"
    r = a.iloc[0]
    ok_mu = bool(np.isfinite(r["pnl"]) and r["pnl"] > 0)
    ok_t = bool(np.isfinite(r["t"]) and r["t"] >= min_t)
    ok_n = int(r["mk_tr"]) >= min_markets
    passed = ok_mu and ok_t and ok_n
    why = [x for x, ok in ((f"每份 {r['pnl']:+.5f} ≤ 0", ok_mu), (f"t = {r['t']:.2f} < {min_t:g}", ok_t),
                           (f"有成交市场 {int(r['mk_tr'])} < {min_markets}", ok_n)) if not ok]
    text = (f"[0.95, 0.99)，五个时点合并，币安结算类型：每份 {r['pnl']:+.5f}（{100 * r['pnl']:+.2f}¢），"
            f"t = {r['t']:.2f}（按市场聚类），有成交市场 {int(r['mk_tr'])} 个 → "
            + ("**通过**（每份 > 0、t ≥ 2、≥ 200 个市场）" if passed else "**不通过**（" + "；".join(why) + "）"))
    if passed and not (np.isfinite(r["pnl_taker"]) and r["pnl_taker"] > 0):
        text += f"；但按吃单扣费后每份 {r['pnl_taker']:+.5f}"
    return passed, r, text


# ===================================================================== report

_f = rs._f


def _t(t, G, losses):
    tc = getattr(rs, "t_cell", None)
    if tc is not None:
        return tc(t, G, losses)
    return _f(t, 2)


NEAR_COLS = ["bucket", "group", "points", "model", "win_all", "loss_pts", "exp_fail", "pts_tr", "mk_tr", "trades",
             "shares", "vwap", "win_tr", "losses", "pnl", "t_s", "pnl_taker", "tt_s", "mk_sell", "pnl_sell", "ts_s"]
NEAR_HEADS = ["模型", "分钟前/类型/月", "时点", "模型均值", "实际胜率", "输的时点", "模型预期输的时点", "有成交时点", "有成交市场",
              "笔数", "份数", "均价", "成交加权胜率", "有成交且输的市场", "每份盈亏", "t", "吃单每份", "t", "卖方主动市场",
              "卖方主动每份", "t"]


def near_md(tab, group_col):
    n2 = tab.copy()
    n2["group"] = n2[group_col].map(lambda k: rs.KIND_LABEL.get(k, k) if hasattr(rs, "KIND_LABEL") else k)
    n2["t_s"] = [_t(r.t, r.mk_tr, r.losses) for r in n2.itertuples()]
    n2["tt_s"] = [_t(r.t_taker, r.mk_tr, r.losses) for r in n2.itertuples()]
    n2["ts_s"] = [_t(r.t_sell, r.mk_sell, r.losses_sell) for r in n2.itertuples()]
    for c in ("model", "win_all", "win_tr", "vwap"):
        n2[c] = n2[c].map(lambda x: _f(x, 4))
    for c in ("pnl", "pnl_taker", "pnl_sell"):
        n2[c] = n2[c].map(lambda x: _f(x, 5))
    n2["shares"] = n2["shares"].map(lambda x: _f(x))
    n2["exp_fail"] = n2["exp_fail"].map(lambda x: _f(x, 1)) if "exp_fail" in n2 else "–"
    return rs.md_table(n2, NEAR_COLS, NEAR_HEADS)


def yield_month(w, kinds=BINANCE_KINDS, L=5):
    """ET month x kind: profit $ of winning-token trades at p < 1, dt >= L (any taker side), and the
    Binance-settled total, its taker-sell part and notional at p <= 0.995."""
    s = w[(w["dt"] >= L)]
    if "after_close" in s:
        s = s[~s["after_close"]]
    if not len(s):
        return pd.DataFrame()
    piv = s.groupby(["month", "kind"])["profit"].sum().unstack(fill_value=0.0)
    b = s[s["kind"].isin(kinds)]
    piv["all"] = b.groupby("month")["profit"].sum()
    piv["sell"] = b[~b["taker_buy"]].groupby("month")["profit"].sum()
    piv["n995"] = b[b["price"] <= 0.995].groupby("month")["notional"].sum()
    return piv.fillna(0.0).reset_index()


def yield_top(w, kinds=BINANCE_KINDS, L=5):
    """Markets by profit $ of winning-token trades at p < 1, dt >= L, before closedTime (Binance kinds)."""
    s = w[(w["dt"] >= L) & w["kind"].isin(kinds)]
    if "after_close" in s:
        s = s[~s["after_close"]]
    if not len(s):
        return pd.DataFrame()
    return s.groupby(["slug", "kind"]).agg(trades=("size", "size"), notional=("notional", "sum"),
                                           profit=("profit", "sum"), pmin=("price", "min")) \
        .sort_values("profit", ascending=False).reset_index()


def report(ctx, out_md):
    mk, cov, missing = ctx["mk"], ctx["cov"], ctx["missing"]
    L = ["# “九成五以上”一边的历史独立段检验（NEARCERT.md 检验 1）\n"]
    passed, r0, vtext = verdict(ctx["near"])
    L.append(f"**判定**：{vtext}。\n")
    L.append(f"数据：Gamma 已结束事件（{FIRST}–{LAST}，最后一天按 ET）、data-api 逐笔成交、币安 BTCUSDT 1 秒 K 线（"
             f"{SPOT_FIRST}–{SPOT_LAST}）和官方 1 分钟 K 线。代码 `nearcert.py`（发现、结算、模型、成交、表格都从 "
             f"`resolved.py` 导入）。本次运行 {ctx['runtime'] / 60:.1f} 分钟"
             + (f"；首次抓取：K 线 {ctx['timing_spot']['spot_s'] / 60:.0f} 分钟（{ctx['timing_spot']['downloads']} 个文件）"
                if ctx.get("timing_spot") else "")
             + (f"，成交 {ctx['timing']['total_s'] / 60:.0f} 分钟、{ctx['timing']['requests']:,} 次请求（≤ 4 次/秒）"
                if ctx.get("timing") else "") + "。\n")
    # ---------------------------------------------------------------- near-certain
    near, near_kind, mon = ctx["near"], ctx["near_kind"], ctx["mon"]
    L.append("## 几乎确定：按时点（币安结算类型合并）\n")
    L.append("和 resolved.md 同一张表：无漂移模型，σ = 过去 1 小时 1 秒对数收益标准差；该时点之后 60 秒内这一边的成交（任何主动方），"
             "按份数加权；每份盈亏 = 官方结果 − 价格（挂单无费），吃单再扣 0.07·p(1−p)；t 按市场聚类（“–”= 少于 20 个市场，"
             "“†”= 没有市场输过）。“卖方主动”= 只算 taker 卖出这一边的成交（挂单买入能接到的）。\n")
    if len(near):
        L.append(near_md(near, "check"))
        L.append("\n按类型（各时点合并；updown_4h 按 Chainlink 结算，只作对照）：\n")
        L.append(near_md(near_kind, "kind"))
    if len(mon):
        L.append("\n按月（检查时点所在 ET 月份，币安结算类型、各时点合并）：\n")
        L.append(near_md(mon, "month"))
    rb = ctx.get("robust") or {}
    if rb:
        L.append(f"\n稳健性（描述，不是判定口径）：[0.95, 0.99) 按市场等权每份 {rb['eq']:+.5f}（t {rb['eq_t']:.2f}，中位数 "
                 f"{rb['eq_med']:+.5f}，{rb['n']} 个市场）；份数最多的 10 个市场占 {rb['top_share']:.0%}，去掉后每份 {rb['ex_top']:+.5f}"
                 f"（t {rb['ex_top_t']:.2f}）；按 ET 日聚类 t {rb['day_t']:.2f}（{rb['days']} 天）。有成交且输的市场只有 {int(r0['losses']) if r0 is not None else 0} 个，"
                 f"t 主要反映价格离散；时点上模型预期输 {r0['exp_fail'] if r0 is not None else NAN:.1f} 个、实际 "
                 f"{int(r0['loss_pts']) if r0 is not None else 0} 个，尾部要靠前向检验。")
    L.append("\n对照 resolved.md（2026-03-14–09-30，同一代码，币安结算合并）：[0.95, 0.99) 每份 +0.01611，t 7.18，"
             "244 个有成交市场（吃单 +0.01497；卖方主动 +0.06211，t 5.14，97 个市场）；≥ 0.99 每份 −0.00548，t −0.59，1438 个市场。")
    # ---------------------------------------------------------------- coverage
    L.append("\n## 覆盖\n")
    c2 = cov.copy()
    c2["missing_days"] = [len(missing.get(k, [])) if k in missing else "–" for k in c2["kind"]]
    ex = mk.groupby("kind")["excluded"].agg(lambda s: int((s == "").sum()))
    rule_x = mk.groupby("kind")["excluded"].agg(lambda s: int(s.str.startswith("rule").sum()))
    c2["rule_x"] = [int(rule_x.get(k, 0)) for k in c2["kind"]]
    c2["used"] = [int(ex.get(k, 0)) for k in c2["kind"]]
    first_day = mk.groupby("kind")["day"].min()
    c2["first"] = [first_day.get(k, "–") for k in c2["kind"]]
    L.append(rs.md_table(c2, ["kind", "events", "markets", "resolved", "days", "missing_days", "first", "rule_x", "used"],
                         ["类型", "事件", "市场", "已结算", "有事件的天数", "缺的天", "最早一天", "规则不同剔除", "纳入"]))
    for k, v in missing.items():
        if v:
            L.append(f"\n- {k} 缺的日期（{len(v)}）：{', '.join(v[:12])}{' …' if len(v) > 12 else ''}")
    ex = mk[mk["excluded"] != ""]
    head = ex["excluded"].str.replace(r"^(rule: [a-z_]+)=.*$", r"\1", regex=True)
    exc = ex.groupby([ex["kind"], head]).size()
    L.append("\n- 排除：" + "；".join(f"{k} {why} {n}" for (k, why), n in exc.items()) + f"；共 {len(mk):,} 个市场。"
             + "hit_daily 2026-03-05 才开始；updown_4h 2025-10-15 才开始（冬令时按 ET 零点起算，另查了 01:00 UTC 起的 slug）。")
    notes = ctx["notes"]
    L.append(f"- 搜索：public-search {notes.get('search_pages')} 页、{notes.get('search_hit_slugs')} 个 hit slug；"
             f"标题解析不了的 hit 市场 {len(notes.get('hit_window_unparsed') or [])} 个"
             + (f"（如 {notes['hit_window_unparsed'][0]}）" if notes.get("hit_window_unparsed") else "") + "。")
    sp = ctx["spot_info"]
    L.append(f"- 1 秒 K 线：{sp['days']} 天（{SPOT_FIRST}–{SPOT_LAST}），时间戳单位 {sp['units']}（微秒，已换算），缺 {sp['missing'] or '无'}。")
    mall = ctx["mall"]
    if mall is not None and len(mall):
        ok = mall[mall["minutes"] > 0]
        L.append(f"- 官方 1 分钟 K 线逐分钟核对（{len(ok)} 天，{int(ok['minutes'].sum()):,} 分钟）：1 秒聚合最高价比 1m 高 "
                 f"{int(ok['h_wider'].sum())} 分钟、最低价更低 {int(ok['l_wider'].sum())} 分钟，最高价不一致 {int(ok['bad_h'].sum())}、"
                 f"最低价 {int(ok['bad_l'].sum())}、收盘价 {int(ok['bad_c'].sum())}；最大差 {ok['max_diff_cents'].max() / 100:.2f} 美元。"
                 f"和 resolved.py 一样，T* 和结果用 1m，1 秒只用于波动率和秒级时刻。")
    L.append(f"- 中午 K 线约定（本段独立选）：与官方不符 open12 = {ctx['mism']['open12']}，close12 = {ctx['mism']['close12']}；"
             f"采用 **{ctx['conv']}**。")
    bad = mk[mk["excluded"] == "mismatch"]
    if len(bad):
        L.append(f"- 规则算出与官方不符（已排除）{len(bad)} 个：" + ", ".join(bad["slug"].head(8)) + (" …" if len(bad) > 8 else ""))
    if "trade_info" in ctx and len(ctx["trade_info"]):
        info = ctx["trade_info"]
        L.append(f"- 成交抓取：{len(info):,} 个市场、{int(info['pages'].sum()):,} 页，改用时间窗 {int((info['windows'] > 1).sum())} 个，"
                 f"仍被截断 {int(info['truncated'].sum())} 个。")
    # ---------------------------------------------------------------- rules
    L.append("\n## 结算规则核对\n")
    L.append("每个市场的 Gamma 描述解析成“来源 交易对 K 线 字段 时刻 比较 窗口 参考”，和 resolved.py 的 2026 规则逐项比（任何一项不同即剔除）。"
             "2026 样本（每类每月一个事件，03–09 月）先验证解析：\n")
    ref = ctx["ref_rules"]
    if len(ref):
        rr = ref.groupby("kind").agg(months=("month", "nunique"), events=("event_slug", "nunique"), markets=("slug", "size"),
                                     ok=("rule", lambda s: int((s == "").sum())),
                                     sig=("sig", lambda s: s.value_counts().index[0])).reset_index()
        L.append(rs.md_table(rr, ["kind", "months", "events", "markets", "ok", "sig"],
                             ["类型", "月数", "事件", "市场", "合 2026 规则", "最常见签名"]))
        rbad = ref[ref["rule"] != ""]
        if (rbad["rule"] == "cmp=twap>=start").any():
            L.append("\n（2026 样本里 08–09 月的 4 小时涨跌已改为 Chainlink TWAP；本段的 4 小时都是“结束价 ≥ 开始价”，且只作对照。）")
        if len(rbad):
            L.append(f"\n2026 样本里签名不同的 {len(rbad)} 个：" + "；".join(f"{r.slug}: {r.rule}" for r in rbad.head(6).itertuples()))
    L.append("\n本段（每个市场都查；按类型汇总，“不同的月”= 有签名不同的市场的月份）：\n")
    rt = ctx["rule_tab"]
    rk = rt.groupby("kind").agg(first=("month", "min"), last=("month", "max"), months=("month", "nunique"),
                                events=("events", "sum"), markets=("markets", "sum"), ok=("ok", "sum")).reset_index()
    rk["span"] = [f"{a}..{b}（{n}）" for a, b, n in zip(rk["first"], rk["last"], rk["months"])]
    bad_m = rt[rt["ok"] < rt["markets"]].groupby("kind")["month"].agg(lambda x: ", ".join(x))
    rk["bad_months"] = [bad_m.get(k, "–") for k in rk["kind"]]
    rk["other"] = [_other(mk[(mk["kind"] == k) & (mk["rule"] != "")]) for k in rk["kind"]]
    L.append(rs.md_table(rk, ["kind", "span", "events", "markets", "ok", "bad_months", "other"],
                         ["类型", "月份（个数）", "事件", "市场", "合 2026 规则", "不同的月", "其他签名（数量，例）"]))
    sig_by_kind = mk.groupby("kind")["sig"].agg(lambda s: s.value_counts().index[0])
    L.append("\n本段最常见签名：" + "；".join(f"{k}: `{v}`" for k, v in sig_by_kind.items()))
    xk = ctx["excluded_kinds"]
    n_feb6 = int(((mk["kind"] == "hit_monthly") & mk["rule"].str.startswith("window=february 6")).sum())
    L.append("\n剔除的类型：" + ("；".join(f"**{k}**（{int((mk['kind'] == k).sum())} 个市场；{v}）" for k, v in xk.items())
                                 if xk else "无（本段存在的类型全部和 2026 规则相同）") + "。"
             + ("hit_other = 月度系列在 2025 年 12 月没有开月度事件，而是“What price will Bitcoin hit in 2025?”（年度，窗口从 "
                "2024-12-30 20:00 ET 或市场创建起到 2025-12-31 23:59 ET），resolved.py 没有这种窗口。" if "hit_other" in xk else "")
             + (f"另外 2026-02 月度事件里 {n_feb6} 个市场的窗口是“2 月 6 日 11:00 ET 起”（不是整月），按市场剔除。"
                if n_feb6 else ""))
    absent = [k for k in KINDS if k not in set(mk["kind"])]
    if absent:
        L.append(f"本段不存在的类型：{', '.join(absent)}。")
    if ctx.get("quotes"):
        L.append("\n描述原文（本段每类最早一个市场，节选）：\n")
        for k, (slug, q) in ctx["quotes"].items():
            L.append(f"- {k} `{slug}`：{q}")
    # ---------------------------------------------------------------- yield
    L.append("\n## 结果已定、未结算的收益（描述，和 resolved.md 同表）\n")
    lt = ctx["lag"]
    if len(lt):
        L.append(rs.lag_md(lt, "notional", "profit", "任何主动方：名义$ / 收益$"))
        L.append("")
        L.append(rs.lag_md(lt, "sell_notional", "sell_profit", "其中卖方主动（挂单买入能接到的）：名义$ / 收益$"))
        dd = ctx["dd"]
        L.append(f"\n每天（{len(dd)} 天，T*+5 秒、p ≤ 0.995、币安结算类型、结算之前）：名义$ 平均 {dd['notional'].mean():,.1f}、"
                 f"中位数 {dd['notional'].median():,.1f}，≥ $50 的天 {int((dd['notional'] >= 50).sum())}；收益$ 平均 {dd['profit'].mean():,.2f}、"
                 f"中位数 {dd['profit'].median():,.2f}。"
                 f"卖方主动：名义$ 平均 {dd['sell_notional'].mean():,.1f}、中位数 {dd['sell_notional'].median():,.1f}。"
                 f"resolved.md（2026-03-14–09-30，201 天，同一口径）：名义平均 $1,315.6、中位数 $23.6，≥ $50 的天 91；"
                 f"收益平均 $38.66；卖方主动平均 $759.7、中位数 $8.8。")
        yt = ctx.get("ytop")
        if yt is not None and len(yt):
            tot = yt["profit"].sum()
            L.append(f"\n收益集中（T*+5 秒、结算前、币安结算）：共 ${tot:,.0f}，最多的 10 个市场占 {yt['profit'].head(10).sum() / tot:.0%}；"
                     + "；".join(f"{r.slug} ${r.profit:,.0f}（最低价 {r.pmin:.3f}）" for r in yt.head(3).itertuples()) + "。")
        ym = ctx["ymon"]
        if len(ym):
            y2 = ym.copy()
            cols = ["month"] + [k for k in KINDS if k in y2.columns] + ["all", "sell", "n995"]
            for c in cols[1:]:
                y2[c] = y2[c].map(lambda x: _f(x))
            L.append("\n按月收益$（T*+5 秒，任何主动方；all = 币安结算类型，sell = 其中卖方主动，n995 = p ≤ 0.995 的名义$）：\n")
            L.append(rs.md_table(y2, cols))
    L.append(f"\n## 判定（NEARCERT.md 检验 1）\n\n- {vtext}。")
    if r0 is not None:
        L.append(f"- 同一行：吃单每份 {r0['pnl_taker']:+.5f}（t {_f(r0['t_taker'], 2)}），卖方主动每份 {_f(r0['pnl_sell'], 5)}"
                 f"（t {_f(r0['t_sell'], 2)}，{int(r0['mk_sell'])} 个市场）；成交加权胜率 {r0['win_tr']:.4f}、均价 {r0['vwap']:.4f}；"
                 f"有成交且输的市场 {int(r0['losses'])}。")
    ref_row = near[(near["bucket"] == REF) & (near["group"] == "all")] if len(near) else pd.DataFrame()
    if len(ref_row):
        q = ref_row.iloc[0]
        L.append(f"- 参照 ≥ 0.99：每份 {q['pnl']:+.5f}，t {_f(q['t'], 2)}，{int(q['mk_tr'])} 个市场。")
    L.append("- 前向检验（检验 2）另做；两者都过才考虑小额实盘，由用户决定。")
    Path(out_md).write_text("\n".join(L) + "\n")
    return "\n".join(L)


# ===================================================================== main

def run(args, log=print):
    t0 = time.time()
    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)
    http = rs.Http(cache, rate=args.rate)
    ctx = {}
    mk_path = cache / "markets.parquet"
    if mk_path.exists() and not args.refresh:
        mk = pd.read_parquet(mk_path)
        notes = json.loads((cache / "discover_notes.json").read_text())
    else:
        mk, notes = discover(http, log=log)
        mk.to_parquet(mk_path)
        (cache / "discover_notes.json").write_text(json.dumps(notes, indent=1))
    cov, missing = _call(rs.coverage, mk, first=FIRST, last=LAST)
    log(cov.to_string(index=False))
    if args.step == "discover":
        return
    # ------------------------------------------------ rules
    mk = rule_check(mk)
    mk["month"] = mk["day"].str[:7]
    ref_path = cache / "ref_rules.parquet"
    if ref_path.exists():
        ref = pd.read_parquet(ref_path)
    else:
        ref = reference_rules(http)
        ref.to_parquet(ref_path)
    log(f"2026 sample: {len(ref)} markets, {int((ref['rule'] != '').sum())} with another signature")
    rule_tab = rule_table(mk)
    log(rule_tab[rule_tab["ok"] < rule_tab["markets"]].to_string(index=False))
    excluded_kinds = {}
    for k, g in mk.groupby("kind"):
        if (g["rule"] != "").all():
            excluded_kinds[k] = g["rule"].value_counts().index[0]
    quotes = {}
    for k, g in mk.sort_values("end").groupby("kind"):
        r = g.iloc[0]
        quotes[k] = (r["slug"], re.sub(r"\s+", " ", str(r["description"]))[:260] + "…")
    if args.step == "rules":
        return
    # ------------------------------------------------ spot
    t_spot, calls_spot = time.time(), http.calls
    spot, spot_missing, units = build_spot(cache, http, log=log)
    mall_path = cache / "minute_check_all.parquet"
    mall = rs.attach_1m(spot, http, cache, first=SPOT_FIRST, last=SPOT_LAST, log=log)
    if not mall_path.exists():
        mall.to_parquet(mall_path)
    log(f"1m all days: {int(mall['minutes'].sum())} minutes, bad H/L/C {int(mall['bad_h'].clip(lower=0).sum())}/"
        f"{int(mall['bad_l'].clip(lower=0).sum())}/{int(mall['bad_c'].clip(lower=0).sum())}")
    if http.calls - calls_spot > 50:
        (cache / "timing_spot.json").write_text(json.dumps({"spot_s": time.time() - t_spot,
                                                            "downloads": http.calls - calls_spot}))
    if args.step == "spot":
        return
    # ------------------------------------------------ decide (rule-checked markets only)
    mk, conv, mism = decide_checked(mk, spot)
    if hasattr(rs, "risk_flags"):
        mk["risk"] = rs.risk_flags(mk)
    log(f"noon convention mismatches {mism}; using {conv}; excluded {mk['excluded'].value_counts().to_dict()}")
    mk.to_parquet(cache / "decided.parquet")
    if args.step == "decide":
        return
    # ------------------------------------------------ trades
    calls0 = http.calls
    trades, info = rs.fetch_all_trades(http, mk, cache, workers=args.workers, log=log)
    tj = cache / "timing.json"
    if http.calls - calls0 > 100:
        tj.write_text(json.dumps({"total_s": time.time() - t0, "requests": http.calls}))
    trades = trades[trades["cid"].isin(set(mk["cid"]))]
    if args.step == "trades":
        return
    # ------------------------------------------------ measure
    tabs = tables(mk, spot, trades)
    tabs["cp"].to_parquet(cache / "checkpoints.parquet")
    ctx.update(tabs)
    ctx.update(mk=mk, cov=cov, missing=missing, notes=notes, ref_rules=ref, rule_tab=rule_tab,
               excluded_kinds=excluded_kinds, quotes=quotes, conv=conv, mism=mism, mall=mall,
               spot_info={"days": len(rs.days(SPOT_FIRST, SPOT_LAST)) - len(spot_missing), "units": units,
                          "missing": spot_missing},
               trade_info=info[info["cid"].isin(set(mk["cid"]))] if len(info) else info,
               timing=json.loads(tj.read_text()) if tj.exists() else None,
               timing_spot=json.loads((cache / "timing_spot.json").read_text())
               if (cache / "timing_spot.json").exists() else None)
    ctx["runtime"] = time.time() - t0
    text = report(ctx, args.out)
    log(text)
    log(f"runtime {ctx['runtime']:.0f} s; requests {http.calls:,} (cache hits {http.hits:,})")
    return ctx


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", nargs="?", default="all",
                    choices=["all", "discover", "rules", "spot", "decide", "trades", "report"])
    ap.add_argument("--cache", default=str(CACHE))
    ap.add_argument("--out", default=str(HERE / "real" / "nearcert-history.md"))
    ap.add_argument("--rate", type=float, default=RATE)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--refresh", action="store_true", help="rediscover markets")
    args = ap.parse_args(argv)
    run(args)


if __name__ == "__main__":
    main()
