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
             "down_best_ask", "up_ask_sizes", "down_ask_sizes", "up_bid_sizes", "down_bid_sizes"]
MKT_COLS = ["market_id", "slug", "session_start_ts", "session_end_ts", "chainlink_open_price", "up_won",
            "outcome_direction", "lifecycle_state", "up_token_id", "down_token_id"]


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
                for c in ("up_ask_sizes", "down_ask_sizes", "up_bid_sizes", "down_bid_sizes"):  # size at the best price
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


GATE_THETAS = (0.0, 0.02, 0.04, 0.06, 0.08, 0.12)
GATE_LAGS = (300, 500)


def gated_trades(feat, mkts, binance, z0=2.0, lags=GATE_LAGS, thetas=GATE_THETAS, tau_lo=15, horizon=5, tau_hi=None):
    """Stale-ask sniping gated by the fair-value jump a Binance move implies (`horizon`-minute
    markets, all settled on the 60 s TWAP).

    Every Binance trade (as received) with tau_hi..tau_lo s left (tau_hi defaults to all but the
    first minute: 240 s for 5m, 840 s for 15m) whose log price moved more than z0
    sigma from the last trade at least a second earlier is a candidate. The market's own Up mid
    just before it (the snapshot at receipt) is taken as the prior probability P0; the move shifts
    the expected settlement TWAP by the whole move, so the fair Up price becomes
    Phi(Phi^-1(P0) + dx / (sigma * twap_std_factor(t))). The side of the move is bought at the
    ask L ms after receipt when fair - ask - fee >= theta; the first such trade per market and
    (L, theta) is kept."""
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
    rows = []
    for mid, mk in m5.iterrows():
        if mid not in by:
            continue
        f = by[mid]
        fts = f["timestamp_ms"].to_numpy()
        ub, ua = f["up_best_bid"].to_numpy(), f["up_best_ask"].to_numpy()
        da, uas, das = f["down_best_ask"].to_numpy(), f["up_ask_size"].to_numpy(), f["down_ask_size"].to_numpy()
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
            p0 = min(max((ub[k0] + ua[k0]) / 2, 0.005), 0.995)
            t_mkt = (t0 - (mk["end"] - window * 1000)) / 1000
            fac = float(bo.twap_std_factor(t_mkt, window=window)) * sg[i]
            if not (np.isfinite(fac) and fac > 0):
                continue
            p1 = float(norm.cdf(norm.ppf(p0) + dx[i] / fac))
            up = dx[i] > 0
            fair = p1 if up else 1 - p1
            won = float(mk["up_won"] == (1.0 if up else 0.0))
            for lag in lags:
                k = np.searchsorted(fts, t0 + lag, "left")
                if k >= len(f) or fts[k] > t0 + lag + 1000:
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
                    rows.append((mid, lag, th, t0, (mk["end"] - t0) / 1000, p0, fair, px, size, fee, won, won - px - fee))
    return pd.DataFrame(rows, columns=["market_id", "lag", "theta", "t0", "tau", "p0", "fair", "price", "size", "fee",
                                       "won", "pnl"])


def gated(workdir, out, days=None, reps=5000, horizon=5):
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
            feat, mk, rs = read_day(local)
            binance = read_binance(local)
            Path(local).unlink()
            t = gated_trades(feat, market_table(mk, rs), binance, horizon=horizon)
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
    for c in ("up_token_id", "down_token_id"):
        if c in mk:
            out[c.replace("_id", "")] = g[c].agg(lambda x: str(x.dropna().iloc[-1]) if x.notna().any() else None)
    won = pd.Series(np.nan, index=out.index)
    if "up_won" in mk:
        w = mk.dropna(subset=["up_won"]).groupby("market_id")["up_won"].last()
        won.loc[w.index.intersection(won.index)] = w
    if not rs.empty:  # resolution records: take whatever column says who won
        rs = rs.copy()
        key = next((c for c in ("market_id", "condition_id", "slug") if c in rs), None)
        col = next((c for c in ("up_won", "outcome_direction", "winner", "winning_outcome") if c in rs), None)
        if key == "market_id" and col:
            rs[col] = rs[col].map(lambda x: (x[0] if len(x) else None) if isinstance(x, (list, tuple, np.ndarray)) else x)
            v = rs.dropna(subset=[col]).groupby(rs["market_id"].astype(str))[col].last()
            v = v.map(lambda x: 1.0 if str(x).lower() in ("1", "1.0", "up", "true") else
                      0.0 if str(x).lower() in ("0", "0.0", "-1", "-1.0", "down", "false") else np.nan)
            fill = won.isna() & won.index.isin(v.index)
            won.loc[fill] = v.reindex(won.index[fill]).to_numpy()
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


