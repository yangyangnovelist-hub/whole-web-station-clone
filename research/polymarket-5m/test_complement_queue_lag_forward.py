from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

import complement_queue_lag as replay
import complement_queue_lag_forward as forward
import pytest

START_MS = 1_800_000_000_000.0
DAY_MS = 86_400_000.0
ARTIFACT = "a" * 64
ROOT = Path(__file__).resolve().parent
DEPENDENCY_FILES = (
    "complement_queue_lag.py",
    "complement_queue_lag_forward.py",
    "h_forward.py",
    "h_replay.py",
    "h_replay_archive.py",
    "h_replay_run.py",
    "source_race_forward.py",
)


def dependency_hashes() -> dict[str, str]:
    return {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in DEPENDENCY_FILES
    }


def frozen(*, cutoff_ms: float = START_MS - 1_000.0) -> dict[str, Any]:
    return {
        "schema": "complement-queue-lag-forward-freeze-v1",
        "strategy_id": "FAMILY34-COMPLEMENT-QUEUE-LAG-V1",
        "paper_only": True,
        "prospective_cutoff_ms": cutoff_ms,
        "dependencies_sha256": dependency_hashes(),
        "execution": {
            "minimum_queue_shares": 5.0,
            "minimum_midpoint_move": 0.01,
            "maximum_spread_widen": 0.0,
            "confirmation_ms": 250.0,
            "target_shares": 5.0,
            "minimum_total_edge": 0.05,
            "minimum_order_usd": 1.0,
            "minimum_remaining_s": 30.0,
            "maximum_remaining_s": 240.0,
            "book_fresh_ms": 2_000.0,
            "conservative_limit_tick": 0.01,
            "evaluation_ms": [400.0, 500.0],
            "delayed_control_ms": 5_000.0,
        },
        "statistics": {
            "minimum_full_fills_per_latency": 1_000,
            "minimum_utc_days": 7,
            "family_tests": 100,
            "one_sided_alpha": 0.01,
        },
    }


def charged_fee(price: float, shares: float = 5.0, rate: float = 0.07) -> float:
    raw = (
        Decimal(str(shares))
        * Decimal(str(rate))
        * Decimal(str(price))
        * (Decimal(1) - Decimal(str(price)))
    )
    return float(raw.quantize(Decimal("0.00001"), rounding=ROUND_DOWN))


def charged_level_fee(
    levels: list[tuple[float, float]], rate: float = 0.07
) -> float:
    raw = sum(
        (
            Decimal(str(shares))
            * Decimal(str(rate))
            * Decimal(str(price))
            * (Decimal(1) - Decimal(str(price)))
            for price, shares in levels
        ),
        Decimal(0),
    )
    return float(raw.quantize(Decimal("0.00001"), rounding=ROUND_DOWN))


def admission_kwargs(
    timestamp_ms: float = START_MS,
    *,
    started_ms: float | None = None,
    ended_ms: float | None = None,
) -> dict[str, Any]:
    instant = datetime.fromtimestamp(timestamp_ms / 1_000.0, tz=UTC)
    day_start_ms = instant.replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timestamp() * 1_000.0
    return {
        "expected_utc_day": instant.date().isoformat(),
        "artifact_start_ms": (
            day_start_ms if started_ms is None else float(started_ms)
        ),
        "artifact_end_ms": (
            day_start_ms + DAY_MS - 1.0 if ended_ms is None else float(ended_ms)
        ),
    }


