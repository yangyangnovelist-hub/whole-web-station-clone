"""Bounded-memory paper replay adapter for frozen US-017."""
from __future__ import annotations

import math
import heapq
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import asdict, replace
from itertools import groupby
from pathlib import Path
from typing import Any

import basis_wedge_run as execution
import common_mode_residual as cmr
import eu_strict
from h_replay_run import _mapping_dict, _merged_events


_EPS = execution._EPS
BASE_VARIANT = "base"
BENCHMARK_CONTROL = "benchmark_confirmation"
NO_FOLLOW_CONTROL = "no_pm_follow"
DIRECTION_CONTROL = "direction_reversal"
TIME_SHIFT_CONTROL = "time_shift"
NO_FOLLOW_MAX_MOVE = 0.005
MATCHED_BASE_ARMS = frozenset({DIRECTION_CONTROL, TIME_SHIFT_CONTROL})
NEGATIVE_CONTROL_COHORTS = frozenset({BENCHMARK_CONTROL, NO_FOLLOW_CONTROL})


def iter_deribit_index_events(
    paths: Iterable[str | Path],
) -> Iterator[dict[str, Any]]:
    """Yield lifecycle-scoped raw BTCUSD index frames in receipt order."""
    active_epoch: int | None = None
    sequence = 0
    stats = {"skipped_irrelevant_frames": 0}
    normalized_paths = [Path(path) for path in paths]
    for receive_ns, _, payload in eu_strict._iter_route(
        normalized_paths,
        "deribit",
        "deribit_index",
        stats,
    ):
        marker = payload.get("_recorder")
        if isinstance(marker, Mapping):
            if marker.get("schema") != "recorder-lifecycle-v1":
                continue
            kind = str(marker.get("kind"))
            epoch = int(marker["epoch"])
            if kind == "connection":
                active_epoch = epoch
            elif kind == "disconnect" and active_epoch == epoch:
                active_epoch = None
            continue
        if active_epoch is None:
            continue
        params = payload.get("params")
        if not isinstance(params, Mapping):
            continue
        if params.get("channel") != "deribit_price_index.btc_usd":
            continue
        data = params.get("data")
        if not isinstance(data, Mapping):
            continue
        price = float(data["price"])
        source_ms = eu_strict._clock_ms(data.get("timestamp"))
        if source_ms is None or not math.isfinite(source_ms) or not 0.0 < price:
            raise ValueError("invalid deribit_price_index.btc_usd frame")
        yield {
            "kind": "deribit_index",
            "recv_ms": receive_ns / 1_000_000.0,
            "seq": sequence,
            "source_ts_ms": source_ms,
            "price": price,
            "symbol": "BTC",
            "connection_epoch": active_epoch,
        }
        sequence += 1


def iter_binance_spot_bbo_events(
    paths: Iterable[str | Path],
) -> Iterator[dict[str, Any]]:
    """Yield lifecycle-scoped raw BTCUSDT spot BBO frames in receipt order."""
    active_epoch: int | None = None
    sequence = 0
    stats = {"skipped_irrelevant_frames": 0}
    normalized_paths = [Path(path) for path in paths]
    for receive_ns, _, payload in eu_strict._iter_route(
        normalized_paths,
        "bn_spot",
        "spot_bbo_sidecar",
        stats,
    ):
        marker = payload.get("_recorder")
        if isinstance(marker, Mapping):
            if marker.get("schema") != "recorder-lifecycle-v1":
                continue
            kind = str(marker.get("kind"))
            epoch = int(marker["epoch"])
            if kind == "connection":
                active_epoch = epoch
                yield {
                    "kind": "spot_bbo_connection",
                    "recv_ms": receive_ns / 1_000_000.0,
                    "seq": sequence,
                    "source_ts_ms": None,
                    "connection_epoch": epoch,
                }
                sequence += 1
            elif kind == "disconnect" and active_epoch == epoch:
                yield {
                    "kind": "spot_bbo_disconnect",
                    "recv_ms": receive_ns / 1_000_000.0,
                    "seq": sequence,
                    "source_ts_ms": None,
                    "connection_epoch": epoch,
                }
                sequence += 1
                active_epoch = None
            continue
        if active_epoch is None:
            continue
        data = payload.get("data", payload)
        if not isinstance(data, Mapping):
            continue
        stream = str(payload.get("stream", ""))
        event_name = str(data.get("e", ""))
        if event_name != "bookTicker" and not stream.endswith("@bookTicker"):
            continue
        raw_symbol = str(data.get("s") or stream.partition("@")[0]).upper()
        if raw_symbol != "BTCUSDT":
            raise ValueError(f"unexpected spot BBO symbol {raw_symbol!r}")
        bid = float(data["b"])
        ask = float(data["a"])
        if not (math.isfinite(bid) and math.isfinite(ask) and 0.0 < bid < ask):
            raise ValueError("invalid BTCUSDT spot bookTicker frame")
        yield {
            "kind": "spot_bbo",
            "recv_ms": receive_ns / 1_000_000.0,
            "seq": sequence,
            "source_ts_ms": eu_strict._clock_ms(data.get("T") or data.get("E")),
            "bid": bid,
            "ask": ask,
            "symbol": "BTC",
            "connection_epoch": active_epoch,
        }
        sequence += 1


