"""Fail-closed chain upper bound for Family 31 dust-fill capacity.

The public trade API can aggregate independently rounded match fragments.  A
fee-paying parent row may therefore hide fee-free fragments, while chain feeds
can expose one aggressor aggregate plus several maker children.  Classifying
rows by their aggregate fee or counting every event as another opportunity can
both be wrong.

This module instead groups only explicitly identified aggressor events by
parent order and bounds hidden fee-free weighted volume with concavity of
``p * (1-p)``.  The operator-supplied fee is never used to shrink that bound,
because the event alone does not prove a fixed fee curve. Complete evidence below Bronze can reject capacity. A bound
at or above Bronze remains incomplete until account reward credit and genuine
third-party executability are authenticated.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from fractions import Fraction
from typing import Final

from reward_dust import BRONZE_WEIGHTED_VOLUME, CATEGORY_WEIGHT, FEE_RATE


REQUIRED_DAYS: Final = 30
MARKETS_PER_DAY: Final = 288
TOKENS_PER_MARKET: Final = 2
MARKET_SECONDS: Final = 300
CAPTURE_GRACE_SECONDS: Final = 3_600

PARITY_METRIC: Final = "aggressor_parent_token_amount_by_market_day"
AGGRESSOR_PARENT: Final = "aggressor_parent"
MAKER_CHILD: Final = "maker_child"
PARENT_FRAGMENT_UPPER_BOUND: Final = "parent_nominal_concavity_upper_bound"

REJECTED_CAPACITY_UPPER_BOUND: Final = "REJECTED_CAPACITY_UPPER_BOUND"
INCOMPLETE_CHAIN_MANIFEST: Final = "INCOMPLETE_CHAIN_MANIFEST"
INCOMPLETE_NEEDS_AUTHENTICATED_CREDIT: Final = "INCOMPLETE_NEEDS_AUTHENTICATED_CREDIT"

REQUIRED_RAW_COLUMNS: Final[frozenset[str]] = frozenset(
    {
        "timestamp",
        "datetime",
        "block_number",
        "transaction_hash",
        "log_index",
        "contract",
        "order_hash",
        "maker",
        "taker",
        "side",
        "token_id",
        "maker_amount_filled",
        "taker_amount_filled",
        "fee",
        "builder",
        "metadata",
    }
)

_SHA256 = re.compile(r"[0-9a-f]{64}")
_ROLES = frozenset({AGGRESSOR_PARENT, MAKER_CHILD})


def _upper_decimal(value: Fraction) -> Decimal:
    """Convert a non-negative exact rational without ever rounding it downward."""
    if value < 0:
        raise ValueError("upper-bound conversion requires a non-negative value")
    with localcontext() as context:
        context.prec = max(
            80,
            len(str(abs(value.numerator))) + len(str(value.denominator)) + 8,
        )
        context.rounding = ROUND_CEILING
        return Decimal(value.numerator) / Decimal(value.denominator)


@dataclass(frozen=True)
class DayManifest:
    """Completeness and unit proof for one UTC BTC5m market day."""

    market_day: date
    capture_start_s: int
    capture_end_s: int
    source_hashes: tuple[str, ...]
    source_complete: bool
    truncated: bool
    finalized_ranges_complete: bool
    reorg_conflicts: int
    identity_semantics_proven: bool
    chain_id: int
    allowed_exchange_contracts: frozenset[str]
    supported_abi_versions: frozenset[str]
    amount_semantics_proven: bool
    fee_semantics_proven: bool
    fragment_increment: Decimal
    fragment_precision_proven: bool
    zero_fee_threshold_usdc: Decimal
    zero_fee_threshold_proven: bool
    raw_columns: frozenset[str]
    raw_rows: int
    selected_rows: int
    market_starts: tuple[int, ...]
    token_ids: tuple[str, ...]
    parity_checked: bool
    parity_metric: str
    parity_api_token_amount: Decimal
    parity_chain_token_amount: Decimal
    parity_abs_tolerance: Decimal


@dataclass(frozen=True)
class ChainFill:
    """One raw chain event normalised with proven human-unit semantics."""

    market_day: date
    chain_id: int
    exchange_contract: str
    abi_version: str
    transaction_hash: str
    log_index: int | None
    block_number: int | None
    timestamp_s: int
    market_start_s: int
    market_id: str
    token_asset_id: str
    order_hash: str
    maker: str
    taker: str
    role: str
    gross_shares: Decimal
    gross_collateral: Decimal
    fee_usdc: Decimal | None

    @property
    def event_key(self) -> tuple[int, str, str, int] | None:
        if (
            not self.transaction_hash
            or self.block_number is None
            or self.block_number < 0
            or self.log_index is None
            or self.log_index < 0
        ):
            return None
        return (
            self.chain_id,
            self.exchange_contract.lower(),
            self.transaction_hash.lower(),
            self.log_index,
        )

    @property
    def parent_key(self) -> tuple[int, str, str] | None:
        if not self.order_hash:
            return None
        return (
            self.chain_id,
            self.exchange_contract.lower(),
            self.order_hash.lower(),
        )


@dataclass(frozen=True)
class ParentBound:
    parent_key: tuple[int, str, str]
    market_day: date
    events: int
    shares: Decimal
    collateral: Decimal
    fee_usdc: Decimal
    average_price: Decimal
    fragment_bound: Decimal
    curve_bound: Decimal
    weighted_volume_bound: Decimal


@dataclass(frozen=True)
class ChainCapacityResult:
    verdict: str
    evidence_class: str
    inflation_mode: str
    complete_days: int
    complete_markets: int
    unique_events: int
    exact_duplicates: int
    aggressor_events: int
    maker_child_events: int
    distinct_parent_orders: int
    fee_paying_parent_orders: int
    inflated_weighted_volume: Decimal
    bronze_weighted_volume: Decimal
    parent_bounds: tuple[ParentBound, ...]
    incomplete_reasons: tuple[str, ...]


@dataclass(frozen=True)
class DayCapacityAudit:
    market_day: date
    unique_events: int
    exact_duplicates: int
    aggressor_events: int
    maker_child_events: int
    distinct_parent_orders: int
    fee_paying_parent_orders: int
    parent_shares: Decimal
    inflated_weighted_volume: Decimal
    parent_bounds: tuple[ParentBound, ...]
    parent_makers: tuple[tuple[tuple[int, str, str], str], ...]
    incomplete_reasons: tuple[str, ...]


def _utc_start(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())


def _manifest_reasons(manifests: list[DayManifest]) -> list[str]:
    reasons: list[str] = []
    by_day = {manifest.market_day: manifest for manifest in manifests}
    if len(manifests) != REQUIRED_DAYS or len(by_day) != REQUIRED_DAYS:
        reasons.append("requires exactly 30 contiguous unique UTC market days")
    ordered_days = sorted(by_day)
    if ordered_days:
        expected_days = [ordered_days[0] + timedelta(days=i) for i in range(len(ordered_days))]
        if ordered_days != expected_days:
            reasons.append("30 contiguous UTC market days are not present")

    for manifest in sorted(manifests, key=lambda item: item.market_day):
        prefix = manifest.market_day.isoformat()
        start = _utc_start(manifest.market_day)
        expected_starts = tuple(start + MARKET_SECONDS * i for i in range(MARKETS_PER_DAY))
        if manifest.market_starts != expected_starts:
            reasons.append(f"{prefix}: expected exactly 288 aligned BTC5m markets")
        expected_tokens = MARKETS_PER_DAY * TOKENS_PER_MARKET
        if len(manifest.token_ids) != expected_tokens or len(set(manifest.token_ids)) != expected_tokens:
            reasons.append(f"{prefix}: expected 576 unique target token ids")
        if manifest.capture_start_s > start or manifest.capture_end_s < start + 86_400 + CAPTURE_GRACE_SECONDS:
            reasons.append(f"{prefix}: capture misses the market day plus one-hour grace")
        if not manifest.source_hashes or any(not _SHA256.fullmatch(value) for value in manifest.source_hashes):
            reasons.append(f"{prefix}: source SHA-256 manifest is missing or invalid")
        if not manifest.source_complete or manifest.truncated:
            reasons.append(f"{prefix}: raw source is incomplete or truncated")
        if not manifest.finalized_ranges_complete or manifest.reorg_conflicts:
            reasons.append(f"{prefix}: finalized block coverage is incomplete or reorg-conflicted")
        if not manifest.identity_semantics_proven:
            reasons.append(f"{prefix}: source event identity semantics are not proven")
        if manifest.chain_id <= 0:
            reasons.append(f"{prefix}: chain_id is invalid")
        if not manifest.allowed_exchange_contracts:
            reasons.append(f"{prefix}: explicit exchange-contract allowlist is missing")
        if not manifest.supported_abi_versions:
            reasons.append(f"{prefix}: supported ABI/version set is missing")
        if not manifest.amount_semantics_proven:
            reasons.append(f"{prefix}: amount semantics are not proven")
        if not manifest.fee_semantics_proven:
            reasons.append(f"{prefix}: fee semantics are not proven")
        if not manifest.fragment_precision_proven or manifest.fragment_increment <= 0:
            reasons.append(f"{prefix}: fragment precision is not proven")
        if not manifest.zero_fee_threshold_proven or manifest.zero_fee_threshold_usdc <= 0:
            reasons.append(f"{prefix}: zero-fee threshold is not proven")
        missing = REQUIRED_RAW_COLUMNS - manifest.raw_columns
        if missing:
            reasons.append(f"{prefix}: required columns missing: {','.join(sorted(missing))}")
        if manifest.raw_rows < 0 or not 0 <= manifest.selected_rows <= manifest.raw_rows:
            reasons.append(f"{prefix}: raw/selected row counts are invalid")
        if not manifest.parity_checked:
            reasons.append(f"{prefix}: Data API aggregate parity was not checked")
        if manifest.parity_metric != PARITY_METRIC:
            reasons.append(f"{prefix}: unsupported Data API aggregate parity metric")
        if (
            manifest.parity_api_token_amount < 0
            or manifest.parity_chain_token_amount < 0
            or manifest.parity_abs_tolerance < 0
            or abs(manifest.parity_api_token_amount - manifest.parity_chain_token_amount)
            > manifest.parity_abs_tolerance
        ):
            reasons.append(f"{prefix}: Data API aggregate parity failed")
    return reasons


def _event_reasons(
    event: ChainFill,
    manifest: DayManifest | None,
    *,
    market_starts: frozenset[int] | None = None,
    token_ids: frozenset[str] | None = None,
) -> list[str]:
    prefix = f"{event.market_day.isoformat()}:{event.transaction_hash or '<missing>'}"
    reasons: list[str] = []
    if event.event_key is None:
        reasons.append(f"{prefix}: immutable chain event key is missing")
    if event.block_number is None or event.block_number < 0:
        reasons.append(f"{prefix}: finalized block identity is missing")
    if event.parent_key is None:
        reasons.append(f"{prefix}: parent order_hash is missing")
    if not event.maker or not event.taker:
        reasons.append(f"{prefix}: maker/taker identity is missing")
    if event.role not in _ROLES:
        reasons.append(f"{prefix}: exchange role is unknown")
    if not event.market_id or not event.token_asset_id:
        reasons.append(f"{prefix}: market or token identity is missing")
    if event.gross_shares <= 0 or event.gross_collateral <= 0:
        reasons.append(f"{prefix}: gross amounts must be positive")
    if event.fee_usdc is None:
        reasons.append(f"{prefix}: normalized fee_usdc is missing")
    elif event.fee_usdc < 0:
        reasons.append(f"{prefix}: normalized fee_usdc is negative")
    if manifest is None:
        reasons.append(f"{prefix}: no market-day manifest")
        return reasons
    contracts = {value.lower() for value in manifest.allowed_exchange_contracts}
    if event.chain_id != manifest.chain_id:
        reasons.append(f"{prefix}: chain_id conflicts with manifest")
    if event.exchange_contract.lower() not in contracts:
        reasons.append(f"{prefix}: exchange contract is not allowlisted")
    if event.abi_version not in manifest.supported_abi_versions:
        reasons.append(f"{prefix}: ABI/version is unsupported")
    if event.role == AGGRESSOR_PARENT and event.taker.lower() != event.exchange_contract.lower():
        reasons.append(f"{prefix}: aggressor role is not proven by the exchange address")
    market_starts = market_starts or frozenset(manifest.market_starts)
    token_ids = token_ids or frozenset(manifest.token_ids)
    if event.market_start_s not in market_starts:
        reasons.append(f"{prefix}: market start is outside the complete manifest")
    if event.token_asset_id not in token_ids:
        reasons.append(f"{prefix}: token is outside the complete manifest")
    if not manifest.capture_start_s <= event.timestamp_s < manifest.capture_end_s:
        reasons.append(f"{prefix}: timestamp is outside the proven capture interval")
    if manifest.fragment_increment > 0:
        units = event.gross_shares / manifest.fragment_increment
        if units != units.to_integral_value():
            reasons.append(f"{prefix}: shares violate the proven fragment precision")
    return reasons


def _parent_bound(
    parent_key: tuple[int, str, str],
    events: list[ChainFill],
    manifest: DayManifest,
) -> tuple[ParentBound | None, list[str]]:
    reasons: list[str] = []
    first = events[0]
    if any(
        (event.market_day, event.market_id, event.token_asset_id, event.abi_version, event.maker)
        != (first.market_day, first.market_id, first.token_asset_id, first.abi_version, first.maker)
        for event in events[1:]
    ):
        reasons.append(f"{parent_key[2]}: one parent spans conflicting market/token/ABI identities")
        return None, reasons

    exact_shares = sum((Fraction(event.gross_shares) for event in events), Fraction(0))
    exact_collateral = sum((Fraction(event.gross_collateral) for event in events), Fraction(0))
    exact_fee = sum((Fraction(event.fee_usdc or Decimal(0)) for event in events), Fraction(0))
    if exact_shares <= 0 or exact_collateral <= 0:
        reasons.append(f"{parent_key[2]}: parent gross amounts are nonpositive")
        return None, reasons
    if exact_collateral >= exact_shares:
        reasons.append(f"{parent_key[2]}: parent average price is outside (0,1)")
        return None, reasons
    shares = _upper_decimal(exact_shares)
    collateral = _upper_decimal(exact_collateral)
    fee = _upper_decimal(exact_fee)
    price = collateral / shares

    fragment_count = int(
        (shares / manifest.fragment_increment).to_integral_value(rounding=ROUND_FLOOR)
    )
    fragment_bound = (
        Decimal(fragment_count)
        * manifest.zero_fee_threshold_usdc
        * CATEGORY_WEIGHT
        / FEE_RATE
    )
    exact_curve = (
        Fraction(CATEGORY_WEIGHT)
        * exact_collateral
        * (exact_shares - exact_collateral)
        / exact_shares
    )
    # The formal value must never round below the exact rational upper bound.  In particular,
    # computing a rounded average price first can turn an exact threshold value into 1999.99…
    # and cause a false capacity rejection.  Decimal ROUND_CEILING preserves the upper-bound
    # direction while retaining enough significant digits for reporting and threshold checks.
    nominal_curve = _upper_decimal(exact_curve)
    # V2's operator supplies the fee and the contract only enforces an upper bound.  The event
    # therefore does not prove that a positive fee consumed ``fee / 7%`` of this curve.  Ignoring
    # charged fees is looser but is the only safe upper bound on hidden zero-fee capacity.
    curve_bound = nominal_curve
    # The fragment ceiling is only conditional on an independently proven operator fee policy.
    # Until that proof exists, a zero event fee could be operator-supplied at any fragment size,
    # so the full nominal concavity ceiling is the formal upper bound.
    bound = curve_bound
    return (
        ParentBound(
            parent_key=parent_key,
            market_day=first.market_day,
            events=len(events),
            shares=shares,
            collateral=collateral,
            fee_usdc=fee,
            average_price=price,
            fragment_bound=fragment_bound,
            curve_bound=curve_bound,
            weighted_volume_bound=bound,
        ),
        reasons,
    )


def audit_day_events(
    events: list[ChainFill],
    manifest: DayManifest,
    *,
    retain_parent_bounds: bool = True,
) -> DayCapacityAudit:
    """Deduplicate and bound one market day so large scans stay memory-bounded."""
    reasons: list[str] = []
    market_starts = frozenset(manifest.market_starts)
    token_ids = frozenset(manifest.token_ids)
    unique: dict[tuple[int, str, str, int], ChainFill] = {}
    exact_duplicates = 0
    for event in events:
        reasons.extend(
            _event_reasons(
                event,
                manifest,
                market_starts=market_starts,
                token_ids=token_ids,
            )
        )
        key = event.event_key
        if key is None:
            continue
        previous = unique.get(key)
        if previous is None:
            unique[key] = event
        elif previous == event:
            exact_duplicates += 1
        else:
            reasons.append(
                f"{event.market_day.isoformat()}:{event.transaction_hash}:{event.log_index}: "
                "conflicting payload for one chain event key"
            )

    parents: dict[tuple[int, str, str], list[ChainFill]] = defaultdict(list)
    parent_makers: dict[tuple[int, str, str], str] = {}
    maker_children = 0
    exact_parent_shares = Fraction(0)
    for event in unique.values():
        if event.parent_key is not None:
            maker = event.maker.lower()
            previous_maker = parent_makers.setdefault(event.parent_key, maker)
            if previous_maker != maker:
                reasons.append(
                    f"{event.parent_key[2]}: one parent hash has conflicting maker identities"
                )
        if event.role == AGGRESSOR_PARENT and event.parent_key is not None:
            parents[event.parent_key].append(event)
            exact_parent_shares += Fraction(event.gross_shares)
        elif event.role == MAKER_CHILD:
            maker_children += 1

    if len(unique) != manifest.selected_rows:
        reasons.append(
            f"{manifest.market_day.isoformat()}: selected chain row count does not match manifest"
        )
    parent_shares = _upper_decimal(exact_parent_shares)
    if abs(exact_parent_shares - Fraction(manifest.parity_chain_token_amount)) > Fraction(
        manifest.parity_abs_tolerance
    ):
        reasons.append(
            f"{manifest.market_day.isoformat()}: extracted parent amount does not match chain parity manifest"
        )

    parent_bounds: list[ParentBound] = []
    distinct_parent_orders = 0
    fee_paying_parent_orders = 0
    exact_inflated_weighted_volume = Fraction(0)
    for parent_key, parent_events in parents.items():
        bound, parent_reasons = _parent_bound(parent_key, parent_events, manifest)
        reasons.extend(parent_reasons)
        if bound is not None:
            distinct_parent_orders += 1
            fee_paying_parent_orders += int(bound.fee_usdc > 0)
            exact_inflated_weighted_volume += Fraction(bound.weighted_volume_bound)
            if retain_parent_bounds:
                parent_bounds.append(bound)

    return DayCapacityAudit(
        market_day=manifest.market_day,
        unique_events=len(unique),
        exact_duplicates=exact_duplicates,
        aggressor_events=sum(len(group) for group in parents.values()),
        maker_child_events=maker_children,
        distinct_parent_orders=distinct_parent_orders,
        fee_paying_parent_orders=fee_paying_parent_orders,
        parent_shares=parent_shares,
        inflated_weighted_volume=_upper_decimal(exact_inflated_weighted_volume),
        parent_bounds=(
            tuple(sorted(parent_bounds, key=lambda item: item.parent_key))
            if retain_parent_bounds
            else ()
        ),
        parent_makers=tuple(sorted(parent_makers.items())),
        incomplete_reasons=tuple(dict.fromkeys(reasons)),
    )


def combine_day_audits(
    audits: list[DayCapacityAudit],
    manifests: list[DayManifest],
    *,
    bronze_weighted_volume: Decimal = BRONZE_WEIGHTED_VOLUME,
) -> ChainCapacityResult:
    """Combine 30 bounded daily audits; this function has no pass verdict."""
    if bronze_weighted_volume <= 0:
        raise ValueError("bronze_weighted_volume must be positive")
    reasons = _manifest_reasons(manifests)
    by_day = {audit.market_day: audit for audit in audits}
    manifest_days = {manifest.market_day for manifest in manifests}
    if len(audits) != len(by_day) or set(by_day) != manifest_days:
        reasons.append("daily audits do not map one-to-one to the manifest days")
    for audit in audits:
        reasons.extend(audit.incomplete_reasons)
    global_parent_makers: dict[tuple[int, str, str], str] = {}
    for audit in audits:
        for parent_key, maker in audit.parent_makers:
            previous_maker = global_parent_makers.setdefault(parent_key, maker)
            if previous_maker != maker:
                reasons.append(
                    f"{parent_key[2]}: one parent hash has conflicting maker identities across days"
                )
    inflated_wv = _upper_decimal(
        sum((Fraction(audit.inflated_weighted_volume) for audit in audits), Fraction(0))
    )
    deduplicated_reasons = tuple(dict.fromkeys(reasons))
    if deduplicated_reasons:
        verdict = INCOMPLETE_CHAIN_MANIFEST
    elif inflated_wv < bronze_weighted_volume:
        verdict = REJECTED_CAPACITY_UPPER_BOUND
    else:
        verdict = INCOMPLETE_NEEDS_AUTHENTICATED_CREDIT

    return ChainCapacityResult(
        verdict=verdict,
        evidence_class="parent_order_chain_upper_bound",
        inflation_mode=PARENT_FRAGMENT_UPPER_BOUND,
        complete_days=len(manifest_days),
        complete_markets=sum(len(manifest.market_starts) for manifest in manifests),
        unique_events=sum(audit.unique_events for audit in audits),
        exact_duplicates=sum(audit.exact_duplicates for audit in audits),
        aggressor_events=sum(audit.aggressor_events for audit in audits),
        maker_child_events=sum(audit.maker_child_events for audit in audits),
        distinct_parent_orders=sum(audit.distinct_parent_orders for audit in audits),
        fee_paying_parent_orders=sum(audit.fee_paying_parent_orders for audit in audits),
        inflated_weighted_volume=inflated_wv,
        bronze_weighted_volume=bronze_weighted_volume,
        parent_bounds=tuple(
            bound
            for audit in sorted(audits, key=lambda item: item.market_day)
            for bound in audit.parent_bounds
        ),
        incomplete_reasons=deduplicated_reasons,
    )


def evaluate_capacity(
    events: list[ChainFill],
    manifests: list[DayManifest],
    *,
    bronze_weighted_volume: Decimal = BRONZE_WEIGHTED_VOLUME,
) -> ChainCapacityResult:
    """Convenience evaluator for small in-memory fixtures and filtered datasets."""
    manifests_by_day = {manifest.market_day: manifest for manifest in manifests}
    events_by_day: dict[date, list[ChainFill]] = defaultdict(list)
    outside: list[str] = []
    for event in events:
        if event.market_day not in manifests_by_day:
            outside.append(f"{event.market_day.isoformat()}: event has no manifest day")
        else:
            events_by_day[event.market_day].append(event)
    audits = [
        audit_day_events(events_by_day.get(day, []), manifest)
        for day, manifest in sorted(manifests_by_day.items())
    ]
    result = combine_day_audits(
        audits,
        manifests,
        bronze_weighted_volume=bronze_weighted_volume,
    )
    if not outside:
        return result
    reasons = tuple(dict.fromkeys((*result.incomplete_reasons, *outside)))
    return ChainCapacityResult(
        **{
            **result.__dict__,
            "verdict": INCOMPLETE_CHAIN_MANIFEST,
            "incomplete_reasons": reasons,
        }
    )
