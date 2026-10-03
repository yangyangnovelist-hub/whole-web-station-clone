import numpy as np
import pandas as pd
import pytest

import crossvenue120 as cv


def ms(value):
    return int(pd.Timestamp(value, tz="UTC").timestamp() * 1000)


def test_historical_taker_hold_boundaries():
    aug = ms("2026-08-17 14:00:00")
    sep = ms("2026-09-04 14:00:00")
    assert cv.taker_hold_ms(aug - 1) == 250
    assert cv.taker_hold_ms(aug) == 50
    assert cv.taker_hold_ms(sep - 1) == 50
    assert cv.taker_hold_ms(sep) == 150


def test_forward_jump_label_is_strictly_after_decision_and_within_120ms():
    trades = pd.DataFrame(
        {
            "recv_ts_ms": [900, 1000, 1119, 1121, 1240],
            "price": [100.0, 101.0, 102.0, 103.0, 98.0],
        }
    )
    # The move already visible at t=1000 cannot become the label.  A +0.99%
    # move at t+119 can; the larger print at t+121 is outside the window.
    got = cv.forward_jump_labels(
        trades, np.array([1000, 1240], dtype=np.int64), horizon_ms=120, threshold_bps=90
    )
    assert got["direction"].tolist() == [1, 0]
    assert got["label_ts_ms"].tolist()[0] == 1119
    assert np.isnan(got["label_ts_ms"].tolist()[1])
    assert (got["label_ts_ms"].dropna() > got.loc[got["label_ts_ms"].notna(), "decision_ms"]).all()


def test_causal_features_never_read_past_decision_time():
    trades = pd.DataFrame(
        {
            "recv_ts_ms": [700, 900, 999, 1001, 1099],
            "price": [100.0, 100.1, 100.2, 150.0, 160.0],
            "size": [1.0, 2.0, 3.0, 1000.0, 1000.0],
            "taker_side": ["buy", "sell", "buy", "buy", "buy"],
        }
    )
    books = pd.DataFrame(
        {
            "timestamp_ms": [800, 950, 1050],
            "market_id": ["m1"] * 3,
            "up_best_bid": [0.48, 0.49, 0.90],
            "up_best_ask": [0.51, 0.52, 0.91],
            "down_best_bid": [0.48, 0.47, 0.08],
            "down_best_ask": [0.51, 0.50, 0.09],
            "up_bid_size": [10.0, 12.0, 999.0],
            "up_ask_size": [11.0, 13.0, 999.0],
            "down_bid_size": [14.0, 15.0, 999.0],
            "down_ask_size": [16.0, 17.0, 999.0],
        }
    )
    got = cv.causal_feature_rows(trades, books, np.array([1000]), market_id="m1")
    r = got.iloc[0]
    assert r.feature_max_ms == 999
    assert r.book_ts_ms == 950
    assert r.spot_price == pytest.approx(100.2)
    assert r.up_ask == pytest.approx(0.52)
    assert r.flow_100ms == pytest.approx(1.0)  # +3 buy -2 sell; future size is excluded.
    assert r.feature_max_ms <= r.decision_ms


def test_spot_candidates_use_only_arrived_cross_venue_data():
    primary = pd.DataFrame(
        {
            "recv_ts_ms": [800, 900, 980, 1000, 1050, 1119, 1121],
            "price": [100.0, 100.0, 100.001, 100.002, 100.003, 100.020, 100.030],
            "size": [1.0] * 7,
            "taker_side": ["buy", "sell", "buy", "buy", "buy", "buy", "buy"],
        }
    )
    secondary = pd.DataFrame(
        {
            "recv_ts_ms": [850, 990, 1001],
            "price": [99.0, 99.01, 120.0],
            "size": [2.0, 3.0, 999.0],
            "taker_side": ["sell", "buy", "buy"],
        }
    )
    got = cv.spot_candidate_features(
        primary,
        secondary,
        decision_times=np.array([1000], dtype=np.int64),
        gate_bps=0.0,
        jump_bps=1.0,
    )
    r = got.iloc[0]
    assert r.decision_ms == 1000
    assert r.feature_max_ms == 1000
    assert r.venue_feature_max_ms == 990
    assert r.venue_last_price == pytest.approx(99.01)
    assert r.venue_flow_250ms == pytest.approx(1.0)  # +3 buy -2 sell
    assert r.jump_direction == 1
    assert r.jump_ts_ms == 1119
    assert r.jump_lead_ms == 119


