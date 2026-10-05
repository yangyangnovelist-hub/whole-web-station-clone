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
  <coin>usdt price (RTDS crypto_prices) with its own timestamp, and the Coinbase <COIN>-USD
  trade prints with their exchange time (coinbase_trades.jsonl.gz).
- for BTC, under strict/, receipt-ordered Binance trade/bookTicker events, direct Up and Down
  CLOB snapshots/deltas, and explicit connection epochs for lossless sub-second replay.

All recorded days go into one bundle, so the forward sample keeps growing.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import heapq
import io
import json
import math
import os
import time
from datetime import datetime
from pathlib import Path

import numpy as np

import binary as bo
import paper_trader as pt
from real_day import FIXED_POINT


STRICT_SCHEMA = "polymarket-5m-strict-replay-v2"


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


def iter_jsonl_strict(paths):
    """Read a strict evidence input without hiding a truncated gzip or JSON line."""
    for path in paths:
        opener = gzip.open if str(path).endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}:{line_number}: invalid JSON") from exc
                if not isinstance(row, dict):
                    raise ValueError(f"{path}:{line_number}: expected JSON object")
                yield row


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
    exchange time of the message that made the change (one row per message, not per websocket
    frame; messages without a time are left out of them)."""
    ladders, last_top, pending, last_x = {}, {}, {}, {}
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
                continue
            if et == "book":
                toks = [m["asset_id"]]
            elif et == "price_change":
                toks = list(dict.fromkeys(pc["asset_id"] for pc in m.get("price_changes", [])))
            else:
                continue
            pt.apply_clob_message(ladders, m, ms)
            touched += toks
            stamp = m.get("timestamp")
            for tok in toks:
                lad = ladders[tok]
                x = (lad.bid, lad.ask, lad.size_at("bid", lad.bid), lad.size_at("ask", lad.ask))
                if stamp and x != last_x.get(tok):
                    last_x[tok] = x
                    yield "xtop", {"asset_id": tok, "ts": int(stamp) / 1000, "recv_ms": ms, "top": x}
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


def binance_ws_ticks(records, symbol="BTCUSDT"):
    """(exchange time s, price, recv_ms) of one symbol's recorded Binance aggregated trades."""
    for rec in records:
        if rec.get("s") != symbol or rec.get("T") is None or rec.get("p") is None:
            continue
        try:
            yield float(rec["T"]) / 1000, float(rec["p"]), int(rec["recv_ms"])
        except (ValueError, TypeError):
            continue


def coinbase_ticks(records, product="BTC-USD"):
    """(exchange time s, price, recv_ms) of one product's recorded Coinbase trade prints."""
    from datetime import datetime
    for rec in records:
        if rec.get("p") != product or rec.get("t") is None or rec.get("px") is None:
            continue
        try:
            ts = datetime.fromisoformat(str(rec["t"]).replace("Z", "+00:00")).timestamp()
            yield ts, float(rec["px"]), int(rec["recv_ms"])
        except (ValueError, TypeError):
            continue


def _num(x):
    return "" if x is None or not np.isfinite(x) else f"{x:g}"


def clob_close_times(rows):
    """Recorder-clock seconds of each CLOB disconnect in an errors log: every "clob" row (the socket
    closed or failed), plus every "clob-open" (a reconnect) with no "clob" row since the previous
    one, because a clean close (codes 1000/1001) ends the read loop without raising, and recorders
    before 2026-09-30 17:30 UTC logged no row for it."""
    ev = sorted(((float(e["at"]) / 1000, e["where"]) for e in rows
                 if isinstance(e, dict) and e.get("at") is not None and e.get("where") in ("clob", "clob-open")),
                key=lambda x: x[0])  # stable: rows of the same millisecond keep the log's order
    out, is_open = [], False
    for t, where in ev:
        if where == "clob":
            out.append(t)
            is_open = False
        else:
            if is_open:
                out.append(t)
            is_open = True
    return out


