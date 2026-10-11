"""Calibrate public-book paper fills against private FAK order receipts.

This does not create signals, alter limits, or turn displayed depth into a fill. It accepts one
already-frozen attempt per row and measures how often the exact paper match snapshot overstated
real execution. The four clocks are explicit: decision, send, match and response. ``match_ms`` is
the measured venue match time and already includes the venue hold; callers must not add it again.
Rows without a final private outcome remain unknown and never count as fills or kills.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from scipy.stats import beta, fisher_exact


FINAL_STATUSES = frozenset({"filled_full", "filled_partial", "killed", "rejected"})
ALL_STATUSES = FINAL_STATUSES | {"unknown"}


@dataclass(frozen=True)
class Attempt:
    attempt_id: str
    market_id: str
    decision_ms: float
    send_ms: float
    match_ms: float
    response_ms: float
    requested_shares: float
    paper_fill_shares: float
    actual_fill_shares: float
    actual_status: str
    receipt_source: str
    venue_hold_ms: float | None = None
    net_pnl_per_share: float | None = None

    @property
    def paper_full(self) -> bool:
        return self.paper_fill_shares + 1e-9 >= self.requested_shares

    @property
    def actual_full(self) -> bool:
        return self.actual_fill_shares + 1e-9 >= self.requested_shares

    @property
    def actual_any(self) -> bool:
        return self.actual_fill_shares > 1e-9

    @property
    def known(self) -> bool:
        return self.actual_status in FINAL_STATUSES


def _finite(row: Mapping[str, Any], field: str) -> float:
    try:
        value = float(row[field])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"{field} must be present and numeric") from error
    if not math.isfinite(value):
        raise ValueError(f"{field} must be finite")
    return value


def parse_attempt(row: Mapping[str, Any]) -> Attempt:
    """Validate one private-receipt row, failing closed on ambiguity."""
    attempt_id = str(row.get("attempt_id", "")).strip()
    market_id = str(row.get("market_id", "")).strip()
    if not attempt_id or not market_id:
        raise ValueError("attempt_id and market_id must be non-empty")
    status = str(row.get("actual_status", "")).strip()
    if status not in ALL_STATUSES:
        raise ValueError(f"unsupported actual_status: {status!r}")
    receipt_source = str(row.get("receipt_source", "")).strip()
    if receipt_source != "private_order_response":
        raise ValueError("receipt_source must be private_order_response")

    decision = _finite(row, "decision_ms")
    send = _finite(row, "send_ms")
    match = _finite(row, "match_ms")
    response = _finite(row, "response_ms")
    if not decision <= send <= match <= response:
        raise ValueError("clocks must satisfy decision_ms <= send_ms <= match_ms <= response_ms")

    requested = _finite(row, "requested_shares")
    paper = _finite(row, "paper_fill_shares")
    actual = _finite(row, "actual_fill_shares")
    if requested <= 0 or paper < 0 or actual < 0:
        raise ValueError("requested shares must be positive and fill shares non-negative")
    if paper > requested + 1e-9 or actual > requested + 1e-9:
        raise ValueError("fill shares cannot exceed requested_shares")
    if status == "filled_full" and actual + 1e-9 < requested:
        raise ValueError("filled_full requires all requested shares")
    if status == "filled_partial" and not 1e-9 < actual < requested - 1e-9:
        raise ValueError("filled_partial requires a strict partial fill")
    if status in {"killed", "rejected"} and actual > 1e-9:
        raise ValueError(f"{status} cannot contain a fill")
    if status == "unknown" and actual > 1e-9:
        raise ValueError("unknown outcomes cannot be imputed as fills")

    venue_hold = row.get("venue_hold_ms")
    if venue_hold is not None:
        venue_hold = float(venue_hold)
        if not math.isfinite(venue_hold) or venue_hold < 0:
            raise ValueError("venue_hold_ms must be finite and non-negative")
        if match - send + 1e-9 < venue_hold:
            raise ValueError("measured send-to-match time cannot be shorter than venue_hold_ms")

    pnl = row.get("net_pnl_per_share")
    if pnl is not None:
        pnl = float(pnl)
        if not math.isfinite(pnl):
            raise ValueError("net_pnl_per_share must be finite when present")

    return Attempt(
        attempt_id, market_id, decision, send, match, response, requested, paper, actual,
        status, receipt_source, venue_hold, pnl,
    )


def load_attempts(path: str | Path) -> list[Attempt]:
    rows: list[Attempt] = []
    seen: set[str] = set()
    with Path(path).open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = parse_attempt(json.loads(line))
            except (json.JSONDecodeError, ValueError) as error:
                raise ValueError(f"{path}:{number}: {error}") from error
            if row.attempt_id in seen:
                raise ValueError(f"{path}:{number}: duplicate attempt_id {row.attempt_id!r}")
            seen.add(row.attempt_id)
            rows.append(row)
    return rows


def exact_binomial_interval(successes: int, trials: int,
                            confidence: float = 0.99) -> tuple[float, float]:
    """Two-sided Clopper-Pearson interval; return NaNs with no trials."""
    if trials < 0 or successes < 0 or successes > trials:
        raise ValueError("invalid binomial counts")
    if not 0 < confidence < 1:
        raise ValueError("confidence must lie strictly between zero and one")
    if trials == 0:
        return math.nan, math.nan
    alpha = 1.0 - confidence
    lower = 0.0 if successes == 0 else float(beta.ppf(alpha / 2, successes, trials - successes + 1))
    upper = 1.0 if successes == trials else float(beta.ppf(1 - alpha / 2, successes + 1, trials - successes))
    return lower, upper


def _quantile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * q
    lo, hi = math.floor(index), math.ceil(index)
    if lo == hi:
        return float(ordered[lo])
    return float(ordered[lo] * (hi - index) + ordered[hi] * (index - lo))


def _latency_bucket(value: float) -> str:
    if value < 500:
        return "lt500ms"
    if value < 600:
        return "500_599ms"
    if value < 1000:
        return "600_999ms"
    return "ge1000ms"


def _bucket_summary(rows: Sequence[Attempt]) -> dict[str, dict[str, Any]]:
    buckets: dict[str, list[Attempt]] = defaultdict(list)
    for row in rows:
        buckets[_latency_bucket(row.match_ms - row.decision_ms)].append(row)
    result: dict[str, dict[str, Any]] = {}
    for name, members in sorted(buckets.items()):
        predicted = [row for row in members if row.known and row.paper_full]
        full = sum(row.actual_full for row in predicted)
        lower, upper = exact_binomial_interval(full, len(predicted))
        result[name] = {
            "attempts": len(members),
            "known_paper_full_predictions": len(predicted),
            "actual_full_fills": full,
            "actual_full_rate": full / len(predicted) if predicted else None,
            "exact_99pct_interval": [lower, upper] if predicted else None,
        }
    return result


def _adverse_selection(rows: Sequence[Attempt]) -> dict[str, Any] | None:
    sample = [row for row in rows if row.known and row.paper_full and row.net_pnl_per_share is not None]
    if not sample:
        return None
    loss_fill = sum(row.actual_any for row in sample if row.net_pnl_per_share < 0)
    loss_miss = sum(not row.actual_any for row in sample if row.net_pnl_per_share < 0)
    profit_fill = sum(row.actual_any for row in sample if row.net_pnl_per_share >= 0)
    profit_miss = sum(not row.actual_any for row in sample if row.net_pnl_per_share >= 0)
    if not (loss_fill + loss_miss) or not (profit_fill + profit_miss):
        odds = p_value = math.nan
    else:
        odds, p_value = fisher_exact(
            [[loss_fill, loss_miss], [profit_fill, profit_miss]], alternative="greater"
        )
    return {
        "fee_inclusive_rows": len(sample),
        "loss_fill_miss_profit_fill_miss": [loss_fill, loss_miss, profit_fill, profit_miss],
        "loss_fill_odds_ratio": float(odds) if math.isfinite(odds) else None,
        "one_sided_fisher_p_value": float(p_value) if math.isfinite(p_value) else None,
        "paper_pnl_usd": sum(row.paper_fill_shares * row.net_pnl_per_share for row in sample),
        "actual_pnl_usd": sum(row.actual_fill_shares * row.net_pnl_per_share for row in sample),
    }


def calibrate(attempts: Iterable[Attempt]) -> dict[str, Any]:
    rows = list(attempts)
    ids = [row.attempt_id for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("attempt_id values must be unique")
    known = [row for row in rows if row.known]
    predicted = [row for row in known if row.paper_full]
    full = sum(row.actual_full for row in predicted)
    any_fill = sum(row.actual_any for row in predicted)
    lower, upper = exact_binomial_interval(full, len(predicted))
    paper_shares = sum(row.paper_fill_shares for row in known)
    actual_shares = sum(row.actual_fill_shares for row in known)
    decision_match = [row.match_ms - row.decision_ms for row in rows]
    send_match = [row.match_ms - row.send_ms for row in rows]
    response_match = [row.response_ms - row.match_ms for row in rows]
    return {
        "schema": "mine500-private-fak-calibration-v1",
        "attempts": len(rows),
        "unique_markets": len({row.market_id for row in rows}),
        "status_counts": dict(sorted(Counter(row.actual_status for row in rows).items())),
        "known_outcomes": len(known),
        "unknown_outcomes": len(rows) - len(known),
        "paper_full_predictions_known": len(predicted),
        "actual_full_fills_among_paper_full": full,
        "actual_any_fills_among_paper_full": any_fill,
        "paper_full_realization_rate": full / len(predicted) if predicted else None,
        "paper_any_realization_rate": any_fill / len(predicted) if predicted else None,
        "paper_full_realization_exact_99pct_interval": [lower, upper] if predicted else None,
        "paper_false_positive_full_count": sum(not row.actual_full for row in predicted),
        "paper_false_negative_any_count": sum(row.actual_any and row.paper_fill_shares <= 1e-9 for row in known),
        "requested_shares_known": sum(row.requested_shares for row in known),
        "paper_fill_shares_known": paper_shares,
        "actual_fill_shares_known": actual_shares,
        "share_realization_ratio": actual_shares / paper_shares if paper_shares > 0 else None,
        "latency_ms": {
            "decision_to_match_p50": _quantile(decision_match, 0.5),
            "decision_to_match_p95": _quantile(decision_match, 0.95),
            "send_to_match_p50": _quantile(send_match, 0.5),
            "send_to_match_p95": _quantile(send_match, 0.95),
            "match_to_response_p50": _quantile(response_match, 0.5),
            "match_to_response_p95": _quantile(response_match, 0.95),
            "venue_hold_is_already_in_match_clock": True,
        },
        "by_total_decision_to_match_bucket": _bucket_summary(rows),
        "adverse_selection": _adverse_selection(rows),
        "qualification": {
            "strategy_result": False,
            "public_depth_remains_upper_bound": True,
            "may_change_frozen_candidate_rules": False,
            "note": "Use only prospective private receipts from already-frozen orders; unknown outcomes remain unresolved.",
        },
    }


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("attempts_jsonl")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    result = calibrate(load_attempts(args.attempts_jsonl))
    Path(args.out).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
