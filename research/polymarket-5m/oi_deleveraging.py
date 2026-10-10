"""Receipt-causal cross-venue BTC open-interest deleveraging research."""

from __future__ import annotations

import argparse
import heapq
import json
import math
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import groupby
from pathlib import Path
from typing import Any

from absorption import TokenBook, iter_raw_clob_events
import eu_strict
from liquidation_breadth import merge_receipt_streams, replay_execution


SOURCE_ROUTES = {
    "deribit": "deribit_vol",
    "okx": "okx_liq",
    "bybit": "bybit_liq",
    "binance": "bn_fut_mark",
}


def _positive(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def iter_source_events(
    source: str,
    paths: Iterable[str | Path],
    *,
    segment_end_ms: float | None = None,
    diagnostics: dict[str, int] | None = None,
) -> Iterator[dict[str, Any]]:
    """Normalize one recorder route without ordering by venue timestamps."""
    if source not in SOURCE_ROUTES:
        raise ValueError(f"unsupported OI source: {source}")
    audit = diagnostics if diagnostics is not None else {}
    stats = {"skipped_irrelevant_frames": 0}
    active_epoch: int | None = None
    bybit_state: dict[str, float] = {}
    last_recv_ms = -math.inf

    def bump(key: str) -> None:
        audit[key] = audit.get(key, 0) + 1

    parser_role = "okx_oi" if source == "okx" else "oi"
    for receive_ns, route, payload in eu_strict._iter_route(
        [Path(path) for path in paths], SOURCE_ROUTES[source], parser_role, stats
    ):
        recv_ms = receive_ns / 1_000_000.0
        last_recv_ms = recv_ms
        bump("frames_seen")
        marker = payload.get("_recorder")
        if isinstance(marker, Mapping):
            if marker.get("schema") != "recorder-lifecycle-v1":
                bump("invalid_lifecycle_schema")
                continue
            kind = str(marker.get("kind") or "")
            try:
                marker_epoch = int(marker["epoch"])
            except (KeyError, TypeError, ValueError):
                bump("invalid_lifecycle_epoch")
                continue
            if kind == "connection":
                active_epoch = marker_epoch
                bybit_state.clear()
                bump("connections")
                yield {
                    "kind": "source_connection",
                    "source": source,
                    "recv_ms": recv_ms,
                    "recv_ns": receive_ns,
                    "connection_epoch": marker_epoch,
                    "ordering_clock": "local_receipt_ms",
                }
            elif kind == "disconnect" and marker_epoch == active_epoch:
                bump("disconnects")
                yield {
                    "kind": "source_disconnect",
                    "source": source,
                    "recv_ms": recv_ms,
                    "recv_ns": receive_ns,
                    "connection_epoch": marker_epoch,
                    "ordering_clock": "local_receipt_ms",
                }
                active_epoch = None
                bybit_state.clear()
            elif kind not in {"connection", "disconnect"}:
                bump("unknown_lifecycle_kind")
            continue
        if active_epoch is None:
            bump("inactive_epoch_frames")
            continue

        row: dict[str, Any] | None = None
        if source == "deribit":
            params = payload.get("params")
            if not isinstance(params, Mapping):
                bump("irrelevant_frames")
                continue
            channel = str(params.get("channel") or "")
            data = params.get("data")
            if channel != "ticker.BTC-PERPETUAL.agg2" or not isinstance(data, Mapping):
                bump("irrelevant_frames")
                continue
            oi = _positive(data.get("open_interest"))
            source_ms = eu_strict._clock_ms(data.get("timestamp"))
            if oi is not None and source_ms is not None:
                row = {
                    "kind": "oi",
                    "oi_level": oi,
                    "unit": "usd_contracts",
                    "source_ts_ms": source_ms,
                }
        elif source == "okx":
            arg = payload.get("arg")
            data_rows = payload.get("data")
            if (
                not isinstance(arg, Mapping)
                or arg.get("channel") != "open-interest"
                or arg.get("instId") != "BTC-USDT-SWAP"
                or not isinstance(data_rows, list)
            ):
                bump("irrelevant_frames")
                continue
            for data in data_rows:
                if not isinstance(data, Mapping) or data.get("instId") != "BTC-USDT-SWAP":
                    continue
                oi = _positive(data.get("oiCcy"))
                source_ms = eu_strict._clock_ms(data.get("ts"))
                if oi is not None and source_ms is not None:
                    row = {
                        "kind": "oi",
                        "oi_level": oi,
                        "unit": "btc",
                        "source_ts_ms": source_ms,
                    }
                    break
        elif source == "bybit":
            if payload.get("topic") != "tickers.BTCUSDT":
                bump("irrelevant_frames")
                continue
            data = payload.get("data")
            if not isinstance(data, Mapping) or data.get("symbol") not in {None, "BTCUSDT"}:
                bump("invalid_frames")
                continue
            previous_value = bybit_state.get("openInterestValue")
            previous_oi = bybit_state.get("openInterest")
            for key in ("markPrice", "openInterestValue", "openInterest"):
                value = _positive(data.get(key))
                if value is not None:
                    bybit_state[key] = value
            direct_oi = bybit_state.get("openInterest")
            oi_value = bybit_state.get("openInterestValue")
            mark = bybit_state.get("markPrice")
            changed = (
                ("openInterest" in data and direct_oi != previous_oi)
                or ("openInterestValue" in data and oi_value != previous_value)
            )
            source_ms = eu_strict._clock_ms(payload.get("ts"))
            if changed and source_ms is not None and direct_oi is not None:
                row = {
                    "kind": "oi",
                    "oi_level": direct_oi,
                    "unit": "btc",
                    "source_ts_ms": source_ms,
                }
            elif changed and source_ms is not None and oi_value is not None and mark is not None:
                row = {
                    "kind": "oi",
                    "oi_level": oi_value / mark,
                    "unit": "btc_derived_from_value_over_mark",
                    "source_ts_ms": source_ms,
                }
        else:
            data = payload.get("data", payload)
            stream = str(payload.get("stream") or "")
            if (
                not isinstance(data, Mapping)
                or data.get("e") != "markPriceUpdate"
                or (data.get("s") or "").upper() != "BTCUSDT"
                or (stream and stream != "btcusdt@markPrice@1s")
            ):
                bump("irrelevant_frames")
                continue
            price = _positive(data.get("i"))
            source_ms = eu_strict._clock_ms(data.get("E"))
            if price is not None and source_ms is not None:
                row = {
                    "kind": "reference_price",
                    "price": price,
                    "unit": "usdt_per_btc",
                    "source_ts_ms": source_ms,
                }

        if row is None:
            bump("invalid_frames")
            continue
        bump("observations")
        yield {
            **row,
            "source": source,
            "recv_ms": recv_ms,
            "recv_ns": receive_ns,
            "connection_epoch": active_epoch,
            "source_route": route,
            "ordering_clock": "local_receipt_ms",
            "venue_timestamp_role": "audit_only",
        }

    if active_epoch is not None and segment_end_ms is not None and math.isfinite(last_recv_ms):
        disconnect_ms = max(float(segment_end_ms) - 0.001, last_recv_ms + 0.001)
        yield {
            "kind": "source_disconnect",
            "source": source,
            "recv_ms": disconnect_ms,
            "recv_ns": int(disconnect_ms * 1_000_000),
            "connection_epoch": active_epoch,
            "reason": "segment_boundary",
            "ordering_clock": "local_receipt_ms",
        }


def merge_source_events(
    paths_by_source: Mapping[str, Iterable[str | Path]],
    *,
    segment_end_ms: float | None = None,
    diagnostics: dict[str, dict[str, int]] | None = None,
) -> Iterator[dict[str, Any]]:
    """Merge normalized source streams by local receipt and mark exact ties."""

    def ranked(source: str, paths: Iterable[str | Path], rank: int):
        audit = diagnostics.setdefault(source, {}) if diagnostics is not None else None
        previous = -math.inf
        for sequence, row in enumerate(
            iter_source_events(
                source, paths, segment_end_ms=segment_end_ms, diagnostics=audit
            )
        ):
            recv_ms = float(row["recv_ms"])
            if not math.isfinite(recv_ms) or recv_ms < previous:
                raise ValueError(f"{source} receipt clocks must be finite and nondecreasing")
            previous = recv_ms
            yield (recv_ms, rank, sequence), row

    streams = [
        ranked(source, paths_by_source[source], rank)
        for rank, source in enumerate(SOURCE_ROUTES)
        if source in paths_by_source
    ]
    merged = heapq.merge(*streams, key=lambda item: item[0])
    for recv_ms, same_time in groupby(merged, key=lambda item: item[0][0]):
        group = list(same_time)
        ambiguous = len({key[1] for key, _row in group}) > 1
        for _key, row in group:
            yield {
                **row,
                "recv_ms": recv_ms,
                "ambiguous_cross_source_receipt_tie": ambiguous,
            }


@dataclass(frozen=True)
class OiDeleveragingConfig:
    window_ms: float = 10_000.0
    minimum_oi_drop: float = 0.0005
    minimum_price_move: float = 0.0001
    minimum_venues: int = 2
    source_fresh_ms: float = 4_000.0
    price_fresh_ms: float = 2_000.0
    minimum_remaining_s: float = 20.0
    maximum_remaining_s: float = 240.0
    target_shares: float = 5.0
    book_fresh_ms: float = 2_000.0
    time_shift_ms: float = 5_000.0

    def __post_init__(self) -> None:
        positive = (
            self.window_ms,
            self.minimum_oi_drop,
            self.minimum_price_move,
            self.source_fresh_ms,
            self.price_fresh_ms,
            self.target_shares,
            self.book_fresh_ms,
            self.time_shift_ms,
        )
        if any(not math.isfinite(value) or value <= 0 for value in positive):
            raise ValueError("positive OI-deleveraging parameters must be finite")
        if not 2 <= self.minimum_venues <= 3:
            raise ValueError("minimum_venues must be in [2, 3]")
        if not 0 <= self.minimum_remaining_s < self.maximum_remaining_s <= 300:
            raise ValueError("remaining-time window is invalid")


class OiDeleveragingDetector:
    """Emit one cross-venue OI exhaustion signal per five-minute market."""

    OI_SOURCES = ("deribit", "okx", "bybit")
    REQUIRED_SOURCES = frozenset((*OI_SOURCES, "binance"))

    def __init__(self, config: OiDeleveragingConfig | None = None) -> None:
        self.config = config or OiDeleveragingConfig()
        self.markets: dict[str, dict[str, Any]] = {}
        self.books: dict[str, TokenBook] = {}
        self.token_market: dict[str, str] = {}
        self.clob_connected = False
        self.clob_epoch = 0
        self.source_epochs: dict[str, int] = {}
        self.oi_history: dict[str, list[tuple[float, float]]] = {
            source: [] for source in self.OI_SOURCES
        }
        self.price_history: list[tuple[float, float]] = []
        self.base_locked: set[str] = set()
        self.tainted_markets: set[str] = set()
        self.delayed: list[dict[str, Any]] = []
        self.outcomes: list[dict[str, Any]] = []

    def on_event(self, event: Mapping[str, Any]) -> list[dict[str, Any]]:
        recv_ms = float(event.get("recv_ms", -math.inf))
        kind = str(event.get("kind") or "")
        lifecycle = kind in {
            "connection", "disconnect", "source_connection", "source_disconnect",
        }
        signals = [] if lifecycle else self._advance(recv_ms)
        ambiguous = event.get("ambiguous_cross_stream_receipt_tie") is True or event.get(
            "ambiguous_cross_source_receipt_tie"
        ) is True
        if ambiguous and not lifecycle:
            market = self._active_market(recv_ms)
            if market is not None:
                self.tainted_markets.add(str(market["market_id"]))
            return signals
        if kind == "market":
            self._register_market(event)
        elif kind == "connection":
            self.clob_connected = True
            self.clob_epoch = int(event.get("epoch") or self.clob_epoch + 1)
            self.books.clear()
            self.delayed.clear()
        elif kind == "disconnect":
            self.clob_connected = False
            self.books.clear()
            self.delayed.clear()
        elif kind == "source_connection":
            source = str(event.get("source") or "")
            if source in self.REQUIRED_SOURCES:
                self.source_epochs[source] = int(event.get("connection_epoch") or 0)
                self._clear_source(source)
                self.delayed.clear()
        elif kind == "source_disconnect":
            source = str(event.get("source") or "")
            epoch = int(event.get("connection_epoch") or 0)
            if self.source_epochs.get(source) == epoch:
                self.source_epochs.pop(source, None)
                self._clear_source(source)
                self.delayed.clear()
        elif kind == "snapshot":
            self._snapshot(event)
        elif kind == "price_change":
            self._price_change(event)
        elif kind in {"oi", "reference_price"}:
            signals.extend(self._observation(event))
        if ambiguous:
            market = self._active_market(recv_ms)
            if market is not None:
                self.tainted_markets.add(str(market["market_id"]))
        return signals

    def flush(self, recv_ms: float) -> list[dict[str, Any]]:
        return self._advance(float(recv_ms))

    def _clear_source(self, source: str) -> None:
        if source == "binance":
            self.price_history.clear()
        elif source in self.oi_history:
            self.oi_history[source].clear()

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
        if not self.clob_connected or int(event.get("epoch") or self.clob_epoch) != self.clob_epoch:
            return
        market_id = str(event.get("market_id") or "")
        if event.get("ambiguous_receipt") is True:
            self.tainted_markets.add(market_id)
            return
        token = str(event.get("asset_id") or "")
        book = self.books.setdefault(token, TokenBook())
        try:
            book.replace(event.get("bids") or (), event.get("asks") or (), float(event["recv_ms"]))
        except (KeyError, TypeError, ValueError):
            book.ready = False

    def _price_change(self, event: Mapping[str, Any]) -> None:
        if not self.clob_connected or int(event.get("epoch") or self.clob_epoch) != self.clob_epoch:
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

    def _observation(self, event: Mapping[str, Any]) -> list[dict[str, Any]]:
        source = str(event.get("source") or "")
        try:
            recv_ms = float(event["recv_ms"])
            epoch = int(event["connection_epoch"])
        except (KeyError, TypeError, ValueError):
            return []
        if self.source_epochs.get(source) != epoch:
            return []
        if source == "binance" and event.get("kind") == "reference_price":
            value = _positive(event.get("price"))
            target = self.price_history
        elif source in self.OI_SOURCES and event.get("kind") == "oi":
            value = _positive(event.get("oi_level"))
            target = self.oi_history[source]
        else:
            return []
        if value is None:
            return []
        target.append((recv_ms, value))
        oldest = recv_ms - self.config.window_ms - max(
            self.config.source_fresh_ms, self.config.price_fresh_ms
        )
        while len(target) > 1 and target[1][0] < oldest:
            target.pop(0)
        return self._candidate(recv_ms)

    def _active_market(self, recv_ms: float) -> dict[str, Any] | None:
        second = recv_ms / 1_000.0
        candidates = [
            market for market in self.markets.values()
            if self.config.minimum_remaining_s
            <= market["slot"] + 300 - second
            <= self.config.maximum_remaining_s
        ]
        return candidates[0] if len(candidates) == 1 else None

    @staticmethod
    def _window_return(
        rows: list[tuple[float, float]], now_ms: float, window_ms: float, fresh_ms: float
    ) -> float | None:
        if not rows or now_ms - rows[-1][0] > fresh_ms:
            return None
        target_ms = now_ms - window_ms
        # Treat each venue's published OI as a receipt-clock step function:
        # compare the latest state known at the common anchors now and now-10s,
        # never the venues' asynchronous frame-to-frame timestamp intervals.
        before = [row for row in rows if row[0] <= target_ms]
        if not before:
            return None
        baseline = before[-1]
        if target_ms - baseline[0] > fresh_ms:
            return None
        return rows[-1][1] / baseline[1] - 1.0

    def _candidate(self, recv_ms: float) -> list[dict[str, Any]]:
        if not self.clob_connected or set(self.source_epochs) != self.REQUIRED_SOURCES:
            return []
        market = self._active_market(recv_ms)
        if market is None:
            return []
        market_id = str(market["market_id"])
        if market_id in self.base_locked or market_id in self.tainted_markets:
            return []
        price_return = self._window_return(
            self.price_history,
            recv_ms,
            self.config.window_ms,
            self.config.price_fresh_ms,
        )
        if price_return is None or abs(price_return) + 1e-15 < self.config.minimum_price_move:
            return []
        oi_returns = {
            source: value
            for source in self.OI_SOURCES
            if (value := self._window_return(
                self.oi_history[source],
                recv_ms,
                self.config.window_ms,
                self.config.source_fresh_ms,
            )) is not None
        }
        qualifying = sorted(
            source for source, value in oi_returns.items()
            if value <= -self.config.minimum_oi_drop + 1e-15
        )
        if len(qualifying) < self.config.minimum_venues:
            return []
        reversal_side = "Down" if price_return > 0 else "Up"
        base = self._build_signal(
            market, reversal_side, recv_ms, "base", None,
            qualifying, oi_returns, price_return,
        )
        control_side = "Up" if price_return > 0 else "Down"
        control = self._build_signal(
            market, control_side, recv_ms, "direction_control", str(base["signal_id"]),
            qualifying, oi_returns, price_return,
        )
        self.base_locked.add(market_id)
        if base["sent"]:
            self.delayed.append({
                "due_ms": recv_ms + self.config.time_shift_ms,
                "parent": base,
            })
        return [base, control]

    def _advance(self, recv_ms: float) -> list[dict[str, Any]]:
        due = sorted(
            (row for row in self.delayed if float(row["due_ms"]) <= recv_ms + 1e-12),
            key=lambda row: (float(row["due_ms"]), str(row["parent"]["signal_id"])),
        )
        if not due:
            return []
        due_ids = {id(row) for row in due}
        self.delayed = [row for row in self.delayed if id(row) not in due_ids]
        if not self.clob_connected or set(self.source_epochs) != self.REQUIRED_SOURCES:
            return []
        signals: list[dict[str, Any]] = []
        for row in due:
            parent = row["parent"]
            market = self.markets.get(str(parent["market_id"]))
            if market is None or str(market["market_id"]) in self.tainted_markets:
                continue
            shifted = self._build_signal(
                market,
                str(parent["buy_side"]),
                float(row["due_ms"]),
                "time_shift",
                str(parent["signal_id"]),
                list(parent["qualifying_venues"]),
                dict(parent["oi_returns"]),
                float(parent["price_return"]),
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
        parent_signal_id: str | None,
        qualifying: list[str],
        oi_returns: Mapping[str, float],
        price_return: float,
    ) -> dict[str, Any]:
        token = str(market["up_token_id"] if buy_side == "Up" else market["down_token_id"])
        levels, shares, vwap, full = self._direct_levels(token, decision_ms)
        signal_id = f"oi-deleveraging:{variant}:{market['market_id']}:{round(decision_ms * 1_000)}"
        return {
            "schema": "oi-deleveraging-signal-v1",
            "signal_id": signal_id,
            "parent_signal_id": parent_signal_id,
            "variant": variant,
            "market_id": str(market["market_id"]),
            "buy_asset_id": token,
            "buy_side": buy_side,
            "decision_recv_ms": decision_ms,
            "qualifying_venues": qualifying,
            "oi_returns": dict(oi_returns),
            "price_return": price_return,
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
            if filled > 0 else None
        )
        return levels, filled, vwap, remaining <= 1e-12


def summarize_execution(
    signals: Iterable[Mapping[str, Any]], rows: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    signal_rows = list(signals)
    execution_rows = list(rows)
    by_cell: dict[str, dict[str, Any]] = {}
    cells = sorted({
        (str(row.get("variant") or "base"), float(row["evaluation_ms"]))
        for row in execution_rows
    })
    for variant, latency in cells:
        group = [
            row for row in execution_rows
            if str(row.get("variant") or "base") == variant
            and float(row["evaluation_ms"]) == latency
        ]
        sent = [row for row in group if row.get("sent")]
        fills = [row for row in sent if row.get("filled")]
        resolved = [row for row in fills if row.get("pnl") is not None]
        shares = sum(float(row.get("filled_shares") or 0.0) for row in resolved)
        pnl = sum(float(row["pnl"]) for row in resolved)
        by_cell[f"{variant}@{latency:g}ms"] = {
            "evaluations": len(group),
            "sent": len(sent),
            "fills": len(fills),
            "full_fills": sum(bool(row.get("full_fill")) for row in fills),
            "fill_rate": len(fills) / len(sent) if sent else None,
            "filled_shares": shares,
            "resolved_fills": len(resolved),
            "wins": sum(bool(row.get("won")) for row in resolved),
            "pnl_usd": pnl if resolved else None,
            "net_ev_per_share": pnl / shares if shares > 0 else None,
            "reasons": dict(sorted(Counter(str(row.get("reason")) for row in sent).items())),
        }
    return {
        "signals": len(signal_rows),
        "sent_signals": sum(bool(row.get("sent")) for row in signal_rows),
        "signals_by_variant": dict(sorted(Counter(
            str(row.get("variant") or "base") for row in signal_rows
        ).items())),
        "sent_by_variant": dict(sorted(Counter(
            str(row.get("variant") or "base")
            for row in signal_rows if row.get("sent")
        ).items())),
        "execution": by_cell,
    }


def iter_clob_segments(
    segments: Iterable[tuple[Iterable[Path], float]],
) -> Iterator[dict[str, Any]]:
    """Replay contiguous CLOB segments with an explicit disconnect at every gap."""
    for paths, segment_end_ms in segments:
        yield from iter_raw_clob_events(
            paths,
            segment_end_ms,
            preserve_input_order=True,
            allow_leading_carryover=True,
        )


def run_development(
    raw_data: str | Path,
    clob_data: str | Path,
    day: str,
    *,
    clob_start_hour: int = 9,
    source_start_hour: int = 8,
    end_hour: int = 23,
    excluded_clob_hours: Iterable[int] = (),
) -> dict[str, Any]:
    """Run the frozen family on the known development segment only."""
    raw_root = Path(raw_data)
    clob_root = Path(clob_data)
    hours = range(source_start_hour, end_hour + 1)
    source_paths = {
        source: [
            raw_root / f"{route}.{day}T{hour:02d}.txt.gz" for hour in hours
        ]
        for source, route in SOURCE_ROUTES.items()
    }
    excluded = {int(hour) for hour in excluded_clob_hours}
    good_hours = [
        hour for hour in range(clob_start_hour, end_hour + 1)
        if hour not in excluded
    ]
    clob_paths = [
        clob_root / f"poly_clob.{day}T{hour:02d}.jsonl.gz" for hour in good_hours
    ]
    missing = [
        str(path)
        for paths in (*source_paths.values(), clob_paths)
        for path in paths if not path.is_file()
    ]
    if missing:
        raise FileNotFoundError(f"missing development inputs: {missing[:5]}")
    segment_end_ms = (
        datetime.strptime(day, "%Y%m%d").replace(tzinfo=timezone.utc).timestamp()
        + (end_hour + 1) * 3_600
    ) * 1_000
    day_start_ms = datetime.strptime(day, "%Y%m%d").replace(
        tzinfo=timezone.utc
    ).timestamp() * 1_000
    hour_groups: list[list[int]] = []
    for hour in good_hours:
        if not hour_groups or hour != hour_groups[-1][-1] + 1:
            hour_groups.append([hour])
        else:
            hour_groups[-1].append(hour)
    clob_segments = [
        (
            [clob_root / f"poly_clob.{day}T{hour:02d}.jsonl.gz" for hour in group],
            day_start_ms + (group[-1] + 1) * 3_600_000,
        )
        for group in hour_groups
    ]
    diagnostics: dict[str, dict[str, int]] = {}
    detector = OiDeleveragingDetector()
    signals: list[dict[str, Any]] = []
    merged = merge_receipt_streams(
        iter_clob_segments(clob_segments),
        merge_source_events(
            source_paths, segment_end_ms=segment_end_ms, diagnostics=diagnostics
        ),
    )
    for event in merged:
        signals.extend(detector.on_event(event))
    signals.extend(detector.flush(segment_end_ms))

    signaled_market_ids = {str(row["market_id"]) for row in signals}
    outcome_request = {
        market_id: {
            **market,
            "slug": f"btc-updown-5m-{market['slot']}",
            "start_ts": int(market["slot"]),
        }
        for market_id, market in detector.markets.items()
        if market_id in signaled_market_ids
    }
    official = eu_strict.fetch_official_outcomes(outcome_request) if outcome_request else {}
    outcomes = {
        market_id: str(row["winner"]) for market_id, row in official.items()
    }
    execution = replay_execution(
        iter_clob_segments(clob_segments),
        signals,
        outcomes,
        evaluation_ms=(400.0, 500.0),
        recording_end_ms=segment_end_ms,
    )
    return {
        "schema": "oi-deleveraging-development-v1",
        "paper_only": True,
        "sample_role": "development_audit_not_holdout",
        "hypothesis": {
            "window_ms": detector.config.window_ms,
            "minimum_oi_drop": detector.config.minimum_oi_drop,
            "minimum_price_move": detector.config.minimum_price_move,
            "minimum_venues": detector.config.minimum_venues,
            "remaining_seconds": [
                detector.config.maximum_remaining_s,
                detector.config.minimum_remaining_s,
            ],
            "target_shares": detector.config.target_shares,
            "evaluation_ms": [400.0, 500.0],
        },
        "input": {
            "day": day,
            "source_hours": [source_start_hour, end_hour],
            "clob_hours": [clob_start_hour, end_hour],
            "excluded_clob_hours": sorted(excluded),
            "source_files": {
                source: [path.name for path in paths]
                for source, paths in source_paths.items()
            },
            "clob_files": [path.name for path in clob_paths],
        },
        "source_diagnostics": diagnostics,
        "market_count": len(detector.markets),
        "signaled_market_count": len(signaled_market_ids),
        "resolved_market_count": len(outcomes),
        "summary": summarize_execution(signals, execution),
        "signals": signals,
        "execution": execution,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-data", required=True)
    parser.add_argument("--clob-data", required=True)
    parser.add_argument("--day", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--clob-start-hour", type=int, default=9)
    parser.add_argument("--source-start-hour", type=int, default=8)
    parser.add_argument("--end-hour", type=int, default=23)
    parser.add_argument("--exclude-clob-hour", type=int, action="append", default=[])
    args = parser.parse_args()
    result = run_development(
        args.raw_data,
        args.clob_data,
        args.day,
        clob_start_hour=args.clob_start_hour,
        source_start_hour=args.source_start_hour,
        end_hour=args.end_hour,
        excluded_clob_hours=args.exclude_clob_hour,
    )
    path = Path(args.output)
    path.write_text(
        json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result["summary"], sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
