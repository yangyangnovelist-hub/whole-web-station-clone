"""Convert the eu-west data-only recorder into one strict receipt-causal replay artifact."""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import heapq
import json
import math
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

import pm_outcomes


SCHEMA = "polymarket-5m-strict-replay-v2"
SOURCE_ROUTES = {
    "spot": ("bn_spot", "bn_spot_2", "bn_spot_3"),
    "futures": ("bn_fut_pub", "bn_fut_pub_2"),
    "futures_trade": ("bn_fut_trade", "bn_fut_trade_2"),
    "deribit": ("deribit",),
}
SOURCE_RANK = {name: rank for rank, name in enumerate(SOURCE_ROUTES)}


def _loads(value: str | bytes) -> Any:
    return json.loads(value)


def _clock_ms(value: object) -> float | None:
    if value is None:
        return None
    result = float(value)
    if result > 1e17:
        result /= 1_000_000.0
    elif result > 1e14:
        result /= 1_000.0
    return result


def _iter_route(paths: Iterable[Path], route: str) -> Iterator[tuple[int, str, dict[str, Any]]]:
    previous = -1
    for path in sorted(paths):
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                stamp, separator, raw = line.rstrip("\n").partition("\t")
                if not separator:
                    raise ValueError(f"{path.name}:{line_number}: missing receive timestamp")
                receive_ns = int(stamp)
                if receive_ns < previous:
                    raise ValueError(f"{path.name}:{line_number}: receive clock regressed")
                previous = receive_ns
                payload = _loads(raw)
                if not isinstance(payload, dict):
                    raise ValueError(f"{path.name}:{line_number}: frame is not an object")
                yield receive_ns, route, payload


def _raw_paths(root: Path, route: str, day: str) -> list[Path]:
    return sorted(root.glob(f"{route}.{day}T??.txt.gz"))


def _frame_event(role: str, payload: Mapping[str, Any]) -> dict[str, Any] | None:
    data = payload.get("data", payload)
    if not isinstance(data, Mapping):
        return None
    stream = str(payload.get("stream", ""))
    event_name = str(data.get("e", ""))
    if role == "spot":
        if event_name != "trade" and not stream.endswith("@trade"):
            return None
        return {
            "kind": "spot_trade", "source_ts_ms": _clock_ms(data.get("T")),
            "price": float(data["p"]), "size": float(data.get("q") or 0),
            "dedupe": (data.get("t"), data.get("T"), data.get("p"), data.get("q")),
        }
    if role == "futures":
        if event_name != "bookTicker" and "bookTicker" not in stream:
            return None
        bid, ask = float(data["b"]), float(data["a"])
        if not 0 < bid < ask:
            return None
        return {
            "kind": "futures_bbo", "source_ts_ms": _clock_ms(data.get("T") or data.get("E")),
            "event_ts_ms": _clock_ms(data.get("E")), "bid": bid, "ask": ask,
            "dedupe": (data.get("u"), data.get("T"), data.get("b"), data.get("a")),
        }
    if role == "futures_trade":
        if event_name != "trade" and not stream.endswith("@trade"):
            return None
        return {
            "kind": "futures_trade", "source_ts_ms": _clock_ms(data.get("T")),
            "event_ts_ms": _clock_ms(data.get("E")), "price": float(data["p"]),
            "size": float(data.get("q") or 0),
            "dedupe": (data.get("t"), data.get("T"), data.get("p"), data.get("q")),
        }
    if role == "deribit":
        params = payload.get("params")
        if not isinstance(params, Mapping) or params.get("channel") != "quote.BTC-PERPETUAL":
            return None
        quote = params.get("data")
        if not isinstance(quote, Mapping):
            return None
        bid, ask = float(quote["best_bid_price"]), float(quote["best_ask_price"])
        if not 0 < bid < ask:
            return None
        return {
            "kind": "deribit_quote", "source_ts_ms": _clock_ms(quote.get("timestamp")),
            "bid": bid, "ask": ask, "bid_size": float(quote.get("best_bid_amount") or 0),
            "ask_size": float(quote.get("best_ask_amount") or 0),
            "dedupe": (quote.get("timestamp"), quote.get("best_bid_price"),
                       quote.get("best_ask_price")),
        }
    return None


