from __future__ import annotations

import hashlib
import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import basis_wedge as bw
import common_mode_residual as cmr
import pytest


ROOT = Path(__file__).resolve().parent
FREEZE = ROOT / "forward" / "common-mode-residual-freeze.json"


def bbo(kind: str, recv_ms: float, mid: float, **extra: object) -> dict[str, object]:
    return {
        "kind": kind,
        "symbol": "BTC",
        "recv_ms": recv_ms,
        "bid": mid - 0.005,
        "ask": mid + 0.005,
        **extra,
    }


def index(recv_ms: float, price: float, **extra: object) -> dict[str, object]:
    return {
        "kind": "deribit_index",
        "symbol": "BTC",
        "recv_ms": recv_ms,
        "price": price,
        **extra,
    }


def book(
    recv_ms: float,
    *,
    market_id: str = "m",
    end_ms: float = 120_000.0,
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


def prime(detector: cmr.CommonModeResidualDetector, *, market_id: str = "m") -> None:
    detector.feed(bbo("deribit_quote", 0.0, 100.0))
    detector.feed(index(0.0, 100.0))
    detector.feed(book(0.0, market_id=market_id))
    detector.feed(bbo("spot_bbo", 0.0, 100.0))
    detector.feed(bbo("futures_bbo", 0.0, 100.0))


def cross_up(detector: cmr.CommonModeResidualDetector) -> None:
    detector.feed(bbo("spot_bbo", 200.0, 100.02))
    detector.feed(bbo("futures_bbo", 210.0, 100.02))


def quiet_benchmarks(detector: cmr.CommonModeResidualDetector) -> None:
    detector.feed(bbo("deribit_quote", 220.0, 100.0))
    detector.feed(index(230.0, 100.0))


def test_frozen_protocol_matches_detector_and_dependency_bytes() -> None:
    raw = FREEZE.read_bytes()
    frozen = json.loads(raw)
    config = cmr.DetectorConfig()

    assert frozen["schema"] == "common-mode-residual-protocol-v1"
    assert hashlib.sha256(raw).hexdigest() == FREEZE.with_suffix(".sha256").read_text(
        encoding="ascii",
    ).strip()
    assert frozen["holdout_start_ms"] == 1_791_676_800_000
    assert frozen["promotion"]["forward_evaluator_ready"] is False
    assert frozen["signal"]["common_move_window_ms"] == config.window_ms
    assert (
        frozen["signal"]["benchmark_confirmation_timeout_ms"]
        == config.confirmation_timeout_ms
    )
    assert frozen["signal"]["minimum_same_sign_spot_and_perpetual_move_bp"] == (
        config.min_common_move_bp
    )
    assert frozen["signal"]["maximum_absolute_basis_change_bp"] == (
        config.max_basis_change_bp
    )
    assert frozen["signal"]["maximum_absolute_deribit_quote_and_index_path_move_bp"] == (
        config.max_benchmark_move_bp
    )
    assert frozen["execution"]["minimum_net_edge_per_share"] == config.min_net_edge
    assert frozen["execution"]["minimum_full_fill_shares"] == config.min_direct_depth
    for relative, expected in frozen["dependencies_sha256"].items():
        actual = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
        assert actual == expected, relative


def followed_up_book(
    recv_ms: float = 240.0,
    *,
    down: tuple[float, float] = (0.43, 0.45),
    down_asks: tuple[tuple[float, float], ...] = ((0.45, 5.0),),
    **extra: object,
) -> dict[str, object]:
    return book(
        recv_ms,
        up=(0.52, 0.54),
        down=down,
        down_asks=down_asks,
        **extra,
    )


def test_positive_common_mode_residual_buys_direct_opposite_token() -> None:
    detector = cmr.CommonModeResidualDetector()
    prime(detector)
    cross_up(detector)
    quiet_benchmarks(detector)

    signal = detector.feed(followed_up_book())

    assert signal is not None
    assert signal.common_direction == "up"
    assert signal.buy_side == "down"
    assert signal.fair == 0.50
    assert signal.ask == 0.45
    assert signal.fixed_limit == 0.45
    expected_total_fee = (
        Decimal(5) * Decimal("0.07") * Decimal("0.45") * Decimal("0.55")
    ).quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP)
    assert signal.fee == float(expected_total_fee) / 5.0
    assert signal.net_edge >= 0.03
    assert signal.direct_depth == 5.0
    assert signal.spot_move_bp >= 1.5
    assert signal.futures_move_bp >= 1.5
    assert abs(signal.basis_change_bp) <= 0.5
    assert abs(signal.deribit_quote_move_bp) <= 0.5
    assert abs(signal.deribit_index_move_bp) <= 0.5
    assert signal.pm_move >= 0.02


