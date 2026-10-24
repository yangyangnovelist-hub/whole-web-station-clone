"""Pure, fail-closed strategy kernel for Family 32 wallet following."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import ROUND_DOWN, Decimal, localcontext
from typing import Any, Literal, Mapping

CHAIN_ID = 137
CTF_EXCHANGE_V2_LABEL = "CTF_EXCHANGE_V2"
CTF_EXCHANGE_V2_ADDRESS = "0xE111180000d2663C0091e4f400237545B87B996B"
RAW_SCALE = 1_000_000
FEE_RATE = Decimal("0.07")
FEE_QUANTUM = Decimal("0.00001")
SOURCE_AMOUNT_QUANTUM = Decimal("0.000001")
SOURCE_FLOAT_TOLERANCE = Decimal("0.000000001")
SOURCE_PRICE_TOLERANCE = Decimal("0.000001")
MIN_SHARES = Decimal(5)
Side = Literal["Up", "Down"]
EventKey = tuple[int, str, str, int]


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _unsigned_integer(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an unsigned integer")
    text = str(value)
    if re.fullmatch(r"[0-9]+", text) is None:
        raise ValueError(f"{field} must be a lossless unsigned integer")
    return int(text)


def _human_units(value: int) -> Decimal:
    whole, fraction = divmod(value, RAW_SCALE)
    return Decimal(f"{whole}.{fraction:06d}")


def _exact_decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{field} must use exact decimal input")
    try:
        if isinstance(value, Decimal):
            result = value
        elif isinstance(value, (str, int)):
            result = Decimal(value)
        else:
            raise TypeError
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be a finite decimal")
    return result


def _one_name(
    primary: object | None,
    alias: object | None,
    primary_name: str,
    alias_name: str,
) -> object:
    if primary is not None and alias is not None:
        raise ValueError(f"use either {primary_name} or {alias_name}, not both")
    value = primary if primary is not None else alias
    if value is None:
        raise ValueError(f"{primary_name} is required")
    return value


@dataclass(frozen=True, init=False)
class TokenMeta:
    day: date
    start_s: int
    market: str
    token: str
    side: Side
    up_won: bool

    def __init__(
        self,
        day: date | None = None,
        start_s: int | None = None,
        market: str | None = None,
        token: str | None = None,
        side: Side | None = None,
        up_won: bool | None = None,
        *,
        market_day: date | None = None,
        market_start_s: int | None = None,
        market_id: str | None = None,
        token_id: str | None = None,
    ) -> None:
        values = (
            _one_name(day, market_day, "day", "market_day"),
            _one_name(start_s, market_start_s, "start_s", "market_start_s"),
            _one_name(market, market_id, "market", "market_id"),
            _one_name(token, token_id, "token", "token_id"),
        )
        object.__setattr__(self, "day", values[0])
        object.__setattr__(self, "start_s", values[1])
        object.__setattr__(self, "market", values[2])
        object.__setattr__(self, "token", values[3])
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "up_won", up_won)
        self.__post_init__()

    def __post_init__(self) -> None:
        if type(self.day) is not date:
            raise ValueError("day must be a UTC calendar date")
        if (
            isinstance(self.start_s, bool)
            or not isinstance(self.start_s, int)
            or self.start_s < 0
        ):
            raise ValueError("start_s must be a nonnegative integer second")
        market = _text(self.market, "market")
        token = _text(self.token, "token")
        if self.side not in ("Up", "Down"):
            raise ValueError("side must be Up or Down")
        if not isinstance(self.up_won, bool):
            raise ValueError("up_won must be boolean")
        object.__setattr__(self, "market", market)
        object.__setattr__(self, "token", token)

    @property
    def settlement(self) -> Decimal:
        return Decimal(int(self.up_won == (self.side == "Up")))

    @property
    def market_day(self) -> date:
        return self.day

    @property
    def market_start_s(self) -> int:
        return self.start_s

    @property
    def market_id(self) -> str:
        return self.market

    @property
    def token_id(self) -> str:
        return self.token


@dataclass(frozen=True)
class ParentBuy:
    day: date
    start_s: int
    market: str
    token: str
    opposite_token: str | None
    side: Side
    up_won: bool
    wallet: str
    timestamp_s: int
    price: Decimal
    shares: Decimal
    fee: Decimal
    event_key: EventKey
    source_event_id: str
    transaction_hash: str | None
    order_hash: str
    block_number: int
    log_index: int

    def __post_init__(self) -> None:
        if type(self.day) is not date:
            raise ValueError("day must be a UTC calendar date")
        if (
            isinstance(self.start_s, bool)
            or not isinstance(self.start_s, int)
            or self.start_s < 0
        ):
            raise ValueError("start_s must be a nonnegative integer second")
        market = _text(self.market, "market")
        token = _text(self.token, "token")
        opposite_token = (
            _text(self.opposite_token, "opposite_token")
            if self.opposite_token is not None
            else None
        )
        if opposite_token == token:
            raise ValueError("opposite_token must differ from token")
        if self.side not in ("Up", "Down"):
            raise ValueError("side must be Up or Down")
        if not isinstance(self.up_won, bool):
            raise ValueError("up_won must be boolean")
        wallet = _text(self.wallet, "wallet").lower()
        if (
            isinstance(self.timestamp_s, bool)
            or not isinstance(self.timestamp_s, int)
            or self.timestamp_s < 0
        ):
            raise ValueError("timestamp_s must be a nonnegative integer second")
        price = _exact_decimal(self.price, "price")
        shares = _exact_decimal(self.shares, "shares")
        fee = _exact_decimal(self.fee, "fee")
        if not Decimal(0) < price < Decimal(1):
            raise ValueError("price must be strictly between zero and one")
        if shares <= 0:
            raise ValueError("parent BUY shares must be positive")
        if fee < 0 or fee > shares * price:
            raise ValueError("fee must be between zero and collateral paid")
        source_event_id = _text(self.source_event_id, "source_event_id").lower()
        transaction_hash = (
            _text(self.transaction_hash, "transaction_hash").lower()
            if self.transaction_hash is not None
            else None
        )
        order_hash = _text(self.order_hash, "order_hash").lower()
        if (
            isinstance(self.block_number, bool)
            or not isinstance(self.block_number, int)
            or self.block_number < 0
        ):
            raise ValueError("block_number must be nonnegative")
        if (
            isinstance(self.log_index, bool)
            or not isinstance(self.log_index, int)
            or self.log_index < 0
        ):
            raise ValueError("log_index must be nonnegative")
        event_key = _validate_event_key(self.event_key)
        if event_key[2] != source_event_id or event_key[3] != self.log_index:
            raise ValueError("event_key does not match source_event_id and log_index")
        object.__setattr__(self, "market", market)
        object.__setattr__(self, "token", token)
        object.__setattr__(self, "opposite_token", opposite_token)
        object.__setattr__(self, "wallet", wallet)
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "shares", shares)
        object.__setattr__(self, "fee", fee)
        object.__setattr__(self, "event_key", event_key)
        object.__setattr__(self, "source_event_id", source_event_id)
        object.__setattr__(self, "transaction_hash", transaction_hash)
        object.__setattr__(self, "order_hash", order_hash)

    @property
    def settlement(self) -> Decimal:
        return Decimal(int(self.up_won == (self.side == "Up")))

    @property
    def fee_per_share(self) -> Decimal:
        return self.fee / self.shares


@dataclass(frozen=True)
class WalletObservation:
    day: date
    wallet: str
    market: str
    timestamp_s: int
    price: Decimal
    shares: Decimal
    fee: Decimal
    settlement: Decimal
    event_key: EventKey | None = None
    block_number: int | None = None
    log_index: int | None = None

    def __post_init__(self) -> None:
        if type(self.day) is not date:
            raise ValueError("day must be a UTC calendar date")
        wallet = _text(self.wallet, "wallet").lower()
        market = _text(self.market, "market")
        if (
            isinstance(self.timestamp_s, bool)
            or not isinstance(self.timestamp_s, int)
            or self.timestamp_s < 0
        ):
            raise ValueError("timestamp_s must be a nonnegative integer second")
        price = _exact_decimal(self.price, "price")
        shares = _exact_decimal(self.shares, "shares")
        fee = _exact_decimal(self.fee, "fee")
        settlement = _exact_decimal(self.settlement, "settlement")
        if not Decimal(0) < price < Decimal(1):
            raise ValueError("price must be strictly between zero and one")
        if shares <= 0:
            raise ValueError("observation shares must be positive")
        if fee < 0 or fee > shares * price:
            raise ValueError("fee must be between zero and collateral paid")
        if settlement not in (Decimal(0), Decimal(1)):
            raise ValueError("settlement must be zero or one")
        event_key = (
            _validate_event_key(self.event_key) if self.event_key is not None else None
        )
        if (self.block_number is None) != (self.log_index is None):
            raise ValueError("block_number and log_index must be present together")
        if self.block_number is not None and (
            isinstance(self.block_number, bool)
            or not isinstance(self.block_number, int)
            or self.block_number < 0
        ):
            raise ValueError("block_number must be nonnegative")
        if self.log_index is not None and (
            isinstance(self.log_index, bool)
            or not isinstance(self.log_index, int)
            or self.log_index < 0
        ):
            raise ValueError("log_index must be nonnegative")
        if (
            event_key is not None
            and self.log_index is not None
            and event_key[3] != self.log_index
        ):
            raise ValueError("event_key does not match log_index")
        object.__setattr__(self, "wallet", wallet)
        object.__setattr__(self, "market", market)
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "shares", shares)
        object.__setattr__(self, "fee", fee)
        object.__setattr__(self, "settlement", settlement)
        object.__setattr__(self, "event_key", event_key)

    @classmethod
    def from_parent_buy(cls, event: ParentBuy) -> WalletObservation:
        if not isinstance(event, ParentBuy):
            raise ValueError("event must be a ParentBuy")
        return cls(
            day=event.day,
            wallet=event.wallet,
            market=event.market,
            timestamp_s=event.timestamp_s,
            price=event.price,
            shares=event.shares,
            fee=event.fee,
            settlement=event.settlement,
            event_key=event.event_key,
            block_number=event.block_number,
            log_index=event.log_index,
        )


@dataclass(frozen=True)
class WalletScore:
    asof_day: date
    wallet: str
    score: Decimal
    n: int
    pnl_sum: Decimal

    def __post_init__(self) -> None:
        if type(self.asof_day) is not date:
            raise ValueError("asof_day must be a UTC calendar date")
        wallet = _text(self.wallet, "wallet").lower()
        score = _exact_decimal(self.score, "score")
        pnl_sum = _exact_decimal(self.pnl_sum, "pnl_sum")
        if isinstance(self.n, bool) or not isinstance(self.n, int) or self.n < 0:
            raise ValueError("n must be a nonnegative integer")
        if score != pnl_sum / Decimal(self.n + 20):
            raise ValueError("score must equal pnl_sum divided by n plus 20")
        object.__setattr__(self, "wallet", wallet)
        object.__setattr__(self, "score", score)
        object.__setattr__(self, "pnl_sum", pnl_sum)

    @property
    def markets(self) -> int:
        return self.n


@dataclass(frozen=True)
class BookSnapshot:
    timestamp_ms: int
    up_ask: Decimal | None
    up_ask_size: Decimal | None
    down_ask: Decimal | None
    down_ask_size: Decimal | None
    active: bool
    halted: bool
    fresh: bool = True
    market: str | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.timestamp_ms, bool)
            or not isinstance(self.timestamp_ms, int)
            or self.timestamp_ms < 0
        ):
            raise ValueError("timestamp_ms must be a nonnegative integer")
        if not isinstance(self.active, bool) or not isinstance(self.halted, bool):
            raise ValueError("active and halted must be boolean")
        if not isinstance(self.fresh, bool):
            raise ValueError("fresh must be boolean")
        if self.market is not None:
            object.__setattr__(self, "market", _text(self.market, "market"))
        for ask_field, size_field in (
            ("up_ask", "up_ask_size"),
            ("down_ask", "down_ask_size"),
        ):
            ask_value = getattr(self, ask_field)
            size_value = getattr(self, size_field)
            if ask_value is not None:
                ask = _exact_decimal(ask_value, ask_field)
                if not Decimal(0) < ask < Decimal(1):
                    raise ValueError(
                        f"{ask_field} must be strictly between zero and one"
                    )
                object.__setattr__(self, ask_field, ask)
            if size_value is not None:
                size = _exact_decimal(size_value, size_field)
                if size < 0:
                    raise ValueError(f"{size_field} must be nonnegative")
                object.__setattr__(self, size_field, size)

    def direct_ask(self, side: Side) -> tuple[Decimal | None, Decimal | None]:
        if side == "Up":
            return self.up_ask, self.up_ask_size
        if side == "Down":
            return self.down_ask, self.down_ask_size
        raise ValueError("side must be Up or Down")


@dataclass(frozen=True)
class ReplayResult:
    variant: str
    horizon_ms: int
    event_key: EventKey
    wallet: str
    market: str
    token: str
    side: Side
    target_ms: int
    snapshot_ms: int | None
    limit_price: Decimal | None
    filled: bool
    reason: str
    ask_price: Decimal | None
    ask_size: Decimal | None
    fill_price: Decimal | None
    shares: Decimal
    fee: Decimal
    settlement: Decimal
    pnl: Decimal


def _validate_event_key(event_key: object) -> EventKey:
    if not isinstance(event_key, tuple) or len(event_key) != 4:
        raise ValueError("event_key must contain chain, exchange, source id, and log index")
    chain_id, exchange, source_event_id, log_index = event_key
    if chain_id != CHAIN_ID:
        raise ValueError("event_key has the wrong chain")
    if _text(exchange, "event exchange").lower() != CTF_EXCHANGE_V2_ADDRESS.lower():
        raise ValueError("event_key has the wrong exchange")
    source_id = _text(source_event_id, "event source id").lower()
    if isinstance(log_index, bool) or not isinstance(log_index, int) or log_index < 0:
        raise ValueError("event log index must be nonnegative")
    return (chain_id, exchange.lower(), source_id, log_index)


def charged_taker_fee(shares: object, price: object) -> Decimal:
    """Return the venue's five-decimal-truncated total crypto taker fee."""
    exact_shares = _exact_decimal(shares, "shares")
    exact_price = _exact_decimal(price, "price")
    if exact_shares <= 0:
        raise ValueError("shares must be positive")
    if not Decimal(0) < exact_price < Decimal(1):
        raise ValueError("price must be strictly between zero and one")
    return (
        exact_shares * FEE_RATE * exact_price * (Decimal(1) - exact_price)
    ).quantize(
        FEE_QUANTUM,
        rounding=ROUND_DOWN,
    )


