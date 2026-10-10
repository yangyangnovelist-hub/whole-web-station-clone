import unittest
from mine500_fragility import sensitivity, signature, aggregate, analyse_document


def fill(m, payout, cost, shares=5, fee=.1):
    return dict(market_id=m, decision_ms=10, filled_shares=shares,
                payout_usd=payout, cost_usd=cost, net_pnl_usd=payout-cost,
                fees_usd=fee)


class FragilityTests(unittest.TestCase):
    def test_adversarial_removal_keeps_losers_and_reconciles_partial(self):
        fills = [fill('a', 5, 1), fill('b', 0, 2), fill('c', 2, .5, shares=2), fill('d', 5, 4)]
        r = sensitivity(fills, .25, .01)
        self.assertEqual(r['removed_market_ids'], ['a'])
        self.assertEqual(r['remaining_shares'], 12)
        self.assertAlmostEqual(r['net_pnl_usd'], .38)
        self.assertEqual(r['minimum_positive_orders_to_erase_profit'], 2)
        self.assertEqual(len(fills), 4)

    def test_full_miss_budget_never_deletes_losers(self):
        r = sensitivity([fill('a', 5, 1), fill('b', 0, 2)], 1, 0)
        self.assertEqual(r['missed_positive_orders'], 1)
        self.assertEqual(r['net_pnl_usd'], -2)

    def test_small_sample_floor_and_single_market_exposure(self):
        r = sensitivity([fill('a', 5, 1), fill('b', 0, 2)], .1, 0)
        self.assertEqual(r['missed_positive_orders'], 0)
        self.assertEqual(r['net_after_worst_single_market_deleted_usd'], -2)
        self.assertEqual(r['break_even_missed_fraction_of_all_fills'], .5)

    def test_cost_changes_adversarial_ranking(self):
        r = sensitivity([fill('a', 5, 4, 100), fill('b', 2, 1.1, 1)], .5, .01)
        self.assertEqual(r['removed_market_ids'], ['b'])
        self.assertAlmostEqual(r['net_pnl_usd'], 0)

    def test_loss_already_broken_and_invalid_configuration(self):
        self.assertEqual(sensitivity([fill('a', 0, 2)], .5, 0)['minimum_positive_orders_to_erase_profit'], 0)
        for fraction, cost in ((-.1, 0), (1.1, 0), (.1, -1), (.1, float('nan'))):
            with self.assertRaises(ValueError):
                sensitivity([], fraction, cost)

    def test_one_market_multiple_fills_grouped(self):
        r = sensitivity([fill('a', 5, 1), fill('a', 5, 2), fill('b', 0, 2)], 0, 0)
        self.assertEqual(r['net_after_worst_single_market_deleted_usd'], -2)

    def test_signature_includes_failures_but_ignores_candidate_id(self):
        a = fill('a', 5, 1); b = dict(a, candidate_id='other')
        self.assertEqual(signature([a]), signature([b]))
        self.assertNotEqual(signature([a]), signature([a, dict(a, filled_shares=0, status='no_fill')]))
        self.assertEqual(signature([a], True), signature([a, dict(a, filled_shares=0)], True))

    def test_refuses_holdout_inputs(self):
        with self.assertRaises(ValueError):
            analyse_document({'split_validation': {'effective_split': 'holdout'}})

    def test_empty_fills_not_profitable(self):
        r = sensitivity([], 1, .01)
        self.assertFalse(r['positive'])
        self.assertIsNone(r['break_even_missed_fraction_of_all_fills'])


if __name__ == '__main__':
    unittest.main()
