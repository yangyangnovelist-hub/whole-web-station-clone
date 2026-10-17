"""Receipt-causal paper replay for Family 35 adverse-excursion reclaim."""

from __future__ import annotations

import argparse
import heapq
import json
import math
from collections import Counter, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from itertools import groupby
from pathlib import Path
from typing import Any, cast

import complement_queue_lag as queue_lag

_EPS = 1e-9


@dataclass(frozen=True)
class AdverseReclaimConfig:
    lookback_s: int = 15
    sample_interval_ms: float = 1_000.0
    minimum_remaining_s: float = 90.0
    maximum_remaining_s: float = 240.0
    sample_fresh_ms: float = 250.0
    execution_fresh_ms: float = 2_000.0
    target_shares: float = 5.0
    minimum_order_usd: float = 1.0
    evaluation_ms: tuple[float, ...] = (400.0, 500.0)
    delayed_control_ms: float = 5_000.0
    correction_denominator: int = 873

    def __post_init__(self) -> None:
        frozen = (
            self.lookback_s == 15
            and self.sample_interval_ms == 1_000.0
            and self.minimum_remaining_s == 90.0
            and self.maximum_remaining_s == 240.0
            and self.sample_fresh_ms == 250.0
            and self.execution_fresh_ms == 2_000.0
            and self.target_shares == 5.0
            and self.minimum_order_usd == 1.0
            and self.evaluation_ms == (400.0, 500.0)
            and self.delayed_control_ms == 5_000.0
            and self.correction_denominator >= 873
        )
        if not frozen:
            raise ValueError("Family 35 frozen protocol parameters changed")


@dataclass(frozen=True)
class _Observation:
    sample_ms: float
    up_midpoint: float | None
    down_midpoint: float | None
    epoch: int | None


@dataclass
class _FamilyMarket:
    history: deque[_Observation] = field(
        default_factory=lambda: deque(maxlen=16)
    )
    consumed: bool = False
    tick_sizes: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class _Signal:
    signal_id: str
    parent_signal_id: str | None
    variant: str
    market_id: str
    epoch: int
    signal_ms: float
    decision_ms: float
    remaining_s: float
    buy_asset_id: str
    buy_side: str
    path_start: float
    path_end: float
    adverse_excursion: float
    final_move: float
    decision_spread: float
    decision_depth: float
    decision_vwap: float
    fixed_limit: float
    tick_size: float


@dataclass(frozen=True)
class _Pending:
    due_ms: float
    sequence: int
    evaluation_ms: float
    signal: _Signal


def _up_cross(path: Sequence[float], *, shallow: bool) -> bool:
    start = path[0]
    crossing = (
        0.40 <= start <= 0.75
        and path[-2] < start <= path[-1] <= round(start + 0.01, 12)
    )
    if not crossing:
        return False
    adverse = round(start - min(path[1:-1]), 12)
    return adverse < 0.02 if shallow else adverse >= 0.08


def _canonical_path(path: Sequence[float]) -> tuple[float, ...]:
    if len(path) != 16:
        raise ValueError("adverse reclaim requires exactly 16 observations")
    if any(
        isinstance(price, bool)
        or not isinstance(price, (int, float))
        or not math.isfinite(price)
        or not 0 < price < 1
        for price in path
    ):
        raise ValueError("path midpoints must be finite probabilities")
    return tuple(round(float(price), 12) for price in path)


def _path_variant(path: Sequence[float]) -> str | None:
    for variant, shallow in (("base", False), ("shallow", True)):
        if _up_cross(path, shallow=shallow):
            return variant
    return None


def _direct_variants(
    up_path: Sequence[float],
    down_path: Sequence[float],
) -> tuple[str | None, str | None]:
    canonical_up = _canonical_path(up_path)
    canonical_down = _canonical_path(down_path)
    return _path_variant(canonical_up), _path_variant(canonical_down)


def classify_path(
    up_path: Sequence[float],
    down_path: Sequence[float],
) -> tuple[str, str] | None:
    """Classify two complete direct-token paths without complement synthesis."""
    up_variant, down_variant = _direct_variants(up_path, down_path)
    if up_variant is not None and down_variant is None:
        return up_variant, "Up"
    if down_variant is not None and up_variant is None:
        return down_variant, "Down"
    return None


