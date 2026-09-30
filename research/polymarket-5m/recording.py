"""Turn paper_trader recordings into an outcometick-style bundle.

    python recording.py paper_data --out paper_data/bundle
    python recording.py paper_data --coin eth           # -> paper_data/bundle-eth
    python recording.py paper_data --fetch-outcomes     # after a run stops: resolve what it missed
    python strategy_zoo.py paper_data/bundle --confirm --out paper/confirm.md

`paper_trader.py live` saves every websocket message it receives under
raw/<day>/ as gzipped JSONL: clob-<coin>, rtds, markets (older runs wrote a
single uncompressed clob.jsonl for BTC; both are read). This rebuilds, for one
coin at a time, the files real_day.py and strategy_zoo.py read, so every
strategy can be re-tested on data recorded after it was chosen:

- Chainlink <COIN>/USD from RTDS, one row per feed second;
- top of book and full-depth snapshots (one per token per second, the state
  after the last message received in that second), by replaying the CLOB
  messages through paper_trader.apply_clob_message;
- last_trade_price prints as received;
- markets with the official outcome from the latest Gamma snapshot. Gamma
  does not publish the price to beat, so the strike is the mean of the
  recorded Chainlink prices over the 60 seconds ending at the open, the same
  approximation that reproduced 265 of 266 outcomes on the sample day;
- under latency/, the files latency.py reads (as in the Dublin exports): the
  Up token's top of book and first-level sizes stamped with the exchange's own
  message time, markets and winners, and Polymarket's relay of the Binance
  <coin>usdt price (RTDS crypto_prices) with its own timestamp.

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
        opener = gzip.open if str(path).endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8") as f:
            try:
                for line in f:
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:  # a killed recorder can leave half a line
                        continue
            except (EOFError, OSError):  # ... or a gzip stream without its trailer
                continue


def raw_files(src, name):
    files = list(Path(src).glob(f"raw/*/{name}.jsonl")) + list(Path(src).glob(f"raw/*/{name}.jsonl.gz"))
    return sorted(files, key=lambda p: (p.parent.name, p.name))


def clob_files(src, coin="btc"):
    """Per-coin CLOB recordings; runs before the per-coin split wrote BTC to clob.jsonl."""
    return sorted(raw_files(src, f"clob-{coin}") + (raw_files(src, "clob") if coin == "btc" else []),
                  key=lambda p: (p.parent.name, p.name))


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


def market_rows(records, spot_by_sec, coin="btc"):
    """Latest Gamma snapshot per slug of one coin, in the outcometick markets layout."""
    latest = {}
    for m in records:
        if isinstance(m, dict) and str(m.get("slug", "")).startswith(f"{coin}-updown-5m-"):
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
        if isinstance(m, dict) and "-updown-5m-" in str(m.get("slug", "")):
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
    rec.close()
    return missing


def diagnose(src, taus=(60, 30), sample_chars=700, coin="btc", max_markets=12):
    """Print what the recorder actually received: event types, one raw example of
    each, and the replayed top of book at each decision time. For runs whose raw
    files cannot be downloaded, the job log is the only window into them."""
    counts, examples, first_seen = {}, {}, {}
    ladders, tops = {}, {}
    markets = {}
    for m in iter_jsonl(raw_files(src, "markets")):
        try:
            mk = pt.parse_gamma_market(m)
        except (ValueError, KeyError, TypeError):
            continue
        if mk["slug"].startswith(f"{coin}-"):
            markets[mk["slug"]] = mk
    checks = sorted((mk["end"] - tau, mk["slug"], tau) for mk in markets.values() for tau in taus)
    ci = 0
    for rec in iter_jsonl(clob_files(src, coin)):
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
    print(f"{coin}: {len(markets)} markets recorded; showing {min(max_markets, len(markets))}")
    for slug, mk in sorted(markets.items())[:max_markets]:
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
    """Yield ("bba" | "book" | "trade" | "xtop", row) from recorded CLOB messages in arrival order.
    "xtop" rows carry the top of book and first-level sizes whenever they change, stamped with the
    exchange's message time (messages without one are left out of them)."""
    ladders, last_top, pending, last_x = {}, {}, {}, {}
    for rec in records:
        ms, msg = int(rec["recv_ms"]), rec.get("msg")
        touched, stamp = [], {}
        for m in msg if isinstance(msg, list) else [msg]:
            if not isinstance(m, dict):
                continue
            et = m.get("event_type")
            if et == "last_trade_price":
                yield "trade", {"asset_id": m["asset_id"], "recv_ms": ms,
                                "event_ts_ms": int(m.get("timestamp") or ms), "payload": m}
            elif et == "book":
                touched.append(m["asset_id"])
                stamp[m["asset_id"]] = m.get("timestamp")
            elif et == "price_change":
                for pc in m.get("price_changes", []):
                    touched.append(pc["asset_id"])
                    stamp[pc["asset_id"]] = m.get("timestamp")
        pt.apply_clob_message(ladders, msg, ms)
        for tok in dict.fromkeys(touched):
            lad = ladders[tok]
            x = (lad.bid, lad.ask, lad.size_at("bid", lad.bid), lad.size_at("ask", lad.ask))
            if stamp.get(tok) and x != last_x.get(tok):
                last_x[tok] = x
                yield "xtop", {"asset_id": tok, "ts": int(stamp[tok]) / 1000, "recv_ms": ms, "top": x}
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


