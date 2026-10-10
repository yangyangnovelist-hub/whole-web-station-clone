"""Polymarket CLOB market-channel recorder for BTC Up/Down 5m markets (research only, no orders).
Every WS message is written verbatim with our receive time (ns) to hourly gzip files:
  <out>/poly_clob.<YYYYMMDD>T<HH>.jsonl.gz   lines: {"rn": <recv_ns>, "c": <conn>, "m": <raw message>}
Four redundant connections per market set (same subscription as the live H feed: level 2, initial dump).
Markets are rolled every 5 s: current and next 5m windows."""
import asyncio
import gzip
import json
import os
import signal
import sys
import time
import urllib.parse
import urllib.request
import websockets

GAMMA = "https://gamma-api.polymarket.com"
WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
OUT = sys.argv[1] if __name__ == "__main__" else ""
if OUT:
    os.makedirs(OUT, exist_ok=True)


class Sink:
    def __init__(self):
        self.h = None
        self.f = None
        self.metadata = {}
        self.active_slugs = set()
        self.last_rn = -1

    def remember_metadata(self, metadata):
        slug = str(metadata.get("meta") or "")
        if slug:
            self.metadata[slug] = dict(metadata)

    def set_active_slugs(self, slugs):
        self.active_slugs = {str(slug) for slug in slugs}

    def _rewrite_active_metadata(self):
        for slug in sorted(self.active_slugs):
            metadata = self.metadata.get(slug)
            if metadata is None:
                continue
            self.last_rn = max(self.last_rn + 1, time.time_ns())
            row = {"rn": self.last_rn, "c": -1, "k": "meta", "m": metadata}
            self.f.write(json.dumps(row, separators=(",", ":")) + "\n")

    def write(self, line: str):
        h = time.strftime("%Y%m%dT%H", time.gmtime())
        rotated = self.h is not None and h != self.h
        if h != self.h:
            if self.f:
                self.f.close()
            self.f = gzip.open(
                os.path.join(OUT, f"poly_clob.{h}.jsonl.gz"),
                "at",
                compresslevel=3,
            )
            self.h = h
        self.f.write(line + "\n")
        try:
            self.last_rn = max(self.last_rn, int(json.loads(line).get("rn", -1)))
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            pass
        if rotated:
            self._rewrite_active_metadata()

    def flush(self):
        if self.f:
            self.f.flush()

    def close(self):
        if self.f:
            self.f.close()
            self.f = None


sink = Sink()


def _close_and_exit(*_):
    """Finish the active gzip member before a systemd restart or shutdown."""
    try:
        sink.close()
    finally:
        os._exit(0)


def envelope(rn, idx, epoch, sequence, kind, **details):
    return {
        "rn": rn,
        "c": idx,
        "e": epoch,
        "s": sequence,
        "k": kind,
        **details,
    }


class RecentFrames:
    """Keep only the first receipt of a duplicated market frame across warm sockets."""

    def __init__(self, keep_ns=2_000_000_000):
        self.keep_ns = keep_ns
        self.seen = {}
        self.next_prune_ns = 0

    def first(self, frame, receive_ns):
        previous = self.seen.get(frame)
        self.seen[frame] = receive_ns
        if receive_ns >= self.next_prune_ns:
            cutoff = receive_ns - self.keep_ns
            self.seen = {key: value for key, value in self.seen.items() if value >= cutoff}
            self.next_prune_ns = receive_ns + self.keep_ns
        return previous is None or receive_ns - previous > self.keep_ns


recent_frames = RecentFrames()


def single_line_frame(message):
    """Keep one websocket frame on one JSONL physical line before envelope embedding."""
    if isinstance(message, bytes):
        message = message.decode()
    return message.replace("\r", " ").replace("\n", " ")


def market_slugs(now):
    start = int(now) // 300 * 300
    return [f"btc-updown-5m-{slot}" for slot in (start, start + 300)]


async def tokens(http, slug):
    try:
        url = f"{GAMMA}/events?" + urllib.parse.urlencode({"slug": slug})
        req = urllib.request.Request(url, headers={"User-Agent": "research-recorder (read-only)"})
        ev = await asyncio.get_running_loop().run_in_executor(None, lambda: json.load(urllib.request.urlopen(req, timeout=8)))
        m = ev[0]["markets"][0]
        metadata = {
            "meta": slug, "cid": m["conditionId"], "tokens": m["clobTokenIds"],
            "outcomes": m["outcomes"],
        }
        sink.remember_metadata(metadata)
        sink.write(json.dumps({"rn": time.time_ns(), "c": -1, "k": "meta", "m": metadata}))
        return json.loads(m["clobTokenIds"])
    except Exception:
        return []


async def conn(assets, idx, stop):
    while not stop.is_set():
        epoch = None
        sequence = 0
        try:
            async with websockets.connect(WS, max_size=2 ** 24, max_queue=20000, ping_interval=None, open_timeout=20) as ws:
                await ws.send(json.dumps({"assets_ids": sorted(assets), "type": "market", "initial_dump": True, "level": 2,
                                          "custom_feature_enabled": False}))
                epoch = time.time_ns()
                sink.write(json.dumps(
                    envelope(epoch, idx, epoch, sequence, "connection", assets=sorted(assets)),
                    separators=(",", ":"),
                ))

                async def hb():
                    while True:
                        await asyncio.sleep(10)
                        await ws.send("PING")
                t = asyncio.create_task(hb())
                try:
                    while not stop.is_set():
                        try:
                            async with asyncio.timeout(1.0):
                                msg = await ws.recv()
                        except TimeoutError:
                            continue
                        rn = time.time_ns()
                        msg = single_line_frame(msg)
                        if msg in ("PONG", ""):
                            continue
                        if not recent_frames.first(msg, rn):
                            continue
                        sequence += 1
                        sink.write(
                            '{"rn":%d,"c":%d,"e":%d,"s":%d,"k":"message","m":%s}'
                            % (rn, idx, epoch, sequence,
                               msg if msg[:1] in "[{" else json.dumps(msg))
                        )
                finally:
                    t.cancel()
                    if epoch is not None:
                        sequence += 1
                        sink.write(json.dumps(
                            envelope(time.time_ns(), idx, epoch, sequence, "disconnect"),
                            separators=(",", ":"),
                        ))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            sink.write(json.dumps(envelope(
                time.time_ns(), idx, epoch, sequence, "error",
                err=f"{type(exc).__name__}: {str(exc)[:120]}",
            ), separators=(",", ":")))
            await asyncio.sleep(0.5)


async def main():
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, _close_and_exit)
    http = None
    cur, stop = None, None
    cache = {}

    async def flusher():
        while True:
            await asyncio.sleep(2)
            sink.flush()
    asyncio.create_task(flusher())
    while True:
        now = int(time.time())
        slugs = market_slugs(now)
        sink.set_active_slugs(slugs)
        assets = []
        for s in slugs:
            if s not in cache or not cache[s]:
                cache[s] = await tokens(http, s)
            assets += cache[s]
        a = frozenset(assets)
        if a and a != cur:
            new = asyncio.Event()
            for i in range(4):
                asyncio.create_task(conn(a, i, new))
            old = stop
            cur, stop = a, new
            if old is not None:
                async def retire(e=old):
                    await asyncio.sleep(10)
                    e.set()
                asyncio.create_task(retire())
        for k in [k for k in cache if int(k.split("-")[-1]) < now - 1800]:
            del cache[k]
        await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
