"""Strict, paper-only replay adapter for the independent basis-wedge detector.

Only local receipt time orders events.  Execution reads direct token asks; it
never manufactures liquidity from the opposite token.
"""
from __future__ import annotations

import argparse
import heapq
import json
import math
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass, replace
from decimal import ROUND_HALF_UP, Decimal
from itertools import groupby
from pathlib import Path
from typing import Any

import basis_wedge as wedge
import h_replay_archive as archive
from h_replay_run import DirectMarketBook, TokenBook, _mapping_dict, _merged_events

EVALUATION_MS = (400.0, 500.0)
TARGET_SHARES = 5.0
TIME_SHIFT_MS = 5_000.0
NO_WEDGE_MAX_BP = 0.5
BASE_VARIANT = "base"
CONTROL_VARIANTS = (
    "no_wedge",
    "direction_reversal",
    "time_shift",
    "confirmation",
)
VARIANTS = (BASE_VARIANT, *CONTROL_VARIANTS)
_EPS = 1e-9


@dataclass
class _MarketState:
    book: DirectMarketBook
    fee_rate: float
    recv_ms: float = -math.inf


@dataclass(frozen=True)
class _Pending:
    due_ms: float
    sequence: int
    evaluation_ms: float
    epoch: int
    signal_id: str
    signal: wedge.WedgeSignal
    parent_signal_id: str | None = None
    variant: str = BASE_VARIANT
    decision_recv_ms: float = -math.inf
    scheduled_decision_recv_ms: float = -math.inf
    decision_book_recv_ms: float | None = None
    sent: bool = True
    terminal_reason: str | None = None


@dataclass(frozen=True)
class _DelayedDecision:
    due_ms: float
    sequence: int
    epoch: int
    signal: wedge.WedgeSignal
    parent_signal_id: str


def _unique_markets(
    token_index: dict[str, tuple[DirectMarketBook, str]],
    fees: dict[str, float],
    default_fee_rate: float,
) -> dict[str, _MarketState]:
    states: dict[str, _MarketState] = {}
    for market, _ in token_index.values():
        states.setdefault(
            market.market_id,
            _MarketState(market, fees.get(market.market_id, default_fee_rate)),
        )
    return states


def _reset_books(states: dict[str, _MarketState]) -> None:
    for state in states.values():
        state.book.up = TokenBook()
        state.book.down = TokenBook()
        state.book._effective_cache.clear()
        state.recv_ms = -math.inf


def _direct_pm_event(
    state: _MarketState,
    recv_ms: float,
    symbol: str,
) -> dict[str, object]:
    market = state.book
    return {
        "kind": "pm_book",
        "symbol": symbol,
        "market_id": market.market_id,
        "recv_ms": recv_ms,
        "end_ms": (market.slot + 300) * 1_000.0,
        "up_bids": tuple(sorted(market.up.bids.items(), reverse=True)),
        "up_asks": tuple(sorted(market.up.asks.items())),
        "down_bids": tuple(sorted(market.down.bids.items(), reverse=True)),
        "down_asks": tuple(sorted(market.down.asks.items())),
    }


def _rounded_fee(levels: list[tuple[float, float]], rate: float) -> float:
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


def _fill_direct(
    token: TokenBook,
    fixed_limit: float,
    target_shares: float,
    fee_rate: float,
) -> tuple[
    str,
    float,
    float | None,
    float | None,
    float | None,
    tuple[tuple[float, float], ...],
]:
    asks = tuple(sorted(token.asks.items())) if token.ready else ()
    if not token.ready:
        return "book_unavailable", 0.0, None, None, None, ()
    if not asks:
        return "no_direct_ask", 0.0, None, None, None, ()
    if asks[0][0] > fixed_limit + _EPS:
        return "ask_above_frozen_limit", 0.0, None, None, None, ()

    remaining = target_shares
    fills: list[tuple[float, float]] = []
    notional = 0.0
    for price, available in asks:
        if price > fixed_limit + _EPS:
            break
        take = min(remaining, available)
        if take <= 0.0:
            continue
        fills.append((price, take))
        notional += price * take
        remaining -= take
        if remaining <= _EPS:
            break
    filled_shares = target_shares - remaining
    if filled_shares <= _EPS:
        return "insufficient_direct_depth", 0.0, None, None, None, ()
    fee = _rounded_fee(fills, fee_rate)
    reason = "filled" if remaining <= _EPS else "partial_fill"
    return reason, filled_shares, notional / filled_shares, fee, notional, tuple(fills)


