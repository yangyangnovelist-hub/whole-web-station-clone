# 检验 C：coinbase 触发的过期报价（btc，GitHub 前向录制）

事先写死：2026-09-30 11:00 UTC 起开始的 5m 市场；coinbase 逐笔成交在剩 240–60 秒时第一次相对至少一秒前涨跌超过 3σ，0.3 秒后按交易所时间戳盘口的卖一买顺势一方，付 taker 费，持有到结算；按触发时间取前 1,500 笔判定一次，EV > 0 且精确 p < 0.025 才算通过。每段录制单独计算（σ 不跨越 10 秒以上的空档，参考价不早于 5 秒），下单时刻前 30 秒内和后 10 秒内盘口都要有更新。

| 币种 | 录制段 | 触发（按 L = 检验值） |
|---|---:|---:|
| btc | 14 | 626 |

录制段备注（跳过的和断线检查）：

- btc 36671472407: skipped (FileNotFoundError: no COINBASE_TRADE events under /home/runner/work/_temp/rec/36671472407/x/bundle-btc/latency)
- btc 36704245701: 1 candidates dropped because the CLOB socket closed between the quote and the order (25 disconnects in the recording; test C counts its judged lag only)
- btc 36740728504: 2 candidates dropped because the CLOB socket closed between the quote and the order (33 disconnects in the recording; test C counts its judged lag only)
- btc 37002779006: 3 candidates dropped because the CLOB socket closed between the quote and the order (107 disconnects in the recording; test C counts its judged lag only)
- btc 37041041140: 4 candidates dropped because the CLOB socket closed between the quote and the order (70 disconnects in the recording; test C counts its judged lag only)
- btc 37075141917: 5 candidates dropped because the CLOB socket closed between the quote and the order (80 disconnects in the recording; test C counts its judged lag only)
- btc 37095301195: 10 candidates dropped because the CLOB socket closed between the quote and the order (76 disconnects in the recording; test C counts its judged lag only)

| L | 笔数 | 胜率 | 平均价 | EV | p | 卖一数量中位 |
|---:|---|---|---|---|---|---|
| 0 秒 | 633 | 58.0% | 0.556 | +1.06¢ | 0.2889 | 86 |
| 0.1 秒 | 629 | 58.0% | 0.567 | +0.07¢ | 0.5050 | 60 |
| 0.2 秒 | 626 | 58.1% | 0.579 | -1.06¢ | 1.0000 | 53 |
| 0.3 秒 | 626 | 58.1% | 0.594 | -2.52¢ | 1.0000 | 74 |
| 0.4 秒 | 625 | 58.1% | 0.602 | -3.37¢ | 1.0000 | 98 |
| 0.5 秒 | 624 | 58.0% | 0.605 | -3.79¢ | 1.0000 | 147 |
| 1 秒 | 620 | 57.7% | 0.607 | -4.18¢ | 1.0000 | 290 |
| 2 秒 | 617 | 57.9% | 0.605 | -3.87¢ | 1.0000 | 373 |
| 5 秒 | 615 | 57.7% | 0.597 | -3.22¢ | 1.0000 | 349 |

检验 C（前 1,500 笔）：目前 626 笔，不到 1,500 笔，不判定。
