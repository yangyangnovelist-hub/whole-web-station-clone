from __future__ import annotations

import ast
import gzip
import hashlib
import io
import json
from pathlib import Path

import absorption_daily as daily
import absorption_forward as forward
import poly_clob_rec as recorder
import pytest
from botocore.exceptions import ClientError

DAY = "20261011"
PROTOCOL_SHA = "a" * 64
EVALUATOR_SHA = "b" * 64
ROOT = Path(__file__).resolve().parent


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _poly_manifest(
    root: Path, *, day: str = DAY, missing_hour: int | None = None
) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    files = []
    for hour in range(24):
        if hour == missing_hour:
            continue
        name = f"poly_clob.{day}T{hour:02d}.jsonl.gz"
        path = root / name
        with gzip.open(path, "wt", encoding="utf-8") as stream:
            stream.write(json.dumps({"hour": hour}) + "\n")
        files.append(
            {
                "file": name,
                "size": path.stat().st_size,
                "sha256": _sha256(path),
                "uploaded_ok": True,
                "gzip_ok": True,
            }
        )
    return {"day": day, "group": "poly", "files": files}


def _protocol() -> dict:
    return {
        "schema": "post-sweep-absorption-protocol-v1",
        "strategy_id": "BTC-5M-POST-SWEEP-ASK-ABSORPTION-V1",
        "holdout_start": "2026-10-11T00:00:00Z",
        "holdout_start_ms": 1791676800000,
        "collector_region": "eu-west-1",
        "signal": {
            "maximum_interprint_ms": 50.0,
            "minimum_burst_shares": 5.0,
            "depletion_deadline_ms": 50.0,
            "minimum_depletion_fraction": 0.5,
            "refill_deadline_ms": 250.0,
            "remaining_seconds": [20.0, 240.0],
        },
        "causality": {"book_fresh_ms": 2000.0},
        "execution": {
            "evaluation_ms": [400.0, 500.0],
            "minimum_full_fill_shares": 5.0,
            "taker_fee_rate": 0.07,
            "unwind_wait_ms": 5000.0,
        },
    }


def test_raw_poly_binding_requires_all_24_verified_hours(tmp_path: Path) -> None:
    manifest = _poly_manifest(tmp_path)

    paths, identity = daily.bind_poly_hours(manifest, tmp_path, DAY)

    assert len(paths) == 24
    assert identity == daily.raw_poly_identity(manifest, DAY)
    assert len(identity) == 64

    incomplete_root = tmp_path / "incomplete"
    incomplete = _poly_manifest(incomplete_root, missing_hour=7)
    with pytest.raises(ValueError, match="hour coverage"):
        daily.bind_poly_hours(incomplete, incomplete_root, DAY)


def test_raw_poly_binding_rejects_local_hash_drift(tmp_path: Path) -> None:
    manifest = _poly_manifest(tmp_path)
    target = tmp_path / f"poly_clob.{DAY}T13.jsonl.gz"
    target.write_bytes(b"changed")

    with pytest.raises(ValueError, match="verified S3 manifest"):
        daily.bind_poly_hours(manifest, tmp_path, DAY)


def test_missing_local_poly_hours_restore_from_verified_s3_bytes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    manifest = _poly_manifest(source)
    payloads = {
        f"poly/{DAY}/{item['file']}": (source / item["file"]).read_bytes()
        for item in manifest["files"]
    }
    local = tmp_path / "local"
    local.mkdir()
    cache = tmp_path / "cache"

    class S3:
        def get_object(self, *, Bucket, Key):
            assert Bucket == "bucket"
            return {"Body": io.BytesIO(payloads[Key])}

    paths, identity = daily.materialize_poly_hours(
        S3(), "bucket", manifest, local, cache, DAY
    )

    assert len(paths) == 24
    assert all(path.parent == cache and path.is_file() for path in paths)
    assert identity == daily.raw_poly_identity(manifest, DAY)

    (paths[7]).write_bytes(b"corrupt")
    repaired, _identity = daily.materialize_poly_hours(
        S3(), "bucket", manifest, local, cache, DAY
    )
    assert _sha256(repaired[7]) == manifest["files"][7]["sha256"]


