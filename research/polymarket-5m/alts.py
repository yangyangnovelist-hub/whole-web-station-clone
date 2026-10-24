"""ALTS.md (fixed and committed 2026-10-04 20:37 UTC, commit 68fceb4, before any run): test G's and
H's stale-ask taker rule on the ETH, SOL, XRP and DOGE 5m up/down markets of the GitHub forward
recordings, with BTC beside them on the same code path. Paper research on market data only, no orders.

    python alts.py ROOT [--out real/alts.md] [--itode]

ROOT holds the forward recordings as the polymarket-late-ms lane unpacks them
(ROOT/<run id>/x/bundle-<coin>/latency). Coins are passed explicitly (latency._latency_dirs with
COINS, never latency.G_COINS); nothing in latency.py is changed.

Rules (the calls forward_variants.py makes for the BTC curve, real/forward-latency-curve.md)
- Each recording on its own (latency._per_recording from IND_FROM = latency.G_SINCE, feed
  latency.G_SPOT: the coin's own Binance <COIN>USDT aggTrades), latency.gated_trades with
  lag = L, theta = G_THETA, z0 = G_Z0, tau_lo = G_TAU_LO and
  - "G" (G first): max_pre = G_MAX_PRE, the first fill per market; a market recorded twice keeps
    its earliest trade;
  - "H" (H opposite 2x): anchor = 2 s, every = 2 s, all fills; weight 1 for a market's first trade
    and later ones on its side, 2 for later ones on the other side (latency.opp_weights).
- L in LAGS (0.2 .. 0.5 s from the Binance print's exchange time to the fill, the 150 ms taker
  hold included). Execution, fee (binary.taker_fee), book health (C_ALIVE) and the CLOB-disconnect
  check are gated_trades' own.

Controls, for every rule trade (same coin, rule, lag and weight)
- "opposite": the other side's ask at the same moment (t0 + L), 0.02..0.98, no edge gate;
- "random": a moment uniform in the same market's 240..G_TAU_LO s window and a random side, drawn
  from a generator seeded by the coin, the rule and the trigger time in ms (independent of how
  recordings are split), bought at its ask L s later if the book feed was running then (C_ALIVE),
  the CLOB socket did not close across it and the ask is in 0.02..0.98.

Segments (market start, UTC): "ind" from IND_FROM to before FWD_FROM (the independent descriptive
check, reported only); "fwd" from FWD_FROM (after ALTS.md was committed).

Statistics: cents a share sum(w pnl) / sum(w) with the standard error clustered by market and
the one-sided normal p (latency.clustered). Days = a coin's markets with book rows in the segment
x 300 s / 86,400 s (the recorder covers part of each day). $/day at 5 shares a trade (5 w) and at
min(20 w, the ask's displayed size).

Forward (ALTS.md, pinned in <out>.verdict.md, append only, like rt_fwd.py)
- Entry, once per coin of COINS: the first run in which the coin has at least one G-first trade at
  FWD_LAG in the independent segment pins "入选" (per share > 0) or "不入选" (else); a run with no
  such trade (e.g. its bundles were not unpacked) is not a successful run for that coin and pins
  nothing.
- Verdict, once per entered coin: the first FWD_N G-first trades at FWD_LAG in the forward segment
  by trigger time pass with per share > 0, clustered one-sided p < ALPHA = 0.025 / len(COINS), and
  both halves (first and last FWD_N / 2) positive; the line is pinned, the trades appended to
  <out>.trades.csv; a pinned line is only ever read back.

--itode looks up each coin's current 5m market on Gamma and its CLOB /clob-markets record and
reports "itode" (150 ms taker hold), the tick and the fee rate (public market data only).
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
import zlib
from pathlib import Path

import numpy as np
import pandas as pd

import binary as bo
import latency as lt

COINS = ("eth", "sol", "xrp", "doge")  # judged (ALTS.md)
REF = "btc"                            # reported beside them on the same code path, never judged
LAGS = (0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5)
TABLE_LAGS = (0.3, 0.4, 0.5)
IND_FROM, FWD_FROM = lt.G_SINCE, "2026-10-04 21:30"
FWD_LAG, FWD_N = 0.4, 600
ALPHA = 0.025 / len(COINS)
SHARES, CAP = 5.0, 20.0
P_LO, P_HI = 0.02, 0.98
RULES = {"G": ("G 首笔", dict(max_pre=lt.G_MAX_PRE), True),
         "H": ("H 反向 2 倍", dict(anchor=2.0, every=2.0), False)}
ARMS = {"rule": "规则本身", "opposite": "对照：反方向", "random": "对照：随机时刻、随机方向"}
OPP = {"Up": "Down", "Down": "Up"}
COLS = ["coin", "run", "market_id", "start_ts", "segment", "rule", "lag", "arm", "t", "t_buy", "side", "won",
        "price", "fee", "pnl", "size", "w"]
KEYS = ["coin", "market_id", "rule", "lag", "arm", "t"]
GAMMA = "https://gamma-api.polymarket.com/markets?slug={slug}"
CLOB_MARKET = "https://clob.polymarket.com/clob-markets/{cid}"
SLUG = "{coin}-updown-5m-{start}"


def _ts(s):
    return pd.Timestamp(s, tz="UTC").timestamp()


def _utc(x, f="%Y-%m-%d %H:%M"):
    return pd.to_datetime(float(x), unit="s", utc=True).strftime(f)


def segment(start_ts):
    """'ind' for markets starting before FWD_FROM, 'fwd' from it on (IND_FROM is applied on load)."""
    return "fwd" if float(start_ts) >= _ts(FWD_FROM) else "ind"


def _rng(coin, rule, t0):
    return np.random.default_rng([zlib.crc32(coin.encode()), list(RULES).index(rule), int(round(1000 * float(t0)))])


def controls(tr, markets, book, coin, rule, lag, tau_lo=None):
    """The opposite and random-time rows of one rule's trades (gated_trades rows) at one lag; `t` is
    the source trade's trigger time, `t_buy` when the control buys."""
    tau_lo = lt.G_TAU_LO if tau_lo is None else tau_lo
    mk = markets.set_index("market_id")
    rows = []
    for r in tr.itertuples():
        mid, t0 = r.market_id, float(r.t)
        win = mk.at[mid, "winner"]
        end = float(mk.at[mid, "start_ts"]) + bo.WINDOW_S
        g = _rng(coin, rule, t0)
        t_r = float(g.uniform(end - lt.TAUS[0], end - tau_lo))
        s_r = "Up" if g.random() < 0.5 else "Down"
        for arm, sd, t_buy, ok in (("opposite", OPP[r.side], t0 + lag, True),
                                   ("random", s_r, t_r + lag, None)):
            if ok is None:
                ok = book.alive(mid, t_buy, *lt.C_ALIVE) and not book.across_close(mid, t_r, t_buy)
            if not ok:
                continue
            px, size = book.side_ask(mid, t_buy, sd)
            if not (np.isfinite(px) and P_LO <= px <= P_HI):
                continue
            won = float(win == sd)
            fee = float(bo.taker_fee(px))
            rows.append((won, px, fee, won - px - fee, size, t0, mid, lag, sd, rule, arm, t_buy))
    return pd.DataFrame(rows, columns=["won", "price", "fee", "pnl", "size", "t", "market_id", "lag", "side", "rule",
                                       "arm", "t_buy"])


