"""Receipt-causal detector for high one-sided trade flow pinned by hidden liquidity.

The mechanism is deliberately disjoint from ``absorption.AbsorptionDetector``:
that family requires a visible ask depletion followed by a displayed refill,
whereas this detector rejects any half-depth depletion during the observation
window.  A signal therefore represents repeated aggressive buying that consumes
at least the displayed best-ask quantity without moving the quote materially.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from absorption import TokenBook


@dataclass(frozen=True)
class PinnedFlowConfig:
    window_ms: float = 500.0
    minimum_buy_shares: float = 15.0
    minimum_buy_prints: int = 3
    minimum_buy_fraction: float = 0.80
    minimum_consumption_multiple: float = 1.0
    visible_depletion_fraction: float = 0.50
    maximum_mid_impact: float = 0.01
    maximum_trade_price_impact: float = 0.01
    minimum_remaining_s: float = 20.0
    maximum_remaining_s: float = 240.0
    target_shares: float = 5.0
    book_fresh_ms: float = 2_000.0

    def __post_init__(self) -> None:
        positive = (
            self.window_ms,
            self.minimum_buy_shares,
            self.minimum_consumption_multiple,
            self.target_shares,
            self.book_fresh_ms,
        )
        if any(not math.isfinite(value) or value <= 0 for value in positive):
            raise ValueError("positive pinned-flow parameters must be finite")
        if self.minimum_buy_prints < 1:
            raise ValueError("minimum_buy_prints must be positive")
        for value in (self.minimum_buy_fraction, self.visible_depletion_fraction):
            if not math.isfinite(value) or not 0 < value <= 1:
                raise ValueError("fractional pinned-flow parameters must be in (0, 1]")
        for value in (self.maximum_mid_impact, self.maximum_trade_price_impact):
            if not math.isfinite(value) or value < 0:
                raise ValueError("impact limits must be finite and non-negative")


@dataclass
class FlowWindow:
    market_id: str
    asset_id: str
    start_ms: float
    deadline_ms: float
    baseline_bid: float
    baseline_ask: float
    baseline_mid: float
    baseline_ask_depth: float
    trough_ask_depth: float
    buy_shares: float = 0.0
    sell_shares: float = 0.0
    buy_prints: int = 0
    highest_buy_price: float = -math.inf


class PinnedFlowDetector:
    """Find the first immutable hidden-liquidity pinning episode per market."""

    def __init__(self, config: PinnedFlowConfig | None = None) -> None:
        self.config = config or PinnedFlowConfig()
        self.markets: dict[str, dict[str, Any]] = {}
        self.token_market: dict[str, str] = {}
        self.token_side: dict[str, str] = {}
        self.books: dict[str, TokenBook] = {}
        self.windows: dict[str, FlowWindow] = {}
        self.locked_markets: set[str] = set()
        self.tainted_markets: set[str] = set()
        self.outcomes: list[dict[str, Any]] = []
        self.connected = False
        self.epoch = 0

    def on_event(self, event: Mapping[str, Any]) -> list[dict[str, Any]]:
        kind = str(event.get("kind") or "")
        recv_ms = float(event.get("recv_ms", -math.inf))
        signals = self._advance(recv_ms)
        if kind == "market":
            self._register_market(event)
            return signals
        if kind == "connection":
            self.connected = True
            self.epoch = int(event.get("epoch") or self.epoch + 1)
            self.books.clear()
            self.windows.clear()
            return signals
        if kind == "disconnect":
            self.connected = False
            self.books.clear()
            self.windows.clear()
            return signals
        if not self.connected:
            return signals
        event_epoch = event.get("epoch")
        if event_epoch is not None and int(event_epoch) != self.epoch:
            return signals
        market_id = self._event_market(event)
        if event.get("ambiguous_receipt") is True and market_id is not None:
            self._taint(market_id, recv_ms, "equal_receipt_ambiguity")
            return signals
        if market_id is not None and market_id in self.tainted_markets:
            return signals
        if kind == "snapshot":
            self._snapshot(event)
        elif kind == "price_change":
            self._price_change(event)
        elif kind == "trade":
            self._trade(event)
        return signals

    def flush(self, recv_ms: float) -> list[dict[str, Any]]:
        return self._advance(float(recv_ms))

    def _register_market(self, event: Mapping[str, Any]) -> None:
        market_id = str(event["market_id"])
        up = str(event["up_token_id"])
        down = str(event["down_token_id"])
        self.markets[market_id] = {
            "market_id": market_id,
            "slot": int(event["slot"]),
            "up_token_id": up,
            "down_token_id": down,
        }
        self.token_market[up] = market_id
        self.token_market[down] = market_id
        self.token_side[up] = "Up"
        self.token_side[down] = "Down"

    def _event_market(self, event: Mapping[str, Any]) -> str | None:
        if event.get("market_id") is not None:
            return str(event["market_id"])
        token = event.get("asset_id")
        return self.token_market.get(str(token)) if token is not None else None

    def _taint(self, market_id: str, recv_ms: float, reason: str) -> None:
        self.tainted_markets.add(market_id)
        self.locked_markets.add(market_id)
        for token in [key for key, window in self.windows.items() if window.market_id == market_id]:
            self.windows.pop(token, None)
        self.outcomes.append({"market_id": market_id, "recv_ms": recv_ms, "reason": reason})

    def _snapshot(self, event: Mapping[str, Any]) -> None:
        token = str(event["asset_id"])
        book = self.books.setdefault(token, TokenBook())
        try:
            book.replace(event.get("bids") or (), event.get("asks") or (), float(event["recv_ms"]))
        except (KeyError, TypeError, ValueError):
            book.ready = False
        self._observe_depth(token, float(event["recv_ms"]))

    def _price_change(self, event: Mapping[str, Any]) -> None:
        recv_ms = float(event["recv_ms"])
        touched: set[str] = set()
        for change in event.get("changes") or ():
            token = str(change.get("asset_id") or "")
            book = self.books.setdefault(token, TokenBook())
            try:
                book.change(
                    str(change.get("side") or ""),
                    change.get("price"),
                    change.get("size"),
                    recv_ms,
                )
            except (TypeError, ValueError):
                book.ready = False
            touched.add(token)
        for token in touched:
            self._observe_depth(token, recv_ms)

    def _observe_depth(self, token: str, recv_ms: float) -> None:
        book = self.books.get(token)
        market_id = self.token_market.get(token)
        if book is not None and not book.ready:
            # A later snapshot cannot certify continuous depth across a gap.
            # Invalidate either token's active market window, including when
            # the complementary execution book loses its valid state.
            if market_id is not None and any(
                window.market_id == market_id for window in self.windows.values()
            ):
                self._taint(market_id, recv_ms, "invalid_book_during_window")
            return
        window = self.windows.get(token)
        if window is None or book is None:
            return
        window.trough_ask_depth = min(
            window.trough_ask_depth,
            book.ask_depth(window.baseline_ask),
        )

    @staticmethod
    def _mid(book: TokenBook) -> float:
        return (max(book.bids) + min(book.asks)) / 2.0

    def _trade(self, event: Mapping[str, Any]) -> None:
        token = str(event.get("asset_id") or "")
        market_id = self.token_market.get(token)
        if market_id is None or market_id in self.locked_markets:
            return
        try:
            recv_ms = float(event["recv_ms"])
            price = float(event["price"])
            size = float(event["size"])
        except (KeyError, TypeError, ValueError):
            return
        if not (math.isfinite(price) and math.isfinite(size) and 0 < price < 1 and size > 0):
            return
        book = self.books.get(token)
        side = (
            book.aggressor_side(price)
            if book is not None and book.valid_at(recv_ms, self.config.book_fresh_ms)
            else None
        )
        if side is None:
            return
        window = self.windows.get(token)
        if window is None:
            window = self._start_window(market_id, token, recv_ms)
            if window is None:
                return
            self.windows[token] = window
        if side == "BUY":
            window.buy_shares += size
            window.buy_prints += 1
            window.highest_buy_price = max(window.highest_buy_price, price)
        else:
            window.sell_shares += size

    def _start_window(self, market_id: str, token: str, recv_ms: float) -> FlowWindow | None:
        market = self.markets.get(market_id)
        book = self.books.get(token)
        if market is None or book is None or not book.valid_at(recv_ms, self.config.book_fresh_ms):
            return None
        remaining_s = market["slot"] + 300 - recv_ms / 1_000.0
        if not self.config.minimum_remaining_s <= remaining_s <= self.config.maximum_remaining_s:
            return None
        baseline_ask = min(book.asks)
        baseline_depth = book.ask_depth(baseline_ask)
        if baseline_depth <= 0:
            return None
        return FlowWindow(
            market_id=market_id,
            asset_id=token,
            start_ms=recv_ms,
            deadline_ms=recv_ms + self.config.window_ms,
            baseline_bid=max(book.bids),
            baseline_ask=baseline_ask,
            baseline_mid=self._mid(book),
            baseline_ask_depth=baseline_depth,
            trough_ask_depth=baseline_depth,
        )

    def _advance(self, recv_ms: float) -> list[dict[str, Any]]:
        due = sorted(
            (window for window in self.windows.values() if window.deadline_ms < recv_ms),
            key=lambda window: (window.deadline_ms, window.market_id, window.asset_id),
        )
        signals: list[dict[str, Any]] = []
        for window in due:
            if self.windows.pop(window.asset_id, None) is None:
                continue
            if window.market_id in self.locked_markets:
                continue
            signal, reason = self._evaluate(window)
            self.outcomes.append({
                "market_id": window.market_id,
                "recv_ms": window.deadline_ms,
                "reason": reason,
                "signal_id": signal.get("signal_id") if signal else None,
            })
            if signal is not None:
                self.locked_markets.add(window.market_id)
                for token in [
                    key for key, other in self.windows.items()
                    if other.market_id == window.market_id
                ]:
                    self.windows.pop(token, None)
                signals.append(signal)
        return signals

    def _evaluate(self, window: FlowWindow) -> tuple[dict[str, Any] | None, str]:
        total = window.buy_shares + window.sell_shares
        buy_fraction = window.buy_shares / total if total > 0 else 0.0
        if window.buy_shares + 1e-12 < self.config.minimum_buy_shares:
            return None, "insufficient_buy_volume"
        if window.buy_prints < self.config.minimum_buy_prints:
            return None, "insufficient_buy_prints"
        if buy_fraction + 1e-12 < self.config.minimum_buy_fraction:
            return None, "weak_trade_sign_imbalance"
        depletion_floor = window.baseline_ask_depth * (1.0 - self.config.visible_depletion_fraction)
        if window.trough_ask_depth <= depletion_floor + 1e-12:
            return None, "visible_depletion"
        if window.buy_shares + 1e-12 < (
            window.baseline_ask_depth * self.config.minimum_consumption_multiple
        ):
            return None, "insufficient_displayed_consumption"
        book = self.books.get(window.asset_id)
        if book is None or not book.valid_at(window.deadline_ms, self.config.book_fresh_ms):
            return None, "stale_or_invalid_book"
        mid_impact = self._mid(book) - window.baseline_mid
        trade_impact = window.highest_buy_price - window.baseline_ask
        if (
            mid_impact > self.config.maximum_mid_impact + 1e-12
            or trade_impact > self.config.maximum_trade_price_impact + 1e-12
        ):
            return None, "price_not_pinned"
        return self._build_signal(window, buy_fraction, mid_impact, trade_impact), "pinned_flow"

    def _build_signal(
        self,
        window: FlowWindow,
        buy_fraction: float,
        mid_impact: float,
        trade_impact: float,
    ) -> dict[str, Any]:
        market = self.markets[window.market_id]
        buy_token = (
            market["down_token_id"]
            if window.asset_id == market["up_token_id"]
            else market["up_token_id"]
        )
        levels, filled, vwap, full = self._direct_buy_levels(buy_token, window.deadline_ms)
        signal_id = (
            f"pinned-flow:{window.market_id}:{round(window.deadline_ms * 1_000)}:{window.asset_id}"
        )
        return {
            "schema": "pinned-flow-signal-v1",
            "signal_id": signal_id,
            "parent_signal_id": None,
            "variant": "base",
            "market_id": window.market_id,
            "swept_asset_id": window.asset_id,
            "buy_asset_id": buy_token,
            "swept_side": self.token_side[window.asset_id],
            "buy_side": self.token_side[buy_token],
            "window_start_ms": window.start_ms,
            "decision_recv_ms": window.deadline_ms,
            "buy_shares": window.buy_shares,
            "sell_shares": window.sell_shares,
            "trade_count": window.buy_prints,
            "buy_fraction": buy_fraction,
            "baseline_bid": window.baseline_bid,
            "baseline_ask": window.baseline_ask,
            "baseline_ask_depth": window.baseline_ask_depth,
            "trough_ask_depth": window.trough_ask_depth,
            "mid_impact": mid_impact,
            "trade_price_impact": trade_impact,
            "target_shares": self.config.target_shares,
            "available_shares": filled,
            "decision_vwap": vwap,
            "fixed_limit": levels[-1]["price"] if full else None,
            "fill_levels": levels,
            "sent": full,
            "reason": "eligible" if full else "insufficient_direct_depth",
            "ordering_clock": "local_receipt_ms",
            "venue_timestamp_role": "audit_only",
        }

    def _direct_buy_levels(
        self,
        buy_token: str,
        decision_ms: float,
    ) -> tuple[list[dict[str, float]], float, float | None, bool]:
        book = self.books.get(buy_token)
        remaining = self.config.target_shares
        levels: list[dict[str, float]] = []
        if book is not None and book.valid_at(decision_ms, self.config.book_fresh_ms):
            for price in sorted(book.asks):
                shares = min(remaining, book.asks[price])
                if shares > 0:
                    levels.append({"price": price, "shares": shares})
                    remaining -= shares
                if remaining <= 1e-12:
                    break
        filled = self.config.target_shares - remaining
        vwap = (
            sum(level["price"] * level["shares"] for level in levels) / filled
            if filled > 0
            else None
        )
        return levels, filled, vwap, remaining <= 1e-12
