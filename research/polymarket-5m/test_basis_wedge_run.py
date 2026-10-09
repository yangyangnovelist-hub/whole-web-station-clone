from __future__ import annotations

import csv
import gzip
import hashlib
import importlib
import importlib.util
import json
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import pytest

import basis_wedge as wedge
import basis_wedge_run as run


ROOT = Path(__file__).parent


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_strict_replay_adapter_exposes_a_normalized_entrypoint() -> None:
    assert importlib.util.find_spec("basis_wedge_run") is not None
    module = importlib.import_module("basis_wedge_run")
    assert callable(module.replay_normalized)


def test_frozen_protocol_matches_detector_and_replay_constants() -> None:
    freeze_path = ROOT / "forward" / "basis-wedge-freeze.json"
    frozen = json.loads(freeze_path.read_text())
    expected_sha = (ROOT / "forward" / "basis-wedge-freeze.sha256").read_text().strip()
    config = wedge.DetectorConfig()

    assert _sha256(freeze_path) == expected_sha
    assert frozen["holdout_start"] == "2026-10-10T00:00:00Z"
    assert frozen["dependencies_sha256"]["basis_wedge.py"] == _sha256(
        ROOT / "basis_wedge.py"
    )
    assert frozen["signal"]["window_ms"] == config.window_ms
    assert frozen["signal"]["minimum_absolute_basis_move_bp"] == config.min_basis_move_bp
    assert frozen["signal"]["maximum_absolute_spot_move_bp"] == config.max_spot_move_bp
    assert frozen["signal"]["maximum_absolute_deribit_move_bp"] == config.max_deribit_move_bp
    assert frozen["signal"]["minimum_polymarket_follow_move"] == config.min_pm_move
    assert frozen["execution"]["evaluation_ms"] == list(run.EVALUATION_MS)
    assert frozen["execution"]["minimum_full_fill_shares"] == run.TARGET_SHARES
    assert frozen["execution"]["taker_fee_rate"] == config.fee_rate


BASE_MS = 180_000.0


def _source_events(*, tail_ms: float = BASE_MS + 1_000.0) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = [
        {"kind": "spot_connection", "recv_ms": BASE_MS - 100.0},
        {"kind": "futures_connection", "recv_ms": BASE_MS - 99.0},
        {"kind": "deribit_connection", "recv_ms": BASE_MS - 98.0},
        {"kind": "spot_bbo", "recv_ms": BASE_MS, "bid": 99.995, "ask": 100.005},
        {"kind": "futures_bbo", "recv_ms": BASE_MS, "bid": 99.995, "ask": 100.005},
        {"kind": "deribit_quote", "recv_ms": BASE_MS, "bid": 99.995, "ask": 100.005},
        {"kind": "futures_bbo", "recv_ms": BASE_MS + 200.0,
         "bid": 100.015, "ask": 100.025},
        {"kind": "spot_bbo", "recv_ms": BASE_MS + 210.0,
         "bid": 99.995, "ask": 100.005},
        {"kind": "deribit_quote", "recv_ms": BASE_MS + 220.0,
         "bid": 99.995, "ask": 100.005},
        {"kind": "spot_bbo", "recv_ms": tail_ms, "bid": 99.995, "ask": 100.005},
    ]
    for sequence, row in enumerate(rows):
        row.update({"seq": sequence, "source_ts_ms": row.get("recv_ms")})
    return rows


def _snapshot(
    recv_ms: float,
    token: str,
    bids: tuple[tuple[float, float], ...],
    asks: tuple[tuple[float, float], ...],
) -> dict[str, object]:
    return {
        "kind": "clob_snapshot",
        "recv_ms": recv_ms,
        "source_ts_ms": recv_ms,
        "market_id": "m",
        "asset_id": token,
        "bids": [{"price": price, "size": size} for price, size in bids],
        "asks": [{"price": price, "size": size} for price, size in asks],
    }


def _book_pair(
    recv_ms: float,
    *,
    up_bids: tuple[tuple[float, float], ...],
    up_asks: tuple[tuple[float, float], ...],
    down_bids: tuple[tuple[float, float], ...],
    down_asks: tuple[tuple[float, float], ...],
) -> list[dict[str, object]]:
    return [
        _snapshot(recv_ms, "up", up_bids, up_asks),
        _snapshot(recv_ms, "down", down_bids, down_asks),
    ]


