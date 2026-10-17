"""Receipt-causal cross-asset breadth audit for BTC five-minute binaries.

The economic hypothesis is independent of the stale-quote trigger: broad same-direction moves in
ETH, SOL, XRP and DOGE may lead BTC while BTC's own one-second return is still quiet.  Signal
features use only local receive time.  Execution freezes a direct-token five-share limit at the
decision and reads only direct Up/Down depth at 400/500 ms.
"""

from __future__ import annotations

import argparse
import gzip
import heapq
import json
import math
from collections import Counter, deque
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal
from itertools import groupby
from pathlib import Path
from typing import Any

import h_replay_archive as archive
from basis_wedge_run import _fill_direct as _legacy_fill_direct
from h_replay_run import TokenBook, _exact_pvalue


ASSETS = ("btc", "eth", "sol", "xrp", "doge")
ALT_ASSETS = ASSETS[1:]
BASE_VARIANT = "base"
CONTROL_VARIANTS = ("direction_reversal", "time_shift")
PRICE_SIDECAR_SCHEMA = "polymarket-binance-price-sidecar-v1"
SOURCE_COVERAGE_TOLERANCE_MS = 2_000.0
_EPS = 1e-9
_FEE_QUANTUM = Decimal("0.00001")
_FEE_RATE_072_UNTIL_MS = 1_782_864_000_000.0


@dataclass(frozen=True)
class BreadthConfig:
    return_horizon_ms: float = 1_000.0
    sigma_window_s: int = 600
    sigma_min_observations: int = 300
    forward_fill_ms: float = 2_000.0
    latest_max_age_ms: float = 250.0
    reference_max_age_ms: float = 250.0
    alt_z_threshold: float = 2.0
    btc_quiet_z: float = 0.5
    minimum_consensus: int = 2
    minimum_remaining_s: float = 60.0
    maximum_remaining_s: float = 240.0
    evaluation_ms: tuple[float, ...] = (400.0, 500.0)
    target_shares: float = 5.0
    limit_slack: float = 0.01
    maximum_limit: float = 0.99
    minimum_order_usd: float = 1.0
    book_fresh_ms: float = 2_000.0
    time_shift_ms: float = 5_000.0
    family_tests: int = 200

    def __post_init__(self) -> None:
        positive = (
            self.return_horizon_ms,
            self.sigma_window_s,
            self.sigma_min_observations,
            self.forward_fill_ms,
            self.latest_max_age_ms,
            self.reference_max_age_ms,
            self.alt_z_threshold,
            self.btc_quiet_z,
            self.minimum_consensus,
            self.minimum_remaining_s,
            self.maximum_remaining_s,
            self.target_shares,
            self.limit_slack,
            self.maximum_limit,
            self.minimum_order_usd,
            self.book_fresh_ms,
            self.time_shift_ms,
            self.family_tests,
        )
        if any(not math.isfinite(float(value)) or float(value) <= 0 for value in positive):
            raise ValueError("breadth parameters must be finite and positive")
        if self.sigma_min_observations > self.sigma_window_s:
            raise ValueError("sigma minimum exceeds its causal window")
        if self.minimum_consensus > len(ALT_ASSETS):
            raise ValueError("consensus exceeds the alt-asset universe")
        if self.maximum_remaining_s <= self.minimum_remaining_s:
            raise ValueError("active market interval is empty")
        if not self.evaluation_ms or any(value < 0 for value in self.evaluation_ms):
            raise ValueError("execution latencies must be non-negative")
        if not 0 < self.maximum_limit < 1:
            raise ValueError("maximum limit must be inside the binary price interval")


@dataclass(frozen=True)
class BreadthSignal:
    decision_recv_ms: float
    slot: int
    direction: int
    contributors: tuple[str, ...]
    z_scores: dict[str, float]