def _strict_market_tables(markets):
    registry, outcomes, token_market, market_tokens = [], [], {}, {}
    for row in markets:
        raw = row["raw"]
        try:
            market = pt.parse_gamma_market(raw)
        except (KeyError, TypeError, ValueError):
            continue
        market_id = str(raw.get("conditionId") or market["slug"])
        recorded_at = raw.get("_recorded_at_ms")
        recorded_at = float(recorded_at) / 1000 if recorded_at is not None else None
        registry.append({"market_id": market_id, "start_ts": market["start"],
                         "up_token_id": market["up_token"], "down_token_id": market["down_token"],
                         "updated_at": recorded_at, "fee_rate": market.get("fee_rate")})
        token_market[market["up_token"]] = market_id
        token_market[market["down_token"]] = market_id
        market_tokens[market_id] = (market["up_token"], market["down_token"])
        if market["up_won"] is not None:
            source_time = raw.get("closedTime")
            try:
                source_time = datetime.fromisoformat(str(source_time).replace("Z", "+00:00")).timestamp()
            except (TypeError, ValueError):
                source_time = None
            outcomes.append({"market_id": market_id, "winner": "Up" if market["up_won"] else "Down",
                             "resolution_ts": source_time, "source": "gamma", "recorded_at": recorded_at})
    return registry, outcomes, token_market, market_tokens


def _strict_spot_events(records, symbol):
    sequence = 0
    for row in records:
        if str(row.get("s", "")).upper() != symbol:
            continue
        recv_ms = row.get("recv_ms")
        if recv_ms is None:
            raise ValueError(f"{symbol} strict spot row is missing recv_ms")
        if row.get("kind") == "bookTicker" or (row.get("b") is not None and row.get("a") is not None
                                                 and row.get("p") is None):
            try:
                event = {"kind": "spot_bbo", "recv_ms": float(recv_ms), "seq": sequence,
                         "source_ts_ms": None if row.get("E") is None else float(row["E"]),
                         "bid": float(row["b"]), "ask": float(row["a"])}
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"malformed {symbol} bookTicker row") from exc
        elif row.get("T") is not None and row.get("p") is not None:
            try:
                event = {"kind": "spot_trade", "recv_ms": float(recv_ms), "seq": sequence,
                         "source_ts_ms": float(row["T"]), "price": float(row["p"]),
                         "size": float(row.get("q") or 0)}
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"malformed {symbol} trade row") from exc
        else:
            raise ValueError(f"unknown {symbol} strict spot row")
        yield event
        sequence += 1


def _source_stats():
    return {"explicit_order": True, "sequence_regressions": 0, "receive_time_regressions": 0,
            "epoch_mismatches": 0, "dropped_outside_epoch": 0, "open_epoch_at_eof": None,
            "max_active_connections": 0}