def observable_ms(timestamp_s: int) -> int:
    if (
        isinstance(timestamp_s, bool)
        or not isinstance(timestamp_s, int)
        or timestamp_s < 0
    ):
        raise ValueError("timestamp_s must be a nonnegative integer second")
    return (timestamp_s + 1) * 1000


def fixed_limit(
    signal_price: object,
    wallet_score: object,
    shares: object = 5,
    *,
    signal_fee: object | None = None,
) -> Decimal | None:
    """Choose the highest cent limit preserving the frozen one-cent edge."""
    price = _exact_decimal(signal_price, "signal_price")
    score = _exact_decimal(wallet_score, "wallet_score")
    exact_shares = _exact_decimal(shares, "shares")
    if not Decimal(0) < price < Decimal(1):
        raise ValueError("signal_price must be strictly between zero and one")
    if exact_shares <= 0:
        raise ValueError("signal shares must be positive")
    exact_signal_fee = (
        charged_taker_fee(exact_shares, price)
        if signal_fee is None
        else _exact_decimal(signal_fee, "signal_fee")
    )
    if exact_signal_fee < 0 or exact_signal_fee > exact_shares * price:
        raise ValueError("signal_fee must be between zero and collateral paid")
    signal_fee_per_share = exact_signal_fee / exact_shares
    ceiling = price + signal_fee_per_share + score - Decimal("0.01")
    for cents in range(98, 0, -1):
        candidate = Decimal(cents) / Decimal(100)
        follower_fee_per_share = charged_taker_fee(MIN_SHARES, candidate) / MIN_SHARES
        if candidate + follower_fee_per_share <= ceiling:
            return candidate
    return None