def signal_rows(
    index: int,
    *,
    variant: str = "base",
    decision_ms: float | None = None,
    parent_signal_id: str | None = None,
    market_id: str | None = None,
    fill_price: float = 0.30,
    won: bool = True,
    winner: str | None = None,
    filled_by_latency: dict[float, bool] | None = None,
    fee_rate: float = 0.07,
) -> list[dict[str, Any]]:
    day_index = min(index // 143, 6)
    intra_day = float(index % 143) * 1_000.0
    decision = (
        START_MS + day_index * DAY_MS + intra_day
        if decision_ms is None
        else float(decision_ms)
    )
    market = market_id or f"m{index:04d}"
    signal_id = f"{market}:{variant}:{index + 1}"
    if variant == "delay_5s":
        exhausted = decision - 5_100.0
        remaining = 115.0
    elif variant == "reverse_order":
        exhausted = decision
        remaining = 120.0
    else:
        exhausted = decision - 100.0
        remaining = 120.0
    decision_vwap = 0.40
    decision_fee = charged_fee(decision_vwap, rate=fee_rate)
    resolved_winner = ("Up" if won else "Down") if winner is None else winner
    rows: list[dict[str, Any]] = []
    for latency in (400.0, 500.0):
        filled = True if filled_by_latency is None else filled_by_latency[latency]
        fee = charged_fee(fill_price, rate=fee_rate) if filled else None
        total_cost = fill_price * 5.0 + fee if fee is not None else None
        resolved = resolved_winner in {"Up", "Down"}
        row_won = resolved_winner == "Up" if filled and resolved else None
        if filled and resolved:
            assert total_cost is not None
            pnl = (5.0 if row_won else 0.0) - total_cost
        else:
            pnl = None
        rows.append(
            {
                "signal_id": signal_id,
                "parent_signal_id": parent_signal_id,
                "variant": variant,
                "market_id": market,
                "epoch": 1,
                "decision_ms": decision,
                "remaining_s": remaining,
                "exhausted_ms": exhausted,
                "exhausted_source_ms": exhausted - 1.0,
                "exhausted_asset_id": f"{market}-down",
                "buy_asset_id": f"{market}-up",
                "buy_side": "Up",
                "fair": 0.70,
                "decision_source_ms": (
                    decision - 5_001.0 if variant == "delay_5s" else decision - 1.0
                ),
                "decision_vwap": decision_vwap,
                "decision_levels": [
                    {"price": decision_vwap, "shares": 5.0}
                ],
                "fixed_limit": 0.40,
                "decision_fee": decision_fee,
                "net_edge": 0.70 * 5.0 - decision_vwap * 5.0 - decision_fee,
                "pre_best_bid": 0.31,
                "pre_best_bid_size": 5.0,
                "pre_midpoint": 0.32,
                "post_midpoint": 0.30,
                "paper_only": True,
                "ordering_clock": "local_receipt_ms",
                "match_book_clock": "local_receipt_ms",
                "evaluation_ms": latency,
                "evaluation_recv_ms": decision + latency,
                "book_recv_ms": decision + latency - 1.0,
                "book_source_ms": decision + latency - 2.0,
                "target_shares": 5.0,
                "filled_shares": 5.0 if filled else 0.0,
                "fill_vwap": fill_price if filled else None,
                "fill_levels": (
                    [{"price": fill_price, "shares": 5.0}] if filled else []
                ),
                "fee_rate": fee_rate,
                "fee": fee,
                "total_cost": total_cost,
                "winner": resolved_winner,
                "won": row_won,
                "pnl_per_share": pnl / 5.0 if pnl is not None else None,
                "pnl": pnl,
                "filled": filled,
                "full_fill": filled,
                "sent": True,
                "reason": "filled" if filled else "ask_above_frozen_limit",
            }
        )
    return rows


def base_rows(count: int) -> list[dict[str, Any]]:
    return [row for index in range(count) for row in signal_rows(index)]


@pytest.fixture(scope="module")
def passing_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index in range(1_000):
        base = signal_rows(index, fill_price=0.30)
        rows.extend(base)
        parent = str(base[0]["signal_id"])
        decision = float(base[0]["decision_ms"])
        rows.extend(
            signal_rows(
                index + 10_000,
                variant="reverse_order",
                market_id=f"r{index:04d}",
                decision_ms=decision,
                fill_price=0.40,
                won=False,
            )
        )
        rows.extend(
            signal_rows(
                index + 20_000,
                variant="delay_5s",
                market_id=str(base[0]["market_id"]),
                parent_signal_id=parent,
                decision_ms=decision + 5_000.0,
                fill_price=0.40,
                won=True,
            )
        )
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("field", "drifted_value"),
    [
        ("minimum_queue_shares", 6.0),
        ("minimum_midpoint_move", 0.02),
        ("maximum_spread_widen", 0.01),
        ("confirmation_ms", 251.0),
        ("target_shares", 6.0),
        ("minimum_total_edge", 0.06),
        ("minimum_order_usd", 2.0),
        ("minimum_remaining_s", 31.0),
        ("maximum_remaining_s", 241.0),
        ("book_fresh_ms", 2_001.0),
        ("conservative_limit_tick", 0.02),
        ("evaluation_ms", [400.0, 501.0]),
        ("delayed_control_ms", 5_001.0),
    ],
)
def test_protocol_requires_every_exact_queue_lag_rule(
    field: str,
    drifted_value: object,
) -> None:
    missing = frozen()
    del missing["execution"][field]
    with pytest.raises(ValueError, match="execution config"):
        forward.evaluate([], missing)

    drifted = frozen()
    drifted["execution"][field] = drifted_value
    with pytest.raises(ValueError, match="execution config"):
        forward.evaluate([], drifted)


