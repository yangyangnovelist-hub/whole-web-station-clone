"""Archive verified hourly research recordings to S3 before pruning local copies."""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import re
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

import boto3
from botocore.exceptions import ClientError


BUCKET = "sgk90-rec-archive-114469424813-euw1"
REGION = "eu-west-1"
LOG_PATH = Path("/home/ubuntu/rec/archive.log")
_STAMP = re.compile(r"\.(?P<day>\d{8})T(?P<hour>\d{2})\.")


@dataclass(frozen=True)
class Group:
    name: str
    root: Path
    suffix: str
    required_routes: tuple[str, ...] = ()
    complete_from: str | None = None

    @property
    def prefix(self) -> str:
        return self.name


GROUPS = {
    "rec": Group(
        "rec", Path("/home/ubuntu/rec/data"), "txt.gz",
        ("bn_spot_2", "bn_spot_3", "bn_spot_4", "bn_fut_pub_2", "bn_fut_pub_3",
         "bn_fut_trade", "bn_fut_trade_2", "deribit"),
        "20261010",
    ),
    "poly": Group(
        "poly", Path("/home/ubuntu/rec/data_poly"), "jsonl.gz",
        ("poly_clob",), "20261010",
    ),
    "mix": Group(
        "mix", Path("/home/ubuntu/rec/data_mix"), "txt.gz",
        ("bn_spot_agg", "bn_spot_agg_2", "bn_spot_agg_3"),
        "20261010",
    ),
}

FORMAL_CUTOFF_DAY = "20261010"
LANE_STATES = {
    "source": (
        Path("/home/ubuntu/rec/formal/source-race-state"), ("verdict.json",),
    ),
    "settlement": (
        Path("/home/ubuntu/rec/formal/settlement-proxy-state"), ("verdict.json",),
    ),
    "mix": (
        Path("/home/ubuntu/rec/formal/mix-state"),
        ("mix-control-verdict.json", "mix-r1-17f-verdict.json"),
    ),
}