def _strict_binance_stream(records, source, symbol, stats):
    """Normalize one independently ordered price-source socket without loading it into memory."""
    active_epoch = None
    last_epoch = 0
    previous_sequence = -1
    previous_receive_ms = -float("inf")
    for row in records:
        if str(row.get("s", symbol)).upper() != symbol and row.get("kind") not in ("connection", "disconnect"):
            continue
        try:
            receive_ms = float(row["recv_ms"])
            sequence = int(row["sequence"])
            epoch = int(row["connection_epoch"])
        except (KeyError, TypeError, ValueError) as exc:
            stats["explicit_order"] = False
            raise ValueError(f"{source} strict source row is missing causal metadata") from exc
        if sequence != previous_sequence + 1:
            stats["sequence_regressions"] += 1
        if receive_ms < previous_receive_ms:
            stats["receive_time_regressions"] += 1
        previous_sequence, previous_receive_ms = sequence, receive_ms
        stats["max_active_connections"] = max(stats["max_active_connections"],
                                                  int(row.get("max_active_connections") or 0))
        kind = row.get("kind")
        event = {"recv_ms": receive_ms, "source_ts_ms": None, "connection_epoch": epoch,
                 "stream_sequence": sequence, "stream": source}
        if kind == "connection":
            if active_epoch is not None or epoch <= last_epoch:
                stats["epoch_mismatches"] += 1
                continue
            active_epoch = last_epoch = epoch
            event["kind"] = f"{source}_connection"
        elif kind == "disconnect":
            if active_epoch != epoch:
                stats["epoch_mismatches"] += 1
                continue
            event.update(kind=f"{source}_disconnect", error=row.get("error"))
            active_epoch = None
        else:
            if active_epoch != epoch:
                stats["dropped_outside_epoch"] += 1
                continue
            try:
                bid, ask = float(row.get("b")), float(row.get("a"))
            except (TypeError, ValueError):
                bid = ask = math.nan
            if source == "spot" and kind == "trade":
                try:
                    event.update(kind="spot_trade", source_ts_ms=float(row["T"]),
                                 price=float(row["p"]), size=float(row.get("q") or 0))
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError("malformed strict spot trade") from exc
            elif source == "spot" and kind == "bookTicker" and 0 < bid < ask:
                event.update(kind="spot_bbo", bid=bid, ask=ask)
            elif source == "futures" and kind == "bookTicker" and 0 < bid < ask:
                try:
                    event.update(kind="futures_bbo", source_ts_ms=float(row["T"]),
                                 event_ts_ms=float(row["E"]), bid=bid, ask=ask)
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError("malformed strict futures bookTicker") from exc
            elif source == "futures_trade" and kind == "trade":
                try:
                    event.update(kind="futures_trade", source_ts_ms=float(row["T"]),
                                 event_ts_ms=float(row["E"]), price=float(row["p"]),
                                 size=float(row.get("q") or 0))
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError("malformed strict futures trade") from exc
            elif source == "deribit" and kind == "quote":
                try:
                    bid, ask = float(row["bid"]), float(row["ask"])
                    event.update(kind="deribit_quote", source_ts_ms=float(row["timestamp"]),
                                 bid=bid, ask=ask,
                                 bid_size=float(row.get("bid_size") or 0),
                                 ask_size=float(row.get("ask_size") or 0))
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError("malformed strict Deribit quote") from exc
                if not 0 < bid < ask:
                    raise ValueError("crossed strict Deribit quote")
            elif source == "deribit" and kind == "dvol":
                try:
                    volatility = float(row["volatility"])
                    event.update(kind="deribit_dvol", source_ts_ms=float(row["timestamp"]),
                                 volatility=volatility, index_name=str(row["index_name"]))
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError("malformed strict Deribit DVOL") from exc
                if not math.isfinite(volatility) or volatility <= 0 or event["index_name"] != "btc_usd":
                    raise ValueError("invalid strict Deribit DVOL")
            else:
                raise ValueError(f"unknown {source} strict source row")
        source_rank = {"spot": 0, "futures": 1, "futures_trade": 2, "deribit": 3}[source]
        yield (receive_ms, source_rank, sequence), event
    stats["open_epoch_at_eof"] = active_epoch


def _strict_source_events(src, symbol):
    """Merge all timestamped trigger feeds by actual local receipt time."""
    inputs = (
        ("spot", "binance-strict"),
        ("futures", "binance-futures-strict"),
        ("futures_trade", "binance-futures-trade-strict"),
        ("deribit", "deribit-strict"),
    )
    stats = {source: _source_stats() for source, _ in inputs}
    streams = [
        _strict_binance_stream(iter_jsonl_strict(raw_files(src, raw_name)),
                               source, symbol, stats[source])
        for source, raw_name in inputs
    ]
    for sequence, (_, event) in enumerate(heapq.merge(*streams, key=lambda item: item[0])):
        event["seq"] = sequence
        yield event, stats


