"""Checksum-bound daily binder for the frozen US-017 paper evaluator."""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import heapq
import importlib
import json
import re
import shutil
import time
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

import boto3

import common_mode_residual as detector
import common_mode_run as replay
import h_replay_archive as archive
import source_race_daily as archive_daily


ROOT = Path(__file__).resolve().parent
PROTOCOL_FREEZE = ROOT / "forward" / "common-mode-residual-freeze.json"
EVALUATOR_FREEZE = ROOT / "forward" / "common-mode-evaluator-freeze.json"
RAW_SIDECAR_ROUTES = ("bn_spot", "deribit")
DAY_SCHEMA = "common-mode-residual-day-v1"
COMPLETE_SCHEMA = "common-mode-residual-complete-day-v1"
DAILY_PENDING = "common-mode-daily-pending.json"
DAILY_PENDING_SCHEMA = "common-mode-daily-pending-v1"
BUCKET = archive_daily.BUCKET
EVIDENCE_PREFIX = "formal/common-mode-residual-v1"


def _sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _atomic_json(path: str | Path, payload: Mapping[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)


def _load_hashed_json(path: str | Path, schema: str) -> tuple[dict[str, Any], str]:
    source = Path(path)
    digest = _sha256(source)
    checksum_path = source.with_suffix(".sha256")
    expected = checksum_path.read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", expected) or digest != expected:
        raise ValueError(f"freeze checksum drift: {source.name}")
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != schema:
        raise ValueError(f"freeze schema drift: {source.name}")
    for raw_name, expected_sha256 in payload.get("dependencies_sha256", {}).items():
        relative = Path(str(raw_name))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("freeze dependency path is invalid")
        dependency = ROOT / relative
        if not dependency.is_file() or _sha256(dependency) != expected_sha256:
            raise ValueError(f"freeze dependency drift: {raw_name}")
    return payload, digest


def load_protocol_freeze(
    path: str | Path = PROTOCOL_FREEZE,
) -> tuple[dict[str, Any], str]:
    return _load_hashed_json(path, "common-mode-residual-protocol-v1")


def load_evaluator_freeze(
    protocol: Mapping[str, Any],
    protocol_sha256: str,
    path: str | Path = EVALUATOR_FREEZE,
) -> tuple[dict[str, Any], str]:
    payload, digest = _load_hashed_json(path, "common-mode-evaluator-freeze-v1")
    if (
        payload.get("protocol_sha256") != protocol_sha256
        or payload.get("strategy_id") != protocol.get("strategy_id")
        or payload.get("holdout_start") != protocol.get("holdout_start")
    ):
        raise ValueError("common-mode evaluator/protocol identity drift")
    validator = getattr(_forward_module(), "validate_evaluator_freeze", None)
    if not callable(validator):
        raise ValueError("common-mode evaluator validator is unavailable")
    validator(payload, protocol)
    return payload, digest


def _selected_sidecar_items(
    manifest: Mapping[str, Any], route: str, day: str,
) -> list[Mapping[str, Any]]:
    if manifest.get("day") != day or manifest.get("group") != "rec":
        raise ValueError("rec archive manifest identity mismatch")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ValueError("rec archive manifest has no files")
    pattern = re.compile(rf"{re.escape(route)}\.{day}T(\d{{2}})\.txt\.gz")
    selected: dict[int, Mapping[str, Any]] = {}
    for item in files:
        if not isinstance(item, Mapping):
            continue
        match = pattern.fullmatch(str(item.get("file") or ""))
        if match is None:
            continue
        hour = int(match.group(1))
        if hour in selected:
            raise ValueError(f"{route} archive contains duplicate hour {hour:02d}")
        if not (
            item.get("uploaded_ok") is True
            and item.get("gzip_ok") is True
            and int(item.get("size") or 0) > 0
            and re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256") or ""))
        ):
            raise ValueError(f"{route} archive contains an unverified hour")
        selected[hour] = item
    if set(selected) != set(range(24)):
        raise ValueError(f"{route} archive hour coverage is incomplete for {day}")
    return [selected[hour] for hour in range(24)]


def _identity_rows(manifest: Mapping[str, Any], day: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for route in RAW_SIDECAR_ROUTES:
        for item in _selected_sidecar_items(manifest, route, day):
            rows.append({
                "file": str(item["file"]),
                "size": int(item["size"]),
                "sha256": str(item["sha256"]),
            })
    return rows


def raw_sidecar_identity(manifest: Mapping[str, Any], day: str) -> str:
    return _canonical_sha256({
        "schema": "common-mode-raw-sidecars-v1",
        "day": day,
        "files": _identity_rows(manifest, day),
    })


def bind_raw_sidecars(
    manifest: Mapping[str, Any], root: str | Path, day: str,
) -> tuple[dict[str, tuple[Path, ...]], str]:
    data_root = Path(root)
    selected: dict[str, tuple[Path, ...]] = {}
    for route in RAW_SIDECAR_ROUTES:
        paths: list[Path] = []
        for item in _selected_sidecar_items(manifest, route, day):
            path = data_root / str(item["file"])
            if (
                not path.is_file()
                or path.stat().st_size != int(item["size"])
                or _sha256(path) != item["sha256"]
            ):
                raise ValueError(
                    f"local rec archive differs from verified S3 manifest: {item['file']}"
                )
            paths.append(path)
        selected[route] = tuple(paths)
    return selected, raw_sidecar_identity(manifest, day)


def merge_source_events(
    strict_events: Iterable[dict[str, Any]],
    spot_bbo_events: Iterable[dict[str, Any]],
) -> Iterator[dict[str, Any]]:
    """Merge two independently sorted receipt streams without hiding exact ties."""

    def ranked(
        events: Iterable[dict[str, Any]], rank: int,
    ) -> Iterator[tuple[tuple[float, int, int], dict[str, Any]]]:
        previous = -float("inf")
        for sequence, event in enumerate(events):
            receipt = float(event["recv_ms"])
            if receipt < previous:
                raise ValueError("source sidecar receipt clock regressed")
            previous = receipt
            yield (
                (receipt, rank, int(event.get("seq", sequence))),
                event,
            )

    merged = heapq.merge(
        ranked(strict_events, 0),
        ranked(spot_bbo_events, 1),
        key=lambda item: item[0],
    )
    for sequence, (_, event) in enumerate(merged):
        yield {**event, "seq": sequence}


def persist_day_result(path: str | Path, payload: Mapping[str, Any]) -> str:
    destination = Path(path)
    encoded = json.dumps(
        payload,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ) + "\n"
    if destination.is_file():
        if destination.read_text(encoding="utf-8") != encoded:
            raise ValueError("immutable day result cannot be replaced")
        return _sha256(destination)
    _atomic_json(destination, payload)
    return _sha256(destination)


def _forward_module():
    return importlib.import_module("common_mode_forward")


def _iso_day(day: str) -> str:
    if not re.fullmatch(r"\d{8}", day):
        raise ValueError("day must be YYYYMMDD")
    return f"{day[:4]}-{day[4:6]}-{day[6:]}"


def _utc_today() -> str:
    return dt.datetime.now(dt.timezone.utc).date().strftime("%Y%m%d")


def detector_config(protocol: Mapping[str, Any]) -> detector.DetectorConfig:
    signal = protocol["signal"]
    execution = protocol["execution"]
    remaining = signal["remaining_seconds"]
    config = detector.DetectorConfig(
        symbol=str(signal["symbol"]),
        window_ms=float(signal["common_move_window_ms"]),
        confirmation_timeout_ms=float(signal["benchmark_confirmation_timeout_ms"]),
        max_source_age_ms=float(signal["maximum_source_age_ms"]),
        min_common_move_bp=float(signal["minimum_same_sign_spot_and_perpetual_move_bp"]),
        max_basis_change_bp=float(signal["maximum_absolute_basis_change_bp"]),
        max_benchmark_move_bp=float(
            signal["maximum_absolute_deribit_quote_and_index_path_move_bp"]
        ),
        min_pm_move=float(signal["minimum_polymarket_follow_move"]),
        max_pm_spread=float(signal["maximum_polymarket_direction_spread"]),
        min_pm_quote_depth=float(signal["minimum_polymarket_direction_top_depth"]),
        min_remaining_s=float(remaining[0]),
        max_remaining_s=float(remaining[1]),
        min_net_edge=float(execution["minimum_net_edge_per_share"]),
        min_direct_depth=float(execution["minimum_full_fill_shares"]),
        fee_rate=float(execution["taker_fee_rate"]),
    )
    if (
        list(map(float, execution["evaluation_ms"]))
        != list(replay.execution.EVALUATION_MS)
        or float(execution["minimum_full_fill_shares"])
        != replay.execution.TARGET_SHARES
        or replay.execution.TIME_SHIFT_MS != 5_000.0
        or "5000 ms" not in str(protocol["controls"]["time_shift"])
    ):
        raise ValueError("frozen execution constants differ from replay runtime")
    return config


def replay_bound_day(
    artifact: str | Path,
    sidecars: Mapping[str, tuple[Path, ...]],
    *,
    day: str,
    protocol: Mapping[str, Any],
    protocol_sha256: str,
    evaluator_sha256: str,
    raw_sidecar_identity_sha256: str,
    archive_manifests_sha256: str,
    archive_manifests: Mapping[str, Any],
    raw_sidecar_binding: Mapping[str, Any],
) -> dict[str, Any]:
    standard = Path(artifact)
    validation = archive.validate_standard_artifact(standard)
    manifest_path = standard / "manifest.json"
    binding_path = standard / archive_daily.ARTIFACT_BINDING
    if not binding_path.is_file():
        raise ValueError("strict artifact lacks raw input binding")
    artifact_binding = json.loads(binding_path.read_text(encoding="utf-8"))
    strict_manifest_sha256 = _sha256(manifest_path)
    mappings = list(archive.iter_market_mappings(standard / "market_registry.csv.gz"))
    outcomes = {
        str(row["market_id"]): str(row["winner"])
        for row in archive.iter_outcomes(standard / "market_outcomes.csv.gz")
    }
    strict_source = archive.iter_normalized_events(
        standard / "source_events.jsonl.gz",
        family="source",
    )
    spot_bbo = replay.iter_binance_spot_bbo_events(sidecars["bn_spot"])
    index_events = replay.iter_deribit_index_events(sidecars["deribit"])
    clob_events = archive.iter_normalized_events(
        standard / "clob_events.jsonl.gz",
        family="clob",
    )
    rows, counters = replay.replay_normalized(
        merge_source_events(strict_source, spot_bbo),
        index_events,
        clob_events,
        mappings,
        outcomes,
        detector_config=detector_config(protocol),
        include_controls=True,
    )
    return {
        "schema": DAY_SCHEMA,
        "complete": True,
        "day": _iso_day(day),
        "collector_region": str(protocol["collector_region"]),
        "protocol_sha256": protocol_sha256,
        "evaluator_sha256": evaluator_sha256,
        "raw_sidecar_identity_sha256": raw_sidecar_identity_sha256,
        "archive_manifests_sha256": archive_manifests_sha256,
        "strict_manifest_sha256": strict_manifest_sha256,
        "artifact_binding_sha256": _sha256(binding_path),
        "input_bindings": {
            "archive_manifests": dict(archive_manifests),
            "artifact_binding": artifact_binding,
            "raw_sidecar_binding": dict(raw_sidecar_binding),
        },
        "strict_run_id": validation["manifest"].get("run_id"),
        "detector_config": asdict(detector_config(protocol)),
        "counters": counters,
        "rows": rows,
    }


def _upload_evidence(
    s3: Any,
    bucket: str,
    day: str,
    root: Path,
    paths: Iterable[Path],
) -> list[dict[str, Any]]:
    rows = []
    for path in paths:
        relative = path.relative_to(root).as_posix()
        rows.append(archive_daily._upload_file(
            s3,
            bucket,
            f"{EVIDENCE_PREFIX}/{day}/{relative}",
            path,
        ))
    return rows


def _evidence_rows(root: Path, paths: Iterable[Path]) -> list[dict[str, Any]]:
    return [
        {
            "path": path.relative_to(root).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path in paths
    ]


def _validate_complete(
    payload: Mapping[str, Any],
    *,
    day: str,
    protocol_sha256: str,
    evaluator_sha256: str,
) -> list[dict[str, Any]]:
    if (
        payload.get("schema") != COMPLETE_SCHEMA
        or payload.get("status") != "complete"
        or payload.get("day") != _iso_day(day)
        or payload.get("protocol_sha256") != protocol_sha256
        or payload.get("evaluator_sha256") != evaluator_sha256
    ):
        raise ValueError("completed common-mode day identity drift")
    evidence = payload.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("completed common-mode evidence is missing")
    result: list[dict[str, Any]] = []
    for item in evidence:
        if not isinstance(item, Mapping):
            raise ValueError("completed common-mode evidence drift")
        relative = Path(str(item.get("path") or ""))
        if (
            relative.is_absolute()
            or not relative.parts
            or ".." in relative.parts
            or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256") or ""))
        ):
            raise ValueError("completed common-mode evidence drift")
        result.append(dict(item))
    if len({item["path"] for item in result}) != len(result):
        raise ValueError("completed common-mode evidence path is duplicated")
    return result


def _read_daily_pending(
    state: Path,
    *,
    day: str,
    protocol_sha256: str,
    evaluator_sha256: str,
) -> dict[str, Any] | None:
    path = state / DAILY_PENDING
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        payload.get("schema") != DAILY_PENDING_SCHEMA
        or payload.get("day") != _iso_day(day)
        or payload.get("protocol_sha256") != protocol_sha256
        or payload.get("evaluator_sha256") != evaluator_sha256
        or not re.fullmatch(r"[0-9a-f]{64}", str(payload.get("result_sha256") or ""))
        or not isinstance(payload.get("evidence"), list)
    ):
        raise ValueError("common-mode daily pending identity drift")
    return payload


def _write_or_validate_daily_pending(
    state: Path,
    payload: Mapping[str, Any],
) -> None:
    path = state / DAILY_PENDING
    if path.is_file():
        current = json.loads(path.read_text(encoding="utf-8"))
        if current != dict(payload):
            raise ValueError("common-mode daily pending evidence drift")
        return
    _atomic_json(path, payload)


def process_day(
    day: str,
    data_dir: str | Path,
    poly_dir: str | Path,
    work_dir: str | Path,
    state_dir: str | Path,
    *,
    s3: Any,
    bucket: str = BUCKET,
    protocol_freeze_path: str | Path = PROTOCOL_FREEZE,
    evaluator_freeze_path: str | Path = EVALUATOR_FREEZE,
    _crash_after_forward_update: bool = False,
) -> dict[str, Any]:
    protocol, protocol_sha256 = load_protocol_freeze(protocol_freeze_path)
    _evaluator, evaluator_sha256 = load_evaluator_freeze(
        protocol,
        protocol_sha256,
        evaluator_freeze_path,
    )
    iso_day = _iso_day(day)
    if iso_day < str(protocol["holdout_start"])[:10]:
        raise ValueError("day is outside the frozen post-cutoff sample")
    if day >= _utc_today():
        raise ValueError("UTC day is not closed")

    data_root = Path(data_dir)
    poly_root = Path(poly_dir)
    day_root = Path(work_dir) / day
    day_root.mkdir(parents=True, exist_ok=True)
    state = Path(state_dir)
    state.mkdir(parents=True, exist_ok=True)
    result_path = day_root / "result.json"
    complete_path = day_root / "complete.json"
    strict_copy = day_root / "strict-manifest.json"
    archive_snapshot = day_root / "archive-manifests.json"
    sidecar_binding = day_root / "raw-sidecar-binding.json"
    forward = _forward_module()
    pending = _read_daily_pending(
        state,
        day=day,
        protocol_sha256=protocol_sha256,
        evaluator_sha256=evaluator_sha256,
    )

    if complete_path.is_file():
        complete = json.loads(complete_path.read_text(encoding="utf-8"))
        evidence_rows = _validate_complete(
            complete,
            day=day,
            protocol_sha256=protocol_sha256,
            evaluator_sha256=evaluator_sha256,
        )
        evidence = [day_root / str(item["path"]) for item in evidence_rows]
        for item, path in zip(evidence_rows, evidence):
            if not path.is_file() or _sha256(path) != item["sha256"]:
                raise ValueError("completed common-mode evidence changed")
        if pending is not None and (
            pending["result_sha256"] != complete.get("result_sha256")
            or pending["evidence"] != evidence_rows
        ):
            raise ValueError("completed common-mode pending evidence drift")
        _upload_evidence(s3, bucket, day, day_root, [*evidence, complete_path])
        verdict = forward.update(
            result_path,
            state_dir,
            frozen=protocol,
            protocol_sha256=protocol_sha256,
            evaluator_sha256=evaluator_sha256,
        )
        if verdict != complete.get("pooled_verdict"):
            raise ValueError("completed common-mode verdict drift")
        (state / DAILY_PENDING).unlink(missing_ok=True)
        archive_daily._discard_local_artifact(day_root / "artifact", day_root)
        return complete

    rec_manifest = archive_daily._archive_manifest(s3, bucket, day, "rec")
    poly_manifest = archive_daily._archive_manifest(s3, bucket, day, "poly")
    archive_daily.validate_local_archive(rec_manifest, data_root, day, "rec")
    archive_daily.validate_local_archive(poly_manifest, poly_root, day, "poly")
    sidecars, raw_identity = bind_raw_sidecars(rec_manifest, data_root, day)
    archive_payload = {"rec": rec_manifest, "poly": poly_manifest}
    _atomic_json(archive_snapshot, archive_payload)
    archive_sha256 = _sha256(archive_snapshot)
    artifact = archive_daily._build_artifact(
        data_root,
        poly_root,
        day_root,
        day,
        archive_snapshot,
    )
    strict_manifest = artifact / "manifest.json"
    strict_manifest_sha256 = _sha256(strict_manifest)
    raw_binding_payload = {
        "schema": "common-mode-raw-sidecars-v1",
        "day": day,
        "protocol_sha256": protocol_sha256,
        "evaluator_sha256": evaluator_sha256,
        "archive_manifests_sha256": archive_sha256,
        "raw_sidecar_identity_sha256": raw_identity,
        "strict_manifest_sha256": strict_manifest_sha256,
        "files": _identity_rows(rec_manifest, day),
    }
    _atomic_json(sidecar_binding, raw_binding_payload)

    if result_path.is_file():
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        expected = {
            "day": iso_day,
            "protocol_sha256": protocol_sha256,
            "evaluator_sha256": evaluator_sha256,
            "raw_sidecar_identity_sha256": raw_identity,
            "archive_manifests_sha256": archive_sha256,
            "strict_manifest_sha256": strict_manifest_sha256,
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise ValueError("persisted common-mode day input drift")
    else:
        payload = replay_bound_day(
            artifact,
            sidecars,
            day=day,
            protocol=protocol,
            protocol_sha256=protocol_sha256,
            evaluator_sha256=evaluator_sha256,
            raw_sidecar_identity_sha256=raw_identity,
            archive_manifests_sha256=archive_sha256,
            archive_manifests=archive_payload,
            raw_sidecar_binding=raw_binding_payload,
        )
        persist_day_result(result_path, payload)
    shutil.copyfile(strict_manifest, strict_copy)
    evidence = [archive_snapshot, sidecar_binding, strict_copy, result_path]
    evidence_rows = _evidence_rows(day_root, evidence)
    _upload_evidence(s3, bucket, day, day_root, evidence)
    pending_payload = {
        "schema": DAILY_PENDING_SCHEMA,
        "day": iso_day,
        "protocol_sha256": protocol_sha256,
        "evaluator_sha256": evaluator_sha256,
        "result_sha256": _sha256(result_path),
        "evidence": evidence_rows,
    }
    _write_or_validate_daily_pending(state, pending_payload)
    verdict = forward.update(
        result_path,
        state_dir,
        frozen=protocol,
        protocol_sha256=protocol_sha256,
        evaluator_sha256=evaluator_sha256,
    )
    if _crash_after_forward_update:
        raise RuntimeError("simulated crash after common-mode forward update")
    complete = {
        "schema": COMPLETE_SCHEMA,
        "status": "complete",
        "day": iso_day,
        "protocol_sha256": protocol_sha256,
        "evaluator_sha256": evaluator_sha256,
        "result_sha256": _sha256(result_path),
        "pooled_verdict": verdict,
        "evidence": evidence_rows,
    }
    _atomic_json(complete_path, complete)
    _upload_evidence(s3, bucket, day, day_root, [complete_path])
    (state / DAILY_PENDING).unlink()
    archive_daily._discard_local_artifact(artifact, day_root)
    return complete


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day")
    parser.add_argument("--data", default="/home/ubuntu/rec/data")
    parser.add_argument("--poly", default="/home/ubuntu/rec/data_poly")
    parser.add_argument("--work", default="/home/ubuntu/rec/formal/common-mode-days")
    parser.add_argument("--state", default="/home/ubuntu/rec/formal/common-mode-state")
    parser.add_argument("--bucket", default=BUCKET)
    args = parser.parse_args()
    protocol, protocol_sha256 = load_protocol_freeze()
    _evaluator, evaluator_sha256 = load_evaluator_freeze(
        protocol,
        protocol_sha256,
    )
    forward = _forward_module()
    state = Path(args.state)
    state.mkdir(parents=True, exist_ok=True)
    if args.day is None:
        daily_pending_path = state / DAILY_PENDING
        journal_path = state / forward.JOURNAL
        if daily_pending_path.is_file():
            raw_pending = json.loads(daily_pending_path.read_text(encoding="utf-8"))
            pending_day = str(raw_pending.get("day") or "")
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", pending_day):
                raise ValueError("common-mode daily pending day is invalid")
            day = pending_day.replace("-", "")
            _read_daily_pending(
                state,
                day=day,
                protocol_sha256=protocol_sha256,
                evaluator_sha256=evaluator_sha256,
            )
        elif journal_path.is_file():
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            journal_day = str(journal.get("day") or "")
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", journal_day):
                raise ValueError("common-mode admission journal day is invalid")
            day = journal_day.replace("-", "")
        else:
            index, _rows, current = forward._load_state(
                state,
                frozen=protocol,
                protocol_sha256=protocol_sha256,
                evaluator_sha256=evaluator_sha256,
                now_ms=time.time() * 1_000,
            )
            if current is not None and current["status"] != "collecting":
                print(json.dumps({"status": "terminal", "verdict": current}, sort_keys=True))
                return
            indexed = [str(item["day"]) for item in index["days"]]
            if indexed:
                day = (
                    dt.date.fromisoformat(indexed[-1]) + dt.timedelta(days=1)
                ).strftime("%Y%m%d")
            else:
                day = str(protocol["holdout_start"])[:10].replace("-", "")
    else:
        day = args.day
    if day >= _utc_today():
        print(json.dumps({
            "status": "skipped",
            "day": day,
            "reason": "UTC_day_not_closed",
        }, sort_keys=True))
        return
    with (state / ".daily.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = process_day(
            day,
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
