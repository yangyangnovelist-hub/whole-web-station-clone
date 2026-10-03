# 检验 C：coinbase 触发的过期报价（btc，GitHub 前向录制）

事先写死：2026-09-30 11:00 UTC 起开始的 5m 市场；coinbase 逐笔成交在剩 240–60 秒时第一次相对至少一秒前涨跌超过 3σ，0.3 秒后按交易所时间戳盘口的卖一买顺势一方，付 taker 费，持有到结算；按触发时间取前 1,500 笔判定一次，EV > 0 且精确 p < 0.025 才算通过。每段录制单独计算（σ 不跨越 10 秒以上的空档，参考价不早于 5 秒），下单时刻前 30 秒内和后 10 秒内盘口都要有更新。

| 币种 | 录制段 | 触发（按 L = 检验值） |
|---|---:|---:|
| btc | 13 | 587 |

录制段备注（跳过的和断线检查）：

- btc 36671472407: skipped (FileNotFoundError: no COINBASE_TRADE events under /home/runner/work/_temp/rec/36671472407/x/bundle-btc/latency)
- btc 36704245701: 1 candidates dropped because the CLOB socket closed between the quote and the order (25 disconnects in the recording; test C counts its judged lag only)
- btc 36740728504: 2 candidates dropped because the CLOB socket closed between the quote and the order (33 disconnects in the recording; test C counts its judged lag only)
- btc 37002779006: 3 candidates dropped because the CLOB socket closed between the quote and the order (107 disconnects in the recording; test C counts its judged lag only)
- btc 37041041140: 4 candidates dropped because the CLOB socket closed between the quote and the order (70 disconnects in the recording; test C counts its judged lag only)
- btc 37075141917: 5 candidates dropped because the CLOB socket closed between the quote and the order (80 disconnects in the recording; test C counts its judged lag only)

| L | 笔数 | 胜率 | 平均价 | EV | p | 卖一数量中位 |
|---:|---|---|---|---|---|---|
| 0 秒 | 591 | 57.4% | 0.549 | +1.14¢ | 0.2798 | 86 |
| 0.1 秒 | 589 | 57.4% | 0.559 | +0.21¢ | 0.4769 | 60 |
| 0.2 秒 | 587 | 57.4% | 0.571 | -0.99¢ | 1.0000 | 53 |
| 0.3 秒 | 587 | 57.4% | 0.586 | -2.48¢ | 1.0000 | 75 |
| 0.4 秒 | 586 | 57.3% | 0.594 | -3.35¢ | 1.0000 | 100 |
| 0.5 秒 | 585 | 57.3% | 0.597 | -3.75¢ | 1.0000 | 148 |
| 1 秒 | 582 | 57.0% | 0.599 | -4.12¢ | 1.0000 | 284 |
| 2 秒 | 579 | 57.2% | 0.597 | -3.78¢ | 1.0000 | 381 |
| 5 秒 | 578 | 57.1% | 0.590 | -3.13¢ | 1.0000 | 379 |

检验 C（前 1,500 笔）：目前 587 笔，不到 1,500 笔，不判定。