def coin_rows(root, coin, lags=LAGS, notes=None):
    """Every rule and control row of one coin over all recordings under ROOT (COLS), and the coin's
    recorded markets with book rows (coin, market_id, start_ts, segment)."""
    notes = [] if notes is None else notes
    seen = []

    def make(mk, sp, sg, bk):
        mk = mk[mk["market_id"].isin(set(bk.by))]  # a market with no book row has nothing to trade
        seen.append(mk[["market_id", "start_ts"]])
        parts, cut = [], 0
        for lag in lags:
            for rule, (_, extra, _) in RULES.items():
                bk.cut = 0
                tr = lt.gated_trades(mk, sp, sg, bk, lag=lag, theta=lt.G_THETA, z0=lt.G_Z0, tau_lo=lt.G_TAU_LO,
                                     **extra)
                if rule == "G" and np.isclose(lag, FWD_LAG):
                    cut = bk.cut
                tr = tr.assign(rule=rule, arm="rule", t_buy=tr["t"] + lag)
                parts += [tr, controls(tr, mk, bk, coin, rule, lag)]
        bk.cut = cut  # the notes count drops of the judged rule (G first at FWD_LAG) only
        return pd.concat(parts, ignore_index=True)

    t = lt._per_recording(lt._latency_dirs([root], (coin,)), IND_FROM, lt.G_SPOT, make, KEYS, notes)
    markets = pd.concat(seen).drop_duplicates("market_id") if seen else pd.DataFrame(columns=["market_id", "start_ts"])
    markets = markets.assign(coin=coin, segment=[segment(s) for s in markets["start_ts"]])
    if "arm" not in t or not len(t):
        return pd.DataFrame(columns=COLS), markets
    rule = t[t["arm"] == "rule"]
    first = rule["rule"].map(lambda r: RULES[r][2]).astype(bool)
    # a market recorded twice: G keeps its earliest trade over all recordings (t is sorted)
    kept = pd.concat([rule[first].drop_duplicates(["coin", "market_id", "rule", "lag"]), rule[~first]])
    kept = kept.sort_values("t", kind="stable").copy()
    kept["w"] = 1.0
    for (r, _), g in kept.groupby(["rule", "lag"]):
        if not RULES[r][2]:
            kept.loc[g.index, "w"] = lt.opp_weights(g, lt.I_OPP_W)
    src = ["coin", "market_id", "rule", "lag", "t", "run"]
    ctl = t[t["arm"] != "rule"].merge(kept[src + ["w"]], on=src)  # controls of the kept trades only
    out = pd.concat([kept, ctl], ignore_index=True)
    out["start_ts"] = out["market_id"].map(markets.set_index("market_id")["start_ts"]).astype(float)
    out["segment"] = [segment(s) for s in out["start_ts"]]
    for c in ("lag", "t", "t_buy", "won", "price", "fee", "pnl", "size", "w"):
        out[c] = out[c].astype(float)
    out["run"] = out["run"].astype(str)
    return out[COLS].sort_values("t", kind="stable").reset_index(drop=True), markets


