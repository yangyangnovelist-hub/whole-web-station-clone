"""How long do Polymarket BTC 5m quotes stay stale after a Binance move, and what could a
trader with a given latency take? Exploratory study on exchange-stamped books.

    python latency.py <export dir> --out real/latency-t1.md

Input: one directory of the sgk90-shadow-archive `research-export` branch (e.g.
shadow-t1-20260914-20260916/), which holds, split into parts:
- runtime_1000.poly_probability_observations_v1.csv.zst: the Up token's top-5 book at every
  update, `source_ts` being the exchange's own timestamp;
- runtime_1000.market_registry / market_outcomes .csv.zst: windows and official winners;
- shadow_current.jsonl.gz: the engine's event log, whose BINANCE_AGG_TRADE events are the
  Binance BTCUSDT perpetual trades with their exchange time (trade_ts) and the time Dublin
  received them (receive_ts).

Triggers: the first Binance trade in each market's window with 240 >= tau >= 60 whose price
moved more than z sigma from the last trade at least one second earlier (sigma: std of 1 s log
returns over the preceding 10 minutes, up to the previous whole second). A trader who reacts `L`
seconds after that trade (Binance exchange time) buys the side of the move at the ask the
exchange shows at that moment (the last book update stamped at or before it), pays the taker fee
and holds to settlement. Two versions: at whatever the ask is ("market"), or only if it is still
at or below the ask shown at the move ("stale limit"). One trade per market.

Correction, 2026-09-30 01:40 UTC: until then a trigger's time was the start of the second whose
last trade made the move, so a reaction L < 1 s after it could use prices the trader would only
see up to a second later. That look-ahead produced most of the "edge that decays with latency"
reported for 09-26..29 (+4.9c at 0 s, +2.0c at 0.5 s for z = 3). Measured from the trade itself,
09-26..29 gives z = 3: +2.8c at 0 s, +0.7c at 0.4 s, +0.5c at 0.5 s (p = 0.38); z = 2: +3.9c at
0 s, then a flat +1.7..2.1c from 0.4 s to 30 s. What is left does not depend on speed.

09-14..16 lies inside the September range already explored on chain, so this is for
understanding the mechanism and its time scale, not a pass/fail test.

Pass/fail test, fixed 2026-09-29 21:15 UTC before any of the Dublin export's 09-26..29 books
were looked at (run with --since 2026-09-26):
- stale quotes: z = 3, reaction L = 0.5 s after the Binance trade (Dublin receives Binance in a
  median 0.27 s; 0.5 s leaves room to send the order), buying at the ask of that moment: passes
  with EV > 0 and exact p < 0.05;
- taker rules on the real book: #9, #10, #11 (preregistered 2026-09-08) and #103, #104
  (2026-09-29): each passes with EV > 0 and exact p < 0.05 / 5.
Result (real/latency-dublin-0926-0929.md): nothing passed; stale quotes at 0.5 s gave +2.00c,
p = 0.065, with the edge falling from +4.9c at 0 s to zero at 1 s.

Next test, fixed 2026-09-29 (commit f92f5f7, 21:14 UTC) after that result and before any later data: on Dublin
books recorded after 2026-09-29 20:30 UTC, z = 2 and a 0.4 s reaction at the ask of that moment
pass with EV > 0 and exact p < 0.05 (see NEXT_*). Its triggers are the corrected ones above
(fixed 2026-09-30 01:40 UTC, before any data after 20:30 was exported or looked at); with them
09-26..29 gave +2.1c (p = 0.052) at these settings, so a pass is far from certain. Second
source, fixed at the same time: the GitHub forward recordings from 2026-09-30 05:00 UTC
(`recording.py` writes <bundle>/latency/ with the Up token's exchange-stamped book and
Polymarket's relay of the Binance price). The same test, judged on whichever source first holds
NEXT_N trades from NEXT_SINCE (the GitHub one from 05:00); the other is reported, not judged. Stopping rule, fixed 2026-09-29 21:40 UTC
before any of these books were exported: judged once, on the first 700 such trades in time order
among markets starting from 2026-09-29 20:30 UTC (run with --since "2026-09-29 20:30" on the daily
exports); with fewer the run reports the count and no verdict, so watching the data arrive cannot
pick a favourable stopping point.

Test C, fixed 2026-09-30 09:00 UTC, before any of its data was recorded (the Coinbase feed starts
with the forward run dispatched after 10:00 UTC): on the GitHub forward recordings, 5m markets of
btc, eth, sol, xrp and doge starting from 2026-09-30 11:00 UTC; trigger on each coin's Coinbase
trade prints (exchange time) as in `triggers` with z = 3; buy the side of the move at the
exchange-stamped ask 0.3 s after the triggering print; taker fee; held to settlement. All coins
pooled, judged once on the first 3,000 trades in trigger-time order (see C_*); fewer give no
verdict. Why: the May-August study with every Binance trade (cross.py stale, README section 20)
puts the edge at +2..3c within 300-500 ms, where the sparse Binance feeds used so far (about one
print a second) cannot see it; 3,000 trades give about three chances in four of passing at +2c.
With two tests running (the one above and test C), each passes only with p < 0.025 (FAMILY_ALPHA),
so the chance of a false pass stays 5%.

Revision, 2026-09-30 09:40 UTC, still before any of test C's data was recorded (its markets start
at 11:00) and before any data of the test above was looked at: the same study on ETH
(real/cross-stale-eth.md) shows the edge gone by 300 ms (z = 3: +2.1c at 0 ms, -0.05c at 300 ms)
with a quarter of BTC's size at the ask, so pooling coins would dilute a BTC effect. Test C is
now BTC only, judged once on the first 1,500 trades (about six days), with p < 0.05. The
2-sigma / 0.4 s test above is withdrawn: it triggers on Binance feeds of about one print a second,
which the May-August study shows cannot see an edge that lives within half a second.

Implementation fixes to test C, 2026-09-30 about 12:00 UTC, before any of its data was converted
(its first recording ends about 15:15 UTC), after an adversarial review of the code path: each
recording is processed on its own, since pooling them carried prices and zero returns across the
45-60 min gaps between runs and made the first print of a run a trigger against a stale price; the
spot grid leaves gaps of more than C_MAX_GAP s empty and the reference print must be at most
C_REF_AGE s old; a trade needs the book feed running around the order time (C_ALIVE), so an outage
of the recording cannot pass for a stale exchange quote; a market recorded in two runs keeps its
earlier trigger; damaged recordings are listed and skipped; the verdict, once reached, is written to
real/latency-test-c.verdict.md with the recordings behind it and never recomputed. The rule itself
(C_SPOT, C_Z, C_LAG, C_N, C_SINCE, C_COINS, FAMILY_ALPHA) is unchanged.
"""
from __future__ import annotations

