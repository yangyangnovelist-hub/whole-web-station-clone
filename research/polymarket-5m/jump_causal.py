"""Causal dataset contract for predicting the production Binance 1 bp jump.

The feature clock is local receipt time.  The target clock is Binance spot trade time.  A frame
may affect a decision only after that frame was received; exchange timestamps never reorder the
feature stream.  The detector itself is an exact port of the production 1 s / 1 bp / 5 s rule.
"""
from __future__ import annotations

import argparse
import calendar
import collections
import datetime as dt
import decimal
import gzip
import hashlib
import heapq
import inspect
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Sequence

import numpy as np


NS = 1_000_000_000
MS = 1_000_000
NAN = math.nan
RAW_SOURCES = (
    "bn_spot",
    "bn_fut_mkt",
    "bn_fut_trade",
    "bn_fut_pub",
    "okx",
    "bybit_spot",
    "bybit_lin",
    "coinbase",
    "deribit",
)
CHANNELS = (
    "bn_spot",
    "bn_perp",
    "okx_spot",
    "okx_perp",
    "bybit_spot",
    "bybit_perp",
    "coinbase",
    "deribit",
)
LEADER_GROUPS = {
    "okx": ("okx",),
    "bybit": ("bybit_spot", "bybit_lin"),
    "coinbase": ("coinbase",),
    "deribit": ("deribit",),
}
FEATURE_WINDOWS_MS = (50, 200, 1_000)

CONSUMED_BASELINE = {
    "status": "consumed_do_not_tune",
    "start_utc": "2026-10-03T07:10:55Z",
    "end_utc": "2026-10-04T07:10:56Z",
    "test_alerts": 259,
    "test_hits": 1,
    "precision": 0.003861003861003861,
    "wilson_lower": 0.0006818698910721091,
    "assumed_ev_cents_per_share": -2.4613899613899615,
}
CONSUMED_START_NS = calendar.timegm((2026, 10, 3, 7, 10, 55, 0, 0, 0)) * NS
CONSUMED_END_NS = calendar.timegm((2026, 10, 4, 7, 10, 56, 0, 0, 0)) * NS

PROTOCOL = {
    "schema": "jump-causal-v1",
    "raw_schema": "source.YYYYMMDDTHH.txt.gz: <receive_unix_ns>\\t<raw_websocket_frame>\\n",
    "source_allowlist": list(RAW_SOURCES),
    "target_subscription": "Binance spot BTCUSDT @trade with timeUnit=MICROSECOND",
    "book_subscription": (
        "Binance spot/perpetual BTCUSDT depth20@100ms snapshots plus "
        "Deribit quote.BTC-PERPETUAL; Deribit 100ms batched trades are excluded"
    ),
    "decision_clock": "local_receive_unix_ns",
    "target_clock": "binance_spot_trade_exchange_unix_ns",
    "decision_step_ms": 50,
    "target_lead_ms": [120, 220],
    "feature_lookback_ms": 1_000,
    "detector": {
        "threshold_bp": 1.0,
        "window_ms": 1_000,
        "refractory_ms": 5_000,
        "slot_seconds": 300,
        "active_seconds": [60, 285],
        "extreme_ties": "latest",
        "window_left": "open",
        "window_right": "closed",
    },
    "gap_policy": {
        "max_interframe_gap_ms": 1_000,
        "required_primary": "bn_spot",
        "minimum_fresh_leaders": 2,
        "decision_window": "[decision-1000ms, decision+220ms] must fit one source session",
        "target_gap_recovery": "remainder of detector slot unavailable",
    },
    "split_policy": {
        "chronological": True,
        "purge_left_ms": 5_000,
        "purge_right_ms": 220,
        "boundary_rule": "no detector-state/feature/label interval may touch two folds",
    },
}


@dataclass(frozen=True)
class MarketEvent:
    recv_ns: int
    source: str
    channel: str
    kind: str
    exchange_ns: int | None = None
    price: float = NAN
    quantity: float = 0.0
    trade_sign: int = 0
    bid: float = NAN
    ask: float = NAN
    bid_depth: float = NAN
    ask_depth: float = NAN
    book_kind: str | None = None
    sequence_id: str | None = None


@dataclass(frozen=True)
class DetectedJump:
    exchange_ns: int
    direction: int
    extremum_ns: int
    move_bp: float


@dataclass(frozen=True)
class TargetJump:
    exchange_ns: int
    recv_ns: int
    direction: int
    move_bp: float


@dataclass(frozen=True)
class TargetSession:
    start_recv_ns: int
    end_recv_ns: int
    first_exchange_ns: int
    max_exchange_ns: int
    first_sequence: int | None
    last_sequence: int | None
    clock_sane: bool


@dataclass(frozen=True)
class DetectorState:
    last_price: float = NAN
    up_move_bp: float = NAN
    down_move_bp: float = NAN
    up_distance_bp: float = NAN
    down_distance_bp: float = NAN
    min_age_ms: float = NAN
    max_age_ms: float = NAN
    refractory_remaining_ms: float = 0.0
    last_exchange_ns: int | None = None


@dataclass(frozen=True)
class FeatureRows:
    times_ns: np.ndarray
    X: np.ndarray
    feature_names: tuple[str, ...]


@dataclass(frozen=True)
class PurgedSplit:
    train: np.ndarray
    validation: np.ndarray
    test: np.ndarray


