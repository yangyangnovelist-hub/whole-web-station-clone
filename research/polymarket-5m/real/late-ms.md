# 毫秒级下单能拿到 kacho 每秒盘口上“晚 1–2 秒”的钱吗（GitHub 前向录制，探索性，事先写定）

录制：市场开始于 10-01 02:50 – 10-04 14:30 UTC（约 3.5 天），866 个有盘口的 BTC 5m 市场；币安 2σ 一秒急动 4,703 次（逐笔成交触发，剩 240–15 秒，每个市场每 10 秒取第一次）。

真实执行：触发成交（币安交易所时间）后 s 秒发单，再过 375 ms 撮合（试点实测发单→撮合，含 150 ms 冻结），按撮合时刻交易所显示的卖一买（0.02–0.98、至少 5 份），付 taker 费，持有到结算。kacho 模拟：同一批急动和盘口，按 kacho 的做法用急动秒 S 之后第 d 秒的“行”，行内报价取该行最晚可能的状态（行末）或该秒内随机时刻的状态（行内随机），不管能否成交。每份 ± 按市场聚类的标准误（笔数）。

## 表 1 真实执行，市价单（按撮合时卖一）

| 发单 s 秒（撮合 s + 0.375） | 顺急动方向 | 只买每个市场第一次急动 | 每个市场第一次能成交的急动 | 反方向 | 随机时刻、随机方向 | 每天美元（每张 5 份） | 每天美元（每张 min(20, 挂单量)） |
|---|---:|---:|---:|---:|---:|---:|---:|
| 0.11 | -0.09¢ ±0.62（3,158） | +0.28¢ ±1.54（682） | -0.08¢ ±1.46（754） | -3.37¢ ±0.59（3,338） | -2.61¢ ±0.68（3,240） | -4 | -16 |
| 0.3 | -0.58¢ ±0.63（3,175） | -0.12¢ ±1.54（691） | -0.82¢ ±1.48（751） | -2.59¢ ±0.60（3,316） | -2.74¢ ±0.68（3,236） | -26 | -96 |
| 0.5 | -0.60¢ ±0.63（3,166） | +0.04¢ ±1.54（679） | -0.09¢ ±1.48（747） | -2.51¢ ±0.61（3,296） | -2.66¢ ±0.69（3,231） | -27 | -109 |
| 0.75 | -0.61¢ ±0.63（3,176） | -0.36¢ ±1.53（687） | -0.32¢ ±1.47（744） | -2.50¢ ±0.60（3,290） | -2.58¢ ±0.69（3,229） | -28 | -106 |
| 1 | -0.62¢ ±0.62（3,160） | -0.18¢ ±1.53（687） | -0.73¢ ±1.48（747） | -2.48¢ ±0.60（3,294） | -2.57¢ ±0.68（3,229） | -28 | -109 |
| 1.5 | -0.76¢ ±0.63（3,156） | -0.20¢ ±1.54（687） | -0.37¢ ±1.48（745） | -2.31¢ ±0.60（3,269） | -2.49¢ ±0.70（3,212） | -34 | -142 |
| 2 | -0.75¢ ±0.63（3,151） | -0.34¢ ±1.54（690） | -0.66¢ ±1.48（745） | -2.31¢ ±0.61（3,274） | -2.59¢ ±0.69（3,213） | -34 | -142 |
| 3 | -0.68¢ ±0.63（3,146） | -0.18¢ ±1.52（708） | -0.36¢ ±1.46（754） | -2.36¢ ±0.61（3,290） | -2.41¢ ±0.69（3,203） | -31 | -135 |
| 5 | -0.65¢ ±0.62（3,149） | +0.10¢ ±1.50（719） | -0.24¢ ±1.46（758） | -2.33¢ ±0.60（3,262） | -2.27¢ ±0.70（3,172） | -29 | -111 |
| 10 | -0.55¢ ±0.65（3,074） | +0.16¢ ±1.50（719） | +0.10¢ ±1.46（752） | -2.37¢ ±0.63（3,201） | -2.43¢ ±0.70（3,094） | -24 | -97 |