@dataclass
class _AssetState:
    config: BreadthConfig
    current_second: int | None = None
    last_event_ms: float = -math.inf
    last_log_price: float | None = None
    previous_close_second: int | None = None
    previous_close_log: float | None = None
    recent_prices: deque[tuple[float, float]] = field(default_factory=deque)
    returns: deque[tuple[int, float]] = field(default_factory=deque)
    return_sum: float = 0.0
    return_square_sum: float = 0.0

    def _add_return(self, second: int, value: float) -> None:
        self.returns.append((second, value))
        self.return_sum += value
        self.return_square_sum += value * value

    def _trim_returns(self, current_second: int) -> None:
        cutoff = current_second - self.config.sigma_window_s
        while self.returns and self.returns[0][0] < cutoff:
            _, value = self.returns.popleft()
            self.return_sum -= value
            self.return_square_sum -= value * value

    def advance(self, recv_ms: float) -> None:
        target_second = math.floor(recv_ms / 1_000.0)
        if self.current_second is None:
            return
        while self.current_second < target_second:
            boundary_ms = (self.current_second + 1) * 1_000.0
            close_log = (
                self.last_log_price
                if self.last_log_price is not None
                and 0 <= boundary_ms - self.last_event_ms <= self.config.forward_fill_ms
                else None
            )
            if (
                close_log is not None
                and self.previous_close_log is not None
                and self.previous_close_second == self.current_second - 1
            ):
                self._add_return(
                    self.current_second,
                    close_log - self.previous_close_log,
                )
            self.previous_close_second = self.current_second if close_log is not None else None
            self.previous_close_log = close_log
            self.current_second += 1
            self._trim_returns(self.current_second)

    def update(self, recv_ms: float, price: float) -> None:
        if not math.isfinite(recv_ms) or not math.isfinite(price) or price <= 0:
            raise ValueError("invalid receipt-clock trade")
        if recv_ms < self.last_event_ms:
            raise ValueError("trade receipt clock regressed")
        if self.current_second is None:
            self.current_second = math.floor(recv_ms / 1_000.0)
        self.advance(recv_ms)
        log_price = math.log(price)
        self.last_event_ms = recv_ms
        self.last_log_price = log_price
        self.recent_prices.append((recv_ms, log_price))
        keep_after = recv_ms - self.config.return_horizon_ms - self.config.reference_max_age_ms
        while len(self.recent_prices) > 1 and self.recent_prices[1][0] <= keep_after:
            self.recent_prices.popleft()

    def sigma(self) -> float | None:
        count = len(self.returns)
        if count < self.config.sigma_min_observations or count < 2:
            return None
        variance = (
            self.return_square_sum - self.return_sum * self.return_sum / count
        ) / (count - 1)
        if variance <= 0 or not math.isfinite(variance):
            return None
        return math.sqrt(variance)

    def z_score(self, recv_ms: float) -> float | None:
        self.advance(recv_ms)
        sigma = self.sigma()
        if (
            sigma is None
            or self.last_log_price is None
            or not 0 <= recv_ms - self.last_event_ms <= self.config.latest_max_age_ms
        ):
            return None
        target_ms = recv_ms - self.config.return_horizon_ms
        reference = next(
            (item for item in reversed(self.recent_prices) if item[0] <= target_ms + _EPS),
            None,
        )
        if reference is None or target_ms - reference[0] > self.config.reference_max_age_ms:
            return None
        return (self.last_log_price - reference[1]) / sigma


def detect_signals(
    events: Iterable[Mapping[str, Any]],
    config: BreadthConfig | None = None,
    diagnostics: Counter[str] | None = None,
) -> list[BreadthSignal]:
    cfg = config or BreadthConfig()
    audit = diagnostics if diagnostics is not None else Counter()
    states = {asset: _AssetState(cfg) for asset in ASSETS}
    connected: dict[str, bool | None] = {asset: None for asset in ASSETS}
    epochs: dict[str, int | None] = {asset: None for asset in ASSETS}
    signals: list[BreadthSignal] = []
    signaled_slots: set[int] = set()
    previous_recv_ms = -math.inf
    for recv_ms, grouped in groupby(events, key=lambda row: float(row["recv_ms"])):
        audit["receipt_batches"] += 1
        if not math.isfinite(recv_ms) or recv_ms < previous_recv_ms:
            raise ValueError("trade events must be finite and receipt ordered")
        previous_recv_ms = recv_ms
        batch = list(grouped)
        for state in states.values():
            state.advance(recv_ms)
        updated_assets: set[str] = set()
        for event in batch:
            asset = str(event["asset"]).lower()
            if asset not in states:
                raise ValueError(f"unknown breadth asset: {asset}")
            kind = str(event.get("kind") or "price")
            if kind == "connection":
                states[asset] = _AssetState(cfg)
                connected[asset] = True
                epochs[asset] = int(event["connection_epoch"])
                audit[f"connections_{asset}"] += 1
                continue
            if kind == "disconnect":
                if epochs[asset] != int(event["connection_epoch"]):
                    audit[f"epoch_mismatch_{asset}"] += 1
                    continue
                states[asset] = _AssetState(cfg)
                connected[asset] = False
                audit[f"disconnects_{asset}"] += 1
                continue
            if kind != "price":
                raise ValueError(f"unknown breadth event kind: {kind}")
            if connected[asset] is False:
                audit[f"ignored_disconnected_{asset}"] += 1
                continue
            if (
                event.get("lifecycle_proven")
                and (
                    connected[asset] is not True
                    or epochs[asset] != int(event["connection_epoch"])
                )
            ):
                audit[f"epoch_mismatch_{asset}"] += 1
                states[asset] = _AssetState(cfg)
                connected[asset] = False
                continue
            states[asset].update(recv_ms, float(event["price"]))
            updated_assets.add(asset)
            audit[
                "strict_price_events" if event.get("lifecycle_proven") else "legacy_price_events"
            ] += 1
        if not updated_assets.intersection(ALT_ASSETS):
            audit["btc_only_batches"] += 1
            continue
        audit["alt_update_batches"] += 1
        z_scores = {asset: states[asset].z_score(recv_ms) for asset in ASSETS}
        if any(value is None for value in z_scores.values()):
            audit["incomplete_features"] += 1
            for asset, value in z_scores.items():
                if value is None:
                    audit[f"incomplete_{asset}"] += 1
            continue
        audit["complete_features"] += 1
        numeric = {asset: float(value) for asset, value in z_scores.items() if value is not None}
        if abs(numeric["btc"]) > cfg.btc_quiet_z:
            audit["btc_not_quiet"] += 1
            continue
        audit["btc_quiet"] += 1
        positive = tuple(asset for asset in ALT_ASSETS if numeric[asset] >= cfg.alt_z_threshold)
        negative = tuple(asset for asset in ALT_ASSETS if numeric[asset] <= -cfg.alt_z_threshold)
        if len(positive) >= cfg.minimum_consensus and len(negative) < cfg.minimum_consensus:
            direction, contributors = 1, positive
        elif len(negative) >= cfg.minimum_consensus and len(positive) < cfg.minimum_consensus:
            direction, contributors = -1, negative
        else:
            audit["no_unique_consensus"] += 1
            continue
        audit["unique_consensus"] += 1
        slot = math.floor(recv_ms / 300_000.0) * 300
        remaining_s = slot + 300.0 - recv_ms / 1_000.0
        if not cfg.minimum_remaining_s <= remaining_s <= cfg.maximum_remaining_s:
            audit["outside_active_window"] += 1
            continue
        if slot in signaled_slots:
            audit["duplicate_market_signal"] += 1
            continue
        signaled_slots.add(slot)
        signals.append(BreadthSignal(
            decision_recv_ms=recv_ms,
            slot=slot,
            direction=direction,
            contributors=contributors,
            z_scores=numeric,
        ))
        audit["signals"] += 1
    return signals