def test_cross_midnight_carry_in_restores_active_market_metadata(
    tmp_path: Path,
) -> None:
    previous_day = "20261010"
    previous_root = tmp_path / "previous"
    manifest = _poly_manifest(previous_root, day=previous_day)
    source = previous_root / f"poly_clob.{previous_day}T23.jsonl.gz"
    start_ns = 1_791_676_800_000_000_000
    with gzip.open(source, "wt", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "rn": start_ns - 1_000_000,
                    "c": -1,
                    "k": "meta",
                    "m": {
                        "meta": "btc-updown-5m-1791676800",
                        "cid": "midnight-market",
                        "tokens": json.dumps(["up", "down"]),
                        "outcomes": json.dumps(["Up", "Down"]),
                    },
                }
            )
            + "\n"
        )
    item = next(row for row in manifest["files"] if row["file"] == source.name)
    item.update(size=source.stat().st_size, sha256=_sha256(source))

    carry_path, binding = daily.materialize_poly_carry_in(
        object(),
        "bucket",
        manifest,
        previous_root,
        tmp_path / "raw-poly-carry",
        DAY,
    )
    current_root = tmp_path / "raw-poly"
    current_root.mkdir()
    current = current_root / f"poly_clob.{DAY}T00.jsonl.gz"
    with gzip.open(current, "wt", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "rn": start_ns + 1,
                    "c": 0,
                    "e": start_ns,
                    "s": 0,
                    "k": "connection",
                    "assets": ["up", "down"],
                }
            )
            + "\n"
        )
        stream.write(
            json.dumps(
                {
                    "rn": start_ns + 2,
                    "c": 0,
                    "e": start_ns,
                    "s": 1,
                    "k": "message",
                    "m": {
                        "event_type": "book",
                        "asset_id": "up",
                        "market": "midnight-market",
                        "timestamp": "1791676800000",
                        "bids": [],
                        "asks": [{"price": "0.50", "size": "5"}],
                    },
                }
            )
            + "\n"
        )

    events = list(
        daily.detector.iter_raw_clob_events(
            [carry_path, current],
            segment_end_ms=1_791_676_801_000.0,
            preserve_input_order=True,
        )
    )

    assert binding["source_file"] == source.name
    assert binding["active_market_ids"] == ["midnight-market"]
    assert [event["kind"] for event in events[:3]] == [
        "market",
        "connection",
        "snapshot",
    ]


def test_recorder_hour_rotation_rewrites_active_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(recorder, "OUT", str(tmp_path))
    hours = iter(["20261010T23", "20261011T00"])
    monkeypatch.setattr(recorder.time, "strftime", lambda *_args: next(hours))
    monkeypatch.setattr(recorder.time, "time_ns", lambda: 102)
    metadata = {
        "meta": "btc-updown-5m-1791676800",
        "cid": "midnight-market",
        "tokens": json.dumps(["up", "down"]),
        "outcomes": json.dumps(["Up", "Down"]),
    }
    sink = recorder.Sink()
    sink.remember_metadata(metadata)
    sink.set_active_slugs([metadata["meta"]])
    sink.write(json.dumps({"rn": 100, "c": -1, "k": "meta", "m": metadata}))
    sink.write(
        json.dumps(
            {"rn": 101, "c": 0, "e": 1, "s": 0, "k": "connection"}
        )
    )
    sink.close()

    with gzip.open(
        tmp_path / "poly_clob.20261011T00.jsonl.gz", "rt", encoding="utf-8"
    ) as stream:
        rows = [json.loads(line) for line in stream]

    assert rows[0]["k"] == "connection"
    assert rows[1]["k"] == "meta"
    assert rows[1]["m"] == metadata
    assert rows[1]["rn"] > rows[0]["rn"]


