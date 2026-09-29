"""Test the stale-quote effect behind #101 on September 2026 on-chain fills.

    python onchain.py fetch data/onchain --from 2026-09-01 --to 2026-09-26   # Gamma, HF fills, Binance
    python onchain.py run data/onchain --out real/onchain-sept.md
    python onchain.py calibrate data/onchain --prints <dir with last_trade_price files> --out real/onchain-delay.md

Per-second books for September are not public, but every Polymarket trade is
on chain: TimeSeventeen/Polymarket-v2 on Hugging Face (CC-BY-4.0) republishes
the exchange's OrderFilled events day by day. That is enough to ask the
question #101 rests on: after a sharp Binance move, could the token on the
side of the move still be bought at a price that loses money for the seller?

The design below was fixed before any September profit and loss was computed;
it is #101's trigger with the book test replaced by what the chain shows.
- Markets: every {coin}-updown-5m market that starts 2026-09-01 00:00 to
  2026-09-26 23:55 UTC for btc, eth, sol, xrp and doge; outcomes from Gamma.
  Days after 09-26 are kept apart as a later check.
- Trigger: the first second t with 240 >= tau >= 60 at which the Binance 1s
  log move over 10 seconds, known with a 2 second lag as in stage 3, exceeds
  2 sigma (sigma: real_day.MAIN_VOL on Binance). Buy the side of the move.
- Price: a buy limit one tick above that side's price before the move (the
  volume-weighted price of its fills with block time in [t-20, t-10)),
  filled by the fills at or below the limit whose block time falls in
  [t + D, t + D + 5), at their volume-weighted price. No such fills, no
  trade; prices outside [0.05, 0.95] do not trade. Taker fee 0.07 p (1 - p),
  one share, held to settlement.
- D: block time trails the off-chain match by the settlement delay, so a
  fill stamped after t may have been matched before the move was known. D
  is the 95th percentile of that delay, read from how fills die out after
  each market's close (trading stops at the close; what is mined later was
  matched earlier) without looking at profit and loss; 10 seconds if the
  fills do not die out.
- Pass: EV > 0 and exact fair-price p < 0.05 on all five coins pooled, and
  EV > 0 in both halves of the month (09-01..13, 09-14..26).

Reported beside it, not part of the pass test: the pre-move price itself
(out of reach, an upper bound), market prices D and 30-60 seconds after the
move, the rule restricted to triggers with no fill on either token in the
12 seconds before (closest to "the book did not move"), and D from 0 to 20.
A pass says quotes stayed stale long enough for someone to take them at
that delay; the fills were someone's trades, so it says nothing about
queue position, and the capacity at the stale price stays small.
"""
from __future__ import annotations

import argparse
import json
import math
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import binary as bo
import kacho
import paper_trader as pt
import real_day as rd
import strategy_zoo as sz

COINS = ("btc", "eth", "sol", "xrp", "doge")
FIRST, LAST = date(2026, 9, 1), date(2026, 9, 26)
HALF = date(2026, 9, 14)  # second half starts here
FILLS = "https://huggingface.co/datasets/TimeSeventeen/Polymarket-v2/resolve/main/OrderFilled/{d}.parquet"
RAW_COLS = ["timestamp", "token_asset_id", "price", "token_amount", "maker_direction", "taker_direction", "fee_usdc",
            "taker"]
# Kept per fill. `agg` marks the exchange's own event for the aggressor's order (its taker field
# is the exchange contract, the most common taker of the day); the other rows are one per resting
# order filled, with maker_direction the resting order's side.
FILL_COLS = ["timestamp", "token_asset_id", "price", "token_amount", "maker_direction", "taker_direction",
             "fee_usdc", "agg"]
LOOKBACK, Z, TAUS, SIGNAL_LAG = 10, 2.0, (240, 60), 2
WINDOW = 5
QUIET_S = 12
D_FALLBACK = 10
SWEEP = (0, 2, 4, 6, 8, 10, 12, 15, 20)


def paths(root):
    root = Path(root)
    return {"markets": root / "markets.csv", "fills": root / "fills", "binance": root / "binance_{coin}.parquet"}


def days(d0, d1):
    return [d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)]


def epoch(d):
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


# ------------------------------------------------------------------- fetching

