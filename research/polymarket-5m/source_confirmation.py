"""Receipt-causal secondary confirmation for the frozen current-H replay."""

from __future__ import annotations

import argparse
import heapq
import json
import math
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from itertools import groupby
from pathlib import Path
from typing import Any

import h_replay as replay
import h_replay_archive as archive
import h_replay_run as run
import h_source_race as source_race

PRIMARY_SOURCES = source_race.BASELINE_SOURCES
SECONDARY_SOURCES = frozenset({"futures_trade", "deribit_quote"})
CONFIRMATION_WINDOW_MS = 120.0
PLACEBO_SHIFT_MS = 5_000.0
EVALUATION_MS = (400.0, 500.0)
TARGET_SHARES = 5.0
CORRECTION_DENOMINATOR = 874
MAX_CONFIRMATION_CANDIDATES = 4_096
ARMS = ("baseline", "confirmation_veto", "receipt_time_shifted_placebo")
STRATEGY_ID = "FAMILY36-SOURCE-CONFIRMATION-V1"
PROSPECTIVE_CUTOFF_MS = 1_791_750_600_000.0
_EPS = 1e-9


@dataclass(frozen=True)
class ConfirmationDecision:
    confirmed: bool
    reason: str
    same_sources: tuple[str, ...] = ()
    opposite_sources: tuple[str, ...] = ()


class ConfirmationWindow:
    """Fixed-memory, receipt-clock state for already-arrived secondary candidates."""

    def __init__(
        self,
        window_ms: float = CONFIRMATION_WINDOW_MS,
        *,
        max_candidates: int = MAX_CONFIRMATION_CANDIDATES,
    ) -> None:
        if not math.isfinite(window_ms) or window_ms <= 0:
            raise ValueError("confirmation window must be positive")
        if max_candidates <= 0:
            raise ValueError("max_candidates must be positive")
        self.window_ms = float(window_ms)
        self.max_candidates = int(max_candidates)
        self._connected: dict[str, int | None] = {
            source: None for source in SECONDARY_SOURCES
        }
        self._last_epoch = {source: 0 for source in SECONDARY_SOURCES}
        self._latest: dict[tuple[str, int, int], tuple[float, int]] = {}
        self._last_receive_ms = -math.inf

    @property
    def memory_size(self) -> int:
        return len(self._latest)

    def epoch(self, source: str) -> int | None:
        self._require_source(source)
        return self._connected[source]

    def _require_source(self, source: str) -> None:
        if source not in SECONDARY_SOURCES:
            raise ValueError(f"unknown secondary source: {source}")

    def _advance(self, receive_ms: float) -> float:
        timestamp = float(receive_ms)
        if not math.isfinite(timestamp):
            raise ValueError("invalid receipt timestamp")
        if timestamp < self._last_receive_ms - _EPS:
            raise ValueError("receipt clock regressed")
        self._last_receive_ms = max(self._last_receive_ms, timestamp)
        cutoff = timestamp - self.window_ms
        self._latest = {
            key: value
            for key, value in self._latest.items()
            if value[0] >= cutoff - _EPS
        }
        return timestamp

    def advance(self, *, receive_ms: float) -> None:
        self._advance(receive_ms)

    def connect(self, source: str, *, epoch: int, receive_ms: float) -> None:
        self._require_source(source)
        self._advance(receive_ms)
        if self._connected[source] is not None or epoch <= self._last_epoch[source]:
            raise ValueError(f"invalid {source} connection epoch {epoch}")
        self._connected[source] = int(epoch)
        self._last_epoch[source] = int(epoch)
        self._drop_source(source)

    def disconnect(self, source: str, *, epoch: int, receive_ms: float) -> None:
        self._require_source(source)
        self._advance(receive_ms)
        if self._connected[source] != int(epoch):
            raise ValueError(f"invalid {source} disconnect epoch {epoch}")
        self._connected[source] = None
        self._drop_source(source)

    def _drop_source(self, source: str) -> None:
        self._latest = {
            key: value for key, value in self._latest.items() if key[0] != source
        }

    def observe(
        self,
        source: str,
        *,
        direction: int,
        slot: int,
        receive_ms: float,
        epoch: int,
    ) -> None:
        self._require_source(source)
        timestamp = self._advance(receive_ms)
        if direction not in (-1, 1):
            raise ValueError("candidate direction must be -1 or 1")
        if self._connected[source] != int(epoch):
            raise ValueError(f"{source} candidate outside its active epoch")
        key = (source, int(direction), int(slot))
        if key not in self._latest and len(self._latest) >= self.max_candidates:
            raise ValueError("confirmation candidate bound exceeded")
        self._latest[key] = (timestamp, int(epoch))

    def decide(
        self, *, direction: int, slot: int, receive_ms: float
    ) -> ConfirmationDecision:
        timestamp = self._advance(receive_ms)
        if direction not in (-1, 1):
            raise ValueError("primary direction must be -1 or 1")
        if any(self._connected[source] is None for source in SECONDARY_SOURCES):
            return ConfirmationDecision(False, "secondary_source_unavailable")

        def sources_for(candidate_direction: int, *, equal: bool) -> tuple[str, ...]:
            sources = []
            for source in sorted(SECONDARY_SOURCES):
                observed = self._latest.get((source, candidate_direction, int(slot)))
                if observed is None or observed[1] != self._connected[source]:
                    continue
                is_equal = math.isclose(
                    observed[0], timestamp, rel_tol=0.0, abs_tol=_EPS
                )
                if is_equal == equal and observed[0] <= timestamp + _EPS:
                    sources.append(source)
            return tuple(sources)

        equal_sources = tuple(
            sorted(
                {
                    *sources_for(direction, equal=True),
                    *sources_for(-direction, equal=True),
                }
            )
        )
        if equal_sources:
            return ConfirmationDecision(False, "equal_receipt_ambiguous")
        same = sources_for(direction, equal=False)
        opposite = sources_for(-direction, equal=False)
        if opposite:
            return ConfirmationDecision(False, "opposite_secondary", same, opposite)
        if not same:
            return ConfirmationDecision(False, "no_same_direction_secondary")
        return ConfirmationDecision(True, "confirmed", same, ())


