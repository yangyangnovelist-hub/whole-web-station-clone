"""Atomic UTC-day admission for the Family 34 paper-forward ledger."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import complement_queue_lag as replay
import complement_queue_lag_forward as forward
import h_replay_archive as archive
import mix_runtime
import source_race_daily as source_daily

CURRENT = "CURRENT"
GENERATIONS = "generations"
COMMITTED_DAYS = "committed-days.json"
GENERATION_MANIFEST = "generation.json"

FREEZE_SCHEMA = "complement-queue-lag-daily-freeze-v1"
COMMITTED_DAYS_SCHEMA = "complement-queue-lag-committed-days-v1"
GENERATION_SCHEMA = "complement-queue-lag-generation-v1"
POINTER_SCHEMA = "complement-queue-lag-current-v1"

STANDARD_SCHEMA = "polymarket-5m-strict-replay-v2"
PHYSICAL_BATCH_ENCODING = "price-change-batch-v1"
HASH_ALGORITHM = "sha256"

ROOT = Path(__file__).resolve().parent
FREEZE_PATH = ROOT / "forward" / "complement-queue-lag-freeze.json"
DEFAULT_SHARED_ROOT = "/home/ubuntu/rec/formal/strict-days"
DEFAULT_STATE_DIR = "/home/ubuntu/rec/formal/complement-queue-lag-state"
_DAY_MS = 86_400_000.0
_DIGEST = re.compile(r"[0-9a-f]{64}")
_GENERATION_NAME = re.compile(r"[0-9]{8}-[0-9a-f]{64}")
_ARTIFACT_FILES = (
    "manifest.json",
    "source_events.jsonl.gz",
    "clob_events.jsonl.gz",
    "market_registry.csv.gz",
    "market_outcomes.csv.gz",
)
DEPENDENCY_FILES = frozenset(
    {
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
    }
)
_DEPENDENCY_FILES = DEPENDENCY_FILES
_STATE_FILES = (forward.LEDGER, forward.STATUS, forward.VERDICT)
_FREEZE_FIELDS = frozenset({"schema", "artifact", "dependencies_sha256", "forward"})
_ARTIFACT_CONFIG_FIELDS = frozenset(
    {
        "schema",
        "collector_region",
        "receipt_race_ready",
        "physical_batch_encoding",
        "maximum_boundary_lag_ms",
        "hash_algorithm",
        "required_files",
    }
)
_DAY_FIELDS = frozenset(
    {
        "day",
        "artifact_sha256",
        "artifact_files_sha256",
        "artifact_binding_sha256",
        "archive_identity_sha256",
        "archive_snapshot_sha256",
        "strict_manifest_sha256",
        "collector_region",
        "started_ms",
        "ended_ms",
        "rows_sha256",
        "row_count",
        "forward_status",
        "state_sha256",
        "day_sha256",
    }
)
_COMMITTED_FIELDS = frozenset(
    {
        "schema",
        "config_sha256",
        "forward_config_sha256",
        "days",
        "manifest_sha256",
    }
)
_GENERATION_FIELDS = frozenset(
    {
        "schema",
        "sequence",
        "parent_generation",
        "parent_generation_manifest_sha256",
        "config_sha256",
        "forward_config_sha256",
        "committed_days_sha256",
        "state_sha256",
        "generation_sha256",
    }
)
_POINTER_FIELDS = frozenset(
    {
        "schema",
        "generation",
        "generation_manifest_sha256",
        "config_sha256",
        "forward_config_sha256",
        "pointer_sha256",
    }
)
_EU_STRICT_BINDING_FIELDS = frozenset(
    {
        "schema",
        "day",
        "raw_archive_manifests_sha256",
        "strict_manifest_sha256",
    }
)
_SOURCE_RACE_BINDING_FIELDS = frozenset(
    {
        "schema",
        "day",
        "archive_manifests_sha256",
        "raw_archive_manifests_sha256",
        "strict_manifest_sha256",
    }
)


@dataclass(frozen=True)
class _Protocol:
    config: dict[str, Any]
    config_sha256: str
    forward_config: dict[str, Any]
    forward_config_sha256: str
    collector_region: str
    maximum_boundary_lag_ms: float
    first_day: str
    cutoff_ms: float


@dataclass(frozen=True)
class _ArtifactIdentity:
    root: Path
    binding_path: Path
    digest: str
    files: dict[str, str]
    binding_sha256: str
    archive_identity_sha256: str
    archive_snapshot_sha256: str
    collector_region: str
    started_ms: float
    ended_ms: float


@dataclass(frozen=True)
class _CurrentGeneration:
    pointer: dict[str, Any]
    path: Path
    generation: dict[str, Any]
    committed: dict[str, Any]


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    return source_daily._sha256(path)


def _digest(value: Any, label: str) -> str:
    text = str(value).lower()
    if _DIGEST.fullmatch(text) is None:
        raise ValueError(f"{label} must be a SHA-256 digest")
    return text


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{label} is not numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{label} is not numeric") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} is non-finite")
    return number


def _require_exact_keys(
    value: Mapping[str, Any], expected: frozenset[str], label: str
) -> None:
    actual = frozenset(value)
    if actual == expected:
        return
    missing = sorted(expected - actual)
    extra = sorted(repr(key) for key in actual - expected)
    details: list[str] = []
    if missing:
        details.append(f"missing {', '.join(missing)}")
    if extra:
        details.append(f"unexpected {', '.join(extra)}")
    raise ValueError(f"Family34 daily {label} keys drift: {'; '.join(details)}")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"invalid JSON constant {value}")
            ),
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"invalid Family34 daily JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise TypeError(f"expected a JSON object: {path}")
    return payload


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    encoded = (
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    with temporary.open("wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _verify_embedded_hash(payload: Mapping[str, Any], field: str, label: str) -> None:
    expected = _digest(payload.get(field), f"{label} {field}")
    core = {key: value for key, value in payload.items() if key != field}
    if expected != _canonical_sha256(core):
        raise ValueError(f"Family34 {label} hash mismatch")


def _normalize_day(day: str) -> str:
    text = str(day)
    if re.fullmatch(r"\d{8}", text):
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("Family34 admission day must be a UTC calendar day") from exc
    if parsed.isoformat() != text:
        raise ValueError("Family34 admission day must be a UTC calendar day")
    return text


def _next_day(day: str) -> str:
    return (date.fromisoformat(day) + timedelta(days=1)).isoformat()


def _day_start_ms(day: str) -> float:
    return (
        datetime.combine(
            date.fromisoformat(day), datetime.min.time(), tzinfo=UTC
        ).timestamp()
        * 1_000.0
    )


def _first_full_day(cutoff_ms: float) -> str:
    instant = datetime.fromtimestamp(cutoff_ms / 1_000.0, tz=UTC)
    day = instant.date()
    if cutoff_ms > _day_start_ms(day.isoformat()):
        day += timedelta(days=1)
    return day.isoformat()


def _utc_today() -> str:
    return datetime.now(UTC).date().isoformat()


def _validate_config(frozen: Mapping[str, Any]) -> _Protocol:
    if not isinstance(frozen, Mapping):
        raise TypeError("Family34 daily freeze/config must be an object")
    _require_exact_keys(frozen, _FREEZE_FIELDS, "freeze/config")
    if frozen.get("schema") != FREEZE_SCHEMA:
        raise ValueError("Family34 daily freeze/config schema drift")

    dependencies = frozen.get("dependencies_sha256")
    if not isinstance(dependencies, Mapping):
        raise TypeError("Family34 daily dependency hashes must be an object")
    _require_exact_keys(dependencies, _DEPENDENCY_FILES, "dependency hashes")
    for name in sorted(_DEPENDENCY_FILES):
        expected = _digest(dependencies[name], f"dependency hash {name}")
        path = ROOT / name
        if not path.is_file() or _file_sha256(path) != expected:
            raise ValueError(f"Family34 daily dependency hash mismatch: {name}")
    mix_runtime.verify_runtime(
        runner_identity={
            "sha256": mix_runtime.EXPECTED_MIGRATION_RUNNER_SHA256,
        }
    )

    artifact_config = frozen.get("artifact")
    if not isinstance(artifact_config, Mapping):
        raise TypeError("Family34 daily artifact config must be an object")
    _require_exact_keys(artifact_config, _ARTIFACT_CONFIG_FIELDS, "artifact config")
    if artifact_config.get("schema") != STANDARD_SCHEMA:
        raise ValueError("Family34 standard artifact schema drift")
    collector_region = str(artifact_config.get("collector_region") or "")
    if not re.fullmatch(r"[a-z]{2}(?:-[a-z0-9]+)+-\d", collector_region):
        raise ValueError("Family34 collector region is invalid")
    if artifact_config.get("receipt_race_ready") is not True:
        raise ValueError("Family34 receipt readiness must be frozen true")
    if artifact_config.get("physical_batch_encoding") != PHYSICAL_BATCH_ENCODING:
        raise ValueError("Family34 physical-batch encoding freeze drift")
    if artifact_config.get("hash_algorithm") != HASH_ALGORITHM:
        raise ValueError("Family34 artifact hash algorithm drift")
    if artifact_config.get("required_files") != list(_ARTIFACT_FILES):
        raise ValueError("Family34 standard artifact file set drift")
    boundary_lag = _finite(
        artifact_config.get("maximum_boundary_lag_ms"),
        "maximum artifact boundary lag",
    )
    if not 0 <= boundary_lag < _DAY_MS / 2:
        raise ValueError("Family34 artifact boundary lag is invalid")

    forward_config = frozen.get("forward")
    if not isinstance(forward_config, Mapping):
        raise TypeError("Family34 forward freeze must be an object")
    materialized_forward = dict(forward_config)
    forward_protocol = forward._protocol(materialized_forward)
    cutoff_ms = _finite(forward_protocol["cutoff_ms"], "prospective cutoff")
    materialized = dict(frozen)
    return _Protocol(
        config=materialized,
        config_sha256=_canonical_sha256(materialized),
        forward_config=materialized_forward,
        forward_config_sha256=str(forward_protocol["config_sha256"]),
        collector_region=collector_region,
        maximum_boundary_lag_ms=boundary_lag,
        first_day=_first_full_day(cutoff_ms),
        cutoff_ms=cutoff_ms,
    )


def _artifact_root(path: str | Path) -> Path:
    supplied = Path(path)
    nested = supplied / "strict"
    if (nested / "manifest.json").is_file():
        return nested
    return supplied


def _artifact_file_hashes(root: Path) -> dict[str, str]:
    missing = [name for name in _ARTIFACT_FILES if not (root / name).is_file()]
    if missing:
        raise ValueError(
            "Family34 requires one standard artifact; missing " + ", ".join(missing)
        )
    return {name: _file_sha256(root / name) for name in _ARTIFACT_FILES}


def _validate_input_binding(
    root: Path,
    day: str,
    binding_path: str | Path | None,
    archive_snapshot: str | Path | None,
) -> tuple[Path, str, str, str]:
    binding = (
        root / source_daily.ARTIFACT_BINDING
        if binding_path is None
        else Path(binding_path)
    )
    if not binding.is_file():
        raise ValueError("Family34 artifact lacks a verified raw-day input binding")
    payload = _read_json(binding)
    schema = payload.get("schema")
    if schema == "eu-strict-input-binding-v1":
        _require_exact_keys(payload, _EU_STRICT_BINDING_FIELDS, "artifact binding")
    elif schema == "current-h-source-race-input-v1":
        _require_exact_keys(payload, _SOURCE_RACE_BINDING_FIELDS, "artifact binding")
        _digest(
            payload.get("archive_manifests_sha256"),
            "bound archive snapshot hash",
        )
    else:
        raise ValueError("Family34 artifact lacks a verified raw-day input binding")
    if payload.get("day") != day.replace("-", ""):
        raise ValueError("Family34 raw-day input binding day drift")
    strict_manifest_sha256 = _file_sha256(root / "manifest.json")
    if payload.get("strict_manifest_sha256") != strict_manifest_sha256:
        raise ValueError("Family34 raw-day binding strict manifest hash mismatch")
    raw_identity = _digest(
        payload.get("raw_archive_manifests_sha256"),
        "raw archive identity hash",
    )
    if archive_snapshot is None:
        raise ValueError("Family34 verified archive snapshot is required")
    snapshot = Path(archive_snapshot)
    if not snapshot.is_file():
        raise ValueError("Family34 verified archive snapshot is missing")
    root_resolved = root.resolve()
    snapshot_resolved = snapshot.resolve()
    if snapshot_resolved == root_resolved or root_resolved in snapshot_resolved.parents:
        raise ValueError(
            "Family34 verified archive snapshot must be outside the submitted artifact"
        )
    if binding != root / source_daily.ARTIFACT_BINDING:
        raise ValueError(
            "Family34 archive snapshot verification requires the artifact binding"
        )
    if not source_daily._valid_artifact(root, day.replace("-", ""), snapshot):
        raise ValueError(
            "Family34 artifact differs from its verified hourly archive binding"
        )
    snapshot_sha256 = _file_sha256(snapshot)
    return binding, _file_sha256(binding), raw_identity, snapshot_sha256


@contextmanager
def _private_input_snapshot(
    state: Path,
    artifact_path: str | Path,
    *,
    binding_path: str | Path | None,
    archive_snapshot: str | Path | None,
) -> Iterator[tuple[Path, Path]]:
    """Copy one immutable replay view and reject any source drift during copying."""
    source_root = _artifact_root(artifact_path)
    source_binding = (
        source_root / source_daily.ARTIFACT_BINDING
        if binding_path is None
        else Path(binding_path)
    )
    if archive_snapshot is None:
        raise ValueError("Family34 verified archive snapshot is required")
    source_snapshot = Path(archive_snapshot)
    if not source_binding.is_file():
        raise ValueError("Family34 artifact lacks a verified raw-day input binding")
    source_root_resolved = source_root.resolve()
    snapshot_resolved = source_snapshot.resolve()
    if (
        snapshot_resolved == source_root_resolved
        or source_root_resolved in snapshot_resolved.parents
    ):
        raise ValueError(
            "Family34 verified archive snapshot must be outside the submitted artifact"
        )
    sources = {
        name: source_root / name for name in _ARTIFACT_FILES
    } | {
        source_daily.ARTIFACT_BINDING: source_binding,
        "archive-manifests.json": source_snapshot,
    }
    missing = [name for name, path in sources.items() if not path.is_file()]
    if missing:
        raise ValueError(
            "Family34 immutable input snapshot is incomplete: " + ", ".join(missing)
        )
    before = {name: _file_sha256(path) for name, path in sources.items()}
    stage = Path(tempfile.mkdtemp(prefix=".input-", dir=state))
    snapshot_root = stage / "artifact"
    snapshot_root.mkdir()
    try:
        for name in _ARTIFACT_FILES:
            shutil.copyfile(sources[name], snapshot_root / name)
        copied_binding = snapshot_root / source_daily.ARTIFACT_BINDING
        shutil.copyfile(sources[source_daily.ARTIFACT_BINDING], copied_binding)
        copied_snapshot = stage / "archive-manifests.json"
        shutil.copyfile(source_snapshot, copied_snapshot)
        after = {name: _file_sha256(path) for name, path in sources.items()}
        copied = {
            name: _file_sha256(snapshot_root / name) for name in _ARTIFACT_FILES
        } | {
            source_daily.ARTIFACT_BINDING: _file_sha256(copied_binding),
            "archive-manifests.json": _file_sha256(copied_snapshot),
        }
        if before != after or copied != before:
            raise ValueError("Family34 artifact changed while creating replay snapshot")
        yield snapshot_root, copied_snapshot
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _validate_time_bounds(
    day: str, started_ms: float, ended_ms: float, maximum_lag_ms: float
) -> None:
    day_start = _day_start_ms(day)
    day_end = day_start + _DAY_MS
    if not day_start <= started_ms <= day_start + maximum_lag_ms:
        raise ValueError("Family34 artifact start boundary exceeds the frozen bound")
    if ended_ms >= day_end:
        raise ValueError("Family34 artifact is not a single UTC day")
    if ended_ms < day_end - maximum_lag_ms:
        raise ValueError("Family34 artifact end boundary exceeds the frozen bound")
    if started_ms > ended_ms:
        raise ValueError("Family34 artifact time bounds are reversed")


def _validate_outcomes(
    root: Path, manifest: Mapping[str, Any], checked_counts: Mapping[str, Any]
) -> None:
    mappings = list(archive.iter_market_mappings(root / "market_registry.csv.gz"))
    outcomes = list(archive.iter_outcomes(root / "market_outcomes.csv.gz"))
    market_ids = [str(row["market_id"]) for row in mappings]
    outcome_ids = [str(row["market_id"]) for row in outcomes]
    missing = sorted(set(market_ids) - set(outcome_ids))
    if (
        not market_ids
        or len(market_ids) != len(set(market_ids))
        or len(outcome_ids) != len(set(outcome_ids))
        or missing
    ):
        raise ValueError(
            "Family34 standard artifact has missing outcomes"
            + (f": {', '.join(missing)}" if missing else "")
        )
    if set(outcome_ids) != set(market_ids):
        raise ValueError("Family34 standard artifact outcome coverage drift")
    counts = manifest.get("counts")
    checks = manifest.get("completeness_checks")
    if (
        not isinstance(counts, Mapping)
        or int(counts.get("markets", -1)) != len(market_ids)
        or int(counts.get("outcomes", -1)) != len(outcome_ids)
        or int(checked_counts.get("markets", -1)) != len(market_ids)
        or int(checked_counts.get("outcomes", -1)) != len(outcome_ids)
        or not isinstance(checks, Mapping)
        or checks.get("market_outcomes") is not True
        or checks.get("all_market_outcomes") is not True
        or manifest.get("unresolved_market_ids") not in ([], ())
        or manifest.get("missing_resolved_market_ids") not in ([], ())
    ):
        raise ValueError("Family34 standard artifact has missing outcomes")


def _inspect_artifact(
    artifact_path: str | Path,
    day: str,
    protocol: _Protocol,
    *,
    binding_path: str | Path | None,
    archive_snapshot: str | Path | None,
) -> _ArtifactIdentity:
    root = _artifact_root(artifact_path)
    checked = archive.validate_standard_artifact(root)
    manifest = checked.get("manifest")
    counts = checked.get("counts")
    if not isinstance(manifest, Mapping) or not isinstance(counts, Mapping):
        raise TypeError("Family34 standard artifact validation result drift")
    if (
        manifest.get("schema") != STANDARD_SCHEMA
        or manifest.get("complete") is not True
    ):
        raise ValueError("Family34 artifact is not a complete standard artifact")
    if manifest.get("collector_region") != protocol.collector_region:
        raise ValueError("Family34 artifact collector region differs from the freeze")
    run_prefix = protocol.collector_region.rsplit("-", 1)[0]
    if manifest.get("run_id") != f"{run_prefix}-{day.replace('-', '')}":
        raise ValueError("Family34 artifact UTC-day identity drift")
    if manifest.get("receipt_race_ready") is not True:
        raise ValueError("Family34 artifact is not receipt-race-ready")
    runtime = manifest.get("runtime")
    if (
        not isinstance(runtime, Mapping)
        or runtime.get("clob_storage_encoding") != PHYSICAL_BATCH_ENCODING
    ):
        raise ValueError("Family34 artifact physical-batch encoding drift")
    checks = manifest.get("completeness_checks")
    if not isinstance(checks, Mapping) or checks.get("full_day") is not True:
        raise ValueError("Family34 artifact does not declare complete UTC-day coverage")
    if manifest.get("failure_reasons") not in ([], ()):
        raise ValueError("Family34 artifact completeness checks failed")
    if manifest.get("receipt_race_failure_reasons") not in ([], ()):
        raise ValueError("Family34 artifact receipt readiness checks failed")

    started_ms = _finite(manifest.get("started_ms"), "artifact started_ms")
    ended_ms = _finite(manifest.get("ended_ms"), "artifact ended_ms")
    _validate_time_bounds(day, started_ms, ended_ms, protocol.maximum_boundary_lag_ms)
    if _day_start_ms(day) < protocol.cutoff_ms:
        raise ValueError(
            "Family34 artifact day is outside the frozen post-cutoff sample"
        )
    _validate_outcomes(root, manifest, counts)
    files = _artifact_file_hashes(root)
    binding, binding_sha256, raw_identity, snapshot_sha256 = _validate_input_binding(
        root, day, binding_path, archive_snapshot
    )
    artifact_digest = _canonical_sha256(
        {
            "files": files,
            "input_binding_sha256": binding_sha256,
            "raw_archive_manifests_sha256": raw_identity,
        }
    )
    return _ArtifactIdentity(
        root=root,
        binding_path=binding,
        digest=artifact_digest,
        files=files,
        binding_sha256=binding_sha256,
        archive_identity_sha256=raw_identity,
        archive_snapshot_sha256=snapshot_sha256,
        collector_region=protocol.collector_region,
        started_ms=started_ms,
        ended_ms=ended_ms,
    )


def _state_hashes(path: Path) -> dict[str, str | None]:
    return {
        name: _file_sha256(path / name) if (path / name).is_file() else None
        for name in _STATE_FILES
    }


def _validate_state_hashes(path: Path, hashes: Mapping[str, Any]) -> None:
    if set(hashes) != set(_STATE_FILES):
        raise ValueError("Family34 committed generation state set drift")
    for name in _STATE_FILES:
        expected = hashes[name]
        target = path / name
        if expected is None:
            if target.exists():
                raise ValueError("Family34 committed generation state hash mismatch")
            continue
        digest = _digest(expected, f"committed state hash {name}")
        if not target.is_file() or _file_sha256(target) != digest:
            raise ValueError("Family34 committed generation state hash mismatch")
    if hashes[forward.LEDGER] is None or hashes[forward.STATUS] is None:
        raise ValueError("Family34 committed generation lacks ledger or status")


def _validate_day_entry(
    entry: Mapping[str, Any], protocol: _Protocol, previous: Mapping[str, Any] | None
) -> None:
    _require_exact_keys(entry, _DAY_FIELDS, "committed-day manifest")
    _verify_embedded_hash(entry, "day_sha256", "committed-day manifest")
    day = _normalize_day(str(entry.get("day")))
    files = entry.get("artifact_files_sha256")
    if not isinstance(files, Mapping) or set(files) != set(_ARTIFACT_FILES):
        raise ValueError("Family34 committed artifact file set drift")
    for name, digest in files.items():
        _digest(digest, f"committed artifact hash {name}")
    artifact_digest = _digest(entry.get("artifact_sha256"), "artifact hash")
    binding_digest = _digest(
        entry.get("artifact_binding_sha256"), "artifact binding hash"
    )
    archive_identity = _digest(
        entry.get("archive_identity_sha256"), "archive identity hash"
    )
    if artifact_digest != _canonical_sha256(
        {
            "files": dict(files),
            "input_binding_sha256": binding_digest,
            "raw_archive_manifests_sha256": archive_identity,
        }
    ):
        raise ValueError("Family34 committed artifact aggregate hash mismatch")
    _digest(entry.get("archive_snapshot_sha256"), "archive snapshot hash")
    strict_manifest_digest = _digest(
        entry.get("strict_manifest_sha256"), "strict manifest hash"
    )
    if strict_manifest_digest != files["manifest.json"]:
        raise ValueError("Family34 committed strict manifest binding drift")
    _digest(entry.get("rows_sha256"), "rows hash")
    if entry.get("collector_region") != protocol.collector_region:
        raise ValueError("Family34 committed collector region drift")
    started_ms = _finite(entry.get("started_ms"), "committed artifact start")
    ended_ms = _finite(entry.get("ended_ms"), "committed artifact end")
    _validate_time_bounds(day, started_ms, ended_ms, protocol.maximum_boundary_lag_ms)
    row_count = entry.get("row_count")
    if isinstance(row_count, bool) or not isinstance(row_count, int) or row_count < 0:
        raise ValueError("Family34 committed row count is invalid")
    if not isinstance(entry.get("forward_status"), Mapping):
        raise TypeError("Family34 committed forward status is invalid")
    state_hashes = entry.get("state_sha256")
    if not isinstance(state_hashes, Mapping) or set(state_hashes) != set(_STATE_FILES):
        raise ValueError("Family34 committed-day state set drift")
    for name, digest in state_hashes.items():
        if digest is not None:
            _digest(digest, f"committed-day state hash {name}")
    if previous is None:
        if day != protocol.first_day:
            raise ValueError(
                "Family34 committed history does not start at the cutoff day"
            )
        return
    expected_day = _next_day(str(previous["day"]))
    if day != expected_day:
        raise ValueError("Family34 committed history is out of order")
    if started_ms <= _finite(previous.get("ended_ms"), "prior artifact end"):
        raise ValueError("Family34 artifact overlaps the prior committed artifact")


def _validate_committed_days(
    payload: Mapping[str, Any], protocol: _Protocol, sequence: int
) -> None:
    _require_exact_keys(payload, _COMMITTED_FIELDS, "committed-days index")
    if payload.get("schema") != COMMITTED_DAYS_SCHEMA:
        raise ValueError("Family34 committed-days schema drift")
    _verify_embedded_hash(payload, "manifest_sha256", "committed-days index")
    if (
        payload.get("config_sha256") != protocol.config_sha256
        or payload.get("forward_config_sha256") != protocol.forward_config_sha256
    ):
        raise ValueError("Family34 immutable daily freeze/config drift")
    days = payload.get("days")
    if not isinstance(days, list) or len(days) != sequence or not days:
        raise ValueError("Family34 committed-day sequence drift")
    previous: Mapping[str, Any] | None = None
    for entry in days:
        if not isinstance(entry, Mapping):
            raise TypeError("Family34 committed-day entry is not an object")
        _validate_day_entry(entry, protocol, previous)
        previous = entry


def _validate_generation(
    path: Path,
    protocol: _Protocol,
    *,
    expected_manifest_sha256: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    generation_path = path / GENERATION_MANIFEST
    committed_path = path / COMMITTED_DAYS
    if not generation_path.is_file() or not committed_path.is_file():
        raise ValueError("Family34 committed generation is incomplete")
    if (
        expected_manifest_sha256 is not None
        and _file_sha256(generation_path) != expected_manifest_sha256
    ):
        raise ValueError("Family34 current generation manifest hash mismatch")
    generation = _read_json(generation_path)
    _require_exact_keys(generation, _GENERATION_FIELDS, "generation manifest")
    if generation.get("schema") != GENERATION_SCHEMA:
        raise ValueError("Family34 generation schema drift")
    _verify_embedded_hash(generation, "generation_sha256", "generation manifest")
    sequence = generation.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
        raise ValueError("Family34 generation sequence is invalid")
    if (
        generation.get("config_sha256") != protocol.config_sha256
        or generation.get("forward_config_sha256") != protocol.forward_config_sha256
    ):
        raise ValueError("Family34 immutable daily freeze/config drift")
    if generation.get("committed_days_sha256") != _file_sha256(committed_path):
        raise ValueError("Family34 committed-days file hash mismatch")
    state_hashes = generation.get("state_sha256")
    if not isinstance(state_hashes, Mapping):
        raise TypeError("Family34 generation state hashes are not an object")
    _validate_state_hashes(path, state_hashes)
    committed = _read_json(committed_path)
    _validate_committed_days(committed, protocol, sequence)
    if committed["days"][-1]["state_sha256"] != dict(state_hashes):
        raise ValueError("Family34 committed-day/generation state hash drift")
    return generation, committed


def _validate_ancestry(
    state: Path,
    path: Path,
    generation: Mapping[str, Any],
    committed: Mapping[str, Any],
    protocol: _Protocol,
) -> None:
    """Validate every immutable ancestor and its exact committed-history prefix."""
    current_path = path
    current_generation = dict(generation)
    current_days = list(committed["days"])
    visited = {current_path.name}
    while int(current_generation["sequence"]) > 1:
        parent_name = str(current_generation.get("parent_generation") or "")
        if _GENERATION_NAME.fullmatch(parent_name) is None or parent_name in visited:
            raise ValueError("Family34 generation ancestry is cyclic or malformed")
        parent_hash = _digest(
            current_generation.get("parent_generation_manifest_sha256"),
            "parent generation manifest hash",
        )
        parent_path = state / GENERATIONS / parent_name
        if not parent_path.is_dir():
            raise ValueError("Family34 parent generation is missing")
        parent_generation, parent_committed = _validate_generation(
            parent_path,
            protocol,
            expected_manifest_sha256=parent_hash,
        )
        if int(parent_generation["sequence"]) != int(current_generation["sequence"]) - 1:
            raise ValueError("Family34 generation ancestry sequence drift")
        parent_days = list(parent_committed["days"])
        if parent_days != current_days[:-1]:
            raise ValueError("Family34 generation ancestry history-prefix drift")
        if parent_name != (
            f"{parent_generation['sequence']:08d}-{parent_generation['generation_sha256']}"
        ):
            raise ValueError("Family34 parent generation name/content mismatch")
        visited.add(parent_name)
        current_path = parent_path
        current_generation = parent_generation
        current_days = parent_days
    if (
        current_generation.get("parent_generation") is not None
        or current_generation.get("parent_generation_manifest_sha256") is not None
        or len(current_days) != 1
    ):
        raise ValueError("Family34 first generation ancestry drift")


def _load_current(state: Path, protocol: _Protocol) -> _CurrentGeneration | None:
    pointer_path = state / CURRENT
    if not pointer_path.exists():
        stray = [name for name in _STATE_FILES if (state / name).exists()]
        if stray:
            raise ValueError("Family34 state exists outside a committed generation")
        return None
    if not pointer_path.is_file():
        raise ValueError("Family34 current pointer is not a file")
    pointer = _read_json(pointer_path)
    _require_exact_keys(pointer, _POINTER_FIELDS, "current pointer")
    if pointer.get("schema") != POINTER_SCHEMA:
        raise ValueError("Family34 current pointer schema drift")
    _verify_embedded_hash(pointer, "pointer_sha256", "current pointer")
    if (
        pointer.get("config_sha256") != protocol.config_sha256
        or pointer.get("forward_config_sha256") != protocol.forward_config_sha256
    ):
        raise ValueError("Family34 immutable daily freeze/config drift")
    generation_name = str(pointer.get("generation") or "")
    if _GENERATION_NAME.fullmatch(generation_name) is None:
        raise ValueError("Family34 current generation name is invalid")
    expected_manifest = _digest(
        pointer.get("generation_manifest_sha256"), "generation manifest hash"
    )
    path = state / GENERATIONS / generation_name
    if not path.is_dir():
        raise ValueError("Family34 current generation is missing")
    generation, committed = _validate_generation(
        path, protocol, expected_manifest_sha256=expected_manifest
    )
    _validate_ancestry(state, path, generation, committed, protocol)
    if (
        generation_name
        != f"{generation['sequence']:08d}-{generation['generation_sha256']}"
    ):
        raise ValueError("Family34 generation name/content mismatch")
    parent_name = generation.get("parent_generation")
    parent_hash = generation.get("parent_generation_manifest_sha256")
    if generation["sequence"] == 1:
        if parent_name is not None or parent_hash is not None:
            raise ValueError("Family34 first generation has an unexpected parent")
    else:
        if (
            not isinstance(parent_name, str)
            or _GENERATION_NAME.fullmatch(parent_name) is None
            or not isinstance(parent_hash, str)
        ):
            raise ValueError("Family34 generation parent binding drift")
        parent_manifest = state / GENERATIONS / parent_name / GENERATION_MANIFEST
        if not parent_manifest.is_file() or _file_sha256(parent_manifest) != _digest(
            parent_hash, "parent generation manifest hash"
        ):
            raise ValueError("Family34 parent generation manifest drift")
    return _CurrentGeneration(pointer, path, generation, committed)


def read_current(
    state_dir: str | Path, *, frozen: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Validate and return the published generation pointer."""
    protocol = _validate_config(frozen)
    current = _load_current(Path(state_dir), protocol)
    return None if current is None else dict(current.pointer)