def hourly_trades(feat, mkts, binance, windows=HOUR_WINDOWS, thetas=HOUR_THETAS, lag_ms=300):
    """1h markets settle on Binance's own 1-hour candle (close >= open), so Binance's price is the
    settlement variable itself: with K the first Binance trade of the hour and S the last trade
    received by second t, the fair Up price is the European digital Phi(ln(S/K) / (sigma*sqrt(tau))),
    sigma the trailing 10 min std of 1 s log returns up to two whole seconds back. Once a second
    (recorder clock) in each window of seconds left, the side whose fair - ask - fee is largest is
    bought at its ask lag_ms later if that edge is at least theta (edge measured on the quote shown
    at t, at most 1 s old); first trade per market, window and theta. Also returns, per market,
    whether the Binance candle agrees with the official outcome."""
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
        checks.append((mid, float(close >= k), float(mk["up_won"])))
        if mid not in by:
            continue
        f = by[mid]
        fts = f["timestamp_ms"].to_numpy()
        ua, da = f["up_best_ask"].to_numpy(), f["down_best_ask"].to_numpy()
        uas, das = f["up_ask_size"].to_numpy(), f["down_ask_size"].to_numpy()
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
            fresh = (q >= 0) & (t - fts[np.maximum(q, 0)] <= 1000)
            a_up, a_dn = ua[np.maximum(q, 0)], da[np.maximum(q, 0)]
            e_up = fair_up - a_up - 0.07 * a_up * (1 - a_up)
            e_dn = (1 - fair_up) - a_dn - 0.07 * a_dn * (1 - a_dn)
            with np.errstate(invalid="ignore"):
                up = np.where(np.isnan(e_dn), True, np.where(np.isnan(e_up), False, e_up >= e_dn))
                edge = np.where(up, e_up, e_dn)
                good = fresh & ok & np.isfinite(edge)
            for th in thetas:
                hit = np.flatnonzero(good & (edge >= th))
                for i in hit:
                    kk = np.searchsorted(fts, t[i] + lag_ms, "left")
                    if kk >= len(f) or fts[kk] > t[i] + lag_ms + 1000:
                        continue
                    side_up = bool(up[i])
                    p = ua[kk] if side_up else da[kk]
                    if not (np.isfinite(p) and 0.02 <= p <= 0.98):
                        continue
                    fee = 0.07 * p * (1 - p)
                    won = won_up if side_up else 1 - won_up
                    rows.append((mid, f"{hi}-{lo}", th, int(t[i]), tau[i], "Up" if side_up else "Down",
                                 float(fair_up[i] if side_up else 1 - fair_up[i]), p, uas[kk] if side_up else das[kk],
                                 fee, won, won - p - fee))
                    break
    cols = ["market_id", "window", "theta", "t", "tau", "side", "fair", "price", "size", "fee", "won", "pnl"]
    return pd.DataFrame(rows, columns=cols), pd.DataFrame(checks, columns=["market_id", "binance_up", "up_won"])


