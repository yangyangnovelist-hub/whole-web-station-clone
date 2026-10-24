from __future__ import annotations

import gzip
import json
import math
from pathlib import Path

import mix_dvol


def _raw(path: Path, rows: list[tuple[int, dict]]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        for receive_ns, payload in rows:
            stream.write(f"{receive_ns}\t{json.dumps(payload, separators=(',', ':'))}\n")


def _marker(kind: str, epoch: int) -> dict:
    return {"_recorder": {"schema": "recorder-lifecycle-v1", "kind": kind, "epoch": epoch}}


def _dvol(source_ms: int, value: float) -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "subscription",
        "params": {
            "channel": "deribit_volatility_index.btc_usd",
            "data": {"timestamp": source_ms, "volatility": value, "index_name": "btc_usd"},
        },
    }


def test_dvol_adapter_is_lifecycle_and_receipt_causal(tmp_path):
    path = tmp_path / "deribit_vol.20261010T00.txt.gz"
    _raw(path, [
        (1_000_000, _dvol(1, 49.0)),
        (1_500_000, _marker("disconnect", 6)),
        (2_000_000, _marker("connection", 7)),
        (3_000_000, _dvol(2, 50.0)),
        (4_000_000, _marker("disconnect", 7)),
        (5_000_000, _dvol(3, 99.0)),
    ])
    diagnostics: dict[str, int] = {}

    events = list(mix_dvol.iter_dvol_events([path], diagnostics=diagnostics))

    assert [row["kind"] for row in events] == [
        "deribit_connection", "deribit_dvol", "deribit_disconnect",
    ]
    assert events[1]["recv_ms"] == 3.0
    assert events[1]["source_ts_ms"] == 2.0
    assert events[1]["volatility"] == 50.0
    assert diagnostics["leading_carryover_frames"] == 1
    assert diagnostics["inactive_disconnects"] == 1
    assert diagnostics["dvol_events"] == 1
    assert diagnostics["outside_epoch_frames"] == 1


def test_dvol_adapter_closes_an_epoch_when_a_new_connection_supersedes_it(tmp_path):
    path = tmp_path / "deribit_vol.20261010T00.txt.gz"
    _raw(path, [
        (1_000_000, _marker("connection", 7)),
        (2_000_000, _dvol(1, 50.0)),
        (3_000_000, _marker("connection", 8)),
        (4_000_000, _dvol(2, 51.0)),
    ])
    diagnostics: dict[str, int] = {}

    events = list(mix_dvol.iter_dvol_events([path], diagnostics=diagnostics))

    assert [row["kind"] for row in events] == [
        "deribit_connection", "deribit_dvol", "deribit_disconnect",
        "deribit_connection", "deribit_dvol",
    ]
    assert events[2]["error"] == "superseded_connection"
    assert diagnostics["superseded_connections"] == 1


def test_enrichment_uses_only_dvol_received_before_each_signal(tmp_path):
    base_ms = 3_600_000
    raw = tmp_path / "deribit_vol.20261010T00.txt.gz"
    _raw(raw, [
        ((base_ms - 3_000) * 1_000_000, _marker("connection", 1)),
        ((base_ms - 2_000) * 1_000_000, _dvol(1_000, 50.0)),
        ((base_ms + 100) * 1_000_000, _dvol(base_ms + 1, 51.0)),
    ])
    tape = tmp_path / "aggregate.jsonl.gz"
    with gzip.open(tape, "wt", encoding="utf-8") as stream:
        for index in range(903):
            receive_ms = base_ms - 901_000 + index * 1_000
            stream.write(json.dumps({
                "event": "BINANCE_WS_TRADE",
                "receive_ts": receive_ms / 1_000.0,
                "trade_ts": (receive_ms - 10) / 1_000.0,
                "price": 80_000.0 + (index % 2),
            }, separators=(",", ":")) + "\n")
    rows = [
        {"day": "1970-01-01", "signal_receive_ms": base_ms + 50,
         "model_features_aux": {"dvol_rv": None}},
        {"day": "1970-01-01", "signal_receive_ms": base_ms + 500,
         "model_features_aux": {"dvol_rv": None}},
    ]

    enriched, profile = mix_dvol.enrich_rows(rows, tape, [raw])

    assert enriched[0]["model_features_aux"]["dvol_rv"] is None
    value = enriched[1]["model_features_aux"]["dvol_rv"]
    assert value is not None and math.isfinite(value)
    assert profile["target_rows"] == 2
    assert profile["populated_rows"] == 1
    assert profile["future_dvol_frames_consumed"] == 0
