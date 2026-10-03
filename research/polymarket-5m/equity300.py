"""$300 account equity curves for G and H (local, from the depth backtests' trade lists).

Each trade buys min(ask size, CAP) shares x 0.78 (the share other takers leave us, measured on
the September prints) at the ask 300 or 500 ms after the Binance print, pays the taker fee and
is held to settlement. G = gated rule with the pre-move skip (real/cross-gated-depth.csv.gz), H =
anchored prior (real/cross-gated-depth-anchor.csv.gz); theta 12c, healthy books, May 25 - Aug 29
(66 days with data).

    python equity300.py   # writes real/equity300.png and prints the table
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

CAP, FILL, START = 20, 0.78, 300.0
plt.rcParams["font.family"] = ["WenQuanYi Zen Hei", "DejaVu Sans"]
SURF, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e6e5e0"
COLOR = {"G": "#2a78d6", "H": "#eb6834"}


def curve(path, lag, skip_moved):
    d = pd.read_csv(path)
    t = d[(d["lag"] == lag) & (d["theta"] == 0.12)].copy()
    if skip_moved:
        t = t[t["pre_move"] < 0.03]
    t = t.sort_values("t0")
    usd = t["pnl"] * np.minimum(t["size"].fillna(0), CAP) * FILL
    daily = usd.groupby(t["day"]).sum()
    eq = START + usd.cumsum().to_numpy()
    peak = np.maximum.accumulate(np.r_[START, eq])[1:]
    return daily, dict(per_day=daily.mean(), worst_day=daily.min(), pos=(daily > 0).mean(),
                       mdd=(peak - eq).max(), low=min(START, eq.min()), end=eq[-1])


fig, axes = plt.subplots(1, 2, figsize=(11, 4.6), sharey=True, facecolor=SURF)
rows = []
for ax, lag in zip(axes, (300, 500)):
    ax.set_facecolor(SURF)
    for name, path, skip in (("G", "real/cross-gated-depth.csv.gz", True), ("H", "real/cross-gated-depth-anchor.csv.gz", False)):
        daily, s = curve(path, lag, skip)
        y = START + daily.cumsum().to_numpy()
        x = np.arange(1, len(y) + 1)
        ax.plot(np.r_[0, x], np.r_[START, y], color=COLOR[name], lw=2, solid_capstyle="round", solid_joinstyle="round")
        ax.annotate(f"{name}  ${y[-1]:,.0f}", (x[-1], y[-1]), xytext=(6, 0), textcoords="offset points",
                    va="center", fontsize=10, color=INK)
        rows.append((lag, name, s))
    ax.axhline(START, color=INK2, lw=1)
    ax.set_title(f"{lag / 1000:g} 秒成交", fontsize=11, color=INK, loc="left")
    ax.grid(axis="y", color=GRID, lw=1)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    for sp in ("left", "bottom"):
        ax.spines[sp].set_color(GRID)
    ax.tick_params(colors=INK2, labelsize=9)
    ax.set_xlabel("有数据的交易日（5/25–8/29 共 66 天）", color=INK2, fontsize=9)
    ax.set_xlim(0, 75)
axes[0].set_ylabel("账户金额（美元）", color=INK2, fontsize=9)
axes[0].yaxis.set_major_formatter(matplotlib.ticker.StrMethodFormatter("${x:,.0f}"))
handles = [plt.Line2D([], [], color=COLOR[k], lw=2) for k in ("G", "H")]
fig.legend(handles, ["G（跳过已被抢先的）", "H（2 秒前起算）"], loc="upper left", bbox_to_anchor=(0.06, 0.97),
           ncol=2, frameon=False, fontsize=10, labelcolor=INK)
fig.suptitle("300 美元起步的回测资金曲线（每笔最多 20 份，按 78% 成交，扣手续费，每天收盘记一点）",
             x=0.06, y=1.04, ha="left", fontsize=12, color=INK)
fig.tight_layout()
fig.savefig("real/equity300.png", dpi=160, bbox_inches="tight", facecolor=SURF)
for lag, name, s in rows:
    print(f"{lag} ms {name}: 每天 ${s['per_day']:.0f}，最差一天 ${s['worst_day']:.0f}，赚钱的天 {100 * s['pos']:.0f}%，"
          f"最大回撤 ${s['mdd']:.0f}，账户最低 ${s['low']:.0f}，期末 ${s['end']:,.0f}")