@pytest.mark.parametrize(
    "case",
    [
        "top_level_extra",
        "cutoff_alias",
        "execution_extra",
        "target_alias",
        "statistics_extra",
        "minimum_entries_alias",
    ],
)
def test_protocol_rejects_unknown_keys_and_aliases(case: str) -> None:
    config = frozen()
    if case == "top_level_extra":
        config["description"] = "not frozen"
    elif case == "cutoff_alias":
        config["cutoff_ms"] = config.pop("prospective_cutoff_ms")
    elif case == "execution_extra":
        config["execution"]["unknown_rule"] = 1
    elif case == "target_alias":
        config["execution"]["minimum_full_fill_shares"] = config["execution"].pop(
            "target_shares"
        )
    elif case == "statistics_extra":
        config["statistics"]["unknown_rule"] = 1
    else:
        config["statistics"]["minimum_entries_per_latency"] = config["statistics"].pop(
            "minimum_full_fills_per_latency"
        )

    with pytest.raises(
        ValueError, match="freeze/config|execution config|statistics config"
    ):
        forward.evaluate([], config)


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("missing", "dependency hashes keys drift.*missing h_forward.py"),
        ("extra", "dependency hashes keys drift.*unexpected 'unrelated.py'"),
        ("dependency_mismatch", "dependency hash mismatch.*complement_queue_lag.py"),
        (
            "evaluator_mismatch",
            "dependency hash mismatch.*complement_queue_lag_forward.py",
        ),
    ],
)
def test_protocol_requires_exact_dependency_hash_closure(
    case: str,
    message: str,
) -> None:
    config = frozen()
    hashes = config["dependencies_sha256"]
    assert isinstance(hashes, dict)
    if case == "missing":
        del hashes["h_forward.py"]
    elif case == "extra":
        hashes["unrelated.py"] = "0" * 64
    elif case == "dependency_mismatch":
        hashes["complement_queue_lag.py"] = "0" * 64
    else:
        hashes["complement_queue_lag_forward.py"] = "0" * 64

    with pytest.raises(ValueError, match=message):
        forward.evaluate([], config)


def test_stopping_boundaries_999_1000_and_6_7_days(
    passing_rows: list[dict[str, Any]],
) -> None:
    rows_999 = [
        row
        for row in passing_rows
        if str(row["market_id"]) not in {"m0999", "r0999"}
    ]
    result_999 = forward.evaluate(rows_999, frozen())
    assert result_999["status"] == "collecting"
    assert result_999["progress"]["400"]["entries"] == 999
    assert result_999["progress"]["500"]["entries"] == 999

    result_1000 = forward.evaluate(passing_rows, frozen())
    assert result_1000["status"] == "passed"
    assert result_1000["common_cutoff_ms"] is not None

    six_days = copy.deepcopy(passing_rows)
    for row in six_days:
        if float(row["decision_ms"]) < START_MS + 6 * DAY_MS:
            continue
        for field in (
            "decision_ms",
            "decision_source_ms",
            "exhausted_ms",
            "exhausted_source_ms",
            "evaluation_recv_ms",
            "book_recv_ms",
            "book_source_ms",
        ):
            row[field] = float(row[field]) - DAY_MS
    assert forward.evaluate(six_days, frozen())["status"] == "collecting"


def test_each_latency_must_independently_reach_the_stop() -> None:
    rows = base_rows(1_000)
    last_500 = next(row for row in reversed(rows) if row["evaluation_ms"] == 500.0)
    almost_five = 4.9999999995
    price = float(last_500["fill_vwap"])
    fee = charged_fee(price, almost_five)
    total_cost = price * almost_five + fee
    pnl = almost_five - total_cost
    last_500.update(
        filled_shares=almost_five,
        fill_levels=[{"price": price, "shares": almost_five}],
        fee=fee,
        total_cost=total_cost,
        pnl_per_share=pnl / almost_five,
        pnl=pnl,
    )

    result = forward.evaluate(rows, frozen())

    assert result["status"] == "collecting"
    assert result["progress"]["400"]["entries"] == 1_000
    assert result["progress"]["500"]["entries"] == 999


def test_common_cutoff_includes_every_row_tied_at_the_boundary(
    passing_rows: list[dict[str, Any]],
) -> None:
    rows = copy.deepcopy(passing_rows)
    tie = START_MS + 6 * DAY_MS + 141_000.0
    rows.extend(signal_rows(2_000, decision_ms=tie, market_id="tie-a"))
    rows.extend(signal_rows(2_001, decision_ms=tie, market_id="tie-b"))

    result = forward.evaluate(rows, frozen())

    assert result["common_cutoff_ms"] == tie + 5_500.0
    assert result["base"]["400"]["entries"] == 1_002
    assert result["base"]["500"]["entries"] == 1_002


def test_common_cutoff_waits_until_delayed_control_execution_is_observable(
    passing_rows: list[dict[str, Any]],
) -> None:
    result = forward.evaluate(passing_rows, frozen())
    cutoff = float(result["common_cutoff_ms"])

    selected_delay_500 = [
        row
        for row in passing_rows
        if row["variant"] == "delay_5s"
        and row["evaluation_ms"] == 500.0
        and float(row["evaluation_recv_ms"]) <= cutoff
    ]
    assert len(selected_delay_500) == 1_000
    assert cutoff == max(float(row["evaluation_recv_ms"]) for row in selected_delay_500)


def test_passing_gate_uses_both_latencies_and_both_controls(
    passing_rows: list[dict[str, Any]],
) -> None:
    result = forward.evaluate(passing_rows, frozen())

    assert result["status"] == "passed"
    assert result["paper_only"] is True
    assert result["production_eligible"] is False
    for latency in ("400", "500"):
        assert result["base"][latency]["net_ev_per_share"] > 0
        assert result["base"][latency]["day_cluster_lower_99"] > 0
        assert result["base"][latency]["corrected_exact_p"] < 0.01
        assert (
            result["base"][latency]["net_ev_per_share"]
            > (result["controls"][latency]["reverse_order"]["net_ev_per_share"])
        )
        assert (
            result["base"][latency]["net_ev_per_share"]
            > (result["controls"][latency]["delay_5s"]["net_ev_per_share"])
        )
    assert all(result["checks"].values())


