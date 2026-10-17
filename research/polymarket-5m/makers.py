"""Who earns in September: resting orders (makers) and aggressors, by price and time left.

    python onchain.py fetch data/onchain            # the same Gamma / Hugging Face / Binance inputs
    python makers.py data/onchain --out real/makers-sept.md

Every OrderFilled event of the 5m up/down markets for 2026-09-01..25 (from
onchain.py) is either the exchange's event for the aggressor's order (`agg`)
or one event per resting order it filled. Each is turned into "long outcome O
at price q": a buy of a token at p is long that token at p, a sell is long
the other token at 1 - p. Its profit per share at settlement is won(O) - q,
less the taker fee 0.07 q (1 - q) for aggressors; resting orders pay none.

The design below was fixed before any September profit and loss was split
this way:
- Cells: role (resting / aggressor) x q in ten 0.1-wide buckets x time left
  at the fill (block time minus 2 s, the median settlement delay) in nine
  buckets: before the window, 300-240, 240-180, 180-120, 120-60, 60-30,
  30-10, 10-0 seconds (fills after the close are dropped).
- One observation per market and cell: the share-weighted price q and the
  outcome, so markets count once however many fills they had.
- Selection on 09-01..09-13: cells with at least 300 markets, EV > 0 and
  exact fair-price p < 0.01; the five with the largest z (EV over its
  standard error) per role, at most ten in all.
- Test on 09-14..09-25: a selected cell passes with EV > 0 and exact
  p < 0.05 / (number selected), and EV > 0 on at least four of five coins.

Resting-order profits are an upper bound for anyone joining the queue:
fills that reach the back of a queue are the larger, better-informed ones,
and there may be maker rebates not counted here.
"""
from __future__ import annotations

import argparse
import math
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

import binary as bo
import onchain as oc

SPLIT = date(2026, 9, 14)
DELAY_S = 2
Q_EDGES = np.linspace(0, 1, 11)
TAU_EDGES = (-np.inf, 0, 10, 30, 60, 120, 180, 240, 300, np.inf)
TAU_NAMES = ("收盘后", "10–0 秒", "30–10 秒", "60–30 秒", "120–60 秒", "180–120 秒", "240–180 秒",
             "300–240 秒", "开盘前")
MIN_MARKETS, SELECT_P, PER_ROLE = 300, 0.01, 5
ROLES = {False: "挂单方", True: "吃单方"}


def positions(fills, markets):
    """One row per fill: market, role, long-outcome price q, time left, shares, whether O won.
    Market attributes go through the token categories, so no per-row strings are built."""
    tok = pd.concat([
        pd.DataFrame({"tok": markets["up_token"].astype(str), "start": markets["start"], "coin": markets["coin"],
                      "won": markets["up_won"].astype(bool)}),
        pd.DataFrame({"tok": markets["down_token"].astype(str), "start": markets["start"], "coin": markets["coin"],
                      "won": ~markets["up_won"].astype(bool)})]).drop_duplicates("tok").set_index("tok")
    tokens = fills["token_asset_id"].astype("category")
    info = tok.reindex(tokens.cat.categories.astype(str))
    codes = tokens.cat.codes.to_numpy()
    known = (codes >= 0) & info["start"].notna().to_numpy()[np.maximum(codes, 0)]
    codes = codes[known]
    f = fills[known]
    start = info["start"].to_numpy(float)[codes].astype("int64")
    coin = pd.Categorical(info["coin"].to_numpy()[codes])
    won = info["won"].to_numpy(bool)[codes]
    agg = f["agg"].to_numpy(bool)
    side = f["maker_direction"].astype("category")  # aggressor rows: the maker field is its own order
    buy = np.asarray(side.cat.categories.astype(str).str.upper() == "BUY")[side.cat.codes.to_numpy()]
    price = f["price"].to_numpy(float)
    q = np.where(buy, price, 1 - price)
    tau = start + bo.WINDOW_S - (f["timestamp"].to_numpy() - DELAY_S)
    out = pd.DataFrame({"coin": coin, "start": start, "agg": agg, "q": q,
                        "won": np.where(buy, won, ~won).astype(float), "tau": tau,
                        "shares": f["token_amount"].to_numpy(float)})
    out["qb"] = np.clip(np.digitize(out["q"], Q_EDGES) - 1, 0, 9).astype("int8")
    out["tb"] = np.digitize(out["tau"], TAU_EDGES[1:-1], right=True).astype("int8")
    return out[(out["tb"] > 0) & (out["q"] > 0) & (out["q"] < 1)]


