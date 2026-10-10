"""Run the candidate strategies on your own shadow recordings, on your own machine.

    python run_local.py <path> [<path> ...] [--since 2026-08-23] [--until 2026-10-01]
                        [--split 2026-09-04] [--out local-report.md] [--inspect]

Each <path> may be a shadow runtime database (.sqlite / .db), an engine log (.jsonl / .jsonl.gz),
or a folder (searched recursively) holding either of those or the csv(.gz/.zst) files of an
export (`export/<label>` branches of sgk90-shadow-archive, parts .part-aa ... are joined). Only
market data is read: the Up token's book (poly_probability_observations_v1), the market registry
and outcomes, and Binance trades (BINANCE_AGG_TRADE log events or a binance_*trades* table).
The database is opened read-only; nothing is written anywhere but --out and its .csv.gz.

Strategies (all BTC 5m, buy one side and hold to settlement, taker fee 0.07 p (1 - p)):
- G: test G. A Binance trade with 240..15 s left whose log price moved more than 2 sigma (sigma:
  10 min of 1 s returns) from the last trade 1..5 s earlier; fair Up = Phi(Phi^-1(mid at the
  print) + move / (sigma * TWAP factor)); buy the move's side at the ask 0.3 s after the print if
  fair - ask - fee >= 12c; skip it when the Up mid had already moved 3c or more toward that side
  in the 2 s before the print; first trade per market.
- F: test F, the same without the skip.
- H: the same as F with the prior anchored 2 s before the print (mid then, plus the Binance move
  since), so a quote that already followed Binance is not counted twice.
- M27: once a second with 240..30 s left, Binance's 5 s move above 2.5 sigma * sqrt(5): buy its
  side at the ask 0.3 s later; first per market (rule #27 of zoo100.py).
- with --scale-in also "G 加仓" and "H 加仓": G and H keeping every fill in a market (scaling in),
  the next at least 2 s after the last (equity_compound.py --extra reads them).
- with --episodes, every Binance jump episode (2 s apart) with what scalein.py's rules need, in
  the columns of cross.py jitter, to <out>-episodes.csv.gz; with --prints, every candidate print
  (as G, H and the live test see them) to <out>-prints.csv.gz. scalein.py --extra and
  equity_compound.py --extra read either.
For G, F and H the report also splits by how flat the Up mid was in the 30 s before (range over
32..2 s before the print at most 0.42, the upper tercile cut chosen on May 25 - Jul 15).

Times are the exchange's (book source_ts, Binance trade_ts); with --split the report shows the
periods between the given dates separately (e.g. 2026-09-04, when the 150 ms taker delay began)."""
from __future__ import annotations

import argparse
import gzip
import io
import json
import re
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

import binary as bo
import latency as lt

BOOK_COLS = ["market_id", "source_ts", "receive_ts", "best_bid", "best_ask", "bids_json", "asks_json"]
FLAT_CUT = 0.42


def _sources(paths):
    """(sqlite files, logs as {base: [parts]}, csvs as {base: [parts]}) under the given paths."""
    dbs, logs, csvs = [], {}, {}
    files = []
    for p in map(Path, paths):
        files += [p] if p.is_file() else sorted(q for q in p.rglob("*") if q.is_file())
    for f in files:
        base = re.sub(r"\.part-[a-z]+$", "", f.name)
        key = str(f.parent / base)
        if f.suffix in (".sqlite", ".db") or f.name.endswith(".sqlite3"):
            dbs.append(f)
        elif re.search(r"\.jsonl(\.gz)?$", base):
            logs.setdefault(key, []).append(f)
        elif re.search(r"\.csv(\.gz|\.zst)?$", base):
            csvs.setdefault(key, []).append(f)
    return dbs, logs, csvs


def _bytes(parts):
    return b"".join(q.read_bytes() for q in sorted(parts))


def _csv(key, parts, usecols=None):
    raw = _bytes(parts)
    if key.endswith(".zst"):
        import pyarrow as pa
        raw = pa.CompressedInputStream(pa.BufferReader(raw), "zstd").read()
    elif key.endswith(".gz"):
        raw = gzip.decompress(raw)
    return pd.read_csv(io.BytesIO(raw), usecols=lambda c: usecols is None or c in usecols, low_memory=False)


