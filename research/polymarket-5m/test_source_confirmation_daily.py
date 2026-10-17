import datetime as dt
import json
import shutil
from pathlib import Path

import eu_strict
import pytest
import source_confirmation_daily as daily
from botocore.exceptions import ClientError

FIRST_DAY = "20261012"
SECOND_DAY = "20261013"
THIRD_DAY = "20261014"
FREEZE_SHA256 = "f" * 64
FORMAL_CUTOFF_MS = 1_791_750_600_000.0


def _archive_manifest(day: str, group: str) -> dict:
    if group == "poly":
        names = [f"poly_clob.{day}T{hour:02d}.jsonl.gz" for hour in range(24)]
    else:
        routes = sorted(
            {
                route
                for candidates in eu_strict.SOURCE_ROUTES.values()
                for route in candidates
            }
        )
        names = [
            f"{route}.{day}T{hour:02d}.txt.gz" for route in routes for hour in range(24)
        ]
    return {
        "day": day,
        "group": group,
        "files": [
            {
                "file": name,
                "size": 10,
                "sha256": "a" * 64,
                "gzip_ok": True,
                "uploaded_ok": True,
            }
            for name in names
        ],
    }


def _day_bounds_ms(day: str) -> tuple[float, float]:
    start = dt.datetime.strptime(day, "%Y%m%d").replace(tzinfo=dt.UTC)
    start_ms = start.timestamp() * 1_000.0
    return start_ms + 1.0, start_ms + 86_400_000.0 - 1.0


def _write_strict_day(root: Path, day: str) -> Path:
    day_root = root / day
    artifact = day_root / "artifact"
    artifact.mkdir(parents=True)
    started_ms, ended_ms = _day_bounds_ms(day)
    manifest = {
        "schema": "polymarket-5m-strict-replay-v2",
        "complete": True,
        "receipt_race_ready": True,
        "run_id": f"eu-west-{day}",
        "collector_region": "eu-west-1",
        "started_ms": started_ms,
        "ended_ms": ended_ms,
    }
    manifest_path = artifact / "manifest.json"
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    for name in daily.archive_daily.STRICT_FILES:
        target = artifact / name
        if target != manifest_path:
            target.write_text("{}\n", encoding="utf-8")
    rec = _archive_manifest(day, "rec")
    poly = _archive_manifest(day, "poly")
    (day_root / "archive-manifests.json").write_text(
        json.dumps({"rec": rec, "poly": poly}) + "\n", encoding="utf-8"
    )
    binding = {
        "schema": "eu-strict-input-binding-v1",
        "day": day,
        "raw_archive_manifests_sha256": daily.strict_prebuild.raw_archive_identity(
            rec, poly
        ),
        "strict_manifest_sha256": daily.archive_daily._sha256(manifest_path),
    }
    (artifact / daily.archive_daily.ARTIFACT_BINDING).write_text(
        json.dumps(binding) + "\n", encoding="utf-8"
    )
    return artifact


