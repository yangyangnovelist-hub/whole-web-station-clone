from __future__ import annotations

import json

import pytest

import settlement_proxy as proxy
import settlement_proxy_calibrate as calibrate


def test_binary_cache_round_trip_and_identity_binding(tmp_path):
    path = tmp_path / "source.cache.gz"
    calibrate._write_cache(
        path,
        "bn_spot",
        {"receive_ms": [1.0, 2.0], "value": [100.0, 101.0]},
        fingerprint="frozen",
        manifest_sha256="manifest",
        stats={"raw_points": 2},
    )

    fields, header = calibrate._read_cache(path, "bn_spot", "frozen", "manifest")

    assert list(fields["receive_ms"]) == [1.0, 2.0]
    assert list(fields["value"]) == [100.0, 101.0]
    assert header["stats"] == {"raw_points": 2}
    with pytest.raises(ValueError, match="identity drift"):
        calibrate._read_cache(path, "bn_spot", "another-freeze", "manifest")


def test_chainlink_cache_round_trip(tmp_path):
    path = tmp_path / "chainlink.cache.gz"
    calibrate._write_cache(
        path,
        "rtds_cl",
        {
            "payload_ms": [1.0],
            "push_ms": [2.0],
            "receive_ms": [3.0],
            "value": [100.0],
        },
        fingerprint="frozen",
        manifest_sha256="manifest",
        stats={"points": 1},
    )

    fields, _ = calibrate._read_cache(path, "rtds_cl", "frozen", "manifest")

    assert proxy.ChainlinkPoint(*[fields[key][0] for key in (
        "payload_ms", "push_ms", "receive_ms", "value",
    )]) == proxy.ChainlinkPoint(1.0, 2.0, 3.0, 100.0)


def test_input_manifest_is_bound_to_the_frozen_cutoff(tmp_path):
    freeze = proxy.load_freeze()
    path = tmp_path / "input.json"
    payload = {
        "schema": "settlement-proxy-calibration-input-v2",
        "calibration_end": freeze["calibration_end"],
        "files": [{
            "file": "bn_spot.20261007T00.txt.gz",
            "size": 1,
            "sha256": "a" * 64,
            "gzip_ok": True,
            "uploaded_ok": True,
        }],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded, digest = calibrate.load_input_manifest(path, freeze)

    assert loaded == payload
    assert len(digest) == 64
    payload["calibration_end"] = "2026-10-07T00:00:00Z"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="cutoff"):
        calibrate.load_input_manifest(path, freeze)