def hourly(workdir, out, days=None, reps=5000):
    """hourly_trades on every day; thresholds and windows are compared on May 25 - Jul 15 and
    checked on the later days."""
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
            t, c = hourly_trades(feat, market_table(mk, rs), binance)
            t["day"] = name[15:25]
            parts.append(t)
            checks.append(c)
            print(f"{name}: {len(t):,} trades, {len(c)} candles checked", flush=True)
        except Exception:
            import traceback
            print(f"{name}: failed\n{traceback.format_exc()}", flush=True)
    df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    ck = pd.concat(checks, ignore_index=True).drop_duplicates("market_id") if checks else pd.DataFrame()
    df.to_csv(Path(out).with_suffix(".csv.gz"), index=False)
    L = [f"# 1 小时市场：按币安 1 小时 K 线算的公平价（{DS}，记录机时钟）", "",
         "1 小时市场按币安 BTCUSDT 1 小时 K 线的收盘 ≥ 开盘结算，所以公平 Up 价 = Φ(ln(S/K) / (σ√τ))："
         "K 是这一小时第一笔币安成交，S 是 t 时刻（记录机收到）最后一笔，σ 是之前 10 分钟每秒对数收益的标准差。"
         "每个剩余时间窗口里每秒看一次，公平价 − 卖一 − taker 费 最大的一方超过 θ 时，300 ms 后按卖一买，"
         "每个市场、窗口、θ 只取第一笔，持有到结算。5 月 25 日–7 月 15 日比较窗口和门槛，之后只核对。探索性质。", ""]
    if len(ck):
        agree = (ck["binance_up"] == ck["up_won"]).mean()
        L += [f"核对：{len(ck):,} 个 1 小时市场里，币安 K 线（收盘 ≥ 开盘）与官方结果一致的比例 {agree:.1%}。", ""]
    if df.empty:
        L.append("没有可用的交易。")
    else:
        df["period"] = np.select([df["day"] <= "2026-07-15", df["day"] <= "2026-08-16"],
                                 ["A 5/25–7/15", "B 7/16–8/16"], "C 8/17–8/29")
        L += ["| 窗口（剩余秒） | θ | 时段 | 笔数 | 胜率 | 平均价 | 平均公平价 | EV/份 | p | 卖一数量中位 |",
              "|---|---:|---|---:|---:|---:|---:|---:|---:|---:|"]
        for (w, th, per), g in df.groupby(["window", "theta", "period"], sort=False):
            pv = bo.fair_price_pvalue(g["pnl"].to_numpy(), (g["price"] + g["fee"]).to_numpy(), sims=reps) \
                if len(g) >= 10 and g["pnl"].mean() > 0 else 1.0
            L.append(f"| {w} | {100 * th:.0f}¢ | {per} | {len(g):,} | {g['won'].mean():.1%} | {g['price'].mean():.3f} | "
                     f"{g['fair'].mean():.3f} | {100 * g['pnl'].mean():+.2f}¢ | {pv:.4f} | {g['size'].median():.0f} |")
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
    p = sub.add_parser("probe-trades")
    p.add_argument("--workdir", default="/tmp/cross")
    p.add_argument("--out", default="real/cross-probe-trades.md")
    for name, default in (("analyze", "real/cross-boxes.md"), ("stale", "real/cross-stale.md"),
                          ("makers", "real/cross-makers.md"), ("gated", "real/cross-gated.md"),
                          ("openmis", "real/cross-open.md"), ("hourly", "real/cross-hourly.md")):
        p = sub.add_parser(name)
        p.add_argument("days", nargs="?", type=int, help="only the last N daily archives")
        p.add_argument("--workdir", default="/tmp/cross")
        p.add_argument("--out", default=default)
        p.add_argument("--dataset", default=DS, help="another dataset of the same layout")
        if name == "gated":
            p.add_argument("--horizon", type=int, default=5, choices=(5, 15), help="market length in minutes")
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
    elif a.cmd == "probe-trades":
        probe_trades(a.workdir, a.out)
    elif a.cmd == "analyze":
        analyze(a.workdir, a.out, a.days)
    elif a.cmd == "makers":
        makers(a.workdir, a.out, a.days)
    elif a.cmd == "gated":
        gated(a.workdir, a.out, a.days, horizon=a.horizon)
    elif a.cmd == "openmis":
        openmis(a.workdir, a.out, a.days)
    elif a.cmd == "hourly":
        hourly(a.workdir, a.out, a.days)
    else:
        stale(a.workdir, a.out, a.days)


if __name__ == "__main__":
    main()