class ShiftedConfirmationWindow:
    """Causal positive-time-shift placebo backed by the same confirmation state."""

    def __init__(
        self,
        *,
        window_ms: float = CONFIRMATION_WINDOW_MS,
        shift_ms: float,
        max_pending: int = 65_536,
        max_lifecycle_events: int = 65_536,
        max_candidates: int = MAX_CONFIRMATION_CANDIDATES,
    ) -> None:
        if not math.isfinite(shift_ms) or shift_ms <= window_ms:
            raise ValueError("placebo shift must exceed the confirmation window")
        if max_pending <= 0:
            raise ValueError("max_pending must be positive")
        if max_lifecycle_events <= 0:
            raise ValueError("max_lifecycle_events must be positive")
        self.shift_ms = float(shift_ms)
        self.max_pending = int(max_pending)
        self.max_lifecycle_events = int(max_lifecycle_events)
        self._window = ConfirmationWindow(
            window_ms,
            max_candidates=max_candidates,
        )
        self._pending: list[tuple[float, int, str, str, int, int, int]] = []
        self._input_connected: dict[str, int | None] = {
            source: None for source in SECONDARY_SOURCES
        }
        self._input_last_epoch = {source: 0 for source in SECONDARY_SOURCES}
        self._pending_candidates = 0
        self._pending_lifecycle_events = 0
        self._sequence = 0
        self._last_input_ms = -math.inf
        self._last_shifted_lifecycle_ms = -math.inf

    @property
    def pending_size(self) -> int:
        return self._pending_candidates

    @property
    def memory_size(self) -> int:
        return self._window.memory_size + len(self._pending)

    def _advance_input(self, receive_ms: float) -> float:
        timestamp = float(receive_ms)
        if not math.isfinite(timestamp):
            raise ValueError("invalid receipt timestamp")
        if timestamp < self._last_input_ms - _EPS:
            raise ValueError("receipt clock regressed")
        self._last_input_ms = max(self._last_input_ms, timestamp)
        return timestamp

    def _prepare(self, receive_ms: float) -> float:
        timestamp = self._advance_input(receive_ms)
        self._release(timestamp)
        self._window.advance(receive_ms=timestamp)
        return timestamp

    def advance(self, *, receive_ms: float) -> None:
        self._prepare(receive_ms)

    def connect(self, source: str, *, epoch: int, receive_ms: float) -> None:
        timestamp = self._prepare(receive_ms)
        self._require_source(source)
        if (
            self._input_connected[source] is not None
            or epoch <= self._input_last_epoch[source]
        ):
            raise ValueError(f"invalid {source} connection epoch {epoch}")
        self._queue_lifecycle("connect", source, int(epoch), timestamp)
        self._input_connected[source] = int(epoch)
        self._input_last_epoch[source] = int(epoch)

    def disconnect(self, source: str, *, epoch: int, receive_ms: float) -> None:
        timestamp = self._prepare(receive_ms)
        self._require_source(source)
        if self._input_connected[source] != int(epoch):
            raise ValueError(f"invalid {source} disconnect epoch {epoch}")
        self._queue_lifecycle("disconnect", source, int(epoch), timestamp)
        self._input_connected[source] = None

    def observe(
        self,
        source: str,
        *,
        direction: int,
        slot: int,
        receive_ms: float,
        epoch: int,
    ) -> None:
        timestamp = self._prepare(receive_ms)
        self._require_source(source)
        if direction not in (-1, 1):
            raise ValueError("candidate direction must be -1 or 1")
        if self._input_connected[source] != int(epoch):
            raise ValueError(f"{source} candidate outside its active epoch")
        if self._pending_candidates >= self.max_pending:
            raise ValueError("placebo pending-candidate bound exceeded")
        self._sequence += 1
        heapq.heappush(
            self._pending,
            (
                timestamp + self.shift_ms,
                self._sequence,
                "candidate",
                source,
                int(direction),
                int(slot),
                int(epoch),
            ),
        )
        self._pending_candidates += 1

    def _require_source(self, source: str) -> None:
        if source not in SECONDARY_SOURCES:
            raise ValueError(f"unknown secondary source: {source}")

    def _queue_lifecycle(
        self,
        action: str,
        source: str,
        epoch: int,
        receive_ms: float,
    ) -> None:
        if self._pending_lifecycle_events >= self.max_lifecycle_events:
            raise ValueError("placebo pending-lifecycle-event bound exceeded")
        self._sequence += 1
        heapq.heappush(
            self._pending,
            (
                receive_ms + self.shift_ms,
                self._sequence,
                action,
                source,
                0,
                0,
                epoch,
            ),
        )
        self._pending_lifecycle_events += 1

    def _release(self, receive_ms: float) -> None:
        while self._pending and self._pending[0][0] <= receive_ms + _EPS:
            due_ms, _sequence, action, source, direction, slot, epoch = (
                heapq.heappop(self._pending)
            )
            if action == "candidate":
                self._pending_candidates -= 1
                self._window.observe(
                    source,
                    direction=direction,
                    slot=slot,
                    receive_ms=due_ms,
                    epoch=epoch,
                )
                continue
            self._pending_lifecycle_events -= 1
            self._last_shifted_lifecycle_ms = due_ms
            if action == "connect":
                self._window.connect(source, epoch=epoch, receive_ms=due_ms)
            elif action == "disconnect":
                self._window.disconnect(source, epoch=epoch, receive_ms=due_ms)
            else:
                raise ValueError(f"unknown shifted lifecycle action: {action}")

    def decide(
        self, *, direction: int, slot: int, receive_ms: float
    ) -> ConfirmationDecision:
        timestamp = self._prepare(receive_ms)
        if math.isclose(
            self._last_shifted_lifecycle_ms,
            timestamp,
            rel_tol=0.0,
            abs_tol=_EPS,
        ):
            return ConfirmationDecision(False, "equal_receipt_ambiguous")
        return self._window.decide(direction=direction, slot=slot, receive_ms=timestamp)


def configs_from_current_h(
    frozen: dict[str, Any],
) -> tuple[replay.ReplayConfig, run.SignalConfig]:
    """Derive Family 36 configs without mutating or replacing current-H logic."""
    protocol = {
        "execution": {
            "evaluation_ms": list(EVALUATION_MS),
            "primary_evaluation_ms": EVALUATION_MS[0],
            "source_z0": dict(source_race.CANDIDATE_SOURCE_Z0),
        },
    }
    receipt_frozen = source_race._receipt_clock_replay_freeze(frozen, protocol)
    execution = replay.config_from_freeze(receipt_frozen)
    execution = replace(
        execution,
        evaluation_ms=EVALUATION_MS,
        base_shares=TARGET_SHARES,
        reentry_enabled=False,
        max_orders_per_min=1,
    )
    return execution, run.signal_config_from_freeze(receipt_frozen)


