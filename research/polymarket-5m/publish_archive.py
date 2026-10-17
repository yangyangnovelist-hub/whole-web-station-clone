"""Offline control plane for bounded, separate GitHub Actions archive parts.

This module never uploads files, reads credentials, extracts captures, or runs
economic replay. The composite action uploads each ZIP independently and only
publishes the small index after every expected upload succeeds.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re

from artifact_parts import BLOCK, DEFAULT_PART, digest_file, read_manifest, split_archive

MAX_PARTS = 16
PART_IDS = tuple(f"part_{i:02d}" for i in range(MAX_PARTS))


def valid_prefix(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", value):
        raise ValueError("unsafe or excessive artifact prefix")
    return value


def valid_path(value):
    if not str(value) or any(c in str(value) for c in "\r\n\x00"):
        raise ValueError("unsafe archive or parts path")
    return Path(value)


def retention_days(value):
    if not re.fullmatch(r"[1-9][0-9]?", str(value)) or not 1 <= int(value) <= 90:
        raise ValueError("retention must be an integer from 1 through 90")
    return int(value)


def github_outputs(values, destination=None):
    destination = destination if destination is not None else os.environ.get("GITHUB_OUTPUT")
    if destination:
        with open(destination, "a", encoding="utf-8") as stream:
            for key, value in values.items():
                if not re.fullmatch(r"[a-z0-9-]+", key) or any(c in str(value) for c in "\r\n"):
                    raise ValueError("unsafe output field")
                stream.write(f"{key}={value}\n")


def prepare(archive, prefix, parts_dir, retention=30, part_bytes=DEFAULT_PART):
    archive, parts_dir = valid_path(archive), valid_path(parts_dir)
    prefix = valid_prefix(prefix)
    retention = retention_days(retention)
    if type(part_bytes) is not int or not 1 <= part_bytes <= DEFAULT_PART:
        raise ValueError("publishing payload size cannot exceed 400 MiB")
    if archive.is_symlink() or not archive.is_file():
        raise ValueError("archive must be a sealed regular file")
    if (archive.stat().st_size + part_bytes - 1) // part_bytes > MAX_PARTS:
        raise ValueError("archive exceeds 16 separate artifacts; refusing truncation")
    manifest = split_archive(archive, parts_dir, part_bytes)
    return {
        "part-count": len(manifest["parts"]),
        "manifest-sha256": digest_file(parts_dir / "manifest.json"),
        "source-sha256": manifest["source_sha256"],
        "index-name": prefix + "-index",
        "retention-days": retention,
    }


def complete(parts_dir, prefix, expected_manifest_sha256, step_results):
    parts_dir = valid_path(parts_dir)
    prefix = valid_prefix(prefix)
    manifest = read_manifest(parts_dir / "manifest.json", expected_manifest_sha256)
    count = len(manifest["parts"])
    if count > MAX_PARTS:
        raise ValueError("too many publishing parts")
    if not isinstance(step_results, dict):
        raise ValueError("invalid upload step results")
    records = []
    seen_ids = set()
    for i, step_id in enumerate(PART_IDS):
        step = step_results.get(step_id)
        if not isinstance(step, dict):
            raise ValueError("missing upload step result: " + step_id)
        if i >= count:
            if step.get("outcome") != "skipped":
                raise ValueError("unexpected extra part upload: " + step_id)
            continue
        if step.get("outcome") != "success":
            raise ValueError("required part upload failed or skipped: " + step_id)
        outputs = step.get("outputs")
        artifact_id = outputs.get("artifact-id") if isinstance(outputs, dict) else None
        if not isinstance(artifact_id, str) or not re.fullmatch(r"[1-9][0-9]*", artifact_id):
            raise ValueError("missing or invalid artifact ID: " + step_id)
        if artifact_id in seen_ids:
            raise ValueError("duplicate uploaded artifact ID")
        seen_ids.add(artifact_id)
        p = manifest["parts"][i]
        # Detect changes between prepare and the index publication gate. An
        # upload action is responsible for the actual transmitted byte identity.
        part = parts_dir / p["name"]
        if part.is_symlink() or not part.is_file() or part.stat().st_size != p["zip_bytes"] or digest_file(part) != p["zip_sha256"]:
            raise ValueError("part changed after prepare: " + p["name"])
        records.append({"index": i, "name": p["name"],
                        "artifact_name": f"{prefix}-part-{i:05d}",
                        "artifact_id": artifact_id,
                        "zip_bytes": p["zip_bytes"], "zip_sha256": p["zip_sha256"]})
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if not re.fullmatch(r"[1-9][0-9]*", run_id):
        raise ValueError("a valid producer GITHUB_RUN_ID is required")
    index = {"schema": "research-archive-publication-v1", "status": "all_parts_uploaded",
             "run_id": run_id, "prefix": prefix,
             "manifest_sha256": expected_manifest_sha256,
             "source_sha256": manifest["source_sha256"],
             "source_bytes": manifest["source_bytes"], "parts": records,
             "admission": "transport_only_not_market_coverage_or_economic_validation"}
    with (parts_dir / "publish-index.json").open("x", encoding="utf-8") as stream:
        json.dump(index, stream, sort_keys=True, indent=2)
        stream.write("\n")
    return {"part-count": count, "manifest-sha256": expected_manifest_sha256,
            "source-sha256": manifest["source_sha256"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "complete"))
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            part_mib = os.environ.get("PART_MIB", "400")
            if not re.fullmatch(r"[1-9][0-9]{0,2}", part_mib) or not 1 <= int(part_mib) <= 400:
                raise ValueError("part-mib must be an integer from 1 through 400")
            result = prepare(os.environ["ARCHIVE_PATH"], os.environ["ARTIFACT_PREFIX"],
                             os.environ["PARTS_DIR"], os.environ["RETENTION_DAYS"], int(part_mib) * BLOCK)
        else:
            result = complete(os.environ["PARTS_DIR"], os.environ["ARTIFACT_PREFIX"],
                              os.environ["EXPECTED_MANIFEST_SHA256"],
                              json.loads(os.environ["UPLOAD_STEP_RESULTS"]))
        github_outputs(result)
        print(json.dumps({"status": "prepared" if args.command == "prepare" else "all_parts_uploaded",
                          **result}, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError) as error:
        # Do not dump environment variables, step-output dictionaries, or paths.
        print(json.dumps({"status": "blocked", "reason": str(error)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
