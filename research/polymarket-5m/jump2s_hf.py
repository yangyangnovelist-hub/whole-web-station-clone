"""JUMP2S.md, the Hugging Face lane: "buy the side of a >= 1.2 bp Binance jump 2 s later and hold to
settlement" on whodisidk/polymarket-btc-updown-exchange-data (BTC 5m, 2026-05-25 .. 08-29, 100 ms
books, Polymarket prints, every Binance BTCUSDT print with exchange and recorder times). Paper
research on market data only. Runs on GitHub (Hugging Face and data.binance.vision are reachable
there) as `python cross.py jump2s [N] --workdir DIR` (ci/cross2.request); the shared rules,
statistics and report tables are in jump2s.py.

Per daily archive (downloaded, read and deleted one at a time as cross.stale / cross.gated do; only
the priced rows are kept):
- Markets: BTC 5m (horizon 5) whose outcome (up_won) is known, from cross.market_table.
- Jumps: jump2s.jumps in trades mode on the archive's Binance prints sorted by exchange (trade) time,
  with t_obs = the recorder's receipt time (as cross.stale_trades: the trigger is the receipt, the
  reference is the last print by trade time at least 1 s and at most 5 s earlier). t0 = receipt
  time, t_ex = trade time of the jump print.
- Plan: jump2s.plan (big, small, opposite) plus jump2s.random_controls run per market with the seed
  zlib.crc32(market id), so a market's random rows do not depend on the other markets of the day.
- Book entry at t0 + 2.0 s: the bought side's best ask in the last 100 ms snapshot at or before then
  (<= 1 s old), 0.02..0.98, >= 5 shares at the best ask. Trade entry: jump2s.first_print over the
  market's Polymarket prints with receipt time in [t0 + 2.0, t0 + 4.0] s (the bought token at p, the
  other token at 1 - p, any taker side), same band. Every kind (big, small, opposite, random) is
  priced both ways. Fee 0.07 p (1 - p), 1 share, held to the official result (up_won).
- Decomposition (big and small rows): the bought token's mid change from t0 - 0.5 s to t0 + 2.0 s
  against jump2s.model_fair over the same two moments; cont_bp = the Binance log move from t0 + 2 s
  to the market's end along the jump, in bp.
- H1 / H2: jump2s.add_trend at t_ex on Binance 1-minute klines (data.binance.vision, exchange time,
  2026-04 .. 2026-08, cached in the workdir).

Where JUMP2S.md and the task are silent, the conservative choice made here:
- Snapshots are de-duplicated per (market, timestamp_ms), keeping the last row. The entry snapshot
  must be lifecycle_state "active" and not observed_halt_flag (as cross.book_health); otherwise no
  book entry. The size at the best ask is the first entry of the recorded ask ladder (read_day's
  up_ask_size / down_ask_size; the ladder is best first, checked on the 08-19 archive), as in every
  cross.py study.
- A print's order within one receipt millisecond is the archive's order. taker_buy (reported as its
  own row) = the first print is on the bought token and its taker side is "buy"; a sell print on the
  other token does not count. The flag describes the print the trade entry used; it does not look
  further for a buy print.
- Mids: (best bid + best ask) / 2 of the bought token at the last snapshot at or before t0 - 0.5 s
  and t0 + 2.0 s (<= 1 s old, any lifecycle state); NaN when a side is missing or the token's book is
  crossed. The model fair at those two (recorder) times uses x = the log price of the last Binance
  print received by then (<= 5 s old; the jump print is received at t0, so it is in the second
  and not the first), and sigma, the price to beat and the known part of a TWAP from the per-second
  grid in exchange time (jump2s.Seconds; the recorder's lag is ignored there).
- cont_bp: x_end = the last Binance print by trade time at or before the end (<= 5 s old, else NaN),
  a point price also for the TWAP markets of August (as the task defines the outcome side), minus x at
  t0 + 2 s as above, times the jump's sign.
- Only the archive's own Binance prints can be jumps. The previous archive's last 15 minutes are
  carried over for the model (600 s sigma, 60 s TWAP strike) and the price lookups only. A market
  whose rows sit in two archives is priced from each archive's own prints (the 10 s spacing is not
  carried across midnight); duplicate rows are dropped.
- H1 / H2 use the klines alone when every month downloaded. If a month is missing, its minutes come
  from the archives' own Binance prints (jump2s.minute_closes on trade time), and the report says so.
  The recorder clock is within a second of the exchange clock, which does not matter for 1 h / 4 h
  features; the features are taken at the jump print's trade time (random rows: their own time).
"""
from __future__ import annotations

