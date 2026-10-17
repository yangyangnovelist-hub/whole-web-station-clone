from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import reward_dust_chain as chain


DAY0 = date(2026, 9, 1)
CHAIN_ID = 137
EXCHANGE = "0xexchange"
ABI = "ctf-exchange-v1"


def _day_start(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())


def _manifest(
    day: date,
    *,
    selected_rows: int = 0,
    parity_amount: Decimal = Decimal(0),
    parity_delta: Decimal = Decimal(0),
    fee_semantics_proven: bool = True,
) -> chain.DayManifest:
    start = _day_start(day)
    market_starts = tuple(start + 300 * i for i in range(288))
    token_ids = tuple(
        token
        for market_start in market_starts
        for token in (f"up-{market_start}", f"down-{market_start}")
    )
    return chain.DayManifest(
        market_day=day,
        capture_start_s=start,
        capture_end_s=start + 90_000,
        source_hashes=("a" * 64, "b" * 64),
        source_complete=True,
        truncated=False,
        finalized_ranges_complete=True,
        reorg_conflicts=0,
        identity_semantics_proven=True,
        chain_id=CHAIN_ID,
        allowed_exchange_contracts=frozenset({EXCHANGE}),
        supported_abi_versions=frozenset({ABI}),
        amount_semantics_proven=True,
        fee_semantics_proven=fee_semantics_proven,
        fragment_increment=Decimal("0.01"),
        fragment_precision_proven=True,
        zero_fee_threshold_usdc=Decimal("0.00001"),
        zero_fee_threshold_proven=True,
        raw_columns=chain.REQUIRED_RAW_COLUMNS,
        raw_rows=10_000,
        selected_rows=selected_rows,
        market_starts=market_starts,
        token_ids=token_ids,
        parity_checked=True,
        parity_metric=chain.PARITY_METRIC,
        parity_api_token_amount=parity_amount,
        parity_chain_token_amount=parity_amount + parity_delta,
        parity_abs_tolerance=Decimal("0.000001"),
    )


def _manifests(events: list[chain.ChainFill] | None = None) -> list[chain.DayManifest]:
    events = events or []
    rows_by_day: dict[date, int] = {}
    parent_shares_by_day: dict[date, Decimal] = {}
    seen: set[tuple[int, str, str, int]] = set()
    for event in events:
        if event.event_key is None or event.event_key in seen:
            continue
        seen.add(event.event_key)
        rows_by_day[event.market_day] = rows_by_day.get(event.market_day, 0) + 1
        if event.role == chain.AGGRESSOR_PARENT:
            parent_shares_by_day[event.market_day] = (
                parent_shares_by_day.get(event.market_day, Decimal(0)) + event.gross_shares
            )
    return [
        _manifest(
            DAY0 + timedelta(days=i),
            selected_rows=rows_by_day.get(DAY0 + timedelta(days=i), 0),
            parity_amount=parent_shares_by_day.get(DAY0 + timedelta(days=i), Decimal(0)),
        )
        for i in range(30)
    ]


def _event(
    *,
    event_day: date = DAY0,
    block_number: int = 1,
    log_index: int | None = 1,
    order_hash: str = "0xorder",
    gross_shares: Decimal = Decimal("0.01"),
    gross_collateral: Decimal = Decimal("0.0001"),
    fee_usdc: Decimal | None = Decimal(0),
    role: str = chain.AGGRESSOR_PARENT,
) -> chain.ChainFill:
    start = _day_start(event_day)
    return chain.ChainFill(
        market_day=event_day,
        chain_id=CHAIN_ID,
        exchange_contract=EXCHANGE,
        abi_version=ABI,
        transaction_hash=f"0x{block_number:064x}",
        log_index=log_index,
        block_number=block_number,
        timestamp_s=start + 301,
        market_start_s=start,
        market_id=f"condition-{start}",
        token_asset_id=f"up-{start}",
        order_hash=order_hash,
        maker="0xmaker",
        taker=EXCHANGE if role == chain.AGGRESSOR_PARENT else "0xtaker",
        role=role,
        gross_shares=gross_shares,
        gross_collateral=gross_collateral,
        fee_usdc=fee_usdc,
    )


def test_exact_event_identity_deduplicates_overlap_but_parent_order_keeps_all_partials():
    first = _event(block_number=7, log_index=7, order_hash="0xparent")
    second = _event(block_number=8, log_index=9, order_hash="0xparent")
    events = [first, first, second]

    result = chain.evaluate_capacity(events, _manifests(events))

    assert result.unique_events == 2
    assert result.exact_duplicates == 1
    assert result.aggressor_events == 2
    assert result.distinct_parent_orders == 1
    assert result.inflated_weighted_volume == Decimal("0.0004554")
    assert result.verdict == chain.REJECTED_CAPACITY_UPPER_BOUND


