from __future__ import annotations

import math
from decimal import Decimal, ROUND_HALF_UP

import basis_wedge as bw
import pytest


def bbo(kind: str, recv_ms: float, mid: float, **extra: object) -> dict[str, object]:
    return {
        "kind": kind,
        "symbol": "BTC",
        "recv_ms": recv_ms,
        "bid": mid - 0.005,
        "ask": mid + 0.005,
        **extra,
    }


def book(
    market_id: str,
    recv_ms: float,
    *,
    end_ms: float,
    up: tuple[float, float] = (0.49, 0.51),
    down: tuple[float, float] = (0.49, 0.51),
    up_bids: tuple[tuple[float, float], ...] | None = None,
    up_asks: tuple[tuple[float, float], ...] | None = None,
    down_bids: tuple[tuple[float, float], ...] | None = None,
    down_asks: tuple[tuple[float, float], ...] | None = None,
    **extra: object,
) -> dict[str, object]:
    return {
        "kind": "pm_book",
        "symbol": "BTC",
        "market_id": market_id,
        "recv_ms": recv_ms,
        "end_ms": end_ms,
        "up_bids": up_bids or ((up[0], 10.0),),
        "up_asks": up_asks or ((up[1], 10.0),),
        "down_bids": down_bids or ((down[0], 10.0),),
        "down_asks": down_asks or ((down[1], 10.0),),
        **extra,
    }


def prime(detector: bw.BasisWedgeDetector, market_id: str = "m", recv_ms: float = 0.0) -> None:
    detector.feed(bbo("spot_bbo", recv_ms, 100.0))
    detector.feed(bbo("futures_bbo", recv_ms, 100.0))
    detector.feed(bbo("deribit_quote", recv_ms, 100.0))
    detector.feed(book(market_id, recv_ms, end_ms=120_000.0))


def confirm(detector: bw.BasisWedgeDetector, recv_ms: float = 210.0) -> None:
    detector.feed(bbo("spot_bbo", recv_ms, 100.0))
    detector.feed(bbo("deribit_quote", recv_ms, 100.0))


def test_positive_basis_wedge_buys_direct_down_ask_with_preimpact_fair() -> None:
    detector = bw.BasisWedgeDetector()
    prime(detector)

    assert detector.feed(bbo("futures_bbo", 200.0, 100.02)) is None
    confirm(detector)
    signal = detector.feed(book(
        "m",
        300.0,
        end_ms=120_000.0,
        up=(0.52, 0.54),
        down=(0.43, 0.45),
        down_asks=((0.45, 5.0),),
    ))

    assert signal is not None
    assert signal.basis_direction == "up"
    assert signal.buy_side == "down"
    assert signal.fair == 0.50
    assert signal.ask == 0.45
    assert signal.net_edge >= 0.03
    assert signal.direct_depth == 5.0


def test_negative_basis_wedge_buys_direct_up_ask() -> None:
    detector = bw.BasisWedgeDetector()
    prime(detector)

    detector.feed(bbo("futures_bbo", 200.0, 99.98))
    confirm(detector)
    signal = detector.feed(book(
        "m",
        300.0,
        end_ms=120_000.0,
        up=(0.43, 0.45),
        down=(0.52, 0.54),
        up_asks=((0.45, 5.0),),
    ))

    assert signal is not None
    assert signal.basis_direction == "down"
    assert signal.buy_side == "up"
    assert signal.fair == 0.50
    assert signal.basis_change_bp <= -1.5


def test_spot_or_deribit_confirmation_rejects_the_wedge() -> None:
    confirming_frames = (
        (bbo("spot_bbo", 210.0, 100.004), bbo("deribit_quote", 220.0, 100.0)),
        (bbo("spot_bbo", 210.0, 100.0), bbo("deribit_quote", 220.0, 100.006)),
    )
    for first, second in confirming_frames:
        detector = bw.BasisWedgeDetector()
        prime(detector)
        detector.feed(bbo("futures_bbo", 200.0, 100.02))
        detector.feed(first)
        detector.feed(second)

        signal = detector.feed(book(
            "m",
            300.0,
            end_ms=120_000.0,
            up=(0.52, 0.54),
            down=(0.43, 0.45),
            down_asks=((0.45, 5.0),),
        ))

        assert signal is None


