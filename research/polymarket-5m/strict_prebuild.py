"""Build one immutable strict eu-west artifact for all daily forward evaluators."""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
import eu_strict
import h_replay_archive as archive
import source_race_daily as daily

DEFAULT_ROOT = "/home/ubuntu/rec/formal/strict-days"
FORMAL_CUTOFF_DAY = "20261010"


@dataclass(frozen=True)
class Lane:
    state: Path
    terminal_files: tuple[str, ...]
    progress_format: str = "admission"
    cutoff_day: str | None = None


DEFAULT_LANES = {
    "source": Lane(
        Path("/home/ubuntu/rec/formal/source-race-state"), ("verdict.json",),
    ),
    "settlement": Lane(
        Path("/home/ubuntu/rec/formal/settlement-proxy-state"), ("verdict.json",),
    ),
    "mix": Lane(
        Path("/home/ubuntu/rec/formal/mix-state"),
        ("mix-control-verdict.json", "mix-r1-17f-verdict.json"),
    ),
    "absorption": Lane(
        Path("/home/ubuntu/rec/formal/absorption-state"), ("verdict.json",),
    ),
    "complement_queue_lag": Lane(
        Path("/home/ubuntu/rec/formal/complement-queue-lag-state"),
        (),
        progress_format="family34_generation",
        cutoff_day="20261012",
    ),
}


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(payload).hexdigest()


def raw_archive_identity(rec: Mapping[str, Any], poly: Mapping[str, Any]) -> str:
    return _canonical_sha256({"rec": rec, "poly": poly})


def _previous_utc_day() -> str:
    return (dt.datetime.now(dt.UTC).date() - dt.timedelta(days=1)).strftime("%Y%m%d")


def _next_day(day: str) -> str:
    value = dt.datetime.strptime(day, "%Y%m%d").date() + dt.timedelta(days=1)
    return value.strftime("%Y%m%d")


def read_lane_progress(
    lanes: Mapping[str, Lane], cutoff_day: str,
) -> dict[str, dict[str, Any]]:
    if not cutoff_day.isdigit() or len(cutoff_day) != 8:
        raise ValueError("formal cutoff day must be YYYYMMDD")
    progress: dict[str, dict[str, Any]] = {}
    for name, lane in lanes.items():
        lane_cutoff = lane.cutoff_day or cutoff_day
        if not lane_cutoff.isdigit() or len(lane_cutoff) != 8:
            raise ValueError(f"{name} cutoff day drift")
        if lane.progress_format == "family34_generation":
            import complement_queue_lag_daily as queue_daily

            if not lane.state.exists():
                generation_day = None
                generation_terminal = False
            else:
                frozen = queue_daily._read_json(queue_daily.FREEZE_PATH)
                protocol = queue_daily._validate_config(frozen)
                current = queue_daily._load_current(lane.state, protocol)
                if current is None:
                    generation_day = None
                    generation_terminal = False
                else:
                    latest = current.committed["days"][-1]
                    generation_day = str(latest["day"]).replace("-", "")
                    generation_terminal = (
                        latest["forward_status"].get("status") != "collecting"
                    )
            progress[name] = {
                "admitted_day": generation_day,
                "next_day": None if generation_terminal else (
                    lane_cutoff
                    if generation_day is None
                    else _next_day(generation_day)
                ),
                "terminal": generation_terminal,
            }
            continue
        if lane.progress_format != "admission":
            raise ValueError(f"{name} progress format drift")
        admission_path = lane.state / "admission.json"
        admitted_day: str | None = None
        if admission_path.is_file():
            admission = json.loads(admission_path.read_text(encoding="utf-8"))
            admitted_day = str(admission.get("day") or "")
            if not admitted_day.isdigit() or len(admitted_day) != 8 or admitted_day < lane_cutoff:
                raise ValueError(f"{name} admission day drift")
        terminal = bool(lane.terminal_files) and all(
            (lane.state / filename).is_file() for filename in lane.terminal_files
        )
        if terminal and admitted_day is None:
            raise ValueError(f"{name} terminal state exists without admission")
        progress[name] = {
            "admitted_day": admitted_day,
            "next_day": None if terminal else (
                lane_cutoff if admitted_day is None else _next_day(admitted_day)
            ),
            "terminal": terminal,
        }
    return progress


def pending_day(progress: Mapping[str, Mapping[str, Any]]) -> str | None:
    pending = [str(row["next_day"]) for row in progress.values() if row.get("next_day")]
    return min(pending) if pending else None


def gc_admitted_artifacts(
    root: str | Path, progress: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    admitted = [row.get("admitted_day") for row in progress.values()]
    if not admitted or any(day is None for day in admitted):
        return []
    watermark = min(str(day) for day in admitted)
    removed: list[str] = []
    root = Path(root)
    if not root.is_dir():
        return removed
    for day_root in sorted(root.iterdir()):
        if not day_root.is_dir() or not day_root.name.isdigit() or len(day_root.name) != 8:
            continue
        if day_root.name > watermark:
            continue
        changed = False
        for name in ("artifact", "artifact.tmp"):
            path = day_root / name
            if path.exists():
                shutil.rmtree(path)
                changed = True
        if changed:
            removed.append(day_root.name)
    return removed


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
    if day >= dt.datetime.now(dt.UTC).strftime("%Y%m%d"):
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
    staging = day_root / "artifact.tmp"
    if _valid_artifact(artifact, day, rec, poly):
        if staging.exists():
            shutil.rmtree(staging)
        return {"status": "complete", "day": day, "artifact": str(artifact)}
    if artifact.exists():
        raise ValueError("invalid immutable strict artifact already exists")
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
    parser.add_argument("--day")
    parser.add_argument("--phase", required=True, choices=("source", "clob", "finalize"))
    parser.add_argument("--data", default="/home/ubuntu/rec/data")
    parser.add_argument("--poly", default="/home/ubuntu/rec/data_poly")
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--bucket", default=daily.BUCKET)
    parser.add_argument("--cutoff-day", default=FORMAL_CUTOFF_DAY)
    args = parser.parse_args()
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".prebuild.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        progress = read_lane_progress(DEFAULT_LANES, args.cutoff_day)
        removed = gc_admitted_artifacts(root, progress)
        if args.day is None:
            args.day = pending_day(progress)
        if args.day is None:
            print(json.dumps({
                "status": "skipped",
                "reason": "all_forward_lanes_terminal",
                "removed_artifacts": removed,
            }, sort_keys=True))
            return
        if args.day >= dt.datetime.now(dt.UTC).strftime("%Y%m%d"):
            print(json.dumps({
                "status": "skipped",
                "day": args.day,
                "reason": "UTC_day_not_closed",
                "removed_artifacts": removed,
            }, sort_keys=True))
            return
        result = run_phase(
            args.day, args.phase, args.data, args.poly, args.root,
            s3=boto3.client("s3"), bucket=args.bucket,
        )
        result["removed_artifacts"] = removed
    print(json.dumps(result, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
