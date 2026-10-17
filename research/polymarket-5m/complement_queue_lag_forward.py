"""Immutable paper-forward accumulator for Family 34 queue-lag observations."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import complement_queue_lag as replay
import h_forward
import h_replay_run
import source_race_forward

LEDGER = "observations.jsonl"
STATUS = "status.json"
VERDICT = "verdict.json"
JOURNAL = ".update-journal.json"

FREEZE_SCHEMA = "complement-queue-lag-forward-freeze-v1"
STATUS_SCHEMA = "complement-queue-lag-forward-status-v1"
VERDICT_SCHEMA = "complement-queue-lag-forward-verdict-v1"
JOURNAL_SCHEMA = "complement-queue-lag-forward-journal-v1"

_STAGED_FILES = {
    LEDGER: f".{LEDGER}.next",
    STATUS: f".{STATUS}.next",
    VERDICT: f".{VERDICT}.next",
}

ROOT = Path(__file__).resolve().parent
_DEPENDENCY_FILES = frozenset(
    {
        "complement_queue_lag.py",
        "complement_queue_lag_forward.py",
        "h_forward.py",
        "h_replay.py",
        "h_replay_archive.py",
        "h_replay_run.py",
        "source_race_forward.py",
    }
)

VARIANTS = ("base", "reverse_order", "delay_5s")
LATENCIES = (400.0, 500.0)
TARGET_SHARES = 5.0
MINIMUM_ORDER_USD = 1.0
DELAYED_CONTROL_MS = 5_000.0
MINIMUM_ENTRIES = 1_000
MINIMUM_DAYS = 7
FAMILY_TESTS = 100
ALPHA = 0.01

_FREEZE_FIELDS = frozenset(
    {
        "schema",
        "strategy_id",
        "paper_only",
        "prospective_cutoff_ms",
        "dependencies_sha256",
        "execution",
        "statistics",
    }
)
_QUEUE_LAG_RULES = {
    "minimum_queue_shares": 5.0,
    "minimum_midpoint_move": 0.01,
    "maximum_spread_widen": 0.0,
    "confirmation_ms": 250.0,
    "target_shares": 5.0,
    "minimum_total_edge": 0.05,
    "minimum_order_usd": 1.0,
    "minimum_remaining_s": 30.0,
    "maximum_remaining_s": 240.0,
    "book_fresh_ms": 2_000.0,
    "conservative_limit_tick": 0.01,
    "delayed_control_ms": 5_000.0,
}
_EXECUTION_FIELDS = frozenset({*_QUEUE_LAG_RULES, "evaluation_ms"})
_STATISTICS_FIELDS = frozenset(
    {
        "minimum_full_fills_per_latency",
        "minimum_utc_days",
        "family_tests",
        "one_sided_alpha",
    }
)

_EPS = 1e-9
_DIGEST = re.compile(r"[0-9a-f]{64}")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,511}")
_META_FIELDS = frozenset(
    {
        "_artifact_sha256",
        "_artifact_utc_day",
        "_artifact_start_ms",
        "_artifact_end_ms",
        "_observation_key",
    }
)

_REQUIRED_FIELDS = frozenset(
    {
        "signal_id",
        "parent_signal_id",
        "variant",
        "market_id",
        "epoch",
        "decision_ms",
        "remaining_s",
        "exhausted_ms",
        "exhausted_source_ms",
        "exhausted_asset_id",
        "buy_asset_id",
        "buy_side",
        "fair",
        "decision_source_ms",
        "decision_vwap",
        "decision_levels",
        "fixed_limit",
        "decision_fee",
        "net_edge",
        "pre_best_bid",
        "pre_best_bid_size",
        "pre_midpoint",
        "post_midpoint",
        "paper_only",
        "ordering_clock",
        "match_book_clock",
        "evaluation_ms",
        "evaluation_recv_ms",
        "book_recv_ms",
        "book_source_ms",
        "target_shares",
        "filled_shares",
        "fill_vwap",
        "fill_levels",
        "fee_rate",
        "fee",
        "total_cost",
        "winner",
        "won",
        "pnl_per_share",
        "pnl",
        "filled",
        "full_fill",
        "sent",
        "reason",
    }
)

_PAIR_FIELDS = (
    "signal_id",
    "parent_signal_id",
    "variant",
    "market_id",
    "epoch",
    "decision_ms",
    "remaining_s",
    "exhausted_ms",
    "exhausted_source_ms",
    "exhausted_asset_id",
    "buy_asset_id",
    "buy_side",
    "fair",
    "decision_source_ms",
    "decision_vwap",
    "decision_levels",
    "fixed_limit",
    "decision_fee",
    "net_edge",
    "pre_best_bid",
    "pre_best_bid_size",
    "pre_midpoint",
    "post_midpoint",
    "paper_only",
    "ordering_clock",
    "match_book_clock",
    "target_shares",
    "fee_rate",
    "winner",
)

_PARENT_FIELDS = (
    "market_id",
    "epoch",
    "exhausted_ms",
    "exhausted_source_ms",
    "exhausted_asset_id",
    "buy_asset_id",
    "buy_side",
    "fair",
    "decision_source_ms",
    "decision_vwap",
    "decision_levels",
    "fixed_limit",
    "decision_fee",
    "net_edge",
    "pre_best_bid",
    "pre_best_bid_size",
    "pre_midpoint",
    "post_midpoint",
    "target_shares",
    "fee_rate",
    "winner",
)

_NO_FILL_REASONS = frozenset(
    {
        "ask_above_frozen_limit",
        "book_unavailable",
        "disconnect",
        "equal_receipt_ambiguous",
        "right_censored",
    }
)


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{label} is not a number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{label} is not a number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} is non-finite")
    return number


def _reject_nonfinite(value: Any, label: str) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{label} contains a non-finite value")
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_nonfinite(item, f"{label}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_nonfinite(item, f"{label}[{index}]")


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _digest(value: Any, label: str = "artifact digest") -> str:
    digest = str(value).lower()
    if _DIGEST.fullmatch(digest) is None:
        raise ValueError(f"{label} must be a 64-character SHA-256 digest")
    return digest


def _same(left: Any, right: Any, *, tolerance: float = _EPS) -> bool:
    if (
        isinstance(left, (int, float))
        and not isinstance(left, bool)
        and isinstance(right, (int, float))
        and not isinstance(right, bool)
    ):
        return math.isclose(float(left), float(right), rel_tol=0, abs_tol=tolerance)
    return _canonical(left) == _canonical(right)


def _config_integer(value: Any, label: str) -> int:
    number = _finite_number(value, label)
    integer = int(number)
    if number != integer:
        raise ValueError(f"{label} must be an integer")
    return integer


def _require_exact_keys(
    value: Mapping[str, Any], expected: frozenset[str], label: str
) -> None:
    actual = frozenset(value)
    if actual == expected:
        return
    missing = sorted(expected - actual)
    unexpected = sorted(repr(key) for key in actual - expected)
    details: list[str] = []
    if missing:
        details.append(f"missing {', '.join(missing)}")
    if unexpected:
        details.append(f"unexpected {', '.join(unexpected)}")
    raise ValueError(f"Family34 {label} keys drift: {'; '.join(details)}")


def _utc_day(timestamp_ms: float) -> str:
    try:
        return (
            datetime.fromtimestamp(timestamp_ms / 1_000.0, tz=UTC)
            .date()
            .isoformat()
        )
    except (OSError, OverflowError, ValueError) as exc:
        raise ValueError("invalid decision clock") from exc


def _artifact_window(
    expected_utc_day: str,
    artifact_start_ms: Any,
    artifact_end_ms: Any,
) -> tuple[str, float, float]:
    try:
        parsed_day = datetime.strptime(expected_utc_day, "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise ValueError("artifact expected UTC day is invalid") from exc
    if parsed_day.isoformat() != expected_utc_day:
        raise ValueError("artifact expected UTC day is invalid")
    start = _finite_number(artifact_start_ms, "artifact start")
    end = _finite_number(artifact_end_ms, "artifact end")
    day_start = datetime.combine(parsed_day, datetime.min.time(), tzinfo=UTC).timestamp() * 1_000.0
    day_end = day_start + 86_400_000.0
    if start > end:
        raise ValueError("artifact time bounds are reversed")
    if start < day_start or end >= day_end:
        raise ValueError("artifact time bounds are outside the expected UTC day")
    return expected_utc_day, start, end


def _validate_artifact_clock(
    value: Any,
    field: str,
    *,
    prefix: str,
    window: tuple[str, float, float],
) -> float:
    timestamp = _finite_number(value, f"{prefix} {field}")
    day, start, end = window
    if (
        timestamp < start - _EPS
        or timestamp > end + _EPS
        or _utc_day(timestamp) != day
    ):
        raise ValueError(f"{prefix} artifact timestamp {field} is outside its bounds")
    return timestamp


def _protocol(frozen: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(frozen, Mapping):
        raise TypeError("Family34 freeze/config must be an object")
    _reject_nonfinite(frozen, "freeze/config")
    _require_exact_keys(frozen, _FREEZE_FIELDS, "freeze/config")
    if frozen["schema"] != FREEZE_SCHEMA:
        raise ValueError("Family34 freeze/config schema drift")
    cutoff = _finite_number(frozen["prospective_cutoff_ms"], "prospective cutoff")
    strategy_id = _identifier(frozen["strategy_id"], "strategy_id")
    if frozen["paper_only"] is not True:
        raise ValueError("Family34 freeze/config is not paper-only")

    dependencies = frozen["dependencies_sha256"]
    if not isinstance(dependencies, Mapping):
        raise TypeError("Family34 dependency hashes must be an object")
    _require_exact_keys(dependencies, _DEPENDENCY_FILES, "dependency hashes")
    for name in sorted(_DEPENDENCY_FILES):
        expected_digest = _digest(dependencies[name], f"dependency hash {name}")
        dependency = ROOT / name
        if not dependency.is_file() or _file_sha256(dependency) != expected_digest:
            raise ValueError(f"Family34 dependency hash mismatch: {name}")

    execution = frozen["execution"]
    if not isinstance(execution, Mapping):
        raise TypeError("Family34 execution config must be an object")
    _require_exact_keys(execution, _EXECUTION_FIELDS, "execution config")
    for field, expected in _QUEUE_LAG_RULES.items():
        actual = _finite_number(execution[field], f"execution config {field}")
        if actual != expected:
            raise ValueError(f"Family34 execution config {field} drift")
    serialized_evaluations = execution["evaluation_ms"]
    if not isinstance(serialized_evaluations, list):
        raise TypeError("Family34 execution config evaluation_ms is not serialized")
    evaluations = tuple(
        _finite_number(value, "execution config evaluation_ms")
        for value in serialized_evaluations
    )
    if evaluations != LATENCIES:
        raise ValueError("Family34 execution config evaluation_ms drift")
    target = float(execution["target_shares"])
    minimum_order = float(execution["minimum_order_usd"])
    delay = float(execution["delayed_control_ms"])

    statistics = frozen["statistics"]
    if not isinstance(statistics, Mapping):
        raise TypeError("Family34 statistics config must be an object")
    _require_exact_keys(statistics, _STATISTICS_FIELDS, "statistics config")
    minimum_entries = _config_integer(
        statistics["minimum_full_fills_per_latency"],
        "minimum entries per latency",
    )
    minimum_days = _config_integer(statistics["minimum_utc_days"], "minimum UTC days")
    family_tests = _config_integer(statistics["family_tests"], "family tests")
    alpha = _finite_number(statistics["one_sided_alpha"], "one-sided alpha")
    if (
        minimum_entries != MINIMUM_ENTRIES
        or minimum_days != MINIMUM_DAYS
        or family_tests != FAMILY_TESTS
        or not math.isclose(alpha, ALPHA, rel_tol=0, abs_tol=1e-15)
    ):
        raise ValueError("Family34 stopping/statistical constants drift")
    return {
        "strategy_id": strategy_id,
        "cutoff_ms": cutoff,
        "evaluation_ms": LATENCIES,
        "target_shares": target,
        "minimum_order_usd": minimum_order,
        "delayed_control_ms": delay,
        "minimum_entries": minimum_entries,
        "minimum_days": minimum_days,
        "family_tests": family_tests,
        "alpha": alpha,
        "config_sha256": _canonical_sha256(frozen),
    }


def observation_key(row: Mapping[str, Any], artifact_sha256: str) -> str:
    """Return an unambiguous artifact-namespaced observation identity."""
    digest = _digest(artifact_sha256)
    signal_id = _identifier(row.get("signal_id"), "signal_id")
    variant = str(row.get("variant") or "")
    if variant not in VARIANTS:
        raise ValueError("ordinary Family34 observation has an invalid variant")
    evaluation = _finite_number(row.get("evaluation_ms"), "evaluation_ms")
    if evaluation not in LATENCIES:
        raise ValueError("ordinary Family34 observation is not a 400/500 ms row")
    identity = ["family34", digest, signal_id, variant, int(evaluation)]
    return f"family34:{digest}:{_canonical_sha256(identity)}"


def _ordinary_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key not in _META_FIELDS}


def _validate_row(
    row: Mapping[str, Any],
    *,
    cutoff_ms: float,
    index: int,
    artifact_window: tuple[str, float, float] | None = None,
) -> None:
    prefix = f"row {index}"
    if not isinstance(row, Mapping):
        raise TypeError(f"{prefix} is not an object")
    variant = str(row.get("variant") or "")
    if variant == "same_batch":
        raise ValueError(
            f"{prefix} same_batch diagnostic is not an ordinary Family34 row"
        )
    missing = _REQUIRED_FIELDS - row.keys()
    if missing:
        raise ValueError(f"{prefix} is missing fields: {', '.join(sorted(missing))}")
    unexpected = row.keys() - _REQUIRED_FIELDS
    if unexpected:
        raise ValueError(
            f"{prefix} has unexpected fields: {', '.join(sorted(unexpected))}"
        )
    if variant not in VARIANTS:
        raise ValueError(f"{prefix} has an unfrozen Family34 variant")
    _reject_nonfinite(row, prefix)
    if row["paper_only"] is not True:
        raise ValueError(f"{prefix} is not paper-only")
    if (
        row["ordering_clock"] != "local_receipt_ms"
        or row["match_book_clock"] != "local_receipt_ms"
    ):
        raise ValueError(f"{prefix} receipt-clock semantics drift")

    market_id = _identifier(row["market_id"], f"{prefix} market_id")
    signal_id = _identifier(row["signal_id"], f"{prefix} signal_id")
    expected_prefix = f"{market_id}:{variant}:"
    suffix = (
        signal_id[len(expected_prefix) :]
        if signal_id.startswith(expected_prefix)
        else ""
    )
    if not suffix.isdigit() or int(suffix) <= 0:
        raise ValueError(f"{prefix} has an invalid or colliding signal ID")
    exhausted_asset = _identifier(
        row["exhausted_asset_id"], f"{prefix} exhausted asset ID"
    )
    buy_asset = _identifier(row["buy_asset_id"], f"{prefix} buy asset ID")
    if exhausted_asset == buy_asset:
        raise ValueError(f"{prefix} has colliding direct-token IDs")
    parent = row["parent_signal_id"]
    if variant == "delay_5s":
        _identifier(parent, f"{prefix} parent_signal_id")
    elif parent is not None:
        raise ValueError(f"{prefix} independent variant unexpectedly has a parent")
    epoch = row["epoch"]
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
        raise ValueError(f"{prefix} epoch is invalid")
    if row["buy_side"] not in {"Up", "Down"}:
        raise ValueError(f"{prefix} buy side is invalid")
    for name in ("sent", "filled", "full_fill"):
        if not isinstance(row[name], bool):
            raise TypeError(f"{prefix} {name} flag is invalid")
    reason = row["reason"]
    if not isinstance(reason, str) or not reason:
        raise ValueError(f"{prefix} execution reason is missing")

    exhausted_ms = _finite_number(row["exhausted_ms"], f"{prefix} exhausted clock")
    exhausted_source_ms = _finite_number(
        row["exhausted_source_ms"], f"{prefix} exhausted source clock"
    )
    decision_ms = _finite_number(row["decision_ms"], f"{prefix} decision clock")
    decision_source_ms = _finite_number(
        row["decision_source_ms"], f"{prefix} decision source clock"
    )
    remaining_s = _finite_number(row["remaining_s"], f"{prefix} remaining time")
    evaluation_recv = _finite_number(
        row["evaluation_recv_ms"], f"{prefix} evaluation clock"
    )
    book_recv = row["book_recv_ms"]
    book_source = row["book_source_ms"]
    if (book_recv is None) is not (book_source is None):
        raise ValueError(f"{prefix} book receipt/source clocks are inconsistent")
    if artifact_window is not None:
        for field, value in (
            ("exhausted_ms", exhausted_ms),
            ("exhausted_source_ms", exhausted_source_ms),
            ("decision_ms", decision_ms),
            ("decision_source_ms", decision_source_ms),
        ):
            _validate_artifact_clock(
                value, field, prefix=prefix, window=artifact_window
            )
        if reason == "right_censored":
            _day, artifact_start, artifact_end = artifact_window
            if (
                evaluation_recv < artifact_start - _EPS
                or evaluation_recv > artifact_end + max(LATENCIES) + _EPS
                or evaluation_recv <= artifact_end + _EPS
            ):
                raise ValueError(
                    f"{prefix} artifact timestamp evaluation_recv_ms is outside "
                    "its scheduled censoring bound"
                )
        else:
            _validate_artifact_clock(
                evaluation_recv,
                "evaluation_recv_ms",
                prefix=prefix,
                window=artifact_window,
            )
        if book_recv is not None:
            _validate_artifact_clock(
                book_recv, "book_recv_ms", prefix=prefix, window=artifact_window
            )
            _validate_artifact_clock(
                book_source,
                "book_source_ms",
                prefix=prefix,
                window=artifact_window,
            )
    if exhausted_ms < cutoff_ms or decision_ms < cutoff_ms:
        raise ValueError(f"{prefix} is before the frozen cutoff")
    if exhausted_ms > decision_ms + _EPS:
        raise ValueError(f"{prefix} decision predates queue exhaustion")
    _utc_day(decision_ms)
    if variant == "base" and not (
        exhausted_ms + _EPS < decision_ms and decision_ms - exhausted_ms <= 250.0 + _EPS
    ):
        raise ValueError(f"{prefix} base confirmation timing drift")
    if variant == "reverse_order" and not math.isclose(
        decision_ms, exhausted_ms, rel_tol=0, abs_tol=_EPS
    ):
        raise ValueError(f"{prefix} reverse-order decision timing drift")
    if variant != "delay_5s" and not 30.0 - _EPS <= remaining_s <= 240.0 + _EPS:
        raise ValueError(f"{prefix} is outside the frozen market window")

    evaluation = _finite_number(row["evaluation_ms"], f"{prefix} evaluation")
    if evaluation not in LATENCIES or not math.isclose(
        evaluation_recv,
        decision_ms + evaluation,
        rel_tol=0,
        abs_tol=1e-6,
    ):
        raise ValueError(f"{prefix} evaluation timing drift")
    if (
        book_recv is not None
        and _finite_number(book_recv, f"{prefix} book clock") > evaluation_recv + _EPS
    ):
        raise ValueError(f"{prefix} uses a future direct book")

    target = _finite_number(row["target_shares"], f"{prefix} target shares")
    shares = _finite_number(row["filled_shares"], f"{prefix} filled shares")
    fee_rate = _finite_number(row["fee_rate"], f"{prefix} fee rate")
    fair = _finite_number(row["fair"], f"{prefix} fair")
    decision_vwap = _finite_number(row["decision_vwap"], f"{prefix} decision VWAP")
    fixed_limit = _finite_number(row["fixed_limit"], f"{prefix} fixed limit")
    decision_fee = _finite_number(row["decision_fee"], f"{prefix} decision fee")
    net_edge = _finite_number(row["net_edge"], f"{prefix} net edge")
    pre_best_bid = _finite_number(row["pre_best_bid"], f"{prefix} pre-event bid")
    pre_queue = _finite_number(row["pre_best_bid_size"], f"{prefix} depleted queue")
    pre_midpoint = _finite_number(row["pre_midpoint"], f"{prefix} pre-event midpoint")
    post_midpoint = _finite_number(
        row["post_midpoint"], f"{prefix} post-event midpoint"
    )
    if not math.isclose(target, TARGET_SHARES, rel_tol=0, abs_tol=_EPS):
        raise ValueError(f"{prefix} target is not exactly five shares")
    if not 0 < fee_rate < 1:
        raise ValueError(f"{prefix} historical fee rate is impossible")
    expected_fee_rate = 0.072 if decision_ms < replay._FEE_RATE_072_UNTIL_MS else 0.07
    if fee_rate != expected_fee_rate:
        raise ValueError(f"{prefix} historical fee rate is inconsistent")
    decision_levels_payload = row["decision_levels"]
    if not isinstance(decision_levels_payload, list):
        raise TypeError(f"{prefix} decision levels are not a list")
    decision_levels: list[tuple[float, float]] = []
    previous_price = -math.inf
    for level_index, level in enumerate(decision_levels_payload, 1):
        if not isinstance(level, Mapping):
            raise TypeError(f"{prefix} decision level {level_index} is invalid")
        if set(level) != {"price", "shares"}:
            raise ValueError(f"{prefix} decision level {level_index} schema drift")
        price = _finite_number(
            level.get("price"), f"{prefix} decision level price"
        )
        level_shares = _finite_number(
            level.get("shares"), f"{prefix} decision level shares"
        )
        if not 0 < price < 1 or level_shares <= 0 or price <= previous_price:
            raise ValueError(f"{prefix} decision levels are not executable asks")
        decision_levels.append((price, level_shares))
        previous_price = price
    decision_level_shares = sum(size for _, size in decision_levels)
    decision_notional = sum(price * size for price, size in decision_levels)
    if (
        not decision_levels
        or not math.isclose(
            decision_level_shares, target, rel_tol=0, abs_tol=1e-8
        )
        or not math.isclose(
            decision_vwap,
            decision_notional / target,
            rel_tol=0,
            abs_tol=1e-9,
        )
        or not math.isclose(
            fixed_limit, decision_levels[-1][0], rel_tol=0, abs_tol=_EPS
        )
    ):
        raise ValueError(
            f"{prefix} decision levels do not reproduce five-share VWAP/limit"
        )
    expected_decision_fee = replay._charged_fee(decision_levels, fee_rate)
    if not math.isclose(
        decision_fee, expected_decision_fee, rel_tol=0, abs_tol=5e-9
    ):
        raise ValueError(f"{prefix} decision fee does not match venue truncation")
    if not (
        0 < fair < 1
        and 0 < decision_vwap <= fixed_limit < 1
        and 0 < pre_best_bid < 1
        and 0 < post_midpoint < pre_midpoint < 1
    ):
        raise ValueError(f"{prefix} signal prices are impossible")
    if pre_queue < TARGET_SHARES - _EPS:
        raise ValueError(f"{prefix} depleted queue is below five shares")
    if pre_midpoint - post_midpoint < 0.01 - _EPS:
        raise ValueError(f"{prefix} midpoint move is below one cent")
    if not math.isclose(fair, 1.0 - post_midpoint, rel_tol=0, abs_tol=1e-9):
        raise ValueError(f"{prefix} complementary-token fair value is inconsistent")
    limit_units = fixed_limit / 0.01
    if not math.isclose(limit_units, round(limit_units), rel_tol=0, abs_tol=_EPS):
        raise ValueError(f"{prefix} fixed limit is not on the conservative cent grid")
    if fixed_limit * target < MINIMUM_ORDER_USD - _EPS:
        raise ValueError(f"{prefix} is below the venue minimum")
    if (
        net_edge < 0.05 - _EPS
        or not math.isclose(
            net_edge,
            fair * target - decision_notional - expected_decision_fee,
            rel_tol=0,
            abs_tol=1e-8,
        )
    ):
        raise ValueError(f"{prefix} decision fee/edge is inconsistent")

    levels_payload = row["fill_levels"]
    if not isinstance(levels_payload, list):
        raise TypeError(f"{prefix} fill levels are not a list")
    if not row["filled"]:
        nullable = ("fill_vwap", "total_cost", "won", "pnl_per_share", "pnl")
        fee = row["fee"]
        if (
            abs(shares) > _EPS
            or levels_payload
            or row["full_fill"]
            or any(row[name] is not None for name in nullable)
            or (fee is not None and abs(_finite_number(fee, f"{prefix} fee")) > _EPS)
        ):
            raise ValueError(f"{prefix} unfilled economics are inconsistent")
        if row["winner"] not in {None, "Up", "Down"}:
            raise ValueError(f"{prefix} winner is invalid")
        if reason not in _NO_FILL_REASONS:
            raise ValueError(f"{prefix} no-fill reason is invalid")
        if (reason == "right_censored") is row["sent"]:
            raise ValueError(f"{prefix} sent/censoring status is inconsistent")
        return

    if row["sent"] is not True or not _EPS < shares <= target + _EPS:
        raise ValueError(f"{prefix} filled quantity is impossible")
    if (
        book_recv is None
        or not 0 <= evaluation_recv - float(book_recv) <= 2_000.0 + _EPS
    ):
        raise ValueError(f"{prefix} fill lacks a fresh direct book")
    levels: list[tuple[float, float]] = []
    for level_index, level in enumerate(levels_payload, 1):
        if not isinstance(level, Mapping):
            raise TypeError(f"{prefix} fill level {level_index} is invalid")
        if set(level) != {"price", "shares"}:
            raise ValueError(f"{prefix} fill level {level_index} schema drift")
        price = _finite_number(level.get("price"), f"{prefix} fill level price")
        level_shares = _finite_number(
            level.get("shares"), f"{prefix} fill level shares"
        )
        if not 0 < price <= fixed_limit + _EPS or level_shares <= 0:
            raise ValueError(f"{prefix} fill levels violate direct depth/limit")
        levels.append((price, level_shares))
    level_shares = sum(size for _, size in levels)
    notional = sum(price * size for price, size in levels)
    fill_vwap = _finite_number(row["fill_vwap"], f"{prefix} fill VWAP")
    if (
        not levels
        or not math.isclose(level_shares, shares, rel_tol=0, abs_tol=1e-8)
        or not math.isclose(fill_vwap, notional / shares, rel_tol=0, abs_tol=1e-9)
    ):
        raise ValueError(
            f"{prefix} fill levels do not reproduce direct fill depth/VWAP"
        )
    expected_fee = replay._charged_fee(levels, fee_rate)
    fee = _finite_number(row["fee"], f"{prefix} fee")
    if not math.isclose(fee, expected_fee, rel_tol=0, abs_tol=5e-9):
        raise ValueError(f"{prefix} fee does not match venue truncation")
    expected_total = notional + expected_fee
    total_cost = _finite_number(row["total_cost"], f"{prefix} total cost")
    if not math.isclose(total_cost, expected_total, rel_tol=0, abs_tol=1e-8):
        raise ValueError(f"{prefix} total cost is inconsistent")
    full_fill = math.isclose(shares, TARGET_SHARES, rel_tol=0, abs_tol=_EPS)
    if row["full_fill"] is not full_fill or reason != (
        "filled" if full_fill else "partial_fill"
    ):
        raise ValueError(f"{prefix} full/partial fill status is inconsistent")
    winner = row["winner"]
    if winner is None:
        if any(row[name] is not None for name in ("won", "pnl_per_share", "pnl")):
            raise ValueError(f"{prefix} unresolved fill has PnL")
        return
    if winner not in {"Up", "Down"} or not isinstance(row["won"], bool):
        raise ValueError(f"{prefix} resolved winner is invalid")
    won = winner == row["buy_side"]
    expected_pnl = (shares if won else 0.0) - expected_total
    pnl = _finite_number(row["pnl"], f"{prefix} PnL")
    pnl_per_share = _finite_number(row["pnl_per_share"], f"{prefix} PnL per share")
    if (
        row["won"] is not won
        or not math.isclose(pnl, expected_pnl, rel_tol=0, abs_tol=1e-8)
        or not math.isclose(
            pnl_per_share, expected_pnl / shares, rel_tol=0, abs_tol=1e-9
        )
    ):
        raise ValueError(f"{prefix} fill PnL is inconsistent")


def _artifact_of(row: Mapping[str, Any]) -> str:
    value = row.get("_artifact_sha256")
    return "" if value is None else _digest(value)


def _group_signals(
    rows: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str, str], list[Mapping[str, Any]]]:
    grouped: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    for row in rows:
        key = (_artifact_of(row), str(row["variant"]), str(row["signal_id"]))
        grouped.setdefault(key, []).append(row)
    return grouped


def _validate_dataset(
    rows: Sequence[Mapping[str, Any]], frozen: Mapping[str, Any]
) -> dict[tuple[str, str, str], list[Mapping[str, Any]]]:
    protocol = _protocol(frozen)
    artifact_bindings: dict[str, tuple[str, float, float]] = {}
    for index, decorated in enumerate(rows, 1):
        if not isinstance(decorated, Mapping):
            raise TypeError(f"row {index} is not an object")
        metadata = _META_FIELDS & decorated.keys()
        window = None
        if metadata and metadata != _META_FIELDS:
            raise ValueError(f"row {index} has incomplete observation metadata")
        if metadata:
            window = _artifact_window(
                str(decorated["_artifact_utc_day"]),
                decorated["_artifact_start_ms"],
                decorated["_artifact_end_ms"],
            )
        _validate_row(
            _ordinary_row(decorated),
            cutoff_ms=protocol["cutoff_ms"],
            index=index,
            artifact_window=window,
        )
        if metadata:
            assert window is not None
            artifact = _artifact_of(decorated)
            previous_window = artifact_bindings.setdefault(artifact, window)
            if previous_window != window:
                raise ValueError(f"row {index} artifact admission binding drift")
            expected_key = observation_key(decorated, artifact)
            if decorated["_observation_key"] != expected_key:
                raise ValueError(f"row {index} observation key drift")

    grouped = _group_signals(rows)
    signal_identity: dict[tuple[str, str], tuple[str, str]] = {}
    market_identity: dict[tuple[str, str], tuple[str, str]] = {}
    for (artifact, variant, signal_id), signal_rows in grouped.items():
        representative = signal_rows[0]
        identity_key = (artifact, signal_id)
        identity = (variant, str(representative["market_id"]))
        previous = signal_identity.setdefault(identity_key, identity)
        if previous != identity:
            raise ValueError("colliding signal ID across Family34 observations")
        market_key = (variant, str(representative["market_id"]))
        market_value = (artifact, signal_id)
        previous_market = market_identity.setdefault(market_key, market_value)
        if previous_market != market_value:
            raise ValueError("colliding Family34 market/signal identity")
        evaluations = [float(row["evaluation_ms"]) for row in signal_rows]
        if len(evaluations) != len(set(evaluations)):
            raise ValueError("duplicate signal evaluation")
        if set(evaluations) != set(LATENCIES):
            raise ValueError("ordinary Family34 signal must have paired 400/500 rows")
        ordered = sorted(signal_rows, key=lambda row: float(row["evaluation_ms"]))
        first = ordered[0]
        for sibling in ordered[1:]:
            if any(not _same(first[field], sibling[field]) for field in _PAIR_FIELDS):
                raise ValueError("paired Family34 rows have decision-field drift")

    base = {
        (artifact, signal_id): signal_rows
        for (artifact, variant, signal_id), signal_rows in grouped.items()
        if variant == "base"
    }
    child_of: dict[tuple[str, str], str] = {}
    for (artifact, variant, signal_id), signal_rows in grouped.items():
        representative = signal_rows[0]
        if variant != "delay_5s":
            continue
        parent_id = str(representative["parent_signal_id"])
        parent_rows = base.get((artifact, parent_id))
        if parent_rows is None:
            raise ValueError("delay_5s row has an orphan or cross-artifact parent")
        if child_of.setdefault((artifact, parent_id), signal_id) != signal_id:
            raise ValueError("base signal has colliding delay_5s children")
        parent = parent_rows[0]
        if any(
            not _same(representative[field], parent[field]) for field in _PARENT_FIELDS
        ):
            raise ValueError("delay_5s fields drift from the immutable parent signal")
        if not math.isclose(
            float(representative["decision_ms"]),
            float(parent["decision_ms"]) + DELAYED_CONTROL_MS,
            rel_tol=0,
            abs_tol=1e-6,
        ) or not math.isclose(
            float(representative["remaining_s"]),
            float(parent["remaining_s"]) - DELAYED_CONTROL_MS / 1_000.0,
            rel_tol=0,
            abs_tol=1e-6,
        ):
            raise ValueError("delay_5s does not use its parent's decision clock")
    return grouped


def validate_rows(
    rows: Iterable[Mapping[str, Any]],
    frozen: Mapping[str, Any],
    artifact_sha256: str | None = None,
    *,
    expected_utc_day: str,
    artifact_start_ms: float,
    artifact_end_ms: float,
    artifact_digest: str | None = None,
) -> list[dict[str, Any]]:
    """Validate one complete artifact and attach stable ledger identities."""
    if (
        artifact_sha256 is not None
        and artifact_digest is not None
        and _digest(artifact_sha256) != _digest(artifact_digest)
    ):
        raise ValueError("artifact digest arguments disagree")
    selected_digest = (
        artifact_sha256 if artifact_sha256 is not None else artifact_digest
    )
    if selected_digest is None:
        raise ValueError("artifact digest is required")
    digest = _digest(selected_digest)
    protocol = _protocol(frozen)
    window = _artifact_window(
        expected_utc_day, artifact_start_ms, artifact_end_ms
    )
    materialized: list[dict[str, Any]] = []
    for index, source in enumerate(rows, 1):
        if not isinstance(source, Mapping):
            raise TypeError(f"row {index} is not an object")
        if _META_FIELDS & source.keys():
            raise ValueError(f"row {index} attempts to supply reserved ledger metadata")
        row = dict(source)
        _validate_row(
            row,
            cutoff_ms=protocol["cutoff_ms"],
            index=index,
            artifact_window=window,
        )
        row["_artifact_sha256"] = digest
        row["_artifact_utc_day"] = window[0]
        row["_artifact_start_ms"] = window[1]
        row["_artifact_end_ms"] = window[2]
        row["_observation_key"] = observation_key(row, digest)
        materialized.append(row)
    pooled = merge([], materialized)
    _validate_dataset(pooled, frozen)
    return pooled


def merge(
    existing: Iterable[Mapping[str, Any]], fresh: Iterable[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Deterministically merge exact repeats and fail on every key conflict."""
    pooled: dict[str, dict[str, Any]] = {}
    semantic: dict[tuple[str, str, float], str] = {}
    for index, source in enumerate([*existing, *fresh], 1):
        if not isinstance(source, Mapping):
            raise TypeError(f"ledger row {index} is not an object")
        row = dict(source)
        if _META_FIELDS - row.keys():
            raise ValueError("ledger observation metadata is missing")
        artifact = _artifact_of(row)
        key = str(row["_observation_key"])
        expected = observation_key(row, artifact)
        if key != expected:
            raise ValueError("ledger observation key drift")
        semantic_key = (artifact, str(row["signal_id"]), float(row["evaluation_ms"]))
        previous_key = semantic.setdefault(semantic_key, key)
        if previous_key != key:
            raise ValueError("colliding semantic observation IDs")
        if key in pooled:
            if _canonical(pooled[key]) != _canonical(row):
                raise ValueError("conflicting duplicate observation payload")
            continue
        pooled[key] = row
    return sorted(
        pooled.values(),
        key=lambda row: (
            float(row["decision_ms"]),
            str(row["_artifact_sha256"]),
            str(row["variant"]),
            str(row["signal_id"]),
            float(row["evaluation_ms"]),
        ),
    )


