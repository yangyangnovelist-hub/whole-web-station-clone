# 币安行情在 GitHub 机器上能不能连（逐笔成交 websocket）

- wss://data-stream.binance.vision/ws/btcusdt@aggTrade: 28 条 / 20 秒，收到延迟中位 51 ms
  样例：`{"e":"aggTrade","E":1790804233459,"s":"BTCUSDT","a":4078006484,"p":"83816.04000000","q":"0.00328000","f":6726360126,"l":6726360126,"T":1790804233458,"m":false,"M":true}`
- wss://stream.binance.com:9443/ws/btcusdt@aggTrade: 连不上（InvalidStatus: server rejected WebSocket connection: HTTP 451）
- wss://fstream.binance.com/ws/btcusdt@aggTrade: 0 条 / 20 秒
- wss://stream.binance.us:9443/ws/btcusd@aggTrade: 1 条 / 20 秒，收到延迟中位 49 ms
  样例：`{"e":"aggTrade","E":1790804299211,"s":"BTCUSD","a":21057472,"p":"83748.98000000","q":"0.00058000","f":89907387,"l":89907387,"T":1790804299211,"m":false,"M":true}`

REST：
- https://data-api.binance.vision/api/v3/aggTrades?symbol=BTCUSDT&limit=3: [{"a":4078006791,"p":"83790.00000000","q":"0.00011000","f":6726361207,"l":6726361207,"T":1790804314227,"m":true,"M":true},{"a":4078006792,"p":"83790.01000000","
- https://api.binance.com/api/v3/aggTrades?symbol=BTCUSDT&limit=3: HTTPError: HTTP Error 451: 
