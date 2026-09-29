"""Analyse one real day of Polymarket BTC 5m markets from the outcometick sample.

    curl -L https://github.com/Ligengxin96/polymarket-data-samples/releases/latest/download/polymarket-data-samples.tar.gz | tar xz
    python real_day.py polymarket-data-samples --out real/2026-09-08.md

The bundle (CC BY 4.0, github.com/Ligengxin96/polymarket-data-samples) holds
one UTC day of BTC 5-minute markets: the unthrottled top of book, the
instantaneous Chainlink feed and the 60s TWAP stream that settles them. Unlike
backtest_book.py this prices every quote with the Asian-digital model, so it
tests the implied-vol idea directly.

Every decision time only sees what had been *received* by then (recv_ms), for
quotes and for Chainlink ticks alike, so there is no look-ahead.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

import backtest_book as bb
import binary as bo

TAUS = (240, 180, 120, 60, 30, 10)
# name: (EWMA half-life in seconds, return horizon in seconds). Chainlink's
# aggregated price is smoothed and has gaps, so 1s returns understate vol;
# the 1s variant is kept to show that trap.
VOL_MODELS = {"30分钟·1秒收益": (1800, 1), "30分钟·15秒收益": (1800, 15), "5分钟·15秒收益": (300, 15)}
MAIN_VOL = "30分钟·15秒收益"
MIN_HISTORY_S = 900
FIXED_POINT = 10**18


# ------------------------------------------------------------------- loading

DAY = None  # --day: pick one UTC day out of a multi-day recording (python -m bot paper writes one file per day)


def _one(root, pattern):
    if DAY:
        pattern = pattern.replace("-*.", f"-{DAY}.")
    hits = sorted(Path(root).rglob(pattern))
    if len(hits) != 1:
        raise SystemExit(f"expected one {pattern} under {root}, found {len(hits)}")
    return hits[0]


def load_markets(root):
    rows = []
    with gzip.open(_one(root, "*-markets-*.jsonl.gz"), "rt") as f:
        for line in f:
            m = json.loads(line)
            outcomes = json.loads(m["raw"]["outcomes"])
            up_i = outcomes.index("Up")
            prices = [float(p) for p in m["outcome_prices"]]
            cfg = m["raw"].get("cryptoMarketConfig") or {}
            rows.append(dict(
                slug=m["slug"], start=int(m["start_sec"]), end=int(m["end_sec"]),
                up_token=m["token_ids"][up_i], down_token=m["token_ids"][1 - up_i],
                resolved=bool(m["resolved"]) and max(prices) == 1.0,
                up_won=prices[up_i] == 1.0,
                strike_int=int(m["strike_value"]) if m.get("strike_value") else None,
                strike=int(m["strike_value"]) / FIXED_POINT if m.get("strike_value") else np.nan,
                lookback=cfg.get("twapLookbackSeconds"),
                fee_rate=(m["raw"].get("feeSchedule") or {}).get("rate"),
            ))
    return pd.DataFrame(rows)


def load_chainlink(path):
    df = pd.read_csv(path, usecols=["feed_ts_ms", "value", "full_accuracy_value", "recv_ms"],
                     dtype={"full_accuracy_value": str})
    df["sec"] = df["feed_ts_ms"] // 1000
    return df.drop_duplicates("sec").sort_values("sec").reset_index(drop=True)


def load_quotes(root, tokens):
    rows = []
    with gzip.open(_one(root, "*-best_bid_ask-*.jsonl.gz"), "rt") as f:
        for line in f:
            d = json.loads(line)
            if d["asset_id"] not in tokens:
                continue
            p = d["payload"]
            bid = float(p["best_bid"]) if p.get("best_bid") not in (None, "") else np.nan
            ask = float(p["best_ask"]) if p.get("best_ask") not in (None, "") else np.nan
            rows.append((d["asset_id"], int(d["recv_ms"]), bid, ask))
    return pd.DataFrame(rows, columns=["token", "recv_ms", "bid", "ask"]).sort_values("recv_ms")


# ----------------------------------------------------------------- features

def spot_grid(spot):
    """Log Chainlink price on a gap-free 1s grid (gaps forward-filled)."""
    grid = spot.set_index("sec")["value"].reindex(
        range(int(spot["sec"].min()), int(spot["sec"].max()) + 1)).ffill()
    return np.log(grid)


def vol_forecasts(spot):
    """Causal per-second vol at each feed second, one column per VOL_MODELS entry.

    k-second log returns are taken at every second (overlapping) and scaled to
    a per-second variance before the EWMA, so every variant is in the same units.
    """
    log_spot = spot_grid(spot)
    out = pd.DataFrame(index=log_spot.index)
    for name, (halflife, k) in VOL_MODELS.items():
        per_sec_var = log_spot.diff(k).pow(2) / k
        out[name] = np.sqrt(per_sec_var.ewm(halflife=halflife, adjust=True, ignore_na=True).mean())
    out["history_s"] = np.arange(len(log_spot))
    out["log_spot"] = log_spot
    return out


def variance_ratio(spot, horizons=(1, 5, 15, 60, 300)):
    log_spot = spot_grid(spot)
    return {k: float(log_spot.diff(k).dropna()[::k].std() / math.sqrt(k)) for k in horizons}


def quote_asof(quotes, token, times_ms):
    q = quotes[quotes["token"] == token]
    left = pd.DataFrame({"t": np.asarray(times_ms, dtype=np.int64)})
    m = pd.merge_asof(left, q, left_on="t", right_on="recv_ms", direction="backward")
    return m["bid"].to_numpy(), m["ask"].to_numpy()


def spot_asof(spot, times_ms):
    """Last Chainlink second whose tick had been received by each time."""
    s = spot.sort_values("recv_ms")
    left = pd.DataFrame({"t": np.asarray(times_ms, dtype=np.int64)})
    m = pd.merge_asof(left, s[["recv_ms", "sec"]], left_on="t", right_on="recv_ms", direction="backward")
    # Ticks can arrive out of order; never use a second later than one already seen.
    return pd.Series(m["sec"]).cummax().to_numpy()


def build_panel(markets, spot, quotes, vols):
    """One row per market and second 0..299 with the book and the model's inputs."""
    first, last = vols.index.min(), vols.index.max()
    rows = []
    for mk in markets.itertuples():
        if not mk.resolved or mk.start - 60 < first or mk.end > last or not np.isfinite(mk.strike):
            continue
        t = np.arange(0, bo.WINDOW_S)
        times_ms = (mk.start + t) * 1000
        bu, au = quote_asof(quotes, mk.up_token, times_ms)
        bd, ad = quote_asof(quotes, mk.down_token, times_ms)
        bid_up = np.fmax(bu, 1 - ad)
        ask_up = np.fmin(au, 1 - bd)
        info_sec = spot_asof(spot, times_ms)
        ok = np.isfinite(info_sec)
        info_sec = np.where(ok, info_sec, mk.start).astype(int)
        t_info = np.clip(info_sec - mk.start, 0, bo.WINDOW_S)
        log_spot = vols["log_spot"].reindex(info_sec).to_numpy()
        # Settlement samples are the seconds end-59..end; sum the ones already seen.
        window = vols["log_spot"].reindex(range(mk.end - bo.TWAP_S + 1, mk.end + 1)).to_numpy()
        seen = np.clip(info_sec - (mk.end - bo.TWAP_S), 0, bo.TWAP_S)
        csum = np.concatenate([[0.0], np.cumsum(window)])
        frame = pd.DataFrame(dict(
            slug=mk.slug, start=mk.start, end=mk.end, t=t, tau=bo.WINDOW_S - t,
            bid_up=bid_up, ask_up=ask_up, up_won=mk.up_won, log_strike=math.log(mk.strike),
            t_info=t_info, log_spot=log_spot, known_sum=csum[seen], spot_ok=ok,
            history_s=vols["history_s"].reindex(info_sec).to_numpy(),
        ))
        for name in VOL_MODELS:
            frame[name] = vols[name].reindex(info_sec).to_numpy()
        rows.append(frame)
    panel = pd.concat(rows, ignore_index=True)
    panel["mid"] = (panel["bid_up"] + panel["ask_up"]) / 2
    panel["ask_down"] = 1 - panel["bid_up"]
    for name in VOL_MODELS:
        panel[f"p[{name}]"] = bo.prob_up(panel["log_spot"], panel["log_strike"], panel[name],
                                         panel["t_info"], panel["known_sum"])
    return panel


