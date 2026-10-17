# 候选策略：在本地 shadow 数据上自己跑

全部是 BTC 5 分钟 Up/Down 市场：只买一边，持有到结算，按卖一成交，付 taker 费 0.07·p·(1−p)。
代码：`run_local.py`（只读你的数据，不上传任何东西，也不下单）。

## 怎么跑

```bash
git clone https://github.com/yangyangnovelist-hub/whole-web-station-clone.git
cd whole-web-station-clone && git checkout claude/polymarket-options-strategy-1lc7i4
cd research/polymarket-5m && pip install -r requirements.txt

# 1. 先看找到了什么（表名、行数、币安成交的时间范围）
python run_local.py <shadow 数据路径 ...> --inspect

# 2. 跑策略
python run_local.py <shadow 数据路径 ...> --since 2026-08-30 --split 2026-09-04 2026-09-14 --out local-report.md
```

`<shadow 数据路径>` 可以混着给，也可以给目录（会往下找）：

- shadow 的数据库 `runtime_1000.sqlite`，只读打开；
- 引擎日志 `.jsonl` / `.jsonl.gz`，里面要有 `BINANCE_AGG_TRADE`；
- `export_to_archive.sh` 导出的文件夹，`.part-aa` 这类分片会自动拼起来。

只读四样东西：

- Up 的盘口 `poly_probability_observations_v1`
- `market_registry`
- `market_outcomes`
- 币安成交

结果写到 `local-report.md`，逐笔明细在 `local-report.csv.gz`。

**用哪段数据最干净：**

- **8 月 30 日到 9 月 25 日：** 这些策略都没在这段上挑过，是真正的样本外。在这段上 G 每份赚钱且 p < 0.05，就是一条独立的证据。
- **8 月 29 日以前：** 和挑规则用的公开数据重叠。
- **9 月 26–29 日的都柏林数据：** 我看过。

`--split 2026-09-04` 把吃单延迟改成 150 ms 前后分开看。

**$300 复利资金曲线带上 9 月：**

```bash
# 加 --scale-in 会多出“G 加仓”“H 加仓”：同一市场每次急动都买，两笔至少隔 2 秒
python run_local.py <9 月 shadow 数据路径 ...> --since 2026-08-30 --scale-in --out sept.md
# 5/25–8/29 的公开数据接上 sept.csv.gz，画到 real/equity_compound.png；没数据的日子空仓（图上灰色）
python equity_compound.py --extra sept.csv.gz
```

默认每笔投账户的 1%，最少 5 份、最多 200 份，不超过卖一挂单量，按 78% 成交。可以用 `--fraction 0.025`、`--cap 40` 换别的假设。

**加仓规则和复利曲线，按实盘的逐笔定义**（每一笔币安成交都看，买到后 2 秒内不再买；急动段每 2 秒只看第一笔，九月会少算约 4/5 的成交）：

```bash
# 5–8 月：从 Hugging Face 重新下载 67 个日档，写出每一笔候选成交（不要覆盖 real/cross-jitter.csv.gz）
python cross.py jitter --gap-ms 0 --out real/cross-prints.md            # → real/cross-prints.csv.gz
# 九月
python run_local.py <9 月 shadow 数据路径 ...> --since 2026-08-30 --prints --out sept.md   # → sept-prints.csv.gz
# 63 条加仓规则（A 段选，B+C 核对，X 是九月）和 $300 复利曲线，两边都用逐笔
python scalein.py --jitter real/cross-prints.csv.gz --extra sept-prints.csv.gz > scalein-prints.md
python equity_compound.py --jitter real/cross-prints.csv.gz --extra sept-prints.csv.gz --out equity-prints.png
```

## 四个策略

所有时间都是交易所时间戳。σ 是近 10 分钟币安每秒对数涨跌的标准差。

| 代号 | 规则 |
|---|---|
| **G**（检验 G，最看好） | 剩 240–15 秒时，某笔币安成交相对 1–5 秒前的最后一笔涨跌超过 2σ（触发）。公平价 = Φ(Φ⁻¹(触发时 Up 中间价) + 涨跌 / (σ × TWAP 系数))。触发后 0.3 秒按卖一买顺势一方，只在 公平价 − 卖一 − 手续费 ≥ 12¢ 时买。**触发前 2 秒内 Up 中间价已经朝要买的方向动了 ≥ 3¢ 就跳过**。每个市场第一笔。 |
| **F**（检验 F） | 同 G，不跳过。 |
| **H**（2 秒前起算） | 同 F，但公平价从触发前 2 秒的中间价起算，加上这 2 秒币安的总涨跌。Polymarket 已经跟上的部分不重复算。 |
| **M27**（币安 5 秒动量） | 剩 240–30 秒，每秒看一次：币安 5 秒涨跌超过 2.5σ×√5 就在 0.3 秒后按卖一买顺势一方，每个市场第一次。 |

G、F、H 的报告还会按"触发前 30 秒是否平静"拆开：Up 中间价在触发前 32–2 秒的幅度 ≤ 0.42 算平静。这个分界是在 5–7 月数据上定的三分位。

## 回测和实盘到目前为止

θ = 12¢、0.3 秒、盘口健康过滤，每份赚的钱（笔数）：

