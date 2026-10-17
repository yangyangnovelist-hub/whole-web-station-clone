"""Multi-factor model of the Binance-jump trade (local, on real/cross-jitter.csv.gz).

Each healthy jump episode of `cross.py jitter` (BTC 5m, May 25 - Aug 29) is described only by
what was known when the Binance print arrived: the market's own price, the two fair values of the
gated rule (from the mid at the print, and from the mid 2 s before plus the Binance move since),
the move's size, the Up mid's moves over the 2/10/32 s before, how flat the book and Binance were
in the 30 s before, the spread, the top ask sizes, the Polymarket taker flow toward and against
the move in the 2 s before, the time left and the hour. Nothing after the print is used (not the
mid 0.3/2/10 s later, not Binance after it).

A model of "the side of the move wins" is fit on A (May 25 - Jul 15) only: a logistic regression
and gradient-boosted trees. A trade buys the move's side when p - ask - fee >= theta, or the other
side when (1 - p) - its ask - fee >= theta (the asks 300 ms after the print, as an immediate-or-
cancel limit order sent at the print would meet them), first trade per market. The model and theta
are chosen on B (Jul 16 - Aug 16); C (Aug 17 - 29) is read once for the chosen one. Baselines: the
gated rule's own fair values (tests F and H) and test G's skip."""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

import binary as bo

THETAS = (0.04, 0.08, 0.12)
MIN_B = 150


def features(d):
    """Model inputs oriented to the side of the move (all known at the print)."""
    s = np.where(d["up"] == 1, 1.0, -1.0)

    def logit(p):
        p = np.clip(p, 0.01, 0.99)
        return np.log(p / (1 - p))

    q0 = np.where(d["up"] == 1, d["p0"], 1 - d["p0"])
    fair = np.where(d["up"] == 1, d["fair_up"], 1 - d["fair_up"])
    fa2 = np.where(d["up"] == 1, d["fair_a2_up"], 1 - d["fair_a2_up"])
    side_ask0 = np.where(d["up"] == 1, d["uas_0"], d["das_0"])
    other_ask0 = np.where(d["up"] == 1, d["das_0"], d["uas_0"])
    side_ask_m2 = np.where(d["up"] == 1, d["uas_m2"], d["das_m2"])
    hour = pd.to_datetime(d["t0"], unit="ms", utc=True).dt.hour.to_numpy()
    X = pd.DataFrame({
        "lq0": logit(q0), "lfair": logit(fair), "lfa2": logit(fa2), "z": d["z"],
        "bn_move2": s * d["bn_move2"], "pre2": s * (d["m_0"] - d["m_m2"]), "pre10": s * (d["m_0"] - d["m_m10"]),
        "pre32": s * (d["m_0"] - d["m_m32"]), "tau": d["tau"], "ltau": np.log(d["tau"].clip(lower=1)),
        "spread0": d["spread_0"], "spread_m2": d["spread_m2"], "range30": d["pm_range30"],
        "chg30": d["pm_changes30"], "rv30": d["bn_rv30"], "l_side_ask0": np.log1p(side_ask0),
        "l_other_ask0": np.log1p(other_ask0), "side_ask_drop": np.log1p(side_ask_m2) - np.log1p(side_ask0),
        "l_sweep": np.log1p(d["sweep2"]), "l_against": np.log1p(d["against2"]), "n_trades2": d["n_trades2"],
        "hsin": np.sin(2 * np.pi * hour / 24), "hcos": np.cos(2 * np.pi * hour / 24)})
    return X.replace([np.inf, -np.inf], np.nan)


def trades(d, p_side, theta, fade=True):
    """First trade per market: the move's side if p - ask - fee >= theta, else (with `fade`) the
    other side if (1 - p) - its ask - fee >= theta (the larger edge when both). NaN p: no trade."""
    up = d["up"].to_numpy() == 1
    a_side = np.where(up, d["ua_l"], d["da_l"])
    a_oth = np.where(up, d["da_l"], d["ua_l"])
    e_side = p_side - a_side - 0.07 * a_side * (1 - a_side)
    e_oth = (1 - p_side) - a_oth - 0.07 * a_oth * (1 - a_oth)
    ok_s = np.isfinite(a_side) & (a_side >= 0.02) & (a_side <= 0.98) & (e_side >= theta)
    ok_o = np.isfinite(a_oth) & (a_oth >= 0.02) & (a_oth <= 0.98) & (e_oth >= theta) & fade
    take_side = ok_s & (~ok_o | (e_side >= e_oth))
    take = ok_s | ok_o
    side_won = np.where(up, d["up_won"], 1 - d["up_won"])
    px = np.where(take_side, a_side, a_oth)
    won = np.where(take_side, side_won, 1 - side_won)
    size = np.where(take_side == up, d["uas_l"], d["das_l"])
    t = pd.DataFrame({"market_id": d["market_id"], "t0": d["t0"], "period": d["period"], "day": d["day"],
                      "fade": ~take_side, "price": px, "won": won, "size": size})[take]
    t = t.sort_values("t0").drop_duplicates("market_id")
    t["fee"] = 0.07 * t["price"] * (1 - t["price"])
    t["pnl"] = t["won"] - t["price"] - t["fee"]
    return t


def stats(t, reps):
    if t.empty:
        return dict(n=0, ev=np.nan, p=1.0, roi=np.nan, fade=np.nan, price=np.nan, per_day=np.nan)
    p = bo.fair_price_pvalue(t["pnl"].to_numpy(), (t["price"] + t["fee"]).to_numpy(), sims=reps) \
        if len(t) >= 10 and t["pnl"].mean() > 0 else 1.0
    return dict(n=len(t), ev=100 * t["pnl"].mean(), p=p, roi=100 * t["pnl"].sum() / (t["price"] + t["fee"]).sum(),
                fade=t["fade"].mean(), price=t["price"].mean(), per_day=len(t) / t["day"].nunique())


