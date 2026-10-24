"""Pure, paper-only formal evaluator for Family 36 forward rows."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import h_forward
import h_replay_run
import source_confirmation
import source_race_forward

ROOT = Path(__file__).resolve().parent
FREEZE_PATH = ROOT / "forward" / "source-confirmation-freeze.json"
FREEZE_SHA256_PATH = FREEZE_PATH.with_suffix(".sha256")
SCHEMA = "source-confirmation-forward-freeze-v1"
RESULT_SCHEMA = "source-confirmation-forward-evaluation-v1"
STRATEGY_ID = source_confirmation.STRATEGY_ID
FAMILY = 36
ARMS = tuple(source_confirmation.ARMS)
EVALUATION_MS = tuple(float(value) for value in source_confirmation.EVALUATION_MS)
TARGET_SHARES = float(source_confirmation.TARGET_SHARES)
CONFIRMATION_WINDOW_MS = float(source_confirmation.CONFIRMATION_WINDOW_MS)
PLACEBO_SHIFT_MS = float(source_confirmation.PLACEBO_SHIFT_MS)
COLLECTOR_REGION = "eu-west-1"
REQUIRED_DEPENDENCIES = frozenset(
    {
        "archive_day.py",
        "complement_queue_lag.py",
        "complement_queue_lag_daily.py",
        "complement_queue_lag_forward.py",
        "eu_strict.py",
        "forward/complement-queue-lag-freeze.json",
        "forward/current-h-freeze.json",
        "h_forward.py",
        "h_replay.py",
        "h_replay_archive.py",
        "h_replay_run.py",
        "h_source_race.py",
        "mix-runtime-requirements.txt",
        "mix_runtime.py",
        "pm_outcomes.py",
        "poly_clob_rec.py",
        "raw_rec.py",
        "requirements.txt",
        "source_confirmation.py",
        "source_confirmation_daily.py",
        "source_confirmation_forward.py",
        "source_race_daily.py",
        "source_race_forward.py",
        "strict_prebuild.py",
        "systemd/recorder-day-boundary.service",
        "systemd/recorder-day-boundary.timer",
        "systemd/complement-queue-lag-daily.service",
        "systemd/complement-queue-lag-daily.timer",
        "systemd/source-confirmation-daily.service",
        "systemd/source-confirmation-daily.timer",
        "systemd/strict-artifact-daily.service",
        "systemd/strict-artifact-daily.timer",
    }
)
_EPS = 1e-9
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_RESULT_FIELDS = tuple(source_confirmation._RESULT_AVAILABILITY_FIELDS)
_ARTIFACT_ROW_BINDING_FIELDS = frozenset({"artifact_row_count", "artifact_rows_sha256"})
_DECISION_NUMBER_FIELDS = (
    "signal_receive_ms",
    "signal_source_ms",
    "fair",
    "fixed_limit",
    "decision_ask",
    "target_shares",
)
_DECISION_VALUE_FIELDS = (
    "market_id",
    "opportunity_id",
    "signal_source",
    "direction",
)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON value: {value}")


def _as_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be an object")
    return value


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{label} is invalid")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} is invalid") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} is non-finite")
    return number


def _same_number(left: Any, right: Any, tolerance: float = _EPS) -> bool:
    try:
        return math.isclose(
            _finite(left, "number"),
            _finite(right, "number"),
            rel_tol=0.0,
            abs_tol=tolerance,
        )
    except ValueError:
        return False


def _optional_number(value: Any, label: str) -> float | None:
    return None if value is None else _finite(value, label)


def _integer(value: Any, label: str) -> int:
    number = _finite(value, label)
    if not number.is_integer():
        raise ValueError(f"{label} must be an integer")
    return int(number)


def _execution_target(execution: Mapping[str, Any]) -> float:
    value = execution.get("target_shares")
    if value is None:
        value = execution.get("minimum_full_fill_shares")
    return _finite(value, "freeze target shares")


def _execution_clock_value(
    frozen: Mapping[str, Any], execution: Mapping[str, Any], name: str
) -> float:
    value = execution.get(name, frozen.get(name))
    return _finite(value, f"freeze {name}")


def _statistic(statistics: Mapping[str, Any], names: Sequence[str], label: str) -> Any:
    for name in names:
        if name in statistics:
            return statistics[name]
    raise ValueError(f"freeze {label} is missing")


def _freeze_parameters(frozen: Mapping[str, Any]) -> dict[str, Any]:
    execution = _as_mapping(frozen.get("execution"), "freeze execution")
    statistics = _as_mapping(frozen.get("statistics"), "freeze statistics")
    evaluations_raw = execution.get("evaluation_ms")
    if not isinstance(evaluations_raw, Sequence) or isinstance(
        evaluations_raw, (str, bytes)
    ):
        raise TypeError("freeze evaluation_ms must be an array")
    evaluations = tuple(
        _finite(value, "freeze evaluation latency") for value in evaluations_raw
    )
    minimum_full_fills = _integer(
        _statistic(
            statistics,
            (
                "minimum_full_fills_per_cell",
                "minimum_resolved_full_fills_per_cell",
                "minimum_full_fills_per_arm_latency",
                "minimum_full_fills_per_arm_and_latency",
                "minimum_full_fills_per_latency",
                "minimum_full_fills",
                "minimum_fills",
            ),
            "minimum full fills",
        ),
        "freeze minimum full fills",
    )
    minimum_days = _integer(
        _statistic(statistics, ("minimum_utc_days",), "minimum UTC days"),
        "freeze minimum UTC days",
    )
    family_tests = _integer(
        _statistic(statistics, ("family_tests",), "family tests"),
        "freeze family tests",
    )
    alpha = _finite(
        _statistic(
            statistics,
            ("one_sided_alpha", "one_sided_day_cluster_alpha", "alpha"),
            "one-sided alpha",
        ),
        "freeze one-sided alpha",
    )
    cutoff_raw = frozen.get("cutoff_ms")
    if cutoff_raw is None:
        cutoff_raw = frozen.get("holdout_start_ms", frozen.get("prospective_cutoff_ms"))
    return {
        "cutoff_ms": _finite(cutoff_raw, "freeze cutoff"),
        "evaluations": evaluations,
        "target_shares": _execution_target(execution),
        "max_order_usd": _finite(
            execution.get("max_order_usd"), "freeze maximum order USD"
        ),
        "confirmation_window_ms": _execution_clock_value(
            frozen, execution, "confirmation_window_ms"
        ),
        "placebo_shift_ms": _execution_clock_value(
            frozen, execution, "placebo_shift_ms"
        ),
        "minimum_full_fills": minimum_full_fills,
        "minimum_days": minimum_days,
        "family_tests": family_tests,
        "alpha": alpha,
    }


def _validate_freeze_contract(
    frozen: Mapping[str, Any], *, production_thresholds: bool
) -> dict[str, Any]:
    if frozen.get("schema") != SCHEMA:
        raise ValueError("unknown source-confirmation forward freeze schema")
    if frozen.get("strategy_id") != STRATEGY_ID:
        raise ValueError("source-confirmation forward freeze strategy drift")
    if frozen.get("family") != FAMILY or isinstance(frozen.get("family"), bool):
        raise ValueError("source-confirmation forward freeze family drift")
    if frozen.get("paper_only") is not True:
        raise ValueError("source-confirmation forward freeze is not paper-only")
    if frozen.get("collector_region") != COLLECTOR_REGION:
        raise ValueError("source-confirmation forward collector region drift")
    arms = frozen.get("arms")
    if not isinstance(arms, Sequence) or isinstance(arms, (str, bytes)):
        raise TypeError("source-confirmation forward arms must be an array")
    if tuple(arms) != ARMS:
        raise ValueError("source-confirmation forward arms drift")

    parameters = _freeze_parameters(frozen)
    created_at = frozen.get("created_at")
    if not isinstance(created_at, str):
        raise TypeError("source-confirmation forward creation time is missing")
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError as exc:
        raise ValueError(
            "source-confirmation forward creation time is invalid"
        ) from exc
    if created.tzinfo is None:
        raise ValueError("source-confirmation forward creation time lacks a timezone")
    created_ms = created.astimezone(UTC).timestamp() * 1_000.0
    cutoff_text = frozen.get("cutoff")
    if not isinstance(cutoff_text, str):
        raise TypeError("source-confirmation forward cutoff time is missing")
    try:
        cutoff = datetime.fromisoformat(cutoff_text)
    except ValueError as exc:
        raise ValueError("source-confirmation forward cutoff time is invalid") from exc
    if cutoff.tzinfo is None or not _same_number(
        cutoff.astimezone(UTC).timestamp() * 1_000.0, parameters["cutoff_ms"]
    ):
        raise ValueError("source-confirmation forward cutoff time drift")
    if parameters["cutoff_ms"] < created_ms - _EPS:
        raise ValueError("source-confirmation forward cutoff predates its freeze")
    if not _same_number(
        parameters["cutoff_ms"], source_confirmation.PROSPECTIVE_CUTOFF_MS
    ):
        raise ValueError("source-confirmation forward cutoff drift")
    if parameters["evaluations"] != EVALUATION_MS:
        raise ValueError("source-confirmation forward evaluation latencies drift")
    if not _same_number(parameters["target_shares"], TARGET_SHARES):
        raise ValueError("source-confirmation forward target shares drift")
    current_h = source_confirmation.replay.load_freeze()
    current_max_order_usd = float(current_h["execution"]["max_order_usd"])
    if not _same_number(parameters["max_order_usd"], current_max_order_usd):
        raise ValueError("source-confirmation forward maximum order USD drift")
    if not _same_number(parameters["confirmation_window_ms"], CONFIRMATION_WINDOW_MS):
        raise ValueError("source-confirmation forward confirmation window drift")
    if not _same_number(parameters["placebo_shift_ms"], PLACEBO_SHIFT_MS):
        raise ValueError("source-confirmation forward placebo shift drift")
    if (
        parameters["minimum_full_fills"] <= 0
        or parameters["minimum_days"] <= 0
        or parameters["family_tests"] <= 0
        or not 0 < parameters["alpha"] < 1
    ):
        raise ValueError("source-confirmation forward statistics are invalid")
    if production_thresholds and (
        parameters["minimum_full_fills"] != 1_000
        or parameters["minimum_days"] != 7
        or parameters["family_tests"] < 874
        or not _same_number(parameters["alpha"], 0.01)
    ):
        raise ValueError("source-confirmation forward production thresholds drift")
    return parameters


def load_freeze(path: str | Path = FREEZE_PATH) -> tuple[dict[str, Any], str]:
    """Load and verify the preregistered evaluator bytes and dependency chain."""
    source = Path(path)
    raw = source.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    expected_path = (
        FREEZE_SHA256_PATH
        if source.resolve() == FREEZE_PATH.resolve()
        else source.with_suffix(".sha256")
    )
    expected = expected_path.read_text(encoding="ascii").strip()
    if _SHA256_RE.fullmatch(expected) is None or digest != expected:
        raise ValueError("source-confirmation forward freeze fingerprint drift")
    try:
        payload = json.loads(raw, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("invalid source-confirmation forward freeze JSON") from exc
    if not isinstance(payload, dict):
        raise TypeError("source-confirmation forward freeze must be an object")
    _validate_freeze_contract(payload, production_thresholds=True)

    dependencies = _as_mapping(
        payload.get("dependencies_sha256"), "freeze dependencies_sha256"
    )
    if set(dependencies) != REQUIRED_DEPENDENCIES:
        raise ValueError("source-confirmation forward dependency closure drift")
    root = ROOT.resolve()
    for raw_name, raw_digest in dependencies.items():
        if not isinstance(raw_name, str) or not raw_name:
            raise ValueError("freeze dependency path is invalid")
        relative = Path(raw_name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("freeze dependency path is invalid")
        if not isinstance(raw_digest, str) or _SHA256_RE.fullmatch(raw_digest) is None:
            raise ValueError(f"freeze dependency hash is invalid: {raw_name}")
        dependency = (ROOT / relative).resolve()
        try:
            dependency.relative_to(root)
        except ValueError as exc:
            raise ValueError("freeze dependency path is invalid") from exc
        if (
            not dependency.is_file()
            or source_race_forward._sha256(dependency) != raw_digest
        ):
            raise ValueError(
                f"source-confirmation forward dependency drift: {raw_name}"
            )
    return payload, digest


_load_freeze = load_freeze


def _duplicate_key(row: Mapping[str, Any]) -> tuple[str, str, float]:
    raw_evaluation = row.get("evaluation_ms")
    try:
        evaluation = math.nan if raw_evaluation is None else float(raw_evaluation)
    except (TypeError, ValueError):
        evaluation = math.nan
    return str(row.get("opportunity_id") or ""), str(row.get("arm") or ""), evaluation


def _canonical_row(row: Mapping[str, Any]) -> str:
    try:
        return json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("row is not canonical JSON data") from exc


def _rows_sha256(rows: Sequence[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(_canonical_row(row).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _deduplicate(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    chosen: dict[tuple[str, str, float], tuple[str, dict[str, Any]]] = {}
    for index, original in enumerate(rows, 1):
        if not isinstance(original, Mapping):
            raise TypeError(f"row {index} is not an object")
        row = dict(original)
        key = _duplicate_key(row)
        canonical = _canonical_row(row)
        previous = chosen.get(key)
        if previous is not None:
            if previous[0] != canonical:
                raise ValueError(f"conflicting duplicate Family 36 row: {key}")
            continue
        chosen[key] = canonical, row
    return [item[1] for item in chosen.values()]


def _result_availability(row: Mapping[str, Any], prefix: str) -> float:
    observed = []
    for field in _RESULT_FIELDS:
        if row.get(field) is not None:
            observed.append(_finite(row[field], f"{prefix} {field}"))
    if not observed:
        raise ValueError(f"{prefix} resolved fill has no result availability timestamp")
    return max(observed)


def _validate_artifact(row: Mapping[str, Any], cutoff: float, prefix: str) -> None:
    started = _finite(row.get("artifact_started_ms"), f"{prefix} artifact start")
    ended = _finite(row.get("artifact_ended_ms"), f"{prefix} artifact end")
    if started < cutoff - _EPS or ended < started - _EPS:
        raise ValueError(f"{prefix} artifact timing is invalid")
    if row.get("artifact_collector_region") != COLLECTOR_REGION:
        raise ValueError(f"{prefix} artifact collector region drift")
    if row.get("artifact_receipt_race_ready") is not True:
        raise ValueError(f"{prefix} artifact is not receipt-race ready")
    artifact_hash = row.get("artifact_files_sha256")
    if (
        not isinstance(artifact_hash, str)
        or _SHA256_RE.fullmatch(artifact_hash) is None
    ):
        raise ValueError(f"{prefix} artifact hash is invalid")
    artifact_row_count = _integer(
        row.get("artifact_row_count"), f"{prefix} artifact row count"
    )
    if artifact_row_count <= 0:
        raise ValueError(f"{prefix} artifact row count is invalid")
    artifact_rows_hash = row.get("artifact_rows_sha256")
    if (
        not isinstance(artifact_rows_hash, str)
        or _SHA256_RE.fullmatch(artifact_rows_hash) is None
    ):
        raise ValueError(f"{prefix} artifact rows hash is invalid")
    run_id = row.get("observation_run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError(f"{prefix} observation run id is missing")
    signal_receive = _finite(row.get("signal_receive_ms"), f"{prefix} signal receipt")
    if signal_receive < started - _EPS or signal_receive > ended + _EPS:
        raise ValueError(f"{prefix} falls outside its artifact interval")


def _validate_zero_fill(row: Mapping[str, Any], prefix: str) -> None:
    if row.get("full_fill") is not False:
        raise ValueError(f"{prefix} no-fill full-fill flag is inconsistent")
    if (
        row.get("fill_price") is not None
        or row.get("fill_fee_per_share") is not None
        or row.get("all_in_cost") is not None
        or row.get("pnl_per_share") is not None
        or row.get("won") is not None
    ):
        raise ValueError(f"{prefix} no-fill economics are inconsistent")
    fee = _finite(row.get("fill_fee"), f"{prefix} no-fill fee")
    pnl = _finite(row.get("pnl"), f"{prefix} no-fill PnL")
    itt = _finite(row.get("intention_to_trade_pnl"), f"{prefix} no-fill ITT PnL")
    if (
        not _same_number(fee, 0.0)
        or not _same_number(pnl, 0.0)
        or not _same_number(itt, 0.0)
    ):
        raise ValueError(f"{prefix} no-fill ITT PnL must be zero")


def _validate_fill(
    row: Mapping[str, Any],
    shares: float,
    target: float,
    max_order_usd: float,
    evaluation_time: float,
    prefix: str,
) -> None:
    if row.get("sent") is not True:
        raise ValueError(f"{prefix} fill was not sent")
    expected_full = shares >= target - _EPS
    if row.get("full_fill") is not expected_full:
        raise ValueError(f"{prefix} full-fill flag is inconsistent")
    if row.get("reason") != ("filled" if expected_full else "partial_fill"):
        raise ValueError(f"{prefix} full/partial fill reason is inconsistent")
    book_receive = row.get("book_receive_ms")
    if book_receive is None:
        raise ValueError(f"{prefix} fill has no causal book receipt")
    if _finite(book_receive, f"{prefix} book receipt") > evaluation_time + _EPS:
        raise ValueError(f"{prefix} book receipt is after evaluation")

    fill_price = _finite(row.get("fill_price"), f"{prefix} fill price")
    fill_fee = _finite(row.get("fill_fee"), f"{prefix} fill fee")
    fee_per_share = _finite(
        row.get("fill_fee_per_share"), f"{prefix} fill fee per share"
    )
    all_in_cost = _finite(row.get("all_in_cost"), f"{prefix} all-in cost")
    if not 0 < fill_price < 1 or fill_fee < 0 or not 0 < all_in_cost < 1:
        raise ValueError(f"{prefix} fill economics are impossible")
    fixed_limit = row.get("fixed_limit")
    if fixed_limit is None:
        raise ValueError(f"{prefix} fill has no fixed limit")
    if fill_price > _finite(fixed_limit, f"{prefix} fixed limit") + _EPS:
        raise ValueError(f"{prefix} fill exceeds its fixed limit")
    if not _same_number(fee_per_share, fill_fee / shares) or not _same_number(
        all_in_cost, fill_price + fill_fee / shares
    ):
        raise ValueError(f"{prefix} fill cost is inconsistent")
    signed_size = _finite(row.get("signed_size"), f"{prefix} signed size")
    maker_usd = _finite(row.get("maker_usd"), f"{prefix} maker USD")
    if signed_size <= 0 or maker_usd <= 0 or maker_usd > max_order_usd + 0.011:
        raise ValueError(f"{prefix} frozen order budget is inconsistent")
    if fill_price * shares > maker_usd + 1e-5:
        raise ValueError(f"{prefix} fill exceeds its frozen maker budget")

    winner = row.get("winner")
    if winner is None:
        raise ValueError(f"{prefix} unresolved fill cannot enter the formal ledger")
    if winner not in ("Up", "Down"):
        raise ValueError(f"{prefix} resolved fill winner is invalid")
    won = row.get("direction") == winner
    expected_per_share = 1.0 - all_in_cost if won else -all_in_cost
    pnl_per_share = _finite(row.get("pnl_per_share"), f"{prefix} PnL per share")
    pnl = _finite(row.get("pnl"), f"{prefix} PnL")
    itt = _finite(row.get("intention_to_trade_pnl"), f"{prefix} ITT PnL")
    if (
        row.get("won") is not won
        or not _same_number(pnl_per_share, expected_per_share)
        or not _same_number(pnl, expected_per_share * shares, 1e-8)
    ):
        raise ValueError(f"{prefix} fill PnL is inconsistent")
    if not _same_number(itt, pnl, 1e-8):
        raise ValueError(f"{prefix} fill ITT PnL is inconsistent")
    result_available = _result_availability(row, prefix)
    if result_available < evaluation_time - _EPS:
        raise ValueError(f"{prefix} result availability predates evaluation")


def _validate_row(
    row: Mapping[str, Any], parameters: Mapping[str, Any], index: int
) -> None:
    prefix = f"row {index}"
    if row.get("strategy_id") != STRATEGY_ID:
        raise ValueError(f"{prefix} strategy_id drift")
    if row.get("family") != FAMILY or isinstance(row.get("family"), bool):
        raise ValueError(f"{prefix} family drift")
    arm = row.get("arm")
    if arm not in ARMS:
        raise ValueError(f"{prefix} arm is not frozen")
    if row.get("variant", arm) != arm:
        raise ValueError(f"{prefix} arm/variant drift")
    if not isinstance(row.get("market_id"), str) or not row.get("market_id"):
        raise ValueError(f"{prefix} market identity is missing")
    if not isinstance(row.get("opportunity_id"), str) or not row.get("opportunity_id"):
        raise ValueError(f"{prefix} opportunity identity is missing")
    if row.get("signal_source") not in source_confirmation.PRIMARY_SOURCES:
        raise ValueError(f"{prefix} signal source is not a frozen primary source")
    if row.get("direction") not in ("Up", "Down"):
        raise ValueError(f"{prefix} direction is invalid")
    if row.get("direct_token", row.get("direction")) != row.get("direction"):
        raise ValueError(f"{prefix} direct token drift")

    receipt = _finite(row.get("signal_receive_ms"), f"{prefix} receipt clock")
    signal = _finite(row.get("signal_ms"), f"{prefix} signal receipt clock")
    _finite(row.get("signal_source_ms"), f"{prefix} source clock")
    evaluation = _finite(row.get("evaluation_ms"), f"{prefix} evaluation latency")
    evaluation_time = _finite(
        row.get("evaluation_time_ms"), f"{prefix} evaluation clock"
    )
    if receipt < float(parameters["cutoff_ms"]) - _EPS:
        raise ValueError(f"{prefix} is before the frozen cutoff")
    if evaluation not in parameters["evaluations"]:
        raise ValueError(f"{prefix} evaluation latency is not frozen")
    if not _same_number(signal, receipt):
        raise ValueError(f"{prefix} signal/receipt clock drift")
    if not _same_number(evaluation_time, receipt + evaluation):
        raise ValueError(f"{prefix} evaluation clock drift")
    if (
        row.get("ordering_clock") != "receive_ms"
        or row.get("match_book_clock") != "receive_ms"
    ):
        raise ValueError(f"{prefix} does not use receipt-clock matching")
    try:
        expected_day = source_race_forward._receipt_day(row)
    except (OverflowError, OSError, TypeError, ValueError) as exc:
        raise ValueError(f"{prefix} receipt day is invalid") from exc
    if row.get("day") != expected_day:
        raise ValueError(f"{prefix} day is not derived from its receipt clock")

    _validate_artifact(row, float(parameters["cutoff_ms"]), prefix)
    if evaluation_time > float(row["artifact_ended_ms"]) + _EPS:
        raise ValueError(f"{prefix} evaluation falls outside its artifact interval")
    fair = _finite(row.get("fair"), f"{prefix} fair")
    if not 0 <= fair <= 1:
        raise ValueError(f"{prefix} fair is invalid")
    for field in ("fixed_limit", "decision_ask"):
        value = _optional_number(row.get(field), f"{prefix} {field}")
        if value is not None and not 0 < value < 1:
            raise ValueError(f"{prefix} {field} is invalid")
    target = _finite(row.get("target_shares"), f"{prefix} target shares")
    if not _same_number(target, parameters["target_shares"]):
        raise ValueError(f"{prefix} target shares drift")
    for field in ("sent", "filled", "full_fill", "censored"):
        if not isinstance(row.get(field), bool):
            raise TypeError(f"{prefix} {field} flag is invalid")
    if row.get("censored") is not False:
        raise ValueError(f"{prefix} is right-censored")

    book_receive = row.get("book_receive_ms")
    if (
        book_receive is not None
        and _finite(book_receive, f"{prefix} book receipt") > evaluation_time + _EPS
    ):
        raise ValueError(f"{prefix} book receipt is after evaluation")
    shares = _finite(row.get("filled_shares"), f"{prefix} filled shares")
    if shares < 0:
        raise ValueError(f"{prefix} filled shares are invalid")
    expected_filled = shares > _EPS
    if row.get("filled") is not expected_filled:
        raise ValueError(f"{prefix} filled flag is inconsistent")
    winner = row.get("winner")
    if winner not in (None, "Up", "Down"):
        raise ValueError(f"{prefix} winner is invalid")
    if expected_filled:
        _validate_fill(
            row,
            shares,
            target,
            float(parameters["max_order_usd"]),
            evaluation_time,
            prefix,
        )
    else:
        _validate_zero_fill(row, prefix)
    source_confirmation._observation_available_ms(row)


def _artifact_identity(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        row["observation_run_id"],
        row["artifact_files_sha256"],
        float(row["artifact_started_ms"]),
        float(row["artifact_ended_ms"]),
        row["artifact_collector_region"],
        row["artifact_receipt_race_ready"],
        int(row["artifact_row_count"]),
        row["artifact_rows_sha256"],
    )


def _same_decision(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    if any(left.get(field) != right.get(field) for field in _DECISION_VALUE_FIELDS):
        return False
    for field in _DECISION_NUMBER_FIELDS:
        left_value = left.get(field)
        right_value = right.get(field)
        if left_value is None or right_value is None:
            if left_value is not right_value:
                return False
        elif not _same_number(left_value, right_value):
            return False
    return True


def _validate_groups(rows: list[dict[str, Any]]) -> None:
    expected_cells = {(arm, latency) for arm in ARMS for latency in EVALUATION_MS}
    groups: dict[str, list[dict[str, Any]]] = {}
    artifact_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(str(row["opportunity_id"]), []).append(row)
        artifact_groups.setdefault(_artifact_identity(row), []).append(row)

    for artifact, sample in artifact_groups.items():
        expected_count = int(sample[0]["artifact_row_count"])
        if len(sample) != expected_count:
            raise ValueError("Family 36 artifact row set is incomplete")
        unbound = [
            {
                key: value
                for key, value in row.items()
                if key not in _ARTIFACT_ROW_BINDING_FIELDS
            }
            for row in sorted(sample, key=_sort_key)
        ]
        digest = source_confirmation.source_race._canonical_digest(unbound)
        if digest != sample[0]["artifact_rows_sha256"]:
            raise ValueError("Family 36 artifact row set hash drift")
        if any(_artifact_identity(row) != artifact for row in sample):
            raise ValueError("Family 36 artifact row binding drift")

    market_artifacts: dict[str, tuple[Any, ...]] = {}
    run_artifacts: dict[str, tuple[Any, ...]] = {}
    hash_artifacts: dict[str, tuple[Any, ...]] = {}
    for opportunity_id, group in groups.items():
        cells = {(str(row["arm"]), float(row["evaluation_ms"])) for row in group}
        if len(group) != 6 or cells != expected_cells:
            raise ValueError(
                f"opportunity {opportunity_id!r} must contain exactly six complete cells"
            )
        canonical = group[0]
        artifact = _artifact_identity(canonical)
        for row in group[1:]:
            if not _same_decision(canonical, row):
                raise ValueError(
                    f"opportunity {opportunity_id!r} frozen decision identity drift"
                )
            if _artifact_identity(row) != artifact:
                raise ValueError(
                    f"opportunity {opportunity_id!r} mixes artifacts or runs"
                )
            if row.get("winner") != canonical.get("winner"):
                raise ValueError(f"opportunity {opportunity_id!r} winner drift")
        resolved_fills = [
            row
            for row in group
            if row.get("filled") and row.get("winner") in ("Up", "Down")
        ]
        if resolved_fills:
            result_identity = tuple(
                resolved_fills[0].get(field) for field in _RESULT_FIELDS
            )
            if any(
                tuple(row.get(field) for field in _RESULT_FIELDS) != result_identity
                for row in resolved_fills[1:]
            ):
                raise ValueError(
                    f"opportunity {opportunity_id!r} result availability drift"
                )

        market = str(canonical["market_id"])
        previous_market_artifact = market_artifacts.setdefault(market, artifact)
        if previous_market_artifact != artifact:
            raise ValueError(f"market {market!r} mixes artifacts or runs")
        run_id = str(canonical["observation_run_id"])
        artifact_hash = str(canonical["artifact_files_sha256"])
        previous_run = run_artifacts.setdefault(run_id, artifact)
        previous_hash = hash_artifacts.setdefault(artifact_hash, artifact)
        if previous_run != artifact or previous_hash != artifact:
            raise ValueError("immutable artifact hash/run id drift")


def _sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        float(row["signal_receive_ms"]),
        str(row["market_id"]),
        str(row["opportunity_id"]),
        ARMS.index(str(row["arm"])),
        float(row["evaluation_ms"]),
    )


def validate_rows(
    rows: Sequence[Mapping[str, Any]], frozen: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Validate and deterministically deduplicate complete Family 36 groups."""
    parameters = _validate_freeze_contract(frozen, production_thresholds=False)
    candidates: list[dict[str, Any]] = []
    for index, original in enumerate(rows, 1):
        if not isinstance(original, Mapping):
            raise TypeError(f"row {index} is not an object")
        row = dict(original)
        _validate_row(row, parameters, index)
        candidates.append(row)
    deduplicated = _deduplicate(candidates)
    deduplicated.sort(key=_sort_key)
    _validate_groups(deduplicated)
    return deduplicated


