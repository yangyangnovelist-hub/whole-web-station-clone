from __future__ import annotations

import math
import json
from datetime import datetime, timezone

import numpy as np
import pytest

import mix_forward as forward
import mix_hf as mix
import mix_projection as projection


BASE_MS = datetime(2026, 10, 5, tzinfo=timezone.utc).timestamp() * 1_000.0


def _scorer() -> forward.FrozenMixScorer:
    rows = np.arange(8 * len(mix.FEATURES), dtype=float).reshape(8, -1) / 100.0
    target = rows[:, 0] * 0.2 + rows[:, 5] * 0.1
    model = mix.Ridge().fit(rows, target)
    rule = {"id": "R1", "policy": "settle", "model": "ridge", "q": 0.02,
            "cut": projection.RULE_CUT}
    return forward.FrozenMixScorer(tuple(mix.FEATURES), (rule,), {"R1": model})


def _constant_scorer(value: float) -> projection.ProjectedR1Scorer:
    rows = np.arange(8 * len(mix.FEATURES), dtype=float).reshape(8, -1) / 100.0
    model = mix.Ridge().fit(rows, np.full(8, value))
    rule = {"id": "R1", "policy": "settle", "model": "ridge", "q": 0.02,
            "cut": projection.RULE_CUT}
    frozen = forward.FrozenMixScorer(tuple(mix.FEATURES), (rule,), {"R1": model})
    return projection.ProjectedR1Scorer(frozen)


def _row() -> dict:
    direct = {name: float(index + 1) / 100.0
              for index, name in enumerate(forward.DIRECT_MODEL_FEATURES)}
    trend = {name: float(index + 20) / 100.0
             for index, name in enumerate(forward.TREND_MODEL_FEATURES)}
    auxiliary = {"dvol_rv": 0.25}
    return {
        "market_id": "m1", "kind": "jump", "signal_receive_ms": BASE_MS + 1_000.0,
        "signal_source_ms": BASE_MS + 900.0, "direction": "Up", "evaluation_ms": 500.0,
        "jump_z": forward.CONTROL_CUT, "filled": False, "winner": None,
        "day": "2026-10-05",
        "source_strategy_id": forward.STRATEGY_ID,
        "source_protocol_sha256": "d" * 64,
        "model_features_direct": direct, "model_features_trend": trend,
        "model_features_aux": auxiliary,
    }


def test_r1_17f_projection_uses_every_causal_feature_and_mean_projects_absent_factors():
    scorer = projection.ProjectedR1Scorer(_scorer())

    values = scorer.values(_row())
    score = scorer.score(_row())
    model = scorer.frozen.models[projection.RULE_ID]
    explicit = values.copy()
    for name in forward.ABSENT_MODEL_FEATURES:
        explicit[name] = float(model.mu[list(scorer.frozen.features).index(name)])
    explicit_score = float(model.predict(np.asarray(
        [[explicit[name] for name in scorer.frozen.features]], dtype=float,
    ))[0])

    assert all(math.isfinite(values[name]) for name in projection.CAUSAL_FEATURES)
    assert all(math.isnan(values[name]) for name in forward.ABSENT_MODEL_FEATURES)
    assert score == pytest.approx(explicit_score)


def test_projection_selects_only_post_holdout_rows_above_the_frozen_score_cut():
    before = {**_row(), "market_id": "before", "signal_receive_ms": BASE_MS + 999.0}
    eligible = {**_row(), "market_id": "eligible", "signal_receive_ms": BASE_MS + 1_001.0}

    selected, counters = projection.project_rows(
        [before, eligible], _constant_scorer(0.10), holdout_start_ms=BASE_MS + 1_000.0,
        source_protocol_sha256="d" * 64,
    )
    rejected, rejected_counters = projection.project_rows(
        [eligible], _constant_scorer(0.01), holdout_start_ms=BASE_MS + 1_000.0,
        source_protocol_sha256="d" * 64,
    )

    assert [row["market_id"] for row in selected] == ["eligible"]
    assert selected[0]["projection_strategy_id"] == projection.STRATEGY_ID
    assert selected[0]["projection_score"] == 0.10
    assert selected[0]["projected_features"] == list(forward.ABSENT_MODEL_FEATURES)
    assert selected[0]["projection_missing_policy"] == "frozen_ridge_raw_training_mean"
    assert counters == {"before_holdout": 1, "scored": 1, "selected": 1}
    assert rejected == []
    assert rejected_counters == {"scored": 1, "below_score_cut": 1}


