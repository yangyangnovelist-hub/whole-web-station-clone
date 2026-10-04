"""Is kacho's "a second or two late" edge after a Binance jump there for an order matched when a real
one is? The same jumps on the GitHub forward books, at millisecond resolution (exploratory; market
data only, no orders; fixed before it was run, 2026-10-04).

kacho_late.py found on kacho.io's per-second Polymarket BTC 5m books (24 Mar - 18 May 2026,
real/kacho-late.md) that buying the side of a Binance 2-sigma one-second jump at the ask kacho
shows d seconds later, taker fee 0.07 p (1 - p), held to resolution, made +5.57c a share at d = 1,
+3.09c at d = 2, +0.10c at d = 3 and lost from d = 5 on (random times -1.96c). The user's server has
an order matched about 0.48 s after the Binance print, so a second-level edge reads like a large
one at millisecond speed. But a kacho row stamped s may hold quotes from anywhere in [s, s + 1), and
at d = 1 the row used is the jump second itself: it can show the ask from before the jump, which
Polymarket's makers reprice in a median 178 ms and which a real order, held 150 ms by Polymarket
before it is matched, cannot reach. kacho's d = 1 may then partly measure stale-quote sniping no
order can do. The GitHub forward recordings stamp every Polymarket book message with the exchange's
own time and carry Binance trades with their exchange time, so both can be measured on one set of
jumps and books.

Fixed before it was run, 2026-10-04:
- Jumps: the candidate prints of fill_rate.sends: a Binance trade whose log price moved more than
  2 sigma from the last trade at least 1 s and at most latency.C_REF_AGE s earlier (sigma from
  latency.spot_grid, up to the previous whole second), with 240..15 s left in the market window,
  in the recorded markets that have book rows; the first candidate of each market and then the
  first at least 10 s after the last one kept (kacho's "first of each 10 s"). t0 = the triggering
  trade's exchange time; side = the move's.
- Realistic execution: sent at t0 + s for s in SENDS, matched at t0 + s + M (M = 0.375 s, the real
  pilot's send -> match, which includes Polymarket's 150 ms taker hold). (a) Market order: buys at
  the side's ask at the match if it is in 0.02..0.98 and shows >= 5 shares; pnl = won - ask - fee.
  (b) Limit at the ask seen at the send + k, k in KS: sent when the ask at the send is in
  0.02..0.98, filled when the ask at the match is at most that limit and shows >= 5 shares, at the
  ask at the match (no 0.02..0.98 band on it); reported as the fill rate, per filled share and per
  order sent (unfilled = 0). A candidate is skipped (for that s) when the book feed was not running
  around the match (Book.alive with latency.C_ALIVE), the CLOB socket closed across the order
  (Book.across_close(t0 + s, match)) or the side shows no ask at the match. No book row stamped
  after the moment it is used at is used.
- kacho emulation on the same jumps and books: S = floor(t0), the jump second. For d in DS, kacho's
  row stamped S + d - 1 is used from S + d; its quote is taken as the book at S + d - 0.001 ("end",
  the freshest the row can be) and at S + d - 1 + u ("uniform", u ~ U(0, 1), one u per jump drawn
  from np.random.default_rng([0, t0 in ms]), so the draws are independent across jumps and do not
  depend on which recording finds the jump); bought there (0.02..0.98, >= 5 shares, feed running
  at that moment, no socket close across it), no fill check, as kacho_late does. For d = 1, the
  share of those rows whose side's ask had not changed since before t0 (a stale pre-jump quote)
  and the pnl of those against the rest. Next to each d, the realistic market order sent at
  max(S + d, t0 + SENDS[0]) (matched M later): a real order cannot go out before the fastest send,
  which at d = 1 moves the jumps printed in the last 110 ms of their second.
- Controls (realistic market order at each s): the opposite side at the same moments, and a
  random time (uniform in the same 240..15 s window of the same market, one per jump, drawn from
  the jump's generator above) with a random side.
- Statistics: cents a share with a standard error clustered by market (kacho_late.cl's formula),
  counts; every jump, only each market's first jump (dropped at an s where it does not fill, so
  the same jumps at every s) and, as kacho_late's "first", each market's first jump that fills at
  that s; first and second half of the recorded markets by start time; dollars a day at 5 shares
  an order and at min(20, size) for the realistic market order, days = (last recorded market's
  start - first's) / 86400.

Changed after a code review, 2026-10-04, before it was run on any forward recording: the draws per
jump (they restarted at seed 0 in every recording, so the k-th jump of each recording got the same
u, random time and side); the realistic S + d order not sent before t0 + SENDS[0]; jumps counted
and searched only in markets with book rows; the strict first-jump column added; limit orders with
no ask at the match skipped and their fill not held to the market order's 0.02..0.98 band.

    python late_ms.py ROOT [--out real/late-ms.md]
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

import numpy as np
import pandas as pd

import binary as bo
import latency as lt

SENDS = (0.11, 0.3, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0)
M = 0.375           # send -> match of the real pilot, including the 150 ms taker hold
KS = (0.0, 0.02, 0.05)
DS = (1, 2, 3, 5)
Z0, SPACING, MIN_SIZE = 2.0, 10.0, 5.0
# kacho.io, 24 Mar - 18 May 2026, side of the jump, every jump (real/kacho-late.md)
KACHO_IO = {1: "+5.57¢ ±0.11", 2: "+3.09¢ ±0.12", 3: "+0.10¢ ±0.12", 5: "-1.20¢ ±0.12"}
OPP = {"Up": "Down", "Down": "Up"}
COLS = ["market_id", "start_ts", "t", "arm", "variant", "x", "side", "won", "t_ref", "ask_send", "ask", "size",
        "stale", "first"]
KEYS = ["coin", "market_id", "t", "arm", "variant", "x"]


def jumps(markets, spot_trades, sigma, z0=Z0, tau_lo=None, spacing=SPACING):
    """The jumps of each market: fill_rate.sends' candidate prints (moved more than z0 sigma from
    the last print 1..C_REF_AGE s earlier, 240..tau_lo s left), the first of each `spacing` s."""
    tau_lo = lt.G_TAU_LO if tau_lo is None else tau_lo
    ts = spot_trades["trade_ts"].to_numpy(dtype=float)
    lp = np.log(spot_trades["price"].to_numpy(dtype=float))
    rows = []
    for m in markets.itertuples():
        end = m.start_ts + bo.WINDOW_S
        a, b = np.searchsorted(ts, [end - lt.TAUS[0], end - tau_lo])
        if b <= a:
            continue
        j = np.searchsorted(ts, ts[a:b] - 1.0, "right") - 1
        ok = (j >= 0) & (ts[a:b] - ts[np.maximum(j, 0)] <= lt.C_REF_AGE)
        dx = np.where(ok, lp[a:b] - lp[np.maximum(j, 0)], np.nan)
        sg = sigma.reindex(np.floor(ts[a:b]).astype("int64") - 1).to_numpy()
        with np.errstate(invalid="ignore"):
            cand = np.flatnonzero(np.abs(dx) > z0 * sg)
        next_t = -np.inf
        for i in cand:
            t0 = float(ts[a + i])
            if t0 < next_t:
                continue
            next_t = t0 + spacing
            rows.append((m.market_id, int(m.start_ts), t0, "Up" if dx[i] > 0 else "Down", m.winner, float(dx[i])))
    return pd.DataFrame(rows, columns=["market_id", "start_ts", "t0", "side", "winner", "dx"])


def _side(r, side):
    """(ask, size) of `side` from an Up book row (bid, ask, bid_size, ask_size): Down's ask is
    1 - the Up bid, with the Up bid's size."""
    if r is None:
        return np.nan, np.nan
    return (float(r[1]), float(r[3])) if side == "Up" else (1.0 - float(r[0]), float(r[2]))


