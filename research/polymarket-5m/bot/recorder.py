"""Write live feeds in the outcometick layout, so `replay` and real_day.py read them.

gzip files stay open and are sync-flushed every couple of seconds; a crash
loses at most that much, and `replay` stops cleanly at a truncated tail.
"""
from __future__ import annotations

import gzip
import json
import time
from pathlib import Path

CLOB_KINDS = ("book", "best_bid_ask", "price_change", "last_trade_price")
CSV_HEADER = "feed_ts_ms,value,full_accuracy_value,server_ts_ms,recv_ms\n"


def _day(ms):
    return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))


class Recorder:
    def __init__(self, root, series="BTC-5m", symbol="BTCUSD", flush_s=2.0):
        self.root, self.series, self.symbol, self.flush_s = Path(root), series, symbol, flush_s
        self.files: dict[Path, gzip.GzipFile] = {}
        self.slug_by_asset: dict[str, str] = {}
        self.slug_by_condition: dict[str, str] = {}
        self.last_flush = time.monotonic()

    def track(self, market):
        self.slug_by_asset[market.up_token] = market.slug
        self.slug_by_asset[market.down_token] = market.slug
        if market.condition_id:
            self.slug_by_condition[market.condition_id] = market.slug

    def _open(self, path, header=None):
        f = self.files.get(path)
        if f is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            fresh = not path.exists()
            f = self.files[path] = gzip.open(path, "at", encoding="utf-8")
            if header and fresh:
                f.write(header)
        return f

    def clob(self, msg, recv_ms):
        kind = msg.get("event_type")
        if kind not in CLOB_KINDS:
            return
        slug = self.slug_by_asset.get(msg.get("asset_id")) or self.slug_by_condition.get(msg.get("market"))
        day = _day(recv_ms)
        path = self.root / f"polymarket/daily/{kind}/{self.series}/{self.series}-{kind}-{day}.jsonl.gz"
        ts = msg.get("timestamp")
        line = dict(slug=slug, asset_id=msg.get("asset_id"), event_type=kind,
                    event_ts_ms=int(ts) if ts not in (None, "") else None, recv_ms=recv_ms, payload=msg)
        self._open(path).write(json.dumps(line, separators=(",", ":")) + "\n")
        self._maybe_flush()

    def price(self, stream, feed_ts_ms, value, full, server_ts_ms, recv_ms):
        """stream: "spot" (instantaneous Chainlink) or "twap60"."""
        day = _day(feed_ts_ms)
        if stream == "spot":
            path = self.root / f"chainlink/daily/prices/{self.symbol}/{self.symbol}-prices-{day}.csv.gz"
        else:
            path = self.root / f"chainlink-twap-60s/daily/prices/{self.symbol}/{self.symbol}-twap60s-prices-{day}.csv.gz"
        self._open(path, CSV_HEADER).write(
            f"{feed_ts_ms},{value!r},{'' if full is None else full},{'' if server_ts_ms is None else server_ts_ms},{recv_ms}\n")
        self._maybe_flush()

    def market(self, m, strike_full=None):
        """One line per market once it has resolved (same fields as the sample)."""
        raw = m.raw
        tokens = raw.get("clobTokenIds")
        tokens = json.loads(tokens) if isinstance(tokens, str) else tokens
        prices = raw.get("outcomePrices")
        prices = json.loads(prices) if isinstance(prices, str) else prices
        path = self.root / f"polymarket/daily/markets/{self.series}/{self.series}-markets-{_day(m.start * 1000)}.jsonl.gz"
        line = dict(slug=m.slug, asset="btc", interval_sec=300, condition_id=m.condition_id, token_ids=tokens,
                    start_sec=m.start, end_sec=m.end, resolved=m.up_won is not None,
                    outcome_prices=prices, strike_value=None if strike_full is None else str(strike_full), raw=raw)
        self._open(path).write(json.dumps(line, separators=(",", ":")) + "\n")
        self.flush()

    def _maybe_flush(self):
        if time.monotonic() - self.last_flush >= self.flush_s:
            self.flush()

    def flush(self):
        today = _day(time.time() * 1000)
        yesterday = _day((time.time() - 86400) * 1000)
        for path, f in list(self.files.items()):
            f.flush()
            if today not in path.name and yesterday not in path.name:
                f.close()                        # rotate: days we no longer write to
                del self.files[path]
        self.last_flush = time.monotonic()

    def close(self):
        for f in self.files.values():
            f.close()
        self.files.clear()
