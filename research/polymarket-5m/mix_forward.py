"""Strict paper replay for the frozen MIX rules and their receipt-causal jump-z control."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import heapq
import json
import math
import os
import platform
from bisect import bisect_right, insort_right
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

import numpy as np

import h_replay_archive as archive
import h_replay_run as hrun
import mix_hf as mix


CONTROL_CUT = 5.834074974060059
PRIMARY_LATENCY_MS = 500.0
MIN_FILLS = 1_000
MIN_DAYS = 7
FAMILY_TESTS = 100
ALPHA = 0.01
FREEZE_SCHEMA = "polymarket-mix-control-forward-v1"
STRATEGY_ID = "mix-r1-jump-z-settle-control"


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha(value: Mapping[str, Any]) -> str:
    payload = {key: item for key, item in value.items() if key != "protocol_sha256"}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _atomic_json(path: str | Path, value: Mapping[str, Any]) -> None:
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def load_control_freeze(path: str | Path) -> tuple[dict[str, Any], "ControlConfig"]:
    path = Path(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != FREEZE_SCHEMA or payload.get("strategy_id") != STRATEGY_ID:
        raise ValueError("unknown MIX control freeze")
    if payload.get("protocol_sha256") != _canonical_sha(payload):
        raise ValueError("MIX control freeze fingerprint mismatch")
    expected_protocol = os.environ.get("EXPECTED_PROTOCOL_SHA256")
    if not expected_protocol:
        raise ValueError("EXPECTED_PROTOCOL_SHA256 is required")
    if payload.get("protocol_sha256") != expected_protocol:
        raise ValueError("MIX control freeze does not match EXPECTED_PROTOCOL_SHA256")
    if payload.get("runner_sha256") != sha256_file(__file__):
        raise ValueError("MIX control freeze does not match the audited runner")
    expected = {
        "z_cut": CONTROL_CUT,
        "primary_latency_ms": PRIMARY_LATENCY_MS,
        "min_fills": MIN_FILLS,
        "min_days": MIN_DAYS,
        "family_tests": FAMILY_TESTS,
        "alpha": ALPHA,
    }
    bad = [key for key, value in expected.items() if payload.get(key) != value]
    if bad:
        raise ValueError("MIX control freeze differs from code: " + ", ".join(bad))
    historical = (path.parent / str(payload["historical_frozen_file"])).resolve()
    if payload.get("historical_frozen_sha256") != sha256_file(historical):
        raise ValueError("MIX historical freeze sha256 mismatch")
    start = datetime.fromisoformat(str(payload["holdout_start_utc"]).replace("Z", "+00:00"))
    if start.tzinfo is None:
        raise ValueError("MIX holdout start must include a timezone")
    config = ControlConfig(
        jump_bp=float(payload["jump_bp"]), z_cut=float(payload["z_cut"]),
        reference_min_s=float(payload["reference_min_s"]),
        reference_max_s=float(payload["reference_max_s"]), spacing_s=float(payload["spacing_s"]),
        tau_hi_s=float(payload["tau_hi_s"]), tau_lo_s=float(payload["tau_lo_s"]),
        fixed_taus_s=tuple(int(value) for value in payload["fixed_taus_s"]),
        sigma_window_s=int(payload["sigma_window_s"]),
        sigma_min_observations=int(payload["sigma_min_observations"]),
        forward_fill_s=int(payload["forward_fill_s"]),
        fill_latencies_ms=tuple(float(value) for value in payload["fill_latencies_ms"]),
        shares=float(payload["shares"]), min_price=float(payload["min_price"]),
        limit_price=float(payload["limit_price"]),
        fee_rate=float(payload["fee_rate"]), book_fresh_ms=float(payload["book_fresh_ms"]),
        holdout_start_ms=start.timestamp() * 1_000.0,
    )
    if PRIMARY_LATENCY_MS not in config.fill_latencies_ms:
        raise ValueError("MIX primary latency is absent from the freeze")
    return payload, config


@dataclass(frozen=True)
class FrozenMixScorer:
    features: tuple[str, ...]
    rules: tuple[dict[str, Any], ...]
    models: Mapping[str, Any]

    def score(self, values: Mapping[str, float]) -> dict[str, float]:
        missing = [name for name in self.features if name not in values]
        if missing:
            raise ValueError("missing MIX features: " + ", ".join(missing))
        row = np.asarray([[float(values[name]) for name in self.features]], dtype=float)
        return {str(rule["id"]): float(self.models[str(rule["id"])].predict(row)[0]) for rule in self.rules}

    def selected(self, values: Mapping[str, float]) -> dict[str, float]:
        scores = self.score(values)
        return {str(rule["id"]): scores[str(rule["id"])] for rule in self.rules
                if scores[str(rule["id"])] >= float(rule["cut"])}


def load_scorer(frozen_path: str | Path) -> FrozenMixScorer:
    import joblib
    import sklearn

    frozen_path = Path(frozen_path)
    frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
    model_path, manifest_path = mix.model_bundle_paths(frozen_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != mix.MODEL_BUNDLE_FORMAT:
        raise ValueError("unknown MIX model bundle format")
    if manifest.get("frozen_sha256") != sha256_file(frozen_path):
        raise ValueError("MIX frozen sha256 mismatch")
    if manifest.get("features") != frozen.get("features"):
        raise ValueError("MIX feature schema differs from the frozen study")
    frozen_rules = [{key: rule[key] for key in ("id", "policy", "model", "q", "cut")}
                    for rule in frozen.get("rules", [])]
    if manifest.get("rules") != frozen_rules:
        raise ValueError("MIX model rules differ from the frozen study")
    if manifest.get("a_fingerprint") != frozen.get("a_fingerprint"):
        raise ValueError("MIX A fingerprint mismatch")
    if manifest.get("model_file") != model_path.name or manifest.get("model_sha256") != sha256_file(model_path):
        raise ValueError("MIX model bundle sha256 mismatch")
    payload = joblib.load(model_path)
    runtime = {"python": platform.python_version(), "numpy": np.__version__, "sklearn": sklearn.__version__,
               "joblib": joblib.__version__}
    if manifest.get("versions") != runtime:
        raise ValueError("MIX model runtime differs from the fitted bundle")
    if (payload.get("format") != mix.MODEL_BUNDLE_FORMAT or
            payload.get("frozen_sha256") != manifest.get("frozen_sha256") or
            payload.get("features") != manifest.get("features") or
            payload.get("rules") != manifest.get("rules") or
            payload.get("versions") != manifest.get("versions")):
        raise ValueError("MIX model payload differs from its manifest")
    return FrozenMixScorer(tuple(manifest["features"]), tuple(manifest["rules"]), payload["models"])


@dataclass(frozen=True)
class ControlConfig:
    jump_bp: float = 2.0
    z_cut: float = CONTROL_CUT
    reference_min_s: float = 1.0
    reference_max_s: float = 5.0
    spacing_s: float = 5.0
    tau_hi_s: float = 285.0
    tau_lo_s: float = 20.0
    fixed_taus_s: tuple[int, ...] = (240, 180, 120, 60)
    sigma_window_s: int = 600
    sigma_min_observations: int = 300
    forward_fill_s: int = 20
    fill_latencies_ms: tuple[float, ...] = (300.0, 400.0, 500.0)
    shares: float = 5.0
    min_price: float = 0.02
    limit_price: float = 0.98
    fee_rate: float = 0.07
    book_fresh_ms: float = 1_000.0
    holdout_start_ms: Optional[float] = None


class ReceiptSigma:
    """One-second receipt-clock volatility, excluding the current partial second."""

    def __init__(self, window: int, minimum: int, forward_fill_s: int) -> None:
        self.window = int(window)
        self.minimum = int(minimum)
        self.forward_fill_s = int(forward_fill_s)
        self.current_second: Optional[int] = None
        self.current_value = math.nan
        self.last_trade_second: Optional[int] = None
        self.last_trade_value = math.nan
        self.last_final_value = math.nan
        self.returns: deque[float] = deque()
        self.count = 0
        self.total = 0.0
        self.total_sq = 0.0

    def _push_return(self, value: float) -> None:
        self.returns.append(value)
        if math.isfinite(value):
            self.count += 1
            self.total += value
            self.total_sq += value * value
        if len(self.returns) > self.window:
            old = self.returns.popleft()
            if math.isfinite(old):
                self.count -= 1
                self.total -= old
                self.total_sq -= old * old

    def _finalize(self, value: float) -> None:
        difference = value - self.last_final_value if math.isfinite(value) and math.isfinite(self.last_final_value) else math.nan
        if math.isfinite(self.last_final_value) or self.returns:
            self._push_return(difference)
        self.last_final_value = value

    def advance(self, timestamp_s: float) -> None:
        target = int(math.floor(timestamp_s))
        if self.current_second is None:
            self.current_second = target
            return
        while self.current_second < target:
            self._finalize(self.current_value)
            self.current_second += 1
            if (self.last_trade_second is not None and
                    self.current_second - self.last_trade_second <= self.forward_fill_s):
                self.current_value = self.last_trade_value
            else:
                self.current_value = math.nan

    def update(self, timestamp_s: float, log_price: float) -> None:
        self.advance(timestamp_s)
        second = int(math.floor(timestamp_s))
        if self.current_second != second:
            return
        self.current_value = log_price
        self.last_trade_second = second
        self.last_trade_value = log_price

    def sigma(self, timestamp_s: float) -> float:
        self.advance(timestamp_s)
        if self.count < max(self.minimum, 2):
            return math.nan
        variance = (self.total_sq - self.total * self.total / self.count) / (self.count - 1)
        return math.sqrt(max(variance, 0.0))


@dataclass(frozen=True)
class ControlCandidate:
    slot: int
    receive_ms: float
    source_ms: float
    direction: int
    move: float
    sigma: float
    z: float
    kind: str


class ReceiptJumpTrigger:
    """MIX receipt-clock trigger, including raw-jump spacing before score filtering."""

    def __init__(self, config: ControlConfig | None = None) -> None:
        self.config = config or ControlConfig()
        self.grid = ReceiptSigma(self.config.sigma_window_s, self.config.sigma_min_observations,
                                 self.config.forward_fill_s)
        self.source_rows: list[tuple[float, int, float]] = []
        self.order = 0
        self.last_received: Optional[tuple[float, float, float, float]] = None
        self.last_kept: dict[int, float] = {}

    def reset(self) -> None:
        self.__init__(self.config)

    def _reference(self, source_s: float) -> Optional[float]:
        key = (source_s - self.config.reference_min_s + 1e-6, math.inf, math.inf)
        index = bisect_right(self.source_rows, key) - 1
        if index < 0:
            return None
        reference_s, _, reference_price = self.source_rows[index]
        if source_s - reference_s > self.config.reference_max_s + 1e-6:
            return None
        return reference_price

    def _candidate(self, receive_s: float, source_s: float, move: float, sigma: float, kind: str,
                   slot: Optional[int] = None) -> Optional[ControlCandidate]:
        if not (math.isfinite(move) and math.isfinite(sigma) and sigma > 0 and move != 0):
            return None
        slot = int(receive_s // 300) * 300 if slot is None else int(slot)
        tau = slot + 300 - receive_s
        if kind == "jump" and not (self.config.tau_lo_s - 1e-6 <= tau <= self.config.tau_hi_s + 1e-6):
            return None
        direction = 1 if move > 0 else -1
        return ControlCandidate(slot, receive_s * 1_000.0, source_s * 1_000.0, direction, move, sigma,
                                abs(move) / sigma, kind)

    def update(self, receive_ms: float, source_ms: float, price: float) -> Optional[ControlCandidate]:
        if not (math.isfinite(price) and price > 0):
            return None
        receive_s, source_s, log_price = receive_ms / 1_000.0, source_ms / 1_000.0, math.log(price)
        self.grid.update(receive_s, log_price)
        reference = self._reference(source_s)
        move = log_price - reference if reference is not None else math.nan
        insort_right(self.source_rows, (source_s, self.order, log_price))
        self.order += 1
        if self.source_rows:
            latest = self.source_rows[-1][0]
            cut = bisect_right(self.source_rows, (latest - 30.0, -1, -math.inf))
            if cut:
                del self.source_rows[:cut]
        self.last_received = (receive_s, source_s, log_price, move)
        if not math.isfinite(move) or abs(move) * 1e4 < self.config.jump_bp - 1e-9:
            return None
        slot = int(receive_s // 300) * 300
        last = self.last_kept.get(slot)
        if last is not None and receive_s < last + self.config.spacing_s - 1e-6:
            return None
        tau = slot + 300 - receive_s
        if not (self.config.tau_lo_s - 1e-6 <= tau <= self.config.tau_hi_s + 1e-6):
            return None
        self.last_kept[slot] = receive_s
        return self._candidate(receive_s, source_s, move, self.grid.sigma(receive_s), "jump", slot)

    def fixed(self, receive_ms: float, slot: int) -> Optional[ControlCandidate]:
        receive_s = receive_ms / 1_000.0
        sigma = self.grid.sigma(receive_s)
        if self.last_received is None or receive_s - self.last_received[0] > mix.PRICE_MAX_AGE_S + 1e-6:
            return None
        _, source_s, _, move = self.last_received
        return self._candidate(receive_s, source_s, move, sigma, "fixed", slot)


@dataclass(frozen=True)
class _BookState:
    receive_ms: float
    epoch: int
    up_ready: bool
    down_ready: bool
    up_source_ms: float
    down_source_ms: float
    up_last_change_ms: float
    down_last_change_ms: float
    up_asks: tuple[tuple[float, float], ...]
    down_asks: tuple[tuple[float, float], ...]


@dataclass
class _BookHistory:
    receives: list[float] = field(default_factory=list)
    states: list[_BookState] = field(default_factory=list)

    def push(self, state: _BookState) -> None:
        receive_ms = max(state.receive_ms, self.receives[-1]) if self.receives else state.receive_ms
        self.receives.append(receive_ms)
        self.states.append(_BookState(receive_ms, state.epoch, state.up_ready, state.down_ready,
                                      state.up_source_ms, state.down_source_ms,
                                      state.up_last_change_ms, state.down_last_change_ms,
                                      state.up_asks, state.down_asks))
        cutoff = receive_ms - 10_000.0
        index = bisect_right(self.receives, cutoff) - 1
        if index > 0:
            del self.receives[:index]
            del self.states[:index]

    def at(self, receive_ms: float) -> Optional[_BookState]:
        index = bisect_right(self.receives, receive_ms) - 1
        return self.states[index] if index >= 0 else None


def _direct_asks(market: hrun.DirectMarketBook, direction: str) -> tuple[tuple[float, float], ...]:
    token = market.up if direction == "Up" else market.down
    return tuple(sorted((float(price), float(size)) for price, size in token.asks.items() if size > 0))


def _token_uncrossed(token: hrun.TokenBook) -> bool:
    return bool(token.asks and (not token.bids or max(token.bids) < min(token.asks)))


def _fill_best(levels: tuple[tuple[float, float], ...], shares: float, limit: float,
               fee_rate: float, minimum: float = 0.02) -> tuple[float, Optional[float], float, str]:
    """Frozen MIX entry semantics: all five shares must exist at the single best ask."""
    if not levels:
        return 0.0, None, 0.0, "ask_missing"
    price, available = levels[0]
    if price < minimum - 1e-9:
        return 0.0, None, 0.0, "below_price_band"
    if price > limit + 1e-9:
        return 0.0, None, 0.0, "above_limit"
    if available < shares - 1e-9:
        return 0.0, None, 0.0, "insufficient_depth"
    fee = shares * fee_rate * price * (1.0 - price)
    return shares, price, fee, "filled"


def replay_normalized(source_events: Iterable[dict[str, Any]], clob_events: Iterable[dict[str, Any]],
                      mappings: Iterable[dict[str, Any]], outcomes: Mapping[str, str],
                      config: ControlConfig | None = None) -> tuple[list[dict[str, Any]], Counter]:
    config = config or ControlConfig()
    mapping_rows = list(mappings)
    markets, tokens = hrun._mapping_dict(mapping_rows)
    by_id = {market.market_id: market for market in markets.values()}
    histories = {market.market_id: _BookHistory() for market in markets.values()}
    book_signatures: dict[tuple[str, str], tuple[Any, ...]] = {}
    last_change_ms: dict[tuple[str, str], float] = {}
    trigger = ReceiptJumpTrigger(config)
    counters: Counter = Counter()
    rows: list[dict[str, Any]] = []
    pending: list[tuple[float, int, ControlCandidate, float, int]] = []
    sequence = 0
    connected = False
    active_epoch = 0
    watermark = -math.inf

    timers = sorted((int(row["slot"] + 300 - tau) * 1_000.0, int(row["slot"]))
                    for row in mapping_rows for tau in config.fixed_taus_s)
    base = hrun._merged_events(source_events, clob_events)
    decorated_base = ((float(event["recv_ms"]), 0, index, event) for index, event in enumerate(base))
    decorated_timers = ((timestamp, 1, index, {"kind": "fixed_timer", "recv_ms": timestamp, "slot": slot})
                        for index, (timestamp, slot) in enumerate(timers))

    def evaluate(candidate: ControlCandidate, latency: float, epoch: int, reason_override: Optional[str] = None) -> None:
        target = candidate.receive_ms + latency
        market = markets.get(candidate.slot)
        direction = "Up" if candidate.direction > 0 else "Down"
        state = histories.get(market.market_id if market else "", _BookHistory()).at(target) if market else None
        reason = reason_override
        quantity, price, fee = 0.0, None, 0.0
        if reason is None and market is None:
            reason = "market_missing"
        elif reason is None and state is None:
            reason = "book_unavailable"
        elif reason is None and (state.epoch != epoch or
                                 not (state.up_ready if direction == "Up" else state.down_ready)):
            reason = "book_epoch_changed"
        elif reason is None and target - (state.up_last_change_ms if direction == "Up" else
                                          state.down_last_change_ms) > config.book_fresh_ms + 1e-6:
            reason = "book_stale"
        elif reason is None:
            levels = state.up_asks if direction == "Up" else state.down_asks
            quantity, price, fee, reason = _fill_best(
                levels, config.shares, config.limit_price, config.fee_rate, config.min_price
            )
        filled = reason == "filled"
        winner = outcomes.get(market.market_id) if filled and market else None
        all_in = price + fee / quantity if filled and price is not None and quantity else None
        won = bool(winner == direction) if winner is not None else None
        pnl_per_share = ((1.0 if won else 0.0) - all_in) if filled and winner is not None else None
        rows.append({
            "market_id": market.market_id if market else None,
            "slot": candidate.slot,
            "kind": candidate.kind,
            "signal_receive_ms": candidate.receive_ms,
            "signal_source_ms": candidate.source_ms,
            "direction": direction,
            "jump_bp": candidate.move * candidate.direction * 1e4,
            "jump_z": candidate.z,
            "evaluation_ms": latency,
            "target_receive_ms": target,
            "book_source_ms": ((state.up_source_ms if direction == "Up" else state.down_source_ms)
                               if state is not None and math.isfinite(
                                   state.up_source_ms if direction == "Up" else state.down_source_ms)
                               else None),
            "limit_price": config.limit_price,
            "reason": reason,
            "filled": filled,
            "filled_shares": config.shares if filled else quantity,
            "fill_price": price if filled else None,
            "fill_fee": fee if filled else 0.0,
            "all_in_cost": all_in,
            "winner": winner,
            "won": won,
            "pnl_per_share": pnl_per_share,
            "pnl": pnl_per_share * config.shares if pnl_per_share is not None else None,
            "day": datetime.fromtimestamp(candidate.receive_ms / 1_000.0, timezone.utc).date().isoformat(),
        })

    def drain(inclusive: bool = True) -> None:
        threshold = watermark + 1e-6 if inclusive else math.nextafter(watermark, -math.inf)
        while pending and pending[0][0] <= threshold:
            _, _, candidate, latency, epoch = heapq.heappop(pending)
            evaluate(candidate, latency, epoch)

    def select(candidate: Optional[ControlCandidate]) -> None:
        nonlocal sequence
        if candidate is None:
            return
        counters[f"candidate_{candidate.kind}"] += 1
        if config.holdout_start_ms is not None and candidate.receive_ms < config.holdout_start_ms:
            counters["before_holdout"] += 1
            return
        if candidate.z < config.z_cut:
            counters["below_z_cut"] += 1
            return
        counters["selected"] += 1
        if not connected:
            for latency in config.fill_latencies_ms:
                evaluate(candidate, float(latency), active_epoch, "clob_disconnected")
            return
        for latency in config.fill_latencies_ms:
            heapq.heappush(pending, (candidate.receive_ms + float(latency), sequence, candidate,
                                    float(latency), active_epoch))
            sequence += 1

    for _, _, _, event in heapq.merge(decorated_base, decorated_timers, key=lambda item: item[:3]):
        kind = str(event["kind"])
        if kind == "fixed_timer":
            select(trigger.fixed(float(event["recv_ms"]), int(event["slot"])))
            continue
        if kind == "spot_connection" or kind == "spot_disconnect":
            trigger.reset()
            counters[kind] += 1
            watermark = max(watermark, float(event["recv_ms"]))
            drain(inclusive=False)
            continue
        if kind == "spot_trade":
            counters["spot_trades"] += 1
            select(trigger.update(float(event["recv_ms"]), float(event["source_ts_ms"]), float(event["price"])))
            watermark = max(watermark, float(event["recv_ms"]))
            drain(inclusive=False)
            continue
        if kind != "clob_batch":
            continue
        touched: dict[str, hrun.DirectMarketBook] = {}
        for change in event["events"]:
            change_kind = str(change["kind"])
            if change_kind == "clob_connection":
                connected = True
                active_epoch = int(change["connection_epoch"])
                for market in by_id.values():
                    market.up = hrun.TokenBook()
                    market.down = hrun.TokenBook()
                    for direction in ("Up", "Down"):
                        book_signatures.pop((market.market_id, direction), None)
                        last_change_ms[(market.market_id, direction)] = float(event["recv_ms"])
                    histories[market.market_id].push(_BookState(
                        float(event["recv_ms"]), active_epoch, False, False, -math.inf, -math.inf,
                        float(event["recv_ms"]), float(event["recv_ms"]), (), ()
                    ))
                continue
            if change_kind == "clob_error":
                connected = False
                for market in by_id.values():
                    market.up = hrun.TokenBook()
                    market.down = hrun.TokenBook()
                    for direction in ("Up", "Down"):
                        book_signatures.pop((market.market_id, direction), None)
                        last_change_ms[(market.market_id, direction)] = float(event["recv_ms"])
                    histories[market.market_id].push(_BookState(
                        float(event["recv_ms"]), active_epoch, False, False, -math.inf, -math.inf,
                        float(event["recv_ms"]), float(event["recv_ms"]), (), ()
                    ))
                continue
            if not connected:
                continue
            hit = tokens.get(str(change.get("asset_id")))
            if hit is None:
                continue
            market, direction = hit
            token = market.up if direction == "Up" else market.down
            source_ms = float(change["source_ts_ms"])
            if change_kind == "clob_snapshot" and source_ms >= token.source_ms:
                token.replace(change["bids"], change["asks"], source_ms)
            elif change_kind == "clob_price_change":
                token.change(str(change["side"]), float(change["price"]), float(change["size"]), source_ms,
                             change.get("best_bid"), change.get("best_ask"))
            touched[market.market_id] = market
        for market in touched.values():
            up_ready = _token_uncrossed(market.up)
            down_ready = _token_uncrossed(market.down)
            up_asks, down_asks = _direct_asks(market, "Up"), _direct_asks(market, "Down")
            def top(token: hrun.TokenBook) -> tuple[Optional[float], Optional[float], float, float]:
                bid = max(token.bids) if token.bids else None
                ask = min(token.asks) if token.asks else None
                return bid, ask, float(token.bids.get(bid, 0.0)), float(token.asks.get(ask, 0.0))

            for direction, token, ready in (("Up", market.up, up_ready),
                                            ("Down", market.down, down_ready)):
                key = (market.market_id, direction)
                signature = (ready, top(token))
                if signature != book_signatures.get(key):
                    last_change_ms[key] = float(event["recv_ms"])
                    book_signatures[key] = signature
            histories[market.market_id].push(_BookState(
                float(event["recv_ms"]), active_epoch, up_ready, down_ready,
                market.up.source_ms, market.down.source_ms,
                last_change_ms[(market.market_id, "Up")], last_change_ms[(market.market_id, "Down")],
                up_asks, down_asks
            ))
        watermark = max(watermark, float(event["recv_ms"]))
        drain()
    drain()
    while pending:
        _, _, candidate, latency, epoch = heapq.heappop(pending)
        evaluate(candidate, latency, epoch, "book_horizon_incomplete")
    return rows, counters


def iter_aggregate_spot_events(archive_dir: str | Path) -> Iterable[dict[str, Any]]:
    """Yield the aggregate-trade tape used by frozen MIX, preserving its receipt order."""
    root = Path(archive_dir)
    candidates = (root / "latency" / "binance_trades.jsonl.gz",
                  root.parent / "latency" / "binance_trades.jsonl.gz")
    path = next((candidate for candidate in candidates if candidate.exists()), None)
    if path is None:
        raise FileNotFoundError(f"MIX aggregate trade tape missing under {root}")
    previous_receive = -math.inf
    sequence = 0
    opened = False
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            row = json.loads(line)
            if row.get("event") != "BINANCE_WS_TRADE":
                continue
            try:
                receive_ms = float(row["receive_ts"]) * 1_000.0
                source_ms = float(row["trade_ts"]) * 1_000.0
                price = float(row["price"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"malformed MIX aggregate trade at {path}:{line_number}") from exc
            if receive_ms < previous_receive:
                raise ValueError(f"MIX aggregate trade receipt time regressed at {path}:{line_number}")
            if not (math.isfinite(source_ms) and math.isfinite(receive_ms) and math.isfinite(price) and price > 0):
                raise ValueError(f"invalid MIX aggregate trade at {path}:{line_number}")
            if not opened:
                yield {"kind": "spot_connection", "seq": sequence, "recv_ms": receive_ms,
                       "source_ts_ms": None, "stream": "mix_aggregate"}
                sequence += 1
                opened = True
            yield {"kind": "spot_trade", "seq": sequence, "recv_ms": receive_ms,
                   "source_ts_ms": source_ms, "price": price, "size": 0.0,
                   "stream": "mix_aggregate"}
            sequence += 1
            previous_receive = receive_ms
    if not opened:
        raise ValueError(f"MIX aggregate trade tape is empty: {path}")
    yield {"kind": "spot_disconnect", "seq": sequence, "recv_ms": previous_receive + 1e-3,
           "source_ts_ms": None, "stream": "mix_aggregate"}


def exact_pvalue(rows: list[dict[str, Any]]) -> float:
    if not rows:
        return 1.0
    probabilities = [min(max(float(row["all_in_cost"]), 0.0), 1.0) for row in rows]
    observed = sum(bool(row["won"]) for row in rows)
    pmf = [0.0] * (len(probabilities) + 1)
    pmf[0] = 1.0
    for index, probability in enumerate(probabilities):
        for wins in range(index + 1, 0, -1):
            pmf[wins] = pmf[wins] * (1.0 - probability) + pmf[wins - 1] * probability
        pmf[0] *= 1.0 - probability
    return min(max(sum(pmf[observed:]), 0.0), 1.0)


def daily_lower_99(rows: list[dict[str, Any]]) -> Optional[float]:
    from scipy.stats import t as student_t

    by_day: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
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
    if total_shares <= 0:
        return None
    estimate = total_pnl / total_shares
    influence = [pnl - estimate * shares for pnl, shares in clusters]
    count = len(clusters)
    standard_error = math.sqrt(count / (count - 1) * sum(value * value for value in influence)) / total_shares
    return estimate - float(student_t.ppf(0.99, count - 1)) * standard_error


def primary_market_fills(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(
        (row for row in rows if float(row["evaluation_ms"]) == PRIMARY_LATENCY_MS and row.get("filled")),
        key=lambda row: (float(row["signal_receive_ms"]), str(row.get("market_id")),
                         float(row.get("signal_source_ms", row["signal_receive_ms"])),
                         str(row.get("kind", "")), str(row.get("direction", ""))),
    )
    fills: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in ordered:
        market_id = str(row.get("market_id"))
        if market_id in seen:
            continue
        seen.add(market_id)
        fills.append(row)
    return fills


def stopping_sample(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fills = primary_market_fills(rows)
    days = set()
    for index, row in enumerate(fills):
        days.add(str(row["day"]))
        if index + 1 >= MIN_FILLS and len(days) >= MIN_DAYS:
            return fills[:index + 1]
    return []


def verdict(rows: list[dict[str, Any]]) -> dict[str, Any]:
    current = primary_market_fills(rows)
    sample = stopping_sample(rows)
    if not sample or any(row.get("winner") is None for row in sample):
        progress = sample or current
        collecting = {"status": "collecting", "fills": len(progress),
                      "days": len({str(row["day"]) for row in progress}),
                      "required_fills": MIN_FILLS, "required_days": MIN_DAYS}
        if sample:
            collecting["pending_outcomes"] = sum(row.get("winner") is None for row in sample)
        return collecting
    shares = sum(float(row["filled_shares"]) for row in sample)
    net_ev = sum(float(row["pnl"]) for row in sample) / shares
    lower = daily_lower_99(sample)
    raw_p = exact_pvalue(sample) if net_ev > 0 else 1.0
    corrected_p = min(1.0, raw_p * FAMILY_TESTS)
    passed = net_ev > 0 and lower is not None and lower > 0 and corrected_p < ALPHA
    return {"status": "passed" if passed else "rejected", "fills": len(sample),
            "days": len({str(row["day"]) for row in sample}), "net_ev_per_share": net_ev,
            "day_cluster_lower_99": lower, "raw_exact_p": raw_p, "corrected_exact_p": corrected_p,
            "family_tests": FAMILY_TESTS, "last_signal_ms": max(float(row["signal_receive_ms"]) for row in sample)}


def _read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def observation_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (str(row.get("market_id")), str(row.get("kind")), float(row["signal_source_ms"]),
            str(row["direction"]), float(row["evaluation_ms"]))


_OUTCOME_FIELD_NAMES = ("winner", "won", "pnl_per_share", "pnl")
_OUTCOME_FIELDS = frozenset(_OUTCOME_FIELD_NAMES)


def _execution_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key not in _OUTCOME_FIELDS}


def _outcome_values(row: Mapping[str, Any]) -> tuple[Any, ...]:
    values = tuple(row.get(field) for field in _OUTCOME_FIELD_NAMES)
    if any(value is None for value in values) and not all(value is None for value in values):
        raise ValueError("MIX observation has a partial outcome")
    if not row.get("filled") and any(value is not None for value in values):
        raise ValueError("unfilled MIX observation carries an outcome")
    return values


def _merge_observation(previous: dict[str, Any], row: dict[str, Any], key: tuple[Any, ...]) -> dict[str, Any]:
    if _execution_fields(previous) != _execution_fields(row):
        raise ValueError(f"execution-field conflict for MIX observation {key!r}")
    previous_outcome = _outcome_values(previous)
    new_outcome = _outcome_values(row)
    if previous_outcome == new_outcome:
        return previous
    if all(value is None for value in previous_outcome) and all(value is not None for value in new_outcome):
        return {**previous, **{field: row.get(field) for field in _OUTCOME_FIELD_NAMES}}
    raise ValueError(f"outcome conflict for MIX observation {key!r}")


def _ledger_sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (float(row["signal_receive_ms"]), str(row.get("market_id")),
            float(row["evaluation_ms"]), str(row["direction"]),
            str(row.get("kind")), float(row["signal_source_ms"]))


def _jsonl_bytes(rows: Iterable[Mapping[str, Any]]) -> bytes:
    return "".join(json.dumps(row, sort_keys=True, allow_nan=False) + "\n" for row in rows).encode()


def _jsonl_sha256(rows: Iterable[Mapping[str, Any]]) -> str:
    return hashlib.sha256(_jsonl_bytes(rows)).hexdigest()


def append_observations(destination: str | Path, new_rows: str | Path,
                        verdict_path: str | Path | None = None) -> list[dict[str, Any]]:
    """Append once: an already-recorded market/signal/latency can never be replaced by a rerun."""
    destination = Path(destination)
    pooled: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in [*_read_jsonl(destination), *_read_jsonl(new_rows)]:
        key = observation_key(row)
        _outcome_values(row)
        previous = pooled.get(key)
        pooled[key] = row if previous is None else _merge_observation(previous, row, key)
    rows = sorted(pooled.values(), key=_ledger_sort_key)
    if verdict_path is not None and Path(verdict_path).exists():
        pinned = json.loads(Path(verdict_path).read_text(encoding="utf-8"))
        pinned_keys = pinned.get("sample_keys")
        if not isinstance(pinned_keys, list) or not pinned_keys:
            raise ValueError("pinned MIX verdict is missing its stopping prefix")
        current_sample = stopping_sample(rows)
        current_keys = [list(observation_key(row)) for row in current_sample]
        if current_keys != pinned_keys:
            raise ValueError("MIX observation would change the pinned stopping prefix")
        if pinned.get("last_key") != current_keys[-1]:
            raise ValueError("pinned MIX verdict has an invalid last key")
        if pinned.get("sample_sha256") != _jsonl_sha256(current_sample):
            raise ValueError("MIX observation would change the pinned sample SHA")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
    os.replace(temporary, destination)
    return rows


def pin_verdict(rows: list[dict[str, Any]], path: str | Path, freeze: Mapping[str, Any],
                ledger_path: str | Path | None = None) -> dict[str, Any]:
    destination = Path(path)
    sample = stopping_sample(rows)
    sample_keys = [list(observation_key(row)) for row in sample]
    if destination.exists():
        pinned = json.loads(destination.read_text(encoding="utf-8"))
        if pinned.get("strategy_id") != STRATEGY_ID or pinned.get("protocol_sha256") != freeze["protocol_sha256"]:
            raise ValueError("pinned MIX verdict belongs to another protocol")
        if (not sample_keys or pinned.get("sample_keys") != sample_keys or
                pinned.get("last_key") != sample_keys[-1]):
            raise ValueError("current MIX ledger changes the pinned stopping prefix")
        if pinned.get("sample_sha256") != _jsonl_sha256(sample):
            raise ValueError("current MIX ledger changes the pinned sample SHA")
        return pinned
    result = verdict(rows)
    if result["status"] == "collecting":
        return result
    ordered_rows = sorted(rows, key=_ledger_sort_key)
    if ledger_path is not None:
        ledger_path = Path(ledger_path)
        if _read_jsonl(ledger_path) != ordered_rows:
            raise ValueError("MIX verdict rows do not match the persisted ledger")
        ledger_sha256 = sha256_file(ledger_path)
    else:
        ledger_sha256 = _jsonl_sha256(ordered_rows)
    record = {**result, "strategy_id": STRATEGY_ID, "protocol_sha256": freeze["protocol_sha256"],
              "sample_keys": sample_keys,
              "sample_sha256": _jsonl_sha256(sample),
              "ledger_sha256": ledger_sha256, "last_key": sample_keys[-1],
              "pinned_at": datetime.now(timezone.utc).isoformat()}
    destination.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json(destination, record)
    return record


def summarize(rows: list[dict[str, Any]], counters: Counter) -> dict[str, Any]:
    result: dict[str, Any] = {"counters": dict(counters), "latencies": {}}
    for latency in sorted({float(row["evaluation_ms"]) for row in rows}):
        sample = [row for row in rows if float(row["evaluation_ms"]) == latency]
        fills = [row for row in sample if row["filled"] and row["winner"] is not None]
        shares = sum(float(row["filled_shares"]) for row in fills)
        pnl = sum(float(row["pnl"]) for row in fills)
        mean = pnl / shares if shares else None
        result["latencies"][str(int(latency))] = {
            "signals": len(sample),
            "fills": len(fills),
            "fill_rate": len(fills) / len(sample) if sample else 0.0,
            "net_ev_per_share": mean,
            "pnl": pnl,
            "days": len({row["day"] for row in fills}),
            "day_cluster_lower_99": daily_lower_99(fills),
            "exact_p": exact_pvalue(fills) if mean is not None and mean > 0 else 1.0,
            "reasons": dict(Counter(str(row["reason"]) for row in sample)),
        }
    return result


def replay_archive(archive_dir: str | Path, config: ControlConfig | None = None,
                   rows_out: str | Path | None = None) -> dict[str, Any]:
    archive_dir = Path(archive_dir)
    standard = archive_dir / "strict" if (archive_dir / "strict" / "manifest.json").exists() else archive_dir
    checked = archive.validate_standard_artifact(standard)
    mappings = list(archive.iter_market_mappings(standard / "market_registry.csv.gz"))
    outcomes = {row["market_id"]: row["winner"] for row in archive.iter_outcomes(standard / "market_outcomes.csv.gz")}
    source = iter_aggregate_spot_events(archive_dir)
    clob = archive.iter_normalized_events(standard / "clob_events.jsonl.gz", family="clob")
    rows, counters = replay_normalized(source, clob, mappings, outcomes, config)
    if rows_out is not None:
        destination = Path(rows_out)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("w", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
    result = summarize(rows, counters)
    result["verdict"] = verdict(rows)
    result["dataset"] = {"archive": archive_dir.name, "manifest": checked["manifest"],
                         "paper_gate_eligible": checked["manifest"].get("collector_region") == "eu-west-1",
                         "signal_tape": "aggregate_trade_receipt_clock"}
    result["protocol"] = {"rule": "jump_z_settle_control", "z_cut": (config or ControlConfig()).z_cut,
                          "primary_latency_ms": PRIMARY_LATENCY_MS,
                          "book_clock": "local_receipt", "depth": "single_best_ask_at_least_five",
                          "outcome_use": "after_fill_only", "orders": "paper_only",
                          "limitation": "standard artifact has no explicit lifecycle/halt field"}
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", required=True)
    parser.add_argument("--rows-out")
    parser.add_argument("--freeze")
    parser.add_argument("--append")
    parser.add_argument("--verdict-out")
    args = parser.parse_args()
    if (args.append or args.verdict_out) and not args.freeze:
        parser.error("--freeze is required with --append or --verdict-out")
    if args.verdict_out and not args.append:
        parser.error("--verdict-out requires --append")
    if args.append and not args.rows_out:
        parser.error("--append requires --rows-out")
    freeze, config = load_control_freeze(args.freeze) if args.freeze else (None, ControlConfig())
    rows_out = args.rows_out
    result = replay_archive(args.archive, config=config, rows_out=rows_out)
    if args.append:
        if result["dataset"]["paper_gate_eligible"]:
            pooled = append_observations(args.append, rows_out, args.verdict_out)
            result["pooled_verdict"] = (pin_verdict(pooled, args.verdict_out, freeze, args.append)
                                         if args.verdict_out else verdict(pooled))
        else:
            result["pooled_verdict"] = {"status": "audit_only", "reason": "collector is not eu-west-1"}
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
