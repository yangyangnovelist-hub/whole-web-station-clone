"""Receipt-causal Chainlink settlement proxy and direct-L2 execution replay.

The module deliberately separates three clocks:

* source payload time selects the 61 members of each settlement window;
* local receipt time limits what the predictor may know at a decision;
* Polymarket local receipt time selects the executable book after 300/500 ms.

Calibration is completed before the holdout boundary.  The holdout path never
refits venue offsets, timing offsets, or the residual-error bound.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import heapq
import json
import math
from array import array
from bisect import bisect_right
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence

import h_replay
import h_replay_archive
import h_replay_run


SPOT_SOURCES = ("coinbase", "kraken", "bitstamp", "bn_spot", "okx", "bybit_spot")
DERIBIT_SOURCE = "deribit_index"
PRICE_FRAME_MARKERS = {
    "bn_spot": ("btcusdt@bookTicker", "btcusdt@depth"),
    "coinbase": ('"ticker"',),
    "kraken": ('"book"',),
    "bitstamp": ("order_book_btcusd",),
    "okx": ("bbo-tbt",),
    "bybit_spot": ("orderbook.1.BTCUSDT",),
    "deribit": ("deribit_price_index.btc_usd",),
}
BASE_VARIANT = "proxy_spot6_deribit"
BASELINE_VARIANT = "delayed_chainlink"
TIMING_VARIANT = "proxy_execution_250ms_delayed"
MIN_STOPPING_FILLS = 1_000
MIN_STOPPING_DAYS = 7
FAMILY_TESTS = 100
FREEZE_PATH = Path(__file__).with_name("forward") / "settlement-proxy-freeze.json"
CALIBRATION_PIN = Path(__file__).with_name("forward") / "settlement-proxy-calibration.json"
VERDICT_PIN = Path(__file__).with_name("forward") / "settlement-proxy-verdict.json"
# Updated only before the untouched holdout starts.  load_freeze() rejects any
# later edit to the preregistered protocol instead of silently accepting a new
# fingerprint.
EXPECTED_FREEZE_SHA256 = "2d064f057ae5f169dfab66e355baa2789797e7c75ae552fdce290c81e14400ee"


@dataclass(frozen=True)
class PricePoint:
    source: str
    receive_ms: float
    value: float


@dataclass(frozen=True)
class ChainlinkPoint:
    payload_ms: float
    push_ms: float
    receive_ms: float
    value: float


@dataclass
class FrameState:
    kraken_bids: dict[float, float] = field(default_factory=dict)
    kraken_asks: dict[float, float] = field(default_factory=dict)
    kraken_ready: bool = False
    bybit_bid: float = math.nan
    bybit_ask: float = math.nan


@dataclass(frozen=True)
class ProxyConfig:
    decision_lead_ms: float = 1_000.0
    timing_placebo_delay_ms: float = 250.0
    max_quote_age_ms: float = 2_000.0
    lag_grid_ms: tuple[float, ...] = tuple(float(value) for value in range(-2_500, 1_001, 100))
    calibration_fit_fraction: float = 0.60
    minimum_spot_sources: int = 4
    min_fit_points: int = 300
    min_validation_windows: int = 100
    residual_quantile: float = 0.99
    residual_safety_multiplier: float = 1.25
    downsample_ms: float = 100.0


@dataclass(frozen=True)
class ProxyCalibration:
    calibration_end_ms: float
    fit_end_ms: float
    spot_query_offset_ms: float
    deribit_query_offset_ms: float
    venue_log_offsets: dict[str, float]
    deribit_log_offset: float
    residual_bound_bp: float
    residual_quantile_bp: float
    validation_windows: int
    spot_validation_mae_bp: float
    deribit_validation_mae_bp: float
    combined_validation_mae_bp: float
    ablation_residual_bounds_bp: dict[str, float] = field(default_factory=dict)
    ablation_validation_windows: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ProxyWindowEstimate:
    combined: float
    spot: Optional[float]
    deribit: Optional[float]
    latest_query_ms: float


@dataclass(frozen=True)
class ProxySignal:
    variant: str
    market_id: str
    slot: int
    decision_ms: float
    direction: str
    opening: float
    projected_close: float
    spot_close: Optional[float]
    deribit_close: Optional[float]
    margin: float
    bound: float
    actual_winner: Optional[str]


class StepSeries:
    """Compact last-value series indexed solely by local receipt time."""

    def __init__(self, receives: Sequence[float], values: Sequence[float]) -> None:
        if len(receives) != len(values):
            raise ValueError("receive/value length mismatch")
        if any(receives[index] > receives[index + 1] for index in range(len(receives) - 1)):
            raise ValueError("receipt clock moved backwards")
        self.receives = array("d", receives)
        self.values = array("d", values)

    @classmethod
    def from_points(cls, points: Iterable[PricePoint]) -> "StepSeries":
        ordered = sorted(points, key=lambda point: point.receive_ms)
        return cls([point.receive_ms for point in ordered], [point.value for point in ordered])

    def at(self, cutoff_ms: float, *, max_age_ms: float = math.inf) -> Optional[float]:
        index = bisect_right(self.receives, cutoff_ms) - 1
        if index < 0 or cutoff_ms - self.receives[index] > max_age_ms + 1e-9:
            return None
        value = self.values[index]
        return value if math.isfinite(value) and value > 0 else None

    def __len__(self) -> int:
        return len(self.receives)


def _midpoint(bid: Any, ask: Any) -> Optional[float]:
    try:
        bid_value, ask_value = float(bid), float(ask)
    except (TypeError, ValueError):
        return None
    if not (0 < bid_value <= ask_value and math.isfinite(bid_value) and math.isfinite(ask_value)):
        return None
    return (bid_value + ask_value) / 2.0


def _book_midpoint(bids: Any, asks: Any) -> Optional[float]:
    try:
        return _midpoint(bids[0][0], asks[0][0])
    except (IndexError, KeyError, TypeError):
        return None


def parse_raw_frame(
    source: str,
    receive_ms: float,
    text: str,
    state: FrameState,
) -> tuple[list[PricePoint], list[ChainlinkPoint]]:
    """Parse one recorder frame; irrelevant channels yield empty lists."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return [], []
    value: Optional[float] = None
    normalized_source = source
    chainlink: list[ChainlinkPoint] = []
    try:
        if source == "bn_spot":
            stream, data = str(payload.get("stream", "")), payload.get("data") or {}
            if stream == "btcusdt@bookTicker":
                value = _midpoint(data.get("b"), data.get("a"))
            elif stream.startswith("btcusdt@depth"):
                value = _book_midpoint(data.get("bids"), data.get("asks"))
        elif source == "coinbase":
            if payload.get("type") == "ticker" and payload.get("product_id") == "BTC-USD":
                value = _midpoint(payload.get("best_bid"), payload.get("best_ask"))
        elif source == "kraken" and payload.get("channel") == "book":
            for book in payload.get("data") or []:
                if payload.get("type") == "snapshot":
                    state.kraken_bids.clear()
                    state.kraken_asks.clear()
                    state.kraken_ready = True
                for level in book.get("bids") or []:
                    price, size = float(level["price"]), float(level["qty"])
                    if size > 0:
                        state.kraken_bids[price] = size
                    else:
                        state.kraken_bids.pop(price, None)
                for level in book.get("asks") or []:
                    price, size = float(level["price"]), float(level["qty"])
                    if size > 0:
                        state.kraken_asks[price] = size
                    else:
                        state.kraken_asks.pop(price, None)
            if len(state.kraken_bids) > 10:
                keep = set(sorted(state.kraken_bids)[-10:])
                state.kraken_bids = {price: size for price, size in state.kraken_bids.items()
                                     if price in keep}
            if len(state.kraken_asks) > 10:
                keep = set(sorted(state.kraken_asks)[:10])
                state.kraken_asks = {price: size for price, size in state.kraken_asks.items()
                                     if price in keep}
            if state.kraken_ready and state.kraken_bids and state.kraken_asks:
                value = _midpoint(max(state.kraken_bids), min(state.kraken_asks))
        elif source == "bitstamp":
            if payload.get("channel") == "order_book_btcusd" and payload.get("event") == "data":
                data = payload.get("data") or {}
                value = _book_midpoint(data.get("bids"), data.get("asks"))
        elif source == "okx":
            argument = payload.get("arg") or {}
            if argument.get("channel") == "bbo-tbt" and argument.get("instId") == "BTC-USDT":
                data = (payload.get("data") or [None])[0] or {}
                value = _book_midpoint(data.get("bids"), data.get("asks"))
        elif source == "bybit_spot" and payload.get("topic") == "orderbook.1.BTCUSDT":
            data = payload.get("data") or {}
            if data.get("b"):
                state.bybit_bid = float(data["b"][0][0])
            if data.get("a"):
                state.bybit_ask = float(data["a"][0][0])
            value = _midpoint(state.bybit_bid, state.bybit_ask)
        elif source == "deribit":
            params = payload.get("params") or {}
            if params.get("channel") == "deribit_price_index.btc_usd":
                value = float((params.get("data") or {})["price"])
                normalized_source = DERIBIT_SOURCE
        elif source == "rtds_cl" and payload.get("topic") == "crypto_prices_chainlink":
            data = payload.get("payload") or {}
            if str(data.get("symbol", "")).lower() == "btc/usd":
                chainlink.append(ChainlinkPoint(
                    float(data["timestamp"]), float(payload["timestamp"]), receive_ms, float(data["value"]),
                ))
    except (IndexError, KeyError, TypeError, ValueError):
        return [], []
    points = []
    if value is not None and math.isfinite(value) and value > 0:
        points.append(PricePoint(normalized_source, receive_ms, value))
    return points, chainlink


