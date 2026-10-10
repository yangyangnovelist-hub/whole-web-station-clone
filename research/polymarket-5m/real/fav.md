# 临近到期的热门一边：同一规则铺到更多市场（FAV.md）

**判定**（每格：挂单每份 > 0、按市场聚类单侧 p < 0.05/10 = 0.005（规则数 × 市场组数 = 2 × 5）、≥ 100 个有成交市场、前后两半都 > 0）：
- 规则 1（NEARCERT，[0.95, 0.99)） × BTC 每小时高于：每份 +1.94¢（挂单），t 1.25，单侧 p 0.106，有成交市场 373，前半 +1.20¢（187 个）/ 后半 +2.81¢（186 个），利润上限 $2.4/天 → **不通过**（单侧 p 0.106 ≥ 0.005）
- 规则 1（NEARCERT，[0.95, 0.99)） × BTC 每小时涨跌：每份 -0.48¢（挂单），t -0.93，单侧 p 0.823，有成交市场 3570，前半 +0.34¢（1793 个）/ 后半 -2.10¢（1777 个），利润上限 $-69.3/天 → **不通过**（每份 -0.48¢ ≤ 0；单侧 p 0.823 ≥ 0.005；前后两半不都为正）
- 规则 1（NEARCERT，[0.95, 0.99)） × ETH 日内：每份 +5.02¢（挂单），t 2.46，单侧 p 0.007，有成交市场 451，前半 +3.84¢（227 个）/ 后半 +7.51¢（224 个），利润上限 $24.7/天 → **不通过**（单侧 p 0.007 ≥ 0.005）
- 规则 1（NEARCERT，[0.95, 0.99)） × SOL 日内：每份 +0.64¢（挂单），t 0.76，单侧 p 0.224，有成交市场 164，前半 +0.18¢（82 个）/ 后半 +1.69¢（82 个），利润上限 $0.5/天 → **不通过**（单侧 p 0.224 ≥ 0.005）
- 规则 1（NEARCERT，[0.95, 0.99)） × XRP 日内：每份 -9.95¢（挂单），t -0.89，单侧 p 0.812，有成交市场 213，前半 +1.77¢（107 个）/ 后半 -25.29¢（106 个），利润上限 $-16.4/天 → **不通过**（每份 -9.95¢ ≤ 0；单侧 p 0.812 ≥ 0.005；前后两半不都为正）
- 规则 2（CALIB，高于，[0.85, 0.95)） × BTC 每小时高于：每份 -2.33¢（挂单），t -0.47，单侧 p 0.679，有成交市场 119，前半 -6.95¢（60 个）/ 后半 +1.60¢（59 个），利润上限 $-0.5/天 → **不通过**（每份 -2.33¢ ≤ 0；单侧 p 0.679 ≥ 0.005；前后两半不都为正）
- 规则 2（CALIB，高于，[0.85, 0.95)） × BTC 每小时涨跌：不适用（规则 2 只用于“高于”类）。
- 规则 2（CALIB，高于，[0.85, 0.95)） × ETH 日内：每份 -19.90¢（挂单），t -1.36，单侧 p 0.912，有成交市场 94，前半 -28.87¢（47 个）/ 后半 -10.87¢（47 个），利润上限 $-16.2/天 → **不通过**（每份 -19.90¢ ≤ 0；单侧 p 0.912 ≥ 0.005；有成交市场 94 < 100；前后两半不都为正）
- 规则 2（CALIB，高于，[0.85, 0.95)） × SOL 日内：每份 +9.42¢（挂单），t –，单侧 p 0.065，有成交市场 17，前半 +4.32¢（9 个）/ 后半 +26.77¢（8 个），利润上限 $0.6/天 → **不通过**（单侧 p 0.065 ≥ 0.005；有成交市场 17 < 100）
- 规则 2（CALIB，高于，[0.85, 0.95)） × XRP 日内：每份 -8.74¢（挂单），t -0.86，单侧 p 0.801，有成交市场 28，前半 -11.14¢（14 个）/ 后半 -5.52¢（14 个），利润上限 $-0.4/天 → **不通过**（每份 -8.74¢ ≤ 0；单侧 p 0.801 ≥ 0.005；有成交市场 28 < 100；前后两半不都为正）
- 对照（BTC 日内慢盘，同一规则、同一代码口径）：规则 1 在 NEARCERT 独立段每份 +2.44¢（t 6.70，275 个有成交市场，180 天）；规则 2 在 CALIB 的 V 段每份 +5.88¢（t 4.34，64 个市场）。
- 通过 0 / 9 格。没有格子通过，不上前向。