def replay(
    clob_events: Iterable[Mapping[str, Any]],
    mappings: Iterable[Mapping[str, Any]],
    outcomes: Mapping[str, str],
    *,
    segment_end_ms: float,
    config: AdverseReclaimConfig | None = None,
) -> tuple[list[dict[str, Any]], Counter[str]]:
    """Replay the frozen Family 35 detector on strict physical CLOB batches."""
    cfg = config or AdverseReclaimConfig()
    markets, tokens = queue_lag._market_maps(mappings)
    family = {market_id: _FamilyMarket() for market_id in markets}
    counters: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    pending: list[tuple[float, int, _Pending]] = []
    grid: list[tuple[float, int, str]] = []
    grid_end: dict[str, float] = {}
    connected = False
    epoch = 0
    pending_sequence = 0
    signal_sequence = 0
    expected_event_sequence = 0
    last_receive_ms = -math.inf
    seen_batches: set[str] = set()

    for order, (market_id, market) in enumerate(markets.items()):
        first_sample = (market.slot + 45) * 1_000.0
        last_decision = (market.slot + 210) * 1_000.0
        grid_end[market_id] = last_decision
        heapq.heappush(grid, (first_sample, order, market_id))

    def reset_epoch_state() -> None:
        for market_id, market in markets.items():
            market.reset()
            state = family[market_id]
            state.history.clear()
            state.tick_sizes.clear()

    def push_next_grid(sample_ms: float, order: int, market_id: str) -> None:
        next_ms = sample_ms + cfg.sample_interval_ms
        if next_ms <= grid_end[market_id] + _EPS:
            heapq.heappush(grid, (next_ms, order, market_id))

    def append_unsent(
        signal_fields: Mapping[str, Any],
        reason: str,
        *,
        decision_spread: float | None = None,
        decision_depth: float = 0.0,
        decision_vwap: float | None = None,
        tick_size: float | None = None,
    ) -> None:
        for latency in cfg.evaluation_ms:
            rows.append({
                **signal_fields,
                "paper_only": True,
                "family": 35,
                "decision_spread": decision_spread,
                "decision_depth": decision_depth,
                "decision_vwap": decision_vwap,
                "fixed_limit": None,
                "tick_size": tick_size,
                "evaluation_ms": latency,
                "target_shares": cfg.target_shares,
                "filled_shares": 0.0,
                "filled": False,
                "full_fill": False,
                "sent": False,
                "censored": False,
                "pnl": 0.0,
                "reason": reason,
            })
        counters[f"decision_{reason}"] += 1

    def schedule(signal: _Signal) -> None:
        nonlocal pending_sequence
        for latency in cfg.evaluation_ms:
            pending_sequence += 1
            item = _Pending(
                signal.decision_ms + latency,
                pending_sequence,
                latency,
                signal,
            )
            heapq.heappush(pending, (item.due_ms, item.sequence, item))

    def emit_signal(
        market_id: str,
        variant: str,
        side: str,
        path: Sequence[float],
        decision_ms: float,
    ) -> None:
        nonlocal signal_sequence
        market = markets[market_id]
        state = family[market_id]
        buy_asset_id = (
            market.up_token_id if side == "Up" else market.down_token_id
        )
        signal_sequence += 1
        signal_id = f"{market_id}:{variant}:{signal_sequence}"
        remaining_s = market.slot + 300.0 - decision_ms / 1_000.0
        path_start = float(path[0])
        path_end = float(path[-1])
        adverse_excursion = path_start - min(path[1:-1])
        final_move = path_end - path[-2]
        signal_fields = {
            "signal_id": signal_id,
            "parent_signal_id": None,
            "variant": variant,
            "market_id": market_id,
            "epoch": epoch,
            "signal_ms": decision_ms,
            "decision_ms": decision_ms,
            "remaining_s": remaining_s,
            "buy_asset_id": buy_asset_id,
            "buy_side": side,
            "path_start": path_start,
            "path_end": path_end,
            "adverse_excursion": adverse_excursion,
            "final_move": final_move,
        }
        book = market.books.get(buy_asset_id)
        if book is None or not book.valid_at(
            decision_ms, cfg.execution_fresh_ms
        ):
            append_unsent(signal_fields, "book_unavailable")
            return
        decision_spread = book.spread
        if decision_spread is None:
            append_unsent(signal_fields, "book_unavailable")
            return
        filled, notional, levels = queue_lag._take_levels(
            book, cfg.target_shares
        )
        decision_depth = sum(book.asks.values())
        if filled < cfg.target_shares - _EPS:
            append_unsent(
                signal_fields,
                "insufficient_decision_depth",
                decision_spread=decision_spread,
                decision_depth=decision_depth,
                decision_vwap=notional / filled if filled > _EPS else None,
                tick_size=state.tick_sizes.get(buy_asset_id),
            )
            return
        fixed_limit = levels[-1][0]
        tick_size = state.tick_sizes.get(buy_asset_id)
        if tick_size is None:
            append_unsent(
                signal_fields,
                "missing_tick_size",
                decision_spread=decision_spread,
                decision_depth=decision_depth,
                decision_vwap=notional / cfg.target_shares,
            )
            return
        units = fixed_limit / tick_size
        if not math.isclose(units, round(units), rel_tol=0.0, abs_tol=_EPS):
            append_unsent(
                signal_fields,
                "invalid_limit_tick",
                decision_spread=decision_spread,
                decision_depth=decision_depth,
                decision_vwap=notional / cfg.target_shares,
                tick_size=tick_size,
            )
            return
        if fixed_limit * cfg.target_shares < cfg.minimum_order_usd - _EPS:
            append_unsent(
                signal_fields,
                "below_venue_minimum",
                decision_spread=decision_spread,
                decision_depth=decision_depth,
                decision_vwap=notional / cfg.target_shares,
                tick_size=tick_size,
            )
            return
        signal = _Signal(
            signal_id=signal_id,
            parent_signal_id=None,
            variant=variant,
            market_id=market_id,
            epoch=epoch,
            signal_ms=decision_ms,
            decision_ms=decision_ms,
            remaining_s=remaining_s,
            buy_asset_id=buy_asset_id,
            buy_side=side,
            path_start=path_start,
            path_end=path_end,
            adverse_excursion=adverse_excursion,
            final_move=final_move,
            decision_spread=decision_spread,
            decision_depth=sum(
                size for price, size in book.asks.items()
                if price <= fixed_limit + _EPS
            ),
            decision_vwap=notional / cfg.target_shares,
            fixed_limit=fixed_limit,
            tick_size=tick_size,
        )
        counters[f"{variant}_signals"] += 1
        schedule(signal)
        if variant == "base":
            signal_sequence += 1
            delayed = replace(
                signal,
                signal_id=f"{market_id}:delay_5s:{signal_sequence}",
                parent_signal_id=signal.signal_id,
                variant="delay_5s",
                decision_ms=decision_ms + cfg.delayed_control_ms,
                remaining_s=(
                    market.slot
                    + 300.0
                    - (decision_ms + cfg.delayed_control_ms) / 1_000.0
                ),
            )
            counters["delay_5s_signals"] += 1
            schedule(delayed)

    def sample_grid(
        sample_ms: float,
        order: int,
        market_id: str,
        *,
        ambiguous: bool = False,
    ) -> None:
        market = markets[market_id]
        state = family[market_id]
        up_book = market.books.get(market.up_token_id)
        down_book = market.books.get(market.down_token_id)
        up_midpoint = None
        down_midpoint = None
        observation_epoch = None
        if ambiguous:
            counters["grid_same_time_ambiguous"] += 1
        elif (
            connected
            and up_book is not None
            and down_book is not None
            and up_book.valid_at(sample_ms, cfg.sample_fresh_ms)
            and down_book.valid_at(sample_ms, cfg.sample_fresh_ms)
        ):
            up_midpoint = up_book.midpoint
            down_midpoint = down_book.midpoint
            if up_midpoint is not None and down_midpoint is not None:
                observation_epoch = epoch
            else:
                counters["grid_missing"] += 1
        else:
            counters["grid_missing"] += 1
        state.history.append(
            _Observation(
                sample_ms,
                up_midpoint,
                down_midpoint,
                observation_epoch,
            )
        )
        push_next_grid(sample_ms, order, market_id)
        remaining_s = market.slot + 300.0 - sample_ms / 1_000.0
        if not (
            cfg.minimum_remaining_s
            <= remaining_s
            <= cfg.maximum_remaining_s
        ):
            return
        if len(state.history) != cfg.lookback_s + 1:
            return
        expected_start = sample_ms - cfg.lookback_s * cfg.sample_interval_ms
        if any(
            observation.up_midpoint is None
            or observation.down_midpoint is None
            or observation.epoch != epoch
            or not math.isclose(
                observation.sample_ms,
                expected_start + index * cfg.sample_interval_ms,
                rel_tol=0.0,
                abs_tol=_EPS,
            )
            for index, observation in enumerate(state.history)
        ):
            return
        up_path = [
            cast(float, observation.up_midpoint)
            for observation in state.history
        ]
        down_path = [
            cast(float, observation.down_midpoint)
            for observation in state.history
        ]
        up_variant, down_variant = _direct_variants(up_path, down_path)
        if up_variant is not None and down_variant is not None:
            state.consumed = True
            counters["both_direct_qualified_rejected"] += 1
            return
        if up_variant is not None:
            variant, side = up_variant, "Up"
        elif down_variant is not None:
            variant, side = down_variant, "Down"
        else:
            return
        if state.consumed:
            counters["qualifying_after_consumption"] += 1
            counters[f"{variant}_after_consumption"] += 1
            return
        state.consumed = True
        counters[f"{variant}_triggers"] += 1
        selected_path = up_path if side == "Up" else down_path
        emit_signal(market_id, variant, side, selected_path, sample_ms)

    def execute(item: _Pending, forced_reason: str | None = None) -> None:
        signal = item.signal
        market = markets[signal.market_id]
        state = family[signal.market_id]
        book = market.books.get(signal.buy_asset_id)
        reason = forced_reason
        filled = 0.0
        notional = 0.0
        levels: tuple[tuple[float, float], ...] = ()
        fee = None
        if reason is None and (not connected or signal.epoch != epoch):
            reason = "disconnect"
        elif reason is None and item.due_ms >= (market.slot + 300) * 1_000:
            reason = "market_closed"
        elif reason is None and (
            book is None
            or not book.valid_at(item.due_ms, cfg.execution_fresh_ms)
        ):
            reason = "book_unavailable"
        elif reason is None:
            current_tick = state.tick_sizes.get(signal.buy_asset_id)
            if current_tick is None:
                reason = "missing_tick_size"
            elif not math.isclose(
                signal.fixed_limit / current_tick,
                round(signal.fixed_limit / current_tick),
                rel_tol=0.0,
                abs_tol=_EPS,
            ):
                reason = "invalid_limit_tick"
        if reason is None:
            assert book is not None
            filled, notional, levels = queue_lag._take_levels(
                book,
                cfg.target_shares,
                limit=signal.fixed_limit,
            )
            reason = (
                "ask_above_frozen_limit"
                if filled <= _EPS
                else "partial_fill"
                if filled < cfg.target_shares - _EPS
                else "filled"
            )
            fee = queue_lag._charged_fee(levels, market.fee_rate)
        did_fill = filled > _EPS
        winner = outcomes.get(signal.market_id)
        won = signal.buy_side == winner if did_fill and winner is not None else None
        total_cost = notional + fee if did_fill and fee is not None else None
        pnl = (
            (filled if won else 0.0) - total_cost
            if total_cost is not None and won is not None
            else None
            if did_fill or forced_reason is not None
            else 0.0
        )
        rows.append({
            **asdict(signal),
            "paper_only": True,
            "family": 35,
            "ordering_clock": "local_receipt_ms",
            "evaluation_ms": item.evaluation_ms,
            "evaluation_recv_ms": item.due_ms,
            "target_shares": cfg.target_shares,
            "filled_shares": filled,
            "fill_vwap": notional / filled if did_fill else None,
            "fill_levels": [
                {"price": price, "shares": shares}
                for price, shares in levels
            ],
            "fee_rate": market.fee_rate,
            "fee": fee,
            "total_cost": total_cost,
            "winner": winner,
            "won": won,
            "pnl_per_share": pnl / filled if pnl is not None and did_fill else None,
            "pnl": pnl,
            "filled": did_fill,
            "full_fill": reason == "filled",
            "sent": forced_reason not in {
                "right_censored",
                "equal_receipt_ambiguous",
            },
            "censored": forced_reason is not None,
            "reason": reason,
        })
        counters[f"execution_{reason}"] += 1

    def process_before(cutoff_ms: float, *, inclusive: bool = False) -> None:
        while True:
            grid_due = grid[0][0] if grid else math.inf
            execution_due = pending[0][0] if pending else math.inf
            due = min(grid_due, execution_due)
            ready = (
                due <= cutoff_ms + _EPS
                if inclusive
                else due < cutoff_ms - _EPS
            )
            if not ready:
                return
            if grid_due <= execution_due:
                sample_ms, order, market_id = heapq.heappop(grid)
                sample_grid(sample_ms, order, market_id)
            else:
                _due, _sequence, item = heapq.heappop(pending)
                execute(item)

    def fail_equal(receive_ms: float) -> None:
        while grid and math.isclose(
            grid[0][0], receive_ms, rel_tol=0.0, abs_tol=_EPS
        ):
            sample_ms, order, market_id = heapq.heappop(grid)
            sample_grid(sample_ms, order, market_id, ambiguous=True)
        while pending and math.isclose(
            pending[0][0], receive_ms, rel_tol=0.0, abs_tol=_EPS
        ):
            _due, _sequence, item = heapq.heappop(pending)
            execute(item, "equal_receipt_ambiguous")

    def physical_key(row: Mapping[str, Any]) -> str:
        batch_id = row.get("physical_batch_id")
        if not isinstance(batch_id, str) or not batch_id:
            raise ValueError("strict CLOB event missing physical_batch_id")
        return batch_id

    for batch_id, raw_group in groupby(clob_events, key=physical_key):
        if batch_id in seen_batches:
            raise ValueError("physical_batch_id is not contiguous")
        seen_batches.add(batch_id)
        group = list(raw_group)
        receive_ms = float(group[0]["recv_ms"])
        if receive_ms < last_receive_ms - _EPS:
            raise ValueError("CLOB receipt clock regressed")
        if receive_ms > segment_end_ms + _EPS:
            raise ValueError("CLOB event exceeds declared segment end")
        if any(
            not math.isclose(
                float(row["recv_ms"]),
                receive_ms,
                rel_tol=0.0,
                abs_tol=_EPS,
            )
            for row in group
        ):
            raise ValueError("physical CLOB batch spans receipt clocks")
        for row in group:
            sequence = int(row["seq"])
            if sequence != expected_event_sequence:
                raise ValueError("non-contiguous strict sequence")
            expected_event_sequence += 1
        last_receive_ms = receive_ms
        process_before(receive_ms)
        fail_equal(receive_ms)

        lifecycle = [
            str(row["kind"])
            for row in group
            if str(row["kind"]) in {"clob_connection", "clob_error"}
        ]
        if lifecycle:
            if len(group) != 1:
                raise ValueError("lifecycle event shares a physical batch")
            row_epoch = int(group[0].get("connection_epoch") or 0)
            if group[0]["kind"] == "clob_connection":
                if connected or row_epoch <= epoch:
                    raise ValueError("invalid CLOB connection epoch")
                connected = True
                epoch = row_epoch
            else:
                connected = False
            reset_epoch_state()
            continue
        if not connected:
            counters["outside_connection"] += len(group)
            continue
        if any(int(row.get("connection_epoch") or 0) != epoch for row in group):
            raise ValueError("CLOB event epoch drift")

        for row in group:
            asset_id = str(row.get("asset_id") or "")
            event_market = tokens.get(asset_id)
            if event_market is None:
                counters["unknown_token"] += 1
                continue
            if str(row.get("market_id") or "") != event_market.market_id:
                raise ValueError("token/market mapping mismatch")
            source_ms = float(row["source_ts_ms"])
            book = event_market.books.setdefault(
                asset_id, queue_lag.DirectBook()
            )
            if row["kind"] == "clob_snapshot":
                applied = book.replace(
                    row.get("bids") or (),
                    row.get("asks") or (),
                    receive_ms,
                    source_ms,
                )
            elif row["kind"] == "clob_price_change":
                applied = book.change(
                    str(row.get("side") or ""),
                    row.get("price"),
                    row.get("size"),
                    receive_ms,
                    source_ms,
                )
            else:
                raise ValueError(f"unexpected CLOB event kind: {row['kind']}")
            if not applied:
                counters["source_time_regression"] += 1
                family[event_market.market_id].history.clear()
            if "tick_size" in row:
                tick_size = float(row["tick_size"])
                if not math.isfinite(tick_size) or not 0 < tick_size < 1:
                    raise ValueError("invalid historical tick size")
                family[event_market.market_id].tick_sizes[asset_id] = tick_size

    process_before(segment_end_ms, inclusive=True)
    while pending:
        _due, _sequence, item = heapq.heappop(pending)
        execute(item, "right_censored")
    return rows, counters