import time
import urllib.request
import zlib
from pathlib import Path

import numpy as np
import pandas as pd

import jump2s as j2

KLINE_URL = "https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-{}.zip"
KLINE_MONTHS = ("2026-04", "2026-05", "2026-06", "2026-07", "2026-08")
MID_BEFORE_S = 0.5      # the decomposition's "before" mid: t0 - 0.5 s
PRICE_MAX_AGE_S = 5.0   # a Binance price older than this is unknown
CARRY_S = 900           # the previous archive's last 15 min, for sigma (600 s) and the TWAP strike
OUT_COLS = ["market", "t0", "t_ex", "kind", "price_type", "sign", "jump_sign", "size_bp", "price", "size",
            "won", "pnl", "taker_buy", "mid0", "mid2", "fair0", "fair2", "cont_bp"]
ROUND = {"t0": 3, "t_ex": 3, "size_bp": 3, "price": 4, "size": 2, "pnl": 5, "mid0": 4, "mid2": 4,
         "fair0": 5, "fair2": 5, "cont_bp": 3, "ret4h": 6, "vr60": 4}


def market_seed(market):
    """Deterministic per-market seed for the random controls."""
    return zlib.crc32(str(market).encode())


def _segments(keys):
    """{key: (a, b)} of the runs of equal values in an already grouped array."""
    if not len(keys):
        return {}
    cut = np.flatnonzero(keys[1:] != keys[:-1]) + 1
    a = np.concatenate([[0], cut])
    b = np.concatenate([cut, [len(keys)]])
    return {keys[i]: (i, j) for i, j in zip(a, b)}


def _last_before(ts, val, t, max_age=PRICE_MAX_AGE_S):
    """val at the last ts <= t (sorted ts), NaN when none or older than max_age."""
    t = np.asarray(t, float)
    if not len(ts):
        return np.full(t.shape, np.nan)
    k = np.searchsorted(ts, t + j2.T_EPS, "right") - 1
    kk = np.maximum(k, 0)
    return np.where((k >= 0) & (t - ts[kk] <= max_age + j2.T_EPS), val[kk], np.nan)


def _binance(binance):
    """Finite prints as float arrays: (trade-time sorted ts, logp, recv) and (recv-sorted recv, logp)."""
    b = binance[["trade_ts_ms", "recv_ts_ms", "price"]].apply(pd.to_numeric, errors="coerce")
    v = b.to_numpy(float)
    b = b[np.isfinite(v).all(axis=1) & (v[:, 2] > 0)].drop_duplicates()
    b = b.sort_values(["trade_ts_ms", "recv_ts_ms"], kind="stable")
    ts = b["trade_ts_ms"].to_numpy(float) / 1000.0
    rv = b["recv_ts_ms"].to_numpy(float) / 1000.0
    lp = np.log(b["price"].to_numpy(float))
    o = np.argsort(rv, kind="stable")
    return ts, lp, rv, rv[o], lp[o]


def _plan(sel, win):
    """jump2s.plan without its pooled random rows, plus random controls seeded per market."""
    p = j2.plan(sel, win)
    p = p[p["kind"] != "random"]
    big = sel[sel["bucket"] == "big"]
    rnd = [j2.random_controls(g, win[win["market"] == m], seed=market_seed(m))
           for m, g in big.groupby("market", sort=False)]
    if rnd:
        r = pd.concat(rnd, ignore_index=True).drop(columns="jump_t0")
        r = r.assign(kind="random", jump_sign=np.nan, t_entry=r["t0"] + j2.ENTRY_S)
        p = pd.concat([p, r], ignore_index=True) if len(p) else r
    return p.reset_index(drop=True)


