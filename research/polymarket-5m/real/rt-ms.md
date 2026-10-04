# 币安急动后买入、h 秒后在盘口卖出（GitHub 前向录制的毫秒盘口，探索性，事先写定）

市场开始于 10-01 03:05 – 10-04 14:20 UTC，442 个 BTC 5m 市场。

触发后 0.11 秒发单、0.375 秒撮合，按撮合时卖一买（≥ 5 份）；成交后 h 秒决定卖出，再 0.375 秒按买一卖（≥ 5 份），最晚剩 15 秒；两腿都付 taker 费。卖不掉的持有到结算（另计占比）。每份 ± 按市场聚类的标准误（笔数 / 市场数）。这是 kacho 每秒盘口上最好的那一族（ROUNDTRIP.md 第 1 族）在真实延迟下的复核。

## 每个市场第一次

| 急动 | h 秒 | 顺急动 | 反方向 | 随机时刻、随机方向 | 卖不掉、持有到结算 |
|---|---:|---:|---:|---:|---:|
| ≥ 2 bp | 5 | -3.21¢ ±0.73（347 / 347） | -4.67¢ ±0.70（374 / 374） | -2.98¢ ±0.37（343 / 343） | 4% |
| ≥ 2 bp | 10 | -2.87¢ ±0.73（347 / 347） | -4.16¢ ±0.75（374 / 374） | -2.37¢ ±0.60（343 / 343） | 4% |
| ≥ 2 bp | 20 | -3.68¢ ±0.86（347 / 347） | -4.33¢ ±0.81（374 / 374） | -3.50¢ ±0.85（343 / 343） | 3% |
| ≥ 2 bp | 30 | -4.28¢ ±0.99（347 / 347） | -3.78¢ ±0.95（374 / 374） | -3.79¢ ±1.04（343 / 343） | 3% |
| ≥ 2 bp | 60 | -4.80¢ ±1.23（347 / 347） | -2.37¢ ±1.19（374 / 374） | -3.09¢ ±1.40（343 / 343） | 5% |
| ≥ 4 bp | 5 | -1.74¢ ±1.32（109 / 109） | -7.00¢ ±1.25（120 / 120） | -2.95¢ ±0.60（112 / 112） | 3% |
| ≥ 4 bp | 10 | -0.87¢ ±1.38（109 / 109） | -6.95¢ ±1.48（120 / 120） | -2.27¢ ±1.20（112 / 112） | 4% |
| ≥ 4 bp | 20 | -0.84¢ ±1.60（109 / 109） | -7.19¢ ±1.55（120 / 120） | -3.93¢ ±1.08（112 / 112） | 6% |
| ≥ 4 bp | 30 | -1.73¢ ±2.05（109 / 109） | -5.70¢ ±2.01（120 / 120） | -3.36¢ ±1.49（112 / 112） | 7% |
| ≥ 4 bp | 60 | -2.75¢ ±2.76（109 / 109） | -4.90¢ ±2.54（120 / 120） | -1.24¢ ±2.26（112 / 112） | 6% |
| ≥ 8 bp | 5 | -2.20¢ ±3.43（20 / 20） | -7.06¢ ±3.33（22 / 22） | -4.74¢ ±1.42（16 / 16） | 5% |
| ≥ 8 bp | 10 | -2.84¢ ±3.61（20 / 20） | -2.70¢ ±4.50（22 / 22） | -1.67¢ ±2.14（16 / 16） | 5% |
| ≥ 8 bp | 20 | -3.28¢ ±3.56（20 / 20） | -6.31¢ ±2.92（22 / 22） | -4.08¢ ±3.11（16 / 16） | 0% |
| ≥ 8 bp | 30 | -4.34¢ ±5.45（20 / 20） | -4.97¢ ±5.16（22 / 22） | -4.46¢ ±4.44（16 / 16） | 5% |
| ≥ 8 bp | 60 | -3.74¢ ±6.47（20 / 20） | -5.05¢ ±5.79（22 / 22） | -2.81¢ ±5.32（16 / 16） | 10% |

## 每次（间隔 ≥ 10 秒）

| 急动 | h 秒 | 顺急动 | 反方向 | 随机时刻、随机方向 | 卖不掉、持有到结算 |
|---|---:|---:|---:|---:|---:|
| ≥ 2 bp | 5 | -3.18¢ ±0.34（1,206 / 373） | -4.04¢ ±0.33（1,265 / 393） | -3.19¢ ±0.27（1,268 / 411） | 4% |
| ≥ 2 bp | 10 | -2.97¢ ±0.41（1,206 / 373） | -3.85¢ ±0.39（1,265 / 393） | -2.63¢ ±0.39（1,268 / 411） | 5% |
| ≥ 2 bp | 20 | -2.29¢ ±0.58（1,206 / 373） | -4.60¢ ±0.54（1,265 / 393） | -2.52¢ ±0.44（1,268 / 411） | 6% |
| ≥ 2 bp | 30 | -2.23¢ ±0.57（1,206 / 373） | -4.48¢ ±0.55（1,265 / 393） | -2.39¢ ±0.53（1,268 / 411） | 7% |
| ≥ 2 bp | 60 | -2.80¢ ±0.78（1,206 / 373） | -3.49¢ ±0.71（1,265 / 393） | -2.38¢ ±0.72（1,268 / 411） | 12% |
| ≥ 4 bp | 5 | -1.68¢ ±0.88（187 / 117） | -5.61¢ ±0.96（202 / 126） | -3.11¢ ±0.42（207 / 133） | 5% |
| ≥ 4 bp | 10 | -0.78¢ ±0.93（187 / 117） | -6.00¢ ±1.06（202 / 126） | -2.84¢ ±0.77（207 / 133） | 7% |
| ≥ 4 bp | 20 | -0.97¢ ±1.22（187 / 117） | -5.70¢ ±1.25（202 / 126） | -3.90¢ ±0.75（207 / 133） | 9% |
| ≥ 4 bp | 30 | -1.52¢ ±1.30（187 / 117） | -4.83¢ ±1.27（202 / 126） | -3.27¢ ±1.02（207 / 133） | 10% |
| ≥ 4 bp | 60 | -2.38¢ ±1.78（187 / 117） | -4.36¢ ±1.64（202 / 126） | -2.97¢ ±1.49（207 / 133） | 13% |
| ≥ 8 bp | 5 | -0.87¢ ±2.99（24 / 22） | -7.20¢ ±2.72（28 / 24） | -3.17¢ ±1.40（24 / 18） | 8% |
| ≥ 8 bp | 10 | -1.51¢ ±3.20（24 / 22） | -3.44¢ ±3.81（28 / 24） | -0.82¢ ±1.64（24 / 18） | 8% |
| ≥ 8 bp | 20 | -1.59¢ ±3.19（24 / 22） | -6.22¢ ±2.46（28 / 24） | -2.69¢ ±2.25（24 / 18） | 4% |
| ≥ 8 bp | 30 | -2.78¢ ±4.68（24 / 22） | -4.51¢ ±4.27（28 / 24） | -3.77¢ ±3.63（24 / 18） | 4% |
| ≥ 8 bp | 60 | -2.61¢ ±5.41（24 / 22） | -3.52¢ ±5.42（28 / 24） | -2.90¢ ±3.80（24 / 18） | 8% |

