from __future__ import annotations

import copy
import hashlib
import json
import math
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import source_confirmation_forward as forward

DAY_MS = 86_400_000.0
CUTOFF_MS = forward.source_confirmation.PROSPECTIVE_CUTOFF_MS
ARTIFACT_END_MS = CUTOFF_MS + 10 * DAY_MS
ARMS = (
    "baseline",
    "confirmation_veto",
    "receipt_time_shifted_placebo",
)
LATENCIES = (400.0, 500.0)
ROOT = Path(__file__).resolve().parent
_DELETE = object()


def _frozen(
    *,
    minimum_full_fills: int = 2,
    minimum_days: int = 2,
    family_tests: int = 1,
    alpha: float = 0.5,
) -> dict[str, Any]:
    created_at = datetime.fromtimestamp((CUTOFF_MS - 1_000.0) / 1_000.0, tz=UTC)
    return {
        "schema": "source-confirmation-forward-freeze-v1",
        "created_at": created_at.isoformat().replace("+00:00", "Z"),
        "strategy_id": "FAMILY36-SOURCE-CONFIRMATION-V1",
        "family": 36,
        "paper_only": True,
        "collector_region": "eu-west-1",
        "cutoff": datetime.fromtimestamp(CUTOFF_MS / 1_000.0, tz=UTC)
        .isoformat()
        .replace("+00:00", "Z"),
        "cutoff_ms": CUTOFF_MS,
        "arms": list(ARMS),
        "execution": {
            "evaluation_ms": list(LATENCIES),
            "target_shares": 5.0,
            "max_order_usd": 5.0,
            "confirmation_window_ms": 120.0,
            "placebo_shift_ms": 5_000.0,
        },
        "statistics": {
            "family_tests": family_tests,
            "minimum_full_fills_per_cell": minimum_full_fills,
            "minimum_utc_days": minimum_days,
            "one_sided_alpha": alpha,
        },
        "dependencies_sha256": {},
    }


def _day(timestamp_ms: float) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1_000, tz=UTC).date().isoformat()


def _row(
    *,
    arm: str,
    latency: float,
    opportunity: int,
    day_index: int,
    shares: float,
    market_won: bool,
    result_delay_ms: float = 10_000.0,
    run_id: str = "run-a",
    artifact_hash: str = "a" * 64,
) -> dict[str, Any]:
    receipt_ms = CUTOFF_MS + day_index * DAY_MS + 60_000.0 + opportunity * 100.0
    filled = shares > 0
    full_fill = shares >= 5.0
    winner = "Up" if market_won else "Down"
    all_in_cost = 0.1 if filled else None
    pnl_per_share = (0.9 if market_won else -0.1) if filled else None
    pnl = shares * pnl_per_share if pnl_per_share is not None else 0.0
    return {
        "strategy_id": "FAMILY36-SOURCE-CONFIRMATION-V1",
        "family": 36,
        "arm": arm,
        "variant": arm,
        "opportunity_id": f"opportunity-{opportunity}",
        "market_id": f"market-{opportunity}",
        "ordering_clock": "receive_ms",
        "match_book_clock": "receive_ms",
        "signal_receive_ms": receipt_ms,
        "signal_source_ms": receipt_ms - 5.0,
        "signal_source": "futures_book_ticker",
        "signal_ms": receipt_ms,
        "direction": "Up",
        "direct_token": "Up",
        "fair": 0.6,
        "decision_ask": 0.15,
        "fixed_limit": 0.2,
        "sent": filled,
        "decision_reason": "eligible" if filled else "confirmation_veto",
        "evaluation_ms": latency,
        "evaluation_time_ms": receipt_ms + latency,
        "target_shares": 5.0,
        "signed_size": 5.0 if filled else None,
        "maker_usd": 1.0 if filled else None,
        "eligible_depth": shares,
        "filled_shares": shares,
        "fill_price": 0.09 if filled else None,
        "fill_fee": shares * 0.01 if filled else 0.0,
        "fill_fee_per_share": 0.01 if filled else None,
        "all_in_cost": all_in_cost,
        "book_receive_ms": receipt_ms + latency if filled else None,
        "filled": filled,
        "full_fill": full_fill,
        "winner": winner,
        "won": market_won if filled else None,
        "pnl_per_share": pnl_per_share,
        "pnl": pnl,
        "intention_to_trade_pnl": pnl,
        "day": _day(receipt_ms),
        "censored": False,
        "reason": "filled" if full_fill else "partial_fill" if filled else "no_fill",
        **({"result_time_ms": receipt_ms + result_delay_ms} if filled else {}),
        "observation_run_id": run_id,
        "artifact_started_ms": CUTOFF_MS,
        "artifact_ended_ms": ARTIFACT_END_MS,
        "artifact_collector_region": "eu-west-1",
        "artifact_receipt_race_ready": True,
        "artifact_files_sha256": artifact_hash,
        "artifact_row_count": 6,
        "artifact_rows_sha256": "0" * 64,
    }


