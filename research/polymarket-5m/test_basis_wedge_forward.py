from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import basis_wedge_forward as forward
import pytest

ROOT = Path(__file__).parent
PROTOCOL_SHA = "protocol-test-sha"
EVALUATOR_SHA = "evaluator-test-sha"


def _ms(day: str, seconds: float = 60.0) -> float:
    start = datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp() * 1_000
    return start + seconds * 1_000


def _frozen() -> dict[str, object]:
    frozen = json.loads((ROOT / "forward" / "basis-wedge-freeze.json").read_text())
    frozen["holdout_start"] = "2026-10-10T00:00:00Z"
    frozen["holdout_start_ms"] = _ms("2026-10-10", 0)
    frozen["statistics"].update(
        {
            "minimum_full_fills_per_latency": 2,
            "minimum_utc_days": 2,
            "family_tests": 1,
            "one_sided_alpha": 0.5,
        }
    )
    return frozen


def _row(
    day: str,
    variant: str,
    latency: float,
    *,
    signal: int = 1,
    full: bool = True,
    won: bool = True,
    resolved: bool = True,
) -> dict[str, object]:
    shares = 5.0 if full else 2.0
    price = Decimal("0.4")
    decimal_shares = Decimal(str(shares))
    fee = (decimal_shares * Decimal("0.07") * price * (Decimal(1) - price)).quantize(
        Decimal("0.00001"),
        rounding=ROUND_HALF_UP,
    )
    total_cost = decimal_shares * price + fee
    all_in_cost = float(total_cost / decimal_shares)
    pnl_per_share = 1.0 - all_in_cost if won else -all_in_cost
    base_decision_ms = _ms(day, 60.0 + signal)
    decision_ms = (
        base_decision_ms + 5_000.0 if variant == "time_shift" else base_decision_ms
    )
    signal_ms = base_decision_ms
    buy_side = "up" if variant == "direction_reversal" else "down"
    winner = buy_side.title() if won else ("Down" if buy_side == "up" else "Up")
    parent_signal_id = (
        f"base:{day}:{signal}"
        if variant in {"direction_reversal", "time_shift"}
        else None
    )
    return {
        "signal_id": f"{variant}:{day}:{signal}",
        "parent_signal_id": parent_signal_id,
        "paper_only": True,
        "ordering_clock": "local_receipt_ms",
        "match_book_clock": "local_receipt_ms",
        "market_id": f"m-{day}-{signal}",
        "variant": variant,
        "signal_recv_ms": signal_ms,
        "decision_recv_ms": decision_ms,
        "scheduled_decision_recv_ms": decision_ms,
        "decision_book_recv_ms": decision_ms,
        "evaluation_ms": latency,
        "evaluation_recv_ms": decision_ms + latency,
        "book_recv_ms": decision_ms + latency,
        "basis_direction": "up",
        "buy_side": buy_side,
        "fair": 0.5,
        "decision_vwap": 0.39,
        "fixed_limit": 0.4,
        "signal_fee_per_share": 0.0,
        "signal_net_edge": 0.1,
        "target_shares": 5.0,
        "filled_shares": shares,
        "fill_vwap": float(price),
        "fill_levels": [{"price": float(price), "shares": shares}],
        "fee_rate": 0.07,
        "fee": float(fee),
        "total_cost": float(total_cost),
        "all_in_cost": all_in_cost,
        "winner": winner if resolved else None,
        "won": won if resolved else None,
        "pnl_per_share": pnl_per_share if resolved else None,
        "pnl": shares * pnl_per_share if resolved else None,
        "filled": True,
        "full_fill": full,
        "sent": True,
        "reason": "filled" if full else "partial_fill",
        "signal": {"fixed_limit": 0.4},
    }


def _rows(day: str, *, full_500: bool = True) -> list[dict[str, object]]:
    rows = []
    for variant in forward.VARIANTS:
        for latency in (400.0, 500.0):
            rows.append(
                _row(
                    day,
                    variant,
                    latency,
                    full=full_500 or latency == 400.0,
                )
            )
    return rows


def _day(path: Path, day: str, rows: list[dict[str, object]]) -> Path:
    payload = {
        "schema": "basis-wedge-day-v1",
        "complete": True,
        "day": day,
        "collector_region": "eu-west-1",
        "protocol_sha256": PROTOCOL_SHA,
        "evaluator_sha256": EVALUATOR_SHA,
        "artifact_sha256": hashlib.sha256(day.encode()).hexdigest(),
        "rows": rows,
    }
    path.write_text(json.dumps(payload, sort_keys=True) + "\n")
    return path


def _update(day_file: Path, state: Path, frozen: dict[str, object] | None = None):
    return forward.update(
        day_file,
        state,
        frozen=_frozen() if frozen is None else frozen,
        protocol_sha256=PROTOCOL_SHA,
        evaluator_sha256=EVALUATOR_SHA,
        now_ms=_ms("2026-10-13", 0),
        _allow_test_freeze=True,
    )