def binance_ticks(records, symbol="btcusdt"):
    """(ts, value, recv_ms) of Polymarket's relay of one Binance symbol (RTDS crypto_prices)."""
    for rec in records:
        msg = rec.get("msg") or {}
        if msg.get("topic") != "crypto_prices":
            continue
        payload = msg.get("payload") or {}
        if str(payload.get("symbol", "")).lower() != symbol:
            continue
        points = payload.get("data") if isinstance(payload.get("data"), list) else [payload]
        for pnt in points:
            if pnt.get("value") is None or pnt.get("timestamp") is None:
                continue
            ts = float(pnt["timestamp"])
            yield (ts / 1000 if ts > 10**11 else ts), float(pnt["value"]), int(rec["recv_ms"])


def _num(x):
    return "" if x is None or not np.isfinite(x) else f"{x:g}"


class LatencyFiles:
    """latency.py's input under <out>/latency: markets and winners, the Up token's exchange-stamped
    top of book (streamed in through `book`) and the relayed Binance price (written on `close`).
    Any failure only disables these files; the bundle itself is unaffected."""

    def __init__(self, out, markets):
        self.d, self.error, self.n = Path(out) / "latency", None, 0
        try:
            self.d.mkdir(parents=True, exist_ok=True)
            self.up, reg, won = {}, [], []
            for m in markets:
                try:
                    mk = pt.parse_gamma_market(m["raw"])
                except (ValueError, KeyError):
                    continue
                self.up[mk["up_token"]] = m["slug"]
                reg.append((m["slug"], mk["start"]))
                if mk["up_won"] is not None:
                    won.append((m["slug"], "Up" if mk["up_won"] else "Down"))
            for name, header, rows in (("market_registry", ("market_id", "start_ts"), reg),
                                       ("market_outcomes", ("market_id", "winner"), won)):
                with gzip.open(self.d / f"runtime_1000.{name}.csv.gz", "wt", newline="") as f:
                    w = csv.writer(f)
                    w.writerow(header)
                    w.writerows(rows)
            self.f = gzip.open(self.d / "runtime_1000.poly_probability_observations_v1.csv.gz", "wt", newline="")
            self.w = csv.writer(self.f)
            self.w.writerow(["market_id", "source_ts", "receive_ts", "best_bid", "best_ask", "bids_json", "asks_json"])
        except Exception as e:
            self.error = repr(e)

    def book(self, row):
        slug = None if self.error else self.up.get(row["asset_id"])
        if slug is None:
            return
        try:
            bid, ask, bid_size, ask_size = row["top"]
            self.w.writerow([slug, f"{row['ts']:.3f}", f"{row['recv_ms'] / 1000:.3f}", _num(bid), _num(ask),
                             f"[[{_num(bid)}, {_num(bid_size)}]]" if np.isfinite(bid) else "[]",
                             f"[[{_num(ask)}, {_num(ask_size)}]]" if np.isfinite(ask) else "[]"])
            self.n += 1
        except Exception as e:
            self.error = repr(e)

    def close(self, src, coin):
        if self.error:
            return {"latency_error": self.error}
        try:
            self.f.close()
            k = 0
            with gzip.open(self.d / "shadow_current.jsonl.gz", "wt") as f:
                for ts, value, recv_ms in binance_ticks(iter_jsonl(raw_files(src, "rtds-binance")), f"{coin}usdt"):
                    f.write(json.dumps({"event": "BINANCE_AGG_TRADE", "trade_ts": ts, "receive_ts": recv_ms / 1000,
                                        "price": value}) + "\n")
                    k += 1
            return {"latency_markets": len(self.up), "latency_books": self.n, "latency_binance": k}
        except Exception as e:
            return {"latency_error": repr(e)}


