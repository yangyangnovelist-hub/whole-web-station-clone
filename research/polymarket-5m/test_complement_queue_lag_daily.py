from __future__ import annotations

import copy
import csv
import gzip
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import complement_queue_lag_daily as daily
import pytest

DAY = "2026-10-10"
NEXT_DAY = "2026-10-11"
DAY_MS = 86_400_000.0
ROOT = Path(__file__).resolve().parent
ARTIFACT_FILES = (
    "manifest.json",
    "source_events.jsonl.gz",
    "clob_events.jsonl.gz",
    "market_registry.csv.gz",
    "market_outcomes.csv.gz",
)
DAILY_DEPENDENCIES = (
    "complement_queue_lag.py",
    "complement_queue_lag_daily.py",
    "complement_queue_lag_forward.py",
    "h_replay_archive.py",
    "mix-runtime-requirements.txt",
    "mix_runtime.py",
    "source_race_daily.py",
    "strict_prebuild.py",
    "systemd/complement-queue-lag-daily.service",
    "systemd/complement-queue-lag-daily.timer",
)
FORWARD_DEPENDENCIES = (
    "complement_queue_lag.py",
    "complement_queue_lag_forward.py",
    "h_forward.py",
    "h_replay.py",
    "h_replay_archive.py",
    "h_replay_run.py",
    "source_race_forward.py",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _day_start(day: str) -> float:
    return (
        datetime.combine(
            date.fromisoformat(day), datetime.min.time(), tzinfo=UTC
        ).timestamp()
        * 1_000.0
    )


def _forward_freeze() -> dict[str, Any]:
    return {
        "schema": "complement-queue-lag-forward-freeze-v1",
        "strategy_id": "FAMILY34-COMPLEMENT-QUEUE-LAG-V1",
        "paper_only": True,
        "prospective_cutoff_ms": _day_start(DAY),
        "dependencies_sha256": {
            name: _sha256(ROOT / name) for name in FORWARD_DEPENDENCIES
        },
        "execution": {
            "minimum_queue_shares": 5.0,
            "minimum_midpoint_move": 0.01,
            "maximum_spread_widen": 0.0,
            "confirmation_ms": 250.0,
            "target_shares": 5.0,
            "minimum_total_edge": 0.05,
            "minimum_order_usd": 1.0,
            "minimum_remaining_s": 30.0,
            "maximum_remaining_s": 240.0,
            "book_fresh_ms": 2_000.0,
            "conservative_limit_tick": 0.01,
            "evaluation_ms": [400.0, 500.0],
            "delayed_control_ms": 5_000.0,
        },
        "statistics": {
            "minimum_full_fills_per_latency": 1_000,
            "minimum_utc_days": 7,
            "family_tests": 100,
            "one_sided_alpha": 0.01,
        },
    }


def frozen() -> dict[str, Any]:
    return {
        "schema": "complement-queue-lag-daily-freeze-v1",
        "artifact": {
            "schema": "polymarket-5m-strict-replay-v2",
            "collector_region": "eu-west-1",
            "receipt_race_ready": True,
            "physical_batch_encoding": "price-change-batch-v1",
            "maximum_boundary_lag_ms": 60_000.0,
            "hash_algorithm": "sha256",
            "required_files": list(ARTIFACT_FILES),
        },
        "dependencies_sha256": {
            name: _sha256(ROOT / name) for name in DAILY_DEPENDENCIES
        },
        "forward": _forward_freeze(),
    }


def _gzip_rows(path: Path, fields: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    with gzip.open(path, "wt", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def artifact(
    root: Path,
    day: str = DAY,
    *,
    tag: str = "a",
    started_offset_ms: float = 1_000.0,
    ended_offset_ms: float = DAY_MS - 1_000.0,
    markets: tuple[str, ...] = ("m1",),
    outcomes: tuple[str, ...] | None = None,
    receipt_ready: bool = True,
    physical_encoding: str = "price-change-batch-v1",
    run_id: str | None = None,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    start = _day_start(day)
    with gzip.open(root / "source_events.jsonl.gz", "wt", encoding="utf-8") as stream:
        stream.write(json.dumps({"tag": tag}) + "\n")
    with gzip.open(root / "clob_events.jsonl.gz", "wt", encoding="utf-8") as stream:
        stream.write(json.dumps({"tag": tag}) + "\n")
    _gzip_rows(
        root / "market_registry.csv.gz",
        (
            "market_id",
            "start_ts",
            "up_token_id",
            "down_token_id",
            "updated_at",
            "fee_rate",
        ),
        [
            {
                "market_id": market,
                "start_ts": int(start / 1_000) + index * 300,
                "up_token_id": f"{market}-up",
                "down_token_id": f"{market}-down",
                "updated_at": int(start / 1_000),
                "fee_rate": 0.07,
            }
            for index, market in enumerate(markets)
        ],
    )
    settled = markets if outcomes is None else outcomes
    _gzip_rows(
        root / "market_outcomes.csv.gz",
        ("market_id", "winner", "resolution_ts", "source", "recorded_at"),
        [
            {
                "market_id": market,
                "winner": "Up",
                "resolution_ts": int(start / 1_000) + 300,
                "source": "fixture",
                "recorded_at": int(start / 1_000) + 301,
            }
            for market in settled
        ],
    )
    payload_files = {
        name: {
            "bytes": (root / name).stat().st_size,
            "sha256": _sha256(root / name),
        }
        for name in ARTIFACT_FILES
        if name != "manifest.json"
    }
    manifest = {
        "schema": "polymarket-5m-strict-replay-v2",
        "run_id": run_id or f"eu-west-{day.replace('-', '')}",
        "collector_region": "eu-west-1",
        "complete": True,
        "receipt_race_ready": receipt_ready,
        "runtime": {"clob_storage_encoding": physical_encoding},
        "converter_validation": {
            "schema": "eu-strict-converter-validation-v1",
            "files": payload_files,
        },
        "completeness_checks": {
            "market_outcomes": bool(settled),
            "all_market_outcomes": len(settled) == len(markets),
            "full_day": True,
        },
        "failure_reasons": [],
        "receipt_race_failure_reasons": [] if receipt_ready else ["fixture"],
        "started_ms": start + started_offset_ms,
        "ended_ms": start + ended_offset_ms,
        "counts": {"markets": len(markets), "outcomes": len(settled)},
        "missing_resolved_market_ids": [],
        "unresolved_market_ids": list(set(markets) - set(settled)),
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    binding = {
        "schema": "eu-strict-input-binding-v1",
        "day": day.replace("-", ""),
        "raw_archive_manifests_sha256": hashlib.sha256(
            f"{day}:{tag}".encode()
        ).hexdigest(),
        "strict_manifest_sha256": _sha256(root / "manifest.json"),
    }
    (root / "input-binding.json").write_text(
        json.dumps(binding, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    snapshot = _snapshot_path(root)
    snapshot.write_text(
        json.dumps(
            {
                "rec": {"day": day.replace("-", ""), "group": "rec", "files": []},
                "poly": {"day": day.replace("-", ""), "group": "poly", "files": []},
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    binding["raw_archive_manifests_sha256"] = daily.source_daily._raw_archive_identity(
        snapshot
    )
    (root / "input-binding.json").write_text(
        json.dumps(binding, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return root


def _snapshot_path(archive: Path) -> Path:
    return archive.parent / f".{archive.name}-archive-manifests.json"


def _process_day(
    day: str,
    archive: str | Path,
    state: str | Path,
    **kwargs: Any,
) -> dict[str, Any]:
    kwargs.setdefault("archive_snapshot", _snapshot_path(Path(archive)))
    return daily.process_day(day, archive, state, **kwargs)


@pytest.fixture(autouse=True)
def closed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daily, "_utc_today", lambda: "2026-10-20")
    monkeypatch.setattr(
        daily.mix_runtime,
        "verify_runtime",
        lambda **_kwargs: {"status": "ok"},
    )


@pytest.fixture
def fake_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, list[dict[str, Any]]]:
    calls: dict[str, list[dict[str, Any]]] = {"replay": [], "forward": []}

    def fake_replay(
        archive_dir: str | Path,
        *,
        rows_out: str | Path | None = None,
        config: object | None = None,
    ) -> dict[str, Any]:
        assert rows_out is not None
        rows_path = Path(rows_out)
        assert ".stage-" in rows_path.as_posix()
        rows_path.write_text('{"fixture":true}\n', encoding="utf-8")
        calls["replay"].append(
            {"artifact": str(archive_dir), "rows": str(rows_path), "config": config}
        )
        return {"record_count": 1, "dataset": {"receipt_race_ready": True}}

    def fake_update(
        rows_path: str | Path,
        state_dir: str | Path,
        *,
        frozen: dict[str, Any],
        artifact_sha256: str,
        expected_utc_day: str,
        artifact_start_ms: float,
        artifact_end_ms: float,
    ) -> dict[str, Any]:
        state = Path(state_dir)
        assert ".stage-" in state.as_posix()
        state.mkdir(parents=True, exist_ok=True)
        prior = (
            (state / daily.forward.LEDGER).read_text()
            if (state / daily.forward.LEDGER).is_file()
            else ""
        )
        (state / daily.forward.LEDGER).write_text(
            prior + artifact_sha256 + "\n", encoding="utf-8"
        )
        (state / daily.forward.STATUS).write_text(
            json.dumps({"status": "collecting", "artifact": artifact_sha256}) + "\n",
            encoding="utf-8",
        )
        (state / daily.forward.VERDICT).unlink(missing_ok=True)
        calls["forward"].append(
            {
                "rows": str(rows_path),
                "state": str(state),
                "frozen": frozen,
                "artifact_sha256": artifact_sha256,
                "expected_utc_day": expected_utc_day,
                "artifact_start_ms": artifact_start_ms,
                "artifact_end_ms": artifact_end_ms,
            }
        )
        return {"status": "collecting", "common_cutoff_ms": None}

    monkeypatch.setattr(daily.replay, "replay_archive", fake_replay)
    monkeypatch.setattr(daily.forward, "update", fake_update)
    return calls


def _current_generation(state: Path) -> tuple[dict[str, Any], Path]:
    pointer = json.loads((state / daily.CURRENT).read_text(encoding="utf-8"))
    return pointer, state / daily.GENERATIONS / pointer["generation"]


def test_same_artifact_day_is_idempotent_and_different_duplicate_is_rejected(
    tmp_path: Path,
    fake_pipeline: dict[str, list[dict[str, Any]]],
) -> None:
    archive = artifact(tmp_path / "artifact")
    state = tmp_path / "state"
    config = frozen()

    first = _process_day(DAY, archive, state, frozen=config)
    pointer_before = (state / daily.CURRENT).read_bytes()
    second = _process_day(DAY, archive, state, frozen=config)

    assert second == first
    assert len(fake_pipeline["replay"]) == 1
    assert len(fake_pipeline["forward"]) == 1
    assert (state / daily.CURRENT).read_bytes() == pointer_before
    pointer, generation = _current_generation(state)
    assert pointer["generation_manifest_sha256"] == _sha256(
        generation / daily.GENERATION_MANIFEST
    )
    committed = json.loads(
        (generation / daily.COMMITTED_DAYS).read_text(encoding="utf-8")
    )
    assert [item["day"] for item in committed["days"]] == [DAY]
    assert committed["days"][0]["artifact_sha256"] == first["artifact_sha256"]
    assert set(committed["days"][0]["artifact_files_sha256"]) == set(ARTIFACT_FILES)
    assert committed["days"][0]["artifact_binding_sha256"] == _sha256(
        archive / "input-binding.json"
    )

    artifact(archive, tag="different")
    with pytest.raises(ValueError, match="duplicate UTC day.*different artifact"):
        _process_day(DAY, archive, state, frozen=config)


def test_daily_module_is_frozen_and_bound_artifact_can_reuse_verified_snapshot(
    tmp_path: Path,
    fake_pipeline: dict[str, list[dict[str, Any]]],
) -> None:
    assert "complement_queue_lag_daily.py" in daily.DEPENDENCY_FILES
    archive = artifact(tmp_path / "artifact")
    snapshot = tmp_path / "archive-manifests.json"
    snapshot.write_text(
        json.dumps(
            {
                "rec": {"day": DAY.replace("-", ""), "group": "rec", "files": []},
                "poly": {"day": DAY.replace("-", ""), "group": "poly", "files": []},
            }
        ),
        encoding="utf-8",
    )
    binding_path = archive / "input-binding.json"
    binding = json.loads(binding_path.read_text(encoding="utf-8"))
    binding["raw_archive_manifests_sha256"] = daily.source_daily._raw_archive_identity(
        snapshot
    )
    binding_path.write_text(
        json.dumps(binding, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    result = _process_day(
        DAY,
        archive,
        tmp_path / "state",
        frozen=frozen(),
        archive_snapshot=snapshot,
    )

    assert result["status"] == "complete"
    assert len(fake_pipeline["replay"]) == 1

    changed_snapshot = json.loads(snapshot.read_text(encoding="utf-8"))
    changed_snapshot["poly"]["tampered"] = True
    snapshot.write_text(json.dumps(changed_snapshot), encoding="utf-8")
    with pytest.raises(ValueError, match="verified hourly archive binding"):
        _process_day(
            DAY,
            archive,
            tmp_path / "other-state",
            frozen=frozen(),
            archive_snapshot=snapshot,
        )


def test_runtime_lock_is_enforced_before_any_admission(
    tmp_path: Path,
    fake_pipeline: dict[str, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = artifact(tmp_path / "artifact")

    def reject_runtime(**_kwargs: object) -> dict[str, Any]:
        raise RuntimeError("frozen runtime mismatch")

    monkeypatch.setattr(daily.mix_runtime, "verify_runtime", reject_runtime)
    with pytest.raises(RuntimeError, match="frozen runtime mismatch"):
        _process_day(DAY, archive, tmp_path / "state", frozen=frozen())

    assert fake_pipeline["replay"] == []
    assert fake_pipeline["forward"] == []


def test_unbound_workflow_artifact_is_never_admitted(
    tmp_path: Path,
    fake_pipeline: dict[str, list[dict[str, Any]]],
) -> None:
    archive = artifact(tmp_path / "artifact")
    (archive / "input-binding.json").unlink()

    with pytest.raises(ValueError, match="verified raw-day input binding"):
        _process_day(DAY, archive, tmp_path / "state", frozen=frozen())

    assert fake_pipeline["replay"] == []
    assert fake_pipeline["forward"] == []


def test_external_archive_snapshot_is_mandatory_for_new_admission(
    tmp_path: Path,
    fake_pipeline: dict[str, list[dict[str, Any]]],
) -> None:
    archive = artifact(tmp_path / "artifact")

    with pytest.raises(ValueError, match="verified archive snapshot is required"):
        daily.process_day(DAY, archive, tmp_path / "state", frozen=frozen())

    embedded = archive / "archive-manifests.json"
    embedded.write_bytes(_snapshot_path(archive).read_bytes())
    with pytest.raises(ValueError, match="outside the submitted artifact"):
        daily.process_day(
            DAY,
            archive,
            tmp_path / "other-state",
            frozen=frozen(),
            archive_snapshot=embedded,
        )

    assert fake_pipeline["replay"] == []
    assert fake_pipeline["forward"] == []


def test_crash_after_generation_rename_keeps_old_pointer_and_recovers(
    tmp_path: Path,
    fake_pipeline: dict[str, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = frozen()
    state = tmp_path / "state"
    first = _process_day(
        DAY, artifact(tmp_path / "day-one", DAY), state, frozen=config
    )
    pointer_before = (state / daily.CURRENT).read_bytes()
    real_publish: Callable[..., None] = daily._publish_current

    def crash_before_pointer(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated pointer crash")

    monkeypatch.setattr(daily, "_publish_current", crash_before_pointer)
    day_two = artifact(tmp_path / "day-two", NEXT_DAY)
    with pytest.raises(RuntimeError, match="pointer crash"):
        _process_day(NEXT_DAY, day_two, state, frozen=config)

    assert (state / daily.CURRENT).read_bytes() == pointer_before
    current = daily.read_current(state, frozen=config)
    assert current is not None
    assert current["generation"] == first["generation"]
    assert len(list((state / daily.GENERATIONS).iterdir())) == 2

    monkeypatch.setattr(daily, "_publish_current", real_publish)
    recovered = _process_day(NEXT_DAY, day_two, state, frozen=config)

    assert recovered["day"] == NEXT_DAY
    current = daily.read_current(state, frozen=config)
    assert current is not None
    assert current["generation"] == recovered["generation"]
    assert len(list((state / daily.GENERATIONS).iterdir())) == 2
    assert len(fake_pipeline["forward"]) == 3


def test_partial_forward_write_is_never_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = artifact(tmp_path / "artifact")
    state = tmp_path / "state"

    def fake_replay(
        _artifact: str | Path,
        *,
        rows_out: str | Path | None = None,
        config: object | None = None,
    ) -> dict[str, Any]:
        assert rows_out is not None and config is not None
        Path(rows_out).write_text("{}\n", encoding="utf-8")
        return {"record_count": 1}

    def broken_update(
        _rows: str | Path,
        staged: str | Path,
        **_kwargs: object,
    ) -> dict[str, Any]:
        target = Path(staged)
        target.mkdir(parents=True, exist_ok=True)
        (target / daily.forward.LEDGER).write_text("partial\n", encoding="utf-8")
        raise RuntimeError("forward crashed")

    monkeypatch.setattr(daily.replay, "replay_archive", fake_replay)
    monkeypatch.setattr(daily.forward, "update", broken_update)

    with pytest.raises(RuntimeError, match="forward crashed"):
        _process_day(DAY, archive, state, frozen=frozen())

    assert not (state / daily.CURRENT).exists()
    assert not (state / daily.GENERATIONS).exists() or not list(
        (state / daily.GENERATIONS).iterdir()
    )


def test_real_forward_state_is_published_only_inside_the_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def empty_replay(
        _artifact: str | Path,
        *,
        rows_out: str | Path | None = None,
        config: object | None = None,
    ) -> dict[str, Any]:
        assert rows_out is not None and config is not None
        Path(rows_out).write_text("", encoding="utf-8")
        return {"record_count": 0}

    monkeypatch.setattr(daily.replay, "replay_archive", empty_replay)
    config = frozen()
    state = tmp_path / "state"

    result = _process_day(
        DAY, artifact(tmp_path / "artifact"), state, frozen=config
    )

    assert result["forward_status"]["status"] == "collecting"
    _pointer, generation = _current_generation(state)
    rows, status = daily.forward._load_state(generation, config["forward"])
    assert rows == []
    assert status is not None
    assert status["verdict"]["status"] == "collecting"
    assert not (generation / daily.forward.VERDICT).exists()
    assert all(not (state / name).exists() for name in daily._STATE_FILES)


def test_terminal_verdict_is_committed_with_ledger_and_status_as_one_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"replay": 0, "forward": 0}

    def fake_replay(
        _artifact: str | Path,
        *,
        rows_out: str | Path | None = None,
        config: object | None = None,
    ) -> dict[str, Any]:
        assert rows_out is not None and config is not None
        calls["replay"] += 1
        Path(rows_out).write_text("{}\n", encoding="utf-8")
        return {"record_count": 1}

    def terminal_update(
        _rows: str | Path,
        state_dir: str | Path,
        **_kwargs: object,
    ) -> dict[str, Any]:
        calls["forward"] += 1
        state = Path(state_dir)
        (state / daily.forward.LEDGER).write_text("ledger\n", encoding="utf-8")
        (state / daily.forward.STATUS).write_text("status\n", encoding="utf-8")
        (state / daily.forward.VERDICT).write_text("verdict\n", encoding="utf-8")
        return {"status": "passed", "common_cutoff_ms": _day_start(DAY) + 1_000}

    monkeypatch.setattr(daily.replay, "replay_archive", fake_replay)
    monkeypatch.setattr(daily.forward, "update", terminal_update)
    state = tmp_path / "state"
    config = frozen()

    committed = _process_day(
        DAY, artifact(tmp_path / "artifact"), state, frozen=config
    )
    _pointer, generation = _current_generation(state)

    assert committed["forward_status"]["status"] == "passed"
    assert all((generation / name).is_file() for name in daily._STATE_FILES)
    pending_day, terminal = daily._pending_day_or_terminal(
        state, daily._validate_config(config)
    )
    assert pending_day is None
    assert terminal is not None and terminal["status"] == "passed"
    stopped = _process_day(NEXT_DAY, tmp_path / "not-read", state, frozen=config)
    assert stopped["status"] == "terminal"
    assert calls == {"replay": 1, "forward": 1}


def test_automatic_runner_is_bound_to_the_shared_strict_artifact_and_retries() -> None:
    service = (
        ROOT / "systemd" / "complement-queue-lag-daily.service"
    ).read_text(encoding="utf-8")
    timer = (
        ROOT / "systemd" / "complement-queue-lag-daily.timer"
    ).read_text(encoding="utf-8")

    assert "After=network-online.target time-sync.target strict-artifact-daily.service" in service
    assert "STRICT_SHARED_ROOT=/home/ubuntu/rec/formal/strict-days" in service
    assert "complement_queue_lag_daily.py" in service
    assert "MemoryMax=4G" in service
    assert "OnCalendar=*-*-* *:35:00 UTC" in timer
    assert "Persistent=true" in timer


def test_config_dependency_and_committed_generation_drift_fail_closed(
    tmp_path: Path,
    fake_pipeline: dict[str, list[dict[str, Any]]],
) -> None:
    archive = artifact(tmp_path / "artifact")
    state = tmp_path / "state"
    config = frozen()
    _process_day(DAY, archive, state, frozen=config)

    changed_config = copy.deepcopy(config)
    changed_config["artifact"]["maximum_boundary_lag_ms"] = 59_999.0
    with pytest.raises(ValueError, match="immutable daily freeze/config drift"):
        _process_day(DAY, archive, state, frozen=changed_config)

    changed_dependency = copy.deepcopy(config)
    changed_dependency["dependencies_sha256"]["h_replay_archive.py"] = "0" * 64
    with pytest.raises(
        ValueError, match="dependency hash mismatch.*h_replay_archive.py"
    ):
        _process_day(DAY, archive, state, frozen=changed_dependency)

    _pointer, generation = _current_generation(state)
    (generation / daily.forward.STATUS).write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="committed generation state hash mismatch"):
        _process_day(DAY, archive, state, frozen=config)


def test_current_generation_validates_the_entire_ancestor_chain(
    tmp_path: Path,
    fake_pipeline: dict[str, list[dict[str, Any]]],
) -> None:
    state = tmp_path / "state"
    config = frozen()
    _process_day(DAY, artifact(tmp_path / "first", DAY), state, frozen=config)
    _process_day(
        NEXT_DAY,
        artifact(tmp_path / "second", NEXT_DAY),
        state,
        frozen=config,
    )
    _pointer, current = _current_generation(state)
    current_manifest = json.loads(
        (current / daily.GENERATION_MANIFEST).read_text(encoding="utf-8")
    )
    parent = state / daily.GENERATIONS / current_manifest["parent_generation"]
    (parent / daily.forward.LEDGER).write_text("tampered ancestor\n", encoding="utf-8")

    with pytest.raises(ValueError, match="committed generation state hash mismatch"):
        daily.read_current(state, frozen=config)


def test_replay_reads_a_private_snapshot_not_the_live_artifact(
    tmp_path: Path,
    fake_pipeline: dict[str, list[dict[str, Any]]],
) -> None:
    archive = artifact(tmp_path / "artifact")

    result = _process_day(DAY, archive, tmp_path / "state", frozen=frozen())

    replay_root = Path(fake_pipeline["replay"][0]["artifact"])
    assert result["status"] == "complete"
    assert replay_root != archive
    assert ".input-" in replay_root.as_posix()
    assert not replay_root.exists()


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"receipt_ready": False}, "receipt-race-ready"),
        ({"physical_encoding": "logical-events-v1"}, "physical-batch encoding"),
        ({"started_offset_ms": 60_001.0}, "start boundary"),
        ({"ended_offset_ms": DAY_MS - 60_001.0}, "end boundary"),
        ({"ended_offset_ms": 4.5 * 60 * 60 * 1_000}, "end boundary"),
        ({"ended_offset_ms": DAY_MS}, "single UTC day"),
        ({"run_id": "github-workflow-4.5h"}, "UTC-day identity"),
        ({"markets": ("m1", "m2"), "outcomes": ("m1",)}, "missing outcomes"),
    ],
)
def test_artifact_admission_fails_closed(
    tmp_path: Path,
    fake_pipeline: dict[str, list[dict[str, Any]]],
    changes: dict[str, Any],
    message: str,
) -> None:
    archive = artifact(tmp_path / "artifact", **changes)

    with pytest.raises(ValueError, match=message):
        _process_day(DAY, archive, tmp_path / "state", frozen=frozen())

    assert fake_pipeline["replay"] == []
    assert fake_pipeline["forward"] == []


def test_days_are_closed_post_cutoff_contiguous_and_non_overlapping(
    tmp_path: Path,
    fake_pipeline: dict[str, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = frozen()
    with pytest.raises(ValueError, match="post-cutoff"):
        _process_day(
            "2026-10-09",
            artifact(tmp_path / "before", "2026-10-09"),
            tmp_path / "state-before",
            frozen=config,
        )

    monkeypatch.setattr(daily, "_utc_today", lambda: DAY)
    with pytest.raises(ValueError, match="closed UTC day"):
        _process_day(
            DAY,
            artifact(tmp_path / "today", DAY),
            tmp_path / "state-today",
            frozen=config,
        )
    monkeypatch.setattr(daily, "_utc_today", lambda: "2026-10-20")

    state = tmp_path / "state"
    _process_day(DAY, artifact(tmp_path / "first", DAY), state, frozen=config)
    with pytest.raises(ValueError, match="next chronological UTC day.*2026-10-11"):
        _process_day(
            "2026-10-12",
            artifact(tmp_path / "skipped", "2026-10-12"),
            state,
            frozen=config,
        )

    _process_day(
        NEXT_DAY,
        artifact(tmp_path / "second", NEXT_DAY),
        state,
        frozen=config,
    )
    _pointer, generation = _current_generation(state)
    committed = json.loads(
        (generation / daily.COMMITTED_DAYS).read_text(encoding="utf-8")
    )
    previous = copy.deepcopy(committed["days"][0])
    previous["ended_ms"] = committed["days"][1]["started_ms"]

    with pytest.raises(ValueError, match="overlaps the prior committed artifact"):
        daily._validate_day_entry(
            committed["days"][1],
            daily._validate_config(config),
            previous,
        )
