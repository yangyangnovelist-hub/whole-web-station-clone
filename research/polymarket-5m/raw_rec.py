"""Raw multi-venue BTC recorder: every websocket frame as `<receive unix ns>\t<raw text>`, one gzip per source per hour.
Research only (no keys, no orders).  Usage: python raw_rec.py <out_dir> <hours>

v2: the original 8 sources run exactly as in v1, in the main process (v2 only adds a SIGTERM handler that closes
every gzip file, so a restart no longer leaves an unterminated gzip member).  The extra public sources (NEW) run in child
processes (`raw_rec.py <out_dir> <hours> --group <name>`) that the main process starts and restarts, so their load
cannot delay the original sources' receive timestamps inside one event loop.  New-source line format is the same;
binary frames are gunzipped (HTX) or stored as `b64:<base64>` (MEXC spot protobuf).

v3: adds child group x3 (liquidations, mark price/funding, open interest, Deribit DVOL); every v2 source is unchanged.
Binance USD-M forceOrder and markPrice are served only on the /market path (the /ws, /stream and /public paths accept
the connection but never push these streams)."""
import asyncio, gzip, json, os, sys, time
import websockets

if __name__ == "__main__":
    OUT, HOURS = sys.argv[1], float(sys.argv[2])
else:
    OUT, HOURS = "", 0.0
SOURCES = {
    "bn_spot": ("wss://stream.binance.com:9443/stream?streams=btcusdt@trade/btcusdt@bookTicker/btcusdt@depth20@100ms"
                "&timeUnit=MICROSECOND", None, None),
    "bn_spot_2": ("wss://stream.binance.com:9443/ws/btcusdt@trade?timeUnit=MICROSECOND", None, None),
    "bn_spot_3": ("wss://stream.binance.com:9443/ws/btcusdt@trade?timeUnit=MICROSECOND", None, None),
    "bn_spot_4": ("wss://stream.binance.com:9443/ws/btcusdt@trade?timeUnit=MICROSECOND", None, None),
    "bn_fut_mkt": ("wss://fstream.binance.com/market/stream?streams=btcusdt@trade/btcusdt@aggTrade", None, None),
    "bn_fut_trade": ("wss://fstream.binance.com/ws/btcusdt@trade", None, None),
    "bn_fut_trade_2": ("wss://fstream.binance.com/ws/btcusdt@trade", None, None),
    "bn_fut_pub": ("wss://fstream.binance.com/public/stream?streams=btcusdt@bookTicker/btcusdt@depth20@100ms", None, None),
    "bn_fut_pub_2": ("wss://fstream.binance.com/public/stream?streams=btcusdt@bookTicker", None, None),
    "bn_fut_pub_3": ("wss://fstream.binance.com/public/stream?streams=btcusdt@bookTicker", None, None),
    "okx": ("wss://ws.okx.com:8443/ws/v5/public",
            {"op": "subscribe", "args": [{"channel": "bbo-tbt", "instId": "BTC-USDT"}, {"channel": "trades", "instId": "BTC-USDT"},
                                         {"channel": "bbo-tbt", "instId": "BTC-USDT-SWAP"},
                                         {"channel": "trades", "instId": "BTC-USDT-SWAP"}]}, ("ping", 20)),
    "bybit_spot": ("wss://stream.bybit.com/v5/public/spot",
                   {"op": "subscribe", "args": ["orderbook.1.BTCUSDT", "publicTrade.BTCUSDT"]}, ('{"op":"ping"}', 15)),
    "bybit_lin": ("wss://stream.bybit.com/v5/public/linear",
                  {"op": "subscribe", "args": ["orderbook.1.BTCUSDT", "publicTrade.BTCUSDT"]}, ('{"op":"ping"}', 15)),
    "coinbase": ("wss://ws-feed.exchange.coinbase.com",
                 {"type": "subscribe", "product_ids": ["BTC-USD"], "channels": ["matches", "ticker"]}, None),
}
END = time.time() + HOURS * 3600
counts = {k: 0 for k in SOURCES}
errors = {k: 0 for k in SOURCES}


SINKS = []


