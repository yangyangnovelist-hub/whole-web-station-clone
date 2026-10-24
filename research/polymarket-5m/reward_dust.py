"""Public-row diagnostic for Polymarket's zero-fee dust-fill quantum.

The Public Data API exposes the taker side but not a unique trade id, parent
order, match fragments, actual charged fee, or credited weighted volume.  A
single row may aggregate independently rounded fragments, so these rows are
neither an upper nor a lower bound on fee-free capacity.  This module therefore
never labels the mechanism executable from public rows alone.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import ssl
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any, cast
from urllib.request import Request, urlopen

import certifi
import pm_outcomes
import resolved

FEE_RATE = Decimal("0.07")
CATEGORY_WEIGHT = Decimal("2.3")
FEE_QUANTUM = Decimal("0.00001")
BRONZE_WEIGHTED_VOLUME = Decimal(2000)
REQUIRED_COMPLETE_DAYS = 30
SECONDS_PER_DAY = 86_400
MARKET_SECONDS = 300
TRADE_CAPTURE_SECONDS = 3_600
TradeRecord = Sequence[Any]


def certified_json_get(url: str) -> Any:
    """Read official JSON endpoints with an explicit maintained CA bundle."""
    request = Request(url, headers={"User-Agent": "polymarket-reward-dust/1.0"})
    context = ssl.create_default_context(cafile=certifi.where())
    with urlopen(request, timeout=30, context=context) as response:
        return json.loads(response.read())


@dataclass(frozen=True)
class PublicTrade:
    public_row_id: str
    occurrence: int
    market_id: str
    market_start_s: int
    timestamp_s: int
    side: str
    asset: str
    outcome: str
    price: Decimal
    size: Decimal
    transaction_hash: str
    proxy_wallet: str
    settlement_value: Decimal


@dataclass(frozen=True)
class CapacitySummary:
    public_rows: int
    theoretical_zero_fee_rows: int
    nonzero_fee_rows: int
    zero_fee_weighted_volume: Decimal
    zero_fee_settlement_pnl: Decimal
    complete_days: int
    complete_markets: int
    bronze_reached: bool
    evidence_class: str


@dataclass(frozen=True)
class MarketMeta:
    start_s: int
    slug: str
    condition_id: str
    token_up: str
    token_down: str
    up_won: bool
    source: str
    source_window_s: int


@dataclass(frozen=True)
class MarketAudit:
    market: MarketMeta
    public_rows: int
    dust_rows: tuple[PublicTrade, ...]
    dust_weighted_volume: Decimal
    dust_settlement_pnl: Decimal
    cent_size_nonzero_rows: int
    cent_size_nonzero_settlement_pnl: Decimal
    pages: int
    windows: int


def raw_taker_fee(price: Decimal, shares: Decimal) -> Decimal:
    _validate_price_size(price, shares)
    return shares * FEE_RATE * price * (Decimal(1) - price)


def weighted_volume(price: Decimal, shares: Decimal) -> Decimal:
    _validate_price_size(price, shares)
    return shares * price * (Decimal(1) - price) * CATEGORY_WEIGHT


def _validate_price_size(price: Decimal, shares: Decimal) -> None:
    if not Decimal(0) < price < Decimal(1):
        raise ValueError("price must be strictly between zero and one")
    if shares <= 0:
        raise ValueError("shares must be positive")


def is_below_fee_quantum(raw_fee: Decimal) -> bool:
    if raw_fee < 0:
        raise ValueError("fee must be nonnegative")
    return raw_fee < FEE_QUANTUM


def is_fee_free(price: Decimal, shares: Decimal) -> bool:
    return is_below_fee_quantum(raw_taker_fee(price, shares))


def fills_required(target_weighted_volume: Decimal, per_fill: Decimal) -> int:
    if target_weighted_volume <= 0 or per_fill <= 0:
        raise ValueError("target and per-fill weighted volume must be positive")
    return int((target_weighted_volume / per_fill).to_integral_value(rounding=ROUND_CEILING))


def _record_key(record: TradeRecord) -> tuple[str, ...]:
    if len(record) != len(resolved.TRADE_FIELDS):
        raise ValueError("public trade row must contain all eight retained fields")
    return tuple(str(value) for value in record)


def normalise_public_rows(
    records: Sequence[TradeRecord],
    *,
    market_id: str,
    market_start_s: int,
    token_up: str,
    token_down: str,
    up_won: bool,
) -> list[PublicTrade]:
    """Retain public-row multiplicity while assigning deterministic local ids.

    The local id is deliberately named ``public_row_id``: it is not a venue
    trade id or parent-order id and must not be used as proof of either.
    """
    occurrences: Counter[tuple[str, ...]] = Counter()
    rows: list[PublicTrade] = []
    for record in records:
        key = _record_key(record)
        occurrence = occurrences[key]
        occurrences[key] += 1
        timestamp, side, asset, price_raw, size_raw, _outcome_index, tx, wallet = record
        asset = str(asset)
        if asset == str(token_up):
            outcome = "Up"
            settlement = Decimal(1 if up_won else 0)
        elif asset == str(token_down):
            outcome = "Down"
            settlement = Decimal(0 if up_won else 1)
        else:
            raise ValueError(f"unknown token {asset!r} for market {market_id}")
        side = str(side).upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError(f"invalid taker side {side!r}")
        price = Decimal(str(price_raw))
        size = Decimal(str(size_raw))
        _validate_price_size(price, size)
        digest = hashlib.sha256("\x1f".join(key).encode()).hexdigest()[:20]
        rows.append(
            PublicTrade(
                public_row_id=f"{market_id}:{digest}:{occurrence}",
                occurrence=occurrence,
                market_id=market_id,
                market_start_s=int(market_start_s),
                timestamp_s=int(timestamp),
                side=side,
                asset=asset,
                outcome=outcome,
                price=price,
                size=size,
                transaction_hash=str(tx or ""),
                proxy_wallet=str(wallet or "").lower(),
                settlement_value=settlement,
            )
        )
    return rows


def settlement_pnl(row: PublicTrade) -> Decimal:
    if row.side == "BUY":
        return row.size * (row.settlement_value - row.price)
    return row.size * (row.price - row.settlement_value)


def summarise_capacity(
    rows: list[PublicTrade], *, complete_days: int, complete_markets: int
) -> CapacitySummary:
    zero = [row for row in rows if is_fee_free(row.price, row.size)]
    zero_wv = sum((weighted_volume(row.price, row.size) for row in zero), Decimal(0))
    zero_pnl = sum((settlement_pnl(row) for row in zero), Decimal(0))
    return CapacitySummary(
        public_rows=len(rows),
        theoretical_zero_fee_rows=len(zero),
        nonzero_fee_rows=len(rows) - len(zero),
        zero_fee_weighted_volume=zero_wv,
        zero_fee_settlement_pnl=zero_pnl,
        complete_days=complete_days,
        complete_markets=complete_markets,
        bronze_reached=zero_wv >= BRONZE_WEIGHTED_VOLUME,
        evidence_class="public_row_diagnostic_only",
    )


def fetch_taker_records(
    http: Any,
    condition_id: str,
    *,
    start_s: int,
    end_s: int | None,
    page: int = resolved.PAGE,
    max_offset: int = resolved.MAX_OFFSET,
) -> tuple[list[TradeRecord], dict[str, Any]]:
    """Fetch every public taker row, explicitly pinning taker-only semantics."""

    def get(url: str) -> Any:
        separator = "&" if "?" in url else "?"
        return http.get(f"{url}{separator}takerOnly=true", slim=resolved.slim_trades)

    return cast(
        tuple[list[TradeRecord], dict[str, Any]],
        resolved.fetch_trades(
            get,
            condition_id,
            start_s,
            page=page,
            max_offset=max_offset,
            end=end_s,
        ),
    )


def require_complete_fetch(
    records: Sequence[TradeRecord], info: dict[str, Any]
) -> Sequence[TradeRecord]:
    if info.get("truncated"):
        raise RuntimeError(f"public trades fetch was truncated: {info.get('note', '')}")
    return records


def audit_public_records(
    market: MarketMeta,
    records: Sequence[TradeRecord],
    info: dict[str, Any],
) -> MarketAudit:
    require_complete_fetch(records, info)
    rows = normalise_public_rows(
        records,
        market_id=market.condition_id,
        market_start_s=market.start_s,
        token_up=market.token_up,
        token_down=market.token_down,
        up_won=market.up_won,
    )
    dust_rows = tuple(row for row in rows if is_fee_free(row.price, row.size))
    cent_control = [
        row for row in rows
        if row.size == Decimal("0.01") and not is_fee_free(row.price, row.size)
    ]
    return MarketAudit(
        market=market,
        public_rows=len(rows),
        dust_rows=dust_rows,
        dust_weighted_volume=sum(
            (weighted_volume(row.price, row.size) for row in dust_rows), Decimal(0)
        ),
        dust_settlement_pnl=sum((settlement_pnl(row) for row in dust_rows), Decimal(0)),
        cent_size_nonzero_rows=len(cent_control),
        cent_size_nonzero_settlement_pnl=sum(
            (settlement_pnl(row) for row in cent_control), Decimal(0)
        ),
        pages=int(info.get("pages", 0)),
        windows=int(info.get("windows", 0)),
    )


def _counter_text(values: list[str]) -> str:
    counts = Counter(values)
    return ", ".join(f"{key}: {counts[key]:,}" for key in sorted(counts)) or "none"


def render_report(
    *,
    observed_at: str,
    start_day: str,
    requested_days: int,
    complete_days: int,
    audits: list[MarketAudit],
) -> str:
    dust_rows = [row for audit in audits for row in audit.dust_rows]
    total_rows = sum(audit.public_rows for audit in audits)
    total_wv = sum((audit.dust_weighted_volume for audit in audits), Decimal(0))
    total_pnl = sum((audit.dust_settlement_pnl for audit in audits), Decimal(0))
    total_notional = sum((row.price * row.size for row in dust_rows), Decimal(0))
    control_rows = sum(audit.cent_size_nonzero_rows for audit in audits)
    control_pnl = sum(
        (audit.cent_size_nonzero_settlement_pnl for audit in audits), Decimal(0)
    )
    daily_wv = total_wv / complete_days if complete_days else Decimal(0)
    projected_30d = daily_wv * 30
    bronze_fraction = projected_30d / BRONZE_WEIGHTED_VOLUME
    required_capacity_multiple = (
        BRONZE_WEIGHTED_VOLUME / projected_30d
        if projected_30d > 0 else Decimal("Infinity")
    )
    average_wv = total_wv / len(dust_rows) if dust_rows else Decimal(0)
    observed_mix_fills = (
        fills_required(BRONZE_WEIGHTED_VOLUME, average_wv) if average_wv else 0
    )
    witness_wv = weighted_volume(Decimal("0.01"), Decimal("0.01"))
    witness_fills = fills_required(BRONZE_WEIGHTED_VOLUME, witness_wv)
    markets_with_dust = len({row.market_id for row in dust_rows})
    tx_count = len({row.transaction_hash for row in dust_rows})
    wallet_counts = Counter(row.proxy_wallet for row in dust_rows)
    top_wallet_share = (
        Decimal(max(wallet_counts.values())) / len(dust_rows) if dust_rows else Decimal(0)
    )
    lines = [
        "# Family 31: Zero-fee dust-fill capacity audit",
        "",
        f"**Observed:** {observed_at}",
        "**Verdict:** **INCOMPLETE**",
        "**Evidence class:** public-row diagnostic only",
        "",
        "## Scope and identification",
        "",
        (
            f"The requested window starts {start_day}; {complete_days} of {requested_days} complete UTC days "
            f"and {len(audits):,} resolved BTC five-minute markets are currently included. "
            f"The public taker-only feed contributed {total_rows:,} rows. Each condition is read from its "
            "window open through one hour later, covering the five-minute trading window and delayed chain stamps."
        ),
        "",
        (
            "A row is a theoretical zero-fee candidate only when "
            "`shares × 0.07 × price × (1-price) < 0.00001`. The inequality is strict. "
            "Public rows do not expose a unique trade id, parent order, match fragments, actual charged fee, "
            "or credited weighted volume. A larger public row can conceal several independently rounded match "
            "fragments. Transaction hashes and local occurrence counters therefore support overlap-safe row "
            "counting, not a complete upper bound on fee-free fragments or execution attribution."
        ),
        "",
        "## Capacity",
        "",
        "| Metric | Directly visible public rows |",
        "|---|---:|",
        f"| Theoretical zero-fee rows | {len(dust_rows):,} |",
        f"| Distinct transaction hashes | {tx_count:,} |",
        f"| Markets with a candidate | {markets_with_dust:,} |",
        f"| Nominal weighted volume | {total_wv} |",
        f"| Linear 30-day weighted volume at the observed daily rate | {projected_30d} |",
        f"| Fraction of Bronze (2,000 wV) | {bronze_fraction:.6%} |",
        f"| Daily capacity multiple needed inside 30 days | {required_capacity_multiple:,.1f}× |",
        f"| Fills to Bronze at the observed candidate mix | {observed_mix_fills:,} |",
        f"| Candidate notional | ${total_notional} |",
        f"| Hold-to-settlement PnL before any operational cost | ${total_pnl} |",
        f"| Largest candidate-wallet share | {top_wallet_share:.2%} |",
        "",
        (
            f"The canonical 0.01-share fill at price 0.01 contributes {witness_wv} wV, so even that "
            f"reference case needs {witness_fills:,} fills to reach Bronze. Bronze was not reached in the "
            "directly visible sample. At the observed rate Bronze is unreachable within a rolling 30-day window."
        ),
        "",
        "## Fixed controls",
        "",
        f"Candidate taker sides: {_counter_text([row.side for row in dust_rows])}.",
        f"Candidate outcomes: {_counter_text([row.outcome for row in dust_rows])}.",
        f"Candidate prices: {_counter_text([str(row.price) for row in dust_rows])}.",
        f"Candidate sizes: {_counter_text([str(row.size) for row in dust_rows])}.",
        (
            f"The fixed ordinary-fee control contains {control_rows:,} other 0.01-share public taker rows; "
            f"their hold-to-settlement PnL is ${control_pnl}. These controls are descriptive only because "
            "neither arm is a controllable parent-order sample."
        ),
        "",
        "## Decision",
        "",
        (
            "The sample cannot promote a strategy. Directly visible dust rows miss Bronze by orders of magnitude, "
            "but omitted match-fragment boundaries mean this public-row scan cannot support formal capacity rejection. "
            "No public row proves that an account can choose the partial-fill size or receive nominal weighted-volume "
            "credit. The frozen 30-day scan remains a concentration diagnostic; rejection additionally requires a "
            "fragment-complete chain or authenticated-account bound."
        ),
        "",
        "## Sources",
        "",
        "- https://docs.polymarket.com/trading/fees",
        "- https://docs.polymarket.com/programs/taker-rebates",
        "- https://docs.polymarket.com/api-reference/feeds/list-trades",
        "- https://docs.polymarket.com/api-reference/trade/get-trades",
        "",
    ]
    return "\n".join(lines)


CANDIDATE_FIELDS = (
    "public_row_id",
    "market_id",
    "market_start_s",
    "timestamp_s",
    "side",
    "asset",
    "outcome",
    "price",
    "size",
    "transaction_hash",
    "proxy_wallet",
    "settlement_value",
    "raw_fee",
    "nominal_weighted_volume",
    "settlement_pnl",
)


def write_candidate_csv(path: Path, rows: list[PublicTrade]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(temporary, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CANDIDATE_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "public_row_id": row.public_row_id,
                "market_id": row.market_id,
                "market_start_s": row.market_start_s,
                "timestamp_s": row.timestamp_s,
                "side": row.side,
                "asset": row.asset,
                "outcome": row.outcome,
                "price": str(row.price),
                "size": str(row.size),
                "transaction_hash": row.transaction_hash,
                "proxy_wallet": row.proxy_wallet,
                "settlement_value": str(row.settlement_value),
                "raw_fee": str(raw_taker_fee(row.price, row.size)),
                "nominal_weighted_volume": str(weighted_volume(row.price, row.size)),
                "settlement_pnl": str(settlement_pnl(row)),
            })
    temporary.replace(path)


def audit_day(
    http: Any,
    day_start_s: int,
    *,
    workers: int = 2,
    log: Callable[[str], None] = print,
) -> list[MarketAudit]:
    markets = fetch_btc5m_markets(http, day_start_s)

    def one(market: MarketMeta) -> MarketAudit:
        records, info = fetch_taker_records(
            http,
            market.condition_id,
            start_s=market.start_s,
            end_s=market.start_s + TRADE_CAPTURE_SECONDS - 1,
        )
        return audit_public_records(market, records, info)

    audits: list[MarketAudit] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for index, audit in enumerate(executor.map(one, markets), 1):
            audits.append(audit)
            if index % 48 == 0:
                log(
                    f"{datetime.fromtimestamp(day_start_s, UTC).date()}: "
                    f"{index}/288 markets, {sum(row.public_rows for row in audits):,} rows"
                )
    if len(audits) != 288:
        raise RuntimeError(f"incomplete UTC day: expected 288 audits, got {len(audits)}")
    return audits


def audit_date_range(
    http: Any,
    start_day: str,
    days: int,
    *,
    workers: int = 2,
    log: Callable[[str], None] = print,
) -> list[MarketAudit]:
    if days <= 0:
        raise ValueError("days must be positive")
    start = datetime.fromisoformat(start_day).replace(tzinfo=UTC)
    if start.time() != datetime.min.time():
        raise ValueError("start day must be a UTC date")
    start_s = int(start.timestamp())
    audits: list[MarketAudit] = []
    for day_index in range(days):
        day_start_s = start_s + day_index * SECONDS_PER_DAY
        audits.extend(audit_day(http, day_start_s, workers=workers, log=log))
        log(f"complete days: {day_index + 1}/{days}")
    return audits


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2026-09-11")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("real/reward-dust.md"))
    parser.add_argument("--csv-out", type=Path, default=Path("real/reward-dust.csv.gz"))
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--rate", type=float, default=2.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.workers <= 0 or args.rate <= 0:
        raise ValueError("workers and rate must be positive")
    http = resolved.Http(
        args.workdir / "reward-dust",
        rate=args.rate,
        opener=certified_json_get,
        tries=7,
    )
    audits = audit_date_range(
        http,
        args.start,
        args.days,
        workers=args.workers,
    )
    observed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    report = render_report(
        observed_at=observed_at,
        start_day=args.start,
        requested_days=REQUIRED_COMPLETE_DAYS,
        complete_days=args.days,
        audits=audits,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report, encoding="utf-8")
    candidates = [row for audit in audits for row in audit.dust_rows]
    write_candidate_csv(args.csv_out, candidates)
    print(
        f"wrote {args.out} and {args.csv_out}: {len(audits):,} markets, "
        f"{sum(audit.public_rows for audit in audits):,} rows, {len(candidates):,} candidates"
    )
    return 0


def validate_complete_day(day_start_s: int, market_starts: list[int]) -> list[int]:
    if day_start_s % SECONDS_PER_DAY:
        raise ValueError("day start must be midnight UTC")
    if len(market_starts) != 288:
        raise ValueError("a complete UTC day must contain exactly 288 BTC 5m markets")
    if len(set(market_starts)) != len(market_starts):
        raise ValueError("market starts must be unique")
    expected = list(range(day_start_s, day_start_s + SECONDS_PER_DAY, MARKET_SECONDS))
    if sorted(market_starts) != expected:
        raise ValueError("market starts must cover every five-minute slot of the UTC day")
    return expected


def gamma_up_index(http: Any, row: Mapping[str, Any]) -> int:
    event = http.get(f"{pm_outcomes.GAMMA}/slug/{row['slug']}")
    if not isinstance(event, Mapping):
        raise ValueError(f"missing Gamma outcome order for {row.get('slug')}")
    markets = [
        market for market in event.get("markets", [])
        if market.get("conditionId") == row.get("condition_id")
    ]
    if len(markets) != 1:
        raise ValueError(f"ambiguous Gamma outcome order for {row.get('slug')}")
    market = markets[0]
    outcomes = json.loads(market.get("outcomes") or "[]")
    tokens = json.loads(market.get("clobTokenIds") or "[]")
    if sorted(outcomes) != ["Down", "Up"] or len(tokens) != 2:
        raise ValueError(f"invalid Gamma outcome order for {row.get('slug')}")
    index = outcomes.index("Up")
    if str(tokens[index]) != str(row.get("token_up")):
        raise ValueError(f"conflicting Gamma Up token for {row.get('slug')}")
    return index


def fetch_btc5m_markets(
    http: Any,
    day_start_s: int,
    *,
    batch: int = 100,
    resolve_unsettled: Callable[[str, int], bool | None] | None = None,
) -> list[MarketMeta]:
    expected = list(range(day_start_s, day_start_s + SECONDS_PER_DAY, MARKET_SECONDS))
    slugs = [pm_outcomes.slug("5m", start) for start in expected]
    raw: list[dict[str, Any]] = []
    for offset in range(0, len(slugs), batch):
        raw.extend(pm_outcomes.fetch_batch(slugs[offset:offset + batch], get=http.get))

    # Gamma occasionally returns a successful but incomplete multi-slug response.  Retry only the
    # omitted slots through distinct singleton URLs so a cached partial batch cannot silently turn
    # a complete historical day into a source failure.  Persistent omissions still fail closed.
    by_start: dict[int, dict[str, Any]] = {}
    for row in raw:
        start = int(row["T"])
        previous = by_start.setdefault(start, row)
        if previous != row:
            raise ValueError(f"conflicting Gamma identity for {row.get('slug')}")
    for start in sorted(set(expected) - set(by_start)):
        slug = pm_outcomes.slug("5m", start)
        event = http.get(f"{pm_outcomes.GAMMA}/slug/{slug}")
        recovered = (
            [pm_outcomes.parse_event(event)]
            if isinstance(event, dict) and event.get("markets")
            else []
        )
        for row in recovered:
            recovered_start = int(row["T"])
            previous = by_start.setdefault(recovered_start, row)
            if previous != row:
                raise ValueError(f"conflicting Gamma identity for {row.get('slug')}")
    raw = list(by_start.values())
    validate_complete_day(day_start_s, [int(row["T"]) for row in raw])
    markets: list[MarketMeta] = []
    for row in sorted(raw, key=lambda item: int(item["T"])):
        condition_id = row.get("condition_id")
        token_up = row.get("token_up")
        token_down = row.get("token_down")
        up = row.get("up")
        if not condition_id or not token_up or not token_down:
            raise ValueError(f"incomplete Gamma identity for {row.get('slug')}")
        if up not in (0.0, 1.0) and resolve_unsettled is not None:
            resolved_up = resolve_unsettled(
                str(condition_id),
                gamma_up_index(http, row),
            )
            if isinstance(resolved_up, bool):
                up = float(resolved_up)
        if up not in (0.0, 1.0):
            raise ValueError(f"unresolved Gamma outcome for {row.get('slug')}")
        markets.append(
            MarketMeta(
                start_s=int(row["T"]),
                slug=str(row["slug"]),
                condition_id=str(condition_id),
                token_up=str(token_up),
                token_down=str(token_down),
                up_won=bool(up),
                source=str(row.get("source") or ""),
                source_window_s=int(row.get("w") or 0),
            )
        )
    return markets


if __name__ == "__main__":
    raise SystemExit(main())