def _group(
    opportunity: int,
    day_index: int,
    *,
    market_won: bool,
    shares: dict[str, float] | None = None,
    result_delay_ms: float = 10_000.0,
) -> list[dict[str, Any]]:
    arm_shares = shares or {arm: 5.0 for arm in ARMS}
    rows = [
        _row(
            arm=arm,
            latency=latency,
            opportunity=opportunity,
            day_index=day_index,
            shares=arm_shares[arm],
            market_won=market_won,
            result_delay_ms=result_delay_ms,
            run_id=f"run-{opportunity}",
            artifact_hash=hashlib.sha256(
                f"artifact-{opportunity}".encode()
            ).hexdigest(),
        )
        for arm in ARMS
        for latency in LATENCIES
    ]
    digest_rows = [
        {
            key: value
            for key, value in row.items()
            if key not in forward._ARTIFACT_ROW_BINDING_FIELDS
        }
        for row in sorted(rows, key=forward._sort_key)
    ]
    digest = forward.source_confirmation.source_race._canonical_digest(digest_rows)
    for row in rows:
        row["artifact_rows_sha256"] = digest
    return rows


def _rebind_artifact(rows: list[dict[str, Any]], run_id: str) -> None:
    sample = [row for row in rows if row["observation_run_id"] == run_id]
    digest_rows = [
        {
            key: value
            for key, value in row.items()
            if key not in forward._ARTIFACT_ROW_BINDING_FIELDS
        }
        for row in sorted(sample, key=forward._sort_key)
    ]
    digest = forward.source_confirmation.source_race._canonical_digest(digest_rows)
    for row in sample:
        row["artifact_row_count"] = len(sample)
        row["artifact_rows_sha256"] = digest


def _passing_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    rows += _group(0, 0, market_won=True)
    rows += _group(
        1,
        0,
        market_won=False,
        shares={
            "baseline": 5.0,
            "confirmation_veto": 0.0,
            "receipt_time_shifted_placebo": 5.0,
        },
    )
    rows += _group(2, 0, market_won=True, shares={arm: 2.0 for arm in ARMS})
    rows += _group(3, 0, market_won=True, shares={arm: 0.0 for arm in ARMS})
    rows += _group(4, 1, market_won=True)
    return rows


def _production_freeze() -> dict[str, Any]:
    frozen = _frozen(
        minimum_full_fills=1_000,
        minimum_days=7,
        family_tests=874,
        alpha=0.01,
    )
    frozen["dependencies_sha256"] = {
        relative: hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
        for relative in sorted(forward.REQUIRED_DEPENDENCIES)
    }
    return frozen


def _write_hashed_freeze(path: Path, payload: dict[str, Any]) -> str:
    raw = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    digest = hashlib.sha256(raw).hexdigest()
    path.write_bytes(raw)
    path.with_suffix(".sha256").write_text(digest + "\n", encoding="ascii")
    return digest