class Sink:
    def __init__(self, src):
        self.src, self.hour, self.f, self.last_flush = src, None, None, 0.0
        SINKS.append(self)

    def write(self, ns, text):
        h = time.strftime("%Y%m%dT%H", time.gmtime(ns / 1e9))
        if h != self.hour:
            if self.f:
                self.f.close()
            self.hour = h
            self.f = gzip.open(os.path.join(OUT, f"{self.src}.{h}.txt.gz"), "at", compresslevel=3)
        self.f.write(f"{ns}\t{text}\n")
        now = time.monotonic()
        if now - self.last_flush > 5:
            self.f.flush()
            self.last_flush = now

    def lifecycle(self, ns, kind, epoch, **details):
        payload = {
            "_recorder": {
                "schema": "recorder-lifecycle-v1",
                "kind": kind,
                "epoch": epoch,
                **details,
            }
        }
        self.write(ns, json.dumps(payload, separators=(",", ":")))

    def close(self):
        if self.f:
            self.f.close()


async def pinger(ws, msg, every):
    while True:
        await asyncio.sleep(every)
        await ws.send(msg)


async def run(src):
    url, sub, ping = SOURCES[src]
    sink = Sink(src)
    fails = 0
    try:
        while time.time() < END:
            try:
                async with websockets.connect(url, max_size=None, open_timeout=10, compression=None) as ws:
                    if sub:
                        await ws.send(json.dumps(sub))
                    epoch = time.time_ns()
                    sink.lifecycle(epoch, "connection", epoch, source=src)
                    pt = asyncio.create_task(pinger(ws, *ping)) if ping else None
                    fails = 0
                    try:
                        while time.time() < END:
                            raw = await asyncio.wait_for(ws.recv(), timeout=30)
                            ns = time.time_ns()
                            sink.write(ns, raw if isinstance(raw, str) else raw.decode())
                            counts[src] += 1
                    finally:
                        if pt:
                            pt.cancel()
                        sink.lifecycle(time.time_ns(), "disconnect", epoch, source=src)
            except Exception as exc:
                errors[src] += 1
                fails += 1
                print(f"{time.strftime('%H:%M:%S')} {src} error {type(exc).__name__}: {str(exc)[:150]}", flush=True)
                await asyncio.sleep(min(30, 0.5 * 2 ** min(fails, 6)))
    finally:
        sink.close()


async def report():
    while time.time() < END:
        await asyncio.sleep(60)
        print(f"{time.strftime('%H:%M:%S')} counts {json.dumps(counts)} errors {json.dumps(errors)}", flush=True)


# ---------------------------------------------------------------------------------------------------------------
# v2 additions: extra public sources, run in child processes.
# Spec keys: g = child group; url = str or zero-arg async function returning the url (KuCoin token);
# subs = list of frames (dict -> json, str as is) or zero-arg function returning that list; ping = (frame or
# zero-arg function, seconds) application-level keepalive; reply = function(text) -> frame to send back or None;
# dec = how binary frames become text (None utf-8, "gzip", "b64"); delay = seconds to wait before subscribing.
import base64, signal, urllib.request, uuid

UA = "raw-recorder/2 (public market data research)"


def _ms():
    return int(time.time() * 1000)


