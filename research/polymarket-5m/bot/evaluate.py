"""Score paper/replay trade logs and decide GO / NO-GO per variant.

    python -m bot evaluate paper.jsonl [more.jsonl ...] --gate gate.json --out report.md

Why not a t-value: these bets win 85-99% of the time, so a day with no
losses has near-zero sample variance and t explodes (§10 reported t=8 on 25
trades that a calibrated coin would produce 1 time in 9). The test here is
exact: under H0 each filled trade wins with its own break-even probability
(price + fee per share), and we ask how often H0 produces total P&L at
least as large as observed.
"""
from __future__ import annotations

import json
import math
import time
from collections import defaultdict

import numpy as np
from scipy.stats import norm

from .strategy import VARIANTS

# Days used to *choose* the variants are not evidence for them.
IN_SAMPLE_DAYS = ("2026-09-08",)

GATE = dict(alpha=0.05, min_trades=200, min_days=7, min_positive_day_share=0.55, max_age_days=14)


def load(paths):
    rows = []
    for p in paths:
        with open(p, encoding="utf-8") as f:
            rows += [json.loads(l) for l in f if l.strip()]
    # One record per (variant, market): a restarted run may log a market twice.
    uniq = {(r["variant"], r["slug"]): r for r in rows}
    return list(uniq.values())


def exact_p(filled, cost, won, sims=200_000, seed=7):
    """P(total P&L >= observed | every trade wins at its break-even rate)."""
    filled, cost = np.asarray(filled, float), np.asarray(cost, float)
    b = np.clip(cost / filled, 1e-9, 1 - 1e-9)
    obs = float(np.sum(filled * np.asarray(won, float) - cost))
    rng = np.random.default_rng(seed)
    hits, done = 0, 0
    while done < sims:
        k = min(20_000, sims - done)
        wins = rng.random((k, len(b))) < b
        s = (wins * filled).sum(axis=1) - cost.sum()
        hits += int((s >= obs - 1e-9).sum())
        done += k
    return (hits + 1) / (sims + 1)


def trades_needed(p_hat, b, alpha, power=0.8):
    """Trades for a one-sided binomial test at win rate p_hat vs break-even b."""
    if not (0 < b < 1) or p_hat <= b:
        return math.inf
    za, zb = norm.ppf(1 - alpha), norm.ppf(power)
    n = ((za * math.sqrt(b * (1 - b)) + zb * math.sqrt(p_hat * (1 - p_hat))) / (p_hat - b)) ** 2
    return math.ceil(n)


def day_bootstrap(per_day_pnl, per_day_shares, reps=5000, seed=11):
    d = len(per_day_pnl)
    if d < 2:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, d, (reps, d))
    pnl, sh = np.asarray(per_day_pnl)[idx].sum(1), np.asarray(per_day_shares)[idx].sum(1)
    x = pnl / np.where(sh > 0, sh, np.nan)
    return tuple(np.nanpercentile(x, [2.5, 97.5]))


def stats(rows, alpha_each):
    orders = len(rows)
    f = [r for r in rows if r["filled"] > 0]
    out = dict(orders=orders, filled=len(f), fill_rate=len(f) / orders if orders else float("nan"))
    if not f:
        return out
    filled = np.array([r["filled"] for r in f])
    cost = np.array([r["cost"] + r["fee"] for r in f])
    won = np.array([r["won"] for r in f], bool)
    pnl = filled * won - cost
    b = cost.sum() / filled.sum()
    p_hat = float((filled * won).sum() / filled.sum())
    days = defaultdict(lambda: [0.0, 0.0])
    for r, x, s in zip(f, pnl, filled):
        days[r["day"]][0] += x
        days[r["day"]][1] += s
    lo, hi = day_bootstrap([v[0] for v in days.values()], [v[1] for v in days.values()])
    curve = np.cumsum(pnl[np.argsort([r["end"] for r in f])])
    out.update(
        wins=int(won.sum()), win_rate=float(won.mean()), avg_cost=float((cost / filled).mean()),
        breakeven=float(b), pnl=float(pnl.sum()), pnl_per_share=float(pnl.sum() / filled.sum()),
        roi=float(pnl.sum() / cost.sum()), losses=int((~won).sum()),
        worst_loss=float(pnl.min()), max_drawdown=float((np.maximum.accumulate(np.r_[0, curve]) - np.r_[0, curve]).max()),
        days=len(days), positive_day_share=float(np.mean([v[0] > 0 for v in days.values()])),
        ci_per_share=(float(lo), float(hi)), p_value=exact_p(filled, cost, won),
        trades_needed=trades_needed(p_hat, b, alpha_each),
    )
    return out


