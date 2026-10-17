# 检验 D：按公平价跳变筛选的过期报价（btc，coinbase 触发，GitHub 前向录制）

事先写死（9 月 30 日 13:40 UTC，数据还没看过）：2026-09-30 14:00 UTC 起开始的 5m 市场；剩 240–15 秒时，与至少一秒前（不早于 5 秒）相比涨跌超过 2σ 的每一笔 coinbase 成交都是候选；以那一刻交易所显示的 Up 中间价为原来的概率，这次涨跌让结算 TWAP 的期望整体移动，算出新的公平价；0.3 秒后按卖一买顺势一方，只在 公平价 − 卖一 − taker 费 ≥ 12¢ 时成交（相当于在触发时下一张成交不了就取消的限价单），每个市场取第一笔，持有到结算；按触发时间取前 1,200 笔判定一次，EV > 0 且精确 p < 0.025 才算通过。数据处理同检验 C。

录制段 65 个，成交 1,731 笔。

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
- btc 37274274070: 490 candidates dropped because the CLOB socket closed between the quote and the order (214 disconnects in the recording; test C counts its judged lag only)
- btc 37307261464: 1050 candidates dropped because the CLOB socket closed between the quote and the order (182 disconnects in the recording; test C counts its judged lag only)
- btc 37347643542: 669 candidates dropped because the CLOB socket closed between the quote and the order (249 disconnects in the recording; test C counts its judged lag only)
- btc 37384144976: 168 candidates dropped because the CLOB socket closed between the quote and the order (157 disconnects in the recording; test C counts its judged lag only)
- btc 37412793391: 426 candidates dropped because the CLOB socket closed between the quote and the order (219 disconnects in the recording; test C counts its judged lag only)
- btc 37444926603: 741 candidates dropped because the CLOB socket closed between the quote and the order (244 disconnects in the recording; test C counts its judged lag only)
- btc 37484528542: 789 candidates dropped because the CLOB socket closed between the quote and the order (232 disconnects in the recording; test C counts its judged lag only)
- btc 37527655881: 160 candidates dropped because the CLOB socket closed between the quote and the order (120 disconnects in the recording; test C counts its judged lag only)
- btc 37558084377: 734 candidates dropped because the CLOB socket closed between the quote and the order (184 disconnects in the recording; test C counts its judged lag only)
- btc 37586562398: 642 candidates dropped because the CLOB socket closed between the quote and the order (289 disconnects in the recording; test C counts its judged lag only)
- btc 37621711008: 2239 candidates dropped because the CLOB socket closed between the quote and the order (356 disconnects in the recording; test C counts its judged lag only)
- btc 37663361030: 516 candidates dropped because the CLOB socket closed between the quote and the order (260 disconnects in the recording; test C counts its judged lag only)
- btc 37702189414: 415 candidates dropped because the CLOB socket closed between the quote and the order (230 disconnects in the recording; test C counts its judged lag only)
- btc 37729957174: 488 candidates dropped because the CLOB socket closed between the quote and the order (230 disconnects in the recording; test C counts its judged lag only)
- btc 37764001551: 1163 candidates dropped because the CLOB socket closed between the quote and the order (361 disconnects in the recording; test C counts its judged lag only)
- btc 37806403126: 1151 candidates dropped because the CLOB socket closed between the quote and the order (338 disconnects in the recording; test C counts its judged lag only)
- btc 37847134073: 424 candidates dropped because the CLOB socket closed between the quote and the order (240 disconnects in the recording; test C counts its judged lag only)
- btc 37875151310: 535 candidates dropped because the CLOB socket closed between the quote and the order (235 disconnects in the recording; test C counts its judged lag only)
- btc 37903011520: 548 candidates dropped because the CLOB socket closed between the quote and the order (236 disconnects in the recording; test C counts its judged lag only)
- btc 37938384834: 704 candidates dropped because the CLOB socket closed between the quote and the order (231 disconnects in the recording; test C counts its judged lag only)
- btc 38110498321: 55 candidates dropped because the CLOB socket closed between the quote and the order (29 disconnects in the recording; test C counts its judged lag only)
- btc 38128792010: 33 candidates dropped because the CLOB socket closed between the quote and the order (44 disconnects in the recording; test C counts its judged lag only)
- btc 38149300477: 149 candidates dropped because the CLOB socket closed between the quote and the order (43 disconnects in the recording; test C counts its judged lag only)
- btc 38204238337: 196 candidates dropped because the CLOB socket closed between the quote and the order (55 disconnects in the recording; test C counts its judged lag only)
- btc 38224319208: 468 candidates dropped because the CLOB socket closed between the quote and the order (102 disconnects in the recording; test C counts its judged lag only)
- btc 38256540882: 449 candidates dropped because the CLOB socket closed between the quote and the order (155 disconnects in the recording; test C counts its judged lag only)
- btc 38295792174: 378 candidates dropped because the CLOB socket closed between the quote and the order (114 disconnects in the recording; test C counts its judged lag only)
- btc 38328336070: 263 candidates dropped because the CLOB socket closed between the quote and the order (90 disconnects in the recording; test C counts its judged lag only)
- btc 38356084728: 90 candidates dropped because the CLOB socket closed between the quote and the order (115 disconnects in the recording; test C counts its judged lag only)
- btc 38392307218: 657 candidates dropped because the CLOB socket closed between the quote and the order (168 disconnects in the recording; test C counts its judged lag only)
- btc 38433952235: 192 candidates dropped because the CLOB socket closed between the quote and the order (92 disconnects in the recording; test C counts its judged lag only)
- btc 38478901038: 165 candidates dropped because the CLOB socket closed between the quote and the order (75 disconnects in the recording; test C counts its judged lag only)
- btc 38508898150: 84 candidates dropped because the CLOB socket closed between the quote and the order (64 disconnects in the recording; test C counts its judged lag only)
- btc 38540600818: 233 candidates dropped because the CLOB socket closed between the quote and the order (60 disconnects in the recording; test C counts its judged lag only)
- btc 38586775442: 271 candidates dropped because the CLOB socket closed between the quote and the order (61 disconnects in the recording; test C counts its judged lag only)
- btc 38637645559: 10 candidates dropped because the CLOB socket closed between the quote and the order (18 disconnects in the recording; test C counts its judged lag only)
- btc 38674242681: 64 candidates dropped because the CLOB socket closed between the quote and the order (38 disconnects in the recording; test C counts its judged lag only)
- btc 38706009620: 168 candidates dropped because the CLOB socket closed between the quote and the order (87 disconnects in the recording; test C counts its judged lag only)
- btc 38751308326: 37 candidates dropped because the CLOB socket closed between the quote and the order (8 disconnects in the recording; test C counts its judged lag only)
- btc 38801525953: 74 candidates dropped because the CLOB socket closed between the quote and the order (27 disconnects in the recording; test C counts its judged lag only)
- btc 38838476036: 16 candidates dropped because the CLOB socket closed between the quote and the order (24 disconnects in the recording; test C counts its judged lag only)
- btc 38869686142: 669 candidates dropped because the CLOB socket closed between the quote and the order (207 disconnects in the recording; test C counts its judged lag only)
- btc 38909708508: 811 candidates dropped because the CLOB socket closed between the quote and the order (260 disconnects in the recording; test C counts its judged lag only)

| 笔数 | 胜率 | 平均价 | EV | p | 卖一数量中位 |
|---|---|---|---|---|---|
| 1,731 | 41.0% | 0.385 | +1.12¢ | 0.1520 | 109 |

检验 D（前 1,200 笔）：公平价筛选的过期报价（θ=12¢，z=2，0.3 秒后按卖一买）：1200 笔，EV -0.04¢，p = 1.0000 → 没通过（录制段 44 个：36704245701, 36740728504, 36781277830, 36807987110, 36835222993, 36869869857, 36912544596, 36946930986, 36972067946, 37002779006, 37041041140, 37075141917, 37095301195, 37111395430, 37128550183, 37146417247, 37164037946, 37179397738, 37194238626, 37230483105, 37250413580, 37274274070, 37307261464, 37347643542, 37384144976, 37412793391, 37444926603, 37484528542, 37527655881, 37558084377, 37586562398, 37621711008, 37663361030, 37702189414, 37729957174, 37764001551, 37806403126, 37847134073, 37875151310, 37903011520, 37938384834, 38110498321, 38128792010, 38149300477；最后一笔触发于 2026-10-11 18:42 UTC）（已判定，不再重算）