def _is_full_fill(row: Mapping[str, Any]) -> bool:
    return bool(
        row["sent"] is True
        and row["filled"] is True
        and row["full_fill"] is True
        and row["reason"] == "filled"
        and float(row["target_shares"]) == TARGET_SHARES
        and float(row["filled_shares"]) == TARGET_SHARES
    )


def _is_fill(row: Mapping[str, Any]) -> bool:
    return bool(
        row["sent"] is True
        and row["filled"] is True
        and float(row["filled_shares"]) > _EPS
        and row["reason"] in {"filled", "partial_fill"}
    )


def _selection_clock(
    row: Mapping[str, Any],
    _groups: Mapping[tuple[str, str, str], list[Mapping[str, Any]]],
) -> float:
    """Clock when this row's execution result first became observable."""
    if row["reason"] == "right_censored" and row.get("_artifact_end_ms") is not None:
        return float(row["_artifact_end_ms"])
    return float(row["evaluation_recv_ms"])


def _full_fills(
    rows: Iterable[Mapping[str, Any]],
    variant: str,
    latency: float,
    groups: Mapping[tuple[str, str, str], list[Mapping[str, Any]]],
) -> list[Mapping[str, Any]]:
    return sorted(
        (
            row
            for row in rows
            if row["variant"] == variant
            and float(row["evaluation_ms"]) == latency
            and _is_full_fill(row)
        ),
        key=lambda row: (
            _selection_clock(row, groups),
            str(row.get("_artifact_sha256") or ""),
            str(row["signal_id"]),
        ),
    )


