"""Backtest taker strategies on real Polymarket 5m order-book ticks.

Built for the kacho.io dataset (<coin>_markets.parquet + <coin>_ticks.parquet,
joined on condition_id, one top-of-book sample per second), but column names
are resolved from candidate lists and can be overridden with --col.

    python backtest_book.py data/btc_markets.parquet data/btc_ticks.parquet
    python backtest_book.py data/*_markets.parquet data/*_ticks.parquet --out real.md
    python backtest_book.py m.parquet t.parquet --col ticks.bid_up=best_bid --col markets.end=close

It needs only the book and the resolved outcome, no underlying price, so it
tests the market-level claims in README §4 and §7: calibration by time to
close, and whether buying the favourite or the longshot at the ask beats
spread plus fees. Standard errors are clustered by window close time, since
every coin's market closes on the same 5-minute grid and moves together.
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import binary as bo

TICK_COLS = {
    "cid": ["condition_id", "conditionId", "market_id", "cid"],
    "ts": ["ts_utc", "ts", "timestamp", "time"],
    "bid_up": ["bu", "bid_up", "up_bid", "best_bid_up"],
    "ask_up": ["au", "ask_up", "up_ask", "best_ask_up"],
    "bid_down": ["bd", "bid_down", "down_bid", "best_bid_down"],
    "ask_down": ["ad", "ask_down", "down_ask", "best_ask_down"],
}
MARKET_COLS = {
    "cid": TICK_COLS["cid"],
    "end": ["end_ts", "end_utc", "end_time", "window_end", "close_ts", "end_date", "endDate"],
    "start": ["start_ts", "start_utc", "start_time", "window_start", "open_ts", "start_date", "startDate"],
    "outcome": ["outcome", "winner", "winning_outcome", "resolved_outcome", "resolution", "result", "up_won"],
}
OPTIONAL = {"ticks": {"bid_down", "ask_down"}, "markets": {"end", "start"}}

TAUS = (120, 60, 30, 10)
SNAP_TOLERANCE_S = 5
STRATEGIES = {  # name: (side, ask low, ask high)
    "买强势方 0.80–0.97": ("fav", 0.80, 0.97),
    "买强势方 0.97–0.99（扫尾盘）": ("fav", 0.97, 0.99),
    "买弱势方 0.03–0.20": ("weak", 0.03, 0.20),
    "买弱势方 0.01–0.03": ("weak", 0.01, 0.03),
}
# Settlement rule changes for 5m markets (UTC).
ERAS = [("单点结算", None), ("30s TWAP", "2026-08-07"), ("60s TWAP", "2026-08-14")]


# ------------------------------------------------------------------- loading

def read_table(path):
    path = Path(path)
    if path.suffix == ".parquet":
        return pd.read_parquet(path)
    if path.suffix in (".csv", ".gz"):
        return pd.read_csv(path)
    raise SystemExit(f"不支持的文件类型：{path}")


def resolve_columns(df, candidates, overrides, kind):
    found = {}
    for key, names in candidates.items():
        if key in overrides:
            if overrides[key] not in df.columns:
                raise SystemExit(f"{kind}: --col 指定的列 {overrides[key]!r} 不存在。现有列：{list(df.columns)}")
            found[key] = overrides[key]
            continue
        hit = next((n for n in names if n in df.columns), None)
        if hit is not None:
            found[key] = hit
        elif key not in OPTIONAL[kind]:
            raise SystemExit(f"{kind}: 找不到 {key} 列（试过 {names}）。现有列：{list(df.columns)}\n"
                             f"用 --col {kind}.{key}=<列名> 指定。")
    if kind == "markets" and "end" not in found and "start" not in found:
        raise SystemExit(f"markets: 需要 end 或 start 列。现有列：{list(df.columns)}")
    return found


def to_seconds(s):
    """Epoch seconds from datetimes, ISO strings, or numeric s/ms/us/ns."""
    if pd.api.types.is_numeric_dtype(s) and not pd.api.types.is_bool_dtype(s):
        v = s.astype(float)
        scale = next((f for lim, f in ((1e17, 1e9), (1e14, 1e6), (1e11, 1e3)) if v.abs().median() > lim), 1.0)
        return v / scale
    dt = pd.to_datetime(s, utc=True)
    return (dt - pd.Timestamp(0, tz="UTC")) / pd.Timedelta(seconds=1)


def parse_up(s):
    """True where Up won. Accepts bool, 0/1, or strings like Up/Down/Yes/No."""
    if pd.api.types.is_bool_dtype(s):
        return s.astype(float)
    if pd.api.types.is_numeric_dtype(s):
        return s.where(s.isin([0, 1])).astype(float)
    t = s.astype(str).str.strip().str.lower()
    return t.map({"up": 1.0, "yes": 1.0, "true": 1.0, "1": 1.0,
                  "down": 0.0, "no": 0.0, "false": 0.0, "0": 0.0})


def as_probability(s):
    s = s.astype(float)
    return s / 100.0 if s.max(skipna=True) > 1.5 else s


def load(markets_paths, ticks_paths, overrides):
    mk_parts, tk_parts = [], []
    for mp in markets_paths:
        df = read_table(mp)
        cols = resolve_columns(df, MARKET_COLS, overrides.get("markets", {}), "markets")
        out = pd.DataFrame({"cid": df[cols["cid"]].astype(str), "up": parse_up(df[cols["outcome"]])})
        if "end" in cols:
            out["end"] = to_seconds(df[cols["end"]])
        else:
            out["end"] = to_seconds(df[cols["start"]]) + bo.WINDOW_S
        out["coin"] = Path(mp).name.split("_")[0]
        mk_parts.append(out)
    for tp in ticks_paths:
        df = read_table(tp)
        cols = resolve_columns(df, TICK_COLS, overrides.get("ticks", {}), "ticks")
        out = pd.DataFrame({"cid": df[cols["cid"]].astype(str), "ts": to_seconds(df[cols["ts"]])})
        for k in ("bid_up", "ask_up", "bid_down", "ask_down"):
            if k in cols:
                out[k] = as_probability(df[cols[k]])
        tk_parts.append(out)

    markets = pd.concat(mk_parts, ignore_index=True)
    n_all = len(markets)
    markets = markets.dropna(subset=["up", "end"]).drop_duplicates("cid")
    if len(markets) < n_all:
        print(f"注意：丢弃 {n_all - len(markets)} 个没有明确结果的市场", file=sys.stderr)
    ticks = pd.concat(tk_parts, ignore_index=True).dropna(subset=["ts"])
    return markets, ticks


# ------------------------------------------------------------------ snapshots

def effective_book(df):
    """Best Up bid/ask and Down ask, using the complementary book where present."""
    bid_up, ask_up = df["bid_up"], df["ask_up"]
    if "ask_down" in df:
        bid_up = np.fmax(bid_up, 1.0 - df["ask_down"])
    if "bid_down" in df:
        ask_up = np.fmin(ask_up, 1.0 - df["bid_down"])
    ask_down = 1.0 - bid_up
    return bid_up, ask_up, ask_down


def snapshots(markets, ticks, taus=TAUS, tolerance=SNAP_TOLERANCE_S):
    """Last tick at or before close - tau, per market, for each tau."""
    right = ticks.sort_values("ts")
    rows = []
    for tau in taus:
        left = markets.assign(target=markets["end"] - tau).sort_values("target")
        snap = pd.merge_asof(left, right, left_on="target", right_on="ts", by="cid",
                             direction="backward", tolerance=tolerance)
        snap["tau"] = tau
        rows.append(snap.dropna(subset=["ts"]))
    snap = pd.concat(rows, ignore_index=True)
    bid_up, ask_up, ask_down = effective_book(snap)
    snap["bid_up"], snap["ask_up"], snap["ask_down"] = bid_up, ask_up, ask_down
    snap = snap[(snap["bid_up"] >= 0) & (snap["ask_up"] <= 1) & (snap["bid_up"] <= snap["ask_up"])]
    snap["mid"] = (snap["bid_up"] + snap["ask_up"]) / 2
    return snap.reset_index(drop=True)


def era_of(end_seconds):
    labels = np.full(len(end_seconds), ERAS[0][0], dtype=object)
    for label, start in ERAS[1:]:
        labels[end_seconds.to_numpy() >= pd.Timestamp(start, tz="UTC").timestamp()] = label
    return labels


# ------------------------------------------------------------------- stats

def clustered_t(pnl, clusters):
    n = len(pnl)
    if n < 2:
        return np.nan
    dev = pd.Series(pnl - pnl.mean()).groupby(np.asarray(clusters)).sum()
    se = math.sqrt((dev**2).sum()) / n
    return pnl.mean() / se if se > 0 else np.nan


def trades(snap, side, lo, hi, fee_rate):
    fav_up = snap["mid"] >= 0.5
    buy_up = fav_up if side == "fav" else ~fav_up
    ask = np.where(buy_up, snap["ask_up"], snap["ask_down"]).round(4)
    mask = (ask >= lo - 1e-9) & (ask <= hi + 1e-9)
    win = np.where(buy_up, snap["up"] == 1, snap["up"] == 0)[mask].astype(float)
    ask = ask[mask]
    mid_side = np.where(buy_up, snap["mid"], 1 - snap["mid"])[mask]
    fee = bo.taker_fee(ask, fee_rate)
    return dict(pnl=win - ask - fee, gross=win - mid_side, cost=ask, fee=fee,
                spread=ask - mid_side, win=win, cluster=snap["end"].to_numpy()[mask])


def fmt_trade_row(name, tau, t):
    n = len(t["pnl"])
    if n < 2:
        return f"| {name} | {tau}s | {n} | – | – | – | – | – | – |"
    return (f"| {name} | {tau}s | {n:,} | {t['win'].mean()*100:.1f}% | {t['cost'].mean():.3f} | "
            f"{t['gross'].mean()*100:+.2f}¢ | {(t['spread'].mean() + t['fee'].mean())*100:.2f}¢ | "
            f"{t['pnl'].mean()*100:+.2f}¢ ({t['pnl'].mean()/t['cost'].mean()*100:+.2f}%) | "
            f"{clustered_t(t['pnl'], t['cluster']):+.1f} |")


def strategy_table(snap, fee_rate):
    head = ("| 策略 | 剩余 | 笔数 | 胜率 | 平均成本 | 毛 EV（按 mid） | 价差+费 | 净 EV/份（ROI） | 聚类 t |\n"
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    lines = [head]
    for name, (side, lo, hi) in STRATEGIES.items():
        for tau in sorted(snap["tau"].unique(), reverse=True):
            t = trades(snap[snap["tau"] == tau], side, lo, hi, fee_rate)
            lines.append(fmt_trade_row(name, tau, t))
    return "\n".join(lines)


def calibration_table(snap, tau):
    s = snap[snap["tau"] == tau]
    edges = [0, 0.03, 0.1, 0.2, 0.35, 0.5, 0.65, 0.8, 0.9, 0.97, 1.0001]
    lines = ["| mid 区间 | 样本 | 平均 mid | 实际 Up 频率 | 偏差 | ±2SE |", "|---|---:|---:|---:|---:|---:|"]
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (s["mid"] >= lo) & (s["mid"] < hi)
        n = int(m.sum())
        if n < 30:
            continue
        f, p = s.loc[m, "up"].mean(), s.loc[m, "mid"].mean()
        se = math.sqrt(max(f * (1 - f), 1e-4) / n)
        lines.append(f"| {lo:.2f}–{min(hi, 1):.2f} | {n:,} | {p:.3f} | {f:.3f} | "
                     f"{(f - p)*100:+.2f} 个百分点 | {2*se*100:.2f} |")
    return "\n".join(lines)


def report(markets, snap, fee_rate):
    out = ["# 真实盘口回测", ""]
    out.append(f"市场数 {len(markets):,}（{', '.join(sorted(markets['coin'].unique()))}），"
               f"时间 {pd.to_datetime(markets['end'].min(), unit='s', utc=True):%Y-%m-%d} 至 "
               f"{pd.to_datetime(markets['end'].max(), unit='s', utc=True):%Y-%m-%d}。"
               f"吃单按当时 ask 成交，taker 费率 {fee_rate}，每笔 1 份持有到结算。")
    out.append("快照取收盘前 τ 秒或更早的最后一个 tick（最多早 5 秒）。"
               "Down 的 ask 取 1 − Up 的最佳 bid（有 Down 盘口时与之取优）。")
    snap = snap.assign(era=era_of(snap["end"]))
    for era, part in snap.groupby("era", sort=False):
        out += ["", f"## {era}（{part['cid'].nunique():,} 个市场）", "", "### 策略", "",
                strategy_table(part, fee_rate)]
        for tau in (60, 10):
            if (part["tau"] == tau).any():
                out += ["", f"### 校准（τ={tau}s）", "", calibration_table(part, tau)]
    out += ["", "读法：毛 EV ≈ 0 说明报价校准；净 EV 显著为正（|聚类 t| > 2 且样本够大）才值得往下做。",
            "注意多重比较：这里有十几行，偶尔一行 |t|≈2 并不说明什么；同一 τ 的强势方和弱势方行是同一批状态的两面，不算两个证据。",
            "笔数少于约 300 的行噪声很大，胜率接近 0 或 1 时样本标准差偏小，t 值会被高估；"
            "这时看 binary.fair_price_pvalue 的精确 p 值（零假设：胜率 = 买价 + 手续费）。"]
    return "\n".join(out)


def parse_overrides(items):
    overrides = {}
    for item in items or []:
        key, _, col = item.partition("=")
        kind, _, field = key.partition(".")
        if kind not in ("ticks", "markets") or not field or not col:
            raise SystemExit(f"--col 格式应为 ticks.<字段>=<列名> 或 markets.<字段>=<列名>：{item}")
        overrides.setdefault(kind, {})[field] = col
    return overrides


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", help="*_markets.* 和 *_ticks.* 文件")
    ap.add_argument("--col", action="append", help="列名覆盖，如 ticks.bid_up=bu")
    ap.add_argument("--fee-rate", type=float, default=bo.CRYPTO_FEE_RATE)
    ap.add_argument("--out", help="写入 markdown 文件（默认打印）")
    args = ap.parse_args(argv)

    markets_paths = [f for f in args.files if "_markets" in Path(f).name]
    ticks_paths = [f for f in args.files if "_ticks" in Path(f).name]
    if not markets_paths or not ticks_paths:
        raise SystemExit("需要至少一个 *_markets.* 和一个 *_ticks.* 文件")

    markets, ticks = load(markets_paths, ticks_paths, parse_overrides(args.col))
    snap = snapshots(markets, ticks)
    if snap.empty:
        raise SystemExit("没有匹配到任何快照：检查 markets 的 end/start 与 ticks 的 ts 是否同一时间基准。")
    text = report(markets, snap, args.fee_rate)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