def day_rows(feat, mkts, binance, prints, carry=None):
    """Priced rows (OUT_COLS) of one archive and a dict of counts.

    feat / mkts / binance / prints as from cross.read_day + cross.market_table, cross.read_binance and
    cross.read_poly_trades; `carry`: the previous archive's Binance prints (same columns) for the model
    and the price lookups only."""
    info = dict(markets=0, prints=len(binance), big=0, small=0, delay=(np.nan, np.nan, np.nan),
                plan={}, priced={})
    empty = pd.DataFrame(columns=OUT_COLS)
    if mkts is None or mkts.empty or binance.empty:
        return empty, info
    m5 = mkts[(mkts["horizon"] == 5) & mkts["up_won"].notna()].copy()
    m5["market_id"] = m5["market_id"].astype(str)
    m5 = m5.drop_duplicates("market_id", keep="last")
    info["markets"] = len(m5)
    if m5.empty:
        return empty, info
    win = pd.DataFrame({"market": m5["market_id"].to_numpy(), "start": m5["start"].to_numpy(float) / 1000.0,
                        "end": m5["end"].to_numpy(float) / 1000.0})
    ts, lp, rv, _, _ = _binance(binance)
    if len(ts):
        info["delay"] = tuple(np.nanpercentile((rv - ts) * 1000.0, [10, 50, 90]))
    sel = j2.jumps(ts, lp, win, mode="trades", t_obs=rv)
    info["big"], info["small"] = int((sel["bucket"] == "big").sum()), int((sel["bucket"] == "small").sum())
    if sel.empty:
        return empty, info
    p = _plan(sel, win)
    p = p.sort_values(["market", "t0"], kind="stable").reset_index(drop=True)
    info["plan"] = p["kind"].value_counts().to_dict()
    n = len(p)
    mk = p["market"].astype(str).to_numpy()
    sign = p["sign"].to_numpy(float)
    t0 = p["t0"].to_numpy(float)
    te = t0 + j2.ENTRY_S

    # ---- book: snapshots of the day's 5m markets, grouped by market, by time
    f = feat[feat["market_id"].astype(str).isin(set(win["market"]))] if len(feat) else feat
    book_px, book_sz = np.full(n, np.nan), np.full(n, np.nan)
    mid0, mid2 = np.full(n, np.nan), np.full(n, np.nan)
    if len(f):
        f = f.assign(market_id=f["market_id"].astype(str))
        f = f.sort_values(["market_id", "timestamp_ms"], kind="stable")
        f = f.drop_duplicates(["market_id", "timestamp_ms"], keep="last")
        fk = f["market_id"].to_numpy()
        FT = f["timestamp_ms"].to_numpy(float) / 1000.0
        col = {c: (f[c].to_numpy(float) if c in f else np.full(len(f), np.nan))
               for c in ("up_best_bid", "up_best_ask", "down_best_bid", "down_best_ask", "up_ask_size",
                         "down_ask_size")}
        state = f["lifecycle_state"].astype(str).str.lower().to_numpy() if "lifecycle_state" in f else \
            np.full(len(f), "active")
        halt = f["observed_halt_flag"].fillna(False).astype(bool).to_numpy() if "observed_halt_flag" in f else \
            np.zeros(len(f), bool)
        live = (state == "active") & ~halt
        with np.errstate(invalid="ignore"):
            mid_up = np.where(col["up_best_bid"] <= col["up_best_ask"], (col["up_best_bid"] + col["up_best_ask"]) / 2,
                              np.nan)
            mid_dn = np.where(col["down_best_bid"] <= col["down_best_ask"],
                              (col["down_best_bid"] + col["down_best_ask"]) / 2, np.nan)
        fseg = _segments(fk)
    else:
        fseg = {}

    # ---- prints of the day's 5m tokens: market, is-Up, Up-equivalent price, taker buy
    tok = {}
    for m, u, d in zip(m5["market_id"], m5.get("up_token", [None] * len(m5)), m5.get("down_token", [None] * len(m5))):
        if u is not None and str(u) not in ("", "None", "nan"):
            tok[str(u)] = (m, True)
        if d is not None and str(d) not in ("", "None", "nan"):
            tok[str(d)] = (m, False)
    trade_px, taker = np.full(n, np.nan), np.full(n, np.nan)
    trade_sz = np.full(n, np.nan)
    pseg = {}
    if len(prints) and tok:
        q = prints[prints["instrument"].astype(str).isin(tok)]
        if len(q):
            ins = q["instrument"].astype(str)
            q = q.assign(_m=ins.map({k: v[0] for k, v in tok.items()}),
                         _up=ins.map({k: v[1] for k, v in tok.items()}).astype(bool))
            q = q.sort_values(["_m", "recv_ts_ms"], kind="stable")
            PT = q["recv_ts_ms"].to_numpy(float) / 1000.0
            PX = q["price"].to_numpy(float)
            PU = q["_up"].to_numpy(bool)
            UPX = np.where(PU, PX, 1.0 - PX)
            PB = (q["taker_side"].astype(str).str.lower() == "buy").to_numpy()
            PS = q["size"].to_numpy(float) if "size" in q else np.full(len(q), np.nan)
            pseg = _segments(q["_m"].to_numpy())

    for m, idx in pd.Series(np.arange(n)).groupby(mk, sort=False).indices.items():
        s, t, e = sign[idx], t0[idx], te[idx]
        if m in fseg:
            a, b = fseg[m]
            k = j2.asof_idx(FT[a:b], e, j2.BOOK_MAX_AGE_S)
            g = np.maximum(k, 0) + a
            ask = np.where(s > 0, col["up_best_ask"][g], col["down_best_ask"][g])
            sz = np.where(s > 0, col["up_ask_size"][g], col["down_ask_size"][g])
            ok = (k >= 0) & live[g] & j2.price_ok(ask, sz)
            book_px[idx], book_sz[idx] = np.where(ok, ask, np.nan), np.where(ok, sz, np.nan)
            mid2[idx] = np.where(k >= 0, np.where(s > 0, mid_up[g], mid_dn[g]), np.nan)
            k0 = j2.asof_idx(FT[a:b], t - MID_BEFORE_S, j2.BOOK_MAX_AGE_S)
            g0 = np.maximum(k0, 0) + a
            mid0[idx] = np.where(k0 >= 0, np.where(s > 0, mid_up[g0], mid_dn[g0]), np.nan)
        if m in pseg:
            a, b = pseg[m]
            px = j2.first_print(PT[a:b], UPX[a:b], t + j2.TRADE_WIN_S[0], t + j2.TRADE_WIN_S[1], s)
            kp = np.minimum(np.searchsorted(PT[a:b], t + j2.TRADE_WIN_S[0] - j2.T_EPS, "left"), b - a - 1) + a
            ok = j2.price_ok(px)
            trade_px[idx] = np.where(ok, px, np.nan)
            taker[idx] = np.where(ok, ((PU[kp] == (s > 0)) & PB[kp]).astype(float), np.nan)
            trade_sz[idx] = np.where(ok, PS[kp], np.nan)

    # ---- model fair and the outcome side (jump rows), with the previous archive's tail for the model
    jump = p["kind"].isin(["big", "small"]).to_numpy()
    fair0, fair2, cont = np.full(n, np.nan), np.full(n, np.nan), np.full(n, np.nan)
    if jump.any():
        both = binance if carry is None or carry.empty else pd.concat([carry, binance], ignore_index=True)
        cts, clp, _, crv, crlp = _binance(both)
        sp = j2.Seconds(cts, np.exp(clp))
        st = dict(zip(win["market"], win["start"]))
        en = dict(zip(win["market"], win["end"]))
        start = np.array([st[m] for m in mk[jump]], float)
        end = np.array([en[m] for m in mk[jump]], float)
        sj, tj = sign[jump], t0[jump]
        x0 = _last_before(crv, crlp, tj - MID_BEFORE_S)
        x2 = _last_before(crv, crlp, tj + j2.ENTRY_S)
        fair0[jump] = j2.model_fair(sp, sj, start, tj - MID_BEFORE_S, x=x0)
        fair2[jump] = j2.model_fair(sp, sj, start, tj + j2.ENTRY_S, x=x2)
        x_end = _last_before(cts, clp, end)
        cont[jump] = p["jump_sign"].to_numpy(float)[jump] * (x_end - x2) * 1e4

    won_up = dict(zip(m5["market_id"], m5["up_won"].astype(float)))
    up = np.array([won_up[m] for m in mk], float)
    won = np.where(sign > 0, up == 1.0, up == 0.0).astype(float)
    base = pd.DataFrame({"market": mk, "t0": t0, "t_ex": p["t_ex"].to_numpy(float), "kind": p["kind"].to_numpy(),
                         "sign": sign.astype(int), "jump_sign": p["jump_sign"].to_numpy(float),
                         "size_bp": p["size_bp"].to_numpy(float) if "size_bp" in p else np.nan, "won": won,
                         "mid0": mid0, "mid2": mid2, "fair0": fair0, "fair2": fair2, "cont_bp": cont})
    parts = []
    for name, px, sz, tb in (("book", book_px, book_sz, np.full(n, np.nan)), ("trade", trade_px, trade_sz, taker)):
        ok = np.isfinite(px)
        r = base[ok].assign(price_type=name, price=px[ok], size=sz[ok], taker_buy=tb[ok])
        r["pnl"] = j2.pnl(r["won"].to_numpy(float), r["price"].to_numpy(float))
        parts.append(r)
        info["priced"].update({(k, name): int(v) for k, v in r["kind"].value_counts().items()})
    out = pd.concat(parts, ignore_index=True)[OUT_COLS]
    return out, info


