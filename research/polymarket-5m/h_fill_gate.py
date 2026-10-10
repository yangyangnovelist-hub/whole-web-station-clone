"""Paper-only send-veto seam for the frozen current-H replay.

The wrapper deliberately does not learn a direction, fair value, limit or order size.  It asks a
read-only policy only when canonical H is otherwise able to send, then either delegates the exact
unchanged signal or consumes none of that signal's order/rate/pending state.  Clocks and older due
evaluations still advance exactly as in H.  The frozen H implementation remains byte-for-byte
untouched and is always the ungated control.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any, Callable, Optional

import h_replay as replay


@dataclass(frozen=True)
class GateDecision:
    market_id: str
    signal_receive_ms: float
    signal_source_ms: Optional[float]
    signal_ms: float
    signal_source: Optional[str]
    side: str
    fair: float
    decision_ask: float
    fixed_limit: float
    net_edge: float
    target_shares: float
    send_ms: float
    eligible_evaluation_ms: tuple[float, ...]


@dataclass(frozen=True)
class GateVerdict:
    allow: bool
    score: float = 1.0
    reason: str = "policy"

    def __post_init__(self) -> None:
        if not isinstance(self.allow, bool):
            raise TypeError("gate allow flag must be bool")
        if not math.isfinite(self.score):
            raise ValueError("gate score must be finite")
        if not isinstance(self.reason, str) or not self.reason:
            raise ValueError("gate reason must be a non-empty string")

    @classmethod
    def pass_order(cls, *, score: float = 1.0, reason: str = "allow") -> "GateVerdict":
        return cls(True, score, reason)


@dataclass(frozen=True)
class GateAudit:
    decision: GateDecision
    verdict: GateVerdict


GatePolicy = Callable[[GateDecision], GateVerdict]


class GatePolicyError(RuntimeError):
    """Permanent fail-closed error raised after a policy contract violation."""


def single_horizon_config(config: replay.ReplayConfig, evaluation_ms: float) -> replay.ReplayConfig:
    """Derive one independent H branch without changing any execution parameter."""
    horizon = float(evaluation_ms)
    if horizon not in tuple(float(value) for value in config.evaluation_ms):
        raise ValueError("gate horizon must already exist in the frozen H configuration")
    return replace(config, evaluation_ms=(horizon,))


class HFillGateReplay:
    """Wrap :class:`h_replay.HReplay` without changing its canonical execution code."""

    def __init__(self, config: replay.ReplayConfig, policy: GatePolicy) -> None:
        if not callable(policy):
            raise TypeError("gate policy must be callable")
        if not config.retry_after_no_send_or_kill or config.reentry_enabled:
            raise ValueError(
                "current-H veto requires retry-after-kill and no post-fill re-entry"
            )
        if len(config.evaluation_ms) != 1:
            raise ValueError("current-H veto requires one independent evaluation branch")
        self.config = config
        self.policy = policy
        self.machine = replay.HReplay(config)
        self.gate_decisions: list[GateAudit] = []
        self._failed = False

    @property
    def records(self) -> list[dict[str, Any]]:
        return self.machine.records

    def _signal_ms(self, event: dict[str, Any]) -> float:
        receive_ms = float(event["receive_ms"])
        source_ms = event.get("source_ms")
        if self.config.signal_time_basis == "exchange_source_timestamp" and source_ms is not None:
            return float(source_ms)
        if self.config.signal_time_basis == "local_receipt_timestamp":
            return receive_ms
        return receive_ms - self.config.book_lag_ms

    def _eligible_delays(
        self,
        market: Any,
        *,
        fixed_limit: float,
        send_ms: float,
    ) -> tuple[float, ...]:
        signed_size, _ = replay._pilot_plan(fixed_limit, self.config.base_shares, self.config)
        if signed_size is None:
            return ()
        eligible = []
        cutoff = send_ms - 60_000.0
        for evaluation_ms, branch in market.branches.items():
            if branch.pending:
                continue
            if branch.first_fill_direction is None:
                if branch.first_locked and not self.config.retry_after_no_send_or_kill:
                    continue
            elif not self.config.reentry_enabled:
                continue
            elif branch.last_fill_signal_ms is not None:
                # A re-entry-capable strategy can require direction-dependent sizing.  Current-H
                # has re-entry disabled; delegate such states rather than approximating them.
                continue
            sent_times = self.machine._sent_by_evaluation[float(evaluation_ms)]
            active = sum(timestamp > cutoff for timestamp in sent_times)
            if self.config.max_orders_per_min > 0 and active >= self.config.max_orders_per_min:
                continue
            eligible.append(float(evaluation_ms))
        return tuple(sorted(eligible))

    def _decision(self, event: dict[str, Any]) -> GateDecision | None:
        market_id = str(event["market_id"])
        market = self.machine._market(market_id)
        side = "Up" if event["direction"] in ("Up", "up", 1, True) else "Down"
        fair = float(event["fair"])
        fixed_limit = replay.limit_price(
            fair,
            self.config.theta,
            self.config.tick,
            self.config.fee_rate,
        )
        asks = market.book.asks(side) if market.book.ready else ()
        if fixed_limit is None or not asks:
            return None
        decision_ask = float(asks[0][0])
        if not math.isfinite(decision_ask) or not decision_ask <= fixed_limit + 1e-9:
            return None
        send_ms = float(event["receive_ms"]) + self.config.local_path_ms
        eligible = self._eligible_delays(market, fixed_limit=fixed_limit, send_ms=send_ms)
        if not eligible:
            return None
        signal_ms = self._signal_ms(event)
        return GateDecision(
            market_id=market_id,
            signal_receive_ms=float(event["receive_ms"]),
            signal_source_ms=(float(event["source_ms"]) if event.get("source_ms") is not None else None),
            signal_ms=signal_ms,
            signal_source=(str(event["signal_source"]) if event.get("signal_source") is not None else None),
            side=side,
            fair=fair,
            decision_ask=decision_ask,
            fixed_limit=float(fixed_limit),
            net_edge=fair - decision_ask - replay.taker_fee(decision_ask, self.config.fee_rate),
            target_shares=self.config.base_shares,
            send_ms=send_ms,
            eligible_evaluation_ms=eligible,
        )

    def feed(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        if self._failed:
            raise GatePolicyError("gate replay is permanently failed")
        if str(event.get("kind")) != "signal":
            return self.machine.feed(event)
        before = len(self.machine.records)
        receive_ms = float(event["receive_ms"])
        if receive_ms < self.machine._receive_watermark:
            # Delegate the canonical exception text and behavior.
            return self.machine.feed(event)
        # HReplay drains strictly older horizons before evaluating a newly received signal.  The
        # gate must observe that post-drain branch/rate state; calling _decision first would miss
        # vetoing the first signal after a kill/fill.  A later delegate repeats this no-op preamble.
        self.machine._drain(receive_ms, inclusive=False)
        self.machine._receive_watermark = receive_ms
        decision = self._decision(event)
        if decision is None:
            self.machine.feed(event)
            return self.machine.records[before:]
        try:
            verdict = self.policy(decision)
            if not isinstance(verdict, GateVerdict):
                raise TypeError("gate policy must return GateVerdict")
        except Exception as error:
            self._failed = True
            raise GatePolicyError("gate policy failed; replay cannot continue") from error
        self.gate_decisions.append(GateAudit(decision, verdict))
        if verdict.allow:
            self.machine.feed(event)
        elif self.config.signal_time_basis != "local_receipt_timestamp":
            # Canonical source-clock feed drains horizons due exactly at this receipt after every
            # signal.  A veto skips _signal only, not that post-event clock transition.
            self.machine._drain(receive_ms, inclusive=True)
        return self.machine.records[before:]

    def finish(self, *, force: bool = True) -> list[dict[str, Any]]:
        if self._failed:
            raise GatePolicyError("gate replay is permanently failed")
        return self.machine.finish(force=force)

    def pending_evaluations(self) -> list[dict[str, Any]]:
        return self.machine.pending_evaluations()

    def has_pending_evaluations(self) -> bool:
        return self.machine.has_pending_evaluations()