class ExactJumpDetector:
    """Production-equivalent detector with explicit state for causal precursor features."""

    def __init__(
        self,
        *,
        threshold_bp: float = 1.0,
        window_ns: int = NS,
        refractory_ns: int = 5 * NS,
        slot_ns: int = 300 * NS,
        active_start_ns: int = 60 * NS,
        active_end_ns: int = 285 * NS,
    ) -> None:
        self.threshold_bp = float(threshold_bp)
        self.window_ns = int(window_ns)
        self.refractory_ns = int(refractory_ns)
        self.slot_ns = int(slot_ns)
        self.active_start_ns = int(active_start_ns)
        self.active_end_ns = int(active_end_ns)
        self._minimum: collections.deque[tuple[int, float]] = collections.deque()
        self._maximum: collections.deque[tuple[int, float]] = collections.deque()
        self._last_fire_ns = -10**30
        self._last_exchange_ns: int | None = None
        self._last_log_bp = NAN

    def reset_window(self) -> None:
        self._minimum.clear()
        self._maximum.clear()
        self._last_exchange_ns = None
        self._last_log_bp = NAN

    def update(self, exchange_ns: int, price: float) -> DetectedJump | None:
        stamp = int(exchange_ns)
        if price <= 0 or not math.isfinite(price):
            return None
        if self._last_exchange_ns is not None and stamp < self._last_exchange_ns:
            raise ValueError("Binance target exchange timestamp regressed")
        lp = math.log(price) * 10_000.0
        while self._minimum and self._minimum[-1][1] >= lp:
            self._minimum.pop()
        self._minimum.append((stamp, lp))
        while self._maximum and self._maximum[-1][1] <= lp:
            self._maximum.pop()
        self._maximum.append((stamp, lp))
        left = stamp - self.window_ns
        while self._minimum and self._minimum[0][0] <= left:
            self._minimum.popleft()
        while self._maximum and self._maximum[0][0] <= left:
            self._maximum.popleft()
        self._last_exchange_ns = stamp
        self._last_log_bp = lp

        rel = stamp % self.slot_ns
        if rel < self.active_start_ns or rel >= self.active_end_ns:
            return None
        if stamp - self._last_fire_ns < self.refractory_ns:
            return None
        min_ns, minimum = self._minimum[0]
        max_ns, maximum = self._maximum[0]
        up = lp - minimum
        down = maximum - lp
        if up >= self.threshold_bp and up >= down:
            self._last_fire_ns = stamp
            return DetectedJump(stamp, 1, min_ns, up)
        if down >= self.threshold_bp:
            self._last_fire_ns = stamp
            return DetectedJump(stamp, -1, max_ns, down)
        return None

    def state(self) -> DetectorState:
        if self._last_exchange_ns is None or not self._minimum or not self._maximum:
            return DetectorState()
        min_ns, minimum = self._minimum[0]
        max_ns, maximum = self._maximum[0]
        up = self._last_log_bp - minimum
        down = maximum - self._last_log_bp
        elapsed = self._last_exchange_ns - self._last_fire_ns
        refractory = max(0.0, (self.refractory_ns - elapsed) / MS)
        return DetectorState(
            last_price=math.exp(self._last_log_bp / 10_000.0),
            up_move_bp=up,
            down_move_bp=down,
            up_distance_bp=max(0.0, self.threshold_bp - up),
            down_distance_bp=max(0.0, self.threshold_bp - down),
            min_age_ms=(self._last_exchange_ns - min_ns) / MS,
            max_age_ms=(self._last_exchange_ns - max_ns) / MS,
            refractory_remaining_ms=refractory,
            last_exchange_ns=self._last_exchange_ns,
        )


class _ChannelState:
    def __init__(self) -> None:
        self.log_price = NAN
        self.last_recv_ns: int | None = None
        self.bid = NAN
        self.ask = NAN
        self.bid_depth = NAN
        self.ask_depth = NAN
        self.last_depth_recv_ns: int | None = None
        self.trades: collections.deque[tuple[int, float]] = collections.deque()
        self.books: collections.deque[tuple[int, float, float]] = collections.deque()
        self.prices: collections.deque[tuple[int, float]] = collections.deque()

    def apply(self, event: MarketEvent) -> None:
        self.last_recv_ns = event.recv_ns
        if event.kind == "trade" and event.price > 0:
            self.log_price = math.log(event.price)
            self.prices.append((event.recv_ns, self.log_price))
            notional = event.price * max(0.0, event.quantity) * event.trade_sign
            self.trades.append((event.recv_ns, notional))
        elif event.kind == "book" and event.bid > 0 and event.ask >= event.bid:
            self.log_price = math.log((event.bid + event.ask) / 2.0)
            self.prices.append((event.recv_ns, self.log_price))
            self.bid = event.bid
            self.ask = event.ask
            if event.book_kind == "depth_snapshot":
                self.bid_depth = event.bid_depth
                self.ask_depth = event.ask_depth
                self.last_depth_recv_ns = event.recv_ns
                self.books.append((event.recv_ns, event.bid_depth, event.ask_depth))

    def prune(self, now_ns: int) -> None:
        left = now_ns - 1_000 * MS
        while self.trades and self.trades[0][0] < left:
            self.trades.popleft()
        while self.books and self.books[0][0] < left:
            self.books.popleft()
        while len(self.prices) > 1 and self.prices[1][0] <= left:
            self.prices.popleft()

    def values(self, now_ns: int) -> dict[str, float]:
        self.prune(now_ns)
        values: dict[str, float] = {}
        for window_ms in FEATURE_WINDOWS_MS:
            left = now_ns - window_ms * MS
            trades = [value for stamp, value in self.trades if stamp >= left]
            books = [(bid, ask) for stamp, bid, ask in self.books if stamp >= left]
            values[f"trade_count_{window_ms}ms"] = float(len(trades))
            values[f"signed_notional_{window_ms}ms"] = float(sum(trades))
            values[f"book_updates_{window_ms}ms"] = float(len(books))
            anchor = next(
                (price for stamp, price in reversed(self.prices) if stamp <= left),
                NAN,
            )
            values[f"return_{window_ms}ms_bp"] = (
                (self.log_price - anchor) * 10_000.0
                if math.isfinite(self.log_price) and math.isfinite(anchor) else NAN
            )
            if books and math.isfinite(self.bid_depth) and math.isfinite(self.ask_depth):
                max_bid = max((bid for bid, _ in books if math.isfinite(bid)), default=NAN)
                max_ask = max((ask for _, ask in books if math.isfinite(ask)), default=NAN)
                values[f"bid_depletion_{window_ms}ms"] = (
                    max(0.0, 1.0 - self.bid_depth / max_bid) if max_bid > 0 else NAN
                )
                values[f"ask_depletion_{window_ms}ms"] = (
                    max(0.0, 1.0 - self.ask_depth / max_ask) if max_ask > 0 else NAN
                )
            else:
                values[f"bid_depletion_{window_ms}ms"] = NAN
                values[f"ask_depletion_{window_ms}ms"] = NAN
        mid = math.exp(self.log_price) if math.isfinite(self.log_price) else NAN
        depth_age_ms = (
            (now_ns - self.last_depth_recv_ns) / MS
            if self.last_depth_recv_ns is not None else NAN
        )
        depth_fresh = math.isfinite(depth_age_ms) and depth_age_ms <= 1_000.0
        values["mid"] = mid
        values["spread_bp"] = (
            (self.ask - self.bid) / mid * 10_000.0
            if mid > 0 and math.isfinite(self.bid) and math.isfinite(self.ask) else NAN
        )
        total = self.bid_depth + self.ask_depth
        values["depth_imbalance"] = (
            (self.bid_depth - self.ask_depth) / total
            if depth_fresh and total > 0 and math.isfinite(total) else NAN
        )
        values["depth_age_ms"] = depth_age_ms
        values["age_ms"] = (
            (now_ns - self.last_recv_ns) / MS if self.last_recv_ns is not None else NAN
        )
        return values