def _iter_source_role(
    root: Path,
    day: str,
    role: str,
    segment_end_ms: float,
    stats: dict[str, Any],
) -> Iterator[tuple[float, int, int, dict[str, Any]]]:
    routes = SOURCE_ROUTES[role]
    route_streams = [
        _iter_route(_raw_paths(root, route, day), route)
        for route in routes if _raw_paths(root, route, day)
    ]
    merged = heapq.merge(*route_streams, key=lambda row: (row[0], row[1]))
    active: dict[str, int] = {}
    logical_epoch = 0
    sequence = 0
    seen: dict[tuple[Any, ...], int] = {}
    last_receive_ns = -1
    for receive_ns, route, payload in merged:
        last_receive_ns = max(last_receive_ns, receive_ns)
        marker = payload.get("_recorder")
        if isinstance(marker, Mapping) and marker.get("schema") == "recorder-lifecycle-v1":
            kind = marker.get("kind")
            epoch = int(marker["epoch"])
            if kind == "connection":
                was_empty = not active
                active[route] = epoch
                stats["max_active_connections"] = max(stats["max_active_connections"], len(active))
                if was_empty:
                    logical_epoch += 1
                    event = {
                        "kind": f"{role}_connection", "recv_ms": receive_ns / 1e6,
                        "source_ts_ms": None, "connection_epoch": logical_epoch,
                        "stream_sequence": sequence, "stream": role,
                    }
                    yield event["recv_ms"], SOURCE_RANK[role], sequence, event
                    sequence += 1
            elif kind == "disconnect" and active.get(route) == epoch:
                active.pop(route)
                if not active:
                    event = {
                        "kind": f"{role}_disconnect", "recv_ms": receive_ns / 1e6,
                        "source_ts_ms": None, "connection_epoch": logical_epoch,
                        "stream_sequence": sequence, "stream": role,
                    }
                    yield event["recv_ms"], SOURCE_RANK[role], sequence, event
                    sequence += 1
            continue
        if route not in active:
            key = "leading_carryover_frames" if logical_epoch == 0 else "dropped_outside_epoch"
            stats[key] += 1
            continue
        parsed = _frame_event(role, payload)
        if parsed is None:
            continue
        key = (parsed["kind"], *parsed.pop("dedupe"))
        prior = seen.get(key)
        if prior is not None and receive_ns - prior <= 2_000_000_000:
            stats["deduplicated_frames"] += 1
            continue
        seen[key] = receive_ns
        if len(seen) > 200_000:
            cutoff = receive_ns - 2_000_000_000
            seen = {item: timestamp for item, timestamp in seen.items() if timestamp >= cutoff}
        numeric = [parsed.get(name) for name in ("source_ts_ms", "price", "size", "bid", "ask")
                   if parsed.get(name) is not None]
        if not all(math.isfinite(float(value)) for value in numeric):
            stats["dropped_invalid_frames"] += 1
            continue
        if parsed.get("price") is not None and (
            float(parsed["price"]) <= 0 or float(parsed.get("size") or 0) <= 0
        ):
            stats["dropped_invalid_frames"] += 1
            continue
        parsed.update(
            recv_ms=receive_ns / 1e6,
            connection_epoch=logical_epoch,
            stream_sequence=sequence,
            stream=role,
            source_route=route,
        )
        yield parsed["recv_ms"], SOURCE_RANK[role], sequence, parsed
        sequence += 1
    if active and last_receive_ns >= 0:
        event = {
            "kind": f"{role}_disconnect", "recv_ms": segment_end_ms - 0.001,
            "source_ts_ms": None, "connection_epoch": logical_epoch,
            "stream_sequence": sequence, "stream": role, "error": "segment_boundary",
        }
        yield event["recv_ms"], SOURCE_RANK[role], sequence, event
        active.clear()
    stats["closed_at_segment_end"] = not active


def iter_source_events(root: Path, day: str, segment_end_ms: float):
    stats = {
        role: {
            "max_active_connections": 0,
            "leading_carryover_frames": 0,
            "dropped_outside_epoch": 0,
            "deduplicated_frames": 0,
            "dropped_invalid_frames": 0,
            "closed_at_segment_end": False,
        }
        for role in SOURCE_ROUTES
    }
    streams = [
        _iter_source_role(root, day, role, segment_end_ms, stats[role])
        for role in SOURCE_ROUTES
    ]
    for global_sequence, (_, _, _, event) in enumerate(heapq.merge(*streams, key=lambda row: row[:3])):
        event["seq"] = global_sequence
        yield event, stats


def _parse_list(value: object) -> list[Any]:
    if isinstance(value, str):
        value = json.loads(value)
    return list(value) if isinstance(value, (list, tuple)) else []


