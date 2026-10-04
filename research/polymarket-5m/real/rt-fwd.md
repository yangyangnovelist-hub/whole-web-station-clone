# 往返族两条规则的前向检验：币安急动后买入、持有到结算（GitHub 前向录制的毫秒盘口）

> 设计见 [ROUNDTRIP.md](../ROUNDTRIP.md) 最后一节“通过之后的前向检验”（2026-10-04 17:20 UTC 写死，在看任何 10 月数据之前）；Hugging Face 复核见 [roundtrip-hf-strict.md](roundtrip-hf-strict.md)；脚本 rt_fwd.py。只用行情数据，纸面研究，不下单。

- **R1** `w1-bp4-all-all10-settle`：币安 BTCUSDT 逐笔成交（按记录机收到的时刻）的对数价相对 1 秒前收到的最后一笔变动 ≥ 4 bp；**R2** `w3-bp8-all-all10-settle`：相对 3 秒前 ≥ 8 bp。参考成交最多早 5 秒。买变动方向，持有到官方结算。
- 同一市场先取第一次触发，之后只取距上一次保留的触发至少 10 秒的；同一规则在同一市场同时最多一笔仓位，持有到结算，所以每个市场最多一笔：保留的触发按时间逐个试，第一个能成交的就是这笔。
- 时序：收到触发成交的时刻 t 之后的第一个整秒决定，决定后 0.5 秒按那一刻的卖一吃单买（Down 的卖一 = 1 − Up 买一，挂单量用 Up 买一的）：卖一 0.02–0.98、至少 5 份、成交快照的买一或卖一价 1 秒内变过、盘口数据流活着（前 30 秒、后 10 秒内有更新）、决定到撮合之间 CLOB 没断线；付 taker 费 0.07·p(1 − p)。剩 15 秒以内不再开仓（决定最晚在开盘后 284 秒）。
- 分段（按市场开始时间，UTC）：独立检验 = 2026-10-01 02:30 至 2026-10-04 17:20 开始的市场（含两端，只报告）；前向 = 2026-10-04 17:20 之后开始的市场，每条规则攒满 300 笔（按触发时间）判定一次：每份 > 0、按市场聚类单侧 p < 0.025、前后两半都为正。

## 判定（前向段，每条规则一次，写入 rt-fwd.verdict.md 后不再重算）

- R1 `w1-bp4-all-all10-settle`：前向段目前 0 笔，不到 300 笔，不判定。
- R2 `w3-bp8-all-all10-settle`：前向段目前 0 笔，不到 300 笔，不判定。

列：笔数 | 市场 | 每天笔数 | 每份（± 按市场聚类的标准误，t） | 胜率 | 平均买入价 | 每天美元（每笔 5 份） | 每天美元（每笔 min(20, 卖一挂单量) 份）。每份、胜率、平均价都是每份（1 份结算为 1 美元）；天数 = 该段有盘口的市场数 × 5 分钟（录制只覆盖每天的一部分）。对照用同样的过滤和“每个市场最多一笔”。

## R1 `w1-bp4-all-all10-settle`

### 独立检验

有盘口的市场 866 个（开始于 10-01 02:50 – 10-04 14:30 UTC，约 3.01 天）；有触发的市场 169 个，保留的触发 342 次，其中顺变动方向能成交的 205 次。

| 买什么 | 笔数 | 市场 | 每天笔数 | 每份 ± 标准误（t） | 胜率 | 平均价 | 每天美元（5 份） | 每天美元（min(20, 挂单量) 份） |
|---|---:|---:|---:|---|---:|---:|---:|---:|
| 规则（顺变动方向，每个市场最多一笔） | 124 | 124 | 41.2 | -10.15¢ ±4.00（t -2.5） | 58.1% | 0.670 | -20.93 | -74.83 |
| 对照：反方向（同一触发时刻） | 126 | 126 | 41.9 | +3.21¢ ±3.84（t +0.8） | 38.1% | 0.337 | +6.73 | +17.35 |
| 对照：随机时刻、随机方向 | 86 | 86 | 28.6 | +3.16¢ ±4.74（t +0.7） | 55.8% | 0.514 | +4.52 | +14.05 |
| 描述：每次触发都买（不限一笔仓位，不判定） | 205 | 124 | 68.2 | -8.91¢ ±3.04（t -2.9） | 57.1% | 0.648 | -30.38 | -111.22 |

### 前向

有盘口的市场 0 个（开始于 无，约 0.00 天）；有触发的市场 0 个，保留的触发 0 次，其中顺变动方向能成交的 0 次。

| 买什么 | 笔数 | 市场 | 每天笔数 | 每份 ± 标准误（t） | 胜率 | 平均价 | 每天美元（5 份） | 每天美元（min(20, 挂单量) 份） |
|---|---:|---:|---:|---|---:|---:|---:|---:|
| 规则（顺变动方向，每个市场最多一笔） | – | – | – | – | – | – | – | – |
| 对照：反方向（同一触发时刻） | – | – | – | – | – | – | – | – |
| 对照：随机时刻、随机方向 | – | – | – | – | – | – | – | – |
| 描述：每次触发都买（不限一笔仓位，不判定） | – | – | – | – | – | – | – | – |

