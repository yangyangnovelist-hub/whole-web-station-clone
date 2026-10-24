"""Which BTC price feed reaches this machine first? (market data only: no keys, no orders)

Records, stamped with this machine's receive time, every BTC trade from Binance spot (trade),
Binance USD-M perpetual (aggTrade), OKX BTC-USDT-SWAP, Bybit BTCUSDT linear, Deribit
BTC-PERPETUAL (public trades come in 100 ms batches; the raw channel needs a logged-in
connection) and Coinbase BTC-USD; the best bid / ask of Binance spot and perpetual whenever a
price changes; and once a second the round trip of GET clob.polymarket.com/time over one kept-alive
connection (the order path's network and gateway, without the 150 ms taker hold).

Run it on the trading server, next to the engine (same clock), for a day:

    python feeds.py record --out feeds.jsonl.gz --hours 24
    python feeds.py analyze feeds.jsonl.gz

The report gives, per source, the delay from the exchange's own timestamp to arrival here, and,
for every 2-sigma jump seen on any source (as the G / H trigger: the log price against the last
print at least a second earlier, sigma from 10 minutes of 1 s returns), which source showed it
first here and how many milliseconds the others came later.
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import http.client
import json
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

BINANCE_SPOT = "wss://stream.binance.com:9443/stream?streams=btcusdt@trade/btcusdt@bookTicker"
# USD-M futures split their streams: trades only on /market, best bid/ask on /public (the old
# fstream.binance.com/ws path still sends bookTicker but no aggTrade any more)
BINANCE_PERP = ("wss://fstream.binance.com/market/stream?streams=btcusdt@aggTrade",
                "wss://fstream.binance.com/public/stream?streams=btcusdt@bookTicker")
OKX = "wss://ws.okx.com:8443/ws/v5/public"
BYBIT = "wss://stream.bybit.com/v5/public/linear"
DERIBIT = "wss://www.deribit.com/ws/api/v2"
COINBASE = "wss://ws-feed.exchange.coinbase.com"
SUBSCRIBE = {
    "okx": {"op": "subscribe", "args": [{"channel": "trades", "instId": "BTC-USDT-SWAP"}]},
    "bybit": {"op": "subscribe", "args": ["publicTrade.BTCUSDT"]},
    "deribit": {"jsonrpc": "2.0", "id": 1, "method": "public/subscribe",
                "params": {"channels": ["trades.BTC-PERPETUAL.100ms"]}},
    "coinbase": {"type": "subscribe", "product_ids": ["BTC-USD"], "channels": ["matches"]},
}


def _iso_ms(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000


def parse(source, text, recv_ns, last_bbo):
    """Records (dicts) from one websocket message: trades {s, r, e, p, q} (receive ns, exchange ms,
    price, size) and best bid / ask changes {s: <source>_bbo, r, e, b, a}."""
    m = json.loads(text)
    out = []
    if source in ("binance_spot", "binance_perp"):
        d = m.get("data", m)
        ev = d.get("e")
        if ev in ("trade", "aggTrade"):
            out.append({"s": source, "r": recv_ns, "e": d["T"], "p": float(d["p"]), "q": float(d["q"])})
        elif "b" in d and "a" in d:  # bookTicker: keep only price changes
            b, a = float(d["b"]), float(d["a"])
            if last_bbo.get(source) != (b, a):
                last_bbo[source] = (b, a)
                out.append({"s": source + "_bbo", "r": recv_ns, "e": d.get("T") or d.get("E"), "b": b, "a": a})
    elif source == "okx":
        for d in m.get("data", []):
            out.append({"s": source, "r": recv_ns, "e": int(d["ts"]), "p": float(d["px"]), "q": float(d["sz"])})
    elif source == "bybit":
        for d in m.get("data", []) if str(m.get("topic", "")).startswith("publicTrade") else []:
            out.append({"s": source, "r": recv_ns, "e": int(d["T"]), "p": float(d["p"]), "q": float(d["v"])})
    elif source == "deribit":
        for d in (m.get("params") or {}).get("data", []) if m.get("method") == "subscription" else []:
            out.append({"s": source, "r": recv_ns, "e": int(d["timestamp"]), "p": float(d["price"]),
                        "q": float(d["amount"])})
    elif source == "coinbase" and m.get("type") in ("match", "last_match"):
        out.append({"s": source, "r": recv_ns, "e": _iso_ms(m["time"]), "p": float(m["price"]), "q": float(m["size"])})
    return out


async def _feed(source, url, queue, stop):
    import websockets
    last_bbo = {}
    while not stop.is_set():
        try:
            async with websockets.connect(url, ping_interval=20, max_size=None, compression=None) as ws:
                if source in SUBSCRIBE:
                    await ws.send(json.dumps(SUBSCRIBE[source]))
                last_ping = time.monotonic()
                while not stop.is_set():
                    text = await asyncio.wait_for(ws.recv(), timeout=30)
                    recv_ns = time.time_ns()  # first thing after the frame arrives
                    for rec in parse(source, text, recv_ns, last_bbo):
                        queue.put_nowait(rec)
                    if source == "bybit" and time.monotonic() - last_ping > 15:  # Bybit wants an app-level ping
                        await ws.send('{"op":"ping"}')
                        last_ping = time.monotonic()
        except Exception as e:  # reconnect after any drop; note it in the file
            queue.put_nowait({"s": source + "_error", "r": time.time_ns(), "err": repr(e)[:200]})
            await asyncio.sleep(2)


def _probe_once(conn):
    t0 = time.perf_counter_ns()
    conn.request("GET", "/time")
    resp = conn.getresponse()
    resp.read()
    return (time.perf_counter_ns() - t0) / 1e6, resp.status


async def _probe(queue, stop, host="clob.polymarket.com", every=1.0):
    conn = None
    while not stop.is_set():
        try:
            if conn is None:
                conn = http.client.HTTPSConnection(host, timeout=5)
            ms, status = await asyncio.to_thread(_probe_once, conn)
            queue.put_nowait({"s": "polymarket_rtt", "r": time.time_ns(), "ms": ms, "status": status})
        except Exception as e:
            queue.put_nowait({"s": "polymarket_rtt_error", "r": time.time_ns(), "err": repr(e)[:200]})
            conn = None
        await asyncio.sleep(every)


async def _writer(path, queue, stop):
    with gzip.open(path, "at", compresslevel=3) as f:
        while not (stop.is_set() and queue.empty()):
            try:
                rec = await asyncio.wait_for(queue.get(), timeout=1)
            except asyncio.TimeoutError:
                f.flush()
                continue
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")


async def record(out, hours, sources):
    queue, stop = asyncio.Queue(), asyncio.Event()
    urls = {"binance_spot": BINANCE_SPOT, "binance_perp": BINANCE_PERP, "okx": OKX, "bybit": BYBIT,
            "deribit": DERIBIT, "coinbase": COINBASE}
    tasks = [asyncio.create_task(_feed(s, u, queue, stop)) for s in sources
             for u in (urls[s] if isinstance(urls[s], tuple) else (urls[s],))]
    tasks.append(asyncio.create_task(_probe(queue, stop)))
    writer = asyncio.create_task(_writer(out, queue, stop))
    await asyncio.sleep(hours * 3600)
    stop.set()
    for t in tasks:
        t.cancel()
    await writer


def load(path):
    with gzip.open(path, "rt") as f:
        return pd.DataFrame([json.loads(line) for line in f])


def jumps(t, z0=2.0, ref_s=1.0, max_ref=5.0, sigma_window=600):
    """2-sigma prints of one source (as gated_trades): log price against the last print at least
    ref_s and at most max_ref seconds earlier, sigma from sigma_window s of 1 s log returns (by
    exchange time); returns exchange times (s), receive times (s) and the sign of each jump."""
    t = t.sort_values("e")
    ts, lp = t["e"].to_numpy() / 1000, np.log(t["p"].to_numpy())
    rt = t["r"].to_numpy() / 1e9
    sec = np.floor(ts).astype("int64")
    last = pd.Series(lp, index=sec).groupby(level=0).last()
    grid = last.reindex(range(int(sec[0]), int(sec[-1]) + 1)).ffill(limit=10)
    sigma = grid.diff().rolling(sigma_window, min_periods=sigma_window // 2).std()
    j = np.searchsorted(ts, ts - ref_s, "right") - 1
    ok = (j >= 0) & (ts - ts[np.maximum(j, 0)] <= max_ref)
    dx = np.where(ok, lp - lp[np.maximum(j, 0)], np.nan)
    sg = sigma.reindex(sec - 1).to_numpy()
    with np.errstate(invalid="ignore"):
        hit = np.abs(dx) > z0 * sg
    return ts[hit], rt[hit], np.sign(dx[hit])


def analyze(path, z0=2.0, gap=2.0):
    d = load(path)
    if d.empty:
        return "没有记录。\n"
    trades = d[d["p"].notna() & ~d["s"].str.endswith("_bbo")] if "p" in d else d.iloc[0:0]
    L = [f"# 哪个 BTC 行情源最先到这台机器（{Path(path).name}）", ""]
    span = (d["r"].max() - d["r"].min()) / 3.6e12
    L += [f"录了 {span:.1f} 小时，{len(trades):,} 笔成交。", "",
          "## 交易所时间戳 → 本机收到（毫秒）", "",
          "| 来源 | 笔数 | 中位 | 90% | 99% |", "|---|---:|---:|---:|---:|"]
    for s, g in trades.groupby("s"):
        lag = g["r"] / 1e6 - g["e"]
        q = lag.quantile([0.5, 0.9, 0.99])
        L.append(f"| {s} | {len(g):,} | {q[0.5]:.0f} | {q[0.9]:.0f} | {q[0.99]:.0f} |")
    rtt = d[d["s"] == "polymarket_rtt"] if "ms" in d else d.iloc[0:0]
    if len(rtt):
        q = rtt["ms"].quantile([0.5, 0.9, 0.99])
        L += ["", f"clob.polymarket.com/time 往返（长连接）：中位 {q[0.5]:.1f} ms，90% {q[0.9]:.1f} ms，99% {q[0.99]:.1f} ms"
                  f"（{len(rtt):,} 次）。"]
    # first arrival: 2-sigma hits of every source, clustered by sign into episodes (a new one after
    # `gap` s without a hit of that sign); per episode, each source's first hit here
    ev = []
    for s, g in trades.groupby("s"):
        if len(g) < 100:
            continue
        _, rt, sg = jumps(g, z0)
        ev.append(pd.DataFrame({"s": s, "r": rt, "sign": sg}))
    if ev:
        e = pd.concat(ev).sort_values("r").reset_index(drop=True)
        rows, open_ = [], {}
        for s, r, sg in zip(e["s"], e["r"], e["sign"]):
            cur = open_.get(sg)
            if cur is None or r - cur["last"] > gap:
                cur = open_[sg] = {"last": r, "first": {}}
                rows.append(cur["first"])
            cur["last"] = r
            cur["first"].setdefault(s, r)
        f = pd.DataFrame(rows)
        f = f[f.notna().sum(axis=1) >= 2]  # jumps seen by at least two sources
        L += ["", f"## 2σ 急动谁先到（{len(f):,} 次至少两个来源都看到的急动）", "",
              "| 来源 | 看到的次数 | 最先到的占比 | 比最先到的晚（中位 ms） | 比币安现货早（中位 ms，正数为早） |",
              "|---|---:|---:|---:|---:|"]
        earliest = f.min(axis=1)
        for s in f.columns:
            seen = f[s].notna()
            later = 1000 * (f.loc[seen, s] - earliest[seen])
            vs = 1000 * (f["binance_spot"] - f[s])[seen & f["binance_spot"].notna()] if "binance_spot" in f else pd.Series(dtype=float)
            L.append(f"| {s} | {seen.sum():,} | {100 * (later < 1e-6).mean():.0f}% | {later.median():.0f} | "
                     f"{vs.median() if len(vs) else float('nan'):.0f} |")
    return "\n".join(L) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("record")
    p.add_argument("--out", default="feeds.jsonl.gz")
    p.add_argument("--hours", type=float, default=24)
    p.add_argument("--sources", nargs="*", default=["binance_spot", "binance_perp", "okx", "bybit", "deribit", "coinbase"])
    p = sub.add_parser("analyze")
    p.add_argument("path")
    p.add_argument("--out")
    a = ap.parse_args(argv)
    if a.cmd == "record":
        asyncio.run(record(a.out, a.hours, a.sources))
    else:
        text = analyze(a.path)
        if a.out:
            Path(a.out).write_text(text, encoding="utf-8")
        print(text)


if __name__ == "__main__":
    main()