def main(argv=None):
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="real/cross-jitter.csv.gz")
    ap.add_argument("--out", default="real/multifactor.md")
    ap.add_argument("--reps", type=int, default=5000)
    a = ap.parse_args(argv)
    d = pd.read_csv(a.data)
    d = d[d["ok"]].copy()
    d["period"] = np.select([d["day"] <= "2026-07-15", d["day"] <= "2026-08-16"], ["A", "B"], "C")
    d = d.dropna(subset=["p0", "fair_up", "ua_l", "da_l", "up_won"]).reset_index(drop=True)
    X = features(d)
    y = np.where(d["up"] == 1, d["up_won"], 1 - d["up_won"])
    A = (d["period"] == "A").to_numpy()
    models = {
        "逻辑回归": make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), LogisticRegression(C=0.5, max_iter=2000)),
        "梯度提升树": HistGradientBoostingClassifier(max_iter=300, learning_rate=0.04, max_leaf_nodes=15,
                                                    min_samples_leaf=200, l2_regularization=1.0, random_state=0),
    }
    probs = {}
    for name, m in models.items():
        m.fit(X[A], y[A])
        probs[name] = m.predict_proba(X)[:, 1]
    fair = np.where(d["up"] == 1, d["fair_up"], 1 - d["fair_up"])
    fa2 = np.where(d["up"] == 1, d["fair_a2_up"], 1 - d["fair_a2_up"])
    pre2 = np.where(d["up"] == 1, 1, -1) * (d["m_0"] - d["m_m2"])
    base = {"F：触发时中间价起算的公平价": fair, "H：2 秒前起算的公平价": fa2}
    # test G's skip: the gated fair value, but only where the mid had not already moved 3c our way
    base["G：F 加“还没动”"] = np.where(pre2 < 0.03, fair, np.nan)

    # calibration on B and C: mean predicted vs realized for the move side, by decile of prediction
    rows, cal = [], []
    runs = [(n, p, False) for n, p in base.items()]  # the gated rules only ever buy the move's side
    runs += [(n + "（只顺势）", p, False) for n, p in probs.items()] + [(n + "（可反向）", p, True) for n, p in probs.items()]
    for name, p, fd in runs:
        for th in THETAS:
            t = trades(d, p, th, fade=fd)
            for per in ("A", "B", "C"):
                r = stats(t[t["period"] == per], a.reps if per != "A" else 500)
                rows.append({"模型": name, "θ": th, "时段": per, **r})
    for name, p in probs.items():
        for per in ("B", "C"):
            m = (d["period"] == per).to_numpy()
            q = pd.qcut(p[m], 5, labels=False, duplicates="drop")
            g2 = pd.DataFrame({"q": q, "pred": p[m], "real": y[m]}).groupby("q").mean()
            cal.append((name, per, g2))
    R = pd.DataFrame(rows)
    # choose on B: highest B EV among cells with at least MIN_B trades in B (models only, not baselines)
    cand = R[(R["时段"] == "B") & R["模型"].str.contains("逻辑回归|梯度提升树") & (R["n"] >= MIN_B)]
    best = cand.sort_values("ev", ascending=False).iloc[0] if len(cand) else None
    L = ["# 多因子：把急动单的十几个特征合在一起（本地，cross-jitter 的 12.8 万次急动）", "", __doc__.split("\n\n", 1)[1].strip(), "",
         "| 模型 | θ | 时段 | 笔数 | 每天 | 均价 | 每份 | p | 每投 1 美元 | 反向单占比 |", "|---|---:|---|---:|---:|---:|---:|---:|---:|---:|"]
    for r in R.itertuples():
        L.append(f"| {r.模型} | {100 * r.θ:.0f}¢ | {r.时段} | {r.n:,} | {r.per_day:.0f} | {r.price:.3f} | {r.ev:+.2f}¢ | "
                 f"{r.p:.4f} | {r.roi:+.1f}¢ | {100 * r.fade:.0f}% |")
    if best is not None:
        c = R[(R["模型"] == best["模型"]) & (R["θ"] == best["θ"]) & (R["时段"] == "C")].iloc[0]
        L += ["", f"**在 B 段选出的**（B 段至少 {MIN_B} 笔里每份最高）：{best['模型']}，θ = {100 * best['θ']:.0f}¢；"
                  f"B 段 {best['n']:,} 笔 {best['ev']:+.2f}¢；C 段（只看这一次）{c['n']:,} 笔 {c['ev']:+.2f}¢，p = {c['p']:.4f}，"
                  f"每投 1 美元 {c['roi']:+.1f} 美分。"]
    L += ["", "## 校准（预测的“急动方向赢”的概率 vs 实际，按预测分五档）", ""]
    for name, per, g2 in cal:
        L.append(f"- {name} {per}：" + "；".join(f"{r.pred:.2f}→{r.real:.2f}" for r in g2.itertuples()))
    if "梯度提升树" in models:
        from sklearn.inspection import permutation_importance
        m = models["梯度提升树"]
        B = (d["period"] == "B").to_numpy()
        imp = permutation_importance(m, X[B], y[B], n_repeats=3, random_state=0, scoring="neg_log_loss")
        order = np.argsort(-imp.importances_mean)
        L += ["", "## 哪些特征有用（梯度提升树，在 B 段打乱一个特征后对数损失变差多少）", ""]
        L += [f"- {X.columns[i]}：{imp.importances_mean[i] * 1e4:+.1f}（×10⁻⁴）" for i in order[:12]]
    Path(a.out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