def test_persisted_day_result_is_byte_stable_or_rejected(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    payload = {
        "schema": daily.DAY_SCHEMA,
        "complete": True,
        "day": "2026-10-11",
        "protocol_sha256": PROTOCOL_SHA,
        "evaluator_sha256": EVALUATOR_SHA,
        "artifact_sha256": "c" * 64,
        "rows": [],
    }

    first = daily.persist_day_result(path, payload)
    second = daily.persist_day_result(path, dict(reversed(list(payload.items()))))
    assert first == second == _sha256(path)

    with pytest.raises(ValueError, match="immutable day result"):
        daily.persist_day_result(path, {**payload, "rows": [{"different": True}]})


def test_immutable_upload_propagates_head_permission_error(tmp_path: Path) -> None:
    path = tmp_path / "evidence.json"
    path.write_bytes(b"evidence")

    class S3:
        def head_object(self, **_kwargs):
            raise ClientError(
                {
                    "Error": {"Code": "AccessDenied"},
                    "ResponseMetadata": {"HTTPStatusCode": 403},
                },
                "HeadObject",
            )

        def put_object(self, **_kwargs):
            raise AssertionError("403 must not be treated as absence")

    with pytest.raises(ClientError):
        daily.archive_daily._upload_file(S3(), "bucket", "key", path)


def _s3_error(code: str, status: int, operation: str) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        operation,
    )


class _ImmutableS3:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, dict[str, str]]] = {}
        self.puts = 0
        self.concurrent: tuple[bytes, dict[str, str]] | None = None

    def head_object(self, *, Bucket, Key):
        assert Bucket == "bucket"
        if Key not in self.objects:
            raise _s3_error("NoSuchKey", 404, "HeadObject")
        body, metadata = self.objects[Key]
        return {"ContentLength": len(body), "Metadata": dict(metadata)}

    def put_object(
        self, *, Bucket, Key, Body, ContentLength, Metadata, IfNoneMatch
    ):
        assert Bucket == "bucket"
        assert IfNoneMatch == "*"
        self.puts += 1
        if self.concurrent is not None:
            self.objects[Key] = self.concurrent
            self.concurrent = None
            raise _s3_error("PreconditionFailed", 412, "PutObject")
        if Key in self.objects:
            raise _s3_error("PreconditionFailed", 412, "PutObject")
        body = Body.read()
        assert len(body) == ContentLength
        self.objects[Key] = (body, dict(Metadata))
        return {"ETag": '"created"'}


def test_immutable_upload_is_atomic_idempotent_and_non_overwriting(
    tmp_path: Path,
) -> None:
    path = tmp_path / "evidence.json"
    path.write_bytes(b"evidence")
    s3 = _ImmutableS3()

    first = daily.archive_daily._upload_file(s3, "bucket", "key", path)
    second = daily.archive_daily._upload_file(s3, "bucket", "key", path)

    assert first == second
    assert s3.puts == 1
    assert s3.objects["key"][0] == b"evidence"

    different = tmp_path / "different.json"
    different.write_bytes(b"different")
    with pytest.raises(ValueError, match="immutable S3 evidence collision"):
        daily.archive_daily._upload_file(s3, "bucket", "key", different)
    assert s3.objects["key"][0] == b"evidence"


def test_immutable_upload_accepts_same_hash_concurrent_winner(
    tmp_path: Path,
) -> None:
    path = tmp_path / "evidence.json"
    path.write_bytes(b"evidence")
    digest = _sha256(path)
    s3 = _ImmutableS3()
    s3.concurrent = (b"evidence", {"sha256": digest})

    result = daily.archive_daily._upload_file(s3, "bucket", "key", path)

    assert result["sha256"] == digest
    assert s3.puts == 1