import argparse
import gzip
import io
import json
import math
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.csv as pacsv

import binary as bo

LAGS = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 1.0, 2.0, 5.0)
PRIMARY_Z, PRIMARY_LAG = 3.0, 0.5
NEXT_Z, NEXT_LAG = 2.0, 0.4  # withdrawn 2026-09-30 09:40 UTC (see module notes); reported, not judged
NEXT_SINCE, NEXT_N = "2026-09-29 20:30", 700  # judged once, on the first NEXT_N trades from NEXT_SINCE
ZS = (2.0, 3.0, 4.0)
TAUS = (240, 60)
# Test C: Coinbase-triggered, all recorded coins pooled, on the GitHub forward recordings.
C_SPOT, C_Z, C_LAG, C_N = "coinbase", 3.0, 0.3, 1500
C_SINCE = "2026-09-30 11:00"
C_COINS = ("btc",)  # revised 09:40 UTC from all five coins, before any of the test's data existed
FAMILY_ALPHA = 0.05  # test C alone: the 2-sigma / 0.4 s test above was withdrawn at 09:40 UTC
# Test C data hygiene, fixed 2026-09-30 before any of its data was converted (see module notes):
# each recording is processed on its own; the spot grid does not carry prices across gaps of more
# than C_MAX_GAP s; the reference print is at most C_REF_AGE s old; a trade needs the book feed
# running (an update within C_ALIVE[0] s before and C_ALIVE[1] s after the order time).
C_MAX_GAP, C_REF_AGE, C_ALIVE = 10, 5.0, (30.0, 10.0)


