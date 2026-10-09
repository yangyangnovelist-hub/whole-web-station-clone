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
            if source.name == ".daily.lock":
                continue
            relative = source.relative_to(state)
            target = destination / relative
            if source.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
    return destination


def _commit_state(staged: Path, state: Path) -> None:
    state.mkdir(parents=True, exist_ok=True)
    day_files = sorted((staged / forward.DAYS_DIR).glob("*.json"))
    for source in day_files:
        destination = state / forward.DAYS_DIR / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".tmp")
        shutil.copyfile(source, temporary)
        temporary.replace(destination)
    for name in (forward.INDEX, forward.VERDICT, forward.STATUS):
        source = staged / name
        if not source.is_file():
            continue
        destination = state / name
        temporary = destination.with_name(destination.name + ".tmp")
        shutil.copyfile(source, temporary)
        temporary.replace(destination)


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

    state = Path(state_dir)
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
    evidence.extend(
        staged_state / name
        for name in (forward.INDEX, forward.STATUS, forward.VERDICT)
        if (staged_state / name).is_file()
    )
    complete_path = day_root / "complete.json"
    _atomic_json(complete_path, {
        "schema": "settlement-proxy-complete-day-v1",
        "day": day,
        "strategy_fingerprint": fingerprint,
        "calibration_sha256": calibration_sha256,
        "provenance_sha256": provenance_sha256,
        "evaluator_sha256": evaluator_sha256,
        "result_sha256": archive_daily._sha256(result_path),
        "pooled_verdict": pooled["verdict"],
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
    _commit_state(staged_state, state)
    archive_daily._discard_local_artifact(artifact, day_root)
    if staged_state.exists():
        shutil.rmtree(staged_state)
    return json.loads(complete_path.read_text(encoding="utf-8"))


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
    holdout_day = str(freeze["holdout_start"])[:10].replace("-", "")
    state = Path(args.state)
    index_path = state / forward.INDEX
    if args.day is None:
        if index_path.is_file():
            index = json.loads(index_path.read_text(encoding="utf-8"))
            args.day = _next_day(str(index["days"][-1]["day"]))
        else:
            args.day = holdout_day
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