def _books_frame(df):
    out = pd.DataFrame({"market_id": df["market_id"].astype(str), "ts": pd.to_numeric(df["source_ts"], errors="coerce"),
                        "recv": pd.to_numeric(df.get("receive_ts"), errors="coerce") if "receive_ts" in df else np.nan,
                        "bid": pd.to_numeric(df["best_bid"], errors="coerce"),
                        "ask": pd.to_numeric(df["best_ask"], errors="coerce"),
                        "bid_size": lt.first_size(df["bids_json"]) if "bids_json" in df else np.nan,
                        "ask_size": lt.first_size(df["asks_json"]) if "asks_json" in df else np.nan})
    return out.dropna(subset=["ts"])


def load(paths, inspect=False):
    """Books (exchange time), markets (start_ts, winner) and Binance trades (trade_ts, price)."""
    dbs, logs, csvs = _sources(paths)
    books, reg, outc, bn = [], [], [], []
    for db in dbs:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
        if inspect:
            for t in tables:
                cols = [r[1] for r in con.execute(f'pragma table_info("{t}")')]
                n = con.execute(f'select count(*) from "{t}"').fetchone()[0]
                print(f"{db.name}: {t} ({n:,} rows): {', '.join(cols)}")
        if "poly_probability_observations_v1" in tables:
            have = [r[1] for r in con.execute('pragma table_info("poly_probability_observations_v1")')]
            cols = ", ".join(f'"{c}"' for c in BOOK_COLS if c in have)
            for chunk in pd.read_sql_query(f'select {cols} from poly_probability_observations_v1', con, chunksize=500_000):
                books.append(_books_frame(chunk))
        if "market_registry" in tables:
            reg.append(pd.read_sql_query('select * from market_registry', con))
        if "market_outcomes" in tables:
            outc.append(pd.read_sql_query('select * from market_outcomes', con))
        for t in tables:
            if re.search(r"binance.*trade", t, re.I):
                d = pd.read_sql_query(f'select * from "{t}"', con)
                if {"trade_ts", "price"} <= set(d.columns):
                    bn.append(d[["trade_ts", "price"]])
        con.close()
    for key, parts in csvs.items():
        name = Path(key).name
        if inspect:
            print(f"csv: {name} ({len(parts)} part(s))")
        if "poly_probability_observations_v1.csv" in name:
            books.append(_books_frame(_csv(key, parts, set(BOOK_COLS))))
        elif "market_registry.csv" in name:
            reg.append(_csv(key, parts))
        elif "market_outcomes.csv" in name:
            outc.append(_csv(key, parts))
        elif re.search(r"binance.*trade", name, re.I):
            d = _csv(key, parts)
            if {"trade_ts", "price"} <= set(d.columns):
                bn.append(d[["trade_ts", "price"]])
    for key, parts in logs.items():
        raw = _bytes(parts)
        if key.endswith(".gz"):
            raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
        rows = []
        for line in raw.decode("utf-8", "replace").splitlines():
            if '"BINANCE_AGG_TRADE"' not in line:
                continue
            try:
                e = json.loads(line)
                rows.append((float(e["trade_ts"]), float(e["price"])))
            except (ValueError, KeyError, TypeError):
                continue
        if inspect:
            print(f"log: {Path(key).name}: {len(rows):,} BINANCE_AGG_TRADE events")
        if rows:
            bn.append(pd.DataFrame(rows, columns=["trade_ts", "price"]))
    if not books or not reg or not outc or not bn:
        missing = [n for n, v in (("books", books), ("market_registry", reg), ("market_outcomes", outc),
                                  ("Binance trades", bn)) if not v]
        raise SystemExit(f"missing {', '.join(missing)} under {', '.join(map(str, paths))} (try --inspect)")
    books = pd.concat(books, ignore_index=True).drop_duplicates(subset=["market_id", "ts", "bid", "ask"])
    books = books.sort_values(["market_id", "ts"], kind="stable").reset_index(drop=True)
    reg = pd.concat(reg, ignore_index=True)
    outc = pd.concat(outc, ignore_index=True)
    markets = (reg[["market_id", "start_ts"]].assign(market_id=lambda d: d["market_id"].astype(str))
               .drop_duplicates("market_id")
               .merge(outc[["market_id", "winner"]].assign(market_id=lambda d: d["market_id"].astype(str))
                      .dropna().drop_duplicates("market_id"), on="market_id"))
    markets["start_ts"] = pd.to_numeric(markets["start_ts"], errors="coerce")
    markets["winner"] = markets["winner"].astype(str).str.capitalize()
    markets = markets[markets["winner"].isin(["Up", "Down"])].dropna(subset=["start_ts"])
    bn = pd.concat(bn, ignore_index=True).astype(float)
    if bn["trade_ts"].median() > 1e11:  # milliseconds
        bn["trade_ts"] /= 1000
    bn = bn.drop_duplicates().sort_values("trade_ts", kind="stable").reset_index(drop=True)
    return books, markets, bn


