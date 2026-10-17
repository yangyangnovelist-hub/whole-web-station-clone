# 检验 G：检验 F 加“Polymarket 还没动”的条件（BTC 5m，币安逐笔成交触发，GitHub 前向录制）

事先写死（10 月 1 日 02:00 UTC，数据还没录）：2026-10-01 02:30 UTC 起开始的 BTC 5m 市场；规则同检验 F（币安逐笔成交 2σ 触发、0.3 秒后按卖一、公平价 − 卖一 − 手续费 ≥ 12¢、每个市场第一笔、持有到结算），另外：触发前 2 秒内 Up 中间价已经朝要买的方向动了 3¢ 或更多（或 2 秒前没有报价）就跳过这个候选。这个条件在 5 月 25 日–7 月 15 日上定（没动的 +5.5¢、已动的 +0.4¢），之后两段核对（B +7.8/+6.1¢，C +13.1/+3.1¢）。按触发时间取前 600 笔判定一次，EV > 0 且精确 p < 0.025 才算通过。数据处理同检验 C、D、F。

录制段 65 个，成交 736 笔。

录制段备注（跳过的和断线检查）：

- btc 36671472407: skipped (FileNotFoundError: no BINANCE_WS_TRADE events under /home/runner/work/_temp/rec/36671472407/x/bundle-btc/latency)
- btc 36704245701: skipped (FileNotFoundError: no BINANCE_WS_TRADE events under /home/runner/work/_temp/rec/36704245701/x/bundle-btc/latency)
- btc 36740728504: skipped (FileNotFoundError: no BINANCE_WS_TRADE events under /home/runner/work/_temp/rec/36740728504/x/bundle-btc/latency)
- btc 36781277830: no markets from 2026-10-01 02:30 with book and spot data
- btc 36869869857: 18 candidates dropped because the CLOB socket closed between the quote and the order (3 disconnects in the recording; test C counts its judged lag only)
- btc 36972067946: 136 candidates dropped because the CLOB socket closed between the quote and the order (15 disconnects in the recording; test C counts its judged lag only)
- btc 37002779006: 1564 candidates dropped because the CLOB socket closed between the quote and the order (107 disconnects in the recording; test C counts its judged lag only)
- btc 37041041140: 544 candidates dropped because the CLOB socket closed between the quote and the order (70 disconnects in the recording; test C counts its judged lag only)
- btc 37075141917: 555 candidates dropped because the CLOB socket closed between the quote and the order (80 disconnects in the recording; test C counts its judged lag only)
- btc 37095301195: 1007 candidates dropped because the CLOB socket closed between the quote and the order (76 disconnects in the recording; test C counts its judged lag only)
- btc 37111395430: 388 candidates dropped because the CLOB socket closed between the quote and the order (77 disconnects in the recording; test C counts its judged lag only)
- btc 37128550183: 308 candidates dropped because the CLOB socket closed between the quote and the order (58 disconnects in the recording; test C counts its judged lag only)
- btc 37146417247: 1288 candidates dropped because the CLOB socket closed between the quote and the order (102 disconnects in the recording; test C counts its judged lag only)
- btc 37164037946: 542 candidates dropped because the CLOB socket closed between the quote and the order (61 disconnects in the recording; test C counts its judged lag only)
- btc 37179397738: 443 candidates dropped because the CLOB socket closed between the quote and the order (70 disconnects in the recording; test C counts its judged lag only)
- btc 37194238626: 678 candidates dropped because the CLOB socket closed between the quote and the order (78 disconnects in the recording; test C counts its judged lag only)
- btc 37230483105: 2881 candidates dropped because the CLOB socket closed between the quote and the order (179 disconnects in the recording; test C counts its judged lag only)
- btc 37250413580: 2490 candidates dropped because the CLOB socket closed between the quote and the order (161 disconnects in the recording; test C counts its judged lag only)
- btc 37274274070: 1973 candidates dropped because the CLOB socket closed between the quote and the order (214 disconnects in the recording; test C counts its judged lag only)
- btc 37307261464: 3839 candidates dropped because the CLOB socket closed between the quote and the order (182 disconnects in the recording; test C counts its judged lag only)
- btc 37347643542: 2201 candidates dropped because the CLOB socket closed between the quote and the order (249 disconnects in the recording; test C counts its judged lag only)
- btc 37384144976: 686 candidates dropped because the CLOB socket closed between the quote and the order (157 disconnects in the recording; test C counts its judged lag only)
- btc 37412793391: 2468 candidates dropped because the CLOB socket closed between the quote and the order (219 disconnects in the recording; test C counts its judged lag only)
- btc 37444926603: 3220 candidates dropped because the CLOB socket closed between the quote and the order (244 disconnects in the recording; test C counts its judged lag only)
- btc 37484528542: 3042 candidates dropped because the CLOB socket closed between the quote and the order (232 disconnects in the recording; test C counts its judged lag only)
- btc 37527655881: 1011 candidates dropped because the CLOB socket closed between the quote and the order (120 disconnects in the recording; test C counts its judged lag only)
- btc 37558084377: 2355 candidates dropped because the CLOB socket closed between the quote and the order (184 disconnects in the recording; test C counts its judged lag only)
- btc 37586562398: 9755 candidates dropped because the CLOB socket closed between the quote and the order (289 disconnects in the recording; test C counts its judged lag only)
- btc 37621711008: 6329 candidates dropped because the CLOB socket closed between the quote and the order (356 disconnects in the recording; test C counts its judged lag only)
- btc 37663361030: 1813 candidates dropped because the CLOB socket closed between the quote and the order (260 disconnects in the recording; test C counts its judged lag only)
- btc 37702189414: 1448 candidates dropped because the CLOB socket closed between the quote and the order (230 disconnects in the recording; test C counts its judged lag only)
- btc 37729957174: 2242 candidates dropped because the CLOB socket closed between the quote and the order (230 disconnects in the recording; test C counts its judged lag only)
- btc 37764001551: 5094 candidates dropped because the CLOB socket closed between the quote and the order (361 disconnects in the recording; test C counts its judged lag only)
- btc 37806403126: 3877 candidates dropped because the CLOB socket closed between the quote and the order (338 disconnects in the recording; test C counts its judged lag only)
- btc 37847134073: 1855 candidates dropped because the CLOB socket closed between the quote and the order (240 disconnects in the recording; test C counts its judged lag only)
- btc 37875151310: 1763 candidates dropped because the CLOB socket closed between the quote and the order (235 disconnects in the recording; test C counts its judged lag only)
- btc 37903011520: 3499 candidates dropped because the CLOB socket closed between the quote and the order (236 disconnects in the recording; test C counts its judged lag only)
- btc 37938384834: 2773 candidates dropped because the CLOB socket closed between the quote and the order (231 disconnects in the recording; test C counts its judged lag only)
- btc 38110498321: 162 candidates dropped because the CLOB socket closed between the quote and the order (29 disconnects in the recording; test C counts its judged lag only)
- btc 38128792010: 191 candidates dropped because the CLOB socket closed between the quote and the order (44 disconnects in the recording; test C counts its judged lag only)
- btc 38149300477: 478 candidates dropped because the CLOB socket closed between the quote and the order (43 disconnects in the recording; test C counts its judged lag only)
- btc 38204238337: 1151 candidates dropped because the CLOB socket closed between the quote and the order (55 disconnects in the recording; test C counts its judged lag only)
- btc 38224319208: 2220 candidates dropped because the CLOB socket closed between the quote and the order (102 disconnects in the recording; test C counts its judged lag only)
- btc 38256540882: 1225 candidates dropped because the CLOB socket closed between the quote and the order (155 disconnects in the recording; test C counts its judged lag only)
- btc 38295792174: 1195 candidates dropped because the CLOB socket closed between the quote and the order (114 disconnects in the recording; test C counts its judged lag only)
- btc 38328336070: 870 candidates dropped because the CLOB socket closed between the quote and the order (90 disconnects in the recording; test C counts its judged lag only)
- btc 38356084728: 777 candidates dropped because the CLOB socket closed between the quote and the order (115 disconnects in the recording; test C counts its judged lag only)
- btc 38392307218: 2009 candidates dropped because the CLOB socket closed between the quote and the order (168 disconnects in the recording; test C counts its judged lag only)
- btc 38433952235: 1096 candidates dropped because the CLOB socket closed between the quote and the order (92 disconnects in the recording; test C counts its judged lag only)
- btc 38478901038: 634 candidates dropped because the CLOB socket closed between the quote and the order (75 disconnects in the recording; test C counts its judged lag only)
- btc 38508898150: 190 candidates dropped because the CLOB socket closed between the quote and the order (64 disconnects in the recording; test C counts its judged lag only)
- btc 38540600818: 997 candidates dropped because the CLOB socket closed between the quote and the order (60 disconnects in the recording; test C counts its judged lag only)
- btc 38586775442: 543 candidates dropped because the CLOB socket closed between the quote and the order (61 disconnects in the recording; test C counts its judged lag only)
- btc 38637645559: 66 candidates dropped because the CLOB socket closed between the quote and the order (18 disconnects in the recording; test C counts its judged lag only)
- btc 38674242681: 384 candidates dropped because the CLOB socket closed between the quote and the order (38 disconnects in the recording; test C counts its judged lag only)
- btc 38706009620: 674 candidates dropped because the CLOB socket closed between the quote and the order (87 disconnects in the recording; test C counts its judged lag only)
- btc 38751308326: 281 candidates dropped because the CLOB socket closed between the quote and the order (8 disconnects in the recording; test C counts its judged lag only)
- btc 38801525953: 368 candidates dropped because the CLOB socket closed between the quote and the order (27 disconnects in the recording; test C counts its judged lag only)
- btc 38838476036: 129 candidates dropped because the CLOB socket closed between the quote and the order (24 disconnects in the recording; test C counts its judged lag only)
- btc 38869686142: 2651 candidates dropped because the CLOB socket closed between the quote and the order (207 disconnects in the recording; test C counts its judged lag only)
- btc 38909708508: 1697 candidates dropped because the CLOB socket closed between the quote and the order (260 disconnects in the recording; test C counts its judged lag only)

