"""Convert the eu-west data-only recorder into one strict receipt-causal replay artifact."""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import gzip
import hashlib
import heapq
import json
import math
import os
import shutil
import time
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pm_outcomes

try:
    import orjson
except ImportError:  # The converter remains usable in minimal audit environments.
    orjson = None

try:
    import isal
    from isal import igzip
except ImportError:  # Standard gzip remains the correctness fallback.
    isal = None
    igzip = None


SCHEMA = "polymarket-5m-strict-replay-v2"
SOURCE_ROUTES = {
    "spot": ("bn_spot_2", "bn_spot_3", "bn_spot_4"),
    "futures": ("bn_fut_pub_2", "bn_fut_pub_3"),
    "futures_trade": ("bn_fut_trade", "bn_fut_trade_2"),
    "deribit": ("deribit",),
}
SOURCE_RANK = {name: rank for rank, name in enumerate(SOURCE_ROUTES)}
GZIP_LEVEL = 1
JSON_BACKEND = "orjson" if orjson is not None else "json"
JSON_BACKEND_VERSION = getattr(orjson, "__version__", None)
GZIP_BACKEND = "isal.igzip" if igzip is not None else "gzip"
GZIP_BACKEND_VERSION = getattr(isal, "__version__", None)
MAX_JSON_RECORD_BYTES = 64 * 1024 * 1024
def _gzip_open(path: Path, mode: str, *, compresslevel: int | None = None):
    if "r" in mode:
        # stdlib gzip validates trailers and supports concatenated members in one pass.  Probing
        # with isal first decompressed every large input twice and could not improve correctness.
        opener = gzip.open
    else:
        opener = igzip.open if igzip is not None else gzip.open
    options = {} if compresslevel is None else {"compresslevel": compresslevel}
    return opener(path, mode, **options)


def _loads(value: str | bytes) -> Any:
    return orjson.loads(value) if orjson is not None else json.loads(value)


def _dumps_line(value: Mapping[str, Any]) -> bytes:
    if orjson is not None:
        return orjson.dumps(value, option=orjson.OPT_APPEND_NEWLINE)
    return (json.dumps(value, separators=(",", ":"), allow_nan=False) + "\n").encode()


def _iter_json_records(path: Path) -> Iterator[tuple[int, Any]]:
    """Read JSONL while recovering a record split by raw transport whitespace.

    Older CLOB recordings occasionally embedded a trailing websocket newline immediately before
    the envelope's closing brace.  That creates two physical gzip lines but remains one valid JSON
    value once joined.  Only parse failures exactly at the current buffer end are continued; all
    other malformed data still fails closed.
    """
    pending = bytearray()
    start_line = 0
    with _gzip_open(path, "rb") as stream:
        for line_number, line in enumerate(stream, 1):
            if not pending:
                start_line = line_number
            pending.extend(line)
            if len(pending) > MAX_JSON_RECORD_BYTES:
                raise ValueError(f"{path.name}:{start_line}: JSON record exceeds size limit")
            try:
                row = _loads(pending)
            except ValueError as exc:
                position = getattr(exc, "pos", None)
                if position is not None and position >= len(pending.rstrip(b"\r\n")):
                    continue
                raise ValueError(f"{path.name}:{start_line}: invalid JSON record") from exc
            yield start_line, row
            pending.clear()
    if pending:
        raise ValueError(f"{path.name}:{start_line}: truncated JSON record")


def _known_irrelevant_frame(role: str, raw: str | bytes) -> bool:
    """Fast-reject only frame types explicitly subscribed but unused by the strict tape.

    Unknown or malformed frames still reach the JSON parser and retain the previous fail-closed
    behaviour.  The predicate therefore cannot hide a malformed trade, quote, or lifecycle marker.
    """
    encoded = raw.encode() if isinstance(raw, str) else raw
    if b'"_recorder"' in encoded:
        return False
    if role == "spot":
        relevant = b"@trade" in encoded or b'"e":"trade"' in encoded or b'"e": "trade"' in encoded
        irrelevant = b"@bookTicker" in encoded or b"@depth20" in encoded
        return irrelevant and not relevant
    if role == "futures":
        return b"@depth20" in encoded and b"bookTicker" not in encoded
    if role == "deribit":
        relevant = b"quote.BTC-PERPETUAL" in encoded
        irrelevant = (b"trades.BTC-PERPETUAL.100ms" in encoded
                      or b"deribit_price_index.btc_usd" in encoded)
        return irrelevant and not relevant
    return False


