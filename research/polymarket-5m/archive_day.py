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
from typing import Any, Iterable

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

    @property
    def prefix(self) -> str:
        return self.name


GROUPS = {
    "rec": Group("rec", Path("/home/ubuntu/rec/data"), "txt.gz"),
    "poly": Group("poly", Path("/home/ubuntu/rec/data_poly"), "jsonl.gz"),
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


def remote_ok(
    s3: Any,
    key: str,
    size: int,
    digest: str,
    *,
    require_valid_gzip: bool = False,
) -> bool:
    try:
        result = s3.head_object(Bucket=BUCKET, Key=key)
    except ClientError:
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


def upload_day(s3: Any, group: Group, day: str, current_hour: str) -> bool:
    manifest = []
    for path in files_for_day(group, day, current_hour):
        size = path.stat().st_size
        digest = sha256(path)
        valid_gzip = gzip_ok(path)
        key = f"{group.prefix}/{day}/{path.name}"
        if not remote_ok(s3, key, size, digest):
            s3.upload_file(
                str(path), BUCKET, key,
                ExtraArgs={"Metadata": {"sha256": digest, "gzip_ok": str(valid_gzip)}},
            )
        uploaded = remote_ok(s3, key, size, digest)
        manifest.append({
            "file": path.name,
            "size": size,
            "sha256": digest,
            "gzip_ok": valid_gzip,
            "uploaded_ok": uploaded,
        })
        if not uploaded:
            log(f"UPLOAD FAILED {group.name}/{path.name}")
    body = json.dumps({
        "day": day,
        "bucket": BUCKET,
        "group": group.name,
        "files": manifest,
    }, indent=1).encode()
    s3.put_object(Bucket=BUCKET, Key=f"{group.prefix}/{day}/MANIFEST.json", Body=body)
    failures = sum(not item["uploaded_ok"] for item in manifest)
    gzip_errors = sum(not item["gzip_ok"] for item in manifest)
    total = sum(int(item["size"]) for item in manifest)
    log(
        f"{group.name} day {day}: {len(manifest)} files, {total / 1e9:.2f} GB, "
        f"upload failures {failures}, gzip errors {gzip_errors}"
    )
    return failures == 0


def prune(s3: Any, group: Group, keep_days: int, today: dt.date) -> tuple[int, int]:
    cutoff = (today - dt.timedelta(days=keep_days)).strftime("%Y%m%d")
    freed = deleted = kept = 0
    for path in sorted(group.root.glob(f"*.{group.suffix}")):
        parsed = stamp(path)
        if parsed is None or parsed[0] >= cutoff:
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
        f"({freed / 1e9:.2f} GB), kept {kept} unverified"
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
        for group in selected_groups(args.group):
            prune(s3, group, args.keep_days, now.date())
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
