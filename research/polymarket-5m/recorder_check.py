"""Recorder check: how late does a recorder show Polymarket's reaction to a Binance jump?
(market data only: no keys, no orders)

A recorder whose Polymarket book arrives or is stamped late keeps showing the stale ask after a
Binance jump, and every sub-second backtest on it (G, H) reads high: the Dublin export showed the
same markets' asks changing some 50 ms later than the GitHub recordings, the Hong Kong one
0.25-0.5 s later (README section 20). This puts one number on any recorder, so that a recorder
can be checked before its backtests are believed, and watched while it runs.

Jumps: every Binance print with 240..15 s left whose log price moved more than 2 sigma from the
last print 1..5 s earlier (the G / H candidates), the first of each 2 s episode, on a book that
updated within 30 s before the jump and within 10 s after the 5 s horizon. For each jump, the Up
mid the recorder shows h seconds later, signed in the jump's direction:
- response(h): the mid's move by t0 + h summed over the jumps, over its move by t0 + 5 s summed
  the same way (the share of the eventual reaction already shown at h);
- first move: seconds until the mid first moves 1c or more the jump's way (median and 90%, over
  the jumps where it does within 5 s).
Also the recorder's receive time minus the exchange's timestamp over all book rows (needs both
clocks right; GitHub runners are far from London, so theirs is large by design and only its
spread matters).

Two recorders of the same markets over the same hours, triggered by the same Binance trades,
compare directly: the later one shows a lower response at 0.3-0.5 s and a later first move.
Across different days the market's own speed may differ.

    python recorder_check.py DIR [DIR ...] [--spot binance|binancews|coinbase] [--since UTC] [--until UTC]
                             [--out recorder-check.md]
    python recorder_check.py ROOT --github [--since UTC] [--out real/recorder-check.md]

DIR is anything latency.load reads (a shadow export, or a GitHub recording's bundle-btc/latency);
with --github, ROOT holds the forward recordings and each one is checked on its own book, then
pooled. From Python, fingerprint(books, spot_trades, markets) takes the frames directly, so the
same Binance trades (for example Binance's daily archive) can trigger the check on every recorder.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

import binary as bo
import latency as lt

HS = (0.1, 0.2, 0.3, 0.4, 0.5, 1.0, 2.0)
HORIZON, STEP, GAP = 5.0, 0.01, 2.0


def jump_rows(books, spot_trades, markets, z0=lt.G_Z0, tau_lo=lt.G_TAU_LO):
    """One row per jump: market, t0, sign, the signed mid move at each h in HS and at HORIZON, and
    the seconds to the first 1c move the jump's way (NaN if none within HORIZON)."""
    _, sigma = lt.spot_grid(spot_trades, max_gap=lt.C_MAX_GAP)
    book = lt.Book(books)
    ts = spot_trades["trade_ts"].to_numpy(float)
    lp = np.log(spot_trades["price"].to_numpy(float))
    rows = []
    for m in markets.itertuples():
        if m.market_id not in book.by:
            continue
        bts, bv = book.by[m.market_id]
        end = m.start_ts + bo.WINDOW_S
        a, b = np.searchsorted(ts, [end - lt.TAUS[0], end - tau_lo])
        if b <= a:
            continue
        j = np.searchsorted(ts, ts[a:b] - 1.0, "right") - 1
        ok = (j >= 0) & (ts[a:b] - ts[np.maximum(j, 0)] <= lt.C_REF_AGE)
        dx = np.where(ok, lp[a:b] - lp[np.maximum(j, 0)], np.nan)
        sg = sigma.reindex(np.floor(ts[a:b]).astype("int64") - 1).to_numpy()
        with np.errstate(invalid="ignore"):
            cand = np.flatnonzero(np.abs(dx) > z0 * sg)
        nxt = -np.inf
        for i in cand:
            t0 = float(ts[a + i])
            if t0 < nxt:
                continue
            nxt = t0 + GAP
            if not book.alive(m.market_id, t0, 30.0, HORIZON + 10.0) or \
                    not book.alive(m.market_id, t0 + HORIZON, HORIZON + 30.0, 10.0):
                continue
            k = np.searchsorted(bts, [t0] + [t0 + h for h in HS] + [t0 + HORIZON], "right") - 1
            if k[0] < 0:
                continue
            mid = (bv[k, 0] + bv[k, 1]) / 2
            if not np.isfinite(mid).all():
                continue
            s = float(np.sign(dx[i]))
            d = s * (mid[1:] - mid[0])
            i0, i1 = np.searchsorted(bts, [t0, t0 + HORIZON], "right")
            path = s * ((bv[i0:i1, 0] + bv[i0:i1, 1]) / 2 - mid[0])
            hit = np.flatnonzero(path >= STEP - 1e-9)
            first = float(bts[i0 + hit[0]] - t0) if len(hit) else np.nan
            rows.append((m.market_id, t0, s, *d, first))
    cols = ["market_id", "t0", "sign"] + [f"d{h:g}" for h in HS] + ["d_end", "first"]
    return pd.DataFrame(rows, columns=cols)