def settlement_payload_times(boundary_ms: float) -> tuple[float, ...]:
    """The exact 61 Chainlink seconds [boundary-62s, boundary-2s]."""
    return tuple(boundary_ms + offset * 1_000.0 for offset in range(-62, -1))


def _quantile(values: Sequence[float], probability: float) -> float:
    if not values:
        return math.nan
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _normalised_chainlink(points: Iterable[ChainlinkPoint]) -> list[ChainlinkPoint]:
    by_payload: dict[float, ChainlinkPoint] = {}
    for point in points:
        previous = by_payload.get(point.payload_ms)
        if previous is None or point.receive_ms < previous.receive_ms:
            by_payload[point.payload_ms] = point
    return sorted(by_payload.values(), key=lambda point: point.payload_ms)


def _fit_group(
    series: Mapping[str, StepSeries],
    chainlink: Sequence[ChainlinkPoint],
    sources: Sequence[str],
    config: ProxyConfig,
    *,
    latest_receive_ms: float = math.inf,
) -> tuple[float, dict[str, float], float]:
    best: Optional[tuple[float, float, dict[str, float], int]] = None
    for query_offset in config.lag_grid_ms:
        offsets: dict[str, float] = {}
        for source in sources:
            residuals = []
            source_series = series[source]
            for point in chainlink:
                query_ms = point.payload_ms + query_offset
                if query_ms >= latest_receive_ms:
                    continue
                value = source_series.at(
                    query_ms, max_age_ms=config.max_quote_age_ms,
                )
                if value is not None:
                    residuals.append(math.log(value) - math.log(point.value))
            if len(residuals) < config.min_fit_points:
                break
            offsets[source] = median(residuals)
        if len(offsets) != len(sources):
            continue
        errors = []
        for point in chainlink:
            adjusted = []
            query_ms = point.payload_ms + query_offset
            if query_ms >= latest_receive_ms:
                continue
            for source in sources:
                value = series[source].at(
                    query_ms, max_age_ms=config.max_quote_age_ms,
                )
                if value is None:
                    break
                adjusted.append(math.log(value) - offsets[source])
            if len(adjusted) == len(sources):
                errors.append(abs((median(adjusted) - math.log(point.value)) * 10_000.0))
        if len(errors) < config.min_fit_points:
            continue
        score = median(errors)
        candidate = (score, abs(query_offset), offsets, len(errors))
        if best is None or candidate[:2] < best[:2]:
            best = candidate
            best_query_offset = query_offset
    if best is None:
        raise ValueError(f"insufficient calibration observations for {','.join(sources)}")
    return best_query_offset, best[2], best[0]


def _component_value(
    payload_ms: float,
    decision_ms: float,
    series: Mapping[str, StepSeries],
    sources: Sequence[str],
    query_offset_ms: float,
    offsets: Mapping[str, float],
    config: ProxyConfig,
) -> Optional[float]:
    query_ms = payload_ms + query_offset_ms
    # Every decision/cutoff is exclusive: a quote received at the exact
    # decision instant is not information available before that instant.
    if query_ms >= decision_ms:
        return None
    adjusted = []
    for source in sources:
        value = series[source].at(query_ms, max_age_ms=config.max_quote_age_ms)
        if value is None:
            return None
        adjusted.append(math.log(value) - float(offsets[source]))
    return math.exp(median(adjusted))


def estimate_proxy_window(
    boundary_ms: float,
    decision_ms: float,
    series: Mapping[str, StepSeries],
    calibration: ProxyCalibration,
    config: ProxyConfig,
    *,
    omit_source: Optional[str] = None,
) -> Optional[ProxyWindowEstimate]:
    spot_sources = tuple(source for source in SPOT_SOURCES if source != omit_source)
    use_deribit = omit_source != DERIBIT_SOURCE
    if len(spot_sources) < config.minimum_spot_sources:
        return None
    spot_values: list[float] = []
    deribit_values: list[float] = []
    latest_query = -math.inf
    for payload_ms in settlement_payload_times(boundary_ms):
        adjusted_spot = []
        query_ms = payload_ms + calibration.spot_query_offset_ms
        if query_ms >= decision_ms:
            return None
        for source in spot_sources:
            value = series[source].at(query_ms, max_age_ms=config.max_quote_age_ms)
            if value is not None:
                adjusted_spot.append(math.log(value) - calibration.venue_log_offsets[source])
        if len(adjusted_spot) < config.minimum_spot_sources:
            return None
        spot_values.append(math.exp(median(adjusted_spot)))
        latest_query = max(latest_query, query_ms)
        if use_deribit:
            deribit = _component_value(
                payload_ms, decision_ms, series, (DERIBIT_SOURCE,),
                calibration.deribit_query_offset_ms,
                {DERIBIT_SOURCE: calibration.deribit_log_offset}, config,
            )
            if deribit is not None:
                deribit_values.append(deribit)
            latest_query = max(latest_query, payload_ms + calibration.deribit_query_offset_ms)
    spot_close = sum(spot_values) / len(spot_values) if spot_values else None
    deribit_close = (
        sum(deribit_values) / len(deribit_values)
        if use_deribit and len(deribit_values) == len(settlement_payload_times(boundary_ms))
        else None
    )
    if spot_close is not None and deribit_close is not None:
        combined = math.sqrt(spot_close * deribit_close)
    else:
        combined = spot_close if spot_close is not None else deribit_close
    assert combined is not None
    return ProxyWindowEstimate(combined, spot_close, deribit_close, latest_query)


def _window_average(exact: Mapping[float, ChainlinkPoint], boundary_ms: float) -> Optional[float]:
    points = [exact.get(timestamp) for timestamp in settlement_payload_times(boundary_ms)]
    if any(point is None for point in points):
        return None
    return sum(point.value for point in points if point is not None) / 61.0


def _validation_mae(
    records: Sequence[ChainlinkPoint],
    series: Mapping[str, StepSeries],
    query_offset_ms: float,
    sources: Sequence[str],
    offsets: Mapping[str, float],
    config: ProxyConfig,
    *,
    latest_receive_ms: float,
) -> float:
    errors = []
    for point in records:
        estimate = _component_value(
            point.payload_ms, latest_receive_ms, series, sources, query_offset_ms, offsets, config,
        )
        if estimate is not None:
            errors.append(abs(math.log(estimate / point.value)) * 10_000.0)
    return sum(errors) / len(errors) if errors else math.nan


