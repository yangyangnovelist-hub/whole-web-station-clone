from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

import wallet_lead as lead


START = int(datetime(2026, 8, 17, tzinfo=UTC).timestamp())
OBS_START = int(datetime(2026, 8, 10, 12, tzinfo=UTC).timestamp())
MARKET = "condition-1"
UP = "up-token"
DOWN = "down-token"
WALLET = "0xAaBb"


def _tokens() -> dict[str, lead.TokenMeta]:
    return {
        UP: lead.TokenMeta(date(2026, 8, 17), START, MARKET, UP, "Up", True),
        DOWN: lead.TokenMeta(date(2026, 8, 17), START, MARKET, DOWN, "Down", True),
    }


def test_token_meta_accepts_scanner_field_aliases_and_parent_buy_is_plain_dataclass():
    token = lead.TokenMeta(
        market_day=date(2026, 8, 17),
        market_start_s=START,
        market_id=MARKET,
        token_id=UP,
        side="Up",
        up_won=True,
    )

    assert (token.day, token.start_s, token.market, token.token) == (
        token.market_day,
        token.market_start_s,
        token.market_id,
        token.token_id,
    )
    event = lead.normalise_parent_buy(_row(), {UP: token})
    assert event.__dict__["event_key"] == event.event_key


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "timestamp": START + 61,
        "block_number": 61_000_001,
        "transaction_hash": "0x" + "ab" * 32,
        "log_index": 7,
        "contract": "CTF_EXCHANGE_V2",
        "order_hash": "0x" + "cd" * 32,
        "maker": WALLET,
        "taker": lead.CTF_EXCHANGE_V2_ADDRESS,
        "side": 0,
        "token_id": UP,
        "maker_amount_filled": "2000000",
        "taker_amount_filled": "5000000",
        "fee": "84000",
    }
    row.update(overrides)
    return row


def test_normalise_parent_buy_preserves_exact_v2_identity_and_units():
    event = lead.normalise_parent_buy(_row(), _tokens())

    assert event.wallet == WALLET.lower()
    assert event.side == "Up"
    assert event.shares == Decimal("5.000000")
    assert event.price == Decimal("0.4")
    assert event.fee == Decimal("0.084000")
    assert event.event_key == (
        137,
        lead.CTF_EXCHANGE_V2_ADDRESS.lower(),
        "0x" + "ab" * 32,
        7,
    )
    assert event.order_hash == "0x" + "cd" * 32
    assert event.source_event_id == "0x" + "ab" * 32
    assert event.transaction_hash == "0x" + "ab" * 32
    assert event.block_number == 61_000_001
    assert event.log_index == 7


def _daily_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "137_61000001_7",
        "order_hash": "0x" + "ef" * 32,
        "maker": WALLET,
        "taker": lead.CTF_EXCHANGE_V2_ADDRESS,
        "timestamp": START + 61,
        "maker_direction": "BUY",
        "taker_direction": "SELL",
        "token_asset_id": UP,
        "token_amount": 54.147691,
        "usdc_amount": 14.010011,
        "price": 0.258737,
        "fee_usdc": 0.72695,
    }
    row.update(overrides)
    return row


def test_normalise_daily_parent_buy_preserves_daily_event_identity_and_actual_fee():
    event = lead.normalise_daily_parent_buy(_daily_row(), _tokens())

    assert event.wallet == WALLET.lower()
    assert event.side == "Up"
    assert event.shares == Decimal("54.147691")
    assert event.price == Decimal(
        "0.25873699766810001187308245516877164716035629293962"
    )
    assert event.fee == Decimal("0.72695")
    assert event.source_event_id == "137_61000001_7"
    assert event.event_key == (
        137,
        lead.CTF_EXCHANGE_V2_ADDRESS.lower(),
        "137_61000001_7",
        7,
    )
    assert event.transaction_hash is None
    assert event.order_hash == "0x" + "ef" * 32
    assert event.block_number == 61_000_001