def _read(d, pattern):
    """Bytes and name of the export file matching `pattern` (whole or in .part-* pieces, .zst or .gz),
    preferring the live runtime over staging and archive copies."""
    d = Path(d)
    names = sorted({re.sub(r"\.part-[a-z]+$", "", q.name) for q in d.iterdir() if re.search(pattern, q.name)})
    names = [n for n in names if "staging" not in n and "archive" not in n] or names
    if not names:
        raise FileNotFoundError(f"nothing matching {pattern} under {d}")
    base = sorted(names, key=lambda n: (not n.startswith(("state__runtime_1000", "runtime_1000")), n))[0]
    parts = sorted(d.glob(base + ".part-*")) or [d / base]
    return base, b"".join(q.read_bytes() for q in parts)


def read_table(d, pattern, columns=None):
    base, raw = _read(d, pattern)
    codec = "zstd" if base.endswith(".zst") else "gzip" if base.endswith(".gz") else None
    stream = pa.CompressedInputStream(pa.BufferReader(raw), codec) if codec else pa.BufferReader(raw)
    opts = pacsv.ConvertOptions(strings_can_be_null=True, include_columns=columns)
    return pacsv.read_csv(stream, convert_options=opts).to_pandas()


def first_size(ladder):
    """Size at the first level of JSON ladders like [[0.17,677.85],...], vectorised."""
    return pd.to_numeric(ladder.astype(str).str.extract(r"^\[\[\s*[0-9.eE+-]+\s*,\s*([0-9.eE+-]+)")[0], errors="coerce")


def load_books(d):
    """The Up token's best bid/ask and first-level sizes per update, read in streamed batches so the
    JSON ladders (millions of rows) never sit in memory at once."""
    import pyarrow.compute as pc
    base, raw = _read(d, r"poly_probability_observations_v1\.csv")
    codec = "zstd" if base.endswith(".zst") else "gzip" if base.endswith(".gz") else None
    stream = pa.CompressedInputStream(pa.BufferReader(raw), codec) if codec else pa.BufferReader(raw)
    cols = ["market_id", "source_ts", "best_bid", "best_ask", "bids_json", "asks_json"]
    reader = pacsv.open_csv(stream, read_options=pacsv.ReadOptions(block_size=64 << 20),
                            convert_options=pacsv.ConvertOptions(include_columns=cols, column_types={
                                "market_id": pa.string(), "source_ts": pa.float64(), "best_bid": pa.float64(),
                                "best_ask": pa.float64(), "bids_json": pa.string(), "asks_json": pa.string()}))
    pat = r"^\[\[\s*(?P<p>[0-9.eE+-]+)\s*,\s*(?P<s>[0-9.eE+-]+)"
    parts = []
    for batch in reader:
        size = {k: pc.cast(pc.struct_field(pc.extract_regex(batch.column(c), pat), [1]), pa.float64())
                for k, c in (("bid_size", "bids_json"), ("ask_size", "asks_json"))}
        parts.append(pa.table({"market_id": pc.dictionary_encode(batch.column("market_id")),
                               "ts": batch.column("source_ts"), "bid": batch.column("best_bid"),
                               "ask": batch.column("best_ask"), **size}))
    books = pa.concat_tables(parts, promote_options="permissive").to_pandas()
    books["market_id"] = books["market_id"].astype(str)
    return books.sort_values(["market_id", "ts"], kind="stable").reset_index(drop=True)


SPOT = {"binance": (r"shadow_current\.jsonl\.gz", "BINANCE_AGG_TRADE"),  # which trade feed triggers
        "coinbase": (r"coinbase_trades\.jsonl\.gz", "COINBASE_TRADE")}


def _binance(d, spot="binance"):
    pattern, event = SPOT[spot]
    try:
        _, raw = _read(d, pattern)
    except FileNotFoundError:
        return []
    rows = []
    with gzip.open(io.BytesIO(raw), "rt", errors="replace") as f:
        for line in f:
            if f'"{event}"' not in line:
                continue
            try:
                e = json.loads(line)
                rows.append((float(e["trade_ts"]), float(e["receive_ts"]), float(e["price"])))
            except (ValueError, KeyError, TypeError):
                continue
    return rows


