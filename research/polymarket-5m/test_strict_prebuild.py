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


def _write_admission(state: Path, day: str) -> None:
    state.mkdir(parents=True, exist_ok=True)
    (state / "admission.json").write_text(
        json.dumps({"day": day}) + "\n", encoding="utf-8",
    )


def test_pending_day_tracks_the_slowest_nonterminal_lane(tmp_path):
    source = tmp_path / "source"
    settlement = tmp_path / "settlement"
    mix = tmp_path / "mix"
    _write_admission(source, "20261011")
    _write_admission(settlement, "20261010")
    _write_admission(mix, "20261012")
    lanes = {
        "source": prebuild.Lane(source, ("verdict.json",)),
        "settlement": prebuild.Lane(settlement, ("verdict.json",)),
        "mix": prebuild.Lane(mix, ("control-verdict.json", "projection-verdict.json")),
    }

    progress = prebuild.read_lane_progress(lanes, "20261010")

    assert prebuild.pending_day(progress) == "20261011"
    assert progress["settlement"]["next_day"] == "20261011"

    (settlement / "verdict.json").write_text("{}\n", encoding="utf-8")
    progress = prebuild.read_lane_progress(lanes, "20261010")
    assert prebuild.pending_day(progress) == "20261012"


def test_pending_day_starts_at_cutoff_and_stops_when_every_lane_terminal(tmp_path):
    lanes = {
        name: prebuild.Lane(tmp_path / name, ("verdict.json",))
        for name in ("source", "settlement", "mix")
    }

    progress = prebuild.read_lane_progress(lanes, "20261010")
    assert prebuild.pending_day(progress) == "20261010"

    for lane in lanes.values():
        _write_admission(lane.state, "20261012")
        (lane.state / "verdict.json").write_text("{}\n", encoding="utf-8")
    progress = prebuild.read_lane_progress(lanes, "20261010")
    assert prebuild.pending_day(progress) is None


def test_gc_waits_for_all_lanes_and_removes_only_admitted_artifacts(tmp_path):
    root = tmp_path / "strict"
    for day in ("20261010", "20261011", "20261012"):
        (root / day / "artifact").mkdir(parents=True)
        (root / day / "artifact.tmp").mkdir()
    progress = {
        "source": {"admitted_day": "20261012", "next_day": "20261013", "terminal": False},
        "settlement": {"admitted_day": "20261010", "next_day": "20261011", "terminal": False},
        "mix": {"admitted_day": "20261011", "next_day": "20261012", "terminal": False},
    }

    removed = prebuild.gc_admitted_artifacts(root, progress)

    assert removed == ["20261010"]
    assert not (root / "20261010" / "artifact").exists()
    assert not (root / "20261010" / "artifact.tmp").exists()
    assert (root / "20261011" / "artifact").is_dir()
    assert (root / "20261012" / "artifact").is_dir()


def test_default_lanes_wait_for_absorption_admission() -> None:
    absorption = prebuild.DEFAULT_LANES["absorption"]

    assert absorption.state == Path(
        "/home/ubuntu/rec/formal/absorption-state"
    )
    assert absorption.terminal_files == ("verdict.json",)
