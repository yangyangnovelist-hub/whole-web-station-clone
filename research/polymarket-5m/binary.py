"""Pricing helpers for Polymarket 5-minute crypto Up/Down markets.

Since 2026-08-14 a 5m market pays 1 USDC when the 60-second Chainlink TWAP at
the close is >= the "price to beat" (the 60-second TWAP at the open), else 0.
That is an Asian (average-price) cash-or-nothing digital, not a European one.

Everything works in log-price space. Over five minutes the gap between the
arithmetic and geometric average is ~1e-7 and is ignored.
"""
from __future__ import annotations

import math

import numpy as np
from scipy.stats import norm

SECONDS_PER_YEAR = 365 * 24 * 3600
WINDOW_S = 300          # market length in seconds
TWAP_S = 60             # settlement averaging window for 5m markets
CRYPTO_FEE_RATE = 0.07  # taker feeRate for crypto markets (0.072 before 2026-07)
TICK = 0.01


def per_second_vol(annual_vol):
    return np.asarray(annual_vol, dtype=float) / math.sqrt(SECONDS_PER_YEAR)


def annual_vol(sigma_s):
    return np.asarray(sigma_s, dtype=float) * math.sqrt(SECONDS_PER_YEAR)


def taker_fee(price, rate=CRYPTO_FEE_RATE):
    """USDC fee per share taken: rate * p * (1 - p). Makers pay nothing."""
    p = np.asarray(price, dtype=float)
    return rate * p * (1.0 - p)


def long_binary_pnl(won, price, shares=1.0, rate=CRYPTO_FEE_RATE):
    """Settlement P&L for a long binary claim bought as a taker."""
    p = np.asarray(price, dtype=float)
    return np.asarray(shares, dtype=float) * (np.asarray(won, dtype=float) - p - taker_fee(p, rate))


def paired_lock_pnl(first_price, hedge_price, shares=1.0, rate=CRYPTO_FEE_RATE):
    """Outcome-independent P&L after buying equal shares of both outcomes."""
    first = np.asarray(first_price, dtype=float)
    hedge = np.asarray(hedge_price, dtype=float)
    return np.asarray(shares, dtype=float) * (
        1.0 - first - taker_fee(first, rate) - hedge - taker_fee(hedge, rate)
    )


def can_lock_profit(first_price, hedge_price, rate=CRYPTO_FEE_RATE):
    """Whether an equal-size opposite-side purchase locks strictly positive P&L."""
    result = paired_lock_pnl(first_price, hedge_price, rate=rate) > 0
    return bool(result) if np.ndim(result) == 0 else result


def future_samples(t, window=WINDOW_S, twap=TWAP_S):
    """Settlement samples (seconds window-twap+1 .. window) still unknown at second t."""
    return np.clip(window - np.asarray(t), 0, twap)


def twap_std_factor(t, window=WINDOW_S, twap=TWAP_S):
    """Std of the closing TWAP given info at second t, in units of per-second vol.

    Before the averaging window opens the unknown part is a random walk of
    `gap` steps plus the average of a fresh walk (variance ~ twap/3). Inside the
    window only the remaining m samples are unknown and the variance collapses
    like m^3 instead of the European m.
    """
    t = np.asarray(t)
    m = future_samples(t, window, twap)
    gap = np.maximum(0, window - twap - t)
    var = twap**2 * gap + m * (m + 1) * (2 * m + 1) / 6.0
    return np.sqrt(var) / twap


def twap_expected(x_t, known_sum, t, window=WINDOW_S, twap=TWAP_S):
    """Expected closing TWAP given the current log price and the sum of the
    log prices already inside the averaging window (0 before it opens)."""
    m = future_samples(t, window, twap)
    return (np.asarray(known_sum) + m * np.asarray(x_t)) / twap


def prob_up(x_t, strike, sigma_s, t, known_sum=0.0, window=WINDOW_S, twap=TWAP_S):
    """P(closing TWAP >= strike) for a driftless Gaussian random walk."""
    mean = twap_expected(x_t, known_sum, t, window, twap)
    std = np.asarray(sigma_s) * twap_std_factor(t, window, twap)
    with np.errstate(divide="ignore", invalid="ignore"):
        z = (mean - strike) / std
    return np.where(std > 0, norm.cdf(z), (mean >= strike).astype(float))


def prob_up_european(x_t, strike, sigma_s, tau):
    """Naive spot-vs-strike digital N(d), d = (x - k) / (sigma * sqrt(tau))."""
    std = np.asarray(sigma_s) * np.sqrt(np.asarray(tau, dtype=float))
    with np.errstate(divide="ignore", invalid="ignore"):
        z = (np.asarray(x_t) - strike) / std
    return np.where(std > 0, norm.cdf(z), (np.asarray(x_t) >= strike).astype(float))


def implied_vol_s(price, x_t, strike, t, known_sum=0.0, window=WINDOW_S, twap=TWAP_S):
    """Per-second vol at which prob_up equals the quoted price.

    NaN when the quote carries no vol information: at the money (price 0.5 or
    expected TWAP == strike), or when price and distance point opposite ways,
    which means the market is looking at a different oracle price than you.
    """
    mean = twap_expected(x_t, known_sum, t, window, twap)
    with np.errstate(divide="ignore", invalid="ignore"):
        sigma = (mean - strike) / (norm.ppf(price) * twap_std_factor(t, window, twap))
    return np.where(np.isfinite(sigma) & (sigma > 0), sigma, np.nan)


def quote_book(mid):
    """One-tick Up book around a model mid: bid on the tick below, ask one tick up."""
    mid = np.clip(np.asarray(mid, dtype=float), 0.0, 1.0)
    bid = np.clip(np.floor(mid / TICK + 1e-9) * TICK, 0.0, 1.0 - TICK)
    ask = np.clip(bid + TICK, TICK, 1.0)
    return bid, ask


def fair_price_pvalue(pnl, cost, sims=20000, seed=0):
    """One-sided p-value for "these trades beat fair prices".

    Null: each trade wins with probability equal to its all-in cost (price +
    fee), i.e. the market was fairly priced after fees. Simulates the total
    P&L under that null. Unlike a t-test this stays honest when every trade in
    the sample happened to win.
    """
    pnl, cost = np.asarray(pnl, float), np.asarray(cost, float)
    if pnl.size == 0:
        return 1.0
    rng = np.random.default_rng(seed)
    total = np.zeros(sims)
    for start in range(0, pnl.size, 256):
        c = cost[start:start + 256]
        total += ((rng.random((sims, c.size)) < c) - c).sum(axis=1)
    return float((total >= pnl.sum() - 1e-12).mean())


def kelly_fraction(q, price, rate=CRYPTO_FEE_RATE):
    """Bankroll fraction for buying at `price` (taker) when the true win prob is q."""
    cost = np.asarray(price) + taker_fee(price, rate)
    return np.maximum(0.0, (np.asarray(q) - cost) / (1.0 - cost))