数据：Gamma 已结束事件（标题日期 2025-09-15–2026-09-30，ET）、data-api 逐笔成交、data.binance.vision 的 1 秒和 1 分钟 K 线（BTCUSDT、ETHUSDT、SOLUSDT、XRPUSDT，UTC 2025-09-14–2026-10-01）。模型、结算、成交窗口和统计全部从 resolved.py / nearcert.py / calib.py 导入（见 `fav.py` 文档）。本次运行 0.4 分钟；首次运行各步：发现 10.0 分钟，K 线下载 10.9 分钟，K 线缓存和核对 2.9 分钟，结算核对 0.4 分钟，模型 0.8 分钟，抓成交 59.3 分钟。

单位：¢/份 = 按份数加权的（结果 − 成交价）×100，挂单无费，吃单再扣 0.07·p(1 − p)；t 按市场聚类（少于 20 个市场记“–”），单侧 p 用 G − 1 自由度的 t 分布；份/天、花费 $/天（Σ p·份）、利润 $/天（Σ(结果 − p)·份）是这一格**全部成交的合计，是上限**，不是一个买家能拿到的；天数 = 该组有市场的 ET 日（抽样的组只算抽中的日，等于全期合计 ÷ 天数 × 1/抽样比例）；在区间市场/天 = 有一边落在规则区间（任一时点）的市场；最大单市场亏损 = 一个市场在这一格的挂单净亏损。

## 每格结果

| 规则 | 组 | 抽样比例 | 天数 | 在区间市场/天 | 有成交市场 | 有成交市场/天 | 输的市场 | 笔数 | 份/天 | 花费 $/天 | 利润 $/天 | 挂单 ¢/份 | t | 单侧 p | 吃单 ¢/份 | t | 前半 ¢/份（市场） | 后半 ¢/份（市场） | 最大单市场亏损 $ | 模型 q | 成交价 p | 成交加权胜率 | 日聚类 t（描述） | 判定 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| R1 | BTC 每小时高于 | 1.00 | 196 | 24.35 | 373 | 1.90 | 17 | 804 | 126 | 119 | 2.4 | +1.94 | 1.25 | 0.106 | +1.65 | 1.08 | +1.20（187） | +2.81（186） | -273 | 0.9775 | 0.9453 | 0.9646 | 1.26 | 不通过 |
| R1 | BTC 每小时涨跌 | 1.00 | 381 | 9.46 | 3570 | 9.37 | 178 | 141,971 | 14,394 | 13,729 | -69.3 | -0.48 | -0.93 | 0.823 | -0.76 | -1.45 | +0.34（1793） | -2.10（1777） | -9,063 | 0.9732 | 0.9538 | 0.9490 | -0.94 | 不通过 |
| R1 | ETH 日内 | 1.00 | 381 | 1.89 | 451 | 1.18 | 17 | 2,199 | 491 | 463 | 24.7 | +5.02 | 2.46 | 0.007 | +4.74 | 2.39 | +3.84（227） | +7.51（224） | -709 | 0.9716 | 0.9417 | 0.9919 | 2.43 | 不通过 |
| R1 | SOL 日内 | 1.00 | 381 | 1.12 | 164 | 0.43 | 4 | 581 | 84 | 82 | 0.5 | +0.64 | 0.76 | 0.224 | +0.52 | 0.62 | +0.18（82） | +1.69（82） | -246 | 0.9751 | 0.9823 | 0.9887 | 0.63 | 不通过 |
| R1 | XRP 日内 | 1.00 | 381 | 1.33 | 213 | 0.56 | 3 | 699 | 165 | 160 | -16.4 | -9.95 | -0.89 | 0.812 | -10.14 | -0.90 | +1.77（107） | -25.29（106） | -7,634 | 0.9728 | 0.9708 | 0.8713 | -0.88 | 不通过 |
| R2 | BTC 每小时高于 | 1.00 | 196 | 11.36 | 119 | 0.61 | 21 | 230 | 22 | 19 | -0.5 | -2.33 | -0.47 | 0.679 | -3.15 | -0.63 | -6.95（60） | +1.60（59） | -102 | 0.9075 | 0.8459 | 0.8226 | -0.50 | 不通过 |
| R2 | BTC 每小时涨跌 | – | – | – | – | – | – | – | – | – | – | – | – | – | – | – | – | – | – | – | – | – | – | 不适用 |
| R2 | ETH 日内 | 1.00 | 381 | 0.36 | 94 | 0.25 | 11 | 354 | 81 | 75 | -16.2 | -19.90 | -1.36 | 0.912 | -20.37 | -1.40 | -28.87（47） | -10.87（47） | -5,116 | 0.9116 | 0.9273 | 0.7282 | -1.36 | 不通过 |
| R2 | SOL 日内 | 1.00 | 381 | 0.17 | 17 | 0.04 | 1 | 45 | 6 | 5 | 0.6 | +9.42 | – | 0.065 | +8.93 | – | +4.32（9） | +26.77（8） | -1 | 0.8806 | 0.9053 | 0.9995 | – | 不通过 |
| R2 | XRP 日内 | 1.00 | 381 | 0.25 | 28 | 0.07 | 3 | 62 | 4 | 4 | -0.4 | -8.74 | -0.86 | 0.801 | -9.13 | -0.90 | -11.14（14） | -5.52（14） | -157 | 0.8970 | 0.9403 | 0.8529 | -0.86 | 不通过 |