def _order(book, mid, t_send):
    """The Up book rows shown at the send and at the match (t_send + M) of an order sent at t_send;
    None when the feed was not running around the match or the CLOB socket closed across it."""
    t_match = t_send + M
    if not book.alive(mid, t_match, *lt.C_ALIVE) or book.across_close(mid, t_send, t_match):
        return None
    return book.at(mid, t_send), book.at(mid, t_match)


def stale(book, mid, t0, q, side):
    """Whether the side's ask shown at q is still the one shown before t0: no book row stamped in
    [t0, q] changed it (True when q < t0; False when nothing was shown before t0)."""
    ts, v = book.by[mid]
    i0 = int(np.searchsorted(ts, t0, "left")) - 1
    iq = int(np.searchsorted(ts, q, "right")) - 1
    if i0 < 0:
        return False
    if iq <= i0:
        return True
    a = v[i0:iq + 1, 1] if side == "Up" else 1.0 - v[i0:iq + 1, 0]
    return bool(np.all(a == a[0]))


def _rng(seed, t0):
    """The generator of one jump, seeded by its own time (ms): independent draws across jumps,
    the same whichever recording (or call) finds the jump."""
    return np.random.default_rng([int(seed), int(round(1000 * float(t0)))])


def entries(jumps_, book, sends=SENDS, ds=DS, seed=0, tau_lo=None):
    """One row per jump and execution: realistic orders (variant "ms", x = s; arms "with",
    "against" and "random"), the kacho emulations ("end", "uniform"; x = d) and the realistic
    market order sent at max(S + d, t0 + SENDS[0]) ("ms_sd"). ask_send / ask / size: the side's
    ask at the send and at the match (the emulated row's quote for end / uniform); t_ref: the send
    (the emulated moment for end / uniform); first: the market's first jump in jumps_; ok: a
    market order fills there."""
    tau_lo = lt.G_TAU_LO if tau_lo is None else tau_lo
    first_t0 = jumps_.groupby("market_id")["t0"].transform("min").to_numpy(float) if len(jumps_) else []
    rows = []
    for j, f0 in zip(jumps_.itertuples(), first_t0):
        mid, t0, side = j.market_id, float(j.t0), j.side
        end = j.start_ts + bo.WINDOW_S
        rng = _rng(seed, t0)
        u = float(rng.random())
        tr = float(rng.uniform(end - lt.TAUS[0], end - tau_lo))
        rside = "Up" if rng.random() < 0.5 else "Down"
        if mid not in book.by:
            continue

        def add(arm, variant, x, sd, t_ref, a_send, a, z, st=False):
            rows.append((mid, int(j.start_ts), t0, arm, variant, float(x), sd, float(j.winner == sd), t_ref,
                         a_send, a, z, st, t0 == f0))

        for s in sends:
            o = _order(book, mid, t0 + s)
            if o is not None:
                for arm, sd in (("with", side), ("against", OPP[side])):
                    add(arm, "ms", s, sd, t0 + s, _side(o[0], sd)[0], *_side(o[1], sd))
            o = _order(book, mid, tr + s)
            if o is not None:
                add("random", "ms", s, rside, tr + s, _side(o[0], rside)[0], *_side(o[1], rside))
        S = math.floor(t0)
        for d in ds:
            for variant, q in (("end", S + d - 0.001), ("uniform", S + d - 1 + u)):
                if not book.alive(mid, q, *lt.C_ALIVE) or book.across_close(mid, q, q):
                    continue
                add("with", variant, d, side, q, np.nan, *_side(book.at(mid, q), side), stale(book, mid, t0, q, side))
            t_sd = max(S + d, t0 + SENDS[0])  # never before the fastest send a real server has
            o = _order(book, mid, t_sd)
            if o is not None:
                add("with", "ms_sd", d, side, t_sd, _side(o[0], side)[0], *_side(o[1], side))
    t = pd.DataFrame(rows, columns=COLS)
    t["stale"] = t["stale"].astype(bool)
    t["first"] = t["first"].astype(bool)
    a = t["ask"].to_numpy(dtype=float)
    with np.errstate(invalid="ignore"):
        t["ok"] = np.isfinite(a) & (a >= 0.02) & (a <= 0.98) & (t["size"].to_numpy(dtype=float) >= MIN_SIZE)
    t["pnl"] = np.where(t["ok"], t["won"] - a - bo.taker_fee(a), np.nan)
    return t


