# 币安急动后买入、h 秒后在盘口卖出（GitHub 前向录制的毫秒盘口，探索性，事先写定）

市场开始于 10-01 03:05 – 10-04 09:35 UTC，427 个 BTC 5m 市场。

触发后 0.11 秒发单、0.375 秒撮合，按撮合时卖一买（≥ 5 份）；成交后 h 秒决定卖出，再 0.375 秒按买一卖（≥ 5 份），最晚剩 15 秒；两腿都付 taker 费。卖不掉的持有到结算（另计占比）。每份 ± 按市场聚类的标准误（笔数 / 市场数）。这是 kacho 每秒盘口上最好的那一族（ROUNDTRIP.md 第 1 族）在真实延迟下的复核。

## 每个市场第一次

| 急动 | h 秒 | 顺急动 | 反方向 | 随机时刻、随机方向 | 卖不掉、持有到结算 |
|---|---:|---:|---:|---:|---:|
| ≥ 2 bp | 5 | -3.24¢ ±0.70（340 / 340） | -4.61¢ ±0.67（367 / 367） | -2.97¢ ±0.38（329 / 329） | 4% |
| ≥ 2 bp | 10 | -3.13¢ ±0.72（340 / 340） | -3.88¢ ±0.74（367 / 367） | -2.49¢ ±0.61（329 / 329） | 4% |
| ≥ 2 bp | 20 | -4.01¢ ±0.84（340 / 340） | -4.00¢ ±0.79（367 / 367） | -3.82¢ ±0.85（329 / 329） | 4% |
| ≥ 2 bp | 30 | -4.62¢ ±0.99（340 / 340） | -3.43¢ ±0.95（367 / 367） | -4.36¢ ±0.99（329 / 329） | 3% |
| ≥ 2 bp | 60 | -4.78¢ ±1.22（340 / 340） | -2.37¢ ±1.18（367 / 367） | -3.49¢ ±1.41（329 / 329） | 5% |
| ≥ 4 bp | 5 | -1.06¢ ±1.12（107 / 107） | -7.54¢ ±1.10（118 / 118） | -3.36¢ ±0.49（109 / 109） | 2% |
| ≥ 4 bp | 10 | -0.80¢ ±1.39（107 / 107） | -6.89¢ ±1.50（118 / 118） | -2.05¢ ±1.20（109 / 109） | 4% |
| ≥ 4 bp | 20 | -0.73¢ ±1.62（107 / 107） | -7.15¢ ±1.57（118 / 118） | -3.80¢ ±1.08（109 / 109） | 6% |
| ≥ 4 bp | 30 | -1.77¢ ±2.09（107 / 107） | -5.54¢ ±2.04（118 / 118） | -3.25¢ ±1.46（109 / 109） | 7% |
| ≥ 4 bp | 60 | -2.15¢ ±2.71（107 / 107） | -5.36¢ ±2.50（118 / 118） | -1.90¢ ±2.28（109 / 109） | 6% |
| ≥ 8 bp | 5 | -2.20¢ ±3.43（20 / 20） | -7.06¢ ±3.33（22 / 22） | -4.74¢ ±1.42（16 / 16） | 5% |
| ≥ 8 bp | 10 | -2.84¢ ±3.61（20 / 20） | -2.70¢ ±4.50（22 / 22） | -1.67¢ ±2.14（16 / 16） | 5% |
| ≥ 8 bp | 20 | -3.28¢ ±3.56（20 / 20） | -6.31¢ ±2.92（22 / 22） | -4.08¢ ±3.11（16 / 16） | 0% |
| ≥ 8 bp | 30 | -4.34¢ ±5.45（20 / 20） | -4.97¢ ±5.16（22 / 22） | -4.46¢ ±4.44（16 / 16） | 5% |
| ≥ 8 bp | 60 | -3.74¢ ±6.47（20 / 20） | -5.05¢ ±5.79（22 / 22） | -2.81¢ ±5.32（16 / 16） | 10% |

## 每次（间隔 ≥ 10 秒）

