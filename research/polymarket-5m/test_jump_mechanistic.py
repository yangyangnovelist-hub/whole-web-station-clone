from __future__ import annotations

import math

import numpy as np

import jump_mechanistic as jm


NS = 1_000_000_000


def matrix(rows: list[dict[str, float]]) -> tuple[np.ndarray, tuple[str, ...]]:
    names = jm.required_feature_names()
    values = np.full((len(rows), len(names)), np.nan, dtype=np.float32)
    positions = {name: index for index, name in enumerate(names)}
    for row_index, row in enumerate(rows):
        for name, value in row.items():
            values[row_index, positions[name]] = value
    return values, names


def passing_up_row(scale: float = 1.0) -> dict[str, float]:
    row = {
        "detector_up_distance_bp": 0.1,
        "detector_down_distance_bp": 0.9,
        "bn_spot_return_50ms_bp": 2.0 * scale,
        "bn_perp_return_50ms_bp": 2.0 * scale,
        "deribit_return_50ms_bp": 2.0 * scale,
        "bn_spot_signed_notional_50ms": 100.0 * scale,
        "bn_perp_signed_notional_50ms": 100.0 * scale,
        "bn_spot_ask_depletion_200ms": 0.5,
        "bn_spot_bid_depletion_200ms": 0.0,
        "bn_perp_ask_depletion_200ms": 0.5,
        "bn_perp_bid_depletion_200ms": 0.0,
    }
    for name in jm.LEADER_RESIDUAL_FEATURES:
        row[name] = 1.0 * scale
    return row


def fixed_thresholds() -> dict[str, float]:
    return {
        "spot_velocity_lo": 1.0,
        "spot_velocity_hi": 1.5,
        "perp_velocity_lo": 1.0,
        "perp_velocity_hi": 1.5,
        "spot_flow_lo": 10.0,
        "spot_flow_hi": 50.0,
        "perp_flow_lo": 10.0,
        "perp_flow_hi": 50.0,
        "depth_lo": 0.1,
        "depth_hi": 0.3,
        "leader_residual": 0.1,
        "deribit_velocity_lo": 1.0,
        "deribit_velocity_hi": 1.5,
        "deribit_residual_lo": 0.1,
        "deribit_residual_hi": 0.5,
    }


def test_thresholds_use_training_rows_only():
    rows = [passing_up_row(0.5), passing_up_row(1.0), passing_up_row(1_000.0)]
    values, names = matrix(rows)
    first = jm.fit_thresholds(values, names, np.array([True, True, False]))
    values[2] *= 1_000.0
    second = jm.fit_thresholds(values, names, np.array([True, True, False]))

    assert first == second
    assert first["spot_velocity_hi"] <= 2.0
    assert first["spot_flow_hi"] <= 100.0


def test_frozen_rules_choose_direction_without_labels():
    values, names = matrix([passing_up_row()])
    predictions = jm.predict_rule_family(values, names, fixed_thresholds())

    assert set(predictions) == {rule.name for rule in jm.RULES}
    assert all(int(direction[0]) == 1 for direction in predictions.values())
    assert predictions["deribit_lead"].tolist() == [1]


def test_five_second_cooldown_scores_every_quiet_row_but_emits_once():
    times = np.array([0, NS, 5 * NS, 6 * NS], dtype=np.int64)
    raw = np.array([1, 1, -1, 1], dtype=np.int8)

    cooled = jm.apply_alert_cooldown(times, raw)

    assert cooled.tolist() == [1, 0, -1, 0]


def test_metrics_include_placebo_wilson_lead_and_assumed_ev():
    times = np.array([0, 5 * NS, 10 * NS], dtype=np.int64)
    predictions = np.array([1, 1, -1], dtype=np.int8)
    labels = np.array([1, 0, -1], dtype=np.int8)
    matched = np.array([0, -1, 1], dtype=np.int32)
    jump_exchange_ns = np.array([150_000_000, 10 * NS + 200_000_000], dtype=np.int64)

    result = jm.score_predictions(
        times,
        predictions,
        labels,
        matched,
        jump_exchange_ns,
        family_size=len(jm.RULES),
    )

    assert result["alerts"] == 3
    assert result["hits"] == 2
    assert result["precision"] == 2 / 3
    assert result["direction_placebo_hits"] == 0
    assert result["assumed_ev_cents_per_share"] == 10 * (2 / 3) - 2.5
    assert result["lead_time_ms"]["p50"] == 175.0
    assert 0.0 < result["wilson_lower_95"] < result["precision"]
    assert result["family_adjusted_wilson_lower"] < result["wilson_lower_95"]


def test_nan_required_component_abstains_instead_of_becoming_zero():
    row = passing_up_row()
    row["bn_perp_return_50ms_bp"] = math.nan
    values, names = matrix([row])

    predictions = jm.predict_rule_family(values, names, fixed_thresholds())

    assert predictions["barrier_spot_sweep"].tolist() == [1]
    assert predictions["perp_lead"].tolist() == [0]
    assert predictions["all_medium"].tolist() == [0]
