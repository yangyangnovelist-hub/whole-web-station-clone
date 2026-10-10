# ETH、SOL、XRP、DOGE 5m 上的 G/H：币安跳变后吃 Polymarket 的旧卖一（GitHub 前向录制的毫秒盘口）

> 设计见 [ALTS.md](../ALTS.md)（2026-10-04 20:37 UTC 提交，在任何运行之前）；脚本 alts.py。只用行情数据，纸面研究，不下单。

规则与 BTC 曲线（forward-latency-curve.md，forward_variants.py）同一段代码（latency.gated_trades），币种显式传入：该币自己的币安 USDT 逐笔成交 2σ 触发（剩 240–15 秒），公平价 − 卖一 − 手续费 ≥ 12¢，延迟 L 后按交易所时间戳盘口的卖一买，taker 费，持有到结算。**G 首笔**：触发前 2 秒中间价已朝买的方向动 ≥ 3¢ 跳过，每个市场第一笔。**H 反向 2 倍**：公平价从触发前 2 秒的中间价加这 2 秒的币安涨跌，每次都买（间隔 ≥ 2 秒），反方向加仓 2 份。延迟包括 150 ms 吃单冻结。每格：每份 ± 按市场聚类的标准误（笔数）。

分段（按市场开始时间，UTC）：独立段 = 2026-10-01 02:30 起、2026-10-04 21:30 之前（只描述）；前向段 = 2026-10-04 21:30 起。

## 判定（入选和前向判定都写入 alts.verdict.md，写过的不再改）

- 入选 eth：独立段（2026-10-01 02:30 UTC 起、2026-10-04 21:30 UTC 之前开始，这次运行已下载的市场，最后一笔触发于 2026-10-04 14:32 UTC）G 首笔 0.4 秒 169 笔，每份 -1.60¢ ±3.40 → **不入选**（事先写死的条件：每份 > 0）。（定于 2026-10-04 20:59 UTC；录制段 16 个：36807987110, 36835222993, 36869869857, 36912544596, 36946930986, 36972067946, 37002779006, 37041041140, 37075141917, 37095301195, 37111395430, 37128550183, 37146417247, 37164037946, 37179397738, 37194238626）
- 判定 eth：没入选，前向段只描述，不判定。
- 入选 sol：独立段（2026-10-01 02:30 UTC 起、2026-10-04 21:30 UTC 之前开始，这次运行已下载的市场，最后一笔触发于 2026-10-04 14:22 UTC）G 首笔 0.4 秒 89 笔，每份 -2.56¢ ±4.49 → **不入选**（事先写死的条件：每份 > 0）。（定于 2026-10-04 20:59 UTC；录制段 16 个：36807987110, 36835222993, 36869869857, 36912544596, 36946930986, 36972067946, 37002779006, 37041041140, 37075141917, 37095301195, 37111395430, 37128550183, 37146417247, 37164037946, 37179397738, 37194238626）
- 判定 sol：没入选，前向段只描述，不判定。
- 入选 xrp：独立段（2026-10-01 02:30 UTC 起、2026-10-04 21:30 UTC 之前开始，这次运行已下载的市场，最后一笔触发于 2026-10-04 14:22 UTC）G 首笔 0.4 秒 121 笔，每份 -1.80¢ ±3.42 → **不入选**（事先写死的条件：每份 > 0）。（定于 2026-10-04 20:59 UTC；录制段 16 个：36807987110, 36835222993, 36869869857, 36912544596, 36946930986, 36972067946, 37002779006, 37041041140, 37075141917, 37095301195, 37111395430, 37128550183, 37146417247, 37164037946, 37179397738, 37194238626）
- 判定 xrp：没入选，前向段只描述，不判定。
- 入选 doge：独立段（2026-10-01 02:30 UTC 起、2026-10-04 21:30 UTC 之前开始，这次运行已下载的市场，最后一笔触发于 2026-10-04 14:08 UTC）G 首笔 0.4 秒 98 笔，每份 -4.83¢ ±3.89 → **不入选**（事先写死的条件：每份 > 0）。（定于 2026-10-04 20:59 UTC；录制段 16 个：36807987110, 36835222993, 36869869857, 36912544596, 36946930986, 36972067946, 37002779006, 37041041140, 37075141917, 37095301195, 37111395430, 37128550183, 37146417247, 37164037946, 37179397738, 37194238626）
- 判定 doge：没入选，前向段只描述，不判定。

## 150 ms 吃单冻结（CLOB /clob-markets 的 itode）

| 币种 | 查的市场 | itode | tick | 费率 |
|---|---|---|---:|---:|
| btc | `btc-updown-5m-1791147300` | true（有 150 ms 冻结） | 0.01 | 0.07 |
| eth | `eth-updown-5m-1791147300` | true（有 150 ms 冻结） | 0.01 | 0.07 |
| sol | `sol-updown-5m-1791147300` | true（有 150 ms 冻结） | 0.01 | 0.07 |
| xrp | `xrp-updown-5m-1791147300` | true（有 150 ms 冻结） | 0.001 | 0.07 |
| doge | `doge-updown-5m-1791147300` | true（有 150 ms 冻结） | 0.001 | 0.07 |

