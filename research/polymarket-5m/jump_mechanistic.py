"""Preregistered, label-blind microstructure rules for 120--220 ms jump warnings.

Thresholds are estimated from the training fold only.  Rule evaluation may inspect validation,
but never the frozen test fold.  Deribit contributes only its live BTC-PERPETUAL quote stream;
its 100 ms batched trade stream is excluded upstream by :mod:`jump_causal`.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np


NS = 1_000_000_000
COOLDOWN_NS = 5 * NS
BARRIER_MAX_BP = 0.35
BREAK_EVEN_PRECISION = 0.25

LEADER_RESIDUAL_GROUPS = {
    "deribit": ("deribit_residual_change_50ms",),
    "okx": ("okx_spot_residual_change_50ms", "okx_perp_residual_change_50ms"),
    "bybit": ("bybit_spot_residual_change_50ms", "bybit_perp_residual_change_50ms"),
    "coinbase": ("coinbase_residual_change_50ms",),
}
LEADER_RESIDUAL_FEATURES = tuple(
    feature for group in LEADER_RESIDUAL_GROUPS.values() for feature in group
)


@dataclass(frozen=True)
class Rule:
    name: str


RULES = (
    Rule("barrier_spot_sweep"),
    Rule("perp_lead"),
    Rule("deribit_lead"),
    Rule("crossvenue_velocity"),
    Rule("depth_flow"),
    Rule("strict_consensus"),
    Rule("all_medium"),
)


def required_feature_names() -> tuple[str, ...]:
    core = (
        "detector_up_distance_bp",
        "detector_down_distance_bp",
        "bn_spot_return_50ms_bp",
        "bn_perp_return_50ms_bp",
        "deribit_return_50ms_bp",
        "bn_spot_signed_notional_50ms",
        "bn_perp_signed_notional_50ms",
        "bn_spot_ask_depletion_200ms",
        "bn_spot_bid_depletion_200ms",
        "bn_perp_ask_depletion_200ms",
        "bn_perp_bid_depletion_200ms",
    )
    return core + LEADER_RESIDUAL_FEATURES


def _column(X: np.ndarray, positions: Mapping[str, int], name: str) -> np.ndarray:
    if name not in positions:
        raise ValueError(f"missing required feature: {name}")
    return np.asarray(X[:, positions[name]], dtype=np.float64)


def _quantile(values: np.ndarray, q: float, floor: float) -> float:
    finite = np.abs(np.asarray(values, dtype=np.float64))
    finite = finite[np.isfinite(finite)]
    if not len(finite):
        return float(floor)
    return max(float(floor), float(np.quantile(finite, q)))


def fit_thresholds(
    X: np.ndarray,
    feature_names: Sequence[str],
    train_mask: np.ndarray,
) -> dict[str, float]:
    """Fit the frozen q75/q90 cutoffs using training rows and no labels."""
    values = np.asarray(X)
    mask = np.asarray(train_mask, dtype=bool)
    if values.ndim != 2 or len(mask) != len(values):
        raise ValueError("training mask must match a two-dimensional feature matrix")
    positions = {name: index for index, name in enumerate(feature_names)}

    def selected(name: str) -> np.ndarray:
        return _column(values, positions, name)[mask]

    spot_velocity = selected("bn_spot_return_50ms_bp")
    perp_velocity = selected("bn_perp_return_50ms_bp")
    deribit_velocity = selected("deribit_return_50ms_bp")
    spot_flow = selected("bn_spot_signed_notional_50ms")
    perp_flow = selected("bn_perp_signed_notional_50ms")
    spot_depth = selected("bn_spot_ask_depletion_200ms") - selected(
        "bn_spot_bid_depletion_200ms"
    )
    perp_depth = selected("bn_perp_ask_depletion_200ms") - selected(
        "bn_perp_bid_depletion_200ms"
    )
    depths = np.concatenate((spot_depth, perp_depth))
    leader_values = np.concatenate([selected(name) for name in LEADER_RESIDUAL_FEATURES])
    deribit_residual = selected("deribit_residual_change_50ms")

    return {
        "spot_velocity_lo": _quantile(spot_velocity, 0.75, 0.05),
        "spot_velocity_hi": _quantile(spot_velocity, 0.90, 0.10),
        "perp_velocity_lo": _quantile(perp_velocity, 0.75, 0.05),
        "perp_velocity_hi": _quantile(perp_velocity, 0.90, 0.10),
        "deribit_velocity_lo": _quantile(deribit_velocity, 0.75, 0.05),
        "deribit_velocity_hi": _quantile(deribit_velocity, 0.90, 0.10),
        "spot_flow_lo": _quantile(spot_flow, 0.75, 1.0),
        "spot_flow_hi": _quantile(spot_flow, 0.90, 2.0),
        "perp_flow_lo": _quantile(perp_flow, 0.75, 1.0),
        "perp_flow_hi": _quantile(perp_flow, 0.90, 2.0),
        "depth_lo": _quantile(depths, 0.75, 0.02),
        "depth_hi": _quantile(depths, 0.90, 0.05),
        "leader_residual": _quantile(leader_values, 0.75, 0.02),
        "deribit_residual_lo": _quantile(deribit_residual, 0.75, 0.02),
        "deribit_residual_hi": _quantile(deribit_residual, 0.90, 0.05),
    }


def _finite_ge(value: float, threshold: float) -> bool:
    return math.isfinite(value) and value >= threshold


def _direction_passes(
    rule: str,
    row: np.ndarray,
    positions: Mapping[str, int],
    thresholds: Mapping[str, float],
    direction: int,
) -> bool:
    def raw(name: str) -> float:
        return float(row[positions[name]])

    def oriented(name: str) -> float:
        return direction * raw(name)

    barrier = raw(
        "detector_up_distance_bp" if direction > 0 else "detector_down_distance_bp"
    )
    if not math.isfinite(barrier) or not 0.0 <= barrier <= BARRIER_MAX_BP:
        return False

    spot_velocity = oriented("bn_spot_return_50ms_bp")
    perp_velocity = oriented("bn_perp_return_50ms_bp")
    deribit_velocity = oriented("deribit_return_50ms_bp")
    spot_flow = oriented("bn_spot_signed_notional_50ms")
    perp_flow = oriented("bn_perp_signed_notional_50ms")
    spot_depth = direction * (
        raw("bn_spot_ask_depletion_200ms") - raw("bn_spot_bid_depletion_200ms")
    )
    perp_depth = direction * (
        raw("bn_perp_ask_depletion_200ms") - raw("bn_perp_bid_depletion_200ms")
    )

    votes: dict[str, bool] = {}
    for venue, features in LEADER_RESIDUAL_GROUPS.items():
        oriented_values = [oriented(name) for name in features]
        votes[venue] = any(
            _finite_ge(value, float(thresholds["leader_residual"]))
            for value in oriented_values
        )
    leader_count = sum(votes.values())
    non_deribit_count = sum(value for venue, value in votes.items() if venue != "deribit")
    deribit_residual = oriented("deribit_residual_change_50ms")

    if rule == "barrier_spot_sweep":
        return (
            _finite_ge(spot_velocity, float(thresholds["spot_velocity_hi"]))
            and _finite_ge(spot_flow, float(thresholds["spot_flow_hi"]))
            and _finite_ge(spot_depth, float(thresholds["depth_lo"]))
        )
    if rule == "perp_lead":
        return (
            _finite_ge(perp_velocity, float(thresholds["perp_velocity_hi"]))
            and _finite_ge(perp_flow, float(thresholds["perp_flow_hi"]))
            and leader_count >= 1
        )
    if rule == "deribit_lead":
        return (
            _finite_ge(deribit_velocity, float(thresholds["deribit_velocity_hi"]))
            and _finite_ge(deribit_residual, float(thresholds["deribit_residual_hi"]))
            and (
                _finite_ge(perp_velocity, float(thresholds["perp_velocity_lo"]))
                or non_deribit_count >= 1
            )
        )
    if rule == "crossvenue_velocity":
        return (
            _finite_ge(spot_velocity, float(thresholds["spot_velocity_lo"]))
            and _finite_ge(perp_velocity, float(thresholds["perp_velocity_lo"]))
            and _finite_ge(deribit_velocity, float(thresholds["deribit_velocity_lo"]))
            and leader_count >= 2
        )
    if rule == "depth_flow":
        return (
            _finite_ge(spot_flow, float(thresholds["spot_flow_lo"]))
            and _finite_ge(perp_flow, float(thresholds["perp_flow_lo"]))
            and _finite_ge(spot_depth, float(thresholds["depth_hi"]))
            and _finite_ge(perp_depth, float(thresholds["depth_hi"]))
        )
    if rule == "strict_consensus":
        return (
            _finite_ge(spot_velocity, float(thresholds["spot_velocity_hi"]))
            and _finite_ge(perp_velocity, float(thresholds["perp_velocity_hi"]))
            and _finite_ge(deribit_velocity, float(thresholds["deribit_velocity_hi"]))
            and _finite_ge(spot_flow, float(thresholds["spot_flow_hi"]))
            and _finite_ge(perp_flow, float(thresholds["perp_flow_hi"]))
            and _finite_ge(spot_depth, float(thresholds["depth_hi"]))
            and _finite_ge(perp_depth, float(thresholds["depth_hi"]))
            and leader_count >= 3
        )
    if rule == "all_medium":
        return (
            _finite_ge(spot_velocity, float(thresholds["spot_velocity_lo"]))
            and _finite_ge(perp_velocity, float(thresholds["perp_velocity_lo"]))
            and _finite_ge(deribit_velocity, float(thresholds["deribit_velocity_lo"]))
            and _finite_ge(spot_flow, float(thresholds["spot_flow_lo"]))
            and _finite_ge(perp_flow, float(thresholds["perp_flow_lo"]))
            and _finite_ge(spot_depth, float(thresholds["depth_lo"]))
            and _finite_ge(perp_depth, float(thresholds["depth_lo"]))
            and leader_count >= 2
        )
    raise ValueError(f"unknown frozen rule: {rule}")


def predict_rule_family(
    X: np.ndarray,
    feature_names: Sequence[str],
    thresholds: Mapping[str, float],
) -> dict[str, np.ndarray]:
    values = np.asarray(X)
    positions = {name: index for index, name in enumerate(feature_names)}
    missing = sorted(set(required_feature_names()) - positions.keys())
    if missing:
        raise ValueError(f"missing required features: {missing}")
    predictions = {rule.name: np.zeros(len(values), dtype=np.int8) for rule in RULES}
    for row_index, row in enumerate(values):
        up_distance = float(row[positions["detector_up_distance_bp"]])
        down_distance = float(row[positions["detector_down_distance_bp"]])
        for rule in RULES:
            up = _direction_passes(rule.name, row, positions, thresholds, 1)
            down = _direction_passes(rule.name, row, positions, thresholds, -1)
            if up and not down:
                predictions[rule.name][row_index] = 1
            elif down and not up:
                predictions[rule.name][row_index] = -1
            elif up and down and math.isfinite(up_distance) and math.isfinite(down_distance):
                if up_distance < down_distance:
                    predictions[rule.name][row_index] = 1
                elif down_distance < up_distance:
                    predictions[rule.name][row_index] = -1
    return predictions


def apply_alert_cooldown(
    times_ns: np.ndarray,
    predictions: np.ndarray,
    *,
    cooldown_ns: int = COOLDOWN_NS,
) -> np.ndarray:
    times = np.asarray(times_ns, dtype=np.int64)
    raw = np.asarray(predictions, dtype=np.int8)
    if len(times) != len(raw):
        raise ValueError("times and predictions must have the same length")
    out = np.zeros(len(raw), dtype=np.int8)
    last_alert = -10**30
    for index, direction in enumerate(raw):
        if direction and int(times[index]) - last_alert >= int(cooldown_ns):
            out[index] = direction
            last_alert = int(times[index])
    return out


def _wilson_lower(hits: int, alerts: int, *, alpha: float) -> float | None:
    if alerts <= 0:
        return None
    z = statistics.NormalDist().inv_cdf(1.0 - alpha / 2.0)
    p = hits / alerts
    denominator = 1.0 + z * z / alerts
    centre = p + z * z / (2.0 * alerts)
    margin = z * math.sqrt(p * (1.0 - p) / alerts + z * z / (4.0 * alerts * alerts))
    return max(0.0, (centre - margin) / denominator)


def _binomial_upper_tail(hits: int, alerts: int, probability: float) -> float | None:
    if alerts <= 0:
        return None
    return min(1.0, sum(
        math.comb(alerts, count)
        * probability**count
        * (1.0 - probability) ** (alerts - count)
        for count in range(hits, alerts + 1)
    ))


def score_predictions(
    times_ns: np.ndarray,
    predictions: np.ndarray,
    labels: np.ndarray,
    matched_jump: np.ndarray,
    jump_exchange_ns: np.ndarray,
    *,
    family_size: int,
) -> dict[str, object]:
    times = np.asarray(times_ns, dtype=np.int64)
    pred = np.asarray(predictions, dtype=np.int8)
    actual = np.asarray(labels, dtype=np.int8)
    matched = np.asarray(matched_jump, dtype=np.int32)
    if not (len(times) == len(pred) == len(actual) == len(matched)):
        raise ValueError("scoring arrays must have equal length")
    selected = pred != 0
    alerts = int(np.count_nonzero(selected))
    hit_mask = selected & (pred == actual)
    hits = int(np.count_nonzero(hit_mask))
    placebo_hits = int(np.count_nonzero(selected & (-pred == actual)))
    precision = hits / alerts if alerts else None
    placebo_precision = placebo_hits / alerts if alerts else None
    valid_matches = hit_mask & (matched >= 0)
    lead = (
        (np.asarray(jump_exchange_ns, dtype=np.int64)[matched[valid_matches]] - times[valid_matches])
        / 1_000_000.0
        if np.any(valid_matches) else np.empty(0, dtype=np.float64)
    )
    lead_summary = {
        "p10": float(np.quantile(lead, 0.10)) if len(lead) else None,
        "p50": float(np.quantile(lead, 0.50)) if len(lead) else None,
        "p90": float(np.quantile(lead, 0.90)) if len(lead) else None,
    }
    adjusted_alpha = 0.05 / max(1, int(family_size))
    return {
        "alerts": alerts,
        "hits": hits,
        "precision": precision,
        "direction_placebo_hits": placebo_hits,
        "direction_placebo_precision": placebo_precision,
        "wilson_lower_95": _wilson_lower(hits, alerts, alpha=0.05),
        "family_adjusted_wilson_lower": _wilson_lower(
            hits, alerts, alpha=adjusted_alpha
        ),
        "break_even_exact_p": _binomial_upper_tail(
            hits, alerts, BREAK_EVEN_PRECISION
        ),
        "assumed_ev_cents_per_share": (
            10.0 * precision - 2.5 if precision is not None else None
        ),
        "lead_time_ms": lead_summary,
    }


def _score_mask(
    times: np.ndarray,
    raw_predictions: np.ndarray,
    labels: np.ndarray,
    matched: np.ndarray,
    jump_times: np.ndarray,
    mask: np.ndarray,
) -> tuple[np.ndarray, dict[str, object]]:
    eligible_predictions = np.where(mask, raw_predictions, 0).astype(np.int8)
    cooled = apply_alert_cooldown(times, eligible_predictions)
    return cooled, score_predictions(
        times,
        cooled,
        labels,
        matched,
        jump_times,
        family_size=len(RULES),
    )


def _validation_subfold_masks(validation: np.ndarray, count: int = 3) -> list[np.ndarray]:
    indices = np.flatnonzero(validation)
    masks: list[np.ndarray] = []
    for part in np.array_split(indices, count):
        mask = np.zeros(len(validation), dtype=bool)
        mask[part] = True
        masks.append(mask)
    return masks


def _ablate(X: np.ndarray, names: Sequence[str], prefix: str) -> np.ndarray:
    values = np.asarray(X, dtype=np.float32).copy()
    columns = [index for index, name in enumerate(names) if name.startswith(prefix)]
    values[:, columns] = np.nan
    return values


def evaluate_dataset(dataset_dir: str | Path) -> dict[str, object]:
    root = Path(dataset_dir)
    X = np.load(root / "features.npy", mmap_mode="r")
    times = np.load(root / "times.npy", mmap_mode="r")
    labels = np.load(root / "labels.npy", mmap_mode="r")
    matched = np.load(root / "matched_jump.npy", mmap_mode="r")
    split = np.load(root / "split.npy", mmap_mode="r")
    eligible = np.load(root / "eligible.npy", mmap_mode="r").astype(bool)
    names = tuple(json.loads((root / "feature_names.json").read_text(encoding="utf-8")))
    jumps = json.loads((root / "target_jumps.json").read_text(encoding="utf-8"))
    jump_times = np.asarray([int(item["exchange_ns"]) for item in jumps], dtype=np.int64)

    train = eligible & (split == 1)
    validation = eligible & (split == 2)
    thresholds = fit_thresholds(X, names, train)
    family = predict_rule_family(X, names, thresholds)
    subfolds = _validation_subfold_masks(validation)

    ablated_predictions = {
        "without_deribit": predict_rule_family(_ablate(X, names, "deribit_"), names, thresholds),
        "without_binance_perp": predict_rule_family(_ablate(X, names, "bn_perp_"), names, thresholds),
    }
    report_rules: dict[str, object] = {}
    passing: list[tuple[float, str]] = []
    for rule in RULES:
        raw = family[rule.name]
        _, train_score = _score_mask(times, raw, labels, matched, jump_times, train)
        validation_predictions, validation_score = _score_mask(
            times, raw, labels, matched, jump_times, validation
        )
        # Preserve the single validation cooldown stream.  Resetting it at artificial subfold
        # boundaries could manufacture one extra alert on each boundary.
        subfold_scores = [
            score_predictions(
                times,
                np.where(fold, validation_predictions, 0).astype(np.int8),
                labels,
                matched,
                jump_times,
                family_size=len(RULES),
            )
            for fold in subfolds
        ]
        shifted = np.zeros_like(validation_predictions)
        shifted[20:] = validation_predictions[:-20]
        shifted = np.where(validation, shifted, 0).astype(np.int8)
        time_shift_score = score_predictions(
            times, shifted, labels, matched, jump_times, family_size=len(RULES)
        )
        ablations = {
            name: _score_mask(
                times,
                predictions[rule.name],
                labels,
                matched,
                jump_times,
                validation,
            )[1]
            for name, predictions in ablated_predictions.items()
        }
        report_rules[rule.name] = {
            "train": train_score,
            "validation": validation_score,
            "validation_subfolds": subfold_scores,
            "time_shift_1000ms": time_shift_score,
            "source_ablations": ablations,
        }
        adjusted = validation_score["family_adjusted_wilson_lower"]
        precision = validation_score["precision"]
        placebo = validation_score["direction_placebo_precision"]
        shifted_precision = time_shift_score["precision"]
        subfold_ok = all(
            score["alerts"] >= 5
            and score["assumed_ev_cents_per_share"] is not None
            and score["assumed_ev_cents_per_share"] > 0
            for score in subfold_scores
        )
        passes = (
            validation_score["alerts"] >= 30
            and precision is not None
            and precision >= 0.30
            and adjusted is not None
            and adjusted > 0.25
            and validation_score["assumed_ev_cents_per_share"] is not None
            and validation_score["assumed_ev_cents_per_share"] > 0
            and placebo is not None
            and precision > placebo
            and shifted_precision is not None
            and precision > shifted_precision
            and subfold_ok
        )
        report_rules[rule.name]["passes_validation_gate"] = passes
        if passes:
            passing.append((float(adjusted), rule.name))

    selected = max(passing)[1] if passing else None
    return {
        "schema": "jump-mechanistic-audit-v1",
        "selection_uses_split_codes": [1, 2],
        "test_split_read_for_selection": False,
        "rows": int(len(times)),
        "eligible_train_rows": int(np.count_nonzero(train)),
        "eligible_validation_rows": int(np.count_nonzero(validation)),
        "thresholds": thresholds,
        "rules": report_rules,
        "selected_rule": selected,
        "family_decision": "promote" if selected else "reject",
        "gate": {
            "minimum_validation_alerts": 30,
            "minimum_subfold_alerts": 5,
            "minimum_precision": 0.30,
            "minimum_family_adjusted_wilson_lower": 0.25,
            "minimum_subfolds": 3,
            "requires_positive_ev_each_subfold": True,
            "requires_direction_and_1000ms_time_shift_placebos": True,
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
