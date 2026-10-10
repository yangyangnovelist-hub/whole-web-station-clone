import json
import math

import pytest

import dvol_repricing as dr


def _frame(receive_ns: int, volatility: float, *, channel: str = "deribit_volatility_index.btc_usd") -> str:
    payload = {
        "jsonrpc": "2.0",
        "method": "subscription",
        "params": {
            "channel": channel,
            "data": {"timestamp": receive_ns // 1_000_000 - 25, "volatility": volatility},
        },
    }
    return f"{receive_ns}\t{json.dumps(payload)}\n"


def test_parse_dvol_line_uses_local_receipt_clock_and_filters_other_channels():
    point = dr.parse_dvol_line(_frame(1_234_567_890_000_000_000, 37.21))
    assert point == dr.DvolPoint(
        receive_ms=1_234_567_890_000.0,
        volatility=37.21,
        source_ms=1_234_567_889_975.0,
    )
    assert dr.parse_dvol_line(_frame(1_234_567_890_000_000_000, 37.21, channel="ticker.BTC-PERPETUAL.agg2")) is None
    assert dr.parse_dvol_line("not a recorder row\n") is None


@pytest.mark.parametrize("prior", [0.01, 0.10, 0.25, 0.50, 0.75, 0.90, 0.99])
def test_volatility_increase_moves_probability_toward_half(prior):
    moved = dr.reprice_probability(prior, old_volatility=40.0, new_volatility=44.0)
    assert abs(moved - 0.5) <= abs(prior - 0.5) + 1e-15
    if prior < 0.5:
        assert moved >= prior
    elif prior > 0.5:
        assert moved <= prior
    else:
        assert moved == pytest.approx(0.5)


@pytest.mark.parametrize("ratio", [0.5, 36.66 / 37.21, 37.21 / 36.66, 2.0])
def test_exact_shift_bound_dominates_dense_probability_grid(ratio):
    bound = dr.maximum_probability_shift(ratio)
    dense = max(
        abs(dr.reprice_probability(i / 100_000, 1.0, ratio) - i / 100_000)
        for i in range(1, 100_000)
    )
    assert bound >= dense - 1e-9
    assert bound == pytest.approx(dense, abs=2e-7)


def test_horizon_audit_is_causal_and_reports_structural_hurdle():
    points = [
        dr.DvolPoint(0.0, 40.0),
        dr.DvolPoint(1_000.0, 40.1),
        dr.DvolPoint(2_000.0, 39.8),
        dr.DvolPoint(5_000.0, 40.4),
    ]
    row = dr.audit_horizon(points, horizon_ms=2_000.0)
    assert row.observations == 2
    assert row.max_absolute_points == pytest.approx(0.6)
    assert row.max_relative_move == pytest.approx(abs(40.4 / 39.8 - 1.0))
    assert row.max_probability_shift == pytest.approx(
        dr.maximum_probability_shift(40.4 / 39.8)
    )
    assert not row.crosses_one_cent_tick


def test_invalid_volatility_or_probability_fails_closed():
    for args in ((0.0, 40.0, 41.0), (1.0, 40.0, 41.0), (0.5, 0.0, 41.0), (0.5, 40.0, math.nan)):
        with pytest.raises(ValueError):
            dr.reprice_probability(*args)
