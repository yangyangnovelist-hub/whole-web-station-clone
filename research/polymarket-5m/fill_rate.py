"""How often a stale ask seen at decision time is still there when the order is matched, on the
GitHub forward books (exploratory; market data only, no orders).

The capped real-money H pilot (user's local session, 2026-10-03; ralph/progress.txt) sent 10 FAK
orders and got 1 fill: 9 asks were gone by the time the orders were matched (send -> match
374.89 ms for the one fill, send -> response p50 310 ms). The same question on the books:
for every H candidate (latency.gated_trades with anchor=2, every=2, the test I rule) whose edge
fair - ask - fee >= theta at the decision time t0 + D (D: the Binance print reaching the server
and the order being sent, about 0.11 s), is the edge still >= theta at t0 + L, the match time?
The fill rate as a function of L, set against the pilot's 1 in 10, says roughly what L the
pilot's orders actually had; the EV of the filled ones says what that L is worth.

    python fill_rate.py ROOT [--decision 0.11] [--lags 0.2 0.25 0.3 0.35 0.4 0.45 0.5 0.6]
                        [--out real/fill-rate.md]
"""
import argparse
from pathlib import Path

import numpy as np
import pandas as pd

import binary as bo
import latency as lt


def sends(markets, spot_trades, sigma, book, decision, lags, theta=None, z0=lt.G_Z0,
          tau_lo=lt.G_TAU_LO, anchor=2.0, every=2.0):
    """One row per candidate print (as gated_trades, H prior) with a quote at t0 + decision: its
    edge then (sent when it clears theta; the next candidate counts only `every` s after a send),
    and for each lag whether the edge still clears theta at t0 + lag (filled), the ask and size
    then, and the outcome."""
    from scipy.stats import norm
    theta = lt.G_THETA if theta is None else theta
    ts = spot_trades["trade_ts"].to_numpy()
    lp = np.log(spot_trades["price"].to_numpy())
    rows = []
    for m in markets.itertuples():
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
        next_t = -np.inf
        for i in cand:
            t0 = float(ts[a + i])
            if t0 < next_t:
                continue
            r, q = book.at(m.market_id, t0), book.at(m.market_id, t0 - anchor)
            ja = np.searchsorted(ts, t0 - anchor, "right") - 1
            if r is None or q is None or ja < 0 or not np.isfinite([r[0], r[1], q[0], q[1]]).all():
                continue
            fac = float(bo.twap_std_factor(t0 - m.start_ts)) * sg[i]
            if not (np.isfinite(fac) and fac > 0):
                continue
            up = dx[i] > 0
            prior = min(max((q[0] + q[1]) / 2, 0.005), 0.995)
            p1 = float(norm.cdf(norm.ppf(prior) + (lp[a + i] - lp[ja]) / fac))
            fair = p1 if up else 1 - p1
            side = "Up" if up else "Down"
            if not book.alive(m.market_id, t0 + max(lags), *lt.C_ALIVE) or \
                    book.across_close(m.market_id, t0, t0 + max(lags)):
                continue
            pd_, _ = book.side_ask(m.market_id, t0 + decision, side)
            if not (np.isfinite(pd_) and 0.02 <= pd_ <= 0.98):
                continue
            edge_d = fair - pd_ - bo.taker_fee(pd_)
            row = {"market_id": m.market_id, "t": t0, "side": side, "fair": fair, "ask_d": pd_,
                   "edge_d": edge_d, "sent": bool(edge_d >= theta), "won": float(m.winner == side)}
            for lag in lags:
                px, size = book.side_ask(m.market_id, t0 + lag, side)
                fill = bool(np.isfinite(px) and fair - px - bo.taker_fee(px) >= theta)
                row[f"fill{lag:g}"], row[f"ask{lag:g}"], row[f"size{lag:g}"] = fill, px, size
            rows.append(row)
            if row["sent"]:
                next_t = t0 + every
    return pd.DataFrame(rows)


def run(root, decision, lags):
    dirs = lt._latency_dirs([root], lt.G_COINS)
    return lt._per_recording(dirs, lt.G_SINCE, lt.G_SPOT,
                             lambda mk, sp, sg, bk: sends(mk, sp, sg, bk, decision, lags),
                             ["coin", "market_id", "t"], [])


def ev_per_send(t, lag, theta_send, theta_lim, shares=None):
    """Mean P&L per sent order (cents a share, or dollars with `shares` = cap per order) when an
    order is sent if the decision-time edge >= theta_send, with its limit at fair - theta_lim
    (it fills if the ask at the match leaves an edge >= theta_lim), the rest are killed."""
    s = t[t["edge_d"] >= theta_send].sort_values(["market_id", "t"])
    if not len(s):
        return np.nan, np.nan, 0
    keep, last_m, last_t = [], None, -np.inf  # a market's next send at least `every` (2 s) later
    for m, tt in zip(s["market_id"], s["t"]):
        if m != last_m:
            last_m, last_t = m, -np.inf
        ok = tt >= last_t + 2.0
        keep.append(ok)
        if ok:
            last_t = tt
    s = s[np.array(keep)]
    px = s[f"ask{lag:g}"]
    fee = bo.taker_fee(px)
    fill = np.isfinite(px) & (s["fair"] - px - fee >= theta_lim)
    pnl = np.where(fill, s["won"] - px - fee, 0.0)
    if shares is not None:
        pnl = pnl * np.minimum(s[f"size{lag:g}"].fillna(0), shares)
    return float(np.mean(pnl)), float(fill.mean()), len(s)