def test_taker_execution_uses_first_book_after_hold_and_respects_limit():
    decision = ms("2026-09-10 00:00:00")
    books = pd.DataFrame(
        {
            "timestamp_ms": [decision + 149, decision + 154, decision + 155, decision + 200],
            "up_best_ask": [0.40, 0.41, 0.42, 0.39],
            "down_best_ask": [0.62, 0.61, 0.60, 0.63],
            "up_ask_size": [9.0, 8.0, 7.0, 6.0],
            "down_ask_size": [9.0, 8.0, 7.0, 6.0],
        }
    )
    # 150 ms hold + 5 ms transport means the t+154 quote is not executable.
    fill = cv.simulate_taker(
        books, decision_ms=decision, side="Up", limit_price=0.42,
        won=1.0, transport_ms=5, shares=5.0,
    )
    assert fill["eligible_ms"] == decision + 155
    assert fill["match_ms"] == decision + 155
    assert fill["filled"]
    assert fill["price"] == pytest.approx(0.42)
    assert fill["shares"] == pytest.approx(5.0)
    fee = 0.07 * 0.42 * 0.58
    assert fill["pnl_per_share"] == pytest.approx(1 - 0.42 - fee)
    assert fill["pnl_usd"] == pytest.approx(5 * (1 - 0.42 - fee))

    missed = cv.simulate_taker(
        books, decision_ms=decision, side="Up", limit_price=0.41,
        won=1.0, transport_ms=5, shares=5.0,
    )
    assert not missed["filled"]
    assert np.isnan(missed["price"])
    assert missed["pnl_usd"] == 0.0


def test_market_replay_attaches_only_current_and_first_eligible_quote():
    decision = ms("2026-09-10 00:02:00")
    candidates = pd.DataFrame(
        {
            "decision_ms": [decision],
            "feature_max_ms": [decision],
            "spot_price": [100.0],
            "jump_direction": [1],
            "jump_ts_ms": [decision + 100],
        }
    )
    books = pd.DataFrame(
        {
            "timestamp_ms": [decision - 20, decision + 100, decision + 155, decision + 220],
            "market_id": ["m1"] * 4,
            "up_best_bid": [0.49, 0.55, 0.58, 0.59],
            "up_best_ask": [0.51, 0.57, 0.60, 0.61],
            "down_best_bid": [0.48, 0.42, 0.39, 0.38],
            "down_best_ask": [0.50, 0.44, 0.41, 0.40],
            "up_bid_size": [10.0] * 4,
            "up_ask_size": [11.0, 9.0, 7.0, 6.0],
            "down_bid_size": [12.0] * 4,
            "down_ask_size": [13.0] * 4,
        }
    )
    markets = pd.DataFrame(
        {
            "market_id": ["m1"],
            "start": [decision - 120_000],
            "end": [decision + 180_000],
            "horizon": [5],
            "up_won": [1.0],
        }
    )
    got = cv.attach_market_replay(candidates, books, markets, transport_ms=5)
    r = got.iloc[0]
    assert r.book_ts_ms == decision - 20
    assert r.feature_max_ms == decision
    assert r.eligible_ms == decision + 155
    assert r.match_ms == decision + 155
    assert r.up_ask0 == pytest.approx(0.51)
    assert r.match_up_ask == pytest.approx(0.60)
    assert r.up_won == 1.0
    assert r.book_ok and r.match_book_ok


def test_joint_trade_selection_is_thresholded_and_one_trade_per_market():
    candidates = pd.DataFrame(
        {
            "market_id": ["a", "a", "b", "c"],
            "decision_ms": [1000, 1100, 1000, 1000],
            "p_jump": [0.80, 0.95, 0.80, 0.79],
            "p_up_given_jump": [0.90, 0.95, 0.55, 0.99],
            "p_fill": [0.75, 0.90, 0.80, 0.99],
            "expected_edge": [0.04, 0.08, 0.04, 0.20],
        }
    )
    got = cv.select_joint_signals(
        candidates, min_jump=0.80, min_direction=0.60, min_fill=0.70, min_edge=0.03
    )
    assert got[["market_id", "decision_ms"]].values.tolist() == [["a", 1000]]


def test_market_cluster_summary_counts_markets_not_duplicate_fills():
    fills = pd.DataFrame(
        {
            "market_id": ["a", "a", "b"],
            "pnl_per_share": [0.10, 0.20, -0.10],
            "pnl_usd": [0.50, 1.00, -0.50],
        }
    )
    got = cv.cluster_summary(fills, bootstrap_reps=2000, seed=7)
    assert got["fills"] == 3
    assert got["markets"] == 2
    assert got["edge"] == pytest.approx((0.10 + 0.20 - 0.10) / 3)
    assert got["profit_usd"] == pytest.approx(1.0)
    assert got["ci_low"] <= got["edge"] <= got["ci_high"]
