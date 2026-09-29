"""Paper execution against the recorded or live book.

Taker: a FAK buy capped at the decision-time ask, executed `latency_ms`
later against whatever the book holds *then* (so a fleeting ask is missed,
as it would be live). Maker: a bid that joins the back of the queue at its
price; it fills only when trade prints eat through the size that was ahead
of it, or trade through its price. Cancels are not modelled (they would
only move us up the queue), so maker fills are slightly pessimistic.
Paper orders never remove real liquidity; keep sizes small for that to hold.
"""
from __future__ import annotations

import json
import time

import binary as bo

EPS = 1e-6


class PaperBroker:
    def __init__(self, sink=None, latency_ms=250):
        self.sink = sink if sink is not None else (lambda rec: None)
        self.latency_ms = latency_ms
        self.pending: list[dict] = []        # takers waiting for their latency, makers waiting to rest
        self.resting: dict[str, list] = {}   # token -> live maker orders
        self.orders: dict[str, list] = {}    # slug -> every order, for settlement

    # ----------------------------------------------------------- intake
    def _new(self, ms, v, token, price, size, now, ctx, **extra):
        m = ms.meta
        o = dict(variant=v.name, mode=v.mode, slug=m.slug, start=m.start, end=m.end, token=token,
                 outcome=m.token_side(token), comp=m.down_token if token == m.up_token else m.up_token,
                 fee_rate=m.fee_rate, decision_ms=now, price=round(price, 6), size=size, filled=0.0,
                 cost=0.0, fee=0.0, fills=[], status="pending", ctx=ctx, due=now + self.latency_ms, **extra)
        self.orders.setdefault(m.slug, []).append(o)
        self.pending.append(o)
        return o

    def taker(self, ms, v, token, limit, size, now, ctx):
        self._new(ms, v, token, limit, size, now, ctx, book=ms.books[token])

    def maker(self, ms, v, token, price, size, now, expire_ms, ctx):
        self._new(ms, v, token, price, size, now, ctx, book=ms.books[token], expire_ms=expire_ms, queue=None)

    # ------------------------------------------------------------ clock
    def on_time(self, now):
        if self.pending and any(o["due"] <= now for o in self.pending):
            keep = []
            for o in self.pending:
                if o["due"] > now:
                    keep.append(o)
                elif o["mode"] == "taker":
                    self._take(o, now)
                else:
                    o["queue"] = o["book"].size_at("BUY", o["price"])
                    o["status"] = "resting"
                    self.resting.setdefault(o["token"], []).append(o)
                    self.resting.setdefault(o["comp"], []).append(o)
            self.pending = keep
        for token in list(self.resting):
            live = [o for o in self.resting[token] if o["status"] == "resting" and now < o["expire_ms"]]
            for o in self.resting[token]:
                if o["status"] == "resting" and now >= o["expire_ms"]:
                    o["status"] = "partial" if o["filled"] > 0 else "expired"
            if live:
                self.resting[token] = live
            else:
                del self.resting[token]

    def _take(self, o, now):
        fills = o["book"].take_asks(o["price"], o["size"])
        for p, q in fills:
            self._fill(o, p, q, now, taker=True)
        o["status"] = "filled" if o["filled"] >= o["size"] - EPS else ("partial" if o["filled"] else "missed")
        o["ask_at_exec"] = o["book"].best_ask()

    def _fill(self, o, p, q, now, taker):
        o["filled"] += q
        o["cost"] += p * q
        if taker:
            o["fee"] += q * float(bo.taker_fee(p, o["fee_rate"]))
        o["fills"].append((now, p, q))

    # ------------------------------------------------------------ trades
    def on_trade(self, tr, engine=None):
        for o in self.resting.get(tr.token, ()):
            if o["status"] != "resting":
                continue
            if tr.token == o["token"]:
                if tr.side != "SELL":        # taker sold our token into the bids
                    continue
                px = tr.price
            else:
                if tr.side != "BUY":         # taker bought the complement: matched against our bids
                    continue
                px = round(1.0 - tr.price, 6)
            q = tr.size
            if px < o["price"] - EPS:        # traded through our level: we were filled first
                take = o["size"] - o["filled"]
            elif abs(px - o["price"]) <= EPS:
                eat = min(q, o["queue"])
                o["queue"] -= eat
                take = min(q - eat, o["size"] - o["filled"])
            else:
                continue
            if take > EPS:
                self._fill(o, o["price"], take, tr.recv_ms, taker=False)
                if o["filled"] >= o["size"] - EPS:
                    o["status"] = "filled"

    # -------------------------------------------------------- settlement
    def settle(self, ms):
        m = ms.meta
        for o in self.orders.pop(m.slug, ()):
            if o["status"] in ("pending", "resting"):
                o["status"] = "partial" if o["filled"] else ("missed" if o["mode"] == "taker" else "expired")
            won = (o["outcome"] == "Up") == m.up_won
            payout = o["filled"] * (1.0 if won else 0.0)
            rec = dict(
                variant=o["variant"], mode=o["mode"], slug=o["slug"], start=o["start"], end=o["end"],
                day=time.strftime("%Y-%m-%d", time.gmtime(o["start"])), outcome=o["outcome"],
                status=o["status"], won=won, decision_ms=o["decision_ms"], tau=o["ctx"].get("tau"),
                limit=o["price"], size=o["size"], filled=round(o["filled"], 6),
                avg_price=round(o["cost"] / o["filled"], 6) if o["filled"] else None,
                cost=round(o["cost"], 6), fee=round(o["fee"], 6),
                pnl=round(payout - o["cost"] - o["fee"], 6),
                first_fill_ms=o["fills"][0][0] if o["fills"] else None,
                queue_at_rest=o.get("queue") if o["mode"] == "maker" else None,
                ask_at_exec=o.get("ask_at_exec"),
                **{k: o["ctx"].get(k) for k in ("fav_ask", "fav_bid", "bid_up", "ask_up", "model_q", "sigma_ann", "cl_lag_s")},
            )
            self.sink(rec)


class JsonlSink:
    def __init__(self, path):
        self.f = open(path, "a", encoding="utf-8")

    def __call__(self, rec):
        self.f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.f.flush()

    def close(self):
        self.f.close()
