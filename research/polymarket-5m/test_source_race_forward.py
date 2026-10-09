import json

import pytest

import h_forward
import source_race_forward as forward


START_MS = 1_791_590_400_000
DAY_MS = 86_400_000


def frozen(*, minimum_fills=3, minimum_days=2):
    return {
        "strategy_id": h_forward.STRATEGY_ID,
        "cutoff_ms": START_MS,
        "arms": {
            "baseline": ["spot_trade", "futures_book_ticker"],
            "candidate": ["spot_trade", "futures_book_ticker", "deribit_quote"],
        },
        "execution": {
            "evaluation_ms": [300.0, 350.0, 400.0, 500.0],
            "primary_evaluation_ms": 500.0,
            "minimum_direct_depth_shares": 5.0,
        },
        "statistics": {
            "minimum_fills": minimum_fills,
            "minimum_utc_days": minimum_days,
            "one_sided_alpha": 0.2,
            "family_tests": 1,
            "require_positive_net_ev": True,
            "require_positive_day_cluster_lower_99": True,
            "require_corrected_exact_p_below_alpha": True,
            "require_incremental_fill_or_ev_improvement": True,
            "require_loss_rate_not_worse": True,
        },
    }


def row(
    market, signal_ms, day, *, won=True, source="spot_trade", run="run-1",
    receive_ms=None, shares=5.0,
):
    cost = 0.4
    pnl_per_share = 1.0 - cost if won else -cost
    receive_ms = signal_ms if receive_ms is None else receive_ms
    return {
        "market_id": market,
        "evaluation_ms": 500.0,
        "signal_ms": float(START_MS + signal_ms),
        "signal_receive_ms": float(START_MS + receive_ms),
        "direction": "Up",
        "signal_source": source,
        "filled": True,
        "winner": "Up" if won else "Down",
        "won": won,
        "day": day,
        "pnl": pnl_per_share * shares,
        "pnl_per_share": pnl_per_share,
        "filled_shares": shares,
        "all_in_cost": cost,
        "sent": True,
        "reason": "filled",
        "observation_run_id": run,
        "strategy_id": h_forward.STRATEGY_ID,
    }


def write_rows(path, rows):
    path.write_text("".join(json.dumps(value) + "\n" for value in rows), encoding="utf-8")


def passing_rows():
    baseline = [
        row("b1", 1, "2026-10-10", won=True),
        row("b2", 2, "2026-10-10", won=False),
        row("b3", 3, "2026-10-10", won=True, receive_ms=DAY_MS + 3),
    ]
    candidate = [
        row("c1", 1, "2026-10-10", won=True, source="deribit_quote"),
        row("c2", 2, "2026-10-10", won=True, source="deribit_quote"),
        row("c3", 3, "2026-10-10", won=True, source="spot_trade", receive_ms=DAY_MS + 3),
        row("late", 4, "2026-10-10", won=False, source="deribit_quote", receive_ms=DAY_MS + 4),
    ]
    return baseline, candidate


def test_uses_earliest_common_cutoff_and_excludes_later_fills():
    baseline, candidate = passing_rows()

    result = forward.evaluate(baseline, candidate, frozen())

    assert result["status"] == "passed"
    assert result["common_cutoff_ms"] == START_MS + DAY_MS + 3.0
    assert result["arms"]["baseline"]["fills"] == 3
    assert result["arms"]["candidate"]["fills"] == 3
    assert result["arms"]["candidate"]["net_ev_per_share"] == pytest.approx(0.6)
    assert all(result["checks"].values())


def test_rejects_positive_candidate_when_loss_rate_is_worse():
    baseline, candidate = passing_rows()
    baseline[1] = row("b2", 2, "2026-10-10", won=True)
    candidate[1] = row("c2", 2, "2026-10-10", won=False, source="deribit_quote")

    result = forward.evaluate(baseline, candidate, frozen())

    assert result["status"] == "rejected"
    assert result["checks"]["loss_rate_not_worse"] is False


