"""Fail-closed contracts for future BTC 5m source and TWAP captures.

This module is intentionally separate from the frozen October development
evaluator.  It validates newly recorded inputs before a later development
lane may build flow or settlement-tail features.  Receipt time is the causal
clock.  Missing aggressor direction stays missing; it is never inferred from
price, quote movement, or quantity sign.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math


BUY = "BUY"
SELL = "SELL"


def _finite_number(value, name):
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _integer_ns(value, name):
    result = _finite_number(value, name)
    if result < 0 or int(result) != result:
        raise ValueError(f"{name} must be a non-negative integer")
    return int(result)


def _side(value, name):
    if value is None:
        return None
    normalized = str(value).strip().upper()
    aliases = {"B": BUY, "BUY": BUY, "S": SELL, "SELL": SELL}
    if normalized not in aliases:
        raise ValueError(f"unsupported {name}: {value!r}")
    return aliases[normalized]


def explicit_aggressor_side(event):
    """Return ``(side, provenance)`` using only documented source fields.

    Conflicting explicit fields fail closed.  ``side`` is accepted only when
    the caller has already identified the record as an external trade, rather
    than a CLOB book update where that name has different semantics.
    """
    claims = []
    for field in ("aggressor_side", "side"):
        if field in event and event[field] is not None:
            claims.append((_side(event[field], field), field))
    for field in ("is_buyer_maker", "buyer_is_maker"):
        if field in event and event[field] is not None:
            value = event[field]
            if not isinstance(value, bool):
                raise ValueError(f"{field} must be boolean")
            claims.append((SELL if value else BUY, field))
    if not claims:
        return None, "missing"
    sides = {side for side, _field in claims}
    if len(sides) != 1:
        detail = ", ".join(f"{field}={side}" for side, field in claims)
        raise ValueError(f"conflicting aggressor fields: {detail}")
    return claims[0][0], "+".join(sorted(field for _side_value, field in claims))


@dataclass(frozen=True)
class ExternalTrade:
    source: str
    symbol: str
    source_event_ns: int
    local_recv_ns: int
    sequence: str
    price: float
    quantity: float
    aggressor_side: str | None
    side_provenance: str

    def row(self):
        return asdict(self)


def normalize_external_trade(event, *, max_source_ahead_ns=10_000_000_000):
    """Normalize one external trade while preserving both clocks."""
    recv = _integer_ns(event.get("local_recv_ns"), "local_recv_ns")
    source_event = _integer_ns(event.get("source_event_ns"), "source_event_ns")
    if source_event > recv + int(max_source_ahead_ns):
        raise ValueError("source_event_ns implausibly ahead of local receipt")
    price = _finite_number(event.get("price"), "price")
    quantity = _finite_number(event.get("quantity"), "quantity")
    if price <= 0 or quantity <= 0:
        raise ValueError("price and quantity must be positive")
    side, provenance = explicit_aggressor_side(event)
    source = str(event.get("source", "")).strip().lower()
    symbol = str(event.get("symbol", "")).strip().upper()
    sequence = str(event.get("sequence", "")).strip()
    if not source or not symbol or not sequence:
        raise ValueError("source, symbol, and sequence are required")
    return ExternalTrade(source, symbol, source_event, recv, sequence,
                         price, quantity, side, provenance)


def signed_volume_fraction(trades, *, decision_recv_ns, window_seconds):
    """Receipt-causal signed volume fraction, or ``None`` if any side is absent."""
    decision = _integer_ns(decision_recv_ns, "decision_recv_ns")
    if window_seconds not in (15, 60):
        raise ValueError("only frozen 15s and 60s windows are supported")
    lower = decision - window_seconds * 1_000_000_000
    rows = [row for row in trades if lower < row.local_recv_ns <= decision]
    if not rows or any(row.aggressor_side is None for row in rows):
        return None
    total = sum(row.quantity for row in rows)
    signed = sum(row.quantity if row.aggressor_side == BUY else -row.quantity
                 for row in rows)
    return signed / total if total > 0 else None


@dataclass(frozen=True)
class TwapSample:
    provider: str
    symbol: str
    window_seconds: int
    window_start_ns: int
    window_end_ns: int
    source_event_ns: int
    local_recv_ns: int
    sequence: str
    value: float
    provenance: str

    def row(self):
        return asdict(self)


def normalize_twap_sample(event, *, max_source_ahead_ns=10_000_000_000):
    """Accept only an explicit Chainlink BTC/USD 60-second TWAP sample."""
    provider = str(event.get("provider", "")).strip().lower()
    symbol = str(event.get("symbol", "")).replace("-", "/").strip().upper()
    window_seconds = int(event.get("window_seconds", 0))
    if provider not in {"chainlink", "chainlink_data_streams"}:
        raise ValueError("TWAP provider must be Chainlink Data Streams")
    if symbol not in {"BTC/USD", "BTCUSD"}:
        raise ValueError("TWAP symbol must be BTC/USD")
    if window_seconds != 60:
        raise ValueError("TWAP window_seconds must equal 60")
    start = _integer_ns(event.get("window_start_ns"), "window_start_ns")
    end = _integer_ns(event.get("window_end_ns"), "window_end_ns")
    if end <= start or end - start != 60_000_000_000:
        raise ValueError("TWAP boundaries must span exactly 60 seconds")
    recv = _integer_ns(event.get("local_recv_ns"), "local_recv_ns")
    source_event = _integer_ns(event.get("source_event_ns"), "source_event_ns")
    if source_event > recv + int(max_source_ahead_ns):
        raise ValueError("source_event_ns implausibly ahead of local receipt")
    value = _finite_number(event.get("value"), "value")
    if value <= 0:
        raise ValueError("TWAP value must be positive")
    sequence = str(event.get("sequence", "")).strip()
    provenance = str(event.get("provenance", "")).strip()
    if not sequence or not provenance:
        raise ValueError("TWAP sequence and provenance are required")
    return TwapSample(provider, "BTC/USD", window_seconds, start, end,
                      source_event, recv, sequence, value, provenance)


class ReceiptTape:
    """Append-only receipt-order tape; source timestamps never reorder rows."""

    def __init__(self):
        self.trades = []
        self.twap_samples = []
        self._last_recv_ns = -1

    def _append(self, row, target):
        if row.local_recv_ns < self._last_recv_ns:
            raise ValueError("local receipt clock moved backwards")
        self._last_recv_ns = row.local_recv_ns
        target.append(row)
        return row

    def add_trade(self, event):
        return self._append(normalize_external_trade(event), self.trades)

    def add_twap(self, event):
        return self._append(normalize_twap_sample(event), self.twap_samples)

    def flow(self, decision_recv_ns, window_seconds):
        return signed_volume_fraction(self.trades,
                                      decision_recv_ns=decision_recv_ns,
                                      window_seconds=window_seconds)

    def known_twap(self, decision_recv_ns):
        decision = _integer_ns(decision_recv_ns, "decision_recv_ns")
        rows = [row for row in self.twap_samples if row.local_recv_ns <= decision]
        return rows[-1] if rows else None
