"""Risk-free merge / split arbitrage scan on Polymarket BTC Up/Down books (ROUNDTRIP.md, family 8).
Paper research on recorded market data only: nothing here places or signs an order.

- Merge: buy one Up and one Down at the two best asks, merge the pair into 1 USDC (CTF merge).
  Edge per pair = 1 - ask_up - ask_down - fee(ask_up) - fee(ask_down).
- Split: split 1 USDC into one Up and one Down, sell both at the two best bids.
  Edge per pair = bid_up + bid_down - fee(bid_up) - fee(bid_down) - 1.
- fee(p) = binary.taker_fee(p, rate): both legs are taker trades. Merging and splitting are contract
  calls with no trading fee; the Polygon gas they cost (per transaction, independent of size) is
  ignored here, which flatters every number below.

Three sources, one core (`scan`):

    python arb_scan.py kacho --kacho DIR --out real/arb-scan-kacho.md       # kacho.io 1 s rows (local)
    python arb_scan.py hf --workdir DIR [--days N] --out real/arb-scan-hf.md  # whodisidk 100 ms, 5m/15m/1h
    python arb_scan.py forward ROOT --out real/arb-scan-forward.md          # GitHub forward recordings

Definitions (fixed in ROUNDTRIP.md, or the conservative choice where it is silent):
- "raw": the inequality alone, at any price and size. "tradable": also both legs priced within
  0.02..0.98 and at least 5 shares shown at both touches (ROUNDTRIP.md's entry rule).
- Size of a pair = the smaller of the two touch sizes (merge: the two ask sizes; split: the two bid
  sizes). Dollars = edge x min(size, 100), taken once per episode, divided by the calendar days in the
  sample (days with any data).
- Episode: a maximal run of consecutive rows of one market that satisfy the condition (kacho: rows
  1 s apart; hf: snapshots at most 150 ms apart; forward: consecutive book events). Duration = time
  from its first row to the first later row of the market that no longer satisfies it.
- A row is one snapshot of a cache, so a one-row "arb" may be two halves of the book sampled at
  different moments. Hence "persists >= 2 rows" and the execution check:
  kacho (ROUNDTRIP.md timing): the row stamped s is usable from s + 1, the orders go in then and fill
  at the prices of the row stamped s + 2. hf and forward: fill at the snapshot shown 0.5 s after the
  one that showed the arb. An episode is executable if some row of it is followed, lag later, by a
  row (the last one stamped at or before that moment, at most 5 s old, before the market's end)
  where the condition (same level) still holds; it is traded once, at the first such row, at that
  later row's edge and size. Both legs are assumed to fill together at that row; the risk of one
  leg filling and the other not is not modelled.
- Crossed: a token's ask below its own bid. No CLOB lets that stand, so such a row is a stale or
  broken snapshot, not a quote anyone could trade; episodes starting on one are flagged.
- Mirror: Polymarket's CLOB matches complementary orders (a bid for Up at p also offers Down at
  1 - p; buys of both summing to >= 1 are matched by minting a pair, sells summing to <= 1 by merging
  one). A book that shows those synthetic orders has ask_down = 1 - bid_up and bid_down = 1 - ask_up,
  so ask_up + ask_down = 1 + spread_up >= 1 and neither arb can exist by construction. The share of
  rows that mirror exactly is reported for each source.
- Fee rate: cross.taker_rate (0.072 before 2026-07-01, 0.07 after), the higher of ROUNDTRIP.md's
  0.07 and the rate actually charged then.
- Splits: A = markets starting 2026-03-24..04-25 (UTC), B = 04-26..05-18 (kacho only).
- hf: only rows whose lifecycle_state is active (or missing) and not halted, within the market's
  window [start, end), one row per market and timestamp (as cross.boxes). The feature lists are
  best-first (checked on 2026-08-29: the first element equals the best price on every row), so
  first-level sizes are the sizes at the touch. Archives are read by read_day_lean (same columns
  and sizes as cross.read_day, tested against it) because cross.read_day peaked at ~11 GB on a May
  archive; windows come from cross.market_table, downloads from cross.fetch / cross.archives.
- forward: the latency/ files written by recording.LatencyFiles hold the Up token's book only; Down
  quotes there can only be written as 1 - Up bid / 1 - Up ask, which makes merge cost = 1 + spread
  and split proceeds = 1 - spread identically. That is detected and reported instead of a number.
  If the bundle's own data/polymarket/daily/{best_bid_ask,book,markets} files sit beside latency/
  (both tokens, receive-time stamped; extract them from raw.tgz), the real two-token scan runs on
  them: prices from best_bid_ask (every change), sizes from the per-second book rows where their
  best price equals it. These are change events, so the 5 s age limit does not apply to them.
"""
from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path

import numpy as np
import pandas as pd

import binary as bo
import cross

PRICE_LO, PRICE_HI, MIN_SHARES, CAP = 0.02, 0.98, 5.0, 100.0
MAX_AGE_MS = 5000
KACHO_LAG_MS, HF_LAG_MS = 2000, 500
A_FROM, B_FROM, B_UNTIL = "2026-03-24", "2026-04-26", "2026-05-19"
TAU_BINS = (0, 15, 30, 60, 120, 180, 240, 300, 900, 1800, 3600)
COLS = ["market", "ts_ms", "start_ms", "end_ms", "horizon", "bu", "au", "bd", "ad", "bu_sz", "au_sz", "bd_sz", "ad_sz"]
KINDS = ("merge", "split")
EP_COLS = ["kind", "level", "market", "horizon", "start_ms", "ts_ms", "rows", "dur_s", "tau", "edge0", "size0", "sum0",
           "crossed0", "exec", "exec_ts_ms", "exec_edge", "exec_size", "exec_crossed", "usd"]
KIND_ZH = {"merge": "合并（两边卖一买入，合成 1）", "split": "拆分（1 拆两边，两边买一卖出）"}
KIND_SHORT = {"merge": "合并", "split": "拆分"}


