"""Build and admit one closed eu-west settlement-proxy stopping day."""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import re
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import boto3

import settlement_proxy as proxy
import settlement_proxy_calibrate as calibrate
import settlement_proxy_forward as forward
import source_race_daily as archive_daily


BUCKET = archive_daily.BUCKET
EVIDENCE_PREFIX = "formal/settlement-proxy-v3"
CALIBRATION_PROVENANCE = (
    Path(__file__).with_name("forward") / "settlement-proxy-calibration-input.json"
)
EVALUATOR_FREEZE = (
    Path(__file__).with_name("forward") / "settlement-proxy-evaluator-freeze.json"
)
SETTLEMENT_SOURCES = calibrate.CACHE_SOURCES
ADMISSION = "admission.json"
STATE_ROOT_FILES = (forward.INDEX, forward.STATUS, forward.VERDICT)


def _atomic_json(path: str | Path, payload: Mapping[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)


def _next_day(day: str, amount: int = 1) -> str:
    value = dt.date.fromisoformat(f"{day[:4]}-{day[4:6]}-{day[6:]}") + dt.timedelta(days=amount)
    return value.strftime("%Y%m%d")


def _utc_today() -> str:
    return dt.datetime.now(dt.timezone.utc).date().strftime("%Y%m%d")


def _pin_identity(
    calibration_path: str | Path,
    provenance_path: str | Path,
    fingerprint: str,
) -> tuple[str, str]:
    calibration_path = Path(calibration_path)
    provenance_path = Path(provenance_path)
    calibration_sha256 = archive_daily._sha256(calibration_path)
    provenance_sha256 = archive_daily._sha256(provenance_path)
    calibration = json.loads(calibration_path.read_text(encoding="utf-8"))
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    if (
        calibration.get("strategy_fingerprint") != fingerprint
        or provenance.get("strategy_fingerprint") != fingerprint
        or provenance.get("calibration_sha256") != calibration_sha256
        or not re.fullmatch(r"[0-9a-f]{64}", str(provenance.get("input_manifest_sha256") or ""))
    ):
        raise ValueError("settlement-proxy calibration provenance drift")
    return calibration_sha256, provenance_sha256


def load_evaluator_freeze(
    path: str | Path = EVALUATOR_FREEZE,
) -> tuple[dict[str, Any], str]:
    source = Path(path)
    digest = archive_daily._sha256(source)
    checksum_path = source.with_suffix(".sha256")
    expected = checksum_path.read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", expected) or digest != expected:
        raise ValueError("settlement-proxy evaluator freeze fingerprint drift")
    payload = json.loads(source.read_text(encoding="utf-8"))
    if payload.get("schema") != "settlement-proxy-evaluator-freeze-v1":
        raise ValueError("unknown settlement-proxy evaluator freeze schema")
    root = Path(__file__).resolve().parent
    for relative, expected_sha256 in payload.get("dependencies_sha256", {}).items():
        dependency = root / str(relative)
        if (
            not dependency.is_file()
            or archive_daily._sha256(dependency) != expected_sha256
        ):
            raise ValueError(f"settlement-proxy evaluator dependency drift: {relative}")
    return payload, digest


def _source_items(
    manifest: Mapping[str, Any], source: str, day: str, hours: set[int],
) -> list[Mapping[str, Any]]:
    expected = {f"{source}.{day}T{hour:02d}.txt.gz" for hour in hours}
    rows = [item for item in manifest["files"] if str(item.get("file")) in expected]
    if {str(item["file"]) for item in rows} != expected:
        raise ValueError(f"{source} archive hour coverage is incomplete for {day}")
    return rows


def _validated_venue_items(
    data_root: Path,
    previous_manifest: Mapping[str, Any],
    current_manifest: Mapping[str, Any],
    following_manifest: Mapping[str, Any],
    day: str,
) -> list[Mapping[str, Any]]:
    previous_day, following_day = _next_day(day, -1), _next_day(day)
    selected: list[Mapping[str, Any]] = []
    for source in SETTLEMENT_SOURCES:
        rows = [
            *_source_items(previous_manifest, source, previous_day, {23}),
            *_source_items(current_manifest, source, day, set(range(24))),
            *_source_items(following_manifest, source, following_day, {0}),
        ]
        calibrate.validate_source_files(data_root, rows)
        selected.extend(rows)
    return selected


def _venue_view(data_root: Path, destination: Path, items: list[Mapping[str, Any]]) -> Path:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    for item in items:
        name = str(item["file"])
        (destination / name).symlink_to((data_root / name).resolve())
    return destination


def _stage_state(state: Path, destination: Path) -> Path:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    if state.is_dir():
        for source in state.rglob("*"):
            if source.name in (".daily.lock", ADMISSION):
                continue
            relative = source.relative_to(state)
            target = destination / relative
            if source.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
    return destination


def _state_paths(root: Path) -> list[Path]:
    paths = [Path(name) for name in STATE_ROOT_FILES if (root / name).is_file()]
    days = root / forward.DAYS_DIR
    if days.is_dir():
        paths.extend(path.relative_to(root) for path in sorted(days.glob("*.json")))
    return sorted(paths, key=lambda path: path.as_posix())


def _valid_state_path(value: str) -> bool:
    path = Path(value)
    return bool(
        not path.is_absolute()
        and ".." not in path.parts
        and (
            path.as_posix() in STATE_ROOT_FILES
            or (
                len(path.parts) == 2
                and path.parts[0] == forward.DAYS_DIR
                and re.fullmatch(r"\d{8}\.json", path.parts[1])
            )
        )
    )


def _state_hashes(root: Path) -> dict[str, str]:
    return {
        relative.as_posix(): archive_daily._sha256(root / relative)
        for relative in _state_paths(root)
    }


def _aligned_state_hashes(base_root: Path, target_root: Path) -> tuple[dict[str, str | None], dict[str, str | None]]:
    base_present = _state_hashes(base_root)
    target_present = _state_hashes(target_root)
    if not set(base_present) <= set(target_present):
        raise ValueError("settlement-proxy forward update removed state files")
    names = sorted(set(base_present) | set(target_present))
    return (
        {name: base_present.get(name) for name in names},
        {name: target_present.get(name) for name in names},
    )


def _read_admission(
    state: Path,
    *,
    fingerprint: str,
    calibration_sha256: str,
    provenance_sha256: str,
    evaluator_sha256: str,
    validate_state: bool = True,
) -> dict[str, Any] | None:
    path = state / ADMISSION
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        payload.get("schema") != "settlement-proxy-admission-v1"
        or payload.get("strategy_fingerprint") != fingerprint
        or payload.get("calibration_sha256") != calibration_sha256
        or payload.get("provenance_sha256") != provenance_sha256
        or payload.get("evaluator_sha256") != evaluator_sha256
        or not re.fullmatch(r"\d{8}", str(payload.get("day") or ""))
        or not re.fullmatch(r"[0-9a-f]{64}", str(payload.get("complete_sha256") or ""))
    ):
        raise ValueError("settlement-proxy admission identity drift")
    expected = payload.get("state_sha256")
    if (
        not isinstance(expected, Mapping)
        or not expected
        or any(not _valid_state_path(str(name)) for name in expected)
        or any(not re.fullmatch(r"[0-9a-f]{64}", str(digest or "")) for digest in expected.values())
    ):
        raise ValueError("settlement-proxy admission state drift")
    if validate_state and _state_hashes(state) != dict(expected):
        raise ValueError("settlement-proxy admitted state hash mismatch")
    return payload