def iter_trade_file(
    asset: str,
    path: str | Path,
    audit: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    previous_recv_ms = -math.inf
    previous_sequence = -1
    previous_raw_sequence = -1
    active_epoch = None
    last_epoch = 0
    strict_rows = 0
    legacy_rows = 0
    first_recv_ms = None
    last_recv_ms = None
    metadata: dict[str, Any] | None = None
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        for line_number, raw in enumerate(stream, 1):
            try:
                row = json.loads(raw)
                if row.get("asset") is not None and str(row["asset"]).lower() != asset:
                    raise ValueError("price sidecar asset mismatch")
                if row.get("kind") == "metadata":
                    if metadata is not None:
                        raise ValueError("duplicate price sidecar metadata")
                    metadata = dict(row)
                    continue
                if metadata is not None:
                    raise ValueError("price sidecar metadata must be the final row")
                if row.get("event") == "BINANCE_WS_TRADE":
                    if strict_rows:
                        raise ValueError("mixed legacy and strict price rows")
                    recv_ms = float(row["receive_ts"]) * 1_000.0
                    event = {
                        "asset": asset,
                        "kind": "price",
                        "recv_ms": recv_ms,
                        "price": float(row["price"]),
                        "order": line_number,
                        "lifecycle_proven": False,
                    }
                    legacy_rows += 1
                elif row.get("kind") in {"connection", "disconnect"}:
                    if legacy_rows:
                        raise ValueError("mixed legacy and strict price rows")
                    recv_ms = float(row["recv_ms"])
                    sequence = int(row["sequence"])
                    raw_sequence = int(row["raw_sequence"])
                    epoch = int(row["connection_epoch"])
                    if sequence != previous_sequence + 1 or raw_sequence <= previous_raw_sequence:
                        raise ValueError("non-contiguous price sidecar sequence")
                    if row["kind"] == "connection":
                        if active_epoch is not None or epoch <= last_epoch:
                            raise ValueError("invalid price sidecar connection epoch")
                        active_epoch = last_epoch = epoch
                    elif active_epoch != epoch:
                        raise ValueError("invalid price sidecar disconnect epoch")
                    event = {
                        "asset": asset,
                        "kind": str(row["kind"]),
                        "recv_ms": recv_ms,
                        "order": sequence,
                        "connection_epoch": epoch,
                        "lifecycle_proven": True,
                    }
                    previous_sequence = sequence
                    previous_raw_sequence = raw_sequence
                    strict_rows += 1
                    if row["kind"] == "disconnect":
                        active_epoch = None
                elif row.get("kind") in {"trade", "bookTicker"}:
                    if legacy_rows:
                        raise ValueError("mixed legacy and strict price rows")
                    recv_ms = float(row["recv_ms"])
                    sequence = int(row["sequence"])
                    raw_sequence = int(row["raw_sequence"])
                    epoch = int(row["connection_epoch"])
                    source_timestamp = row.get("source_ts_ms")
                    source_ts_ms = (
                        None if source_timestamp is None else float(source_timestamp)
                    )
                    if sequence != previous_sequence + 1 or raw_sequence <= previous_raw_sequence:
                        raise ValueError("non-contiguous price sidecar sequence")
                    if active_epoch != epoch:
                        raise ValueError("price row outside its connection epoch")
                    if source_ts_ms is not None and not math.isfinite(source_ts_ms):
                        raise ValueError("invalid price source timestamp")
                    event = {
                        "asset": asset,
                        "kind": "price",
                        "recv_ms": recv_ms,
                        "price": float(row["price"] if row["kind"] == "trade" else row["mid"]),
                        "order": sequence,
                        "connection_epoch": epoch,
                        "lifecycle_proven": True,
                    }
                    previous_sequence = sequence
                    previous_raw_sequence = raw_sequence
                    strict_rows += 1
                else:
                    raise ValueError("unknown price sidecar row")
                price = event.get("price")
                numeric_price = None if price is None else float(str(price))
                if (
                    recv_ms < previous_recv_ms
                    or not math.isfinite(recv_ms)
                    or (
                        numeric_price is not None
                        and (not math.isfinite(numeric_price) or numeric_price <= 0)
                    )
                ):
                    raise ValueError("invalid or regressed price event")
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"{Path(path).name}:{line_number}: invalid price row") from exc
            previous_recv_ms = recv_ms
            first_recv_ms = recv_ms if first_recv_ms is None else first_recv_ms
            last_recv_ms = recv_ms
            yield event
    if strict_rows:
        if active_epoch is not None:
            raise ValueError(f"{Path(path).name}: price sidecar ended with an open epoch")
        if metadata is None or metadata.get("schema") != PRICE_SIDECAR_SCHEMA:
            raise ValueError(f"{Path(path).name}: missing strict price sidecar metadata")
        if not bool(metadata.get("recorder_complete")):
            raise ValueError(f"{Path(path).name}: source recorder is incomplete")
        if int(metadata.get("emitted_event_count", -1)) != strict_rows:
            raise ValueError(f"{Path(path).name}: strict event count mismatch")
        raw_count = int(metadata.get("raw_event_count", -1))
        raw_started_ms = float(metadata["raw_started_ms"])
        raw_ended_ms = float(metadata["raw_ended_ms"])
        if (
            raw_count < strict_rows
            or first_recv_ms is None
            or last_recv_ms is None
            or not all(math.isfinite(value) for value in (raw_started_ms, raw_ended_ms))
            or raw_started_ms > first_recv_ms
            or raw_ended_ms < last_recv_ms
        ):
            raise ValueError(f"{Path(path).name}: invalid strict source coverage")
        mode = "strict"
    elif metadata is not None:
        raise ValueError(f"{Path(path).name}: metadata has no strict events")
    else:
        mode = "legacy"
    if audit is not None:
        audit.update({
            "mode": mode,
            "strict_rows": strict_rows,
            "legacy_rows": legacy_rows,
            "metadata": metadata,
            "first_recv_ms": first_recv_ms,
            "last_recv_ms": last_recv_ms,
        })


