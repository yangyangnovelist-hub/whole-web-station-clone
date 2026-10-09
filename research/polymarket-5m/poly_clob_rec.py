"""Polymarket CLOB market-channel recorder for BTC Up/Down 5m and 15m markets (research only, no orders).
Every WS message is written verbatim with our receive time (ns) to hourly gzip files:
  <out>/poly_clob.<YYYYMMDD>T<HH>.jsonl.gz   lines: {"rn": <recv_ns>, "c": <conn>, "m": <raw message>}
Two redundant connections per market set (same subscription as the live H feed: level 2, initial dump).
Markets are rolled every 5 s: current and next 5m and 15m windows."""
import asyncio, gzip, json, os, sys, time
import urllib.request, urllib.parse
import websockets

GAMMA = "https://gamma-api.polymarket.com"
WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
OUT = sys.argv[1] if __name__ == "__main__" else ""
if OUT:
    os.makedirs(OUT, exist_ok=True)


class Sink:
    def __init__(self):
        self.h = None; self.f = None

    def write(self, line: str):
        h = time.strftime("%Y%m%dT%H", time.gmtime())
        if h != self.h:
            if self.f: self.f.close()
            self.f = gzip.open(os.path.join(OUT, f"poly_clob.{h}.jsonl.gz"), "at", compresslevel=3); self.h = h
        self.f.write(line + "\n")

    def flush(self):
        if self.f: self.f.flush()


sink = Sink()


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


async def tokens(http, slug):
    try:
        url = f"{GAMMA}/events?" + urllib.parse.urlencode({"slug": slug})
        req = urllib.request.Request(url, headers={"User-Agent": "research-recorder (read-only)"})
        ev = await asyncio.get_running_loop().run_in_executor(None, lambda: json.load(urllib.request.urlopen(req, timeout=8)))
        m = ev[0]["markets"][0]
        sink.write(json.dumps({"rn": time.time_ns(), "c": -1, "k": "meta", "m": {
            "meta": slug, "cid": m["conditionId"], "tokens": m["clobTokenIds"],
            "outcomes": m["outcomes"],
        }}))
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
                        await asyncio.sleep(10); await ws.send("PING")
                t = asyncio.create_task(hb())
                try:
                    while not stop.is_set():
                        try:
                            async with asyncio.timeout(1.0):
                                msg = await ws.recv()
                        except TimeoutError:
                            continue
                        rn = time.time_ns()
                        if msg in ("PONG", ""): continue
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
    http = None
    cur, stop = None, None
    cache = {}

    async def flusher():
        while True:
            await asyncio.sleep(2); sink.flush()
    asyncio.create_task(flusher())
    while True:
        now = int(time.time())
        slugs = [f"btc-updown-5m-{s}" for s in (now // 300 * 300, now // 300 * 300 + 300)] + \
                [f"btc-updown-15m-{s}" for s in (now // 900 * 900, now // 900 * 900 + 900)]
        assets = []
        for s in slugs:
            if s not in cache or not cache[s]:
                cache[s] = await tokens(http, s)
            assets += cache[s]
        a = frozenset(assets)
        if a and a != cur:
            new = asyncio.Event()
            for i in range(4): asyncio.create_task(conn(a, i, new))
            old = stop; cur, stop = a, new
            if old is not None:
                async def retire(e=old):
                    await asyncio.sleep(10); e.set()
                asyncio.create_task(retire())
        for k in [k for k in cache if int(k.split("-")[-1]) < now - 1800]: del cache[k]
        await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
