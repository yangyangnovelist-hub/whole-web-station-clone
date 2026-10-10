"""Receipt-causal non-BTC liquidation-breadth research strategy.

Binance's all-market liquidation feed is a one-second per-symbol snapshot, not
an exhaustive trade tape.  This module therefore treats breadth and filled
notional as delayed stress evidence; venue timestamps are audit-only and local
receipt time is the sole decision clock.
"""

from __future__ import annotations

import math
import heapq
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from itertools import groupby
from pathlib import Path
from typing import Any

from absorption import TokenBook
from basis_wedge_run import _fill_direct
import eu_strict


def iter_binance_liquidation_events(
    paths: Iterable[str | Path],
    *,
    segment_end_ms: float | None = None,
    diagnostics: dict[str, int] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield lifecycle-scoped UM non-BTC USDT liquidation snapshots.

    Binance publishes at most the final forced order for each symbol in each
    one-second update window.  Consequently ``z * ap`` below is sampled
    cumulative filled notional, never an estimate of exhaustive liquidation
    volume.  Current live payloads put ``st`` and ``ps`` at event level; the
    nested fallback is retained only so already-recorded legacy files remain
    auditable and is counted explicitly in ``diagnostics``.
    """
    active_epoch: int | None = None
    stats = {"skipped_irrelevant_frames": 0}
    audit = diagnostics if diagnostics is not None else {}

    def bump(key: str) -> None:
        audit[key] = audit.get(key, 0) + 1

    normalized = [Path(path) for path in paths]
    last_recv_ms = -math.inf
    for receive_ns, _route, payload in eu_strict._iter_route(
        normalized, "bn_fut_liq", "liquidation", stats
    ):
        bump("frames_seen")
        recv_ms = receive_ns / 1_000_000.0
        last_recv_ms = recv_ms
        marker = payload.get("_recorder")
        if isinstance(marker, Mapping):
            if marker.get("schema") != "recorder-lifecycle-v1":
                bump("invalid_lifecycle_schema")
                continue
            kind = str(marker.get("kind") or "")
            try:
                epoch = int(marker["epoch"])
            except (KeyError, TypeError, ValueError):
                bump("invalid_lifecycle_epoch")
                continue
            if kind == "connection":
                active_epoch = epoch
                bump("connections")
                yield {
                    "kind": "liquidation_connection",
                    "recv_ms": recv_ms,
                    "recv_ns": receive_ns,
                    "connection_epoch": epoch,
                }
            elif kind == "disconnect" and epoch == active_epoch:
                bump("disconnects")
                yield {
                    "kind": "liquidation_disconnect",
                    "recv_ms": recv_ms,
                    "recv_ns": receive_ns,
                    "connection_epoch": epoch,
                    "reason": "recorder_disconnect",
                }
                active_epoch = None
            elif kind not in {"connection", "disconnect"}:
                bump("unknown_lifecycle_kind")
            continue
        if active_epoch is None:
            bump("inactive_epoch_frames")
            continue
        if payload.get("stream") != "!forceOrder@arr":
            bump("unexpected_stream_frames")
            continue
        data = payload.get("data", payload)
        if not isinstance(data, Mapping) or data.get("e") != "forceOrder":
            bump("invalid_event_schema")
            continue
        order = data.get("o")
        if not isinstance(order, Mapping):
            bump("invalid_order_schema")
            continue
        try:
            symbol = str(order["s"]).upper()
            side = str(order["S"]).upper()
            raw_symbol_type = data.get("st")
            if raw_symbol_type is None:
                raw_symbol_type = order.get("st")
                bump("legacy_nested_product_type_frames")
            symbol_type = int(raw_symbol_type)
            accumulated = float(order["z"])
            average_price = float(order["ap"])
            event_ms = eu_strict._clock_ms(data.get("E"))
            trade_ms = eu_strict._clock_ms(order.get("T"))
        except (KeyError, TypeError, ValueError):
            bump("invalid_required_fields")
            continue
        if (
            symbol_type != 1
            or symbol == "BTCUSDT"
            or not symbol.endswith("USDT")
            or side not in {"BUY", "SELL"}
            or event_ms is None
            or trade_ms is None
        ):
            bump("filtered_product_or_symbol_frames")
            continue
        sampled_notional = accumulated * average_price
        if not (
            math.isfinite(accumulated)
            and math.isfinite(average_price)
            and math.isfinite(event_ms)
            and math.isfinite(trade_ms)
            and accumulated > 0
            and average_price > 0
            and sampled_notional > 0
        ):
            bump("invalid_numeric_fields")
            continue
        bump("yielded_liquidations")
        yield {
            "kind": "liquidation",
            "recv_ms": recv_ms,
            "recv_ns": receive_ns,
            "source_ts_ms": event_ms,
            "trade_ts_ms": trade_ms,
            "symbol": symbol,
            "side": side,
            "filled_quantity": accumulated,
            "average_price": average_price,
            "sampled_cumulative_filled_notional": sampled_notional,
            "filled_notional": sampled_notional,
            "pair": data.get("ps", order.get("ps")),
            "product_type": symbol_type,
            "order_type": order.get("o"),
            "time_in_force": order.get("f"),
            "order_status": order.get("X"),
            "original_quantity": order.get("q"),
            "order_price": order.get("p"),
            "last_filled_quantity": order.get("l"),
            "connection_epoch": active_epoch,
            "source_route": "bn_fut_liq:!forceOrder@arr",
            "ordering_clock": "local_receipt_ms",
            "venue_timestamp_role": "audit_only",
            "feed_semantics": "latest_per_symbol_per_1000ms",
            "raw_payload": payload,
        }
    if active_epoch is not None and segment_end_ms is not None and math.isfinite(last_recv_ms):
        yield {
            "kind": "liquidation_disconnect",
            "recv_ms": float(segment_end_ms) - 0.001,
            "recv_ns": int((float(segment_end_ms) - 0.001) * 1_000_000),
            "connection_epoch": active_epoch,
            "reason": "segment_boundary",
        }


@dataclass(frozen=True)
class LiquidationBreadthConfig:
    window_ms: float = 1_000.0
    minimum_side_symbols: int = 4
    minimum_side_notional: float = 1_000.0
    minimum_side_fraction: float = 2.0 / 3.0
    minimum_remaining_s: float = 20.0
    maximum_remaining_s: float = 240.0
    target_shares: float = 5.0
    book_fresh_ms: float = 2_000.0
    time_shift_ms: float = 5_000.0

    def __post_init__(self) -> None:
        positive = (
            self.window_ms,
            self.minimum_side_notional,
            self.target_shares,
            self.book_fresh_ms,
            self.time_shift_ms,
        )
        if any(not math.isfinite(value) or value <= 0 for value in positive):
            raise ValueError("positive liquidation-breadth parameters must be finite")
        if self.minimum_side_symbols < 2:
            raise ValueError("minimum_side_symbols must be at least two")
        if not 0.5 < self.minimum_side_fraction <= 1:
            raise ValueError("minimum_side_fraction must be in (0.5, 1]")
        if not 0 <= self.minimum_remaining_s < self.maximum_remaining_s <= 300:
            raise ValueError("remaining-time window is invalid")


class LiquidationBreadthDetector:
    """Emit the first broad forced-flow signal in each active five-minute market."""

    def __init__(self, config: LiquidationBreadthConfig | None = None) -> None:
        self.config = config or LiquidationBreadthConfig()
        self.markets: dict[str, dict[str, Any]] = {}
        self.books: dict[str, TokenBook] = {}
        self.token_market: dict[str, str] = {}
        self.clob_connected = False
        self.clob_epoch = 0
        self.liquidation_connected = False
        self.liquidation_epoch = 0
        self.liquidations: list[dict[str, Any]] = []
        self.base_locked: set[str] = set()
        self.short_control_locked: set[str] = set()
        self.tainted_markets: set[str] = set()
        self.delayed: list[dict[str, Any]] = []
        self.outcomes: list[dict[str, Any]] = []

    def on_event(self, event: Mapping[str, Any]) -> list[dict[str, Any]]:
        recv_ms = float(event.get("recv_ms", -math.inf))
        signals = self._advance(recv_ms)
        kind = str(event.get("kind") or "")
        if event.get("ambiguous_cross_stream_receipt_tie") is True and kind in {
            "snapshot",
            "price_change",
            "liquidation",
        }:
            market_id = str(event.get("market_id") or "")
            if not market_id and kind == "liquidation":
                market = self._active_market(recv_ms)
                market_id = str(market["market_id"]) if market is not None else ""
            if market_id:
                self.tainted_markets.add(market_id)
            return signals
        if kind == "market":
            self._register_market(event)
        elif kind == "connection":
            self.clob_connected = True
            self.clob_epoch = int(event.get("epoch") or self.clob_epoch + 1)
            self.books.clear()
        elif kind == "disconnect":
            self.clob_connected = False
            self.books.clear()
            self.liquidations.clear()
            self.delayed.clear()
        elif kind == "liquidation_connection":
            self.liquidation_connected = True
            self.liquidation_epoch = int(
                event.get("connection_epoch") or self.liquidation_epoch + 1
            )
            self.liquidations.clear()
        elif kind == "liquidation_disconnect":
            if int(event.get("connection_epoch") or self.liquidation_epoch) == self.liquidation_epoch:
                self.liquidation_connected = False
                self.liquidations.clear()
                self.delayed.clear()
        elif not (self.clob_connected and self.liquidation_connected):
            return signals
        elif kind == "snapshot":
            self._snapshot(event)
        elif kind == "price_change":
            self._price_change(event)
        elif kind == "liquidation":
            signals.extend(self._liquidation(event))
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

    def _snapshot(self, event: Mapping[str, Any]) -> None:
        if int(event.get("epoch") or self.clob_epoch) != self.clob_epoch:
            return
        market_id = str(event.get("market_id") or "")
        if event.get("ambiguous_receipt") is True:
            self.tainted_markets.add(market_id)
            return
        token = str(event["asset_id"])
        book = self.books.setdefault(token, TokenBook())
        try:
            book.replace(event.get("bids") or (), event.get("asks") or (), float(event["recv_ms"]))
        except (KeyError, TypeError, ValueError):
            book.ready = False

    def _price_change(self, event: Mapping[str, Any]) -> None:
        if int(event.get("epoch") or self.clob_epoch) != self.clob_epoch:
            return
        market_id = str(event.get("market_id") or "")
        if event.get("ambiguous_receipt") is True:
            self.tainted_markets.add(market_id)
            return
        recv_ms = float(event["recv_ms"])
        for change in event.get("changes") or ():
            token = str(change.get("asset_id") or "")
            book = self.books.setdefault(token, TokenBook())
            book.change(
                str(change.get("side") or ""),
                change.get("price"),
                change.get("size"),
                recv_ms,
            )

    def _active_market(self, recv_ms: float) -> dict[str, Any] | None:
        second = recv_ms / 1_000.0
        candidates = [
            market
            for market in self.markets.values()
            if self.config.minimum_remaining_s
            <= market["slot"] + 300 - second
            <= self.config.maximum_remaining_s
        ]
        return candidates[0] if len(candidates) == 1 else None

    def _liquidation(self, event: Mapping[str, Any]) -> list[dict[str, Any]]:
        if int(event.get("connection_epoch") or -1) != self.liquidation_epoch:
            return []
        try:
            recv_ms = float(event["recv_ms"])
            side = str(event["side"]).upper()
            symbol = str(event["symbol"]).upper()
            notional = float(event["filled_notional"])
            source_ms = float(event["source_ts_ms"])
            trade_ms = float(event["trade_ts_ms"])
        except (KeyError, TypeError, ValueError):
            return []
        if (
            side not in {"BUY", "SELL"}
            or symbol == "BTCUSDT"
            or not symbol.endswith("USDT")
            or not all(math.isfinite(value) for value in (recv_ms, notional, source_ms, trade_ms))
            or notional <= 0
        ):
            return []
        self.liquidations.append({
            "recv_ms": recv_ms,
            "symbol": symbol,
            "side": side,
            "notional": notional,
            "source_ts_ms": source_ms,
            "trade_ts_ms": trade_ms,
        })
        cutoff = recv_ms - self.config.window_ms
        self.liquidations = [row for row in self.liquidations if row["recv_ms"] >= cutoff]
        market = self._active_market(recv_ms)
        if market is None or market["market_id"] in self.tainted_markets:
            return []
        signals: list[dict[str, Any]] = []
        if market["market_id"] not in self.base_locked:
            signal, reason = self._candidate(market, "SELL", recv_ms, "base")
            if reason is not None:
                self.outcomes.append({"market_id": market["market_id"], "recv_ms": recv_ms, "reason": reason})
            if signal is not None:
                self.base_locked.add(market["market_id"])
                signals.extend(self._base_and_reversal(signal))
        if market["market_id"] not in self.short_control_locked:
            control, reason = self._candidate(market, "BUY", recv_ms, "short_liquidation")
            if reason is not None:
                self.outcomes.append({"market_id": market["market_id"], "recv_ms": recv_ms, "reason": reason})
            if control is not None:
                self.short_control_locked.add(market["market_id"])
                signals.append(control)
        return signals

    def _candidate(
        self,
        market: Mapping[str, Any],
        side: str,
        recv_ms: float,
        variant: str,
    ) -> tuple[dict[str, Any] | None, str | None]:
        # The public stream is a lossy latest-per-symbol snapshot and carries no
        # order identifier.  Counting two observations for one symbol as two
        # independent liquidations would overstate both amount and side share,
        # so only the latest locally received observation per symbol can enter
        # a decision window.
        latest_by_symbol: dict[str, dict[str, Any]] = {}
        for row in self.liquidations:
            latest_by_symbol[str(row["symbol"])] = row
        sampled_rows = list(latest_by_symbol.values())
        side_rows = [row for row in sampled_rows if row["side"] == side]
        symbols = {row["symbol"] for row in side_rows}
        if len(symbols) < self.config.minimum_side_symbols:
            return None, None
        side_notional = sum(row["notional"] for row in side_rows)
        total_notional = sum(row["notional"] for row in sampled_rows)
        fraction = side_notional / total_notional if total_notional > 0 else 0.0
        if side_notional + 1e-12 < self.config.minimum_side_notional:
            return None, "insufficient_side_notional"
        if fraction + 1e-12 < self.config.minimum_side_fraction:
            return None, "weak_side_fraction"
        latest = max(side_rows, key=lambda row: row["recv_ms"])
        buy_side = "Down" if side == "SELL" else "Up"
        signal = self._build_signal(
            market,
            buy_side,
            recv_ms,
            variant,
            parent_signal_id=None,
            distinct_symbols=len(symbols),
            side_notional=side_notional,
            total_notional=total_notional,
            side_fraction=fraction,
            source_age_ms=recv_ms - latest["source_ts_ms"],
            trade_age_ms=recv_ms - latest["trade_ts_ms"],
        )
        return signal, "qualified"

    def _base_and_reversal(self, base: dict[str, Any]) -> list[dict[str, Any]]:
        reversal = self._build_signal(
            self.markets[str(base["market_id"])],
            "Up",
            float(base["decision_recv_ms"]),
            "direction_reversal",
            parent_signal_id=str(base["signal_id"]),
            distinct_symbols=int(base["distinct_symbols"]),
            side_notional=float(base["side_notional"]),
            total_notional=float(base["total_notional"]),
            side_fraction=float(base["side_fraction"]),
            source_age_ms=float(base["source_age_ms"]),
            trade_age_ms=float(base["trade_age_ms"]),
        )
        if base["sent"]:
            self.delayed.append({
                "due_ms": float(base["decision_recv_ms"]) + self.config.time_shift_ms,
                "parent": base,
            })
        return [base, reversal]

    def _advance(self, recv_ms: float) -> list[dict[str, Any]]:
        due = sorted(
            (
                row
                for row in self.delayed
                if float(row["due_ms"]) <= recv_ms + 1e-12
            ),
            key=lambda row: (float(row["due_ms"]), str(row["parent"]["signal_id"])),
        )
        if not due:
            return []
        due_ids = {id(row) for row in due}
        self.delayed = [row for row in self.delayed if id(row) not in due_ids]
        signals: list[dict[str, Any]] = []
        if not (self.clob_connected and self.liquidation_connected):
            return signals
        for row in due:
            parent = row["parent"]
            market = self.markets.get(str(parent["market_id"]))
            if market is None or market["market_id"] in self.tainted_markets:
                continue
            shifted = self._build_signal(
                market,
                str(parent["buy_side"]),
                float(row["due_ms"]),
                "time_shift",
                parent_signal_id=str(parent["signal_id"]),
                distinct_symbols=int(parent["distinct_symbols"]),
                side_notional=float(parent["side_notional"]),
                total_notional=float(parent["total_notional"]),
                side_fraction=float(parent["side_fraction"]),
                source_age_ms=float(parent["source_age_ms"]) + self.config.time_shift_ms,
                trade_age_ms=float(parent["trade_age_ms"]) + self.config.time_shift_ms,
            )
            shifted["scheduled_decision_recv_ms"] = float(row["due_ms"])
            signals.append(shifted)
        return signals

    def _build_signal(
        self,
        market: Mapping[str, Any],
        buy_side: str,
        decision_ms: float,
        variant: str,
        *,
        parent_signal_id: str | None,
        distinct_symbols: int,
        side_notional: float,
        total_notional: float,
        side_fraction: float,
        source_age_ms: float,
        trade_age_ms: float,
    ) -> dict[str, Any]:
        token = str(market["down_token_id"] if buy_side == "Down" else market["up_token_id"])
        opposite = str(market["up_token_id"] if buy_side == "Down" else market["down_token_id"])
        levels, shares, vwap, full = self._direct_levels(token, decision_ms)
        signal_id = (
            f"liquidation-breadth:{variant}:{market['market_id']}:{round(decision_ms * 1_000)}"
        )
        return {
            "schema": "liquidation-breadth-signal-v1",
            "signal_id": signal_id,
            "parent_signal_id": parent_signal_id,
            "variant": variant,
            "market_id": str(market["market_id"]),
            "buy_asset_id": token,
            "buy_side": buy_side,
            "swept_asset_id": opposite,
            "swept_side": "Up" if buy_side == "Down" else "Down",
            "decision_recv_ms": decision_ms,
            "distinct_symbols": distinct_symbols,
            "side_notional": side_notional,
            "total_notional": total_notional,
            "side_fraction": side_fraction,
            "source_age_ms": source_age_ms,
            "trade_age_ms": trade_age_ms,
            "target_shares": self.config.target_shares,
            "available_shares": shares,
            "decision_vwap": vwap,
            "fixed_limit": levels[-1]["price"] if full else None,
            "fill_levels": levels,
            "sent": full,
            "reason": "eligible" if full else "insufficient_direct_depth",
            "ordering_clock": "local_receipt_ms",
            "venue_timestamp_role": "audit_only",
        }

    def _direct_levels(
        self, token: str, decision_ms: float
    ) -> tuple[list[dict[str, float]], float, float | None, bool]:
        book = self.books.get(token)
        remaining = self.config.target_shares
        levels: list[dict[str, float]] = []
        if book is not None and book.valid_at(decision_ms, self.config.book_fresh_ms):
            for price in sorted(book.asks):
                take = min(remaining, book.asks[price])
                if take > 0:
                    levels.append({"price": price, "shares": take})
                    remaining -= take
                if remaining <= 1e-12:
                    break
        filled = self.config.target_shares - remaining
        vwap = (
            sum(level["price"] * level["shares"] for level in levels) / filled
            if filled > 0
            else None
        )
        return levels, filled, vwap, remaining <= 1e-12


def merge_receipt_streams(
    clob_events: Iterable[Mapping[str, Any]],
    liquidation_events: Iterable[Mapping[str, Any]],
) -> Iterator[dict[str, Any]]:
    """Merge two already ordered streams and fail closed on exact receipt ties."""

    def ranked(
        rows: Iterable[Mapping[str, Any]], rank: int, label: str
    ) -> Iterator[tuple[tuple[float, int, int], Mapping[str, Any]]]:
        previous = -math.inf
        for sequence, row in enumerate(rows):
            recv_ms = float(row["recv_ms"])
            if not math.isfinite(recv_ms) or recv_ms < previous:
                raise ValueError(f"{label} receipt clocks must be finite and nondecreasing")
            previous = recv_ms
            yield (recv_ms, rank, sequence), row

    merged = heapq.merge(
        ranked(clob_events, 0, "CLOB"),
        ranked(liquidation_events, 1, "liquidation"),
        key=lambda item: item[0],
    )
    for recv_ms, same_time in groupby(merged, key=lambda item: item[0][0]):
        group = list(same_time)
        ambiguous = len({key[1] for key, _row in group}) > 1
        for _key, row in group:
            yield {
                **row,
                "recv_ms": recv_ms,
                "ambiguous_cross_stream_receipt_tie": ambiguous,
            }


def replay_execution(
    events: Iterable[Mapping[str, Any]],
    signals: Iterable[Mapping[str, Any]],
    outcomes: Mapping[str, str],
    *,
    evaluation_ms: Iterable[float] = (400.0, 500.0),
    book_fresh_ms: float = 2_000.0,
    fee_rate: float = 0.07,
    recording_end_ms: float | None = None,
) -> list[dict[str, Any]]:
    """Replay fixed-limit direct-depth FAK entries held to binary settlement."""
    latencies = tuple(float(value) for value in evaluation_ms)
    if not latencies or any(not math.isfinite(value) or value < 0 for value in latencies):
        raise ValueError("evaluation_ms must contain non-negative finite values")
    if not math.isfinite(book_fresh_ms) or book_fresh_ms < 0:
        raise ValueError("book_fresh_ms must be non-negative and finite")

    books: dict[str, TokenBook] = {}
    token_market: dict[str, str] = {}
    connected = False
    epoch = 0
    tainted: set[str] = set()
    pending: list[tuple[float, int, float, int, Mapping[str, Any]]] = []
    rows: list[dict[str, Any]] = []
    sequence = 0
    last_event_ms = -math.inf

    def apply_event(event: Mapping[str, Any]) -> None:
        nonlocal connected, epoch, last_event_ms
        recv_ms = float(event["recv_ms"])
        last_event_ms = max(last_event_ms, recv_ms)
        kind = str(event.get("kind") or "")
        if kind == "market":
            market_id = str(event["market_id"])
            token_market[str(event["up_token_id"])] = market_id
            token_market[str(event["down_token_id"])] = market_id
            return
        if kind == "connection":
            connected = True
            epoch = int(event.get("epoch") or epoch + 1)
            books.clear()
            tainted.clear()
            return
        if kind == "disconnect":
            connected = False
            books.clear()
            return
        if not connected or int(event.get("epoch") or epoch) != epoch:
            return
        market_id = str(
            event.get("market_id")
            or token_market.get(str(event.get("asset_id") or ""))
            or ""
        )
        if event.get("ambiguous_receipt") is True or event.get(
            "ambiguous_cross_stream_receipt_tie"
        ) is True:
            if market_id:
                tainted.add(market_id)
            return
        if kind == "snapshot":
            token = str(event["asset_id"])
            book = books.setdefault(token, TokenBook())
            try:
                book.replace(event.get("bids") or (), event.get("asks") or (), recv_ms)
            except (KeyError, TypeError, ValueError):
                book.ready = False
        elif kind == "price_change":
            for change in event.get("changes") or ():
                token = str(change.get("asset_id") or "")
                book = books.setdefault(token, TokenBook())
                book.change(
                    str(change.get("side") or ""),
                    change.get("price"),
                    change.get("size"),
                    recv_ms,
                )

    def evaluate(
        due_ms: float,
        latency: float,
        expected_epoch: int,
        signal: Mapping[str, Any],
        terminal_reason: str | None = None,
    ) -> None:
        market_id = str(signal.get("market_id") or "")
        token = str(signal.get("buy_asset_id") or "")
        book = books.get(token)
        reason = terminal_reason
        if reason is None and not bool(signal.get("sent")):
            reason = str(signal.get("reason") or "not_sent")
        elif reason is None and (not connected or expected_epoch != epoch):
            reason = "disconnect"
        elif reason is None and market_id in tainted:
            reason = "ambiguous_book"
        elif reason is None and (
            book is None or not book.valid_at(due_ms, book_fresh_ms)
        ):
            reason = "stale_book"
        elif reason is None and book is not None and book.recv_ms > due_ms + 1e-9:
            reason = "future_book_guard"

        shares = 0.0
        vwap = fee = notional = None
        levels: tuple[tuple[float, float], ...] = ()
        if reason is None and book is not None:
            reason, shares, vwap, fee, notional, levels = _fill_direct(
                book,
                float(signal["fixed_limit"]),
                float(signal.get("target_shares") or 5.0),
                fee_rate,
            )
        filled = reason in {"filled", "partial_fill"}
        winner = outcomes.get(market_id)
        won = (
            winner.lower() == str(signal.get("buy_side") or "").lower()
            if filled and winner is not None
            else None
        )
        total_cost = (
            float(notional) + float(fee)
            if filled and notional is not None and fee is not None
            else None
        )
        pnl = (
            (shares if won else 0.0) - total_cost
            if total_cost is not None and won is not None
            else (0.0 if not filled else None)
        )
        rows.append({
            "signal_id": signal.get("signal_id"),
            "parent_signal_id": signal.get("parent_signal_id"),
            "variant": signal.get("variant", "base"),
            "paper_only": True,
            "ordering_clock": "local_receipt_ms",
            "market_id": market_id,
            "buy_asset_id": token,
            "buy_side": signal.get("buy_side"),
            "decision_recv_ms": float(signal.get("decision_recv_ms", -math.inf)),
            "evaluation_ms": latency,
            "evaluation_recv_ms": due_ms,
            "book_recv_ms": book.recv_ms if book is not None else None,
            "fixed_limit": signal.get("fixed_limit"),
            "target_shares": float(signal.get("target_shares") or 5.0),
            "filled_shares": shares if filled else 0.0,
            "fill_vwap": vwap if filled else None,
            "fill_levels": [
                {"price": price, "shares": size} for price, size in levels
            ],
            "fee_rate": fee_rate,
            "fee": fee if filled else 0.0,
            "notional": notional if filled else 0.0,
            "total_cost": total_cost,
            "winner": winner,
            "won": won,
            "pnl": pnl,
            "pnl_per_share": pnl / shares if pnl is not None and shares > 0 else None,
            "filled": filled,
            "full_fill": filled and shares + 1e-9 >= float(signal.get("target_shares") or 5.0),
            "sent": bool(signal.get("sent")),
            "reason": reason,
            "signal": dict(signal),
        })

    def drain(limit_ms: float, *, inclusive: bool) -> None:
        while pending:
            due = pending[0][0]
            outside_window = (
                due > limit_ms + 1e-9
                if inclusive
                else due >= limit_ms - 1e-9
            )
            if outside_window:
                break
            due_ms, _seq, latency, expected_epoch, signal = heapq.heappop(pending)
            evaluate(due_ms, latency, expected_epoch, signal)

    event_rows = ((float(row["recv_ms"]), 0, row) for row in events)
    signal_rows = (
        (float(row["decision_recv_ms"]), 1, row) for row in signals
    )
    timeline = heapq.merge(event_rows, signal_rows, key=lambda item: (item[0], item[1]))
    end_limit = float(recording_end_ms) if recording_end_ms is not None else math.inf
    for recv_ms, same_time in groupby(timeline, key=lambda item: item[0]):
        if recv_ms > end_limit + 1e-9:
            break
        drain(recv_ms, inclusive=False)
        group = list(same_time)
        for _clock, kind, payload in group:
            if kind == 0:
                apply_event(payload)
        for _clock, kind, signal in group:
            if kind != 1:
                continue
            for latency in latencies:
                due = recv_ms + latency
                heapq.heappush(pending, (due, sequence, latency, epoch, signal))
                sequence += 1
        drain(recv_ms, inclusive=True)

    end_ms = (
        float(recording_end_ms)
        if recording_end_ms is not None
        else last_event_ms
    )
    drain(end_ms, inclusive=True)
    while pending:
        due_ms, _seq, latency, expected_epoch, signal = heapq.heappop(pending)
        evaluate(due_ms, latency, expected_epoch, signal, "recording_end_censored")
    return sorted(
        rows,
        key=lambda row: (
            float(row["decision_recv_ms"]),
            str(row.get("signal_id") or ""),
            float(row["evaluation_ms"]),
        ),
    )
