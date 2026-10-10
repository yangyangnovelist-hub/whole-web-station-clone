"""Crash-safe, immutable forward admission for the frozen basis-wedge family."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import time
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from itertools import groupby
from pathlib import Path
from typing import Any

import basis_wedge_run as replay
import h_forward
import h_replay_run

VARIANTS = replay.VARIANTS
CONTROL_VARIANTS = replay.CONTROL_VARIANTS
LEDGER = "observations.jsonl"
STATUS = "status.json"
VERDICT = "verdict.json"
JOURNAL = "admission-journal.json"
STAGING = "pending-day.json"
DAYS = "days"
_SCHEMA = "basis-wedge-forward-v1"
_DAY_SCHEMA = "basis-wedge-day-v1"
_EPS = 1e-9
ROOT = Path(__file__).resolve().parent
PROTOCOL_FREEZE = ROOT / "forward" / "basis-wedge-freeze.json"
EVALUATOR_FREEZE = ROOT / "forward" / "basis-wedge-evaluator-freeze.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_rows(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
    temporary.replace(path)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON artifact: {path}") from exc
    if not isinstance(payload, dict):
        raise TypeError(f"expected JSON object: {path}")
    return payload


def _load_hashed_json(path: Path, schema: str) -> tuple[dict[str, Any], str]:
    digest = _sha256(path)
    expected = path.with_suffix(".sha256").read_text(encoding="ascii").strip()
    if len(expected) != 64 or digest != expected:
        raise ValueError(f"freeze checksum drift: {path.name}")
    payload = _load_json(path)
    if payload.get("schema") != schema:
        raise ValueError(f"freeze schema drift: {path.name}")
    for name, dependency_sha in payload.get("dependencies_sha256", {}).items():
        relative = Path(str(name))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("freeze dependency path is invalid")
        dependency = ROOT / relative
        if not dependency.is_file() or _sha256(dependency) != dependency_sha:
            raise ValueError(f"freeze dependency drift: {name}")
    return payload, digest


def load_freezes(
    protocol_path: str | Path = PROTOCOL_FREEZE,
    evaluator_path: str | Path = EVALUATOR_FREEZE,
) -> tuple[dict[str, Any], str, dict[str, Any], str]:
    """Load the exact preregistered protocol and executable evaluator chain."""
    protocol, protocol_sha = _load_hashed_json(
        Path(protocol_path),
        "basis-wedge-protocol-v2",
    )
    evaluator, evaluator_sha = _load_hashed_json(
        Path(evaluator_path),
        "basis-wedge-evaluator-freeze-v1",
    )
    if (
        evaluator.get("protocol_sha256") != protocol_sha
        or evaluator.get("strategy_id") != protocol.get("strategy_id")
        or evaluator.get("holdout_start") != protocol.get("holdout_start")
    ):
        raise ValueError("basis-wedge evaluator/protocol identity drift")
    return protocol, protocol_sha, evaluator, evaluator_sha


def _utc_day(timestamp_ms: float) -> str:
    try:
        return (
            datetime.fromtimestamp(timestamp_ms / 1_000, tz=timezone.utc)
            .date()
            .isoformat()
        )
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("invalid receipt clock") from exc


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid UTC day: {value!r}") from exc


def _holdout_day(frozen: Mapping[str, Any]) -> date:
    return datetime.fromtimestamp(
        float(frozen["holdout_start_ms"]) / 1_000,
        tz=timezone.utc,
    ).date()


def _finite_number(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} is invalid") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} is non-finite")
    return number


def _same_number(left: Any, right: Any, *, tolerance: float = 1e-9) -> bool:
    try:
        return math.isclose(float(left), float(right), rel_tol=0, abs_tol=tolerance)
    except (TypeError, ValueError):
        return False


def _rounded_fee(levels: Iterable[tuple[float, float]], rate: float) -> float:
    decimal_rate = Decimal(str(rate))
    raw = sum(
        (
            Decimal(str(shares))
            * decimal_rate
            * Decimal(str(price))
            * (Decimal(1) - Decimal(str(price)))
        )
        for price, shares in levels
    )
    return float(raw.quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP))


def _validate_row(
    row: Mapping[str, Any],
    *,
    day: str,
    frozen: Mapping[str, Any],
    index: int,
) -> None:
    prefix = f"row {index}"
    variant = str(row.get("variant") or "")
    if variant not in VARIANTS:
        raise ValueError(f"{prefix} has an unfrozen variant")
    if row.get("paper_only") is not True:
        raise ValueError(f"{prefix} is not paper-only")
    if row.get("ordering_clock") != "local_receipt_ms" or (
        row.get("match_book_clock") != "local_receipt_ms"
    ):
        raise ValueError(f"{prefix} clock drift")
    try:
        decision_ms = _finite_number(row["decision_recv_ms"], f"{prefix} decision time")
        signal_ms = _finite_number(row["signal_recv_ms"], f"{prefix} signal time")
        evaluation_ms = _finite_number(row["evaluation_ms"], f"{prefix} evaluation")
        evaluation_recv_ms = _finite_number(
            row["evaluation_recv_ms"],
            f"{prefix} evaluation time",
        )
    except KeyError as exc:
        raise ValueError(f"{prefix} timing is invalid") from exc
    if signal_ms > decision_ms + _EPS:
        raise ValueError(f"{prefix} decision predates its signal")
    if decision_ms < float(frozen["holdout_start_ms"]):
        raise ValueError(f"{prefix} is before the frozen cutoff")
    if _utc_day(decision_ms) != day:
        raise ValueError(f"{prefix} receipt day mismatch")
    allowed = {float(value) for value in frozen["execution"]["evaluation_ms"]}
    if evaluation_ms not in allowed or not math.isclose(
        evaluation_recv_ms,
        decision_ms + evaluation_ms,
        rel_tol=0,
        abs_tol=1e-6,
    ):
        raise ValueError(f"{prefix} evaluation timing drift")
    scheduled_ms = _finite_number(
        row.get("scheduled_decision_recv_ms", decision_ms),
        f"{prefix} scheduled decision time",
    )
    if variant == "time_shift":
        expected_shift = signal_ms + replay.TIME_SHIFT_MS
        if not math.isclose(scheduled_ms, expected_shift, rel_tol=0, abs_tol=1e-6):
            raise ValueError(f"{prefix} shifted decision timing drift")
        censored = row.get("sent") is False and row.get("reason") == (
            "censored_before_shifted_decision"
        )
        expected_decision = signal_ms if censored else scheduled_ms
        if not math.isclose(decision_ms, expected_decision, rel_tol=0, abs_tol=1e-6):
            raise ValueError(f"{prefix} shifted decision timing drift")
    elif not math.isclose(scheduled_ms, decision_ms, rel_tol=0, abs_tol=1e-6):
        raise ValueError(f"{prefix} scheduled decision timing drift")
    decision_book_ms = row.get("decision_book_recv_ms")
    if (
        decision_book_ms is not None
        and _finite_number(
            decision_book_ms,
            f"{prefix} decision book time",
        )
        > decision_ms + _EPS
    ):
        raise ValueError(f"{prefix} uses a future decision book")
    book_recv_ms = row.get("book_recv_ms")
    if (
        book_recv_ms is not None
        and _finite_number(
            book_recv_ms,
            f"{prefix} book time",
        )
        > evaluation_recv_ms + _EPS
    ):
        raise ValueError(f"{prefix} uses a future book")
    if not str(row.get("signal_id") or "") or not str(row.get("market_id") or ""):
        raise ValueError(f"{prefix} identity is missing")
    if not isinstance(row.get("sent"), bool) or not isinstance(row.get("filled"), bool):
        raise TypeError(f"{prefix} send/fill flags are invalid")

    try:
        shares = _finite_number(
            row.get("filled_shares") or 0.0, f"{prefix} filled shares"
        )
        target = _finite_number(row["target_shares"], f"{prefix} target shares")
        fee_rate = _finite_number(row["fee_rate"], f"{prefix} fee rate")
        fixed_limit = _finite_number(row["fixed_limit"], f"{prefix} frozen limit")
    except KeyError as exc:
        raise ValueError(f"{prefix} share or execution quantity is invalid") from exc
    frozen_target = float(frozen["execution"]["minimum_full_fill_shares"])
    frozen_fee_rate = float(frozen["execution"]["taker_fee_rate"])
    if not math.isclose(target, frozen_target, rel_tol=0, abs_tol=_EPS):
        raise ValueError(f"{prefix} target shares drift")
    if not math.isclose(fee_rate, frozen_fee_rate, rel_tol=0, abs_tol=_EPS):
        raise ValueError(f"{prefix} fee rate drift")
    if shares < 0 or not 0 <= fixed_limit < 1:
        raise ValueError(f"{prefix} share quantity is impossible")
    levels_payload = row.get("fill_levels")
    if not isinstance(levels_payload, list):
        raise TypeError(f"{prefix} fill levels are missing")
    if not row["filled"]:
        forbidden = (
            "fill_vwap",
            "fee",
            "total_cost",
            "all_in_cost",
            "pnl_per_share",
            "pnl",
        )
        if (
            shares > _EPS
            or levels_payload
            or bool(row.get("full_fill"))
            or any(row.get(name) is not None for name in forbidden)
        ):
            raise ValueError(f"{prefix} unfilled economics are inconsistent")
        return
    if row["sent"] is not True or not _EPS < shares <= target + _EPS:
        raise ValueError(f"{prefix} filled quantity is impossible")
    full = bool(row.get("full_fill"))
    expected_full = shares + _EPS >= target
    if full != expected_full or str(row.get("reason")) != (
        "filled" if full else "partial_fill"
    ):
        raise ValueError(f"{prefix} full/partial status is inconsistent")
    if row.get("winner") not in ("Up", "Down") or row.get("buy_side") not in (
        "up",
        "down",
    ):
        raise ValueError(f"{prefix} has an unresolved fill")
    won = str(row["winner"]).lower() == str(row["buy_side"])
    try:
        fill_vwap = _finite_number(row["fill_vwap"], f"{prefix} fill VWAP")
        fee = _finite_number(row["fee"], f"{prefix} fee")
        all_in_cost = float(row["all_in_cost"])
        total_cost = float(row["total_cost"])
        pnl_per_share = float(row["pnl_per_share"])
        pnl = float(row["pnl"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{prefix} fill economics are invalid") from exc
    values = (fill_vwap, fee, all_in_cost, total_cost, pnl_per_share, pnl)
    if not all(math.isfinite(value) for value in values) or not 0 < all_in_cost < 1:
        raise ValueError(f"{prefix} fill economics are impossible")
    levels: list[tuple[float, float]] = []
    for level in levels_payload:
        if not isinstance(level, Mapping):
            raise TypeError(f"{prefix} fill levels are invalid")
        price = _finite_number(level.get("price"), f"{prefix} fill level price")
        level_shares = _finite_number(
            level.get("shares"), f"{prefix} fill level shares"
        )
        if not 0 < price <= fixed_limit + _EPS or level_shares <= 0:
            raise ValueError(f"{prefix} fill levels violate the frozen limit")
        levels.append((price, level_shares))
    level_shares = sum(level[1] for level in levels)
    notional = sum(price * level_size for price, level_size in levels)
    expected_fee = _rounded_fee(levels, fee_rate)
    if (
        not levels
        or not math.isclose(level_shares, shares, rel_tol=0, abs_tol=1e-8)
        or not math.isclose(fill_vwap, notional / shares, rel_tol=0, abs_tol=1e-9)
    ):
        raise ValueError(f"{prefix} fill levels do not reproduce the fill VWAP")
    if not math.isclose(fee, expected_fee, rel_tol=0, abs_tol=5e-9):
        raise ValueError(f"{prefix} fee does not match the frozen formula")
    expected_total_cost = notional + expected_fee
    expected_per_share = 1.0 - all_in_cost if won else -all_in_cost
    if (
        row.get("won") is not won
        or not math.isclose(
            total_cost,
            expected_total_cost,
            rel_tol=0,
            abs_tol=1e-8,
        )
        or not math.isclose(
            all_in_cost,
            expected_total_cost / shares,
            rel_tol=0,
            abs_tol=1e-9,
        )
        or not math.isclose(
            pnl_per_share,
            expected_per_share,
            rel_tol=0,
            abs_tol=1e-9,
        )
        or not math.isclose(
            pnl,
            expected_per_share * shares,
            rel_tol=0,
            abs_tol=1e-8,
        )
    ):
        raise ValueError(f"{prefix} fill PnL is inconsistent")


_PAIR_FIELDS = (
    "market_id",
    "parent_signal_id",
    "signal_recv_ms",
    "decision_recv_ms",
    "scheduled_decision_recv_ms",
    "decision_book_recv_ms",
    "basis_direction",
    "buy_side",
    "fair",
    "decision_vwap",
    "fixed_limit",
    "signal_fee_per_share",
    "signal_net_edge",
    "target_shares",
    "fee_rate",
    "sent",
    "signal",
)


def _canonical_value(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _validate_signal_pair(rows: list[dict[str, Any]]) -> None:
    ordered = sorted(rows, key=lambda row: float(row["evaluation_ms"]))
    canonical = ordered[0]
    signal = canonical.get("signal")
    if not isinstance(signal, Mapping):
        raise TypeError("signal payload is missing")
    if "fixed_limit" in signal and not _same_number(
        canonical["fixed_limit"],
        signal["fixed_limit"],
    ):
        raise ValueError("frozen limit differs from the immutable signal")
    for row in ordered[1:]:
        if any(
            _canonical_value(row.get(field)) != _canonical_value(canonical.get(field))
            for field in _PAIR_FIELDS
        ):
            raise ValueError(
                "paired decision fields differ across evaluation latencies"
            )


def _validate_matched_controls(
    grouped_rows: Mapping[tuple[str, str], list[dict[str, Any]]],
) -> bool:
    base = {
        signal_id: rows[0]
        for (variant, signal_id), rows in grouped_rows.items()
        if variant == replay.BASE_VARIANT
    }
    children: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for (variant, _signal_id), rows in grouped_rows.items():
        parent = rows[0].get("parent_signal_id")
        if variant in {"direction_reversal", "time_shift"}:
            if not isinstance(parent, str) or parent not in base:
                raise ValueError("matched control has an orphan parent signal")
            children.setdefault((variant, parent), []).append(rows[0])
        elif parent is not None:
            raise ValueError("independent signal unexpectedly has a parent")
    complete = True
    for signal_id, parent in base.items():
        for variant in ("direction_reversal", "time_shift"):
            matches = children.get((variant, signal_id), [])
            if not matches:
                complete = False
                continue
            if len(matches) != 1:
                raise ValueError(f"base signal has duplicate {variant} controls")
            child = matches[0]
            if (
                child["market_id"] != parent["market_id"]
                or not _same_number(child["signal_recv_ms"], parent["signal_recv_ms"])
                or child["basis_direction"] != parent["basis_direction"]
            ):
                raise ValueError(
                    "matched control identity differs from its base signal"
                )
            if variant == "direction_reversal":
                if (
                    child["buy_side"] == parent["buy_side"]
                    or child["buy_side"] != parent["basis_direction"]
                    or not _same_number(child["fair"], 1.0 - float(parent["fair"]))
                    or not _same_number(
                        child["decision_recv_ms"], parent["decision_recv_ms"]
                    )
                ):
                    raise ValueError("direction-reversal control semantics drift")
            else:
                if (
                    child["buy_side"] != parent["buy_side"]
                    or not _same_number(child["fair"], parent["fair"])
                    or not _same_number(child["fixed_limit"], parent["fixed_limit"])
                    or not _same_number(
                        child["scheduled_decision_recv_ms"],
                        float(parent["decision_recv_ms"]) + replay.TIME_SHIFT_MS,
                        tolerance=1e-6,
                    )
                ):
                    raise ValueError("time-shift control semantics drift")
                if child.get("reason") == "censored_before_shifted_decision":
                    complete = False
    return complete


def _group_signals(
    rows: Iterable[dict[str, Any]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(
            (str(row["variant"]), str(row["signal_id"])),
            [],
        ).append(row)
    return grouped


def _validate_day(
    path: Path,
    *,
    frozen: Mapping[str, Any],
    protocol_sha256: str,
    evaluator_sha256: str,
    now_ms: float,
) -> dict[str, Any]:
    payload = _load_json(path)
    if payload.get("schema") != _DAY_SCHEMA or payload.get("complete") is not True:
        raise ValueError("day artifact is incomplete or has schema drift")
    if payload.get("collector_region") != frozen["collector_region"]:
        raise ValueError("day collector region drift")
    if payload.get("protocol_sha256") != protocol_sha256:
        raise ValueError("day protocol hash drift")
    if payload.get("evaluator_sha256") != evaluator_sha256:
        raise ValueError("day evaluator hash drift")
    if not str(payload.get("artifact_sha256") or ""):
        raise ValueError("day source artifact hash is missing")
    day = str(payload.get("day") or "")
    day_value = _date(day)
    if day_value < _holdout_day(frozen):
        raise ValueError("day is before the frozen cutoff")
    next_midnight_ms = (
        datetime.combine(
            day_value + timedelta(days=1),
            datetime.min.time(),
            tzinfo=timezone.utc,
        ).timestamp()
        * 1_000
    )
    if now_ms + _EPS < next_midnight_ms:
        raise ValueError("UTC day is not closed")
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise TypeError("day rows are missing")
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise TypeError(f"row {index} is not an object")
        _validate_row(row, day=day, frozen=frozen, index=index)
    expected_evaluations = {
        float(value) for value in frozen["execution"]["evaluation_ms"]
    }
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (str(row["variant"]), str(row["signal_id"]))
        signal_rows = grouped.setdefault(key, [])
        evaluation = float(row["evaluation_ms"])
        if any(
            float(existing["evaluation_ms"]) == evaluation for existing in signal_rows
        ):
            raise ValueError("duplicate signal evaluation")
        signal_rows.append(row)
    for signal_rows in grouped.values():
        if {float(row["evaluation_ms"]) for row in signal_rows} != expected_evaluations:
            raise ValueError("signal is missing a frozen evaluation latency")
        _validate_signal_pair(signal_rows)
    return payload


def _primary(
    rows: Iterable[dict[str, Any]],
    latency: float,
    target_shares: float,
) -> list[dict[str, Any]]:
    return sorted(
        (
            row
            for row in rows
            if row["variant"] == replay.BASE_VARIANT
            and float(row["evaluation_ms"]) == latency
            and row.get("filled")
            and row.get("full_fill")
            and row.get("winner") in ("Up", "Down")
            and float(row.get("filled_shares") or 0.0) + _EPS >= target_shares
        ),
        key=lambda row: (float(row["decision_recv_ms"]), str(row["signal_id"])),
    )


def _common_cutoff(
    samples: Mapping[str, list[dict[str, Any]]],
    *,
    minimum_fills: int,
    minimum_days: int,
) -> float | None:
    events = []
    for latency, rows in samples.items():
        events.extend(
            (
                float(row["decision_recv_ms"]),
                latency,
                _utc_day(float(row["decision_recv_ms"])),
            )
            for row in rows
        )
    counts = {latency: 0 for latency in samples}
    days = {latency: set() for latency in samples}
    for timestamp, group in groupby(sorted(events), key=lambda item: item[0]):
        for _timestamp, latency, day in group:
            counts[latency] += 1
            days[latency].add(day)
        if all(
            counts[latency] >= minimum_fills and len(days[latency]) >= minimum_days
            for latency in samples
        ):
            return timestamp
    return None


def _variant_statistics(
    rows: Iterable[dict[str, Any]],
    *,
    cutoff_ms: float | None = None,
) -> dict[str, dict[str, Any]]:
    retained = list(rows)
    output: dict[str, dict[str, Any]] = {}
    for latency in replay.EVALUATION_MS:
        sample = [
            row
            for row in retained
            if float(row["evaluation_ms"]) == latency
            and (
                cutoff_ms is None or float(row["decision_recv_ms"]) <= cutoff_ms + _EPS
            )
        ]
        fills = [row for row in sample if row.get("filled")]
        full = [row for row in fills if row.get("full_fill")]
        partial = [row for row in fills if not row.get("full_fill")]
        shares = sum(float(row["filled_shares"]) for row in fills)
        pnl = sum(float(row["pnl"]) for row in fills)
        output[str(int(latency))] = {
            "signals": len(sample),
            "sent": sum(bool(row["sent"]) for row in sample),
            "fills": len(fills),
            "full_fills": len(full),
            "partial_fills": len(partial),
            "filled_shares": shares,
            "pnl": pnl,
            "net_ev_per_share": pnl / shares if shares else None,
            "wins": sum(bool(row["won"]) for row in fills),
            "losses": sum(not bool(row["won"]) for row in fills),
            "reasons": dict(Counter(str(row["reason"]) for row in sample)),
        }
    return output


def _base_statistics(
    rows: list[dict[str, Any]],
    *,
    alpha: float,
    family_tests: int,
) -> dict[str, Any]:
    sample = [{**row, "day": _utc_day(float(row["decision_recv_ms"]))} for row in rows]
    shares = sum(float(row["filled_shares"]) for row in sample)
    pnl = sum(float(row["pnl"]) for row in sample)
    net_ev = pnl / shares
    lower = h_forward.daily_cluster_lower(sample, alpha)
    raw_p = h_replay_run._exact_pvalue(sample) if net_ev > 0 else 1.0
    corrected = min(1.0, raw_p * family_tests)
    return {
        "fills": len(sample),
        "days": len({row["day"] for row in sample}),
        "filled_shares": shares,
        "pnl": pnl,
        "net_ev_per_share": net_ev,
        "day_cluster_lower": lower,
        "raw_exact_p": raw_p,
        "corrected_exact_p": corrected,
        "wins": sum(bool(row["won"]) for row in sample),
        "losses": sum(not bool(row["won"]) for row in sample),
    }


def evaluate(rows: list[dict[str, Any]], frozen: Mapping[str, Any]) -> dict[str, Any]:
    statistics = frozen["statistics"]
    minimum_fills = int(statistics["minimum_full_fills_per_latency"])
    minimum_days = int(statistics["minimum_utc_days"])
    target = float(frozen["execution"]["minimum_full_fill_shares"])
    samples = {
        str(int(latency)): _primary(rows, latency, target)
        for latency in replay.EVALUATION_MS
    }
    progress = {
        latency: {
            "full_fills": len(sample),
            "days": len({_utc_day(float(row["decision_recv_ms"])) for row in sample}),
        }
        for latency, sample in samples.items()
    }
    variants = {
        variant: _variant_statistics(
            (row for row in rows if row["variant"] == variant),
        )
        for variant in VARIANTS
    }
    cutoff = _common_cutoff(
        samples,
        minimum_fills=minimum_fills,
        minimum_days=minimum_days,
    )
    if cutoff is None:
        return {
            "status": "collecting",
            "common_cutoff_ms": None,
            "required_fills_per_latency": minimum_fills,
            "required_days": minimum_days,
            "progress": progress,
            "variants": variants,
        }

    alpha = float(statistics["one_sided_alpha"])
    family_tests = int(statistics["family_tests"])
    base = {
        latency: _base_statistics(
            [row for row in sample if float(row["decision_recv_ms"]) <= cutoff + _EPS],
            alpha=alpha,
            family_tests=family_tests,
        )
        for latency, sample in samples.items()
    }
    checks: dict[str, bool] = {}
    for latency, stats in base.items():
        checks[f"{latency}_positive_net_ev"] = stats["net_ev_per_share"] > 0
        checks[f"{latency}_positive_day_cluster_lower"] = (
            stats["day_cluster_lower"] is not None and stats["day_cluster_lower"] > 0
        )
        checks[f"{latency}_corrected_exact_p"] = stats["corrected_exact_p"] < alpha
    controls = {
        variant: _variant_statistics(
            (row for row in rows if row["variant"] == variant),
            cutoff_ms=cutoff,
        )
        for variant in CONTROL_VARIANTS
    }
    checks["all_controls_observed"] = all(
        any(
            row["variant"] == variant
            and float(row["decision_recv_ms"]) <= cutoff + _EPS
            and row.get("reason") != "censored_before_shifted_decision"
            for row in rows
        )
        for variant in CONTROL_VARIANTS
    )
    checks["matched_controls_complete"] = _validate_matched_controls(
        _group_signals(rows),
    )
    return {
        "status": "passed" if all(checks.values()) else "rejected",
        "common_cutoff_ms": cutoff,
        "alpha": alpha,
        "family_tests": family_tests,
        "base": base,
        "controls": controls,
        "checks": checks,
        "progress": progress,
        "variants": variants,
    }


def _day_files(state: Path) -> list[Path]:
    directory = state / DAYS
    return sorted(directory.glob("*.json")) if directory.is_dir() else []


def _validate_status(
    state: Path,
    *,
    protocol_sha256: str,
    evaluator_sha256: str,
    allow_pending_day: str | None = None,
) -> dict[str, Any] | None:
    status_path = state / STATUS
    if not status_path.exists():
        if (state / LEDGER).exists() or (
            _day_files(state) and allow_pending_day is None
        ):
            raise ValueError("forward files exist without a hashed status")
        return None
    payload = _load_json(status_path)
    if payload.get("schema") != _SCHEMA:
        raise ValueError("forward status schema drift")
    if payload.get("protocol_sha256") != protocol_sha256:
        raise ValueError("forward protocol hash drift")
    if payload.get("evaluator_sha256") != evaluator_sha256:
        raise ValueError("forward evaluator hash drift")
    ledger = state / LEDGER
    if not ledger.is_file() or payload.get("ledger_sha256") != _sha256(ledger):
        raise ValueError("forward ledger hash mismatch")
    indexed = {
        str(item["day"]): str(item["sha256"]) for item in payload.get("days", [])
    }
    files = {path.stem: path for path in _day_files(state)}
    allowed_files = set(indexed)
    if allow_pending_day is not None:
        allowed_files.add(allow_pending_day)
    if set(files) != allowed_files:
        raise ValueError("forward day index mismatch")
    for day, expected in indexed.items():
        if _sha256(files[day]) != expected:
            raise ValueError("forward immutable day hash mismatch")
    verdict_path = state / VERDICT
    terminal_status = payload.get("verdict", {}).get("status") != "collecting"
    if terminal_status and not verdict_path.exists():
        raise ValueError("terminal verdict is missing")
    if not terminal_status and verdict_path.exists():
        raise ValueError("collecting state has a terminal verdict")
    if verdict_path.exists():
        terminal = _load_json(verdict_path)
        if terminal != payload:
            raise ValueError("terminal forward verdict drift")
    return payload


def _validate_status_for_recovery(
    state: Path,
    *,
    protocol_sha256: str,
    evaluator_sha256: str,
    pending_day: str,
) -> dict[str, Any] | None:
    """Validate immutable inputs while allowing a partially replaced ledger/status."""
    status_path = state / STATUS
    if not status_path.exists():
        files = {path.stem for path in _day_files(state)}
        if files not in (set(), {pending_day}):
            raise ValueError("forward day index mismatch during recovery")
        if (state / VERDICT).exists():
            raise ValueError("terminal verdict exists without status")
        return None
    payload = _load_json(status_path)
    if payload.get("schema") != _SCHEMA:
        raise ValueError("forward status schema drift")
    if payload.get("protocol_sha256") != protocol_sha256:
        raise ValueError("forward protocol hash drift")
    if payload.get("evaluator_sha256") != evaluator_sha256:
        raise ValueError("forward evaluator hash drift")
    indexed = {
        str(item["day"]): str(item["sha256"]) for item in payload.get("days", [])
    }
    files = {path.stem: path for path in _day_files(state)}
    if set(files) not in (set(indexed), {*indexed, pending_day}):
        raise ValueError("forward day index mismatch during recovery")
    for day, expected in indexed.items():
        if day == pending_day and _sha256(files[day]) != expected:
            # A newly committed status may already index the pending day.
            raise ValueError("forward immutable day hash mismatch")
        if day != pending_day and _sha256(files[day]) != expected:
            raise ValueError("forward immutable day hash mismatch")
    verdict_path = state / VERDICT
    if verdict_path.exists() and _load_json(verdict_path) != payload:
        raise ValueError("terminal forward verdict drift")
    return payload


def _read_all_days(
    state: Path,
    *,
    frozen: Mapping[str, Any],
    protocol_sha256: str,
    evaluator_sha256: str,
    now_ms: float,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    rows: list[dict[str, Any]] = []
    index: list[dict[str, str]] = []
    expected = _holdout_day(frozen)
    for path in _day_files(state):
        payload = _validate_day(
            path,
            frozen=frozen,
            protocol_sha256=protocol_sha256,
            evaluator_sha256=evaluator_sha256,
            now_ms=now_ms,
        )
        actual = _date(str(payload["day"]))
        if actual != expected:
            raise ValueError(
                f"expected day {expected.isoformat()}, got {actual.isoformat()}"
            )
        expected += timedelta(days=1)
        rows.extend(payload["rows"])
        index.append({"day": actual.isoformat(), "sha256": _sha256(path)})
    rows.sort(
        key=lambda row: (
            float(row["decision_recv_ms"]),
            str(row["variant"]),
            str(row["signal_id"]),
            float(row["evaluation_ms"]),
        )
    )
    _validate_matched_controls(_group_signals(rows))
    return rows, index


def _rebuild(
    state: Path,
    *,
    frozen: Mapping[str, Any],
    protocol_sha256: str,
    evaluator_sha256: str,
    now_ms: float,
    _crash_after_ledger_commit: bool = False,
    _crash_after_status_commit: bool = False,
) -> dict[str, Any]:
    rows, day_index = _read_all_days(
        state,
        frozen=frozen,
        protocol_sha256=protocol_sha256,
        evaluator_sha256=evaluator_sha256,
        now_ms=now_ms,
    )
    ledger = state / LEDGER
    _write_rows(ledger, rows)
    if _crash_after_ledger_commit:
        raise RuntimeError("simulated crash after ledger commit")
    result = evaluate(rows, frozen)
    payload = {
        "schema": _SCHEMA,
        "strategy_id": frozen["strategy_id"],
        "protocol_sha256": protocol_sha256,
        "evaluator_sha256": evaluator_sha256,
        "days": day_index,
        "ledger_sha256": _sha256(ledger),
        "verdict": result,
    }
    _atomic_json(state / STATUS, payload)
    if _crash_after_status_commit:
        raise RuntimeError("simulated crash after status commit")
    if result["status"] != "collecting":
        _atomic_json(state / VERDICT, payload)
    return result


def _recover_journal(
    state: Path,
    *,
    frozen: Mapping[str, Any],
    protocol_sha256: str,
    evaluator_sha256: str,
    now_ms: float,
) -> dict[str, Any]:
    journal_path = state / JOURNAL
    journal = _load_json(journal_path)
    if journal.get("schema") != _SCHEMA or (
        journal.get("protocol_sha256") != protocol_sha256
        or journal.get("evaluator_sha256") != evaluator_sha256
    ):
        raise ValueError("admission journal hash drift")
    day = str(journal.get("day") or "")
    expected_sha = str(journal.get("day_sha256") or "")
    _validate_status_for_recovery(
        state,
        protocol_sha256=protocol_sha256,
        evaluator_sha256=evaluator_sha256,
        pending_day=day,
    )
    destination = state / DAYS / f"{day}.json"
    staging = state / STAGING
    if destination.exists():
        if _sha256(destination) != expected_sha:
            raise ValueError("journaled immutable day hash mismatch")
    elif staging.exists() and _sha256(staging) == expected_sha:
        staging.replace(destination)
    else:
        raise ValueError("journaled day bytes are missing")
    result = _rebuild(
        state,
        frozen=frozen,
        protocol_sha256=protocol_sha256,
        evaluator_sha256=evaluator_sha256,
        now_ms=now_ms,
    )
    journal_path.unlink()
    staging.unlink(missing_ok=True)
    return result


def update(
    day_result: str | Path,
    state_dir: str | Path,
    *,
    frozen: Mapping[str, Any] | None = None,
    protocol_sha256: str | None = None,
    evaluator_sha256: str | None = None,
    now_ms: float | None = None,
    _crash_after_day_commit: bool = False,
    _crash_after_ledger_commit: bool = False,
    _crash_after_status_commit: bool = False,
    _allow_test_freeze: bool = False,
) -> dict[str, Any]:
    """Admit one closed UTC day or recover an interrupted immutable commit."""
    if _allow_test_freeze:
        if frozen is None or protocol_sha256 is None or evaluator_sha256 is None:
            raise ValueError("test freeze override requires complete identity")
    else:
        loaded, loaded_protocol_sha, _evaluator, loaded_evaluator_sha = load_freezes()
        if frozen is not None and dict(frozen) != loaded:
            raise ValueError("caller protocol freeze differs from the hashed freeze")
        if protocol_sha256 is not None and protocol_sha256 != loaded_protocol_sha:
            raise ValueError("caller protocol hash differs from the hashed freeze")
        if evaluator_sha256 is not None and evaluator_sha256 != loaded_evaluator_sha:
            raise ValueError("caller evaluator hash differs from the hashed freeze")
        frozen = loaded
        protocol_sha256 = loaded_protocol_sha
        evaluator_sha256 = loaded_evaluator_sha
    assert frozen is not None
    assert protocol_sha256 is not None
    assert evaluator_sha256 is not None
    now = time.time() * 1_000 if now_ms is None else float(now_ms)
    state = Path(state_dir)
    (state / DAYS).mkdir(parents=True, exist_ok=True)
    if (state / JOURNAL).exists():
        _recover_journal(
            state,
            frozen=frozen,
            protocol_sha256=protocol_sha256,
            evaluator_sha256=evaluator_sha256,
            now_ms=now,
        )
    status = _validate_status(
        state,
        protocol_sha256=protocol_sha256,
        evaluator_sha256=evaluator_sha256,
    )
    source = Path(day_result)
    preview = _load_json(source)
    preview_day = str(preview.get("day") or "")
    _date(preview_day)
    incoming_sha = _sha256(source)
    destination = state / DAYS / f"{preview_day}.json"
    if destination.exists():
        if _sha256(destination) != incoming_sha:
            raise ValueError("immutable day cannot be replaced")
        if status is None:
            raise ValueError("immutable day exists without status")
        return status["verdict"]
    incoming = _validate_day(
        source,
        frozen=frozen,
        protocol_sha256=protocol_sha256,
        evaluator_sha256=evaluator_sha256,
        now_ms=now,
    )
    day = str(incoming["day"])
    destination = state / DAYS / f"{day}.json"
    if status is not None and status["verdict"]["status"] != "collecting":
        raise ValueError("terminal forward verdict cannot accept another day")
    indexed_days = (
        [str(item["day"]) for item in status.get("days", [])] if status else []
    )
    expected = (
        _holdout_day(frozen)
        if not indexed_days
        else _date(indexed_days[-1]) + timedelta(days=1)
    )
    if _date(day) != expected:
        raise ValueError(f"expected day {expected.isoformat()}, got {day}")

    staging = state / STAGING
    staging_tmp = staging.with_name(staging.name + ".tmp")
    shutil.copyfile(source, staging_tmp)
    staging_tmp.replace(staging)
    if _sha256(staging) != incoming_sha:
        raise ValueError("staged day hash mismatch")
    _atomic_json(
        state / JOURNAL,
        {
            "schema": _SCHEMA,
            "day": day,
            "day_sha256": incoming_sha,
            "protocol_sha256": protocol_sha256,
            "evaluator_sha256": evaluator_sha256,
        },
    )
    staging.replace(destination)
    if _crash_after_day_commit:
        raise RuntimeError("simulated crash after day commit")
    result = _rebuild(
        state,
        frozen=frozen,
        protocol_sha256=protocol_sha256,
        evaluator_sha256=evaluator_sha256,
        now_ms=now,
        _crash_after_ledger_commit=_crash_after_ledger_commit,
        _crash_after_status_commit=_crash_after_status_commit,
    )
    (state / JOURNAL).unlink()
    return result