| 急动 | h 秒 | 顺急动 | 反方向 | 随机时刻、随机方向 | 卖不掉、持有到结算 |
|---|---:|---:|---:|---:|---:|
| ≥ 2 bp | 5 | -3.18¢ ±0.33（1,197 / 366） | -4.02¢ ±0.32（1,256 / 386） | -3.18¢ ±0.27（1,252 / 396） | 4% |
| ≥ 2 bp | 10 | -3.03¢ ±0.41（1,197 / 366） | -3.77¢ ±0.39（1,256 / 386） | -2.67¢ ±0.40（1,252 / 396） | 5% |
| ≥ 2 bp | 20 | -2.38¢ ±0.58（1,197 / 366） | -4.51¢ ±0.54（1,256 / 386） | -2.60¢ ±0.44（1,252 / 396） | 6% |
| ≥ 2 bp | 30 | -2.31¢ ±0.57（1,197 / 366） | -4.38¢ ±0.55（1,256 / 386） | -2.52¢ ±0.53（1,252 / 396） | 7% |
| ≥ 2 bp | 60 | -2.77¢ ±0.78（1,197 / 366） | -3.51¢ ±0.71（1,256 / 386） | -2.47¢ ±0.72（1,252 / 396） | 12% |
| ≥ 4 bp | 5 | -1.29¢ ±0.78（185 / 115） | -5.92¢ ±0.90（200 / 124） | -3.34¢ ±0.38（204 / 130） | 4% |
| ≥ 4 bp | 10 | -0.74¢ ±0.94（185 / 115） | -5.95¢ ±1.06（200 / 124） | -2.73¢ ±0.77（204 / 130） | 7% |
| ≥ 4 bp | 20 | -0.91¢ ±1.23（185 / 115） | -5.66¢ ±1.26（200 / 124） | -3.83¢ ±0.75（204 / 130） | 9% |
| ≥ 4 bp | 30 | -1.54¢ ±1.31（185 / 115） | -4.73¢ ±1.28（200 / 124） | -3.21¢ ±1.01（204 / 130） | 10% |
| ≥ 4 bp | 60 | -2.03¢ ±1.75（185 / 115） | -4.62¢ ±1.62（200 / 124） | -3.34¢ ±1.49（204 / 130） | 13% |
| ≥ 8 bp | 5 | -0.87¢ ±2.99（24 / 22） | -7.20¢ ±2.72（28 / 24） | -3.17¢ ±1.40（24 / 18） | 8% |
| ≥ 8 bp | 10 | -1.51¢ ±3.20（24 / 22） | -3.44¢ ±3.81（28 / 24） | -0.82¢ ±1.64（24 / 18） | 8% |
| ≥ 8 bp | 20 | -1.59¢ ±3.19（24 / 22） | -6.22¢ ±2.46（28 / 24） | -2.69¢ ±2.25（24 / 18） | 4% |
| ≥ 8 bp | 30 | -2.78¢ ±4.68（24 / 22） | -4.51¢ ±4.27（28 / 24） | -3.77¢ ±3.63（24 / 18） | 4% |
| ≥ 8 bp | 60 | -2.61¢ ±5.41（24 / 22） | -3.52¢ ±5.42（28 / 24） | -2.90¢ ±3.80（24 / 18） | 8% |

## 前后两半（顺急动，每次）

| 急动 | h 秒 | 前半 | 后半 |
|---|---:|---:|---:|
| ≥ 2 bp | 5 | -2.78¢ ±0.39（639 / 200） | -3.64¢ ±0.55（558 / 166） |
| ≥ 2 bp | 10 | -2.51¢ ±0.49（639 / 200） | -3.64¢ ±0.68（558 / 166） |
| ≥ 2 bp | 20 | -2.11¢ ±0.59（639 / 200） | -2.68¢ ±1.04（558 / 166） |
| ≥ 4 bp | 5 | -0.79¢ ±0.86（88 / 59） | -1.74¢ ±1.27（97 / 56） |
| ≥ 4 bp | 10 | -0.76¢ ±0.92（88 / 59） | -0.72¢ ±1.60（97 / 56） |
| ≥ 4 bp | 20 | -0.29¢ ±1.25（88 / 59） | -1.48¢ ±2.05（97 / 56） |
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
