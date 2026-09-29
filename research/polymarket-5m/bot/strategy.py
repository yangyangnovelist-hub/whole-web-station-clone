"""Pre-registered strategy variants and the event-driven engine that runs them.

The variants are fixed *before* looking at forward data so the validation
gate can correct for exactly this many tries (Bonferroni over VARIANTS).
Changing them after seeing results restarts the clock.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import binary as bo

from .book import Ladder
from .events import Book, Level, Market, Quote, Spot, Tick, Trade, Twap


@dataclass(frozen=True)
class Variant:
    name: str
    mode: str                 # "taker": FAK buy at the ask; "maker": rest a bid
    tau_max: float            # act only while tau_min <= seconds-to-close <= tau_max
    tau_min: float
    lo: float                 # favourite's ask (taker) or bid (maker) must be in [lo, hi]
    hi: float
    model_edge: float | None = None   # taker: need model_q - ask - fee >= this
    size: float = 10.0        # shares
    slip: float = 0.0         # taker: limit = ask at decision + slip (pays up to catch a moving ask)
    cancel_tau: float = 3.0   # maker: cancel when this close to the end
    note: str = ""


# Frozen 2026-09-29, after looking only at the 2026-09-08 sample (which the
# gate therefore ignores). slip=0.02 was set on that day too: with 250 ms
# latency, capping at the decision ask missed a third of the orders and every
# miss was a winner; +2c recovers most of them (see README §11).
VARIANTS = (
    Variant("fav_taker_30", "taker", 35, 25, 0.80, 0.97, slip=0.02,
            note="单日样本里唯一为正的一行：剩 25–35 秒吃 0.80–0.97 的强势方"),
    Variant("fav_taker_60", "taker", 60, 45, 0.80, 0.97, slip=0.02,
            note="同上，提前到剩 45–60 秒（TWAP 窗口还没锁定多少）"),
    Variant("fav_taker_model", "taker", 60, 10, 0.70, 0.97, model_edge=0.02, slip=0.02,
            note="亚式二元模型认为强势方便宜 ≥2¢ 才吃"),
    Variant("fav_maker", "maker", 60, 10, 0.85, 0.97,
            note="在强势方买一挂单排队（0 手续费），剩 3 秒撤单"),
    Variant("sweep_taker", "taker", 30, 5, 0.97, 0.99, slip=0.02,
            note="扫尾盘：剩 5–30 秒吃 0.97–0.99，尾部风险最大"),
)
BY_NAME = {v.name: v for v in VARIANTS}


class Chainlink:
    """Chainlink instantaneous feed on a 1s grid plus a causal vol forecast.

    Vol is an EWMA of overlapping 15s log returns scaled to per-second
    variance (the §10 finding: 1s returns of this smoothed feed understate
    vol by ~30%).
    """

    def __init__(self, halflife_s=1800, k=15, warmup_s=900, keep_s=900, max_gap_s=120):
        self.k, self.warmup_s, self.keep_s, self.max_gap_s = k, warmup_s, keep_s, max_gap_s
        self.decay = 0.5 ** (1.0 / halflife_s)
        self.px: dict[int, float] = {}
        self.last_sec = None
        self.var = None
        self.n = 0
        self.twap: dict[int, float] = {}

    def add(self, sec, value):
        lp = math.log(value)
        if self.last_sec is None or sec - self.last_sec > self.max_gap_s:
            self.px, self.var, self.n = {sec: lp}, None, 0
            self.last_sec = sec
            return
        if sec <= self.last_sec:          # late tick: fill a hole, never rewrite
            self.px.setdefault(sec, lp)
            return
        prev = self.px[self.last_sec]
        for s in range(self.last_sec + 1, sec):
            self.px[s] = prev
            self._step(s)
        self.px[sec] = lp
        self._step(sec)
        self.last_sec = sec
        if len(self.px) > self.keep_s + 60:
            cut = sec - self.keep_s
            self.px = {s: v for s, v in self.px.items() if s >= cut}

    def _step(self, s):
        base = self.px.get(s - self.k)
        if base is None:
            return
        r2 = (self.px[s] - base) ** 2 / self.k
        self.var = r2 if self.var is None else self.decay * self.var + (1 - self.decay) * r2
        self.n += 1

    def sigma_s(self):
        return math.sqrt(self.var) if self.var is not None and self.n >= self.warmup_s else None

    def prob_up(self, m: Market):
        """Asian-digital P(Up) from what has been received, or None."""
        sig = self.sigma_s()
        if sig is None or m.strike is None or self.last_sec is None:
            return None
        t = min(max(self.last_sec - m.start, 0), bo.WINDOW_S)
        first = m.end - bo.TWAP_S + 1
        known = sum(self.px.get(s, self.px[self.last_sec]) for s in range(first, min(self.last_sec, m.end) + 1))
        x = self.px[self.last_sec]
        return float(bo.prob_up(x, math.log(m.strike), sig, t, known))


class MarketState:
    __slots__ = ("meta", "books", "acted", "settled")

    def __init__(self, meta: Market):
        self.meta = meta
        self.books = {meta.up_token: Ladder(), meta.down_token: Ladder()}
        self.acted: set[str] = set()
        self.settled = False

    def top(self):
        """(bid_up, ask_up) combining each book with its mirror."""
        up, dn = self.books[self.meta.up_token], self.books[self.meta.down_token]
        bids = [x for x in (up.best_bid(), _inv(dn.best_ask())) if x is not None]
        asks = [x for x in (up.best_ask(), _inv(dn.best_bid())) if x is not None]
        return (max(bids) if bids else None), (min(asks) if asks else None)


def _inv(x):
    return None if x is None else round(1.0 - x, 6)


class Engine:
    def __init__(self, broker, variants=VARIANTS, chainlink=None):
        self.broker = broker
        self.variants = tuple(variants)
        self.cl = chainlink or Chainlink()
        self.markets: dict[str, MarketState] = {}
        self.by_token: dict[str, MarketState] = {}
        self.max_tau = max(v.tau_max for v in self.variants)
        self.now_ms = 0
        self.halted = False      # live driver: market data stale -> no new orders
        self.model_ok = True     # live driver: Chainlink stale -> no model-based orders

    # ----------------------------------------------------------- markets
    def add_market(self, m: Market):
        if m.slug in self.markets:
            old = self.markets[m.slug].meta
            old.up_won = m.up_won if m.up_won is not None else old.up_won
            old.strike = old.strike if old.strike is not None else m.strike
            return
        ms = MarketState(m)
        if m.strike is None and m.start in self.cl.twap:
            m.strike = self.cl.twap[m.start]
        self.markets[m.slug] = ms
        self.by_token[m.up_token] = ms
        self.by_token[m.down_token] = ms

    def drop_market(self, slug):
        ms = self.markets.pop(slug, None)
        if ms:
            self.by_token.pop(ms.meta.up_token, None)
            self.by_token.pop(ms.meta.down_token, None)

    # ------------------------------------------------------------ events
    def on_event(self, ev):
        self.now_ms = now = ev.recv_ms
        if isinstance(ev, (Quote, Book, Level)):
            ms = self.by_token.get(ev.token)
            if ms is None:
                return
            book = ms.books[ev.token]
            if isinstance(ev, Quote):
                book.trim(ev.bid, ev.ask, now)
            elif isinstance(ev, Book):
                book.snapshot(ev.bids, ev.asks, now)
            else:
                book.level(ev.side, ev.price, ev.size, now)
            self.broker.on_time(now)
            self._decide(ms, now)
        elif isinstance(ev, Trade):
            self.broker.on_trade(ev, self)
        elif isinstance(ev, Spot):
            self.cl.add(ev.sec, ev.value)
        elif isinstance(ev, Twap):
            self.cl.twap[ev.sec] = ev.value
            for ms in self.markets.values():
                if ms.meta.strike is None and ms.meta.start == ev.sec:
                    ms.meta.strike = ev.value
            if len(self.cl.twap) > 4000:
                cut = ev.sec - 3600
                self.cl.twap = {s: v for s, v in self.cl.twap.items() if s >= cut}
        elif isinstance(ev, Tick):
            self.broker.on_time(now)
            for ms in list(self.markets.values()):
                self._decide(ms, now)
            self.settle_ready(now)

    def settle_ready(self, now_ms, grace_s=2):
        for slug, ms in list(self.markets.items()):
            m = ms.meta
            if now_ms / 1000 < m.end + grace_s:
                continue
            if m.up_won is not None and not ms.settled:
                self.broker.settle(ms)
                ms.settled = True
            if ms.settled:
                self.drop_market(slug)
            elif now_ms / 1000 > m.end + 3600:        # never resolved: forget it
                self.broker.orders.pop(slug, None)
                self.drop_market(slug)

    # ---------------------------------------------------------- decisions
    def context(self, ms, now):
        bid_up, ask_up = ms.top()
        if bid_up is None or ask_up is None or ask_up < bid_up:
            return None
        m = ms.meta
        fav_up = (bid_up + ask_up) / 2 >= 0.5
        fav = m.up_token if fav_up else m.down_token
        book = ms.books[fav]
        fa = book.best_ask() if book.best_ask() is not None else (ask_up if fav_up else _inv(bid_up))
        fb = book.best_bid() if book.best_bid() is not None else (bid_up if fav_up else _inv(ask_up))
        return dict(tau=round(m.end - now / 1000, 3), bid_up=bid_up, ask_up=ask_up, fav=fav,
                    fav_side="Up" if fav_up else "Down", fav_ask=fa, fav_bid=fb,
                    cl_lag_s=None if self.cl.last_sec is None else round(now / 1000 - self.cl.last_sec, 3))

    def with_model(self, ms, ctx):
        """Add the model's view of the favourite (lazy: it is the slow part)."""
        if "model_q" not in ctx:
            q_up = self.cl.prob_up(ms.meta)
            sig = self.cl.sigma_s()
            ctx["model_q"] = None if q_up is None else (q_up if ctx["fav_side"] == "Up" else 1 - q_up)
            ctx["sigma_ann"] = None if sig is None else float(bo.annual_vol(sig))
        return ctx

    def _decide(self, ms, now):
        m = ms.meta
        tau = m.end - now / 1000
        if self.halted or tau <= 0 or tau > self.max_tau or len(ms.acted) == len(self.variants):
            return
        ctx = None
        for v in self.variants:
            if v.name in ms.acted or not (v.tau_min <= tau <= v.tau_max):
                continue
            ctx = ctx or self.context(ms, now)
            if ctx is None:
                return
            if v.mode == "taker":
                a = ctx["fav_ask"]
                if a is None or not (v.lo - 1e-9 <= a <= v.hi + 1e-9):
                    continue
                if v.model_edge is not None:
                    if not self.model_ok:
                        continue
                    q = self.with_model(ms, ctx)["model_q"]
                    if q is None or q - a - float(bo.taker_fee(a, m.fee_rate)) < v.model_edge:
                        continue
                ms.acted.add(v.name)
                limit = round(min(a + v.slip, 0.99), 6)
                self.broker.taker(ms, v, ctx["fav"], limit, v.size, now, dict(self.with_model(ms, ctx)))
            else:
                b = ctx["fav_bid"]
                if b is None or not (v.lo - 1e-9 <= b <= v.hi + 1e-9):
                    continue
                ms.acted.add(v.name)
                self.broker.maker(ms, v, ctx["fav"], b, v.size, now, int((m.end - v.cancel_tau) * 1000),
                                  dict(self.with_model(ms, ctx)))
