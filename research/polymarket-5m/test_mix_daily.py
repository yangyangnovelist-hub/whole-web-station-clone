from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

import mix_daily as daily


DAY = "20261010"
PREVIOUS = "20261009"


def _raw(path: Path, rows: list[tuple[int, dict]]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        for receive_ns, payload in rows:
            stream.write(f"{receive_ns}\t{json.dumps(payload, separators=(',', ':'))}\n")


def _connection(epoch: int) -> dict:
    return {"_recorder": {"schema": "recorder-lifecycle-v1", "kind": "connection", "epoch": epoch}}


def _disconnect(epoch: int) -> dict:
    return {"_recorder": {"schema": "recorder-lifecycle-v1", "kind": "disconnect", "epoch": epoch}}


def _trade(aggregate_id: int, price: str = "80000", source_ms: int = 1_791_590_400_000) -> dict:
    return {
        "stream": "btcusdt@aggTrade",
        "data": {
            "e": "aggTrade",
            "a": aggregate_id,
            "T": source_ms,
            "p": price,
            "q": "0.1",
            "m": False,
        },
    }


def test_aggregate_source_selection_requires_every_route_hour():
    files = [
        {"file": f"{source}.{DAY}T{hour:02d}.txt.gz", "size": 1, "sha256": "a" * 64}
        for source in daily.AGG_SOURCES
        for hour in range(24)
    ]
    manifest = {"day": DAY, "group": "mix", "files": files}

    selected = daily.aggregate_source_items(manifest, DAY, {0, 23})

    assert len(selected) == len(daily.AGG_SOURCES) * 2
    manifest["files"].pop()
    with pytest.raises(ValueError, match="aggregate route coverage"):
        daily.aggregate_source_items(manifest, DAY, {0, 23})


def test_build_aggregate_tape_keeps_first_copy_and_records_identity(tmp_path):
    roots = []
    receive = [120_000_000, 100_000_000, 110_000_000]
    for index, source in enumerate(daily.AGG_SOURCES):
        path = tmp_path / f"{source}.{DAY}T00.txt.gz"
        _raw(path, [
            (10_000_000 + index, _connection(index + 1)),
            (receive[index], _trade(7)),
            (220_000_000 + index, _trade(8, "80001", 1_791_590_400_010)),
        ])
        roots.append({"file": path.name, "size": path.stat().st_size,
                      "sha256": daily.archive_daily._sha256(path)})
    destination = tmp_path / "latency" / "binance_trades.jsonl.gz"

    profile = daily.build_aggregate_tape(tmp_path, roots, destination)

    with gzip.open(destination, "rt", encoding="utf-8") as stream:
        rows = [json.loads(line) for line in stream]
    assert [row["aggregate_id"] for row in rows] == [7, 8]
    assert rows[0]["receive_ts"] == pytest.approx(0.1)
    assert rows[0]["event"] == "BINANCE_WS_TRADE"
    assert profile["raw_trades"] == 6
    assert profile["deduplicated_trades"] == 2
    assert profile["duplicate_trades"] == 4
    assert profile["joint_disconnects"] == 0
    assert profile["tape_sha256"] == daily.archive_daily._sha256(destination)


def test_build_aggregate_tape_rejects_conflicting_duplicate(tmp_path):
    items = []
    for index, source in enumerate(daily.AGG_SOURCES):
        path = tmp_path / f"{source}.{DAY}T00.txt.gz"
        _raw(path, [
            (10_000_000 + index, _connection(index + 1)),
            (100_000_000 + index, _trade(7, "80001" if index == 2 else "80000")),
        ])
        items.append({"file": path.name, "size": path.stat().st_size,
                      "sha256": daily.archive_daily._sha256(path)})

    with pytest.raises(ValueError, match="conflicting aggregate trade"):
        daily.build_aggregate_tape(
            tmp_path, items, tmp_path / "latency" / "binance_trades.jsonl.gz",
        )


def test_build_aggregate_tape_rejects_joint_disconnect(tmp_path):
    items = []
    for index, source in enumerate(daily.AGG_SOURCES):
        path = tmp_path / f"{source}.{DAY}T00.txt.gz"
        epoch = index + 1
        _raw(path, [
            (10_000_000 + index, _connection(epoch)),
            (100_000_000 + index, _trade(7)),
            (200_000_000 + index, _disconnect(epoch)),
            (300_000_000 + index, _connection(epoch + 10)),
            (400_000_000 + index, _trade(8)),
        ])
        items.append({"file": path.name, "size": path.stat().st_size,
                      "sha256": daily.archive_daily._sha256(path)})

    with pytest.raises(ValueError, match="jointly disconnected"):
        daily.build_aggregate_tape(
            tmp_path, items, tmp_path / "latency" / "binance_trades.jsonl.gz",
        )


def test_build_aggregate_tape_tracks_disconnect_after_mid_epoch_start(tmp_path):
    items = []
    for index, source in enumerate(daily.AGG_SOURCES):
        path = tmp_path / f"{source}.{DAY}T00.txt.gz"
        epoch = index + 1
        _raw(path, [
            (100_000_000 + index, _trade(7)),
            (200_000_000 + index, _disconnect(epoch)),
        ])
        items.append({"file": path.name, "size": path.stat().st_size,
                      "sha256": daily.archive_daily._sha256(path)})

    with pytest.raises(ValueError, match="jointly disconnected"):
        daily.build_aggregate_tape(
            tmp_path, items, tmp_path / "latency" / "binance_trades.jsonl.gz",
        )


def test_stage_is_not_committed_before_evidence_upload(tmp_path, monkeypatch):
    state = tmp_path / "state"
    staged = tmp_path / "state-next"
    staged.mkdir()
    (staged / daily.CONTROL_LEDGER).write_text('{"row":1}\n', encoding="utf-8")
    base = daily.state_hashes(state)
    target = daily.state_hashes(staged)
    complete = {
        "base_state_sha256": base,
        "target_state_sha256": target,
        "day": DAY,
        "evaluator_freeze_sha256": "e" * 64,
    }

    assert not (state / daily.CONTROL_LEDGER).exists()
    daily.commit_state(complete, state, staged, "complete-sha")
    assert (state / daily.CONTROL_LEDGER).is_file()
    admission = json.loads((state / daily.ADMISSION).read_text(encoding="utf-8"))
    assert admission["day"] == DAY
    assert admission["state_sha256"] == target


def test_evaluator_freeze_binds_dependencies(tmp_path):
    frozen, digest = daily.load_evaluator_freeze()

    assert digest == "109e7f97b32bc83e318ae4b1f9fb0241f1107dc03b5d65bdf837df0cfa6b7dc3"
    assert frozen["control_protocol_sha256"] == (
        "28588718bd3a944595fc8a786beeb6fb1b54d3bd51a2246775995c31cdfe8b45"
    )
    frozen["dependencies_sha256"]["mix_daily.py"] = "0" * 64
    path = tmp_path / "mix-evaluator-freeze.json"
    path.write_text(json.dumps(frozen, sort_keys=True) + "\n", encoding="utf-8")
    path.with_suffix(".sha256").write_text(
        daily.archive_daily._sha256(path) + "\n", encoding="ascii",
    )
    with pytest.raises(ValueError, match="dependency drift"):
        daily.load_evaluator_freeze(path)


def _install_daily_fakes(tmp_path, monkeypatch, calls):
    control_protocol, projection_protocol, evaluator_sha = "c" * 64, "p" * 64, "e" * 64
    monkeypatch.setattr(daily, "_utc_today", lambda: "20261011")
    monkeypatch.setattr(daily, "load_evaluator_freeze", lambda *_args: ({
        "holdout_start_utc": "2026-10-10T00:00:00Z",
        "control_protocol_sha256": control_protocol,
        "projection_protocol_sha256": projection_protocol,
    }, evaluator_sha))
    monkeypatch.setattr(
        daily.control, "load_control_freeze",
        lambda *_args: ({"protocol_sha256": control_protocol}, object()),
    )
    monkeypatch.setattr(
        daily.projection, "load_protocol",
        lambda *_args: ({"protocol_sha256": projection_protocol}, 0.0),
    )
    monkeypatch.setattr(
        daily.archive_daily, "_archive_manifest",
        lambda _s3, _bucket, day, group: {"day": day, "group": group, "files": []},
    )
    monkeypatch.setattr(
        daily, "_mix_manifest",
        lambda _s3, _bucket, day: {"day": day, "group": "mix", "files": []},
    )
    monkeypatch.setattr(daily.archive_daily, "validate_local_archive", lambda *args: None)
    monkeypatch.setattr(daily, "aggregate_source_items", lambda *args: [])
    monkeypatch.setattr(daily, "_validate_local_items", lambda *args: None)

    def build_artifact(_data, _poly, day_root, day, _snapshot):
        calls["build"] += 1
        artifact = Path(day_root) / "artifact"
        artifact.mkdir(parents=True, exist_ok=True)
        (artifact / "manifest.json").write_text(
            json.dumps({"run_id": f"eu-west-{day}"}) + "\n", encoding="utf-8",
        )
        (artifact / daily.archive_daily.ARTIFACT_BINDING).write_text("{}\n", encoding="utf-8")
        return artifact

    def build_tape(_root, _items, destination):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(destination, "wt", encoding="utf-8") as stream:
            stream.write('{}\n')
        return {"tape_sha256": daily.archive_daily._sha256(destination)}

    def replay(_artifact, *, config, rows_out, source_protocol_sha256, aggregate_tape_path):
        calls["replay"] += 1
        calls["aggregate_tape_path"] = Path(aggregate_tape_path)
        Path(rows_out).write_text('{"market_id":"m"}\n', encoding="utf-8")
        return {"dataset": {"paper_gate_eligible": True}, "verdict": {"status": "collecting"}}

    def append(destination, incoming, _verdict):
        Path(destination).write_bytes(Path(incoming).read_bytes())
        return [{"market_id": "m"}]

    def run_projection(_freeze, _source, ledger, report, _verdict):
        Path(ledger).write_text('{"projection":1}\n', encoding="utf-8")
        Path(report).write_text('{"verdict":{"status":"collecting"}}\n', encoding="utf-8")
        return {"verdict": {"status": "collecting"}}

    monkeypatch.setattr(daily.archive_daily, "_build_artifact", build_artifact)
    monkeypatch.setattr(daily, "build_aggregate_tape", build_tape)
    monkeypatch.setattr(daily.control, "replay_archive", replay)
    monkeypatch.setattr(daily.control, "append_observations", append)
    monkeypatch.setattr(
        daily.control, "pin_verdict",
        lambda *_args, **_kwargs: {"status": "collecting"},
    )
    monkeypatch.setattr(daily.projection, "run_protocol", run_projection)
    return evaluator_sha


def test_process_day_is_transactional_and_idempotent(tmp_path, monkeypatch):
    calls = {"build": 0, "replay": 0, "upload": 0, "aggregate_tape_path": None}
    _install_daily_fakes(tmp_path, monkeypatch, calls)

    def upload(*_args):
        calls["upload"] += 1
        return []

    monkeypatch.setattr(daily, "_upload_evidence", upload)
    kwargs = {
        "day": DAY,
        "data_dir": tmp_path / "data",
        "poly_dir": tmp_path / "poly",
        "mix_dir": tmp_path / "mix",
        "work_dir": tmp_path / "work",
        "state_dir": tmp_path / "state",
        "s3": object(),
        "bucket": "bucket",
    }

    first = daily.process_day(**kwargs)
    second = daily.process_day(**kwargs)

    assert first == second
    assert calls == {
        "build": 1,
        "replay": 1,
        "upload": 2,
        "aggregate_tape_path": tmp_path / "work" / DAY / "latency" / "binance_trades.jsonl.gz",
    }
    assert (tmp_path / "state" / daily.CONTROL_LEDGER).is_file()
    assert (tmp_path / "state" / daily.PROJECTION_LEDGER).is_file()
    assert not (tmp_path / "work" / DAY / "state-next").exists()
    assert not (tmp_path / "work" / DAY / "artifact").exists()


def test_failed_upload_preserves_staging_without_admission(tmp_path, monkeypatch):
    calls = {"build": 0, "replay": 0, "aggregate_tape_path": None}
    _install_daily_fakes(tmp_path, monkeypatch, calls)
    monkeypatch.setattr(
        daily,
        "_upload_evidence",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("upload failed")),
    )
    state = tmp_path / "state"

    with pytest.raises(RuntimeError, match="upload failed"):
        daily.process_day(
            DAY,
            tmp_path / "data",
            tmp_path / "poly",
            tmp_path / "mix",
            tmp_path / "work",
            state,
            s3=object(),
            bucket="bucket",
        )

    assert not (state / daily.ADMISSION).exists()
    assert not (state / daily.CONTROL_LEDGER).exists()
    assert (tmp_path / "work" / DAY / "state-next" / daily.CONTROL_LEDGER).is_file()

    monkeypatch.setattr(daily, "_upload_evidence", lambda *_args: [])
    recovered = daily.process_day(
        DAY,
        tmp_path / "data",
        tmp_path / "poly",
        tmp_path / "mix",
        tmp_path / "work",
        state,
        s3=object(),
        bucket="bucket",
    )

    assert recovered["status"] == "complete"
    assert calls == {
        "build": 1,
        "replay": 1,
        "aggregate_tape_path": tmp_path / "work" / DAY / "latency" / "binance_trades.jsonl.gz",
    }
    assert (state / daily.ADMISSION).is_file()
    assert (state / daily.CONTROL_LEDGER).is_file()


def test_terminal_requires_two_pinned_terminal_verdicts(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / daily.CONTROL_VERDICT).write_text(
        '{"status":"passed"}\n', encoding="utf-8",
    )
    assert daily._terminal(state) is False
    (state / daily.PROJECTION_VERDICT).write_text(
        '{"status":"rejected"}\n', encoding="utf-8",
    )
    assert daily._terminal(state) is True
    (state / daily.PROJECTION_VERDICT).write_text(
        '{"status":"collecting"}\n', encoding="utf-8",
    )
    with pytest.raises(ValueError, match="non-terminal"):
        daily._terminal(state)


def test_mix_recorder_uses_isolated_historical_endpoint():
    import mix_agg_rec

    specs = mix_agg_rec.source_specs()
    assert tuple(specs) == daily.AGG_SOURCES
    assert all(spec["g"] == "mix-agg" for spec in specs.values())
    assert all("data-stream.binance.vision/ws/btcusdt@aggTrade" in spec["url"]
               for spec in specs.values())
