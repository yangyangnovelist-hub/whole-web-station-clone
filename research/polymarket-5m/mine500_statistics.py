"""Descriptive, fail-closed statistics for the mine500 simulated attempt ledger.

Standard library only.  This module does not execute trades, fit a rule, label
development results OOS, or certify profitability.  Separate latency/size runs
are counterfactual alternatives and are never added as portfolio returns.
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import random
from typing import Any


SCHEMA = "mine500.statistics.v1"
EPS = 1e-8


def _number(value: Any, name: str, *, default: float | None = None) -> float:
    if value is None or value == "":
        if default is None:
            raise ValueError(f"missing {name}")
        return default
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"non-finite {name}")
    return result


def _bool(value: Any) -> bool:
    if value in (True, 1, "true", "True", "1"):
        return True
    if value in (False, 0, None, "", "false", "False", "0"):
        return False
    raise ValueError(f"invalid boolean {value!r}")


def normalize_attempt(row: dict[str, Any]) -> dict[str, Any]:
    """Runner cost_usd INCLUDES fees; reject invented or double-deducted PnL."""
    r = dict(row)
    for name, aliases in {
        "filled_shares": ("shares_filled",),
        "fees_usd": ("fee_usd",),
        "net_pnl_usd": ("pnl_usd",),
    }.items():
        if name not in r:
            for alias in aliases:
                if alias in r:
                    r[name] = r[alias]
                    break
    for name in ("candidate_id", "market_id", "status"):
        if not str(r.get(name, "")):
            raise ValueError(f"missing {name}")
        r[name] = str(r[name])
    for name in ("decision_ms", "latency_ms", "requested_shares"):
        r[name] = _number(r.get(name), name)
    if r["latency_ms"] < 0 or r["requested_shares"] <= 0:
        raise ValueError("negative latency or nonpositive requested_shares")
    for name in ("filled_shares", "cost_usd", "fees_usd"):
        r[name] = _number(r.get(name), name, default=0.0)
        if r[name] < -EPS:
            raise ValueError(f"negative {name}")
    if r["filled_shares"] > r["requested_shares"] + EPS:
        raise ValueError("filled_shares exceeds requested_shares")
    if r["fees_usd"] > r["cost_usd"] + EPS:
        raise ValueError("fees_usd exceeds all-in cost_usd")
    r["settled"] = _bool(r.get("settled", False))
    if "submitted" in r:
        r["sent"] = _bool(r["submitted"])
    else:
        r["sent"] = _bool(r["sent"]) if "sent" in r else not r["status"].startswith("rejected_")
    if r["filled_shares"] > EPS and not r["sent"]:
        raise ValueError("unsent attempt has a fill")
    if r["status"] == "filled_full" and not math.isclose(r["filled_shares"], r["requested_shares"], abs_tol=EPS):
        raise ValueError("filled_full status disagrees with shares")
    if r["status"] == "filled_partial" and not EPS < r["filled_shares"] < r["requested_shares"] - EPS:
        raise ValueError("filled_partial status disagrees with shares")
    if r["filled_shares"] <= EPS and (abs(r["cost_usd"]) > EPS or abs(r["fees_usd"]) > EPS):
        raise ValueError("zero fill has trading cost; explicit non-trade cost model required")
    r["decision_day_utc"] = datetime.fromtimestamp(r["decision_ms"] / 1000, timezone.utc).date().isoformat()
    r["settlement_day_utc"] = None
    if r["settled"] and r["filled_shares"] > EPS:
        r["settlement_ms"] = _number(r.get("settlement_ms"), "settlement_ms")
        if r["settlement_ms"] < r["decision_ms"]:
            raise ValueError("settlement precedes decision")
        r["settlement_day_utc"] = datetime.fromtimestamp(r["settlement_ms"] / 1000, timezone.utc).date().isoformat()
        r["payout_usd"] = _number(r.get("payout_usd"), "payout_usd")
        if r["payout_usd"] < -EPS or r["payout_usd"] > r["filled_shares"] + EPS:
            raise ValueError("payout outside binary share payout bounds")
        pnl = r["payout_usd"] - r["cost_usd"]
        if r.get("net_pnl_usd") not in (None, "") and not math.isclose(_number(r["net_pnl_usd"], "net_pnl_usd"), pnl, rel_tol=1e-8, abs_tol=EPS):
            raise ValueError("net_pnl_usd disagrees with payout minus all-in cost (fees already included)")
        r["net_pnl_usd"] = pnl
    else:
        if r.get("net_pnl_usd") not in (None, "", 0, 0.0, "0"):
            raise ValueError("unsettled or unfilled attempt reports realized PnL")
        r["net_pnl_usd"] = None
        r["payout_usd"] = None
    return r


def poisson_binomial_upper_tail(probabilities: list[float], wins: int) -> float:
    """Exact one-sided independent-Bernoulli tail; no normal approximation."""
    n = len(probabilities)
    if not 0 <= wins <= n:
        raise ValueError("wins outside [0,n]")
    if any(not math.isfinite(p) or not 0 <= p <= 1 for p in probabilities):
        raise ValueError("invalid Bernoulli probability")
    if wins == 0:
        return 1.0
    # Prefer the smaller truncated state space.  With unusually many wins,
    # compute the lower tail of failures directly, avoiding cancellation.
    use_failures = n - wins < wins
    limit = n - wins if use_failures else wins - 1
    probabilities = [1 - p for p in probabilities] if use_failures else probabilities
    dp = [1.0] + [0.0] * limit
    for i, p in enumerate(probabilities):
        for k in range(min(limit, i + 1), 0, -1):
            dp[k] = dp[k] * (1 - p) + dp[k - 1] * p
        dp[0] *= 1 - p
    cdf = math.fsum(dp)
    return max(0.0, min(1.0, cdf if use_failures else 1 - cdf))


def fair_price_test(fills: list[dict[str, Any]], family_tests: int) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "not_applicable", "p_value": None, "bonferroni_p_value": None,
        "family_tests": family_tests, "alpha": 0.01,
        "assumption": "Independent market Bernoulli outcomes under break-even probability all-in cost/filled shares (fee already included); descriptive reference, not proof of independence.",
    }
    if not fills:
        result["reason"] = "no_settled_fills"
        return result
    if len({r["market_id"] for r in fills}) != len(fills):
        result["reason"] = "multiple_settled_entries_per_market"
        return result
    size = fills[0]["filled_shares"]
    if any(not math.isclose(r["filled_shares"], size, abs_tol=EPS) for r in fills):
        result["reason"] = "unequal_actual_filled_shares"
        return result
    if any(not (math.isclose(r["payout_usd"], 0, abs_tol=EPS) or math.isclose(r["payout_usd"], size, abs_tol=EPS)) for r in fills):
        result["reason"] = "nonbinary_payout_or_exit_trade"
        return result
    probabilities = [r["cost_usd"] / size for r in fills]
    if any(not 0 <= p <= 1 for p in probabilities):
        result["reason"] = "break_even_probability_outside_unit_interval"
        return result
    wins = sum(math.isclose(r["payout_usd"], size, abs_tol=EPS) for r in fills)
    p = poisson_binomial_upper_tail(probabilities, wins)
    result.update(status="computed_reference_only", wins=wins, entries=len(fills), p_value=p,
                  bonferroni_p_value=min(1.0, p * family_tests),
                  bonferroni_below_alpha=p < 0.01 / family_tests)
    return result


def day_cluster_bootstrap(days: list[dict[str, Any]], *, reps: int = 10000, seed: int = 500) -> dict[str, Any]:
    """Resample whole UTC decision-day clusters; ratio of sums per draw."""
    active = [d for d in days if d["settled_shares"] > EPS]
    result: dict[str, Any] = {"status": "insufficient_days", "cluster": "UTC decision day", "confidence": 0.99,
                             "lower_bound_net_usd_per_share": None, "settled_fill_days": len(active),
                             "minimum_days": 7, "repetitions": reps, "seed": seed}
    if len(active) < 7:
        return result
    if reps < 100:
        raise ValueError("bootstrap reps must be at least 100")
    rng = random.Random(seed)
    samples = []
    for _ in range(reps):
        pnl = shares = 0.0
        for _ in active:
            d = active[rng.randrange(len(active))]
            pnl += d.get("realized_pnl_usd", d["net_pnl_usd"])
            shares += d["settled_shares"]
        samples.append(pnl / shares)
    samples.sort()
    # Inverse empirical CDF, one-sided 99% lower confidence bound.
    lower = samples[max(0, math.ceil(0.01 * reps) - 1)]
    result.update(status="descriptive_only", lower_bound_net_usd_per_share=lower,
                  warning="Assumes exchangeable independent days; seven days is a minimum, not strong evidence.")
    return result


def _aggregation(rows: list[dict[str, Any]]) -> dict[str, Any]:
    fills = [r for r in rows if r["filled_shares"] > EPS]
    settled = [r for r in fills if r["settled"]]
    count = Counter(r["status"] for r in rows)
    total_shares = math.fsum(r["filled_shares"] for r in fills)
    shares = math.fsum(r["filled_shares"] for r in settled)
    pnl = math.fsum(r["net_pnl_usd"] for r in settled)
    return {
        "attempts": len(rows), "sends": sum(r["sent"] for r in rows),
        "full_fills": sum(math.isclose(r["filled_shares"], r["requested_shares"], abs_tol=EPS) for r in fills),
        "partial_fills": sum(r["filled_shares"] < r["requested_shares"] - EPS for r in fills),
        "no_fills": count.get("no_fill", 0),
        "rejected": sum(r["status"].startswith("rejected_") for r in rows),
        "rejected_before_send": sum(not r["sent"] for r in rows),
        "rejected_after_send": sum(r["sent"] and r["status"].startswith("rejected_") for r in rows),
        "incomplete_match_horizon": count.get("incomplete_match_horizon", 0),
        "failures_and_unresolved_matches": sum(r["filled_shares"] <= EPS for r in rows),
        "settled_fills": len(settled), "unsettled_fills": len(fills) - len(settled),
        "markets": len({r["market_id"] for r in rows}),
        "filled_markets": len({r["market_id"] for r in fills}),
        "settled_markets": len({r["market_id"] for r in settled}),
        "filled_shares": total_shares, "settled_shares": shares,
        "cost_usd_all_fills": math.fsum(r["cost_usd"] for r in fills),
        "fees_usd_all_fills": math.fsum(r["fees_usd"] for r in fills),
        "unsettled_cost_and_fees_usd": math.fsum(r["cost_usd"] for r in fills if not r["settled"]),
        "realized_pnl_usd": pnl if settled else None,
        "net_pnl_usd": pnl if settled and len(settled) == len(fills) and not count.get("incomplete_match_horizon", 0) else None,
        "net_usd_per_settled_share": pnl / shares if shares > EPS else None,
        "status_counts": dict(sorted(count.items())),
    }


def _runner_summary(payload: dict[str, Any], candidate: str, latency: float, size: float) -> dict[str, Any] | None:
    summaries = payload.get("summaries", payload.get("summary", []))
    if isinstance(summaries, dict):
        summaries = list(summaries.values())
    if not isinstance(summaries, list):
        return None
    found = [r for r in summaries if isinstance(r, dict) and str(r.get("candidate_id")) == candidate
             and float(r.get("latency_ms", -1)) == latency
             and float(r.get("requested_shares", size)) == size]
    return found[0] if len(found) == 1 else None


def denominator_audit(summaries: list[dict[str, Any]], declared_rules: int = 500) -> list[dict[str, Any]]:
    """Account for all registered candidates, including no-feature/no-signal rules."""
    grouped: dict[tuple[float, float], list[dict[str, Any]]] = defaultdict(list)
    seen = set()
    for row in summaries:
        key = (str(row["candidate_id"]), float(row["latency_ms"]), float(row["requested_shares"]))
        if key in seen:
            raise ValueError(f"duplicate runner candidate/latency/size summary {key}")
        seen.add(key)
        grouped[key[1:]].append(row)
    output = []
    feature_reasons = ("missing_or_invalid_feature:", "missing_feature_availability:", "missing_feature_maps")
    for (latency, size), rows in sorted(grouped.items()):
        no_valid = [s for s in rows if not s.get("valid_feature_rows", 0)]
        missing = [s for s in no_valid if s.get("invalid_decision_reasons") and
                   all(k.startswith(feature_reasons) for k in s["invalid_decision_reasons"])]
        def count_positive(field):
            return sum((s.get(field) or 0) > 0 for s in rows)
        output.append({"latency_ms": latency, "requested_shares": size,
                       "declared_rules": declared_rules, "registered_rules_in_runner": len(rows),
                       "missing_runner_summaries": max(0, declared_rules - len(rows)),
                       "no_valid_feature_rows": len(no_valid),
                       "fully_blocked_missing_or_invalid_features": len(missing),
                       "evaluable_with_valid_features": sum(s.get("valid_feature_rows", 0) > 0 and s.get("status") != "blocked_missing_data_provenance" for s in rows),
                       "rules_with_signal_attempt": count_positive("attempts"),
                       "rules_with_submitted_order": count_positive("submitted_orders"),
                       "rules_with_fill": count_positive("filled_shares"),
                       "rules_with_settled_fill": count_positive("settled_positions"),
                       "rules_with_positive_complete_net_pnl": sum(s.get("status") == "simulated_complete" and (s.get("net_pnl_usd") or 0) > 0 for s in rows),
                       "confirmed_strategies": 0,
                       "status_counts": dict(sorted(Counter(s.get("status", "unknown") for s in rows).items()))})
    return output


def analyze(payload: dict[str, Any] | list[dict[str, Any]], *, family_tests: int = 500,
            primary_tests_per_rule: int = 1, primary_latency_ms: float = 500,
            primary_requested_shares: float = 5,
            bootstrap_reps: int = 10000, seed: int = 500) -> dict[str, Any]:
    """Return evidence statistics; ``qualified`` is always False here."""
    if isinstance(payload, list):
        payload = {"attempts": payload}
    raw = payload.get("attempts")
    if not isinstance(raw, list):
        raise ValueError("input requires an attempts list")
    rows = []
    for i, row in enumerate(raw):
        try:
            rows.append(normalize_attempt(row))
        except (TypeError, ValueError, OverflowError, OSError) as exc:
            raise ValueError(f"attempt row {i}: {exc}") from exc
    groups: dict[tuple[str, float, float], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[(r["candidate_id"], r["latency_ms"], r["requested_shares"])].append(r)
    candidate_count = len({r["candidate_id"] for r in rows})
    primary_sizes: dict[str, set[float]] = defaultdict(set)
    for candidate, latency, size in groups:
        if latency == primary_latency_ms:
            primary_sizes[candidate].add(size)
    tests_per_rule = max(1, primary_tests_per_rule, max(map(len, primary_sizes.values()), default=1))
    n_tests = max(500, family_tests, candidate_count) * tests_per_rule
    results = []
    for (candidate, latency, size), attempts in sorted(groups.items()):
        result = {"candidate_id": candidate, "latency_ms": latency, "requested_shares": size,
                  "latency_role": "primary" if latency == primary_latency_ms else "stress_only",
                  "size_role": "primary" if size == primary_requested_shares else "capacity_stress_only",
                  "is_primary_test": latency == primary_latency_ms and size == primary_requested_shares,
                  **_aggregation(attempts)}
        days, markets = defaultdict(list), defaultdict(list)
        for r in attempts:
            days[r["decision_day_utc"]].append(r)
            markets[r["market_id"]].append(r)
        result["utc_days"] = [{"day": d, **_aggregation(items)} for d, items in sorted(days.items())]
        result["market_clusters"] = [{"market_id": m, **_aggregation(items)} for m, items in sorted(markets.items())]
        result["attempt_days"] = len(days)
        fills = [r for r in attempts if r["settled"] and r["filled_shares"] > EPS]
        result["settlement_days"] = len({r["settlement_day_utc"] for r in fills})
        result["settled_fill_days"] = len({r["decision_day_utc"] for r in fills})
        derived_seed = seed + int(hashlib.sha256(f"{candidate}|{latency}|{size}".encode()).hexdigest()[:8], 16)
        result["day_cluster_bootstrap"] = day_cluster_bootstrap(result["utc_days"], reps=bootstrap_reps, seed=derived_seed)
        result["fair_price_test"] = fair_price_test(fills, n_tests)
        if result["unsettled_fills"] or result["incomplete_match_horizon"]:
            reason = "unsettled_inventory" if result["unsettled_fills"] else "incomplete_match_horizon"
            result["fair_price_test"] = {"status": "not_applicable", "reason": reason,
                                         "p_value": None, "bonferroni_p_value": None, "family_tests": n_tests,
                                         "alpha": 0.01}
            result["day_cluster_bootstrap"].update(status="incomplete_sample", lower_bound_net_usd_per_share=None)
        result["additional_cost_sensitivity"] = [
            {"extra_cents_per_filled_share": cents,
             "settled_net_pnl_usd": None if result["realized_pnl_usd"] is None else result["realized_pnl_usd"] - cents / 100 * result["settled_shares"],
             "settled_net_usd_per_share": None if result["net_usd_per_settled_share"] is None else result["net_usd_per_settled_share"] - cents / 100,
             "extra_cost_usd_all_filled_shares": cents / 100 * result["filled_shares"]}
            for cents in (0.25, 0.5, 1.0)]
        summary = _runner_summary(payload, candidate, latency, size)
        cash_fields = ("max_realized_drawdown_usd", "min_available_cash_usd", "ending_cash_usd", "initial_cash_usd")
        result["cash_ledger"] = {"source": "runner_summary" if summary else "unavailable",
                                 "mark_to_market_drawdown_available": False,
                                 **{k: summary.get(k) if summary else None for k in cash_fields}}
        if summary and summary.get("net_pnl_usd") is not None and result["net_pnl_usd"] is not None:
            if not math.isclose(float(summary["net_pnl_usd"]), result["net_pnl_usd"], rel_tol=1e-8, abs_tol=EPS):
                raise ValueError(f"runner/attempt PnL mismatch: {candidate}/{latency}/{size}")
        if summary and summary.get("realized_pnl_usd") is not None and result["realized_pnl_usd"] is not None:
            if not math.isclose(float(summary["realized_pnl_usd"]), result["realized_pnl_usd"], rel_tol=1e-8, abs_tol=EPS):
                raise ValueError(f"runner/attempt realized PnL mismatch: {candidate}/{latency}/{size}")
        result["runner_status"] = summary.get("status") if summary else None
        if summary and summary.get("status") == "evaluated_no_fills" and summary.get("net_pnl_usd") == 0:
            result["net_pnl_usd"] = 0.0
        if summary and str(summary.get("status", "")).startswith(("incomplete_", "blocked_")):
            result["net_pnl_usd"] = None
            result["fair_price_test"] = {"status": "not_applicable", "reason": "runner_" + summary["status"],
                                         "p_value": None, "bonferroni_p_value": None, "family_tests": n_tests,
                                         "alpha": 0.01}
            result["day_cluster_bootstrap"].update(status="incomplete_sample", lower_bound_net_usd_per_share=None)
        result["evidence_class"] = "development_descriptive_only"
        result["qualified"] = False
        result["oos_verified"] = False
        result["qualification_reason"] = "Statistics do not verify unobserved holdout provenance, live execution or portfolio capacity. Development cannot qualify."
        results.append(result)
    capacity = []
    for candidate, latency in sorted({(c, l) for c, l, _ in groups}):
        actual_sizes = sorted(s for c, l, s in groups if c == candidate and l == latency)
        capacity.append({"candidate_id": candidate, "latency_ms": latency,
                         "sizes": [{"requested_shares": size, "status": "separately_replayed"} for size in actual_sizes],
                         "scope": "Only sizes present in this input; other independent batch size files are not assessed here.",
                         "depth_extrapolation_used": False,
                         "warning": "A separate simulated size run is not evidence of live fill capacity; runs must not be added together."})
    summaries = payload.get("summaries", payload.get("summary", []))
    if isinstance(summaries, dict):
        summaries = list(summaries.values())
    coverage_fields = ("candidate_id", "latency_ms", "requested_shares", "status", "attempts",
                       "valid_feature_rows", "invalid_decision_reasons", "effective_split", "submitted_orders",
                       "full_fills", "partial_fills", "filled_shares", "settled_positions", "net_pnl_usd")
    coverage = [{k: s.get(k) for k in coverage_fields} for s in summaries if isinstance(s, dict)] if isinstance(summaries, list) else []
    # Older hand-built summaries need not contain replay coverage information.
    auditable = [s for s in summaries if isinstance(s, dict) and all(s.get(k) is not None for k in ("candidate_id", "latency_ms", "requested_shares", "valid_feature_rows"))] if isinstance(summaries, list) else []
    dataset = payload.get("dataset")
    synthetic = isinstance(dataset, dict) and (
        dataset.get("split") == "synthetic" or
        isinstance(dataset.get("provenance"), dict) and dataset["provenance"].get("source") == "synthetic_fixture")
    evidence_class = "synthetic_validation_only" if synthetic else "development_descriptive_only"
    for result in results:
        result["evidence_class"] = evidence_class
    return {"schema": SCHEMA, "dataset": payload.get("dataset"), "split_validation": payload.get("split_validation"),
            "input_attempts": len(rows), "tested_candidates_with_attempts": candidate_count,
            "runner_candidate_count": len({s["candidate_id"] for s in coverage}), "candidate_run_coverage": coverage,
            "denominator_audit": denominator_audit(auditable, max(500, family_tests, candidate_count)),
            "declared_family_rules": max(500, family_tests, candidate_count),
            "primary_tests_per_rule": tests_per_rule, "bonferroni_tests": n_tests,
            "primary_latency_ms": primary_latency_ms, "qualified_strategy_count": 0,
            "primary_requested_shares": primary_requested_shares,
            "evidence_class": evidence_class, "results": results, "capacity_runs": capacity,
            "cost_convention": "cost_usd includes fees_usd; net_pnl_usd = payout_usd - cost_usd",
            "warnings": ["All PnL is simulated and net of recorded trading fees; no live order is inferred.",
                         "No attempts is missing evidence, not zero PnL or an OOS test.",
                         "Latency and size alternatives, overlapping rules and correlated markets are not an additive portfolio.",
                         "The 99% day-cluster bound is descriptive and is not multiplicity-adjusted. Bonferroni applies to the declared primary fair-price reference tests.",
                         "A positive development result requires frozen prospective testing; this report never marks it OOS or qualified."]}


def markdown_report(report: dict[str, Any]) -> str:
    label = "Synthetic validation only; no market performance evidence." if report["evidence_class"] == "synthetic_validation_only" else "Development evidence only."
    lines = ["# Mine500 simulated attempt statistics", "", f"{label} Confirmed strategies: **0**. No OOS claim is made.", "",
             f"Rules represented by attempts: {report['tested_candidates_with_attempts']}; declared family: {report['declared_family_rules']}; Bonferroni tests: {report['bonferroni_tests']}.", "",
             "Separate latency and size runs must not be summed as a portfolio.", "",
             "| Candidate | Lag ms | Size | Attempts / sent | Full / partial / no-fill | Settled | UTC fill days | Net USD | USD/share | 99% day lower | Corrected p |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    def fmt(value: Any) -> str:
        return "missing" if value is None else f"{value:.6g}"
    for r in report["results"]:
        lines.append(f"| {r['candidate_id']} | {r['latency_ms']:g} | {r['requested_shares']:g} | {r['attempts']} / {r['sends']} | {r['full_fills']} / {r['partial_fills']} / {r['no_fills']} | {r['settled_fills']} | {r['settled_fill_days']} | {fmt(r['net_pnl_usd'])} | {fmt(r['net_usd_per_settled_share'])} | {fmt(r['day_cluster_bootstrap']['lower_bound_net_usd_per_share'])} | {fmt(r['fair_price_test']['bonferroni_p_value'])} |")
    lines.extend(["", "Candidate denominator by independent latency/size run:", "",
                  "| Lag ms | Size | Registered | Missing/invalid features | Evaluable | Signal | Submitted | Filled | Settled | Positive complete PnL | Confirmed |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"])
    for d in report["denominator_audit"]:
        lines.append(f"| {d['latency_ms']:g} | {d['requested_shares']:g} | {d['registered_rules_in_runner']} | {d['fully_blocked_missing_or_invalid_features']} | {d['evaluable_with_valid_features']} | {d['rules_with_signal_attempt']} | {d['rules_with_submitted_order']} | {d['rules_with_fill']} | {d['rules_with_settled_fill']} | {d['rules_with_positive_complete_net_pnl']} | 0 |")
    lines.extend(["", "Fewer than seven UTC days with settled fills: uncertainty is insufficient. An independent-Bernoulli fair-price reference is available only with one settled entry per market, equal actual shares and binary payouts.", "",
                  "JSON includes rejected attempts, unmatched horizons, pending inventory, market/day clusters, 0.25/0.5/1-cent per-share cost stresses, runner cash statistics and separate-size replay evidence actually present in this input.", ""])
    lines.extend(f"- {w}" for w in report["warnings"])
    return "\n".join(lines) + "\n"


def load_attempts(path: str | Path) -> dict[str, Any] | list[dict[str, Any]]:
    path = Path(path)
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    if path.suffix.lower() in (".jsonl", ".ndjson"):
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempts", required=True)
    parser.add_argument("--out", required=True, help="Output JSON path; sibling .md is also written")
    parser.add_argument("--family-tests", type=int, default=500)
    parser.add_argument("--primary-tests-per-rule", type=int, default=1)
    parser.add_argument("--primary-latency-ms", type=float, default=500)
    parser.add_argument("--bootstrap-reps", type=int, default=10000)
    args = parser.parse_args(argv)
    report = analyze(load_attempts(args.attempts), family_tests=args.family_tests,
                     primary_tests_per_rule=args.primary_tests_per_rule, primary_latency_ms=args.primary_latency_ms,
                     bootstrap_reps=args.bootstrap_reps)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    out.with_suffix(".md").write_text(markdown_report(report), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
