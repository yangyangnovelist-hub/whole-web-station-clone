"""JUMP2S.md, local lane: the "buy the side of a >= 1.2 bp Binance jump 2 s later" rule on kacho.io's
per-second Polymarket books (2026-03-24 .. 05-18) and, without any Polymarket price, on Binance 1 s
klines alone (2026-03-14 .. 09-30). Paper research, market data only.

JUMP2S.md (committed before anything was run) fixes the design; jump2s.py holds the shared part (jump
detection, entry plan and controls, trend features, model fair, statistics, report tables). Per
JUMP2S.md both data sets here are side evidence (旁证): they get no verdict of their own; the report
shows the numbers the decision thresholds would give, labelled as such.

1. kacho.io books (BTC 5m, official outcome): big jumps (adjacent 1 s kline closes, >= 1.2 bp),
   small jumps (1.0..1.2 bp), the other side at the same moment, a random time and side; bought at
   the ask of the row stamped S + 2 (S = the jump second; used from S + 3), and as a secondary column
   at the row stamped S + 1 (used from S + 2); price / outcome decomposition; H1 / H2; by month and
   by day.
2. Binance 1 s klines only: every UTC 5-minute window; per jump the outcome side (continuation, bp,
   from S + 3 to the window end) and the model edge = won (Binance proxy outcome) - the driftless
   model's fair probability of the jump side at S + 3; by month March .. September, H1 / H2, days and
   3-day windows. This asks whether continuation regimes exist over 6.5 months and whether H1 / H2
   pick them out, not whether Polymarket prices leave money on the table.

    python jump2s_local.py --kacho DIR --klines DIR [--out real/jump2s-local.md] \\
        [--csv real/jump2s-local.csv.gz]

Where JUMP2S.md is silent, the conservative choice made here (on top of jump2s.py's):
- Klines are read raw (open time, close), not through regime.load_klines, which forward fills
  missing seconds: jump2s.jumps(mode="closes") must not bridge a gap.
- Jump time. A kline jump close(S) vs close(S - 1) is known when second S closes, at S + 1. That is
  the jump's t0 (jumps(..., t_obs=ts + 1)); t_ex = S is the exchange time. So the window filters read
  S + 1 - start >= 15 and end - (S + 1) >= 12, the 10 s spacing runs on S + 1, and the buy at
  t0 + 2 = S + 3 has >= 10 s left, which is JUMP2S.md's "row stamped S + 2, used from S + 3". The
  4 h / 1 h features use t_ex = S (minutes closed by S; never the jump second's own minute).
- kacho row: a row stamped s may hold quotes up to s + 0.999 s, so it is used from s + 1; a buy at
  time t uses the row stamped exactly floor(t) - 1 (S + 2 for a jump). JUMP2S.md allows a snapshot at
  most 1 s before the buy, and an earlier row holds quotes more than 1 s old, so a missing row means
  no trade (no fallback; kacho_late.py allowed 5 s, JUMP2S.md does not). Same for the secondary
  column (row S + 1, used from S + 2, i.e. 1 s after the jump is known).
- Ask in 0.02..0.98 and >= 5 shares at the ask (sau / sad; an empty side shows ask 1.0 and a NaN
  size, so it fails both). Official outcome: btc_outcomes.csv up_won (Gamma), via
  kacho_late.load_books. Taker fee 0.07 p (1 - p). Rows that do not trade are dropped (counted).
- Decomposition (kacho): the bought token's own mid (bid + ask) / 2 in the row stamped S - 1 (quotes
  up to S, before the jump second) and in the entry row, NaN unless both sides of that token's book
  are present (su / sd and sau / sad not NaN). Model fair at S (Binance close of S - 1) and at S + 3
  (close of S + 2): jump2s.model_fair, point-price model (every kacho market opened before
  2026-08-07). Outcome side: jump2s.continuation_bp from close(S + 2) to the close of the market's
  last second (start + 299).
- Random control: jump2s.random_controls (seed 0), one per kept big jump; bought from the row
  stamped floor(t0 + 2) - 1.
- Binance-only lane: every UTC 5-minute window 03-14 00:00 .. 09-30 23:55. Price to beat = close of
  the second before the window, settlement = close of the window's last second (point prices, so
  model fair, outcome and continuation all use twap = 0). The real markets settle on Chainlink, and
  from 08-07 on a 30 / 60 s TWAP; the outcome here is a Binance proxy (ties count as Up). Per row:
  won (proxy), price = model fair of the bought side at t0 + 2 (= S + 3; sigma = std of the 600
  one-second returns before), pnl = edge = won - fair (no fee, no Polymarket price), cont_bp. The
  "opposite" control is omitted there (its edge and continuation are exactly minus the jump's); the
  random control is the model's calibration check (expect ~0).
- Month blocks: kacho 3月 (from 03-24), 4月, 5月 (to 05-18), jump2s.KACHO_BLOCKS; Binance 3月 (from
  03-14) .. 9月, jump2s.KLINE_BLOCKS. The design's "two of three months" is shown as-is for kacho and
  scaled to 5 of 7 for the Binance months; neither is a verdict.
- Days: the design's 3-day threshold (+2c) is applied to the per-share pnl (kacho) and to the edge
  (Binance). A second, descriptive 3-day count for the continuation uses +1.0 bp (about the +1.06 bp
  the user saw on 09-30 .. 10-02); that threshold is not in JUMP2S.md.
"""
from __future__ import annotations

