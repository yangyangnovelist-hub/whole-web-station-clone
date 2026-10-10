"""Receipt-causal replay for protected complementary Polymarket maker pairs.

The complementary quote geometry follows the reservation-price construction used by the
MIT-licensed ``warproxxx/poly-maker`` project: BUY-Up at ``r-delta`` and BUY-Down at
``1-r-delta`` creates a pair whose prices sum below one.  This module deliberately does not reuse
that project's optimistic fill model.  It uses direct CLOB lifecycle events, displayed queue ahead,
observed sell aggression and settlement outcomes.
"""

from __future__ import annotations

import argparse
import heapq
import json
import math
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from absorption import TokenBook

EPS = 1e-12


def _receipt_ns(event: Mapping[str, Any]) -> int:
    raw = event.get("recv_ns")
    if raw is not None:
        return int(raw)
    recv_ms = float(event["recv_ms"])
    return round(recv_ms * 1_000_000)


@dataclass(frozen=True)
class PairedMakerConfig:
    shares: float = 5.0
    maximum_pair_cost: float = 0.98
    minimum_locked_edge: float = 0.02
    placement_latency_ms: float = 30.0
    cancel_latency_ms: float = 30.0
    reprice_latency_ms: float = 30.0
    external_cancel_delay_ms: float | None = 30.0
    quote_tau_hi_s: float = 240.0
    quote_tau_lo_s: float = 30.0
    end_cancel_tau_s: float = 10.0
    book_fresh_ms: float = 2_000.0
    tick_size: float = 0.01
    queue_cancellation_credit: bool = False
    fee_curve_rate: float = 0.07
    maker_rebate_fraction: float = 0.20
    rescue_mode: str = "hold"
    rescue_trigger: str = "first_fill"
    rescue_latency_ms: float = 300.0
    rescue_min_shares: float = 5.0

    def __post_init__(self) -> None:
        finite_positive = (
            self.shares,
            self.placement_latency_ms,
            self.cancel_latency_ms,
            self.reprice_latency_ms,
            self.quote_tau_hi_s,
            self.quote_tau_lo_s,
            self.end_cancel_tau_s,
            self.book_fresh_ms,
            self.tick_size,
            self.rescue_latency_ms,
            self.rescue_min_shares,
        )
        if not all(math.isfinite(value) and value > 0 for value in finite_positive):
            raise ValueError("paired-maker timing, size and tick parameters must be positive")
        if not 0 < self.minimum_locked_edge < 1:
            raise ValueError("minimum locked edge must be a probability")
        if not 0 < self.maximum_pair_cost <= 1 - self.minimum_locked_edge + EPS:
            raise ValueError("maximum pair cost does not preserve the locked edge")
        if self.quote_tau_hi_s <= self.quote_tau_lo_s:
            raise ValueError("quote interval is empty")
        if self.external_cancel_delay_ms is not None and (
            not math.isfinite(self.external_cancel_delay_ms)
            or self.external_cancel_delay_ms < 0
        ):
            raise ValueError("external cancel delay must be non-negative or None")
        if not 0 <= self.fee_curve_rate <= 1 or not 0 <= self.maker_rebate_fraction <= 1:
            raise ValueError("fee and rebate rates must be probabilities")
        if self.rescue_mode not in {"hold", "hedge", "unwind"}:
            raise ValueError("rescue mode must be hold, hedge or unwind")
        if self.rescue_trigger not in {"first_fill", "end_cancel"}:
            raise ValueError("rescue trigger must be first_fill or end_cancel")


@dataclass
class MakerOrder:
    side: str
    asset_id: str
    price: float
    target_shares: float
    queue_ahead: float = 0.0
    filled: float = 0.0
    fill_cost: float = 0.0
    fill_fee_equivalent: float = 0.0
    active: bool = False
    activation_ms: float | None = None
    cancelled_ms: float | None = None
    reprice_count: int = 0

    @property
    def remaining(self) -> float:
        return max(0.0, self.target_shares - self.filled)

    @property
    def average_fill_price(self) -> float | None:
        return self.fill_cost / self.filled if self.filled > EPS else None

    def fill(self, shares: float, fee_curve_rate: float = 0.07) -> float:
        take = min(max(0.0, shares), self.remaining)
        if take > EPS:
            self.filled += take
            self.fill_cost += take * self.price
            self.fill_fee_equivalent += (
                take * fee_curve_rate * self.price * (1.0 - self.price)
            )
            if self.remaining <= EPS:
                self.active = False
        return take


@dataclass
class RescueOrder:
    kind: str
    asset_id: str
    target_shares: float
    limit_price: float
    decision_ms: float
    due_ms: float
    filled: float = 0.0
    vwap: float | None = None
    fee: float = 0.0
    notional: float = 0.0
    levels: tuple[tuple[float, float], ...] = ()
    reason: str | None = None


@dataclass
class MarketState:
    market_id: str
    slot: int
    up_token_id: str
    down_token_id: str
    up_book: TokenBook = field(default_factory=TokenBook)
    down_book: TokenBook = field(default_factory=TokenBook)
    quote_taken: bool = False
    quote_decision_ms: float | None = None
    placement_due_ms: float | None = None
    activation_ms: float | None = None
    up_order: MakerOrder | None = None
    down_order: MakerOrder | None = None
    cancel_due_ms: float | None = None
    cancel_effective_ms: float | None = None
    cancel_source: str | None = None
    warning_ms: float | None = None
    warning_source: str | None = None
    warning_direction: int | None = None
    warning_action: str | None = None
    end_cancel_due_ms: float | None = None
    reprice_due_ms: float | None = None
    repriced_side: str | None = None
    taint_reason: str | None = None
    taint_ms: float | None = None
    unknown_live_order_shares: float = 0.0
    placement_reason: str | None = None
    rescue_cancel_due_ms: float | None = None
    rescue_cancel_effective_ms: float | None = None
    rescue_order: RescueOrder | None = None
    rescue_status: str | None = None

    @property
    def end_ms(self) -> float:
        return (self.slot + 300) * 1_000.0

    @property
    def orders(self) -> tuple[MakerOrder, MakerOrder] | tuple[()]:
        if self.up_order is None or self.down_order is None:
            return ()
        return self.up_order, self.down_order