def test_normalise_daily_parent_buy_accepts_event_fee_that_differs_from_current_formula():
    event = lead.normalise_daily_parent_buy(
        _daily_row(
            token_amount=5.5,
            usdc_amount=2.86,
            price=0.52,
            fee_usdc=0.11754,
        ),
        _tokens(),
    )

    assert event.fee == Decimal("0.11754")
    assert event.fee != lead.charged_taker_fee(event.shares, event.price)
    assert lead.opposite_signal(event).fee == event.fee


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": "137_bad_7"},
        {"taker": "0xmaker-child"},
        {"maker_direction": "SELL", "taker_direction": "BUY"},
        {"token_asset_id": "unknown"},
        {"token_amount": 0.0},
        {"token_amount": 5.0000001},
        {"usdc_amount": 2.0000001},
        {"price": 0.3},
        {"fee_usdc": -0.01},
        {"fee_usdc": 0.0000001},
        {"fee_usdc": 20.0},
    ],
)
def test_normalise_daily_parent_buy_rejects_nonparent_or_impossible_rows(overrides):
    with pytest.raises(ValueError):
        lead.normalise_daily_parent_buy(_daily_row(**overrides), _tokens())


@pytest.mark.parametrize(
    "overrides,tokens",
    [
        ({"taker": "0xwallet"}, _tokens()),
        ({"side": 1}, _tokens()),
        ({"token_id": "unknown"}, _tokens()),
        ({"maker_amount_filled": "0"}, _tokens()),
        ({"maker_amount_filled": "5000000"}, _tokens()),
        ({"maker_amount_filled": "2.0"}, _tokens()),
        ({"fee": "83000"}, _tokens()),
    ],
)
def test_normalise_parent_buy_rejects_every_noneligible_or_inexact_row(
    overrides, tokens
):
    with pytest.raises(ValueError):
        lead.normalise_parent_buy(_row(**overrides), tokens)


def test_normalise_parent_buy_keeps_sub_five_share_first_buys():
    event = lead.normalise_parent_buy(
        _row(
            maker_amount_filled="400000",
            taker_amount_filled="1000000",
            fee="16800",
        ),
        _tokens(),
    )

    assert event.shares == Decimal("1.000000")
    assert event.price == Decimal("0.4")


def test_normalise_parent_buy_rejects_conflicting_complement_metadata():
    tokens = _tokens()
    tokens[DOWN] = replace(tokens[DOWN], up_won=False)

    with pytest.raises(ValueError, match="settlement"):
        lead.normalise_parent_buy(_row(), tokens)


def test_charged_taker_fee_truncates_the_total_to_five_decimals():
    assert lead.charged_taker_fee(Decimal("5"), Decimal("0.333333")) == Decimal(
        "0.07777"
    )
    assert lead.charged_taker_fee(Decimal("5"), Decimal("0.4")) == Decimal("0.08400")

    with pytest.raises(ValueError, match="exact decimal"):
        lead.charged_taker_fee(5.0, Decimal("0.4"))


def _observation(
    wallet: str,
    market: str,
    *,
    day: date = date(2026, 8, 10),
    timestamp_s: int | None = None,
    price: str = "0.5",
    settlement: str = "1",
    shares: str = "5",
) -> lead.WalletObservation:
    if timestamp_s is None:
        timestamp_s = int(
            datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp()
        )
    return lead.WalletObservation(
        day=day,
        wallet=wallet,
        market=market,
        timestamp_s=timestamp_s,
        price=Decimal(price),
        shares=Decimal(shares),
        fee=lead.charged_taker_fee(Decimal(shares), Decimal(price)),
        settlement=Decimal(settlement),
    )