def select_wallets(
    observations: Iterable[WalletObservation],
    asof_day: date,
) -> tuple[WalletScore, ...]:
    """Rank from 14 settlement-safe UTC days, excluding the immediately prior day."""
    if type(asof_day) is not date:
        raise ValueError("asof_day must be a UTC calendar date")
    first_day = asof_day - timedelta(days=15)
    final_day = asof_day - timedelta(days=1)
    first_by_market: dict[tuple[str, str], WalletObservation] = {}
    seen_keys: set[EventKey] = set()
    for observation in observations:
        if not isinstance(observation, WalletObservation):
            raise ValueError("observations must contain WalletObservation values")
        if not first_day <= observation.day < final_day:
            continue
        if observation.event_key is not None:
            if observation.event_key in seen_keys:
                raise ValueError(f"duplicate event key {observation.event_key}")
            seen_keys.add(observation.event_key)
        key = (observation.wallet, observation.market)
        previous = first_by_market.get(key)
        if previous is None:
            first_by_market[key] = observation
            continue
        if observation.timestamp_s == previous.timestamp_s:
            if (
                observation.block_number is None
                or observation.log_index is None
                or observation.event_key is None
                or previous.block_number is None
                or previous.log_index is None
                or previous.event_key is None
            ):
                raise ValueError("first BUY is ambiguous without complete chain order")
            observation_order = (
                observation.block_number,
                observation.log_index,
                observation.event_key,
            )
            previous_order = (
                previous.block_number,
                previous.log_index,
                previous.event_key,
            )
            if observation_order < previous_order:
                first_by_market[key] = observation
        elif observation.timestamp_s < previous.timestamp_s:
            first_by_market[key] = observation

    pnl_by_wallet: dict[str, list[Decimal]] = {}
    for observation in first_by_market.values():
        fee_per_share = observation.fee / observation.shares
        pnl = observation.settlement - observation.price - fee_per_share
        pnl_by_wallet.setdefault(observation.wallet, []).append(pnl)

    scores: list[WalletScore] = []
    for wallet, pnls in pnl_by_wallet.items():
        n = len(pnls)
        if n < 20:
            continue
        pnl_sum = sum(pnls, Decimal(0))
        score = pnl_sum / Decimal(n + 20)
        if score > Decimal("0.02"):
            scores.append(
                WalletScore(
                    asof_day=asof_day,
                    wallet=wallet,
                    score=score,
                    n=n,
                    pnl_sum=pnl_sum,
                )
            )
    scores.sort(key=lambda item: (-item.score, item.wallet))
    return tuple(scores[:5])