def iter_trade_files(
    paths: Mapping[str, str | Path],
    audits: dict[str, dict[str, Any]] | None = None,
) -> Iterator[dict[str, Any]]:
    missing = set(ASSETS) - {str(asset).lower() for asset in paths}
    if missing:
        raise ValueError(f"missing breadth trade files: {', '.join(sorted(missing))}")
    target_audits = audits if audits is not None else {}
    streams = [
        iter_trade_file(asset, paths[asset], target_audits.setdefault(asset, {}))
        for asset in ASSETS
    ]
    yield from heapq.merge(
        *streams,
        key=lambda row: (
            float(row["recv_ms"]),
            int(row.get("order") or 0),
            ASSETS.index(str(row["asset"])),
        ),
    )


def _source_bundle_proven(
    audits: Mapping[str, Mapping[str, Any]],
    manifest: Mapping[str, Any],
) -> bool:
    """Bind all strict sidecars to one complete recorder run and the BTC CLOB archive."""
    if any(audits.get(asset, {}).get("mode") != "strict" for asset in ASSETS):
        return False
    metadata: list[Mapping[str, Any]] = []
    for asset in ASSETS:
        item = audits[asset].get("metadata")
        if not isinstance(item, Mapping):
            raise ValueError("strict source sidecar metadata is incomplete")
        metadata.append(item)
    identities = {
        (
            item.get("run_id"),
            item.get("collector_region"),
            float(item["raw_started_ms"]),
            float(item["raw_ended_ms"]),
            int(item["raw_event_count"]),
        )
        for item in metadata
    }
    if len(identities) != 1:
        raise ValueError("price sidecars do not belong to one recorder run")
    run_id, collector_region, source_start_ms, source_end_ms, _raw_count = next(iter(identities))
    if run_id != manifest.get("run_id") or collector_region != manifest.get("collector_region"):
        raise ValueError("price sidecars do not match the CLOB recorder identity")
    archive_start_ms = float(manifest["started_ms"])
    archive_end_ms = float(manifest["ended_ms"])
    if (
        source_start_ms > archive_start_ms + SOURCE_COVERAGE_TOLERANCE_MS
        or source_end_ms < archive_end_ms - SOURCE_COVERAGE_TOLERANCE_MS
    ):
        raise ValueError("price sidecars do not cover the CLOB recording interval")
    return run_id not in (None, "")


@dataclass
class _MarketState:
    market_id: str
    slot: int
    up_token_id: str
    down_token_id: str
    fee_rate: float
    books: dict[str, TokenBook] = field(default_factory=dict)
    book_recv_ms: dict[str, float] = field(default_factory=dict)

    def reset(self) -> None:
        self.books.clear()
        self.book_recv_ms.clear()


@dataclass(frozen=True)
class _Pending:
    due_ms: float
    sequence: int
    evaluation_ms: float
    epoch: int
    decision: Mapping[str, Any]


@dataclass(frozen=True)
class _Delayed:
    due_ms: float
    sequence: int
    epoch: int
    signal: BreadthSignal
    parent_signal_id: str


def _ceil_cent(value: float, increment: float = 0.0) -> float:
    return float(
        (Decimal(str(value)) + Decimal(str(increment))).quantize(
            Decimal("0.01"), rounding=ROUND_CEILING,
        )
    )