@pytest.mark.parametrize("control", ["reverse_order", "delay_5s"])
def test_empty_or_censored_controls_fail_closed(
    passing_rows: list[dict[str, Any]],
    control: str,
) -> None:
    rows = copy.deepcopy(passing_rows)
    if control == "reverse_order":
        rows = [row for row in rows if row["variant"] != control]
    else:
        row = next(
            item
            for item in rows
            if item["variant"] == control and item["evaluation_ms"] == 400.0
        )
        row.update(
            filled_shares=0.0,
            fill_vwap=None,
            fill_levels=[],
            fee=None,
            total_cost=None,
            won=None,
            pnl_per_share=None,
            pnl=None,
            filled=False,
            full_fill=False,
            sent=False,
            reason="right_censored",
        )

    result = forward.evaluate(rows, frozen())

    assert result["status"] == "collecting"
    assert result["common_cutoff_ms"] is None
    assert result["control_progress"][control]["400"]["full_fills"] < 1_000


def test_unresolved_full_fill_selects_outcome_blind_cutoff_but_cannot_terminalize(
    passing_rows: list[dict[str, Any]],
) -> None:
    rows = copy.deepcopy(passing_rows)
    unresolved_market = next(
        str(row["market_id"])
        for row in rows
        if row["variant"] == "base" and row["evaluation_ms"] == 400.0
    )
    for row in rows:
        if row["market_id"] == unresolved_market:
            row.update(winner=None, won=None, pnl_per_share=None, pnl=None)

    result = forward.evaluate(rows, frozen())

    assert result["common_cutoff_ms"] is not None
    assert result["progress"]["400"]["entries"] == 1_000
    assert result["progress"]["500"]["entries"] == 1_000
    assert result["status"] == "collecting"
    assert result["pending"]["unresolved_full_fills"] == 4


def test_pre_cutoff_right_censored_base_observation_cannot_terminalize(
    passing_rows: list[dict[str, Any]],
) -> None:
    rows = copy.deepcopy(passing_rows)
    censored = signal_rows(
        30_000,
        market_id="censored",
        decision_ms=START_MS + 1.0,
        filled_by_latency={400.0: False, 500.0: False},
    )
    for row in censored:
        row.update(sent=False, reason="right_censored")
    rows.extend(censored)

    result = forward.evaluate(rows, frozen())

    assert result["common_cutoff_ms"] is not None
    assert result["status"] == "collecting"
    assert result["pending"]["right_censored_base"] == 2


def test_unresolved_control_full_fill_cannot_terminalize(
    passing_rows: list[dict[str, Any]],
) -> None:
    rows = copy.deepcopy(passing_rows)
    unresolved_id = next(
        str(item["signal_id"])
        for item in rows
        if item["variant"] == "reverse_order" and item["evaluation_ms"] == 400.0
    )
    for row in rows:
        if row["signal_id"] == unresolved_id:
            row.update(winner=None, won=None, pnl_per_share=None, pnl=None)

    result = forward.evaluate(rows, frozen())

    assert result["common_cutoff_ms"] is not None
    assert result["status"] == "collecting"
    assert result["pending"]["unresolved_full_fills"] == 2


def _make_partial(
    row: dict[str, Any], *, shares: float, price: float, winner: str | None
) -> None:
    fee = charged_fee(price, shares)
    total_cost = price * shares + fee
    won = winner == row["buy_side"] if winner in {"Up", "Down"} else None
    pnl = ((shares if won else 0.0) - total_cost) if won is not None else None
    row.update(
        filled_shares=shares,
        fill_vwap=price,
        fill_levels=[{"price": price, "shares": shares}],
        fee=fee,
        total_cost=total_cost,
        winner=winner,
        won=won,
        pnl_per_share=pnl / shares if pnl is not None else None,
        pnl=pnl,
        filled=True,
        full_fill=False,
        sent=True,
        reason="partial_fill",
    )


def test_terminal_ev_includes_every_partial_fill_before_the_common_cutoff(
    passing_rows: list[dict[str, Any]],
) -> None:
    baseline = forward.evaluate(passing_rows, frozen())
    rows = copy.deepcopy(passing_rows)
    partial = signal_rows(
        30_000,
        market_id="partial-loss",
        decision_ms=START_MS + 1.0,
        winner="Down",
    )
    for row in partial:
        _make_partial(row, shares=2.0, price=0.40, winner="Down")
    rows.extend(partial)

    result = forward.evaluate(rows, frozen())
    partial_pnl = float(partial[0]["pnl"])

    assert result["status"] == "passed"
    for latency in ("400", "500"):
        assert result["base"][latency]["fills"] == 1_001
        assert result["base"][latency]["full_fills"] == 1_000
        assert result["base"][latency]["filled_shares"] == pytest.approx(5_002.0)
        assert result["base"][latency]["pnl"] == pytest.approx(
            baseline["base"][latency]["pnl"] + partial_pnl
        )
        assert (
            result["base"][latency]["net_ev_per_share"]
            < baseline["base"][latency]["net_ev_per_share"]
        )


