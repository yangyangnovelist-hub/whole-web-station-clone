import numpy as np
import pytest

import binary as bo

SIG = float(bo.per_second_vol(0.45))


def walks(n, steps, seed):
    rng = np.random.default_rng(seed)
    return np.cumsum(rng.standard_normal((n, steps)) * SIG, axis=1)


@pytest.mark.parametrize("t", [0, 120, 240, 255, 270, 299])
def test_twap_std_matches_monte_carlo(t):
    tau = bo.WINDOW_S - t
    x = walks(200_000, tau, seed=t)
    m = int(bo.future_samples(t))
    # Future settlement samples are the last m steps; x_t is 0 so known_sum drops out.
    avg_future = x[:, tau - m:].sum(axis=1) / bo.TWAP_S
    assert np.std(avg_future) == pytest.approx(SIG * bo.twap_std_factor(t), rel=0.01)


def test_prob_up_matches_monte_carlo_inside_twap_window():
    t, x_t, strike = 270, 1.0 * SIG, 0.0
    known_sum = 30 * (-2.0 * SIG)  # first half of the window sat below the strike
    x = x_t + walks(200_000, bo.WINDOW_S - t, seed=7)
    settle = (known_sum + x.sum(axis=1)) / bo.TWAP_S
    mc = np.mean(settle >= strike)
    assert bo.prob_up(x_t, strike, SIG, t, known_sum) == pytest.approx(mc, abs=0.005)


def test_twap_differs_from_european_inside_window():
    # 30s left, spot above strike but the window so far averaged below it.
    t, x_t = 270, 2.5 * SIG
    known_sum = 30 * (-5.0 * SIG)
    assert bo.prob_up_european(x_t, 0.0, SIG, 30) > 0.6
    assert bo.prob_up(x_t, 0.0, SIG, t, known_sum) < 0.3


@pytest.mark.parametrize("t", [60, 240, 280])
def test_implied_vol_round_trip(t):
    x_t, known_sum = 3.0 * SIG, 0.0 if t <= 240 else (t - 240) * 2.0 * SIG
    for true_sigma in (0.5 * SIG, SIG, 2.0 * SIG):
        p = bo.prob_up(x_t, 0.0, true_sigma, t, known_sum)
        assert bo.implied_vol_s(p, x_t, 0.0, t, known_sum) == pytest.approx(true_sigma, rel=1e-9)


def test_implied_vol_undefined_at_the_money_or_wrong_side():
    assert np.isnan(bo.implied_vol_s(0.5, 0.0, 0.0, 100))
    assert np.isnan(bo.implied_vol_s(0.3, 1e-4, 0.0, 100))  # above strike but priced < 0.5


def test_fee_curve():
    assert bo.taker_fee(0.5) == pytest.approx(0.0175)
    assert bo.taker_fee(0.9) == pytest.approx(bo.taker_fee(0.1))
    assert bo.taker_fee(0.99) == pytest.approx(0.000693)


def test_quote_book():
    bid, ask = bo.quote_book(np.array([0.503, 0.5, 0.995, 0.002]))
    np.testing.assert_allclose(bid, [0.50, 0.50, 0.99, 0.0])
    np.testing.assert_allclose(ask, [0.51, 0.51, 1.00, 0.01])


def test_kelly():
    assert bo.kelly_fraction(0.5, 0.5) == 0.0
    cost = 0.9 + bo.taker_fee(0.9)
    assert bo.kelly_fraction(0.95, 0.9) == pytest.approx((0.95 - cost) / (1 - cost))


def test_binary_and_equal_size_insurance_payoffs_include_both_taker_fees():
    assert bo.long_binary_pnl(True, 0.40, shares=2) == pytest.approx(
        2 * (1 - 0.40 - bo.taker_fee(0.40))
    )
    assert bo.long_binary_pnl(False, 0.40, shares=2) == pytest.approx(
        -2 * (0.40 + bo.taker_fee(0.40))
    )

    expected_lock = 1 - 0.40 - bo.taker_fee(0.40) - 0.55 - bo.taker_fee(0.55)
    assert bo.paired_lock_pnl(0.40, 0.55) == pytest.approx(expected_lock)
    assert bo.can_lock_profit(0.40, 0.55) is bool(expected_lock > 0)


def test_routine_opposite_side_insurance_is_rejected_when_all_in_cost_reaches_one():
    assert bo.paired_lock_pnl(0.50, 0.50) < 0
    assert bo.can_lock_profit(0.50, 0.50) is False