# --------------------------------------------------------------------------- 1-minute klines
def download(url, dest):
    """url -> dest through a temporary file, so a broken download never stays in the cache."""
    tmp = Path(str(dest) + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "research"})
    with urllib.request.urlopen(req, timeout=300) as r, open(tmp, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    tmp.replace(dest)
    return dest


def klines(workdir, months=KLINE_MONTHS):
    """(minute closes from the Binance 1m kline zips, cached in workdir, [months that failed])."""
    workdir = Path(workdir)
    parts, missing = [], []
    for m in months:
        dest = workdir / f"BTCUSDT-1m-{m}.zip"
        try:
            if not dest.exists() or dest.stat().st_size == 0:
                download(KLINE_URL.format(m), dest)
            parts.append(j2.load_minute_klines([dest]))
        except Exception as e:  # keep going: the archives' own prints stand in for a missing month
            print(f"klines {m}: {e!r}", flush=True)
            missing.append(m)
            if dest.exists():
                dest.unlink()
    s = pd.concat(parts) if parts else pd.Series(dtype=float)
    return s[~s.index.duplicated(keep="last")].sort_index(), missing


def closes_for(kl, own, missing):
    """The minute closes the H features use: the klines, plus the archives' own minutes when a month
    is missing."""
    if not missing or own is None or own.empty:
        return kl
    return kl.combine_first(own) if len(kl) else own


# --------------------------------------------------------------------------- the run
def run(workdir, out, days=None, dataset=None):
    """Every daily archive (the last `days` with days), rows to <out>.csv.gz, the report to out."""
    import traceback

    import cross
    if dataset and dataset != cross.DS:
        cross.set_dataset(dataset)
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    kl, missing = klines(workdir)
    print(f"klines: {len(kl):,} minutes, missing months {missing or 'none'}", flush=True)
    arcs = cross.archives(cross.fetch("MANIFEST.txt").decode())
    if days:
        arcs = arcs[-int(days):]
    parts, infos, own, failed, carry = [], [], [], [], None
    for name, _ in arcs:
        day = name[15:25]
        try:
            local = cross.fetch(name, workdir / name)
            feat, mk, rs = cross.read_day(local)
            binance = cross.read_binance(local)
            prints = cross.read_poly_trades(local)
            Path(local).unlink()
            rows, info = day_rows(feat, cross.market_table(mk, rs), binance, prints, carry=carry)
            del feat, prints
            if len(binance):
                tt = pd.to_numeric(binance["trade_ts_ms"], errors="coerce")
                carry = binance[tt >= tt.max() - CARRY_S * 1000][["trade_ts_ms", "recv_ts_ms", "price"]].copy()
                own.append(j2.minute_closes(tt.to_numpy(float) / 1000.0, binance["price"].to_numpy(float)))
            else:
                carry = None
            info["day"] = day
            infos.append(info)
            parts.append(rows)
            nb = info["priced"].get(("big", "book"), 0)
            print(f"{name}: {info['markets']} markets, {info['big']:,} big / {info['small']:,} small jumps, "
                  f"{nb:,} big book entries, {len(rows):,} rows ({time.time() - t_start:.0f} s)", flush=True)
        except Exception:
            print(f"{name}: failed\n{traceback.format_exc()}", flush=True)
            failed.append(day)
            carry = None
    rows = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=OUT_COLS)
    rows = rows.drop_duplicates(["market", "kind", "price_type", "t0", "sign"]).reset_index(drop=True)
    own_c = pd.concat(own).groupby(level=0).last() if own else pd.Series(dtype=float)
    closes = closes_for(kl, own_c, missing)
    rows = j2.add_trend(rows, closes, t_col="t_ex") if len(rows) else rows.assign(ret4h=[], vr60=[], h1=[], h2=[])
    rows.round(ROUND).to_csv(Path(out).with_suffix(".csv.gz"), index=False)
    L = report(rows, infos, failed, len(arcs), kl, own_c, missing, ds=cross.DS)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))
    return rows


