"""Batch-test executable rules on the on-chain tape (every 5m market, five coins).

    python onchain.py fetch data/onchain                         # Gamma, Hugging Face fills, Binance
    python rules.py data/onchain --out real/rules-sept.md        # explore 09-01..25, pick candidates
    python rules.py data/onchain --final real/rules-candidates.json --from 2026-09-26 --to 2026-10-05

What a rule can see and pay, per market and second s of its window:
- The tape: a fill is known from its block time minus 1 s (the settlement
  delay is 1.5-3.6 s, so it was matched before that).
- Quotes from the aggressors: an aggressor buying Up or selling Down at an
  Up-equivalent price x shows the Up ask at x; selling Up or buying Down shows
  the Up bid. The Down ask is 1 - Up bid.
- Buying pays the worse of the last ask seen (at most 10 s old) and the next
  one seen (within 5 s after s), plus the taker fee 0.07 p (1 - p); one share,
  held to settlement. No quote seen, no trade.
- Binance 1s closes with a 2 s lag (4 s for the slower momentum variant),
  and real_day.MAIN_VOL as the per-second vol.

Rules 1-116 were fixed 2026-09-29 before any of them was run:
- A favourite at tau in {240,180,120,90,60,45,30,20,10} s with its ask in
  [0.50,0.60) ... [0.90,0.97): 45
- B underdog at the same tau with its ask in [0.03,0.10) ... [0.40,0.50): 45
- C Binance momentum: first 10 s move > z sigma (z 1.5, 2, 3) with 240..60 s
  left, signal lag 2 or 4 s: 6
- D Asian-binary model on Binance (strike = 60 s mean at the open) against
  the ask: edge after fee > theta (0.02, 0.05, 0.10) at tau 240, 180, 120, 60: 12
- E fade a Polymarket move of more than x (0.10, 0.20) over 30 s while
  Binance moved less than 0.5 sigma, at tau 180, 120, 60: 6
- F previous market's winner, followed or reversed, bought 30 s in: 2
Rules 117-172 were added after 1-116 had run and every taker rule lost:
- G a resting bid at the current bid of the favourite (bid in [0.60,0.70) ...
  [0.90,0.97)) or of the underdog ([0.02,0.10) ... [0.30,0.40)) at tau 240,
  180, 120, 90, 60, 45, 30; filled only if a later trade of that side prints
  below the bid (so the whole queue at that price was used up), no fee: 56

Exploration on 2026-09-01..25 uses a split-half holdout, both ways: the ten
rules with the smallest exact p (EV > 0, at least 100 trades) on one half are
tested on the other at p < 0.005 (Bonferroni over ten) with EV > 0. The
rules that pass either way are written to a candidates file; the verdict
comes from --final on days after 2026-09-25 that no choice here has seen:
EV > 0 and exact p < 0.05 / (number of candidates).

September was already looked at by price and time left in makers.py, so the
A and B families are not blind here; only --final is.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

import binary as bo
import onchain as oc
import real_day as rd

KNOWN_AFTER_BLOCK = 1
ASK_MAX_AGE, ASK_MAX_LEAD = 10, 5
LAG, SLOW_LAG = 2, 4
SPLIT = date(2026, 9, 14)
K_SELECT, MIN_TRADES = 10, 100
GRID = np.arange(bo.WINDOW_S)


# ---------------------------------------------------------------- per market

def _last_next(times, values, grid):
    """Last value at or before each grid second (with its age) and the next one after it (with its lead)."""
    if len(times) == 0:
        nan = np.full(len(grid), np.nan)
        return nan, nan, nan, nan
    i = np.searchsorted(times, grid, "right") - 1
    j = i + 1
    last = np.where(i >= 0, values[np.maximum(i, 0)], np.nan)
    age = np.where(i >= 0, grid - times[np.maximum(i, 0)], np.nan)
    nxt = np.where(j < len(times), values[np.minimum(j, len(times) - 1)], np.nan)
    lead = np.where(j < len(times), times[np.minimum(j, len(times) - 1)] - grid, np.nan)
    return last, age, nxt, lead


@dataclass
class State:
    """What a rule can know in one market, per second of its window."""
    start: int
    coin: str
    up_won: bool
    prev_up_won: float        # NaN if unknown
    up_px: np.ndarray         # last Up-equivalent trade price (NaN if none in 30 s)
    ask_up: np.ndarray        # executable Up ask: worse of last and next (NaN if neither)
    ask_down: np.ndarray
    mid: np.ndarray
    last_bid_up: np.ndarray   # last Up bid / ask seen, at most 10 s old
    last_ask_up: np.ndarray
    fut_min_up: np.ndarray    # lowest / highest Up-equivalent trade after each second
    fut_max_up: np.ndarray
    spot: Callable            # spot(sec) -> log price arrays
    vol: Callable

    def buy(self, side, s):
        p = (self.ask_up if side == "Up" else self.ask_down)[s]
        return float(p) if np.isfinite(p) and 0 < p < 1 else None

    def fav(self, s):
        m = self.mid[s]
        return None if not np.isfinite(m) else ("Up" if m >= 0.5 else "Down")


def build_state(start, coin, up_won, prev_up_won, t_block, is_up, price, agg, buy, spot, vol):
    t = t_block.astype("int64") - KNOWN_AFTER_BLOCK - start
    order = np.argsort(t, kind="stable")
    t, is_up, price, agg, buy = t[order], is_up[order], price[order], agg[order], buy[order]
    up_eq = np.where(is_up, price, 1 - price)
    ask_obs = agg & (is_up == buy)   # buy Up or sell Down: someone paid the Up ask
    bid_obs = agg & (is_up != buy)   # sell Up or buy Down: someone hit the Up bid

    def exec_side(mask, worse):
        last, age, nxt, lead = _last_next(t[mask], up_eq[mask], GRID)
        a = np.where(age <= ASK_MAX_AGE, last, np.nan)
        b = np.where(lead <= ASK_MAX_LEAD, nxt, np.nan)
        return worse(a, b), np.where(age <= ASK_MAX_AGE, last, np.nan)

    ask_up, last_ask = exec_side(ask_obs, np.fmax)
    bid_up, last_bid = exec_side(bid_obs, np.fmin)
    px, px_age, _, _ = _last_next(t, up_eq, GRID)
    px = np.where(px_age <= 30, px, np.nan)
    mid = np.where(np.isfinite(last_ask) & np.isfinite(last_bid), (last_ask + last_bid) / 2, px)
    after = np.searchsorted(t, GRID, "right")
    suf_min = np.append(np.minimum.accumulate(up_eq[::-1])[::-1], np.inf)
    suf_max = np.append(np.maximum.accumulate(up_eq[::-1])[::-1], -np.inf)
    return State(start, coin, up_won, prev_up_won, px, ask_up, 1 - bid_up, mid, last_bid, last_ask,
                 suf_min[after], suf_max[after], spot, vol)


# --------------------------------------------------------------------- rules

@dataclass
class Rule:
    id: int
    name: str
    family: str
    fn: Callable


def _in(p, lo, hi):
    return p is not None and lo - 1e-9 <= p < hi - 1e-9


def favourite(tau, lo, hi):
    def f(st):
        s = bo.WINDOW_S - tau
        side = st.fav(s)
        p = st.buy(side, s) if side else None
        return (side, p, s) if _in(p, lo, hi) else None
    return f


def underdog(tau, lo, hi):
    def f(st):
        s = bo.WINDOW_S - tau
        fav = st.fav(s)
        side = None if fav is None else ("Down" if fav == "Up" else "Up")
        p = st.buy(side, s) if side else None
        return (side, p, s) if _in(p, lo, hi) else None
    return f


def momentum(z, lag, lookback=10, taus=(240, 60)):
    def f(st):
        s = np.arange(bo.WINDOW_S - taus[0], bo.WINDOW_S - taus[1] + 1)
        info = st.start + s - lag
        (x, h), v = st.spot(info), st.vol(info)
        move = x - st.spot(info - lookback)[0]
        with np.errstate(invalid="ignore"):
            hit = (h >= rd.MIN_HISTORY_S) & (np.abs(move) > z * v * math.sqrt(lookback))
        for k in np.flatnonzero(hit):
            side = "Up" if move[k] > 0 else "Down"
            p = st.buy(side, int(s[k]))
            if _in(p, 0.05, 0.95):
                return side, p, int(s[k])
        return None
    return f


def model_edge(tau, theta):
    def f(st):
        s = bo.WINDOW_S - tau
        info = st.start + s - LAG
        strike_secs = np.arange(st.start - bo.TWAP_S + 1, st.start + 1)
        k = np.nanmean(st.spot(strike_secs)[0])
        x, h = st.spot(np.array([info]))
        v = st.vol(np.array([info]))
        if not (np.isfinite(k) and np.isfinite(x[0]) and np.isfinite(v[0]) and h[0] >= rd.MIN_HISTORY_S):
            return None
        t_info = s - LAG
        in_window = np.arange(st.start + bo.WINDOW_S - bo.TWAP_S + 1, info + 1)
        known = float(np.nansum(st.spot(in_window)[0])) if len(in_window) else 0.0
        p_up = float(bo.prob_up(x[0], k, v[0], t_info, known))
        best = None
        for side, prob in (("Up", p_up), ("Down", 1 - p_up)):
            p = st.buy(side, s)
            if _in(p, 0.03, 0.97):
                e = prob - p - float(bo.taker_fee(p))
                if e > theta and (best is None or e > best[0]):
                    best = (e, side, p)
        return (best[1], best[2], s) if best else None
    return f


def fade(tau, x, lookback=30):
    def f(st):
        s = bo.WINDOW_S - tau
        now, then = st.mid[s], st.mid[s - lookback]
        if not (np.isfinite(now) and np.isfinite(then)) or abs(now - then) <= x:
            return None
        info = st.start + s - LAG
        xs, h = st.spot(np.array([info, info - lookback]))
        v = st.vol(np.array([info]))[0]
        if not (np.all(np.isfinite(xs)) and np.isfinite(v)) or abs(xs[0] - xs[1]) >= 0.5 * v * math.sqrt(lookback):
            return None
        side = "Down" if now > then else "Up"
        p = st.buy(side, s)
        return (side, p, s) if _in(p, 0.05, 0.95) else None
    return f


def previous(mode):
    def f(st):
        if not np.isfinite(st.prev_up_won):
            return None
        side = "Up" if bool(st.prev_up_won) == (mode == "follow") else "Down"
        p = st.buy(side, 30)
        return (side, p, 30) if _in(p, 0.05, 0.95) else None
    return f


def maker(tau, which, lo, hi):
    """Join the bid of one side; filled only if a later trade goes through it (conservative:
    the whole queue at that price, us included, was used up). Makers pay no fee."""
    def f(st):
        s = bo.WINDOW_S - tau
        fav = st.fav(s)
        if fav is None:
            return None
        side = fav if which == "favourite" else ("Down" if fav == "Up" else "Up")
        if side == "Up":
            bid = st.last_bid_up[s]
            filled = st.fut_min_up[s] < bid - 1e-9
        else:
            bid = 1 - st.last_ask_up[s]
            filled = st.fut_max_up[s] > st.last_ask_up[s] + 1e-9
        if not (np.isfinite(bid) and _in(float(bid), lo, hi) and filled):
            return None
        return side, float(bid), s, "maker"
    return f


TAUS = (240, 180, 120, 90, 60, 45, 30, 20, 10)
FAV_BANDS = ((0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 0.90), (0.90, 0.97))
DOG_BANDS = ((0.03, 0.10), (0.10, 0.20), (0.20, 0.30), (0.30, 0.40), (0.40, 0.50))


def registry():
    rules = []

    def add(name, family, fn):
        rules.append(Rule(len(rules) + 1, name, family, fn))

    for tau in TAUS:
        for lo, hi in FAV_BANDS:
            add(f"强势方 τ={tau} 卖一∈[{lo:.2f},{hi:.2f})", "A 强势方", favourite(tau, lo, hi))
    for tau in TAUS:
        for lo, hi in DOG_BANDS:
            add(f"弱势方 τ={tau} 卖一∈[{lo:.2f},{hi:.2f})", "B 弱势方", underdog(tau, lo, hi))
    for z in (1.5, 2.0, 3.0):
        for lag in (LAG, SLOW_LAG):
            add(f"币安动量 10s z>{z:g} 延后{lag}s τ=240..60", "C 币安动量", momentum(z, lag))
    for tau in (240, 180, 120, 60):
        for th in (0.02, 0.05, 0.10):
            add(f"亚式模型价差 τ={tau} θ={th:.2f}", "D 模型价差", model_edge(tau, th))
    for tau in (180, 120, 60):
        for x in (0.10, 0.20):
            add(f"盘口 30s 变动>{x:.2f} 币安没动 反向 τ={tau}", "E 反向", fade(tau, x))
    for mode in ("follow", "reverse"):
        add(f"上一局结果{'延续' if mode == 'follow' else '反转'}", "F 上一局", previous(mode))
    # Added 2026-09-29 after rules 1-116 had run (all taker rules lost): resting bids,
    # filled only when a later trade goes through them. Ids 117-172.
    for tau in (240, 180, 120, 90, 60, 45, 30):
        for which, bands in (("favourite", ((0.60, 0.70), (0.70, 0.80), (0.80, 0.90), (0.90, 0.97))),
                             ("underdog", ((0.02, 0.10), (0.10, 0.20), (0.20, 0.30), (0.30, 0.40)))):
            for lo, hi in bands:
                add(f"挂买单 {'强势方' if which == 'favourite' else '弱势方'} τ={tau} 买一∈[{lo:.2f},{hi:.2f})",
                    "G 挂单（被穿价才算成交）", maker(tau, which, lo, hi))
    return rules


# --------------------------------------------------------------------- run

def spot_arrays(spot):
    """Callables sec -> (log price, history) and sec -> vol on a dense grid (NaN outside)."""
    v = rd.vol_forecasts(spot)
    sec0 = int(v.index[0])
    lp, hist, vol = (v["log_spot"].to_numpy(float), v["history_s"].to_numpy(float), v[rd.MAIN_VOL].to_numpy(float))

    def take(arr, secs):
        i = np.asarray(secs, dtype="int64") - sec0
        ok = (i >= 0) & (i < len(arr))
        return np.where(ok, arr[np.clip(i, 0, len(arr) - 1)], np.nan)

    return (lambda secs: (take(lp, secs), take(hist, secs))), (lambda secs: take(vol, secs))


def evaluate(root, rules, d0, d1, progress=True):
    """One row per (rule, market) that traded."""
    markets, fills = oc.load(root, extra=["maker_direction", "agg"])
    days = pd.to_datetime(markets["start"], unit="s", utc=True).dt.date
    markets = markets[(days >= d0) & (days <= d1)].sort_values(["coin", "start"]).reset_index(drop=True)
    prev = {(c, s + bo.WINDOW_S): w for c, s, w in zip(markets["coin"], markets["start"], markets["up_won"])}
    tokens = fills["token_asset_id"].astype("category")
    cats = pd.Index(tokens.cat.categories.astype(str))
    tok_market = pd.Series(-1, index=cats)
    tok_is_up = pd.Series(False, index=cats)
    for i, (u, d) in enumerate(zip(markets["up_token"].astype(str), markets["down_token"].astype(str))):
        if u in tok_market.index:
            tok_market[u], tok_is_up[u] = i, True
        if d in tok_market.index:
            tok_market[d] = i
    codes = tokens.cat.codes.to_numpy()
    mi = np.where(codes >= 0, tok_market.to_numpy()[np.maximum(codes, 0)], -1)
    keep = mi >= 0
    side = fills["maker_direction"].astype("category")
    buy_all = np.asarray(side.cat.categories.astype(str).str.upper() == "BUY")[side.cat.codes.to_numpy()]
    f = pd.DataFrame({"m": mi[keep], "t": fills["timestamp"].to_numpy("int64")[keep],
                      "up": tok_is_up.to_numpy()[np.maximum(codes, 0)][keep],
                      "price": fills["price"].to_numpy(float)[keep], "agg": fills["agg"].to_numpy(bool)[keep],
                      "buy": buy_all[keep]}).sort_values(["m", "t"], kind="stable")
    del fills, tokens
    bounds = np.searchsorted(f["m"].to_numpy(), np.arange(len(markets) + 1))
    arr = {c: f[c].to_numpy() for c in ("t", "up", "price", "agg", "buy")}
    spots = {}
    for coin in markets["coin"].unique():
        p = Path(str(oc.paths(root)["binance"]).format(coin=coin))
        spots[coin] = spot_arrays(pd.read_parquet(p)) if p.exists() else (None, None)
    out = []
    for i, mk in enumerate(markets.itertuples()):
        a, b = bounds[i], bounds[i + 1]
        if b - a == 0 or spots[mk.coin][0] is None:
            continue
        st = build_state(int(mk.start), mk.coin, bool(mk.up_won), float(prev.get((mk.coin, mk.start), np.nan)),
                         arr["t"][a:b], arr["up"][a:b], arr["price"][a:b], arr["agg"][a:b], arr["buy"][a:b],
                         *spots[mk.coin])
        for r in rules:
            got = r.fn(st)
            if got:
                sd, p, s = got[:3]
                won = st.up_won if sd == "Up" else not st.up_won
                fee = 0.0 if len(got) > 3 and got[3] == "maker" else float(bo.taker_fee(p))
                out.append((r.id, mk.coin, int(mk.start), sd, p, fee, float(won), float(won) - p - fee, s))
        if progress and i % 5000 == 0:
            print(f"  {i:,}/{len(markets):,} markets", flush=True)
    t = pd.DataFrame(out, columns=["id", "coin", "start", "side", "price", "fee", "won", "pnl", "s"])
    t["day"] = pd.to_datetime(t["start"], unit="s", utc=True).dt.date
    return t, len(markets)


def stats(t, reps):
    if t.empty:
        return {"n": 0, "win": np.nan, "price": np.nan, "ev": np.nan, "p": 1.0}
    p = bo.fair_price_pvalue(t["pnl"].to_numpy(), (t["price"] + t["fee"]).to_numpy(), sims=reps) \
        if len(t) >= 10 and t["pnl"].mean() > 0 else 1.0
    return {"n": len(t), "win": t["won"].mean(), "price": t["price"].mean(), "ev": t["pnl"].mean(), "p": p}


def fmt(s):
    if not s["n"]:
        return "0 | – | – | – | –"
    return f"{s['n']:,} | {s['win']:.1%} | {s['price']:.3f} | {100 * s['ev']:+.2f}¢ | {s['p']:.4f}"


def holdout(t, rules, reps):
    """Pick on one half, test on the other, both ways."""
    halves = {"前半（1–13 日）": t[t["day"] < SPLIT], "后半（14 日起）": t[t["day"] >= SPLIT]}
    names = list(halves)
    rows, passed = [], set()
    for a, b in ((0, 1), (1, 0)):
        sel, test = halves[names[a]], halves[names[b]]
        cand = []
        for r in rules:
            g = sel[sel["id"] == r.id]
            if len(g) >= MIN_TRADES and g["pnl"].mean() > 0:
                cand.append((stats(g, 2000)["p"], r))
        cand.sort(key=lambda x: x[0])
        for p_sel, r in cand[:K_SELECT]:
            s_sel = stats(sel[sel["id"] == r.id], reps)
            s_test = stats(test[test["id"] == r.id], reps)
            ok = s_test["n"] > 0 and s_test["ev"] > 0 and s_test["p"] < 0.05 / K_SELECT
            if ok:
                passed.add(r.id)
            rows.append(f"| {names[a]} → {names[b]} | {r.id} | {r.name} | {fmt(s_sel)} | {fmt(s_test)} | {'✓' if ok else ''} |")
    return rows, passed


def run(root, out, reps=20000, d0=date(2026, 9, 1), d1=date(2026, 9, 25), cand_out=None):
    rules = registry()
    t, n_mk = evaluate(root, rules, d0, d1)
    L = [f"# 通用回测器：{len(rules)} 条可执行规则，链上 {d0} 至 {d1}", "",
         "规则、成交假设和检验方法写在 `rules.py` 文件头（2026-09-29 定好，运行前）。要点：成交在区块时间减 1 秒后才算看到；"
         "买一价、卖一价从吃单方的成交推出来；买入按“上一次看到的卖一”和“下一次看到的卖一”中更贵的那个成交，再付 taker 费；"
         "每条规则每个市场最多一笔，1 份持有到结算。", "",
         f"数据：{n_mk:,} 个已结算的 5 分钟市场（5 个币），{len(t):,} 笔规则成交。", "",
         "## 前半选、后半验（两个方向；每次选 p 最小的 10 条，检验门槛 p < 0.005 且 EV > 0）", "",
         "| 方向 | # | 规则 | 选择期 笔数 / 胜率 / 平均价 / EV / p | 检验期 笔数 / 胜率 / 平均价 / EV / p | 通过 |",
         "|---|---:|---|---|---|:-:|"]
    rows, passed = holdout(t, rules, reps)
    L += rows
    L += ["", f"**{len(passed)} 条规则在另一半数据上通过。**" + (
        " 它们写进候选文件，最终判定要看 9 月 25 日以后、没参与任何选择的数据（`--final`）。" if passed else ""), "",
          "## 全部规则（全月，p 用 2000 次模拟，只作参考）", "",
          "| # | 类别 | 规则 | 笔数 | 胜率 | 平均价 | EV/份 | p | 前半 EV | 后半 EV |", "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for r in rules:
        g = t[t["id"] == r.id]
        s = stats(g, 2000)
        e1 = g[g["day"] < SPLIT]["pnl"].mean() if len(g) else np.nan
        e2 = g[g["day"] >= SPLIT]["pnl"].mean() if len(g) else np.nan
        L.append(f"| {r.id} | {r.family} | {r.name} | {fmt(s)} | {100 * e1:+.2f}¢ | {100 * e2:+.2f}¢ |"
                 if s["n"] else f"| {r.id} | {r.family} | {r.name} | 0 | – | – | – | – | – | – |")
    by_coin = [f"| {r.id} | " + " | ".join(
        f"{100 * t[(t['id'] == r.id) & (t['coin'] == c)]['pnl'].mean():+.2f}¢" for c in oc.COINS) + " |"
        for r in rules if r.id in passed]
    if by_coin:
        L += ["", "## 通过的规则分币种（全月 EV）", "", "| # | " + " | ".join(oc.COINS) + " |",
              "|---:|" + "---:|" * len(oc.COINS)] + by_coin
    L += ["", "注意：买一、卖一是从吃单成交推出来的，不是真实盘口；对比真实盘口会有偏差。"
          "这些结果只用来挑候选，不算“确定能赚钱”。"]
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text("\n".join(L) + "\n", encoding="utf-8")
    if cand_out:
        Path(cand_out).write_text(json.dumps({"chosen_on": f"{d0}..{d1}", "ids": sorted(passed),
                                              "names": {r.id: r.name for r in rules if r.id in passed}},
                                             ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n".join(L))
    return t, passed


def final(root, cand_file, out, d0, d1, reps=20000):
    """The verdict on days no choice has seen."""
    cand = json.loads(Path(cand_file).read_text())
    ids = [int(i) for i in cand["ids"]]
    rules = [r for r in registry() if r.id in ids]
    L = [f"# 候选规则的最终检验：{d0} 至 {d1}", "",
         f"候选在 {cand['chosen_on']} 上选出（`{cand_file}`），这里的日子没参与任何选择。"
         f"通过标准：EV > 0 且精确 p < 0.05 / {max(len(ids), 1)}。", ""]
    if not rules:
        L.append("没有候选。")
    else:
        t, n_mk = evaluate(root, rules, d0, d1, progress=False)
        L += [f"数据：{n_mk:,} 个市场。", "", "| # | 规则 | 笔数 | 胜率 | 平均价 | EV/份 | p | 通过 |",
              "|---:|---|---:|---:|---:|---:|---:|:-:|"]
        for r in rules:
            s = stats(t[t["id"] == r.id], reps)
            ok = s["n"] and s["ev"] > 0 and s["p"] < 0.05 / len(ids)
            L.append(f"| {r.id} | {r.name} | {fmt(s)} | {'✓' if ok else ''} |")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root")
    ap.add_argument("--out", default="real/rules-sept.md")
    ap.add_argument("--candidates", default="real/rules-candidates.json")
    ap.add_argument("--final", help="candidates file: judge only those rules on --from..--to")
    ap.add_argument("--from", dest="d0", default="2026-09-01")
    ap.add_argument("--to", dest="d1", default="2026-09-25")
    ap.add_argument("--reps", type=int, default=20000)
    a = ap.parse_args(argv)
    d0, d1 = date.fromisoformat(a.d0), date.fromisoformat(a.d1)
    if a.final:
        final(a.root, a.final, a.out, d0, d1, a.reps)
    else:
        run(a.root, a.out, a.reps, d0, d1, a.candidates)


if __name__ == "__main__":
    main()
