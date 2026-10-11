import math

import pytest

import mine500_execution_calibration as cal


def row(identifier, *, paper=5.0, actual=5.0, status="filled_full", pnl=0.1,
        decision=0.0, send=100.0, match=500.0, response=550.0, hold=150.0):
    return {
        "attempt_id": identifier,
        "market_id": f"m-{identifier}",
        "decision_ms": decision,
        "send_ms": send,
        "match_ms": match,
        "response_ms": response,
        "requested_shares": 5.0,
        "paper_fill_shares": paper,
        "actual_fill_shares": actual,
        "actual_status": status,
        "receipt_source": "private_order_response",
        "venue_hold_ms": hold,
        "net_pnl_per_share": pnl,
    }


def test_clock_contract_uses_measured_match_without_adding_hold_twice():
    attempt = cal.parse_attempt(row("a", decision=1000, send=1100, match=1500, response=1550))
    result = cal.calibrate([attempt])
    assert result["latency_ms"]["decision_to_match_p50"] == 500
    assert result["latency_ms"]["send_to_match_p50"] == 400
    assert result["latency_ms"]["venue_hold_is_already_in_match_clock"] is True
    with pytest.raises(ValueError, match="shorter than venue_hold"):
        cal.parse_attempt(row("b", send=100, match=200, hold=150))


@pytest.mark.parametrize(
    "updates,message",
    [
        ({"receipt_source": "paper_replay"}, "private_order_response"),
        ({"actual_status": "killed", "actual_fill_shares": 1}, "cannot contain a fill"),
        ({"actual_status": "unknown", "actual_fill_shares": 1}, "cannot be imputed"),
        ({"actual_status": "filled_partial", "actual_fill_shares": 5}, "strict partial"),
        ({"send_ms": 600}, "clocks must satisfy"),
    ],
)
def test_ambiguous_or_inconsistent_private_receipts_fail_closed(updates, message):
    value = row("a")
    value.update(updates)
    with pytest.raises(ValueError, match=message):
        cal.parse_attempt(value)


def test_unknown_is_reported_but_never_imputed_as_kill_or_fill():
    result = cal.calibrate([cal.parse_attempt(row("u", actual=0, status="unknown"))])
    assert result["known_outcomes"] == 0
    assert result["unknown_outcomes"] == 1
    assert result["paper_full_predictions_known"] == 0
    assert result["paper_full_realization_rate"] is None


def test_partial_full_kill_and_public_false_negative_are_separate():
    attempts = [
        cal.parse_attempt(row("full")),
        cal.parse_attempt(row("partial", actual=2, status="filled_partial")),
        cal.parse_attempt(row("kill", actual=0, status="killed")),
        cal.parse_attempt(row("hidden", paper=0, actual=1, status="filled_partial")),
    ]
    result = cal.calibrate(attempts)
    assert result["paper_full_predictions_known"] == 3
    assert result["actual_full_fills_among_paper_full"] == 1
    assert result["actual_any_fills_among_paper_full"] == 2
    assert result["paper_false_positive_full_count"] == 2
    assert result["paper_false_negative_any_count"] == 1
    assert result["actual_fill_shares_known"] == 8
    assert result["paper_fill_shares_known"] == 15


def test_exact_interval_and_latency_buckets_are_reported_without_fake_precision():
    attempts = [
        cal.parse_attempt(row("a", match=499)),
        cal.parse_attempt(row("b", actual=0, status="killed", match=500)),
        cal.parse_attempt(row("c", match=700, response=750)),
        cal.parse_attempt(row("d", actual=0, status="rejected", match=1000, response=1050)),
    ]
    result = cal.calibrate(attempts)
    lo, hi = result["paper_full_realization_exact_99pct_interval"]
    assert 0 < lo < 0.5 < hi < 1
    assert set(result["by_total_decision_to_match_bucket"]) == {
        "lt500ms", "500_599ms", "600_999ms", "ge1000ms"
    }
    assert result["by_total_decision_to_match_bucket"]["500_599ms"]["actual_full_rate"] == 0


def test_adverse_selection_uses_actual_shares_and_fee_inclusive_pnl_only():
    attempts = [
        cal.parse_attempt(row("loss-fill", pnl=-0.2)),
        cal.parse_attempt(row("loss-miss", actual=0, status="killed", pnl=-0.2)),
        cal.parse_attempt(row("profit-fill", pnl=0.1)),
        cal.parse_attempt(row("profit-miss", actual=0, status="killed", pnl=0.1)),
    ]
    result = cal.calibrate(attempts)["adverse_selection"]
    assert result["loss_fill_miss_profit_fill_miss"] == [1, 1, 1, 1]
    assert math.isclose(result["paper_pnl_usd"], -1.0)
    assert math.isclose(result["actual_pnl_usd"], -0.5)
    assert 0 <= result["one_sided_fisher_p_value"] <= 1


def test_duplicate_attempts_are_rejected_even_for_programmatic_callers():
    attempt = cal.parse_attempt(row("dup"))
    with pytest.raises(ValueError, match="unique"):
        cal.calibrate([attempt, attempt])