class PairedMakerReplay:
    """Streaming state machine with conservative maker queue semantics."""

    def __init__(self, config: PairedMakerConfig | None = None) -> None:
        self.config = config or PairedMakerConfig()
        self.markets: dict[str, MarketState] = {}
        self.slots: dict[int, list[str]] = {}
        self.tokens: dict[str, tuple[str, str]] = {}
        self.connected = False
        self.epoch = 0
        self.last_recv_ms = -math.inf
        self.last_recv_ns = -1
        self._closed_recv_ms = -math.inf
        self._closed_recv_ns = -1
        self._pending_stamp_ms: float | None = None
        self._pending_stamp_ns: int | None = None
        self._pending_events: list[dict[str, Any]] = []
        self._seen_trade_events: set[tuple[str, str, float, float]] = set()

    def market(self, market_id: str) -> MarketState:
        return self.markets[market_id]

    def on_event(self, event: Mapping[str, Any]) -> None:
        recv_ms = float(event.get("recv_ms", -math.inf))
        if not math.isfinite(recv_ms):
            raise ValueError("event is missing a finite receipt clock")
        recv_ns = _receipt_ns(event)
        if recv_ns < self.last_recv_ns or recv_ns <= self._closed_recv_ns:
            raise ValueError("receipt clock regressed")
        if self._pending_stamp_ns is None:
            self._pending_stamp_ns = recv_ns
            self._pending_stamp_ms = recv_ms
        elif recv_ns > self._pending_stamp_ns:
            self._release_pending()
            self._pending_stamp_ns = recv_ns
            self._pending_stamp_ms = recv_ms
        self._pending_events.append(dict(event))
        self.last_recv_ms = recv_ms
        self.last_recv_ns = recv_ns

    def flush(self, recv_ms: float) -> None:
        stamp = float(recv_ms)
        if stamp < self.last_recv_ms - EPS:
            raise ValueError("flush clock regressed")
        self._release_pending()
        self._advance(stamp, inclusive=True)
        self.last_recv_ms = stamp
        self.last_recv_ns = max(self.last_recv_ns, round(stamp * 1_000_000))
        self._closed_recv_ms = stamp
        self._closed_recv_ns = self.last_recv_ns

    def _release_pending(self) -> None:
        if self._pending_stamp_ns is None or self._pending_stamp_ms is None:
            return
        stamp = self._pending_stamp_ms
        stamp_ns = self._pending_stamp_ns
        events = self._pending_events
        self._pending_stamp_ms = None
        self._pending_stamp_ns = None
        self._pending_events = []
        self._advance(stamp, inclusive=False)

        clob_slots: set[int] = set()
        for event in events:
            if str(event.get("kind") or "") not in {"snapshot", "price_change", "trade"}:
                continue
            market_id = self._event_market(event)
            state = self.markets.get(market_id) if market_id is not None else None
            if state is not None:
                clob_slots.add(state.slot)

        touched: set[str] = set()
        for raw_event in events:
            event = dict(raw_event)
            kind = str(event.get("kind") or "")
            if kind == "external_jump" and int(event.get("slot", -1)) in clob_slots:
                event["ambiguous_receipt"] = True
            if kind == "market":
                self._register_market(event)
            elif kind == "connection":
                self._connect(event)
            elif kind == "disconnect":
                self._disconnect(stamp)
            elif kind == "external_jump":
                self._external_jump(event)
            elif self.connected:
                market_id = self._event_market(event)
                if market_id is None:
                    continue
                state = self.markets.get(market_id)
                if state is None:
                    continue
                touched.add(market_id)
                if event.get("ambiguous_receipt") is True:
                    self._taint(state, "equal_receipt_ambiguity", stamp)
                elif state.taint_reason is None:
                    if kind == "snapshot":
                        self._snapshot(state, event)
                    elif kind == "price_change":
                        self._price_change(state, event)
                    elif kind == "trade":
                        self._trade(state, event)
        for market_id in touched:
            state = self.markets[market_id]
            if self.connected and state.taint_reason is None:
                self._try_quote(state, stamp)
        self._advance(stamp, inclusive=True)
        self._closed_recv_ms = stamp
        self._closed_recv_ns = stamp_ns

    def finish(self, outcomes: Mapping[str, str]) -> list[dict[str, Any]]:
        if self.markets:
            self.flush(max(state.end_ms + self.config.cancel_latency_ms for state in self.markets.values()))
        rows: list[dict[str, Any]] = []
        for state in sorted(self.markets.values(), key=lambda item: (item.slot, item.market_id)):
            if not state.quote_taken:
                continue
            up = state.up_order
            down = state.down_order
            maker_up_filled = up.filled if up is not None else 0.0
            maker_down_filled = down.filled if down is not None else 0.0
            up_cost = up.fill_cost if up is not None else 0.0
            down_cost = down.fill_cost if down is not None else 0.0
            up_filled = maker_up_filled
            down_filled = maker_down_filled
            rescue = state.rescue_order
            rescue_fee = rescue.fee if rescue is not None else 0.0
            rescue_notional = rescue.notional if rescue is not None else 0.0
            if rescue is not None and rescue.kind == "hedge":
                if rescue.asset_id == state.up_token_id:
                    up_filled += rescue.filled
                    up_cost += rescue.notional
                else:
                    down_filled += rescue.filled
                    down_cost += rescue.notional
            elif rescue is not None and rescue.kind == "unwind":
                if rescue.asset_id == state.up_token_id:
                    up_filled = max(0.0, up_filled - rescue.filled)
                    up_cost -= rescue.notional
                else:
                    down_filled = max(0.0, down_filled - rescue.filled)
                    down_cost -= rescue.notional
            paired = min(up_filled, down_filled)
            winner = outcomes.get(state.market_id)
            net_holdings = up_filled + down_filled
            gross_filled = maker_up_filled + maker_down_filled + (
                rescue.filled if rescue is not None else 0.0
            )
            if net_holdings > EPS and winner not in {"Up", "Down"}:
                raise ValueError(f"missing official outcome for filled market {state.market_id}")
            eligible = state.activation_ms is not None and (
                state.taint_reason is None or gross_filled > EPS
            )
            payout = None
            pnl = None
            if eligible and winner in {"Up", "Down"}:
                payout = up_filled if winner == "Up" else down_filled
                pnl = payout - up_cost - down_cost - rescue_fee
            elif eligible and net_holdings <= EPS:
                payout = 0.0
                pnl = -up_cost - down_cost - rescue_fee
            fee_equivalent = (
                (up.fill_fee_equivalent if up is not None else 0.0)
                + (down.fill_fee_equivalent if down is not None else 0.0)
            )
            rebate_sensitivity = fee_equivalent * self.config.maker_rebate_fraction
            up_average = up.average_fill_price if up is not None else None
            down_average = down.average_fill_price if down is not None else None
            locked_edge = (
                1.0 - (up_cost + down_cost + rescue_fee) / paired
                if paired > EPS and abs(up_filled - down_filled) <= EPS
                else None
            )
            rows.append({
                "market_id": state.market_id,
                "slot": state.slot,
                "eligible": eligible,
                "taint_reason": state.taint_reason,
                "taint_ms": state.taint_ms,
                "unknown_live_order_shares": state.unknown_live_order_shares,
                "placement_reason": state.placement_reason,
                "quote_decision_ms": state.quote_decision_ms,
                "activation_ms": state.activation_ms,
                "up_quote_price": up.price if up is not None else None,
                "down_quote_price": down.price if down is not None else None,
                "maker_up_filled": maker_up_filled,
                "maker_down_filled": maker_down_filled,
                "up_filled": up_filled,
                "down_filled": down_filled,
                "up_fill_price": up_average,
                "down_fill_price": down_average,
                "up_cost": up_cost,
                "down_cost": down_cost,
                "paired_shares": paired,
                "unmatched_up": max(0.0, up_filled - paired),
                "unmatched_down": max(0.0, down_filled - paired),
                "locked_edge_per_pair": locked_edge,
                "cancel_source": state.cancel_source,
                "warning_ms": state.warning_ms,
                "warning_source": state.warning_source,
                "warning_direction": state.warning_direction,
                "warning_action": state.warning_action,
                "cancel_effective_ms": state.cancel_effective_ms,
                "winner": winner,
                "payout": payout,
                "pnl": pnl,
                "gross_filled_shares": gross_filled,
                "rescue_kind": rescue.kind if rescue is not None else None,
                "rescue_decision_ms": rescue.decision_ms if rescue is not None else None,
                "rescue_due_ms": rescue.due_ms if rescue is not None else None,
                "rescue_cancel_effective_ms": state.rescue_cancel_effective_ms,
                "rescue_limit": rescue.limit_price if rescue is not None else None,
                "rescue_target": rescue.target_shares if rescue is not None else 0.0,
                "rescue_filled": rescue.filled if rescue is not None else 0.0,
                "rescue_vwap": rescue.vwap if rescue is not None else None,
                "rescue_fee": rescue_fee,
                "rescue_notional": rescue_notional,
                "rescue_levels": rescue.levels if rescue is not None else (),
                "rescue_reason": (
                    rescue.reason if rescue is not None else state.rescue_status
                ),
                "maker_fee_equivalent": fee_equivalent,
                "maker_rebate_sensitivity": rebate_sensitivity,
                "pnl_with_rebate_sensitivity": (
                    pnl + rebate_sensitivity if pnl is not None else None
                ),
                "completed_pair": bool(
                    up_filled + EPS >= self.config.shares
                    and down_filled + EPS >= self.config.shares
                ),
            })
        return rows

    def _register_market(self, event: Mapping[str, Any]) -> None:
        market_id = str(event["market_id"])
        if market_id in self.markets:
            return
        slot = int(event["slot"])
        up = str(event["up_token_id"])
        down = str(event["down_token_id"])
        state = MarketState(market_id, slot, up, down)
        self.markets[market_id] = state
        self.slots.setdefault(slot, []).append(market_id)
        self.tokens[up] = (market_id, "Up")
        self.tokens[down] = (market_id, "Down")

    def _connect(self, event: Mapping[str, Any]) -> None:
        new_epoch = int(event.get("epoch") or self.epoch + 1)
        if self.connected and new_epoch != self.epoch:
            for state in self.markets.values():
                exposed = (
                    state.placement_due_ms is not None
                    or state.reprice_due_ms is not None
                    or any(order.active for order in state.orders)
                )
                if exposed:
                    self._taint(state, "connection_epoch_change", float(event["recv_ms"]))
                elif state.rescue_order is not None and state.rescue_order.reason is None:
                    state.rescue_order.reason = "connection_epoch_change"
                    state.rescue_status = "connection_epoch_change"
        self.connected = True
        self.epoch = new_epoch
        for state in self.markets.values():
            state.up_book = TokenBook()
            state.down_book = TokenBook()

    def _disconnect(self, recv_ms: float) -> None:
        for state in self.markets.values():
            exposed = (
                state.placement_due_ms is not None
                or state.reprice_due_ms is not None
                or any(order.active for order in state.orders)
            )
            if exposed and state.taint_reason is None:
                self._taint(state, "disconnect", recv_ms)
            elif state.rescue_order is not None and state.rescue_order.reason is None:
                state.rescue_order.reason = "disconnect"
                state.rescue_status = "disconnect"
            state.up_book = TokenBook()
            state.down_book = TokenBook()
        self.connected = False

    def _event_market(self, event: Mapping[str, Any]) -> str | None:
        market_id = event.get("market_id")
        if market_id is not None:
            return str(market_id)
        asset_id = event.get("asset_id")
        hit = self.tokens.get(str(asset_id)) if asset_id is not None else None
        return hit[0] if hit is not None else None

    def _book(self, state: MarketState, asset_id: str) -> TokenBook | None:
        if asset_id == state.up_token_id:
            return state.up_book
        if asset_id == state.down_token_id:
            return state.down_book
        return None

    def _order(self, state: MarketState, asset_id: str) -> MakerOrder | None:
        if asset_id == state.up_token_id:
            return state.up_order
        if asset_id == state.down_token_id:
            return state.down_order
        return None

    def _snapshot(self, state: MarketState, event: Mapping[str, Any]) -> None:
        book = self._book(state, str(event.get("asset_id") or ""))
        if book is None:
            return
        try:
            book.replace(event.get("bids") or (), event.get("asks") or (), float(event["recv_ms"]))
        except (KeyError, TypeError, ValueError):
            book.ready = False

    def _price_change(self, state: MarketState, event: Mapping[str, Any]) -> None:
        recv_ms = float(event["recv_ms"])
        for change in event.get("changes") or ():
            asset_id = str(change.get("asset_id") or "")
            book = self._book(state, asset_id)
            if book is None:
                continue
            order = self._order(state, asset_id)
            side = str(change.get("side") or "").upper()
            try:
                changed_price = float(change.get("price"))
            except (TypeError, ValueError):
                changed_price = math.nan
            book.change(
                side,
                change.get("price"),
                change.get("size"),
                recv_ms,
            )
            if (
                self.config.queue_cancellation_credit
                and order is not None
                and order.active
                and side == "BUY"
                and math.isfinite(changed_price)
                and abs(changed_price - order.price) <= EPS
            ):
                order.queue_ahead = min(
                    order.queue_ahead,
                    max(0.0, float(book.bids.get(order.price, 0.0))),
                )

    def _try_quote(self, state: MarketState, recv_ms: float) -> None:
        if state.quote_taken or state.taint_reason is not None:
            return
        remaining_s = (state.end_ms - recv_ms) / 1_000.0
        if not self.config.quote_tau_lo_s <= remaining_s <= self.config.quote_tau_hi_s:
            return
        if not (
            state.up_book.valid_at(recv_ms, self.config.book_fresh_ms)
            and state.down_book.valid_at(recv_ms, self.config.book_fresh_ms)
        ):
            return
        up_bid = max(state.up_book.bids)
        down_bid = max(state.down_book.bids)
        if up_bid + down_bid > self.config.maximum_pair_cost + EPS:
            return
        state.quote_taken = True
        state.quote_decision_ms = recv_ms
        state.placement_due_ms = recv_ms + self.config.placement_latency_ms
        state.end_cancel_due_ms = state.end_ms - self.config.end_cancel_tau_s * 1_000.0
        state.up_order = MakerOrder("Up", state.up_token_id, up_bid, self.config.shares)
        state.down_order = MakerOrder("Down", state.down_token_id, down_bid, self.config.shares)

    def _activate(self, state: MarketState, due_ms: float) -> None:
        state.placement_due_ms = None
        if state.taint_reason is not None or not self.connected:
            state.placement_reason = "disconnected_or_tainted"
            return
        if not (
            state.up_book.valid_at(due_ms, self.config.book_fresh_ms)
            and state.down_book.valid_at(due_ms, self.config.book_fresh_ms)
        ):
            state.placement_reason = "book_stale_or_invalid"
            return
        assert state.up_order is not None and state.down_order is not None
        for order, book in (
            (state.up_order, state.up_book),
            (state.down_order, state.down_book),
        ):
            if order.price >= min(book.asks) - EPS:
                state.placement_reason = "post_only_cross"
                return
        for order, book in (
            (state.up_order, state.up_book),
            (state.down_order, state.down_book),
        ):
            order.queue_ahead = max(0.0, float(book.bids.get(order.price, 0.0)))
            order.active = True
            order.activation_ms = due_ms
        state.activation_ms = due_ms
        state.placement_reason = "active"

    def _trade(self, state: MarketState, event: Mapping[str, Any]) -> None:
        asset_id = str(event.get("asset_id") or "")
        book = self._book(state, asset_id)
        order = self._order(state, asset_id)
        if book is None or order is None or not order.active or order.remaining <= EPS:
            return
        try:
            price = float(event["price"])
            size = float(event["size"])
        except (KeyError, TypeError, ValueError):
            return
        if not (math.isfinite(price) and math.isfinite(size) and size > 0):
            return
        transaction_hash = str(event.get("transaction_hash") or "")
        if transaction_hash:
            identity = (asset_id, transaction_hash, price, size)
            if identity in self._seen_trade_events:
                return
            self._seen_trade_events.add(identity)
        if str(event.get("side") or "").upper() != "SELL":
            return
        if book.aggressor_side(price) != "SELL" or price > order.price + EPS:
            return
        if price < order.price - EPS:
            fillable = size
            order.queue_ahead = 0.0
        else:
            consumed = min(order.queue_ahead, size)
            order.queue_ahead -= consumed
            fillable = size - consumed
        filled = order.fill(fillable, self.config.fee_curve_rate)
        if filled > EPS:
            self._after_fill(state, float(event["recv_ms"]))

    def _after_fill(self, state: MarketState, recv_ms: float) -> None:
        assert state.up_order is not None and state.down_order is not None
        imbalance = state.up_order.filled - state.down_order.filled
        if abs(imbalance) <= EPS:
            state.rescue_cancel_due_ms = None
            state.reprice_due_ms = None
            return
        if (
            self.config.rescue_mode != "hold"
            and self.config.rescue_trigger == "first_fill"
            and abs(imbalance) + EPS >= self.config.rescue_min_shares
            and state.rescue_cancel_due_ms is None
            and state.rescue_order is None
        ):
            state.rescue_cancel_due_ms = recv_ms + self.config.cancel_latency_ms
            state.rescue_status = "cancel_pending"
            state.reprice_due_ms = None
            return
        up_full = state.up_order.remaining <= EPS
        down_full = state.down_order.remaining <= EPS
        if up_full and down_full:
            state.reprice_due_ms = None
            return
        if up_full != down_full and state.reprice_due_ms is None:
            other = state.down_order if up_full else state.up_order
            if other.active:
                state.reprice_due_ms = recv_ms + self.config.reprice_latency_ms

    def _rescue_cancel(self, state: MarketState, due_ms: float) -> None:
        state.rescue_cancel_due_ms = None
        if state.up_order is None or state.down_order is None:
            state.rescue_status = "orders_unavailable"
            return
        imbalance = state.up_order.filled - state.down_order.filled
        if abs(imbalance) + EPS < self.config.rescue_min_shares:
            state.rescue_status = "maker_hedge_completed_in_transit"
            return

        for order in state.orders:
            if order.active:
                order.active = False
                order.cancelled_ms = due_ms
        state.reprice_due_ms = None
        state.rescue_cancel_effective_ms = due_ms
        state.cancel_effective_ms = due_ms
        self._prepare_rescue(state, due_ms)

    def _prepare_rescue(self, state: MarketState, due_ms: float) -> None:
        if state.up_order is None or state.down_order is None:
            state.rescue_status = "orders_unavailable"
            return
        imbalance = state.up_order.filled - state.down_order.filled
        if abs(imbalance) + EPS < self.config.rescue_min_shares:
            state.rescue_status = "maker_hedge_completed"
            return

        if state.taint_reason is not None or not self.connected:
            state.rescue_status = "disconnected_or_tainted"
            return

        overfilled = state.up_order if imbalance > 0 else state.down_order
        target = abs(imbalance)
        if self.config.rescue_mode == "hedge":
            average = overfilled.average_fill_price
            if average is None:
                state.rescue_status = "maker_fill_price_unavailable"
                return
            from h_replay import limit_price

            fixed_limit = limit_price(
                1.0 - average,
                self.config.minimum_locked_edge,
                tick=self.config.tick_size,
                fee_rate=self.config.fee_curve_rate,
            )
            if fixed_limit is None:
                state.rescue_status = "no_profitable_hedge_limit"
                return
            asset_id = (
                state.down_token_id if overfilled.side == "Up" else state.up_token_id
            )
        elif self.config.rescue_mode == "unwind":
            asset_id = overfilled.asset_id
            book = self._book(state, asset_id)
            if book is None or not book.valid_at(due_ms, self.config.book_fresh_ms):
                state.rescue_status = "book_stale_or_invalid_at_cancel"
                return
            fixed_limit = max(book.bids)
        else:
            state.rescue_status = "hold"
            return

        state.rescue_order = RescueOrder(
            kind=self.config.rescue_mode,
            asset_id=asset_id,
            target_shares=target,
            limit_price=fixed_limit,
            decision_ms=due_ms,
            due_ms=due_ms + self.config.rescue_latency_ms,
        )
        state.rescue_status = "submitted"

    def _execute_rescue(self, state: MarketState, due_ms: float) -> None:
        rescue = state.rescue_order
        if rescue is None or rescue.reason is not None:
            return
        if state.taint_reason is not None or not self.connected:
            rescue.reason = "disconnected_or_tainted"
            return
        book = self._book(state, rescue.asset_id)
        if book is None or not book.valid_at(due_ms, self.config.book_fresh_ms):
            rescue.reason = "book_stale_or_invalid"
            return

        if rescue.kind == "hedge":
            from basis_wedge_run import _fill_direct

            reason, filled, vwap, fee, notional, levels = _fill_direct(
                book,
                rescue.limit_price,
                rescue.target_shares,
                self.config.fee_curve_rate,
            )
        else:
            from absorption_run import _sell_direct

            reason, filled, vwap, fee, notional, levels = _sell_direct(
                book,
                rescue.target_shares,
                self.config.fee_curve_rate,
                minimum_price=rescue.limit_price,
            )
        rescue.reason = reason
        rescue.filled = filled
        rescue.vwap = vwap
        rescue.fee = float(fee or 0.0)
        rescue.notional = float(notional or 0.0)
        rescue.levels = tuple(levels)
        state.rescue_status = reason

    def _reprice(self, state: MarketState, due_ms: float) -> None:
        state.reprice_due_ms = None
        if state.taint_reason is not None:
            return
        assert state.up_order is not None and state.down_order is not None
        up_full = state.up_order.remaining <= EPS
        down_full = state.down_order.remaining <= EPS
        if up_full == down_full:
            return
        filled = state.up_order if up_full else state.down_order
        other = state.down_order if up_full else state.up_order
        book = state.down_book if up_full else state.up_book
        average = filled.average_fill_price
        if average is None or not other.active or not book.valid_at(due_ms, self.config.book_fresh_ms):
            return
        cap = 1.0 - average - self.config.minimum_locked_edge
        post_only_cap = min(book.asks) - self.config.tick_size
        allowed = min(cap, post_only_cap)
        tick = self.config.tick_size
        allowed = math.floor((allowed + EPS) / tick) * tick
        allowed = round(allowed, 10)
        if allowed <= other.price + EPS:
            return
        other.price = allowed
        other.queue_ahead = max(0.0, float(book.bids.get(allowed, 0.0)))
        other.reprice_count += 1
        state.repriced_side = other.side

    def _external_jump(self, event: Mapping[str, Any]) -> None:
        delay = self.config.external_cancel_delay_ms
        slot = int(event.get("slot", -1))
        recv_ms = float(event["recv_ms"])
        for market_id in self.slots.get(slot, ()):
            state = self.markets[market_id]
            if not state.quote_taken or state.taint_reason is not None:
                continue
            if event.get("ambiguous_receipt") is True:
                self._taint(state, "equal_cross_transport_receipt", recv_ms)
                continue
            active = [order for order in state.orders if order.active]
            if not active and state.placement_due_ms is None:
                continue
            if state.warning_ms is None:
                state.warning_ms = recv_ms
                state.warning_source = str(event.get("source") or "unknown")
                state.warning_direction = int(event.get("direction") or 0)
            if delay is None:
                state.warning_action = "observe_only"
                continue
            due = recv_ms + delay
            if state.cancel_due_ms is None or due < state.cancel_due_ms:
                state.cancel_due_ms = due
                state.cancel_source = str(event.get("source") or "unknown")
                state.warning_action = (
                    "preserve_inventory_hedge"
                    if any(order.filled > EPS for order in state.orders)
                    else "cancel_flat_pair"
                )

    def _cancel(self, state: MarketState, due_ms: float, reason: str) -> None:
        if reason == "external_warning":
            state.cancel_due_ms = None
            if state.placement_due_ms is not None:
                state.placement_due_ms = None
                state.cancel_effective_ms = due_ms
                state.placement_reason = "warning_before_activation"
                return
            up_filled = state.up_order.filled if state.up_order is not None else 0.0
            down_filled = state.down_order.filled if state.down_order is not None else 0.0
            if up_filled > EPS or down_filled > EPS:
                assert state.up_order is not None and state.down_order is not None
                state.warning_action = "preserve_inventory_hedge"
                if abs(up_filled - down_filled) <= EPS:
                    retained: MakerOrder | None = None
                elif up_filled > down_filled:
                    retained = state.down_order
                    retained.target_shares = up_filled
                else:
                    retained = state.up_order
                    retained.target_shares = down_filled
                for order in state.orders:
                    if order is not retained and order.active:
                        order.active = False
                        order.cancelled_ms = due_ms
                if retained is not None and retained.remaining <= EPS:
                    retained.active = False
                state.cancel_effective_ms = due_ms
                if retained is None:
                    state.reprice_due_ms = None
                return
        active = [order for order in state.orders if order.active]
        if not active:
            if reason == "external_warning":
                state.cancel_due_ms = None
            else:
                state.end_cancel_due_ms = None
                if (
                    reason == "tau10"
                    and self.config.rescue_mode != "hold"
                    and self.config.rescue_trigger == "end_cancel"
                    and state.rescue_order is None
                ):
                    state.rescue_cancel_effective_ms = due_ms
                    self._prepare_rescue(state, due_ms)
            return
        for order in active:
            order.active = False
            order.cancelled_ms = due_ms
        state.cancel_effective_ms = due_ms
        state.placement_reason = reason if state.placement_reason is None else state.placement_reason
        if reason == "external_warning":
            state.cancel_due_ms = None
        else:
            state.end_cancel_due_ms = None
            if (
                reason == "tau10"
                and self.config.rescue_mode != "hold"
                and self.config.rescue_trigger == "end_cancel"
                and state.rescue_order is None
            ):
                state.rescue_cancel_effective_ms = due_ms
                self._prepare_rescue(state, due_ms)

    def _taint(self, state: MarketState, reason: str, recv_ms: float) -> None:
        if state.taint_reason is None:
            state.taint_reason = reason
            state.taint_ms = recv_ms
            state.unknown_live_order_shares = sum(
                order.remaining for order in state.orders if order.active
            )
        for order in state.orders:
            order.active = False
        state.cancel_due_ms = None
        state.reprice_due_ms = None
        state.rescue_cancel_due_ms = None
        if state.rescue_order is not None and state.rescue_order.reason is None:
            state.rescue_order.reason = reason
            state.rescue_status = reason

    def _advance(self, stamp: float, *, inclusive: bool) -> None:
        while True:
            due_actions: list[tuple[float, int, str, MarketState]] = []
            for state in self.markets.values():
                candidates = (
                    (state.placement_due_ms, 0, "placement"),
                    (state.rescue_cancel_due_ms, 1, "rescue_cancel"),
                    (state.reprice_due_ms, 2, "reprice"),
                    (state.cancel_due_ms, 3, "external_warning"),
                    (
                        state.rescue_order.due_ms
                        if state.rescue_order is not None
                        and state.rescue_order.reason is None
                        else None,
                        4,
                        "rescue_execute",
                    ),
                    (
                        None if state.end_cancel_due_ms is None
                        else state.end_cancel_due_ms + self.config.cancel_latency_ms,
                        5,
                        "tau10",
                    ),
                )
                for due, priority, action in candidates:
                    if due is None:
                        continue
                    is_due = due <= stamp + EPS if inclusive else due < stamp - EPS
                    if is_due:
                        due_actions.append((due, priority, action, state))
            if not due_actions:
                return
            due, _priority, action, state = min(
                due_actions,
                key=lambda item: (item[0], item[1], item[3].market_id),
            )
            if action == "placement":
                self._activate(state, due)
            elif action == "rescue_cancel":
                self._rescue_cancel(state, due)
            elif action == "reprice":
                self._reprice(state, due)
            elif action == "rescue_execute":
                self._execute_rescue(state, due)
            else:
                self._cancel(state, due, action)


