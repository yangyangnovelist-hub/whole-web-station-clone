"""Stream the frozen current-H rule over receipt-timestamped raw archives."""
from __future__ import annotations

import argparse
import heapq
import json
import math
from bisect import bisect_right
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import groupby
from pathlib import Path
from statistics import NormalDist
from typing import Any, Iterable, Iterator, Optional

import h_replay as replay
import h_replay_archive as archive


WINDOW_S = 300
TWAP_S = 60
_NORMAL = NormalDist()
DEFAULT_TRIGGER_SOURCES = frozenset({"spot_trade", "futures_book_ticker"})


@dataclass(frozen=True)
class SignalConfig:
    book_lag_ms: float = 106.0
    z0: float = 2.0
    tau_hi_s: float = 240.0
    tau_lo_s: float = 15.0
    reference_age_s: float = 5.0
    anchor_s: float = 2.0
    sigma_window_s: int = 600
    sigma_min_observations: int = 300
    forward_fill_s: int = 10
    book_fresh_s: float = 2.0


def signal_config_from_freeze(frozen: dict[str, Any]) -> SignalConfig:
    timing = frozen["timing"]
    signal = frozen["signal"]
    return SignalConfig(
        book_lag_ms=float(timing["book_trigger_synthetic_lag_ms"]),
        z0=float(signal["z0"]),
        tau_hi_s=float(signal["tau_hi_s"]),
        tau_lo_s=float(signal["tau_lo_s"]),
        reference_age_s=float(signal["reference_age_s"]),
        anchor_s=float(signal["anchor_s"]),
        sigma_window_s=int(signal["sigma_window_s"]),
        sigma_min_observations=int(signal["sigma_min_observations"]),
        forward_fill_s=int(signal["forward_fill_s"]),
        book_fresh_s=float(signal["book_fresh_s"]),
    )


def twap_std_factor(t: float, window: int = WINDOW_S, twap: int = TWAP_S) -> float:
    remaining = min(max(window - t, 0.0), float(twap))
    gap = max(0.0, window - twap - t)
    variance = twap ** 2 * gap + remaining * (remaining + 1) * (2 * remaining + 1) / 6.0
    return math.sqrt(variance) / twap


def twap_move_factor(t: float, window: int = WINDOW_S, twap: int = TWAP_S) -> float:
    """Fraction of the closing TWAP that a new endpoint price can still affect."""
    return min(max(window - t, 0.0), float(twap)) / twap


def fair_price(prior: float, move: float, sigma: float, t_rel: float, direction: int) -> float:
    scale = twap_std_factor(t_rel) * sigma
    if not (math.isfinite(scale) and scale > 0 and math.isfinite(prior) and math.isfinite(move)):
        return math.nan
    effective_move = move * twap_move_factor(t_rel)
    up = _NORMAL.cdf(_NORMAL.inv_cdf(min(max(prior, 0.005), 0.995)) + effective_move / scale)
    return up if direction > 0 else 1.0 - up


class SigmaGrid:
    def __init__(self, window: int, min_n: int, forward_fill_s: int) -> None:
        self.window = window
        self.min_n = min_n
        self.forward_fill_s = forward_fill_s
        self.current_second: Optional[int] = None
        self.current_log_price = math.nan
        self.last_value = math.nan
        self.last_final_second: Optional[int] = None
        self.diffs: deque[float] = deque()
        self.count = 0
        self.total = 0.0
        self.total_sq = 0.0
        self.since_exact = 0

    def _push(self, difference: float) -> None:
        self.diffs.append(difference)
        if math.isfinite(difference):
            self.count += 1
            self.total += difference
            self.total_sq += difference * difference
        if len(self.diffs) > self.window:
            old = self.diffs.popleft()
            if math.isfinite(old):
                self.count -= 1
                self.total -= old
                self.total_sq -= old * old
        self.since_exact += 1
        if self.since_exact >= self.window:
            finite = [value for value in self.diffs if math.isfinite(value)]
            self.count = len(finite)
            self.total = sum(finite)
            self.total_sq = sum(value * value for value in finite)
            self.since_exact = 0

    def _finalise(self, second: int, value: float) -> None:
        difference = value - self.last_value if math.isfinite(value) and math.isfinite(self.last_value) else math.nan
        if self.last_final_second is not None:
            self._push(difference)
        self.last_value = value
        self.last_final_second = second

    def update(self, timestamp_s: float, log_price: float) -> None:
        second = int(math.floor(timestamp_s))
        if self.current_second is None:
            self.current_second = second
            self.current_log_price = log_price
            return
        if second == self.current_second:
            self.current_log_price = log_price
            return
        if second < self.current_second:
            return
        self._finalise(self.current_second, self.current_log_price)
        for empty_second in range(self.current_second + 1, second):
            value = self.current_log_price if empty_second - self.current_second <= self.forward_fill_s else math.nan
            self._finalise(empty_second, value)
        self.current_second = second
        self.current_log_price = log_price

    def sigma(self) -> float:
        if self.count < max(self.min_n, 2):
            return math.nan
        variance = (self.total_sq - self.total * self.total / self.count) / (self.count - 1)
        return math.sqrt(max(variance, 0.0))