def test_unresolved_partial_fill_before_cutoff_cannot_terminalize(
    passing_rows: list[dict[str, Any]],
) -> None:
    rows = copy.deepcopy(passing_rows)
    partial = signal_rows(
        30_001,
        market_id="partial-unresolved",
        decision_ms=START_MS + 2.0,
        winner=None,
    )
    for row in partial:
        _make_partial(row, shares=2.0, price=0.40, winner=None)
    rows.extend(partial)

    result = forward.evaluate(rows, frozen())

    assert result["status"] == "collecting"
    assert result["pending"]["unresolved_fills"] == 2


def test_control_requires_seven_utc_days_even_with_one_thousand_full_fills(
    passing_rows: list[dict[str, Any]],
) -> None:
    rows = copy.deepcopy(passing_rows)
    for row in rows:
        if (
            row["variant"] == "reverse_order"
            and float(row["decision_ms"]) >= START_MS + 6 * DAY_MS
        ):
            for field in (
                "decision_ms",
                "decision_source_ms",
                "exhausted_ms",
                "exhausted_source_ms",
                "evaluation_recv_ms",
                "book_recv_ms",
                "book_source_ms",
            ):
                row[field] = float(row[field]) - DAY_MS

    result = forward.evaluate(rows, frozen())

    assert result["status"] == "collecting"
    assert result["common_cutoff_ms"] is None
    assert result["control_progress"]["reverse_order"]["400"] == {
        "full_fills": 1_000,
        "days": 6,
    }
    assert result["control_progress"]["reverse_order"]["500"] == {
        "full_fills": 1_000,
        "days": 6,
    }


