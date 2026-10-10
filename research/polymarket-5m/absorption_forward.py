"""Immutable, crash-safe forward admission for post-sweep absorption."""

from __future__ import annotations

import json
import math
import shutil
import time
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import basis_wedge_forward as basis
import h_forward

BASE_VARIANT = "base"
CONTROL_VARIANTS = (
    "no_refill",
    "direction_reversal",
    "time_shift",
    "quote_add_no_trade",
)
VARIANTS = (BASE_VARIANT, *CONTROL_VARIANTS)
MATCHED_CONTROLS = ("direction_reversal", "time_shift")

DAY_SCHEMA = "post-sweep-absorption-day-v1"
INDEX_SCHEMA = "post-sweep-absorption-forward-index-v1"
STATUS_SCHEMA = "post-sweep-absorption-forward-status-v1"
VERDICT_SCHEMA = "post-sweep-absorption-forward-verdict-v1"
JOURNAL_SCHEMA = "post-sweep-absorption-admission-journal-v1"
INDEX = "day-index.json"
STATUS = "status.json"
VERDICT = "verdict.json"
JOURNAL = "admission-journal.json"
STAGING = "pending-day.json"
DAYS_DIR = "days"

ROOT = Path(__file__).resolve().parent
PROTOCOL_FREEZE = ROOT / "forward" / "absorption-freeze.json"
EVALUATOR_FREEZE = ROOT / "forward" / "absorption-evaluator-freeze.json"
_EPS = 1e-9


