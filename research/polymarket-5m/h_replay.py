"""Deterministic, paper-only replay of the deployed current-H execution path."""
from __future__ import annotations

import math
import json
import heapq
from bisect import bisect_right
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from pathlib import Path
from typing import Any, Iterable, Optional


FREEZE_PATH = Path(__file__).with_name("forward") / "current-h-freeze.json"


def taker_fee(price: float, rate: float = 0.07) -> float:
    return rate * price * (1.0 - price)


def limit_price(
    fair: float,
    theta: float,
    tick: float = 0.01,
    fee_rate: float = 0.07,
    lo: float = 0.02,
    hi: float = 0.98,
) -> Optional[float]:
    """Match the deployed ``gsig.limit_price`` fee equation and tick flooring."""
    c = fair - theta
    if not math.isfinite(c) or c <= 0:
        return None
    if fee_rate > 0:
        discriminant = (1 + fee_rate) ** 2 - 4 * fee_rate * c
        root = ((1 + fee_rate) - math.sqrt(max(discriminant, 0.0))) / (2 * fee_rate)
    else:
        root = c
    price = math.floor(root / tick + 1e-9) * tick
    while price > 0 and fair - price - taker_fee(price, fee_rate) < theta - 1e-12:
        price -= tick
    price = round(min(price, hi), 6)
    return price if price >= lo - 1e-12 else None


@dataclass(frozen=True)
class ReplayConfig:
    book_lag_ms: float = 106.0
    local_path_ms: float = 0.86
    order_wire_ms: float = 0.0
    venue_hold_ms: float = 150.0
    evaluation_ms: tuple[float, ...] = (300.0, 350.0, 400.0, 500.0)
    theta: float = 0.12
    tick: float = 0.01
    fee_rate: float = 0.07
    base_shares: float = 5.0
    reentry_min_ms: float = 2_000.0
    reverse_multiplier: float = 2.0
    pilot_min_usd: float = 1.0
    max_order_usd: float = 3.9
    signal_time_basis: str = "synthetic_receive_minus_lag"
    retry_after_no_send_or_kill: bool = False
    reentry_enabled: bool = True
    max_orders_per_min: int = 0


def load_freeze(path: str | Path = FREEZE_PATH) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def config_from_freeze(frozen: dict[str, Any]) -> ReplayConfig:
    timing = frozen["timing"]
    execution = frozen["execution"]
    return ReplayConfig(
        book_lag_ms=float(timing["book_trigger_synthetic_lag_ms"]),
        local_path_ms=float(timing["decision_sign_send_warm_ms"]),
        order_wire_ms=float(timing.get("order_wire_ms", 0.0)),
        venue_hold_ms=float(timing["venue_hold_ms"]),
        evaluation_ms=tuple(float(value) for value in timing["evaluation_ms"]),
        theta=float(execution["theta"]),
        tick=float(execution["tick"]),
        fee_rate=float(execution["fee_rate"]),
        base_shares=float(execution["base_shares"]),
        reentry_min_ms=float(execution["reentry_min_ms"]),
        reverse_multiplier=float(execution["opposite_signal_multiplier"]),
        pilot_min_usd=float(execution["pilot_min_usd"]),
        max_order_usd=float(execution["max_order_usd"]),
        signal_time_basis=str(timing.get("signal_time_basis", "synthetic_receive_minus_lag")),
        retry_after_no_send_or_kill=bool(frozen["semantics"].get("retry_after_no_send_or_kill", False)),
        reentry_enabled=bool(execution.get("reentry", True)),
        max_orders_per_min=int(execution.get("max_orders_per_min", 0)),
    )


@dataclass
class _Book:
    ready: bool = False
    up_asks: tuple[tuple[float, float], ...] = ()
    down_asks: tuple[tuple[float, float], ...] = ()
    receive_ms: Optional[float] = None
    source_ms: Optional[float] = None
    epoch: int = 0

    def asks(self, direction: str) -> tuple[tuple[float, float], ...]:
        return self.up_asks if direction == "Up" else self.down_asks


@dataclass
class _Branch:
    first_locked: bool = False
    pending: bool = False
    first_fill_direction: Optional[str] = None
    last_fill_signal_ms: Optional[float] = None


@dataclass(frozen=True)
class _Attempt:
    market_id: str
    signal_receive_ms: float
    signal_source_ms: Optional[float]
    signal_source: Optional[str]
    signal_ms: float
    direction: str
    fair: float
    decision_ask: Optional[float]
    fixed_limit: Optional[float]
    sent: bool
    decision_reason: str
    book_epoch: int
    first_fill_direction: Optional[str]
    size_multiplier: float
    is_reverse: bool
    send_ms: float
    earliest_match_ms: float
    trial_edge: Optional[float]
    trigger_reason: Optional[str]


