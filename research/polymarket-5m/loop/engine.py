"""Turn a hypothesis' signal SQL into trades, fills and an exact p-value.

A signal query runs over the views `m` (markets) and `q` (quotes) of one split
and returns candidate decisions: event, slug, ts, side ('YES'|'NO'),
cost (price paid per share at that snapshot), y, fee_rate. The engine keeps
the first decision per event (events group the moneyline, spread, totals and
props of one game, whose outcomes are correlated), then fills it:

- fill="same": at the decision snapshot's price (optimistic: the snapshot is
  up to 2 minutes old by the time you could act);
- fill="next": at the market's next snapshot (<= 10 min later) if the price
  has not moved against us by more than SLIP; otherwise the trade is missed.

P&L per share = payout - price - fee_rate * price * (1 - price). The exact
p-value asks how often trades that each win with probability (price + fee)
would do at least this well (bot.evaluate.exact_p).
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot.evaluate import exact_p  # noqa: E402

SLIP = 0.01
NEXT_WITHIN_S = 600


def trades(con, signal_sql):
    first = f"""
        SELECT event, arg_min(struct_pack(ts := ts, slug := slug, side := side, cost := cost, y := y,
                                          fee_rate := fee_rate), (ts, slug)) AS t
        FROM ({signal_sql}) GROUP BY event"""
    return con.sql(f"""
        SELECT event, t.ts AS ts, t.slug AS slug, t.side AS side, t.cost AS cost, t.y AS y, t.fee_rate AS fee_rate
        FROM ({first})""").df()


def fill_next(con, tr):
    if tr.empty:
        return tr.assign(ts2=pd.NaT, cost2=np.nan)
    con.register("tr_df", tr[["event", "slug", "ts", "side"]])
    nxt = con.sql("""
        SELECT t.event, n.ts AS ts2, CASE WHEN t.side = 'YES' THEN n.ask ELSE 1 - n.bid END AS cost2
        FROM tr_df t ASOF LEFT JOIN q n ON t.slug = n.slug AND n.ts > t.ts""").df()
    con.unregister("tr_df")
    return tr.merge(nxt, on="event", how="left")


def score(tr, fill):
    """Per-trade P&L for one fill mode; returns (filled trades, fill rate)."""
    if tr.empty:
        return tr, float("nan")
    if fill == "same":
        f = tr.assign(price=tr["cost"])
    else:
        ok = (tr["cost2"] <= tr["cost"] + SLIP + 1e-9) & \
             ((tr["ts2"] - tr["ts"]).dt.total_seconds() <= NEXT_WITHIN_S)
        f = tr[ok.fillna(False)].assign(price=lambda d: d["cost2"])
    f = f[(f["price"] > 0) & (f["price"] < 1)]
    payout = np.where(f["side"] == "YES", f["y"], 1 - f["y"]).astype(float)
    fee = f["fee_rate"] * f["price"] * (1 - f["price"])
    f = f.assign(payout=payout, fee=fee, pnl=payout - f["price"] - fee)
    return f, len(f) / len(tr)


def stats(f, sims=50_000):
    if len(f) == 0:
        return dict(trades=0)
    cost = (f["price"] + f["fee"]).to_numpy()
    won = f["payout"].to_numpy() > 0.5
    wk = f.groupby(f["ts"].dt.isocalendar().week)["pnl"].sum()
    return dict(trades=len(f), win_rate=float(won.mean()), avg_cost=float(cost.mean()),
                pnl_per_share=float(f["pnl"].mean()), roi=float(f["pnl"].sum() / cost.sum()),
                p=float(exact_p(np.ones(len(f)), cost, won, sims=sims)),
                weeks=int(len(wk)), weeks_positive=float((wk > 0).mean()))


def evaluate(con, signal_sql, sims=50_000):
    tr = fill_next(con, trades(con, signal_sql))
    out = {"events": len(tr)}
    for fill in ("same", "next"):
        f, rate = score(tr, fill)
        out[fill] = dict(stats(f, sims), fill_rate=rate)
    return out
