"""Test 100 reasonable strategies on one recorded day, with multiple-testing control.

    python strategy_zoo.py polymarket-data-samples --out real/strategy-zoo-2026-09-08.md
    python strategy_zoo.py paper_data/bundle --confirm --out paper/confirm.md

Ten families: late favourite, longshot, Asian-digital model edge, Chainlink
momentum, mid move (follow/fade), book imbalance, trade flow, opening edge,
previous-window outcome, and passive maker bids. Every strategy trades at
most once per market, sees only data received by its decision time, and pays
the taker fee unless it is a resting maker order (maker fills are counted
only when a later trade prints strictly through the resting price).

With 100 strategies about five look significant at p < 0.05 by luck alone.
p-values come from binary.fair_price_pvalue (null: every trade wins with
probability equal to its all-in cost), which stays honest when a sample has
no losses; a t-test does not. The report then applies Benjamini-Hochberg and
Bonferroni across all strategies and checks each on both halves of the day.

--confirm re-tests only PREREGISTERED, the candidates picked from the sample
day, on new data (recording.py turns paper_trader recordings into a bundle).
Without --confirm the report also runs split_holdout: pick on one half of the
markets, test on the other half.
Correcting for 7 instead of 100 is what makes a real edge provable in weeks.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

import binary as bo
import real_day as rd

PRICE_BAND = (0.05, 0.95)

# Picked on the 2026-09-08 sample (raw p < 0.20 and positive in both halves)
# before any forward data existed. Never edit once forward data is in: the
# confirmation test is only honest for a list fixed in advance.
PREREGISTERED = (61, 10, 9, 11, 65, 62, 80)


# ------------------------------------------------------------------- loading

def load_books(root, tokens):
    """Full-depth snapshots per token: {token: (recv_ms array, [(bids, asks), ...])}."""
    raw = {}
    with gzip.open(rd._one(root, "*-book-*.jsonl.gz"), "rt") as f:
        for line in f:
            d = json.loads(line)
            if d["asset_id"] not in tokens:
                continue
            p = d["payload"]
            bids = [(float(x["price"]), float(x["size"])) for x in p.get("bids", [])]
            asks = [(float(x["price"]), float(x["size"])) for x in p.get("asks", [])]
            raw.setdefault(d["asset_id"], []).append((int(d["recv_ms"]), bids, asks))
    out = {}
    for tok, rows in raw.items():
        rows.sort(key=lambda r: r[0])
        out[tok] = (np.array([r[0] for r in rows]), [(r[1], r[2]) for r in rows])
    return out


def load_trades(root, tokens):
    rows = []
    with gzip.open(rd._one(root, "*-last_trade_price-*.jsonl.gz"), "rt") as f:
        for line in f:
            d = json.loads(line)
            if d["asset_id"] not in tokens:
                continue
            p = d["payload"]
            rows.append((d["asset_id"], int(d["recv_ms"]), p["side"], float(p["size"]), float(p["price"])))
    df = pd.DataFrame(rows, columns=["token", "recv_ms", "side", "size", "price"])
    return {tok: g.sort_values("recv_ms").reset_index(drop=True) for tok, g in df.groupby("token")}


# ------------------------------------------------------------------- context

@dataclass
class Market:
    slug: str
    start: int
    end: int
    up_won: bool
    prev_up_won: object
    up_token: str
    down_token: str
    rows: pd.DataFrame          # panel rows indexed by second 0..299
    log_spot: pd.Series         # Chainlink log price on a 1s grid
    books: dict
    trades: dict

    def at(self, t):
        return self.rows.loc[t] if t in self.rows.index else None

    def ask(self, r, side):
        return float(r["ask_up"] if side == "Up" else r["ask_down"])

    def book(self, token, t_ms):
        times, snaps = self.books.get(token, (np.array([]), []))
        i = np.searchsorted(times, t_ms, side="right") - 1
        return snaps[i] if i >= 0 else None

    def prints(self, token, t0_ms, t1_ms):
        g = self.trades.get(token)
        if g is None:
            return g
        return g[(g["recv_ms"] > t0_ms) & (g["recv_ms"] <= t1_ms)]


def build_markets(root):
    markets = rd.load_markets(root)
    spot = rd.load_chainlink(rd._one(root, "BTCUSD-prices-*.csv.gz"))
    tokens = set(markets["up_token"]) | set(markets["down_token"])
    quotes = rd.load_quotes(root, tokens)
    vols = rd.vol_forecasts(spot)
    panel = rd.build_panel(markets, spot, quotes, vols)
    books, trades = load_books(root, tokens), load_trades(root, tokens)
    outcome = dict(zip(markets["start"], markets["up_won"]))
    meta = markets.set_index("slug")
    out = []
    for slug, rows in panel.groupby("slug", sort=False):
        m = meta.loc[slug]
        out.append(Market(slug, int(m["start"]), int(m["end"]), bool(m["up_won"]),
                          outcome.get(int(m["start"]) - bo.WINDOW_S), m["up_token"], m["down_token"],
                          rows.set_index("t"), vols["log_spot"], books, trades))
    out.sort(key=lambda m: m.start)
    return out


# ----------------------------------------------------------------- strategies

def taker(side, price, t):
    if not (np.isfinite(price) and 0 < price < 1):
        return None
    return {"side": side, "price": float(price), "fee": float(bo.taker_fee(price)), "t": t, "kind": "taker"}


def in_band(price, lo=PRICE_BAND[0], hi=PRICE_BAND[1]):
    return np.isfinite(price) and lo - 1e-9 <= price <= hi + 1e-9


def late_favourite(tau, lo, hi):
    def f(m):
        r = m.at(bo.WINDOW_S - tau)
        if r is None or not np.isfinite(r["mid"]):
            return None
        side = "Up" if r["mid"] >= 0.5 else "Down"
        p = m.ask(r, side)
        return taker(side, p, bo.WINDOW_S - tau) if in_band(p, lo, hi) else None
    return f


def longshot(tau, lo, hi):
    def f(m):
        r = m.at(bo.WINDOW_S - tau)
        if r is None or not np.isfinite(r["mid"]):
            return None
        side = "Down" if r["mid"] >= 0.5 else "Up"
        p = m.ask(r, side)
        return taker(side, p, bo.WINDOW_S - tau) if in_band(p, lo, hi) else None
    return f


def model_edge(tau, theta):
    def f(m):
        t = bo.WINDOW_S - tau
        r = m.at(t)
        if r is None or r["history_s"] < rd.MIN_HISTORY_S or not r["spot_ok"]:
            return None
        p = r[f"p[{rd.MAIN_VOL}]"]
        if not np.isfinite(p):
            return None
        au, ad = r["ask_up"], r["ask_down"]
        e_u = p - au - bo.taker_fee(au) if np.isfinite(au) else -1
        e_d = (1 - p) - ad - bo.taker_fee(ad) if np.isfinite(ad) else -1
        side, e, px = ("Up", e_u, au) if e_u >= e_d else ("Down", e_d, ad)
        return taker(side, px, t) if e > theta else None
    return f


def chainlink_momentum(tau, lookback, z):
    def f(m):
        t = bo.WINDOW_S - tau
        r = m.at(t)
        if r is None or not r["spot_ok"] or r["history_s"] < rd.MIN_HISTORY_S:
            return None
        now_sec = m.start + int(r["t_info"])
        past = m.log_spot.get(now_sec - lookback)
        if past is None or not np.isfinite(past):
            return None
        move = r["log_spot"] - past
        if abs(move) <= z * r[rd.MAIN_VOL] * math.sqrt(lookback):
            return None
        side = "Up" if move > 0 else "Down"
        p = m.ask(r, side)
        return taker(side, p, t) if in_band(p) else None
    return f


def mid_move(tau, threshold, mode):
    def f(m):
        t = bo.WINDOW_S - tau
        r, r0 = m.at(t), m.at(t - 30)
        if r is None or r0 is None or not (np.isfinite(r["mid"]) and np.isfinite(r0["mid"])):
            return None
        d = r["mid"] - r0["mid"]
        if abs(d) <= threshold:
            return None
        up = (d > 0) == (mode == "follow")
        side = "Up" if up else "Down"
        p = m.ask(r, side)
        return taker(side, p, t) if in_band(p) else None
    return f


def book_imbalance(tau, threshold, depth=3):
    def f(m):
        t = bo.WINDOW_S - tau
        r = m.at(t)
        snap = m.book(m.up_token, (m.start + t) * 1000)
        if r is None or snap is None:
            return None
        bids, asks = snap
        b = sum(s for _, s in sorted(bids, reverse=True)[:depth])
        a = sum(s for _, s in sorted(asks)[:depth])
        if b + a == 0:
            return None
        imb = (b - a) / (b + a)
        if abs(imb) <= threshold:
            return None
        side = "Up" if imb > 0 else "Down"
        p = m.ask(r, side)
        return taker(side, p, t) if in_band(p) else None
    return f


def trade_flow(tau, mode, window_s=60, threshold=0.3):
    def f(m):
        t = bo.WINDOW_S - tau
        r = m.at(t)
        t1 = (m.start + t) * 1000
        if r is None:
            return None
        net = total = 0.0
        for tok, sign in ((m.up_token, 1), (m.down_token, -1)):
            g = m.prints(tok, t1 - window_s * 1000, t1)
            if g is None or g.empty:
                continue
            notional = g["size"] * g["price"]
            signed = np.where(g["side"] == "BUY", notional, -notional)
            net += sign * signed.sum()
            total += notional.sum()
        if total == 0 or abs(net) / total <= threshold:
            return None
        up = (net > 0) == (mode == "follow")
        side = "Up" if up else "Down"
        p = m.ask(r, side)
        return taker(side, p, t) if in_band(p) else None
    return f


def previous_outcome(mode, t=5):
    def f(m):
        r = m.at(t)
        if r is None or m.prev_up_won is None or not (0.40 <= r["mid"] <= 0.60):
            return None
        up = bool(m.prev_up_won) == (mode == "follow")
        side = "Up" if up else "Down"
        return taker(side, m.ask(r, side), t)
    return f


def maker_bid(tau, which):
    """Join the best bid on one side; filled only if a later print trades through it."""
    def f(m):
        t = bo.WINDOW_S - tau
        r = m.at(t)
        if r is None or not np.isfinite(r["mid"]):
            return None
        fav = "Up" if r["mid"] >= 0.5 else "Down"
        side = fav if which == "favourite" else ("Down" if fav == "Up" else "Up")
        bid = r["bid_up"] if side == "Up" else 1 - r["ask_up"]
        if not in_band(bid):
            return None
        token = m.up_token if side == "Up" else m.down_token
        g = m.prints(token, (m.start + t) * 1000, m.end * 1000)
        if g is None or not ((g["side"] == "SELL") & (g["price"] < bid - 1e-9)).any():
            return None
        return {"side": side, "price": float(round(bid, 4)), "fee": 0.0, "t": t, "kind": "maker"}
    return f


@dataclass
class Strategy:
    name: str
    family: str
    fn: Callable


def registry():
    s = []
    for tau in (10, 20, 30, 45, 60, 90, 120, 180):
        for lo, hi in ((0.70, 0.97), (0.80, 0.97), (0.85, 0.99), (0.90, 0.99)):
            s.append(Strategy(f"强势方 τ={tau} [{lo:.2f},{hi:.2f}]", "A 买强势方", late_favourite(tau, lo, hi)))
    for tau in (30, 60, 120, 240):
        for lo, hi in ((0.03, 0.20), (0.10, 0.30)):
            s.append(Strategy(f"弱势方 τ={tau} [{lo:.2f},{hi:.2f}]", "B 买弱势方", longshot(tau, lo, hi)))
    for tau in (30, 60, 120, 240):
        for th in (0.01, 0.03, 0.05):
            s.append(Strategy(f"模型价差 τ={tau} θ={th:.2f}", "C 亚式模型价差", model_edge(tau, th)))
    for tau in (60, 120, 240):
        for lb in (10, 30):
            for z in (1.0, 2.0):
                s.append(Strategy(f"Chainlink 动量 τ={tau} 回看{lb}s z>{z:.0f}", "D Chainlink 动量",
                                  chainlink_momentum(tau, lb, z)))
    for tau in (60, 120, 240):
        for x in (0.10, 0.20):
            for mode in ("follow", "fade"):
                s.append(Strategy(f"mid 30s 变动>{x:.2f} {'跟随' if mode == 'follow' else '反向'} τ={tau}",
                                  "E mid 动量/反转", mid_move(tau, x, mode)))
    for tau in (60, 120, 240):
        for th in (0.3, 0.6):
            s.append(Strategy(f"盘口失衡>{th:.1f} τ={tau}", "F 盘口失衡", book_imbalance(tau, th)))
    for tau in (60, 120, 240):
        for mode in ("follow", "fade"):
            s.append(Strategy(f"成交流 60s {'跟随' if mode == 'follow' else '反向'} τ={tau}", "G 成交流",
                              trade_flow(tau, mode)))
    for tau in (290, 270):
        for th in (0.02, 0.05):
            s.append(Strategy(f"开盘模型价差 t={bo.WINDOW_S - tau}s θ={th:.2f}", "H 开盘价差", model_edge(tau, th)))
    for mode in ("follow", "reverse"):
        s.append(Strategy(f"上一局结果{'延续' if mode == 'follow' else '反转'}", "I 上一局结果", previous_outcome(mode)))
    for tau in (60, 120, 240):
        for which in ("favourite", "longshot"):
            s.append(Strategy(f"挂单做市 {'强势方' if which == 'favourite' else '弱势方'} τ={tau}", "J 挂单(maker)",
                              maker_bid(tau, which)))
    return s


# ----------------------------------------------------------------- evaluation

def run_all(markets, strategies):
    """Per-strategy trades and a markets x strategies P&L matrix (0 where no trade)."""
    idx = {m.slug: i for i, m in enumerate(markets)}
    matrix = np.zeros((len(markets), len(strategies)))
    trades = []
    for j, st in enumerate(strategies):
        for m in markets:
            tr = st.fn(m)
            if tr is None:
                continue
            won = (tr["side"] == "Up") == m.up_won
            pnl = (1.0 if won else 0.0) - tr["price"] - tr["fee"]
            matrix[idx[m.slug], j] = pnl
            trades.append({"strategy": j, "slug": m.slug, "start": m.start, "won": won, "pnl": pnl, **tr})
    return pd.DataFrame(trades), matrix


def benjamini_hochberg(p, q=0.10):
    p = np.asarray(p)
    order = np.argsort(p)
    ranked = p[order] * len(p) / (np.arange(len(p)) + 1)
    passed = np.zeros(len(p), bool)
    below = np.where(ranked <= q)[0]
    if below.size:
        passed[order[: below.max() + 1]] = True
    return passed


def evaluate(markets, strategies, reps=2000, ids=None):
    """One row per strategy. `ids` keeps registry numbering when testing a subset;
    Bonferroni (p_fwer) and BH correct for however many strategies are passed."""
    trades, _ = run_all(markets, strategies)
    return summarize(trades, strategies, [m.start for m in markets], reps, ids), trades


def summarize(trades, strategies, starts, reps=2000, ids=None):
    """evaluate() on trades already made; `trades["strategy"]` indexes `strategies`."""
    ids = list(ids) if ids is not None else list(range(1, len(strategies) + 1))
    half = np.median(starts)
    rows = []
    for j, st in enumerate(strategies):
        g = trades[trades["strategy"] == j] if not trades.empty else trades
        n = len(g)
        row = {"id": ids[j], "family": st.family, "name": st.name, "n": n}
        if n >= 2:
            sd = g["pnl"].std(ddof=1)
            t = g["pnl"].mean() / (sd / math.sqrt(n)) if sd > 0 else np.nan
            cost = (g["price"] + g["fee"]).to_numpy()
            row.update(win=g["won"].mean(), price=g["price"].mean(), ev=g["pnl"].mean(),
                       roi=g["pnl"].sum() / cost.sum(), t=t,
                       p=bo.fair_price_pvalue(g["pnl"].to_numpy(), cost, sims=reps) if n >= 10 else 1.0)
            h1, h2 = g[g["start"] <= half], g[g["start"] > half]
            row.update(ev1=h1["pnl"].mean() if len(h1) else np.nan, n1=len(h1),
                       ev2=h2["pnl"].mean() if len(h2) else np.nan, n2=len(h2))
        else:
            row.update(win=np.nan, price=np.nan, ev=np.nan, roi=np.nan, t=np.nan, p=1.0,
                       ev1=np.nan, n1=0, ev2=np.nan, n2=0)
        rows.append(row)
    res = pd.DataFrame(rows)
    res["p_fwer"] = np.minimum(1.0, res["p"] * len(res))
    res["bh"] = benjamini_hochberg(res["p"].fillna(1.0).to_numpy())
    res["both_halves"] = (res["ev1"] > 0) & (res["ev2"] > 0) & (res["n1"] >= 5) & (res["n2"] >= 5)
    return res


def split_holdout(trades, markets, n_strategies, k=5, reps=20000):
    """Out-of-sample check inside one sample, fixed before looking: pick the k
    lowest-p strategies (>= 10 trades) on one half of the markets, test only those
    on the other half, both directions. Passing needs p < 0.05 / (2k) and EV > 0."""
    half = np.median([m.start for m in markets])
    halves = {"前半": trades[trades["start"] <= half], "后半": trades[trades["start"] > half]} \
        if not trades.empty else {"前半": trades, "后半": trades}

    def stats(g):
        if len(g) < 10:
            return np.nan, len(g), np.nan
        cost = (g["price"] + g["fee"]).to_numpy()
        return bo.fair_price_pvalue(g["pnl"].to_numpy(), cost, sims=reps), len(g), g["pnl"].mean()

    rows = []
    for sel, test in (("前半", "后半"), ("后半", "前半")):
        cand = []
        for j in range(n_strategies):
            p, n, ev = stats(halves[sel][halves[sel]["strategy"] == j] if len(halves[sel]) else halves[sel])
            if n >= 10:
                cand.append((p, j, n, ev))
        for p, j, n, ev in sorted(cand)[:k]:
            tp, tn, tev = stats(halves[test][halves[test]["strategy"] == j])
            rows.append({"direction": f"{sel}选 → {test}验", "id": j + 1, "n_sel": n, "ev_sel": ev, "p_sel": p,
                         "n_test": tn, "ev_test": tev, "p_test": tp,
                         "passed": bool(np.isfinite(tp) and tp < 0.05 / (2 * k) and tev > 0)})
    cols = ["direction", "id", "n_sel", "ev_sel", "p_sel", "n_test", "ev_test", "p_test", "passed"]
    return pd.DataFrame(rows, columns=cols)


def holdout_md(ho, res, k=5):
    names = dict(zip(res["id"], res["name"]))
    lines = ["", "## 样本外检验：一半选、另一半验", "",
             f"规则事先定好：在一半市场上按精确 p 选出最好的 {k} 个，只在另一半上检验这 {k} 个，两个方向各做一次；"
             f"通过标准是检验半的 p < {0.05 / (2 * k):.3f} 且 EV > 0。这比“两半都为正”严格：选出来的策略必须在没参与挑选的数据上复现。", ""]
    if ho.empty:
        return lines + ["（每半可检验的策略不足，跳过）"]
    lines += ["| 方向 | # | 策略 | 选择半 笔数 | 选择半 EV | 选择半 p | 检验半 笔数 | 检验半 EV | 检验半 p | 通过 |",
              "|---|---:|---|---:|---:|---:|---:|---:|---:|:-:|"]
    for r in ho.itertuples():
        lines.append(f"| {r.direction} | {r.id} | {names.get(r.id, '')} | {r.n_sel} | {r.ev_sel*100:+.2f}¢ | {r.p_sel:.3f} | "
                     f"{r.n_test} | {fmt(r.ev_test * 100 if np.isfinite(r.ev_test) else np.nan, '+.2f')}¢ | "
                     f"{fmt(r.p_test, '.3f')} | {'✓' if r.passed else ''} |")
    lines += ["", f"通过：{int(ho['passed'].sum())} / {len(ho)}；检验半 EV 为正：{int((ho['ev_test'] > 0).sum())} / {len(ho)}。"]
    return lines


def fmt(v, spec, default="–"):
    return default if v is None or (isinstance(v, float) and not np.isfinite(v)) else format(v, spec)


def report(res, markets, reps, confirm=False, holdout=None, intro=None):
    n_mk, k = len(markets), len(res)
    tested = res[res["n"] >= 10]
    lucky = 0.05 * len(tested)
    first, last = (pd.to_datetime(m.start, unit="s", utc=True) for m in (markets[0], markets[-1]))
    span = f"{first:%Y-%m-%d}" if first.date() == last.date() else f"{first:%Y-%m-%d} 至 {last:%Y-%m-%d}"
    if intro:
        lines = [intro.format(span=span, k=k, n_mk=n_mk, alpha=0.05 / k)]
    elif confirm:
        lines = [f"# 预注册候选的前向检验：{span}", "",
                 f"只检验 {k} 个在 2026-09-08 样本上事先选定的候选（strategy_zoo.PREREGISTERED），数据全部是选定之后录制的。"
                 f"判定标准：Bonferroni 校正后 p < 0.05（即原始 p < {0.05 / k:.4f}）。达标的才算“确定能赚钱”；"
                 "还没达标只说明样本不够或没有优势，不能提前下结论。", "",
                 f"数据：纸面交易录制，{n_mk} 个 BTC 5 分钟市场，60 秒 TWAP 结算。"]
    else:
        lines = [f"# {k} 个策略：{span} 单日检验", "",
                 f"数据：outcometick 样例，{n_mk} 个 BTC 5 分钟市场，60 秒 TWAP 结算。"]
    lines += [f"每个策略每个市场最多一笔，只用决策时点已收到的数据；"
             "吃单按当时卖一价成交并付 taker 费；挂单（maker）不付费，只有之后出现**更低价**的成交把价位击穿时才算成交（保守）。每笔 1 份，持有到结算。",
             "",
             f"p 值用精确模拟（{reps} 次）：零假设是“每笔的真实胜率 = 买价 + 手续费”，即市场扣费后定价公平，"
             "看实际总盈亏有多大概率被运气达到。**不用 t 检验**：样本全赢时 t 检验的方差只剩价格波动，会把 t 值夸大到离谱。"
             "t 值列只作参考。",
             "", "## 总览", "",
             f"- 策略数：{k}；成交 ≥ 10 笔、可检验的：{len(tested)}。",
             f"- 原始 p < 0.05 的：{int((tested['p'] < 0.05).sum())} 个（全是运气时预期约 {lucky:.1f} 个）。",
             f"- Benjamini–Hochberg（FDR 10%）通过：{int(res['bh'].sum())} 个。",
             f"- Bonferroni（整体误报率 5%，即原始 p < {0.05 / k:.4f}）通过：{int((res['p_fwer'] < 0.05).sum())} 个。",
             f"- 前后半天都为正（各 ≥ 5 笔）：{int(res['both_halves'].sum())} 个。"]
    if (res["n"] >= 10).any():
        top = res.loc[res["p"].idxmin()]
        lines.append(f"- 精确 p 值最小的是 #{int(top['id'])}「{top['name']}」：{int(top['n'])} 笔，胜率 {top['win']*100:.0f}%，"
                     f"每份 {top['ev']*100:+.2f}¢，原始 p = {top['p']:.3f}，Bonferroni 校正后 p = {top['p_fwer']:.3f}。")
    lines += ["", "## 各类最好的一个", "",
              "| 类别 | 最好的策略 | 笔数 | 胜率 | 平均价 | EV/份 | ROI | t | 原始 p | 校正 p | 前半 EV | 后半 EV |",
              "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for fam, g in res.groupby("family"):
        g = g[g["n"] >= 2]
        if g.empty:
            lines.append(f"| {fam} | （无成交） | 0 | – | – | – | – | – | – | – | – | – |")
            continue
        r = g.loc[g["p"].idxmin()]
        lines.append(row_md(r, fam))
    lines += ["", f"## 全部 {k} 个（按精确 p 值排序）", "",
              "| # | 类别 | 策略 | 笔数 | 胜率 | 平均价 | EV/份 | ROI | t | 原始 p | 校正 p | 前半 EV | 后半 EV | BH | 两半都正 |",
              "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|:-:|:-:|"]
    for _, r in res.sort_values(["p", "t"], ascending=[True, False], na_position="last").iterrows():
        lines.append(f"| {int(r['id'])} " + row_md(r, r["family"])[:-1] +
                     f" {'✓' if r['bh'] else ''} | {'✓' if r['both_halves'] else ''} |")
    if holdout is not None:
        lines += holdout_md(holdout, res)
    lines += ["", "## 需要多少笔才能证明", "",
              "以“收盘前 30 秒买 0.80–0.97 的强势方”为例：全部成本约 0.92。如果它一直不输，"
              f"原始 p < 0.05 需要连赢约 {math.ceil(math.log(0.05) / math.log(0.92))} 笔；"
              f"扛住 {k} 个策略的 Bonferroni 校正（p < {0.05 / k:.4f}）需要连赢约 {math.ceil(math.log(0.05 / k) / math.log(0.92))} 笔。"
              "只要中途输一两笔，需要的笔数还会大幅增加。这就是为什么要继续做前向纸面交易。"]
    return "\n".join(lines)


def row_md(r, fam):
    return (f"| {fam} | {r['name']} | {int(r['n'])} | {fmt(r['win']*100 if np.isfinite(r['win']) else np.nan, '.0f')}% | "
            f"{fmt(r['price'], '.3f')} | {fmt(r['ev']*100 if np.isfinite(r['ev']) else np.nan, '+.2f')}¢ | "
            f"{fmt(r['roi']*100 if np.isfinite(r['roi']) else np.nan, '+.1f')}% | {fmt(r['t'], '+.1f')} | "
            f"{fmt(r['p'], '.3f')} | {fmt(r['p_fwer'], '.3f')} | "
            f"{fmt(r['ev1']*100 if np.isfinite(r['ev1']) else np.nan, '+.1f')}¢ | "
            f"{fmt(r['ev2']*100 if np.isfinite(r['ev2']) else np.nan, '+.1f')}¢ |")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root")
    ap.add_argument("--out")
    ap.add_argument("--reps", type=int, default=20000, help="模拟次数（精确 p 值）")
    ap.add_argument("--confirm", action="store_true", help="只检验预注册的候选（用新录制的数据）")
    ap.add_argument("--trades-out", help="逐笔交易另存为 CSV（id 列是注册表编号），供 forward.py 累积")
    args = ap.parse_args(argv)
    markets = build_markets(args.root)
    strategies, ids = registry(), None
    if args.confirm:
        ids = list(PREREGISTERED)
        strategies = [strategies[i - 1] for i in ids]
    res, trades = evaluate(markets, strategies, reps=args.reps, ids=ids)
    holdout = None if args.confirm else split_holdout(trades, markets, len(strategies), reps=args.reps)
    text = report(res, markets, args.reps, confirm=args.confirm, holdout=holdout)
    if args.trades_out:
        out_ids = ids or list(range(1, len(strategies) + 1))
        trades.assign(id=[out_ids[j] for j in trades["strategy"]] if len(trades) else []) \
            .drop(columns=["strategy"], errors="ignore").to_csv(args.trades_out, index=False)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        res.to_csv(Path(args.out).with_suffix(".csv"), index=False)
    print(text)


if __name__ == "__main__":
    main()