def test_select_wallets_uses_first_buy_in_14_settlement_safe_full_days():
    observations = [
        _observation("0xWinner", f"market-{index}", timestamp_s=OBS_START + index)
        for index in range(20)
    ]
    observations += [
        _observation(
            "0xWinner",
            "market-0",
            timestamp_s=OBS_START - 1,
            price="0.75",
        ),
        _observation(
            "0xWinner",
            "old-market",
            day=date(2026, 8, 1),
            settlement="0",
        ),
        _observation(
            "0xWinner",
            "yesterday-market",
            day=date(2026, 8, 16),
            settlement="0",
        ),
        _observation(
            "0xWinner",
            "today-market",
            day=date(2026, 8, 17),
            settlement="0",
        ),
    ]

    selected = lead.select_wallets(observations, date(2026, 8, 17))

    assert len(selected) == 1
    assert selected[0].wallet == "0xwinner"
    assert selected[0].n == 20
    assert selected[0].pnl_sum == Decimal("9.404376")
    assert selected[0].score == Decimal("0.2351094")
    assert selected[0].asof_day == date(2026, 8, 17)


def test_select_wallets_requires_20_markets_caps_at_five_and_sorts_ties_by_wallet():
    observations = []
    for wallet, price in (
        ("0xbbb", "0.1"),
        ("0xaaa", "0.1"),
        ("0xccc", "0.2"),
        ("0xddd", "0.3"),
        ("0xeee", "0.4"),
        ("0xfff", "0.5"),
    ):
        observations.extend(
            _observation(wallet, f"market-{index}", price=price) for index in range(20)
        )
    observations.extend(
        _observation("0x000", f"short-{index}", price="0.01") for index in range(19)
    )
    observations.extend(
        _observation(
            "0xLow",
            f"low-{index}",
            price="0.5",
            settlement="1" if index < 11 else "0",
        )
        for index in range(20)
    )

    selected = lead.select_wallets(observations, date(2026, 8, 17))

    assert [item.wallet for item in selected] == [
        "0xaaa",
        "0xbbb",
        "0xccc",
        "0xddd",
        "0xeee",
    ]
    assert selected[0].score == selected[1].score


def test_select_wallets_orders_same_second_buys_by_block_then_log_index():
    earlier = lead.normalise_parent_buy(
        _row(
            timestamp=START + 100,
            block_number=100,
            log_index=99,
            transaction_hash="0x" + "ff" * 32,
            maker_amount_filled="3750000",
            fee="65620",
        ),
        _tokens(),
    )
    later = lead.normalise_parent_buy(
        _row(
            timestamp=START + 100,
            block_number=101,
            log_index=1,
            transaction_hash="0x" + "00" * 32,
            maker_amount_filled="2500000",
            fee="87500",
        ),
        _tokens(),
    )
    observations = [_observation(WALLET, f"other-{index}") for index in range(19)] + [
        lead.WalletObservation.from_parent_buy(later),
        lead.WalletObservation.from_parent_buy(earlier),
    ]

    selected = lead.select_wallets(observations, date(2026, 8, 19))

    assert selected[0].pnl_sum == Decimal("9.404376")
    assert selected[0].score == Decimal("0.2351094")


def test_select_wallets_uses_each_leaders_actual_historical_fee():
    observations = [
        lead.WalletObservation(
            day=date(2026, 8, 10),
            wallet=WALLET,
            market=f"actual-fee-{index}",
            timestamp_s=OBS_START + index,
            price=Decimal("0.5"),
            shares=Decimal("5"),
            fee=Decimal("0.5"),
            settlement=Decimal("1"),
        )
        for index in range(20)
    ]

    selected = lead.select_wallets(observations, date(2026, 8, 19))

    assert selected[0].pnl_sum == Decimal("8.0")
    assert selected[0].score == Decimal("0.2")