def test_point_estimate_control_advantage_needs_positive_clustered_lower_bound(
    passing_rows: list[dict[str, Any]],
) -> None:
    rows = copy.deepcopy(passing_rows)
    for row in rows:
        day = int((float(row["decision_ms"]) - START_MS) // DAY_MS)
        if row["variant"] != "reverse_order" or day > 3:
            continue
        price = 0.10
        fee = charged_fee(price)
        total_cost = price * 5.0 + fee
        pnl = 5.0 - total_cost
        row.update(
            fill_vwap=price,
            fill_levels=[{"price": price, "shares": 5.0}],
            fee=fee,
            total_cost=total_cost,
            winner="Up",
            won=True,
            pnl_per_share=pnl / 5.0,
            pnl=pnl,
        )

    result = forward.evaluate(rows, frozen())

    comparison = result["comparisons"]["400"]["reverse_order"]
    assert comparison["net_ev_difference_per_share"] > 0
    assert comparison["day_cluster_lower_99"] <= 0
    assert result["checks"]["400_above_reverse_order"] is False
    assert result["status"] == "rejected"


def test_rejects_pre_cutoff_and_same_batch_rows() -> None:
    rows = signal_rows(0)
    rows[0]["exhausted_ms"] = float(frozen()["prospective_cutoff_ms"]) - 1.0
    with pytest.raises(ValueError, match="frozen cutoff"):
        forward.validate_rows(rows, frozen(), ARTIFACT, **admission_kwargs())

    diagnostic = signal_rows(0)[0]
    diagnostic["variant"] = "same_batch"
    with pytest.raises(ValueError, match="same_batch"):
        forward.validate_rows(
            [diagnostic], frozen(), ARTIFACT, **admission_kwargs()
        )


def test_admission_binds_rows_to_utc_day_and_artifact_window() -> None:
    bounds = admission_kwargs(
        started_ms=START_MS - 200.0,
        ended_ms=START_MS + 1_000.0,
    )

    decorated = forward.validate_rows(
        signal_rows(0), frozen(), ARTIFACT, **bounds
    )

    assert {row["_artifact_utc_day"] for row in decorated} == {
        bounds["expected_utc_day"]
    }
    assert {row["_artifact_start_ms"] for row in decorated} == {
        bounds["artifact_start_ms"]
    }
    assert {row["_artifact_end_ms"] for row in decorated} == {
        bounds["artifact_end_ms"]
    }

    wrong_day = dict(bounds)
    wrong_day["expected_utc_day"] = "2027-01-16"
    with pytest.raises(ValueError, match="UTC day"):
        forward.validate_rows(signal_rows(0), frozen(), ARTIFACT, **wrong_day)


@pytest.mark.parametrize(
    ("field", "outside"),
    [
        ("exhausted_ms", START_MS - 201.0),
        ("exhausted_source_ms", START_MS - 201.0),
        ("decision_ms", START_MS + 1_001.0),
        ("decision_source_ms", START_MS - 201.0),
        ("evaluation_recv_ms", START_MS + 1_001.0),
        ("book_recv_ms", START_MS - 201.0),
        ("book_source_ms", START_MS + 1_001.0),
    ],
)
def test_admission_rejects_every_row_clock_outside_artifact_bounds(
    field: str,
    outside: float,
) -> None:
    rows = signal_rows(0)
    rows[0][field] = outside

    with pytest.raises(ValueError, match=f"artifact.*{field}"):
        forward.validate_rows(
            rows,
            frozen(),
            ARTIFACT,
            **admission_kwargs(
                started_ms=START_MS - 200.0,
                ended_ms=START_MS + 1_000.0,
            ),
        )


def test_admission_allows_only_scheduled_right_censor_clock_past_artifact_end() -> None:
    rows = signal_rows(0, decision_ms=START_MS)
    for row in rows:
        row.update(
            filled_shares=0.0,
            fill_vwap=None,
            fill_levels=[],
            fee=None,
            total_cost=None,
            won=None,
            pnl_per_share=None,
            pnl=None,
            filled=False,
            full_fill=False,
            sent=False,
            reason="right_censored",
            book_recv_ms=START_MS - 1.0,
            book_source_ms=START_MS - 2.0,
        )
    bounds = admission_kwargs(
        started_ms=START_MS - 200.0,
        ended_ms=START_MS + 100.0,
    )

    admitted = forward.validate_rows(rows, frozen(), ARTIFACT, **bounds)
    assert len(admitted) == 2

    not_censored = copy.deepcopy(rows)
    for row in not_censored:
        row.update(sent=True, reason="ask_above_frozen_limit")
    with pytest.raises(ValueError, match="artifact.*evaluation_recv_ms"):
        forward.validate_rows(not_censored, frozen(), ARTIFACT, **bounds)


def test_artifact_end_known_censor_before_cutoff_cannot_be_skipped(
    passing_rows: list[dict[str, Any]],
) -> None:
    rows = copy.deepcopy(passing_rows)
    cutoff = float(forward.evaluate(rows, frozen())["common_cutoff_ms"])
    censored = signal_rows(
        31_000,
        market_id="known-censor",
        decision_ms=cutoff - 100.0,
    )
    for row in censored:
        row.update(
            filled_shares=0.0,
            fill_vwap=None,
            fill_levels=[],
            fee=None,
            total_cost=None,
            won=None,
            pnl_per_share=None,
            pnl=None,
            filled=False,
            full_fill=False,
            sent=False,
            reason="right_censored",
            book_recv_ms=cutoff - 101.0,
            book_source_ms=cutoff - 102.0,
        )
    decorated = forward.validate_rows(
        censored,
        frozen(),
        "b" * 64,
        **admission_kwargs(
            cutoff - 100.0,
            started_ms=cutoff - 1_000.0,
            ended_ms=cutoff - 50.0,
        ),
    )

    result = forward.evaluate([*rows, *decorated], frozen())

    assert result["common_cutoff_ms"] == cutoff
    assert result["status"] == "collecting"
    assert result["pending"]["right_censored_base"] == 2


def test_decision_fee_is_recomputed_from_exact_executable_levels() -> None:
    rows = signal_rows(0)
    levels = [(0.39, 2.0), (0.40, 3.0)]
    decision_vwap = 0.396
    decision_fee = charged_level_fee(levels)
    assert decision_fee != charged_fee(decision_vwap)
    for row in rows:
        row.update(
            decision_vwap=decision_vwap,
            decision_levels=[
                {"price": price, "shares": shares} for price, shares in levels
            ],
            fixed_limit=0.40,
            decision_fee=decision_fee,
            net_edge=float(row["fair"]) * 5.0
            - decision_vwap * 5.0
            - decision_fee,
        )

    forward.validate_rows(
        rows, frozen(), ARTIFACT, **admission_kwargs()
    )

    for row in rows:
        row["decision_fee"] = charged_fee(decision_vwap)
        row["net_edge"] = (
            float(row["fair"]) * 5.0
            - decision_vwap * 5.0
            - float(row["decision_fee"])
        )
    with pytest.raises(ValueError, match="decision fee.*venue truncation"):
        forward.validate_rows(
            rows, frozen(), ARTIFACT, **admission_kwargs()
        )


def test_rejects_missing_pair_row_drift_and_colliding_ids() -> None:
    pair = signal_rows(0)
    with pytest.raises(ValueError, match="400/500"):
        forward.validate_rows(
            pair[:1], frozen(), ARTIFACT, **admission_kwargs()
        )

    drift = copy.deepcopy(pair)
    decision_fee = charged_fee(0.41)
    drift[1].update(
        decision_vwap=0.41,
        decision_levels=[{"price": 0.41, "shares": 5.0}],
        fixed_limit=0.41,
        decision_fee=decision_fee,
        net_edge=float(drift[1]["fair"]) * 5.0 - 0.41 * 5.0 - decision_fee,
    )
    with pytest.raises(ValueError, match="paired.*drift"):
        forward.validate_rows(drift, frozen(), ARTIFACT, **admission_kwargs())

    collision = [*pair, *signal_rows(0, variant="reverse_order", market_id="m0000")]
    for row in collision[2:]:
        row["signal_id"] = pair[0]["signal_id"]
    with pytest.raises(ValueError, match="collid"):
        forward.validate_rows(
            collision, frozen(), ARTIFACT, **admission_kwargs()
        )


def test_duplicate_payloads_are_idempotent_but_conflicts_are_fatal() -> None:
    raw = signal_rows(0)
    decorated = forward.validate_rows(
        [*raw, copy.deepcopy(raw[0])],
        frozen(),
        ARTIFACT,
        **admission_kwargs(),
    )
    assert len(decorated) == 2
    assert forward.merge(decorated, copy.deepcopy(decorated)) == decorated
    assert decorated[0]["_observation_key"].startswith(f"family34:{ARTIFACT}:")
    assert (
        forward.observation_key(decorated[0], "b" * 64)
        != decorated[0]["_observation_key"]
    )

    conflict = copy.deepcopy(decorated)
    conflict[0]["pnl"] = float(conflict[0]["pnl"]) + 0.01
    with pytest.raises(ValueError, match="conflicting duplicate"):
        forward.merge(decorated, conflict)

    conflicting_raw = [*raw, copy.deepcopy(raw[0])]
    conflicting_raw[-1]["book_recv_ms"] = (
        float(conflicting_raw[-1]["book_recv_ms"]) - 1.0
    )
    with pytest.raises(ValueError, match="conflicting duplicate"):
        forward.validate_rows(
            conflicting_raw, frozen(), ARTIFACT, **admission_kwargs()
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("fee", 0.0, "fee"),
        ("total_cost", 0.0, "total cost"),
        ("pnl", 0.0, "PnL"),
        ("filled_shares", 4.0, "fill levels"),
        ("fair", float("nan"), "non-finite"),
    ],
)
def test_recomputes_fee_fill_depth_and_pnl(
    field: str,
    value: object,
    message: str,
) -> None:
    rows = signal_rows(0)
    rows[0][field] = value

    with pytest.raises(ValueError, match=message):
        forward.validate_rows(rows, frozen(), ARTIFACT, **admission_kwargs())


