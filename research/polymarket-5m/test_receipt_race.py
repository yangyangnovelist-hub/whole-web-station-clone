from __future__ import annotations

import receipt_race as rr
from h_replay_run import SignalConfig, TimestampedFirstTrigger
from jump_causal import MS, MarketEvent


def candidate(source: str, recv_ms: float, direction: int, *, slot: int = 0) -> rr.ReceiptCandidate:
    return rr.ReceiptCandidate(
        source=source,
        recv_ns=int(recv_ms * MS),
        source_ns=int((recv_ms - 10.0) * MS),
        slot=slot,
        direction=direction,
        z_score=3.0,
    )


def test_timestamped_trigger_accepts_named_sources_without_changing_frozen_methods():
    config = SignalConfig(
        z0=1.0,
        tau_hi_s=300.0,
        tau_lo_s=0.0,
        sigma_window_s=3,
        sigma_min_observations=2,
        forward_fill_s=1,
    )
    trigger = TimestampedFirstTrigger(config)
    for timestamp_ms, price in ((0, 100.0), (1_000, 100.01), (2_000, 100.0), (3_000, 100.01)):
        trigger.update_trade(timestamp_ms, price)

    assert trigger.update_source("futures_trade", 4_000, 100.0) is None
    futures = trigger.update_source("futures_trade", 5_100, 100.1)
    assert futures is not None and futures.direction == 1

    assert trigger.update_source("deribit_quote", 4_000, 100.0) is None
    deribit = trigger.update_source("deribit_quote", 5_100, 99.9)
    assert deribit is not None and deribit.direction == -1