def external_jump_events(
    candidates: Iterable[Any],
    *,
    allowed_sources: frozenset[str] | set[str] = frozenset({
        "spot_trade", "futures_book_ticker", "futures_trade", "deribit_quote",
    }),
) -> Iterator[dict[str, Any]]:
    """Translate the frozen receipt-race candidates without changing their clocks."""
    rows = sorted(candidates, key=lambda row: (int(row.recv_ns), str(row.source)))
    for row in rows:
        source = str(row.source)
        if source not in allowed_sources:
            continue
        yield {
            "kind": "external_jump",
            "recv_ms": int(row.recv_ns) / 1_000_000.0,
            "recv_ns": int(row.recv_ns),
            "source_ts_ms": int(row.source_ns) / 1_000_000.0,
            "source_ts_ns": int(row.source_ns),
            "slot": int(row.slot),
            "direction": int(row.direction),
            "source": source,
            "z_score": float(row.z_score),
        }


def merge_replay_events(
    clob_events: Iterable[Mapping[str, Any]],
    warning_events: Iterable[Mapping[str, Any]],
) -> Iterator[dict[str, Any]]:
    """Merge transports by local receipt clock and fail closed on exact cross-feed ties."""
    clob = (
        ((_receipt_ns(event), 0, sequence), "clob", dict(event))
        for sequence, event in enumerate(clob_events)
    )
    warnings = (
        ((_receipt_ns(event), 1, sequence), "warning", dict(event))
        for sequence, event in enumerate(warning_events)
    )
    merged = heapq.merge(clob, warnings, key=lambda item: item[0])
    pending: list[tuple[str, dict[str, Any]]] = []
    pending_stamp: int | None = None
    for key, family, event in merged:
        stamp = key[0]
        if pending_stamp is not None and stamp != pending_stamp:
            yield from _release_receipt_group(pending)
            pending = []
        pending_stamp = stamp
        pending.append((family, event))
    if pending:
        yield from _release_receipt_group(pending)