def _atomic_json(path: str | Path, payload: Mapping[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)


def _load_json(path: str | Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON artifact: {path}") from exc
    if not isinstance(payload, dict):
        raise TypeError(f"expected JSON object: {path}")
    return payload


def load_freezes(
    protocol_path: str | Path = PROTOCOL_FREEZE,
    evaluator_path: str | Path = EVALUATOR_FREEZE,
) -> tuple[dict[str, Any], str, dict[str, Any], str]:
    protocol, protocol_sha = basis._load_hashed_json(
        Path(protocol_path), "post-sweep-absorption-protocol-v1"
    )
    evaluator, evaluator_sha = basis._load_hashed_json(
        Path(evaluator_path), "post-sweep-absorption-evaluator-freeze-v1"
    )
    if (
        evaluator.get("protocol_sha256") != protocol_sha
        or evaluator.get("strategy_id") != protocol.get("strategy_id")
        or evaluator.get("holdout_start") != protocol.get("holdout_start")
        or evaluator.get("variants") != list(VARIANTS)
    ):
        raise ValueError("absorption evaluator/protocol identity drift")
    return protocol, protocol_sha, evaluator, evaluator_sha


def _utc_day(timestamp_ms: float) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1_000, tz=UTC).date().isoformat()


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid UTC day: {value!r}") from exc


def _holdout_day(frozen: Mapping[str, Any]) -> date:
    return datetime.fromtimestamp(
        float(frozen["holdout_start_ms"]) / 1_000, tz=UTC
    ).date()


def _finite(value: Any, label: str) -> float:
    return basis._finite_number(value, label)


def _same(left: Any, right: Any, tolerance: float = 1e-9) -> bool:
    return basis._same_number(left, right, tolerance=tolerance)


def _levels(
    payload: Any,
    *,
    label: str,
    price_cap: float | None = None,
) -> list[tuple[float, float]]:
    if not isinstance(payload, list):
        raise TypeError(f"{label} levels are missing")
    levels: list[tuple[float, float]] = []
    for item in payload:
        if not isinstance(item, Mapping):
            raise TypeError(f"{label} level is invalid")
        price = _finite(item.get("price"), f"{label} price")
        shares = _finite(item.get("shares"), f"{label} shares")
        if not 0 < price < 1 or shares <= 0:
            raise ValueError(f"{label} level is impossible")
        if price_cap is not None and price > price_cap + _EPS:
            raise ValueError(f"{label} level exceeds the frozen limit")
        levels.append((price, shares))
    return levels


def _validate_leg(
    row: Mapping[str, Any],
    *,
    prefix: str,
    shares_field: str,
    vwap_field: str,
    levels_field: str,
    fee_field: str,
    notional_field: str,
    fee_rate: float,
    price_cap: float | None = None,
) -> float:
    shares = _finite(row.get(shares_field), f"{prefix} shares")
    levels = _levels(row.get(levels_field), label=prefix, price_cap=price_cap)
    if shares <= _EPS:
        if levels or row.get(vwap_field) is not None or not _same(row.get(fee_field), 0):
            raise ValueError(f"{prefix} zero-fill economics are inconsistent")
        if not _same(row.get(notional_field), 0):
            raise ValueError(f"{prefix} zero-fill notional is inconsistent")
        return 0.0
    level_shares = sum(size for _price, size in levels)
    notional = sum(price * size for price, size in levels)
    if not levels or not _same(level_shares, shares, 1e-8):
        raise ValueError(f"{prefix} levels do not reproduce shares")
    if not _same(row.get(vwap_field), notional / shares):
        raise ValueError(f"{prefix} levels do not reproduce VWAP")
    if not _same(row.get(notional_field), notional, 1e-8):
        raise ValueError(f"{prefix} notional is inconsistent")
    expected_fee = basis._rounded_fee(levels, fee_rate)
    if not _same(row.get(fee_field), expected_fee, 5e-9):
        raise ValueError(f"{prefix} fee is inconsistent")
    return shares


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
        raise ValueError(f"{prefix} variant drift")
    if row.get("paper_only") is not True or row.get("ordering_clock") != "local_receipt_ms":
        raise ValueError(f"{prefix} is not receipt-clock paper execution")
    if not str(row.get("signal_id") or "") or not str(row.get("market_id") or ""):
        raise ValueError(f"{prefix} identity is missing")
    signal = row.get("signal")
    if not isinstance(signal, Mapping):
        raise TypeError(f"{prefix} immutable signal is missing")
    for field in (
        "signal_id",
        "parent_signal_id",
        "variant",
        "market_id",
        "buy_asset_id",
        "buy_side",
        "decision_recv_ms",
        "target_shares",
        "fixed_limit",
        "sent",
    ):
        if json.dumps(row.get(field), sort_keys=True) != json.dumps(
            signal.get(field), sort_keys=True
        ):
            raise ValueError(f"{prefix} differs from its immutable signal")

    scheduled_raw = row.get("scheduled_decision_recv_ms")
    signal_scheduled_raw = signal.get("scheduled_decision_recv_ms")
    if variant == "time_shift":
        scheduled = _finite(scheduled_raw, f"{prefix} scheduled decision")
        if not _same(signal_scheduled_raw, scheduled):
            raise ValueError(f"{prefix} differs from its immutable scheduled decision")
    elif scheduled_raw is not None or signal_scheduled_raw is not None:
        raise ValueError(f"{prefix} unexpectedly has a scheduled decision")

    decision_ms = _finite(row.get("decision_recv_ms"), f"{prefix} decision")
    evaluation_ms = _finite(row.get("evaluation_ms"), f"{prefix} evaluation")
    entry_match_ms = _finite(row.get("entry_match_ms"), f"{prefix} entry match")
    if decision_ms < float(frozen["holdout_start_ms"]) or _utc_day(decision_ms) != day:
        raise ValueError(f"{prefix} is outside its frozen receipt day")
    allowed = {float(value) for value in frozen["execution"]["evaluation_ms"]}
    if evaluation_ms not in allowed or not _same(
        entry_match_ms, decision_ms + evaluation_ms, 1e-6
    ):
        raise ValueError(f"{prefix} entry timing drift")
    entry_book_ms = row.get("entry_book_recv_ms")
    if entry_book_ms is not None and _finite(
        entry_book_ms, f"{prefix} entry book"
    ) > entry_match_ms + _EPS:
        raise ValueError(f"{prefix} uses a future entry book")

    target = _finite(row.get("target_shares"), f"{prefix} target")
    fee_rate = _finite(row.get("fee_rate"), f"{prefix} fee rate")
    frozen_target = float(frozen["execution"]["minimum_full_fill_shares"])
    frozen_fee = float(frozen["execution"]["taker_fee_rate"])
    if not _same(target, frozen_target) or not _same(fee_rate, frozen_fee):
        raise ValueError(f"{prefix} target or fee-rate drift")
    sent = row.get("sent")
    filled = row.get("filled")
    full_fill = row.get("full_fill")
    if not all(isinstance(value, bool) for value in (sent, filled, full_fill)):
        raise TypeError(f"{prefix} execution flags are invalid")
    fixed_limit_raw = row.get("fixed_limit")
    fixed_limit = None if fixed_limit_raw is None else _finite(
        fixed_limit_raw, f"{prefix} limit"
    )
    if fixed_limit is not None and not 0 < fixed_limit < 1:
        raise ValueError(f"{prefix} frozen limit is invalid")

    entry_shares = _validate_leg(
        row,
        prefix=f"{prefix} entry",
        shares_field="entry_shares",
        vwap_field="entry_vwap",
        levels_field="entry_levels",
        fee_field="entry_fee",
        notional_field="entry_notional",
        fee_rate=fee_rate,
        price_cap=fixed_limit,
    )
    if filled != (entry_shares > _EPS) or full_fill != (
        entry_shares + _EPS >= target
    ):
        raise ValueError(f"{prefix} fill flags are inconsistent")
    if entry_shares > target + _EPS or (filled and (not sent or fixed_limit is None)):
        raise ValueError(f"{prefix} entry quantity is impossible")
    expected_entry_reason = "filled" if full_fill else "partial_fill"
    if filled and row.get("entry_reason") != expected_entry_reason:
        raise ValueError(f"{prefix} entry reason is inconsistent")
    if not filled:
        if (
            row.get("pnl") != 0.0
            or row.get("pnl_per_share") is not None
            or row.get("profitable") is not None
        ):
            raise ValueError(f"{prefix} unfilled PnL is inconsistent")
        return

    exit_decision_ms = _finite(row.get("exit_decision_ms"), f"{prefix} exit decision")
    exit_match_ms = _finite(row.get("exit_match_ms"), f"{prefix} exit match")
    wait_ms = float(frozen["execution"]["unwind_wait_ms"])
    if not _same(exit_decision_ms, entry_match_ms + wait_ms, 1e-6) or not _same(
        exit_match_ms, exit_decision_ms + evaluation_ms, 1e-6
    ):
        raise ValueError(f"{prefix} exit timing drift")
    exit_book_ms = row.get("exit_book_recv_ms")
    if exit_book_ms is not None and _finite(
        exit_book_ms, f"{prefix} exit book"
    ) > exit_match_ms + _EPS:
        raise ValueError(f"{prefix} uses a future exit book")
    exit_shares = _validate_leg(
        row,
        prefix=f"{prefix} exit",
        shares_field="exit_shares",
        vwap_field="exit_vwap",
        levels_field="exit_levels",
        fee_field="exit_fee",
        notional_field="exit_notional",
        fee_rate=fee_rate,
    )
    if exit_shares > entry_shares + _EPS:
        raise ValueError(f"{prefix} exits more shares than entered")
    settled = _finite(row.get("settled_shares"), f"{prefix} settled shares")
    if not _same(settled, entry_shares - exit_shares, 1e-8):
        raise ValueError(f"{prefix} residual shares are inconsistent")
    winner = row.get("winner")
    payout_raw = row.get("settlement_payout")
    if settled > _EPS:
        if winner not in ("Up", "Down") or payout_raw is None:
            raise ValueError(f"{prefix} has an unresolved fill")
        payout = _finite(payout_raw, f"{prefix} settlement payout")
        expected_payout = settled if str(winner).lower() == str(row["buy_side"]).lower() else 0.0
    else:
        payout = _finite(payout_raw, f"{prefix} settlement payout")
        expected_payout = 0.0
    if not _same(payout, expected_payout, 1e-8):
        raise ValueError(f"{prefix} settlement payout is inconsistent")
    if row.get("pnl") is None:
        raise ValueError(f"{prefix} has an unresolved fill")
    pnl = _finite(row.get("pnl"), f"{prefix} PnL")
    expected_pnl = (
        _finite(row.get("exit_notional"), f"{prefix} exit notional")
        - _finite(row.get("exit_fee"), f"{prefix} exit fee")
        + payout
        - _finite(row.get("entry_notional"), f"{prefix} entry notional")
        - _finite(row.get("entry_fee"), f"{prefix} entry fee")
    )
    if not _same(pnl, expected_pnl, 1e-8):
        raise ValueError(f"{prefix} PnL is inconsistent")
    if not _same(row.get("pnl_per_share"), pnl / entry_shares):
        raise ValueError(f"{prefix} PnL per share is inconsistent")
    if row.get("profitable") is not (pnl > 0):
        raise ValueError(f"{prefix} profitability flag is inconsistent")


def _group_rows(
    rows: Iterable[dict[str, Any]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault((str(row["variant"]), str(row["signal_id"])), []).append(row)
    return grouped


def _validate_groups(rows: list[dict[str, Any]], frozen: Mapping[str, Any]) -> None:
    expected = {float(value) for value in frozen["execution"]["evaluation_ms"]}
    grouped = _group_rows(rows)
    base = {
        signal_id: group[0]
        for (variant, signal_id), group in grouped.items()
        if variant == BASE_VARIANT
    }
    children: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for (variant, _signal_id), group in grouped.items():
        if {float(row["evaluation_ms"]) for row in group} != expected:
            raise ValueError("signal is missing a frozen latency")
        canonical = group[0]
        for row in group[1:]:
            for field in (
                "parent_signal_id",
                "market_id",
                "buy_asset_id",
                "buy_side",
                "decision_recv_ms",
                "target_shares",
                "fixed_limit",
                "fee_rate",
                "sent",
                "signal",
            ):
                if json.dumps(row.get(field), sort_keys=True) != json.dumps(
                    canonical.get(field), sort_keys=True
                ):
                    raise ValueError("paired decision fields differ across latencies")
        if variant in MATCHED_CONTROLS:
            parent = canonical.get("parent_signal_id")
            if not isinstance(parent, str) or parent not in base:
                raise ValueError("matched control has an orphan parent")
            children.setdefault((variant, parent), []).append(canonical)
    for signal_id, parent in base.items():
        if parent.get("parent_signal_id") is not None:
            raise ValueError("base signal unexpectedly has a parent")
        for variant in MATCHED_CONTROLS:
            matches = children.get((variant, signal_id), [])
            if len(matches) != 1:
                raise ValueError(f"base signal does not have exactly one {variant} control")
            child = matches[0]
            if child["market_id"] != parent["market_id"]:
                raise ValueError("matched control market differs from base")
            if variant == "direction_reversal":
                if (
                    child["buy_side"] == parent["buy_side"]
                    or not _same(child["decision_recv_ms"], parent["decision_recv_ms"])
                ):
                    raise ValueError("direction-reversal semantics drift")
            elif (
                child["buy_side"] != parent["buy_side"]
                or not _same(child["fixed_limit"], parent["fixed_limit"])
                or not _same(
                    child["scheduled_decision_recv_ms"],
                    child["decision_recv_ms"],
                )
                or not _same(
                    child["decision_recv_ms"],
                    float(parent["decision_recv_ms"]) + 5_000.0,
                    1e-6,
                )
            ):
                raise ValueError("time-shift semantics drift")


def validate_day_result(
    payload: Mapping[str, Any],
    *,
    frozen: Mapping[str, Any],
    protocol_sha256: str,
    evaluator_sha256: str,
    now_ms: float,
) -> str:
    if payload.get("schema") != DAY_SCHEMA or payload.get("complete") is not True:
        raise ValueError("absorption day artifact is incomplete")
    if (
        payload.get("collector_region") != frozen["collector_region"]
        or payload.get("protocol_sha256") != protocol_sha256
        or payload.get("evaluator_sha256") != evaluator_sha256
        or not str(payload.get("artifact_sha256") or "")
    ):
        raise ValueError("absorption day identity drift")
    day = str(payload.get("day") or "")
    day_value = _date(day)
    if day_value < _holdout_day(frozen):
        raise ValueError("absorption day predates the frozen holdout")
    next_midnight = datetime.combine(
        day_value + timedelta(days=1), datetime.min.time(), tzinfo=UTC
    ).timestamp() * 1_000
    if now_ms + _EPS < next_midnight:
        raise ValueError("UTC day is not closed")
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise TypeError("absorption day rows are missing")
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise TypeError(f"row {index} is not an object")
        _validate_row(row, day=day, frozen=frozen, index=index)
    _validate_groups(rows, frozen)
    return day


def _primary(
    rows: Iterable[dict[str, Any]], latency: float, target: float
) -> list[dict[str, Any]]:
    return sorted(
        (
            row
            for row in rows
            if row["variant"] == BASE_VARIANT
            and float(row["evaluation_ms"]) == latency
            and row["full_fill"]
            and row["pnl"] is not None
            and float(row["entry_shares"]) + _EPS >= target
        ),
        key=lambda row: (float(row["decision_recv_ms"]), str(row["signal_id"])),
    )


def _common_cutoff(
    samples: Mapping[str, list[dict[str, Any]]], minimum: int, minimum_days: int
) -> float | None:
    clocks = sorted(
        {
            float(row["decision_recv_ms"])
            for sample in samples.values()
            for row in sample
        }
    )
    for clock in clocks:
        if all(
            len([row for row in sample if float(row["decision_recv_ms"]) <= clock + _EPS])
            >= minimum
            and len(
                {
                    _utc_day(float(row["decision_recv_ms"]))
                    for row in sample
                    if float(row["decision_recv_ms"]) <= clock + _EPS
                }
            )
            >= minimum_days
            for sample in samples.values()
        ):
            return clock
    return None


def _variant_statistics(
    rows: Iterable[dict[str, Any]], *, cutoff_ms: float | None = None
) -> dict[str, dict[str, Any]]:
    retained = list(rows)
    output: dict[str, dict[str, Any]] = {}
    for latency in (400.0, 500.0):
        sample = [
            row
            for row in retained
            if float(row["evaluation_ms"]) == latency
            and (cutoff_ms is None or float(row["decision_recv_ms"]) <= cutoff_ms + _EPS)
        ]
        fills = [row for row in sample if row["filled"] and row["pnl"] is not None]
        full = [row for row in fills if row["full_fill"]]
        partial = [row for row in fills if not row["full_fill"]]
        shares = sum(float(row["entry_shares"]) for row in fills)
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
            "profitable": sum(bool(row["profitable"]) for row in fills),
            "non_profitable": sum(not bool(row["profitable"]) for row in fills),
            "reasons": dict(Counter(str(row["entry_reason"]) for row in sample)),
        }
    return output


def _exact_sign_pvalue(rows: list[dict[str, Any]]) -> float:
    count = len(rows)
    wins = sum(float(row["pnl"]) > 0 for row in rows)
    return sum(math.comb(count, k) for k in range(wins, count + 1)) / (2**count)


def _base_statistics(
    rows: list[dict[str, Any]], *, alpha: float, family_tests: int
) -> dict[str, Any]:
    sample = [
        {
            **row,
            "day": _utc_day(float(row["decision_recv_ms"])),
            "filled_shares": float(row["entry_shares"]),
        }
        for row in rows
    ]
    shares = sum(float(row["entry_shares"]) for row in sample)
    pnl = sum(float(row["pnl"]) for row in sample)
    ev = pnl / shares
    lower = h_forward.daily_cluster_lower(sample, alpha)
    raw_p = _exact_sign_pvalue(sample) if ev > 0 else 1.0
    return {
        "entries": len(sample),
        "days": len({row["day"] for row in sample}),
        "filled_shares": shares,
        "pnl": pnl,
        "net_ev_per_share": ev,
        "day_cluster_lower": lower,
        "raw_exact_sign_p": raw_p,
        "corrected_exact_sign_p": min(1.0, raw_p * family_tests),
        "profitable": sum(float(row["pnl"]) > 0 for row in sample),
        "non_profitable": sum(float(row["pnl"]) <= 0 for row in sample),
    }


def evaluate(rows: list[dict[str, Any]], frozen: Mapping[str, Any]) -> dict[str, Any]:
    statistics = frozen["statistics"]
    minimum = int(statistics["minimum_full_base_entries_per_latency"])
    minimum_days = int(statistics["minimum_utc_days"])
    target = float(frozen["execution"]["minimum_full_fill_shares"])
    samples = {
        str(int(latency)): _primary(rows, latency, target)
        for latency in (400.0, 500.0)
    }
    progress = {
        latency: {
            "full_entries": len(sample),
            "days": len({_utc_day(float(row["decision_recv_ms"])) for row in sample}),
        }
        for latency, sample in samples.items()
    }
    variants = {
        variant: _variant_statistics(row for row in rows if row["variant"] == variant)
        for variant in VARIANTS
    }
    cutoff = _common_cutoff(samples, minimum, minimum_days)
    if cutoff is None:
        return {
            "status": "collecting",
            "common_cutoff_ms": None,
            "required_full_entries_per_latency": minimum,
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
    controls = {
        variant: _variant_statistics(
            (row for row in rows if row["variant"] == variant), cutoff_ms=cutoff
        )
        for variant in CONTROL_VARIANTS
    }
    checks: dict[str, bool] = {}
    for latency, result in base.items():
        checks[f"{latency}_positive_net_ev"] = result["net_ev_per_share"] > 0
        checks[f"{latency}_positive_day_cluster_lower"] = (
            result["day_cluster_lower"] is not None
            and result["day_cluster_lower"] > 0
        )
        checks[f"{latency}_corrected_exact_sign_p"] = (
            result["corrected_exact_sign_p"] < alpha
        )
    control_minimum = int(
        statistics["minimum_resolved_full_control_entries_per_latency"]
    )
    checks["all_controls_observed"] = all(
        controls[variant][latency]["full_fills"] >= control_minimum
        for variant in CONTROL_VARIANTS
        for latency in ("400", "500")
    )
    checks["base_above_every_control"] = all(
        controls[variant][latency]["net_ev_per_share"] is not None
        and base[latency]["net_ev_per_share"]
        > controls[variant][latency]["net_ev_per_share"]
        for variant in CONTROL_VARIANTS
        for latency in ("400", "500")
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


def _initial_index(
    frozen: Mapping[str, Any], protocol_sha256: str, evaluator_sha256: str
) -> dict[str, Any]:
    return {
        "schema": INDEX_SCHEMA,
        "strategy_id": frozen["strategy_id"],
        "protocol_sha256": protocol_sha256,
        "evaluator_sha256": evaluator_sha256,
        "days": [],
    }


def _load_index(
    state: Path,
    *,
    frozen: Mapping[str, Any],
    protocol_sha256: str,
    evaluator_sha256: str,
    allow_pending_day: str | None = None,
) -> dict[str, Any]:
    path = state / INDEX
    if not path.is_file():
        indexed_files = set((state / DAYS_DIR).glob("*.json")) if (state / DAYS_DIR).is_dir() else set()
        allowed = (
            {state / DAYS_DIR / f"{allow_pending_day}.json"}
            if allow_pending_day is not None
            else set()
        )
        if indexed_files - allowed or (state / STATUS).exists() or (state / VERDICT).exists():
            raise ValueError("absorption state exists without an index")
        return _initial_index(frozen, protocol_sha256, evaluator_sha256)
    payload = _load_json(path)
    if (
        payload.get("schema") != INDEX_SCHEMA
        or payload.get("strategy_id") != frozen["strategy_id"]
        or payload.get("protocol_sha256") != protocol_sha256
        or payload.get("evaluator_sha256") != evaluator_sha256
        or not isinstance(payload.get("days"), list)
    ):
        raise ValueError("absorption forward index drift")
    expected = _holdout_day(frozen)
    indexed_names: set[str] = set()
    for item in payload["days"]:
        if not isinstance(item, Mapping):
            raise TypeError("absorption forward day index is invalid")
        day = str(item.get("day") or "")
        if _date(day) != expected:
            raise ValueError("absorption forward day index is not contiguous")
        expected += timedelta(days=1)
        path = state / DAYS_DIR / f"{day}.json"
        if not path.is_file() or basis._sha256(path) != item.get("sha256"):
            raise ValueError("absorption immutable day hash mismatch")
        indexed_names.add(day)
    actual_names = {
        path.stem for path in (state / DAYS_DIR).glob("*.json")
    } if (state / DAYS_DIR).is_dir() else set()
    allowed_names = set(indexed_names)
    if allow_pending_day is not None:
        allowed_names.add(allow_pending_day)
    if actual_names != allowed_names:
        raise ValueError("absorption forward day index mismatch")
    return payload


def _payloads(
    state: Path,
    index: Mapping[str, Any],
    *,
    frozen: Mapping[str, Any],
    protocol_sha256: str,
    evaluator_sha256: str,
    now_ms: float,
) -> list[dict[str, Any]]:
    payloads = []
    for item in index["days"]:
        payload = _load_json(state / DAYS_DIR / f"{item['day']}.json")
        validate_day_result(
            payload,
            frozen=frozen,
            protocol_sha256=protocol_sha256,
            evaluator_sha256=evaluator_sha256,
            now_ms=now_ms,
        )
        payloads.append(payload)
    return payloads


def _rebuild_outputs(
    state: Path,
    index: Mapping[str, Any],
    *,
    frozen: Mapping[str, Any],
    protocol_sha256: str,
    evaluator_sha256: str,
    now_ms: float,
    allow_stale_status: bool,
) -> dict[str, Any]:
    payloads = _payloads(
        state,
        index,
        frozen=frozen,
        protocol_sha256=protocol_sha256,
        evaluator_sha256=evaluator_sha256,
        now_ms=now_ms,
    )
    rows = [row for payload in payloads for row in payload["rows"]]
    result = evaluate(rows, frozen)
    status = {
        "schema": STATUS_SCHEMA,
        "strategy_id": frozen["strategy_id"],
        "protocol_sha256": protocol_sha256,
        "evaluator_sha256": evaluator_sha256,
        "index_sha256": basis._sha256(state / INDEX),
        "result": result,
    }
    status_path = state / STATUS
    if status_path.exists() and not allow_stale_status and _load_json(status_path) != status:
        raise ValueError("absorption forward status drift")
    _atomic_json(status_path, status)
    verdict_path = state / VERDICT
    if result["status"] == "collecting":
        if verdict_path.exists():
            raise ValueError("terminal absorption verdict exists while collecting")
    else:
        verdict = {**status, "schema": VERDICT_SCHEMA}
        if verdict_path.exists() and _load_json(verdict_path) != verdict:
            raise ValueError("pinned absorption verdict changed")
        _atomic_json(verdict_path, verdict)
    return result


def _recover(
    state: Path,
    *,
    frozen: Mapping[str, Any],
    protocol_sha256: str,
    evaluator_sha256: str,
    now_ms: float,
) -> tuple[str, dict[str, Any]]:
    journal = _load_json(state / JOURNAL)
    if (
        journal.get("schema") != JOURNAL_SCHEMA
        or journal.get("protocol_sha256") != protocol_sha256
        or journal.get("evaluator_sha256") != evaluator_sha256
    ):
        raise ValueError("absorption admission journal drift")
    day = str(journal.get("day") or "")
    expected_sha = str(journal.get("sha256") or "")
    index = _load_index(
        state,
        frozen=frozen,
        protocol_sha256=protocol_sha256,
        evaluator_sha256=evaluator_sha256,
        allow_pending_day=day,
    )
    destination = state / DAYS_DIR / f"{day}.json"
    staging = state / STAGING
    if destination.exists():
        if basis._sha256(destination) != expected_sha:
            raise ValueError("journaled absorption day changed")
    elif staging.exists() and basis._sha256(staging) == expected_sha:
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging.replace(destination)
    else:
        raise ValueError("journaled absorption day bytes are missing")
    days = [str(item["day"]) for item in index["days"]]
    if day not in days:
        expected = (
            _holdout_day(frozen)
            if not days
            else _date(days[-1]) + timedelta(days=1)
        )
        if _date(day) != expected:
            raise ValueError("journaled absorption day is not chronological")
        index["days"].append({"day": day, "sha256": expected_sha})
        _atomic_json(state / INDEX, index)
    result = _rebuild_outputs(
        state,
        index,
        frozen=frozen,
        protocol_sha256=protocol_sha256,
        evaluator_sha256=evaluator_sha256,
        now_ms=now_ms,
        allow_stale_status=True,
    )
    (state / JOURNAL).unlink()
    staging.unlink(missing_ok=True)
    return day, result


def update(
    day_result: str | Path,
    state_dir: str | Path,
    *,
    frozen: Mapping[str, Any] | None = None,
    protocol_sha256: str | None = None,
    evaluator_sha256: str | None = None,
    now_ms: float | None = None,
    _allow_test_freeze: bool = False,
    _crash_after_day_commit: bool = False,
    _crash_after_index_commit: bool = False,
) -> dict[str, Any]:
    if _allow_test_freeze:
        if frozen is None or protocol_sha256 is None or evaluator_sha256 is None:
            raise ValueError("test freeze identity is incomplete")
    else:
        loaded, protocol_sha, _evaluator, evaluator_sha = load_freezes()
        if frozen is not None and frozen != loaded:
            raise ValueError("caller-supplied absorption protocol drift")
        if protocol_sha256 is not None and protocol_sha256 != protocol_sha:
            raise ValueError("caller-supplied absorption protocol hash drift")
        if evaluator_sha256 is not None and evaluator_sha256 != evaluator_sha:
            raise ValueError("caller-supplied absorption evaluator hash drift")
        frozen = loaded
        protocol_sha256 = protocol_sha
        evaluator_sha256 = evaluator_sha
    assert frozen is not None
    assert protocol_sha256 is not None
    assert evaluator_sha256 is not None
    clock = time.time() * 1_000 if now_ms is None else float(now_ms)
    state = Path(state_dir)
    state.mkdir(parents=True, exist_ok=True)

    recovered_result = None
    if (state / JOURNAL).exists():
        _recovered_day, recovered_result = _recover(
            state,
            frozen=frozen,
            protocol_sha256=protocol_sha256,
            evaluator_sha256=evaluator_sha256,
            now_ms=clock,
        )

    source = Path(day_result)
    incoming = _load_json(source)
    incoming_sha = basis._sha256(source)
    index = _load_index(
        state,
        frozen=frozen,
        protocol_sha256=protocol_sha256,
        evaluator_sha256=evaluator_sha256,
    )
    days = [str(item["day"]) for item in index["days"]]
    incoming_day = str(incoming.get("day") or "")
    if incoming_day in days:
        item = index["days"][days.index(incoming_day)]
        if item["sha256"] != incoming_sha:
            raise ValueError("absorption admitted day changed")
        return recovered_result or _rebuild_outputs(
            state,
            index,
            frozen=frozen,
            protocol_sha256=protocol_sha256,
            evaluator_sha256=evaluator_sha256,
            now_ms=clock,
            allow_stale_status=False,
        )
    day = validate_day_result(
        incoming,
        frozen=frozen,
        protocol_sha256=protocol_sha256,
        evaluator_sha256=evaluator_sha256,
        now_ms=clock,
    )
    if (state / VERDICT).exists():
        raise ValueError("terminal absorption sample cannot accept another day")
    expected = _holdout_day(frozen) if not days else _date(days[-1]) + timedelta(days=1)
    if _date(day) != expected:
        raise ValueError(f"expected {expected.isoformat()}, got {day}")

    staging = state / STAGING
    shutil.copyfile(source, staging)
    if basis._sha256(staging) != incoming_sha:
        raise ValueError("absorption day copy changed")
    _atomic_json(
        state / JOURNAL,
        {
            "schema": JOURNAL_SCHEMA,
            "day": day,
            "sha256": incoming_sha,
            "protocol_sha256": protocol_sha256,
            "evaluator_sha256": evaluator_sha256,
        },
    )
    destination = state / DAYS_DIR / f"{day}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging.replace(destination)
    if _crash_after_day_commit:
        raise RuntimeError("simulated crash after day commit")
    index["days"].append({"day": day, "sha256": incoming_sha})
    _atomic_json(state / INDEX, index)
    if _crash_after_index_commit:
        raise RuntimeError("simulated crash after index commit")
    result = _rebuild_outputs(
        state,
        index,
        frozen=frozen,
        protocol_sha256=protocol_sha256,
        evaluator_sha256=evaluator_sha256,
        now_ms=clock,
        allow_stale_status=True,
    )
    (state / JOURNAL).unlink()
    return result
