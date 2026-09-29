"""Accumulate forward-test trades of the preregistered candidates across recording runs.

    python strategy_zoo.py bundle --confirm --trades-out new.csv   # one recording run
    python forward.py add new.csv --markets bundle                  # append to forward/
    python forward.py report                                        # forward/confirm.md

Recording usually happens in separate sessions of a few hours or days, so the
evidence has to be pooled. Trades are keyed by (strategy id,
market slug) and never double-counted; the report is the same --confirm test
as strategy_zoo.py, on every market recorded so far.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

import paper_trader as pt
import real_day as rd
import strategy_zoo as sz

HERE = Path(__file__).parent
ONCHAIN_ADDED = 1_790_704_800  # 2026-09-29 18:00 UTC: the first recording run that evaluates #103-#106
STORE = HERE / "forward"
TRADE_COLS = ["id", "slug", "start", "side", "price", "fee", "won", "pnl", "t", "kind"]


def add(new_trades, bundle, store=STORE):
    """Merge one run's trades and markets into the store; returns (trades, markets) totals."""
    store = Path(store)
    store.mkdir(parents=True, exist_ok=True)
    tpath, mpath = store / "trades.csv", store / "markets.csv"
    new = pd.read_csv(new_trades) if Path(new_trades).stat().st_size else pd.DataFrame(columns=TRADE_COLS)
    old = pd.read_csv(tpath) if tpath.exists() else pd.DataFrame(columns=TRADE_COLS)
    trades = pd.concat([old, new.reindex(columns=TRADE_COLS)], ignore_index=True) \
        .drop_duplicates(["id", "slug"], keep="first").sort_values(["start", "id"])
    trades.to_csv(tpath, index=False)
    mk = rd.load_markets(bundle)
    mk = mk.loc[mk["resolved"], ["slug", "start"]]
    old_mk = pd.read_csv(mpath) if mpath.exists() else pd.DataFrame(columns=["slug", "start"])
    markets = pd.concat([old_mk, mk], ignore_index=True).drop_duplicates("slug").sort_values("start")
    markets.to_csv(mpath, index=False)
    return len(trades), len(markets)


def add_ledger(ledger_csv, store=STORE):
    """Pool paper_trader's ledger.csv across runs and rewrite forward/paper_report.md."""
    store = Path(store)
    store.mkdir(parents=True, exist_ok=True)
    path = store / "paper_ledger.csv"
    parts = [pd.read_csv(f) for f in (path, Path(ledger_csv)) if f.exists() and f.stat().st_size]
    if not parts:
        return 0
    ledger = pd.concat(parts, ignore_index=True).drop_duplicates(["slug", "tau"]).sort_values(["end", "tau"])
    ledger.to_csv(path, index=False)
    (store / "paper_report.md").write_text(
        f"# 纸面账（所有录制累计，只模拟不下单）\n\n{pt.summarize(ledger)}\n", encoding="utf-8")
    return len(ledger)


def report(store=STORE, reps=20000, onchain_since=None):
    store = Path(store)
    tpath, mpath = store / "trades.csv", store / "markets.csv"
    if not mpath.exists():
        return "# 预注册候选的前向检验\n\n还没有录到数据。\n"
    markets = pd.read_csv(mpath)
    trades = pd.read_csv(tpath) if tpath.exists() else pd.DataFrame(columns=TRADE_COLS)
    ids = list(sz.PREREGISTERED)
    reg = sz.registry()
    strategies = [reg[i - 1] for i in ids]
    trades = trades[trades["id"].isin(ids)].assign(strategy=lambda d: d["id"].map(ids.index))
    trades["won"] = trades["won"].astype(str).str.lower().isin(["true", "1"])
    res = sz.summarize(trades, strategies, markets["start"].tolist(), reps, ids)
    shells = [SimpleNamespace(start=int(s)) for s in markets["start"]]
    text = sz.report(res, shells, reps, confirm=True) + "\n"
    extra = pd.read_csv(tpath) if tpath.exists() else pd.DataFrame(columns=TRADE_COLS)
    extra["won"] = extra["won"].astype(str).str.lower().isin(["true", "1"])
    one = extra[extra["id"] == 101].assign(strategy=0)
    res101 = sz.summarize(one, [sz.STALE[101]], markets["start"].tolist(), reps, [101])
    text += "\n---\n\n" + sz.report(res101, shells, reps, intro=(
        "# #101 的前向检验：{span}\n\n#101（盘口未动的价格跳变）在 kacho 第三阶段通过后才加入前向检验，单独检验："
        "原始 p < 0.05 且 EV > 0 才算在现行市场里复现。只用加入之后录制的数据。\n\n数据：纸面交易录制，{n_mk} 个 5 分钟市场，60 秒 TWAP 结算。")) + "\n"
    ids = sorted(sz.ONCHAIN)
    since = ONCHAIN_ADDED if onchain_since is None else onchain_since
    later = markets[markets["start"] >= since]
    if len(later):
        fam = extra[extra["id"].isin(ids)].assign(strategy=lambda d: d["id"].map(ids.index))
        res = sz.summarize(fam, [sz.ONCHAIN[i] for i in ids], later["start"].tolist(), reps, ids)
        text += "\n---\n\n" + sz.report(res, [SimpleNamespace(start=int(s)) for s in later["start"]], reps, intro=(
            "# 九月链上规律的前向检验：{span}\n\n#103–#108 来自 `makers.py` 在九月链上成交里看到的规律"
            "（那些是别人主动成交的平均结果），这里检验能直接执行的版本，"
            + ("只用加入之后录制的数据。" if since >= ONCHAIN_ADDED else
               "这里是重放：数据在规则定下之前录的，但它们（9 月 28–29 日）没有参与规则的设计（设计只用了 9 月 1–25 日的链上成交）。")
            + "6 条一起按 Bonferroni 校正：原始 p < {alpha:.4f} 且 EV > 0 才算通过。#105/#106 的挂单只在之后有更低的成交价时才算成交；#107/#108 按排队顺序，以挂单价卖出的量超过挂单时排在前面的量才算成交。"
            "\n\n数据：纸面交易录制，{n_mk} 个 5 分钟市场，60 秒 TWAP 结算。")) + "\n"
    return text


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("trades")
    a.add_argument("--markets", required=True, help="recording.py 生成的 bundle 目录")
    a.add_argument("--store", default=str(STORE))
    lg = sub.add_parser("add-ledger")
    lg.add_argument("ledger", help="paper_trader.py live 输出目录里的 ledger.csv")
    lg.add_argument("--store", default=str(STORE))
    r = sub.add_parser("report")
    r.add_argument("--store", default=str(STORE))
    r.add_argument("--out", default=str(STORE / "confirm.md"))
    r.add_argument("--reps", type=int, default=20000)
    r.add_argument("--onchain-since", type=int, help="#103-#108 on markets from this unix second (default: when they were added)")
    args = ap.parse_args(argv)
    if args.cmd == "add-ledger":
        print(f"paper ledger: {add_ledger(args.ledger, args.store)} trades")
    elif args.cmd == "add":
        n_trades, n_markets = add(args.trades, args.markets, args.store)
        print(f"store: {n_trades} trades, {n_markets} markets")
    else:
        text = report(args.store, args.reps, args.onchain_since)
        Path(args.out).write_text(text, encoding="utf-8")
        print(text)


if __name__ == "__main__":
    main()