def log(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def stamp(path: Path) -> tuple[str, str] | None:
    match = _STAMP.search(path.name)
    return (match.group("day"), match.group("hour")) if match else None


def files_for_day(group: Group, day: str, current_hour: str) -> list[Path]:
    output = []
    for path in sorted(group.root.glob(f"*.{day}T??.{group.suffix}")):
        parsed = stamp(path)
        if parsed is None or "".join(parsed) == current_hour:
            continue
        output.append(path)
    return output


def remote_head(s3: Any, key: str) -> Mapping[str, Any] | None:
    try:
        return s3.head_object(Bucket=BUCKET, Key=key)
    except ClientError as error:
        code = str(error.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise


def remote_ok(
    s3: Any,
    key: str,
    size: int,
    digest: str,
    *,
    require_valid_gzip: bool = False,
) -> bool:
    result = remote_head(s3, key)
    if result is None:
        return False
    matches = (
        result["ContentLength"] == size
        and result.get("Metadata", {}).get("sha256") == digest
    )
    if require_valid_gzip:
        matches = matches and result.get("Metadata", {}).get("gzip_ok") == "True"
    return matches


def gzip_ok(path: Path) -> bool:
    try:
        with gzip.open(path, "rb") as stream:
            for _ in iter(lambda: stream.read(1 << 20), b""):
                pass
    except (OSError, EOFError, zlib.error):
        return False
    return True


def complete_hour_coverage(group: Group, day: str, paths: Iterable[Path]) -> bool:
    if not group.required_routes or group.complete_from is None or day < group.complete_from:
        return True
    expected = {f"{hour:02d}" for hour in range(24)}
    names = [path.name for path in paths]
    for route in group.required_routes:
        hours = {
            match.group(1) for name in names
            if (match := re.fullmatch(
                rf"{re.escape(route)}\.{day}T(\d{{2}})\.{re.escape(group.suffix)}", name,
            ))
        }
        if hours != expected:
            return False
    return True


def read_lane_progress() -> dict[str, dict[str, Any]]:
    progress: dict[str, dict[str, Any]] = {}
    for name, (state, terminal_files) in LANE_STATES.items():
        admission_path = state / "admission.json"
        admitted_day: str | None = None
        if admission_path.is_file():
            admission = json.loads(admission_path.read_text(encoding="utf-8"))
            admitted_day = str(admission.get("day") or "")
            if (
                not re.fullmatch(r"\d{8}", admitted_day)
                or admitted_day < FORMAL_CUTOFF_DAY
            ):
                raise ValueError(f"{name} admission day drift")
        terminal = all((state / filename).is_file() for filename in terminal_files)
        if terminal and admitted_day is None:
            raise ValueError(f"{name} terminal state exists without admission")
        progress[name] = {"admitted_day": admitted_day, "terminal": terminal}
    return progress


def _shift_day(day: str, amount: int) -> str:
    value = dt.datetime.strptime(day, "%Y%m%d").date() + dt.timedelta(days=amount)
    return value.strftime("%Y%m%d")


def formal_prune_watermark(
    group_name: str, progress: Mapping[str, Mapping[str, Any]],
) -> str | None:
    requirements = {
        "rec": (("source", 0), ("settlement", -1), ("mix", 0)),
        "poly": (("source", 0), ("settlement", 0), ("mix", 0)),
        "mix": (("mix", -1),),
    }
    if group_name not in requirements:
        raise ValueError(f"unknown archive group for formal pruning: {group_name}")
    safe_days: list[str] = []
    for lane, offset in requirements[group_name]:
        row = progress.get(lane)
        if not isinstance(row, Mapping):
            raise ValueError(f"missing {lane} lane progress")
        if row.get("terminal") is True:
            continue
        admitted_day = row.get("admitted_day")
        if admitted_day is None:
            return None
        day = str(admitted_day)
        if not re.fullmatch(r"\d{8}", day):
            raise ValueError(f"invalid {lane} admission watermark")
        safe_days.append(_shift_day(day, offset))
    return min(safe_days) if safe_days else "99999999"


def _remote_manifest(s3: Any, key: str) -> bytes | None:
    try:
        return s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    except ClientError as error:
        code = str(error.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise


def upload_day(s3: Any, group: Group, day: str, current_hour: str) -> bool:
    paths = files_for_day(group, day, current_hour)
    manifest = []
    for path in paths:
        size = path.stat().st_size
        digest = sha256(path)
        valid_gzip = gzip_ok(path)
        key = f"{group.prefix}/{day}/{path.name}"
        manifest.append({
            "file": path.name,
            "size": size,
            "sha256": digest,
            "gzip_ok": valid_gzip,
            "uploaded_ok": False,
        })
        if not valid_gzip:
            log(f"INVALID GZIP {group.name}/{path.name}")
    if not complete_hour_coverage(group, day, paths):
        log(f"INCOMPLETE HOURS {group.name}/{day}")
        return False
    if any(not item["gzip_ok"] for item in manifest):
        return False

    for path, item in zip(paths, manifest):
        key = f"{group.prefix}/{day}/{path.name}"
        head = remote_head(s3, key)
        if head is not None and (
            head["ContentLength"] != item["size"]
            or head.get("Metadata", {}).get("sha256") != item["sha256"]
        ):
            raise ValueError(f"immutable S3 collision: {key}")
        if head is None or head.get("Metadata", {}).get("gzip_ok") != "True":
            s3.upload_file(
                str(path), BUCKET, key,
                ExtraArgs={"Metadata": {"sha256": item["sha256"], "gzip_ok": "True"}},
            )
        item["uploaded_ok"] = remote_ok(
            s3, key, int(item["size"]), str(item["sha256"]), require_valid_gzip=True,
        )
        if not item["uploaded_ok"]:
            log(f"UPLOAD FAILED {group.name}/{path.name}")
    body = json.dumps({
        "day": day,
        "bucket": BUCKET,
        "group": group.name,
        "files": manifest,
    }, indent=1).encode()
    failures = sum(not item["uploaded_ok"] for item in manifest)
    gzip_errors = sum(not item["gzip_ok"] for item in manifest)
    total = sum(int(item["size"]) for item in manifest)
    log(
        f"{group.name} day {day}: {len(manifest)} files, {total / 1e9:.2f} GB, "
        f"upload failures {failures}, gzip errors {gzip_errors}"
    )
    if failures or gzip_errors:
        return False
    manifest_key = f"{group.prefix}/{day}/MANIFEST.json"
    prior = _remote_manifest(s3, manifest_key)
    formal = group.complete_from is not None and day >= group.complete_from
    if prior is not None and prior != body and formal:
        raise ValueError(f"immutable S3 manifest collision: {manifest_key}")
    if prior != body:
        s3.put_object(Bucket=BUCKET, Key=manifest_key, Body=body)
    return True


def prune(
    s3: Any,
    group: Group,
    keep_days: int,
    today: dt.date,
    *,
    lane_progress: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[int, int]:
    cutoff = (today - dt.timedelta(days=keep_days)).strftime("%Y%m%d")
    watermark = (
        formal_prune_watermark(
            group.name, read_lane_progress() if lane_progress is None else lane_progress,
        )
        if group.complete_from is not None else None
    )
    freed = deleted = kept = pending = 0
    for path in sorted(group.root.glob(f"*.{group.suffix}")):
        parsed = stamp(path)
        if parsed is None or parsed[0] >= cutoff:
            continue
        if (
            group.complete_from is not None
            and parsed[0] >= group.complete_from
            and (watermark is None or parsed[0] > watermark)
        ):
            pending += 1
            continue
        size = path.stat().st_size
        key = f"{group.prefix}/{parsed[0]}/{path.name}"
        if remote_ok(s3, key, size, sha256(path), require_valid_gzip=True):
            path.unlink()
            freed += size
            deleted += 1
        else:
            kept += 1
            log(f"KEEP {group.name}/{path.name}: no verified copy in S3")
    log(
        f"{group.name} prune before {cutoff}: deleted {deleted} files "
        f"({freed / 1e9:.2f} GB), kept {kept} unverified, "
        f"kept {pending} pending formal admission"
    )
    return deleted, freed


def selected_groups(name: str) -> Iterable[Group]:
    return GROUPS.values() if name == "all" else (GROUPS[name],)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--day",
        default=(dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)).strftime("%Y%m%d"),
    )
    parser.add_argument("--keep-days", type=int, default=3)
    parser.add_argument("--group", choices=("all", *GROUPS), default="all")
    parser.add_argument("--no-delete", action="store_true")
    args = parser.parse_args()
    now = dt.datetime.now(dt.timezone.utc)
    current_hour = now.strftime("%Y%m%d%H")
    s3 = boto3.client("s3", region_name=REGION)
    ok = True
    for group in selected_groups(args.group):
        ok = upload_day(s3, group, args.day, current_hour) and ok
    if not args.no_delete:
        lane_progress = read_lane_progress()
        for group in selected_groups(args.group):
            prune(
                s3, group, args.keep_days, now.date(), lane_progress=lane_progress,
            )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
