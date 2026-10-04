"""Do BTC jumps continue or reverse depending on the hedging regime, and does that move the G/H
edge? (exploratory; Binance public klines and the May-August per-print file, no orders)

Idea. Options dealers delta-hedge. Short gamma (customers long options) they buy into rallies and
sell into drops, so moves extend and jump; long gamma they lean against moves, so moves fade and
price pins. Historical dealer positioning is not free, but its footprint is: the autocorrelation of
returns. The variance ratio VR = var(10 s returns) / (10 var(1 s returns)) over the trailing 30
minutes is above 1 when moves extend and below 1 when they fade. G and H price a 5-minute binary
as if the price were a driftless random walk with the trailing 1 s volatility, so a regime that
makes a jump continue (or fade) should show up as a larger (or smaller) edge, and as an edge that
survives a later entry.

Fixed before any result was looked at (2026-10-04):
- Binance BTCUSDT 1 s klines (data.binance.vision). sigma: std of 1 s log returns over the
  previous 600 s (at least 300), up to the previous second. A jump: a second whose return exceeds
  2 sigma; the first of each 10 s.
- Regime features, all from seconds strictly before the jump second: VR10 over 1,800 s (as above),
  VR60 = var(60 s returns) / (60 var(1 s returns)) over 3,600 s, RV30 (annualised std of 1 s
  returns over 1,800 s), taker-buy share of volume over the previous 30 s (signed toward the jump),
  hour of day (UTC), and Deribit's DVOL (1 min, the last value before the jump) with DVOL - RV30.
- Continuation: the log return from the jump second's close to h seconds later, h in 5, 15, 30,
  60, 120, 240, signed toward the jump and in units of sigma sqrt(h). Periods A (May 25 - Jul 15),
  B (Jul 16 - Aug 16), C (Aug 17 - Aug 29), S (Aug 30 - Sep 30).
- Regime buckets: quintiles of VR10 with cut points from period A, applied unchanged later.
- Hypotheses: (1) continuation rises with VR10 in A and again in B + C + S; (2) the per-share EV of
  G and H trades (per-print file, first trade per market and every fill 2 s apart) rises with VR10
  in A and again in B + C. A difference only counts if it has the same sign in both halves.
- Multi-factor and walk-forward: a ridge regression of a trade's P&L on its features (edge, z,
  tau, price, ask size, VR10, VR60, RV30, flow, DVOL - RV30, hour bucket), refit every
  week on all earlier weeks, trading only where the prediction is >= 0; compared week by week with
  the plain rule on the same trades.

    python regime.py --klines DIR [--dvol dvol.csv] [--prints real/cross-prints-0.csv.gz real/cross-prints-1.csv.gz]
                     [--out real/regime.md]
"""
from __future__ import annotations

import argparse
import io
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

HS = (5, 15, 30, 60, 120, 240)
PERIODS = (("A", "2026-05-25", "2026-07-16"), ("B", "2026-07-16", "2026-08-17"),
           ("C", "2026-08-17", "2026-08-30"), ("S", "2026-08-30", "2026-10-01"))


