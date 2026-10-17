"""Build bounded calibration caches and finalize the frozen settlement proxy."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
from array import array
from pathlib import Path
from typing import Any, Mapping, Sequence

import settlement_proxy as proxy


RAW_TO_NORMALIZED = {
    "bn_spot": "bn_spot",
    "coinbase": "coinbase",
    "kraken": "kraken",
    "bitstamp": "bitstamp",
    "okx": "okx",
    "bybit_spot": "bybit_spot",
    "deribit": proxy.DERIBIT_SOURCE,
}
CACHE_SOURCES = (*RAW_TO_NORMALIZED, "rtds_cl")
CACHE_SCHEMA = "settlement-proxy-calibration-cache-v2"
PROVENANCE_SCHEMA = "settlement-proxy-calibration-provenance-v2"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def load_input_manifest(path: str | Path, freeze: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    if payload.get("schema") != "settlement-proxy-calibration-input-v2":
        raise ValueError("unexpected calibration input manifest schema")
    if payload.get("calibration_end") != freeze.get("calibration_end"):
        raise ValueError("calibration input cutoff differs from the frozen protocol")
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("calibration input manifest has no files")
    names: set[str] = set()
    for item in files:
        if not (
            isinstance(item, Mapping)
            and item.get("gzip_ok") is True
            and item.get("uploaded_ok") is True
            and int(item.get("size") or 0) > 0
            and len(str(item.get("sha256") or "")) == 64
        ):
            raise ValueError("calibration input manifest contains an unverified file")
        name = str(item.get("file") or "")
        if name in names:
            raise ValueError("calibration input manifest contains duplicate files")
        names.add(name)
    return payload, _sha256(source)


def _source_items(manifest: Mapping[str, Any], raw_source: str) -> list[Mapping[str, Any]]:
    return [
        item for item in manifest["files"]
        if str(item["file"]).startswith(f"{raw_source}.")
    ]


def validate_source_files(root: Path, items: Sequence[Mapping[str, Any]]) -> None:
    if not items:
        raise ValueError("calibration source has no input files")
    for item in items:
        path = root / str(item["file"])
        if (
            not path.is_file()
            or path.stat().st_size != int(item["size"])
            or _sha256(path) != item["sha256"]
        ):
            raise ValueError(f"calibration input differs from manifest: {path.name}")


def _write_cache(
    path: Path,
    source: str,
    fields: Mapping[str, Sequence[float]],
    *,
    fingerprint: str,
    manifest_sha256: str,
    stats: Mapping[str, Any],
) -> None:
    lengths = {len(values) for values in fields.values()}
    if len(lengths) != 1 or not lengths or next(iter(lengths)) == 0:
        raise ValueError("calibration cache fields are empty or misaligned")
    count = next(iter(lengths))
    header = {
        "schema": CACHE_SCHEMA,
        "source": source,
        "strategy_fingerprint": fingerprint,
        "input_manifest_sha256": manifest_sha256,
        "byteorder": sys.byteorder,
        "double_itemsize": array("d").itemsize,
        "count": count,
        "fields": list(fields),
        "stats": dict(stats),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with gzip.open(temporary, "wb", compresslevel=6) as stream:
        stream.write(json.dumps(header, sort_keys=True, allow_nan=False).encode() + b"\n")
        for values in fields.values():
            stream.write(array("d", values).tobytes())
    temporary.replace(path)


def _read_cache(
    path: Path,
    source: str,
    fingerprint: str,
    manifest_sha256: str,
) -> tuple[dict[str, array], dict[str, Any]]:
    with gzip.open(path, "rb") as stream:
        header = json.loads(stream.readline())
        if (
            header.get("schema") != CACHE_SCHEMA
            or header.get("source") != source
            or header.get("strategy_fingerprint") != fingerprint
            or header.get("input_manifest_sha256") != manifest_sha256
            or header.get("byteorder") != sys.byteorder
            or header.get("double_itemsize") != array("d").itemsize
        ):
            raise ValueError(f"calibration cache identity drift: {source}")
        count = int(header.get("count") or 0)
        fields = header.get("fields")
        if count <= 0 or not isinstance(fields, list) or not fields:
            raise ValueError(f"invalid calibration cache header: {source}")
        output: dict[str, array] = {}
        field_bytes = count * array("d").itemsize
        for field in fields:
            raw = stream.read(field_bytes)
            if len(raw) != field_bytes:
                raise ValueError(f"truncated calibration cache: {source}")
            values = array("d")
            values.frombytes(raw)
            output[str(field)] = values
        if stream.read(1):
            raise ValueError(f"trailing bytes in calibration cache: {source}")
    return output, header


def build_cache(
    source: str,
    input_root: str | Path,
    input_manifest: str | Path,
    cache_dir: str | Path,
) -> dict[str, Any]:
    freeze = proxy.load_freeze()
    fingerprint = proxy.strategy_fingerprint(freeze)
    manifest, manifest_sha256 = load_input_manifest(input_manifest, freeze)
    root = Path(input_root)
    cache_path = Path(cache_dir) / f"{source}.cache.gz"
    if cache_path.is_file():
        fields, header = _read_cache(cache_path, source, fingerprint, manifest_sha256)
        return {"status": "cached", "source": source, "count": len(next(iter(fields.values()))),
                "sha256": _sha256(cache_path), "stats": header["stats"]}
    items = _source_items(manifest, source)
    validate_source_files(root, items)
    config = proxy.ProxyConfig(**freeze["proxy_config"])
    if source == "rtds_cl":
        state = proxy.FrameState()
        points: list[proxy.ChainlinkPoint] = []
        paths = [root / str(item["file"]) for item in items]
        for receive_ms, text in proxy._iter_recorder_lines(paths):
            _, rows = proxy.parse_raw_frame("rtds_cl", receive_ms, text, state)
            points.extend(rows)
        points = proxy._normalised_chainlink(points)
        fields = {
            "payload_ms": [point.payload_ms for point in points],
            "push_ms": [point.push_ms for point in points],
            "receive_ms": [point.receive_ms for point in points],
            "value": [point.value for point in points],
        }
        stats = {"points": len(points), "input_files": len(items)}
    else:
        series, raw_count = proxy._load_source(root, source, config)
        fields = {"receive_ms": series.receives, "value": series.values}
        stats = {"raw_points": raw_count, "downsampled_points": len(series),
                 "input_files": len(items)}
    _write_cache(
        cache_path, source, fields, fingerprint=fingerprint,
        manifest_sha256=manifest_sha256, stats=stats,
    )
    return {"status": "built", "source": source, "count": len(next(iter(fields.values()))),
            "sha256": _sha256(cache_path), "stats": stats}


def finalize(
    input_manifest: str | Path,
    cache_dir: str | Path,
    calibration_pin: str | Path,
    provenance_path: str | Path,
) -> dict[str, Any]:
    freeze = proxy.load_freeze()
    fingerprint = proxy.strategy_fingerprint(freeze)
    manifest, manifest_sha256 = load_input_manifest(input_manifest, freeze)
    cache_root = Path(cache_dir)
    series: dict[str, proxy.StepSeries] = {}
    cache_hashes = {}
    for raw_source, normalized in RAW_TO_NORMALIZED.items():
        cache_path = cache_root / f"{raw_source}.cache.gz"
        fields, _ = _read_cache(cache_path, raw_source, fingerprint, manifest_sha256)
        series[normalized] = proxy.StepSeries(fields["receive_ms"], fields["value"])
        cache_hashes[raw_source] = _sha256(cache_path)
    chainlink_path = cache_root / "rtds_cl.cache.gz"
    fields, _ = _read_cache(chainlink_path, "rtds_cl", fingerprint, manifest_sha256)
    chainlink = [
        proxy.ChainlinkPoint(*values)
        for values in zip(
            fields["payload_ms"], fields["push_ms"], fields["receive_ms"], fields["value"]
        )
    ]
    cache_hashes["rtds_cl"] = _sha256(chainlink_path)
    config = proxy.ProxyConfig(**freeze["proxy_config"])
    calibration = proxy.load_or_pin_calibration(
        series,
        chainlink,
        proxy._iso_timestamp(freeze["calibration_end"]),
        config,
        calibration_pin,
        fingerprint,
    )
    provenance = {
        "schema": PROVENANCE_SCHEMA,
        "strategy_fingerprint": fingerprint,
        "input_manifest_sha256": manifest_sha256,
        "input_manifest": manifest,
        "cache_sha256": cache_hashes,
        "calibration_sha256": _sha256(Path(calibration_pin)),
    }
    proxy._atomic_json(provenance_path, provenance)
    return {
        "status": "complete",
        "strategy_fingerprint": fingerprint,
        "calibration_sha256": provenance["calibration_sha256"],
        "provenance_sha256": _sha256(Path(provenance_path)),
        "validation_windows": calibration.validation_windows,
        "residual_bound_bp": calibration.residual_bound_bp,
        "residual_quantile_bp": calibration.residual_quantile_bp,
        "spot_validation_mae_bp": calibration.spot_validation_mae_bp,
        "deribit_validation_mae_bp": calibration.deribit_validation_mae_bp,
        "combined_validation_mae_bp": calibration.combined_validation_mae_bp,
        "spot_query_offset_ms": calibration.spot_query_offset_ms,
        "deribit_query_offset_ms": calibration.deribit_query_offset_ms,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    cache = subparsers.add_parser("cache")
    cache.add_argument("--source", required=True, choices=CACHE_SOURCES)
    cache.add_argument("--input-root", required=True)
    cache.add_argument("--input-manifest", required=True)
    cache.add_argument("--cache-dir", required=True)
    final = subparsers.add_parser("finalize")
    final.add_argument("--input-manifest", required=True)
    final.add_argument("--cache-dir", required=True)
    final.add_argument("--calibration-pin", required=True)
    final.add_argument("--provenance", required=True)
    args = parser.parse_args()
    if args.command == "cache":
        result = build_cache(args.source, args.input_root, args.input_manifest, args.cache_dir)
    else:
        result = finalize(
            args.input_manifest, args.cache_dir, args.calibration_pin, args.provenance,
        )
    print(json.dumps(result, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