def load(*dirs, spot="binance"):
    """Books, markets and spot trades (Binance by default, or Coinbase) of one or more export
    directories (e.g. the daily exports), with rows that appear in several of them kept once."""
    dirs = [Path(d) for d in dirs]
    books = pd.concat([load_books(d) for d in dirs], ignore_index=True)
    if len(dirs) > 1:
        books = books.drop_duplicates().sort_values(["market_id", "ts"], kind="stable").reset_index(drop=True)
    reg = pd.concat([read_table(d, r"market_registry\.csv", ["market_id", "start_ts"]) for d in dirs])
    out = pd.concat([read_table(d, r"market_outcomes\.csv", ["market_id", "winner"]) for d in dirs])
    markets = reg.drop_duplicates("market_id").merge(out.drop_duplicates("market_id"), on="market_id")
    rows = [r for d in dirs for r in _binance(d, spot)]
    if not rows:
        raise FileNotFoundError(f"no {SPOT[spot][1]} events under {', '.join(map(str, dirs))}")
    binance = pd.DataFrame(rows, columns=["trade_ts", "receive_ts", "price"]).drop_duplicates().sort_values("trade_ts")
    return books, markets, binance.reset_index(drop=True)


def spot_grid(binance, max_gap=None):
    """Last Binance log price at each whole second and the trailing 10 min std of 1 s returns.
    With `max_gap`, seconds more than that far from the last print stay empty instead of carrying
    it forward, so an outage does not count as a run of zero returns."""
    sec = np.floor(binance["trade_ts"].to_numpy()).astype("int64")
    last = pd.Series(np.log(binance["price"].to_numpy()), index=sec).groupby(level=0).last()
    grid = last.reindex(range(int(last.index[0]), int(last.index[-1]) + 1)).ffill(limit=max_gap)
    sigma = grid.diff().rolling(600, min_periods=300).std()
    return grid, sigma


def triggers(markets, binance, sigma, z, max_ref_age=None):
    """First Binance move above z sigma in each market's 240..60 s window: (market, t, side, winner).

    A move is a trade whose log price differs from the last trade at least one second earlier by more
    than z times sigma (sigma of the per-second series up to the previous whole second), and `t` is
    that trade's own exchange time, so everything the trigger uses is known at t. (Before
    2026-09-30 01:40 UTC, t was the start of the second whose last trade made the move, which let a
    trade at t + L see prices up to a second later; see the module notes.)"""
    ts = binance["trade_ts"].to_numpy()
    lp = np.log(binance["price"].to_numpy())
    out = []
    for m in markets.itertuples():
        a, b = np.searchsorted(ts, [m.start_ts + bo.WINDOW_S - TAUS[0], m.start_ts + bo.WINDOW_S - TAUS[1] + 1])
        if b <= a:
            continue
        j = np.searchsorted(ts, ts[a:b] - 1.0, "right") - 1
        ref = np.where(j >= 0, lp[np.maximum(j, 0)], np.nan)
        if max_ref_age is not None:  # the reference print must be recent, not from before a gap
            ref = np.where(ts[a:b] - ts[np.maximum(j, 0)] <= max_ref_age, ref, np.nan)
        sg = sigma.reindex(np.floor(ts[a:b]).astype("int64") - 1).to_numpy()
        with np.errstate(invalid="ignore"):
            hit = np.abs(lp[a:b] - ref) > z * sg
        if hit.any():
            i = int(np.argmax(hit))
            out.append((m.market_id, float(ts[a + i]), "Up" if lp[a + i] > ref[i] else "Down", m.winner))
    return pd.DataFrame(out, columns=["market_id", "t", "side", "winner"])


