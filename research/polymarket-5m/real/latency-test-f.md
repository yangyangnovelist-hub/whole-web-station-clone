# 检验 F：按公平价跳变筛选的过期报价，币安逐笔成交触发（BTC 5m，GitHub 前向录制）

事先写死（9 月 30 日 21:42 UTC，数据还没录）：2026-09-30 23:00 UTC 起开始的 BTC 5m 市场；规则与检验 D 完全相同，只是触发改用回测用的币安 BTCUSDT 逐笔成交（data-stream.binance.vision，交易所时间）：剩 240–15 秒时，与至少一秒前（不早于 5 秒）相比涨跌超过 2σ 的每一笔币安成交是候选；以那一刻交易所显示的 Up 中间价为原来的概率算公平价；0.3 秒后按卖一买顺势一方，只在 公平价 − 卖一 − taker 费 ≥ 12¢ 时成交，每个市场取第一笔，持有到结算；按触发时间取前 1,200 笔判定一次，EV > 0 且精确 p < 0.025 才算通过。数据处理同检验 C、D（含断线检查）。

录制段 34 个，成交 825 笔。

录制段备注（跳过的和断线检查）：

- btc 36671472407: skipped (FileNotFoundError: no BINANCE_WS_TRADE events under /home/runner/work/_temp/rec/36671472407/x/bundle-btc/latency)
- btc 36704245701: skipped (FileNotFoundError: no BINANCE_WS_TRADE events under /home/runner/work/_temp/rec/36704245701/x/bundle-btc/latency)
- btc 36740728504: skipped (FileNotFoundError: no BINANCE_WS_TRADE events under /home/runner/work/_temp/rec/36740728504/x/bundle-btc/latency)
- btc 36869869857: 18 candidates dropped because the CLOB socket closed between the quote and the order (3 disconnects in the recording; test C counts its judged lag only)
- btc 36972067946: 42 candidates dropped because the CLOB socket closed between the quote and the order (15 disconnects in the recording; test C counts its judged lag only)
- btc 37002779006: 1552 candidates dropped because the CLOB socket closed between the quote and the order (107 disconnects in the recording; test C counts its judged lag only)
- btc 37041041140: 619 candidates dropped because the CLOB socket closed between the quote and the order (70 disconnects in the recording; test C counts its judged lag only)
- btc 37075141917: 451 candidates dropped because the CLOB socket closed between the quote and the order (80 disconnects in the recording; test C counts its judged lag only)
- btc 37095301195: 759 candidates dropped because the CLOB socket closed between the quote and the order (76 disconnects in the recording; test C counts its judged lag only)
- btc 37111395430: 431 candidates dropped because the CLOB socket closed between the quote and the order (77 disconnects in the recording; test C counts its judged lag only)
- btc 37128550183: 334 candidates dropped because the CLOB socket closed between the quote and the order (58 disconnects in the recording; test C counts its judged lag only)
- btc 37146417247: 1372 candidates dropped because the CLOB socket closed between the quote and the order (102 disconnects in the recording; test C counts its judged lag only)
- btc 37164037946: 332 candidates dropped because the CLOB socket closed between the quote and the order (61 disconnects in the recording; test C counts its judged lag only)
- btc 37179397738: 474 candidates dropped because the CLOB socket closed between the quote and the order (70 disconnects in the recording; test C counts its judged lag only)
- btc 37194238626: 804 candidates dropped because the CLOB socket closed between the quote and the order (78 disconnects in the recording; test C counts its judged lag only)
- btc 37230483105: 2868 candidates dropped because the CLOB socket closed between the quote and the order (179 disconnects in the recording; test C counts its judged lag only)
- btc 37250413580: 2607 candidates dropped because the CLOB socket closed between the quote and the order (161 disconnects in the recording; test C counts its judged lag only)
- btc 37274274070: 2021 candidates dropped because the CLOB socket closed between the quote and the order (214 disconnects in the recording; test C counts its judged lag only)
- btc 37307261464: 3486 candidates dropped because the CLOB socket closed between the quote and the order (182 disconnects in the recording; test C counts its judged lag only)
- btc 37347643542: 1942 candidates dropped because the CLOB socket closed between the quote and the order (249 disconnects in the recording; test C counts its judged lag only)
- btc 37384144976: 941 candidates dropped because the CLOB socket closed between the quote and the order (157 disconnects in the recording; test C counts its judged lag only)
- btc 37412793391: 2755 candidates dropped because the CLOB socket closed between the quote and the order (219 disconnects in the recording; test C counts its judged lag only)
- btc 37444926603: 2477 candidates dropped because the CLOB socket closed between the quote and the order (244 disconnects in the recording; test C counts its judged lag only)
- btc 37484528542: 2858 candidates dropped because the CLOB socket closed between the quote and the order (232 disconnects in the recording; test C counts its judged lag only)
- btc 37527655881: 799 candidates dropped because the CLOB socket closed between the quote and the order (120 disconnects in the recording; test C counts its judged lag only)
- btc 37558084377: 1901 candidates dropped because the CLOB socket closed between the quote and the order (184 disconnects in the recording; test C counts its judged lag only)
- btc 37586562398: 10351 candidates dropped because the CLOB socket closed between the quote and the order (289 disconnects in the recording; test C counts its judged lag only)
- btc 37621711008: 6532 candidates dropped because the CLOB socket closed between the quote and the order (356 disconnects in the recording; test C counts its judged lag only)
- btc 37663361030: 1609 candidates dropped because the CLOB socket closed between the quote and the order (260 disconnects in the recording; test C counts its judged lag only)

| 笔数 | 胜率 | 平均价 | EV | p | 卖一数量中位 |
|---|---|---|---|---|---|
| 825 | 45.8% | 0.404 | +4.09¢ | 0.0038 | 71 |

检验 F（前 1,200 笔）：目前 825 笔，不到 1,200 笔，不判定。
