import numpy as np
import pandas as pd
import pytest

import cross
import cross_box_batch as box


BASE = 1_787_200_000_000
SOURCE = "https://data.chain.link/streams/btc-usd-twap-60s-streams"


def _markets(*, source_5m=SOURCE, source_15m=SOURCE):
    return pd.DataFrame({
        "market_id": ["m5", "m15"],
        "start": [BASE - 300_000, BASE - 900_000],
        "end": [BASE + 10_000, BASE + 10_000],
        "k": [100.0, 101.0],
        "up_won": [1.0, 1.0],
        "horizon": [5, 15],
        "resolution_source": [source_5m, source_15m],
    })


def _documented_markets(day, *, annotation=None):
    common_end_s = int(pd.Timestamp(day, tz="UTC").timestamp())
    markets = _markets(source_5m=annotation, source_15m=annotation)
    starts = [common_end_s - 300, common_end_s - 900]
    markets["start"] = np.asarray(starts, dtype=np.int64) * 1_000
    markets["slug"] = [
        f"btc-updown-5m-{starts[0]}",
        f"btc-updown-15m-{starts[1]}",
    ]
    return markets


def _raw_markets():
    table = _markets()
    return pd.DataFrame({
        "market_id": table.market_id,
        "slug": ["btc-updown-5m-x", "btc-updown-15m-x"],
        "session_start_ts": table.start,
        "session_end_ts": table.end,
        "chainlink_open_price": table.k,
        "up_won": table.up_won,
        "outcome_direction": [None, None],
        "oracle_source": ["chainlink", "chainlink"],
        "chainlink_open_price_source": [SOURCE, SOURCE],
        "resolution_price_source": [SOURCE, SOURCE],
    })


def _books(*, upper_available=True, unwind_depth=10.0):
    times = BASE + np.arange(0, 1_000, 100)
    rows = []
    for market_id in ("m5", "m15"):
        for offset, stamp in enumerate(times):
            cheap = offset >= 1
            row = {
                "timestamp_ms": stamp,
                "market_id": market_id,
                "lifecycle_state": "active",
                "observed_halt_flag": False,
                "up_best_bid": 0.39,
                "up_best_ask": 0.40 if market_id == "m5" and cheap else 0.60,
                "down_best_bid": 0.53,
                "down_best_ask": 0.54 if market_id == "m15" and cheap else 0.60,
                "up_ask_size": 10.0 + offset / 100,
                "down_ask_size": 10.0 + offset / 100,
                "up_bid_size": unwind_depth + offset / 100,
                "down_bid_size": 10.0 + offset / 100,
            }
            if market_id == "m15" and stamp >= BASE + 400 and not upper_available:
                row["down_best_ask"] = 0.60
            rows.append(row)
    return pd.DataFrame(rows)


def _fee(price, rate=0.07):
    return rate * price * (1.0 - price)


def test_fee_rounding_is_exact_five_decimal_half_up():
    assert box._fee_total(0.81, 5.0, BASE) == 0.05387


def test_replays_lower_up_plus_upper_down_after_two_cheap_receipts():
    rows = box.replay_same_expiry_boxes(
        _books(),
        _markets(),
        config=box.BoxBatchConfig(latencies_ms=(200,)),
    )

    assert len(rows) == 1
    row = rows.iloc[0]
    assert (row.lower_market_id, row.upper_market_id) == ("m5", "m15")
    assert row.decision_ms == BASE + 200
    assert row.match_ms == BASE + 400
    assert row.status == "paired"
    assert row.source_verified and row.evidence_tier == "aligned_100ms_snapshot_upper_bound"
    assert row.decision_cost_per_set == pytest.approx(0.40 + 0.54 + _fee(0.40) + _fee(0.54))
    expected = 5 * (1.0 - 0.40 - 0.54 - _fee(0.40) - _fee(0.54))
    assert row.pnl == pytest.approx(expected)
    assert row.gross_floor_edge_per_set == pytest.approx(1.0 - 0.40 - 0.54)
    assert row.execution_valid and row.paired_sets == 5.0