def test_polymarket_must_follow_the_basis_direction_by_two_cents() -> None:
    detector = bw.BasisWedgeDetector()
    prime(detector)
    detector.feed(bbo("futures_bbo", 200.0, 100.02))
    confirm(detector)

    signal = detector.feed(book(
        "m",
        300.0,
        end_ms=120_000.0,
        up=(0.50, 0.52),
        down=(0.43, 0.45),
        down_asks=((0.45, 5.0),),
    ))

    assert signal is None


def test_net_edge_and_eligible_direct_depth_are_both_required() -> None:
    rejected_asks = (
        ((0.46, 10.0),),
        ((0.45, 4.0), (0.46, 20.0)),
    )
    for asks in rejected_asks:
        detector = bw.BasisWedgeDetector()
        prime(detector)
        detector.feed(bbo("futures_bbo", 200.0, 100.02))
        confirm(detector)

        signal = detector.feed(book(
            "m",
            300.0,
            end_ms=120_000.0,
            up=(0.52, 0.54),
            down=(0.43, asks[0][0]),
            down_asks=asks,
        ))

        assert signal is None


def test_stale_source_frame_rejects_otherwise_qualifying_book() -> None:
    detector = bw.BasisWedgeDetector(bw.DetectorConfig(max_source_age_ms=100.0))
    prime(detector)
    detector.feed(bbo("futures_bbo", 50.0, 100.02))

    signal = detector.feed(book(
        "m",
        200.0,
        end_ms=120_000.0,
        up=(0.52, 0.54),
        down=(0.43, 0.45),
        down_asks=((0.45, 5.0),),
    ))

    assert signal is None


def test_disconnect_clears_state_and_every_source_needs_a_new_frame() -> None:
    detector = bw.BasisWedgeDetector()
    prime(detector)
    detector.feed(bbo("futures_bbo", 100.0, 100.02))
    detector.feed({
        "kind": "disconnect", "symbol": "BTC", "source": "futures", "recv_ms": 150.0,
    })

    assert detector.feed(book(
        "m",
        200.0,
        end_ms=120_000.0,
        up=(0.52, 0.54),
        down=(0.43, 0.45),
        down_asks=((0.45, 5.0),),
    )) is None

    detector.feed(bbo("spot_bbo", 210.0, 100.0))
    detector.feed(bbo("futures_bbo", 210.0, 100.0))
    detector.feed(bbo("deribit_quote", 210.0, 100.0))
    detector.feed(book("m", 220.0, end_ms=120_000.0))
    detector.feed(bbo("futures_bbo", 250.0, 100.02))
    assert detector.feed(book(
        "m",
        300.0,
        end_ms=120_000.0,
        up=(0.52, 0.54),
        down=(0.43, 0.45),
        down_asks=((0.45, 5.0),),
    )) is None

    # Start a genuinely fresh epoch.  Merely unwinding the first basis shock
    # would itself be a valid opposite-direction wedge and could emit first.
    detector.feed({
        "kind": "disconnect", "symbol": "BTC", "source": "futures", "recv_ms": 350.0,
    })
    detector.feed(bbo("spot_bbo", 400.0, 100.0))
    detector.feed(bbo("futures_bbo", 400.0, 100.0))
    detector.feed(bbo("deribit_quote", 400.0, 100.0))
    detector.feed(book("m", 400.0, end_ms=120_000.0))
    detector.feed(bbo("futures_bbo", 450.0, 100.02))
    confirm(detector, 460.0)
    assert detector.feed(book(
        "m",
        500.0,
        end_ms=120_000.0,
        up=(0.52, 0.54),
        down=(0.43, 0.45),
        down_asks=((0.45, 5.0),),
    )) is not None


def test_only_the_first_signal_per_market_is_emitted() -> None:
    detector = bw.BasisWedgeDetector()
    prime(detector, "m1")
    detector.feed(book("m2", 0.0, end_ms=120_000.0))
    detector.feed(bbo("futures_bbo", 200.0, 100.02))
    confirm(detector)
    followed = {
        "end_ms": 120_000.0,
        "up": (0.52, 0.54),
        "down": (0.43, 0.45),
        "down_asks": ((0.45, 5.0),),
    }

    assert detector.feed(book("m1", 300.0, **followed)) is not None
    assert detector.feed(book("m1", 310.0, **followed)) is None
    assert detector.feed(book("m2", 320.0, **followed)) is not None


