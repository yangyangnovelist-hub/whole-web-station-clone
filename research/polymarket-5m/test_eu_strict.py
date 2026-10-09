import gzip
import json

import pytest

import eu_strict as eu
import h_replay_archive as archive


DAY = "20261010"
START_NS = 1_791_590_400_000_000_000
SOURCE_MS = START_NS // 1_000_000 + 1_000


def _raw(path, rows):
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        for receive_ns, payload in rows:
            stream.write(f"{receive_ns}\t{json.dumps(payload, separators=(',', ':'))}\n")


def _marker(kind, epoch):
    return {"_recorder": {
        "schema": "recorder-lifecycle-v1", "kind": kind, "epoch": epoch,
    }}


def test_builds_valid_receipt_race_artifact_from_eu_recorders(tmp_path):
    data = tmp_path / "data"
    poly = tmp_path / "poly"
    out = tmp_path / "out"
    data.mkdir()
    poly.mkdir()
    routes = {
        "bn_spot_2": {"e": "trade", "T": SOURCE_MS, "p": "80000", "q": "0.1", "t": 1},
        "bn_spot_3": {"e": "trade", "T": SOURCE_MS, "p": "80000", "q": "0.1", "t": 1},
        "bn_spot_4": {"e": "trade", "T": SOURCE_MS, "p": "80000", "q": "0.1", "t": 1},
        "bn_fut_pub_2": {"e": "bookTicker", "E": SOURCE_MS, "T": SOURCE_MS,
                         "u": 1, "b": "79999", "a": "80001"},
        "bn_fut_pub_3": {"e": "bookTicker", "E": SOURCE_MS, "T": SOURCE_MS,
                         "u": 1, "b": "79999", "a": "80001"},
        "bn_fut_trade": {"e": "trade", "E": SOURCE_MS, "T": SOURCE_MS,
                         "t": 1, "p": "80000", "q": "0.1"},
        "bn_fut_trade_2": {"e": "trade", "E": SOURCE_MS, "T": SOURCE_MS,
                           "t": 1, "p": "80000", "q": "0.1"},
    }
    for offset, (route, frame) in enumerate(routes.items()):
        epoch = START_NS + 100 + offset
        _raw(data / f"{route}.{DAY}T00.txt.gz", [
            (START_NS + offset, frame),
            (epoch, _marker("connection", epoch)),
            (START_NS + 1_000_000_000 + offset, frame),
            (START_NS + 86_399_000_000_000 + offset, _marker("disconnect", epoch)),
        ])
    epoch = START_NS + 199
    quote = {"params": {"channel": "quote.BTC-PERPETUAL", "data": {
        "timestamp": SOURCE_MS, "best_bid_price": 79999,
        "best_ask_price": 80001, "best_bid_amount": 10, "best_ask_amount": 11,
    }}}
    _raw(data / f"deribit.{DAY}T00.txt.gz", [
        (START_NS + 99, quote),
        (epoch, _marker("connection", epoch)),
        (START_NS + 1_000_000_099, quote),
        (START_NS + 86_399_000_000_099, _marker("disconnect", epoch)),
    ])

    up, down, market = "up-token", "down-token", "condition"
    rows = [
        {"rn": START_NS + 100, "c": -1, "k": "meta", "m": {
            "meta": f"btc-updown-5m-{START_NS // 1_000_000_000}", "cid": market,
            "tokens": json.dumps([up, down]), "outcomes": json.dumps(["Up", "Down"]),
        }},
        {"rn": START_NS + 200, "c": 0, "e": START_NS - 1, "s": 99,
         "k": "message", "m": {"event_type": "book", "timestamp": str(SOURCE_MS),
                                    "market": market, "asset_id": up,
                                    "bids": [], "asks": []}},
    ]
    for connection in range(4):
        epoch = START_NS + 1_000 + connection
        rows.append({"rn": epoch, "c": connection, "e": epoch, "s": 0,
                     "k": "connection", "assets": [up, down]})
    book_time = START_NS + 2_000_000_000
    for sequence, token in enumerate((up, down), 1):
        rows.append({
            "rn": book_time + sequence, "c": 0, "e": START_NS + 1_000, "s": sequence,
            "k": "message", "m": {
                "event_type": "book", "timestamp": str(book_time // 1_000_000),
                "market": market, "asset_id": token,
                "bids": [{"price": "0.4", "size": "10"}],
                "asks": [{"price": "0.6", "size": "10"}],
            },
        })
    for connection in range(4):
        epoch = START_NS + 1_000 + connection
        rows.append({"rn": START_NS + 86_399_000_001_000 + connection, "c": connection,
                     "e": epoch, "s": 3 if connection == 0 else 1, "k": "disconnect"})
    with gzip.open(poly / f"poly_clob.{DAY}T00.jsonl.gz", "wt", encoding="utf-8") as stream:
        for row in sorted(rows, key=lambda item: item["rn"]):
            stream.write(json.dumps(row, separators=(",", ":")) + "\n")
    outcomes = {market: {
        "market_id": market, "winner": "Up", "resolution_ts": START_NS / 1e9 + 300,
        "source": "gamma", "recorded_at": START_NS / 1e9 + 400,
    }}

    manifest = eu.build(data, poly, out, DAY, outcomes=outcomes)
    validated = archive.validate_standard_artifact(out)

    assert manifest["complete"] is True
    assert manifest["receipt_race_ready"] is True
    assert manifest["counts"]["spot_trade"] == 1
    assert manifest["source_integrity"]["spot"]["deduplicated_frames"] == 2
    assert manifest["source_integrity"]["spot"]["leading_carryover_frames"] == 3
    assert manifest["clob_integrity"]["leading_carryover_frames"] == 1
    assert manifest["runtime"] | {"phase_seconds": {}} == {
        "json_backend": eu.JSON_BACKEND,
        "json_backend_version": eu.JSON_BACKEND_VERSION,
        "gzip_backend": eu.GZIP_BACKEND,
        "gzip_backend_version": eu.GZIP_BACKEND_VERSION,
        "gzip_input_policy": "stdlib_single_pass_integrity_check",
        "clob_storage_encoding": "price-change-batch-v1",
        "clob_dedupe_policy": "recorder-first-copy-v1",
        "gzip_compresslevel": eu.GZIP_LEVEL,
        "phase_seconds": {},
    }
    phases = manifest["runtime"]["phase_seconds"]
    assert set(phases["source_workers"]) == set(eu.SOURCE_ROUTES)
    assert all(value >= 0 for value in phases["source_workers"].values())
    assert all(
        value >= 0 for key, value in phases.items()
        if key != "source_workers"
    )
    assert validated["manifest"]["collector_region"] == "eu-west-1"
    strict = out / "strict"
    assert manifest["converter_validation"]["schema"] == "eu-strict-converter-validation-v1"
    assert not (strict / ".source-phase.json").exists()
    assert not (strict / ".clob-phase.json").exists()
    with (strict / "clob_events.jsonl.gz").open("ab") as stream:
        stream.write(b"corrupt")
    with pytest.raises(archive.ArchiveFormatError, match="converter-validated file drift"):
        archive.validate_standard_artifact(out)


def test_route_reader_fast_rejects_only_known_irrelevant_frames(tmp_path):
    path = tmp_path / f"bn_spot.{DAY}T00.txt.gz"
    irrelevant = {
        "stream": "btcusdt@depth20@100ms",
        "data": {"e": "depthUpdate", "E": SOURCE_MS, "b": [], "a": []},
    }
    relevant = {
        "stream": "btcusdt@trade",
        "data": {"e": "trade", "T": SOURCE_MS, "p": "80000", "q": "0.1", "t": 7},
    }
    _raw(path, [(START_NS, irrelevant), (START_NS + 1, relevant)])
    stats = {"skipped_irrelevant_frames": 0}

    rows = list(eu._iter_route([path], "bn_spot", "spot", stats))

    assert rows == [(START_NS + 1, "bn_spot", relevant)]
    assert stats["skipped_irrelevant_frames"] == 1


def test_route_reader_does_not_hide_unknown_malformed_frames(tmp_path):
    path = tmp_path / f"bn_spot.{DAY}T00.txt.gz"
    with gzip.open(path, "wb") as stream:
        stream.write(f"{START_NS}\t".encode() + b'{"unknown":broken}\n')

    with pytest.raises(ValueError):
        list(eu._iter_route(
            [path], "bn_spot", "spot", {"skipped_irrelevant_frames": 0},
        ))


@pytest.mark.parametrize("role", ["spot", "futures_trade"])
@pytest.mark.parametrize("price,size", [
    ("0", "0"),
    ("80000", "0"),
    ("nan", "0.1"),
    ("80000", "-0.1"),
])
def test_trade_adapter_drops_non_economic_numeric_sentinels(role, price, size):
    payload = {
        "e": "trade", "E": SOURCE_MS, "T": SOURCE_MS,
        "t": 7, "p": price, "q": size,
    }

    assert eu._frame_event(role, payload) is None


def test_clob_reader_recovers_legacy_transport_newline_split(tmp_path):
    path = tmp_path / f"poly_clob.{DAY}T00.jsonl.gz"
    with gzip.open(path, "wb") as stream:
        stream.write(b'{"rn":1,"c":0,"m":[{"event_type":"book"}]\n}\n')
        stream.write(b'{"rn":2,"c":0,"m":[]}\n')

    rows = list(eu._iter_json_records(path))

    assert rows == [
        (1, {"rn": 1, "c": 0, "m": [{"event_type": "book"}]}),
        (3, {"rn": 2, "c": 0, "m": []}),
    ]


def test_clob_reader_rejects_mid_record_corruption(tmp_path):
    path = tmp_path / f"poly_clob.{DAY}T00.jsonl.gz"
    with gzip.open(path, "wb") as stream:
        stream.write(b'{"rn":1,"c":broken}\n')

    with pytest.raises(ValueError, match="invalid JSON record"):
        list(eu._iter_json_records(path))


def test_clob_reader_supports_concatenated_gzip_members(tmp_path):
    path = tmp_path / f"poly_clob.{DAY}T00.jsonl.gz"
    with gzip.open(path, "wb") as stream:
        stream.write(b'{"rn":1,"c":0,"m":[]}\n')
    with gzip.open(path, "ab") as stream:
        stream.write(b'{"rn":2,"c":0,"m":[]}\n')

    assert list(eu._iter_json_records(path)) == [
        (1, {"rn": 1, "c": 0, "m": []}),
        (2, {"rn": 2, "c": 0, "m": []}),
    ]