def fit_calibration(
    series: Mapping[str, StepSeries],
    chainlink_points: Iterable[ChainlinkPoint],
    calibration_end_ms: float,
    config: ProxyConfig | None = None,
) -> ProxyCalibration:
    config = config or ProxyConfig()
    missing = set((*SPOT_SOURCES, DERIBIT_SOURCE)) - set(series)
    if missing:
        raise ValueError(f"missing proxy sources: {sorted(missing)}")
    eligible = [point for point in _normalised_chainlink(chainlink_points)
                if point.payload_ms < calibration_end_ms and point.receive_ms < calibration_end_ms]
    if len(eligible) < config.min_fit_points * 2:
        raise ValueError("insufficient pre-holdout Chainlink observations")
    split = max(config.min_fit_points, int(len(eligible) * config.calibration_fit_fraction))
    split = min(split, len(eligible) - config.min_fit_points)
    fit, validation = eligible[:split], eligible[split:]
    fit_end_ms = validation[0].payload_ms
    spot_query, spot_offsets, _ = _fit_group(
        series, fit, SPOT_SOURCES, config, latest_receive_ms=fit_end_ms,
    )
    deribit_query, deribit_offsets, _ = _fit_group(
        series, fit, (DERIBIT_SOURCE,), config, latest_receive_ms=fit_end_ms,
    )
    provisional = ProxyCalibration(
        calibration_end_ms=calibration_end_ms,
        fit_end_ms=fit_end_ms,
        spot_query_offset_ms=spot_query,
        deribit_query_offset_ms=deribit_query,
        venue_log_offsets=spot_offsets,
        deribit_log_offset=deribit_offsets[DERIBIT_SOURCE],
        residual_bound_bp=0.0,
        residual_quantile_bp=0.0,
        validation_windows=0,
        spot_validation_mae_bp=math.nan,
        deribit_validation_mae_bp=math.nan,
        combined_validation_mae_bp=math.nan,
    )
    exact = {point.payload_ms: point for point in eligible}
    first_boundary = int(math.ceil((fit_end_ms + 62_000.0) / 300_000.0)) * 300_000
    last_boundary = int(calibration_end_ms // 300_000.0) * 300_000
    combined_point_errors = []
    for point in validation:
        diagnostic_decision_ms = min(
            point.payload_ms + 1_000.0,
            math.nextafter(calibration_end_ms, -math.inf),
        )
        estimate = estimate_proxy_window(
            point.payload_ms + 2_000.0,
            diagnostic_decision_ms,
            series,
            provisional,
            config,
        )
        # This diagnostic uses only windows when the supplied point is the final member.
        if estimate is not None:
            exact_window = _window_average(exact, point.payload_ms + 2_000.0)
            if exact_window is not None:
                combined_point_errors.append(abs(math.log(estimate.combined / exact_window)) * 10_000.0)
    def window_residuals(omit_source: Optional[str] = None) -> list[float]:
        values = []
        for boundary in range(first_boundary, last_boundary + 1, 300_000):
            exact_window = _window_average(exact, float(boundary))
            estimate = estimate_proxy_window(
                float(boundary), float(boundary) - config.decision_lead_ms,
                series, provisional, config, omit_source=omit_source,
            )
            if exact_window is not None and estimate is not None:
                values.append(abs(math.log(estimate.combined / exact_window)) * 10_000.0)
        return values

    residuals = window_residuals()
    if len(residuals) < config.min_validation_windows:
        raise ValueError(
            f"only {len(residuals)} complete pre-holdout settlement windows; "
            f"need {config.min_validation_windows}"
        )
    residual_quantile = _quantile(residuals, config.residual_quantile)
    residual_bound = residual_quantile * config.residual_safety_multiplier
    ablation_bounds = {}
    ablation_windows = {}
    for source in (*SPOT_SOURCES, DERIBIT_SOURCE):
        ablation_residuals = window_residuals(source)
        if len(ablation_residuals) < config.min_validation_windows:
            raise ValueError(f"insufficient validation windows for omit_{source}")
        ablation_windows[source] = len(ablation_residuals)
        ablation_bounds[source] = (
            _quantile(ablation_residuals, config.residual_quantile)
            * config.residual_safety_multiplier
        )
    return ProxyCalibration(
        calibration_end_ms=calibration_end_ms,
        fit_end_ms=fit_end_ms,
        spot_query_offset_ms=spot_query,
        deribit_query_offset_ms=deribit_query,
        venue_log_offsets=spot_offsets,
        deribit_log_offset=deribit_offsets[DERIBIT_SOURCE],
        residual_bound_bp=residual_bound,
        residual_quantile_bp=residual_quantile,
        validation_windows=len(residuals),
        spot_validation_mae_bp=_validation_mae(
            validation, series, spot_query, SPOT_SOURCES, spot_offsets, config,
            latest_receive_ms=calibration_end_ms,
        ),
        deribit_validation_mae_bp=_validation_mae(
            validation, series, deribit_query, (DERIBIT_SOURCE,), deribit_offsets, config,
            latest_receive_ms=calibration_end_ms,
        ),
        combined_validation_mae_bp=(sum(combined_point_errors) / len(combined_point_errors)
                                    if combined_point_errors else math.nan),
        ablation_residual_bounds_bp=ablation_bounds,
        ablation_validation_windows=ablation_windows,
    )


def _atomic_json(path: str | Path, payload: Mapping[str, Any]) -> None:
    """Create one immutable JSON pin; concurrent/repeated writers fail closed."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    try:
        with destination.open("x", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
    except FileExistsError:
        existing = json.loads(destination.read_text(encoding="utf-8"))
        if existing != payload:
            raise ValueError(f"immutable pin already exists with different contents: {destination}")


def load_freeze(path: str | Path = FREEZE_PATH) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("strategy_id") != "settlement-proxy-v3":
        raise ValueError("unexpected settlement-proxy strategy id")
    fingerprint = strategy_fingerprint(payload)
    if fingerprint != EXPECTED_FREEZE_SHA256:
        raise ValueError(
            f"settlement-proxy freeze fingerprint mismatch: {fingerprint}"
        )
    return payload


def strategy_fingerprint(freeze: Mapping[str, Any]) -> str:
    canonical = json.dumps(freeze, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def load_or_pin_calibration(
    series: Mapping[str, StepSeries],
    chainlink_points: Sequence[ChainlinkPoint],
    calibration_end_ms: float,
    config: ProxyConfig,
    path: str | Path,
    fingerprint: str,
) -> ProxyCalibration:
    destination = Path(path)
    if destination.exists():
        payload = json.loads(destination.read_text(encoding="utf-8"))
        if payload.get("strategy_fingerprint") != fingerprint:
            raise ValueError("pinned calibration belongs to another protocol")
        calibration = ProxyCalibration(**payload["calibration"])
        if not math.isclose(calibration.calibration_end_ms, calibration_end_ms):
            raise ValueError("pinned calibration uses another cutoff")
        return calibration
    if not chainlink_points or max(point.receive_ms for point in chainlink_points) < calibration_end_ms:
        raise ValueError("cannot pin calibration before the frozen cutoff is fully observed")
    if any(not values.receives or values.receives[-1] < calibration_end_ms
           for values in series.values()):
        raise ValueError("one or more proxy sources do not cover the frozen cutoff")
    calibration = fit_calibration(series, chainlink_points, calibration_end_ms, config)
    _atomic_json(destination, {
        "strategy_fingerprint": fingerprint,
        "calibration": asdict(calibration),
    })
    return calibration


def _exact_open(
    exact: Mapping[float, ChainlinkPoint], boundary_ms: float, decision_ms: float,
) -> Optional[float]:
    rows = [exact.get(timestamp) for timestamp in settlement_payload_times(boundary_ms)]
    if any(row is None or row.receive_ms > decision_ms for row in rows):
        return None
    return sum(row.value for row in rows if row is not None) / 61.0


def _delayed_chainlink_close(
    exact: Mapping[float, ChainlinkPoint],
    received: StepSeries,
    boundary_ms: float,
    decision_ms: float,
) -> Optional[float]:
    latest = received.at(decision_ms, max_age_ms=5_000.0)
    if latest is None:
        return None
    values = []
    for timestamp in settlement_payload_times(boundary_ms):
        point = exact.get(timestamp)
        values.append(point.value if point is not None and point.receive_ms <= decision_ms else latest)
    return sum(values) / len(values)


def _signal_for_estimate(
    variant: str,
    mapping: Mapping[str, Any],
    decision_ms: float,
    opening: float,
    estimate: ProxyWindowEstimate,
    actual_winner: Optional[str],
    bound: float,
    *,
    require_agreement: bool,
) -> Optional[ProxySignal]:
    margin = estimate.combined - opening
    if abs(margin) <= bound:
        return None
    direction = "Up" if margin >= 0 else "Down"
    if require_agreement and estimate.spot is not None and estimate.deribit is not None:
        spot_direction = estimate.spot >= opening
        deribit_direction = estimate.deribit >= opening
        if spot_direction != deribit_direction or spot_direction != (direction == "Up"):
            return None
    return ProxySignal(
        variant=variant,
        market_id=str(mapping["market_id"]),
        slot=int(mapping["slot"]),
        decision_ms=decision_ms,
        direction=direction,
        opening=opening,
        projected_close=estimate.combined,
        spot_close=estimate.spot,
        deribit_close=estimate.deribit,
        margin=margin,
        bound=bound,
        actual_winner=actual_winner,
    )


def generate_signals(
    mappings: Iterable[Mapping[str, Any]],
    series: Mapping[str, StepSeries],
    chainlink_points: Iterable[ChainlinkPoint],
    calibration: ProxyCalibration,
    holdout_start_ms: float,
    config: ProxyConfig | None = None,
    official_outcomes: Optional[Mapping[str, str]] = None,
) -> tuple[dict[str, list[ProxySignal]], dict[str, dict[str, Any]]]:
    config = config or ProxyConfig()
    if holdout_start_ms < calibration.calibration_end_ms:
        raise ValueError("holdout start cannot precede calibration end")
    if config.timing_placebo_delay_ms + 500.0 >= config.decision_lead_ms:
        raise ValueError("timing placebo must remain executable before market close")
    official_outcomes = official_outcomes or {}
    points = _normalised_chainlink(chainlink_points)
    exact = {point.payload_ms: point for point in points}
    received = StepSeries.from_points(
        PricePoint("chainlink", point.receive_ms, point.value) for point in points
    )
    variants = [BASE_VARIANT, BASELINE_VARIANT, TIMING_VARIANT]
    variants += [f"omit_{source}" for source in (*SPOT_SOURCES, DERIBIT_SOURCE)]
    signals: dict[str, list[ProxySignal]] = {variant: [] for variant in variants}
    audit: dict[str, Counter[str]] = {variant: Counter() for variant in variants}
    for mapping in sorted(mappings, key=lambda row: int(row["slot"])):
        slot = int(mapping["slot"])
        if slot * 1_000.0 < holdout_start_ms:
            continue
        close_boundary = (slot + 300) * 1_000.0
        decision_ms = close_boundary - config.decision_lead_ms
        opening = _exact_open(exact, slot * 1_000.0, decision_ms)
        if opening is None:
            for counter in audit.values():
                counter["missing_exact_opening"] += 1
            continue
        actual_winner = official_outcomes.get(str(mapping["market_id"]))
        if actual_winner is None:
            for counter in audit.values():
                counter["missing_official_outcome"] += 1
        bound = opening * calibration.residual_bound_bp / 10_000.0

        def make_proxy(
            variant: str,
            decision: float,
            omit: Optional[str] = None,
            error_bound: float = bound,
        ) -> Optional[ProxySignal]:
            estimate = estimate_proxy_window(
                close_boundary, decision, series, calibration, config, omit_source=omit,
            )
            if estimate is None:
                audit[variant]["proxy_unavailable"] += 1
                return None
            signal = _signal_for_estimate(
                variant, mapping, decision, opening, estimate, actual_winner, error_bound,
                require_agreement=omit != DERIBIT_SOURCE,
            )
            if signal is None:
                audit[variant]["margin_or_agreement_gate"] += 1
            else:
                signals[variant].append(signal)
                audit[variant]["signals"] += 1
                if signal.actual_winner is not None:
                    audit[variant]["direction_correct"] += signal.direction == signal.actual_winner
            return signal

        base_signal = make_proxy(BASE_VARIANT, decision_ms)
        if base_signal is not None:
            # Delay an already-known decision. Recomputing at an earlier clock would
            # backdate information and create lookahead.
            timing_signal = replace(
                base_signal,
                variant=TIMING_VARIANT,
                decision_ms=base_signal.decision_ms + config.timing_placebo_delay_ms,
            )
            signals[TIMING_VARIANT].append(timing_signal)
            audit[TIMING_VARIANT]["signals"] += 1
            if timing_signal.actual_winner is not None:
                audit[TIMING_VARIANT]["direction_correct"] += (
                    timing_signal.direction == timing_signal.actual_winner
                )
            audit[TIMING_VARIANT]["paired_base_signals"] += 1
            audit[TIMING_VARIANT]["paired_direction_matches"] += 1

        # Generate each ablation independently. Conditioning an ablation's existence
        # on the base signal would selection-bias robustness coverage.
        for source in (*SPOT_SOURCES, DERIBIT_SOURCE):
            variant = f"omit_{source}"
            ablation_bound = opening * calibration.ablation_residual_bounds_bp.get(
                source, calibration.residual_bound_bp,
            ) / 10_000.0
            ablated = make_proxy(variant, decision_ms, source, ablation_bound)
            if base_signal is not None:
                audit[variant]["paired_base_signals"] += 1
                if ablated is not None:
                    audit[variant]["paired_available_signals"] += 1
                    audit[variant]["paired_direction_matches"] += (
                        ablated.direction == base_signal.direction
                    )

        baseline_close = _delayed_chainlink_close(exact, received, close_boundary, decision_ms)
        if baseline_close is None:
            audit[BASELINE_VARIANT]["proxy_unavailable"] += 1
        else:
            baseline_estimate = ProxyWindowEstimate(
                baseline_close, baseline_close, None, decision_ms,
            )
            baseline = _signal_for_estimate(
                BASELINE_VARIANT, mapping, decision_ms, opening, baseline_estimate,
                actual_winner, bound, require_agreement=False,
            )
            if baseline is None:
                audit[BASELINE_VARIANT]["margin_or_agreement_gate"] += 1
            else:
                signals[BASELINE_VARIANT].append(baseline)
                audit[BASELINE_VARIANT]["signals"] += 1
                if baseline.actual_winner is not None:
                    audit[BASELINE_VARIANT]["direction_correct"] += (
                        baseline.direction == baseline.actual_winner
                    )
    return signals, {variant: dict(counter) for variant, counter in audit.items()}


def outcome_observations(
    mappings: Iterable[Mapping[str, Any]],
    official_outcomes: Mapping[str, str],
    chainlink_points: Iterable[ChainlinkPoint],
) -> list[dict[str, Any]]:
    """Build per-market settlement observations without discarding prefix identity."""
    exact = {point.payload_ms: point for point in _normalised_chainlink(chainlink_points)}
    rows = []
    for mapping in mappings:
        market_id = str(mapping["market_id"])
        slot = int(mapping["slot"])
        official = official_outcomes.get(market_id)
        if official is None:
            rows.append({
                "market_id": market_id,
                "slot": slot,
                "official": None,
                "derived": None,
                "status": "unresolved",
            })
            continue
        opening = _window_average(exact, slot * 1_000.0)
        closing = _window_average(exact, (slot + 300) * 1_000.0)
        if opening is None or closing is None:
            rows.append({
                "market_id": market_id,
                "slot": slot,
                "official": official,
                "derived": None,
                "status": "missing_exact",
            })
            continue
        derived = "Up" if closing >= opening else "Down"
        rows.append({
            "market_id": market_id,
            "slot": slot,
            "official": official,
            "derived": derived,
            "status": "compared",
        })
    return rows


def summarize_outcome_observations(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Verify the reconstructed settlement rule for an explicit market prefix."""
    rows = list(rows)
    compared_rows = [row for row in rows if row.get("status") == "compared"]
    missing_exact = sum(row.get("status") == "missing_exact" for row in rows)
    unresolved = [str(row["market_id"]) for row in rows if row.get("status") == "unresolved"]
    mismatches = [
        {
            "market_id": str(row["market_id"]),
            "slot": int(row["slot"]),
            "official": str(row["official"]),
            "derived": str(row["derived"]),
        }
        for row in compared_rows
        if row.get("derived") != row.get("official")
    ]
    compared = len(compared_rows)
    eligible = compared + missing_exact
    coverage = compared / eligible if eligible else 0.0
    return {
        "compared": compared,
        "missing_exact": missing_exact,
        "unresolved_market_count": len(unresolved),
        "unresolved_market_ids": unresolved[:20],
        "coverage": coverage,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:20],
        "passes": compared > 0 and coverage >= 0.95 and not mismatches and not unresolved,
    }