def test_receipt_time_order_prevents_a_pre_shock_pm_move_from_counting() -> None:
    detector = bw.BasisWedgeDetector()
    prime(detector)
    moved_before_shock = {
        "end_ms": 120_000.0,
        "up": (0.52, 0.54),
        "down": (0.43, 0.45),
        "down_asks": ((0.45, 5.0),),
    }
    assert detector.feed(book("m", 100.0, source_ms=10_000.0, **moved_before_shock)) is None
    detector.feed(bbo("futures_bbo", 200.0, 100.02, source_ms=-10_000.0))
    confirm(detector)

    # The move received before the shock remains the anchor even though its
    # exchange timestamp claims it happened later.
    assert detector.feed(book("m", 250.0, **moved_before_shock)) is None

    signal = detector.feed(book(
        "m",
        300.0,
        end_ms=120_000.0,
        up=(0.55, 0.57),
        down=(0.37, 0.39),
        down_asks=((0.39, 5.0),),
    ))
    assert signal is not None
    assert signal.anchor_recv_ms == 100.0
    assert signal.fair == 0.47


def test_required_chainlink_must_be_fresh_and_quiet() -> None:
    followed = {
        "end_ms": 120_000.0,
        "up": (0.52, 0.54),
        "down": (0.43, 0.45),
        "down_asks": ((0.45, 5.0),),
    }
    missing = bw.BasisWedgeDetector(bw.DetectorConfig(require_chainlink=True))
    prime(missing)
    missing.feed(bbo("futures_bbo", 200.0, 100.02))
    assert missing.feed(book("m", 300.0, **followed)) is None

    confirming = bw.BasisWedgeDetector(bw.DetectorConfig(require_chainlink=True))
    confirming.feed(bbo("spot_bbo", 0.0, 100.0))
    confirming.feed(bbo("futures_bbo", 0.0, 100.0))
    confirming.feed(bbo("deribit_quote", 0.0, 100.0))
    confirming.feed({"kind": "chainlink", "symbol": "BTC", "recv_ms": 0.0, "price": 100.0})
    confirming.feed(book("m", 0.0, end_ms=120_000.0))
    confirming.feed({
        "kind": "chainlink", "symbol": "BTC", "recv_ms": 150.0, "price": 100.004,
    })
    confirming.feed(bbo("futures_bbo", 200.0, 100.02))
    confirm(confirming)
    confirming.feed({
        "kind": "chainlink", "symbol": "BTC", "recv_ms": 220.0, "price": 100.004,
    })
    assert confirming.feed(book("m", 300.0, **followed)) is None

    quiet = bw.BasisWedgeDetector(bw.DetectorConfig(require_chainlink=True))
    quiet.feed(bbo("spot_bbo", 0.0, 100.0))
    quiet.feed(bbo("futures_bbo", 0.0, 100.0))
    quiet.feed(bbo("deribit_quote", 0.0, 100.0))
    quiet.feed({"kind": "chainlink", "symbol": "BTC", "recv_ms": 0.0, "price": 100.0})
    quiet.feed(book("m", 0.0, end_ms=120_000.0))
    quiet.feed(bbo("futures_bbo", 200.0, 100.02))
    confirm(quiet)
    quiet.feed({"kind": "chainlink", "symbol": "BTC", "recv_ms": 220.0, "price": 100.0})
    assert quiet.feed(book("m", 300.0, **followed)) is not None


def test_remaining_time_must_be_between_30_and_240_seconds() -> None:
    for remaining_s in (29.999, 240.001):
        detector = bw.BasisWedgeDetector()
        prime(detector)
        detector.feed(bbo("futures_bbo", 200.0, 100.02))
        confirm(detector)
        assert detector.feed(book(
            "m",
            300.0,
            end_ms=300.0 + remaining_s * 1_000.0,
            up=(0.52, 0.54),
            down=(0.43, 0.45),
            down_asks=((0.45, 5.0),),
        )) is None


def test_mirrored_ask_is_never_used_in_place_of_the_direct_ask() -> None:
    detector = bw.BasisWedgeDetector()
    prime(detector)
    detector.feed(bbo("futures_bbo", 200.0, 100.02))
    confirm(detector)

    signal = detector.feed(book(
        "m",
        300.0,
        end_ms=120_000.0,
        up=(0.60, 0.62),
        down=(0.46, 0.48),
        down_asks=((0.48, 20.0),),
    ))

    assert signal is None