def test_fixed_five_share_leg_respects_one_dollar_minimum():
    books = _books()
    books.loc[
        (books["market_id"] == "m5") & (books["timestamp_ms"] >= BASE + 100),
        "up_best_ask",
    ] = 0.10
    books.loc[
        (books["market_id"] == "m5") & (books["timestamp_ms"] >= BASE + 100),
        ["up_best_bid", "down_best_bid", "down_best_ask"],
    ] = [0.08, 0.89, 0.91]

    rows = box.replay_same_expiry_boxes(
        books,
        _markets(),
        config=box.BoxBatchConfig(latencies_ms=(200,)),
    )

    row = rows.iloc[0]
    assert row.status == "order_below_minimum"
    assert row.candidate_count == 1 and row.signal_count == 0


def test_independent_batch_unwinds_the_only_filled_leg():
    rows = box.replay_same_expiry_boxes(
        _books(upper_available=False),
        _markets(),
        config=box.BoxBatchConfig(latencies_ms=(200,), orphan_unwind_ms=100),
    )

    row = rows.iloc[0]
    assert row.status == "orphan_lower_unwound"
    assert row.match_ms == BASE + 400 and row.unwind_ms == BASE + 500
    assert row.lower_filled and not row.upper_filled
    expected = 5 * (0.39 - 0.40) - round(5 * _fee(0.40), 5) - round(
        5 * _fee(0.39), 5
    )
    assert row.pnl == pytest.approx(expected)
    assert row.execution_valid and row.paired_sets == 0.0


def test_orphan_without_bid_depth_fails_closed():
    rows = box.replay_same_expiry_boxes(
        _books(upper_available=False, unwind_depth=4.0),
        _markets(),
        config=box.BoxBatchConfig(latencies_ms=(200,), orphan_unwind_ms=100),
    )

    row = rows.iloc[0]
    assert row.status == "orphan_lower_invalid"
    assert not row.execution_valid
    assert np.isnan(row.pnl)
    assert row.failure == "insufficient_unwind_depth"
    assert row.entry_cost == pytest.approx(5 * 0.40)
    assert row.fees == pytest.approx(5 * _fee(0.40))


def test_orphan_cannot_unwind_after_market_stops_trading():
    books = _books(upper_available=False)
    books.loc[
        (books["market_id"] == "m5")
        & (books["timestamp_ms"] == BASE + 500),
        "lifecycle_state",
    ] = "closed"

    row = box.replay_same_expiry_boxes(
        books,
        _markets(),
        config=box.BoxBatchConfig(latencies_ms=(200,), orphan_unwind_ms=100),
    ).iloc[0]

    assert row.status == "orphan_lower_invalid"
    assert row.failure == "unwind_market_inactive"
    assert not row.execution_valid and np.isnan(row.pnl)


def test_exact_latency_snapshot_is_required():
    books = _books()
    books = books[books["timestamp_ms"] != BASE + 400]

    row = box.replay_same_expiry_boxes(
        books,
        _markets(),
        config=box.BoxBatchConfig(latencies_ms=(200,)),
    ).iloc[0]

    assert row.status == "match_snapshot_missing"
    assert np.isnan(row.match_ms) and not row.execution_valid


@pytest.mark.parametrize("problem", ["halt", "null_lifecycle", "zero_ask"])
def test_match_requires_a_live_sane_book(problem):
    books = _books()
    at_match = books["timestamp_ms"] == BASE + 400
    if problem == "halt":
        books.loc[at_match & (books["market_id"] == "m5"), "observed_halt_flag"] = True
    elif problem == "null_lifecycle":
        books.loc[at_match & (books["market_id"] == "m5"), "lifecycle_state"] = None
    else:
        books.loc[at_match & (books["market_id"] == "m5"), "up_best_ask"] = 0.0

    row = box.replay_same_expiry_boxes(
        books,
        _markets(),
        config=box.BoxBatchConfig(latencies_ms=(200,)),
    ).iloc[0]

    assert row.status == "match_book_unhealthy"
    assert not row.execution_valid and np.isnan(row.pnl)


def test_match_after_expiry_is_invalid_not_a_fill():
    markets = _markets()
    markets["end"] = BASE + 350

    row = box.replay_same_expiry_boxes(
        _books(),
        markets,
        config=box.BoxBatchConfig(latencies_ms=(200,)),
    ).iloc[0]

    assert row.status == "match_outside_market"
    assert not row.execution_valid