def _primary(
    rows: Iterable[Mapping[str, Any]], latency: float
) -> list[Mapping[str, Any]]:
    return sorted(
        (
            row
            for row in rows
            if row["variant"] == "base"
            and float(row["evaluation_ms"]) == latency
            and _is_full_fill(row)
        ),
        key=lambda row: (
            float(row["decision_ms"]),
            str(row.get("_artifact_sha256") or ""),
            str(row["signal_id"]),
        ),
    )


def _base_statistics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    fills = [row for row in rows if _is_fill(row)]
    sample = [
        {
            **row,
            "day": _utc_day(float(row["decision_ms"])),
            "all_in_cost": float(row["total_cost"]) / float(row["filled_shares"]),
        }
        for row in fills
    ]
    shares = sum(float(row["filled_shares"]) for row in sample)
    pnl = sum(float(row["pnl"]) for row in sample)
    net_ev = pnl / shares
    lower = h_forward.daily_cluster_lower(sample, ALPHA)
    full_fill_sample = [row for row in sample if _is_full_fill(row)]
    raw_p = (
        h_replay_run._exact_pvalue(full_fill_sample) if net_ev > 0 else 1.0
    )
    return {
        "entries": len(rows),
        "sent": sum(row["sent"] is True for row in rows),
        "fills": len(sample),
        "full_fills": len(full_fill_sample),
        "days": len({str(row["day"]) for row in sample}),
        "filled_shares": shares,
        "pnl": pnl,
        "net_ev_per_share": net_ev,
        "day_cluster_lower_99": lower,
        "raw_exact_p": raw_p,
        "corrected_exact_p": min(1.0, raw_p * FAMILY_TESTS),
        "wins": sum(row["won"] is True for row in sample),
        "losses": sum(row["won"] is False for row in sample),
    }