def _pending_day_or_terminal(
    state_dir: str | Path,
    protocol: _Protocol,
) -> tuple[str | None, dict[str, Any] | None]:
    current = _load_current(Path(state_dir), protocol)
    if current is None:
        return protocol.first_day, None
    latest = dict(current.committed["days"][-1]["forward_status"])
    if latest.get("status") != "collecting":
        return None, latest
    return _next_day(str(current.committed["days"][-1]["day"])), None


def _cleanup_private_staging(state: Path) -> None:
    for path in state.glob(".stage-*"):
        if path.is_dir() and path.parent == state:
            shutil.rmtree(path)
        elif path.is_file() and path.parent == state:
            path.unlink()
    temporary_pointer = state / f".{CURRENT}.tmp"
    temporary_pointer.unlink(missing_ok=True)


@contextmanager
def _locked_state(state: Path) -> Iterator[None]:
    state.mkdir(parents=True, exist_ok=True)
    with (state / ".daily.lock").open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _copy_current_state(current: _CurrentGeneration | None, destination: Path) -> None:
    destination.mkdir(parents=True)
    if current is None:
        return
    for name in _STATE_FILES:
        source = current.path / name
        if source.is_file():
            shutil.copyfile(source, destination / name)


def _replay_config(forward_config: Mapping[str, Any]) -> replay.QueueLagConfig:
    execution = forward_config["execution"]
    if not isinstance(execution, Mapping):
        raise TypeError("Family34 forward execution freeze is not an object")
    return replay.QueueLagConfig(
        minimum_queue_shares=float(execution["minimum_queue_shares"]),
        minimum_midpoint_move=float(execution["minimum_midpoint_move"]),
        maximum_spread_widen=float(execution["maximum_spread_widen"]),
        confirmation_ms=float(execution["confirmation_ms"]),
        target_shares=float(execution["target_shares"]),
        minimum_total_edge=float(execution["minimum_total_edge"]),
        minimum_order_usd=float(execution["minimum_order_usd"]),
        minimum_remaining_s=float(execution["minimum_remaining_s"]),
        maximum_remaining_s=float(execution["maximum_remaining_s"]),
        book_fresh_ms=float(execution["book_fresh_ms"]),
        conservative_limit_tick=float(execution["conservative_limit_tick"]),
        evaluation_ms=tuple(float(value) for value in execution["evaluation_ms"]),
        delayed_control_ms=float(execution["delayed_control_ms"]),
    )