def _metrics(
    sample: Sequence[Mapping[str, Any]],
    target_shares: float,
) -> dict[str, Any]:
    censored = [row for row in sample if row.get("censored")]
    intents = [row for row in sample if not row.get("censored")]
    sent = [row for row in intents if row.get("sent")]
    fills = [row for row in intents if row.get("filled")]
    full = [row for row in intents if row.get("full_fill")]
    unresolved = [row for row in fills if row.get("pnl") is None]
    complete = bool(intents) and not unresolved
    known_pnl = sum(
        float(row["pnl"])
        for row in intents
        if row.get("pnl") is not None
    )
    filled_shares = sum(float(row["filled_shares"]) for row in fills)
    return {
        "intents": len(intents),
        "sent": len(sent),
        "censored": len(censored),
        "fills": len(fills),
        "full_fills": len(full),
        "unresolved_fills": len(unresolved),
        "filled_shares": filled_shares,
        "fill_rate_per_intent": len(full) / len(intents) if intents else None,
        "intention_to_trade_pnl": known_pnl if complete else None,
        "intention_to_trade_ev_per_signal": (
            known_pnl / len(intents) if intents and complete else None
        ),
        "intention_to_trade_ev_per_requested_share": (
            known_pnl / (len(intents) * target_shares)
            if intents and complete
            else None
        ),
        "filled_share_ev": (
            known_pnl / filled_shares
            if filled_shares and complete
            else None
        ),
        "resolved_pnl_diagnostic": known_pnl if intents else None,
        "reasons": dict(Counter(str(row.get("reason")) for row in sample)),
    }