## 表 2 真实执行，限价单（限价 = 发单时看到的卖一 + k；撮合时卖一不高于限价且至少 5 份才成交，按撮合时卖一成交）

每张发单的每份把没成交的算 0。

| 发单 s 秒 | k = 0¢ 成交率 | 成交的每份 | 每张发单 | k = 2¢ 成交率 | 成交的每份 | 每张发单 | k = 5¢ 成交率 | 成交的每份 | 每张发单 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.11 | 22% | +1.16¢ | +0.25¢ ±0.28（3,255） | 48% | +0.44¢ | +0.21¢ ±0.39（3,255） | 70% | +0.10¢ | +0.07¢ ±0.50（3,255） |
| 0.3 | 50% | -0.74¢ | -0.37¢ ±0.49（3,231） | 77% | -0.78¢ | -0.60¢ ±0.58（3,231） | 88% | -0.19¢ | -0.17¢ ±0.61（3,231） |
| 0.5 | 69% | -0.69¢ | -0.48¢ ±0.55（3,226） | 88% | -0.59¢ | -0.52¢ ±0.62（3,226） | 95% | -0.30¢ | -0.28¢ ±0.63（3,226） |
| 0.75 | 78% | -0.91¢ | -0.71¢ ±0.55（3,209） | 93% | -0.65¢ | -0.60¢ ±0.61（3,209） | 97% | -0.54¢ | -0.53¢ ±0.62（3,209） |
| 1 | 81% | -0.90¢ | -0.73¢ ±0.55（3,192） | 94% | -0.76¢ | -0.71¢ ±0.59（3,192） | 98% | -0.63¢ | -0.62¢ ±0.61（3,192） |
| 1.5 | 82% | -0.89¢ | -0.73¢ ±0.56（3,180） | 95% | -0.55¢ | -0.52¢ ±0.60（3,180） | 98% | -0.77¢ | -0.75¢ ±0.61（3,180） |
| 2 | 83% | -0.67¢ | -0.56¢ ±0.57（3,174） | 95% | -0.94¢ | -0.90¢ ±0.60（3,174） | 99% | -0.79¢ | -0.78¢ ±0.62（3,174） |
| 3 | 83% | -1.25¢ | -1.04¢ ±0.60（3,175） | 96% | -0.88¢ | -0.84¢ ±0.63（3,175） | 99% | -0.80¢ | -0.79¢ ±0.63（3,175） |
| 5 | 83% | -1.24¢ | -1.03¢ ±0.55（3,174） | 95% | -0.80¢ | -0.76¢ ±0.59（3,174） | 98% | -0.68¢ | -0.67¢ ±0.60（3,174） |
| 10 | 85% | -0.97¢ | -0.83¢ ±0.59（3,089） | 96% | -0.83¢ | -0.80¢ ±0.64（3,089） | 99% | -0.66¢ | -0.65¢ ±0.64（3,089） |

## 表 3 kacho 的做法放到同一批急动上（顺急动方向）

S = 急动所在的整秒；kacho 在第 S + d 秒用的是标为 S + d − 1 的那一行。
行末：第 S + d − 0.001 秒的盘口；行内随机：第 S + d − 1 + u 秒（u 均匀，每次急动一个）；真实：第 S + d 秒发单（但不早于触发成交后 0.11 秒，最快的真实发单），发单后 0.375 秒按卖一撮合；d = 1 时有 10% 的急动落在该秒最后 110 ms，改在触发后 0.11 秒发单。