def _count_rows(path: Path) -> int:
    with path.open("rb") as stream:
        return sum(bool(line.strip()) for line in stream)


def _with_hash(payload: Mapping[str, Any], field: str) -> dict[str, Any]:
    result = dict(payload)
    result[field] = _canonical_sha256(result)
    return result


def _build_day_entry(
    *,
    day: str,
    identity: _ArtifactIdentity,
    rows_path: Path,
    row_count: int,
    forward_status: Mapping[str, Any],
    state_sha256: Mapping[str, str | None],
) -> dict[str, Any]:
    return _with_hash(
        {
            "day": day,
            "artifact_sha256": identity.digest,
            "artifact_files_sha256": identity.files,
            "artifact_binding_sha256": identity.binding_sha256,
            "archive_identity_sha256": identity.archive_identity_sha256,
            "archive_snapshot_sha256": identity.archive_snapshot_sha256,
            "strict_manifest_sha256": identity.files["manifest.json"],
            "collector_region": identity.collector_region,
            "started_ms": identity.started_ms,
            "ended_ms": identity.ended_ms,
            "rows_sha256": _file_sha256(rows_path),
            "row_count": row_count,
            "forward_status": dict(forward_status),
            "state_sha256": dict(state_sha256),
        },
        "day_sha256",
    )


