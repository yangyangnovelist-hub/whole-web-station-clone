"""Build and retain one closed eu-west source-race stopping day."""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Mapping

import boto3
from botocore.exceptions import ClientError

import eu_strict
import h_replay_archive as archive
import h_source_race as race
import source_race_forward as forward


BUCKET = "sgk90-rec-archive-114469424813-euw1"
STRICT_FILES = (
    "manifest.json",
    "source_events.jsonl.gz",
    "clob_events.jsonl.gz",
    "market_registry.csv.gz",
    "market_outcomes.csv.gz",
)
EVIDENCE_PREFIX = "formal/source-race-v1"
ADMISSION = "admission.json"
ARTIFACT_BINDING = "input-binding.json"
STATE_FILES = (
    forward.BASELINE_LEDGER,
    forward.CANDIDATE_LEDGER,
    forward.STATUS,
    forward.VERDICT,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _raw_archive_identity(archive_snapshot: Path) -> str:
    payload = json.loads(archive_snapshot.read_text(encoding="utf-8"))
    rec = payload.get("rec") or payload.get("current_rec")
    poly = payload.get("poly") or payload.get("current_poly")
    if not isinstance(rec, Mapping) or not isinstance(poly, Mapping):
        raise ValueError("archive snapshot lacks current rec/poly manifests")
    encoded = json.dumps(
        {"rec": rec, "poly": poly}, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def validate_archive_manifest(manifest: Mapping[str, Any], day: str, group: str) -> None:
    if manifest.get("day") != day or manifest.get("group") != group:
        raise ValueError(f"{group} archive manifest identity mismatch")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError(f"{group} archive manifest has no files")
    for item in files:
        if not (
            isinstance(item, Mapping)
            and item.get("uploaded_ok") is True
            and item.get("gzip_ok") is True
            and int(item.get("size") or 0) > 0
            and re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256") or ""))
        ):
            raise ValueError(f"{group} unverified archive file")
    names = [str(item["file"]) for item in files]
    if len(names) != len(set(names)):
        raise ValueError(f"{group} archive manifest contains duplicate files")
    expected_hours = {f"{hour:02d}" for hour in range(24)}
    if group == "poly":
        hours = {
            match.group(1) for name in names
            if (match := re.fullmatch(rf"poly_clob\.{day}T(\d{{2}})\.jsonl\.gz", name))
        }
        if hours != expected_hours:
            raise ValueError("poly archive hour coverage is incomplete")
        return
    for routes in eu_strict.SOURCE_ROUTES.values():
        for route in routes:
            hours = {
                match.group(1) for name in names
                if (match := re.fullmatch(rf"{re.escape(route)}\.{day}T(\d{{2}})\.txt\.gz", name))
            }
            if hours != expected_hours:
                raise ValueError(f"rec archive hour coverage is incomplete for {route}")


def _archive_manifest(s3: Any, bucket: str, day: str, group: str) -> dict[str, Any]:
    response = s3.get_object(Bucket=bucket, Key=f"{group}/{day}/MANIFEST.json")
    manifest = json.loads(response["Body"].read())
    validate_archive_manifest(manifest, day, group)
    return manifest


def validate_local_archive(manifest: Mapping[str, Any], root: Path, day: str, group: str) -> None:
    required_routes = {
        route for routes in eu_strict.SOURCE_ROUTES.values() for route in routes
    }
    for item in manifest["files"]:
        name = str(item["file"])
        if group == "poly":
            required = re.fullmatch(rf"poly_clob\.{day}T\d{{2}}\.jsonl\.gz", name) is not None
        else:
            required = any(
                re.fullmatch(rf"{re.escape(route)}\.{day}T\d{{2}}\.txt\.gz", name)
                for route in required_routes
            )
        if not required:
            continue
        path = root / name
        if (
            not path.is_file()
            or path.stat().st_size != int(item["size"])
            or _sha256(path) != item["sha256"]
        ):
            raise ValueError(f"local {group} archive differs from verified S3 manifest: {name}")


def _valid_artifact(path: Path, day: str, archive_snapshot: Path) -> bool:
    try:
        result = archive.validate_standard_artifact(path)
    except (FileNotFoundError, OSError, ValueError):
        return False
    binding_path = path / ARTIFACT_BINDING
    if not binding_path.is_file():
        return False
    try:
        binding = json.loads(binding_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    manifest_path = path / "manifest.json"
    if binding.get("schema") == "eu-strict-input-binding-v1":
        archive_matches = (
            binding.get("raw_archive_manifests_sha256") == _raw_archive_identity(archive_snapshot)
        )
    else:
        archive_matches = binding.get("archive_manifests_sha256") == _sha256(archive_snapshot)
    return bool(
        result["manifest"].get("run_id") == f"eu-west-{day}"
        and binding.get("day") == day
        and archive_matches
        and binding.get("strict_manifest_sha256") == _sha256(manifest_path)
    )


def _build_artifact(
    data_dir: Path,
    poly_dir: Path,
    day_dir: Path,
    day: str,
    archive_snapshot: Path,
) -> Path:
    shared_root = os.environ.get("STRICT_SHARED_ROOT")
    if shared_root:
        shared = Path(shared_root) / day / "artifact"
        if _valid_artifact(shared, day, archive_snapshot):
            return shared
        raise ValueError(f"shared strict artifact is absent or invalid for {day}")
    artifact = day_dir / "artifact"
    if _valid_artifact(artifact, day, archive_snapshot):
        return artifact
    if artifact.exists():
        shutil.rmtree(artifact)
    staging = day_dir / "artifact.tmp"
    if staging.exists():
        shutil.rmtree(staging)
    eu_strict.build(data_dir, poly_dir, staging, day)
    built = staging / "strict"
    validated = archive.validate_standard_artifact(built)
    if validated["manifest"].get("run_id") != f"eu-west-{day}":
        raise ValueError("strict artifact day identity drift")
    _atomic_json(built / ARTIFACT_BINDING, {
        "schema": "current-h-source-race-input-v1",
        "day": day,
        "archive_manifests_sha256": _sha256(archive_snapshot),
        "raw_archive_manifests_sha256": _raw_archive_identity(archive_snapshot),
        "strict_manifest_sha256": _sha256(built / "manifest.json"),
    })
    built.replace(artifact)
    staging.rmdir()
    return artifact


def _discard_local_artifact(artifact: Path, day_dir: Path) -> None:
    if artifact == day_dir / "artifact" and artifact.exists():
        shutil.rmtree(artifact)


def _load_or_compare(
    artifact: Path,
    day_dir: Path,
    current_h_freeze: Path,
    source_race_freeze: Path,
) -> dict[str, Any]:
    comparison_path = day_dir / "comparison.json"
    rows_dir = day_dir / "rows"
    if comparison_path.is_file():
        result = json.loads(comparison_path.read_text(encoding="utf-8"))
        artifact_identity, _stats, _root = race._capture_artifact_identity(artifact)
        if result.get("artifact_identity") != artifact_identity:
            raise ValueError("persisted comparison artifact identity drift")
        if result.get("freeze_sha256") != _sha256(current_h_freeze):
            raise ValueError("persisted comparison current-H freeze drift")
        if result.get("source_race_freeze_sha256") != _sha256(source_race_freeze):
            raise ValueError("persisted comparison source-race freeze drift")
        recorded = result.get("observation_files") or {}
        for arm in ("baseline", "candidate"):
            path = rows_dir / f"{arm}.jsonl"
            if not path.is_file() or _sha256(path) != recorded.get(arm, {}).get("sha256"):
                raise ValueError(f"persisted {arm} rows do not match comparison")
        if result.get("formal_sample_eligible") is not True:
            raise ValueError("persisted comparison is not formal-sample eligible")
        return result
    result = race.compare_archive(
        artifact,
        current_h_freeze,
        rows_dir,
        source_race_freeze,
        True,
    )
    if result.get("formal_sample_eligible") is not True:
        raise ValueError("source-race comparison is not formal-sample eligible")
    _atomic_json(comparison_path, result)
    return result


def _state_hashes(state_dir: Path) -> dict[str, str | None]:
    return {
        name: _sha256(state_dir / name) if (state_dir / name).is_file() else None
        for name in STATE_FILES
    }


def _stage_state(state_dir: Path, day_dir: Path) -> Path:
    staged = day_dir / "state-next"
    if staged.exists():
        shutil.rmtree(staged)
    staged.mkdir()
    for name in STATE_FILES:
        source = state_dir / name
        if source.is_file():
            shutil.copyfile(source, staged / name)
    return staged


def _read_admission(
    state_dir: Path, freeze_sha256: str, *, validate_state: bool = True,
) -> dict[str, Any] | None:
    path = state_dir / ADMISSION
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        payload.get("schema") != "current-h-source-race-admission-v1"
        or payload.get("source_race_freeze_sha256") != freeze_sha256
    ):
        raise ValueError("source-race admission drift")
    hashes = payload.get("state_sha256", {})
    if set(hashes) != set(STATE_FILES):
        raise ValueError("source-race admission state set drift")
    if not validate_state:
        return payload
    for name, digest in hashes.items():
        target = state_dir / name
        if digest is None and target.exists():
            raise ValueError("source-race admitted state hash mismatch")
        if digest is not None and (not target.is_file() or _sha256(target) != digest):
            raise ValueError("source-race admitted state hash mismatch")
    return payload


def _next_day(day: str) -> str:
    value = dt.datetime.strptime(day, "%Y%m%d").date() + dt.timedelta(days=1)
    return value.strftime("%Y%m%d")


def _utc_today() -> str:
    return dt.datetime.now(dt.timezone.utc).date().strftime("%Y%m%d")


def _commit_completed_day(
    complete: Mapping[str, Any], complete_path: Path, state_dir: Path, day_dir: Path,
) -> None:
    base = complete["base_state_sha256"]
    target = complete["target_state_sha256"]
    for name in STATE_FILES:
        destination = state_dir / name
        current = _sha256(destination) if destination.is_file() else None
        if current not in (base.get(name), target.get(name)):
            raise ValueError(f"state changed while committing {name}")
    state_dir.mkdir(parents=True, exist_ok=True)
    staged = day_dir / "state-next"
    for name, digest in target.items():
        source = staged / name
        if digest is None:
            continue
        if not source.is_file() or _sha256(source) != digest:
            raise ValueError(f"staged state changed: {name}")
        temporary = state_dir / f".{name}.tmp"
        shutil.copyfile(source, temporary)
        temporary.replace(state_dir / name)
    _atomic_json(state_dir / ADMISSION, {
        "schema": "current-h-source-race-admission-v1",
        "day": complete["day"],
        "source_race_freeze_sha256": complete["source_race_freeze_sha256"],
        "complete_sha256": _sha256(complete_path),
        "state_sha256": target,
    })


def _validate_complete(
    complete: Mapping[str, Any], day: str, freeze_sha256: str,
) -> list[dict[str, Any]]:
    if (
        complete.get("schema") != "current-h-source-race-day-v1"
        or complete.get("status") != "complete"
        or complete.get("day") != day
        or complete.get("source_race_freeze_sha256") != freeze_sha256
    ):
        raise ValueError("completed daily identity or freeze fingerprint drift")
    if set(complete.get("base_state_sha256", {})) != set(STATE_FILES) or set(
        complete.get("target_state_sha256", {})
    ) != set(STATE_FILES):
        raise ValueError("completed daily state set drift")
    evidence = complete.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("completed daily evidence is missing")
    paths = []
    for item in evidence:
        relative = Path(str(item.get("path") or "")) if isinstance(item, Mapping) else Path()
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise ValueError("completed daily evidence path is invalid")
        paths.append(relative.as_posix())
    if len(paths) != len(set(paths)):
        raise ValueError("completed daily evidence path is duplicated")
    return list(evidence)


def _client_error_matches(error: ClientError, *, codes: set[str], status: int) -> bool:
    response = error.response if isinstance(error.response, Mapping) else {}
    details = response.get("Error") if isinstance(response.get("Error"), Mapping) else {}
    metadata = (
        response.get("ResponseMetadata")
        if isinstance(response.get("ResponseMetadata"), Mapping)
        else {}
    )
    return str(details.get("Code") or "") in codes or metadata.get(
        "HTTPStatusCode"
    ) == status


def _verified_remote_evidence(
    head: Mapping[str, Any], key: str, size: int, digest: str
) -> None:
    if (
        head.get("ContentLength") != size
        or not isinstance(head.get("Metadata"), Mapping)
        or head["Metadata"].get("sha256") != digest
    ):
        raise ValueError(f"immutable S3 evidence collision for {key}")


def _upload_file(s3: Any, bucket: str, key: str, path: Path) -> dict[str, Any]:
    digest = _sha256(path)
    size = path.stat().st_size
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
    except ClientError as error:
        if not _client_error_matches(
            error, codes={"404", "NoSuchKey"}, status=404
        ):
            raise
        try:
            with path.open("rb") as stream:
                s3.put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=stream,
                    ContentLength=size,
                    Metadata={"sha256": digest},
                    IfNoneMatch="*",
                )
        except ClientError as put_error:
            if not _client_error_matches(
                put_error, codes={"412", "PreconditionFailed"}, status=412
            ):
                raise
        head = s3.head_object(Bucket=bucket, Key=key)
    _verified_remote_evidence(head, key, size, digest)
    return {"file": path.name, "bytes": size, "sha256": digest, "s3_key": key}


def _upload_evidence(s3: Any, bucket: str, day: str, day_dir: Path, files: list[Path]) -> list[dict[str, Any]]:
    prefix = f"{EVIDENCE_PREFIX}/{day}"
    return [
        _upload_file(s3, bucket, f"{prefix}/{path.relative_to(day_dir).as_posix()}", path)
        for path in files
    ]


def process_day(
    day: str,
    data_dir: str | Path,
    poly_dir: str | Path,
    work_dir: str | Path,
    state_dir: str | Path,
    *,
    s3: Any,
    bucket: str = BUCKET,
    current_h_freeze: str | Path = race.replay.FREEZE_PATH,
    source_race_freeze: str | Path = race.SOURCE_RACE_FREEZE_PATH,
) -> dict[str, Any]:
    frozen, freeze_sha256 = race._load_source_race_freeze(source_race_freeze)
    cutoff_day = str(frozen["cutoff"])[:10].replace("-", "")
    if not re.fullmatch(r"\d{8}", day) or day < cutoff_day:
        raise ValueError("day is outside the frozen post-cutoff sample")
    if day >= _utc_today():
        raise ValueError("day is not a closed UTC day")
    state_dir = Path(state_dir)
    day_dir = Path(work_dir) / day
    complete_path = day_dir / "complete.json"
    admission = _read_admission(
        state_dir, freeze_sha256, validate_state=not complete_path.is_file(),
    )
    terminal = (
        forward.read_terminal(state_dir, freeze_sha256)
        if admission is not None and not complete_path.is_file()
        else None
    )
    if terminal is not None:
        return {"status": "terminal", "verdict": terminal}
    if admission is None:
        if any((state_dir / name).exists() for name in STATE_FILES) and not complete_path.is_file():
            raise ValueError("source-race state exists without daily admission")
        expected_day = cutoff_day
    else:
        admitted_day = str(admission["day"])
        if day == admitted_day:
            expected_day = admitted_day
        else:
            expected_day = _next_day(admitted_day)
    if day != expected_day:
        raise ValueError(f"day is not the next chronological admission: expected {expected_day}")
    day_dir.mkdir(parents=True, exist_ok=True)
    if complete_path.is_file():
        complete = json.loads(complete_path.read_text(encoding="utf-8"))
        evidence_rows = _validate_complete(complete, day, freeze_sha256)
        if (
            admission is not None
            and day == admission.get("day")
            and admission.get("complete_sha256") != _sha256(complete_path)
        ):
            raise ValueError("admitted daily complete hash mismatch")
        evidence = [day_dir / item["path"] for item in evidence_rows]
        for item, path in zip(evidence_rows, evidence):
            if not path.is_file() or _sha256(path) != item["sha256"]:
                raise ValueError("completed daily evidence changed")
        _upload_evidence(s3, bucket, day, day_dir, [*evidence, complete_path])
        _commit_completed_day(complete, complete_path, state_dir, day_dir)
        artifact = day_dir / "artifact"
        if artifact.exists():
            shutil.rmtree(artifact)
        return complete
    rec_manifest = _archive_manifest(s3, bucket, day, "rec")
    poly_manifest = _archive_manifest(s3, bucket, day, "poly")
    validate_local_archive(rec_manifest, Path(data_dir), day, "rec")
    validate_local_archive(poly_manifest, Path(poly_dir), day, "poly")
    archive_snapshot = day_dir / "archive-manifests.json"
    _atomic_json(archive_snapshot, {"rec": rec_manifest, "poly": poly_manifest})
    artifact = _build_artifact(
        Path(data_dir), Path(poly_dir), day_dir, day, archive_snapshot,
    )
    _load_or_compare(
        artifact, day_dir, Path(current_h_freeze), Path(source_race_freeze),
    )
    base_state_hashes = _state_hashes(state_dir)
    staged_state = _stage_state(state_dir, day_dir)
    result = forward.update(
        day_dir / "rows" / "baseline.jsonl",
        day_dir / "rows" / "candidate.jsonl",
        staged_state,
        frozen=frozen,
        freeze_sha256=freeze_sha256,
    )
    shutil.copyfile(artifact / "manifest.json", day_dir / "strict-manifest.json")
    shutil.copyfile(artifact / ARTIFACT_BINDING, day_dir / "strict-input-binding.json")
    state_snapshots = [staged_state / name for name in STATE_FILES if (staged_state / name).is_file()]
    target_state_hashes = _state_hashes(staged_state)
    evidence = [
        archive_snapshot,
        day_dir / "strict-manifest.json",
        day_dir / "strict-input-binding.json",
        day_dir / "comparison.json",
        day_dir / "rows" / "baseline.jsonl",
        day_dir / "rows" / "candidate.jsonl",
        *state_snapshots,
    ]
    evidence_rows = [
        {"path": path.relative_to(day_dir).as_posix(), "bytes": path.stat().st_size, "sha256": _sha256(path)}
        for path in evidence
    ]
    complete = {
        "schema": "current-h-source-race-day-v1",
        "status": "complete",
        "day": day,
        "source_race_freeze_sha256": freeze_sha256,
        "comparison_sha256": _sha256(day_dir / "comparison.json"),
        "forward_status": result,
        "base_state_sha256": base_state_hashes,
        "target_state_sha256": target_state_hashes,
        "evidence": evidence_rows,
    }
    _atomic_json(complete_path, complete)
    _upload_evidence(s3, bucket, day, day_dir, [*evidence, complete_path])
    _commit_completed_day(complete, complete_path, state_dir, day_dir)
    _discard_local_artifact(artifact, day_dir)
    return complete


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day")
    parser.add_argument("--data", default="/home/ubuntu/rec/data")
    parser.add_argument("--poly", default="/home/ubuntu/rec/data_poly")
    parser.add_argument("--work", default="/home/ubuntu/rec/formal/source-race-days")
    parser.add_argument("--state", default="/home/ubuntu/rec/formal/source-race-state")
    parser.add_argument("--bucket", default=BUCKET)
    args = parser.parse_args()
    frozen, _freeze_sha256 = race._load_source_race_freeze()
    cutoff_day = str(frozen["cutoff"])[:10].replace("-", "")
    state = Path(args.state)
    admission = _read_admission(state, _freeze_sha256, validate_state=False)
    if args.day is None:
        args.day = cutoff_day if admission is None else _next_day(str(admission["day"]))
        today = _utc_today()
        if args.day >= today:
            print(json.dumps({
                "status": "skipped",
                "day": args.day,
                "reason": "UTC_day_not_closed",
            }, sort_keys=True))
            return
    if re.fullmatch(r"\d{8}", args.day) and args.day < cutoff_day:
        print(json.dumps({
            "status": "skipped",
            "day": args.day,
            "reason": "before_frozen_cutoff",
        }, sort_keys=True))
        return
    state.mkdir(parents=True, exist_ok=True)
    with (state / ".daily.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = process_day(
            args.day, args.data, args.poly, args.work, args.state,
            s3=boto3.client("s3", region_name="eu-west-1"), bucket=args.bucket,
        )
    print(json.dumps(result, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
