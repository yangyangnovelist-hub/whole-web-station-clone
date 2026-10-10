"""Receipt-causal paper execution for post-sweep absorption signals."""

from __future__ import annotations

import heapq
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from itertools import groupby
from typing import Any

from absorption import TokenBook
from basis_wedge_run import _fill_direct, _rounded_fee

EVALUATION_MS = (400.0, 500.0)
EXIT_WAIT_MS = 5_000.0
TIME_SHIFT_MS = 5_000.0
TARGET_SHARES = 5.0
DEFAULT_FEE_RATE = 0.07
_EPS = 1e-9


@dataclass
class _Market:
    market_id: str
    up_token_id: str
    down_token_id: str
    books: dict[str, TokenBook] = field(default_factory=dict)
    tainted: bool = False

    def reset(self) -> None:
        self.books.clear()
        self.tainted = False


@dataclass
class _Order:
    due_ms: float
    sequence: int
    evaluation_ms: float
    epoch: int
    signal: Mapping[str, Any]


@dataclass
class _Position:
    due_ms: float
    sequence: int
    evaluation_ms: float
    epoch: int
    row: dict[str, Any]


@dataclass
class _DelayedDecision:
    due_ms: float
    sequence: int
    epoch: int
    signal: Mapping[str, Any]


def _sell_direct(
    book: TokenBook,
    shares: float,
    fee_rate: float,
) -> tuple[str, float, float | None, float, float, tuple[tuple[float, float], ...]]:
    if not book.ready:
        return "book_unavailable", 0.0, None, 0.0, 0.0, ()
    remaining = shares
    levels: list[tuple[float, float]] = []
    notional = 0.0
    for price, available in sorted(book.bids.items(), reverse=True):
        take = min(remaining, available)
        if take <= 0:
            continue
        levels.append((price, take))
        notional += price * take
        remaining -= take
        if remaining <= _EPS:
            break
    sold = shares - remaining
    if sold <= _EPS:
        return "no_direct_bid", 0.0, None, 0.0, 0.0, ()
    reason = "filled" if remaining <= _EPS else "partial_fill"
    return (
        reason,
        sold,
        notional / sold,
        _rounded_fee(levels, fee_rate),
        notional,
        tuple(levels),
    )


def _levels_json(levels: Iterable[tuple[float, float]]) -> list[dict[str, float]]:
    return [{"price": price, "shares": shares} for price, shares in levels]


