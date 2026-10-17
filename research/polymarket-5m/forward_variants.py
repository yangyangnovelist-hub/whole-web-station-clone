"""Test G's live recordings, re-read with H and the scale-in versions (exploratory, not preregistered).

The same recordings, markets (from latency.G_SINCE), Binance feed, health and disconnect checks
as test G (latency.run_test_g); only the rule changes: H (prior 2 s before), F (no skip), and G, H
keeping every fill 2 s apart, at each lag. polymarket-shadow runs it after the tests on the
recordings it downloads (ROOT/<run id>/x/bundle-*/latency).

    python forward_variants.py ROOT [--lags 0.3 0.4] [--out real/forward-variants.md]
                               [--trades real/forward-trades.csv.gz]
                               [--curve 0.2 0.25 0.3 0.35 0.4 0.45 0.5 --curve-out real/forward-latency-curve.md]

--trades also writes every trade (variant, lag, market slug, trigger time, side, price, won, pnl),
so the markets can be matched one by one against another recorder of the same markets.

"反向 2 倍" rows reweight the "每次都加" trades: 1 share for a market's first trade and later ones on
its side, 2 for later ones on the other side (latency.opp_weights; at 0.4 s from I_SINCE on, the
H row is test I's rule); their standard error is clustered by market and p is the one-sided
normal p (latency.clustered), not the fair-price simulation of the other rows.

--curve: G's first trade, H buying every fill and H with opposite adds at twice the size at each
of those lags (seconds from the Binance print's exchange time to the fill, the 150 ms taker hold
included), all with standard errors clustered by market: what each 50 ms of latency is worth on
the GitHub books, and the lag past which a trade is no longer worth sending.
"""
import argparse
from pathlib import Path

import numpy as np
import pandas as pd

import binary as bo
import latency as lt

VARIANTS = {
    "G 首笔（检验 G）": (dict(max_pre=lt.G_MAX_PRE), ["coin", "market_id"]),
    "H 首笔": (dict(anchor=2.0), ["coin", "market_id"]),
    "F 首笔（同一币安源）": (dict(), ["coin", "market_id"]),
    "G 每次都加": (dict(max_pre=lt.G_MAX_PRE, every=2.0), ["coin", "market_id", "t"]),
    "H 每次都加": (dict(anchor=2.0, every=2.0), ["coin", "market_id", "t"]),
}


def run(root, lag, names=None):
    dirs = lt._latency_dirs([root], lt.G_COINS)
    kw = dict(lag=lag, theta=lt.G_THETA, z0=lt.G_Z0, tau_lo=lt.G_TAU_LO)
    out = {}
    for name, (extra, keys) in VARIANTS.items():
        if names is not None and name not in names:
            continue
        t = lt._per_recording(dirs, lt.G_SINCE, lt.G_SPOT,
                              lambda mk, sp, sg, bk, e=extra: lt.gated_trades(mk, sp, sg, bk, **kw, **e), keys, [])
        out[name] = t.sort_values("t") if len(t) else t
    return out


OPP2 = {"G 反向 2 倍": "G 每次都加", "H 反向 2 倍（检验 I 的规则）": "H 每次都加"}


def opp2_cell(t):
    if not len(t):
        return "0 | – | – | – | –"
    w = lt.opp_weights(t)
    c, se, p, _ = lt.clustered(t, w)
    cost = t["price"] + t["fee"]
    return (f"{len(t):,} | {c:+.2f}¢ ±{se:.1f} | {100 * (w * t['pnl']).sum() / (w * cost).sum():+.1f}% | "
            f"{t['won'].mean():.0%} | {p:.3f}")


CURVE = ("G 首笔（检验 G）", "H 每次都加")


def curve_cell(t, w):
    if not len(t):
        return "–"
    c, se, _, _ = lt.clustered(t, w)
    return f"{c:+.1f}¢ ±{se:.1f}（{len(t)}）"