class CausalFeatureEngine:
    def __init__(self, channels: Sequence[str] = CHANNELS) -> None:
        self.channels = tuple(channels)
        self.states = {channel: _ChannelState() for channel in self.channels}
        self.detector = ExactJumpDetector()
        self._last_spot_sequence: int | None = None
        self._last_spot_transport_recv_ns: int | None = None

    def apply(self, event: MarketEvent) -> DetectedJump | None:
        if event.channel == "bn_spot":
            if (
                self._last_spot_transport_recv_ns is not None
                and event.recv_ns - self._last_spot_transport_recv_ns > NS
            ):
                self.detector = ExactJumpDetector()
            self._last_spot_transport_recv_ns = event.recv_ns
        if (
            event.channel == "bn_spot"
            and event.kind == "trade"
            and event.exchange_ns is not None
        ):
            if event.sequence_id is not None:
                sequence = int(event.sequence_id)
                if self._last_spot_sequence is not None:
                    if sequence == self._last_spot_sequence:
                        return None
                    if sequence < self._last_spot_sequence:
                        raise ValueError("Binance target trade id regressed")
                    if sequence != self._last_spot_sequence + 1:
                        self.detector = ExactJumpDetector()
                self._last_spot_sequence = sequence
            state = self.states.get(event.channel)
            if state is not None:
                state.apply(event)
            return self.detector.update(event.exchange_ns, event.price)
        state = self.states.get(event.channel)
        if state is not None:
            state.apply(event)
        return None

    def snapshot(self, decision_ns: int) -> dict[str, float]:
        detector = self.detector.state()
        row = {
            "detector_last_price": detector.last_price,
            "detector_up_move_bp": detector.up_move_bp,
            "detector_down_move_bp": detector.down_move_bp,
            "detector_up_distance_bp": detector.up_distance_bp,
            "detector_down_distance_bp": detector.down_distance_bp,
            "detector_min_age_ms": detector.min_age_ms,
            "detector_max_age_ms": detector.max_age_ms,
            "detector_refractory_remaining_ms": detector.refractory_remaining_ms,
            "detector_feed_lag_ms": (
                (decision_ns - detector.last_exchange_ns) / MS
                if detector.last_exchange_ns is not None else NAN
            ),
        }
        channel_values = {channel: self.states[channel].values(decision_ns) for channel in self.channels}
        spot_log = self.states["bn_spot"].log_price if "bn_spot" in self.states else NAN
        for channel in self.channels:
            for name, value in channel_values[channel].items():
                row[f"{channel}_{name}"] = value
            log_price = self.states[channel].log_price
            row[f"{channel}_residual_bp"] = (
                (log_price - spot_log) * 10_000.0
                if math.isfinite(log_price) and math.isfinite(spot_log) else NAN
            )
        return row


def build_feature_rows(events: Iterable[MarketEvent], decision_times_ns: np.ndarray) -> FeatureRows:
    """Build snapshots after applying exactly the frames received by each decision time."""
    decisions = np.asarray(decision_times_ns, dtype=np.int64)
    if len(decisions) and np.any(np.diff(decisions) < 0):
        raise ValueError("decision times must be sorted")
    ordered = iter(sorted(events, key=lambda event: event.recv_ns))
    pending = next(ordered, None)
    engine = CausalFeatureEngine()
    rows: list[dict[str, float]] = []
    residual_history: dict[str, collections.deque[tuple[int, float]]] = {
        channel: collections.deque() for channel in CHANNELS
    }
    for decision in decisions:
        while pending is not None and pending.recv_ns <= int(decision):
            engine.apply(pending)
            pending = next(ordered, None)
        row = engine.snapshot(int(decision))
        _add_residual_changes(row, int(decision), residual_history)
        rows.append(row)
    names = tuple(rows[0]) if rows else _empty_feature_names()
    matrix = (
        np.asarray([[row[name] for name in names] for row in rows], dtype=np.float32)
        if rows else np.empty((0, len(names)), dtype=np.float32)
    )
    return FeatureRows(decisions, matrix, names)


def _add_residual_changes(
    row: dict[str, float],
    decision_ns: int,
    residual_history: Mapping[str, collections.deque[tuple[int, float]]],
) -> None:
    for channel in CHANNELS:
        name = f"{channel}_residual_bp"
        value = row[name]
        history = residual_history[channel]
        history.append((decision_ns, value))
        while history and history[0][0] < decision_ns - 1_000 * MS:
            history.popleft()
        for window_ms in (50, 200):
            old = NAN
            for stamp, historical in reversed(history):
                if stamp <= decision_ns - window_ms * MS:
                    old = historical
                    break
            row[f"{channel}_residual_change_{window_ms}ms"] = (
                value - old if math.isfinite(value) and math.isfinite(old) else NAN
            )


def _empty_feature_names() -> tuple[str, ...]:
    engine = CausalFeatureEngine()
    row = engine.snapshot(0)
    for channel in CHANNELS:
        row[f"{channel}_residual_change_50ms"] = NAN
        row[f"{channel}_residual_change_200ms"] = NAN
    return tuple(row)


def extract_target_jumps(
    events: Iterable[MarketEvent],
    *,
    max_gap_ms: int = 1_000,
) -> tuple[TargetJump, ...]:
    detector = ExactJumpDetector()
    jumps: list[TargetJump] = []
    last_recv_ns = -1
    last_transport_recv_ns: int | None = None
    last_sequence: int | None = None
    for event in events:
        if event.recv_ns < last_recv_ns:
            raise ValueError("target events must be ordered by receipt time")
        last_recv_ns = event.recv_ns
        if event.channel != "bn_spot":
            continue
        if last_transport_recv_ns is not None and event.recv_ns - last_transport_recv_ns > max_gap_ms * MS:
            detector = ExactJumpDetector()
        last_transport_recv_ns = event.recv_ns
        if event.kind != "trade" or event.exchange_ns is None:
            continue
        if event.sequence_id is None:
            raise ValueError("Binance target trade is missing its trade id")
        sequence = int(event.sequence_id)
        if last_sequence is not None:
            if sequence == last_sequence:
                continue
            if sequence < last_sequence:
                raise ValueError("Binance target trade id regressed")
            if sequence != last_sequence + 1:
                detector = ExactJumpDetector()
        last_sequence = sequence
        jump = detector.update(event.exchange_ns, event.price)
        if jump is not None:
            jumps.append(TargetJump(jump.exchange_ns, event.recv_ns, jump.direction, jump.move_bp))
    return tuple(jumps)


def label_decisions(
    decision_times_ns: np.ndarray,
    jumps: Sequence[TargetJump],
    *,
    lead_min_ms: int = 120,
    lead_max_ms: int = 220,
) -> tuple[np.ndarray, np.ndarray]:
    times = np.asarray(decision_times_ns, dtype=np.int64)
    labels = np.zeros(len(times), dtype=np.int8)
    matched = np.full(len(times), -1, dtype=np.int32)
    if not len(times) or not jumps:
        return labels, matched
    ordered = sorted(enumerate(jumps), key=lambda pair: pair[1].exchange_ns)
    jump_times = np.asarray([jump.exchange_ns for _, jump in ordered], dtype=np.int64)
    lower = times + lead_min_ms * MS
    upper = times + lead_max_ms * MS
    positions = np.searchsorted(jump_times, lower, side="left")
    for row, start in enumerate(positions):
        for position in range(int(start), len(ordered)):
            original, jump = ordered[position]
            if jump.exchange_ns > int(upper[row]):
                break
            # Receipt-time causality is non-negotiable: an already received crossing is not a
            # prediction even if a skewed exchange clock places it after the decision epoch.
            if jump.recv_ns <= int(times[row]):
                continue
            labels[row] = jump.direction
            matched[row] = original
            break
    return labels, matched


def active_decision_mask(
    decision_times_ns: np.ndarray,
    *,
    lead_min_ms: int = 120,
    lead_max_ms: int = 220,
) -> np.ndarray:
    """Only score decisions whose complete target interval lies in the production active slot."""
    times = np.asarray(decision_times_ns, dtype=np.int64)
    relative = times % (300 * NS)
    return (
        (relative >= 60 * NS - lead_min_ms * MS)
        & (relative < 285 * NS - lead_max_ms * MS)
    )