def _decision_price(
    book: TokenBook,
    config: BreadthConfig,
) -> tuple[str | None, float | None, float | None, tuple[tuple[float, float], ...]]:
    if not book.ready or not book.asks:
        return "book_unavailable", None, None, ()
    remaining = config.target_shares
    notional = 0.0
    levels: list[tuple[float, float]] = []
    for price, available in sorted(book.asks.items()):
        take = min(remaining, available)
        if take <= 0:
            continue
        levels.append((price, take))
        notional += price * take
        remaining -= take
        if remaining <= _EPS:
            break
    if remaining > _EPS:
        return "insufficient_decision_depth", None, None, tuple(levels)
    fixed_limit = min(
        config.maximum_limit,
        _ceil_cent(levels[-1][0], config.limit_slack),
    )
    if fixed_limit + _EPS < levels[-1][0]:
        return "decision_ask_above_maximum", None, None, tuple(levels)
    if fixed_limit * config.target_shares + _EPS < config.minimum_order_usd:
        return "below_venue_minimum", None, None, tuple(levels)
    return None, notional / config.target_shares, fixed_limit, tuple(levels)


def _fill_direct(
    book: TokenBook,
    fixed_limit: float,
    target_shares: float,
    fee_rate: float,
) -> tuple[
    str,
    float,
    float | None,
    float | None,
    float | None,
    tuple[tuple[float, float], ...],
]:
    """Reuse direct-depth matching but apply the venue's fee truncation."""
    reason, shares, vwap, _fee, notional, levels = _legacy_fill_direct(
        book, fixed_limit, target_shares, fee_rate,
    )
    fee = None
    if shares > _EPS:
        rate = Decimal(str(fee_rate))
        raw_fee = sum(
            (
                Decimal(str(size)) * rate * Decimal(str(price))
                * (Decimal(1) - Decimal(str(price)))
                for price, size in levels
            )
            , Decimal(0)
        )
        fee = float(raw_fee.quantize(_FEE_QUANTUM, rounding=ROUND_DOWN))
    return reason, shares, vwap, fee, notional, levels


