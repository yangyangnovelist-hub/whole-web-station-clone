"""Paper-trade Polymarket BTC 5-minute Up/Down markets. Never places orders, needs no keys.

    pip install -r requirements.txt
    python paper_trader.py live --out paper_data            # leave running for days
    python paper_trader.py report paper_data                 # P&L so far
    python paper_trader.py replay polymarket-data-samples    # same rule on a recorded day

Rule under test (README §10): with `tau` seconds left, buy the favourite when
its best ask is within [lo, hi], taking at most the size shown at that ask
(capped at --max-shares), pay the taker fee, hold to settlement. Several tau
values run side by side as independent paper books.

Live mode also writes every raw message it receives (order book, Chainlink
prices, market metadata and every market's outcome) to disk, so the recording
doubles as a dataset: recording.py turns it into a bundle strategy_zoo.py reads.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import gzip
import json
import math
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

import binary as bo

GAMMA = "https://gamma-api.polymarket.com/markets?slug={slug}"
# /markets leaves closed markets out unless asked, so resolution lookups ask twice.
GAMMA_CLOSED = GAMMA + "&closed=true"
CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
RTDS_WS = "wss://ws-live-data.polymarket.com"
# Chainlink (settlement) and, on a connection of its own so a rejected subscription cannot cost the
# Chainlink stream, the Binance prices Polymarket relays (for latency.py's stale-quote test).
RTDS_SUBS = {"rtds": [{"topic": "crypto_prices_chainlink", "type": "*", "filters": ""}],
             "rtds-binance": [{"topic": "crypto_prices", "type": "update", "filters": ""}]}
COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"  # public trade prints, exchange-stamped
SLUG = "{coin}-updown-5m-{start}"
COINS = ("btc", "eth", "sol", "xrp", "doge")  # the coins with 5m Up/Down series
NAN = float("nan")


@dataclass
class Params:
    taus: tuple = (60, 30)
    lo: float = 0.80
    hi: float = 0.97
    max_shares: float = 100.0
    fee_rate: float = bo.CRYPTO_FEE_RATE


# ------------------------------------------------------------------ order book

@dataclass
class Ladder:
    """Price levels for one token, kept from `book` and `price_change` events."""
    bids: dict = field(default_factory=dict)
    asks: dict = field(default_factory=dict)
    recv_ms: int = 0

    @property
    def bid(self):
        return max(self.bids) if self.bids else NAN

    @property
    def ask(self):
        return min(self.asks) if self.asks else NAN

    def size_at(self, side, price):
        return (self.asks if side == "ask" else self.bids).get(price, NAN)


def fetch_market(fetch, slug):
    """Latest Gamma record for one slug, closed or open, or None."""
    for url in (GAMMA_CLOSED, GAMMA):
        found = fetch(url.format(slug=slug))
        if found:
            return found[0]
    return None


def _levels(rows):
    out = {}
    for r in rows or []:
        size = float(r["size"])
        if size > 0:
            out[round(float(r["price"]), 4)] = size
    return out


def apply_clob_message(ladders, msg, recv_ms):
    """Update ladders from one market-channel message (a dict or a list of dicts)."""
    if isinstance(msg, list):
        for m in msg:
            apply_clob_message(ladders, m, recv_ms)
        return
    et = msg.get("event_type")
    if et == "book":  # the market channel has used both bids/asks and buys/sells
        lad = ladders.setdefault(msg["asset_id"], Ladder())
        lad.bids = _levels(msg.get("bids") if "bids" in msg else msg.get("buys"))
        lad.asks = _levels(msg.get("asks") if "asks" in msg else msg.get("sells"))
        lad.recv_ms = recv_ms
    elif et == "price_change":
        for pc in msg.get("price_changes", []):
            lad = ladders.setdefault(pc["asset_id"], Ladder())
            book = lad.bids if pc.get("side") == "BUY" else lad.asks
            price, size = round(float(pc["price"]), 4), float(pc["size"])
            if size > 0:
                book[price] = size
            else:
                book.pop(price, None)
            lad.recv_ms = recv_ms


# -------------------------------------------------------------------- decision

def decide(up, down, tau, params):
    """Paper order for the favourite, or a dict with `skip` explaining why not.

    `up` and `down` are (bid, ask, ask_size, bid_size) for each token. The
    favourite can be bought on its own book or, equivalently, by hitting the
    other token's bid, so the effective ask is the better of the two.
    """
    bu, au, au_size, bu_size = up
    bd, ad, ad_size, bd_size = down
    bid_up = np.fmax(bu, 1 - ad)
    ask_up = np.fmin(au, 1 - bd)
    if not (np.isfinite(bid_up) and np.isfinite(ask_up)):
        return {"skip": "no book"}
    mid = (bid_up + ask_up) / 2
    fav_up = mid >= 0.5
    if fav_up:
        own, own_size, other, other_size = au, au_size, 1 - bd, bd_size
    else:
        own, own_size, other, other_size = ad, ad_size, 1 - bu, bu_size
    ask, size = (own, own_size) if not (other < own) else (other, other_size)
    ask = round(float(ask), 4)
    base = {"tau": tau, "side": "Up" if fav_up else "Down", "mid_up": round(float(mid), 4), "ask": ask}
    if not (params.lo - 1e-9 <= ask <= params.hi + 1e-9):
        return {**base, "skip": "ask outside band"}
    shares = min(params.max_shares, size) if np.isfinite(size) else params.max_shares
    if shares <= 0:
        return {**base, "skip": "no size"}
    fee = float(bo.taker_fee(ask, params.fee_rate)) * shares
    return {**base, "shares": float(shares), "cost": ask * shares + fee, "fee": fee}


def settle(order, up_won):
    won = (order["side"] == "Up") == bool(up_won)
    return {**order, "won": won, "pnl": (order["shares"] if won else 0.0) - order["cost"]}


def summarize(ledger):
    """Markdown table of settled paper trades per tau."""
    if ledger.empty:
        return "还没有结算的纸面交易。"
    lines = ["| 剩余 | 笔数 | 胜率 | 平均买价 | 总份数 | 总成本 | 总盈亏 | 每份盈亏 | ROI | t 值 | 最大回撤 |",
             "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for tau, g in ledger.groupby("tau"):
        per_share = g["pnl"] / g["shares"]
        sd = per_share.std(ddof=1) if len(g) > 1 else float("nan")
        t = per_share.mean() / (sd / math.sqrt(len(g))) if sd and sd > 0 else float("nan")
        curve = g.sort_values("end")["pnl"].cumsum()
        dd = float((curve.cummax() - curve).max()) if len(curve) else 0.0
        lines.append(f"| {tau}s | {len(g)} | {g['won'].mean()*100:.1f}% | {g['ask'].mean():.3f} | "
                     f"{g['shares'].sum():,.0f} | {g['cost'].sum():,.2f} | {g['pnl'].sum():+,.2f} | "
                     f"{per_share.mean()*100:+.2f}¢ | {g['pnl'].sum()/g['cost'].sum()*100:+.2f}% | {t:+.1f} | {dd:,.2f} |")
    return "\n".join(lines)


# ---------------------------------------------------------------------- replay

def replay(root, params):
    """Run the rule on a recorded outcometick day, using what was received by each time."""
    import real_day as rd
    markets = rd.load_markets(root)
    markets = markets[markets["resolved"]]
    tokens = set(markets["up_token"]) | set(markets["down_token"])
    quotes = rd.load_quotes(root, tokens)
    books = rd.load_book_asks(root, tokens)

    def ask_size(token, t_ms, price):
        snaps = books.get(token, [])
        i = np.searchsorted([s[0] for s in snaps], t_ms, side="right") - 1
        if i < 0:
            return NAN
        return sum(z for p, z in snaps[i][1] if abs(p - price) < 1e-9) or NAN

    rows = []
    for mk in markets.itertuples():
        for tau in params.taus:
            t_ms = (mk.end - tau) * 1000
            bu, au = (x[0] for x in rd.quote_asof(quotes, mk.up_token, [t_ms]))
            bd, ad = (x[0] for x in rd.quote_asof(quotes, mk.down_token, [t_ms]))
            up = (bu, au, ask_size(mk.up_token, t_ms, au), NAN)
            down = (bd, ad, ask_size(mk.down_token, t_ms, ad), NAN)
            order = decide(up, down, tau, params)
            if "skip" not in order:
                rows.append({"slug": mk.slug, "end": mk.end, **settle(order, mk.up_won)})
    return pd.DataFrame(rows)


# ------------------------------------------------------------------------ live

def now_ms():
    return int(time.time() * 1000)


def fetch_json(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "paper-trader/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _prefetched(fetched, slug, fetch):
    """The Gamma record of `slug` from a prefetched map (re-raising a failed lookup), else looked up."""
    if fetched is not None and slug in fetched:
        m = fetched[slug]
        if isinstance(m, Exception):
            raise m
        return m
    return fetch_market(fetch, slug)


def parse_gamma_market(m):
    """Tokens, window and (if resolved) outcome from one Gamma market object."""
    outcomes = json.loads(m["outcomes"]) if isinstance(m.get("outcomes"), str) else m.get("outcomes") or []
    tokens = json.loads(m["clobTokenIds"]) if isinstance(m.get("clobTokenIds"), str) else m.get("clobTokenIds") or []
    prices = json.loads(m["outcomePrices"]) if isinstance(m.get("outcomePrices"), str) else m.get("outcomePrices") or []
    if len(outcomes) != 2 or len(tokens) != 2 or "Up" not in outcomes:
        raise ValueError(f"unexpected market shape: {m.get('slug')}")
    up_i = outcomes.index("Up")
    start = int(m["slug"].rsplit("-", 1)[1])
    up_won = None
    if m.get("closed") and prices and max(float(p) for p in prices) == 1.0:
        up_won = float(prices[up_i]) == 1.0
    return {"slug": m["slug"], "start": start, "end": start + bo.WINDOW_S,
            "up_token": tokens[up_i], "down_token": tokens[1 - up_i], "up_won": up_won,
            "fee_rate": (m.get("feeSchedule") or {}).get("rate")}


class Recorder:
    """Append-only gzipped JSONL files, one directory per UTC day.

    Five coins of order-book traffic run to gigabytes a day, so files stay
    open and are flushed (a sync point readable after a crash) at most once
    a second rather than per line. Call close() before reading them back.
    """

    def __init__(self, out, flush_every=1.0):
        self.out, self.flush_every = Path(out), flush_every
        self.files, self.last_flush = {}, 0.0

    def write(self, name, obj):
        day = time.strftime("%Y-%m-%d", time.gmtime())
        f = self.files.get((day, name))
        if f is None:
            for key in [k for k in self.files if k[1] == name]:  # a new day closes yesterday's file
                self.files.pop(key).close()
            path = self.out / "raw" / day / f"{name}.jsonl.gz"
            path.parent.mkdir(parents=True, exist_ok=True)
            f = self.files[(day, name)] = gzip.open(path, "at", compresslevel=3, encoding="utf-8")
        f.write(json.dumps(obj, separators=(",", ":")) + "\n")
        now = time.monotonic()
        if now - self.last_flush >= self.flush_every:
            self.flush()
            self.last_flush = now

    def flush(self):
        for f in self.files.values():
            f.flush()

    def close(self):
        for f in self.files.values():
            f.close()
        self.files.clear()


class LiveTrader:
    """Records every coin in `coins`; paper-trades only the markets of `trade_coins`."""

    def __init__(self, out, params, fetch=fetch_json, coins=("btc",), trade_coins=("btc",)):
        self.out, self.params, self.fetch = Path(out), params, fetch
        self.coins, self.trade_coins = tuple(coins), tuple(trade_coins)
        self.token_coin = {}       # token -> coin, to file CLOB traffic per coin
        self.rec = Recorder(out)
        self.ladders = {}
        self.markets = {}          # slug -> parsed market
        self.pending = []          # paper orders waiting for settlement
        self.decided = set()       # (slug, tau)
        self.last_btc = None
        self.subscribed = set()
        self.ws = None

    # -- state updates (pure, unit-tested) --
    def on_clob(self, text, recv_ms):
        if text in ("PONG", "pong"):
            return
        msg = json.loads(text)
        by_coin = {}
        for ev in msg if isinstance(msg, list) else [msg]:
            if not isinstance(ev, dict) or ev.get("event_type") == "new_market":
                continue  # new_market announces every market on the venue; none of ours
            token = ev.get("asset_id") or next((pc.get("asset_id") for pc in ev.get("price_changes") or []), None)
            if token is None:
                token = next(iter(ev.get("assets_ids") or []), None)
            by_coin.setdefault(self.token_coin.get(token, "other"), []).append(ev)
        for coin, events in by_coin.items():
            if coin != "other":
                self.rec.write(f"clob-{coin}", {"recv_ms": recv_ms, "msg": events})
        apply_clob_message(self.ladders, msg, recv_ms)

    def on_rtds(self, text, recv_ms, stream="rtds"):
        if not text or text in ("pong", "PONG"):
            return
        msg = json.loads(text)
        self.rec.write(stream, {"recv_ms": recv_ms, "msg": msg})
        payload = msg.get("payload") or {}
        if msg.get("topic") == "crypto_prices_chainlink" and "btc" in str(payload.get("symbol", "")).lower() \
                and "value" in payload:
            self.last_btc = (payload.get("timestamp"), float(payload["value"]))

    def on_coinbase(self, text, recv_ms):
        """Coinbase trade prints (a dense spot feed reachable from US runners), kept compact."""
        msg = json.loads(text)
        if msg.get("type") in ("match", "last_match"):
            self.rec.write("coinbase", {"recv_ms": recv_ms, "p": msg.get("product_id"), "t": msg.get("time"),
                                        "px": msg.get("price"), "sz": msg.get("size"), "side": msg.get("side")})

    def top(self, token):
        lad = self.ladders.get(token, Ladder())
        return (lad.bid, lad.ask, lad.size_at("ask", lad.ask), lad.size_at("bid", lad.bid))

    def evaluate(self, mk, tau):
        order = decide(self.top(mk["up_token"]), self.top(mk["down_token"]), tau, self.params)
        record = {"slug": mk["slug"], "end": mk["end"], "decided_ms": now_ms(), "btc": self.last_btc, **order}
        self.rec.write("decisions", record)
        if "skip" not in order:
            self.pending.append(record)
        return record

    def due_orders(self, now=None):
        now = time.time() if now is None else now
        return [o["slug"] for o in self.pending if now >= max(o["end"] + 30, o.get("retry_at", 0))]

    def resolve_pending(self, fetched=None):
        """Settle paper orders whose market Gamma reports resolved. `fetched` maps slugs to Gamma
        records (or the exception the lookup raised) fetched off the event loop; other slugs are
        looked up here."""
        still, now = [], time.time()
        for order in self.pending:
            if now < max(order["end"] + 30, order.get("retry_at", 0)) or (fetched is not None and
                                                                          order["slug"] not in fetched):
                still.append(order)
                continue
            order["retry_at"] = now + 20
            try:
                m = _prefetched(fetched, order["slug"], self.fetch)
                up_won = parse_gamma_market(m)["up_won"] if m else None
            except Exception as e:  # network hiccup: try again later
                self.rec.write("errors", {"at": now_ms(), "where": "resolve", "err": repr(e)})
                still.append(order)
                continue
            if up_won is None:
                still.append(order)
                continue
            done = settle(order, up_won)
            self.rec.write("markets", m)
            self._append_ledger(done)
        self.pending = still

    def due_outcomes(self, now=None):
        now = time.time() if now is None else now
        return [slug for slug, mk in self.markets.items() if now >= mk["end"] + 60 and now >= mk.get("retry_at", 0)]

    def record_outcomes(self, fetched=None):
        """Save the resolved Gamma record of every market seen, traded or not, so the
        recording is a complete dataset (recording.py), then forget the market and leave its
        tokens out of later (re)subscriptions. `fetched` as in resolve_pending."""
        now = time.time()
        for slug, mk in list(self.markets.items()):
            if now < mk["end"] + 60 or now < mk.get("retry_at", 0) or (fetched is not None and slug not in fetched):
                continue
            try:
                m = _prefetched(fetched, slug, self.fetch)
                resolved = m is not None and parse_gamma_market(m)["up_won"] is not None
            except Exception as e:
                self.rec.write("errors", {"at": now_ms(), "where": "outcome", "slug": slug, "err": repr(e)})
                resolved = False
            if resolved:
                self.rec.write("markets", m)
            if resolved or now > mk["end"] + 3600:
                del self.markets[slug]
                self.subscribed.difference_update((mk["up_token"], mk["down_token"]))
            else:
                mk["retry_at"] = now + 30

    def _append_ledger(self, row):
        path = self.out / "ledger.csv"
        cols = ["slug", "end", "tau", "side", "ask", "shares", "fee", "cost", "won", "pnl", "mid_up", "decided_ms"]
        new = not path.exists()
        with path.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow(row)

    # -- network loops --
    async def _fetch_markets(self, slugs):
        """Gamma records of several slugs, looked up in worker threads so the websocket readers
        keep draining their sockets (a blocked event loop gets the CLOB feed closed as a slow
        consumer); a failed lookup maps to its exception."""
        async def one(slug):
            try:
                return slug, await asyncio.to_thread(fetch_market, self.fetch, slug)
            except Exception as e:
                return slug, e
        return dict(await asyncio.gather(*(one(s) for s in slugs))) if slugs else {}

    async def discover(self):
        while True:
            start = int(time.time()) // bo.WINDOW_S * bo.WINDOW_S
            for coin in self.coins:
                for s in (start, start + bo.WINDOW_S):
                    slug = SLUG.format(coin=coin, start=s)
                    if slug in self.markets:
                        continue
                    try:
                        found = await asyncio.to_thread(self.fetch, GAMMA.format(slug=slug))
                        if found:
                            await self.add_market(found[0], coin)
                    except Exception as e:
                        self.rec.write("errors", {"at": now_ms(), "where": "discover", "slug": slug,
                                                  "err": repr(e)})
            self.record_outcomes(await self._fetch_markets(self.due_outcomes()))
            await asyncio.sleep(10)

    async def add_market(self, gamma_market, coin):
        mk = parse_gamma_market(gamma_market)
        self.markets[mk["slug"]] = mk
        self.token_coin[mk["up_token"]] = self.token_coin[mk["down_token"]] = coin
        self.rec.write("markets", gamma_market)
        await self.subscribe(mk)

    async def subscribe(self, mk):
        tokens = [mk["up_token"], mk["down_token"]]
        self.subscribed.update(tokens)
        if self.ws is not None:
            await self.ws.send(json.dumps({"assets_ids": tokens, "operation": "subscribe",
                                           "custom_feature_enabled": True}))

    async def clob_loop(self):
        import websockets
        while True:
            try:
                async with websockets.connect(CLOB_WS, ping_interval=None, max_size=None) as ws:
                    self.ws = ws
                    await ws.send(json.dumps({"assets_ids": sorted(self.subscribed), "type": "market",
                                              "custom_feature_enabled": True}))
                    self.rec.write("errors", {"at": now_ms(), "where": "clob-open", "tokens": len(self.subscribed)})
                    pinger = asyncio.create_task(self._ping(ws, "PING", 10))
                    try:
                        async for text in ws:
                            self.on_clob(text, now_ms())
                    finally:
                        pinger.cancel()
            except Exception as e:
                self.rec.write("errors", {"at": now_ms(), "where": "clob", "err": repr(e)})
            self.ws = None
            await asyncio.sleep(2)

    async def rtds_loop(self, stream="rtds"):
        import websockets
        sub = {"action": "subscribe", "subscriptions": RTDS_SUBS[stream]}
        while True:
            try:
                async with websockets.connect(RTDS_WS, ping_interval=None, max_size=None) as ws:
                    await ws.send(json.dumps(sub))
                    pinger = asyncio.create_task(self._ping(ws, "ping", 5))
                    try:
                        async for text in ws:
                            self.on_rtds(text, now_ms(), stream)
                    finally:
                        pinger.cancel()
            except Exception as e:
                self.rec.write("errors", {"at": now_ms(), "where": stream, "err": repr(e)})
            await asyncio.sleep(2)

    async def coinbase_loop(self):
        import websockets
        sub = {"type": "subscribe", "channels": ["matches"],
               "product_ids": [f"{c.upper()}-USD" for c in self.coins]}
        while True:
            try:
                async with websockets.connect(COINBASE_WS, ping_interval=20, max_size=None) as ws:
                    await ws.send(json.dumps(sub))
                    async for text in ws:
                        self.on_coinbase(text, now_ms())
            except Exception as e:
                self.rec.write("errors", {"at": now_ms(), "where": "coinbase", "err": repr(e)})
            await asyncio.sleep(2)

    @staticmethod
    async def _ping(ws, word, every):
        while True:
            await asyncio.sleep(every)
            await ws.send(word)

    async def scheduler(self):
        while True:
            now = time.time()
            for mk in list(self.markets.values()):
                if not mk["slug"].startswith(tuple(f"{c}-" for c in self.trade_coins)):
                    continue
                for tau in self.params.taus:
                    key = (mk["slug"], tau)
                    if key not in self.decided and mk["end"] - tau <= now < mk["end"] - tau + 2:
                        self.decided.add(key)
                        rec = self.evaluate(mk, tau)
                        side = rec.get("side", "-")
                        print(f"{time.strftime('%H:%M:%S')} {mk['slug']} τ={tau}s {side} ask={rec.get('ask')} "
                              f"{rec.get('skip') or 'PAPER BUY %.0f' % rec['shares']}", flush=True)
            await asyncio.sleep(0.25)

    async def resolver(self, every=5):
        """Settle paper orders on a loop of its own, so a slow Gamma lookup never delays the
        scheduler past a decision time."""
        while True:
            due = self.due_orders()
            if due:
                self.resolve_pending(await self._fetch_markets(due))
            await asyncio.sleep(every)

    async def reporter(self, every=3600):
        while True:
            await asyncio.sleep(every)
            write_report(self.out)

    async def run(self, hours):
        # Connection failures inside the websocket library surface as loop callbacks;
        # log them with the rest instead of printing tracebacks every retry.
        asyncio.get_running_loop().set_exception_handler(
            lambda loop, ctx: self.rec.write("errors", {"at": now_ms(), "where": "loop",
                                                        "err": repr(ctx.get("exception") or ctx.get("message"))}))
        tasks = [asyncio.create_task(c) for c in
                 (self.discover(), self.clob_loop(), self.rtds_loop(), self.rtds_loop("rtds-binance"),
                  self.coinbase_loop(), self.scheduler(), self.resolver(), self.reporter())]
        try:
            await asyncio.sleep(hours * 3600)
        finally:
            for t in tasks:
                t.cancel()
            self.rec.close()
            write_report(self.out)


def write_report(out):
    path = Path(out) / "ledger.csv"
    ledger = pd.read_csv(path) if path.exists() else pd.DataFrame()
    decisions = sum(1 for _ in Path(out).glob("raw/*/decisions.jsonl"))
    text = (f"# 纸面交易报告（{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}）\n\n"
            f"只模拟、不下单。决策日志 {decisions} 天，已结算 {len(ledger)} 笔。\n\n{summarize(ledger)}\n")
    (Path(out) / "report.md").write_text(text, encoding="utf-8")
    return text


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("live", "replay"):
        p = sub.add_parser(name)
        p.add_argument("--taus", default="60,30", help="逗号分隔的剩余秒数，如 60,30")
        p.add_argument("--lo", type=float, default=0.80)
        p.add_argument("--hi", type=float, default=0.97)
        p.add_argument("--max-shares", type=float, default=100.0)
        if name == "live":
            p.add_argument("--out", default="paper_data")
            p.add_argument("--hours", type=float, default=24 * 14)
            p.add_argument("--coins", default="btc", help="逗号分隔，录制哪些币种，如 btc,eth,sol,xrp,doge；纸面单只下 BTC")
        else:
            p.add_argument("root", help="解压后的 polymarket-data-samples 目录")
    rp = sub.add_parser("report")
    rp.add_argument("out")
    args = ap.parse_args(argv)

    if args.cmd == "report":
        print(write_report(args.out))
        return
    params = Params(taus=tuple(int(x) for x in args.taus.split(",")), lo=args.lo, hi=args.hi,
                    max_shares=args.max_shares)
    if args.cmd == "replay":
        ledger = replay(args.root, params)
        print(summarize(ledger))
        return
    print(f"纸面交易开始：τ={params.taus}，买价区间 [{params.lo}, {params.hi}]，每笔最多 {params.max_shares:.0f} 份。"
          f"只记录、不下单。输出目录 {args.out}", flush=True)
    asyncio.run(LiveTrader(args.out, params, coins=tuple(args.coins.split(","))).run(args.hours))


if __name__ == "__main__":
    main()
