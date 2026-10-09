import json
from pathlib import Path

import pytest

import eu_strict
import source_race_daily as daily


DAY = "20261010"


@pytest.fixture(autouse=True)
def closed_utc_clock(monkeypatch):
    monkeypatch.setattr(daily, "_utc_today", lambda: "20261011")


def archived_manifest(group):
    if group == "poly":
        names = [f"poly_clob.{DAY}T{hour:02d}.jsonl.gz" for hour in range(24)]
    else:
        names = [
            f"{route}.{DAY}T{hour:02d}.txt.gz"
            for route in sorted({route for routes in eu_strict.SOURCE_ROUTES.values() for route in routes})
            for hour in range(24)
        ]
    return {
        "day": DAY,
        "group": group,
        "files": [
            {"file": name, "size": 10, "sha256": "a" * 64,
             "gzip_ok": True, "uploaded_ok": True}
            for name in names
        ],
    }


class FakeBody:
    def __init__(self, value):
        self.value = value

    def read(self):
        return self.value


class FakeS3:
    def __init__(self):
        self.objects = {
            f"rec/{DAY}/MANIFEST.json": json.dumps(archived_manifest("rec")).encode(),
            f"poly/{DAY}/MANIFEST.json": json.dumps(archived_manifest("poly")).encode(),
        }
        self.metadata = {}

    def get_object(self, *, Bucket, Key):
        return {"Body": FakeBody(self.objects[Key])}

    def upload_file(self, path, bucket, key, ExtraArgs):
        self.objects[key] = Path(path).read_bytes()
        self.metadata[key] = ExtraArgs["Metadata"]

    def head_object(self, *, Bucket, Key):
        value = self.objects[Key]
        return {
            "ContentLength": len(value),
            "Metadata": self.metadata.get(Key, {}),
        }


def test_archive_manifest_requires_every_frozen_route_hour():
    manifest = archived_manifest("rec")
    daily.validate_archive_manifest(manifest, DAY, "rec")
    manifest["files"].pop()

    with pytest.raises(ValueError, match="hour coverage"):
        daily.validate_archive_manifest(manifest, DAY, "rec")


def test_archive_manifest_rejects_unverified_gzip():
    manifest = archived_manifest("poly")
    manifest["files"][0]["gzip_ok"] = False

    with pytest.raises(ValueError, match="unverified archive file"):
        daily.validate_archive_manifest(manifest, DAY, "poly")


def test_local_archive_must_match_verified_manifest(tmp_path):
    path = tmp_path / f"poly_clob.{DAY}T00.jsonl.gz"
    path.write_bytes(b"expected")
    manifest = {
        "files": [{
            "file": path.name,
            "size": path.stat().st_size,
            "sha256": daily._sha256(path),
        }],
    }
    daily.validate_local_archive(manifest, tmp_path, DAY, "poly")
    path.write_bytes(b"changed!")

    with pytest.raises(ValueError, match="differs from verified S3 manifest"):
        daily.validate_local_archive(manifest, tmp_path, DAY, "poly")


def test_process_day_is_idempotent_and_updates_ledger_once(tmp_path, monkeypatch):
    calls = {"build": 0, "compare": 0, "update": 0}

    def fake_build(data, poly, out, day):
        calls["build"] += 1
        strict = Path(out) / "strict"
        strict.mkdir(parents=True)
        for name in daily.STRICT_FILES:
            (strict / name).write_text("{}\n", encoding="utf-8")
        manifest = {"complete": True, "receipt_race_ready": True, "run_id": f"eu-west-{day}"}
        (strict / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return manifest

    def fake_compare(archive_dir, freeze_path, rows_dir, source_race_freeze_path, require_formal):
        calls["compare"] += 1
        assert require_formal is True
        rows = Path(rows_dir)
        rows.mkdir(parents=True)
        (rows / "baseline.jsonl").write_text('{"arm":"baseline"}\n', encoding="utf-8")
        (rows / "candidate.jsonl").write_text('{"arm":"candidate"}\n', encoding="utf-8")
        return {"formal_sample_eligible": True, "artifact_identity": {"run_id": f"eu-west-{DAY}"}}

    def fake_update(baseline, candidate, state, *, frozen, freeze_sha256):
        calls["update"] += 1
        state = Path(state)
        state.mkdir(parents=True, exist_ok=True)
        (state / "status.json").write_text('{"status":"collecting"}\n', encoding="utf-8")
        return {"status": "collecting", "arms": {}}

    monkeypatch.setattr(daily.eu_strict, "build", fake_build)
    monkeypatch.setattr(daily, "validate_local_archive", lambda *args: None)
    def fake_validate(path):
        if not (Path(path) / "manifest.json").is_file():
            raise FileNotFoundError(path)
        return {"manifest": {"run_id": f"eu-west-{DAY}"}}

    monkeypatch.setattr(daily.archive, "validate_standard_artifact", fake_validate)
    monkeypatch.setattr(daily.race, "compare_archive", fake_compare)
    monkeypatch.setattr(daily.forward, "update", fake_update)
    s3 = FakeS3()
    kwargs = {
        "day": DAY,
        "data_dir": tmp_path / "data",
        "poly_dir": tmp_path / "poly",
        "work_dir": tmp_path / "work",
        "state_dir": tmp_path / "state",
        "s3": s3,
        "bucket": "bucket",
    }

    first = daily.process_day(**kwargs)
    second = daily.process_day(**kwargs)

    assert first == second
    assert calls == {"build": 1, "compare": 1, "update": 1}
    assert first["status"] == "complete"
    assert not (tmp_path / "work" / DAY / "artifact").exists()

    complete_path = tmp_path / "work" / DAY / "complete.json"
    changed = json.loads(complete_path.read_text(encoding="utf-8"))
    changed["forward_status"] = {"status": "tampered"}
    complete_path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="admitted daily complete hash"):
        daily.process_day(**kwargs)