def replay_execution(
    clob_events: Iterable[Mapping[str, Any]],
    signals: Iterable[BreadthSignal],
    mappings: Iterable[Mapping[str, Any]],
    outcomes: Mapping[str, str],
    *,
    config: BreadthConfig | None = None,
    evaluation_ms: Sequence[float] | None = None,
    include_controls: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    cfg = config or BreadthConfig()
    latencies = tuple(cfg.evaluation_ms if evaluation_ms is None else evaluation_ms)
    if not latencies or any(not math.isfinite(value) or value < 0 for value in latencies):
        raise ValueError("execution latencies must be finite and non-negative")
    markets: dict[int, _MarketState] = {}
    tokens: dict[str, _MarketState] = {}
    for row in mappings:
        slot = int(row["slot"])
        raw_fee_rate = row.get("fee_rate")
        fee_rate = (
            float(raw_fee_rate)
            if raw_fee_rate is not None
            else (0.072 if slot * 1_000 < _FEE_RATE_072_UNTIL_MS else 0.07)
        )
        state = _MarketState(
            market_id=str(row["market_id"]),
            slot=slot,
            up_token_id=str(row["up_token_id"]),
            down_token_id=str(row["down_token_id"]),
            fee_rate=fee_rate,
        )
        if state.slot in markets:
            raise ValueError(f"duplicate market slot: {state.slot}")
        if (
            state.up_token_id == state.down_token_id
            or state.up_token_id in tokens
            or state.down_token_id in tokens
        ):
            raise ValueError(f"duplicate direct token mapping: {state.market_id}")
        markets[state.slot] = state
        tokens[state.up_token_id] = state
        tokens[state.down_token_id] = state

    counters: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    pending: list[tuple[float, int, _Pending]] = []
    delayed: list[tuple[float, int, _Delayed]] = []
    sequence = 0
    signal_sequence = 0
    epoch = 0
    connected = False
    last_recv_ms = -math.inf

    def reset_books() -> None:
        for state in markets.values():
            state.reset()

    def token_for(state: _MarketState, direction: int) -> tuple[str, str]:
        return (
            (state.up_token_id, "Up")
            if direction > 0
            else (state.down_token_id, "Down")
        )

    def freeze_decision(
        signal: BreadthSignal,
        direction: int,
        variant: str,
        decision_ms: float,
        parent_signal_id: str | None,
        forced_reason: str | None = None,
    ) -> dict[str, Any]:
        nonlocal signal_sequence
        signal_sequence += 1
        state = markets.get(signal.slot)
        reason = forced_reason
        asset_id = buy_side = None
        decision_vwap = fixed_limit = None
        decision_levels: tuple[tuple[float, float], ...] = ()
        book_recv_ms = None
        if reason is not None:
            pass
        elif state is None:
            reason = "unknown_market"
        elif not connected:
            reason = "disconnect"
        else:
            asset_id, buy_side = token_for(state, direction)
            book = state.books.get(asset_id)
            book_recv_ms = state.book_recv_ms.get(asset_id)
            if (
                book is None
                or book_recv_ms is None
                or not 0 <= decision_ms - book_recv_ms <= cfg.book_fresh_ms
            ):
                reason = "stale_decision_book"
            else:
                reason, decision_vwap, fixed_limit, decision_levels = _decision_price(book, cfg)
        signal_id = (
            f"{variant}:{state.market_id if state else signal.slot}:"
            f"{decision_ms:.6f}:{signal_sequence}"
        )
        return {
            "signal_id": signal_id,
            "parent_signal_id": parent_signal_id,
            "variant": variant,
            "market_id": state.market_id if state else None,
            "slot": signal.slot,
            "direction": direction,
            "buy_asset_id": asset_id,
            "buy_side": buy_side,
            "signal_recv_ms": signal.decision_recv_ms,
            "decision_recv_ms": decision_ms,
            "decision_book_recv_ms": book_recv_ms,
            "decision_vwap": decision_vwap,
            "decision_levels": [
                {"price": price, "shares": shares}
                for price, shares in decision_levels
            ],
            "fixed_limit": fixed_limit,
            "target_shares": cfg.target_shares,
            "contributors": list(signal.contributors),
            "z_scores": dict(signal.z_scores),
            "sent": reason is None,
            "reason": "eligible" if reason is None else reason,
            "epoch": epoch,
        }

    def schedule(decision: Mapping[str, Any]) -> None:
        nonlocal sequence
        for latency in latencies:
            item = _Pending(
                due_ms=float(decision["decision_recv_ms"]) + latency,
                sequence=sequence,
                evaluation_ms=float(latency),
                epoch=int(decision["epoch"]),
                decision=decision,
            )
            heapq.heappush(pending, (item.due_ms, item.sequence, item))
            sequence += 1

    def handle_signal(signal: BreadthSignal, *, ambiguous_clock: bool = False) -> None:
        nonlocal sequence
        forced_reason = "ambiguous_cross_stream_receipt_tie" if ambiguous_clock else None
        base = freeze_decision(
            signal,
            signal.direction,
            BASE_VARIANT,
            signal.decision_recv_ms,
            None,
            forced_reason,
        )
        schedule(base)
        if not include_controls:
            return
        reverse = freeze_decision(
            signal,
            -signal.direction,
            "direction_reversal",
            signal.decision_recv_ms,
            str(base["signal_id"]),
            forced_reason,
        )
        schedule(reverse)
        item = _Delayed(
            due_ms=signal.decision_recv_ms + cfg.time_shift_ms,
            sequence=sequence,
            epoch=epoch,
            signal=signal,
            parent_signal_id=str(base["signal_id"]),
        )
        heapq.heappush(delayed, (item.due_ms, item.sequence, item))
        sequence += 1

    def result(item: _Pending, terminal_reason: str | None = None) -> None:
        decision = item.decision
        state = markets.get(int(decision["slot"]))
        reason = None
        book = None
        book_recv_ms = None
        if not bool(decision["sent"]):
            reason = str(decision["reason"])
        elif terminal_reason is not None:
            reason = terminal_reason
        elif reason is None and (item.epoch != epoch or not connected):
            reason = "disconnect"
        elif reason is None and state is None:
            reason = "unknown_market"
        elif reason is None:
            assert state is not None
            asset_id = str(decision.get("buy_asset_id") or "")
            book = state.books.get(asset_id)
            book_recv_ms = state.book_recv_ms.get(asset_id)
            if (
                book is None
                or book_recv_ms is None
                or not 0 <= item.due_ms - book_recv_ms <= cfg.book_fresh_ms
            ):
                reason = "stale_book"
        filled_shares = 0.0
        fill_vwap = fee = notional = None
        fill_levels: tuple[tuple[float, float], ...] = ()
        if reason is None and book is not None and state is not None:
            fill_reason, shares, fill_vwap, fee, notional, fill_levels = _fill_direct(
                book,
                float(decision["fixed_limit"]),
                cfg.target_shares,
                state.fee_rate,
            )
            if fill_reason == "filled" and shares + _EPS >= cfg.target_shares:
                reason = "filled"
                filled_shares = shares
            elif fill_reason == "partial_fill" and shares > _EPS:
                reason = "partial_fill"
                filled_shares = shares
            else:
                reason = fill_reason
                fill_vwap = fee = notional = None
                fill_levels = ()
        full_fill = reason == "filled"
        executed = filled_shares > _EPS
        winner = outcomes.get(state.market_id) if state is not None else None
        won = (
            str(winner).lower() == str(decision.get("buy_side")).lower()
            if executed and winner is not None
            else None
        )
        total_cost = (
            float(notional) + float(fee)
            if executed and notional is not None and fee is not None
            else None
        )
        all_in_cost = total_cost / filled_shares if total_cost is not None else None
        pnl = (
            (filled_shares if won else 0.0) - total_cost
            if won is not None and total_cost is not None
            else None
        )
        rows.append({
            **{key: value for key, value in decision.items() if key != "epoch"},
            "paper_only": True,
            "ordering_clock": "local_receipt_ms",
            "match_book_clock": "local_receipt_ms",
            "evaluation_ms": item.evaluation_ms,
            "evaluation_recv_ms": item.due_ms,
            "book_recv_ms": book_recv_ms,
            "fee_rate": state.fee_rate if state is not None else None,
            "filled": full_fill,
            "full_fill": full_fill,
            "executed": executed,
            "filled_shares": filled_shares,
            "observed_partial_shares": filled_shares if executed and not full_fill else 0.0,
            "fill_vwap": fill_vwap if executed else None,
            "fill_levels": [
                {"price": price, "shares": shares}
                for price, shares in fill_levels
            ],
            "fee": fee if executed else None,
            "notional": notional if executed else None,
            "total_cost": total_cost,
            "all_in_cost": all_in_cost,
            "winner": winner,
            "won": won,
            "pnl_per_share": pnl / filled_shares if pnl is not None else None,
            "pnl": pnl,
            "day": datetime.fromtimestamp(
                float(decision["decision_recv_ms"]) / 1_000.0,
                timezone.utc,
            ).date().isoformat(),
            "reason": reason,
        })
        counters[f"reason_{reason}"] += 1
        counters["fills"] += int(full_fill)
        counters["partial_fills"] += int(executed and not full_fill)

    def decide_shift(item: _Delayed, *, ambiguous_clock: bool = False) -> None:
        forced_reason = "ambiguous_cross_stream_receipt_tie" if ambiguous_clock else None
        decision = freeze_decision(
            item.signal,
            item.signal.direction,
            "time_shift",
            item.due_ms,
            item.parent_signal_id,
            forced_reason,
        )
        decision["scheduled_decision_recv_ms"] = item.due_ms
        if item.epoch != epoch:
            decision["sent"] = False
            decision["reason"] = "disconnect"
        schedule(decision)

    def drain(
        cutoff_ms: float,
        *,
        inclusive: bool,
        ambiguous_clob_ms: float | None = None,
    ) -> None:
        while pending or delayed:
            pending_due = pending[0][0] if pending else math.inf
            delayed_due = delayed[0][0] if delayed else math.inf
            due_ms = min(pending_due, delayed_due)
            due = due_ms <= cutoff_ms + _EPS if inclusive else due_ms < cutoff_ms - _EPS
            if not due:
                return
            if delayed_due <= pending_due:
                delayed_item = heapq.heappop(delayed)[2]
                decide_shift(
                    delayed_item,
                    ambiguous_clock=(
                        ambiguous_clob_ms is not None
                        and abs(delayed_item.due_ms - ambiguous_clob_ms) <= _EPS
                    ),
                )
            else:
                pending_item = heapq.heappop(pending)[2]
                terminal_reason = (
                    "ambiguous_cross_stream_receipt_tie"
                    if ambiguous_clob_ms is not None
                    and abs(pending_item.due_ms - ambiguous_clob_ms) <= _EPS
                    else None
                )
                result(pending_item, terminal_reason)

    def apply_clob(event: Mapping[str, Any]) -> None:
        nonlocal connected, epoch
        kind = str(event["kind"])
        recv_ms = float(event["recv_ms"])
        counters[kind] += 1
        if kind == "clob_connection":
            epoch = int(event["connection_epoch"])
            connected = True
            reset_books()
            return
        if kind == "clob_error":
            connected = False
            reset_books()
            return
        if not connected or int(event["connection_epoch"]) != epoch:
            counters["ignored_disconnected_clob"] += 1
            return
        asset_id = str(event.get("asset_id") or "")
        state = tokens.get(asset_id)
        if state is None or str(event.get("market_id")) != state.market_id:
            counters["unknown_or_mismatched_token"] += 1
            return
        book = state.books.setdefault(asset_id, TokenBook())
        source_ms = float(event["source_ts_ms"])
        applied = False
        if kind == "clob_snapshot" and source_ms >= book.source_ms:
            book.replace(event["bids"], event["asks"], source_ms)
            applied = True
        elif kind == "clob_price_change":
            applied = book.change(
                str(event["side"]),
                float(event["price"]),
                float(event["size"]),
                source_ms,
                event.get("best_bid"),
                event.get("best_ask"),
            )
        if applied:
            if not book.bids or not book.asks or max(book.bids) >= min(book.asks):
                book.ready = False
            state.book_recv_ms[asset_id] = recv_ms
        else:
            counters["clob_update_not_applied"] += 1

    clob_timeline = (
        (float(event["recv_ms"]), 0, int(event["seq"]), event)
        for event in clob_events
    )
    signal_timeline = (
        (signal.decision_recv_ms, 1, index, signal)
        for index, signal in enumerate(signals)
    )
    timeline = heapq.merge(clob_timeline, signal_timeline, key=lambda item: item[:3])
    for recv_ms, grouped in groupby(timeline, key=lambda item: item[0]):
        if recv_ms < last_recv_ms:
            raise ValueError("execution timeline regressed")
        drain(recv_ms, inclusive=False)
        batch = list(grouped)
        has_clob = any(kind == 0 for _clock, kind, _order, _payload in batch)
        for _clock, kind, _order, payload in batch:
            if kind == 0:
                if not isinstance(payload, Mapping):
                    raise TypeError("CLOB timeline payload is not a mapping")
                apply_clob(payload)
        for _clock, kind, _order, payload in batch:
            if kind == 1:
                if not isinstance(payload, BreadthSignal):
                    raise TypeError("signal timeline payload has the wrong type")
                handle_signal(payload, ambiguous_clock=has_clob)
        drain(
            recv_ms,
            inclusive=True,
            ambiguous_clob_ms=recv_ms if has_clob else None,
        )
        last_recv_ms = recv_ms

    drain(last_recv_ms, inclusive=True)
    while delayed:
        delayed_item = heapq.heappop(delayed)[2]
        decision = freeze_decision(
            delayed_item.signal,
            delayed_item.signal.direction,
            "time_shift",
            delayed_item.due_ms,
            delayed_item.parent_signal_id,
        )
        decision["sent"] = False
        decision["reason"] = "censored_before_shifted_decision"
        schedule(decision)
    while pending:
        pending_item = heapq.heappop(pending)[2]
        terminal_reason = (
            "disconnect"
            if not connected or pending_item.epoch != epoch
            else "censored_recording_end"
        )
        result(pending_item, terminal_reason)
    rows.sort(key=lambda row: (
        float(row["decision_recv_ms"]),
        str(row["variant"]),
        float(row["evaluation_ms"]),
    ))
    return rows, dict(counters)


def summarize(
    rows: Sequence[Mapping[str, Any]],
    counters: Mapping[str, int],
    config: BreadthConfig | None = None,
) -> dict[str, Any]:
    cfg = config or BreadthConfig()
    variants: dict[str, Any] = {}
    for variant in (BASE_VARIANT, *CONTROL_VARIANTS):
        latencies: dict[str, Any] = {}
        for latency in cfg.evaluation_ms:
            sample = [
                row for row in rows
                if row["variant"] == variant and float(row["evaluation_ms"]) == latency
            ]
            fills = [row for row in sample if row.get("filled")]
            executions = [row for row in sample if row.get("executed")]
            scored = [row for row in executions if row.get("pnl") is not None]
            shares = sum(float(row["filled_shares"]) for row in scored)
            pnl = sum(float(row["pnl"]) for row in scored)
            raw_p = _exact_pvalue(list(scored)) if shares and pnl > 0 else 1.0
            latencies[str(int(latency))] = {
                "signals": len(sample),
                "sent": sum(bool(row.get("sent")) for row in sample),
                "fills": len(fills),
                "partial_fills": sum(
                    bool(row.get("executed")) and not bool(row.get("full_fill"))
                    for row in sample
                ),
                "executions": len(executions),
                "resolved_fills": len(scored),
                "fill_rate": len(fills) / len(sample) if sample else 0.0,
                "execution_rate": len(executions) / len(sample) if sample else 0.0,
                "filled_shares": shares,
                "wins": sum(bool(row.get("won")) for row in scored),
                "losses": sum(row.get("won") is False for row in scored),
                "net_ev_per_share": pnl / shares if shares else None,
                "pnl": pnl,
                "raw_exact_p": raw_p,
                "corrected_exact_p": min(1.0, raw_p * cfg.family_tests),
                "reasons": dict(Counter(str(row.get("reason")) for row in sample)),
            }
        variants[variant] = {"latencies": latencies}
    return {"counters": dict(counters), "variants": variants}


def replay_archive(
    archive_dir: str | Path,
    trade_paths: Mapping[str, str | Path],
    *,
    rows_out: str | Path | None = None,
    config: BreadthConfig | None = None,
) -> dict[str, Any]:
    cfg = config or BreadthConfig()
    root = Path(archive_dir)
    standard = root / "strict" if (root / "strict" / "manifest.json").exists() else root
    validation = archive.validate_standard_artifact(standard)
    mappings = list(archive.iter_market_mappings(standard / "market_registry.csv.gz"))
    outcomes = {
        str(row["market_id"]): str(row["winner"])
        for row in archive.iter_outcomes(standard / "market_outcomes.csv.gz")
    }
    detection_counters: Counter[str] = Counter()
    price_audits: dict[str, dict[str, Any]] = {}
    signals = detect_signals(
        iter_trade_files(trade_paths, price_audits), cfg, detection_counters,
    )
    source_bundle_proven = _source_bundle_proven(price_audits, validation["manifest"])
    clob_events = archive.iter_normalized_events(
        standard / "clob_events.jsonl.gz", family="clob",
    )
    rows, counters = replay_execution(
        clob_events,
        signals,
        mappings,
        outcomes,
        config=cfg,
    )
    if rows_out is not None:
        destination = Path(rows_out)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("w", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
    result = summarize(rows, counters, cfg)
    result["records"] = rows
    result["signals"] = [asdict(signal) for signal in signals]
    result["detection_counters"] = dict(detection_counters)
    result["dataset"] = {
        "paper_only": True,
        "sample_scope": "development_forward_artifact",
        "archive": root.name,
        "manifest": validation["manifest"],
        "mapped_markets": len(mappings),
        "outcomes": len(outcomes),
        "trade_assets": list(ASSETS),
        "source_lifecycle_proven": source_bundle_proven,
        "source_price_audits": price_audits,
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", required=True)
    for asset in ASSETS:
        parser.add_argument(
            f"--{asset}-prices",
            f"--{asset}-trades",
            dest=f"{asset}_prices",
            required=True,
        )
    parser.add_argument("--rows-out")
    args = parser.parse_args()
    paths = {
        asset: getattr(args, f"{asset}_prices")
        for asset in ASSETS
    }
    print(json.dumps(
        replay_archive(args.archive, paths, rows_out=args.rows_out),
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ))


if __name__ == "__main__":
    main()
