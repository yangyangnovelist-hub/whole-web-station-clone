"""Audit Polymarket reward subsidies as a standalone BTC 5m mechanism."""

from __future__ import annotations

import argparse
import json
import math
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

CRYPTO_CATEGORY_WEIGHT = 2.3
TAKER_FEE_RATE = 0.07
FEE_QUANTUM = Decimal("0.00001")
SIZE_INCREMENT = 0.01
MARKET_SECONDS = 300
GAMMA_MARKET_URL = "https://gamma-api.polymarket.com/markets?slug={slug}"
REWARDS_URL = "https://clob.polymarket.com/rewards/markets/{condition_id}"


@dataclass(frozen=True)
class Tier:
    name: str
    threshold_wv: float
    rebate_fraction: float
    bonus: float


@dataclass(frozen=True)
class TierEconomics:
    name: str
    threshold_wv: float
    rebate_fraction: float
    bonus: float
    gross_fee_floor: float
    cumulative_bonus: float
    subsidy_net: float
    residual_fee_per_wv: float


@dataclass(frozen=True)
class RoundingFloor:
    fee_per_wv: float
    price: float
    fill_shares: float
    raw_fee: float
    rounded_fee: float


@dataclass(frozen=True)
class RewardCheck:
    start_s: int
    condition_id: str
    slug: str
    min_order_size: float
    min_tick_size: float
    native_active_config_count: int
    sponsored_active_config_count: int


TIERS = (
    Tier("Bronze", 2_000.0, 0.03, 10.0),
    Tier("Silver", 20_000.0, 0.08, 50.0),
    Tier("Gold", 200_000.0, 0.18, 250.0),
    Tier("Platinum", 1_000_000.0, 0.32, 1_500.0),
    Tier("Diamond", 4_000_000.0, 0.44, 7_500.0),
    Tier("Obsidian", 10_000_000.0, 0.50, 25_000.0),
)


def _validate_trade(shares: float, price: float) -> None:
    if shares <= 0:
        raise ValueError("shares must be positive")
    if not 0 < price < 1:
        raise ValueError("price must be strictly between zero and one")


def weighted_volume(shares: float, price: float) -> float:
    """Official crypto weighted-volume expression for one taker trade."""
    _validate_trade(shares, price)
    return shares * price * (1.0 - price) * CRYPTO_CATEGORY_WEIGHT


def taker_fee(shares: float, price: float) -> float:
    """Official crypto taker fee before five-decimal venue rounding."""
    _validate_trade(shares, price)
    return shares * TAKER_FEE_RATE * price * (1.0 - price)


def rounded_fee_floor(
    price_tick: float,
    size_increment: float,
    min_order_size: float,
) -> RoundingFloor:
    """Find the most subsidy-favourable fee/wV ratio allowed by market precision.

    A minimum order may execute in smaller partial fills.  To avoid overstating
    fees, the search permits every two-decimal partial-fill size from one size
    increment through the market's minimum order size and rounds each fill
    independently. Official documentation says any fee smaller than one
    0.00001 fee quantum becomes zero, so venue precision is modelled as
    truncation rather than conventional half-up rounding.
    """
    tick = Decimal(str(price_tick))
    increment = Decimal(str(size_increment))
    order_minimum = Decimal(str(min_order_size))
    if tick <= 0 or tick >= 1 or increment <= 0 or order_minimum < increment:
        raise ValueError("invalid tick, size increment, or minimum order size")
    price_steps_decimal = Decimal(1) / tick
    if price_steps_decimal != price_steps_decimal.to_integral_value():
        raise ValueError("price tick must divide one exactly")
    price_steps = int(price_steps_decimal)
    fill_steps = int((order_minimum / increment).to_integral_value(rounding=ROUND_CEILING))

    best: tuple[Decimal, Decimal, Decimal, Decimal, Decimal] | None = None
    rate = Decimal(str(TAKER_FEE_RATE))
    weight = Decimal(str(CRYPTO_CATEGORY_WEIGHT))
    for price_index in range(1, price_steps):
        price = tick * price_index
        for size_index in range(1, fill_steps + 1):
            shares = increment * size_index
            raw_fee = shares * rate * price * (Decimal(1) - price)
            rounded_fee = raw_fee.quantize(FEE_QUANTUM, rounding=ROUND_DOWN)
            weighted = shares * price * (Decimal(1) - price) * weight
            ratio = rounded_fee / weighted
            row = (ratio, price, shares, raw_fee, rounded_fee)
            if best is None or row < best:
                best = row
    if best is None:
        raise ValueError("no executable price/size grid")
    ratio, price, shares, raw_fee, rounded_fee = best
    return RoundingFloor(
        fee_per_wv=float(ratio),
        price=float(price),
        fill_shares=float(shares),
        raw_fee=float(raw_fee),
        rounded_fee=float(rounded_fee),
    )


