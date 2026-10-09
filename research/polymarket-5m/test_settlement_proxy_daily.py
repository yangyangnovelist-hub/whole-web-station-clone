from __future__ import annotations

import json
from pathlib import Path

import pytest
import settlement_proxy as proxy
import settlement_proxy_daily as daily
import settlement_proxy_forward as forward

DAY = "20261010"
PREVIOUS = "20261009"
FOLLOWING = "20261011"


def _manifest(day: str, group: str = "rec") -> dict:
    return {"day": day, "group": group, "files": []}


def _fake_run_result(day: str) -> dict:
    variants = forward.expected_variants()
    slot = 1_791_590_400
    return {
        "protocol": {},
        "source_stats": {},
        "signals": {},
        "execution": {},
        "gate": {},
        "observed_outcome_audit": {
            "compared": 1,
            "missing_exact": 0,
            "unresolved_market_count": 0,
            "mismatch_count": 0,
            "passes": True,
        },
        "observations": {
            "signals": {variant: [] for variant in variants},
            "execution_rows": {variant: [] for variant in variants},
            "outcome_rows": [{
                "market_id": f"m-{day}",
                "slot": slot,
                "official": "Up",
                "derived": "Up",
                "status": "compared",
            }],
        },
    }


def _install_fakes(tmp_path, monkeypatch, calls):
    monkeypatch.setattr(daily, "_utc_today", lambda: "20261012")
    monkeypatch.setattr(daily, "_pin_identity", lambda *args: ("c" * 64, "p" * 64))
    freeze = proxy.load_freeze()
    monkeypatch.setattr(daily, "load_evaluator_freeze", lambda *args: ({
        "strategy_fingerprint": proxy.strategy_fingerprint(freeze),
        "holdout_start": freeze["holdout_start"],
        "calibration_sha256": "c" * 64,
        "provenance_sha256": "p" * 64,
    }, "e" * 64))
    monkeypatch.setattr(
        daily.archive_daily,
        "_archive_manifest",
        lambda _s3, _bucket, day, group: _manifest(day, group),
    )
    monkeypatch.setattr(daily.archive_daily, "validate_local_archive", lambda *args: None)
    monkeypatch.setattr(daily, "_validated_venue_items", lambda *args: [])

    def fake_build(_data, _poly, day_root, day, _snapshot):
        artifact = Path(day_root) / "artifact"
        artifact.mkdir(parents=True, exist_ok=True)
        (artifact / "manifest.json").write_text(
            json.dumps({"run_id": f"eu-west-{day}"}) + "\n", encoding="utf-8",
        )
        return artifact

    def fake_run(*args, **kwargs):
        calls["run"] += 1
        assert kwargs["include_observations"] is True
        return _fake_run_result(DAY)

    monkeypatch.setattr(daily.archive_daily, "_build_artifact", fake_build)
    monkeypatch.setattr(daily.proxy, "run", fake_run)
    monkeypatch.setattr(
        daily,
        "_upload_evidence",
        lambda _s3, _bucket, _day, _root, files: [path.name for path in files],
    )


def test_source_hour_selection_is_exact():
    manifest = {
        "files": [
            {
                "file": f"bn_spot.{DAY}T{hour:02d}.txt.gz",
                "size": 1,
                "sha256": "a" * 64,
            }
            for hour in range(24)
        ],
    }

    rows = daily._source_items(manifest, "bn_spot", DAY, {0, 23})

    assert [row["file"] for row in rows] == [
        f"bn_spot.{DAY}T00.txt.gz",
        f"bn_spot.{DAY}T23.txt.gz",
    ]
    with pytest.raises(ValueError, match="hour coverage"):
        daily._source_items(manifest, "coinbase", DAY, {0})


def test_evaluator_freeze_binds_every_dependency(tmp_path):
    frozen, digest = daily.load_evaluator_freeze()

    assert digest == "79fcb19b084b05ba308ea0619951db57828f1b94f62fb295a247f7a9a1491967"
    assert frozen["strategy_fingerprint"] == proxy.EXPECTED_FREEZE_SHA256
    frozen["dependencies_sha256"]["settlement_proxy.py"] = "0" * 64
    path = tmp_path / "freeze.json"
    path.write_text(json.dumps(frozen, sort_keys=True) + "\n", encoding="utf-8")
    path.with_suffix(".sha256").write_text(
        daily.archive_daily._sha256(path) + "\n", encoding="ascii",
    )

    with pytest.raises(ValueError, match="dependency drift"):
        daily.load_evaluator_freeze(path)