按类型 / 时点拆开（描述，不判定；¢/份 挂单，市场 = 有成交市场）：

| 规则 | 组 | 类型 / 时点：¢/份（有成交市场，t） |
|---|---|---|
| R1 | BTC 每小时高于 | 1分 +0.98（79，t 2.08）；5分 +7.14（85，t 2.39）；10分 +0.71（114，t 0.35）；30分 -0.09（101，t -0.02） |
| R1 | BTC 每小时涨跌 | 1分 -1.23（551，t -1.03）；5分 +0.20（1138，t 0.31）；10分 -0.09（1489，t -0.12）；30分 -1.99（954，t -1.02） |
| R1 | ETH 日内 | 高于 +4.63（149，t 1.53）；触及 +2.01（5，t –）；区间 +1.94（207，t 3.56）；按日涨跌 +7.89（90，t 1.72）；1分 +10.75（34，t 1.66）；5分 +2.13（71，t 3.08）；10分 +2.87（81，t 6.41）；30分 +3.05（130，t 2.57）；60分 +2.60（177，t 3.25） |
| R1 | SOL 日内 | 高于 +1.77（34，t 5.10）；触及 +13.04（8，t –）；区间 -2.51（59，t -0.59）；按日涨跌 +0.85（63，t 1.89）；1分 +0.83（21，t 4.95）；5分 +2.14（25，t 5.56）；10分 +0.84（33，t 0.82）；30分 -2.22（51，t -0.43）；60分 +1.64（51，t 1.50） |
| R1 | XRP 日内 | 高于 +4.15（53，t 1.88）；触及 +2.12（4，t –）；区间 +1.70（98，t 2.72）；按日涨跌 -49.36（58，t -1.88）；1分 -21.53（32，t -1.11）；5分 +8.04（22，t 1.98）；10分 +3.44（42，t 2.99）；30分 +2.43（74，t 4.21）；60分 -2.80（76，t -0.73） |
| R2 | BTC 每小时高于 | 30分 -2.33（119，t -0.47） |
| R2 | ETH 日内 | 30分 +2.43（40，t 0.59）；60分 -33.66（58，t -1.72） |
| R2 | SOL 日内 | 30分 +10.09（7，t –）；60分 +9.31（10，t –） |
| R2 | XRP 日内 | 30分 +2.09（13，t –）；60分 -13.62（15，t –） |

## 覆盖与剔除