def continuous_sessions(receive_times_ns: np.ndarray, *, max_gap_ms: int = 1_000) -> tuple[tuple[int, int], ...]:
    values = np.asarray(receive_times_ns, dtype=np.int64)
    if not len(values):
        return ()
    values = np.sort(values)
    max_gap_ns = int(max_gap_ms) * MS
    starts = np.r_[0, np.flatnonzero(np.diff(values) > max_gap_ns) + 1]
    ends = np.r_[starts[1:] - 1, len(values) - 1]
    return tuple((int(values[start]), int(values[end])) for start, end in zip(starts, ends, strict=True))


def _covered(sessions: Sequence[tuple[int, int]], left: int, right: int) -> bool:
    for start, end in sessions:
        if start > left:
            return False
        if start <= left and end >= right:
            return True
    return False


def availability_mask(
    decision_times_ns: np.ndarray,
    sessions: Mapping[str, Sequence[tuple[int, int]]],
    *,
    primary: str = "bn_spot",
    leaders: Sequence[str] | None = None,
    leader_groups: Mapping[str, Sequence[str]] | None = None,
    min_leaders: int = 2,
    feature_lookback_ms: int = 1_000,
    label_lookahead_ms: int = 220,
) -> np.ndarray:
    if primary not in sessions:
        return np.zeros(len(decision_times_ns), dtype=bool)
    if leaders is not None and leader_groups is not None:
        raise ValueError("provide leaders or leader_groups, not both")
    groups = (
        {source: (source,) for source in leaders}
        if leaders is not None else dict(leader_groups or LEADER_GROUPS)
    )
    out = np.zeros(len(decision_times_ns), dtype=bool)
    for index, stamp in enumerate(np.asarray(decision_times_ns, dtype=np.int64)):
        left = int(stamp) - feature_lookback_ms * MS
        right = int(stamp) + label_lookahead_ms * MS
        if not _covered(sessions[primary], left, right):
            continue
        # Leader health at the decision may use only history.  Requiring leader traffic after the
        # decision would select rows using future information.  Only the target oracle must cover
        # the label horizon through ``right``.
        count = sum(
            any(_covered(sessions.get(source, ()), left, int(stamp)) for source in members)
            for members in groups.values()
        )
        out[index] = count >= int(min_leaders)
    return out


def purged_three_way_split(
    decision_times_ns: np.ndarray,
    *,
    train_end_ns: int,
    validation_end_ns: int,
    feature_lookback_ms: int = 1_000,
    label_lookahead_ms: int = 220,
    detector_history_ms: int = 5_000,
) -> PurgedSplit:
    if train_end_ns >= validation_end_ns:
        raise ValueError("train_end_ns must precede validation_end_ns")
    times = np.asarray(decision_times_ns, dtype=np.int64)
    left = max(feature_lookback_ms, detector_history_ms) * MS
    right = label_lookahead_ms * MS
    train = times + right < int(train_end_ns)
    validation = (times - left >= int(train_end_ns)) & (times + right < int(validation_end_ns))
    test = times - left >= int(validation_end_ns)
    return PurgedSplit(train, validation, test)


def _clock_to_ns(value: object | None) -> int | None:
    if value is None or value == "":
        return None
    raw = int(decimal.Decimal(str(value)))
    magnitude = abs(raw)
    if magnitude >= 10**17:
        return raw
    if magnitude >= 10**14:
        return raw * 1_000
    return raw * 1_000_000


def _iso_to_ns(value: str) -> int:
    stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("exchange ISO timestamp must include a timezone")
    stamp = stamp.astimezone(dt.UTC)
    seconds = calendar.timegm(stamp.utctimetuple())
    return seconds * NS + stamp.microsecond * 1_000


def _trade_event(
    recv_ns: int,
    source: str,
    channel: str,
    exchange: object | None,
    price: object,
    quantity: object,
    sign: int,
    sequence_id: object | None = None,
) -> MarketEvent:
    return MarketEvent(
        int(recv_ns), source, channel, "trade", _clock_to_ns(exchange),
        float(price), float(quantity), int(sign),
        sequence_id=None if sequence_id is None else str(sequence_id),
    )


def _book_event(
    recv_ns: int,
    source: str,
    channel: str,
    exchange: object | None,
    bids: Sequence[Sequence[object]],
    asks: Sequence[Sequence[object]],
    book_kind: str,
) -> MarketEvent | None:
    if not bids or not asks:
        return None
    return MarketEvent(
        int(recv_ns), source, channel, "book", _clock_to_ns(exchange),
        bid=float(bids[0][0]), ask=float(asks[0][0]),
        bid_depth=sum(float(level[1]) for level in bids),
        ask_depth=sum(float(level[1]) for level in asks),
        book_kind=book_kind,
    )