def test_negative_common_mode_residual_buys_direct_up_token() -> None:
    detector = cmr.CommonModeResidualDetector()
    prime(detector)
    detector.feed(bbo("spot_bbo", 200.0, 99.98))
    detector.feed(bbo("futures_bbo", 210.0, 99.98))
    quiet_benchmarks(detector)

    signal = detector.feed(book(
        240.0,
        up=(0.43, 0.45),
        down=(0.52, 0.54),
        up_asks=((0.45, 5.0),),
    ))

    assert signal is not None
    assert signal.common_direction == "down"
    assert signal.buy_side == "up"
    assert signal.spot_move_bp <= -1.5
    assert signal.futures_move_bp <= -1.5


def test_common_mode_and_us015_basis_wedge_are_disjoint() -> None:
    common = cmr.CommonModeResidualDetector()
    prime(common)
    cross_up(common)
    quiet_benchmarks(common)
    common_signal = common.feed(followed_up_book())
    assert common_signal is not None
    assert abs(common_signal.basis_change_bp) <= 0.5

    wedge_on_common = bw.BasisWedgeDetector()
    wedge_on_common.feed(bbo("spot_bbo", 0.0, 100.0))
    wedge_on_common.feed(bbo("futures_bbo", 0.0, 100.0))
    wedge_on_common.feed(bbo("deribit_quote", 0.0, 100.0))
    wedge_on_common.feed(book(0.0))
    wedge_on_common.feed(bbo("spot_bbo", 200.0, 100.02))
    wedge_on_common.feed(bbo("futures_bbo", 210.0, 100.02))
    wedge_on_common.feed(bbo("spot_bbo", 220.0, 100.02))
    wedge_on_common.feed(bbo("deribit_quote", 230.0, 100.0))
    assert wedge_on_common.feed(followed_up_book()) is None

    residual_on_wedge = cmr.CommonModeResidualDetector()
    prime(residual_on_wedge)
    residual_on_wedge.feed(bbo("futures_bbo", 210.0, 100.02))
    quiet_benchmarks(residual_on_wedge)
    assert residual_on_wedge.feed(followed_up_book()) is None

    wedge = bw.BasisWedgeDetector()
    wedge.feed(bbo("spot_bbo", 0.0, 100.0))
    wedge.feed(bbo("futures_bbo", 0.0, 100.0))
    wedge.feed(bbo("deribit_quote", 0.0, 100.0))
    wedge.feed(book(0.0))
    wedge.feed(bbo("futures_bbo", 210.0, 100.02))
    wedge.feed(bbo("spot_bbo", 220.0, 100.0))
    wedge.feed(bbo("deribit_quote", 230.0, 100.0))
    wedge_signal = wedge.feed(followed_up_book())
    assert wedge_signal is not None
    assert abs(wedge_signal.basis_change_bp) >= 1.5


def test_asynchronous_anchors_cannot_make_us015_and_us017_both_emit() -> None:
    common = cmr.CommonModeResidualDetector()
    wedge = bw.BasisWedgeDetector()
    tape = (
        bbo("spot_bbo", 0.0, 100.0),
        bbo("futures_bbo", 0.0, 100.0),
        bbo("deribit_quote", 0.0, 100.0),
        index(0.0, 100.0),
        book(0.0),
        bbo("futures_bbo", 100.0, 100.02),
        bbo("spot_bbo", 110.0, 100.0),
        bbo("deribit_quote", 120.0, 100.0),
        followed_up_book(130.0),
        bbo("spot_bbo", 200.0, 100.02),
        bbo("futures_bbo", 210.0, 100.04),
        bbo("deribit_quote", 220.0, 100.0),
        index(230.0, 100.0),
        followed_up_book(240.0),
    )
    wedge_signal = None
    common_signal = None
    for event in tape:
        if event["kind"] != "deribit_index":
            wedge_signal = wedge.feed(event) or wedge_signal
        common_signal = common.feed(event) or common_signal

    assert wedge_signal is not None
    assert wedge_signal.anchor_recv_ms == 0.0
    assert common_signal is None


