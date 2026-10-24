import json

import numpy as np
import pandas as pd
import pytest

import asian180 as a180
import binary as bo


def _book_row(src_ms, rcv_ms, bid=.39, ask=.40, bid_size=20.0, ask_size=20.0):
    row = {
        "market_id": "m1",
        "src_ms": src_ms,
        "rcv_ms": rcv_ms,
    }
    for level in range(1, 6):
        row[f"b{level}p"] = bid - .01 * (level - 1)
        row[f"b{level}s"] = bid_size
        row[f"a{level}p"] = ask + .01 * (level - 1)
        row[f"a{level}s"] = ask_size
    return row


def test_order_limit_is_fixed_on_a_tick_and_preserves_required_edge():
    limit = a180.max_profitable_limit(.70, .075)
    assert limit == pytest.approx(.60)
    assert .70 - limit - bo.taker_fee(limit) >= .075
    assert .70 - (limit + .01) - bo.taker_fee(limit + .01) < .075


def test_spot_snapshot_cannot_see_prices_after_the_receive_cutoff():
    seconds = np.arange(1_000, 1_801)
    prices = 50_000 * np.exp(np.linspace(0, .01, len(seconds)))
    spot = pd.DataFrame({"T_us": seconds * 1_000_000 + 100_000, "price": prices})
    state = a180.SpotHistory(spot)
    first = state.snapshot(start_ts=1_600, decision_ms=1_720_000, receive_delay_ms=110)

    future = pd.concat([
        spot,
        pd.DataFrame({"T_us": [1_719_950_000, 1_720_010_000], "price": [1_000_000, 1.0]}),
    ]).sort_values("T_us", kind="stable")
    second = a180.SpotHistory(future).snapshot(
        start_ts=1_600, decision_ms=1_720_000, receive_delay_ms=110
    )

    assert second.cutoff_ms == 1_719_890
    assert second.current_log == pytest.approx(first.current_log)
    assert second.strike_log == pytest.approx(first.strike_log)
    assert second.sigma_s == pytest.approx(first.sigma_s)
    assert second.feature_max_ms <= second.cutoff_ms


def test_official_strike_overrides_cross_venue_opening_proxy():
    seconds = np.arange(1_000, 1_801)
    spot = pd.DataFrame({
        "T_us": seconds * 1_000_000 + 100_000,
        "price": np.linspace(49_000, 51_000, len(seconds)),
    })
    official = np.log(50_123.45)
    snapshot = a180.SpotHistory(spot).snapshot(
        start_ts=1_600,
        decision_ms=1_720_000,
        receive_delay_ms=110,
        official_strike_log=official,
    )
    assert snapshot.strike_log == pytest.approx(official)


def test_decision_book_uses_receive_order_and_drops_late_or_regressive_source_rows():
    rows = pd.DataFrame([
        _book_row(1_000, 1_010, ask=.42),
        _book_row(1_100, 1_150, ask=.43),
        _book_row(1_050, 1_160, ask=.01),  # stale source arriving out of order
        _book_row(1_200, 1_250, ask=.99),  # not received at the decision
    ])
    view = a180.received_book_at(rows, decision_ms=1_200, max_age_ms=250)
    assert view is not None
    assert view.src_ms == 1_100
    assert view.ask_up == pytest.approx(.43)


def test_persistent_taker_fill_requires_fixed_limit_and_continuous_depth():
    rows = pd.DataFrame([
        _book_row(1_100, 1_110, ask=.40, ask_size=7),
        _book_row(1_160, 1_170, ask=.41, ask_size=7),
        _book_row(1_250, 1_260, ask=.42, ask_size=7),
        _book_row(1_360, 1_370, ask=.43, ask_size=7),
    ])
    fill = a180.persistent_taker_fill(
        rows, side_up=True, fixed_limit=.42, shares=5, start_ms=1_150, end_ms=1_350
    )
    assert fill is not None
    assert fill.price == pytest.approx(.42)  # worst executable state, not the decision ask
    assert fill.shares == 5

    failed = rows.copy()
    failed.loc[2, "a1s"] = 4
    for level in range(2, 6):
        failed.loc[2, f"a{level}p"] = .50 + level / 100
    assert a180.persistent_taker_fill(
        failed, side_up=True, fixed_limit=.42, shares=5, start_ms=1_150, end_ms=1_350
    ) is None


def test_down_fill_uses_complementary_up_bids_and_real_depth():
    rows = pd.DataFrame([
        _book_row(1_100, 1_110, bid=.62, bid_size=8),
        _book_row(1_200, 1_210, bid=.61, bid_size=8),
    ])
    fill = a180.persistent_taker_fill(
        rows, side_up=False, fixed_limit=.39, shares=5, start_ms=1_150, end_ms=1_250
    )
    assert fill is not None
    assert fill.price == pytest.approx(.39)


def test_insurance_is_only_allowed_after_a_confirmed_fill_and_positive_lock():
    assert a180.profitable_lock_price(.40, .55, first_fill_confirmed=False) is None
    assert a180.profitable_lock_price(.40, .60, first_fill_confirmed=True) is None
    assert a180.profitable_lock_price(.40, .55, first_fill_confirmed=True) == pytest.approx(.55)


def test_forward_observations_reject_prefreeze_and_duplicate_markets(tmp_path):
    manifest = a180.FreezeManifest(
        strategy=a180.Asian180Spec(),
        frozen_at="2026-10-03T13:30:00Z",
        discovery_through="2026-10-02T00:00:00Z",
        code_commit="abc123",
        explored_variants=280,
    )
    path = tmp_path / "freeze.json"
    a180.write_freeze_manifest(path, manifest)
    loaded = a180.load_freeze_manifest(path)
    assert loaded.fingerprint == manifest.fingerprint
    assert json.loads(path.read_text())["fingerprint"] == manifest.fingerprint

    observations = pd.DataFrame({
        "market_id": ["old", "new", "new"],
        "decision_ms": [1_791_000_000_000, 1_792_000_000_000, 1_792_000_000_001],
    })
    with pytest.raises(ValueError, match="pre-freeze"):
        a180.validate_forward_observations(observations, loaded)

    observations = observations.iloc[1:].copy()
    with pytest.raises(ValueError, match="duplicate"):
        a180.validate_forward_observations(observations, loaded)


def test_forward_gate_requires_enough_fills_and_a_positive_lower_bound():
    profitable = pd.DataFrame({
        "market_id": [f"m{i}" for i in range(30)],
        "day": [f"d{i // 6}" for i in range(30)],
        "pnl_per_share": ([.20] * 5 + [-.10]) * 5,
    })
    gate = a180.forward_gate(profitable, alpha=.01, min_fills=30, min_days=5)
    assert gate.passed
    assert gate.lower_bound > 0

    too_few = a180.forward_gate(profitable.iloc[:29], alpha=.01, min_fills=30, min_days=5)
    assert not too_few.passed
    assert too_few.reason == "insufficient_fills"