def test_extract_candidates_uses_receipt_order_and_exchange_source_clock():
    config = SignalConfig(
        z0=1.0,
        tau_hi_s=300.0,
        tau_lo_s=0.0,
        sigma_window_s=3,
        sigma_min_observations=2,
        forward_fill_s=1,
    )
    events = []
    for second, price in ((0, 100.0), (1, 100.01), (2, 100.0), (3, 100.01)):
        events.append(MarketEvent(
            recv_ns=(second * 1_000 + 100) * MS,
            source="bn_spot",
            channel="bn_spot",
            kind="trade",
            exchange_ns=second * 1_000 * MS,
            price=price,
        ))
    events.extend([
        MarketEvent(4_100 * MS, "bn_spot", "bn_spot", "trade", 4_000 * MS, price=100.0),
        MarketEvent(4_100 * MS, "bn_fut_trade", "bn_perp", "trade", 4_000 * MS, price=100.0),
        MarketEvent(4_120 * MS, "deribit", "deribit", "book", 4_040 * MS,
                    bid=99.99, ask=100.01),
        MarketEvent(5_070 * MS, "deribit", "deribit", "book", 5_050 * MS,
                    bid=100.09, ask=100.11),
        MarketEvent(5_080 * MS, "bn_fut_trade", "bn_perp", "trade", 5_000 * MS, price=100.1),
        MarketEvent(5_100 * MS, "bn_spot", "bn_spot", "trade", 5_000 * MS, price=100.1),
    ])

    rows = rr.extract_candidates(iter(events), config=config, episode_ms=1_000)

    assert [(row.source, row.recv_ns // MS) for row in rows[-3:]] == [
        ("deribit_quote", 5_070),
        ("futures_trade", 5_080),
        ("spot_trade", 5_100),
    ]
    assert all(row.source_ns <= row.recv_ns for row in rows)


def test_source_evaluation_requires_real_confirmation_lift_over_placebos():
    rows = [
        candidate("futures_trade", 61_000, 1),
        candidate("spot_trade", 61_020, 1),
        candidate("futures_trade", 62_000, -1),
        candidate("spot_trade", 62_040, -1),
        candidate("futures_trade", 63_000, 1),
        candidate("spot_trade", 64_000, -1),
    ]

    result = rr.evaluate_source(
        rows,
        "futures_trade",
        horizons_ms=(250, 500),
        placebo_shifts_ms=(5_000,),
        minimum_candidates=3,
    )

    assert result["candidate_count"] == 3
    assert result["horizons"]["250"]["leading_confirmed"] == 2
    assert result["horizons"]["250"]["leading_precision"] == 2 / 3
    assert result["horizons"]["250"]["spot_leading_coverage"] == 2 / 3
    assert result["horizons"]["250"]["signed_lead_ms_p50"] == 30.0
    assert result["controls"]["direction_reverse_precision_250ms"] == 0.0
    assert result["execution_replay_eligibility"]["passes"] is True
    assert result["active_gate"]["passes"] is False


def test_placebo_like_source_does_not_pass_active_gate():
    rows = [
        candidate("deribit_quote", 61_000, 1),
        candidate("spot_trade", 61_020, 1),
        candidate("deribit_quote", 62_000, 1),
        candidate("spot_trade", 62_020, 1),
        candidate("deribit_quote", 66_000, 1),
        candidate("spot_trade", 66_020, 1),
        candidate("deribit_quote", 67_000, 1),
        candidate("spot_trade", 67_020, 1),
        candidate("spot_trade", 71_020, 1),
        candidate("spot_trade", 72_020, 1),
    ]

    result = rr.evaluate_source(
        rows,
        "deribit_quote",
        horizons_ms=(250,),
        placebo_shifts_ms=(5_000,),
        minimum_candidates=4,
    )

    assert result["horizons"]["250"]["leading_precision"] == 1.0
    assert result["controls"]["max_placebo_precision_250ms"] == 1.0
    assert result["active_gate"]["passes"] is False


def test_combined_race_keeps_only_the_first_source_for_each_move():
    rows = [
        candidate("deribit_quote", 60_980, 1),
        candidate("futures_trade", 60_990, 1),
        candidate("spot_trade", 61_020, 1),
        candidate("futures_trade", 62_000, -1),
        candidate("spot_trade", 62_030, -1),
    ]

    result = rr.evaluate_race(
        rows,
        ("futures_trade", "deribit_quote"),
        episode_ms=1_000,
        horizons_ms=(250,),
        placebo_shifts_ms=(),
        minimum_candidates=2,
    )

    assert result["candidate_count"] == 2
    assert result["winner_counts"] == {"deribit_quote": 1, "futures_trade": 1}
    assert result["horizons"]["250"]["leading_precision"] == 1.0
    assert result["horizons"]["250"]["spot_leading_coverage"] == 1.0


def test_extract_candidates_rejects_duplicate_future_and_stale_source_clocks():
    config = SignalConfig(
        z0=1.0,
        tau_hi_s=300.0,
        tau_lo_s=0.0,
        sigma_window_s=3,
        sigma_min_observations=2,
        forward_fill_s=1,
    )
    events = [
        MarketEvent((second * 1_000 + 100) * MS, "bn_spot", "bn_spot", "trade",
                    second * 1_000 * MS, price=price, sequence_id=str(second))
        for second, price in ((0, 100.0), (1, 100.01), (2, 100.0), (3, 100.01))
    ]
    events.extend([
        MarketEvent(4_100 * MS, "bn_fut_trade", "bn_perp", "trade", 4_000 * MS,
                    price=100.0, sequence_id="7"),
        MarketEvent(5_080 * MS, "bn_fut_trade", "bn_perp", "trade", 5_000 * MS,
                    price=100.1, sequence_id="8"),
        MarketEvent(5_081 * MS, "bn_fut_trade", "bn_perp", "trade", 5_000 * MS,
                    price=100.1, sequence_id="8"),
        MarketEvent(5_090 * MS, "bn_fut_trade", "bn_perp", "trade", 2_000 * MS,
                    price=99.0, sequence_id="9"),
    ])
    health = rr.Counter()

    rows = rr.extract_candidates(iter(events), config=config, episode_ms=0, health=health)

    assert sum(row.source == "futures_trade" for row in rows) == 1
    assert health["duplicate_frame"] == 1
    assert health["stale_source_clock"] == 1
