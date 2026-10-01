"""Scale-in rules for G and H (local): 63 rules written down and committed before any was run,
chosen on A, checked on B + C, and on September (X) with --extra.

Episodes: every Binance jump episode in a BTC 5m market (cross.py jitter: the first print with
240..15 s left that moved more than 2 sigma from the last trade 1..5 s earlier, then nothing for
2 s) on a healthy book, bought at its side's ask 300 ms later (fee 0.07 p (1 - p)) and held to
settlement. G values it from the Up mid at the print and skips it when that mid had already
moved 3c or more toward the side in the 2 s before; H from the mid 2 s before plus the Binance
move since. A market's first trade is its first episode with fair - ask - fee >= 12c (as the G and
H backtests); a rule decides which later episodes are also bought ("adds") and at what weight.
"Same" and "opposite" are relative to the side of the market's first trade.

Stake: weight x $3 (1% of $300) at the ask, at most the shares shown, 78% filled; no compounding,
so the rules compare on the same footing. Per period: trades, adds, cents per share, dollars per
day, daily Sharpe (mean / standard deviation of the daily dollars over the days with any
episode), worst day; and against "每次都加" (the current rule) the mean daily difference and its t
statistic, paired by day.

Fixed before running: this rule list, and the choice: the rule with the highest daily Sharpe on A
(May 25 - Jul 15), checked on B (Jul 16 - Aug 16) + C (Aug 17 - 29) pooled. The Spearman
correlation between the rules' Sharpe on A and on B + C says whether A's ranking carries over at
all. Caveat: a breakdown of the adds by edge, time left, gap and direction on A, B and C was seen
before this list was written (the first, 9-rule version of this script), so B and C are not fully
clean for those dimensions; September (X, the user's own recordings) is.

Result on A / B + C (real/scalein.md): the rule chosen on A did not beat the current one on B + C
(G: Sharpe 0.83 vs 0.81 but $3.20 a day less; H: 0.75 vs 0.77 and $16 less), and the rank
correlation was +0.27 (G) / +0.31 (H). Written down after seeing A, B and C and before any of
September was run, the two rules to check on X: "同向全仓，反向 2 倍" and "加仓门槛 8¢" make more a day
than "每次都加" with a daily Sharpe no more than 0.01 lower, for both G and H, on A and on B + C.

    python scalein.py > real/scalein.md
    python scalein.py --extra sept-episodes.csv.gz      # from run_local.py ... --episodes
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass

import numpy as np
import pandas as pd

STAKE, FILL, THETA = 3.0, 0.78, 0.12
BASE = "每次都加（现在的）"


@dataclass(frozen=True)
class Rule:
    name: str
    family: str
    adds: bool = True
    direction: str = "any"   # any / same / opp
    max_n: int = 99          # most trades in a market, the first included
    max_same: int = 99       # most same-side adds
    gap: float = 0.0         # seconds since the last fill (episodes are 2 s apart anyway)
    theta: float = THETA     # edge an add needs
    theta_same: float | None = None
    theta_opp: float | None = None
    tau: tuple = (0.0, 999.0)  # seconds left an add needs (lo <= tau < hi)
    price: tuple = (0.0, 1.0)  # ask an add needs
    zmin: float = 0.0        # jump size (sigma) an add needs
    state: str = ""          # winning / losing / cheaper / dearer: an add on a side already held needs its
    #                          mid above / below that side's average cost, or its ask below / above the last
    #                          fill there; lock: an add while the other side is held needs that side's average
    #                          cost + this ask + fee < 1 (both sides held then pay at least $1 per pair)
    pre: bool = True         # G's "already moved" skip applies to adds
    size: str = "1"          # add weight: 1, 0.5, 1.5, 2, 1/k, k, edge, same0.5, opp2
    first: str = "1"         # first trade weight: 1, 0.5, edge
    cap: float = 99.0        # most total weight in a market


R = Rule
RULES = [
    R("只买第一笔", "基准", adds=False),
    R(BASE, "基准"),
    R("最多 2 笔", "笔数", max_n=2),
    R("最多 3 笔", "笔数", max_n=3),
    R("最多 4 笔", "笔数", max_n=4),
    R("最多 5 笔", "笔数", max_n=5),
    R("只加同向", "方向", direction="same"),
    R("只加反向", "方向", direction="opp"),
    R("同向最多加 1 次，反向不限", "方向", max_same=1),
    R("同向最多加 2 次，反向不限", "方向", max_same=2),
    R("距上一笔 ≥ 5 秒", "间隔", gap=5),
    R("距上一笔 ≥ 10 秒", "间隔", gap=10),
    R("距上一笔 ≥ 20 秒", "间隔", gap=20),
    R("距上一笔 ≥ 30 秒", "间隔", gap=30),
    R("距上一笔 ≥ 60 秒", "间隔", gap=60),
    R("加仓门槛 4¢", "门槛", theta=0.04),
    R("加仓门槛 8¢", "门槛", theta=0.08),
    R("加仓门槛 10¢", "门槛", theta=0.10),
    R("加仓门槛 14¢", "门槛", theta=0.14),
    R("加仓门槛 16¢", "门槛", theta=0.16),
    R("加仓门槛 20¢", "门槛", theta=0.20),
    R("加仓门槛 25¢", "门槛", theta=0.25),
    R("同向门槛 16¢，反向 8¢", "门槛", theta_same=0.16, theta_opp=0.08),
    R("同向门槛 20¢，反向 12¢", "门槛", theta_same=0.20, theta_opp=0.12),
    R("加仓只在剩 ≥ 60 秒", "剩余时间", tau=(60, 999)),
    R("加仓只在剩 < 60 秒", "剩余时间", tau=(0, 60)),
    R("加仓只在剩 ≥ 30 秒", "剩余时间", tau=(30, 999)),
    R("加仓只在剩 < 120 秒", "剩余时间", tau=(0, 120)),
    R("加仓只在剩 ≥ 90 秒", "剩余时间", tau=(90, 999)),
    R("加仓价 ≤ 0.3", "价格", price=(0, 0.3)),
    R("加仓价 ≤ 0.5", "价格", price=(0, 0.5)),
    R("加仓价 ≥ 0.3", "价格", price=(0.3, 1)),
    R("加仓价 ≥ 0.5", "价格", price=(0.5, 1)),
    R("加仓价 0.2–0.6", "价格", price=(0.2, 0.6)),
    R("同向只在浮盈时加（顺势加码）", "持仓状态", state="winning"),
    R("同向只在浮亏时加（摊低成本）", "持仓状态", state="losing"),
    R("同向只在比上一笔便宜时加", "持仓状态", state="cheaper"),
    R("同向只在比上一笔贵时加", "持仓状态", state="dearer"),
    R("反向只在能锁利时加，同向照常", "持仓状态", state="lock"),
    R("只加反向且能锁利", "持仓状态", direction="opp", state="lock"),
    R("加仓要急动 ≥ 2.5σ", "急动大小", zmin=2.5),
    R("加仓要急动 ≥ 3σ", "急动大小", zmin=3.0),
    R("加仓要急动 ≥ 4σ", "急动大小", zmin=4.0),
    R("加仓半仓", "仓位", size="0.5"),
    R("加仓 1.5 倍", "仓位", size="1.5"),
    R("加仓 2 倍", "仓位", size="2"),
    R("第 k 笔 1/k 倍", "仓位", size="1/k"),
    R("第 k 笔 k 倍（最多 3 倍）", "仓位", size="k"),
    R("加仓按边际（边际/12¢，最多 3 倍）", "仓位", size="edge"),
    R("每笔都按边际（含第一笔）", "仓位", size="edge", first="edge"),
    R("同向半仓，反向全仓", "仓位", size="same0.5"),
    R("同向全仓，反向 2 倍", "仓位", size="opp2"),
    R("每个市场最多 2 倍仓位", "仓位", cap=2),
    R("每个市场最多 3 倍仓位", "仓位", cap=3),
    R("第一笔半仓，加仓全仓", "仓位", first="0.5"),
    R("最多 3 笔 + 加仓门槛 16¢", "组合", max_n=3, theta=0.16),
    R("只加反向 + 反向门槛 8¢", "组合", direction="opp", theta=0.08),
    R("同向半仓反向全仓 + 最多 4 笔", "组合", size="same0.5", max_n=4),
    R("距上一笔 ≥ 10 秒 + 最多 3 笔", "组合", gap=10, max_n=3),
    R("同向最多 1 次 + 反向锁利", "组合", max_same=1, state="lock"),
    R("剩 ≥ 30 秒 + 加仓门槛 14¢", "组合", tau=(30, 999), theta=0.14),
    R("急动 ≥ 2.5σ + 最多 4 笔", "组合", zmin=2.5, max_n=4),
    R("加仓不用 G 的“已被抢先”过滤", "G 专用", pre=False),
]


def episodes(path, rule, period=None):
    """Healthy episodes oriented to the side of the jump, valued by `rule` (G or H)."""
    d = pd.read_parquet(path) if str(path).endswith(".parquet") else pd.read_csv(path)
    d = d[d["ok"].astype(bool)].dropna(subset=["p0", "ua_l", "da_l", "up_won"])
    up = d["up"].to_numpy() == 1
    fu = (d["fair_up"] if rule == "G" else d["fair_a2_up"]).to_numpy(float)
    px = np.where(up, d["ua_l"], d["da_l"]).astype(float)
    fee = 0.07 * px * (1 - px)
    p0, m2 = d["p0"].to_numpy(float), d["m_m2"].to_numpy(float)
    pre = np.where(up, 1, -1) * (d["m_0"].to_numpy(float) - m2)
    t = pd.DataFrame({"t": d["t0"].to_numpy(float) / 1000, "market_id": d["market_id"].to_numpy(), "up": up,
                      "price": px, "fee": fee, "edge": np.where(up, fu, 1 - fu) - px - fee,
                      "pre_ok": (rule != "G") | (np.isfinite(pre) & (pre < 0.03)),
                      "tau": d["tau"].to_numpy(float), "z": np.abs(d["z"].to_numpy(float)),
                      "mid": np.where(up, p0, 1 - p0), "size": np.where(up, d["uas_l"], d["das_l"]).astype(float),
                      "won": np.where(up, d["up_won"], 1 - d["up_won"]).astype(float),
                      "day": d["day"].astype(str).to_numpy()})
    t = t[np.isfinite(t["edge"]) & (t["price"] >= 0.02) & (t["price"] <= 0.98)]
    t = t.sort_values("t", kind="stable").reset_index(drop=True)
    t["size"] = t["size"].fillna(0.0)
    t["pnl"] = t["won"] - t["price"] - t["fee"]
    t["period"] = period if period else np.select([t["day"] <= "2026-07-15", t["day"] <= "2026-08-16"], ["A", "B"], "C")
    return t


def _w(kind, n, same, edge):
    if kind == "1/k":
        return 1.0 / (n + 1)
    if kind == "k":
        return float(min(n + 1, 3))
    if kind == "edge":
        return float(np.clip(edge / THETA, 0, 3))
    if kind == "same0.5":
        return 0.5 if same else 1.0
    if kind == "opp2":
        return 1.0 if same else 2.0
    return float(kind)


def weights(t, r):
    """Weight of each episode under rule r (0: not bought), market by market in time order."""
    w = np.zeros(len(t))
    ts, up, edge, pre = t["t"].to_numpy(), t["up"].to_numpy(), t["edge"].to_numpy(), t["pre_ok"].to_numpy()
    tau, z, px, fee, mid = (t[c].to_numpy() for c in ("tau", "z", "price", "fee", "mid"))
    for idx in t.groupby("market_id", sort=False).indices.values():
        n = 0
        for i in idx:
            if n == 0:
                if pre[i] and edge[i] >= THETA:
                    x = _w(r.first, 0, True, edge[i])
                    w[i], n, last, first_up, n_same, tot = x, 1, ts[i], up[i], 0, x
                    held = {up[i]: [x, x * (px[i] + fee[i]), px[i]], not up[i]: [0.0, 0.0, np.nan]}
                    if not r.adds:
                        break
                continue
            same = up[i] == first_up
            if (r.direction == "same" and not same) or (r.direction == "opp" and same):
                continue
            if (r.pre and not pre[i]) or n >= r.max_n or (same and n_same >= r.max_same) or ts[i] - last < r.gap:
                continue
            th = r.theta_same if same and r.theta_same is not None else \
                r.theta_opp if not same and r.theta_opp is not None else r.theta
            if edge[i] < th or not (r.tau[0] <= tau[i] < r.tau[1]) or not (r.price[0] <= px[i] <= r.price[1]) \
                    or z[i] < r.zmin:
                continue
            mine, other = held[up[i]], held[not up[i]]
            if mine[0] > 0 and r.state in ("winning", "losing", "cheaper", "dearer"):
                avg = mine[1] / mine[0]
                if (r.state == "winning" and not mid[i] > avg) or (r.state == "losing" and not mid[i] < avg) \
                        or (r.state == "cheaper" and not px[i] < mine[2]) or (r.state == "dearer" and not px[i] > mine[2]):
                    continue
            if r.state == "lock" and other[0] > 0 and other[1] / other[0] + px[i] + fee[i] >= 1:
                continue
            x = min(_w(r.size, n, same, edge[i]), r.cap - tot)
            if x <= 1e-9:
                continue
            w[i], n, last, tot = x, n + 1, ts[i], tot + x
            n_same += int(same)
            mine[0] += x
            mine[1] += x * (px[i] + fee[i])
            mine[2] = px[i]
    return w


def daily(t, w):
    """Dollars per day (every day with an episode, 0 without a trade), trades, adds, cents per share."""
    m = w > 0
    cost = t["price"].to_numpy() + t["fee"].to_numpy()
    sh = np.minimum(STAKE * w / cost, t["size"].to_numpy()) * FILL * m
    usd = pd.Series(sh * t["pnl"].to_numpy()).groupby(t["day"].to_numpy()).sum()
    n_mk = t.loc[m, "market_id"].nunique()
    return usd, int(m.sum()), int(m.sum()) - n_mk, 100 * (sh * t["pnl"].to_numpy()).sum() / max(sh.sum(), 1e-9)


def run(t, rules, periods):
    """Per rule and period: dict of n, adds, c (cents per share), day ($), sharpe, worst, diff and
    t-stat against BASE paired by day."""
    sel = {q: (t["period"] == q).to_numpy() for p in periods for q in p.split("+")}
    out = {}
    for r in rules:
        w = weights(t, r)
        out[r.name] = {}
        for p in periods:
            m = np.zeros(len(t), bool)
            for q in p.split("+"):
                m |= sel[q]
            usd, n, adds, c = daily(t[m].reset_index(drop=True), w[m])
            out[r.name][p] = dict(n=n, adds=adds, c=c, usd=usd, day=usd.mean(),
                                  sharpe=usd.mean() / usd.std() if usd.std() > 0 else np.nan, worst=usd.min())
    for r in rules:
        for p in periods:
            d = out[r.name][p]["usd"] - out[BASE][p]["usd"]
            se = d.std() / np.sqrt(len(d)) if len(d) > 1 else np.nan
            out[r.name][p].update(diff=d.mean(), tstat=d.mean() / se if se and se > 0 else np.nan)
    return out


def main(argv=None):
    from scipy.stats import spearmanr
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jitter", default="real/cross-jitter.csv.gz")
    ap.add_argument("--extra", help="September episodes: run_local.py ... --episodes (period X)")
    a = ap.parse_args(argv)
    print("# 加仓规则：63 条，A 段选、B+C 段核对" + ("、九月（X）检验" if a.extra else ""), "\n")
    print("\n\n".join(__doc__.split("\n\n")[1:-1]), "\n")
    for rule in ("G", "H"):
        t = episodes(a.jitter, rule)
        periods = ["A", "B+C"]
        if a.extra:
            t = pd.concat([t, episodes(a.extra, rule, period="X")], ignore_index=True)
            periods.append("X")
        rules = [r for r in RULES if rule == "G" or r.family != "G 专用"]
        res = run(t, rules, periods)
        names = [r.name for r in rules]
        best = max(names, key=lambda k: res[k]["A"]["sharpe"])
        sa = [res[k]["A"]["sharpe"] for k in names]
        sbc = [res[k]["B+C"]["sharpe"] for k in names]
        rho = spearmanr(sa, sbc).correlation
        top = sorted(names, key=lambda k: -res[k]["A"]["sharpe"])[:10]
        beat = sum(res[k]["B+C"]["sharpe"] > res[BASE]["B+C"]["sharpe"] for k in top if k != BASE)
        b, base = res[best], res[BASE]
        print(f"## {rule}\n")
        print(f"- A 段日夏普最高：**{best}**（A {b['A']['sharpe']:.2f}，现在的 {base['A']['sharpe']:.2f}）。"
              f"B+C 段 {b['B+C']['sharpe']:.2f}（现在的 {base['B+C']['sharpe']:.2f}），"
              f"每天比现在的 {b['B+C']['diff']:+.2f} 美元（t = {b['B+C']['tstat']:.2f}）。")
        if a.extra:
            print(f"- 九月（X）：{best} 日夏普 {b['X']['sharpe']:.2f}（现在的 {base['X']['sharpe']:.2f}），"
                  f"每天比现在的 {b['X']['diff']:+.2f} 美元（t = {b['X']['tstat']:.2f}）。")
        print(f"- {len(rules)} 条规则的 A 段日夏普和 B+C 段日夏普的等级相关 {rho:+.2f}；"
              f"A 段前 10 名里有 {beat} 条在 B+C 段超过现在的规则。\n")
        cols = ["A"] + ["B+C"] + (["X"] if a.extra else [])
        head = "| # | 规则 | 类 | " + " | ".join(f"{p} 笔数 | {p} 每份 | {p} 每天 | {p} 日夏普 | {p} 比现在每天（t）" for p in cols) + " |"
        print(head)
        print("|" + "---|" * (3 + 5 * len(cols)))
        for i, k in enumerate(sorted(names, key=lambda k: -res[k]["A"]["sharpe"]), 1):
            fam = next(r.family for r in rules if r.name == k)
            cells = []
            for p in cols:
                s = res[k][p]
                vs = "–" if k == BASE else f"{s['diff']:+.2f}（{s['tstat']:+.1f}）"
                cells.append(f"{s['n']:,} | {s['c']:+.2f}¢ | ${s['day']:.2f} | {s['sharpe']:.2f} | {vs}")
            mark = "**" if k in (best, BASE) else ""
            print(f"| {i} | {mark}{k}{mark} | {fam} | " + " | ".join(cells) + " |")
        print()


if __name__ == "__main__":
    main()