class Book:
    def __init__(self, books):
        self.by = {mid: (g["ts"].to_numpy(), g[["bid", "ask", "bid_size", "ask_size"]].to_numpy())
                   for mid, g in books.groupby("market_id", sort=False)}

    def at(self, mid, t, max_age=None):
        """(bid, ask, bid_size, ask_size) of the Up token as the exchange showed it at time t
        (None if nothing was shown yet, or the last update is older than max_age seconds)."""
        if mid not in self.by:
            return None
        ts, v = self.by[mid]
        i = np.searchsorted(ts, t, "right") - 1
        if i < 0 or (max_age is not None and t - ts[i] > max_age):
            return None
        return v[i]

    def alive(self, mid, t, before, after):
        """Whether the recorded book updated within `before` s up to t and within `after` s after
        it, i.e. the feed was running, so an unchanged ask at t is the exchange's and not a gap."""
        if mid not in self.by:
            return False
        ts = self.by[mid][0]
        i = np.searchsorted(ts, t, "right")
        return i > 0 and t - ts[i - 1] <= before and i < len(ts) and ts[i] - t <= after

    def side_ask(self, mid, t, side):
        r = self.at(mid, t)
        if r is None:
            return np.nan, np.nan
        bid, ask, bid_size, ask_size = r
        return (ask, ask_size) if side == "Up" else (1 - bid, bid_size)

    def reaction(self, mid, t0, side, horizon=10.0):
        """Seconds until the side's ask first moves up from its level at t0 (NaN if not within horizon)."""
        if mid not in self.by:
            return np.nan
        ts, v = self.by[mid]
        a0 = self.side_ask(mid, t0, side)[0]
        i, j = np.searchsorted(ts, [t0, t0 + horizon], "right")
        asks = v[i:j, 1] if side == "Up" else 1 - v[i:j, 0]
        moved = np.flatnonzero(asks > a0 + 1e-9)
        return float(ts[i + moved[0]] - t0) if len(moved) and np.isfinite(a0) else np.nan


def trade(trig, book, lag, stale_only, alive=None):
    """Buy the side of each trigger at the ask `lag` s later; with `alive=(before, after)` only
    where the book feed was running around that moment (see Book.alive)."""
    rows = []
    for r in trig.itertuples():
        if alive is not None and not book.alive(r.market_id, r.t + lag, *alive):
            continue
        a0 = book.side_ask(r.market_id, r.t, r.side)[0]
        p, size = book.side_ask(r.market_id, r.t + lag, r.side)
        if not (np.isfinite(p) and 0.02 <= p <= 0.98):
            continue
        if stale_only and not (np.isfinite(a0) and p <= a0 + 1e-9):
            continue
        won = float(r.winner == r.side)
        fee = float(bo.taker_fee(p))
        rows.append((won, p, fee, won - p - fee, size, r.t, r.market_id))
    return pd.DataFrame(rows, columns=["won", "price", "fee", "pnl", "size", "t", "market_id"])


# Taker rules that only need the book: (name, tau, lo, hi); ids as in strategy_zoo.
BOOK_RULES = {9: ("强势方 τ=30 卖一∈[0.70,0.97]", 30, 0.70, 0.97), 10: ("强势方 τ=30 卖一∈[0.80,0.97]", 30, 0.80, 0.97),
              11: ("强势方 τ=30 卖一∈[0.85,0.99]", 30, 0.85, 0.99), 103: ("强势方 τ=90 卖一∈[0.60,0.80]", 90, 0.60, 0.80),
              104: ("强势方 τ=45 卖一∈[0.60,0.80]", 45, 0.60, 0.80)}


def book_rule(markets, book, tau, lo, hi):
    """Buy the favourite at its ask with `tau` seconds left when the ask is in [lo, hi]; the book must
    have updated within the last 10 seconds. One share, taker fee, held to settlement."""
    rows = []
    for m in markets.itertuples():
        r = book.at(m.market_id, m.start_ts + bo.WINDOW_S - tau, max_age=10)
        if r is None or not (np.isfinite(r[0]) and np.isfinite(r[1])):
            continue
        side = "Up" if (r[0] + r[1]) / 2 >= 0.5 else "Down"
        p, size = (r[1], r[3]) if side == "Up" else (1 - r[0], r[2])
        if not (lo - 1e-9 <= p <= hi + 1e-9):
            continue
        won = float(m.winner == side)
        fee = float(bo.taker_fee(p))
        rows.append((won, p, fee, won - p - fee, size, m.start_ts))
    return pd.DataFrame(rows, columns=["won", "price", "fee", "pnl", "size", "start"])


def fmt(t, reps):
    if t.empty:
        return "0 | – | – | – | – | –"
    p = bo.fair_price_pvalue(t["pnl"].to_numpy(), (t["price"] + t["fee"]).to_numpy(), sims=reps) \
        if t["pnl"].mean() > 0 and len(t) >= 10 else 1.0
    return (f"{len(t):,} | {t['won'].mean():.1%} | {t['price'].mean():.3f} | {100 * t['pnl'].mean():+.2f}¢ | "
            f"{p:.4f} | {t['size'].median():.0f}")