def report(t, decision, lags):
    L = ["# 盘口上的“下单时还在、撮合时还在吗”（GitHub 前向录制，探索性）", "",
         f"H 规则（test I 的信号，每次都加、2 秒间隔）的每个候选：触发后 {1000 * decision:.0f} ms（币安到都柏林约 105 ms 加发单）"
         "时边际 ≥ 12¢ 才算发单；撮合时刻（延迟 L，含 150 ms 冻结）边际仍 ≥ 12¢ 才算成交。"
         "真单试点 10 张发单只成交 1 张，下表看在哪个 L 上成交率会低到这个程度。", "",
         f"发单 {len(t):,} 次，{t['market_id'].nunique() if len(t) else 0:,} 个市场。", "",
         "| 撮合延迟 L | 成交率 | 成交的每份 | 至少 5 份时的成交率 | 10 张里成交 ≤ 1 张的概率 |",
         "|---|---:|---:|---:|---:|"]
    from scipy.stats import binom
    allc, t = t, t[t["sent"]] if len(t) else t
    for lag in lags:
        if not len(t):
            break
        f = t[f"fill{lag:g}"].astype(bool)
        f5 = f & (t[f"size{lag:g}"] >= 5)
        px = t.loc[f, f"ask{lag:g}"]
        ev = (t.loc[f, "won"] - px - bo.taker_fee(px)).mean() if f.any() else np.nan
        L.append(f"| {lag:g} 秒 | {f.mean():.0%} | {100 * ev:+.1f}¢ | {f5.mean():.0%} | {binom.cdf(1, 10, f.mean()):.3f} |")
    if not len(allc):
        return "\n".join(L) + "\n"
    days = (allc["t"].max() - allc["t"].min()) / 86400
    lims = (0.12, 0.09, 0.06, 0.04, 0.02, 0.0)
    L += ["", "## 执行策略：触发时边际 ≥ 12¢ 才发，限价放到 公平价 − θ（每张发出的单平均赚多少，¢/份；括号里是成交率）", "",
          "| 撮合延迟 L | " + " | ".join(f"θ = {100 * x:.0f}¢" for x in lims) + " |", "|---|" + "---:|" * len(lims)]
    for lag in lags:
        cells = []
        for lim in lims:
            ev, fr, _ = ev_per_send(allc, lag, 0.12, lim)
            cells.append(f"{100 * ev:+.2f}（{fr:.0%}）")
        L.append(f"| {lag:g} 秒 | " + " | ".join(cells) + " |")
    sends = (0.06, 0.09, 0.12, 0.15)
    L += ["", "## 发单门槛和限价一起换（每天的钱，美元；每张最多 20 份、不超过卖一挂单量）", "",
          f"录制覆盖约 {days:.1f} 天。", "",
          "| 撮合延迟 L | 发单门槛 | " + " | ".join(f"θ = {100 * x:.0f}¢" for x in lims) + " |", "|---|---|" + "---:|" * len(lims)]
    for lag in (0.3, 0.45):
        if lag not in lags:
            continue
        for snd in sends:
            cells = []
            for lim in lims:
                if lim > snd:
                    cells.append("–")
                    continue
                ev, _, n = ev_per_send(allc, lag, snd, lim, shares=20)
                cells.append(f"{ev * n / max(days, 1e-9):+.0f}")
            L.append(f"| {lag:g} 秒 | {100 * snd:.0f}¢ | " + " | ".join(cells) + " |")
    late = allc[~allc["sent"]]
    L += ["", "## 触发时还不够便宜、撮合时才变便宜的那些（实盘看不到、不会发；回测曲线把它们也算进去了）", "",
          "| 撮合延迟 L | 笔数 | 每份 |", "|---|---:|---:|"]
    for lag in lags:
        f = late[f"fill{lag:g}"].astype(bool)
        px = late.loc[f, f"ask{lag:g}"]
        ev = (late.loc[f, "won"] - px - bo.taker_fee(px)).mean() if f.any() else np.nan
        L.append(f"| {lag:g} 秒 | {int(f.sum()):,} | {100 * ev:+.1f}¢ |")
    return "\n".join(L) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root")
    ap.add_argument("--decision", type=float, default=0.11)
    ap.add_argument("--lags", type=float, nargs="+", default=[0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5, 0.6])
    ap.add_argument("--out", default="real/fill-rate.md")
    a = ap.parse_args(argv)
    text = report(run(a.root, a.decision, a.lags), a.decision, a.lags)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
