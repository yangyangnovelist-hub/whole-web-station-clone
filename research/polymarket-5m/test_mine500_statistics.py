"""Semantic ledger checks: no fabricated OOS, fees, fills or certainty."""
import itertools
import json
from pathlib import Path
import tempfile
import unittest

import mine500_statistics as stats


BASE = 1790812800000  # 2026-10-01T00:00:00Z


def attempt(market="m1", day=0, **updates):
    row = dict(candidate_id="M500-001", latency_ms=500, market_id=market,
               decision_ms=BASE + day * 86400000, settlement_ms=BASE + day * 86400000 + 300000,
               requested_shares=5, filled_shares=5, cost_usd=2.55, fees_usd=0.05,
               status="filled_full", settled=True, payout_usd=5, net_pnl_usd=2.45)
    row.update(updates)
    return row


class Mine500StatisticsTests(unittest.TestCase):
    def test_exact_tail_matches_brute_force(self):
        probabilities = [0.1, 0.23, 0.58, 0.91]
        for wins in range(5):
            expected = 0.0
            for outcome in itertools.product((0, 1), repeat=4):
                if sum(outcome) >= wins:
                    prob = 1.0
                    for p, win in zip(probabilities, outcome):
                        prob *= p if win else 1 - p
                    expected += prob
            self.assertAlmostEqual(stats.poisson_binomial_upper_tail(probabilities, wins), expected)

    def test_fee_is_included_exactly_once(self):
        result = stats.analyze([attempt()])["results"][0]
        self.assertAlmostEqual(result["net_pnl_usd"], 2.45)
        self.assertAlmostEqual(result["fair_price_test"]["p_value"], 0.51)
        with self.assertRaisesRegex(ValueError, "all-in cost"):
            stats.analyze([attempt(net_pnl_usd=2.4)])

    def test_unsettled_inventory_is_missing_not_zero_pnl(self):
        result = stats.analyze([attempt(settled=False, settlement_ms=None, payout_usd=None, net_pnl_usd=None)])["results"][0]
        self.assertIsNone(result["net_pnl_usd"])
        self.assertEqual(result["unsettled_fills"], 1)
        self.assertAlmostEqual(result["unsettled_cost_and_fees_usd"], 2.55)
        self.assertEqual(result["fair_price_test"]["reason"], "unsettled_inventory")
        self.assertIsNone(result["additional_cost_sensitivity"][0]["settled_net_pnl_usd"])

    def test_attempt_and_send_counts_include_failure_and_partial(self):
        rows = [attempt(),
                attempt("m2", status="filled_partial", filled_shares=2, cost_usd=1.02, fees_usd=0.02,
                        payout_usd=0, net_pnl_usd=-1.02),
                attempt("m3", status="no_fill", filled_shares=0, cost_usd=0, fees_usd=0,
                        payout_usd=None, net_pnl_usd=None, settled=False),
                attempt("m4", status="rejected_cash", filled_shares=0, cost_usd=0, fees_usd=0,
                        payout_usd=None, net_pnl_usd=None, settled=False),
                attempt("m5", status="incomplete_match_horizon", filled_shares=0, cost_usd=0, fees_usd=0,
                        payout_usd=None, net_pnl_usd=None, settled=False)]
        r = stats.analyze(rows)["results"][0]
        self.assertEqual((r["attempts"], r["sends"], r["full_fills"], r["partial_fills"], r["no_fills"], r["rejected"], r["incomplete_match_horizon"]),
                         (5, 4, 1, 1, 1, 1, 1))
        self.assertEqual(r["fair_price_test"]["reason"], "incomplete_match_horizon")
        self.assertEqual(stats.analyze(rows[:2])["results"][0]["fair_price_test"]["reason"], "unequal_actual_filled_shares")

    def test_duplicate_market_is_not_independent_bernoulli(self):
        r = stats.analyze([attempt(), attempt()])["results"][0]
        self.assertEqual(r["settled_fills"], 2)
        self.assertEqual(r["settled_markets"], 1)
        self.assertEqual(r["fair_price_test"]["reason"], "multiple_settled_entries_per_market")

    def test_match_rejection_is_still_a_submitted_order(self):
        r = stats.analyze([attempt(status="rejected_fee_changed_after_decision", submitted=True,
                                  filled_shares=0, cost_usd=0, fees_usd=0,
                                  payout_usd=None, net_pnl_usd=None, settled=False)])["results"][0]
        self.assertEqual((r["sends"], r["rejected_after_send"], r["rejected_before_send"], r["no_fills"]), (1, 1, 0, 0))

    def test_partially_settled_run_never_reports_complete_total(self):
        r = stats.analyze([attempt("a"), attempt("b", settled=False, payout_usd=None, net_pnl_usd=None)])["results"][0]
        self.assertIsNone(r["net_pnl_usd"])
        self.assertAlmostEqual(r["realized_pnl_usd"], 2.45)
        self.assertEqual(r["day_cluster_bootstrap"]["status"], "incomplete_sample")

    def test_utc_day_assignment_uses_decision_not_settlement(self):
        rows = [attempt("a", decision_ms=BASE - 1), attempt("b")]
        r = stats.analyze(rows)["results"][0]
        self.assertEqual([d["day"] for d in r["utc_days"]], ["2026-09-30", "2026-10-01"])
        self.assertEqual(r["settlement_days"], 1)
        self.assertEqual(r["settled_fill_days"], 2)

    def test_less_than_seven_days_has_no_confidence_bound(self):
        r = stats.analyze([attempt(str(i), day=i) for i in range(6)], bootstrap_reps=100)["results"][0]
        self.assertEqual(r["day_cluster_bootstrap"]["status"], "insufficient_days")
        self.assertIsNone(r["day_cluster_bootstrap"]["lower_bound_net_usd_per_share"])

    def test_bootstrap_whole_days_and_weighted_ratio(self):
        days = [{"settled_shares": n, "net_pnl_usd": n * 0.25} for n in range(1, 8)]
        got = stats.day_cluster_bootstrap(days, reps=100, seed=9)
        self.assertAlmostEqual(got["lower_bound_net_usd_per_share"], 0.25)
        self.assertEqual(got, stats.day_cluster_bootstrap(days, reps=100, seed=9))

    def test_all_development_remains_unqualified_even_with_winning_days(self):
        payload = {"attempts": [attempt(str(i), day=i) for i in range(30)],
                   "dataset": {"split": "oos"}, "split_validation": {"valid": True}}
        report = stats.analyze(payload, family_tests=2, bootstrap_reps=100)
        self.assertEqual(report["bonferroni_tests"], 500)
        self.assertEqual(report["qualified_strategy_count"], 0)
        self.assertFalse(report["results"][0]["qualified"])
        self.assertFalse(report["results"][0]["oos_verified"])

    def test_cost_stress_and_capacity_not_extrapolated(self):
        report = stats.analyze([attempt()])
        r = report["results"][0]
        self.assertAlmostEqual(r["additional_cost_sensitivity"][0]["settled_net_pnl_usd"], 2.4375)
        self.assertEqual([x["status"] for x in report["capacity_runs"][0]["sizes"]],
                         ["separately_replayed"])
        self.assertEqual([x["requested_shares"] for x in report["capacity_runs"][0]["sizes"]], [5])
        self.assertIn("other independent batch size files", report["capacity_runs"][0]["scope"])

    def test_legal_size_runs_are_read_from_input_not_depth_extrapolated(self):
        rows = [attempt(), attempt(requested_shares=10, filled_shares=10,
                                  cost_usd=5.10, fees_usd=0.1, payout_usd=10, net_pnl_usd=4.9),
                attempt(requested_shares=20, filled_shares=20,
                        cost_usd=10.20, fees_usd=0.2, payout_usd=20, net_pnl_usd=9.8)]
        report = stats.analyze(rows)
        self.assertEqual([s["requested_shares"] for s in report["capacity_runs"][0]["sizes"]], [5, 10, 20])
        self.assertEqual([r["is_primary_test"] for r in report["results"]], [True, False, False])

    def test_latency_and_size_results_are_separate_and_tests_count_sizes(self):
        rows = [attempt(), attempt(latency_ms=1000),
                attempt(requested_shares=1, filled_shares=1, cost_usd=0.51, fees_usd=0.01, payout_usd=1, net_pnl_usd=0.49)]
        report = stats.analyze(rows)
        self.assertEqual(len(report["results"]), 3)
        self.assertNotIn("net_pnl_usd", report)
        self.assertEqual(report["bonferroni_tests"], 1000)
        self.assertEqual(sum(r["latency_role"] == "stress_only" for r in report["results"]), 1)

    def test_runner_cash_is_preserved_and_pnl_mismatch_rejected(self):
        summary = dict(candidate_id="M500-001", latency_ms=500, requested_shares=5,
                       initial_cash_usd=1000, ending_cash_usd=1002.45,
                       min_available_cash_usd=997.45, max_realized_drawdown_usd=0, net_pnl_usd=2.45)
        payload = {"attempts": [attempt()], "summary": [summary]}
        result = stats.analyze(payload)["results"][0]
        self.assertEqual(result["cash_ledger"]["initial_cash_usd"], 1000)
        self.assertFalse(result["cash_ledger"]["mark_to_market_drawdown_available"])
        summary["net_pnl_usd"] = 9
        with self.assertRaisesRegex(ValueError, "runner/attempt"):
            stats.analyze(payload)

    def test_runner_incomplete_inputs_does_not_become_positive_strategy(self):
        payload = {"attempts": [attempt()], "summary": [{"candidate_id": "M500-001", "latency_ms": 500,
                   "requested_shares": 5, "status": "incomplete_missing_execution_inputs", "net_pnl_usd": None,
                   "realized_pnl_usd": 2.45}]}
        r = stats.analyze(payload)["results"][0]
        self.assertIsNone(r["net_pnl_usd"])
        self.assertAlmostEqual(r["realized_pnl_usd"], 2.45)
        self.assertEqual(r["fair_price_test"]["status"], "not_applicable")

    def test_denominator_does_not_hide_missing_feature_rules(self):
        rows = [dict(candidate_id="a", latency_ms=500, requested_shares=5,
                     status="blocked_no_valid_feature_rows", valid_feature_rows=0,
                     invalid_decision_reasons={"missing_or_invalid_feature:flow": 10}),
                dict(candidate_id="b", latency_ms=500, requested_shares=5,
                     status="simulated_complete", valid_feature_rows=3, attempts=1,
                     submitted_orders=1, filled_shares=5, settled_positions=1, net_pnl_usd=2)]
        d = stats.denominator_audit(rows)[0]
        self.assertEqual(d["fully_blocked_missing_or_invalid_features"], 1)
        self.assertEqual(d["evaluable_with_valid_features"], 1)
        self.assertEqual(d["missing_runner_summaries"], 498)
        self.assertEqual(d["rules_with_positive_complete_net_pnl"], 1)
        self.assertEqual(d["confirmed_strategies"], 0)

    def test_invalid_ledger_fails_closed(self):
        for changes in [dict(filled_shares=6), dict(sent=False), dict(cost_usd=float("nan")),
                        dict(settlement_ms=BASE - 1), dict(requested_shares=0),
                        dict(payout_usd=6, net_pnl_usd=3.45)]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                stats.analyze([attempt(**changes)])

    def test_cli_writes_json_and_markdown_and_empty_has_missing_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "attempts.json"
            out = Path(tmp) / "statistics.json"
            source.write_text(json.dumps({"attempts": [attempt()]}))
            self.assertEqual(stats.main(["--attempts", str(source), "--out", str(out)]), 0)
            self.assertEqual(json.loads(out.read_text())["input_attempts"], 1)
            self.assertIn("Development evidence only", out.with_suffix(".md").read_text())
        empty = stats.analyze([])
        self.assertEqual(empty["results"], [])
        self.assertEqual(empty["qualified_strategy_count"], 0)


if __name__ == "__main__":
    unittest.main()