def per_market(pos):
    """Collapse fills to one observation per (market, role, q bucket, time bucket)."""
    g = pos.assign(qs=pos["q"] * pos["shares"]).groupby(["coin", "start", "agg", "qb", "tb"], observed=True)
    m = g.agg(shares=("shares", "sum"), qs=("qs", "sum"), won=("won", "first")).reset_index()
    m["q"] = m["qs"] / m["shares"]
    m["fee"] = np.where(m["agg"], bo.taker_fee(m["q"]), 0.0)
    m["pnl"] = m["won"] - m["q"] - m["fee"]
    m["day"] = pd.to_datetime(m["start"], unit="s", utc=True).dt.date
    return m.drop(columns="qs")


def cell_stats(m, reps=2000, with_p=True):
    """EV, its standard error and the exact fair-price p per cell."""
    rows = []
    for (agg, qb, tb), g in m.groupby(["agg", "qb", "tb"]):
        n = len(g)
        ev = g["pnl"].mean()
        se = g["pnl"].std(ddof=1) / math.sqrt(n) if n > 1 else np.nan
        p = bo.fair_price_pvalue(g["pnl"].to_numpy(), (g["q"] + g["fee"]).to_numpy(), sims=reps) \
            if with_p and n >= 30 and ev > 0 else 1.0
        rows.append({"agg": agg, "qb": qb, "tb": tb, "n": n, "win": g["won"].mean(), "q": g["q"].mean(),
                     "ev": ev, "z": ev / se if se and se > 0 else np.nan, "p": p})
    return pd.DataFrame(rows)


def select(stats):
    ok = stats[(stats["n"] >= MIN_MARKETS) & (stats["ev"] > 0) & (stats["p"] < SELECT_P)]
    return pd.concat([g.nlargest(PER_ROLE, "z") for _, g in ok.groupby("agg")]) if len(ok) else ok


def label(r):
    lo = Q_EDGES[int(r["qb"])]
    return f"{ROLES[bool(r['agg'])]} q∈[{lo:.1f},{lo + 0.1:.1f}) 剩 {TAU_NAMES[int(r['tb'])]}"


def fmt(n, win, q, ev, p):
    return f"{n:,} | {win:.1%} | {q:.3f} | {100 * ev:+.2f}¢ | {p:.4f}"


def heat(stats, agg, title):
    s = stats[stats["agg"] == agg]
    L = [f"### {title}", "", "| q \\ 剩余时间 | " + " | ".join(TAU_NAMES[1:]) + " |", "|---|" + "---:|" * 8]
    for qb in range(10):
        cells = []
        for tb in range(1, 9):
            r = s[(s["qb"] == qb) & (s["tb"] == tb)]
            cells.append("–" if r.empty else f"{100 * r['ev'].iloc[0]:+.1f}¢ ({int(r['n'].iloc[0])})")
        L.append(f"| [{Q_EDGES[qb]:.1f},{Q_EDGES[qb] + 0.1:.1f}) | " + " | ".join(cells) + " |")
    return L