def curve_lines(res):
    L = ["# 优势随延迟怎么变（GitHub 前向录制的盘口；探索性，每轮录盘后自动更新）", "",
         f"和检验 G 相同的录盘（{lt.G_SINCE} UTC 起的 BTC 5m 市场）、同一币安源和数据处理。延迟 = 从币安成交的交易所时间"
         "到我们的单按当时的卖一成交，**包括 Polymarket 的 150 ms 吃单冻结**；所以 0.3 秒相当于币安成交后 150 ms 内单子已到 "
         "Polymarket。每格：每份赚的钱 ± 按市场聚类的标准误（笔数）。", "",
         "| 延迟 | G 首笔 | H 每次都加 | H 反向 2 倍 |", "|---|---:|---:|---:|"]
    for lag in sorted(res):
        g, h = res[lag].get(CURVE[0], pd.DataFrame()), res[lag].get(CURVE[1], pd.DataFrame())
        one = lambda t: pd.Series(1.0, index=t.index)
        L.append(f"| {lag:g} 秒 | {curve_cell(g, one(g))} | {curve_cell(h, one(h))} | "
                 f"{curve_cell(h, lt.opp_weights(h)) if len(h) else '–'} |")
    return L


def cell(t, reps=20000):
    if not len(t):
        return "0 | – | – | – | –"
    cost = t["price"] + t["fee"]
    p = bo.fair_price_pvalue(t["pnl"].to_numpy(), cost.to_numpy(), sims=reps) if len(t) >= 10 and t["pnl"].mean() > 0 else 1.0
    se = t["pnl"].std() / np.sqrt(len(t)) if len(t) > 1 else np.nan
    return (f"{len(t):,} | {100 * t['pnl'].mean():+.2f}¢ ±{100 * se:.1f} | {100 * t['pnl'].sum() / cost.sum():+.1f}% | "
            f"{t['won'].mean():.0%} | {p:.3f}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root")
    ap.add_argument("--lags", type=float, nargs="+", default=[0.3, 0.4])
    ap.add_argument("--out", default="real/forward-variants.md")
    ap.add_argument("--trades", help="also write every trade here (csv.gz)")
    ap.add_argument("--curve", type=float, nargs="+", help="lags (s) for the edge-against-latency table")
    ap.add_argument("--curve-out", default="real/forward-latency-curve.md")
    a = ap.parse_args(argv)
    res = {lag: run(a.root, lag) for lag in a.lags}
    if a.curve:
        cur = {lag: res[lag] if lag in res else run(a.root, lag, CURVE) for lag in a.curve}
        Path(a.curve_out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.curve_out).write_text("\n".join(curve_lines(cur)) + "\n", encoding="utf-8")
    L = ["# 检验 G 的录盘，换成 H 和加仓版本再算（探索性，不是预注册检验；每轮录盘后自动更新）", "",
         f"和检验 G 完全相同的录盘（{lt.G_SINCE} UTC 起开始的 BTC 5m 市场）、同一币安源、同样的盘口健康和断线检查，只换规则。"
         f"几个版本是一起看的，最好的那个有挑选偏差；正式判定是检验 G 的 {lt.G_N} 笔和检验 I"
         f"（{lt.I_SINCE} UTC 起前 {lt.I_N:,} 个市场，H 反向 2 倍、{lt.I_LAG:g} 秒，见 latency-test-i.md）。", "",
         "“反向 2 倍”两行是把“每次都加”的成交重新加权（反方向的加仓 2 份），标准误按市场聚类、p 用正态近似。", "",
         "| 版本 | 延迟 | 笔数 | 每份（±标准误） | ROI | 胜率 | p |", "|---|---|---:|---:|---:|---:|---:|"]
    for name in VARIANTS:
        for lag in a.lags:
            L.append(f"| {name} | {lag:g} 秒 | {cell(res[lag][name])} |")
    for name in OPP2:
        for lag in a.lags:
            L.append(f"| {name} | {lag:g} 秒 | {opp2_cell(res[lag][OPP2[name]])} |")
    if a.trades:
        rows = [t.assign(variant=name, lag=lag) for lag, r in res.items() for name, t in r.items() if len(t)]
        cols = ["variant", "lag", "run", "market_id", "t", "side", "price", "fee", "size", "won", "pnl", "p0", "fair"]
        pd.concat(rows, ignore_index=True)[cols].to_csv(a.trades, index=False)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