| 笔数 | 胜率 | 平均价 | EV | p | 卖一数量中位 |
|---|---|---|---|---|---|
| 736 | 51.0% | 0.374 | +12.30¢ | 0.0000 | 51 |

检验 G（前 600 笔）：币安触发、Polymarket 未动、公平价筛选的过期报价（θ=12¢，z=2，0.3 秒后按卖一买）：600 笔，EV +12.23¢，p = 0.0000 → 通过（录制段 53 个：36807987110, 36835222993, 36869869857, 36912544596, 36946930986, 36972067946, 37002779006, 37041041140, 37075141917, 37095301195, 37111395430, 37128550183, 37146417247, 37164037946, 37179397738, 37194238626, 37230483105, 37250413580, 37274274070, 37307261464, 37347643542, 37384144976, 37412793391, 37444926603, 37484528542, 37527655881, 37558084377, 37586562398, 37621711008, 37663361030, 37702189414, 37729957174, 37764001551, 37806403126, 37847134073, 37875151310, 37903011520, 37938384834, 38110498321, 38128792010, 38149300477, 38204238337, 38224319208, 38256540882, 38295792174, 38328336070, 38356084728, 38392307218, 38433952235, 38478901038, 38508898150, 38540600818, 38586775442；最后一笔触发于 2026-10-14 18:08 UTC）（已判定，不再重算）
