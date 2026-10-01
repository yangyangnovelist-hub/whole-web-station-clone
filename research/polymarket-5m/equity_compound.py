"""Compounding equity curves for G and H, with and without scaling in (local).

Trades come from scalein.py's engine on candidate rows (cross.py jitter's columns): the jump
episodes of real/cross-jitter.csv.gz (May 25 - Aug 29), or every candidate print with
`cross.py jitter --gap-ms 0` / `run_local.py --prints` (what a live bot sees; 2 s after a fill
nothing more is bought). The left panel runs rule "只买第一笔" (G and H as tested), the right one
--scale-rule (default "每次都加（现在的）"); its add weights scale the stake.

Sizing compounds: each trade stakes FRACTION of the current account (1%; 2.5% is about 20 shares
at $300) times the rule's weight, at least MIN_SHARES (Polymarket's minimum order) and at most CAP
shares (what the book plausibly gives a sniper: the September best ask had a median of about 33
shares, and walls of thousands are where other takers compete hardest), capped by the shares
shown at the ask, times FILL (0.78, the share other takers left us in September). The calendar
runs day by day; days without data are flat (no trades).

September (or any other) candidates are appended with --extra FILE (run_local.py --prints or
--episodes); use the same kind of rows as --jitter, or the two parts are not comparable.

    python equity_compound.py [--jitter prints.csv.gz] [--extra sept-prints.csv.gz] [--cap 200]
"""
import argparse

import matplotlib
matplotlib.use("Agg")
import matplotlib.patches
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import scalein as si

plt.rcParams["font.family"] = ["WenQuanYi Zen Hei", "Noto Sans CJK SC", "DejaVu Sans"]
SURF, INK, INK2, GRID, GAP = "#fcfcfb", "#0b0b0b", "#52514e", "#e6e5e0", "#efeee9"
COLOR = {"G": "#2a78d6", "H": "#eb6834"}


def rule_trades(paths, rule, name):
    """The trades rule `name` of scalein.py takes on the candidate rows of `paths`, with weight w."""
    r = {x.name: x for x in si.RULES}[name]
    t = pd.concat([si.episodes(p, rule, period="-") for p in paths], ignore_index=True)
    t = t.sort_values("t", kind="stable").reset_index(drop=True)
    w = si.weights(t, r)
    return t[w > 0].assign(w=w[w > 0])[["t", "market_id", "price", "size", "won", "w"]]