def _control_statistics(
    rows: Sequence[Mapping[str, Any]],
    groups: Mapping[tuple[str, str, str], list[Mapping[str, Any]]],
    *,
    parent_ids: Sequence[str] = (),
) -> dict[str, Any]:
    fills = [row for row in rows if _is_fill(row)]
    full_fills = [row for row in fills if _is_full_fill(row)]
    shares = sum(float(row["filled_shares"]) for row in fills)
    pnl = sum(float(row["pnl"]) for row in fills)
    return {
        "entries": len(rows),
        "sent": sum(row["sent"] is True for row in rows),
        "fills": len(fills),
        "full_fills": len(full_fills),
        "days": len({_utc_day(float(row["decision_ms"])) for row in fills}),
        "filled_shares": shares,
        "pnl": pnl if shares else None,
        "net_ev_per_share": pnl / shares if shares else None,
        "parent_signal_ids": sorted(set(parent_ids)),
    }


def _reverse_pairs(
    base_rows: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
) -> tuple[list[tuple[Mapping[str, Any], Mapping[str, Any]]], int]:
    base_by_stratum: dict[tuple[Any, ...], list[Mapping[str, Any]]] = defaultdict(list)
    control_by_stratum: dict[tuple[Any, ...], list[Mapping[str, Any]]] = defaultdict(
        list
    )
    for row in base_rows:
        base_by_stratum[replay._matching_stratum(row)].append(row)
    for row in candidates:
        control_by_stratum[replay._matching_stratum(row)].append(row)
    pairs: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for stratum, base_group in sorted(
        base_by_stratum.items(), key=lambda item: repr(item[0])
    ):
        bases = sorted(
            base_group,
            key=lambda row: (
                float(row["decision_ms"]),
                str(row["market_id"]),
                str(row["signal_id"]),
            ),
        )
        controls = sorted(
            control_by_stratum.get(stratum, []),
            key=lambda row: (
                float(row["decision_ms"]),
                str(row["market_id"]),
                str(row["signal_id"]),
            ),
        )
        pairs.extend(zip(bases, controls, strict=False))
    return pairs, len(base_by_stratum)