def first_market_signal(
    events: Iterable[ParentBuy],
    selected_scores: Iterable[WalletScore],
    market_start_s: int,
) -> ParentBuy | None:
    """Return the first causally observable selected-wallet BUY in the market window."""
    if (
        isinstance(market_start_s, bool)
        or not isinstance(market_start_s, int)
        or market_start_s < 0
    ):
        raise ValueError("market_start_s must be a nonnegative integer second")
    selected: dict[str, WalletScore] = {}
    for score in selected_scores:
        if not isinstance(score, WalletScore):
            raise ValueError("selected_scores must contain WalletScore values")
        if score.n < 20 or score.score <= Decimal("0.02"):
            raise ValueError(
                "selected wallet does not satisfy the frozen selection rule"
            )
        if score.wallet in selected:
            raise ValueError(f"duplicate selected wallet {score.wallet}")
        selected[score.wallet] = score
    if len(selected) > 5:
        raise ValueError("at most five wallets may be selected")

    earliest_ms = (market_start_s + 60) * 1000
    latest_ms = (market_start_s + 300 - 20) * 1000
    seen_keys: set[EventKey] = set()
    markets: set[str] = set()
    candidates: list[ParentBuy] = []
    for event in events:
        if not isinstance(event, ParentBuy):
            raise ValueError("events must contain ParentBuy values")
        if event.event_key in seen_keys:
            raise ValueError(f"duplicate event key {event.event_key}")
        seen_keys.add(event.event_key)
        if event.start_s != market_start_s:
            continue
        markets.add(event.market)
        selected_score = selected.get(event.wallet)
        if selected_score is None or selected_score.asof_day != event.day:
            continue
        observed = observable_ms(event.timestamp_s)
        if earliest_ms <= observed <= latest_ms:
            candidates.append(event)
    if len(markets) > 1:
        raise ValueError("market_start_s identifies multiple markets")
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda event: (
            observable_ms(event.timestamp_s),
            event.block_number,
            event.log_index,
            event.event_key,
        ),
    )