def run(root, coins=(REF,) + COINS, lags=LAGS, notes=None):
    """All rows and recorded markets of `coins` (each on its own recordings and Binance feed)."""
    notes = [] if notes is None else notes
    rows, mks = [], []
    for coin in coins:
        t, m = coin_rows(root, coin, lags, notes)
        rows.append(t)
        mks.append(m)
    t = pd.concat([x for x in rows if len(x)], ignore_index=True) if any(len(x) for x in rows) else \
        pd.DataFrame(columns=COLS)
    markets = pd.concat(mks, ignore_index=True)
    return t, markets, notes


def days(markets, coin, seg):
    m = markets[(markets["coin"] == coin) & (markets["segment"] == seg)]
    return len(m) * bo.WINDOW_S / 86400


def sel(t, coin, rule, lag, arm="rule", seg="ind"):
    return t[(t["coin"] == coin) & (t["rule"] == rule) & np.isclose(t["lag"].astype(float), lag) &
             (t["arm"] == arm) & (t["segment"] == seg)]


def stats(t):
    """(cents a share, clustered SE in cents, one-sided p, trades, markets)."""
    if not len(t):
        return np.nan, np.nan, 1.0, 0, 0
    c, se, p, m = lt.clustered(t, t["w"])
    return c, se, p, len(t), m


def cell(t):
    c, se, _, n, _ = stats(t)
    return f"{c:+.1f}¢ ±{se:.1f}（{n}）" if n else "–"