def _matching_stratum(row: Mapping[str, Any]) -> tuple[Any, ...] | None:
    required = (
        "buy_side",
        "decision_ms",
        "remaining_s",
        "path_start",
        "path_end",
        "final_move",
        "fixed_limit",
        "decision_spread",
        "decision_depth",
    )
    if any(row.get(name) is None for name in required):
        return None

    def bin_of(name: str, width: float) -> int:
        return math.floor(float(row[name]) / width + _EPS)

    return (
        str(row["buy_side"]),
        math.floor(float(row["decision_ms"]) / 86_400_000.0),
        bin_of("remaining_s", 30.0),
        bin_of("path_start", 0.05),
        bin_of("path_end", 0.01),
        bin_of("final_move", 0.01),
        bin_of("fixed_limit", 0.01),
        bin_of("decision_spread", 0.01),
        bin_of("decision_depth", 5.0),
    )


def _outcome_blind_pairs(
    base: Sequence[Mapping[str, Any]],
    shallow: Sequence[Mapping[str, Any]],
) -> list[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    available: dict[tuple[Any, ...], list[Mapping[str, Any]]] = {}
    for row in sorted(
        shallow,
        key=lambda item: (float(item["decision_ms"]), str(item["market_id"])),
    ):
        stratum = _matching_stratum(row)
        if stratum is not None:
            available.setdefault(stratum, []).append(row)
    pairs: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for base_row in sorted(
        base,
        key=lambda item: (float(item["decision_ms"]), str(item["market_id"])),
    ):
        stratum = _matching_stratum(base_row)
        controls = available.get(stratum, []) if stratum is not None else []
        control_index = next(
            (
                index
                for index, control in enumerate(controls)
                if control["market_id"] != base_row["market_id"]
            ),
            None,
        )
        if control_index is None:
            continue
        pairs.append((base_row, controls.pop(control_index)))
    return pairs


def summarize(
    rows: Sequence[Mapping[str, Any]],
    counters: Mapping[str, int],
    config: AdverseReclaimConfig | None = None,
) -> dict[str, Any]:
    """Summarize development evidence without creating a promotion verdict."""
    cfg = config or AdverseReclaimConfig()
    variants: dict[str, Any] = {}
    samples: dict[tuple[str, float], list[Mapping[str, Any]]] = {}
    for variant in ("base", "shallow", "delay_5s"):
        latencies: dict[str, Any] = {}
        for latency in cfg.evaluation_ms:
            sample = [
                row
                for row in rows
                if row.get("variant") == variant
                and float(row["evaluation_ms"]) == latency
            ]
            samples[(variant, latency)] = sample
            latencies[f"{latency:g}"] = _metrics(
                sample, cfg.target_shares
            )
        variants[variant] = {"latencies": latencies}

    matching: dict[str, Any] = {}
    for latency in cfg.evaluation_ms:
        pairs = _outcome_blind_pairs(
            samples[("base", latency)],
            samples[("shallow", latency)],
        )
        matching[f"{latency:g}"] = {
            "pairs": len(pairs),
            "base": _metrics(
                [base for base, _control in pairs], cfg.target_shares
            ),
            "shallow": _metrics(
                [control for _base, control in pairs], cfg.target_shares
            ),
        }

    return {
        "schema": "adverse-reclaim-development-v1",
        "family": 35,
        "paper_only": True,
        "effective_split": "development",
        "historical_data_status": "previously_inspected_development_only",
        "promotion_eligible": False,
        "formal_pooled_fresh_gate_implemented": False,
        "correction_denominator": cfg.correction_denominator,
        "primary_metric": "intention_to_trade_pnl",
        "fill_model": "displayed_direct_depth_upper_bound",
        "requires_live_fak_calibration": True,
        "control_matching": {
            "outcome_blind": True,
            "without_replacement": True,
            "different_market_required": True,
            "fields": [
                "buy_side",
                "utc_day",
                "remaining_s_30",
                "path_start_0.05",
                "path_end_0.01",
                "final_move_0.01",
                "fixed_limit_0.01",
                "decision_spread_0.01",
                "decision_depth_5",
            ],
            "latencies": matching,
        },
        "counters": dict(sorted(counters.items())),
        "variants": variants,
    }


def replay_archive(
    archive_dir: str | Path,
    *,
    rows_out: str | Path | None = None,
    config: AdverseReclaimConfig | None = None,
) -> dict[str, Any]:
    """Run Family 35 on one already-recorded strict archive."""
    cfg = config or AdverseReclaimConfig()
    root = Path(archive_dir)
    standard = (
        root / "strict"
        if (root / "strict" / "manifest.json").exists()
        else root
    )
    checked = queue_lag.archive.validate_standard_artifact(standard)
    manifest = checked["manifest"]
    if manifest.get("receipt_race_ready") is not True:
        raise ValueError("strict archive is not receipt-race ready")
    mappings = list(
        queue_lag.archive.iter_market_mappings(
            standard / "market_registry.csv.gz"
        )
    )
    outcomes = {
        str(row["market_id"]): str(row["winner"])
        for row in queue_lag.archive.iter_outcomes(
            standard / "market_outcomes.csv.gz"
        )
    }
    rows, counters = replay(
        queue_lag.iter_physical_clob_events(
            standard / "clob_events.jsonl.gz"
        ),
        mappings,
        outcomes,
        segment_end_ms=float(manifest["ended_ms"]),
        config=cfg,
    )
    if rows_out is not None:
        destination = Path(rows_out)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            for row in rows:
                stream.write(
                    json.dumps(row, sort_keys=True, allow_nan=False) + "\n"
                )
        temporary.replace(destination)
    result = summarize(rows, counters, cfg)
    result["dataset"] = {
        "sample_scope": "development_forward_artifact",
        "archive": root.name,
        "run_id": manifest.get("run_id"),
        "collector_region": manifest.get("collector_region"),
        "started_ms": manifest.get("started_ms"),
        "ended_ms": manifest.get("ended_ms"),
        "mapped_markets": len(mappings),
        "outcomes": len(outcomes),
        "receipt_race_ready": manifest.get("receipt_race_ready") is True,
    }
    result["record_count"] = len(rows)
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
