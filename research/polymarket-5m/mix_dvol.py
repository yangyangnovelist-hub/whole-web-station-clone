"""Receipt-causal DVOL adapter for the frozen MIX projection."""
from __future__ import annotations

import heapq
import math
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import eu_strict
import mix_forward as mix


ROUTE = "deribit_vol"
CHANNEL = "deribit_volatility_index.btc_usd"


def _bump(diagnostics: dict[str, int], key: str) -> None:
    diagnostics[key] = diagnostics.get(key, 0) + 1


def iter_dvol_events(
    paths: Iterable[str | Path], *, diagnostics: dict[str, int] | None = None,
) -> Iterator[dict[str, Any]]:
    """Normalize the dedicated DVOL recorder route without venue-clock ordering."""
    audit = diagnostics if diagnostics is not None else {}
    stats = {"skipped_irrelevant_frames": 0}
    active_epoch: int | None = None
    stream_sequence = 0
    for receive_ns, _route, payload in eu_strict._iter_route(
        [Path(path) for path in paths], ROUTE, "mix_dvol", stats,
    ):
        receive_ms = receive_ns / 1_000_000.0
        marker = payload.get("_recorder")
        if isinstance(marker, Mapping):
            if marker.get("schema") != "recorder-lifecycle-v1":
                raise ValueError("invalid DVOL recorder lifecycle schema")
            kind = str(marker.get("kind") or "")
            try:
                epoch = int(marker["epoch"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("invalid DVOL recorder lifecycle epoch") from exc
            if kind == "connection":
                if active_epoch is not None:
                    yield {
                        "kind": "deribit_disconnect", "recv_ms": receive_ms,
                        "source_ts_ms": None, "connection_epoch": active_epoch,
                        "stream_sequence": stream_sequence, "stream": "deribit",
                        "error": "superseded_connection",
                    }
                    stream_sequence += 1
                    _bump(audit, "superseded_connections")
                active_epoch = epoch
                yield {
                    "kind": "deribit_connection", "recv_ms": receive_ms,
                    "source_ts_ms": None, "connection_epoch": epoch,
                    "stream_sequence": stream_sequence, "stream": "deribit",
                }
                stream_sequence += 1
                _bump(audit, "connections")
            elif kind == "disconnect":
                if active_epoch is None:
                    _bump(audit, "inactive_disconnects")
                    continue
                if active_epoch != epoch:
                    _bump(audit, "stale_disconnects")
                    continue
                yield {
                    "kind": "deribit_disconnect", "recv_ms": receive_ms,
                    "source_ts_ms": None, "connection_epoch": epoch,
                    "stream_sequence": stream_sequence, "stream": "deribit",
                }
                stream_sequence += 1
                active_epoch = None
                _bump(audit, "disconnects")
            else:
                raise ValueError(f"unknown DVOL recorder lifecycle kind: {kind}")
            continue
        if active_epoch is None:
            key = "leading_carryover_frames" if stream_sequence == 0 else "outside_epoch_frames"
            _bump(audit, key)
            continue
        params = payload.get("params")
        if not isinstance(params, Mapping) or params.get("channel") != CHANNEL:
            _bump(audit, "irrelevant_frames")
            continue
        data = params.get("data")
        if not isinstance(data, Mapping):
            raise ValueError("DVOL subscription frame has no data object")
        try:
            source_ms = float(data["timestamp"])
            volatility = float(data["volatility"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("malformed DVOL subscription frame") from exc
        if not (
            math.isfinite(source_ms) and math.isfinite(volatility) and volatility > 0
        ):
            raise ValueError("invalid DVOL subscription value")
        yield {
            "kind": "deribit_dvol", "recv_ms": receive_ms,
            "source_ts_ms": source_ms, "volatility": volatility,
            "index_name": "btc_usd", "connection_epoch": active_epoch,
            "stream_sequence": stream_sequence, "stream": "deribit",
        }
        stream_sequence += 1
        _bump(audit, "dvol_events")
    audit["skipped_irrelevant_frames"] = stats["skipped_irrelevant_frames"]
    audit["open_epoch_at_eof"] = int(active_epoch is not None)


def _apply_event(tracker: mix.ReceiptDvolRv, event: Mapping[str, Any]) -> None:
    kind = str(event["kind"])
    receive_ms = float(event["recv_ms"])
    if kind in {"spot_connection", "spot_disconnect"}:
        tracker.reset_spot()
    elif kind == "spot_trade":
        tracker.update_spot(receive_ms, float(event["price"]))
    elif kind in {"deribit_connection", "deribit_disconnect"}:
        tracker.reset_dvol_current()
    elif kind == "deribit_dvol":
        tracker.update_dvol(
            receive_ms, float(event["source_ts_ms"]), float(event["volatility"]),
        )


def enrich_rows(
    rows: Sequence[Mapping[str, Any]],
    aggregate_tape_path: str | Path,
    dvol_paths: Iterable[str | Path],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fill dvol_rv using only source frames received by each signal timestamp."""
    diagnostics: dict[str, int] = {}
    spot = mix.iter_aggregate_spot_events(
        Path(aggregate_tape_path).parent,
        aggregate_tape_path=aggregate_tape_path,
    )
    dvol = iter_dvol_events(dvol_paths, diagnostics=diagnostics)
    events = heapq.merge(
        ((float(event["recv_ms"]), 0, index, event) for index, event in enumerate(spot)),
        ((float(event["recv_ms"]), 1, index, event) for index, event in enumerate(dvol)),
        key=lambda item: item[:3],
    )
    event_iter = iter(events)
    next_event = next(event_iter, None)
    tracker = mix.ReceiptDvolRv()
    result = [dict(row) for row in rows]
    order = sorted(range(len(result)), key=lambda index: (
        float(result[index]["signal_receive_ms"]), index,
    ))
    populated = 0
    for index in order:
        signal_ms = float(result[index]["signal_receive_ms"])
        while next_event is not None and next_event[0] <= signal_ms + 1e-9:
            if next_event[0] > signal_ms + 1e-9:
                raise AssertionError("future DVOL frame reached the current signal")
            _apply_event(tracker, next_event[3])
            next_event = next(event_iter, None)
        value = tracker.value(signal_ms)
        aux = dict(result[index].get("model_features_aux") or {})
        prior = aux.get("dvol_rv")
        replacement = value if math.isfinite(value) else None
        if prior is not None and replacement is not None and not math.isclose(
            float(prior), replacement, rel_tol=0.0, abs_tol=1e-12,
        ):
            raise ValueError("existing MIX dvol_rv differs from receipt-causal reconstruction")
        if prior is None:
            aux["dvol_rv"] = replacement
        if aux.get("dvol_rv") is not None:
            populated += 1
        result[index]["model_features_aux"] = aux
    return result, {
        "schema": "mix-dvol-enrichment-v1",
        "target_rows": len(result),
        "populated_rows": populated,
        "missing_rows": len(result) - populated,
        "future_dvol_frames_consumed": 0,
        "dvol_diagnostics": diagnostics,
    }


def enrich_ledger_rows(
    rows: Sequence[Mapping[str, Any]],
    day: str,
    aggregate_tape_path: str | Path,
    dvol_paths: Iterable[str | Path],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Enrich only one newly admitted UTC day, preserving all earlier rows byte-semantically."""
    target_day = f"{day[:4]}-{day[4:6]}-{day[6:]}"
    result = [dict(row) for row in rows]
    indices = [index for index, row in enumerate(result) if row.get("day") == target_day]
    selected = [result[index] for index in indices]
    enriched, profile = enrich_rows(selected, aggregate_tape_path, dvol_paths)
    for index, row in zip(indices, enriched, strict=True):
        result[index] = row
    profile.update({"day": day, "ledger_rows": len(result)})
    return result, profile