def opposite_signal(signal: ParentBuy) -> ParentBuy:
    """Construct the opposite-token control without deriving a synthetic book quote."""
    if not isinstance(signal, ParentBuy):
        raise ValueError("signal must be a ParentBuy")
    if signal.opposite_token is None:
        raise ValueError("signal has no complementary token")
    price = Decimal(1) - signal.price
    return replace(
        signal,
        token=signal.opposite_token,
        opposite_token=signal.token,
        side="Down" if signal.side == "Up" else "Up",
        price=price,
        fee=signal.fee,
    )


def replay_target_ms(signal: ParentBuy, horizon_ms: int, variant: str = "base") -> int:
    if not isinstance(signal, ParentBuy):
        raise ValueError("signal must be a ParentBuy")
    if isinstance(horizon_ms, bool) or horizon_ms not in (400, 500):
        raise ValueError("horizon_ms must be 400 or 500")
    if variant not in {"base", "delay_5s", "opposite"}:
        raise ValueError("variant must be base, delay_5s, or opposite")
    delay_ms = 5_000 if variant == "delay_5s" else 0
    return observable_ms(signal.timestamp_s) + delay_ms + horizon_ms


def replay(
    signal: ParentBuy,
    score: WalletScore | Decimal,
    snapshots: Iterable[BookSnapshot],
    horizon_ms: int,
    variant: str = "base",
) -> ReplayResult:
    """Replay one fixed-limit, five-share attempt against the first causal snapshot."""
    if not isinstance(signal, ParentBuy):
        raise ValueError("signal must be a ParentBuy")
    target_ms = replay_target_ms(signal, horizon_ms, variant)
    execution_signal = opposite_signal(signal) if variant == "opposite" else signal
    if isinstance(score, WalletScore):
        if score.wallet != signal.wallet or score.asof_day != signal.day:
            raise ValueError(
                "wallet score does not belong to the signal wallet and day"
            )
        if score.n < 20 or score.score <= Decimal("0.02"):
            raise ValueError("wallet score does not satisfy the frozen selection rule")
        score_value = score.score
    else:
        score_value = _exact_decimal(score, "score")
        if score_value <= Decimal("0.02"):
            raise ValueError("score must be greater than 0.02")

    limit_price = fixed_limit(
        execution_signal.price,
        score_value,
        execution_signal.shares,
        signal_fee=execution_signal.fee,
    )

    def result(
        reason: str,
        *,
        snapshot: BookSnapshot | None = None,
        ask_price: Decimal | None = None,
        ask_size: Decimal | None = None,
        filled: bool = False,
    ) -> ReplayResult:
        fill_price = ask_price if filled else None
        filled_shares = MIN_SHARES if filled else Decimal(0)
        fee = (
            charged_taker_fee(filled_shares, fill_price)
            if filled and fill_price
            else Decimal(0)
        )
        pnl = (
            filled_shares * (execution_signal.settlement - fill_price) - fee
            if filled and fill_price is not None
            else Decimal(0)
        )
        return ReplayResult(
            variant=variant,
            horizon_ms=horizon_ms,
            event_key=execution_signal.event_key,
            wallet=execution_signal.wallet,
            market=execution_signal.market,
            token=execution_signal.token,
            side=execution_signal.side,
            target_ms=target_ms,
            snapshot_ms=snapshot.timestamp_ms if snapshot else None,
            limit_price=limit_price,
            filled=filled,
            reason=reason,
            ask_price=ask_price,
            ask_size=ask_size,
            fill_price=fill_price,
            shares=filled_shares,
            fee=fee,
            settlement=execution_signal.settlement,
            pnl=pnl,
        )

    if limit_price is None:
        return result("no_limit")

    ordered: list[BookSnapshot] = []
    seen_timestamps: set[int] = set()
    for snapshot in snapshots:
        if not isinstance(snapshot, BookSnapshot):
            raise ValueError("snapshots must contain BookSnapshot values")
        if snapshot.market is not None and snapshot.market != execution_signal.market:
            raise ValueError("snapshot market does not match signal market")
        if snapshot.timestamp_ms in seen_timestamps:
            raise ValueError(f"duplicate snapshot timestamp {snapshot.timestamp_ms}")
        seen_timestamps.add(snapshot.timestamp_ms)
        ordered.append(snapshot)
    ordered.sort(key=lambda item: item.timestamp_ms)
    chosen_snapshot = next(
        (item for item in ordered if item.timestamp_ms >= target_ms), None
    )
    if chosen_snapshot is None:
        return result("no_snapshot")
    if chosen_snapshot.timestamp_ms > target_ms + 100:
        return result("snapshot_too_late", snapshot=chosen_snapshot)

    ask_price, ask_size = chosen_snapshot.direct_ask(execution_signal.side)
    if not chosen_snapshot.active:
        return result(
            "inactive_book",
            snapshot=chosen_snapshot,
            ask_price=ask_price,
            ask_size=ask_size,
        )
    if chosen_snapshot.halted:
        return result(
            "halted_book",
            snapshot=chosen_snapshot,
            ask_price=ask_price,
            ask_size=ask_size,
        )
    if not chosen_snapshot.fresh:
        return result(
            "stale_book",
            snapshot=chosen_snapshot,
            ask_price=ask_price,
            ask_size=ask_size,
        )
    if ask_price is None or ask_size is None:
        return result(
            "missing_direct_ask",
            snapshot=chosen_snapshot,
            ask_price=ask_price,
            ask_size=ask_size,
        )
    if ask_size < MIN_SHARES:
        return result(
            "insufficient_depth",
            snapshot=chosen_snapshot,
            ask_price=ask_price,
            ask_size=ask_size,
        )
    if ask_price > limit_price:
        return result(
            "ask_above_limit",
            snapshot=chosen_snapshot,
            ask_price=ask_price,
            ask_size=ask_size,
        )
    return result(
        "filled",
        snapshot=chosen_snapshot,
        ask_price=ask_price,
        ask_size=ask_size,
        filled=True,
    )