def test_passing_evaluation_reports_both_horizons_controls_and_paired_deltas() -> None:
    result = forward.evaluate(_passing_rows(), _frozen())

    assert result["status"] == "passed"
    assert result["paper_only"] is True
    assert result["stopping_ms"] is not None
    for latency in ("400", "500"):
        assert all(result["checks"][latency].values())
        candidate = result["arms"]["confirmation_veto"]["latencies"][latency]
        baseline = result["arms"]["baseline"]["latencies"][latency]
        assert candidate["opportunities"] == 5
        assert candidate["intention_to_trade_pnl"] == pytest.approx(10.8)
        assert candidate["intention_to_trade_ev_per_opportunity"] == pytest.approx(2.16)
        assert candidate["filled_shares"] == pytest.approx(12.0)
        assert candidate["net_ev_per_share"] == pytest.approx(0.9)
        assert candidate["full_fills"] == 2
        assert (candidate["wins"], candidate["losses"]) == (3, 0)
        assert baseline["loss_rate"] == pytest.approx(0.25)
        paired = result["paired"][latency]
        assert paired["common_opportunities"] == 5
        assert paired["candidate_minus_baseline_pnl"] == pytest.approx(0.5)
        assert paired["candidate_minus_placebo_pnl"] == pytest.approx(0.5)
        assert result["controls"][latency]["baseline"]["loss_rate"] == pytest.approx(
            0.25
        )


def test_collecting_reports_every_cell_count_and_days() -> None:
    result = forward.evaluate(_group(0, 0, market_won=True), _frozen())

    assert result["status"] == "collecting"
    assert result["stopping_ms"] is None
    for arm in ARMS:
        for latency in ("400", "500"):
            assert result["cells"][arm][latency] == {
                "resolved_full_fills": 1,
                "utc_days": 1,
            }


def test_zero_rows_are_in_itt_and_partials_are_in_ev_but_not_exact_or_maturity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exact_samples: list[list[dict[str, Any]]] = []

    def exact(rows: list[dict[str, Any]]) -> float:
        exact_samples.append(rows)
        return 0.01

    monkeypatch.setattr(forward.h_replay_run, "_exact_pvalue", exact)
    result = forward.evaluate(_passing_rows(), _frozen())

    candidate = result["arms"]["confirmation_veto"]["latencies"]["400"]
    assert candidate["opportunities"] == 5
    assert candidate["fills"] == 3
    assert candidate["partial_fills"] == 1
    assert candidate["filled_shares"] == pytest.approx(12.0)
    assert result["cells"]["confirmation_veto"]["400"]["resolved_full_fills"] == 2
    assert exact_samples
    assert all(
        row["full_fill"] and float(row["filled_shares"]) >= 5.0
        for sample in exact_samples
        for row in sample
    )


def test_latest_observed_result_availability_sets_the_common_stop() -> None:
    rows = _group(0, 0, market_won=True)
    rows += _group(1, 1, market_won=True, result_delay_ms=50_000.0)

    result = forward.evaluate(rows, _frozen())

    expected = max(float(row["result_time_ms"]) for row in rows)
    assert result["stopping_ms"] == pytest.approx(expected)


def test_candidate_must_pass_both_horizons_and_matched_controls() -> None:
    rows = _passing_rows()
    rows[9] = _row(
        arm="confirmation_veto",
        latency=500.0,
        opportunity=1,
        day_index=0,
        shares=5.0,
        market_won=False,
        run_id="run-1",
        artifact_hash=hashlib.sha256(b"artifact-1").hexdigest(),
    )
    _rebind_artifact(rows, "run-1")

    result = forward.evaluate(rows, _frozen())

    assert result["status"] == "rejected"
    assert all(result["checks"]["400"].values())
    assert result["checks"]["500"]["loss_rate_below_baseline"] is False
    assert result["checks"]["500"]["common_opportunity_pnl_above_baseline"] is False


