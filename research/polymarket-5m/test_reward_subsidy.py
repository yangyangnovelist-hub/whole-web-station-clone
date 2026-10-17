import pytest
import reward_subsidy as subsidy

NOW = 1_791_675_000


def _empty_check(index=0):
    return subsidy.RewardCheck(
        start_s=NOW + 300 * index,
        condition_id=f"condition-{index}",
        slug=f"btc-updown-5m-{NOW + 300 * index}",
        min_order_size=5.0,
        min_tick_size=0.01,
        native_active_config_count=0,
        sponsored_active_config_count=0,
    )


def _reward_page(condition_id, configs=None):
    if configs is None:
        return {"limit": 100, "count": 0, "next_cursor": "LTE=", "data": []}
    return {
        "limit": 100,
        "count": 1,
        "next_cursor": "LTE=",
        "data": [{"condition_id": condition_id, "rewards_config": configs}],
    }


@pytest.mark.parametrize("price", [0.05, 0.50, 0.95])
def test_crypto_fee_per_weighted_volume_is_price_invariant_before_rounding(price):
    shares = 1_000.0

    fee = subsidy.taker_fee(shares, price)
    weighted = subsidy.weighted_volume(shares, price)

    assert fee / weighted == pytest.approx(0.07 / 2.3)


def test_fee_quantum_zeros_a_real_two_decimal_partial_fill():
    floor = subsidy.rounded_fee_floor(
        price_tick=0.01,
        size_increment=0.01,
        min_order_size=5.0,
    )

    assert floor.price == pytest.approx(0.01)
    assert floor.fill_shares == pytest.approx(0.01)
    assert floor.raw_fee == pytest.approx(0.00000693)
    assert floor.rounded_fee == 0.0
    assert floor.fee_per_wv == 0.0
    assert floor.fee_per_wv < 0.07 / 2.3


def test_zero_fee_quantum_prevents_deterministic_subsidy_rejection():
    fee_floor = subsidy.rounded_fee_floor(0.01, 0.01, 5.0).fee_per_wv
    rows = subsidy.tier_economics(fee_floor)

    assert [row.name for row in rows] == [
        "Bronze", "Silver", "Gold", "Platinum", "Diamond", "Obsidian"
    ]
    assert rows[0].gross_fee_floor == 0.0
    assert rows[0].cumulative_bonus == 10.0
    assert rows[0].subsidy_net == 10.0
    assert rows[-1].cumulative_bonus == 34_310.0
    assert all(row.subsidy_net > 0 for row in rows)
    assert all(row.residual_fee_per_wv == 0 for row in rows)


def test_prior_tier_rebate_reduces_only_the_next_qualification_tranche():
    fee_floor = 0.02
    bronze, silver = subsidy.tier_economics(fee_floor)[:2]

    assert bronze.gross_fee_floor == pytest.approx(2_000 * fee_floor)
    assert silver.gross_fee_floor == pytest.approx(
        2_000 * fee_floor + 18_000 * fee_floor * (1.0 - 0.03)
    )
    assert silver.residual_fee_per_wv == pytest.approx(fee_floor * (1.0 - 0.08))


def test_fetches_native_and_sponsored_views_for_four_btc5m_markets():
    calls = []

    def fake_fetch(url):
        calls.append(url)
        if "gamma-api" in url:
            stamp = url.rsplit("-", 1)[-1]
            return [{
                "conditionId": f"condition-{stamp}",
                "slug": f"btc-updown-5m-{stamp}",
                "orderMinSize": 5,
                "orderPriceMinTickSize": 0.01,
            }]
        condition_id = url.split("/rewards/markets/", 1)[1].split("?", 1)[0]
        return _reward_page(condition_id)

    checks = subsidy.fetch_btc5m_reward_checks(NOW, fetch_json=fake_fetch)

    assert len(checks) == 4
    assert [row.start_s for row in checks] == [
        NOW, NOW + 300, NOW + 600, NOW + 900
    ]
    assert all(row.native_active_config_count == 0 for row in checks)
    assert all(row.sponsored_active_config_count == 0 for row in checks)
    assert all(row.min_order_size == 5.0 and row.min_tick_size == 0.01 for row in checks)
    assert len(calls) == 12
    assert sum("sponsored=true" in call for call in calls) == 4


def test_sponsored_active_reward_prevents_rejection():
    checks = [_empty_check(i) for i in range(4)]
    checks[-1] = subsidy.RewardCheck(
        start_s=checks[-1].start_s,
        condition_id=checks[-1].condition_id,
        slug=checks[-1].slug,
        min_order_size=5.0,
        min_tick_size=0.01,
        native_active_config_count=0,
        sponsored_active_config_count=1,
    )

    report = subsidy.render_report("2026-10-10T23:38:29Z", checks)

    assert "INCOMPLETE" in report


def test_fetch_propagates_an_active_sponsored_configuration():
    def fake_fetch(url):
        if "gamma-api" in url:
            stamp = url.rsplit("-", 1)[-1]
            return [{
                "conditionId": f"condition-{stamp}",
                "slug": f"btc-updown-5m-{stamp}",
                "orderMinSize": 5,
                "orderPriceMinTickSize": 0.01,
            }]
        condition_id = url.split("/rewards/markets/", 1)[1].split("?", 1)[0]
        if "sponsored=true" in url and condition_id.endswith(str(NOW + 900)):
            return _reward_page(condition_id, [{
                "id": 7,
                "start_date": "2026-10-01",
                "end_date": "2026-10-31",
                "rate_per_day": 4.0,
            }])
        return _reward_page(condition_id)

    checks = subsidy.fetch_btc5m_reward_checks(NOW, fetch_json=fake_fetch)

    assert checks[-1].native_active_config_count == 0
    assert checks[-1].sponsored_active_config_count == 1


def test_raw_reward_parser_counts_only_active_nested_configs():
    condition_id = "condition-1"
    configs = [
        {
            "id": 1,
            "start_date": "2026-10-01",
            "end_date": "2026-10-31",
            "rate_per_day": 2.0,
        },
        {
            "id": 2,
            "start_date": "2026-11-01",
            "end_date": "2026-11-30",
            "rate_per_day": 10.0,
        },
    ]

    assert subsidy.active_reward_count(
        _reward_page(condition_id, configs), condition_id, NOW
    ) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"limit": 100, "count": 0, "next_cursor": "LTE="},
        {"limit": 100, "count": 1, "next_cursor": "LTE=", "data": []},
        {"limit": 100, "count": 0, "next_cursor": "MORE", "data": []},
    ],
)
def test_reward_parser_fails_closed_on_incomplete_or_unread_pagination(payload):
    with pytest.raises((TypeError, ValueError)):
        subsidy.active_reward_count(payload, "condition-1", NOW)


def test_report_promotes_zero_fee_dust_to_capacity_audit_not_profit_claim():
    empty = [_empty_check(i) for i in range(4)]

    report = subsidy.render_report("2026-10-10T23:38:29Z", empty)

    assert "INCOMPLETE" in report
    assert "$0.00" in report and "$10.00" in report
    assert "3%" in report and "50%" in report
    assert "maker rebate was already tested in Family 24" in report
    assert "zero-fee dust-fill candidate" in report
    assert "not evidence of executable capacity" in report
    assert "get-raw-rewards-for-a-specific-market" in report
