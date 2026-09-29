"""Normalised events shared by the replay and live drivers.

Raw CLOB market-channel messages are what the outcometick sample stores as
`payload`, and what `record` stores too, so one parser serves both.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Quote:          # best_bid_ask
    recv_ms: int
    token: str
    bid: float | None
    ask: float | None


@dataclass(slots=True)
class Book:           # full-depth snapshot
    recv_ms: int
    token: str
    bids: list
    asks: list


@dataclass(slots=True)
class Level:          # one price_change entry: new total size at a price
    recv_ms: int
    token: str
    side: str
    price: float
    size: float
    bid: float | None
    ask: float | None


@dataclass(slots=True)
class Trade:          # last_trade_price; side is the taker's side
    recv_ms: int
    token: str
    side: str
    price: float
    size: float


@dataclass(slots=True)
class Spot:           # Chainlink instantaneous price for one feed second
    recv_ms: int
    sec: int
    value: float


@dataclass(slots=True)
class Twap:           # Chainlink 60s TWAP for one feed second (settles 5m markets)
    recv_ms: int
    sec: int
    value: float
    full: int | None = None


@dataclass(slots=True)
class Market:
    slug: str
    start: int
    end: int
    up_token: str
    down_token: str
    condition_id: str = ""
    strike: float | None = None       # TWAP60 at the open
    up_won: bool | None = None        # None until resolved
    fee_rate: float = 0.07
    min_size: float = 5.0
    raw: dict = field(default_factory=dict)

    def token_side(self, token):
        return "Up" if token == self.up_token else "Down"


@dataclass(slots=True)
class Tick:           # a clock tick so decisions happen even on quiet books
    recv_ms: int


def _f(x):
    return float(x) if x not in (None, "") else None


def _ladder(levels):
    return [(float(l["price"]), float(l["size"])) for l in levels or ()]


def parse_clob(msg, recv_ms):
    """One market-channel message (dict) -> list of events."""
    kind = msg.get("event_type")
    if kind == "best_bid_ask":
        return [Quote(recv_ms, msg["asset_id"], _f(msg.get("best_bid")), _f(msg.get("best_ask")))]
    if kind == "book":
        return [Book(recv_ms, msg["asset_id"], _ladder(msg.get("bids")), _ladder(msg.get("asks")))]
    if kind == "price_change":
        return [Level(recv_ms, c["asset_id"], c["side"], float(c["price"]), float(c["size"]),
                      _f(c.get("best_bid")), _f(c.get("best_ask")))
                for c in msg.get("price_changes", ())]
    if kind == "last_trade_price":
        return [Trade(recv_ms, msg["asset_id"], msg["side"], float(msg["price"]), float(msg["size"]))]
    return []


FIXED_POINT = 10**18


def market_from_gamma(m, strike=None):
    """A Gamma /markets row (or the sample's `raw`) -> Market."""
    import json

    outcomes = m["outcomes"] if isinstance(m["outcomes"], list) else json.loads(m["outcomes"])
    tokens = m["clobTokenIds"] if isinstance(m["clobTokenIds"], list) else json.loads(m["clobTokenIds"])
    up = outcomes.index("Up")
    slug = m["slug"]
    start = int(slug.rsplit("-", 1)[1])
    prices = m.get("outcomePrices")
    prices = json.loads(prices) if isinstance(prices, str) else prices
    up_won = None
    if m.get("closed") and prices and sorted(float(p) for p in prices) == [0.0, 1.0]:
        up_won = float(prices[up]) == 1.0
    fee = (m.get("feeSchedule") or {}).get("rate", 0.07)
    return Market(slug=slug, start=start, end=start + 300, up_token=tokens[up], down_token=tokens[1 - up],
                  condition_id=m.get("conditionId", ""), strike=strike, up_won=up_won, fee_rate=float(fee),
                  min_size=float(m.get("orderMinSize") or 5), raw=m)