## R2 `w3-bp8-all-all10-settle`

### 独立检验

有盘口的市场 866 个（开始于 10-01 02:50 – 10-04 14:30 UTC，约 3.01 天）；有触发的市场 50 个，保留的触发 95 次，其中顺变动方向能成交的 55 次。

| 买什么 | 笔数 | 市场 | 每天笔数 | 每份 ± 标准误（t） | 胜率 | 平均价 | 每天美元（5 份） | 每天美元（min(20, 挂单量) 份） |
|---|---:|---:|---:|---|---:|---:|---:|---:|
| 规则（顺变动方向，每个市场最多一笔） | 37 | 37 | 12.3 | -17.95¢ ±6.85（t -2.6） | 51.4% | 0.681 | -11.05 | -38.23 |
| 对照：反方向（同一触发时刻） | 40 | 40 | 13.3 | +11.24¢ ±6.48（t +1.7） | 42.5% | 0.300 | +7.48 | +28.26 |
| 对照：随机时刻、随机方向 | 32 | 32 | 10.6 | -3.95¢ ±7.34（t -0.5） | 50.0% | 0.527 | -2.10 | -0.54 |
| 描述：每次触发都买（不限一笔仓位，不判定） | 55 | 37 | 18.3 | -15.15¢ ±6.22（t -2.4） | 50.9% | 0.649 | -13.85 | -50.27 |

### 前向

有盘口的市场 0 个（开始于 无，约 0.00 天）；有触发的市场 0 个，保留的触发 0 次，其中顺变动方向能成交的 0 次。

| 买什么 | 笔数 | 市场 | 每天笔数 | 每份 ± 标准误（t） | 胜率 | 平均价 | 每天美元（5 份） | 每天美元（min(20, 挂单量) 份） |
|---|---:|---:|---:|---|---:|---:|---:|---:|
| 规则（顺变动方向，每个市场最多一笔） | – | – | – | – | – | – | – | – |
| 对照：反方向（同一触发时刻） | – | – | – | – | – | – | – | – |
| 对照：随机时刻、随机方向 | – | – | – | – | – | – | – | – |
| 描述：每次触发都买（不限一笔仓位，不判定） | – | – | – | – | – | – | – | – |

## 备注

- 只用行情，没有下单。成交价、挂单量和“1 秒内变过”只用撮合时刻及以前的盘口行；“数据流活着”和“没断线”会看撮合之后的行，只用来剔除录制故障。币安成交按记录机收到的时刻排序（同一时刻按成交时间）。
- 随机对照：每个保留的触发配一个随机决定秒（开盘后 1–284 秒均匀）和随机方向（按规则和触发时刻各自取随机数），每个市场取第一个能成交的。“每次触发都买”不受一笔仓位限制，只作描述。
- 每段录制单独计算；同一市场出现在两段录制里时，每条规则每种买法取最早的一笔。录制里没有官方结果的市场不计。
- btc 36671472407: skipped (FileNotFoundError: no BINANCE_WS_TRADE events under /home/runner/work/_temp/rec/36671472407/x/bundle-btc/latency)
- btc 36704245701: skipped (FileNotFoundError: no BINANCE_WS_TRADE events under /home/runner/work/_temp/rec/36704245701/x/bundle-btc/latency)
- btc 36740728504: skipped (FileNotFoundError: no BINANCE_WS_TRADE events under /home/runner/work/_temp/rec/36740728504/x/bundle-btc/latency)
- btc 36781277830: no markets from 2026-10-01 02:30 with book and spot data
- btc 36972067946: 2 candidates dropped because the CLOB socket closed between the quote and the order (15 disconnects in the recording; test C counts its judged lag only)
- btc 37002779006: 16 candidates dropped because the CLOB socket closed between the quote and the order (107 disconnects in the recording; test C counts its judged lag only)
- btc 37041041140: 3 candidates dropped because the CLOB socket closed between the quote and the order (70 disconnects in the recording; test C counts its judged lag only)
- btc 37075141917: 4 candidates dropped because the CLOB socket closed between the quote and the order (80 disconnects in the recording; test C counts its judged lag only)
- btc 37095301195: 2 candidates dropped because the CLOB socket closed between the quote and the order (76 disconnects in the recording; test C counts its judged lag only)
- btc 37146417247: 5 candidates dropped because the CLOB socket closed between the quote and the order (102 disconnects in the recording; test C counts its judged lag only)
- btc 37164037946: 2 candidates dropped because the CLOB socket closed between the quote and the order (61 disconnects in the recording; test C counts its judged lag only)
- btc 37194238626: 2 candidates dropped because the CLOB socket closed between the quote and the order (78 disconnects in the recording; test C counts its judged lag only)
