# 检验 C：coinbase 触发的过期报价（btc，GitHub 前向录制）

事先写死：2026-09-30 11:00 UTC 起开始的 5m 市场；coinbase 逐笔成交在剩 240–60 秒时第一次相对至少一秒前涨跌超过 3σ，0.3 秒后按交易所时间戳盘口的卖一买顺势一方，付 taker 费，持有到结算；按触发时间取前 1,500 笔判定一次，EV > 0 且精确 p < 0.025 才算通过。每段录制单独计算（σ 不跨越 10 秒以上的空档，参考价不早于 5 秒），下单时刻前 30 秒内和后 10 秒内盘口都要有更新。

| 币种 | 录制段 | 触发（按 L = 检验值） |
|---|---:|---:|
| btc | 28 | 1,164 |

录制段备注（跳过的和断线检查）：

- btc 36671472407: skipped (FileNotFoundError: no COINBASE_TRADE events under /home/runner/work/_temp/rec/36671472407/x/bundle-btc/latency)
- btc 36704245701: 1 candidates dropped because the CLOB socket closed between the quote and the order (25 disconnects in the recording; test C counts its judged lag only)
- btc 36740728504: 2 candidates dropped because the CLOB socket closed between the quote and the order (33 disconnects in the recording; test C counts its judged lag only)
- btc 37002779006: 3 candidates dropped because the CLOB socket closed between the quote and the order (107 disconnects in the recording; test C counts its judged lag only)
- btc 37041041140: 4 candidates dropped because the CLOB socket closed between the quote and the order (70 disconnects in the recording; test C counts its judged lag only)
- btc 37075141917: 5 candidates dropped because the CLOB socket closed between the quote and the order (80 disconnects in the recording; test C counts its judged lag only)
- btc 37095301195: 10 candidates dropped because the CLOB socket closed between the quote and the order (76 disconnects in the recording; test C counts its judged lag only)
- btc 37111395430: 8 candidates dropped because the CLOB socket closed between the quote and the order (77 disconnects in the recording; test C counts its judged lag only)
- btc 37128550183: 3 candidates dropped because the CLOB socket closed between the quote and the order (58 disconnects in the recording; test C counts its judged lag only)
- btc 37146417247: 4 candidates dropped because the CLOB socket closed between the quote and the order (102 disconnects in the recording; test C counts its judged lag only)
- btc 37164037946: 3 candidates dropped because the CLOB socket closed between the quote and the order (61 disconnects in the recording; test C counts its judged lag only)
- btc 37179397738: 3 candidates dropped because the CLOB socket closed between the quote and the order (70 disconnects in the recording; test C counts its judged lag only)
- btc 37194238626: 7 candidates dropped because the CLOB socket closed between the quote and the order (78 disconnects in the recording; test C counts its judged lag only)
- btc 37230483105: 8 candidates dropped because the CLOB socket closed between the quote and the order (179 disconnects in the recording; test C counts its judged lag only)
- btc 37250413580: 11 candidates dropped because the CLOB socket closed between the quote and the order (161 disconnects in the recording; test C counts its judged lag only)
- btc 37274274070: 9 candidates dropped because the CLOB socket closed between the quote and the order (214 disconnects in the recording; test C counts its judged lag only)
- btc 37307261464: 7 candidates dropped because the CLOB socket closed between the quote and the order (182 disconnects in the recording; test C counts its judged lag only)
- btc 37347643542: 18 candidates dropped because the CLOB socket closed between the quote and the order (249 disconnects in the recording; test C counts its judged lag only)
- btc 37384144976: 7 candidates dropped because the CLOB socket closed between the quote and the order (157 disconnects in the recording; test C counts its judged lag only)
- btc 37412793391: 12 candidates dropped because the CLOB socket closed between the quote and the order (219 disconnects in the recording; test C counts its judged lag only)
- btc 37444926603: 10 candidates dropped because the CLOB socket closed between the quote and the order (244 disconnects in the recording; test C counts its judged lag only)

| L | 笔数 | 胜率 | 平均价 | EV | p | 卖一数量中位 |
|---:|---|---|---|---|---|---|
| 0 秒 | 1,210 | 59.6% | 0.569 | +1.40¢ | 0.1286 | 107 |
| 0.1 秒 | 1,185 | 59.6% | 0.580 | +0.39¢ | 0.3899 | 72 |
| 0.2 秒 | 1,170 | 59.5% | 0.592 | -0.96¢ | 1.0000 | 60 |
| 0.3 秒 | 1,164 | 59.4% | 0.607 | -2.60¢ | 1.0000 | 79 |
| 0.4 秒 | 1,159 | 59.3% | 0.614 | -3.31¢ | 1.0000 | 101 |
| 0.5 秒 | 1,152 | 59.2% | 0.617 | -3.74¢ | 1.0000 | 133 |
| 1 秒 | 1,138 | 59.2% | 0.620 | -4.00¢ | 1.0000 | 256 |
| 2 秒 | 1,127 | 59.3% | 0.620 | -3.89¢ | 1.0000 | 341 |
| 5 秒 | 1,098 | 59.3% | 0.617 | -3.55¢ | 1.0000 | 337 |

检验 C（前 1,500 笔）：目前 1,164 笔，不到 1,500 笔，不判定。