@pytest.mark.parametrize(
    ("index", "field", "value", "message"),
    [
        (0, "strategy_id", "OTHER", "strategy"),
        (0, "family", 35, "family"),
        (0, "arm", "candidate", "arm"),
        (0, "evaluation_ms", 600.0, "evaluation"),
        (0, "signal_receive_ms", CUTOFF_MS - 1.0, "cutoff"),
        (0, "signal_receive_ms", math.nan, "receipt"),
        (0, "signal_ms", CUTOFF_MS + 1.0, "receipt"),
        (0, "evaluation_time_ms", CUTOFF_MS + 1.0, "evaluation"),
        (0, "ordering_clock", "source_ms", "receipt-clock"),
        (0, "match_book_clock", "source_ms", "receipt-clock"),
        (0, "artifact_started_ms", CUTOFF_MS - 1.0, "artifact"),
        (0, "artifact_ended_ms", CUTOFF_MS - 1.0, "artifact"),
        (0, "artifact_collector_region", "us-east-1", "region"),
        (0, "artifact_receipt_race_ready", False, "receipt-race"),
        (0, "artifact_files_sha256", "not-a-hash", "artifact hash"),
        (0, "observation_run_id", "", "run id"),
        (0, "result_time_ms", _DELETE, "result availability"),
        (0, "book_receive_ms", CUTOFF_MS + 1_000_000.0, "book receipt"),
        (0, "pnl", 123.0, "PnL"),
        (0, "intention_to_trade_pnl", 123.0, "ITT"),
        (0, "day", "1900-01-01", "day"),
        (0, "target_shares", 4.0, "target"),
        (8, "intention_to_trade_pnl", None, "no-fill"),
        (14, "full_fill", True, "full-fill"),
    ],
)
def test_row_guards_reject_invalid_evidence(
    index: int, field: str, value: object, message: str
) -> None:
    rows = _passing_rows()
    if value is _DELETE:
        del rows[index][field]
    else:
        rows[index][field] = value

    with pytest.raises((TypeError, ValueError), match=message):
        forward.evaluate(rows, _frozen())


def test_group_requires_all_six_cells_and_a_common_frozen_decision() -> None:
    missing = _passing_rows()[:-1]
    with pytest.raises(ValueError, match="six|cell|complete"):
        forward.evaluate(missing, _frozen())

    drift = _passing_rows()
    drift[1]["fair"] = 0.61
    with pytest.raises(ValueError, match="row set hash|decision identity"):
        forward.evaluate(drift, _frozen())


def test_a_fill_requires_the_common_frozen_limit() -> None:
    rows = _passing_rows()
    for row in rows[:6]:
        row["fixed_limit"] = None

    with pytest.raises(ValueError, match="fixed limit"):
        forward.evaluate(rows, _frozen())


def test_a_fixed_usd_fak_may_fill_more_than_the_five_share_target() -> None:
    rows = _passing_rows()
    for decision_row in rows[:6]:
        decision_row["decision_ask"] = 0.33
        decision_row["fixed_limit"] = 0.49
    row = rows[0]
    shares = 5.697675
    fee_per_share = forward.source_confirmation.replay.taker_fee(0.43)
    all_in_cost = 0.43 + fee_per_share
    pnl_per_share = 1.0 - all_in_cost
    row.update(
        filled_shares=shares,
        fill_price=0.43,
        fill_fee=shares * fee_per_share,
        fill_fee_per_share=fee_per_share,
        all_in_cost=all_in_cost,
        maker_usd=2.45,
        pnl_per_share=pnl_per_share,
        pnl=shares * pnl_per_share,
        intention_to_trade_pnl=shares * pnl_per_share,
    )
    _rebind_artifact(rows, "run-0")

    validated = forward.validate_rows(rows, _frozen())
    assert next(
        item
        for item in validated
        if item["opportunity_id"] == "opportunity-0"
        and item["arm"] == "baseline"
        and item["evaluation_ms"] == 400.0
    )["filled_shares"] == pytest.approx(shares)