import argparse
import io
import time
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

import jump2s as j2
import kacho_late

KACHO_SPAN = ("2026-03-24", "2026-05-19")      # [first day, end day exclusive)
KLINE_SPAN = ("2026-03-14", "2026-10-01")
CONT_HOT_BP = 1.0                               # descriptive 3-day threshold for cont_bp (not in JUMP2S.md)
ENTRY_LAGS = (("book", 2.0), ("book_s1", 1.0))  # main: row S + 2 (buy at S + 3); secondary: row S + 1
LOCAL_PRICE_NAMES = {"book": "盘口价：S + 2 行（S + 3 起用，设计口径）", "book_s1": "盘口价：S + 1 行（S + 2 起用，副列）"}
BIN_KINDS = ("big", "small", "random")
ROW_COLS = ["market", "t0", "t_ex", "kind", "price_type", "sign", "jump_sign", "size_bp", "price", "won",
            "pnl", "mid0", "mid2", "fair0", "fair2", "cont_bp", "ret4h", "vr60", "h1", "h2"]


# --------------------------------------------------------------------------- loading
def _day(f):
    return Path(f).stem.rsplit("-1s-", 1)[-1]


def load_seconds(directory, first=None, end=None):
    """Binance BTCUSDT 1 s klines (data.binance.vision zips) -> (open time s int64, close float64),
    sorted, duplicates dropped (last kept), missing seconds NOT filled. `first` / `end` (YYYY-MM-DD,
    end exclusive) select files by the day in their name."""
    files = sorted(Path(directory).glob("BTCUSDT-1s-*.zip"))
    files = [f for f in files if (first is None or _day(f) >= first) and (end is None or _day(f) < end)]
    ts, cl = [], []
    for f in files:
        with zipfile.ZipFile(f) as z:
            raw = z.read(z.namelist()[0])
        d = pd.read_csv(io.BytesIO(raw), header=None, usecols=[0, 4])
        if not np.issubdtype(d[0].dtype, np.number):  # a header row
            d = d.iloc[1:].astype(float)
        t = d[0].to_numpy(np.int64)
        ts.append(np.where(t > 10**14, t // 1_000_000, t // 1000))  # us or ms -> s
        cl.append(d[4].to_numpy(float))
    if not ts:
        return np.array([], np.int64), np.array([], float)
    t, c = np.concatenate(ts), np.concatenate(cl)
    o = np.argsort(t, kind="stable")
    t, c = t[o], c[o]
    last = np.r_[t[1:] != t[:-1], True]
    return t[last], c[last]


def load_kacho(kacho):
    """(markets, ticks) from kacho_late.load_books plus the bids and bid sizes (bu, bd, su, sd)."""
    m, t = kacho_late.load_books(kacho)
    b = pd.read_parquet(Path(kacho) / "btc_ticks.parquet", columns=["condition_id", "t", "bu", "bd", "su", "sd"])
    t = t.merge(b, on=["condition_id", "t"], how="left")
    return m, t.sort_values(["start", "t"]).reset_index(drop=True)


def kline_jumps(ts, lp, windows):
    """jump2s.jumps on adjacent 1 s closes; t0 = S + 1 (when the jump second closes), t_ex = S."""
    ts = np.asarray(ts, float)
    return j2.jumps(ts, lp, windows, mode="closes", t_obs=ts + 1.0)


def window_grid(first_day, end_day):
    """Every UTC 5-minute window opening in [first_day, end_day): market = start (s)."""
    a = int(pd.Timestamp(first_day, tz="UTC").timestamp())
    b = int(pd.Timestamp(end_day, tz="UTC").timestamp())
    st = np.arange(a, b, 300, dtype=np.int64)
    return pd.DataFrame({"market": st, "start": st, "end": st + 300})


# --------------------------------------------------------------------------- kacho books
class Book:
    """kacho rows on a sorted (market start, stamp) key for exact-stamp lookups."""

    def __init__(self, ticks):
        t = ticks.sort_values(["start", "t"], kind="stable")
        st = t["start"].to_numpy(np.int64)
        self.key = st * 1000 + (t["t"].to_numpy(np.int64) - st)
        if len(self.key) and np.any(self.key[1:] <= self.key[:-1]):
            raise ValueError("duplicate (market, t) rows")
        self.cols = {c: t[c].to_numpy(float) for c in ("au", "ad", "sau", "sad", "bu", "bd", "su", "sd")
                     if c in t}
        self.up_won = t.groupby("start")["up_won"].first().astype(float)

    def row(self, start, stamp):
        """Index of the row of market `start` stamped exactly `stamp`, -1 if there is none."""
        start = np.asarray(start, np.int64)
        off = np.asarray(stamp, np.int64) - start
        key = start * 1000 + off
        k = np.searchsorted(self.key, key)
        kk = np.minimum(k, max(len(self.key) - 1, 0))
        ok = (off >= 0) & (off < 1000) & (k < len(self.key))
        if len(self.key):
            ok &= self.key[kk] == key
        return np.where(ok, k, -1)

    def _side(self, idx, sign, up, down):
        idx = np.asarray(idx)
        kk = np.maximum(idx, 0)
        v = np.where(np.asarray(sign) > 0, self.cols[up][kk], self.cols[down][kk]) if len(kk) else np.array([])
        return np.where(idx >= 0, v, np.nan)

    def ask(self, idx, sign):
        return self._side(idx, sign, "au", "ad"), self._side(idx, sign, "sau", "sad")

    def mid(self, idx, sign):
        """The bought token's (bid + ask) / 2, NaN unless both sides of its book are present."""
        a, sa = self.ask(idx, sign)
        b, sb = self._side(idx, sign, "bu", "bd"), self._side(idx, sign, "su", "sd")
        return np.where(np.isfinite(sa) & np.isfinite(sb), (a + b) / 2.0, np.nan)


def kacho_rows(p, book, sp, closes=None):
    """Price every plan row (jump2s.plan) on kacho books: one row per (plan row, entry lag) that
    trades. Returns (rows, counts) with counts[(price_type, kind)] = (planned, no row, ask/size)."""
    p = p.reset_index(drop=True)
    start = p["market"].to_numpy(np.int64)
    t0 = p["t0"].to_numpy(float)
    sign = p["sign"].to_numpy(float)
    up_won = book.up_won.reindex(start).to_numpy(float)
    won = np.where(sign > 0, up_won, 1.0 - up_won)
    # decomposition, on the plan rows (independent of the entry lag)
    tb = t0 + j2.ENTRY_S
    i0 = book.row(start, np.floor(t0).astype(np.int64) - 2)          # row S - 1
    i2 = book.row(start, np.floor(tb).astype(np.int64) - 1)          # row S + 2
    dec = dict(mid0=book.mid(i0, sign), mid2=book.mid(i2, sign),
               fair0=j2.model_fair(sp, sign, start, t0 - 1.0), fair2=j2.model_fair(sp, sign, start, tb),
               cont_bp=j2.continuation_bp(sp, sign, start, sp.price(tb)))
    parts, counts = [], {}
    for ptype, lag in ENTRY_LAGS:
        idx = book.row(start, np.floor(t0 + lag).astype(np.int64) - 1)
        ask, size = book.ask(idx, sign)
        ok = (idx >= 0) & j2.price_ok(ask, size) & np.isfinite(won)
        for k in j2.KINDS:
            m = (p["kind"] == k).to_numpy()
            counts[(ptype, k)] = (int(m.sum()), int((m & (idx < 0)).sum()), int((m & (idx >= 0) & ~ok).sum()))
        r = p.loc[ok, ["market", "t0", "t_ex", "kind", "sign", "jump_sign"]].copy()
        r["size_bp"] = p["size_bp"].to_numpy(float)[ok] if "size_bp" in p else np.nan
        r["price_type"] = ptype
        r["price"], r["won"] = ask[ok], won[ok]
        r["pnl"] = j2.pnl(won[ok], ask[ok])
        for c, v in dec.items():
            r[c] = v[ok] if ptype == "book" else np.nan
        parts.append(r)
    rows = pd.concat(parts, ignore_index=True)
    if closes is not None:
        rows = j2.add_trend(rows, closes)
    return rows.reindex(columns=ROW_COLS), counts


# --------------------------------------------------------------------------- Binance only
def binance_rows(p, sp, closes=None):
    """Big, small and random plan rows priced by the driftless model at t0 + 2 against the Binance
    point-price proxy outcome: price = fair, pnl = edge = won - fair, cont_bp."""
    p = p[p["kind"].isin(BIN_KINDS)].reset_index(drop=True)
    start = p["market"].to_numpy(np.int64)
    sign = p["sign"].to_numpy(float)
    tb = p["t0"].to_numpy(float) + j2.ENTRY_S
    up = j2.proxy_up(sp, start, twap=0)
    won = np.where(np.isfinite(up), np.where(sign > 0, up, 1.0 - up), np.nan)
    fair = j2.model_fair(sp, sign, start, tb, twap=0)
    r = p[["market", "t0", "t_ex", "kind", "sign", "jump_sign"]].copy()
    r["size_bp"] = p["size_bp"].to_numpy(float) if "size_bp" in p else np.nan
    r["price_type"] = "model"
    r["price"], r["won"], r["pnl"] = fair, won, won - fair
    r["cont_bp"] = j2.continuation_bp(sp, sign, start, sp.price(tb), twap=0)
    if closes is not None:
        r = j2.add_trend(r, closes)
    return r.reindex(columns=ROW_COLS)


# --------------------------------------------------------------------------- report helpers
def fmt_bp(s):
    if not s.get("n"):
        return "–"
    tt = f"{s['t']:+.1f}" if np.isfinite(s["t"]) else "–"
    return f"{s['mean']:+.2f} ±{s['se']:.2f} bp（t {tt}，{s['n']:,}）"


def fmt_bp_diff(d):
    if not np.isfinite(d["mean"]):
        return "–"
    tt = f"{d['t']:+.1f}" if np.isfinite(d["t"]) else "–"
    return f"{d['mean']:+.2f} ±{d['se']:.2f} bp（t {tt}）"


def kacho_main_table(rows):
    pts = [p for p, _ in ENTRY_LAGS]
    L = ["| 买法 | " + " | ".join(LOCAL_PRICE_NAMES[p] for p in pts) + " |", "|---|" + "---:|" * len(pts)]
    for k in j2.KINDS:
        L.append(f"| {j2.KIND_NAMES[k]} | " + " | ".join(j2.fmt(j2.stat(j2._sel(rows, k, p))) for p in pts) + " |")
    return L


def bin_main_table(rows):
    L = ["| 买法 | 模型优势：结算（代理）− 模型价，每份 | 结果端：买后到结算顺所买方向（bp） | 胜率 | 模型价均值 |",
         "|---|---:|---:|---:|---:|"]
    for k in BIN_KINDS:
        r = j2._sel(rows, k)
        w, pr = j2.stat(r, "won"), j2.stat(r, "price")
        win = "–" if not w["n"] else f"{100 * w['mean']:.2f}%"
        price = "–" if not pr["n"] else f"{100 * pr['mean']:.2f}¢"
        L.append(f"| {j2.KIND_NAMES[k]} | {j2.fmt(j2.stat(r))} | {fmt_bp(j2.stat(r, 'cont_bp'))} | {win} | {price} |")
    return L


def bin_block_table(rows, blocks=j2.KLINE_BLOCKS):
    L = ["| 月份 | 大跳 优势 | 大跳 结果端 | 小跳 优势 | 小跳 结果端 | 随机 优势 |", "|---|---:|---:|---:|---:|---:|"]
    b = j2.block_of(rows["t0"].to_numpy(float), blocks) if len(rows) else np.array([], dtype=object)
    for name, _, _ in blocks:
        r = rows[b == name]
        big, small, rnd = (j2._sel(r, k) for k in BIN_KINDS)
        L.append(f"| {name} | {j2.fmt(j2.stat(big))} | {fmt_bp(j2.stat(big, 'cont_bp'))} | {j2.fmt(j2.stat(small))} | "
                 f"{fmt_bp(j2.stat(small, 'cont_bp'))} | {j2.fmt(j2.stat(rnd))} |")
    return L


def h_bp_table(rows, flag, blocks, price_type=None, kind="big"):
    """Like jump2s.h_table on cont_bp (bp)."""
    r = j2._sel(rows, kind, price_type)
    f = pd.to_numeric(r[flag], errors="coerce") if len(r) else pd.Series(dtype=float)
    L = ["| 段 | 子集 | 补集 | 子集 − 补集 |", "|---|---:|---:|---:|"]
    b = j2.block_of(r["t0"].to_numpy(float), blocks) if len(r) else np.array([], dtype=object)
    for name, m in [("合计", np.ones(len(r), bool))] + [(name, b == name) for name, _, _ in blocks]:
        s, fs = r[m], f[m]
        L.append(f"| {name} | {fmt_bp(j2.stat(s[fs == 1], 'cont_bp'))} | {fmt_bp(j2.stat(s[fs == 0], 'cont_bp'))} | "
                 f"{fmt_bp_diff(j2.diff_stat(s, flag, 'cont_bp'))} |")
    return L


def cont_day_lines(rows, kind="big", price_type=None, thr=CONT_HOT_BP):
    r = j2._sel(rows, kind, price_type)
    d = j2.daily(r, "cont_bp")
    th = j2.three_day(r, "cont_bp", thr=thr)
    pos = int((d["mean"] > 0).sum()) if len(d) else 0
    share = "–" if not len(d) else f"{pos} / {len(d)}（{pos / len(d):.0%}）"
    med = "–" if not len(d) else f"{d['mean'].median():+.2f} bp"
    rate = "–" if not th["windows"] else f"{th['hits']} / {th['windows']}（{th['rate']:.0%}）"
    return [f"| 天数（≥ {j2.DAY_MIN_N} 笔） | 结果端为正的天数 | 日均值中位数 | 连续 3 天合计 ≥ +{thr:.1f} bp 的窗口 |",
            "|---:|---:|---:|---:|", f"| {len(d)} | {share} | {med} | {rate} |"]


def month_day_table(rows, blocks, kind="big", price_type=None):
    """Per block: days, share of days with mean pnl > 0, 3-day windows >= +2c (pnl) and >= +1 bp (cont)."""
    r = j2._sel(rows, kind, price_type)
    b = j2.block_of(r["t0"].to_numpy(float), blocks) if len(r) else np.array([], dtype=object)
    L = [f"| 月份 | 天数 | 优势为正的天数 | 连续 3 天优势 ≥ +2¢ | 结果端为正的天数 | 连续 3 天结果端 ≥ +{CONT_HOT_BP:.1f} bp |",
         "|---|---:|---:|---:|---:|---:|"]
    for name, _, _ in blocks:
        s = r[b == name]
        ds, dc = j2.day_summary(s), j2.daily(s, "cont_bp")
        tc = j2.three_day(s, "cont_bp", thr=CONT_HOT_BP)
        th = ds["three"]
        pc = int((dc["mean"] > 0).sum()) if len(dc) else 0
        L.append(f"| {name} | {ds['days']} | {ds['pos']} | {th['hits']} / {th['windows']} | {pc} | "
                 f"{tc['hits']} / {tc['windows']} |")
    return L


def _soft(lines):
    """decision_lines with the verdict words replaced: side evidence gets no verdict."""
    return [x.replace("**成立**", "门槛满足（旁证，不作判定）").replace("**不成立**", "门槛不满足（旁证，不作判定）")
            for x in lines]


# --------------------------------------------------------------------------- run
def run_kacho(kacho, ts, lp, sp, closes, seed=0):
    m, ticks = load_kacho(kacho)
    w = pd.DataFrame({"market": m["start"].to_numpy(np.int64), "start": m["start"].to_numpy(np.int64)})
    w["end"] = w["start"] + 300
    w = w[w["start"].isin(ticks["start"].unique())].drop_duplicates("market").sort_values("start")
    sel = kline_jumps(ts, lp, w)
    p = j2.plan(sel, w, seed=seed)
    rows, counts = kacho_rows(p, Book(ticks), sp, closes)
    info = dict(markets=len(w), days=len(set(j2.day_of(w["start"].to_numpy(float)))),
                first=j2.day_of([w["start"].min()])[0], last=j2.day_of([w["start"].max()])[0],
                big=int((sel["bucket"] == "big").sum()), small=int((sel["bucket"] == "small").sum()), counts=counts)
    return rows, info


def run_binance(ts, lp, sp, closes, first, end, seed=0):
    w = window_grid(first, end)
    sel = kline_jumps(ts, lp, w)
    p = j2.plan(sel, w, seed=seed)
    rows = binance_rows(p, sp, closes)
    info = dict(windows=len(w), big=int((sel["bucket"] == "big").sum()), small=int((sel["bucket"] == "small").sum()),
                days=len(set(j2.day_of(w["start"].to_numpy(float)))))
    return rows, info


def report(kr, kinfo, br, binfo, kl):
    """Chinese markdown report (list of lines)."""
    kb = j2._sel(kr, "big", "book")
    bb = j2._sel(br, "big")
    L = ["# 大跳后 2 秒买入：本地旁证（kacho 3–5 月盘口；币安 1 秒 K 线 3–9 月）", "",
         "设计见 JUMP2S.md（跑之前提交）。按设计，这两份数据都是**旁证，不单独判定**；下面给出数，判定门槛算出的结果只作参考。"
         "生成：`jump2s_local.py`。每份 ± 按市场聚类的标准误（t，笔数）。", "",
         "## 数据", "",
         "| 数据 | 时间段（UTC） | 市场 / 窗口 | 入选大跳（≥ 1.2 bp） | 入选小跳（1.0–1.2 bp） | 每天大跳 |",
         "|---|---|---:|---:|---:|---:|",
         f"| kacho.io 每秒盘口（BTC 5m，官方结算） | {kinfo['first']} 至 {kinfo['last']}（{kinfo['days']} 天） | "
         f"{kinfo['markets']:,} 个市场 | {kinfo['big']:,} | {kinfo['small']:,} | {kinfo['big'] / max(kinfo['days'], 1):,.0f} |",
         f"| 币安 BTCUSDT 1 秒 K 线（无 Polymarket 价格） | {KLINE_SPAN[0]} 至 {kl['last']}（{binfo['days']} 天） | "
         f"{binfo['windows']:,} 个 5 分钟窗口 | {binfo['big']:,} | {binfo['small']:,} | "
         f"{binfo['big'] / max(binfo['days'], 1):,.0f} |", "",
         f"币安 1 秒 K 线共 {kl['n']:,} 秒，缺 {kl['missing']:,} 秒（跳变只比相邻两秒，缺口不跨）。"
         "4 小时、1 小时特征用这些 1 秒 K 线拼出的 1 分钟收盘（交易所时间），3/14 起有，所以 kacho 段从 3/24 起全有。", "",
         "**口径提醒。** 按 JUMP2S.md 的写法（相邻两秒收盘 ≥ 1.2 bp，同一市场 10 秒间隔，距开盘 ≥ 15 秒、距结束 ≥ 12 秒），"
         f"每天约 {binfo['big'] / max(binfo['days'], 1):,.0f} 次大跳；用户报的是 9 月 1,519 笔 / 14 天（约 108 次/天）、"
         "09-30–10-02 547 笔（约 182 次/天）。用户的规则多半还有 JUMP2S.md 没写的筛选，所以这里的数和他们的不能一笔笔对上，"
         "只能看方向。", "",
         "跳变秒 S 的收盘在 S + 1 才知道，这里把 S + 1 当跳变时刻：开盘、结束、10 秒间隔都按 S + 1 算，"
         "跳后 2 秒 = S + 3，用 kacho 标为 S + 2 的那一行（正好是设计写的“S + 3 起才用”）。", ""]

    # ------------------------------------------------------------- kacho
    c = kinfo["counts"]
    dropped = "；".join(
        f"{LOCAL_PRICE_NAMES[pt].split('（')[0]} {j2.KIND_NAMES[k]}：计划 {c[(pt, k)][0]:,}，缺该行 {c[(pt, k)][1]:,}，"
        f"卖一不在 0.02–0.98 或不足 5 份 {c[(pt, k)][2]:,}" for pt, _ in ENTRY_LAGS for k in ("big",))
    L += ["## 一、kacho.io 每秒盘口（3/24–5/18，BTC 5m，官方结算）", "",
          "每次 1 份，按所买一方的卖一（≥ 5 份，0.02–0.98），付 taker 费 0.07·p(1 − p)，持有到官方结算。"
          "主列是设计口径（S + 2 行）；副列早 1 秒（S + 1 行），只作参考。", "",
          *kacho_main_table(kr), "",
          f"没成交的：{dropped}。", "",
          "### 按月份段（盘口价，S + 2 行）", "", *j2.block_table(kr, j2.KACHO_BLOCKS, "book"), "",
          "### 分解：价格端 vs 结果端（大跳，S + 2 行）", "",
          "中间价 2 秒变动 = 所买一方中间价从 S − 1 行到 S + 2 行的变化；模型应变动 = 无漂移模型从 S（S − 1 收盘）到 S + 3"
          "（S + 2 收盘）的变化；价格端跟上比例 = 两者之和的比。结果端 = 币安从 S + 2 收盘到市场最后一秒收盘、沿所买方向走了多少 bp。", "",
          *j2.decomp_table(kr, j2.KACHO_BLOCKS, "book"), "",
          "### H1 顺势（跳变方向与过去 4 小时同号），大跳、盘口价", "", *j2.h_table(kr, "h1", j2.KACHO_BLOCKS, "book"), "",
          "### H2 趋势型（过去 60 分钟 VR > 1），大跳、盘口价", "", *j2.h_table(kr, "h2", j2.KACHO_BLOCKS, "book"), "",
          "### 按天（大跳、盘口价，S + 2 行）", "", *j2.daily_lines(kr, "book", "big"), "",
          "同一口径下结果端（bp）按天：", "", *cont_day_lines(kr, "big", "book"), "",
          "### 按判定门槛算出的数（旁证，不作判定）", "",
          *_soft(j2.decision_lines(j2.decide_main(kb, j2.KACHO_BLOCKS), j2.decide_h(kb, "h1", j2.KACHO_BLOCKS),
                                   j2.decide_h(kb, "h2", j2.KACHO_BLOCKS), scope="kacho 3–5 月")), ""]

    # ------------------------------------------------------------- Binance only
    L += ["## 二、币安 1 秒 K 线（3/14–9/30，没有 Polymarket 价格）", "",
          "每个 UTC 5 分钟窗口当一场：开盘价 = 窗口前一秒收盘，结算 = 窗口最后一秒收盘，收盘 ≥ 开盘算涨。"
          "这是近似：真实市场按 Chainlink 结算，8/7 起还是 30 / 60 秒 TWAP。"
          "“模型优势” = 结算（代理）− 无漂移模型在 S + 3 给所买一方的概率（σ = 之前 600 个 1 秒收益的标准差），"
          "不含手续费，也不是 Polymarket 的价格：它只问“跳后 BTC 是否比无漂移模型多走同向”。"
          "随机时刻、随机方向那一行是模型自身的校准对照（应接近 0）。反方向对照这里不列（正好是大跳的相反数）。", "",
          *bin_main_table(br), "",
          "### 逐月", "", *bin_block_table(br), "",
          "### H1 顺势，大跳", "", "模型优势：", "", *j2.h_table(br, "h1", j2.KLINE_BLOCKS, "model"), "",
          "结果端（bp）：", "", *h_bp_table(br, "h1", j2.KLINE_BLOCKS), "",
          "### H2 趋势型，大跳", "", "模型优势：", "", *j2.h_table(br, "h2", j2.KLINE_BLOCKS, "model"), "",
          "结果端（bp）：", "", *h_bp_table(br, "h2", j2.KLINE_BLOCKS), "",
          "### 按天（大跳）", "", "模型优势：", "", *j2.daily_lines(br, "model", "big"), "",
          "结果端：", "", *cont_day_lines(br, "big"), "",
          f"逐月的天数（一天 ≥ {j2.DAY_MIN_N} 笔才算；3 天窗口不跨月）：", "", *month_day_table(br, j2.KLINE_BLOCKS), "",
          f"结果端的 +{CONT_HOT_BP:.1f} bp 门槛是描述用的（约等于用户 09-30–10-02 的 +1.06 bp），不在 JUMP2S.md 里。", "",
          "### 按判定门槛算出的数（旁证，不作判定；7 个月按 2/3 折算为至少 5 个月为正）", "",
          *_soft(j2.decision_lines(j2.decide_main(bb, j2.KLINE_BLOCKS, need=5),
                                   j2.decide_h(bb, "h1", j2.KLINE_BLOCKS, need=5),
                                   j2.decide_h(bb, "h2", j2.KLINE_BLOCKS, need=5), scope="币安 3–9 月（模型优势）")), ""]
    return L


def compact_csv(kr, br, path, limit=5 * 2**20):
    """Row-level kacho rows plus the Binance rows' daily aggregates (lane, day, kind, h1, h2: n, sums),
    gzipped; returns the size, or None (nothing written) when it would exceed `limit`."""
    k = kr.assign(lane="kacho", day=j2.day_of(kr["t0"].to_numpy(float)))
    b = br.assign(day=j2.day_of(br["t0"].to_numpy(float)), h1=br["h1"].fillna(-1), h2=br["h2"].fillna(-1))
    g = b.groupby(["day", "kind", "h1", "h2"]).agg(
        n=("pnl", "count"), pnl_sum=("pnl", "sum"), pnl_sq=("pnl", lambda x: float((x ** 2).sum())),
        cont_sum=("cont_bp", "sum"), cont_sq=("cont_bp", lambda x: float((x ** 2).sum())),
        won_sum=("won", "sum")).reset_index().assign(lane="binance_daily")
    out = pd.concat([k, g], ignore_index=True)
    cols = ["lane", "day"] + ROW_COLS + ["n", "pnl_sum", "pnl_sq", "cont_sum", "cont_sq", "won_sum"]
    out = out.reindex(columns=cols).round(6)
    buf = io.BytesIO()
    out.to_csv(buf, index=False, compression={"method": "gzip", "mtime": 0})
    if buf.tell() > limit:
        return None
    Path(path).write_bytes(buf.getvalue())
    return buf.tell()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kacho", required=True)
    ap.add_argument("--klines", required=True)
    ap.add_argument("--out", default="real/jump2s-local.md")
    ap.add_argument("--csv", default="real/jump2s-local.csv.gz")
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args(argv)
    t_start = time.time()
    ts, close = load_seconds(a.klines, *KLINE_SPAN)
    lp = np.log(close)
    kl = dict(n=len(ts), missing=int(ts[-1] - ts[0] + 1 - len(ts)) if len(ts) else 0,
              last=j2.day_of([ts[-1]])[0] if len(ts) else "")
    sp = j2.Seconds(ts, close)
    closes = j2.minute_closes(ts, close)
    del close
    print(f"klines {len(ts):,} s loaded, {time.time() - t_start:.0f} s", flush=True)
    kr, kinfo = run_kacho(a.kacho, ts, lp, sp, closes, a.seed)
    print(f"kacho {len(kr):,} rows, {time.time() - t_start:.0f} s", flush=True)
    br, binfo = run_binance(ts, lp, sp, closes, *KLINE_SPAN, seed=a.seed)
    print(f"binance {len(br):,} rows, {time.time() - t_start:.0f} s", flush=True)
    L = report(kr, kinfo, br, binfo, kl)
    if a.csv:
        size = compact_csv(kr, br, a.csv)
        L += [f"行数据：`{Path(a.csv).name}`（kacho 每笔一行；币安按 天 × 买法 × H1 × H2 汇总，H 缺失记 −1）。" if size
              else "行数据超过 5 MB，没有写 csv。", ""]
    L.append(f"运行时间 {time.time() - t_start:.0f} 秒。")
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