def parse_frame(source: str, recv_ns: int, payload: str | bytes | Mapping[str, object]) -> list[MarketEvent]:
    if isinstance(payload, (str, bytes, bytearray)):
        try:
            message = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return []
    else:
        message = dict(payload)
    if not isinstance(message, Mapping):
        return []
    out: list[MarketEvent] = []

    if source == "bn_spot":
        data = message.get("data", message)
        if not isinstance(data, Mapping):
            return out
        stream = str(message.get("stream", ""))
        if data.get("e") == "trade" or stream.endswith("@trade"):
            out.append(_trade_event(
                recv_ns, source, "bn_spot", data.get("T"), data["p"], data["q"],
                -1 if bool(data.get("m")) else 1,
                data.get("t"),
            ))
        elif "depth" in stream:
            event = _book_event(
                recv_ns, source, "bn_spot", data.get("T") or data.get("E"),
                data.get("bids") or data.get("b") or (), data.get("asks") or data.get("a") or (),
                "depth_snapshot",
            )
            if event is not None:
                out.append(event)
        elif "bookTicker" in stream or all(key in data for key in ("b", "a", "B", "A")):
            event = _book_event(
                recv_ns, source, "bn_spot", data.get("T") or data.get("E"),
                ((data["b"], data["B"]),), ((data["a"], data["A"]),),
                "bbo",
            )
            if event is not None:
                out.append(event)
        return out

    if source in ("bn_fut_trade", "bn_fut_pub", "bn_fut_mkt"):
        data = message.get("data", message)
        if not isinstance(data, Mapping):
            return out
        stream = str(message.get("stream", ""))
        event_name = str(data.get("e", ""))
        if source == "bn_fut_trade" and event_name == "trade":
            out.append(_trade_event(
                recv_ns, source, "bn_perp", data.get("T"), data["p"], data["q"],
                -1 if bool(data.get("m")) else 1,
                data.get("t"),
            ))
        elif source == "bn_fut_pub" and (
            "depth20" in stream or ("lastUpdateId" in data and "bids" in data and "asks" in data)
        ):
            event = _book_event(
                recv_ns, source, "bn_perp", data.get("T") or data.get("E"),
                data.get("b") or (), data.get("a") or (),
                "depth_snapshot",
            )
            if event is not None:
                out.append(event)
        elif source == "bn_fut_pub" and (event_name == "bookTicker" or "bookTicker" in stream):
            event = _book_event(
                recv_ns, source, "bn_perp", data.get("T") or data.get("E"),
                ((data["b"], data["B"]),), ((data["a"], data["A"]),),
                "bbo",
            )
            if event is not None:
                out.append(event)
        return out

    if source == "okx":
        arg = message.get("arg") or {}
        rows = message.get("data") or []
        if not isinstance(arg, Mapping) or not isinstance(rows, Sequence):
            return out
        channel = "okx_perp" if str(arg.get("instId", "")).endswith("-SWAP") else "okx_spot"
        topic = str(arg.get("channel", ""))
        for item in rows:
            if not isinstance(item, Mapping):
                continue
            if topic == "trades":
                out.append(_trade_event(
                    recv_ns, source, channel, item.get("ts"), item["px"], item["sz"],
                    1 if str(item.get("side", "")).lower() == "buy" else -1,
                ))
            elif topic.startswith("bbo"):
                event = _book_event(
                    recv_ns, source, channel, item.get("ts"), item.get("bids") or (), item.get("asks") or (),
                    "bbo",
                )
                if event is not None:
                    out.append(event)
        return out

    if source in ("bybit_spot", "bybit_lin"):
        channel = "bybit_spot" if source == "bybit_spot" else "bybit_perp"
        topic = str(message.get("topic", ""))
        data = message.get("data")
        if topic.startswith("publicTrade") and isinstance(data, Sequence):
            for item in data:
                if isinstance(item, Mapping):
                    out.append(_trade_event(
                        recv_ns, source, channel, item.get("T") or message.get("ts"),
                        item["p"], item["v"], 1 if str(item.get("S", "")).lower() == "buy" else -1,
                    ))
        elif topic.startswith("orderbook") and isinstance(data, Mapping):
            event = _book_event(
                recv_ns, source, channel, message.get("cts") or message.get("ts"),
                data.get("b") or (), data.get("a") or (),
                "bbo",
            )
            if event is not None:
                out.append(event)
        return out

    if source == "coinbase":
        kind = str(message.get("type", ""))
        exchange = _iso_to_ns(str(message["time"])) if message.get("time") else None
        if kind in ("match", "last_match"):
            maker_side = str(message.get("side", "")).lower()
            out.append(_trade_event(
                recv_ns, source, "coinbase", exchange, message["price"], message["size"],
                -1 if maker_side == "buy" else 1,
            ))
        elif kind == "ticker" and all(key in message for key in ("best_bid", "best_ask")):
            event = _book_event(
                recv_ns, source, "coinbase", exchange,
                ((message["best_bid"], message.get("best_bid_size", 0)),),
                ((message["best_ask"], message.get("best_ask_size", 0)),),
                "bbo",
            )
            if event is not None:
                out.append(event)
        return out

    if source == "deribit":
        params = message.get("params") or {}
        if not isinstance(params, Mapping):
            return out
        topic = str(params.get("channel", ""))
        data = params.get("data")
        # Deribit trades are intentionally excluded: the public trade channel is batched every
        # 100 ms, so its receipt time is not an event-time lead.  The quote feed is pushed live
        # from London and is the only Deribit stream admitted to the jump predictor.
        if topic == "quote.BTC-PERPETUAL" and isinstance(data, Mapping):
            required = (
                "best_bid_price", "best_ask_price", "best_bid_amount", "best_ask_amount"
            )
            if all(data.get(key) is not None for key in required):
                event = _book_event(
                    recv_ns,
                    source,
                    "deribit",
                    data.get("timestamp"),
                    ((data["best_bid_price"], data["best_bid_amount"]),),
                    ((data["best_ask_price"], data["best_ask_amount"]),),
                    "bbo",
                )
                if event is not None:
                    out.append(event)
        return out
    return out


def _iter_source_lines(paths: Sequence[Path], source: str) -> Iterator[tuple[int, str, str]]:
    for path in sorted(paths, key=lambda item: item.name):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                stamp, separator, payload = line.rstrip("\n").partition("\t")
                if separator:
                    yield int(stamp), source, payload


def iter_raw_events(data_dir: str | Path, sources: Sequence[str] = RAW_SOURCES) -> Iterator[MarketEvent]:
    root = Path(data_dir)
    paths = [path for source in sources for path in root.glob(f"{source}.*.txt.gz")]
    yield from iter_raw_events_from_files(paths)


def iter_raw_events_from_files(
    source_files: Sequence[str | Path],
    *,
    recv_start_ns: int | None = None,
    recv_end_ns: int | None = None,
) -> Iterator[MarketEvent]:
    grouped: dict[str, list[Path]] = collections.defaultdict(list)
    for raw_path in source_files:
        path = Path(raw_path)
        grouped[path.name.split(".", 1)[0]].append(path)
    streams = [_iter_source_lines(paths, source) for source, paths in sorted(grouped.items())]
    for recv_ns, source, payload in heapq.merge(*streams, key=lambda row: row[0]):
        if recv_start_ns is not None and recv_ns < recv_start_ns:
            continue
        if recv_end_ns is not None and recv_ns > recv_end_ns:
            continue
        yield from parse_frame(source, recv_ns, payload)


def source_sessions_from_files(
    source_files: Sequence[str | Path],
    *,
    max_gap_ms: int = 1_000,
    recv_start_ns: int | None = None,
    recv_end_ns: int | None = None,
) -> dict[str, tuple[tuple[int, int], ...]]:
    grouped: dict[str, list[Path]] = collections.defaultdict(list)
    for raw_path in source_files:
        path = Path(raw_path)
        grouped[path.name.split(".", 1)[0]].append(path)
    max_gap_ns = max_gap_ms * MS
    result: dict[str, tuple[tuple[int, int], ...]] = {}
    for source, paths in sorted(grouped.items()):
        sessions: list[tuple[int, int]] = []
        start = previous = None
        for stamp, _, _ in _iter_source_lines(paths, source):
            if recv_start_ns is not None and stamp < recv_start_ns:
                continue
            if recv_end_ns is not None and stamp > recv_end_ns:
                break
            if start is None:
                start = previous = stamp
            elif stamp - int(previous) > max_gap_ns:
                sessions.append((int(start), int(previous)))
                start = stamp
            previous = stamp
        if start is not None and previous is not None:
            sessions.append((int(start), int(previous)))
        result[source] = tuple(sessions)
    return result