def audit_official_outcomes(
    mappings: Iterable[Mapping[str, Any]],
    official_outcomes: Mapping[str, str],
    chainlink_points: Iterable[ChainlinkPoint],
) -> dict[str, Any]:
    """Verify the reconstructed settlement rule against resolved market outcomes."""
    return summarize_outcome_observations(
        outcome_observations(mappings, official_outcomes, chainlink_points)
    )


def execution_config(fee_rate: float = 0.07) -> h_replay.ReplayConfig:
    config = h_replay.ReplayConfig(
        book_lag_ms=0.0,
        local_path_ms=0.86,
        order_wire_ms=0.0,
        venue_hold_ms=150.0,
        evaluation_ms=(300.0, 500.0),
        theta=0.025,
        tick=0.01,
        fee_rate=fee_rate,
        base_shares=5.0,
        reentry_min_ms=2_000.0,
        reverse_multiplier=1.0,
        pilot_min_usd=1.0,
        max_order_usd=5.0,
        signal_time_basis="synthetic_receive_minus_lag",
        retry_after_no_send_or_kill=False,
        reentry_enabled=False,
        max_orders_per_min=0,
    )
    if not math.isclose(fee_rate, 0.07, abs_tol=1e-12):
        raise ValueError(f"unsupported BTC 5m fee rate {fee_rate}; expected frozen 0.07")
    if h_replay.limit_price(1.0, config.theta, config.tick, config.fee_rate) != 0.97:
        raise AssertionError("settlement execution no longer freezes a 0.97 limit")
    return config