def test_immutable_upload_rejects_different_concurrent_winner(
    tmp_path: Path,
) -> None:
    path = tmp_path / "evidence.json"
    path.write_bytes(b"evidence")
    s3 = _ImmutableS3()
    s3.concurrent = (b"different", {"sha256": "0" * 64})

    with pytest.raises(ValueError, match="immutable S3 evidence collision"):
        daily.archive_daily._upload_file(s3, "bucket", "key", path)


def test_immutable_upload_propagates_head_server_error(tmp_path: Path) -> None:
    path = tmp_path / "evidence.json"
    path.write_bytes(b"evidence")

    class S3:
        def head_object(self, **_kwargs):
            raise _s3_error("InternalError", 500, "HeadObject")

    with pytest.raises(ClientError):
        daily.archive_daily._upload_file(S3(), "bucket", "key", path)


def test_immutable_upload_propagates_put_server_error(tmp_path: Path) -> None:
    path = tmp_path / "evidence.json"
    path.write_bytes(b"evidence")

    class S3(_ImmutableS3):
        def put_object(self, **_kwargs):
            raise _s3_error("InternalError", 500, "PutObject")

    with pytest.raises(ClientError):
        daily.archive_daily._upload_file(S3(), "bucket", "key", path)


def test_completed_day_binds_result_hash() -> None:
    payload = {
        "schema": daily.COMPLETE_SCHEMA,
        "status": "complete",
        "day": "2026-10-11",
        "protocol_sha256": PROTOCOL_SHA,
        "evaluator_sha256": EVALUATOR_SHA,
        "result_sha256": "c" * 64,
        "evidence": [
            {"path": "result.json", "bytes": 1, "sha256": "d" * 64}
        ],
    }

    with pytest.raises(ValueError, match="result hash"):
        daily._validate_complete(
            payload,
            day=DAY,
            protocol_sha256=PROTOCOL_SHA,
            evaluator_sha256=EVALUATOR_SHA,
        )


