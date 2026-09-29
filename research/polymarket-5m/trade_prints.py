"""Who made money trading late: every trade print of a recorded day, by price and time left.

    python trade_prints.py <data root> [--out real/trade-prints.md]

`last_trade_price.side` is the taker's side (checked against the prevailing
quote: BUY prints sit at the ask, SELL prints at the bid). For each print we
score the *buyer* of the printed token per share: a taker buyer pays the fee,
a maker buyer (the print was a taker SELL) does not. Size-weighted; the t
value clusters by market, because prints inside one market share one outcome.
Cells where almost nothing lost have near-zero variance and an inflated t,
so the number of losing markets is shown next to it.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

import binary as bo
from bot import replay

TAU = [(0, 10), (10, 30), (30, 60), (60, 120), (120, 300)]
PX = [(0.0, 0.05), (0.05, 0.2), (0.2, 0.5), (0.5, 0.8), (0.8, 0.9), (0.9, 0.97), (0.97, 1.0)]


def load(root):
    ms = replay.load_markets(root)
    tok = {}
    for m in ms.values():
        if m.up_won is None:
            continue
        tok[m.up_token] = (m.slug, m.end, float(m.up_won))
        tok[m.down_token] = (m.slug, m.end, 1.0 - m.up_won)
    rows = []
    for path in replay.files(root, "last_trade_price"):
        for line in replay._lines(path):
            d = json.loads(line)
            p = d["payload"]
            hit = tok.get(p["asset_id"])
            if hit:
                rows.append((hit[0], hit[1], hit[2], int(p["timestamp"]), p["side"], float(p["price"]), float(p["size"])))
    tr = pd.DataFrame(rows, columns=["slug", "end", "win", "ts_ms", "side", "price", "size"])
    tr["tau"] = tr["end"] - tr["ts_ms"] / 1000
    tr = tr[(tr["tau"] > 0) & (tr["tau"] <= 300)]
    fee = bo.taker_fee(tr["price"])
    tr["pnl"] = tr["win"] - tr["price"] - np.where(tr["side"] == "BUY", fee, 0.0)
    return tr, len(ms)


def cell(g):
    s = g["size"].to_numpy()
    mu = float((g["pnl"] * s).sum() / s.sum())
    per = (g.assign(x=(g["pnl"] - mu) * g["size"]).groupby("slug")["x"].sum())
    se = float(np.sqrt((per ** 2).sum()) / s.sum())
    lost = int(g.loc[g["win"] == 0, "slug"].nunique())
    return mu, (mu / se if se > 0 else float("nan")), g["slug"].nunique(), lost


def table(tr, side):
    sub = tr[tr["side"] == side]
    head = "| 剩余 \\ 买入价 | " + " | ".join(f"{a:.2f}–{b:.2f}" for a, b in PX) + " |"
    lines = [head, "|---|" + "---:|" * len(PX)]
    for a, b in TAU:
        cells = []
        for pa, pb in PX:
            g = sub[(sub["tau"] > a) & (sub["tau"] <= b) & (sub["price"] >= pa) & (sub["price"] < pb)]
            if g["slug"].nunique() < 10:
                cells.append("–")
                continue
            mu, t, n, lost = cell(g)
            cells.append(f"{mu * 100:+.1f}¢ (t {t:+.1f}; {n} 市/{lost} 输)")
        lines.append(f"| {a}–{b}s | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    tr, n = load(a.root)
    doc = f"""# 成交回放：谁在最后几分钟赚钱

{n} 个市场，{len(tr):,} 笔成交，{tr['size'].sum():,.0f} 份。每格：买入方每份净盈亏（taker 买方已扣费）、按市场聚类的 t、
（涉及市场数 / 其中买入方输掉的市场数）。输掉的市场 ≤ 1 的格子方差接近 0，t 不可信。

## taker 买入（print side = BUY）

{table(tr, "BUY")}

## maker 买入（print side = SELL：taker 卖出，挂买单的一方接）

{table(tr, "SELL")}
"""
    if a.out:
        Path(a.out).write_text(doc, encoding="utf-8")
    print(doc)


if __name__ == "__main__":
    main()