def tier_economics(
    fee_per_wv: float = TAKER_FEE_RATE / CRYPTO_CATEGORY_WEIGHT,
) -> list[TierEconomics]:
    """Apply each unlocked rebate only to the next qualification tranche."""
    if not math.isfinite(fee_per_wv) or fee_per_wv < 0:
        raise ValueError("fee_per_wv must be finite and nonnegative")
    rows: list[TierEconomics] = []
    cumulative_bonus = 0.0
    qualification_fees = 0.0
    previous_threshold = 0.0
    previous_rebate = 0.0
    for tier in TIERS:
        tranche = tier.threshold_wv - previous_threshold
        qualification_fees += tranche * fee_per_wv * (1.0 - previous_rebate)
        cumulative_bonus += tier.bonus
        rows.append(
            TierEconomics(
                name=tier.name,
                threshold_wv=tier.threshold_wv,
                rebate_fraction=tier.rebate_fraction,
                bonus=tier.bonus,
                gross_fee_floor=qualification_fees,
                cumulative_bonus=cumulative_bonus,
                subsidy_net=cumulative_bonus - qualification_fees,
                residual_fee_per_wv=(1.0 - tier.rebate_fraction) * fee_per_wv,
            )
        )
        previous_threshold = tier.threshold_wv
        previous_rebate = tier.rebate_fraction
    return rows


def _fetch_json(url: str) -> Any:
    request = Request(url, headers={"User-Agent": "polymarket-research/1.0"})
    context = ssl.create_default_context()
    try:
        import certifi
    except ImportError:
        pass
    else:
        context = ssl.create_default_context(cafile=certifi.where())
    with urlopen(request, timeout=15, context=context) as response:
        return json.load(response)


def _one_market(payload: Any, slug: str) -> dict[str, Any]:
    markets = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(markets, list):
        raise TypeError(f"unexpected Gamma response for {slug}")
    matches = [row for row in markets if isinstance(row, dict) and row.get("slug") == slug]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one Gamma market for {slug}, got {len(matches)}")
    return matches[0]


def _positive_float(value: Any, field: str, slug: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f"invalid {field} for {slug}")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"invalid {field} for {slug}") from exc
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"invalid {field} for {slug}")
    return result


def _config_bound(value: Any, *, end: bool) -> datetime:
    if not isinstance(value, str) or not value:
        raise TypeError("reward date must be a nonempty string")
    if len(value) == 10:
        parsed_date = date.fromisoformat(value)
        return datetime.combine(parsed_date, time.max if end else time.min, tzinfo=UTC)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("reward timestamp must carry a timezone")
    return parsed.astimezone(UTC)


def active_reward_count(payload: Any, condition_id: str, observed_s: int) -> int:
    """Count active nested reward configs, rejecting incomplete pagination."""
    if not isinstance(payload, dict):
        raise TypeError(f"unexpected rewards response for {condition_id}")
    for field in ("limit", "count", "next_cursor", "data"):
        if field not in payload:
            raise ValueError(f"missing {field} in rewards response for {condition_id}")
    limit = payload["limit"]
    count = payload["count"]
    cursor = payload["next_cursor"]
    data = payload["data"]
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError(f"invalid rewards page limit for {condition_id}")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError(f"invalid rewards page count for {condition_id}")
    if cursor != "LTE=":
        raise ValueError(f"unread rewards pagination for {condition_id}")
    if not isinstance(data, list) or len(data) != count:
        raise ValueError(f"reward count/data mismatch for {condition_id}")
    if count > 1:
        raise ValueError(f"multiple market rows for specific condition {condition_id}")

    observed = datetime.fromtimestamp(observed_s, UTC)
    active = 0
    for market in data:
        if not isinstance(market, dict) or market.get("condition_id") != condition_id:
            raise ValueError(f"wrong market row in rewards response for {condition_id}")
        configs = market.get("rewards_config")
        if not isinstance(configs, list):
            raise TypeError(f"missing nested rewards_config for {condition_id}")
        for config in configs:
            if not isinstance(config, dict) or "id" not in config:
                raise TypeError(f"invalid reward config for {condition_id}")
            rate = config.get("rate_per_day")
            if isinstance(rate, bool) or not isinstance(rate, (int, float)):
                raise TypeError(f"invalid reward rate for {condition_id}")
            if not math.isfinite(float(rate)) or float(rate) < 0:
                raise ValueError(f"invalid reward rate for {condition_id}")
            start = _config_bound(config.get("start_date"), end=False)
            end = _config_bound(config.get("end_date"), end=True)
            if end < start:
                raise ValueError(f"reward end precedes start for {condition_id}")
            if float(rate) > 0 and start <= observed <= end:
                active += 1
    return active