def _source_and_index_events(
    source_events: Iterable[dict[str, Any]],
    index_events: Iterable[dict[str, Any]],
) -> Iterator[dict[str, Any]]:
    def ranked(
        events: Iterable[dict[str, Any]],
        rank: int,
    ) -> Iterator[tuple[tuple[float, int, int], dict[str, Any]]]:
        for sequence, event in enumerate(events):
            yield (
                (float(event["recv_ms"]), rank, int(event.get("seq", sequence))),
                event,
            )
    streams = [
        ranked(source_events, 0),
        ranked(index_events, 1),
    ]
    merged = heapq.merge(*streams, key=lambda item: item[0])
    for _, same_receipt in groupby(merged, key=lambda item: item[0][0]):
        group = list(same_receipt)
        origins: set[str] = set()
        for key, event in group:
            if key[1] == 1:
                origins.add("deribit_index_sidecar")
                continue
            kind = str(event["kind"])
            if kind.startswith("spot_bbo"):
                origins.add("spot_bbo_sidecar")
            elif kind.startswith("futures"):
                origins.add("futures")
            elif kind.startswith("deribit"):
                origins.add("deribit_quote")
        ambiguous = len(origins) > 1
        for sequence, (_, event) in enumerate(group):
            yield {
                **event,
                "seq": sequence,
                "ambiguous_cross_stream_receipt_tie": ambiguous,
            }


