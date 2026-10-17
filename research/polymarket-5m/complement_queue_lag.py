"""Paper-only replay of complementary-token queue exhaustion and lagged repricing.

The detector uses only direct Polymarket token books ordered by local receipt.
It never turns the opposite token into synthetic liquidity and never submits an
order.  Previously recorded data is development evidence only.
"""
from __future__ import annotations

import argparse
import gzip
import heapq
import json
import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from decimal import ROUND_DOWN, Decimal
from itertools import groupby
from pathlib import Path
from typing import Any

import h_replay_archive as archive

_EPS = 1e-9
_BATCH_ENVELOPE_FIELDS = frozenset(
    {
        "kind",
        "recv_ms",
        "source_ts_ms",
        "connection_epoch",
        "seq",
        "connection_id",
        "market_id",
        "physical_batch_id",
    }
)
_FEE_QUANTUM = Decimal("0.00001")
_FEE_RATE_072_UNTIL_MS = 1_782_864_000_000


@dataclass(frozen=True)
class QueueLagConfig:
    minimum_queue_shares: float = 5.0
    minimum_midpoint_move: float = 0.01
    maximum_spread_widen: float = 0.0
    confirmation_ms: float = 250.0
    target_shares: float = 5.0
    minimum_total_edge: float = 0.05
    minimum_order_usd: float = 1.0
    minimum_remaining_s: float = 30.0
    maximum_remaining_s: float = 240.0
    book_fresh_ms: float = 2_000.0
    conservative_limit_tick: float = 0.01
    evaluation_ms: tuple[float, ...] = (400.0, 500.0)
    delayed_control_ms: float = 5_000.0


@dataclass
class DirectBook:
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    recv_ms: float = -math.inf
    source_ms: float = -math.inf
    ready: bool = False
    snapshot_required: bool = True

    @staticmethod
    def _levels(raw: Iterable[Mapping[str, Any]]) -> dict[float, float]:
        levels: dict[float, float] = {}
        for item in raw:
            price = float(item["price"])
            size = float(item["size"])
            if not (
                math.isfinite(price)
                and math.isfinite(size)
                and 0 < price < 1
                and size >= 0
            ):
                raise ValueError("invalid direct-book level")
            if size > 0:
                levels[price] = size
        return levels

    def _validate(self) -> None:
        if self.bids and self.asks and max(self.bids) >= min(self.asks):
            self.bids.clear()
            self.asks.clear()
            self.snapshot_required = True
            self.ready = False
            return
        self.ready = bool(
            self.bids
            and self.asks
            and max(self.bids) < min(self.asks)
        )

    def replace(
        self,
        bids: Iterable[Mapping[str, Any]],
        asks: Iterable[Mapping[str, Any]],
        recv_ms: float,
        source_ms: float,
    ) -> bool:
        if source_ms < self.source_ms - _EPS:
            self.bids.clear()
            self.asks.clear()
            self.ready = False
            self.snapshot_required = True
            return False
        self.bids = self._levels(bids)
        self.asks = self._levels(asks)
        self.recv_ms = recv_ms
        self.source_ms = source_ms
        self.snapshot_required = False
        self._validate()
        return True

    def change(
        self,
        side: str,
        price_raw: Any,
        size_raw: Any,
        recv_ms: float,
        source_ms: float,
    ) -> bool:
        if source_ms < self.source_ms - _EPS:
            self.bids.clear()
            self.asks.clear()
            self.ready = False
            self.snapshot_required = True
            return False
        if self.snapshot_required:
            self.recv_ms = recv_ms
            self.source_ms = source_ms
            return True
        try:
            price = float(price_raw)
            size = float(size_raw)
        except (TypeError, ValueError):
            self.ready = False
            return True
        if not (
            math.isfinite(price)
            and math.isfinite(size)
            and 0 < price < 1
            and size >= 0
        ):
            self.ready = False
            return True
        if side.upper() == "BUY":
            levels = self.bids
        elif side.upper() == "SELL":
            levels = self.asks
        else:
            self.ready = False
            return True
        if size > 0:
            levels[price] = size
        else:
            levels.pop(price, None)
        self.recv_ms = recv_ms
        self.source_ms = source_ms
        self._validate()
        return True

    @property
    def best_bid(self) -> float | None:
        return max(self.bids) if self.ready else None

    @property
    def best_ask(self) -> float | None:
        return min(self.asks) if self.ready else None

    @property
    def midpoint(self) -> float | None:
        bid = self.best_bid
        ask = self.best_ask
        return (bid + ask) / 2 if bid is not None and ask is not None else None

    @property
    def spread(self) -> float | None:
        bid = self.best_bid
        ask = self.best_ask
        return ask - bid if bid is not None and ask is not None else None

    def valid_at(self, receive_ms: float, freshness_ms: float) -> bool:
        return bool(
            self.ready
            and 0 <= receive_ms - self.recv_ms <= freshness_ms + _EPS
        )