| 晚 d 秒 | kacho.io 实测（3/24–5/18） | 模拟：行末状态 | 模拟：行内随机时刻 | 真实执行（S + d 发单，不早于触发后 0.11 秒） |
|---|---:|---:|---:|---:|
| 1 | +5.57¢ ±0.11 | +0.81¢ ±0.63（3,208） | +4.14¢ ±0.63（3,282） | -0.38¢ ±0.63（3,174） |
| 2 | +3.09¢ ±0.12 | -0.66¢ ±0.62（3,169） | -0.44¢ ±0.62（3,166） | -0.75¢ ±0.63（3,153） |
| 3 | +0.10¢ ±0.12 | -0.64¢ ±0.64（3,154） | -0.61¢ ±0.63（3,165） | -0.58¢ ±0.65（3,132） |
| 5 | -1.20¢ ±0.12 | -0.76¢ ±0.63（3,154） | -0.80¢ ±0.63（3,172） | -0.69¢ ±0.62（3,148） |

d = 1 的模拟行里，卖一还是急动前那个（触发成交之后这一方的卖一没变过）的有多少，各赚多少：

| d = 1 的模拟 | 行数 | 急动前的旧卖一占比 | 旧卖一的每份 | 其余的每份 |
|---|---:|---:|---:|---:|
| 行末状态 | 3,208 | 27% | +3.14¢ ±1.11（865） | -0.05¢ ±0.76（2,343） |
| 行内随机时刻 | 3,282 | 69% | +6.19¢ ±0.76（2,278） | -0.53¢ ±1.20（1,004） |

## 表 4 前后两半（按市场开始时间把录到的市场分成两半）

| 执行（顺急动方向） | 前半（10-01 02:50 – 10-02 21:45） | 后半（10-02 21:50 – 10-04 14:30） |
|---|---:|---:|
| 真实市价单 s = 0.11 | -0.60¢ ±0.72（2,339） | +1.36¢ ±1.22（819） |
| 真实市价单 s = 0.3 | -1.17¢ ±0.72（2,354） | +1.14¢ ±1.27（821） |
| 真实市价单 s = 0.5 | -1.18¢ ±0.71（2,351） | +1.07¢ ±1.31（815） |
| 真实市价单 s = 0.75 | -1.17¢ ±0.71（2,359） | +1.01¢ ±1.31（817） |
| 真实市价单 s = 1 | -1.20¢ ±0.69（2,350） | +1.07¢ ±1.32（810） |
| 真实市价单 s = 1.5 | -1.35¢ ±0.72（2,344） | +0.92¢ ±1.32（812） |
| 真实市价单 s = 2 | -1.29¢ ±0.71（2,349） | +0.82¢ ±1.32（802） |
| 真实市价单 s = 3 | -1.16¢ ±0.72（2,314） | +0.65¢ ±1.31（832） |
| 真实市价单 s = 5 | -1.07¢ ±0.72（2,294） | +0.48¢ ±1.18（855） |
| 真实市价单 s = 10 | -0.63¢ ±0.75（2,242） | -0.31¢ ±1.28（832） |
| kacho 模拟·行末 d = 1 | +0.05¢ ±0.72（2,364） | +2.92¢ ±1.27（844） |
| kacho 模拟·行末 d = 2 | -1.27¢ ±0.71（2,352） | +1.09¢ ±1.30（817） |
| kacho 模拟·行末 d = 3 | -1.23¢ ±0.72（2,340） | +1.04¢ ±1.34（814） |
| kacho 模拟·行末 d = 5 | -1.09¢ ±0.73（2,299） | +0.11¢ ±1.22（855） |
| kacho 模拟·行内随机 d = 1 | +3.15¢ ±0.73（2,384） | +6.75¢ ±1.22（898） |
| kacho 模拟·行内随机 d = 2 | -1.11¢ ±0.71（2,354） | +1.51¢ ±1.28（812） |
| kacho 模拟·行内随机 d = 3 | -1.18¢ ±0.71（2,349） | +1.04¢ ±1.30（816） |
| kacho 模拟·行内随机 d = 5 | -1.10¢ ±0.73（2,316） | +0.02¢ ±1.25（856） |
| 真实 S + 1 发单 | -0.96¢ ±0.72（2,356） | +1.31¢ ±1.29（818） |
| 真实 S + 2 发单 | -1.32¢ ±0.72（2,339） | +0.91¢ ±1.32（814） |
| 真实 S + 3 发单 | -1.13¢ ±0.74（2,324） | +0.99¢ ±1.35（808） |
| 真实 S + 5 发单 | -1.07¢ ±0.73（2,298） | +0.34¢ ±1.21（850） |