# ------------------------------------------------------------------------------------- core

def edges(f):
    """Per row and kind: (edge per pair, pair size, raw ok, tradable ok, sum of the two prices)."""
    rate = cross.taker_rate(f["ts_ms"].to_numpy())
    bu, au, bd, ad = (f[c].to_numpy(float) for c in ("bu", "au", "bd", "ad"))
    bus, aus, bds, ads = (f[c].to_numpy(float) for c in ("bu_sz", "au_sz", "bd_sz", "ad_sz"))

    def priced(*ps):
        ok = np.ones(len(f), bool)
        for p in ps:
            ok &= (p >= PRICE_LO) & (p <= PRICE_HI)
        return ok

    with np.errstate(invalid="ignore"):
        merge = 1.0 - au - ad - bo.taker_fee(au, rate) - bo.taker_fee(ad, rate)
        split = bu + bd - bo.taker_fee(bu, rate) - bo.taker_fee(bd, rate) - 1.0
        m_sz, s_sz = np.minimum(aus, ads), np.minimum(bus, bds)
        out = {"merge": (merge, m_sz, merge > 0, (merge > 0) & priced(au, ad) & (m_sz >= MIN_SHARES), au + ad),
               "split": (split, s_sz, split > 0, (split > 0) & priced(bu, bd) & (s_sz >= MIN_SHARES), bu + bd)}
    return out


def row_stats(f):
    """Counts over all rows: mirror share, crossed rows, raw rows of each arb with and without fees."""
    bu, au, bd, ad = (f[c].to_numpy(float) for c in ("bu", "au", "bd", "ad"))
    e = edges(f)
    with np.errstate(invalid="ignore"):
        both = np.isfinite(bu) & np.isfinite(au) & np.isfinite(bd) & np.isfinite(ad)
        mirror = both & (np.abs(ad - (1 - bu)) < 1e-9) & (np.abs(bd - (1 - au)) < 1e-9)
        crossed = (au < bu - 1e-9) | (ad < bd - 1e-9)
        return {"rows": len(f), "both": int(both.sum()), "mirror": int(mirror.sum()), "crossed": int(crossed.sum()),
                "merge_nofee": int((au + ad < 1 - 1e-9).sum()), "split_nofee": int((bu + bd > 1 + 1e-9).sum()),
                "merge_raw": int(e["merge"][2].sum()), "split_raw": int(e["split"][2].sum()),
                "merge_tr": int(e["merge"][3].sum()), "split_tr": int(e["split"][3].sum()),
                "markets": int(f["market"].nunique())}


def scan(f, lag_ms, gap_ms, step_ms, max_age_ms=MAX_AGE_MS):
    """Episodes of both arbs at both levels in frame `f` (columns COLS, any row order).

    lag_ms: execution delay (the row at or before ts + lag_ms, at most max_age_ms old, is where the
    pair fills); gap_ms: rows further apart than this break an episode; step_ms: duration credited to
    an episode that runs into the market's last row. max_age_ms=None disables the age limit."""
    f = f.sort_values(["market", "ts_ms"], kind="stable").reset_index(drop=True)
    if f.empty:
        return pd.DataFrame(columns=EP_COLS)
    code = pd.factorize(f["market"])[0].astype(np.int64)
    ts = f["ts_ms"].to_numpy(np.int64)
    end = f["end_ms"].to_numpy(np.int64)
    key = code * 10**13 + ts
    j = np.searchsorted(key, key + lag_ms, "right") - 1  # never below the row itself, never another market
    age = ts + lag_ms - ts[j]
    fill_ok = (ts + lag_ms < end) if max_age_ms is None else (ts + lag_ms < end) & (age <= max_age_ms)
    same_next = np.r_[code[1:] == code[:-1], False]
    nxt = np.minimum(np.arange(len(f)) + 1, len(f) - 1)
    linked = np.r_[False, (code[1:] == code[:-1]) & (ts[1:] - ts[:-1] <= gap_ms)]
    with np.errstate(invalid="ignore"):
        crossed = ((f["au"] < f["bu"] - 1e-9) | (f["ad"] < f["bd"] - 1e-9)).to_numpy()
    tau = (end - ts) / 1000.0
    parts = []
    for kind, (edge, size, raw, tradable, psum) in edges(f).items():
        for level, ok in (("raw", raw), ("tradable", tradable)):
            if not ok.any():
                continue
            start = ok & ~(np.r_[False, ok[:-1]] & linked)
            eid = np.cumsum(start) - 1
            idx = np.flatnonzero(ok)
            e_of = eid[idx]
            first = idx[np.r_[True, e_of[1:] != e_of[:-1]]]
            last = idx[np.r_[e_of[1:] != e_of[:-1], True]]
            rows = last - first + 1
            stop = np.where(same_next[last], ts[nxt[last]], np.minimum(ts[last] + step_ms, end[last]))
            persist = ok & fill_ok & ok[j]
            pi = np.flatnonzero(persist)
            ep = pd.DataFrame({"kind": kind, "level": level, "market": f["market"].to_numpy()[first],
                               "horizon": f["horizon"].to_numpy()[first], "start_ms": f["start_ms"].to_numpy()[first],
                               "ts_ms": ts[first], "rows": rows, "dur_s": (stop - ts[first]) / 1000.0,
                               "tau": tau[first], "edge0": edge[first], "size0": size[first], "sum0": psum[first],
                               "crossed0": crossed[first], "exec": False, "exec_ts_ms": np.nan,
                               "exec_edge": np.nan, "exec_size": np.nan, "exec_crossed": False})
            if len(pi):
                first_pi = pd.Series(pi).groupby(eid[pi]).first()
                k = first_pi.index.to_numpy()  # episode numbers (0..) that execute, in order
                r = first_pi.to_numpy()
                ep.loc[k, "exec"] = True
                ep.loc[k, "exec_ts_ms"] = ts[j[r]]
                ep.loc[k, "exec_edge"] = edge[j[r]]
                ep.loc[k, "exec_size"] = size[j[r]]
                ep.loc[k, "exec_crossed"] = crossed[j[r]]
            ep["usd"] = np.where(ep["exec"], ep["exec_edge"] * np.minimum(ep["exec_size"], CAP), 0.0)
            parts.append(ep)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=EP_COLS)