def test_chronological_days_stop_once_both_latencies_reach_the_common_cutoff(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    first = _update(
        _day(tmp_path / "d1.json", "2026-10-10", _rows("2026-10-10")), state
    )
    terminal = _update(
        _day(tmp_path / "d2.json", "2026-10-11", _rows("2026-10-11")), state
    )

    assert first["status"] == "collecting"
    assert terminal["status"] == "passed"
    assert terminal["common_cutoff_ms"] == _ms("2026-10-11", 61.0)
    assert set(terminal["base"]) == {"400", "500"}
    assert all(item["fills"] == 2 for item in terminal["base"].values())
    assert set(terminal["controls"]) == set(forward.CONTROL_VARIANTS)
    assert (state / forward.VERDICT).is_file()


def test_partial_fills_are_reported_but_never_advance_the_primary_stop(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    _update(
        _day(
            tmp_path / "d1.json",
            "2026-10-10",
            _rows(
                "2026-10-10",
                full_500=False,
            ),
        ),
        state,
    )
    result = _update(
        _day(
            tmp_path / "d2.json",
            "2026-10-11",
            _rows(
                "2026-10-11",
                full_500=False,
            ),
        ),
        state,
    )

    assert result["status"] == "collecting"
    assert result["progress"]["400"]["full_fills"] == 2
    assert result["progress"]["500"]["full_fills"] == 0
    assert result["variants"]["base"]["500"]["partial_fills"] == 2
    assert result["variants"]["base"]["500"]["pnl"] > 0


def test_base_cannot_pass_when_any_preregistered_control_is_absent() -> None:
    rows = []
    for day in ("2026-10-10", "2026-10-11"):
        rows.extend(_row(day, "base", latency) for latency in (400.0, 500.0))

    result = forward.evaluate(rows, _frozen())

    assert result["status"] == "rejected"
    assert result["checks"]["all_controls_observed"] is False


def test_gap_mutation_pre_cutoff_and_unresolved_fill_fail_closed(
    tmp_path: Path,
) -> None:
    frozen = _frozen()
    with pytest.raises(ValueError, match="expected day"):
        _update(
            _day(tmp_path / "gap.json", "2026-10-11", _rows("2026-10-11")),
            tmp_path / "gap-state",
            frozen,
        )

    pre = _rows("2026-10-09")
    with pytest.raises(ValueError, match="before the frozen cutoff"):
        _update(
            _day(tmp_path / "pre.json", "2026-10-09", pre),
            tmp_path / "pre-state",
            frozen,
        )

    unresolved = _rows("2026-10-10")
    unresolved[0] = _row("2026-10-10", "base", 400.0, resolved=False)
    with pytest.raises(ValueError, match="unresolved fill"):
        _update(
            _day(tmp_path / "unresolved.json", "2026-10-10", unresolved),
            tmp_path / "unresolved-state",
            frozen,
        )

    state = tmp_path / "mut-state"
    day = _day(tmp_path / "original.json", "2026-10-10", _rows("2026-10-10"))
    _update(day, state, frozen)
    changed_rows = _rows("2026-10-10")
    changed_rows[0]["fixed_limit"] = 0.41
    changed = _day(tmp_path / "changed.json", "2026-10-10", changed_rows)
    with pytest.raises(ValueError, match="immutable day"):
        _update(changed, state, frozen)


def test_terminal_pin_rejects_any_later_observation(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _update(_day(tmp_path / "d1.json", "2026-10-10", _rows("2026-10-10")), state)
    terminal = _update(
        _day(tmp_path / "d2.json", "2026-10-11", _rows("2026-10-11")), state
    )

    same = _update(tmp_path / "d2.json", state)
    assert same == terminal
    with pytest.raises(ValueError, match="terminal"):
        _update(_day(tmp_path / "d3.json", "2026-10-12", _rows("2026-10-12")), state)


def test_interrupted_commit_recovers_from_the_hashed_day_journal(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    day = _day(tmp_path / "d1.json", "2026-10-10", _rows("2026-10-10"))
    with pytest.raises(RuntimeError, match="simulated crash"):
        forward.update(
            day,
            state,
            frozen=_frozen(),
            protocol_sha256=PROTOCOL_SHA,
            evaluator_sha256=EVALUATOR_SHA,
            now_ms=_ms("2026-10-13", 0),
            _crash_after_day_commit=True,
            _allow_test_freeze=True,
        )

    assert (state / forward.JOURNAL).is_file()
    result = _update(day, state)
    assert result["status"] == "collecting"
    assert not (state / forward.JOURNAL).exists()
    assert (
        json.loads((state / forward.STATUS).read_text())["days"][0]["day"]
        == "2026-10-10"
    )


@pytest.mark.parametrize(
    "crash_flag",
    [
        "_crash_after_ledger_commit",
        "_crash_after_status_commit",
    ],
)
def test_every_multifile_commit_boundary_recovers(
    tmp_path: Path,
    crash_flag: str,
) -> None:
    state = tmp_path / crash_flag
    day = _day(tmp_path / f"{crash_flag}.json", "2026-10-10", _rows("2026-10-10"))
    kwargs = {crash_flag: True}
    with pytest.raises(RuntimeError, match="simulated crash"):
        forward.update(
            day,
            state,
            frozen=_frozen(),
            protocol_sha256=PROTOCOL_SHA,
            evaluator_sha256=EVALUATOR_SHA,
            now_ms=_ms("2026-10-13", 0),
            _allow_test_freeze=True,
            **kwargs,
        )
    assert (state / forward.JOURNAL).is_file()
    assert _update(day, state)["status"] == "collecting"
    assert not (state / forward.JOURNAL).exists()


def test_fill_and_pair_economics_are_recomputed_not_trusted(tmp_path: Path) -> None:
    mutations = [
        (
            lambda rows: rows[0].update(book_recv_ms=rows[0]["evaluation_recv_ms"] + 1),
            "future book",
        ),
        (lambda rows: rows[0].update(fill_vwap=0.41), "fill levels"),
        (lambda rows: rows[0].update(fixed_limit=0.39), "frozen limit"),
        (lambda rows: rows[0].update(target_shares=4.0), "target shares"),
        (lambda rows: rows[0].update(fee=0.0), "fee"),
        (lambda rows: rows[1].update(fixed_limit=0.41), "paired decision"),
    ]
    for index, (mutate, message) in enumerate(mutations):
        rows = _rows("2026-10-10")
        mutate(rows)
        with pytest.raises(ValueError, match=message):
            _update(
                _day(tmp_path / f"bad-{index}.json", "2026-10-10", rows),
                tmp_path / f"state-{index}",
            )


def test_existing_ledger_or_day_tampering_is_detected_before_next_admission(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    _update(_day(tmp_path / "d1.json", "2026-10-10", _rows("2026-10-10")), state)
    with (state / forward.LEDGER).open("a") as stream:
        stream.write("{}\n")

    with pytest.raises(ValueError, match="ledger hash mismatch"):
        _update(_day(tmp_path / "d2.json", "2026-10-11", _rows("2026-10-11")), state)


def test_day_tampering_and_a_missing_terminal_pin_fail_closed(tmp_path: Path) -> None:
    day_state = tmp_path / "day-state"
    _update(_day(tmp_path / "d1.json", "2026-10-10", _rows("2026-10-10")), day_state)
    admitted = day_state / forward.DAYS / "2026-10-10.json"
    admitted.write_text(admitted.read_text() + " ")
    with pytest.raises(ValueError, match="immutable day hash mismatch"):
        _update(
            _day(tmp_path / "d2.json", "2026-10-11", _rows("2026-10-11")), day_state
        )

    terminal_state = tmp_path / "terminal-state"
    _update(
        _day(tmp_path / "t1.json", "2026-10-10", _rows("2026-10-10")), terminal_state
    )
    _update(
        _day(tmp_path / "t2.json", "2026-10-11", _rows("2026-10-11")), terminal_state
    )
    (terminal_state / forward.VERDICT).unlink()
    with pytest.raises(ValueError, match="terminal verdict is missing"):
        _update(tmp_path / "t2.json", terminal_state)


def test_evaluator_freeze_binds_protocol_runner_and_forward_code() -> None:
    freeze_path = ROOT / "forward" / "basis-wedge-evaluator-freeze.json"
    sha_path = ROOT / "forward" / "basis-wedge-evaluator-freeze.sha256"
    frozen = json.loads(freeze_path.read_text())

    assert (
        hashlib.sha256(freeze_path.read_bytes()).hexdigest()
        == sha_path.read_text().strip()
    )
    assert (
        frozen["protocol_sha256"]
        == (ROOT / "forward" / "basis-wedge-freeze.sha256").read_text().strip()
    )
    for name in ("basis_wedge.py", "basis_wedge_run.py", "basis_wedge_forward.py"):
        assert (
            frozen["dependencies_sha256"][name]
            == hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        )
    protocol, protocol_sha, evaluator, evaluator_sha = forward.load_freezes()
    assert protocol["strategy_id"] == evaluator["strategy_id"]
    assert protocol_sha == frozen["protocol_sha256"]
    assert evaluator_sha == sha_path.read_text().strip()


def test_runtime_cannot_reuse_frozen_hashes_with_mutated_rules(tmp_path: Path) -> None:
    protocol, protocol_sha, _evaluator, evaluator_sha = forward.load_freezes()
    mutated = json.loads(json.dumps(protocol))
    mutated["statistics"]["minimum_full_fills_per_latency"] = 1

    with pytest.raises(ValueError, match="protocol freeze differs"):
        forward.update(
            tmp_path / "never-read.json",
            tmp_path / "state",
            frozen=mutated,
            protocol_sha256=protocol_sha,
            evaluator_sha256=evaluator_sha,
            now_ms=_ms("2026-10-13", 0),
        )