def _clob_events(*updates: dict[str, object]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = [{
        "kind": "clob_connection",
        "recv_ms": BASE_MS - 97.0,
        "source_ts_ms": None,
    }]
    rows += _book_pair(
        BASE_MS,
        up_bids=((0.49, 10.0),), up_asks=((0.51, 10.0),),
        down_bids=((0.49, 10.0),), down_asks=((0.51, 10.0),),
    )
    rows += _book_pair(
        BASE_MS + 300.0,
        up_bids=((0.52, 10.0),), up_asks=((0.54, 10.0),),
        down_bids=((0.43, 10.0),), down_asks=((0.44, 1.0), (0.45, 10.0)),
    )
    rows += list(updates)
    rows.sort(key=lambda row: float(row["recv_ms"]))
    for sequence, row in enumerate(rows):
        row["seq"] = sequence
    return rows


def _mapping() -> list[dict[str, object]]:
    return [{
        "market_id": "m",
        "slot": 0,
        "up_token_id": "up",
        "down_token_id": "down",
        "fee_rate": 0.07,
    }]


def _replay(
    clob: list[dict[str, object]],
    *,
    source: list[dict[str, object]] | None = None,
    winner: str = "Down",
) -> tuple[list[dict[str, object]], dict[str, int]]:
    return run.replay_normalized(
        _source_events() if source is None else source,
        clob,
        _mapping(),
        {"m": winner},
        detector_config=wedge.DetectorConfig(),
    )


def test_exact_due_book_is_used_but_a_book_one_millisecond_later_is_not() -> None:
    at_due = _book_pair(
        BASE_MS + 700.0,
        up_bids=((0.52, 10.0),), up_asks=((0.54, 10.0),),
        down_bids=((0.43, 10.0),), down_asks=((0.44, 5.0),),
    )
    after_due = _book_pair(
        BASE_MS + 701.0,
        up_bids=((0.52, 10.0),), up_asks=((0.54, 10.0),),
        down_bids=((0.43, 10.0),), down_asks=((0.42, 5.0),),
    )

    rows, _ = _replay(_clob_events(*at_due, *after_due))
    by_latency = {int(row["evaluation_ms"]): row for row in rows}

    assert by_latency[400]["filled"] is True
    assert by_latency[400]["fill_vwap"] == pytest.approx(0.44)
    assert by_latency[400]["book_recv_ms"] == BASE_MS + 700.0
    assert by_latency[500]["filled"] is True
    assert by_latency[500]["fill_vwap"] == pytest.approx(0.42)
    assert by_latency[500]["book_recv_ms"] == BASE_MS + 701.0


def test_last_source_confirmation_sends_without_waiting_for_another_clob_frame() -> None:
    clob = _clob_events()
    clob = [row for row in clob if float(row["recv_ms"]) != BASE_MS + 300.0]
    clob += _book_pair(
        BASE_MS + 205.0,
        up_bids=((0.52, 10.0),), up_asks=((0.54, 10.0),),
        down_bids=((0.43, 10.0),), down_asks=((0.44, 5.0),),
    )
    clob.sort(key=lambda row: float(row["recv_ms"]))
    for sequence, row in enumerate(clob):
        row["seq"] = sequence

    rows, _ = _replay(clob)

    assert len(rows) == 2
    assert {row["signal_recv_ms"] for row in rows} == {BASE_MS + 220.0}


def test_future_book_cannot_rescue_the_400ms_evaluation() -> None:
    unavailable = _book_pair(
        BASE_MS + 650.0,
        up_bids=((0.52, 10.0),), up_asks=((0.54, 10.0),),
        down_bids=((0.43, 10.0),), down_asks=((0.60, 10.0),),
    )
    future = _book_pair(
        BASE_MS + 701.0,
        up_bids=((0.52, 10.0),), up_asks=((0.54, 10.0),),
        down_bids=((0.43, 10.0),), down_asks=((0.42, 5.0),),
    )

    rows, counters = _replay(_clob_events(*unavailable, *future))
    by_latency = {int(row["evaluation_ms"]): row for row in rows}

    assert by_latency[400]["filled"] is False
    assert by_latency[400]["reason"] == "ask_above_frozen_limit"
    assert by_latency[400]["book_recv_ms"] == BASE_MS + 650.0
    assert by_latency[500]["filled"] is True
    assert counters["reason_ask_above_frozen_limit"] == 1


def test_source_disconnect_invalidates_both_pending_latency_branches() -> None:
    source = _source_events()
    source.insert(-1, {
        "kind": "deribit_disconnect",
        "recv_ms": BASE_MS + 600.0,
        "source_ts_ms": None,
    })
    source.sort(key=lambda row: float(row["recv_ms"]))
    for sequence, row in enumerate(source):
        row["seq"] = sequence

    rows, counters = _replay(_clob_events(), source=source)

    assert len(rows) == 2
    assert {row["reason"] for row in rows} == {"disconnect"}
    assert not any(row["filled"] for row in rows)
    assert counters["fail_closed_resets"] >= 1
    assert counters["reason_disconnect"] == 2


def test_direct_only_execution_does_not_mirror_the_opposite_bid() -> None:
    # Up bid 0.56 would imply a synthetic Down ask of 0.44.  Direct Down asks
    # are all above the signal's frozen 0.45 limit and must not fill.
    direct_expensive = _book_pair(
        BASE_MS + 650.0,
        up_bids=((0.56, 20.0),), up_asks=((0.58, 20.0),),
        down_bids=((0.40, 20.0),), down_asks=((0.60, 20.0),),
    )

    rows, _ = _replay(_clob_events(*direct_expensive))

    assert len(rows) == 2
    assert {row["reason"] for row in rows} == {"ask_above_frozen_limit"}
    assert not any(row["filled"] for row in rows)


def test_exact_five_share_vwap_real_fee_and_settlement_pnl() -> None:
    depth = _book_pair(
        BASE_MS + 650.0,
        up_bids=((0.52, 10.0),), up_asks=((0.54, 10.0),),
        down_bids=((0.42, 10.0),), down_asks=((0.43, 2.0), (0.44, 4.0)),
    )

    rows, counters = _replay(_clob_events(*depth))
    row = next(item for item in rows if item["evaluation_ms"] == 400.0)
    notional = 2.0 * 0.43 + 3.0 * 0.44
    raw_fee = (
        Decimal("2") * Decimal("0.07") * Decimal("0.43") * Decimal("0.57")
        + Decimal("3") * Decimal("0.07") * Decimal("0.44") * Decimal("0.56")
    )
    rounded_fee = float(raw_fee.quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP))

    assert row["filled"] is True
    assert row["filled_shares"] == 5.0
    assert row["fill_vwap"] == pytest.approx(notional / 5.0)
    assert row["fee"] == pytest.approx(rounded_fee)
    assert row["total_cost"] == pytest.approx(notional + rounded_fee)
    assert row["all_in_cost"] == pytest.approx((notional + rounded_fee) / 5.0)
    assert row["won"] is True
    assert row["pnl"] == pytest.approx(5.0 - notional - rounded_fee)
    assert row["pnl_per_share"] == pytest.approx(row["pnl"] / 5.0)
    assert counters["fills_400ms"] == 1


