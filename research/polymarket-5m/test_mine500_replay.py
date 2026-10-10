"""Adversarial accounting and receipt-causality checks; no market PnL evidence."""
import unittest

from mine500_replay import ReplayConfig, replay_events


CANDIDATE = {"id": "test", "required_features": ["alpha", "tau_s"]}


def always_up(candidate, features):
    return "Up" if features["alpha"] > 0 else None


def header():
    return {"type": "dataset", "recv_ms": 0, "dataset_id": "synthetic-test-only",
            "split": "development", "provenance": {"source": "synthetic_fixture"}}


def market(mid="A", start=0):
    return {"type": "market", "recv_ms": start, "market_id": mid,
            "start_ms": start, "end_ms": start + 300000, "label_source": "polymarket"}


def book(t, asks=None, mid="A", **kw):
    value = {"type": "book", "recv_ms": t, "market_id": mid,
             "up_asks": asks if asks is not None else [[0.5, 10]],
             "down_asks": [[0.5, 10]], "healthy": True, "fee_rate": 0.07}
    value.update(kw)
    return value


def decision(t, mid="A", start=0, **kw):
    value = {"type": "decision", "recv_ms": t, "market_id": mid,
             "features": {"alpha": 1, "tau_s": (start + 300000 - t) / 1000},
             "feature_available_ms": {"alpha": t, "tau_s": t}}
    value.update(kw)
    return value


def outcome(t=300000, mid="A", winner="Up", resolved=300000, source="polymarket"):
    return {"type": "outcome", "recv_ms": t, "market_id": mid,
             "resolved_ms": resolved, "winner": winner, "source": source}


def replay(events, **kw):
    config = ReplayConfig(**({"latencies_ms": (500,)} | kw))
    return replay_events(events, [CANDIDATE], always_up, config)


