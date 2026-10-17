"""Receipt-time-causal candidate detector for the Binance-perpetual basis wedge.

The state machine is research-only: it consumes already-normalized market-data
events and emits immutable candidates.  It never places or models an order.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal

from h_replay import taker_fee

UP = "up"
DOWN = "down"
_SOURCES = ("spot", "futures", "deribit", "chainlink")
_BASE_SOURCES = ("spot", "futures", "deribit")
_KIND_TO_SOURCE = {
    "spot_bbo": "spot",
    "futures_bbo": "futures",
    "deribit_quote": "deribit",
}
_EPS = 1e-12


@dataclass(frozen=True)
class DetectorConfig:
    symbol: str = "BTC"
    window_ms: float = 500.0
    max_source_age_ms: float = 500.0
    min_basis_move_bp: float = 1.5
    max_basis_move_bp: float | None = None
    max_spot_move_bp: float = 0.35
    max_deribit_move_bp: float = 0.5
    max_chainlink_move_bp: float = 0.35
    min_pm_move: float = 0.02
    max_pm_spread: float = 0.05
    min_pm_quote_depth: float = 5.0
    min_remaining_s: float = 30.0
    max_remaining_s: float = 240.0
    min_net_edge: float = 0.03
    min_direct_depth: float = 5.0
    fee_rate: float = 0.07
    require_chainlink: bool = False
    require_confirmation: bool = False


@dataclass(frozen=True)
class WedgeSignal:
    market_id: str
    recv_ms: float
    anchor_recv_ms: float
    basis_direction: str
    buy_side: str
    fair: float
    ask: float
    fixed_limit: float
    fee: float
    net_edge: float
    direct_depth: float
    basis_change_bp: float
    spot_move_bp: float
    deribit_move_bp: float
    chainlink_move_bp: float | None
    pm_move: float
    remaining_s: float


@dataclass(frozen=True)
class _Quote:
    recv_ms: float
    order: int
    mid: float


@dataclass(frozen=True)
class _DirectBook:
    market_id: str
    recv_ms: float
    order: int
    end_ms: float
    up_bids: tuple[tuple[float, float], ...]
    up_asks: tuple[tuple[float, float], ...]
    down_bids: tuple[tuple[float, float], ...]
    down_asks: tuple[tuple[float, float], ...]

    def probability(self, side: str, *, max_spread: float, min_depth: float) -> float | None:
        bids = self.up_bids if side == UP else self.down_bids
        asks = self.up_asks if side == UP else self.down_asks
        if (not bids or not asks or bids[0][0] >= asks[0][0] - _EPS
                or asks[0][0] - bids[0][0] > max_spread + _EPS
                or bids[0][1] + _EPS < min_depth or asks[0][1] + _EPS < min_depth):
            return None
        return (bids[0][0] + asks[0][0]) / 2.0

    def asks(self, side: str) -> tuple[tuple[float, float], ...]:
        return self.up_asks if side == UP else self.down_asks


@dataclass(frozen=True)
class _WedgeCandidate:
    anchor: _DirectBook
    shock_recv_ms: float
    shock_order: int
    basis_direction: str
    before: _SourceState


@dataclass(frozen=True)
class _SourceState:
    spot: _Quote
    futures: _Quote
    deribit: _Quote
    chainlink: _Quote | None = None


def _finite(value: object, name: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _levels(raw: object, *, bids: bool) -> tuple[tuple[float, float], ...]:
    levels = []
    for raw_price, raw_size in raw if isinstance(raw, Sequence) else ():
        price = _finite(raw_price, "level price")
        size = _finite(raw_size, "level size")
        if not 0.0 <= price <= 1.0:
            raise ValueError("Polymarket prices must be in [0, 1]")
        if size > 0.0:
            levels.append((price, size))
    levels.sort(key=lambda level: level[0], reverse=bids)
    return tuple(levels)


def _bp_move(before: float, after: float) -> float:
    return (after / before - 1.0) * 10_000.0


def price_direct_asks(
    asks: Sequence[tuple[float, float]],
    fair: float,
    config: DetectorConfig,
    *,
    require_edge: bool = True,
) -> tuple[float, float, float, float, float] | None:
    """Price a directly displayed FAK without synthesising the other token.

    Direction-reversal is a diagnostic placebo, so it deliberately fixes the
    real same-side five-share cost even when that cost has negative model edge.
    Every candidate strategy still uses ``require_edge=True``.
    """
    remaining = config.min_direct_depth
    cost = 0.0
    raw_fee = Decimal(0)
    decimal_rate = Decimal(str(config.fee_rate))
    fixed_limit = 0.0
    eligible_depth = 0.0
    for price, available in asks:
        per_share_fee = float(taker_fee(price, config.fee_rate))
        if require_edge and fair - price - per_share_fee + _EPS < config.min_net_edge:
            break
        eligible_depth += available
        take = min(remaining, available)
        if take <= 0.0:
            continue
        remaining -= take
        cost += take * price
        decimal_price = Decimal(str(price))
        raw_fee += (
            Decimal(str(take)) * decimal_rate * decimal_price * (Decimal(1) - decimal_price)
        )
        fixed_limit = price
    if remaining > _EPS:
        return None
    shares = config.min_direct_depth
    rounded_fee = float(raw_fee.quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP))
    average_price = cost / shares
    fee_per_share = rounded_fee / shares
    net_edge = fair - average_price - fee_per_share
    if require_edge and net_edge + _EPS < config.min_net_edge:
        return None
    return average_price, fixed_limit, fee_per_share, net_edge, eligible_depth


class BasisWedgeDetector:
    """Consume normalized frames in local ``recv_ms`` order and emit candidates."""

    def __init__(self, config: DetectorConfig | None = None) -> None:
        self.config = config or DetectorConfig()
        self._clock_ms = -math.inf
        self._order = 0
        self._quotes: dict[str, list[_Quote]] = {source: [] for source in _SOURCES}
        self._books: dict[str, list[_DirectBook]] = {}
        self._candidates: dict[str, _WedgeCandidate] = {}
        self._emitted_markets: set[str] = set()

    def feed(self, event: Mapping[str, object]) -> WedgeSignal | None:
        event_symbol = event.get("symbol")
        if event_symbol is None:
            raise ValueError("symbol is required")
        if str(event_symbol).upper() != self.config.symbol.upper():
            return None
        recv_ms = _finite(event["recv_ms"], "recv_ms")
        kind = str(event["kind"])
        if recv_ms < self._clock_ms - _EPS:
            # Receipt regressions mean the merge is no longer causal.  Clear every live
            # dependency rather than silently retaining a candidate across the bad frame.
            self._clear_live_state()
            return None
        self._clock_ms = max(self._clock_ms, recv_ms)
        self._order += 1
        order = self._order
        if kind == "disconnect" or kind.endswith("_disconnect"):
            self._clear_live_state()
            return None
        if kind in _KIND_TO_SOURCE:
            source = _KIND_TO_SOURCE[kind]
            self._feed_bbo(source, recv_ms, order, event)
            if source == "futures":
                self._capture_candidates(recv_ms, order)
            return self._evaluate_cached_books(recv_ms, order)
        if kind in ("chainlink", "chainlink_quote"):
            self._feed_price("chainlink", recv_ms, order, event)
            return self._evaluate_cached_books(recv_ms, order)
        if kind == "pm_book":
            current = self._direct_book(recv_ms, order, event)
            signal = self._evaluate(current)
            history = self._books.setdefault(current.market_id, [])
            history.append(current)
            self._prune_books(history, recv_ms)
            if signal is not None:
                self._emitted_markets.add(current.market_id)
                self._candidates.pop(current.market_id, None)
            return signal
        raise ValueError(f"unknown event kind: {kind}")

    def _evaluate_cached_books(self, recv_ms: float, order: int) -> WedgeSignal | None:
        """Decide immediately when the last required source confirmation arrives."""
        for market_id in sorted(self._books):
            history = self._books[market_id]
            if not history:
                continue
            current = replace(history[-1], recv_ms=recv_ms, order=order)
            signal = self._evaluate(current)
            if signal is not None:
                self._emitted_markets.add(market_id)
                self._candidates.pop(market_id, None)
                return signal
        return None

    def _clear_live_state(self) -> None:
        self._quotes = {source: [] for source in _SOURCES}
        self._books.clear()
        self._candidates.clear()

    def _feed_bbo(
        self,
        source: str,
        recv_ms: float,
        order: int,
        event: Mapping[str, object],
    ) -> None:
        bid = _finite(event["bid"], "bid")
        ask = _finite(event["ask"], "ask")
        if bid <= 0.0 or ask <= bid:
            raise ValueError(f"invalid {source} BBO")
        history = self._quotes[source]
        history.append(_Quote(recv_ms, order, (bid + ask) / 2.0))
        cutoff = recv_ms - self.config.window_ms - self.config.max_source_age_ms
        while history and history[0].recv_ms < cutoff - _EPS:
            history.pop(0)

    def _feed_price(
        self,
        source: str,
        recv_ms: float,
        order: int,
        event: Mapping[str, object],
    ) -> None:
        price = _finite(event.get("price", event.get("value")), "price")
        if price <= 0.0:
            raise ValueError(f"invalid {source} price")
        history = self._quotes[source]
        history.append(_Quote(recv_ms, order, price))
        cutoff = recv_ms - self.config.window_ms - self.config.max_source_age_ms
        while history and history[0].recv_ms < cutoff - _EPS:
            history.pop(0)

    def _direct_book(
        self,
        recv_ms: float,
        order: int,
        event: Mapping[str, object],
    ) -> _DirectBook:
        return _DirectBook(
            market_id=str(event["market_id"]),
            recv_ms=recv_ms,
            order=order,
            end_ms=_finite(event["end_ms"], "end_ms"),
            up_bids=_levels(event.get("up_bids"), bids=True),
            up_asks=_levels(event.get("up_asks"), bids=False),
            down_bids=_levels(event.get("down_bids"), bids=True),
            down_asks=_levels(event.get("down_asks"), bids=False),
        )

    def _capture_candidates(self, recv_ms: float, shock_order: int) -> None:
        latest = self._latest_state(recv_ms)
        if latest is None:
            return
        shock_recv_ms = latest.futures.recv_ms
        for market_id, history in self._books.items():
            if market_id in self._emitted_markets:
                continue
            existing = self._candidates.get(market_id)
            if existing is not None:
                if recv_ms - existing.anchor.recv_ms <= self.config.window_ms + _EPS:
                    continue
                self._candidates.pop(market_id, None)
            anchor = next(
                (
                    book for book in reversed(history)
                    if (book.recv_ms, book.order) <= (shock_recv_ms, shock_order)
                ),
                None,
            )
            if anchor is None or recv_ms - anchor.recv_ms > self.config.window_ms + _EPS:
                continue
            remaining_s = (anchor.end_ms - shock_recv_ms) / 1_000.0
            if not self.config.min_remaining_s - _EPS <= remaining_s <= self.config.max_remaining_s + _EPS:
                continue
            before = self._state_at(anchor.recv_ms, anchor.order)
            if before is None:
                continue
            metrics = self._source_metrics(before, latest)
            if not self._capture_gates_pass(metrics):
                continue
            direction = UP if metrics[0] > 0.0 else DOWN
            self._candidates[market_id] = _WedgeCandidate(
                anchor=anchor,
                shock_recv_ms=shock_recv_ms,
                shock_order=shock_order,
                basis_direction=direction,
                before=before,
            )

    def _evaluate(self, current: _DirectBook) -> WedgeSignal | None:
        if current.market_id in self._emitted_markets:
            return None
        candidate = self._candidates.get(current.market_id)
        if candidate is None or current.recv_ms < candidate.shock_recv_ms - _EPS:
            return None
        if current.recv_ms - candidate.anchor.recv_ms > self.config.window_ms + _EPS:
            self._candidates.pop(current.market_id, None)
            return None
        remaining_s = (current.end_ms - current.recv_ms) / 1_000.0
        if not self.config.min_remaining_s - _EPS <= remaining_s <= self.config.max_remaining_s + _EPS:
            return None
        latest = self._latest_state(
            current.recv_ms,
            shock_recv_ms=candidate.shock_recv_ms,
            shock_order=candidate.shock_order,
        )
        if latest is None:
            return None
        metrics = self._source_metrics(candidate.before, latest)
        if not self._source_gates_pass(metrics):
            return None
        basis_change_bp, spot_move_bp, deribit_move_bp, chainlink_move_bp = metrics
        direction = UP if basis_change_bp > 0.0 else DOWN
        if direction != candidate.basis_direction:
            return None
        buy_side = DOWN if direction == UP else UP
        probability_args = {
            "max_spread": self.config.max_pm_spread,
            "min_depth": self.config.min_pm_quote_depth,
        }
        pm_before = candidate.anchor.probability(direction, **probability_args)
        pm_now = current.probability(direction, **probability_args)
        if pm_before is None or pm_now is None:
            return None
        fair = 1.0 - pm_before
        pm_move = pm_now - pm_before
        if pm_move + _EPS < self.config.min_pm_move:
            return None
        asks = current.asks(buy_side)
        if not asks:
            return None
        fill = self._direct_fill(asks, fair)
        if fill is None:
            return None
        ask, fixed_limit, fee, net_edge, direct_depth = fill
        return WedgeSignal(
            market_id=current.market_id,
            recv_ms=current.recv_ms,
            anchor_recv_ms=candidate.anchor.recv_ms,
            basis_direction=direction,
            buy_side=buy_side,
            fair=fair,
            ask=ask,
            fixed_limit=fixed_limit,
            fee=fee,
            net_edge=net_edge,
            direct_depth=direct_depth,
            basis_change_bp=basis_change_bp,
            spot_move_bp=spot_move_bp,
            deribit_move_bp=deribit_move_bp,
            chainlink_move_bp=chainlink_move_bp,
            pm_move=pm_move,
            remaining_s=remaining_s,
        )

    def _latest_state(
        self,
        recv_ms: float,
        *,
        shock_recv_ms: float | None = None,
        shock_order: int | None = None,
    ) -> _SourceState | None:
        quotes = [self._latest(source) for source in _BASE_SOURCES]
        if any(quote is None for quote in quotes):
            return None
        spot, futures, deribit = quotes
        if any(recv_ms - quote.recv_ms > self.config.max_source_age_ms + _EPS
               for quote in quotes if quote is not None):
            return None
        if shock_recv_ms is not None:
            if shock_order is None:
                raise ValueError("shock_order is required with shock_recv_ms")
            shock_key = (shock_recv_ms, shock_order)
            if (futures.recv_ms, futures.order) < shock_key or any(
                (quote.recv_ms, quote.order) <= shock_key for quote in (spot, deribit)
            ):
                return None
        chainlink = self._latest("chainlink")
        if (
            chainlink is not None
            and shock_recv_ms is not None
            and (chainlink.recv_ms, chainlink.order) <= (shock_recv_ms, shock_order)
        ):
            chainlink = None
        if self.config.require_chainlink and (
                chainlink is None
                or recv_ms - chainlink.recv_ms > self.config.max_source_age_ms + _EPS):
            return None
        return _SourceState(spot, futures, deribit, chainlink)  # type: ignore[arg-type]

    def _state_at(self, recv_ms: float, order: int) -> _SourceState | None:
        quotes = [self._at_or_before(source, recv_ms, order) for source in _BASE_SOURCES]
        if any(quote is None for quote in quotes):
            return None
        spot, futures, deribit = quotes
        if any(recv_ms - quote.recv_ms > self.config.max_source_age_ms + _EPS
               for quote in quotes if quote is not None):
            return None
        chainlink = self._at_or_before("chainlink", recv_ms, order)
        if self.config.require_chainlink and (
                chainlink is None
                or recv_ms - chainlink.recv_ms > self.config.max_source_age_ms + _EPS):
            return None
        return _SourceState(spot, futures, deribit, chainlink)  # type: ignore[arg-type]

    @staticmethod
    def _source_metrics(
        before: _SourceState,
        latest: _SourceState,
    ) -> tuple[float, float, float, float | None]:
        basis_before = before.futures.mid / before.spot.mid - 1.0
        basis_now = latest.futures.mid / latest.spot.mid - 1.0
        chainlink_move = None
        if before.chainlink is not None and latest.chainlink is not None:
            chainlink_move = _bp_move(before.chainlink.mid, latest.chainlink.mid)
        return (
            (basis_now - basis_before) * 10_000.0,
            _bp_move(before.spot.mid, latest.spot.mid),
            _bp_move(before.deribit.mid, latest.deribit.mid),
            chainlink_move,
        )

    def _source_gates_pass(self, metrics: tuple[float, float, float, float | None]) -> bool:
        basis, spot, deribit, chainlink = metrics
        if not self._basis_gate_pass(basis):
            return False
        chainlink_quiet = (
            chainlink is None or abs(chainlink) <= self.config.max_chainlink_move_bp + _EPS
        )
        if self.config.require_confirmation:
            direction = 1.0 if basis > 0.0 else -1.0
            confirms = (
                direction * spot > self.config.max_spot_move_bp + _EPS
                or direction * deribit > self.config.max_deribit_move_bp + _EPS
            )
            return (
                confirms
                and chainlink_quiet
                and (not self.config.require_chainlink or chainlink is not None)
            )
        return (
            abs(spot) <= self.config.max_spot_move_bp + _EPS
            and abs(deribit) <= self.config.max_deribit_move_bp + _EPS
            and chainlink_quiet
            and (not self.config.require_chainlink or chainlink is not None)
        )

    def _capture_gates_pass(
        self,
        metrics: tuple[float, float, float, float | None],
    ) -> bool:
        if not self._basis_gate_pass(metrics[0]):
            return False
        # Confirmation must be observed on strictly post-shock source frames,
        # so it cannot be required at the instant the immutable shock is saved.
        return self.config.require_confirmation or self._source_gates_pass(metrics)

    def _basis_gate_pass(self, basis: float) -> bool:
        magnitude = abs(basis)
        maximum = self.config.max_basis_move_bp
        return (
            magnitude > _EPS
            and magnitude + _EPS >= self.config.min_basis_move_bp
            and (maximum is None or magnitude <= maximum + _EPS)
        )

    def _direct_fill(
        self,
        asks: tuple[tuple[float, float], ...],
        fair: float,
    ) -> tuple[float, float, float, float, float] | None:
        return price_direct_asks(asks, fair, self.config)

    def _latest(self, source: str) -> _Quote | None:
        history = self._quotes[source]
        return history[-1] if history else None

    def _at_or_before(self, source: str, recv_ms: float, order: int) -> _Quote | None:
        for quote in reversed(self._quotes[source]):
            if (quote.recv_ms, quote.order) <= (recv_ms, order):
                return quote
        return None

    def _prune_books(self, history: list[_DirectBook], recv_ms: float) -> None:
        cutoff = recv_ms - self.config.window_ms
        while history and history[0].recv_ms < cutoff - _EPS:
            history.pop(0)
