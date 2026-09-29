"""Multi-leg arbitrage families: H4 line ladders, H5 mutually exclusive sets.

H4: within one game, "total > a" can never be less likely than "total > b"
for a < b (same for "at least k", "above x", "cover -x"). If the lower line's
YES ask is below the higher line's YES bid, buying YES(a) and NO(b) pays at
least 1 whatever happens, for less than 1.
H5: when exactly one of a set of markets resolves YES (a tournament winner,
the temperature bucket), buying every YES for a total under 1 pays 1; if the
bids sum above 1, buying every NO pays n - 1 for less than that.

Legs are read from the same 2-minute poll bucket; "next" fill re-prices every
leg at its next snapshot and drops the bundle if any leg moved against us by
more than SLIP. P&L uses the official outcomes, so a mis-grouped ladder or an
incomplete "exclusive" set shows up as a loss, not as a free lunch.
"""
from __future__ import annotations

import math
import re

import numpy as np
import pandas as pd
from scipy.stats import t as student_t

from .engine import NEXT_WITHIN_S, SLIP

LADDER_RE = re.compile(r"^(?P<key>.*\d{4}-\d{2}-\d{2}-(?:.*-)?)(?:gte(?P<k>\d+)|gt(?P<g1>\d+)pt(?P<g2>\d+)|(?P<t1>\d+)pt(?P<t2>\d+))$")


def ladder_markets(con):
    """Markets that belong to a decreasing ladder: P(YES) falls as the line rises."""
    m = con.sql("SELECT slug, event, y, fee_rate FROM m").df()
    rows = []
    for r in m.itertuples():
        g = LADDER_RE.match(r.slug)
        if not g:
            continue
        prefix, key = r.slug.split("-", 1)[0], g["key"]
        if g["k"]:
            line = float(g["k"])
        elif g["g1"]:
            line = float(f"{g['g1']}.{g['g2']}")
        else:
            line = float(f"{g['t1']}.{g['t2']}")
        decreasing = (prefix == "tsc") or (prefix == "astatc" and (g["k"] or g["g1"])) or \
                     (prefix == "asc" and key.endswith("neg-"))
        if decreasing:
            rows.append((r.slug, key, r.event, line, r.y, r.fee_rate))
    lad = pd.DataFrame(rows, columns=["slug", "gkey", "event", "line", "y", "fee_rate"])
    n = lad.groupby("gkey")["slug"].transform("size")
    return lad[n >= 2].reset_index(drop=True)


def _buckets(con, table):
    return f"""
        SELECT slug, time_bucket(INTERVAL 2 MINUTE, ts) AS b,
               arg_max(struct_pack(ts := ts, bid := bid, ask := ask), ts) AS s
        FROM q WHERE slug IN (SELECT slug FROM {table}) GROUP BY 1, 2"""


def ladder_bundles(con, margin):
    lad = ladder_markets(con)
    con.register("lad", lad)
    df = con.sql(f"""
        WITH x AS (
            SELECT qb.b, l.*, qb.s.ts AS ts, qb.s.bid AS bid, qb.s.ask AS ask
            FROM ({_buckets(con, 'lad')}) qb JOIN lad l USING (slug)),
        pair AS (
            SELECT a.gkey, a.event, a.b, a.slug AS sa, c.slug AS sb, a.ts AS ta, c.ts AS tb,
                   a.ask AS pa, 1 - c.bid AS pb, a.y AS ya, c.y AS yb, a.fee_rate AS fr,
                   c.bid - a.ask - a.fee_rate * a.ask * (1 - a.ask) - c.fee_rate * c.bid * (1 - c.bid) AS edge
            FROM x a JOIN x c ON a.gkey = c.gkey AND a.b = c.b AND a.line < c.line
            WHERE c.bid - a.ask - a.fee_rate * a.ask * (1 - a.ask) - c.fee_rate * c.bid * (1 - c.bid) > {margin})
        SELECT gkey, arg_min(struct_pack(event := event, b := b, sa := sa, sb := sb, ta := ta, tb := tb,
                                          pa := pa, pb := pb, ya := ya, yb := yb, fr := fr), (b, -edge)) AS z
        FROM pair GROUP BY gkey""").df()
    con.unregister("lad")
    if df.empty:
        return []
    z = pd.json_normalize(df["z"])
    return [dict(event=r.event, ts=r.ta, legs=[(r.sa, "YES", r.ta, r.pa, r.ya, r.fr), (r.sb, "NO", r.tb, r.pb, r.yb, r.fr)])
            for r in z.itertuples()]


EXCLUSIVE_MIN_SHARE, EXCLUSIVE_MIN_SETS = 0.95, 5


