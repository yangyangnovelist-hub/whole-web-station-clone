"""Receipt-causal detector for a Binance common-mode benchmark residual."""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

import basis_wedge as bw

UP = bw.UP
DOWN = bw.DOWN
_EPS = 1e-12
_QUOTE_KINDS = {
    "spot_bbo": "spot",
    "futures_bbo": "futures",
    "deribit_quote": "deribit_quote",
}
_SOURCES = ("spot", "futures", "deribit_quote", "deribit_index")
_WEDGE_SOURCE_KINDS = frozenset({"spot_bbo", "futures_bbo", "deribit_quote"})


@dataclass(frozen=True)
class DetectorConfig:
    symbol: str = "BTC"
    window_ms: float = 500.0
    confirmation_timeout_ms: float = 1_500.0
    max_source_age_ms: float = 500.0
    min_common_move_bp: float = 1.5
    max_basis_change_bp: float = 0.5
    max_benchmark_move_bp: float = 0.5
    min_pm_move: float = 0.02
    max_pm_move: float | None = None
    max_pm_spread: float = 0.05
    min_pm_quote_depth: float = 5.0
    min_remaining_s: float = 30.0
    max_remaining_s: float = 240.0
    min_net_edge: float = 0.03
    min_direct_depth: float = 5.0
    fee_rate: float = 0.07
    require_benchmark_confirmation: bool = False


@dataclass(frozen=True)
class CommonModeResidualSignal:
    market_id: str
    recv_ms: float
    anchor_recv_ms: float
    crossing_recv_ms: float
    common_direction: str
    buy_side: str
    fair: float
    ask: float
    fixed_limit: float
    fee: float
    net_edge: float
    direct_depth: float
    spot_move_bp: float
    futures_move_bp: float
    basis_change_bp: float
    deribit_quote_move_bp: float
    deribit_index_move_bp: float
    pm_move: float
    remaining_s: float


@dataclass(frozen=True)
class _Quote:
    recv_ms: float
    order: int
    mid: float

    @property
    def key(self) -> tuple[float, int]:
        return self.recv_ms, self.order


@dataclass(frozen=True)
class _Crossing:
    anchor_recv_ms: float
    anchor_order: int
    crossing_recv_ms: float
    crossing_order: int
    common_direction: str
    spot_move_bp: float
    futures_move_bp: float
    basis_change_bp: float
    anchor_book: bw._DirectBook
    deribit_quote_before: _Quote
    deribit_index_before: _Quote

    @property
    def anchor_key(self) -> tuple[float, int]:
        return self.anchor_recv_ms, self.anchor_order

    @property
    def crossing_key(self) -> tuple[float, int]:
        return self.crossing_recv_ms, self.crossing_order


