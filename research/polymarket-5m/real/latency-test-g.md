# 检验 G：检验 F 加“Polymarket 还没动”的条件（BTC 5m，币安逐笔成交触发，GitHub 前向录制）

事先写死（10 月 1 日 02:00 UTC，数据还没录）：2026-10-01 02:30 UTC 起开始的 BTC 5m 市场；规则同检验 F（币安逐笔成交 2σ 触发、0.3 秒后按卖一、公平价 − 卖一 − 手续费 ≥ 12¢、每个市场第一笔、持有到结算），另外：触发前 2 秒内 Up 中间价已经朝要买的方向动了 3¢ 或更多（或 2 秒前没有报价）就跳过这个候选。这个条件在 5 月 25 日–7 月 15 日上定（没动的 +5.5¢、已动的 +0.4¢），之后两段核对（B +7.8/+6.1¢，C +13.1/+3.1¢）。按触发时间取前 600 笔判定一次，EV > 0 且精确 p < 0.025 才算通过。数据处理同检验 C、D、F。

录制段 17 个，成交 180 笔。

录制段备注（跳过的和断线检查）：

- btc 36671472407: skipped (FileNotFoundError: no BINANCE_WS_TRADE events under /home/runner/work/_temp/rec/36671472407/x/bundle-btc/latency)
- btc 36704245701: skipped (FileNotFoundError: no BINANCE_WS_TRADE events under /home/runner/work/_temp/rec/36704245701/x/bundle-btc/latency)
- btc 36740728504: skipped (FileNotFoundError: no BINANCE_WS_TRADE events under /home/runner/work/_temp/rec/36740728504/x/bundle-btc/latency)
- btc 36781277830: no markets from 2026-10-01 02:30 with book and spot data
- btc 36869869857: 18 candidates dropped because the CLOB socket closed between the quote and the order (3 disconnects in the recording; test C counts its judged lag only)
- btc 36972067946: 136 candidates dropped because the CLOB socket closed between the quote and the order (15 disconnects in the recording; test C counts its judged lag only)
- btc 37002779006: 1564 candidates dropped because the CLOB socket closed between the quote and the order (107 disconnects in the recording; test C counts its judged lag only)
- btc 37041041140: 544 candidates dropped because the CLOB socket closed between the quote and the order (70 disconnects in the recording; test C counts its judged lag only)
- btc 37075141917: 555 candidates dropped because the CLOB socket closed between the quote and the order (80 disconnects in the recording; test C counts its judged lag only)
- btc 37095301195: 1007 candidates dropped because the CLOB socket closed between the quote and the order (76 disconnects in the recording; test C counts its judged lag only)
- btc 37111395430: 388 candidates dropped because the CLOB socket closed between the quote and the order (77 disconnects in the recording; test C counts its judged lag only)
- btc 37128550183: 308 candidates dropped because the CLOB socket closed between the quote and the order (58 disconnects in the recording; test C counts its judged lag only)
- btc 37146417247: 1288 candidates dropped because the CLOB socket closed between the quote and the order (102 disconnects in the recording; test C counts its judged lag only)

| 笔数 | 胜率 | 平均价 | EV | p | 卖一数量中位 |
|---|---|---|---|---|---|
| 180 | 50.6% | 0.406 | +8.72¢ | 0.0039 | 48 |

检验 G（前 600 笔）：目前 180 笔，不到 600 笔，不判定。