def test_rows_must_be_observed_inside_the_immutable_artifact() -> None:
    rows = _passing_rows()
    rows[0]["artifact_ended_ms"] = float(rows[0]["evaluation_time_ms"]) - 1.0

    with pytest.raises(ValueError, match="artifact interval"):
        forward.evaluate(rows, _frozen())


def test_hashed_outcome_may_arrive_after_the_market_data_interval() -> None:
    rows = _passing_rows()
    late_result = ARTIFACT_END_MS + 1_000.0
    for row in rows[:6]:
        row["result_time_ms"] = late_result
    _rebind_artifact(rows, "run-0")

    result = forward.evaluate(rows, _frozen())
    assert result["stopping_ms"] == pytest.approx(late_result)


def test_unresolved_fill_cannot_enter_the_formal_ledger() -> None:
    rows = _passing_rows()
    rows[0].update(
        winner=None,
        won=None,
        pnl_per_share=None,
        pnl=None,
        intention_to_trade_pnl=None,
    )

    with pytest.raises(ValueError, match="unresolved fill"):
        forward.evaluate(rows, _frozen())


def test_deleting_a_complete_six_cell_opportunity_breaks_artifact_binding() -> None:
    rows = _group(20, 0, market_won=True) + _group(21, 1, market_won=False)
    common_hash = hashlib.sha256(b"shared-artifact").hexdigest()
    for row in rows:
        row["observation_run_id"] = "shared-run"
        row["artifact_files_sha256"] = common_hash
    _rebind_artifact(rows, "shared-run")

    with pytest.raises(ValueError, match="artifact row set is incomplete"):
        forward.evaluate(rows[6:], _frozen())


def test_replay_fair_probability_may_be_on_the_boundary() -> None:
    rows = _passing_rows()
    for row in rows[:6]:
        row["fair"] = 1.0
    _rebind_artifact(rows, "run-0")

    assert len(forward.validate_rows(rows, _frozen())) == len(rows)


def test_one_market_cannot_mix_artifacts_or_runs() -> None:
    rows = _passing_rows()
    for row in rows[-6:]:
        row["market_id"] = "market-0"
        row["observation_run_id"] = "run-b"
        row["artifact_files_sha256"] = "b" * 64
    _rebind_artifact(rows, "run-b")

    with pytest.raises(ValueError, match="market.*artifact|artifact.*market|mix"):
        forward.evaluate(rows, _frozen())


def test_identical_duplicates_are_deduplicated_but_conflicts_fail() -> None:
    rows = _passing_rows()
    duplicate = copy.deepcopy(rows[0])
    deduplicated = forward.evaluate(
        [duplicate, *rows, copy.deepcopy(duplicate)], _frozen()
    )
    assert deduplicated["input_rows"] == len(rows) + 2
    assert deduplicated["deduplicated_rows"] == len(rows)
    assert forward.evaluate(list(reversed(rows)), _frozen()) == forward.evaluate(
        rows, _frozen()
    )

    conflict = copy.deepcopy(duplicate)
    conflict["decision_reason"] = "conflicting-replay"
    with pytest.raises(ValueError, match="conflicting duplicate"):
        forward.evaluate([*rows, conflict], _frozen())


def test_load_freeze_verifies_bytes_contract_and_dependency_hashes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "source-confirmation-freeze.json"
    payload = _production_freeze()
    digest = _write_hashed_freeze(path, payload)

    loaded, loaded_digest = forward.load_freeze(path)

    assert loaded == payload
    assert loaded_digest == digest

    path.with_suffix(".sha256").write_text("0" * 64 + "\n", encoding="ascii")
    with pytest.raises(ValueError, match="fingerprint"):
        forward.load_freeze(path)

    payload["dependencies_sha256"]["source_confirmation.py"] = "0" * 64
    _write_hashed_freeze(path, payload)
    with pytest.raises(ValueError, match="dependency"):
        forward.load_freeze(path)