def _result_row(
    pending: _Pending,
    state: _MarketState,
    outcomes: dict[str, str],
    current_epoch: int,
    *,
    terminal_reason: str | None = None,
) -> dict[str, Any]:
    signal = pending.signal
    book_recv_ms = state.recv_ms if math.isfinite(state.recv_ms) else None
    reason = terminal_reason or pending.terminal_reason
    shares = 0.0
    fill_vwap = None
    fee = None
    notional = None
    fill_levels: tuple[tuple[float, float], ...] = ()
    if reason is None and pending.epoch != current_epoch:
        reason = "disconnect"
    elif reason is None and book_recv_ms is not None and book_recv_ms > pending.due_ms + _EPS:
        reason = "future_book_guard"
    elif reason is None:
        token = state.book.up if signal.buy_side == wedge.UP else state.book.down
        reason, shares, fill_vwap, fee, notional, fill_levels = _fill_direct(
            token,
            signal.fixed_limit,
            TARGET_SHARES,
            state.fee_rate,
        )

    filled = reason in {"filled", "partial_fill"}
    full_fill = reason == "filled"
    winner = outcomes.get(signal.market_id)
    won = winner.lower() == signal.buy_side if filled and winner is not None else None
    total_cost = notional + fee if filled and notional is not None and fee is not None else None
    all_in_cost = total_cost / shares if total_cost is not None and shares > 0 else None
    pnl = ((shares if won else 0.0) - total_cost
           if filled and won is not None and total_cost is not None else None)
    return {
        "signal_id": pending.signal_id,
        "parent_signal_id": pending.parent_signal_id,
        "paper_only": True,
        "ordering_clock": "local_receipt_ms",
        "match_book_clock": "local_receipt_ms",
        "market_id": signal.market_id,
        "variant": pending.variant,
        "signal_recv_ms": signal.recv_ms,
        "decision_recv_ms": pending.decision_recv_ms,
        "scheduled_decision_recv_ms": pending.scheduled_decision_recv_ms,
        "decision_book_recv_ms": pending.decision_book_recv_ms,
        "evaluation_ms": pending.evaluation_ms,
        "evaluation_recv_ms": pending.due_ms,
        "book_recv_ms": book_recv_ms,
        "basis_direction": signal.basis_direction,
        "buy_side": signal.buy_side,
        "fair": signal.fair,
        "decision_vwap": signal.ask,
        "fixed_limit": signal.fixed_limit,
        "signal_fee_per_share": signal.fee,
        "signal_net_edge": signal.net_edge,
        "target_shares": TARGET_SHARES,
        "filled_shares": shares if filled else 0.0,
        "fill_vwap": fill_vwap,
        "fill_levels": [
            {"price": price, "shares": level_shares}
            for price, level_shares in fill_levels
        ],
        "fee_rate": state.fee_rate,
        "fee": fee,
        "total_cost": total_cost,
        "all_in_cost": all_in_cost,
        "winner": winner,
        "won": won,
        "pnl_per_share": pnl / shares if pnl is not None and shares > 0 else None,
        "pnl": pnl,
        "filled": filled,
        "full_fill": full_fill,
        "sent": pending.sent,
        "reason": reason,
        "signal": asdict(signal),
    }


