import json
from pathlib import Path

import pytest

import strict_prebuild as prebuild


DAY = "20261008"


def _manifest(group):
    return {"day": DAY, "group": group, "files": [{
        "file": f"{group}.gz", "uploaded_ok": True, "gzip_ok": True,
        "size": 1, "sha256": "a" * 64,
    }]}


def _stub_archives(monkeypatch):
    monkeypatch.setattr(
        prebuild.daily, "_archive_manifest",
        lambda _s3, _bucket, _day, group: _manifest(group),
    )
    monkeypatch.setattr(prebuild.daily, "validate_local_archive", lambda *_args: None)


def test_source_phase_uses_persistent_staging(monkeypatch, tmp_path):
    _stub_archives(monkeypatch)
    seen = {}

    def prepare(data, out, day):
        seen.update(data=Path(data), out=Path(out), day=day)
        return {"schema": "source"}

    monkeypatch.setattr(prebuild.eu_strict, "prepare_source", prepare)
    monkeypatch.setattr(prebuild, "_valid_artifact", lambda *_args: False)

    result = prebuild.run_phase(
        DAY, "source", tmp_path / "data", tmp_path / "poly", tmp_path / "formal", s3=object(),
    )

    assert result["status"] == "source_ready"
    assert seen == {
        "data": tmp_path / "data",
        "out": tmp_path / "formal" / DAY / "artifact.tmp",
        "day": DAY,
    }
    snapshot = json.loads(
        (tmp_path / "formal" / DAY / "archive-manifests.json").read_text(encoding="utf-8")
    )
    assert snapshot == {"rec": _manifest("rec"), "poly": _manifest("poly")}


def test_finalize_promotes_one_immutable_shared_artifact(monkeypatch, tmp_path):
    _stub_archives(monkeypatch)
    monkeypatch.setattr(prebuild, "_valid_artifact", lambda *_args: False)
    root = tmp_path / "formal"
    staging = root / DAY / "artifact.tmp" / "strict"
    staging.mkdir(parents=True)

    def finalize(out, day):
        assert Path(out) == root / DAY / "artifact.tmp"
        (staging / "manifest.json").write_text(
            json.dumps({"complete": True, "run_id": f"eu-west-{day}"}), encoding="utf-8",
        )
        return {"complete": True, "failure_reasons": []}

    monkeypatch.setattr(prebuild.eu_strict, "finalize", finalize)
    monkeypatch.setattr(
        prebuild.archive, "validate_standard_artifact",
        lambda _path: {"manifest": {"run_id": f"eu-west-{DAY}"}, "counts": {}},
    )

    result = prebuild.run_phase(
        DAY, "finalize", tmp_path / "data", tmp_path / "poly", root, s3=object(),
    )

    artifact = root / DAY / "artifact"
    assert result == {"status": "complete", "day": DAY, "artifact": str(artifact)}
    binding = json.loads((artifact / prebuild.daily.ARTIFACT_BINDING).read_text(encoding="utf-8"))
    assert binding["schema"] == "eu-strict-input-binding-v1"
    assert binding["raw_archive_manifests_sha256"] == prebuild.raw_archive_identity(
        _manifest("rec"), _manifest("poly"),
    )
    assert not (root / DAY / "artifact.tmp").exists()


def test_invalid_existing_shared_artifact_fails_closed(monkeypatch, tmp_path):
    _stub_archives(monkeypatch)
    artifact = tmp_path / "formal" / DAY / "artifact"
    artifact.mkdir(parents=True)
    monkeypatch.setattr(prebuild, "_valid_artifact", lambda *_args: False)

    with pytest.raises(ValueError, match="invalid immutable"):
        prebuild.run_phase(
            DAY, "source", tmp_path / "data", tmp_path / "poly", tmp_path / "formal", s3=object(),
        )