def test_observable_time_rounds_to_second_end_and_fixed_limit_uses_total_fee_rounding():
    assert lead.observable_ms(START + 61) == (START + 62) * 1000
    assert lead.fixed_limit(Decimal("0.4"), Decimal("0.03")) == Decimal("0.41")
    assert lead.fixed_limit(Decimal("0.99"), Decimal("0.5")) == Decimal("0.98")
    assert lead.fixed_limit(Decimal("0.01"), Decimal("-0.02")) is None
    assert lead.fixed_limit(
        Decimal("0.22"), Decimal("0.0721"), shares=Decimal("5.01")
    ) == Decimal("0.27")

    with pytest.raises(ValueError, match="integer second"):
        lead.observable_ms(1.2)
    assert lead.fixed_limit(
        Decimal("0.4"), Decimal("0.03"), shares=Decimal("0.5")
    ) == Decimal("0.41")
    assert lead.fixed_limit(
        Decimal("0.52"),
        Decimal("0.03"),
        shares=Decimal("5.5"),
        signal_fee=Decimal("0.05"),
    ) == Decimal("0.53")


def _score(wallet: str = WALLET, score: str = "0.03") -> lead.WalletScore:
    exact_score = Decimal(score)
    return lead.WalletScore(
        asof_day=date(2026, 8, 17),
        wallet=wallet,
        score=exact_score,
        n=20,
        pnl_sum=exact_score * 40,
    )


def test_first_market_signal_uses_observation_window_and_first_selected_wallet_event():
    events = [
        lead.normalise_parent_buy(
            _row(timestamp=START + 58, log_index=1, maker=WALLET), _tokens()
        ),
        lead.normalise_parent_buy(
            _row(timestamp=START + 60, log_index=2, maker="0xNotSelected"), _tokens()
        ),
        lead.normalise_parent_buy(
            _row(timestamp=START + 100, log_index=3, maker=WALLET), _tokens()
        ),
        lead.normalise_parent_buy(
            _row(timestamp=START + 59, log_index=4, maker=WALLET), _tokens()
        ),
        lead.normalise_parent_buy(
            _row(timestamp=START + 280, log_index=5, maker=WALLET), _tokens()
        ),
    ]

    signal = lead.first_market_signal(events, (_score(),), START)

    assert signal is not None
    assert signal.timestamp_s == START + 59
    assert signal.log_index == 4
    assert lead.first_market_signal(events, (_score("0xAbsent"),), START) is None

    with pytest.raises(ValueError, match="duplicate event key"):
        lead.first_market_signal([events[2], events[2]], (_score(),), START)


def _snapshot(timestamp_ms: int, **overrides: object) -> lead.BookSnapshot:
    values: dict[str, object] = {
        "timestamp_ms": timestamp_ms,
        "up_ask": Decimal("0.4"),
        "up_ask_size": Decimal("5"),
        "down_ask": Decimal("0.59"),
        "down_ask_size": Decimal("5"),
        "active": True,
        "halted": False,
        "market": MARKET,
    }
    values.update(overrides)
    return lead.BookSnapshot(**values)


def test_replay_fills_first_snapshot_at_most_100ms_late_and_charges_total_fee():
    signal = lead.normalise_parent_buy(_row(), _tokens())
    target_ms = lead.observable_ms(signal.timestamp_s) + 400
    snapshots = [
        _snapshot(target_ms - 100, up_ask=Decimal("0.01")),
        _snapshot(target_ms + 100),
        _snapshot(target_ms + 101, up_ask=Decimal("0.01")),
    ]

    result = lead.replay(signal, _score(), snapshots, 400)

    assert isinstance(result, lead.ReplayResult)
    assert result.variant == "base"
    assert result.target_ms == target_ms
    assert result.snapshot_ms == target_ms + 100
    assert result.limit_price == Decimal("0.41")
    assert result.fill_price == Decimal("0.4")
    assert result.shares == Decimal("5")
    assert result.fee == Decimal("0.08400")
    assert result.pnl == Decimal("2.91600")
    assert result.filled and result.reason == "filled"


def test_replay_uses_actual_leader_fee_for_limit_and_current_fee_for_our_fill():
    signal = lead.normalise_daily_parent_buy(
        _daily_row(
            token_amount=5.5,
            usdc_amount=2.86,
            price=0.52,
            fee_usdc=0.05,
        ),
        _tokens(),
    )
    target_ms = lead.observable_ms(signal.timestamp_s) + 400

    result = lead.replay(
        signal,
        _score(),
        [_snapshot(target_ms, up_ask=Decimal("0.53"))],
        400,
    )

    assert result.limit_price == Decimal("0.53")
    assert result.filled
    assert result.fee == Decimal("0.08718")