def _progress(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    cells: dict[str, dict[str, Any]] = {}
    for arm in ARMS:
        by_latency: dict[str, Any] = {}
        for latency in EVALUATION_MS:
            sample = [
                row
                for row in rows
                if row["arm"] == arm
                and float(row["evaluation_ms"]) == latency
                and row.get("full_fill")
                and row.get("winner") in ("Up", "Down")
            ]
            by_latency[f"{latency:g}"] = {
                "resolved_full_fills": len(sample),
                "utc_days": len({str(row["day"]) for row in sample}),
            }
        cells[arm] = by_latency
    return cells


def _available_at(row: Mapping[str, Any], stopping_ms: float) -> bool:
    if row.get("filled") and row.get("winner") is None:
        return False
    return source_confirmation._observation_available_ms(row) <= stopping_ms + _EPS


def _cell_statistics(
    sample: list[dict[str, Any]], *, alpha: float, family_tests: int
) -> dict[str, Any]:
    fills = [row for row in sample if row.get("filled")]
    partials = [row for row in fills if not row.get("full_fill")]
    full_fills = [
        row
        for row in fills
        if row.get("full_fill") and row.get("winner") in ("Up", "Down")
    ]
    shares = sum(float(row["filled_shares"]) for row in fills)
    pnl = sum(float(row["pnl"]) for row in sample)
    wins = sum(row.get("won") is True for row in fills)
    losses = len(fills) - wins
    per_share_sample = [dict(row) for row in fills]
    per_opportunity_sample = [
        {
            **row,
            "pnl": float(row["intention_to_trade_pnl"]),
            "filled_shares": 1.0,
        }
        for row in sample
    ]
    share_lower = (
        h_forward.daily_cluster_lower(per_share_sample, alpha)
        if per_share_sample
        else None
    )
    opportunity_lower = (
        h_forward.daily_cluster_lower(per_opportunity_sample, alpha)
        if per_opportunity_sample
        else None
    )
    raw_exact_p = h_replay_run._exact_pvalue(full_fills)
    corrected_exact_p = min(1.0, raw_exact_p * family_tests)
    net_ev = pnl / shares if shares else None
    itt_ev = pnl / len(sample) if sample else None
    return {
        "opportunities": len(sample),
        "days": len({str(row["day"]) for row in sample}),
        "fills": len(fills),
        "partial_fills": len(partials),
        "full_fills": len(full_fills),
        "full_fill_days": len({str(row["day"]) for row in full_fills}),
        "filled_shares": shares,
        "pnl": pnl,
        "intention_to_trade_pnl": pnl,
        "intention_to_trade_ev_per_opportunity": itt_ev,
        "net_ev_per_share": net_ev,
        "wins": wins,
        "losses": losses,
        "loss_rate": losses / len(fills) if fills else 0.0,
        "day_cluster_lower_per_share": share_lower,
        "day_cluster_lower_per_opportunity": opportunity_lower,
        "day_cluster_lower_99_per_share": share_lower,
        "day_cluster_lower_99_per_opportunity": opportunity_lower,
        "raw_exact_p": raw_exact_p,
        "corrected_exact_p": corrected_exact_p,
    }


def _paired_statistics(
    rows: Sequence[dict[str, Any]], latency: float, arm_metrics: Mapping[str, Any]
) -> dict[str, Any]:
    by_arm = {
        arm: {
            str(row["opportunity_id"]): row
            for row in rows
            if row["arm"] == arm and float(row["evaluation_ms"]) == latency
        }
        for arm in ARMS
    }
    common = sorted(set.intersection(*(set(by_arm[arm]) for arm in ARMS)))
    pnl = {
        arm: math.fsum(
            float(by_arm[arm][key]["intention_to_trade_pnl"]) for key in common
        )
        for arm in ARMS
    }
    count = len(common)
    candidate = "confirmation_veto"
    baseline_delta = pnl[candidate] - pnl["baseline"]
    placebo_delta = pnl[candidate] - pnl["receipt_time_shifted_placebo"]
    return {
        "common_opportunities": count,
        "pnl": pnl,
        "candidate_minus_baseline_pnl": baseline_delta,
        "candidate_minus_placebo_pnl": placebo_delta,
        "candidate_minus_baseline_ev_per_opportunity": (
            baseline_delta / count if count else None
        ),
        "candidate_minus_placebo_ev_per_opportunity": (
            placebo_delta / count if count else None
        ),
        "candidate_minus_baseline_loss_rate": (
            arm_metrics[candidate]["loss_rate"] - arm_metrics["baseline"]["loss_rate"]
        ),
    }


def evaluate(
    rows: Sequence[Mapping[str, Any]], frozen: Mapping[str, Any]
) -> dict[str, Any]:
    """Evaluate one immutable collection without reading or mutating state."""
    parameters = _validate_freeze_contract(frozen, production_thresholds=False)
    validated = validate_rows(rows, frozen)
    cells = _progress(validated)
    stopping_ms = source_confirmation.common_earliest_stopping_timestamp(
        validated,
        minimum_full_fills=int(parameters["minimum_full_fills"]),
        minimum_days=int(parameters["minimum_days"]),
        arms=ARMS,
        evaluation_ms=EVALUATION_MS,
    )
    result: dict[str, Any] = {
        "schema": RESULT_SCHEMA,
        "strategy_id": STRATEGY_ID,
        "family": FAMILY,
        "paper_only": True,
        "promotion_eligible": False,
        "input_rows": len(rows),
        "deduplicated_rows": len(validated),
        "rows_sha256": _rows_sha256(validated),
        "stopping_ms": stopping_ms,
        "required_full_fills_per_cell": parameters["minimum_full_fills"],
        "required_utc_days": parameters["minimum_days"],
        "family_tests": parameters["family_tests"],
        "alpha": parameters["alpha"],
        "cells": cells,
    }
    if stopping_ms is None:
        result["status"] = "collecting"
        return result

    observed = [row for row in validated if _available_at(row, stopping_ms)]
    arms: dict[str, Any] = {}
    metrics_by_latency: dict[str, dict[str, Any]] = {
        f"{latency:g}": {} for latency in EVALUATION_MS
    }
    for arm in ARMS:
        latencies: dict[str, Any] = {}
        for latency in EVALUATION_MS:
            key = f"{latency:g}"
            sample = [
                row
                for row in observed
                if row["arm"] == arm and float(row["evaluation_ms"]) == latency
            ]
            metrics = _cell_statistics(
                sample,
                alpha=float(parameters["alpha"]),
                family_tests=int(parameters["family_tests"]),
            )
            latencies[key] = metrics
            metrics_by_latency[key][arm] = metrics
        arms[arm] = {"latencies": latencies}

    paired = {
        key: _paired_statistics(observed, latency, metrics_by_latency[key])
        for latency, key in ((value, f"{value:g}") for value in EVALUATION_MS)
    }
    controls = {
        key: {
            "baseline": metrics_by_latency[key]["baseline"],
            "receipt_time_shifted_placebo": metrics_by_latency[key][
                "receipt_time_shifted_placebo"
            ],
        }
        for key in metrics_by_latency
    }
    checks: dict[str, dict[str, bool]] = {}
    candidate_name = "confirmation_veto"
    for key in metrics_by_latency:
        candidate = metrics_by_latency[key][candidate_name]
        baseline = metrics_by_latency[key]["baseline"]
        comparison = paired[key]
        checks[key] = {
            "positive_net_ev_per_share": (
                candidate["net_ev_per_share"] is not None
                and candidate["net_ev_per_share"] > 0
            ),
            "positive_itt_ev_per_opportunity": (
                candidate["intention_to_trade_ev_per_opportunity"] is not None
                and candidate["intention_to_trade_ev_per_opportunity"] > 0
            ),
            "positive_day_cluster_lower_per_share": (
                candidate["day_cluster_lower_per_share"] is not None
                and candidate["day_cluster_lower_per_share"] > 0
            ),
            "positive_day_cluster_lower_per_opportunity": (
                candidate["day_cluster_lower_per_opportunity"] is not None
                and candidate["day_cluster_lower_per_opportunity"] > 0
            ),
            "corrected_exact_p_below_alpha": (
                candidate["corrected_exact_p"] < float(parameters["alpha"])
            ),
            "loss_rate_below_baseline": (
                candidate["loss_rate"] < baseline["loss_rate"]
            ),
            "common_opportunity_pnl_above_baseline": (
                comparison["common_opportunities"] > 0
                and comparison["candidate_minus_baseline_pnl"] > 0
            ),
            "common_opportunity_pnl_above_placebo": (
                comparison["common_opportunities"] > 0
                and comparison["candidate_minus_placebo_pnl"] > 0
            ),
        }
    result.update(
        {
            "status": "passed"
            if all(value for latency in checks.values() for value in latency.values())
            else "rejected",
            "arms": arms,
            "controls": controls,
            "paired": paired,
            "checks": checks,
            "observed_rows_at_stop": len(observed),
        }
    )
    return result


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes(), parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"invalid {label} JSON") from exc
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be an object")
    return value