def _completion(entry: Mapping[str, Any], generation: str) -> dict[str, Any]:
    return {
        "status": "complete",
        "day": entry["day"],
        "generation": generation,
        "artifact_sha256": entry["artifact_sha256"],
        "day_sha256": entry["day_sha256"],
        "forward_status": dict(entry["forward_status"]),
    }


def _fsync_tree(path: Path) -> None:
    for target in sorted(path.rglob("*")):
        if target.is_file():
            with target.open("rb") as stream:
                os.fsync(stream.fileno())
    directories = [target for target in path.rglob("*") if target.is_dir()]
    for directory in sorted(
        directories, key=lambda item: len(item.parts), reverse=True
    ):
        _fsync_directory(directory)
    _fsync_directory(path)


def _install_generation(
    candidate: Path, state: Path, protocol: _Protocol
) -> tuple[str, Path]:
    manifest = _read_json(candidate / GENERATION_MANIFEST)
    name = f"{manifest['sequence']:08d}-{manifest['generation_sha256']}"
    generations = state / GENERATIONS
    generations.mkdir(exist_ok=True)
    destination = generations / name
    _fsync_tree(candidate)
    if destination.exists():
        existing_generation, existing_committed = _validate_generation(
            destination, protocol
        )
        candidate_generation, candidate_committed = _validate_generation(
            candidate, protocol
        )
        if (
            existing_generation != candidate_generation
            or existing_committed != candidate_committed
            or _state_hashes(destination) != _state_hashes(candidate)
        ):
            raise ValueError("Family34 generation identity collision")
        shutil.rmtree(candidate)
        return name, destination
    os.replace(candidate, destination)
    _fsync_directory(generations)
    return name, destination


