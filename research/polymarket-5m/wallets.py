"""Who trades BTC markets on Polymarket: bots or people? A heuristic split by wallet behaviour
(exploratory; public market data only).

Polymarket labels no one as a bot. Every data-api trade carries the taker's proxy wallet (and, with
takerOnly=false, the maker's too), so wallets are classed by behaviour over the sample:
- bot: >= 200 trades per active day, or trades in >= 16 distinct UTC hours on some day, or >= 20
  trades within one minute, or >= 100 distinct markets on some day;
- person-like: <= 20 trades per active day, <= 8 distinct hours on every day, never > 5 trades in a
  minute, and >= 50% of its trades at round sizes (whole shares that are multiples of 5, or a notional
  within 1% of $1/$2/$5/$10/$20/$25/$50/$100/$200/$500/$1000);
- uncertain: the rest.
These are heuristics: a careful bot can look like a person and a busy person like a bot.

Samples:
- slow: every closed BTC hit/above/range/daily up-down/4h market of resolved.py (2026-03-14..09-30)
  and nearcert.py (2025-09-15..2026-03-13), from their cached data-api pages (takers only);
- 5m: the BTC 5m up/down markets of one UTC day, fetched here with takerOnly=true and =false
  (takers and makers).

    python wallets.py [--day 2026-10-03] [--out real/wallets.md]
"""
from __future__ import annotations

import argparse
import glob
import gzip
import hashlib
import json
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

S = Path("/tmp/claude-0/-home-user-whole-web-station-clone/d12980d9-5808-53a6-9da0-aeb3bb8bdfb4/scratchpad")
ROUND_USD = np.array([1, 2, 5, 10, 20, 25, 50, 100, 200, 500, 1000], dtype=float)
GAMMA = "https://gamma-api.polymarket.com/events?slug=btc-updown-5m-{ts}"
TRADES = "https://data-api.polymarket.com/trades?market={cid}&limit=500&offset={off}&takerOnly={taker}"


def get(url, cache):
    p = cache / (hashlib.md5(url.encode()).hexdigest() + ".json.gz")
    if p.exists():
        return json.load(gzip.open(p))
    for k in range(5):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "research"})
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read().decode())
            p.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(p, "wt") as f:
                json.dump(d, f)
            time.sleep(0.25)
            return d
        except Exception:
            time.sleep(2 ** k)
    return None


def slow_trades():
    """Rows (ts, side, asset, price, size, wallet) from resolved.py / nearcert.py cached pages."""
    rows = []
    for d in ("resolved", "nearcert"):
        for f in glob.glob(str(S / d / "http" / "*" / "*.json.gz")):
            try:
                x = json.load(gzip.open(f))
            except Exception:
                continue
            if not isinstance(x, list):
                continue
            for r in x:
                if isinstance(r, list) and len(r) >= 8 and isinstance(r[-1], str) and r[-1].startswith("0x") \
                        and len(r[-1]) == 42:
                    rows.append((int(r[0]), r[1], str(r[2]), float(r[3]), float(r[4]), r[-1].lower()))
    t = pd.DataFrame(rows, columns=["ts", "side", "asset", "price", "size", "wallet"]).drop_duplicates()
    return t


def fivemin_trades(day, cache):
    start = int(pd.Timestamp(day, tz="UTC").timestamp())
    rows = []
    for ts in range(start, start + 86400, 300):
        ev = get(GAMMA.format(ts=ts), cache)
        if not ev:
            continue
        for m in ev[0].get("markets", []):
            cid = m.get("conditionId")
            for taker in ("true", "false"):
                off = 0
                while off <= 10000:
                    page = get(TRADES.format(cid=cid, off=off, taker=taker), cache)
                    if not page:
                        break
                    for r in page:
                        w = (r.get("proxyWallet") or "").lower()
                        rows.append((int(r["timestamp"]), r.get("side"), str(r.get("asset")), float(r["price"]),
                                     float(r["size"]), w, taker == "true", cid, r.get("transactionHash")))
                    if len(page) < 500:
                        break
                    off += 500
    t = pd.DataFrame(rows, columns=["ts", "side", "asset", "price", "size", "wallet", "taker_only", "market", "tx"])
    return t