def run(root, out, reps=20000, last=date(2026, 9, 25)):
    markets, fills = oc.load(root, extra=["maker_direction", "fee_usdc", "agg"])
    markets = markets[pd.to_datetime(markets["start"], unit="s", utc=True).dt.date <= last]
    pos = positions(fills, markets)
    m = per_market(pos)
    sel_half, test_half = m[m["day"] < SPLIT], m[m["day"] >= SPLIT]
    s_sel = cell_stats(sel_half, reps=2000)
    chosen = select(s_sel)
    k = max(len(chosen), 1)
    L = ["# 九月谁在赚钱：挂单方和吃单方，按价格和剩余时间", "",
         "设计写在 `makers.py` 文件头，**在按这种方式拆分任何九月盈亏之前定好**：9 月 1–13 日选格子，14–25 日检验。", "",
         f"数据：{len(m[['coin', 'start']].drop_duplicates()):,} 个市场，{len(pos):,} 笔链上成交（挂单方 {int((~pos['agg']).sum()):,} 笔，"
         f"吃单方 {int(pos['agg'].sum()):,} 笔）。每个市场在每个格子里只算一次（按份数加权的价格）。", "",
         "## 核对：哪一行是吃单方", "",
         "| 行 | 笔数 | 付了手续费的比例 | 平均手续费/份 |", "|---|---:|---:|---:|"]
    for agg, g in fills.groupby("agg", observed=True):
        paid = (g["fee_usdc"] > 0).mean()
        per = (g["fee_usdc"] / g["token_amount"]).mean()
        L.append(f"| {'交易所事件（吃单方）' if agg else '挂单成交'} | {len(g):,} | {paid:.1%} | {100 * per:.2f}¢ |")
    L += ["", "吃单方付费、挂单方不付费，说明分类对了。", "",
          f"## 选出的格子（9 月 1–13 日：≥{MIN_MARKETS} 个市场、EV > 0、p < {SELECT_P}，每方最多 {PER_ROLE} 个）", ""]
    if chosen.empty:
        L.append("没有格子达到选入标准。")
    else:
        L += ["| 格子 | 选择期 笔数 / 胜率 / 平均价 / EV / p | 检验期 笔数 / 胜率 / 平均价 / EV / p | 检验期为正的币种 | 通过 |",
              "|---|---|---|---:|:-:|"]
    passed = []
    for _, r in chosen.iterrows():
        g = test_half[(test_half["agg"] == r["agg"]) & (test_half["qb"] == r["qb"]) & (test_half["tb"] == r["tb"])]
        if g.empty:
            L.append(f"| {label(r)} | {fmt(r['n'], r['win'], r['q'], r['ev'], r['p'])} | 0 | – | |")
            continue
        p = bo.fair_price_pvalue(g["pnl"].to_numpy(), (g["q"] + g["fee"]).to_numpy(), sims=reps)
        coins = g.groupby("coin")["pnl"].mean()
        ok = g["pnl"].mean() > 0 and p < 0.05 / k and (coins > 0).sum() >= 4
        if ok:
            passed.append(label(r))
        L.append(f"| {label(r)} | {fmt(r['n'], r['win'], r['q'], r['ev'], r['p'])} | "
                 f"{fmt(len(g), g['won'].mean(), g['q'].mean(), g['pnl'].mean(), p)} | "
                 f"{int((coins > 0).sum())}/{len(coins)} | {'✓' if ok else ''} |")
    L += ["", f"检验门槛：p < 0.05 / {k} = {0.05 / k:.4f}，EV > 0，且至少 4 个币种为正。", "",
          f"**结论：{len(passed)} 个格子通过。**" + (" " + "；".join(passed) if passed else ""), "",
          "## 全月每格 EV（括号里是市场数；挂单方不付费，吃单方扣 taker 费）", ""]
    s_all = cell_stats(m, with_p=False)
    L += heat(s_all, False, "挂单方") + [""] + heat(s_all, True, "吃单方")
    L += ["", "注意：挂单方的数字是排在队列前面的人实际拿到的结果。新挂的单排在队尾，"
          "能成交的往往是更大、更有信息的吃单，所以对新加入的人是上限；平台可能另有挂单返佣，这里没算。"]
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))
    return passed


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root")
    ap.add_argument("--out", default="real/makers-sept.md")
    ap.add_argument("--reps", type=int, default=20000)
    a = ap.parse_args(argv)
    run(a.root, a.out, a.reps)


if __name__ == "__main__":
    main()