def _publish_current(state: Path, pointer_core: Mapping[str, Any]) -> None:
    _atomic_json(state / CURRENT, _with_hash(pointer_core, "pointer_sha256"))


def _stage_generation(
    *,
    day: str,
    identity: _ArtifactIdentity,
    state: Path,
    protocol: _Protocol,
    current: _CurrentGeneration | None,
) -> tuple[dict[str, Any], str, Path]:
    stage_root = Path(tempfile.mkdtemp(prefix=".stage-", dir=state))
    rows_path = stage_root / "rows.jsonl"
    candidate = stage_root / "generation"
    try:
        _copy_current_state(current, candidate)
        replay_result = replay.replay_archive(
            identity.root,
            rows_out=rows_path,
            config=_replay_config(protocol.forward_config),
        )
        if not rows_path.is_file():
            raise ValueError("Family34 replay did not stage observation rows")
        row_count = _count_rows(rows_path)
        if (
            not isinstance(replay_result, Mapping)
            or replay_result.get("record_count") != row_count
        ):
            raise ValueError("Family34 replay row count drift")
        if _artifact_file_hashes(identity.root) != identity.files:
            raise ValueError("Family34 artifact changed while replaying")
        if _file_sha256(identity.binding_path) != identity.binding_sha256:
            raise ValueError("Family34 artifact binding changed while replaying")

        forward_status = forward.update(
            rows_path,
            candidate,
            frozen=protocol.forward_config,
            artifact_sha256=identity.digest,
            expected_utc_day=day,
            artifact_start_ms=identity.started_ms,
            artifact_end_ms=identity.ended_ms,
        )
        if not isinstance(forward_status, Mapping):
            raise TypeError("Family34 forward update did not return a verdict object")
        state_hashes = _state_hashes(candidate)
        if state_hashes[forward.LEDGER] is None or state_hashes[forward.STATUS] is None:
            raise ValueError("Family34 forward update left incomplete staged state")
        terminal = forward_status.get("status") != "collecting"
        if terminal != (state_hashes[forward.VERDICT] is not None):
            raise ValueError("Family34 forward staged verdict lifecycle drift")

        entry = _build_day_entry(
            day=day,
            identity=identity,
            rows_path=rows_path,
            row_count=row_count,
            forward_status=forward_status,
            state_sha256=state_hashes,
        )
        prior_days: Sequence[Any] = (
            current.committed["days"] if current is not None else ()
        )
        committed = _with_hash(
            {
                "schema": COMMITTED_DAYS_SCHEMA,
                "config_sha256": protocol.config_sha256,
                "forward_config_sha256": protocol.forward_config_sha256,
                "days": [*prior_days, entry],
            },
            "manifest_sha256",
        )
        _atomic_json(candidate / COMMITTED_DAYS, committed)
        sequence = len(committed["days"])
        generation = _with_hash(
            {
                "schema": GENERATION_SCHEMA,
                "sequence": sequence,
                "parent_generation": (
                    current.pointer["generation"] if current is not None else None
                ),
                "parent_generation_manifest_sha256": (
                    current.pointer["generation_manifest_sha256"]
                    if current is not None
                    else None
                ),
                "config_sha256": protocol.config_sha256,
                "forward_config_sha256": protocol.forward_config_sha256,
                "committed_days_sha256": _file_sha256(candidate / COMMITTED_DAYS),
                "state_sha256": state_hashes,
            },
            "generation_sha256",
        )
        _atomic_json(candidate / GENERATION_MANIFEST, generation)
        _validate_generation(candidate, protocol)
        generation_name, installed = _install_generation(candidate, state, protocol)
        return entry, generation_name, installed
    finally:
        if stage_root.exists():
            shutil.rmtree(stage_root)


