# 检验 C：coinbase 触发的过期报价（btc，GitHub 前向录制）

事先写死：2026-09-30 11:00 UTC 起开始的 5m 市场；coinbase 逐笔成交在剩 240–60 秒时第一次相对至少一秒前涨跌超过 3σ，0.3 秒后按交易所时间戳盘口的卖一买顺势一方，付 taker 费，持有到结算；按触发时间取前 1,500 笔判定一次，EV > 0 且精确 p < 0.025 才算通过。每段录制单独计算（σ 不跨越 10 秒以上的空档，参考价不早于 5 秒），下单时刻前 30 秒内和后 10 秒内盘口都要有更新。

| 币种 | 录制段 | 触发（按 L = 检验值） |
|---|---:|---:|
| btc | 5 | 199 |

录制段备注（跳过的和断线检查）：

- btc 36671472407: skipped (FileNotFoundError: no COINBASE_TRADE events under /home/runner/work/_temp/rec/36671472407/x/bundle-btc/latency)
- btc 36704245701: 1 candidates dropped because the CLOB socket closed between the quote and the order (25 disconnects in the recording; test C counts its judged lag only)
- btc 36740728504: 2 candidates dropped because the CLOB socket closed between the quote and the order (33 disconnects in the recording; test C counts its judged lag only)

| L | 笔数 | 胜率 | 平均价 | EV | p | 卖一数量中位 |
|---:|---|---|---|---|---|---|
| 0 秒 | 200 | 53.0% | 0.542 | -2.47¢ | 1.0000 | 74 |
| 0.1 秒 | 199 | 53.3% | 0.553 | -3.32¢ | 1.0000 | 49 |
| 0.2 秒 | 199 | 53.3% | 0.566 | -4.60¢ | 1.0000 | 48 |
| 0.3 秒 | 199 | 53.3% | 0.580 | -6.01¢ | 1.0000 | 68 |
| 0.4 秒 | 199 | 53.3% | 0.588 | -6.86¢ | 1.0000 | 93 |
| 0.5 秒 | 198 | 53.0% | 0.590 | -7.28¢ | 1.0000 | 160 |
| 1 秒 | 197 | 52.3% | 0.590 | -7.97¢ | 1.0000 | 337 |
| 2 秒 | 196 | 52.6% | 0.591 | -7.82¢ | 1.0000 | 425 |
| 5 秒 | 195 | 52.3% | 0.582 | -7.14¢ | 1.0000 | 412 |

检验 C（前 1,500 笔）：目前 199 笔，不到 1,500 笔，不判定。
