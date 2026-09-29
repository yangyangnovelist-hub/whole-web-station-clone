"""Real orders for gated taker variants, on top of the paper broker.

Every variant keeps paper-trading exactly as in `paper` mode; the ones named
with --variant *also* send a real FAK buy capped at the decision-time ask.
Guards, all checked before every order:

- the variant's verdict in --gate is GO, the gate is at most 14 days old and
  was computed without the in-sample day (re-read every 10 minutes);
- the --kill-file does not exist;
- shares <= --max-shares, orders today <= --max-orders-per-day;
- today's realised loss (UTC) has not reached --max-daily-loss.

Credentials come from the environment: POLYMARKET_PRIVATE_KEY and optionally
POLYMARKET_WALLET (defaults to the signer's deposit wallet). Uses the
official SDK: pip install polymarket-client.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from .evaluate import GATE
from .paper import JsonlSink, PaperBroker

log = logging.getLogger("bot.live")


def gate_allows(path, variant, now=None):
    """(ok, reason) for trading `variant` for real under the gate file."""
    now = now or time.time()
    try:
        g = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return False, f"gate unreadable: {e}"
    if g.get("include_in_sample"):
        return False, "gate was computed with the in-sample day"
    if now - g.get("generated_at", 0) > GATE["max_age_days"] * 86400:
        return False, "gate is stale; re-run evaluate on fresh paper logs"
    v = g.get("variants", {}).get(variant)
    if not v or v.get("verdict") != "GO":
        return False, f"{variant} is not GO in the gate"
    return True, "ok"


class LiveBroker(PaperBroker):
    def __init__(self, sink, args, latency_ms=250):
        super().__init__(sink, latency_ms)
        self.args = args
        self.live = set(args.variant)
        self.real_sink = JsonlSink(str(Path(args.log).with_name("real_orders.jsonl")))
        self.client = None
        self.allowed = {}
        self.day = None
        self.day_orders = 0
        self.day_pnl = 0.0
        self.real = {}          # slug -> list of real fills awaiting settlement
        self.inflight = set()   # keep task references so they are not garbage-collected

    async def start(self):
        from polymarket import AsyncSecureClient

        key = os.environ.get("POLYMARKET_PRIVATE_KEY")
        if not key:
            raise SystemExit("POLYMARKET_PRIVATE_KEY is not set")
        self.client = await AsyncSecureClient.create(private_key=key, wallet=os.environ.get("POLYMARKET_WALLET") or None)
        self._refresh_gate()
        if not any(self.allowed.values()):
            raise SystemExit(f"no variant passes the gate: {self.allowed}")

    def _refresh_gate(self):
        self.allowed = {}
        for v in self.live:
            ok, why = gate_allows(self.args.gate, v)
            self.allowed[v] = ok
            if not ok:
                log.warning("gate: %s blocked (%s)", v, why)

    async def guard(self):
        while True:
            await asyncio.sleep(600)
            self._refresh_gate()

    def _roll_day(self):
        d = time.strftime("%Y-%m-%d", time.gmtime())
        if d != self.day:
            self.day, self.day_orders, self.day_pnl = d, 0, 0.0

    def _blocked(self, v, size):
        self._roll_day()
        if not self.allowed.get(v.name):
            return "gate"
        if Path(self.args.kill_file).exists():
            return "kill-file"
        if size > self.args.max_shares:
            return "max-shares"
        if self.day_orders >= self.args.max_orders_per_day:
            return "max-orders"
        if -self.day_pnl >= self.args.max_daily_loss:
            return "max-daily-loss"
        return None

    def taker(self, ms, v, token, limit, size, now, ctx):
        super().taker(ms, v, token, limit, size, now, ctx)
        if v.name not in self.live:
            return
        size = min(size, self.args.max_shares)
        why = self._blocked(v, size)
        if why:
            self.real_sink(dict(variant=v.name, slug=ms.meta.slug, status="blocked", reason=why, ts=now))
            return
        self.day_orders += 1
        task = asyncio.get_running_loop().create_task(self._send(ms.meta, v, token, limit, size, now, ctx))
        self.inflight.add(task)
        task.add_done_callback(self.inflight.discard)

    async def _send(self, m, v, token, limit, size, now, ctx):
        rec = dict(variant=v.name, slug=m.slug, token=token, outcome=m.token_side(token), limit=limit,
                   size=size, decision_ms=now, tau=ctx["tau"])
        try:
            resp = await self.client.place_market_order(
                token_id=token, side="BUY", amount=round(size * limit, 2), max_price=limit, order_type="FAK")
        except Exception as e:  # noqa: BLE001
            rec.update(status="error", error=repr(e), sent_ms=int(time.time() * 1000))
            self.real_sink(rec)
            return
        rec["sent_ms"] = int(time.time() * 1000)
        if getattr(resp, "ok", False):
            rec.update(status=str(resp.status), order_id=resp.order_id,
                       shares=float(resp.taking_amount), spent=float(resp.making_amount))
            if rec["shares"] > 0:
                self.real.setdefault(m.slug, []).append(rec)
        else:
            rec.update(status="rejected", code=getattr(resp, "code", None), message=getattr(resp, "message", None))
        self.real_sink(rec)

    def settle(self, ms):
        super().settle(ms)
        m = ms.meta
        for rec in self.real.pop(m.slug, ()):
            won = (rec["outcome"] == "Up") == m.up_won
            pnl = rec["shares"] * (1.0 if won else 0.0) - rec["spent"]
            self._roll_day()
            self.day_pnl += pnl
            self.real_sink(dict(rec, status="settled", won=won, pnl=round(pnl, 6),
                                note="spent = USDC the exchange reported; reconcile fees against your activity export"))
