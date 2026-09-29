"""Live driver: discover markets, stream CLOB + Chainlink, record, paper/live trade.

Endpoints and wire formats follow Polymarket's official SDK
(github.com/Polymarket/py-sdk, src/polymarket/environments.py and
_internal/streams/{clob,rtds}): the market channel takes
{"type":"market","assets_ids":[...],"custom_feature_enabled":true} and a
"PING" every 10s; RTDS takes {"action":"subscribe","subscriptions":[...]}
and a "PING" every 5s.

This file cannot be exercised from a sandbox without network access; the
parsing and engine paths it uses are covered by tests, the sockets are not.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

from .events import Spot, Tick, Twap, market_from_gamma, parse_clob
from .paper import JsonlSink, PaperBroker
from .recorder import Recorder
from .strategy import BY_NAME, VARIANTS, Engine

GAMMA = "https://gamma-api.polymarket.com"
CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
RTDS_WS = "wss://ws-live-data.polymarket.com"
SERIES_PREFIX = "btc-updown-5m-"
SYMBOL = "btc/usd"
STALE_BOOK_S = 15       # no market-channel message for this long -> halt new orders
STALE_CHAINLINK_S = 10  # no Chainlink tick for this long -> halt model-based orders

log = logging.getLogger("bot")


def now_ms():
    return int(time.time() * 1000)


class Live:
    def __init__(self, args, mode):
        self.args, self.mode = args, mode
        self.rec = Recorder(args.data)
        self.sink = JsonlSink(args.log)
        broker = PaperBroker(self.sink, latency_ms=args.latency_ms)
        if mode == "live":
            from .live_exec import LiveBroker
            broker = LiveBroker(self.sink, args, latency_ms=args.latency_ms)
        self.broker = broker
        self.engine = Engine(broker, VARIANTS) if mode != "record" else None
        self.markets = {}           # slug -> Market (incl. ones waiting for resolution)
        self.twap_full = {}         # sec -> full-accuracy int (for strike_value)
        self.assets = set()
        self.clob_ws = None
        self.last_clob = 0.0
        self.last_cl = 0.0

    # ---------------------------------------------------------- markets
    async def discover(self, http):
        while True:
            t = int(time.time())
            for start in (t - t % 300 + k * 300 for k in range(0, 3)):
                slug = f"{SERIES_PREFIX}{start}"
                if slug in self.markets:
                    continue
                try:
                    r = await http.get(f"{GAMMA}/markets", params={"slug": slug}, timeout=10)
                    r.raise_for_status()
                    rows = r.json()
                    if not rows:
                        continue
                    m = market_from_gamma(rows[0])
                except Exception as e:  # noqa: BLE001
                    log.warning("gamma %s: %s", slug, e)
                    continue
                m.strike = self.engine.cl.twap.get(m.start) if self.engine else None  # TWAP60 at the open
                self.markets[slug] = m
                self.rec.track(m)
                if self.engine:
                    self.engine.add_market(m)
                await self.subscribe([m.up_token, m.down_token])
                log.info("tracking %s", slug)
            await asyncio.sleep(20)

    async def resolve(self, http):
        """Poll Gamma for outcomes of ended markets; record and settle them."""
        while True:
            await asyncio.sleep(15)
            t = time.time()
            for slug, m in list(self.markets.items()):
                if t < m.end + 5:
                    continue
                if m.up_won is None:
                    try:
                        r = await http.get(f"{GAMMA}/markets", params={"slug": slug}, timeout=10)
                        r.raise_for_status()
                        rows = r.json()
                        if rows:
                            fresh = market_from_gamma(rows[0])
                            m.raw, m.up_won = rows[0], fresh.up_won
                    except Exception as e:  # noqa: BLE001
                        log.warning("gamma resolve %s: %s", slug, e)
                if m.up_won is None and t > m.end + 1800:
                    log.warning("giving up on %s (unresolved after 30 min)", slug)
                if m.up_won is not None or t > m.end + 1800:
                    self.rec.market(m, self.twap_full.get(m.start))
                    await self.unsubscribe([m.up_token, m.down_token])
                    del self.markets[slug]

    # ------------------------------------------------------------- CLOB
    async def subscribe(self, tokens):
        new = [x for x in tokens if x not in self.assets]
        self.assets.update(new)
        if new and self.clob_ws is not None:
            await self.clob_ws.send(json.dumps({"operation": "subscribe", "assets_ids": new,
                                                "custom_feature_enabled": True}))

    async def unsubscribe(self, tokens):
        gone = [x for x in tokens if x in self.assets]
        self.assets.difference_update(gone)
        if gone and self.clob_ws is not None:
            await self.clob_ws.send(json.dumps({"operation": "unsubscribe", "assets_ids": gone}))

    async def clob(self):
        import websockets

        backoff = 1
        while True:
            while not self.assets:
                await asyncio.sleep(1)
            try:
                async with websockets.connect(CLOB_WS, ping_interval=None, max_size=None) as ws:
                    await ws.send(json.dumps({"type": "market", "assets_ids": sorted(self.assets),
                                              "custom_feature_enabled": True}))
                    self.clob_ws, backoff = ws, 1
                    pinger = asyncio.create_task(self._ping(ws, 10))
                    try:
                        async for raw in ws:
                            self.last_clob = time.time()
                            if raw == "PONG":
                                continue
                            self.on_clob(raw, now_ms())
                    finally:
                        pinger.cancel()
            except Exception as e:  # noqa: BLE001
                log.warning("clob socket: %s", e)
            self.clob_ws = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    def on_clob(self, raw, recv):
        try:
            data = json.loads(raw)
        except ValueError:
            return
        for msg in data if isinstance(data, list) else [data]:
            if not isinstance(msg, dict):
                continue
            self.rec.clob(msg, recv)
            if self.engine:
                for ev in parse_clob(msg, recv):
                    self.engine.on_event(ev)

    # ------------------------------------------------------------- RTDS
    async def rtds(self):
        import websockets

        # Same frame the official SDK sends: no server-side filter, symbols are filtered here.
        sub = {"action": "subscribe", "subscriptions": [
            {"topic": "crypto_prices_chainlink", "type": "update"},
            {"topic": "crypto_prices_twap_sixty", "type": "update"},
        ]}
        backoff = 1
        while True:
            try:
                async with websockets.connect(RTDS_WS, ping_interval=None, max_size=None) as ws:
                    await ws.send(json.dumps(sub))
                    backoff = 1
                    pinger = asyncio.create_task(self._ping(ws, 5))
                    try:
                        async for raw in ws:
                            if isinstance(raw, str) and "payload" in raw:
                                self.on_rtds(raw, now_ms())
                    finally:
                        pinger.cancel()
            except Exception as e:  # noqa: BLE001
                log.warning("rtds socket: %s", e)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    def on_rtds(self, raw, recv):
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        topic, p = msg.get("topic"), msg.get("payload") or {}
        if str(p.get("symbol", "")).lower().replace("/", "") != SYMBOL.replace("/", ""):
            return
        server_ts = msg.get("timestamp")
        points = p.get("data") if isinstance(p.get("data"), list) else [p]   # initial dump or update
        for d in points:
            ts, value = d.get("timestamp"), d.get("value")
            if ts is None or value is None:
                continue
            ts, value = int(ts), float(value)
            full = d.get("full_accuracy_value")
            full = int(full) if isinstance(full, str) and full.lstrip("-").isdigit() else None
            if topic == "crypto_prices_chainlink":
                self.last_cl = time.time()
                self.rec.price("spot", ts, value, full, server_ts, recv)
                if self.engine:
                    self.engine.on_event(Spot(recv, ts // 1000, value))
            elif topic == "crypto_prices_twap_sixty":
                sec = ts // 1000
                if full is not None:
                    self.twap_full[sec] = full
                    if len(self.twap_full) > 4000:
                        for s in [s for s in self.twap_full if s < sec - 3600]:
                            del self.twap_full[s]
                self.rec.price("twap60", ts, value, full, server_ts, recv)
                if self.engine:
                    self.engine.on_event(Twap(recv, sec, value, full))

    # ------------------------------------------------------------ misc
    @staticmethod
    async def _ping(ws, every):
        while True:
            await asyncio.sleep(every)
            await ws.send("PING")

    async def ticker(self):
        while True:
            await asyncio.sleep(0.25)
            if not self.engine:
                self.rec.flush()
                continue
            t = time.time()
            healthy = t - self.last_clob < STALE_BOOK_S
            self.engine.halted = not healthy
            self.engine.model_ok = t - self.last_cl < STALE_CHAINLINK_S
            self.engine.on_event(Tick(now_ms()))

    async def run(self):
        import httpx

        async with httpx.AsyncClient(headers={"User-Agent": "pm5m-forward-test"}) as http:
            tasks = [self.discover(http), self.resolve(http), self.clob(), self.rtds(), self.ticker()]
            if self.mode == "live":
                tasks.append(self.broker.guard())
            await asyncio.gather(*tasks)


async def main(args, mode):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    Path(args.log).parent.mkdir(parents=True, exist_ok=True)
    if mode == "live":
        bad = [v for v in args.variant if v not in BY_NAME or BY_NAME[v].mode != "taker"]
        if bad:
            raise SystemExit(f"live trading supports taker variants only; refusing {bad}")
    live = Live(args, mode)
    if mode == "live":
        await live.broker.start()
    try:
        await live.run()
    finally:
        live.rec.close()
        live.sink.close()