def _strict_clob_inputs(src, coin):
    """Merge CLOB frames and connection markers without loading the L2 stream into memory."""
    def books():
        for ordinal, row in enumerate(iter_jsonl_strict(clob_files(src, coin))):
            if row.get("recv_ms") is None:
                raise ValueError(f"strict {coin} CLOB row is missing recv_ms")
            sequence = row.get("sequence")
            key = (0, int(sequence), 1) if sequence is not None else (1, float(row["recv_ms"]), 1, ordinal)
            yield key, "message", row

    def markers():
        ordinal = 0
        for row in iter_jsonl_strict(raw_files(src, "errors")):
            if row.get("where") not in ("clob-open", "clob"):
                continue
            if row.get("at") is None:
                raise ValueError(f"strict {coin} CLOB marker is missing at")
            if row.get("where") == "clob" and row.get("connection_active") is False:
                continue  # a failed connect has no book epoch to close
            sequence = row.get("sequence")
            rank = 0 if row["where"] == "clob-open" else 2
            key = (0, int(sequence), rank) if sequence is not None else (1, float(row["at"]), rank, ordinal)
            yield key, "marker", row
            ordinal += 1

    yield from heapq.merge(books(), markers(), key=lambda item: item[0])


def _strict_clob_events(src, coin, token_market):
    sequence = 0
    active_epoch = None
    last_epoch = 0
    stats = {"explicit_order": True, "source_sequence_regressions": 0,
             "source_time_regressions": 0,
             "missing_source_ts": 0, "dropped_pre_epoch": 0,
             "dropped_epoch_mismatch": 0, "receive_time_regressions": 0, "open_epoch_at_eof": None}
    snapshots = set()
    last_receive_ms = -float("inf")
    last_source_sequence = -1
    last_source_ms = -float("inf")
    for _, source, row in _strict_clob_inputs(src, coin):
        receive_ms = float(row["at"] if source == "marker" else row["recv_ms"])
        if receive_ms < last_receive_ms:
            stats["receive_time_regressions"] += 1
        last_receive_ms = receive_ms
        explicit = row.get("sequence") is not None and row.get("connection_epoch") is not None
        stats["explicit_order"] = stats["explicit_order"] and explicit
        if row.get("sequence") is not None:
            source_sequence = int(row["sequence"])
            if source_sequence <= last_source_sequence:
                stats["source_sequence_regressions"] += 1
            last_source_sequence = source_sequence
        if source == "marker":
            if row["where"] == "clob-open":
                epoch = int(row.get("connection_epoch") or last_epoch + 1)
                if epoch <= last_epoch or active_epoch is not None:
                    stats["dropped_epoch_mismatch"] += 1
                    continue
                active_epoch = last_epoch = epoch
                yield {"kind": "clob_connection", "recv_ms": float(row["at"]), "seq": sequence,
                       "source_ts_ms": None, "connection_epoch": epoch,
                       "token_count": row.get("tokens")}, stats, snapshots
                sequence += 1
            else:
                epoch = int(row.get("connection_epoch") or active_epoch or 0)
                if epoch <= 0 or active_epoch != epoch:
                    stats["dropped_epoch_mismatch"] += 1
                    continue
                yield {"kind": "clob_error", "recv_ms": float(row["at"]), "seq": sequence,
                       "source_ts_ms": None, "connection_epoch": epoch, "error": row.get("err")}, stats, snapshots
                sequence += 1
                active_epoch = None
            continue

        epoch = int(row.get("connection_epoch") or active_epoch or 0)
        if active_epoch is None or epoch <= 0:
            stats["dropped_pre_epoch"] += 1
            continue
        if epoch != active_epoch:
            stats["dropped_epoch_mismatch"] += 1
            continue
        messages = row.get("msg")
        for message in messages if isinstance(messages, list) else [messages]:
            if not isinstance(message, dict):
                continue
            source_ts = message.get("timestamp")
            try:
                source_ts = float(source_ts)
            except (TypeError, ValueError):
                source_ts = None
            event_type = message.get("event_type")
            if event_type == "book":
                token = str(message.get("asset_id", ""))
                if token not in token_market:
                    continue
                if source_ts is None:
                    stats["missing_source_ts"] += 1
                    continue
                if source_ts < last_source_ms:
                    stats["source_time_regressions"] += 1
                last_source_ms = max(last_source_ms, source_ts)
                event = {"kind": "clob_snapshot", "recv_ms": float(row["recv_ms"]), "seq": sequence,
                         "source_ts_ms": source_ts, "connection_epoch": epoch,
                         "market_id": str(message.get("market") or token_market[token]), "asset_id": token,
                         "bids": message.get("bids") if "bids" in message else message.get("buys") or [],
                         "asks": message.get("asks") if "asks" in message else message.get("sells") or []}
                if "tick_size" in message:
                    event["tick_size"] = message["tick_size"]
                snapshots.add((epoch, token))
                yield event, stats, snapshots
                sequence += 1
            elif event_type == "price_change":
                if source_ts is None:
                    stats["missing_source_ts"] += 1
                    continue
                if source_ts < last_source_ms:
                    stats["source_time_regressions"] += 1
                last_source_ms = max(last_source_ms, source_ts)
                for change in message.get("price_changes") or []:
                    token = str(change.get("asset_id", ""))
                    if token not in token_market:
                        continue
                    event = {"kind": "clob_price_change", "recv_ms": float(row["recv_ms"]),
                             "seq": sequence, "source_ts_ms": source_ts, "connection_epoch": epoch,
                             "market_id": str(message.get("market") or token_market[token]),
                             "asset_id": token, "price": change.get("price"), "size": change.get("size"),
                             "side": change.get("side")}
                    for key in ("best_bid", "best_ask"):
                        if key in change:
                            event[key] = change[key]
                    yield event, stats, snapshots
                    sequence += 1
    stats["open_epoch_at_eof"] = active_epoch