def _market_meta(row: Mapping[str, Any], markets: dict[str, dict[str, Any]], tokens: dict[str, str]) -> None:
    message = row.get("m")
    if not isinstance(message, Mapping):
        return
    slug = str(message.get("meta", ""))
    if not slug.startswith("btc-updown-5m-"):
        return
    outcomes = _parse_list(message.get("outcomes"))
    token_ids = [str(value) for value in _parse_list(message.get("tokens"))]
    if len(outcomes) != 2 or len(token_ids) != 2 or set(outcomes) != {"Up", "Down"}:
        raise ValueError(f"invalid market metadata for {slug}")
    up = token_ids[outcomes.index("Up")]
    down = token_ids[outcomes.index("Down")]
    market_id = str(message.get("cid") or slug)
    market = {
        "market_id": market_id,
        "slug": slug,
        "start_ts": int(slug.rsplit("-", 1)[1]),
        "up_token_id": up,
        "down_token_id": down,
        "updated_at": float(row["rn"]) / 1e9,
        "fee_rate": "",
    }
    markets[market_id] = market
    tokens[up] = market_id
    tokens[down] = market_id


def iter_clob_events(paths: Iterable[Path], segment_end_ms: float):
    markets: dict[str, dict[str, Any]] = {}
    tokens: dict[str, str] = {}
    active: set[tuple[int, int]] = set()
    physical_sequence: dict[tuple[int, int], int] = {}
    logical_epoch = 0
    sequence = 0
    seen: dict[str, int] = {}
    next_seen_prune_ns = 0
    stats = {
        "explicit_order": True,
        "physical_sequence_regressions": 0,
        "leading_carryover_frames": 0,
        "dropped_outside_epoch": 0,
        "deduplicated_frames": 0,
        "max_active_connections": 0,
        "closed_at_segment_end": False,
        "snapshots": set(),
    }
    last_receive_ns = -1
    for path in sorted(paths):
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                row = _loads(line)
                if not isinstance(row, Mapping) or row.get("rn") is None:
                    raise ValueError(f"{path.name}:{line_number}: invalid CLOB envelope")
                receive_ns = int(row["rn"])
                if receive_ns < last_receive_ns:
                    raise ValueError(f"{path.name}:{line_number}: CLOB receive clock regressed")
                last_receive_ns = receive_ns
                if row.get("c") == -1 or row.get("k") == "meta":
                    _market_meta(row, markets, tokens)
                    continue
                kind = row.get("k")
                if kind in ("connection", "disconnect", "message", "error"):
                    try:
                        connection = (int(row["c"]), int(row["e"]))
                        physical = int(row["s"])
                    except (KeyError, TypeError, ValueError) as exc:
                        stats["explicit_order"] = False
                        raise ValueError(f"{path.name}:{line_number}: missing CLOB lifecycle") from exc
                    if logical_epoch == 0 and kind != "connection":
                        stats["leading_carryover_frames"] += 1
                        continue
                    previous = physical_sequence.get(connection, -1)
                    if physical != previous + 1:
                        stats["physical_sequence_regressions"] += 1
                    physical_sequence[connection] = physical
                else:
                    stats["explicit_order"] = False
                    stats["dropped_outside_epoch"] += 1
                    continue
                if kind == "connection":
                    was_empty = not active
                    active.add(connection)
                    stats["max_active_connections"] = max(stats["max_active_connections"], len(active))
                    if was_empty:
                        logical_epoch += 1
                        yield {
                            "kind": "clob_connection", "recv_ms": receive_ns / 1e6,
                            "source_ts_ms": None, "connection_epoch": logical_epoch,
                            "seq": sequence, "token_count": len(row.get("assets") or ()),
                        }, markets, tokens, stats
                        sequence += 1
                    continue
                if kind == "disconnect":
                    active.discard(connection)
                    if not active:
                        yield {
                            "kind": "clob_error", "recv_ms": receive_ns / 1e6,
                            "source_ts_ms": None, "connection_epoch": logical_epoch,
                            "seq": sequence, "error": "all_warm_connections_closed",
                        }, markets, tokens, stats
                        sequence += 1
                    continue
                if kind == "error":
                    continue
                if connection not in active:
                    stats["dropped_outside_epoch"] += 1
                    continue
                messages = row.get("m")
                for message in messages if isinstance(messages, list) else [messages]:
                    if not isinstance(message, Mapping):
                        continue
                    canonical = json.dumps(message, sort_keys=True, separators=(",", ":"))
                    digest = hashlib.sha256(canonical.encode()).hexdigest()
                    prior = seen.get(digest)
                    if prior is not None and receive_ns - prior <= 2_000_000_000:
                        stats["deduplicated_frames"] += 1
                        continue
                    seen[digest] = receive_ns
                    if receive_ns >= next_seen_prune_ns:
                        cutoff = receive_ns - 2_000_000_000
                        seen = {key: timestamp for key, timestamp in seen.items() if timestamp >= cutoff}
                        next_seen_prune_ns = receive_ns + 2_000_000_000
                    source_ms = _clock_ms(message.get("timestamp"))
                    if source_ms is None:
                        continue
                    event_type = message.get("event_type")
                    if event_type == "book":
                        token = str(message.get("asset_id", ""))
                        if token not in tokens:
                            continue
                        event = {
                            "kind": "clob_snapshot", "recv_ms": receive_ns / 1e6,
                            "source_ts_ms": source_ms, "connection_epoch": logical_epoch,
                            "seq": sequence, "connection_id": connection[0],
                            "market_id": str(message.get("market") or tokens[token]),
                            "asset_id": token,
                            "bids": message.get("bids") if "bids" in message else message.get("buys") or [],
                            "asks": message.get("asks") if "asks" in message else message.get("sells") or [],
                        }
                        stats["snapshots"].add((logical_epoch, token))
                        yield event, markets, tokens, stats
                        sequence += 1
                    elif event_type == "price_change":
                        for change in message.get("price_changes") or ():
                            token = str(change.get("asset_id", ""))
                            if token not in tokens:
                                continue
                            event = {
                                "kind": "clob_price_change", "recv_ms": receive_ns / 1e6,
                                "source_ts_ms": source_ms, "connection_epoch": logical_epoch,
                                "seq": sequence, "connection_id": connection[0],
                                "market_id": str(message.get("market") or tokens[token]),
                                "asset_id": token, "price": change.get("price"),
                                "size": change.get("size"), "side": change.get("side"),
                            }
                            for name in ("best_bid", "best_ask"):
                                if name in change:
                                    event[name] = change[name]
                            yield event, markets, tokens, stats
                            sequence += 1
    if active and last_receive_ns >= 0:
        yield {
            "kind": "clob_error", "recv_ms": segment_end_ms - 0.001,
            "source_ts_ms": None, "connection_epoch": logical_epoch,
            "seq": sequence, "error": "segment_boundary",
        }, markets, tokens, stats
        active.clear()
    stats["closed_at_segment_end"] = not active