def replay_normalized(
    source_events: Iterable[dict[str, Any]],
    clob_events: Iterable[dict[str, Any]],
    mappings: Iterable[dict[str, Any]],
    outcomes: dict[str, str],
    *,
    detector_config: wedge.DetectorConfig | None = None,
    include_controls: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Replay normalized strict streams with 400/500 ms direct-book execution."""
    config = detector_config or wedge.DetectorConfig()
    mapping_rows = list(mappings)
    _, token_index = _mapping_dict(mapping_rows)
    fees = {
        str(row["market_id"]): float(row.get("fee_rate", config.fee_rate))
        for row in mapping_rows
    }
    states = _unique_markets(token_index, fees, config.fee_rate)
    detectors = {BASE_VARIANT: wedge.BasisWedgeDetector(config)}
    if include_controls:
        detectors.update({
            "no_wedge": wedge.BasisWedgeDetector(replace(
                config,
                min_basis_move_bp=0.0,
                max_basis_move_bp=NO_WEDGE_MAX_BP,
                require_confirmation=False,
            )),
            "confirmation": wedge.BasisWedgeDetector(replace(
                config,
                require_confirmation=True,
            )),
        })
    counters: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    pending: list[tuple[float, int, _Pending]] = []
    delayed: list[tuple[float, int, _DelayedDecision]] = []
    pending_sequence = 0
    delayed_sequence = 0
    signal_sequence = 0
    epoch = 0
    clob_connected = False
    source_active = {"spot": False, "futures": False, "deribit": False}
    source_fresh = {source: False for source in source_active}

    def fail_closed(recv_ms: float, reason: str) -> None:
        nonlocal epoch
        epoch += 1
        source_fresh.update({source: False for source in source_fresh})
        _reset_books(states)
        for detector in detectors.values():
            detector.feed({"kind": "disconnect", "recv_ms": recv_ms, "symbol": config.symbol})
        counters["fail_closed_resets"] += 1
        counters[f"reset_{reason}"] += 1

    def schedule(
        signal: wedge.WedgeSignal,
        variant: str,
        *,
        decision_recv_ms: float | None = None,
        scheduled_decision_recv_ms: float | None = None,
        parent_signal_id: str | None = None,
        sent: bool = True,
        terminal_reason: str | None = None,
    ) -> str:
        nonlocal pending_sequence, signal_sequence
        signal_sequence += 1
        signal_id = (
            f"{variant}:{signal.market_id}:{signal.recv_ms:.6f}:{signal_sequence}"
        )
        decision_ms = signal.recv_ms if decision_recv_ms is None else decision_recv_ms
        scheduled_decision_ms = (
            decision_ms
            if scheduled_decision_recv_ms is None
            else scheduled_decision_recv_ms
        )
        state = states[signal.market_id]
        decision_book_ms = state.recv_ms if math.isfinite(state.recv_ms) else None
        counters["signals"] += 1
        counters[f"signals_{variant}"] += 1
        for latency in EVALUATION_MS:
            item = _Pending(
                decision_ms + latency,
                pending_sequence,
                latency,
                epoch,
                signal_id,
                signal,
                parent_signal_id,
                variant,
                decision_ms,
                scheduled_decision_ms,
                decision_book_ms,
                sent,
                terminal_reason,
            )
            heapq.heappush(pending, (item.due_ms, item.sequence, item))
            pending_sequence += 1
            counters["evaluations_scheduled"] += 1
        return signal_id

    def reversal_signal(signal: wedge.WedgeSignal) -> tuple[wedge.WedgeSignal, str | None]:
        state = states[signal.market_id]
        token = state.book.up if signal.basis_direction == wedge.UP else state.book.down
        asks = tuple(sorted(token.asks.items())) if token.ready else ()
        fair = 1.0 - signal.fair
        priced = wedge.price_direct_asks(asks, fair, config, require_edge=False)
        if priced is None:
            counters["direction_reversal_unavailable"] += 1
            top = asks[0][0] if asks else 0.0
            visible = sum(size for _price, size in asks)
            return (
                replace(
                    signal,
                    buy_side=signal.basis_direction,
                    fair=fair,
                    ask=top,
                    fixed_limit=top,
                    fee=0.0,
                    net_edge=fair - top,
                    direct_depth=visible,
                ),
                "direction_reversal_decision_unavailable",
            )
        ask, fixed_limit, fee, net_edge, direct_depth = priced
        return (
            replace(
                signal,
                buy_side=signal.basis_direction,
                fair=fair,
                ask=ask,
                fixed_limit=fixed_limit,
                fee=fee,
                net_edge=net_edge,
                direct_depth=direct_depth,
            ),
            None,
        )

    def handle_signal(variant: str, signal: wedge.WedgeSignal) -> None:
        nonlocal delayed_sequence
        base_signal_id = schedule(signal, variant)
        if not include_controls or variant != BASE_VARIANT:
            return
        reversed_signal, reversal_reason = reversal_signal(signal)
        schedule(
            reversed_signal,
            "direction_reversal",
            parent_signal_id=base_signal_id,
            sent=reversal_reason is None,
            terminal_reason=reversal_reason,
        )
        item = _DelayedDecision(
            signal.recv_ms + TIME_SHIFT_MS,
            delayed_sequence,
            epoch,
            signal,
            base_signal_id,
        )
        heapq.heappush(delayed, (item.due_ms, item.sequence, item))
        delayed_sequence += 1
        counters["time_shift_decisions_scheduled"] += 1

    def record(item: _Pending, terminal_reason: str | None = None) -> None:
        state = states[item.signal.market_id]
        row = _result_row(
            item,
            state,
            outcomes,
            epoch,
            terminal_reason=terminal_reason,
        )
        rows.append(row)
        counters["evaluations"] += 1
        counters[f"reason_{row['reason']}"] += 1
        counters[f"reason_{item.variant}_{row['reason']}"] += 1
        if row["filled"]:
            counters["fills"] += 1
            counters[f"fills_{int(item.evaluation_ms)}ms"] += 1
            counters["full_fills" if row["full_fill"] else "partial_fills"] += 1
            if row["winner"] is None:
                counters["fills_without_outcome"] += 1

    def decide_time_shift(item: _DelayedDecision) -> None:
        state = states[item.signal.market_id]
        reason = None
        if item.epoch != epoch:
            reason = "disconnect"
        elif math.isfinite(state.recv_ms) and state.recv_ms > item.due_ms + _EPS:
            reason = "future_book_guard"
        else:
            token = state.book.up if item.signal.buy_side == wedge.UP else state.book.down
            decision_reason, shares, _vwap, _fee, _notional, _levels = _fill_direct(
                token,
                item.signal.fixed_limit,
                TARGET_SHARES,
                state.fee_rate,
            )
            if decision_reason != "filled" or shares + _EPS < TARGET_SHARES:
                suffix = {
                    "book_unavailable": "book_unavailable",
                    "no_direct_ask": "no_direct_ask",
                    "ask_above_frozen_limit": "ask_above_limit",
                    "partial_fill": "insufficient_direct_depth",
                    "insufficient_direct_depth": "insufficient_direct_depth",
                }.get(decision_reason, decision_reason)
                reason = f"shifted_decision_{suffix}"
        schedule(
            item.signal,
            "time_shift",
            decision_recv_ms=item.due_ms,
            scheduled_decision_recv_ms=item.due_ms,
            parent_signal_id=item.parent_signal_id,
            sent=reason is None,
            terminal_reason=reason,
        )

    def drain(cutoff_ms: float, *, inclusive: bool) -> None:
        while pending or delayed:
            pending_due = pending[0][0] if pending else math.inf
            delayed_due = delayed[0][0] if delayed else math.inf
            due_ms = min(pending_due, delayed_due)
            due = due_ms <= cutoff_ms + _EPS if inclusive else due_ms < cutoff_ms - _EPS
            if not due:
                return
            if pending_due <= delayed_due:
                _, _, item = heapq.heappop(pending)
                record(item)
            else:
                _, _, item = heapq.heappop(delayed)
                decide_time_shift(item)

    def process_source(event: dict[str, Any]) -> None:
        nonlocal clob_connected
        kind = str(event["kind"])
        recv_ms = float(event["recv_ms"])
        counters[kind] += 1
        if kind.endswith("_connection"):
            source = kind.removesuffix("_connection")
            if source in source_active:
                source_active[source] = True
            fail_closed(recv_ms, kind)
            return
        if kind.endswith("_disconnect"):
            source = kind.removesuffix("_disconnect")
            if source in source_active:
                source_active[source] = False
            fail_closed(recv_ms, kind)
            return
        source_by_kind = {
            "spot_bbo": "spot",
            "futures_bbo": "futures",
            "deribit_quote": "deribit",
        }
        source = source_by_kind.get(kind)
        if source is None:
            counters["source_events_ignored"] += 1
            return
        if not source_active[source]:
            counters[f"ignored_{source}_while_disconnected"] += 1
            return
        if "symbol" not in event:
            raise ValueError("symbol is required on every normalized source frame")
        if str(event["symbol"]).upper() != config.symbol.upper():
            raise ValueError("normalized source symbol drift")
        source_fresh[source] = True
        normalized = {
            "kind": kind,
            "recv_ms": recv_ms,
            "bid": float(event["bid"]),
            "ask": float(event["ask"]),
            "symbol": event["symbol"],
        }
        for variant, detector in detectors.items():
            signal = detector.feed(normalized)
            if signal is not None:
                handle_signal(variant, signal)

    def process_clob(batch: dict[str, Any]) -> None:
        nonlocal clob_connected
        recv_ms = float(batch["recv_ms"])
        touched: dict[str, _MarketState] = {}
        for event in batch["events"]:
            kind = str(event["kind"])
            counters[kind] += 1
            if kind == "clob_connection":
                fail_closed(recv_ms, kind)
                clob_connected = True
                continue
            if kind in {"clob_error", "clob_disconnect"}:
                clob_connected = False
                fail_closed(recv_ms, kind)
                continue
            if not clob_connected:
                counters["ignored_clob_while_disconnected"] += 1
                continue
            hit = token_index.get(str(event.get("asset_id")))
            if hit is None:
                counters["unknown_token"] += 1
                continue
            market, direction = hit
            if str(event.get("market_id")) != market.market_id:
                counters["token_market_mismatch"] += 1
                continue
            token = market.up if direction == "Up" else market.down
            source_ms = float(event["source_ts_ms"])
            applied = False
            if kind == "clob_snapshot" and source_ms >= token.source_ms:
                token.replace(event["bids"], event["asks"], source_ms)
                applied = True
            elif kind == "clob_price_change":
                applied = token.change(
                    str(event["side"]),
                    float(event["price"]),
                    float(event["size"]),
                    source_ms,
                    event.get("best_bid"),
                    event.get("best_ask"),
                )
            if not applied:
                counters["clob_updates_not_applied"] += 1
                continue
            state = states[market.market_id]
            state.recv_ms = recv_ms
            touched[market.market_id] = state

        if not clob_connected or not all(source_fresh.values()):
            return
        for state in touched.values():
            if not state.book.ready:
                counters["direct_book_not_ready"] += 1
                continue
            normalized = _direct_pm_event(state, recv_ms, config.symbol)
            for variant, detector in detectors.items():
                signal = detector.feed(normalized)
                if signal is not None:
                    handle_signal(variant, signal)

    last_recv_ms = -math.inf
    for receive_ms, group in groupby(
        _merged_events(source_events, clob_events),
        key=lambda event: float(event["recv_ms"]),
    ):
        drain(receive_ms, inclusive=False)
        for event in group:
            if event["kind"] == "clob_batch":
                process_clob(event)
            else:
                process_source(event)
        drain(receive_ms, inclusive=True)
        last_recv_ms = receive_ms

    while delayed:
        _, _, item = heapq.heappop(delayed)
        counters["time_shift_censored_before_decision"] += 1
        schedule(
            item.signal,
            "time_shift",
            decision_recv_ms=item.signal.recv_ms,
            scheduled_decision_recv_ms=item.due_ms,
            parent_signal_id=item.parent_signal_id,
            sent=False,
            terminal_reason="censored_before_shifted_decision",
        )
    while pending:
        _, _, item = heapq.heappop(pending)
        terminal_reason = item.terminal_reason
        if terminal_reason is None:
            terminal_reason = "disconnect" if item.epoch != epoch else "censored_recording_end"
        record(item, terminal_reason)
    counters["last_recv_ms_finite"] = int(math.isfinite(last_recv_ms))
    return rows, dict(counters)


def summarize(rows: list[dict[str, Any]], counters: dict[str, int]) -> dict[str, Any]:
    variants: dict[str, Any] = {}
    for variant in VARIANTS:
        latencies: dict[str, Any] = {}
        variant_rows = [row for row in rows if row.get("variant", BASE_VARIANT) == variant]
        for latency in EVALUATION_MS:
            sample = [row for row in variant_rows if row["evaluation_ms"] == latency]
            fills = [row for row in sample if row["filled"]]
            full_fills = [row for row in fills if row["full_fill"]]
            scored = [row for row in fills if row["pnl"] is not None]
            filled_shares = sum(float(row["filled_shares"]) for row in scored)
            pnl = sum(float(row["pnl"]) for row in scored)
            latencies[str(int(latency))] = {
                "signals": len(sample),
                "sent": sum(bool(row.get("sent", True)) for row in sample),
                "fills": len(fills),
                "full_fills": len(full_fills),
                "partial_fills": len(fills) - len(full_fills),
                "fill_rate": len(fills) / len(sample) if sample else 0.0,
                "full_fill_rate": len(full_fills) / len(sample) if sample else 0.0,
                "filled_shares": sum(float(row["filled_shares"]) for row in fills),
                "pnl": pnl,
                "net_ev_per_share": pnl / filled_shares if filled_shares else None,
                "reasons": dict(Counter(str(row["reason"]) for row in sample)),
            }
        variants[variant] = {"latencies": latencies}
    return {
        "counters": counters,
        "variants": variants,
        # Compatibility view used by the pre-control audit and old report code.
        "latencies": variants[BASE_VARIANT]["latencies"],
    }


def replay_archive(
    archive_dir: str | Path,
    *,
    rows_out: str | Path | None = None,
    detector_config: wedge.DetectorConfig | None = None,
) -> dict[str, Any]:
    """Validate and replay one standard strict artifact without network access."""
    root = Path(archive_dir)
    standard = root / "strict" if (root / "strict" / "manifest.json").exists() else root
    validation = archive.validate_standard_artifact(standard)
    mappings = list(archive.iter_market_mappings(standard / "market_registry.csv.gz"))
    outcomes = {
        str(row["market_id"]): str(row["winner"])
        for row in archive.iter_outcomes(standard / "market_outcomes.csv.gz")
    }
    source_events = archive.iter_normalized_events(
        standard / "source_events.jsonl.gz",
        family="source",
    )
    config = detector_config or wedge.DetectorConfig()
    clob_events = archive.iter_normalized_events(
        standard / "clob_events.jsonl.gz",
        family="clob",
    )
    rows, counters = replay_normalized(
        source_events,
        clob_events,
        mappings,
        outcomes,
        detector_config=config,
    )
    if rows_out is not None:
        destination = Path(rows_out)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("w", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
    result = summarize(rows, counters)
    result["records"] = rows
    result["dataset"] = {
        "paper_only": True,
        "sample_scope": "standard_forward_artifact",
        "archive": root.name,
        "manifest": validation["manifest"],
        "source_symbol_provenance": "validated_on_each_source_price_frame",
        "mapped_markets": len(mappings),
        "outcomes": len(outcomes),
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", required=True)
    parser.add_argument("--rows-out")
    args = parser.parse_args()
    print(json.dumps(
        replay_archive(args.archive, rows_out=args.rows_out),
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ))


if __name__ == "__main__":
    main()