def test_failed_comparison_never_enters_ledger_and_keeps_artifact(tmp_path, monkeypatch):
    def fake_build(data, poly, out, day):
        strict = Path(out) / "strict"
        strict.mkdir(parents=True)
        for name in daily.STRICT_FILES:
            (strict / name).write_text("{}\n", encoding="utf-8")
        return {"complete": True}

    updates = []
    monkeypatch.setattr(daily.eu_strict, "build", fake_build)
    monkeypatch.setattr(daily, "validate_local_archive", lambda *args: None)
    def fake_validate(path):
        if not (Path(path) / "manifest.json").is_file():
            raise FileNotFoundError(path)
        return {"manifest": {"run_id": f"eu-west-{DAY}"}}

    monkeypatch.setattr(daily.archive, "validate_standard_artifact", fake_validate)
    monkeypatch.setattr(daily.race, "compare_archive",
                        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("not formal")))
    monkeypatch.setattr(daily.forward, "update", lambda *args, **kwargs: updates.append(True))

    with pytest.raises(ValueError, match="not formal"):
        daily.process_day(
            DAY, tmp_path / "data", tmp_path / "poly", tmp_path / "work", tmp_path / "state",
            s3=FakeS3(), bucket="bucket",
        )

    assert updates == []
    assert (tmp_path / "work" / DAY / "artifact").is_dir()
    assert not (tmp_path / "work" / DAY / "complete.json").exists()


