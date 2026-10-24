from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

import absorption_run as run
import pytest

DECISION_MS = 1_000.0


def _market() -> dict[str, object]:
    return {
        "kind": "market",
        "recv_ms": 1.0,
        "market_id": "m",
        "slot": 0,
        "up_token_id": "up",
        "down_token_id": "down",
    }


def _snapshot(
    recv_ms: float,
    token: str,
    *,
    bids: tuple[tuple[float, float], ...],
    asks: tuple[tuple[float, float], ...],
) -> dict[str, object]:
    return {
        "kind": "snapshot",
        "recv_ms": recv_ms,
        "market_id": "m",
        "asset_id": token,
        "bids": [{"price": price, "size": size} for price, size in bids],
        "asks": [{"price": price, "size": size} for price, size in asks],
    }


def _pair(
    recv_ms: float,
    *,
    down_bid: tuple[tuple[float, float], ...] = ((0.39, 10.0),),
    down_ask: tuple[tuple[float, float], ...] = ((0.40, 10.0),),
) -> list[dict[str, object]]:
    return [
        _snapshot(recv_ms, "up", bids=((0.59, 10.0),), asks=((0.60, 10.0),)),
        _snapshot(recv_ms, "down", bids=down_bid, asks=down_ask),
    ]


def _events(*extra: dict[str, object]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = [
        _market(),
        {"kind": "connection", "recv_ms": 2.0, "epoch": 1},
        *_pair(DECISION_MS),
        *extra,
    ]
    return sorted(rows, key=lambda row: float(row["recv_ms"]))


def _signal(**updates: object) -> dict[str, object]:
    signal: dict[str, object] = {
        "schema": "post-sweep-absorption-signal-v1",
        "signal_id": "s",
        "parent_signal_id": None,
        "variant": "base",
        "market_id": "m",
        "swept_asset_id": "up",
        "swept_side": "Up",
        "buy_asset_id": "down",
        "buy_side": "Down",
        "decision_recv_ms": DECISION_MS,
        "target_shares": 5.0,
        "fixed_limit": 0.40,
        "sent": True,
        "reason": "eligible",
    }
    signal.update(updates)
    return signal


def _fee(levels: tuple[tuple[float, float], ...], rate: float = 0.07) -> float:
    total = sum(
        Decimal(str(size)) * Decimal(str(rate)) * Decimal(str(price))
        * (Decimal(1) - Decimal(str(price)))
        for price, size in levels
    )
    return float(total.quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP))


def test_entry_uses_the_complete_exact_due_receipt_group_without_future_books() -> None:
    rows = run.replay_execution(
        _events(
            *_pair(1_400.0, down_bid=((0.35, 10.0),), down_ask=((0.38, 5.0),)),
            *_pair(1_400.1, down_bid=((0.35, 10.0),), down_ask=((0.37, 5.0),)),
            *_pair(1_500.0, down_bid=((0.35, 10.0),), down_ask=((0.36, 5.0),)),
        ),
        [_signal()],
        {"m": "Down"},
        recording_end_ms=2_000.0,
    )
    by_delay = {row["evaluation_ms"]: row for row in rows}

    assert by_delay[400.0]["entry_vwap"] == pytest.approx(0.38)
    assert by_delay[400.0]["entry_book_recv_ms"] == 1_400.0
    assert by_delay[500.0]["entry_vwap"] == pytest.approx(0.36)
    assert by_delay[500.0]["entry_book_recv_ms"] == 1_500.0
    assert {row["exit_reason"] for row in rows} == {"recording_end_hold"}
    assert all(row["pnl"] is not None for row in rows)
    assert all("scheduled_decision_recv_ms" not in row for row in rows)


def test_partial_entry_and_partial_exit_keep_residual_shares_in_settlement_pnl() -> None:
    rows = run.replay_execution(
        _events(
            *_pair(1_400.0, down_ask=((0.40, 2.0),)),
            *_pair(6_800.0, down_bid=((0.50, 1.0),), down_ask=((0.51, 5.0),)),
        ),
        [_signal()],
        {"m": "Down"},
        evaluation_ms=(400.0,),
        recording_end_ms=7_000.0,
    )
    row = rows[0]
    entry_fee = _fee(((0.40, 2.0),))
    exit_fee = _fee(((0.50, 1.0),))

    assert row["entry_reason"] == "partial_fill"
    assert row["entry_shares"] == pytest.approx(2.0)
    assert row["exit_reason"] == "partial_fill"
    assert row["exit_shares"] == pytest.approx(1.0)
    assert row["settled_shares"] == pytest.approx(1.0)
    assert row["entry_fee"] == pytest.approx(entry_fee)
    assert row["exit_fee"] == pytest.approx(exit_fee)
    assert row["pnl"] == pytest.approx((0.50 - exit_fee) + 1.0 - (0.80 + entry_fee))