def test_preopen_cheap_quotes_do_not_create_a_signal():
    markets = _markets()
    markets["start"] = BASE + 300
    books = _books()
    after_open = books["timestamp_ms"] >= BASE + 300
    books.loc[after_open & (books["market_id"] == "m5"), "up_best_ask"] = 0.60
    books.loc[after_open & (books["market_id"] == "m15"), "down_best_ask"] = 0.60

    row = box.replay_same_expiry_boxes(
        books,
        markets,
        config=box.BoxBatchConfig(latencies_ms=(200,)),
    ).iloc[0]

    assert row.status == "no_signal"
    assert row.signal_count == 0


def test_impossible_nested_resolution_fails_closed():
    markets = _markets()
    markets.loc[markets["market_id"] == "m5", "up_won"] = 0.0
    markets.loc[markets["market_id"] == "m15", "up_won"] = 1.0

    row = box.replay_same_expiry_boxes(
        _books(),
        markets,
        config=box.BoxBatchConfig(latencies_ms=(200,)),
    ).iloc[0]

    assert row.status == "settlement_inconsistent"
    assert row.failure == "nested_payoff_below_one"
    assert not row.execution_valid and np.isnan(row.pnl)


def test_between_strikes_outcome_reports_the_stochastic_bonus_separately():
    markets = _markets()
    markets.loc[markets["market_id"] == "m15", "up_won"] = 0.0

    row = box.replay_same_expiry_boxes(
        _books(),
        markets,
        config=box.BoxBatchConfig(latencies_ms=(200,)),
    ).iloc[0]

    assert row.status == "paired"
    assert row.payout == pytest.approx(10.0)
    assert row.bonus_payout == pytest.approx(5.0)
    assert row.gross_floor_edge_per_set == pytest.approx(0.06)


def test_a_15m_market_can_be_the_lower_strike_and_identity_is_preserved():
    markets = _markets()
    markets["k"] = [101.0, 100.0]
    markets["slug"] = ["five", "fifteen"]
    markets["up_token"] = ["u5", "u15"]
    markets["down_token"] = ["d5", "d15"]
    books = _books()
    active = books["timestamp_ms"] >= BASE + 100
    books.loc[active & (books["market_id"] == "m15"), ["up_best_ask", "down_best_ask"]] = [0.40, 0.60]
    books.loc[active & (books["market_id"] == "m15"), ["up_best_bid", "down_best_bid"]] = [0.38, 0.58]
    books.loc[active & (books["market_id"] == "m5"), ["up_best_ask", "down_best_ask"]] = [0.46, 0.54]
    books.loc[active & (books["market_id"] == "m5"), ["up_best_bid", "down_best_bid"]] = [0.44, 0.52]

    row = box.replay_same_expiry_boxes(
        books,
        markets,
        config=box.BoxBatchConfig(latencies_ms=(200,)),
    ).iloc[0]

    assert row.status == "paired"
    assert (row.lower_market_id, row.lower_horizon, row.lower_side) == ("m15", 15, "Up")
    assert (row.upper_market_id, row.upper_horizon, row.upper_side) == ("m5", 5, "Down")
    assert (row.lower_slug, row.upper_slug) == ("fifteen", "five")
    assert (row.lower_token_id, row.upper_token_id) == ("u15", "d5")


def test_mismatched_or_missing_resolution_sources_are_diagnostic_only():
    mismatch = box.replay_same_expiry_boxes(
        _books(),
        _markets(source_15m="https://data.chain.link/streams/btc-usd"),
        config=box.BoxBatchConfig(latencies_ms=(200,)),
    )
    missing = box.replay_same_expiry_boxes(
        _books(),
        _markets(source_5m=None, source_15m=None),
        config=box.BoxBatchConfig(latencies_ms=(200,)),
    )

    assert mismatch.iloc[0].status == "source_mismatch"
    assert missing.iloc[0].status == "source_unverified"
    assert not mismatch.iloc[0].execution_valid and not missing.iloc[0].execution_valid
    assert mismatch.iloc[0].signal_count == 0 and missing.iloc[0].signal_count == 0


def test_documented_gamma_regime_verifies_same_60s_source_from_august_14():
    rows = box.replay_same_expiry_boxes(
        _books(),
        _documented_markets(
            "2026-08-20 00:15:00",
            annotation="lifecycle.eventMetadata.priceToBeat",
        ),
        config=box.BoxBatchConfig(latencies_ms=(200,)),
    )

    row = rows.iloc[0]
    assert row.status == "paired"
    assert row.resolution_source == "chainlink_btc_usd_twap_60s"
    assert row.source_basis == "documented_gamma_regime"


