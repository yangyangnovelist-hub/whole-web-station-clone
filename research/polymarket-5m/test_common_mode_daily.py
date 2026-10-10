from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import common_mode_daily as daily
import common_mode_residual as detector
import common_mode_run as replay


DAY = "20261011"
ROOT = Path(__file__).resolve().parent


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sidecar_manifest(tmp_path: Path, *, missing: str | None = None) -> dict:
    tmp_path.mkdir(parents=True, exist_ok=True)
    files = []
    for route in daily.RAW_SIDECAR_ROUTES:
        for hour in range(24):
            name = f"{route}.{DAY}T{hour:02d}.txt.gz"
            if name == missing:
                continue
            path = tmp_path / name
            path.write_bytes(f"{route}-{hour}\n".encode())
            files.append({
                "file": name,
                "size": path.stat().st_size,
                "sha256": _sha256(path),
                "uploaded_ok": True,
                "gzip_ok": True,
            })
    return {"day": DAY, "group": "rec", "files": files}


def test_sidecar_binding_requires_all_48_verified_hours(tmp_path):
    manifest = _sidecar_manifest(tmp_path)

    selected, identity = daily.bind_raw_sidecars(manifest, tmp_path, DAY)

    assert list(selected) == list(daily.RAW_SIDECAR_ROUTES)
    assert all(len(paths) == 24 for paths in selected.values())
    assert len(identity) == 64
    assert identity == daily.raw_sidecar_identity(manifest, DAY)

    missing = f"deribit.{DAY}T07.txt.gz"
    incomplete = _sidecar_manifest(tmp_path / "incomplete", missing=missing)
    with pytest.raises(ValueError, match="deribit.*hour coverage"):
        daily.bind_raw_sidecars(incomplete, tmp_path / "incomplete", DAY)


def test_sidecar_binding_rejects_local_hash_drift(tmp_path):
    manifest = _sidecar_manifest(tmp_path)
    target = tmp_path / f"bn_spot.{DAY}T13.txt.gz"
    target.write_bytes(b"changed")

    with pytest.raises(ValueError, match="differs from verified S3 manifest"):
        daily.bind_raw_sidecars(manifest, tmp_path, DAY)


def test_source_merge_is_stable_and_cross_transport_ties_fail_closed():
    strict = iter([
        {"kind": "futures_bbo", "recv_ms": 10.0, "seq": 0, "symbol": "BTC"},
        {"kind": "deribit_quote", "recv_ms": 12.0, "seq": 1, "symbol": "BTC"},
    ])
    spot = iter([
        {"kind": "spot_bbo", "recv_ms": 9.0, "seq": 0, "symbol": "BTC"},
        {"kind": "spot_bbo", "recv_ms": 10.0, "seq": 1, "symbol": "BTC"},
    ])

    merged = list(daily.merge_source_events(strict, spot))
    assert [(row["recv_ms"], row["kind"]) for row in merged] == [
        (9.0, "spot_bbo"),
        (10.0, "futures_bbo"),
        (10.0, "spot_bbo"),
        (12.0, "deribit_quote"),
    ]

    normalized = list(replay._source_and_index_events(merged, ()))
    tied = [row for row in normalized if row["recv_ms"] == 10.0]
    assert len(tied) == 2
    assert all(row["ambiguous_cross_stream_receipt_tie"] for row in tied)


def test_persisted_day_result_is_byte_stable_or_rejected(tmp_path):
    path = tmp_path / "result.json"
    payload = {
        "schema": "common-mode-residual-day-v1",
        "complete": True,
        "day": "2026-10-11",
        "protocol_sha256": "a" * 64,
        "evaluator_sha256": "b" * 64,
        "raw_sidecar_identity_sha256": "c" * 64,
        "strict_manifest_sha256": "d" * 64,
        "rows": [],
    }

    first = daily.persist_day_result(path, payload)
    second = daily.persist_day_result(path, dict(reversed(list(payload.items()))))
    assert first == second == _sha256(path)

    changed = {**payload, "rows": [{"signal_id": "different"}]}
    with pytest.raises(ValueError, match="immutable day result"):
        daily.persist_day_result(path, changed)


def test_protocol_loader_rejects_dependency_drift(tmp_path):
    frozen, digest = daily.load_protocol_freeze()
    assert digest == "462d54a77a8e9f666d8a198fb63f3f9acb856d3db2ce8e54a23bdab81aaa67b4"
    assert daily.detector_config(frozen) == detector.DetectorConfig()

    copied = json.loads(json.dumps(frozen))
    copied["dependencies_sha256"]["common_mode_run.py"] = "0" * 64
    path = tmp_path / "freeze.json"
    path.write_text(json.dumps(copied, sort_keys=True) + "\n", encoding="utf-8")
    path.with_suffix(".sha256").write_text(_sha256(path) + "\n", encoding="ascii")
    with pytest.raises(ValueError, match="dependency drift"):
        daily.load_protocol_freeze(path)