def _direct_clob_batch(
    batch: Mapping[str, Any],
    machines: Mapping[str, h_replay.HReplay],
    token_index: Mapping[str, tuple[h_replay_run.DirectMarketBook, str]],
    connected: bool,
) -> bool:
    receive_ms = float(batch["recv_ms"])
    touched: dict[str, h_replay_run.DirectMarketBook] = {}
    source_values = []
    for event in batch["events"]:
        kind = event["kind"]
        if kind == "clob_error":
            for machine in machines.values():
                machine.feed({"kind": "disconnect", "receive_ms": receive_ms})
            for market, _ in token_index.values():
                market.up = h_replay_run.TokenBook()
                market.down = h_replay_run.TokenBook()
            connected = False
            continue
        if kind == "clob_connection":
            for machine in machines.values():
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
        source_values.append(source_ms)
        applied = False
        if kind == "clob_snapshot" and source_ms >= token.source_ms:
            token.replace(event["bids"], event["asks"], source_ms)
            applied = True
        elif kind == "clob_price_change":
            applied = token.change(
                str(event["side"]), float(event["price"]), float(event["size"]),
                source_ms, event.get("best_bid"), event.get("best_ask"),
            )
        if applied:
            touched[market.market_id] = market
    for market in touched.values():
        for machine in machines.values():
            if market.ready:
                machine.feed({
                    "kind": "snapshot",
                    "market_id": market.market_id,
                    "receive_ms": receive_ms,
                    # Match conservatively from the book already received by this time.
                    # Exchange source timestamps still guard update ordering inside TokenBook,
                    # but never pull a late-arriving update backwards into an earlier fill.
                    "source_ms": receive_ms,
                    "up_asks": market.up.ask_levels(),
                    "down_asks": market.down.ask_levels(),
                    "advance_watermark": False,
                })
            else:
                machine.feed({
                    "kind": "invalidate",
                    "market_id": market.market_id,
                    "receive_ms": receive_ms,
                    "source_ms": receive_ms,
                    "advance_watermark": False,
                })
    if source_values:
        for machine in machines.values():
            machine.feed({
                "kind": "watermark", "receive_ms": receive_ms, "source_ms": receive_ms,
            })
    return connected


def replay_execution(
    signals: Mapping[str, Sequence[ProxySignal]],
    clob_events: Iterable[dict[str, Any]],
    mappings: Iterable[dict[str, Any]],
    config: h_replay.ReplayConfig | None = None,
) -> dict[str, list[dict[str, Any]]]:
    config = config or execution_config()
    _, token_index = h_replay_run._mapping_dict(mappings)
    machines = {variant: h_replay.HReplay(config) for variant in signals}
    signal_events = []
    signal_lookup: dict[tuple[str, str], ProxySignal] = {}
    sequence = 0
    for variant, rows in signals.items():
        for signal in rows:
            signal_lookup[(variant, signal.market_id)] = signal
            signal_events.append((signal.decision_ms, 0, sequence, variant, signal))
            sequence += 1
    signal_events.sort()
    batches = (
        (float(batch["recv_ms"]), 1, int(batch["seq"]), None, batch)
        for batch in h_replay_run._clob_batches(clob_events)
    )
    connected = False
    for _, kind_order, _, variant, payload in heapq.merge(signal_events, batches):
        if kind_order == 0:
            signal = payload
            machines[str(variant)].feed({
                "kind": "signal",
                "market_id": signal.market_id,
                "receive_ms": signal.decision_ms,
                "source_ms": signal.decision_ms,
                "signal_source": signal.variant,
                "direction": signal.direction,
                "fair": 1.0,
                "trial_edge": signal.margin,
                "trigger_reason": "settlement_proxy",
            })
        else:
            connected = _direct_clob_batch(payload, machines, token_index, connected)
    output: dict[str, list[dict[str, Any]]] = {}
    for variant, machine in machines.items():
        machine.finish(force=False)
        rows = list(machine.records)
        rows.extend({
            **pending,
            "filled_shares": 0.0,
            "fill_price": None,
            "fill_fee": 0.0,
            "all_in_cost": None,
            "reason": "censored_end_of_recording",
        } for pending in machine.pending_evaluations())
        enriched = []
        for row in rows:
            signal = signal_lookup[(variant, str(row["market_id"]))]
            row["variant"] = variant
            market_close_ms = (signal.slot + 300) * 1_000.0
            evaluation_clock_ms = signal.decision_ms + float(row["evaluation_ms"])
            if evaluation_clock_ms >= market_close_ms:
                row.update({
                    "filled_shares": 0.0,
                    "fill_price": None,
                    "fill_fee": 0.0,
                    "all_in_cost": None,
                    "reason": "after_market_close",
                })
            row["winner"] = signal.actual_winner
            row["won"] = (row["direction"] == signal.actual_winner
                          if signal.actual_winner is not None else None)
            row["match_book_clock"] = "receipt_ms"
            row["protocol_slot"] = signal.slot
            row["market_close_ms"] = market_close_ms
            row["day"] = datetime.fromtimestamp(
                signal.decision_ms / 1_000.0, timezone.utc,
            ).date().isoformat()
            row["qualified_fill"] = bool(
                row["filled_shares"] >= config.base_shares - 1e-9 and row["reason"] == "filled"
            )
            if row["filled_shares"] > 0 and row["won"] is not None:
                row["pnl_per_share"] = (1.0 if row["won"] else 0.0) - float(row["all_in_cost"])
                row["pnl"] = row["pnl_per_share"] * float(row["filled_shares"])
            else:
                row["pnl_per_share"] = None
                row["pnl"] = None
            enriched.append(row)
        output[variant] = enriched
    return output