def kucoin_url(rest):
    async def f():
        def post():
            req = urllib.request.Request(rest, data=b"", method="POST", headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        d = (await asyncio.to_thread(post))["data"]
        return f'{d["instanceServers"][0]["endpoint"]}?token={d["token"]}&connectId={uuid.uuid4().hex}'
    return f


def htx_pong(text):  # HTX sends {"ping": <ms>} (gzipped) and drops the link after 2 unanswered pings
    if text.startswith('{"ping"'):
        return text.replace('"ping"', '"pong"', 1)


def deribit_reply(text):  # answer heartbeat test_request (enabled by public/set_heartbeat)
    if '"test_request"' in text:
        return '{"jsonrpc":"2.0","id":9,"method":"public/test","params":{}}'


def cryptocom_reply(text):  # must answer public/heartbeat with the same id or the server disconnects
    if '"public/heartbeat"' in text:
        return json.dumps({"id": json.loads(text)["id"], "method": "public/respond-heartbeat"})


def bitstamp_reply(text):
    if '"bts:request_reconnect"' in text:
        raise ConnectionResetError("bts:request_reconnect")


RTDS_CL = json.dumps({"action": "subscribe", "subscriptions": [
    {"topic": "crypto_prices_chainlink", "type": "*", "filters": '{"symbol":"btc/usd"}'},
    {"topic": "crypto_prices_twap_sixty", "type": "update", "filters": '{"symbol":"btc/usd"}'}]}, separators=(",", ":"))
RTDS_BN = json.dumps({"action": "subscribe", "subscriptions": [
    {"topic": "crypto_prices", "type": "update", "filters": '{"symbol":"btcusdt"}'}]}, separators=(",", ":"))

NEW = {
    # Coinbase International Exchange BTC perpetual: its own market-data websocket (ws-md.international.coinbase.com)
    # rejects unsigned subscriptions ("Unauthorized User"), so this is the anonymous Advanced Trade route that carries
    # the INTX perp (same route as the T1 shadow).  INTX perps are PAUSED since 2026-10-01 09:00 UTC; kept so a
    # restart is captured.  cb_perp is Coinbase Derivatives' US "BTC PERP" (BIP-20DEC30-CDE), which is trading.
    "cb_perp": dict(g="x1", url="wss://advanced-trade-ws.coinbase.com", subs=[
        {"type": "subscribe", "product_ids": ["BIP-20DEC30-CDE"], "channel": "level2"},
        {"type": "subscribe", "product_ids": ["BIP-20DEC30-CDE"], "channel": "market_trades"},
        {"type": "subscribe", "channel": "heartbeats"}]),
    "cbintl_perp": dict(g="x1", url="wss://advanced-trade-ws.coinbase.com", subs=[
        {"type": "subscribe", "product_ids": ["BTC-PERP-INTX"], "channel": "level2"},
        {"type": "subscribe", "product_ids": ["BTC-PERP-INTX"], "channel": "market_trades"},
        {"type": "subscribe", "channel": "heartbeats"}]),
    "cbadv_spot": dict(g="x1", url="wss://advanced-trade-ws.coinbase.com", subs=[
        {"type": "subscribe", "product_ids": ["BTC-USD"], "channel": "ticker"},
        {"type": "subscribe", "product_ids": ["BTC-USD"], "channel": "market_trades"},
        {"type": "subscribe", "channel": "heartbeats"}]),
    "deribit": dict(g="x1", url="wss://www.deribit.com/ws/api/v2", subs=[
        {"jsonrpc": "2.0", "id": 1, "method": "public/set_heartbeat", "params": {"interval": 30}},
        {"jsonrpc": "2.0", "id": 2, "method": "public/subscribe", "params": {"channels": [
            "trades.BTC-PERPETUAL.100ms", "quote.BTC-PERPETUAL", "deribit_price_index.btc_usd"]}}],
        reply=deribit_reply),
    "kraken": dict(g="x1", url="wss://ws.kraken.com/v2", subs=[
        {"method": "subscribe", "params": {"channel": "trade", "symbol": ["BTC/USD"]}},
        {"method": "subscribe", "params": {"channel": "book", "symbol": ["BTC/USD"], "depth": 10}}]),
    "kraken_fut": dict(g="x1", url="wss://futures.kraken.com/ws/v1", subs=[
        {"event": "subscribe", "feed": "trade", "product_ids": ["PF_XBTUSD"]},
        {"event": "subscribe", "feed": "ticker", "product_ids": ["PF_XBTUSD"]}]),
    "bitstamp": dict(g="x1", url="wss://ws.bitstamp.net", subs=[
        {"event": "bts:subscribe", "data": {"channel": "live_trades_btcusd"}},
        {"event": "bts:subscribe", "data": {"channel": "order_book_btcusd"}}],
        reply=bitstamp_reply),
    "bitfinex": dict(g="x1", url="wss://api-pub.bitfinex.com/ws/2", subs=[
        {"event": "conf", "flags": 32768},
        {"event": "subscribe", "channel": "trades", "symbol": "tBTCUSD"},
        {"event": "subscribe", "channel": "book", "symbol": "tBTCUSD", "prec": "P0", "freq": "F0", "len": "1"},
        {"event": "subscribe", "channel": "trades", "symbol": "tBTCF0:USTF0"},
        {"event": "subscribe", "channel": "book", "symbol": "tBTCF0:USTF0", "prec": "P0", "freq": "F0", "len": "1"}]),
    "gemini": dict(g="x1", url="wss://api.gemini.com/v1/marketdata/BTCUSD?top_of_book=true&trades=true&heartbeat=true"),
    "hyperliquid": dict(g="x1", url="wss://api.hyperliquid.xyz/ws", subs=[
        {"method": "subscribe", "subscription": {"type": "trades", "coin": "BTC"}},
        {"method": "subscribe", "subscription": {"type": "bbo", "coin": "BTC"}},
        {"method": "subscribe", "subscription": {"type": "l2Book", "coin": "BTC"}}],
        ping=('{"method":"ping"}', 30)),
    "rtds_cl": dict(g="x1", url="wss://ws-live-data.polymarket.com", subs=[RTDS_CL], ping=("PING", 5), wsping=None),
    "rtds_bn": dict(g="x1", url="wss://ws-live-data.polymarket.com", subs=[RTDS_BN], ping=("PING", 5), wsping=None),
    "upbit": dict(g="x2", url="wss://api.upbit.com/websocket/v1", subs=[json.dumps([
        {"ticket": "rec-upbit"}, {"type": "trade", "codes": ["KRW-BTC", "KRW-USDT", "USDT-BTC"]},
        {"type": "orderbook", "codes": ["KRW-BTC.1", "KRW-USDT.1", "USDT-BTC.1"]}, {"format": "DEFAULT"}])]),
    "bithumb": dict(g="x2", url="wss://ws-api.bithumb.com/websocket/v1", subs=[json.dumps([
        {"ticket": "rec-bithumb"}, {"type": "trade", "codes": ["KRW-BTC", "KRW-USDT"]},
        {"type": "orderbook", "codes": ["KRW-BTC.1", "KRW-USDT.1"]}, {"format": "DEFAULT"}])]),
    "bitget": dict(g="x2", url="wss://ws.bitget.com/v2/ws/public", subs=[{"op": "subscribe", "args": [
        {"instType": "SPOT", "channel": "trade", "instId": "BTCUSDT"},
        {"instType": "SPOT", "channel": "books1", "instId": "BTCUSDT"},
        {"instType": "USDT-FUTURES", "channel": "trade", "instId": "BTCUSDT"},
        {"instType": "USDT-FUTURES", "channel": "books1", "instId": "BTCUSDT"}]}], ping=("ping", 25)),
    "gate": dict(g="x2", url="wss://api.gateio.ws/ws/v4/", subs=lambda: [
        {"time": int(time.time()), "channel": "spot.trades", "event": "subscribe", "payload": ["BTC_USDT"]},
        {"time": int(time.time()), "channel": "spot.book_ticker", "event": "subscribe", "payload": ["BTC_USDT"]}],
        ping=(lambda: json.dumps({"time": int(time.time()), "channel": "spot.ping"}), 20)),
    "gate_fut": dict(g="x2", url="wss://fx-ws.gateio.ws/v4/ws/usdt", subs=lambda: [
        {"time": int(time.time()), "channel": "futures.trades", "event": "subscribe", "payload": ["BTC_USDT"]},
        {"time": int(time.time()), "channel": "futures.book_ticker", "event": "subscribe", "payload": ["BTC_USDT"]}],
        ping=(lambda: json.dumps({"time": int(time.time()), "channel": "futures.ping"}), 20)),
    "kucoin": dict(g="x2", url=kucoin_url("https://api.kucoin.com/api/v1/bullet-public"), subs=[
        {"id": "1", "type": "subscribe", "topic": "/market/match:BTC-USDT", "privateChannel": False, "response": True},
        {"id": "2", "type": "subscribe", "topic": "/spotMarket/level1:BTC-USDT", "privateChannel": False,
         "response": True}], ping=(lambda: json.dumps({"id": str(_ms()), "type": "ping"}), 15)),
    "kucoin_fut": dict(g="x2", url=kucoin_url("https://api-futures.kucoin.com/api/v1/bullet-public"), subs=[
        {"id": "1", "type": "subscribe", "topic": "/contractMarket/execution:XBTUSDTM", "privateChannel": False,
         "response": True},
        {"id": "2", "type": "subscribe", "topic": "/contractMarket/tickerV2:XBTUSDTM", "privateChannel": False,
         "response": True}], ping=(lambda: json.dumps({"id": str(_ms()), "type": "ping"}), 15)),
    "mexc": dict(g="x2", url="wss://wbs-api.mexc.com/ws", dec="b64", subs=[{"method": "SUBSCRIPTION", "params": [
        "spot@public.aggre.deals.v3.api.pb@10ms@BTCUSDT", "spot@public.aggre.bookTicker.v3.api.pb@10ms@BTCUSDT"]}],
        ping=('{"method":"PING"}', 20)),
    "mexc_fut": dict(g="x2", url="wss://contract.mexc.com/edge", subs=[
        {"method": "sub.deal", "param": {"symbol": "BTC_USDT"}},
        {"method": "sub.depth.full", "param": {"symbol": "BTC_USDT", "limit": 5}}], ping=('{"method":"ping"}', 15)),
    "cryptocom": dict(g="x2", url="wss://stream.crypto.com/exchange/v1/market", delay=1.0, reply=cryptocom_reply, subs=[
        {"id": 1, "method": "subscribe", "params": {"channels": [
            "trade.BTC_USD", "ticker.BTC_USD", "trade.BTCUSD-PERP", "ticker.BTCUSD-PERP"]}, "nonce": 1}]),
    "htx": dict(g="x2", url="wss://api-aws.huobi.pro/ws", dec="gzip", reply=htx_pong, subs=[
        {"sub": "market.btcusdt.trade.detail", "id": "t"}, {"sub": "market.btcusdt.bbo", "id": "b"}]),
    "htx_swap": dict(g="x2", url="wss://api.hbdm.com/linear-swap-ws", dec="gzip", reply=htx_pong, subs=[
        {"sub": "market.BTC-USDT.trade.detail", "id": "t"}, {"sub": "market.BTC-USDT.bbo", "id": "b"}]),
    # ---- v3 (group x3) ----
    # Binance USD-M liquidation snapshots (at most one per symbol per 1000 ms): BTCUSDT plus the all-market stream
    # (market-wide stress; also keeps the link busy, so a silent BTC hour is not mistaken for a dead link).
    "bn_fut_liq": dict(g="x3", url="wss://fstream.binance.com/market/stream?streams=btcusdt@forceOrder/!forceOrder@arr",
                       idle=300),
    # Binance USD-M BTCUSDT mark price, index price, funding rate and next funding time, 1 frame per second.
    "bn_fut_mark": dict(g="x3", url="wss://fstream.binance.com/market/stream?streams=btcusdt@markPrice@1s"),
    # Deribit DVOL (BTC 30-day implied volatility index, ~1/s) and BTC-PERPETUAL ticker (open interest, funding,
    # mark/index, ~1/s on the agg2 interval).
    "deribit_vol": dict(g="x3", url="wss://www.deribit.com/ws/api/v2", subs=[
        {"jsonrpc": "2.0", "id": 1, "method": "public/set_heartbeat", "params": {"interval": 30}},
        {"jsonrpc": "2.0", "id": 2, "method": "public/subscribe", "params": {"channels": [
            "deribit_volatility_index.btc_usd", "ticker.BTC-PERPETUAL.agg2"]}}],
        reply=deribit_reply),
    # OKX liquidation orders (all SWAP contracts; filter instId later), BTC-USDT-SWAP open interest and funding rate.
    "okx_liq": dict(g="x3", url="wss://ws.okx.com:8443/ws/v5/public", subs=[{"op": "subscribe", "args": [
        {"channel": "liquidation-orders", "instType": "SWAP"},
        {"channel": "open-interest", "instId": "BTC-USDT-SWAP"},
        {"channel": "funding-rate", "instId": "BTC-USDT-SWAP"}]}], ping=("ping", 20)),
    # Bybit linear BTCUSDT: every liquidation (allLiquidation) and the ticker (open interest, funding, mark/index).
    "bybit_liq": dict(g="x3", url="wss://stream.bybit.com/v5/public/linear", subs=[
        {"op": "subscribe", "args": ["allLiquidation.BTCUSDT", "tickers.BTCUSDT"]}], ping=('{"op":"ping"}', 15)),
}
GROUPS = sorted({spec["g"] for spec in NEW.values()})


async def pinger_new(ws, msg, every):
    while True:
        await asyncio.sleep(every)
        await ws.send(msg() if callable(msg) else msg)


def _text(raw, dec):
    if isinstance(raw, str):
        text = raw
    elif dec == "gzip":
        text = gzip.decompress(raw).decode()
    elif dec == "b64":
        return "b64:" + base64.b64encode(raw).decode()
    else:
        text = raw.decode()
    if "\n" in text or "\r" in text:  # keep one frame per line (JSON whitespace only)
        text = text.replace("\r", " ").replace("\n", " ")
    return text


async def ws_session(src, spec, sink, cnt, st):
    url = spec["url"]
    if callable(url):
        url = await url()
    async with websockets.connect(url, max_size=None, open_timeout=10, compression=None,
                                  ping_interval=spec.get("wsping", 20)) as ws:
        if spec.get("delay"):
            await asyncio.sleep(spec["delay"])
        subs = spec.get("subs") or []
        for m in (subs() if callable(subs) else subs):
            await ws.send(m if isinstance(m, str) else json.dumps(m))
        epoch = time.time_ns()
        sink.lifecycle(epoch, "connection", epoch, source=src)
        pt = asyncio.create_task(pinger_new(ws, *spec["ping"])) if spec.get("ping") else None
        st["fails"] = 0
        dec, reply, idle = spec.get("dec"), spec.get("reply"), spec.get("idle", 60)
        try:
            while time.time() < END:
                raw = await asyncio.wait_for(ws.recv(), timeout=idle)
                ns = time.time_ns()
                text = _text(raw, dec)
                sink.write(ns, text)
                cnt[src] += 1
                if reply:
                    out = reply(text)
                    if out:
                        await ws.send(out)
        finally:
            if pt:
                pt.cancel()
            sink.lifecycle(time.time_ns(), "disconnect", epoch, source=src)


def _close_and_exit(*_):  # SIGTERM (systemd stop/restart, reboot) or orphaned child: finish every gzip member, exit
    for sk in SINKS:
        try:
            sk.close()
        except Exception:
            pass
    os._exit(0)


async def run_new(src, cnt, err, stagger=0.0):
    spec = NEW[src]
    sink = Sink(src)
    st = {"fails": 0}
    await asyncio.sleep(stagger)  # spread the opening TLS handshakes
    try:
        while time.time() < END:
            try:
                await ws_session(src, spec, sink, cnt, st)
            except Exception as exc:
                if time.time() >= END:
                    break
                err[src] += 1
                st["fails"] += 1
                print(f"{time.strftime('%H:%M:%S')} {src} error {type(exc).__name__}: {str(exc)[:150]}", flush=True)
                await asyncio.sleep(min(30, 0.5 * 2 ** min(st["fails"], 6)))
    finally:
        sink.close()


async def child_main(group, names):
    os.makedirs(OUT, exist_ok=True)
    cnt, err = {k: 0 for k in names}, {k: 0 for k in names}

    parent = os.getppid()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, _close_and_exit)

    async def rep():
        last = time.time()
        while time.time() < END:
            await asyncio.sleep(5)
            if os.getppid() != parent:  # supervisor gone: do not linger as an orphan
                _close_and_exit()
            if time.time() - last >= 60:
                last += 60
                print(f"{time.strftime('%H:%M:%S')} [{group}] counts {json.dumps(cnt)} errors {json.dumps(err)}",
                      flush=True)

    await asyncio.gather(rep(), *(run_new(s, cnt, err, 0.3 * i) for i, s in enumerate(names)))