def fetch_markets(root, coins=COINS, d0=FIRST, d1=LAST, batch=100, get=kacho._get):
    """Tokens and official outcome of every 5m market starting in [d0, d1]."""
    slugs = [f"{c}-updown-5m-{s}" for c in coins for s in range(epoch(d0), epoch(d1) + 86400, bo.WINDOW_S)]

    def one(chunk):
        q = "&".join(f"slug={s}" for s in chunk) + f"&limit={len(chunk)}"
        rows = []
        for ev in json.loads(get(f"{kacho.GAMMA_EVENTS}?{q}")):
            for m in ev.get("markets") or []:
                m = {**m, "slug": m.get("slug") or ev.get("slug")}
                try:
                    p = pt.parse_gamma_market(m)
                except (ValueError, KeyError, TypeError):
                    continue
                rows.append({"slug": p["slug"], "coin": p["slug"].split("-", 1)[0], "start": p["start"],
                             "up_token": str(p["up_token"]), "down_token": str(p["down_token"]),
                             "up_won": p["up_won"]})
        return rows

    with ThreadPoolExecutor(4) as ex:
        rows = [r for part in ex.map(one, [slugs[i:i + batch] for i in range(0, len(slugs), batch)]) for r in part]
    out = pd.DataFrame(rows, columns=["slug", "coin", "start", "up_token", "down_token", "up_won"])
    out = out.drop_duplicates("slug").sort_values(["coin", "start"])
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    out.to_csv(paths(root)["markets"], index=False)
    return len(out), len(slugs)


def filter_fills(src, tokens, dst):
    """Keep the fills of `tokens` from one OrderFilled day file, row group by row group."""
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq
    pf = pq.ParquetFile(src)
    keep = pa.array(sorted(tokens), type=pa.string())
    cols = [c for c in RAW_COLS if c in pf.schema_arrow.names]
    parts = []
    for i in range(pf.metadata.num_row_groups):
        t = pf.read_row_group(i, columns=cols)
        t = t.cast(pa.schema([(f.name, pa.string() if pa.types.is_large_string(f.type) else f.type)
                              for f in t.schema]))
        parts.append(t.filter(pc.is_in(t["token_asset_id"], value_set=keep)))
    table = pa.concat_tables(parts)
    if "taker" in table.column_names:
        counts = pc.value_counts(table["taker"]).to_pylist()
        exchange = max(counts, key=lambda c: c["counts"])["values"] if counts else None
        table = table.append_column("agg", pc.equal(table["taker"], pa.scalar(exchange, pa.string())))
        table = table.drop(["taker"])
        print(f"  exchange (most common taker): {exchange}", flush=True)
    pq.write_table(table, dst, compression="zstd")
    return pf.metadata.num_rows, table.num_rows


def fetch_fills(root, d0=FIRST, d1=LAST, workdir=None):
    """Download each OrderFilled day, keep the 5m up/down fills, delete the day file."""
    pth = paths(root)
    mk = pd.read_csv(pth["markets"], dtype={"up_token": str, "down_token": str})
    tokens = set(mk["up_token"]) | set(mk["down_token"])
    pth["fills"].mkdir(parents=True, exist_ok=True)
    workdir = Path(workdir or root)
    log = []
    for d in days(d0, d1):
        dst = pth["fills"] / f"{d:%Y_%m_%d}.parquet"
        if dst.exists():
            continue
        tmp = workdir / f"OrderFilled_{d:%Y_%m_%d}.parquet"
        try:
            for k in range(5):
                try:
                    urllib.request.urlretrieve(FILLS.format(d=f"{d:%Y_%m_%d}"), tmp)
                    break
                except Exception:
                    if k == 4:
                        raise
                    time.sleep(2 ** k)
            n, kept = filter_fills(tmp, tokens, dst)
            log.append((d, n, kept))
            print(f"{d}: {n:,} fills, {kept:,} in 5m up/down markets", flush=True)
        except Exception as e:
            print(f"{d}: failed ({e!r})", flush=True)
        finally:
            tmp.unlink(missing_ok=True)
    return log


def fetch_binance(root, coins=COINS, d0=FIRST, d1=LAST):
    for coin in coins:
        sym = f"{coin.upper()}USDT"
        spot = kacho.binance_days(sym, days(d0 - timedelta(days=1), d1 + timedelta(days=1)))
        spot.to_parquet(str(paths(root)["binance"]).format(coin=coin), index=False)
        print(f"{coin}: {len(spot):,} Binance seconds", flush=True)


def load_fills(root, extra=()):
    """All kept fills; tokens as a categorical, since a month of five coins runs to tens of millions of rows."""
    files = sorted(paths(root)["fills"].glob("*.parquet"))
    cols = ["timestamp", "token_asset_id", "price", "token_amount", *extra]
    if not files:
        return pd.DataFrame(columns=cols)
    parts = []
    for p in files:
        f = pd.read_parquet(p, columns=cols)
        parts.append(f[(f["price"] > 0) & (f["price"] < 1) & (f["token_amount"] > 0)])
    f = pd.concat(parts, ignore_index=True)
    f["timestamp"] = f["timestamp"].astype("int64")
    f["token_asset_id"] = f["token_asset_id"].astype("category")
    for c in extra:
        if f[c].dtype == object:
            f[c] = f[c].astype("category")
    return f


