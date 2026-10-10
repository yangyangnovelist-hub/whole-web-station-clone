"""Per-day amounts with units for real/resolved.md: how many shares a day traded on the winning side
after the outcome was decided, what they cost, what they paid, the price, the profit per share, the
holding time and the capital tied up. From real/resolved-trades.csv.gz (resolved.py's rows), with the
report's verdict scope: Binance-settled kinds (no 4 h), T* + 5 s or later, ET days 03-14..09-30.

These are market totals: every trade by anyone. They are the ceiling of what one trader could have
taken, not an estimate of it.

    python resolved_units.py [--csv real/resolved-trades.csv.gz] [--out real/resolved-units.md]
"""
import argparse
from pathlib import Path

import numpy as np
import pandas as pd

FIRST, LAST = "2026-03-14", "2026-09-30"


def load(path):
    t = pd.read_csv(path)
    t = t[(t["dt"] >= 5) & (t["kind"] != "updown_4h") & (t["day"] >= FIRST) & (t["day"] <= LAST) & (t["price"] < 1)].copy()
    t["cost"] = t["price"] * t["size"]
    t["profit"] = (1 - t["price"]) * t["size"]
    t["cap_day"] = t["cost"] * t["hours"] / 24  # dollar-days of capital, spread over the day
    return t


def summary(x, ndays):
    sh, c, p = x["size"].sum(), x["cost"].sum(), x["profit"].sum()
    return {"trades": len(x) / ndays, "shares": sh / ndays, "cost": c / ndays, "profit": p / ndays,
            "price": c / sh if sh else np.nan, "cents": 100 * p / sh if sh else np.nan,
            "ret": p / c if c else np.nan, "hours": x["hours"].median() if len(x) else np.nan,
            "capital": x["cap_day"].sum() / ndays}


def row(label, s):
    return (f"| {label} | {s['trades']:,.1f} | {s['shares']:,.0f} | ${s['cost']:,.0f} | ${s['profit']:,.1f} | "
            f"{s['price']:.3f} | {s['cents']:.2f}¢ | {100 * s['ret']:.2f}% | {s['hours']:.1f} | ${s['capital']:,.0f} |")


def report(t):
    days = pd.date_range(FIRST, LAST).strftime("%Y-%m-%d")
    nd = len(days)
    head = ["| 口径 | 笔数/天 | 份数/天 | 买入花费/天 | 利润/天 | 均价 | 每份利润 | 收益率 | 持有小时（中位） | 平均占用资金 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    L = ["# 结果已定、未结算：每天多少份、花多少钱、赚多少（带单位）", "",
         f"来自 `real/resolved-trades.csv.gz`（resolved.py）。口径同 resolved.md 的判定：币安结算的六类（不含 4 小时）、"
         f"结果确定后 5 秒起、官方结算前、{FIRST}–{LAST} 的 {nd} 个 ET 日（没有成交的天算 0）。",
         "", "**这些是全市场的合计（所有人的成交），是一个人最多能拿到的上限，不是我们能拿到的量。** "
         "1 份 = 结算时赢了付 $1 的代币；买入花费 = 价格 × 份数；利润 = (1 − 价格) × 份数（挂单不付手续费）；"
         "平均占用资金 = Σ(花费 × 持有小时 / 24) / 天数，即为了接下这些成交平均要压在场上的钱。"
         "“别人主动卖出”= taker 卖出赢方代币，接到它的是已经挂着的买单，我们挂单最多能接到这些。", ""]
    for band, cond in (("价格 ≤ 0.995", t["price"] <= 0.995), ("价格 < 1（含 0.995–0.999）", t["price"] < 1)):
        L += [f"## {band}，{nd} 天平均", ""] + head
        L.append(row("所有成交", summary(t[cond], nd)))
        L.append(row("别人主动卖出（挂单能接的上限）", summary(t[cond & (t["taker_buy"] == 0)], nd)))
        L.append("")
    L += ["## 价格 ≤ 0.995，逐月（每天平均）", "",
          "| 月 | 天数 | 所有成交 份数/天 | 花费/天 | 利润/天 | 每份 | 挂单能接 份数/天 | 花费/天 | 利润/天 |",
          "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    x = t[t["price"] <= 0.995]
    for mo in sorted({d[:7] for d in days}):
        n = sum(d[:7] == mo for d in days)
        g = x[x["day"].str[:7] == mo]
        a, s = summary(g, n), summary(g[g["taker_buy"] == 0], n)
        L.append(f"| {mo} | {n} | {a['shares']:,.0f} | ${a['cost']:,.0f} | ${a['profit']:,.1f} | {a['cents']:.2f}¢ | "
                 f"{s['shares']:,.0f} | ${s['cost']:,.0f} | ${s['profit']:,.1f} |")
    return "\n".join(L) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", default="real/resolved-trades.csv.gz")
    ap.add_argument("--out", default="real/resolved-units.md")
    a = ap.parse_args(argv)
    text = report(load(a.csv))
    Path(a.out).write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