def _validate_complete(
    complete: Mapping[str, Any],
    *,
    day: str,
    fingerprint: str,
    calibration_sha256: str,
    provenance_sha256: str,
    evaluator_sha256: str,
) -> list[dict[str, Any]]:
    if (
        complete.get("schema") != "settlement-proxy-complete-day-v2"
        or complete.get("status") != "complete"
        or complete.get("day") != day
        or complete.get("strategy_fingerprint") != fingerprint
        or complete.get("calibration_sha256") != calibration_sha256
        or complete.get("provenance_sha256") != provenance_sha256
        or complete.get("evaluator_sha256") != evaluator_sha256
    ):
        raise ValueError("completed settlement-proxy day identity drift")
    base = complete.get("base_state_sha256")
    target = complete.get("target_state_sha256")
    if (
        not isinstance(base, Mapping)
        or not isinstance(target, Mapping)
        or set(base) != set(target)
        or not target
        or any(not _valid_state_path(str(name)) for name in target)
    ):
        raise ValueError("completed settlement-proxy state set drift")
    for hashes in (base, target):
        if any(
            digest is not None and not re.fullmatch(r"[0-9a-f]{64}", str(digest))
            for digest in hashes.values()
        ):
            raise ValueError("completed settlement-proxy state hash drift")
    evidence = complete.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("completed settlement-proxy evidence is missing")
    paths: list[str] = []
    for item in evidence:
        relative = Path(str(item.get("path") or "")) if isinstance(item, Mapping) else Path()
        if (
            relative.is_absolute()
            or not relative.parts
            or ".." in relative.parts
            or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256") or ""))
        ):
            raise ValueError("completed settlement-proxy evidence drift")
        paths.append(relative.as_posix())
    if len(paths) != len(set(paths)):
        raise ValueError("completed settlement-proxy evidence path is duplicated")
    return list(evidence)