def normalise_parent_buy(
    row: Mapping[str, Any],
    tokens: Mapping[str, TokenMeta],
) -> ParentBuy:
    """Validate and normalise one V2 aggressor-parent BUY event."""
    try:
        contract = _text(row["contract"], "contract")
        taker = _text(row["taker"], "taker")
        token_id = _text(row["token_id"], "token_id")
    except KeyError as exc:
        raise ValueError(f"missing raw field {exc.args[0]}") from exc
    if contract != CTF_EXCHANGE_V2_LABEL:
        raise ValueError("row is not from CTF Exchange V2")
    if taker.lower() != CTF_EXCHANGE_V2_ADDRESS.lower():
        raise ValueError("row is not an aggressor-parent event")
    try:
        side = _unsigned_integer(row["side"], "side")
    except KeyError as exc:
        raise ValueError("missing raw field side") from exc
    if side != 0:
        raise ValueError("row is not a BUY")
    token = tokens.get(token_id)
    if token is None:
        raise ValueError(f"unknown token {token_id}")
    if token.token != token_id:
        raise ValueError("token mapping key and metadata disagree")

    try:
        collateral_raw = _unsigned_integer(
            row["maker_amount_filled"], "maker_amount_filled"
        )
        shares_raw = _unsigned_integer(
            row["taker_amount_filled"], "taker_amount_filled"
        )
        fee_raw = _unsigned_integer(row["fee"], "fee")
        timestamp_s = _unsigned_integer(row["timestamp"], "timestamp")
        block_number = _unsigned_integer(row["block_number"], "block_number")
        log_index = _unsigned_integer(row["log_index"], "log_index")
        transaction_hash = _text(row["transaction_hash"], "transaction_hash").lower()
        order_hash = _text(row["order_hash"], "order_hash").lower()
        wallet = _text(row["maker"], "maker").lower()
    except KeyError as exc:
        raise ValueError(f"missing raw field {exc.args[0]}") from exc

    shares = _human_units(shares_raw)
    collateral = _human_units(collateral_raw)
    fee = _human_units(fee_raw)
    if shares <= 0:
        raise ValueError("parent BUY shares must be positive")
    with localcontext() as context:
        context.prec = max(50, len(str(collateral_raw)) + len(str(shares_raw)) + 6)
        price = collateral / shares
    if not Decimal(0) < price < Decimal(1):
        raise ValueError("price must be strictly between zero and one")
    if fee != charged_taker_fee(shares, price):
        raise ValueError("raw fee does not match the five-decimal-truncated taker fee")

    opposite_side = "Down" if token.side == "Up" else "Up"
    complements = [
        item
        for item in tokens.values()
        if item.market == token.market
        and item.start_s == token.start_s
        and item.day == token.day
        and item.side == opposite_side
    ]
    if len(complements) > 1:
        raise ValueError("token mapping has multiple complementary tokens")
    if complements and complements[0].up_won != token.up_won:
        raise ValueError("complementary tokens disagree on market settlement")

    event_key = (
        CHAIN_ID,
        CTF_EXCHANGE_V2_ADDRESS.lower(),
        transaction_hash,
        log_index,
    )
    return ParentBuy(
        day=token.day,
        start_s=token.start_s,
        market=token.market,
        token=token.token,
        opposite_token=complements[0].token if complements else None,
        side=token.side,
        up_won=token.up_won,
        wallet=wallet,
        timestamp_s=timestamp_s,
        price=price,
        shares=shares,
        fee=fee,
        event_key=event_key,
        source_event_id=transaction_hash,
        transaction_hash=transaction_hash,
        order_hash=order_hash,
        block_number=block_number,
        log_index=log_index,
    )