def _delay_pairs(
    base_rows: Sequence[Mapping[str, Any]],
    latency: float,
    groups: Mapping[tuple[str, str, str], list[Mapping[str, Any]]],
    cutoff: float,
) -> list[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    children: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for (artifact, variant, _signal_id), signal_rows in groups.items():
        if variant != "delay_5s":
            continue
        parent_id = str(signal_rows[0]["parent_signal_id"])
        children[(artifact, parent_id)] = signal_rows
    pairs: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for parent in base_rows:
        artifact = _artifact_of(parent)
        parent_id = str(parent["signal_id"])
        child_rows = children.get((artifact, parent_id))
        if child_rows is None:
            continue
        matches = [row for row in child_rows if float(row["evaluation_ms"]) == latency]
        if len(matches) != 1:
            continue
        child = matches[0]
        if _selection_clock(child, groups) <= cutoff + _EPS:
            pairs.append((parent, child))
    return pairs


def _coverage(
    rows: Sequence[Mapping[str, Any]],
    groups: Mapping[tuple[str, str, str], list[Mapping[str, Any]]],
) -> dict[str, int]:
    full_fills = [row for row in rows if _is_full_fill(row)]
    return {
        "full_fills": len(full_fills),
        "days": len(
            {_utc_day(float(row["decision_ms"])) for row in full_fills}
        ),
    }


def _samples_at_cutoff(
    rows: Sequence[Mapping[str, Any]],
    groups: Mapping[tuple[str, str, str], list[Mapping[str, Any]]],
    cutoff: float,
) -> tuple[
    dict[str, list[Mapping[str, Any]]],
    dict[str, dict[str, list[tuple[Mapping[str, Any], Mapping[str, Any]]]]],
]:
    base: dict[str, list[Mapping[str, Any]]] = {}
    pairs: dict[
        str, dict[str, list[tuple[Mapping[str, Any], Mapping[str, Any]]]]
    ] = {}
    for latency_value in LATENCIES:
        latency = str(int(latency_value))
        base_rows = [
            row
            for row in rows
            if row["variant"] == "base"
            and float(row["evaluation_ms"]) == latency_value
            and _selection_clock(row, groups) <= cutoff + _EPS
        ]
        reverse_candidates = [
            row
            for row in rows
            if row["variant"] == "reverse_order"
            and float(row["evaluation_ms"]) == latency_value
            and _selection_clock(row, groups) <= cutoff + _EPS
        ]
        reverse, _common_strata = _reverse_pairs(base_rows, reverse_candidates)
        base[latency] = base_rows
        pairs[latency] = {
            "reverse_order": reverse,
            "delay_5s": _delay_pairs(base_rows, latency_value, groups, cutoff),
        }
    return base, pairs


def _ready_at_cutoff(
    rows: Sequence[Mapping[str, Any]],
    groups: Mapping[tuple[str, str, str], list[Mapping[str, Any]]],
    cutoff: float,
) -> bool:
    base, pairs = _samples_at_cutoff(rows, groups, cutoff)
    for latency in ("400", "500"):
        base_coverage = _coverage(base[latency], groups)
        if (
            base_coverage["full_fills"] < MINIMUM_ENTRIES
            or base_coverage["days"] < MINIMUM_DAYS
        ):
            return False
        for variant in ("reverse_order", "delay_5s"):
            controls = [control for _base, control in pairs[latency][variant]]
            coverage = _coverage(controls, groups)
            if (
                coverage["full_fills"] < MINIMUM_ENTRIES
                or coverage["days"] < MINIMUM_DAYS
            ):
                return False
    return True


def _common_cutoff(
    rows: Sequence[Mapping[str, Any]],
    groups: Mapping[tuple[str, str, str], list[Mapping[str, Any]]],
) -> float | None:
    clocks = sorted(
        {
            _selection_clock(row, groups)
            for row in rows
            if row["variant"] in VARIANTS
        }
    )
    if not clocks or not _ready_at_cutoff(rows, groups, clocks[-1]):
        return None
    low = 0
    high = len(clocks) - 1
    while low < high:
        middle = (low + high) // 2
        if _ready_at_cutoff(rows, groups, clocks[middle]):
            high = middle
        else:
            low = middle + 1
    return clocks[low]


def _comparison_statistics(
    pairs: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
) -> dict[str, Any]:
    sample: list[dict[str, Any]] = [
        {
            "day": _utc_day(float(base["decision_ms"])),
            "filled_shares": TARGET_SHARES,
            "pnl": (
                float(base["pnl"]) if _is_fill(base) else 0.0
            ) - (
                float(control["pnl"]) if _is_fill(control) else 0.0
            ),
        }
        for base, control in pairs
    ]
    pnl = sum(float(row["pnl"]) for row in sample)
    shares = sum(float(row["filled_shares"]) for row in sample)
    return {
        "pairs": len(sample),
        "days": len({str(row["day"]) for row in sample}),
        "net_ev_difference_per_share": pnl / shares,
        "day_cluster_lower_99": h_forward.daily_cluster_lower(sample, ALPHA),
    }


def evaluate(
    rows: Sequence[Mapping[str, Any]], frozen: Mapping[str, Any]
) -> dict[str, Any]:
    """Evaluate the first immutable common stopping sample."""
    materialized = [dict(row) for row in rows]
    groups = _validate_dataset(materialized, frozen)
    samples = {
        str(int(latency)): _primary(materialized, latency) for latency in LATENCIES
    }
    progress = {
        latency: {
            "entries": len(sample),
            "days": len({_utc_day(float(row["decision_ms"])) for row in sample}),
        }
        for latency, sample in samples.items()
    }
    _all_base, all_pairs = _samples_at_cutoff(materialized, groups, math.inf)
    control_progress = {
        variant: {
            latency: _coverage(
                [control for _base, control in all_pairs[latency][variant]],
                groups,
            )
            for latency in ("400", "500")
        }
        for variant in ("reverse_order", "delay_5s")
    }
    cutoff = _common_cutoff(materialized, groups)
    common = {
        "paper_only": True,
        "production_eligible": False,
        "common_cutoff_ms": cutoff,
        "required_entries_per_latency": MINIMUM_ENTRIES,
        "required_days": MINIMUM_DAYS,
        "family_tests": FAMILY_TESTS,
        "alpha": ALPHA,
        "progress": progress,
        "control_progress": control_progress,
    }
    if cutoff is None:
        return {"status": "collecting", **common}

    selected, matched_pairs = _samples_at_cutoff(materialized, groups, cutoff)
    cohort_rows: dict[tuple[str, str, str, float], Mapping[str, Any]] = {}
    for latency in ("400", "500"):
        for row in selected[latency]:
            key = (
                _artifact_of(row),
                str(row["variant"]),
                str(row["signal_id"]),
                float(row["evaluation_ms"]),
            )
            cohort_rows[key] = row
        for variant in ("reverse_order", "delay_5s"):
            for _base, control in matched_pairs[latency][variant]:
                key = (
                    _artifact_of(control),
                    str(control["variant"]),
                    str(control["signal_id"]),
                    float(control["evaluation_ms"]),
                )
                cohort_rows[key] = control
    pending = {
        "unresolved_fills": sum(
            _is_fill(row)
            and (
                row["winner"] not in {"Up", "Down"}
                or not isinstance(row["won"], bool)
                or row["pnl"] is None
            )
            for row in cohort_rows.values()
        ),
        # Kept for status-schema compatibility; it now reports only the strict
        # full-fill subset while unresolved partials are covered above.
        "unresolved_full_fills": sum(
            _is_full_fill(row)
            and (
                row["winner"] not in {"Up", "Down"}
                or not isinstance(row["won"], bool)
                or row["pnl"] is None
            )
            for row in cohort_rows.values()
        ),
        "right_censored_base": sum(
            row["variant"] == "base"
            and _selection_clock(row, groups) <= cutoff + _EPS
            and row["reason"] == "right_censored"
            for row in materialized
        ),
        "right_censored_controls": sum(
            row["variant"] != "base" and row["reason"] == "right_censored"
            for row in cohort_rows.values()
        ),
    }
    if any(pending.values()):
        return {"status": "collecting", **common, "pending": pending}

    base = {
        latency: _base_statistics(sample) for latency, sample in selected.items()
    }
    controls: dict[str, dict[str, Any]] = {}
    comparisons: dict[str, dict[str, Any]] = {}
    checks: dict[str, bool] = {}
    for latency_value in LATENCIES:
        latency = str(int(latency_value))
        base_sample = selected[latency]
        reverse_pairs = matched_pairs[latency]["reverse_order"]
        delay_pairs = matched_pairs[latency]["delay_5s"]
        reverse_rows = [control for _base, control in reverse_pairs]
        delay_rows = [control for _base, control in delay_pairs]
        parent_ids = [str(parent["signal_id"]) for parent, _child in delay_pairs]
        reverse_stats = _control_statistics(reverse_rows, groups)
        reverse_stats["common_strata"] = len(
            {replay._matching_stratum(base_row) for base_row in base_sample}
        )
        delay_stats = _control_statistics(
            delay_rows, groups, parent_ids=parent_ids
        )
        controls[latency] = {
            "reverse_order": reverse_stats,
            "delay_5s": delay_stats,
        }
        comparisons[latency] = {
            "reverse_order": _comparison_statistics(reverse_pairs),
            "delay_5s": _comparison_statistics(delay_pairs),
        }
        stats = base[latency]
        checks[f"{latency}_positive_net_ev"] = stats["net_ev_per_share"] > 0
        checks[f"{latency}_positive_day_cluster_lower"] = (
            stats["day_cluster_lower_99"] is not None
            and stats["day_cluster_lower_99"] > 0
        )
        checks[f"{latency}_corrected_exact_p"] = stats["corrected_exact_p"] < ALPHA
        for variant, control_rows in (
            ("reverse_order", reverse_rows),
            ("delay_5s", delay_rows),
        ):
            coverage = _coverage(control_rows, groups)
            checks[f"{latency}_{variant}_complete"] = (
                coverage["full_fills"] >= MINIMUM_ENTRIES
                and coverage["days"] >= MINIMUM_DAYS
            )
            lower = comparisons[latency][variant]["day_cluster_lower_99"]
            checks[f"{latency}_above_{variant}"] = (
                lower is not None and lower > 0
            )

    frozen_rows = sorted(
        (row for row in materialized if _selection_clock(row, groups) <= cutoff),
        key=lambda row: (
            _selection_clock(row, groups),
            str(row["variant"]),
            str(row["market_id"]),
            str(row["signal_id"]),
            float(row["evaluation_ms"]),
        ),
    )
    return {
        "status": "passed" if all(checks.values()) else "rejected",
        **common,
        "base": base,
        "controls": controls,
        "comparisons": comparisons,
        "checks": checks,
        "frozen_observations": len(frozen_rows),
        "frozen_rows_sha256": _canonical_sha256(
            [_ordinary_row(row) for row in frozen_rows]
        ),
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"invalid JSON constant {value}")
            ),
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"invalid JSON state: {path}") from exc
    if not isinstance(payload, dict):
        raise TypeError(f"expected JSON object: {path}")
    return payload


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                row = json.loads(
                    line,
                    parse_constant=lambda value: (_ for _ in ()).throw(
                        ValueError(f"invalid JSON constant {value}")
                    ),
                )
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON") from exc
            if not isinstance(row, dict):
                raise TypeError(f"{path}:{line_number}: expected object")
            rows.append(row)
    return rows