def simulate(t, start, fraction, fill, cap=np.inf, min_shares=5.0):
    t = t.sort_values("t")
    if "w" not in t:
        t = t.assign(w=1.0)
    eq, path = start, []
    for r in t.itertuples():
        fee = 0.07 * r.price * (1 - r.price)
        cost = r.price + fee
        want = min(max(fraction * r.w * eq / cost, min_shares), cap, eq / cost)
        sh = min(want, r.size if np.isfinite(r.size) else 0.0) * fill
        eq += sh * (r.won - cost)
        path.append((r.t, eq))
    return pd.DataFrame(path, columns=["t", "eq"])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jitter", default="real/cross-jitter.csv.gz", help="candidate rows (cross.py jitter columns)")
    ap.add_argument("--extra", help="more candidate rows (e.g. September: run_local.py --prints / --episodes)")
    ap.add_argument("--scale-rule", default=si.BASE, help="scalein.py rule of the right panel")
    ap.add_argument("--start", type=float, default=300.0)
    ap.add_argument("--fraction", type=float, default=0.01)
    ap.add_argument("--cap", type=float, default=200.0, help="most shares per trade")
    ap.add_argument("--min-shares", type=float, default=5.0)
    ap.add_argument("--fill", type=float, default=0.78)
    ap.add_argument("--out", default="real/equity_compound.png")
    a = ap.parse_args(argv)
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.8), sharey=True, facecolor=SURF)
    for ax, scale_in in zip(axes, (False, True)):
        ax.set_facecolor(SURF)
        traded, cal, ends = set(), None, {}
        for rule in ("G", "H"):
            t = rule_trades([a.jitter] + ([a.extra] if a.extra else []), rule, a.scale_rule if scale_in else "只买第一笔")
            p = simulate(t, a.start, a.fraction, a.fill, a.cap, a.min_shares)
            p["day"] = pd.to_datetime(p["t"], unit="s", utc=True).dt.normalize()
            daily = p.groupby("day")["eq"].last()
            cal = pd.date_range(daily.index.min() - pd.Timedelta(days=1), daily.index.max(), freq="D", tz="UTC")
            daily = daily.reindex(cal)
            daily.iloc[0] = a.start
            daily = daily.ffill()  # days without data: flat
            traded |= set(p["day"])
            ax.plot(daily.index, daily.to_numpy(), color=COLOR[rule], lw=2)
            ends[rule] = (daily.index[-1], daily.iloc[-1])
            dd = (daily.cummax() - daily)
            hi = p["eq"].cummax()
            last20 = (daily.iloc[-1] - daily.iloc[-21]) / 20 if len(daily) > 21 else np.nan
            print(f"{'加仓' if scale_in else '首笔'} {rule}: {len(t):,} 笔，期末 ${daily.iloc[-1]:,.0f}，"
                  f"按天最大回撤 ${dd.max():,.0f}（{100 * (dd / daily.cummax()).max():.0f}%），"
                  f"逐笔最大回撤 {100 * ((hi - p['eq']) / hi).max():.0f}%，账户最低 ${p['eq'].min():,.0f}，"
                  f"最后 20 天 ${last20:,.0f}/天")
        lo, hi = sorted(ends, key=lambda k: ends[k][1])
        close = ends[hi][1] / ends[lo][1] < 1.3  # labels would overlap: push them apart
        for k in ends:
            ax.annotate(f"{k}  ${ends[k][1]:,.0f}", ends[k], xytext=(6, (7 if k == hi else -7) if close else 0),
                        textcoords="offset points", va="center", fontsize=10, color=INK)
        # shade the days on which neither rule traded (no data, or no healthy book): flat
        gap = pd.Series([d not in traded for d in cal[1:]], index=cal[1:])
        run = (gap != gap.shift()).cumsum()
        for _, g in gap[gap].groupby(run[gap]):
            ax.axvspan(g.index[0] - pd.Timedelta(hours=12), g.index[-1] + pd.Timedelta(hours=12), color=GAP, lw=0, zorder=0)
        ax.axhline(a.start, color=INK2, lw=1)
        ax.set_yscale("log")
        ax.set_title("每个市场只买第一笔" if not scale_in else f"加仓：{a.scale_rule}", fontsize=11, color=INK, loc="left")
        ax.grid(axis="y", color=GRID, lw=1, which="both")
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
        for sp in ("left", "bottom"):
            ax.spines[sp].set_color(GRID)
        ax.tick_params(colors=INK2, labelsize=9)
        ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%m/%d"))
    axes[0].set_ylabel("账户金额（美元，对数刻度）", color=INK2, fontsize=9)
    axes[0].yaxis.set_major_formatter(matplotlib.ticker.StrMethodFormatter("${x:,.0f}"))
    handles = [plt.Line2D([], [], color=COLOR[k], lw=2) for k in ("G", "H")]
    handles.append(matplotlib.patches.Patch(color=GAP))
    fig.legend(handles, ["G（跳过已被抢先的）", "H（2 秒前起算）", "没数据的日子（空仓）"], loc="upper left",
               bbox_to_anchor=(0.06, 0.97), ncol=3, frameon=False, fontsize=10, labelcolor=INK)
    src = "5/25–8/29" + ("＋补充数据" if a.extra else "")
    cap = "" if not np.isfinite(a.cap) else f"、最多 {a.cap:g} 份"
    fig.suptitle(f"${a.start:,.0f} 起步、复利的回测资金曲线（{src}；每笔投账户的 {100 * a.fraction:g}%，最少 {a.min_shares:g} 份{cap}，"
                 f"不超过卖一挂单量，按 {100 * a.fill:.0f}% 成交；0.3 秒，扣手续费；没数据的日子空仓）",
                 x=0.06, y=1.04, ha="left", fontsize=11, color=INK)
    fig.tight_layout()
    fig.savefig(a.out, dpi=160, bbox_inches="tight", facecolor=SURF)


if __name__ == "__main__":
    main()