# ------------------------------------------------------------------------------- sources

def kacho_frame(ticks, markets):
    """kacho.io ticks + markets -> COLS. Column meanings (README, checked on the data): bu/au and bd/ad
    are the Up and Down best bid/ask; su/sd the sizes at the Up/Down best BID; sau/sad the sizes at
    the Up/Down best ASK (su equals sad wherever the two books mirror); du/dd bid depth (unused)."""
    m = markets[["condition_id", "market_start", "market_end"]].copy()
    ms = lambda c: (pd.to_datetime(m[c], utc=True) - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(milliseconds=1)  # noqa: E731
    m["start_ms"], m["end_ms"] = ms("market_start"), ms("market_end")
    t = ticks.merge(m[["condition_id", "start_ms", "end_ms"]], on="condition_id")
    return pd.DataFrame({"market": t["condition_id"].to_numpy(), "ts_ms": t["t"].to_numpy(np.int64) * 1000,
                         "start_ms": t["start_ms"].to_numpy(), "end_ms": t["end_ms"].to_numpy(), "horizon": 5,
                         "bu": t["bu"].to_numpy(float), "au": t["au"].to_numpy(float), "bd": t["bd"].to_numpy(float),
                         "ad": t["ad"].to_numpy(float), "bu_sz": t["su"].to_numpy(float),
                         "au_sz": t["sau"].to_numpy(float), "bd_sz": t["sd"].to_numpy(float),
                         "ad_sz": t["sad"].to_numpy(float)})


def load_kacho(kacho):
    kacho = Path(kacho)
    markets = pd.read_parquet(kacho / "btc_markets.parquet", columns=["condition_id", "market_start", "market_end"])
    ticks = pd.read_parquet(kacho / "btc_ticks.parquet",
                            columns=["condition_id", "t", "bu", "au", "bd", "ad", "su", "sd", "sau", "sad"])
    return ticks, markets


def hf_frame(feat, mkts):
    """cross.read_day features + cross.market_table -> COLS (active, not halted, inside the window)."""
    if feat.empty or mkts.empty:
        return pd.DataFrame(columns=COLS)
    keep = np.ones(len(feat), bool)
    if "lifecycle_state" in feat:
        keep &= (feat["lifecycle_state"].isin(["active", "open", "trading"]) | feat["lifecycle_state"].isna()).to_numpy()
    if "observed_halt_flag" in feat:
        keep &= ~feat["observed_halt_flag"].fillna(False).astype(bool).to_numpy()
    src = {"bu": "up_best_bid", "au": "up_best_ask", "bd": "down_best_bid", "ad": "down_best_ask",
           "bu_sz": "up_bid_size", "au_sz": "up_ask_size", "bd_sz": "down_bid_size", "ad_sz": "down_ask_size"}
    f = feat.loc[keep, ["market_id", "timestamp_ms"] + [c for c in src.values() if c in feat]]
    f = f.drop_duplicates(["market_id", "timestamp_ms"])
    m = mkts[["market_id", "start", "end", "horizon"]].dropna(subset=["start", "end"]).drop_duplicates("market_id")
    m = m.set_index(m["market_id"].astype(str))
    mid = f["market_id"].astype(str)
    start, end = mid.map(m["start"]).to_numpy(float), mid.map(m["end"]).to_numpy(float)
    ts = f["timestamp_ms"].to_numpy(np.int64)
    w = np.isfinite(start) & (ts >= start) & (ts < end)
    out = pd.DataFrame({"market": mid.to_numpy()[w], "ts_ms": ts[w], "start_ms": start[w].astype(np.int64),
                        "end_ms": end[w].astype(np.int64), "horizon": mid.map(m["horizon"]).to_numpy(float)[w]})
    for k, c in src.items():
        out[k] = f[c].to_numpy(float)[w] if c in f else np.nan
    return out


def latency_degenerate(books):
    """Whether a latency.load_books frame holds one token only (then Down = 1 - Up and both arbs are
    an identity, not a measurement), with the numbers that show it."""
    one_token = not any(c.startswith(("down", "bd", "ad")) or c in ("token", "asset_id", "side") for c in books.columns)
    bid, ask = books["bid"].to_numpy(float), books["ask"].to_numpy(float)
    ok = np.isfinite(bid) & np.isfinite(ask)
    spread = ask[ok] - bid[ok]
    return {"one_token": one_token, "rows": int(len(books)), "markets": int(books["market_id"].nunique()),
            "quoted": int(ok.sum()), "crossed": int((spread < -1e-9).sum()),
            "min_spread": float(spread.min()) if len(spread) else np.nan,
            "median_spread": float(np.median(spread)) if len(spread) else np.nan}


def _jsonl(paths):
    for p in paths:
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            try:
                for line in fh:
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue
            except (EOFError, OSError):  # a truncated gzip stream: keep what was read
                continue


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return np.nan


def bundle_files(bundle):
    d = Path(bundle) / "data" / "polymarket" / "daily"
    return {k: sorted((d / k).glob("*/*.jsonl.gz")) for k in ("best_bid_ask", "book", "markets")}


def forward_frame(markets, bba, book):
    """Two-token frame (COLS) from bundle rows: markets (slug, start_sec, end_sec, up_token, down_token),
    bba (token, recv_ms, bid, ask) and book (token, recv_ms, bid, bid_sz, ask, ask_sz: best levels)."""
    if markets.empty or bba.empty:
        return pd.DataFrame(columns=COLS)
    side = {}
    for m in markets.itertuples():
        side[m.up_token], side[m.down_token] = (m.slug, "u"), (m.slug, "d")
    q = bba[bba["token"].isin(side)].copy()
    q["slug"] = q["token"].map(lambda x: side[x][0])
    q["s"] = q["token"].map(lambda x: side[x][1])
    if not book.empty:  # first-level sizes where the per-second book's best price equals the quote
        b = book[book["token"].isin(side)].sort_values("recv_ms")
        q = pd.merge_asof(q.sort_values("recv_ms"), b.rename(columns={"bid": "b_bid", "ask": "b_ask"}),
                          on="recv_ms", by="token")
        q["bid_sz"] = np.where(np.isclose(q["b_bid"], q["bid"]), q["bid_sz"], np.nan)
        q["ask_sz"] = np.where(np.isclose(q["b_ask"], q["ask"]), q["ask_sz"], np.nan)
    else:
        q["bid_sz"] = q["ask_sz"] = np.nan
    out = []
    mk = markets.set_index("slug")
    for slug, g in q.groupby("slug", sort=False):
        g = g.sort_values("recv_ms", kind="stable")
        w = pd.DataFrame({"ts_ms": g["recv_ms"].to_numpy(np.int64)})
        for s, cols in (("u", ("bu", "au", "bu_sz", "au_sz")), ("d", ("bd", "ad", "bd_sz", "ad_sz"))):
            # each token's state carries forward from its own last event (an empty side stays empty)
            last = np.maximum.accumulate(np.where((g["s"] == s).to_numpy(), np.arange(len(g)), -1))
            for c, src in zip(cols, ("bid", "ask", "bid_sz", "ask_sz")):
                v = g[src].to_numpy(float)
                w[c] = np.where(last >= 0, v[np.maximum(last, 0)], np.nan)
        w = w.groupby("ts_ms", as_index=False).last()  # the state after every message of that millisecond
        r = mk.loc[slug]
        w["market"], w["start_ms"], w["end_ms"], w["horizon"] = slug, int(r.start_sec) * 1000, int(r.end_sec) * 1000, 5
        out.append(w[(w["ts_ms"] >= w["start_ms"]) & (w["ts_ms"] < w["end_ms"])])
    return pd.concat(out, ignore_index=True)[COLS] if out else pd.DataFrame(columns=COLS)


def load_bundle(bundle):
    """(markets, bba, book) frames of one bundle's two-token files, or None if they are not there."""
    files = bundle_files(bundle)
    if not files["best_bid_ask"] or not files["markets"]:
        return None
    mk = []
    for m in _jsonl(files["markets"]):
        try:
            outcomes = json.loads(m["raw"]["outcomes"]) if isinstance(m["raw"]["outcomes"], str) else m["raw"]["outcomes"]
            up_i = outcomes.index("Up")
            mk.append((m["slug"], int(m["start_sec"]), int(m["end_sec"]), str(m["token_ids"][up_i]),
                       str(m["token_ids"][1 - up_i])))
        except (KeyError, ValueError, TypeError, IndexError):
            continue
    markets = pd.DataFrame(mk, columns=["slug", "start_sec", "end_sec", "up_token", "down_token"]).drop_duplicates("slug")
    toks = set(markets["up_token"]) | set(markets["down_token"])
    bba = pd.DataFrame([(str(d["asset_id"]), int(d["recv_ms"]), _num(d["payload"].get("best_bid")),
                         _num(d["payload"].get("best_ask")))
                        for d in _jsonl(files["best_bid_ask"]) if str(d.get("asset_id")) in toks],
                       columns=["token", "recv_ms", "bid", "ask"])
    rows = []
    for d in _jsonl(files["book"]):
        if str(d.get("asset_id")) not in toks:
            continue
        p = d.get("payload") or {}
        bids = [(_num(x.get("price")), _num(x.get("size"))) for x in p.get("bids", [])]
        asks = [(_num(x.get("price")), _num(x.get("size"))) for x in p.get("asks", [])]
        bb = max(bids, default=(np.nan, np.nan))
        ba = min(asks, default=(np.nan, np.nan))
        rows.append((str(d["asset_id"]), int(d["recv_ms"]), bb[0], bb[1], ba[0], ba[1]))
    book = pd.DataFrame(rows, columns=["token", "recv_ms", "bid", "bid_sz", "ask", "ask_sz"])
    return markets, bba, book


# ------------------------------------------------------------------------------- reports

def _c(x):
    return f"{100 * x:+.2f}¢" if np.isfinite(x) else "–"


def _med(s):
    s = pd.Series(s).dropna()
    return float(s.median()) if len(s) else np.nan


def summary_table(ep, ndays, kinds=KINDS):
    """Markdown rows, one per kind: the funnel from raw episodes to executable dollars."""
    L = ["| | " + " | ".join(KIND_ZH[k] for k in kinds) + " |", "|---|" + "---:|" * len(kinds)]
    cell = {}
    for k in kinds:
        e = ep[ep["kind"] == k]
        raw, tr = e[e["level"] == "raw"], e[e["level"] == "tradable"]
        ex = tr[tr["exec"].astype(bool)]
        size = lambda s: f"{_med(s):.0f}" if len(s) else "–"  # noqa: E731
        cell[k] = [
            f"{len(raw):,}", f"{int(raw['crossed0'].astype(bool).sum()):,}", f"{int((raw['rows'] >= 2).sum()):,}",
            f"{len(tr):,}", f"{int((tr['rows'] >= 2).sum()):,}",
            f"{_med(tr['dur_s']):.1f}" if len(tr) else "–",
            f"{_c(_med(tr['edge0']))} × {size(tr['size0'])}",
            f"{len(ex):,}（交叉 {int(ex['exec_crossed'].astype(bool).sum())}）",
            f"{_c(_med(ex['exec_edge']))} × {size(ex['exec_size'])}",
            f"${ex['usd'].sum() / max(ndays, 1):,.2f}"]
        clean = ex[~ex["crossed0"].astype(bool) & ~ex["exec_crossed"].astype(bool)]
        cell[k] += [f"{len(clean):,}", f"${clean['usd'].sum() / max(ndays, 1):,.2f}"]
    names = ["回合（不限价格档和挂单量）", "其中首行盘口交叉（坏行）", "其中持续 ≥ 2 行", "可成交回合（0.02–0.98，两边 ≥ 5 份）",
             "其中持续 ≥ 2 行", "可成交回合持续秒数中位", "首行每对利润中位 × 数量中位", "按规定延迟仍成立（可执行）",
             "执行时每对利润中位 × 数量中位", "每天美元（每回合 ≤ 100 对）",
             "可执行且看到时、成交时都不交叉", "其中每天美元"]
    for i, n in enumerate(names):
        L.append(f"| {n} | " + " | ".join(cell[k][i] for k in kinds) + " |")
    return L


def stats_lines(st):
    both = max(st["both"], 1)
    return [f"- 行数 {st['rows']:,}（{st['markets']:,} 个市场），四个价格都有的 {st['both']:,} 行。",
            f"- 两边互为镜像（Down 卖一 = 1 − Up 买一 且 Down 买一 = 1 − Up 卖一）：{st['mirror']:,} 行，"
            f"{st['mirror'] / both:.4%}。",
            f"- 盘口交叉（某一边卖一 < 买一）：{st['crossed']:,} 行。",
            f"- 不算手续费：Up 卖一 + Down 卖一 < 1 的 {st['merge_nofee']:,} 行；Up 买一 + Down 买一 > 1 的 "
            f"{st['split_nofee']:,} 行。算手续费：合并 {st['merge_raw']:,} 行、拆分 {st['split_raw']:,} 行；"
            f"再加价格档和挂单量：合并 {st['merge_tr']:,} 行、拆分 {st['split_tr']:,} 行。"]


def breakdown(ep, col, bins=None, labels=None, kinds=KINDS):
    """Tradable episodes / executable / $ per kind, by a column (binned if `bins`)."""
    tr = ep[ep["level"] == "tradable"].copy()
    if not len(tr):
        return ["（没有可成交回合）"]
    tr["_k"] = pd.cut(tr[col], bins, right=False, labels=labels) if bins is not None else tr[col]
    L = ["| " + col + " | " + " | ".join(f"{KIND_SHORT[k]}：回合 / 可执行 / $" for k in kinds) + " |",
         "|---|" + "---:|" * len(kinds)]
    for kv, g in tr.groupby("_k", sort=True, observed=True):
        cells = []
        for k in kinds:
            h = g[g["kind"] == k]
            cells.append(f"{len(h):,} / {int(h['exec'].astype(bool).sum()):,} / ${h['usd'].sum():,.2f}")
        L.append(f"| {kv} | " + " | ".join(cells) + " |")
    return L


def tau_labels(bins):
    return [f"{a}–{b}" for a, b in zip(bins[:-1], bins[1:])]


def episode_list(ep, n=30):
    """The first n tradable episodes (or raw ones if there are none), for reading one by one."""
    e = ep[ep["level"] == "tradable"] if len(ep) and (ep["level"] == "tradable").any() else ep
    if not len(e):
        return []
    e = e.sort_values("ts_ms").head(n)
    L = ["| 时间（UTC） | 市场 | 类 | 级 | 剩余秒 | 行数 | 持续秒 | 两价之和 | 首行每对 | 数量 | 交叉 | 可执行 | 执行每对 | 执行数量 |",
         "|---|---|---|---|---:|---:|---:|---:|---:|---:|---|---|---:|---:|"]
    for r in e.itertuples():
        L.append(f"| {pd.to_datetime(r.ts_ms, unit='ms'):%m-%d %H:%M:%S} | {str(r.market)[:12]} | {r.kind} | {r.level} | "
                 f"{r.tau:.0f} | {r.rows} | {r.dur_s:.1f} | {r.sum0:.3f} | {_c(r.edge0)} | {r.size0:.0f} | "
                 f"{'是' if r.crossed0 else ''} | {'是' if r.exec else ''} | {_c(r.exec_edge)} | "
                 f"{r.exec_size:.0f} |".replace("| nan |", "| – |"))
    return L


def headline(ep, ndays):
    """One data-first sentence: how many episodes, where, how many survive the crossed-row and delay checks."""
    raw = ep[ep["level"] == "raw"]
    if not len(raw):
        return "没有一行满足任一种套利（含手续费）。"
    tr = ep[ep["level"] == "tradable"]
    ex = tr[tr["exec"].astype(bool)]
    clean = ex[~ex["crossed0"].astype(bool) & ~ex["exec_crossed"].astype(bool)]
    when = pd.to_datetime(raw["ts_ms"], unit="ms")
    days = when.dt.strftime("%m-%d").nunique()
    span = f"{when.min():%m-%d %H:%M} – {when.max():%m-%d %H:%M} UTC" if days <= 2 else f"{days} 个日期"
    return (f"含手续费满足条件的回合：合并 {int((raw['kind'] == 'merge').sum()):,}、拆分 {int((raw['kind'] == 'split').sum()):,}，"
            f"分布在 {raw['market'].nunique():,} 个市场、{span}；两类合计首行盘口交叉的 {int(raw['crossed0'].astype(bool).sum()):,} 个，"
            f"持续 ≥ 2 行的 {int((raw['rows'] >= 2).sum()):,} 个。按规定延迟仍成立的可成交回合 {len(ex):,} 个，"
            f"其中看到时和成交时都不交叉的 {len(clean):,} 个，合计每天 ${clean['usd'].sum() / max(ndays, 1):,.2f}。")


def kacho_report(f, out, label="kacho.io BTC 5m 每秒盘口"):
    st = row_stats(f)
    ep = scan(f, KACHO_LAG_MS, gap_ms=1000, step_ms=1000)
    day_of = lambda ms: pd.to_datetime(ms, unit="ms").dt.strftime("%Y-%m-%d")  # noqa: E731
    f_day = day_of(pd.Series(f["start_ms"].unique()))
    if len(ep):
        ep["day"] = day_of(ep["start_ms"])
        ep["period"] = np.where(ep["day"] < B_FROM, "A", np.where(ep["day"] < B_UNTIL, "B", "其他"))
    su_sad = np.isclose(f["bu_sz"], f["ad_sz"]).mean()
    days = {"全部": f_day.nunique(), "A": f_day[f_day < B_FROM].nunique(),
            "B": f_day[(f_day >= B_FROM) & (f_day < B_UNTIL)].nunique()}
    L = [f"# 合并 / 拆分无风险套利扫描：{label}（{f_day.min()} – {f_day.max()}）", "",
         "ROUNDTRIP.md 第 8 族。合并：两边按卖一各买 1 份、合并成 1 USDC；拆分：1 USDC 拆成两边、按买一各卖 1 份。"
         "两腿都付 taker 费（3/24–5/18 的费率 0.072·p(1−p)）；合并/拆分本身不收交易费，链上 gas 忽略（对套利偏乐观）。"
         "执行按规定时序：看到标为 s 的行（s + 1 起可用）立即下单，按标为 s + 2 的行成交，那一行仍满足才算可执行。", "",
         "## 结果", "", headline(ep, days["全部"]), ""]
    L += summary_table(ep, days["全部"])
    L += ["", f"每天美元按 {days['全部']} 个有数据的日历日折算。", "", "## 行级计数", ""] + stats_lines(st)
    L += [f"- 挂单量列核对：Up 买一量 su = Down 卖一量 sad 的行占 {su_sad:.1%}（镜像时两者是同一批挂单），"
          "所以 su/sd 是买一量、sau/sad 是卖一量。", ""]
    if len(ep):
        L += ["## 按段（市场开始日期）", "", "| 段 | 日数 | " + " | ".join(f"{KIND_SHORT[k]}：可成交回合 / 可执行 / 每天 $" for k in KINDS) + " |",
              "|---|---:|" + "---:|" * len(KINDS)]
        for p in ("A", "B"):
            g = ep[(ep["period"] == p) & (ep["level"] == "tradable")]
            cells = [f"{len(g[g['kind'] == k]):,} / {int(g[g['kind'] == k]['exec'].sum()):,} / "
                     f"${g[g['kind'] == k]['usd'].sum() / max(days[p], 1):,.2f}" for k in KINDS]
            L.append(f"| {p} | {days[p]} | " + " | ".join(cells) + " |")
        L += ["", "## 按剩余时间（秒，回合首行）", ""] + breakdown(ep, "tau", TAU_BINS[:8], tau_labels(TAU_BINS[:8]))
        L += ["", "## 按日期（只列有回合的日子；不论价格档的回合数在括号里）", ""]
        rows = ["| 日期 | 合并回合（raw） | 拆分回合（raw） | 可执行 | 美元 |", "|---|---:|---:|---:|---:|"]
        for day, g in ep.groupby("day"):
            tr = g[g["level"] == "tradable"]
            raw = g[g["level"] == "raw"]
            rows.append(f"| {day} | {len(tr[tr['kind'] == 'merge'])}（{len(raw[raw['kind'] == 'merge'])}） | "
                        f"{len(tr[tr['kind'] == 'split'])}（{len(raw[raw['kind'] == 'split'])}） | {int(tr['exec'].sum())} | "
                        f"${tr['usd'].sum():,.2f} |")
        L += rows + ["", "## 回合明细（按时间，最多 40 个）", ""] + episode_list(ep, 40)
    L += ["", "## 说明", "",
          "- 两边盘口几乎处处互为镜像：Polymarket 的撮合把 Up 的买单同时当成 Down 的卖单（买 Up 和买 Down 之和 ≥ 1 时"
          "撮合成铸造一对，卖两边之和 ≤ 1 时撮合成合并），所以 Up 卖一 + Down 卖一 = 1 + Up 价差 ≥ 1，"
          "Up 买一 + Down 买一 = 1 − 价差 ≤ 1。两种套利在一致的快照里按构造就不存在；剩下的几行要么一边盘口交叉"
          "（卖一低于买一，交易所不允许，是缓存里的旧数据），要么两本书在不同时刻取样。",
          "- 一行是缓存的一次快照，单行的“套利”可能是两本书不同时刻的拼接；看持续 ≥ 2 行和按 s + 2 行仍成立的数字。",
          "- 两腿同一行成交是乐观假设：实际一腿成交、另一腿落空时要按市价平掉，这里没算这部分风险。"]
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))
    return ep, st