def limit(t, k):
    """Limit orders at the ask seen at the send + k on the realistic rows of the side of the jump:
    sent when that ask is in 0.02..0.98, skipped when the side shows no ask at the match; filled
    when the ask at the match is at most the limit and shows >= 5 shares, at that ask; `pnl` per
    order sent (0 when unfilled)."""
    s = t[(t["variant"] == "ms") & (t["arm"] == "with")]
    a0, a1 = s["ask_send"].to_numpy(dtype=float), s["ask"].to_numpy(dtype=float)
    with np.errstate(invalid="ignore"):
        s = s[np.isfinite(a0) & (a0 >= 0.02) & (a0 <= 0.98) & np.isfinite(a1)].copy()
    a0, a1 = s["ask_send"].to_numpy(dtype=float), s["ask"].to_numpy(dtype=float)
    fill = (s["size"].to_numpy(dtype=float) >= MIN_SIZE) & (a1 <= a0 + k + 1e-9)
    s["fill"] = fill
    s["pnl"] = np.where(fill, s["won"].to_numpy(dtype=float) - a1 - bo.taker_fee(a1), 0.0)
    return s


def run(root, notes=None):
    """All rows of every recording under ROOT (latency._per_recording, test G's coins, feed and
    start), the recorded markets with book rows and the number of jumps found in them; `first`
    marks each market's first jump over all recordings."""
    notes = [] if notes is None else notes
    seen_markets, seen_jumps = [], []

    def make(mk, sp, sg, bk):
        mk = mk[mk["market_id"].isin(set(bk.by))]  # a market with no book row has nothing to trade
        seen_markets.append(mk[["market_id", "start_ts"]])
        jp = jumps(mk, sp, sg)
        seen_jumps.append(jp[["market_id", "t0"]])
        return entries(jp, bk)

    dirs = lt._latency_dirs([root], lt.G_COINS)
    t = lt._per_recording(dirs, lt.G_SINCE, lt.G_SPOT, make, KEYS, notes)
    if "variant" not in t:
        t = pd.DataFrame(columns=COLS + ["ok", "pnl", "coin", "run"])
    t["ok"] = t["ok"].astype(bool)
    t["stale"] = t["stale"].astype(bool)
    for c in ("x", "won", "ask_send", "ask", "size", "pnl", "t", "start_ts"):
        t[c] = t[c].astype(float)
    markets = pd.concat(seen_markets).drop_duplicates("market_id") if seen_markets else \
        pd.DataFrame(columns=["market_id", "start_ts"])
    jp = pd.concat(seen_jumps).drop_duplicates() if seen_jumps else pd.DataFrame(columns=["market_id", "t0"])
    # a market recorded twice: its first jump over all recordings, not each recording's first
    f0 = jp.groupby("market_id")["t0"].min()
    t["first"] = (t["t"].to_numpy(float) == t["market_id"].map(f0).to_numpy(float)) if len(t) else False
    t["first"] = t["first"].astype(bool)
    return t, markets, len(jp), notes