def _commit_completed_day(
    complete: Mapping[str, Any], complete_path: Path, state: Path, day_root: Path,
) -> None:
    base = dict(complete["base_state_sha256"])
    target = dict(complete["target_state_sha256"])
    current = _state_hashes(state)
    if not set(current) <= set(target):
        raise ValueError("unexpected settlement-proxy state while committing")
    for name in target:
        if current.get(name) not in (base.get(name), target.get(name)):
            raise ValueError(f"settlement-proxy state changed while committing {name}")
    staged = day_root / "state-next"
    state.mkdir(parents=True, exist_ok=True)
    for name, digest in target.items():
        source = staged / name
        if digest is None:
            destination = state / name
            if destination.exists():
                destination.unlink()
            continue
        if not source.is_file() or archive_daily._sha256(source) != digest:
            raise ValueError(f"staged settlement-proxy state changed: {name}")
        destination = state / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name("." + destination.name + ".tmp")
        shutil.copyfile(source, temporary)
        temporary.replace(destination)
    expected = {name: digest for name, digest in target.items() if digest is not None}
    if _state_hashes(state) != expected:
        raise ValueError("settlement-proxy state commit verification failed")
    _atomic_json(state / ADMISSION, {
        "schema": "settlement-proxy-admission-v1",
        "day": complete["day"],
        "strategy_fingerprint": complete["strategy_fingerprint"],
        "calibration_sha256": complete["calibration_sha256"],
        "provenance_sha256": complete["provenance_sha256"],
        "evaluator_sha256": complete["evaluator_sha256"],
        "complete_sha256": archive_daily._sha256(complete_path),
        "state_sha256": expected,
    })


def _upload_evidence(
    s3: Any, bucket: str, day: str, root: Path, files: list[Path],
) -> list[dict[str, Any]]:
    rows = []
    for path in files:
        relative = path.relative_to(root).as_posix()
        rows.append(archive_daily._upload_file(
            s3, bucket, f"{EVIDENCE_PREFIX}/{day}/{relative}", path,
        ))
    return rows