def _merge_stats(acc, st):
    for k, v in st.items():
        acc[k] = acc.get(k, 0) + v
    return acc


def hf_report(ep, stats, ndays, out, ndays_total, extra=None):
    hz = sorted({h for h in stats})
    L = [f"# 合并 / 拆分无风险套利扫描：{cross.DS} 100 ms 盘口（{ndays} 个日档）", "",
         "两腿都付 taker 费（7/01 前 0.072、之后 0.07）；合并/拆分不收交易费，gas 忽略。只用 active、未暂停、"
         "在市场窗口内的快照。执行：看到快照后 0.5 秒按那一刻的快照（≤ 5 秒旧）成交，那时仍满足才算可执行（“0.5 秒后还在”）。",
         ""]
    for h in hz:
        e = ep[ep["horizon"] == h] if len(ep) else ep
        L += [f"## {int(h)} 分钟市场", "", headline(e, ndays), ""] + summary_table(e, ndays) + [""] + stats_lines(stats[h]) + [""]
        if len(e):
            L += ["按剩余时间（秒，回合首行）：", ""] + breakdown(e, "tau", TAU_BINS, tau_labels(TAU_BINS)) + [""]
    if len(ep):
        L += ["## 按日期（可成交回合 / 可执行 / 美元，各周期合计）", ""] + breakdown(ep, "day") + [""]
        L += ["## 可成交回合明细（按时间，最多 30 个）", ""] + episode_list(ep) + [""]
    if extra:
        L += extra
    L += ["", f"每天美元按 {ndays_total} 个日档折算。明细见同名 .csv.gz。", "",
          "两种套利都要求两本书合起来交叉。CLOB 会把互补的单子直接撮合（两边买单之和 ≥ 1 时铸造一对，两边卖单之和 ≤ 1 "
          "时合并一对），所以一致的快照里不会出现；首行“交叉”的回合是某一本书自己卖一低于买一的坏快照，不是能成交的报价。"]
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