| 组 | 类型 | 事件 | 市场 | 有市场的天 | 第一天 | 最后一天 | 规则不同 | 结束时间不对 | 未结算 | 缺 K 线 | 结果不符 | 纳入 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| BTC 每小时高于 | 每小时高于 | 4461 | 73080 | 196 | 2026-03-19 | 2026-09-30 | 0 | 0 | 0 | 0 | 0 | 73080 |
| BTC 每小时涨跌 | 每小时涨跌 | 9136 | 9136 | 381 | 2025-09-15 | 2026-09-30 | 0 | 2 | 0 | 0 | 0 | 9134 |
| ETH 日内 | 高于 | 374 | 4105 | 374 | 2025-09-15 | 2026-09-30 | 0 | 0 | 0 | 0 | 0 | 4105 |
| ETH 日内 | 触及 | 203 | 2842 | 203 | 2026-03-09 | 2026-09-30 | 0 | 0 | 0 | 0 | 0 | 2842 |
| ETH 日内 | 区间 | 368 | 4048 | 368 | 2025-09-15 | 2026-09-30 | 0 | 133 | 0 | 0 | 0 | 3915 |
| ETH 日内 | 按日涨跌 | 380 | 380 | 380 | 2025-09-15 | 2026-09-30 | 0 | 0 | 0 | 0 | 0 | 380 |
| SOL 日内 | 高于 | 370 | 4062 | 370 | 2025-09-15 | 2026-09-30 | 0 | 0 | 0 | 0 | 0 | 4062 |
| SOL 日内 | 触及 | 203 | 2030 | 203 | 2026-03-09 | 2026-09-30 | 0 | 0 | 0 | 0 | 0 | 2030 |
| SOL 日内 | 区间 | 368 | 4048 | 368 | 2025-09-15 | 2026-09-30 | 0 | 121 | 0 | 0 | 0 | 3927 |
| SOL 日内 | 按日涨跌 | 379 | 379 | 379 | 2025-09-15 | 2026-09-30 | 0 | 0 | 0 | 0 | 0 | 379 |
| XRP 日内 | 高于 | 371 | 4088 | 371 | 2025-09-15 | 2026-09-30 | 0 | 0 | 0 | 0 | 0 | 4088 |
| XRP 日内 | 触及 | 203 | 2030 | 203 | 2026-03-09 | 2026-09-30 | 0 | 0 | 0 | 0 | 0 | 2030 |
| XRP 日内 | 区间 | 367 | 4037 | 367 | 2025-09-15 | 2026-09-30 | 0 | 121 | 0 | 0 | 0 | 3916 |
| XRP 日内 | 按日涨跌 | 380 | 380 | 380 | 2025-09-15 | 2026-09-30 | 0 | 0 | 0 | 0 | 0 | 380 |

- 每小时高于：系列 `bitcoin-multi-strikes-hourly`（id 11372），按结束日逐日列出 4,481 个事件；缺的整点按两种 slug 拼法补查 9,364 个，补到 1 个；标题解析不了 0 个。有事件的 196 天里每天事件数中位数 24，少于 24 个的 49 天（如 2026-03-19 2, 2026-03-20 13, 2026-03-23 23, 2026-04-01 13, 2026-04-23 23, 2026-04-26 23）。
  - 每个事件的价位数：中位数 20（最少 10、最多 20）；按月中位数 2026-03 10，2026-04 10，2026-05 10，2026-06 20，2026-07 20，2026-08 20，2026-09 20。
  - 相邻价位间隔中位数 $200；结束 = 标题时刻（1 小时 K 线收盘），市场开放（startDate/acceptingOrders）在结束前中位数 78 分钟（p10 76）。
- 每小时涨跌：系列 `btc-up-or-down-hourly`（id 10114），按结束日逐日列出 9,184 个事件；缺的整点按两种 slug 拼法补查 44 个，补到 2 个；标题解析不了 0 个。有事件的 381 天里每天事件数中位数 24，少于 24 个的 12 天（如 2025-11-02 23, 2025-11-19 23, 2025-12-19 23, 2025-12-21 23, 2026-01-10 23, 2026-01-16 23）。
  - 结束 = 标题时刻 + 1 小时；市场开放在结束前中位数 49 小时。
  - 同一小时有两个事件（两种 slug 拼法，各自独立的市场，都保留）：13 个小时，如 `bitcoin-up-or-down-april-5-7am-et` 和 `bitcoin-up-or-down-april-5-2026-7am-et`。
  - 夏令时切换日 2025-11-02：23 个事件，标题小时 [0, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23]，结束时间不对（剔除）0 个。
  - 夏令时切换日 2026-03-08：24 个事件，标题小时 [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23]，结束时间不对（剔除）2 个。
