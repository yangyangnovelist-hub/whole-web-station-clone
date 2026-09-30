"""Do Polymarket's BTC Up/Down markets of different lengths that end at the same moment price
the same final price consistently?

A 5-minute and a 15-minute market ending at the same time E both pay on the same settlement
price at E, against different reference prices (each market's own opening price). If the 15m
market's reference K15 is at or above the 5m market's K5, "15m Up" can only win when "5m Up"
also wins, so buying 5m Up and 15m Down pays at least 1 whatever happens (2 when the final
price lands between the two references). When the two asks plus fees cost less than 1, that is
an arbitrage; when they cost a little more, it can still be worth it for the chance of 2.

Source: the Hugging Face dataset whodisidk/polymarket-btc-updown-exchange-data (BTC 5m / 15m /
1h, 100 ms book snapshots, 2026-05-25 .. 08-29, daily tar archives). Runs where Hugging Face
is reachable (the polymarket-kacho workflow, "mode: cross").

    python cross.py probe --workdir /tmp/x --out real/cross-probe.md    # tables, schemas, samples
    python cross.py analyze 3 --workdir /tmp/x                         # the box on the last 3 days
    python cross.py analyze --workdir /tmp/x                           # ... on every day
    python cross.py stale 10 --workdir /tmp/x                          # stale 5m quotes after Binance moves

For every pair of markets ending together (5m-15m, 5m-1h, 15m-1h), at every 100 ms snapshot
while both trade, the cost of the box that pays at least 1 (Up on the lower reference, Down on
the higher one) at the best asks, with and without the taker fee. The realized payoff of every
box checks that the two markets really settle on the same price.
"""
from __future__ import annotations

import argparse
import io
import re
import tarfile
import urllib.request
from pathlib import Path

DS = "whodisidk/polymarket-btc-updown-exchange-data"
BASE = f"https://huggingface.co/datasets/{DS}/resolve/main/"


def fetch(name, dest=None):
    req = urllib.request.Request(BASE + name, headers={"User-Agent": "research"})
    with urllib.request.urlopen(req, timeout=600) as r:
        if dest is None:
            return r.read()
        with open(dest, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
    return dest


def archives(manifest):
    """(name, bytes) of the daily archives listed in MANIFEST.txt."""
    out = []
    for line in manifest.splitlines():
        m = re.search(r"(market_parquet_\d{4}-\d{2}-\d{2}\.tar\.gz)", line)
        size = re.findall(r"\b(\d{6,})\b", line)
        if m:
            out.append((m.group(1), int(size[-1]) if size else 0))
    return out


def describe_parquet(raw, rows=8):
    import pyarrow.parquet as pq
    pf = pq.ParquetFile(io.BytesIO(raw))
    t = pf.read_row_group(0) if pf.metadata.num_row_groups else pf.read()
    head = t.slice(0, rows).to_pandas()
    lines = [f"行数 {pf.metadata.num_rows:,}", "", "```", str(pf.schema_arrow)[:6000], "```", "", "```",
             head.to_string(max_colwidth=60)[:6000], "```"]
    df = t.to_pandas()
    for c in df.columns:  # distinct values of the small categorical columns
        if df[c].dtype == object and df[c].nunique() <= 12:
            lines.append(f"- `{c}`: {sorted(map(str, df[c].dropna().unique()))}")
    return lines


def probe(workdir, out):
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    L = [f"# 跨周期一致性：数据探查（{DS}）", ""]
    for name in ("README.md", "SCHEMA.md", "COVERAGE.md"):
        try:
            text = fetch(name).decode("utf-8", "replace")
        except Exception as e:  # keep going: the archive itself matters most
            text = f"({name} unavailable: {e!r})"
        L += [f"<details><summary>{name}</summary>", "", "```", text[:20000].replace("```", "'''"), "```", "",
              "</details>", ""]
    manifest = fetch("MANIFEST.txt").decode()
    arcs = archives(manifest)
    L += [f"{len(arcs)} 个日档：{arcs[0][0] if arcs else '-'} … {arcs[-1][0] if arcs else '-'}", ""]
    # the smallest archive from the last month keeps the download short
    last = [a for a in arcs if a[0] >= "market_parquet_2026-08"] or arcs
    name = min(last, key=lambda a: a[1] or 1 << 62)[0]
    local = fetch(name, workdir / name)
    L += [f"## {name}", ""]
    seen = set()
    with tarfile.open(local, "r:gz") as tar:
        members = [m for m in tar.getmembers() if m.isfile()]
        by_table = {}
        for m in members:
            t = re.search(r"dataset=([^/]+)", m.name)
            by_table.setdefault(t.group(1) if t else m.name.split("/")[0], []).append(m)
        for table, ms in sorted(by_table.items()):
            size = sum(m.size for m in ms)
            L += [f"### {table}：{len(ms)} 个文件，{size / 1e6:.1f} MB", ""]
            L += [f"- `{m.name}` {m.size / 1e6:.1f} MB" for m in ms[:6]]
            first = min(ms, key=lambda m: m.size if m.size > 0 else 1 << 62)
            raw = tar.extractfile(first).read()
            if first.name.endswith(".parquet") and table not in seen:
                seen.add(table)
                try:
                    L += [""] + describe_parquet(raw)
                except Exception as e:
                    L.append(f"(unreadable: {e!r})")
            elif first.name.endswith((".jsonl", ".jsonl.gz", ".json")):
                import gzip
                text = (gzip.decompress(raw) if first.name.endswith(".gz") else raw).decode("utf-8", "replace")
                L += ["", "```", "\n".join(text.splitlines()[:5])[:4000], "```"]
            L.append("")
    Path(local).unlink()
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L)[:30000])


