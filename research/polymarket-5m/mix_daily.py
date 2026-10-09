"""Build and admit one closed eu-west MIX control and R1-17F day."""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import gzip
import heapq
import json
import math
import os
import re
import shutil
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

import boto3

import mix_agg_rec
import mix_forward as control
import mix_hf
import mix_projection as projection
import source_race_daily as archive_daily


BUCKET = archive_daily.BUCKET
EVIDENCE_PREFIX = "formal/mix-v1"
EVALUATOR_FREEZE = Path(__file__).with_name("forward") / "mix-evaluator-freeze.json"
CONTROL_FREEZE = Path(__file__).with_name("forward") / "mix-control-freeze.json"
PROJECTION_FREEZE = Path(__file__).with_name("forward") / "mix-r1-17f-freeze.json"
AGG_SOURCES = mix_agg_rec.AGG_SOURCES
PRE_ROLL_HOURS = math.ceil(mix_hf.MIGRATION_KLINE_PRE_ROLL_S / 3_600)
ADMISSION = "admission.json"
STATUS = "status.json"
CONTROL_LEDGER = "mix-control-observations.jsonl"
CONTROL_VERDICT = "mix-control-verdict.json"
PROJECTION_LEDGER = "mix-r1-17f-observations.jsonl"
PROJECTION_REPORT = "mix-r1-17f-report.json"
PROJECTION_VERDICT = "mix-r1-17f-verdict.json"
STATE_FILES = (
    CONTROL_LEDGER,
    CONTROL_VERDICT,
    PROJECTION_LEDGER,
    PROJECTION_REPORT,
    PROJECTION_VERDICT,
    STATUS,
)


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
    value = dt.date.fromisoformat(f"{day[:4]}-{day[4:6]}-{day[6:]}")
    return (value + dt.timedelta(days=amount)).strftime("%Y%m%d")


def _utc_today() -> str:
    return dt.datetime.now(dt.timezone.utc).date().strftime("%Y%m%d")


def load_evaluator_freeze(path: str | Path = EVALUATOR_FREEZE) -> tuple[dict[str, Any], str]:
    source = Path(path)
    digest = archive_daily._sha256(source)
    expected = source.with_suffix(".sha256").read_text(encoding="ascii").strip()
    if digest != expected or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("MIX evaluator freeze fingerprint drift")
    payload = json.loads(source.read_text(encoding="utf-8"))
    if payload.get("schema") != "polymarket-mix-evaluator-freeze-v1":
        raise ValueError("unknown MIX evaluator freeze schema")
    expected_fields = {
        "aggregate_feed": mix_agg_rec.AGG_URL,
        "aggregate_sources": list(AGG_SOURCES),
        "holdout_start_utc": "2026-10-10T00:00:00Z",
        "orders": "paper_only",
        "pre_roll_hours": PRE_ROLL_HOURS,
    }
    changed = [key for key, value in expected_fields.items() if payload.get(key) != value]
    if changed:
        raise ValueError("MIX evaluator differs from code: " + ", ".join(changed))
    root = Path(__file__).resolve().parent
    for relative, expected_sha256 in payload.get("dependencies_sha256", {}).items():
        dependency = root / str(relative)
        if not dependency.is_file() or archive_daily._sha256(dependency) != expected_sha256:
            raise ValueError(f"MIX evaluator dependency drift: {relative}")
    return payload, digest


def _validate_manifest_files(manifest: Mapping[str, Any], day: str, group: str) -> None:
    if manifest.get("day") != day or manifest.get("group") != group:
        raise ValueError(f"{group} archive manifest identity mismatch")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError(f"{group} archive manifest has no files")
    names: list[str] = []
    for item in files:
        if not (
            isinstance(item, Mapping)
            and item.get("uploaded_ok") is True
            and item.get("gzip_ok") is True
            and int(item.get("size") or 0) > 0
            and re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256") or ""))
        ):
            raise ValueError(f"{group} archive contains an unverified file")
        names.append(str(item["file"]))
    if len(names) != len(set(names)):
        raise ValueError(f"{group} archive manifest contains duplicate files")