@dataclass(frozen=True)
class Candidate:
    slot: int
    timestamp_s: float
    direction: int
    move_1s: float
    sigma: float
    log_price: float
    anchor_log_price: float


class BookTickerTrigger:
    def __init__(self, config: SignalConfig) -> None:
        self.config = config
        self.grid = SigmaGrid(
            config.sigma_window_s,
            config.sigma_min_observations,
            config.forward_fill_s,
        )
        self.timestamps: list[float] = []
        self.log_prices: list[float] = []

    def update_trade(self, source_timestamp_ms: float, price: float) -> None:
        if price > 0:
            self.grid.update(source_timestamp_ms / 1_000.0, math.log(price))

    def _last_at_or_before(self, timestamp_s: float) -> tuple[float, float]:
        index = bisect_right(self.timestamps, timestamp_s) - 1
        if index < 0:
            return math.nan, math.nan
        return self.timestamps[index], self.log_prices[index]

    def update_bbo(self, receive_ms: float, bid: float, ask: float) -> Optional[Candidate]:
        midpoint = (bid + ask) / 2.0
        if not midpoint > 0:
            return None
        timestamp_s = receive_ms / 1_000.0 - self.config.book_lag_ms / 1_000.0
        if self.timestamps and timestamp_s < self.timestamps[-1]:
            timestamp_s = self.timestamps[-1]
        log_price = math.log(midpoint)
        self.timestamps.append(timestamp_s)
        self.log_prices.append(log_price)
        if len(self.timestamps) > 4_096 and self.timestamps[0] < timestamp_s - 10.0:
            cut = bisect_right(self.timestamps, timestamp_s - 10.0)
            del self.timestamps[:cut]
            del self.log_prices[:cut]
        slot = int(timestamp_s // WINDOW_S) * WINDOW_S
        end = slot + WINDOW_S
        if not end - self.config.tau_hi_s <= timestamp_s < end - self.config.tau_lo_s:
            return None
        reference_timestamp, reference_log_price = self._last_at_or_before(timestamp_s - 1.0)
        if not math.isfinite(reference_timestamp) or timestamp_s - reference_timestamp > self.config.reference_age_s:
            return None
        sigma = self.grid.sigma()
        move = log_price - reference_log_price
        if not math.isfinite(sigma) or abs(move) <= self.config.z0 * sigma:
            return None
        _, anchor = self._last_at_or_before(timestamp_s - self.config.anchor_s)
        return Candidate(slot, timestamp_s, 1 if move > 0 else -1, move, sigma, log_price, anchor)


class TimestampedFirstTrigger:
    """Production H source race: spot trades train sigma and race timestamped futures BBO."""

    def __init__(self, config: SignalConfig) -> None:
        self.config = config
        self.grid = SigmaGrid(config.sigma_window_s, config.sigma_min_observations, config.forward_fill_s)
        self.source_timestamps: dict[str, list[float]] = {}
        self.source_log_prices: dict[str, list[float]] = {}
        self.trade_timestamps = self.source_timestamps.setdefault("spot_trade", [])
        self.trade_log_prices = self.source_log_prices.setdefault("spot_trade", [])
        self.futures_timestamps = self.source_timestamps.setdefault("futures_book_ticker", [])
        self.futures_log_prices = self.source_log_prices.setdefault("futures_book_ticker", [])

    def _candidate(self, timestamps: list[float], log_prices: list[float], timestamp_ms: float,
                   price: float) -> Optional[Candidate]:
        if not price > 0:
            return None
        timestamp_s = timestamp_ms / 1_000.0
        if timestamps and timestamp_s < timestamps[-1]:
            return None
        log_price = math.log(price)
        timestamps.append(timestamp_s)
        log_prices.append(log_price)
        if len(timestamps) > 4_096 and timestamps[0] < timestamp_s - 10.0:
            cut = bisect_right(timestamps, timestamp_s - 10.0)
            del timestamps[:cut]
            del log_prices[:cut]
        slot = int(timestamp_s // WINDOW_S) * WINDOW_S
        end = slot + WINDOW_S
        if not end - self.config.tau_hi_s <= timestamp_s < end - self.config.tau_lo_s:
            return None
        reference_index = bisect_right(timestamps, timestamp_s - 1.0) - 1
        if reference_index < 0 or timestamp_s - timestamps[reference_index] > self.config.reference_age_s:
            return None
        sigma = self.grid.sigma()
        move = log_price - log_prices[reference_index]
        if not math.isfinite(sigma) or abs(move) <= self.config.z0 * sigma:
            return None
        anchor_index = bisect_right(timestamps, timestamp_s - self.config.anchor_s) - 1
        anchor = log_prices[anchor_index] if anchor_index >= 0 else math.nan
        return Candidate(slot, timestamp_s, 1 if move > 0 else -1, move, sigma, log_price, anchor)

    def update_trade(self, source_timestamp_ms: float, price: float) -> Optional[Candidate]:
        if price > 0:
            self.grid.update(source_timestamp_ms / 1_000.0, math.log(price))
        return self.update_source("spot_trade", source_timestamp_ms, price)

    def update_futures(self, source_timestamp_ms: float, bid: float, ask: float) -> Optional[Candidate]:
        return self.update_source("futures_book_ticker", source_timestamp_ms, (bid + ask) / 2.0)

    def update_source(self, source: str, source_timestamp_ms: float, price: float) -> Optional[Candidate]:
        timestamps = self.source_timestamps.setdefault(source, [])
        log_prices = self.source_log_prices.setdefault(source, [])
        return self._candidate(timestamps, log_prices, source_timestamp_ms, price)

    def reset_source(self, source: str) -> None:
        self.source_timestamps[source] = []
        self.source_log_prices[source] = []
        if source == "spot_trade":
            self.trade_timestamps = self.source_timestamps[source]
            self.trade_log_prices = self.source_log_prices[source]
        elif source == "futures_book_ticker":
            self.futures_timestamps = self.source_timestamps[source]
            self.futures_log_prices = self.source_log_prices[source]


@dataclass
class TokenBook:
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    source_ms: float = -math.inf
    ready: bool = False
    bid_version: int = 0
    ask_version: int = 0

    def replace(self, bids: Iterable[dict[str, Any]], asks: Iterable[dict[str, Any]], source_ms: float) -> None:
        self.bids = {float(level["price"]): float(level["size"]) for level in bids if float(level["size"]) > 0}
        self.asks = {float(level["price"]): float(level["size"]) for level in asks if float(level["size"]) > 0}
        self.bid_version += 1
        self.ask_version += 1
        self.source_ms = source_ms
        self.ready = True

    def change(
        self,
        side: str,
        price: float,
        size: float,
        source_ms: float,
        best_bid: Any,
        best_ask: Any,
    ) -> bool:
        if not self.ready or source_ms < self.source_ms:
            return False
        buy = side.upper() == "BUY"
        levels = self.bids if buy else self.asks
        previous = levels.get(price)
        if size > 0:
            levels[price] = size
        else:
            levels.pop(price, None)
        if previous != (size if size > 0 else None):
            if buy:
                self.bid_version += 1
            else:
                self.ask_version += 1
        self.source_ms = source_ms
        try:
            venue_bid = float(best_bid)
            venue_ask = float(best_ask)
        except (TypeError, ValueError):
            return True
        if not 0 < venue_bid < venue_ask < 1:
            return True
        local_bid = max(self.bids, default=math.nan)
        local_ask = min(self.asks, default=math.nan)
        if local_bid > venue_bid + 1e-9:
            bid_count = len(self.bids)
            self.bids = {
                level: amount for level, amount in self.bids.items()
                if level <= venue_bid + 1e-9
            }
            self.bid_version += int(len(self.bids) != bid_count)
            local_bid = max(self.bids, default=math.nan)
        if local_ask < venue_ask - 1e-9:
            ask_count = len(self.asks)
            self.asks = {
                level: amount for level, amount in self.asks.items()
                if level >= venue_ask - 1e-9
            }
            self.ask_version += int(len(self.asks) != ask_count)
            local_ask = min(self.asks, default=math.nan)
        if not (math.isclose(local_bid, venue_bid, abs_tol=1e-9)
                and math.isclose(local_ask, venue_ask, abs_tol=1e-9)):
            self.ready = False
        return True

    def ask_levels(self) -> list[list[float]]:
        return [[price, self.asks[price]] for price in sorted(self.asks)]


@dataclass
class DirectMarketBook:
    market_id: str
    slot: int
    up_token_id: str
    down_token_id: str
    up: TokenBook = field(default_factory=TokenBook)
    down: TokenBook = field(default_factory=TokenBook)
    _effective_cache: dict[str, tuple[tuple[int, int, int, int], tuple[tuple[float, float], ...]]] = \
        field(default_factory=dict, repr=False)

    @property
    def ready(self) -> bool:
        return self.up.ready and self.down.ready

    @property
    def source_ms(self) -> float:
        return max(self.up.source_ms, self.down.source_ms)

    @staticmethod
    def _key(price: float) -> int:
        return int(round(price * 10_000))

    def effective_asks(self, direction: str) -> tuple[tuple[float, float], ...]:
        own = self.up if direction == "Up" else self.down
        opposite = self.down if direction == "Up" else self.up
        signature = (id(own), own.ask_version, id(opposite), opposite.bid_version)
        cached = self._effective_cache.get(direction)
        if cached is not None and cached[0] == signature:
            return cached[1]
        levels: dict[int, float] = {}
        for price, size in own.asks.items():
            key = self._key(price)
            levels[key] = max(levels.get(key, 0.0), size)
        for price, size in opposite.bids.items():
            key = 10_000 - self._key(price)
            levels[key] = max(levels.get(key, 0.0), size)
        result = tuple((key / 10_000.0, levels[key]) for key in sorted(levels))
        self._effective_cache[direction] = (signature, result)
        return result

    def midpoint(self, up_asks: Optional[tuple[tuple[float, float], ...]] = None) -> float:
        if not self.ready:
            return math.nan
        direct_bid = self._key(max(self.up.bids)) if self.up.bids else None
        mirrored_bid = 10_000 - self._key(min(self.down.asks)) if self.down.asks else None
        bids = [value for value in (direct_bid, mirrored_bid) if value is not None]
        if up_asks is None:
            direct_ask = self._key(min(self.up.asks)) if self.up.asks else None
            mirrored_ask = 10_000 - self._key(max(self.down.bids)) if self.down.bids else None
            ask_keys = [value for value in (direct_ask, mirrored_ask) if value is not None]
            ask = min(ask_keys) / 10_000.0 if ask_keys else math.nan
        else:
            ask = up_asks[0][0] if up_asks else math.nan
        if not bids or not math.isfinite(ask):
            return math.nan
        bid = max(bids) / 10_000.0
        return (bid + ask) / 2.0 if bid < ask else math.nan


@dataclass(frozen=True)
class RingState:
    source_ms: float
    midpoint: float
    ok: bool


class SourceRing:
    def __init__(self, keep_ms: float = 6_000.0) -> None:
        self.keep_ms = keep_ms
        self.sources: list[float] = []
        self.states: list[RingState] = []

    def push(self, state: RingState) -> None:
        source_ms = max(state.source_ms, self.sources[-1]) if self.sources else state.source_ms
        state = RingState(source_ms, state.midpoint, state.ok)
        self.sources.append(source_ms)
        self.states.append(state)
        cutoff = source_ms - self.keep_ms
        index = bisect_right(self.sources, cutoff) - 1
        if index > 0:
            del self.sources[:index]
            del self.states[:index]

    def state_at(self, source_ms: float) -> Optional[RingState]:
        index = bisect_right(self.sources, source_ms) - 1
        return self.states[index] if index >= 0 else None


def _clob_batches(events: Iterable[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for receive_ms, rows in groupby(events, key=lambda event: float(event["recv_ms"])):
        group = list(rows)
        yield {
            "kind": "clob_batch",
            "recv_ms": receive_ms,
            "seq": int(group[0]["seq"]),
            "events": group,
        }


def _merged_events(
    source_events: Iterable[dict[str, Any]],
    clob_events: Iterable[dict[str, Any]],
) -> Iterator[dict[str, Any]]:
    spot = (((float(event["recv_ms"]), 0, int(event["seq"])), event) for event in source_events)
    clob = (((float(event["recv_ms"]), 1, int(event["seq"])), event) for event in _clob_batches(clob_events))
    for _, event in heapq.merge(spot, clob, key=lambda item: item[0]):
        yield event


def _mapping_dict(rows: Iterable[dict[str, Any]]) -> tuple[dict[int, DirectMarketBook], dict[str, tuple[DirectMarketBook, str]]]:
    markets: dict[int, DirectMarketBook] = {}
    tokens: dict[str, tuple[DirectMarketBook, str]] = {}
    for row in rows:
        market = DirectMarketBook(
            str(row["market_id"]),
            int(row["slot"]),
            str(row["up_token_id"]),
            str(row["down_token_id"]),
        )
        markets[market.slot] = market
        tokens[market.up_token_id] = (market, "Up")
        tokens[market.down_token_id] = (market, "Down")
    return markets, tokens


def _publish_market_book(
    machine: replay.HReplay,
    market: DirectMarketBook,
    receive_ms: float,
) -> None:
    """Synchronize the execution engine only when a decision or evaluation needs full depth."""
    if not market.ready:
        machine.feed({
            "kind": "invalidate",
            "market_id": market.market_id,
            "receive_ms": receive_ms,
            "source_ms": market.source_ms if math.isfinite(market.source_ms) else receive_ms,
            "advance_watermark": False,
        })
        return
    machine.feed({
        "kind": "snapshot",
        "market_id": market.market_id,
        "receive_ms": receive_ms,
        "source_ms": market.source_ms,
        "up_asks": market.effective_asks("Up"),
        "down_asks": market.effective_asks("Down"),
        "levels_normalized": True,
        "advance_watermark": False,
    })


def _apply_clob_batch(
    batch: dict[str, Any],
    machine: replay.HReplay,
    token_index: dict[str, tuple[DirectMarketBook, str]],
    rings: dict[int, SourceRing],
    connected: bool,
    materialize_idle_books: bool = False,
) -> bool:
    receive_ms = float(batch["recv_ms"])
    touched: dict[str, DirectMarketBook] = {}
    source_values = [
        float(event["source_ts_ms"])
        for event in batch["events"]
        if event.get("source_ts_ms") is not None
    ]
    for event in batch["events"]:
        if event["kind"] == "clob_error":
            machine.feed({"kind": "disconnect", "receive_ms": receive_ms})
            for market, _ in token_index.values():
                market.up = TokenBook()
                market.down = TokenBook()
            connected = False
            continue
        if event["kind"] == "clob_connection":
            machine.feed({"kind": "reconnect", "receive_ms": receive_ms})
            connected = True
            continue
        if not connected:
            continue
        hit = token_index.get(str(event.get("asset_id")))
        if hit is None:
            continue
        market, direction = hit
        token = market.up if direction == "Up" else market.down
        source_ms = float(event["source_ts_ms"])
        applied = False
        if event["kind"] == "clob_snapshot":
            if source_ms >= token.source_ms:
                token.replace(event["bids"], event["asks"], source_ms)
                applied = True
        elif event["kind"] == "clob_price_change":
            applied = token.change(
                str(event["side"]),
                float(event["price"]),
                float(event["size"]),
                source_ms,
                event.get("best_bid"),
                event.get("best_ask"),
            )
        if applied:
            touched[market.market_id] = market
    for market in touched.values():
        ring = rings.setdefault(market.slot, SourceRing())
        if market.ready:
            midpoint = market.midpoint()
            ring.push(RingState(market.source_ms, midpoint, math.isfinite(midpoint)))
            if materialize_idle_books or machine.has_pending_evaluations():
                _publish_market_book(machine, market, receive_ms)
        else:
            ring.push(RingState(max(market.source_ms, receive_ms), math.nan, False))
            if materialize_idle_books or machine.has_pending_evaluations():
                _publish_market_book(machine, market, receive_ms)
    if source_values:
        machine.feed({
            "kind": "watermark",
            "receive_ms": receive_ms,
            "source_ms": max(source_values),
        })
    return connected


def _candidate_fair(
    candidate: Candidate,
    ring: Optional[SourceRing],
    config: SignalConfig,
) -> tuple[float, str]:
    if ring is None:
        return math.nan, "no_poly_history"
    source_ms = candidate.timestamp_s * 1_000.0
    current = ring.state_at(source_ms)
    anchor = ring.state_at(source_ms - config.anchor_s * 1_000.0)
    if current is None or not current.ok:
        return math.nan, "book_crossed_or_empty"
    if source_ms - current.source_ms > config.book_fresh_s * 1_000.0:
        return math.nan, "book_stale"
    if anchor is None or not anchor.ok or not math.isfinite(candidate.anchor_log_price):
        return math.nan, "no_anchor"
    fair = fair_price(
        anchor.midpoint,
        candidate.log_price - candidate.anchor_log_price,
        candidate.sigma,
        candidate.timestamp_s - candidate.slot,
        candidate.direction,
    )
    return (fair, "trigger") if math.isfinite(fair) else (math.nan, "no_fair")


def replay_normalized(
    source_events: Iterable[dict[str, Any]],
    clob_events: Iterable[dict[str, Any]],
    mappings: Iterable[dict[str, Any]],
    outcomes: dict[str, str],
    execution_config: replay.ReplayConfig,
    signal_config: SignalConfig,
    trigger_sources: frozenset[str] | set[str] | None = None,
    materialize_idle_books: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    markets, tokens = _mapping_dict(mappings)
    rings: dict[int, SourceRing] = {}
    trigger = TimestampedFirstTrigger(signal_config)
    machine = replay.HReplay(execution_config)
    enabled_sources = DEFAULT_TRIGGER_SOURCES if trigger_sources is None else frozenset(trigger_sources)
    known_sources = {"spot_trade", "futures_book_ticker", "futures_trade", "deribit_quote"}
    unknown_sources = enabled_sources - known_sources
    if unknown_sources:
        raise ValueError(f"unknown trigger sources: {sorted(unknown_sources)}")
    connected = False
    counters: Counter[str] = Counter()
    for event in _merged_events(source_events, clob_events):
        kind = event["kind"]
        if kind == "clob_batch":
            connected = _apply_clob_batch(
                event, machine, tokens, rings, connected,
                materialize_idle_books=materialize_idle_books,
            )
            counters["clob_batches"] += 1
            continue
        candidate = None
        source = None
        source_reset = {
            "spot_connection": "spot_trade", "spot_disconnect": "spot_trade",
            "futures_connection": "futures_book_ticker", "futures_disconnect": "futures_book_ticker",
            "futures_trade_connection": "futures_trade",
            "futures_trade_disconnect": "futures_trade",
            "deribit_connection": "deribit_quote", "deribit_disconnect": "deribit_quote",
        }.get(kind)
        if source_reset is not None:
            trigger.reset_source(source_reset)
            counters[kind] += 1
            continue
        if kind == "spot_trade":
            candidate = trigger.update_trade(float(event["source_ts_ms"]), float(event["price"]))
            counters["spot_trades"] += 1
            source = "spot_trade"
        elif kind == "futures_bbo":
            candidate = trigger.update_futures(float(event["source_ts_ms"]),
                                               float(event["bid"]), float(event["ask"]))
            counters["futures_bbo"] += 1
            source = "futures_book_ticker"
        elif kind == "futures_trade":
            candidate = trigger.update_source("futures_trade", float(event["source_ts_ms"]),
                                              float(event["price"]))
            counters["futures_trades"] += 1
            source = "futures_trade"
        elif kind == "deribit_quote":
            candidate = trigger.update_source("deribit_quote", float(event["source_ts_ms"]),
                                              (float(event["bid"]) + float(event["ask"])) / 2.0)
            counters["deribit_quotes"] += 1
            source = "deribit_quote"
        else:
            counters[kind] += 1
            continue
        if source not in enabled_sources:
            counters[f"candidate_source_disabled_{source}"] += int(candidate is not None)
            continue
        if candidate is None or not connected:
            continue
        market = markets.get(candidate.slot)
        if market is None:
            counters["candidate_no_market"] += 1
            continue
        fair, reason = _candidate_fair(candidate, rings.get(candidate.slot), signal_config)
        counters[f"candidate_{reason}"] += 1
        counters[f"candidate_source_{source}"] += 1
        if reason != "trigger":
            continue
        if not materialize_idle_books:
            _publish_market_book(machine, market, float(event["recv_ms"]))
        machine.feed({
            "kind": "signal",
            "market_id": market.market_id,
            "receive_ms": float(event["recv_ms"]),
            "source_ms": candidate.timestamp_s * 1_000.0,
            "signal_source": source,
            "direction": "Up" if candidate.direction > 0 else "Down",
            "fair": fair,
            "trigger_reason": reason,
            "trial_edge": None,
        })
    machine.finish(force=False)
    counters["censored_evaluations"] += len(machine.pending_evaluations())
    rows = machine.records
    for row in rows:
        winner = outcomes.get(str(row["market_id"]))
        row["winner"] = winner
        row["won"] = winner == row["direction"] if winner is not None else None
        if row["filled"] and winner is not None:
            payout = 1.0 if row["won"] else 0.0
            row["pnl_per_share"] = payout - float(row["all_in_cost"])
            row["pnl"] = row["pnl_per_share"] * float(row["filled_shares"])
            row["day"] = datetime.fromtimestamp(row["signal_ms"] / 1_000.0, timezone.utc).date().isoformat()
        else:
            row["pnl_per_share"] = None
            row["pnl"] = None
            row["day"] = None
    return rows, dict(counters)


def _exact_pvalue(fills: list[dict[str, Any]]) -> float:
    if not fills:
        return 1.0
    probabilities = [min(max(float(row["all_in_cost"]), 0.0), 1.0) for row in fills]
    observed_wins = sum(bool(row["won"]) for row in fills)
    pmf = [0.0] * (len(probabilities) + 1)
    pmf[0] = 1.0
    for index, probability in enumerate(probabilities):
        for wins in range(index + 1, 0, -1):
            pmf[wins] = pmf[wins] * (1.0 - probability) + pmf[wins - 1] * probability
        pmf[0] *= 1.0 - probability
    return min(max(sum(pmf[observed_wins:]), 0.0), 1.0)


def summarize(rows: list[dict[str, Any]], counters: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {"counters": counters, "latencies": {}}
    for latency in sorted({float(row["evaluation_ms"]) for row in rows}):
        sample = [row for row in rows if float(row["evaluation_ms"]) == latency]
        fills = [row for row in sample if row["filled"] and row["winner"] is not None]
        sent = sum(bool(row["sent"]) for row in sample)
        total_shares = sum(float(row["filled_shares"]) for row in fills)
        total_pnl = sum(float(row["pnl"]) for row in fills)
        days = sorted({str(row["day"]) for row in fills})
        reasons = Counter(str(row["reason"]) for row in sample)
        sent_sources = Counter(str(row.get("signal_source") or "unknown") for row in sample if row["sent"])
        fill_sources = Counter(str(row.get("signal_source") or "unknown") for row in fills)
        mean_ev = total_pnl / total_shares if total_shares else None
        wins = sum(bool(row["won"]) for row in fills)
        losses = len(fills) - wins
        output["latencies"][str(int(latency))] = {
            "signals": len(sample),
            "sent": sent,
            "fills": len(fills),
            "filled_shares": total_shares,
            "fill_rate_per_signal": len(fills) / len(sample) if sample else 0.0,
            "fill_rate_per_send": len(fills) / sent if sent else 0.0,
            "net_ev_per_share": mean_ev,
            "pnl": total_pnl,
            "wins": wins,
            "losses": losses,
            "loss_rate": losses / len(fills) if fills else 0.0,
            "days": len(days),
            "day_cluster_lower_99": None,
            "exact_p": _exact_pvalue(fills) if mean_ev is not None and mean_ev > 0 else 1.0,
            "reasons": dict(sorted(reasons.items())),
            "sent_by_source": dict(sorted(sent_sources.items())),
            "fills_by_source": dict(sorted(fill_sources.items())),
        }
    return output


def replay_archive(archive_dir: str | Path, freeze_path: str | Path = replay.FREEZE_PATH,
                   rows_out: str | Path | None = None,
                   trigger_sources: frozenset[str] | set[str] | None = None) -> dict[str, Any]:
    archive_dir = Path(archive_dir)
    frozen = replay.load_freeze(freeze_path)
    execution = replay.config_from_freeze(frozen)
    signal = signal_config_from_freeze(frozen)
    standard = archive_dir / "strict" if (archive_dir / "strict" / "manifest.json").exists() else archive_dir
    manifest_path = standard / "manifest.json"
    if manifest_path.exists():
        manifest = archive.validate_standard_artifact(standard)["manifest"]
        mappings = list(archive.iter_market_mappings(standard / "market_registry.csv.gz"))
        outcomes = {row["market_id"]: row["winner"]
                    for row in archive.iter_outcomes(standard / "market_outcomes.csv.gz")}
        spot = archive.iter_normalized_events(standard / "source_events.jsonl.gz", family="source")
        clob = archive.iter_normalized_events(standard / "clob_events.jsonl.gz", family="clob")
        dataset = {"archive": archive_dir.name, "mapped_markets": len(mappings), "raw_clob_files": 1,
                   "sample_scope": "standard_forward_artifact",
                   "collector_region": manifest.get("collector_region", "unknown"),
                   "paper_gate_eligible": manifest.get("collector_region") == "eu-west-1",
                   "manifest": manifest}
    else:
        mappings = list(archive.iter_market_mappings(archive_dir / "market_registry.csv.gz"))
        outcomes = {row["market_id"]: row["winner"]
                    for row in archive.iter_outcomes(archive_dir / "market_outcomes.csv.gz")}
        spot = archive.iter_spot_events(archive_dir / "feeds_probe.feeds_20261002.jsonl.zst")
        clob_paths = sorted(archive_dir.glob("gh_recorder.clob-btc.*.jsonl.zst"))
        clob = archive.iter_clob_events(clob_paths, error_paths=[archive_dir / "gh_recorder.errors.jsonl.zst"])
        dataset = {"archive": archive_dir.name, "mapped_markets": len(mappings),
                   "raw_clob_files": len(clob_paths), "sample_scope": "single_utc_day_audit",
                   "paper_gate_eligible": False}
    if trigger_sources is None:
        rows, counters = replay_normalized(spot, clob, mappings, outcomes, execution, signal)
    else:
        rows, counters = replay_normalized(
            spot, clob, mappings, outcomes, execution, signal,
            trigger_sources=trigger_sources,
        )
    observation_run_id = str(dataset.get("manifest", {}).get("run_id") or archive_dir.name)
    for row in rows:
        row["observation_run_id"] = observation_run_id
        row["strategy_id"] = str(frozen["strategy_id"])
    if rows_out is not None:
        destination = Path(rows_out)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("w", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
    result = summarize(rows, counters)
    pending_outcomes = sorted({str(row["market_id"]) for row in rows if row.get("winner") is None})
    if dataset["sample_scope"] == "standard_forward_artifact":
        dataset["pending_signal_market_outcomes"] = pending_outcomes
        dataset["paper_gate_eligible"] = bool(dataset["paper_gate_eligible"] and not pending_outcomes)
    result["dataset"] = dataset
    result["protocol"] = {
        "strategy_id": frozen["strategy_id"],
        "scope": frozen["semantics"]["scope"],
        "fill_qualification": frozen["semantics"]["fill_qualification"],
        "endpoint_move_sensitivity": frozen["semantics"]["endpoint_move_sensitivity"],
        "candidate_sources": frozen["semantics"]["candidate_sources"],
        "active_trigger_sources": sorted(DEFAULT_TRIGGER_SOURCES if trigger_sources is None
                                          else trigger_sources),
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", required=True)
    parser.add_argument("--freeze", default=str(replay.FREEZE_PATH))
    parser.add_argument("--require-paper-gate", action="store_true",
                        help="exit nonzero unless every signal-bearing market has an outcome")
    parser.add_argument("--rows-out", help="write replay observations as JSONL for idempotent accumulation")
    parser.add_argument(
        "--trigger-sources",
        default=",".join(sorted(DEFAULT_TRIGGER_SOURCES)),
        help="comma-separated trigger race; choices: spot_trade,futures_book_ticker,futures_trade,deribit_quote",
    )
    args = parser.parse_args()
    trigger_sources = frozenset(source.strip() for source in args.trigger_sources.split(",") if source.strip())
    result = replay_archive(args.archive, args.freeze, args.rows_out, trigger_sources)
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    if args.require_paper_gate and not result["dataset"].get("paper_gate_eligible", False):
        raise SystemExit("strict replay is not paper-gate eligible")


if __name__ == "__main__":
    main()