async def supervise(group):
    """Start and keep restarting one child group; never raises, so it cannot stop the original sources."""
    fails = 0
    while time.time() < END:
        proc = None
        try:
            hours = max((END - time.time()) / 3600, 0.0)
            proc = await asyncio.create_subprocess_exec(sys.executable, os.path.abspath(__file__), OUT, f"{hours:.6f}",
                                                        "--group", group)
            t0 = time.time()
            rc = await proc.wait()
            proc = None
            if time.time() >= END:
                break
            fails = fails + 1 if time.time() - t0 < 600 else 1
            print(f"{time.strftime('%H:%M:%S')} group {group} exited rc={rc}; restart", flush=True)
        except asyncio.CancelledError:
            if proc and proc.returncode is None:
                proc.terminate()
            raise
        except Exception as exc:
            fails += 1
            print(f"{time.strftime('%H:%M:%S')} group {group} supervisor error {type(exc).__name__}: {str(exc)[:150]}",
                  flush=True)
        await asyncio.sleep(min(120, 5 * 2 ** min(fails, 5)))


async def main():
    os.makedirs(OUT, exist_ok=True)
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, _close_and_exit)
    await asyncio.gather(report(), *(run(s) for s in SOURCES), *(supervise(g) for g in GROUPS))


if __name__ == "__main__":
    if "--group" in sys.argv:
        _g = sys.argv[sys.argv.index("--group") + 1]
        _names = (sys.argv[sys.argv.index("--sources") + 1].split(",") if "--sources" in sys.argv
                  else [s for s, spec in NEW.items() if spec["g"] == _g])
        asyncio.run(child_main(_g, _names))
    else:
        asyncio.run(main())
