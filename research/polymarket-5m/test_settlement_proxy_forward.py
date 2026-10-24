from __future__ import annotations

import datetime as dt
import json

import pytest

import settlement_proxy as proxy
import settlement_proxy_forward as forward


FINGERPRINT = "f" * 64
CALIBRATION = "c" * 64
PROVENANCE = "p" * 64
EVALUATOR = "e" * 64


def _slot(day: str) -> int:
    return int(dt.datetime.strptime(day, "%Y%m%d").replace(tzinfo=dt.timezone.utc).timestamp())


def _day_payload(day: str) -> dict:
    variants = forward.expected_variants()
    return {
        "schema": forward.DAY_SCHEMA,
        "day": day,
        "strategy_fingerprint": FINGERPRINT,
        "calibration_sha256": CALIBRATION,
        "provenance_sha256": PROVENANCE,
        "evaluator_sha256": EVALUATOR,
        "observations": {
            "signals": {variant: [] for variant in variants},
            "execution_rows": {variant: [] for variant in variants},
            "outcome_rows": [{
                "market_id": f"m-{day}",
                "slot": _slot(day),
                "official": "Up",
                "derived": "Up",
                "status": "compared",
            }],
        },
    }


def _write(path, payload):
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def test_forward_update_is_chronological_and_idempotent(tmp_path):
    first = tmp_path / "first.json"
    _write(first, _day_payload("20261010"))

    result = forward.update(
        first,
        tmp_path / "state",
        fingerprint=FINGERPRINT,
        calibration_sha256=CALIBRATION,
        provenance_sha256=PROVENANCE,
        evaluator_sha256=EVALUATOR,
        holdout_day="20261010",
    )
    repeated = forward.update(
        first,
        tmp_path / "state",
        fingerprint=FINGERPRINT,
        calibration_sha256=CALIBRATION,
        provenance_sha256=PROVENANCE,
        evaluator_sha256=EVALUATOR,
        holdout_day="20261010",
    )

    assert result["days"] == ["20261010"]
    assert result["verdict"]["status"] == "collecting"
    assert repeated == result
    index = json.loads((tmp_path / "state" / forward.INDEX).read_text())
    assert [row["day"] for row in index["days"]] == ["20261010"]


def test_forward_update_rejects_gap_and_changed_admitted_day(tmp_path):
    state = tmp_path / "state"
    first = tmp_path / "first.json"
    _write(first, _day_payload("20261010"))
    forward.update(
        first, state, fingerprint=FINGERPRINT,
        calibration_sha256=CALIBRATION, provenance_sha256=PROVENANCE,
        evaluator_sha256=EVALUATOR,
        holdout_day="20261010",
    )
    gap = tmp_path / "gap.json"
    _write(gap, _day_payload("20261012"))
    with pytest.raises(ValueError, match="next chronological"):
        forward.update(
            gap, state, fingerprint=FINGERPRINT,
            calibration_sha256=CALIBRATION, provenance_sha256=PROVENANCE,
            evaluator_sha256=EVALUATOR,
            holdout_day="20261010",
        )

    changed = _day_payload("20261010")
    changed["observations"]["outcome_rows"][0]["derived"] = "Down"
    _write(first, changed)
    with pytest.raises(ValueError, match="admitted day changed"):
        forward.update(
            first, state, fingerprint=FINGERPRINT,
            calibration_sha256=CALIBRATION, provenance_sha256=PROVENANCE,
            evaluator_sha256=EVALUATOR,
            holdout_day="20261010",
        )


def test_forward_state_without_index_fails_closed(tmp_path):
    state = tmp_path / "state"
    (state / forward.DAYS_DIR).mkdir(parents=True)
    first = tmp_path / "first.json"
    _write(first, _day_payload("20261010"))

    with pytest.raises(ValueError, match="without an index"):
        forward.update(
            first, state, fingerprint=FINGERPRINT,
            calibration_sha256=CALIBRATION, provenance_sha256=PROVENANCE,
            evaluator_sha256=EVALUATOR,
            holdout_day="20261010",
        )


def test_day_validation_rejects_variant_or_utc_day_drift():
    payload = _day_payload("20261010")
    signal = {
        "variant": proxy.BASE_VARIANT,
        "market_id": "m",
        "slot": _slot("20261011"),
        "decision_ms": 1.0,
        "direction": "Up",
        "opening": 100.0,
        "projected_close": 101.0,
        "spot_close": 101.0,
        "deribit_close": None,
        "margin": 1.0,
        "bound": 0.1,
        "actual_winner": "Up",
    }
    payload["observations"]["signals"][proxy.BASE_VARIANT].append(signal)

    with pytest.raises(ValueError, match="outside its UTC day"):
        forward.validate_day_result(
            payload,
            fingerprint=FINGERPRINT,
            calibration_sha256=CALIBRATION,
            provenance_sha256=PROVENANCE,
            evaluator_sha256=EVALUATOR,
        )


def test_day_validation_rejects_signal_without_both_execution_rows():
    payload = _day_payload("20261010")
    payload["observations"]["signals"][proxy.BASE_VARIANT].append({
        "variant": proxy.BASE_VARIANT,
        "market_id": "m",
        "slot": _slot("20261010"),
        "decision_ms": 1.0,
        "direction": "Up",
        "opening": 100.0,
        "projected_close": 101.0,
        "spot_close": 101.0,
        "deribit_close": None,
        "margin": 1.0,
        "bound": 0.1,
        "actual_winner": "Up",
    })

    with pytest.raises(ValueError, match="execution rows are incomplete"):
        forward.validate_day_result(
            payload,
            fingerprint=FINGERPRINT,
            calibration_sha256=CALIBRATION,
            provenance_sha256=PROVENANCE,
            evaluator_sha256=EVALUATOR,
        )


def test_outcome_observations_preserve_prefix_identity():
    rows = [
        {"market_id": "a", "slot": _slot("20261010"), "official": "Up",
         "derived": "Up", "status": "compared"},
        {"market_id": "b", "slot": _slot("20261011"), "official": None,
         "derived": None, "status": "unresolved"},
    ]

    all_rows = proxy.summarize_outcome_observations(rows)
    prefix = proxy.summarize_outcome_observations(rows[:1])

    assert all_rows["passes"] is False
    assert all_rows["unresolved_market_count"] == 1
    assert prefix["passes"] is True