def _with_hash(payload: Mapping[str, Any], field: str) -> dict[str, Any]:
    result = dict(payload)
    result[field] = _canonical_sha256(result)
    return result


def _verify_hash(payload: Mapping[str, Any], field: str, label: str) -> None:
    expected = payload.get(field)
    unhashed = {key: value for key, value in payload.items() if key != field}
    if expected != _canonical_sha256(unhashed):
        raise ValueError(f"Family34 {label} hash mismatch")


def _fsync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _discard_unjournaled_staging(state: Path) -> None:
    if (state / JOURNAL).exists():
        return
    for staged_name in _STAGED_FILES.values():
        staged = state / staged_name
        staged.unlink(missing_ok=True)
        staged.with_name(staged.name + ".tmp").unlink(missing_ok=True)
    (state / f"{JOURNAL}.tmp").unlink(missing_ok=True)


def _validate_journal(
    journal: Mapping[str, Any], protocol: Mapping[str, Any]
) -> dict[str, str | None]:
    expected_fields = frozenset(
        {
            "schema",
            "strategy_id",
            "config_sha256",
            "prospective_cutoff_ms",
            "files",
            "journal_sha256",
        }
    )
    _require_exact_keys(journal, expected_fields, "update journal")
    if journal["schema"] != JOURNAL_SCHEMA:
        raise ValueError("Family34 update journal schema drift")
    _verify_hash(journal, "journal_sha256", "update journal")
    if (
        journal["strategy_id"] != protocol["strategy_id"]
        or journal["config_sha256"] != protocol["config_sha256"]
        or not _same(journal["prospective_cutoff_ms"], protocol["cutoff_ms"])
    ):
        raise ValueError("Family34 update journal freeze/config drift")
    files = journal["files"]
    if not isinstance(files, Mapping) or set(files) != set(_STAGED_FILES):
        raise ValueError("Family34 update journal file set drift")
    validated: dict[str, str | None] = {}
    for name in _STAGED_FILES:
        value = files[name]
        if value is None:
            if name != VERDICT:
                raise ValueError("Family34 update journal omits required state")
            validated[name] = None
        else:
            validated[name] = _digest(value, f"journaled {name} hash")
    return validated


