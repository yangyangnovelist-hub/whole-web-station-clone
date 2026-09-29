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

Triggers: the first second in each market's window with 240 >= tau >= 60 at which the
Binance price moved more than z sigma over the previous second (sigma: std of 1 s log returns
over the preceding 10 minutes). A trader who reacts `L` seconds after the move (Binance
exchange time) buys the side of the move at the ask the exchange shows at that moment (the
last book update stamped at or before it), pays the taker fee and holds to settlement. Two
versions: at whatever the ask is ("market"), or only if it is still at or below the ask
shown at the move ("stale limit"). One trade per market.

09-14..16 lies inside the September range already explored on chain, so this is for
understanding the mechanism and its time scale, not a pass/fail test.
"""
from __future__ import annotations

import argparse
import gzip
import io
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.csv as pacsv

import binary as bo

LAGS = (0.0, 0.1, 0.2, 0.3, 0.5, 1.0, 2.0, 5.0)
ZS = (2.0, 3.0, 4.0)
TAUS = (240, 60)


def _parts(d, stem):
    parts = sorted(Path(d).glob(f"{stem}.part-*"))
    if not parts:
        raise FileNotFoundError(f"no {stem}.part-* under {d}")
    return b"".join(p.read_bytes() for p in parts)


def read_csv_zst(d, stem):
    raw = _parts(d, stem)
    with pa.CompressedInputStream(pa.BufferReader(raw), "zstd") as f:
        return pacsv.read_csv(f, convert_options=pacsv.ConvertOptions(strings_can_be_null=True)).to_pandas()


def first_level(js):
    """Price and size of the first level of a JSON ladder like [[0.17,677.85],...]."""
    try:
        lv = json.loads(js)
        return (float(lv[0][0]), float(lv[0][1])) if lv else (np.nan, np.nan)
    except (TypeError, ValueError, IndexError):
        return np.nan, np.nan


def load(d):
    d = Path(d)
    books = read_csv_zst(d, "runtime_1000.poly_probability_observations_v1.csv.zst")
    ask = np.array([first_level(x) for x in books["asks_json"]])
    bid = np.array([first_level(x) for x in books["bids_json"]])
    books = pd.DataFrame({"market_id": books["market_id"], "ts": books["source_ts"].astype(float),
                          "bid": books["best_bid"].astype(float), "ask": books["best_ask"].astype(float),
                          "bid_size": bid[:, 1], "ask_size": ask[:, 1]}).sort_values(["market_id", "ts"])
    reg = read_csv_zst(d, "runtime_1000.market_registry.csv.zst")
    out = read_csv_zst(d, "runtime_1000.market_outcomes.csv.zst")
    markets = reg[["market_id", "start_ts"]].merge(out[["market_id", "winner"]], on="market_id")
    raw = _parts(d, "shadow_current.jsonl.gz")
    rows = []
    with gzip.open(io.BytesIO(raw), "rt") as f:
        for line in f:
            if '"BINANCE_AGG_TRADE"' not in line:
                continue
            try:
                e = json.loads(line)
                rows.append((float(e["trade_ts"]), float(e["receive_ts"]), float(e["price"])))
            except (ValueError, KeyError, TypeError):
                continue
    binance = pd.DataFrame(rows, columns=["trade_ts", "receive_ts", "price"]).sort_values("trade_ts")
    return books, markets, binance


def spot_grid(binance):
    """Last Binance log price at each whole second and the trailing 10 min std of 1 s returns."""
    sec = np.floor(binance["trade_ts"].to_numpy()).astype("int64")
    last = pd.Series(np.log(binance["price"].to_numpy()), index=sec).groupby(level=0).last()
    grid = last.reindex(range(int(last.index[0]), int(last.index[-1]) + 1)).ffill()
    sigma = grid.diff().rolling(600, min_periods=300).std()
    return grid, sigma


def triggers(markets, grid, sigma, z):
    """First 1 s Binance move above z sigma in each market's 240..60 s window: (market, t, side)."""
    out = []
    for m in markets.itertuples():
        secs = np.arange(m.start_ts + bo.WINDOW_S - TAUS[0], m.start_ts + bo.WINDOW_S - TAUS[1] + 1)
        now, prev = grid.reindex(secs).to_numpy(), grid.reindex(secs - 1).to_numpy()
        sg = sigma.reindex(secs - 1).to_numpy()
        with np.errstate(invalid="ignore"):
            hit = np.abs(now - prev) > z * sg
        if hit.any():
            i = int(np.argmax(hit))
            out.append((m.market_id, float(secs[i]), "Up" if now[i] > prev[i] else "Down", m.winner))
    return pd.DataFrame(out, columns=["market_id", "t", "side", "winner"])


class Book:
    def __init__(self, books):
        self.by = {mid: (g["ts"].to_numpy(), g[["bid", "ask", "bid_size", "ask_size"]].to_numpy())
                   for mid, g in books.groupby("market_id", sort=False)}

    def at(self, mid, t):
        """(bid, ask, bid_size, ask_size) of the Up token as the exchange showed it at time t."""
        if mid not in self.by:
            return None
        ts, v = self.by[mid]
        i = np.searchsorted(ts, t, "right") - 1
        return v[i] if i >= 0 else None

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
        rows.append((won, p, fee, won - p - fee, size))
    return pd.DataFrame(rows, columns=["won", "price", "fee", "pnl", "size"])


def fmt(t, reps):
    if t.empty:
        return "0 | – | – | – | – | –"
    p = bo.fair_price_pvalue(t["pnl"].to_numpy(), (t["price"] + t["fee"]).to_numpy(), sims=reps) \
        if t["pnl"].mean() > 0 and len(t) >= 10 else 1.0
    return (f"{len(t):,} | {t['won'].mean():.1%} | {t['price'].mean():.3f} | {100 * t['pnl'].mean():+.2f}¢ | "
            f"{p:.4f} | {t['size'].median():.0f}")


def run(d, out, reps=20000):
    books, markets, binance = load(d)
    grid, sigma = spot_grid(binance)
    book = Book(books)
    delay = (binance["receive_ts"] - binance["trade_ts"]).quantile([0.5, 0.9, 0.99])
    L = ["# 过期报价的时间尺度：币安跳变后 Polymarket 卖一能挂多久", "",
         f"数据：`{Path(d).name}`，{len(markets):,} 个 BTC 5 分钟市场，{len(books):,} 次盘口更新（交易所时间戳），"
         f"{len(binance):,} 笔币安永续逐笔成交。探索性质：这段时间在九月链上分析里已经看过。", "",
         f"币安成交到 Dublin 收到的延迟：中位 {1000 * delay[0.5]:.0f} ms，90% {1000 * delay[0.9]:.0f} ms，"
         f"99% {1000 * delay[0.99]:.0f} ms。", ""]
    for z in ZS:
        trig = triggers(markets, grid, sigma, z)
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
    L += ["每格：笔数 | 胜率 | 平均价 | EV/份 | 精确 p | 卖一挂单量中位（份）。反应延迟从币安成交所在的交易所时间算起，"
          "包括收到行情、决策和订单到达交易所的全部时间；放在 Dublin 的程序实际大约 0.15–0.3 秒。"]
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("export_dir")
    ap.add_argument("--out", default="real/latency-t1.md")
    ap.add_argument("--reps", type=int, default=20000)
    a = ap.parse_args(argv)
    run(a.export_dir, a.out, a.reps)


if __name__ == "__main__":
    main()