- ETH：高于 从 2025-09-15 起，之后缺 7 天（2025-12-14, 2026-03-02, 2026-03-15, 2026-07-18 …）；区间 从 2025-09-15 起，之后缺 13 天（2025-11-09, 2025-11-12, 2026-01-25, 2026-03-15 …）；触及 从 2026-03-09 起，之后缺 3 天（2026-07-12, 2026-07-26, 2026-08-15）；按日涨跌 从 2025-09-15 起，之后缺 1 天（2026-05-24）。
- SOL：高于 从 2025-09-15 起，之后缺 11 天（2025-11-08, 2025-11-10, 2025-11-11, 2025-12-14 …）；区间 从 2025-09-15 起，之后缺 13 天（2025-11-08, 2025-11-09, 2025-11-10, 2025-11-11 …）；触及 从 2026-03-09 起，之后缺 3 天（2026-07-12, 2026-07-26, 2026-08-15）；按日涨跌 从 2025-09-15 起，之后缺 2 天（2025-11-07, 2026-05-24）。
- XRP：高于 从 2025-09-15 起，之后缺 10 天（2025-11-08, 2025-11-10, 2025-11-11, 2025-12-14 …）；区间 从 2025-09-15 起，之后缺 14 天（2025-10-15, 2025-11-08, 2025-11-09, 2025-11-10 …）；触及 从 2026-03-09 起，之后缺 3 天（2026-07-12, 2026-07-26, 2026-08-15）；按日涨跌 从 2025-09-15 起，之后缺 1 天（2026-05-24）。
- 范围（FAV.md 只列了这些）：ETH / SOL / XRP 只用日内四类；它们的每小时“高于”阶梯和周、月触及不在内。规则文字逐个市场解析，没有一个和模型假定的结算规则不同（上表“规则不同”全为 0）。
- 短 slug 命中去年同日的事件（跳过）548 个；其他跳过 0 个。

规则文字（每组每类：最常见的解析签名；纳入的市场描述节选；签名不同而剔除的，附原文）：

