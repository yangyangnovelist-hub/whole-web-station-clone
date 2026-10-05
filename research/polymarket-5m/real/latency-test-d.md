# 检验 D：按公平价跳变筛选的过期报价（btc，coinbase 触发，GitHub 前向录制）

事先写死（9 月 30 日 13:40 UTC，数据还没看过）：2026-09-30 14:00 UTC 起开始的 5m 市场；剩 240–15 秒时，与至少一秒前（不早于 5 秒）相比涨跌超过 2σ 的每一笔 coinbase 成交都是候选；以那一刻交易所显示的 Up 中间价为原来的概率，这次涨跌让结算 TWAP 的期望整体移动，算出新的公平价；0.3 秒后按卖一买顺势一方，只在 公平价 − 卖一 − taker 费 ≥ 12¢ 时成交（相当于在触发时下一张成交不了就取消的限价单），每个市场取第一笔，持有到结算；按触发时间取前 1,200 笔判定一次，EV > 0 且精确 p < 0.025 才算通过。数据处理同检验 C。

录制段 22 个，成交 605 笔。

录制段备注（跳过的和断线检查）：

- btc 36671472407: skipped (FileNotFoundError: no COINBASE_TRADE events under /home/runner/work/_temp/rec/36671472407/x/bundle-btc/latency)
- btc 36740728504: 43 candidates dropped because the CLOB socket closed between the quote and the order (33 disconnects in the recording; test C counts its judged lag only)
- btc 36807987110: 3 candidates dropped because the CLOB socket closed between the quote and the order (1 disconnects in the recording; test C counts its judged lag only)
- btc 36869869857: 5 candidates dropped because the CLOB socket closed between the quote and the order (3 disconnects in the recording; test C counts its judged lag only)
- btc 36972067946: 25 candidates dropped because the CLOB socket closed between the quote and the order (15 disconnects in the recording; test C counts its judged lag only)
- btc 37002779006: 457 candidates dropped because the CLOB socket closed between the quote and the order (107 disconnects in the recording; test C counts its judged lag only)
- btc 37041041140: 99 candidates dropped because the CLOB socket closed between the quote and the order (70 disconnects in the recording; test C counts its judged lag only)
- btc 37075141917: 88 candidates dropped because the CLOB socket closed between the quote and the order (80 disconnects in the recording; test C counts its judged lag only)
- btc 37095301195: 292 candidates dropped because the CLOB socket closed between the quote and the order (76 disconnects in the recording; test C counts its judged lag only)
- btc 37111395430: 190 candidates dropped because the CLOB socket closed between the quote and the order (77 disconnects in the recording; test C counts its judged lag only)
- btc 37128550183: 89 candidates dropped because the CLOB socket closed between the quote and the order (58 disconnects in the recording; test C counts its judged lag only)
- btc 37146417247: 336 candidates dropped because the CLOB socket closed between the quote and the order (102 disconnects in the recording; test C counts its judged lag only)
- btc 37164037946: 133 candidates dropped because the CLOB socket closed between the quote and the order (61 disconnects in the recording; test C counts its judged lag only)
- btc 37179397738: 127 candidates dropped because the CLOB socket closed between the quote and the order (70 disconnects in the recording; test C counts its judged lag only)
- btc 37194238626: 379 candidates dropped because the CLOB socket closed between the quote and the order (78 disconnects in the recording; test C counts its judged lag only)
- btc 37230483105: 590 candidates dropped because the CLOB socket closed between the quote and the order (179 disconnects in the recording; test C counts its judged lag only)
- btc 37250413580: 380 candidates dropped because the CLOB socket closed between the quote and the order (161 disconnects in the recording; test C counts its judged lag only)

| 笔数 | 胜率 | 平均价 | EV | p | 卖一数量中位 |
|---|---|---|---|---|---|
| 605 | 39.2% | 0.400 | -2.18¢ | 1.0000 | 100 |

检验 D（前 1,200 笔）：目前 605 笔，不到 1,200 笔，不判定。