def test_terminal_pin_accepts_identical_rerun_but_rejects_new_rows(tmp_path):
    baseline, candidate = passing_rows()
    baseline_path = tmp_path / "baseline.jsonl"
    candidate_path = tmp_path / "candidate.jsonl"
    write_rows(baseline_path, baseline)
    write_rows(candidate_path, candidate)

    first = forward.update(
        baseline_path, candidate_path, tmp_path / "state", frozen=frozen(), freeze_sha256="freeze",
    )
    second = forward.update(
        baseline_path, candidate_path, tmp_path / "state", frozen=frozen(), freeze_sha256="freeze",
    )

    assert first["status"] == "passed"
    assert second == first
    assert (tmp_path / "state" / "verdict.json").is_file()

    write_rows(baseline_path, [*baseline, row("new", 2 * DAY_MS + 5, "2026-10-12")])
    with pytest.raises(ValueError, match="terminal source-race ledger"):
        forward.update(
            baseline_path, candidate_path, tmp_path / "state", frozen=frozen(), freeze_sha256="freeze",
        )


def test_collecting_state_does_not_create_terminal_pin(tmp_path):
    baseline_path = tmp_path / "baseline.jsonl"
    candidate_path = tmp_path / "candidate.jsonl"
    write_rows(baseline_path, [row("b", 1, "2026-10-10")])
    write_rows(candidate_path, [row("c", 1, "2026-10-10", source="deribit_quote")])

    result = forward.update(
        baseline_path, candidate_path, tmp_path / "state", frozen=frozen(), freeze_sha256="freeze",
    )

    assert result["status"] == "collecting"
    assert not (tmp_path / "state" / "verdict.json").exists()


def test_rejects_precutoff_receipt_or_unfrozen_arm_source():
    baseline, candidate = passing_rows()
    baseline[0]["signal_receive_ms"] = START_MS - 1

    with pytest.raises(ValueError, match="before the frozen cutoff"):
        forward.evaluate(baseline, candidate, frozen())

    baseline, candidate = passing_rows()
    candidate[0]["signal_source"] = "futures_trade"
    with pytest.raises(ValueError, match="candidate signal source"):
        forward.evaluate(baseline, candidate, frozen())


def test_stopping_uses_receive_clock_and_excludes_sub_five_share_fills():
    baseline = [row("b", 100, "2026-10-10", receive_ms=10)]
    candidate = [
        row("dust", 1, "2026-10-10", source="deribit_quote", receive_ms=5, shares=1.0),
        row("c", 1, "2026-10-10", source="deribit_quote", receive_ms=20),
    ]

    result = forward.evaluate(baseline, candidate, frozen(minimum_fills=1, minimum_days=1))

    assert result["common_cutoff_ms"] == START_MS + 20
    assert result["arms"]["candidate"]["fills"] == 1


def test_terminal_pin_detects_ledger_tampering(tmp_path):
    baseline, candidate = passing_rows()
    baseline_path = tmp_path / "baseline.jsonl"
    candidate_path = tmp_path / "candidate.jsonl"
    state = tmp_path / "state"
    write_rows(baseline_path, baseline)
    write_rows(candidate_path, candidate)
    forward.update(
        baseline_path, candidate_path, state, frozen=frozen(), freeze_sha256="freeze",
    )
    persisted = h_forward._read_jsonl(state / forward.BASELINE_LEDGER)
    persisted[0]["pnl"] = -999.0
    write_rows(state / forward.BASELINE_LEDGER, persisted)

    with pytest.raises(ValueError, match="ledger hash"):
        forward.update(
            baseline_path, candidate_path, state, frozen=frozen(), freeze_sha256="freeze",
        )


def test_collecting_status_detects_ledger_tampering(tmp_path):
    baseline_path = tmp_path / "baseline.jsonl"
    candidate_path = tmp_path / "candidate.jsonl"
    state = tmp_path / "state"
    write_rows(baseline_path, [row("b", 1, "2026-10-10")])
    write_rows(candidate_path, [row("c", 1, "2026-10-10", source="deribit_quote")])
    forward.update(
        baseline_path, candidate_path, state, frozen=frozen(), freeze_sha256="freeze",
    )
    persisted = h_forward._read_jsonl(state / forward.BASELINE_LEDGER)
    persisted[0]["pnl"] = -999.0
    write_rows(state / forward.BASELINE_LEDGER, persisted)

    with pytest.raises(ValueError, match="status ledger hash"):
        forward.update(
            baseline_path, candidate_path, state, frozen=frozen(), freeze_sha256="freeze",
        )