# --------------------------------------------------------------------------- report
def _kline_check(kl, own):
    """Share of minutes where the kline close and the recorder's last print of the minute agree."""
    if kl is None or own is None or kl.empty or own.empty:
        return np.nan, 0
    both = pd.concat([kl.rename("k"), own.rename("o")], axis=1, join="inner").dropna()
    if both.empty:
        return np.nan, 0
    return float((np.abs(both["k"] / both["o"] - 1) < 1e-6).mean()), len(both)


def report(rows, infos, failed, n_arcs, kl=None, own=None, missing=(), ds=None):
    """The Chinese report (markdown lines)."""
    B = j2.HF_BLOCKS
    big = rows[(rows["kind"] == "big") & (rows["price_type"] == "book")] if len(rows) else rows
    days = [i["day"] for i in infos]
    nmk = sum(i["markets"] for i in infos)
    nbig = sum(i["big"] for i in infos)
    nsmall = sum(i["small"] for i in infos)
    L = [f"# 大跳后 2 秒买入：5–8 月复核（{ds or 'Hugging Face'}，BTC 5m，记录机时钟）", "",
         "设计在 [`JUMP2S.md`](../JUMP2S.md)（跑之前写死并提交），这里照它执行；`jump2s.py` 是共用的规则和统计，"
         "`jump2s_hf.py` 是这条数据线。纸面研究，只用行情数据，不下单。", "",
         f"用户本地的结论（用来对照，不参与这里的判定）：9 月 14 天每份 −1.37¢（1,519 笔），09-30–10-02 每份 +2.04¢（547 笔）。", "",
         f"数据：{n_arcs} 个日档里读到 {len(days)} 个（{days[0] if days else '–'} 至 {days[-1] if days else '–'}）"
         + (f"，读失败 {len(failed)} 个：{'、'.join(failed)}" if failed else "")
         + f"。只用结果已知的 BTC 5m 市场，共 {nmk:,} 个（按日档计）。", "",
         "## 规则（照 JUMP2S.md）", "",
         "- **跳变**：币安逐笔成交，以记录机收到的时刻触发；参照按成交时间至少 1 秒前、不超过 5 秒前的最后一笔，"
         "对数变动 |Δ| ≥ 1.2 bp 为大跳，1.0–1.2 bp 为小跳（对照）。同一市场同一档，下一次至少在上一次入选的 10 秒后；"
         "收到时刻距开盘 ≥ 15 秒、距结束 ≥ 12 秒。",
         "- **买入**：收到后 2.0 秒买跳变方向。盘口价：该时刻或之前最近的 100 ms 快照（不超过 1 秒前、在交易状态、未暂停）"
         "里这一方的卖一，0.02–0.98，卖一至少 5 份。成交价：收到后 2.0–4.0 秒内这个市场的第一笔 Polymarket 成交"
         "（这一方代币按原价、另一方代币按 1 − p，不论吃单方向），同样 0.02–0.98。另列一行只算“那一笔正好是吃单买这一方代币”的。"
         "taker 费 0.07·p(1 − p)，每次 1 份，持有到官方结算。",
         "- **对照**：同一时刻买反方向；同一市场允许时段内随机时刻、随机方向（每个大跳配一个，每个市场固定种子）；小跳。"
         "每种都按盘口价和成交价各算一遍。",
         "- **分解**（大跳、小跳）：价格端 = 收到前 0.5 秒到收到后 2.0 秒，这一方 Polymarket 中间价的变动之和 ÷ "
         "同一区间无漂移模型公平价的变动之和（模型用记录机那时已收到的最后一笔币安价、之前 600 秒的每秒波动、"
         "币安代理的开盘价 / 结算 TWAP，与 latency.py 等用的是同一个模型）。结果端 = 收到后 2 秒的币安价到市场结束"
         "（按成交时间，结束时或之前最后一笔）沿跳变方向又走了多少 bp。",
         "- **副假设**：H1 顺势 = 跳变方向与过去 4 小时币安收益同号；H2 趋势型 = 过去 60 个 1 分钟收益的方差比 VR > 1。"
         "用币安 1 分钟 K 线（data.binance.vision，交易所时间），在跳变那笔成交的交易所时间只用已经收完的分钟。"
         "记录机时钟和交易所时钟相差不到 1 秒（个别日子收到得更晚），对 1 小时、4 小时的特征没有影响。",
         "- **统计**：每份均值 ± 按市场聚类的标准误（t，笔数）。月份段：5/25–6/30、7/1–7/31、8/1–8/29。", ""]
    ok, nk = _kline_check(kl, own)
    if missing:
        L += [f"K 线缺 {'、'.join(missing)}，这些月份的分钟收盘改用日档里的币安逐笔（按成交时间）推出。", ""]
    if nk:
        L += [f"核对：{nk:,} 个两边都有的分钟里，K 线收盘与日档逐笔推出的分钟收盘一致（差 < 0.0001%）的占 {ok:.1%}。", ""]
    nd = max(len(days), 1)
    L += [f"跳变次数：大跳 {nbig:,}（每天约 {nbig / nd:,.0f}），小跳 {nsmall:,}（每天约 {nsmall / nd:,.0f}）。"
          "用户的口径约每天 108 次（9 月）到 182 次（09-30–10-02），他们的规则可能还有这里没写的筛选，"
          "这里按 JUMP2S.md 原文执行，次数不同是预料之中的。", ""]
    if not len(rows):
        return L + ["没有可用的数据。"]
    main = j2.decide_main(big, B)
    h1_ = j2.decide_h(big, "h1", B)
    h2_ = j2.decide_h(big, "h2", B)
    L += ["## 判定（JUMP2S.md 事先写死，只用这里的数据）", ""]
    L += j2.decision_lines(main, h1_, h2_, scope="5–8 月（Hugging Face）")
    L += ["", "## 每份盈亏", ""]
    L += j2.main_table(rows)
    tr = rows[(rows["price_type"] == "trade") & (pd.to_numeric(rows["taker_buy"], errors="coerce") == 1)]
    L += ["", "成交价只算“第一笔正好是吃单买这一方代币”的那些（其余的成交价行不算）：", "",
          "| 买法 | 成交价（吃单买入这一方） | 占成交价行 |", "|---|---:|---:|"]
    for k in j2.KINDS:
        allk = ((rows["kind"] == k) & (rows["price_type"] == "trade")).sum()
        sub = tr[tr["kind"] == k]
        L.append(f"| {j2.KIND_NAMES[k]} | {j2.fmt(j2.stat(sub))} | {len(sub) / allk:.0%} |" if allk else
                 f"| {j2.KIND_NAMES[k]} | – | – |")
    for pt in ("book", "trade"):
        L += ["", f"## 按月份段（{j2.PRICE_NAMES[pt]}）", ""]
        L += j2.block_table(rows, B, pt)
    L += ["", "## 分解：价格端还是结果端（大跳，盘口价）", "",
          "中间价变动、模型应变动都是这一方的、每笔平均；跟上比例是两者总和之比；结果端是买后到市场结束 BTC 顺跳变方向的 bp。", ""]
    L += j2.decomp_table(rows, B, "book")
    sm = rows[(rows["kind"] == "small") & (rows["price_type"] == "book")]
    if len(sm):
        d = j2.decomposition(sm)
        ratio = "–" if not np.isfinite(d["ratio"]) else f"{100 * d['ratio']:.0f}% ±{100 * d['ratio_se']:.0f}"
        c = d["cont"]
        L += ["", f"小跳（盘口价）：跟上比例 {ratio}，结果端 "
              + ("–" if not c["n"] else f"{c['mean']:+.2f} ±{c['se']:.2f} bp") + f"，每份 {j2.fmt(d['pnl'])}。"]
    for flag, name in (("h1", "H1 顺势（跳变与过去 4 小时同号）"), ("h2", "H2 趋势型（VR60 > 1）")):
        L += ["", f"## {name}，大跳、盘口价", ""]
        L += j2.h_table(rows, flag, B, "book")
    L += ["", "## 按天（大跳、盘口价）", ""]
    L += j2.daily_lines(rows, "book", "big", detail=True)
    L += ["", "“连续 3 天合计 ≥ +2¢”在各种买法里出现的频率（同一把尺子，看大跳是不是比对照更常出现）：", "",
          "| 买法 | 盘口价 | 成交价 |", "|---|---:|---:|"]
    for k in j2.KINDS:
        cells = []
        for pt in ("book", "trade"):
            th = j2.three_day(rows[(rows["kind"] == k) & (rows["price_type"] == pt)])
            cells.append("–" if not th["windows"] else f"{th['hits']} / {th['windows']}（{th['rate']:.0%}）")
        L.append(f"| {j2.KIND_NAMES[k]} | " + " | ".join(cells) + " |")
    L += ["", "## 按天（大跳、成交价）", ""]
    L += j2.daily_lines(rows, "trade", "big")
    L += ["", "## 数据覆盖", "", "<details><summary>每个日档</summary>", "",
          "| 日期 | 市场 | 大跳 | 小跳 | 大跳盘口价入场 | 大跳成交价入场 | 币安收到 − 成交时间 p10 / 中位 / p90（ms） |",
          "|---|---:|---:|---:|---:|---:|---|"]
    for i in infos:
        q = i["delay"]
        dl = "–" if not np.isfinite(q[1]) else f"{q[0]:.0f} / {q[1]:.0f} / {q[2]:.0f}"
        L.append(f"| {i['day']} | {i['markets']} | {i['big']:,} | {i['small']:,} | {i['priced'].get(('big', 'book'), 0):,} | "
                 f"{i['priced'].get(('big', 'trade'), 0):,} | {dl} |")
    L += ["", "</details>", "",
          "记录机时钟：触发、盘口和成交都用记录机收到的时间，它比交易所晚（见上表；README 里都柏林盘口录制中位晚约 60 ms，"
          "个别日子币安成交收到得晚好几秒），这对 2 秒后的买入影响小，但收到晚的日子“跳变”本身已经旧了。"
          "逐行数据在 `real/cross-jump2s.csv.gz`（t0 = 收到时刻，t_ex = 币安成交时间，秒）。"]
    return L