def classify(t, market_col):
    t = t.copy()
    t["day"] = pd.to_datetime(t["ts"], unit="s", utc=True).dt.strftime("%Y-%m-%d")
    t["hour"] = pd.to_datetime(t["ts"], unit="s", utc=True).dt.hour
    t["minute"] = t["ts"] // 60
    notional = t["price"] * t["size"]
    round_sh = (np.abs(t["size"] - np.round(t["size"])) < 1e-6) & (np.round(t["size"]) % 5 == 0)
    round_usd = np.min(np.abs(notional.to_numpy()[:, None] - ROUND_USD[None, :]) / ROUND_USD[None, :], axis=1) <= 0.01
    t["round"] = round_sh | round_usd
    t["notional"] = notional
    g = t.groupby("wallet")
    per_day = t.groupby(["wallet", "day"]).agg(n=("ts", "size"), hours=("hour", "nunique"), mk=(market_col, "nunique"))
    burst = t.groupby(["wallet", "minute"]).size().groupby("wallet").max()
    w = pd.DataFrame({
        "trades": g.size(), "days": g["day"].nunique(), "notional": g["notional"].sum(),
        "round_share": g["round"].mean(),
        "per_day": per_day.groupby("wallet")["n"].mean(), "max_hours": per_day.groupby("wallet")["hours"].max(),
        "max_markets": per_day.groupby("wallet")["mk"].max(), "burst": burst})
    bot = (w["per_day"] >= 200) | (w["max_hours"] >= 16) | (w["burst"] >= 20) | (w["max_markets"] >= 100)
    person = (~bot) & (w["per_day"] <= 20) & (w["max_hours"] <= 8) & (w["burst"] <= 5) & (w["round_share"] >= 0.5)
    w["class"] = np.where(bot, "机器人", np.where(person, "像人", "不确定"))
    return w


def table(w, title):
    tot_w, tot_t, tot_n = len(w), w["trades"].sum(), w["notional"].sum()
    L = [f"### {title}", "", f"钱包 {tot_w:,} 个，成交 {tot_t:,} 笔，成交额 ${tot_n:,.0f}。", "",
         "| 类别 | 钱包数 | 占钱包 | 成交笔数 | 占笔数 | 成交额 | 占成交额 |", "|---|---:|---:|---:|---:|---:|---:|"]
    for c in ("机器人", "不确定", "像人"):
        x = w[w["class"] == c]
        L.append(f"| {c} | {len(x):,} | {len(x) / tot_w:.0%} | {x['trades'].sum():,} | {x['trades'].sum() / tot_t:.0%} | "
                 f"${x['notional'].sum():,.0f} | {x['notional'].sum() / tot_n:.0%} |")
    top = w.sort_values("notional", ascending=False).head(10)
    L += ["", f"成交额最大的 10 个钱包占 {top['notional'].sum() / tot_n:.0%}；其中机器人 {(top['class'] == '机器人').sum()} 个。", ""]
    return L


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--day", default="2026-10-03")
    ap.add_argument("--out", default="real/wallets.md")
    a = ap.parse_args(argv)
    cache = S / "wallets"
    L = ["# BTC 市场里有多少是机器人：按钱包行为粗分（探索性）", "",
         "Polymarket 不标注谁是机器人。这里按每个钱包的行为粗分（规则见 `wallets.py` 文件头）：每天 ≥ 200 笔、"
         "某天 ≥ 16 个小时都在下单、一分钟内 ≥ 20 笔、或某天做 ≥ 100 个市场 → 机器人；每天 ≤ 20 笔、每天 ≤ 8 个小时、"
         "一分钟内 ≤ 5 笔、且一半以上是整数份数或整数金额 → 像人；其余不确定。是启发式，不是事实。", ""]
    slow = slow_trades()
    mk = pd.read_parquet(S / "resolved" / "markets.parquet", columns=["kind", "cid", "yes_token", "no_token"])
    mp = pd.concat([mk[["yes_token", "kind"]].rename(columns={"yes_token": "asset"}),
                    mk[["no_token", "kind"]].rename(columns={"no_token": "asset"})])
    mp["asset"] = mp["asset"].astype(str)
    slow = slow.merge(mp.drop_duplicates("asset"), on="asset", how="left")
    slow["market"] = slow["asset"]
    L += ["## 慢盘（触及、高于、区间、按日涨跌、4 小时；2025-09 至 2026-09；只有吃单方）", ""]
    L += table(classify(slow, "market"), "全部慢盘")
    for kinds, name in ((("above", "range"), "高于 + 区间"), (("hit_daily", "hit_weekly", "hit_monthly"), "触及"),
                        (("updown_day",), "按日涨跌"), (("updown_4h",), "4 小时涨跌")):
        x = slow[slow["kind"].isin(kinds)]
        if len(x):
            L += table(classify(x, "market"), name)
    f5 = fivemin_trades(a.day, cache)
    if len(f5):
        L += [f"## BTC 5 分钟涨跌（{a.day} 一天，288 个市场）", ""]
        tk = f5[f5["taker_only"]].drop_duplicates(["tx", "wallet", "asset", "size", "price"])
        L += table(classify(tk, "market"), "吃单方")
        allp = f5[~f5["taker_only"]].drop_duplicates(["tx", "wallet", "asset", "size", "price"])
        mk_rows = allp.merge(tk[["tx", "wallet"]].drop_duplicates(), on=["tx", "wallet"], how="left", indicator=True)
        maker = mk_rows[mk_rows["_merge"] == "left_only"].drop(columns="_merge")
        if len(maker):
            L += table(classify(maker, "market"), "挂单方（takerOnly=false 里去掉吃单方记录）")
    Path(a.out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