## 录到的市场

| 币种 | 段 | 有盘口的市场 | 开始于（UTC） | 折算天数 |
|---|---|---:|---|---:|
| BTC（对照） | 独立段 | 866 | 10-01 02:50 – 10-04 14:30 | 3.01 |
| BTC（对照） | 前向段 | 0 | – | 0.00 |
| ETH | 独立段 | 866 | 10-01 02:50 – 10-04 14:30 | 3.01 |
| ETH | 前向段 | 0 | – | 0.00 |
| SOL | 独立段 | 865 | 10-01 02:50 – 10-04 14:30 | 3.00 |
| SOL | 前向段 | 0 | – | 0.00 |
| XRP | 独立段 | 866 | 10-01 02:50 – 10-04 14:25 | 3.01 |
| XRP | 前向段 | 0 | – | 0.00 |
| DOGE | 独立段 | 867 | 10-01 02:50 – 10-04 14:30 | 3.01 |
| DOGE | 前向段 | 0 | – | 0.00 |

折算天数 = 有盘口的市场数 × 5 分钟 / 24 小时（录制只覆盖每天的一部分，下面的“每天”都按录满一整天折算）。

## 表 1a G 首笔：每份随延迟（独立段）

| 延迟 | BTC（对照） | ETH | SOL | XRP | DOGE |
|---|---:|---:|---:|---:|---:|
| 0.2 秒 | +17.6¢ ±2.5（293） | +2.4¢ ±3.0（216） | +14.4¢ ±4.0（136） | +1.1¢ ±3.1（144） | +4.6¢ ±3.7（122） |
| 0.25 秒 | +15.9¢ ±2.6（262） | +1.5¢ ±3.1（196） | +7.3¢ ±4.6（101） | -2.7¢ ±3.3（129） | -1.0¢ ±3.9（103） |
| 0.3 秒 | +10.4¢ ±2.9（213） | +0.8¢ ±3.2（183） | +4.1¢ ±4.6（90） | -3.2¢ ±3.3（121） | -3.8¢ ±4.0（96） |
| 0.35 秒 | +7.2¢ ±3.0（189） | -1.7¢ ±3.3（173） | -0.7¢ ±4.6（89） | -2.8¢ ±3.4（118） | -4.0¢ ±4.0（96） |
| 0.4 秒 | +6.0¢ ±3.1（178） | -1.6¢ ±3.4（169） | -2.6¢ ±4.5（89） | -1.8¢ ±3.4（121） | -4.8¢ ±3.9（98） |
| 0.45 秒 | +4.6¢ ±3.1（165） | -3.7¢ ±3.3（170） | -1.7¢ ±4.5（91） | -1.8¢ ±3.4（121） | -5.9¢ ±3.9（101） |
| 0.5 秒 | +3.7¢ ±3.3（157） | -3.9¢ ±3.3（169） | -2.3¢ ±4.4（91） | -1.2¢ ±3.4（121） | -4.9¢ ±3.8（102） |

## 表 1b H 反向 2 倍：每份随延迟（独立段）

| 延迟 | BTC（对照） | ETH | SOL | XRP | DOGE |
|---|---:|---:|---:|---:|---:|
| 0.2 秒 | +17.6¢ ±1.8（560） | +3.8¢ ±2.4（420） | +9.3¢ ±2.9（253） | +1.0¢ ±2.6（277） | -4.3¢ ±2.3（263） |
| 0.25 秒 | +17.0¢ ±2.0（483） | +1.3¢ ±2.4（383） | +5.4¢ ±3.4（192） | +0.2¢ ±2.7（256） | -8.2¢ ±2.5（236） |
| 0.3 秒 | +12.1¢ ±2.2（382） | -0.7¢ ±2.4（372） | +4.5¢ ±3.6（183） | -0.7¢ ±2.7（245） | -9.2¢ ±2.6（232） |
| 0.35 秒 | +9.3¢ ±2.4（332） | -0.4¢ ±2.5（371） | +3.0¢ ±3.5（187） | -1.8¢ ±2.7（241） | -8.8¢ ±2.6（232） |
| 0.4 秒 | +7.3¢ ±2.4（323） | -1.1¢ ±2.5（360） | +2.9¢ ±3.4（188） | -1.6¢ ±2.7（242） | -7.8¢ ±2.5（235） |
| 0.45 秒 | +5.1¢ ±2.5（313） | +0.0¢ ±2.5（360） | +0.5¢ ±3.4（189） | -1.9¢ ±2.7（239） | -7.5¢ ±2.5（239） |
| 0.5 秒 | +5.0¢ ±2.6（307） | -1.1¢ ±2.6（357） | -1.1¢ ±3.3（193） | -1.1¢ ±2.7（244） | -7.0¢ ±2.5（248） |