def certified_target_sessions_from_files(
    source_files: Sequence[str | Path],
    *,
    max_gap_ms: int = 1_000,
    recv_start_ns: int | None = None,
    recv_end_ns: int | None = None,
) -> tuple[TargetSession, ...]:
    paths = [Path(path) for path in source_files if Path(path).name.startswith("bn_spot.")]
    max_gap_ns = max_gap_ms * MS
    sessions: list[TargetSession] = []
    state: dict[str, int | bool | None] | None = None
    transport_start_ns: int | None = None
    previous_transport_ns: int | None = None
    last_trade_recv_ns: int | None = None

    def close(*, end_recv_ns: int | None = None) -> None:
        nonlocal state
        if state is None:
            return
        if end_recv_ns is not None:
            state["end_recv_ns"] = end_recv_ns
        sessions.append(TargetSession(
            int(state["start_recv_ns"]),
            int(state["end_recv_ns"]),
            int(state["first_exchange_ns"]),
            int(state["max_exchange_ns"]),
            None if state["first_sequence"] is None else int(state["first_sequence"]),
            None if state["last_sequence"] is None else int(state["last_sequence"]),
            bool(state["clock_sane"]),
        ))
        state = None

    for stamp, _, payload in _iter_source_lines(paths, "bn_spot"):
        if recv_start_ns is not None and stamp < recv_start_ns:
            continue
        if recv_end_ns is not None and stamp > recv_end_ns:
            break
        if previous_transport_ns is None or stamp - previous_transport_ns > max_gap_ns:
            close()
            transport_start_ns = stamp
            last_trade_recv_ns = None
        previous_transport_ns = stamp
        events = parse_frame("bn_spot", stamp, payload)
        target_trades = [
            event for event in events if event.kind == "trade" and event.exchange_ns is not None
        ]
        for event in target_trades:
            sequence = int(event.sequence_id) if event.sequence_id is not None else None
            if sequence is None:
                raise ValueError("Binance target trade is missing its trade id")
            if state is not None and sequence is not None and sequence == state["last_sequence"]:
                continue
            broken = False
            if state is not None:
                broken |= event.exchange_ns < int(state["max_exchange_ns"])
                previous_sequence = state["last_sequence"]
                if sequence is not None and previous_sequence is not None:
                    broken |= sequence != int(previous_sequence) + 1
            if broken:
                close(end_recv_ns=last_trade_recv_ns)
                transport_start_ns = event.recv_ns
            sane = 0 <= event.recv_ns - event.exchange_ns <= 2 * NS
            if state is None:
                state = {
                    "start_recv_ns": transport_start_ns if transport_start_ns is not None else event.recv_ns,
                    "end_recv_ns": event.recv_ns,
                    "first_exchange_ns": event.exchange_ns,
                    "max_exchange_ns": event.exchange_ns,
                    "first_sequence": sequence,
                    "last_sequence": sequence,
                    "clock_sane": sane,
                }
            else:
                state["end_recv_ns"] = event.recv_ns
                state["max_exchange_ns"] = max(int(state["max_exchange_ns"]), event.exchange_ns)
                state["last_sequence"] = sequence
                state["clock_sane"] = bool(state["clock_sane"]) and sane
            last_trade_recv_ns = event.recv_ns
        if state is not None:
            state["end_recv_ns"] = stamp
    close()
    return tuple(sessions)


def target_trade_sessions_from_files(
    source_files: Sequence[str | Path],
    *,
    max_gap_ms: int = 1_000,
    recv_start_ns: int | None = None,
    recv_end_ns: int | None = None,
) -> tuple[tuple[int, int], ...]:
    sessions = certified_target_sessions_from_files(
        source_files,
        max_gap_ms=max_gap_ms,
        recv_start_ns=recv_start_ns,
        recv_end_ns=recv_end_ns,
    )
    return tuple((session.start_recv_ns, session.end_recv_ns) for session in sessions)


def target_watermark_mask(
    decision_times_ns: np.ndarray,
    target_sessions: Sequence[TargetSession],
    *,
    feature_lookback_ms: int = 1_000,
    label_lookahead_ms: int = 220,
) -> np.ndarray:
    times = np.asarray(decision_times_ns, dtype=np.int64)
    out = np.zeros(len(times), dtype=bool)
    for index, stamp in enumerate(times):
        left = int(stamp) - feature_lookback_ms * MS
        target = int(stamp) + label_lookahead_ms * MS
        out[index] = any(
            session.clock_sane
            and session.start_recv_ns <= left
            and session.end_recv_ns >= int(stamp)
            and session.max_exchange_ns >= target
            for session in target_sessions
        )
    return out