HF_PRICES = ("up_best_bid", "up_best_ask", "down_best_bid", "down_best_ask")
HF_SIZES = ("up_bid_sizes", "up_ask_sizes", "down_bid_sizes", "down_ask_sizes")


def _first(col):
    """First element of each list in a pyarrow list column as float64 (NaN for null or empty lists)."""
    out = []
    for ch in (col.chunks if hasattr(col, "chunks") else [col]):
        off = ch.offsets.to_numpy()
        vals = ch.values.to_numpy(zero_copy_only=False).astype(float) if len(ch.values) else np.array([np.nan])
        n = np.diff(off)
        v = np.where(n > 0, vals[np.clip(off[:-1], 0, len(vals) - 1)], np.nan)
        valid = ch.is_valid().to_numpy(zero_copy_only=False)
        out.append(np.where(valid, v, np.nan))
    return np.concatenate(out) if out else np.array([])


def read_day_lean(path):
    """What hf_frame needs from one daily archive, as (features, markets): the same columns and the
    same first-level sizes cross.read_day gives (size lists are best-first; see the module notes),
    but only rows that are trading and only one market row per market and file. cross.read_day keeps
    every column of every 100 ms market row and peaked at ~11 GB on a May archive."""
    import io
    import re
    import tarfile

    import pyarrow.compute as pc
    import pyarrow.parquet as pq
    feats, mkts = [], []
    with tarfile.open(path, "r|gz") as tar:
        for m in tar:
            if not (m.isfile() and m.name.endswith(".parquet")):
                continue
            table = (re.search(r"dataset=([^/]+)", m.name) or [None, ""])[1]
            if table == "polymarket_features_100ms":
                pf = pq.ParquetFile(io.BytesIO(tar.extractfile(m).read()))
                names = set(pf.schema_arrow.names)
                cols = [c for c in ("timestamp_ms", "market_id", "lifecycle_state", "observed_halt_flag") + HF_PRICES
                        + HF_SIZES if c in names]
                t = pf.read(columns=cols)
                keep = pc.is_in(t["lifecycle_state"].cast("string"), pa_strings(["active", "open", "trading"])) \
                    if "lifecycle_state" in names else None
                if keep is not None:
                    keep = pc.or_kleene(keep, pc.is_null(t["lifecycle_state"])).fill_null(True)
                if "observed_halt_flag" in names:
                    calm = pc.invert(t["observed_halt_flag"].fill_null(False))
                    keep = calm if keep is None else pc.and_(keep, calm)
                if keep is not None:
                    t = t.filter(keep)
                d = {"timestamp_ms": t["timestamp_ms"].to_numpy(), "market_id": t["market_id"].cast("string").to_numpy(
                    zero_copy_only=False)}
                for c in HF_PRICES:
                    d[c] = t[c].to_numpy(zero_copy_only=False).astype(float) if c in names else np.nan
                for c in HF_SIZES:
                    d[c.replace("_sizes", "_size")] = _first(t[c]) if c in names else np.nan
                feats.append(pd.DataFrame(d))
            elif table == "polymarket_market_100ms":
                pf = pq.ParquetFile(io.BytesIO(tar.extractfile(m).read()))
                cols = [c for c in ("market_id", "slug", "session_start_ts", "session_end_ts", "chainlink_open_price")
                        if c in pf.schema_arrow.names]
                mk = pf.read(columns=cols).to_pandas()
                mk["market_id"] = mk["market_id"].astype(str)
                mkts.append(mk.drop_duplicates("market_id", keep="last"))
    feat = pd.concat(feats, ignore_index=True) if feats else pd.DataFrame()
    if len(feat):
        feat["market_id"] = feat["market_id"].astype("category")
    mk = pd.concat(mkts, ignore_index=True) if mkts else pd.DataFrame()
    return feat, mk