def test_fak_partial_fill_is_scored_but_not_counted_as_a_full_fill() -> None:
    partial = _book_pair(
        BASE_MS + 650.0,
        up_bids=((0.52, 10.0),), up_asks=((0.54, 10.0),),
        down_bids=((0.42, 10.0),), down_asks=((0.44, 2.0),),
    )

    rows, counters = _replay(_clob_events(*partial))
    row = next(item for item in rows if item["evaluation_ms"] == 400.0)

    assert row["reason"] == "partial_fill"
    assert row["filled"] is True
    assert row["full_fill"] is False
    assert row["filled_shares"] == 2.0
    assert row["pnl"] is not None
    assert counters["partial_fills"] >= 1


def test_clob_reconnect_rejects_price_changes_until_both_direct_snapshots_arrive() -> None:
    clob = [{
        "kind": "clob_connection", "recv_ms": BASE_MS - 97.0, "source_ts_ms": None,
    }]
    clob += _book_pair(
        BASE_MS,
        up_bids=((0.49, 10.0),), up_asks=((0.51, 10.0),),
        down_bids=((0.49, 10.0),), down_asks=((0.51, 10.0),),
    )
    clob += [
        {
            "kind": "clob_error", "recv_ms": BASE_MS + 240.0,
            "source_ts_ms": None, "error": "closed",
        },
        {
            "kind": "clob_connection", "recv_ms": BASE_MS + 250.0,
            "source_ts_ms": None,
        },
        {
            "kind": "clob_price_change", "recv_ms": BASE_MS + 300.0,
            "source_ts_ms": BASE_MS + 300.0, "market_id": "m", "asset_id": "up",
            "side": "SELL", "price": 0.54, "size": 10.0,
        },
        {
            "kind": "clob_price_change", "recv_ms": BASE_MS + 300.0,
            "source_ts_ms": BASE_MS + 300.0, "market_id": "m", "asset_id": "down",
            "side": "SELL", "price": 0.45, "size": 10.0,
        },
    ]
    clob.sort(key=lambda row: float(row["recv_ms"]))
    for sequence, row in enumerate(clob):
        row["seq"] = sequence
    source = _source_events()
    source += [
        {"kind": "spot_bbo", "recv_ms": BASE_MS + 260.0,
         "bid": 99.995, "ask": 100.005},
        {"kind": "futures_bbo", "recv_ms": BASE_MS + 261.0,
         "bid": 100.015, "ask": 100.025},
        {"kind": "deribit_quote", "recv_ms": BASE_MS + 262.0,
         "bid": 99.995, "ask": 100.005},
    ]
    source.sort(key=lambda row: float(row["recv_ms"]))
    for sequence, row in enumerate(source):
        row.update({"seq": sequence, "source_ts_ms": row.get("recv_ms")})

    rows, counters = _replay(clob, source=source)

    assert rows == []
    assert counters["clob_updates_not_applied"] >= 2