def test_rejects_fee_rate_drift_even_when_economics_are_self_consistent() -> None:
    rows = signal_rows(0)
    rate = 0.06
    for row in rows:
        decision_fee = charged_fee(float(row["decision_vwap"]), rate=rate)
        fill_fee = charged_fee(float(row["fill_vwap"]), rate=rate)
        total_cost = float(row["fill_vwap"]) * 5.0 + fill_fee
        pnl = 5.0 - total_cost
        row.update(
            fee_rate=rate,
            decision_fee=decision_fee,
            net_edge=float(row["fair"]) * 5.0
            - float(row["decision_vwap"]) * 5.0
            - decision_fee,
            fee=fill_fee,
            total_cost=total_cost,
            pnl_per_share=pnl / 5.0,
            pnl=pnl,
        )

    with pytest.raises(ValueError, match="historical fee rate"):
        forward.validate_rows(rows, frozen(), ARTIFACT, **admission_kwargs())


@pytest.mark.parametrize(
    ("decision_ms", "accepted_rate", "rejected_rate"),
    [
        (replay._FEE_RATE_072_UNTIL_MS - 501.0, 0.072, 0.07),
        (replay._FEE_RATE_072_UNTIL_MS + 101.0, 0.07, 0.072),
    ],
)
def test_fee_rate_switch_is_pinned_to_the_decision_clock(
    decision_ms: float,
    accepted_rate: float,
    rejected_rate: float,
) -> None:
    config = frozen(cutoff_ms=replay._FEE_RATE_072_UNTIL_MS - 1_000.0)
    forward.validate_rows(
        signal_rows(0, decision_ms=decision_ms, fee_rate=accepted_rate),
        config,
        ARTIFACT,
        **admission_kwargs(decision_ms),
    )

    with pytest.raises(ValueError, match="historical fee rate"):
        forward.validate_rows(
            signal_rows(0, decision_ms=decision_ms, fee_rate=rejected_rate),
            config,
            ARTIFACT,
            **admission_kwargs(decision_ms),
        )


def test_rejects_orders_below_the_venue_minimum() -> None:
    rows = signal_rows(0)
    for row in rows:
        decision_fee = charged_fee(0.19)
        row.update(
            decision_vwap=0.19,
            decision_levels=[{"price": 0.19, "shares": 5.0}],
            fixed_limit=0.19,
            decision_fee=decision_fee,
            net_edge=float(row["fair"]) * 5.0 - 0.19 * 5.0 - decision_fee,
        )

    with pytest.raises(ValueError, match="venue minimum"):
        forward.validate_rows(rows, frozen(), ARTIFACT, **admission_kwargs())