- BTC 每小时高于 · 每小时高于（73080 个市场）：`binance btc/usdt 1h close > ends`（73080）。例：“This market will resolve to "Yes" if the "Close" price for the BTC/USDT 1 hour candle that ends on the time and date specified in the title is higher than the price specified in the title. Otherwise, this market will resolve to "No". The resolution source for this market is Binance, specifically the BTC/USDT "Close" prices curre…”
- BTC 每小时涨跌 · 每小时涨跌（9136 个市场）：`binance btc/usdt 1h close+open up>=open begins`（9136）。例：“This market will resolve to "Up" if the close price is greater than or equal to the open price for the BTC/USDT 1 hour candle that begins on the time and date specified in the title. Otherwise, this market will resolve to "Down". The resolution source for this market is information from Binance, specifically the BTC/USDT pair (h…”
- ETH 日内 · 高于（4105 个市场）：`binance eth/usdt 1m close noon >`（4105）。例：“This market will resolve to "Yes" if the Binance 1 minute candle for ETH/USDT 12:00 in the ET timezone (noon) on the date specified in the title has a final “Close” price higher than the price specified in the title. Otherwise, this market will resolve to "No". The resolution source for this market is Binance, specifically the E…”
- ETH 日内 · 触及（2842 个市场）：`binance eth/usdt 1m high 12:00 am et touch>= day`（1421）；`binance eth/usdt 1m low 12:00 am et touch>= day`（1421）。例：“This market will immediately resolve to "Yes" if any Binance 1-minute candle for Ethereum (ETH/USDT) on the date specified in the title, between 12:00 AM ET and 11:59 PM ET has a final "High" price equal to or greater than the price specified in the title. Otherwise, this market will resolve to "No". The resolution source for th…”
- ETH 日内 · 区间（4048 个市场）：`binance eth/usdt 1m close noon tie_up`（4048）。例：“This market will resolve according to the final "Close" price of the Binance 1 minute candle for ETH/USDT 12:00 in the ET timezone (noon) on the date specified in the title. Otherwise, this market will resolve to "No". The resolution source for this market is Binance, specifically the ETH/USDT "Close" prices currently available …”
- ETH 日内 · 按日涨跌（380 个市场）：`binance eth/usdt 1m close noon up>ref,tie50 prev_noon`（380）。例：“This market will resolve to "Up" if the "Close" price for the Binance 1 minute candle for ETH/USDT Sep 14 '25 12:00 in the ET timezone (noon) is lower than the final "Close" price for the Sep 15 '25 12:00 ET candle. This market will resolve to "Down" if the "Close" price for the Binance 1 minute candle for ETH/USDT Sep 14 '25 12…”
- SOL 日内 · 高于（4062 个市场）：`binance sol/usdt 1m close noon >`（4062）。例：“This market will resolve to "Yes" if the Binance 1 minute candle for SOL/USDT 12:00 in the ET timezone (noon) on the date specified in the title has a final “Close” price higher than the price specified in the title. Otherwise, this market will resolve to "No". The resolution source for this market is Binance, specifically the S…”
- SOL 日内 · 触及（2030 个市场）：`binance sol/usdt 1m high 12:00 am et touch>= day`（1015）；`binance sol/usdt 1m low 12:00 am et touch>= day`（1015）。例：“This market will immediately resolve to "Yes" if any Binance 1-minute candle for Solana (SOL/USDT) on the date specified in the title, between 12:00 AM ET and 11:59 PM ET has a final "High" price equal to or greater than the price specified in the title. Otherwise, this market will resolve to "No". The resolution source for this…”
- SOL 日内 · 区间（4048 个市场）：`binance sol/usdt 1m close noon tie_up`（4048）。例：“This market will resolve according to the final "Close" price of the Binance 1 minute candle for SOL/USDT 12:00 in the ET timezone (noon) on the date specified in the title. Otherwise, this market will resolve to "No". The resolution source for this market is Binance, specifically the SOL/USDT "Close" prices currently available …”
- SOL 日内 · 按日涨跌（379 个市场）：`binance sol/usdt 1m close noon up>ref,tie50 prev_noon`（379）。例：“This market will resolve to "Up" if the "Close" price for the Binance 1 minute candle for SOL/USDT Sep 14 '25 12:00 in the ET timezone (noon) is lower than the final "Close" price for the Sep 15 '25 12:00 ET candle. This market will resolve to "Down" if the "Close" price for the Binance 1 minute candle for SOL/USDT Sep 14 '25 12…”
- XRP 日内 · 高于（4088 个市场）：`binance xrp/usdt 1m close noon >`（4088）。例：“This market will resolve to "Yes" if the Binance 1 minute candle for XRP/USDT 12:00 in the ET timezone (noon) on the date specified in the title has a final “Close” price higher than the price specified in the title. Otherwise, this market will resolve to "No". The resolution source for this market is Binance, specifically the X…”
- XRP 日内 · 触及（2030 个市场）：`binance xrp/usdt 1m high 12:00 am et touch>= day`（1015）；`binance xrp/usdt 1m low 12:00 am et touch>= day`（1015）。例：“This market will immediately resolve to "Yes" if any Binance 1-minute candle for XRP (XRP/USDT) on the date specified in the title, between 12:00 AM ET and 11:59 PM ET has a final "High" price equal to or greater than the price specified in the title. Otherwise, this market will resolve to "No". The resolution source for this ma…”
- XRP 日内 · 区间（4037 个市场）：`binance xrp/usdt 1m close noon tie_up`（4037）。例：“This market will resolve according to the final "Close" price of the Binance 1 minute candle for XRP/USDT 12:00 in the ET timezone (noon) on the date specified in the title. Otherwise, this market will resolve to "No". The resolution source for this market is Binance, specifically the XRP/USDT "Close" prices currently available …”
- XRP 日内 · 按日涨跌（380 个市场）：`binance xrp/usdt 1m close noon up>ref,tie50 prev_noon`（380）。例：“This market will resolve to "Up" if the "Close" price for the Binance 1 minute candle for XRP/USDT Sep 14 '25 12:00 in the ET timezone (noon) is lower than the final "Close" price for the Sep 15 '25 12:00 ET candle. This market will resolve to "Down" if the "Close" price for the Binance 1 minute candle for XRP/USDT Sep 14 '25 12…”

## K 线与结算口径

- BTCUSDT：1 秒 383 天（时间戳单位 {'us': 383}），1 分钟 383 天（{'us': 383}）；价格按 0.01 美元的整数存（最大取整误差 9.3e-10 个最小单位）。逐分钟核对 551,520 分钟：1 秒聚合最高价高于官方 635、低于 0，最低价低于官方 652、高于 0，收盘价不同 0（最大差 $2.31）→ 结果和 T* 用官方 1 分钟，1 秒只用于 σ 和秒级越过。
- ETHUSDT：1 秒 383 天（时间戳单位 {'us': 383}），1 分钟 383 天（{'us': 383}）；价格按 0.01 美元的整数存（最大取整误差 5.8e-11 个最小单位）。逐分钟核对 551,520 分钟：1 秒聚合最高价高于官方 3185、低于 0，最低价低于官方 3456、高于 0，收盘价不同 0（最大差 $0.32）→ 结果和 T* 用官方 1 分钟，1 秒只用于 σ 和秒级越过。
- SOLUSDT：1 秒 383 天（时间戳单位 {'us': 383}），1 分钟 383 天（{'us': 383}）；价格按 0.01 美元的整数存（最大取整误差 1.8e-12 个最小单位）。逐分钟核对 551,520 分钟：1 秒聚合最高价高于官方 5072、低于 0，最低价低于官方 4776、高于 0，收盘价不同 0（最大差 $0.01）→ 结果和 T* 用官方 1 分钟，1 秒只用于 σ 和秒级越过。
- XRPUSDT：1 秒 383 天（时间戳单位 {'us': 383}），1 分钟 383 天（{'us': 383}）；价格按 0.0001 美元的整数存（最大取整误差 3.6e-12 个最小单位）。逐分钟核对 551,520 分钟：1 秒聚合最高价高于官方 7715、低于 0，最低价低于官方 8155、高于 0，收盘价不同 0（最大差 $0.0003）→ 结果和 T* 用官方 1 分钟，1 秒只用于 σ 和秒级越过。
- ETH 中午 K 线约定：与官方不符 open12（12:00 开盘、12:01 收盘）= 0，close12（11:59 开盘）= 27 → 用 **open12**（和 BTC 日内相同的规则文字）。
- SOL 中午 K 线约定：与官方不符 open12（12:00 开盘、12:01 收盘）= 0，close12（11:59 开盘）= 15 → 用 **open12**（和 BTC 日内相同的规则文字）。
- XRP 中午 K 线约定：与官方不符 open12（12:00 开盘、12:01 收盘）= 0，close12（11:59 开盘）= 25 → 用 **open12**（和 BTC 日内相同的规则文字）。
- BTC 每小时高于：“结束于标题时刻的 1 小时 K 线收盘”（= 结束前最后一根 1 分钟 K 线收盘）与官方不符 0，另一种读法（开始于标题时刻）2464 → 用 **ends**。BTC 每小时涨跌：收盘 ≥ 本小时开盘（第一根 1 分钟 K 线开盘）与官方不符 0（若用上一小时收盘当开盘：0）。
- 币安自己的 BTCUSDT 1 小时 K 线（12 个月度文件）对 1 分钟拼出的开盘/收盘：8,448 小时，开盘不同 0、收盘不同 0。

## 抓取与抽样

先只用 K 线算模型（不看任何成交），列出有一边落在规则区间的时点窗口，再只抓这些窗口 [t, t+60) 的成交。预算 40,000 个窗口（约 3 小时 × 4 次/秒）。

| 组 | 窗口 | 规则 1 | 规则 2 | 有市场的天 | 抽中的天 | 抽样比例 | 抓的窗口 |
|---|---|---|---|---|---|---|---|
| BTC 每小时高于 | 7685 | 5459 | 2226 | 196 | 196 | 1.000 | 7685 |
| BTC 每小时涨跌 | 4175 | 4175 | 0 | 381 | 381 | 1.000 | 4175 |
| ETH 日内 | 982 | 836 | 146 | 381 | 381 | 1.000 | 982 |
| SOL 日内 | 580 | 513 | 67 | 381 | 381 | 1.000 | 580 |
| XRP 日内 | 729 | 626 | 103 | 381 | 381 | 1.000 | 729 |

- 共 14,151 个窗口，在预算内，全部抓取（抽样比例 1）；抓到 14,151 个窗口、14,151 页，截断 0，缺 0；有成交的窗口 6,081。 首次抓取 59 分钟、14,151 次请求。

文件：`fav.py`（代码，含全部预先写定的保守选择），`test_fav.py`（测试）。