def verdict(t, z, lag, reps, alpha=0.05):
    p = bo.fair_price_pvalue(t["pnl"].to_numpy(), (t["price"] + t["fee"]).to_numpy(), sims=reps) \
        if len(t) >= 10 and t["pnl"].mean() > 0 else 1.0
    ok = len(t) >= 10 and t["pnl"].mean() > 0 and p < alpha
    return (f"过期报价（z={z:g}，{lag:g} 秒后按卖一买）：{len(t)} 笔，EV "
            f"{100 * t['pnl'].mean() if len(t) else float('nan'):+.2f}¢，p = {p:.4f} → {'通过' if ok else '没通过'}")


def run(d, out, reps=20000, since=None, until=None, label="", spot="binance"):
    dirs = [d] if isinstance(d, (str, Path)) else list(d)
    books, markets, binance = load(*dirs, spot=spot)
    if since or until:
        lo = pd.Timestamp(since or "2000-01-01", tz="UTC").timestamp()
        hi = pd.Timestamp(until or "2100-01-01", tz="UTC").timestamp()
        markets = markets[(markets["start_ts"] >= lo) & (markets["start_ts"] < hi)]
        books = books[books["market_id"].isin(set(markets["market_id"]))]
    grid, sigma = spot_grid(binance)
    book = Book(books)
    delay = (binance["receive_ts"] - binance["trade_ts"]).quantile([0.5, 0.9, 0.99])
    L = [f"# 过期报价的时间尺度：币安跳变后 Polymarket 卖一能挂多久{label}", "",
         f"数据：{'、'.join(f'`{Path(x).name}`' for x in dirs)}，{len(markets):,} 个 BTC 5 分钟市场"
         f"{f'（{since} UTC 起开始的）' if since else ''}，{len(books):,} 次盘口更新（交易所时间戳），"
         f"{len(binance):,} 笔币安永续逐笔成交。", "",
         f"币安成交到 Dublin 收到的延迟：中位 {1000 * delay[0.5]:.0f} ms，90% {1000 * delay[0.9]:.0f} ms，"
         f"99% {1000 * delay[0.99]:.0f} ms。", ""]
    verdicts = []
    for z in ZS:
        trig = triggers(markets, binance, sigma, z)
        react = np.array([book.reaction(r.market_id, r.t, r.side) for r in trig.itertuples()])
        ok = np.isfinite(react)
        q = np.nanpercentile(react, [25, 50, 75, 90]) if ok.any() else [np.nan] * 4
        L += [f"## 币安 1 秒涨跌超过 {z:g}σ（{len(trig):,} 个市场触发）", "",
              f"卖一在跳变后第一次上移：{ok.mean():.0%} 在 10 秒内上移；用时 25% {q[0]:.2f} 秒，中位 {q[1]:.2f} 秒，"
              f"75% {q[2]:.2f} 秒，90% {q[3]:.2f} 秒。", "",
              "| 反应延迟 | 按当时卖一买（笔数 / 胜率 / 平均价 / EV / p / 卖一挂单量中位） | 卖一没动才买 |",
              "|---:|---|---|"]
        for lag in LAGS:
            L.append(f"| {lag:g} 秒 | {fmt(trade(trig, book, lag, False), reps)} | {fmt(trade(trig, book, lag, True), reps)} |")
        L.append("")
        if z == PRIMARY_Z:
            verdicts.append("09-26..29 的检验：" + verdict(trade(trig, book, PRIMARY_LAG, False), z, PRIMARY_LAG, reps))
        if z == NEXT_Z:
            t = trade(trig, book, NEXT_LAG, False).sort_values("t", kind="stable")
            head = f"已撤销的 2σ/0.4 秒检验（{NEXT_SINCE} UTC 起开始的市场，按时间取前 {NEXT_N} 笔，只作描述）："
            if not since or pd.Timestamp(since, tz="UTC") < pd.Timestamp(NEXT_SINCE, tz="UTC"):
                verdicts.append(head + f"不适用，这次运行含更早的数据（用 --since \"{NEXT_SINCE}\"）")
            elif len(t) < NEXT_N:
                verdicts.append(head + f"目前 {len(t)} 笔，不到 {NEXT_N} 笔，不判定")
            else:
                verdicts.append(head + verdict(t.head(NEXT_N), z, NEXT_LAG, reps).split(" → ")[0] + "（已撤销，不判定）")
    L += ["## 真实盘口上的吃单规则", "",
          "剩 τ 秒时，强势方卖一在区间内就按卖一买 1 份（盘口 10 秒内有更新才算），付 taker 费，持有到结算。", "",
          "| # | 规则 | 笔数 / 胜率 / 平均价 / EV / p / 卖一挂单量中位 |", "|---:|---|---|"]
    for rid, (name, tau, lo, hi) in BOOK_RULES.items():
        t = book_rule(markets, book, tau, lo, hi)
        L.append(f"| {rid} | {name} | {fmt(t, reps)} |")
        p = bo.fair_price_pvalue(t["pnl"].to_numpy(), (t["price"] + t["fee"]).to_numpy(), sims=reps) \
            if len(t) >= 10 and t["pnl"].mean() > 0 else 1.0
        ok = len(t) >= 10 and t["pnl"].mean() > 0 and p < 0.05 / len(BOOK_RULES)
        verdicts.append(f"#{rid} {name}：{len(t)} 笔，EV {100 * t['pnl'].mean() if len(t) else float('nan'):+.2f}¢，"
                        f"p = {p:.4f} → {'通过' if ok else '没通过'}")
    L += ["", "## 事先定好的判定", "",
          "09-26..29 的检验和 #9–#104 只对 `--since 2026-09-26` 的那次运行有效；2σ/0.4 秒检验已撤销（见第一条），现在的检验是检验 C（`--pooled`）。", ""] + [f"- {v}" for v in verdicts] + [""]
    L += ["每格：笔数 | 胜率 | 平均价 | EV/份 | 精确 p | 卖一挂单量中位（份）。反应延迟从币安成交所在的交易所时间算起，"
          "包括收到行情、决策和订单到达交易所的全部时间；放在 Dublin 的程序实际大约 0.15–0.3 秒。"]
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


