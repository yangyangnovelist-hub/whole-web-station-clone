from __future__ import annotations

import numpy as np

import jump_models as jm


NS = 1_000_000_000


def test_chronological_masks_reserve_first_validation_third_for_calibration():
    split = np.array([1] * 6 + [2] * 9 + [3] * 3, dtype=np.uint8)
    eligible = np.ones(len(split), dtype=bool)
    eligible[7] = False

    masks = jm.chronological_model_masks(split, eligible)

    assert np.flatnonzero(masks.train).tolist() == list(range(6))
    assert np.flatnonzero(masks.calibration).tolist() == [6, 8]
    assert np.flatnonzero(masks.evaluation).tolist() == list(range(9, 15))
    assert not (masks.train | masks.calibration | masks.evaluation)[split == 3].any()


def test_alerts_require_arrival_and_direction_confidence_then_apply_cooldown():
    times = np.array([0, NS, 5 * NS, 6 * NS], dtype=np.int64)
    arrival = np.array([0.9, 0.9, 0.9, 0.9])
    up = np.array([0.8, 0.4, 0.2, 0.51])

    alerts = jm.alerts_from_scores(
        times,
        arrival,
        up,
        np.ones(4, dtype=bool),
        arrival_threshold=0.5,
        direction_confidence=0.6,
    )

    assert alerts.tolist() == [1, 0, -1, 0]


def test_operating_point_ignores_labels_outside_calibration_mask():
    times = np.arange(20, dtype=np.int64) * 5 * NS
    arrival = np.linspace(0.1, 0.99, 20)
    up = np.full(20, 0.9)
    labels = np.array(([1, 0] * 5) + ([-1, -1] * 5), dtype=np.int8)
    calibration = np.zeros(20, dtype=bool)
    calibration[:10] = True
    matched = np.full(20, -1, dtype=np.int32)

    first = jm.select_operating_point(
        times, arrival, up, labels, matched, np.empty(0, dtype=np.int64), calibration,
        family_size=12, minimum_alerts=2,
    )
    labels[10:] = 1
    second = jm.select_operating_point(
        times, arrival, up, labels, matched, np.empty(0, dtype=np.int64), calibration,
        family_size=12, minimum_alerts=2,
    )

    assert first == second


def test_candidate_grid_is_bounded_and_uses_three_maintained_estimators():
    specs = jm.candidate_specs()

    assert len(specs) == 6
    assert {spec.kind for spec in specs} == {"logistic", "hist_gradient_boosting", "gradient_boosting"}
    assert len({spec.name for spec in specs}) == len(specs)


def test_two_stage_candidate_fits_and_returns_probabilities():
    rng = np.random.default_rng(7)
    X = rng.normal(size=(80, 6)).astype(np.float32)
    labels = np.zeros(80, dtype=np.int8)
    labels[::8] = 1
    labels[4::8] = -1
    model = jm.fit_two_stage_candidate(jm.candidate_specs()[0], X, labels)

    arrival, up = model.predict_scores(X[:7])

    assert arrival.shape == up.shape == (7,)
    assert np.all((0 <= arrival) & (arrival <= 1))
    assert np.all((0 <= up) & (up <= 1))


def test_reliability_table_accounts_for_every_masked_row():
    scores = np.array([0.01, 0.10, 0.30, 0.60, 0.90])
    labels = np.array([0, 0, 1, 0, 1], dtype=np.int8)
    table = jm.reliability_table(scores, labels, np.ones(5, dtype=bool))

    assert sum(row["rows"] for row in table) == 5
    assert sum(row["positives"] for row in table) == 2