def momentum5(markets, spot, sigma, book, z=2.5, lag=0.3):
    """Rule M27: once a second with 240..30 s left, Binance's 5 s move above z sigma sqrt(5)."""
    ts, lp = spot["trade_ts"].to_numpy(), np.log(spot["price"].to_numpy())
    rows = []
    for m in markets.itertuples():
        end = m.start_ts + bo.WINDOW_S
        for tau in range(240, 29, -1):
            x = end - tau
            i, j = np.searchsorted(ts, [x, x - 5], "right") - 1
            if j < 0:
                continue
            sg = sigma.get(int(np.floor(x)) - 1, np.nan)
            r = (lp[i] - lp[j]) / (sg * np.sqrt(5)) if np.isfinite(sg) and sg > 0 else np.nan
            if not (np.isfinite(r) and abs(r) > z):
                continue
            side = "Up" if r > 0 else "Down"
            if not book.alive(m.market_id, x + lag, *lt.C_ALIVE):
                continue
            px, size = book.side_ask(m.market_id, x + lag, side)
            if not (np.isfinite(px) and 0.02 <= px <= 0.98):
                continue
            fee = float(bo.taker_fee(px))
            won = float(m.winner == side)
            rows.append((won, px, fee, won - px - fee, size, x, m.market_id, np.nan, np.nan, lag))
            break
    return pd.DataFrame(rows, columns=["won", "price", "fee", "pnl", "size", "t", "market_id", "p0", "fair", "lag"])


def add_context(t, book):
    """pre: the Up mid's move toward the side bought over the 2 s before the trigger; range30: the
    range of the Up mid from 32 to 2 s before it."""
    if t.empty:
        return t
    pre, rng = [], []
    for r in t.itertuples():
        ts, v = book.by[r.market_id]
        mids = (v[:, 0] + v[:, 1]) / 2
        q0, q2 = book.at(r.market_id, r.t), book.at(r.market_id, r.t - 2.0)
        side_up = abs(book.side_ask(r.market_id, r.t + r.lag, "Up")[0] - r.price) < 1e-9  # the side bought
        a, b = np.searchsorted(ts, [r.t - 32.0, r.t - 2.0])
        w = mids[a:b]
        w = w[np.isfinite(w)]
        rng.append(float(w.max() - w.min()) if len(w) else np.nan)
        move = np.nan if q0 is None or q2 is None else (q0[0] + q0[1]) / 2 - (q2[0] + q2[1]) / 2
        pre.append(move if side_up else -move)
    t = t.copy()
    t["mid_move2"], t["range30"] = pre, rng
    return t