def _daily_lower_99(fills: Sequence[Mapping[str, Any]]) -> Optional[float]:
    by_day: dict[str, list[Mapping[str, Any]]] = {}
    for row in fills:
        by_day.setdefault(str(row["day"]), []).append(row)
    if len(by_day) < 2:
        return None
    clusters = [
        (sum(float(row["pnl"]) for row in day_rows),
         sum(float(row["filled_shares"]) for row in day_rows))
        for day_rows in by_day.values()
    ]
    total_pnl = sum(pnl for pnl, _ in clusters)
    total_shares = sum(shares for _, shares in clusters)
    estimate = total_pnl / total_shares
    influence = [pnl - estimate * shares for pnl, shares in clusters]
    cluster_count = len(clusters)
    standard_error = (
        math.sqrt(cluster_count / (cluster_count - 1) * sum(value * value for value in influence))
        / total_shares
    )
    try:
        from scipy.stats import t as student_t
    except ImportError:
        return None
    return estimate - float(student_t.ppf(0.99, cluster_count - 1)) * standard_error


def _row_protocol_key(row: Mapping[str, Any]) -> tuple[float, str]:
    return (
        float(row.get("protocol_slot", row["signal_ms"])),
        str(row["market_id"]),
    )


def _signal_protocol_key(signal: ProxySignal) -> tuple[float, str]:
    return float(signal.slot), signal.market_id


def _first_crossing_sample(
    rows: Iterable[Mapping[str, Any]],
    *,
    min_fills: int = MIN_STOPPING_FILLS,
    min_days: int = MIN_STOPPING_DAYS,
) -> tuple[list[Mapping[str, Any]], bool]:
    """Return the chronological prefix that first reaches both thresholds.

    Outcomes are not part of the crossing rule: an unresolved fill is still one
    of the first fills and may not be replaced by a later resolved trade.
    """
    ordered = sorted(rows, key=_row_protocol_key)
    fills = 0
    days: set[str] = set()
    for index, row in enumerate(ordered):
        if bool(row.get("qualified_fill")):
            fills += 1
            days.add(str(row["day"]))
        if fills >= min_fills and len(days) >= min_days:
            return ordered[:index + 1], True
    return ordered, False


def summarize_execution(
    rows: Mapping[str, Sequence[Mapping[str, Any]]],
    signals: Mapping[str, Sequence[ProxySignal]],
    *,
    min_stopping_fills: int = MIN_STOPPING_FILLS,
    min_stopping_days: int = MIN_STOPPING_DAYS,
) -> dict[str, dict[str, Any]]:
    primary_horizons: dict[
        float,
        tuple[list[Mapping[str, Any]], bool, tuple[float, str] | None],
    ] = {}
    for latency in (300.0, 500.0):
        available = [
            row for row in rows.get(BASE_VARIANT, ())
            if float(row["evaluation_ms"]) == latency
        ]
        sample, reached = _first_crossing_sample(
            available, min_fills=min_stopping_fills, min_days=min_stopping_days,
        )
        cutoff = None
        if reached:
            cutoff = _row_protocol_key(sample[-1])
        primary_horizons[latency] = (sample, reached, cutoff)

    result = {}
    for variant, variant_rows in rows.items():
        by_latency = {}
        for latency in (300.0, 500.0):
            available = [row for row in variant_rows if float(row["evaluation_ms"]) == latency]
            base_sample, stopping_reached, cutoff = primary_horizons[latency]
            if variant == BASE_VARIANT:
                sample = base_sample
            else:
                ordered = sorted(available, key=_row_protocol_key)
                sample = ordered if cutoff is None else [
                    row for row in ordered if _row_protocol_key(row) <= cutoff
                ]
            sample_signals = [
                signal for signal in signals[variant]
                if cutoff is None or _signal_protocol_key(signal) <= cutoff
            ]
            base_sample_signals = [
                signal for signal in signals[BASE_VARIANT]
                if cutoff is None or _signal_protocol_key(signal) <= cutoff
            ]
            base_by_market = {signal.market_id: signal for signal in base_sample_signals}
            control_by_market = {signal.market_id: signal for signal in sample_signals}
            paired_available = sum(market_id in control_by_market for market_id in base_by_market)
            paired_direction_matches = sum(
                control_by_market[market_id].direction == base_signal.direction
                for market_id, base_signal in base_by_market.items()
                if market_id in control_by_market
            )
            paired_base = len(base_by_market)
            labelled_signals = [
                signal for signal in sample_signals if signal.actual_winner is not None
            ]
            direction_correct = sum(
                signal.direction == signal.actual_winner for signal in labelled_signals
            )
            executions = [row for row in sample
                          if float(row["filled_shares"]) > 0 and row["winner"] is not None]
            qualified = [row for row in executions if row["qualified_fill"]]
            unresolved_market_ids = sorted({
                str(row["market_id"]) for row in sample if row.get("winner") is None
            })
            total_shares = sum(float(row["filled_shares"]) for row in executions)
            total_pnl = sum(float(row["pnl"]) for row in executions)
            sends = sum(bool(row["sent"]) for row in sample)
            raw_exact_p = h_replay_run._exact_pvalue(executions)
            corrected_exact_p = min(1.0, raw_exact_p * FAMILY_TESTS)
            by_latency[str(int(latency))] = {
                "signals": len(sample_signals),
                "labelled_signals": len(labelled_signals),
                "sends": sends,
                "executions_including_partials": len(executions),
                "fills": len(qualified),
                "partial_fills": sum(0 < float(row["filled_shares"]) < 5.0 - 1e-9
                                     for row in executions),
                "fill_rate_per_signal": len(qualified) / len(sample_signals)
                if sample_signals else 0.0,
                "any_execution_rate_per_send": len(executions) / sends if sends else 0.0,
                "fill_rate_per_send": len(qualified) / sends if sends else 0.0,
                "direction_accuracy": direction_correct / len(labelled_signals)
                if labelled_signals else None,
                "net_ev_per_share": total_pnl / total_shares if total_shares else None,
                "pnl": total_pnl,
                "pnl_per_signal": total_pnl / len(sample_signals) if sample_signals else 0.0,
                "shares": total_shares,
                "days": len({str(row["day"]) for row in qualified}),
                "day_cluster_lower_99": _daily_lower_99(executions),
                "reasons": dict(Counter(str(row["reason"]) for row in sample)),
                "raw_exact_p": raw_exact_p,
                "corrected_exact_p": corrected_exact_p,
                "family_tests": FAMILY_TESTS,
                "stopping_sample_reached": stopping_reached,
                "stopping_last_signal_ms": (
                    float(base_sample[-1]["signal_ms"]) if stopping_reached else None
                ),
                "stopping_last_protocol_slot": (cutoff[0] if cutoff is not None else None),
                "stopping_last_market_id": (cutoff[1] if cutoff is not None else None),
                "paired_base_signals": paired_base,
                "paired_available_signals": paired_available,
                "paired_direction_matches": paired_direction_matches,
                "coverage_vs_base": paired_available / paired_base if paired_base else 0.0,
                "direction_match_rate": (
                    paired_direction_matches / paired_base if paired_base else 0.0
                ),
                "control_only_signals": sum(
                    signal.market_id not in base_by_market for signal in sample_signals
                ),
                "unresolved_market_count": len(unresolved_market_ids),
                "unresolved_market_ids": unresolved_market_ids[:20],
            }
        result[variant] = by_latency
    return result