def test_aggressor_aggregate_is_counted_once_and_maker_children_are_not_extra_parents():
    parent = _event(gross_shares=Decimal("1"), gross_collateral=Decimal("0.5"))
    maker1 = _event(block_number=2, log_index=2, order_hash="0xmaker1", role=chain.MAKER_CHILD)
    maker2 = _event(block_number=3, log_index=3, order_hash="0xmaker2", role=chain.MAKER_CHILD)
    events = [parent, maker1, maker2]

    result = chain.evaluate_capacity(events, _manifests(events))

    assert result.aggressor_events == 1
    assert result.maker_child_events == 2
    assert result.distinct_parent_orders == 1
    assert result.inflation_mode == chain.PARENT_FRAGMENT_UPPER_BOUND


def test_fee_paying_parent_retains_hidden_zero_fee_fragment_capacity():
    low = _event(
        block_number=2,
        log_index=1,
        order_hash="0xparent",
        gross_shares=Decimal("0.01"),
        gross_collateral=Decimal("0.0001"),
        fee_usdc=Decimal("0.000001"),
    )
    high = _event(
        block_number=3,
        log_index=2,
        order_hash="0xparent",
        gross_shares=Decimal("0.01"),
        gross_collateral=Decimal("0.0099"),
        fee_usdc=Decimal(0),
    )
    events = [low, high]

    result = chain.evaluate_capacity(events, _manifests(events))

    assert result.distinct_parent_orders == 1
    assert result.fee_paying_parent_orders == 1
    assert result.inflated_weighted_volume > 0
    assert result.inflated_weighted_volume == Decimal("0.0115000")


def test_operator_supplied_positive_fee_is_never_subtracted_from_the_capacity_upper_bound():
    charged = _event(
        block_number=2,
        log_index=1,
        order_hash="0xparent",
        gross_shares=Decimal("0.01"),
        gross_collateral=Decimal("0.005"),
        fee_usdc=Decimal("0.0002"),
    )
    free = _event(
        block_number=3,
        log_index=2,
        order_hash="0xparent",
        gross_shares=Decimal("0.01"),
        gross_collateral=Decimal("0.005"),
        fee_usdc=Decimal(0),
    )
    events = [charged, free]

    result = chain.evaluate_capacity(events, _manifests(events))

    actual_free_weighted_volume = Decimal("0.01") * Decimal("0.5") * Decimal("0.5") * chain.CATEGORY_WEIGHT
    assert result.inflated_weighted_volume >= actual_free_weighted_volume


def test_formal_curve_bound_rounds_up_instead_of_false_rejecting_an_exact_threshold():
    event = _event(
        gross_shares=Decimal("1660303.614870"),
        gross_collateral=Decimal("1659433.593750"),
    )
    manifest = replace(_manifests([event])[0], fragment_increment=Decimal("0.000001"))

    bound, reasons = chain._parent_bound(event.parent_key, [event], manifest)

    assert reasons == []
    assert bound is not None
    assert bound.weighted_volume_bound >= Decimal("2000")


def test_parent_and_day_aggregation_cannot_round_an_exact_threshold_down():
    multipliers = (46_891, 518_722, 284_017)
    events = [
        _event(
            block_number=index + 10,
            log_index=index,
            order_hash=f"0xparent-{index}",
            gross_shares=Decimal("1.954149") * multiplier,
            gross_collateral=Decimal("1.953125") * multiplier,
        )
        for index, multiplier in enumerate(multipliers)
    ]
    manifests = _manifests(events)
    manifests[0] = replace(manifests[0], fragment_increment=Decimal("0.000001"))

    result = chain.evaluate_capacity(events, manifests)

    assert result.inflated_weighted_volume >= Decimal("2000")
    assert result.verdict == chain.INCOMPLETE_NEEDS_AUTHENTICATED_CREDIT


def test_one_order_hash_with_conflicting_makers_fails_closed_instead_of_splitting_the_bound():
    first = _event(block_number=2, log_index=1, order_hash="0xparent")
    second = replace(_event(block_number=3, log_index=2, order_hash="0xparent"), maker="0xother")
    events = [first, second]

    result = chain.evaluate_capacity(events, _manifests(events))

    assert result.verdict == chain.INCOMPLETE_CHAIN_MANIFEST
    assert any("conflicting market/token/ABI identities" in reason for reason in result.incomplete_reasons)