def fetch_btc5m_reward_checks(
    now_s: int,
    *,
    fetch_json: Callable[[str], Any] = _fetch_json,
) -> list[RewardCheck]:
    """Query native and sponsored rewards for current plus three BTC 5m markets."""
    current_start = (int(now_s) // MARKET_SECONDS) * MARKET_SECONDS
    checks: list[RewardCheck] = []
    for offset in range(4):
        start_s = current_start + offset * MARKET_SECONDS
        slug = f"btc-updown-5m-{start_s}"
        gamma_url = GAMMA_MARKET_URL.format(slug=quote(slug, safe="-"))
        market = _one_market(fetch_json(gamma_url), slug)
        condition_id = market.get("conditionId") or market.get("condition_id")
        if not isinstance(condition_id, str) or not condition_id:
            raise ValueError(f"missing condition id for {slug}")
        min_order_size = _positive_float(market.get("orderMinSize"), "orderMinSize", slug)
        min_tick_size = _positive_float(
            market.get("orderPriceMinTickSize"), "orderPriceMinTickSize", slug
        )
        rewards_url = REWARDS_URL.format(condition_id=quote(condition_id, safe=""))
        native = active_reward_count(fetch_json(rewards_url), condition_id, now_s)
        sponsored = active_reward_count(
            fetch_json(f"{rewards_url}?sponsored=true"), condition_id, now_s
        )
        checks.append(
            RewardCheck(
                start_s=start_s,
                condition_id=condition_id,
                slug=slug,
                min_order_size=min_order_size,
                min_tick_size=min_tick_size,
                native_active_config_count=native,
                sponsored_active_config_count=sponsored,
            )
        )
    return checks


def _money(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def _best_rounding_floor(checks: list[RewardCheck]) -> RoundingFloor | None:
    if len(checks) != 4:
        return None
    candidates = [
        rounded_fee_floor(row.min_tick_size, SIZE_INCREMENT, row.min_order_size)
        for row in checks
    ]
    return min(candidates, key=lambda row: row.fee_per_wv)


def render_report(observed_at: str, checks: list[RewardCheck]) -> str:
    floor = _best_rounding_floor(checks)
    fee_per_wv = floor.fee_per_wv if floor is not None else 0.0
    rows = tier_economics(fee_per_wv)
    complete_market_check = len(checks) == 4
    no_active_pool = complete_market_check and all(
        row.native_active_config_count == 0 and row.sponsored_active_config_count == 0
        for row in checks
    )
    all_tiers_negative = floor is not None and all(row.subsidy_net < 0 for row in rows)
    verdict = "REJECTED" if no_active_pool and all_tiers_negative else "INCOMPLETE"

    lines = [
        "# Family 30: Reward-subsidy standalone audit",
        "",
        f"**Observed:** {observed_at}",
        f"**Verdict:** **{verdict}**",
        "",
        "## Frozen mechanism",
        "",
        "Before rounding, crypto weighted volume is `shares × p × (1-p) × 2.3` ",
        "and the fee is `shares × 0.07 × p × (1-p)`, so nominal fee/wV is ",
        "price-invariant at `0.07 / 2.3 = 3.043478%`. That identity is not used ",
        "as an executable lower bound because Polymarket truncates fees to five ",
        "decimal places and charges zero below the 0.00001 quantum.",
        "",
    ]
    if floor is not None:
        lines.extend(
            [
                "Using each checked market's live tick and minimum-order settings, ",
                "and allowing a minimum order to fragment into two-decimal partial ",
                "fills, exhaustive enumeration finds the most favourable rule-derived ",
                f"fee/wV ratio is `{100.0 * floor.fee_per_wv:.6f}%`: a ",
                f"{floor.fill_shares:.2f}-share fill at {floor.price:.2f} has raw ",
                f"fee {floor.raw_fee:.8f}, rounded fee {floor.rounded_fee:.5f}. ",
                "This is a theoretical arithmetic witness, not evidence that a ",
                "minimum taker order can repeatedly obtain that fragmentation. ",
                "The optimistic arithmetic screen sets spread, slippage, adverse ",
                "selection, and market risk to zero; it does not claim those costs ",
                "are zero in trading.",
                "",
            ]
        )
    else:
        lines.extend(["No complete price/size grid under the venue precision rules was established.", ""])
    lines.extend(
        [
            "The first tier receives no rebate. For each later tier, the already-",
            "unlocked prior-tier rebate is optimistically applied to the next wV ",
            "tranche; no rebate is backfilled onto earlier qualifying volume.",
            "",
            "## Tier economics",
            "",
            "| Tier | Required wV | Tier rebate | Optimistic fees to qualify | Cumulative bonus | Subsidy net | Future residual fee / wV |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in rows:
        lines.append(
            "| "
            f"{row.name} | {row.threshold_wv:,.0f} | {100 * row.rebate_fraction:.0f}% | "
            f"{_money(row.gross_fee_floor)} | {_money(row.cumulative_bonus)} | "
            f"{_money(row.subsidy_net)} | {100.0 * row.residual_fee_per_wv:.4f}% |"
        )

    maximum_residual = rows[-1].residual_fee_per_wv if rows else float("nan")
    lines.append("")
    if all_tiers_negative:
        lines.extend(
            [
                "Every tier remains negative before trading friction or prediction ",
                (
                    f"risk. Even the maximum 50% future rebate leaves "
                    f"{100.0 * maximum_residual:.6f}% per weighted-volume dollar."
                ),
            ]
        )
    else:
        lines.extend(
            [
                "The arithmetic screen cannot reject subsidy alpha: the enumerated ",
                "price/size grid under the venue precision rules contains a raw fee ",
                "below one quantum, so repeated ",
                "fee-free fragments would leave the one-time bonuses positive.",
            ]
        )
    lines.extend(
        [
            "The maker rebate was already tested in Family 24 and is not counted again here.",
            "",
            "## Live reward-configuration check",
            "",
            "Both the native and `sponsored=true` views of the official raw endpoint ",
            "were read. Nested configurations were counted only when their dates covered ",
            "the observation instant and `rate_per_day` was positive.",
            "",
            "| Start (Unix s) | Market | Min order | Tick | Native active | Active with sponsored |",
            "|---:|---|---:|---:|---:|---:|",
        ]
    )
    for check in checks:
        lines.append(
            f"| {check.start_s} | `{check.slug}` | {check.min_order_size:g} | "
            f"{check.min_tick_size:g} | {check.native_active_config_count} | "
            f"{check.sponsored_active_config_count} |"
        )
    if not checks:
        lines.append("| — | — | — | — | not checked | not checked |")

    lines.extend(["", "## Decision", ""])
    if verdict == "REJECTED":
        lines.extend(
            [
                "Family 30 is rejected as standalone alpha. The current and next ",
                "three BTC 5m markets have no active native or sponsored liquidity-",
                "reward configuration, the special August TWAP allocation has ended, ",
                "and even maximally favourable fee rounding plus previously unlocked ",
                "taker rebates cannot make any one-time tier bonus cover qualification ",
                "cost. Artificial volume and self-matching are prohibited.",
                "",
                "If a trade has positive expectancy before subsidy, that profit belongs to that underlying strategy; ",
                "it is not evidence for subsidy-only alpha.",
            ]
        )
    else:
        lines.extend(
            [
                "Family 30 exposes a zero-fee dust-fill candidate but does not yet prove ",
                "a strategy. A 0.01-share partial fill at a one-cent outcome price is ",
                "below the documented fee quantum, while submitted orders still require ",
                "at least five shares. The next audit must establish how much genuine, ",
                "non-self-matched weighted volume a taker can deliberately obtain through ",
                "such fragments and charge spread, adverse selection, settlement loss, ",
                "and operational limits. The arithmetic witness alone is not evidence of executable capacity.",
            ]
        )

    lines.extend(
        [
            "",
            "## Sources",
            "",
            "- https://docs.polymarket.com/programs/taker-rebates",
            "- https://docs.polymarket.com/programs/maker-rebates",
            "- https://docs.polymarket.com/programs/liquidity-rewards",
            "- https://docs.polymarket.com/trading/fees",
            "- https://docs.polymarket.com/trading/place-orders",
            "- https://docs.polymarket.com/api-reference/rewards/get-raw-rewards-for-a-specific-market",
            "",
        ]
    )
    return "\n".join(line.rstrip() for line in lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--now", type=int, help="Unix seconds; defaults to current UTC")
    args = parser.parse_args()

    now_s = args.now if args.now is not None else int(datetime.now(UTC).timestamp())
    observed_at = datetime.fromtimestamp(now_s, UTC).isoformat().replace("+00:00", "Z")
    checks = fetch_btc5m_reward_checks(now_s)
    report = render_report(observed_at, checks)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report, encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