def test_bound_replay_uses_raw_trade_stream_twice_and_emits_valid_rows(
    tmp_path: Path, monkeypatch
) -> None:
    base_ms = 1791676860000.0
    market = "m"
    up = "up"
    down = "down"

    def snapshot(token: str, recv_ms: float, bids, asks):
        return {
            "kind": "snapshot",
            "recv_ms": recv_ms,
            "source_ts_ms": recv_ms - 2,
            "market_id": market,
            "asset_id": token,
            "bids": [{"price": price, "size": size} for price, size in bids],
            "asks": [{"price": price, "size": size} for price, size in asks],
            "ambiguous_receipt": False,
        }

    events = [
        {
            "kind": "market",
            "recv_ms": base_ms - 10,
            "market_id": market,
            "slot": 1791676800,
            "up_token_id": up,
            "down_token_id": down,
        },
        {"kind": "connection", "recv_ms": base_ms - 9, "epoch": 1},
        snapshot(up, base_ms - 8, [(0.49, 20)], [(0.50, 10), (0.51, 10)]),
        snapshot(down, base_ms - 7, [(0.47, 20)], [(0.48, 10), (0.49, 10)]),
        {
            "kind": "trade",
            "recv_ms": base_ms,
            "source_ts_ms": base_ms - 2,
            "market_id": market,
            "asset_id": up,
            "side": "BUY",
            "price": 0.50,
            "size": 3.0,
            "ambiguous_receipt": False,
        },
        {
            "kind": "trade",
            "recv_ms": base_ms + 30,
            "source_ts_ms": base_ms + 28,
            "market_id": market,
            "asset_id": up,
            "side": "BUY",
            "price": 0.50,
            "size": 2.0,
            "ambiguous_receipt": False,
        },
        {
            "kind": "price_change",
            "recv_ms": base_ms + 45,
            "source_ts_ms": base_ms + 43,
            "market_id": market,
            "changes": [
                {"asset_id": up, "side": "SELL", "price": 0.50, "size": 4.0}
            ],
            "ambiguous_receipt": False,
        },
        {
            "kind": "price_change",
            "recv_ms": base_ms + 120,
            "source_ts_ms": base_ms + 118,
            "market_id": market,
            "changes": [
                {"asset_id": up, "side": "SELL", "price": 0.50, "size": 9.0}
            ],
            "ambiguous_receipt": False,
        },
        {"kind": "heartbeat", "recv_ms": base_ms + 300},
        snapshot(up, base_ms + 680, [(0.49, 20)], [(0.50, 10)]),
        snapshot(down, base_ms + 680, [(0.50, 20)], [(0.48, 10)]),
        snapshot(up, base_ms + 780, [(0.49, 20)], [(0.50, 10)]),
        snapshot(down, base_ms + 780, [(0.50, 20)], [(0.48, 10)]),
    ]
    calls = []

    def raw_events(paths, end_ms, *, preserve_input_order=False):
        assert preserve_input_order is True
        calls.append((tuple(paths), end_ms))
        return iter(events)

    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "manifest.json").write_text("{}\n", encoding="utf-8")
    (artifact / daily.archive_daily.ARTIFACT_BINDING).write_text(
        "{}\n", encoding="utf-8"
    )
    (artifact / "market_outcomes.csv.gz").write_bytes(b"unused")
    monkeypatch.setattr(daily.detector, "iter_raw_clob_events", raw_events)
    monkeypatch.setattr(
        daily.archive,
        "validate_standard_artifact",
        lambda _path: {"manifest": {"run_id": f"eu-west-{DAY}"}},
    )
    monkeypatch.setattr(
        daily.archive,
        "iter_outcomes",
        lambda _path: iter([{"market_id": market, "winner": "Down"}]),
    )

    payload = daily.replay_bound_day(
        artifact,
        [tmp_path / "raw.gz"],
        day=DAY,
        protocol=_protocol(),
        protocol_sha256=PROTOCOL_SHA,
        evaluator_sha256=EVALUATOR_SHA,
        raw_poly_identity_sha256="c" * 64,
        archive_manifests_sha256="d" * 64,
        archive_manifests={},
        raw_poly_binding={},
    )

    assert len(calls) == 2
    assert payload["counters"]["signals"] == {"base": 1}
    base_ids = sorted(
        {row["signal_id"] for row in payload["rows"] if row["variant"] == "base"}
    )
    assert payload["counters"]["signal_ids"] == {"base": base_ids}
    assert {row["variant"] for row in payload["rows"]} == {
        "base",
        "direction_reversal",
        "time_shift",
    }
    assert all(row["pnl"] is not None for row in payload["rows"])
    forward.validate_day_result(
        payload,
        frozen=_protocol(),
        protocol_sha256=PROTOCOL_SHA,
        evaluator_sha256=EVALUATOR_SHA,
        now_ms=1791936000000,
    )