## 表 2 频率和每天美元（独立段）

列：笔数 | 有成交的市场 | 每天有成交的市场 | 每天笔数 | 每份 ± 聚类标准误 | 单侧 p | 胜率 | 平均买入价 | 每天美元（每笔 5 份）| 每天美元（每笔 min(20, 卖一挂单量) 份）。H 的份数、胜率和均价按权重算（反向加仓 2 倍）。

| 币种 | 规则 | 延迟 | 笔数 | 有成交的市场 | 市场/天 | 笔/天 | 每份 | p | 胜率 | 均价 | $/天（5 份） | $/天（min(20, 挂单量)） |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| BTC（对照） | G 首笔 | 0.3 秒 | 213 | 213 | 70.8 | 70.8 | +10.38¢ ±2.89 | 0.0002 | 53.5% | 0.419 | +36.76 | +118.11 |
| BTC（对照） | G 首笔 | 0.4 秒 | 178 | 178 | 59.2 | 59.2 | +6.00¢ ±3.05 | 0.0246 | 50.6% | 0.433 | +17.76 | +52.20 |
| BTC（对照） | G 首笔 | 0.5 秒 | 157 | 157 | 52.2 | 52.2 | +3.67¢ ±3.28 | 0.1317 | 49.0% | 0.441 | +9.57 | +24.10 |
| BTC（对照） | H 反向 2 倍 | 0.3 秒 | 382 | 263 | 87.5 | 127.0 | +12.06¢ ±2.18 | 0.0000 | 57.0% | 0.436 | +87.66 | +275.58 |
| BTC（对照） | H 反向 2 倍 | 0.4 秒 | 323 | 222 | 73.8 | 107.4 | +7.29¢ ±2.42 | 0.0013 | 53.8% | 0.452 | +44.36 | +146.62 |
| BTC（对照） | H 反向 2 倍 | 0.5 秒 | 307 | 212 | 70.5 | 102.1 | +5.05¢ ±2.58 | 0.0251 | 52.5% | 0.461 | +28.96 | +92.95 |
| ETH | G 首笔 | 0.3 秒 | 183 | 183 | 60.9 | 60.9 | +0.81¢ ±3.16 | 0.3990 | 38.8% | 0.368 | +2.46 | +16.24 |
| ETH | G 首笔 | 0.4 秒 | 169 | 169 | 56.2 | 56.2 | -1.60¢ ±3.40 | 1.0000 | 36.1% | 0.365 | -4.49 | -1.01 |
| ETH | G 首笔 | 0.5 秒 | 169 | 169 | 56.2 | 56.2 | -3.89¢ ±3.32 | 1.0000 | 33.7% | 0.364 | -10.92 | -25.54 |
| ETH | H 反向 2 倍 | 0.3 秒 | 372 | 252 | 83.8 | 123.7 | -0.67¢ ±2.44 | 1.0000 | 39.7% | 0.391 | -4.85 | +29.96 |
| ETH | H 反向 2 倍 | 0.4 秒 | 360 | 239 | 79.5 | 119.7 | -1.09¢ ±2.54 | 1.0000 | 39.2% | 0.390 | -7.66 | -35.00 |
| ETH | H 反向 2 倍 | 0.5 秒 | 357 | 239 | 79.5 | 118.7 | -1.08¢ ±2.56 | 1.0000 | 38.3% | 0.381 | -7.46 | +11.42 |
| SOL | G 首笔 | 0.3 秒 | 90 | 90 | 30.0 | 30.0 | +4.11¢ ±4.64 | 0.1881 | 37.8% | 0.324 | +6.15 | +19.17 |
| SOL | G 首笔 | 0.4 秒 | 89 | 89 | 29.6 | 29.6 | -2.56¢ ±4.49 | 1.0000 | 31.5% | 0.328 | -3.79 | +4.88 |
| SOL | G 首笔 | 0.5 秒 | 91 | 91 | 30.3 | 30.3 | -2.32¢ ±4.42 | 1.0000 | 31.9% | 0.329 | -3.51 | -11.26 |
| SOL | H 反向 2 倍 | 0.3 秒 | 183 | 131 | 43.6 | 60.9 | +4.50¢ ±3.56 | 0.1031 | 42.5% | 0.367 | +14.99 | +42.12 |
| SOL | H 反向 2 倍 | 0.4 秒 | 188 | 131 | 43.6 | 62.6 | +2.89¢ ±3.45 | 0.2012 | 42.3% | 0.381 | +9.99 | +33.05 |
| SOL | H 反向 2 倍 | 0.5 秒 | 193 | 135 | 44.9 | 64.3 | -1.06¢ ±3.31 | 1.0000 | 38.4% | 0.382 | -3.82 | -22.13 |
| XRP | G 首笔 | 0.3 秒 | 121 | 121 | 40.2 | 40.2 | -3.25¢ ±3.27 | 1.0000 | 27.3% | 0.294 | -6.53 | -14.77 |
| XRP | G 首笔 | 0.4 秒 | 121 | 121 | 40.2 | 40.2 | -1.80¢ ±3.42 | 1.0000 | 28.1% | 0.288 | -3.61 | -12.46 |
| XRP | G 首笔 | 0.5 秒 | 121 | 121 | 40.2 | 40.2 | -1.18¢ ±3.41 | 1.0000 | 29.8% | 0.298 | -2.37 | +2.35 |
| XRP | H 反向 2 倍 | 0.3 秒 | 245 | 176 | 58.5 | 81.5 | -0.75¢ ±2.65 | 1.0000 | 37.1% | 0.367 | -3.47 | -12.98 |
| XRP | H 反向 2 倍 | 0.4 秒 | 242 | 173 | 57.5 | 80.5 | -1.55¢ ±2.70 | 1.0000 | 34.5% | 0.349 | -7.11 | -40.74 |
| XRP | H 反向 2 倍 | 0.5 秒 | 244 | 173 | 57.5 | 81.1 | -1.12¢ ±2.74 | 1.0000 | 34.8% | 0.347 | -5.14 | -7.61 |
| DOGE | G 首笔 | 0.3 秒 | 96 | 96 | 31.9 | 31.9 | -3.76¢ ±4.00 | 1.0000 | 26.0% | 0.287 | -5.99 | -2.79 |
| DOGE | G 首笔 | 0.4 秒 | 98 | 98 | 32.6 | 32.6 | -4.83¢ ±3.89 | 1.0000 | 23.5% | 0.272 | -7.87 | -11.96 |
| DOGE | G 首笔 | 0.5 秒 | 102 | 102 | 33.9 | 33.9 | -4.91¢ ±3.76 | 1.0000 | 24.5% | 0.283 | -8.32 | -15.84 |
| DOGE | H 反向 2 倍 | 0.3 秒 | 232 | 149 | 49.5 | 77.1 | -9.23¢ ±2.59 | 1.0000 | 27.8% | 0.358 | -39.71 | -116.84 |
| DOGE | H 反向 2 倍 | 0.4 秒 | 235 | 148 | 49.2 | 78.1 | -7.79¢ ±2.53 | 1.0000 | 29.3% | 0.359 | -34.40 | -110.80 |
| DOGE | H 反向 2 倍 | 0.5 秒 | 248 | 156 | 51.8 | 82.4 | -6.99¢ ±2.47 | 1.0000 | 29.9% | 0.357 | -32.64 | -91.68 |