def _strict_source_rows() -> list[dict[str, object]]:
    rows = _source_events()
    rows += [
        {"kind": "spot_disconnect", "recv_ms": BASE_MS + 1_100.0,
         "source_ts_ms": None},
        {"kind": "futures_disconnect", "recv_ms": BASE_MS + 1_101.0,
         "source_ts_ms": None},
        {"kind": "deribit_disconnect", "recv_ms": BASE_MS + 1_102.0,
         "source_ts_ms": None},
    ]
    rows.sort(key=lambda row: float(row["recv_ms"]))
    stream_sequence = Counter()
    for sequence, row in enumerate(rows):
        kind = str(row["kind"])
        stream = next(name for name in ("spot", "futures", "deribit") if kind.startswith(name))
        row.update({
            "seq": sequence,
            "stream": stream,
            "stream_sequence": stream_sequence[stream],
            "connection_epoch": 1,
        })
        stream_sequence[stream] += 1
        if kind.endswith(("_connection", "_disconnect")):
            row["source_ts_ms"] = None
    return rows


def _strict_clob_rows() -> list[dict[str, object]]:
    depth = _book_pair(
        BASE_MS + 650.0,
        up_bids=((0.52, 10.0),), up_asks=((0.54, 10.0),),
        down_bids=((0.42, 10.0),), down_asks=((0.43, 2.0), (0.44, 4.0)),
    )
    rows = _clob_events(*depth)
    rows.append({
        "kind": "clob_error", "recv_ms": BASE_MS + 1_103.0,
        "source_ts_ms": None, "error": "closed",
    })
    rows.sort(key=lambda row: float(row["recv_ms"]))
    for sequence, row in enumerate(rows):
        row.update({"seq": sequence, "connection_epoch": 1})
    return rows


def test_standard_strict_artifact_entrypoint_streams_records_and_counters(tmp_path) -> None:
    strict = tmp_path / "strict"
    strict.mkdir()
    source_rows = _strict_source_rows()
    clob_rows = _strict_clob_rows()
    with gzip.open(strict / "source_events.jsonl.gz", "wt") as stream:
        stream.writelines(json.dumps(row) + "\n" for row in source_rows)
    with gzip.open(strict / "clob_events.jsonl.gz", "wt") as stream:
        stream.writelines(json.dumps(row) + "\n" for row in clob_rows)
    with gzip.open(strict / "market_registry.csv.gz", "wt", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=[
            "market_id", "start_ts", "up_token_id", "down_token_id", "updated_at",
            "fee_rate",
        ])
        writer.writeheader()
        writer.writerow({
            "market_id": "m", "start_ts": 0, "up_token_id": "up",
            "down_token_id": "down", "updated_at": 0, "fee_rate": 0.07,
        })
    with gzip.open(strict / "market_outcomes.csv.gz", "wt", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=[
            "market_id", "winner", "resolution_ts", "source", "recorded_at",
        ])
        writer.writeheader()
        writer.writerow({
            "market_id": "m", "winner": "Down", "resolution_ts": 300,
            "source": "gamma", "recorded_at": 301,
        })
    counts = Counter(str(row["kind"]) for row in source_rows + clob_rows)
    counts.update({"markets": 1, "outcomes": 1})
    (strict / "manifest.json").write_text(json.dumps({
        "schema": "polymarket-5m-strict-replay-v2",
        "complete": True,
        "run_id": "basis-wedge-test",
        "collector_region": "eu-west-1",
        "recorder_complete": True,
        "missing_resolved_market_ids": [],
        "counts": dict(counts),
    }))
    rows_out = tmp_path / "basis-wedge-rows.jsonl"

    result = run.replay_archive(tmp_path, rows_out=rows_out)
    persisted = [json.loads(line) for line in rows_out.read_text().splitlines()]

    assert result["dataset"]["paper_only"] is True
    assert result["dataset"]["sample_scope"] == "standard_forward_artifact"
    assert len(result["records"]) == 2
    assert persisted == result["records"]
    assert result["counters"]["signals"] == 1
    assert result["latencies"]["400"]["fills"] == 1
    assert result["latencies"]["500"]["fills"] == 1
