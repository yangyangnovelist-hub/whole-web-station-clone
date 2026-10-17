"""Build and admit one closed UTC day of frozen post-sweep absorption evidence."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import gzip
import hashlib
import json
import os
import re
import shutil
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import absorption as detector
import absorption_forward as forward
import absorption_run as execution
import boto3
import h_replay_archive as archive
import source_race_daily as archive_daily

ROOT = Path(__file__).resolve().parent
PROTOCOL_FREEZE = forward.PROTOCOL_FREEZE
EVALUATOR_FREEZE = forward.EVALUATOR_FREEZE
DAY_SCHEMA = forward.DAY_SCHEMA
COMPLETE_SCHEMA = "post-sweep-absorption-complete-day-v1"
ADMISSION = "admission.json"
ADMISSION_SCHEMA = "post-sweep-absorption-admission-v1"
BUCKET = archive_daily.BUCKET
EVIDENCE_PREFIX = "formal/post-sweep-absorption-v1"
STATE_ROOT_FILES = (forward.INDEX, forward.STATUS, forward.VERDICT)


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


def _selected_poly_items(
    manifest: Mapping[str, Any], day: str
) -> list[Mapping[str, Any]]:
    if manifest.get("day") != day or manifest.get("group") != "poly":
        raise ValueError("poly archive manifest identity mismatch")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise TypeError("poly archive manifest has no files")
    pattern = re.compile(rf"poly_clob\.{re.escape(day)}T(\d{{2}})\.jsonl\.gz")
    selected: dict[int, Mapping[str, Any]] = {}
    for item in files:
        if not isinstance(item, Mapping):
            continue
        match = pattern.fullmatch(str(item.get("file") or ""))
        if match is None:
            continue
        hour = int(match.group(1))
        if hour in selected:
            raise ValueError(f"poly archive contains duplicate hour {hour:02d}")
        if not (
            item.get("uploaded_ok") is True
            and item.get("gzip_ok") is True
            and int(item.get("size") or 0) > 0
            and re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256") or ""))
        ):
            raise ValueError("poly archive contains an unverified hour")
        selected[hour] = item
    if set(selected) != set(range(24)):
        raise ValueError(f"poly archive hour coverage is incomplete for {day}")
    return [selected[hour] for hour in range(24)]


def _poly_identity_rows(
    manifest: Mapping[str, Any], day: str
) -> list[dict[str, Any]]:
    return [
        {
            "file": str(item["file"]),
            "size": int(item["size"]),
            "sha256": str(item["sha256"]),
        }
        for item in _selected_poly_items(manifest, day)
    ]


def raw_poly_identity(manifest: Mapping[str, Any], day: str) -> str:
    return _canonical_sha256(
        {
            "schema": "post-sweep-absorption-raw-poly-v1",
            "day": day,
            "files": _poly_identity_rows(manifest, day),
        }
    )


def bind_poly_hours(
    manifest: Mapping[str, Any], root: str | Path, day: str
) -> tuple[tuple[Path, ...], str]:
    data_root = Path(root)
    paths = []
    for item in _selected_poly_items(manifest, day):
        path = data_root / str(item["file"])
        if (
            not path.is_file()
            or path.stat().st_size != int(item["size"])
            or _sha256(path) != item["sha256"]
        ):
            raise ValueError(
                f"local poly archive differs from verified S3 manifest: {item['file']}"
            )
        paths.append(path)
    return tuple(paths), raw_poly_identity(manifest, day)


def _matches_item(path: Path, item: Mapping[str, Any]) -> bool:
    return bool(
        path.is_file()
        and path.stat().st_size == int(item["size"])
        and _sha256(path) == item["sha256"]
    )


def materialize_poly_hours(
    s3: Any,
    bucket: str,
    manifest: Mapping[str, Any],
    local_root: str | Path,
    cache_root: str | Path,
    day: str,
) -> tuple[tuple[Path, ...], str]:
    """Use one verified local set, restoring the complete set from S3 when needed."""
    items = _selected_poly_items(manifest, day)
    local = Path(local_root)
    local_paths = tuple(local / str(item["file"]) for item in items)
    if all(_matches_item(path, item) for path, item in zip(local_paths, items)):
        return local_paths, raw_poly_identity(manifest, day)

    cache = Path(cache_root)
    cache.mkdir(parents=True, exist_ok=True)
    restored: list[Path] = []
    for item, local_path in zip(items, local_paths):
        destination = cache / str(item["file"])
        if _matches_item(destination, item):
            restored.append(destination)
            continue
        temporary = destination.with_name(destination.name + ".tmp")
        temporary.unlink(missing_ok=True)
        try:
            if _matches_item(local_path, item):
                shutil.copyfile(local_path, temporary)
            else:
                response = s3.get_object(
                    Bucket=bucket,
                    Key=f"poly/{day}/{item['file']}",
                )
                body = response["Body"]
                try:
                    with temporary.open("wb") as stream:
                        for block in iter(lambda source=body: source.read(1 << 20), b""):
                            stream.write(block)
                finally:
                    close = getattr(body, "close", None)
                    if callable(close):
                        close()
            if not _matches_item(temporary, item):
                raise ValueError(
                    f"restored poly archive failed verification: {item['file']}"
                )
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        restored.append(destination)
    return tuple(restored), raw_poly_identity(manifest, day)


def materialize_poly_carry_in(
    s3: Any,
    bucket: str,
    previous_manifest: Mapping[str, Any],
    local_root: str | Path,
    cache_root: str | Path,
    day: str,
) -> tuple[Path, dict[str, Any]]:
    """Materialize metadata for markets already active at the UTC boundary."""
    current_start = dt.datetime.strptime(day, "%Y%m%d").replace(
        tzinfo=dt.UTC
    ).timestamp()
    previous_day = (dt.datetime.strptime(day, "%Y%m%d").date() - dt.timedelta(days=1)).strftime(
        "%Y%m%d"
    )
    source_item = _selected_poly_items(previous_manifest, previous_day)[23]
    source_name = str(source_item["file"])
    local_source = Path(local_root) / source_name
    cache = Path(cache_root)
    cache.mkdir(parents=True, exist_ok=True)
    source = local_source
    if not _matches_item(source, source_item):
        source = cache / "source" / source_name
        source.parent.mkdir(parents=True, exist_ok=True)
        if not _matches_item(source, source_item):
            temporary = source.with_name(source.name + ".tmp")
            temporary.unlink(missing_ok=True)
            try:
                response = s3.get_object(
                    Bucket=bucket,
                    Key=f"poly/{previous_day}/{source_name}",
                )
                body = response["Body"]
                try:
                    with temporary.open("wb") as stream:
                        for block in iter(
                            lambda source_body=body: source_body.read(1 << 20), b""
                        ):
                            stream.write(block)
                finally:
                    close = getattr(body, "close", None)
                    if callable(close):
                        close()
                if not _matches_item(temporary, source_item):
                    raise ValueError(
                        f"restored poly carry-in failed verification: {source_name}"
                    )
                temporary.replace(source)
            finally:
                temporary.unlink(missing_ok=True)

    active: dict[str, tuple[int, dict[str, Any]]] = {}
    with gzip.open(source, "rt", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{source.name}:{line_number}: invalid carry-in JSON"
                ) from exc
            if not isinstance(row, Mapping) or not (
                row.get("c") == -1 or row.get("k") == "meta"
            ):
                continue
            metadata = row.get("m")
            if not isinstance(metadata, Mapping):
                continue
            match = re.fullmatch(
                r"btc-updown-5m-(\d+)", str(metadata.get("meta") or "")
            )
            if match is None:
                continue
            slot = int(match.group(1))
            if not slot <= current_start < slot + 300:
                continue
            try:
                receive_ns = int(row["rn"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"{source.name}:{line_number}: invalid carry-in receipt clock"
                ) from exc
            market_id = str(metadata.get("cid") or "")
            if not market_id:
                raise ValueError(
                    f"{source.name}:{line_number}: carry-in market identity is missing"
                )
            if market_id not in active or receive_ns > active[market_id][0]:
                active[market_id] = (receive_ns, dict(row))

    rows = [row for _receive_ns, row in sorted(active.values())]
    destination = cache / f"poly_clob.{day}T00.carry-in.jsonl.gz"
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.unlink(missing_ok=True)
    try:
        with temporary.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as stream:
                for row in rows:
                    stream.write(
                        (
                            json.dumps(
                                row,
                                sort_keys=True,
                                separators=(",", ":"),
                                allow_nan=False,
                            )
                            + "\n"
                        ).encode()
                    )
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    binding = {
        "schema": "post-sweep-absorption-poly-carry-in-v1",
        "day": day,
        "source_day": previous_day,
        "source_file": source_name,
        "source_size": int(source_item["size"]),
        "source_sha256": str(source_item["sha256"]),
        "active_market_ids": sorted(active),
        "metadata_rows": len(rows),
        "metadata_sha256": _canonical_sha256(rows),
        "carry_file": destination.name,
        "carry_sha256": _sha256(destination),
    }
    return destination, binding


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


def _iso_day(day: str) -> str:
    if not re.fullmatch(r"\d{8}", day):
        raise ValueError("day must be YYYYMMDD")
    return f"{day[:4]}-{day[4:6]}-{day[6:]}"


def _utc_today() -> str:
    return dt.datetime.now(dt.UTC).date().strftime("%Y%m%d")


def detector_config(protocol: Mapping[str, Any]) -> detector.AbsorptionConfig:
    signal = protocol["signal"]
    causality = protocol["causality"]
    execution_frozen = protocol["execution"]
    remaining = signal["remaining_seconds"]
    config = detector.AbsorptionConfig(
        max_interprint_ms=float(signal["maximum_interprint_ms"]),
        minimum_burst_shares=float(signal["minimum_burst_shares"]),
        depletion_deadline_ms=float(signal["depletion_deadline_ms"]),
        depletion_fraction=float(signal["minimum_depletion_fraction"]),
        refill_deadline_ms=float(signal["refill_deadline_ms"]),
        minimum_remaining_s=float(remaining[0]),
        maximum_remaining_s=float(remaining[1]),
        target_shares=float(execution_frozen["minimum_full_fill_shares"]),
        book_fresh_ms=float(causality["book_fresh_ms"]),
    )
    if (
        list(map(float, execution_frozen["evaluation_ms"]))
        != list(execution.EVALUATION_MS)
        or float(execution_frozen["minimum_full_fill_shares"])
        != execution.TARGET_SHARES
        or float(execution_frozen["taker_fee_rate"])
        != execution.DEFAULT_FEE_RATE
        or float(execution_frozen["unwind_wait_ms"]) != execution.EXIT_WAIT_MS
        or execution.TIME_SHIFT_MS != 5_000.0
    ):
        raise ValueError("frozen absorption execution differs from runtime")
    return config


def _day_end_ms(day: str) -> float:
    start = dt.datetime.strptime(day, "%Y%m%d").replace(tzinfo=dt.UTC)
    return (start + dt.timedelta(days=1)).timestamp() * 1_000


def replay_bound_day(
    artifact: str | Path,
    raw_paths: Iterable[Path],
    *,
    day: str,
    protocol: Mapping[str, Any],
    protocol_sha256: str,
    evaluator_sha256: str,
    raw_poly_identity_sha256: str,
    archive_manifests_sha256: str,
    archive_manifests: Mapping[str, Any],
    raw_poly_binding: Mapping[str, Any],
) -> dict[str, Any]:
    standard = Path(artifact)
    validation = archive.validate_standard_artifact(standard)
    manifest_path = standard / "manifest.json"
    binding_path = standard / archive_daily.ARTIFACT_BINDING
    if not binding_path.is_file():
        raise ValueError("strict artifact lacks raw input binding")
    artifact_binding = json.loads(binding_path.read_text(encoding="utf-8"))
    outcomes = {
        str(row["market_id"]): str(row["winner"])
        for row in archive.iter_outcomes(standard / "market_outcomes.csv.gz")
    }
    paths = tuple(Path(path) for path in raw_paths)
    end_ms = _day_end_ms(day)
    config = detector_config(protocol)
    machine = detector.AbsorptionDetector(config)
    signals: list[dict[str, Any]] = []
    event_counts: Counter[str] = Counter()
    for event in detector.iter_raw_clob_events(
        paths, end_ms, preserve_input_order=True
    ):
        event_counts[str(event.get("kind") or "unknown")] += 1
        signals.extend(machine.on_event(event))
    signals.extend(machine.flush(end_ms))
    rows = execution.replay_execution(
        detector.iter_raw_clob_events(paths, end_ms, preserve_input_order=True),
        signals,
        outcomes,
        evaluation_ms=execution.EVALUATION_MS,
        book_fresh_ms=config.book_fresh_ms,
        fee_rate=execution.DEFAULT_FEE_RATE,
        include_controls=True,
        recording_end_ms=end_ms,
    )
    signal_ids = {
        variant: sorted(
            {
                str(row["signal_id"])
                for row in signals
                if str(row["variant"]) == variant
            }
        )
        for variant in sorted({str(row["variant"]) for row in signals})
    }
    input_identity = {
        "archive_manifests_sha256": archive_manifests_sha256,
        "raw_poly_identity_sha256": raw_poly_identity_sha256,
        "strict_manifest_sha256": _sha256(manifest_path),
        "artifact_binding_sha256": _sha256(binding_path),
    }
    return {
        "schema": DAY_SCHEMA,
        "complete": True,
        "day": _iso_day(day),
        "collector_region": str(protocol["collector_region"]),
        "protocol_sha256": protocol_sha256,
        "evaluator_sha256": evaluator_sha256,
        "artifact_sha256": _canonical_sha256(input_identity),
        **input_identity,
        "input_bindings": {
            "archive_manifests": dict(archive_manifests),
            "artifact_binding": artifact_binding,
            "raw_poly_binding": dict(raw_poly_binding),
        },
        "strict_run_id": validation["manifest"].get("run_id"),
        "detector_config": config.__dict__,
        "counters": {
            "events": dict(event_counts),
            "signals": dict(Counter(str(row["variant"]) for row in signals)),
            "signal_ids": signal_ids,
            "detector_outcomes": dict(
                Counter(str(row["reason"]) for row in machine.outcomes)
            ),
            "execution_rows": len(rows),
        },
        "rows": rows,
    }


def _upload_evidence(
    s3: Any,
    bucket: str,
    day: str,
    root: Path,
    paths: Iterable[Path],
) -> list[dict[str, Any]]:
    return [
        archive_daily._upload_file(
            s3,
            bucket,
            f"{EVIDENCE_PREFIX}/{day}/{path.relative_to(root).as_posix()}",
            path,
        )
        for path in paths
    ]


def _evidence_rows(root: Path, paths: Iterable[Path]) -> list[dict[str, Any]]:
    return [
        {
            "path": path.relative_to(root).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path in paths
    ]


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
                and re.fullmatch(r"\d{4}-\d{2}-\d{2}\.json", path.parts[1])
            )
        )
    )


def _state_hashes(root: Path) -> dict[str, str]:
    return {
        relative.as_posix(): _sha256(root / relative)
        for relative in _state_paths(root)
    }


def _aligned_state_hashes(
    base_root: Path, target_root: Path
) -> tuple[dict[str, str | None], dict[str, str | None]]:
    base = _state_hashes(base_root)
    target = _state_hashes(target_root)
    if not set(base) <= set(target):
        raise ValueError("absorption forward update removed state files")
    names = sorted(set(base) | set(target))
    return (
        {name: base.get(name) for name in names},
        {name: target.get(name) for name in names},
    )


def _commit_completed_day(
    complete: Mapping[str, Any], complete_path: Path, state: Path, day_root: Path
) -> None:
    base = dict(complete["base_state_sha256"])
    target = dict(complete["target_state_sha256"])
    current = _state_hashes(state)
    if not set(current) <= set(target):
        raise ValueError("unexpected absorption state while committing")
    for name in target:
        if current.get(name) not in (base.get(name), target.get(name)):
            raise ValueError(f"absorption state changed while committing {name}")
    staged = day_root / "state-next"
    state.mkdir(parents=True, exist_ok=True)
    for name, digest in target.items():
        if digest is None:
            continue
        source = staged / name
        if not source.is_file() or _sha256(source) != digest:
            raise ValueError(f"staged absorption state changed: {name}")
        destination = state / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name("." + destination.name + ".tmp")
        shutil.copyfile(source, temporary)
        temporary.replace(destination)
    expected = {name: digest for name, digest in target.items() if digest is not None}
    if _state_hashes(state) != expected:
        raise ValueError("absorption state commit verification failed")
    _atomic_json(
        state / ADMISSION,
        {
            "schema": ADMISSION_SCHEMA,
            "day": complete["day"],
            "protocol_sha256": complete["protocol_sha256"],
            "evaluator_sha256": complete["evaluator_sha256"],
            "complete_sha256": _sha256(complete_path),
            "state_sha256": expected,
        },
    )


def _read_admission(
    state: Path,
    *,
    protocol_sha256: str,
    evaluator_sha256: str,
    validate_state: bool = True,
) -> dict[str, Any] | None:
    path = state / ADMISSION
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    state_sha256 = payload.get("state_sha256")
    if (
        payload.get("schema") != ADMISSION_SCHEMA
        or payload.get("protocol_sha256") != protocol_sha256
        or payload.get("evaluator_sha256") != evaluator_sha256
        or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(payload.get("day") or ""))
        or not re.fullmatch(
            r"[0-9a-f]{64}", str(payload.get("complete_sha256") or "")
        )
        or not isinstance(state_sha256, Mapping)
        or not state_sha256
        or any(not _valid_state_path(str(name)) for name in state_sha256)
        or any(
            not re.fullmatch(r"[0-9a-f]{64}", str(digest or ""))
            for digest in state_sha256.values()
        )
    ):
        raise ValueError("absorption admission identity drift")
    if validate_state and _state_hashes(state) != dict(state_sha256):
        raise ValueError("absorption admitted state hash mismatch")
    return payload


def _completed_day_is_indexed(
    state: Path, complete: Mapping[str, Any]
) -> bool:
    index_path = state / forward.INDEX
    if not index_path.is_file():
        return False
    index = json.loads(index_path.read_text(encoding="utf-8"))
    days = index.get("days")
    if not isinstance(days, list):
        return False
    return any(
        isinstance(item, Mapping)
        and item.get("day") == complete.get("day")
        and item.get("sha256") == complete.get("result_sha256")
        for item in days
    )


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
        raise ValueError("completed absorption day identity drift")
    evidence = payload.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("completed absorption evidence is missing")
    rows: list[dict[str, Any]] = []
    for item in evidence:
        if not isinstance(item, Mapping):
            raise TypeError("completed absorption evidence drift")
        relative = Path(str(item.get("path") or ""))
        if (
            relative.is_absolute()
            or not relative.parts
            or ".." in relative.parts
            or int(item.get("bytes") or -1) < 0
            or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256") or ""))
        ):
            raise ValueError("completed absorption evidence drift")
        rows.append(dict(item))
    if len({item["path"] for item in rows}) != len(rows):
        raise ValueError("completed absorption evidence path is duplicated")
    result_sha256 = str(payload.get("result_sha256") or "")
    result_rows = [item for item in rows if item["path"] == "result.json"]
    if (
        not re.fullmatch(r"[0-9a-f]{64}", result_sha256)
        or len(result_rows) != 1
        or result_rows[0]["sha256"] != result_sha256
    ):
        raise ValueError("completed absorption result hash drift")
    base = payload.get("base_state_sha256")
    target = payload.get("target_state_sha256")
    if (
        not isinstance(base, Mapping)
        or not isinstance(target, Mapping)
        or set(base) != set(target)
        or not target
        or any(not _valid_state_path(str(name)) for name in target)
    ):
        raise ValueError("completed absorption state set drift")
    for hashes in (base, target):
        if any(
            digest is not None
            and not re.fullmatch(r"[0-9a-f]{64}", str(digest))
            for digest in hashes.values()
        ):
            raise ValueError("completed absorption state hash drift")
    return rows


def process_day(
    day: str,
    data_dir: str | Path,
    poly_dir: str | Path,
    work_dir: str | Path,
    state_dir: str | Path,
    *,
    s3: Any,
    bucket: str = BUCKET,
    _crash_after_forward_update: bool = False,
) -> dict[str, Any]:
    protocol, protocol_sha256, _evaluator, evaluator_sha256 = forward.load_freezes()
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
    poly_binding = day_root / "raw-poly-binding.json"
    admission = _read_admission(
        state,
        protocol_sha256=protocol_sha256,
        evaluator_sha256=evaluator_sha256,
        validate_state=not complete_path.is_file(),
    )
    holdout_day = str(protocol["holdout_start"])[:10]
    if admission is None:
        if _state_hashes(state) and not complete_path.is_file():
            raise ValueError("absorption state exists without daily admission")
        expected_day = holdout_day
    else:
        admitted_day = str(admission["day"])
        if iso_day > admitted_day and (state / forward.VERDICT).is_file():
            return {
                "status": "terminal",
                "verdict": json.loads(
                    (state / forward.VERDICT).read_text(encoding="utf-8")
                ),
            }
        expected_day = (
            iso_day
            if complete_path.is_file() and iso_day <= admitted_day
            else (dt.date.fromisoformat(admitted_day) + dt.timedelta(days=1)).isoformat()
        )
    if iso_day != expected_day:
        raise ValueError(
            f"day is not the next absorption admission: expected {expected_day}"
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
            if (
                not path.is_file()
                or path.stat().st_size != int(item["bytes"])
                or _sha256(path) != item["sha256"]
            ):
                raise ValueError("completed absorption evidence changed")
        if admission is not None and iso_day <= str(admission["day"]):
            if _state_hashes(state) != dict(admission["state_sha256"]):
                raise ValueError("absorption admitted state hash mismatch")
            if iso_day == admission["day"]:
                if admission["complete_sha256"] != _sha256(complete_path):
                    raise ValueError("admitted absorption complete hash mismatch")
            elif not _completed_day_is_indexed(state, complete):
                raise ValueError("completed absorption day is not admitted")
            return complete
        _upload_evidence(s3, bucket, day, day_root, [*evidence, complete_path])
        _commit_completed_day(complete, complete_path, state, day_root)
        shutil.rmtree(day_root / "raw-poly", ignore_errors=True)
        archive_daily._discard_local_artifact(day_root / "artifact", day_root)
        return complete

    previous_day = (
        dt.datetime.strptime(day, "%Y%m%d").date() - dt.timedelta(days=1)
    ).strftime("%Y%m%d")
    rec_manifest = archive_daily._archive_manifest(s3, bucket, day, "rec")
    previous_poly_manifest = archive_daily._archive_manifest(
        s3, bucket, previous_day, "poly"
    )
    poly_manifest = archive_daily._archive_manifest(s3, bucket, day, "poly")
    current_raw_paths, current_raw_identity = materialize_poly_hours(
        s3,
        bucket,
        poly_manifest,
        poly_root,
        day_root / "raw-poly",
        day,
    )
    carry_path, carry_binding = materialize_poly_carry_in(
        s3,
        bucket,
        previous_poly_manifest,
        poly_root,
        day_root / "raw-poly-carry",
        day,
    )
    raw_paths = (carry_path, *current_raw_paths)
    raw_identity = _canonical_sha256(
        {
            "current_poly_identity_sha256": current_raw_identity,
            "carry_in": carry_binding,
        }
    )
    poly_input_root = current_raw_paths[0].parent
    previous_item = _selected_poly_items(previous_poly_manifest, previous_day)[23]
    archive_payload = {
        "rec": rec_manifest,
        "poly": poly_manifest,
        "poly_previous_boundary": {
            "day": previous_day,
            "files": [dict(previous_item)],
        },
    }
    _atomic_json(archive_snapshot, archive_payload)
    archive_sha256 = _sha256(archive_snapshot)
    if not os.environ.get("STRICT_SHARED_ROOT"):
        archive_daily.validate_local_archive(rec_manifest, data_root, day, "rec")
        archive_daily.validate_local_archive(
            poly_manifest, poly_input_root, day, "poly"
        )
    artifact = archive_daily._build_artifact(
        data_root,
        poly_input_root,
        day_root,
        day,
        archive_snapshot,
    )
    strict_manifest = artifact / "manifest.json"
    strict_manifest_sha256 = _sha256(strict_manifest)
    raw_binding_payload = {
        "schema": "post-sweep-absorption-raw-poly-v1",
        "day": day,
        "protocol_sha256": protocol_sha256,
        "evaluator_sha256": evaluator_sha256,
        "archive_manifests_sha256": archive_sha256,
        "raw_poly_identity_sha256": raw_identity,
        "current_poly_identity_sha256": current_raw_identity,
        "carry_in": carry_binding,
        "strict_manifest_sha256": strict_manifest_sha256,
        "files": _poly_identity_rows(poly_manifest, day),
    }
    _atomic_json(poly_binding, raw_binding_payload)

    if result_path.is_file():
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        forward.validate_day_result(
            payload,
            frozen=protocol,
            protocol_sha256=protocol_sha256,
            evaluator_sha256=evaluator_sha256,
            now_ms=_day_end_ms(day),
        )
        expected = {
            "day": iso_day,
            "protocol_sha256": protocol_sha256,
            "evaluator_sha256": evaluator_sha256,
            "raw_poly_identity_sha256": raw_identity,
            "archive_manifests_sha256": archive_sha256,
            "strict_manifest_sha256": strict_manifest_sha256,
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise ValueError("persisted absorption day input drift")
    else:
        payload = replay_bound_day(
            artifact,
            raw_paths,
            day=day,
            protocol=protocol,
            protocol_sha256=protocol_sha256,
            evaluator_sha256=evaluator_sha256,
            raw_poly_identity_sha256=raw_identity,
            archive_manifests_sha256=archive_sha256,
            archive_manifests=archive_payload,
            raw_poly_binding=raw_binding_payload,
        )
        forward.validate_day_result(
            payload,
            frozen=protocol,
            protocol_sha256=protocol_sha256,
            evaluator_sha256=evaluator_sha256,
            now_ms=_day_end_ms(day),
        )
        persist_day_result(result_path, payload)
    base_state_hashes = _state_hashes(state)
    staged_state = _stage_state(state, day_root / "state-next")
    verdict = forward.update(result_path, staged_state)
    if _crash_after_forward_update:
        raise RuntimeError("simulated crash after absorption forward update")
    shutil.copyfile(strict_manifest, strict_copy)
    state_paths = [staged_state / path for path in _state_paths(staged_state)]
    evidence = [
        archive_snapshot,
        poly_binding,
        carry_path,
        strict_copy,
        result_path,
        *state_paths,
    ]
    evidence_rows = _evidence_rows(day_root, evidence)
    aligned_base, target_state_hashes = _aligned_state_hashes(state, staged_state)
    if {
        name: digest for name, digest in aligned_base.items() if digest is not None
    } != base_state_hashes:
        raise ValueError("absorption base state changed while staging")
    complete = {
        "schema": COMPLETE_SCHEMA,
        "status": "complete",
        "day": iso_day,
        "protocol_sha256": protocol_sha256,
        "evaluator_sha256": evaluator_sha256,
        "result_sha256": _sha256(result_path),
        "pooled_verdict": verdict,
        "base_state_sha256": aligned_base,
        "target_state_sha256": target_state_hashes,
        "evidence": evidence_rows,
    }
    _validate_complete(
        complete,
        day=day,
        protocol_sha256=protocol_sha256,
        evaluator_sha256=evaluator_sha256,
    )
    _atomic_json(complete_path, complete)
    _upload_evidence(s3, bucket, day, day_root, [*evidence, complete_path])
    _commit_completed_day(complete, complete_path, state, day_root)
    shutil.rmtree(day_root / "raw-poly", ignore_errors=True)
    archive_daily._discard_local_artifact(artifact, day_root)
    return complete


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day")
    parser.add_argument("--data", default="/home/ubuntu/rec/data")
    parser.add_argument("--poly", default="/home/ubuntu/rec/data_poly")
    parser.add_argument("--work", default="/home/ubuntu/rec/formal/absorption-days")
    parser.add_argument("--state", default="/home/ubuntu/rec/formal/absorption-state")
    parser.add_argument("--bucket", default=BUCKET)
    args = parser.parse_args()
    protocol, protocol_sha256, _evaluator, evaluator_sha256 = forward.load_freezes()
    state = Path(args.state)
    state.mkdir(parents=True, exist_ok=True)
    admission = _read_admission(
        state,
        protocol_sha256=protocol_sha256,
        evaluator_sha256=evaluator_sha256,
    )
    if args.day is None:
        if admission is not None and (state / forward.VERDICT).is_file():
            verdict = json.loads((state / forward.VERDICT).read_text(encoding="utf-8"))
            print(json.dumps({"status": "terminal", "verdict": verdict}, sort_keys=True))
            return
        if admission is None:
            day = str(protocol["holdout_start"])[:10].replace("-", "")
        else:
            day = (
                dt.date.fromisoformat(str(admission["day"])) + dt.timedelta(days=1)
            ).strftime("%Y%m%d")
    else:
        day = args.day
    if day >= _utc_today():
        print(
            json.dumps(
                {"status": "skipped", "day": day, "reason": "UTC_day_not_closed"},
                sort_keys=True,
            )
        )
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