## 表 3 对照（独立段）

反方向：同一笔的同一时刻买另一边的卖一（不设门槛）；随机：同一市场剩 240–15 秒内随机时刻、随机方向，L 秒后按卖一买。份数同规则。

| 币种 | 规则 | 延迟 | 规则本身 | 对照：反方向 | 对照：随机时刻、随机方向 |
|---|---|---|---:|---:|---:|
| BTC（对照） | G 首笔 | 0.3 秒 | +10.4¢ ±2.9（213） | -16.1¢ ±2.9（211） | +0.5¢ ±3.8（173） |
| BTC（对照） | G 首笔 | 0.4 秒 | +6.0¢ ±3.1（178） | -11.4¢ ±3.1（174） | +2.2¢ ±4.2（139） |
| BTC（对照） | G 首笔 | 0.5 秒 | +3.7¢ ±3.3（157） | -8.3¢ ±3.3（155） | +5.0¢ ±4.2（124） |
| BTC（对照） | H 反向 2 倍 | 0.3 秒 | +12.1¢ ±2.2（382） | -18.2¢ ±2.2（379） | +1.7¢ ±3.1（317） |
| BTC（对照） | H 反向 2 倍 | 0.4 秒 | +7.3¢ ±2.4（323） | -12.8¢ ±2.5（320） | -0.7¢ ±3.0（270） |
| BTC（对照） | H 反向 2 倍 | 0.5 秒 | +5.0¢ ±2.6（307） | -9.9¢ ±2.6（306） | +0.9¢ ±3.5（259） |
| ETH | G 首笔 | 0.3 秒 | +0.8¢ ±3.2（183） | -5.1¢ ±3.2（182） | +0.9¢ ±3.5（146） |
| ETH | G 首笔 | 0.4 秒 | -1.6¢ ±3.4（169） | -2.4¢ ±3.4（168） | -0.5¢ ±3.7（134） |
| ETH | G 首笔 | 0.5 秒 | -3.9¢ ±3.3（169） | +0.1¢ ±3.3（168） | -3.7¢ ±3.7（134） |
| ETH | H 反向 2 倍 | 0.3 秒 | -0.7¢ ±2.4（372） | -4.3¢ ±2.5（370） | -1.6¢ ±2.8（324） |
| ETH | H 反向 2 倍 | 0.4 秒 | -1.1¢ ±2.5（360） | -3.4¢ ±2.6（358） | +0.3¢ ±2.7（307） |
| ETH | H 反向 2 倍 | 0.5 秒 | -1.1¢ ±2.6（357） | -3.3¢ ±2.6（354） | +0.5¢ ±2.8（308） |
| SOL | G 首笔 | 0.3 秒 | +4.1¢ ±4.6（90） | -10.4¢ ±4.9（88） | +8.1¢ ±5.4（79） |
| SOL | G 首笔 | 0.4 秒 | -2.6¢ ±4.5（89） | -3.1¢ ±4.8（86） | +8.4¢ ±5.6（74） |
| SOL | G 首笔 | 0.5 秒 | -2.3¢ ±4.4（91） | -3.1¢ ±4.6（88） | +10.0¢ ±5.2（77） |
| SOL | H 反向 2 倍 | 0.3 秒 | +4.5¢ ±3.6（183） | -11.7¢ ±3.7（178） | -9.4¢ ±4.8（166） |
| SOL | H 反向 2 倍 | 0.4 秒 | +2.9¢ ±3.4（188） | -9.3¢ ±3.6（184） | -8.1¢ ±4.8（171） |
| SOL | H 反向 2 倍 | 0.5 秒 | -1.1¢ ±3.3（193） | -5.4¢ ±3.5（190） | -5.5¢ ±4.8（170） |
| XRP | G 首笔 | 0.3 秒 | -3.2¢ ±3.3（121） | -3.8¢ ±3.5（115） | -2.2¢ ±4.1（110） |
| XRP | G 首笔 | 0.4 秒 | -1.8¢ ±3.4（121） | -4.6¢ ±3.6（116） | -3.3¢ ±4.3（105） |
| XRP | G 首笔 | 0.5 秒 | -1.2¢ ±3.4（121） | -5.5¢ ±3.6（116） | +1.3¢ ±4.2（106） |
| XRP | H 反向 2 倍 | 0.3 秒 | -0.7¢ ±2.7（245） | -7.9¢ ±2.7（238） | -1.0¢ ±3.3（222） |
| XRP | H 反向 2 倍 | 0.4 秒 | -1.6¢ ±2.7（242） | -6.3¢ ±2.8（235） | -1.5¢ ±3.4（219） |
| XRP | H 反向 2 倍 | 0.5 秒 | -1.1¢ ±2.7（244） | -6.4¢ ±2.9（237） | -2.0¢ ±3.4（219） |
| DOGE | G 首笔 | 0.3 秒 | -3.8¢ ±4.0（96） | -5.1¢ ±4.4（86） | -0.9¢ ±5.3（86） |
| DOGE | G 首笔 | 0.4 秒 | -4.8¢ ±3.9（98） | -3.0¢ ±4.2（90） | -2.9¢ ±5.2（85） |
| DOGE | G 首笔 | 0.5 秒 | -4.9¢ ±3.8（102） | -2.3¢ ±4.1（93） | -4.0¢ ±5.2（86） |
| DOGE | H 反向 2 倍 | 0.3 秒 | -9.2¢ ±2.6（232） | +0.8¢ ±2.8（211） | -7.5¢ ±3.6（207） |
| DOGE | H 反向 2 倍 | 0.4 秒 | -7.8¢ ±2.5（235） | -0.3¢ ±2.8（216） | -6.9¢ ±3.7（213） |
| DOGE | H 反向 2 倍 | 0.5 秒 | -7.0¢ ±2.5（248） | -1.3¢ ±2.6（229） | -5.9¢ ±3.7（219） |

