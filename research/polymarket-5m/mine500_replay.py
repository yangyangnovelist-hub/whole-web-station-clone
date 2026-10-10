"""Receipt-clock, cash-constrained research replay for a registered candidate set.

This is a displayed-depth upper-bound simulation, never evidence of actual fills.
Input JSONL is in nondecreasing local ``recv_ms`` order. Records: dataset,
market, book, decision and outcome; see ``SCHEMA`` below. Total latency already
includes any exchange waiting period. No additional 150 ms is added.

The runner deliberately does not download data, submit orders or tune signals.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
import hashlib
import heapq
import gzip
import importlib
import inspect
import json
import math
from pathlib import Path
from typing import Any, Callable, Iterable


SCHEMA = {
    "dataset": ["recv_ms", "dataset_id", "split", "provenance"],
    "market": ["recv_ms", "market_id", "start_ms", "end_ms", "label_source"],
    "book": ["recv_ms", "market_id", "up_asks", "down_asks", "healthy", "fee_rate"],
    "decision": ["recv_ms", "market_id", "features", "feature_available_ms"],
    "outcome": ["recv_ms", "market_id", "resolved_ms", "winner", "source"],
}
OFFICIAL_SOURCES = frozenset({"polymarket", "gamma", "chainlink"})
EPS = 1e-9


@dataclass(frozen=True)
class ReplayConfig:
    latencies_ms: tuple[int, ...] = (500, 600, 1000)
    shares: float = 5.0
    initial_cash_usd: float = 1000.0
    max_quote_age_ms: int = 250

    def __post_init__(self):
        if not self.latencies_ms or any(type(x) is not int or x <= 0 for x in self.latencies_ms):
            raise ValueError("latencies must be positive integer total milliseconds")
        if len(set(self.latencies_ms)) != len(self.latencies_ms):
            raise ValueError("duplicate latency buckets are not independent results")
        if not _number(self.shares) or self.shares <= 0:
            raise ValueError("shares must be positive and finite")
        if not _number(self.initial_cash_usd) or self.initial_cash_usd <= 0:
            raise ValueError("initial_cash_usd must be positive and finite")
        if type(self.max_quote_age_ms) is not int or self.max_quote_age_ms < 0:
            raise ValueError("max_quote_age_ms must be a nonnegative integer")


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _levels(value):
    if not isinstance(value, list):
        raise ValueError("missing_asks")
    levels = defaultdict(float)
    for row in value:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            raise ValueError("invalid_asks")
        price, size = row
        if not _number(price) or not 0 < price < 1 or not _number(size) or size < 0:
            raise ValueError("invalid_asks")
        if size:
            levels[float(price)] += float(size)
    return sorted(levels.items())


def _book(raw, now, config):
    if raw is None:
        return None, "missing_book"
    if raw.get("healthy") is not True:
        return None, "unhealthy_book"
    age = now - raw["recv_ms"]
    if age < 0 or age > config.max_quote_age_ms:
        return None, "stale_book"
    for side in ("up", "down"):
        side_recv = raw.get(side + "_recv_ms", raw["recv_ms"])
        if not _number(side_recv) or not 0 <= now - side_recv <= config.max_quote_age_ms:
            return None, "stale_" + side + "_book"
    fee = raw.get("fee_rate")
    if not _number(fee) or not 0 <= fee <= 1:
        return None, "missing_or_invalid_fee"
    try:
        return {"Up": _levels(raw.get("up_asks")),
                "Down": _levels(raw.get("down_asks")), "fee_rate": float(fee)}, None
    except ValueError as exc:
        return None, str(exc)


def _fee(shares, price, rate):
    return shares * rate * price * (1 - price)


def read_jsonl(path):
    """Stream records; malformed input is an error, not a missing-market filter."""
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON at line {line_number}") from exc
            if not isinstance(event, dict):
                raise ValueError(f"record at line {line_number} is not an object")
            yield event


def _split_validation(dataset, candidates, config, first_decision, actual_signal_hash):
    requested = dataset.get("split", "development") if dataset else "development"
    actual = "development"
    reasons = []
    if requested != "development":
        freeze = dataset.get("freeze", {})
        frozen_at = freeze.get("frozen_at_ms")
        if not _number(frozen_at) or first_decision is None or frozen_at >= first_decision:
            reasons.append("missing_or_late_freeze")
        if freeze.get("candidate_sha256") != fingerprint(candidates):
            reasons.append("candidate_hash_not_frozen")
        if freeze.get("execution_sha256") != fingerprint(asdict(config)):
            reasons.append("execution_hash_not_frozen")
        replay_code_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        if freeze.get("replay_code_sha256") != replay_code_hash:
            reasons.append("replay_implementation_not_frozen")
        adapter_hash = dataset.get("adapter_code_sha256")
        if not adapter_hash or freeze.get("adapter_code_sha256") != adapter_hash:
            reasons.append("input_adapter_not_frozen")
        if freeze.get("signal_code_sha256") != dataset.get("signal_code_sha256") or not freeze.get("signal_code_sha256"):
            reasons.append("signal_code_not_frozen")
        if not actual_signal_hash or dataset.get("signal_code_sha256") != actual_signal_hash:
            reasons.append("executed_signal_code_hash_mismatch")
        if dataset.get("untouched_attestation") is not True:
            reasons.append("no_untouched_data_attestation")
        if not reasons:
            actual = "declared_prospective_holdout"
    return {"requested_split": requested, "effective_split": actual,
            "reasons": reasons,
            "qualification": "Attestations are provenance claims, not proof the data was never inspected."}


def replay_events(events: Iterable[dict], candidates: list[dict],
                  signal_fn: Callable[[dict, dict], str | None],
                  config: ReplayConfig | None = None) -> dict:
    """Replay each candidate/latency on its own isolated starting-cash ledger.

    All feature availability timestamps are mandatory. Invalid feature rows are
    counted as decision rejections, not evaluated with future data. Once a causal
    signal qualifies, that candidate's market is consumed even if its order fails.
    The first observed price on the selected side freezes the FAK limit.
    """
    config = config or ReplayConfig()
    ids = [str(c["id"]) for c in candidates]
    if len(ids) != len(set(ids)):
        raise ValueError("candidate IDs must be unique")
    if any(not isinstance(c.get("required_features"), list) for c in candidates):
        raise ValueError("every candidate requires an explicit required_features list")
    books, markets, outcomes = {}, {}, {}
    seen = set()
    attempts = []
    queue = []
    sequence = 0
    diagnostics = Counter()
    invalid_by_candidate = defaultdict(Counter)
    valid_by_candidate = Counter()
    dataset = None
    first_decision = None
    last_recv = None
    ledgers = {}
    open_by_market = defaultdict(list)
    for cid in ids:
        for latency in config.latencies_ms:
            ledgers[(cid, latency)] = {
                "cash": config.initial_cash_usd, "reserved": 0.0,
                "min_available": config.initial_cash_usd, "realized_pnl": 0.0,
                "peak_realized_equity": config.initial_cash_usd, "max_drawdown": 0.0,
            }

    def schedule(at, kind, payload):
        nonlocal sequence
        sequence += 1
        heapq.heappush(queue, (at, sequence, kind, payload))

    def settle(attempt, winner, when):
        if attempt["settled"] or not attempt["filled_shares"]:
            return
        ledger = ledgers[(attempt["candidate_id"], attempt["latency_ms"])]
        payout = attempt["filled_shares"] if attempt["side"] == winner else 0.0
        pnl = payout - attempt["cost_usd"]
        ledger["cash"] += payout
        ledger["realized_pnl"] += pnl
        equity = config.initial_cash_usd + ledger["realized_pnl"]
        ledger["peak_realized_equity"] = max(ledger["peak_realized_equity"], equity)
        ledger["max_drawdown"] = max(ledger["max_drawdown"], ledger["peak_realized_equity"] - equity)
        attempt.update(settled=True, settlement_ms=when, winner=winner,
                       payout_usd=payout, net_pnl_usd=pnl,
                       cash_after_settlement=ledger["cash"], realized_equity_after_settlement=equity)

    def execute(attempt, when, horizon=None):
        ledger = ledgers[(attempt["candidate_id"], attempt["latency_ms"])]
        ledger["reserved"] -= attempt["reserved_usd_at_decision"]
        ledger["reserved"] = max(0.0, ledger["reserved"])
        # Later label receipts do not extend recorded order-book coverage.
        recorded_end = dataset.get("coverage_end_ms") if dataset else None
        if _number(recorded_end) and when > recorded_end:
            attempt["status"] = "incomplete_match_horizon"
            return
        if horizon is not None and when > horizon:
            attempt["status"] = "incomplete_match_horizon"
            return
        market = markets.get(attempt["market_id"])
        if market is None or when >= market["end_ms"]:
            attempt["status"] = "rejected_market_closed_at_match"
            return
        book, error = _book(books.get(attempt["market_id"]), when, config)
        if error:
            attempt["status"] = "rejected_" + error + "_at_match"
            return
        if abs(book["fee_rate"] - attempt["fee_rate_decision"]) > EPS:
            attempt["status"] = "rejected_fee_changed_after_decision"
            return
        remaining, notional, fees = config.shares, 0.0, 0.0
        for price, size in book[attempt["side"]]:
            if price > attempt["limit_price"] + EPS or remaining <= EPS:
                break
            quantity = min(size, remaining)
            notional += quantity * price
            fees += _fee(quantity, price, book["fee_rate"])
            remaining -= quantity
        filled = config.shares - remaining
        cost = notional + fees
        if cost > ledger["cash"] + EPS:
            raise AssertionError("reserved order cost exceeds ledger cash")
        ledger["cash"] -= cost
        ledger["min_available"] = min(ledger["min_available"], ledger["cash"] - ledger["reserved"])
        attempt.update(status="no_fill" if filled <= EPS else ("filled_full" if remaining <= EPS else "filled_partial"),
                       filled_shares=filled, fill_vwap=notional / filled if filled > EPS else None,
                       fees_usd=fees, cost_usd=cost, cash_after_match=ledger["cash"],
                       book_recv_ms_at_match=books[attempt["market_id"]]["recv_ms"])
        if filled > EPS:
            open_by_market[attempt["market_id"]].append(attempt)
            outcome = outcomes.get(attempt["market_id"])
            if outcome and outcome["known_ms"] <= when:
                settle(attempt, outcome["winner"], when)
        else:
            attempt["net_pnl_usd"] = 0.0

    def drain(before, inclusive=False, horizon=None):
        while queue and (queue[0][0] < before or (inclusive and queue[0][0] == before)):
            when, _, kind, payload = heapq.heappop(queue)
            if kind == "match":
                execute(payload, when, horizon)
            elif horizon is None or when <= horizon:
                for attempt in open_by_market.get(payload, []):
                    settle(attempt, outcomes[payload]["winner"], when)

    def process(event):
        nonlocal dataset, first_decision
        now, kind = event["recv_ms"], event.get("type")
        diagnostics["input_" + str(kind)] += 1
        if kind == "dataset":
            if dataset is not None:
                raise ValueError("multiple dataset headers are not allowed")
            dataset = event
            if event.get("book_resolution") == "decision_and_match_projection":
                supported = event.get("supported_latencies_ms")
                grid = event.get("decision_grid_ms")
                if (not isinstance(supported, list) or
                        any(type(v) is not int or v <= 0 for v in supported) or
                        not set(config.latencies_ms).issubset(supported)):
                    raise ValueError("projection does not support requested total latency")
                if type(grid) is not int or grid <= 0:
                    raise ValueError("projection requires fixed positive decision_grid_ms")
            return
        market_id = event.get("market_id")
        if not isinstance(market_id, str) or not market_id:
            diagnostics["rejected_missing_market_id"] += 1
            return
        if kind == "market":
            start, end = event.get("start_ms"), event.get("end_ms")
            if not _number(start) or not _number(end) or end - start != 300000 or event.get("label_source") not in OFFICIAL_SOURCES:
                diagnostics["rejected_invalid_market_metadata"] += 1
                markets.pop(market_id, None)
                return
            previous = markets.get(market_id)
            if previous and any(previous[key] != event[key] for key in ("start_ms", "end_ms", "label_source")):
                raise ValueError("market settlement definition changed during replay")
            markets[market_id] = event
            return
        if kind == "book":
            if (dataset and dataset.get("book_resolution") == "decision_and_match_projection"
                    and any(name not in event for name in ("up_recv_ms", "down_recv_ms"))):
                raise ValueError("projected books must preserve both original side receipt timestamps")
            # Invalid new books replace old books too. Never resurrect stale depth.
            books[market_id] = event
            return
        if kind == "outcome":
            market = markets.get(market_id)
            resolved = event.get("resolved_ms")
            if (market is None or event.get("winner") not in ("Up", "Down")
                    or event.get("source") not in OFFICIAL_SOURCES
                    or event.get("source") != market["label_source"]
                    or not _number(resolved) or resolved < market["end_ms"]):
                diagnostics["rejected_invalid_or_unofficial_outcome"] += 1
                return
            prior = outcomes.get(market_id)
            if prior:
                if prior["winner"] != event["winner"]:
                    raise ValueError("conflicting official outcomes")
                diagnostics["duplicate_outcome"] += 1
                return
            known = max(now, resolved)
            outcomes[market_id] = {"winner": event["winner"], "known_ms": known}
            if known == now:
                for attempt in open_by_market.get(market_id, []):
                    settle(attempt, event["winner"], known)
            else:
                schedule(known, "settle", market_id)
            return
        if kind != "decision":
            diagnostics["rejected_unknown_record_type"] += 1
            return
        if (dataset and dataset.get("book_resolution") == "decision_and_match_projection"
                and abs(now / dataset["decision_grid_ms"] - round(now / dataset["decision_grid_ms"])) > 1e-6):
            raise ValueError("decision outside registered projection grid")
        first_decision = now if first_decision is None else min(first_decision, now)
        features, available = event.get("features"), event.get("feature_available_ms")
        market = markets.get(market_id)
        for candidate in candidates:
            cid = str(candidate["id"])
            if (cid, market_id) in seen:
                continue
            error = None
            if market is None or not market["start_ms"] <= now < market["end_ms"]:
                error = "missing_market_or_outside_window"
            elif not isinstance(features, dict) or not isinstance(available, dict):
                error = "missing_feature_maps"
            else:
                for name in set(candidate["required_features"]) | {"tau_s"}:
                    if not _number(features.get(name)):
                        error = "missing_or_invalid_feature:" + name
                        break
                    if not _number(available.get(name)):
                        error = "missing_feature_availability:" + name
                        break
                    if available[name] > now:
                        error = "future_feature:" + name
                        break
                if error is None and (not 0 <= features["tau_s"] <= 300 or
                        abs(features["tau_s"] - (market["end_ms"] - now) / 1000) > 0.001):
                    error = "invalid_tau_or_market_clock"
            if error:
                invalid_by_candidate[cid][error] += 1
                continue
            valid_by_candidate[cid] += 1
            try:
                # Do not expose an unchecked extra column to the signal function.
                visible = {name: features[name] for name in set(candidate["required_features"]) | {"tau_s"}}
                side = signal_fn(candidate, visible)
            except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
                invalid_by_candidate[cid]["signal_error:" + type(exc).__name__] += 1
                continue
            if side is None:
                continue
            if side not in ("Up", "Down"):
                raise ValueError(f"invalid signal return for {cid}: {side!r}")
            seen.add((cid, market_id))
            book, book_error = _book(books.get(market_id), now, config)
            for latency in config.latencies_ms:
                ledger = ledgers[(cid, latency)]
                attempt = {"candidate_id": cid, "latency_ms": latency, "market_id": market_id,
                    "decision_ms": now, "match_ms": now + latency, "side": side,
                    "requested_shares": config.shares, "limit_price": None,
                    "submitted": False,
                    "fee_rate_decision": None, "status": "scheduled", "filled_shares": 0.0,
                    "fill_vwap": None, "fees_usd": 0.0, "cost_usd": 0.0,
                    "settled": False, "settlement_ms": None, "winner": None,
                    "payout_usd": None, "net_pnl_usd": None,
                    "cash_before_decision": ledger["cash"], "cash_after_match": None,
                    "cash_after_settlement": None, "reserved_usd_at_decision": 0.0}
                attempts.append(attempt)
                minimum = market.get("min_order_size")
                if minimum is not None:
                    if not _number(minimum) or minimum <= 0:
                        attempt["status"] = "rejected_invalid_min_order_size_metadata"
                        continue
                    if config.shares + EPS < minimum:
                        attempt["status"] = "rejected_below_market_min_order_size"
                        continue
                if book_error:
                    attempt["status"] = "rejected_" + book_error + "_at_decision"
                    continue
                if not book[side]:
                    attempt["status"] = "rejected_no_depth_at_decision"
                    continue
                limit = book[side][0][0]
                fee = book["fee_rate"]
                reservation = config.shares * limit + _fee(config.shares, limit, fee)
                attempt.update(limit_price=limit, fee_rate_decision=fee)
                if ledger["cash"] - ledger["reserved"] + EPS < reservation:
                    attempt["status"] = "rejected_insufficient_available_cash"
                    continue
                attempt["reserved_usd_at_decision"] = reservation
                attempt["submitted"] = True
                ledger["reserved"] += reservation
                ledger["min_available"] = min(ledger["min_available"], ledger["cash"] - ledger["reserved"])
                schedule(now + latency, "match", attempt)

    # Buffer only one equal-timestamp group; matches see all events received by
    # their due timestamp. Decisions remain in observed receipt order within it.
    group = []
    for event in events:
        now = event.get("recv_ms")
        if not _number(now):
            raise ValueError("every record must include finite local recv_ms")
        if last_recv is not None and now < last_recv:
            raise ValueError("input is not sorted by local receipt time")
        if last_recv is not None and now != last_recv:
            drain(last_recv)
            for grouped_event in group:
                process(grouped_event)
            drain(last_recv, inclusive=True)
            group = []
            drain(now)
        group.append(event)
        last_recv = now
    if group:
        drain(last_recv)
        for event in group:
            process(event)
        drain(last_recv, inclusive=True)
    horizon = last_recv if last_recv is not None else -1
    # A declared endpoint may cover quiet periods. execute() separately ensures
    # later outcome receipts never extend the book-recording coverage endpoint.
    if dataset and _number(dataset.get("coverage_end_ms")):
        horizon = max(horizon, dataset["coverage_end_ms"])
    drain(math.inf, horizon=horizon)
    valid_provenance = bool(dataset and dataset.get("dataset_id") and isinstance(dataset.get("provenance"), dict)
                            and dataset["provenance"].get("source"))
    try:
        signal_path = inspect.getsourcefile(signal_fn)
        actual_signal_hash = hashlib.sha256(Path(signal_path).read_bytes()).hexdigest() if signal_path else None
    except (TypeError, OSError):
        actual_signal_hash = None
    split = _split_validation(dataset, candidates, config, first_decision, actual_signal_hash)
    by_key = defaultdict(list)
    for attempt in attempts:
        by_key[(attempt["candidate_id"], attempt["latency_ms"])].append(attempt)
    summary = []
    for cid in ids:
        for latency in config.latencies_ms:
            rows = by_key[(cid, latency)]
            ledger = ledgers[(cid, latency)]
            statuses = Counter(row["status"] for row in rows)
            filled = [row for row in rows if row["filled_shares"] > EPS]
            settled = [row for row in filled if row["settled"]]
            qty = sum(row["filled_shares"] for row in filled)
            incomplete = any(row["status"] == "incomplete_match_horizon" for row in rows) or len(filled) != len(settled)
            missing_execution_inputs = any(any(marker in row["status"] for marker in (
                "missing_book", "missing_or_invalid_fee", "missing_asks", "invalid_asks",
                "fee_changed_after_decision", "invalid_min_order_size_metadata")) for row in rows)
            if not valid_provenance:
                status, pnl = "blocked_missing_data_provenance", None
            elif not diagnostics["input_decision"]:
                status, pnl = "blocked_no_decision_data", None
            elif not valid_by_candidate[cid]:
                status, pnl = "blocked_no_valid_feature_rows", None
            elif not rows:
                status, pnl = "evaluated_no_signal", None
            elif statuses["rejected_below_market_min_order_size"]:
                status, pnl = "blocked_below_market_min_order_size", None
            elif missing_execution_inputs:
                status, pnl = "incomplete_missing_execution_inputs", None
            elif incomplete:
                status, pnl = "incomplete_unresolved_or_truncated", None
            elif not filled:
                status, pnl = "evaluated_no_fills", 0.0
            else:
                status, pnl = "simulated_complete", ledger["realized_pnl"]
            summary.append({"candidate_id": cid, "latency_ms": latency, "status": status,
                "effective_split": split["effective_split"], "attempts": len(rows),
                "requested_shares": config.shares,
                "submitted_orders": sum(r["submitted"] for r in rows),
                "markets": len({r["market_id"] for r in rows}),
                "decision_utc_days": len({int(r["decision_ms"] // 86400000) for r in rows}),
                "valid_feature_rows": valid_by_candidate[cid],
                "invalid_decision_reasons": dict(invalid_by_candidate[cid]),
                "status_counts": dict(statuses), "full_fills": statuses["filled_full"],
                "missing_execution_inputs": missing_execution_inputs,
                "partial_fills": statuses["filled_partial"], "filled_shares": qty,
                "settled_positions": len(settled), "unresolved_positions": len(filled) - len(settled),
                "net_pnl_usd": pnl, "realized_pnl_usd": ledger["realized_pnl"] if rows and valid_provenance else None,
                "net_pnl_per_filled_share_usd": pnl / qty if pnl is not None and qty else None,
                "fees_usd": sum(r["fees_usd"] for r in rows), "initial_cash_usd": config.initial_cash_usd,
                "ending_cash_usd": ledger["cash"], "min_available_cash_usd": ledger["min_available"],
                "max_realized_drawdown_usd": ledger["max_drawdown"],
                "drawdown_basis": "realized_only_no_mark_to_market", "confirmed_edge": False})
    return {"schema_version": "mine500_replay_v1", "dataset": dataset,
        "config": asdict(config), "candidate_sha256": fingerprint(candidates),
        "execution_sha256": fingerprint(asdict(config)), "split_validation": split,
        "replay_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "simulation_only": True, "fill_model": "displayed_depth_upper_bound_partial_FAK",
        "fee_calculation": "sum(quantity * fee_rate * fill_price * (1-fill_price)); unrounded, no rebates",
        "fee_rounding_limitation": "Venue-specific rounding/minimum collection is not modeled; formula is computed per filled price level.",
        "latency_clock": "local_decision_to_match_total_including_venue_wait",
        "capital_scope": "each_candidate_and_latency_has_independent_cash_do_not_sum",
        "diagnostics": dict(diagnostics), "summary": summary, "attempts": attempts}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--latencies", default="500,600,1000")
    parser.add_argument("--shares", type=float, default=5.0)
    parser.add_argument("--initial-cash", type=float, default=1000.0)
    parser.add_argument("--max-quote-age-ms", type=int, default=250)
    parser.add_argument("--catalog-module", default="mine500_catalog")
    args = parser.parse_args(argv)
    catalog = importlib.import_module(args.catalog_module)
    candidates = catalog.make_candidates()
    config = ReplayConfig(tuple(int(s) for s in args.latencies.split(",")), args.shares,
                          args.initial_cash, args.max_quote_age_ms)
    args.out.mkdir(parents=True, exist_ok=True)
    blocked = None
    if args.input is None or not args.input.is_file():
        blocked = "No normalized input file supplied; no replay or PnL calculation occurred."
        events = []
    else:
        events = read_jsonl(args.input)
    result = replay_events(events, candidates, catalog.signal, config)
    if blocked:
        result["blocked_reason"] = blocked
    result["candidate_count"] = len(candidates)
    result["required_feature_coverage"] = dict(Counter(f for c in candidates for f in c["required_features"]))
    source_path = Path(catalog.__file__)
    result["signal_code_sha256_actual"] = hashlib.sha256(source_path.read_bytes()).hexdigest()
    # A claimed signal hash is not enough: verify against code actually executed.
    if (result["split_validation"]["effective_split"] != "development" and
            (result["dataset"] or {}).get("signal_code_sha256") != result["signal_code_sha256_actual"]):
        result["split_validation"]["effective_split"] = "development"
        result["split_validation"]["reasons"].append("executed_signal_code_hash_mismatch")
        for row in result["summary"]:
            row["effective_split"] = "development"
    with (args.out / "attempts.jsonl").open("w", encoding="utf-8") as handle:
        for row in result.pop("attempts"):
            handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
    (args.out / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"candidate_count": len(candidates), "latency_count": len(config.latencies_ms),
                      "input_available": not bool(blocked), "simulation_only": True,
                      "summary_path": str(args.out / "summary.json")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