def test_daily_loader_rejects_evaluator_semantic_drift(tmp_path, monkeypatch):
    protocol, protocol_sha = daily.load_protocol_freeze()
    evaluator = json.loads(
        daily.EVALUATOR_FREEZE.read_text(encoding="utf-8")
    )
    evaluator["statistics"]["matched_arms_must_be_complete"] = False
    evaluator["dependencies_sha256"] = {}
    path = tmp_path / "evaluator.json"
    path.write_text(json.dumps(evaluator, sort_keys=True) + "\n", encoding="utf-8")
    path.with_suffix(".sha256").write_text(_sha256(path) + "\n", encoding="ascii")

    calls = []

    def reject(payload, frozen_protocol):
        calls.append((payload, frozen_protocol))
        raise ValueError("common-mode evaluator execution/statistics drift")

    monkeypatch.setattr(
        daily,
        "_forward_module",
        lambda: SimpleNamespace(validate_evaluator_freeze=reject),
    )
    with pytest.raises(ValueError, match="execution/statistics drift"):
        daily.load_evaluator_freeze(protocol, protocol_sha, path)
    assert calls == [(evaluator, protocol)]


def test_frozen_daily_units_run_paper_evaluator_after_shared_prebuild() -> None:
    service_path = ROOT / "systemd" / "common-mode-daily.service"
    timer_path = ROOT / "systemd" / "common-mode-daily.timer"
    service = service_path.read_text(encoding="utf-8")
    timer = timer_path.read_text(encoding="utf-8")
    evaluator = json.loads(daily.EVALUATOR_FREEZE.read_text(encoding="utf-8"))
    dependencies = evaluator["dependencies_sha256"]

    assert "User=ubuntu" in service
    assert "STRICT_SHARED_ROOT=/home/ubuntu/rec/formal/strict-days" in service
    assert "common_mode_daily.py" in service
    assert "MemoryMax=4G" in service
    assert "OnCalendar=*-*-* 03:30:00 UTC" in timer
    assert "Persistent=true" in timer
    assert dependencies["forward/current-h-freeze.json"] == _sha256(
        ROOT / "forward" / "current-h-freeze.json"
    )
    assert dependencies["h_source_race.py"] == _sha256(ROOT / "h_source_race.py")
    assert dependencies["source_race_forward.py"] == _sha256(
        ROOT / "source_race_forward.py"
    )
    assert dependencies["systemd/common-mode-daily.service"] == _sha256(service_path)
    assert dependencies["systemd/common-mode-daily.timer"] == _sha256(timer_path)


def _install_process_fakes(tmp_path, monkeypatch, calls):
    data = tmp_path / "data"
    poly = tmp_path / "poly"
    manifest = _sidecar_manifest(data)
    poly_manifest = {"day": DAY, "group": "poly", "files": [{
        "file": f"poly_clob.{DAY}T00.jsonl.gz",
        "size": 1,
        "sha256": "f" * 64,
        "uploaded_ok": True,
        "gzip_ok": True,
    }]}
    monkeypatch.setattr(daily, "_utc_today", lambda: "20261013")
    monkeypatch.setattr(
        daily,
        "load_evaluator_freeze",
        lambda *_args, **_kwargs: ({}, "e" * 64),
    )
    monkeypatch.setattr(
        daily.archive_daily,
        "_archive_manifest",
        lambda _s3, _bucket, _day, group: manifest if group == "rec" else poly_manifest,
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
        sidecars,
        *,
        day,
        protocol,
        protocol_sha256,
        evaluator_sha256,
        raw_sidecar_identity_sha256,
        archive_manifests_sha256,
        archive_manifests,
        raw_sidecar_binding,
    ):
        calls["replay"] += 1
        return {
            "schema": daily.DAY_SCHEMA,
            "complete": True,
            "day": daily._iso_day(day),
            "collector_region": protocol["collector_region"],
            "protocol_sha256": protocol_sha256,
            "evaluator_sha256": evaluator_sha256,
            "raw_sidecar_identity_sha256": raw_sidecar_identity_sha256,
            "archive_manifests_sha256": archive_manifests_sha256,
            "strict_manifest_sha256": daily._sha256(Path(artifact) / "manifest.json"),
            "artifact_binding_sha256": daily._sha256(
                Path(artifact) / daily.archive_daily.ARTIFACT_BINDING
            ),
            "input_bindings": {
                "archive_manifests": archive_manifests,
                "artifact_binding": {},
                "raw_sidecar_binding": raw_sidecar_binding,
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
        Path(state).mkdir(parents=True, exist_ok=True)
        return {"status": "collecting"}

    monkeypatch.setattr(
        daily,
        "_forward_module",
        lambda: SimpleNamespace(update=update),
    )
    return data, poly


def test_process_day_replays_once_and_reuses_immutable_completion(tmp_path, monkeypatch):
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
    assert calls["update"] == 2
    assert first["pooled_verdict"] == {"status": "collecting"}
    assert not (tmp_path / "work" / DAY / "artifact").exists()


def test_upload_failure_never_admits_forward_state(tmp_path, monkeypatch):
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
    assert calls["update"] == 0
    assert (tmp_path / "work" / DAY / "artifact").is_dir()
    assert not (tmp_path / "state" / daily.DAILY_PENDING).exists()


def test_crash_after_forward_admission_retries_same_day_completion(
    tmp_path, monkeypatch
):
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

    with pytest.raises(RuntimeError, match="after common-mode forward update"):
        daily.process_day(**kwargs, _crash_after_forward_update=True)

    pending = tmp_path / "state" / daily.DAILY_PENDING
    assert pending.is_file()
    assert not (tmp_path / "work" / DAY / "complete.json").exists()

    completed = daily.process_day(**kwargs)

    assert completed["status"] == "complete"
    assert calls["replay"] == 1
    assert calls["update"] == 2
    assert not pending.exists()