def test_us015_candidate_capture_excludes_us017_before_delayed_confirmation() -> None:
    common = cmr.CommonModeResidualDetector()
    wedge = bw.BasisWedgeDetector()
    before_delayed_confirmation = (
        bbo("spot_bbo", 0.0, 100.0),
        bbo("futures_bbo", 0.0, 100.0),
        bbo("deribit_quote", 0.0, 100.0),
        index(0.0, 100.0),
        book(0.0),
        bbo("futures_bbo", 100.0, 100.02),
        bbo("spot_bbo", 200.0, 100.02),
        bbo("futures_bbo", 210.0, 100.04),
        bbo("deribit_quote", 220.0, 100.0),
        index(230.0, 100.0),
        followed_up_book(240.0),
    )
    common_signal = None
    wedge_signal = None
    for event in before_delayed_confirmation:
        if event["kind"] != "deribit_index":
            wedge_signal = wedge.feed(event) or wedge_signal
        common_signal = common.feed(event) or common_signal

    assert wedge_signal is None
    assert common_signal is None

    delayed_confirmation = bbo("spot_bbo", 250.0, 100.0)
    wedge_signal = wedge.feed(delayed_confirmation)
    assert wedge_signal is not None
    assert common.feed(delayed_confirmation) is None


def test_both_benchmarks_require_strictly_post_crossing_frames() -> None:
    detector = cmr.CommonModeResidualDetector()
    prime(detector)
    cross_up(detector)

    assert detector.feed(followed_up_book(215.0)) is None
    assert detector.feed(bbo("deribit_quote", 220.0, 100.0)) is None
    signal = detector.feed(index(230.0, 100.0))

    assert signal is not None
    assert signal.recv_ms == 230.0


def test_deribit_confirmation_allows_one_second_cadence_but_expires_after_1500ms() -> None:
    one_second = cmr.CommonModeResidualDetector()
    prime(one_second)
    cross_up(one_second)
    one_second.feed(bbo("deribit_quote", 220.0, 100.0))
    one_second.feed(bbo("spot_bbo", 1_180.0, 100.02))
    one_second.feed(bbo("futures_bbo", 1_190.0, 100.02))
    assert one_second.feed(followed_up_book(1_200.0)) is None

    signal = one_second.feed(index(1_210.0, 100.0))

    assert signal is not None
    assert signal.recv_ms == 1_210.0
    assert signal.crossing_recv_ms == 210.0

    expired = cmr.CommonModeResidualDetector()
    prime(expired)
    cross_up(expired)
    expired.feed(bbo("deribit_quote", 220.0, 100.0))
    expired.feed(bbo("spot_bbo", 1_690.0, 100.02))
    expired.feed(bbo("futures_bbo", 1_700.0, 100.02))
    expired.feed(followed_up_book(1_705.0))
    assert expired.feed(index(1_711.0, 100.0)) is None


def test_any_benchmark_confirmation_before_decision_rejects_quiet_base_arm() -> None:
    detector = cmr.CommonModeResidualDetector()
    prime(detector)
    cross_up(detector)
    detector.feed(bbo("deribit_quote", 220.0, 100.0))
    detector.feed(bbo("deribit_quote", 225.0, 100.006))
    detector.feed(index(230.0, 100.0))

    assert detector.feed(followed_up_book()) is None


def test_late_quiet_frame_cannot_erase_earlier_benchmark_confirmation() -> None:
    detector = cmr.CommonModeResidualDetector()
    prime(detector)
    cross_up(detector)
    detector.feed(bbo("deribit_quote", 220.0, 100.006))
    detector.feed(bbo("deribit_quote", 225.0, 100.0))
    detector.feed(index(230.0, 100.0))

    assert detector.feed(followed_up_book()) is None