def pooled_trades(dirs_by_coin, since, spot=C_SPOT, z=C_Z, lags=(C_LAG,), notes=None):
    """Trades of the stale-quote rule, each recording (directory) on its own spot feed and book,
    pooled over recordings and coins; a market recorded twice keeps its earlier trigger. One row
    per (coin, market, lag) with the trigger time `t` and the recording `run`."""
    lo = pd.Timestamp(since, tz="UTC").timestamp()
    notes = [] if notes is None else notes
    out = []
    for coin, dirs in sorted(dirs_by_coin.items()):
        for d in dirs:
            run = Path(d).parent.parent.parent.name  # <rec>/<run id>/x/bundle-<coin>/latency
            try:
                books, markets, spot_trades = load(d, spot=spot)
            except Exception as e:  # a missing or damaged recording is noted and skipped
                notes.append(f"{coin} {run}: skipped ({type(e).__name__}: {e})")
                continue
            markets = markets[markets["start_ts"] >= lo]
            books = books[books["market_id"].isin(set(markets["market_id"]))]
            if markets.empty or books.empty or len(spot_trades) < 2:
                notes.append(f"{coin} {run}: no markets from {since} with book and spot data")
                continue
            _, sigma = spot_grid(spot_trades, max_gap=C_MAX_GAP)
            trig = triggers(markets, spot_trades, sigma, z, max_ref_age=C_REF_AGE)
            book = Book(books)
            for lag in lags:
                t = trade(trig, book, lag, False, alive=C_ALIVE)
                t["coin"], t["lag"], t["run"] = coin, lag, run
                out.append(t)
    if not out:
        return pd.DataFrame(columns=["t", "pnl", "coin", "lag", "run", "market_id"])
    t = pd.concat(out, ignore_index=True).sort_values("t", kind="stable")
    return t.drop_duplicates(["coin", "lag", "market_id"], keep="first").reset_index(drop=True)


