"""Receipt-causal Binance-perpetual hedge for Polymarket maker residuals.

The digital-delta structure follows the MIT-licensed YISOWAK/polybot-market-maker
implementation.  This version uses the project's 60-second Asian-TWAP binary model
instead of a European expiry and never assumes fills without a received futures BBO.
"""
from __future__ import annotations

import argparse
import bisect
import collections
import heapq
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scipy.stats import norm

import binary

NS_PER_MS = 1_000_000
NS_PER_S = 1_000_000_000
EPS = 1e-15


@dataclass(frozen=True)
class SpotState:
    recv_ns: int
    price: float
    sigma_s: float


@dataclass(frozen=True)
class FuturesBBO:
    recv_ns: int
    exchange_ns: int
    bid: float
    ask: float
    bid_depth: float = math.nan
    ask_depth: float = math.nan


@dataclass(frozen=True)
class ExternalHedgeConfig:
    latency_ms: float = 200.0
    fee_rate: float = 0.0005
    max_bbo_wait_ms: float = 250.0
    max_bbo_source_age_ms: float = 250.0
    future_clock_tolerance_ms: float = 5.0
    spot_max_age_ms: float = 1_000.0

    def __post_init__(self) -> None:
        for name in (
            "latency_ms",
            "max_bbo_wait_ms",
            "max_bbo_source_age_ms",
            "future_clock_tolerance_ms",
            "spot_max_age_ms",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(f"{name} must be finite and non-negative")
        if not math.isfinite(self.fee_rate) or not 0.0 <= self.fee_rate <= 1.0:
            raise ValueError("fee_rate must be between zero and one")


class SourceTape:
    """Immutable receipt-ordered source state used by the hedge simulator."""

    def __init__(
        self,
        *,
        spot_states: Sequence[SpotState],
        futures_bbos: Sequence[FuturesBBO],
    ) -> None:
        self.spot_states = tuple(sorted(spot_states, key=lambda item: item.recv_ns))
        self.futures_bbos = tuple(sorted(futures_bbos, key=lambda item: item.recv_ns))
        self._spot_times = tuple(item.recv_ns for item in self.spot_states)
        self._bbo_times = tuple(item.recv_ns for item in self.futures_bbos)

    def spot_before(self, recv_ns: int) -> SpotState | None:
        index = bisect.bisect_left(self._spot_times, int(recv_ns)) - 1
        return self.spot_states[index] if index >= 0 else None

    def execution_bbo(
        self,
        due_ns: int,
        *,
        max_wait_ns: int,
        max_source_age_ns: int,
        future_clock_tolerance_ns: int,
    ) -> tuple[FuturesBBO | None, str | None]:
        deadline = int(due_ns) + int(max_wait_ns)
        index = bisect.bisect_left(self._bbo_times, int(due_ns))
        while index < len(self.futures_bbos):
            quote = self.futures_bbos[index]
            if quote.recv_ns > deadline:
                break
            if (
                math.isfinite(quote.bid)
                and math.isfinite(quote.ask)
                and quote.bid > 0.0
                and quote.ask >= quote.bid
                and math.isfinite(quote.bid_depth)
                and math.isfinite(quote.ask_depth)
                and quote.bid_depth > 0.0
                and quote.ask_depth > 0.0
                and quote.exchange_ns <= quote.recv_ns + future_clock_tolerance_ns
                and quote.recv_ns - quote.exchange_ns <= max_source_age_ns
            ):
                return quote, None
            index += 1
        return None, "no_fresh_bbo_before_deadline"


def build_source_tape(
    events: Iterable[Any],
    *,
    sigma_window_s: int = 600,
    sigma_min_observations: int = 300,
    forward_fill_s: int = 10,
) -> SourceTape:
    """Build a bounded execution tape without reordering exchange clocks."""
    from h_replay_run import SigmaGrid

    grid = SigmaGrid(sigma_window_s, sigma_min_observations, forward_fill_s)
    spots: list[SpotState] = []
    bbos: list[FuturesBBO] = []
    previous_recv_ns = -1
    for event in events:
        recv_ns = int(event.recv_ns)
        if recv_ns < previous_recv_ns:
            raise ValueError("source receipt clock regressed")
        previous_recv_ns = recv_ns
        if (
            event.source == "bn_spot"
            and event.channel == "bn_spot"
            and event.kind == "trade"
            and math.isfinite(event.price)
            and event.price > 0.0
        ):
            source_ns = event.exchange_ns if event.exchange_ns is not None else recv_ns
            grid.update(source_ns / NS_PER_S, math.log(event.price))
            spots.append(SpotState(recv_ns, float(event.price), float(grid.sigma())))
        elif (
            event.source == "bn_fut_pub"
            and event.channel == "bn_perp"
            and event.kind == "book"
            and event.book_kind == "bbo"
            and event.exchange_ns is not None
            and math.isfinite(event.bid)
            and math.isfinite(event.ask)
            and event.bid > 0.0
            and event.ask >= event.bid
            and math.isfinite(event.bid_depth)
            and math.isfinite(event.ask_depth)
            and event.bid_depth > 0.0
            and event.ask_depth > 0.0
        ):
            bbos.append(FuturesBBO(
                recv_ns=recv_ns,
                exchange_ns=int(event.exchange_ns),
                bid=float(event.bid),
                ask=float(event.ask),
                bid_depth=float(event.bid_depth),
                ask_depth=float(event.ask_depth),
            ))
    return SourceTape(spot_states=spots, futures_bbos=bbos)


def _finite_positive(value: float, name: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"{name} must be finite and positive")
    return number


def asian_binary_delta_btc(
    up_probability: float,
    spot_price: float,
    sigma_s: float,
    elapsed_s: float,
    side: str,
) -> float:
    if side not in {"Up", "Down"}:
        raise ValueError("side must be Up or Down")
    if not math.isfinite(up_probability) or not 0.0 < up_probability < 1.0:
        raise ValueError("up_probability must be strictly between zero and one")
    spot_price = _finite_positive(spot_price, "spot_price")
    sigma_s = _finite_positive(sigma_s, "sigma_s")
    if not math.isfinite(elapsed_s):
        raise ValueError("elapsed_s must be finite")
    remaining_fraction = float(binary.future_samples(elapsed_s)) / binary.TWAP_S
    scale = sigma_s * float(binary.twap_std_factor(elapsed_s))
    if remaining_fraction <= 0.0 or scale <= 0.0:
        return 0.0
    magnitude = (
        norm.pdf(norm.ppf(up_probability))
        * remaining_fraction
        / (spot_price * scale)
    )
    return magnitude if side == "Up" else -magnitude


def perpetual_cashflow_pnl(
    fills: list[tuple[float, float]],
    fee_rate: float,
) -> float:
    """Cash P&L of signed perpetual fills, including taker fees.

    Positive quantity is a buy and therefore a cash outflow; negative quantity
    is a sell and therefore a cash inflow.  This deliberately does not mark an
    open position to market: callers must close it with an observed BBO.
    """
    if not math.isfinite(fee_rate) or not 0.0 <= fee_rate <= 1.0:
        raise ValueError("fee_rate must be between zero and one")
    cashflow = 0.0
    fees = 0.0
    for quantity, raw_price in fills:
        quantity = float(quantity)
        price = _finite_positive(raw_price, "fill price")
        if not math.isfinite(quantity):
            raise ValueError("fill quantity must be finite")
        cashflow -= quantity * price
        fees += abs(quantity) * price * fee_rate
    return cashflow - fees


@dataclass(frozen=True)
class _TargetOrder:
    due_ns: int
    target_btc: float
    cause: str
    sequence: int


@dataclass(frozen=True)
class _ArmedFill:
    send_ns: int
    execution_ns: int
    quantity_btc: float
    target_btc: float
    cause: str
    sequence: int
    quote: FuturesBBO | None
    failure_reason: str | None = None


def simulate_external_delta_hedge(
    maker_row: Mapping[str, Any],
    tape: SourceTape,
    config: ExternalHedgeConfig | None = None,
) -> dict[str, Any]:
    """Replay a delayed perpetual delta hedge for one maker cycle.

    A maker fill can replace a hedge only before that hedge's send clock.  At an
    exact timestamp tie the hedge is sent (and, if a BBO is available, executed)
    before the new Polymarket fill is observed.  Orders already sent retain their
    fixed quantity; later fills schedule a correcting target.
    """
    cfg = config or ExternalHedgeConfig()
    latency_ns = round(cfg.latency_ms * NS_PER_MS)
    max_wait_ns = round(cfg.max_bbo_wait_ms * NS_PER_MS)
    max_source_age_ns = round(cfg.max_bbo_source_age_ms * NS_PER_MS)
    future_clock_tolerance_ns = round(cfg.future_clock_tolerance_ms * NS_PER_MS)
    spot_max_age_ns = round(cfg.spot_max_age_ms * NS_PER_MS)
    slot = int(maker_row["slot"])
    market_end_ns = (slot + binary.WINDOW_S) * NS_PER_S
    raw_fills = [
        {"_input_sequence": index, **dict(item)}
        for index, item in enumerate(maker_row.get("maker_fill_events", ()))
    ]
    raw_fills.sort(key=lambda item: (
        int(item["recv_ns"]),
        int(item.get("sequence", item["_input_sequence"])),
    ))
    for item in raw_fills:
        if item.get("observation_basis") != "public_clob_trade_queue_inference":
            raise ValueError("maker fill must be labelled as public queue inference")
        if item.get("side") not in {"Up", "Down"}:
            raise ValueError("maker fill side must be Up or Down")
        _finite_positive(float(item["price"]), "maker fill price")
        _finite_positive(float(item["shares"]), "maker fill shares")

    failures: collections.Counter[str] = collections.Counter()
    futures_fills: list[dict[str, Any]] = []
    armed: list[tuple[int, int, _ArmedFill]] = []
    pending: _TargetOrder | None = None
    position_btc = 0.0
    projected_btc = 0.0
    up_shares = 0.0
    down_shares = 0.0
    up_inventory_probability: float | None = None
    down_inventory_probability: float | None = None
    peak_abs_btc = 0.0
    peak_notional = 0.0
    cancelled_before_send = 0
    sequence = 0
    fill_index = 0
    close_done = False
    execution_invalid = False

    def next_sequence() -> int:
        nonlocal sequence
        sequence += 1
        return sequence

    def arm(order: _TargetOrder) -> None:
        nonlocal projected_btc
        quantity = order.target_btc - projected_btc
        if abs(quantity) <= EPS:
            projected_btc = order.target_btc
            return
        quote, reason = tape.execution_bbo(
            order.due_ns,
            max_wait_ns=max_wait_ns,
            max_source_age_ns=max_source_age_ns,
            future_clock_tolerance_ns=future_clock_tolerance_ns,
        )
        if quote is None:
            execution_ns = order.due_ns + max_wait_ns
            failure_reason = str(reason)
        else:
            execution_ns = quote.recv_ns
            available_depth = quote.ask_depth if quantity > 0.0 else quote.bid_depth
            failure_reason = (
                "insufficient_top_depth"
                if abs(quantity) > available_depth + EPS
                else None
            )
        armed_fill = _ArmedFill(
            send_ns=order.due_ns,
            execution_ns=execution_ns,
            quantity_btc=quantity,
            target_btc=order.target_btc,
            cause=order.cause,
            sequence=order.sequence,
            quote=quote,
            failure_reason=failure_reason,
        )
        heapq.heappush(armed, (armed_fill.execution_ns, armed_fill.sequence, armed_fill))
        projected_btc += quantity

    def execute(armed_fill: _ArmedFill) -> None:
        nonlocal cancelled_before_send, execution_invalid, pending
        nonlocal position_btc, projected_btc, peak_abs_btc, peak_notional
        quantity = armed_fill.quantity_btc
        if armed_fill.failure_reason is not None:
            projected_btc -= quantity
            failures[f"{armed_fill.cause}:{armed_fill.failure_reason}"] += 1
            execution_invalid = True
            if pending is not None and armed_fill.execution_ns <= pending.due_ns:
                pending = None
                cancelled_before_send += 1
            return
        assert armed_fill.quote is not None
        price = armed_fill.quote.ask if quantity > 0.0 else armed_fill.quote.bid
        fee = abs(quantity) * price * cfg.fee_rate
        position_btc += quantity
        peak_abs_btc = max(peak_abs_btc, abs(position_btc))
        peak_notional = max(peak_notional, abs(position_btc) * price)
        futures_fills.append({
            "send_ns": armed_fill.send_ns,
            "execution_ns": armed_fill.execution_ns,
            "quantity_btc": quantity,
            "price": price,
            "fee": fee,
            "cause": armed_fill.cause,
            "target_btc": armed_fill.target_btc,
            "bid": armed_fill.quote.bid,
            "ask": armed_fill.quote.ask,
        })

    close_due_ns = market_end_ns + latency_ns
    while fill_index < len(raw_fills) or pending is not None or armed or not close_done:
        next_fill_ns = (
            int(raw_fills[fill_index]["recv_ns"])
            if fill_index < len(raw_fills)
            else 10**30
        )
        next_pending_ns = pending.due_ns if pending is not None else 10**30
        next_execution_ns = armed[0][0] if armed else 10**30
        next_close_ns = close_due_ns if not close_done else 10**30
        event_ns = min(next_pending_ns, next_execution_ns, next_fill_ns, next_close_ns)

        # Existing in-flight results win ties, then new sends, then maker fills.
        if next_execution_ns == event_ns:
            _stamp, _seq, armed_fill = heapq.heappop(armed)
            execute(armed_fill)
            continue
        if next_pending_ns == event_ns:
            order = pending
            pending = None
            assert order is not None
            arm(order)
            continue
        if next_fill_ns == event_ns:
            item = raw_fills[fill_index]
            fill_index += 1
            if pending is not None and event_ns < pending.due_ns:
                pending = None
                cancelled_before_send += 1
            side = str(item["side"])
            shares = float(item["shares"])
            if side == "Up":
                up_shares += shares
                up_inventory_probability = float(item["price"])
            else:
                down_shares += shares
                down_inventory_probability = 1.0 - float(item["price"])
            if execution_invalid:
                continue
            imbalance = up_shares - down_shares
            if abs(imbalance) <= EPS:
                target = 0.0
            else:
                spot = tape.spot_before(event_ns)
                if spot is None or event_ns - spot.recv_ns > spot_max_age_ns:
                    failures["maker_fill:no_fresh_spot_state"] += 1
                    execution_invalid = True
                    continue
                up_probability = (
                    up_inventory_probability if imbalance > 0.0 else down_inventory_probability
                )
                if up_probability is None:
                    failures["maker_fill:missing_inventory_probability"] += 1
                    execution_invalid = True
                    continue
                elapsed_s = event_ns / NS_PER_S - slot
                try:
                    unit_delta = asian_binary_delta_btc(
                        up_probability,
                        spot.price,
                        spot.sigma_s,
                        elapsed_s,
                        "Up",
                    )
                except ValueError:
                    failures["maker_fill:invalid_delta_inputs"] += 1
                    execution_invalid = True
                    continue
                target = -imbalance * unit_delta
            pending = _TargetOrder(
                due_ns=event_ns + latency_ns,
                target_btc=target,
                cause="maker_fill",
                sequence=next_sequence(),
            )
            continue

        close_done = True
        if pending is not None and event_ns < pending.due_ns:
            pending = None
            cancelled_before_send += 1
        arm(_TargetOrder(event_ns, 0.0, "market_end_close", next_sequence()))

    signed_fills = [
        (float(item["quantity_btc"]), float(item["price"]))
        for item in futures_fills
    ]
    futures_pnl = perpetual_cashflow_pnl(signed_fills, cfg.fee_rate)
    flat = abs(position_btc) <= EPS
    poly_pnl = maker_row.get("pnl")
    combined_pnl = (
        float(poly_pnl) + futures_pnl
        if flat and not execution_invalid and poly_pnl is not None
        else None
    )
    return {
        "market_id": maker_row.get("market_id"),
        "slot": slot,
        "poly_pnl": poly_pnl,
        "futures_pnl": futures_pnl if flat else None,
        "combined_pnl": combined_pnl,
        "futures_fills": futures_fills,
        "final_position_btc": position_btc,
        "projected_position_btc": projected_btc,
        "execution_valid": not execution_invalid,
        "peak_abs_btc": peak_abs_btc,
        "peak_notional_usdt": peak_notional,
        "cancelled_before_send": cancelled_before_send,
        "failures": dict(sorted(failures.items())),
    }


def summarize_hedge_rows(
    maker_rows: Sequence[Mapping[str, Any]],
    hedge_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if len(maker_rows) != len(hedge_rows):
        raise ValueError("maker and hedge rows must be matched")
    resolved = [
        (maker, hedge)
        for maker, hedge in zip(maker_rows, hedge_rows, strict=True)
        if maker.get("pnl") is not None
    ]
    filled_shares = sum(
        float(maker.get("maker_up_filled") or 0.0)
        + float(maker.get("maker_down_filled") or 0.0)
        for maker, _hedge in resolved
    )
    poly_pnl = sum(float(maker["pnl"]) for maker, _hedge in resolved)
    closed_combined = [
        float(hedge["combined_pnl"])
        for _maker, hedge in resolved
        if hedge.get("combined_pnl") is not None
    ]
    all_closed = all(
        abs(float(hedge.get("final_position_btc") or 0.0)) <= EPS
        and hedge.get("combined_pnl") is not None
        for _maker, hedge in resolved
    )
    failures: collections.Counter[str] = collections.Counter()
    for _maker, hedge in resolved:
        failures.update({
            str(reason): int(count)
            for reason, count in dict(hedge.get("failures") or {}).items()
        })
    futures_fees = sum(
        float(fill.get("fee") or 0.0)
        for _maker, hedge in resolved
        for fill in hedge.get("futures_fills", ())
    )
    combined = sum(closed_combined) if all_closed else None
    return {
        "cycles": len(maker_rows),
        "resolved_cycles": len(resolved),
        "maker_filled_cycles": sum(
            float(maker.get("maker_up_filled") or 0.0)
            + float(maker.get("maker_down_filled") or 0.0) > EPS
            for maker, _hedge in resolved
        ),
        "maker_filled_shares": filled_shares,
        "poly_pnl": poly_pnl,
        "poly_pnl_per_filled_share": poly_pnl / filled_shares if filled_shares else None,
        "futures_fill_count": sum(
            len(hedge.get("futures_fills", ())) for _maker, hedge in resolved
        ),
        "futures_fees": futures_fees,
        "futures_pnl_closed_only": sum(
            float(hedge["futures_pnl"])
            for _maker, hedge in resolved
            if hedge.get("futures_pnl") is not None
        ),
        "all_futures_positions_closed": all_closed,
        "open_futures_cycles": sum(
            abs(float(hedge.get("final_position_btc") or 0.0)) > EPS
            for _maker, hedge in resolved
        ),
        "combined_pnl": combined,
        "combined_pnl_closed_only": sum(closed_combined),
        "combined_pnl_per_filled_share": (
            combined / filled_shares if combined is not None and filled_shares else None
        ),
        "peak_abs_btc": max(
            (float(hedge.get("peak_abs_btc") or 0.0) for _maker, hedge in resolved),
            default=0.0,
        ),
        "peak_notional_usdt": max(
            (float(hedge.get("peak_notional_usdt") or 0.0) for _maker, hedge in resolved),
            default=0.0,
        ),
        "cancelled_before_send": sum(
            int(hedge.get("cancelled_before_send") or 0) for _maker, hedge in resolved
        ),
        "failures": dict(sorted(failures.items())),
    }


def development_arms() -> dict[str, ExternalHedgeConfig]:
    return {
        "latency100_fee5bps": ExternalHedgeConfig(latency_ms=100.0, fee_rate=0.0005),
        "latency150_fee5bps": ExternalHedgeConfig(latency_ms=150.0, fee_rate=0.0005),
        "latency200_fee5bps": ExternalHedgeConfig(latency_ms=200.0, fee_rate=0.0005),
        "latency200_fee4bps": ExternalHedgeConfig(latency_ms=200.0, fee_rate=0.0004),
    }


def run_external_hedge_development(
    *,
    clob_paths: Sequence[str | Path],
    source_paths: Sequence[str | Path],
    segment_start_ms: float,
    segment_end_ms: float,
    fetch_outcomes: bool = False,
) -> dict[str, Any]:
    """Run Family 27 over one bounded raw-recording segment."""
    import jump_causal
    import paired_maker

    maker_result = paired_maker.run_raw_development(
        clob_paths=clob_paths,
        source_paths=source_paths,
        segment_start_ms=segment_start_ms,
        segment_end_ms=segment_end_ms,
        fetch_outcomes=fetch_outcomes,
        variant_names=["place30_cancel30"],
    )
    maker_variant = maker_result["variants"]["place30_cancel30"]
    maker_rows = maker_variant["rows"]
    arms = development_arms()
    maximum_tail_ms = max(
        config.latency_ms + config.max_bbo_wait_ms for config in arms.values()
    )
    source_events = jump_causal.iter_raw_events_from_files(
        source_paths,
        recv_start_ns=round((segment_start_ms - 620_000.0) * NS_PER_MS),
        recv_end_ns=round((segment_end_ms + maximum_tail_ms + 1_000.0) * NS_PER_MS),
    )
    tape = build_source_tape(source_events)
    variants: dict[str, Any] = {}
    for name, config in arms.items():
        rows = [simulate_external_delta_hedge(row, tape, config) for row in maker_rows]
        variants[name] = {
            "config": {
                "latency_ms": config.latency_ms,
                "fee_rate": config.fee_rate,
                "max_bbo_wait_ms": config.max_bbo_wait_ms,
                "max_bbo_source_age_ms": config.max_bbo_source_age_ms,
                "future_clock_tolerance_ms": config.future_clock_tolerance_ms,
                "spot_max_age_ms": config.spot_max_age_ms,
            },
            "summary": summarize_hedge_rows(maker_rows, rows),
            "rows": rows,
        }
    return {
        "schema": "external-delta-hedge-development-v1",
        "maker_fill_observation_basis": "public_clob_trade_queue_inference",
        "segment_start_ms": segment_start_ms,
        "segment_end_ms": segment_end_ms,
        "maker_warning_count": maker_result["warning_count"],
        "maker_warning_sources": maker_result["warning_sources"],
        "maker_source_health": maker_result["source_health"],
        "official_outcomes": maker_result["official_outcomes"],
        "maker_summary": maker_variant["summary"],
        "maker_rows": maker_rows,
        "source_tape": {
            "spot_states": len(tape.spot_states),
            "futures_bbos": len(tape.futures_bbos),
        },
        "variants": variants,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clob", action="append", required=True)
    parser.add_argument("--source", action="append", required=True)
    parser.add_argument("--segment-start-ms", type=float, required=True)
    parser.add_argument("--segment-end-ms", type=float, required=True)
    parser.add_argument("--fetch-outcomes", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result = run_external_hedge_development(
        clob_paths=args.clob,
        source_paths=args.source,
        segment_start_ms=args.segment_start_ms,
        segment_end_ms=args.segment_end_ms,
        fetch_outcomes=args.fetch_outcomes,
    )
    encoded = json.dumps(result, indent=2, sort_keys=True, allow_nan=False)
    if args.output is None:
        print(encoded)
    else:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