# ------------------------------------------------------------------- the test

def settlement_delay(fills, markets, span=40, base=(30, 10)):
    """How fills die out after the close, pooled over markets.

    Trading stops when the window closes, so a fill mined k seconds later was
    matched at least k seconds earlier: the rate of fills at close+k relative
    to the rate just before the close estimates P(delay > k). Returns the
    counts, the base rate, that ratio and the first k where it is below 5%
    (None if it never gets there, e.g. because trading did not stop)."""
    end = {**dict(zip(markets["up_token"], markets["start"] + bo.WINDOW_S)),
           **dict(zip(markets["down_token"], markets["start"] + bo.WINDOW_S))}
    rel = fills["timestamp"].to_numpy() - fills["token_asset_id"].astype(object).map(end).to_numpy(float)
    rel = rel[np.isfinite(rel) & (rel >= -span) & (rel <= span)].astype(int)
    counts = pd.Series(rel).value_counts().reindex(range(-span, span + 1), fill_value=0)
    rate = float(counts.loc[-base[0]:-base[1] - 1].mean())
    tail = counts.loc[0:] / rate if rate > 0 else counts.loc[0:] * np.nan
    below = tail[tail < 0.05]
    return counts, rate, tail, (int(below.index[0]) if len(below) else None)


def robust_delay(tail, level=0.05, ahead=10):
    """First k from which the ratio stays below `level` for `ahead` seconds.

    Polygon mines a block about every two seconds, so single seconds after the
    close can be nearly empty while later ones are not; the first dip alone
    understates the delay."""
    for k in tail.index:
        if tail.loc[k:k + ahead - 1].max() < level:
            return int(k)
    return None


def cadence(fills, markets, at=150, span=20, base=(20, 10)):
    """The same ratio around a second inside the window, where trading goes on:
    how much per-second counts swing from block timing alone."""
    ref = {**dict(zip(markets["up_token"], markets["start"] + at)),
           **dict(zip(markets["down_token"], markets["start"] + at))}
    rel = fills["timestamp"].to_numpy() - fills["token_asset_id"].astype(object).map(ref).to_numpy(float)
    rel = rel[np.isfinite(rel) & (rel >= -span) & (rel <= span)].astype(int)
    counts = pd.Series(rel).value_counts().reindex(range(-span, span + 1), fill_value=0)
    rate = float(counts.loc[-base[0]:-base[1] - 1].mean())
    return counts.loc[0:] / rate if rate > 0 else counts.loc[0:] * np.nan


def triggers(spot, markets, lag=SIGNAL_LAG):
    """First #101 jump per market: (slug, t, side), known at market second t."""
    vols = rd.vol_forecasts(spot)
    log_spot, vol, hist = vols["log_spot"], vols[rd.MAIN_VOL], vols["history_s"]
    ts = np.arange(bo.WINDOW_S - TAUS[0], bo.WINDOW_S - TAUS[1] + 1)
    out = []
    for m in markets.itertuples():
        info = m.start + ts - lag
        move = log_spot.reindex(info).to_numpy() - log_spot.reindex(info - LOOKBACK).to_numpy()
        v = vol.reindex(info).to_numpy()
        ok = hist.reindex(info).to_numpy() >= rd.MIN_HISTORY_S
        with np.errstate(invalid="ignore"):
            hit = ok & (np.abs(move) > Z * v * math.sqrt(LOOKBACK))
        if hit.any():
            i = int(np.argmax(hit))
            out.append((m.slug, int(ts[i]), "Up" if move[i] > 0 else "Down"))
    return pd.DataFrame(out, columns=["slug", "t", "side"])


class Tape:
    """Fills per token, sorted by block time."""

    def __init__(self, fills):
        f = fills.sort_values("timestamp", kind="stable")
        self.by = {tok: (g["timestamp"].to_numpy(), g["price"].to_numpy(float), g["token_amount"].to_numpy(float))
                   for tok, g in f.groupby("token_asset_id", sort=False, observed=True)}

    def vwap(self, tok, lo, hi, limit=None):
        """Volume-weighted price and count of `tok` fills with block time in [lo, hi),
        only those at or below `limit` if given."""
        if tok not in self.by:
            return np.nan, 0
        t, p, s = self.by[tok]
        a, b = np.searchsorted(t, [lo, hi], "left")
        p, s = p[a:b], s[a:b]
        if limit is not None:
            keep = p <= limit + 1e-9
            p, s = p[keep], s[keep]
        return (float(np.dot(p, s) / s.sum()), len(p)) if len(p) else (np.nan, 0)

    def any_in(self, tok, lo, hi):
        if tok not in self.by:
            return False
        a, b = np.searchsorted(self.by[tok][0], [lo, hi], "left")
        return b > a