def run_pooled(roots, out, reps=20000):
    """Test C on every <root>/**/bundle-<coin>/latency directory: judged once on the first C_N
    trades in trigger-time order, pooled over C_COINS; fewer trades give the count, no verdict.
    Once a verdict is written to <out>.verdict.md it is kept and never recomputed."""
    dirs = {}
    for r in roots:
        for d in sorted(Path(r).glob("**/bundle-*/latency")):
            coin = d.parent.name.removeprefix("bundle-")
            if coin in C_COINS:
                dirs.setdefault(coin, []).append(d)
    notes = []
    t = pooled_trades(dirs, C_SINCE, lags=sorted(set(LAGS) | {C_LAG}), notes=notes)
    coins = "、".join(C_COINS)
    L = [f"# 检验 C：{C_SPOT} 触发的过期报价（{coins}，GitHub 前向录制）", "",
         f"事先写死：{C_SINCE} UTC 起开始的 5m 市场；{C_SPOT} 逐笔成交在剩 240–60 秒时第一次相对至少一秒前涨跌超过 "
         f"{C_Z:g}σ，{C_LAG:g} 秒后按交易所时间戳盘口的卖一买顺势一方，付 taker 费，持有到结算；"
         f"按触发时间取前 {C_N:,} 笔判定一次，EV > 0 且精确 p < {FAMILY_ALPHA} 才算通过。"
         f"每段录制单独计算（σ 不跨越 {C_MAX_GAP} 秒以上的空档，参考价不早于 {C_REF_AGE:g} 秒），"
         f"下单时刻前 {C_ALIVE[0]:g} 秒内和后 {C_ALIVE[1]:g} 秒内盘口都要有更新。", "",
         "| 币种 | 录制段 | 触发（按 L = 检验值） |", "|---|---:|---:|"]
    main_t = t[t["lag"] == C_LAG].sort_values("t", kind="stable") if len(t) else t
    for coin in C_COINS:
        L.append(f"| {coin} | {len(dirs.get(coin, []))} | {int((main_t['coin'] == coin).sum()) if len(main_t) else 0:,} |")
    if notes:
        L += ["", "跳过的录制段：", ""] + [f"- {n}" for n in notes]
    L += ["", "| L | 笔数 | 胜率 | 平均价 | EV | p | 卖一数量中位 |", "|---:|---|---|---|---|---|---|"]
    for lag, g in t.groupby("lag") if len(t) else []:
        L.append(f"| {lag:g} 秒 | {fmt(g, reps)} |")
    head = f"检验 C（前 {C_N:,} 笔）："
    pinned = Path(out).with_suffix(".verdict.md")
    if pinned.exists():
        L += ["", pinned.read_text(encoding="utf-8").strip() + "（已判定，不再重算）"]
    elif len(main_t) < C_N:
        L += ["", head + f"目前 {len(main_t):,} 笔，不到 {C_N:,} 笔，不判定。"]
    else:
        first = main_t.head(C_N)
        v = head + verdict(first, C_Z, C_LAG, reps, FAMILY_ALPHA)
        runs = sorted(first["run"].unique())
        pinned.parent.mkdir(parents=True, exist_ok=True)
        pinned.write_text(v + f"（录制段 {len(runs)} 个：{', '.join(map(str, runs))}；"
                          f"最后一笔触发于 {pd.to_datetime(first['t'].max(), unit='s', utc=True):%Y-%m-%d %H:%M} UTC）\n",
                          encoding="utf-8")
        first.to_csv(Path(out).with_suffix(".trades.csv"), index=False)
        L += ["", pinned.read_text(encoding="utf-8").strip()]
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("export_dir", nargs="+", help="one or more export directories (e.g. the daily ones)")
    ap.add_argument("--out", default="real/latency-t1.md")
    ap.add_argument("--reps", type=int, default=20000)
    ap.add_argument("--since", help="only markets starting on/after this UTC date")
    ap.add_argument("--until", help="only markets starting before this UTC date")
    ap.add_argument("--label", default="")
    ap.add_argument("--spot", default="binance", choices=sorted(SPOT), help="trade feed that triggers")
    ap.add_argument("--pooled", action="store_true",
                    help="test C: export_dir are roots holding recording bundle-<coin>/latency directories")
    a = ap.parse_args(argv)
    if a.pooled:
        run_pooled(a.export_dir, a.out, a.reps)
    else:
        run(a.export_dir, a.out, a.reps, a.since, a.until, a.label, a.spot)


if __name__ == "__main__":
    main()