def test_benchmark_confirmation_placebo_is_disjoint_from_quiet_base_arm() -> None:
    base = cmr.CommonModeResidualDetector()
    prime(base)
    cross_up(base)
    base.feed(bbo("deribit_quote", 220.0, 100.006))
    base.feed(index(230.0, 100.006))
    assert base.feed(followed_up_book()) is None

    confirming = cmr.CommonModeResidualDetector(cmr.DetectorConfig(
        require_benchmark_confirmation=True,
    ))
    prime(confirming)
    cross_up(confirming)
    confirming.feed(bbo("deribit_quote", 220.0, 100.006))
    confirming.feed(index(230.0, 100.006))
    signal = confirming.feed(followed_up_book())
    assert signal is not None
    assert signal.deribit_quote_move_bp > 0.5
    assert signal.deribit_index_move_bp > 0.5

    mixed = cmr.CommonModeResidualDetector(cmr.DetectorConfig(
        require_benchmark_confirmation=True,
    ))
    prime(mixed)
    cross_up(mixed)
    mixed.feed(bbo("deribit_quote", 220.0, 100.006))
    mixed.feed(index(230.0, 100.0))
    assert mixed.feed(followed_up_book()) is None


def test_pm_follow_direct_depth_and_net_edge_are_all_required() -> None:
    rejected_books = (
        book(
            240.0,
            up=(0.50, 0.52),
            down=(0.43, 0.45),
            down_asks=((0.45, 5.0),),
        ),
        followed_up_book(down_asks=((0.45, 4.0),)),
        followed_up_book(down=(0.44, 0.46), down_asks=((0.46, 10.0),)),
    )
    for current_book in rejected_books:
        detector = cmr.CommonModeResidualDetector()
        prime(detector)
        cross_up(detector)
        quiet_benchmarks(detector)
        assert detector.feed(current_book) is None


def test_max_pm_move_rejects_overshoot_permanently_before_emission() -> None:
    detector = cmr.CommonModeResidualDetector(cmr.DetectorConfig(
        min_pm_move=0.0,
        max_pm_move=0.005,
    ))
    prime(detector)
    cross_up(detector)
    quiet_benchmarks(detector)

    assert detector.feed(followed_up_book()) is None
    assert detector.feed(book(
        250.0,
        up=(0.50, 0.51),
        down=(0.43, 0.45),
        down_asks=((0.45, 5.0),),
    )) is None


def test_first_qualifying_crossing_is_immutable_and_market_emits_once() -> None:
    detector = cmr.CommonModeResidualDetector()
    prime(detector)
    cross_up(detector)
    detector.feed(bbo("spot_bbo", 212.0, 100.04))
    detector.feed(bbo("futures_bbo", 214.0, 100.04))
    quiet_benchmarks(detector)

    signal = detector.feed(followed_up_book())

    assert signal is not None
    assert signal.crossing_recv_ms == 210.0
    assert 1.9 < signal.spot_move_bp < 2.1
    assert 1.9 < signal.futures_move_bp < 2.1
    assert detector.feed(followed_up_book(250.0)) is None


def test_first_qualifying_crossing_is_consumed_when_prerequisites_are_missing() -> None:
    detector = cmr.CommonModeResidualDetector()
    detector.feed(book(0.0))
    detector.feed(bbo("spot_bbo", 0.0, 100.0))
    detector.feed(bbo("futures_bbo", 0.0, 100.0))
    cross_up(detector)

    detector.feed(bbo("deribit_quote", 220.0, 100.0))
    detector.feed(index(220.0, 100.0))
    detector.feed(bbo("spot_bbo", 400.0, 100.04))
    detector.feed(bbo("futures_bbo", 410.0, 100.04))
    detector.feed(bbo("deribit_quote", 420.0, 100.0))
    detector.feed(index(430.0, 100.0))

    assert detector.feed(followed_up_book(440.0)) is None