def _daily_decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite decimal")
    try:
        result = Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be a finite decimal")
    return result


def _daily_amount(value: object, field: str) -> Decimal:
    result = _daily_decimal(value, field)
    quantized = result.quantize(SOURCE_AMOUNT_QUANTUM)
    if abs(result - quantized) > SOURCE_FLOAT_TOLERANCE:
        raise ValueError(f"{field} exceeds six-decimal token precision")
    return quantized


def normalise_daily_parent_buy(
    row: Mapping[str, Any],
    tokens: Mapping[str, TokenMeta],
) -> ParentBuy:
    """Validate one TimeSeventeen CTF Exchange V2 parent BUY row."""
    try:
        source_event_id = _text(row["id"], "id").lower()
        order_hash = _text(row["order_hash"], "order_hash").lower()
        wallet = _text(row["maker"], "maker").lower()
        taker = _text(row["taker"], "taker")
        maker_direction = _text(row["maker_direction"], "maker_direction")
        taker_direction = _text(row["taker_direction"], "taker_direction")
        token_id = _text(row["token_asset_id"], "token_asset_id")
        timestamp_s = _unsigned_integer(row["timestamp"], "timestamp")
    except KeyError as exc:
        raise ValueError(f"missing daily source field {exc.args[0]}") from exc
    match = re.fullmatch(r"([0-9]+)_([0-9]+)_([0-9]+)", source_event_id)
    if match is None or int(match.group(1)) != CHAIN_ID:
        raise ValueError("id must contain the Polygon chain, block, and log index")
    block_number = int(match.group(2))
    log_index = int(match.group(3))
    if taker.lower() != CTF_EXCHANGE_V2_ADDRESS.lower():
        raise ValueError("row is not an aggressor-parent event")
    if maker_direction != "BUY" or taker_direction != "SELL":
        raise ValueError("row is not a parent BUY")
    token = tokens.get(token_id)
    if token is None:
        raise ValueError(f"unknown token {token_id}")
    if token.token != token_id:
        raise ValueError("token mapping key and metadata disagree")

    try:
        shares = _daily_amount(row["token_amount"], "token_amount")
        collateral = _daily_amount(row["usdc_amount"], "usdc_amount")
        source_price = _daily_decimal(row["price"], "price")
        fee = _daily_amount(row["fee_usdc"], "fee_usdc")
    except KeyError as exc:
        raise ValueError(f"missing daily source field {exc.args[0]}") from exc
    if shares <= 0:
        raise ValueError("parent BUY shares must be positive")
    if collateral <= 0:
        raise ValueError("parent BUY collateral must be positive")
    with localcontext() as context:
        context.prec = 50
        price = collateral / shares
    if not Decimal(0) < source_price < Decimal(1):
        raise ValueError("source price must be strictly between zero and one")
    if not Decimal(0) < price < Decimal(1):
        raise ValueError("price must be strictly between zero and one")
    if abs(source_price - price) > SOURCE_PRICE_TOLERANCE:
        raise ValueError("source price disagrees with collateral divided by shares")
    if fee < 0 or fee > collateral:
        raise ValueError("daily source fee must be between zero and collateral paid")

    opposite_side = "Down" if token.side == "Up" else "Up"
    complements = [
        item
        for item in tokens.values()
        if item.market == token.market
        and item.start_s == token.start_s
        and item.day == token.day
        and item.side == opposite_side
    ]
    if len(complements) > 1:
        raise ValueError("token mapping has multiple complementary tokens")
    if complements and complements[0].up_won != token.up_won:
        raise ValueError("complementary tokens disagree on market settlement")

    event_key = (
        CHAIN_ID,
        CTF_EXCHANGE_V2_ADDRESS.lower(),
        source_event_id,
        log_index,
    )
    return ParentBuy(
        day=token.day,
        start_s=token.start_s,
        market=token.market,
        token=token.token,
        opposite_token=complements[0].token if complements else None,
        side=token.side,
        up_won=token.up_won,
        wallet=wallet,
        timestamp_s=timestamp_s,
        price=price,
        shares=shares,
        fee=fee,
        event_key=event_key,
        source_event_id=source_event_id,
        transaction_hash=None,
        order_hash=order_hash,
        block_number=block_number,
        log_index=log_index,
    )
