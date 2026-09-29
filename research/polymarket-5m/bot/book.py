"""L2 order book for one outcome token.

Polymarket mirrors every order into both outcome books (a Down ask at 0.03 is
an Up bid at 0.97), so each token's own book is complete; no cross-book
merging is needed.
"""
from __future__ import annotations

EPS = 1e-9


class Ladder:
    __slots__ = ("bids", "asks", "updated_ms")

    def __init__(self):
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.updated_ms = 0

    def snapshot(self, bids, asks, recv_ms):
        self.bids = {p: s for p, s in bids if s > 0}
        self.asks = {p: s for p, s in asks if s > 0}
        self.updated_ms = recv_ms

    def level(self, side, price, size, recv_ms):
        """price_change semantics: `size` is the new total resting at `price`."""
        book = self.bids if side == "BUY" else self.asks
        if size > 0:
            book[price] = size
        else:
            book.pop(price, None)
        self.updated_ms = recv_ms

    def trim(self, best_bid, best_ask, recv_ms):
        """Drop levels a newer top-of-book quote proves are gone.

        best_bid_ask is unthrottled while full snapshots are not; anything
        better than the quoted top has been taken or cancelled.
        """
        if best_ask is not None:
            for p in [p for p in self.asks if p < best_ask - EPS]:
                del self.asks[p]
        if best_bid is not None:
            for p in [p for p in self.bids if p > best_bid + EPS]:
                del self.bids[p]
        self.updated_ms = max(self.updated_ms, recv_ms)

    def best_bid(self):
        return max(self.bids) if self.bids else None

    def best_ask(self):
        return min(self.asks) if self.asks else None

    def size_at(self, side, price):
        book = self.bids if side == "BUY" else self.asks
        return next((s for p, s in book.items() if abs(p - price) < EPS), 0.0)

    def take_asks(self, limit, shares):
        """Walk the asks up to `limit` for at most `shares`. Returns [(price, size)]."""
        fills, left = [], shares
        for p in sorted(self.asks):
            if p > limit + EPS or left <= EPS:
                break
            q = min(left, self.asks[p])
            fills.append((p, q))
            left -= q
        return fills
