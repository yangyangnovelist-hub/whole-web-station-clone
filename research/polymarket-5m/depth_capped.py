"""Depth taking with a share cap per trade, from real/cross-gated-depth.csv.gz (local).

The report's own depth table sums every qualifying ask uncapped, and a few resting walls of tens
of thousands of shares behind the best ask decide those totals. Here each trade takes the best ask
first (up to the cap) and then the deeper asks that still pay at least theta_d, at their average
profit per share (deeper asks cost more, so the cheapest of them is somewhat better than that
average: conservative). No haircut for other takers at the same prices.

    python depth_capped.py [csv] > real/cross-gated-depth-capped.txt
"""
import sys

import numpy as np
import pandas as pd

path = sys.argv[1] if len(sys.argv) > 1 else "real/cross-gated-depth.csv.gz"
d = pd.read_csv(path)
c = d[(d["lag"] == 300) & (d["theta"] == 0.12)].copy()
c["period"] = np.select([c["day"] <= "2026-07-15", c["day"] <= "2026-08-16"], ["A 5/25-7/15", "B 7/16-8/16"], "C 8/17-8/29")
rows = []
for rule, g0 in (("F", c), ("G", c[c["pre_move"] < 0.03])):
    for per, g in g0.groupby("period"):
        nd = g["day"].nunique()
        for cap in (40, 100, 200):
            l1 = np.minimum(g["size"], cap)
            base = (g["pnl"] * l1).sum()
            r = {"rule": rule, "period": per, "cap": cap, "trades/day": round(len(g) / nd),
                 "L1 shares": round(l1.mean()), "L1 c/share": round(100 * base / l1.sum(), 2), "L1 $/day": round(base / nd)}
            for dd in (12, 8, 4):
                extra_sh = (g[f"sh_d{dd}"] - g["size"]).clip(lower=0)
                extra_pnl = g[f"pnl_d{dd}"] - g["pnl"] * g["size"]
                per_sh = np.where(extra_sh > 0, extra_pnl / extra_sh.replace(0, np.nan), 0.0)
                add = np.minimum(extra_sh, cap - l1)
                sh, pnl = l1 + add, g["pnl"] * l1 + add * per_sh
                r.update({f"d{dd} shares": round(sh.mean()), f"d{dd} c/share": round(100 * pnl.sum() / sh.sum(), 2),
                          f"d{dd} $/day": round(pnl.sum() / nd)})
            rows.append(r)
pd.set_option("display.width", 250)
pd.set_option("display.max_columns", 30)
print(__doc__.strip(), "\n")
print(pd.DataFrame(rows).to_string(index=False))