def test_no_signal_until_spot_and_deribit_publish_after_the_futures_shock() -> None:
    detector = bw.BasisWedgeDetector()
    prime(detector)
    detector.feed(bbo("futures_bbo", 200.0, 100.02))
    followed = dict(end_ms=120_000.0, up=(0.52, 0.54), down=(0.43, 0.45),
                    down_asks=((0.45, 5.0),))

    assert detector.feed(book("m", 250.0, **followed)) is None
    detector.feed(bbo("spot_bbo", 260.0, 100.0))
    assert detector.feed(book("m", 270.0, **followed)) is None
    signal = detector.feed(bbo("deribit_quote", 280.0, 100.0))
    assert signal is not None
    assert signal.recv_ms == 280.0
    assert detector.feed(book("m", 290.0, **followed)) is None


def test_out_of_order_disconnect_fails_closed_instead_of_preserving_candidate() -> None:
    detector = bw.BasisWedgeDetector()
    prime(detector)
    detector.feed(bbo("futures_bbo", 200.0, 100.02))
    confirm(detector, 220.0)
    detector.feed({"kind": "futures_disconnect", "symbol": "BTC", "recv_ms": 150.0})

    assert detector.feed(book(
        "m", 300.0, end_ms=120_000.0, up=(0.52, 0.54), down=(0.43, 0.45),
        down_asks=((0.45, 5.0),),
    )) is None


def test_reverse_fair_is_the_complement_of_the_anchor_direction_midpoint() -> None:
    detector = bw.BasisWedgeDetector()
    detector.feed(bbo("spot_bbo", 0.0, 100.0))
    detector.feed(bbo("futures_bbo", 0.0, 100.0))
    detector.feed(bbo("deribit_quote", 0.0, 100.0))
    detector.feed(book("m", 0.0, end_ms=120_000.0, up=(0.59, 0.61), down=(0.49, 0.51)))
    detector.feed(bbo("futures_bbo", 200.0, 100.02))
    confirm(detector)

    signal = detector.feed(book(
        "m", 300.0, end_ms=120_000.0, up=(0.62, 0.64), down=(0.33, 0.35),
        down_asks=((0.35, 5.0),),
    ))

    assert signal is not None
    assert signal.fair == 0.40


def test_follow_midpoint_requires_tight_five_share_bid_and_ask() -> None:
    cases = (
        dict(up=(0.45, 0.55)),
        dict(up_bids=((0.52, 1.0),), up_asks=((0.54, 10.0),)),
    )
    for override in cases:
        detector = bw.BasisWedgeDetector()
        prime(detector)
        detector.feed(bbo("futures_bbo", 200.0, 100.02))
        confirm(detector)
        assert detector.feed(book(
            "m", 300.0, end_ms=120_000.0, down=(0.43, 0.45),
            down_asks=((0.45, 5.0),), **override,
        )) is None


def test_optional_chainlink_is_checked_when_a_post_shock_frame_exists() -> None:
    detector = bw.BasisWedgeDetector()
    detector.feed(bbo("spot_bbo", 0.0, 100.0))
    detector.feed(bbo("futures_bbo", 0.0, 100.0))
    detector.feed(bbo("deribit_quote", 0.0, 100.0))
    detector.feed({"kind": "chainlink", "symbol": "BTC", "recv_ms": 0.0, "price": 100.0})
    detector.feed(book("m", 0.0, end_ms=120_000.0))
    detector.feed(bbo("futures_bbo", 200.0, 100.02))
    confirm(detector)
    detector.feed({
        "kind": "chainlink", "symbol": "BTC", "recv_ms": 220.0, "price": 100.004,
    })

    assert detector.feed(book(
        "m", 300.0, end_ms=120_000.0, up=(0.52, 0.54), down=(0.43, 0.45),
        down_asks=((0.45, 5.0),),
    )) is None