## 备注

- 只用行情，没有下单。盘口行只在其交易所时间戳不晚于使用时刻时才用；“盘口在跑”（撮合前 30 秒、后 10 秒内有更新）和“撮合前后 CLOB 连接没断”只用来剔除录制故障。
- 限价单只在发单时卖一在 0.02–0.98 时才发；撮合时这一方没有卖一的不算（跳过）；成交价不受 0.02–0.98 限制。随机对照每次急动配一个随机时刻和随机方向（与行内随机的 u 一样，按急动时刻各自取随机数）。
- 表 1“只买每个市场第一次急动”：每个市场只看它的第一次急动，某个 s 下没成交就不算，不拿第二次顶替（各 s 是同一批急动）；“第一次能成交的急动”是 kacho-late.md 的算法：某个 s 下每个市场第一笔能成交的急动。
- 急动只在有盘口的市场里找和计数。
- 下面 CLOB 断线剔除的条数是所有 s、方向和模拟方式加在一起的。
- btc 36671472407: skipped (FileNotFoundError: no BINANCE_WS_TRADE events under /home/runner/work/_temp/rec/36671472407/x/bundle-btc/latency)
- btc 36704245701: skipped (FileNotFoundError: no BINANCE_WS_TRADE events under /home/runner/work/_temp/rec/36704245701/x/bundle-btc/latency)
- btc 36740728504: skipped (FileNotFoundError: no BINANCE_WS_TRADE events under /home/runner/work/_temp/rec/36740728504/x/bundle-btc/latency)
- btc 36781277830: no markets from 2026-10-01 02:30 with book and spot data
- btc 36807987110: 1 candidates dropped because the CLOB socket closed between the quote and the order (1 disconnects in the recording; test C counts its judged lag only)
- btc 36835222993: 9 candidates dropped because the CLOB socket closed between the quote and the order (3 disconnects in the recording; test C counts its judged lag only)
- btc 36869869857: 21 candidates dropped because the CLOB socket closed between the quote and the order (3 disconnects in the recording; test C counts its judged lag only)
- btc 36946930986: 38 candidates dropped because the CLOB socket closed between the quote and the order (1 disconnects in the recording; test C counts its judged lag only)
- btc 36972067946: 156 candidates dropped because the CLOB socket closed between the quote and the order (15 disconnects in the recording; test C counts its judged lag only)
- btc 37002779006: 964 candidates dropped because the CLOB socket closed between the quote and the order (107 disconnects in the recording; test C counts its judged lag only)
- btc 37041041140: 426 candidates dropped because the CLOB socket closed between the quote and the order (70 disconnects in the recording; test C counts its judged lag only)
- btc 37075141917: 306 candidates dropped because the CLOB socket closed between the quote and the order (80 disconnects in the recording; test C counts its judged lag only)
- btc 37095301195: 406 candidates dropped because the CLOB socket closed between the quote and the order (76 disconnects in the recording; test C counts its judged lag only)
- btc 37111395430: 226 candidates dropped because the CLOB socket closed between the quote and the order (77 disconnects in the recording; test C counts its judged lag only)
- btc 37128550183: 245 candidates dropped because the CLOB socket closed between the quote and the order (58 disconnects in the recording; test C counts its judged lag only)
- btc 37146417247: 383 candidates dropped because the CLOB socket closed between the quote and the order (102 disconnects in the recording; test C counts its judged lag only)
- btc 37164037946: 200 candidates dropped because the CLOB socket closed between the quote and the order (61 disconnects in the recording; test C counts its judged lag only)
- btc 37179397738: 217 candidates dropped because the CLOB socket closed between the quote and the order (70 disconnects in the recording; test C counts its judged lag only)
- btc 37194238626: 461 candidates dropped because the CLOB socket closed between the quote and the order (78 disconnects in the recording; test C counts its judged lag only)
