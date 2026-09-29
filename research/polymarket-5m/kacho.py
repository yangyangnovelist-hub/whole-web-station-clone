"""Test the preregistered candidates on the kacho.io dataset, which played no part in picking them.

    python kacho.py fetch-outcomes data/kacho          # official outcomes from Gamma
    python kacho.py fetch-binance data/kacho           # Binance 1s closes for the same days
    python kacho.py run data/kacho --out real/kacho-btc.md
    python kacho.py stage2 data/kacho --out real/kacho-stage2.md   # after fetching eth/sol/xrp/doge
    python kacho.py stage3 data/kacho --out real/kacho-stage3.md   # stale-book rules, all five coins

kacho.io recorded per-second top of book for every Polymarket BTC 5m market
from 2026-03-24 to 2026-05-18 (CC0, huggingface.co/datasets/kachoio/
polymarket-5-minute-crypto-up-down-markets): best bid, ask and size for Up
(bu, au, su, sau) and Down (bd, ad, sd, sad), and depth within 5c (du, dd).
The container this was written in cannot reach Hugging Face; run it on any
machine that can (download btc_markets.parquet and btc_ticks.parquet first).

That period predates the 60s TWAP settlement, so this asks whether the rules
also made money under the old single-print settlement. Adaptations, all
neutral or conservative:
- a row stamped second s is used only from second s+1 (it may aggregate
  quotes up to s+0.999), and a quote older than 5 seconds counts as missing;
- book imbalance (#80) uses depth within 5c instead of the top three levels;
- Chainlink momentum (#61, #62) runs on Binance 1s closes, since Chainlink
  history is not public (the rule is unchanged; the price source differs);
- there are no trade prints and no strike, so trade-flow, maker and model
  strategies do not trade here.
"""
from __future__ import annotations

import argparse
import io
import json
import math
import time
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import binary as bo
import real_day as rd
import strategy_zoo as sz

GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
BINANCE = "https://data.binance.vision/data/spot/daily/klines/{sym}/1s/{sym}-1s-{d}.zip"
STALE_S = 5

# Stage 2, fixed on 2026-09-29 after the BTC run and before any other coin was
# loaded: the best preregistered candidate on BTC (#62) and the best of all 100
# on BTC (#54), both Binance-momentum rules, tested on the other four coins
# pooled. The signal is lagged 2s, so the quote used is never older than the
# price move that triggers it (kacho stamps quotes per second). Pass: pooled
# p < 0.05 / 2 and EV > 0 at that lag.
STAGE2 = (62, 54)
STAGE2_COINS = ("eth", "sol", "xrp", "doge")
STAGE2_LAG = 2
STAGE3_COINS = ("btc", "eth", "sol", "xrp", "doge")


def paths(root, coin="btc"):
    root = Path(root)
    return {"markets": root / f"{coin}_markets.parquet", "ticks": root / f"{coin}_ticks.parquet",
            "outcomes": root / f"{coin}_outcomes.csv", "binance": root / f"{coin}_binance_1s.parquet"}


def _get(url, timeout=60, tries=5):
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "polymarket-research/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception:
            if k == tries - 1:
                raise
            time.sleep(2 ** k)


def slug_start(slug):
    return int(str(slug).rsplit("-", 1)[1])


# ------------------------------------------------------------------- fetching

def fetch_outcomes(root, coin="btc", batch=100):
    """Official outcome per slug from Gamma events (the dataset's own label is
    inferred from the last tick and is missing or wrong for some markets)."""
    pt = paths(root, coin)
    slugs = pd.read_parquet(pt["markets"], columns=["slug"])["slug"].dropna().tolist()

    def one(chunk):
        q = "&".join(f"slug={s}" for s in chunk) + f"&limit={len(chunk)}"
        rows = []
        for ev in json.loads(_get(f"{GAMMA_EVENTS}?{q}")):
            for m in ev.get("markets") or []:
                outcomes = json.loads(m["outcomes"]) if isinstance(m.get("outcomes"), str) else m.get("outcomes")
                prices = json.loads(m["outcomePrices"]) if isinstance(m.get("outcomePrices"), str) \
                    else m.get("outcomePrices")
                if not (m.get("closed") and outcomes and prices and "Up" in outcomes):
                    continue
                p = [float(x) for x in prices]
                if max(p) == 1.0:
                    rows.append((m.get("slug") or ev.get("slug"), p[outcomes.index("Up")] == 1.0))
        return rows

    with ThreadPoolExecutor(4) as ex:
        rows = [r for part in ex.map(one, [slugs[i:i + batch] for i in range(0, len(slugs), batch)]) for r in part]
    out = pd.DataFrame(rows, columns=["slug", "up_won"]).drop_duplicates("slug")
    out.to_csv(pt["outcomes"], index=False)
    return len(out), len(slugs)