@dataclass
class _Market:
    book: _Book = field(default_factory=_Book)
    history: "_BookHistory" = field(default_factory=lambda: _BookHistory())
    branches: dict[float, _Branch] = field(default_factory=dict)
    book_epoch: int = 0


@dataclass
class _BookHistory:
    """Short causal history, indexed by the venue's source clock."""

    keep_ms: float = 6_000.0
    sources: list[float] = field(default_factory=list)
    books: list[_Book] = field(default_factory=list)

    def push(self, book: _Book) -> None:
        source_ms = float(book.source_ms if book.source_ms is not None else book.receive_ms)
        if self.sources and source_ms < self.sources[-1]:
            source_ms = self.sources[-1]
        stored = _Book(
            ready=book.ready,
            up_asks=book.up_asks,
            down_asks=book.down_asks,
            receive_ms=book.receive_ms,
            source_ms=source_ms,
            epoch=book.epoch,
        )
        self.sources.append(source_ms)
        self.books.append(stored)
        cutoff = source_ms - self.keep_ms
        index = bisect_right(self.sources, cutoff) - 1
        if index > 0:
            del self.sources[:index]
            del self.books[:index]

    def state_at(self, source_ms: float) -> Optional[_Book]:
        index = bisect_right(self.sources, source_ms) - 1
        return self.books[index] if index >= 0 else None


def _levels(raw: Any) -> tuple[tuple[float, float], ...]:
    levels = ((float(price), float(size)) for price, size in (raw or ()))
    return tuple(sorted(((price, size) for price, size in levels if size > 0), key=lambda level: level[0]))


def _direct_depth(
    levels: tuple[tuple[float, float], ...],
    fixed_limit: Optional[float],
    target_shares: float,
) -> tuple[float, Optional[float]]:
    if fixed_limit is None:
        return 0.0, None
    depth = 0.0
    match_price = None
    for price, size in levels:
        if price > fixed_limit + 1e-9:
            break
        depth += size
        if match_price is None and depth >= target_shares - 1e-9:
            match_price = price
    return depth, match_price


def _fak_fill(
    levels: tuple[tuple[float, float], ...],
    fixed_limit: float,
    maker_usd: float,
    fee_rate: float,
) -> tuple[float, Optional[float], float]:
    remaining_usd = maker_usd
    filled_shares = 0.0
    spent_usd = 0.0
    fee = 0.0
    for price, available in levels:
        if price > fixed_limit + 1e-9 or remaining_usd <= 1e-12:
            break
        budget_shares = remaining_usd / price
        shares = min(available, math.ceil(budget_shares * 1_000_000.0 - 1e-9) / 1_000_000.0)
        spent = shares * price
        filled_shares += shares
        spent_usd += spent
        fee += shares * taker_fee(price, fee_rate)
        remaining_usd -= spent
    fill_price = spent_usd / filled_shares if filled_shares > 0 else None
    return filled_shares, fill_price, fee


def _pilot_plan(limit: float, target_shares: float, config: ReplayConfig) -> tuple[Optional[float], Optional[float]]:
    price = Decimal(str(limit))
    required = (Decimal(str(target_shares)) * price).quantize(Decimal("0.01"), rounding=ROUND_UP)
    cap = max(required, Decimal(str(config.pilot_min_usd)) + price) + Decimal("0.001")
    if cap > Decimal(str(config.max_order_usd)) + Decimal("0.011"):
        return None, None
    raw_size = (cap / price).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    price_units = int(price * 100)
    step = 100 // math.gcd(price_units, 100)
    size_units = int(raw_size * 100)
    size = Decimal(size_units - size_units % step) / 100
    maker = size * price
    return float(size), float(maker)