def money(t, d):
    """($/day at 5 shares a trade, $/day at min(20, size) shares; both times the trade's weight)."""
    if not len(t) or d <= 0:
        return np.nan, np.nan
    w, pnl, size = t["w"].to_numpy(float), t["pnl"].to_numpy(float), t["size"].to_numpy(float)
    cap = np.where(np.isfinite(size), np.minimum(CAP * w, size), 0.0)
    return float((SHARES * w * pnl).sum() / d), float((cap * pnl).sum() / d)


def freq_row(t, d):
    """trades | markets traded | markets/day | trades/day | ¢ ± SE | p | win | price | $/day 5 | $/day cap."""
    c, se, p, n, m = stats(t)
    if not n:
        return "0 | 0 | – | – | – | – | – | – | – | –"
    w = t["w"]
    d5, dc = money(t, d)
    per = (lambda x: f"{x / d:,.1f}") if d > 0 else (lambda x: "–")
    return (f"{n:,} | {m:,} | {per(m)} | {per(n)} | {c:+.2f}¢ ±{se:.2f} | {p:.4f} | "
            f"{(w * t['won']).sum() / w.sum():.1%} | {(w * t['price']).sum() / w.sum():.3f} | {d5:+,.2f} | {dc:+,.2f}")


def _cents(c, se):
    return f"{c:+.2f}¢ ±{se:.2f}"


def entry_line(coin, s, run_at):
    c, se, _, n, _ = stats(s)
    ok = bool(n and c > 0)
    runs = sorted(s["run"].astype(str).unique())
    return (f"- 入选 {coin}：独立段（{IND_FROM} UTC 起、{FWD_FROM} UTC 之前开始，这次运行已下载的市场，"
            f"最后一笔触发于 {_utc(s['t'].max())} UTC）G 首笔 {FWD_LAG:g} 秒 {n:,} 笔，每份 {_cents(c, se)} → "
            f"{'**入选**' if ok else '**不入选**'}（事先写死的条件：每份 > 0）。"
            f"（定于 {_utc(run_at)} UTC；录制段 {len(runs)} 个：{', '.join(runs)}）")


def verdict_line(coin, first):
    c, se, p, n, m = stats(first)
    h = len(first) // 2
    c1, c2 = stats(first.iloc[:h])[0], stats(first.iloc[h:])[0]
    ok = bool(c > 0 and p < ALPHA and c1 > 0 and c2 > 0)
    runs = sorted(first["run"].astype(str).unique())
    return (f"- 判定 {coin}：G 首笔 {FWD_LAG:g} 秒（前向段前 {n:,} 笔，{m:,} 个市场）：{'**通过**' if ok else '**不通过**'}。"
            f"每份 {_cents(c, se)}，单侧 p = {p:.2e}；前 {h:,} 笔 {c1:+.2f}¢、后 {n - h:,} 笔 {c2:+.2f}¢；"
            f"胜率 {first['won'].mean():.1%}，平均价 {first['price'].mean():.3f}。"
            f"条件（事先写死）：每份 > 0、按市场聚类单侧 p < {ALPHA:g}、前后两半都为正。"
            f"（录制段 {len(runs)} 个：{', '.join(runs)}；第一笔触发于 {_utc(first['t'].min())} UTC，"
            f"最后一笔触发于 {_utc(first['t'].max())} UTC）")


def _append(path, line):
    path.parent.mkdir(parents=True, exist_ok=True)
    lead = "\n" if path.exists() and path.stat().st_size and not path.read_bytes().endswith(b"\n") else ""
    with open(path, "a", encoding="utf-8") as fh:  # append only: a pinned line is never rewritten
        fh.write(lead + line + "\n")


