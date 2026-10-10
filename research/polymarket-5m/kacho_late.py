"""Late entry after a Binance jump on kacho.io's per-second Polymarket books (exploratory;
written down before it was run, 2026-10-04).

regime.py found that after a 2-sigma one-second Binance jump the price keeps drifting the same way
for minutes, and that a driftless 5-minute binary model underprices the side of the jump even 5-10
seconds later, most when Deribit's DVOL is high relative to realised volatility. If Polymarket's
own quotes, once they have caught up with the jump, still price it like that model, a buyer who is
seconds late (no latency race) still has an edge. kacho.io's BTC 5m books (24 Mar - 18 May 2026,
one top-of-book row a second, CC0) played no part in G, H or regime.py.

Fixed before running:
- Jumps: regime.features / the 2-sigma one-second rule on Binance BTCUSDT 1 s klines, the first of
  each 10 s, with 240..15 s left in the market window.
- Entry d seconds after the jump second, d in 1, 2, 3, 5, 10, 20, 30: the side of the jump at the
  ask kacho shows in its row stamped d - 1 seconds after the jump second (a row stamped s may hold
  quotes up to s + 0.999 s, so it is used from s + 1), at most 5 s old, ask in 0.02..0.98 and at
  least 5 shares shown; taker fee 0.07 p (1 - p); held to the official (Gamma) outcome.
- Per share, clustered by market; every jump and only each market's first jump.
- By DVOL - RV30 and by VR10 quintile, with the cut points regime.py took from May 25 - Jul 15.
- Controls: the other side at the same moment (fading the jump), and the side of a random sign at
  a random second of the same markets.
- Hypotheses: (K1) at d >= 2 the side of the jump earns more than the fee; (K2) it earns more in the
  top DVOL - RV quintile than in the bottom one.

    python kacho_late.py --kacho DIR --klines DIR --dvol dvol.csv [--out real/kacho-late.md]
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

import regime as rg

DS = (1, 2, 3, 5, 10, 20, 30)


def load_books(kacho):
    m = pd.read_parquet(Path(kacho) / "btc_markets.parquet", columns=["condition_id", "slug", "market_start"])
    o = pd.read_csv(Path(kacho) / "btc_outcomes.csv")
    m = m.merge(o, on="slug")
    m["start"] = pd.to_datetime(m["market_start"], utc=True).astype("int64") // 10**9
    t = pd.read_parquet(Path(kacho) / "btc_ticks.parquet", columns=["condition_id", "t", "au", "ad", "sau", "sad"])
    t = t.merge(m[["condition_id", "start", "up_won"]], on="condition_id")
    return m, t.sort_values(["start", "t"]).reset_index(drop=True)


def entries(jumps, ticks, ds=DS, seed=0):
    """Rows: jump, d, side ask and size from the row stamped s + d - 1 (<= 5 s old), won, pnl, and
    the same for the opposite side; plus random-time controls."""
    by = {s: g for s, g in ticks.groupby("start", sort=False)}
    rows = []
    rng = np.random.default_rng(seed)
    for sec, sign, ivrv, vr10 in zip(jumps["sec"], jumps["sign"], jumps["ivrv"], jumps["vr10"]):
        S = (sec // 300) * 300
        left = S + 300 - sec
        if not (15 <= left <= 240) or S not in by:
            continue
        g = by[S]
        tt = g["t"].to_numpy()
        up_won = float(g["up_won"].iloc[0])
        for kind, when, sg in (("jump", sec, sign), ("random", S + int(rng.integers(60, 286)), rng.choice([-1, 1]))):
            for d in ds:
                k = np.searchsorted(tt, when + d - 1, "right") - 1
                if k < 0 or when + d - 1 - tt[k] > 5:
                    continue
                for side, s_ in (("with", sg), ("against", -sg)):
                    up = s_ > 0
                    ask = g["au"].iloc[k] if up else g["ad"].iloc[k]
                    size = g["sau"].iloc[k] if up else g["sad"].iloc[k]
                    if not (np.isfinite(ask) and 0.02 <= ask <= 0.98 and size >= 5):
                        continue
                    won = up_won if up else 1 - up_won
                    rows.append((kind, side, S, sec, d, ask, won, won - ask - 0.07 * ask * (1 - ask), ivrv, vr10))
    return pd.DataFrame(rows, columns=["kind", "side", "market", "sec", "d", "ask", "won", "pnl", "ivrv", "vr10"])


def cl(t):
    if not len(t):
        return np.nan, np.nan, 0
    g = t.groupby("market")["pnl"].agg(["sum", "count"])
    m = g["sum"].sum() / g["count"].sum()
    r = g["sum"] - m * g["count"]
    k = len(g)
    return m, np.sqrt((r ** 2).sum() * k / max(k - 1, 1)) / g["count"].sum(), len(t)


def fmt(t):
    m, se, n = cl(t)
    return f"{100 * m:+.2f}¢ ±{100 * se:.2f}（{n:,}）" if n else "–"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kacho", required=True)
    ap.add_argument("--klines", required=True)
    ap.add_argument("--dvol", required=True)
    ap.add_argument("--out", default="real/kacho-late.md")
    a = ap.parse_args(argv)
    f = rg.add_dvol(rg.features(rg.load_klines(a.klines)), a.dvol)
    # jumps on the whole kline range; period labels from regime.PERIODS plus K for kacho's span
    r, sg = f["r"].to_numpy(), f["sigma"].to_numpy()
    with np.errstate(invalid="ignore"):
        hit = np.flatnonzero(np.abs(r) > 2.0 * sg)
    keep, last = [], -np.inf
    for i in hit:
        if i - last >= 10:
            keep.append(i)
            last = i
    keep = np.array(keep, dtype=int)
    j = pd.DataFrame({"sec": f.index.to_numpy()[keep], "sign": np.sign(r[keep]),
                      "ivrv": f["ivrv"].to_numpy()[keep], "vr10": f["vr10"].to_numpy()[keep]})
    day = pd.to_datetime(j["sec"], unit="s", utc=True).dt.strftime("%Y-%m-%d")
    a_mask = (day >= "2026-05-25") & (day < "2026-07-16")
    ivrv_cuts, vr_cuts = rg.cuts(j.loc[a_mask, "ivrv"]), rg.cuts(j.loc[a_mask, "vr10"])
    j = j[(day >= "2026-03-24") & (day < "2026-05-19")]
    _, ticks = load_books(a.kacho)
    e = entries(j, ticks)
    e["qi"] = rg.bucket(e["ivrv"].to_numpy(), ivrv_cuts)
    e["qv"] = rg.bucket(e["vr10"].to_numpy(), vr_cuts)
    first = e[e["kind"] == "jump"].sort_values("sec").groupby(["market", "d", "side"]).head(1)
    L = ["# 急动之后晚几秒进场（kacho.io 每秒盘口，3/24–5/18，BTC 5m；探索性，事先写定）", "",
         f"币安 2σ 一秒急动 {len(j):,} 次（每 10 秒第一次）。按急动方向，在急动后第 d 秒按 kacho 显示的卖一买入（至少 5 份），"
         "付 taker 费，持有到官方结算。每份 ± 按市场聚类的标准误（笔数）。", "",
         "| 晚 d 秒 | 顺急动方向 | 只买每个市场第一次急动 | 反方向（逆势） | 随机时刻、随机方向 |", "|---|---:|---:|---:|---:|"]
    for d in DS:
        sel = e["d"] == d
        L.append(f"| {d} | {fmt(e[sel & (e.kind == 'jump') & (e.side == 'with')])} | "
                 f"{fmt(first[(first.d == d) & (first.side == 'with')])} | "
                 f"{fmt(e[sel & (e.kind == 'jump') & (e.side == 'against')])} | "
                 f"{fmt(e[sel & (e.kind == 'random') & (e.side == 'with')])} |")
    for col, name, cuts_ in (("qi", "DVOL − RV30", ivrv_cuts), ("qv", "VR10", vr_cuts)):
        L += ["", f"## 顺急动方向，按 {name} 五分位（切点来自 5/25–7/15：{', '.join(f'{c:.2f}' for c in cuts_)}）", "",
              "| 五分位 | " + " | ".join(f"晚 {d} 秒" for d in DS) + " |", "|---|" + "---:|" * len(DS)]
        for q in range(5):
            cells = [fmt(e[(e.d == d) & (e.kind == "jump") & (e.side == "with") & (e[col] == q)]) for d in DS]
            L.append(f"| {q + 1} | " + " | ".join(cells) + " |")
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