def evaluate_observations(
    rows: Mapping[str, Sequence[Mapping[str, Any]]],
    signals: Mapping[str, Sequence[ProxySignal]],
    outcome_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Evaluate a pooled chronological sample under the frozen stopping rule."""
    execution = summarize_execution(rows, signals)
    base_300 = execution[BASE_VARIANT]["300"]
    base_500 = execution[BASE_VARIANT]["500"]
    base_rows = (base_300, base_500)
    observed_outcome_audit = summarize_outcome_observations(outcome_rows)
    if all(row["stopping_sample_reached"] for row in base_rows):
        cutoffs = [
            (float(row["stopping_last_protocol_slot"]), str(row["stopping_last_market_id"]))
            for row in base_rows
        ]
        judged_market_ids = {
            signal.market_id
            for variant_signals in signals.values()
            for signal in variant_signals
            if any(_signal_protocol_key(signal) <= cutoff for cutoff in cutoffs)
        }
        outcome_audit = summarize_outcome_observations(
            row for row in outcome_rows if str(row["market_id"]) in judged_market_ids
        )
    else:
        outcome_audit = observed_outcome_audit

    robust_variants = [
        variant for variant in execution
        if variant.startswith("omit_") or variant == TIMING_VARIANT
    ]
    robustness = {
        variant: {
            "coverage_vs_base_300": execution[variant]["300"]["coverage_vs_base"],
            "coverage_vs_base_500": execution[variant]["500"]["coverage_vs_base"],
            "direction_match_rate_300": execution[variant]["300"]["direction_match_rate"],
            "direction_match_rate_500": execution[variant]["500"]["direction_match_rate"],
            "ev_300": execution[variant]["300"]["net_ev_per_share"],
            "ev_500": execution[variant]["500"]["net_ev_per_share"],
            "unresolved_300": execution[variant]["300"]["unresolved_market_count"],
            "unresolved_500": execution[variant]["500"]["unresolved_market_count"],
        }
        for variant in robust_variants
    }
    positive_robustness = all(
        row["coverage_vs_base_300"] >= 0.80
        and row["coverage_vs_base_500"] >= 0.80
        and row["direction_match_rate_300"] >= 0.80
        and row["direction_match_rate_500"] >= 0.80
        and row["ev_300"] is not None and row["ev_300"] > 0
        and row["ev_500"] is not None and row["ev_500"] > 0
        and row["unresolved_300"] == 0
        and row["unresolved_500"] == 0
        for row in robustness.values()
    )

    def statistical(row: Mapping[str, Any]) -> bool:
        return bool(
            row["stopping_sample_reached"]
            and row["unresolved_market_count"] == 0
            and row["fills"] >= MIN_STOPPING_FILLS
            and row["days"] >= MIN_STOPPING_DAYS
            and row["day_cluster_lower_99"] is not None
            and row["day_cluster_lower_99"] > 0
            and row["corrected_exact_p"] < 0.01
        )

    statistical_by_latency = {"300": statistical(base_300), "500": statistical(base_500)}
    both_base_positive = all(
        row["fills"] and row["net_ev_per_share"] is not None and row["net_ev_per_share"] > 0
        for row in (base_300, base_500)
    )
    baseline_increment = {
        latency: execution[BASE_VARIANT][latency]["pnl_per_signal"]
        - execution[BASELINE_VARIANT][latency]["pnl_per_signal"]
        for latency in ("300", "500")
    }
    beats_baseline = all(value > 0 for value in baseline_increment.values())
    all_evaluated_outcomes_resolved = all(
        execution[variant][latency]["unresolved_market_count"] == 0
        for variant in execution
        for latency in ("300", "500")
    )
    paper_gate_passes = bool(
        both_base_positive and positive_robustness and beats_baseline
        and all_evaluated_outcomes_resolved
        and outcome_audit["passes"] and all(statistical_by_latency.values())
    )
    gate = {
        # Displayed L2 proves only that depth survived to the checkpoint. It cannot
        # certify queue priority or hidden-liquidity competition, so overall passage
        # remains false until a separately authorised bounded live FAK calibration.
        "passes": False,
        "paper_gate_passes": paper_gate_passes,
        "live_fak_calibration_passes": False,
        "requires_live_fak_calibration": True,
        "base_300ms_and_500ms_positive": both_base_positive,
        "beats_delayed_chainlink_baseline": beats_baseline,
        "pnl_per_signal_increment_vs_baseline": baseline_increment,
        "all_source_ablations_and_timing_positive": positive_robustness,
        "all_evaluated_outcomes_resolved": all_evaluated_outcomes_resolved,
        "robustness": robustness,
        "settlement_rule_matches_official_outcomes": outcome_audit["passes"],
        "statistical_gate_by_latency": statistical_by_latency,
        "required_fills": MIN_STOPPING_FILLS,
        "required_days": MIN_STOPPING_DAYS,
        "paper_depth_warning": (
            "displayed direct depth does not prove queue priority or hidden live FAK fill"
        ),
    }
    return {
        "execution": execution,
        "outcome_audit": outcome_audit,
        "observed_outcome_audit": observed_outcome_audit,
        "gate": gate,
    }


def paper_verdict(result: Mapping[str, Any]) -> dict[str, Any]:
    base = result["execution"][BASE_VARIANT]
    primary_rows = [base["300"], base["500"]]
    evaluated_rows = [
        latency_row
        for variant_rows in result["execution"].values()
        for latency_row in (variant_rows["300"], variant_rows["500"])
    ]
    ready = bool(
        all(row["stopping_sample_reached"] for row in primary_rows)
        and all(row["unresolved_market_count"] == 0 for row in evaluated_rows)
        and result["outcome_audit"]["unresolved_market_count"] == 0
    )
    if not ready:
        return {
            "status": "collecting",
            "fills_300": base["300"]["fills"],
            "fills_500": base["500"]["fills"],
            "required_fills": MIN_STOPPING_FILLS,
            "required_days": MIN_STOPPING_DAYS,
        }
    return {
        "status": "paper_passed" if result["gate"]["paper_gate_passes"] else "rejected",
        "paper_gate_passes": bool(result["gate"]["paper_gate_passes"]),
        "fills_300": base["300"]["fills"],
        "fills_500": base["500"]["fills"],
        "last_signal_ms_300": base["300"]["stopping_last_signal_ms"],
        "last_signal_ms_500": base["500"]["stopping_last_signal_ms"],
        "net_ev_per_share_300": base["300"]["net_ev_per_share"],
        "net_ev_per_share_500": base["500"]["net_ev_per_share"],
        "requires_live_fak_calibration": True,
    }


def pin_verdict(
    result: Mapping[str, Any],
    path: str | Path,
    fingerprint: str,
) -> dict[str, Any]:
    destination = Path(path)
    if destination.exists():
        payload = json.loads(destination.read_text(encoding="utf-8"))
        if payload.get("strategy_fingerprint") != fingerprint:
            raise ValueError("pinned verdict belongs to another protocol")
        return payload
    verdict = paper_verdict(result)
    if verdict["status"] == "collecting":
        return verdict
    payload = {"strategy_fingerprint": fingerprint, "verdict": verdict}
    _atomic_json(destination, payload)
    return payload


def _iter_recorder_lines(paths: Sequence[Path]) -> Iterator[tuple[float, str]]:
    for path in sorted(paths):
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            for line in stream:
                clock, separator, payload = line.partition("\t")
                if not separator:
                    continue
                try:
                    yield int(clock) / 1_000_000.0, payload
                except ValueError:
                    continue


def _load_source(root: Path, source: str, config: ProxyConfig) -> tuple[StepSeries, int]:
    state = FrameState()
    last_frame_ms: Optional[float] = None
    receives, values = array("d"), array("d")
    pending_bucket: Optional[int] = None
    pending: Optional[PricePoint] = None
    count = 0
    for receive_ms, text in _iter_recorder_lines(list(root.glob(f"{source}.*.txt.gz"))):
        if (last_frame_ms is not None
                and receive_ms - last_frame_ms > config.max_quote_age_ms):
            # A long silence is a connection epoch boundary for stateful books.
            # Require a fresh Kraken snapshot / both Bybit sides afterwards.
            state = FrameState()
        last_frame_ms = receive_ms
        if not any(marker in text for marker in PRICE_FRAME_MARKERS[source]):
            continue
        points, _ = parse_raw_frame(source, receive_ms, text, state)
        for point in points:
            count += 1
            bucket = int(point.receive_ms // config.downsample_ms)
            if pending_bucket is not None and bucket != pending_bucket and pending is not None:
                receives.append(pending.receive_ms)
                values.append(pending.value)
            pending_bucket, pending = bucket, point
    if pending is not None:
        receives.append(pending.receive_ms)
        values.append(pending.value)
    if not receives:
        raise ValueError(f"no usable {source} prices in {root}")
    return StepSeries(receives, values), count


def load_raw_archive(
    root: str | Path,
    config: ProxyConfig | None = None,
) -> tuple[dict[str, StepSeries], list[ChainlinkPoint], dict[str, Any]]:
    config = config or ProxyConfig()
    root = Path(root)
    series = {}
    stats = {}
    for raw_source, normalized in [
        ("bn_spot", "bn_spot"), ("coinbase", "coinbase"), ("kraken", "kraken"),
        ("bitstamp", "bitstamp"), ("okx", "okx"), ("bybit_spot", "bybit_spot"),
        ("deribit", DERIBIT_SOURCE),
    ]:
        source_series, raw_count = _load_source(root, raw_source, config)
        series[normalized] = source_series
        stats[normalized] = {"raw_points": raw_count, "downsampled_points": len(source_series)}
    chainlink = []
    state = FrameState()
    for receive_ms, text in _iter_recorder_lines(list(root.glob("rtds_cl.*.txt.gz"))):
        _, rows = parse_raw_frame("rtds_cl", receive_ms, text, state)
        chainlink.extend(rows)
    chainlink = _normalised_chainlink(chainlink)
    if not chainlink:
        raise ValueError(f"no usable Chainlink prices in {root}")
    stats["chainlink"] = {"points": len(chainlink)}
    return series, chainlink, stats


def _strict_root(path: str | Path) -> Path:
    root = Path(path)
    return root / "strict" if (root / "strict" / "manifest.json").exists() else root


def _iso_timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1_000.0


def run(
    venues_root: str | Path,
    poly_root: str | Path,
    calibration_end_ms: float,
    holdout_start_ms: float,
    config: ProxyConfig | None = None,
    *,
    calibration_pin: str | Path | None = None,
    fingerprint: str | None = None,
    include_observations: bool = False,
) -> dict[str, Any]:
    config = config or ProxyConfig()
    if holdout_start_ms < calibration_end_ms:
        raise ValueError("holdout start cannot precede calibration end")
    strict = _strict_root(poly_root)
    h_replay_archive.validate_standard_artifact(strict)
    series, chainlink, source_stats = load_raw_archive(venues_root, config)
    if calibration_pin is None:
        calibration = fit_calibration(series, chainlink, calibration_end_ms, config)
    else:
        if fingerprint is None:
            raise ValueError("a strategy fingerprint is required when pinning calibration")
        calibration = load_or_pin_calibration(
            series, chainlink, calibration_end_ms, config, calibration_pin, fingerprint,
        )
    mappings = list(h_replay_archive.iter_market_mappings(strict / "market_registry.csv.gz"))
    fee_rates = {float(mapping["fee_rate"]) for mapping in mappings if "fee_rate" in mapping}
    if (len(fee_rates) != 1 or any("fee_rate" not in mapping for mapping in mappings)
            or not math.isclose(next(iter(fee_rates), math.nan), 0.07, abs_tol=1e-12)):
        raise ValueError("every market must carry the frozen 0.07 taker fee rate")
    fee_rate = next(iter(fee_rates))
    official_outcomes = {
        str(row["market_id"]): str(row["winner"])
        for row in h_replay_archive.iter_outcomes(strict / "market_outcomes.csv.gz")
    }
    holdout_mappings = [
        mapping for mapping in mappings
        if int(mapping["slot"]) * 1_000.0 >= holdout_start_ms
    ]
    outcome_rows = outcome_observations(
        holdout_mappings, official_outcomes, chainlink,
    )
    signals, signal_audit = generate_signals(
        holdout_mappings, series, chainlink, calibration, holdout_start_ms, config,
        official_outcomes=official_outcomes,
    )
    execution_rows = replay_execution(
        signals,
        h_replay_archive.iter_normalized_events(strict / "clob_events.jsonl.gz", family="clob"),
        mappings,
        execution_config(fee_rate),
    )
    evaluation = evaluate_observations(execution_rows, signals, outcome_rows)
    result = {
        "protocol": {
            "calibration_end_ms": calibration_end_ms,
            "holdout_start_ms": holdout_start_ms,
            "window_payload_offsets_s": [-62, -2, 61],
            "decision_lead_ms": config.decision_lead_ms,
            "timing_placebo_delay_ms": config.timing_placebo_delay_ms,
            "execution_ms": [300, 500],
            "shares": 5,
            "fixed_limit": 0.97,
            "fee_rate": fee_rate,
        },
        "source_stats": source_stats,
        "calibration": asdict(calibration),
        "signals": signal_audit,
        **evaluation,
    }
    if include_observations:
        result["observations"] = {
            "signals": {
                variant: [asdict(signal) for signal in variant_signals]
                for variant, variant_signals in signals.items()
            },
            "execution_rows": execution_rows,
            "outcome_rows": outcome_rows,
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venues", required=True, help="raw venue recorder directory")
    parser.add_argument("--poly", required=True, help="completed strict Polymarket replay bundle")
    parser.add_argument("--freeze", default=str(FREEZE_PATH), help="frozen protocol JSON")
    parser.add_argument("--calibration-end", help="must equal the frozen exclusive cutoff")
    parser.add_argument("--holdout-start", help="must equal the frozen inclusive holdout start")
    parser.add_argument("--calibration-pin", default=str(CALIBRATION_PIN))
    parser.add_argument("--verdict", default=str(VERDICT_PIN))
    parser.add_argument("--output", help="optional single current JSON result path")
    args = parser.parse_args()
    freeze = load_freeze(args.freeze)
    frozen_calibration_end = _iso_timestamp(freeze["calibration_end"])
    frozen_holdout_start = _iso_timestamp(freeze["holdout_start"])
    calibration_end = _iso_timestamp(args.calibration_end) if args.calibration_end else frozen_calibration_end
    holdout_start = _iso_timestamp(args.holdout_start) if args.holdout_start else frozen_holdout_start
    if not math.isclose(calibration_end, frozen_calibration_end):
        raise SystemExit("calibration cutoff differs from the frozen protocol")
    if not math.isclose(holdout_start, frozen_holdout_start):
        raise SystemExit("holdout start differs from the frozen protocol")
    if holdout_start < calibration_end:
        raise SystemExit("holdout start cannot precede calibration end")
    config = ProxyConfig(**freeze["proxy_config"])
    fingerprint = strategy_fingerprint(freeze)
    result = run(
        args.venues,
        args.poly,
        calibration_end,
        holdout_start,
        config,
        calibration_pin=args.calibration_pin,
        fingerprint=fingerprint,
    )
    result["strategy_fingerprint"] = fingerprint
    result["verdict"] = pin_verdict(result, args.verdict, fingerprint)
    text = json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    print(text, end="")


if __name__ == "__main__":
    main()