def prepare(trig, markets):
    tr = trig.join(markets.set_index("slug")[["coin", "start", "up_token", "down_token", "up_won"]], on="slug")
    tr["token"] = np.where(tr["side"] == "Up", tr["up_token"], tr["down_token"])
    tr["abs_t"] = tr["start"] + tr["t"]
    tr["won"] = np.where(tr["side"] == "Up", tr["up_won"].astype(bool), ~tr["up_won"].astype(bool)).astype(float)
    tr["day"] = pd.to_datetime(tr["start"], unit="s", utc=True).dt.date
    return tr.reset_index(drop=True)


def price(tr, tape, lo, hi, stale=True):
    """Price each trigger. stale=True is the tested rule: a buy limit one tick above the
    side's pre-move price (fills in [t-20, t-10)), filled by whatever traded at or below
    it in [t+lo, t+hi). stale=False takes every fill in that window."""
    out = tr.copy()
    ref, px, n = np.full(len(tr), np.nan), np.full(len(tr), np.nan), np.zeros(len(tr), int)
    for i, (tok, t0) in enumerate(zip(tr["token"], tr["abs_t"])):
        ref[i] = tape.vwap(tok, t0 - 20, t0 - 10)[0]
        if stale and not np.isfinite(ref[i]):
            continue
        px[i], n[i] = tape.vwap(tok, t0 + lo, t0 + hi, ref[i] + bo.TICK if stale else None)
    out["ref"], out["price"], out["n_fills"] = ref, px, n
    out["fee"] = bo.taker_fee(out["price"])
    out["pnl"] = out["won"] - out["price"] - out["fee"]
    return out


def stats(tr, reps=20000):
    t = tr[tr["price"].between(*sz.PRICE_BAND)]
    if t.empty:
        return {"n": 0, "win": np.nan, "price": np.nan, "ev": np.nan, "p": 1.0}
    cost = (t["price"] + t["fee"]).to_numpy()
    return {"n": len(t), "win": t["won"].mean(), "price": t["price"].mean(), "ev": t["pnl"].mean(),
            "p": bo.fair_price_pvalue(t["pnl"].to_numpy(), cost, sims=reps) if len(t) >= 10 else 1.0}


def fmt(s):
    if not s["n"]:
        return "0 | – | – | – | –"
    return f"{s['n']:,} | {s['win']:.1%} | {s['price']:.3f} | {100 * s['ev']:+.2f}¢ | {s['p']:.4f}"


HEAD = ["| 样本 | 笔数 | 胜率 | 平均价 | EV/份 | 精确 p |", "|---|---:|---:|---:|---:|---:|"]


def load(root, extra=()):
    pth = paths(root)
    markets = pd.read_csv(pth["markets"], dtype={"up_token": str, "down_token": str})
    markets = markets[markets["up_won"].notna()].copy()
    markets["up_won"] = markets["up_won"].astype(str).str.lower().isin(("true", "1", "1.0"))
    return markets, load_fills(root, extra)