def fetch_official_outcomes(markets: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    by_slug = {str(market["slug"]): market for market in markets.values()}
    output = {}
    slugs = sorted(by_slug)
    for offset in range(0, len(slugs), 50):
        for row in pm_outcomes.fetch_batch(slugs[offset:offset + 50]):
            up = float(row["up"])
            if not math.isfinite(up):
                continue
            market = by_slug[str(row["slug"])]
            output[str(market["market_id"])] = {
                "market_id": str(market["market_id"]),
                "winner": "Up" if up > 0.5 else "Down",
                "resolution_ts": int(market["start_ts"]) + 300,
                "source": "gamma",
                "recorded_at": time.time(),
            }
    return output


def _write_csv(path: Path, fields: tuple[str, ...], rows: Iterable[Mapping[str, Any]]) -> None:
    with gzip.open(path, "wt", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def build(
    data_dir: str | Path,
    poly_dir: str | Path,
    out: str | Path,
    day: str,
    *,
    outcomes: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    start = datetime.strptime(day, "%Y%m%d").replace(tzinfo=timezone.utc).timestamp()
    end = start + 86_400
    end_ms = end * 1_000
    destination = Path(out) / "strict"
    destination.mkdir(parents=True, exist_ok=True)
    for name in (
        "manifest.json", "source_events.jsonl.gz", "clob_events.jsonl.gz",
        "market_registry.csv.gz", "market_outcomes.csv.gz",
    ):
        path = destination / name
        if path.exists():
            path.unlink()
    counts: Counter[str] = Counter()
    first_receive = math.inf
    last_receive = -math.inf
    source_stats = None
    with gzip.open(destination / "source_events.jsonl.gz", "wt", encoding="utf-8", compresslevel=3) as stream:
        for event, source_stats in iter_source_events(Path(data_dir), day, end_ms):
            stream.write(json.dumps(event, separators=(",", ":"), allow_nan=False) + "\n")
            counts[event["kind"]] += 1
            first_receive = min(first_receive, float(event["recv_ms"]))
            last_receive = max(last_receive, float(event["recv_ms"]))
    markets: dict[str, dict[str, Any]] = {}
    tokens: dict[str, str] = {}
    clob_stats = None
    poly_paths = sorted(Path(poly_dir).glob(f"poly_clob.{day}T??.jsonl.gz"))
    with gzip.open(destination / "clob_events.jsonl.gz", "wt", encoding="utf-8", compresslevel=3) as stream:
        for event, markets, tokens, clob_stats in iter_clob_events(poly_paths, end_ms):
            stream.write(json.dumps(event, separators=(",", ":"), allow_nan=False) + "\n")
            counts[event["kind"]] += 1
            first_receive = min(first_receive, float(event["recv_ms"]))
            last_receive = max(last_receive, float(event["recv_ms"]))
    official = dict(outcomes) if outcomes is not None else fetch_official_outcomes(markets)
    registry = [markets[key] for key in sorted(markets, key=lambda key: markets[key]["start_ts"])]
    settled = [official[key] for key in sorted(official) if key in markets]
    _write_csv(
        destination / "market_registry.csv.gz",
        ("market_id", "slug", "start_ts", "up_token_id", "down_token_id", "updated_at", "fee_rate"),
        registry,
    )
    _write_csv(
        destination / "market_outcomes.csv.gz",
        ("market_id", "winner", "resolution_ts", "source", "recorded_at"),
        settled,
    )
    counts["markets"] = len(registry)
    counts["outcomes"] = len(settled)
    snapshots = set() if clob_stats is None else clob_stats["snapshots"]
    snapshot_epochs = {epoch for epoch, _token in snapshots}
    paired = {
        market_id for market_id, market in markets.items()
        if any((epoch, market["up_token_id"]) in snapshots
               and (epoch, market["down_token_id"]) in snapshots
               for epoch in snapshot_epochs)
    }
    resolved_ids = {str(row["market_id"]) for row in settled}
    source_stats = source_stats or {}
    clob_stats = clob_stats or {}
    required_source_counts = (
        "spot_connection", "spot_trade", "spot_disconnect",
        "futures_connection", "futures_bbo", "futures_disconnect",
        "futures_trade_connection", "futures_trade", "futures_trade_disconnect",
        "deribit_connection", "deribit_quote", "deribit_disconnect",
    )
    complete_checks = {
        "market_registry": bool(registry),
        "market_outcomes": bool(settled),
        "all_market_outcomes": len(settled) == len(registry),
        "both_token_snapshots": bool(paired),
        "all_resolved_token_snapshots": resolved_ids <= paired,
        "all_source_lifecycles": all(counts[name] > 0 for name in required_source_counts),
        "source_race_width": all(
            source_stats.get(role, {}).get("max_active_connections", 0) >= len(routes)
            for role, routes in SOURCE_ROUTES.items()
        ),
        "source_closed": all(
            source_stats.get(role, {}).get("closed_at_segment_end") is True
            for role in SOURCE_ROUTES
        ),
        "source_epoch": all(
            source_stats.get(role, {}).get("dropped_outside_epoch") == 0
            for role in SOURCE_ROUTES
        ),
        "clob_explicit_order": clob_stats.get("explicit_order") is True,
        "clob_sequence": clob_stats.get("physical_sequence_regressions") == 0,
        "clob_epoch": clob_stats.get("dropped_outside_epoch") == 0,
        "clob_race_width": clob_stats.get("max_active_connections", 0) >= 4,
        "clob_closed": clob_stats.get("closed_at_segment_end") is True,
        "full_day": bool(math.isfinite(first_receive) and first_receive <= start * 1_000 + 60_000
                         and last_receive >= end_ms - 60_000),
    }
    failure_reasons = sorted(name for name, passed in complete_checks.items() if not passed)
    serial_clob_stats = {key: value for key, value in clob_stats.items() if key != "snapshots"}
    manifest = {
        "schema": SCHEMA,
        "run_id": f"eu-west-{day}",
        "collector_region": "eu-west-1",
        "complete": not failure_reasons,
        "receipt_race_ready": not failure_reasons,
        "recorder_complete": not failure_reasons,
        "completeness_checks": complete_checks,
        "failure_reasons": failure_reasons,
        "receipt_race_checks": complete_checks,
        "receipt_race_failure_reasons": failure_reasons,
        "started_ms": first_receive if math.isfinite(first_receive) else None,
        "ended_ms": last_receive if math.isfinite(last_receive) else None,
        "counts": dict(sorted(counts.items())),
        "source_integrity": source_stats,
        "clob_integrity": serial_clob_stats,
        "markets_with_both_token_snapshots": len(paired),
        "missing_resolved_market_ids": sorted(resolved_ids - paired),
        "unresolved_market_ids": sorted(set(markets) - set(official)),
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True)
    parser.add_argument("--poly", required=True)
    parser.add_argument("--day", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    result = build(args.data, args.poly, args.out, args.day)
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