class ReplayTests(unittest.TestCase):
    def test_future_feature_is_never_evaluated(self):
        d = decision(1000)
        d["feature_available_ms"]["alpha"] = 1001
        result = replay([header(), market(), book(1000), d, book(1500), outcome()])
        self.assertEqual(result["attempts"], [])
        self.assertIsNone(result["summary"][0]["net_pnl_usd"])
        self.assertEqual(result["summary"][0]["invalid_decision_reasons"], {"future_feature:alpha": 1})

    def test_unregistered_extra_feature_is_not_exposed(self):
        d = decision(1000)
        d["features"]["secret_future"] = 99
        def signal(candidate, features):
            self.assertNotIn("secret_future", features)
            return "Up"
        result = replay_events([header(), market(), book(1000), d, book(1500), outcome()],
                               [CANDIDATE], signal, ReplayConfig(latencies_ms=(500,)))
        self.assertEqual(result["summary"][0]["full_fills"], 1)

    def test_missing_timestamp_fails_closed(self):
        d = decision(1000)
        del d["feature_available_ms"]["alpha"]
        result = replay([header(), market(), book(1000), d, book(1500), outcome()])
        self.assertEqual(result["attempts"], [])
        self.assertIn("missing_feature_availability:alpha", result["summary"][0]["invalid_decision_reasons"])

    def test_first_failed_signal_cannot_select_next_signal(self):
        result = replay([header(), market(), book(1000), decision(1000),
                         book(1500, [[0.6, 10]]), book(2000), decision(2000), book(2500), outcome()])
        self.assertEqual(len(result["attempts"]), 1)
        self.assertEqual(result["attempts"][0]["status"], "no_fill")

    def test_invalid_decision_book_also_consumes_first_signal(self):
        result = replay([header(), market(), book(1000, healthy=False), decision(1000),
                         book(2000), decision(2000), book(2500), outcome()])
        self.assertEqual(len(result["attempts"]), 1)
        self.assertEqual(result["attempts"][0]["status"], "rejected_unhealthy_book_at_decision")

    def test_partial_fak_and_price_dependent_fees(self):
        result = replay([header(), market(), book(1000), decision(1000),
                         book(1500, [[0.4, 1], [0.5, 1], [0.6, 10]]), outcome()])
        a = result["attempts"][0]
        fee = 0.07 * (0.4 * 0.6 + 0.5 * 0.5)
        self.assertEqual(a["status"], "filled_partial")
        self.assertEqual(a["filled_shares"], 2)
        self.assertAlmostEqual(a["fees_usd"], fee)
        self.assertAlmostEqual(a["cost_usd"], 0.9 + fee)
        self.assertAlmostEqual(a["net_pnl_usd"], 1.1 - fee)
        self.assertAlmostEqual(result["summary"][0]["ending_cash_usd"], 1001.1 - fee)

    def test_each_latency_uses_actual_match_clock(self):
        result = replay([header(), market(), book(1000), decision(1000),
                         book(1500), book(1600, [[0.6, 10]]), book(2000, [[0.5, 2]]), outcome()],
                        latencies_ms=(500, 600, 1000))
        statuses = {a["latency_ms"]: a["status"] for a in result["attempts"]}
        self.assertEqual(statuses, {500: "filled_full", 600: "no_fill", 1000: "filled_partial"})

    def test_quote_after_match_cannot_be_used(self):
        result = replay([header(), market(), book(1000), decision(1000), book(1501), outcome()])
        self.assertEqual(result["attempts"][0]["status"], "rejected_stale_book_at_match")

    def test_new_invalid_book_never_resurrects_old_valid_depth(self):
        result = replay([header(), market(), book(1000), decision(1000),
                         book(1400), book(1499, healthy=False), outcome()])
        self.assertEqual(result["attempts"][0]["status"], "rejected_unhealthy_book_at_match")

    def test_one_side_updates_do_not_refresh_other_side(self):
        result = replay([header(), market(), book(1000), decision(1000),
                         book(1500, up_recv_ms=1500, down_recv_ms=1100), outcome()])
        self.assertEqual(result["attempts"][0]["status"], "rejected_stale_down_book_at_match")

    def test_fee_metadata_missing_fails_closed(self):
        b = book(1500)
        del b["fee_rate"]
        result = replay([header(), market(), book(1000), decision(1000), b, outcome()])
        self.assertEqual(result["attempts"][0]["status"], "rejected_missing_or_invalid_fee_at_match")
        self.assertEqual(result["summary"][0]["status"], "incomplete_missing_execution_inputs")
        self.assertIsNone(result["summary"][0]["net_pnl_usd"])

    def test_missing_decision_book_is_not_evaluated_zero_pnl(self):
        result = replay([header(), market(), decision(1000), book(1500), outcome()])
        self.assertEqual(result["attempts"][0]["status"], "rejected_missing_book_at_decision")
        self.assertEqual(result["summary"][0]["status"], "incomplete_missing_execution_inputs")
        self.assertIsNone(result["summary"][0]["net_pnl_usd"])

    def test_cash_is_not_reused_until_outcome_receipt(self):
        events = [header(), market(), book(1000), decision(1000), book(1500),
                  market("B", 300000), book(301000, mid="B"), decision(301000, mid="B", start=300000),
                  outcome(302000), book(302001, mid="B"), decision(302001, mid="B", start=300000),
                  outcome(600000, mid="B", resolved=600000)]
        result = replay(events, initial_cash_usd=3)
        self.assertEqual(len(result["attempts"]), 2)
        self.assertEqual(result["attempts"][1]["status"], "rejected_insufficient_available_cash")
        self.assertEqual(result["attempts"][0]["settlement_ms"], 302000)

    def test_cash_can_be_reused_after_outcome_receipt(self):
        events = [header(), market(), book(1000), decision(1000), book(1500), outcome(),
                  market("B", 300000), book(301000, mid="B"), decision(301000, mid="B", start=300000),
                  book(301500, mid="B"), outcome(600000, mid="B", resolved=600000)]
        result = replay(events, initial_cash_usd=3)
        self.assertTrue(all(a["settled"] for a in result["attempts"]))
        self.assertEqual(result["summary"][0]["full_fills"], 2)

    def test_pending_orders_reserve_cash(self):
        events = [header(), market(), market("B"), book(1000), book(1000, mid="B"),
                  decision(1000), decision(1000, mid="B"), book(1500), book(1500, mid="B"),
                  outcome(), outcome(mid="B")]
        result = replay(events, initial_cash_usd=3)
        self.assertEqual(result["attempts"][1]["status"], "rejected_insufficient_available_cash")

    def test_unofficial_label_never_settles_inventory(self):
        result = replay([header(), market(), book(1000), decision(1000), book(1500),
                         outcome(source="binance_ohlc")])
        self.assertFalse(result["attempts"][0]["settled"])
        self.assertIsNone(result["summary"][0]["net_pnl_usd"])

    def test_data_tail_does_not_fake_no_fill(self):
        result = replay([header(), market(), book(1000), decision(1000)])
        self.assertEqual(result["attempts"][0]["status"], "incomplete_match_horizon")
        self.assertIsNone(result["summary"][0]["net_pnl_usd"])

    def test_late_outcome_does_not_extend_book_coverage(self):
        h = header()
        h["coverage_end_ms"] = 1200
        result = replay([h, market(), book(1000), decision(1000), outcome()])
        self.assertEqual(result["attempts"][0]["status"], "incomplete_match_horizon")
        self.assertIsNone(result["summary"][0]["net_pnl_usd"])

    def test_missing_input_has_null_pnl(self):
        result = replay([])
        self.assertIsNone(result["summary"][0]["net_pnl_usd"])
        self.assertIsNone(result["summary"][0]["realized_pnl_usd"])

    def test_unsorted_receipts_raise(self):
        with self.assertRaisesRegex(ValueError, "receipt time"):
            replay([header(), market(), book(1001), decision(1000)])

    def test_unfrozen_holdout_is_development(self):
        h = header()
        h["split"] = "holdout"
        result = replay([h, market(), book(1000), decision(1000), book(1500), outcome()])
        self.assertEqual(result["split_validation"]["effective_split"], "development")
        self.assertIn("missing_or_late_freeze", result["split_validation"]["reasons"])

    def test_projection_rejects_unrepresented_latency(self):
        h = header()
        h.update(book_resolution="decision_and_match_projection", supported_latencies_ms=[500, 600, 1000], decision_grid_ms=1000)
        with self.assertRaisesRegex(ValueError, "requested total latency"):
            replay([h], latencies_ms=(400,))

    def test_market_minimum_order_size_is_enforced(self):
        m = market()
        m["min_order_size"] = 5
        result = replay([header(), m, book(1000), decision(1000), book(1500), outcome()], shares=2)
        self.assertEqual(result["attempts"][0]["status"], "rejected_below_market_min_order_size")
        self.assertFalse(result["attempts"][0]["submitted"])
        self.assertIsNone(result["summary"][0]["net_pnl_usd"])

    def test_projection_requires_original_side_clocks(self):
        h = header()
        h.update(book_resolution="decision_and_match_projection", supported_latencies_ms=[500], decision_grid_ms=1000)
        with self.assertRaisesRegex(ValueError, "original side receipt"):
            replay([h, market(), book(1000)])

    def test_projection_rejects_different_decision_grid(self):
        h = header()
        h.update(book_resolution="decision_and_match_projection", supported_latencies_ms=[500], decision_grid_ms=1000)
        with self.assertRaisesRegex(ValueError, "projection grid"):
            replay([h, market(), decision(1100)])


if __name__ == "__main__":
    unittest.main()