def _install_process_fakes(tmp_path: Path, monkeypatch, calls: dict) -> tuple[Path, Path]:
    data = tmp_path / "data"
    poly = tmp_path / "poly"
    data.mkdir()
    previous_manifest = _poly_manifest(poly, day="20261010")
    poly_manifest = _poly_manifest(poly)
    rec_manifest = {"day": DAY, "group": "rec", "files": [{"file": "unused"}]}
    protocol = _protocol()
    monkeypatch.setattr(daily, "_utc_today", lambda: "20261013")
    monkeypatch.setattr(
        daily.forward,
        "load_freezes",
        lambda: (protocol, PROTOCOL_SHA, {"schema": "evaluator"}, EVALUATOR_SHA),
    )
    monkeypatch.setattr(
        daily.archive_daily,
        "_archive_manifest",
        lambda _s3, _bucket, requested_day, group: (
            rec_manifest
            if group == "rec"
            else poly_manifest
            if requested_day == DAY
            else previous_manifest
        ),
    )
    monkeypatch.setattr(daily.archive_daily, "validate_local_archive", lambda *_args: None)

    def build(_data, _poly, day_root, _day, _snapshot):
        artifact = Path(day_root) / "artifact"
        artifact.mkdir(parents=True, exist_ok=True)
        (artifact / "manifest.json").write_text("{}\n", encoding="utf-8")
        (artifact / daily.archive_daily.ARTIFACT_BINDING).write_text(
            "{}\n", encoding="utf-8"
        )
        return artifact

    monkeypatch.setattr(daily.archive_daily, "_build_artifact", build)

    def replay_day(
        artifact,
        raw_paths,
        *,
        day,
        protocol,
        protocol_sha256,
        evaluator_sha256,
        raw_poly_identity_sha256,
        archive_manifests_sha256,
        archive_manifests,
        raw_poly_binding,
    ):
        calls["replay"] += 1
        calls["raw_paths"] = tuple(Path(path) for path in raw_paths)
        calls["raw_binding"] = dict(raw_poly_binding)
        return {
            "schema": daily.DAY_SCHEMA,
            "complete": True,
            "day": daily._iso_day(day),
            "collector_region": protocol["collector_region"],
            "protocol_sha256": protocol_sha256,
            "evaluator_sha256": evaluator_sha256,
            "artifact_sha256": daily._canonical_sha256(
                {
                    "raw": raw_poly_identity_sha256,
                    "archive": archive_manifests_sha256,
                }
            ),
            "raw_poly_identity_sha256": raw_poly_identity_sha256,
            "archive_manifests_sha256": archive_manifests_sha256,
            "strict_manifest_sha256": daily._sha256(Path(artifact) / "manifest.json"),
            "input_bindings": {
                "archive_manifests": archive_manifests,
                "raw_poly_binding": raw_poly_binding,
            },
            "counters": {
                "events": {},
                "signals": {},
                "signal_ids": {},
                "detector_outcomes": {},
                "execution_rows": 0,
            },
            "rows": [],
        }

    monkeypatch.setattr(daily, "replay_bound_day", replay_day)
    monkeypatch.setattr(
        daily,
        "_upload_evidence",
        lambda *_args, **_kwargs: calls.__setitem__("uploads", calls["uploads"] + 1),
    )

    def update(_result, state, **_identity):
        calls["update"] += 1
        staged = Path(state)
        staged.mkdir(parents=True, exist_ok=True)
        (staged / forward.STATUS).write_text(
            '{"status":"collecting"}\n', encoding="utf-8"
        )
        return {"status": "collecting"}

    monkeypatch.setattr(daily.forward, "update", update)
    return data, poly


def test_process_day_binds_previous_boundary_carry_in(
    tmp_path: Path, monkeypatch
) -> None:
    calls = {"replay": 0, "uploads": 0, "update": 0}
    data, poly = _install_process_fakes(tmp_path, monkeypatch, calls)

    daily.process_day(
        DAY,
        data,
        poly,
        tmp_path / "work",
        tmp_path / "state",
        s3=object(),
    )

    assert calls["raw_paths"][0].name.endswith("carry-in.jsonl.gz")
    assert calls["raw_binding"]["carry_in"]["source_day"] == "20261010"


def test_process_day_replays_once_and_reuses_immutable_completion(
    tmp_path: Path, monkeypatch
) -> None:
    calls = {"replay": 0, "uploads": 0, "update": 0}
    data, poly = _install_process_fakes(tmp_path, monkeypatch, calls)
    kwargs = {
        "day": DAY,
        "data_dir": data,
        "poly_dir": poly,
        "work_dir": tmp_path / "work",
        "state_dir": tmp_path / "state",
        "s3": object(),
    }

    first = daily.process_day(**kwargs)
    second = daily.process_day(**kwargs)

    assert first == second
    assert calls["replay"] == 1
    assert calls["update"] == 1
    assert calls["uploads"] == 1
    assert first["pooled_verdict"] == {"status": "collecting"}
    assert not (tmp_path / "work" / DAY / "artifact").exists()


