"""Walk-forward version of multifactor.py, with and without scaling in (local).

From the 15th day of real/cross-jitter.csv.gz on, the gradient-boosted model is retrained every
7 days on all earlier days and predicts only the next 7, so every prediction is out of sample.
A trade buys the move's side at the ask 300 ms after a Binance jump when p - ask - fee >= theta;
"first only" keeps the first per market, "scale in" keeps up to 3 or all qualifying jumps in a
market (each at its own stale ask). z is clustered by market (one sum per market).

    python multifactor_wf.py > real/multifactor-wf.txt
"""
import numpy as np, pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
import multifactor as mf

d = pd.read_csv("real/cross-jitter.csv.gz")
d = d[d["ok"]].copy()
d["period"] = np.select([d["day"] <= "2026-07-15", d["day"] <= "2026-08-16"], ["A", "B"], "C")
d = d.dropna(subset=["p0", "fair_up", "ua_l", "da_l", "up_won"]).sort_values("t0").reset_index(drop=True)
X = mf.features(d)
y = np.where(d["up"] == 1, d["up_won"], 1 - d["up_won"])
days = sorted(d["day"].unique())
p = np.full(len(d), np.nan)
# walk-forward: from the 15th day on, retrain every 7 days on all earlier days, predict the next 7
start = 14
for k in range(start, len(days), 7):
    train = d["day"] < days[k]
    test = d["day"].isin(days[k:k + 7])
    m = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.04, max_leaf_nodes=15, min_samples_leaf=200,
                                       l2_regularization=1.0, random_state=0).fit(X[train], y[train])
    p[test.to_numpy()] = m.predict_proba(X[test])[:, 1]
d["p"] = p
print("first walk-forward day", days[start])

def all_trades(d, p, theta, max_per_market=None):
    up = d["up"].to_numpy() == 1
    a_side = np.where(up, d["ua_l"], d["da_l"])
    e = p - a_side - 0.07 * a_side * (1 - a_side)
    ok = np.isfinite(e) & (a_side >= 0.02) & (a_side <= 0.98) & (e >= theta)
    t = pd.DataFrame({"market_id": d["market_id"], "t0": d["t0"], "period": d["period"], "day": d["day"],
                      "price": a_side, "won": np.where(up, d["up_won"], 1 - d["up_won"]),
                      "size": np.where(up, d["uas_l"], d["das_l"]), "up": up})[ok].sort_values("t0")
    if max_per_market is not None:
        t = t.groupby("market_id").head(max_per_market)
    t["fee"] = 0.07 * t["price"] * (1 - t["price"])
    t["pnl"] = t["won"] - t["price"] - t["fee"]
    return t

def report(t, label):
    rows = []
    for per in ("A", "B", "C"):
        g = t[(t["period"] == per) & (t["day"] >= days[start])]
        if g.empty:
            continue
        mk = g.groupby("market_id")["pnl"].sum()
        z = mk.mean() / (mk.std(ddof=1) / np.sqrt(len(mk))) if len(mk) > 2 else np.nan
        nd = g["day"].nunique()
        usd = (g["pnl"] * g["size"].clip(upper=30)).sum() / nd
        roi = 100 * g["pnl"].sum() / (g["price"] + g["fee"]).sum()
        rows.append(f"{per}: {len(g):,} 笔 / {len(mk):,} 个市场，每天 {len(g) / nd:.0f} 笔，均价 {g['price'].mean():.3f}，"
                    f"每份 {100 * g['pnl'].mean():+.2f}¢，每投 1 美元 {roi:+.1f} 美分，卖一数量中位 {g['size'].median():.0f}，"
                    f"按市场聚类 z = {z:.1f}，每笔最多 30 份约 ${usd:,.0f}/天")
    print(f"\n{label}"); print("\n".join("  " + r for r in rows))

for th in (0.04, 0.08, 0.12):
    report(all_trades(d, d["p"].to_numpy(), th, 1), f"滚动重训，θ={100*th:.0f}¢，每个市场只买第一笔")
    report(all_trades(d, d["p"].to_numpy(), th, 3), f"滚动重训，θ={100*th:.0f}¢，每个市场最多 3 笔（加仓）")
    report(all_trades(d, d["p"].to_numpy(), th, None), f"滚动重训，θ={100*th:.0f}¢，不限笔数（加仓）")
# same-direction scale-in only: later entries must be on the side already held
t = all_trades(d, d["p"].to_numpy(), 0.08, None)
first_side = t.groupby("market_id")["up"].transform("first")
report(t[t["up"] == first_side], "滚动重训，θ=8¢，只在已持有的方向上加仓")