def _install_journaled_state(
    state: Path,
    journal: Mapping[str, Any],
    protocol: Mapping[str, Any],
    *,
    crash_after_ledger: bool = False,
    crash_after_status: bool = False,
) -> None:
    files = _validate_journal(journal, protocol)
    for name in (LEDGER, STATUS, VERDICT):
        expected = files[name]
        target = state / name
        staged = state / _STAGED_FILES[name]
        if expected is None:
            if target.exists():
                raise ValueError("Family34 collecting update found a terminal verdict")
            staged.unlink(missing_ok=True)
        elif target.is_file() and source_race_forward._sha256(target) == expected:
            staged.unlink(missing_ok=True)
        elif staged.is_file() and source_race_forward._sha256(staged) == expected:
            os.replace(staged, target)
            _fsync_directory(state)
        else:
            raise ValueError(f"Family34 journaled {name} bytes are missing")
        if name == LEDGER and crash_after_ledger:
            raise RuntimeError("simulated crash after ledger commit")
        if name == STATUS and crash_after_status:
            raise RuntimeError("simulated crash after status commit")
    (state / JOURNAL).unlink()
    (state / f"{JOURNAL}.tmp").unlink(missing_ok=True)
    _fsync_directory(state)


def _recover_journal(state: Path, protocol: Mapping[str, Any]) -> None:
    journal_path = state / JOURNAL
    if not journal_path.exists():
        _discard_unjournaled_staging(state)
        return
    journal = _read_json(journal_path)
    _install_journaled_state(state, journal, protocol)