def _release_receipt_group(
    group: Sequence[tuple[str, dict[str, Any]]],
) -> Iterator[dict[str, Any]]:
    families = {family for family, _event in group}
    cross_transport_tie = len(families) > 1
    for family, event in group:
        if cross_transport_tie and family == "warning":
            event["ambiguous_receipt"] = True
        yield event


def replay_variants(
    clob_events: Iterable[Mapping[str, Any]],
    warning_events: Iterable[Mapping[str, Any]],
    outcomes: Mapping[str, str],
    configs: Mapping[str, PairedMakerConfig],
) -> dict[str, list[dict[str, Any]]]:
    """Run matched execution arms in one bounded-memory pass over the raw CLOB stream."""
    machines = {name: PairedMakerReplay(config) for name, config in configs.items()}
    for event in merge_replay_events(clob_events, warning_events):
        for machine in machines.values():
            machine.on_event(event)
    return {name: machine.finish(outcomes) for name, machine in machines.items()}


def summarize(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    eligible = [row for row in rows if row.get("eligible")]
    resolved = [row for row in eligible if row.get("pnl") is not None]
    completed = [row for row in resolved if row.get("completed_pair")]
    one_leg = [
        row for row in resolved
        if (float(row.get("up_filled") or 0) > EPS)
        != (float(row.get("down_filled") or 0) > EPS)
    ]
    total_pnl = sum(float(row["pnl"]) for row in resolved)
    total_filled = sum(
        float(row.get("gross_filled_shares") or 0)
        for row in resolved
    )
    rebate_sensitivity = sum(float(row.get("maker_rebate_sensitivity") or 0) for row in resolved)
    return {
        "quote_cycles": len(rows),
        "eligible_cycles": len(eligible),
        "resolved_cycles": len(resolved),
        "completed_pairs": len(completed),
        "one_leg_cycles": len(one_leg),
        "completed_pair_rate": len(completed) / len(resolved) if resolved else 0.0,
        "one_leg_rate": len(one_leg) / len(resolved) if resolved else 0.0,
        "rescue_cycles": sum(row.get("rescue_kind") is not None for row in resolved),
        "rescue_fill_cycles": sum(float(row.get("rescue_filled") or 0) > EPS for row in resolved),
        "rescue_filled_shares": sum(float(row.get("rescue_filled") or 0) for row in resolved),
        "rescue_fees": sum(float(row.get("rescue_fee") or 0) for row in resolved),
        "pnl": total_pnl,
        "maker_rebate_sensitivity": rebate_sensitivity,
        "pnl_with_rebate_sensitivity": total_pnl + rebate_sensitivity,
        "unknown_exposure_cycles": sum(
            float(row.get("unknown_live_order_shares") or 0) > EPS for row in rows
        ),
        "pnl_per_filled_share": total_pnl / total_filled if total_filled else None,
    }


def _gamma_outcomes(start_ms: float, end_ms: float) -> dict[str, str]:
    """Fetch resolved official outcomes for a bounded development interval."""
    import pm_outcomes

    start_s = math.ceil(start_ms / 1_000.0 / 300.0) * 300
    end_s = math.ceil(end_ms / 1_000.0 / 300.0) * 300
    slots = list(range(start_s, end_s, 300))
    rows: list[dict[str, Any]] = []
    for offset in range(0, len(slots), 100):
        rows.extend(pm_outcomes.fetch_batch([
            pm_outcomes.slug("5m", slot) for slot in slots[offset:offset + 100]
        ]))
    result: dict[str, str] = {}
    for row in rows:
        up = float(row["up"])
        market_id = row.get("condition_id")
        if market_id and math.isfinite(up):
            result[str(market_id)] = "Up" if up > 0.5 else "Down"
    return result


def development_configs(
    variant_names: Sequence[str] | None = None,
) -> dict[str, PairedMakerConfig]:
    """Return frozen development arms, optionally restricted in caller-specified order."""
    base = PairedMakerConfig()
    configs = {
        "place30_cancel30": base,
        "place30_cancel60": PairedMakerConfig(
            cancel_latency_ms=60.0,
            external_cancel_delay_ms=60.0,
        ),
        "place30_cancel30_queue_credit": PairedMakerConfig(
            queue_cancellation_credit=True,
        ),
        "place60_cancel30": PairedMakerConfig(placement_latency_ms=60.0),
        "place60_cancel60": PairedMakerConfig(
            placement_latency_ms=60.0,
            cancel_latency_ms=60.0,
            external_cancel_delay_ms=60.0,
        ),
        "place30_no_cancel": PairedMakerConfig(external_cancel_delay_ms=None),
        "place30_late5s": PairedMakerConfig(external_cancel_delay_ms=5_030.0),
        "place30_rescue_hedge300": PairedMakerConfig(
            rescue_mode="hedge",
            rescue_latency_ms=300.0,
        ),
        "place30_rescue_hedge400": PairedMakerConfig(
            rescue_mode="hedge",
            rescue_latency_ms=400.0,
        ),
        "place30_rescue_unwind300": PairedMakerConfig(
            rescue_mode="unwind",
            rescue_latency_ms=300.0,
        ),
        "place30_rescue_unwind400": PairedMakerConfig(
            rescue_mode="unwind",
            rescue_latency_ms=400.0,
        ),
        "place30_deadline_hedge300": PairedMakerConfig(
            rescue_mode="hedge",
            rescue_trigger="end_cancel",
            rescue_latency_ms=300.0,
        ),
        "place30_deadline_hedge400": PairedMakerConfig(
            rescue_mode="hedge",
            rescue_trigger="end_cancel",
            rescue_latency_ms=400.0,
        ),
        "place30_deadline_unwind300": PairedMakerConfig(
            rescue_mode="unwind",
            rescue_trigger="end_cancel",
            rescue_latency_ms=300.0,
        ),
        "place30_deadline_unwind400": PairedMakerConfig(
            rescue_mode="unwind",
            rescue_trigger="end_cancel",
            rescue_latency_ms=400.0,
        ),
    }
    if variant_names is None:
        return configs
    unknown = [name for name in variant_names if name not in configs]
    if unknown:
        raise ValueError(f"unknown development variants: {', '.join(unknown)}")
    return {name: configs[name] for name in variant_names}


def run_raw_development(
    *,
    clob_paths: Sequence[str | Path],
    source_paths: Sequence[str | Path],
    segment_start_ms: float,
    segment_end_ms: float,
    fetch_outcomes: bool = False,
    variant_names: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Run frozen Family-24/25/26 development arms over raw eu-west recordings."""
    import absorption
    import jump_causal
    import receipt_race
    from h_replay_run import SignalConfig

    health: Counter[str] = Counter()
    candidates = receipt_race.extract_candidates(
        jump_causal.iter_raw_events_from_files(
            source_paths,
            recv_start_ns=int(segment_start_ms * 1_000_000),
            recv_end_ns=int(segment_end_ms * 1_000_000),
        ),
        config=SignalConfig(
            book_lag_ms=0.0,
            source_z0={"deribit_quote": 3.0},
        ),
        episode_ms=1_000,
        # Raw trade files do not expose lifecycle as MarketEvents.  Five seconds matches the
        # deployed reference-age fail-close without treating an ordinary quiet second as a socket
        # disconnect; formal artifacts use explicit connection epochs instead.
        max_gap_ms=5_000,
        max_source_age_ms=250,
        future_clock_tolerance_ms=250,
        health=health,
    )
    warnings = list(external_jump_events(candidates))
    configs = development_configs(variant_names)
    outcomes = _gamma_outcomes(segment_start_ms, segment_end_ms) if fetch_outcomes else {}
    clob = absorption.iter_raw_clob_events(
        [Path(path) for path in clob_paths],
        segment_end_ms,
        preserve_input_order=False,
        allow_leading_carryover=False,
    )
    rows = replay_variants(clob, warnings, outcomes, configs)
    market_keys = {
        name: {(str(row["market_id"]), int(row["slot"])) for row in sample}
        for name, sample in rows.items()
    }
    reference_name = next(iter(market_keys))
    for name, keys in market_keys.items():
        if keys != market_keys[reference_name]:
            raise RuntimeError(
                f"execution arms are not matched: {reference_name}={len(market_keys[reference_name])}, "
                f"{name}={len(keys)}"
            )
    source_counts = Counter(row["source"] for row in warnings)
    return {
        "schema": "paired-maker-development-v1",
        "segment_start_ms": segment_start_ms,
        "segment_end_ms": segment_end_ms,
        "candidate_count": len(candidates),
        "warning_count": len(warnings),
        "warning_sources": dict(sorted(source_counts.items())),
        "source_health": dict(sorted(health.items())),
        "official_outcomes": len(outcomes),
        "variants": {
            name: {
                "config": {
                    "placement_latency_ms": configs[name].placement_latency_ms,
                    "external_cancel_delay_ms": configs[name].external_cancel_delay_ms,
                    "rescue_mode": configs[name].rescue_mode,
                    "rescue_trigger": configs[name].rescue_trigger,
                    "rescue_latency_ms": configs[name].rescue_latency_ms,
                },
                "summary": summarize(sample),
                "rows": sample,
            }
            for name, sample in rows.items()
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clob", action="append", required=True)
    parser.add_argument("--source", action="append", required=True)
    parser.add_argument("--segment-start-ms", type=float, required=True)
    parser.add_argument("--segment-end-ms", type=float, required=True)
    parser.add_argument("--fetch-outcomes", action="store_true")
    parser.add_argument("--variant", action="append")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result = run_raw_development(
        clob_paths=args.clob,
        source_paths=args.source,
        segment_start_ms=args.segment_start_ms,
        segment_end_ms=args.segment_end_ms,
        fetch_outcomes=args.fetch_outcomes,
        variant_names=args.variant,
    )
    encoded = json.dumps(result, indent=2, sort_keys=True, allow_nan=False)
    if args.output is not None:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    else:
        print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