## 表 4 前后两半（独立段，0.4 秒，按市场开始时间把每个币种的市场分成两半）

| 币种 | 前半开始于 | 后半开始于 | G 首笔 前半 | G 首笔 后半 | H 反向 2 倍 前半 | H 反向 2 倍 后半 |
|---|---|---|---:|---:|---:|---:|
| BTC（对照） | 10-01 02:50 – 10-02 21:45 | 10-02 21:50 – 10-04 14:30 | +1.1¢ ±4.2（96） | +11.8¢ ±4.3（82） | +6.4¢ ±3.3（207） | +8.8¢ ±3.3（116） |
| ETH | 10-01 02:50 – 10-02 21:50 | 10-02 21:55 – 10-04 14:30 | -6.4¢ ±4.6（72） | +2.0¢ ±4.8（97） | -8.0¢ ±3.2（190） | +6.9¢ ±3.6（170） |
| SOL | 10-01 02:50 – 10-02 21:50 | 10-02 21:55 – 10-04 14:30 | -7.2¢ ±6.9（33） | +0.2¢ ±5.9（56） | +0.9¢ ±4.9（79） | +4.3¢ ±4.7（109） |
| XRP | 10-01 02:50 – 10-02 21:50 | 10-02 21:55 – 10-04 14:25 | -0.8¢ ±4.7（59） | -2.8¢ ±5.0（62） | -2.6¢ ±4.0（126） | -0.4¢ ±3.7（116） |
| DOGE | 10-01 02:50 – 10-02 21:45 | 10-02 21:50 – 10-04 14:30 | -4.8¢ ±6.4（37） | -4.8¢ ±5.0（61） | -0.3¢ ±4.7（67） | -10.5¢ ±3.0（168） |