def run(root, out, reps=20000, d0=FIRST, d1=LAST, csv_out=None, delay_json=None):
    pth = paths(root)
    markets, fills = load(root)
    counts, rate, tail, d95 = settlement_delay(fills, markets)
    D = max(2, d95) if d95 is not None else D_FALLBACK
    trig = []
    for coin in sorted(markets["coin"].unique()):
        f = Path(str(pth["binance"]).format(coin=coin))
        if f.exists():
            trig.append(triggers(pd.read_parquet(f), markets[markets["coin"] == coin]))
    trig = prepare(pd.concat(trig, ignore_index=True) if trig else pd.DataFrame(columns=["slug", "t", "side"]),
                   markets)
    tape = Tape(fills)
    main_ = trig[(trig["day"] >= d0) & (trig["day"] <= d1)].reset_index(drop=True)
    later = trig[trig["day"] > d1].reset_index(drop=True)

    prim = price(main_, tape, D, D + WINDOW)
    first = prim["day"] < HALF
    s_all, s_a, s_b = stats(prim, reps), stats(prim[first], reps), stats(prim[~first], reps)
    passed = bool(s_all["n"] and s_all["ev"] > 0 and s_all["p"] < 0.05 and s_a["ev"] > 0 and s_b["ev"] > 0)
    n_days = len(list(pth["fills"].glob("*.parquet")))
    n_mk = int(((markets["start"] >= epoch(d0)) & (markets["start"] < epoch(d1) + 86400)).sum())

    L = [f"# #101 的机制在九月链上成交里还在吗：{d0} 至 {d1}", "",
         "九月没有公开的逐秒盘口，但每一笔成交都在链上（Hugging Face 上的 TimeSeventeen/Polymarket-v2，"
         "OrderFilled 事件）。检验方案写在 `onchain.py` 文件头，**在算任何九月盈亏之前定好**：", "",
         "- 触发：币安 10 秒涨跌超过 2σ（信号延后 2 秒，同第三阶段），τ=240..60 秒内每个市场第一次；",
         "- 下单：按涨跌方向，在该方向代币跳变前的价格（区块时间 [t−20, t−10) 的成交均价）上加 1 格挂买单；",
         f"- 成交：区块时间 [t+D, t+D+{WINDOW}) 内、价格不高于这个限价的真实成交，按量加权；没有就不算交易；",
         "- 付 taker 费 0.07·p(1−p)，每笔 1 份，持有到结算；价格限 0.05–0.95。", "",
         "这批数据**完全没有参与挑选 #101**（#101 是在三到五月的 kacho 数据上选出的），而且九月已经是 60 秒 TWAP 结算。", "",
         f"数据：{n_mk:,} 个已结算的 5 分钟市场（{', '.join(sorted(markets['coin'].unique()))}），"
         f"{n_days} 天链上成交，其中 {len(fills):,} 笔属于这些市场。", "",
         "## 结算延迟 D（只看成交在收盘后怎么消失，不看盈亏）", "",
         f"收盘前 30–10 秒，所有市场合计平均每秒 {rate:.1f} 笔。收盘后第 k 秒的成交数除以这个速率，约等于“上链延迟 > k 秒”的比例：", "",
         "| 收盘后秒数 | " + " | ".join(str(k) for k in (0, 1, 2, 3, 4, 5, 6, 8, 10, 15, 20, 30)) + " |",
         "|---|" + "---:|" * 12,
         "| 相对速率 | " + " | ".join(f"{tail.get(k, np.nan):.3f}" for k in (0, 1, 2, 3, 4, 5, 6, 8, 10, 15, 20, 30)) + " |",
         "",
         (f"95% 的成交在 {d95} 秒内上链，所以 D = {D} 秒（至少 2 秒）。" if d95 is not None else
          f"收盘后成交没有降到 5% 以下（可能收盘后仍在撮合），按事先规定 D = {D_FALLBACK} 秒。"), "",
         "## 主检验", "",
         "事先规定的通过标准：五币合计 EV > 0 且精确 p < 0.05，并且前后半月 EV 都 > 0。", ""] + HEAD + [
         f"| 全部 | {fmt(s_all)} |", f"| 前半月（1–13 日） | {fmt(s_a)} |", f"| 后半月（14–{d1.day} 日） | {fmt(s_b)} |"]
    for coin in sorted(prim["coin"].unique()):
        L.append(f"| {coin} | {fmt(stats(prim[prim['coin'] == coin], reps))} |")
    L += ["", f"触发 {len(main_):,} 次；有跳变前价格的 {int(prim['ref'].notna().sum()):,} 次；"
          f"窗口里有不高于限价的成交的 {int((prim['n_fills'] > 0).sum()):,} 次。", "",
          "**结论：通过。**" if passed else "**结论：没通过。**", "",
          "## 对照（不参与判定）", ""] + HEAD
    rows = (("跳变前价格本身（拿不到，只是上限）", price(main_, tape, -20, -10, stale=False)),
            (f"D={D} 秒后按市价（不限价）", price(main_, tape, D, D + WINDOW, stale=False)),
            ("30–60 秒后按市价（已重新定价）", price(main_, tape, 30, 60, stale=False)))
    for name, t in rows:
        L.append(f"| {name} | {fmt(stats(t, reps))} |")
    quiet = np.array([not (tape.any_in(u, t0 - QUIET_S, t0) or tape.any_in(d, t0 - QUIET_S, t0))
                      for u, d, t0 in zip(prim["up_token"], prim["down_token"], prim["abs_t"])], bool)
    L.append(f"| 主规则，只算前 {QUIET_S} 秒两边都没成交的 | {fmt(stats(prim[quiet], reps))} |")
    L += ["", "### 主规则在不同 D 下", "", "| D | 笔数 | 胜率 | 平均价 | EV/份 | 精确 p |",
          "|---:|---:|---:|---:|---:|---:|"]
    for d in SWEEP:
        L.append(f"| {d} | {fmt(stats(price(main_, tape, d, d + WINDOW), reps))} |")
    L += robustness(main_, tape, markets, fills, tail, D, passed, reps)
    if delay_json and Path(delay_json).exists():
        L += measured_verdict(main_, tape, json.loads(Path(delay_json).read_text()), reps)
    if len(later):
        L += ["", f"## 之后的日子（{later['day'].min()} 起，事后检查，D = {D}）", ""] + HEAD + [
              f"| 全部 | {fmt(stats(price(later, tape, D, D + WINDOW), reps))} |"]
    L += ["", "注意：窗口里的成交是别人真实的成交，说明那个价格当时有人拿到了，不代表我们能排在前面；"
          "吃到过期报价的容量也很小（kacho 数据里卖一中位数只有 25 份）。"]
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text("\n".join(L) + "\n", encoding="utf-8")
    if csv_out:
        prim.drop(columns=["up_token", "down_token", "token"]).to_csv(csv_out, index=False)
    print("\n".join(L))
    return passed