def judge(t, out, coins=COINS, n=None, run_at=None):
    """Per coin, the pinned entry and the once-only forward verdict (see the module notes); returns
    the report lines."""
    n = FWD_N if n is None else n
    run_at = time.time() if run_at is None else run_at
    pinned = Path(out).with_suffix(".verdict.md")
    tcsv = Path(out).with_suffix(".trades.csv")
    have = pinned.read_text(encoding="utf-8").splitlines() if pinned.exists() else []
    lines = []
    for coin in coins:
        old = [ln for ln in have if ln.startswith(f"- 入选 {coin}：")]
        if old:
            entry = old[0].rstrip()
            lines.append(entry + "（已定，不再改）")
        else:
            s = sel(t, coin, "G", FWD_LAG)
            if not len(s):
                lines.append(f"- 入选 {coin}：独立段还没有 G 首笔 {FWD_LAG:g} 秒的成交，入选待定。")
                continue
            entry = entry_line(coin, s.sort_values("t", kind="stable"), run_at)
            _append(pinned, entry)
            lines.append(entry)
        if "→ **入选**" not in entry:
            lines.append(f"- 判定 {coin}：没入选，前向段只描述，不判定。")
            continue
        old = [ln for ln in have if ln.startswith(f"- 判定 {coin}：")]
        if old:
            lines.append(old[0].rstrip() + "（已判定，不再重算）")
            continue
        s = sel(t, coin, "G", FWD_LAG, seg="fwd").sort_values("t", kind="stable")
        if len(s) < n:
            lines.append(f"- 判定 {coin}：前向段目前 {len(s):,} 笔，不到 {n:,} 笔，不判定。")
            continue
        first = s.head(n)
        v = verdict_line(coin, first)
        _append(pinned, v)
        first.to_csv(tcsv, mode="a", header=not tcsv.exists(), index=False)
        lines.append(v)
    return lines