def test_disconnect_and_receipt_regression_clear_all_live_state() -> None:
    disruptions = (
        {"kind": "disconnect", "symbol": "BTC", "recv_ms": 215.0},
        {"kind": "futures_disconnect", "symbol": "BTC", "recv_ms": 215.0},
        bbo("spot_bbo", 100.0, 100.0),
    )
    for disruption in disruptions:
        detector = cmr.CommonModeResidualDetector()
        prime(detector)
        cross_up(detector)
        detector.feed(disruption)
        quiet_benchmarks(detector)
        assert detector.feed(followed_up_book()) is None


def test_receipt_clock_regression_does_not_lower_high_watermark() -> None:
    detector = cmr.CommonModeResidualDetector()
    prime(detector)
    cross_up(detector)
    detector.feed(bbo("spot_bbo", 100.0, 100.0))

    detector.feed(bbo("deribit_quote", 150.0, 100.0))
    detector.feed(index(160.0, 100.0))
    detector.feed(book(170.0))
    detector.feed(bbo("spot_bbo", 180.0, 100.0))
    detector.feed(bbo("futures_bbo", 190.0, 100.0))

    assert detector.feed(followed_up_book(200.0)) is None


def test_equal_receipt_timestamps_follow_feed_order() -> None:
    post = cmr.CommonModeResidualDetector()
    prime(post)
    post.feed(bbo("spot_bbo", 200.0, 100.02))
    post.feed(bbo("futures_bbo", 200.0, 100.02))
    assert post.feed(followed_up_book(200.0)) is None
    assert post.feed(bbo("deribit_quote", 200.0, 100.0)) is None
    assert post.feed(index(200.0, 100.0)) is not None

    pre = cmr.CommonModeResidualDetector()
    prime(pre)
    pre.feed(bbo("deribit_quote", 200.0, 100.0))
    pre.feed(index(200.0, 100.0))
    pre.feed(bbo("spot_bbo", 200.0, 100.02))
    pre.feed(bbo("futures_bbo", 200.0, 100.02))
    assert pre.feed(followed_up_book(200.0)) is None


def test_common_move_threshold_direction_basis_and_window_gates_fail_closed() -> None:
    source_paths = (
        ((200.0, 100.014), (210.0, 100.02)),
        ((200.0, 100.02), (210.0, 99.98)),
        ((200.0, 100.015), (210.0, 100.021)),
        ((501.0, 100.02), (502.0, 100.02)),
    )
    for spot_frame, futures_frame in source_paths:
        detector = cmr.CommonModeResidualDetector()
        prime(detector)
        detector.feed(bbo("spot_bbo", *spot_frame))
        detector.feed(bbo("futures_bbo", *futures_frame))
        detector.feed(bbo("deribit_quote", futures_frame[0] + 10.0, 100.0))
        detector.feed(index(futures_frame[0] + 20.0, 100.0))
        assert detector.feed(followed_up_book(futures_frame[0] + 30.0)) is None

    split_window = cmr.CommonModeResidualDetector()
    split_window.feed(bbo("deribit_quote", 0.0, 100.0))
    split_window.feed(index(0.0, 100.0))
    split_window.feed(book(0.0, end_ms=120_000.0))
    split_window.feed(bbo("futures_bbo", 0.0, 100.0))
    split_window.feed(bbo("spot_bbo", 400.0, 100.0))
    split_window.feed(bbo("spot_bbo", 590.0, 100.02))
    split_window.feed(bbo("futures_bbo", 600.0, 100.02))
    split_window.feed(bbo("deribit_quote", 610.0, 100.0))
    split_window.feed(index(620.0, 100.0))
    assert split_window.feed(followed_up_book(630.0)) is None