@dataclass(frozen=True)
class _BookSummary:
    ready: bool
    best_bid: float | None
    best_bid_size: float
    midpoint: float | None
    spread: float | None


@dataclass
class _Market:
    market_id: str
    slot: int
    up_token_id: str
    down_token_id: str
    fee_rate: float
    books: dict[str, DirectBook] = field(default_factory=dict)
    last_update_ms: dict[str, float] = field(default_factory=dict)
    last_update_order: dict[str, int] = field(default_factory=dict)
    consumed: bool = False

    def other(self, asset_id: str) -> str:
        if asset_id == self.up_token_id:
            return self.down_token_id
        if asset_id == self.down_token_id:
            return self.up_token_id
        raise ValueError("asset does not belong to market")

    def side(self, asset_id: str) -> str:
        return "Up" if asset_id == self.up_token_id else "Down"

    def reset(self) -> None:
        self.books.clear()
        self.last_update_ms.clear()
        self.last_update_order.clear()


@dataclass(frozen=True)
class _Watch:
    market_id: str
    epoch: int
    exhausted_asset_id: str
    buy_asset_id: str
    exhausted_ms: float
    exhausted_source_ms: float
    exhausted_order: int
    deadline_ms: float
    fair: float
    pre_best_bid: float
    pre_best_bid_size: float
    pre_midpoint: float
    post_midpoint: float


@dataclass(frozen=True)
class _Signal:
    signal_id: str
    parent_signal_id: str | None
    variant: str
    market_id: str
    epoch: int
    decision_ms: float
    remaining_s: float
    exhausted_ms: float
    exhausted_source_ms: float
    exhausted_asset_id: str
    buy_asset_id: str
    buy_side: str
    fair: float
    decision_source_ms: float
    decision_vwap: float
    decision_levels: tuple[tuple[float, float], ...]
    fixed_limit: float
    decision_fee: float
    net_edge: float
    pre_best_bid: float
    pre_best_bid_size: float
    pre_midpoint: float
    post_midpoint: float


@dataclass(frozen=True)
class _Pending:
    due_ms: float
    sequence: int
    evaluation_ms: float
    signal: _Signal


def _summary(book: DirectBook | None) -> _BookSummary:
    if book is None:
        return _BookSummary(False, None, 0.0, None, None)
    bid = book.best_bid
    return _BookSummary(
        book.ready,
        bid,
        float(book.bids.get(bid, 0.0)) if bid is not None else 0.0,
        book.midpoint,
        book.spread,
    )


def _charged_fee(levels: Sequence[tuple[float, float]], fee_rate: float) -> float:
    rate = Decimal(str(fee_rate))
    raw = sum(
        (
            Decimal(str(shares)) * rate * Decimal(str(price))
            * (Decimal(1) - Decimal(str(price)))
            for price, shares in levels
        ),
        Decimal(0),
    )
    return float(raw.quantize(_FEE_QUANTUM, rounding=ROUND_DOWN))


def _take_levels(
    book: DirectBook,
    shares: float,
    *,
    limit: float | None = None,
) -> tuple[float, float, tuple[tuple[float, float], ...]]:
    remaining = shares
    notional = 0.0
    fills: list[tuple[float, float]] = []
    for price, available in sorted(book.asks.items()):
        if limit is not None and price > limit + _EPS:
            break
        take = min(remaining, available)
        if take <= _EPS:
            continue
        fills.append((price, take))
        notional += price * take
        remaining -= take
        if remaining <= _EPS:
            break
    return shares - remaining, notional, tuple(fills)


def _market_maps(
    mappings: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, _Market], dict[str, _Market]]:
    markets: dict[str, _Market] = {}
    tokens: dict[str, _Market] = {}
    for row in mappings:
        slot = int(row["slot"])
        raw_fee_rate = row.get("fee_rate")
        fee_rate = (
            float(raw_fee_rate)
            if raw_fee_rate not in (None, "")
            else (0.072 if slot * 1_000 < _FEE_RATE_072_UNTIL_MS else 0.07)
        )
        market = _Market(
            str(row["market_id"]),
            slot,
            str(row["up_token_id"]),
            str(row["down_token_id"]),
            fee_rate,
        )
        if market.market_id in markets:
            raise ValueError(f"duplicate market: {market.market_id}")
        if (
            market.up_token_id == market.down_token_id
            or market.up_token_id in tokens
            or market.down_token_id in tokens
        ):
            raise ValueError(f"duplicate token mapping: {market.market_id}")
        markets[market.market_id] = market
        tokens[market.up_token_id] = market
        tokens[market.down_token_id] = market
    return markets, tokens