def test_daily_process_reuses_result_and_commits_once(tmp_path, monkeypatch):
    calls = {"run": 0}
    _install_fakes(tmp_path, monkeypatch, calls)
    kwargs = {
        "day": DAY,
        "data_dir": tmp_path / "data",
        "poly_dir": tmp_path / "poly",
        "work_dir": tmp_path / "work",
        "state_dir": tmp_path / "state",
        "s3": object(),
        "bucket": "bucket",
        "calibration_path": tmp_path / "calibration.json",
        "provenance_path": tmp_path / "provenance.json",
    }

    first = daily.process_day(**kwargs)
    second = daily.process_day(**kwargs)

    assert first == second
    assert calls["run"] == 1
    assert first["pooled_verdict"]["status"] == "collecting"
    assert (tmp_path / "state" / forward.INDEX).is_file()
    assert (tmp_path / "state" / forward.STATUS).is_file()
    assert not (tmp_path / "work" / DAY / "artifact").exists()
    assert not (tmp_path / "work" / DAY / "venues").exists()
    assert not (tmp_path / "work" / DAY / "state-next").exists()


def test_failed_upload_does_not_commit_staged_state(tmp_path, monkeypatch):
    calls = {"run": 0}
    _install_fakes(tmp_path, monkeypatch, calls)
    monkeypatch.setattr(
        daily,
        "_upload_evidence",
        lambda *args: (_ for _ in ()).throw(RuntimeError("upload failed")),
    )
    state = tmp_path / "state"

    with pytest.raises(RuntimeError, match="upload failed"):
        daily.process_day(
            DAY,
            tmp_path / "data",
            tmp_path / "poly",
            tmp_path / "work",
            state,
            s3=object(),
            bucket="bucket",
            calibration_path=tmp_path / "calibration.json",
            provenance_path=tmp_path / "provenance.json",
        )

    assert not (state / forward.INDEX).exists()
    assert (tmp_path / "work" / DAY / "state-next" / forward.INDEX).is_file()


def test_terminal_day_recovers_after_index_only_partial_commit(tmp_path, monkeypatch):
    calls = {"run": 0}
    _install_fakes(tmp_path, monkeypatch, calls)
    monkeypatch.setattr(daily, "_utc_today", lambda: "20261013")

    def fake_terminal_update(result_path, staged_state, **_kwargs):
        staged_state = Path(staged_state)
        day_ledger = staged_state / forward.DAYS_DIR / f"{DAY}.json"
        day_ledger.parent.mkdir(parents=True, exist_ok=True)
        day_ledger.write_bytes(Path(result_path).read_bytes())
        (staged_state / forward.INDEX).write_text(
            json.dumps({"days": [{"day": DAY}]}) + "\n", encoding="utf-8",
        )
        (staged_state / forward.STATUS).write_text(
            json.dumps({"status": "terminal"}) + "\n", encoding="utf-8",
        )
        (staged_state / forward.VERDICT).write_text(
            json.dumps({"verdict": {"status": "pass"}}) + "\n", encoding="utf-8",
        )
        return {"verdict": {"status": "pass"}}

    monkeypatch.setattr(daily.forward, "update", fake_terminal_update)
    original_commit = daily._commit_completed_day
    state = tmp_path / "state"
    kwargs = {
        "day": DAY,
        "data_dir": tmp_path / "data",
        "poly_dir": tmp_path / "poly",
        "work_dir": tmp_path / "work",
        "state_dir": state,
        "s3": object(),
        "bucket": "bucket",
        "calibration_path": tmp_path / "calibration.json",
        "provenance_path": tmp_path / "provenance.json",
    }

    def crash_after_index(complete, complete_path, state_dir, day_root):
        staged_index = Path(day_root) / "state-next" / forward.INDEX
        destination = Path(state_dir) / forward.INDEX
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(staged_index.read_bytes())
        raise RuntimeError("crash after index")

    monkeypatch.setattr(daily, "_commit_completed_day", crash_after_index)
    with pytest.raises(RuntimeError, match="crash after index"):
        daily.process_day(**kwargs)

    assert (state / forward.INDEX).is_file()
    assert not (state / forward.VERDICT).exists()
    assert not (state / daily.ADMISSION).exists()
    assert (tmp_path / "work" / DAY / "complete.json").is_file()

    monkeypatch.setattr(daily, "_commit_completed_day", original_commit)
    recovered = daily.process_day(**kwargs)

    assert recovered["pooled_verdict"]["status"] == "pass"
    assert (state / forward.VERDICT).is_file()
    admission = json.loads((state / daily.ADMISSION).read_text(encoding="utf-8"))
    assert admission["day"] == DAY
    assert not (tmp_path / "work" / DAY / "state-next").exists()

    terminal = daily.process_day(
        FOLLOWING,
        tmp_path / "data",
        tmp_path / "poly",
        tmp_path / "work",
        state,
        s3=object(),
        bucket="bucket",
        calibration_path=tmp_path / "calibration.json",
        provenance_path=tmp_path / "provenance.json",
    )
    assert terminal["status"] == "terminal"
    assert calls["run"] == 1


def test_daily_process_requires_closed_following_boundary_day(tmp_path, monkeypatch):
    monkeypatch.setattr(daily, "_utc_today", lambda: FOLLOWING)

    with pytest.raises(ValueError, match="boundary day is not closed"):
        daily.process_day(
            DAY,
            tmp_path / "data",
            tmp_path / "poly",
            tmp_path / "work",
            tmp_path / "state",
            s3=object(),
        )
