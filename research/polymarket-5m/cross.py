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
    python cross.py gated --workdir /tmp/x                             # ... only when the fair jump is worth it
    python cross.py openmis --workdir /tmp/x                           # mispricing just after the open
    python cross.py hourly --workdir /tmp/x                            # 1h markets against the Binance candle
    python cross.py datasets                                           # newer datasets on Hugging Face

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


def set_dataset(ds):
    """Use another dataset of the same layout (e.g. whodisidk/polymarket-eth-updown-exchange-data)."""
    global DS, BASE
    DS, BASE = ds, f"https://huggingface.co/datasets/{ds}/resolve/main/"


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
             "down_best_ask", "up_ask_sizes", "down_ask_sizes", "up_bid_sizes", "down_bid_sizes", "observed_halt_flag"]
FEAT_COLS += ["up_side_asks", "down_side_asks", "up_side_bids", "down_side_bids"]
MKT_COLS = ["market_id", "slug", "session_start_ts", "session_end_ts", "chainlink_open_price", "up_won",
            "outcome_direction", "lifecycle_state", "up_token_id", "down_token_id"]
MKT_COLS += ["oracle_source", "chainlink_open_price_source", "resolution_price_source"]
MKT_COLS += ["open_boundary_source", "close_boundary_source", "open_boundary_fallback_used",
             "close_boundary_fallback_used", "usable_for_backtest", "resolution_consistency"]


def _ladder(prices, sizes, depth):
    """The `depth` cheapest asks of each row as (prices, sizes) column lists, cheapest first."""
    ps = [[None] * len(prices) for _ in range(depth)]
    ss = [[None] * len(prices) for _ in range(depth)]
    for r, (pr, sz) in enumerate(zip(prices, sizes)):
        if not pr or not sz:
            continue
        lv = sorted((p, q) for p, q in zip(pr, sz) if p is not None and q is not None and q > 0)[:depth]
        for i, (p, q) in enumerate(lv):
            ps[i][r], ss[i][r] = p, q
    return ps, ss


def _size_at_best(best, prices, sizes):
    """Displayed size at the scalar best price; None when the ladder cannot prove it."""
    out = []
    for target, level_prices, level_sizes in zip(best, prices, sizes):
        if target is None or not level_prices or not level_sizes:
            out.append(None)
            continue
        matched = [
            float(size)
            for price, size in zip(level_prices, level_sizes)
            if price is not None and size is not None
            and abs(float(price) - float(target)) <= 1e-9
            and float(size) > 0
        ]
        out.append(sum(matched) if matched else None)
    return out


def read_day(path, depth=0):
    """(features, markets, resolution rows) of one daily archive, streamed member by member. With
    `depth`, also the `depth` cheapest asks of both tokens (up_ask_p1.., up_ask_s1.., down_...)."""
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
                ladders = {}
                if depth:
                    for side in ("up", "down"):
                        pc, sc = f"{side}_side_asks", f"{side}_ask_sizes"
                        if pc in t.column_names and sc in t.column_names:
                            ladders[side] = _ladder(t[pc].to_pylist(), t[sc].to_pylist(), depth)
                t = t.select(have)
                cols = {
                    c: t[c]
                    for c in have
                    if not c.endswith("_sizes") and "_side_" not in c
                }
                for side, (ps, ss) in ladders.items():
                    for i in range(depth):
                        cols[f"{side}_ask_p{i + 1}"] = pa.array(ps[i], pa.float64())
                        cols[f"{side}_ask_s{i + 1}"] = pa.array(ss[i], pa.float64())
                for side in ("up", "down"):
                    for book_side in ("ask", "bid"):
                        pc = f"{side}_side_{book_side}s"
                        sc = f"{side}_{book_side}_sizes"
                        bc = f"{side}_best_{book_side}"
                        if pc in have and sc in have and bc in have:
                            values = _size_at_best(
                                t[bc].to_pylist(),
                                t[pc].to_pylist(),
                                t[sc].to_pylist(),
                            )
                        else:
                            values = [None] * len(t)
                        cols[f"{side}_{book_side}_size"] = pa.array(values, pa.float64())
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