def binance_days(sym, days):
    """Binance 1s closes for `sym` on each of `days` (missing days skipped), as sec,value."""
    def one(d):
        try:
            blob = _get(BINANCE.format(sym=sym, d=d.isoformat()), timeout=120)
        except Exception:
            return None
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            df = pd.read_csv(z.open(z.namelist()[0]), header=None, usecols=[0, 4], names=["open_time", "close"])
        ts = df["open_time"].astype("int64")
        ts = np.where(ts > 10**14, ts // 1000, ts)  # archives moved from ms to µs in 2025
        return pd.DataFrame({"sec": ts // 1000, "value": df["close"].astype(float)})

    with ThreadPoolExecutor(8) as ex:
        parts = [p for p in ex.map(one, days) if p is not None]
    if not parts:
        return pd.DataFrame(columns=["sec", "value"])
    return pd.concat(parts).drop_duplicates("sec").sort_values("sec")


def fetch_binance(root, coin="btc"):
    """Binance <COIN>USDT 1s closes for every day the markets cover, as sec,value."""
    pt, sym = paths(root, coin), f"{coin.upper()}USDT"
    starts = pd.read_parquet(pt["markets"], columns=["slug"])["slug"].dropna().map(slug_start)
    d0 = datetime.fromtimestamp(starts.min() - 3600, timezone.utc).date()
    d1 = datetime.fromtimestamp(starts.max() + 600, timezone.utc).date()
    days = [d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)]
    spot = binance_days(sym, days)
    spot.to_parquet(pt["binance"], index=False)
    return spot["sec"].floordiv(86400).nunique(), len(days)


# -------------------------------------------------------------------- markets

class KachoMarket(sz.Market):
    """Top of book only: the one-level "book" carries depth within 5c."""

    def book(self, token, t_ms):
        r = self.at(int(t_ms // 1000 - self.start))
        if token != self.up_token or r is None or \
                not np.isfinite(np.array([r["bid_up"], r["ask_up"], r["du"], r["dd"]], dtype=float)).all():
            return None
        return [(r["bid_up"], r["du"])], [(r["ask_up"], r["dd"])]

    def prints(self, token, t0_ms, t1_ms):
        return None


def outcome_labels(markets, outcomes=None):
    """up_won per slug: Gamma if given, else a dataset column if one exists."""
    if outcomes is not None:
        return dict(zip(outcomes["slug"], outcomes["up_won"].astype(bool)))
    for col in ("up_won", "outcome", "winner", "result", "resolved_outcome"):
        if col in markets.columns:
            v = markets[col]
            won = v.astype(str).str.lower().isin(["up", "true", "1", "yes"])
            known = v.notna() & (v.astype(str) != "")
            return dict(zip(markets.loc[known, "slug"], won[known]))
    return {}


def build_markets(root, outcomes=None, spot=None, limit=None, coin="btc", signal_lag=1, detail=False):
    """KachoMarkets for one coin. The Binance close stamped s is known from s+1;
    `signal_lag` > 1 withholds it longer, so the book has had time to react.
    `detail` also keeps top-of-book sizes and whether a tick really arrived that
    second (`present`), for telling a still market from a stalled recorder."""
    pt = paths(root, coin)
    mk = pd.read_parquet(pt["markets"])
    labels = outcome_labels(mk, outcomes)
    cid2slug = dict(zip(mk["condition_id"], mk["slug"]))
    book_cols = ["bu", "au", "bd", "ad", "du", "dd"] + (["su", "sau", "sd", "sad"] if detail else [])
    ticks = pd.read_parquet(pt["ticks"], columns=["t", "condition_id"] + book_cols)
    ticks = ticks[ticks["condition_id"].isin(cid2slug)]
    vols = rd.vol_forecasts(spot) if spot is not None and len(spot) else None
    main_vol = rd.MAIN_VOL
    out = []
    grid = np.arange(bo.WINDOW_S)
    for cid, g in ticks.groupby("condition_id", sort=False):
        slug = cid2slug[cid]
        if slug not in labels:
            continue
        start = slug_start(slug)
        g = g.assign(sec=g["t"].astype("int64") - start + 1)  # usable one second after its stamp
        g = g[(g["sec"] >= -STALE_S) & (g["sec"] < bo.WINDOW_S)].drop_duplicates("sec", keep="last")
        raw = g.set_index("sec")[book_cols].astype(float).reindex(range(-STALE_S, bo.WINDOW_S))
        f = raw.ffill(limit=STALE_S).loc[0:]
        bid_up = np.fmax(f["bu"].to_numpy(), 1 - f["ad"].to_numpy()).round(4)
        ask_up = np.fmin(f["au"].to_numpy(), 1 - f["bd"].to_numpy()).round(4)
        rows = pd.DataFrame({"t": grid, "bid_up": bid_up, "ask_up": ask_up, "mid": (bid_up + ask_up) / 2,
                             "ask_down": (1 - bid_up).round(4), "du": f["du"].to_numpy(), "dd": f["dd"].to_numpy()})
        if detail:
            for c in ("su", "sau", "sd", "sad"):
                rows[c] = f[c].to_numpy()
            rows["present"] = raw["bu"].notna().loc[0:].to_numpy()
        if vols is not None:
            info = start + grid - signal_lag
            v = vols.reindex(info)
            rows = rows.assign(t_info=grid - signal_lag, log_spot=v["log_spot"].to_numpy(),
                               history_s=v["history_s"].to_numpy(), **{main_vol: v[main_vol].to_numpy()})
            rows["spot_ok"] = np.isfinite(rows["log_spot"])
        else:
            rows = rows.assign(t_info=0, log_spot=np.nan, history_s=0, spot_ok=False, **{main_vol: np.nan})
        rows[f"p[{main_vol}]"] = np.nan
        out.append(KachoMarket(slug, start, start + bo.WINDOW_S, bool(labels[slug]),
                               labels.get(f"{slug.rsplit('-', 1)[0]}-{start - bo.WINDOW_S}"), "up", "down",
                               rows.set_index("t"), vols["log_spot"] if vols is not None else pd.Series(dtype=float),
                               {}, {}))
        if limit and len(out) >= limit:
            break
    out.sort(key=lambda m: m.start)
    return out


INTRO = """# 预注册候选在 kacho.io 数据上的检验：{span}

这 7 个候选是在 2026-09-08 的 outcometick 数据上选出的（strategy_zoo.PREREGISTERED），kacho.io 的数据没有参与挑选，所以这是一次独立检验。
判定标准和前向检验相同：Bonferroni 校正后 p < 0.05（即原始 p < {alpha:.4f}）。

注意：这段时间还是**单点结算**（60 秒 TWAP 从 2026-08 开始），所以它回答的是“这些规则在旧结算方式下是否也赚钱”。
在这里不过关不能完全否定 TWAP 时代的效果，但过关是很强的证据。改动见 kacho.py 文件头：报价延后 1 秒才使用；
#80 用 5¢ 内深度代替前三档；#61/#62 用币安 1 秒收盘价代替 Chainlink。

数据：kacho.io，{n_mk} 个 BTC 5 分钟市场，每秒最优报价。"""


def load_inputs(root, coin):
    pt = paths(root, coin)
    outcomes = pd.read_csv(pt["outcomes"]) if pt["outcomes"].exists() else None
    spot = pd.read_parquet(pt["binance"]) if pt["binance"].exists() else None
    return outcomes, spot


def run(root, out, reps=20000, limit=None):
    outcomes, spot = load_inputs(root, "btc")
    markets = build_markets(root, outcomes, spot, limit)
    reg = sz.registry()
    ids = list(sz.PREREGISTERED)
    res, trades = sz.evaluate(markets, [reg[i - 1] for i in ids], reps=reps, ids=ids)
    source = "Gamma 官方结算" if outcomes is not None else "数据集自带标签"
    text = sz.report(res, markets, reps, intro=INTRO + f"结果来源：{source}；价格源（#61/#62）："
                     f"{'币安 1 秒' if spot is not None else '无，#61/#62 不交易'}。")
    full, full_trades = sz.evaluate(markets, reg, reps=reps)
    holdout = sz.split_holdout(full_trades, markets, len(reg), reps=reps)
    text += "\n\n---\n\n" + sz.report(full, markets, reps, holdout=holdout, intro=(
        "# 附：100 个策略在 kacho.io 数据上（探索性）\n\n这一节只作参考：在同一份数据上挑最好的一个会重复单日数据的多重检验问题，"
        "判断以上面的预注册检验和本节最后的样本外检验为准。\n\n数据：kacho.io，{n_mk} 个 BTC 5 分钟市场（{span}）。"))
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(text + "\n", encoding="utf-8")
    res.to_csv(Path(out).with_suffix(".csv"), index=False)
    full.to_csv(Path(out).with_name(Path(out).stem + "-all.csv"), index=False)
    return text


STAGE2_INTRO = """# 第二阶段：动量规则在其他币种上的检验

规则在跑之前写死（kacho.STAGE2，2026-09-29）：只检验在 BTC 上表现最好的两条币安动量规则
#62「剩 240 秒、10 秒涨跌超过 2σ 就跟」和 #54「剩 60 秒、10 秒涨跌超过 2σ 就跟」，
数据换成 kacho.io 的 {coins}（这些币种的数据从没用来挑选任何策略），合并检验。
价格信号额外延后到 {lag} 秒，保证用到的盘口报价不会比触发它的价格变动更旧，避免“拿旧报价对新价格”的假优势。
通过标准：合并后 p < 0.05 / 2 = 0.025，且 EV > 0。"""


STAGE3_INTRO = """# 第三阶段：盘口停滞时的错价（kacho.STAGE3）

想法来自“价格是跳着动的”：信息到来时价格跳，没有信息时停住。停住的报价如果没跟上信息或时间，就可能错价。
规则在跑之前写死（2026-09-29），每个市场最多一笔：
- #101「信息到了、盘口没动」：参考价格（币安 1 秒收盘）10 秒内涨跌超过 2σ，而 Up 报价从这次变动开始前就一直没变，
  在剩 240 到 60 秒之间第一次出现时顺着方向买；
- #102「什么都没发生、时间在走」：Up 报价 20 秒没变、参考价格 20 秒内变动不到 0.5σ，
  在剩 120 到 20 秒之间第一次出现时买价格在 0.60–0.95 的强势方。
数据：kacho.io 的 {coins}（2026-03 至 05，单点结算时期）。信号延后 {lag} 秒。
通过标准：合并后 p < 0.05 / 2 = 0.025，且 EV > 0。
说明：#101 的“动量”部分和前两阶段相关，这些币种的数据也在前两阶段用过，所以这不是完全干净的独立检验；
过关后还要在前向数据上复核。"""


def pooled_test(root, out, rules, coins, intro, reps=20000, lags=(STAGE2_LAG, 1)):
    """Run `rules` ({id: Strategy}) on every coin, pool the trades, and report a
    Bonferroni-corrected test at lags[0] (the preregistered lag) plus the others."""
    ids = list(rules)
    strategies = [rules[i] for i in ids]
    pooled = {lag: [] for lag in lags}
    starts = {lag: [] for lag in lags}
    per_coin, missing = [], []
    for coin in coins:
        pt = paths(root, coin)
        if not (pt["markets"].exists() and pt["ticks"].exists()):
            missing.append(coin)
            continue
        outcomes, spot = load_inputs(root, coin)
        if spot is None:
            missing.append(f"{coin}（无币安价格）")
            continue
        for lag in lags:
            markets = build_markets(root, outcomes, spot, coin=coin, signal_lag=lag)
            trades, _ = sz.run_all(markets, strategies)
            ms = [m.start for m in markets]
            pooled[lag].append(trades)
            starts[lag] += ms
            res = sz.summarize(trades, strategies, ms or [0], reps, ids)
            per_coin += [{"coin": coin, "lag": lag, "markets": len(markets), **r} for r in res.to_dict("records")]
            print(f"{coin} lag={lag}: {len(markets)} markets, {len(trades)} trades", flush=True)
            del markets
    lines = [intro.format(coins="、".join(c.upper() for c in coins), lag=lags[0])]
    if missing:
        lines += ["", f"缺数据、未参与：{'、'.join(missing)}。"]
    verdict = None
    for lag in lags:
        trades = pd.concat(pooled[lag], ignore_index=True) if pooled[lag] else pd.DataFrame(columns=["strategy"])
        res = sz.summarize(trades, strategies, starts[lag] or [0], reps, ids)
        head = "主检验" if lag == lags[0] else f"参考（信号延后 {lag} 秒）"
        lines += ["", f"## {head}：信号延后 {lag} 秒，{len(starts[lag])} 个市场", "",
                  "| # | 策略 | 笔数 | 胜率 | 平均价 | EV/份 | ROI | 原始 p | 校正 p | 前半 EV | 后半 EV | 通过 |",
                  "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|:-:|"]
        for r in res.itertuples():
            ok = lag == lags[0] and r.n >= 10 and r.p < 0.05 / len(ids) and r.ev > 0
            lines.append(f"| {r.id} | {r.name} | {r.n} | {sz.fmt(r.win * 100 if r.n else np.nan, '.0f')}% | "
                         f"{sz.fmt(r.price, '.3f')} | {sz.fmt(r.ev * 100 if r.n else np.nan, '+.2f')}¢ | "
                         f"{sz.fmt(r.roi * 100 if r.n else np.nan, '+.1f')}% | {r.p:.4f} | {r.p_fwer:.4f} | "
                         f"{sz.fmt(r.ev1 * 100 if np.isfinite(r.ev1) else np.nan, '+.2f')}¢ | "
                         f"{sz.fmt(r.ev2 * 100 if np.isfinite(r.ev2) else np.nan, '+.2f')}¢ | {'✓' if ok else ''} |")
        if lag == lags[0]:
            verdict = res.assign(passed=(res["n"] >= 10) & (res["p"] < 0.05 / len(ids)) & (res["ev"] > 0))
    pc = pd.DataFrame(per_coin)
    if not pc.empty:
        lines += ["", "## 分币种", "", "| 币种 | 信号延后 | # | 市场 | 笔数 | EV/份 | 原始 p |", "|---|---:|---:|---:|---:|---:|---:|"]
        for r in pc.itertuples():
            lines.append(f"| {r.coin.upper()} | {r.lag}s | {r.id} | {r.markets} | {r.n} | "
                         f"{sz.fmt(r.ev * 100 if r.n else np.nan, '+.2f')}¢ | {r.p:.3f} |")
    if verdict is not None:
        n_pass = int(verdict["passed"].sum())
        lines += ["", f"**结论：{n_pass} / {len(ids)} 条规则通过。**" + (
            "通过只说明在这段历史数据上有统计优势；成交按显示的卖一价计算，真实下单可能更差，必须先小仓位实盘验证。"
            if n_pass else "没有规则通过。")]
    text = "\n".join(lines) + "\n"
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(text, encoding="utf-8")
    pc.to_csv(Path(out).with_suffix(".csv"), index=False)
    return text


def stage2(root, out, coins=STAGE2_COINS, reps=20000, lags=(STAGE2_LAG, 1)):
    reg = sz.registry()
    return pooled_test(root, out, {i: reg[i - 1] for i in STAGE2}, coins, STAGE2_INTRO, reps, lags)


def stage3(root, out, coins=STAGE3_COINS, reps=20000, lags=(STAGE2_LAG, 1)):
    return pooled_test(root, out, dict(sz.STALE), coins, STAGE3_INTRO, reps, lags)


STAGE4 = (103, 104)
STAGE4_INTRO = """# 第四阶段：九月链上规律的吃单版本放到 3–5 月（kacho.STAGE4）

#103（剩 90 秒）和 #104（剩 45 秒）：强势方卖一在 0.60–0.80 时吃单买入。它们来自 9 月 1–25 日链上成交里
“吃单方买 0.6–0.8 的强势方赚钱”的格子（`makers.py`），在 2026-09-29 写死。kacho.io 的 {coins}（2026-03 至 05）
没有参与设计，所以这是独立检验；但那是单点结算时期，不是现在的 60 秒 TWAP。
挂单版本（#105–#108）需要成交记录，kacho 数据没有，不在这里检验。
通过标准：合并后 p < 0.05 / 2 = 0.025，且 EV > 0。"""


def stage4(root, out, coins=STAGE3_COINS, reps=20000):
    return pooled_test(root, out, {i: sz.ONCHAIN[i] for i in STAGE4}, coins, STAGE4_INTRO, reps, lags=(1,))


DIAG_INTRO = """# #101 是真的还是录制假象？

#101 挑的是“参考价格已经大幅变动、Up 报价却 12 秒以上一动不动”的时刻。这也正是录制程序卡住时的样子：
如果 kacho 的录制断了，它每秒写下的是冻结的旧报价，看起来像盘口没动，其实真实市场早变了，这些“便宜价”并不存在。

区分方法（跑之前写死）：真实市场里，即使最优价不变，挂单量和 5¢ 内深度也会不停变化；录制卡住时它们一起冻结。
只看“这段时间内每秒都有真实记录、并且挂单量或深度有变化”（录制确实是活的）的交易，
这部分每份仍为正且 p < 0.05，#101 才算可信。信号延后 {lag} 秒，和第三阶段相同。"""


def diagnose_101(root, out, coins=STAGE3_COINS, lag=STAGE2_LAG, reps=20000):
    """Split #101's trades by whether the recorder was demonstrably live while the
    quote sat still, and describe what the book did next."""
    rule = sz.STALE[101]
    lookback = 10
    rows = []
    for coin in coins:
        pt = paths(root, coin)
        if not (pt["markets"].exists() and pt["ticks"].exists()):
            continue
        outcomes, spot = load_inputs(root, coin)
        if spot is None:
            continue
        for m in build_markets(root, outcomes, spot, coin=coin, signal_lag=lag, detail=True):
            tr = rule.fn(m)
            if tr is None:
                continue
            t = tr["t"]
            r = m.rows
            w = r.loc[max(0, t - lag - lookback):t]
            sizes = w[["su", "sau", "sd", "sad"]].to_numpy(float)
            depth = w[["du", "dd"]].to_numpy(float)
            moved = lambda a: bool(len(a) > 1 and (np.nan_to_num(np.diff(a, axis=0), nan=1.0) != 0).any())
            after = r.loc[t + 1:]
            diff = after[(after["bid_up"] != r.at[t, "bid_up"]) | (after["ask_up"] != r.at[t, "ask_up"])]
            nxt = int(diff.index[0]) if len(diff) else None
            toward = None
            if nxt is not None:
                dmid = r.at[nxt, "mid"] - r.at[t, "mid"]
                toward = bool((dmid > 0) == (tr["side"] == "Up")) if dmid != 0 else None
            won = (tr["side"] == "Up") == m.up_won
            rows.append({"coin": coin, "slug": m.slug, "start": m.start, "abs": m.start + t, "t": t,
                         "side": tr["side"], "price": tr["price"], "fee": tr["fee"], "won": won,
                         "pnl": float(won) - tr["price"] - tr["fee"],
                         "all_present": bool(w["present"].all()), "sizes_moved": moved(sizes),
                         "depth_moved": moved(depth), "next_change_s": None if nxt is None else nxt - t,
                         "moved_toward": toward,
                         "ask_size": float(r.at[t, "sau"] if tr["side"] == "Up" else r.at[t, "sad"])})
    d = pd.DataFrame(rows)
    lines = [DIAG_INTRO.format(lag=lag), ""]
    if d.empty:
        text = "\n".join(lines + ["#101 没有成交。"]) + "\n"
        Path(out).write_text(text, encoding="utf-8")
        return text
    d["live"] = d["all_present"] & (d["sizes_moved"] | d["depth_moved"])
    d = d.sort_values("abs")
    d["cluster"] = d["abs"].map(lambda a: int(((d["abs"] - a).abs() <= 5).sum()))

    def row(name, g):
        if len(g) < 2:
            return f"| {name} | {len(g)} | – | – | – | – |"
        cost = (g["price"] + g["fee"]).to_numpy()
        p = bo.fair_price_pvalue(g["pnl"].to_numpy(), cost, sims=reps)
        return (f"| {name} | {len(g)} | {g['won'].mean() * 100:.0f}% | {g['price'].mean():.3f} | "
                f"{g['pnl'].mean() * 100:+.2f}¢ | {p:.4f} |")

    lines += ["| 子集 | 笔数 | 胜率 | 平均价 | EV/份 | p |", "|---|---:|---:|---:|---:|---:|",
              row("全部", d),
              row("**录制确实是活的**（每秒有记录，且挂单量或深度有变化）", d[d["live"]]),
              row("每秒有记录，但挂单量和深度都冻结", d[d["all_present"] & ~d["live"]]),
              row("窗口内有缺秒", d[~d["all_present"]]),
              row("同一时刻（±5 秒）只有这一笔", d[d["cluster"] == 1]),
              row("同一时刻有多个币种同时触发", d[d["cluster"] > 1])]
    for coin, g in d.groupby("coin"):
        lines.append(row(f"{coin.upper()}（录制活着）", g[g["live"]]))
    nc = d["next_change_s"].dropna()
    tw = d["moved_toward"].dropna()
    lines += ["", f"- 触发后报价多久才变：中位 {nc.median():.0f} 秒，75% 分位 {nc.quantile(.75):.0f} 秒（{len(nc)} 笔有后续变化）。",
              f"- 报价变化的方向和我们买的方向一致：{tw.mean() * 100:.0f}%（{len(tw)} 笔）。真实的“盘口没跟上”应该明显高于 50%。",
              f"- 触发时这一边卖一上的挂单量：中位 {d['ask_size'].median():.0f} 份，25% 分位 {d['ask_size'].quantile(.25):.0f} 份。"]
    live = d[d["live"]]
    ok = False
    if len(live) >= 10:
        p = bo.fair_price_pvalue(live["pnl"].to_numpy(), (live["price"] + live["fee"]).to_numpy(), sims=reps)
        ok = live["pnl"].mean() > 0 and p < 0.05
    lines += ["", "**判定：" + ("可信。录制确实活着的那部分仍显著为正。" if ok else
                               "不可信或证据不足。录制确实活着的那部分不显著为正，优势很可能来自录制冻结。") + "**"]
    text = "\n".join(lines) + "\n"
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(text, encoding="utf-8")
    d.to_csv(Path(out).with_suffix(".csv"), index=False)
    return text


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("fetch-outcomes", "fetch-binance", "run", "stage2", "stage3", "stage4", "diag101"):
        p = sub.add_parser(name)
        p.add_argument("root", help="放 <coin>_markets.parquet 和 <coin>_ticks.parquet 的目录")
        if name.startswith("fetch"):
            p.add_argument("--coin", default="btc")
        else:
            p.add_argument("--out", default={"run": "real/kacho-btc.md", "stage2": "real/kacho-stage2.md",
                                             "stage3": "real/kacho-stage3.md", "stage4": "real/kacho-stage4.md",
                                             "diag101": "real/kacho-diag101.md"}[name])
            p.add_argument("--reps", type=int, default=20000)
        if name == "run":
            p.add_argument("--limit", type=int, help="只用前 N 个市场（调试用）")
    args = ap.parse_args(argv)
    if args.cmd == "fetch-outcomes":
        print("%s outcomes: %d of %d slugs" % (args.coin, *fetch_outcomes(args.root, args.coin)))
    elif args.cmd == "fetch-binance":
        print("%s binance days: %d of %d" % (args.coin, *fetch_binance(args.root, args.coin)))
    elif args.cmd == "stage2":
        print(stage2(args.root, args.out, reps=args.reps))
    elif args.cmd == "stage3":
        print(stage3(args.root, args.out, reps=args.reps))
    elif args.cmd == "stage4":
        print(stage4(args.root, args.out, reps=args.reps))
    elif args.cmd == "diag101":
        print(diagnose_101(args.root, args.out, reps=args.reps))
    else:
        print(run(args.root, args.out, args.reps, args.limit))


if __name__ == "__main__":
    main()
