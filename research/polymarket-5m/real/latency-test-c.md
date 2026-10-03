# 检验 C：coinbase 触发的过期报价（btc，GitHub 前向录制）

事先写死：2026-09-30 11:00 UTC 起开始的 5m 市场；coinbase 逐笔成交在剩 240–60 秒时第一次相对至少一秒前涨跌超过 3σ，0.3 秒后按交易所时间戳盘口的卖一买顺势一方，付 taker 费，持有到结算；按触发时间取前 1,500 笔判定一次，EV > 0 且精确 p < 0.025 才算通过。每段录制单独计算（σ 不跨越 10 秒以上的空档，参考价不早于 5 秒），下单时刻前 30 秒内和后 10 秒内盘口都要有更新。

| 币种 | 录制段 | 触发（按 L = 检验值） |
|---|---:|---:|
| btc | 15 | 657 |

录制段备注（跳过的和断线检查）：

- btc 36671472407: skipped (FileNotFoundError: no COINBASE_TRADE events under /home/runner/work/_temp/rec/36671472407/x/bundle-btc/latency)
- btc 36704245701: 1 candidates dropped because the CLOB socket closed between the quote and the order (25 disconnects in the recording; test C counts its judged lag only)
- btc 36740728504: 2 candidates dropped because the CLOB socket closed between the quote and the order (33 disconnects in the recording; test C counts its judged lag only)
- btc 37002779006: 3 candidates dropped because the CLOB socket closed between the quote and the order (107 disconnects in the recording; test C counts its judged lag only)
- btc 37041041140: 4 candidates dropped because the CLOB socket closed between the quote and the order (70 disconnects in the recording; test C counts its judged lag only)
- btc 37075141917: 5 candidates dropped because the CLOB socket closed between the quote and the order (80 disconnects in the recording; test C counts its judged lag only)
- btc 37095301195: 10 candidates dropped because the CLOB socket closed between the quote and the order (76 disconnects in the recording; test C counts its judged lag only)
- btc 37111395430: 8 candidates dropped because the CLOB socket closed between the quote and the order (77 disconnects in the recording; test C counts its judged lag only)

| L | 笔数 | 胜率 | 平均价 | EV | p | 卖一数量中位 |
|---:|---|---|---|---|---|---|
| 0 秒 | 669 | 58.0% | 0.558 | +0.89¢ | 0.3114 | 90 |
| 0.1 秒 | 662 | 58.0% | 0.569 | -0.15¢ | 1.0000 | 61 |
| 0.2 秒 | 658 | 58.2% | 0.582 | -1.27¢ | 1.0000 | 53 |
| 0.3 秒 | 657 | 58.1% | 0.597 | -2.81¢ | 1.0000 | 75 |
| 0.4 秒 | 655 | 58.2% | 0.605 | -3.59¢ | 1.0000 | 98 |
| 0.5 秒 | 654 | 58.1% | 0.609 | -4.00¢ | 1.0000 | 146 |
| 1 秒 | 650 | 57.8% | 0.610 | -4.36¢ | 1.0000 | 285 |
| 2 秒 | 647 | 58.0% | 0.608 | -4.05¢ | 1.0000 | 368 |
| 5 秒 | 644 | 57.9% | 0.601 | -3.36¢ | 1.0000 | 346 |

检验 C（前 1,500 笔）：目前 657 笔，不到 1,500 笔，不判定。
