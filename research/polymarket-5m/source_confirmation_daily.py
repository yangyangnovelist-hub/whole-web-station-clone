"""Admit one immutable eu-west UTC day into the Family 36 paper ledger."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import math
import re
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import boto3
import h_forward
import h_replay_archive as archive
import source_confirmation
import source_confirmation_forward as forward
import source_race_daily as archive_daily
import strict_prebuild

BUCKET = archive_daily.BUCKET
EVIDENCE_PREFIX = "formal/source-confirmation-v1"
DEFAULT_STRICT_ROOT = "/home/ubuntu/rec/formal/strict-days"
DEFAULT_WORK_ROOT = "/home/ubuntu/rec/formal/source-confirmation-days"
DEFAULT_STATE_ROOT = "/home/ubuntu/rec/formal/source-confirmation-state"
FIRST_COMPLETE_DAY = "20261012"
FORMAL_CUTOFF_MS = 1_791_750_600_000.0
OBSERVATIONS = "observations.jsonl"
STATUS = "status.json"
VERDICT = "verdict.json"
ADMISSION = "admission.json"
STATE_FILES = (OBSERVATIONS, STATUS, VERDICT)
PRIVATE_ARTIFACT = "input-artifact"
INPUT_ARTIFACT_FILES = (
    *archive_daily.STRICT_FILES,
    archive_daily.ARTIFACT_BINDING,
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _utc_today() -> str:
    return dt.datetime.now(dt.UTC).date().strftime("%Y%m%d")


def _next_day(day: str) -> str:
    value = dt.datetime.strptime(day, "%Y%m%d").date() + dt.timedelta(days=1)
    return value.strftime("%Y%m%d")


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {label}") from exc
    if not isinstance(payload, dict):
        raise TypeError(f"{label} must be an object")
    return payload


def _day_window(day: str) -> tuple[float, float]:
    start = dt.datetime.strptime(day, "%Y%m%d").replace(tzinfo=dt.UTC)
    start_ms = start.timestamp() * 1_000.0
    return start_ms, start_ms + 86_400_000.0


def _finite(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} is invalid") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} is non-finite")
    return number


def _validate_input_paths(
    artifact: Path,
    snapshot_path: Path,
    day: str,
    cutoff_ms: float,
) -> dict[str, Any]:
    snapshot = _read_json(snapshot_path, "strict archive snapshot")
    if set(snapshot) != {"rec", "poly"}:
        raise ValueError("strict archive snapshot identity drift")
    rec = snapshot["rec"]
    poly = snapshot["poly"]
    if not isinstance(rec, Mapping) or not isinstance(poly, Mapping):
        raise TypeError("strict archive snapshot manifests must be objects")
    archive_daily.validate_archive_manifest(rec, day, "rec")
    archive_daily.validate_archive_manifest(poly, day, "poly")
    raw_identity = strict_prebuild.raw_archive_identity(rec, poly)

    checked = archive.validate_standard_artifact(artifact)
    manifest = checked.get("manifest")
    if not isinstance(manifest, Mapping):
        raise TypeError("standard artifact manifest must be an object")
    if (
        manifest.get("complete") is not True
        or manifest.get("run_id") != f"eu-west-{day}"
        or manifest.get("collector_region") != "eu-west-1"
        or manifest.get("receipt_race_ready") is not True
    ):
        raise ValueError("standard artifact eu-west identity drift")
    started_ms = _finite(manifest.get("started_ms"), "artifact started_ms")
    ended_ms = _finite(manifest.get("ended_ms"), "artifact ended_ms")
    day_start_ms, day_end_ms = _day_window(day)
    if not (
        started_ms >= cutoff_ms
        and day_start_ms <= started_ms < day_end_ms
        and started_ms <= ended_ms < day_end_ms
    ):
        raise ValueError("standard artifact UTC-day coverage drift")

    manifest_path = artifact / "manifest.json"
    binding_path = artifact / archive_daily.ARTIFACT_BINDING
    binding = _read_json(binding_path, "strict input binding")
    manifest_sha256 = archive_daily._sha256(manifest_path)
    if (
        binding.get("schema") != "eu-strict-input-binding-v1"
        or binding.get("day") != day
        or binding.get("raw_archive_manifests_sha256") != raw_identity
        or binding.get("strict_manifest_sha256") != manifest_sha256
    ):
        raise ValueError("strict input binding or archive identity drift")
    return {
        "artifact": artifact,
        "snapshot_path": snapshot_path,
        "manifest_path": manifest_path,
        "binding_path": binding_path,
        "run_id": manifest["run_id"],
        "started_ms": started_ms,
        "ended_ms": ended_ms,
        "raw_archive_manifests_sha256": raw_identity,
        "archive_snapshot_sha256": archive_daily._sha256(snapshot_path),
        "strict_manifest_sha256": manifest_sha256,
        "strict_input_binding_sha256": archive_daily._sha256(binding_path),
    }


def _validate_daily_input(
    strict_root: Path, day: str, cutoff_ms: float
) -> dict[str, Any]:
    day_root = strict_root / day
    return _validate_input_paths(
        day_root / "artifact",
        day_root / "archive-manifests.json",
        day,
        cutoff_ms,
    )


def _input_fingerprint(identity: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: identity[key]
        for key in (
            "run_id",
            "started_ms",
            "ended_ms",
            "raw_archive_manifests_sha256",
            "archive_snapshot_sha256",
            "strict_manifest_sha256",
            "strict_input_binding_sha256",
        )
    }


def _copy_or_compare(source: Path, destination: Path, label: str) -> None:
    if destination.is_file():
        if archive_daily._sha256(destination) != archive_daily._sha256(source):
            raise ValueError(f"persisted {label} input drift")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name("." + destination.name + ".tmp")
    shutil.copyfile(source, temporary)
    temporary.replace(destination)


def _persist_input_snapshot(
    day_root: Path,
    identity: Mapping[str, Any],
    day: str,
    cutoff_ms: float,
) -> dict[str, Any]:
    """Persist the exact replay input before any fallible evaluation work."""
    destination = day_root / PRIVATE_ARTIFACT
    snapshot = day_root / "archive-manifests.json"
    if destination.exists():
        if not destination.is_dir():
            raise ValueError("persisted Family36 input artifact is not a directory")
        return _validate_input_paths(destination, snapshot, day, cutoff_ms)

    source = Path(identity["artifact"])
    sources = {name: source / name for name in INPUT_ARTIFACT_FILES}
    missing = [name for name, path in sources.items() if not path.is_file()]
    if missing:
        raise ValueError(
            "Family36 immutable input snapshot is incomplete: " + ", ".join(missing)
        )
    day_root.mkdir(parents=True, exist_ok=True)
    _copy_or_compare(Path(identity["snapshot_path"]), snapshot, "archive snapshot")
    before = {name: archive_daily._sha256(path) for name, path in sources.items()}
    before["archive-manifests.json"] = archive_daily._sha256(
        Path(identity["snapshot_path"])
    )

    staging = day_root / f".{PRIVATE_ARTIFACT}.tmp"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    try:
        for name, path in sources.items():
            shutil.copyfile(path, staging / name)
        after = {name: archive_daily._sha256(path) for name, path in sources.items()}
        after["archive-manifests.json"] = archive_daily._sha256(
            Path(identity["snapshot_path"])
        )
        copied = {
            name: archive_daily._sha256(staging / name) for name in INPUT_ARTIFACT_FILES
        }
        copied["archive-manifests.json"] = archive_daily._sha256(snapshot)
        if before != after or copied != before:
            raise ValueError("Family36 input changed while creating its snapshot")
        staging.replace(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return _validate_input_paths(destination, snapshot, day, cutoff_ms)


def _load_or_persist_input(
    strict_root: Path,
    day_root: Path,
    day: str,
    cutoff_ms: float,
) -> dict[str, Any]:
    private = day_root / PRIVATE_ARTIFACT
    if private.exists():
        return _validate_input_paths(
            private,
            day_root / "archive-manifests.json",
            day,
            cutoff_ms,
        )
    return _persist_input_snapshot(
        day_root,
        _validate_daily_input(strict_root, day, cutoff_ms),
        day,
        cutoff_ms,
    )


def _write_json_or_compare(
    destination: Path, payload: Mapping[str, Any], label: str
) -> None:
    pending = destination.with_name("." + destination.name + ".pending")
    archive_daily._atomic_json(pending, payload)
    if destination.is_file():
        if archive_daily._sha256(destination) != archive_daily._sha256(pending):
            pending.unlink()
            raise ValueError(f"persisted {label} drift")
        pending.unlink()
        return
    pending.replace(destination)


def _persist_replay(
    day_root: Path,
    identity: Mapping[str, Any],
    cutoff_ms: float,
) -> tuple[Path, Path]:
    archive_snapshot = day_root / "archive-manifests.json"
    strict_manifest = day_root / "strict-manifest.json"
    strict_binding = day_root / "strict-input-binding.json"
    _copy_or_compare(
        Path(identity["snapshot_path"]), archive_snapshot, "archive snapshot"
    )
    _copy_or_compare(
        Path(identity["manifest_path"]), strict_manifest, "strict manifest"
    )
    _copy_or_compare(Path(identity["binding_path"]), strict_binding, "strict binding")

    rows_path = day_root / "replay-rows.jsonl"
    pending_rows = day_root / ".replay-rows.pending.jsonl"
    if pending_rows.exists():
        pending_rows.unlink()
    summary = source_confirmation.replay_archive(
        identity["artifact"],
        rows_out=pending_rows,
        prospective_cutoff_ms=cutoff_ms,
    )
    dataset = summary.get("dataset")
    if not isinstance(dataset, Mapping) or (
        dataset.get("paper_gate_eligible") is not True
        or dataset.get("pending_signal_market_outcomes") not in ([], ())
        or dataset.get("run_id") != identity["run_id"]
        or dataset.get("collector_region") != "eu-west-1"
        or dataset.get("receipt_race_ready") is not True
        or _finite(dataset.get("started_ms"), "replay started_ms")
        != identity["started_ms"]
        or _finite(dataset.get("ended_ms"), "replay ended_ms") != identity["ended_ms"]
    ):
        pending_rows.unlink(missing_ok=True)
        raise ValueError("Family36 replay is not eligible for formal admission")
    if not pending_rows.is_file():
        raise ValueError("Family36 replay did not produce rows")
    replay_rows = h_forward._read_jsonl(pending_rows)
    if int(summary.get("record_count", -1)) != len(replay_rows):
        pending_rows.unlink()
        raise ValueError("Family36 replay row count drift")
    for row in replay_rows:
        if row.get("winner") not in {"Up", "Down"}:
            pending_rows.unlink()
            raise ValueError("Family36 pending outcome or unsettled fill")
        if (
            row.get("filled") or float(row.get("filled_shares") or 0.0) > 0.0
        ) and row.get("result_time_ms") is None:
            pending_rows.unlink()
            raise ValueError("Family36 pending outcome or unsettled fill")
    if rows_path.is_file():
        if archive_daily._sha256(rows_path) != archive_daily._sha256(pending_rows):
            pending_rows.unlink()
            raise ValueError("persisted Family36 replay rows drift")
        pending_rows.unlink()
    else:
        pending_rows.replace(rows_path)
    summary_path = day_root / "replay-summary.json"
    _write_json_or_compare(summary_path, summary, "Family36 replay summary")
    return summary_path, rows_path


def _state_hashes(root: Path) -> dict[str, str | None]:
    return {
        name: archive_daily._sha256(root / name) if (root / name).is_file() else None
        for name in STATE_FILES
    }


def _read_admission(
    state: Path, freeze_sha256: str, *, validate_state: bool = True
) -> dict[str, Any] | None:
    path = state / ADMISSION
    if not path.is_file():
        return None
    payload = _read_json(path, "source-confirmation admission")
    hashes = payload.get("state_sha256")
    if (
        payload.get("schema") != "source-confirmation-admission-v1"
        or payload.get("freeze_sha256") != freeze_sha256
        or not re.fullmatch(r"\d{8}", str(payload.get("day") or ""))
        or str(payload.get("day")) < FIRST_COMPLETE_DAY
        or _SHA256_RE.fullmatch(str(payload.get("complete_sha256") or "")) is None
        or not isinstance(hashes, Mapping)
        or set(hashes) != set(STATE_FILES)
        or any(
            digest is not None and _SHA256_RE.fullmatch(str(digest)) is None
            for digest in hashes.values()
        )
    ):
        raise ValueError("source-confirmation admission identity drift")
    if validate_state and _state_hashes(state) != dict(hashes):
        raise ValueError("source-confirmation admitted state hash mismatch")
    return payload


def _stage_state(state: Path, day_root: Path) -> Path:
    staged = day_root / "state-next"
    if staged.exists():
        shutil.rmtree(staged)
    staged.mkdir(parents=True)
    for name in STATE_FILES:
        source = state / name
        if source.is_file():
            shutil.copyfile(source, staged / name)
    return staged


def _evidence_row(path: Path, day_root: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(day_root).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": archive_daily._sha256(path),
    }


def _validate_complete(
    complete: Mapping[str, Any],
    *,
    day: str,
    freeze_sha256: str,
    identity: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if (
        complete.get("schema") != "source-confirmation-complete-day-v1"
        or complete.get("status") != "complete"
        or complete.get("day") != day
        or complete.get("freeze_sha256") != freeze_sha256
        or complete.get("run_id") != identity["run_id"]
        or complete.get("raw_archive_manifests_sha256")
        != identity["raw_archive_manifests_sha256"]
        or complete.get("archive_snapshot_sha256")
        != identity["archive_snapshot_sha256"]
        or complete.get("strict_manifest_sha256") != identity["strict_manifest_sha256"]
        or complete.get("strict_input_binding_sha256")
        != identity["strict_input_binding_sha256"]
    ):
        raise ValueError("completed Family36 day input or freeze drift")
    base = complete.get("base_state_sha256")
    target = complete.get("target_state_sha256")
    if (
        not isinstance(base, Mapping)
        or not isinstance(target, Mapping)
        or set(base) != set(STATE_FILES)
        or set(target) != set(STATE_FILES)
        or any(
            digest is not None and _SHA256_RE.fullmatch(str(digest)) is None
            for hashes in (base, target)
            for digest in hashes.values()
        )
    ):
        raise ValueError("completed Family36 state hash drift")
    expected_paths = {
        "archive-manifests.json",
        "strict-manifest.json",
        "strict-input-binding.json",
        "replay-summary.json",
        "replay-rows.jsonl",
        *(f"{PRIVATE_ARTIFACT}/{name}" for name in INPUT_ARTIFACT_FILES),
        *(
            f"state-next/{name}"
            for name, digest in target.items()
            if digest is not None
        ),
    }
    evidence = complete.get("evidence")
    if not isinstance(evidence, list):
        raise TypeError("completed Family36 evidence must be an array")
    paths: list[str] = []
    for item in evidence:
        if not isinstance(item, Mapping):
            raise TypeError("completed Family36 evidence item must be an object")
        relative = Path(str(item.get("path") or ""))
        if (
            relative.is_absolute()
            or not relative.parts
            or ".." in relative.parts
            or not isinstance(item.get("bytes"), int)
            or int(item["bytes"]) < 0
            or _SHA256_RE.fullmatch(str(item.get("sha256") or "")) is None
        ):
            raise ValueError("completed Family36 evidence drift")
        paths.append(relative.as_posix())
    if set(paths) != expected_paths or len(paths) != len(expected_paths):
        raise ValueError("completed Family36 evidence set drift")
    return [dict(item) for item in evidence]


def _upload_evidence(
    s3: Any, bucket: str, day: str, day_root: Path, files: Sequence[Path]
) -> list[dict[str, Any]]:
    rows = []
    for path in files:
        relative = path.relative_to(day_root).as_posix()
        rows.append(
            archive_daily._upload_file(
                s3,
                bucket,
                f"{EVIDENCE_PREFIX}/{day}/{relative}",
                path,
            )
        )
    return rows


def _commit_completed_day(
    complete: Mapping[str, Any], complete_path: Path, state: Path, day_root: Path
) -> None:
    base = dict(complete["base_state_sha256"])
    target = dict(complete["target_state_sha256"])
    current = _state_hashes(state)
    for name in STATE_FILES:
        if current[name] not in (base[name], target[name]):
            raise ValueError(
                f"source-confirmation state changed while committing {name}"
            )
    state.mkdir(parents=True, exist_ok=True)
    staged = day_root / "state-next"
    for name in STATE_FILES:
        digest = target[name]
        destination = state / name
        if digest is None:
            if destination.exists():
                destination.unlink()
            continue
        source = staged / name
        if not source.is_file() or archive_daily._sha256(source) != digest:
            raise ValueError(f"staged source-confirmation state changed: {name}")
        temporary = state / f".{name}.tmp"
        shutil.copyfile(source, temporary)
        temporary.replace(destination)
    if _state_hashes(state) != target:
        raise ValueError("source-confirmation state commit verification failed")
    archive_daily._atomic_json(
        state / ADMISSION,
        {
            "schema": "source-confirmation-admission-v1",
            "day": complete["day"],
            "freeze_sha256": complete["freeze_sha256"],
            "complete_sha256": archive_daily._sha256(complete_path),
            "state_sha256": target,
        },
    )


def _verify_local_evidence(
    day_root: Path, evidence_rows: Sequence[Mapping[str, Any]]
) -> list[Path]:
    paths: list[Path] = []
    for item in evidence_rows:
        path = day_root / str(item["path"])
        if (
            not path.is_file()
            or path.stat().st_size != int(item["bytes"])
            or archive_daily._sha256(path) != item["sha256"]
        ):
            raise ValueError("completed Family36 evidence changed")
        paths.append(path)
    return paths


def _terminal_result(state: Path) -> dict[str, Any] | None:
    path = state / VERDICT
    if not path.is_file():
        return None
    verdict = _read_json(path, "source-confirmation verdict")
    if verdict.get("status") not in {"passed", "rejected"}:
        raise ValueError("source-confirmation verdict is not terminal")
    return {"status": "terminal", "verdict": verdict}


def _admitted_terminal(
    state: Path, admission: Mapping[str, Any]
) -> dict[str, Any] | None:
    expected = admission["state_sha256"][VERDICT]
    if expected is None:
        return None
    verdict = state / VERDICT
    if not verdict.is_file() or archive_daily._sha256(verdict) != expected:
        raise ValueError("source-confirmation admitted state hash mismatch")
    return _terminal_result(state)


def process_day(
    day: str,
    strict_root: str | Path,
    work_root: str | Path,
    state_dir: str | Path,
    *,
    s3: Any,
    bucket: str = BUCKET,
    freeze_path: str | Path = forward.FREEZE_PATH,
) -> dict[str, Any]:
    frozen, freeze_sha256 = forward.load_freeze(freeze_path)
    cutoff_ms = _finite(frozen.get("cutoff_ms"), "Family36 cutoff")
    if cutoff_ms != FORMAL_CUTOFF_MS:
        raise ValueError("Family36 formal cutoff drift")
    cutoff_day = dt.datetime.fromtimestamp(cutoff_ms / 1_000.0, tz=dt.UTC).date()
    derived_first_day = (cutoff_day + dt.timedelta(days=1)).strftime("%Y%m%d")
    if derived_first_day != FIRST_COMPLETE_DAY:
        raise ValueError("Family36 first complete UTC day drift")
    if not re.fullmatch(r"\d{8}", day) or day < FIRST_COMPLETE_DAY:
        raise ValueError("day is outside the frozen complete-day sample")
    if day >= _utc_today():
        raise ValueError("Family36 admission requires a closed UTC day")

    strict_root = Path(strict_root)
    work_root = Path(work_root)
    state = Path(state_dir)
    day_root = work_root / day
    complete_path = day_root / "complete.json"
    admission = _read_admission(
        state, freeze_sha256, validate_state=not complete_path.is_file()
    )
    if admission is not None and day != admission["day"]:
        # A completion journal only permits mixed base/target state recovery for
        # the day it belongs to.  An unrelated journal must never suppress the
        # integrity check on the already-admitted ledger.
        _read_admission(state, freeze_sha256, validate_state=True)
        terminal = _admitted_terminal(state, admission)
        if terminal is not None:
            return terminal
    if admission is None:
        if (
            any((state / name).exists() for name in STATE_FILES)
            and not complete_path.is_file()
        ):
            raise ValueError("source-confirmation state exists without admission")
        expected_day = FIRST_COMPLETE_DAY
    else:
        admitted_day = str(admission["day"])
        if day == admitted_day:
            if not complete_path.is_file():
                raise ValueError(
                    "admitted source-confirmation day lacks completion journal"
                )
            expected_day = admitted_day
        else:
            expected_day = _next_day(admitted_day)
    if day != expected_day:
        raise ValueError(
            f"day is not the next source-confirmation admission: expected {expected_day}"
        )

    if complete_path.is_file():
        identity = _load_or_persist_input(strict_root, day_root, day, cutoff_ms)
        complete = _read_json(complete_path, "completed Family36 day")
        evidence_rows = _validate_complete(
            complete,
            day=day,
            freeze_sha256=freeze_sha256,
            identity=identity,
        )
        evidence = _verify_local_evidence(day_root, evidence_rows)
        if admission is not None and day == admission["day"]:
            if admission["complete_sha256"] != archive_daily._sha256(complete_path):
                raise ValueError("admitted Family36 complete hash mismatch")
            _read_admission(state, freeze_sha256, validate_state=True)
            return complete
        _upload_evidence(s3, bucket, day, day_root, [*evidence, complete_path])
        _commit_completed_day(complete, complete_path, state, day_root)
        return complete

    day_root.mkdir(parents=True, exist_ok=True)
    identity = _load_or_persist_input(strict_root, day_root, day, cutoff_ms)
    initial_fingerprint = _input_fingerprint(identity)
    summary_path, rows_path = _persist_replay(day_root, identity, cutoff_ms)
    if (
        _input_fingerprint(
            _validate_input_paths(
                day_root / PRIVATE_ARTIFACT,
                day_root / "archive-manifests.json",
                day,
                cutoff_ms,
            )
        )
        != initial_fingerprint
    ):
        raise ValueError("Family36 daily input drifted during replay")

    base_state_hashes = _state_hashes(state)
    staged = _stage_state(state, day_root)
    result = forward.accumulate(
        h_forward._read_jsonl(rows_path),
        frozen,
        freeze_sha256,
        ledger_path=staged / OBSERVATIONS,
        status_path=staged / STATUS,
        verdict_path=staged / VERDICT,
    )
    if _state_hashes(state) != base_state_hashes:
        raise ValueError("source-confirmation base state changed while staging")
    target_state_hashes = _state_hashes(staged)
    evidence = [
        day_root / "archive-manifests.json",
        day_root / "strict-manifest.json",
        day_root / "strict-input-binding.json",
        summary_path,
        rows_path,
        *(day_root / PRIVATE_ARTIFACT / name for name in INPUT_ARTIFACT_FILES),
        *(
            staged / name
            for name, digest in target_state_hashes.items()
            if digest is not None
        ),
    ]
    complete = {
        "schema": "source-confirmation-complete-day-v1",
        "status": "complete",
        "day": day,
        "freeze_sha256": freeze_sha256,
        **initial_fingerprint,
        "replay_summary_sha256": archive_daily._sha256(summary_path),
        "replay_rows_sha256": archive_daily._sha256(rows_path),
        "forward_status": result,
        "base_state_sha256": base_state_hashes,
        "target_state_sha256": target_state_hashes,
        "evidence": [_evidence_row(path, day_root) for path in evidence],
    }
    archive_daily._atomic_json(complete_path, complete)
    _upload_evidence(s3, bucket, day, day_root, [*evidence, complete_path])
    _commit_completed_day(complete, complete_path, state, day_root)
    return complete


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day")
    parser.add_argument("--strict-root", default=DEFAULT_STRICT_ROOT)
    parser.add_argument("--work", default=DEFAULT_WORK_ROOT)
    parser.add_argument("--state", default=DEFAULT_STATE_ROOT)
    parser.add_argument("--bucket", default=BUCKET)
    parser.add_argument("--freeze", default=str(forward.FREEZE_PATH))
    args = parser.parse_args()

    _frozen, freeze_sha256 = forward.load_freeze(args.freeze)
    state = Path(args.state)
    admission = _read_admission(state, freeze_sha256, validate_state=False)
    if args.day is None:
        args.day = (
            FIRST_COMPLETE_DAY
            if admission is None
            else _next_day(str(admission["day"]))
        )
    if re.fullmatch(r"\d{8}", args.day) and args.day >= _utc_today():
        print(
            json.dumps(
                {
                    "status": "skipped",
                    "day": args.day,
                    "reason": "UTC_day_not_closed",
                },
                sort_keys=True,
            )
        )
        return
    state.mkdir(parents=True, exist_ok=True)
    with (state / ".daily.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = process_day(
            args.day,
            args.strict_root,
            args.work,
            args.state,
            s3=boto3.client("s3", region_name="eu-west-1"),
            bucket=args.bucket,
            freeze_path=args.freeze,
        )
    print(json.dumps(result, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