def _row_sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    arm = str(row.get("arm", ""))
    return (
        float(row.get("signal_receive_ms", math.inf)),
        str(row.get("market_id", "")),
        ARMS.index(arm) if arm in ARMS else len(ARMS),
        float(row.get("evaluation_ms", math.inf)),
        str(row.get("reason", "")),
    )


_RESULT_AVAILABILITY_FIELDS = (
    "result_time_ms",
    "result_receive_ms",
    "result_available_ms",
    "result_ms",
    "outcome_time_ms",
    "outcome_receive_ms",
    "outcome_available_ms",
    "outcome_ms",
    "resolution_time_ms",
    "resolution_receive_ms",
    "resolution_available_ms",
    "resolution_ms",
)
_OBSERVATION_AVAILABILITY_FIELDS = (
    "observation_available_ms",
    "match_time_ms",
    "match_receive_ms",
    "match_ms",
    "fill_time_ms",
    "fill_receive_ms",
    "fill_ms",
    *_RESULT_AVAILABILITY_FIELDS,
)


def _observation_available_ms(row: Mapping[str, Any]) -> float:
    try:
        signal_receive_ms = float(row["signal_receive_ms"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("row has no valid signal receipt timestamp") from exc
    if not math.isfinite(signal_receive_ms):
        raise ValueError("row has no valid signal receipt timestamp")

    evaluation_raw = row.get("evaluation_time_ms")
    if evaluation_raw is None:
        try:
            evaluation_time_ms = signal_receive_ms + float(row["evaluation_ms"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("row has no evaluation availability timestamp") from exc
    else:
        try:
            evaluation_time_ms = float(evaluation_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("row has no evaluation availability timestamp") from exc
    if not math.isfinite(evaluation_time_ms):
        raise ValueError("row has no evaluation availability timestamp")
    if evaluation_time_ms < signal_receive_ms - _EPS:
        raise ValueError("row evaluation precedes signal receipt")

    has_fill = bool(row.get("filled") or row.get("full_fill"))
    filled_shares_raw = row.get("filled_shares")
    if filled_shares_raw is not None:
        try:
            has_fill = has_fill or float(filled_shares_raw) > _EPS
        except (TypeError, ValueError) as exc:
            raise ValueError("row has invalid filled_shares") from exc
    if (
        has_fill
        and row.get("winner") is not None
        and not any(row.get(field) is not None for field in _RESULT_AVAILABILITY_FIELDS)
    ):
        raise ValueError("resolved fill has no result availability timestamp")

    availability = max(signal_receive_ms, evaluation_time_ms)
    for field_name in _OBSERVATION_AVAILABILITY_FIELDS:
        raw_value = row.get(field_name)
        if raw_value is None:
            continue
        try:
            field_time_ms = float(raw_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"row has invalid {field_name}") from exc
        if not math.isfinite(field_time_ms):
            raise ValueError(f"row has invalid {field_name}")
        availability = max(availability, field_time_ms)
    return availability


def _metrics(sample: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    intents = [row for row in sample if not row.get("censored")]
    sent = [row for row in intents if row.get("sent")]
    fills = [row for row in intents if float(row.get("filled_shares") or 0.0) > _EPS]
    full_fills = [row for row in fills if row.get("full_fill")]
    unresolved = [row for row in fills if row.get("pnl") is None]
    complete = not unresolved
    total_pnl = sum(float(row.get("pnl") or 0.0) for row in intents)
    filled_shares = sum(float(row.get("filled_shares") or 0.0) for row in fills)
    resolved_fills = [row for row in fills if row.get("won") is not None]
    resolved_full_fills = [row for row in full_fills if row.get("won") is not None]
    wins = sum(bool(row.get("won")) for row in resolved_fills)
    losses = len(resolved_fills) - wins
    by_source = Counter(str(row.get("signal_source") or "unknown") for row in intents)
    sent_by_source = Counter(str(row.get("signal_source") or "unknown") for row in sent)
    fills_by_source = Counter(
        str(row.get("signal_source") or "unknown") for row in full_fills
    )
    net_ev = total_pnl / filled_shares if complete and filled_shares else None
    exact_p = 1.0
    if net_ev is not None and net_ev > 0 and resolved_full_fills:
        exact_p = run._exact_pvalue([dict(row) for row in resolved_full_fills])
    return {
        "opportunities": len(intents),
        "sent": len(sent),
        "censored": len(sample) - len(intents),
        "fills": len(fills),
        "full_fills": len(full_fills),
        "unresolved_fills": len(unresolved),
        "filled_shares": filled_shares,
        "fill_rate_per_opportunity": len(full_fills) / len(intents) if intents else 0.0,
        "fill_rate_per_send": len(full_fills) / len(sent) if sent else 0.0,
        "intention_to_trade_pnl": total_pnl if complete else None,
        "intention_to_trade_ev_per_opportunity": (
            total_pnl / len(intents) if complete and intents else None
        ),
        "intention_to_trade_ev_per_requested_share": (
            total_pnl / (len(intents) * TARGET_SHARES) if complete and intents else None
        ),
        "net_ev_per_filled_share": net_ev,
        "net_ev_per_share": net_ev,
        "wins": wins,
        "losses": losses,
        "loss_rate": losses / len(resolved_fills) if resolved_fills else 0.0,
        "exact_p": exact_p,
        "reasons": dict(
            sorted(Counter(str(row.get("reason")) for row in sample).items())
        ),
        "opportunities_by_primary_source": dict(sorted(by_source.items())),
        "sent_by_primary_source": dict(sorted(sent_by_source.items())),
        "full_fills_by_primary_source": dict(sorted(fills_by_source.items())),
    }


def summarize(
    rows: Sequence[Mapping[str, Any]],
    counters: Mapping[str, int],
    *,
    stopping_ms: float | None = None,
) -> dict[str, Any]:
    """Build an order-independent ITT summary; this is not a promotion verdict."""
    ordered = sorted(rows, key=_row_sort_key)
    if stopping_ms is not None:
        ordered = [
            row
            for row in ordered
            if _observation_available_ms(row) <= stopping_ms + _EPS
        ]
    arms: dict[str, Any] = {}
    for arm in ARMS:
        arm_rows = [row for row in ordered if row.get("arm") == arm]
        latencies: dict[str, Any] = {}
        for latency in sorted({float(row["evaluation_ms"]) for row in arm_rows}):
            sample = [row for row in arm_rows if float(row["evaluation_ms"]) == latency]
            latencies[f"{latency:g}"] = _metrics(sample)
        arms[arm] = {"latencies": latencies}
    canonical_rows = [dict(row) for row in ordered]
    return {
        "schema": "current-h-source-confirmation-development-v1",
        "family": 36,
        "paper_only": True,
        "promotion_eligible": False,
        "correction_denominator": CORRECTION_DENOMINATOR,
        "confirmation_window_ms": CONFIRMATION_WINDOW_MS,
        "placebo_shift_ms": PLACEBO_SHIFT_MS,
        "stopping_ms": stopping_ms,
        "rows_sha256": source_race._canonical_digest(canonical_rows),
        "counters": dict(sorted(counters.items())),
        "arms": arms,
    }


def common_earliest_stopping_timestamp(
    rows: Sequence[Mapping[str, Any]],
    *,
    minimum_full_fills: int = 1_000,
    minimum_days: int = 7,
    arms: Sequence[str] = ARMS,
    evaluation_ms: Sequence[float] = EVALUATION_MS,
) -> float | None:
    """Return the first observation-availability time when every cell is mature."""
    if minimum_full_fills <= 0 or minimum_days <= 0:
        raise ValueError("stopping requirements must be positive")
    cells = {(str(arm), float(latency)) for arm in arms for latency in evaluation_ms}
    counts = {cell: 0 for cell in cells}
    days: dict[tuple[str, float], set[str]] = {cell: set() for cell in cells}
    eligible = sorted(
        (
            row
            for row in rows
            if (str(row.get("arm")), float(row.get("evaluation_ms", math.nan))) in cells
            and not row.get("censored")
            and row.get("full_fill")
            and row.get("winner") is not None
        ),
        key=lambda row: (_observation_available_ms(row), _row_sort_key(row)),
    )
    for timestamp, group in groupby(
        eligible, key=_observation_available_ms
    ):
        for row in group:
            cell = (str(row["arm"]), float(row["evaluation_ms"]))
            counts[cell] += 1
            days[cell].add(str(row["day"]))
        if all(
            counts[cell] >= minimum_full_fills and len(days[cell]) >= minimum_days
            for cell in cells
        ):
            return timestamp
    return None


_EVENT_SOURCE = {
    "spot_connection": "spot_trade",
    "spot_trade": "spot_trade",
    "spot_bbo": "spot_trade",
    "spot_disconnect": "spot_trade",
    "futures_connection": "futures_book_ticker",
    "futures_bbo": "futures_book_ticker",
    "futures_disconnect": "futures_book_ticker",
    "futures_trade_connection": "futures_trade",
    "futures_trade": "futures_trade",
    "futures_trade_disconnect": "futures_trade",
    "deribit_connection": "deribit_quote",
    "deribit_quote": "deribit_quote",
    "deribit_dvol": "deribit_quote",
    "deribit_disconnect": "deribit_quote",
}
_STREAM_NAME = {
    "spot_trade": "spot",
    "futures_book_ticker": "futures",
    "futures_trade": "futures_trade",
    "deribit_quote": "deribit",
}


@dataclass(frozen=True)
class _LifecycleChange:
    source: str
    action: str
    epoch: int


@dataclass
class _StreamLifecycle:
    epoch: int | None = None
    last_epoch: int = 0
    next_sequence: int = 0


class _SourceLifecycle:
    def __init__(self) -> None:
        self._states = {
            source: _StreamLifecycle()
            for source in (*sorted(PRIMARY_SOURCES), *sorted(SECONDARY_SOURCES))
        }

    def epoch(self, source: str) -> int | None:
        return self._states[source].epoch

    def connected(self, source: str) -> bool:
        return self.epoch(source) is not None

    def apply(self, event: Mapping[str, Any]) -> _LifecycleChange | None:
        kind = str(event["kind"])
        source = _EVENT_SOURCE.get(kind)
        if source is None:
            return None
        state = self._states[source]
        expected_stream = _STREAM_NAME[source]
        supplied_stream = event.get("stream")
        if supplied_stream is not None and str(supplied_stream) != expected_stream:
            raise ValueError(f"invalid source stream {supplied_stream!r} for {kind}")
        stream_sequence = event.get("stream_sequence")
        if stream_sequence is not None:
            sequence = int(stream_sequence)
            if sequence != state.next_sequence:
                raise ValueError(f"non-contiguous {expected_stream} stream sequence")
            state.next_sequence += 1
        supplied_epoch = event.get("connection_epoch")
        connection_kind = kind == f"{expected_stream}_connection"
        disconnect_kind = kind == f"{expected_stream}_disconnect"
        if connection_kind:
            epoch = int(
                supplied_epoch if supplied_epoch is not None else state.last_epoch + 1
            )
            if state.epoch is not None or epoch <= state.last_epoch:
                raise ValueError(f"invalid {expected_stream} connection epoch {epoch}")
            state.epoch = epoch
            state.last_epoch = epoch
            return _LifecycleChange(source, "connect", epoch)
        if state.epoch is None:
            if supplied_epoch is not None or stream_sequence is not None:
                raise ValueError(f"{expected_stream} event outside an active epoch")
            state.epoch = state.last_epoch + 1
            state.last_epoch = state.epoch
            implicit = _LifecycleChange(source, "connect", state.epoch)
        else:
            implicit = None
        if supplied_epoch is not None and int(supplied_epoch) != state.epoch:
            raise ValueError(f"{expected_stream} event epoch mismatch")
        if disconnect_kind:
            epoch = state.epoch
            if epoch is None:
                raise ValueError(
                    f"{expected_stream} disconnect outside an active epoch"
                )
            state.epoch = None
            return _LifecycleChange(source, "disconnect", epoch)
        return implicit


@dataclass
class _ClobLifecycle:
    epoch: int | None = None
    last_epoch: int = 0

    def apply(self, batch: Mapping[str, Any]) -> str | None:
        events = list(batch["events"])
        lifecycle = [
            event
            for event in events
            if event["kind"] in {"clob_connection", "clob_error"}
        ]
        if lifecycle:
            if len(events) != 1:
                raise ValueError("CLOB lifecycle event shares an equal receipt")
            event = lifecycle[0]
            epoch = int(event["connection_epoch"])
            if event["kind"] == "clob_connection":
                if self.epoch is not None or epoch <= self.last_epoch:
                    raise ValueError(f"invalid CLOB connection epoch {epoch}")
                self.epoch = epoch
                self.last_epoch = epoch
                return "connect"
            if self.epoch != epoch:
                raise ValueError(f"invalid CLOB disconnect epoch {epoch}")
            self.epoch = None
            return "disconnect"
        if self.epoch is None:
            raise ValueError("CLOB event outside an active epoch")
        if any(int(event["connection_epoch"]) != self.epoch for event in events):
            raise ValueError("CLOB event epoch mismatch")
        return None


def _clear_trigger_source(trigger: run.TimestampedFirstTrigger, source: str) -> None:
    timestamps = trigger.source_timestamps.get(source)
    log_prices = trigger.source_log_prices.get(source)
    if timestamps is not None:
        timestamps.clear()
    if log_prices is not None:
        log_prices.clear()


@dataclass(frozen=True)
class _ObservedCandidate:
    source: str
    receive_ms: float
    candidate: run.Candidate
    epoch: int


@dataclass(frozen=True)
class _Opportunity:
    opportunity_id: str
    market_id: str
    slot: int
    receive_ms: float
    source_ms: float
    source: str
    direction: int
    fair: float


@dataclass(frozen=True)
class _Dispatch:
    opportunity: _Opportunity
    confirmation: ConfirmationDecision
    fixed_limit: float | None
    decision_ask: float | None
    decision_direct_depth: float


@dataclass
class _ArmState:
    name: str
    machine: replay.HReplay
    markets: dict[int, run.DirectMarketBook]
    tokens: dict[str, tuple[run.DirectMarketBook, str]]
    rings: dict[int, run.SourceRing] = field(default_factory=dict)
    connected: bool = False
    dispatches: dict[str, _Dispatch] = field(default_factory=dict)
    manual_rows: list[dict[str, Any]] = field(default_factory=list)


def _validated_events(
    events: Iterable[dict[str, Any]],
    family: str,
) -> Iterator[dict[str, Any]]:
    previous_receive_ms = -math.inf
    for expected_sequence, event in enumerate(events):
        sequence = int(event["seq"])
        receive_ms = float(event["recv_ms"])
        if sequence != expected_sequence:
            raise ValueError(f"non-contiguous {family} sequence")
        if receive_ms < previous_receive_ms - _EPS:
            raise ValueError(f"{family} receipt clock regressed")
        previous_receive_ms = receive_ms
        yield event


def _source_candidate(
    event: Mapping[str, Any],
    trigger: run.TimestampedFirstTrigger,
) -> tuple[str | None, run.Candidate | None]:
    kind = str(event["kind"])
    if kind == "spot_trade":
        return "spot_trade", trigger.update_trade(
            float(event["source_ts_ms"]),
            float(event["price"]),
        )
    if kind == "futures_bbo":
        return "futures_book_ticker", trigger.update_futures(
            float(event["source_ts_ms"]),
            float(event["bid"]),
            float(event["ask"]),
        )
    if kind == "futures_trade":
        return "futures_trade", trigger.update_source(
            "futures_trade",
            float(event["source_ts_ms"]),
            float(event["price"]),
        )
    if kind == "deribit_quote":
        return "deribit_quote", trigger.update_source(
            "deribit_quote",
            float(event["source_ts_ms"]),
            (float(event["bid"]) + float(event["ask"])) / 2.0,
        )
    return None, None


def _zero_rows(
    arm: _ArmState,
    dispatch: _Dispatch,
    outcomes: Mapping[str, str],
    reason: str,
    execution_config: replay.ReplayConfig,
) -> None:
    opportunity = dispatch.opportunity
    direction = "Up" if opportunity.direction > 0 else "Down"
    winner = outcomes.get(opportunity.market_id)
    day = (
        datetime.fromtimestamp(opportunity.receive_ms / 1_000.0, UTC)
        .date()
        .isoformat()
    )
    for latency in execution_config.evaluation_ms:
        arm.manual_rows.append(
            {
                "arm": arm.name,
                "variant": arm.name,
                "opportunity_id": opportunity.opportunity_id,
                "market_id": opportunity.market_id,
                "ordering_clock": "receive_ms",
                "match_book_clock": "receive_ms",
                "signal_receive_ms": opportunity.receive_ms,
                "signal_source_ms": opportunity.source_ms,
                "signal_source": opportunity.source,
                "signal_ms": opportunity.receive_ms,
                "direction": direction,
                "direct_token": direction,
                "fair": opportunity.fair,
                "decision_ask": dispatch.decision_ask,
                "fixed_limit": dispatch.fixed_limit,
                "decision_direct_depth": dispatch.decision_direct_depth,
                "confirmation_reason": dispatch.confirmation.reason,
                "same_secondary_sources": dispatch.confirmation.same_sources,
                "opposite_secondary_sources": dispatch.confirmation.opposite_sources,
                "sent": False,
                "decision_reason": reason,
                "send_ms": None,
                "signal_to_send_ms": None,
                "evaluation_ms": float(latency),
                "evaluation_time_ms": opportunity.receive_ms + float(latency),
                "target_shares": TARGET_SHARES,
                "eligible_depth": 0.0,
                "filled_shares": 0.0,
                "fill_price": None,
                "fill_fee": 0.0,
                "fill_fee_per_share": None,
                "all_in_cost": None,
                "filled": False,
                "full_fill": False,
                "winner": winner,
                "won": None,
                "pnl_per_share": None,
                "pnl": 0.0,
                "intention_to_trade_pnl": 0.0,
                "day": day,
                "censored": False,
                "reason": reason,
            }
        )


def _dispatch_opportunity(
    arm: _ArmState,
    opportunity: _Opportunity,
    decision: ConfirmationDecision,
    outcomes: Mapping[str, str],
    execution_config: replay.ReplayConfig,
) -> None:
    market = arm.markets[opportunity.slot]
    direction = "Up" if opportunity.direction > 0 else "Down"
    fixed_limit = replay.limit_price(
        opportunity.fair,
        execution_config.theta,
        execution_config.tick,
        execution_config.fee_rate,
    )
    levels = market.effective_asks(direction) if market.ready else ()
    decision_ask = levels[0][0] if levels else None
    decision_depth, _match_price = replay._direct_depth(
        levels,
        fixed_limit,
        TARGET_SHARES,
    )
    dispatch = _Dispatch(
        opportunity,
        decision,
        fixed_limit,
        decision_ask,
        decision_depth,
    )
    arm.dispatches[opportunity.market_id] = dispatch
    if not decision.confirmed:
        _zero_rows(arm, dispatch, outcomes, decision.reason, execution_config)
        return
    if (
        fixed_limit is not None
        and decision_ask is not None
        and decision_ask <= fixed_limit + _EPS
        and decision_depth < TARGET_SHARES - _EPS
    ):
        _zero_rows(
            arm,
            dispatch,
            outcomes,
            "insufficient_decision_depth",
            execution_config,
        )
        return
    run._publish_market_book(arm.machine, market, opportunity.receive_ms)
    arm.machine.feed(
        {
            "kind": "signal",
            "market_id": opportunity.market_id,
            "receive_ms": opportunity.receive_ms,
            "source_ms": opportunity.source_ms,
            "signal_source": opportunity.source,
            "direction": direction,
            "fair": opportunity.fair,
            "trigger_reason": "trigger",
            "trial_edge": None,
            "suppress_unsent_record": False,
        }
    )


def _score_machine_row(
    row: Mapping[str, Any],
    arm: _ArmState,
    outcomes: Mapping[str, str],
    ambiguous_clob_evaluations: set[tuple[str, float]],
) -> dict[str, Any]:
    output = dict(row)
    dispatch = arm.dispatches[str(row["market_id"])]
    opportunity = dispatch.opportunity
    output.update(
        {
            "arm": arm.name,
            "variant": arm.name,
            "opportunity_id": opportunity.opportunity_id,
            "decision_direct_depth": dispatch.decision_direct_depth,
            "confirmation_reason": dispatch.confirmation.reason,
            "same_secondary_sources": dispatch.confirmation.same_sources,
            "opposite_secondary_sources": dispatch.confirmation.opposite_sources,
            "censored": False,
        }
    )
    evaluation_key = (str(row["market_id"]), float(row["evaluation_ms"]))
    if evaluation_key in ambiguous_clob_evaluations:
        output.update(
            {
                "eligible_depth": 0.0,
                "match_price": None,
                "filled_shares": 0.0,
                "fill_price": None,
                "fill_fee": 0.0,
                "fill_fee_per_share": None,
                "all_in_cost": None,
                "filled": False,
                "reason": "equal_receipt_ambiguous",
            }
        )
    filled_shares = float(output.get("filled_shares") or 0.0)
    filled = filled_shares > _EPS
    winner = outcomes.get(opportunity.market_id)
    won = direction_won = None
    pnl: float | None
    if filled and winner is not None:
        direction_won = winner == output["direction"]
        won = direction_won
        payout = filled_shares if direction_won else 0.0
        fill_price = float(output["fill_price"])
        fee = float(output.get("fill_fee") or 0.0)
        pnl = payout - filled_shares * fill_price - fee
    elif filled:
        pnl = None
    else:
        pnl = 0.0
    day = (
        datetime.fromtimestamp(opportunity.receive_ms / 1_000.0, UTC)
        .date()
        .isoformat()
    )
    output.update(
        {
            "winner": winner,
            "won": won,
            "pnl_per_share": pnl / filled_shares
            if pnl is not None and filled
            else None,
            "pnl": pnl,
            "intention_to_trade_pnl": pnl,
            "day": day,
            "filled": filled,
            "full_fill": filled_shares >= TARGET_SHARES - _EPS,
        }
    )
    return output


def replay_normalized(
    source_events: Iterable[dict[str, Any]],
    clob_events: Iterable[dict[str, Any]],
    mappings: Iterable[dict[str, Any]],
    outcomes: Mapping[str, str],
    execution_config: replay.ReplayConfig,
    signal_config: run.SignalConfig,
    *,
    confirmation_window_ms: float = CONFIRMATION_WINDOW_MS,
    placebo_shift_ms: float = PLACEBO_SHIFT_MS,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Replay baseline, confirmation-veto and shifted-placebo arms on one causal stream."""
    family_execution = replace(
        execution_config,
        evaluation_ms=EVALUATION_MS,
        base_shares=TARGET_SHARES,
        signal_time_basis="local_receipt_timestamp",
        book_lag_ms=0.0,
        reentry_enabled=False,
        max_orders_per_min=1,
    )
    family_signal = replace(
        signal_config,
        z0=2.0,
        source_z0=dict(source_race.CANDIDATE_SOURCE_Z0),
    )
    mapping_rows = list(mappings)
    arms: dict[str, _ArmState] = {}
    for name in ARMS:
        markets, tokens = run._mapping_dict(mapping_rows)
        arms[name] = _ArmState(name, replay.HReplay(family_execution), markets, tokens)
    trigger = run.TimestampedFirstTrigger(family_signal)
    lifecycle = _SourceLifecycle()
    clob_lifecycle = _ClobLifecycle()
    confirmation = ConfirmationWindow(confirmation_window_ms)
    placebo = ShiftedConfirmationWindow(
        window_ms=confirmation_window_ms,
        shift_ms=placebo_shift_ms,
    )
    consumed_markets: set[str] = set()
    counters: Counter[str] = Counter()
    ambiguous_clob_evaluations: dict[str, set[tuple[str, float]]] = {
        name: set() for name in ARMS
    }
    baseline_arm = arms["baseline"]

    source = _validated_events(source_events, "source")
    clob = _validated_events(clob_events, "clob")
    merged = run._merged_events(source, clob)
    for receive_ms, group_iterator in groupby(
        merged, key=lambda event: float(event["recv_ms"])
    ):
        confirmation.advance(receive_ms=receive_ms)
        placebo.advance(receive_ms=receive_ms)
        group = list(group_iterator)
        source_group = [event for event in group if event["kind"] != "clob_batch"]
        clob_group = [event for event in group if event["kind"] == "clob_batch"]
        observed: list[_ObservedCandidate] = []
        source_lifecycle_changed = False
        for event in source_group:
            change = lifecycle.apply(event)
            if change is not None:
                source_lifecycle_changed = True
                _clear_trigger_source(trigger, change.source)
            if change is not None and change.source in SECONDARY_SOURCES:
                if change.action == "connect":
                    confirmation.connect(
                        change.source,
                        epoch=change.epoch,
                        receive_ms=receive_ms,
                    )
                    placebo.connect(
                        change.source, epoch=change.epoch, receive_ms=receive_ms
                    )
                else:
                    confirmation.disconnect(
                        change.source,
                        epoch=change.epoch,
                        receive_ms=receive_ms,
                    )
                    placebo.disconnect(
                        change.source, epoch=change.epoch, receive_ms=receive_ms
                    )
            source_name, candidate = _source_candidate(event, trigger)
            counters[str(event["kind"])] += 1
            if source_name is None or candidate is None:
                continue
            epoch = lifecycle.epoch(source_name)
            if epoch is None:
                counters[f"candidate_disconnected_{source_name}"] += 1
                continue
            item = _ObservedCandidate(source_name, receive_ms, candidate, epoch)
            observed.append(item)
            counters[f"candidate_source_{source_name}"] += 1
            if item.source in SECONDARY_SOURCES:
                if item.candidate.slot not in baseline_arm.markets:
                    counters["secondary_no_market"] += 1
                    continue
                confirmation.observe(
                    item.source,
                    direction=item.candidate.direction,
                    slot=item.candidate.slot,
                    receive_ms=receive_ms,
                    epoch=item.epoch,
                )
                placebo.observe(
                    item.source,
                    direction=item.candidate.direction,
                    slot=item.candidate.slot,
                    receive_ms=receive_ms,
                    epoch=item.epoch,
                )

        eligible: list[_Opportunity] = []
        for item in observed:
            if item.source not in PRIMARY_SOURCES or not lifecycle.connected(
                item.source
            ):
                continue
            market = baseline_arm.markets.get(item.candidate.slot)
            if market is None:
                counters["primary_no_market"] += 1
                continue
            if not baseline_arm.connected or not market.ready:
                counters["primary_clob_unavailable"] += 1
                continue
            fair, reason = run._candidate_fair(
                item.candidate,
                baseline_arm.rings.get(item.candidate.slot),
                family_signal,
            )
            counters[f"primary_{reason}"] += 1
            if reason != "trigger":
                continue
            if market.market_id in consumed_markets:
                counters["primary_after_consumption"] += 1
                continue
            eligible.append(
                _Opportunity(
                    opportunity_id=f"{market.market_id}:{receive_ms:g}:{item.source}",
                    market_id=market.market_id,
                    slot=item.candidate.slot,
                    receive_ms=receive_ms,
                    source_ms=item.candidate.timestamp_s * 1_000.0,
                    source=item.source,
                    direction=item.candidate.direction,
                    fair=fair,
                )
            )

        by_market: dict[str, list[_Opportunity]] = {}
        for opportunity in eligible:
            by_market.setdefault(opportunity.market_id, []).append(opportunity)
        for market_id, opportunities in by_market.items():
            opportunity = opportunities[0]
            consumed_markets.add(market_id)
            counters["primary_opportunities"] += 1
            if len(opportunities) > 1 or source_lifecycle_changed or clob_group:
                counters["equal_receipt_primary_ambiguity"] += 1
                ambiguous = ConfirmationDecision(False, "equal_receipt_ambiguous")
                decisions = {name: ambiguous for name in ARMS}
            else:
                decisions = {
                    "baseline": ConfirmationDecision(True, "primary_control"),
                    "confirmation_veto": confirmation.decide(
                        direction=opportunity.direction,
                        slot=opportunity.slot,
                        receive_ms=receive_ms,
                    ),
                    "receipt_time_shifted_placebo": placebo.decide(
                        direction=opportunity.direction,
                        slot=opportunity.slot,
                        receive_ms=receive_ms,
                    ),
                }
            for name in ARMS:
                _dispatch_opportunity(
                    arms[name],
                    opportunity,
                    decisions[name],
                    outcomes,
                    family_execution,
                )
                counters[f"{name}_{decisions[name].reason}"] += 1

        for arm in arms.values():
            arm.machine.feed(
                {
                    "kind": "watermark",
                    "receive_ms": receive_ms,
                    "source_ms": receive_ms,
                }
            )
        for batch in clob_group:
            clob_change = clob_lifecycle.apply(batch)
            for arm in arms.values():
                for pending in arm.machine.pending_evaluations():
                    if pending["sent"] and math.isclose(
                        float(pending["evaluation_time_ms"]),
                        receive_ms,
                        rel_tol=0.0,
                        abs_tol=_EPS,
                    ):
                        ambiguous_clob_evaluations[arm.name].add(
                            (
                                str(pending["market_id"]),
                                float(pending["evaluation_ms"]),
                            )
                        )
                if clob_change is not None:
                    arm.rings.clear()
                arm.connected = run._apply_clob_batch(
                    batch,
                    arm.machine,
                    arm.tokens,
                    arm.rings,
                    arm.connected,
                    materialize_idle_books=False,
                )
            counters["clob_batches"] += 1

    rows: list[dict[str, Any]] = []
    for arm in arms.values():
        arm.machine.finish(force=False)
        rows.extend(arm.manual_rows)
        rows.extend(
            _score_machine_row(
                row,
                arm,
                outcomes,
                ambiguous_clob_evaluations[arm.name],
            )
            for row in arm.machine.records
        )
        for pending in arm.machine.pending_evaluations():
            dispatch = arm.dispatches[str(pending["market_id"])]
            reason = str(pending["decision_reason"])
            if pending["sent"]:
                reason = "right_censored"
            before = len(arm.manual_rows)
            _zero_rows(arm, dispatch, outcomes, reason, family_execution)
            pending_rows = arm.manual_rows[before:]
            latency = float(pending["evaluation_ms"])
            matching = [
                row for row in pending_rows if float(row["evaluation_ms"]) == latency
            ]
            if pending["sent"]:
                for row in matching:
                    row["censored"] = True
                    row["pnl"] = None
                    row["intention_to_trade_pnl"] = None
            rows.extend(matching)
            del arm.manual_rows[before:]
    rows.sort(key=_row_sort_key)
    return rows, dict(sorted(counters.items()))


def _atomic_write_rows(path: str | Path, rows: Sequence[Mapping[str, Any]]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
    temporary.replace(destination)


def replay_archive(
    archive_dir: str | Path,
    *,
    rows_out: str | Path | None = None,
    prospective_cutoff_ms: float = PROSPECTIVE_CUTOFF_MS,
) -> dict[str, Any]:
    """Run Family 36 on one immutable standard strict recording artifact."""
    cutoff_ms = float(prospective_cutoff_ms)
    if not math.isfinite(cutoff_ms):
        raise ValueError("prospective cutoff must be finite")

    root = Path(archive_dir)
    identity, artifact_stats, standard = source_race._capture_artifact_identity(root)
    if identity.get("receipt_race_ready") is not True:
        raise ValueError("Family 36 requires a receipt-race-ready artifact")
    checked = archive.validate_standard_artifact(standard)
    manifest = checked["manifest"]
    for key in ("run_id", "collector_region", "receipt_race_ready", "started_ms", "ended_ms"):
        if manifest.get(key) != identity.get(key):
            raise ValueError(f"artifact identity drift for {key}")

    mappings = list(archive.iter_market_mappings(standard / "market_registry.csv.gz"))
    mapping_by_market = {str(row["market_id"]): row for row in mappings}
    if len(mapping_by_market) != len(mappings):
        raise ValueError("duplicate market mapping")
    outcome_rows = list(archive.iter_outcomes(standard / "market_outcomes.csv.gz"))
    outcomes: dict[str, str] = {}
    result_available_ms: dict[str, float] = {}
    for outcome in outcome_rows:
        market_id = str(outcome["market_id"])
        if market_id in outcomes:
            raise ValueError(f"duplicate market outcome: {market_id}")
        mapping = mapping_by_market.get(market_id)
        if mapping is None:
            raise ValueError(f"market outcome has no direct-token mapping: {market_id}")
        outcomes[market_id] = str(outcome["winner"])
        recorded_ms = float(outcome["recv_ms"])
        market_end_ms = (float(mapping["slot"]) + 300.0) * 1_000.0
        if not math.isfinite(recorded_ms):
            raise ValueError(f"market outcome has invalid receipt time: {market_id}")
        result_available_ms[market_id] = max(recorded_ms, market_end_ms)

    frozen = replay.load_freeze()
    execution, signal = configs_from_current_h(frozen)
    rows, counters = replay_normalized(
        archive.iter_normalized_events(
            standard / "source_events.jsonl.gz", family="source"
        ),
        archive.iter_normalized_events(
            standard / "clob_events.jsonl.gz", family="clob"
        ),
        mappings,
        outcomes,
        execution,
        signal,
    )
    if source_race._artifact_stats(standard) != artifact_stats:
        raise ValueError("strict artifact changed during Family 36 replay")

    observation_run_id = str(identity.get("run_id") or root.name)
    artifact_files_sha256 = source_race._canonical_digest(identity)
    eligible_rows: list[dict[str, Any]] = []
    excluded_pre_cutoff_rows = 0
    for original in rows:
        row = dict(original)
        try:
            signal_receive_ms = float(row["signal_receive_ms"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Family 36 row lacks a valid receipt timestamp") from exc
        if not math.isfinite(signal_receive_ms):
            raise ValueError("Family 36 row lacks a valid receipt timestamp")
        if signal_receive_ms < cutoff_ms - _EPS:
            excluded_pre_cutoff_rows += 1
            continue
        row["observation_run_id"] = observation_run_id
        row["strategy_id"] = STRATEGY_ID
        row["family"] = 36
        row["artifact_started_ms"] = identity.get("started_ms")
        row["artifact_ended_ms"] = identity.get("ended_ms")
        row["artifact_collector_region"] = identity.get("collector_region")
        row["artifact_receipt_race_ready"] = identity.get("receipt_race_ready") is True
        row["artifact_files_sha256"] = artifact_files_sha256
        if (
            row.get("winner") is not None
            and float(row.get("filled_shares") or 0.0) > _EPS
        ):
            market_id = str(row.get("market_id"))
            if market_id not in result_available_ms:
                raise ValueError(f"resolved row has no observable outcome time: {market_id}")
            row["result_time_ms"] = result_available_ms[market_id]
        eligible_rows.append(row)
    eligible_rows.sort(key=_row_sort_key)
    artifact_row_count = len(eligible_rows)
    artifact_rows_sha256 = source_race._canonical_digest(eligible_rows)
    for row in eligible_rows:
        row["artifact_row_count"] = artifact_row_count
        row["artifact_rows_sha256"] = artifact_rows_sha256
    if rows_out is not None:
        _atomic_write_rows(rows_out, eligible_rows)

    pending_outcomes = sorted(
        {
            str(row["market_id"])
            for row in eligible_rows
            if row.get("winner") is None
        }
    )
    summary = summarize(eligible_rows, counters)
    summary["dataset"] = {
        "sample_scope": "prospective_standard_forward_artifact",
        "archive": root.name,
        "run_id": identity.get("run_id"),
        "collector_region": identity.get("collector_region"),
        "started_ms": identity.get("started_ms"),
        "ended_ms": identity.get("ended_ms"),
        "receipt_race_ready": identity.get("receipt_race_ready") is True,
        "artifact_files_sha256": artifact_files_sha256,
        "artifact_row_count": artifact_row_count,
        "artifact_rows_sha256": artifact_rows_sha256,
        "mapped_markets": len(mappings),
        "outcomes": len(outcomes),
        "prospective_cutoff_ms": cutoff_ms,
        "raw_rows": len(rows),
        "excluded_pre_cutoff_rows": excluded_pre_cutoff_rows,
        "eligible_rows": len(eligible_rows),
        "pending_signal_market_outcomes": pending_outcomes,
        "paper_gate_eligible": bool(
            identity.get("collector_region") == "eu-west-1"
            and identity.get("receipt_race_ready") is True
            and not pending_outcomes
            and float(identity.get("started_ms") or -math.inf) >= cutoff_ms
            and float(identity.get("ended_ms") or -math.inf) >= cutoff_ms
        ),
    }
    summary["protocol"] = {
        "strategy_id": STRATEGY_ID,
        "primary_sources": sorted(PRIMARY_SOURCES),
        "secondary_sources": sorted(SECONDARY_SOURCES),
        "confirmation_window_ms": CONFIRMATION_WINDOW_MS,
        "placebo_shift_ms": PLACEBO_SHIFT_MS,
        "evaluation_ms": list(EVALUATION_MS),
        "target_shares": TARGET_SHARES,
        "result_time_basis": "max_market_end_and_outcome_receipt",
    }
    summary["record_count"] = len(eligible_rows)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True)
    parser.add_argument("--rows-out")
    args = parser.parse_args()
    print(
        json.dumps(
            replay_archive(args.archive, rows_out=args.rows_out),
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
    )


if __name__ == "__main__":
    main()