def replay_execution(
    events: Iterable[Mapping[str, Any]],
    signals: Iterable[Mapping[str, Any]],
    outcomes: Mapping[str, str],
    *,
    evaluation_ms: Iterable[float] = EVALUATION_MS,
    book_fresh_ms: float = 2_000.0,
    fee_rate: float = DEFAULT_FEE_RATE,
    include_controls: bool = False,
    recording_end_ms: float | None = None,
) -> list[dict[str, Any]]:
    """Replay fixed-limit FAK entry, delayed unwind and residual settlement.

    All causality uses local receipt time. Events sharing a receipt timestamp are
    applied atomically before an order due at that exact timestamp is evaluated.
    """
    latencies = tuple(float(value) for value in evaluation_ms)
    if not latencies or any(not math.isfinite(value) or value < 0 for value in latencies):
        raise ValueError("evaluation_ms must contain non-negative finite values")
    if not math.isfinite(book_fresh_ms) or book_fresh_ms < 0:
        raise ValueError("book_fresh_ms must be non-negative and finite")

    markets: dict[str, _Market] = {}
    token_market: dict[str, str] = {}
    entries: list[tuple[float, int, _Order]] = []
    exits: list[tuple[float, int, _Position]] = []
    delayed: list[tuple[float, int, _DelayedDecision]] = []
    rows: list[dict[str, Any]] = []
    sequence = 0
    epoch = 0
    connected = False
    last_event_ms = -math.inf

    def reset_books() -> None:
        for market in markets.values():
            market.reset()

    def market_for(signal: Mapping[str, Any]) -> _Market | None:
        return markets.get(str(signal.get("market_id") or ""))

    def schedule(signal: Mapping[str, Any]) -> None:
        nonlocal sequence
        decision_ms = float(signal["decision_recv_ms"])
        for latency in latencies:
            order = _Order(
                decision_ms + latency,
                sequence,
                latency,
                epoch,
                signal,
            )
            heapq.heappush(entries, (order.due_ms, order.sequence, order))
            sequence += 1

    def decision_fill(
        signal: Mapping[str, Any],
        asset_id: str,
        decision_ms: float,
        expected_epoch: int,
        fixed_limit: float,
    ) -> tuple[
        str | None,
        float | None,
        tuple[tuple[float, float], ...],
    ]:
        market = market_for(signal)
        if market is None:
            return "unknown_market", None, ()
        if expected_epoch != epoch or not connected:
            return "disconnect", None, ()
        if market.tainted:
            return "equal_receipt_ambiguity", None, ()
        book = market.books.get(asset_id)
        if book is None or not book.valid_at(decision_ms, book_fresh_ms):
            return "stale_book", None, ()
        if book.recv_ms > decision_ms + _EPS:
            return "future_book_guard", None, ()
        reason, shares, vwap, _fee, _notional, levels = _fill_direct(
            book, fixed_limit, TARGET_SHARES, fee_rate,
        )
        if reason == "filled" and shares + _EPS >= TARGET_SHARES:
            return None, vwap, levels
        suffix = {
            "book_unavailable": "book_unavailable",
            "no_direct_ask": "no_direct_ask",
            "ask_above_frozen_limit": "ask_above_limit",
            "partial_fill": "insufficient_direct_depth",
            "insufficient_direct_depth": "insufficient_direct_depth",
        }.get(reason, reason)
        return suffix, vwap, levels

    def direction_reversal(signal: Mapping[str, Any]) -> dict[str, Any]:
        asset_id = str(signal.get("swept_asset_id") or "")
        side = signal.get("swept_side")
        reason, vwap, levels = decision_fill(
            signal,
            asset_id,
            float(signal["decision_recv_ms"]),
            epoch,
            1.0,
        )
        if not asset_id or side is None:
            reason = "missing_swept_token"
            levels = ()
            vwap = None
        parent_id = str(signal.get("signal_id") or "")
        control = dict(signal)
        control.update({
            "signal_id": f"{parent_id}:direction_reversal",
            "parent_signal_id": parent_id,
            "variant": "direction_reversal",
            "buy_asset_id": asset_id,
            "buy_side": side,
            "decision_vwap": vwap,
            "fixed_limit": levels[-1][0] if reason is None else None,
            "fill_levels": _levels_json(levels),
            "sent": reason is None,
            "reason": (
                "eligible"
                if reason is None
                else f"direction_reversal_decision_{reason}"
            ),
        })
        return control

    def schedule_signal(signal: Mapping[str, Any]) -> None:
        nonlocal sequence
        schedule(signal)
        if not include_controls or signal.get("variant") != "base":
            return
        schedule(direction_reversal(signal))
        decision = _DelayedDecision(
            float(signal["decision_recv_ms"]) + TIME_SHIFT_MS,
            sequence,
            epoch,
            signal,
        )
        heapq.heappush(delayed, (decision.due_ms, decision.sequence, decision))
        sequence += 1

    def settle(row: dict[str, Any], reason: str) -> None:
        residual = max(0.0, row["entry_shares"] - row["exit_shares"])
        winner = outcomes.get(row["market_id"])
        row["exit_reason"] = reason
        row["settled_shares"] = residual
        row["winner"] = winner
        if residual <= _EPS:
            payout = 0.0
        elif winner is None:
            row["settlement_payout"] = None
            row["pnl"] = None
            row["pnl_per_share"] = None
            row["profitable"] = None
            return
        else:
            payout = residual if winner.lower() == str(row["buy_side"]).lower() else 0.0
        row["settlement_payout"] = payout
        row["pnl"] = (
            row["exit_notional"] - row["exit_fee"] + payout
            - row["entry_notional"] - row["entry_fee"]
        )
        row["pnl_per_share"] = row["pnl"] / row["entry_shares"]
        row["profitable"] = row["pnl"] > 0

    def evaluate_entry(item: _Order) -> None:
        signal = item.signal
        market = market_for(signal)
        reason: str | None = None
        book: TokenBook | None = None
        if not bool(signal.get("sent")) or signal.get("fixed_limit") is None:
            reason = str(signal.get("reason") or "not_sent")
        elif market is None:
            reason = "unknown_market"
        elif item.epoch != epoch or not connected:
            reason = "disconnect"
        elif market.tainted:
            reason = "equal_receipt_ambiguity"
        else:
            book = market.books.get(str(signal.get("buy_asset_id") or ""))
            if book is None or not book.valid_at(item.due_ms, book_fresh_ms):
                reason = "stale_book"
            elif book.recv_ms > item.due_ms + _EPS:
                reason = "future_book_guard"

        shares = 0.0
        vwap = fee = notional = None
        fill_levels: tuple[tuple[float, float], ...] = ()
        if reason is None and book is not None:
            reason, shares, vwap, fee, notional, fill_levels = _fill_direct(
                book,
                float(signal["fixed_limit"]),
                TARGET_SHARES,
                fee_rate,
            )
        filled = reason in {"filled", "partial_fill"}
        row = {
            "signal_id": signal.get("signal_id"),
            "parent_signal_id": signal.get("parent_signal_id"),
            "variant": signal.get("variant", "base"),
            "paper_only": True,
            "ordering_clock": "local_receipt_ms",
            "signal": dict(signal),
            "market_id": str(signal.get("market_id") or ""),
            "buy_asset_id": signal.get("buy_asset_id"),
            "buy_side": signal.get("buy_side"),
            "decision_recv_ms": float(signal.get("decision_recv_ms", -math.inf)),
            "evaluation_ms": item.evaluation_ms,
            "entry_match_ms": item.due_ms,
            "entry_book_recv_ms": book.recv_ms if book is not None else None,
            "target_shares": TARGET_SHARES,
            "fixed_limit": signal.get("fixed_limit"),
            "fee_rate": fee_rate,
            "sent": bool(signal.get("sent")),
            "entry_reason": reason,
            "entry_shares": shares if filled else 0.0,
            "filled": filled,
            "full_fill": filled and shares + _EPS >= TARGET_SHARES,
            "entry_vwap": vwap if filled else None,
            "entry_levels": _levels_json(fill_levels),
            "entry_fee": fee if filled and fee is not None else 0.0,
            "entry_notional": notional if filled and notional is not None else 0.0,
            "exit_decision_ms": item.due_ms + EXIT_WAIT_MS if filled else None,
            "exit_match_ms": item.due_ms + EXIT_WAIT_MS + item.evaluation_ms if filled else None,
            "exit_book_recv_ms": None,
            "exit_reason": "not_entered" if not filled else None,
            "exit_shares": 0.0,
            "exit_vwap": None,
            "exit_levels": [],
            "exit_fee": 0.0,
            "exit_notional": 0.0,
            "settled_shares": 0.0,
            "settlement_payout": 0.0,
            "winner": outcomes.get(str(signal.get("market_id") or "")),
            "pnl": 0.0 if not filled else None,
            "pnl_per_share": None,
            "profitable": None,
        }
        if "scheduled_decision_recv_ms" in signal:
            row["scheduled_decision_recv_ms"] = float(
                signal["scheduled_decision_recv_ms"]
            )
        if not filled:
            rows.append(row)
            return
        position = _Position(
            float(row["exit_match_ms"]), item.sequence, item.evaluation_ms, item.epoch, row,
        )
        heapq.heappush(exits, (position.due_ms, position.sequence, position))

    def evaluate_exit(position: _Position) -> None:
        row = position.row
        market = markets.get(row["market_id"])
        if market is None or position.epoch != epoch or not connected:
            settle(row, "disconnect_hold")
            rows.append(row)
            return
        if market.tainted:
            settle(row, "equal_receipt_ambiguity_hold")
            rows.append(row)
            return
        book = market.books.get(str(row["buy_asset_id"] or ""))
        if book is None or not book.valid_at(position.due_ms, book_fresh_ms):
            settle(row, "stale_book_hold")
            rows.append(row)
            return
        if book.recv_ms > position.due_ms + _EPS:
            settle(row, "future_book_guard_hold")
            rows.append(row)
            return
        reason, shares, vwap, fee, notional, levels = _sell_direct(
            book, row["entry_shares"], fee_rate,
        )
        row.update({
            "exit_book_recv_ms": book.recv_ms,
            "exit_shares": shares,
            "exit_vwap": vwap,
            "exit_levels": _levels_json(levels),
            "exit_fee": fee,
            "exit_notional": notional,
        })
        settle(row, reason)
        rows.append(row)

    def decide_time_shift(item: _DelayedDecision) -> None:
        signal = item.signal
        fixed_limit = signal.get("fixed_limit")
        levels: tuple[tuple[float, float], ...] = ()
        vwap = None
        if not bool(signal.get("sent")) or fixed_limit is None:
            reason = "parent_not_sent"
        else:
            reason, vwap, levels = decision_fill(
                signal,
                str(signal.get("buy_asset_id") or ""),
                item.due_ms,
                item.epoch,
                float(fixed_limit),
            )
        parent_id = str(signal.get("signal_id") or "")
        control = dict(signal)
        control.update({
            "signal_id": f"{parent_id}:time_shift",
            "parent_signal_id": parent_id,
            "variant": "time_shift",
            "decision_recv_ms": item.due_ms,
            "scheduled_decision_recv_ms": item.due_ms,
            "decision_vwap": vwap,
            "fill_levels": _levels_json(levels),
            "sent": reason is None,
            "reason": (
                "eligible"
                if reason is None
                else f"shifted_decision_{reason}"
            ),
        })
        schedule(control)

    def drain(before_ms: float, *, inclusive: bool) -> None:
        compare = (lambda due: due <= before_ms + _EPS) if inclusive else (
            lambda due: due < before_ms - _EPS
        )
        while True:
            entry_due = entries[0][0] if entries else math.inf
            exit_due = exits[0][0] if exits else math.inf
            delayed_due = delayed[0][0] if delayed else math.inf
            due = min(entry_due, exit_due, delayed_due)
            if not compare(due):
                return
            if delayed_due <= entry_due and delayed_due <= exit_due:
                decide_time_shift(heapq.heappop(delayed)[2])
            elif entry_due <= exit_due:
                evaluate_entry(heapq.heappop(entries)[2])
            else:
                evaluate_exit(heapq.heappop(exits)[2])

    def apply_event(event: Mapping[str, Any]) -> None:
        nonlocal connected, epoch
        kind = str(event.get("kind") or "")
        recv_ms = float(event["recv_ms"])
        if kind == "market":
            market_id = str(event["market_id"])
            up = str(event["up_token_id"])
            down = str(event["down_token_id"])
            markets[market_id] = _Market(market_id, up, down)
            token_market[up] = market_id
            token_market[down] = market_id
            return
        if kind == "connection":
            epoch = int(event.get("epoch") or epoch + 1)
            connected = True
            reset_books()
            return
        if kind == "disconnect":
            connected = False
            reset_books()
            return
        if not connected:
            return
        event_epoch = event.get("epoch")
        if event_epoch is not None and int(event_epoch) != epoch:
            return
        market_id = str(event.get("market_id") or token_market.get(str(event.get("asset_id") or "")) or "")
        market = markets.get(market_id)
        if market is None:
            return
        if event.get("ambiguous_receipt") is True:
            market.tainted = True
            return
        if kind == "snapshot":
            token = str(event["asset_id"])
            book = market.books.setdefault(token, TokenBook())
            try:
                book.replace(event.get("bids") or (), event.get("asks") or (), recv_ms)
            except (KeyError, TypeError, ValueError):
                book.ready = False
        elif kind == "price_change":
            for change in event.get("changes") or ():
                token = str(change.get("asset_id") or "")
                book = market.books.setdefault(token, TokenBook())
                book.change(
                    str(change.get("side") or ""),
                    change.get("price"),
                    change.get("size"),
                    recv_ms,
                )

    event_timeline = (
        (float(row["recv_ms"]), 0, row)
        for row in _ordered_rows(events, "event")
    )
    signal_timeline = (
        (float(row["decision_recv_ms"]), 1, row)
        for row in _ordered_rows(signals, "signal", clock="decision_recv_ms")
    )
    timeline = heapq.merge(
        event_timeline,
        signal_timeline,
        key=lambda item: (item[0], item[1]),
    )
    end_limit = float(recording_end_ms) if recording_end_ms is not None else math.inf
    for recv_ms, group in groupby(timeline, key=lambda item: item[0]):
        if recv_ms > end_limit + _EPS:
            break
        drain(recv_ms, inclusive=False)
        grouped = list(group)
        for _clock, kind, payload in grouped:
            if kind == 0:
                last_event_ms = recv_ms
                apply_event(payload)
        for _clock, kind, signal in grouped:
            if kind != 1:
                continue
            schedule_signal(signal)
        drain(recv_ms, inclusive=True)

    end_ms = (
        float(recording_end_ms)
        if recording_end_ms is not None
        else last_event_ms
    )
    drain(end_ms, inclusive=True)
    while delayed:
        item = heapq.heappop(delayed)[2]
        parent_id = str(item.signal.get("signal_id") or "")
        control = dict(item.signal)
        control.update({
            "signal_id": f"{parent_id}:time_shift",
            "parent_signal_id": parent_id,
            "variant": "time_shift",
            "decision_recv_ms": item.due_ms,
            "scheduled_decision_recv_ms": item.due_ms,
            "sent": False,
            "reason": "recording_end_censored_before_shifted_decision",
        })
        schedule(control)
    while exits:
        position = heapq.heappop(exits)[2]
        settle(position.row, "recording_end_hold")
        rows.append(position.row)
    while entries:
        item = heapq.heappop(entries)[2]
        row_signal = item.signal
        row = {
            "signal_id": row_signal.get("signal_id"),
            "parent_signal_id": row_signal.get("parent_signal_id"),
            "variant": row_signal.get("variant", "base"),
            "paper_only": True,
            "ordering_clock": "local_receipt_ms",
            "signal": dict(row_signal),
            "market_id": str(row_signal.get("market_id") or ""),
            "buy_asset_id": row_signal.get("buy_asset_id"),
            "buy_side": row_signal.get("buy_side"),
            "decision_recv_ms": float(row_signal.get("decision_recv_ms", -math.inf)),
            "evaluation_ms": item.evaluation_ms,
            "entry_match_ms": item.due_ms,
            "entry_book_recv_ms": None,
            "target_shares": TARGET_SHARES,
            "fixed_limit": row_signal.get("fixed_limit"),
            "fee_rate": fee_rate,
            "sent": bool(row_signal.get("sent")),
            "entry_reason": (
                str(row_signal.get("reason") or "not_sent")
                if (
                    row_signal.get("variant") in {"direction_reversal", "time_shift"}
                    and not bool(row_signal.get("sent"))
                )
                else "recording_end_censored"
            ),
            "entry_shares": 0.0,
            "filled": False,
            "full_fill": False,
            "entry_vwap": None,
            "entry_levels": [],
            "entry_fee": 0.0,
            "entry_notional": 0.0,
            "exit_decision_ms": None,
            "exit_match_ms": None,
            "exit_book_recv_ms": None,
            "exit_reason": "not_entered",
            "exit_shares": 0.0,
            "exit_vwap": None,
            "exit_levels": [],
            "exit_fee": 0.0,
            "exit_notional": 0.0,
            "settled_shares": 0.0,
            "settlement_payout": 0.0,
            "winner": outcomes.get(str(row_signal.get("market_id") or "")),
            "pnl": 0.0,
            "pnl_per_share": None,
            "profitable": None,
        }
        if "scheduled_decision_recv_ms" in row_signal:
            row["scheduled_decision_recv_ms"] = float(
                row_signal["scheduled_decision_recv_ms"]
            )
        rows.append(row)
    return sorted(
        rows,
        key=lambda row: (
            float(row["decision_recv_ms"]),
            str(row.get("signal_id") or ""),
            float(row["evaluation_ms"]),
        ),
    )


def _ordered_rows(
    rows: Iterable[Mapping[str, Any]],
    label: str,
    *,
    clock: str = "recv_ms",
) -> Iterable[Mapping[str, Any]]:
    previous = -math.inf
    for row in rows:
        current = float(row[clock])
        if not math.isfinite(current) or current < previous:
            raise ValueError(f"{label} {clock} values must be finite and nondecreasing")
        previous = current
        yield row
