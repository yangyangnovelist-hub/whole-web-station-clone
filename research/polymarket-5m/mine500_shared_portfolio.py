"""Combine already-produced candidate attempts into one causal cash ledger.

This module does not replay books or generate signals.  It consumes complete
attempt ledgers from ``mine500_replay.py``, chooses at most one first signal per
market under a frozen panel policy, and recomputes reservation, matching and
settlement on one cash account.  Results inherit the input split: development
attempts never become OOS merely because they are combined here.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

EPS = 1e-9


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_document(doc):
    if doc.get("schema_version") != "mine500_replay_v1":
        raise ValueError("unsupported replay schema")
    if doc.get("split_validation", {}).get("effective_split") not in (
            "development", "declared_prospective_holdout"):
        raise ValueError("missing effective split")
    if doc.get("capital_scope") != "each_candidate_and_latency_has_independent_cash_do_not_sum":
        raise ValueError("input is not the expected isolated-ledger replay")
    attempts = doc.get("attempts")
    if not isinstance(attempts, list):
        raise ValueError("missing attempts")
    for row in attempts:
        required = ("candidate_id", "latency_ms", "market_id", "decision_ms", "match_ms",
                    "side", "requested_shares", "submitted", "status", "filled_shares",
                    "cost_usd", "fees_usd", "reserved_usd_at_decision")
        if any(k not in row for k in required):
            raise ValueError("attempt row missing required field")
        if row["side"] not in ("Up", "Down") or not all(_finite(row[k]) for k in
                ("decision_ms", "match_ms", "requested_shares", "filled_shares",
                 "cost_usd", "fees_usd", "reserved_usd_at_decision")):
            raise ValueError("invalid attempt row")
        if row["filled_shares"] > EPS:
            if not row.get("settled") or not _finite(row.get("settlement_ms")) or not _finite(row.get("payout_usd")):
                raise ValueError("filled row is not completely settled")
    return attempts


def select_coverage_representatives(catalog, doc, latency_ms=500):
    """One representative per evaluable family, using coverage but never PnL."""
    family = {row["id"]: row["family"] for row in catalog}
    grouped = defaultdict(list)
    for row in doc["summary"]:
        if row["latency_ms"] == latency_ms and row["status"] == "simulated_complete":
            grouped[family[row["candidate_id"]]].append(row)
    selected = {}
    for name, rows in grouped.items():
        # ID is the stable final tie-break.  net_pnl_usd is deliberately absent.
        row = sorted(rows, key=lambda x: (-x["settled_positions"], -x["submitted_orders"],
                                          x["candidate_id"]))[0]
        selected[name] = row["candidate_id"]
    all_families = sorted(set(family.values()))
    return {"candidate_ids": [selected[x] for x in sorted(selected)],
            "family_to_candidate": dict(sorted(selected.items())),
            "blocked_families": sorted(set(all_families) - set(selected)),
            "selection_uses_pnl": False,
            "selection_rule": "max settled positions, then submitted orders, then smallest candidate id, at 500ms"}


def first_signal_per_market(attempts, candidate_ids, latency_ms):
    ids = set(candidate_ids)
    if len(ids) != len(candidate_ids) or not ids:
        raise ValueError("panel candidate IDs must be unique and nonempty")
    filtered = [row for row in attempts if row["candidate_id"] in ids and row["latency_ms"] == latency_ms]
    present = {row["candidate_id"] for row in filtered}
    missing = ids - present
    if missing:
        raise ValueError("panel candidates missing from ledger: " + ",".join(sorted(missing)))
    by_market = defaultdict(list)
    for row in filtered:
        by_market[row["market_id"]].append(row)
    chosen, diagnostics = [], []
    for market_id, rows in sorted(by_market.items()):
        rows.sort(key=lambda x: (x["decision_ms"], x["candidate_id"]))
        selected = rows[0]
        chosen.append(dict(selected))
        diagnostics.append({"market_id": market_id,
                            "selected_candidate_id": selected["candidate_id"],
                            "selected_decision_ms": selected["decision_ms"],
                            "selected_side": selected["side"],
                            "signals": len(rows),
                            "suppressed_signals": len(rows) - 1,
                            "candidate_ids": sorted({row["candidate_id"] for row in rows}),
                            "sides": sorted({row["side"] for row in rows}),
                            "opposite_side_conflict": len({row["side"] for row in rows}) > 1})
    return chosen, diagnostics


def simulate_shared_cash(chosen, initial_cash=1000.0, extra_cost_per_share=0.0):
    if not _finite(initial_cash) or initial_cash <= 0:
        raise ValueError("initial cash must be positive")
    if not _finite(extra_cost_per_share) or extra_cost_per_share < 0:
        raise ValueError("invalid extra cost")
    events = []
    for index, row in enumerate(chosen):
        events.append((row["decision_ms"], 0, row["candidate_id"], row["market_id"], index, "decision"))
        if row["submitted"]:
            events.append((row["match_ms"], 1, row["candidate_id"], row["market_id"], index, "match"))
            if row["filled_shares"] > EPS:
                events.append((row["settlement_ms"], 2, row["candidate_id"], row["market_id"], index, "settle"))
    # At equal timestamps, decision before releases is explicitly conservative.
    events.sort()
    cash, reserved = float(initial_cash), 0.0
    min_available = float(initial_cash)
    realized = 0.0
    peak_equity = float(initial_cash)
    max_drawdown = 0.0
    records = [dict(row, shared_status="pending", shared_cash_rejected=False,
                    shared_reservation_usd=0.0, shared_cost_usd=0.0,
                    shared_extra_cost_usd=0.0, shared_pnl_usd=None) for row in chosen]
    for _, _, _, _, index, kind in events:
        source, row = chosen[index], records[index]
        if kind == "decision":
            if not source["submitted"]:
                row["shared_status"] = source["status"]
                continue
            reservation = source["reserved_usd_at_decision"] + source["requested_shares"] * extra_cost_per_share
            row["shared_reservation_usd"] = reservation
            row["shared_cash_before_decision"] = cash
            row["shared_available_before_decision"] = cash - reserved
            if cash - reserved + EPS < reservation:
                row["shared_status"] = "rejected_shared_cash"
                row["shared_cash_rejected"] = True
                continue
            reserved += reservation
            min_available = min(min_available, cash - reserved)
            row["shared_status"] = "submitted"
        elif kind == "match":
            if row["shared_cash_rejected"]:
                continue
            reserved -= row["shared_reservation_usd"]
            if reserved < -EPS:
                raise AssertionError("negative reserved cash")
            reserved = max(0.0, reserved)
            extra = source["filled_shares"] * extra_cost_per_share
            cost = source["cost_usd"] + extra
            if cost > cash + EPS:
                raise AssertionError("accepted reservation cannot cover match cost")
            cash -= cost
            min_available = min(min_available, cash - reserved)
            row.update(shared_status=source["status"], shared_cost_usd=cost,
                       shared_extra_cost_usd=extra, shared_cash_after_match=cash)
            if source["filled_shares"] <= EPS:
                row["shared_pnl_usd"] = 0.0
        else:
            if row["shared_cash_rejected"]:
                continue
            payout = source["payout_usd"]
            cash += payout
            pnl = payout - row["shared_cost_usd"]
            realized += pnl
            equity = initial_cash + realized
            peak_equity = max(peak_equity, equity)
            max_drawdown = max(max_drawdown, peak_equity - equity)
            row.update(shared_pnl_usd=pnl, shared_cash_after_settlement=cash,
                       shared_realized_equity_after_settlement=equity)
    if abs(reserved) > EPS:
        raise ValueError("open reservations remain")
    accepted_fills = [r for r in records if not r["shared_cash_rejected"] and r["filled_shares"] > EPS]
    if any(r["shared_pnl_usd"] is None for r in accepted_fills):
        raise ValueError("unsettled shared fill")
    total_pnl = math.fsum(r["shared_pnl_usd"] for r in accepted_fills)
    if not math.isclose(cash, initial_cash + total_pnl, abs_tol=1e-7):
        raise AssertionError("ending cash does not reconcile")
    status_counts = Counter(r["shared_status"] for r in records)
    shares = math.fsum(r["filled_shares"] for r in accepted_fills)
    return records, {"initial_cash_usd": initial_cash, "ending_cash_usd": cash,
                     "net_pnl_usd": total_pnl,
                     "net_pnl_per_filled_share_usd": total_pnl / shares if shares else None,
                     "min_available_cash_usd": min_available,
                     "max_realized_drawdown_usd": max_drawdown,
                     "drawdown_basis": "realized_only_no_mark_to_market",
                     "attempted_markets": len(records),
                     "submitted_orders": sum(r["submitted"] and not r["shared_cash_rejected"] for r in records),
                     "cash_rejections": sum(r["shared_cash_rejected"] for r in records),
                     "filled_orders": len(accepted_fills), "filled_shares": shares,
                     "original_fees_usd": math.fsum(r["fees_usd"] for r in accepted_fills),
                     "extra_costs_usd": math.fsum(r["shared_extra_cost_usd"] for r in accepted_fills),
                     "status_counts": dict(status_counts)}


def run(documents, catalog, plan):
    outputs, records, conflicts = [], [], []
    for doc in documents:
        attempts = validate_document(doc)
        shares = doc["config"]["shares"]
        for panel in plan["panels"]:
            for latency in plan["latencies_ms"]:
                chosen, diagnostic = first_signal_per_market(attempts, panel["candidate_ids"], latency)
                for extra in plan["extra_costs_per_share"]:
                    rows, result = simulate_shared_cash(chosen, plan["initial_cash_usd"], extra)
                    result.update(panel_id=panel["panel_id"], panel_selection=panel["selection"],
                                  selection_uses_pnl=panel["selection_uses_pnl"],
                                  candidate_ids=panel["candidate_ids"], requested_shares=shares,
                                  latency_ms=latency, extra_cost_per_share=extra,
                                  candidate_signals=sum(x["signals"] for x in diagnostic),
                                  suppressed_signals=sum(x["suppressed_signals"] for x in diagnostic),
                                  markets_with_opposite_side_conflict=sum(x["opposite_side_conflict"] for x in diagnostic),
                                  effective_split=doc["split_validation"]["effective_split"],
                                  confirmed_edge=False)
                    outputs.append(result)
                    for row in rows:
                        row.update(panel_id=panel["panel_id"], requested_shares=shares,
                                   portfolio_latency_ms=latency, extra_cost_per_share=extra)
                    records.extend(rows)
                for row in diagnostic:
                    row.update(panel_id=panel["panel_id"], requested_shares=shares,
                               portfolio_latency_ms=latency)
                conflicts.extend(diagnostic)
    return outputs, records, conflicts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", nargs="+", required=True)
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    paths = [Path(x).resolve() for x in args.inputs]
    catalog_path, plan_path = Path(args.catalog).resolve(), Path(args.plan).resolve()
    plan = json.loads(plan_path.read_text())
    expected = {str(Path(x["path"]).resolve()): x["sha256"] for x in plan["inputs"]}
    for path in paths:
        if digest(path) != expected.get(str(path)):
            raise ValueError("input hash mismatch: " + path.name)
    if digest(catalog_path) != plan["catalog_sha256"]:
        raise ValueError("catalog hash mismatch")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=False)
    documents = [json.loads(path.read_text()) for path in paths]
    catalog = json.loads(catalog_path.read_text())
    results, records, conflicts = run(documents, catalog, plan)
    metadata = {"schema": "mine500_shared_portfolio_v1", "simulation_only": True,
                "is_new_signal_backtest": False, "is_new_oos": False,
                "plan": plan, "aggregator_code_sha256": digest(__file__),
                "source_replay_code_sha256": sorted({x["replay_code_sha256"] for x in documents}),
                "limitations": ["Input fills remain displayed-depth upper bounds.",
                                "Portfolio panels were defined on already-consumed development data.",
                                "At most one earliest signal consumes a market globally; no later replacement.",
                                "Equal-timestamp cash releases are ordered after decisions conservatively.",
                                "No mark-to-market drawdown, queue probability, maker fills or two-leg inventory."]}
    (out / "metadata.json").write_text(json.dumps(metadata, indent=2, allow_nan=False) + "\n")
    (out / "summary.json").write_text(json.dumps(results, indent=2, allow_nan=False) + "\n")
    (out / "attempts.json").write_text(json.dumps(records, indent=2, allow_nan=False) + "\n")
    (out / "conflicts.json").write_text(json.dumps(conflicts, indent=2, allow_nan=False) + "\n")
    columns = sorted(set().union(*(row.keys() for row in results)) - {"candidate_ids", "status_counts"})
    with (out / "summary.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader(); writer.writerows(results)
    print(json.dumps({"scenarios": len(results), "attempt_rows": len(records),
                      "conflict_rows": len(conflicts), "new_oos": 0}))


if __name__ == "__main__":
    main()