def test_documented_gamma_regime_verifies_point_source_before_august_7():
    row = box.replay_same_expiry_boxes(
        _books(),
        _documented_markets("2026-08-06 00:15:00"),
        config=box.BoxBatchConfig(latencies_ms=(200,)),
    ).iloc[0]

    assert row.status == "paired"
    assert row.resolution_source == "chainlink_btc_usd_point"


def test_documented_gamma_regime_rejects_august_7_to_13_window_mismatch():
    rows = box.replay_same_expiry_boxes(
        _books(),
        _documented_markets("2026-08-10 00:15:00"),
        config=box.BoxBatchConfig(latencies_ms=(200,)),
    )

    row = rows.iloc[0]
    assert row.status == "source_mismatch"
    assert row.signal_count == 0 and not row.execution_valid


def test_market_table_preserves_last_source_annotations():
    markets = pd.DataFrame({
        "market_id": ["m5", "m5"],
        "slug": ["btc-updown-5m-x", "btc-updown-5m-x"],
        "session_start_ts": [BASE - 300_000, BASE - 300_000],
        "session_end_ts": [BASE, BASE],
        "chainlink_open_price": [np.nan, 100.0],
        "up_won": [np.nan, 1.0],
        "outcome_direction": [np.nan, np.nan],
        "oracle_source": [None, "chainlink"],
        "chainlink_open_price_source": [None, SOURCE],
        "resolution_price_source": [None, SOURCE],
    })

    row = cross.market_table(markets, pd.DataFrame()).iloc[0]
    assert row.oracle_source == "chainlink"
    assert row.chainlink_open_price_source == SOURCE
    assert row.resolution_price_source == SOURCE
    assert row.resolution_source == SOURCE


def test_market_table_preserves_snapshot_settlement_safety_fields():
    raw = _raw_markets().iloc[[0]].copy()
    raw["close_boundary_fallback_used"] = True
    raw["usable_for_backtest"] = False
    raw["resolution_consistency"] = "mismatch"

    row = cross.market_table(raw, pd.DataFrame()).iloc[0]

    assert bool(row.close_boundary_fallback_used)
    assert row.usable_for_backtest is False or not bool(row.usable_for_backtest)
    assert row.resolution_consistency == "mismatch"


def test_generic_snapshot_source_does_not_conflict_with_exact_authority():
    raw = _raw_markets().iloc[[0]].copy()
    raw["resolution_price_source"] = None
    raw["oracle_source"] = "chainlink"
    resolutions = pd.DataFrame([{
        "market_id": "m5", "outcome_direction": "Up", "revision": 1,
        "resolution_price_source": SOURCE,
    }])

    row = cross.market_table(raw, resolutions).iloc[0]

    assert not row.resolution_source_conflict
    assert row.resolution_source == SOURCE


def test_latest_resolution_record_is_authoritative_and_conflicts_are_preserved():
    raw = _raw_markets().iloc[[0]].copy()
    resolutions = pd.DataFrame([
        {"market_id": "m5", "outcome_direction": "Up", "revision": 1,
         "emitted_at_ts": BASE + 10, "resolution_price_source": SOURCE},
        {"market_id": "m5", "outcome_direction": "Down", "revision": 2,
         "emitted_at_ts": BASE + 20,
         "resolution_price_source": "https://data.chain.link/streams/btc-usd",
         "close_boundary_source": "chainlink", "close_boundary_fallback_used": False},
    ])

    row = cross.market_table(raw, resolutions).iloc[0]

    assert row.snapshot_up_won == 1.0
    assert row.up_won == 0.0
    assert row.settlement_conflict
    assert row.resolution_source == "https://data.chain.link/streams/btc-usd"
    assert row.resolution_revision == 2


def test_settlement_conflict_pair_is_diagnostic_only():
    markets = _markets()
    markets["settlement_conflict"] = [True, False]

    row = box.replay_same_expiry_boxes(
        _books(),
        markets,
        config=box.BoxBatchConfig(latencies_ms=(200,)),
    ).iloc[0]

    assert row.status == "settlement_authority_conflict"
    assert row.signal_count == 0 and not row.execution_valid


