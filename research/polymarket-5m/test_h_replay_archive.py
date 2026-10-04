import csv
import gzip
import importlib
import json
import shutil
import subprocess

import pytest


def _write_jsonl(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _write_csv_gz(path, fieldnames, rows):
    with gzip.open(path, "wt", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_jsonl_gz(path, rows):
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        stream.writelines(json.dumps(row) + "\n" for row in rows)


def test_spot_events_preserve_null_source_time_and_receipt_order(tmp_path):
    archive = importlib.import_module("h_replay_archive")
    path = tmp_path / "feeds.jsonl"
    _write_jsonl(path, [
        {"s": "binance_spot_bbo", "r": 1_234_567_890, "e": None, "b": 80_000.25, "a": 80_000.50},
        {"s": "coinbase", "r": 1_234_600_000, "e": 1_100, "p": 80_001.0, "q": 0.1},
        {"s": "binance_spot", "r": 1_234_667_890, "e": 1_200, "p": 80_000.50, "q": 0.02},
    ])

    events = list(archive.iter_spot_events(path))

    assert [event["kind"] for event in events] == ["spot_bbo", "spot_trade"]
    assert [event["seq"] for event in events] == [0, 1]
    assert events[0] == {
        "kind": "spot_bbo",
        "recv_ms": pytest.approx(1_234.56789),
        "seq": 0,
        "source_ts_ms": None,
        "bid": 80_000.25,
        "ask": 80_000.50,
    }
    assert events[1] == {
        "kind": "spot_trade",
        "recv_ms": pytest.approx(1_234.66789),
        "seq": 1,
        "source_ts_ms": 1_200,
        "price": 80_000.50,
        "size": 0.02,
    }


def test_spot_events_stream_a_zstd_fixture(tmp_path):
    archive = importlib.import_module("h_replay_archive")
    zstd = shutil.which("zstd")
    if zstd is None:
        pytest.skip("zstd CLI is unavailable")
    path = tmp_path / "feeds.jsonl.zst"
    payload = json.dumps({
        "s": "binance_spot",
        "r": 2_000_250_000,
        "e": 1_999,
        "p": 80_123.5,
        "q": 0.125,
    }) + "\n"
    subprocess.run([zstd, "-q", "-f", "-o", str(path)], input=payload, text=True, check=True)

    assert list(archive.iter_spot_events(path)) == [{
        "kind": "spot_trade",
        "recv_ms": 2_000.25,
        "seq": 0,
        "source_ts_ms": 1_999,
        "price": 80_123.5,
        "size": 0.125,
    }]


def test_normalized_strict_stream_validates_family_sequence_and_epoch(tmp_path):
    archive = importlib.import_module("h_replay_archive")
    spot = tmp_path / "spot.jsonl.gz"
    _write_jsonl_gz(spot, [
        {"kind": "spot_trade", "recv_ms": 1000.25, "source_ts_ms": 999, "seq": 0,
         "price": 80000.0, "size": 0.01},
        {"kind": "spot_bbo", "recv_ms": 1000.5, "source_ts_ms": None, "seq": 1,
         "bid": 79999.0, "ask": 80001.0},
    ])
    assert [row["kind"] for row in archive.iter_normalized_events(spot, family="spot")] == [
        "spot_trade", "spot_bbo",
    ]

    clob = tmp_path / "clob.jsonl.gz"
    _write_jsonl_gz(clob, [{
        "kind": "clob_connection", "recv_ms": 1000.0, "source_ts_ms": None,
        "seq": 0, "connection_epoch": 1, "token_count": 2,
    }])
    assert next(archive.iter_normalized_events(clob, family="clob"))["connection_epoch"] == 1
    with pytest.raises(archive.ArchiveFormatError, match="still open"):
        list(archive.iter_normalized_events(clob, family="clob"))

    _write_jsonl_gz(clob, [{
        "kind": "clob_connection", "recv_ms": 1000.0, "source_ts_ms": None, "seq": 0,
    }])
    with pytest.raises(archive.ArchiveFormatError, match="connection_epoch"):
        list(archive.iter_normalized_events(clob, family="clob"))


def test_clob_snapshots_use_outer_receive_order_when_source_time_inverts(tmp_path):
    archive = importlib.import_module("h_replay_archive")
    path = tmp_path / "clob.jsonl"
    _write_jsonl(path, [
        {"recv_ms": 2_000, "msg": [{
            "event_type": "book",
            "market": "m1",
            "asset_id": "up-token",
            "timestamp": "5000",
            "bids": [{"price": "0.40", "size": "12"}],
            "asks": [{"price": "0.60", "size": "8"}],
            "tick_size": "0.01",
        }]},
        {"recv_ms": 2_001, "msg": [{
            "event_type": "book",
            "market": "m1",
            "asset_id": "down-token",
            "timestamp": "4000",
            "bids": [{"price": "0.39", "size": "7"}],
            "asks": [{"price": "0.61", "size": "9"}],
        }]},
    ])

    events = list(archive.iter_clob_events([path]))

    assert [(event["recv_ms"], event["source_ts_ms"]) for event in events] == [
        (2_000, 5_000),
        (2_001, 4_000),
    ]
    assert [event["seq"] for event in events] == [0, 1]
    assert events[0] == {
        "kind": "clob_snapshot",
        "recv_ms": 2_000,
        "seq": 0,
        "source_ts_ms": 5_000,
        "market_id": "m1",
        "asset_id": "up-token",
        "bids": [{"price": "0.40", "size": "12"}],
        "asks": [{"price": "0.60", "size": "8"}],
        "tick_size": "0.01",
    }


def test_two_token_price_change_emits_one_direct_event_per_asset(tmp_path):
    archive = importlib.import_module("h_replay_archive")
    path = tmp_path / "clob.jsonl"
    _write_jsonl(path, [{
        "recv_ms": 3_050,
        "msg": [{
            "event_type": "price_change",
            "market": "m2",
            "timestamp": "3001",
            "price_changes": [
                {
                    "asset_id": "up-token",
                    "price": "0.55",
                    "size": "10",
                    "side": "BUY",
                    "best_bid": "0.55",
                    "best_ask": "0.56",
                },
                {
                    "asset_id": "down-token",
                    "price": "0.45",
                    "size": "10",
                    "side": "SELL",
                    "best_bid": "0.44",
                    "best_ask": "0.45",
                },
            ],
        }],
    }])

    events = list(archive.iter_clob_events([path]))

    assert events == [
        {
            "kind": "clob_price_change",
            "recv_ms": 3_050,
            "seq": 0,
            "source_ts_ms": 3_001,
            "market_id": "m2",
            "asset_id": "up-token",
            "price": "0.55",
            "size": "10",
            "side": "BUY",
            "best_bid": "0.55",
            "best_ask": "0.56",
        },
        {
            "kind": "clob_price_change",
            "recv_ms": 3_050,
            "seq": 1,
            "source_ts_ms": 3_001,
            "market_id": "m2",
            "asset_id": "down-token",
            "price": "0.45",
            "size": "10",
            "side": "SELL",
            "best_bid": "0.44",
            "best_ask": "0.45",
        },
    ]


def test_clob_files_merge_by_receive_time_with_deterministic_ties(tmp_path):
    archive = importlib.import_module("h_replay_archive")
    a = tmp_path / "clob-a.jsonl"
    b = tmp_path / "clob-b.jsonl"

    def snapshot(recv_ms, asset_id):
        return {"recv_ms": recv_ms, "msg": [{
            "event_type": "book",
            "market": "m",
            "asset_id": asset_id,
            "timestamp": str(recv_ms - 10),
            "bids": [],
            "asks": [],
        }]}

    _write_jsonl(a, [snapshot(1_000, "a-early"), snapshot(3_000, "a-tie")])
    _write_jsonl(b, [snapshot(2_000, "b-middle"), snapshot(3_000, "b-tie")])

    events = list(archive.iter_clob_events([b, a]))

    assert [event["asset_id"] for event in events] == ["a-early", "b-middle", "a-tie", "b-tie"]
    assert [event["recv_ms"] for event in events] == [1_000, 2_000, 3_000, 3_000]
    assert [event["seq"] for event in events] == [0, 1, 2, 3]


def test_clob_stream_includes_connection_and_error_epoch_inputs(tmp_path):
    archive = importlib.import_module("h_replay_archive")
    clob = tmp_path / "clob.jsonl"
    errors = tmp_path / "errors.jsonl"
    _write_jsonl(clob, [{"recv_ms": 1_000, "msg": [{
        "event_type": "book",
        "market": "m",
        "asset_id": "token",
        "timestamp": "990",
        "bids": [],
        "asks": [],
    }]}])
    _write_jsonl(errors, [
        {"at": 900, "where": "clob-open", "tokens": 4},
        {"at": 1_100, "where": "clob", "err": "ConnectionClosedError(1013)"},
        {"at": 1_200, "where": "discover", "err": "not a CLOB epoch boundary"},
    ])

    events = list(archive.iter_clob_events([clob], error_paths=[errors]))

    assert events == [
        {
            "kind": "clob_connection",
            "recv_ms": 900,
            "seq": 0,
            "source_ts_ms": None,
            "token_count": 4,
        },
        {
            "kind": "clob_snapshot",
            "recv_ms": 1_000,
            "seq": 1,
            "source_ts_ms": 990,
            "market_id": "m",
            "asset_id": "token",
            "bids": [],
            "asks": [],
        },
        {
            "kind": "clob_error",
            "recv_ms": 1_100,
            "seq": 2,
            "source_ts_ms": None,
            "error": "ConnectionClosedError(1013)",
        },
    ]


def test_malformed_jsonl_boundary_is_rejected_with_location(tmp_path):
    archive = importlib.import_module("h_replay_archive")
    path = tmp_path / "clob.jsonl"
    path.write_text(
        json.dumps({"recv_ms": 1_000, "msg": [{
            "event_type": "book",
            "market": "m",
            "asset_id": "token",
            "timestamp": "990",
            "bids": [],
            "asks": [],
        }]}) + "\n" + '{"recv_ms":1001,"msg":' + "\n",
        encoding="utf-8",
    )
    events = archive.iter_clob_events([path])

    assert next(events)["asset_id"] == "token"
    with pytest.raises(archive.ArchiveFormatError, match=r"clob\.jsonl:2: invalid JSON"):
        next(events)


def test_market_registry_streams_direct_up_and_down_token_mapping(tmp_path):
    archive = importlib.import_module("h_replay_archive")
    path = tmp_path / "market_registry.csv.gz"
    _write_csv_gz(
        path,
        ["market_id", "start_ts", "up_token_id", "down_token_id", "updated_at"],
        [{
            "market_id": "0xmarket",
            "start_ts": "1790935800",
            "up_token_id": "up-direct",
            "down_token_id": "down-direct",
            "updated_at": "1790935700.125",
        }],
    )

    assert list(archive.iter_market_mappings(path)) == [{
        "kind": "market_mapping",
        "recv_ms": 1_790_935_700_125.0,
        "seq": 0,
        "source_ts_ms": None,
        "market_id": "0xmarket",
        "slot": 1_790_935_800,
        "up_token_id": "up-direct",
        "down_token_id": "down-direct",
    }]


def test_gamma_market_mapping_follows_outcome_labels_not_token_position(tmp_path):
    archive = importlib.import_module("h_replay_archive")
    path = tmp_path / "markets.jsonl"
    _write_jsonl(path, [{
        "conditionId": "0xgamma",
        "slug": "btc-updown-5m-1790936100",
        "outcomes": '["Down", "Up"]',
        "clobTokenIds": '["down-direct", "up-direct"]',
        "updatedAt": "1970-01-01T00:00:01.250Z",
    }])

    assert list(archive.iter_market_mappings(path)) == [{
        "kind": "market_mapping",
        "recv_ms": None,
        "seq": 0,
        "source_ts_ms": 1_250.0,
        "market_id": "0xgamma",
        "slot": 1_790_936_100,
        "up_token_id": "up-direct",
        "down_token_id": "down-direct",
    }]


def test_market_outcomes_stream_settlement_times_and_up_score(tmp_path):
    archive = importlib.import_module("h_replay_archive")
    path = tmp_path / "market_outcomes.csv.gz"
    _write_csv_gz(
        path,
        ["market_id", "winner", "resolution_ts", "source", "recorded_at"],
        [
            {
                "market_id": "0xup",
                "winner": "Up",
                "resolution_ts": "1790936251.125",
                "source": "POLY_CLOB_MARKET_RESOLVED",
                "recorded_at": "1790936251.250",
            },
            {
                "market_id": "0xdown",
                "winner": "Down",
                "resolution_ts": "1790936551",
                "source": "POLY_CLOB_MARKET_RESOLVED",
                "recorded_at": "1790936551.5",
            },
        ],
    )

    outcomes = list(archive.iter_outcomes(path))

    assert [(row["winner"], row["up_won"]) for row in outcomes] == [("Up", True), ("Down", False)]
    assert outcomes[0] == {
        "kind": "market_outcome",
        "recv_ms": 1_790_936_251_250.0,
        "seq": 0,
        "source_ts_ms": 1_790_936_251_125.0,
        "market_id": "0xup",
        "winner": "Up",
        "up_won": True,
        "resolution_source": "POLY_CLOB_MARKET_RESOLVED",
    }
