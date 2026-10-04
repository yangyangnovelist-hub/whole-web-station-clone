"""Audit exchange-timestamped price sources as current-H receipt-race inputs.

This is a source-quality audit, not a fill backtest and never an activation gate. Every source uses
the frozen H one-second return and the Binance-spot trailing sigma. Raw frames remain in local
receipt order; exchange timestamps define source-local returns and one-to-one move episodes.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from h_replay_run import SignalConfig, TimestampedFirstTrigger
from jump_causal import MS, NS, MarketEvent, iter_raw_events_from_files


ACTIVE_START_S = 60
ACTIVE_END_S = 285


@dataclass(frozen=True)
class ReceiptCandidate:
    source: str
    recv_ns: int
    source_ns: int
    slot: int
    direction: int
    z_score: float


@dataclass(frozen=True)
class CandidatePair:
    external: ReceiptCandidate
    spot: ReceiptCandidate

    @property
    def signed_lead_ms(self) -> float:
        return (self.spot.recv_ns - self.external.recv_ns) / MS

    @property
    def source_delta_ms(self) -> float:
        return (self.spot.source_ns - self.external.source_ns) / MS


def _source_label(event: MarketEvent) -> str | None:
    if event.source == "bn_spot" and event.kind == "trade":
        return "spot_trade"
    if event.source == "bn_fut_trade" and event.kind == "trade":
        return "futures_trade"
    if event.source == "bn_fut_pub" and event.channel == "bn_perp" and event.kind == "book":
        return "futures_book_ticker"
    if event.source == "deribit" and event.channel == "deribit" and event.kind == "book":
        return "deribit_quote"
    return None


def _frame_identity(event: MarketEvent) -> tuple[object, ...] | None:
    if event.sequence_id is not None:
        return event.source, event.kind, event.sequence_id
    if event.kind == "book":
        return event.source, event.kind, event.exchange_ns, event.bid, event.ask
    return None


def _event_candidate(event: MarketEvent, source: str, trigger: TimestampedFirstTrigger) -> object | None:
    source_ms = int(event.exchange_ns) / MS
    if source == "spot_trade":
        return trigger.update_trade(source_ms, event.price)
    if source == "futures_trade":
        return trigger.update_source(source, source_ms, event.price)
    if source == "futures_book_ticker":
        return trigger.update_futures(source_ms, event.bid, event.ask)
    return trigger.update_source(source, source_ms, (event.bid + event.ask) / 2.0)


def extract_candidates(
    events: Iterable[MarketEvent],
    *,
    config: SignalConfig | None = None,
    episode_ms: int = 1_000,
    max_gap_ms: int | None = None,
    max_source_age_ms: int = 2_000,
    future_clock_tolerance_ms: int = 10,
    health: Counter[str] | None = None,
) -> list[ReceiptCandidate]:
    signal_config = config or SignalConfig(book_lag_ms=0.0)
    trigger = TimestampedFirstTrigger(signal_config)
    diagnostics = health if health is not None else Counter()
    seen_frames: set[tuple[object, ...]] = set()
    last_recv: dict[str, int] = {}
    last_exchange: dict[str, int] = {}
    last_episode_source: dict[tuple[str, int, int], int] = {}
    episode_ns = episode_ms * MS
    rows: list[ReceiptCandidate] = []
    for event in events:
        source = _source_label(event)
        if source is None or event.exchange_ns is None:
            continue
        identity = _frame_identity(event)
        if identity is not None and identity in seen_frames:
            diagnostics["duplicate_frame"] += 1
            continue
        if identity is not None:
            seen_frames.add(identity)
        age_ns = event.recv_ns - event.exchange_ns
        if age_ns < -future_clock_tolerance_ms * MS:
            diagnostics["future_source_clock"] += 1
            continue
        if age_ns > max_source_age_ms * MS:
            diagnostics["stale_source_clock"] += 1
            continue
        previous_exchange = last_exchange.get(source)
        if previous_exchange is not None and event.exchange_ns < previous_exchange:
            diagnostics["regressed_source_clock"] += 1
            continue
        previous_recv = last_recv.get(source)
        if max_gap_ms is not None and previous_recv is not None and event.recv_ns - previous_recv > max_gap_ms * MS:
            diagnostics[f"gap_{source}"] += 1
            if source == "spot_trade":
                trigger = TimestampedFirstTrigger(signal_config)
                last_exchange.clear()
                last_episode_source.clear()
            else:
                trigger.reset_source(source)
                last_exchange.pop(source, None)
                for key in [key for key in last_episode_source if key[0] == source]:
                    del last_episode_source[key]
        last_recv[source] = event.recv_ns
        last_exchange[source] = event.exchange_ns
        candidate = _event_candidate(event, source, trigger)
        if candidate is None:
            continue
        source_ns = int(round(candidate.timestamp_s * NS))
        key = (source, candidate.slot, candidate.direction)
        previous_episode = last_episode_source.get(key)
        if previous_episode is not None and source_ns - previous_episode < episode_ns:
            diagnostics["same_episode_suppressed"] += 1
            continue
        last_episode_source[key] = source_ns
        rows.append(ReceiptCandidate(
            source=source,
            recv_ns=event.recv_ns,
            source_ns=source_ns,
            slot=candidate.slot,
            direction=candidate.direction,
            z_score=abs(candidate.move_1s) / candidate.sigma,
        ))
    return rows


def _percentile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return float(ordered[lower] * (1.0 - weight) + ordered[upper] * weight)


def _group_by_move(rows: Sequence[ReceiptCandidate]) -> dict[tuple[int, int], list[ReceiptCandidate]]:
    groups: dict[tuple[int, int], list[ReceiptCandidate]] = {}
    for row in rows:
        groups.setdefault((row.slot, row.direction), []).append(row)
    for group in groups.values():
        group.sort(key=lambda row: row.source_ns)
    return groups


def _pair_same_move(
    external: Sequence[ReceiptCandidate],
    spot: Sequence[ReceiptCandidate],
    source_tolerance_ms: int,
) -> list[CandidatePair]:
    tolerance_ns = source_tolerance_ms * MS
    spot_groups = _group_by_move(spot)
    used: dict[tuple[int, int], set[int]] = {}
    pairs: list[CandidatePair] = []
    for row in sorted(external, key=lambda candidate: candidate.source_ns):
        key = (row.slot, row.direction)
        options = spot_groups.get(key, ())
        source_times = [candidate.source_ns for candidate in options]
        left = bisect.bisect_left(source_times, row.source_ns - tolerance_ns)
        right = bisect.bisect_right(source_times, row.source_ns + tolerance_ns)
        available = [index for index in range(left, right) if index not in used.setdefault(key, set())]
        if not available:
            continue
        index = min(
            available,
            key=lambda candidate_index: (
                abs(options[candidate_index].source_ns - row.source_ns),
                abs(options[candidate_index].recv_ns - row.recv_ns),
            ),
        )
        used[key].add(index)
        pairs.append(CandidatePair(row, options[index]))
    return pairs


def _horizon_metrics(
    external: Sequence[ReceiptCandidate],
    spot: Sequence[ReceiptCandidate],
    pairs: Sequence[CandidatePair],
    horizon_ms: int,
) -> dict[str, object]:
    leading = [pair for pair in pairs if 0.0 < pair.signed_lead_ms <= horizon_ms]
    signed_leads = [pair.signed_lead_ms for pair in pairs]
    source_deltas = [abs(pair.source_delta_ms) for pair in pairs]
    return {
        "same_move_pairs": len(pairs),
        "same_move_precision": len(pairs) / len(external) if external else 0.0,
        "leading_confirmed": len(leading),
        "leading_precision": len(leading) / len(external) if external else 0.0,
        "spot_leading_coverage": len(leading) / len(spot) if spot else 0.0,
        "signed_lead_ms_p50": _percentile(signed_leads, 0.50),
        "signed_lead_ms_p90": _percentile(signed_leads, 0.90),
        "source_delta_abs_ms_p50": _percentile(source_deltas, 0.50),
    }


def _circular_shift(rows: Sequence[ReceiptCandidate], shift_ms: int) -> list[ReceiptCandidate]:
    shift_ns = shift_ms * MS
    active_duration_ns = (ACTIVE_END_S - ACTIVE_START_S) * NS
    shifted: list[ReceiptCandidate] = []
    for row in rows:
        start_ns = (row.slot + ACTIVE_START_S) * NS
        offset_ns = row.source_ns - start_ns
        source_ns = start_ns + ((offset_ns + shift_ns) % active_duration_ns)
        delta_ns = source_ns - row.source_ns
        shifted.append(ReceiptCandidate(
            source=row.source,
            recv_ns=row.recv_ns + delta_ns,
            source_ns=source_ns,
            slot=row.slot,
            direction=row.direction,
            z_score=row.z_score,
        ))
    return shifted


def evaluate_source(
    candidates: Sequence[ReceiptCandidate],
    source: str,
    *,
    horizons_ms: Sequence[int] = (250, 500),
    source_tolerance_ms: int = 250,
    placebo_shifts_ms: Sequence[int] = (-73_000, -47_000, -31_000, -17_000,
                                                   17_000, 31_000, 47_000, 73_000),
    minimum_candidates: int = 30,
    minimum_leading_precision: float = 0.10,
    minimum_lift: float = 0.10,
) -> dict[str, object]:
    spot = [row for row in candidates if row.source == "spot_trade"]
    external = [row for row in candidates if row.source == source]
    pairs = _pair_same_move(external, spot, source_tolerance_ms)
    horizons = {
        str(horizon_ms): _horizon_metrics(external, spot, pairs, int(horizon_ms))
        for horizon_ms in horizons_ms
    }
    primary_ms = int(horizons_ms[0])
    controls: dict[str, dict[str, object]] = {}
    for shift_ms in placebo_shifts_ms:
        shifted = _circular_shift(external, int(shift_ms))
        shifted_pairs = _pair_same_move(shifted, spot, source_tolerance_ms)
        shifted_metrics = _horizon_metrics(shifted, spot, shifted_pairs, primary_ms)
        controls[str(shift_ms)] = {
            "candidate_count": len(shifted),
            "leading_precision": shifted_metrics["leading_precision"],
        }
    reversed_rows = [ReceiptCandidate(
        source=row.source,
        recv_ns=row.recv_ns,
        source_ns=row.source_ns,
        slot=row.slot,
        direction=-row.direction,
        z_score=row.z_score,
    ) for row in external]
    reversed_pairs = _pair_same_move(reversed_rows, spot, source_tolerance_ms)
    reversed_metrics = _horizon_metrics(reversed_rows, spot, reversed_pairs, primary_ms)
    placebo_precisions = [float(row["leading_precision"]) for row in controls.values()]
    max_placebo = max([float(reversed_metrics["leading_precision"]), *placebo_precisions], default=0.0)
    primary = horizons[str(primary_ms)]
    lift = float(primary["leading_precision"]) - max_placebo
    signed_lead_p50 = primary["signed_lead_ms_p50"]
    checks = {
        "enough_candidates": len(external) >= minimum_candidates,
        "positive_signed_median_lead": signed_lead_p50 is not None and float(signed_lead_p50) > 0.0,
        "minimum_leading_precision": float(primary["leading_precision"]) >= minimum_leading_precision,
        "placebo_lift": lift >= minimum_lift,
    }
    return {
        "source": source,
        "candidate_count": len(external),
        "spot_candidate_count": len(spot),
        "source_tolerance_ms": source_tolerance_ms,
        "horizons": horizons,
        "controls": {
            "circular_time_shift_250ms": controls,
            "direction_reverse_precision_250ms": reversed_metrics["leading_precision"],
            "max_placebo_precision_250ms": max_placebo,
        },
        "execution_replay_eligibility": {
            "passes": all(checks.values()),
            "checks": checks,
            "leading_precision_lift": lift,
            "minimum_lift": minimum_lift,
            "minimum_candidates": minimum_candidates,
        },
        "active_gate": {
            "passes": False,
            "reason": "strict_live_equivalent_H_execution_replay_required",
        },
    }


def evaluate_race(
    candidates: Sequence[ReceiptCandidate],
    sources: Sequence[str],
    *,
    episode_ms: int = 1_000,
    **evaluation_options: object,
) -> dict[str, object]:
    allowed = set(sources)
    episode_ns = episode_ms * MS
    representatives: dict[tuple[int, int], list[int]] = {}
    winners: Counter[str] = Counter()
    race_rows: list[ReceiptCandidate] = []
    for row in sorted((item for item in candidates if item.source in allowed), key=lambda item: item.recv_ns):
        key = (row.slot, row.direction)
        if any(abs(row.source_ns - source_ns) < episode_ns for source_ns in representatives.setdefault(key, [])):
            continue
        representatives[key].append(row.source_ns)
        winners[row.source] += 1
        race_rows.append(ReceiptCandidate(
            source="receipt_race",
            recv_ns=row.recv_ns,
            source_ns=row.source_ns,
            slot=row.slot,
            direction=row.direction,
            z_score=row.z_score,
        ))
    spot_rows = [row for row in candidates if row.source == "spot_trade"]
    result = evaluate_source([*spot_rows, *race_rows], "receipt_race", **evaluation_options)
    result["members"] = list(sources)
    result["winner_counts"] = dict(sorted(winners.items()))
    return result


def _source_files(data_dir: Path, hours: Sequence[str]) -> list[Path]:
    sources = ("bn_spot", "bn_fut_trade", "bn_fut_pub", "deribit")
    paths = [data_dir / f"{source}.{hour}.txt.gz" for hour in hours for source in sources]
    required = [path for path in paths if path.name.startswith(("bn_spot.", "bn_fut_trade.", "deribit."))]
    missing = [path.name for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError("missing receipt-race inputs: " + ", ".join(missing))
    return [path for path in paths if path.exists()]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--hours", nargs="+", required=True, help="closed UTC hour stamps, e.g. 20261004T14")
    parser.add_argument("--episode-ms", type=int, default=1_000)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()

    paths = _source_files(args.data_dir, args.hours)
    health: Counter[str] = Counter()
    rows = extract_candidates(iter_raw_events_from_files(paths), episode_ms=args.episode_ms, health=health)
    payload = {
        "schema": "receipt-race-v2",
        "hours": list(args.hours),
        "analysis_episode_ms": args.episode_ms,
        "source_files": [path.name for path in paths],
        "source_health": dict(sorted(health.items())),
        "candidate_counts": {
            source: sum(row.source == source for row in rows)
            for source in ("spot_trade", "futures_trade", "futures_book_ticker", "deribit_quote")
        },
        "sources": {
            source: evaluate_source(rows, source)
            for source in ("futures_trade", "futures_book_ticker", "deribit_quote")
        },
        "races": {
            "futures_trade+deribit_quote": evaluate_race(
                rows, ("futures_trade", "deribit_quote"), episode_ms=args.episode_ms,
            ),
            "all_timestamped": evaluate_race(
                rows, ("futures_trade", "futures_book_ticker", "deribit_quote"),
                episode_ms=args.episode_ms,
            ),
        },
    }
    print(json.dumps(payload, indent=2 if args.pretty else None, sort_keys=True))


if __name__ == "__main__":
    main()
