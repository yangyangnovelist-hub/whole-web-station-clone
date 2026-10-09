"""Streaming adapters for strict replay of raw Polymarket archive files."""

from __future__ import annotations

import csv
import gzip
import heapq
import json
import math
import shutil
import subprocess
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


class ArchiveFormatError(ValueError):
    """A source archive record does not match the replay boundary contract."""


@contextmanager
def _open_text(path):
    path = Path(path)
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            yield stream
        return
    if path.suffix != ".zst":
        with path.open("rt", encoding="utf-8") as stream:
            yield stream
        return

    zstd = shutil.which("zstd")
    if zstd is None:
        raise RuntimeError("reading .zst archives requires the zstd CLI")
    process = subprocess.Popen(
        [zstd, "-q", "-dc", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    assert process.stdout is not None
    assert process.stderr is not None
    try:
        yield process.stdout
        process.stdout.close()
        error = process.stderr.read()
        if process.wait() != 0:
            raise OSError(f"zstd could not read {path}: {error.strip()}")
    finally:
        process.stdout.close()
        process.stderr.close()
        if process.poll() is None:
            process.terminate()
            process.wait()


def _iter_json_objects(path):
    with _open_text(path) as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ArchiveFormatError(f"{Path(path).name}:{line_number}: invalid JSON") from exc
            if not isinstance(row, dict):
                raise ArchiveFormatError(f"{Path(path).name}:{line_number}: expected a JSON object")
            yield line_number, row


def iter_spot_events(path):
    """Yield normalized Binance spot BBO and trade records in receipt order."""
    seq = 0
    for _, row in _iter_json_objects(path):
        source = row.get("s")
        if source == "binance_spot_bbo":
            yield {
                "kind": "spot_bbo",
                "recv_ms": row["r"] / 1_000_000,
                "seq": seq,
                "source_ts_ms": row.get("e"),
                "bid": row["b"],
                "ask": row["a"],
            }
            seq += 1
        elif source == "binance_spot":
            yield {
                "kind": "spot_trade",
                "recv_ms": row["r"] / 1_000_000,
                "seq": seq,
                "source_ts_ms": row["e"],
                "price": row["p"],
                "size": row["q"],
            }
            seq += 1


def iter_normalized_events(path, *, family):
    """Read the standard forward artifact and enforce its causal stream contract."""
    kinds = {
        "spot": {"spot_trade", "spot_bbo"},
        "source": {"spot_connection", "spot_trade", "spot_bbo", "spot_disconnect",
                   "futures_connection", "futures_bbo", "futures_disconnect",
                   "futures_trade_connection", "futures_trade", "futures_trade_disconnect",
                   "deribit_connection", "deribit_quote", "deribit_dvol", "deribit_disconnect"},
        "clob": {"clob_connection", "clob_snapshot", "clob_price_change", "clob_error"},
    }
    if family not in kinds:
        raise ValueError(f"unknown event family: {family}")
    previous_seq = -1
    previous_recv_ms = -float("inf")
    active_epoch = None
    last_epoch = 0
    source_names = ("spot", "futures", "futures_trade", "deribit")
    source_active = {source: None for source in source_names}
    source_last_epoch = {source: 0 for source in source_names}
    source_previous_sequence = {source: -1 for source in source_names}
    for line_number, row in _iter_json_objects(path):
        try:
            kind = row["kind"]
            sequence = int(row["seq"])
            receive_ms = float(row["recv_ms"])
            if kind not in kinds[family]:
                raise ValueError(f"unexpected {family} kind {kind!r}")
            if sequence != previous_seq + 1:
                raise ValueError(f"non-contiguous seq {sequence} after {previous_seq}")
            if receive_ms < previous_recv_ms:
                raise ValueError(f"recv_ms moved backwards from {previous_recv_ms} to {receive_ms}")
            if "source_ts_ms" not in row:
                raise ValueError("missing source_ts_ms")
            price_kinds = ("spot_trade", "futures_trade")
            quote_kinds = ("spot_bbo", "futures_bbo", "deribit_quote")
            if family in ("spot", "source") and kind in price_kinds + quote_kinds:
                required = ("price", "size") if kind in price_kinds else ("bid", "ask")
                if any(key not in row for key in required):
                    raise ValueError(f"missing {required}")
                values = [float(row[key]) for key in required]
                if not all(math.isfinite(value) for value in values) or values[0] <= 0 or values[1] < 0:
                    raise ValueError(f"invalid numeric {kind}")
                if kind in quote_kinds and values[0] >= values[1]:
                    raise ValueError(f"crossed {kind}")
                if kind in ("spot_trade", "futures_bbo", "futures_trade", "deribit_quote"):
                    source_timestamp = float(row["source_ts_ms"])
                    if not math.isfinite(source_timestamp):
                        raise ValueError(f"invalid source_ts_ms for {kind}")
            if family == "source" and kind == "deribit_dvol":
                volatility = float(row.get("volatility", math.nan))
                source_timestamp = float(row.get("source_ts_ms", math.nan))
                if (not math.isfinite(volatility) or volatility <= 0 or
                        not math.isfinite(source_timestamp) or row.get("index_name") != "btc_usd"):
                    raise ValueError("invalid deribit_dvol")
            if family == "source":
                stream = str(row["stream"])
                belongs_to_stream = kind.startswith(stream + "_") or \
                    (stream == "futures_trade" and kind == "futures_trade")
                if stream not in source_active or not belongs_to_stream:
                    raise ValueError(f"invalid source stream {stream!r} for {kind}")
                stream_sequence = int(row["stream_sequence"])
                if stream_sequence != source_previous_sequence[stream] + 1:
                    raise ValueError(f"non-contiguous {stream} sequence {stream_sequence}")
                source_previous_sequence[stream] = stream_sequence
                epoch = int(row["connection_epoch"])
                if epoch <= 0:
                    raise ValueError("connection_epoch must be positive")
                if kind == f"{stream}_connection":
                    if source_active[stream] is not None or epoch <= source_last_epoch[stream]:
                        raise ValueError(f"invalid {stream} connection epoch {epoch}")
                    source_active[stream] = epoch
                    source_last_epoch[stream] = epoch
                elif source_active[stream] != epoch:
                    raise ValueError(f"{stream} event epoch {epoch} without matching open")
                if kind == f"{stream}_disconnect":
                    source_active[stream] = None
            elif family == "clob":
                if "connection_epoch" not in row:
                    raise ValueError("missing connection_epoch")
                epoch = int(row["connection_epoch"])
                if epoch <= 0:
                    raise ValueError("connection_epoch must be positive")
                if kind == "clob_connection":
                    if active_epoch is not None or epoch <= last_epoch:
                        raise ValueError(f"invalid connection epoch {epoch}")
                    active_epoch, last_epoch = epoch, epoch
                elif active_epoch != epoch:
                    raise ValueError(f"event epoch {epoch} without matching open")
                if kind == "clob_snapshot" and any(key not in row for key in
                                                    ("market_id", "asset_id", "bids", "asks")):
                    raise ValueError("incomplete clob_snapshot")
                if kind == "clob_snapshot":
                    source_timestamp = float(row["source_ts_ms"])
                    for side in ("bids", "asks"):
                        if not isinstance(row[side], list):
                            raise ValueError(f"{side} is not a list")
                        for level in row[side]:
                            price, size = float(level["price"]), float(level["size"])
                            if not (math.isfinite(price) and math.isfinite(size) and 0 <= price <= 1 and size >= 0):
                                raise ValueError(f"invalid {side} level")
                if kind == "clob_price_change" and any(key not in row for key in
                                                        ("market_id", "asset_id", "price", "size", "side")):
                    raise ValueError("incomplete clob_price_change")
                if kind == "clob_price_change":
                    source_timestamp = float(row["source_ts_ms"])
                    price, size = float(row["price"]), float(row["size"])
                    if (row["side"] not in ("BUY", "SELL") or not math.isfinite(price) or
                            not math.isfinite(size) or not 0 <= price <= 1 or size < 0):
                        raise ValueError("invalid clob_price_change")
                if kind in ("clob_snapshot", "clob_price_change"):
                    if not math.isfinite(source_timestamp):
                        raise ValueError("invalid clob source_ts_ms")
                    # The channel multiplexes many tokens whose venue clocks can interleave.
                    # recv_ms/seq is the causal order; the replay applies the live per-token
                    # stale-update guard before mutating a book.
                if kind == "clob_error":
                    active_epoch = None
            previous_seq = sequence
            previous_recv_ms = receive_ms
        except (KeyError, TypeError, ValueError) as exc:
            raise ArchiveFormatError(f"{Path(path).name}:{line_number}: {exc}") from exc
        yield row
    if family == "clob" and active_epoch is not None:
        raise ArchiveFormatError(f"{Path(path).name}: EOF with connection_epoch {active_epoch} still open")
    if family == "source" and any(epoch is not None for epoch in source_active.values()):
        raise ArchiveFormatError(f"{Path(path).name}: EOF with price-source connection still open")


def _iter_clob_file(path, file_index):
    for line_number, outer in _iter_json_objects(path):
        recv_ms = outer["recv_ms"]
        messages = outer.get("msg")
        if not isinstance(messages, list):
            messages = [messages]
        for message_index, message in enumerate(messages):
            if not isinstance(message, dict):
                continue
            source_ts_ms = int(message["timestamp"])
            if message.get("event_type") == "book":
                event = {
                    "kind": "clob_snapshot",
                    "recv_ms": recv_ms,
                    "source_ts_ms": source_ts_ms,
                    "market_id": message.get("market"),
                    "asset_id": message["asset_id"],
                    "bids": message["bids"],
                    "asks": message["asks"],
                }
                if "tick_size" in message:
                    event["tick_size"] = message["tick_size"]
                yield (recv_ms, file_index, line_number, message_index, 0), event
            elif message.get("event_type") == "price_change":
                for change_index, change in enumerate(message["price_changes"]):
                    event = {
                        "kind": "clob_price_change",
                        "recv_ms": recv_ms,
                        "source_ts_ms": source_ts_ms,
                        "market_id": message.get("market"),
                        "asset_id": change["asset_id"],
                        "price": change["price"],
                        "size": change["size"],
                        "side": change["side"],
                    }
                    for key in ("best_bid", "best_ask"):
                        if key in change:
                            event[key] = change[key]
                    yield (recv_ms, file_index, line_number, message_index, change_index), event


def _iter_clob_error_file(path, file_index):
    for line_number, row in _iter_json_objects(path):
        where = row.get("where")
        if where == "clob-open":
            event = {
                "kind": "clob_connection",
                "recv_ms": row["at"],
                "source_ts_ms": None,
                "token_count": row.get("tokens"),
            }
        elif where == "clob":
            event = {
                "kind": "clob_error",
                "recv_ms": row["at"],
                "source_ts_ms": None,
                "error": row.get("err"),
            }
        else:
            continue
        yield (event["recv_ms"], file_index, line_number, 0, 0), event


def iter_clob_events(paths, *, error_paths=()):
    """Yield CLOB snapshots and changes in receive order using bounded memory."""
    sources = [(Path(path), "clob") for path in paths]
    sources += [(Path(path), "error") for path in error_paths]
    sources.sort(key=lambda item: (str(item[0]), item[1]))
    streams = [
        (_iter_clob_file(path, file_index) if source == "clob"
         else _iter_clob_error_file(path, file_index))
        for file_index, (path, source) in enumerate(sources)
    ]
    merged = heapq.merge(*streams, key=lambda item: item[0])
    for seq, (_, event) in enumerate(merged):
        event["seq"] = seq
        yield event


def iter_market_mappings(path):
    """Yield direct Up/Down token mappings from registry CSV or Gamma JSONL."""
    path = Path(path)
    if path.name.endswith(".csv.gz"):
        with gzip.open(path, "rt", encoding="utf-8", newline="") as stream:
            for seq, row in enumerate(csv.DictReader(stream)):
                try:
                    mapping = {
                        "kind": "market_mapping",
                        "recv_ms": float(row["updated_at"]) * 1_000 if row.get("updated_at") else None,
                        "seq": seq,
                        "source_ts_ms": None,
                        "market_id": row["market_id"],
                        "slot": int(row["start_ts"]),
                        "up_token_id": row["up_token_id"],
                        "down_token_id": row["down_token_id"],
                    }
                    if row.get("fee_rate") not in (None, ""):
                        mapping["fee_rate"] = float(row["fee_rate"])
                    yield mapping
                except (KeyError, TypeError, ValueError) as exc:
                    raise ArchiveFormatError(f"{path.name}:{seq + 2}: invalid market registry row") from exc
        return

    for seq, (line_number, row) in enumerate(_iter_json_objects(path)):
        try:
            outcomes = json.loads(row["outcomes"]) if isinstance(row["outcomes"], str) else row["outcomes"]
            tokens = json.loads(row["clobTokenIds"]) if isinstance(row["clobTokenIds"], str) else row["clobTokenIds"]
            if len(outcomes) != 2 or len(tokens) != 2 or set(outcomes) != {"Up", "Down"}:
                raise ValueError("expected direct Up/Down outcomes")
            up_index = outcomes.index("Up")
            down_index = outcomes.index("Down")
            updated_at = datetime.fromisoformat(str(row["updatedAt"]).replace("Z", "+00:00"))
            yield {
                "kind": "market_mapping",
                "recv_ms": None,
                "seq": seq,
                "source_ts_ms": updated_at.timestamp() * 1_000,
                "market_id": row["conditionId"],
                "slot": int(str(row["slug"]).rsplit("-", 1)[1]),
                "up_token_id": str(tokens[up_index]),
                "down_token_id": str(tokens[down_index]),
            }
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ArchiveFormatError(f"{path.name}:{line_number}: invalid Gamma market row") from exc


def iter_outcomes(path):
    """Yield settlement outcomes for scoring without loading the CSV into memory."""
    path = Path(path)
    with gzip.open(path, "rt", encoding="utf-8", newline="") as stream:
        for seq, row in enumerate(csv.DictReader(stream)):
            try:
                winner = row["winner"]
                if winner not in ("Up", "Down"):
                    raise ValueError("winner must be Up or Down")
                yield {
                    "kind": "market_outcome",
                    "recv_ms": float(row["recorded_at"]) * 1_000,
                    "seq": seq,
                    "source_ts_ms": float(row["resolution_ts"]) * 1_000 if row.get("resolution_ts") else None,
                    "market_id": row["market_id"],
                    "winner": winner,
                    "up_won": winner == "Up",
                    "resolution_source": row["source"],
                }
            except (KeyError, TypeError, ValueError) as exc:
                raise ArchiveFormatError(f"{path.name}:{seq + 2}: invalid market outcome row") from exc


def validate_standard_artifact(path):
    """Stream-validate one standard strict artifact and reconcile it with its manifest."""
    root = Path(path)
    if (root / "strict" / "manifest.json").exists():
        root = root / "strict"
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != "polymarket-5m-strict-replay-v2" or manifest.get("complete") is not True:
        raise ArchiveFormatError(f"{root}: incomplete or unknown manifest")
    counts = Counter()
    for event in iter_normalized_events(root / "source_events.jsonl.gz", family="source"):
        counts[event["kind"]] += 1
    for event in iter_normalized_events(root / "clob_events.jsonl.gz", family="clob"):
        counts[event["kind"]] += 1
    mappings = list(iter_market_mappings(root / "market_registry.csv.gz"))
    outcomes = list(iter_outcomes(root / "market_outcomes.csv.gz"))
    counts["markets"] = len(mappings)
    counts["outcomes"] = len(outcomes)
    expected = manifest.get("counts") or {}
    if any(int(expected.get(key, -1)) != counts.get(key, 0) for key in set(expected) | set(counts)):
        raise ArchiveFormatError(f"{root}: manifest counts do not match streams")
    market_ids = {row["market_id"] for row in mappings}
    if not outcomes or any(row["market_id"] not in market_ids for row in outcomes):
        raise ArchiveFormatError(f"{root}: outcome without direct-token market mapping")
    if manifest.get("missing_resolved_market_ids") or not manifest.get("recorder_complete"):
        raise ArchiveFormatError(f"{root}: incomplete resolved-market coverage")
    return {"manifest": manifest, "counts": dict(counts)}
