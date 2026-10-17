"""Fail-fast runtime guard for the frozen MIX model."""
from __future__ import annotations

import importlib.metadata
import json
import platform
from collections.abc import Mapping
from pathlib import Path
from typing import Any


EXPECTED_PYTHON = "3.12.14"
EXPECTED_MIGRATION_RUNNER_SHA256 = (
    "783a0cdf348dbc58507483c5bce005d3c32fbd1e761e3c5bb5892eefe091a96a"
)
LOCK = Path(__file__).with_name("mix-runtime-requirements.txt")


def locked_packages(path: str | Path = LOCK) -> dict[str, str]:
    packages: dict[str, str] = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.count("==") != 1:
            raise ValueError(f"MIX runtime dependency is not exactly pinned: {line}")
        name, version = line.split("==", 1)
        if not name or not version or name in packages:
            raise ValueError(f"invalid MIX runtime dependency: {line}")
        packages[name] = version
    if not packages:
        raise ValueError("MIX runtime lock is empty")
    return packages


def verify_runtime(
    *,
    python_version: str | None = None,
    installed: Mapping[str, str] | None = None,
    runner_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    expected = locked_packages()
    actual_python = python_version or platform.python_version()
    actual = dict(installed) if installed is not None else {
        name: importlib.metadata.version(name) for name in expected
    }
    mismatches = {
        name: {"expected": version, "actual": actual.get(name)}
        for name, version in expected.items()
        if actual.get(name) != version
    }
    if actual_python != EXPECTED_PYTHON or mismatches:
        raise RuntimeError(json.dumps({
            "python": {"expected": EXPECTED_PYTHON, "actual": actual_python},
            "packages": mismatches,
        }, sort_keys=True))
    if runner_identity is None:
        import mix_hf

        runner_identity = mix_hf.migration_runner_identity()
    runner_sha256 = str(runner_identity.get("sha256") or "")
    if runner_sha256 != EXPECTED_MIGRATION_RUNNER_SHA256:
        raise RuntimeError(
            "MIX migration runner identity mismatch: "
            f"expected {EXPECTED_MIGRATION_RUNNER_SHA256}, got {runner_sha256}"
        )
    return {
        "status": "ok",
        "python": actual_python,
        "packages": actual,
        "migration_runner_sha256": runner_sha256,
    }


def verify_entrypoint() -> str:
    """Import the formal runner now so missing operational packages fail before replay."""
    import mix_daily

    return str(Path(mix_daily.__file__).resolve())


if __name__ == "__main__":
    result = verify_runtime()
    result["entrypoint"] = verify_entrypoint()
    print(json.dumps(result, sort_keys=True))