def verdict(s, gate):
    reasons = []
    if s.get("filled", 0) < gate["min_trades"]:
        reasons.append(f"成交 {s.get('filled', 0)} 笔 < {gate['min_trades']}")
    if s.get("days", 0) < gate["min_days"]:
        reasons.append(f"只有 {s.get('days', 0)} 天 < {gate['min_days']}")
    if s.get("filled"):
        if s["p_value"] >= gate["alpha_each"]:
            reasons.append(f"p = {s['p_value']:.3f} ≥ {gate['alpha_each']:.3f}（Bonferroni）")
        if s["positive_day_share"] < gate["min_positive_day_share"]:
            reasons.append(f"盈利天数占比 {s['positive_day_share']:.0%}")
        if not s["ci_per_share"][0] > 0:
            reasons.append("按天自助法 95% 区间下沿 ≤ 0")
    return ("GO" if not reasons else "NO-GO"), reasons


def evaluate(paths, include_in_sample=False, gate=None):
    gate = dict(GATE, **(gate or {}))
    gate["alpha_each"] = gate["alpha"] / len(VARIANTS)
    rows = [r for r in load(paths) if include_in_sample or r["day"] not in IN_SAMPLE_DAYS]
    by_v = defaultdict(list)
    for r in rows:
        by_v[r["variant"]].append(r)
    result = {}
    for v in VARIANTS:
        s = stats(by_v.get(v.name, []), gate["alpha_each"])
        verdict_, reasons = verdict(s, gate)
        result[v.name] = dict(verdict=verdict_, reasons=reasons, stats=s, note=v.note)
    fills = {}
    for name, rs in by_v.items():
        hit = [r["won"] for r in rs if r["filled"] > 0]
        miss = [r["won"] for r in rs if r["filled"] == 0]
        fills[name] = (float(np.mean(hit)) if hit else None, float(np.mean(miss)) if miss else None, len(miss))
    return dict(generated_at=int(time.time()), gate=gate, include_in_sample=include_in_sample,
                days=sorted({r["day"] for r in rows}), variants=result, fills=fills)


def _fmt(x, pct=False, cents=False):
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "–"
    if pct:
        return f"{x * 100:.1f}%"
    if cents:
        return f"{x * 100:+.2f}¢"
    return f"{x}"


def markdown(res):
    g = res["gate"]
    lines = [f"数据日：{', '.join(res['days']) or '无'}（{'含' if res['include_in_sample'] else '不含'}选策略用的样本日）",
             f"门槛：每个变体 p < {g['alpha_each']:.3f}（{g['alpha']} / {len(VARIANTS)} 个变体），成交 ≥ {g['min_trades']} 笔，"
             f"≥ {g['min_days']} 天，盈利天数 ≥ {g['min_positive_day_share']:.0%}，按天自助法区间下沿 > 0。", "",
             "| 变体 | 结论 | 下单 | 成交 | 成交率 | 胜率 | 盈亏平衡胜率 | 每份净盈亏 | ROI | 亏损笔数 | 精确 p | 95% 区间（按天） | 还需成交 |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|"]
    for name, r in res["variants"].items():
        s = r["stats"]
        if not s.get("filled"):
            lines.append(f"| {name} | {r['verdict']} | {s['orders']} | 0 | – | – | – | – | – | – | – | – | – |")
            continue
        lo, hi = s["ci_per_share"]
        need = s["trades_needed"]
        lines.append(
            f"| {name} | {r['verdict']} | {s['orders']} | {s['filled']} | {_fmt(s['fill_rate'], pct=True)} | "
            f"{_fmt(s['win_rate'], pct=True)} | {_fmt(s['breakeven'], pct=True)} | {_fmt(s['pnl_per_share'], cents=True)} | "
            f"{s['roi'] * 100:+.2f}% | {s['losses']} | {s['p_value']:.3f} | "
            f"{'–' if not math.isfinite(lo) else f'{lo * 100:+.1f}¢ ~ {hi * 100:+.1f}¢'} | "
            f"{'∞（没有优势）' if not math.isfinite(need) else max(0, need - s['filled'])} |")
    lines.append("")
    for name, r in res["variants"].items():
        if r["reasons"]:
            lines.append(f"- `{name}` 未过：{'；'.join(r['reasons'])}")
    if res.get("fills"):
        lines += ["", "逆向选择检查：没成交的单子如果本来会赢得更多，说明回测里假设“按报价全成交”会高估收益。", "",
                  "| 变体 | 成交单胜率 | 未成交单（若成交）胜率 | 未成交单数 |", "|---|---:|---:|---:|"]
        for name, (wf, wm, nm) in res["fills"].items():
            lines.append(f"| {name} | {_fmt(wf, pct=True)} | {_fmt(wm, pct=True)} | {nm} |")
    return "\n".join(lines)