def _mix_manifest(s3: Any, bucket: str, day: str) -> dict[str, Any]:
    response = s3.get_object(Bucket=bucket, Key=f"mix/{day}/MANIFEST.json")
    manifest = json.loads(response["Body"].read())
    _validate_manifest_files(manifest, day, "mix")
    return manifest


def aggregate_source_items(
    manifest: Mapping[str, Any], day: str, hours: set[int],
) -> list[Mapping[str, Any]]:
    expected = {
        f"{source}.{day}T{hour:02d}.txt.gz"
        for source in AGG_SOURCES
        for hour in hours
    }
    selected = [item for item in manifest["files"] if str(item.get("file")) in expected]
    if {str(item["file"]) for item in selected} != expected:
        raise ValueError(f"MIX aggregate route coverage is incomplete for {day}")
    return sorted(selected, key=lambda item: str(item["file"]))


def _validate_local_items(root: Path, items: Iterable[Mapping[str, Any]]) -> None:
    for item in items:
        path = root / str(item["file"])
        if (
            not path.is_file()
            or path.stat().st_size != int(item["size"])
            or archive_daily._sha256(path) != item["sha256"]
        ):
            raise ValueError(f"local MIX archive differs from verified S3 manifest: {path.name}")


def _source_events(
    root: Path, source: str, items: Iterable[Mapping[str, Any]], source_index: int,
) -> Iterator[tuple[int, int, int, dict[str, Any]]]:
    sequence = 0
    previous_receive = -1
    paths = sorted(
        (root / str(item["file"]) for item in items if str(item["file"]).startswith(source + ".")),
        key=lambda path: path.name,
    )
    for path in paths:
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                try:
                    raw_receive, raw_payload = line.rstrip("\n").split("\t", 1)
                    receive_ns = int(raw_receive)
                    payload = json.loads(raw_payload)
                except (TypeError, ValueError, json.JSONDecodeError) as error:
                    raise ValueError(f"malformed MIX aggregate frame at {path}:{line_number}") from error
                if receive_ns < previous_receive:
                    raise ValueError(f"MIX aggregate receipt clock regressed at {path}:{line_number}")
                previous_receive = receive_ns
                lifecycle = payload.get("_recorder") if isinstance(payload, Mapping) else None
                if isinstance(lifecycle, Mapping):
                    kind = str(lifecycle.get("kind"))
                    if kind not in {"connection", "disconnect"}:
                        continue
                    try:
                        epoch = int(lifecycle["epoch"])
                    except (KeyError, TypeError, ValueError) as error:
                        raise ValueError(f"invalid MIX aggregate lifecycle at {path}:{line_number}") from error
                    event = {"kind": kind, "source": source, "epoch": epoch}
                else:
                    data = payload.get("data", payload) if isinstance(payload, Mapping) else {}
                    if data.get("e") != "aggTrade":
                        continue
                    try:
                        aggregate_id = int(data["a"])
                        source_time = float(data["T"])
                        price = float(data["p"])
                        quantity = float(data["q"])
                        maker = bool(data["m"])
                    except (KeyError, TypeError, ValueError) as error:
                        raise ValueError(f"malformed MIX aggregate trade at {path}:{line_number}") from error
                    if source_time > 1e14:
                        source_time /= 1_000_000.0
                    elif source_time > 1e11:
                        source_time /= 1_000.0
                    if not all(math.isfinite(value) for value in (source_time, price, quantity)) or price <= 0:
                        raise ValueError(f"invalid MIX aggregate trade at {path}:{line_number}")
                    event = {
                        "kind": "trade",
                        "source": source,
                        "aggregate_id": aggregate_id,
                        "trade_ts": source_time,
                        "price": price,
                        "quantity": quantity,
                        "maker": maker,
                    }
                yield receive_ns, source_index, sequence, event
                sequence += 1