class FakeS3:
    def __init__(self, *, fail_upload: bool = False):
        self.fail_upload = fail_upload
        self.objects: dict[str, bytes] = {}
        self.metadata: dict[str, dict[str, str]] = {}

    def head_object(self, *, Bucket, Key):
        del Bucket
        if Key not in self.objects:
            raise ClientError(
                {
                    "Error": {"Code": "NoSuchKey"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                "HeadObject",
            )
        return {
            "ContentLength": len(self.objects[Key]),
            "Metadata": self.metadata[Key],
        }

    def put_object(self, *, Bucket, Key, Body, ContentLength, Metadata, IfNoneMatch):
        del Bucket
        assert IfNoneMatch == "*"
        if self.fail_upload:
            raise RuntimeError("S3 upload failed")
        if Key in self.objects:
            raise ClientError(
                {
                    "Error": {"Code": "PreconditionFailed"},
                    "ResponseMetadata": {"HTTPStatusCode": 412},
                },
                "PutObject",
            )
        payload = Body.read()
        assert len(payload) == ContentLength
        self.objects[Key] = payload
        self.metadata[Key] = dict(Metadata)


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    strict_root = tmp_path / "strict-days"
    work = tmp_path / "source-confirmation-days"
    state = tmp_path / "source-confirmation-state"
    terminal = {"enabled": False}

    monkeypatch.setattr(daily, "_utc_today", lambda: "20261015")
    monkeypatch.setattr(
        daily.archive,
        "validate_standard_artifact",
        lambda path: {
            "manifest": json.loads(
                (Path(path) / "manifest.json").read_text(encoding="utf-8")
            ),
            "counts": {},
        },
    )
    monkeypatch.setattr(
        daily.forward,
        "load_freeze",
        lambda _path=daily.forward.FREEZE_PATH: (
            {"cutoff_ms": FORMAL_CUTOFF_MS},
            FREEZE_SHA256,
        ),
    )

    def replay(artifact, *, rows_out, prospective_cutoff_ms):
        assert prospective_cutoff_ms == FORMAL_CUTOFF_MS
        manifest = json.loads(
            (Path(artifact) / "manifest.json").read_text(encoding="utf-8")
        )
        day = str(manifest["run_id"]).removeprefix("eu-west-")
        Path(rows_out).write_text(
            json.dumps(
                {
                    "day": f"{day[:4]}-{day[4:6]}-{day[6:]}",
                    "run": day,
                    "winner": "Up",
                    "filled": False,
                    "filled_shares": 0.0,
                    "result_time_ms": None,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return {
            "dataset": {
                "run_id": manifest["run_id"],
                "collector_region": manifest["collector_region"],
                "started_ms": manifest["started_ms"],
                "ended_ms": manifest["ended_ms"],
                "receipt_race_ready": True,
                "pending_signal_market_outcomes": [],
                "paper_gate_eligible": True,
            },
            "record_count": 1,
        }

    monkeypatch.setattr(daily.source_confirmation, "replay_archive", replay)

    def accumulate(
        new_rows,
        frozen,
        freeze_sha256,
        *,
        ledger_path,
        status_path,
        verdict_path,
    ):
        del frozen
        assert freeze_sha256 == FREEZE_SHA256
        ledger = Path(ledger_path)
        existing = []
        if ledger.is_file():
            existing = [json.loads(line) for line in ledger.read_text().splitlines()]
        combined = [*existing, *new_rows]
        daily.source_confirmation._atomic_write_rows(ledger, combined)
        result = {
            "status": "passed" if terminal["enabled"] else "collecting",
            "freeze_sha256": freeze_sha256,
            "rows": len(combined),
        }
        daily.archive_daily._atomic_json(Path(status_path), result)
        if terminal["enabled"]:
            daily.archive_daily._atomic_json(Path(verdict_path), result)
        return result

    monkeypatch.setattr(daily.forward, "accumulate", accumulate)
    return {
        "strict": strict_root,
        "work": work,
        "state": state,
        "terminal": terminal,
    }


def _process(harness, day: str, s3: FakeS3):
    return daily.process_day(
        day,
        harness["strict"],
        harness["work"],
        harness["state"],
        s3=s3,
        bucket="test-bucket",
    )


def _state_hashes(state: Path) -> dict[str, str | None]:
    return {
        name: daily.archive_daily._sha256(state / name)
        if (state / name).is_file()
        else None
        for name in daily.STATE_FILES
    }


def test_first_and_consecutive_closed_days_are_admitted(harness):
    _write_strict_day(harness["strict"], FIRST_DAY)
    _write_strict_day(harness["strict"], SECOND_DAY)
    s3 = FakeS3()

    first = _process(harness, FIRST_DAY, s3)
    second = _process(harness, SECOND_DAY, s3)

    assert first["status"] == second["status"] == "complete"
    admission = json.loads(
        (harness["state"] / daily.ADMISSION).read_text(encoding="utf-8")
    )
    assert admission["day"] == SECOND_DAY
    observations = (harness["state"] / daily.OBSERVATIONS).read_text().splitlines()
    assert [json.loads(line)["run"] for line in observations] == [FIRST_DAY, SECOND_DAY]
    assert any(key.endswith(f"/{FIRST_DAY}/complete.json") for key in s3.objects)
    assert any(key.endswith(f"/{SECOND_DAY}/complete.json") for key in s3.objects)


def test_repeating_an_admitted_day_does_not_append_again(harness):
    _write_strict_day(harness["strict"], FIRST_DAY)
    s3 = FakeS3()

    first = _process(harness, FIRST_DAY, s3)
    state_before = _state_hashes(harness["state"])
    object_count = len(s3.objects)
    second = _process(harness, FIRST_DAY, s3)

    assert second == first
    assert _state_hashes(harness["state"]) == state_before
    assert len(s3.objects) == object_count
    assert len((harness["state"] / daily.OBSERVATIONS).read_text().splitlines()) == 1


def test_admitted_private_input_drift_is_rejected(harness):
    _write_strict_day(harness["strict"], FIRST_DAY)
    s3 = FakeS3()
    _process(harness, FIRST_DAY, s3)
    snapshot = harness["work"] / FIRST_DAY / "archive-manifests.json"
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    payload["rec"]["files"][0]["sha256"] = "b" * 64
    snapshot.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="archive.*identity|input.*drift"):
        _process(harness, FIRST_DAY, s3)


def test_admitted_state_tamper_is_rejected_before_next_day(harness):
    _write_strict_day(harness["strict"], FIRST_DAY)
    _write_strict_day(harness["strict"], SECOND_DAY)
    s3 = FakeS3()
    _process(harness, FIRST_DAY, s3)
    (harness["state"] / daily.OBSERVATIONS).write_text("tampered\n", encoding="utf-8")

    with pytest.raises(ValueError, match="state hash"):
        _process(harness, SECOND_DAY, s3)


def test_s3_upload_failure_never_commits_staged_state(harness):
    _write_strict_day(harness["strict"], FIRST_DAY)

    with pytest.raises(RuntimeError, match="S3 upload failed"):
        _process(harness, FIRST_DAY, FakeS3(fail_upload=True))

    assert not (harness["state"] / daily.ADMISSION).exists()
    assert all(not (harness["state"] / name).exists() for name in daily.STATE_FILES)
    staged = harness["work"] / FIRST_DAY / "state-next"
    assert (staged / daily.OBSERVATIONS).is_file()
    assert (harness["work"] / FIRST_DAY / "complete.json").is_file()


def test_private_input_survives_shared_gc_after_failed_upload(harness):
    _write_strict_day(harness["strict"], FIRST_DAY)
    with pytest.raises(RuntimeError, match="S3 upload failed"):
        _process(harness, FIRST_DAY, FakeS3(fail_upload=True))

    private = harness["work"] / FIRST_DAY / daily.PRIVATE_ARTIFACT
    assert private.is_dir()
    shutil.rmtree(harness["strict"] / FIRST_DAY)

    result = _process(harness, FIRST_DAY, FakeS3())

    assert result["status"] == "complete"
    assert (harness["state"] / daily.ADMISSION).is_file()


def test_partial_state_commit_recovers_from_uploaded_complete(
    harness, monkeypatch: pytest.MonkeyPatch
):
    _write_strict_day(harness["strict"], FIRST_DAY)
    s3 = FakeS3()
    original_commit = daily._commit_completed_day

    def interrupted_commit(complete, complete_path, state, day_root):
        del complete_path
        state.mkdir(parents=True, exist_ok=True)
        name = daily.OBSERVATIONS
        source = day_root / "state-next" / name
        shutil.copyfile(source, state / name)
        assert (
            daily.archive_daily._sha256(state / name)
            == complete["target_state_sha256"][name]
        )
        raise RuntimeError("commit interrupted")

    monkeypatch.setattr(daily, "_commit_completed_day", interrupted_commit)
    with pytest.raises(RuntimeError, match="commit interrupted"):
        _process(harness, FIRST_DAY, s3)
    assert not (harness["state"] / daily.ADMISSION).exists()
    assert (harness["state"] / daily.OBSERVATIONS).is_file()

    monkeypatch.setattr(daily, "_commit_completed_day", original_commit)
    result = _process(harness, FIRST_DAY, s3)

    assert result["status"] == "complete"
    admission = json.loads(
        (harness["state"] / daily.ADMISSION).read_text(encoding="utf-8")
    )
    assert admission["day"] == FIRST_DAY
    assert _state_hashes(harness["state"]) == admission["state_sha256"]


def test_terminal_verdict_prevents_any_new_day_replay(
    harness, monkeypatch: pytest.MonkeyPatch
):
    _write_strict_day(harness["strict"], FIRST_DAY)
    _write_strict_day(harness["strict"], SECOND_DAY)
    harness["terminal"]["enabled"] = True
    s3 = FakeS3()
    _process(harness, FIRST_DAY, s3)
    observations_before = daily.archive_daily._sha256(
        harness["state"] / daily.OBSERVATIONS
    )
    monkeypatch.setattr(
        daily.source_confirmation,
        "replay_archive",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("terminal state must not replay a new day")
        ),
    )

    result = _process(harness, SECOND_DAY, s3)

    assert result["status"] == "terminal"
    assert (
        daily.archive_daily._sha256(harness["state"] / daily.OBSERVATIONS)
        == observations_before
    )
    assert not (harness["work"] / SECOND_DAY).exists()


def test_terminal_verdict_ignores_untrusted_next_day_completion_journal(harness):
    _write_strict_day(harness["strict"], FIRST_DAY)
    _write_strict_day(harness["strict"], SECOND_DAY)
    harness["terminal"]["enabled"] = True
    s3 = FakeS3()
    _process(harness, FIRST_DAY, s3)
    next_day = harness["work"] / SECOND_DAY
    next_day.mkdir(parents=True)
    (next_day / "complete.json").write_text("{}\n", encoding="utf-8")

    result = _process(harness, SECOND_DAY, s3)

    assert result["status"] == "terminal"
    admission = json.loads(
        (harness["state"] / daily.ADMISSION).read_text(encoding="utf-8")
    )
    assert admission["day"] == FIRST_DAY


def test_untrusted_next_day_journal_cannot_hide_admitted_state_tamper(harness):
    _write_strict_day(harness["strict"], FIRST_DAY)
    _write_strict_day(harness["strict"], SECOND_DAY)
    harness["terminal"]["enabled"] = True
    s3 = FakeS3()
    _process(harness, FIRST_DAY, s3)
    next_day = harness["work"] / SECOND_DAY
    next_day.mkdir(parents=True)
    (next_day / "complete.json").write_text("{}\n", encoding="utf-8")
    (harness["state"] / daily.OBSERVATIONS).write_text("tampered\n", encoding="utf-8")

    with pytest.raises(ValueError, match="state hash"):
        _process(harness, SECOND_DAY, s3)


def test_non_contiguous_day_is_rejected_before_input_read(harness):
    _write_strict_day(harness["strict"], FIRST_DAY)
    s3 = FakeS3()
    _process(harness, FIRST_DAY, s3)

    with pytest.raises(ValueError, match=f"expected {SECOND_DAY}"):
        _process(harness, THIRD_DAY, s3)


def test_open_utc_day_is_rejected(harness, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(daily, "_utc_today", lambda: FIRST_DAY)

    with pytest.raises(ValueError, match="closed UTC day"):
        _process(harness, FIRST_DAY, FakeS3())


def test_state_without_admission_is_rejected(harness):
    harness["state"].mkdir(parents=True)
    (harness["state"] / daily.OBSERVATIONS).write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="without.*admission"):
        _process(harness, FIRST_DAY, FakeS3())


def test_cutoff_before_final_freeze_publication_is_rejected(
    harness, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        daily.forward,
        "load_freeze",
        lambda _path=daily.forward.FREEZE_PATH: (
            {"cutoff_ms": FORMAL_CUTOFF_MS - 1.0},
            FREEZE_SHA256,
        ),
    )

    with pytest.raises(ValueError, match="formal cutoff drift"):
        _process(harness, FIRST_DAY, FakeS3())


def test_unsettled_fill_cannot_enter_staging_ledger(
    harness, monkeypatch: pytest.MonkeyPatch
):
    artifact = _write_strict_day(harness["strict"], FIRST_DAY)
    manifest = json.loads((artifact / "manifest.json").read_text(encoding="utf-8"))

    def unsettled_replay(_artifact, *, rows_out, prospective_cutoff_ms):
        assert prospective_cutoff_ms == FORMAL_CUTOFF_MS
        Path(rows_out).write_text(
            json.dumps(
                {
                    "filled": True,
                    "filled_shares": 5.0,
                    "winner": None,
                    "result_time_ms": None,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return {
            "dataset": {
                "run_id": manifest["run_id"],
                "collector_region": "eu-west-1",
                "started_ms": manifest["started_ms"],
                "ended_ms": manifest["ended_ms"],
                "receipt_race_ready": True,
                "pending_signal_market_outcomes": [],
                "paper_gate_eligible": True,
            },
            "record_count": 1,
        }

    monkeypatch.setattr(daily.source_confirmation, "replay_archive", unsettled_replay)

    with pytest.raises(ValueError, match="unsettled fill"):
        _process(harness, FIRST_DAY, FakeS3())

    assert not (harness["state"] / daily.ADMISSION).exists()
    assert not (harness["work"] / FIRST_DAY / "state-next").exists()


@pytest.mark.parametrize(
    ("paper_gate", "pending"),
    [(False, []), (True, ["market-1"])],
)
def test_replay_summary_must_be_formally_eligible_and_fully_settled(
    harness,
    monkeypatch: pytest.MonkeyPatch,
    paper_gate: bool,
    pending: list[str],
):
    artifact = _write_strict_day(harness["strict"], FIRST_DAY)
    manifest = json.loads((artifact / "manifest.json").read_text(encoding="utf-8"))

    def ineligible_replay(_artifact, *, rows_out, prospective_cutoff_ms):
        assert prospective_cutoff_ms == FORMAL_CUTOFF_MS
        Path(rows_out).write_text("", encoding="utf-8")
        return {
            "dataset": {
                "run_id": manifest["run_id"],
                "collector_region": "eu-west-1",
                "started_ms": manifest["started_ms"],
                "ended_ms": manifest["ended_ms"],
                "receipt_race_ready": True,
                "pending_signal_market_outcomes": pending,
                "paper_gate_eligible": paper_gate,
            },
            "record_count": 0,
        }

    monkeypatch.setattr(daily.source_confirmation, "replay_archive", ineligible_replay)

    with pytest.raises(ValueError, match="not eligible for formal admission"):
        _process(harness, FIRST_DAY, FakeS3())

    assert not (harness["work"] / FIRST_DAY / "state-next").exists()


def test_settlement_receipt_after_market_data_end_is_admissible(
    harness, monkeypatch: pytest.MonkeyPatch
):
    artifact = _write_strict_day(harness["strict"], FIRST_DAY)
    manifest = json.loads((artifact / "manifest.json").read_text(encoding="utf-8"))

    def late_settlement_replay(_artifact, *, rows_out, prospective_cutoff_ms):
        assert prospective_cutoff_ms == FORMAL_CUTOFF_MS
        Path(rows_out).write_text(
            json.dumps(
                {
                    "filled": True,
                    "filled_shares": 5.0,
                    "winner": "Up",
                    "result_time_ms": manifest["ended_ms"] + 30_000.0,
                    "day": "2026-10-12",
                    "run": FIRST_DAY,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return {
            "dataset": {
                "run_id": manifest["run_id"],
                "collector_region": "eu-west-1",
                "started_ms": manifest["started_ms"],
                "ended_ms": manifest["ended_ms"],
                "receipt_race_ready": True,
                "pending_signal_market_outcomes": [],
                "paper_gate_eligible": True,
            },
            "record_count": 1,
        }

    monkeypatch.setattr(
        daily.source_confirmation, "replay_archive", late_settlement_replay
    )

    result = _process(harness, FIRST_DAY, FakeS3())

    assert result["status"] == "complete"
    assert (harness["state"] / daily.ADMISSION).is_file()