| | A 5/25–7/15（挑选用） | B 7/16–8/16 | C 8/17–8/29 | 实盘（GitHub 前向录制） |
|---|---|---|---|---|
| G | +5.8¢（4,050） | +8.1¢（346） | +13.3¢（433） | 检验 G：到 10/2 05:05 共 74 笔 +6.4¢（0.4 秒时 57 笔 +0.3¢），600 笔判定 |
| F | +2.9¢（8,890） | +7.2¢（606） | +5.9¢（1,732） | 22 笔 −5.2¢（被抢先的 15 笔 −16.3¢，没被抢先的 7 笔 +18.6¢） |
| H | +4.0¢（8,442） | +9.1¢（600） | +5.9¢（1,539） | 检验 I：10/2 09:00 起，每次都加、反向 2 倍、0.4 秒，前 1,000 个市场判定 |
| M27 | +1.6¢（10,220） | +3.4¢（729） | +1.6¢（2,162） | 没开实盘检验 |

C 段 G 的平均买价约 0.36，每投 1 美元约赚 0.36 美元；A 段约 0.14 美元。M27 每投 1 美元只赚约 2.5 美分。

## 九月 shadow 数据上的结果（用户本地，10 月 1 日）

0.3 秒成交、盘口健康检查、15 个高分辨率日：G 每份 +13.1¢（1,334 笔，天天赚钱）、H +12.7¢、F +6.2¢、M27 −1.4¢；0.5 秒 G +7.1¢、H +6.7¢；对照（币安后移 10 分钟）−1.5～−1.8¢。Polymarket 对这类盘的吃单要先扣住 150 ms（9 月 4 日起；8 月 17 日–9 月 4 日是 50 ms，之前 250 ms），扣住期间不能撤单。现实每天 G 约 $240、H 约 $290。详见 README 第 20 节。

**加仓（同一市场后续急动再买）在九月上也成立：** 0.3 秒 G 加仓 +12.7¢（2,713 笔）、H 加仓 +12.4¢（3,785 笔），其中第 2 笔起的每份 +11.4¢；0.4 秒 +10.2¢。加仓规则默认“每次都加”；“只加反向”在 H 上曲线更平、但每天少赚约四分之一。九月录制没有最后一分钟，那一段只在 5–8 月上验证过（三段都赚钱）。

**九月的数可能偏高：** 同一批市场上，都柏林录的盘口在快速行情里比 GitHub 录的晚约 50 ms 才变价，便宜的卖一在回测里留得更久。同一规则在都柏林盘口上每份多 1～8¢、笔数多 30–60%；按 GitHub 盘口、0.4 秒，每份只有 0～+4¢（README 第 20 节末尾）。实盘以 GitHub 前向录制的检验 G、检验 I 为准。

**延迟是现在最大的问题：** 目标、预算、改动清单和监控见 [`LATENCY.md`](LATENCY.md)；每 50 ms 值多少钱见 [`real/forward-latency-curve.md`](real/forward-latency-curve.md)。

## 看结果时注意

- **延迟：** 回测假设币安成交后 0.3 秒单子已经按当时的卖一成交。你的机器到币安和 Polymarket 的延迟，加上 Polymarket 自 9 月 4 日起对吃单的 150 ms 延迟，要能做到这一点。日志里的 `BINANCE_AGG_TRADE` 是永续合约成交，回测用的是现货，两者几乎同步。
- **成交量：** 报告里的"卖一数量"是挂单量，不是你能拿到的量。回测里同一个卖一，别的吃单在我们到达前平均拿走约 16 份、之后又拿走约 19 份，现实一笔大概 20–40 份。所以报告按每笔最多 40 份估算每天的钱。
- **样本量：** 几十笔的结果说明不了什么。G 一天约 30–40 笔，至少几百笔、p < 0.05，才算有证据。
- **别反复挑：** 别在同一段数据上反复调参数再看结果，那样挑出来的数会偏好看。要调，就在一段上调，到另一段上验。

## 哪个行情源最先到你的服务器（`feeds.py`，只录行情，不要密钥，不下单）

在交易服务器上（和实盘程序同一个时钟）跑一天，同时录币安现货、币安 U 本位永续（成交和最优买卖价）、OKX、Bybit、Deribit（伦敦 LD4）、Coinbase 的 BTC 成交，
以及每秒一次 `clob.polymarket.com/time` 的往返（长连接，不含 150 ms 吃单冻结）：

```bash
python feeds.py record --out feeds.jsonl.gz --hours 24
python feeds.py analyze feeds.jsonl.gz --out feeds-report.md
```

报告给出每个来源从交易所时间戳到你这台机器的延迟（要先开 chrony 或 AWS Time Sync 对时），以及每一次 2σ 急动哪个来源最先到、别的晚多少毫秒（这一项只用本机时钟，不受对时影响）。
注意：币安 U 本位合约的行情地址已经拆开，成交只在 `wss://fstream.binance.com/market/...`，最优买卖价在 `/public/...`；旧的 `fstream.binance.com/ws` 只推最优买卖价、不推成交。
Deribit 公开的成交是 100 ms 一批，逐笔要登录。币安现货另有更低延迟的 SBE 二进制行情流（`stream-sbe.binance.com`，要 Ed25519 的 API key，只读即可），这个脚本没录。
