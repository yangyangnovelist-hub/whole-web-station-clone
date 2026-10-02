"""Test G's live recordings, re-read with H and the scale-in versions (exploratory, not preregistered).

The same recordings, markets (from latency.G_SINCE), Binance feed, health and disconnect checks
as test G (latency.run_test_g); only the rule changes: H (prior 2 s before), F (no skip), and G, H
keeping every fill 2 s apart, at each lag. polymarket-shadow runs it after the tests on the
recordings it downloads (ROOT/<run id>/x/bundle-*/latency).

    python forward_variants.py ROOT [--lags 0.3 0.4] [--out real/forward-variants.md]
"""
import argparse
from pathlib import Path

import numpy as np

import binary as bo
import latency as lt

VARIANTS = {
    "G 首笔（检验 G）": (dict(max_pre=lt.G_MAX_PRE), ["coin", "market_id"]),
    "H 首笔": (dict(anchor=2.0), ["coin", "market_id"]),
    "F 首笔（同一币安源）": (dict(), ["coin", "market_id"]),
    "G 每次都加": (dict(max_pre=lt.G_MAX_PRE, every=2.0), ["coin", "market_id", "t"]),
    "H 每次都加": (dict(anchor=2.0, every=2.0), ["coin", "market_id", "t"]),
}


def run(root, lag):
    dirs = lt._latency_dirs([root], lt.G_COINS)
    kw = dict(lag=lag, theta=lt.G_THETA, z0=lt.G_Z0, tau_lo=lt.G_TAU_LO)
    out = {}
    for name, (extra, keys) in VARIANTS.items():
        t = lt._per_recording(dirs, lt.G_SINCE, lt.G_SPOT,
                              lambda mk, sp, sg, bk, e=extra: lt.gated_trades(mk, sp, sg, bk, **kw, **e), keys, [])
        out[name] = t.sort_values("t") if len(t) else t
    return out


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
    a = ap.parse_args(argv)
    res = {lag: run(a.root, lag) for lag in a.lags}
    L = ["# 检验 G 的录盘，换成 H 和加仓版本再算（探索性，不是预注册检验；每轮录盘后自动更新）", "",
         f"和检验 G 完全相同的录盘（{lt.G_SINCE} UTC 起开始的 BTC 5m 市场）、同一币安源、同样的盘口健康和断线检查，只换规则。"
         "几个版本是一起看的，最好的那个有挑选偏差；正式判定仍是检验 G 的 600 笔。", "",
         "| 版本 | 延迟 | 笔数 | 每份（±标准误） | ROI | 胜率 | p |", "|---|---|---:|---:|---:|---:|---:|"]
    for name in VARIANTS:
        for lag in a.lags:
            L.append(f"| {name} | {lag:g} 秒 | {cell(res[lag][name])} |")
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