class CommonModeResidualDetector:
    """Consume normalized frames in local receipt order and emit paper signals."""

    def __init__(self, config: DetectorConfig | None = None) -> None:
        self.config = config or DetectorConfig()
        self._clock_ms = -math.inf
        self._order = 0
        self._quotes: dict[str, list[_Quote]] = {source: [] for source in _SOURCES}
        self._books: dict[str, list[bw._DirectBook]] = {}
        self._crossings: dict[str, _Crossing] = {}
        self._attempted_markets: set[str] = set()
        self._emitted_markets: set[str] = set()
        self._rejected_markets: set[str] = set()
        self._wedge_detectors: dict[str, bw.BasisWedgeDetector] = {}
        self._wedge_source_tape: list[dict[str, object]] = []
        self._basis_wedge_markets: set[str] = set()

    def feed(self, event: Mapping[str, object]) -> CommonModeResidualSignal | None:
        event_symbol = event.get("symbol")
        if event_symbol is None:
            raise ValueError("symbol is required")
        if str(event_symbol).upper() != self.config.symbol.upper():
            return None
        recv_ms = bw._finite(event["recv_ms"], "recv_ms")
        kind = str(event["kind"])
        if recv_ms < self._clock_ms - _EPS:
            self._clear_live_state()
            self._wedge_detectors.clear()
            self._wedge_source_tape.clear()
            return None
        self._clock_ms = max(self._clock_ms, recv_ms)
        self._order += 1
        order = self._order
        if kind == "disconnect" or kind.endswith("_disconnect"):
            self._feed_wedge_exclusion(event)
            self._clear_live_state()
            return None
        self._feed_wedge_exclusion(event)
        if kind in _QUOTE_KINDS:
            source = _QUOTE_KINDS[kind]
            self._feed_bbo(source, recv_ms, order, event)
            if source in ("spot", "futures"):
                self._capture_crossings(recv_ms, order)
            return self._evaluate_cached_books(recv_ms, order)
        if kind == "deribit_index":
            self._feed_index(recv_ms, order, event)
            return self._evaluate_cached_books(recv_ms, order)
        if kind == "pm_book":
            current = self._direct_book(recv_ms, order, event)
            history = self._books.setdefault(current.market_id, [])
            history.append(current)
            self._prune_books(history, recv_ms)
            signal = self._evaluate(current, recv_ms, order)
            if signal is not None:
                self._emitted_markets.add(current.market_id)
            return signal
        raise ValueError(f"unknown event kind: {kind}")

    def _clear_live_state(self) -> None:
        self._quotes = {source: [] for source in _SOURCES}
        self._books.clear()
        self._crossings.clear()

    def _wedge_config(self) -> bw.DetectorConfig:
        return bw.DetectorConfig(
            symbol=self.config.symbol,
            window_ms=self.config.window_ms,
            max_source_age_ms=self.config.max_source_age_ms,
            min_pm_move=self.config.min_pm_move,
            max_pm_spread=self.config.max_pm_spread,
            min_pm_quote_depth=self.config.min_pm_quote_depth,
            min_remaining_s=self.config.min_remaining_s,
            max_remaining_s=self.config.max_remaining_s,
            min_net_edge=self.config.min_net_edge,
            min_direct_depth=self.config.min_direct_depth,
            fee_rate=self.config.fee_rate,
        )

    def _feed_wedge_exclusion(self, event: Mapping[str, object]) -> None:
        """Run frozen US-015 semantics per market before admitting US-017.

        US-015 anchors source moves at the latest Polymarket book, while this
        detector anchors its common move on Binance source frames.  Comparing
        only the two resulting basis deltas is therefore not enough to make the
        families disjoint.  A per-market shadow state machine preserves the
        exact US-015 book-anchor semantics and permanently excludes any market
        as soon as that mechanism captures its immutable candidate.  Waiting
        for its later quiet-source confirmation would allow US-017 to emit in
        the interval between capture and confirmation.
        """
        kind = str(event["kind"])
        if kind == "disconnect" or kind.endswith("_disconnect"):
            for market_id, detector in self._wedge_detectors.items():
                signal = detector.feed(event)
                self._latch_wedge_market(market_id, detector, signal)
            self._wedge_source_tape.clear()
            return
        if kind in _WEDGE_SOURCE_KINDS:
            frozen_event = dict(event)
            self._wedge_source_tape.append(frozen_event)
            recv_ms = bw._finite(event["recv_ms"], "recv_ms")
            cutoff = recv_ms - self.config.window_ms - self.config.max_source_age_ms
            while (
                self._wedge_source_tape
                and bw._finite(self._wedge_source_tape[0]["recv_ms"], "recv_ms")
                < cutoff - _EPS
            ):
                self._wedge_source_tape.pop(0)
            for market_id, detector in self._wedge_detectors.items():
                signal = detector.feed(frozen_event)
                self._latch_wedge_market(market_id, detector, signal)
            return
        if kind != "pm_book":
            return
        market_id = str(event["market_id"])
        detector = self._wedge_detectors.get(market_id)
        if detector is None:
            detector = bw.BasisWedgeDetector(self._wedge_config())
            for source_event in self._wedge_source_tape:
                detector.feed(source_event)
            self._wedge_detectors[market_id] = detector
        signal = detector.feed(event)
        self._latch_wedge_market(market_id, detector, signal)

    def _latch_wedge_market(
        self,
        market_id: str,
        detector: bw.BasisWedgeDetector,
        signal: bw.WedgeSignal | None,
    ) -> None:
        if signal is not None or market_id in detector._candidates:
            self._basis_wedge_markets.add(market_id)

    def _feed_bbo(
        self,
        source: str,
        recv_ms: float,
        order: int,
        event: Mapping[str, object],
    ) -> None:
        bid = bw._finite(event["bid"], "bid")
        ask = bw._finite(event["ask"], "ask")
        if bid <= 0.0 or ask <= bid:
            raise ValueError(f"invalid {source} BBO")
        self._append_quote(source, _Quote(recv_ms, order, (bid + ask) / 2.0))

    def _feed_index(
        self,
        recv_ms: float,
        order: int,
        event: Mapping[str, object],
    ) -> None:
        price = bw._finite(event.get("price", event.get("value")), "price")
        if price <= 0.0:
            raise ValueError("invalid deribit index")
        self._append_quote("deribit_index", _Quote(recv_ms, order, price))

    def _append_quote(self, source: str, quote: _Quote) -> None:
        history = self._quotes[source]
        history.append(quote)
        retention_ms = max(
            self.config.window_ms + self.config.max_source_age_ms,
            self.config.confirmation_timeout_ms,
        )
        cutoff = quote.recv_ms - retention_ms
        while history and history[0].recv_ms < cutoff - _EPS:
            history.pop(0)

    def _direct_book(
        self,
        recv_ms: float,
        order: int,
        event: Mapping[str, object],
    ) -> bw._DirectBook:
        return bw._DirectBook(
            market_id=str(event["market_id"]),
            recv_ms=recv_ms,
            order=order,
            end_ms=bw._finite(event["end_ms"], "end_ms"),
            up_bids=bw._levels(event.get("up_bids"), bids=True),
            up_asks=bw._levels(event.get("up_asks"), bids=False),
            down_bids=bw._levels(event.get("down_bids"), bids=True),
            down_asks=bw._levels(event.get("down_asks"), bids=False),
        )

    def _capture_crossings(self, recv_ms: float, order: int) -> None:
        current_spot = self._latest("spot")
        current_futures = self._latest("futures")
        if current_spot is None or current_futures is None:
            return
        if any(
            recv_ms - quote.recv_ms > self.config.max_source_age_ms + _EPS
            for quote in (current_spot, current_futures)
        ):
            return
        crossing = self._first_qualifying_window(current_spot, current_futures, recv_ms, order)
        if crossing is None:
            return
        anchor_recv_ms, anchor_order, direction, spot_move, futures_move, basis_change = crossing
        anchor_key = (anchor_recv_ms, anchor_order)
        attempted_now = [
            market_id for market_id in self._books
            if market_id not in self._emitted_markets
            and market_id not in self._attempted_markets
        ]
        self._attempted_markets.update(attempted_now)
        if not attempted_now:
            return
        quote_before = self._at_or_before("deribit_quote", anchor_key)
        index_before = self._at_or_before("deribit_index", anchor_key)
        if quote_before is None or index_before is None:
            return
        if any(
            anchor_recv_ms - quote.recv_ms > self.config.max_source_age_ms + _EPS
            for quote in (quote_before, index_before)
        ):
            return
        for market_id in attempted_now:
            history = self._books[market_id]
            anchor_book = self._book_at_or_before(history, anchor_key)
            if anchor_book is None:
                continue
            if anchor_recv_ms - anchor_book.recv_ms > self.config.max_source_age_ms + _EPS:
                continue
            self._crossings[market_id] = _Crossing(
                anchor_recv_ms=anchor_recv_ms,
                anchor_order=anchor_order,
                crossing_recv_ms=recv_ms,
                crossing_order=order,
                common_direction=direction,
                spot_move_bp=spot_move,
                futures_move_bp=futures_move,
                basis_change_bp=basis_change,
                anchor_book=anchor_book,
                deribit_quote_before=quote_before,
                deribit_index_before=index_before,
            )

    def _first_qualifying_window(
        self,
        current_spot: _Quote,
        current_futures: _Quote,
        recv_ms: float,
        order: int,
    ) -> tuple[float, int, str, float, float, float] | None:
        current_key = (recv_ms, order)
        anchor_keys = sorted({
            quote.key
            for source in ("spot", "futures")
            for quote in self._quotes[source]
            if recv_ms - self.config.window_ms - _EPS <= quote.recv_ms
            and quote.key < current_key
        })
        for anchor_key in anchor_keys:
            spot_before = self._at_or_before("spot", anchor_key)
            futures_before = self._at_or_before("futures", anchor_key)
            if spot_before is None or futures_before is None:
                continue
            if any(
                anchor_key[0] - quote.recv_ms > self.config.max_source_age_ms + _EPS
                for quote in (spot_before, futures_before)
            ):
                continue
            if any(
                recv_ms - quote.recv_ms > self.config.window_ms + _EPS
                for quote in (spot_before, futures_before)
            ):
                continue
            spot_move = bw._bp_move(spot_before.mid, current_spot.mid)
            futures_move = bw._bp_move(futures_before.mid, current_futures.mid)
            if not self._same_direction_common_move(spot_move, futures_move):
                continue
            basis_before = futures_before.mid / spot_before.mid - 1.0
            basis_now = current_futures.mid / current_spot.mid - 1.0
            basis_change = (basis_now - basis_before) * 10_000.0
            if abs(basis_change) > self.config.max_basis_change_bp + _EPS:
                continue
            direction = UP if spot_move > 0.0 else DOWN
            return (*anchor_key, direction, spot_move, futures_move, basis_change)
        return None

    def _same_direction_common_move(self, spot_move: float, futures_move: float) -> bool:
        threshold = self.config.min_common_move_bp
        return (
            spot_move >= threshold - _EPS and futures_move >= threshold - _EPS
        ) or (
            spot_move <= -threshold + _EPS and futures_move <= -threshold + _EPS
        )

    def _evaluate_cached_books(
        self,
        recv_ms: float,
        order: int,
    ) -> CommonModeResidualSignal | None:
        for market_id in sorted(self._books):
            history = self._books[market_id]
            if not history:
                continue
            signal = self._evaluate(history[-1], recv_ms, order)
            if signal is not None:
                self._emitted_markets.add(market_id)
                return signal
        return None

    def _evaluate(
        self,
        current: bw._DirectBook,
        decision_recv_ms: float,
        decision_order: int,
    ) -> CommonModeResidualSignal | None:
        if current.market_id in self._emitted_markets:
            return None
        if current.market_id in self._rejected_markets:
            return None
        if current.market_id in self._basis_wedge_markets:
            return None
        crossing = self._crossings.get(current.market_id)
        if crossing is None:
            return None
        decision_key = (decision_recv_ms, decision_order)
        if decision_key <= crossing.crossing_key:
            return None
        if (current.recv_ms, current.order) <= crossing.crossing_key:
            return None
        if (
            decision_recv_ms - crossing.crossing_recv_ms
            > self.config.confirmation_timeout_ms + _EPS
        ):
            return None
        if decision_recv_ms - current.recv_ms > self.config.max_source_age_ms + _EPS:
            return None
        binance_latest = (self._latest("spot"), self._latest("futures"))
        if any(quote is None for quote in binance_latest):
            return None
        if any(
            decision_recv_ms - quote.recv_ms > self.config.max_source_age_ms + _EPS
            for quote in binance_latest
            if quote is not None
        ):
            return None
        quote_path = self._between(
            "deribit_quote", crossing.crossing_key, decision_key,
        )
        index_path = self._between(
            "deribit_index", crossing.crossing_key, decision_key,
        )
        if not quote_path or not index_path:
            return None
        if any(
            quote.recv_ms - crossing.crossing_recv_ms
            > self.config.confirmation_timeout_ms + _EPS
            for quote in (quote_path[0], index_path[0])
        ):
            return None
        quote_moves = tuple(
            bw._bp_move(crossing.deribit_quote_before.mid, quote.mid)
            for quote in quote_path
        )
        index_moves = tuple(
            bw._bp_move(crossing.deribit_index_before.mid, quote.mid)
            for quote in index_path
        )
        if not self._benchmark_gate(crossing.common_direction, quote_moves, index_moves):
            return None
        quote_move = quote_moves[-1]
        index_move = index_moves[-1]
        remaining_s = (current.end_ms - decision_recv_ms) / 1_000.0
        if not (
            self.config.min_remaining_s - _EPS
            <= remaining_s
            <= self.config.max_remaining_s + _EPS
        ):
            return None
        probability_args = {
            "max_spread": self.config.max_pm_spread,
            "min_depth": self.config.min_pm_quote_depth,
        }
        pm_before = crossing.anchor_book.probability(
            crossing.common_direction, **probability_args,
        )
        pm_now = current.probability(crossing.common_direction, **probability_args)
        if pm_before is None or pm_now is None:
            return None
        pm_move = pm_now - pm_before
        if pm_move + _EPS < self.config.min_pm_move:
            return None
        if (
            self.config.max_pm_move is not None
            and pm_move > self.config.max_pm_move + _EPS
        ):
            self._rejected_markets.add(current.market_id)
            return None
        buy_side = DOWN if crossing.common_direction == UP else UP
        fair = 1.0 - pm_before
        fill = bw.price_direct_asks(current.asks(buy_side), fair, self.config)
        if fill is None:
            return None
        ask, fixed_limit, fee, net_edge, direct_depth = fill
        return CommonModeResidualSignal(
            market_id=current.market_id,
            recv_ms=decision_recv_ms,
            anchor_recv_ms=crossing.anchor_recv_ms,
            crossing_recv_ms=crossing.crossing_recv_ms,
            common_direction=crossing.common_direction,
            buy_side=buy_side,
            fair=fair,
            ask=ask,
            fixed_limit=fixed_limit,
            fee=fee,
            net_edge=net_edge,
            direct_depth=direct_depth,
            spot_move_bp=crossing.spot_move_bp,
            futures_move_bp=crossing.futures_move_bp,
            basis_change_bp=crossing.basis_change_bp,
            deribit_quote_move_bp=quote_move,
            deribit_index_move_bp=index_move,
            pm_move=pm_move,
            remaining_s=remaining_s,
        )

    def _benchmark_gate(
        self,
        direction: str,
        quote_moves: tuple[float, ...],
        index_moves: tuple[float, ...],
    ) -> bool:
        maximum = self.config.max_benchmark_move_bp
        if self.config.require_benchmark_confirmation:
            sign = 1.0 if direction == UP else -1.0
            return (
                sign * quote_moves[-1] > maximum + _EPS
                and sign * index_moves[-1] > maximum + _EPS
            )
        return all(
            abs(move) <= maximum + _EPS
            for move in (*quote_moves, *index_moves)
        )

    def _latest(self, source: str) -> _Quote | None:
        history = self._quotes[source]
        return history[-1] if history else None

    def _between(
        self,
        source: str,
        start: tuple[float, int],
        end: tuple[float, int],
    ) -> list[_Quote]:
        return [quote for quote in self._quotes[source] if start < quote.key <= end]

    def _at_or_before(
        self,
        source: str,
        key: tuple[float, int],
    ) -> _Quote | None:
        for quote in reversed(self._quotes[source]):
            if quote.key <= key:
                return quote
        return None

    @staticmethod
    def _book_at_or_before(
        history: list[bw._DirectBook],
        key: tuple[float, int],
    ) -> bw._DirectBook | None:
        for book in reversed(history):
            if (book.recv_ms, book.order) <= key:
                return book
        return None

    def _prune_books(self, history: list[bw._DirectBook], recv_ms: float) -> None:
        cutoff = recv_ms - self.config.window_ms - self.config.max_source_age_ms
        while history and history[0].recv_ms < cutoff - _EPS:
            history.pop(0)
