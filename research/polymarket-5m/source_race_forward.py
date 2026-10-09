"""Pool frozen source-race observations and pin one common-cutoff verdict."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from itertools import groupby
from pathlib import Path
from typing import Any, Iterable, Mapping

import h_forward


BASELINE_LEDGER = "baseline-observations.jsonl"
CANDIDATE_LEDGER = "candidate-observations.jsonl"
STATUS = "status.json"
VERDICT = "verdict.json"


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_arm_rows(
    rows: Iterable[dict[str, Any]], arm: str, frozen: Mapping[str, Any],
) -> None:
    allowed_sources = set(frozen["arms"][arm])
    allowed_evaluations = {float(value) for value in frozen["execution"]["evaluation_ms"]}
    cutoff = float(frozen["cutoff_ms"])
    strategy_id = str(frozen["strategy_id"])
    for index, row in enumerate(rows, 1):
        if row.get("strategy_id") != strategy_id:
            raise ValueError(f"{arm} row {index} strategy_id drift")
        source = str(row.get("signal_source") or "")
        if source not in allowed_sources:
            raise ValueError(f"{arm} signal source {source!r} is not frozen")
        try:
            signal_ms = float(row["signal_ms"])
            receive_ms = float(row["signal_receive_ms"])
            evaluation_ms = float(row["evaluation_ms"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{arm} row {index} has invalid timing") from exc
        if not math.isfinite(signal_ms) or not math.isfinite(receive_ms) or receive_ms < cutoff:
            raise ValueError(f"{arm} row {index} is before the frozen cutoff")
        if evaluation_ms not in allowed_evaluations:
            raise ValueError(f"{arm} row {index} has an unfrozen evaluation latency")
        try:
            datetime.fromtimestamp(receive_ms / 1_000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            raise ValueError(f"{arm} row {index} has an invalid receipt clock") from exc
        if not row.get("filled"):
            continue
        try:
            shares = float(row["filled_shares"])
            all_in_cost = float(row["all_in_cost"])
            pnl_per_share = float(row["pnl_per_share"])
            pnl = float(row["pnl"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{arm} row {index} has invalid fill economics") from exc
        if not all(math.isfinite(value) for value in (shares, all_in_cost, pnl_per_share, pnl)):
            raise ValueError(f"{arm} row {index} has non-finite fill economics")
        if shares <= 0 or not 0 < all_in_cost < 1:
            raise ValueError(f"{arm} row {index} has impossible fill economics")
        direction = row.get("direction")
        winner = row.get("winner")
        if direction not in ("Up", "Down") or winner not in ("Up", "Down"):
            raise ValueError(f"{arm} row {index} has an invalid resolved direction")
        won = direction == winner
        expected_per_share = 1.0 - all_in_cost if won else -all_in_cost
        if row.get("won") is not won or not math.isclose(
            pnl_per_share, expected_per_share, rel_tol=0, abs_tol=1e-9,
        ) or not math.isclose(pnl, expected_per_share * shares, rel_tol=0, abs_tol=1e-8):
            raise ValueError(f"{arm} row {index} fill PnL is inconsistent")


def _validated_terminal(
    path: Path,
    *,
    freeze_sha256: str,
    baseline_path: Path,
    candidate_path: Path,
) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != "current-h-source-race-forward-v1":
        raise ValueError("terminal source-race schema drift")
    if payload.get("source_race_freeze_sha256") != freeze_sha256:
        raise ValueError("terminal source-race freeze drift")
    if (
        payload.get("baseline_ledger_sha256") != _sha256(baseline_path)
        or payload.get("candidate_ledger_sha256") != _sha256(candidate_path)
    ):
        raise ValueError("terminal source-race ledger hash mismatch")
    return payload


def _validated_status(
    path: Path,
    *,
    freeze_sha256: str,
    baseline_path: Path,
    candidate_path: Path,
) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != "current-h-source-race-forward-v1":
        raise ValueError("source-race status schema drift")
    if payload.get("source_race_freeze_sha256") != freeze_sha256:
        raise ValueError("source-race status freeze drift")
    if not baseline_path.is_file() or not candidate_path.is_file() or (
        payload.get("baseline_ledger_sha256") != _sha256(baseline_path)
        or payload.get("candidate_ledger_sha256") != _sha256(candidate_path)
    ):
        raise ValueError("source-race status ledger hash mismatch")
    return payload


def read_terminal(state_dir: str | Path, freeze_sha256: str) -> dict[str, Any] | None:
    state = Path(state_dir)
    verdict_path = state / VERDICT
    if not verdict_path.is_file():
        return None
    return _validated_terminal(
        verdict_path,
        freeze_sha256=freeze_sha256,
        baseline_path=state / BASELINE_LEDGER,
        candidate_path=state / CANDIDATE_LEDGER,
    )


def _receipt_ms(row: Mapping[str, Any]) -> float:
    return float(row["signal_receive_ms"])


def _receipt_day(row: Mapping[str, Any]) -> str:
    return datetime.fromtimestamp(_receipt_ms(row) / 1_000, tz=timezone.utc).date().isoformat()


def _primary_fills(
    rows: Iterable[dict[str, Any]], primary_ms: float, minimum_shares: float,
) -> list[dict[str, Any]]:
    return sorted(
        (
            row for row in rows
            if float(row["evaluation_ms"]) == primary_ms
            and row.get("filled")
            and row.get("winner") is not None
            and float(row.get("filled_shares") or 0) >= minimum_shares
        ),
        key=lambda row: (_receipt_ms(row), str(row["market_id"])),
    )


def common_cutoff(
    baseline: list[dict[str, Any]],
    candidate: list[dict[str, Any]],
    *,
    minimum_fills: int,
    minimum_days: int,
) -> float | None:
    events = [(_receipt_ms(row), "baseline", _receipt_day(row)) for row in baseline]
    events += [(_receipt_ms(row), "candidate", _receipt_day(row)) for row in candidate]
    counts = {"baseline": 0, "candidate": 0}
    days = {"baseline": set(), "candidate": set()}
    for timestamp, group in groupby(sorted(events), key=lambda event: event[0]):
        for _timestamp, arm, day in group:
            counts[arm] += 1
            days[arm].add(day)
        if all(counts[arm] >= minimum_fills and len(days[arm]) >= minimum_days for arm in counts):
            return timestamp
    return None


def _arm_statistics(rows: list[dict[str, Any]], cutoff: float, alpha: float) -> dict[str, Any]:
    sample = [row for row in rows if _receipt_ms(row) <= cutoff]
    clustered_sample = [{**row, "day": _receipt_day(row)} for row in sample]
    shares = sum(float(row["filled_shares"]) for row in sample)
    pnl = sum(float(row["pnl"]) for row in sample)
    net_ev = pnl / shares
    wins = sum(bool(row["won"]) for row in sample)
    losses = len(sample) - wins
    return {
        "fills": len(sample),
        "days": len({_receipt_day(row) for row in sample}),
        "filled_shares": shares,
        "pnl": pnl,
        "net_ev_per_share": net_ev,
        "day_cluster_lower": h_forward.daily_cluster_lower(clustered_sample, alpha),
        "raw_exact_p": h_forward.run._exact_pvalue(sample) if net_ev > 0 else 1.0,
        "wins": wins,
        "losses": losses,
        "loss_rate": losses / len(sample),
    }


def evaluate(
    baseline_rows: list[dict[str, Any]],
    candidate_rows: list[dict[str, Any]],
    frozen: Mapping[str, Any],
) -> dict[str, Any]:
    _validate_arm_rows(baseline_rows, "baseline", frozen)
    _validate_arm_rows(candidate_rows, "candidate", frozen)
    primary = float(frozen["execution"]["primary_evaluation_ms"])
    minimum_shares = float(frozen["execution"]["minimum_direct_depth_shares"])
    statistics = frozen["statistics"]
    minimum_fills = int(statistics["minimum_fills"])
    minimum_days = int(statistics["minimum_utc_days"])
    alpha = float(statistics["one_sided_alpha"])
    family_tests = int(statistics["family_tests"])
    baseline = _primary_fills(baseline_rows, primary, minimum_shares)
    candidate = _primary_fills(candidate_rows, primary, minimum_shares)
    cutoff = common_cutoff(
        baseline, candidate, minimum_fills=minimum_fills, minimum_days=minimum_days,
    )
    if cutoff is None:
        return {
            "status": "collecting",
            "common_cutoff_ms": None,
            "required_fills": minimum_fills,
            "required_days": minimum_days,
            "arms": {
                "baseline": {"fills": len(baseline), "days": len({_receipt_day(row) for row in baseline})},
                "candidate": {"fills": len(candidate), "days": len({_receipt_day(row) for row in candidate})},
            },
        }
    arms = {
        "baseline": _arm_statistics(baseline, cutoff, alpha),
        "candidate": _arm_statistics(candidate, cutoff, alpha),
    }
    candidate_stats = arms["candidate"]
    baseline_stats = arms["baseline"]
    candidate_stats["corrected_exact_p"] = min(1.0, candidate_stats["raw_exact_p"] * family_tests)
    checks = {
        "candidate_positive_net_ev": candidate_stats["net_ev_per_share"] > 0,
        "candidate_positive_day_cluster_lower": (
            candidate_stats["day_cluster_lower"] is not None
            and candidate_stats["day_cluster_lower"] > 0
        ),
        "candidate_corrected_exact_p": candidate_stats["corrected_exact_p"] < alpha,
        "incremental_fill_or_ev_improvement": (
            candidate_stats["fills"] > baseline_stats["fills"]
            or candidate_stats["net_ev_per_share"] > baseline_stats["net_ev_per_share"]
        ),
        "loss_rate_not_worse": candidate_stats["loss_rate"] <= baseline_stats["loss_rate"],
    }
    return {
        "status": "passed" if all(checks.values()) else "rejected",
        "common_cutoff_ms": cutoff,
        "family_tests": family_tests,
        "alpha": alpha,
        "arms": arms,
        "checks": checks,
    }


def update(
    baseline_rows: str | Path,
    candidate_rows: str | Path,
    state_dir: str | Path,
    *,
    frozen: Mapping[str, Any],
    freeze_sha256: str,
) -> dict[str, Any]:
    state = Path(state_dir)
    state.mkdir(parents=True, exist_ok=True)
    baseline_path = state / BASELINE_LEDGER
    candidate_path = state / CANDIDATE_LEDGER
    existing_baseline = h_forward._read_jsonl(baseline_path)
    existing_candidate = h_forward._read_jsonl(candidate_path)
    verdict_path = state / VERDICT
    status_path = state / STATUS
    terminal_payload = None
    if verdict_path.exists():
        terminal_payload = _validated_terminal(
            verdict_path,
            freeze_sha256=freeze_sha256,
            baseline_path=baseline_path,
            candidate_path=candidate_path,
        )
    elif status_path.exists():
        _validated_status(
            status_path,
            freeze_sha256=freeze_sha256,
            baseline_path=baseline_path,
            candidate_path=candidate_path,
        )
    elif baseline_path.exists() or candidate_path.exists():
        raise ValueError("source-race ledgers exist without a hashed status")
    merged_baseline = h_forward.merge(existing_baseline, h_forward._read_jsonl(baseline_rows))
    merged_candidate = h_forward.merge(existing_candidate, h_forward._read_jsonl(candidate_rows))
    _validate_arm_rows(merged_baseline, "baseline", frozen)
    _validate_arm_rows(merged_candidate, "candidate", frozen)
    if terminal_payload is not None:
        if merged_baseline != existing_baseline or merged_candidate != existing_candidate:
            raise ValueError("terminal source-race ledger cannot accept new observations")
        return terminal_payload["verdict"]
    h_forward._write_rows(baseline_path, merged_baseline)
    h_forward._write_rows(candidate_path, merged_candidate)
    result = evaluate(merged_baseline, merged_candidate, frozen)
    payload = {
        "schema": "current-h-source-race-forward-v1",
        "strategy_id": frozen["strategy_id"],
        "source_race_freeze_sha256": freeze_sha256,
        "baseline_ledger_sha256": _sha256(baseline_path),
        "candidate_ledger_sha256": _sha256(candidate_path),
        "verdict": result,
    }
    _atomic_json(state / STATUS, payload)
    if result["status"] != "collecting":
        _atomic_json(verdict_path, payload)
    return result