class HReplay:
    def __init__(self, config: ReplayConfig | None = None) -> None:
        self.config = config or ReplayConfig()
        self._markets: dict[str, _Market] = {}
        self._pending: list[tuple[float, int, float, _Attempt]] = []
        self._sequence = 0
        self._source_watermark = -math.inf
        self._sent_by_evaluation: dict[float, list[float]] = {
            float(value): [] for value in self.config.evaluation_ms
        }
        self.records: list[dict[str, Any]] = []

    def _market(self, market_id: str) -> _Market:
        market = self._markets.setdefault(market_id, _Market())
        for evaluation_ms in self.config.evaluation_ms:
            market.branches.setdefault(float(evaluation_ms), _Branch())
        return market

    def _drain(self, receive_ms: float, inclusive: bool, force: bool = False) -> None:
        while self._pending:
            due_ms, sequence, evaluation_ms, attempt = self._pending[0]
            receive_ready = due_ms < receive_ms or (inclusive and due_ms == receive_ms)
            source_ready = self._source_watermark > due_ms
            if not force and not (receive_ready and source_ready):
                break
            heapq.heappop(self._pending)
            self.records.append(self._evaluate(attempt, evaluation_ms))

    def feed(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        receive_ms = float(event["receive_ms"])
        before = len(self.records)
        self._drain(receive_ms, inclusive=False)
        kind = str(event["kind"])
        market_id = event.get("market_id")
        if kind == "watermark":
            self._source_watermark = max(self._source_watermark, float(event["source_ms"]))
            self._drain(receive_ms, inclusive=True)
            return self.records[before:]
        if kind in ("disconnect", "reconnect") and market_id is None:
            for known in self._markets.values():
                if kind == "disconnect":
                    known.book_epoch += 1
                source_ms = float(event.get("source_ms") or receive_ms)
                known.book = _Book(receive_ms=receive_ms, source_ms=source_ms, epoch=known.book_epoch)
                known.history.push(known.book)
                if event.get("advance_watermark", True):
                    self._source_watermark = max(self._source_watermark, source_ms)
            self._drain(receive_ms, inclusive=True)
            return self.records[before:]
        market = self._market(str(market_id))
        if kind == "snapshot":
            if "up_asks" in event and "down_asks" in event:
                source_ms = float(event.get("source_ms") or receive_ms)
                if event.get("levels_normalized"):
                    up_asks = event["up_asks"]
                    down_asks = event["down_asks"]
                    if not isinstance(up_asks, tuple) or not isinstance(down_asks, tuple):
                        raise ValueError("normalized replay levels must be immutable tuples")
                else:
                    up_asks = _levels(event["up_asks"])
                    down_asks = _levels(event["down_asks"])
                market.book = _Book(
                    ready=True,
                    up_asks=up_asks,
                    down_asks=down_asks,
                    receive_ms=receive_ms,
                    source_ms=source_ms,
                    epoch=market.book_epoch,
                )
                market.history.push(market.book)
                if event.get("advance_watermark", True):
                    self._source_watermark = max(self._source_watermark, source_ms)
        elif kind == "update":
            if market.book.ready:
                direction = "Up" if event["direction"] in ("Up", "up", 1, True) else "Down"
                source_ms = float(event.get("source_ms") or receive_ms)
                market.book = _Book(
                    ready=True,
                    up_asks=_levels(event.get("asks")) if direction == "Up" else market.book.up_asks,
                    down_asks=_levels(event.get("asks")) if direction == "Down" else market.book.down_asks,
                    receive_ms=receive_ms,
                    source_ms=source_ms,
                    epoch=market.book_epoch,
                )
                market.history.push(market.book)
                if event.get("advance_watermark", True):
                    self._source_watermark = max(self._source_watermark, source_ms)
        elif kind == "disconnect":
            market.book_epoch += 1
            source_ms = float(event.get("source_ms") or receive_ms)
            market.book = _Book(receive_ms=receive_ms, source_ms=source_ms, epoch=market.book_epoch)
            market.history.push(market.book)
            if event.get("advance_watermark", True):
                self._source_watermark = max(self._source_watermark, source_ms)
        elif kind == "reconnect":
            source_ms = float(event.get("source_ms") or receive_ms)
            market.book = _Book(receive_ms=receive_ms, source_ms=source_ms, epoch=market.book_epoch)
            market.history.push(market.book)
            if event.get("advance_watermark", True):
                self._source_watermark = max(self._source_watermark, source_ms)
        elif kind == "invalidate":
            source_ms = float(event.get("source_ms") or receive_ms)
            market.book = _Book(receive_ms=receive_ms, source_ms=source_ms, epoch=market.book_epoch)
            market.history.push(market.book)
            if event.get("advance_watermark", True):
                self._source_watermark = max(self._source_watermark, source_ms)
        elif kind == "signal":
            self._signal(market, str(event["market_id"]), event)
        self._drain(receive_ms, inclusive=True)
        return self.records[before:]

    def _signal(self, market: _Market, market_id: str, event: dict[str, Any]) -> None:
        receive_ms = float(event["receive_ms"])
        direction = "Up" if event["direction"] in ("Up", "up", 1, True) else "Down"
        fair = float(event["fair"])
        fixed_limit = limit_price(
            fair,
            self.config.theta,
            self.config.tick,
            self.config.fee_rate,
        )
        asks = market.book.asks(direction) if market.book.ready else ()
        decision_ask = asks[0][0] if asks else None
        sent = bool(fixed_limit is not None and decision_ask is not None and decision_ask <= fixed_limit + 1e-9)
        if not market.book.ready or decision_ask is None:
            decision_reason = "book_unavailable_at_decision"
        elif fixed_limit is None:
            decision_reason = "no_limit"
        elif decision_ask > fixed_limit + 1e-9:
            decision_reason = "ask_above_limit"
        else:
            decision_reason = "sent"
        raw_source_ms = event.get("source_ms")
        if self.config.signal_time_basis == "exchange_source_timestamp" and raw_source_ms is not None:
            signal_ms = float(raw_source_ms)
        else:
            signal_ms = receive_ms - self.config.book_lag_ms
        send_ms = receive_ms + self.config.local_path_ms
        for evaluation_ms, branch in market.branches.items():
            if branch.pending:
                continue
            if branch.first_fill_direction is None:
                if branch.first_locked and not self.config.retry_after_no_send_or_kill:
                    continue
                if not self.config.retry_after_no_send_or_kill:
                    branch.first_locked = True
                size_multiplier = 1.0
                is_reverse = False
            else:
                if not self.config.reentry_enabled:
                    continue
                if (branch.last_fill_signal_ms is not None
                        and signal_ms - branch.last_fill_signal_ms < self.config.reentry_min_ms - 1e-9):
                    continue
                is_reverse = direction != branch.first_fill_direction
                size_multiplier = self.config.reverse_multiplier if is_reverse else 1.0
            target_shares = self.config.base_shares * size_multiplier
            signed_size, _ = (_pilot_plan(fixed_limit, target_shares, self.config)
                              if fixed_limit is not None else (None, None))
            decision_gate = decision_reason
            branch_sent = sent
            if branch_sent and signed_size is None:
                branch_sent = False
                decision_gate = "order_size_cap"
            sent_times = self._sent_by_evaluation[evaluation_ms]
            cutoff = send_ms - 60_000.0
            while sent_times and sent_times[0] <= cutoff:
                sent_times.pop(0)
            if (branch_sent and self.config.max_orders_per_min > 0 and
                    len(sent_times) >= self.config.max_orders_per_min):
                branch_sent = False
                decision_gate = "rate"
            attempt = _Attempt(
                market_id=market_id,
                signal_receive_ms=receive_ms,
                signal_source_ms=event.get("source_ms"),
                signal_source=event.get("signal_source"),
                signal_ms=signal_ms,
                direction=direction,
                fair=fair,
                decision_ask=decision_ask,
                fixed_limit=fixed_limit,
                sent=branch_sent,
                decision_reason=decision_gate,
                book_epoch=market.book_epoch,
                first_fill_direction=branch.first_fill_direction,
                size_multiplier=size_multiplier,
                is_reverse=is_reverse,
                send_ms=send_ms,
                earliest_match_ms=send_ms + self.config.order_wire_ms + self.config.venue_hold_ms,
                trial_edge=event.get("trial_edge"),
                trigger_reason=event.get("trigger_reason"),
            )
            if branch_sent:
                branch.pending = True
                sent_times.append(send_ms)
            self._sequence += 1
            heapq.heappush(
                self._pending,
                (signal_ms + evaluation_ms, self._sequence, evaluation_ms, attempt),
            )

    def _evaluate(self, attempt: _Attempt, evaluation_ms: float) -> dict[str, Any]:
        market = self._market(attempt.market_id)
        branch = market.branches[evaluation_ms]
        evaluation_time_ms = attempt.signal_ms + evaluation_ms
        book = market.history.state_at(evaluation_time_ms)
        target_shares = self.config.base_shares * attempt.size_multiplier
        signed_size, maker_usd = (_pilot_plan(attempt.fixed_limit, target_shares, self.config)
                                  if attempt.fixed_limit is not None else (None, None))
        eligible_depth, match_price = _direct_depth(
            book.asks(attempt.direction) if book is not None and book.ready else (),
            attempt.fixed_limit,
            target_shares,
        )
        can_match = True
        if not attempt.sent:
            reason = attempt.decision_reason
            can_match = False
        elif book is not None and attempt.book_epoch != book.epoch:
            reason = "disconnect"
            can_match = False
        elif signed_size is None:
            reason = "order_size_cap"
            can_match = False
        elif evaluation_time_ms < attempt.earliest_match_ms - 1e-9:
            reason = "venue_hold"
            can_match = False
        elif book is None or not book.ready:
            reason = "book_unavailable"
            can_match = False
        else:
            reason = "matchable"
        if can_match and maker_usd is not None and attempt.fixed_limit is not None:
            filled_shares, fill_price, fill_fee = _fak_fill(
                book.asks(attempt.direction),
                attempt.fixed_limit,
                maker_usd,
                self.config.fee_rate,
            )
        else:
            filled_shares, fill_price, fill_fee = 0.0, None, 0.0
        if reason == "matchable":
            if filled_shares >= target_shares - 1e-9:
                reason = "filled"
            elif filled_shares > 0:
                reason = "partial_fill"
            else:
                reason = "insufficient_effective_depth"
        filled = filled_shares > 0
        record = {
            "market_id": attempt.market_id,
            "ordering_clock": "receive_ms",
            "match_book_clock": "source_ms",
            "future_health_checked": False,
            "signal_receive_ms": attempt.signal_receive_ms,
            "signal_source_ms": attempt.signal_source_ms,
            "signal_source": attempt.signal_source,
            "signal_ms": attempt.signal_ms,
            "direction": attempt.direction,
            "direct_token": attempt.direction,
            "fair": attempt.fair,
            "decision_ask": attempt.decision_ask,
            "fixed_limit": attempt.fixed_limit,
            "trial_edge": attempt.trial_edge,
            "trigger_reason": attempt.trigger_reason,
            "sent": attempt.sent,
            "decision_reason": attempt.decision_reason,
            "first_fill_direction": attempt.first_fill_direction,
            "size_multiplier": attempt.size_multiplier,
            "is_reverse": attempt.is_reverse,
            "send_ms": attempt.send_ms,
            "signal_to_send_ms": attempt.send_ms - attempt.signal_ms,
            "earliest_match_ms": attempt.earliest_match_ms,
            "evaluation_ms": evaluation_ms,
            "evaluation_time_ms": evaluation_time_ms,
            "target_shares": target_shares,
            "signed_size": signed_size,
            "maker_usd": maker_usd,
            "eligible_depth": eligible_depth,
            "match_price": match_price,
            "filled_shares": filled_shares,
            "fill_price": fill_price,
            "fill_fee": fill_fee,
            "fill_fee_per_share": fill_fee / filled_shares if filled_shares else None,
            "all_in_cost": (fill_price + fill_fee / filled_shares
                            if filled_shares and fill_price is not None else None),
            "book_receive_ms": book.receive_ms if book is not None else None,
            "book_source_ms": book.source_ms if book is not None else None,
            "filled": filled,
            "reason": reason,
        }
        if attempt.sent:
            branch.pending = False
        if filled:
            if branch.first_fill_direction is None:
                branch.first_fill_direction = attempt.direction
            branch.last_fill_signal_ms = attempt.signal_ms
        return record

    def finish(self, *, force: bool = True) -> list[dict[str, Any]]:
        """Drain evaluations.

        ``force=False`` is for strict finite recordings: requests whose target
        time was never crossed by the observed watermark remain censored rather
        than being evaluated against a stale terminal book.
        """
        before = len(self.records)
        self._drain(math.inf, inclusive=True, force=force)
        return self.records[before:]

    def pending_evaluations(self) -> list[dict[str, Any]]:
        """Return immutable metadata for end-of-recording censored requests."""
        return [
            {
                "market_id": attempt.market_id,
                "direction": attempt.direction,
                "evaluation_ms": evaluation_ms,
                "evaluation_time_ms": attempt.signal_ms + evaluation_ms,
                "signal_receive_ms": attempt.signal_receive_ms,
                "send_ms": attempt.send_ms,
                "sent": attempt.sent,
                "decision_reason": attempt.decision_reason,
                "fixed_limit": attempt.fixed_limit,
                "target_shares": self.config.base_shares * attempt.size_multiplier,
            }
            for _, _, evaluation_ms, attempt in sorted(self._pending)
        ]

    def has_pending_evaluations(self) -> bool:
        """Cheap hot-path check used to avoid materializing books while no order is in flight."""
        return bool(self._pending)


def replay_events(events: Iterable[dict[str, Any]], config: ReplayConfig | None = None) -> list[dict[str, Any]]:
    machine = HReplay(config)
    ordered = sorted(enumerate(events), key=lambda item: (float(item[1]["receive_ms"]), item[0]))
    for _, event in ordered:
        machine.feed(event)
    machine.finish()
    return machine.records