def fetch_json(url, timeout=15):
    """Public HTTPS JSON (Gamma, CLOB market records); the system trust store."""
    req = urllib.request.Request(url, headers={"User-Agent": "polymarket-research/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def itode(coins, fetch=None, now=None):
    """Per coin: (slug, itode, tick, fee rate, error) of the 5m market open at `now`."""
    fetch = fetch_json if fetch is None else fetch
    now = time.time() if now is None else now
    start = int(now // bo.WINDOW_S * bo.WINDOW_S)
    out = {}
    for coin in coins:
        slug = SLUG.format(coin=coin, start=start)
        try:
            found = fetch(GAMMA.format(slug=slug))
            if not found:
                raise LookupError("not listed on Gamma")
            rec = fetch(CLOB_MARKET.format(cid=found[0]["conditionId"]))
            out[coin] = (slug, bool(rec.get("itode")), rec.get("mts"), (rec.get("fd") or {}).get("r"), None)
        except Exception as e:  # reported, never fatal
            out[coin] = (slug, None, None, None, f"{type(e).__name__}: {e}")
    return out


def report(t, markets, notes, verdict_lines, coins=(REF,) + COINS, itode_rows=None, lags=LAGS):
    name = {c: (f"{c.upper()}（对照）" if c == REF else c.upper()) for c in coins}
    L = ["# ETH、SOL、XRP、DOGE 5m 上的 G/H：币安跳变后吃 Polymarket 的旧卖一（GitHub 前向录制的毫秒盘口）", "",
         "> 设计见 [ALTS.md](../ALTS.md)（2026-10-04 20:37 UTC 提交，在任何运行之前）；脚本 alts.py。只用行情数据，纸面研究，不下单。", "",
         f"规则与 BTC 曲线（forward-latency-curve.md，forward_variants.py）同一段代码（latency.gated_trades），币种显式传入：该币自己的币安 "
         f"USDT 逐笔成交 {lt.G_Z0:g}σ 触发（剩 240–{lt.G_TAU_LO} 秒），公平价 − 卖一 − 手续费 ≥ {100 * lt.G_THETA:.0f}¢，"
         f"延迟 L 后按交易所时间戳盘口的卖一买，taker 费，持有到结算。**G 首笔**：触发前 2 秒中间价已朝买的方向动 ≥ "
         f"{100 * lt.G_MAX_PRE:.0f}¢ 跳过，每个市场第一笔。**H 反向 2 倍**：公平价从触发前 2 秒的中间价加这 2 秒的币安涨跌，"
         "每次都买（间隔 ≥ 2 秒），反方向加仓 2 份。延迟包括 150 ms 吃单冻结。每格：每份 ± 按市场聚类的标准误（笔数）。", "",
         f"分段（按市场开始时间，UTC）：独立段 = {IND_FROM} 起、{FWD_FROM} 之前（只描述）；前向段 = {FWD_FROM} 起。", "",
         "## 判定（入选和前向判定都写入 alts.verdict.md，写过的不再改）", ""] + verdict_lines + [""]
    L += ["## 150 ms 吃单冻结（CLOB /clob-markets 的 itode）", ""]
    if itode_rows:
        L += ["| 币种 | 查的市场 | itode | tick | 费率 |", "|---|---|---|---:|---:|"]
        for coin, (slug, it, mts, fee, err) in itode_rows.items():
            L.append(f"| {coin} | `{slug}` | {'true（有 150 ms 冻结）' if it else '没有' if it is not None else '查询失败：' + err} | "
                     f"{mts if mts is not None else '–'} | {fee if fee is not None else '–'} |")
        L.append("")
    else:
        L += ["这次没查（不带 --itode）。ALTS.md：2026-10-04 20:35 UTC 五个币种的 5m 市场都是 itode: true。", ""]
    L += ["## 录到的市场", "", "| 币种 | 段 | 有盘口的市场 | 开始于（UTC） | 折算天数 |", "|---|---|---:|---|---:|"]
    for coin in coins:
        for seg, sname in (("ind", "独立段"), ("fwd", "前向段")):
            m = markets[(markets["coin"] == coin) & (markets["segment"] == seg)]
            span = f"{_utc(m['start_ts'].min(), '%m-%d %H:%M')} – {_utc(m['start_ts'].max(), '%m-%d %H:%M')}" \
                if len(m) else "–"
            L.append(f"| {name[coin]} | {sname} | {len(m):,} | {span} | {days(markets, coin, seg):.2f} |")
    L += ["", "折算天数 = 有盘口的市场数 × 5 分钟 / 24 小时（录制只覆盖每天的一部分，下面的“每天”都按录满一整天折算）。", ""]
    for rule, (rname, _, _) in RULES.items():
        L += [f"## 表 1{'a' if rule == 'G' else 'b'} {rname}：每份随延迟（独立段）", "",
              "| 延迟 | " + " | ".join(name[c] for c in coins) + " |", "|---|" + "---:|" * len(coins)]
        for lag in lags:
            L.append(f"| {lag:g} 秒 | " + " | ".join(cell(sel(t, c, rule, lag)) for c in coins) + " |")
        L.append("")
    L += ["## 表 2 频率和每天美元（独立段）", "",
          "列：笔数 | 有成交的市场 | 每天有成交的市场 | 每天笔数 | 每份 ± 聚类标准误 | 单侧 p | 胜率 | 平均买入价 | "
          f"每天美元（每笔 {SHARES:g} 份）| 每天美元（每笔 min({CAP:g}, 卖一挂单量) 份）。H 的份数、胜率和均价按权重算（反向加仓 2 倍）。", "",
          "| 币种 | 规则 | 延迟 | 笔数 | 有成交的市场 | 市场/天 | 笔/天 | 每份 | p | 胜率 | 均价 | $/天（5 份） | $/天（min(20, 挂单量)） |",
          "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for coin in coins:
        d = days(markets, coin, "ind")
        for rule, (rname, _, _) in RULES.items():
            for lag in TABLE_LAGS:
                L.append(f"| {name[coin]} | {rname} | {lag:g} 秒 | {freq_row(sel(t, coin, rule, lag), d)} |")
    L += ["", "## 表 3 对照（独立段）", "",
          "反方向：同一笔的同一时刻买另一边的卖一（不设门槛）；随机：同一市场剩 240–15 秒内随机时刻、随机方向，L 秒后按卖一买。份数同规则。", "",
          "| 币种 | 规则 | 延迟 | " + " | ".join(ARMS.values()) + " |", "|---|---|---|---:|---:|---:|"]
    for coin in coins:
        for rule, (rname, _, _) in RULES.items():
            for lag in TABLE_LAGS:
                L.append(f"| {name[coin]} | {rname} | {lag:g} 秒 | " +
                         " | ".join(cell(sel(t, coin, rule, lag, arm)) for arm in ARMS) + " |")
    L += ["", f"## 表 4 前后两半（独立段，{FWD_LAG:g} 秒，按市场开始时间把每个币种的市场分成两半）", "",
          "| 币种 | 前半开始于 | 后半开始于 | " + " | ".join(f"{r[0]} 前半 | {r[0]} 后半" for r in RULES.values()) + " |",
          "|---|---|---|" + "---:|" * (2 * len(RULES))]
    for coin in coins:
        st = np.sort(markets.loc[(markets["coin"] == coin) & (markets["segment"] == "ind"), "start_ts"].astype(float))
        if len(st) < 2:
            continue
        cut = st[len(st) // 2]
        cells = []
        for rule in RULES:
            s = sel(t, coin, rule, FWD_LAG)
            cells += [cell(s[s["start_ts"] < cut]), cell(s[s["start_ts"] >= cut])]
        L.append(f"| {name[coin]} | {_utc(st[0], '%m-%d %H:%M')} – {_utc(st[len(st) // 2 - 1], '%m-%d %H:%M')} | "
                 f"{_utc(cut, '%m-%d %H:%M')} – {_utc(st[-1], '%m-%d %H:%M')} | " + " | ".join(cells) + " |")
    L += ["", f"## 表 5 前向段（{FWD_FROM} UTC 起开始的市场；只描述，判定见上）", "",
          "| 币种 | 延迟 | " + " | ".join(f"{r[0]}" for r in RULES.values()) +
          f" | {RULES['G'][0]} $/天（5 份） | {RULES['H'][0]} $/天（5 份） |", "|---|---|" + "---:|" * (2 * len(RULES))]
    for coin in coins:
        d = days(markets, coin, "fwd")
        for lag in TABLE_LAGS:
            rows = [sel(t, coin, rule, lag, seg="fwd") for rule in RULES]
            m5 = [money(x, d)[0] for x in rows]
            L.append(f"| {name[coin]} | {lag:g} 秒 | " + " | ".join(cell(x) for x in rows) + " | " +
                     " | ".join(f"{x:+,.2f}" if np.isfinite(x) else "–" for x in m5) + " |")
    L += ["", "## 备注", "",
          "- 只用行情，没有下单。成交价和挂单量只用成交时刻及以前的盘口行；“盘口在跑”和“没断线”会看之后的行，只用来剔除录制故障。",
          "- 每段录制单独计算（σ、盘口、断线记录都是这一段的）；同一市场出现在两段录制里时，G 首笔取最早的一笔，"
          "H 的成交按触发时刻去重；对照只跟着留下的成交。录制里没有官方结果的市场不计。",
          f"- 下面“candidates dropped”的条数只计 G 首笔 {FWD_LAG:g} 秒（被判定的规则）。"]
    L += [f"- {n}" for n in notes]
    return "\n".join(L) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root")
    ap.add_argument("--out", default="real/alts.md")
    ap.add_argument("--itode", action="store_true", help="look up each coin's current 5m market's itode flag")
    a = ap.parse_args(argv)
    coins = (REF,) + COINS
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    try:
        t, markets, notes = run(a.root, coins)
        rows = itode(coins) if a.itode else None
        text = report(t, markets, notes, judge(t, a.out), coins, rows)
    except Exception:  # the lane commits the report: leave the error where it can be read
        import traceback
        Path(a.out).write_text("# alts.py 出错（这次没有结果）\n\n```\n" + traceback.format_exc() + "```\n",
                               encoding="utf-8")
        raise
    Path(a.out).write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