def test_projection_gate_uses_first_fill_per_market_and_200_test_correction(monkeypatch):
    monkeypatch.setattr(projection, "MIN_FILLS", 3)
    monkeypatch.setattr(projection, "MIN_DAYS", 2)
    rows = []
    for index, (market, day) in enumerate((
        ("m1", "2026-10-05"), ("m1", "2026-10-05"),
        ("m2", "2026-10-06"), ("m3", "2026-10-07"),
    )):
        rows.append({
            "market_id": market, "kind": "jump", "signal_receive_ms": float(index),
            "signal_source_ms": float(index), "direction": "Up", "evaluation_ms": 500.0,
            "filled": True, "filled_shares": 5.0, "winner": "Up", "won": True,
            "all_in_cost": 0.01, "pnl_per_share": 0.99, "pnl": 4.95, "day": day,
        })
    rows[1].update(winner="Down", won=False, pnl_per_share=-0.99, pnl=-4.95)

    result = projection.verdict(rows)

    assert result["status"] == "passed"
    assert result["fills"] == 3
    assert result["net_ev_per_share"] == pytest.approx(0.99)
    assert result["family_tests"] == 200
    assert result["corrected_exact_p"] == result["raw_exact_p"] * 200


def test_projection_protocol_binds_runner_source_control_and_model_artifacts(tmp_path, monkeypatch):
    historical = tmp_path / "cross-mix-frozen.json"
    rule = {"id": "R1", "policy": "settle", "model": "ridge", "q": 0.02,
            "cut": projection.RULE_CUT}
    historical.write_text(json.dumps({"features": list(mix.FEATURES), "rules": [rule]}),
                          encoding="utf-8")
    model, manifest = mix.model_bundle_paths(historical)
    anchor = mix.model_anchor_path(historical)
    approval = mix.model_approval_path(historical)
    model.write_bytes(b"model")
    approval.write_text('{"approved":true}', encoding="utf-8")
    manifest.write_text(json.dumps({
        "format": mix.MODEL_BUNDLE_FORMAT,
        "frozen_sha256": projection._sha256(historical),
        "features": list(mix.FEATURES), "rules": [rule],
        "model_file": model.name, "model_sha256": projection._sha256(model),
    }), encoding="utf-8")
    anchor.write_text(json.dumps({
        "format": mix.MODEL_ANCHOR_FORMAT,
        "frozen_file": historical.name, "frozen_sha256": projection._sha256(historical),
        "model_file": model.name, "model_sha256": projection._sha256(model),
        "manifest_file": manifest.name, "manifest_sha256": projection._sha256(manifest),
        "approval_file": approval.name, "approval_sha256": projection._sha256(approval),
    }), encoding="utf-8")
    control = tmp_path / "mix-control-freeze.json"
    control_payload = {
        "schema": forward.FREEZE_SCHEMA,
        "strategy_id": forward.STRATEGY_ID,
        "made": "2026-10-05T01:41:16Z",
        "holdout_start_utc": "2026-10-05T13:00:00Z",
        "runner_sha256": projection._sha256(forward.__file__),
    }
    control_payload["protocol_sha256"] = projection._canonical_sha(control_payload)
    control.write_text(json.dumps(control_payload), encoding="utf-8")
    destination = tmp_path / "mix-projection-freeze.json"
    monkeypatch.setattr(forward, "load_scorer", lambda _: pytest.fail(
        "protocol creation must not deserialize a version-pinned model"))

    frozen = projection.create_protocol(
        destination, historical, control, "2099-10-05T13:00:00Z",
    )
    monkeypatch.delenv("EXPECTED_PROJECTION_PROTOCOL_SHA256", raising=False)
    with pytest.raises(ValueError, match="EXPECTED_PROJECTION_PROTOCOL_SHA256 is required"):
        projection.load_protocol(destination)
    monkeypatch.setenv("EXPECTED_PROJECTION_PROTOCOL_SHA256", frozen["protocol_sha256"])

    loaded, holdout_start_ms = projection.load_protocol(destination)
    assert loaded == frozen
    assert holdout_start_ms == datetime(2099, 10, 5, 13, tzinfo=timezone.utc).timestamp() * 1_000.0
    assert loaded["family_tests"] == 200
    assert loaded["source_control_protocol_sha256"] == control_payload["protocol_sha256"]
    assert loaded["model_approval_sha256"] == projection._sha256(approval)
    assert set(loaded["dependency_sha256"]) == {
        "mix_forward.py", "mix_hf.py", "binary.py", "h_replay_archive.py",
        "h_replay_run.py", "jump2s.py", "pm_outcomes.py",
    }
    with pytest.raises(ValueError, match="already exists"):
        projection.create_protocol(destination, historical, control, "2099-10-05T14:00:00Z")
    with pytest.raises(ValueError, match="before its holdout"):
        projection.create_protocol(tmp_path / "past.json", historical, control, "2020-01-01T00:00:00Z")

    model.write_bytes(b"changed")
    with pytest.raises(ValueError, match="model artifact"):
        projection.load_protocol(destination)