def test_upload_failure_never_commits_forward_state(tmp_path: Path, monkeypatch) -> None:
    calls = {"replay": 0, "uploads": 0, "update": 0}
    data, poly = _install_process_fakes(tmp_path, monkeypatch, calls)
    monkeypatch.setattr(
        daily,
        "_upload_evidence",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("upload failed")),
    )

    with pytest.raises(RuntimeError, match="upload failed"):
        daily.process_day(
            DAY,
            data,
            poly,
            tmp_path / "work",
            tmp_path / "state",
            s3=object(),
    )

    assert calls["replay"] == 1
    assert calls["update"] == 1
    assert not (tmp_path / "state" / forward.STATUS).exists()
    assert not (tmp_path / "state" / daily.ADMISSION).exists()
    assert (tmp_path / "work" / DAY / "state-next" / forward.STATUS).is_file()


def test_complete_upload_failure_leaves_formal_state_uncommitted(
    tmp_path: Path, monkeypatch
) -> None:
    calls = {"replay": 0, "uploads": 0, "update": 0}
    data, poly = _install_process_fakes(tmp_path, monkeypatch, calls)

    def fail_complete_upload(_s3, _bucket, _day, _root, paths):
        calls["uploads"] += 1
        if any(Path(path).name == "complete.json" for path in paths):
            raise RuntimeError("complete upload failed")

    monkeypatch.setattr(daily, "_upload_evidence", fail_complete_upload)

    with pytest.raises(RuntimeError, match="complete upload failed"):
        daily.process_day(
            DAY,
            data,
            poly,
            tmp_path / "work",
            tmp_path / "state",
            s3=object(),
        )

    assert not (tmp_path / "state" / forward.STATUS).exists()
    assert (tmp_path / "work" / DAY / "state-next" / forward.STATUS).is_file()
    assert (tmp_path / "work" / DAY / "complete.json").is_file()
    assert calls["update"] == 1

    monkeypatch.setattr(
        daily,
        "_upload_evidence",
        lambda *_args, **_kwargs: calls.__setitem__(
            "uploads", calls["uploads"] + 1
        ),
    )
    completed = daily.process_day(
        DAY,
        data,
        poly,
        tmp_path / "work",
        tmp_path / "state",
        s3=object(),
    )

    assert completed["status"] == "complete"
    assert (tmp_path / "state" / forward.STATUS).is_file()
    assert (tmp_path / "state" / daily.ADMISSION).is_file()
    assert calls["replay"] == 1
    assert calls["update"] == 1


def test_invalid_replay_is_rejected_before_result_persistence_or_upload(
    tmp_path: Path, monkeypatch
) -> None:
    calls = {"replay": 0, "uploads": 0, "update": 0}
    data, poly = _install_process_fakes(tmp_path, monkeypatch, calls)
    replay = daily.replay_bound_day

    def invalid_replay(*args, **kwargs):
        payload = replay(*args, **kwargs)
        payload["collector_region"] = "wrong-region"
        return payload

    monkeypatch.setattr(daily, "replay_bound_day", invalid_replay)

    with pytest.raises(ValueError, match="identity drift"):
        daily.process_day(
            DAY,
            data,
            poly,
            tmp_path / "work",
            tmp_path / "state",
            s3=object(),
        )

    assert not (tmp_path / "work" / DAY / "result.json").exists()
    assert calls["uploads"] == 0
    assert calls["update"] == 0