def build(src, out, coin="btc"):
    """Write one coin's bundle under `out`; returns counts per file for the log."""
    src, out = Path(src), Path(out)
    days = sorted({p.parent.name for p in (src / "raw").glob("*/*.jsonl*")})
    if not days:
        raise SystemExit(f"no recordings under {src}/raw")
    label = days[0] if len(days) == 1 else f"{days[0]}_{days[-1]}"
    poly, c = out / "data/polymarket/daily", coin.upper()
    paths = {
        "prices": out / f"data/chainlink/daily/prices/{c}USD/{c}USD-prices-{label}.csv.gz",
        "markets": poly / f"markets/{c}-5m/{c}-5m-markets-{label}.jsonl.gz",
        "bba": poly / f"best_bid_ask/{c}-5m/{c}-5m-best_bid_ask-{label}.jsonl.gz",
        "book": poly / f"book/{c}-5m/{c}-5m-book-{label}.jsonl.gz",
        "trade": poly / f"last_trade_price/{c}-5m/{c}-5m-last_trade_price-{label}.jsonl.gz",
    }
    for path in paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        for old in path.parent.glob("*.gz"):  # one file per kind, or the loaders refuse
            old.unlink()

    spot = {}
    for feed_ms, value, recv_ms in chainlink_ticks(iter_jsonl(raw_files(src, "rtds")), f"{coin}/usd"):
        spot.setdefault(feed_ms // 1000, (feed_ms, value, recv_ms))  # first report of each second
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["feed_ts_ms", "value", "full_accuracy_value", "server_ts_ms", "recv_ms"])
    for sec in sorted(spot):
        feed_ms, value, recv_ms = spot[sec]
        w.writerow([feed_ms, repr(value), int(round(value * 1e6)) * 10**12, recv_ms, recv_ms])
    with gzip.open(paths["prices"], "wt") as f:
        f.write(buf.getvalue())

    markets = market_rows(iter_jsonl(raw_files(src, "markets")), {s: v[1] for s, v in spot.items()}, coin)
    with gzip.open(paths["markets"], "wt") as f:
        f.writelines(json.dumps(m) + "\n" for m in markets)

    counts = {"prices": len(spot), "markets": len(markets),
              "resolved": sum(m["resolved"] for m in markets),
              "with_strike": sum(m["strike_value"] is not None for m in markets)}
    files = {k: gzip.open(paths[k], "wt") for k in ("bba", "book", "trade")}
    lat = LatencyFiles(out, markets)
    try:
        for kind, row in book_events(iter_jsonl(clob_files(src, coin))):
            if kind == "xtop":
                lat.book(row)
                continue
            files[kind].write(json.dumps(row, separators=(",", ":")) + "\n")
            counts[kind] = counts.get(kind, 0) + 1
    finally:
        for f in files.values():
            f.close()
    counts.update(lat.close(src, coin))
    return counts


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", help="paper_trader.py live 的 --out 目录")
    ap.add_argument("--out", help="输出目录（默认 BTC 为 <src>/bundle，其他币种为 <src>/bundle-<coin>）")
    ap.add_argument("--coin", default="btc")
    ap.add_argument("--fetch-outcomes", action="store_true", help="先向 Gamma 补取尚未结算市场的结果")
    ap.add_argument("--diagnose", action="store_true", help="只打印收到的消息类型、样例和决策时点的盘口，不转换")
    args = ap.parse_args(argv)
    if args.diagnose:
        diagnose(args.src, coin=args.coin)
        return
    if args.fetch_outcomes:
        print(f"unresolved after fetch: {fetch_outcomes(args.src)}")
    default = Path(args.src) / ("bundle" if args.coin == "btc" else f"bundle-{args.coin}")
    counts = build(args.src, args.out or default, args.coin)
    print(" ".join(f"{k}={v}" for k, v in counts.items()))


if __name__ == "__main__":
    main()