def _bound_result(
    rows: Sequence[Mapping[str, Any]],
    frozen: Mapping[str, Any],
    freeze_sha256: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    validated = validate_rows(rows, frozen)
    result = evaluate(validated, frozen)
    result["freeze_sha256"] = freeze_sha256
    result["ledger_sha256"] = _rows_sha256(validated)
    return validated, result


def accumulate(
    new_rows: Sequence[Mapping[str, Any]],
    frozen: Mapping[str, Any],
    freeze_sha256: str,
    *,
    ledger_path: str | Path,
    status_path: str | Path,
    verdict_path: str | Path,
) -> dict[str, Any]:
    """Atomically pool rows and pin the first terminal formal result."""
    ledger = Path(ledger_path)
    status = Path(status_path)
    verdict = Path(verdict_path)
    existing_rows = h_forward._read_jsonl(ledger)

    if verdict.exists():
        validated, current = _bound_result(existing_rows, frozen, freeze_sha256)
        pinned = _read_json_object(verdict, "source-confirmation verdict")
        if pinned != current:
            raise ValueError("source-confirmation terminal verdict drift")
        if _rows_sha256(validated) != pinned.get("ledger_sha256"):
            raise ValueError("source-confirmation terminal ledger drift")
        return pinned

    if existing_rows:
        validated, existing = _bound_result(existing_rows, frozen, freeze_sha256)
        if existing["status"] in {"passed", "rejected"}:
            source_confirmation._atomic_write_rows(ledger, validated)
            source_race_forward._atomic_json(status, existing)
            source_race_forward._atomic_json(verdict, existing)
            return existing

    validated, result = _bound_result(
        [*existing_rows, *new_rows], frozen, freeze_sha256
    )
    source_confirmation._atomic_write_rows(ledger, validated)
    source_race_forward._atomic_json(status, result)
    if result["status"] in {"passed", "rejected"}:
        source_race_forward._atomic_json(verdict, result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", action="append", nargs="+", required=True)
    parser.add_argument("--freeze", default=str(FREEZE_PATH))
    parser.add_argument("--ledger")
    parser.add_argument("--status-out")
    parser.add_argument("--verdict-out")
    args = parser.parse_args()
    frozen, freeze_sha256 = load_freeze(args.freeze)
    paths = [path for group in args.rows for path in group]
    rows = [row for path in paths for row in h_forward._read_jsonl(path)]
    state_paths = (args.ledger, args.status_out, args.verdict_out)
    if any(state_paths) and not all(state_paths):
        parser.error("--ledger, --status-out and --verdict-out must be used together")
    if all(state_paths):
        result = accumulate(
            rows,
            frozen,
            freeze_sha256,
            ledger_path=args.ledger,
            status_path=args.status_out,
            verdict_path=args.verdict_out,
        )
    else:
        result = evaluate(rows, frozen)
        result["freeze_sha256"] = freeze_sha256
    print(json.dumps(result, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