def test_unadmitted_day_gap_is_rejected_before_archive_reads(
    tmp_path: Path, monkeypatch
) -> None:
    protocol = _protocol()
    monkeypatch.setattr(daily, "_utc_today", lambda: "20261014")
    monkeypatch.setattr(
        daily.forward,
        "load_freezes",
        lambda: (protocol, PROTOCOL_SHA, {"schema": "evaluator"}, EVALUATOR_SHA),
    )

    class UnexpectedS3:
        def get_object(self, **_kwargs):
            raise AssertionError("out-of-order day must not read S3")

    with pytest.raises(ValueError, match="next absorption admission"):
        daily.process_day(
            "20261012",
            tmp_path / "data",
            tmp_path / "poly",
            tmp_path / "work",
            tmp_path / "state",
            s3=UnexpectedS3(),
        )


def test_crash_after_forward_admission_retries_same_day_completion(
    tmp_path: Path, monkeypatch
) -> None:
    calls = {"replay": 0, "uploads": 0, "update": 0}
    data, poly = _install_process_fakes(tmp_path, monkeypatch, calls)
    kwargs = {
        "day": DAY,
        "data_dir": data,
        "poly_dir": poly,
        "work_dir": tmp_path / "work",
        "state_dir": tmp_path / "state",
        "s3": object(),
    }

    with pytest.raises(RuntimeError, match="after absorption forward update"):
        daily.process_day(**kwargs, _crash_after_forward_update=True)

    assert not (tmp_path / "state" / forward.STATUS).exists()
    assert (tmp_path / "work" / DAY / "state-next" / forward.STATUS).is_file()
    assert not (tmp_path / "work" / DAY / "complete.json").exists()

    completed = daily.process_day(**kwargs)

    assert completed["status"] == "complete"
    assert calls["replay"] == 1
    assert calls["update"] == 2
    assert (tmp_path / "state" / forward.STATUS).is_file()
    assert (tmp_path / "state" / daily.ADMISSION).is_file()


def test_frozen_daily_units_bind_the_paper_runner() -> None:
    service_path = ROOT / "systemd" / "absorption-daily.service"
    timer_path = ROOT / "systemd" / "absorption-daily.timer"
    service = service_path.read_text(encoding="utf-8")
    timer = timer_path.read_text(encoding="utf-8")
    evaluator = json.loads(
        (ROOT / "forward" / "absorption-evaluator-freeze.json").read_text(
            encoding="utf-8"
        )
    )
    dependencies = evaluator["dependencies_sha256"]
    local_modules = {path.stem: path for path in ROOT.glob("*.py")}
    pending = ["absorption_daily", "strict_prebuild", "poly_clob_rec"]
    closure: set[str] = set()
    while pending:
        module = pending.pop()
        if module in closure or module not in local_modules:
            continue
        closure.add(module)
        tree = ast.parse(local_modules[module].read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                pending.extend(
                    name.name.split(".")[0]
                    for name in node.names
                    if name.name.split(".")[0] in local_modules
                )
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = node.module.split(".")[0]
                if imported in local_modules:
                    pending.append(imported)
    required_dependencies = {f"{module}.py" for module in closure} | {
        "requirements.txt",
        "systemd/absorption-daily.service",
        "systemd/absorption-daily.timer",
    }

    assert "STRICT_SHARED_ROOT=/home/ubuntu/rec/formal/strict-days" in service
    assert "absorption_daily.py" in service
    assert "MemoryMax=4G" in service
    assert "OnCalendar=*-*-* 06:00:00 UTC" in timer
    assert "Persistent=true" in timer
    assert required_dependencies <= set(dependencies)
    for relative in required_dependencies:
        assert dependencies[relative] == _sha256(ROOT / relative)
    assert dependencies["absorption_daily.py"] == _sha256(ROOT / "absorption_daily.py")
    assert dependencies["absorption_forward.py"] == _sha256(
        ROOT / "absorption_forward.py"
    )
    assert dependencies["poly_clob_rec.py"] == _sha256(ROOT / "poly_clob_rec.py")
    assert dependencies["systemd/absorption-daily.service"] == _sha256(service_path)
    assert dependencies["systemd/absorption-daily.timer"] == _sha256(timer_path)
