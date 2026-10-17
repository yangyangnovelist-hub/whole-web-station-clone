# 检验 I：H 每次都加、反向 2 倍，0.4 秒（BTC 5m，币安逐笔成交触发，GitHub 前向录制）

事先写死（10 月 2 日约 08:30 UTC，数据还没录）：2026-10-02 09:00 UTC 起开始的 BTC 5m 市场；币安逐笔成交 2σ 触发（剩 240–15 秒），公平价按 H：从触发前 2 秒的 Up 中间价起算，加上这 2 秒币安的涨跌；0.4 秒后按卖一买顺势一方，公平价 − 卖一 − 手续费 ≥ 12¢ 才买；同一市场每次都买，买到后 2 秒内不再买；第一笔和与第一笔同方向的加仓每笔 1 份，反方向的加仓每笔 2 份；持有到结算。每份赚的钱 = Σ份数×盈亏 / Σ份数，标准误按市场聚类（同一市场的几笔一起结算），单边正态 p。按每个市场第一笔的时间取前 1,000 个有成交的市场、它们的全部成交，判定一次：每份 > 0 且 p < 0.025 才算通过。数据处理同检验 G（每段录制单独算、盘口健康、断线检查）。

录制段 65 个，0.4 秒：成交 1,111 笔、738 个市场。

录制段备注（跳过的和断线检查）：