def write_strict_bundle(src, out, coin, markets):
    """Write the lossless inputs required for receipt-ordered G/H/I/J replay."""
    strict = Path(out) / "strict"
    strict.mkdir(parents=True, exist_ok=True)
    names = ("manifest.json", "source_events.jsonl.gz", "spot_events.jsonl.gz", "clob_events.jsonl.gz",
             "market_registry.csv.gz", "market_outcomes.csv.gz")
    for name in names:
        path = strict / name
        if path.exists():
            path.unlink()

    saw_market = False
    for raw_market in iter_jsonl_strict(raw_files(src, "markets")):
        if str(raw_market.get("slug", "")).startswith(f"{coin}-updown-5m-"):
            pt.parse_gamma_market(raw_market)
            saw_market = True
    if not saw_market:
        raise ValueError(f"strict {coin} recording has no valid market metadata")
    registry, outcomes, token_market, market_tokens = _strict_market_tables(markets)
    with gzip.open(strict / "market_registry.csv.gz", "wt", newline="") as stream:
        fields = ("market_id", "start_ts", "up_token_id", "down_token_id", "updated_at", "fee_rate")
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(registry)
    with gzip.open(strict / "market_outcomes.csv.gz", "wt", newline="") as stream:
        fields = ("market_id", "winner", "resolution_ts", "source", "recorded_at")
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(outcomes)

    counts = {"markets": len(registry), "outcomes": len(outcomes),
              "spot_connection": 0, "spot_trade": 0, "spot_bbo": 0, "spot_disconnect": 0,
              "futures_connection": 0, "futures_bbo": 0, "futures_disconnect": 0,
              "futures_trade_connection": 0, "futures_trade": 0, "futures_trade_disconnect": 0,
              "deribit_connection": 0, "deribit_quote": 0, "deribit_dvol": 0,
              "deribit_disconnect": 0,
              "clob_connection": 0, "clob_snapshot": 0, "clob_price_change": 0, "clob_error": 0}
    times = []
    source_stats = {source: _source_stats()
                    for source in ("spot", "futures", "futures_trade", "deribit")}
    with gzip.open(strict / "source_events.jsonl.gz", "wt", compresslevel=3) as stream:
        for event, source_stats in _strict_source_events(src, f"{coin.upper()}USDT"):
            stream.write(json.dumps(event, separators=(",", ":")) + "\n")
            counts[event["kind"]] += 1
            times.append(float(event["recv_ms"]))

    clob_stats = None
    snapshots = set()
    with gzip.open(strict / "clob_events.jsonl.gz", "wt", compresslevel=3) as stream:
        for event, clob_stats, snapshots in _strict_clob_events(src, coin, token_market):
            stream.write(json.dumps(event, separators=(",", ":")) + "\n")
            counts[event["kind"]] += 1
            times.append(float(event["recv_ms"]))
    clob_stats = clob_stats or {"explicit_order": False, "source_sequence_regressions": 0,
                                "source_time_regressions": 0,
                                "missing_source_ts": 0,
                                "dropped_pre_epoch": 0, "dropped_epoch_mismatch": 0,
                                "receive_time_regressions": 0, "open_epoch_at_eof": None}
    epochs = {epoch for epoch, _ in snapshots}
    paired_market_ids = {market_id for market_id, pair in market_tokens.items()
                         if any((epoch, pair[0]) in snapshots and (epoch, pair[1]) in snapshots for epoch in epochs)}
    resolved_market_ids = {row["market_id"] for row in outcomes}
    unresolved_market_ids = sorted(set(market_tokens) - resolved_market_ids)
    missing_resolved_books = sorted(resolved_market_ids - paired_market_ids)
    recorder_complete = any(row.get("where") == "recorder-complete"
                            for row in iter_jsonl_strict(raw_files(src, "errors")))
    source_stream_integrity = all(
        stats["explicit_order"] and stats["sequence_regressions"] == 0
        and stats["receive_time_regressions"] == 0 and stats["epoch_mismatches"] == 0
        and stats["dropped_outside_epoch"] == 0 and stats["open_epoch_at_eof"] is None
        for stats in source_stats.values()
    )
    completeness_checks = {
        "market_registry": bool(registry),
        "market_outcomes": bool(outcomes),
        "spot_connection": bool(counts["spot_connection"]),
        "spot_trade": bool(counts["spot_trade"]),
        "spot_disconnect": bool(counts["spot_disconnect"]),
        "futures_connection": bool(counts["futures_connection"]),
        "futures_bbo": bool(counts["futures_bbo"]),
        "futures_disconnect": bool(counts["futures_disconnect"]),
        "clob_connection": bool(counts["clob_connection"]),
        "both_token_snapshots": bool(paired_market_ids),
        "resolved_books_covered": not missing_resolved_books,
        "recorder_complete": recorder_complete,
        "source_stream_integrity": source_stream_integrity,
        "spot_race_width": (
            source_stats["spot"]["max_active_connections"] >= pt.SPOT_RACE_CONNECTIONS
        ),
        "futures_race_width": (
            source_stats["futures"]["max_active_connections"] >= pt.FUTURES_RACE_CONNECTIONS
        ),
        "clob_explicit_order": bool(clob_stats["explicit_order"]),
        "clob_source_sequence": clob_stats["source_sequence_regressions"] == 0,
        "clob_source_time": clob_stats["source_time_regressions"] == 0,
        "clob_source_timestamp": clob_stats["missing_source_ts"] == 0,
        "clob_pre_epoch": clob_stats["dropped_pre_epoch"] == 0,
        "clob_epoch_match": clob_stats["dropped_epoch_mismatch"] == 0,
        "clob_receive_time": clob_stats["receive_time_regressions"] == 0,
        "clob_closed_epoch": clob_stats["open_epoch_at_eof"] is None,
    }
    failure_reasons = sorted(key for key, passed in completeness_checks.items() if not passed)
    complete = not failure_reasons
    receipt_race_checks = {
        "strict_complete": complete,
        "futures_trade_connection": bool(counts["futures_trade_connection"]),
        "futures_trade": bool(counts["futures_trade"]),
        "futures_trade_disconnect": bool(counts["futures_trade_disconnect"]),
        "deribit_connection": bool(counts["deribit_connection"]),
        "deribit_quote": bool(counts["deribit_quote"]),
        "deribit_disconnect": bool(counts["deribit_disconnect"]),
        "futures_trade_race_width": (
            source_stats["futures_trade"]["max_active_connections"]
            >= len(pt.BINANCE_FUTURES_TRADE_WS)
        ),
        "deribit_race_width": source_stats["deribit"]["max_active_connections"] >= 1,
    }
    receipt_race_failure_reasons = sorted(
        key for key, passed in receipt_race_checks.items() if not passed
    )
    receipt_race_ready = not receipt_race_failure_reasons
    manifest = {"schema": STRICT_SCHEMA, "coin": coin, "run_id": os.environ.get("GITHUB_RUN_ID"),
                "collector_region": os.environ.get("POLYMARKET_COLLECTOR_REGION", "unknown"),
                "complete": complete, "receipt_race_ready": receipt_race_ready,
                "completeness_checks": completeness_checks,
                "failure_reasons": failure_reasons,
                "receipt_race_checks": receipt_race_checks,
                "receipt_race_failure_reasons": receipt_race_failure_reasons,
                "started_ms": min(times) if times else None,
                "ended_ms": max(times) if times else None, "counts": counts,
                "recorder_complete": recorder_complete, "source_integrity": source_stats,
                "markets_with_both_token_snapshots": len(paired_market_ids),
                "missing_resolved_market_ids": missing_resolved_books,
                "unresolved_market_ids": unresolved_market_ids, "clob_integrity": clob_stats}
    (strict / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"strict_ready": complete, "receipt_race_ready": receipt_race_ready,
            "strict_failure_reasons": failure_reasons,
            "receipt_race_failure_reasons": receipt_race_failure_reasons,
            **{f"strict_{key}": value for key, value in counts.items()}}


