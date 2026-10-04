# 检验 C：coinbase 触发的过期报价（btc，GitHub 前向录制）

事先写死：2026-09-30 11:00 UTC 起开始的 5m 市场；coinbase 逐笔成交在剩 240–60 秒时第一次相对至少一秒前涨跌超过 3σ，0.3 秒后按交易所时间戳盘口的卖一买顺势一方，付 taker 费，持有到结算；按触发时间取前 1,500 笔判定一次，EV > 0 且精确 p < 0.025 才算通过。每段录制单独计算（σ 不跨越 10 秒以上的空档，参考价不早于 5 秒），下单时刻前 30 秒内和后 10 秒内盘口都要有更新。

| 币种 | 录制段 | 触发（按 L = 检验值） |
|---|---:|---:|
| btc | 18 | 787 |

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

| L | 笔数 | 胜率 | 平均价 | EV | p | 卖一数量中位 |
|---:|---|---|---|---|---|---|
| 0 秒 | 803 | 58.8% | 0.561 | +1.44¢ | 0.1789 | 100 |
| 0.1 秒 | 794 | 58.7% | 0.570 | +0.49¢ | 0.3917 | 69 |
| 0.2 秒 | 788 | 58.9% | 0.583 | -0.67¢ | 1.0000 | 60 |
| 0.3 秒 | 787 | 58.8% | 0.600 | -2.44¢ | 1.0000 | 77 |
| 0.4 秒 | 784 | 58.8% | 0.608 | -3.21¢ | 1.0000 | 96 |
| 0.5 秒 | 782 | 58.8% | 0.612 | -3.59¢ | 1.0000 | 135 |
| 1 秒 | 776 | 58.8% | 0.614 | -3.83¢ | 1.0000 | 258 |
| 2 秒 | 771 | 59.0% | 0.612 | -3.44¢ | 1.0000 | 358 |
| 5 秒 | 765 | 59.0% | 0.606 | -2.80¢ | 1.0000 | 333 |

检验 C（前 1,500 笔）：目前 787 笔，不到 1,500 笔，不判定。
