"""Streaming adapters for strict replay of raw Polymarket archive files."""

from __future__ import annotations

import csv
import gzip
import heapq
import json
import shutil
import subprocess
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


class ArchiveFormatError(ValueError):
    """A source archive record does not match the replay boundary contract."""


@contextmanager
def _open_text(path):
    path = Path(path)
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
                    yield {
                        "kind": "market_mapping",
                        "recv_ms": float(row["updated_at"]) * 1_000,
                        "seq": seq,
                        "source_ts_ms": None,
                        "market_id": row["market_id"],
                        "slot": int(row["start_ts"]),
                        "up_token_id": row["up_token_id"],
                        "down_token_id": row["down_token_id"],
                    }
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
                    "source_ts_ms": float(row["resolution_ts"]) * 1_000,
                    "market_id": row["market_id"],
                    "winner": winner,
                    "up_won": winner == "Up",
                    "resolution_source": row["source"],
                }
            except (KeyError, TypeError, ValueError) as exc:
                raise ArchiveFormatError(f"{path.name}:{seq + 2}: invalid market outcome row") from exc