def prints_from_bundles(src):
    """Trade prints (last_trade_price) from recording.py bundles, with the exchange's own timestamp."""
    import gzip
    rows = []
    for f in sorted(Path(src).rglob("*last_trade_price*.jsonl.gz")):
        with gzip.open(f, "rt") as fh:
            for line in fh:
                r = json.loads(line)
                m = r.get("payload") or {}
                try:
                    rows.append((str(r["asset_id"]), float(m["price"]), float(m["size"]), int(r["event_ts_ms"])))
                except (KeyError, TypeError, ValueError):
                    continue
    return pd.DataFrame(rows, columns=["asset_id", "price", "size", "ts_ms"]).drop_duplicates()


def calibrate(prints, fills, before=2, after=60):
    """Block time minus match time for each print found on chain.

    Prints and fills are paired one to one within each (token, price, size):
    in time order, each print takes the first fill not yet taken whose block
    time is at least `before` seconds before it, and the pair is kept if that
    block time is at most `after` seconds after it. Pairing one to one keeps a
    burst of identical fills from all landing on the earliest one, which would
    make the delay look shorter than it is. Block timestamps are whole seconds,
    so -1 < delay < 0 is rounding."""
    def keys(df, tok, px, size):
        return pd.DataFrame({"tok": df[tok].astype(str).to_numpy(),
                             "pk": (df[px].to_numpy(float) * 10_000).round().astype("int64"),
                             "sk": (df[size].to_numpy(float) * 100).round().astype("int64")})

    f = keys(fills, "token_asset_id", "price", "token_amount").assign(ts=fills["timestamp"].to_numpy("int64"))
    p = keys(prints, "asset_id", "price", "size").assign(ts_ms=prints["ts_ms"].to_numpy("int64"))
    f_groups = {k: np.sort(g["ts"].to_numpy()) for k, g in f.groupby(["tok", "pk", "sk"], sort=False)}
    out = []
    for k, g in p.sort_values("ts_ms").groupby(["tok", "pk", "sk"], sort=False):
        fts = f_groups.get(k)
        if fts is None:
            continue
        j = 0
        for ts_ms in g["ts_ms"].to_numpy():
            j = max(j, int(np.searchsorted(fts, ts_ms // 1000 - before, "left")))
            if j >= len(fts):
                break
            if fts[j] <= ts_ms / 1000 + after:
                out.append((k[0], ts_ms, int(fts[j])))
                j += 1
    d = pd.DataFrame(out, columns=["asset_id", "ts_ms", "block_ts"])
    d["delay"] = d["block_ts"] - d["ts_ms"] / 1000
    return d.sort_values("ts_ms").reset_index(drop=True)


def calibration_md(d, n_prints):
    L = ["## 用带撮合时间的成交记录量出的上链延迟", "",
         f"{len(d):,} / {n_prints:,} 条成交推送在链上找到了对应成交（同代币、同价格、同数量，按先后一对一配对）。", ""]
    if d.empty:
        return L
    q = d["delay"].quantile([0.5, 0.9, 0.95, 0.99])
    L += [f"延迟中位 {q[0.5]:.1f} 秒，90% {q[0.9]:.1f} 秒，95% {q[0.95]:.1f} 秒，99% {q[0.99]:.1f} 秒。", "",
          "| k 秒 | " + " | ".join(str(k) for k in range(11)) + " |", "|---|" + "---:|" * 11,
          "| 延迟 > k 的比例 | " + " | ".join(f"{(d['delay'] > k).mean():.3f}" for k in range(11)) + " |"]
    return L


def calibration_run(root, prints_src, out, reps=20000):
    """Delay from exchange-stamped prints, and the rule on those prints by match time."""
    markets, fills = load(root)
    prints = prints_from_bundles(prints_src)
    if prints.empty:
        raise SystemExit(f"no last_trade_price prints under {prints_src}")
    lo, hi = prints["ts_ms"].min() // 1000 - 600, prints["ts_ms"].max() // 1000 + 600
    fills = fills[(fills["timestamp"] >= lo) & (fills["timestamp"] <= hi)]
    d = calibrate(prints, fills)
    L = [f"# 上链延迟校准：{datetime.fromtimestamp(lo + 600, timezone.utc):%Y-%m-%d}", "",
         "成交推送（last_trade_price）带交易所撮合时间，链上 OrderFilled 带区块时间。把两者对上，"
         "就能直接量出链上时间比撮合晚多少，不用再从收盘后的成交去推。", ""] + calibration_md(d, len(prints))
    tokens = set(prints["asset_id"])
    mk = markets[markets["up_token"].isin(tokens) | markets["down_token"].isin(tokens)]
    trig = []
    for coin in sorted(mk["coin"].unique()):
        f = Path(str(paths(root)["binance"]).format(coin=coin))
        if f.exists():
            trig.append(triggers(pd.read_parquet(f), mk[mk["coin"] == coin]))
    if trig:
        tr = prepare(pd.concat(trig, ignore_index=True), mk)
        by_block = Tape(fills)
        by_match = Tape(pd.DataFrame({"token_asset_id": prints["asset_id"], "timestamp": prints["ts_ms"] // 1000,
                                      "price": prints["price"], "token_amount": prints["size"]}))
        L += ["", f"## 同一套规则：按区块时间 vs 按撮合时间（{len(mk):,} 个市场，{len(tr):,} 次触发）", "",
              "限价同主检验（跳变前价格 + 1 格）。按撮合时间的那一列没有上链延迟的问题。", "",
              "| 窗口 | 按区块时间 | 按撮合时间 |", "|---|---|---|"]
        for lo_, hi_ in ((0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (0, 5), (2, 7), (4, 9)):
            a_, b_ = stats(price(tr, by_block, lo_, hi_), reps), stats(price(tr, by_match, lo_, hi_), reps)
            L.append(f"| [t+{lo_}, t+{hi_}) | {fmt(a_)} | {fmt(b_)} |")
        L += ["", "每格：笔数 | 胜率 | 平均价 | EV/份 | 精确 p。"]
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text("\n".join(L) + "\n", encoding="utf-8")
    if len(d):
        q = d["delay"].quantile([0.5, 0.95, 0.99])
        Path(out).with_suffix(".json").write_text(json.dumps({
            "day": f"{datetime.fromtimestamp(lo + 600, timezone.utc):%Y-%m-%d}", "n": len(d),
            "q50": float(q[0.5]), "q95": float(q[0.95]), "q99": float(q[0.99])}), encoding="utf-8")
    print("\n".join(L))
    return d


def measured_verdict(main_, tape, cal, reps):
    """The preregistered intent, D = the 95th percentile of the settlement delay, with the
    delay measured directly from exchange-stamped prints instead of the close."""
    Dm = max(2, math.ceil(cal["q95"]))
    prim = price(main_, tape, Dm, Dm + WINDOW)
    first = prim["day"] < HALF
    s_all, s_a, s_b = stats(prim, reps), stats(prim[first], reps), stats(prim[~first], reps)
    ok = bool(s_all["n"] and s_all["ev"] > 0 and s_all["p"] < 0.05 and s_a["ev"] > 0 and s_b["ev"] > 0)
    L = ["", "## 按直接量出的上链延迟判定", "",
         f"{cal['day']} 的 {cal['n']:,} 条带撮合时间的成交推送和链上成交一一对上（`real/onchain-delay.md`）："
         f"延迟中位 {cal['q50']:.1f} 秒，95% {cal['q95']:.1f} 秒，99% {cal['q99']:.1f} 秒。"
         f"事先规定的意图是 D = 延迟的 95 分位，所以 D = {Dm} 秒。收盘后那截 3% 左右的平尾巴是收盘后仍在撮合，不是延迟。", ""
         ] + HEAD + [f"| 全部 | {fmt(s_all)} |", f"| 前半月 | {fmt(s_a)} |", f"| 后半月 | {fmt(s_b)} |", "",
         f"区块时间在 t+k 的成交，撮合时间一般早 {cal['q50']:.1f} 秒（99% 早不到 {cal['q99']:.1f} 秒）："
         f"上面 1 秒一格的表里，k ≤ {math.floor(cal['q50'])} 的成交大多在决策时点 t 之前撮合"
         f"（比信号晚 2 秒的做法更快的人拿走的），k ≥ {Dm} 的才基本都在 t 之后。", "",
         "**最终结论：通过。**" if ok else
         "**最终结论：没通过。** 过期报价确实存在，但只存在于币安跳变后一两秒内，被更快的程序拿走；"
         "在信号晚 2 秒、按真实延迟排除掉提前撮合的成交之后，没有优势。"]
    return L


def robustness(main_, tape, markets, fills, tail, D, passed, reps):
    """Checks added after the first run showed a pass at D = 2 that vanished at D = 4.
    They do not change the preregistered verdict above; they say whether to believe it."""
    ks = list(range(0, 13)) + [15, 20]
    mid = cadence(fills, markets)
    Dr = robust_delay(tail)
    L = ["", "## 可信度检查（第一次运行之后加的，不改变上面按事先规则的判定）", "",
         "每秒成交数相对收盘前的比例（收盘后），以及窗口中段第 150 秒前后同样算法的比例（那里交易照常，"
         "起伏只来自出块节奏）：", "",
         "| 秒 | " + " | ".join(str(k) for k in ks) + " |", "|---|" + "---:|" * len(ks),
         "| 收盘后 | " + " | ".join(f"{tail.get(k, np.nan):.3f}" for k in ks) + " |",
         "| 窗口中段 | " + " | ".join(f"{mid.get(k, np.nan):.2f}" for k in ks) + " |", ""]
    if Dr is None:
        L.append("收盘后的比例没有连续 10 秒低于 5%，上链延迟的尾巴看不清。")
        rob = None
    else:
        rob = stats(price(main_, tape, Dr, Dr + WINDOW), reps)
        L += [f"要求比例**连续 10 秒**低于 5%，D = {Dr} 秒（事先规则只看第一次低于 5% 的那一秒，得到 {D}）。"
              f"按这个 D，主规则：", ""] + HEAD + [f"| D = {Dr} | {fmt(rob)} |"]
    L += ["", "按区块时间 1 秒一格看主规则（限价同上，窗口 [t+k, t+k+1)）：", "",
          "| k | 笔数 | 胜率 | 平均价 | EV/份 | 精确 p |", "|---:|---:|---:|---:|---:|---:|"]
    for k in range(0, 11):
        L.append(f"| {k} | {fmt(stats(price(main_, tape, k, k + 1), reps))} |")
    ok = rob is not None and rob["n"] and rob["ev"] > 0 and rob["p"] < 0.05
    if passed and not ok:
        L += ["", "**可信度结论：待定。** 事先规则判为通过，但它取的 D 可能太短：收盘后仍有约 10% 的成交在 3–4 秒后"
              "才出现，如果那是上链延迟，区块时间 t+2 之后的成交里就混有触发之前撮合的单子。按“连续 10 秒低于 5%”"
              "取 D，优势消失。反过来，便宜成交的多出部分到 t+3 就几乎没了，比这条延迟尾巴消失得快得多，"
              "说明收盘后的尾巴可能是收盘后仍在撮合，而不是延迟。两种解释要靠带撮合时间的成交记录来分（见 "
              "`onchain.py calibrate`）。无论哪种，优势都只在触发后一两秒内，是拼速度的延迟套利。"]
    elif passed:
        L += ["", "**可信度结论：稳健 D 下仍然通过。**"]
    return L


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=("fetch", "run", "calibrate"))
    ap.add_argument("--prints", help="calibrate: directory with last_trade_price jsonl.gz files")
    ap.add_argument("root")
    ap.add_argument("--from", dest="d0", default=FIRST.isoformat())
    ap.add_argument("--to", dest="d1", default=LAST.isoformat())
    ap.add_argument("--later", help="fetch fills up to this day too (kept apart from the test)")
    ap.add_argument("--coins", default=",".join(COINS))
    ap.add_argument("--workdir")
    ap.add_argument("--out", default="real/onchain-sept.md")
    ap.add_argument("--csv")
    ap.add_argument("--delay-json", help="run: the .json that calibrate writes next to its report")
    ap.add_argument("--reps", type=int, default=20000)
    a = ap.parse_args(argv)
    d0, d1 = date.fromisoformat(a.d0), date.fromisoformat(a.d1)
    last = date.fromisoformat(a.later) if a.later else d1
    coins = tuple(a.coins.split(","))
    if a.cmd == "fetch":
        print("markets:", fetch_markets(a.root, coins, d0, last), flush=True)
        fetch_binance(a.root, coins, d0, last)
        fetch_fills(a.root, d0, last, a.workdir)
    elif a.cmd == "calibrate":
        calibration_run(a.root, a.prints, a.out, a.reps)
    else:
        run(a.root, a.out, a.reps, d0, d1, a.csv, a.delay_json)


if __name__ == "__main__":
    main()