def _state_identity(
    protocol: Mapping[str, Any], ledger_path: Path, result: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "strategy_id": protocol["strategy_id"],
        "config_sha256": protocol["config_sha256"],
        "prospective_cutoff_ms": protocol["cutoff_ms"],
        "ledger_sha256": source_race_forward._sha256(ledger_path),
        "verdict": dict(result),
    }


def _load_state(
    state: Path, frozen: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    protocol = _protocol(frozen)
    ledger_path = state / LEDGER
    status_path = state / STATUS
    verdict_path = state / VERDICT
    if not status_path.exists():
        if ledger_path.exists() or verdict_path.exists():
            raise ValueError("Family34 state files exist without hashed status")
        return [], None
    if not ledger_path.is_file():
        raise ValueError("Family34 status exists without its ledger")
    status = _read_json(status_path)
    if status.get("schema") != STATUS_SCHEMA:
        raise ValueError("Family34 status schema drift")
    _verify_hash(status, "status_sha256", "status")
    if (
        status.get("strategy_id") != protocol["strategy_id"]
        or status.get("config_sha256") != protocol["config_sha256"]
        or not _same(status.get("prospective_cutoff_ms"), protocol["cutoff_ms"])
    ):
        raise ValueError("Family34 immutable freeze/config drift")
    if status.get("ledger_sha256") != source_race_forward._sha256(ledger_path):
        raise ValueError("Family34 status ledger hash mismatch")
    rows = _read_jsonl(ledger_path)
    _validate_dataset(rows, frozen)
    if rows != merge([], rows):
        raise ValueError("Family34 ledger ordering or duplicate drift")
    expected_result = evaluate(rows, frozen)
    expected_status_core = {
        "schema": STATUS_SCHEMA,
        **_state_identity(protocol, ledger_path, expected_result),
    }
    if {
        key: value for key, value in status.items() if key != "status_sha256"
    } != expected_status_core:
        raise ValueError("Family34 status verdict drift")
    terminal = expected_result["status"] != "collecting"
    if terminal != verdict_path.exists():
        raise ValueError("Family34 terminal verdict lifecycle mismatch")
    if terminal:
        verdict = _read_json(verdict_path)
        if verdict.get("schema") != VERDICT_SCHEMA:
            raise ValueError("Family34 verdict schema drift")
        _verify_hash(verdict, "verdict_sha256", "verdict")
        expected_core = {
            "schema": VERDICT_SCHEMA,
            **_state_identity(protocol, ledger_path, expected_result),
            "status_sha256": status["status_sha256"],
        }
        if {
            key: value for key, value in verdict.items() if key != "verdict_sha256"
        } != expected_core:
            raise ValueError("pinned Family34 verdict changed")
    return rows, status


def update(
    rows_path: str | Path,
    state_dir: str | Path,
    *,
    frozen: Mapping[str, Any],
    expected_utc_day: str,
    artifact_start_ms: float,
    artifact_end_ms: float,
    artifact_sha256: str | None = None,
    artifact_digest: str | None = None,
    _crash_after_ledger_commit: bool = False,
    _crash_after_status_commit: bool = False,
) -> dict[str, Any]:
    """Merge one complete artifact into the single immutable Family34 state."""
    protocol = _protocol(frozen)
    state = Path(state_dir)
    state.mkdir(parents=True, exist_ok=True)
    _recover_journal(state, protocol)
    existing, status = _load_state(state, frozen)
    fresh = validate_rows(
        _read_jsonl(Path(rows_path)),
        frozen,
        artifact_sha256,
        expected_utc_day=expected_utc_day,
        artifact_start_ms=artifact_start_ms,
        artifact_end_ms=artifact_end_ms,
        artifact_digest=artifact_digest,
    )
    merged = merge(existing, fresh)
    _validate_dataset(merged, frozen)
    if status is not None and status["verdict"]["status"] != "collecting":
        if merged != existing:
            raise ValueError("terminal Family34 state rejects later mutation")
        return dict(status["verdict"])

    staged_ledger = state / _STAGED_FILES[LEDGER]
    staged_status = state / _STAGED_FILES[STATUS]
    staged_verdict = state / _STAGED_FILES[VERDICT]
    h_forward._write_rows(staged_ledger, merged)
    _fsync_file(staged_ledger)
    result = evaluate(merged, frozen)
    status_core = {
        "schema": STATUS_SCHEMA,
        **_state_identity(protocol, staged_ledger, result),
    }
    next_status = _with_hash(status_core, "status_sha256")
    source_race_forward._atomic_json(staged_status, next_status)
    _fsync_file(staged_status)
    if result["status"] != "collecting":
        verdict_core = {
            "schema": VERDICT_SCHEMA,
            **_state_identity(protocol, staged_ledger, result),
            "status_sha256": next_status["status_sha256"],
        }
        source_race_forward._atomic_json(
            staged_verdict, _with_hash(verdict_core, "verdict_sha256")
        )
        _fsync_file(staged_verdict)
        verdict_hash: str | None = source_race_forward._sha256(staged_verdict)
    else:
        staged_verdict.unlink(missing_ok=True)
        staged_verdict.with_name(staged_verdict.name + ".tmp").unlink(missing_ok=True)
        verdict_hash = None

    _fsync_directory(state)
    journal_core = {
        "schema": JOURNAL_SCHEMA,
        "strategy_id": protocol["strategy_id"],
        "config_sha256": protocol["config_sha256"],
        "prospective_cutoff_ms": protocol["cutoff_ms"],
        "files": {
            LEDGER: source_race_forward._sha256(staged_ledger),
            STATUS: source_race_forward._sha256(staged_status),
            VERDICT: verdict_hash,
        },
    }
    journal = _with_hash(journal_core, "journal_sha256")
    journal_path = state / JOURNAL
    source_race_forward._atomic_json(journal_path, journal)
    _fsync_file(journal_path)
    _fsync_directory(state)
    _install_journaled_state(
        state,
        journal,
        protocol,
        crash_after_ledger=_crash_after_ledger_commit,
        crash_after_status=_crash_after_status_commit,
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rows")
    parser.add_argument("state_dir")
    parser.add_argument("--config", required=True)
    parser.add_argument("--artifact-sha256", required=True)
    parser.add_argument("--expected-utc-day", required=True)
    parser.add_argument("--artifact-start-ms", required=True, type=float)
    parser.add_argument("--artifact-end-ms", required=True, type=float)
    args = parser.parse_args()
    frozen = _read_json(Path(args.config))
    result = update(
        args.rows,
        args.state_dir,
        frozen=frozen,
        artifact_sha256=args.artifact_sha256,
        expected_utc_day=args.expected_utc_day,
        artifact_start_ms=args.artifact_start_ms,
        artifact_end_ms=args.artifact_end_ms,
    )
    print(json.dumps(result, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