def pa_strings(values):
    import pyarrow as pa
    return pa.array(values, pa.string())


def hf_day(path, day):
    """(episodes, row stats by horizon, rows, markets) of one daily archive already on disk."""
    feat, mk = read_day_lean(path)
    fr = hf_frame(feat, cross.market_table(mk, pd.DataFrame()) if len(mk) else pd.DataFrame())
    del feat, mk
    stats = {h: row_stats(g) for h, g in fr.groupby("horizon")}
    ep = scan(fr, HF_LAG_MS, gap_ms=150, step_ms=100)
    ep["day"] = day
    return ep, stats, len(fr), fr["market"].nunique()


def run_hf(workdir, out, days=None):
    """Every daily archive (or the last `days`), one at a time: download, scan, delete."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    arcs = cross.archives(cross.fetch("MANIFEST.txt").decode())
    if days:
        arcs = arcs[-int(days):]
    parts, stats, done, fails = [], {}, 0, []
    for name, size in arcs:
        local = workdir / name
        err = None
        for attempt in range(3):  # a cut-off download ("unexpected end of data") is retried
            try:
                cross.fetch(name, local)
                if size and local.stat().st_size != size:
                    raise OSError(f"{name}: {local.stat().st_size} bytes, manifest says {size}")
                ep, st, nrows, nmk = hf_day(local, name[15:25])
                err = None
                break
            except Exception:
                import traceback
                err = traceback.format_exc()
                print(f"{name}: attempt {attempt + 1} failed\n{err}", flush=True)
            finally:
                local.unlink(missing_ok=True)
        if err:
            fails.append(name)
            continue
        for h, x in st.items():
            stats[h] = _merge_stats(stats.get(h, {}), x)
        parts.append(ep)
        done += 1
        print(f"{name}: {nrows:,} rows, {nmk:,} markets, tradable episodes {int((ep['level'] == 'tradable').sum())}",
              flush=True)
    ep = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=EP_COLS + ["day"])
    if len(ep):
        ep.to_csv(Path(out).with_suffix(".csv.gz"), index=False)
    extra = [f"失败的日档：{', '.join(fails)}"] if fails else None
    hf_report(ep, stats, done, out, done, extra)
    return ep, stats


def run_forward(root, out, coins=("btc",)):
    import latency as lt
    dirs = lt._latency_dirs([root], coins)
    L = [f"# 合并 / 拆分无风险套利扫描：GitHub 前向录制（{root}）", ""]
    deg, parts, stats = [], [], None
    seen = set()
    for coin, ds in dirs.items():
        for d in ds:
            try:
                deg.append((str(d), latency_degenerate(lt.load_books(d))))
            except Exception as e:  # noqa: BLE001 - keep going: one broken recording must not hide the rest
                deg.append((str(d), {"error": repr(e)}))
            loaded = load_bundle(d.parent)
            if loaded is None:
                continue
            fr = forward_frame(*loaded)
            fr = fr[~fr["market"].isin(seen)]  # a market recorded by two overlapping runs: the first one only
            seen |= set(fr["market"])
            parts.append(fr)
    n_one = sum(1 for _, x in deg if x.get("one_token"))
    L += ["## latency/ 文件（latency.load_books）", "",
          f"{len(deg)} 个录制目录，其中 {n_one} 个只有 Up 一个 token 的盘口（没有 Down 列）。"]
    if n_one:
        rows = sum(x["rows"] for _, x in deg if x.get("one_token"))
        crossed = sum(x["crossed"] for _, x in deg if x.get("one_token"))
        L += ["", f"这些目录里 Down 的报价只能写成 1 − Up 买一 / 1 − Up 卖一，于是合并成本 ≡ 1 + 价差、拆分所得 ≡ 1 − 价差："
              f"套利在这里按构造不存在，不是测出来的结论，所以不给数字。{rows:,} 行里只有 {crossed:,} 行盘口交叉"
              "（卖一 < 买一，坏行），只有它们会被误判成“套利”。", "",
              "| 目录 | 行数 | 市场 | 交叉行 | 最小价差 | 价差中位 |", "|---|---:|---:|---:|---:|---:|"]
        for d, x in deg:
            if x.get("one_token"):
                L.append(f"| {d} | {x['rows']:,} | {x['markets']:,} | {x['crossed']:,} | {x['min_spread']:.3f} | "
                         f"{x['median_spread']:.3f} |")
    errs = [(d, x["error"]) for d, x in deg if "error" in x]
    if errs:
        L += [""] + [f"- {d}: {e}" for d, e in errs]
    fr = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=COLS)
    L += ["", "## 两个 token 的盘口（bundle 的 best_bid_ask / book，按接收时间）", ""]
    if fr.empty:
        L += ["没有找到 bundle 的 data/polymarket/daily/best_bid_ask 和 markets 文件，两 token 的检验没跑。"
              "要跑：从 raw.tgz 里一并解出 bundle-btc/data/polymarket/daily（best_bid_ask、book、markets）。"]
        ep = pd.DataFrame()
    else:
        stats = row_stats(fr)
        ep = scan(fr, HF_LAG_MS, gap_ms=np.iinfo(np.int64).max // 4, step_ms=0, max_age_ms=None)
        ndays = pd.to_datetime(fr["start_ms"], unit="ms").dt.date.nunique()
        L += ["价格来自 best_bid_ask（每次变化），挂单量来自每秒一行的 book（最优价一致时）。执行：0.5 秒后（接收时钟）"
              "那一刻的状态仍满足才算可执行。", "", headline(ep, ndays), ""] + summary_table(ep, ndays) + [""] + stats_lines(stats)
        if len(ep):
            ep["day"] = pd.to_datetime(ep["start_ms"], unit="ms").dt.strftime("%Y-%m-%d")
            L += ["", "按剩余时间（秒）：", ""] + breakdown(ep, "tau", TAU_BINS[:8], tau_labels(TAU_BINS[:8]))
            L += ["", "明细（最多 30 个）：", ""] + episode_list(ep)
            ep.to_csv(Path(out).with_suffix(".csv.gz"), index=False)
        L += ["", f"每天美元按 {ndays} 个有数据的日历日折算。"]
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))
    return deg, ep, stats


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="mode", required=True)
    k = sub.add_parser("kacho")
    k.add_argument("--kacho", required=True, help="btc_markets.parquet / btc_ticks.parquet 所在目录")
    k.add_argument("--out", default="real/arb-scan-kacho.md")
    h = sub.add_parser("hf")
    h.add_argument("--workdir", required=True)
    h.add_argument("--days", type=int, default=None, help="只跑最后 N 个日档")
    h.add_argument("--out", default="real/arb-scan-hf.md")
    f = sub.add_parser("forward")
    f.add_argument("root")
    f.add_argument("--out", default="real/arb-scan-forward.md")
    a = ap.parse_args(argv)
    if a.mode == "kacho":
        ticks, markets = load_kacho(a.kacho)
        kacho_report(kacho_frame(ticks, markets), a.out)
    elif a.mode == "hf":
        run_hf(a.workdir, a.out, a.days)
    else:
        run_forward(a.root, a.out)


if __name__ == "__main__":
    main()