def detector_gap_safe_mask(
    decision_times_ns: np.ndarray,
    target_sessions: Sequence[tuple[int, int]],
    *,
    slot_ns: int = 300 * NS,
) -> np.ndarray:
    """Conservatively quarantine detector state after capture start and every target gap."""
    times = np.asarray(decision_times_ns, dtype=np.int64)
    safe = np.ones(len(times), dtype=bool)
    if not target_sessions:
        return np.zeros(len(times), dtype=bool)
    first_start = int(target_sessions[0][0])
    first_safe = max(((first_start // slot_ns) + 1) * slot_ns, first_start + 5 * NS)
    safe &= times >= first_safe
    for previous, current in zip(target_sessions, target_sessions[1:]):
        gap_start = int(previous[1])
        recovery = int(current[0])
        recovery_safe = max(((recovery // slot_ns) + 1) * slot_ns, recovery + 5 * NS)
        safe &= ~((times > gap_start) & (times < recovery_safe))
    return safe


def _source_file_metadata(path: Path) -> dict[str, object]:
    compressed_sha256 = _file_digest(path)
    decompressed = hashlib.sha256()
    decompressed_bytes = 0
    first = last = None
    first_exchange = last_exchange = None
    count = 0
    ignored = 0
    event_counts: collections.Counter[str] = collections.Counter()
    source = path.name.split(".", 1)[0]
    with gzip.open(path, "rb") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            if not raw_line.endswith(b"\n"):
                raise ValueError(f"{path.name}:{line_number}: unterminated raw line")
            decompressed.update(raw_line)
            decompressed_bytes += len(raw_line)
            raw = raw_line[:-1]
            stamp, separator, payload_bytes = raw.partition(b"\t")
            if not separator:
                raise ValueError(f"{path.name}:{line_number}: missing tab separator")
            try:
                value = int(stamp)
                payload = payload_bytes.decode("utf-8")
            except (ValueError, UnicodeDecodeError) as exc:
                raise ValueError(f"{path.name}:{line_number}: malformed frame") from exc
            if last is not None and value < last:
                raise ValueError(f"{path.name}:{line_number}: receipt timestamp regressed")
            first = value if first is None else first
            last = value
            count += 1
            text_control = source == "okx" and payload in {"ping", "pong"}
            if not text_control:
                try:
                    json.loads(payload)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path.name}:{line_number}: malformed JSON payload") from exc
            try:
                events = parse_frame(source, value, payload)
            except (KeyError, TypeError, ValueError, decimal.InvalidOperation) as exc:
                raise ValueError(f"{path.name}:{line_number}: rejected market payload") from exc
            if not events:
                ignored += 1
            for event in events:
                event_counts[event.kind] += 1
                if event.exchange_ns is not None:
                    first_exchange = (
                        event.exchange_ns if first_exchange is None else min(first_exchange, event.exchange_ns)
                    )
                    last_exchange = (
                        event.exchange_ns if last_exchange is None else max(last_exchange, event.exchange_ns)
                    )
    return {
        "name": path.name,
        "source": source,
        "size_bytes": path.stat().st_size,
        "sha256": compressed_sha256,
        "decompressed_size_bytes": decompressed_bytes,
        "decompressed_sha256": decompressed.hexdigest(),
        "frames": count,
        "normalized_events": dict(sorted(event_counts.items())),
        "ignored_control_or_unmodelled_frames": ignored,
        "rejected_lines": 0,
        "first_recv_ns": first,
        "last_recv_ns": last,
        "first_exchange_ns": first_exchange,
        "last_exchange_ns": last_exchange,
        "receipt_monotonic": True,
        "gzip_closed": True,
    }


def manifest_digest(manifest: Mapping[str, object]) -> str:
    payload = dict(manifest)
    payload.pop("manifest_sha256", None)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def build_split_manifest(
    source_files: Sequence[str | Path],
    *,
    train_end_ns: int,
    validation_end_ns: int,
    provenance: Mapping[str, object] | None = None,
) -> dict[str, object]:
    metadata = [_source_file_metadata(Path(path)) for path in sorted(map(Path, source_files), key=lambda p: p.name)]
    starts = [int(item["first_recv_ns"]) for item in metadata if item["first_recv_ns"] is not None]
    ends = [int(item["last_recv_ns"]) for item in metadata if item["last_recv_ns"] is not None]
    protocol = json.loads(json.dumps(PROTOCOL))
    protocol["detector_sha256"] = hashlib.sha256(
        inspect.getsource(ExactJumpDetector).encode("utf-8")
    ).hexdigest()
    protocol["feature_schema_sha256"] = hashlib.sha256(
        json.dumps(_empty_feature_names(), separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    manifest: dict[str, object] = {
        "schema": "jump-causal-manifest-v1",
        "protocol": protocol,
        "consumed_baseline": dict(CONSUMED_BASELINE),
        "provenance": dict(provenance or {}),
        "sources": metadata,
        "receive_range_ns": [min(starts) if starts else None, max(ends) if ends else None],
        "fold_boundaries_ns": {
            "train_end": int(train_end_ns),
            "validation_end": int(validation_end_ns),
        },
    }
    manifest["manifest_sha256"] = manifest_digest(manifest)
    return manifest


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_metadata(path: Path) -> dict[str, object]:
    return {"name": path.name, "size_bytes": path.stat().st_size, "sha256": _file_digest(path)}


def _verify_source_files_unchanged(
    source_files: Sequence[Path],
    source_metadata: Sequence[Mapping[str, object]],
) -> None:
    expected = {str(item["name"]): str(item["sha256"]) for item in source_metadata}
    for path in source_files:
        if _file_digest(path) != expected[path.name]:
            raise ValueError(f"{path.name} changed after initial hash")


def freeze_dataset(
    source_files: Sequence[str | Path],
    *,
    output_dir: str | Path,
    manifest_path: str | Path,
    sample_start_ns: int,
    sample_end_ns: int,
    train_end_ns: int,
    validation_end_ns: int,
    step_ms: int = 50,
    max_gap_ms: int = 1_000,
    provenance: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Write one immutable, mmap-friendly causal dataset plus its source-hash manifest."""
    if not sample_start_ns < train_end_ns < validation_end_ns < sample_end_ns:
        raise ValueError("sample and fold timestamps must be strictly ordered")
    if sample_start_ns <= CONSUMED_END_NS and sample_end_ns >= CONSUMED_START_NS:
        raise ValueError("sample overlaps the consumed immutable baseline")
    if step_ms <= 0:
        raise ValueError("step_ms must be positive")
    if step_ms != int(PROTOCOL["decision_step_ms"]):
        raise ValueError("decision step is frozen at 50 ms")
    if max_gap_ms != int(PROTOCOL["gap_policy"]["max_interframe_gap_ms"]):
        raise ValueError("gap threshold is frozen at 1000 ms")
    files = tuple(sorted((Path(path) for path in source_files), key=lambda path: path.name))
    manifest = build_split_manifest(
        files,
        train_end_ns=train_end_ns,
        validation_end_ns=validation_end_ns,
        provenance=provenance,
    )
    receive_start, receive_end = manifest["receive_range_ns"]
    if receive_start is None or receive_end is None:
        raise ValueError("raw files contain no timestamped frames")
    if sample_start_ns < int(receive_start) or sample_end_ns > int(receive_end):
        raise ValueError("sample range lies outside the hashed raw files")

    history_start = sample_start_ns - 5_000 * MS
    sessions = source_sessions_from_files(
        files,
        max_gap_ms=max_gap_ms,
        recv_end_ns=sample_end_ns,
    )
    certified_target_sessions = certified_target_sessions_from_files(
        files,
        max_gap_ms=max_gap_ms,
        recv_end_ns=sample_end_ns,
    )
    target_sessions = tuple(
        (session.start_recv_ns, session.end_recv_ns) for session in certified_target_sessions
    )
    sessions["bn_spot"] = target_sessions
    jumps = extract_target_jumps(iter_raw_events_from_files(
        files,
        recv_start_ns=history_start,
        recv_end_ns=sample_end_ns,
    ))
    step_ns = step_ms * MS
    first_decision = ((sample_start_ns + step_ns - 1) // step_ns) * step_ns
    decisions = np.arange(first_decision, sample_end_ns + 1, step_ns, dtype=np.int64)
    labels, matched = label_decisions(decisions, jumps)
    source_available = availability_mask(
        decisions,
        sessions,
        primary="bn_spot",
        leader_groups=LEADER_GROUPS,
        min_leaders=2,
        feature_lookback_ms=1_000,
        label_lookahead_ms=0,
    )
    target_watermark = target_watermark_mask(decisions, certified_target_sessions)
    detector_safe = detector_gap_safe_mask(decisions, target_sessions)
    active = active_decision_mask(decisions)
    split = purged_three_way_split(
        decisions,
        train_end_ns=train_end_ns,
        validation_end_ns=validation_end_ns,
    )
    split_code = np.zeros(len(decisions), dtype=np.uint8)
    split_code[split.train] = 1
    split_code[split.validation] = 2
    split_code[split.test] = 3
    eligible = source_available & target_watermark & detector_safe & active & (split_code != 0)
    frozen_labels = labels.copy()
    frozen_labels[~eligible] = np.int8(-128)
    matched = matched.copy()
    matched[~eligible] = -1

    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    artifact_names = (
        "features.npy",
        "times.npy",
        "labels.npy",
        "matched_jump.npy",
        "split.npy",
        "source_available.npy",
        "target_watermark.npy",
        "detector_gap_safe.npy",
        "eligible.npy",
        "feature_names.json",
        "target_jumps.json",
    )
    names = _empty_feature_names()
    features_path = target / "features.npy"
    matrix = np.lib.format.open_memmap(
        features_path,
        mode="w+",
        dtype=np.float32,
        shape=(len(decisions), len(names)),
    )
    engine = CausalFeatureEngine()
    residual_history: dict[str, collections.deque[tuple[int, float]]] = {
        channel: collections.deque() for channel in CHANNELS
    }
    events = iter_raw_events_from_files(
        files,
        recv_start_ns=history_start,
        recv_end_ns=sample_end_ns,
    )
    pending = next(events, None)
    for row_index, decision in enumerate(decisions):
        while pending is not None and pending.recv_ns <= int(decision):
            engine.apply(pending)
            pending = next(events, None)
        row = engine.snapshot(int(decision))
        _add_residual_changes(row, int(decision), residual_history)
        matrix[row_index] = [row[name] for name in names]
    matrix.flush()
    del matrix

    arrays = {
        "times.npy": decisions,
        "labels.npy": frozen_labels,
        "matched_jump.npy": matched,
        "split.npy": split_code,
        "source_available.npy": source_available,
        "target_watermark.npy": target_watermark,
        "eligible.npy": eligible,
        "detector_gap_safe.npy": detector_safe,
    }
    for name, values in arrays.items():
        np.save(target / name, values, allow_pickle=False)
    (target / "feature_names.json").write_text(
        json.dumps(names, indent=2) + "\n", encoding="utf-8",
    )
    jump_rows = [asdict(jump) for jump in jumps]
    (target / "target_jumps.json").write_text(
        json.dumps(jump_rows, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8",
    )
    _verify_source_files_unchanged(files, manifest["sources"])
    artifacts = [_artifact_metadata(target / name) for name in artifact_names]
    manifest["sample_range_ns"] = [int(sample_start_ns), int(sample_end_ns)]
    manifest["sessions"] = {
        source: {
            "count": len(source_sessions),
            "first_start_ns": source_sessions[0][0] if source_sessions else None,
            "last_end_ns": source_sessions[-1][1] if source_sessions else None,
        }
        for source, source_sessions in sorted(sessions.items())
    }
    split_rows = {}
    for code, name in ((1, "train"), (2, "validation"), (3, "test")):
        selected = eligible & (split_code == code)
        selected_times = np.ascontiguousarray(decisions[selected], dtype=np.int64)
        split_rows[name] = {
            "rows": int(selected.sum()),
            "positive": int(np.count_nonzero(frozen_labels[selected])),
            "up": int(np.count_nonzero(frozen_labels[selected] == 1)),
            "down": int(np.count_nonzero(frozen_labels[selected] == -1)),
            "first_decision_ns": int(selected_times[0]) if len(selected_times) else None,
            "last_decision_ns": int(selected_times[-1]) if len(selected_times) else None,
            "decision_times_sha256": hashlib.sha256(selected_times.tobytes()).hexdigest(),
        }
    manifest["dataset"] = {
        "directory": target.name,
        "rows": int(len(decisions)),
        "features": len(names),
        "target_jumps": len(jumps),
        "unavailable_label": -128,
        "eligible_rows": int(eligible.sum()),
        "positive_rows": int(np.count_nonzero(frozen_labels[eligible])),
        "target_trade_sessions": [[int(start), int(end)] for start, end in target_sessions],
        "excluded_rows": {
            "source_unavailable": int(np.count_nonzero(~source_available)),
            "target_watermark_incomplete": int(np.count_nonzero(source_available & ~target_watermark)),
            "detector_gap_unsafe": int(np.count_nonzero(source_available & target_watermark & ~detector_safe)),
            "inactive_target_horizon": int(np.count_nonzero(source_available & target_watermark & detector_safe & ~active)),
            "fold_purge": int(np.count_nonzero(
                source_available & target_watermark & detector_safe & active & (split_code == 0)
            )),
        },
        "split_rows": split_rows,
        "artifacts": artifacts,
    }
    manifest["manifest_sha256"] = manifest_digest(manifest)
    _write_manifest(Path(manifest_path), manifest)
    return manifest


def _write_manifest(path: Path, manifest: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _parse_utc(value: str) -> int:
    return _iso_to_ns(value)


def select_closed_hour_files(
    data_dir: str | Path,
    hours: Sequence[str],
    *,
    sources: Sequence[str] = RAW_SOURCES,
) -> tuple[Path, ...]:
    """Resolve complete closed hours; a partially recorded hour fails closed."""
    root = Path(data_dir)
    selected: list[Path] = []
    for hour in dict.fromkeys(hours):
        by_source = {source: root / f"{source}.{hour}.txt.gz" for source in sources}
        missing = sorted(source for source, path in by_source.items() if not path.is_file())
        if missing:
            raise ValueError(f"closed hour {hour} is missing required sources: {missing}")
        selected.extend(by_source[source] for source in sources)
    return tuple(selected)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True)
    parser.add_argument(
        "--hour",
        action="append",
        help="closed UTC hour; repeat for a contiguous multi-hour freeze, e.g. 20261004T14",
    )
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--dataset-dir")
    parser.add_argument("--sample-start", help="ISO-8601 UTC; required with --dataset-dir")
    parser.add_argument("--sample-end", help="ISO-8601 UTC; required with --dataset-dir")
    parser.add_argument("--train-end", required=True, help="ISO-8601 UTC fold boundary")
    parser.add_argument("--validation-end", required=True, help="ISO-8601 UTC fold boundary")
    parser.add_argument("--step-ms", type=int, default=50)
    parser.add_argument("--collector-region", default="unknown")
    parser.add_argument("--recorder-source", help="exact deployed recorder source for provenance hashing")
    args = parser.parse_args(argv)
    root = Path(args.data)
    try:
        files = (
            list(select_closed_hour_files(root, args.hour))
            if args.hour
            else [path for source in RAW_SOURCES for path in root.glob(f"{source}.*.txt.gz")]
        )
    except ValueError as error:
        raise SystemExit(str(error)) from error
    if not files:
        raise SystemExit("no raw source files")
    module_path = Path(__file__).resolve()
    requirements = module_path.with_name("requirements.txt")
    provenance: dict[str, object] = {
        "collector_region": args.collector_region,
        "receipt_clock": "CLOCK_REALTIME via time.time_ns",
        "builder_name": module_path.name,
        "builder_sha256": _file_digest(module_path),
        "requirements_sha256": _file_digest(requirements) if requirements.exists() else None,
        "closed_hours": list(dict.fromkeys(args.hour or ())),
    }
    if args.recorder_source:
        recorder = Path(args.recorder_source)
        provenance["recorder_source_name"] = recorder.name
        provenance["recorder_source_sha256"] = _file_digest(recorder)
    if args.dataset_dir:
        if not args.sample_start or not args.sample_end:
            parser.error("--sample-start and --sample-end are required with --dataset-dir")
        manifest = freeze_dataset(
            files,
            output_dir=args.dataset_dir,
            manifest_path=args.manifest,
            sample_start_ns=_parse_utc(args.sample_start),
            sample_end_ns=_parse_utc(args.sample_end),
            train_end_ns=_parse_utc(args.train_end),
            validation_end_ns=_parse_utc(args.validation_end),
            step_ms=args.step_ms,
            provenance=provenance,
        )
    else:
        manifest = build_split_manifest(
            files,
            train_end_ns=_parse_utc(args.train_end),
            validation_end_ns=_parse_utc(args.validation_end),
            provenance=provenance,
        )
        _write_manifest(Path(args.manifest), manifest)
    print(json.dumps({
        "manifest": str(args.manifest),
        "sources": len(manifest["sources"]),
        "sha256": manifest["manifest_sha256"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
