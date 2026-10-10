import unittest
from mine500_shared_portfolio import (first_signal_per_market, simulate_shared_cash,
                                      select_coverage_representatives, validate_document)


def attempt(cid, market, decision, side="Up", status="filled_full", submitted=True,
            filled=5, cost=2, payout=5, reserve=2.2, latency=500, settlement=1000):
    return {"candidate_id":cid,"latency_ms":latency,"market_id":market,
            "decision_ms":decision,"match_ms":decision+latency,"side":side,
            "requested_shares":5,"submitted":submitted,"status":status,
            "filled_shares":filled,"cost_usd":cost,"fees_usd":.1 if filled else 0,
            "reserved_usd_at_decision":reserve if submitted else 0,
            "settled":bool(filled),"settlement_ms":settlement if filled else None,
            "payout_usd":payout if filled else None}


class SharedPortfolioTests(unittest.TestCase):
    def test_first_signal_consumes_market_and_flags_opposite_side(self):
        rows=[attempt("B","m",20,"Down"),attempt("A","m",10,"Up")]
        chosen,diag=first_signal_per_market(rows,["A","B"],500)
        self.assertEqual(chosen[0]["candidate_id"],"A")
        self.assertEqual(diag[0]["suppressed_signals"],1)
        self.assertTrue(diag[0]["opposite_side_conflict"])

    def test_unsubmitted_first_signal_still_consumes_market(self):
        rows=[attempt("A","m",10,status="rejected_unhealthy_book_at_decision",submitted=False,filled=0,cost=0,payout=0,reserve=0),attempt("B","m",20)]
        chosen,_=first_signal_per_market(rows,["A","B"],500)
        records,summary=simulate_shared_cash(chosen)
        self.assertEqual(records[0]["candidate_id"],"A")
        self.assertEqual(summary["filled_orders"],0)

    def test_shared_cash_rejects_concurrent_reservation(self):
        rows=[attempt("A","a",10,reserve=4,cost=3,payout=5,settlement=1000),
              attempt("A","b",11,reserve=4,cost=3,payout=5,settlement=1000)]
        records,summary=simulate_shared_cash(rows,initial_cash=5)
        self.assertEqual(summary["cash_rejections"],1)
        self.assertAlmostEqual(summary["ending_cash_usd"],7)

    def test_settlement_reuse_after_known_time(self):
        rows=[attempt("A","a",10,reserve=4,cost=3,payout=5,settlement=600),
              attempt("A","b",700,reserve=4,cost=3,payout=5,settlement=1400)]
        _,summary=simulate_shared_cash(rows,initial_cash=5)
        self.assertEqual(summary["cash_rejections"],0)
        self.assertAlmostEqual(summary["ending_cash_usd"],9)

    def test_equal_timestamp_is_conservative_decision_first(self):
        rows=[attempt("A","a",10,reserve=4,cost=3,payout=5,settlement=700),
              attempt("A","b",700,reserve=4,cost=3,payout=5,settlement=1400)]
        _,summary=simulate_shared_cash(rows,initial_cash=5)
        self.assertEqual(summary["cash_rejections"],1)

    def test_extra_cost_hits_cash_and_pnl(self):
        _,summary=simulate_shared_cash([attempt("A","a",10,reserve=4,cost=3,payout=5)],initial_cash=10,extra_cost_per_share=.01)
        self.assertAlmostEqual(summary["net_pnl_usd"],1.95)
        self.assertAlmostEqual(summary["extra_costs_usd"],.05)

    def test_no_fill_releases_reservation(self):
        row=attempt("A","a",10,status="no_fill",filled=0,cost=0,payout=0)
        _,summary=simulate_shared_cash([row],initial_cash=5)
        self.assertEqual(summary["filled_orders"],0)
        self.assertEqual(summary["ending_cash_usd"],5)

    def test_coverage_selection_never_uses_pnl(self):
        catalog=[{"id":"A","family":"f"},{"id":"B","family":"f"},{"id":"C","family":"blocked"}]
        doc={"summary":[
            {"candidate_id":"A","latency_ms":500,"status":"simulated_complete","settled_positions":2,"submitted_orders":3,"net_pnl_usd":-99},
            {"candidate_id":"B","latency_ms":500,"status":"simulated_complete","settled_positions":1,"submitted_orders":4,"net_pnl_usd":99}]}
        selected=select_coverage_representatives(catalog,doc)
        self.assertEqual(selected["candidate_ids"],["A"])
        self.assertEqual(selected["blocked_families"],["blocked"])
        self.assertFalse(selected["selection_uses_pnl"])

    def test_validation_refuses_unsettled_fill(self):
        row=attempt("A","a",10); row["settled"]=False
        doc={"schema_version":"mine500_replay_v1","split_validation":{"effective_split":"development"},
             "capital_scope":"each_candidate_and_latency_has_independent_cash_do_not_sum","attempts":[row]}
        with self.assertRaises(ValueError):validate_document(doc)


if __name__ == "__main__":
    unittest.main()