def _result_row(
    pending: execution._Pending,
    state: execution._MarketState,
    outcomes: Mapping[str, str],
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
        token = state.book.up if signal.buy_side == cmr.UP else state.book.down
        reason, shares, fill_vwap, fee, notional, fill_levels = execution._fill_direct(
            token,
            signal.fixed_limit,
            execution.TARGET_SHARES,
            state.fee_rate,
        )

    filled = reason in {"filled", "partial_fill"}
    full_fill = reason == "filled"
    winner = outcomes.get(signal.market_id) if filled else None
    won = winner.lower() == signal.buy_side if winner is not None else None
    total_cost = notional + fee if filled and notional is not None and fee is not None else None
    all_in_cost = total_cost / shares if total_cost is not None and shares > 0 else None
    pnl = (
        (shares if won else 0.0) - total_cost
        if total_cost is not None and won is not None
        else None
    )
    return {
        "signal_id": pending.signal_id,
        "parent_signal_id": pending.parent_signal_id,
        "paper_only": True,
        "ordering_clock": "local_receipt_ms",
        "match_book_clock": "local_receipt_ms",
        "market_id": signal.market_id,
        "variant": pending.variant,
        "comparison_design": (
            "matched_base_arm"
            if pending.variant in MATCHED_BASE_ARMS
            else "disjoint_negative_control_cohort"
            if pending.variant in NEGATIVE_CONTROL_COHORTS
            else "base"
        ),
        "signal_recv_ms": signal.recv_ms,
        "decision_recv_ms": pending.decision_recv_ms,
        "scheduled_decision_recv_ms": pending.scheduled_decision_recv_ms,
        "decision_book_recv_ms": pending.decision_book_recv_ms,
        "evaluation_ms": pending.evaluation_ms,
        "evaluation_recv_ms": pending.due_ms,
        "book_recv_ms": book_recv_ms,
        "common_direction": signal.common_direction,
        "buy_side": signal.buy_side,
        "fair": signal.fair,
        "decision_vwap": signal.ask,
        "fixed_limit": signal.fixed_limit,
        "signal_fee_per_share": signal.fee,
        "signal_net_edge": signal.net_edge,
        "target_shares": execution.TARGET_SHARES,
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
    index_events: Iterable[dict[str, Any]],
    clob_events: Iterable[dict[str, Any]],
    mappings: Iterable[dict[str, Any]],
    outcomes: Mapping[str, str],
    *,
    detector_config: cmr.DetectorConfig | None = None,
    include_controls: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Replay frozen US-017 inputs with direct-book paper execution."""
    config = detector_config or cmr.DetectorConfig()
    mapping_rows = list(mappings)
    _, token_index = _mapping_dict(mapping_rows)
    fees = {
        str(row["market_id"]): float(row.get("fee_rate", config.fee_rate))
        for row in mapping_rows
    }
    states = execution._unique_markets(token_index, fees, config.fee_rate)
    detectors = {BASE_VARIANT: cmr.CommonModeResidualDetector(config)}
    if include_controls:
        detectors.update({
            BENCHMARK_CONTROL: cmr.CommonModeResidualDetector(replace(
                config,
                require_benchmark_confirmation=True,
            )),
            NO_FOLLOW_CONTROL: cmr.CommonModeResidualDetector(replace(
                config,
                min_pm_move=0.0,
                max_pm_move=NO_FOLLOW_MAX_MOVE,
            )),
        })
    counters: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    pending: list[tuple[float, int, execution._Pending]] = []
    delayed: list[tuple[float, int, execution._DelayedDecision]] = []
    pending_sequence = 0
    delayed_sequence = 0
    signal_sequence = 0
    epoch = 0
    clob_connected = False
    clob_sequence = 0
    source_active = {"spot_bbo": False, "futures": False, "deribit": False}

    def fail_closed(recv_ms: float, reason: str) -> None:
        nonlocal epoch
        epoch += 1
        execution._reset_books(states)
        disconnect = {"kind": "disconnect", "recv_ms": recv_ms, "symbol": config.symbol}
        for detector in detectors.values():
            detector.feed(disconnect)
        counters["fail_closed_resets"] += 1
        counters[f"reset_{reason}"] += 1

    def schedule(
        signal: cmr.CommonModeResidualSignal,
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
        signal_id = f"{variant}:{signal.market_id}:{signal.recv_ms:.6f}:{signal_sequence}"
        decision_ms = signal.recv_ms if decision_recv_ms is None else decision_recv_ms
        scheduled_ms = (
            decision_ms
            if scheduled_decision_recv_ms is None
            else scheduled_decision_recv_ms
        )
        state = states[signal.market_id]
        decision_book_ms = state.recv_ms if math.isfinite(state.recv_ms) else None
        counters["signals"] += 1
        counters[f"signals_{variant}"] += 1
        for latency in execution.EVALUATION_MS:
            item = execution._Pending(
                due_ms=decision_ms + latency,
                sequence=pending_sequence,
                evaluation_ms=latency,
                epoch=epoch,
                signal_id=signal_id,
                signal=signal,
                parent_signal_id=parent_signal_id,
                variant=variant,
                decision_recv_ms=decision_ms,
                scheduled_decision_recv_ms=scheduled_ms,
                decision_book_recv_ms=decision_book_ms,
                sent=sent,
                terminal_reason=terminal_reason,
            )
            heapq.heappush(pending, (item.due_ms, item.sequence, item))
            pending_sequence += 1
            counters["evaluations_scheduled"] += 1
        return signal_id

    def reversal_signal(
        signal: cmr.CommonModeResidualSignal,
    ) -> tuple[cmr.CommonModeResidualSignal, str | None]:
        state = states[signal.market_id]
        token = state.book.up if signal.common_direction == cmr.UP else state.book.down
        asks = tuple(sorted(token.asks.items())) if token.ready else ()
        fair = 1.0 - signal.fair
        priced = cmr.bw.price_direct_asks(asks, fair, config, require_edge=False)
        if priced is None:
            counters["direction_reversal_unavailable"] += 1
            top = asks[0][0] if asks else 0.0
            visible = sum(size for _, size in asks)
            return replace(
                signal,
                buy_side=signal.common_direction,
                fair=fair,
                ask=top,
                fixed_limit=top,
                fee=0.0,
                net_edge=fair - top,
                direct_depth=visible,
            ), "direction_reversal_decision_unavailable"
        ask, fixed_limit, fee, net_edge, direct_depth = priced
        return replace(
            signal,
            buy_side=signal.common_direction,
            fair=fair,
            ask=ask,
            fixed_limit=fixed_limit,
            fee=fee,
            net_edge=net_edge,
            direct_depth=direct_depth,
        ), None

    def handle_signal(variant: str, signal: cmr.CommonModeResidualSignal) -> None:
        nonlocal delayed_sequence
        signal_id = schedule(signal, variant)
        if not include_controls or variant != BASE_VARIANT:
            return
        reversed_signal, reversal_reason = reversal_signal(signal)
        schedule(
            reversed_signal,
            DIRECTION_CONTROL,
            parent_signal_id=signal_id,
            sent=reversal_reason is None,
            terminal_reason=reversal_reason,
        )
        delayed_item = execution._DelayedDecision(
            due_ms=signal.recv_ms + execution.TIME_SHIFT_MS,
            sequence=delayed_sequence,
            epoch=epoch,
            signal=signal,
            parent_signal_id=signal_id,
        )
        heapq.heappush(
            delayed,
            (delayed_item.due_ms, delayed_item.sequence, delayed_item),
        )
        delayed_sequence += 1
        counters["time_shift_decisions_scheduled"] += 1

    def record(item: execution._Pending, terminal_reason: str | None = None) -> None:
        row = _result_row(
            item,
            states[item.signal.market_id],
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

    def decide_time_shift(item: execution._DelayedDecision) -> None:
        state = states[item.signal.market_id]
        reason = None
        if item.epoch != epoch:
            reason = "disconnect"
        elif math.isfinite(state.recv_ms) and state.recv_ms > item.due_ms + _EPS:
            reason = "future_book_guard"
        else:
            token = state.book.up if item.signal.buy_side == cmr.UP else state.book.down
            decision_reason, shares, _vwap, _fee, _notional, _levels = (
                execution._fill_direct(
                    token,
                    item.signal.fixed_limit,
                    execution.TARGET_SHARES,
                    state.fee_rate,
                )
            )
            if decision_reason != "filled" or shares + _EPS < execution.TARGET_SHARES:
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
            TIME_SHIFT_CONTROL,
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
        kind = str(event["kind"])
        recv_ms = float(event["recv_ms"])
        counters[kind] += 1
        if kind.endswith("_connection"):
            source = kind.removesuffix("_connection")
            if source not in source_active:
                counters["source_lifecycle_ignored"] += 1
                return
            source_active[source] = True
            fail_closed(recv_ms, kind)
            return
        if kind.endswith("_disconnect"):
            source = kind.removesuffix("_disconnect")
            if source not in source_active:
                counters["source_lifecycle_ignored"] += 1
                return
            source_active[source] = False
            fail_closed(recv_ms, kind)
            return
        source_by_kind = {
            "spot_bbo": "spot_bbo",
            "futures_bbo": "futures",
            "deribit_quote": "deribit",
            "deribit_index": "deribit",
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
        if kind == "deribit_index":
            normalized = {
                "kind": kind,
                "recv_ms": recv_ms,
                "price": float(event["price"]),
                "symbol": event["symbol"],
            }
        else:
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
        nonlocal clob_connected, clob_sequence
        recv_ms = float(batch["recv_ms"])
        touched: dict[str, execution._MarketState] = {}
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
            clob_sequence += 1
            receipt_order = float(clob_sequence)
            applied = False
            if kind == "clob_snapshot":
                token.replace(event["bids"], event["asks"], receipt_order)
                applied = True
            elif kind == "clob_price_change":
                applied = token.change(
                    str(event["side"]),
                    float(event["price"]),
                    float(event["size"]),
                    receipt_order,
                    event.get("best_bid"),
                    event.get("best_ask"),
                )
            if not applied:
                counters["clob_updates_not_applied"] += 1
                continue
            state = states[market.market_id]
            state.recv_ms = recv_ms
            touched[market.market_id] = state

        if not clob_connected:
            return
        for state in touched.values():
            if not state.book.ready:
                counters["direct_book_not_ready"] += 1
                continue
            normalized = execution._direct_pm_event(state, recv_ms, config.symbol)
            for variant, detector in detectors.items():
                signal = detector.feed(normalized)
                if signal is not None:
                    handle_signal(variant, signal)

    last_recv_ms = -math.inf
    merged_source = _source_and_index_events(source_events, index_events)
    for receive_ms, event_group in groupby(
        _merged_events(merged_source, clob_events),
        key=lambda event: float(event["recv_ms"]),
    ):
        events = list(event_group)
        if receive_ms < last_recv_ms - _EPS:
            fail_closed(receive_ms, "receipt_regression")
            counters["receipt_regressed_events"] += len(events)
            continue
        drain(receive_ms, inclusive=True)
        source_and_clob_tie = (
            any(event["kind"] == "clob_batch" for event in events)
            and any(event["kind"] != "clob_batch" for event in events)
        )
        if source_and_clob_tie or any(
            bool(event.get("ambiguous_cross_stream_receipt_tie"))
            for event in events
        ):
            fail_closed(receive_ms, "ambiguous_receipt_tie")
            counters["ambiguous_receipt_tie_events"] += len(events)
            last_recv_ms = max(last_recv_ms, receive_ms)
            continue
        for event in events:
            if event["kind"] == "clob_batch":
                process_clob(event)
            else:
                process_source(event)
        last_recv_ms = max(last_recv_ms, receive_ms)

    while delayed:
        _, _, item = heapq.heappop(delayed)
        counters["time_shift_censored_before_decision"] += 1
        schedule(
            item.signal,
            TIME_SHIFT_CONTROL,
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