def _process_locked(
    day: str,
    artifact_path: str | Path,
    state: Path,
    protocol: _Protocol,
    binding_path: str | Path | None,
    archive_snapshot: str | Path | None,
) -> dict[str, Any]:
    current = _load_current(state, protocol)
    _cleanup_private_staging(state)
    days: list[dict[str, Any]] = (
        list(current.committed["days"]) if current is not None else []
    )
    existing = next((entry for entry in days if entry["day"] == day), None)
    if existing is not None:
        with _private_input_snapshot(
            state,
            artifact_path,
            binding_path=binding_path,
            archive_snapshot=archive_snapshot,
        ) as (snapshot_root, snapshot_manifest):
            identity = _inspect_artifact(
                snapshot_root,
                day,
                protocol,
                binding_path=None,
                archive_snapshot=snapshot_manifest,
            )
            if (
                existing["artifact_sha256"] != identity.digest
                or existing["artifact_files_sha256"] != identity.files
            ):
                raise ValueError("Family34 duplicate UTC day names a different artifact")
            assert current is not None
            return _completion(existing, str(current.pointer["generation"]))

    expected_day = _next_day(str(days[-1]["day"])) if days else protocol.first_day
    if day != expected_day:
        raise ValueError(
            f"Family34 day is not the next chronological UTC day: expected {expected_day}"
        )
    if current is not None:
        latest_status = current.committed["days"][-1]["forward_status"]
        if latest_status.get("status") != "collecting":
            return {
                "status": "terminal",
                "day": days[-1]["day"],
                "generation": current.pointer["generation"],
                "forward_status": dict(latest_status),
            }

    with _private_input_snapshot(
        state,
        artifact_path,
        binding_path=binding_path,
        archive_snapshot=archive_snapshot,
    ) as (snapshot_root, snapshot_manifest):
        identity = _inspect_artifact(
            snapshot_root,
            day,
            protocol,
            binding_path=None,
            archive_snapshot=snapshot_manifest,
        )
        if days and identity.started_ms <= _finite(
            days[-1]["ended_ms"], "prior committed artifact end"
        ):
            raise ValueError("Family34 artifact overlaps the prior committed artifact")
        entry, generation_name, installed = _stage_generation(
            day=day,
            identity=identity,
            state=state,
            protocol=protocol,
            current=current,
        )
    _publish_current(
        state,
        {
            "schema": POINTER_SCHEMA,
            "generation": generation_name,
            "generation_manifest_sha256": _file_sha256(installed / GENERATION_MANIFEST),
            "config_sha256": protocol.config_sha256,
            "forward_config_sha256": protocol.forward_config_sha256,
        },
    )
    return _completion(entry, generation_name)