def test_repository_freeze_loads_the_exact_dependency_closure() -> None:
    frozen, digest = forward.load_freeze()

    assert set(frozen["dependencies_sha256"]) == forward.REQUIRED_DEPENDENCIES
    assert digest == forward.FREEZE_SHA256_PATH.read_text(encoding="ascii").strip()


def test_accumulator_pools_runs_idempotently_and_pins_the_first_terminal_result(
    tmp_path: Path,
) -> None:
    frozen = _frozen()
    rows = _passing_rows()
    ledger = tmp_path / "observations.jsonl"
    status = tmp_path / "status.json"
    verdict = tmp_path / "verdict.json"
    state = {
        "ledger_path": ledger,
        "status_path": status,
        "verdict_path": verdict,
    }

    collecting = forward.accumulate(rows[:12], frozen, "f" * 64, **state)
    assert collecting["status"] == "collecting"
    terminal = forward.accumulate(rows[12:], frozen, "f" * 64, **state)
    assert terminal["status"] == "passed"
    assert verdict.exists()
    frozen_ledger = ledger.read_bytes()

    verdict.unlink()
    recovered = forward.accumulate(
        _group(5, 2, market_won=False), frozen, "f" * 64, **state
    )
    assert recovered == terminal
    assert ledger.read_bytes() == frozen_ledger
    assert json.loads(verdict.read_text(encoding="utf-8")) == terminal

    pinned = forward.accumulate(
        _group(6, 3, market_won=True), frozen, "f" * 64, **state
    )
    assert pinned == terminal
    assert ledger.read_bytes() == frozen_ledger


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(schema="other"),
        lambda value: value.pop("family"),
        lambda value: value.update(strategy_id="other"),
        lambda value: value.update(created_at="2999-01-01T00:00:00Z"),
        lambda value: value.update(cutoff_ms=CUTOFF_MS - 1.0),
        lambda value: value.update(arms=list(reversed(ARMS))),
        lambda value: value["execution"].update(evaluation_ms=[400.0]),
        lambda value: value["execution"].update(target_shares=4.0),
        lambda value: value["execution"].update(max_order_usd=4.0),
        lambda value: value["execution"].update(confirmation_window_ms=121.0),
        lambda value: value["execution"].update(placebo_shift_ms=4_999.0),
        lambda value: value["statistics"].update(family_tests=873),
        lambda value: value["statistics"].update(minimum_full_fills_per_cell=999),
        lambda value: value["statistics"].update(minimum_utc_days=6),
        lambda value: value["statistics"].update(one_sided_alpha=0.02),
        lambda value: value["dependencies_sha256"].pop("h_replay.py"),
        lambda value: value["dependencies_sha256"].update({"../escape": "0" * 64}),
    ],
)
def test_load_freeze_rejects_protocol_drift(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], object]
) -> None:
    path = tmp_path / "source-confirmation-freeze.json"
    payload = _production_freeze()
    mutate(payload)
    _write_hashed_freeze(path, payload)

    with pytest.raises((TypeError, ValueError)):
        forward.load_freeze(path)


def test_cli_reads_multiple_jsonl_files_and_only_prints_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    freeze_path = tmp_path / "source-confirmation-freeze.json"
    _write_hashed_freeze(freeze_path, _production_freeze())
    rows = _group(0, 0, market_won=True)
    paths = [tmp_path / "one.jsonl", tmp_path / "two.jsonl"]
    for path, sample in zip(paths, (rows[:3], rows[3:]), strict=True):
        path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in sample),
            encoding="utf-8",
        )
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "source_confirmation_forward.py",
            "--freeze",
            str(freeze_path),
            "--rows",
            str(paths[0]),
            "--rows",
            str(paths[1]),
        ],
    )

    forward.main()

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "collecting"
    assert (
        output["freeze_sha256"] == hashlib.sha256(freeze_path.read_bytes()).hexdigest()
    )
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before
