"""Scale-in rules for G and H, chosen on A and checked on B and C (local, real/cross-jitter.csv.gz).

Trades: every qualifying Binance-jump episode in a BTC 5m market (episodes at least 2 s apart,
theta 12c, healthy books, the ask 300 ms after the print, fee included, held to settlement), as in
equity_compound.py. A rule decides which of a market's later qualifying episodes are also bought
("adds") and at what size; the first one is always bought at full size.

Each trade stakes a fixed $3 (1% of $300) at the ask, capped by the shares shown, 78% filled, so
the rules compare without compounding. Per period: trades, cents per share, dollars per day,
daily Sharpe (mean / standard deviation of the daily dollars over the days with trades), worst
day and largest drawdown of the cumulative dollars. The rule with the highest daily Sharpe on A
(May 25 - Jul 15) is chosen; B (Jul 16 - Aug 16) and C (Aug 17 - 29) are read for it once.

With --extra FILE (the .csv.gz of `run_local.py ... --scale-in`, e.g. September) the same rules are
also run on its "G 加仓" / "H 加仓" trades (fills at least 2 s apart), as period X.

    python scalein.py > real/scalein.md
    python scalein.py --extra sept.csv.gz
"""
import argparse

import numpy as np
import pandas as pd

STAKE, FILL = 3.0, 0.78


def episodes(path, rule, theta=0.12):
    d = pd.read_csv(path)
    d = d[d["ok"]].dropna(subset=["p0", "ua_l", "da_l", "up_won"]).copy()
    up = d["up"].to_numpy() == 1
    fair_up = d["fair_up"] if rule == "G" else d["fair_a2_up"]
    fair = np.where(up, fair_up, 1 - fair_up)
    px = np.where(up, d["ua_l"], d["da_l"])
    fee = 0.07 * px * (1 - px)
    edge = fair - px - fee
    keep = np.isfinite(fair) & (px >= 0.02) & (px <= 0.98) & (edge >= theta)
    if rule == "G":
        pre = np.where(up, 1, -1) * (d["m_0"] - d["m_m2"])
        keep &= np.isfinite(pre) & (pre < 0.03)
    t = pd.DataFrame({"t": d["t0"] / 1000, "market_id": d["market_id"], "up": up, "price": px, "fee": fee,
                      "edge": edge, "size": np.where(up, d["uas_l"], d["das_l"]), "day": d["day"],
                      "won": np.where(up, d["up_won"], 1 - d["up_won"])})[keep].sort_values("t").reset_index(drop=True)
    t["pnl"] = t["won"] - t["price"] - t["fee"]
    t["period"] = np.select([t["day"] <= "2026-07-15", t["day"] <= "2026-08-16"], ["A", "B"], "C")
    return t


def extra_episodes(path, rule):
    e = pd.read_parquet(path) if str(path).endswith(".parquet") else pd.read_csv(path)
    e = e[e["strategy"].astype(str).str.strip() == f"{rule} 加仓"]
    if e.empty:
        return None
    t = e["t"].astype(float)
    t = pd.DataFrame({"t": np.where(t > 1e11, t / 1000, t), "market_id": e["market_id"], "up": e["side"] == "Up",
                      "price": e["price"], "fee": e["fee"], "edge": e["fair"] - e["price"] - e["fee"], "size": e["size"],
                      "won": e["won"]}).sort_values("t").reset_index(drop=True)
    t["day"] = pd.to_datetime(t["t"], unit="s", utc=True).dt.strftime("%Y-%m-%d")
    t["pnl"] = t["won"] - t["price"] - t["fee"]
    t["period"] = "X"
    return t


def apply(t, rule):
    """Weight per qualifying episode (0: skipped) under a scale-in rule, market by market in time order."""
    w = np.zeros(len(t))
    for _, idx in t.groupby("market_id").indices.items():
        n, last, first_up = 0, -np.inf, None
        for i in idx:
            r = t.iloc[i]
            if n == 0:
                w[i], n, last, first_up = 1.0, 1, r.t, r.up
                continue
            x = rule(n=n, dt=r.t - last, same=r.up == first_up, edge=r.edge)
            if x > 0:
                w[i], n, last = x, n + 1, r.t
    return w


RULES = {
    "只买第一笔": lambda **k: 0.0,
    "每次都加（现在的）": lambda **k: 1.0,
    "最多 2 笔": lambda n, **k: 1.0 if n < 2 else 0.0,
    "最多 3 笔": lambda n, **k: 1.0 if n < 3 else 0.0,
    "只加同向": lambda same, **k: 1.0 if same else 0.0,
    "只加反向": lambda same, **k: 0.0 if same else 1.0,
    "加仓要边际 ≥ 20¢": lambda edge, **k: 1.0 if edge >= 0.20 else 0.0,
    "加仓距上一笔 ≥ 10 秒": lambda dt, **k: 1.0 if dt >= 10 else 0.0,
    "加仓半仓": lambda **k: 0.5,
}


def stats(t, w):
    m = w > 0
    x = t[m]
    if x.empty:
        return dict(n=0)
    cost = x["price"] + x["fee"]
    sh = np.minimum(STAKE / cost, x["size"].fillna(0)) * FILL * w[m]
    usd = sh * x["pnl"]
    daily = usd.groupby(x["day"]).sum()
    cum = daily.cumsum()
    return dict(n=len(x), adds=int((w[m] > 0).sum() - x["market_id"].nunique()), c=100 * (usd.sum() / sh.sum()),
                day=daily.mean(), sharpe=daily.mean() / daily.std(), worst=daily.min(), dd=(cum.cummax().clip(lower=0) - cum).max())


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jitter", default="real/cross-jitter.csv.gz")
    ap.add_argument("--extra", help="run_local.py --scale-in trades (period X)")
    a = ap.parse_args(argv)
    print(__doc__.split("\n\n")[0], "\n")
    for rule in ("G", "H"):
        t = episodes(a.jitter, rule)
        x = extra_episodes(a.extra, rule) if a.extra else None
        periods = "ABC" if x is None else "ABCX"
        if x is not None:
            t = pd.concat([t, x], ignore_index=True)
        res = {}
        for name, r in RULES.items():
            w = apply(t, r)
            res[name] = {p: stats(t[t["period"] == p].reset_index(drop=True), w[(t["period"] == p).to_numpy()])
                         for p in periods}
        best = max(res, key=lambda k: res[k]["A"]["sharpe"])
        print(f"## {rule}\n")
        print("| 加仓规则 | 时段 | 笔数 | 其中加仓 | 每份 | 每天 | 日夏普 | 最差一天 | 最大回撤 |")
        print("|---|---|---:|---:|---:|---:|---:|---:|---:|")
        for name in RULES:
            for p in periods:
                s = res[name][p]
                print(f"| {name}{'（A 段选中）' if name == best else ''} | {p} | {s['n']:,} | {s['adds']:,} | {s['c']:+.2f}¢ | "
                      f"${s['day']:.2f} | {s['sharpe']:.2f} | ${s['worst']:.2f} | ${s['dd']:.2f} |")
        print(f"\nA 段日夏普最高：**{best}**。\n")


if __name__ == "__main__":
    main()