def summarize(r, books=None):
    """response(h) for HS, first-move median / 90%, and receive - exchange time p50 / p90 / p99 (ms)."""
    out = {"jumps": len(r)}
    den = r["d_end"].sum() if len(r) else np.nan
    for h in HS:
        out[f"r{h:g}"] = r[f"d{h:g}"].sum() / den if len(r) and den > 0 else np.nan
    f = r["first"].dropna() if len(r) else pd.Series(dtype=float)
    out["moved"] = len(f)
    out["first50"], out["first90"] = (f.quantile(0.5), f.quantile(0.9)) if len(f) else (np.nan, np.nan)
    if books is not None and "recv" in books and len(books):
        lag = 1000 * (books["recv"] - books["ts"]).dropna()
        out.update({f"lag{q}": lag.quantile(q / 100) for q in (50, 90, 99)})
    return out


def fingerprint(books, spot_trades, markets):
    """summarize(jump_rows(...)) for one recorder's frames (latency.load's books, spot trades and
    markets)."""
    return summarize(jump_rows(books, spot_trades, markets), books)


def _window(markets, since, until):
    if since:
        markets = markets[markets["start_ts"] >= pd.Timestamp(since, tz="UTC").timestamp()]
    if until:
        markets = markets[markets["start_ts"] < pd.Timestamp(until, tz="UTC").timestamp()]
    return markets


def _row(name, s):
    resp = " | ".join(f"{100 * s[f'r{h:g}']:.0f}%" if np.isfinite(s[f"r{h:g}"]) else "–" for h in HS)
    first = (f"{1000 * s['first50']:.0f} / {1000 * s['first90']:.0f}" if np.isfinite(s["first50"]) else "–")
    lag = (f"{s['lag50']:.0f} / {s['lag90']:.0f} / {s['lag99']:.0f}" if "lag50" in s else "–")
    return f"| {name} | {s['jumps']:,} | {resp} | {first} | {lag} |"


def report(parts, title, notes=()):
    """parts: [(name, jump rows, books)]; the first row pools them all."""
    L = [f"# {title}", "",
         "币安 2σ 急动（G/H 的候选，剩 240–15 秒，每 2 秒一段只取第一笔）之后，录制机显示的 Up 中间价朝急动方向动了多少，"
         "占 5 秒后总反应的比例；越低说明录制机显示得越晚（同一批市场、同一段时间才能直接比）。"
         "首次变动 = 中间价第一次朝急动方向动 1¢ 的时间（中位 / 90%）。收到 − 交易所时间 = 录制机收到盘口消息的时间减去 "
         "Polymarket 自己的时间戳（要两边时钟都准；GitHub 录制机离伦敦远，这一项本来就大，只看它稳不稳）。", "",
         "| 录制 | 急动 | " + " | ".join(f"+{1000 * h:.0f} ms" for h in HS) + " | 首次变动 ms | 收到 − 交易所时间 ms（中位 / 90% / 99%） |",
         "|---|---:|" + "---:|" * len(HS) + "---:|---:|"]
    if len(parts) > 1:
        pooled = pd.concat([p[1] for p in parts], ignore_index=True)
        allb = pd.concat([p[2][["ts", "recv"]] for p in parts if p[2] is not None and "recv" in p[2]], ignore_index=True) \
            if any(p[2] is not None and "recv" in p[2] for p in parts) else None
        L.append(_row("合计", summarize(pooled, allb)))
    for name, r, b in parts:
        L.append(_row(name, summarize(r, b)))
    if notes:
        L += ["", "备注：", ""] + [f"- {n}" for n in notes]
    return "\n".join(L) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dirs", nargs="+")
    ap.add_argument("--github", action="store_true", help="dirs are roots of GitHub forward recordings")
    ap.add_argument("--spot", default=None, choices=sorted(lt.SPOT),
                    help="trigger feed (default: binancews with --github, else binance)")
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--out", default="recorder-check.md")
    a = ap.parse_args(argv)
    spot = a.spot or ("binancews" if a.github else "binance")
    parts, notes = [], []
    if a.github:
        for coin, dirs in sorted(lt._latency_dirs(a.dirs, ("btc",)).items()):
            for d in dirs:
                run = Path(d).parent.parent.parent.name
                try:
                    books, markets, sp = lt.load(d, spot=spot)
                except Exception as e:
                    notes.append(f"{run}: 跳过（{type(e).__name__}: {e}）")
                    continue
                markets = _window(markets, a.since, a.until)
                books = books[books["market_id"].isin(set(markets["market_id"]))]
                if markets.empty or books.empty or len(sp) < 2:
                    notes.append(f"{run}: 这段时间没有市场")
                    continue
                parts.append((f"GitHub {run}", jump_rows(books, sp, markets), books))
        title = f"录制机体检：GitHub 前向录制（{a.since or '全部'} 起，{spot} 触发）"
    else:
        books, markets, sp = lt.load(*a.dirs, spot=spot)
        markets = _window(markets, a.since, a.until)
        books = books[books["market_id"].isin(set(markets["market_id"]))]
        parts.append((", ".join(Path(d).name for d in a.dirs), jump_rows(books, sp, markets), books))
        title = f"录制机体检：{parts[0][0]}（{a.since or '最早'} → {a.until or '最晚'}，{spot} 触发）"
    text = report(parts, title, notes)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