def cl(t, col="pnl"):
    """Mean of `col` per row with a standard error clustered by market (kacho_late.cl)."""
    if not len(t):
        return np.nan, np.nan, 0
    key = ["coin", "market_id"] if "coin" in t else ["market_id"]
    g = t.groupby(key)[col].agg(["sum", "count"])
    m = g["sum"].sum() / g["count"].sum()
    r = g["sum"] - m * g["count"]
    k = len(g)
    return m, np.sqrt((r ** 2).sum() * k / max(k - 1, 1)) / g["count"].sum(), len(t)


def fmt(t, col="pnl"):
    m, se, n = cl(t, col)
    return f"{100 * m:+.2f}¢ ±{100 * se:.2f}（{n:,}）" if n else "–"


def _first(t):
    """kacho_late's "first": per market, the earliest of the rows given (with a fill at that s,
    so a market whose first jump does not fill there contributes its next one)."""
    key = ["coin", "market_id"] if "coin" in t else ["market_id"]
    return t.sort_values("t", kind="stable").groupby(key).head(1)


def _utc(x):
    return pd.to_datetime(x, unit="s", utc=True).strftime("%m-%d %H:%M")


def report(t, markets, n_jumps, notes, sends=SENDS, ks=KS, ds=DS):
    ok = t[t["ok"]]

    def sel(variant, arm, x, frame=ok):
        return frame[(frame["variant"] == variant) & (frame["arm"] == arm) & np.isclose(frame["x"].astype(float), x)]

    st = markets["start_ts"].astype(float).sort_values().to_numpy()
    days = (st[-1] - st[0]) / 86400 if len(st) else 0.0
    span = f"{_utc(st[0])} – {_utc(st[-1])} UTC" if len(st) else "无"
    L = ["# 毫秒级下单能拿到 kacho 每秒盘口上“晚 1–2 秒”的钱吗（GitHub 前向录制，探索性，事先写定）", "",
         f"录制：市场开始于 {span}（约 {days:.1f} 天），{len(markets):,} 个有盘口的 BTC 5m 市场；"
         f"币安 2σ 一秒急动 {n_jumps:,} 次（逐笔成交触发，剩 240–15 秒，每个市场每 10 秒取第一次）。", "",
         f"真实执行：触发成交（币安交易所时间）后 s 秒发单，再过 {1000 * M:.0f} ms 撮合（试点实测发单→撮合，含 150 ms 冻结），"
         "按撮合时刻交易所显示的卖一买（0.02–0.98、至少 5 份），付 taker 费，持有到结算。"
         "kacho 模拟：同一批急动和盘口，按 kacho 的做法用急动秒 S 之后第 d 秒的“行”，行内报价取该行最晚可能的状态（行末）"
         "或该秒内随机时刻的状态（行内随机），不管能否成交。每份 ± 按市场聚类的标准误（笔数）。", "",
         "## 表 1 真实执行，市价单（按撮合时卖一）", "",
         "| 发单 s 秒（撮合 s + 0.375） | 顺急动方向 | 只买每个市场第一次急动 | 每个市场第一次能成交的急动 | 反方向 | "
         "随机时刻、随机方向 | 每天美元（每张 5 份） | 每天美元（每张 min(20, 挂单量)） |",
         "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for s in sends:
        w = sel("ms", "with", s)
        if days > 0 and len(w):
            d5 = f"{5 * w['pnl'].sum() / days:+,.0f}"
            d20 = f"{(w['pnl'] * np.minimum(w['size'], 20)).sum() / days:+,.0f}"
        else:
            d5 = d20 = "–"
        L.append(f"| {s:g} | {fmt(w)} | {fmt(w[w['first']])} | {fmt(_first(w))} | {fmt(sel('ms', 'against', s))} | "
                 f"{fmt(sel('ms', 'random', s))} | {d5} | {d20} |")
    L += ["", "## 表 2 真实执行，限价单（限价 = 发单时看到的卖一 + k；撮合时卖一不高于限价且至少 5 份才成交，按撮合时卖一成交）", "",
          "每张发单的每份把没成交的算 0。", "",
          "| 发单 s 秒 | " + " | ".join(f"k = {100 * k:g}¢ 成交率 | 成交的每份 | 每张发单" for k in ks) + " |",
          "|---|" + "---:|" * (3 * len(ks))]
    lims = {k: limit(t, k) for k in ks}
    for s in sends:
        cells = []
        for k in ks:
            lo = lims[k][np.isclose(lims[k]["x"].astype(float), s)]
            if not len(lo):
                cells += ["–", "–", "–"]
                continue
            f = lo[lo["fill"]]
            m_f = cl(f)[0]
            cells += [f"{lo['fill'].mean():.0%}", f"{100 * m_f:+.2f}¢" if len(f) else "–", fmt(lo)]
        L.append(f"| {s:g} | " + " | ".join(cells) + " |")
    sd1 = sel("ms_sd", "with", 1, t)
    moved = f"{(sd1['t_ref'] > np.floor(sd1['t']) + 1 + 1e-9).mean():.0%}" if len(sd1) else "–"
    L += ["", "## 表 3 kacho 的做法放到同一批急动上（顺急动方向）", "",
          "S = 急动所在的整秒；kacho 在第 S + d 秒用的是标为 S + d − 1 的那一行。",
          "行末：第 S + d − 0.001 秒的盘口；行内随机：第 S + d − 1 + u 秒（u 均匀，每次急动一个）；"
          f"真实：第 S + d 秒发单（但不早于触发成交后 {SENDS[0]:g} 秒，最快的真实发单），发单后 0.375 秒按卖一撮合；"
          f"d = 1 时有 {moved} 的急动落在该秒最后 {1000 * SENDS[0]:.0f} ms，改在触发后 {SENDS[0]:g} 秒发单。", "",
          f"| 晚 d 秒 | kacho.io 实测（3/24–5/18） | 模拟：行末状态 | 模拟：行内随机时刻 | 真实执行（S + d 发单，不早于触发后 {SENDS[0]:g} 秒） |",
          "|---|---:|---:|---:|---:|"]
    for d in ds:
        L.append(f"| {d} | {KACHO_IO.get(d, '–')} | {fmt(sel('end', 'with', d))} | {fmt(sel('uniform', 'with', d))} | "
                 f"{fmt(sel('ms_sd', 'with', d))} |")
    L += ["", "d = 1 的模拟行里，卖一还是急动前那个（触发成交之后这一方的卖一没变过）的有多少，各赚多少：", "",
          "| d = 1 的模拟 | 行数 | 急动前的旧卖一占比 | 旧卖一的每份 | 其余的每份 |", "|---|---:|---:|---:|---:|"]
    for variant, name in (("end", "行末状态"), ("uniform", "行内随机时刻")):
        r = sel(variant, "with", 1)
        share = f"{r['stale'].mean():.0%}" if len(r) else "–"
        L.append(f"| {name} | {len(r):,} | {share} | {fmt(r[r['stale']])} | {fmt(r[~r['stale']])} |")
    L += ["", "## 表 4 前后两半（按市场开始时间把录到的市场分成两半）", ""]
    if len(st) >= 2:
        cut = st[len(st) // 2]
        h1, h2 = ok[ok["start_ts"] < cut], ok[ok["start_ts"] >= cut]
        L += [f"| 执行（顺急动方向） | 前半（{_utc(st[0])} – {_utc(st[len(st) // 2 - 1])}） | "
              f"后半（{_utc(cut)} – {_utc(st[-1])}） |", "|---|---:|---:|"]
        rows = [(f"真实市价单 s = {s:g}", "ms", s) for s in sends]
        rows += [(f"kacho 模拟·行末 d = {d}", "end", d) for d in ds]
        rows += [(f"kacho 模拟·行内随机 d = {d}", "uniform", d) for d in ds]
        rows += [(f"真实 S + {d} 发单", "ms_sd", d) for d in ds]
        for name, variant, x in rows:
            L.append(f"| {name} | {fmt(sel(variant, 'with', x, h1))} | {fmt(sel(variant, 'with', x, h2))} |")
    else:
        L.append("市场太少，不分。")
    L += ["", "## 备注", "",
          "- 只用行情，没有下单。盘口行只在其交易所时间戳不晚于使用时刻时才用；“盘口在跑”（撮合前 30 秒、后 10 秒内有更新）"
          "和“撮合前后 CLOB 连接没断”只用来剔除录制故障。",
          "- 限价单只在发单时卖一在 0.02–0.98 时才发；撮合时这一方没有卖一的不算（跳过）；成交价不受 0.02–0.98 限制。"
          "随机对照每次急动配一个随机时刻和随机方向（与行内随机的 u 一样，按急动时刻各自取随机数）。",
          "- 表 1“只买每个市场第一次急动”：每个市场只看它的第一次急动，某个 s 下没成交就不算，不拿第二次顶替（各 s 是同一批急动）；"
          "“第一次能成交的急动”是 kacho-late.md 的算法：某个 s 下每个市场第一笔能成交的急动。",
          "- 急动只在有盘口的市场里找和计数。",
          "- 下面 CLOB 断线剔除的条数是所有 s、方向和模拟方式加在一起的。"]
    L += [f"- {n}" for n in notes]
    return "\n".join(L) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root")
    ap.add_argument("--out", default="real/late-ms.md")
    a = ap.parse_args(argv)
    t, markets, n_jumps, notes = run(a.root)
    text = report(t, markets, n_jumps, notes)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