# ------------------------------------------------------------------- checks

def settlement_checks(markets, spot, twap):
    """Re-derive outcomes from the TWAP stream with exact fixed-point integers.

    A market is only counted when the exact boundary-second report exists on
    both ends; a neighbouring tick proves nothing about where the boundary landed.
    """
    tw_int = {s: int(v) for s, v in zip(twap["sec"], twap["full_accuracy_value"]) if isinstance(v, str)}
    tw = twap.set_index("sec")["value"]
    inst = spot.set_index("sec")["value"]
    rows = []
    for mk in markets.itertuples():
        if not mk.resolved:
            continue
        open_i, close_i = tw_int.get(mk.start), tw_int.get(mk.end)
        approx = inst.reindex(range(mk.end - bo.TWAP_S + 1, mk.end + 1))
        enough = approx.notna().sum() >= 50 and np.isfinite(mk.strike)
        rows.append(dict(
            has_strike=mk.strike_int is not None,
            strike_is_open=(mk.strike_int == open_i) if mk.strike_int is not None and open_i is not None else np.nan,
            twap_rule=((close_i >= open_i) == mk.up_won) if open_i is not None and close_i is not None else np.nan,
            approx_err=(approx.mean() - tw[mk.end]) if enough and mk.end in tw.index else np.nan,
            approx_rule=((approx.mean() >= mk.strike) == mk.up_won) if enough else np.nan,
        ))
    return pd.DataFrame(rows)