def test_stale_decision_sources_and_stale_preimpact_book_fail_closed() -> None:
    stale_decision = cmr.CommonModeResidualDetector(cmr.DetectorConfig(
        max_source_age_ms=100.0,
    ))
    prime(stale_decision)
    cross_up(stale_decision)
    quiet_benchmarks(stale_decision)
    assert stale_decision.feed(followed_up_book(350.0)) is None

    stale_anchor = cmr.CommonModeResidualDetector(cmr.DetectorConfig(
        max_source_age_ms=100.0,
    ))
    stale_anchor.feed(book(0.0))
    stale_anchor.feed(bbo("deribit_quote", 150.0, 100.0))
    stale_anchor.feed(index(150.0, 100.0))
    stale_anchor.feed(bbo("spot_bbo", 150.0, 100.0))
    stale_anchor.feed(bbo("futures_bbo", 150.0, 100.0))
    stale_anchor.feed(bbo("spot_bbo", 200.0, 100.02))
    stale_anchor.feed(bbo("futures_bbo", 210.0, 100.02))
    stale_anchor.feed(bbo("deribit_quote", 220.0, 100.0))
    stale_anchor.feed(index(230.0, 100.0))
    assert stale_anchor.feed(followed_up_book()) is None

    stale_binance = cmr.CommonModeResidualDetector(cmr.DetectorConfig(
        max_source_age_ms=100.0,
    ))
    prime(stale_binance)
    stale_binance.feed(bbo("spot_bbo", 100.0, 100.02))
    stale_binance.feed(bbo("futures_bbo", 200.0, 100.02))
    stale_binance.feed(bbo("deribit_quote", 210.0, 100.0))
    stale_binance.feed(index(220.0, 100.0))
    assert stale_binance.feed(followed_up_book(300.0)) is None


def test_preimpact_pm_book_must_precede_the_source_anchor_in_feed_order() -> None:
    detector = cmr.CommonModeResidualDetector()
    detector.feed(bbo("deribit_quote", 0.0, 100.0))
    detector.feed(index(0.0, 100.0))
    detector.feed(bbo("spot_bbo", 0.0, 100.0))
    detector.feed(bbo("futures_bbo", 0.0, 100.0))
    detector.feed(book(0.0))
    cross_up(detector)
    quiet_benchmarks(detector)

    assert detector.feed(followed_up_book()) is None


def test_remaining_time_must_be_between_30_and_240_seconds() -> None:
    for remaining_s in (29.999, 240.001):
        detector = cmr.CommonModeResidualDetector()
        prime(detector)
        cross_up(detector)
        quiet_benchmarks(detector)
        decision_ms = 240.0
        assert detector.feed(followed_up_book(
            decision_ms,
            end_ms=decision_ms + remaining_s * 1_000.0,
        )) is None


def test_exchange_timestamps_and_future_outcome_fields_are_ignored() -> None:
    detector = cmr.CommonModeResidualDetector()
    detector.feed(bbo("deribit_quote", 0.0, 100.0, source_ms=90_000.0))
    detector.feed(index(0.0, 100.0, source_ms=80_000.0))
    detector.feed(book(0.0, source_ms=70_000.0, outcome="up", winner=True))
    detector.feed(bbo("spot_bbo", 0.0, 100.0, source_ms=60_000.0))
    detector.feed(bbo("futures_bbo", 0.0, 100.0, source_ms=50_000.0))
    detector.feed(bbo("spot_bbo", 200.0, 100.02, source_ms=40_000.0))
    detector.feed(bbo("futures_bbo", 210.0, 100.02, source_ms=-40_000.0))
    detector.feed(bbo("deribit_quote", 220.0, 100.0, source_ms=-50_000.0))
    detector.feed(index(230.0, 100.0, source_ms=-60_000.0))

    signal = detector.feed(followed_up_book(
        source_ms=-70_000.0,
        outcome="down",
        winner=False,
    ))

    assert signal is not None
    assert signal.crossing_recv_ms == 210.0


def test_symbol_is_mandatory_and_event_kinds_are_normalized() -> None:
    detector = cmr.CommonModeResidualDetector()
    with pytest.raises(ValueError, match="symbol"):
        detector.feed({
            "kind": "spot_bbo",
            "recv_ms": 0.0,
            "bid": 99.995,
            "ask": 100.005,
        })
    assert detector.feed(bbo("spot_bbo", 0.0, 100.0, symbol="ETH")) is None
    assert detector.feed({
        "kind": "futures_disconnect",
        "symbol": "BTC",
        "recv_ms": 0.0,
    }) is None