def _existing_result(
    path: Path,
    *,
    day: str,
    fingerprint: str,
    calibration_sha256: str,
    provenance_sha256: str,
    evaluator_sha256: str,
    archive_sha256: str,
    strict_manifest_sha256: str,
) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    forward.validate_day_result(
        payload,
        fingerprint=fingerprint,
        calibration_sha256=calibration_sha256,
        provenance_sha256=provenance_sha256,
        evaluator_sha256=evaluator_sha256,
    )
    if (
        payload.get("day") != day
        or payload.get("archive_manifests_sha256") != archive_sha256
        or payload.get("strict_manifest_sha256") != strict_manifest_sha256
    ):
        raise ValueError("persisted settlement-proxy day input drift")
    return payload


def process_day(
    day: str,
    data_dir: str | Path,
    poly_dir: str | Path,
    work_dir: str | Path,
    state_dir: str | Path,
    *,
    s3: Any,
    bucket: str = BUCKET,
    calibration_path: str | Path = proxy.CALIBRATION_PIN,
    provenance_path: str | Path = CALIBRATION_PROVENANCE,
    evaluator_freeze_path: str | Path = EVALUATOR_FREEZE,
) -> dict[str, Any]:
    freeze = proxy.load_freeze()
    fingerprint = proxy.strategy_fingerprint(freeze)
    holdout_day = str(freeze["holdout_start"])[:10].replace("-", "")
    if not re.fullmatch(r"\d{8}", day) or day < holdout_day:
        raise ValueError("day is outside the frozen post-cutoff sample")
    following_day = _next_day(day)
    if following_day >= _utc_today():
        raise ValueError("following UTC boundary day is not closed")
    calibration_sha256, provenance_sha256 = _pin_identity(
        calibration_path, provenance_path, fingerprint,
    )
    evaluator, evaluator_sha256 = load_evaluator_freeze(evaluator_freeze_path)
    if (
        evaluator.get("strategy_fingerprint") != fingerprint
        or evaluator.get("holdout_start") != freeze["holdout_start"]
        or evaluator.get("calibration_sha256") != calibration_sha256
        or evaluator.get("provenance_sha256") != provenance_sha256
    ):
        raise ValueError("settlement-proxy evaluator identity drift")
    data_root, poly_root = Path(data_dir), Path(poly_dir)
    day_root = Path(work_dir) / day
    day_root.mkdir(parents=True, exist_ok=True)
    state = Path(state_dir)
    complete_path = day_root / "complete.json"
    admission = _read_admission(
        state,
        fingerprint=fingerprint,
        calibration_sha256=calibration_sha256,
        provenance_sha256=provenance_sha256,
        evaluator_sha256=evaluator_sha256,
        validate_state=not complete_path.is_file(),
    )
    if admission is None:
        if _state_paths(state) and not complete_path.is_file():
            raise ValueError("settlement-proxy state exists without daily admission")
        expected_day = holdout_day
    else:
        admitted_day = str(admission["day"])
        if (state / forward.VERDICT).is_file() and day != admitted_day:
            return {
                "status": "terminal",
                "verdict": json.loads((state / forward.VERDICT).read_text(encoding="utf-8")),
            }
        expected_day = admitted_day if day == admitted_day else _next_day(admitted_day)
    if day != expected_day:
        raise ValueError(f"day is not the next chronological admission: expected {expected_day}")
    if complete_path.is_file():
        complete = json.loads(complete_path.read_text(encoding="utf-8"))
        evidence_rows = _validate_complete(
            complete,
            day=day,
            fingerprint=fingerprint,
            calibration_sha256=calibration_sha256,
            provenance_sha256=provenance_sha256,
            evaluator_sha256=evaluator_sha256,
        )
        if admission is not None and day == admission["day"]:
            if admission["complete_sha256"] != archive_daily._sha256(complete_path):
                raise ValueError("admitted settlement-proxy complete hash mismatch")
            _read_admission(
                state,
                fingerprint=fingerprint,
                calibration_sha256=calibration_sha256,
                provenance_sha256=provenance_sha256,
                evaluator_sha256=evaluator_sha256,
                validate_state=True,
            )
            staged = day_root / "state-next"
            if staged.exists():
                shutil.rmtree(staged)
            return complete
        evidence = [day_root / str(item["path"]) for item in evidence_rows]
        for item, path in zip(evidence_rows, evidence):
            if not path.is_file() or archive_daily._sha256(path) != item["sha256"]:
                raise ValueError("completed settlement-proxy evidence changed")
        _upload_evidence(s3, bucket, day, day_root, [*evidence, complete_path])
        _commit_completed_day(complete, complete_path, state, day_root)
        staged = day_root / "state-next"
        if staged.exists():
            shutil.rmtree(staged)
        artifact = day_root / "artifact"
        archive_daily._discard_local_artifact(artifact, day_root)
        return complete

    previous_day = _next_day(day, -1)
    previous_manifest = archive_daily._archive_manifest(s3, bucket, previous_day, "rec")
    current_manifest = archive_daily._archive_manifest(s3, bucket, day, "rec")
    following_manifest = archive_daily._archive_manifest(s3, bucket, following_day, "rec")
    poly_manifest = archive_daily._archive_manifest(s3, bucket, day, "poly")
    archive_daily.validate_local_archive(current_manifest, data_root, day, "rec")
    archive_daily.validate_local_archive(poly_manifest, poly_root, day, "poly")
    venue_items = _validated_venue_items(
        data_root, previous_manifest, current_manifest, following_manifest, day,
    )
    archive_snapshot = day_root / "archive-manifests.json"
    _atomic_json(archive_snapshot, {
        "previous_boundary": {
            "day": previous_day,
            "files": [row for row in venue_items if f".{previous_day}T23." in str(row["file"])],
        },
        "current_rec": current_manifest,
        "following_boundary": {
            "day": following_day,
            "files": [row for row in venue_items if f".{following_day}T00." in str(row["file"])],
        },
        "current_poly": poly_manifest,
    })
    artifact = archive_daily._build_artifact(
        data_root, poly_root, day_root, day, archive_snapshot,
    )
    strict_manifest = artifact / "manifest.json"
    archive_sha256 = archive_daily._sha256(archive_snapshot)
    strict_manifest_sha256 = archive_daily._sha256(strict_manifest)
    result_path = day_root / "result.json"
    payload = _existing_result(
        result_path,
        day=day,
        fingerprint=fingerprint,
        calibration_sha256=calibration_sha256,
        provenance_sha256=provenance_sha256,
        evaluator_sha256=evaluator_sha256,
        archive_sha256=archive_sha256,
        strict_manifest_sha256=strict_manifest_sha256,
    )
    venue_view = day_root / "venues"
    if payload is None:
        _venue_view(data_root, venue_view, venue_items)
        try:
            result = proxy.run(
                venue_view,
                artifact,
                proxy._iso_timestamp(freeze["calibration_end"]),
                proxy._iso_timestamp(freeze["holdout_start"]),
                proxy.ProxyConfig(**freeze["proxy_config"]),
                calibration_pin=calibration_path,
                fingerprint=fingerprint,
                include_observations=True,
            )
        finally:
            if venue_view.exists():
                shutil.rmtree(venue_view)
        observed = result["observed_outcome_audit"]
        if (
            observed["unresolved_market_count"]
            or observed["missing_exact"]
            or observed["mismatch_count"]
            or not observed["passes"]
        ):
            raise ValueError("daily settlement outcomes are not completely auditable")
        payload = {
            "schema": forward.DAY_SCHEMA,
            "day": day,
            "strategy_fingerprint": fingerprint,
            "calibration_sha256": calibration_sha256,
            "provenance_sha256": provenance_sha256,
            "evaluator_sha256": evaluator_sha256,
            "archive_manifests_sha256": archive_sha256,
            "strict_manifest_sha256": strict_manifest_sha256,
            "protocol": result["protocol"],
            "source_stats": result["source_stats"],
            "signal_audit": result["signals"],
            "daily_execution": result["execution"],
            "daily_gate": result["gate"],
            "observations": result["observations"],
        }
        _atomic_json(result_path, payload)

    base_state_present = _state_hashes(state)
    staged_state = _stage_state(state, day_root / "state-next")
    pooled = forward.update(
        result_path,
        staged_state,
        fingerprint=fingerprint,
        calibration_sha256=calibration_sha256,
        provenance_sha256=provenance_sha256,
        evaluator_sha256=evaluator_sha256,
        holdout_day=holdout_day,
    )
    copied_manifest = day_root / "strict-manifest.json"
    shutil.copyfile(strict_manifest, copied_manifest)
    evidence = [archive_snapshot, copied_manifest, result_path]
    evidence.extend(staged_state / relative for relative in _state_paths(staged_state))
    base_state_hashes, target_state_hashes = _aligned_state_hashes(state, staged_state)
    if {name: digest for name, digest in base_state_hashes.items() if digest is not None} != base_state_present:
        raise ValueError("settlement-proxy base state changed while staging")
    _atomic_json(complete_path, {
        "schema": "settlement-proxy-complete-day-v2",
        "status": "complete",
        "day": day,
        "strategy_fingerprint": fingerprint,
        "calibration_sha256": calibration_sha256,
        "provenance_sha256": provenance_sha256,
        "evaluator_sha256": evaluator_sha256,
        "result_sha256": archive_daily._sha256(result_path),
        "pooled_verdict": pooled["verdict"],
        "base_state_sha256": base_state_hashes,
        "target_state_sha256": target_state_hashes,
        "evidence": [
            {
                "path": path.relative_to(day_root).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": archive_daily._sha256(path),
            }
            for path in evidence
        ],
    })
    _upload_evidence(s3, bucket, day, day_root, [*evidence, complete_path])
    complete = json.loads(complete_path.read_text(encoding="utf-8"))
    _commit_completed_day(complete, complete_path, state, day_root)
    archive_daily._discard_local_artifact(artifact, day_root)
    if staged_state.exists():
        shutil.rmtree(staged_state)
    return complete


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day")
    parser.add_argument("--data", default="/home/ubuntu/rec/data")
    parser.add_argument("--poly", default="/home/ubuntu/rec/data_poly")
    parser.add_argument("--work", default="/home/ubuntu/rec/formal/settlement-proxy-days")
    parser.add_argument("--state", default="/home/ubuntu/rec/formal/settlement-proxy-state")
    parser.add_argument("--bucket", default=BUCKET)
    args = parser.parse_args()
    freeze = proxy.load_freeze()
    fingerprint = proxy.strategy_fingerprint(freeze)
    holdout_day = str(freeze["holdout_start"])[:10].replace("-", "")
    state = Path(args.state)
    evaluator, evaluator_sha256 = load_evaluator_freeze()
    admission = _read_admission(
        state,
        fingerprint=fingerprint,
        calibration_sha256=str(evaluator["calibration_sha256"]),
        provenance_sha256=str(evaluator["provenance_sha256"]),
        evaluator_sha256=evaluator_sha256,
        validate_state=False,
    )
    if args.day is None:
        args.day = holdout_day if admission is None else _next_day(str(admission["day"]))
    if re.fullmatch(r"\d{8}", args.day) and _next_day(args.day) >= _utc_today():
        print(json.dumps({
            "status": "skipped",
            "day": args.day,
            "reason": "following_UTC_boundary_day_not_closed",
        }, sort_keys=True))
        return
    state.mkdir(parents=True, exist_ok=True)
    with (state / ".daily.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = process_day(
            args.day,
            args.data,
            args.poly,
            args.work,
            args.state,
            s3=boto3.client("s3", region_name="eu-west-1"),
            bucket=args.bucket,
        )
    print(json.dumps(result, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
