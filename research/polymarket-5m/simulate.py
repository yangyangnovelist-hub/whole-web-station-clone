"""Monte Carlo experiments behind README.md.

    python simulate.py                      # ~2 min, writes results.md
    python simulate.py --windows 40000 --out /tmp/quick.md

Two synthetic worlds (no real Polymarket data is used here):

  A  Gaussian paths with a random vol per window. The market quotes with the
     true vol times a lognormal error; the trader has an independent error.
     Isolates "is my vol forecast better than the market's" from everything else.
  B  Student-t(5) returns, jumps, clustered vol and scheduled high-vol events.
     The market quotes with a 30-minute EWMA of realised vol and ignores the
     event calendar; traders use a 10-minute EWMA plus the calendar, or the
     true diffusive vol as an upper bound.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

import numpy as np
from scipy.stats import norm, skew

import binary as bo

PRE = bo.TWAP_S          # seconds before the open whose TWAP sets the strike
T = bo.WINDOW_S
W = bo.TWAP_S
TAUS = (120, 60, 30)     # seconds left at the decision points
BASE_VOL = 0.45          # annualised
DAY = 24 * 3600 // T     # 288 windows per day per coin
BTC = 100_000            # only for turning log moves into illustrative dollars


# --------------------------------------------------------------------------- paths

def simulate_paths(sig_s, rng, df=None, jump_rate=0.0, jump_sd=0.0):
    """Log-price paths. Column j is second j - PRE + 1; column PRE - 1 is the open."""
    n, steps = len(sig_s), PRE + T
    if df is None:
        eps = rng.standard_normal((n, steps))
    else:
        eps = rng.standard_t(df, (n, steps)) / math.sqrt(df / (df - 2))
    r = eps * sig_s[:, None]
    if jump_rate:
        hit = rng.random((n, steps)) < jump_rate
        r[hit] += rng.normal(0.0, jump_sd, hit.sum())
    return np.cumsum(r, axis=1), r


def window_states(x):
    strike = x[:, :PRE].mean(axis=1)
    up = x[:, PRE + T - W:].mean(axis=1) >= strike
    xt, ks = {}, {}
    for tau in TAUS:
        t = T - tau
        xt[tau] = x[:, PRE - 1 + t]
        ks[tau] = x[:, PRE + T - W: PRE + t].sum(axis=1)
    return strike, up, xt, ks


def price(sig, strike, xt, ks, tau):
    return bo.prob_up(xt[tau], strike, sig, T - tau, ks[tau])


def world_a(n, rng, chunk=25_000):
    parts = []
    for start in range(0, n, chunk):
        m = min(chunk, n - start)
        sig = bo.per_second_vol(BASE_VOL * np.exp(0.35 * rng.standard_normal(m)))
        x, _ = simulate_paths(sig, rng)
        parts.append((sig, *window_states(x)))
    sig = np.concatenate([p[0] for p in parts])
    strike = np.concatenate([p[1] for p in parts])
    up = np.concatenate([p[2] for p in parts])
    xt = {tau: np.concatenate([p[3][tau] for p in parts]) for tau in TAUS}
    ks = {tau: np.concatenate([p[4][tau] for p in parts]) for tau in TAUS}
    return dict(sig=sig, strike=strike, up=up, xt=xt, ks=ks)


def world_b(n, rng, market_knows_events=False, burn=10):
    chains = max(1, n // 100)
    steps = n // chains + burn
    phi, sd_logvol = 0.95, 0.35
    event_p, event_mult = 1 / 96, 2.5
    lam_mkt, lam_fast = 0.5 ** (1 / 6), 0.5 ** (1 / 2)

    base_var = float(bo.per_second_vol(BASE_VOL)) ** 2
    logv = sd_logvol * rng.standard_normal(chains)
    var_mkt = np.full(chains, base_var)
    var_fast = np.full(chains, base_var)
    rows = {k: [] for k in ("p_mkt", "p_fast", "p_true_vol", "up", "event", "ratio")}
    rows["p_mkt"] = {tau: [] for tau in TAUS}
    rows["p_fast"] = {tau: [] for tau in TAUS}
    rows["p_true_vol"] = {tau: [] for tau in TAUS}

    for step in range(steps):
        logv = phi * logv + sd_logvol * math.sqrt(1 - phi**2) * rng.standard_normal(chains)
        event = rng.random(chains) < event_p
        sig = bo.per_second_vol(BASE_VOL * np.exp(logv)) * np.where(event, event_mult, 1.0)
        x, r = simulate_paths(sig, rng, df=5, jump_rate=1 / 7200, jump_sd=0.0015)
        strike, up, xt, ks = window_states(x)

        mult = np.where(event, event_mult, 1.0)
        sig_mkt = np.sqrt(var_mkt) * (mult if market_knows_events else 1.0)
        sig_fast = np.sqrt(var_fast) * mult
        if step >= burn:
            for tau in TAUS:
                rows["p_mkt"][tau].append(price(sig_mkt, strike, xt, ks, tau))
                rows["p_fast"][tau].append(price(sig_fast, strike, xt, ks, tau))
                rows["p_true_vol"][tau].append(price(sig, strike, xt, ks, tau))
            rows["up"].append(up)
            rows["event"].append(event)
            rows["ratio"].append(sig_fast / sig_mkt)

        rv = np.mean(r[:, PRE:] ** 2, axis=1)
        # Whoever knows the calendar deflates event windows out of the estimate.
        var_mkt = lam_mkt * var_mkt + (1 - lam_mkt) * rv / (mult**2 if market_knows_events else 1.0)
        var_fast = lam_fast * var_fast + (1 - lam_fast) * rv / mult**2

    out = {k: (np.concatenate(v) if isinstance(v, list) else
               {tau: np.concatenate(v[tau]) for tau in TAUS})
           for k, v in rows.items()}
    return out


# ---------------------------------------------------------------------- strategies

def book(p_mkt):
    bid_u, ask_u = bo.quote_book(p_mkt)
    return np.round(ask_u, 2), np.round(1.0 - bid_u, 2)  # Up ask, Down ask


def take(win, ask):
    return win.astype(float) - ask - bo.taker_fee(ask)


def sides(p_mkt, up):
    ask_u, ask_d = book(p_mkt)
    fav_up = p_mkt >= 0.5
    ask_f = np.where(fav_up, ask_u, ask_d)
    win_f = np.where(fav_up, up, ~up)
    ask_w = np.where(fav_up, ask_d, ask_u)
    return ask_f, win_f, ask_w, ~win_f


def fav_mask(ask_f):
    return (ask_f >= 0.80) & (ask_f <= 0.97)


def long_mask(ask_w):
    return (ask_w >= 0.03) & (ask_w <= 0.20)


def vol_edge(p_sig, p_mkt, up, theta=0.01):
    ask_u, ask_d = book(p_mkt)
    e_u = p_sig - ask_u - bo.taker_fee(ask_u)
    e_d = (1 - p_sig) - ask_d - bo.taker_fee(ask_d)
    buy_up = e_u >= e_d
    mask = np.maximum(e_u, e_d) > theta
    ask = np.where(buy_up, ask_u, ask_d)
    win = np.where(buy_up, up, ~up)
    return mask, ask, win


def stats(pnl, cost, n_windows):
    n = len(pnl)
    if n < 2:
        return dict(n=n, per_day=0.0, hit=np.nan, cost=np.nan, ev=np.nan, roi=np.nan, t=np.nan, skew=np.nan)
    sd = pnl.std(ddof=1)
    return dict(n=n, per_day=n / n_windows * DAY, hit=np.mean(pnl > 0), cost=cost.mean(),
                ev=pnl.mean(), roi=pnl.mean() / cost.mean(),
                t=pnl.mean() / (sd / math.sqrt(n)) if sd > 0 else np.nan, skew=skew(pnl))


def fmt_row(name, s):
    if s["n"] < 2:
        return f"| {name} | {s['n']} | – | – | – | – | – | – | – |"
    return (f"| {name} | {s['n']:,} | {s['per_day']:.1f} | {s['hit']*100:.1f}% | {s['cost']:.3f} | "
            f"{s['ev']*100:+.2f}¢ | {s['roi']*100:+.2f}% | {s['t']:+.1f} | {s['skew']:+.1f} |")


STATS_HEAD = ("| 策略 | 笔数 | 笔/天(单币) | 胜率 | 平均成本 | EV/份 | ROI | t 值 | 偏度 |\n"
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|")


# ---------------------------------------------------------------------- experiments

def exp_greeks():
    sig = float(bo.per_second_vol(BASE_VOL))
    tau = 120
    lines = ["| 距离(σ√τ) | 约合美元 | P(Up) | 10 秒后(θ) | 波动率 +10%(vega) | 波动率 −10% |",
             "|---:|---:|---:|---:|---:|---:|"]
    for d in (-2, -1, -0.5, 0, 0.5, 1, 2):
        p = norm.cdf(d)
        p_later = norm.cdf(d * math.sqrt(tau / (tau - 10)))
        p_up_vol, p_dn_vol = norm.cdf(d / 1.1), norm.cdf(d / 0.9)
        dollars = d * sig * math.sqrt(tau) * BTC
        lines.append(f"| {d:+.1f} | {dollars:+,.0f} | {p:.3f} | {(p_later-p)*100:+.2f}¢ | "
                     f"{(p_up_vol-p)*100:+.2f}¢ | {(p_dn_vol-p)*100:+.2f}¢ |")
    return "\n".join(lines)


def exp_fees():
    lines = ["| 买入价 | 手续费/份 | 占成本 | 盈亏平衡所需真实胜率 | 需要的概率优势（不含价差） |",
             "|---:|---:|---:|---:|---:|"]
    for p in (0.03, 0.05, 0.10, 0.30, 0.50, 0.70, 0.90, 0.95, 0.97, 0.99):
        fee = float(bo.taker_fee(p))
        lines.append(f"| {p:.2f} | {fee*100:.3f}¢ | {fee/p*100:.2f}% | {(p+fee)*100:.2f}% | "
                     f"+{fee*100:.2f} 个百分点 |")
    return "\n".join(lines)


def exp_twap():
    sig = float(bo.per_second_vol(BASE_VOL))
    usd = sig * BTC  # dollars per sqrt(second)
    cases = [
        ("还剩 90s，现价高于目标 $30", 210, 30, None),
        ("还剩 60s，现价高于目标 $30", 240, 30, None),
        ("还剩 30s，现价高于目标 $30，窗口前 30s 均价也高 $30", 270, 30, 30),
        ("还剩 30s，现价高于目标 $20，窗口前 30s 均价低 $40", 270, 20, -40),
        ("还剩 10s，现价低于目标 $15，窗口前 50s 均价高 $25", 290, -15, 25),
    ]
    lines = ["| 情形 | 欧式(现价 vs 目标) | 亚式(60s TWAP, 正确) | 差 |", "|---|---:|---:|---:|"]
    for name, t, spot_usd, window_avg_usd in cases:
        x_t = spot_usd / BTC
        known = 0.0 if window_avg_usd is None else (t - (T - W)) * window_avg_usd / BTC
        p_eu = float(bo.prob_up_european(x_t, 0.0, sig, T - t))
        p_tw = float(bo.prob_up(x_t, 0.0, sig, t, known))
        lines.append(f"| {name} | {p_eu:.3f} | {p_tw:.3f} | {(p_tw-p_eu)*100:+.1f}¢ |")
    head = (f"参数：年化波动率 {BASE_VOL:.0%}，BTC=${BTC:,}（仅用于换算美元），"
            f"即每秒 1σ≈${usd:.1f}，60 秒 1σ≈${usd*math.sqrt(60):.0f}，5 分钟 1σ≈${usd*math.sqrt(300):.0f}。")
    return head + "\n\n" + "\n".join(lines)


def exp_fair_market(a):
    n = len(a["up"])
    lines = [STATS_HEAD]
    gross = []
    for tau in TAUS:
        p = price(a["sig"], a["strike"], a["xt"], a["ks"], tau)
        ask_f, win_f, ask_w, win_w = sides(p, a["up"])
        m = fav_mask(ask_f)
        lines.append(fmt_row(f"买强势方(卖虚值) τ={tau}s", stats(take(win_f[m], ask_f[m]), ask_f[m], n)))
        mid_f = np.where(p >= 0.5, p, 1 - p)
        gross.append((f"买强势方 τ={tau}s", np.mean(win_f[m] - mid_f[m]), np.mean(ask_f[m] - mid_f[m]),
                      np.mean(bo.taker_fee(ask_f[m]))))
        if tau == 60:
            lm = long_mask(ask_w)
            lines.append(fmt_row("买弱势方(买虚值) τ=60s", stats(take(win_w[lm], ask_w[lm]), ask_w[lm], n)))
            mid_w = 1 - mid_f
            gross.append(("买弱势方 τ=60s", np.mean(win_w[lm] - mid_w[lm]), np.mean(ask_w[lm] - mid_w[lm]),
                          np.mean(bo.taker_fee(ask_w[lm]))))
            hedged = take(win_f[m], ask_f[m]) + 0.5 * take(win_w[m], ask_w[m])
            lines.append(fmt_row("买强势方 + 同时买 0.5 份对面当“保险”", stats(hedged, ask_f[m] + 0.5 * ask_w[m], n)))
            half = 0.5 * take(win_f[m], ask_f[m])
            lines.append(fmt_row("对照：只买 0.5 份强势方", stats(half, 0.5 * ask_f[m], n)))
    g = ["| 策略 | 毛 EV（按 mid 成交、无费） | 半价差成本 | 手续费 |", "|---|---:|---:|---:|"]
    for name, ev, spread, fee in gross:
        g.append(f"| {name} | {ev*100:+.2f}¢ | {spread*100:.2f}¢ | {fee*100:.2f}¢ |")
    return "\n".join(lines), "\n".join(g)


def exp_vol_edge_grid(a, rng):
    n = len(a["up"])
    mkt_errs, trd_errs = (0.05, 0.10, 0.20, 0.30), (0.0, 0.05, 0.10, 0.20)
    out = []
    for tau in (120, 60):
        lines = ["| 市场波动率误差 ↓ / 你的误差 → | " + " | ".join(f"{e:.0%}" for e in trd_errs) + " |",
                 "|---|" + "---:|" * len(trd_errs)]
        for sm in mkt_errs:
            p_mkt = price(a["sig"] * np.exp(sm * rng.standard_normal(n)), a["strike"], a["xt"], a["ks"], tau)
            cells = []
            for st in trd_errs:
                p_sig = price(a["sig"] * np.exp(st * rng.standard_normal(n)), a["strike"], a["xt"], a["ks"], tau)
                m, ask, win = vol_edge(p_sig, p_mkt, a["up"])
                s = stats(take(win[m], ask[m]), ask[m], n)
                cells.append(f"{s['ev']*100:+.2f}¢ / {s['roi']*100:+.1f}% / {s['per_day']:.0f}笔")
            lines.append(f"| {sm:.0%} | " + " | ".join(cells) + " |")
        out.append(f"**τ = {tau}s**（每格：EV/份 / ROI / 每天笔数）\n\n" + "\n".join(lines))
    return "\n\n".join(out)


def exp_world_b(b, label):
    n = len(b["up"])
    up, event = b["up"], b["event"]
    special = event | (b["ratio"] > 1.25)
    lines = [STATS_HEAD]
    tau = 60
    ask_f, win_f, ask_w, win_w = sides(b["p_mkt"][tau], up)
    fm, lm = fav_mask(ask_f), long_mask(ask_w)
    pf, pw = take(win_f, ask_f), take(win_w, ask_w)

    lines.append(fmt_row("买强势方 τ=60s（全部窗口）", stats(pf[fm], ask_f[fm], n)))
    lines.append(fmt_row("　└ 其中普通窗口", stats(pf[fm & ~event], ask_f[fm & ~event], n)))
    lines.append(fmt_row("　└ 其中事件窗口", stats(pf[fm & event], ask_f[fm & event], n)))
    hedged = pf + np.where(event, 0.5 * pw, 0.0)
    cost_h = ask_f + np.where(event, 0.5 * ask_w, 0.0)
    lines.append(fmt_row("买强势方 + 事件窗口买 0.5 份对面", stats(hedged[fm], cost_h[fm], n)))
    keep = fm & ~special
    lines.append(fmt_row("买强势方，特殊情况不做", stats(pf[keep], ask_f[keep], n)))
    ins = lm & special
    lines.append(fmt_row("特殊情况单独买弱势方（“保险”作为独立交易）", stats(pw[ins], ask_w[ins], n)))
    lines.append(fmt_row("买弱势方 τ=60s（全部窗口）", stats(pw[lm], ask_w[lm], n)))

    for sig_name, key in (("10 分钟 EWMA + 日历", "p_fast"), ("真实扩散波动率（上限）", "p_true_vol")):
        for t2 in (120, 60):
            m, ask, win = vol_edge(b[key][t2], b["p_mkt"][t2], up)
            pnl = take(win, ask)
            lines.append(fmt_row(f"波动率价差[{sig_name}] τ={t2}s", stats(pnl[m], ask[m], n)))
            otm = m & (ask < 0.5)
            lines.append(fmt_row("　└ 买虚值(做多波动率)", stats(pnl[otm], ask[otm], n)))
            lines.append(fmt_row("　└ 买实值(做空波动率)", stats(pnl[m & ~otm], ask[m & ~otm], n)))

    calib = ["| 市场价(τ=60s) | 样本 | 实际 Up 频率 | 偏差 |", "|---|---:|---:|---:|"]
    edges = [0, 0.03, 0.1, 0.2, 0.35, 0.5, 0.65, 0.8, 0.9, 0.97, 1.0001]
    p = b["p_mkt"][tau]
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (p >= lo) & (p < hi)
        if m.sum() > 50:
            calib.append(f"| {lo:.2f}–{min(hi, 1):.2f} | {m.sum():,} | {up[m].mean():.3f} | "
                         f"{(up[m].mean() - p[m].mean())*100:+.2f} 个百分点 |")
    return f"#### {label}\n\n" + "\n".join(lines) + "\n\n市场报价校准（τ=60s）：\n\n" + "\n".join(calib)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--windows", type=int, default=200_000)
    ap.add_argument("--seed", type=int, default=20260928)
    ap.add_argument("--out", default=str(Path(__file__).with_name("results.md")))
    args = ap.parse_args()
    rng = np.random.default_rng(args.seed)

    a = world_a(args.windows, rng)
    fair_tbl, gross_tbl = exp_fair_market(a)
    grid = exp_vol_edge_grid(a, rng)
    b_naive = exp_world_b(world_b(args.windows, rng), "市场不看事件日历")
    b_aware = exp_world_b(world_b(args.windows, rng, market_knows_events=True), "市场也看事件日历")

    doc = f"""# 模拟结果

由 `python simulate.py --windows {args.windows} --seed {args.seed}` 生成，全部为合成数据。
每个世界 {args.windows:,} 个 5 分钟窗口（≈{args.windows / DAY:,.0f} 天单币）。
成交假设：按一跳价差的盘口吃单，付 taker 费 `0.07·p·(1−p)`，每笔 1 份，持有到结算。

## 1. 二元期权的希腊值（欧式近似，τ=120s）

{exp_greeks()}

## 2. 手续费

{exp_fees()}

## 3. 60 秒 TWAP 结算：欧式定价 vs 亚式定价

{exp_twap()}

## 4. 世界 A：市场定价完全正确时

{fair_tbl}

EV 拆解：

{gross_tbl}

## 5. 世界 A：波动率预测优势能换多少钱

信号：你的公允价减去 ask 和手续费后 > 1¢ 才吃单。误差为对数波动率的标准差。

{grid}

## 6. 世界 B：肥尾 + 跳跃 + 波动率聚集 + 事件窗口

“特殊情况” = 事件窗口，或你的快速波动率估计比市场高 25% 以上。

{b_naive}

{b_aware}
"""
    Path(args.out).write_text(doc, encoding="utf-8")
    print(doc)


if __name__ == "__main__":
    main()
