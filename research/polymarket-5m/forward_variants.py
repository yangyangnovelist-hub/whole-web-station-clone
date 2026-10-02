"""Test G's live recordings, re-read with H and the scale-in versions (exploratory, not preregistered).

The same recordings, markets (from latency.G_SINCE), Binance feed, health and disconnect checks
as test G (latency.run_test_g); only the rule changes: H (prior 2 s before), F (no skip), and G, H
keeping every fill 2 s apart. Download the polymarket-forward artifacts as polymarket-shadow does
(recording-<run id>/raw.tgz, bundle-*/latency and raw/*/errors.jsonl*) into ROOT/<run id>/x.

    python forward_variants.py [lag] [ROOT]
"""
import sys

import numpy as np
import pandas as pd
import binary as bo
import latency as lt

root = sys.argv[2] if len(sys.argv) > 2 else "rec"
dirs = lt._latency_dirs([root], lt.G_COINS)
kw = dict(lag=float(sys.argv[1]) if len(sys.argv) > 1 else lt.G_LAG, theta=lt.G_THETA, z0=lt.G_Z0, tau_lo=lt.G_TAU_LO)
variants = {
    "G 首笔（检验 G）": (dict(max_pre=lt.G_MAX_PRE), ["coin", "market_id"]),
    "H 首笔": (dict(anchor=2.0), ["coin", "market_id"]),
    "F 首笔（同一币安源）": (dict(), ["coin", "market_id"]),
    "G 每次都加": (dict(max_pre=lt.G_MAX_PRE, every=2.0), ["coin", "market_id", "t"]),
    "H 每次都加": (dict(anchor=2.0, every=2.0), ["coin", "market_id", "t"]),
}
out = {}
for name, (extra, keys) in variants.items():
    notes = []
    t = lt._per_recording(dirs, lt.G_SINCE, lt.G_SPOT, lambda mk, sp, sg, bk, e=extra: lt.gated_trades(mk, sp, sg, bk, **kw, **e), keys, notes)
    t = t.sort_values("t")
    cost = t["price"] + t["fee"]
    p = bo.fair_price_pvalue(t["pnl"].to_numpy(), cost.to_numpy(), sims=20000) if len(t) >= 10 and t["pnl"].mean() > 0 else 1.0
    se = t["pnl"].std() / np.sqrt(len(t))
    hour = pd.to_datetime(t["t"], unit="s", utc=True).dt.floor("6h")
    print(f"{name:<16} {len(t):>4} 笔 胜率 {t['won'].mean():.1%} 均价 {t['price'].mean():.3f} 每份 {100*t['pnl'].mean():+.2f}¢ ±{100*se:.1f} "
          f"ROI {100*t['pnl'].sum()/cost.sum():+.1f}% p={p:.3f} | 每 6 小时: " +
          " ".join(f"{h:%m-%d %H}h {len(g)}笔{100*g['pnl'].mean():+.1f}¢" for h, g in t.groupby(hour)))
    out[name] = t