- btc 36671472407: skipped (FileNotFoundError: no BINANCE_WS_TRADE events under /home/runner/work/_temp/rec/36671472407/x/bundle-btc/latency)
- btc 36704245701: skipped (FileNotFoundError: no BINANCE_WS_TRADE events under /home/runner/work/_temp/rec/36704245701/x/bundle-btc/latency)
- btc 36740728504: skipped (FileNotFoundError: no BINANCE_WS_TRADE events under /home/runner/work/_temp/rec/36740728504/x/bundle-btc/latency)
- btc 36781277830: no markets from 2026-10-02 09:00 with book and spot data
- btc 36807987110: no markets from 2026-10-02 09:00 with book and spot data
- btc 36835222993: no markets from 2026-10-02 09:00 with book and spot data
- btc 36869869857: no markets from 2026-10-02 09:00 with book and spot data
- btc 36912544596: no markets from 2026-10-02 09:00 with book and spot data
- btc 36946930986: no markets from 2026-10-02 09:00 with book and spot data
- btc 36972067946: 273 candidates dropped because the CLOB socket closed between the quote and the order (15 disconnects in the recording; test C counts its judged lag only)
- btc 37002779006: 1622 candidates dropped because the CLOB socket closed between the quote and the order (107 disconnects in the recording; test C counts its judged lag only)
- btc 37041041140: 958 candidates dropped because the CLOB socket closed between the quote and the order (70 disconnects in the recording; test C counts its judged lag only)
- btc 37075141917: 1013 candidates dropped because the CLOB socket closed between the quote and the order (80 disconnects in the recording; test C counts its judged lag only)
- btc 37095301195: 1153 candidates dropped because the CLOB socket closed between the quote and the order (76 disconnects in the recording; test C counts its judged lag only)
- btc 37111395430: 431 candidates dropped because the CLOB socket closed between the quote and the order (77 disconnects in the recording; test C counts its judged lag only)
- btc 37128550183: 396 candidates dropped because the CLOB socket closed between the quote and the order (58 disconnects in the recording; test C counts its judged lag only)
- btc 37146417247: 1754 candidates dropped because the CLOB socket closed between the quote and the order (102 disconnects in the recording; test C counts its judged lag only)
- btc 37164037946: 762 candidates dropped because the CLOB socket closed between the quote and the order (61 disconnects in the recording; test C counts its judged lag only)
- btc 37179397738: 576 candidates dropped because the CLOB socket closed between the quote and the order (70 disconnects in the recording; test C counts its judged lag only)
- btc 37194238626: 1008 candidates dropped because the CLOB socket closed between the quote and the order (78 disconnects in the recording; test C counts its judged lag only)
- btc 37230483105: 3579 candidates dropped because the CLOB socket closed between the quote and the order (179 disconnects in the recording; test C counts its judged lag only)
- btc 37250413580: 3681 candidates dropped because the CLOB socket closed between the quote and the order (161 disconnects in the recording; test C counts its judged lag only)
- btc 37274274070: 2608 candidates dropped because the CLOB socket closed between the quote and the order (214 disconnects in the recording; test C counts its judged lag only)
- btc 37307261464: 4434 candidates dropped because the CLOB socket closed between the quote and the order (182 disconnects in the recording; test C counts its judged lag only)
- btc 37347643542: 2974 candidates dropped because the CLOB socket closed between the quote and the order (249 disconnects in the recording; test C counts its judged lag only)
- btc 37384144976: 1225 candidates dropped because the CLOB socket closed between the quote and the order (157 disconnects in the recording; test C counts its judged lag only)
- btc 37412793391: 3639 candidates dropped because the CLOB socket closed between the quote and the order (219 disconnects in the recording; test C counts its judged lag only)
- btc 37444926603: 4038 candidates dropped because the CLOB socket closed between the quote and the order (244 disconnects in the recording; test C counts its judged lag only)
- btc 37484528542: 3612 candidates dropped because the CLOB socket closed between the quote and the order (232 disconnects in the recording; test C counts its judged lag only)
- btc 37527655881: 1513 candidates dropped because the CLOB socket closed between the quote and the order (120 disconnects in the recording; test C counts its judged lag only)
- btc 37558084377: 3239 candidates dropped because the CLOB socket closed between the quote and the order (184 disconnects in the recording; test C counts its judged lag only)
- btc 37586562398: 12853 candidates dropped because the CLOB socket closed between the quote and the order (289 disconnects in the recording; test C counts its judged lag only)
- btc 37621711008: 8188 candidates dropped because the CLOB socket closed between the quote and the order (356 disconnects in the recording; test C counts its judged lag only)
- btc 37663361030: 2649 candidates dropped because the CLOB socket closed between the quote and the order (260 disconnects in the recording; test C counts its judged lag only)
- btc 37702189414: 3182 candidates dropped because the CLOB socket closed between the quote and the order (230 disconnects in the recording; test C counts its judged lag only)
- btc 37729957174: 3616 candidates dropped because the CLOB socket closed between the quote and the order (230 disconnects in the recording; test C counts its judged lag only)
- btc 37764001551: 6273 candidates dropped because the CLOB socket closed between the quote and the order (361 disconnects in the recording; test C counts its judged lag only)
- btc 37806403126: 5861 candidates dropped because the CLOB socket closed between the quote and the order (338 disconnects in the recording; test C counts its judged lag only)
- btc 37847134073: 2658 candidates dropped because the CLOB socket closed between the quote and the order (240 disconnects in the recording; test C counts its judged lag only)
- btc 37875151310: 2686 candidates dropped because the CLOB socket closed between the quote and the order (235 disconnects in the recording; test C counts its judged lag only)
- btc 37903011520: 4598 candidates dropped because the CLOB socket closed between the quote and the order (236 disconnects in the recording; test C counts its judged lag only)
- btc 37938384834: 3198 candidates dropped because the CLOB socket closed between the quote and the order (231 disconnects in the recording; test C counts its judged lag only)
- btc 38110498321: 289 candidates dropped because the CLOB socket closed between the quote and the order (29 disconnects in the recording; test C counts its judged lag only)
- btc 38128792010: 267 candidates dropped because the CLOB socket closed between the quote and the order (44 disconnects in the recording; test C counts its judged lag only)
- btc 38149300477: 966 candidates dropped because the CLOB socket closed between the quote and the order (43 disconnects in the recording; test C counts its judged lag only)
- btc 38204238337: 1353 candidates dropped because the CLOB socket closed between the quote and the order (55 disconnects in the recording; test C counts its judged lag only)
- btc 38224319208: 2827 candidates dropped because the CLOB socket closed between the quote and the order (102 disconnects in the recording; test C counts its judged lag only)
- btc 38256540882: 1629 candidates dropped because the CLOB socket closed between the quote and the order (155 disconnects in the recording; test C counts its judged lag only)
- btc 38295792174: 1349 candidates dropped because the CLOB socket closed between the quote and the order (114 disconnects in the recording; test C counts its judged lag only)
- btc 38328336070: 1351 candidates dropped because the CLOB socket closed between the quote and the order (90 disconnects in the recording; test C counts its judged lag only)
- btc 38356084728: 1263 candidates dropped because the CLOB socket closed between the quote and the order (115 disconnects in the recording; test C counts its judged lag only)
- btc 38392307218: 3054 candidates dropped because the CLOB socket closed between the quote and the order (168 disconnects in the recording; test C counts its judged lag only)
- btc 38433952235: 1776 candidates dropped because the CLOB socket closed between the quote and the order (92 disconnects in the recording; test C counts its judged lag only)
- btc 38478901038: 950 candidates dropped because the CLOB socket closed between the quote and the order (75 disconnects in the recording; test C counts its judged lag only)
- btc 38508898150: 657 candidates dropped because the CLOB socket closed between the quote and the order (64 disconnects in the recording; test C counts its judged lag only)
- btc 38540600818: 1207 candidates dropped because the CLOB socket closed between the quote and the order (60 disconnects in the recording; test C counts its judged lag only)
- btc 38586775442: 942 candidates dropped because the CLOB socket closed between the quote and the order (61 disconnects in the recording; test C counts its judged lag only)
- btc 38637645559: 317 candidates dropped because the CLOB socket closed between the quote and the order (18 disconnects in the recording; test C counts its judged lag only)
- btc 38674242681: 641 candidates dropped because the CLOB socket closed between the quote and the order (38 disconnects in the recording; test C counts its judged lag only)
- btc 38706009620: 771 candidates dropped because the CLOB socket closed between the quote and the order (87 disconnects in the recording; test C counts its judged lag only)
- btc 38751308326: 371 candidates dropped because the CLOB socket closed between the quote and the order (8 disconnects in the recording; test C counts its judged lag only)
- btc 38801525953: 417 candidates dropped because the CLOB socket closed between the quote and the order (27 disconnects in the recording; test C counts its judged lag only)
- btc 38838476036: 161 candidates dropped because the CLOB socket closed between the quote and the order (24 disconnects in the recording; test C counts its judged lag only)
- btc 38869686142: 3896 candidates dropped because the CLOB socket closed between the quote and the order (207 disconnects in the recording; test C counts its judged lag only)
- btc 38909708508: 3554 candidates dropped because the CLOB socket closed between the quote and the order (260 disconnects in the recording; test C counts its judged lag only)

| 版本 | 延迟 | 市场 | 笔数 | 每份（±聚类标准误） | p |
|---|---|---:|---:|---:|---:|
| 反向 2 倍（检验的规则） | 0.4 秒 | 738 | 1,111 | +8.59¢ ±1.4 | 0.0000 |
| 每次都加（每笔 1 份） | 0.4 秒 | 738 | 1,111 | +7.78¢ ±1.4 | 0.0000 |
| 只买第一笔 | 0.4 秒 | 738 | 738 | +6.57¢ ±1.6 | 0.0000 |
| 反向 2 倍（检验的规则） | 0.3 秒 | 843 | 1,312 | +11.98¢ ±1.3 | 0.0000 |

检验 I（前 1,000 个市场）：目前 738 个市场，不到 1,000 个，不判定。