def test_projection_ledger_only_backfills_outcomes_and_pins_terminal_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(projection, "MIN_FILLS", 1)
    monkeypatch.setattr(projection, "MIN_DAYS", 1)
    ledger = tmp_path / "ledger.jsonl"
    verdict_path = tmp_path / "verdict.json"
    row = {
        "market_id": "m1", "kind": "jump", "signal_receive_ms": BASE_MS + 1_000.0,
        "signal_source_ms": BASE_MS + 900.0, "direction": "Up", "evaluation_ms": 500.0,
        "filled": True, "filled_shares": 5.0, "fill_price": 0.4, "fill_fee": 0.01,
        "all_in_cost": 0.402, "winner": None, "won": None, "pnl_per_share": None,
        "pnl": None, "day": "2026-10-05", "projection_strategy_id": projection.STRATEGY_ID,
    }
    protocol = {"protocol_sha256": "b" * 64}

    first = projection.append_rows(ledger, [row])
    assert projection.verdict(first)["status"] == "collecting"
    resolved = {**row, "winner": "Up", "won": True, "pnl_per_share": 0.598, "pnl": 2.99}
    merged = projection.append_rows(ledger, [resolved])
    pinned = projection.pin_verdict(merged, verdict_path, protocol, ledger, "f" * 64, 1)

    assert pinned["status"] == "rejected"
    assert pinned["strategy_id"] == projection.STRATEGY_ID
    assert projection.pin_verdict(merged, verdict_path, protocol, ledger, "f" * 64, 1) == pinned

    with pytest.raises(ValueError, match="execution-field conflict"):
        projection.append_rows(ledger, [{**resolved, "fill_price": 0.41}])
    with pytest.raises(ValueError, match="PnL is inconsistent"):
        projection.append_rows(ledger, [{**resolved, "market_id": "m2", "pnl": 9.0}])
    with pytest.raises(ValueError, match="UTC day"):
        projection.append_rows(ledger, [{**resolved, "market_id": "m3", "day": "2026-10-06"}])


def test_run_protocol_projects_control_rows_into_one_current_ledger_and_report(tmp_path, monkeypatch):
    source = tmp_path / "control.jsonl"
    ledger = tmp_path / "projection.jsonl"
    report = tmp_path / "projection.json"
    verdict_path = tmp_path / "verdict.json"
    row = _row()
    source.write_text(json.dumps(row) + "\n", encoding="utf-8")
    protocol = {"protocol_sha256": "d" * 64, "strategy_id": projection.STRATEGY_ID,
                "source_control_protocol_sha256": "d" * 64}
    monkeypatch.setattr(projection, "load_protocol", lambda _: (protocol, 0.0))
    monkeypatch.setattr(projection, "load_projected_scorer", lambda *_: _constant_scorer(0.10))

    result = projection.run_protocol("ignored.json", source, ledger, report, verdict_path)

    persisted = projection._read_jsonl(ledger)
    assert len(persisted) == 1
    assert persisted[0]["projection_strategy_id"] == projection.STRATEGY_ID
    assert result["projection"]["selected"] == 1
    assert result["verdict"]["status"] == "collecting"
    assert result["source_ledger_sha256"] == projection._sha256(source)
    assert result["ledger_sha256"] == projection._sha256(ledger)
    assert json.loads(report.read_text(encoding="utf-8")) == result

    ledger.write_text(ledger.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    recovered = projection.run_protocol("ignored.json", source, ledger, report, verdict_path)
    assert recovered["ledger_sha256"] == projection._sha256(ledger)
    assert json.loads(report.read_text(encoding="utf-8")) == recovered


def test_projection_skips_incomplete_causal_rows_but_rejects_factor_or_protocol_leakage():
    incomplete = _row()
    incomplete["model_features_aux"] = {"dvol_rv": None}
    selected, counters = projection.project_rows(
        [incomplete], _constant_scorer(0.10), holdout_start_ms=0.0,
        source_protocol_sha256="d" * 64,
    )
    assert selected == [] and counters == {"incomplete_features": 1}

    leaked = _row()
    leaked["model_features_aux"] = {"dvol_rv": 0.1, "f_ridge5": 0.9}
    with pytest.raises(ValueError, match="forbidden factor"):
        projection.project_rows([leaked], _constant_scorer(0.10), holdout_start_ms=0.0,
                                source_protocol_sha256="d" * 64)

    wrong_source = {**_row(), "source_protocol_sha256": "e" * 64}
    with pytest.raises(ValueError, match="source protocol"):
        projection.project_rows([wrong_source], _constant_scorer(0.10), holdout_start_ms=0.0,
                                source_protocol_sha256="d" * 64)