def _clock_ms(value: object) -> float | None:
    if value is None:
        return None
    result = float(value)
    if result > 1e17:
        result /= 1_000_000.0
    elif result > 1e14:
        result /= 1_000.0
    return result


def _iter_route(
    paths: Iterable[Path],
    route: str,
    role: str,
    stats: dict[str, Any],
) -> Iterator[tuple[int, str, dict[str, Any]]]:
    previous = -1
    for path in sorted(paths):
        with _gzip_open(path, "rb") as stream:
            for line_number, line in enumerate(stream, 1):
                stamp, separator, raw = line.rstrip(b"\n").partition(b"\t")
                if not separator:
                    raise ValueError(f"{path.name}:{line_number}: missing receive timestamp")
                receive_ns = int(stamp)
                if receive_ns < previous:
                    raise ValueError(f"{path.name}:{line_number}: receive clock regressed")
                previous = receive_ns
                if _known_irrelevant_frame(role, raw):
                    stats["skipped_irrelevant_frames"] += 1
                    continue
                payload = _loads(raw)
                if not isinstance(payload, dict):
                    raise TypeError(f"{path.name}:{line_number}: frame is not an object")
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
        raw_symbol = str(data.get("s") or stream.partition("@")[0]).upper()
        if raw_symbol != "BTCUSDT":
            raise ValueError(f"unexpected spot symbol {raw_symbol!r}")
        price, size = float(data["p"]), float(data.get("q") or 0)
        if not (math.isfinite(price) and math.isfinite(size) and price > 0 and size > 0):
            return None
        return {
            "kind": "spot_trade", "source_ts_ms": _clock_ms(data.get("T")),
            "price": price, "size": size, "symbol": "BTC",
            "dedupe": (data.get("t"), data.get("T"), data.get("p"), data.get("q")),
        }
    if role == "futures":
        if event_name != "bookTicker" and "bookTicker" not in stream:
            return None
        raw_symbol = str(data.get("s") or stream.partition("@")[0]).upper()
        if raw_symbol != "BTCUSDT":
            raise ValueError(f"unexpected futures symbol {raw_symbol!r}")
        bid, ask = float(data["b"]), float(data["a"])
        if not 0 < bid < ask:
            return None
        return {
            "kind": "futures_bbo", "source_ts_ms": _clock_ms(data.get("T") or data.get("E")),
            "event_ts_ms": _clock_ms(data.get("E")), "bid": bid, "ask": ask,
            "symbol": "BTC",
            "dedupe": (data.get("u"), data.get("T"), data.get("b"), data.get("a")),
        }
    if role == "futures_trade":
        if event_name != "trade" and not stream.endswith("@trade"):
            return None
        raw_symbol = str(data.get("s") or stream.partition("@")[0]).upper()
        if raw_symbol != "BTCUSDT":
            raise ValueError(f"unexpected futures-trade symbol {raw_symbol!r}")
        price, size = float(data["p"]), float(data.get("q") or 0)
        if not (math.isfinite(price) and math.isfinite(size) and price > 0 and size > 0):
            return None
        return {
            "kind": "futures_trade", "source_ts_ms": _clock_ms(data.get("T")),
            "event_ts_ms": _clock_ms(data.get("E")), "price": price, "size": size,
            "symbol": "BTC",
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
            "ask_size": float(quote.get("best_ask_amount") or 0), "symbol": "BTC",
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
        _iter_route(_raw_paths(root, route, day), route, role, stats)
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
    stats = {role: _new_source_stats() for role in SOURCE_ROUTES}
    streams = [
        _iter_source_role(root, day, role, segment_end_ms, stats[role])
        for role in SOURCE_ROUTES
    ]
    for global_sequence, (_, _, _, event) in enumerate(heapq.merge(*streams, key=lambda row: row[:3])):
        event["seq"] = global_sequence
        yield event, stats


def _new_source_stats() -> dict[str, Any]:
    return {
        "max_active_connections": 0,
        "leading_carryover_frames": 0,
        "dropped_outside_epoch": 0,
        "deduplicated_frames": 0,
        "dropped_invalid_frames": 0,
        "skipped_irrelevant_frames": 0,
        "closed_at_segment_end": False,
    }


def _write_source_role_part(
    data_dir: str,
    day: str,
    role: str,
    segment_end_ms: float,
    target: str,
) -> dict[str, Any]:
    started = time.perf_counter()
    stats = _new_source_stats()
    count = 0
    with Path(target).open("wb") as stream:
        for _receive, _rank, _sequence, event in _iter_source_role(
            Path(data_dir), day, role, segment_end_ms, stats,
        ):
            stream.write(_dumps_line(event))
            count += 1
    return {"role": role, "stats": stats, "events": count,
            "seconds": time.perf_counter() - started}


def _iter_source_part(path: Path, role: str):
    with path.open("rb") as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                event = _loads(line)
                yield (
                    float(event["recv_ms"]), SOURCE_RANK[role],
                    int(event["stream_sequence"]), event,
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"{path.name}:{line_number}: invalid source part") from exc


def _write_clob_stream(
    poly_dir: str,
    day: str,
    segment_end_ms: float,
    target: str,
) -> dict[str, Any]:
    started = time.perf_counter()
    counts: Counter[str] = Counter()
    first_receive = math.inf
    last_receive = -math.inf
    markets: dict[str, dict[str, Any]] = {}
    tokens: dict[str, str] = {}
    stats = None
    paths = sorted(Path(poly_dir).glob(f"poly_clob.{day}T??.jsonl.gz"))
    with _gzip_open(Path(target), "wb", compresslevel=GZIP_LEVEL) as stream:
        for event, markets, tokens, stats in iter_clob_events(paths, segment_end_ms):
            stream.write(_dumps_line(event))
            if event["kind"] == "clob_price_change_batch":
                counts["clob_price_change"] += len(event["changes"])
            else:
                counts[event["kind"]] += 1
            first_receive = min(first_receive, float(event["recv_ms"]))
            last_receive = max(last_receive, float(event["recv_ms"]))
    return {
        "counts": dict(counts),
        "first_receive": first_receive,
        "last_receive": last_receive,
        "markets": markets,
        "stats": stats,
        "seconds": time.perf_counter() - started,
    }


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
        for line_number, row in _iter_json_records(path):
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
                event_type = message.get("event_type")
                if event_type == "book":
                    if str(message.get("asset_id", "")) not in tokens:
                        continue
                elif event_type == "price_change":
                    changes = [
                        change for change in message.get("price_changes") or ()
                        if isinstance(change, Mapping)
                        and str(change.get("asset_id", "")) in tokens
                    ]
                    if not changes:
                        continue
                else:
                    continue
                source_ms = _clock_ms(message.get("timestamp"))
                if source_ms is None:
                    continue
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
                    normalized = []
                    for change in changes:
                        item = {
                            "asset_id": str(change["asset_id"]),
                            "price": change.get("price"), "size": change.get("size"),
                            "side": change.get("side"),
                        }
                        for name in ("best_bid", "best_ask"):
                            if name in change:
                                item[name] = change[name]
                        normalized.append(item)
                    # One physical row per venue frame.  The canonical reader expands it back to
                    # the exact logical clob_price_change stream with contiguous sequence values.
                    token = normalized[0]["asset_id"]
                    event = {
                        "kind": "clob_price_change_batch", "recv_ms": receive_ns / 1e6,
                        "source_ts_ms": source_ms, "connection_epoch": logical_epoch,
                        "seq": sequence, "connection_id": connection[0],
                        "market_id": str(message.get("market") or tokens[token]),
                        "changes": normalized,
                    }
                    yield event, markets, tokens, stats
                    sequence += len(normalized)
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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _file_identity(path: Path) -> dict[str, Any]:
    return {"bytes": path.stat().st_size, "sha256": _sha256(path)}


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _phase_paths(out: str | Path) -> tuple[Path, Path, Path]:
    destination = Path(out) / "strict"
    destination.mkdir(parents=True, exist_ok=True)
    return destination, destination / ".source-phase.json", destination / ".clob-phase.json"


def prepare_source(data_dir: str | Path, out: str | Path, day: str) -> dict[str, Any]:
    started = time.perf_counter()
    destination, state_path, _ = _phase_paths(out)
    (destination / "manifest.json").unlink(missing_ok=True)
    target = destination / "source_events.jsonl.gz"
    temporary = target.with_name(target.name + ".tmp")
    temporary.unlink(missing_ok=True)
    parts = destination / ".source-parts"
    shutil.rmtree(parts, ignore_errors=True)
    parts.mkdir()
    end_ms = (
        datetime.strptime(day, "%Y%m%d").replace(tzinfo=timezone.utc).timestamp() + 86_400
    ) * 1_000
    workers = max(1, min(int(os.environ.get("STRICT_BUILD_WORKERS", "2")), 2))
    counts: Counter[str] = Counter()
    first_receive, last_receive = math.inf, -math.inf
    try:
        with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as executor:
            futures = {
                role: executor.submit(
                    _write_source_role_part,
                    str(data_dir), day, role, end_ms, str(parts / f"{role}.jsonl"),
                )
                for role in SOURCE_ROUTES
            }
            results = {role: future.result() for role, future in futures.items()}
        merge_started = time.perf_counter()
        streams = [_iter_source_part(parts / f"{role}.jsonl", role) for role in SOURCE_ROUTES]
        with _gzip_open(temporary, "wb", compresslevel=GZIP_LEVEL) as stream:
            for global_sequence, (_receive, _rank, _sequence, event) in enumerate(
                heapq.merge(*streams, key=lambda row: row[:3]),
            ):
                event["seq"] = global_sequence
                stream.write(_dumps_line(event))
                counts[event["kind"]] += 1
                first_receive = min(first_receive, float(event["recv_ms"]))
                last_receive = max(last_receive, float(event["recv_ms"]))
        merge_seconds = time.perf_counter() - merge_started
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
        shutil.rmtree(parts, ignore_errors=True)
    state = {
        "schema": "eu-strict-source-phase-v1", "day": day,
        "output": _file_identity(target), "counts": dict(counts),
        "first_receive": first_receive if math.isfinite(first_receive) else None,
        "last_receive": last_receive if math.isfinite(last_receive) else None,
        "source_integrity": {role: result["stats"] for role, result in results.items()},
        "runtime": {
            "source": time.perf_counter() - started,
            "source_workers": {role: result["seconds"] for role, result in results.items()},
            "source_merge": merge_seconds,
        },
    }
    _atomic_json(state_path, state)
    return state


def prepare_clob(poly_dir: str | Path, out: str | Path, day: str) -> dict[str, Any]:
    destination, _, state_path = _phase_paths(out)
    (destination / "manifest.json").unlink(missing_ok=True)
    target = destination / "clob_events.jsonl.gz"
    temporary = target.with_name(target.name + ".tmp")
    temporary.unlink(missing_ok=True)
    end_ms = (
        datetime.strptime(day, "%Y%m%d").replace(tzinfo=timezone.utc).timestamp() + 86_400
    ) * 1_000
    try:
        result = _write_clob_stream(str(poly_dir), day, end_ms, str(temporary))
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    stats = dict(result["stats"] or {})
    stats["snapshots"] = sorted([list(value) for value in stats.get("snapshots", set())])
    state = {
        "schema": "eu-strict-clob-phase-v1", "day": day,
        "output": _file_identity(target), "counts": result["counts"],
        "first_receive": result["first_receive"] if math.isfinite(result["first_receive"]) else None,
        "last_receive": result["last_receive"] if math.isfinite(result["last_receive"]) else None,
        "markets": result["markets"], "clob_integrity": stats,
        "runtime": {"clob": result["seconds"]},
    }
    _atomic_json(state_path, state)
    return state


def _load_phase(path: Path, schema: str, day: str, output: Path) -> dict[str, Any]:
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("schema") != schema or state.get("day") != day:
        raise ValueError(f"{path.name}: phase identity drift")
    if not output.is_file() or state.get("output") != _file_identity(output):
        raise ValueError(f"{path.name}: phase output drift")
    return state


def finalize(
    out: str | Path,
    day: str,
    *,
    outcomes: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    destination, source_path, clob_path = _phase_paths(out)
    source = _load_phase(
        source_path, "eu-strict-source-phase-v1", day, destination / "source_events.jsonl.gz",
    )
    clob = _load_phase(
        clob_path, "eu-strict-clob-phase-v1", day, destination / "clob_events.jsonl.gz",
    )
    counts = Counter(source["counts"])
    counts.update(clob["counts"])
    first_values = [value for value in (source["first_receive"], clob["first_receive"]) if value is not None]
    last_values = [value for value in (source["last_receive"], clob["last_receive"]) if value is not None]
    first_receive = min(first_values, default=math.inf)
    last_receive = max(last_values, default=-math.inf)
    markets = clob["markets"]
    clob_stats = dict(clob["clob_integrity"])
    clob_stats["snapshots"] = {tuple(value) for value in clob_stats.get("snapshots", [])}
    source_stats = source["source_integrity"]
    outcomes_started = time.perf_counter()
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
    counts["markets"], counts["outcomes"] = len(registry), len(settled)
    snapshots = clob_stats["snapshots"]
    snapshot_epochs = {epoch for epoch, _token in snapshots}
    paired = {
        market_id for market_id, market in markets.items()
        if any((epoch, market["up_token_id"]) in snapshots
               and (epoch, market["down_token_id"]) in snapshots
               for epoch in snapshot_epochs)
    }
    resolved_ids = {str(row["market_id"]) for row in settled}
    start = datetime.strptime(day, "%Y%m%d").replace(tzinfo=timezone.utc).timestamp()
    end_ms = (start + 86_400) * 1_000
    required_source_counts = (
        "spot_connection", "spot_trade", "spot_disconnect",
        "futures_connection", "futures_bbo", "futures_disconnect",
        "futures_trade_connection", "futures_trade", "futures_trade_disconnect",
        "deribit_connection", "deribit_quote", "deribit_disconnect",
    )
    complete_checks = {
        "market_registry": bool(registry), "market_outcomes": bool(settled),
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
    failures = sorted(name for name, passed in complete_checks.items() if not passed)
    outcomes_seconds = time.perf_counter() - outcomes_started
    serial_clob_stats = {key: value for key, value in clob_stats.items() if key != "snapshots"}
    artifact_files = {
        name: _file_identity(destination / name)
        for name in (
            "source_events.jsonl.gz", "clob_events.jsonl.gz",
            "market_registry.csv.gz", "market_outcomes.csv.gz",
        )
    }
    phases = {
        "source": round(float(source["runtime"]["source"]), 6),
        "source_workers": {
            role: round(float(value), 6)
            for role, value in source["runtime"]["source_workers"].items()
        },
        "source_merge": round(float(source["runtime"]["source_merge"]), 6),
        "clob": round(float(clob["runtime"]["clob"]), 6),
        "outcomes_and_tables": round(outcomes_seconds, 6),
        "staged_total": round(
            float(source["runtime"]["source"]) + float(clob["runtime"]["clob"])
            + outcomes_seconds, 6,
        ),
        "finalize": round(time.perf_counter() - started, 6),
    }
    manifest = {
        "schema": SCHEMA, "run_id": f"eu-west-{day}", "collector_region": "eu-west-1",
        "runtime": {
            "json_backend": JSON_BACKEND, "json_backend_version": JSON_BACKEND_VERSION,
            "gzip_backend": GZIP_BACKEND, "gzip_backend_version": GZIP_BACKEND_VERSION,
            "gzip_input_policy": "stdlib_single_pass_integrity_check",
            "clob_storage_encoding": "price-change-batch-v1",
            "clob_dedupe_policy": "recorder-first-copy-v1",
            "gzip_compresslevel": GZIP_LEVEL, "phase_seconds": phases,
        },
        "converter_validation": {
            "schema": "eu-strict-converter-validation-v1", "files": artifact_files,
        },
        "complete": not failures, "receipt_race_ready": not failures,
        "recorder_complete": not failures, "completeness_checks": complete_checks,
        "failure_reasons": failures, "receipt_race_checks": complete_checks,
        "receipt_race_failure_reasons": failures,
        "started_ms": first_receive if math.isfinite(first_receive) else None,
        "ended_ms": last_receive if math.isfinite(last_receive) else None,
        "counts": dict(sorted(counts.items())), "source_integrity": source_stats,
        "clob_integrity": serial_clob_stats,
        "markets_with_both_token_snapshots": len(paired),
        "missing_resolved_market_ids": sorted(resolved_ids - paired),
        "unresolved_market_ids": sorted(set(markets) - set(official)),
    }
    _atomic_json(destination / "manifest.json", manifest)
    source_path.unlink()
    clob_path.unlink()
    return manifest


def build(
    data_dir: str | Path,
    poly_dir: str | Path,
    out: str | Path,
    day: str,
    *,
    outcomes: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    destination, source_state, clob_state = _phase_paths(out)
    for name in (
        "manifest.json", "source_events.jsonl.gz", "clob_events.jsonl.gz",
        "market_registry.csv.gz", "market_outcomes.csv.gz",
    ):
        (destination / name).unlink(missing_ok=True)
    source_state.unlink(missing_ok=True)
    clob_state.unlink(missing_ok=True)
    prepare_source(data_dir, out, day)
    prepare_clob(poly_dir, out, day)
    return finalize(out, day, outcomes=outcomes)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True)
    parser.add_argument("--poly", required=True)
    parser.add_argument("--day", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--phase", choices=("all", "source", "clob", "finalize"), default="all")
    args = parser.parse_args()
    if args.phase == "source":
        result = prepare_source(args.data, args.out, args.day)
    elif args.phase == "clob":
        result = prepare_clob(args.poly, args.out, args.day)
    elif args.phase == "finalize":
        result = finalize(args.out, args.day)
    else:
        result = build(args.data, args.poly, args.out, args.day)
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