def stale_trades(feat, mkts, binance, zs=(2.0, 3.0, 4.0), lags=(0, 100, 200, 300, 500, 1000, 2000, 5000),
                 horizons=(5, 15, 60)):
    """For each market (5m, 15m, 1h): the first Binance trade (as received) in the 240..60 s-left window whose
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
    m5 = mkts[mkts["horizon"].isin(horizons) & mkts["up_won"].notna()].set_index("market_id")
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
                rows.append((mid, int(mk["horizon"]), z, lag, t0, won, p, fee, won - p - fee, size, p <= a0 + 1e-9))
    return pd.DataFrame(rows, columns=["market_id", "horizon", "z", "lag", "t0", "won", "price", "fee", "pnl", "size",
                                       "still"])


def read_poly_trades(path):
    """Polymarket prints (token, receipt time, price, size, taker side) of one daily archive."""
    import pandas as pd
    import pyarrow.parquet as pq
    parts = []
    with tarfile.open(path, "r|gz") as tar:
        for m in tar:
            if m.isfile() and "dataset=trades/" in m.name and m.name.endswith(".parquet"):
                t = pq.read_table(io.BytesIO(tar.extractfile(m).read()),
                                  columns=["recv_ts_ms", "exchange", "instrument", "price", "size", "taker_side"]).to_pandas()
                parts.append(t[t["exchange"].astype(str).str.lower() == "polymarket"])
    if not parts:
        return pd.DataFrame(columns=["recv_ts_ms", "instrument", "price", "size", "taker_side"])
    t = pd.concat(parts, ignore_index=True).drop(columns="exchange").drop_duplicates()
    t["instrument"] = t["instrument"].astype(str)
    t["taker_side"] = t["taker_side"].astype(str).str.lower()
    return t.sort_values("recv_ts_ms", kind="stable").reset_index(drop=True)


MAKER_TAUS = (240, 180, 120, 90, 60, 45, 30)


def maker_orders(feat, mkts, trades, taus=MAKER_TAUS, latency_ms=200, cancel_s=10, binance=None, tag_z=1.5):
    """Resting buy orders in 5m markets, filled by a queue model on the real depth and prints.

    At `tau` seconds left the order reaches the book `latency_ms` after the decision, on the
    favourite or the underdog (by the Up mid then), either joining its best bid behind the size
    already there ("join") or one tick above it at the front ("improve", when that is still below
    the ask). It fills once sells printed at its price exceed the size that was ahead of it, or
    as soon as any sell prints below its price; cancellations ahead of it are never counted, so
    fills come late rather than early. Unfilled orders are cancelled `cancel_s` before the close.
    A fill pays no fee and is held to settlement. With `binance`, each fill is tagged `adverse`
    when Binance moved at least tag_z sigma against the order between 1 s and 0.1 s before the
    fill print (recorder clock): a maker who cancels on such moves would have avoided it."""
    import numpy as np
    import pandas as pd
    m5 = mkts[(mkts["horizon"] == 5) & mkts["up_won"].notna()].set_index("market_id")
    by = {mid: g.drop_duplicates("timestamp_ms").sort_values("timestamp_ms")
          for mid, g in feat[feat["market_id"].isin(m5.index)].groupby("market_id")}
    tr = {tok: (g["recv_ts_ms"].to_numpy(), g["price"].to_numpy(float), g["size"].to_numpy(float),
                (g["taker_side"] == "sell").to_numpy())
          for tok, g in trades.groupby("instrument")}
    if binance is not None and len(binance):
        b_rt = binance["recv_ts_ms"].to_numpy()
        b_lp = np.log(binance["price"].to_numpy())
        last = pd.Series(b_lp, index=binance["trade_ts_ms"].to_numpy() // 1000).groupby(level=0).last()
        b_grid = last.reindex(range(int(last.index.min()), int(last.index.max()) + 1)).ffill(limit=10)
        b_sig = b_grid.diff().rolling(600, min_periods=300).std()

    def adverse(fill_ms, up):
        """Binance move against a buy of Up (down) or of Down (up) over the second before a fill."""
        if binance is None or not len(binance):
            return np.nan
        i0, i1 = np.searchsorted(b_rt, [fill_ms - 1000, fill_ms - 100], "right")
        if i1 - i0 < 2:
            return np.nan
        sg = b_sig.get(int(fill_ms // 1000) - 1, np.nan)
        if not np.isfinite(sg) or sg <= 0:
            return np.nan
        move = (b_lp[i1 - 1] - b_lp[i0]) / sg
        return float((-move if up else move) >= tag_z)
    rows = []
    for mid, mk in m5.iterrows():
        if mid not in by or mk.get("up_token") is None:
            continue
        f = by[mid]
        fts = f["timestamp_ms"].to_numpy()
        for tau in taus:
            ta = mk["end"] - tau * 1000 + latency_ms
            k = np.searchsorted(fts, ta, "left")
            if k >= len(f) or fts[k] > ta + 1000:
                continue
            r = f.iloc[k]
            if not all(np.isfinite([r["up_best_bid"], r["up_best_ask"]])):
                continue
            fav_up = (r["up_best_bid"] + r["up_best_ask"]) / 2 >= 0.5
            for which in ("fav", "dog"):
                up = fav_up if which == "fav" else not fav_up
                bid, ask = (r["up_best_bid"], r["up_best_ask"]) if up else (r["down_best_bid"], r["down_best_ask"])
                q0 = r["up_bid_size"] if up else r["down_bid_size"]
                tok = mk["up_token"] if up else mk["down_token"]
                if not (np.isfinite(bid) and np.isfinite(ask) and 0.01 <= bid < ask):
                    continue
                won = float(mk["up_won"] == (1.0 if up else 0.0))
                t_ts, t_px, t_sz, t_sell = tr.get(tok, (np.array([]), np.array([]), np.array([]), np.array([], bool)))
                a, b = np.searchsorted(t_ts, [ta, mk["end"] - cancel_s * 1000], "right")
                for mode in ("join", "improve"):
                    price = bid if mode == "join" else round(bid + 0.01, 2)
                    if mode == "improve" and price >= ask - 1e-9:
                        continue
                    ahead = (q0 if np.isfinite(q0) else 0.0) if mode == "join" else 0.0
                    fill_t = np.nan
                    sells = t_sell[a:b]
                    px, sz, ts = t_px[a:b][sells], t_sz[a:b][sells], t_ts[a:b][sells]
                    through = np.flatnonzero(px < price - 1e-9)
                    at = np.flatnonzero(np.abs(px - price) < 1e-9)
                    cum = np.cumsum(sz[at]) if len(at) else np.array([])
                    i_at = at[np.argmax(cum > ahead)] if len(at) and (cum > ahead).any() else None
                    cands = [ts[i] for i in ([through[0]] if len(through) else []) + ([i_at] if i_at is not None else [])]
                    if cands:
                        fill_t = min(cands)
                    rows.append((mid, mk["end"], tau, which, mode, price, ahead, np.isfinite(fill_t),
                                 (fill_t - ta) / 1000 if np.isfinite(fill_t) else np.nan, won, won - price,
                                 adverse(fill_t, up) if np.isfinite(fill_t) else np.nan))
    return pd.DataFrame(rows, columns=["market_id", "end", "tau", "which", "mode", "price", "ahead", "filled",
                                       "fill_s", "won", "pnl", "adverse"])


BANDS = (0.0, 0.1, 0.3, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)


def makers(workdir, out, days=None, reps=5000):
    """Queue-model maker orders on every 5m market; cells chosen on the first half of the days
    (by date) are checked on the second half."""
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
            trades = read_poly_trades(local)
            binance = read_binance(local)
            Path(local).unlink()
            o = maker_orders(feat, market_table(mk, rs), trades, binance=binance)
            o["day"] = name[15:25]
            parts.append(o)
            print(f"{name}: {len(trades):,} Polymarket prints, {o['market_id'].nunique() if len(o) else 0} markets, "
                  f"fill rate {o['filled'].mean() if len(o) else float('nan'):.2f}", flush=True)
        except Exception:
            import traceback
            print(f"{name}: failed\n{traceback.format_exc()}", flush=True)
    df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    df.to_csv(Path(out).with_suffix(".csv.gz"), index=False)
    L = [f"# 排队挂单：普通人挂在队尾（或抢到队首）能不能赚（{DS}，5m 市场）", "",
         f"{len(arcs)} 个日档，{df['market_id'].nunique() if len(df) else 0:,} 个市场。剩 τ 秒时下一张 1 份的买单（200 ms 后到达），"
         "强势方或弱势方，排在买一已有的量后面（join）或高一个价位抢队首（improve）。以挂单价卖出的成交量超过排在前面的量、"
         "或有更低价的卖出成交才算成交；前面有人撤单不算，所以成交只会偏晚。收盘前 10 秒未成交就撤单。不付手续费，持有到结算。"
         "按日期前一半选格、后一半检验：前一半每份盈亏最好的格子（成交 ≥ 30 笔），在后一半成交里 EV > 0 且 p < 0.05/格子数才算通过。探索性质。", ""]
    if df.empty:
        L.append("没有可用的数据。")
    else:
        df["band"] = pd.cut(df["price"], BANDS)
        days_sorted = sorted(df["day"].unique())
        first = set(days_sorted[: len(days_sorted) // 2])
        df["half"] = np.where(df["day"].isin(first), "A", "B")
        cells = df.groupby(["mode", "which", "tau", "band"], observed=True)
        L += ["| 挂法 | 方向 | τ | 价位 | 下单 | 成交率 | 成交后胜率 | 平均价 | 每份盈亏（成交） | 每单盈亏（含未成交） | 前一半 | 后一半 |",
              "|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
        summary = []
        for key, g in cells:
            fl = g[g["filled"]]
            if len(fl) < 30:
                continue
            ea = fl[fl["half"] == "A"]["pnl"].mean()
            eb = fl[fl["half"] == "B"]["pnl"].mean()
            summary.append((key, len(g), fl, ea, eb))
            L.append(f"| {key[0]} | {key[1]} | {key[2]} | {key[3]} | {len(g):,} | {g['filled'].mean():.0%} | "
                     f"{fl['won'].mean():.1%} | {fl['price'].mean():.3f} | {100 * fl['pnl'].mean():+.2f}¢ | "
                     f"{100 * fl['pnl'].sum() / len(g):+.2f}¢ | {100 * ea:+.2f}¢ | {100 * eb:+.2f}¢ |")
        # Stage A of the cancel-on-move idea: fills with and without an adverse Binance move in the
        # second before them, maker rebate (0.2 x 0.07 x p(1-p), an upper bound) added. Fixed before
        # the run: if the untagged fills still lose 1c or more per share, cancel-on-move is dropped.
        fl = df[df["filled"]].copy()
        fl["rebate"] = 0.2 * 0.07 * fl["price"] * (1 - fl["price"])
        L += ["", "## 撤单能躲开多少：成交前一秒币安是否已经往不利方向动了 ≥ 1.5σ（加挂单返佣，返佣是上限）", "",
              "| 挂法 | 成交前一秒有不利跳动 | 成交 | 每份盈亏（含返佣） | 胜率 | 平均价 |", "|---|---|---:|---:|---:|---:|"]
        for (mode, tag), g in fl.dropna(subset=["adverse"]).groupby(["mode", "adverse"]):
            L.append(f"| {mode} | {'有' if tag else '没有'} | {len(g):,} | {100 * (g['pnl'] + g['rebate']).mean():+.2f}¢ | "
                     f"{g['won'].mean():.1%} | {g['price'].mean():.3f} |")
        untagged = fl[fl["adverse"] == 0]
        cells = untagged.groupby(["mode", "which", "tau", "band"], observed=True).filter(lambda g: len(g) >= 1000)
        ev = 100 * (cells["pnl"] + cells["rebate"]).mean() if len(cells) else float("nan")
        L += ["", f"事先定的放弃线：成交 ≥ 1,000 笔的格子里，没有不利跳动的成交合计每份 {ev:+.2f}¢ → "
                  f"{'≤ −1¢，放弃“价格一动就撤单”的挂单' if not ev > -1 else '> −1¢，值得做第二步（撤单规则的模拟）'}。"]
        chosen = sorted([x for x in summary if len(x[2][x[2]["half"] == "A"]) >= 30 and x[3] > 0],
                        key=lambda x: -x[3])[:10]
        L += ["", f"## 前一半选出的 {len(chosen)} 个格子在后一半的检验（Bonferroni ÷ {max(len(chosen), 1)}）", "",
              "| 挂法 | 方向 | τ | 价位 | 前一半 EV | 后一半成交 | 后一半 EV | p | 判定 |", "|---|---|---:|---|---:|---:|---:|---:|---|"]
        for key, n, fl, ea, eb in chosen:
            h = fl[fl["half"] == "B"]
            pv = bo.fair_price_pvalue(h["pnl"].to_numpy(), h["price"].to_numpy(), sims=reps) \
                if len(h) >= 10 and h["pnl"].mean() > 0 else 1.0
            ok = len(h) >= 10 and h["pnl"].mean() > 0 and pv < 0.05 / max(len(chosen), 1)
            L.append(f"| {key[0]} | {key[1]} | {key[2]} | {key[3]} | {100 * ea:+.2f}¢ | {len(h):,} | "
                     f"{100 * h['pnl'].mean():+.2f}¢ | {pv:.4f} | {'通过' if ok else '没通过'} |")
    Path(out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


HEALTH_ALIVE = (2000, 2000)  # ms: the recorded book must change this close before the signal and after the fill
HEALTH_STATE = ("up_best_bid", "up_best_ask", "down_best_bid", "down_best_ask", "up_ask_size", "down_ask_size",
                "up_bid_size", "down_bid_size")


def book_health(f):
    """(sane, changes) for one market's snapshots (sorted by time). The archives repeat the last
    observed state when nothing new arrives and keep crossed and incomplete quotes (dataset card),
    so a row's presence does not show the feed was live. `sane` marks rows that are not crossed on
    either side, whose two asks sum to at least 0.99, that are active and not halted; `changes`
    are the snapshot times at which any top-of-book price or size differed from the row before."""
    import numpy as np
    ub, ua = f["up_best_bid"].to_numpy(float), f["up_best_ask"].to_numpy(float)
    db, da = f["down_best_bid"].to_numpy(float), f["down_best_ask"].to_numpy(float)
    with np.errstate(invalid="ignore"):
        sane = ~(ub >= ua) & ~(db >= da) & ~(ua + da < 0.99)
    if "lifecycle_state" in f:
        ls = f["lifecycle_state"]
        sane &= (ls.isin(["active", "open", "trading"]) | ls.isna()).to_numpy(bool)
    if "observed_halt_flag" in f:
        sane &= ~f["observed_halt_flag"].fillna(False).astype(bool).to_numpy()
    st = f[[c for c in HEALTH_STATE if c in f]].to_numpy(float)
    same = (st[1:] == st[:-1]) | (np.isnan(st[1:]) & np.isnan(st[:-1]))
    moved = np.r_[True, ~same.all(axis=1)]
    return sane, f["timestamp_ms"].to_numpy()[moved]


def changed_near(changes, x, before, after):
    """Whether the recorded book changed within `before` ms up to x and within `after` ms after it."""
    import numpy as np
    i = int(np.searchsorted(changes, x, "right"))
    return i > 0 and x - changes[i - 1] <= before and i < len(changes) and changes[i] - x <= after


GATE_THETAS = (0.0, 0.02, 0.04, 0.06, 0.08, 0.12)
GATE_LAGS = (300, 500)


DEPTH_THETAS = (0.12, 0.08, 0.04)  # the edge each deeper ask must still have to be taken


def gated_trades(feat, mkts, binance, z0=2.0, lags=GATE_LAGS, thetas=GATE_THETAS, tau_lo=15, horizon=5, tau_hi=None,
                 health=False, trades=None, anchor_ms=None, depth=0):
    """Stale-ask sniping gated by the fair-value jump a Binance move implies (`horizon`-minute
    markets, all settled on the 60 s TWAP).

    Every Binance trade (as received) with tau_hi..tau_lo s left (tau_hi defaults to all but the
    first minute: 240 s for 5m, 840 s for 15m) whose log price moved more than z0
    sigma from the last trade at least a second earlier is a candidate. The market's own Up mid
    just before it (the snapshot at receipt) is taken as the prior probability P0; the move shifts
    the expected settlement TWAP by the whole move, so the fair Up price becomes
    Phi(Phi^-1(P0) + dx / (sigma * twap_std_factor(t))). The side of the move is bought at the
    ask L ms after receipt when fair - ask - fee >= theta; the first such trade per market and
    (L, theta) is kept. With `health`, the quote at the print and the fill must be sane rows of a book
    that changed within HEALTH_ALIVE before the print and after the fill (see book_health). With
    Polymarket `trades`, each row also has the competition for that ask: the side's ask and size
    shown at the print (ask0, size0), and the shares other takers bought of that token at or below
    the fill price in the lag before our order (taken_before) and in the 300 ms after it (taken_next).
    With `anchor_ms`, the prior is the Up mid anchor_ms before the print instead (the snapshot within
    a second of it) and the move is the Binance price at the print against the last trade received
    by then, so a quote that already followed Binance is not counted twice. With `depth` (and the
    ladders of read_day(depth=...)), each row also has, for every theta_d in DEPTH_THETAS, the shares,
    cost and profit of taking every one of those asks with fair - price - fee >= theta_d at the fill
    (sh_d12, cost_d12, pnl_d12, ...; the cheapest is the trade itself, theta_d below theta deepens it)."""
    import numpy as np
    import pandas as pd
    from scipy.stats import norm
    import binary as bo
    if binance.empty:
        return pd.DataFrame()
    rt = binance["recv_ts_ms"].to_numpy()
    tt = binance["trade_ts_ms"].to_numpy()
    lp = np.log(binance["price"].to_numpy())
    order = np.argsort(tt, kind="stable")
    tt_s, lp_s = tt[order], lp[order]
    last = pd.Series(lp_s, index=tt_s // 1000).groupby(level=0).last()
    grid = last.reindex(range(int(last.index[0]), int(last.index[-1]) + 1)).ffill(limit=10)
    sigma = grid.diff().rolling(600, min_periods=300).std()
    window = 60 * int(horizon)
    tau_hi = window - 60 if tau_hi is None else tau_hi
    m5 = mkts[(mkts["horizon"] == horizon) & mkts["up_won"].notna()].set_index("market_id")
    by = {mid: g.drop_duplicates("timestamp_ms").sort_values("timestamp_ms")
          for mid, g in feat[feat["market_id"].isin(m5.index)].groupby("market_id")}
    buys = {}
    if trades is not None and len(trades):
        tb = trades[trades["taker_side"] == "buy"]
        buys = {tok: (g["recv_ts_ms"].to_numpy(), g["price"].to_numpy(float), g["size"].to_numpy(float))
                for tok, g in tb.groupby("instrument")}

    def taken(tok, lo, hi, px):
        if tok not in buys:
            return np.nan
        r, pr, sz = buys[tok]
        a_, b_ = np.searchsorted(r, [lo, hi], "right")
        return float(sz[a_:b_][pr[a_:b_] <= px + 1e-9].sum())

    rows = []
    for mid, mk in m5.iterrows():
        if mid not in by:
            continue
        f = by[mid]
        fts = f["timestamp_ms"].to_numpy()
        ub, ua = f["up_best_bid"].to_numpy(), f["up_best_ask"].to_numpy()
        da, uas, das = f["down_best_ask"].to_numpy(), f["up_ask_size"].to_numpy(), f["down_ask_size"].to_numpy()
        lad = {side: (np.column_stack([f[f"{side}_ask_p{i + 1}"].to_numpy(float) for i in range(depth)]),
                      np.column_stack([f[f"{side}_ask_s{i + 1}"].to_numpy(float) for i in range(depth)]))
               for side in ("up", "down")} if depth and "up_ask_p1" in f else {}
        if health:
            sane, changes = book_health(f)
        a, b = np.searchsorted(rt, [mk["end"] - tau_hi * 1000, mk["end"] - tau_lo * 1000])
        if b <= a:
            continue
        j = np.searchsorted(tt_s, tt[a:b] - 1000, "right") - 1
        ok = (j >= 0) & (tt[a:b] - tt_s[np.maximum(j, 0)] <= 5000)
        dx = np.where(ok, lp[a:b] - lp_s[np.maximum(j, 0)], np.nan)
        sg = sigma.reindex(tt[a:b] // 1000 - 1).to_numpy()
        with np.errstate(invalid="ignore"):
            cand = np.flatnonzero(np.abs(dx) > z0 * sg)
        done = set()
        for i in cand:
            t0 = rt[a + i]
            k0 = np.searchsorted(fts, t0, "right") - 1  # the quote shown when the print arrived
            if k0 < 0 or t0 - fts[k0] > 1000 or not (np.isfinite(ub[k0]) and np.isfinite(ua[k0])):
                continue
            if health and not (sane[k0] and changed_near(changes, t0, HEALTH_ALIVE[0], 10**12)):
                continue
            p0 = min(max((ub[k0] + ua[k0]) / 2, 0.005), 0.995)
            t_mkt = (t0 - (mk["end"] - window * 1000)) / 1000
            fac = float(bo.twap_std_factor(t_mkt, window=window)) * sg[i]
            if not (np.isfinite(fac) and fac > 0):
                continue
            prior, move = p0, dx[i]
            if anchor_ms is not None:
                ka = np.searchsorted(fts, t0 - anchor_ms, "right") - 1
                ja = np.searchsorted(rt, t0 - anchor_ms, "right") - 1
                if ka < 0 or t0 - anchor_ms - fts[ka] > 1000 or ja < 0 or not (np.isfinite(ub[ka]) and np.isfinite(ua[ka])):
                    continue
                prior, move = min(max((ub[ka] + ua[ka]) / 2, 0.005), 0.995), lp[a + i] - lp[ja]
            p1 = float(norm.cdf(norm.ppf(prior) + move / fac))
            up = dx[i] > 0
            fair = p1 if up else 1 - p1
            won = float(mk["up_won"] == (1.0 if up else 0.0))
            for lag in lags:
                k = np.searchsorted(fts, t0 + lag, "left")
                if k >= len(f) or fts[k] > t0 + lag + 1000:
                    continue
                if health and not (sane[k] and changed_near(changes, fts[k], 10**12, HEALTH_ALIVE[1])):
                    continue
                px = ua[k] if up else da[k]
                size = uas[k] if up else das[k]
                if not (np.isfinite(px) and 0.02 <= px <= 0.98):
                    continue
                fee = 0.07 * px * (1 - px)
                edge = fair - px - fee
                for th in thetas:
                    if (lag, th) in done or edge < th:
                        continue
                    done.add((lag, th))
                    tok = str(mk["up_token"] if up else mk["down_token"]) if "up_token" in mk else None
                    km2 = np.searchsorted(fts, t0 - 2000, "right") - 1  # the quote two seconds before the print
                    mid_m2 = (ub[km2] + ua[km2]) / 2 if km2 >= 0 and t0 - 2000 - fts[km2] <= 1000 else np.nan
                    pre = (p0 - mid_m2) if up else (mid_m2 - p0)  # how far the mid had already moved our way
                    deep = []
                    if lad:
                        lp_, ls_ = lad["up" if up else "down"]
                        lpk, lsk = lp_[k], ls_[k]
                        e = fair - lpk - 0.07 * lpk * (1 - lpk)
                        for thd in DEPTH_THETAS:
                            take = np.isfinite(lpk) & np.isfinite(lsk) & (e >= thd)
                            deep += [float(lsk[take].sum()), float((lsk * (lpk + 0.07 * lpk * (1 - lpk)))[take].sum()),
                                     float((lsk * (won - lpk - 0.07 * lpk * (1 - lpk)))[take].sum())]
                    rows.append((mid, lag, th, t0, (mk["end"] - t0) / 1000, p0, fair, px, size, fee, won, won - px - fee,
                                 ua[k0] if up else da[k0], uas[k0] if up else das[k0],
                                 taken(tok, t0, t0 + lag, px), taken(tok, t0 + lag, t0 + lag + 300, px), pre, *deep))
    cols = ["market_id", "lag", "theta", "t0", "tau", "p0", "fair", "price", "size", "fee",
            "won", "pnl", "ask0", "size0", "taken_before", "taken_next", "pre_move"]
    if depth and rows and len(rows[0]) > len(cols):
        cols += [f"{k}_d{round(100 * thd)}" for thd in DEPTH_THETAS for k in ("sh", "cost", "pnl")]
    return pd.DataFrame(rows, columns=cols)


def gated(workdir, out, days=None, reps=5000, horizon=5, health=False, compete=False, anchor=None, depth=0):
    """gated_trades on every day (`horizon`-minute markets); thresholds are compared on May 25 - Jul 15 and checked on the
    later days (Jul 16 - Aug 16, taker delay 250 ms; Aug 17 onwards, 50 ms)."""
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
            feat, mk, rs = read_day(local, depth=depth)
            binance = read_binance(local)
            prints = read_poly_trades(local) if compete else None
            Path(local).unlink()
            t = gated_trades(feat, market_table(mk, rs), binance, horizon=horizon, health=health, trades=prints,
                             anchor_ms=anchor, depth=depth)
            t["day"] = name[15:25]
            parts.append(t)
            print(f"{name}: {len(t):,} gated trades over {t['market_id'].nunique() if len(t) else 0} markets", flush=True)
        except Exception:
            import traceback
            print(f"{name}: failed\n{traceback.format_exc()}", flush=True)
    df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    df.to_csv(Path(out).with_suffix(".csv.gz"), index=False)
    L = [f"# 按公平价跳变筛选的过期报价（{DS}，{horizon}m 市场，记录机时钟）", "",
         f"候选：剩 {60 * horizon - 60}–15 秒时，与至少一秒前相比涨跌超过 2σ 的每一笔币安成交（记录机收到时）。以收到那一刻的 Up 中间价为原来的概率 P0，"
         "这次涨跌让结算 TWAP 的期望整体移动，新的公平价 = Φ(Φ⁻¹(P0) + Δx / (σ·TWAP 标准差系数))。L 毫秒后按卖一买顺势一方，"
         "只在 公平价 − 卖一 − taker 费 ≥ θ 时买，每个市场、每组 (L, θ) 只取第一笔，持有到结算。"
         "5 月 25 日–7 月 15 日用来比较门槛，之后的日子只用来核对。探索性质。", ""]
    if health:
        L += [f"本次加了盘口健康检查（`book_health`）：触发时和成交时的快照不能是交叉盘、两边卖一之和不低于 0.99、"
              f"处于交易状态且未暂停，并且盘口在触发前 {HEALTH_ALIVE[0] / 1000:g} 秒内和成交后 {HEALTH_ALIVE[1] / 1000:g} 秒内"
              "确实变过（数据集在没有新消息时会重复上一个状态，所以只有快照存在不能说明行情在更新）。"
              "用来检验原结果是不是记录机断流造成的假象；门槛不重新选。", ""]
    if anchor:
        L += [f"**本次换了起点**：原来的概率取触发前 {anchor / 1000:g} 秒的 Up 中间价（那一刻前后一秒内的快照），"
              f"Δx 取触发时币安价格相对 {anchor / 1000:g} 秒前最后一笔的涨跌。Polymarket 已经跟着币安动过的部分不会再算一遍，"
              "动得不够或动过头的部分才是优势。触发条件（一秒 2σ）不变。门槛在 5 月 25 日–7 月 15 日比较。", ""]
    if df.empty:
        L.append("没有可用的数据。")
    else:
        df["period"] = np.select([df["day"] <= "2026-07-15", df["day"] <= "2026-08-16"],
                                 ["A 5/25–7/15", "B 7/16–8/16"], "C 8/17–8/29")
        L += ["| L | θ | 时段 | 笔数 | 胜率 | 平均价 | 平均公平价 | EV/份 | p | 卖一数量中位 | 每天（按卖一数量，上限 500）|",
              "|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for (lag, th, per), g in df.groupby(["lag", "theta", "period"]):
            pv = bo.fair_price_pvalue(g["pnl"].to_numpy(), (g["price"] + g["fee"]).to_numpy(), sims=reps) \
                if len(g) >= 10 and g["pnl"].mean() > 0 else 1.0
            ndays = g["day"].nunique()
            usd = (g["pnl"] * g["size"].clip(upper=500)).sum() / max(ndays, 1)
            L.append(f"| {lag} ms | {100 * th:.0f}¢ | {per} | {len(g):,} | {g['won'].mean():.1%} | {g['price'].mean():.3f} | "
                     f"{g['fair'].mean():.3f} | {100 * g['pnl'].mean():+.2f}¢ | {pv:.4f} | {g['size'].median():.0f} | ${usd:,.0f} |")
        if "pre_move" in df:
            c = df[(df["lag"] == 300) & (df["theta"] == 0.12)].copy()
            c["pm"] = np.select([c["pre_move"] >= 0.03, c["pre_move"] <= -0.03], ["已朝买的方向动 ≥ 3¢", "反方向动 ≥ 3¢"],
                                "动不到 3¢")
            L += ["", "## 触发前两秒，Polymarket 中间价已经朝买的方向动了多少（L = 300 ms、θ = 12¢）", "",
                  "实盘检验 D（Coinbase 触发）几乎每一笔都是已经动过的；如果币安确实领先，回测里（币安触发）这种情况应该少得多。", "",
                  "| 时段 | 触发前已动 | 笔数 | 占比 | 胜率 | 平均价 | EV/份 |", "|---|---|---:|---:|---:|---:|---:|"]
            for (per, pm), g in c.groupby(["period", "pm"]):
                L.append(f"| {per} | {pm} | {len(g):,} | {len(g) / (c['period'] == per).sum():.0%} | {g['won'].mean():.1%} | "
                         f"{g['price'].mean():.3f} | {100 * g['pnl'].mean():+.2f}¢ |")
        if "sh_d12" in df:
            c = df[(df["lag"] == 300) & (df["theta"] == 0.12)].copy()
            L += ["", f"## 吃深几档（L = 300 ms、θ = 12¢，读前 {depth} 档卖单）", "",
                  "触发条件不变（第一档 公平价 − 卖一 − 费 ≥ 12¢）。成交时把 公平价 − 价格 − 费 ≥ θd 的每一档都吃下：θd = 12¢ 是同样的门槛，"
                  "8¢、4¢ 是放宽给更深的档。份数是中位；每份是全部份数的平均盈亏；每天的钱按全部份数（不设上限）和每笔最多 200 份两种算。"
                  "这里不扣别人同时去抢的部分（九月实测约少拿 22%）。", "",
                  "| 规则 | 时段 | 笔数 | 只吃第一档：份数 / 每份 / 每天 | θd 12¢：份数 / 每份 / 每天 | θd 8¢ | θd 4¢ | θd 4¢、每笔 ≤ 200 份每天 |",
                  "|---|---|---:|---|---|---|---|---:|"]
            c["G"] = c["pre_move"] < 0.03
            for rule, g0 in (("F（全部）", c), ("G（还没动）", c[c["G"]])):
                for per, g in g0.groupby("period"):
                    nd = max(g["day"].nunique(), 1)
                    cells = [f"{g['size'].median():.0f} / {100 * g['pnl'].mean():+.1f}¢ / ${(g['pnl'] * g['size']).sum() / nd:,.0f}"]
                    for d_ in (12, 8, 4):
                        sh, pn = g[f"sh_d{d_}"], g[f"pnl_d{d_}"]
                        cells.append(f"{sh.median():.0f} / {100 * pn.sum() / max(sh.sum(), 1):+.1f}¢ / ${pn.sum() / nd:,.0f}")
                    capped = (g["pnl_d4"] * np.minimum(1.0, 200 / g["sh_d4"].clip(lower=1))).sum() / nd
                    L.append(f"| {rule} | {per} | {len(g):,} | " + " | ".join(cells) + f" | ${capped:,.0f} |")
        if compete and "taken_before" in df:
            L += ["", "## 抢单：同一价位别人买走了多少（L = 300 ms、θ = 12¢）", "",
                  "卖一0 / 数量0：触发那一刻看到的卖一和挂单量；前：触发到我们的单到达之间，别的吃单在不高于成交价的价位买走的份数；"
                  "后：我们到达之后 300 ms 内别人又买走的份数；剩：我们到达时还挂着的份数（回测按这个成交）。中位数。", "",
                  "| 时段 | 笔数 | 数量0 | 前 | 后 | 剩 | 前 > 0 的比例 | 后 ≥ 剩 的比例 |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
            c = df[(df["lag"] == 300) & (df["theta"] == 0.12)]
            for per, g in c.groupby("period"):
                L.append(f"| {per} | {len(g):,} | {g['size0'].median():.0f} | {g['taken_before'].median():.0f} | "
                         f"{g['taken_next'].median():.0f} | {g['size'].median():.0f} | {(g['taken_before'] > 0).mean():.0%} | "
                         f"{(g['taken_next'] >= g['size']).mean():.0%} |")
    Path(out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


OPEN_THETAS = (0.0, 0.02, 0.03, 0.05, 0.08)


def open_trades(feat, mkts, binance, delays=(3, 10, 30), lag_ms=300, thetas=OPEN_THETAS, window=60):
    """Just after a 5m market opens, the reference price is the average of the last `window`
    seconds before the open (TWAP era) while the settlement average is still all ahead, so the
    fair Up price is Phi(log(spot now / average before the open) / (sigma * twap_std_factor(t))),
    measured on Binance (the Binance-Chainlink basis cancels in the ratio). At `d` s after the
    open (as received) the side with fair - ask - fee >= theta is bought at the ask lag_ms later
    and held. Before the TWAP settlement (strike = price at the open) the same rule is a placebo."""
    import numpy as np
    import pandas as pd
    from scipy.stats import norm
    import binary as bo
    if binance.empty:
        return pd.DataFrame()
    rt = binance["recv_ts_ms"].to_numpy()
    tt = binance["trade_ts_ms"].to_numpy()
    order = np.argsort(tt, kind="stable")
    tt_s = tt[order]
    lp_s = np.log(binance["price"].to_numpy())[order]
    last = pd.Series(lp_s, index=tt_s // 1000).groupby(level=0).last()
    grid = last.reindex(range(int(last.index[0]), int(last.index[-1]) + 1)).ffill(limit=10)
    sigma = grid.diff().rolling(600, min_periods=300).std()
    m5 = mkts[(mkts["horizon"] == 5) & mkts["up_won"].notna()].set_index("market_id")
    by = {mid: g.drop_duplicates("timestamp_ms").sort_values("timestamp_ms")
          for mid, g in feat[feat["market_id"].isin(m5.index)].groupby("market_id")}
    rows = []
    for mid, mk in m5.iterrows():
        if mid not in by:
            continue
        s0 = int(mk["start"] // 1000)
        before = grid.reindex(range(s0 - window - 1, s0 - 1)).to_numpy()  # [open-W-2, open-2]
        sg = sigma.get(s0 - 1, np.nan)
        if np.isnan(before).sum() > 5 or not np.isfinite(sg) or sg <= 0:
            continue
        k = np.nanmean(before)
        f = by[mid]
        fts = f["timestamp_ms"].to_numpy()
        for d in delays:
            t_dec = mk["start"] + d * 1000
            i = np.searchsorted(rt, t_dec, "right") - 1  # last Binance print received by then
            if i < 0 or t_dec - rt[i] > 5000:
                continue
            x = np.log(binance["price"].to_numpy()[i])
            fac = float(bo.twap_std_factor(d)) * sg
            p_up = float(norm.cdf((x - k) / fac))
            j = np.searchsorted(fts, t_dec + lag_ms, "left")
            if j >= len(f) or fts[j] > t_dec + lag_ms + 1000:
                continue
            r = f.iloc[j]
            for side, fair, px in (("Up", p_up, r["up_best_ask"]), ("Down", 1 - p_up, r["down_best_ask"])):
                if not (np.isfinite(px) and 0.02 <= px <= 0.98):
                    continue
                fee = 0.07 * px * (1 - px)
                won = float(mk["up_won"] == (1.0 if side == "Up" else 0.0))
                for th in thetas:
                    if fair - px - fee >= th:
                        rows.append((mid, d, th, side, p_up, fair, px, fee, won, won - px - fee))
    return pd.DataFrame(rows, columns=["market_id", "delay", "theta", "side", "p_up", "fair", "price", "fee", "won", "pnl"])


def openmis(workdir, out, days=None, reps=5000):
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
            t = open_trades(feat, market_table(mk, rs), binance)
            t["day"] = name[15:25]
            parts.append(t)
            print(f"{name}: {len(t):,} open trades", flush=True)
        except Exception:
            import traceback
            print(f"{name}: failed\n{traceback.format_exc()}", flush=True)
    df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    df.to_csv(Path(out).with_suffix(".csv.gz"), index=False)
    L = [f"# 开盘后几秒的定价（{DS}，5m 市场）", "",
         "参考价是开盘前约一分钟的平均价（TWAP 结算以后），而结算平均还全在后面，所以开盘后公平价 = "
         "Φ(ln(现价 / 开盘前 [−62 s, −2 s] 的平均) / (σ·TWAP 标准差系数))，都用币安价格算（与 Chainlink 的价差在比值里抵消）。"
         "开盘后 d 秒（记录机收到时）、300 ms 后按卖一买 公平价 − 卖一 − taker 费 ≥ θ 的一方，持有到结算。"
         "8 月 14 日以后（60 秒 TWAP）是要看的时段；8 月 6 日以前参考价是开盘那一刻的价格，同一规则应当没有优势（对照）。探索性质。", ""]
    if df.empty:
        L.append("没有可用的数据。")
    else:
        df["period"] = np.select([df["day"] <= "2026-08-06", df["day"] <= "2026-08-13"],
                                 ["对照 5/25–8/6（开盘价）", "8/7–8/13（30 秒 TWAP）"], "8/14–8/29（60 秒 TWAP）")
        L += ["| 时段 | d | θ | 笔数 | 胜率 | 平均价 | 平均公平价 | EV/份 | p |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for (per, d, th), g in df.groupby(["period", "delay", "theta"]):
            pv = bo.fair_price_pvalue(g["pnl"].to_numpy(), (g["price"] + g["fee"]).to_numpy(), sims=reps) \
                if len(g) >= 10 and g["pnl"].mean() > 0 else 1.0
            L.append(f"| {per} | {d} s | {100 * th:.0f}¢ | {len(g):,} | {g['won'].mean():.1%} | {g['price'].mean():.3f} | "
                     f"{g['fair'].mean():.3f} | {100 * g['pnl'].mean():+.2f}¢ | {pv:.4f} |")
    Path(out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


def stale(workdir, out, days=None, reps=5000):
    import numpy as np
    import pandas as pd
    import binary as bo
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    arcs = archives(fetch("MANIFEST.txt").decode())
    if days:
        arcs = arcs[-int(days):]
    parts, delays = [], []
    for name, _ in arcs:
        try:
            local = fetch(name, workdir / name)
            feat, mk, rs = read_day(local)
            binance = read_binance(local)
            Path(local).unlink()
            t = stale_trades(feat, market_table(mk, rs), binance)
            t["day"] = name[15:25]
            parts.append(t)
            if len(binance):
                q = np.nanpercentile(binance["recv_ts_ms"] - binance["trade_ts_ms"], [10, 50, 90])
                delays.append(q)
                print(f"  Binance trade -> recorder: p10 {q[0]:.0f} ms, median {q[1]:.0f} ms, p90 {q[2]:.0f} ms", flush=True)
            print(f"{name}: {len(binance):,} Binance trades, {t['market_id'].nunique() if len(t) else 0} markets", flush=True)
        except Exception:
            import traceback
            print(f"{name}: failed\n{traceback.format_exc()}", flush=True)
    df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    df.to_csv(Path(out).with_suffix(".csv.gz"), index=False)
    L = [f"# 币安跳变后 5m / 15m / 1h 的卖一还挂多久（{DS}，逐笔币安、100 ms 盘口，记录机的时钟）", "",
         f"{len(arcs)} 个日档。触发：窗口剩 240–60 秒时，第一笔与至少一秒前最后一笔相比涨跌超过 zσ 的币安成交，"
         "以记录机收到它的时刻为 0；L 毫秒后按那一刻快照里的卖一买顺势一方，付 taker 费，持有到结算。探索性质。", ""]
    if delays:
        q = np.median(np.array(delays), axis=0)
        L += [f"记录机收到币安成交的延迟（各日分位数的中位）：p10 {q[0]:.0f} ms，中位 {q[1]:.0f} ms，p90 {q[2]:.0f} ms。", ""]
    if df.empty:
        L.append("没有可用的数据。")
    for (hz, z), g in df.groupby(["horizon", "z"]) if not df.empty else []:
        L += [f"## {hz}m 市场，z = {z:g}（{g['market_id'].nunique():,} 个市场触发）", "",
              "| L | 笔数 | 胜率 | 平均价 | EV/份 | p | 卖一数量中位 | 卖一没动的比例 |", "|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for lag, h in g.groupby("lag"):
            pv = bo.fair_price_pvalue(h["pnl"].to_numpy(), (h["price"] + h["fee"]).to_numpy(), sims=reps) \
                if len(h) >= 10 and h["pnl"].mean() > 0 else 1.0
            L.append(f"| {lag} ms | {len(h):,} | {h['won'].mean():.1%} | {h['price'].mean():.3f} | "
                     f"{100 * h['pnl'].mean():+.2f}¢ | {pv:.4f} | {h['size'].median():.0f} | {h['still'].mean():.0%} |")
        L.append("")
    Path(out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


def _scalar(s):
    """Numbers from a column that may hold lists (schema variants) or strings."""
    import numpy as np
    import pandas as pd
    return pd.to_numeric(s.map(lambda v: (v[0] if len(v) else np.nan) if isinstance(v, (list, tuple, np.ndarray)) else v),
                         errors="coerce")


def _resolution_semantics(value):
    """Comparable exact source meaning; generic annotations remain unknown."""
    import pandas as pd
    if value is None or pd.isna(value):
        return None
    text = str(value).strip().lower().rstrip("/")
    if "twap-30" in text:
        return "twap30"
    if "twap-60" in text:
        return "twap60"
    if "chainlink" in text and "btc" in text and "usd" in text:
        return "point"
    return None


def market_table(mk, rs):
    """One row per market: horizon, window, reference price and (if known) whether Up won."""
    import numpy as np
    import pandas as pd
    mk = mk.copy()
    mk["market_id"] = mk["market_id"].astype(str)
    for c in ("session_start_ts", "session_end_ts", "chainlink_open_price", "up_won", "outcome_direction"):
        if c in mk:
            mk[c] = _scalar(mk[c])
    g = mk.sort_values("session_end_ts").groupby("market_id")
    out = g.agg(slug=("slug", "last"), start=("session_start_ts", "last"), end=("session_end_ts", "last"),
                k=("chainlink_open_price", lambda x: x.dropna().iloc[-1] if x.notna().any() else np.nan))
    for c in ("oracle_source", "chainlink_open_price_source", "resolution_price_source"):
        if c in mk:
            out[c] = g[c].agg(
                lambda x: str(x.dropna().iloc[-1]).strip() if x.notna().any() else None
            )
    for c in (
        "open_boundary_source", "close_boundary_source",
        "open_boundary_fallback_used", "close_boundary_fallback_used",
        "usable_for_backtest", "resolution_consistency",
    ):
        if c in mk:
            out[c] = g[c].agg(
                lambda x: x.dropna().iloc[-1] if x.notna().any() else None
            )
    source_columns = [c for c in ("resolution_price_source", "oracle_source") if c in out]
    out["resolution_source"] = (
        out[source_columns].bfill(axis=1).iloc[:, 0]
        if source_columns else None
    )
    out["resolution_source_basis"] = np.where(
        out["resolution_source"].notna(), "market_snapshot", None
    )
    for c in ("up_token_id", "down_token_id"):
        if c in mk:
            out[c.replace("_id", "")] = g[c].agg(lambda x: str(x.dropna().iloc[-1]) if x.notna().any() else None)
    snapshot_won = pd.Series(np.nan, index=out.index)
    if "up_won" in mk:
        w = mk.dropna(subset=["up_won"]).groupby("market_id")["up_won"].last()
        snapshot_won.loc[w.index.intersection(snapshot_won.index)] = w
    out["snapshot_up_won"] = snapshot_won
    out["settlement_conflict"] = False
    out["resolution_source_conflict"] = False
    won = snapshot_won.copy()
    if not rs.empty:  # latest resolution revision is authoritative; conflicts remain explicit
        rs = rs.copy()
        key = next((c for c in ("market_id", "condition_id", "slug") if c in rs), None)
        col = next((c for c in ("up_won", "outcome_direction", "winner", "winning_outcome") if c in rs), None)
        if key == "market_id":
            rs["market_id"] = rs["market_id"].astype(str)
            order = [c for c in ("revision", "emitted_at_ts", "resolution_ts") if c in rs]
            latest = rs.sort_values(order, kind="stable") if order else rs
            latest = latest.drop_duplicates("market_id", keep="last").set_index("market_id")
            if col:
                authority = latest[col].map(
                    lambda x: (x[0] if len(x) else None)
                    if isinstance(x, (list, tuple, np.ndarray)) else x
                )
                authority = authority.map(
                    lambda x: 1.0 if str(x).lower() in ("1", "1.0", "up", "true") else
                    0.0 if str(x).lower() in ("0", "0.0", "-1", "-1.0", "down", "false") else np.nan
                ).reindex(out.index)
                conflict = authority.notna() & snapshot_won.notna() & (authority != snapshot_won)
                out["settlement_conflict"] = conflict
                won = authority.combine_first(snapshot_won)
            for c in (
                "resolution_price_source", "oracle_source", "open_boundary_source",
                "close_boundary_source", "open_boundary_fallback_used",
                "close_boundary_fallback_used", "usable_for_backtest", "consistency_check",
            ):
                if c in latest:
                    out[f"resolution_{c}" if c in out else c] = latest[c].reindex(out.index)
            rs_source_columns = [
                c for c in ("resolution_price_source", "oracle_source") if c in latest
            ]
            if rs_source_columns:
                rs_source = latest[rs_source_columns].bfill(axis=1).iloc[:, 0].reindex(out.index)
                old_source = out["resolution_source"].copy()
                old_semantics = old_source.map(_resolution_semantics)
                new_semantics = rs_source.map(_resolution_semantics)
                out["resolution_source_conflict"] = (
                    old_semantics.notna() & new_semantics.notna()
                    & (old_semantics != new_semantics)
                )
                out["resolution_source"] = rs_source.combine_first(old_source)
                out["resolution_source_basis"] = np.where(
                    rs_source.notna(), "resolution_record", out["resolution_source_basis"]
                )
            if "revision" in latest:
                out["resolution_revision"] = latest["revision"].reindex(out.index)
            if "emitted_at_ts" in latest:
                out["resolution_emitted_at_ts"] = latest["emitted_at_ts"].reindex(out.index)
    out["up_won"] = won
    out["horizon"] = ((out["end"] - out["start"]) / 60000).round().astype("Int64")
    return out.reset_index()


THETAS = (0.0, 0.01, 0.02, 0.03, 0.05, 0.08, 0.12)


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
                rule = {}
                for th in THETAS:  # first box below 1 + th that is still there 100 ms later, traded then
                    c = cost < 1 + th
                    hit = (c & c.shift(1, fill_value=False))
                    hit = hit[hit].index
                    if len(hit):
                        rule[f"cost_{th}"] = float(cost.loc[hit[0]])
                        rule[f"size_{th}"] = float(size.loc[hit[0]])
                        rule[f"tau_{th}"] = (a["end"] - hit[0]) / 1000
                rows.append({**rule,
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
            if not parts[:-1]:
                print("resolution columns:", list(rs.columns), "\n", rs.head(3).to_string()[:3000], flush=True)
                print(mkts["horizon"].value_counts().to_string(), "\nwith reference:", int(mkts["k"].notna().sum()),
                      "with winner:", int(mkts["up_won"].notna().sum()), flush=True)
            print(f"{name}: {len(mkts)} markets, {len(b)} pairs, "
                  f"cheap pairs {int((b['min_cost'] < 1).sum()) if len(b) else 0}", flush=True)
        except Exception:
            import traceback
            print(f"{name}: failed\n{traceback.format_exc()}", flush=True)
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
        import binary as bo
        L += ["", "## 规则：成本第一次低于 1 + θ 且 100 ms 后仍低于时买一组，持有到结算", "",
              "每对最多一组，按那一刻的成本（含费）成交，盈亏 = 实际拿回 − 成本。θ = 0 是纯套利。", "",
              "| 组合 | θ | 组数 | 平均成本 | 拿回 2 的比例 | 每组盈亏 | p | 数量中位 | 每天（按对数折算）赚的钱 × 数量 |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
        ndays = df["day"].nunique()
        for pair, g in df.groupby("pair"):
            for th in THETAS:
                h = g.dropna(subset=[f"cost_{th}", "payoff"])
                if h.empty:
                    continue
                pnl = h["payoff"] - h[f"cost_{th}"]
                # the box pays 1 for sure; only the extra 1 is random, so test that part
                pv = bo.fair_price_pvalue((h["payoff"] - 1).to_numpy() - (h[f"cost_{th}"] - 1).clip(lower=0).to_numpy(),
                                          (h[f"cost_{th}"] - 1).clip(lower=0).to_numpy(), sims=5000) \
                    if pnl.mean() > 0 and len(h) >= 10 and th > 0 else float("nan")
                L.append(f"| {pair} | {th:g} | {len(h):,} | {h[f'cost_{th}'].mean():.3f} | {(h['payoff'] >= 2).mean():.1%} | "
                         f"{100 * pnl.mean():+.2f}¢ | {pv:.4f} | {h[f'size_{th}'].median():.0f} | "
                         f"${(pnl * h[f'size_{th}']).sum() / ndays:,.0f} |")
        L += ["", "按天（100 ms 后仍便宜的对数）：", "",
              "| 日期 | " + " | ".join(sorted(df["pair"].unique())) + " |", "|---|" + "---:|" * df["pair"].nunique()]
        for day, g in df.groupby("day"):
            L.append(f"| {day} | " + " | ".join(str(int(g[g["pair"] == p]["persist_cost"].notna().sum()))
                                               for p in sorted(df["pair"].unique())) + " |")
    Path(out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


def probe_trades(workdir, out):
    """Which sources the trades and books tables hold (is there a Polymarket trade tape?)."""
    import pandas as pd
    import pyarrow.parquet as pq
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    arcs = archives(fetch("MANIFEST.txt").decode())
    name = arcs[-1][0]
    local = fetch(name, workdir / name)
    trades, books = [], []
    with tarfile.open(local, "r|gz") as tar:
        for m in tar:
            if not (m.isfile() and m.name.endswith(".parquet")):
                continue
            if "dataset=trades/" in m.name:
                trades.append(pq.read_table(io.BytesIO(tar.extractfile(m).read())).to_pandas())
            elif "dataset=books_100ms/" in m.name and len(books) < 30:
                books.append(pq.read_table(io.BytesIO(tar.extractfile(m).read())).to_pandas())
    t = pd.concat(trades, ignore_index=True)
    b = pd.concat(books, ignore_index=True)
    L = [f"# trades / books_100ms 的来源（{name}）", "", "## trades", "",
         "```", t.groupby([t["exchange"].astype(str), t["instrument"].astype(str)]).size().to_string()[:5000], "```", ""]
    other = t[t["exchange"].astype(str).str.lower() != "binance"]
    L += ["非币安的样例：", "", "```", other.head(20).to_string(max_colwidth=70)[:6000], "```", "",
          f"taker_side: {t['taker_side'].astype(str).value_counts().to_dict()}", "",
          "## books_100ms", "", "```",
          b.groupby([b["venue"].astype(str), b["snapshot_kind"].astype(str)]).size().to_string()[:3000], "```", "",
          "```", b[b["venue"].astype(str).str.lower().str.contains("poly")].head(5).to_string(max_colwidth=90)[:5000], "```"]
    Path(local).unlink()
    Path(out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


HOUR_WINDOWS = ((1800, 300), (300, 60), (60, 5))
HOUR_THETAS = (0.02, 0.04, 0.06, 0.08, 0.12)
HOUR_LAGS = (300, 500, 1000, 2000)
FEE_072_UNTIL = 1782864000000  # 2026-07-01 00:00 UTC in ms: the crypto taker fee rate was 0.072 before, 0.07 after


def taker_rate(ts_ms):
    import numpy as np
    return np.where(np.asarray(ts_ms) < FEE_072_UNTIL, 0.072, 0.07)


def hourly_trades(feat, mkts, binance, windows=HOUR_WINDOWS, thetas=HOUR_THETAS, lags=HOUR_LAGS, health=False):
    """1h markets settle on Binance's own 1-hour candle (close >= open), so Binance's price is the
    settlement variable itself: with K the first Binance trade of the hour and S the last trade
    received by second t, the fair Up price is the European digital Phi(ln(S/K) / (sigma*sqrt(tau))),
    sigma the trailing 10 min std of 1 s log returns up to two whole seconds back. Once a second
    (recorder clock) in each window of seconds left, the side whose fair - ask - fee is largest,
    on the quote shown at t (at most 1 s old, ask in [0.02, 0.98]), is bought if that edge is at
    least theta, as a limit order at 0.98 filled at the ask L ms later (no fill: the next second
    may try again); first fill per market, window, theta and L. With `health`, both quotes must be
    sane rows of a book that changed within HEALTH_ALIVE before t and after the fill (book_health).
    Also returns, per market, whether the Binance candle agrees with the official outcome."""
    import numpy as np
    import pandas as pd
    from scipy.stats import norm
    if binance.empty:
        return pd.DataFrame(), pd.DataFrame()
    rt = binance["recv_ts_ms"].to_numpy()
    tt = binance["trade_ts_ms"].to_numpy()
    px = binance["price"].to_numpy()
    order = np.argsort(tt, kind="stable")
    tt_s, px_s = tt[order], px[order]
    last = pd.Series(np.log(px_s), index=tt_s // 1000).groupby(level=0).last()
    grid = last.reindex(range(int(last.index[0]), int(last.index[-1]) + 1)).ffill(limit=10)
    sigma = grid.diff().rolling(600, min_periods=300).std()
    h1 = mkts[(mkts["horizon"] == 60) & mkts["up_won"].notna()].set_index("market_id")
    by = {mid: g.drop_duplicates("timestamp_ms").sort_values("timestamp_ms")
          for mid, g in feat[feat["market_id"].isin(h1.index)].groupby("market_id")}
    rows, checks = [], []
    for mid, mk in h1.iterrows():
        start, end = int(mk["start"]), int(mk["end"])
        i0 = np.searchsorted(tt_s, start, "left")
        i1 = np.searchsorted(tt_s, end, "left") - 1
        if i0 >= len(tt_s) or i1 < i0 or tt_s[i0] - start > 5000 or end - tt_s[i1] > 5000:
            continue  # the feed must cover the candle's first and last seconds
        k, close = px_s[i0], px_s[i1]
        k_known = rt[order][i0]  # when the recorder had the opening trade
        checks.append((mid, float(close >= k), float(mk["up_won"]), close / k - 1))
        if mid not in by:
            continue
        f = by[mid]
        fts = f["timestamp_ms"].to_numpy()
        ua, da = f["up_best_ask"].to_numpy(), f["down_best_ask"].to_numpy()
        uas, das = f["up_ask_size"].to_numpy(), f["down_ask_size"].to_numpy()
        sane, changes = book_health(f) if health else (np.ones(len(f), bool), None)
        won_up = float(mk["up_won"])
        for hi, lo in windows:
            t = np.arange(end - hi * 1000, end - lo * 1000 + 1, 1000)
            t = t[t >= k_known]
            if not len(t):
                continue
            j = np.searchsorted(rt, t, "right") - 1  # last Binance trade received by t
            ok = j >= 0
            s = np.where(ok, px[np.maximum(j, 0)], np.nan)
            sg = sigma.reindex(t // 1000 - 2).to_numpy()  # seconds whose trades have all arrived by t
            tau = (end - t) / 1000
            with np.errstate(invalid="ignore", divide="ignore"):
                fair_up = norm.cdf(np.log(s / k) / (sg * np.sqrt(tau)))
            q = np.searchsorted(fts, t, "right") - 1
            qq = np.maximum(q, 0)
            fresh = (q >= 0) & (t - fts[qq] <= 1000) & sane[qq]
            a_up, a_dn = ua[qq], da[qq]
            r = taker_rate(t)
            e_up = fair_up - a_up - r * a_up * (1 - a_up)
            e_dn = (1 - fair_up) - a_dn - r * a_dn * (1 - a_dn)
            with np.errstate(invalid="ignore"):
                up = np.where(np.isnan(e_dn), True, np.where(np.isnan(e_up), False, e_up >= e_dn))
                edge = np.where(up, e_up, e_dn)
                a_sel = np.where(up, a_up, a_dn)
                good = fresh & ok & np.isfinite(edge) & (a_sel >= 0.02) & (a_sel <= 0.98)
            if health:
                good &= np.array([changed_near(changes, x, HEALTH_ALIVE[0], 10**12) for x in t])
            for th in thetas:
                hit = np.flatnonzero(good & (edge >= th))
                for lag in lags:
                    for i in hit:
                        kk = np.searchsorted(fts, t[i] + lag, "left")
                        if kk >= len(f) or fts[kk] > t[i] + lag + 1000:
                            continue
                        if health and not (sane[kk] and changed_near(changes, fts[kk], 10**12, HEALTH_ALIVE[1])):
                            continue
                        side_up = bool(up[i])
                        p = ua[kk] if side_up else da[kk]
                        if not (np.isfinite(p) and p <= 0.98):
                            continue  # the 0.98 limit does not fill
                        fee = float(taker_rate(t[i] + lag)) * p * (1 - p)
                        won = won_up if side_up else 1 - won_up
                        rows.append((mid, f"{hi}-{lo}", th, lag, int(t[i]), tau[i], "Up" if side_up else "Down",
                                     float(fair_up[i] if side_up else 1 - fair_up[i]), p,
                                     uas[kk] if side_up else das[kk], fee, won, won - p - fee))
                        break
    cols = ["market_id", "window", "theta", "lag", "t", "tau", "side", "fair", "price", "size", "fee", "won", "pnl"]
    return (pd.DataFrame(rows, columns=cols),
            pd.DataFrame(checks, columns=["market_id", "binance_up", "up_won", "candle_return"]))


def hourly(workdir, out, days=None, reps=5000, health=False):
    """hourly_trades on every day; the window and threshold are chosen on May 25 - Jul 15 at 300 ms
    (highest EV among cells with at least 100 trades) and only checked on the later days."""
    import numpy as np
    import pandas as pd
    import binary as bo
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    arcs = archives(fetch("MANIFEST.txt").decode())
    if days:
        arcs = arcs[-int(days):]
    parts, checks = [], []
    for name, _ in arcs:
        try:
            local = fetch(name, workdir / name)
            feat, mk, rs = read_day(local)
            binance = read_binance(local)
            Path(local).unlink()
            t, c = hourly_trades(feat, market_table(mk, rs), binance, health=health)
            t["day"] = c["day"] = name[15:25]
            parts.append(t)
            checks.append(c)
            print(f"{name}: {len(t):,} trades, {len(c)} candles checked", flush=True)
        except Exception:
            import traceback
            print(f"{name}: failed\n{traceback.format_exc()}", flush=True)
    df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if len(df):
        df = df.drop_duplicates(["market_id", "window", "theta", "lag"], keep="first")
    ck = pd.concat(checks, ignore_index=True).drop_duplicates("market_id") if checks else pd.DataFrame()
    df.to_csv(Path(out).with_suffix(".csv.gz"), index=False)
    L = [f"# 1 小时市场：按币安 1 小时 K 线算的公平价（{DS}，记录机时钟）", "",
         "1 小时市场按币安 BTCUSDT 1 小时 K 线的收盘 ≥ 开盘结算，所以公平 Up 价 = Φ(ln(S/K) / (σ√τ))："
         "K 是这一小时第一笔币安成交，S 是 t 时刻（记录机收到）最后一笔，σ 是之前 10 分钟每秒对数收益的标准差。"
         "每个剩余时间窗口里每秒看一次，按 t 时刻的报价（不超过 1 秒前、卖一在 0.02–0.98）算 公平价 − 卖一 − taker 费，"
         "最大的一方超过 θ 时发 0.98 的限价买单，L 毫秒后按卖一成交（没成交下一秒可以再试），"
         "每个市场、窗口、θ、L 只取第一笔，持有到结算。taker 费率 7 月前 0.072、之后 0.07。"
         "窗口和门槛只在 5 月 25 日–7 月 15 日、L = 300 ms 上选（笔数至少 100 里 EV 最高），之后只核对。探索性质。", ""]
    if health:
        L += [f"加了盘口健康检查（`book_health`）：两个报价都不能是交叉盘、两边卖一之和不低于 0.99、处于交易状态且未暂停，"
              f"并且盘口在 t 之前 {HEALTH_ALIVE[0] / 1000:g} 秒内和成交后 {HEALTH_ALIVE[1] / 1000:g} 秒内确实变过。", ""]
    if len(ck):
        agree = (ck["binance_up"] == ck["up_won"]).mean()
        bad = ck[ck["binance_up"] != ck["up_won"]]
        L += [f"核对：{len(ck):,} 个 1 小时市场里，币安 K 线（收盘 ≥ 开盘）与官方结果一致的比例 {agree:.1%}；"
              f"不一致的 {len(bad)} 个，K 线涨跌幅中位 {100 * bad['candle_return'].abs().median():.3f}%"
              if len(bad) else f"核对：{len(ck):,} 个 1 小时市场里，币安 K 线（收盘 ≥ 开盘）与官方结果全部一致", ""]
    if df.empty:
        L.append("没有可用的交易。")
    else:
        df["period"] = np.select([df["day"] <= "2026-07-15", df["day"] <= "2026-08-16"],
                                 ["A 5/25–7/15", "B 7/16–8/16"], "C 8/17–8/29")
        a = df[(df["period"].str.startswith("A")) & (df["lag"] == 300)].groupby(["window", "theta"])["pnl"].agg(["mean", "size"])
        a = a[a["size"] >= 100]
        if len(a):
            w, th = a["mean"].idxmax()
            sel = df[(df["window"] == w) & (df["theta"] == th)]
            L += [f"## 在 A 段选出的格子：窗口 {w} 秒、θ = {100 * th:.0f}¢（A 段 300 ms 每份 {100 * a['mean'].max():+.2f}¢）", "",
                  "| L | 时段 | 笔数 | 胜率 | 平均价 | EV/份 | p |", "|---:|---|---:|---:|---:|---:|---:|"]
            for (lag, per), g in sel.groupby(["lag", "period"]):
                pv = bo.fair_price_pvalue(g["pnl"].to_numpy(), (g["price"] + g["fee"]).to_numpy(), sims=reps) \
                    if len(g) >= 10 and g["pnl"].mean() > 0 else 1.0
                L.append(f"| {lag} ms | {per} | {len(g):,} | {g['won'].mean():.1%} | {g['price'].mean():.3f} | "
                         f"{100 * g['pnl'].mean():+.2f}¢ | {pv:.4f} |")
            L.append("")
        L += ["## 全部格子（描述用）", "",
              "| L | 窗口（剩余秒） | θ | 时段 | 笔数 | 胜率 | 平均价 | 平均公平价 | EV/份 | p | 卖一数量中位 |",
              "|---:|---|---:|---|---:|---:|---:|---:|---:|---:|---:|"]
        for (lag, w, th, per), g in df.groupby(["lag", "window", "theta", "period"], sort=False):
            pv = bo.fair_price_pvalue(g["pnl"].to_numpy(), (g["price"] + g["fee"]).to_numpy(), sims=reps) \
                if len(g) >= 10 and g["pnl"].mean() > 0 else 1.0
            L.append(f"| {lag} ms | {w} | {100 * th:.0f}¢ | {per} | {len(g):,} | {g['won'].mean():.1%} | "
                     f"{g['price'].mean():.3f} | {g['fair'].mean():.3f} | {100 * g['pnl'].mean():+.2f}¢ | {pv:.4f} | "
                     f"{g['size'].median():.0f} |")
    Path(out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))

FADE_JUMPS = (0.03, 0.05, 0.08)
FADE_THETAS = (0.0, 0.02, 0.04, 0.06, 0.08, 0.12)


def _spot_state(binance):
    """(receipt times, log prices in receipt order, per-second sigma) of a Binance trade table."""
    import numpy as np
    import pandas as pd
    rt = binance["recv_ts_ms"].to_numpy()
    lp = np.log(binance["price"].to_numpy())
    tt = binance["trade_ts_ms"].to_numpy()
    order = np.argsort(tt, kind="stable")
    last = pd.Series(lp[order], index=tt[order] // 1000).groupby(level=0).last()
    grid = last.reindex(range(int(last.index[0]), int(last.index[-1]) + 1)).ffill(limit=10)
    return rt, lp, grid.diff().rolling(600, min_periods=300).std()


def fade_trades(feat, mkts, binance, jumps=FADE_JUMPS, thetas=FADE_THETAS, lags=GATE_LAGS + (1000,), quiet=0.5,
                spot_lag_ms=2000, min_shares=5.0, health=True):
    """The mirror of the gated rule (5m markets): the Polymarket Up mid moves by at least `jump`
    within a second while Binance moves less than `quiet` sigma over the same second, so the move is
    not explained by spot. With the mid a second earlier as the prior and that second's Binance move,
    fair Up = Phi(Phi^-1(P_prev) + dx / (sigma * twap_std_factor(t))); the side the move made cheaper
    is bought at its ask lag_ms later if fair - ask - fee >= theta. Decisions on every snapshot with
    240..15 s left (recorder clock); first trade per market, lag, jump and theta. Spot must also have
    been quiet for `spot_lag_ms` before the prior (so the jump is not Polymarket catching up with an
    earlier spot move), and at least one Binance trade must have arrived within the second. With
    `health`, the three quotes must be sane, the book must have changed within HEALTH_ALIVE before the
    prior, and at least once after the signal and by the fill row (so the fill is not a repeated copy
    of the jump); the ask must show at least `min_shares`."""
    import numpy as np
    import pandas as pd
    from scipy.stats import norm
    import binary as bo
    if binance.empty:
        return pd.DataFrame()
    rt, lp, sigma = _spot_state(binance)
    m5 = mkts[(mkts["horizon"] == 5) & mkts["up_won"].notna()].set_index("market_id")
    by = {mid: g.drop_duplicates("timestamp_ms").sort_values("timestamp_ms")
          for mid, g in feat[feat["market_id"].isin(m5.index)].groupby("market_id")}
    rows = []
    for mid, mk in m5.iterrows():
        if mid not in by:
            continue
        f = by[mid]
        fts = f["timestamp_ms"].to_numpy()
        ub, ua = f["up_best_bid"].to_numpy(), f["up_best_ask"].to_numpy()
        da, uas, das = f["down_best_ask"].to_numpy(), f["up_ask_size"].to_numpy(), f["down_ask_size"].to_numpy()
        sane, changes = book_health(f) if health else (np.ones(len(f), bool), None)
        mid_up = (ub + ua) / 2
        k = np.flatnonzero((fts >= mk["end"] - 240_000) & (fts <= mk["end"] - 15_000))
        if not len(k):
            continue
        t = fts[k]
        k1 = np.searchsorted(fts, t - 1000, "right") - 1
        ok = (k1 >= 0) & (t - fts[np.maximum(k1, 0)] <= 1500) & sane[k] & sane[np.maximum(k1, 0)]
        dm = mid_up[k] - mid_up[np.maximum(k1, 0)]
        j0, j1 = np.searchsorted(rt, t - 1000, "right") - 1, np.searchsorted(rt, t, "right") - 1
        dx = np.where((j0 >= 0) & (j1 > j0), lp[np.maximum(j1, 0)] - lp[np.maximum(j0, 0)], np.nan)
        jq = np.searchsorted(rt, t - 1000 - spot_lag_ms, "right") - 1
        dxq = np.where(jq >= 0, lp[np.maximum(j1, 0)] - lp[np.maximum(jq, 0)], np.nan)
        sg = sigma.reindex(t // 1000 - 2).to_numpy()
        with np.errstate(invalid="ignore"):
            cand = np.flatnonzero(ok & (np.abs(dm) >= min(jumps)) & (np.abs(dx) <= quiet * sg)
                                  & (np.abs(dxq) <= quiet * sg) & (sg > 0))
        done = set()
        for i in cand:
            if health and not changed_near(changes, fts[k1[i]], HEALTH_ALIVE[0], 10**12):
                continue
            p_prev = min(max(mid_up[k1[i]], 0.005), 0.995)
            t_mkt = (t[i] - (mk["end"] - bo.WINDOW_S * 1000)) / 1000
            fac = float(bo.twap_std_factor(t_mkt)) * sg[i]
            p1 = float(norm.cdf(norm.ppf(p_prev) + dx[i] / fac))
            up = dm[i] < 0  # the move made Up cheaper: buy Up; else buy Down
            fair = p1 if up else 1 - p1
            won = float(mk["up_won"] == (1.0 if up else 0.0))
            for lag in lags:
                kk = np.searchsorted(fts, t[i] + lag, "left")
                if kk >= len(f) or fts[kk] > t[i] + lag + 1000:
                    continue
                if health and not (sane[kk] and changed_near(changes, fts[kk], fts[kk] - t[i] - 1, HEALTH_ALIVE[1])):
                    continue
                px = ua[kk] if up else da[kk]
                sz = uas[kk] if up else das[kk]
                if not (np.isfinite(px) and 0.02 <= px <= 0.98 and np.isfinite(sz) and sz >= min_shares):
                    continue
                fee = float(taker_rate(fts[kk])) * px * (1 - px)
                for jump in jumps:
                    if abs(dm[i]) < jump:
                        continue
                    for th in thetas:
                        if (lag, jump, th) in done or fair - px - fee < th:
                            continue
                        done.add((lag, jump, th))
                        rows.append((mid, lag, jump, th, int(t[i]), (mk["end"] - t[i]) / 1000, float(dm[i]),
                                     float(dx[i] / sg[i]), fair, px, sz, fee, won, won - px - fee))
    return pd.DataFrame(rows, columns=["market_id", "lag", "jump", "theta", "t", "tau", "dmid", "dx_sigma", "fair",
                                       "price", "size", "fee", "won", "pnl"])


FOLLOW_SIZES = (100, 500, 2000)
CANCEL_LAGS = (170, 200, 250, 300, 400)


def follow_trades(feat, mkts, trades, sizes=FOLLOW_SIZES, lags=GATE_LAGS + (1000,), slip=0.01, min_shares=5.0,
                  health=True):
    """Copy large Polymarket takers (5m markets): a taker print of at least `size` USDC with
    240..15 s left is followed by buying the side it bet on (a buy of a token, or the other token
    after a sell) at that side's ask lag_ms after the print was received, if the ask is at most
    `slip` above the price the print implies for that side and shows at least `min_shares`; first trade
    per market, lag and size. With `health`, the quotes must be sane, the book must have changed within
    HEALTH_ALIVE before the print and at least once after the print and by the fill row (so the fill
    reflects the print), and the fee is the rate in force (taker_rate)."""
    import numpy as np
    import pandas as pd
    m5 = mkts[(mkts["horizon"] == 5) & mkts["up_won"].notna()].set_index("market_id")
    if trades.empty or "up_token" not in m5:
        return pd.DataFrame()
    side_of = {}
    for mid, mk in m5.iterrows():
        side_of[str(mk["up_token"])] = (mid, "Up")
        side_of[str(mk["down_token"])] = (mid, "Down")
    tr = trades[trades["instrument"].isin(side_of) & trades["taker_side"].isin(["buy", "sell"])]
    by = {mid: g.drop_duplicates("timestamp_ms").sort_values("timestamp_ms")
          for mid, g in feat[feat["market_id"].isin(m5.index)].groupby("market_id")}
    health_of = {}
    rows, done = [], set()
    for r in tr.itertuples():
        mid, tok_side = side_of[r.instrument]
        mk = m5.loc[mid]
        t = int(r.recv_ts_ms)
        if not (mk["end"] - 240_000 <= t <= mk["end"] - 15_000) or mid not in by:
            continue
        notional = float(r.price) * float(r.size)
        buy = r.taker_side == "buy"
        side = tok_side if buy else ("Down" if tok_side == "Up" else "Up")
        implied = float(r.price) if buy else 1 - float(r.price)
        f = by[mid]
        fts = f["timestamp_ms"].to_numpy()
        if health and mid not in health_of:
            health_of[mid] = book_health(f)
        won = float(mk["up_won"] == (1.0 if side == "Up" else 0.0))
        for lag in lags:
            kk = np.searchsorted(fts, t + lag, "left")
            if kk >= len(f) or fts[kk] > t + lag + 1000:
                continue
            if health:
                sane, changes = health_of[mid]
                if not (sane[kk] and changed_near(changes, t, HEALTH_ALIVE[0], 10**12)
                        and changed_near(changes, fts[kk], fts[kk] - t - 1, 10**12)):  # moved in (t, fill]
                    continue
            px = f["up_best_ask"].to_numpy()[kk] if side == "Up" else f["down_best_ask"].to_numpy()[kk]
            size = f["up_ask_size"].to_numpy()[kk] if side == "Up" else f["down_ask_size"].to_numpy()[kk]
            if not (np.isfinite(px) and 0.02 <= px <= 0.98 and px <= implied + slip + 1e-9
                    and np.isfinite(size) and size >= min_shares):
                continue
            fee = float(taker_rate(fts[kk])) * px * (1 - px)
            for s_min in sizes:
                if notional < s_min or (mid, lag, s_min) in done:
                    continue
                done.add((mid, lag, s_min))
                rows.append((mid, lag, s_min, t, (mk["end"] - t) / 1000, side, notional, implied, px, size, fee, won,
                             won - px - fee))
    return pd.DataFrame(rows, columns=["market_id", "lag", "min_usdc", "t", "tau", "side", "notional", "implied",
                                       "price", "size", "fee", "won", "pnl"])


def cancel_lead_signals(feat, mkts, binance, trades, lags=CANCEL_LAGS, jump_z=2.0, jump_horizon_ms=500,
                        tau_hi=240, tau_lo=15, max_remaining_ratio=0.5, min_removed=5.0, min_shares=5.0,
                        signal_gap_ms=2000):
    """Use a partial top-ask withdrawal as an early directional signal.

    A signal occurs when at least ``min_removed`` shares disappear from one side at an unchanged
    ask, no more than ``max_remaining_ratio`` of that side remains, at least ``min_shares`` remain,
    the opposite ask is stable, and no taker buy explains the disappearance.  The disappearing
    side is bought with a FAK limit at the still-visible ask.  ``lags`` are total signal-to-match
    times, so 170--200 ms model the current 150 ms taker hold plus a warm network path.

    Binance is never used to create or price the order.  It is only an after-the-fact mechanism
    label: whether a >= ``jump_z`` sigma Binance move first arrived in the same direction within
    ``jump_horizon_ms`` after the withdrawal, provided no such jump arrived in the prior 500 ms.
    """
    import numpy as np
    import pandas as pd
    if feat.empty or binance.empty:
        return pd.DataFrame()

    rt = binance["recv_ts_ms"].to_numpy()
    tt = binance["trade_ts_ms"].to_numpy()
    lp = np.log(binance["price"].to_numpy())
    order = np.argsort(tt, kind="stable")
    tt_s, lp_s = tt[order], lp[order]
    last = pd.Series(lp_s, index=tt_s // 1000).groupby(level=0).last()
    grid = last.reindex(range(int(last.index[0]), int(last.index[-1]) + 1)).ffill(limit=10)
    sigma = grid.diff().rolling(600, min_periods=300).std()
    ref = np.searchsorted(tt_s, tt - 1000, "right") - 1
    valid = (ref >= 0) & (tt - tt_s[np.maximum(ref, 0)] <= 5000)
    sg = sigma.reindex(tt // 1000 - 1).to_numpy()
    with np.errstate(invalid="ignore"):
        is_jump = valid & (np.abs(lp - lp_s[np.maximum(ref, 0)]) > jump_z * sg)
    jump_t, jump_up = rt[is_jump], lp[is_jump] > lp_s[np.maximum(ref[is_jump], 0)]
    jump_order = np.argsort(jump_t, kind="stable")
    jump_t, jump_up = jump_t[jump_order], jump_up[jump_order]

    buys = {}
    if trades is not None and len(trades):
        tb = trades[trades["taker_side"] == "buy"]
        buys = {str(tok): (g["recv_ts_ms"].to_numpy(), g["price"].to_numpy(float))
                for tok, g in tb.groupby("instrument")}

    def consumed(tok, lo, hi, ask):
        if tok not in buys:
            return False
        times, prices = buys[tok]
        a, b = np.searchsorted(times, [lo, hi], "right")
        return bool(np.any(prices[a:b] <= ask + 1e-9))

    m5 = mkts[(mkts["horizon"] == 5) & mkts["up_won"].notna()].set_index("market_id")
    by = {mid: g.drop_duplicates("timestamp_ms").sort_values("timestamp_ms")
          for mid, g in feat[feat["market_id"].isin(m5.index)].groupby("market_id")}
    rows = []
    for mid, mk in m5.iterrows():
        if mid not in by:
            continue
        f = by[mid]
        fts = f["timestamp_ms"].to_numpy()
        ua = f["up_best_ask"].to_numpy(float)
        da = f["down_best_ask"].to_numpy(float)
        uas = f["up_ask_size"].to_numpy(float)
        das = f["down_ask_size"].to_numpy(float)
        sane, changes = book_health(f)
        tau = (mk["end"] - fts[1:]) / 1000
        base = (np.diff(fts) <= 250) & (tau >= tau_lo) & (tau <= tau_hi) & sane[1:]

        def partial_pull(ask, size, other_ask, other_size):
            removed = size[:-1] - size[1:]
            other_removed = other_size[:-1] - other_size[1:]
            same_ask = np.isfinite(ask[:-1]) & np.isfinite(ask[1:]) & (np.abs(ask[1:] - ask[:-1]) < 1e-9)
            other_stable = (np.isfinite(other_ask[:-1]) & np.isfinite(other_ask[1:])
                            & (np.abs(other_ask[1:] - other_ask[:-1]) < 1e-9)
                            & np.isfinite(other_size[:-1]) & np.isfinite(other_size[1:])
                            & ((other_removed < min_removed) | (other_size[1:] > max_remaining_ratio * other_size[:-1])))
            return (base & same_ask & np.isfinite(size[:-1]) & np.isfinite(size[1:]) & (size[1:] >= min_shares)
                    & (removed >= min_removed) & (size[1:] <= max_remaining_ratio * size[:-1]) & other_stable)

        up_pull = partial_pull(ua, uas, da, das)
        down_pull = partial_pull(da, das, ua, uas)
        last_signal = -np.inf
        for i in np.flatnonzero(up_pull ^ down_pull) + 1:
            t = int(fts[i])
            tau = (mk["end"] - t) / 1000
            if t - last_signal < signal_gap_ms:
                continue
            if up_pull[i - 1]:
                side, ask, size, tok = "Up", ua, uas, str(mk.get("up_token"))
            else:
                side, ask, size, tok = "Down", da, das, str(mk.get("down_token"))
            if consumed(tok, int(fts[i - 1]), t, ask[i]):
                continue
            limit_price = float(ask[i])
            before_size, remaining_size = float(size[i - 1]), float(size[i])
            removed = before_size - remaining_size
            last_signal = t
            next_jump = int(np.searchsorted(jump_t, t, "right"))
            prev_jump = next_jump - 1
            prior_age = t - jump_t[prev_jump] if prev_jump >= 0 else np.nan
            clean_lead = not np.isfinite(prior_age) or prior_age > 500
            if clean_lead and next_jump < len(jump_t) and jump_t[next_jump] <= t + jump_horizon_ms:
                lead_ms = float(jump_t[next_jump] - t)
                same_side = float(bool(jump_up[next_jump]) == (side == "Up"))
            else:
                lead_ms, same_side = np.nan, np.nan
            won = float(mk["up_won"] == (1.0 if side == "Up" else 0.0))
            row = {"market_id": mid, "signal_t": t, "tau": tau, "side": side, "limit_price": limit_price,
                   "size_before": before_size, "size_remaining": remaining_size, "removed": removed,
                   "removed_fraction": removed / before_size, "prior_jump_age_ms": prior_age,
                   "jump_lead_ms": lead_ms, "jump_same_side": same_side, "won": won}
            ask, size = (ua, uas) if side == "Up" else (da, das)
            for lag in lags:
                k = int(np.searchsorted(fts, t + lag, "left"))
                price = np.nan
                feed_live = k < len(f) and changed_near(changes, fts[k], fts[k] - t + 1, HEALTH_ALIVE[1])
                if (feed_live and fts[k] <= t + lag + 100 and sane[k] and np.isfinite(ask[k])
                        and ask[k] <= limit_price + 1e-9 and np.isfinite(size[k]) and size[k] >= min_shares):
                    price = float(ask[k])
                fee = float(taker_rate(fts[k])) * price * (1 - price) if np.isfinite(price) else np.nan
                row[f"match_t_{lag}"] = int(fts[k]) if np.isfinite(price) else np.nan
                row[f"price_{lag}"] = price
                row[f"size_{lag}"] = float(size[k]) if np.isfinite(price) else np.nan
                row[f"fee_{lag}"] = fee
                row[f"pnl_{lag}"] = won - price - fee if np.isfinite(price) else np.nan
            rows.append(row)
    return pd.DataFrame(rows)


def cancel_lead(workdir, out, days=None, reps=5000):
    """Historical mechanism audit for the partial-cancel trigger; it never places orders."""
    import numpy as np
    import pandas as pd
    from scipy.stats import binomtest
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
            t = cancel_lead_signals(feat, market_table(mk, rs), read_binance(local), read_poly_trades(local))
            Path(local).unlink()
            t["day"] = name[15:25]
            parts.append(t)
            print(f"{name}: {len(t):,} partial-cancel signals", flush=True)
        except Exception:
            import traceback
            print(f"{name}: failed\n{traceback.format_exc()}", flush=True)
    df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    df.to_csv(Path(out).with_suffix(".csv.gz"), index=False)
    L = [f"# 首批撤单领先信号（{DS}，BTC 5m，100 ms 盘口）", "",
         "固定机制：一侧卖一价格不变、至少撤掉 5 份且剩余不超过原量 50%，仍有至少 5 份；另一侧没有同级撤量；"
         "同一 100 ms 内没有 taker 买单解释这次减少。把这次部分撤单视为该方向的领先信号，FAK 限价固定为撤单后仍显示的卖一，"
         "只买尚未撤走的慢报价。每个市场信号至少间隔 2 秒。币安只用于事后检查 500 ms 内是否出现同向 2σ 跳动，不参与下单。", "",
         "主执行口径是信号后 200 ms 撮合（150 ms 固定等待 + warm 路径与处理）；170/250/300/400 ms 只做敏感性。"
         "这是历史机制审计，不是可部署结论；即使通过，也必须冻结后在新的逐条盘口上前向验证。", ""]
    if df.empty:
        L.append("没有符合条件的信号。")
    else:
        df["period"] = np.select([df["day"] <= "2026-07-15", df["day"] <= "2026-08-16"],
                                 ["A 5/25–7/15", "B 7/16–8/16"], "C 8/17–8/29")
        L += ["## 是否领先币安", "", "| 时段 | 信号 | 500 ms 内币安跳动 | 同向率 | 同向二项 p | 同向时领先中位 |",
              "|---|---:|---:|---:|---:|---:|"]
        for period, g in df.groupby("period"):
            j = g.dropna(subset=["jump_same_side"])
            same = int(j["jump_same_side"].sum())
            pv = float(binomtest(same, len(j), 0.5, alternative="greater").pvalue) if len(j) else 1.0
            med = j.loc[j["jump_same_side"] == 1, "jump_lead_ms"].median()
            L.append(f"| {period} | {len(g):,} | {len(j):,} ({len(j) / len(g):.1%}) | "
                     f"{same / len(j):.1%} | {pv:.4f} | {med:.0f} ms |" if len(j) else
                     f"| {period} | {len(g):,} | 0 | – | 1.0000 | – |")
        L += ["", "## 剩余慢报价能否成交并赚钱", "", "每个时段、每个延迟只保留每个市场第一次可成交的信号。", "",
              "| 时段 | 信号→撮合 | 信号 | 成交市场 | 成交率 | EV/份 | p | 均价 |",
              "|---|---:|---:|---:|---:|---:|---:|---:|"]
        gate = {}
        for period, g in df.groupby("period"):
            for lag in CANCEL_LAGS:
                x = g.dropna(subset=[f"price_{lag}"]).sort_values("signal_t").drop_duplicates("market_id")
                ev = x[f"pnl_{lag}"].mean() if len(x) else np.nan
                pv = bo.fair_price_pvalue(x[f"pnl_{lag}"].to_numpy(),
                                          (x[f"price_{lag}"] + x[f"fee_{lag}"]).to_numpy(), sims=reps) \
                    if len(x) >= 10 and ev > 0 else 1.0
                L.append(f"| {period} | {lag} ms | {len(g):,} | {len(x):,} | {len(x) / len(g):.1%} | "
                         f"{100 * ev:+.2f}¢ | {pv:.4f} | {x[f'price_{lag}'].mean():.3f} |" if len(x) else
                         f"| {period} | {lag} ms | {len(g):,} | 0 | 0.0% | – | 1.0000 | – |")
                gate[(period, lag)] = (len(x), ev, pv)
        b = gate.get(("B 7/16–8/16", 200), (0, np.nan, 1.0))
        c = gate.get(("C 8/17–8/29", 200), (0, np.nan, 1.0))
        passed = b[0] >= 100 and c[0] >= 100 and b[1] > 0 and c[1] > 0 and c[2] < 0.05
        L += ["", f"历史机制门：{'通过' if passed else '未通过'}。要求 200 ms 在 B、C 各至少 100 个成交市场且 EV 都为正，C 的精确 p < 0.05。"
              "通过只允许冻结前向检验，不允许直接实盘。"]
    Path(out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


def _period_table(df, keys, reps, title_cols):
    """Rows per key and period (A May 25 - Jul 15, B Jul 16 - Aug 16, C Aug 17 - 29) with EV and p."""
    import numpy as np
    import binary as bo
    df = df.copy()
    df["period"] = np.select([df["day"] <= "2026-07-15", df["day"] <= "2026-08-16"],
                             ["A 5/25–7/15", "B 7/16–8/16"], "C 8/17–8/29")
    L = ["| " + " | ".join(title_cols) + " | 时段 | 笔数 | 胜率 | 平均价 | EV/份 | p | 卖一数量中位 |",
         "|" + "---:|" * len(title_cols) + "---|---:|---:|---:|---:|---:|---:|"]
    for key, g in df.groupby(keys + ["period"]):
        pv = bo.fair_price_pvalue(g["pnl"].to_numpy(), (g["price"] + g["fee"]).to_numpy(), sims=reps) \
            if len(g) >= 10 and g["pnl"].mean() > 0 else 1.0
        L.append("| " + " | ".join(str(k) for k in key[:-1]) + f" | {key[-1]} | {len(g):,} | {g['won'].mean():.1%} | "
                 f"{g['price'].mean():.3f} | {100 * g['pnl'].mean():+.2f}¢ | {pv:.4f} | {g['size'].median():.0f} |")
    return L


def fadefollow(kind, workdir, out, days=None, reps=5000, health=True):
    """fade or follow on every day; parameters chosen on May 25 - Jul 15 at 300 ms (highest EV among
    cells with at least 100 trades) and only checked on the later days and lags."""
    import numpy as np
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
            mkts = market_table(mk, rs)
            if kind == "fade":
                t = fade_trades(feat, mkts, read_binance(local), health=health)
            else:
                t = follow_trades(feat, mkts, read_poly_trades(local), health=health)
            Path(local).unlink()
            t["day"] = name[15:25]
            parts.append(t)
            print(f"{name}: {len(t):,} trades", flush=True)
        except Exception:
            import traceback
            print(f"{name}: failed\n{traceback.format_exc()}", flush=True)
    df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    df.to_csv(Path(out).with_suffix(".csv.gz"), index=False)
    if kind == "fade":
        L = [f"# 反向：Polymarket 自己的跳动（{DS}，5m 市场，记录机时钟）", "",
             "候选：剩 240–15 秒时每个快照，Up 中间价一秒内变动 ≥ 跳动幅度，而币安同一秒的涨跌不到 0.5σ（现货没动）。"
             "以一秒前的中间价为原来的概率，加上这一秒币安的涨跌，算出公平价；买这次跳动变便宜的一方，L 毫秒后按卖一（卖一至少 5 份），"
             "只在 公平价 − 卖一 − taker 费 ≥ θ 时成交；每个市场、每组（跳动幅度, θ）取第一笔，持有到结算。"
             "加盘口健康检查。5 月 25 日–7 月 15 日比较参数，之后只核对。探索性质。", ""]
        keys, cols = ["lag", "jump", "theta"], ["L（ms）", "跳动", "θ"]
    else:
        L = [f"# 跟随大单（{DS}，5m 市场，记录机时钟）", "",
             "候选：剩 240–15 秒时每一笔金额 ≥ 门槛的 Polymarket 吃单（买某个代币，或卖出后等于买另一个）；"
             "记录机收到后 L 毫秒按同一方向的卖一买，只在卖一不超过这笔成交隐含的价格 1¢ 以上、且至少 5 份时成交；"
             "每个市场、每个门槛取第一笔，持有到结算。加盘口健康检查。5 月 25 日–7 月 15 日比较门槛，之后只核对。探索性质。", ""]
        keys, cols = ["lag", "min_usdc"], ["L（ms）", "金额门槛（USDC）"]
    if df.empty:
        L.append("没有可用的交易。")
    else:
        pick = [k for k in keys if k != "lag"]
        d = df.copy()
        d["period"] = np.where(d["day"] <= "2026-07-15", "A", "later")
        a = d[(d["period"] == "A") & (d["lag"] == 300)].groupby(pick)["pnl"].agg(["mean", "size"])
        a = a[a["size"] >= 100]
        if len(a):
            best = a["mean"].idxmax()
            best = best if isinstance(best, tuple) else (best,)
            sel = df.copy()
            for k, v in zip(pick, best):
                sel = sel[sel[k] == v]
            L += [f"## 在 A 段（300 ms）选出的参数：{'，'.join(f'{c} = {v}' for c, v in zip(cols[1:], best))}"
                  f"（A 段每份 {100 * a['mean'].max():+.2f}¢）", ""]
            L += _period_table(sel, ["lag"], reps, ["L（ms）"]) + [""]
        L += ["## 全部格子（描述用）", ""] + _period_table(df, keys, reps, cols)
    Path(out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


JITTER_LAG = 300  # ms after receipt, as in test G
JITTER_OFFSETS = (("m_m32", -32000), ("m_m10", -10000), ("m_m2", -2000), ("m_0", 0), ("m_p03", 300),
                  ("m_p2", 2000), ("m_p10", 10000))


def jitter_rows(feat, mkts, binance, lag=JITTER_LAG, z0=2.0, tau_lo=15, horizon=5, gap_ms=2000, trades=None):
    """One row per Binance jump episode in each `horizon`-minute market: the first print with
    tau_hi..tau_lo s left whose log price moved more than z0 sigma from the last trade at least a
    second earlier (as gated_trades), then nothing for gap_ms. Each row describes the state around
    the jump instead of trading it, so rules on it can be chosen later on May 25 - Jul 15 alone:

    - up, z: the move's side and size (sigma = 10 min of 1 s Binance returns);
    - m_*: the Up mid shown 32, 10 and 2 s before, at, and 0.3, 2 and 10 s after the receipt
      (NaN without a snapshot in the second before), spread_0 / spread_m2 the Up spread then;
    - pm_range30, pm_changes30: range of the Up mid and number of book changes from 32 to 2 s
      before (a flat book before the jump has both small);
    - bn_rv30: standard deviation of 1 s Binance returns over the same 30 s, in sigma units
      (below 1: quieter than usual); bn_move2, bn_after2: the Binance move over the 2 s before and
      after the receipt, in sigma units, signed Up;
    - fair_up: Phi(Phi^-1(mid at receipt) + dx / (sigma * twap_std_factor)) as gated_trades;
      fair_a2_up: the same from the mid 2 s before plus the Binance move since (anchor_ms=2000);
    - ua_l, da_l, uas_l, das_l: both asks and sizes `lag` ms after receipt (the first snapshot then,
      within a second); ok: both snapshots sane and the book changed within HEALTH_ALIVE around them;
    - uas_m2, das_m2, uas_0, das_0: the top ask sizes 2 s before and at the receipt;
    - with Polymarket `trades`, sweep2 / against2: shares that takers bought toward the move (Up
      bought or Down sold for an up move) and against it in the 2 s before the receipt, n_trades2 the
      prints then. A mid that jumped with a sweep2 near zero moved because makers pulled or
      repriced their quotes, not because someone took them."""
    import numpy as np
    import pandas as pd
    from scipy.stats import norm
    import binary as bo
    if binance.empty:
        return pd.DataFrame()
    rt = binance["recv_ts_ms"].to_numpy()
    tt = binance["trade_ts_ms"].to_numpy()
    lp = np.log(binance["price"].to_numpy())
    order = np.argsort(tt, kind="stable")
    tt_s, lp_s = tt[order], lp[order]
    last = pd.Series(lp_s, index=tt_s // 1000).groupby(level=0).last()
    grid = last.reindex(range(int(last.index[0]), int(last.index[-1]) + 1)).ffill(limit=10)
    sigma = grid.diff().rolling(600, min_periods=300).std()
    g0, gd = int(grid.index[0]), grid.diff().to_numpy()
    window = 60 * int(horizon)
    tau_hi = window - 60
    m5 = mkts[(mkts["horizon"] == horizon) & mkts["up_won"].notna()].set_index("market_id")
    by = {mid: g.drop_duplicates("timestamp_ms").sort_values("timestamp_ms")
          for mid, g in feat[feat["market_id"].isin(m5.index)].groupby("market_id")}

    flows = {}
    if trades is not None and len(trades):
        for (tok, side), g in trades.sort_values("recv_ts_ms", kind="stable").groupby(["instrument", "taker_side"]):
            flows[(tok, side)] = (g["recv_ts_ms"].to_numpy(), g["size"].to_numpy(float))

    def flow(tok, side, lo, hi):
        if (tok, side) not in flows:
            return 0.0, 0
        r, sz = flows[(tok, side)]
        a_, b_ = np.searchsorted(r, [lo, hi], "right")
        return float(sz[a_:b_].sum()), int(b_ - a_)

    def at_recv(x):
        j = np.searchsorted(rt, x, "right") - 1
        return lp[j] if j >= 0 else np.nan

    rows = []
    for mid, mk in m5.iterrows():
        if mid not in by:
            continue
        f = by[mid]
        fts = f["timestamp_ms"].to_numpy()
        ub, ua = f["up_best_bid"].to_numpy(float), f["up_best_ask"].to_numpy(float)
        da, uas, das = f["down_best_ask"].to_numpy(float), f["up_ask_size"].to_numpy(float), f["down_ask_size"].to_numpy(float)
        mids = (ub + ua) / 2
        sane, changes = book_health(f)
        a, b = np.searchsorted(rt, [mk["end"] - tau_hi * 1000, mk["end"] - tau_lo * 1000])
        if b <= a:
            continue
        j = np.searchsorted(tt_s, tt[a:b] - 1000, "right") - 1
        ok = (j >= 0) & (tt[a:b] - tt_s[np.maximum(j, 0)] <= 5000)
        dx = np.where(ok, lp[a:b] - lp_s[np.maximum(j, 0)], np.nan)
        sg = sigma.reindex(tt[a:b] // 1000 - 1).to_numpy()
        with np.errstate(invalid="ignore"):
            cand = np.flatnonzero(np.abs(dx) > z0 * sg)

        def snap(x):
            k = np.searchsorted(fts, x, "right") - 1
            return k if k >= 0 and x - fts[k] <= 1000 else -1

        def clip(p):
            return min(max(p, 0.005), 0.995)

        prev = -np.inf
        for i in cand:
            t0 = int(rt[a + i])
            if t0 - prev < gap_ms:
                continue
            prev = t0
            k0 = snap(t0)
            if k0 < 0 or not (np.isfinite(ub[k0]) and np.isfinite(ua[k0])):
                continue
            k = np.searchsorted(fts, t0 + lag, "left")
            if k >= len(f) or fts[k] > t0 + lag + 1000:
                continue
            t_mkt = (t0 - (mk["end"] - window * 1000)) / 1000
            fac = float(bo.twap_std_factor(t_mkt, window=window)) * sg[i]
            if not (np.isfinite(fac) and fac > 0):
                continue
            fair_up = float(norm.cdf(norm.ppf(clip(mids[k0])) + dx[i] / fac))
            ka = snap(t0 - 2000)
            la = at_recv(t0 - 2000)
            fair_a2 = float(norm.cdf(norm.ppf(clip(mids[ka])) + (lp[a + i] - la) / fac)) \
                if ka >= 0 and np.isfinite(mids[ka]) and np.isfinite(la) else np.nan
            lo, hi = np.searchsorted(fts, [t0 - 32000, t0 - 2000], "left")
            w = mids[lo:hi]
            w = w[np.isfinite(w)]
            c_lo, c_hi = np.searchsorted(changes, [t0 - 32000, t0 - 2000], "left")
            s = t0 // 1000
            seg = gd[max(s - 32 - g0, 0):max(s - 2 - g0, 0)]
            seg = seg[np.isfinite(seg)]
            row = {"market_id": mid, "t0": t0, "tau": (mk["end"] - t0) / 1000, "up": int(dx[i] > 0),
                   "z": abs(dx[i]) / sg[i], "p0": mids[k0], "fair_up": fair_up, "fair_a2_up": fair_a2}
            for name, off in JITTER_OFFSETS:
                kk = snap(t0 + off)
                row[name] = mids[kk] if kk >= 0 else np.nan
            row.update({"spread_0": ua[k0] - ub[k0], "spread_m2": (ua[ka] - ub[ka]) if ka >= 0 else np.nan,
                        "pm_range30": float(w.max() - w.min()) if len(w) else np.nan, "pm_changes30": int(c_hi - c_lo),
                        "bn_rv30": float(seg.std() / sg[i]) if len(seg) >= 20 else np.nan,
                        "bn_move2": (lp[a + i] - la) / sg[i] if np.isfinite(la) else np.nan,
                        "bn_after2": (at_recv(t0 + 2000) - lp[a + i]) / sg[i],
                        "ua_l": ua[k], "da_l": da[k], "uas_l": uas[k], "das_l": das[k],
                        "uas_m2": uas[ka] if ka >= 0 else np.nan, "das_m2": das[ka] if ka >= 0 else np.nan,
                        "uas_0": uas[k0], "das_0": das[k0],
                        "ok": bool(sane[k0] and sane[k] and changed_near(changes, t0, HEALTH_ALIVE[0], 10**12)
                                   and changed_near(changes, fts[k], 10**12, HEALTH_ALIVE[1])),
                        "up_won": float(mk["up_won"])})
            if flows and "up_token" in mk:
                up_tok, dn_tok = str(mk["up_token"]), str(mk["down_token"])
                bu, nbu = flow(up_tok, "buy", t0 - 2000, t0)
                su, nsu = flow(up_tok, "sell", t0 - 2000, t0)
                bd, nbd = flow(dn_tok, "buy", t0 - 2000, t0)
                sd, nsd = flow(dn_tok, "sell", t0 - 2000, t0)
                toward, against = (bu + sd, bd + su) if row["up"] else (bd + su, bu + sd)
                row.update({"sweep2": toward, "against2": against, "n_trades2": nbu + nsu + nbd + nsd})
            rows.append(row)
    return pd.DataFrame(rows)


def jitter_trades(df, side_rule, theta, fair_col="fair_up"):
    """First trade per market among healthy rows: `side_rule` 'move' buys the side of the Binance
    move, 'fade' the other side; bought at that side's ask `lag` ms later when fair - ask - fee >=
    theta, with fair from `fair_col` (the side's probability)."""
    import numpy as np
    d = df[df["ok"]].copy()
    buy_up = (d["up"] == 1) if side_rule == "move" else (d["up"] == 0)
    d["px"] = np.where(buy_up, d["ua_l"], d["da_l"])
    d["size"] = np.where(buy_up, d["uas_l"], d["das_l"])
    d["fair"] = np.where(buy_up, d[fair_col], 1 - d[fair_col])
    d["fee"] = 0.07 * d["px"] * (1 - d["px"])
    d["won"] = np.where(buy_up, d["up_won"], 1 - d["up_won"])
    d = d[(d["px"] >= 0.02) & (d["px"] <= 0.98) & (d["fair"] - d["px"] - d["fee"] >= theta)]
    d = d.sort_values("t0").drop_duplicates("market_id")
    d["price"], d["pnl"] = d["px"], d["won"] - d["px"] - d["fee"]
    return d


PRINT_COLS = ["market_id", "t0", "tau", "up", "z", "p0", "fair_up", "fair_a2_up", "m_m2", "m_0", "ua_l", "da_l",
              "uas_l", "das_l", "ok", "up_won", "day"]


def tradable(t, theta=0.04):
    """Rows some scalein.py rule could buy: a healthy book, the side's ask in 0.02..0.98 and G's or
    H's fair value at least `theta` (the lowest add threshold) above it after the fee."""
    import numpy as np
    up = t["up"].to_numpy() == 1
    px = np.where(up, t["ua_l"], t["da_l"]).astype(float)
    fee = 0.07 * px * (1 - px)
    edge = [np.where(up, t[c], 1 - t[c]).astype(float) - px - fee for c in ("fair_up", "fair_a2_up")]
    with np.errstate(invalid="ignore"):
        return t["ok"].astype(bool).to_numpy() & (px >= 0.02) & (px <= 0.98) & ((edge[0] >= theta) | (edge[1] >= theta))


def jitter(workdir, out, days=None, reps=5000, gap_ms=2000, shard=None):
    """jitter_rows on every day; the rows go to <out>.csv.gz for slicing, the report shows the
    split of the gated rule (and its fade) by how flat the book and Binance were before the jump,
    with the cuts at the terciles of May 25 - Jul 15. gap_ms=0 keeps every candidate print (as a
    live bot sees them; scalein.py then waits 2 s after each fill), only the rows some scalein.py
    rule could buy and only the columns it reads, and skips the report. shard "i/n" runs every
    n-th archive from the i-th (0-based), to run in parallel and concatenate."""
    import numpy as np
    import pandas as pd
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    arcs = archives(fetch("MANIFEST.txt").decode())
    if days:
        arcs = arcs[-int(days):]
    if shard:
        i, n = map(int, shard.split("/"))
        arcs = arcs[i::n]
    parts = []
    for name, _ in arcs:
        try:
            local = fetch(name, workdir / name)
            feat, mk, rs = read_day(local)
            binance = read_binance(local)
            prints = read_poly_trades(local)
            Path(local).unlink()
            t = jitter_rows(feat, market_table(mk, rs), binance, trades=None if gap_ms == 0 else prints, gap_ms=gap_ms)
            t["day"] = name[15:25]
            if gap_ms == 0 and len(t):
                t = t.loc[tradable(t), PRINT_COLS].round({"tau": 3, "z": 3, "p0": 4, "fair_up": 5, "fair_a2_up": 5,
                                                          "m_m2": 4, "m_0": 4, "ua_l": 4, "da_l": 4, "uas_l": 2, "das_l": 2})
            parts.append(t)
            print(f"{name}: {len(t):,} jump episodes over {t['market_id'].nunique() if len(t) else 0} markets", flush=True)
        except Exception:
            import traceback
            print(f"{name}: failed\n{traceback.format_exc()}", flush=True)
    df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    df.to_csv(Path(out).with_suffix(".csv.gz"), index=False)
    if gap_ms == 0:
        Path(out).write_text(f"# 逐笔候选（{DS}）\n\n{len(df):,} 行，{df['day'].nunique() if len(df) else 0} 天；"
                             f"只留某条加仓规则可能买到的行（scalein.py）。\n", encoding="utf-8")
        return
    L = [f"# 急动前后的盘口：横盘之后的抖动（{DS}，5m 市场，记录机时钟）", "",
         "每个市场里每一次币安急动（剩 240–15 秒，与至少一秒前相比超过 2σ，之后 2 秒内不再算新的一次）记一行："
         "触发前 32/10/2 秒、触发时、之后 0.3/2/10 秒的 Up 中间价，触发前 30 秒中间价的波动幅度和盘口变化次数，"
         "同期币安一秒涨跌的标准差（相对平时的 σ），触发前后 2 秒币安的涨跌，两种公平价（从触发时的中间价起算 / "
         f"从 2 秒前的中间价加上币安这 2 秒的涨跌起算），{JITTER_LAG} ms 后两边的卖一和数量，结算结果；"
         "还有触发前 2 秒朝急动方向的主动成交量（扫单）和两边卖一挂单量的变化（没有成交、挂单却没了，就是做市商撤单或改价）。"
         "全部数据在同名 .csv.gz 里。下面的分组门槛都取 5 月 25 日–7 月 15 日的三分位，之后两段只核对。探索性质。", ""]
    if df.empty:
        L.append("没有可用的数据。")
    else:
        df["period"] = np.select([df["day"] <= "2026-07-15", df["day"] <= "2026-08-16"],
                                 ["A 5/25–7/15", "B 7/16–8/16"], "C 8/17–8/29")
        h = df[df["ok"]]
        L += [f"急动 {len(df):,} 次（盘口健康的 {len(h):,} 次），{df['market_id'].nunique():,} 个市场。", ""]
        sgn = np.where(df["up"] == 1, 1.0, -1.0)
        df["pre2"] = sgn * (df["m_0"] - df["m_m2"])
        df["rev10"] = sgn * (df["m_p10"] - df["m_0"])
        a = df[df["period"].str.startswith("A") & df["ok"]]
        cuts = {c: a[c].quantile([1 / 3, 2 / 3]).to_list() for c in ("pm_range30", "pm_changes30", "bn_rv30")}

        def bucket(x, c):
            lo, hi = cuts[c]
            return np.select([x <= lo, x <= hi], ["低", "中"], "高")

        L += ["## 中间价在触发前 2 秒先动了，之后 10 秒回不回去（盘口健康的急动，按买的方向）", "",
              "pre2：触发前 2 秒中间价朝急动方向动了多少；rev10：触发后 10 秒中间价再朝同方向动了多少（负数是回吐）。", "",
              "| 时段 | 触发前 30 秒中间价幅度 | 先动 ≥ 3¢ 的次数 | 其后 10 秒平均 | 没先动的次数 | 其后 10 秒平均 |",
              "|---|---|---:|---:|---:|---:|"]
        h = df[df["ok"]].copy()
        h["b_range"] = bucket(h["pm_range30"], "pm_range30")
        for (per, br), g in h.groupby(["period", "b_range"]):
            mv, st = g[g["pre2"] >= 0.03], g[g["pre2"] < 0.03]
            L.append(f"| {per} | {br}（≤ {cuts['pm_range30'][0]:.3f} / {cuts['pm_range30'][1]:.3f}） | {len(mv):,} | "
                     f"{100 * mv['rev10'].mean():+.2f}¢ | {len(st):,} | {100 * st['rev10'].mean():+.2f}¢ |")
        if "sweep2" in h:
            mv = h[h["pre2"] >= 0.03].copy()
            mv["how"] = np.select([mv["sweep2"] >= 5, mv["n_trades2"] == 0], ["扫单（朝急动方向主动成交 ≥ 5 份）", "无成交（撤单/改价）"],
                                  "少量成交")
            for th in (0.04, 0.12):
                for rule, label in (("move", "顺势"), ("fade", "反向")):
                    t = jitter_trades(mv, rule, th, "fair_a2_up")
                    if t.empty:
                        continue
                    L += ["", f"## 先动 ≥ 3¢ 的急动，按怎么动的分组：{label}，2 秒前起算的公平价，θ = {100 * th:.0f}¢", ""]
                    L += _period_table(t, ["how"], reps, ["怎么动的"])
            L += ["", "## 先动 ≥ 3¢ 的急动，之后 10 秒中间价（按急动方向，负数是回吐）", "",
                  "| 时段 | 怎么动的 | 次数 | 其后 10 秒平均 | 价差（触发时）中位 |", "|---|---|---:|---:|---:|"]
            for (per, how), g in mv.groupby(["period", "how"]):
                L.append(f"| {per} | {how} | {len(g):,} | {100 * g['rev10'].mean():+.2f}¢ | {g['spread_0'].median():.3f} |")
        for rule, col, label in (("move", "fair_up", "顺势（检验 D/F 的公平价）"),
                                 ("move", "fair_a2_up", "顺势（2 秒前起算的公平价）"),
                                 ("fade", "fair_a2_up", "反向（2 秒前起算的公平价，买另一边）")):
            for th in (0.04, 0.08, 0.12):
                t = jitter_trades(df, rule, th, col)
                if t.empty:
                    continue
                L += ["", f"## {label}，θ = {100 * th:.0f}¢，按触发前的平静程度分组", ""]
                for c, name in (("pm_range30", "中间价幅度"), ("pm_changes30", "盘口变化次数"), ("bn_rv30", "币安波动/σ")):
                    t["b"] = bucket(t[c], c)
                    L += [f"**{name}**（三分位 {cuts[c][0]:.3g} / {cuts[c][1]:.3g}）", ""]
                    L += _period_table(t, ["b"], reps, [name]) + [""]
    Path(out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


LEAD_THETAS = (0.04, 0.08, 0.12)


def leadlag_trades(feat, mkts, btc, alt, z0=2.0, lag=300, thetas=LEAD_THETAS, tau_lo=15, horizon=5):
    """BTC leads: a BTC print (Binance, as received) with tau_hi..tau_lo s left in an altcoin market
    that moved more than z0 BTC sigma from the last BTC trade a second earlier is a candidate. The
    altcoin is expected to follow by beta times the BTC move (beta: 10 min rolling regression of
    the altcoin's 1 s Binance returns on BTC's), less what the altcoin's own Binance price already
    moved over the same second. Two fair values for the altcoin market's side of the move:
    'plain' from its Up mid at the print plus that remaining move; 'anchored' from its mid 2 s
    before plus beta times the BTC move over those 2 s (whatever the market already did is not
    counted twice). The side is bought at its ask `lag` ms later when fair - ask - fee >= theta, on
    a healthy book (book_health, HEALTH_ALIVE); first trade per market, variant and theta.
    alt_done is the share of the expected move the altcoin's Binance price had already made."""
    import numpy as np
    import pandas as pd
    from scipy.stats import norm
    import binary as bo
    if btc.empty or alt.empty:
        return pd.DataFrame()

    def prep(b):
        rt, tt, lp = b["recv_ts_ms"].to_numpy(), b["trade_ts_ms"].to_numpy(), np.log(b["price"].to_numpy(float))
        o = np.argsort(tt, kind="stable")
        last = pd.Series(lp[o], index=tt[o] // 1000).groupby(level=0).last()
        return rt, tt, lp, tt[o], lp[o], last

    brt, btt, blp, btt_s, blp_s, blast = prep(btc)
    alast = prep(alt)[5]
    art, alp = alt["recv_ts_ms"].to_numpy(), np.log(alt["price"].to_numpy(float))
    lo = int(max(blast.index[0], alast.index[0]))
    hi = int(min(blast.index[-1], alast.index[-1]))
    if hi - lo < 600:
        return pd.DataFrame()
    secs = range(lo, hi + 1)
    rb = blast.reindex(secs).ffill(limit=10).diff()
    ra = alast.reindex(secs).ffill(limit=10).diff()
    sig_b = rb.rolling(600, min_periods=300).std()
    sig_a = ra.rolling(600, min_periods=300).std()
    beta = (rb * ra).rolling(600, min_periods=300).mean() / (rb * rb).rolling(600, min_periods=300).mean()

    def a_recv(x):
        j = np.searchsorted(art, x, "right") - 1
        return alp[j] if j >= 0 else np.nan

    def b_recv(x):
        j = np.searchsorted(brt, x, "right") - 1
        return blp[j] if j >= 0 else np.nan

    window = 60 * int(horizon)
    tau_hi = window - 60
    m5 = mkts[(mkts["horizon"] == horizon) & mkts["up_won"].notna()].set_index("market_id")
    by = {mid: g.drop_duplicates("timestamp_ms").sort_values("timestamp_ms")
          for mid, g in feat[feat["market_id"].isin(m5.index)].groupby("market_id")}
    rows = []
    for mid, mk in m5.iterrows():
        if mid not in by:
            continue
        f = by[mid]
        fts = f["timestamp_ms"].to_numpy()
        ub, ua, da = f["up_best_bid"].to_numpy(float), f["up_best_ask"].to_numpy(float), f["down_best_ask"].to_numpy(float)
        uas, das = f["up_ask_size"].to_numpy(float), f["down_ask_size"].to_numpy(float)
        sane, changes = book_health(f)
        a, b = np.searchsorted(brt, [mk["end"] - tau_hi * 1000, mk["end"] - tau_lo * 1000])
        if b <= a:
            continue
        j = np.searchsorted(btt_s, btt[a:b] - 1000, "right") - 1
        ok = (j >= 0) & (btt[a:b] - btt_s[np.maximum(j, 0)] <= 5000)
        dx = np.where(ok, blp[a:b] - blp_s[np.maximum(j, 0)], np.nan)
        sec = btt[a:b] // 1000 - 1
        sb, sa, be = sig_b.reindex(sec).to_numpy(), sig_a.reindex(sec).to_numpy(), beta.reindex(sec).to_numpy()
        with np.errstate(invalid="ignore"):
            cand = np.flatnonzero(np.abs(dx) > z0 * sb)
        done = set()
        for i in cand:
            t0 = int(brt[a + i])
            k0 = np.searchsorted(fts, t0, "right") - 1
            if k0 < 0 or t0 - fts[k0] > 1000 or not (np.isfinite(ub[k0]) and np.isfinite(ua[k0])):
                continue
            if not (sane[k0] and changed_near(changes, t0, HEALTH_ALIVE[0], 10**12)):
                continue
            if not (np.isfinite(be[i]) and np.isfinite(sa[i]) and sa[i] > 0):
                continue
            k = np.searchsorted(fts, t0 + lag, "left")
            if k >= len(f) or fts[k] > t0 + lag + 1000 or not (sane[k] and changed_near(changes, fts[k], 10**12, HEALTH_ALIVE[1])):
                continue
            t_mkt = (t0 - (mk["end"] - window * 1000)) / 1000
            fac = float(bo.twap_std_factor(t_mkt, window=window)) * sa[i]
            if not (np.isfinite(fac) and fac > 0):
                continue
            up = dx[i] > 0
            expect = be[i] * dx[i]
            alt_moved = a_recv(t0) - a_recv(t0 - 1000)
            ka = np.searchsorted(fts, t0 - 2000, "right") - 1
            p0 = min(max((ub[k0] + ua[k0]) / 2, 0.005), 0.995)
            fairs = {"plain": float(norm.cdf(norm.ppf(p0) + (expect - alt_moved) / fac))}
            if ka >= 0 and t0 - 2000 - fts[ka] <= 1000 and np.isfinite(ub[ka]) and np.isfinite(ua[ka]):
                pa = min(max((ub[ka] + ua[ka]) / 2, 0.005), 0.995)
                fairs["anchored"] = float(norm.cdf(norm.ppf(pa) + be[i] * (b_recv(t0) - b_recv(t0 - 2000)) / fac))
                pre = (p0 - pa) if up else (pa - p0)
            else:
                pre = np.nan
            px, size = (ua[k], uas[k]) if up else (da[k], das[k])
            if not (np.isfinite(px) and 0.02 <= px <= 0.98):
                continue
            fee = 0.07 * px * (1 - px)
            won = float(mk["up_won"] == (1.0 if up else 0.0))
            for var, fu in fairs.items():
                fair = fu if up else 1 - fu
                for th in thetas:
                    if (var, th) in done or fair - px - fee < th:
                        continue
                    done.add((var, th))
                    rows.append((mid, var, th, t0, (mk["end"] - t0) / 1000, p0, fair, px, size, fee, won, won - px - fee,
                                 be[i], dx[i] / sb[i], alt_moved / expect if expect else np.nan, pre))
    return pd.DataFrame(rows, columns=["market_id", "variant", "theta", "t0", "tau", "p0", "fair", "price", "size", "fee",
                                       "won", "pnl", "beta", "z_btc", "alt_done", "pre_move"])


def leadlag(workdir, out, alt_ds, days=None, reps=5000):
    """leadlag_trades on every day both datasets have: BTC prints from the BTC dataset, the
    altcoin's markets and Binance trades from alt_ds. Thresholds compared on May 25 - Jul 15."""
    import numpy as np
    import pandas as pd
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    btc_ds = "whodisidk/polymarket-btc-updown-exchange-data"
    set_dataset(btc_ds)
    btc_days = {n for n, _ in archives(fetch("MANIFEST.txt").decode())}
    set_dataset(alt_ds)
    arcs = [(n, s) for n, s in archives(fetch("MANIFEST.txt").decode()) if n in btc_days]
    if days:
        arcs = arcs[-int(days):]
    parts = []
    for name, _ in arcs:
        try:
            set_dataset(btc_ds)
            local = fetch(name, workdir / ("btc-" + name))
            btc = read_binance(local)
            Path(local).unlink()
            set_dataset(alt_ds)
            local = fetch(name, workdir / name)
            feat, mk, rs = read_day(local)
            alt = read_binance(local)
            Path(local).unlink()
            t = leadlag_trades(feat, market_table(mk, rs), btc, alt)
            t["day"] = name[15:25]
            parts.append(t)
            print(f"{name}: {len(t):,} trades over {t['market_id'].nunique() if len(t) else 0} markets", flush=True)
        except Exception:
            import traceback
            print(f"{name}: failed\n{traceback.format_exc()}", flush=True)
    set_dataset(alt_ds)
    df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    df.to_csv(Path(out).with_suffix(".csv.gz"), index=False)
    L = [f"# BTC 领先：BTC 急动后 {alt_ds.split('/')[1].split('-')[1].upper()} 5m 盘口还没跟上（{alt_ds}，记录机时钟）", "",
         "候选：剩 240–15 秒时，与至少一秒前相比涨跌超过 2σ 的每一笔 BTC 币安成交（记录机收到时）。预期这个币会跟着动 β × BTC 涨跌"
         "（β 是近 10 分钟两者每秒涨跌的回归系数），减去它自己的币安价格这一秒已经动了的部分。两种公平价：plain 从触发时的 Up 中间价起算加上"
         "剩下要动的部分；anchored 从 2 秒前的中间价起算加上 β × BTC 这 2 秒的涨跌（市场已经跟上的不重复算）。0.3 秒后按卖一买，"
         "公平价 − 卖一 − taker 费 ≥ θ 才买，盘口健康检查，每个市场每组取第一笔，持有到结算。5 月 25 日–7 月 15 日比较，之后只核对。探索性质。", ""]
    if df.empty:
        L.append("没有成交。")
    else:
        L += _period_table(df, ["variant", "theta"], reps, ["公平价", "θ"])
        d = df.copy()
        d["已跟"] = np.select([d["alt_done"] >= 0.5, d["alt_done"] >= 0.0], ["币自己已经动了 ≥ 一半", "动了不到一半"], "反方向")
        L += ["", "## 按这个币自己的币安价格已经跟了多少（θ = 8¢）", ""]
        L += _period_table(d[d["theta"] == 0.08], ["variant", "已跟"], reps, ["公平价", "已跟"])
    Path(out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


HF_API = "https://huggingface.co/api/datasets?"
SCAN_QUERIES = ("author=whodisidk", "search=polymarket", "search=updown", "search=up-down", "search=kalshi")


def _get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "research"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def datasets(out, queries=SCAN_QUERIES):
    """Hugging Face datasets that may hold Polymarket up/down books after 2026-08-29 (data no rule
    here was chosen on): id, last change, the date range and archive count the card states, and
    whether the card mentions order books."""
    import json
    seen, rows = set(), []
    for q in queries:
        try:
            items = json.loads(_get(HF_API + q + "&limit=200&sort=lastModified&direction=-1"))
        except Exception as e:
            print(f"{q}: {e}", flush=True)
            continue
        for it in items:
            i = it.get("id", "")
            if i in seen:
                continue
            seen.add(i)
            try:
                card = _get(f"https://huggingface.co/datasets/{i}/resolve/main/README.md", 30).decode("utf-8", "replace")
            except Exception:
                card = ""
            rng = re.search(r"date range[^*]*\*\*([^*]+)\*\*", card, re.I)
            n = re.search(r"\*\*(\d+) daily archives\*\*", card)
            books = bool(re.search(r"order.?book|books?_100ms|100 ?ms|best_ask|bid/ask", card, re.I))
            rows.append((it.get("lastModified", "")[:16], i, rng.group(1).strip() if rng else "",
                         n.group(1) if n else "", "是" if books else "", it.get("downloads", "")))
            print(rows[-1], flush=True)
    rows.sort(reverse=True)
    L = ["# Hugging Face 上可能有 8 月 29 日之后 Polymarket 涨跌盘口的数据集", "",
         "查询：" + "，".join(f"`{q}`" for q in queries) + "。按最后修改时间排列；日期范围和日档数取自数据集说明。", "",
         "| 最后修改 | 数据集 | 说明里的日期范围 | 日档数 | 说明提到盘口 | 下载 |", "|---|---|---|---:|---|---:|"]
    L += [f"| {r[0]} | [{r[1]}](https://huggingface.co/datasets/{r[1]}) | {r[2]} | {r[3]} | {r[4]} | {r[5]} |" for r in rows]
    Path(out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


CARD_IDS = ("Sigrex/polymarket_btc_up_down_5m_251218_260909", "Sigrex/polymarket_btc_up_down_multihorizon",
            "Sigrex/polymarket_btc_up_down_15m_250913_260908", "Houroux/polymarket-l2-history",
            "trentmkelly/polymarket_historical_data", "marketlens/polymarket-btc-5m-l2-depth",
            "aliplayer1/polymarket-crypto-updown", "THULab/polymarket_crypto_5m_15m",
            "DineshKumar8399/polymarket-orderbook-dataset", "Mevboters/polymarket-arbitrage-trading-dataset",
            "TimeSeventeen/Polymarket-v1", "TimeSeventeen/Polymarket-v2", "Joseph3222/polymarket-orderbook",
            "polyorderbooks/polymarket-crypto-updown-orderbooks-l2", "Lazy108/binance-polymarket-orderflow")


def cards(out, ids=CARD_IDS):
    """Card, file list and sizes of candidate datasets, to see which hold sub-second BTC 5m books
    after 2026-08-29."""
    import json
    L = ["# 候选数据集的说明和文件列表", ""]
    for i in ids:
        L += [f"## [{i}](https://huggingface.co/datasets/{i})", ""]
        try:
            meta = json.loads(_get(f"https://huggingface.co/api/datasets/{i}?blobs=true"))
            files = [(f.get("rfilename", ""), f.get("size") or 0) for f in meta.get("siblings", [])]
            total = sum(sz for _, sz in files)
            L.append(f"{len(files):,} 个文件，共 {total / 1e9:.2f} GB，最后修改 {meta.get('lastModified', '')[:16]}。")
            names = sorted(files)
            show = names if len(names) <= 40 else names[:20] + [("…", 0)] + names[-20:]
            L += ["", "```"] + [f"{n}  {sz:,}" for n, sz in show] + ["```"]
        except Exception as e:
            L.append(f"文件列表读取失败：{e}")
        try:
            card = _get(f"https://huggingface.co/datasets/{i}/resolve/main/README.md", 30).decode("utf-8", "replace")
            L += ["", "<details><summary>README.md</summary>", "", "```", card[:6000], "```", "</details>"]
        except Exception as e:
            L.append(f"README 读取失败：{e}")
        L.append("")
        print(f"{i}: done", flush=True)
    Path(out).write_text("\n".join(L) + "\n", encoding="utf-8")


class HttpFile(io.RawIOBase):
    """Read-only, seekable view of a remote file through HTTP range requests, so pyarrow can read
    a parquet footer and single row groups of a multi-GB file without downloading it."""

    def __init__(self, url, timeout=120):
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "research"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            self.url, self.size = r.geturl(), int(r.headers["Content-Length"])
        self.pos, self.timeout, self.fetched = 0, timeout, 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, off, whence=0):
        self.pos = off if whence == 0 else self.pos + off if whence == 1 else self.size + off
        return self.pos

    def readinto(self, b):
        n = min(len(b), self.size - self.pos)
        if n <= 0:
            return 0
        req = urllib.request.Request(self.url, headers={"User-Agent": "research",
                                                        "Range": f"bytes={self.pos}-{self.pos + n - 1}"})
        for i in range(4):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = r.read()
                break
            except Exception:
                if i == 3:
                    raise
        b[:len(data)] = data
        self.pos += len(data)
        self.fetched += len(data)
        return len(data)


def remote_parquet(url):
    """(pyarrow ParquetFile, the HttpFile under it) for a parquet file on a web server."""
    import pyarrow.parquet as pq
    raw = HttpFile(url)
    return pq.ParquetFile(io.BufferedReader(raw, buffer_size=1 << 20)), raw


def _rg_range(pf, col):
    """(row group, rows, min, max) of one column from the parquet statistics."""
    i = pf.schema_arrow.get_field_index(col)
    out = []
    for g in range(pf.metadata.num_row_groups):
        st = pf.metadata.row_group(g).column(i).statistics
        out.append((g, pf.metadata.row_group(g).num_rows, st.min if st is not None and st.has_min_max else None,
                    st.max if st is not None and st.has_min_max else None))
    return out


ALI = "aliplayer1/polymarket-crypto-updown"
UNSEEN_FROM = 1788048000  # 2026-08-30 00:00 UTC: after the whodisidk archives every rule here was chosen on


def probe_ali(out, ds=ALI):
    """What aliplayer1's crypto up/down dataset holds for BTC 5m after 2026-08-29: files and sizes,
    the time range of every row group of the order-book, spot and trade tables, a sample of the
    newest rows, the update rate, and whether Binance's public trade archive is reachable."""
    import json
    import pandas as pd
    base = f"https://huggingface.co/datasets/{ds}/resolve/main/"
    ms = lambda v: "" if v is None else f"{pd.Timestamp(int(v), unit='ms'):%Y-%m-%d %H:%M:%S}"
    L = [f"# 数据探查：{ds}（BTC 5m，8 月 29 日之后）", ""]
    meta = json.loads(_get(f"https://huggingface.co/api/datasets/{ds}?blobs=true"))
    files = [(f["rfilename"], f.get("size") or 0) for f in meta.get("siblings", [])]
    keep = [f for f in files if "crypto=BTC" in f[0] or "spot" in f[0] or f[0].endswith(("markets.parquet", ".json"))
            or "heartbeat" in f[0]]
    L += [f"共 {len(files):,} 个文件；和 BTC、现货、市场表有关的 {len(keep)} 个：", "", "```"]
    L += [f"{n}  {sz:,}" for n, sz in sorted(keep)][:400] + ["```", ""]
    try:
        mk = pd.read_parquet(io.BytesIO(_get(base + "data/markets.parquet", 600)))
        b5 = mk[(mk["crypto"] == "BTC") & (mk["timeframe"] == "5-minute")].copy()
        b5["day"] = pd.to_datetime(b5["end_ts"], unit="s").dt.strftime("%Y-%m")
        L += ["## markets.parquet（BTC 5m）", "", "```", b5.groupby("day").agg(
            n=("market_id", "size"), resolved=("resolution", lambda r: int((r >= 0).sum())),
            fee=("fee_rate_bps", lambda f: ",".join(map(str, sorted(set(f.tolist())))))).to_string(), "```", ""]
        late = b5[b5["end_ts"] > UNSEEN_FROM]
        L.append(f"8 月 30 日以后结束的 BTC 5m 市场：{len(late):,} 个，已结算 {int((late['resolution'] >= 0).sum()):,} 个，"
                 f"最后一个结束于 {ms(late['end_ts'].max() * 1000) if len(late) else '-'}。")
        L.append("")
    except Exception as e:
        L += [f"markets.parquet 读取失败：{e}", ""]
    tables = [n for n, _ in keep if n.endswith(".parquet") and ("timeframe=5-minute" in n or "spot" in n)
              and "part-ws" not in n]
    for name in tables:
        L += [f"## {name}", ""]
        try:
            pf, raw = remote_parquet(base + name)
            cols = pf.schema_arrow.names
            tcol = "ts_ms" if "ts_ms" in cols else "timestamp_ms" if "timestamp_ms" in cols else "timestamp"
            rg = _rg_range(pf, tcol)
            L.append(f"{pf.metadata.num_rows:,} 行，{pf.metadata.num_row_groups} 个 row group，列：{', '.join(cols)}")
            unit = 1000 if tcol == "timestamp" else 1
            L += ["", "```"] + [f"rg {g:4d}  {n:>10,} 行  {ms(a * unit if a is not None else None)} → {ms(b * unit if b is not None else None)}"
                                for g, n, a, b in (rg if len(rg) <= 30 else rg[:5] + rg[-25:])] + ["```", ""]
            g = max(range(len(rg)), key=lambda k: rg[k][3] or 0)
            t = pf.read_row_group(g).to_pandas()
            t = t.sort_values(tcol)
            span = (t[tcol].max() - t[tcol].min()) / (1000 if unit == 1 else 1)
            L.append(f"最新的 row group {g}：{len(t):,} 行，{ms(t[tcol].min() * unit)} → {ms(t[tcol].max() * unit)}，"
                     f"平均每秒 {len(t) / max(span, 1):.1f} 行。")
            for c in ("market_id", "symbol", "source", "outcome"):
                if c in t.columns:
                    L.append(f"`{c}` 取值个数 {t[c].nunique():,}；最多的：{t[c].value_counts().head(6).to_dict()}")
            if "market_id" in t.columns:
                one = t[t["market_id"] == t["market_id"].value_counts().index[0]]
                d = one[tcol].diff().dropna()
                L.append(f"更新最多的市场：{len(one):,} 行，相邻两行间隔中位 {d.median():.0f}，90% {d.quantile(0.9):.0f}（{tcol} 单位）。")
            if "symbol" in t.columns:
                for sym, one in t.groupby("symbol"):
                    d = one[tcol].diff().dropna()
                    if len(d):
                        L.append(f"`{sym}`（{one['source'].iloc[0] if 'source' in one else ''}）：{len(one):,} 行，间隔中位 {d.median():.0f}，90% {d.quantile(0.9):.0f}")
            L += ["", "```", t.tail(8).to_string(max_colwidth=40)[:3000], "```", ""]
            L += [f"（这个文件 {raw.size / 1e9:.2f} GB，读了 {raw.fetched / 1e6:.1f} MB）", ""]
        except Exception:
            import traceback
            L += ["```", traceback.format_exc()[-2000:], "```", ""]
        print(name, "done", flush=True)
    L += ["## 币安公开逐笔数据（data.binance.vision）", ""]
    for kind in ("spot", "futures/um"):
        url = f"https://data.binance.vision/data/{kind}/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2026-09-05.zip"
        try:
            req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "research"})
            with urllib.request.urlopen(req, timeout=60) as r:
                L.append(f"- {kind}: HTTP {r.status}，{int(r.headers.get('Content-Length', 0)) / 1e6:.0f} MB")
        except Exception as e:
            L.append(f"- {kind}: {e}")
    Path(out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


BINANCE_WS = ("wss://data-stream.binance.vision/ws/btcusdt@aggTrade", "wss://stream.binance.com:9443/ws/btcusdt@aggTrade",
              "wss://fstream.binance.com/ws/btcusdt@aggTrade", "wss://stream.binance.us:9443/ws/btcusd@aggTrade")
BINANCE_REST = ("https://data-api.binance.vision/api/v3/aggTrades?symbol=BTCUSDT&limit=3",
                "https://api.binance.com/api/v3/aggTrades?symbol=BTCUSDT&limit=3")


def probe_binance_ws(out, seconds=20):
    """Which Binance trade feeds a GitHub runner (US) can reach: messages received per websocket in
    `seconds`, the delay from trade time to receipt, and the REST endpoints' status."""
    import asyncio
    import json
    import time
    import websockets
    L = ["# 币安行情在 GitHub 机器上能不能连（逐笔成交 websocket）", ""]

    async def one(url):
        n, lags, first = 0, [], None
        try:
            async with websockets.connect(url, open_timeout=15, max_size=None) as ws:
                end = time.time() + seconds
                while time.time() < end:
                    try:
                        text = await asyncio.wait_for(ws.recv(), timeout=max(0.1, end - time.time()))
                    except asyncio.TimeoutError:
                        break
                    m = json.loads(text)
                    first = first or text[:200]
                    if "T" in m:
                        lags.append(time.time() * 1000 - float(m["T"]))
                    n += 1
            return f"- {url}: {n} 条 / {seconds} 秒" + (f"，收到延迟中位 {sorted(lags)[len(lags) // 2]:.0f} ms" if lags else "") + \
                (f"\n  样例：`{first}`" if first else "")
        except Exception as e:
            return f"- {url}: 连不上（{type(e).__name__}: {str(e)[:200]}）"

    async def main():
        return [await one(u) for u in BINANCE_WS]

    L += asyncio.run(main())
    L += ["", "REST："]
    for u in BINANCE_REST:
        try:
            L.append(f"- {u}: {_get(u, 20)[:160].decode('utf-8', 'replace')}")
        except Exception as e:
            L.append(f"- {u}: {type(e).__name__}: {str(e)[:200]}")
    Path(out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe")
    p.add_argument("--workdir", default="/tmp/cross")
    p.add_argument("--out", default="real/cross-probe.md")
    p = sub.add_parser("datasets")
    p.add_argument("--workdir", default="/tmp/cross")
    p.add_argument("--out", default="real/cross-datasets.md")
    p = sub.add_parser("cards")
    p.add_argument("--workdir", default="/tmp/cross")
    p.add_argument("--out", default="real/cross-cards.md")
    p = sub.add_parser("probe-ali")
    p.add_argument("--workdir", default="/tmp/cross")
    p.add_argument("--out", default="real/cross-probe-ali.md")
    p = sub.add_parser("probe-binance-ws")
    p.add_argument("--workdir", default="/tmp/cross")
    p.add_argument("--out", default="real/cross-binance-ws.md")
    p = sub.add_parser("probe-trades")
    p.add_argument("--workdir", default="/tmp/cross")
    p.add_argument("--out", default="real/cross-probe-trades.md")
    for name, default in (("analyze", "real/cross-boxes.md"), ("stale", "real/cross-stale.md"),
                          ("makers", "real/cross-makers.md"), ("gated", "real/cross-gated.md"),
                          ("openmis", "real/cross-open.md"), ("hourly", "real/cross-hourly.md"),
                          ("fade", "real/cross-fade.md"), ("follow", "real/cross-follow.md"),
                          ("boxbatch", "real/cross-box-batch.md"),
                          ("cancel", "real/cross-cancel-lead.md"),
                          ("jitter", "real/cross-jitter.md"), ("zoo", "real/cross-zoo100.md"),
                          ("leadlag", "real/cross-leadlag-eth.md"), ("jump2s", "real/cross-jump2s.md"),
                          ("mix", "real/cross-mix-hf.md"), ("regime", "real/cross-regime-windows.csv.gz")):
        p = sub.add_parser(name)
        p.add_argument("days", nargs="?", type=int, help="only the last N daily archives")
        p.add_argument("--workdir", default="/tmp/cross")
        p.add_argument("--out", default=default)
        p.add_argument("--dataset", default=DS, help="another dataset of the same layout")
        if name == "gated":
            p.add_argument("--horizon", type=int, default=5, choices=(5, 15), help="market length in minutes")
        if name == "jitter":
            p.add_argument("--gap-ms", type=int, default=2000, help="0: a row for every candidate print")
            p.add_argument("--shard", help="i/n: every n-th daily archive from the i-th (0-based)")
        if name == "mix":
            migration = p.add_mutually_exclusive_group()
            migration.add_argument("--bundle-proposal", action="store_true",
                                   help="read complete A only and write its exact identity; fit no model")
            migration.add_argument("--bundle-only", action="store_true",
                                   help="read approved complete A only and anchor the fitted model; never read B/C")
        if name in ("gated", "hourly"):
            p.add_argument("--health", action="store_true", help="only sane, recently changed book snapshots")
        if name == "gated":
            p.add_argument("--compete", action="store_true", help="also measure other takers at the same ask")
            p.add_argument("--anchor", type=int, help="prior and move from this many ms before the print")
            p.add_argument("--depth", type=int, default=0, help="also take deeper asks (read this many levels)")
    a = ap.parse_args(argv)
    if getattr(a, "dataset", DS) != DS:
        set_dataset(a.dataset)
    if a.cmd == "probe":
        probe(a.workdir, a.out)
    elif a.cmd == "datasets":
        datasets(a.out)
    elif a.cmd == "cards":
        cards(a.out)
    elif a.cmd == "probe-ali":
        probe_ali(a.out)
    elif a.cmd == "probe-binance-ws":
        probe_binance_ws(a.out)
    elif a.cmd == "probe-trades":
        probe_trades(a.workdir, a.out)
    elif a.cmd == "analyze":
        analyze(a.workdir, a.out, a.days)
    elif a.cmd == "makers":
        makers(a.workdir, a.out, a.days)
    elif a.cmd == "gated":
        gated(a.workdir, a.out, a.days, horizon=a.horizon, health=a.health, compete=a.compete, anchor=a.anchor,
              depth=a.depth)
    elif a.cmd == "openmis":
        openmis(a.workdir, a.out, a.days)
    elif a.cmd == "hourly":
        hourly(a.workdir, a.out, a.days, health=a.health)
    elif a.cmd == "leadlag":
        leadlag(a.workdir, a.out, a.dataset if "-btc-" not in a.dataset else "whodisidk/polymarket-eth-updown-exchange-data",
                a.days)
    elif a.cmd == "zoo":
        import zoo100
        zoo100.run(a.workdir, a.out, a.days, dataset=DS)  # run as a script, this module is not `cross`
    elif a.cmd == "jitter":
        jitter(a.workdir, a.out, a.days, gap_ms=a.gap_ms, shard=a.shard)
    elif a.cmd in ("fade", "follow"):
        fadefollow(a.cmd, a.workdir, a.out, a.days)
    elif a.cmd == "cancel":
        cancel_lead(a.workdir, a.out, a.days)
    elif a.cmd == "boxbatch":
        import cross_box_batch
        cross_box_batch.run(a.workdir, a.out, a.days, dataset=a.dataset)
    elif a.cmd == "jump2s":
        import jump2s_hf
        jump2s_hf.run(a.workdir, a.out, a.days, dataset=DS)  # JUMP2S.md; as a script this module is not `cross`
    elif a.cmd == "mix":
        import mix_hf
        mix_hf.run(a.workdir, a.out, a.days, dataset=DS, bundle_only=a.bundle_only,
                   bundle_proposal=a.bundle_proposal)
    elif a.cmd == "regime":
        import regime_hf
        regime_hf.run(a.workdir, a.out, a.days, dataset=DS)  # REGIME.md; as a script this module is not `cross`
    else:
        stale(a.workdir, a.out, a.days)


if __name__ == "__main__":
    main()