class LatencyFiles:
    """latency.py's input under <out>/latency: markets and winners, the Up token's exchange-stamped
    top of book (streamed in through `book`), the relayed Binance price, the Coinbase prints and the
    times the CLOB websocket closed (written on `close`).
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
            c = 0
            with gzip.open(self.d / "coinbase_trades.jsonl.gz", "wt") as f:
                for ts, value, recv_ms in coinbase_ticks(iter_jsonl(raw_files(src, "coinbase")), f"{coin.upper()}-USD"):
                    f.write(json.dumps({"event": "COINBASE_TRADE", "trade_ts": ts, "receive_ts": recv_ms / 1000,
                                        "price": value}) + "\n")
                    c += 1
            bw = 0
            with gzip.open(self.d / "binance_trades.jsonl.gz", "wt") as f:
                for ts, value, recv_ms in binance_ws_ticks(iter_jsonl(raw_files(src, "binance")), f"{coin.upper()}USDT"):
                    f.write(json.dumps({"event": "BINANCE_WS_TRADE", "trade_ts": ts, "receive_ts": recv_ms / 1000,
                                        "price": value}) + "\n")
                    bw += 1
            closes = clob_close_times(iter_jsonl(raw_files(src, "errors")))
            with gzip.open(self.d / "clob_closes.csv.gz", "wt", newline="") as f:
                w = csv.writer(f)
                w.writerow(["at_s"])
                w.writerows([f"{x:.3f}"] for x in sorted(closes))
            return {"latency_markets": len(self.up), "latency_books": self.n, "latency_binance": k,
                    "latency_coinbase": c, "latency_binance_ws": bw, "latency_clob_closes": len(closes)}
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
    if coin == "btc":  # G/H/I/J are BTC-only; do not duplicate raw L2 for the four side datasets.
        counts.update(write_strict_bundle(src, out, coin, markets))
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
