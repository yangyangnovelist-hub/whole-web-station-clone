"""Round trips after a Binance jump on the GitHub millisecond books (exploratory; market data only,
no orders). Written 2026-10-04 to check, at real latency, the round-trip family that came out on
top of the kacho per-second search (ROUNDTRIP.md family 1: a >= 4 bp one-second Binance move,
buy that side, sell on the book h seconds later).

Fixed before it was run:
- Jumps: a Binance trade (exchange time) whose log price moved >= B bp from the last trade at least
  1 s earlier (that trade at most lt.C_REF_AGE s old), B in (2, 4, 8); 285..20 s left; per market
  the first one ("first") or every one at least 10 s after the previous kept one ("all").
- Entry: sent t0 + 0.11 s, matched M = 0.375 s later at the side's ask then (>= 5 shares,
  0.02..0.98); Down's ask is 1 - the Up bid. Exit: decided h s after the entry fill
  (h in 5, 10, 20, 30, 60), matched M later at the side's bid then (>= 5 shares; Down's bid is
  1 - the Up ask), at the latest decided at 15 s left; no bid then -> held to settlement (counted).
  Both legs pay binary.taker_fee. Book rows are used only at or before the moment they are used;
  an order needs the feed alive around its match and no CLOB disconnect across it.
- Controls: the opposite side at the same moments; a random time (uniform 285..20 s left) with a
  random side, same exits.
- Per share, standard error clustered by market; by halves of the recorded markets.

    python rt_ms.py ROOT [--out real/rt-ms.md]
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

import binary as bo
import latency as lt

BPS = (2.0, 4.0, 8.0)
HOLDS = (5, 10, 20, 30, 60)
SEND, M = 0.11, 0.375
TAU_HI, TAU_LO, LAST_EXIT, SPACING = 285, 20, 15, 10.0
KEYS = ["coin", "market_id", "t", "arm", "bp", "mode", "h"]  # t = the jump print (lt._per_recording sorts on "t")


def _ask(r, side):
    if r is None:
        return np.nan, np.nan
    return (float(r[1]), float(r[3])) if side == "Up" else (1.0 - float(r[0]), float(r[2]))


def _bid(r, side):
    if r is None:
        return np.nan, np.nan
    return (float(r[0]), float(r[2])) if side == "Up" else (1.0 - float(r[1]), float(r[3]))


def _ok(book, mid, t_send):
    t_match = t_send + M
    return book.alive(mid, t_match, *lt.C_ALIVE) and not book.across_close(mid, t_send, t_match)


def jumps(markets, spot_trades, bp, mode):
    ts = spot_trades["trade_ts"].to_numpy(dtype=float)
    lp = np.log(spot_trades["price"].to_numpy(dtype=float))
    out = []
    for m in markets.itertuples():
        end = m.start_ts + bo.WINDOW_S
        a, b = np.searchsorted(ts, [end - TAU_HI, end - TAU_LO])
        if b <= a:
            continue
        j = np.searchsorted(ts, ts[a:b] - 1.0, "right") - 1
        ok = (j >= 0) & (ts[a:b] - ts[np.maximum(j, 0)] <= lt.C_REF_AGE)
        dx = np.where(ok, lp[a:b] - lp[np.maximum(j, 0)], np.nan)
        with np.errstate(invalid="ignore"):
            cand = np.flatnonzero(np.abs(dx) >= bp * 1e-4)
        nxt = -np.inf
        for i in cand:
            t0 = float(ts[a + i])
            if t0 < nxt:
                continue
            out.append((m.market_id, int(m.start_ts), t0, "Up" if dx[i] > 0 else "Down", m.winner))
            if mode == "first":
                break
            nxt = t0 + SPACING
    return out


def trip(book, mid, start, winner, t0, side, h):
    """(pnl, entry ask, exit bid, settled) of one round trip, or None if the entry cannot fill."""
    end = start + bo.WINDOW_S
    ts = t0 + SEND
    if not _ok(book, mid, ts):
        return None
    ask, size = _ask(book.at(mid, ts + M), side)
    if not (np.isfinite(ask) and 0.02 <= ask <= 0.98 and size >= 5):
        return None
    tx = min(ts + M + h, end - LAST_EXIT)
    bid, bsize = (_bid(book.at(mid, tx + M), side) if _ok(book, mid, tx) else (np.nan, np.nan))
    if np.isfinite(bid) and bsize >= 5 and bid > 0:
        return bid - ask - bo.taker_fee(ask) - bo.taker_fee(bid), ask, bid, False
    won = float(winner == side)
    return won - ask - bo.taker_fee(ask), ask, np.nan, True


def make(markets, spot_trades, sigma, book):
    rows = []
    for bp in BPS:
        for mode in ("first", "all"):
            for mid, start, t0, side, winner in jumps(markets, spot_trades, bp, mode):
                rng = np.random.default_rng([int(bp), int(round(1000 * t0))])
                tr = float(start + bo.WINDOW_S - rng.uniform(TAU_LO, TAU_HI))
                sr = "Up" if rng.random() < 0.5 else "Down"
                for h in HOLDS:
                    for arm, tt, sd in (("jump", t0, side), ("opposite", t0, "Down" if side == "Up" else "Up"),
                                        ("random", tr, sr)):
                        r = trip(book, mid, start, winner, tt, sd, h)
                        if r is not None:
                            rows.append((mid, start, t0, arm, bp, mode, h) + r)
    return pd.DataFrame(rows, columns=["market_id", "start_ts", "t", "arm", "bp", "mode", "h", "pnl", "ask",
                                       "bid", "settled"])


def cl(t):
    if not len(t):
        return np.nan, np.nan, 0, 0
    g = t.groupby(["coin", "market_id"])["pnl"].agg(["sum", "count"])
    mu = g["sum"].sum() / g["count"].sum()
    r = g["sum"] - mu * g["count"]
    k = len(g)
    return mu, np.sqrt((r ** 2).sum() * k / max(k - 1, 1)) / g["count"].sum(), len(t), k


def fmt(t):
    mu, se, n, k = cl(t)
    return f"{100 * mu:+.2f}¢ ±{100 * se:.2f}（{n:,} / {k:,}）" if n else "–"


def report(t, notes):
    span = (pd.to_datetime(t["start_ts"].min(), unit="s", utc=True), pd.to_datetime(t["start_ts"].max(), unit="s", utc=True)) \
        if len(t) else (None, None)
    nm = t["market_id"].nunique() if len(t) else 0
    L = ["# 币安急动后买入、h 秒后在盘口卖出（GitHub 前向录制的毫秒盘口，探索性，事先写定）", "",
         f"市场开始于 {span[0]:%m-%d %H:%M} – {span[1]:%m-%d %H:%M} UTC，{nm:,} 个 BTC 5m 市场。" if nm else "没有数据。", "",
         "触发后 0.11 秒发单、0.375 秒撮合，按撮合时卖一买（≥ 5 份）；成交后 h 秒决定卖出，再 0.375 秒按买一卖（≥ 5 份），"
         "最晚剩 15 秒；两腿都付 taker 费。卖不掉的持有到结算（另计占比）。每份 ± 按市场聚类的标准误（笔数 / 市场数）。"
         "这是 kacho 每秒盘口上最好的那一族（ROUNDTRIP.md 第 1 族）在真实延迟下的复核。", ""]
    for mode, name in (("first", "每个市场第一次"), ("all", "每次（间隔 ≥ 10 秒）")):
        L += [f"## {name}", "", "| 急动 | h 秒 | 顺急动 | 反方向 | 随机时刻、随机方向 | 卖不掉、持有到结算 |", "|---|---:|---:|---:|---:|---:|"]
        for bp in BPS:
            for h in HOLDS:
                s = t[(t["bp"] == bp) & (t["mode"] == mode) & (t["h"] == h)]
                j = s[s["arm"] == "jump"]
                L.append(f"| ≥ {bp:g} bp | {h} | {fmt(j)} | {fmt(s[s['arm'] == 'opposite'])} | "
                         f"{fmt(s[s['arm'] == 'random'])} | {j['settled'].mean():.0%} |" if len(j) else
                         f"| ≥ {bp:g} bp | {h} | – | – | – | – |")
        L.append("")
    if len(t):
        order = t.drop_duplicates(["coin", "market_id"]).sort_values("start_ts")
        half = set(order["market_id"].iloc[: len(order) // 2])
        L += ["## 前后两半（顺急动，每次）", "", "| 急动 | h 秒 | 前半 | 后半 |", "|---|---:|---:|---:|"]
        for bp in BPS:
            for h in (5, 10, 20):
                s = t[(t["bp"] == bp) & (t["mode"] == "all") & (t["h"] == h) & (t["arm"] == "jump")]
                a = s["market_id"].isin(half)
                L.append(f"| ≥ {bp:g} bp | {h} | {fmt(s[a])} | {fmt(s[~a])} |")
        L.append("")
    L += ["## 备注", ""] + [f"- {n}" for n in notes]
    return "\n".join(L) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root")
    ap.add_argument("--out", default="real/rt-ms.md")
    a = ap.parse_args(argv)
    notes = []
    dirs = lt._latency_dirs([a.root], lt.G_COINS)
    t = lt._per_recording(dirs, lt.G_SINCE, lt.G_SPOT, make, KEYS, notes)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(report(t, notes), encoding="utf-8")
    print(Path(a.out).read_text(encoding="utf-8")[:3000])


if __name__ == "__main__":
    main()