def test_parent_hash_maker_conflict_in_maker_child_or_across_days_fails_closed():
    parent = _event(block_number=2, log_index=1, order_hash="0xparent")
    child = replace(
        _event(block_number=3, log_index=2, order_hash="0xparent", role=chain.MAKER_CHILD),
        maker="0xother",
    )
    same_day = chain.evaluate_capacity([parent, child], _manifests([parent, child]))
    assert same_day.verdict == chain.INCOMPLETE_CHAIN_MANIFEST
    assert any("conflicting maker identities" in reason for reason in same_day.incomplete_reasons)

    next_day = replace(
        _event(
            event_day=DAY0 + timedelta(days=1),
            block_number=4,
            log_index=3,
            order_hash="0xparent",
        ),
        maker="0xother",
    )
    across_days = chain.evaluate_capacity([parent, next_day], _manifests([parent, next_day]))
    assert across_days.verdict == chain.INCOMPLETE_CHAIN_MANIFEST
    assert any("across days" in reason for reason in across_days.incomplete_reasons)


def test_missing_identity_unknown_role_or_conflicting_duplicate_blocks_rejection():
    missing_key = _event(log_index=None)
    unknown_role = _event(block_number=2, log_index=2, role="unknown")
    original = _event(block_number=3, log_index=3)
    conflicting = replace(original, gross_shares=Decimal("0.02"))
    events = [missing_key, unknown_role, original, conflicting]

    result = chain.evaluate_capacity(events, _manifests(events))

    assert result.verdict == chain.INCOMPLETE_CHAIN_MANIFEST
    assert any("event key" in reason for reason in result.incomplete_reasons)
    assert any("exchange role" in reason for reason in result.incomplete_reasons)
    assert any("conflicting payload" in reason for reason in result.incomplete_reasons)


def test_fee_units_and_impossible_fee_fail_closed():
    event = _event(
        gross_shares=Decimal("0.01"),
        gross_collateral=Decimal("0.005"),
        fee_usdc=Decimal("0.01"),
    )
    manifests = _manifests([event])

    manifests[0] = replace(manifests[0], fee_semantics_proven=False)
    unknown_units = chain.evaluate_capacity([replace(event, fee_usdc=Decimal(0))], manifests)
    assert any("fee semantics" in reason for reason in unknown_units.incomplete_reasons)


def test_nonretained_parent_bounds_still_keep_exact_aggregate_counts():
    events = [_event(block_number=2, log_index=1, order_hash="a"),
              _event(block_number=3, log_index=2, order_hash="b", fee_usdc=Decimal("0.000001"))]
    manifest = _manifests(events)[0]

    audit = chain.audit_day_events(events, manifest, retain_parent_bounds=False)

    assert audit.parent_bounds == ()
    assert audit.distinct_parent_orders == 2
    assert audit.fee_paying_parent_orders == 1
    assert audit.inflated_weighted_volume > 0


def test_formal_gate_requires_30_complete_days_source_integrity_and_amount_parity():
    short = chain.evaluate_capacity([], _manifests()[:-1])
    assert short.verdict == chain.INCOMPLETE_CHAIN_MANIFEST
    assert any("30 contiguous" in reason for reason in short.incomplete_reasons)

    manifests = _manifests()
    manifests[10] = _manifest(DAY0 + timedelta(days=10), parity_delta=Decimal("0.01"))
    mismatch = chain.evaluate_capacity([], manifests)
    assert any("aggregate parity" in reason for reason in mismatch.incomplete_reasons)

    manifests = _manifests()
    manifests[10] = replace(
        manifests[10],
        raw_columns=frozenset(set(chain.REQUIRED_RAW_COLUMNS) - {"transaction_hash"}),
    )
    incomplete = chain.evaluate_capacity([], manifests)
    assert any("required columns" in reason for reason in incomplete.incomplete_reasons)


def test_complete_upper_bound_can_only_reject_or_request_authenticated_credit():
    event = _event()
    manifests = _manifests([event])

    rejected = chain.evaluate_capacity([event], manifests)
    assert rejected.verdict == chain.REJECTED_CAPACITY_UPPER_BOUND
    assert rejected.inflated_weighted_volume < rejected.bronze_weighted_volume

    not_promoted = chain.evaluate_capacity(
        [event], manifests, bronze_weighted_volume=Decimal("0.0001")
    )
    assert not_promoted.verdict == chain.INCOMPLETE_NEEDS_AUTHENTICATED_CREDIT
    assert not_promoted.inflated_weighted_volume >= not_promoted.bronze_weighted_volume
    assert "PASS" not in not_promoted.verdict
