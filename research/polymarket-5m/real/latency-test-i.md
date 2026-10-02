# 检验 I：H 每次都加、反向 2 倍，0.4 秒（BTC 5m，币安逐笔成交触发，GitHub 前向录制）

事先写死（10 月 2 日约 08:30 UTC，数据还没录）：2026-10-02 09:00 UTC 起开始的 BTC 5m 市场；币安逐笔成交 2σ 触发（剩 240–15 秒），公平价按 H：从触发前 2 秒的 Up 中间价起算，加上这 2 秒币安的涨跌；0.4 秒后按卖一买顺势一方，公平价 − 卖一 − 手续费 ≥ 12¢ 才买；同一市场每次都买，买到后 2 秒内不再买；第一笔和与第一笔同方向的加仓每笔 1 份，反方向的加仓每笔 2 份；持有到结算。每份赚的钱 = Σ份数×盈亏 / Σ份数，标准误按市场聚类（同一市场的几笔一起结算），单边正态 p。按每个市场第一笔的时间取前 1,000 个有成交的市场、它们的全部成交，判定一次：每份 > 0 且 p < 0.025 才算通过。数据处理同检验 G（每段录制单独算、盘口健康、断线检查）。

录制段 12 个，0.4 秒：成交 64 笔、37 个市场。

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

| 版本 | 延迟 | 市场 | 笔数 | 每份（±聚类标准误） | p |
|---|---|---:|---:|---:|---:|
| 反向 2 倍（检验的规则） | 0.4 秒 | 37 | 64 | +3.85¢ ±6.0 | 0.2620 |
| 每次都加（每笔 1 份） | 0.4 秒 | 37 | 64 | +1.17¢ ±6.5 | 0.4285 |
| 只买第一笔 | 0.4 秒 | 37 | 37 | -3.31¢ ±5.8 | 1.0000 |
| 反向 2 倍（检验的规则） | 0.3 秒 | 46 | 74 | +10.61¢ ±5.8 | 0.0340 |

检验 I（前 1,000 个市场）：目前 37 个市场，不到 1,000 个，不判定。