def process_day(
    day: str,
    artifact_path: str | Path,
    state_dir: str | Path,
    *,
    frozen: Mapping[str, Any],
    binding_path: str | Path | None = None,
    archive_snapshot: str | Path | None = None,
) -> dict[str, Any]:
    """Validate and atomically admit one complete Family 34 UTC-day artifact."""
    normalized_day = _normalize_day(day)
    protocol = _validate_config(frozen)
    if normalized_day < protocol.first_day:
        raise ValueError("Family34 day is outside the frozen post-cutoff sample")
    if normalized_day >= _utc_today():
        raise ValueError("Family34 admission requires a closed UTC day")
    state = Path(state_dir)
    with _locked_state(state):
        return _process_locked(
            normalized_day,
            artifact_path,
            state,
            protocol,
            binding_path,
            archive_snapshot,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("day", nargs="?")
    parser.add_argument("artifact", nargs="?")
    parser.add_argument("state_dir", nargs="?", default=DEFAULT_STATE_DIR)
    parser.add_argument("--config", default=str(FREEZE_PATH))
    parser.add_argument("--binding")
    parser.add_argument("--archive-snapshot")
    parser.add_argument(
        "--shared-root",
        default=os.environ.get("STRICT_SHARED_ROOT", DEFAULT_SHARED_ROOT),
    )
    args = parser.parse_args()
    frozen = _read_json(Path(args.config))
    protocol = _validate_config(frozen)
    if args.day is None:
        args.day, terminal = _pending_day_or_terminal(args.state_dir, protocol)
        if terminal is not None:
            print(json.dumps({"status": "terminal", "forward_status": terminal}, sort_keys=True))
            return
        assert args.day is not None
    normalized_day = _normalize_day(args.day)
    if normalized_day >= _utc_today():
        print(
            json.dumps(
                {
                    "status": "skipped",
                    "day": normalized_day,
                    "reason": "UTC_day_not_closed",
                },
                sort_keys=True,
            )
        )
        return
    if args.artifact is None:
        day_root = Path(args.shared_root) / normalized_day.replace("-", "")
        args.artifact = str(day_root / "artifact")
        if args.archive_snapshot is None:
            args.archive_snapshot = str(day_root / "archive-manifests.json")
    result = process_day(
        normalized_day,
        args.artifact,
        args.state_dir,
        frozen=frozen,
        binding_path=args.binding,
        archive_snapshot=args.archive_snapshot,
    )
    print(json.dumps(result, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
