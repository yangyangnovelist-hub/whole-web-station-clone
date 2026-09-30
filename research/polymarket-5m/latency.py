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
NEXT_Z, NEXT_LAG = 2.0, 0.4  # the next preregistered test, for books after 2026-09-29 20:30 UTC
NEXT_SINCE, NEXT_N = "2026-09-29 20:30", 700  # judged once, on the first NEXT_N trades from NEXT_SINCE
ZS = (2.0, 3.0, 4.0)
TAUS = (240, 60)


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


def _binance(d):
    try:
        _, raw = _read(d, r"shadow_current\.jsonl\.gz")
    except FileNotFoundError:
        return []
    rows = []
    with gzip.open(io.BytesIO(raw), "rt", errors="replace") as f:
        for line in f:
            if '"BINANCE_AGG_TRADE"' not in line:
                continue
            try:
                e = json.loads(line)
                rows.append((float(e["trade_ts"]), float(e["receive_ts"]), float(e["price"])))
            except (ValueError, KeyError, TypeError):
                continue
    return rows


def load(*dirs):
    """Books, markets and Binance trades of one or more export directories (e.g. the daily
    exports), with rows that appear in several of them kept once."""
    dirs = [Path(d) for d in dirs]
    books = pd.concat([load_books(d) for d in dirs], ignore_index=True)
    if len(dirs) > 1:
        books = books.drop_duplicates().sort_values(["market_id", "ts"], kind="stable").reset_index(drop=True)
    reg = pd.concat([read_table(d, r"market_registry\.csv", ["market_id", "start_ts"]) for d in dirs])
    out = pd.concat([read_table(d, r"market_outcomes\.csv", ["market_id", "winner"]) for d in dirs])
    markets = reg.drop_duplicates("market_id").merge(out.drop_duplicates("market_id"), on="market_id")
    rows = [r for d in dirs for r in _binance(d)]
    if not rows:
        raise FileNotFoundError(f"no BINANCE_AGG_TRADE events under {', '.join(map(str, dirs))}")
    binance = pd.DataFrame(rows, columns=["trade_ts", "receive_ts", "price"]).drop_duplicates().sort_values("trade_ts")
    return books, markets, binance.reset_index(drop=True)


def spot_grid(binance):
    """Last Binance log price at each whole second and the trailing 10 min std of 1 s returns."""
    sec = np.floor(binance["trade_ts"].to_numpy()).astype("int64")
    last = pd.Series(np.log(binance["price"].to_numpy()), index=sec).groupby(level=0).last()
    grid = last.reindex(range(int(last.index[0]), int(last.index[-1]) + 1)).ffill()
    sigma = grid.diff().rolling(600, min_periods=300).std()
    return grid, sigma


def triggers(markets, binance, sigma, z):
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


def trade(trig, book, lag, stale_only):
    rows = []
    for r in trig.itertuples():
        a0 = book.side_ask(r.market_id, r.t, r.side)[0]
        p, size = book.side_ask(r.market_id, r.t + lag, r.side)
        if not (np.isfinite(p) and 0.02 <= p <= 0.98):
            continue
        if stale_only and not (np.isfinite(a0) and p <= a0 + 1e-9):
            continue
        won = float(r.winner == r.side)
        fee = float(bo.taker_fee(p))
        rows.append((won, p, fee, won - p - fee, size, r.t))
    return pd.DataFrame(rows, columns=["won", "price", "fee", "pnl", "size", "t"])


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


def verdict(t, z, lag, reps):
    p = bo.fair_price_pvalue(t["pnl"].to_numpy(), (t["price"] + t["fee"]).to_numpy(), sims=reps) \
        if len(t) >= 10 and t["pnl"].mean() > 0 else 1.0
    ok = len(t) >= 10 and t["pnl"].mean() > 0 and p < 0.05
    return (f"过期报价（z={z:g}，{lag:g} 秒后按卖一买）：{len(t)} 笔，EV "
            f"{100 * t['pnl'].mean() if len(t) else float('nan'):+.2f}¢，p = {p:.4f} → {'通过' if ok else '没通过'}")


def run(d, out, reps=20000, since=None, until=None, label=""):
    dirs = [d] if isinstance(d, (str, Path)) else list(d)
    books, markets, binance = load(*dirs)
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
            head = f"下一次检验（{NEXT_SINCE} UTC 起开始的市场，按时间取前 {NEXT_N} 笔）："
            if not since or pd.Timestamp(since, tz="UTC") < pd.Timestamp(NEXT_SINCE, tz="UTC"):
                verdicts.append(head + f"不适用，这次运行含更早的数据（用 --since \"{NEXT_SINCE}\"）")
            elif len(t) < NEXT_N:
                verdicts.append(head + f"目前 {len(t)} 笔，不到 {NEXT_N} 笔，不判定")
            else:
                verdicts.append(head + verdict(t.head(NEXT_N), z, NEXT_LAG, reps))
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
          "09-26..29 的检验和 #9–#104 只对 `--since 2026-09-26` 的那次运行有效；下一次检验见第一条。", ""] + [f"- {v}" for v in verdicts] + [""]
    L += ["每格：笔数 | 胜率 | 平均价 | EV/份 | 精确 p | 卖一挂单量中位（份）。反应延迟从币安成交所在的交易所时间算起，"
          "包括收到行情、决策和订单到达交易所的全部时间；放在 Dublin 的程序实际大约 0.15–0.3 秒。"]
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
    a = ap.parse_args(argv)
    run(a.export_dir, a.out, a.reps, a.since, a.until, a.label)


if __name__ == "__main__":
    main()
