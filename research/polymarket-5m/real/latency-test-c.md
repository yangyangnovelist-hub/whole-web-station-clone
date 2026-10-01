# 检验 C：coinbase 触发的过期报价（btc，GitHub 前向录制）

事先写死：2026-09-30 11:00 UTC 起开始的 5m 市场；coinbase 逐笔成交在剩 240–60 秒时第一次相对至少一秒前涨跌超过 3σ，0.3 秒后按交易所时间戳盘口的卖一买顺势一方，付 taker 费，持有到结算；按触发时间取前 1,500 笔判定一次，EV > 0 且精确 p < 0.025 才算通过。每段录制单独计算（σ 不跨越 10 秒以上的空档，参考价不早于 5 秒），下单时刻前 30 秒内和后 10 秒内盘口都要有更新。

| 币种 | 录制段 | 触发（按 L = 检验值） |
|---|---:|---:|
| btc | 7 | 299 |

录制段备注（跳过的和断线检查）：

- btc 36671472407: skipped (FileNotFoundError: no COINBASE_TRADE events under /home/runner/work/_temp/rec/36671472407/x/bundle-btc/latency)
- btc 36704245701: 1 candidates dropped because the CLOB socket closed between the quote and the order (25 disconnects in the recording; test C counts its judged lag only)
- btc 36740728504: 2 candidates dropped because the CLOB socket closed between the quote and the order (33 disconnects in the recording; test C counts its judged lag only)

| L | 笔数 | 胜率 | 平均价 | EV | p | 卖一数量中位 |
|---:|---|---|---|---|---|---|
| 0 秒 | 300 | 55.3% | 0.540 | +0.06¢ | 0.5214 | 72 |
| 0.1 秒 | 299 | 55.5% | 0.550 | -0.80¢ | 1.0000 | 53 |
| 0.2 秒 | 299 | 55.5% | 0.562 | -1.95¢ | 1.0000 | 50 |
| 0.3 秒 | 299 | 55.5% | 0.576 | -3.35¢ | 1.0000 | 74 |
| 0.4 秒 | 299 | 55.5% | 0.586 | -4.33¢ | 1.0000 | 108 |
| 0.5 秒 | 298 | 55.4% | 0.588 | -4.68¢ | 1.0000 | 164 |
| 1 秒 | 297 | 54.9% | 0.589 | -5.29¢ | 1.0000 | 328 |
| 2 秒 | 295 | 54.9% | 0.587 | -5.03¢ | 1.0000 | 445 |
| 5 秒 | 295 | 54.6% | 0.575 | -4.20¢ | 1.0000 | 342 |

检验 C（前 1,500 笔）：目前 299 笔，不到 1,500 笔，不判定。