def test_update_uses_shared_atomic_jsonl_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "rows.jsonl"
    write_jsonl(source, signal_rows(0))
    state = tmp_path / "state"
    writes: list[tuple[Path, list[dict[str, Any]]]] = []
    shared_writer = forward.h_forward._write_rows

    def observed_write(
        path: Path,
        rows: Iterable[dict[str, Any]],
    ) -> None:
        materialized = list(rows)
        writes.append((path, copy.deepcopy(materialized)))
        shared_writer(path, materialized)

    monkeypatch.setattr(forward.h_forward, "_write_rows", observed_write)

    result = forward.update(
        source,
        state,
        frozen=frozen(),
        artifact_sha256=ARTIFACT,
        **admission_kwargs(),
    )

    ledger = state / forward.LEDGER
    persisted = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert result["status"] == "collecting"
    assert len(writes) == 1
    assert writes[0][1] == persisted
    assert not ledger.with_name(ledger.name + ".tmp").exists()


def test_terminal_state_is_hashed_idempotent_and_rejects_mutation(
    tmp_path: Path,
    passing_rows: list[dict[str, Any]],
) -> None:
    state = tmp_path / "state"
    first: dict[str, Any] = {}
    last_source = tmp_path / "rows-6.jsonl"
    for day_index in range(7):
        source = tmp_path / f"rows-{day_index}.jsonl"
        day_start = START_MS + day_index * DAY_MS
        day_end = day_start + DAY_MS
        day_rows = [
            row
            for row in passing_rows
            if day_start <= float(row["decision_ms"]) < day_end
        ]
        write_jsonl(source, day_rows)
        first = forward.update(
            source,
            state,
            frozen=frozen(),
            artifact_sha256=f"{day_index + 1:064x}",
            **admission_kwargs(day_start),
        )
        last_source = source
    second = forward.update(
        last_source,
        state,
        frozen=frozen(),
        artifact_sha256=f"{7:064x}",
        **admission_kwargs(START_MS + 6 * DAY_MS),
    )

    assert first == second
    assert first["status"] == "passed"
    status = json.loads((state / forward.STATUS).read_text(encoding="utf-8"))
    verdict = json.loads((state / forward.VERDICT).read_text(encoding="utf-8"))
    assert len(status["status_sha256"]) == 64
    assert len(verdict["verdict_sha256"]) == 64
    assert verdict["status_sha256"] == status["status_sha256"]

    extra = signal_rows(99_999, decision_ms=START_MS + 8 * DAY_MS)
    extra_path = tmp_path / "extra.jsonl"
    write_jsonl(extra_path, extra)
    with pytest.raises(ValueError, match="terminal.*mutation"):
        forward.update(
            extra_path,
            state,
            frozen=frozen(),
            artifact_sha256="b" * 64,
            **admission_kwargs(START_MS + 8 * DAY_MS),
        )


def test_collecting_state_pins_cutoff_and_status_hash(tmp_path: Path) -> None:
    source = tmp_path / "rows.jsonl"
    write_jsonl(source, signal_rows(0))
    state = tmp_path / "state"
    forward.update(
        source,
        state,
        frozen=frozen(),
        artifact_sha256=ARTIFACT,
        **admission_kwargs(),
    )

    drifted = frozen()
    drifted["prospective_cutoff_ms"] = float(drifted["prospective_cutoff_ms"]) - 1.0
    with pytest.raises(ValueError, match="freeze/config drift"):
        forward.update(
            source,
            state,
            frozen=drifted,
            artifact_sha256=ARTIFACT,
            **admission_kwargs(),
        )

    status_path = state / forward.STATUS
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status["ledger_sha256"] = "0" * 64
    status_path.write_text(json.dumps(status), encoding="utf-8")
    with pytest.raises(ValueError, match="status hash mismatch"):
        forward.update(
            source,
            state,
            frozen=frozen(),
            artifact_sha256=ARTIFACT,
            **admission_kwargs(),
        )


@pytest.mark.parametrize(
    "crash_hook",
    ["_crash_after_ledger_commit", "_crash_after_status_commit"],
)
def test_direct_update_recovers_an_interrupted_commit(
    tmp_path: Path,
    crash_hook: str,
) -> None:
    state = tmp_path / "state"
    first_source = tmp_path / "first.jsonl"
    second_source = tmp_path / "second.jsonl"
    write_jsonl(first_source, signal_rows(0))
    write_jsonl(
        second_source,
        signal_rows(1, decision_ms=START_MS + DAY_MS),
    )
    forward.update(
        first_source,
        state,
        frozen=frozen(),
        artifact_sha256="1" * 64,
        **admission_kwargs(),
    )

    crash_kwargs: dict[str, Any] = {crash_hook: True}
    with pytest.raises(RuntimeError, match="simulated crash"):
        forward.update(
            second_source,
            state,
            frozen=frozen(),
            artifact_sha256="2" * 64,
            **admission_kwargs(START_MS + DAY_MS),
            **crash_kwargs,
        )

    assert (state / forward.JOURNAL).is_file()
    recovered = forward.update(
        second_source,
        state,
        frozen=frozen(),
        artifact_sha256="2" * 64,
        **admission_kwargs(START_MS + DAY_MS),
    )
    persisted = (state / forward.LEDGER).read_text(encoding="utf-8").splitlines()
    assert recovered["status"] == "collecting"
    assert len(persisted) == 4
    assert not (state / forward.JOURNAL).exists()