def count(series):
    s = series.dropna().astype(bool)
    return f"{int(s.sum())}/{len(s)}"


# ------------------------------------------------------------------ metrics

def brier(p, y):
    return float(np.mean((p - y) ** 2))


def logloss(p, y):
    p = np.clip(p, 1e-3, 1 - 1e-3)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def usable(snap):
    return snap["spot_ok"] & (snap["history_s"] >= MIN_HISTORY_S) & snap["mid"].between(0, 1) & \
        (snap["ask_up"] > snap["bid_up"] - 1e-9)


def calibration_section(panel):
    lines = ["| 剩余 | 样本 | 盘口 mid | " + " | ".join(f"模型[{n}]" for n in VOL_MODELS) +
             " | 恒猜 0.5 |", "|---:|---:|---:|" + "---:|" * (len(VOL_MODELS) + 1)]
    for tau in TAUS:
        s = panel[(panel["tau"] == tau)]
        s = s[usable(s)]
        y = s["up_won"].astype(float).to_numpy()
        cells = [f"{brier(s['mid'].to_numpy(), y):.4f} / {logloss(s['mid'].to_numpy(), y):.3f}"]
        for n in VOL_MODELS:
            p = s[f"p[{n}]"].to_numpy()
            cells.append(f"{brier(p, y):.4f} / {logloss(p, y):.3f}")
        cells.append(f"{brier(np.full_like(y, 0.5), y):.4f} / {logloss(np.full_like(y, 0.5), y):.3f}")
        lines.append(f"| {tau}s | {len(s)} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def implied_vol_section(panel):
    fast, slow = "30分钟·1秒收益", MAIN_VOL
    lines = ["| 剩余 | 可反推样本 | 隐含年化波动率（中位） | 预测[1秒收益] | IV/预测[1秒] | 预测[15秒收益] | IV/预测[15秒] 中位（四分位） |",
             "|---:|---:|---:|---:|---:|---:|---:|"]
    for tau in TAUS:
        s = panel[(panel["tau"] == tau)]
        s = s[usable(s) & s["mid"].between(0.05, 0.95) & ((s["mid"] - 0.5).abs() > 0.1)]
        iv = bo.implied_vol_s(s["mid"].to_numpy(), s["log_spot"].to_numpy(), s["log_strike"].to_numpy(),
                              s["t_info"].to_numpy(), s["known_sum"].to_numpy())
        ok = np.isfinite(iv)
        if ok.sum() < 5:
            lines.append(f"| {tau}s | {ok.sum()} | – | – | – | – | – |")
            continue
        f1, f15 = s[fast].to_numpy()[ok], s[slow].to_numpy()[ok]
        r1, r15 = iv[ok] / f1, iv[ok] / f15
        q1, q3 = np.percentile(r15, [25, 75])
        lines.append(f"| {tau}s | {ok.sum()} | {np.median(bo.annual_vol(iv[ok]))*100:.0f}% | "
                     f"{np.median(bo.annual_vol(f1))*100:.0f}% | {np.median(r1):.2f} | "
                     f"{np.median(bo.annual_vol(f15))*100:.0f}% | {np.median(r15):.2f}（{q1:.2f}–{q3:.2f}） |")
    return "\n".join(lines)


def strategy_section(panel, fee_rate):
    head = ("| 策略 | 剩余 | 笔数 | 胜率 | 平均成本 | 净 EV/份 | ROI | t 值 | 精确 p |\n"
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    lines = [head]

    def row(name, tau, win, ask):
        n = len(ask)
        if n < 2:
            lines.append(f"| {name} | {tau}s | {n} | – | – | – | – | – | – |")
            return
        cost = ask + bo.taker_fee(ask, fee_rate)
        pnl = win - cost
        sd = pnl.std(ddof=1)
        t = pnl.mean() / (sd / math.sqrt(n)) if sd > 0 else float("nan")
        p = bo.fair_price_pvalue(pnl, cost)
        lines.append(f"| {name} | {tau}s | {n} | {win.mean()*100:.1f}% | {ask.mean():.3f} | "
                     f"{pnl.mean()*100:+.2f}¢ | {pnl.mean()/ask.mean()*100:+.2f}% | {t:+.1f} | {p:.3f} |")

    for tau in TAUS:
        s = panel[(panel["tau"] == tau)]
        s = s[usable(s)]
        up = s["up_won"].to_numpy().astype(float)
        fav_up = s["mid"].to_numpy() >= 0.5
        ask_f = np.where(fav_up, s["ask_up"], s["ask_down"]).round(4)
        win_f = np.where(fav_up, up, 1 - up)
        ask_w = np.where(fav_up, s["ask_down"], s["ask_up"]).round(4)
        m = (ask_f >= 0.80) & (ask_f <= 0.97)
        row("买强势方 0.80–0.97", tau, win_f[m], ask_f[m])
        m = (ask_w >= 0.03) & (ask_w <= 0.20)
        row("买弱势方 0.03–0.20", tau, 1 - win_f[m], ask_w[m])
        for name in VOL_MODELS:
            p = s[f"p[{name}]"].to_numpy()
            au, ad = s["ask_up"].to_numpy(), s["ask_down"].to_numpy()
            e_u = p - au - bo.taker_fee(au, fee_rate)
            e_d = (1 - p) - ad - bo.taker_fee(ad, fee_rate)
            buy_up = e_u >= e_d
            m = np.fmax(e_u, e_d) > 0.01
            ask = np.where(buy_up, au, ad)[m]
            win = np.where(buy_up, up, 1 - up)[m]
            row(f"模型价差[{name}]", tau, win, ask)
    return "\n".join(lines)


def load_book_asks(root, tokens):
    """Full-depth ask ladders per token: {token: [(recv_ms, [(price, size), ...]), ...]}."""
    snaps = {}
    with gzip.open(_one(root, "*-book-*.jsonl.gz"), "rt") as f:
        for line in f:
            d = json.loads(line)
            if d["asset_id"] not in tokens:
                continue
            asks = [(float(a["price"]), float(a["size"])) for a in d["payload"].get("asks", [])]
            snaps.setdefault(d["asset_id"], []).append((int(d["recv_ms"]), asks))
    for v in snaps.values():
        v.sort(key=lambda x: x[0])
    return snaps


def depth_section(root, markets, panel, taus=(60, 30)):
    """How much size sat at the favourite's ask when the 0.80-0.97 favourite trade fired."""
    tok = markets.set_index("slug")[["up_token", "down_token"]]
    cands = []
    for tau in taus:
        s = panel[(panel["tau"] == tau)]
        s = s[usable(s)]
        fav_up = (s["mid"] >= 0.5).to_numpy()
        ask = np.where(fav_up, s["ask_up"], s["ask_down"]).round(4)
        m = (ask >= 0.80) & (ask <= 0.97)
        for r, up, a in zip(s[m].itertuples(), fav_up[m], ask[m]):
            cands.append((tau, tok.at[r.slug, "up_token" if up else "down_token"], (r.start + r.t) * 1000, a))
    books = load_book_asks(root, {c[1] for c in cands})
    rows = []
    for tau, token, t_ms, a in cands:
        snaps = books.get(token, [])
        i = np.searchsorted([x[0] for x in snaps], t_ms, side="right") - 1
        if i < 0 or not snaps[i][1]:
            continue
        ladder = snaps[i][1]
        best = min(p for p, _ in ladder)
        rows.append((tau, sum(z for p, z in ladder if p <= best + 1e-9),
                     sum(z for p, z in ladder if p <= best + 0.01 + 1e-9), a))
    df = pd.DataFrame(rows, columns=["tau", "at_best", "within_1c", "ask"])
    lines = ["| 剩余 | 笔数 | 最优 ask 挂单（份，中位） | 最优 ask 挂单（美元，中位） | ask+1¢ 以内（份，中位） |",
             "|---:|---:|---:|---:|---:|"]
    for tau, g in df.groupby("tau", sort=False):
        lines.append(f"| {tau}s | {len(g)} | {g['at_best'].median():.0f} | {(g['at_best'] * g['ask']).median():.0f} | "
                     f"{g['within_1c'].median():.0f} |")
    return "\n".join(lines)


def book_report(panel, fee_rate):
    """Feed the same per-second book to backtest_book.py's report."""
    markets = panel.groupby("slug").agg(end=("end", "first"), up=("up_won", "first")).reset_index()
    markets = markets.rename(columns={"slug": "cid"}).assign(up=lambda d: d["up"].astype(float), coin="btc")
    ticks = panel.assign(cid=panel["slug"], ts=panel["start"] + panel["t"])[["cid", "ts", "bid_up", "ask_up"]]
    ticks = ticks.dropna(subset=["bid_up", "ask_up"])
    text = bb.report(markets, bb.snapshots(markets, ticks), fee_rate)
    return text.replace("# 真实盘口回测", "").strip()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", help="unpacked polymarket-data-samples directory")
    ap.add_argument("--out", help="write the markdown report here")
    ap.add_argument("--day", help="UTC day (YYYY-MM-DD) when the directory holds several recorded days")
    args = ap.parse_args(argv)
    global DAY
    DAY = args.day

    markets = load_markets(args.root)
    spot = load_chainlink(_one(args.root, "BTCUSD-prices-*.csv.gz"))
    twap = load_chainlink(_one(args.root, "BTCUSD-twap60s-prices-*.csv.gz"))
    tokens = set(markets["up_token"]) | set(markets["down_token"])
    quotes = load_quotes(args.root, tokens)
    vols = vol_forecasts(spot)
    panel = build_panel(markets, spot, quotes, vols)
    checks = settlement_checks(markets, spot, twap)
    fee_rate = float(markets["fee_rate"].dropna().iloc[0]) if markets["fee_rate"].notna().any() else bo.CRYPTO_FEE_RATE
    used = panel["slug"].nunique()
    lag = np.percentile(spot["recv_ms"] - spot["feed_ts_ms"], [50, 95])
    gaps = np.diff(spot["sec"].to_numpy())
    missing_s, max_gap = int((gaps - 1).clip(0).sum()), int(gaps.max())
    vr = variance_ratio(spot)
    day = pd.to_datetime(spot["sec"].median(), unit="s", utc=True).strftime("%Y-%m-%d")

    doc = f"""# 真实数据：{day} BTC 5 分钟市场（单日）

由 `python real_day.py <polymarket-data-samples 目录> --out real/{day}.md` 生成。
数据：[outcometick 免费样例](https://github.com/Ligengxin96/polymarket-data-samples)（CC BY 4.0），
{len(markets)} 个市场，其中 {used} 个完整落在当天且已结算、进入分析。结算规则：60 秒 Chainlink TWAP
（`twapLookbackSeconds` = {sorted(markets['lookback'].dropna().unique().tolist())}），taker 费率 {fee_rate}。
每个决策时点只使用当时**已收到**的报价与 Chainlink tick（按 `recv_ms`），没有前视。

**只有一天、一个币种，下面所有数字的统计意义都很弱，只能当方向性参考。**

## 1. 结算与数据核对

- 官方目标价等于开盘秒的 TWAP60 报价（全精度整数）：{count(checks['strike_is_open'])}；另有 {int((~checks['has_strike']).sum())} 个市场元数据里没有目标价。
- 用 TWAP60 边界秒报价复算输赢（收盘 ≥ 开盘判 Up）与官方一致：{count(checks['twap_rule'])}（只计两端边界秒报价都在的市场）。
- 用 1 秒瞬时价的 60 秒均值近似 TWAP：与官方 TWAP 的误差中位 {checks['approx_err'].abs().median():.2f} 美元，
  输赢一致 {count(checks['approx_rule'])}（60 秒里至少 50 秒有 tick 的市场）。模型用的就是这个近似。
- Up 胜出比例：{panel.groupby('slug')['up_won'].first().mean()*100:.1f}%。
- Chainlink 瞬时价从事件时刻到采集方收到的延迟：中位 {lag[0]/1000:.1f} 秒，p95 {lag[1]/1000:.1f} 秒；
  盘口报价只有约 10 毫秒。只看 Chainlink 定价的人，比直接看交易所行情的做市机器人慢一拍半以上。
- 当天 Chainlink 共缺 {missing_s} 秒（{missing_s / 864:.1f}%），最长断档 {max_gap} 秒。

**波动率估计陷阱**：同一天、同一条 Chainlink 价格，用不同时间间隔的收益率算出的年化波动率：

| 收益间隔 | {" | ".join(f"{k} 秒" for k in vr)} |
|---|{"---:|" * len(vr)}
| 年化波动率 | {" | ".join(f"{bo.annual_vol(v)*100:.1f}%" for v in vr.values())} |

Chainlink 是多家交易所聚合后的价格，本身被平滑过，加上断档，1 秒收益会把波动率低估约三成。
下面的模型主要用 15 秒收益估计（`{MAIN_VOL}`），1 秒版本保留作对照。

## 2. 谁的概率更准：盘口 mid 与亚式二元模型

每格为 Brier 分数 / 对数损失，越低越准。模型用 Chainlink 瞬时价、60 秒 TWAP 结算公式、因果的已实现波动率。

{calibration_section(panel)}

## 3. 隐含波动率 vs 预测波动率

用 `binary.implied_vol_s` 从盘口 mid 反推每个报价隐含的年化波动率，与同一时刻的两种 30 分钟 EWMA 预测波动率比较。
只取 mid 在 0.05–0.95 且离 0.5 超过 0.1 的报价（平值附近隐含波动率无法识别）。IV/预测 > 1 表示按该预测，市场给弱势方定价偏贵；≈ 1 表示市场定价与预测一致。

{implied_vol_section(panel)}

## 4. 吃单策略（按当时 ask 成交，付 taker 费，每笔 1 份，持有到结算）

{strategy_section(panel, fee_rate)}

精确 p：零假设是“每笔真实胜率 = 买价 + 手续费”（扣费后定价公平），模拟总盈亏有多大概率靠运气达到。胜率接近 100% 的小样本 t 值会严重夸大，以 p 为准；这张表有约 20 行，单行 p < 0.05 也可能是运气（多重检验见 `strategy_zoo.py`）。

触发“买强势方 0.80–0.97”时，当时盘口（最近一次全深度快照，按 `recv_ms`）在强势方卖一上挂了多少量：

{depth_section(args.root, markets, panel)}

## 5. `backtest_book.py` 同一份盘口的标准报告

{book_report(panel, fee_rate)}
"""
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(doc, encoding="utf-8")
    print(doc)


if __name__ == "__main__":
    main()