def build_aggregate_tape(
    root: str | Path,
    items: list[Mapping[str, Any]],
    destination: str | Path,
) -> dict[str, Any]:
    root, destination = Path(root), Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    active: dict[str, int] = {}
    seen_sources: set[str] = set()
    seen: dict[int, tuple[float, float, float, bool]] = {}
    raw_trades = duplicate_trades = lifecycle_events = 0
    first_receive_ns: int | None = None
    last_receive_ns: int | None = None
    iterators = [
        _source_events(root, source, items, index)
        for index, source in enumerate(AGG_SOURCES)
    ]
    try:
        with gzip.open(temporary, "wt", encoding="utf-8", compresslevel=3) as stream:
            for receive_ns, _source_index, _sequence, event in heapq.merge(*iterators):
                source = str(event["source"])
                kind = str(event["kind"])
                if kind == "connection":
                    lifecycle_events += 1
                    seen_sources.add(source)
                    active[source] = int(event["epoch"])
                    continue
                if kind == "disconnect":
                    lifecycle_events += 1
                    if active.get(source) in (-1, int(event["epoch"])):
                        active.pop(source)
                    if seen_sources == set(AGG_SOURCES) and not active:
                        raise ValueError("MIX aggregate routes became jointly disconnected")
                    continue
                raw_trades += 1
                if source not in active:
                    if source in seen_sources:
                        raise ValueError("MIX aggregate trade arrived outside its connection epoch")
                    seen_sources.add(source)
                    active[source] = -1
                identity = int(event["aggregate_id"])
                body = (
                    float(event["trade_ts"]),
                    float(event["price"]),
                    float(event["quantity"]),
                    bool(event["maker"]),
                )
                previous = seen.get(identity)
                if previous is not None:
                    duplicate_trades += 1
                    if previous != body:
                        raise ValueError(f"conflicting aggregate trade {identity}")
                    continue
                seen[identity] = body
                first_receive_ns = receive_ns if first_receive_ns is None else first_receive_ns
                last_receive_ns = receive_ns
                row = {
                    "event": "BINANCE_WS_TRADE",
                    "aggregate_id": identity,
                    "trade_ts": body[0],
                    "receive_ts": receive_ns / 1_000_000_000.0,
                    "price": body[1],
                }
                stream.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
        if seen_sources != set(AGG_SOURCES) or not seen:
            raise ValueError("MIX aggregate tape lacks all three routes or trades")
        temporary.replace(destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {
        "schema": "polymarket-mix-aggregate-tape-v1",
        "sources": list(AGG_SOURCES),
        "input_files": [
            {"file": str(item["file"]), "size": int(item["size"]), "sha256": str(item["sha256"])}
            for item in sorted(items, key=lambda row: str(row["file"]))
        ],
        "raw_trades": raw_trades,
        "deduplicated_trades": len(seen),
        "duplicate_trades": duplicate_trades,
        "lifecycle_events": lifecycle_events,
        "joint_disconnects": 0,
        "first_receive_ns": first_receive_ns,
        "last_receive_ns": last_receive_ns,
        "tape_sha256": archive_daily._sha256(destination),
    }


def state_hashes(state: str | Path) -> dict[str, str | None]:
    root = Path(state)
    return {
        name: archive_daily._sha256(root / name) if (root / name).is_file() else None
        for name in STATE_FILES
    }


def _stage_state(state: Path, staged: Path) -> Path:
    if staged.exists():
        shutil.rmtree(staged)
    staged.mkdir(parents=True)
    for name in STATE_FILES:
        source = state / name
        if source.is_file():
            shutil.copyfile(source, staged / name)
    return staged


def commit_state(
    complete: Mapping[str, Any], state: str | Path, staged: str | Path, complete_sha256: str,
) -> None:
    state, staged = Path(state), Path(staged)
    base = complete["base_state_sha256"]
    target = complete["target_state_sha256"]
    for name in STATE_FILES:
        current = archive_daily._sha256(state / name) if (state / name).is_file() else None
        if current not in (base[name], target[name]):
            raise ValueError(f"MIX state changed while committing {name}")
        source = staged / name
        if target[name] is not None and (
            not source.is_file() or archive_daily._sha256(source) != target[name]
        ):
            raise ValueError(f"staged MIX state changed: {name}")
    state.mkdir(parents=True, exist_ok=True)
    for name, digest in target.items():
        if digest is None:
            continue
        temporary = state / f".{name}.tmp"
        shutil.copyfile(staged / name, temporary)
        temporary.replace(state / name)
    _atomic_json(state / ADMISSION, {
        "schema": "polymarket-mix-admission-v1",
        "day": complete["day"],
        "evaluator_freeze_sha256": complete["evaluator_freeze_sha256"],
        "complete_sha256": complete_sha256,
        "state_sha256": target,
    })


def _read_admission(
    state: Path, evaluator_sha256: str, *, validate_state: bool = True,
) -> dict[str, Any] | None:
    path = state / ADMISSION
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        payload.get("schema") != "polymarket-mix-admission-v1"
        or payload.get("evaluator_freeze_sha256") != evaluator_sha256
        or set(payload.get("state_sha256", {})) != set(STATE_FILES)
    ):
        raise ValueError("MIX admission identity drift")
    if validate_state:
        for name, digest in payload["state_sha256"].items():
            current = archive_daily._sha256(state / name) if (state / name).is_file() else None
            if current != digest:
                raise ValueError("MIX admitted state hash mismatch")
    return payload