def test_full_unwind_has_realized_pnl_without_a_market_outcome() -> None:
    rows = run.replay_execution(
        _events(
            *_pair(1_400.0, down_ask=((0.40, 5.0),)),
            *_pair(6_800.0, down_bid=((0.50, 5.0),), down_ask=((0.51, 5.0),)),
        ),
        [_signal()],
        {},
        evaluation_ms=(400.0,),
        recording_end_ms=7_000.0,
    )
    row = rows[0]

    assert row["exit_reason"] == "filled"
    assert row["settled_shares"] == 0.0
    assert row["winner"] is None
    assert row["pnl"] == pytest.approx(
        2.50 - _fee(((0.50, 5.0),)) - 2.00 - _fee(((0.40, 5.0),))
    )
    assert row["sent"] is True
    assert row["filled"] is True
    assert row["full_fill"] is True
    assert row["fee_rate"] == pytest.approx(0.07)
    assert row["pnl_per_share"] == pytest.approx(row["pnl"] / 5.0)
    assert row["profitable"] is True
    assert row["signal"]["signal_id"] == "s"


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        (({"kind": "disconnect", "recv_ms": 1_200.0, "epoch": 1},), "disconnect"),
        ((), "stale_book"),
    ],
)
def test_entry_fails_closed_on_disconnect_or_stale_book(
    extra: tuple[dict[str, object], ...], expected: str,
) -> None:
    rows = run.replay_execution(
        _events(*extra),
        [_signal()],
        {"m": "Down"},
        evaluation_ms=(400.0,),
        book_fresh_ms=100.0,
        recording_end_ms=2_000.0,
    )

    assert rows[0]["entry_reason"] == expected
    assert rows[0]["entry_shares"] == 0.0
    assert rows[0]["pnl"] == 0.0
    assert rows[0]["sent"] is True
    assert rows[0]["filled"] is False
    assert rows[0]["full_fill"] is False
    assert rows[0]["pnl_per_share"] is None
    assert rows[0]["profitable"] is None


def test_residual_position_without_an_outcome_is_explicitly_unresolved() -> None:
    rows = run.replay_execution(
        _events(*_pair(1_400.0, down_ask=((0.40, 2.0),))),
        [_signal()],
        {},
        evaluation_ms=(400.0,),
        recording_end_ms=2_000.0,
    )

    assert rows[0]["entry_shares"] == pytest.approx(2.0)
    assert rows[0]["settled_shares"] == pytest.approx(2.0)
    assert rows[0]["winner"] is None
    assert rows[0]["pnl"] is None


def test_direction_reversal_control_uses_the_direct_swept_token_ask() -> None:
    rows = run.replay_execution(
        _events(),
        [_signal()],
        {"m": "Up"},
        evaluation_ms=(400.0,),
        include_controls=True,
        recording_end_ms=2_000.0,
    )
    control = next(row for row in rows if row["variant"] == "direction_reversal")

    assert control["parent_signal_id"] == "s"
    assert control["signal_id"] == "s:direction_reversal"
    assert control["buy_asset_id"] == "up"
    assert control["buy_side"] == "Up"
    assert control["fixed_limit"] == pytest.approx(0.60)
    assert control["entry_vwap"] == pytest.approx(0.60)


def test_five_second_control_keeps_the_parent_limit_and_excludes_future_books() -> None:
    rows = run.replay_execution(
        _events(
            *_pair(
                6_000.0,
                down_bid=((0.35, 10.0),),
                down_ask=((0.38, 5.0),),
            ),
            *_pair(
                6_400.0,
                down_bid=((0.35, 10.0),),
                down_ask=((0.36, 5.0),),
            ),
            *_pair(
                6_400.1,
                down_bid=((0.34, 10.0),),
                down_ask=((0.35, 5.0),),
            ),
        ),
        [_signal()],
        {"m": "Down"},
        evaluation_ms=(400.0,),
        include_controls=True,
        recording_end_ms=7_000.0,
    )
    control = next(row for row in rows if row["variant"] == "time_shift")

    assert control["parent_signal_id"] == "s"
    assert control["decision_recv_ms"] == 6_000.0
    assert control["scheduled_decision_recv_ms"] == 6_000.0
    assert control["fixed_limit"] == pytest.approx(0.40)
    assert control["entry_match_ms"] == 6_400.0
    assert control["entry_book_recv_ms"] == 6_400.0
    assert control["entry_vwap"] == pytest.approx(0.36)


def test_five_second_control_does_not_use_depth_arriving_after_its_decision() -> None:
    rows = run.replay_execution(
        _events(
            *_pair(
                6_000.0,
                down_bid=((0.35, 10.0),),
                down_ask=((0.39, 4.0), (0.41, 10.0)),
            ),
            *_pair(
                6_400.0,
                down_bid=((0.35, 10.0),),
                down_ask=((0.38, 5.0),),
            ),
        ),
        [_signal()],
        {"m": "Down"},
        evaluation_ms=(400.0,),
        include_controls=True,
        recording_end_ms=7_000.0,
    )
    control = next(row for row in rows if row["variant"] == "time_shift")

    assert control["decision_recv_ms"] == 6_000.0
    assert control["entry_reason"] == "shifted_decision_insufficient_direct_depth"
    assert control["entry_shares"] == 0.0


def test_controls_and_base_execution_ignore_non_five_share_signal_size() -> None:
    rows = run.replay_execution(
        _events(),
        [_signal(target_shares=2.0)],
        {"m": "Down"},
        evaluation_ms=(400.0,),
        include_controls=True,
        recording_end_ms=2_000.0,
    )

    assert {row["target_shares"] for row in rows} == {5.0}
    assert {
        row["entry_shares"]
        for row in rows
        if row["variant"] in {"base", "direction_reversal"}
    } == {5.0}


def test_default_censored_base_keeps_the_original_terminal_reason() -> None:
    rows = run.replay_execution(
        _events(),
        [_signal(sent=False, reason="insufficient_direct_depth")],
        {"m": "Down"},
        evaluation_ms=(400.0,),
        recording_end_ms=1_100.0,
    )

    assert rows[0]["entry_reason"] == "recording_end_censored"