def exclusive_patterns(con):
    """Question patterns whose sets resolve with exactly one YES in >= 95% of cases (discovery only)."""
    m = con.sql("SELECT event, question, y FROM m").df()
    g = m.groupby(["event", "question"]).agg(n=("y", "size"), ys=("y", "sum")).reset_index()
    g = g[g["n"] >= 2]
    g["pat"] = g["question"].str.replace(r"\d+", "#", regex=True)
    s = g.groupby("pat").agg(sets=("ys", "size"), one=("ys", lambda v: float((v == 1).mean())))
    return sorted(s[(s["sets"] >= EXCLUSIVE_MIN_SETS) & (s["one"] >= EXCLUSIVE_MIN_SHARE)].index)


def set_bundles(con, margin, patterns):
    m = con.sql("SELECT slug, event, question, y, fee_rate FROM m").df()
    m["pat"] = m["question"].str.replace(r"\d+", "#", regex=True)
    m = m[m["pat"].isin(patterns)].copy()
    m["gkey"] = m["event"] + "|" + m["question"]
    m["n"] = m.groupby("gkey")["slug"].transform("size")
    m = m[m["n"] >= 2]
    con.register("st", m[["slug", "gkey", "event", "n", "y", "fee_rate"]])
    df = con.sql(f"""
        WITH x AS (
            SELECT qb.b, s.*, qb.s.ts AS ts, qb.s.bid AS bid, qb.s.ask AS ask
            FROM ({_buckets(con, 'st')}) qb JOIN st s USING (slug)),
        agg AS (
            SELECT gkey, b, any_value(n) AS n, count(*) AS k,
                   sum(ask + fee_rate * ask * (1 - ask)) AS yes_cost,
                   sum(1 - bid + fee_rate * bid * (1 - bid)) AS no_cost,
                   list(struct_pack(slug := slug, ts := ts, bid := bid, ask := ask, y := y, fr := fee_rate)) AS legs
            FROM x GROUP BY gkey, b),
        opp AS (
            SELECT *, CASE WHEN yes_cost < 1 - {margin} THEN 'YES' ELSE 'NO' END AS side
            FROM agg WHERE k = n AND (yes_cost < 1 - {margin} OR no_cost < n - 1 - {margin}))
        SELECT gkey, arg_min(struct_pack(b := b, side := side, legs := legs), b) AS z FROM opp GROUP BY gkey""").df()
    con.unregister("st")
    out = []
    for r in df.itertuples():
        z = r.z
        legs = [(l["slug"], z["side"], l["ts"], l["ask"] if z["side"] == "YES" else 1 - l["bid"], l["y"], l["fr"])
                for l in z["legs"]]
        out.append(dict(event=r.gkey.split("|")[0], ts=min(l[2] for l in legs), legs=legs))
    return out


def fill_and_score(con, bundles):
    """Per-bundle P&L for fill='same' and fill='next'."""
    if not bundles:
        return pd.DataFrame()
    legs = pd.DataFrame([(i, *l) for i, b in enumerate(bundles) for l in b["legs"]],
                        columns=["bid_", "slug", "side", "ts", "price", "y", "fr"])
    con.register("legs_df", legs[["bid_", "slug", "side", "ts"]])
    nxt = con.sql("""
        SELECT l.bid_, l.slug, n.ts AS ts2, CASE WHEN l.side = 'YES' THEN n.ask ELSE 1 - n.bid END AS price2
        FROM legs_df l ASOF LEFT JOIN q n ON l.slug = n.slug AND n.ts > l.ts""").df()
    con.unregister("legs_df")
    legs = legs.merge(nxt, on=["bid_", "slug"], how="left")
    legs["payout"] = np.where(legs["side"] == "YES", legs["y"], 1 - legs["y"]).astype(float)
    ok = (legs["price2"] <= legs["price"] + SLIP + 1e-9) & \
         ((legs["ts2"] - legs["ts"]).dt.total_seconds() <= NEXT_WITHIN_S)
    legs["ok"] = ok.fillna(False)

    def pnl(price):
        return legs["payout"] - price - legs["fr"] * price * (1 - price)
    legs["pnl_same"], legs["pnl_next"] = pnl(legs["price"]), pnl(legs["price2"])
    g = legs.groupby("bid_").agg(pnl_same=("pnl_same", "sum"), pnl_next=("pnl_next", "sum"), ok=("ok", "all"),
                                 cost=("price", "sum"), legs=("slug", "size"))
    g["event"] = [bundles[i]["event"] for i in g.index]
    g["ts"] = [bundles[i]["ts"] for i in g.index]
    return g


def stats(g, col):
    if g.empty:
        return dict(trades=0)
    per_event = g.groupby("event")[col].sum()
    n = len(per_event)
    mu, sd = float(per_event.mean()), float(per_event.std(ddof=1)) if n > 1 else float("nan")
    p = float(student_t.sf(mu / (sd / math.sqrt(n)), n - 1)) if n > 1 and sd > 0 else (0.0 if mu > 0 else 1.0)
    return dict(trades=int(len(g)), events=n, pnl_per_bundle=float(g[col].mean()), losing_share=float((g[col] < 0).mean()),
                worst=float(g[col].min()), p=p)
