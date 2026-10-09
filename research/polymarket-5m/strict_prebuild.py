"""Build one immutable strict eu-west artifact for all daily forward evaluators."""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Mapping

import boto3

import eu_strict
import h_replay_archive as archive
import source_race_daily as daily


DEFAULT_ROOT = "/home/ubuntu/rec/formal/strict-days"


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(payload).hexdigest()


def raw_archive_identity(rec: Mapping[str, Any], poly: Mapping[str, Any]) -> str:
    return _canonical_sha256({"rec": rec, "poly": poly})


def _previous_utc_day() -> str:
    return (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)).strftime("%Y%m%d")


def _valid_artifact(
    artifact: Path,
    day: str,
    rec: Mapping[str, Any],
    poly: Mapping[str, Any],
) -> bool:
    try:
        checked = archive.validate_standard_artifact(artifact)
        binding = json.loads((artifact / daily.ARTIFACT_BINDING).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return False
    return bool(
        checked["manifest"].get("run_id") == f"eu-west-{day}"
        and binding.get("schema") == "eu-strict-input-binding-v1"
        and binding.get("day") == day
        and binding.get("raw_archive_manifests_sha256") == raw_archive_identity(rec, poly)
        and binding.get("strict_manifest_sha256") == daily._sha256(artifact / "manifest.json")
    )


def run_phase(
    day: str,
    phase: str,
    data_dir: str | Path,
    poly_dir: str | Path,
    root: str | Path,
    *,
    s3: Any,
    bucket: str = daily.BUCKET,
) -> dict[str, Any]:
    if phase not in {"source", "clob", "finalize"}:
        raise ValueError(f"unknown strict prebuild phase: {phase}")
    if day >= dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d"):
        raise ValueError("strict prebuild day is not a closed UTC day")
    day_root = Path(root) / day
    day_root.mkdir(parents=True, exist_ok=True)
    rec = daily._archive_manifest(s3, bucket, day, "rec")
    poly = daily._archive_manifest(s3, bucket, day, "poly")
    snapshot = day_root / "archive-manifests.json"
    expected_snapshot = {"rec": rec, "poly": poly}
    if snapshot.is_file():
        if json.loads(snapshot.read_text(encoding="utf-8")) != expected_snapshot:
            raise ValueError("strict prebuild archive manifest drift")
    else:
        daily._atomic_json(snapshot, expected_snapshot)
    artifact = day_root / "artifact"
    if _valid_artifact(artifact, day, rec, poly):
        return {"status": "complete", "day": day, "artifact": str(artifact)}
    if artifact.exists():
        raise ValueError("invalid immutable strict artifact already exists")
    staging = day_root / "artifact.tmp"
    staging.mkdir(exist_ok=True)
    if phase == "source":
        daily.validate_local_archive(rec, Path(data_dir), day, "rec")
        result = eu_strict.prepare_source(data_dir, staging, day)
        return {"status": "source_ready", "day": day, "phase": result}
    if phase == "clob":
        daily.validate_local_archive(poly, Path(poly_dir), day, "poly")
        result = eu_strict.prepare_clob(poly_dir, staging, day)
        return {"status": "clob_ready", "day": day, "phase": result}
    daily.validate_local_archive(rec, Path(data_dir), day, "rec")
    daily.validate_local_archive(poly, Path(poly_dir), day, "poly")
    manifest = eu_strict.finalize(staging, day)
    if manifest.get("complete") is not True:
        raise ValueError("strict prebuild is incomplete: " + ", ".join(manifest.get("failure_reasons") or ()))
    built = staging / "strict"
    archive.validate_standard_artifact(built)
    daily._atomic_json(built / daily.ARTIFACT_BINDING, {
        "schema": "eu-strict-input-binding-v1",
        "day": day,
        "raw_archive_manifests_sha256": raw_archive_identity(rec, poly),
        "strict_manifest_sha256": daily._sha256(built / "manifest.json"),
    })
    built.replace(artifact)
    shutil.rmtree(staging)
    return {"status": "complete", "day": day, "artifact": str(artifact)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", default=_previous_utc_day())
    parser.add_argument("--phase", required=True, choices=("source", "clob", "finalize"))
    parser.add_argument("--data", default="/home/ubuntu/rec/data")
    parser.add_argument("--poly", default="/home/ubuntu/rec/data_poly")
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--bucket", default=daily.BUCKET)
    args = parser.parse_args()
    day_root = Path(args.root) / args.day
    day_root.mkdir(parents=True, exist_ok=True)
    with (day_root / ".prebuild.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = run_phase(
            args.day, args.phase, args.data, args.poly, args.root,
            s3=boto3.client("s3"), bucket=args.bucket,
        )
    print(json.dumps(result, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