def test_reported_price_fee_and_edge_use_the_full_five_share_vwap() -> None:
    detector = bw.BasisWedgeDetector()
    prime(detector)
    detector.feed(bbo("futures_bbo", 200.0, 100.02))
    confirm(detector)
    signal = detector.feed(book(
        "m", 300.0, end_ms=120_000.0, up=(0.52, 0.54), down=(0.42, 0.44),
        down_asks=((0.44, 1.0), (0.45, 4.0)),
    ))

    assert signal is not None
    assert math.isclose(signal.ask, 0.448)
    raw_fee = (
        Decimal("1") * Decimal("0.07") * Decimal("0.44") * Decimal("0.56")
        + Decimal("4") * Decimal("0.07") * Decimal("0.45") * Decimal("0.55")
    )
    rounded_fee = raw_fee.quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP)
    expected_fee_per_share = float(rounded_fee) / 5.0
    assert signal.fee == expected_fee_per_share
    assert math.isclose(signal.net_edge, signal.fair - 0.448 - expected_fee_per_share)


def test_same_millisecond_frames_use_feed_order_not_timestamp_equality() -> None:
    detector = bw.BasisWedgeDetector()
    prime(detector)
    detector.feed(bbo("spot_bbo", 200.0, 100.0))
    detector.feed(bbo("deribit_quote", 200.0, 100.0))
    detector.feed(bbo("futures_bbo", 200.0, 100.02))

    followed = dict(
        end_ms=120_000.0,
        up=(0.52, 0.54),
        down=(0.43, 0.45),
        down_asks=((0.45, 5.0),),
    )
    assert detector.feed(book("m", 200.0, **followed)) is None
    detector.feed(bbo("spot_bbo", 200.0, 100.0))
    signal = detector.feed(bbo("deribit_quote", 200.0, 100.0))
    assert signal is not None
    assert detector.feed(book("m", 200.0, **followed)) is None


def test_same_millisecond_post_anchor_quote_cannot_rewrite_the_anchor_state() -> None:
    detector = bw.BasisWedgeDetector()
    prime(detector)
    detector.feed(book("m", 200.0, end_ms=120_000.0))
    detector.feed(bbo("futures_bbo", 200.0, 100.02))
    confirm(detector, 210.0)

    signal = detector.feed(book(
        "m", 300.0, end_ms=120_000.0, up=(0.52, 0.54), down=(0.43, 0.45),
        down_asks=((0.45, 5.0),),
    ))
    assert signal is not None
    assert signal.anchor_recv_ms == 200.0
    assert signal.basis_change_bp >= 1.5


def test_first_qualifying_futures_crossing_is_immutable_until_confirmation() -> None:
    detector = bw.BasisWedgeDetector()
    prime(detector)
    detector.feed(bbo("futures_bbo", 200.0, 100.02))
    followed = dict(
        end_ms=120_000.0,
        up=(0.52, 0.54),
        down=(0.43, 0.45),
        down_asks=((0.45, 5.0),),
    )
    assert detector.feed(book("m", 205.0, **followed)) is None
    detector.feed(bbo("futures_bbo", 210.0, 100.02))
    detector.feed(bbo("spot_bbo", 220.0, 100.0))
    signal = detector.feed(bbo("deribit_quote", 230.0, 100.0))

    assert signal is not None
    assert signal.anchor_recv_ms == 0.0
    assert detector.feed(book("m", 240.0, **followed)) is None


def test_symbol_is_mandatory_and_other_symbols_cannot_consume_btc_state() -> None:
    detector = bw.BasisWedgeDetector()
    with pytest.raises(ValueError, match="symbol"):
        detector.feed({
            "kind": "spot_bbo", "recv_ms": 0.0, "bid": 99.995, "ask": 100.005,
        })
    assert detector.feed(bbo("spot_bbo", 0.0, 100.0, symbol="ETH")) is None


def test_direct_depth_reports_all_eligible_displayed_shares() -> None:
    detector = bw.BasisWedgeDetector()
    prime(detector)
    detector.feed(bbo("futures_bbo", 200.0, 100.02))
    confirm(detector)
    signal = detector.feed(book(
        "m", 300.0, end_ms=120_000.0, up=(0.52, 0.54), down=(0.42, 0.44),
        down_asks=((0.44, 25.0), (0.45, 75.0)),
    ))

    assert signal is not None
    assert signal.direct_depth == 100.0


def test_fee_rounding_uses_exact_decimal_half_up_at_a_tie() -> None:
    detector = bw.BasisWedgeDetector()
    fill = detector._direct_fill(((0.81, 5.0),), fair=0.90)

    assert fill is not None
    assert fill[2] == 0.05387 / 5.0