def test_replay_does_not_skip_an_unfresh_first_snapshot_for_a_later_healthy_one():
    signal = lead.normalise_parent_buy(_row(), _tokens())
    target_ms = lead.observable_ms(signal.timestamp_s) + 500
    result = lead.replay(
        signal,
        _score(),
        [
            _snapshot(target_ms, fresh=False),
            _snapshot(target_ms + 100, fresh=True),
        ],
        500,
    )

    assert not result.filled
    assert result.snapshot_ms == target_ms
    assert result.reason == "stale_book"
    assert result.shares == result.fee == result.pnl == Decimal(0)


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"active": False}, "inactive_book"),
        ({"halted": True}, "halted_book"),
        ({"fresh": False}, "stale_book"),
        ({"up_ask": None}, "missing_direct_ask"),
        ({"up_ask_size": Decimal("4.999999")}, "insufficient_depth"),
        ({"up_ask": Decimal("0.42")}, "ask_above_limit"),
    ],
)
def test_replay_records_each_book_gate_as_a_zero_pnl_nonfill(overrides, reason):
    signal = lead.normalise_parent_buy(_row(), _tokens())
    target_ms = lead.replay_target_ms(signal, 400)

    result = lead.replay(signal, _score(), [_snapshot(target_ms, **overrides)], 400)

    assert not result.filled
    assert result.reason == reason
    assert result.shares == result.fee == result.pnl == Decimal(0)


def test_replay_rejects_late_snapshots_and_nonfrozen_horizons():
    signal = lead.normalise_parent_buy(_row(), _tokens())
    target_ms = lead.replay_target_ms(signal, 400)

    result = lead.replay(signal, _score(), [_snapshot(target_ms + 101)], 400)

    assert not result.filled
    assert result.reason == "snapshot_too_late"
    assert result.snapshot_ms == target_ms + 101
    with pytest.raises(ValueError, match="400 or 500"):
        lead.replay(signal, _score(), [], 401)


def test_delay_and_opposite_controls_use_shifted_time_and_direct_complement_book():
    signal = lead.normalise_parent_buy(_row(), _tokens())
    delayed_target = lead.replay_target_ms(signal, 400, variant="delay_5s")

    delayed = lead.replay(
        signal,
        _score(),
        [_snapshot(delayed_target)],
        400,
        variant="delay_5s",
    )
    assert delayed.target_ms == lead.observable_ms(signal.timestamp_s) + 5_000 + 400
    assert delayed.filled

    opposite_signal = lead.opposite_signal(signal)
    assert opposite_signal.side == "Down"
    assert opposite_signal.token == DOWN
    assert opposite_signal.opposite_token == UP
    assert opposite_signal.price == Decimal("0.6")

    opposite_target = lead.replay_target_ms(signal, 500, variant="opposite")
    opposite = lead.replay(
        signal,
        _score(),
        [
            _snapshot(
                opposite_target,
                up_ask=Decimal("0.01"),
                down_ask=Decimal("0.59"),
            )
        ],
        500,
        variant="opposite",
    )
    assert opposite.token == DOWN and opposite.side == "Down"
    assert opposite.limit_price == Decimal("0.62")
    assert opposite.fill_price == Decimal("0.59")
    assert opposite.fee == Decimal("0.08466")
    assert opposite.pnl == Decimal("-3.03466")


def test_public_value_objects_reject_invalid_numeric_state():
    signal = lead.normalise_parent_buy(_row(), _tokens())

    with pytest.raises(ValueError, match="price"):
        replace(signal, price=Decimal(0))
    with pytest.raises(ValueError, match="event_key"):
        replace(signal, event_key=(*signal.event_key[:-1], signal.log_index + 1))
    with pytest.raises(ValueError, match="fresh"):
        replace(_snapshot(1), fresh=1)
