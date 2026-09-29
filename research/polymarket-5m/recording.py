"""Turn paper_trader recordings into an outcometick-style bundle.

    python recording.py paper_data --out paper_data/bundle
    python recording.py paper_data --fetch-outcomes     # after a run stops: resolve what it missed
    python strategy_zoo.py paper_data/bundle --confirm --out paper/confirm.md

`paper_trader.py live` saves every websocket message it receives under
raw/<day>/{clob,rtds,markets}.jsonl. This rebuilds the files real_day.py and
strategy_zoo.py read, so every strategy can be re-tested on data recorded
after it was chosen:

- Chainlink BTC/USD from RTDS, one row per feed second;
- top of book and full-depth snapshots (one per token per second, the state
  after the last message received in that second), by replaying the CLOB
  messages through paper_trader.apply_clob_message;
- last_trade_price prints as received;
- markets with the official outcome from the latest Gamma snapshot. Gamma
  does not publish the price to beat, so the strike is the mean of the
  recorded Chainlink prices over the 60 seconds ending at the open, the same
  approximation that reproduced 265 of 266 outcomes on the sample day.

All recorded days go into one bundle, so the forward sample keeps growing.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import time
from pathlib import Path

import numpy as np

import binary as bo
import paper_trader as pt
from real_day import FIXED_POINT


def iter_jsonl(paths):
    for path in paths:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:  # a killed recorder can leave half a line
                    continue


def raw_files(src, name):
    return sorted(Path(src).glob(f"raw/*/{name}.jsonl"))


def chainlink_ticks(records, symbol="btc/usd"):
    """(feed_ms, value, recv_ms) for one symbol from recorded RTDS messages."""
    for rec in records:
        msg = rec.get("msg") or {}
        if msg.get("topic") != "crypto_prices_chainlink":
            continue
        payload = msg.get("payload") or {}
        if str(payload.get("symbol", "")).lower() != symbol:
            continue
        points = payload.get("data") if isinstance(payload.get("data"), list) else [payload]
        for pnt in points:
            if pnt.get("value") is None or pnt.get("timestamp") is None:
                continue
            ts = int(pnt["timestamp"])
            yield (ts * 1000 if ts < 10**11 else ts), float(pnt["value"]), int(rec["recv_ms"])


def market_rows(records, spot_by_sec):
    """Latest Gamma snapshot per slug, in the outcometick markets layout."""
    latest = {}
    for m in records:
        if isinstance(m, dict) and str(m.get("slug", "")).startswith("btc-updown-5m-"):
            latest[m["slug"]] = m
    rows = []
    for slug, m in sorted(latest.items()):
        try:
            mk = pt.parse_gamma_market(m)
        except (ValueError, KeyError):
            continue
        outcomes = json.loads(m["outcomes"]) if isinstance(m["outcomes"], str) else m["outcomes"]
        tokens = json.loads(m["clobTokenIds"]) if isinstance(m["clobTokenIds"], str) else m["clobTokenIds"]
        prices = m.get("outcomePrices") or ["0.5", "0.5"]
        prices = json.loads(prices) if isinstance(prices, str) else prices
        window = [spot_by_sec[s] for s in range(mk["start"] - bo.TWAP_S + 1, mk["start"] + 1) if s in spot_by_sec]
        strike = str(int(round(np.mean(window) * FIXED_POINT))) if len(window) >= 50 else None
        rows.append({"slug": slug, "start_sec": mk["start"], "end_sec": mk["end"],
                     "resolved": mk["up_won"] is not None, "token_ids": tokens,
                     "outcome_prices": [str(p) for p in prices], "strike_value": strike,
                     "raw": {**m, "outcomes": json.dumps(outcomes)}})
    return rows


def fetch_outcomes(src, fetch=pt.fetch_json, now=None):
    """Append resolved Gamma records for recorded markets that have none yet
    (a run that stops right after a close never saw those markets resolve).
    Returns the number still unresolved."""
    src, now = Path(src), now or time.time()
    latest = {}
    for m in iter_jsonl(raw_files(src, "markets")):
        if isinstance(m, dict) and str(m.get("slug", "")).startswith("btc-updown-5m-"):
            latest[m["slug"]] = m
    rec, missing = pt.Recorder(src), 0
    for slug, m in sorted(latest.items()):
        try:
            if pt.parse_gamma_market(m)["up_won"] is not None or pt.parse_gamma_market(m)["end"] > now - 30:
                continue
            fresh = pt.fetch_market(fetch, slug)
        except Exception as e:  # keep going; the market simply stays unresolved
            rec.write("errors", {"at": pt.now_ms(), "where": "fetch_outcomes", "slug": slug, "err": repr(e)})
            missing += 1
            continue
        if fresh and pt.parse_gamma_market(fresh)["up_won"] is not None:
            rec.write("markets", fresh)
        else:
            missing += 1
    return missing


def diagnose(src, taus=(60, 30), sample_chars=700):
    """Print what the recorder actually received: event types, one raw example of
    each, and the replayed top of book at each decision time. For runs whose raw
    files cannot be downloaded, the job log is the only window into them."""
    counts, examples, first_seen = {}, {}, {}
    ladders, tops = {}, {}
    markets = {}
    for m in iter_jsonl(raw_files(src, "markets")):
        try:
            mk = pt.parse_gamma_market(m)
            markets[mk["slug"]] = mk
        except (ValueError, KeyError, TypeError):
            continue
    checks = sorted((mk["end"] - tau, mk["slug"], tau) for mk in markets.values() for tau in taus)
    ci = 0
    for rec in iter_jsonl(raw_files(src, "clob")):
        ms, msg = int(rec["recv_ms"]), rec.get("msg")
        while ci < len(checks) and checks[ci][0] * 1000 <= ms:
            _, slug, tau = checks[ci]
            mk = markets[slug]
            tops[(slug, tau)] = {side: (ladders[t].bid, ladders[t].ask, len(ladders[t].bids), len(ladders[t].asks))
                                 if t in ladders else None
                                 for side, t in (("up", mk["up_token"]), ("down", mk["down_token"]))}
            ci += 1
        for m in msg if isinstance(msg, list) else [msg]:
            et = m.get("event_type", "?") if isinstance(m, dict) else type(m).__name__
            counts[et] = counts.get(et, 0) + 1
            examples.setdefault(et, json.dumps(m)[:sample_chars])
            if isinstance(m, dict) and m.get("asset_id"):
                first_seen.setdefault(m["asset_id"], (ms, et))
        pt.apply_clob_message(ladders, msg, ms)
    print("event types:", json.dumps(counts))
    for et, ex in examples.items():
        print(f"example {et}: {ex}")
    for slug, mk in sorted(markets.items()):
        seen = {side: first_seen.get(tok) for side, tok in (("up", mk["up_token"]), ("down", mk["down_token"]))}
        print(f"{slug} first message per token: {seen} resolved={mk['up_won'] is not None}")
        for tau in taus:
            print(f"  tau={tau}: {tops.get((slug, tau))}")
    rtds = list(iter_jsonl(raw_files(src, "rtds")))[:3]
    for r in rtds:
        print("rtds example:", json.dumps(r)[:sample_chars])


def _top(x):
    return None if not np.isfinite(x) else round(float(x), 4)


def _levels(d):
    return [{"price": f"{p:g}", "size": f"{s:g}"} for p, s in sorted(d.items())]


def book_events(records):
    """Yield ("bba" | "book" | "trade", row) from recorded CLOB messages in arrival order."""
    ladders, last_top, pending = {}, {}, {}
    for rec in records:
        ms, msg = int(rec["recv_ms"]), rec.get("msg")
        touched = []
        for m in msg if isinstance(msg, list) else [msg]:
            if not isinstance(m, dict):
                continue
            et = m.get("event_type")
            if et == "last_trade_price":
                yield "trade", {"asset_id": m["asset_id"], "recv_ms": ms,
                                "event_ts_ms": int(m.get("timestamp") or ms), "payload": m}
            elif et == "book":
                touched.append(m["asset_id"])
            elif et == "price_change":
                touched += [pc["asset_id"] for pc in m.get("price_changes", [])]
        pt.apply_clob_message(ladders, msg, ms)
        for tok in dict.fromkeys(touched):
            lad = ladders[tok]
            top = (_top(lad.bid), _top(lad.ask))
            if top != last_top.get(tok):
                last_top[tok] = top
                yield "bba", {"asset_id": tok, "recv_ms": ms, "event_ts_ms": ms, "payload": {
                    "best_bid": "" if top[0] is None else f"{top[0]:g}",
                    "best_ask": "" if top[1] is None else f"{top[1]:g}"}}
            if tok in pending and pending[tok]["recv_ms"] // 1000 != ms // 1000:
                yield "book", pending.pop(tok)
            pending[tok] = {"asset_id": tok, "recv_ms": ms,
                            "payload": {"bids": _levels(lad.bids), "asks": _levels(lad.asks)}}
    yield from (("book", row) for row in pending.values())


def build(src, out):
    """Write the bundle under `out`; returns counts per file for the log."""
    src, out = Path(src), Path(out)
    days = sorted({p.parent.name for p in (src / "raw").glob("*/*.jsonl")})
    if not days:
        raise SystemExit(f"no recordings under {src}/raw")
    label = days[0] if len(days) == 1 else f"{days[0]}_{days[-1]}"
    poly = out / "data/polymarket/daily"
    paths = {
        "prices": out / f"data/chainlink/daily/prices/BTCUSD/BTCUSD-prices-{label}.csv.gz",
        "markets": poly / f"markets/BTC-5m/BTC-5m-markets-{label}.jsonl.gz",
        "bba": poly / f"best_bid_ask/BTC-5m/BTC-5m-best_bid_ask-{label}.jsonl.gz",
        "book": poly / f"book/BTC-5m/BTC-5m-book-{label}.jsonl.gz",
        "trade": poly / f"last_trade_price/BTC-5m/BTC-5m-last_trade_price-{label}.jsonl.gz",
    }
    for path in paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        for old in path.parent.glob("*.gz"):  # one file per kind, or the loaders refuse
            old.unlink()

    spot = {}
    for feed_ms, value, recv_ms in chainlink_ticks(iter_jsonl(raw_files(src, "rtds"))):
        spot.setdefault(feed_ms // 1000, (feed_ms, value, recv_ms))  # first report of each second
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["feed_ts_ms", "value", "full_accuracy_value", "server_ts_ms", "recv_ms"])
    for sec in sorted(spot):
        feed_ms, value, recv_ms = spot[sec]
        w.writerow([feed_ms, repr(value), int(round(value * 1e6)) * 10**12, recv_ms, recv_ms])
    with gzip.open(paths["prices"], "wt") as f:
        f.write(buf.getvalue())

    markets = market_rows(iter_jsonl(raw_files(src, "markets")), {s: v[1] for s, v in spot.items()})
    with gzip.open(paths["markets"], "wt") as f:
        f.writelines(json.dumps(m) + "\n" for m in markets)

    counts = {"prices": len(spot), "markets": len(markets),
              "resolved": sum(m["resolved"] for m in markets),
              "with_strike": sum(m["strike_value"] is not None for m in markets)}
    files = {k: gzip.open(paths[k], "wt") for k in ("bba", "book", "trade")}
    try:
        for kind, row in book_events(iter_jsonl(raw_files(src, "clob"))):
            files[kind].write(json.dumps(row, separators=(",", ":")) + "\n")
            counts[kind] = counts.get(kind, 0) + 1
    finally:
        for f in files.values():
            f.close()
    return counts


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", help="paper_trader.py live 的 --out 目录")
    ap.add_argument("--out", help="输出目录（默认 <src>/bundle）")
    ap.add_argument("--fetch-outcomes", action="store_true", help="先向 Gamma 补取尚未结算市场的结果")
    ap.add_argument("--diagnose", action="store_true", help="只打印收到的消息类型、样例和决策时点的盘口，不转换")
    args = ap.parse_args(argv)
    if args.diagnose:
        diagnose(args.src)
        return
    if args.fetch_outcomes:
        print(f"unresolved after fetch: {fetch_outcomes(args.src)}")
    counts = build(args.src, args.out or Path(args.src) / "bundle")
    print(" ".join(f"{k}={v}" for k, v in counts.items()))


if __name__ == "__main__":
    main()