FEAT_COLS = ["timestamp_ms", "market_id", "lifecycle_state", "up_best_bid", "up_best_ask", "down_best_bid",
             "down_best_ask", "up_ask_sizes", "down_ask_sizes"]
MKT_COLS = ["market_id", "slug", "session_start_ts", "session_end_ts", "chainlink_open_price", "up_won",
            "outcome_direction", "lifecycle_state"]


def read_day(path):
    """(features, markets, resolution rows) of one daily archive, streamed member by member."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    feats, mkts, res = [], [], []
    with tarfile.open(path, "r|gz") as tar:
        for m in tar:
            if not (m.isfile() and m.name.endswith(".parquet")):
                continue
            table = (re.search(r"dataset=([^/]+)", m.name) or [None, ""])[1]
            if table not in ("polymarket_features_100ms", "polymarket_market_100ms", "resolution"):
                continue
            t = pq.read_table(io.BytesIO(tar.extractfile(m).read()))
            if table == "polymarket_features_100ms":
                have = [c for c in FEAT_COLS if c in t.column_names]
                t = t.select(have)
                cols = {c: t[c] for c in have if not c.endswith("_sizes")}
                for c in ("up_ask_sizes", "down_ask_sizes"):  # size at the best ask
                    if c in have:
                        cols[c.replace("_sizes", "_size")] = pa.array([(v[0] if v else None) for v in t[c].to_pylist()],
                                                                      pa.float64())
                feats.append(pa.table({k: (v.cast(pa.string()) if k in ("market_id", "lifecycle_state") else v)
                                       for k, v in cols.items()}))
            elif table == "polymarket_market_100ms":
                have = [c for c in MKT_COLS if c in t.column_names]
                mkts.append(t.select(have).to_pandas())
            else:
                res.append(t.to_pandas())
    import pandas as pd
    f = pa.concat_tables(feats, promote_options="permissive").to_pandas() if feats else pd.DataFrame()
    mk = pd.concat(mkts, ignore_index=True) if mkts else pd.DataFrame()
    rs = pd.concat(res, ignore_index=True) if res else pd.DataFrame()
    return f, mk, rs


def read_binance(path):
    """Binance trades (trade_ts_ms, recv_ts_ms, price) of one daily archive."""
    import pandas as pd
    import pyarrow.parquet as pq
    parts = []
    with tarfile.open(path, "r|gz") as tar:
        for m in tar:
            if m.isfile() and "dataset=trades/" in m.name and m.name.endswith(".parquet"):
                t = pq.read_table(io.BytesIO(tar.extractfile(m).read()),
                                  columns=["trade_ts_ms", "recv_ts_ms", "exchange", "price"]).to_pandas()
                parts.append(t[t["exchange"].astype(str).str.lower() == "binance"])
    if not parts:
        return pd.DataFrame(columns=["trade_ts_ms", "recv_ts_ms", "price"])
    b = pd.concat(parts, ignore_index=True).drop(columns="exchange").drop_duplicates()
    return b.sort_values("recv_ts_ms", kind="stable").reset_index(drop=True)


def stale_trades(feat, mkts, binance, zs=(2.0, 3.0, 4.0), lags=(0, 100, 200, 300, 500, 1000, 2000, 5000)):
    """For each 5m market: the first Binance trade (as received) in the 240..60 s-left window whose
    price moved more than z sigma from the last trade at least a second earlier; buy that side at
    the ask of the first snapshot at or after receipt + L. Everything in the recorder's clock."""
    import numpy as np
    import pandas as pd
    if binance.empty:
        return pd.DataFrame()
    rt = binance["recv_ts_ms"].to_numpy()
    tt = binance["trade_ts_ms"].to_numpy()
    lp = np.log(binance["price"].to_numpy())
    order = np.argsort(tt, kind="stable")
    tt_s, lp_s = tt[order], lp[order]
    sec = tt_s // 1000
    last = pd.Series(lp_s, index=sec).groupby(level=0).last()
    grid = last.reindex(range(int(sec[0]), int(sec[-1]) + 1)).ffill()
    sigma = grid.diff().rolling(600, min_periods=300).std()
    m5 = mkts[(mkts["horizon"] == 5) & mkts["up_won"].notna()].set_index("market_id")
    by = {mid: g.drop_duplicates("timestamp_ms").sort_values("timestamp_ms")
          for mid, g in feat[feat["market_id"].isin(m5.index)].groupby("market_id")}
    rows = []
    for mid, mk in m5.iterrows():
        if mid not in by:
            continue
        a, b = np.searchsorted(rt, [mk["end"] - 240_000, mk["end"] - 60_000])
        if b <= a:
            continue
        j = np.searchsorted(tt_s, tt[a:b] - 1000, "right") - 1
        ref = np.where(j >= 0, lp_s[np.maximum(j, 0)], np.nan)
        sg = sigma.reindex(tt[a:b] // 1000 - 1).to_numpy()
        f = by[mid]
        fts = f["timestamp_ms"].to_numpy()
        for z in zs:
            with np.errstate(invalid="ignore"):
                hit = np.abs(lp[a:b] - ref) > z * sg
            if not hit.any():
                continue
            i = int(np.argmax(hit))
            t0, up = rt[a + i], lp[a + i] > ref[i]
            k0 = np.searchsorted(fts, t0, "left")
            a0 = np.nan if k0 >= len(f) else f["up_best_ask"].iloc[k0] if up else f["down_best_ask"].iloc[k0]
            for lag in lags:
                k = np.searchsorted(fts, t0 + lag, "left")
                if k >= len(f) or fts[k] > t0 + lag + 1000:
                    continue
                p = f["up_best_ask"].iloc[k] if up else f["down_best_ask"].iloc[k]
                size = f["up_ask_size"].iloc[k] if up else f["down_ask_size"].iloc[k]
                if not (np.isfinite(p) and 0.02 <= p <= 0.98):
                    continue
                won = float(mk["up_won"] == (1.0 if up else 0.0))
                fee = 0.07 * p * (1 - p)
                rows.append((mid, z, lag, t0, won, p, fee, won - p - fee, size, p <= a0 + 1e-9))
    return pd.DataFrame(rows, columns=["market_id", "z", "lag", "t0", "won", "price", "fee", "pnl", "size", "still"])


def stale(workdir, out, days=None, reps=5000):
    import numpy as np
    import pandas as pd
    import binary as bo
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    arcs = archives(fetch("MANIFEST.txt").decode())
    if days:
        arcs = arcs[-int(days):]
    parts = []
    for name, _ in arcs:
        try:
            local = fetch(name, workdir / name)
            feat, mk, rs = read_day(local)
            binance = read_binance(local)
            Path(local).unlink()
            t = stale_trades(feat, market_table(mk, rs), binance)
            t["day"] = name[15:25]
            parts.append(t)
            print(f"{name}: {len(binance):,} Binance trades, {t['market_id'].nunique() if len(t) else 0} markets", flush=True)
        except Exception as e:
            print(f"{name}: failed {e!r}", flush=True)
    df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    df.to_csv(Path(out).with_suffix(".csv.gz"), index=False)
    L = [f"# 币安跳变后 5m 卖一还挂多久（{DS}，逐笔币安、100 ms 盘口，记录机的时钟）", "",
         f"{len(arcs)} 个日档。触发：窗口剩 240–60 秒时，第一笔与至少一秒前最后一笔相比涨跌超过 zσ 的币安成交，"
         "以记录机收到它的时刻为 0；L 毫秒后按那一刻快照里的卖一买顺势一方，付 taker 费，持有到结算。探索性质。", ""]
    if df.empty:
        L.append("没有可用的数据。")
    for z, g in df.groupby("z") if not df.empty else []:
        L += [f"## z = {z:g}（{g['market_id'].nunique():,} 个市场触发）", "",
              "| L | 笔数 | 胜率 | 平均价 | EV/份 | p | 卖一数量中位 | 卖一没动的比例 |", "|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for lag, h in g.groupby("lag"):
            pv = bo.fair_price_pvalue(h["pnl"].to_numpy(), (h["price"] + h["fee"]).to_numpy(), sims=reps) \
                if len(h) >= 10 and h["pnl"].mean() > 0 else 1.0
            L.append(f"| {lag} ms | {len(h):,} | {h['won'].mean():.1%} | {h['price'].mean():.3f} | "
                     f"{100 * h['pnl'].mean():+.2f}¢ | {pv:.4f} | {h['size'].median():.0f} | {h['still'].mean():.0%} |")
        L.append("")
    Path(out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


def market_table(mk, rs):
    """One row per market: horizon, window, reference price and (if known) whether Up won."""
    import numpy as np
    import pandas as pd
    mk = mk.copy()
    mk["market_id"] = mk["market_id"].astype(str)
    g = mk.sort_values("session_end_ts").groupby("market_id")
    out = g.agg(slug=("slug", "last"), start=("session_start_ts", "last"), end=("session_end_ts", "last"),
                k=("chainlink_open_price", lambda x: x.dropna().iloc[-1] if x.notna().any() else np.nan))
    won = pd.Series(np.nan, index=out.index)
    if "up_won" in mk:
        w = mk.dropna(subset=["up_won"]).groupby("market_id")["up_won"].last()
        won.loc[w.index.intersection(won.index)] = w
    if not rs.empty:  # resolution records: take whatever column says who won
        rs = rs.copy()
        key = next((c for c in ("market_id", "condition_id", "slug") if c in rs), None)
        col = next((c for c in ("up_won", "outcome_direction", "winner", "winning_outcome") if c in rs), None)
        if key == "market_id" and col:
            v = rs.dropna(subset=[col]).groupby(rs["market_id"].astype(str))[col].last()
            v = v.map(lambda x: 1.0 if str(x).lower() in ("1", "1.0", "up", "true") else
                      0.0 if str(x).lower() in ("0", "0.0", "-1", "-1.0", "down", "false") else np.nan)
            fill = won.isna() & won.index.isin(v.index)
            won.loc[fill] = v.reindex(won.index[fill]).to_numpy()
    out["up_won"] = won
    out["horizon"] = ((out["end"] - out["start"]) / 60000).round().astype("Int64")
    return out.reset_index()


def boxes(feat, mkts, fee_rate=0.07):
    """Per pair of markets ending together: snapshots, cheapest box, how often it costs < 1."""
    import numpy as np
    import pandas as pd
    feat = feat[feat["lifecycle_state"].isin(["active", "open", "trading"]) | feat["lifecycle_state"].isna()]
    m = mkts.dropna(subset=["k"]).set_index("market_id")
    by = {mid: g.drop_duplicates("timestamp_ms").set_index("timestamp_ms") for mid, g in feat.groupby("market_id")}
    rows = []
    for end, grp in m.groupby("end"):
        grp = grp.sort_values("horizon")
        ids = list(grp.index)
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = m.loc[ids[i]], m.loc[ids[j]]  # a: shorter
                if a["horizon"] == b["horizon"]:
                    continue
                if ids[i] not in by or ids[j] not in by:
                    continue
                x = by[ids[i]].join(by[ids[j]], lsuffix="_a", rsuffix="_b", how="inner")
                x = x[(x.index >= a["start"]) & (x.index < a["end"])]
                if x.empty:
                    continue
                if b["k"] >= a["k"]:  # Up on the shorter (lower reference) + Down on the longer
                    p1, s1, p2, s2 = x["up_best_ask_a"], x["up_ask_size_a"], x["down_best_ask_b"], x["down_ask_size_b"]
                    pay_up = (1.0, 0.0)
                else:
                    p1, s1, p2, s2 = x["down_best_ask_a"], x["down_ask_size_a"], x["up_best_ask_b"], x["up_ask_size_b"]
                    pay_up = (0.0, 1.0)
                raw = p1 + p2
                cost = raw + fee_rate * p1 * (1 - p1) + fee_rate * p2 * (1 - p2)
                ok = cost.notna() & (p1 > 0) & (p2 > 0)
                if not ok.any():
                    continue
                cost, raw, size = cost[ok], raw[ok], np.fmin(s1[ok], s2[ok])
                cheap = cost < 1.0
                persist = cheap & cheap.shift(1, fill_value=False)  # still there 100 ms later
                i_best = cost.idxmin()
                pay = np.nan
                if np.isfinite(a["up_won"]) and np.isfinite(b["up_won"]):
                    first = a["up_won"] if pay_up == (1.0, 0.0) else 1 - a["up_won"]
                    second = 1 - b["up_won"] if pay_up == (1.0, 0.0) else b["up_won"]
                    pay = first + second
                pt = persist[persist].index
                rows.append({
                    "pair": f"{int(a['horizon'])}m-{int(b['horizon'])}m", "end": end, "gap": abs(b["k"] - a["k"]),
                    "n": int(ok.sum()), "min_cost": float(cost.min()), "min_raw": float(raw.min()),
                    "tau_best": (a["end"] - i_best) / 1000, "size_best": float(size.loc[i_best]),
                    "cheap_share": float(cheap.mean()), "n_persist": int(persist.sum()),
                    "persist_cost": float(cost.loc[pt[0]]) if len(pt) else np.nan,
                    "persist_size": float(size.loc[pt[0]]) if len(pt) else np.nan,
                    "persist_tau": (a["end"] - pt[0]) / 1000 if len(pt) else np.nan,
                    "payoff": pay})
    return pd.DataFrame(rows)


def analyze(workdir, out, days=None):
    import pandas as pd
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    arcs = archives(fetch("MANIFEST.txt").decode())
    if days:
        arcs = arcs[-int(days):]
    parts = []
    for name, _ in arcs:
        try:
            local = fetch(name, workdir / name)
            feat, mk, rs = read_day(local)
            Path(local).unlink()
            mkts = market_table(mk, rs)
            b = boxes(feat, mkts)
            b["day"] = name[15:25]
            parts.append(b)
            print(f"{name}: {len(mkts)} markets, {len(b)} pairs, "
                  f"cheap pairs {int((b['min_cost'] < 1).sum()) if len(b) else 0}", flush=True)
        except Exception as e:
            print(f"{name}: failed {e!r}", flush=True)
    df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    df.to_csv(Path(out).with_suffix(".csv"), index=False)
    L = [f"# 同时结束的 5m / 15m / 1h 市场：组合是否便宜到能套利（{DS}）", "",
         f"{len(arcs)} 个日档，{len(df):,} 对同时结束的市场。组合：参考价较低的那个市场买 Up、较高的那个买 Down，"
         "两边都按卖一成交；至少拿回 1，结算价落在两个参考价之间时拿回 2。成本含 taker 费 0.07·p(1−p)。", ""]
    if df.empty:
        L.append("没有可用的数据。")
    else:
        L += ["| 组合 | 对数 | 最便宜时成本中位 | 成本 < 1 的对（含费） | 不含费 < 1 | 100 ms 后仍 < 1 的对 | 这些对首个仍便宜时的平均利润 × 数量中位 | 结算核对：拿回 ≥ 1 的比例 |",
              "|---|---:|---:|---:|---:|---:|---:|---:|"]
        for pair, g in df.groupby("pair"):
            per = g.dropna(subset=["persist_cost"])
            chk = g.dropna(subset=["payoff"])
            L.append(f"| {pair} | {len(g):,} | {g['min_cost'].median():.3f} | {(g['min_cost'] < 1).sum():,} "
                     f"({(g['min_cost'] < 1).mean():.1%}) | {(g['min_raw'] < 1).sum():,} | {len(per):,} | "
                     f"{100 * (1 - per['persist_cost']).mean() if len(per) else float('nan'):+.2f}¢ × "
                     f"{per['persist_size'].median() if len(per) else float('nan'):.0f} | "
                     f"{(chk['payoff'] >= 1).mean() if len(chk) else float('nan'):.1%}（{len(chk):,} 对） |")
        L += ["", "按天（100 ms 后仍便宜的对数）：", "",
              "| 日期 | " + " | ".join(sorted(df["pair"].unique())) + " |", "|---|" + "---:|" * df["pair"].nunique()]
        for day, g in df.groupby("day"):
            L.append(f"| {day} | " + " | ".join(str(int(g[g["pair"] == p]["persist_cost"].notna().sum()))
                                               for p in sorted(df["pair"].unique())) + " |")
    Path(out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe")
    p.add_argument("--workdir", default="/tmp/cross")
    p.add_argument("--out", default="real/cross-probe.md")
    for name, default in (("analyze", "real/cross-boxes.md"), ("stale", "real/cross-stale.md")):
        p = sub.add_parser(name)
        p.add_argument("days", nargs="?", type=int, help="only the last N daily archives")
        p.add_argument("--workdir", default="/tmp/cross")
        p.add_argument("--out", default=default)
    a = ap.parse_args(argv)
    if a.cmd == "probe":
        probe(a.workdir, a.out)
    elif a.cmd == "analyze":
        analyze(a.workdir, a.out, a.days)
    else:
        stale(a.workdir, a.out, a.days)


if __name__ == "__main__":
    main()