@pytest.mark.parametrize(
    ("column", "value"),
    [("usable_for_backtest", False), ("resolution_consistency", "mismatch")],
)
def test_explicitly_unsafe_settlement_metadata_is_diagnostic_only(column, value):
    markets = _markets()
    markets[column] = [value, True if column == "usable_for_backtest" else "pass"]

    row = box.replay_same_expiry_boxes(
        _books(),
        markets,
        config=box.BoxBatchConfig(latencies_ms=(200,)),
    ).iloc[0]

    assert row.status == "settlement_authority_conflict"
    assert row.signal_count == 0 and not row.execution_valid


def test_cross_cli_dispatches_boxbatch(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(cross, "DS", cross.DS)
    monkeypatch.setattr(cross, "BASE", cross.BASE)
    monkeypatch.setattr(
        box,
        "run",
        lambda workdir, out, days=None, dataset=None: called.append(
            (workdir, out, days, dataset)
        ),
    )

    cross.main([
        "boxbatch",
        "3",
        "--workdir",
        str(tmp_path),
        "--out",
        str(tmp_path / "report.md"),
        "--dataset",
        "example/non-default",
    ])

    assert called == [(
        str(tmp_path), str(tmp_path / "report.md"), 3, "example/non-default"
    )]


def test_summary_counts_independent_leg_risk():
    rows = pd.DataFrame([
        {"day": "2026-08-20", "latency_ms": 400, "signal_count": 1,
         "status": "paired", "execution_valid": True, "paired_sets": 5.0,
         "pnl": 0.20, "fees": 0.02, "gross_floor_edge_per_set": 0.04,
         "decision_cost_per_set": 0.97},
        {"day": "2026-08-20", "latency_ms": 400, "signal_count": 1,
         "status": "orphan_lower_unwound", "execution_valid": True, "paired_sets": 0.0,
         "pnl": -0.10, "fees": 0.03, "gross_floor_edge_per_set": np.nan,
         "decision_cost_per_set": 0.96},
        {"day": "2026-08-21", "latency_ms": 400, "signal_count": 1,
         "status": "orphan_upper_invalid", "execution_valid": False, "paired_sets": 0.0,
         "pnl": np.nan, "fees": 0.01, "gross_floor_edge_per_set": np.nan,
         "decision_cost_per_set": 0.95},
    ])

    summary = box.summarize_rows(rows)
    row = summary.iloc[0]
    assert row.period == "C" and row.latency_ms == 400
    assert row.signals == 3 and row.paired == 1 and row.orphans == 2
    assert row.invalid == 1 and row.paired_rate == pytest.approx(1 / 3)
    assert row.orphan_rate == pytest.approx(2 / 3)
    assert row.lower_orphans == 1 and row.upper_orphans == 1
    assert np.isnan(row.net_pnl)
    assert row.fees == pytest.approx(0.06)
    assert row.gross_floor_edge_per_set == pytest.approx(0.04)
    assert row.attempted_notional == pytest.approx(14.4)
    assert row.peak_capital == pytest.approx(4.85)
    assert np.isnan(row.day_cluster_lower_99)


def test_summary_reports_day_cluster_lower_bound_per_attempted_share():
    rows = pd.DataFrame([
        {"day": "2026-08-17", "latency_ms": 400, "signal_count": 1,
         "status": "paired", "execution_valid": True, "paired_sets": 5.0,
         "shares": 5.0, "pnl": 0.50, "decision_cost_per_set": 0.90},
        {"day": "2026-08-18", "latency_ms": 400, "signal_count": 1,
         "status": "paired", "execution_valid": True, "paired_sets": 5.0,
         "shares": 5.0, "pnl": 0.50, "decision_cost_per_set": 0.90},
    ])

    row = box.summarize_rows(rows).iloc[0]

    assert row.net_ev_per_share == pytest.approx(0.10)
    assert row.day_cluster_lower_99 == pytest.approx(0.10)
    assert row.exact_sign_p == pytest.approx(0.25)
    assert row.family100_corrected_p == 1.0
    assert row.max_drawdown == pytest.approx(0.0)


def test_peak_capital_sums_overlapping_box_reservations():
    rows = pd.DataFrame([
        {"day": "2026-08-17", "latency_ms": 400, "signal_count": 1,
         "status": "neither", "execution_valid": True, "paired_sets": 0.0,
         "shares": 5.0, "pnl": 0.0, "fees": 0.0,
         "decision_cost_per_set": 0.95, "decision_ms": BASE,
         "match_ms": BASE + 400, "end_ms": BASE + 10_000,
         "entry_cost": 0.0},
        {"day": "2026-08-17", "latency_ms": 400, "signal_count": 1,
         "status": "neither", "execution_valid": True, "paired_sets": 0.0,
         "shares": 5.0, "pnl": 0.0, "fees": 0.0,
         "decision_cost_per_set": 0.96, "decision_ms": BASE,
         "match_ms": BASE + 400, "end_ms": BASE + 10_000,
         "entry_cost": 0.0},
    ])

    row = box.summarize_rows(rows).iloc[0]

    assert row.peak_capital == pytest.approx(5 * (0.95 + 0.96))


def test_locked_period_starts_on_august_17():
    assert box._period("2026-07-15") == "A"
    assert box._period("2026-07-16") == "B"
    assert box._period("2026-08-15") == "B"
    assert box._period("2026-08-16") == "B"
    assert box._period("2026-08-17") == "C"


def test_historical_gate_only_qualifies_for_a_fresh_forward():
    summary = pd.DataFrame([
        {"period": period, "latency_ms": latency, "invalid": 0,
         "net_ev_per_share": 0.01, "day_cluster_lower_99": 0.001,
         "family100_corrected_p": 0.005}
        for period in ("B", "C")
        for latency in (400, 500)
    ])

    verdict = box._historical_screen_verdict(summary)

    assert "QUALIFIES FOR FRESH DIRECT-L2 FORWARD VALIDATION ONLY" in verdict
    assert "not an untouched holdout" in verdict


def test_historical_gate_rejects_unresolved_orphan_risk():
    summary = pd.DataFrame([
        {"period": period, "latency_ms": latency,
         "invalid": int(period == "C" and latency == 400),
         "net_ev_per_share": 0.01, "day_cluster_lower_99": 0.001,
         "family100_corrected_p": 0.005}
        for period in ("B", "C")
        for latency in (400, 500)
    ])

    assert box._historical_screen_verdict(summary).startswith("REJECTED")


def test_finalize_deduplicates_cross_archive_pair_and_uses_decision_utc_day():
    decision = int(pd.Timestamp("2026-08-16 00:00:00", tz="UTC").timestamp() * 1_000)
    rows = pd.DataFrame([
        {"pair_id": "a|b", "latency_ms": 400, "signal_count": 1,
         "decision_ms": decision + 100, "end_ms": decision + 60_000,
         "status": "paired"},
        {"pair_id": "a|b", "latency_ms": 400, "signal_count": 1,
         "decision_ms": decision + 200, "end_ms": decision + 60_000,
         "status": "paired"},
    ])

    result = box.finalize_rows(rows)

    assert len(result) == 1
    assert result.iloc[0].decision_ms == decision + 100
    assert result.iloc[0].day == "2026-08-16"


def test_primary_flag_follows_configured_primary_latency():
    rows = box.replay_same_expiry_boxes(
        _books(),
        _markets(),
        config=box.BoxBatchConfig(
            latencies_ms=(200, 300),
            primary_latency_ms=300,
        ),
    )

    assert rows.loc[rows["latency_ms"] == 200, "primary"].item() is False
    assert rows.loc[rows["latency_ms"] == 300, "primary"].item() is True


def test_run_reuses_cross_archive_reader_and_writes_one_current_report(monkeypatch, tmp_path):
    archive = tmp_path / "market_parquet_2026-08-20.tar.gz"

    monkeypatch.setattr(cross, "archives", lambda _manifest: [(archive.name, "sha")])

    def fake_fetch(name, path=None):
        if path is None:
            return b"manifest"
        archive.write_bytes(b"fixture")
        return archive

    monkeypatch.setattr(cross, "fetch", fake_fetch)
    monkeypatch.setattr(
        cross,
        "read_day",
        lambda _path: (_books(), _raw_markets(), pd.DataFrame()),
    )
    report = tmp_path / "cross-box-batch.md"

    box.run(str(tmp_path / "work"), str(report), days=1)

    assert report.exists()
    assert report.with_suffix(".csv.gz").exists()
    text = report.read_text()
    assert "Family 29" in text and "5m-15m" in text
    assert "aligned_100ms_snapshot_upper_bound" in text
    assert "Historical screen verdict" in text
    assert "Eligible source-verified pairs" in text