def load_klines(directory):
    """Per-second close, volume and taker-buy volume from Binance 1 s kline zips (open time in ms or
    us), on a complete 1 s grid; seconds without a kline carry the last close and zero volume."""
    parts = []
    for f in sorted(Path(directory).glob("BTCUSDT-1s-*.zip")):
        with zipfile.ZipFile(f) as z:
            raw = z.read(z.namelist()[0])
        d = pd.read_csv(io.BytesIO(raw), header=None, usecols=[0, 4, 5, 9])
        if not np.issubdtype(d[0].dtype, np.number):  # a header row
            d = d.iloc[1:].astype(float)
        parts.append(d)
    d = pd.concat(parts, ignore_index=True)
    t = d[0].to_numpy(np.int64)
    t = np.where(t > 10**14, t // 1_000_000, t // 1000)  # us or ms -> s
    s = pd.DataFrame({"close": d[4].to_numpy(float), "vol": d[5].to_numpy(float),
                      "buy": d[9].to_numpy(float)}, index=t)
    s = s[~s.index.duplicated()].sort_index()
    grid = pd.RangeIndex(int(s.index[0]), int(s.index[-1]) + 1)
    s = s.reindex(grid)
    s["close"] = s["close"].ffill()
    s[["vol", "buy"]] = s[["vol", "buy"]].fillna(0.0)
    return s


def features(s):
    """Per second: r (1 s log return), sigma, and the regime features, all known before the second."""
    lp = np.log(s["close"])
    r = lp.diff()
    f = pd.DataFrame(index=s.index)
    f["r"] = r
    f["sigma"] = r.rolling(600, min_periods=300).std().shift(1)
    v1_30 = r.rolling(1800, min_periods=900).var()
    r10 = lp.diff(10)
    f["vr10"] = (r10.rolling(1800, min_periods=900).var() / (10 * v1_30)).shift(1)
    r60 = lp.diff(60)
    f["vr60"] = (r60.rolling(3600, min_periods=1800).var() / (60 * r.rolling(3600, min_periods=1800).var())).shift(1)
    f["rv30"] = (np.sqrt(v1_30) * np.sqrt(86400 * 365)).shift(1)
    vol30, buy30 = s["vol"].rolling(30).sum().shift(1), s["buy"].rolling(30).sum().shift(1)
    f["buy30"] = (buy30 / vol30.replace(0, np.nan)) - 0.5
    f["hour"] = (s.index.to_numpy() // 3600) % 24
    f["lp"] = lp
    return f


def add_dvol(f, dvol):
    if dvol is None:
        f["dvol"] = np.nan
    else:
        d = pd.read_csv(dvol)
        sec = (d["t"].to_numpy(np.int64) // 1000) + 60  # a 1 min candle is known when it closes
        v = pd.Series(d["close"].to_numpy(float), index=sec).sort_index()
        v = v[~v.index.duplicated()]
        f["dvol"] = v.reindex(f.index, method="ffill").to_numpy() / 100
    f["ivrv"] = f["dvol"] - f["rv30"]
    return f


def period_of(sec):
    day = pd.to_datetime(sec, unit="s", utc=True).strftime("%Y-%m-%d")
    out = np.full(len(day), "", dtype=object)
    for name, lo, hi in PERIODS:
        out[(day >= lo) & (day < hi)] = name
    return out


def jumps(f, z0=2.0, gap=10):
    """First 2-sigma second of each `gap` s with its continuation at each horizon."""
    r, sg = f["r"].to_numpy(), f["sigma"].to_numpy()
    with np.errstate(invalid="ignore"):
        hit = np.flatnonzero(np.abs(r) > z0 * sg)
    keep, last = [], -np.inf
    for i in hit:
        if i - last >= gap:
            keep.append(i)
            last = i
    keep = np.array(keep, dtype=int)
    lp = f["lp"].to_numpy()
    sign = np.sign(r[keep])
    j = pd.DataFrame({"sec": f.index.to_numpy()[keep], "sign": sign, "z": np.abs(r[keep]) / sg[keep]})
    for h in HS:
        k = np.minimum(keep + h, len(lp) - 1)
        j[f"c{h}"] = np.where(keep + h < len(lp), sign * (lp[k] - lp[keep]) / (sg[keep] * np.sqrt(h)), np.nan)
    for c in ("vr10", "vr60", "rv30", "buy30", "hour", "dvol", "ivrv"):
        j[c] = f[c].to_numpy()[keep]
    j["buy30"] = j["buy30"] * j["sign"]  # flow toward the jump
    j["period"] = period_of(j["sec"].to_numpy())
    return j[j["period"] != ""].reset_index(drop=True)


def cuts(x, q=5):
    return np.nanquantile(x, np.linspace(0, 1, q + 1)[1:-1])


def bucket(x, c):
    return np.searchsorted(c, x, side="right")


def se_mean(x):
    x = x[np.isfinite(x)]
    return (x.mean(), x.std() / np.sqrt(len(x)) if len(x) > 1 else np.nan, len(x))


def continuation_table(j, feat="vr10"):
    c = cuts(j.loc[j["period"] == "A", feat])
    j = j.assign(q=bucket(j[feat].to_numpy(), c))
    L = [f"| {feat} 五分位（A 段切点） | 段 | 急动 | " + " | ".join(f"+{h} 秒" for h in HS) + " |",
         "|---|---|---:|" + "---:|" * len(HS)]
    for q in range(5):
        for name, sel in (("A", j["period"] == "A"), ("B+C+S", j["period"] != "A")):
            g = j[(j["q"] == q) & sel]
            cells = []
            for h in HS:
                m, se, _ = se_mean(g[f"c{h}"].to_numpy())
                cells.append(f"{m:+.3f} ±{se:.3f}")
            lo = "−∞" if q == 0 else f"{c[q - 1]:.2f}"
            hi = "∞" if q == 4 else f"{c[q]:.2f}"
            L.append(f"| {q + 1}（{lo}–{hi}） | {name} | {len(g):,} | " + " | ".join(cells) + " |")
    return L


def trades(prints, rule, every):
    """G or H trades from the per-print files (scalein's engine): first per market, or every fill."""
    import scalein as si
    t = pd.concat([si.episodes(p, rule) for p in prints], ignore_index=True)
    t = t.sort_values("t", kind="stable").reset_index(drop=True)
    name = si.BASE if every else "只买第一笔"
    r = {x.name: x for x in si.RULES}[name]
    w = si.weights(t, r)
    t = t[w > 0].copy()
    t["w"] = w[w > 0]
    return t.reset_index(drop=True)


def attach(t, f):
    sec = np.floor(t["t"].to_numpy()).astype(np.int64)
    idx = f.index.get_indexer(sec)
    ok = idx >= 0
    for c in ("vr10", "vr60", "rv30", "buy30", "hour", "dvol", "ivrv", "r", "sigma"):
        v = np.full(len(t), np.nan)
        v[ok] = f[c].to_numpy()[idx[ok]]
        t[c] = v
    sign = np.where(t["up"], 1.0, -1.0)
    t["buy30"] = t["buy30"] * sign
    return t


def clustered(t, col="pnl"):
    """Mean P&L per share and its standard error clustered by market."""
    if not len(t):
        return np.nan, np.nan, 0
    g = t.groupby("market_id")[col].agg(["sum", "count"])
    m = g["sum"].sum() / g["count"].sum()
    r = g["sum"] - m * g["count"]
    k = len(g)
    se = np.sqrt((r ** 2).sum() * k / max(k - 1, 1)) / g["count"].sum()
    return m, se, len(t)


def edge_table(t, feat="vr10", cut_from=None):
    c = cuts((cut_from if cut_from is not None else t.loc[t["period"] == "A", feat]))
    t = t.assign(q=bucket(t[feat].to_numpy(), c))
    L = ["| 五分位 | A 段每份（笔） | B+C 段每份（笔） |", "|---|---:|---:|"]
    for q in range(5):
        cells = []
        for sel in (t["period"] == "A", t["period"].isin(["B", "C"])):
            m, se, n = clustered(t[(t["q"] == q) & sel])
            cells.append(f"{100 * m:+.2f}¢ ±{100 * se:.2f}（{n:,}）")
        L.append(f"| {q + 1} | " + " | ".join(cells) + " |")
    return L


FEATS = ["edge", "z", "tau", "price", "lsize", "vr10", "vr60", "rv30", "buy30", "ivrv", "h_us", "h_asia"]


def design(t):
    x = pd.DataFrame({"edge": t["edge"], "z": np.minimum(t["z"], 8), "tau": t["tau"] / 240, "price": t["price"],
                      "lsize": np.log1p(t["size"]),
                      "vr10": np.clip(t["vr10"], 0, 3), "vr60": np.clip(t["vr60"], 0, 3), "rv30": t["rv30"],
                      "buy30": t["buy30"], "ivrv": t["ivrv"].fillna(0.0),
                      "h_us": t["hour"].between(13, 20).astype(float), "h_asia": t["hour"].between(0, 7).astype(float)})
    return x[FEATS]


def walk_forward(t, alpha=10.0):
    """Weekly refit of a ridge regression of P&L on the features (standardised on the training
    weeks), trading only where the prediction is >= 0; returns the trades of every test week with
    the prediction (NaN in the first four weeks, which only train)."""
    from numpy.linalg import solve
    t = t.copy()
    t["week"] = pd.to_datetime(t["t"], unit="s", utc=True).dt.to_period("W").astype(str)
    weeks = sorted(t["week"].unique())
    x = design(t).to_numpy(float)
    ok = np.isfinite(x).all(axis=1)
    t["pred"] = np.nan
    for k, wk in enumerate(weeks):
        if k < 4:
            continue
        tr = (t["week"].isin(weeks[:k]) & ok).to_numpy()
        te = ((t["week"] == wk) & ok).to_numpy()
        if tr.sum() < 200 or not te.any():
            continue
        mu, sd = x[tr].mean(0), x[tr].std(0) + 1e-9
        xt = (x[tr] - mu) / sd
        y = t.loc[tr, "pnl"].to_numpy()
        b = solve(xt.T @ xt + alpha * np.eye(xt.shape[1]), xt.T @ (y - y.mean()))
        t.loc[te, "pred"] = y.mean() + ((x[te] - mu) / sd) @ b
        t.loc[te, "coef_week"] = wk
    return t


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--klines", required=True)
    ap.add_argument("--dvol")
    ap.add_argument("--prints", nargs="*", default=["real/cross-prints-0.csv.gz", "real/cross-prints-1.csv.gz"])
    ap.add_argument("--out", default="real/regime.md")
    a = ap.parse_args(argv)
    f = add_dvol(features(load_klines(a.klines)), a.dvol)
    j = jumps(f)
    L = ["# 对冲区间（gamma 正负的足迹）和急动之后的延续（探索性）", "",
         f"币安 BTCUSDT 1 秒 K 线，{pd.to_datetime(f.index[0], unit='s'):%m-%d} → {pd.to_datetime(f.index[-1], unit='s'):%m-%d}；"
         f"2σ 急动 {len(j):,} 次（每 10 秒第一次）。延续 = 急动之后 h 秒朝急动方向又走了多少，单位 σ√h；"
         "正数是延续（短 gamma 的样子），负数是回吐（长 gamma 的样子）。", ""]
    L += ["## 急动后延续，按方差比 VR10 分组", ""] + continuation_table(j, "vr10")
    L += ["", "## 急动后延续，按 DVOL − 已实现波动分组", ""] + continuation_table(j.dropna(subset=["ivrv"]), "ivrv")
    L += ["", "## 急动后延续，按急动前 30 秒主动买卖方向分组", ""] + continuation_table(j, "buy30")
    for rule, every in (("G", False), ("H", False), ("H", True)):
        t = attach(trades(a.prints, rule, every), f)
        name = f"{rule} {'每次都加' if every else '首笔'}"
        L += ["", f"## {name}（5–8 月逐笔，0.3 秒）每份按 VR10 分组", ""] + edge_table(t, "vr10")
        L += ["", f"## {name} 每份按 DVOL − 已实现波动分组", ""] + edge_table(t.dropna(subset=["ivrv"]), "ivrv")
        wf = walk_forward(t)
        te = wf.dropna(subset=["pred"])
        sel = te[te["pred"] >= 0]
        m0, s0, n0 = clustered(te)
        m1, s1, n1 = clustered(sel)
        L += ["", f"{name} 每周滚动重拟合的多因子（岭回归）：测试周共 {n0:,} 笔，原规则每份 {100 * m0:+.2f}¢ ±{100 * s0:.2f}；"
              f"只做预测 ≥ 0 的 {n1:,} 笔，每份 {100 * m1:+.2f}¢ ±{100 * s1:.2f}；"
              f"被剔除的 {n0 - n1:,} 笔每份 {100 * clustered(te[te['pred'] < 0])[0]:+.2f}¢。"]
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
