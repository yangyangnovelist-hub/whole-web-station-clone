"""Structural arbitrage and liquidity-reward economics on the undelayed BTC markets that ladder.py
records (paper research on market data only: no keys, no orders).

    python ladder_arb.py DIR [DIR ...] [--out real/ladder-arb.md] [--cache DIR] [--offline]

DIR is a built directory (ladder.py build) or any directory above some; each recording is analysed
on its own (its own books, trades, Binance prints and CLOB disconnects), then pooled. When the
recording's raw CLOB messages sit beside it (DIR/../raw/*/clob-btc.jsonl*), the reward part reads
the full depth from them; otherwise it sees the top of book only.

Settlement, from the Gamma descriptions (read 2026-10-04):
- above K: Yes iff the Binance BTC/USDT 1m candle "12:00 in the ET timezone" (opening at the
  market's end, noon ET) has a final Close strictly higher than K;
- range: the same Close in [a, b) ("if the reported value falls exactly between two brackets ...
  the higher range bracket"); "<b" is (-inf, b) and ">a" is [a, inf); neg-risk event;
- hit_up K / hit_down L: Yes iff any 1m candle of the window ("on the date ..., between 12:00 AM ET
  and 11:59 PM ET", i.e. candles opening in [start_ts, end_ts)) has a final High >= K / Low <= L;
- updown_day: Up iff the Close of the candle *closing* at noon ET (opening 11:59) is above the one
  closing at noon the day before (the reference), 50-50 if equal: not the above / range candle;
- updown_4h settles on Chainlink's 60 s TWAP and is left out of every structural relation.

Baskets. Each candidate is a set of tokens bought at their best asks whose payoff is at least `pay`
on every outcome; it is an arbitrage when pay - sum(ask + fee) > 0, fee = feeRate p (1 - p) per
share (taker; docs.polymarket.com/trading/fees). A sold leg is written as the bought complement (the
No book mirrors the Yes book: ask_No = 1 - bid_Yes, same size).
- pairs on one candle (above, range, updown_day with its reference known): {A, B} where the payoffs
  satisfy f_A(x) + f_B(x) >= 1 for every close x (checked on every strike / bound and between them),
  e.g. above monotonicity Yes(K1) + No(K2), K1 < K2; range-in-above; disjoint ranges;
- candle vs hit: a hit market whose window contains the candle [c, c + 60): its payoff given the
  close x is at least 1{x >= K} (hit_up Yes; the candle's High >= Close) or 1{x <= L} (hit_down
  Yes; Low <= Close), and at least 0 for No; {A, H} qualifies when f_A(x) + low_H(x) >= 1, e.g.
  No(above K) + Yes(hit_up K'), K' <= K: "above K at noon" implies "hit K' that day" exactly when
  the day window contains the noon candle (checked per pair);
- hit nesting: hit_up(K2, W2) implies hit_up(K1, W1) when K1 <= K2 and W2 within W1 -> No(K2) +
  Yes(K1); hit_down mirrored (No of the lower level + Yes of the higher one);
- neg-risk range sums: all Yes (exactly one pays: pay 1) and all No (pay N - 1: hold to settlement,
  or convert the N No through the Neg Risk Adapter, which pays N - 1 USDC at once); "sell every Yes
  at its bid" is the same trade on the mirrored No books, so it is counted as the all-No basket and
  needs no split; only when every bracket of the event is in the recording;
- boxes: No(above a) + Yes([a, b)) + Yes(above b) pays 1 and Yes(above a) + No([a, b)) +
  No(above b) pays 2, except when the Close lands exactly on a or b (cents; flagged "boundary").
Evaluation at every book update of a leg (exchange time), using every leg's latest row at or
before it; valid only while each leg's row is at most MAX_AGE s old (the recorder writes a row when
the top of book changes, so a quiet leg ages out; the report also gives "feed alive only"), the feed
is alive (a row of any token within QUIET s, no CLOB disconnect since the oldest leg row), every
market is open (before its end; a hit market before Binance first prints its level, never if the
day's high / low had reached it before the recording). An episode is a run of instants with a
positive edge (runs separated only by invalid instants are merged); per episode: duration (time
with a positive edge), edge after fees, size (min top-of-book size over the legs), the same basket
0.5 s later, and $ captured taking min(size, 100) shares at the start (or at +0.5 s).

Liquidity rewards (an estimate; docs.polymarket.com/programs/liquidity-rewards): pools from CLOB
/rewards/markets/current (native + sponsored daily rates; snapshots are kept in the cache so a
market that has ended keeps the rate seen while it ran) and Gamma (rewardsMinSize,
rewardsMaxSpread, clobRewards). Every SAMPLE_S s: S(v, s) = ((v - s) / v)^2 for an order of at
least the min size within v cents of the size-cutoff-adjusted midpoint (here: the price where the
cumulative depth first reaches the min size, each side); Q_one = sum S * bid size (Yes book, which
holds the No asks too), Q_two = sum S * ask size; Q_min = max(min(Q1, Q2), max(Q1, Q2) / 3) if the
midpoint is in [0.10, 0.90], else min(Q1, Q2). Our quote: min size each side, at the widest price
that still scores ("edge"), at half the max spread ("half") or joining the best bid / ask ("touch");
every other resting order counted as one maker. Reward per sample = daily rate / 1440 * our share.
Fills: recorded last-trade prints (taker side) on either token, mapped to the Yes book, at or
through our price; markouts at +30 s / +120 s (mid; a missing side as 0 / 1) and to settlement
(CLOB /markets winner), plus the maker rebate estimate (20% of crypto taker fees, fee-curve
weighted).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

import binary as bo
import ladder as ld
import paper_trader as pt
import recording as rc

MAX_AGE = 5.0          # s: a leg's row older than this is stale
QUIET = 5.0            # s: no row of any token for longer -> the feed may be dead
LATER = 0.5            # s: is the basket still there?
TAKE = 100.0           # shares taken per episode at most
EPS = 1e-9
CLOSE_TYPES = ("above", "range", "updown_day")
HIT_TYPES = ("hit_up", "hit_down")
SAMPLE_S, SAMPLE_OFF = 60.0, 30.0   # reward samples: once a minute (Polymarket: random offset)
MARKOUTS = (30.0, 120.0)
REBATE = 0.20          # crypto maker rebate share of taker fees
C_SINGLE = 3.0         # single-sided scaling factor
MID_BAND = (0.10, 0.90)
QUOTES = ("edge", "half", "touch")
TTL_OPEN = 6 * 3600    # s: refetch a response about an open market after this
FEE = bo.CRYPTO_FEE_RATE
UA = "polymarket-ladder-arb-research/1.0 (market data only)"
GAMMA_MARKETS = "https://gamma-api.polymarket.com/markets?{q}"
REWARDS_CURRENT = "https://clob.polymarket.com/rewards/markets/current?next_cursor={cur}"
CLOB_MARKET = "https://clob.polymarket.com/markets/{cid}"
DOC_REWARDS = "https://docs.polymarket.com/programs/liquidity-rewards"

TYPE_ORDER = {t: i for i, t in enumerate(ld.TYPES)}
KIND_NAMES = {
    "above-above": "above 单调（同日 Yes(K1)+No(K2)）", "range-range": "range 两两互斥",
    "above-range": "range ⊂ above / 互斥", "above-hit_up": "above × hit_up（正午 K 线在当天窗口内）",
    "above-hit_down": "above × hit_down", "range-hit_up": "range × hit_up", "range-hit_down": "range × hit_down",
    "hit_up-updown_day": "updown_day × hit_up", "hit_down-updown_day": "updown_day × hit_down",
    "above-updown_day": "above × updown_day", "range-updown_day": "range × updown_day",
    "hit_up-hit_up": "hit_up 嵌套（买低价位 Yes + 高价位 No）", "hit_down-hit_down": "hit_down 嵌套（买高价位 Yes + 低价位 No）",
    "range_sum_yes": "range 全买 Yes（付 1）", "range_sum_no": "range 全买 No（付 N−1）",
    "box_long": "box：No(above a)+Yes[a,b)+Yes(above b)（付 1）",
    "box_short": "box：Yes(above a)+No[a,b)+No(above b)（付 2）"}
TYPE_NAMES = {"above": "above", "range": "range", "hit_up": "hit_up", "hit_down": "hit_down",
              "updown_day": "updown_day", "updown_4h": "updown_4h"}


def _fin(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return np.nan
    return x


def taker_fee(p, rate=FEE):
    p = np.asarray(p, float)
    return rate * p * (1.0 - p)


# ------------------------------------------------------------------ markets

def prep_markets(markets, spot=None):
    """Market dicts with what the baskets need: candle (close-type: open time of the settlement
    candle), window (hit), touch (hit: first Binance print at / beyond the level inside the window,
    -inf if the day's 1m high / low had reached it before the recording), ref (updown_day) and the
    span [t_from, t_to) in which the market can be traded for a basket."""
    rows = markets.to_dict("records") if isinstance(markets, pd.DataFrame) else list(markets)
    ts = spot["trade_ts"].to_numpy(float) if spot is not None and len(spot) else np.array([])
    px = spot["price"].to_numpy(float) if spot is not None and len(spot) else np.array([])
    out = []
    for m in rows:
        m = dict(m)
        typ = m.get("type")
        if typ not in ld.TYPES or not m.get("yes_token") or not m.get("no_token"):
            continue
        m["yes_token"], m["no_token"] = str(m["yes_token"]), str(m["no_token"])
        for c in ("lo", "hi", "end_ts", "start_ts", "ref_ts", "ref_price", "tick", "pre_high", "pre_low"):
            m[c] = _fin(m.get(c))
        fr = _fin(m.get("fee_rate"))
        m["fee"] = fr if np.isfinite(fr) else FEE
        m["t_from"], m["t_to"] = -np.inf, m["end_ts"] if np.isfinite(m["end_ts"]) else np.inf
        m["candle"] = m["window"] = None
        m["touch"] = np.inf
        if typ in ("above", "range"):
            m["candle"] = int(m["end_ts"]) if np.isfinite(m["end_ts"]) else None
        elif typ == "updown_day":
            ref = m["ref_price"]
            if not np.isfinite(ref) and len(ts) and np.isfinite(m["ref_ts"]):
                i = np.searchsorted(ts, m["ref_ts"], "left") - 1   # close of the candle closing at ref_ts
                ref = float(px[i]) if i >= 0 and m["ref_ts"] - ts[i] <= 5.0 else np.nan
            m["ref"] = ref
            if np.isfinite(ref) and np.isfinite(m["end_ts"]):
                m["candle"] = int(m["end_ts"]) - 60
                m["t_from"] = m["ref_ts"] if np.isfinite(m["ref_ts"]) else -np.inf
        elif typ in HIT_TYPES:
            start = m["start_ts"] if np.isfinite(m["start_ts"]) else -np.inf
            # an unknown start contains no candle and nests only inside its own event (same window)
            m["window"] = (start if np.isfinite(start) else np.inf, m["end_ts"])
            pre = (m["pre_high"] >= m["lo"]) if typ == "hit_up" else (m["pre_low"] <= m["hi"])
            if bool(pre):
                m["touch"] = -np.inf
            elif len(ts):
                a, b = np.searchsorted(ts, [start, m["end_ts"]], "left")
                seg = px[a:b]
                hit = np.flatnonzero(seg >= m["lo"]) if typ == "hit_up" else np.flatnonzero(seg <= m["hi"])
                if len(hit):
                    m["touch"] = float(ts[a + hit[0]])
            m["t_to"] = min(m["t_to"], m["touch"])
        out.append(m)
    return out


# ------------------------------------------------------------------ payoffs

def payoff(m, side, x):
    """Payoff of one close-type token given the settlement candle's Close x (vectorised)."""
    x = np.asarray(x, float)
    t = m["type"]
    if t == "above":
        f = (x > m["lo"]).astype(float)
    elif t == "range":
        lo, hi = m["lo"], m["hi"]
        f = (((not np.isfinite(lo)) | (x >= lo)) & ((not np.isfinite(hi)) | (x < hi))).astype(float)
    elif t == "updown_day":
        f = (x > m["ref"]).astype(float) + 0.5 * (x == m["ref"])
    else:
        raise ValueError(t)
    return f if side == "Y" else 1.0 - f


def low_payoff(m, side, x):
    """Lowest payoff of a token over every path whose settlement candle closes at x: the payoff
    itself for a close-type token; for a hit token whose window contains that candle, 1{x >= K}
    (hit_up Yes: that candle's High >= its Close), 1{x <= L} (hit_down Yes), 0 for No."""
    x = np.asarray(x, float)
    if m["type"] in CLOSE_TYPES:
        return payoff(m, side, x)
    if side == "N":
        return np.zeros_like(x)
    return (x >= m["lo"]).astype(float) if m["type"] == "hit_up" else (x <= m["hi"]).astype(float)


def _breaks(m):
    if m["type"] == "updown_day":
        return [m["ref"]]
    return [v for v in (m["lo"], m["hi"]) if np.isfinite(v)]


def min_pay(legs):
    """(almost-sure, exact) minimum payoff of a basket of (market, side) legs settled on one candle
    (at most one hit leg, whose window must contain the candle): the minimum over closes strictly
    between / beyond every strike, and over all closes including exactly at a strike (cents)."""
    b = sorted({v for m, _ in legs for v in _breaks(m)})
    if not b:
        return np.nan, np.nan
    b = np.array(b)
    mids = np.r_[b[0] - 1.0, (b[:-1] + b[1:]) / 2, b[-1] + 1.0]
    tot = lambda x: sum(low_payoff(m, s, x) for m, s in legs)
    a, e = float(tot(mids).min()), float(tot(b).min())
    return a, min(a, e)


def contains(window, candle):
    s, e = window
    return candle is not None and np.isfinite(e) and s <= candle and candle + 60 <= e


# ------------------------------------------------------------------ baskets

@dataclass
class Basket:
    kind: str
    legs: list            # [(market dict, "Y" | "N")]
    pay: float
    boundary: bool = False
    tokens: list = field(default_factory=list)
    fees: list = field(default_factory=list)
    t_from: float = -np.inf
    t_to: float = np.inf
    settle: float = np.nan

    def __post_init__(self):
        self.tokens = [m["yes_token"] if s == "Y" else m["no_token"] for m, s in self.legs]
        self.fees = [m["fee"] for m, _ in self.legs]
        self.t_from = max(m["t_from"] for m, _ in self.legs)
        self.t_to = min(m["t_to"] for m, _ in self.legs)
        self.settle = max(m["end_ts"] for m, _ in self.legs)

    def label(self):
        return " + ".join(f"{'Yes' if s == 'Y' else 'No'}({_mname(m)})" for m, s in self.legs)


def _day(m):
    d = m.get("day")
    if isinstance(d, str) and len(d) >= 10:
        return d[5:10]
    return time.strftime("%m-%d", time.gmtime(m["end_ts"])) if np.isfinite(m["end_ts"]) else "?"


def _mname(m):
    t, d = m["type"], _day(m)
    if t == "above":
        return f"above {m['lo']:,.0f} {d}"
    if t == "range":
        if not np.isfinite(m["lo"]):
            return f"<{m['hi']:,.0f} {d}"
        if not np.isfinite(m["hi"]):
            return f"≥{m['lo']:,.0f} {d}"
        return f"[{m['lo']:,.0f}, {m['hi']:,.0f}) {d}"
    if t == "hit_up":
        return f"↑{m['lo']:,.0f} {_wname(m)}"
    if t == "hit_down":
        return f"↓{m['hi']:,.0f} {_wname(m)}"
    if t == "updown_day":
        return f"updown {d} ref {m['ref']:,.2f}"
    return t


def _wname(m):
    s, e = m["window"]
    if not np.isfinite(s) or not np.isfinite(e):
        return "…" + time.strftime("%m-%d", time.gmtime(e))
    return time.strftime("%m-%d", time.gmtime(s + 12 * 3600)) + ("" if e - s <= 90000 else "+")


def _kind(legs):
    types = sorted({m["type"] for m, _ in legs}, key=TYPE_ORDER.get)
    return "-".join(types) if len(types) == 2 else f"{types[0]}-{types[0]}"


def baskets(ms):
    """Every candidate basket over prepared markets (see the module notes), deduplicated."""
    out, seen = [], set()

    def add(kind, legs, pay, boundary=False):
        key = frozenset((m["condition_id"], s) for m, s in legs)
        if key in seen:
            return
        seen.add(key)
        out.append(Basket(kind, list(legs), float(pay), bool(boundary)))

    close = [m for m in ms if m["type"] in CLOSE_TYPES and m["candle"] is not None]
    hits = [m for m in ms if m["type"] in HIT_TYPES and m["touch"] > -np.inf]
    by_candle = {}
    for m in close:
        by_candle.setdefault(m["candle"], []).append(m)
    for group in by_candle.values():                      # pairs on one candle
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                for sa in "YN":
                    for sb in "YN":
                        legs = [(a, sa), (b, sb)]
                        al, ex = min_pay(legs)
                        if al >= 1 - EPS:
                            add(_kind(legs), legs, 1.0, ex < al - EPS)
    for h in hits:                                         # candle vs hit
        for m in close:
            if not contains(h["window"], m["candle"]):
                continue
            for sm in "YN":
                legs = [(m, sm), (h, "Y")]
                al, ex = min_pay(legs)
                if al >= 1 - EPS:
                    add(_kind(legs), legs, 1.0, ex < al - EPS)
    for a in hits:                                         # hit nesting: a implies b
        for b in hits:
            if a is b or a["type"] != b["type"]:
                continue
            (sa, ea), (sb, eb) = a["window"], b["window"]
            if not (np.isfinite(sa) and np.isfinite(sb)):
                if not (sa == sb and ea == eb and a.get("event_slug") == b.get("event_slug")):
                    continue
            elif not (sb <= sa and ea <= eb):
                continue
            if (a["type"] == "hit_up" and b["lo"] <= a["lo"]) or (a["type"] == "hit_down" and b["hi"] >= a["hi"]):
                add(_kind([(a, "N"), (b, "Y")]), [(a, "N"), (b, "Y")], 1.0)
    events = {}
    for m in close:                                        # neg-risk range sums
        if m["type"] == "range":
            events.setdefault((m.get("event_slug"), m["candle"]), []).append(m)
    for group in events.values():
        legs = [(m, "Y") for m in group]
        b = np.array(sorted({v for m in group for v in _breaks(m)}))
        if not len(b):
            continue
        x = np.r_[b, (b[:-1] + b[1:]) / 2, b[0] - 1, b[-1] + 1]
        tot = sum(payoff(m, "Y", x) for m in group)
        if np.allclose(tot, 1.0) and len(group) >= 2:      # a complete partition
            add("range_sum_yes", legs, 1.0)
            add("range_sum_no", [(m, "N") for m in group], len(group) - 1.0)
    above = {(m["candle"], m["lo"]): m for m in close if m["type"] == "above"}
    for r in close:                                        # boxes
        if r["type"] != "range" or not (np.isfinite(r["lo"]) and np.isfinite(r["hi"])):
            continue
        A, B = above.get((r["candle"], r["lo"])), above.get((r["candle"], r["hi"]))
        if A is None or B is None:
            continue
        for kind, legs in (("box_long", [(A, "N"), (r, "Y"), (B, "Y")]), ("box_short", [(A, "Y"), (r, "N"), (B, "N")])):
            al, ex = min_pay(legs)
            add(kind, legs, al, ex < al - EPS)
    return out


# ------------------------------------------------------------------ books and feed

class Books:
    """Per-token top-of-book arrays (exchange time)."""

    COLS = ("bid", "ask", "bid_size", "ask_size", "tick")

    def __init__(self, books):
        self.by = {}
        for tok, g in books.groupby("market_id", sort=False):
            g = g.sort_values("ts", kind="stable")
            a = {"ts": g["ts"].to_numpy(float)}
            for c in self.COLS:
                a[c] = g[c].to_numpy(float) if c in g else np.full(len(g), np.nan)
            a["mark"] = np.where(np.isfinite(a["bid"]) | np.isfinite(a["ask"]),
                                 (np.nan_to_num(a["bid"], nan=0.0) + np.nan_to_num(a["ask"], nan=1.0)) / 2, np.nan)
            self.by[str(tok)] = a

    def asof(self, tok, t, col):
        a = self.by.get(tok)
        t = np.asarray(t, float)
        if a is None:
            return np.full(t.shape, np.nan)
        i = np.searchsorted(a["ts"], t, "right") - 1
        return np.where(i >= 0, a[col][np.maximum(i, 0)], np.nan)


class Feed:
    """Recording span, liveness (a book row of any token within QUIET s) and CLOB disconnects."""

    def __init__(self, books, closes=None, quiet=QUIET):
        self.all_ts = np.sort(books["ts"].to_numpy(float)) if len(books) else np.array([])
        recv = books["recv"].to_numpy(float) if "recv" in books and len(books) else self.all_ts
        self.start = float(np.nanmin(recv)) if len(recv) else np.nan
        self.end = float(np.nanmax(recv)) if len(recv) else np.nan
        self.hours = (self.end - self.start) / 3600 if len(recv) else 0.0
        self.closes = np.sort(np.asarray(closes, float)) if closes is not None and len(closes) else np.array([])
        self.quiet = quiet
        gap = np.flatnonzero(np.diff(self.all_ts) > quiet)
        self.dead_from = self.all_ts[gap] + quiet + 1e-6    # instants the feed turns quiet

    def alive(self, t):
        t = np.asarray(t, float)
        i = np.searchsorted(self.all_ts, t, "right") - 1
        return (i >= 0) & (t - self.all_ts[np.maximum(i, 0)] <= self.quiet) if len(self.all_ts) else np.zeros(t.shape, bool)

    def no_close(self, since, t):
        if not len(self.closes):
            return np.ones(np.shape(t), bool)
        return np.searchsorted(self.closes, since, "right") == np.searchsorted(self.closes, t, "right")


# ------------------------------------------------------------------ scanning

def evaluate(b, arrs, times, feed, max_age=MAX_AGE):
    """valid, edge, size, cost of basket b at `times` from the legs' latest rows."""
    times = np.asarray(times, float)
    n = len(times)
    valid = np.ones(n, bool)
    cost = np.zeros(n)
    size = np.full(n, np.inf)
    oldest = times.copy()
    for a, fee in zip(arrs, b.fees):
        i = np.searchsorted(a["ts"], times, "right") - 1
        ok = i >= 0
        j = np.maximum(i, 0)
        ask = np.where(ok, a["ask"][j], np.nan)
        lts = np.where(ok, a["ts"][j], -np.inf)
        valid &= ok & np.isfinite(ask) & (times - lts <= max_age)
        cost += ask + taker_fee(ask, fee)
        size = np.fmin(size, np.where(ok, a["ask_size"][j], np.nan))
        oldest = np.minimum(oldest, lts)
    valid &= feed.alive(times) & feed.no_close(oldest, times) & (times >= b.t_from) & (times < b.t_to)
    return valid, b.pay - cost, size, cost


def scan(b, books, feed, max_age=MAX_AGE, later=LATER):
    """(summary dict, list of episode dicts) of one basket over one recording."""
    m0 = b.legs[0][0]
    summ = {"kind": b.kind, "valid_s": 0.0, "best": np.nan, "med_cost": np.nan, "label": b.label(), "n": len(b.legs),
            "pay": b.pay, "event": m0.get("event_slug"), "candle": m0.get("candle")}
    arrs = [books.by.get(t) for t in b.tokens]
    if any(a is None for a in arrs):
        return summ, []
    t0 = max(feed.start, b.t_from, max(a["ts"][0] for a in arrs))
    t1 = min(feed.end, b.t_to)
    if not t1 > t0:
        return summ, []
    pts = [np.array([t0])]
    for a in arrs:
        pts.append(a["ts"][(a["ts"] > t0) & (a["ts"] < t1)])
        if np.isfinite(max_age):
            e = a["ts"] + max_age + 1e-6
            pts.append(e[(e > t0) & (e < t1)])
    pts.append(feed.dead_from[(feed.dead_from > t0) & (feed.dead_from < t1)])
    if len(feed.closes):
        pts.append(feed.closes[(feed.closes > t0) & (feed.closes < t1)])
    times = np.unique(np.concatenate(pts))
    valid, edge, size, cost = evaluate(b, arrs, times, feed, max_age)
    dt = np.diff(np.r_[times, t1])
    summ["valid_s"] = float((dt * valid).sum())
    if valid.any():
        summ["best"] = float(edge[valid].max())
        summ["med_cost"] = float(np.median(cost[valid]))
    hold = valid & (edge > EPS)
    eps = []
    if not hold.any():
        return summ, eps
    d = np.diff(np.r_[0, hold.astype(np.int8), 0])
    starts, ends = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
    runs = [[s, e] for s, e in zip(starts, ends)]
    merged = [runs[0]]
    for s, e in runs[1:]:     # merge runs separated only by invalid instants
        ps, pe = merged[-1]
        if not (valid[pe:s] & ~hold[pe:s]).any():
            merged[-1][1] = e
        else:
            merged.append([s, e])
    for s, e in merged:
        ts = float(times[s])
        te = float(times[e]) if e < len(times) else float(t1)
        held = float((dt[s:e] * hold[s:e]).sum())
        v5, e5, z5, _ = evaluate(b, arrs, np.array([ts + later]), feed, max_age)
        ok5 = bool(v5[0] and e5[0] > EPS and ts + later < t1)
        legs_ask = [float(a["ask"][np.searchsorted(a["ts"], ts, "right") - 1]) for a in arrs]
        eps.append({"kind": b.kind, "label": b.label(), "tokens": list(b.tokens), "boundary": b.boundary,
                    "start": ts, "end": te, "censored": e >= len(times), "dur": held, "edge": float(edge[s]),
                    "peak": float(edge[s:e][hold[s:e]].max()), "size": float(size[s]), "cost": float(cost[s]),
                    "pay": b.pay, "asks": legs_ask, "later": ok5, "edge5": float(e5[0]) if ok5 else np.nan,
                    "size5": float(z5[0]) if ok5 else np.nan, "to_settle_h": (b.settle - ts) / 3600,
                    "apy": float(edge[s] / cost[s] * 8760 / max((b.settle - ts) / 3600, 1.0))
                    if b.kind != "range_sum_no" else np.nan,   # all No converts to N - 1 USDC at once
                    "take": min(float(size[s]), TAKE) * float(edge[s]) if np.isfinite(size[s]) else 0.0,
                    "take5": min(float(z5[0]), TAKE) * float(e5[0]) if ok5 and np.isfinite(z5[0]) else 0.0})
    return summ, eps


def dedupe(eps):
    """Episodes that do not reuse a token another counted episode holds at their start (one stale
    quote is taken once, whatever basket shows it)."""
    busy, keep = {}, []
    for e in sorted(eps, key=lambda e: (e["start"], -e["take"])):
        if all(busy.get(t, -np.inf) <= e["start"] for t in e["tokens"]):
            keep.append(e)
            for t in e["tokens"]:
                busy[t] = max(e["end"], e["start"] + LATER)
    return keep


SUMM_COLS = ["kind", "valid_s", "best", "med_cost", "label", "n", "pay", "event", "candle"]


def scan_all(bs, books, feed, max_age=MAX_AGE):
    summ, eps = [], []
    for b in bs:
        s, e = scan(b, books, feed, max_age)
        summ.append(s)
        eps += e
    return pd.DataFrame(summ, columns=SUMM_COLS), eps


# ------------------------------------------------------------------ reward scoring

def score(v, s):
    """Polymarket's order scoring S(v, s) = ((v - s) / v)^2 (cents), 0 at or beyond v."""
    s = np.abs(np.asarray(s, float))
    return np.where(s < v, ((v - s) / v) ** 2, 0.0)


def q_min(q1, q2, mid):
    """Two-sided minimum with the single-sided allowance inside [0.10, 0.90]."""
    inside = (mid >= MID_BAND[0] - 1e-12) & (mid <= MID_BAND[1] + 1e-12)
    return np.where(inside, np.maximum(np.minimum(q1, q2), np.maximum(q1, q2) / C_SINGLE), np.minimum(q1, q2))


def adjusted_mid(bids, asks, min_size):
    """(best bid, best ask, size-cutoff-adjusted midpoint): the price at which the cumulative
    depth first reaches min_size on each side (an assumption about Polymarket's cutoff)."""
    def cut(levels, rev):
        cum = 0.0
        for p in sorted(levels, reverse=rev):
            cum += levels[p]
            if cum >= min_size - 1e-9:
                return p
        return np.nan
    bb = max(bids) if bids else np.nan
    ba = min(asks) if asks else np.nan
    a, b = cut(bids, True), cut(asks, False)
    return bb, ba, (a + b) / 2 if np.isfinite(a) and np.isfinite(b) else np.nan


def book_q(bids, asks, mid, v, min_size):
    """(Q_one, Q_two) of every level of at least min_size within v cents of mid (each level
    counted as one maker's order)."""
    q1 = sum(float(score(v, 100 * (mid - p))) * s for p, s in bids.items() if s >= min_size - 1e-9)
    q2 = sum(float(score(v, 100 * (p - mid))) * s for p, s in asks.items() if s >= min_size - 1e-9)
    return q1, q2


def our_quote(mid, bb, ba, v, tick, how):
    """(bid, ask) of our quote around mid: 'edge' the widest prices on the tick grid strictly
    inside v cents, 'half' v / 2 away, 'touch' the best bid / ask; never marketable, NaN off
    [tick, 1 - tick]."""
    if not np.isfinite(mid):
        return np.nan, np.nan
    tick = tick if np.isfinite(tick) and tick > 0 else 0.01
    vv = v / 100.0
    if how == "edge":
        pb = (np.floor((mid - vv) / tick + 1e-6) + 1) * tick
        pa = (np.ceil((mid + vv) / tick - 1e-6) - 1) * tick
    elif how == "half":
        pb = np.floor((mid - vv / 2) / tick + 1e-6) * tick
        pa = np.ceil((mid + vv / 2) / tick - 1e-6) * tick
    elif how == "touch":
        pb, pa = bb, ba
    else:
        raise ValueError(how)
    if np.isfinite(ba) and np.isfinite(pb) and pb >= ba - 1e-9:
        pb = ba - tick
    if np.isfinite(bb) and np.isfinite(pa) and pa <= bb + 1e-9:
        pa = bb + tick
    pb = round(float(pb), 6) if np.isfinite(pb) and pb >= tick - 1e-9 else np.nan
    pa = round(float(pa), 6) if np.isfinite(pa) and pa <= 1 - tick + 1e-9 else np.nan
    return pb, pa


def quote_share(mid, bb, ba, q1o, q2o, v, min_size, tick, how):
    """(our share of the sample's Q_min, our bid, our ask)."""
    pb, pa = our_quote(mid, bb, ba, v, tick, how)
    q1 = float(score(v, 100 * (mid - pb))) * min_size if np.isfinite(pb) else 0.0
    q2 = float(score(v, 100 * (pa - mid))) * min_size if np.isfinite(pa) else 0.0
    us = float(q_min(q1, q2, mid))
    oth = float(q_min(q1o, q2o, mid))
    return (us / (us + oth) if us > 0 else 0.0), pb, pa


def depth_samples(src, track, t_from, t_to, step=SAMPLE_S, off=SAMPLE_OFF):
    """Reward samples from raw CLOB messages: rows (token, t, bid, ask, mid, q1, q2) every `step`
    s of exchange time for the Yes tokens in `track` ({token: (v, min_size)}), the full book
    before the first message stamped at or after the sample. None without raw messages."""
    files = rc.clob_files(src, "btc")
    if not files or not track or not np.isfinite(t_from) or not np.isfinite(t_to):
        return None
    ladders, rows = {}, []
    nxt = (np.floor((t_from - off) / step) + 1) * step + off
    for rec in rc.iter_jsonl(files):
        msg, recv = rec.get("msg"), rec.get("recv_ms") or 0
        for m in msg if isinstance(msg, list) else [msg]:
            if not isinstance(m, dict) or m.get("event_type") not in ("book", "price_change"):
                continue
            stamp = (int(m.get("timestamp") or 0) or int(recv)) / 1000
            while stamp >= nxt and nxt <= t_to:
                for tok, (v, ms) in track.items():
                    lad = ladders.get(tok)
                    if lad is None:
                        continue
                    bb, ba, mid = adjusted_mid(lad.bids, lad.asks, ms)
                    q1, q2 = book_q(lad.bids, lad.asks, mid, v, ms) if np.isfinite(mid) else (0.0, 0.0)
                    rows.append((tok, nxt, bb, ba, mid, q1, q2))
                nxt += step
            try:
                pt.apply_clob_message(ladders, m, recv)
            except (KeyError, ValueError, TypeError, AttributeError):
                continue
    return pd.DataFrame(rows, columns=["token", "t", "bid", "ask", "mid", "q1", "q2"])


def l1_samples(books, track, t_from, t_to, step=SAMPLE_S, off=SAMPLE_OFF):
    """The same samples from the built top of book only (deeper levels inside the max spread are
    missing, so the competition is understated)."""
    if not np.isfinite(t_from) or not np.isfinite(t_to):
        return pd.DataFrame(columns=["token", "t", "bid", "ask", "mid", "q1", "q2"])
    grid = np.arange((np.floor((t_from - off) / step) + 1) * step + off, t_to + 1e-9, step)
    rows = []
    for tok, (v, ms) in track.items():
        if tok not in books.by:
            continue
        bb, ba = books.asof(tok, grid, "bid"), books.asof(tok, grid, "ask")
        bs, az = books.asof(tok, grid, "bid_size"), books.asof(tok, grid, "ask_size")
        for t, b, a, x, y in zip(grid, bb, ba, bs, az):
            bids = {b: x} if np.isfinite(b) and np.isfinite(x) else {}
            asks = {a: y} if np.isfinite(a) and np.isfinite(y) else {}
            b0, a0, mid = adjusted_mid(bids, asks, ms)
            if not np.isfinite(mid) and np.isfinite(b0) and np.isfinite(a0):
                mid = (b0 + a0) / 2      # deeper levels unseen: the top-of-book mid
            q1, q2 = book_q(bids, asks, mid, v, ms) if np.isfinite(mid) else (0.0, 0.0)
            rows.append((tok, t, b0, a0, mid, q1, q2))
    return pd.DataFrame(rows, columns=["token", "t", "bid", "ask", "mid", "q1", "q2"])


def yes_trades(trades, ms):
    """Recorded prints on either token as (cid, t, Yes price, taker side on the Yes book, size),
    one per print (a No print at q is a Yes print at 1 - q, the other side)."""
    yes = {m["yes_token"]: m["condition_id"] for m in ms}
    no = {m["no_token"]: m["condition_id"] for m in ms}
    if trades is None or not len(trades):
        return pd.DataFrame(columns=["cid", "t", "p", "side", "size"])
    t = trades.copy()
    t["asset_id"] = t["asset_id"].astype(str)
    a = t[t["asset_id"].isin(yes)].assign(cid=lambda d: d["asset_id"].map(yes), p=lambda d: d["price"].astype(float),
                                          s=lambda d: d["side"].astype(str).str.upper())
    b = t[t["asset_id"].isin(no)].assign(cid=lambda d: d["asset_id"].map(no), p=lambda d: 1 - d["price"].astype(float),
                                         s=lambda d: d["side"].astype(str).str.upper().map({"BUY": "SELL", "SELL": "BUY"}))
    x = pd.concat([a, b], ignore_index=True)
    x = x.assign(t=x["ts"].astype(float), size=x["size"].astype(float), side=x["s"],
                 key=lambda d: d["t"].round(3).astype(str) + d["p"].round(4).astype(str) + d["size"].round(4).astype(str))
    x = x.drop_duplicates(["cid", "key"]).sort_values("t", kind="stable")
    return x[["cid", "t", "p", "side", "size"]].reset_index(drop=True)


def maker_fills(m, prm, books, feed, tr, how, settle=np.nan):
    """Fills of our quote (`how`) in market m from its Yes-book prints `tr`: a taker SELL at or
    below our bid, a taker BUY at or above our ask; markouts per share (+30 s, +120 s, settlement)
    and the rebate estimate."""
    v, size, tick = prm["max_spread"], prm["min_size"], m["tick"]
    rows = []
    tok = m["yes_token"]
    if tok not in books.by or not len(tr):
        return rows
    touched = m["type"] in HIT_TYPES and np.isfinite(m["touch"]) and m["touch"] <= feed.end
    if touched and not np.isfinite(settle):
        settle = 1.0          # Binance reached the level: the hit market resolves Yes
    for t, p, side, q in zip(tr["t"].to_numpy(float), tr["p"].to_numpy(float), tr["side"], tr["size"].to_numpy(float)):
        if not (t < m["t_to"] and t >= feed.start and bool(feed.alive([t])[0])):
            continue
        bb, ba = float(books.asof(tok, t - 1e-6, "bid")), float(books.asof(tok, t - 1e-6, "ask"))
        if not (np.isfinite(bb) and np.isfinite(ba)):
            continue
        pb, pa = our_quote((bb + ba) / 2, bb, ba, v, tick, how)
        if side == "SELL" and np.isfinite(pb) and p <= pb + 1e-9:
            px, sign = pb, 1.0
        elif side == "BUY" and np.isfinite(pa) and p >= pa - 1e-9:
            px, sign = pa, -1.0
        else:
            continue
        r = {"cid": m["condition_id"], "type": m["type"], "t": t, "price": px, "dir": sign, "qty": min(q, size) if np.isfinite(q) else size,
             "rebate": REBATE * m["fee"] * px * (1 - px)}
        for h in MARKOUTS:
            th = t + h
            if th < m["t_to"] and th <= feed.end:
                mk = float(books.asof(tok, th, "mark"))
            elif (th >= m["end_ts"] or (touched and th >= m["touch"])) and np.isfinite(settle):
                mk = settle
            else:
                mk = np.nan
            r[f"m{h:g}"] = sign * (mk - px)
        r["msettle"] = sign * (settle - px) if np.isfinite(settle) else np.nan
        rows.append(r)
    return rows


# ------------------------------------------------------------------ fetching (cached, polite)

def _open(url, timeout=20):
    cafile = os.environ.get("SSL_CERT_FILE")
    if not (cafile and os.path.exists(cafile)):
        try:
            import certifi
            cafile = certifi.where()
        except ImportError:  # pragma: no cover
            cafile = None
    ctx = ssl.create_default_context(cafile=cafile)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return json.loads(r.read().decode())


class Fetcher:
    """GET JSON with a file cache (one file per URL), at most one request per `gap` s, retries
    with exponential backoff on 429 / 5xx / network errors. `ttl` None keeps a response forever;
    `final(data)` marks a cached response that never needs refetching (a resolved market)."""

    def __init__(self, cache, offline=False, gap=0.25, retries=4, opener=_open, sleep=time.sleep):
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.offline, self.gap, self.retries, self.opener, self.sleep = offline, gap, retries, opener, sleep
        self.last = 0.0
        self.requests = 0
        self.at = None   # fetch time of the last response returned

    def path(self, url):
        return self.cache / f"{hashlib.sha1(url.encode()).hexdigest()[:24]}.json"

    def get(self, url, ttl=None, final=None):
        f = self.path(url)
        old = None
        if f.exists():
            try:
                old = json.loads(f.read_text())
            except ValueError:
                old = None
        if old is not None and (self.offline or ttl is None or time.time() - old["at"] < ttl
                                or (final is not None and final(old["data"]))):
            self.at = old["at"]
            return old["data"]
        if self.offline:
            raise LookupError(f"offline and not cached: {url}")
        err = None
        for k in range(self.retries):
            wait = self.gap - (time.monotonic() - self.last)
            if wait > 0:
                self.sleep(wait)
            try:
                self.requests += 1
                data = self.opener(url)
                self.last = time.monotonic()
                tmp = f.with_suffix(".tmp")
                tmp.write_text(json.dumps({"at": time.time(), "url": url, "data": data}))
                tmp.replace(f)
                self.at = time.time()
                return data
            except urllib.error.HTTPError as e:
                err = e
                if e.code not in (429, 500, 502, 503, 504):
                    break
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                err = e
            self.last = time.monotonic()
            self.sleep(min(30.0, 2.0 ** k))
        if old is not None:
            self.at = old["at"]
            return old["data"]
        raise err if err else LookupError(url)


def _resolved(d):
    return isinstance(d, dict) and bool(d.get("closed")) and any(t.get("winner") for t in d.get("tokens") or [])


def reward_params(ms, fetcher, notes, now=None):
    """{condition id: {rate, min_size, max_spread, src}} from the reward snapshots (CLOB
    /rewards/markets/current, kept in the cache's snapshot files) and Gamma."""
    now = time.time() if now is None else now
    cids = sorted({m["condition_id"] for m in ms})
    gamma = {}
    for i in range(0, len(cids), 25):
        chunk = cids[i:i + 25]
        for closed in (None, "true"):
            need = [c for c in chunk if c not in gamma]
            if not need:
                break
            q = [("condition_ids", c) for c in need] + [("limit", "100")] + ([("closed", closed)] if closed else [])
            try:
                rows = fetcher.get(GAMMA_MARKETS.format(q=urllib.parse.urlencode(q)), ttl=TTL_OPEN)
            except Exception as e:
                notes.append(f"Gamma markets: {type(e).__name__}: {e}")
                continue
            for r in rows or []:
                if isinstance(r, dict) and r.get("conditionId"):
                    gamma[r["conditionId"]] = r
    snap_dir = fetcher.cache / "rewards-snapshots"
    snap_dir.mkdir(exist_ok=True)
    try:
        allr, cur, at = [], "", now
        for _ in range(1000):
            d = fetcher.get(REWARDS_CURRENT.format(cur=urllib.parse.quote(cur)), ttl=TTL_OPEN)
            at = min(at, fetcher.at or now)
            allr += d.get("data") or []
            cur = d.get("next_cursor") or ""
            if not cur or cur == "LTE=":
                break
        want = set(cids)
        snap = {r["condition_id"]: {k: r.get(k) for k in ("total_daily_rate", "native_daily_rate", "sponsored_daily_rate",
                                                          "rewards_min_size", "rewards_max_spread")}
                for r in allr if isinstance(r, dict) and r.get("condition_id") in want}
        (snap_dir / f"snap-{int(at)}.json").write_text(json.dumps({"at": int(at), "n_all": len(allr), "markets": snap}))
    except Exception as e:
        notes.append(f"CLOB rewards/markets/current: {type(e).__name__}: {e}（用缓存里以前的快照）")
    seen = {}
    for f in sorted(snap_dir.glob("snap-*.json")):
        try:
            s = json.loads(f.read_text())
        except ValueError:
            continue
        for cid, r in (s.get("markets") or {}).items():
            seen[cid] = (s.get("at"), r)        # the latest snapshot listing it wins
    out = {}
    for cid in cids:
        g = gamma.get(cid) or {}
        rate_g = 0.0
        for c in g.get("clobRewards") or []:
            end = c.get("endDate")
            if not end or end >= time.strftime("%Y-%m-%d", time.gmtime(now)):
                rate_g += _fin(c.get("rewardsDailyRate")) if np.isfinite(_fin(c.get("rewardsDailyRate"))) else 0.0
        at, s = seen.get(cid, (None, {}))
        rate = _fin(s.get("total_daily_rate"))
        rate = rate if np.isfinite(rate) else rate_g
        ms_ = _fin(s.get("rewards_min_size")) if s else np.nan
        ms_ = ms_ if np.isfinite(ms_) else _fin(g.get("rewardsMinSize"))
        mx = _fin(s.get("rewards_max_spread")) if s else np.nan
        mx = mx if np.isfinite(mx) else _fin(g.get("rewardsMaxSpread"))
        out[cid] = {"rate": float(rate) if np.isfinite(rate) else 0.0, "min_size": ms_, "max_spread": mx,
                    "src": "snapshot" if s else ("gamma" if g else "none"), "snap_at": at,
                    "sponsored": _fin(s.get("sponsored_daily_rate")) if s else np.nan}
    return out


def settlements(ms, cids, fetcher, notes, now=None):
    """{condition id: Yes payoff} for ended markets that CLOB /markets reports resolved."""
    now = time.time() if now is None else now
    out = {}
    by = {m["condition_id"]: m for m in ms}
    for cid in cids:
        m = by.get(cid)
        if m is None or not (m["end_ts"] < now):
            continue
        try:
            d = fetcher.get(CLOB_MARKET.format(cid=cid), ttl=TTL_OPEN, final=_resolved)
        except Exception as e:
            notes.append(f"CLOB markets/{cid[:10]}…: {type(e).__name__}: {e}")
            continue
        if not _resolved(d):
            continue
        w = {str(t.get("token_id")): bool(t.get("winner")) for t in d.get("tokens") or []}
        if m["yes_token"] in w:
            out[cid] = 1.0 if w[m["yes_token"]] else (0.5 if not any(w.values()) else 0.0)
    return out


def rewards(ms, books, feed, trades, prm, src=None, settle_fn=None):
    """(samples with our share per quote, fills, depth source) for the markets with a min size and
    a max spread. settle_fn(condition ids) -> {condition id: Yes payoff} is asked only about the
    markets our quotes traded in."""
    track, by_tok = {}, {}
    for m in ms:
        p = prm.get(m["condition_id"]) or {}
        if np.isfinite(_fin(p.get("min_size"))) and np.isfinite(_fin(p.get("max_spread"))) and p["max_spread"] > 0:
            track[m["yes_token"]] = (float(p["max_spread"]), float(p["min_size"]))
            by_tok[m["yes_token"]] = m
    raw = depth_samples(src, track, feed.start, feed.end) if src is not None else None
    full = raw is not None and len(raw) > 0
    s = raw if full else l1_samples(books, track, feed.start, feed.end)
    rows = []
    if len(s):
        alive = feed.alive(s["t"].to_numpy(float))
        for r, ok in zip(s.itertuples(index=False), alive):
            m = by_tok[r.token]
            if not (ok and np.isfinite(r.mid) and max(m["t_from"], feed.start) <= r.t < m["t_to"]):
                continue
            p = prm[m["condition_id"]]
            x = {"cid": m["condition_id"], "type": m["type"], "t": r.t, "mid": r.mid, "rate": p["rate"]}
            for how in QUOTES:
                sh, _, _ = quote_share(r.mid, r.bid, r.ask, r.q1, r.q2, p["max_spread"], p["min_size"], m["tick"], how)
                x[f"share_{how}"] = sh
                x[f"usd_{how}"] = p["rate"] / 1440.0 * sh * (SAMPLE_S / 60.0)
            rows.append(x)
    samples = pd.DataFrame(rows)
    tr = yes_trades(trades, ms)

    def sim(settle):
        out = []
        for tok, m in by_tok.items():
            sub = tr[tr["cid"] == m["condition_id"]]
            for how in QUOTES:
                out += [{**f, "how": how} for f in maker_fills(m, prm[m["condition_id"]], books, feed, sub, how,
                                                               settle.get(m["condition_id"], np.nan))]
        return out

    fills = sim({})
    if fills and settle_fn is not None:
        settle = settle_fn(sorted({f["cid"] for f in fills}))
        if settle:
            fills = sim(settle)
    return samples, pd.DataFrame(fills), "full" if full else "L1"


# ------------------------------------------------------------------ one recording

def analyze(books, markets, spot, closes=None, trades=None, src=None, prm=None, settle_fn=None,
            max_ages=(MAX_AGE, np.inf)):
    ms = prep_markets(markets, spot)
    bk, feed = Books(books), Feed(books, closes)
    bs = baskets(ms)
    res = {"markets": ms, "hours": feed.hours, "span": (feed.start, feed.end), "baskets": len(bs), "scans": {},
           "prm": prm}
    for a in max_ages:
        res["scans"][a] = scan_all(bs, bk, feed, a)
    res["windows"] = nest_windows(bs)
    if prm is not None:
        res["samples"], res["fills"], res["depth"] = rewards(ms, bk, feed, trades, prm, src, settle_fn)
    return res


def nest_windows(bs):
    """The candle / window pairs behind the candle-vs-hit baskets, for the report."""
    out = set()
    for b in bs:
        hs = [m for m, _ in b.legs if m["type"] in HIT_TYPES]
        cs = [m for m, _ in b.legs if m["type"] in CLOSE_TYPES]
        if hs and cs:
            out.add((cs[0]["type"], cs[0]["candle"], hs[0]["window"]))
    return sorted(out, key=lambda x: (x[1], x[0]))


# ------------------------------------------------------------------ report

def _t(x, fmt="%m-%d %H:%M:%S"):
    return time.strftime(fmt, time.gmtime(x)) if np.isfinite(x) else "–"


def _c(x, nd=2):
    return f"{100 * x:+.{nd}f}" if x is not None and np.isfinite(x) else "–"


def _n(x, nd=0):
    return f"{x:,.{nd}f}" if x is not None and np.isfinite(x) else "–"


def kind_table(summ, eps, hours):
    L = ["| 类型 | 篮子 | 可评估 篮子·时 | 最好边际 ¢ | 套利片段 | 时长中位 / 最长 s | 边际中位 ¢ | 份数中位 | 0.5 s 后仍在 | $/天（立即） | $/天（+0.5 s） | 距结算 h | 年化中位 |",
         "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    e = pd.DataFrame(eps)
    per_day = 24.0 / hours if hours > 0 else np.nan
    kinds = list(KIND_NAMES) + sorted(set(summ["kind"]) - set(KIND_NAMES) if len(summ) else [])
    for k in kinds:
        s = summ[summ["kind"] == k] if len(summ) else summ
        if not len(s):
            continue
        q = e[e["kind"] == k] if len(e) else e
        best = s["best"].max() if s["best"].notna().any() else np.nan
        if len(q):
            L.append(f"| {KIND_NAMES.get(k, k)} | {len(s)} | {s['valid_s'].sum() / 3600:,.1f} | {_c(best)} | {len(q)} | "
                     f"{q['dur'].median():.1f} / {q['dur'].max():.1f} | {_c(q['edge'].median())} | {_n(q['size'].median())} | "
                     f"{100 * q['later'].mean():.0f}% | {q['take'].sum() * per_day:,.2f} | {q['take5'].sum() * per_day:,.2f} | "
                     f"{q['to_settle_h'].median():.1f} | {_pct(q['apy'].median())} |")
        else:
            L.append(f"| {KIND_NAMES.get(k, k)} | {len(s)} | {s['valid_s'].sum() / 3600:,.1f} | {_c(best)} | 0 | – | – | – | – | 0 | 0 | – | – |")
    return L


def report(results, names, notes=(), note=None, max_age=MAX_AGE):
    hours = sum(r["hours"] for r in results)
    per_day = 24.0 / hours if hours > 0 else np.nan
    ms = {}
    for r in results:
        for m in r["markets"]:
            ms[m["condition_id"]] = m
    by_type = pd.Series([m["type"] for m in ms.values()]).value_counts() if ms else pd.Series(dtype=int)
    spans = [r["span"] for r in results if np.isfinite(r["span"][0])]
    when = f"{_t(min(s[0] for s in spans), '%Y-%m-%d %H:%M')} → {_t(max(s[1] for s in spans), '%Y-%m-%d %H:%M')} UTC" if spans else "–"
    strict = [r["scans"][max_age] for r in results]
    relax = [r["scans"].get(np.inf) for r in results]
    summ = pd.concat([s for s, _ in strict], ignore_index=True) if strict else pd.DataFrame(columns=SUMM_COLS)
    eps = [e for _, x in strict for e in x]
    ded = dedupe(eps)
    L = [f"# 无延迟 BTC 阶梯市场：结构性套利与流动性奖励（{time.strftime('%Y-%m-%d %H:%M', time.gmtime())} UTC）", ""]
    if note:
        L += [f"> {note}", ""]
    if hours < 1.0:
        L += [f"> 样本只有 {hours:.2f} 小时：数字只说明程序能跑通，不能外推；正式结果由 CI（polymarket-ladder-arb）在 5.5 小时的录制上生成。", ""]
    L += [f"**样本**：录制 {len(results)} 段（{', '.join(names[:8])}{' …' if len(names) > 8 else ''}），共 {hours:.2f} 小时（{when}）；市场 {len(ms)} 个（"
          + "，".join(f"{t} {by_type.get(t, 0)}" for t in ld.TYPES if by_type.get(t, 0)) + f"）；候选篮子 {sum(r['baskets'] for r in results):,} 个。"
          "只用市场数据，不下单。", ""]
    L += ["## 1. 吃单套利（全部腿按卖一买入，扣 taker 费）", "",
          f"每条腿的最新盘口不超过 {max_age:g} 秒、数据流活着（任何代币 {QUIET:g} 秒内有更新、最老一条腿之后没有 CLOB 断线）、市场未结束；"
          f"边际 = 保底支付 − Σ(卖一 + 0.07·p·(1−p))；份数 = 各腿卖一量的最小值；$/天 = 每个片段开始时买 min(份数, {TAKE:g}) 份的边际之和 ÷ 小时 × 24；"
          "「+0.5 s」= 0.5 秒后同一篮子仍有正边际时按那时的价和量买。", ""]
    L += kind_table(summ, eps, hours)
    dd = pd.DataFrame(ded)
    L += ["", f"去重（同一个代币的卖一同一时间只算一次）：片段 {len(dd)} 个，"
          f"$/天 立即 {dd['take'].sum() * per_day if len(dd) else 0:,.2f}，+0.5 s {dd['take5'].sum() * per_day if len(dd) else 0:,.2f}"
          + (f"；其中 {int(dd['boundary'].sum())} 个是「边界」篮子（结算价恰好等于某个整数价位时少付）" if len(dd) and dd["boundary"].any() else "") + "。"]
    rs = [r for r in relax if r is not None]
    if rs:
        rsumm = pd.concat([s for s, _ in rs], ignore_index=True)
        reps = [e for _, x in rs for e in x]
        rd = pd.DataFrame(dedupe(reps))
        L += ["", f"敏感性：不限制单条腿的盘口年龄（录制机只在买一/卖一变动时写一行，安静的腿会「变老」但并没有错；仍要求数据流活着、没断线）："
              f"可评估 {rsumm['valid_s'].sum() / 3600:,.1f} 篮子·时（严格口径 {summ['valid_s'].sum() / 3600:,.1f}），片段 {len(reps)} 个，"
              f"去重后 {len(rd)} 个，$/天 立即 {rd['take'].sum() * per_day if len(rd) else 0:,.2f}，+0.5 s {rd['take5'].sum() * per_day if len(rd) else 0:,.2f}。"]
    if eps:
        top = sorted(eps, key=lambda e: -e["take"])[:12]
        L += ["", "最大的片段（严格口径，按立即可得 $ 排序）：", "",
              "| 开始（UTC） | 篮子 | 卖一 | 边际 ¢ | 份数 | 持续 s | +0.5 s 边际 ¢ |", "|---|---|---|---:|---:|---:|---:|"]
        for e in top:
            L.append(f"| {_t(e['start'])} | {e['label']}{' ⚠边界' if e['boundary'] else ''} | {' / '.join(f'{a:.3f}' for a in e['asks'])} | "
                     f"{_c(e['edge'])} | {_n(e['size'])} | {e['dur']:.1f}{'+' if e['censored'] else ''} | {_c(e['edge5'])} |")
    else:
        L += ["", "严格口径下没有任何正边际的片段。"]
    near = summ.dropna(subset=["best"]).sort_values("best", ascending=False).head(5) if len(summ) else summ
    if len(near):
        L += ["", "离套利最近的篮子（有效时间里的最大边际）：" + "；".join(f"{r.label} {_c(r.best)}¢" for r in near.itertuples()) + "。"]
    L += ["", "## 2. range（neg-risk）全集", "",
          "全买 Yes：恰好一个区间付 1，Σ(卖一+费) < 1 才是套利；全买 No：N 个 No 里 N−1 个付 1（也可以马上用 Neg Risk Adapter 的 convert "
          "把 N 个 No 换成 N−1 USDC），Σ(No 卖一+费) < N−1 才是套利。「按买一卖出全部 Yes」需要先 split（每份 1 USDC 拆成 Yes+No）、"
          "卖 Yes、再把 N 个 No convert 成 N−1 USDC，净得 Σ买一 − 1，与在镜像的 No 盘口上全买 No 完全相同（No 卖一 = 1 − Yes 买一），"
          "所以只按全买 No 计，不需要 split；Polymarket 上可行。", "",
          "| 事件（结算 K 线，UTC） | N | 口径 | 全买 Yes：Σ成本典型 / 最低（对 1） | 全买 No：Σ成本典型 / 最低（对 N−1） | 可评估 h |",
          "|---|---:|---|---:|---:|---:|"]
    for age, name in ((max_age, f"≤ {max_age:g} s"), (np.inf, "不限年龄")):
        parts = [r["scans"][age][0] for r in results if age in r["scans"]]
        sm = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=SUMM_COLS)
        sm = sm[sm["kind"].isin(["range_sum_yes", "range_sum_no"])]
        if not len(sm):
            continue
        last = sorted(sm["candle"].dropna().unique())[-9:]          # the newest nine events
        for (ev, candle), g in sm[sm["candle"].isin(last)].groupby(["event", "candle"], sort=True):
            cells = []
            for kind in ("range_sum_yes", "range_sum_no"):
                q = g[(g["kind"] == kind) & g["med_cost"].notna()]
                n = int(g["n"].iloc[0])
                due = 1 if kind == "range_sum_yes" else n - 1
                if len(q):   # per-recording medians, weighted by valid time; the lowest cost seen
                    typ = float(np.average(q["med_cost"], weights=q["valid_s"].clip(lower=1e-9)))
                    cells.append(f"{_n(typ, 4)} / {_n(due - q['best'].max(), 4)}" + ("" if kind == "range_sum_yes" else f"（{due}）"))
                else:
                    cells.append("–（有区间没有卖一或从未同时新鲜）")
            L.append(f"| {ev}（{_t(candle, '%m-%d %H:%M')}） | {n} | {name} | {cells[0]} | {cells[1]} | "
                     f"{g.loc[g['kind'] == 'range_sum_yes', 'valid_s'].sum() / 3600:.2f} |")
    L += _reward_section(results, hours)
    wins = sorted({w for r in results for w in r["windows"]}, key=lambda x: (x[1], x[0]))
    L += ["", "## 近似和注意事项", "",
          "- above / range 按 12:00 ET 那根 1 分钟 K 线（正午开盘、12:01 收盘）的收盘价；above 是严格大于，range 是 [a, b)（恰好落在边界算较高的区间）。"
          "box 和与 above 共用边界的 range 篮子在收盘价恰好等于整数价位（到分）时会少付，标为「边界」；概率约为百万分之几，但不是零。",
          "- hit 窗口：当天 12:00 AM ET 到 11:59 PM ET 开盘的所有 1 分钟 K 线（[start_ts, end_ts)）。正午 K 线收盘 > K ⇒ 那根 K 线最高价 ≥ K ⇒ 当天 hit_up(K) 为 Yes，"
          "只在窗口包含这根 K 线时成立（逐对检查）；本次检查到的 K 线 ⊂ 窗口："
          + ("；".join(f"{t} {_t(c, '%m-%d %H:%M')}–{_t(c + 60, '%H:%M')} ⊂ [{_t(w[0], '%m-%d %H:%M')}, {_t(w[1], '%m-%d %H:%M')})" for t, c, w in wins[:6]) or "无")
          + "。updown_day 用的是「12:00 收盘」的那根（11:59 开盘），和 above / range 不是同一根，所以两者之间没有结构关系；updown_4h 按 Chainlink TWAP 结算，全部排除。",
          "- hit 市场一旦币安成交价到达价位就立即结算（盘口失效），从那一刻起不再用；录制前当天已到过的价位整段不用。",
          "- 份数只看买一/卖一那一档（built 表只有第一档），吃更多要走更深的价位，所以 $/天 是上限；同一个错价会出现在很多篮子里，看去重那一行。",
          "- 手续费按文档的 fee = C·feeRate·p·(1−p)（crypto 0.07）以 USDC 计；实际按 5 位小数取整，买单的费可能以份额扣除，两腿份数会差一点点。",
          "- 两条以上的腿不是原子成交：0.5 秒后仍在的比例是腿风险的粗略指标。资金要锁到结算（above/range/updown 正午，hit 当天结束；全买 No 可以马上 convert）。",
          "- 交易所时间戳；两个交易所之间的时钟偏差没测。正式结果来自 CI（polymarket-ladder-arb）在 polymarket-ladder 每段 5.5 小时录制（ladder-<run id> 产物）上的运行；$/天 是按录制时长线性外推的。"]
    if notes:
        L += ["", "备注：", ""] + [f"- {n}" for n in notes]
    return "\n".join(L) + "\n"


def _reward_section(results, hours):
    per_day = 24.0 / hours if hours > 0 else np.nan
    L = ["", "## 3. 流动性奖励（估计）", "",
         f"公式按 Polymarket 文档（{DOC_REWARDS}）：「S(v,s) = ((v−s)/v)²·b」，v = 最大价差（美分），s = 到「size-cutoff-adjusted midpoint」的距离；"
         "Q_one = Σ S·买单量（m）+ Σ S·卖单量（m'），Q_two 对称；「If midpoint is in range [0.10, 0.90] … Q_min = max(min(Q_one, Q_two), max(Q_one/c, Q_two/c))」，"
         "其外「liquidity must be double-sided」Q_min = min(Q_one, Q_two)，c = 3.0；每分钟随机采样一次，按 Q_min 占比分当天的奖励池。"
         "这里：b = 1；Yes 盘口已经镜像了 No 的挂单，所以只用 Yes 盘口；调整中价 = 每边累计挂单量第一次达到最小份数的价格的中点（假设）；"
         "每个 ≥ 最小份数的价位算一个挂单者，其他所有挂单合成一个做市商（对手 Q_min 偏高、我们的份额偏低）；"
         "我们的报价 = 每边最小份数，「边缘」= 仍计分的最宽价位，「半价差」= v/2，「贴盘口」= 加入买一/卖一。"
         "成交 = 录到的成交（taker 方向，两个代币的都映射到 Yes 盘口）价格达到或穿过我们的价位（不考虑排队，偏多）；"
         "标记 = 成交后 30 s / 120 s 的中价（缺一边按 0/1）或结算价减去成交价（多空方向已算），另加 20% 的 taker 费返佣估计。", ""]
    samples = [r.get("samples") for r in results if r.get("samples") is not None and len(r.get("samples"))]
    fills = [r.get("fills") for r in results if r.get("fills") is not None and len(r.get("fills"))]
    depth = sorted({r.get("depth") for r in results if r.get("depth")})
    mk, pools = {}, {}
    for r in results:
        for m in r["markets"]:
            p = (r.get("prm") or {}).get(m["condition_id"]) or {}
            mk[m["condition_id"]] = m
            if p.get("rate", 0) > 0:
                pools[m["condition_id"]] = (m, p)
    if not samples:
        return L + ["没有可用的奖励参数或采样（离线且缓存里没有，或没有市场）。"]
    s = pd.concat(samples, ignore_index=True)
    f = pd.concat(fills, ignore_index=True) if fills else \
        pd.DataFrame(columns=["type", "how", "qty", "m30", "m120", "msettle", "rebate", "cid"])
    L += [f"盘口深度来源：{' / '.join({'full': '原始消息的完整盘口', 'L1': 'built 表的第一档（缺深层挂单，份额偏高）'}[d] for d in depth)}；"
          f"采样 {len(s):,} 个（市场 × 分钟）；有奖励池的市场 {len(pools)} 个，池合计 ${sum(p['rate'] for _, p in pools.values()):,.2f}/天。", "",
          "| 类型 | 报价 | 市场（有池） | 池 $/天 | 平均份额（全部 / 有池） | 奖励 $/天 | 成交笔 / 份 | 30 s 标记 $/天 | 120 s 标记 $/天 | 结算标记 $/天（已结算的） | 返佣 $/天 |",
          "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for typ in ld.TYPES:
        st = s[s["type"] == typ]
        n_m = sum(m["type"] == typ for m in mk.values())
        pt_ = [p["rate"] for m, p in pools.values() if m["type"] == typ]
        if not n_m:
            continue
        for how in QUOTES:
            ft = f[(f["type"] == typ) & (f["how"] == how)] if len(f) else f
            usd = lambda c: (ft[c] * ft["qty"]).sum() * per_day if len(ft) and ft[c].notna().any() else np.nan
            pooled = st[st["rate"] > 0] if len(st) else st
            L.append(f"| {typ} | {how} | {n_m}（{len(pt_)}） | {sum(pt_):,.2f} | "
                     f"{_pct(st[f'share_{how}'].mean() if len(st) else np.nan)} / {_pct(pooled[f'share_{how}'].mean() if len(pooled) else np.nan)} | "
                     f"{st[f'usd_{how}'].sum() * per_day if len(st) else 0:,.2f} | {len(ft)} / {_n(ft['qty'].sum() if len(ft) else 0)} | "
                     f"{_n(usd('m30'), 2)} | {_n(usd('m120'), 2)} | {_n(usd('msettle'), 2)} | "
                     f"{_n((ft['rebate'] * ft['qty']).sum() * per_day if len(ft) else 0, 2)} |")
    if pools:
        L += ["", "有奖励池的市场（池来自 /rewards/markets/current 快照，含赞助）：", "",
              "| 市场 | 池 $/天（其中赞助） | 最小份数 / 最大价差 ¢ | 采样 | 中价中位 | 份额 边缘 / 半价差 / 贴盘口 | 奖励 $/天 边缘 / 半价差 / 贴盘口 |",
              "|---|---:|---:|---:|---:|---:|---:|"]
        for cid, (m, p) in sorted(pools.items(), key=lambda x: -x[1][1]["rate"]):
            g = s[s["cid"] == cid]
            L.append(f"| {m.get('slug') or cid} | {p['rate']:,.2f}（{_n(p.get('sponsored'), 2)}） | {_n(p['min_size'])} / {_n(p['max_spread'], 1)} | {len(g)} | "
                     + (f"{g['mid'].median():.3f} | " + " / ".join(_pct(g[f"share_{h}"].mean()) for h in QUOTES) + " | "
                        + " / ".join(f"{g[f'usd_{h}'].sum() * per_day:,.2f}" for h in QUOTES) + " |" if len(g) else
                        "– | 没有可计分的采样（单边盘口：中价无定义，双边报价放不下） | 0 |"))
    else:
        L += ["", "录到的市场里没有一个有奖励池（/rewards/markets/current 快照和 Gamma clobRewards 都没有日奖励额）。"]
    return L


def _pct(x):
    return f"{100 * x:.1f}%" if x is not None and np.isfinite(x) else "–"


# ------------------------------------------------------------------ CLI

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
    ap.add_argument("--out", default="real/ladder-arb.md")
    ap.add_argument("--cache", default=os.environ.get("LADDER_ARB_CACHE", str(Path.home() / ".cache" / "ladder-arb")),
                    help="缓存 Gamma / CLOB 响应的目录")
    ap.add_argument("--offline", action="store_true", help="只用缓存，不联网")
    ap.add_argument("--max-age", type=float, default=MAX_AGE)
    ap.add_argument("--note", default=None, help="写在报告开头的一句说明")
    a = ap.parse_args(argv)
    fetcher = Fetcher(a.cache, offline=a.offline)
    loaded, notes = [], []
    for d in built_dirs(a.dirs):
        try:
            books, markets, spot, trades, closes = ld.load(d)
        except Exception as e:
            notes.append(f"{_name(d)}: 跳过（{type(e).__name__}: {e}）")
            continue
        if books.empty or markets.empty:
            notes.append(f"{_name(d)}: 没有盘口或市场")
            continue
        loaded.append((d, books, markets, spot, trades, closes))
    allm = prep_markets(pd.concat([x[2] for x in loaded], ignore_index=True).drop_duplicates("condition_id")) if loaded else []
    prm = reward_params(allm, fetcher, notes) if allm else {}
    results, names = [], []
    for d, books, markets, spot, trades, closes in loaded:
        try:
            ms = prep_markets(markets, spot)
            src = d.parent if d.name == "built" else d
            res = analyze(books, markets, spot, closes, trades, src=src, prm=prm,
                          settle_fn=lambda cids, ms=ms: settlements(ms, cids, fetcher, notes),
                          max_ages=tuple(dict.fromkeys((a.max_age, np.inf))))
        except Exception as e:  # one bad recording must not stop the report
            notes.append(f"{_name(d)}: 跳过（{type(e).__name__}: {e}）")
            continue
        results.append(res)
        names.append(_name(d))
    notes.append(f"Gamma / CLOB 网络请求 {fetcher.requests} 次（其余来自缓存）")
    text = report(results, names, notes, a.note, a.max_age)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
