"""Hypothesis families, registered BEFORE they are run on discovery data.

Each family is a parameter grid and a signal query (see engine.py). All grid
points count as tried. Promotion rule, fixed in advance: within a family,
among grid points with >= MIN_TRADES trades under fill="next", take the one
with the smallest discovery p; it goes to the holdout only if that p is
< PROMOTE_P and its P&L per share (fees and "next" fill) is > 0.
"""
from __future__ import annotations

import itertools

MIN_TRADES = 300
PROMOTE_P = 0.001

YES_NO = """
    SELECT q.ts, q.slug, m.event, m.y, m.fee_rate, m.sched, m.category,
           q.ask AS yes_cost, 1 - q.bid AS no_cost
    FROM q JOIN m USING (slug)"""

TIMING = {"any": "TRUE", "gameday": "CAST(ts AS DATE) = sched", "before": "CAST(ts AS DATE) < sched"}
CAT = {"sports": "category = 'sports'", "other": "category <> 'sports'"}


MAX_SPREAD = 0.03   # wider books have no meaningful favourite: bid 0.05 / ask 0.93 makes both sides cost > 0.9


def _side(fav):
    """fav=True: the side the mid favours (>= 0.5); fav=False: the other one. Cost is its ask."""
    yes = "(yes_cost + 1 - no_cost) / 2 >= 0.5" if fav else "(yes_cost + 1 - no_cost) / 2 < 0.5"
    return (f"CASE WHEN {yes} THEN 'YES' ELSE 'NO' END",
            f"CASE WHEN {yes} THEN yes_cost ELSE no_cost END")


def band(fav, lo, hi, timing, cat):
    side, cost = _side(fav)
    return f"""
        SELECT event, slug, ts, side, cost, y, fee_rate FROM (
            SELECT *, {side} AS side, {cost} AS cost FROM ({YES_NO}))
        WHERE cost >= {lo} AND cost < {hi} AND {TIMING[timing]} AND {CAT[cat]}
          AND yes_cost + no_cost - 1 <= {MAX_SPREAD}"""


def jump(d, mode, cat):
    """Mid moved >= d between consecutive snapshots (<= 5 min apart): fade or follow it."""
    buy_yes = "dmid < 0" if mode == "fade" else "dmid > 0"
    return f"""
        SELECT event, slug, ts, side, cost, y, fee_rate FROM (
            SELECT *, CASE WHEN {buy_yes} THEN 'YES' ELSE 'NO' END AS side,
                      CASE WHEN {buy_yes} THEN yes_cost ELSE no_cost END AS cost
            FROM (
                SELECT *, (yes_cost + 1 - no_cost) / 2
                          - lag((yes_cost + 1 - no_cost) / 2) OVER w AS dmid,
                          epoch(ts) - epoch(lag(ts) OVER w) AS dt
                FROM ({YES_NO}) WINDOW w AS (PARTITION BY slug ORDER BY ts)))
        WHERE abs(dmid) >= {d} AND dt <= 300 AND {CAT[cat]} AND cost > 0.02 AND cost < 0.98
          AND yes_cost + no_cost - 1 <= {MAX_SPREAD}"""


FAMILIES = {
    "H1_fav_band": dict(
        doc="买强势方：强势方价格第一次进入区间就买（偏爱冷门偏差的另一面）",
        grid=[dict(lo=lo, hi=hi, timing=t, cat=c)
              for (lo, hi), t, c in itertools.product(
                  [(0.60, 0.70), (0.70, 0.80), (0.80, 0.90), (0.90, 0.95), (0.95, 0.98), (0.98, 0.995)],
                  TIMING, CAT)],
        sql=lambda p: band(True, **p)),
    "H2_longshot": dict(
        doc="买弱势方（对照：文献预期亏钱）",
        grid=[dict(lo=lo, hi=hi, timing=t, cat=c)
              for (lo, hi), t, c in itertools.product([(0.01, 0.05), (0.05, 0.10), (0.10, 0.20), (0.20, 0.35)],
                                                      TIMING, CAT)],
        sql=lambda p: band(False, **p)),
    "H3_jump": dict(
        doc="相邻两次快照 mid 跳动 ≥ d：反向（fade）或顺势（follow）",
        grid=[dict(d=d, mode=mo, cat=c) for d, mo, c in itertools.product([0.10, 0.20, 0.30], ["fade", "follow"], CAT)],
        sql=lambda p: jump(**p)),
}

# Multi-leg families (scored by loop.arb, not engine): registered in iteration 1, run in iteration 2.
ARB_FAMILIES = {
    "H4_ladder_arb": dict(doc="同一场比赛的大小分/至少 k 次/让分阶梯被打破：买低线 YES + 高线 NO，至少赔付 1",
                          grid=[dict(margin=mg) for mg in (0.0, 0.01, 0.02, 0.05)]),
    "H5_set_arb": dict(doc="互斥集合（发现集上 ≥95% 恰好一个 YES 的问题模式）：YES 卖价和 < 1 全买，或 NO 成本和 < n−1 全买",
                       grid=[dict(margin=mg) for mg in (0.0, 0.01, 0.02, 0.05)]),
}