def summary(t, reps):
    if t.empty:
        return "0 笔"
    days = max(pd.to_datetime(t["t"], unit="s", utc=True).dt.date.nunique(), 1)
    p = bo.fair_price_pvalue(t["pnl"].to_numpy(), (t["price"] + t["fee"]).to_numpy(), sims=reps) \
        if len(t) >= 10 and t["pnl"].mean() > 0 else 1.0
    roi = t["pnl"].sum() / (t["price"] + t["fee"]).sum()
    usd = (t["pnl"] * t["size"].clip(upper=40)).sum() / days
    return (f"{len(t):,} 笔（{len(t) / days:.0f} 笔/天）| 胜率 {t['won'].mean():.1%} | 均价 {t['price'].mean():.3f} | "
            f"每份 {100 * t['pnl'].mean():+.2f}¢ | p = {p:.4f} | 每投 1 美元 {100 * roi:+.1f} 美分 | "
            f"卖一数量中位 {t['size'].median():.0f} | 按每笔最多 40 份 ${usd:,.0f}/天")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--since", help="markets starting on/after this UTC date")
    ap.add_argument("--until", help="markets starting before this UTC date")
    ap.add_argument("--split", nargs="*", default=[], help="dates that split the report into periods")
    ap.add_argument("--out", default="local-report.md")
    ap.add_argument("--reps", type=int, default=20000)
    ap.add_argument("--inspect", action="store_true", help="list what was found and stop")
    ap.add_argument("--scale-in", action="store_true", help="also G and H keeping every fill per market")
    ap.add_argument("--episodes", action="store_true", help="also write every jump episode for scalein.py")
    ap.add_argument("--prints", action="store_true", help="also write every candidate print for scalein.py")
    a = ap.parse_args(argv)
    books, markets, spot = load(a.paths, inspect=a.inspect)
    if a.inspect:
        print(f"books {len(books):,} rows over {books['market_id'].nunique():,} markets; markets with outcomes "
              f"{len(markets):,}; Binance trades {len(spot):,} "
              f"({pd.to_datetime(spot['trade_ts'].min(), unit='s')} → {pd.to_datetime(spot['trade_ts'].max(), unit='s')})")
        return
    if a.since:
        markets = markets[markets["start_ts"] >= pd.Timestamp(a.since, tz="UTC").timestamp()]
    if a.until:
        markets = markets[markets["start_ts"] < pd.Timestamp(a.until, tz="UTC").timestamp()]
    markets = markets[markets["market_id"].isin(set(books["market_id"]))]
    _, sigma = lt.spot_grid(spot, max_gap=lt.C_MAX_GAP)
    book = lt.Book(books)
    kw = dict(lag=0.3, theta=0.12, z0=2.0, tau_lo=15)
    strategies = {
        "G（检验 G）": lambda: lt.gated_trades(markets, spot, sigma, book, max_pre=0.03, **kw),
        "F（检验 F）": lambda: lt.gated_trades(markets, spot, sigma, book, **kw),
        "H（2 秒前起算）": lambda: lt.gated_trades(markets, spot, sigma, book, anchor=2.0, **kw),
        "M27（币安 5 秒动量）": lambda: momentum5(markets, spot, sigma, book),
    }
    if a.scale_in:
        strategies["G 加仓"] = lambda: lt.gated_trades(markets, spot, sigma, book, max_pre=0.03, every=2.0, **kw)
        strategies["H 加仓"] = lambda: lt.gated_trades(markets, spot, sigma, book, anchor=2.0, every=2.0, **kw)
    cuts = [pd.Timestamp(d, tz="UTC").timestamp() for d in sorted(a.split)]
    span = (pd.to_datetime(markets["start_ts"].min(), unit="s"), pd.to_datetime(markets["start_ts"].max(), unit="s"))
    L = ["# 本地 shadow 数据上的候选策略", "",
         f"市场 {len(markets):,} 个（{span[0]:%Y-%m-%d %H:%M} → {span[1]:%Y-%m-%d %H:%M} UTC），盘口 {len(books):,} 行，"
         f"币安成交 {len(spot):,} 笔。规则见 run_local.py 开头；时间都是交易所时间戳。", ""]
    allt = []
    for name, fn in strategies.items():
        t = fn()
        t = t.assign(strategy=name)
        L += [f"## {name}", "", f"- 全部：{summary(t, a.reps)}"]
        if cuts and len(t):
            edges = [-np.inf] + cuts + [np.inf]
            labels = ["起点"] + sorted(a.split)
            for lo, hi, lab in zip(edges[:-1], edges[1:], labels):
                g = t[(t["t"] >= lo) & (t["t"] < hi)]
                L.append(f"- {lab} 起：{summary(g, a.reps)}")
        if name[0] in "GFH" and len(t):
            t = add_context(t, book)
            flat = t["range30"] <= FLAT_CUT
            L += [f"- 触发前 30 秒平静（中间价幅度 ≤ {FLAT_CUT}）：{summary(t[flat], a.reps)}",
                  f"- 触发前 30 秒波动大：{summary(t[~flat], a.reps)}"]
        L.append("")
        allt.append(t)
    trades = pd.concat(allt, ignore_index=True)
    trades.to_csv(Path(a.out).with_suffix(".csv.gz"), index=False)
    for flag, gap, suffix, what in ((a.episodes, 2.0, "episodes", "急动段"), (a.prints, 0.0, "prints", "逐笔候选")):
        if flag:
            ep = lt.episode_rows(markets, spot, sigma, book, lag=kw["lag"], z0=kw["z0"], tau_lo=kw["tau_lo"], gap=gap)
            ep.to_csv(Path(a.out).with_name(f"{Path(a.out).stem}-{suffix}.csv.gz"), index=False)
            L += [f"{what}（scalein.py 用）：{len(ep):,} 行，盘口健康的 {int(ep['ok'].sum()) if len(ep) else 0:,} 行。"]
    Path(a.out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
