"""Receipt-causal raw-CLOB adapter and detector for post-sweep ask absorption."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

import eu_strict


@dataclass(frozen=True)
class AbsorptionConfig:
    max_interprint_ms: float = 50.0
    minimum_burst_shares: float = 5.0
    depletion_deadline_ms: float = 50.0
    depletion_fraction: float = 0.5
    refill_deadline_ms: float = 250.0
    minimum_remaining_s: float = 20.0
    maximum_remaining_s: float = 240.0
    target_shares: float = 5.0
    book_fresh_ms: float = 2_000.0


@dataclass
class TokenBook:
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    recv_ms: float = -math.inf
    ready: bool = False

    @staticmethod
    def _levels(raw: Iterable[Mapping[str, Any]]) -> dict[float, float]:
        levels: dict[float, float] = {}
        for item in raw:
            price = float(item["price"])
            size = float(item["size"])
            if not (math.isfinite(price) and math.isfinite(size) and 0 < price < 1 and size >= 0):
                raise ValueError("invalid direct-CLOB level")
            if size > 0:
                levels[price] = size
        return levels

    def replace(
        self,
        bids: Iterable[Mapping[str, Any]],
        asks: Iterable[Mapping[str, Any]],
        recv_ms: float,
    ) -> None:
        self.bids = self._levels(bids)
        self.asks = self._levels(asks)
        self.recv_ms = recv_ms
        self.ready = self._uncrossed()

    def change(self, side: str, price_raw: Any, size_raw: Any, recv_ms: float) -> None:
        if not self.ready:
            return
        price = float(price_raw)
        size = float(size_raw)
        if not (math.isfinite(price) and math.isfinite(size) and 0 < price < 1 and size >= 0):
            self.ready = False
            return
        if side.upper() == "BUY":
            levels = self.bids
        elif side.upper() == "SELL":
            levels = self.asks
        else:
            self.ready = False
            return
        if size > 0:
            levels[price] = size
        else:
            levels.pop(price, None)
        self.recv_ms = recv_ms
        self.ready = self._uncrossed()

    def _uncrossed(self) -> bool:
        return bool(self.bids and self.asks and max(self.bids) < min(self.asks))

    def valid_at(self, recv_ms: float, fresh_ms: float) -> bool:
        return bool(
            self.ready
            and self._uncrossed()
            and 0 <= recv_ms - self.recv_ms <= fresh_ms
        )

    def ask_depth(self, cap: float) -> float:
        return sum(size for price, size in self.asks.items() if price <= cap + 1e-12)


@dataclass
class Burst:
    market_id: str
    asset_id: str
    start_ms: float
    last_ms: float
    shares: float
    highest_price: float


@dataclass
class Watch:
    signal_id: str
    market_id: str
    asset_id: str
    buy_asset_id: str
    swept_side: str
    buy_side: str
    burst_start_ms: float
    qualify_ms: float
    burst_shares: float
    highest_price: float
    baseline_depth: float
    trough_depth: float
    depleted: bool
    drop_deadline_ms: float
    refill_deadline_ms: float


def _normalized_messages(
    messages: Any,
    *,
    receive_ms: float,
    epoch: int,
    markets: Mapping[str, Mapping[str, Any]],
    tokens: Mapping[str, str],
) -> list[dict[str, Any]]:
    raw_messages = messages if isinstance(messages, list) else [messages]
    normalized: list[dict[str, Any]] = []
    for message in raw_messages:
        if not isinstance(message, Mapping):
            continue
        event_type = message.get("event_type")
        source_ms = eu_strict._clock_ms(message.get("timestamp"))
        if source_ms is None:
            continue
        if event_type == "book":
            asset_id = str(message.get("asset_id") or "")
            market_id = tokens.get(asset_id)
            if market_id is None:
                continue
            normalized.append({
                "kind": "snapshot",
                "recv_ms": receive_ms,
                "source_ts_ms": source_ms,
                "epoch": epoch,
                "market_id": market_id,
                "asset_id": asset_id,
                "bids": message.get("bids") if "bids" in message else message.get("buys") or [],
                "asks": message.get("asks") if "asks" in message else message.get("sells") or [],
            })
        elif event_type == "price_change":
            changes = []
            market_id = None
            for change in message.get("price_changes") or ():
                if not isinstance(change, Mapping):
                    continue
                asset_id = str(change.get("asset_id") or "")
                current_market = tokens.get(asset_id)
                if current_market is None:
                    continue
                if market_id is not None and current_market != market_id:
                    raise ValueError("price-change frame spans multiple markets")
                market_id = current_market
                changes.append({
                    "asset_id": asset_id,
                    "price": change.get("price"),
                    "size": change.get("size"),
                    "side": change.get("side"),
                })
            if changes and market_id is not None:
                normalized.append({
                    "kind": "price_change",
                    "recv_ms": receive_ms,
                    "source_ts_ms": source_ms,
                    "epoch": epoch,
                    "market_id": market_id,
                    "changes": changes,
                })
        elif event_type == "last_trade_price":
            asset_id = str(message.get("asset_id") or "")
            market_id = tokens.get(asset_id)
            if market_id is None:
                continue
            try:
                price = float(message["price"])
                size = float(message["size"])
            except (KeyError, TypeError, ValueError):
                continue
            if not (
                math.isfinite(price)
                and math.isfinite(size)
                and 0 < price < 1
                and size > 0
            ):
                continue
            normalized.append({
                "kind": "trade",
                "recv_ms": receive_ms,
                "source_ts_ms": source_ms,
                "epoch": epoch,
                "market_id": market_id,
                "asset_id": asset_id,
                "price": price,
                "size": size,
                "side": str(message.get("side") or "").upper(),
            })
    by_market: dict[str, list[dict[str, Any]]] = {}
    for event in normalized:
        by_market.setdefault(str(event["market_id"]), []).append(event)
    for market_events in by_market.values():
        snapshot_tokens = {
            str(event["asset_id"])
            for event in market_events
            if event["kind"] == "snapshot"
        }
        atomic_initial_snapshots = (
            len(snapshot_tokens) == len(market_events)
            and len(snapshot_tokens) <= 2
        )
        ambiguous = len(market_events) > 1 and not atomic_initial_snapshots
        for event in market_events:
            event["ambiguous_receipt"] = ambiguous
    return normalized


def iter_raw_clob_events(
    paths: Iterable[Path], segment_end_ms: float,
) -> Iterator[dict[str, Any]]:
    """Yield direct-CLOB lifecycle, L2 and trade events in strict local receipt order."""
    markets: dict[str, dict[str, Any]] = {}
    tokens: dict[str, str] = {}
    active: set[tuple[int, int]] = set()
    physical_sequence: dict[tuple[int, int], int] = {}
    logical_epoch = 0
    last_receive_ns = -1
    for path in sorted(Path(item) for item in paths):
        for line_number, row in eu_strict._iter_json_records(path):
            if not isinstance(row, Mapping) or row.get("rn") is None:
                raise ValueError(f"{path.name}:{line_number}: invalid CLOB envelope")
            receive_ns = int(row["rn"])
            if receive_ns <= last_receive_ns:
                raise ValueError(
                    f"{path.name}:{line_number}: CLOB receive clocks are equal or regressed"
                )
            last_receive_ns = receive_ns
            receive_ms = receive_ns / 1e6
            if row.get("c") == -1 or row.get("k") == "meta":
                before = set(markets)
                eu_strict._market_meta(row, markets, tokens)
                for market_id in sorted(set(markets) - before):
                    market = markets[market_id]
                    yield {
                        "kind": "market",
                        "recv_ms": receive_ms,
                        "market_id": market_id,
                        "slot": int(market["start_ts"]),
                        "up_token_id": str(market["up_token_id"]),
                        "down_token_id": str(market["down_token_id"]),
                    }
                continue
            kind = row.get("k")
            if kind == "error":
                try:
                    connection = (int(row["c"]), int(row["e"]))
                except (KeyError, TypeError, ValueError):
                    continue
                active.discard(connection)
                if not active:
                    yield {
                        "kind": "disconnect",
                        "recv_ms": receive_ms,
                        "epoch": logical_epoch,
                        "reason": "recorder_error",
                    }
                continue
            if kind not in {"connection", "disconnect", "message"}:
                continue
            if logical_epoch == 0 and kind != "connection":
                continue
            try:
                connection = (int(row["c"]), int(row["e"]))
                physical = int(row["s"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"{path.name}:{line_number}: missing CLOB lifecycle") from exc
            if kind != "connection" and connection not in active:
                continue
            previous = physical_sequence.get(connection, -1)
            if physical != previous + 1:
                raise ValueError(f"{path.name}:{line_number}: CLOB physical sequence gap")
            physical_sequence[connection] = physical
            if kind == "connection":
                was_empty = not active
                active.add(connection)
                if was_empty:
                    logical_epoch += 1
                    yield {
                        "kind": "connection",
                        "recv_ms": receive_ms,
                        "epoch": logical_epoch,
                    }
                continue
            if kind == "disconnect":
                active.discard(connection)
                if not active:
                    yield {
                        "kind": "disconnect",
                        "recv_ms": receive_ms,
                        "epoch": logical_epoch,
                        "reason": "all_warm_connections_closed",
                    }
                continue
            yield from _normalized_messages(
                row.get("m"),
                receive_ms=receive_ms,
                epoch=logical_epoch,
                markets=markets,
                tokens=tokens,
            )
    if active and last_receive_ns >= 0:
        yield {
            "kind": "disconnect",
            "recv_ms": segment_end_ms - 0.001,
            "epoch": logical_epoch,
            "reason": "segment_boundary",
        }


class AbsorptionDetector:
    def __init__(self, config: AbsorptionConfig | None = None) -> None:
        self.config = config or AbsorptionConfig()
        self.markets: dict[str, dict[str, Any]] = {}
        self.token_market: dict[str, str] = {}
        self.token_side: dict[str, str] = {}
        self.books: dict[str, TokenBook] = {}
        self.ask_history: dict[str, deque[tuple[float, dict[float, float]]]] = {}
        self.bursts: dict[str, Burst] = {}
        self.watches: dict[str, Watch] = {}
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
            self.ask_history.clear()
            self.bursts.clear()
            self.watches.clear()
            return signals
        if kind == "disconnect":
            self._disconnect(recv_ms)
            return signals
        if not self.connected:
            return signals
        market_id = self._event_market(event)
        if event.get("ambiguous_receipt") is True and market_id is not None:
            self._taint(market_id, recv_ms, "equal_receipt_ambiguity")
            return signals
        if market_id is not None and market_id in self.tainted_markets:
            return signals
        if kind == "snapshot":
            self._snapshot(event)
            signals.extend(self._observe_watch(str(event["market_id"]), recv_ms))
        elif kind == "price_change":
            self._price_change(event)
            signals.extend(self._observe_watch(str(event["market_id"]), recv_ms))
        elif kind == "trade":
            signals.extend(self._trade(event))
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
        market_id = event.get("market_id")
        if market_id is not None:
            return str(market_id)
        asset_id = event.get("asset_id")
        return self.token_market.get(str(asset_id)) if asset_id is not None else None

    def _disconnect(self, recv_ms: float) -> None:
        for market_id in set(self.watches) | {
            burst.market_id for burst in self.bursts.values()
        }:
            self._taint(market_id, recv_ms, "disconnect")
        self.connected = False
        self.books.clear()
        self.ask_history.clear()
        self.bursts.clear()
        self.watches.clear()

    def _taint(self, market_id: str, recv_ms: float, reason: str) -> None:
        self.tainted_markets.add(market_id)
        self.locked_markets.add(market_id)
        self.watches.pop(market_id, None)
        for asset_id in [key for key, burst in self.bursts.items() if burst.market_id == market_id]:
            self.bursts.pop(asset_id, None)
        self.outcomes.append({"market_id": market_id, "recv_ms": recv_ms, "reason": reason})

    def _snapshot(self, event: Mapping[str, Any]) -> None:
        token = str(event["asset_id"])
        book = self.books.setdefault(token, TokenBook())
        try:
            book.replace(event.get("bids") or (), event.get("asks") or (), float(event["recv_ms"]))
        except (KeyError, TypeError, ValueError):
            book.ready = False
        self._record_asks(token, float(event["recv_ms"]))

    def _price_change(self, event: Mapping[str, Any]) -> None:
        recv_ms = float(event["recv_ms"])
        touched: set[str] = set()
        for change in event.get("changes") or ():
            token = str(change.get("asset_id") or "")
            book = self.books.setdefault(token, TokenBook())
            book.change(
                str(change.get("side") or ""),
                change.get("price"),
                change.get("size"),
                recv_ms,
            )
            touched.add(token)
        for token in touched:
            self._record_asks(token, recv_ms)

    def _record_asks(self, token: str, recv_ms: float) -> None:
        book = self.books.get(token)
        if book is None or not book.ready:
            return
        history = self.ask_history.setdefault(token, deque())
        history.append((recv_ms, dict(book.asks)))
        cutoff = recv_ms - max(self.config.book_fresh_ms, 500.0)
        while history and history[0][0] < cutoff:
            history.popleft()

    def _trade(self, event: Mapping[str, Any]) -> list[dict[str, Any]]:
        if str(event.get("side") or "").upper() != "BUY":
            return []
        asset_id = str(event.get("asset_id") or "")
        market_id = self.token_market.get(asset_id)
        if market_id is None or market_id in self.locked_markets:
            return []
        recv_ms = float(event["recv_ms"])
        price = float(event["price"])
        size = float(event["size"])
        burst = self.bursts.get(asset_id)
        if burst is None or recv_ms - burst.last_ms > self.config.max_interprint_ms:
            burst = Burst(
                market_id=market_id,
                asset_id=asset_id,
                start_ms=recv_ms,
                last_ms=recv_ms,
                shares=size,
                highest_price=price,
            )
        else:
            burst.last_ms = recv_ms
            burst.shares += size
            burst.highest_price = max(burst.highest_price, price)
        self.bursts[asset_id] = burst
        if burst.shares + 1e-12 < self.config.minimum_burst_shares:
            return []
        return self._qualify(burst, recv_ms)

    def _qualify(self, burst: Burst, recv_ms: float) -> list[dict[str, Any]]:
        market = self.markets[burst.market_id]
        remaining_s = (market["slot"] + 300) - recv_ms / 1_000.0
        if not (
            self.config.minimum_remaining_s <= remaining_s <= self.config.maximum_remaining_s
        ):
            return []
        self.locked_markets.add(burst.market_id)
        swept_book = self.books.get(burst.asset_id)
        if (
            swept_book is None
            or not swept_book.valid_at(recv_ms, self.config.book_fresh_ms)
            or recv_ms - swept_book.recv_ms > self.config.book_fresh_ms
        ):
            self.outcomes.append({
                "market_id": burst.market_id,
                "recv_ms": recv_ms,
                "reason": "stale_or_invalid_book",
            })
            return []
        history = self.ask_history.get(burst.asset_id, ())
        preburst = [
            (timestamp, sum(
                size for price, size in asks.items()
                if price <= burst.highest_price + 1e-12
            ))
            for timestamp, asks in history
            if burst.start_ms - self.config.max_interprint_ms - 1e-12
            <= timestamp
            <= burst.start_ms + 1e-12
        ]
        baseline_depth = max((depth for _timestamp, depth in preburst), default=0.0)
        if baseline_depth <= 0:
            self.outcomes.append({
                "market_id": burst.market_id,
                "recv_ms": recv_ms,
                "reason": "no_preburst_ask_depth",
            })
            return []
        buy_asset = (
            market["down_token_id"]
            if burst.asset_id == market["up_token_id"]
            else market["up_token_id"]
        )
        observed_depths = [
            sum(
                size for price, size in asks.items()
                if price <= burst.highest_price + 1e-12
            )
            for timestamp, asks in history
            if burst.start_ms - self.config.max_interprint_ms - 1e-12
            <= timestamp
            <= recv_ms + 1e-12
        ]
        current_depth = swept_book.ask_depth(burst.highest_price)
        trough_depth = min([current_depth, *observed_depths])
        depleted = trough_depth <= baseline_depth * (1.0 - self.config.depletion_fraction) + 1e-12
        signal_id = f"absorption:{burst.market_id}:{int(round(recv_ms * 1_000))}:{burst.asset_id}"
        self.watches[burst.market_id] = Watch(
            signal_id=signal_id,
            market_id=burst.market_id,
            asset_id=burst.asset_id,
            buy_asset_id=buy_asset,
            swept_side=self.token_side[burst.asset_id],
            buy_side=self.token_side[buy_asset],
            burst_start_ms=burst.start_ms,
            qualify_ms=recv_ms,
            burst_shares=burst.shares,
            highest_price=burst.highest_price,
            baseline_depth=baseline_depth,
            trough_depth=trough_depth,
            depleted=depleted,
            drop_deadline_ms=recv_ms + self.config.depletion_deadline_ms,
            refill_deadline_ms=recv_ms + self.config.refill_deadline_ms,
        )
        return self._observe_watch(burst.market_id, recv_ms)

    def _observe_watch(self, market_id: str, recv_ms: float) -> list[dict[str, Any]]:
        watch = self.watches.get(market_id)
        if watch is None:
            return []
        swept = self.books.get(watch.asset_id)
        if swept is None or not swept.valid_at(recv_ms, self.config.book_fresh_ms):
            self._taint(market_id, recv_ms, "stale_or_invalid_book")
            return []
        depth = swept.ask_depth(watch.highest_price)
        if recv_ms <= watch.drop_deadline_ms + 1e-12:
            watch.trough_depth = min(watch.trough_depth, depth)
            if depth <= watch.baseline_depth * (1.0 - self.config.depletion_fraction) + 1e-12:
                watch.depleted = True
        if not watch.depleted or recv_ms > watch.refill_deadline_ms + 1e-12:
            return []
        refill = depth - watch.trough_depth
        best_ask = min(swept.asks, default=math.inf)
        if refill + 1e-12 < watch.burst_shares or best_ask > watch.highest_price + 1e-12:
            return []
        signal = self._build_signal(watch, recv_ms, "base", refill)
        self.watches.pop(market_id, None)
        self.outcomes.append({
            "market_id": market_id,
            "recv_ms": recv_ms,
            "reason": "absorption",
            "signal_id": watch.signal_id,
        })
        return [signal]

    def _advance(self, recv_ms: float) -> list[dict[str, Any]]:
        signals: list[dict[str, Any]] = []
        for market_id, watch in list(self.watches.items()):
            if not watch.depleted and watch.drop_deadline_ms < recv_ms:
                self.watches.pop(market_id, None)
                self.outcomes.append({
                    "market_id": market_id,
                    "recv_ms": watch.drop_deadline_ms,
                    "reason": "no_depletion",
                    "signal_id": watch.signal_id,
                })
            elif watch.depleted and watch.refill_deadline_ms < recv_ms:
                self.watches.pop(market_id, None)
                signals.append(
                    self._build_signal(
                        watch,
                        watch.refill_deadline_ms,
                        "no_refill",
                        max(0.0, self._depth(watch) - watch.trough_depth),
                    )
                )
                self.outcomes.append({
                    "market_id": market_id,
                    "recv_ms": watch.refill_deadline_ms,
                    "reason": "no_refill",
                    "signal_id": watch.signal_id,
                })
        return signals

    def _depth(self, watch: Watch) -> float:
        book = self.books.get(watch.asset_id)
        return book.ask_depth(watch.highest_price) if book is not None and book.ready else 0.0

    def _build_signal(
        self,
        watch: Watch,
        decision_ms: float,
        variant: str,
        refill_shares: float,
    ) -> dict[str, Any]:
        swept = self.books.get(watch.asset_id)
        buy = self.books.get(watch.buy_asset_id)
        levels: list[dict[str, float]] = []
        remaining = self.config.target_shares
        if (
            swept is not None
            and buy is not None
            and swept.valid_at(decision_ms, self.config.book_fresh_ms)
            and buy.valid_at(decision_ms, self.config.book_fresh_ms)
        ):
            for price in sorted(buy.asks):
                shares = min(remaining, buy.asks[price])
                if shares > 0:
                    levels.append({"price": price, "shares": shares})
                    remaining -= shares
                if remaining <= 1e-12:
                    break
        filled = self.config.target_shares - remaining
        vwap = (
            sum(item["price"] * item["shares"] for item in levels) / filled
            if filled > 0
            else None
        )
        full = remaining <= 1e-12
        return {
            "schema": "post-sweep-absorption-signal-v1",
            "signal_id": watch.signal_id,
            "parent_signal_id": watch.signal_id,
            "variant": variant,
            "market_id": watch.market_id,
            "swept_asset_id": watch.asset_id,
            "buy_asset_id": watch.buy_asset_id,
            "swept_side": watch.swept_side,
            "buy_side": watch.buy_side,
            "burst_start_ms": watch.burst_start_ms,
            "qualify_recv_ms": watch.qualify_ms,
            "decision_recv_ms": decision_ms,
            "burst_shares": watch.burst_shares,
            "highest_burst_price": watch.highest_price,
            "baseline_depth": watch.baseline_depth,
            "trough_depth": watch.trough_depth,
            "refill_shares": refill_shares,
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