def iter_physical_clob_events(path: str | Path) -> Iterable[dict[str, Any]]:
    """Expand strict rows while preserving each venue frame as a separate batch.

    The shared canonical reader intentionally exposes logical price changes.  A
    lead/lag test additionally needs the physical-frame boundary so two frames
    89 ns apart cannot collapse into one float-millisecond batch.
    """
    source = Path(path)
    opener = gzip.open if source.suffix == ".gz" else open
    previous_recv_ms = -math.inf
    expected_sequence = 0
    with opener(source, "rt", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                physical = json.loads(line)
                kind = str(physical["kind"])
                receive_ms = float(physical["recv_ms"])
                base_sequence = int(physical["seq"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"{source.name}:{line_number}: invalid strict CLOB row") from exc
            if receive_ms < previous_recv_ms - _EPS:
                raise ValueError(f"{source.name}:{line_number}: receipt clock regressed")
            if base_sequence != expected_sequence:
                raise ValueError(f"{source.name}:{line_number}: non-contiguous strict sequence")
            previous_recv_ms = receive_ms
            supplied_batch_id = physical.get("physical_batch_id")
            batch_id = str(supplied_batch_id or f"{line_number}:{base_sequence}")
            if kind != "clob_price_change_batch":
                if kind == "clob_price_change" and supplied_batch_id is None:
                    raise ValueError(
                        f"{source.name}:{line_number}: missing physical_batch_id"
                    )
                expected_sequence += 1
                yield {**physical, "physical_batch_id": batch_id}
                continue
            changes = physical.get("changes")
            if not isinstance(changes, list) or not changes:
                raise ValueError(f"{source.name}:{line_number}: empty price-change batch")
            for offset, change in enumerate(changes):
                if not isinstance(change, Mapping):
                    raise TypeError(f"{source.name}:{line_number}: invalid price-change item")
                protected = _BATCH_ENVELOPE_FIELDS & change.keys()
                if protected:
                    raise ValueError(
                        f"{source.name}:{line_number}: nested change overrides "
                        f"protected batch field(s): {', '.join(sorted(protected))}"
                    )
                yield {
                    "kind": "clob_price_change",
                    "recv_ms": receive_ms,
                    "source_ts_ms": physical.get("source_ts_ms"),
                    "connection_epoch": physical.get("connection_epoch"),
                    "seq": base_sequence + offset,
                    "connection_id": physical.get("connection_id"),
                    "market_id": physical.get("market_id"),
                    "physical_batch_id": batch_id,
                    **change,
                }
            expected_sequence += len(changes)


def replay(
    clob_events: Iterable[Mapping[str, Any]],
    mappings: Iterable[Mapping[str, Any]],
    outcomes: Mapping[str, str],
    *,
    segment_end_ms: float,
    config: QueueLagConfig | None = None,
) -> tuple[list[dict[str, Any]], Counter[str]]:
    """Detect, execute and settlement-score the frozen development protocol."""
    cfg = config or QueueLagConfig()
    if any(not math.isfinite(value) or value < 0 for value in cfg.evaluation_ms):
        raise ValueError("evaluation latencies must be finite and nonnegative")
    markets, tokens = _market_maps(mappings)
    counters: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    pending: list[tuple[float, int, _Pending]] = []
    watches: dict[str, _Watch] = {}
    connected = False
    epoch = 0
    sequence = 0
    signal_sequence = 0
    batch_order = 0
    last_recv_ms = -math.inf

    def reset_books() -> None:
        for state in markets.values():
            state.reset()
        watches.clear()

    def schedule(signal: _Signal) -> None:
        nonlocal sequence
        for latency in cfg.evaluation_ms:
            sequence += 1
            item = _Pending(signal.decision_ms + latency, sequence, latency, signal)
            heapq.heappush(pending, (item.due_ms, item.sequence, item))

    def decision(
        state: _Market,
        watch: _Watch,
        variant: str,
        decision_ms: float,
        *,
        parent: _Signal | None = None,
    ) -> _Signal | None:
        nonlocal signal_sequence
        book = state.books.get(watch.buy_asset_id)
        if book is None or not book.valid_at(decision_ms, cfg.book_fresh_ms):
            counters["decision_book_unavailable"] += 1
            return None
        filled, notional, levels = _take_levels(book, cfg.target_shares)
        if filled < cfg.target_shares - _EPS:
            counters["insufficient_decision_depth"] += 1
            return None
        fixed_limit = levels[-1][0]
        limit_units = fixed_limit / cfg.conservative_limit_tick
        if not math.isclose(
            limit_units, round(limit_units), rel_tol=0.0, abs_tol=_EPS,
        ):
            counters["invalid_limit_precision"] += 1
            return None
        if fixed_limit * cfg.target_shares < cfg.minimum_order_usd - _EPS:
            counters["below_venue_minimum"] += 1
            return None
        fee = _charged_fee(levels, state.fee_rate)
        edge = watch.fair * cfg.target_shares - notional - fee
        if edge < cfg.minimum_total_edge - _EPS:
            counters["insufficient_edge"] += 1
            return None
        signal_sequence += 1
        signal = _Signal(
            signal_id=f"{state.market_id}:{variant}:{signal_sequence}",
            parent_signal_id=parent.signal_id if parent is not None else None,
            variant=variant,
            market_id=state.market_id,
            epoch=watch.epoch,
            decision_ms=decision_ms,
            remaining_s=state.slot + 300.0 - decision_ms / 1_000.0,
            exhausted_ms=watch.exhausted_ms,
            exhausted_source_ms=watch.exhausted_source_ms,
            exhausted_asset_id=watch.exhausted_asset_id,
            buy_asset_id=watch.buy_asset_id,
            buy_side=state.side(watch.buy_asset_id),
            fair=watch.fair,
            decision_source_ms=book.source_ms,
            decision_vwap=notional / cfg.target_shares,
            decision_levels=levels,
            fixed_limit=fixed_limit,
            decision_fee=fee,
            net_edge=edge,
            pre_best_bid=watch.pre_best_bid,
            pre_best_bid_size=watch.pre_best_bid_size,
            pre_midpoint=watch.pre_midpoint,
            post_midpoint=watch.post_midpoint,
        )
        counters[f"{variant}_signals"] += 1
        schedule(signal)
        return signal

    def execute(item: _Pending, forced_reason: str | None = None) -> None:
        signal = item.signal
        state = markets[signal.market_id]
        book = state.books.get(signal.buy_asset_id)
        reason = forced_reason
        filled = 0.0
        notional = 0.0
        levels: tuple[tuple[float, float], ...] = ()
        fee = None
        if reason is None and (not connected or signal.epoch != epoch):
            reason = "disconnect"
        elif reason is None and (
            book is None or not book.valid_at(item.due_ms, cfg.book_fresh_ms)
        ):
            reason = "book_unavailable"
        elif reason is None:
            assert book is not None
            filled, notional, levels = _take_levels(
                book, cfg.target_shares, limit=signal.fixed_limit,
            )
            if filled <= _EPS:
                reason = "ask_above_frozen_limit"
            elif filled < cfg.target_shares - _EPS:
                reason = "partial_fill"
            else:
                reason = "filled"
            fee = _charged_fee(levels, state.fee_rate)
        did_fill = filled > _EPS
        winner = outcomes.get(signal.market_id)
        won = signal.buy_side == winner if did_fill and winner is not None else None
        total_cost = notional + fee if did_fill and fee is not None else None
        pnl = (
            (filled if won else 0.0) - total_cost
            if total_cost is not None and won is not None
            else None
        )
        rows.append({
            **asdict(signal),
            "decision_levels": [
                {"price": price, "shares": shares}
                for price, shares in signal.decision_levels
            ],
            "paper_only": True,
            "ordering_clock": "local_receipt_ms",
            "match_book_clock": "local_receipt_ms",
            "evaluation_ms": item.evaluation_ms,
            "evaluation_recv_ms": item.due_ms,
            "book_recv_ms": book.recv_ms if book is not None and math.isfinite(book.recv_ms) else None,
            "book_source_ms": (
                book.source_ms
                if book is not None and math.isfinite(book.source_ms)
                else None
            ),
            "target_shares": cfg.target_shares,
            "filled_shares": filled,
            "fill_vwap": notional / filled if did_fill else None,
            "fill_levels": [
                {"price": price, "shares": shares} for price, shares in levels
            ],
            "fee_rate": state.fee_rate,
            "fee": fee,
            "total_cost": total_cost,
            "winner": winner,
            "won": won,
            "pnl_per_share": pnl / filled if pnl is not None and did_fill else None,
            "pnl": pnl,
            "filled": did_fill,
            "full_fill": reason == "filled",
            "sent": reason != "right_censored",
            "reason": reason,
        })
        counters[f"execution_{reason}"] += 1

    def drain_before(receive_ms: float) -> None:
        while pending and pending[0][0] < receive_ms - _EPS:
            _due, _seq, pending_item = heapq.heappop(pending)
            execute(pending_item)

    def fail_equal(receive_ms: float) -> None:
        while pending and math.isclose(
            pending[0][0], receive_ms, rel_tol=0.0, abs_tol=_EPS,
        ):
            _due, _seq, item = heapq.heappop(pending)
            execute(item, "equal_receipt_ambiguous")

    def physical_key(row: Mapping[str, Any]) -> object:
        return row.get("physical_batch_id", ("synthetic-receipt", float(row["recv_ms"])))

    grouped = groupby(clob_events, key=physical_key)
    for _physical_batch_id, raw_group in grouped:
        group = list(raw_group)
        receive_ms = float(group[0]["recv_ms"])
        if any(
            not math.isclose(
                float(row["recv_ms"]), receive_ms, rel_tol=0.0, abs_tol=_EPS,
            )
            for row in group
        ):
            raise ValueError("physical CLOB batch spans multiple receipt clocks")
        batch_order += 1
        if receive_ms < last_recv_ms - _EPS:
            raise ValueError("CLOB receipt clock regressed")
        if receive_ms > segment_end_ms + _EPS:
            raise ValueError("CLOB event exceeds declared segment end")
        last_recv_ms = receive_ms
        drain_before(receive_ms)
        fail_equal(receive_ms)

        for market_id, watch in list(watches.items()):
            if receive_ms > watch.deadline_ms + _EPS:
                counters["confirmation_too_late"] += 1
                del watches[market_id]

        lifecycle = [str(row["kind"]) for row in group if str(row["kind"]) in {
            "clob_connection", "clob_error",
        }]
        if lifecycle:
            if len(group) != 1:
                raise ValueError("lifecycle event shares a physical receipt batch")
            row = group[0]
            row_epoch = int(row.get("connection_epoch") or 0)
            if row["kind"] == "clob_connection":
                if connected or row_epoch <= epoch:
                    raise ValueError("invalid CLOB connection epoch")
                connected = True
                epoch = row_epoch
                reset_books()
            else:
                connected = False
                reset_books()
            continue
        if not connected:
            counters["outside_connection"] += len(group)
            continue
        if any(int(row.get("connection_epoch") or 0) != epoch for row in group):
            raise ValueError("CLOB event epoch drift")

        touched: dict[str, set[str]] = {}
        prior: dict[tuple[str, str], _BookSummary] = {}
        reported_bbo: dict[
            tuple[str, str], tuple[float | None, float | None]
        ] = {}
        explicit_zero: set[tuple[str, str, float]] = set()
        regressed_markets: set[str] = set()
        for row in group:
            asset_id = str(row.get("asset_id") or "")
            state = tokens.get(asset_id)
            if state is None:
                continue
            prior.setdefault((state.market_id, asset_id), _summary(state.books.get(asset_id)))
            touched.setdefault(state.market_id, set()).add(asset_id)
            book = state.books.setdefault(asset_id, DirectBook())
            source_ms = float(row["source_ts_ms"])
            if row["kind"] == "clob_snapshot":
                applied = book.replace(
                    row.get("bids") or (), row.get("asks") or (), receive_ms, source_ms,
                )
            elif row["kind"] == "clob_price_change":
                side = str(row.get("side") or "")
                price = float(row["price"])
                size = float(row["size"])
                if side.upper() == "BUY" and size == 0:
                    explicit_zero.add((state.market_id, asset_id, price))
                applied = book.change(side, price, size, receive_ms, source_ms)
                if "best_bid" in row or "best_ask" in row:
                    venue_bid = (
                        float(row["best_bid"]) if "best_bid" in row else None
                    )
                    venue_ask = (
                        float(row["best_ask"]) if "best_ask" in row else None
                    )
                    if (
                        venue_bid is not None
                        and (not math.isfinite(venue_bid) or not 0 < venue_bid < 1)
                    ) or (
                        venue_ask is not None
                        and (not math.isfinite(venue_ask) or not 0 < venue_ask < 1)
                    ) or (
                        venue_bid is not None
                        and venue_ask is not None
                        and venue_bid >= venue_ask
                    ):
                        raise ValueError("invalid venue BBO")
                    key = (state.market_id, asset_id)
                    previous_bid, previous_ask = reported_bbo.get(key, (None, None))
                    if (
                        venue_bid is not None
                        and previous_bid is not None
                        and venue_bid != previous_bid
                    ) or (
                        venue_ask is not None
                        and previous_ask is not None
                        and venue_ask != previous_ask
                    ):
                        raise ValueError("inconsistent venue BBO within physical batch")
                    reported_bbo[key] = (
                        venue_bid if venue_bid is not None else previous_bid,
                        venue_ask if venue_ask is not None else previous_ask,
                    )
            else:
                raise ValueError(f"unexpected CLOB event kind: {row['kind']}")
            if not applied:
                counters["source_time_regression"] += 1
                regressed_markets.add(state.market_id)
            state.last_update_ms[asset_id] = receive_ms
            state.last_update_order[asset_id] = batch_order

        for (market_id, asset_id), (venue_bid, venue_ask) in reported_bbo.items():
            book = markets[market_id].books[asset_id]
            local_bid = book.best_bid
            local_ask = book.best_ask
            bid_matches = venue_bid is None or (
                local_bid is not None
                and math.isclose(local_bid, venue_bid, rel_tol=0.0, abs_tol=_EPS)
            )
            ask_matches = venue_ask is None or (
                local_ask is not None
                and math.isclose(local_ask, venue_ask, rel_tol=0.0, abs_tol=_EPS)
            )
            if not (bid_matches and ask_matches):
                book.bids.clear()
                book.asks.clear()
                book.ready = False
                book.snapshot_required = True
                counters["venue_bbo_mismatch"] += 1
                regressed_markets.add(market_id)

        for market_id in regressed_markets:
            watches.pop(market_id, None)

        # A pending base watch resolves only on the first later complementary update.
        for market_id, watch in list(watches.items()):
            assets = touched.get(market_id, set())
            if not assets:
                continue
            if watch.buy_asset_id in assets:
                state = markets[market_id]
                signal = decision(state, watch, "base", receive_ms)
                del watches[market_id]
                if signal is not None:
                    signal_sequence += 1
                    delayed_signal = replace(
                        signal,
                        signal_id=f"{state.market_id}:delay_5s:{signal_sequence}",
                        parent_signal_id=signal.signal_id,
                        variant="delay_5s",
                        decision_ms=signal.decision_ms + cfg.delayed_control_ms,
                        remaining_s=(
                            state.slot + 300.0
                            - (signal.decision_ms + cfg.delayed_control_ms) / 1_000.0
                        ),
                    )
                    counters["delay_5s_signals"] += 1
                    schedule(delayed_signal)

        for market_id, assets in touched.items():
            state = markets[market_id]
            if state.consumed or market_id in regressed_markets:
                continue
            for asset_id in tuple(assets):
                before = prior.get((market_id, asset_id), _summary(None))
                after_book = state.books.get(asset_id)
                after = _summary(after_book)
                if not (
                    before.ready
                    and after.ready
                    and before.best_bid is not None
                    and after.best_bid is not None
                    and before.midpoint is not None
                    and after.midpoint is not None
                    and before.spread is not None
                    and after.spread is not None
                    and before.best_bid_size >= cfg.minimum_queue_shares - _EPS
                    and (market_id, asset_id, before.best_bid) in explicit_zero
                    and after.best_bid < before.best_bid - _EPS
                    and before.midpoint - after.midpoint >= cfg.minimum_midpoint_move - _EPS
                    and after.spread - before.spread <= cfg.maximum_spread_widen + _EPS
                ):
                    continue
                remaining_s = state.slot + 300.0 - receive_ms / 1_000.0
                if not cfg.minimum_remaining_s <= remaining_s <= cfg.maximum_remaining_s:
                    counters["outside_market_window"] += 1
                    continue
                buy_asset = state.other(asset_id)
                other_book = state.books.get(buy_asset)
                # The hypothesis is specifically that the complementary book is
                # stale until its first later update.  It must come from the
                # current complete epoch, but freshness is established by that
                # later confirming update rather than at exhaustion time.
                if other_book is None or not other_book.ready:
                    counters["complement_book_unavailable"] += 1
                    continue
                assert after_book is not None
                fair = 1.0 - after.midpoint
                watch = _Watch(
                    market_id=market_id,
                    epoch=epoch,
                    exhausted_asset_id=asset_id,
                    buy_asset_id=buy_asset,
                    exhausted_ms=receive_ms,
                    exhausted_source_ms=after_book.source_ms,
                    exhausted_order=batch_order,
                    deadline_ms=receive_ms + cfg.confirmation_ms,
                    fair=fair,
                    pre_best_bid=before.best_bid,
                    pre_best_bid_size=before.best_bid_size,
                    pre_midpoint=before.midpoint,
                    post_midpoint=after.midpoint,
                )
                state.consumed = True
                if buy_asset in assets:
                    diagnostic_filled = 0.0
                    diagnostic_notional = 0.0
                    diagnostic_levels: tuple[tuple[float, float], ...] = ()
                    diagnostic_fee = None
                    diagnostic_edge = None
                    if other_book.ready:
                        diagnostic_filled, diagnostic_notional, diagnostic_levels = _take_levels(
                            other_book, cfg.target_shares,
                        )
                        if diagnostic_filled >= cfg.target_shares - _EPS:
                            diagnostic_fee = _charged_fee(diagnostic_levels, state.fee_rate)
                            diagnostic_edge = (
                                fair * cfg.target_shares
                                - diagnostic_notional
                                - diagnostic_fee
                            )
                    rows.append({
                        "paper_only": True,
                        "variant": "same_batch",
                        "market_id": market_id,
                        "decision_ms": receive_ms,
                        "evaluation_ms": None,
                        "exhausted_asset_id": asset_id,
                        "buy_asset_id": buy_asset,
                        "buy_side": state.side(buy_asset),
                        "fair": fair,
                        "pre_best_bid": before.best_bid,
                        "pre_best_bid_size": before.best_bid_size,
                        "pre_midpoint": before.midpoint,
                        "post_midpoint": after.midpoint,
                        "direct_five_share_depth": diagnostic_filled,
                        "direct_five_share_vwap": (
                            diagnostic_notional / diagnostic_filled
                            if diagnostic_filled > _EPS else None
                        ),
                        "decision_fee": diagnostic_fee,
                        "net_edge": diagnostic_edge,
                        "sent": False,
                        "filled": False,
                        "full_fill": False,
                        "reason": "same_batch_ambiguous",
                    })
                    counters["same_batch_diagnostics"] += 1
                    break
                prior_other_ms = state.last_update_ms.get(buy_asset, -math.inf)
                prior_other_order = state.last_update_order.get(buy_asset, -1)
                if (
                    0 <= receive_ms - prior_other_ms <= cfg.confirmation_ms + _EPS
                    and 0 <= prior_other_order < batch_order
                ):
                    decision(state, watch, "reverse_order", receive_ms)
                    break
                watches[market_id] = watch
                counters["base_watches"] += 1
                break

    for market_id, watch in list(watches.items()):
        if segment_end_ms > watch.deadline_ms + _EPS:
            counters["confirmation_too_late"] += 1
            del watches[market_id]
    while pending and pending[0][0] <= segment_end_ms + _EPS:
        _due, _seq, pending_item = heapq.heappop(pending)
        execute(pending_item)
    counters["right_censored_execution"] += len(pending)
    while pending:
        _due, _seq, pending_item = heapq.heappop(pending)
        execute(pending_item, "right_censored")
    return rows, counters


def _metrics(sample: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    censored = [row for row in sample if not row.get("sent")]
    attempts = [row for row in sample if row.get("sent")]
    fills = [row for row in attempts if row.get("filled")]
    full = [row for row in attempts if row.get("full_fill")]
    resolved = [row for row in fills if row.get("pnl") is not None]
    unresolved = [row for row in fills if row.get("pnl") is None]
    total_filled_shares = sum(float(row["filled_shares"]) for row in fills)
    shares = sum(float(row["filled_shares"]) for row in resolved)
    resolved_pnl = sum(float(row["pnl"]) for row in resolved)
    complete_outcomes = not unresolved
    return {
        "attempts": len(attempts),
        "right_censored": len(censored),
        "fills": len(fills),
        "full_fills": len(full),
        "resolved_fills": len(resolved),
        "unresolved_fills": len(unresolved),
        "filled_shares": total_filled_shares,
        "resolved_shares": shares,
        "wins": sum(row.get("won") is True for row in resolved),
        "losses": sum(row.get("won") is False for row in resolved),
        "fill_rate": len(full) / len(attempts) if attempts else 0.0,
        "net_ev_per_share": (
            resolved_pnl / shares if shares and complete_outcomes else None
        ),
        "pnl": resolved_pnl if complete_outcomes else None,
        "resolved_net_ev_per_share": resolved_pnl / shares if shares else None,
        "resolved_pnl": resolved_pnl,
        "reasons": dict(Counter(str(row.get("reason")) for row in sample)),
    }


def _matching_stratum(row: Mapping[str, Any]) -> tuple[Any, ...]:
    def bin_of(name: str, width: float) -> int:
        return math.floor(float(row[name]) / width + _EPS)

    return (
        str(row["buy_side"]),
        bin_of("remaining_s", 30.0),
        bin_of("fair", 0.05),
        bin_of("fixed_limit", 0.05),
        bin_of("net_edge", 0.05),
        bin_of("pre_best_bid_size", 5.0),
        math.floor(
            (float(row["pre_midpoint"]) - float(row["post_midpoint"])) / 0.01
            + _EPS
        ),
    )


def summarize(
    rows: Sequence[Mapping[str, Any]],
    counters: Mapping[str, int],
    config: QueueLagConfig | None = None,
) -> dict[str, Any]:
    cfg = config or QueueLagConfig()
    variants: dict[str, Any] = {}
    samples: dict[tuple[str, float], list[Mapping[str, Any]]] = {}
    for variant in ("base", "reverse_order", "delay_5s"):
        latencies: dict[str, Any] = {}
        for latency in cfg.evaluation_ms:
            sample = [
                row for row in rows
                if row.get("variant") == variant
                and float(row["evaluation_ms"]) == latency
            ]
            samples[(variant, latency)] = sample
            latencies[f"{latency:g}"] = _metrics(sample)
        variants[variant] = {"latencies": latencies}

    matched_controls: dict[str, Any] = {}
    for latency in cfg.evaluation_ms:
        base = samples[("base", latency)]
        reverse = samples[("reverse_order", latency)]
        common = (
            {_matching_stratum(row) for row in base if row.get("sent")}
            & {_matching_stratum(row) for row in reverse if row.get("sent")}
        )
        matched_controls[f"{latency:g}"] = {
            "common_strata": len(common),
            "base": _metrics([row for row in base if _matching_stratum(row) in common]),
            "reverse_order": _metrics([
                row for row in reverse if _matching_stratum(row) in common
            ]),
        }
    return {
        "schema": "complement-queue-lag-development-v1",
        "paper_only": True,
        "family": 34,
        "promotion_eligible": False,
        "formal_gate_implemented": False,
        "fill_model": "displayed_direct_depth_upper_bound",
        "requires_live_fak_calibration": True,
        "limit_precision": "observed_direct_ask_on_conservative_0.01_grid",
        "control_matching": {
            "outcome_blind": True,
            "fields": [
                "buy_side", "remaining_s_30", "fair_0.05", "fixed_limit_0.05",
                "net_edge_0.05", "pre_best_bid_size_5", "midpoint_move_0.01",
            ],
            "latencies": matched_controls,
        },
        "counters": dict(sorted(counters.items())),
        "variants": variants,
    }


def replay_archive(
    archive_dir: str | Path,
    *,
    rows_out: str | Path | None = None,
    config: QueueLagConfig | None = None,
) -> dict[str, Any]:
    cfg = config or QueueLagConfig()
    root = Path(archive_dir)
    standard = root / "strict" if (root / "strict" / "manifest.json").exists() else root
    checked = archive.validate_standard_artifact(standard)
    manifest = checked["manifest"]
    mappings = list(archive.iter_market_mappings(standard / "market_registry.csv.gz"))
    outcomes = {
        str(row["market_id"]): str(row["winner"])
        for row in archive.iter_outcomes(standard / "market_outcomes.csv.gz")
    }
    segment_end_ms = float(manifest["ended_ms"])
    rows, counters = replay(
        iter_physical_clob_events(standard / "clob_events.jsonl.gz"),
        mappings,
        outcomes,
        segment_end_ms=segment_end_ms,
        config=cfg,
    )
    if rows_out is not None:
        destination = Path(rows_out)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
        temporary.replace(destination)
    result = summarize(rows, counters, cfg)
    result["dataset"] = {
        "sample_scope": "development_forward_artifact",
        "archive": root.name,
        "run_id": manifest.get("run_id"),
        "collector_region": manifest.get("collector_region"),
        "started_ms": manifest.get("started_ms"),
        "ended_ms": manifest.get("ended_ms"),
        "mapped_markets": len(mappings),
        "outcomes": len(outcomes),
        "receipt_race_ready": manifest.get("receipt_race_ready") is True,
    }
    result["record_count"] = len(rows)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", required=True)
    parser.add_argument("--rows-out")
    args = parser.parse_args()
    print(json.dumps(
        replay_archive(args.archive, rows_out=args.rows_out),
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ))


if __name__ == "__main__":
    main()
