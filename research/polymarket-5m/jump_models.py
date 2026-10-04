"""Precision-first two-stage models for causal 120--220 ms jump warnings.

The first model estimates jump arrival; the second estimates direction conditional on a jump.
Only training rows fit estimators.  The first chronological validation third calibrates scores and
selects an operating point; the remaining two thirds evaluate it.  Split code 3 is never scored.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np
from sklearn.base import BaseEstimator
from sklearn.ensemble import GradientBoostingClassifier, HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.utils.class_weight import compute_sample_weight

import jump_mechanistic as metrics


ARRIVAL_QUANTILES = (0.90, 0.95, 0.975, 0.99)
DIRECTION_CONFIDENCES = (0.50, 0.60, 0.70)
OPERATING_POINTS_PER_MODEL = len(ARRIVAL_QUANTILES) * len(DIRECTION_CONFIDENCES)
RANDOM_STATE = 1701


@dataclass(frozen=True)
class ModelMasks:
    train: np.ndarray
    calibration: np.ndarray
    evaluation: np.ndarray


@dataclass(frozen=True)
class CandidateSpec:
    name: str
    kind: str
    params: tuple[tuple[str, float | int], ...]

    @property
    def kwargs(self) -> dict[str, float | int]:
        return dict(self.params)


@dataclass
class TwoStageCandidate:
    arrival_model: Pipeline
    direction_model: Pipeline

    def predict_scores(self, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        values = _finite_or_nan(X)
        arrival = self.arrival_model.predict_proba(values)[:, 1]
        up = self.direction_model.predict_proba(values)[:, 1]
        return np.asarray(arrival, dtype=np.float64), np.asarray(up, dtype=np.float64)


class PlattCalibrator:
    """Small validation-only calibrator backed by maintained sklearn logistic regression."""

    def __init__(self) -> None:
        self.model: LogisticRegression | None = None
        self.constant: float | None = None

    @staticmethod
    def _logit(scores: np.ndarray) -> np.ndarray:
        clipped = np.clip(np.asarray(scores, dtype=np.float64), 1e-6, 1.0 - 1e-6)
        return np.log(clipped / (1.0 - clipped)).reshape(-1, 1)

    def fit(self, scores: np.ndarray, labels: np.ndarray) -> "PlattCalibrator":
        y = np.asarray(labels, dtype=np.int8)
        if not len(y):
            self.constant = 0.0
        elif len(np.unique(y)) < 2:
            self.constant = float(y[0])
        else:
            self.model = LogisticRegression(
                C=1.0,
                class_weight="balanced",
                max_iter=300,
                random_state=RANDOM_STATE,
                solver="lbfgs",
            ).fit(self._logit(scores), y)
        return self

    def predict(self, scores: np.ndarray) -> np.ndarray:
        if self.model is not None:
            return self.model.predict_proba(self._logit(scores))[:, 1]
        return np.full(len(scores), float(self.constant or 0.0), dtype=np.float64)


@dataclass
class CandidateCalibration:
    arrival: PlattCalibrator
    direction: PlattCalibrator

    def predict(
        self, arrival_scores: np.ndarray, direction_scores: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray]:
        return self.arrival.predict(arrival_scores), self.direction.predict(direction_scores)


def candidate_specs() -> tuple[CandidateSpec, ...]:
    """Frozen six-member grid using only estimators shipped by scikit-learn."""
    return (
        CandidateSpec("logistic_c0.1", "logistic", (("C", 0.1),)),
        CandidateSpec("logistic_c1.0", "logistic", (("C", 1.0),)),
        CandidateSpec(
            "hist_depth2_l2",
            "hist_gradient_boosting",
            (("max_depth", 2), ("l2_regularization", 2.0), ("min_samples_leaf", 10)),
        ),
        CandidateSpec(
            "hist_depth3_l10",
            "hist_gradient_boosting",
            (("max_depth", 3), ("l2_regularization", 10.0), ("min_samples_leaf", 10)),
        ),
        CandidateSpec(
            "gboost_stumps",
            "gradient_boosting",
            (("max_depth", 1), ("min_samples_leaf", 5), ("n_estimators", 60)),
        ),
        CandidateSpec(
            "gboost_depth2",
            "gradient_boosting",
            (("max_depth", 2), ("min_samples_leaf", 5), ("n_estimators", 60)),
        ),
    )


def chronological_model_masks(split: np.ndarray, eligible: np.ndarray) -> ModelMasks:
    split_values = np.asarray(split, dtype=np.uint8)
    available = np.asarray(eligible, dtype=bool)
    train = available & (split_values == 1)
    validation_indices = np.flatnonzero(available & (split_values == 2))
    calibration_count = max(1, len(validation_indices) // 3) if len(validation_indices) else 0
    calibration = np.zeros(len(split_values), dtype=bool)
    evaluation = np.zeros(len(split_values), dtype=bool)
    calibration[validation_indices[:calibration_count]] = True
    evaluation[validation_indices[calibration_count:]] = True
    return ModelMasks(train, calibration, evaluation)


def _finite_or_nan(X: np.ndarray) -> np.ndarray:
    values = np.asarray(X, dtype=np.float32)
    if np.isfinite(values).all():
        return values
    return np.where(np.isfinite(values), values, np.nan).astype(np.float32, copy=False)


def _pipeline(spec: CandidateSpec) -> Pipeline:
    imputer = SimpleImputer(strategy="median", keep_empty_features=True, add_indicator=True)
    kwargs = spec.kwargs
    if spec.kind == "logistic":
        estimator: BaseEstimator = LogisticRegression(
            C=float(kwargs["C"]),
            max_iter=400,
            random_state=RANDOM_STATE,
            solver="lbfgs",
        )
        return Pipeline((
            ("imputer", imputer),
            ("scale", StandardScaler()),
            ("model", estimator),
        ))
    if spec.kind == "hist_gradient_boosting":
        estimator = HistGradientBoostingClassifier(
            learning_rate=0.05,
            max_iter=100,
            max_depth=int(kwargs["max_depth"]),
            min_samples_leaf=int(kwargs["min_samples_leaf"]),
            l2_regularization=float(kwargs["l2_regularization"]),
            early_stopping=False,
            random_state=RANDOM_STATE,
        )
    elif spec.kind == "gradient_boosting":
        estimator = GradientBoostingClassifier(
            learning_rate=0.05,
            n_estimators=int(kwargs["n_estimators"]),
            max_depth=int(kwargs["max_depth"]),
            min_samples_leaf=int(kwargs["min_samples_leaf"]),
            random_state=RANDOM_STATE,
        )
    else:
        raise ValueError(f"unknown candidate kind: {spec.kind}")
    return Pipeline((("imputer", imputer), ("model", estimator)))


def _arrival_training_indices(labels: np.ndarray, *, negative_multiple: int = 20) -> np.ndarray:
    y = np.asarray(labels, dtype=np.int8)
    positive = np.flatnonzero(y != 0)
    negative = np.flatnonzero(y == 0)
    if not len(positive) or not len(negative):
        raise ValueError("arrival training requires both jump and non-jump rows")
    limit = min(len(negative), max(100, negative_multiple * len(positive)))
    positions = np.linspace(0, len(negative) - 1, limit, dtype=np.int64)
    return np.sort(np.concatenate((positive, negative[positions])))


def _fit_pipeline(model: Pipeline, X: np.ndarray, y: np.ndarray) -> Pipeline:
    weights = compute_sample_weight(class_weight="balanced", y=y)
    return model.fit(_finite_or_nan(X), y, model__sample_weight=weights)


def fit_two_stage_candidate(
    spec: CandidateSpec,
    X_train: np.ndarray,
    labels_train: np.ndarray,
) -> TwoStageCandidate:
    labels = np.asarray(labels_train, dtype=np.int8)
    arrival_indices = _arrival_training_indices(labels)
    arrival_y = (labels[arrival_indices] != 0).astype(np.int8)
    direction_indices = np.flatnonzero(labels != 0)
    direction_y = (labels[direction_indices] > 0).astype(np.int8)
    if len(np.unique(direction_y)) < 2:
        raise ValueError("direction training requires both up and down jumps")
    arrival_model = _fit_pipeline(
        _pipeline(spec), np.asarray(X_train)[arrival_indices], arrival_y
    )
    direction_model = _fit_pipeline(
        _pipeline(spec), np.asarray(X_train)[direction_indices], direction_y
    )
    return TwoStageCandidate(arrival_model, direction_model)


def fit_calibration(
    model: TwoStageCandidate,
    X_calibration: np.ndarray,
    labels_calibration: np.ndarray,
) -> CandidateCalibration:
    labels = np.asarray(labels_calibration, dtype=np.int8)
    arrival_raw, direction_raw = model.predict_scores(X_calibration)
    arrival = PlattCalibrator().fit(arrival_raw, (labels != 0).astype(np.int8))
    positive = labels != 0
    direction = PlattCalibrator().fit(
        direction_raw[positive], (labels[positive] > 0).astype(np.int8)
    )
    return CandidateCalibration(arrival, direction)


def alerts_from_scores(
    times_ns: np.ndarray,
    arrival_scores: np.ndarray,
    up_scores: np.ndarray,
    mask: np.ndarray,
    *,
    arrival_threshold: float,
    direction_confidence: float,
) -> np.ndarray:
    arrival = np.asarray(arrival_scores, dtype=np.float64)
    up = np.asarray(up_scores, dtype=np.float64)
    available = np.asarray(mask, dtype=bool)
    raw = np.zeros(len(arrival), dtype=np.int8)
    active = available & np.isfinite(arrival) & (arrival >= float(arrival_threshold))
    raw[active & np.isfinite(up) & (up >= float(direction_confidence))] = 1
    raw[active & np.isfinite(up) & (up <= 1.0 - float(direction_confidence))] = -1
    return metrics.apply_alert_cooldown(np.asarray(times_ns, dtype=np.int64), raw)


def select_operating_point(
    times_ns: np.ndarray,
    arrival_scores: np.ndarray,
    up_scores: np.ndarray,
    labels: np.ndarray,
    matched_jump: np.ndarray,
    jump_exchange_ns: np.ndarray,
    calibration_mask: np.ndarray,
    *,
    family_size: int,
    minimum_alerts: int = 5,
) -> dict[str, object]:
    mask = np.asarray(calibration_mask, dtype=bool)
    finite = np.asarray(arrival_scores, dtype=np.float64)[mask]
    finite = finite[np.isfinite(finite)]
    if not len(finite):
        return {"status": "no_scores", "arrival_threshold": None, "direction_confidence": None}
    thresholds = sorted({float(np.quantile(finite, q)) for q in ARRIVAL_QUANTILES})
    candidates: list[dict[str, object]] = []
    for threshold in thresholds:
        for confidence in DIRECTION_CONFIDENCES:
            alerts = alerts_from_scores(
                times_ns,
                arrival_scores,
                up_scores,
                mask,
                arrival_threshold=threshold,
                direction_confidence=confidence,
            )
            score = metrics.score_predictions(
                times_ns,
                alerts,
                labels,
                matched_jump,
                jump_exchange_ns,
                family_size=family_size,
            )
            if int(score["alerts"]) >= int(minimum_alerts):
                candidates.append({
                    "status": "ok",
                    "arrival_threshold": threshold,
                    "direction_confidence": confidence,
                    "score": score,
                })
    if not candidates:
        return {
            "status": "insufficient_alerts",
            "arrival_threshold": None,
            "direction_confidence": None,
        }

    def rank(item: Mapping[str, object]) -> tuple[float, float, int, float, float]:
        score = item["score"]
        assert isinstance(score, Mapping)
        lower = score["family_adjusted_wilson_lower"]
        precision = score["precision"]
        return (
            -1.0 if lower is None else float(lower),
            -1.0 if precision is None else float(precision),
            int(score["alerts"]),
            float(item["arrival_threshold"]),
            float(item["direction_confidence"]),
        )

    return max(candidates, key=rank)


def reliability_table(
    arrival_scores: np.ndarray,
    labels: np.ndarray,
    mask: np.ndarray,
) -> list[dict[str, object]]:
    scores = np.asarray(arrival_scores, dtype=np.float64)
    actual = np.asarray(labels, dtype=np.int8)
    available = np.asarray(mask, dtype=bool) & np.isfinite(scores)
    bounds = (0.0, 0.01, 0.05, 0.10, 0.25, 0.50, 1.0000001)
    rows: list[dict[str, object]] = []
    for lower, upper in zip(bounds[:-1], bounds[1:], strict=True):
        selected = available & (scores >= lower) & (scores < upper)
        count = int(np.count_nonzero(selected))
        positives = int(np.count_nonzero(selected & (actual != 0)))
        rows.append({
            "lower": lower,
            "upper": min(1.0, upper),
            "rows": count,
            "positives": positives,
            "mean_score": float(np.mean(scores[selected])) if count else None,
            "observed_rate": positives / count if count else None,
        })
    return rows


def _predict_indices(
    model: TwoStageCandidate,
    calibration: CandidateCalibration,
    X: np.ndarray,
    indices: np.ndarray,
    *,
    ablate_columns: Sequence[int] = (),
    batch_size: int = 8_192,
) -> tuple[np.ndarray, np.ndarray]:
    arrival = np.full(len(X), np.nan, dtype=np.float64)
    up = np.full(len(X), np.nan, dtype=np.float64)
    for start in range(0, len(indices), batch_size):
        batch_indices = indices[start:start + batch_size]
        batch = np.asarray(X[batch_indices], dtype=np.float32)
        if ablate_columns:
            batch = batch.copy()
            batch[:, list(ablate_columns)] = np.nan
        raw_arrival, raw_up = model.predict_scores(batch)
        calibrated_arrival, calibrated_up = calibration.predict(raw_arrival, raw_up)
        arrival[batch_indices] = calibrated_arrival
        up[batch_indices] = calibrated_up
    return arrival, up


def _score_alerts(
    times: np.ndarray,
    alerts: np.ndarray,
    labels: np.ndarray,
    matched: np.ndarray,
    jump_times: np.ndarray,
    mask: np.ndarray,
    *,
    family_size: int,
) -> dict[str, object]:
    return metrics.score_predictions(
        times,
        np.where(mask, alerts, 0).astype(np.int8),
        labels,
        matched,
        jump_times,
        family_size=family_size,
    )


def _mask_parts(mask: np.ndarray, parts: int = 2) -> list[np.ndarray]:
    result: list[np.ndarray] = []
    for indices in np.array_split(np.flatnonzero(mask), parts):
        item = np.zeros(len(mask), dtype=bool)
        item[indices] = True
        result.append(item)
    return result


def _gate(score: Mapping[str, object], subfolds: Sequence[Mapping[str, object]], shift: Mapping[str, object]) -> bool:
    precision = score["precision"]
    adjusted = score["family_adjusted_wilson_lower"]
    placebo = score["direction_placebo_precision"]
    shifted = shift["precision"]
    return bool(
        int(score["alerts"]) >= 20
        and precision is not None and float(precision) >= 0.30
        and adjusted is not None and float(adjusted) > 0.25
        and placebo is not None and float(precision) > float(placebo)
        and shifted is not None and float(precision) > float(shifted)
        and all(
            int(fold["alerts"]) >= 5
            and fold["assumed_ev_cents_per_share"] is not None
            and float(fold["assumed_ev_cents_per_share"]) > 0
            for fold in subfolds
        )
    )


def _precision_recall_sweep(
    times: np.ndarray,
    arrival: np.ndarray,
    up: np.ndarray,
    labels: np.ndarray,
    matched: np.ndarray,
    jump_times: np.ndarray,
    calibration_mask: np.ndarray,
    evaluation_mask: np.ndarray,
    *,
    family_size: int,
) -> list[dict[str, object]]:
    calibration_scores = arrival[calibration_mask]
    calibration_scores = calibration_scores[np.isfinite(calibration_scores)]
    thresholds = sorted({float(np.quantile(calibration_scores, q)) for q in ARRIVAL_QUANTILES})
    target_events = set(int(value) for value in matched[evaluation_mask & (labels != 0)] if value >= 0)
    rows: list[dict[str, object]] = []
    for threshold in thresholds:
        for confidence in DIRECTION_CONFIDENCES:
            alerts = alerts_from_scores(
                times,
                arrival,
                up,
                evaluation_mask,
                arrival_threshold=threshold,
                direction_confidence=confidence,
            )
            score = _score_alerts(
                times, alerts, labels, matched, jump_times, evaluation_mask,
                family_size=family_size,
            )
            hit_rows = evaluation_mask & (alerts != 0) & (alerts == labels) & (matched >= 0)
            hit_events = set(int(value) for value in matched[hit_rows])
            rows.append({
                "arrival_threshold": threshold,
                "direction_confidence": confidence,
                "alerts": score["alerts"],
                "hits": score["hits"],
                "precision": score["precision"],
                "event_recall": len(hit_events) / len(target_events) if target_events else None,
            })
    return rows


def evaluate_dataset(dataset_dir: str | Path) -> dict[str, object]:
    root = Path(dataset_dir)
    X = np.load(root / "features.npy", mmap_mode="r")
    times = np.load(root / "times.npy", mmap_mode="r")
    labels = np.load(root / "labels.npy", mmap_mode="r")
    matched = np.load(root / "matched_jump.npy", mmap_mode="r")
    split = np.load(root / "split.npy", mmap_mode="r")
    eligible = np.load(root / "eligible.npy", mmap_mode="r").astype(bool)
    feature_names = tuple(json.loads((root / "feature_names.json").read_text(encoding="utf-8")))
    target_jumps = json.loads((root / "target_jumps.json").read_text(encoding="utf-8"))
    jump_times = np.asarray([int(item["exchange_ns"]) for item in target_jumps], dtype=np.int64)
    masks = chronological_model_masks(split, eligible)
    train_indices = np.flatnonzero(masks.train)
    calibration_indices = np.flatnonzero(masks.calibration)
    evaluation_indices = np.flatnonzero(masks.evaluation)
    model_indices = np.concatenate((calibration_indices, evaluation_indices))
    specs = candidate_specs()
    family_size = len(specs) * OPERATING_POINTS_PER_MODEL
    results: dict[str, object] = {}
    fitted: dict[str, tuple[TwoStageCandidate, CandidateCalibration, np.ndarray, np.ndarray]] = {}

    for spec in specs:
        model = fit_two_stage_candidate(spec, X[train_indices], labels[train_indices])
        calibration = fit_calibration(
            model, X[calibration_indices], labels[calibration_indices]
        )
        arrival, up = _predict_indices(model, calibration, X, model_indices)
        point = select_operating_point(
            times,
            arrival,
            up,
            labels,
            matched,
            jump_times,
            masks.calibration,
            family_size=family_size,
        )
        if point["status"] != "ok":
            results[spec.name] = {"operating_point": point, "passes_validation_gate": False}
            fitted[spec.name] = (model, calibration, arrival, up)
            continue
        alerts = alerts_from_scores(
            times,
            arrival,
            up,
            masks.evaluation,
            arrival_threshold=float(point["arrival_threshold"]),
            direction_confidence=float(point["direction_confidence"]),
        )
        score = _score_alerts(
            times, alerts, labels, matched, jump_times, masks.evaluation, family_size=family_size
        )
        subfold_scores = [
            _score_alerts(times, alerts, labels, matched, jump_times, fold, family_size=family_size)
            for fold in _mask_parts(masks.evaluation)
        ]
        shifted = np.zeros_like(alerts)
        shifted[20:] = alerts[:-20]
        shift_score = _score_alerts(
            times, shifted, labels, matched, jump_times, masks.evaluation, family_size=family_size
        )
        results[spec.name] = {
            "kind": spec.kind,
            "params": spec.kwargs,
            "operating_point": point,
            "evaluation": score,
            "evaluation_subfolds": subfold_scores,
            "time_shift_1000ms": shift_score,
            "reliability": reliability_table(arrival, labels, masks.evaluation),
            "precision_recall": _precision_recall_sweep(
                times,
                arrival,
                up,
                labels,
                matched,
                jump_times,
                masks.calibration,
                masks.evaluation,
                family_size=family_size,
            ),
            "alerts_per_hour": (
                float(score["alerts"])
                / max(
                    1e-9,
                    (float(times[evaluation_indices[-1]]) - float(times[evaluation_indices[0]]))
                    / (3_600 * metrics.NS),
                )
                if len(evaluation_indices) > 1 else None
            ),
            "passes_validation_gate": _gate(score, subfold_scores, shift_score),
        }
        fitted[spec.name] = (model, calibration, arrival, up)

    def result_rank(name: str) -> tuple[float, float, int]:
        result = results[name]
        assert isinstance(result, Mapping)
        score = result.get("evaluation")
        if not isinstance(score, Mapping):
            return (-1.0, -1.0, 0)
        lower = score["family_adjusted_wilson_lower"]
        precision = score["precision"]
        return (
            -1.0 if lower is None else float(lower),
            -1.0 if precision is None else float(precision),
            int(score["alerts"]),
        )

    best_name = max((spec.name for spec in specs), key=result_rank)
    passing = [
        spec.name for spec in specs
        if isinstance(results[spec.name], Mapping)
        and bool(results[spec.name].get("passes_validation_gate"))
    ]
    selected = max(passing, key=result_rank) if passing else None

    best_result = results[best_name]
    if isinstance(best_result, dict) and best_result.get("operating_point", {}).get("status") == "ok":
        model, calibration, _, _ = fitted[best_name]
        point = best_result["operating_point"]
        assert isinstance(point, Mapping)
        prefixes = {
            "without_deribit": ("deribit_",),
            "without_binance_perp": ("bn_perp_",),
            "without_other_venues": ("okx_", "bybit_", "coinbase_"),
        }
        ablations: dict[str, object] = {}
        for label, source_prefixes in prefixes.items():
            columns = [
                index for index, name in enumerate(feature_names)
                if any(name.startswith(prefix) for prefix in source_prefixes)
            ]
            arrival, up = _predict_indices(
                model,
                calibration,
                X,
                evaluation_indices,
                ablate_columns=columns,
            )
            alerts = alerts_from_scores(
                times,
                arrival,
                up,
                masks.evaluation,
                arrival_threshold=float(point["arrival_threshold"]),
                direction_confidence=float(point["direction_confidence"]),
            )
            ablations[label] = _score_alerts(
                times, alerts, labels, matched, jump_times, masks.evaluation, family_size=family_size
            )
        best_result["source_ablations"] = ablations

        spot_index = feature_names.index("bn_spot_return_1000ms_bp")
        train_volatility = np.abs(np.asarray(X[train_indices, spot_index], dtype=np.float64))
        cutoff = float(np.nanmedian(train_volatility))
        _, _, arrival, up = fitted[best_name]
        alerts = alerts_from_scores(
            times,
            arrival,
            up,
            masks.evaluation,
            arrival_threshold=float(point["arrival_threshold"]),
            direction_confidence=float(point["direction_confidence"]),
        )
        current_volatility = np.full(len(X), np.nan, dtype=np.float64)
        current_volatility[evaluation_indices] = np.abs(
            np.asarray(X[evaluation_indices, spot_index], dtype=np.float64)
        )
        best_result["regimes"] = {
            "low_1s_volatility": _score_alerts(
                times, alerts, labels, matched, jump_times,
                masks.evaluation & (current_volatility <= cutoff), family_size=family_size,
            ),
            "high_1s_volatility": _score_alerts(
                times, alerts, labels, matched, jump_times,
                masks.evaluation & (current_volatility > cutoff), family_size=family_size,
            ),
            "training_median_abs_return_1000ms_bp": cutoff,
        }

    return {
        "schema": "jump-model-audit-v1",
        "estimators": {
            "library": "scikit-learn",
            "candidate_count": len(specs),
            "operating_points_per_candidate": OPERATING_POINTS_PER_MODEL,
            "family_size": family_size,
        },
        "rows": {
            "train": int(np.count_nonzero(masks.train)),
            "calibration": int(np.count_nonzero(masks.calibration)),
            "evaluation": int(np.count_nonzero(masks.evaluation)),
            "test_untouched": int(np.count_nonzero(eligible & (split == 3))),
        },
        "test_split_read_for_selection": False,
        "candidates": results,
        "best_diagnostic_candidate": best_name,
        "selected_candidate": selected,
        "family_decision": "promote" if selected else "reject",
        "gate": {
            "minimum_evaluation_alerts": 20,
            "minimum_each_subfold_alerts": 5,
            "minimum_precision": 0.30,
            "minimum_family_adjusted_wilson_lower": 0.25,
            "positive_ev_each_subfold": True,
            "must_beat_direction_and_1000ms_shift_placebos": True,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    report = evaluate_dataset(args.dataset)
    rendered = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