def test_failed_evidence_upload_does_not_admit_staged_state(tmp_path, monkeypatch):
    def fake_build(data, poly, out, day):
        strict = Path(out) / "strict"
        strict.mkdir(parents=True)
        for name in daily.STRICT_FILES:
            (strict / name).write_text("{}\n", encoding="utf-8")
        manifest = {"run_id": f"eu-west-{day}"}
        (strict / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def fake_validate(path):
        if not (Path(path) / "manifest.json").is_file():
            raise FileNotFoundError(path)
        return {"manifest": {"run_id": f"eu-west-{DAY}"}}

    def fake_compare(archive_dir, freeze_path, rows_dir, source_race_freeze_path, require_formal):
        rows = Path(rows_dir)
        rows.mkdir(parents=True)
        (rows / "baseline.jsonl").write_text('{}\n', encoding="utf-8")
        (rows / "candidate.jsonl").write_text('{}\n', encoding="utf-8")
        return {"formal_sample_eligible": True}

    def fake_update(baseline, candidate, state, *, frozen, freeze_sha256):
        state = Path(state)
        state.mkdir(parents=True, exist_ok=True)
        (state / daily.forward.STATUS).write_text('{"status":"collecting"}\n', encoding="utf-8")
        return {"status": "collecting"}

    class FailingS3(FakeS3):
        def upload_file(self, path, bucket, key, ExtraArgs):
            raise RuntimeError("upload failed")

    monkeypatch.setattr(daily.eu_strict, "build", fake_build)
    monkeypatch.setattr(daily.archive, "validate_standard_artifact", fake_validate)
    monkeypatch.setattr(daily.race, "compare_archive", fake_compare)
    monkeypatch.setattr(daily.forward, "update", fake_update)
    monkeypatch.setattr(daily, "validate_local_archive", lambda *args: None)
    state = tmp_path / "state"

    with pytest.raises(RuntimeError, match="upload failed"):
        daily.process_day(
            DAY, tmp_path / "data", tmp_path / "poly", tmp_path / "work", state,
            s3=FailingS3(), bucket="bucket",
        )

    assert not (state / daily.forward.STATUS).exists()
    assert not (state / daily.ADMISSION).exists()
    assert (tmp_path / "work" / DAY / "state-next" / daily.forward.STATUS).is_file()


def test_reused_comparison_must_match_artifact_and_both_freezes(tmp_path, monkeypatch):
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    rows = tmp_path / "rows"
    rows.mkdir()
    baseline = rows / "baseline.jsonl"
    candidate = rows / "candidate.jsonl"
    baseline.write_text("{}\n", encoding="utf-8")
    candidate.write_text("{}\n", encoding="utf-8")
    current = tmp_path / "current.json"
    source = tmp_path / "source.json"
    current.write_text('{"current":true}\n', encoding="utf-8")
    source.write_text('{"source":true}\n', encoding="utf-8")
    comparison = {
        "formal_sample_eligible": True,
        "freeze_sha256": daily._sha256(current),
        "source_race_freeze_sha256": "wrong",
        "artifact_identity": {"run_id": "eu-west-20261010"},
        "observation_files": {
            "baseline": {"sha256": daily._sha256(baseline)},
            "candidate": {"sha256": daily._sha256(candidate)},
        },
    }
    (tmp_path / "comparison.json").write_text(json.dumps(comparison), encoding="utf-8")
    monkeypatch.setattr(
        daily.race, "_capture_artifact_identity",
        lambda path: ({"run_id": "eu-west-20261010"}, {}, artifact),
    )

    with pytest.raises(ValueError, match="source-race freeze"):
        daily._load_or_compare(artifact, tmp_path, current, source)


def test_pre_cutoff_day_cannot_touch_archives_or_state(tmp_path):
    class UnexpectedS3:
        def get_object(self, **kwargs):
            raise AssertionError("pre-cutoff day must not read S3")

    with pytest.raises(ValueError, match="outside the frozen post-cutoff sample"):
        daily.process_day(
            "20261009", tmp_path / "data", tmp_path / "poly", tmp_path / "work",
            tmp_path / "state", s3=UnexpectedS3(), bucket="bucket",
        )

    assert not (tmp_path / "state").exists()


def test_cannot_skip_an_unadmitted_day(tmp_path, monkeypatch):
    monkeypatch.setattr(daily, "_utc_today", lambda: "20261012")
    class UnexpectedS3:
        def get_object(self, **kwargs):
            raise AssertionError("out-of-order day must not read S3")

    with pytest.raises(ValueError, match="next chronological admission: expected 20261010"):
        daily.process_day(
            "20261011", tmp_path / "data", tmp_path / "poly", tmp_path / "work",
            tmp_path / "state", s3=UnexpectedS3(), bucket="bucket",
        )


def test_current_utc_day_is_not_admissible(tmp_path):
    with pytest.raises(ValueError, match="not a closed UTC day"):
        daily.process_day(
            "20261011", tmp_path / "data", tmp_path / "poly", tmp_path / "work",
            tmp_path / "state", s3=FakeS3(), bucket="bucket",
        )


def test_partial_state_commit_recovers_from_completed_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(
        daily.race, "_load_source_race_freeze",
        lambda path=None: ({"cutoff": "2026-10-10T00:00:00Z"}, "freeze"),
    )
    monkeypatch.setattr(daily, "_utc_today", lambda: "20261012")
    state = tmp_path / "state"
    state.mkdir()
    old = {
        daily.forward.BASELINE_LEDGER: b"old baseline\n",
        daily.forward.CANDIDATE_LEDGER: b"old candidate\n",
        daily.forward.STATUS: b"old status\n",
    }
    for name, value in old.items():
        (state / name).write_bytes(value)
    base = daily._state_hashes(state)
    daily._atomic_json(state / daily.ADMISSION, {
        "schema": "current-h-source-race-admission-v1",
        "day": "20261010",
        "source_race_freeze_sha256": "freeze",
        "complete_sha256": "prior",
        "state_sha256": base,
    })
    day_dir = tmp_path / "work" / "20261011"
    staged = day_dir / "state-next"
    staged.mkdir(parents=True)
    new = {
        daily.forward.BASELINE_LEDGER: b"new baseline\n",
        daily.forward.CANDIDATE_LEDGER: b"new candidate\n",
        daily.forward.STATUS: b"new status\n",
    }
    for name, value in new.items():
        (staged / name).write_bytes(value)
    target = daily._state_hashes(staged)
    evidence = [
        {
            "path": path.relative_to(day_dir).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": daily._sha256(path),
        }
        for path in staged.iterdir()
    ]
    complete = {
        "schema": "current-h-source-race-day-v1",
        "status": "complete",
        "day": "20261011",
        "source_race_freeze_sha256": "freeze",
        "comparison_sha256": "unused",
        "forward_status": {"status": "collecting"},
        "base_state_sha256": base,
        "target_state_sha256": target,
        "evidence": evidence,
    }
    daily._atomic_json(day_dir / "complete.json", complete)
    (state / daily.forward.BASELINE_LEDGER).write_bytes(new[daily.forward.BASELINE_LEDGER])

    daily.process_day(
        "20261011", tmp_path / "data", tmp_path / "poly", tmp_path / "work", state,
        s3=FakeS3(), bucket="bucket",
    )

    assert daily._state_hashes(state) == target
    admission = json.loads((state / daily.ADMISSION).read_text(encoding="utf-8"))
    assert admission["day"] == "20261011"