## 前后两半（顺急动，每次）

| 急动 | h 秒 | 前半 | 后半 |
|---|---:|---:|---:|
| ≥ 2 bp | 5 | -2.91¢ ±0.38（663 / 207） | -3.50¢ ±0.59（543 / 166） |
| ≥ 2 bp | 10 | -2.69¢ ±0.49（663 / 207） | -3.30¢ ±0.69（543 / 166） |
| ≥ 2 bp | 20 | -2.43¢ ±0.60（663 / 207） | -2.13¢ ±1.05（543 / 166） |
| ≥ 4 bp | 5 | -0.79¢ ±0.83（92 / 62） | -2.55¢ ±1.54（95 / 55） |
| ≥ 4 bp | 10 | -1.10¢ ±0.92（92 / 62） | -0.48¢ ±1.62（95 / 55） |
| ≥ 4 bp | 20 | -0.52¢ ±1.21（92 / 62） | -1.41¢ ±2.10（95 / 55） |
| ≥ 8 bp | 5 | -6.75¢ ±4.07（12 / 11） | +5.01¢ ±3.96（12 / 11） |
| ≥ 8 bp | 10 | -7.28¢ ±4.48（12 / 11） | +4.25¢ ±4.13（12 / 11） |
| ≥ 8 bp | 20 | -5.19¢ ±4.38（12 / 11） | +2.01¢ ±4.54（12 / 11） |

## 备注

- btc 36671472407: skipped (FileNotFoundError: no BINANCE_WS_TRADE events under /home/runner/work/_temp/rec/36671472407/x/bundle-btc/latency)
- btc 36704245701: skipped (FileNotFoundError: no BINANCE_WS_TRADE events under /home/runner/work/_temp/rec/36704245701/x/bundle-btc/latency)
- btc 36740728504: skipped (FileNotFoundError: no BINANCE_WS_TRADE events under /home/runner/work/_temp/rec/36740728504/x/bundle-btc/latency)
- btc 36781277830: no markets from 2026-10-01 02:30 with book and spot data
- btc 36807987110: 6 candidates dropped because the CLOB socket closed between the quote and the order (1 disconnects in the recording; test C counts its judged lag only)
- btc 36869869857: 12 candidates dropped because the CLOB socket closed between the quote and the order (3 disconnects in the recording; test C counts its judged lag only)
- btc 36912544596: 4 candidates dropped because the CLOB socket closed between the quote and the order (4 disconnects in the recording; test C counts its judged lag only)
- btc 36972067946: 114 candidates dropped because the CLOB socket closed between the quote and the order (15 disconnects in the recording; test C counts its judged lag only)
- btc 37002779006: 1134 candidates dropped because the CLOB socket closed between the quote and the order (107 disconnects in the recording; test C counts its judged lag only)
- btc 37041041140: 250 candidates dropped because the CLOB socket closed between the quote and the order (70 disconnects in the recording; test C counts its judged lag only)
- btc 37075141917: 158 candidates dropped because the CLOB socket closed between the quote and the order (80 disconnects in the recording; test C counts its judged lag only)
- btc 37095301195: 132 candidates dropped because the CLOB socket closed between the quote and the order (76 disconnects in the recording; test C counts its judged lag only)
- btc 37111395430: 26 candidates dropped because the CLOB socket closed between the quote and the order (77 disconnects in the recording; test C counts its judged lag only)
- btc 37128550183: 44 candidates dropped because the CLOB socket closed between the quote and the order (58 disconnects in the recording; test C counts its judged lag only)
- btc 37146417247: 258 candidates dropped because the CLOB socket closed between the quote and the order (102 disconnects in the recording; test C counts its judged lag only)
- btc 37164037946: 122 candidates dropped because the CLOB socket closed between the quote and the order (61 disconnects in the recording; test C counts its judged lag only)
- btc 37179397738: 101 candidates dropped because the CLOB socket closed between the quote and the order (70 disconnects in the recording; test C counts its judged lag only)
- btc 37194238626: 190 candidates dropped because the CLOB socket closed between the quote and the order (78 disconnects in the recording; test C counts its judged lag only)