## 表 5 前向段（2026-10-04 21:30 UTC 起开始的市场；只描述，判定见上）

| 币种 | 延迟 | G 首笔 | H 反向 2 倍 | G 首笔 $/天（5 份） | H 反向 2 倍 $/天（5 份） |
|---|---|---:|---:|---:|---:|
| BTC（对照） | 0.3 秒 | – | – | – | – |
| BTC（对照） | 0.4 秒 | – | – | – | – |
| BTC（对照） | 0.5 秒 | – | – | – | – |
| ETH | 0.3 秒 | – | – | – | – |
| ETH | 0.4 秒 | – | – | – | – |
| ETH | 0.5 秒 | – | – | – | – |
| SOL | 0.3 秒 | – | – | – | – |
| SOL | 0.4 秒 | – | – | – | – |
| SOL | 0.5 秒 | – | – | – | – |
| XRP | 0.3 秒 | – | – | – | – |
| XRP | 0.4 秒 | – | – | – | – |
| XRP | 0.5 秒 | – | – | – | – |
| DOGE | 0.3 秒 | – | – | – | – |
| DOGE | 0.4 秒 | – | – | – | – |
| DOGE | 0.5 秒 | – | – | – | – |

## 备注

- 只用行情，没有下单。成交价和挂单量只用成交时刻及以前的盘口行；“盘口在跑”和“没断线”会看之后的行，只用来剔除录制故障。
- 每段录制单独计算（σ、盘口、断线记录都是这一段的）；同一市场出现在两段录制里时，G 首笔取最早的一笔，H 的成交按触发时刻去重；对照只跟着留下的成交。录制里没有官方结果的市场不计。
- 下面“candidates dropped”的条数只计 G 首笔 0.4 秒（被判定的规则）。
- btc 36671472407: skipped (FileNotFoundError: no BINANCE_WS_TRADE events under /home/runner/work/_temp/rec/36671472407/x/bundle-btc/latency)
- btc 36704245701: skipped (FileNotFoundError: no BINANCE_WS_TRADE events under /home/runner/work/_temp/rec/36704245701/x/bundle-btc/latency)
- btc 36740728504: skipped (FileNotFoundError: no BINANCE_WS_TRADE events under /home/runner/work/_temp/rec/36740728504/x/bundle-btc/latency)
- btc 36781277830: no markets from 2026-10-01 02:30 with book and spot data
- btc 36869869857: 18 candidates dropped because the CLOB socket closed between the quote and the order (3 disconnects in the recording; test C counts its judged lag only)
- btc 36972067946: 136 candidates dropped because the CLOB socket closed between the quote and the order (15 disconnects in the recording; test C counts its judged lag only)
- btc 37002779006: 1593 candidates dropped because the CLOB socket closed between the quote and the order (107 disconnects in the recording; test C counts its judged lag only)
- btc 37041041140: 781 candidates dropped because the CLOB socket closed between the quote and the order (70 disconnects in the recording; test C counts its judged lag only)
- btc 37075141917: 763 candidates dropped because the CLOB socket closed between the quote and the order (80 disconnects in the recording; test C counts its judged lag only)
- btc 37095301195: 1007 candidates dropped because the CLOB socket closed between the quote and the order (76 disconnects in the recording; test C counts its judged lag only)
- btc 37111395430: 388 candidates dropped because the CLOB socket closed between the quote and the order (77 disconnects in the recording; test C counts its judged lag only)
- btc 37128550183: 308 candidates dropped because the CLOB socket closed between the quote and the order (58 disconnects in the recording; test C counts its judged lag only)
- btc 37146417247: 1288 candidates dropped because the CLOB socket closed between the quote and the order (102 disconnects in the recording; test C counts its judged lag only)
- btc 37164037946: 545 candidates dropped because the CLOB socket closed between the quote and the order (61 disconnects in the recording; test C counts its judged lag only)
- btc 37179397738: 451 candidates dropped because the CLOB socket closed between the quote and the order (70 disconnects in the recording; test C counts its judged lag only)
- btc 37194238626: 679 candidates dropped because the CLOB socket closed between the quote and the order (78 disconnects in the recording; test C counts its judged lag only)
- eth 36671472407: skipped (FileNotFoundError: no BINANCE_WS_TRADE events under /home/runner/work/_temp/rec/36671472407/x/bundle-eth/latency)
- eth 36704245701: skipped (FileNotFoundError: no BINANCE_WS_TRADE events under /home/runner/work/_temp/rec/36704245701/x/bundle-eth/latency)
- eth 36740728504: skipped (FileNotFoundError: no BINANCE_WS_TRADE events under /home/runner/work/_temp/rec/36740728504/x/bundle-eth/latency)
- eth 36781277830: no markets from 2026-10-01 02:30 with book and spot data
- eth 36869869857: 9 candidates dropped because the CLOB socket closed between the quote and the order (3 disconnects in the recording; test C counts its judged lag only)
- eth 36972067946: 52 candidates dropped because the CLOB socket closed between the quote and the order (15 disconnects in the recording; test C counts its judged lag only)
- eth 37002779006: 1285 candidates dropped because the CLOB socket closed between the quote and the order (107 disconnects in the recording; test C counts its judged lag only)
- eth 37041041140: 407 candidates dropped because the CLOB socket closed between the quote and the order (70 disconnects in the recording; test C counts its judged lag only)
- eth 37075141917: 155 candidates dropped because the CLOB socket closed between the quote and the order (80 disconnects in the recording; test C counts its judged lag only)
- eth 37095301195: 552 candidates dropped because the CLOB socket closed between the quote and the order (76 disconnects in the recording; test C counts its judged lag only)
- eth 37111395430: 186 candidates dropped because the CLOB socket closed between the quote and the order (77 disconnects in the recording; test C counts its judged lag only)
- eth 37128550183: 73 candidates dropped because the CLOB socket closed between the quote and the order (58 disconnects in the recording; test C counts its judged lag only)
- eth 37146417247: 325 candidates dropped because the CLOB socket closed between the quote and the order (102 disconnects in the recording; test C counts its judged lag only)
- eth 37164037946: 119 candidates dropped because the CLOB socket closed between the quote and the order (61 disconnects in the recording; test C counts its judged lag only)
- eth 37179397738: 287 candidates dropped because the CLOB socket closed between the quote and the order (70 disconnects in the recording; test C counts its judged lag only)
- eth 37194238626: 305 candidates dropped because the CLOB socket closed between the quote and the order (78 disconnects in the recording; test C counts its judged lag only)
- sol 36671472407: skipped (FileNotFoundError: no BINANCE_WS_TRADE events under /home/runner/work/_temp/rec/36671472407/x/bundle-sol/latency)
- sol 36704245701: skipped (FileNotFoundError: no BINANCE_WS_TRADE events under /home/runner/work/_temp/rec/36704245701/x/bundle-sol/latency)
- sol 36740728504: skipped (FileNotFoundError: no BINANCE_WS_TRADE events under /home/runner/work/_temp/rec/36740728504/x/bundle-sol/latency)
- sol 36781277830: no markets from 2026-10-01 02:30 with book and spot data
- sol 36807987110: 3 candidates dropped because the CLOB socket closed between the quote and the order (1 disconnects in the recording; test C counts its judged lag only)
- sol 36835222993: 4 candidates dropped because the CLOB socket closed between the quote and the order (3 disconnects in the recording; test C counts its judged lag only)
- sol 36946930986: 1 candidates dropped because the CLOB socket closed between the quote and the order (1 disconnects in the recording; test C counts its judged lag only)
- sol 36972067946: 20 candidates dropped because the CLOB socket closed between the quote and the order (15 disconnects in the recording; test C counts its judged lag only)
- sol 37002779006: 339 candidates dropped because the CLOB socket closed between the quote and the order (107 disconnects in the recording; test C counts its judged lag only)
- sol 37041041140: 77 candidates dropped because the CLOB socket closed between the quote and the order (70 disconnects in the recording; test C counts its judged lag only)
- sol 37075141917: 113 candidates dropped because the CLOB socket closed between the quote and the order (80 disconnects in the recording; test C counts its judged lag only)
- sol 37095301195: 93 candidates dropped because the CLOB socket closed between the quote and the order (76 disconnects in the recording; test C counts its judged lag only)
- sol 37111395430: 102 candidates dropped because the CLOB socket closed between the quote and the order (77 disconnects in the recording; test C counts its judged lag only)
- sol 37128550183: 22 candidates dropped because the CLOB socket closed between the quote and the order (58 disconnects in the recording; test C counts its judged lag only)
- sol 37146417247: 144 candidates dropped because the CLOB socket closed between the quote and the order (102 disconnects in the recording; test C counts its judged lag only)
- sol 37164037946: 50 candidates dropped because the CLOB socket closed between the quote and the order (61 disconnects in the recording; test C counts its judged lag only)
- sol 37179397738: 117 candidates dropped because the CLOB socket closed between the quote and the order (70 disconnects in the recording; test C counts its judged lag only)
- sol 37194238626: 207 candidates dropped because the CLOB socket closed between the quote and the order (78 disconnects in the recording; test C counts its judged lag only)
- xrp 36671472407: skipped (FileNotFoundError: no BINANCE_WS_TRADE events under /home/runner/work/_temp/rec/36671472407/x/bundle-xrp/latency)
- xrp 36704245701: skipped (FileNotFoundError: no BINANCE_WS_TRADE events under /home/runner/work/_temp/rec/36704245701/x/bundle-xrp/latency)
- xrp 36740728504: skipped (FileNotFoundError: no BINANCE_WS_TRADE events under /home/runner/work/_temp/rec/36740728504/x/bundle-xrp/latency)
- xrp 36781277830: no markets from 2026-10-01 02:30 with book and spot data
- xrp 36807987110: 1 candidates dropped because the CLOB socket closed between the quote and the order (1 disconnects in the recording; test C counts its judged lag only)
- xrp 36869869857: 4 candidates dropped because the CLOB socket closed between the quote and the order (3 disconnects in the recording; test C counts its judged lag only)
- xrp 36972067946: 29 candidates dropped because the CLOB socket closed between the quote and the order (15 disconnects in the recording; test C counts its judged lag only)
- xrp 37002779006: 513 candidates dropped because the CLOB socket closed between the quote and the order (107 disconnects in the recording; test C counts its judged lag only)
- xrp 37041041140: 139 candidates dropped because the CLOB socket closed between the quote and the order (70 disconnects in the recording; test C counts its judged lag only)
- xrp 37075141917: 97 candidates dropped because the CLOB socket closed between the quote and the order (80 disconnects in the recording; test C counts its judged lag only)
- xrp 37095301195: 116 candidates dropped because the CLOB socket closed between the quote and the order (76 disconnects in the recording; test C counts its judged lag only)
- xrp 37111395430: 47 candidates dropped because the CLOB socket closed between the quote and the order (77 disconnects in the recording; test C counts its judged lag only)
- xrp 37128550183: 19 candidates dropped because the CLOB socket closed between the quote and the order (58 disconnects in the recording; test C counts its judged lag only)
- xrp 37146417247: 95 candidates dropped because the CLOB socket closed between the quote and the order (102 disconnects in the recording; test C counts its judged lag only)
- xrp 37164037946: 75 candidates dropped because the CLOB socket closed between the quote and the order (61 disconnects in the recording; test C counts its judged lag only)
- xrp 37179397738: 116 candidates dropped because the CLOB socket closed between the quote and the order (70 disconnects in the recording; test C counts its judged lag only)
- xrp 37194238626: 206 candidates dropped because the CLOB socket closed between the quote and the order (78 disconnects in the recording; test C counts its judged lag only)
- doge 36671472407: skipped (FileNotFoundError: no BINANCE_WS_TRADE events under /home/runner/work/_temp/rec/36671472407/x/bundle-doge/latency)
- doge 36704245701: skipped (FileNotFoundError: no BINANCE_WS_TRADE events under /home/runner/work/_temp/rec/36704245701/x/bundle-doge/latency)
- doge 36740728504: skipped (FileNotFoundError: no BINANCE_WS_TRADE events under /home/runner/work/_temp/rec/36740728504/x/bundle-doge/latency)
- doge 36781277830: no markets from 2026-10-01 02:30 with book and spot data
- doge 36869869857: 8 candidates dropped because the CLOB socket closed between the quote and the order (3 disconnects in the recording; test C counts its judged lag only)
- doge 36912544596: 2 candidates dropped because the CLOB socket closed between the quote and the order (4 disconnects in the recording; test C counts its judged lag only)
- doge 36972067946: 24 candidates dropped because the CLOB socket closed between the quote and the order (15 disconnects in the recording; test C counts its judged lag only)
- doge 37002779006: 223 candidates dropped because the CLOB socket closed between the quote and the order (107 disconnects in the recording; test C counts its judged lag only)
- doge 37041041140: 59 candidates dropped because the CLOB socket closed between the quote and the order (70 disconnects in the recording; test C counts its judged lag only)
- doge 37075141917: 24 candidates dropped because the CLOB socket closed between the quote and the order (80 disconnects in the recording; test C counts its judged lag only)
- doge 37095301195: 88 candidates dropped because the CLOB socket closed between the quote and the order (76 disconnects in the recording; test C counts its judged lag only)
- doge 37111395430: 49 candidates dropped because the CLOB socket closed between the quote and the order (77 disconnects in the recording; test C counts its judged lag only)
- doge 37128550183: 27 candidates dropped because the CLOB socket closed between the quote and the order (58 disconnects in the recording; test C counts its judged lag only)
- doge 37146417247: 56 candidates dropped because the CLOB socket closed between the quote and the order (102 disconnects in the recording; test C counts its judged lag only)
- doge 37164037946: 26 candidates dropped because the CLOB socket closed between the quote and the order (61 disconnects in the recording; test C counts its judged lag only)
- doge 37179397738: 99 candidates dropped because the CLOB socket closed between the quote and the order (70 disconnects in the recording; test C counts its judged lag only)
- doge 37194238626: 167 candidates dropped because the CLOB socket closed between the quote and the order (78 disconnects in the recording; test C counts its judged lag only)
