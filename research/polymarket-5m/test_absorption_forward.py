from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import absorption_forward as forward
import pytest

ROOT = Path(__file__).parent
PROTOCOL_SHA = "protocol-test-sha"
EVALUATOR_SHA = "evaluator-test-sha"


def _ms(day: str, seconds: float) -> float:
    return (
        datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp() * 1_000
        + seconds * 1_000
    )


def _fee(price: float, shares: float) -> float:
    value = (
        Decimal(str(shares))
        * Decimal("0.07")
        * Decimal(str(price))
        * (Decimal(1) - Decimal(str(price)))
    )
    return float(value.quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP))


def _frozen() -> dict[str, object]:
    frozen = json.loads((ROOT / "forward" / "absorption-freeze.json").read_text())
    frozen["holdout_start"] = "2026-10-11T00:00:00Z"
    frozen["holdout_start_ms"] = _ms("2026-10-11", 0)
    frozen["statistics"].update(
        {
            "minimum_full_base_entries_per_latency": 2,
            "minimum_resolved_full_control_entries_per_latency": 1,
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
    signal_number: int = 1,
    parent_signal_id: str | None = None,
    profitable: bool = True,
    full: bool = True,
) -> dict[str, object]:
    base_decision = _ms(day, 60 + signal_number)
    decision = base_decision + (5_000 if variant == "time_shift" else 0)
    signal_id = f"{variant}:{day}:{signal_number}"
    if variant in {"direction_reversal", "time_shift"}:
        signal_id = f"base:{day}:{signal_number}:{variant}"
        parent_signal_id = parent_signal_id or f"base:{day}:{signal_number}"
    elif variant == "no_refill":
        parent_signal_id = parent_signal_id or f"latent:{day}:{signal_number}"
    shares = 5.0 if full else 2.0
    entry_price = 0.40
    exit_price = 0.60 if profitable else 0.40
    entry_fee = _fee(entry_price, shares)
    exit_fee = _fee(exit_price, shares)
    entry_notional = entry_price * shares
    exit_notional = exit_price * shares
    pnl = exit_notional - exit_fee - entry_notional - entry_fee
    buy_side = "Up" if variant == "direction_reversal" else "Down"
    signal = {
        "schema": "post-sweep-absorption-signal-v1",
        "signal_id": signal_id,
        "parent_signal_id": parent_signal_id,
        "variant": variant,
        "market_id": f"m:{day}:{signal_number}",
        "buy_asset_id": buy_side.lower(),
        "buy_side": buy_side,
        "decision_recv_ms": decision,
        "target_shares": 5.0,
        "fixed_limit": entry_price,
        "sent": True,
        "reason": "eligible",
    }
    if variant == "time_shift":
        signal["scheduled_decision_recv_ms"] = decision
    entry_match = decision + latency
    exit_decision = entry_match + 5_000
    exit_match = exit_decision + latency
    row = {
        "signal_id": signal_id,
        "parent_signal_id": parent_signal_id,
        "variant": variant,
        "paper_only": True,
        "ordering_clock": "local_receipt_ms",
        "signal": signal,
        "market_id": signal["market_id"],
        "buy_asset_id": signal["buy_asset_id"],
        "buy_side": buy_side,
        "decision_recv_ms": decision,
        "evaluation_ms": latency,
        "entry_match_ms": entry_match,
        "entry_book_recv_ms": entry_match,
        "target_shares": 5.0,
        "fixed_limit": entry_price,
        "fee_rate": 0.07,
        "sent": True,
        "entry_reason": "filled" if full else "partial_fill",
        "entry_shares": shares,
        "filled": True,
        "full_fill": full,
        "entry_vwap": entry_price,
        "entry_levels": [{"price": entry_price, "shares": shares}],
        "entry_fee": entry_fee,
        "entry_notional": entry_notional,
        "exit_decision_ms": exit_decision,
        "exit_match_ms": exit_match,
        "exit_book_recv_ms": exit_match,
        "exit_reason": "filled",
        "exit_shares": shares,
        "exit_vwap": exit_price,
        "exit_levels": [{"price": exit_price, "shares": shares}],
        "exit_fee": exit_fee,
        "exit_notional": exit_notional,
        "settled_shares": 0.0,
        "settlement_payout": 0.0,
        "winner": None,
        "pnl": pnl,
        "pnl_per_share": pnl / shares,
        "profitable": pnl > 0,
    }
    if variant == "time_shift":
        row["scheduled_decision_recv_ms"] = decision
    return row


def _rows(day: str, *, full_base: bool = True) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for latency in (400.0, 500.0):
        parent = f"base:{day}:1"
        rows.append(_row(day, "base", latency, full=full_base))
        rows.append(
            _row(
                day,
                "direction_reversal",
                latency,
                parent_signal_id=parent,
                profitable=False,
            )
        )
        rows.append(
            _row(
                day,
                "time_shift",
                latency,
                parent_signal_id=parent,
                profitable=False,
            )
        )
        rows.append(_row(day, "no_refill", latency, signal_number=2, profitable=False))
        rows.append(
            _row(
                day,
                "quote_add_no_trade",
                latency,
                signal_number=3,
                profitable=False,
            )
        )
    return rows


def _day(path: Path, day: str, rows: list[dict[str, object]]) -> Path:
    payload = {
        "schema": "post-sweep-absorption-day-v1",
        "complete": True,
        "day": day,
        "collector_region": "eu-west-1",
        "protocol_sha256": PROTOCOL_SHA,
        "evaluator_sha256": EVALUATOR_SHA,
        "artifact_sha256": hashlib.sha256(day.encode()).hexdigest(),
        "rows": rows,
    }
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _update(path: Path, state: Path, frozen: dict[str, object] | None = None, **kwargs):
    return forward.update(
        path,
        state,
        frozen=_frozen() if frozen is None else frozen,
        protocol_sha256=PROTOCOL_SHA,
        evaluator_sha256=EVALUATOR_SHA,
        now_ms=_ms("2026-10-14", 0),
        _allow_test_freeze=True,
        **kwargs,
    )


def test_frozen_protocol_and_evaluator_load_by_checksum(tmp_path: Path) -> None:
    _protocol, protocol_sha, evaluator, evaluator_sha = forward.load_freezes()

    assert protocol_sha == (ROOT / "forward" / "absorption-freeze.sha256").read_text().strip()
    assert evaluator_sha == (
        ROOT / "forward" / "absorption-evaluator-freeze.sha256"
    ).read_text().strip()
    assert evaluator["protocol_sha256"] == protocol_sha
    assert evaluator["variants"] == list(forward.VARIANTS)

    changed = tmp_path / "absorption-freeze.json"
    changed.write_text(
        (ROOT / "forward" / "absorption-freeze.json").read_text() + " ",
        encoding="utf-8",
    )
    changed.with_suffix(".sha256").write_text(protocol_sha + "\n", encoding="ascii")
    with pytest.raises(ValueError, match="checksum drift"):
        forward.load_freezes(changed, ROOT / "forward" / "absorption-evaluator-freeze.json")


def test_chronological_days_pass_only_with_positive_base_and_weaker_controls(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    first = _update(_day(tmp_path / "d1.json", "2026-10-11", _rows("2026-10-11")), state)
    terminal = _update(
        _day(tmp_path / "d2.json", "2026-10-12", _rows("2026-10-12")), state
    )

    assert first["status"] == "collecting"
    assert terminal["status"] == "passed"
    assert terminal["common_cutoff_ms"] == _ms("2026-10-12", 61)
    assert terminal["checks"]["base_above_every_control"] is True
    assert (state / forward.VERDICT).is_file()


def test_partial_base_entries_report_risk_but_do_not_advance_stopping(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    _update(_day(tmp_path / "d1.json", "2026-10-11", _rows("2026-10-11")), state)
    result = _update(
        _day(
            tmp_path / "d2.json",
            "2026-10-12",
            _rows("2026-10-12", full_base=False),
        ),
        state,
    )

    assert result["status"] == "collecting"
    assert result["progress"]["400"]["full_entries"] == 1
    assert result["variants"]["base"]["400"]["partial_fills"] == 1


def test_unresolved_fill_future_book_bad_fee_and_incomplete_pair_fail_closed(
    tmp_path: Path,
) -> None:
    mutations = [
        (
            lambda rows: rows[0].update(
                entry_book_recv_ms=rows[0]["entry_match_ms"] + 1
            ),
            "future entry book",
        ),
        (lambda rows: rows[0].update(entry_fee=0.0), "entry fee"),
        (lambda rows: rows[0].update(pnl=99.0), "PnL"),
        (lambda rows: rows.pop(0), "missing a frozen latency"),
    ]
    for index, (mutate, message) in enumerate(mutations):
        rows = _rows("2026-10-11")
        mutate(rows)
        with pytest.raises(ValueError, match=message):
            _update(
                _day(tmp_path / f"bad-{index}.json", "2026-10-11", rows),
                tmp_path / f"state-{index}",
            )

    unresolved = _rows("2026-10-11")
    unresolved[0].update(
        exit_shares=0.0,
        exit_vwap=None,
        exit_levels=[],
        exit_fee=0.0,
        exit_notional=0.0,
        settled_shares=5.0,
        settlement_payout=None,
        winner=None,
        pnl=None,
        pnl_per_share=None,
        profitable=None,
    )
    with pytest.raises(ValueError, match="unresolved fill"):
        _update(
            _day(tmp_path / "unresolved.json", "2026-10-11", unresolved),
            tmp_path / "unresolved-state",
        )


@pytest.mark.parametrize(
    "crash_flag",
    ["_crash_after_day_commit", "_crash_after_index_commit"],
)
def test_every_admission_boundary_recovers_and_terminal_rejects_later_days(
    tmp_path: Path,
    crash_flag: str,
) -> None:
    state = tmp_path / crash_flag
    first = _day(tmp_path / f"{crash_flag}-d1.json", "2026-10-11", _rows("2026-10-11"))
    with pytest.raises(RuntimeError, match="simulated crash"):
        _update(first, state, **{crash_flag: True})
    assert (state / forward.JOURNAL).is_file()
    assert _update(first, state)["status"] == "collecting"
    assert not (state / forward.JOURNAL).exists()

    second = _day(tmp_path / f"{crash_flag}-d2.json", "2026-10-12", _rows("2026-10-12"))
    assert _update(second, state)["status"] == "passed"
    assert _update(second, state)["status"] == "passed"
    with pytest.raises(ValueError, match="terminal"):
        _update(
            _day(tmp_path / f"{crash_flag}-d3.json", "2026-10-13", _rows("2026-10-13")),
            state,
        )


def test_gap_and_admitted_day_mutation_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="expected 2026-10-11"):
        _update(
            _day(tmp_path / "gap.json", "2026-10-12", _rows("2026-10-12")),
            tmp_path / "gap-state",
        )

    state = tmp_path / "state"
    original = _day(tmp_path / "original.json", "2026-10-11", _rows("2026-10-11"))
    _update(original, state)
    changed_rows = _rows("2026-10-11")
    changed_rows[0]["pnl"] = 0.0
    changed = _day(tmp_path / "changed.json", "2026-10-11", changed_rows)
    with pytest.raises(ValueError, match="admitted day changed"):
        _update(changed, state)
