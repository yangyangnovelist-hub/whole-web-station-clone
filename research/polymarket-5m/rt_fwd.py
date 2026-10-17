"""ROUNDTRIP.md, the forward test written down after the Hugging Face check ("通过之后的前向检验",
fixed 2026-10-04 17:20 UTC, commit 4b9447b at 17:13, before any October data was looked at): the two
surviving round-trip variants that trade most often, both held to settlement, on the GitHub forward
recordings' millisecond BTC 5m books. Paper research on market data only, no orders.

    python rt_fwd.py ROOT [--out real/rt-fwd.md]

Rules (Bonferroni k = 2)
- R1 `w1-bp4-all-all10-settle`: a Binance BTCUSDT print whose log price moved >= 4 bp from the last
  print received at least 1 s earlier; R2 `w3-bp8-all-all10-settle`: >= 8 bp from the last print
  received at least 3 s earlier. Buy the side of the move, hold to the official outcome.
- Binance prints are used by the recorder's RECEIPT time (`receive_ts` of the binancews feed,
  binance_trades.jsonl.gz, as latency.load returns it), never by their exchange time: the prints
  are put in receipt order (ties by exchange time), the trigger time t is the triggering print's
  receipt, and its reference is the last print received at or before t - w (w = 1 for R1, 3 for
  R2), which must have been received at most latency.C_REF_AGE (5) s before t, so a feed gap
  cannot pass for a move. "bp" is the log-price difference x 1e4 (as on kacho and Hugging Face).
- Window: a trigger must be received inside the market's window (start <= t < end; a print
  received before the start is not the market's) and its decision must leave MORE than 15 s
  (end - D > 15; "剩 15 秒以内不再开仓"), i.e. decisions 1..284 s into the market.
- Spacing ("all10"), per rule and market: the first trigger, then the next one received at least
  10 s after the previous KEPT trigger (whether or not that one filled, as roundtrip.thin).
- One position: ROUNDTRIP.md 执行 "同一变体在同一市场同时最多一笔仓位"; a settle variant holds its
  position to the end, so each market has at most one trade per rule: the kept triggers are tried in
  time order and the first whose fill passes is the trade (on kacho and on Hugging Face these
  variants had exactly one trade per market: 2,575 / 2,575 and 1,072 / 1,072). Every kept trigger
  that fills is also reported, as a description only ("不限一笔仓位"), never judged.

Execution (conservative where ROUNDTRIP.md is silent; written before any run)
- Decision at the first whole second strictly after t: D = floor(t) + 1 (a print received exactly on
  a whole second decides one second later). Fill at F = D + 0.5 at the side's ask in the last book
  row stamped at or before F (latency.Book.at; Down's ask is 1 - the Up bid, with the Up bid's
  size). The fill needs: ask in 0.02..0.98 and >= 5 shares there; the book "changed within 1 s":
  the last row stamped at or before F whose best bid or best ask PRICE differs from the row before
  it is stamped at most 1 s before F (size-only changes and repeated rows do not count, a market's
  first row in a recording does not count, missing = missing); the feed alive around F
  (Book.alive with latency.C_ALIVE) and no CLOB socket close across the order (Book.across_close
  from D to F). No price, size or change is read from a row stamped after F; alive and
  across_close look past F only to reject recording faults, as in late_ms / rt_ms.
- Fee binary.taker_fee (0.07 p (1 - p)) on the entry; pnl a share = (winner == side) - ask - fee.
- Times: the recorder's clock (Binance receipt) and Polymarket's book stamps are taken as one clock.

Segments (by market start, UTC)
- Independent check: markets starting 2026-10-01 02:30 .. 2026-10-04 17:20 (both ends included;
  recorded, never used in selection). Reported only.
- Forward: markets starting strictly after 2026-10-04 17:20 (the first is 17:25). Per rule, judged
  ONCE when it has FWD_N = 300 trades: the first 300 by trigger time pass if the mean a share is
  > 0, the one-sided normal p from the standard error clustered by market (CR1, as lt.clustered)
  is < 0.025, and both halves by trigger time (first 150, last 150) have a positive mean. The
  verdict is pinned as one line per rule in <out>.verdict.md (real/rt-fwd.verdict.md) and that
  rule's 300 trades appended to <out>.trades.csv; a pinned line is only ever read back (like
  latency._judge), the other rule's line is appended when it reaches 300.

Controls (same filters, same one-position rule): the opposite side at the same trigger moments
(the first kept trigger whose opposite fill passes); a random time with a random side: for each kept
trigger a decision second uniform on 1..284 s into the same market and a side, drawn from the
trigger's own generator (seeded by the rule and the trigger time in ms, so the draws do not depend
on how recordings are split), the first that fills per market.

Data: every recording processed on its own (latency._per_recording, test G's coin, feed and start);
a market seen in more than one keeps its earliest trade per rule and arm. Markets without an official
outcome in their recording are left out (latency.load). Days = markets with book rows in the segment
x 300 s / 86,400 s (the recorder covers only part of each day). $/day at 5 shares a trade and at
min(20, the ask's size) shares.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

import numpy as np
import pandas as pd

import binary as bo
import latency as lt

RULES = {"R1": ("w1-bp4-all-all10-settle", 1.0, 4.0), "R2": ("w3-bp8-all-all10-settle", 3.0, 8.0)}
SPACING = 10.0
LAST_OPEN = 15          # no new position with 15 s or less left
FILL_LAT = 0.5          # decision -> fill
CHANGE_MAX = 1.0        # the fill snapshot's top of book changed at most this long before the fill
MIN_SIZE, P_LO, P_HI, CAP = 5.0, 0.02, 0.98, 20.0
IND_FROM, FWD_AFTER = "2026-10-01 02:30", "2026-10-04 17:20"
FWD_N, ALPHA = 300, 0.025
D_MAX = bo.WINDOW_S - LAST_OPEN - 1  # last decision second into the market (284)
ARMS = {"rule": "规则（顺变动方向，每个市场最多一笔）", "opposite": "对照：反方向（同一触发时刻）",
        "random": "对照：随机时刻、随机方向", "every": "描述：每次触发都买（不限一笔仓位，不判定）"}
OPP = {"Up": "Down", "Down": "Up"}
COLS = ["market_id", "start_ts", "rule", "arm", "t", "decision", "fill_t", "side", "winner", "ask", "size", "won",
        "fee", "pnl"]
KEYS = ["coin", "market_id", "rule", "arm", "t"]  # lt._per_recording sorts on "t"
SEG_NAMES = {"ind": "独立检验", "fwd": "前向"}


def _ts(s):
    return pd.Timestamp(s, tz="UTC").timestamp()


def segment(start_ts):
    """'ind' for markets starting IND_FROM .. FWD_AFTER (both included), 'fwd' after FWD_AFTER, else None."""
    s = float(start_ts)
    if s > _ts(FWD_AFTER):
        return "fwd"
    return "ind" if s >= _ts(IND_FROM) else None


def receipt_order(spot):
    """Receipt times and log prices of the Binance prints in the order the recorder received them
    (ties by exchange time)."""
    r = spot["receive_ts"].to_numpy(dtype=float)
    tr = spot["trade_ts"].to_numpy(dtype=float)
    p = spot["price"].to_numpy(dtype=float)
    ok = np.isfinite(r) & np.isfinite(tr) & np.isfinite(p) & (p > 0)
    r, tr, p = r[ok], tr[ok], p[ok]
    o = np.lexsort((tr, r))
    return r[o], np.log(p[o])


def triggers(markets, rr, lp, w, bp, spacing=SPACING):
    """Kept triggers of one rule: (market_id, start_ts, winner, t, side, dx, t_ref), t the receipt of
    the triggering print; per market the first, then each at least `spacing` s after the last kept."""
    rows = []
    for m in markets.itertuples():
        start = float(m.start_ts)
        end = start + bo.WINDOW_S
        a, b = np.searchsorted(rr, [start, end], "left")
        if b <= a:
            continue
        r = rr[a:b]
        j = np.searchsorted(rr, r - w, "right") - 1
        jj = np.maximum(j, 0)
        ok = (j >= 0) & (r - rr[jj] <= lt.C_REF_AGE)
        dx = np.where(ok, lp[a:b] - lp[jj], np.nan)
        dec = np.floor(r) + 1.0
        with np.errstate(invalid="ignore"):
            hit = ok & (np.abs(dx) >= bp * 1e-4) & (end - dec > LAST_OPEN)
        nxt = -np.inf
        for i in np.flatnonzero(hit):
            t = float(r[i])
            if t < nxt:
                continue
            nxt = t + spacing
            rows.append((m.market_id, int(m.start_ts), m.winner, t, "Up" if dx[i] > 0 else "Down", float(dx[i]),
                         float(rr[jj[i]])))
    return pd.DataFrame(rows, columns=["market_id", "start_ts", "winner", "t", "side", "dx", "t_ref"])


def _change_times(book, mid):
    """Stamps of the rows of `mid` whose best bid or best ask differs from the row before (cached)."""
    cache = book.__dict__.setdefault("_rt_fwd_changes", {})
    if mid not in cache:
        ts, v = book.by[mid]
        px = np.asarray(v[:, :2], dtype=float)
        same = (px[1:] == px[:-1]) | (np.isnan(px[1:]) & np.isnan(px[:-1]))
        cache[mid] = ts[1:][~same.all(axis=1)]
    return cache[mid]


def changed_within(book, mid, t, limit=CHANGE_MAX):
    """Whether the top of book shown at t changed (bid or ask price) at most `limit` s before t,
    reading only rows stamped at or before t."""
    if mid not in book.by:
        return False
    ch = _change_times(book, mid)
    k = int(np.searchsorted(ch, t, "right")) - 1
    return k >= 0 and t - float(ch[k]) <= limit


def side_ask(row, side):
    """(ask, size) of `side` from an Up book row (bid, ask, bid_size, ask_size)."""
    if row is None:
        return np.nan, np.nan
    return (float(row[1]), float(row[3])) if side == "Up" else (1.0 - float(row[0]), float(row[2]))


def fill(book, mid, decision, side):
    """(ask, size) bought at decision + FILL_LAT, or None when the fill fails a filter."""
    f = decision + FILL_LAT
    if mid not in book.by or not book.alive(mid, f, *lt.C_ALIVE) or book.across_close(mid, decision, f):
        return None
    ask, size = side_ask(book.at(mid, f), side)
    if not (np.isfinite(ask) and P_LO <= ask <= P_HI and np.isfinite(size) and size >= MIN_SIZE):
        return None
    if not changed_within(book, mid, f):
        return None
    return ask, size


def _rng(rule, t):
    return np.random.default_rng([list(RULES).index(rule), int(round(1000 * float(t)))])


def trades(trig, book, rule):
    """Rows (COLS) of one rule's kept triggers: arms rule / opposite / random (first fill per market)
    and every (each kept trigger that fills)."""
    rows = []
    done = set()
    for j in trig.sort_values(["market_id", "t"], kind="stable").itertuples():
        mid, start, t = j.market_id, float(j.start_ts), float(j.t)
        dec = math.floor(t) + 1.0
        g = _rng(rule, t)
        d_r = float(start + g.integers(1, D_MAX + 1))
        s_r = "Up" if g.random() < 0.5 else "Down"
        for arm, d, sd in (("rule", dec, j.side), ("opposite", dec, OPP[j.side]), ("random", d_r, s_r)):
            if arm != "rule" and (mid, arm) in done:
                continue
            f = fill(book, mid, d, sd)
            if f is None:
                continue
            ask, size = f
            won = float(j.winner == sd)
            fee = float(bo.taker_fee(ask))
            row = (mid, int(start), rule, arm, t, d, d + FILL_LAT, sd, j.winner, ask, size, won, fee, won - ask - fee)
            if arm == "rule":
                rows.append(row[:3] + ("every",) + row[4:])
                if (mid, arm) in done:
                    continue
            done.add((mid, arm))
            rows.append(row)
    return pd.DataFrame(rows, columns=COLS)


def run(root, notes=None):
    """All trades of every recording under ROOT, the recorded markets with book rows (with segment)
    and the kept triggers (rule, market, t, filled)."""
    notes = [] if notes is None else notes
    seen, trig_all = [], []

    def make(mk, sp, sg, bk):
        mk = mk[mk["market_id"].isin(set(bk.by))]
        seen.append(mk[["market_id", "start_ts"]])
        rr, lp = receipt_order(sp)
        parts = []
        for rule, (_, w, bp) in RULES.items():
            tg = triggers(mk, rr, lp, w, bp)
            tr = trades(tg, bk, rule)
            filled = set(tr.loc[tr["arm"] == "every", "t"])
            trig_all.append(tg.assign(rule=rule, filled=tg["t"].isin(filled))[["market_id", "start_ts", "rule", "t",
                                                                                "filled"]])
            parts.append(tr)
        return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=COLS)

    dirs = lt._latency_dirs([root], lt.G_COINS)
    t = lt._per_recording(dirs, IND_FROM, lt.G_SPOT, make, KEYS, notes)
    if "arm" not in t:
        t = pd.DataFrame(columns=COLS + ["coin", "run"])
    for c in ("start_ts", "t", "decision", "fill_t", "ask", "size", "won", "fee", "pnl"):
        t[c] = t[c].astype(float)
    # one position per market, rule and arm over all recordings: the earliest trade
    one = t["arm"] != "every"
    t = pd.concat([t[one].sort_values("t", kind="stable").drop_duplicates(["coin", "market_id", "rule", "arm"]),
                   t[~one]]).sort_values("t", kind="stable").reset_index(drop=True)
    t["segment"] = [segment(s) for s in t["start_ts"]]
    t = t[t["segment"].notna()].reset_index(drop=True)
    markets = pd.concat(seen).drop_duplicates("market_id") if seen else pd.DataFrame(columns=["market_id", "start_ts"])
    markets = markets.assign(segment=[segment(s) for s in markets["start_ts"]])
    trig = pd.concat(trig_all).drop_duplicates(["market_id", "rule", "t"]) if trig_all else \
        pd.DataFrame(columns=["market_id", "start_ts", "rule", "t", "filled"])
    trig = trig.assign(segment=[segment(s) for s in trig["start_ts"]])
    return t, markets, trig, notes


def cluster(t):
    """Mean pnl a share, its standard error clustered by market (CR1), t, one-sided normal p for a
    positive mean, trades and markets."""
    if not len(t):
        return np.nan, np.nan, np.nan, np.nan, 0, 0
    key = ["coin", "market_id"] if "coin" in t else ["market_id"]
    g = t.groupby(key)["pnl"].agg(["sum", "count"])
    n, k = int(g["count"].sum()), len(g)
    mu = g["sum"].sum() / n
    se = math.sqrt(((g["sum"] - mu * g["count"]) ** 2).sum() * k / max(k - 1, 1)) / n
    z = mu / se if se > 0 else np.nan
    p = 0.5 * math.erfc(z / math.sqrt(2)) if np.isfinite(z) else np.nan
    return mu, se, z, p, n, k


def _cents(mu, se, z):
    tt = f"t {z:+.1f}" if np.isfinite(z) else "t –"
    return f"{100 * mu:+.2f}¢ ±{100 * se:.2f}（{tt}）"


def row_stats(t, days):
    """One table row: trades | markets | trades/day | ¢ a share ± SE (t) | win rate | average price |
    $/day at 5 shares | $/day at min(20, size)."""
    mu, se, z, _, n, k = cluster(t)
    if not n:
        return "– | – | – | – | – | – | – | –"
    per = f"{n / days:,.1f}" if days > 0 else "–"
    d5 = f"{5 * t['pnl'].sum() / days:+,.2f}" if days > 0 else "–"
    d20 = f"{(t['pnl'] * np.minimum(t['size'], CAP)).sum() / days:+,.2f}" if days > 0 else "–"
    return (f"{n:,} | {k:,} | {per} | {_cents(mu, se, z)} | {t['won'].mean():.1%} | {t['ask'].mean():.3f} | "
            f"{d5} | {d20}")


def _utc(x, f="%Y-%m-%d %H:%M"):
    return pd.to_datetime(float(x), unit="s", utc=True).strftime(f)


def verdict_line(rule, first):
    """The verdict of one rule on its first FWD_N forward trades (by trigger time)."""
    name = RULES[rule][0]
    mu, se, z, p, n, k = cluster(first)
    h = len(first) // 2
    m1, m2 = cluster(first.iloc[:h])[0], cluster(first.iloc[h:])[0]
    ok = bool(mu > 0 and np.isfinite(p) and p < ALPHA and m1 > 0 and m2 > 0)
    runs = sorted(first["run"].astype(str).unique()) if "run" in first else []
    pstr = f"{p:.1e}" if np.isfinite(p) else "–"
    return (f"- {rule} `{name}`（前向段前 {n:,} 笔，{k:,} 个市场）：{'**通过**' if ok else '**不通过**'}。"
            f"每份 {_cents(mu, se, z)}，单侧 p = {pstr}；前 {h:,} 笔 {100 * m1:+.2f}¢、后 {n - h:,} 笔 {100 * m2:+.2f}¢；"
            f"胜率 {first['won'].mean():.1%}，平均价 {first['ask'].mean():.3f}。"
            f"条件（事先写死）：每份 > 0、按市场聚类单侧 p < {ALPHA}、前后两半都为正。"
            f"（录制段 {len(runs)} 个：{', '.join(runs)}；第一笔触发于 {_utc(first['t'].min())} UTC，"
            f"最后一笔触发于 {_utc(first['t'].max())} UTC）")


def judge(t, out, n=None):
    """Per rule, the once-only verdict on the first n (FWD_N) forward trades of the rule arm: a line
    pinned in <out>.verdict.md and the trades appended to <out>.trades.csv when they first exist,
    then only ever read back. Returns the report lines."""
    n = FWD_N if n is None else n
    pinned = Path(out).with_suffix(".verdict.md")
    tcsv = Path(out).with_suffix(".trades.csv")
    have = pinned.read_text(encoding="utf-8").splitlines() if pinned.exists() else []
    lines = []
    for rule, (name, _, _) in RULES.items():
        old = [ln for ln in have if ln.startswith(f"- {rule} ")]
        if old:
            lines.append(old[0].rstrip() + "（已判定，不再重算）")
            continue
        s = t[(t["rule"] == rule) & (t["arm"] == "rule") & (t["segment"] == "fwd")].sort_values("t", kind="stable")
        if len(s) < n:
            lines.append(f"- {rule} `{name}`：前向段目前 {len(s):,} 笔，不到 {n:,} 笔，不判定。")
            continue
        first = s.head(n)
        v = verdict_line(rule, first)
        have.append(v)
        pinned.parent.mkdir(parents=True, exist_ok=True)
        lead = "\n" if pinned.exists() and not pinned.read_bytes().endswith(b"\n") and pinned.stat().st_size else ""
        with open(pinned, "a", encoding="utf-8") as fh:  # append only: a pinned line is never rewritten
            fh.write(lead + v + "\n")
        cols = ["rule"] + [c for c in COLS + ["coin", "run", "segment"] if c != "rule"]
        first.reindex(columns=cols).to_csv(tcsv, mode="a", header=not tcsv.exists(), index=False)
        lines.append(v)
    return lines


def report(t, markets, trig, notes, verdict_lines):
    L = ["# 往返族两条规则的前向检验：币安急动后买入、持有到结算（GitHub 前向录制的毫秒盘口）", "",
         "> 设计见 [ROUNDTRIP.md](../ROUNDTRIP.md) 最后一节“通过之后的前向检验”（2026-10-04 17:20 UTC 写死，在看任何 10 月数据之前）；"
         "Hugging Face 复核见 [roundtrip-hf-strict.md](roundtrip-hf-strict.md)；脚本 rt_fwd.py。只用行情数据，纸面研究，不下单。", "",
         "- **R1** `w1-bp4-all-all10-settle`：币安 BTCUSDT 逐笔成交（按记录机收到的时刻）的对数价相对 1 秒前收到的最后一笔变动 ≥ 4 bp；"
         "**R2** `w3-bp8-all-all10-settle`：相对 3 秒前 ≥ 8 bp。参考成交最多早 "
         f"{lt.C_REF_AGE:g} 秒。买变动方向，持有到官方结算。",
         "- 同一市场先取第一次触发，之后只取距上一次保留的触发至少 10 秒的；同一规则在同一市场同时最多一笔仓位，"
         "持有到结算，所以每个市场最多一笔：保留的触发按时间逐个试，第一个能成交的就是这笔。",
         "- 时序：收到触发成交的时刻 t 之后的第一个整秒决定，决定后 0.5 秒按那一刻的卖一吃单买（Down 的卖一 = 1 − Up 买一，"
         "挂单量用 Up 买一的）：卖一 0.02–0.98、至少 5 份、成交快照的买一或卖一价 1 秒内变过、盘口数据流活着"
         f"（前 {lt.C_ALIVE[0]:g} 秒、后 {lt.C_ALIVE[1]:g} 秒内有更新）、决定到撮合之间 CLOB 没断线；"
         "付 taker 费 0.07·p(1 − p)。剩 15 秒以内不再开仓（决定最晚在开盘后 284 秒）。",
         f"- 分段（按市场开始时间，UTC）：独立检验 = {IND_FROM} 至 {FWD_AFTER} 开始的市场（含两端，只报告）；"
         f"前向 = {FWD_AFTER} 之后开始的市场，每条规则攒满 {FWD_N} 笔（按触发时间）判定一次：每份 > 0、"
         f"按市场聚类单侧 p < {ALPHA}、前后两半都为正。", "",
         "## 判定（前向段，每条规则一次，写入 rt-fwd.verdict.md 后不再重算）", ""] + verdict_lines + [""]
    L += ["列：笔数 | 市场 | 每天笔数 | 每份（± 按市场聚类的标准误，t） | 胜率 | 平均买入价 | 每天美元（每笔 5 份） | "
          "每天美元（每笔 min(20, 卖一挂单量) 份）。每份、胜率、平均价都是每份（1 份结算为 1 美元）；"
          "天数 = 该段有盘口的市场数 × 5 分钟（录制只覆盖每天的一部分）。对照用同样的过滤和“每个市场最多一笔”。", ""]
    for rule, (name, _, _) in RULES.items():
        L += [f"## {rule} `{name}`", ""]
        for seg, sname in SEG_NAMES.items():
            mk = markets[markets["segment"] == seg]
            days = len(mk) * bo.WINDOW_S / 86400
            span = f"{_utc(mk['start_ts'].min(), '%m-%d %H:%M')} – {_utc(mk['start_ts'].max(), '%m-%d %H:%M')} UTC" \
                if len(mk) else "无"
            tg = trig[(trig["segment"] == seg) & (trig["rule"] == rule)]
            L += [f"### {sname}", "",
                  f"有盘口的市场 {len(mk):,} 个（开始于 {span}，约 {days:.2f} 天）；有触发的市场 {tg['market_id'].nunique():,} 个，"
                  f"保留的触发 {len(tg):,} 次，其中顺变动方向能成交的 {int(tg['filled'].sum()):,} 次。", "",
                  "| 买什么 | 笔数 | 市场 | 每天笔数 | 每份 ± 标准误（t） | 胜率 | 平均价 | 每天美元（5 份） | 每天美元（min(20, 挂单量) 份） |",
                  "|---|---:|---:|---:|---|---:|---:|---:|---:|"]
            s = t[(t["rule"] == rule) & (t["segment"] == seg)]
            for arm, aname in ARMS.items():
                L.append(f"| {aname} | {row_stats(s[s['arm'] == arm], days)} |")
            L.append("")
    L += ["## 备注", "",
          "- 只用行情，没有下单。成交价、挂单量和“1 秒内变过”只用撮合时刻及以前的盘口行；“数据流活着”和“没断线”"
          "会看撮合之后的行，只用来剔除录制故障。币安成交按记录机收到的时刻排序（同一时刻按成交时间）。",
          "- 随机对照：每个保留的触发配一个随机决定秒（开盘后 1–284 秒均匀）和随机方向（按规则和触发时刻各自取随机数），"
          "每个市场取第一个能成交的。“每次触发都买”不受一笔仓位限制，只作描述。",
          "- 每段录制单独计算；同一市场出现在两段录制里时，每条规则每种买法取最早的一笔。录制里没有官方结果的市场不计。"]
    L += [f"- {n}" for n in notes]
    return "\n".join(L) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root")
    ap.add_argument("--out", default="real/rt-fwd.md")
    a = ap.parse_args(argv)
    t, markets, trig, notes = run(a.root)
    text = report(t, markets, trig, notes, judge(t, a.out))
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