def _upload_evidence(
    s3: Any, bucket: str, day: str, root: Path, files: Iterable[Path],
) -> list[dict[str, Any]]:
    rows = []
    for path in files:
        relative = path.relative_to(root).as_posix()
        rows.append(archive_daily._upload_file(
            s3, bucket, f"{EVIDENCE_PREFIX}/{day}/{relative}", path,
        ))
    return rows


def _terminal(state: Path) -> bool:
    statuses = []
    for name in (CONTROL_VERDICT, PROJECTION_VERDICT):
        path = state / name
        if not path.is_file():
            return False
        payload = json.loads(path.read_text(encoding="utf-8"))
        statuses.append(payload.get("status"))
    if any(status not in {"passed", "rejected"} for status in statuses):
        raise ValueError("MIX pinned verdict has a non-terminal status")
    return True


def process_day(
    day: str,
    data_dir: str | Path,
    poly_dir: str | Path,
    mix_dir: str | Path,
    work_dir: str | Path,
    state_dir: str | Path,
    *,
    s3: Any,
    bucket: str = BUCKET,
    evaluator_path: str | Path = EVALUATOR_FREEZE,
    control_freeze: str | Path = CONTROL_FREEZE,
    projection_freeze: str | Path = PROJECTION_FREEZE,
) -> dict[str, Any]:
    evaluator, evaluator_sha256 = load_evaluator_freeze(evaluator_path)
    frozen, config = control.load_control_freeze(control_freeze)
    projected, _holdout_ms = projection.load_protocol(projection_freeze)
    if (
        evaluator.get("control_protocol_sha256") != frozen["protocol_sha256"]
        or evaluator.get("projection_protocol_sha256") != projected["protocol_sha256"]
    ):
        raise ValueError("MIX evaluator protocol binding drift")
    cutoff_day = str(evaluator["holdout_start_utc"])[:10].replace("-", "")
    if not re.fullmatch(r"\d{8}", day) or day < cutoff_day:
        raise ValueError("day is outside the frozen MIX holdout")
    if day >= _utc_today():
        raise ValueError("MIX day is not a closed UTC day")
    state = Path(state_dir)
    day_root = Path(work_dir) / day
    complete_path = day_root / "complete.json"
    admission = _read_admission(
        state, evaluator_sha256, validate_state=not complete_path.is_file(),
    )
    if admission is not None and not complete_path.is_file() and _terminal(state):
        return {
            "status": "terminal",
            "day": admission["day"],
            "control": json.loads((state / CONTROL_VERDICT).read_text(encoding="utf-8")),
            "projection": json.loads((state / PROJECTION_VERDICT).read_text(encoding="utf-8")),
        }
    expected_day = cutoff_day if admission is None else _next_day(str(admission["day"]))
    if admission is not None and day == admission["day"]:
        expected_day = day
    if day != expected_day:
        raise ValueError(f"day is not the next MIX admission: expected {expected_day}")
    day_root.mkdir(parents=True, exist_ok=True)
    if complete_path.is_file():
        complete = json.loads(complete_path.read_text(encoding="utf-8"))
        if (
            complete.get("schema") != "polymarket-mix-day-v1"
            or complete.get("status") != "complete"
            or complete.get("day") != day
            or complete.get("evaluator_freeze_sha256") != evaluator_sha256
        ):
            raise ValueError("completed MIX day identity drift")
        evidence = []
        for item in complete.get("evidence", []):
            path = day_root / str(item["path"])
            if not path.is_file() or archive_daily._sha256(path) != item["sha256"]:
                raise ValueError("completed MIX evidence changed")
            evidence.append(path)
        _upload_evidence(s3, bucket, day, day_root, [*evidence, complete_path])
        complete_sha256 = archive_daily._sha256(complete_path)
        if admission is not None and day == admission.get("day"):
            if (
                admission.get("complete_sha256") != complete_sha256
                or admission.get("state_sha256") != complete["target_state_sha256"]
                or state_hashes(state) != complete["target_state_sha256"]
            ):
                raise ValueError("admitted MIX day differs from its completed evidence")
        else:
            staged = day_root / "state-next"
            if not staged.is_dir():
                staged = day_root / "state-snapshot"
            commit_state(complete, state, staged, complete_sha256)
        for temporary in (day_root / "artifact", day_root / "latency", day_root / "state-next"):
            if temporary.exists():
                shutil.rmtree(temporary)
        return complete

    current_rec = archive_daily._archive_manifest(s3, bucket, day, "rec")
    current_poly = archive_daily._archive_manifest(s3, bucket, day, "poly")
    previous_day = _next_day(day, -1)
    previous_mix = _mix_manifest(s3, bucket, previous_day)
    current_mix = _mix_manifest(s3, bucket, day)
    archive_daily.validate_local_archive(current_rec, Path(data_dir), day, "rec")
    archive_daily.validate_local_archive(current_poly, Path(poly_dir), day, "poly")
    pre_roll_hours = set(range(24 - PRE_ROLL_HOURS, 24))
    aggregate_items = [
        *aggregate_source_items(previous_mix, previous_day, pre_roll_hours),
        *aggregate_source_items(current_mix, day, set(range(24))),
    ]
    _validate_local_items(Path(mix_dir), aggregate_items)
    archive_snapshot = day_root / "archive-manifests.json"
    _atomic_json(archive_snapshot, {
        "rec": current_rec,
        "poly": current_poly,
        "mix_previous": previous_mix,
        "mix_current": current_mix,
    })
    artifact = archive_daily._build_artifact(
        Path(data_dir), Path(poly_dir), day_root, day, archive_snapshot,
    )
    tape = day_root / "latency" / "binance_trades.jsonl.gz"
    tape_profile_path = day_root / "aggregate-tape.json"
    tape_profile = build_aggregate_tape(Path(mix_dir), aggregate_items, tape)
    tape_profile.update({
        "day": day,
        "evaluator_freeze_sha256": evaluator_sha256,
        "archive_manifests_sha256": archive_daily._sha256(archive_snapshot),
    })
    _atomic_json(tape_profile_path, tape_profile)
    rows_path = day_root / "control-rows.jsonl"
    report_path = day_root / "control-report.json"
    replay = control.replay_archive(
        artifact,
        config=config,
        rows_out=rows_path,
        source_protocol_sha256=frozen["protocol_sha256"],
    )
    if replay.get("dataset", {}).get("paper_gate_eligible") is not True:
        raise ValueError("MIX replay is not eu-west formal-sample eligible")
    _atomic_json(report_path, replay)
    base_hashes = state_hashes(state)
    staged = _stage_state(state, day_root / "state-next")
    pooled = control.append_observations(
        staged / CONTROL_LEDGER, rows_path, staged / CONTROL_VERDICT,
    )
    control_status = control.pin_verdict(
        pooled, staged / CONTROL_VERDICT, frozen, staged / CONTROL_LEDGER,
    )
    projection_status = projection.run_protocol(
        projection_freeze,
        staged / CONTROL_LEDGER,
        staged / PROJECTION_LEDGER,
        staged / PROJECTION_REPORT,
        staged / PROJECTION_VERDICT,
    )
    status = {
        "schema": "polymarket-mix-status-v1",
        "day": day,
        "evaluator_freeze_sha256": evaluator_sha256,
        "control": control_status,
        "projection": projection_status["verdict"],
    }
    _atomic_json(staged / STATUS, status)
    state_snapshot = day_root / "state-snapshot"
    if state_snapshot.exists():
        shutil.rmtree(state_snapshot)
    state_snapshot.mkdir()
    for name in STATE_FILES:
        source = staged / name
        if source.is_file():
            shutil.copyfile(source, state_snapshot / name)
    shutil.copyfile(artifact / "manifest.json", day_root / "strict-manifest.json")
    shutil.copyfile(
        artifact / archive_daily.ARTIFACT_BINDING,
        day_root / "strict-input-binding.json",
    )
    target_hashes = state_hashes(staged)
    evidence = [
        archive_snapshot,
        tape_profile_path,
        day_root / "strict-manifest.json",
        day_root / "strict-input-binding.json",
        rows_path,
        report_path,
        *(state_snapshot / name for name in STATE_FILES if (state_snapshot / name).is_file()),
    ]
    evidence_rows = [
        {
            "path": path.relative_to(day_root).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": archive_daily._sha256(path),
        }
        for path in evidence
    ]
    complete = {
        "schema": "polymarket-mix-day-v1",
        "status": "complete",
        "day": day,
        "evaluator_freeze_sha256": evaluator_sha256,
        "control_protocol_sha256": frozen["protocol_sha256"],
        "projection_protocol_sha256": projected["protocol_sha256"],
        "base_state_sha256": base_hashes,
        "target_state_sha256": target_hashes,
        "forward_status": status,
        "evidence": evidence_rows,
    }
    _atomic_json(complete_path, complete)
    _upload_evidence(s3, bucket, day, day_root, [*evidence, complete_path])
    commit_state(complete, state, staged, archive_daily._sha256(complete_path))
    archive_daily._discard_local_artifact(artifact, day_root)
    for temporary in (day_root / "latency", staged):
        if temporary.exists():
            shutil.rmtree(temporary)
    return complete


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day")
    parser.add_argument("--data", default="/home/ubuntu/rec/data")
    parser.add_argument("--poly", default="/home/ubuntu/rec/data_poly")
    parser.add_argument("--mix", default="/home/ubuntu/rec/data_mix")
    parser.add_argument("--work", default="/home/ubuntu/rec/formal/mix-days")
    parser.add_argument("--state", default="/home/ubuntu/rec/formal/mix-state")
    parser.add_argument("--bucket", default=BUCKET)
    args = parser.parse_args()
    evaluator, evaluator_sha256 = load_evaluator_freeze()
    cutoff_day = str(evaluator["holdout_start_utc"])[:10].replace("-", "")
    state = Path(args.state)
    admission = _read_admission(state, evaluator_sha256, validate_state=False)
    if args.day is None:
        args.day = cutoff_day if admission is None else _next_day(str(admission["day"]))
        if args.day >= _utc_today():
            print(json.dumps({
                "status": "skipped",
                "day": args.day,
                "reason": "UTC_day_not_closed",
            }, sort_keys=True))
            return
    state.mkdir(parents=True, exist_ok=True)
    with (state / ".daily.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = process_day(
            args.day,
            args.data,
            args.poly,
            args.mix,
            args.work,
            args.state,
            s3=boto3.client("s3", region_name="eu-west-1"),
            bucket=args.bucket,
        )
    print(json.dumps(result, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
