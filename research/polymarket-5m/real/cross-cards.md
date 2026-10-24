# 候选数据集的说明和文件列表

## [Sigrex/polymarket_btc_up_down_5m_251218_260909](https://huggingface.co/datasets/Sigrex/polymarket_btc_up_down_5m_251218_260909)

4 个文件，共 0.15 GB，最后修改 2026-09-30T09:22。

```
.gitattributes  2,665
README.md  2,601
polymarket_btc_up_down_5m_251218_260909.csv  59,460,292
polymarket_btc_up_down_5m_251218_260909.json  94,660,074
```

<details><summary>README.md</summary>

```
---
license: mit
language:
- en
tags:
- polymarket
- btc
- 5m
pretty_name: Polymarket BTC up/down 5m
size_categories:
- 10K<n<100K
---
# Polymarket BTC 5-Minute Up/Down — Historical Dataset

Offline dataset of every **Bitcoin Up or Down 5-minute** binary market on
Polymarket (series `btc-up-or-down-5m`, Gamma series id **10684**), collected
from the official Polymarket Gamma / CLOB / Data APIs, with **Binance BTC/USDT
1-minute spot data** joined for the underlying price signal.

## Summary

| Property | Value |
|---|---|
| Records | **71,602** (all resolved markets) |
| Period | 2025-12-18 → 2026-09-08 (UTC) |
| Horizon | 5 minutes |
| Columns | 42 |
| Label balance | ~50.2% Up / ~49.8% Down |
| Resolution source | Chainlink BTC/USD |

## Files

| File | Format | Notes |
|---|---|---|
| `polymarket_btc_up_down_5m_251218_260909.csv` | CSV | 71,602 rows × 42 columns |
| `polymarket_btc_up_down_5m_251218_260909.json` | JSON (array) | same data; missing values are `null` |

## Resolution rule

Each market resolves to **Up** if the Bitcoin price at the end of its 5-minute
window is ≥ the price at the beginning; otherwise **Down**. Resolution source is
the Chainlink BTC/USD stream. `winning_outcome` / `label_up` are taken from
Polymarket's resolved market state (not inferred from traded price).

## Columns

**Identifiers & time** — `condition_id`, `slug`, `question`, `start_time`,
`end_time` (ISO-8601 UTC), `start_ts`, `end_ts` (epoch s), `window_seconds` (300)

**Label** — `winning_outcome`, `label_up`

**Chainlink resolution values** *(~47,400 markets, ~66% coverage; rest null)* —
`final_price`, `price_to_beat`, `chainlink_return` (agrees with label 100%)

**BTC spot (Binance BTC/USDT)** — `btc_open`, `btc_close`, `btc_return`
(direction agrees with label ~95.5%), `btc_ret_15m` / `btc_ret_1h` /
`btc_ret_4h`, `btc_vol_1h`

**Market microstructure** — `implied_up_last`, `trade_count`

**Technical indicators** (`ind_*`, 20 cols) — computed on the 5-minute
timeframe, snapshotted at **window open** (zero lookahead): EMAs, RSI(14),
MACD(12,26,9), Bollinger(20,2), ATR(14), Stochastic, ROC(5/15/60), relative
volume, VWAP-24h.

> ⚠️ `implied_up_last` is the price *near close*, not a fair "beat the market"
> benchmark.

## Known limitations

- **Implied-prob coverage is ~84%** — ~16% of 5m markets are thin enough to have
  no recorded Up-token price point. The trade tape is fully present regardless.
- **Sparse price history** — closed markets return only 2–30 price points.
- **No order book** — Polymarket has no historical order-book API.
```
</details>

## [Sigrex/polymarket_btc_up_down_multihorizon](https://huggingface.co/datasets/Sigrex/polymarket_btc_up_down_multihorizon)

4 个文件，共 0.23 GB，最后修改 2026-09-30T09:53。

```
.gitattributes  2,657
README.md  3,932
polymarket_btc_up_down_multihorizon.csv  89,759,278
polymarket_btc_up_down_multihorizon.json  143,539,338
```

<details><summary>README.md</summary>

```
---
license: mit
language:
- en
tags:
- polymarket
- btc
pretty_name: Polymarket BTC up/down Multi-Horizon
size_categories:
- 100K<n<1M
---
# Polymarket BTC Up/Down — Multi-Horizon Historical Dataset

Offline datasets of Polymarket **Bitcoin Up or Down** binary markets at four
horizons, collected from the official Polymarket Gamma / CLOB / Data APIs, with
Binance BTC/USDT 1-minute spot data joined for the underlying price signal.

## Series

| Series | Horizon | Markets (resolved) | Period (UTC) | Resolution source |
|---|---|---|---|---|
| 5m | 5 minutes | 71,602 | 2025-12-18 → 2026-09-08 | Chainlink BTC/USD |
| 15m | 15 minutes | 34,564 | 2025-09-13 → 2026-09-08 | Chainlink BTC/USD |
| 4h | 4 hours | 1,936 | 2025-10-15 → 2026-09-08 | Chainlink BTC/USD |
| Daily | 1 day | 561 | 2025-03-14 → 2026-09-29 | Binance BTCUSDT (legacy) |

**Combined: 108,663 markets.**

## Files

| File | Contents |
|---|---|
| `polymarket_btc_up_down_5m_251218_260909.csv` / `.json` | 5-minute horizon (71,602 × 42 cols) |
| `polymarket_btc_up_down_4h_251015_260909.csv` / `.json` | 4-hour horizon (1,936 × 42 cols) |
| `polymarket_btc_up_down_1d_250314_260909.csv` / `.json` | daily horizon (561 × 42 cols) |
| `polymarket_btc_up_down_15m_250913_260908.csv` / `.json` | 15-minute horizon (34,564 × 41 cols) |
| `polymarket_btc_up_down_multihorizon.csv` / `.json` | all four combined (108,663 × 42 cols) |

## Resolution rule

Each market resolves to **Up** if the Bitcoin price at the end of its window is
≥ the price at the beginning; otherwise **Down**. `winning_outcome` / `label_up`
are taken from Polymarket's resolved market state (never inferred from traded
price).

- 5m / 15m / 4h resolve on the **Chainlink BTC/USD** stream.
- Daily is a **legacy series** resolving on **Binance BTCUSDT 1-minute candle
  closes** (noon-ET to noon-ET), and carries no Chainlink fields for most rows.

## Columns (42)

**Identifiers & time** — `condition_id`, `slug`, `question`, `start_time`,
`end_time` (ISO-8601 UTC), `start_ts`, `end_ts` (epoch s), `window_seconds`
(300 / 900 / 14400 / 86400)

**Label** — `winning_outcome` (`"Up"`/`"Down"`), `label_up` (`1`/`0`)

**Chainlink** *(5m/15m/4h; daily mostly null)* — `final_price`,
`price_to_beat`, `chainlink_return`

**BTC spot (Binance BTC/USDT)** — `btc_open`, `btc_close`, `btc_return`
(realized move over the window; direction agrees with label 93–99%),
`btc_ret_15m` / `btc_ret_1h` / `btc_ret_4h` (prior returns), `btc_vol_1h`

**Market microstructure** — `implied_up_last` (last Up-token price before close),
`trade_count`

**Technical indicators** (`ind_*`, 20 cols) — computed on the horizon's own
candle timeframe, snapshotted at **window open** (zero lookahead): EMAs,
RSI(14), MACD(12,26,9), Bollinger(20,2), ATR(14), Stochastic, ROC(5/15/60),
relative volume, VWAP-24h.

> ⚠️ `implied_up_last` is the price *near close* — by the last tick the window
> has often largely resolved, so it is **not** a fair "beat the market"
> benchmark. Dense early-window prices are not available historically.

## Known limitations

- **15m coverage is 99.8%** — 70 fifteen-minute windows have no market
  (Polymarket downtime); genuine gaps, not collection failures.
- **5m implied-prob coverage is 83.7%** — ~16% of 5m markets have no recorded
  Up-token price point (thin/short markets).
- **Daily NaN indicators (19 rows)** — the earliest daily markets (2025-03-14 →
  04-03) predate enough price history for the 60-bar lookback indicators
  (`ind_roc_60`, `ind_rel_vol_60`); those cells are `null`/empty.
- **Daily is a legacy series** — irregular slug format, Binance-based
  resolution, and its raw `startDate` was creation-time (not window start); the
  published window is correctly re-derived as `end_time − 86400s`.
- **No order book** — Polymarket has no historical order-book API; only the
  trade tape and price history are available.
```
</details>

## [Sigrex/polymarket_btc_up_down_15m_250913_260908](https://huggingface.co/datasets/Sigrex/polymarket_btc_up_down_15m_250913_260908)

4 个文件，共 0.09 GB，最后修改 2026-09-30T09:38。

```
.gitattributes  2,776
README.md  4,297
btc_up_down_15m__250913_260908.csv  28,154,078
btc_up_down_15m__250913_260908.json  57,964,099
```

<details><summary>README.md</summary>

```
---
license: mit
language:
- en
tags:
- polymarket
- btc
- 15m
pretty_name: Polymarket BTC up/down 15m
size_categories:
- 10K<n<100K
---
# Polymarket BTC 15-Minute Up/Down — Historical Dataset

Offline dataset of every **Bitcoin Up or Down 15-minute** binary market on
Polymarket (series `btc-up-or-down-15m`, Gamma series id **10192**), collected
from the official Polymarket Gamma / CLOB / Data APIs.

## Summary

| Property | Value |
|---|---|
| Records | **34,564** (all resolved markets; 4 unresolved excluded — no label) |
| Period | 2025-09-13 → 2026-09-08 (UTC) |
| Horizon | 15 minutes |
| Columns | 41 |
| Label balance | ~49.8% Up / ~50.2% Down |
| Resolution source | Chainlink BTC/USD |

## Files

| File | Format | Notes |
|---|---|---|
| `polymarket_btc_up_down_15m_250913_260908.csv` | CSV | 34,564 rows × 41 columns |
| `polymarket_btc_up_down_15m_250913_260908.json` | JSON (array of records) | same data; missing values are `null` |


## Resolution rule

Each market resolves to **Up** if the Bitcoin price at the end of its 15-minute
window is greater than or equal to the price at the beginning of the window;
otherwise **Down**. The resolution source is the Chainlink BTC/USD data stream.

The `winning_outcome` / `label_up` columns are taken from Polymarket's resolved
market state (not inferred from traded price).

## Columns

**Identifiers & time**

| Column | Description |
|---|---|
| `condition_id` | Polymarket condition id (unique per market) |
| `slug` | market slug (unique) |
| `question` | human-readable market question |
| `start_time` / `end_time` | window boundaries (ISO-8601 UTC) |
| `start_ts` / `end_ts` | same as epoch seconds |

**Label**

| Column | Description |
|---|---|
| `winning_outcome` | `"Up"` or `"Down"` (ground-truth winner) |
| `label_up` | `1` = Up, `0` = Down |

**Chainlink resolution values** (only ~15,800 markets — ~46% coverage; the rest are `null`)

| Column | Description |
|---|---|
| `final_price` | Chainlink BTC/USD at window end |
| `price_to_beat` | Chainlink BTC/USD at window start |
| `chainlink_return` | `final_price / price_to_beat − 1` |

**BTC spot (Binance BTC/USDT, joined)** — `btc_return` direction agrees with the
label on ~98% of markets (the gap vs. Chainlink is near-flat-boundary noise).

| Column | Description |
|---|---|
| `btc_open` / `btc_close` | spot at window open / close |
| `btc_return` | `btc_close / btc_open − 1` (the realized 15m move) |
| `btc_ret_15m` / `btc_ret_1h` / `btc_ret_4h` | prior-window returns |
| `btc_vol_1h` | realized volatility (prior 1h) |

**Market microstructure**

| Column | Description |
|---|---|
| `implied_up_last` | last Up-token price before close (market-implied probability of Up) |
| `trade_count` | number of taker fills on the market |

> ⚠️ `implied_up_last` is the price *near close* — on ~68% of markets the window
> has already largely resolved by the last tick, so it is **not** a fair
> "beat the market" benchmark. Dense early-window prices are not available
> historically from Polymarket's API for closed markets.

**Technical indicators** (`ind_*`, 20 columns)

Computed on the **15-minute timeframe**, snapshotted at **window open** (from the
last completed 15m candle before the window begins) — zero lookahead into the
window or its resolution.

- `ind_ema_{9,12,26,50,100,200}_dist` — EMA distance-from-price `(close − EMA)/close`
- `ind_rsi_14` — RSI(14), Wilder
- `ind_macd`, `ind_macd_signal`, `ind_macd_hist` — MACD(12,26,9), price-normalized
- `ind_bb_pctb`, `ind_bb_bw` — Bollinger(20,2) %B and bandwidth
- `ind_atr_pct` — ATR(14), price-normalized
- `ind_stoch_k`, `ind_stoch_d` — Stochastic %K(14,3) / %D
- `ind_roc_{5,15,60}` — rate-of-change over 5/15/60 bars
- `ind_rel_vol_60` — volume ÷ 60-bar SMA
- `ind_vwap_24h_dist` — distance from 24h rolling VWAP

## Known limitations

- **99.8% coverage** — 70 fifteen-minute windows have no market (Polymarket
  downtime); these are genuine gaps, not collection failures.
- **Sparse price history** — Polymarket's API returns only 2–30 price points for
  closed markets, so intra-window price dynamics are limited.
- **No order book** — Polymarket has no historical order-book API; only the trade
  tape and price history are available.
```
</details>

## [Houroux/polymarket-l2-history](https://huggingface.co/datasets/Houroux/polymarket-l2-history)

100,000 个文件，共 260.20 GB，最后修改 2026-09-30T14:27。

```
.gitattributes  2,564
README.md  702
analytics/book_levels/date=2026-07-22/00000000000000000000__discovery__2026__07__22__discovery-20260722T213714.791370Z-2345-0001.parquet  1,649
analytics/book_levels/date=2026-07-22/00000000000000000002__market_ws__2026__07__22__market_ws-20260722T213715.464309Z-2345-0001.parquet  138,890
analytics/book_levels/date=2026-07-22/00000000000000177682__market_ws__2026__07__22__market_ws-20260722T214417.528875Z-2345-0002.parquet  130,495
analytics/book_levels/date=2026-07-22/00000000000000355615__market_ws__2026__07__22__market_ws-20260722T215307.817981Z-2345-0003.parquet  141,834
analytics/book_levels/date=2026-07-22/00000000000000529309__market_ws__2026__07__22__market_ws-20260722T220408.564220Z-2345-0004.parquet  97,440
analytics/book_levels/date=2026-07-22/00000000000000708212__discovery__2026__07__22__discovery-20260722T220734.662537Z-2345-0002.parquet  1,649
analytics/book_levels/date=2026-07-22/00000000000000708214__market_ws__2026__07__22__market_ws-20260722T221325.350146Z-2345-0005.parquet  79,412
analytics/book_levels/date=2026-07-22/00000000000000886586__market_ws__2026__07__22__market_ws-20260722T222727.177521Z-2345-0006.parquet  47,831
analytics/book_levels/date=2026-07-22/00000000000001065918__market_ws__2026__07__22__market_ws-20260722T223712.730988Z-2345-0007.parquet  66,504
analytics/book_levels/date=2026-07-22/00000000000001244726__discovery__2026__07__22__discovery-20260722T223749.396759Z-2345-0003.parquet  1,649
analytics/book_levels/date=2026-07-22/00000000000001244728__market_ws__2026__07__22__market_ws-20260722T224437.529071Z-2345-0008.parquet  85,498
analytics/book_levels/date=2026-07-22/00000000000001422703__market_ws__2026__07__22__market_ws-20260722T225822.763165Z-2345-0009.parquet  67,793
analytics/book_levels/date=2026-07-22/00000000000001577526__discovery__2026__07__22__discovery-20260722T230804.731104Z-2345-0004.parquet  1,649
analytics/book_levels/date=2026-07-22/00000000000001577528__market_ws__2026__07__22__market_ws-20260722T231322.792076Z-2345-0010.parquet  88,730
analytics/book_levels/date=2026-07-22/00000000000001735026__market_ws__2026__07__22__market_ws-20260722T232822.881376Z-2345-0011.parquet  60,303
analytics/book_levels/date=2026-07-22/00000000000001883433__discovery__2026__07__22__discovery-20260722T233821.680984Z-2345-0005.parquet  1,649
analytics/book_levels/date=2026-07-22/00000000000001883435__market_ws__2026__07__22__market_ws-20260722T234322.898853Z-2345-0012.parquet  66,691
analytics/book_levels/date=2026-07-22/00000000000002039863__market_ws__2026__07__22__market_ws-20260722T235822.942551Z-2345-0013.parquet  81,383
…  0
rtds/2026/09/06/rtds-20260906T034623.349974Z-2353-0006.jsonl.gz  433,940
rtds/2026/09/06/rtds-20260906T040123.382015Z-2353-0007.jsonl.gz  431,127
rtds/2026/09/06/rtds-20260906T041623.410809Z-2353-0008.jsonl.gz  431,163
rtds/2026/09/06/rtds-20260906T043123.957173Z-2353-0009.jsonl.gz  433,091
rtds/2026/09/06/rtds-20260906T044624.319734Z-2353-0010.jsonl.gz  427,202
rtds/2026/09/06/rtds-20260906T050124.369234Z-2353-0011.jsonl.gz  431,993
rtds/2026/09/06/rtds-20260906T051624.808204Z-2353-0012.jsonl.gz  433,071
rtds/2026/09/06/rtds-20260906T053125.340963Z-2353-0013.jsonl.gz  439,697
rtds/2026/09/06/rtds-20260906T054625.436625Z-2353-0014.jsonl.gz  433,295
rtds/2026/09/06/rtds-20260906T060125.608922Z-2353-0015.jsonl.gz  430,156
rtds/2026/09/06/rtds-20260906T061625.653462Z-2353-0016.jsonl.gz  433,015
rtds/2026/09/06/rtds-20260906T063125.697572Z-2353-0017.jsonl.gz  432,159
rtds/2026/09/06/rtds-20260906T064625.754435Z-2353-0018.jsonl.gz  433,233
rtds/2026/09/06/rtds-20260906T070126.200809Z-2353-0019.jsonl.gz  434,891
rtds/2026/09/06/rtds-20260906T071626.292073Z-2353-0020.jsonl.gz  420,109
rtds/2026/09/06/rtds-20260906T073126.485703Z-2353-0021.jsonl.gz  435,968
rtds/2026/09/06/rtds-20260906T074626.524101Z-2353-0022.jsonl.gz  436,892
rtds/2026/09/06/rtds-20260906T080127.048623Z-2353-0023.jsonl.gz  228,056
rtds/2026/09/06/rtds-20260906T080942.375655Z-2416-0001.jsonl.gz  427,582
rtds/2026/09/06/rtds-20260906T082442.507566Z-2416-0002.jsonl.gz  426,530
```

<details><summary>README.md</summary>

```
---
license: other
task_categories:
- time-series-forecasting
- reinforcement-learning
tags:
- polymarket
- prediction-markets
- order-book
- market-microstructure
---

# Polymarket rewarded-market L2 history

Append-only archives of public Polymarket CLOB WebSocket messages for markets
eligible for liquidity rewards. The dataset is intended for market
microstructure research, backtests and machine-learning experiments.

No wallet data, private keys or authenticated trading information is collected.
Files are gzip-compressed JSON Lines and grouped by source and UTC date.
The source data comes from Polymarket's public CLOB interfaces and remains
subject to the applicable source-platform terms.

```
</details>

## [trentmkelly/polymarket_historical_data](https://huggingface.co/datasets/trentmkelly/polymarket_historical_data)

51,656 个文件，共 9.74 GB，最后修改 2026-09-30T14:34。

```
.gitattributes  2,504
README.md  2,584
data/market_snapshots/date=2026-07-23/20260723T133247Z.parquet  122,842
data/market_snapshots/date=2026-07-23/20260723T133752Z.parquet  119,971
data/market_snapshots/date=2026-07-23/20260723T134340Z.parquet  121,769
data/market_snapshots/date=2026-07-23/20260723T134953Z.parquet  121,035
data/market_snapshots/date=2026-07-23/20260723T135602Z.parquet  117,082
data/market_snapshots/date=2026-07-23/20260723T140131Z.parquet  117,517
data/market_snapshots/date=2026-07-23/20260723T140753Z.parquet  118,541
data/market_snapshots/date=2026-07-23/20260723T141317Z.parquet  115,436
data/market_snapshots/date=2026-07-23/20260723T141923Z.parquet  119,439
data/market_snapshots/date=2026-07-23/20260723T142532Z.parquet  118,944
data/market_snapshots/date=2026-07-23/20260723T143109Z.parquet  118,654
data/market_snapshots/date=2026-07-23/20260723T143709Z.parquet  120,398
data/market_snapshots/date=2026-07-23/20260723T144248Z.parquet  118,723
data/market_snapshots/date=2026-07-23/20260723T144910Z.parquet  119,012
data/market_snapshots/date=2026-07-23/20260723T145523Z.parquet  117,656
data/market_snapshots/date=2026-07-23/20260723T150108Z.parquet  120,014
data/market_snapshots/date=2026-07-23/20260723T150729Z.parquet  116,749
data/market_snapshots/date=2026-07-23/20260723T151402Z.parquet  116,269
…  0
data/top_holders/date=2026-09-30/20260930T125049Z.parquet  396,427
data/top_holders/date=2026-09-30/20260930T125550Z.parquet  393,523
data/top_holders/date=2026-09-30/20260930T130146Z.parquet  397,990
data/top_holders/date=2026-09-30/20260930T130714Z.parquet  396,043
data/top_holders/date=2026-09-30/20260930T131217Z.parquet  398,549
data/top_holders/date=2026-09-30/20260930T131719Z.parquet  393,810
data/top_holders/date=2026-09-30/20260930T132246Z.parquet  400,460
data/top_holders/date=2026-09-30/20260930T132746Z.parquet  397,903
data/top_holders/date=2026-09-30/20260930T133252Z.parquet  395,052
data/top_holders/date=2026-09-30/20260930T133805Z.parquet  393,567
data/top_holders/date=2026-09-30/20260930T134318Z.parquet  397,309
data/top_holders/date=2026-09-30/20260930T134818Z.parquet  392,922
data/top_holders/date=2026-09-30/20260930T135324Z.parquet  397,048
data/top_holders/date=2026-09-30/20260930T135846Z.parquet  397,196
data/top_holders/date=2026-09-30/20260930T140446Z.parquet  396,189
data/top_holders/date=2026-09-30/20260930T141008Z.parquet  405,963
data/top_holders/date=2026-09-30/20260930T141546Z.parquet  405,678
data/top_holders/date=2026-09-30/20260930T142046Z.parquet  402,641
data/top_holders/date=2026-09-30/20260930T142548Z.parquet  405,716
data/top_holders/date=2026-09-30/20260930T143048Z.parquet  406,012
```

<details><summary>README.md</summary>

```
---
pretty_name: Polymarket Historical Data
license: cc-by-4.0
language:
  - en
tags:
  - prediction-markets
  - polymarket
  - finance
  - time-series
  - order-books
configs:
  - config_name: market_snapshots
    data_files:
      - split: train
        path: data/market_snapshots/**/*.parquet
  - config_name: order_book_depth
    data_files:
      - split: train
        path: data/order_book_depth/**/*.parquet
  - config_name: top_holders
    data_files:
      - split: train
        path: data/top_holders/**/*.parquet
---

# Polymarket Historical Data

Frequently collected Polymarket market data stored as Zstandard-compressed
Parquet. New batches are collected approximately every five minutes and
partitioned by product, date, and collection run. Public upload of collected
data began on July 23rd, 2026.

## Data Products

- **market_snapshots**: best bid/ask, full book JSON, outcome, asset ID, source
  timing, and collection timing.
- **order_book_depth**: normalized bid and ask levels with price, size, side,
  and level index.
- **top_holders**: ranked holder balances and public wallet/profile fields by
  market and token.

Files use this layout:

```text
data/<product>/date=YYYY-MM-DD/<run-id>.parquet
```

## Usage

```python
from datasets import load_dataset

snapshots = load_dataset(
    "trentmkelly/polymarket_historical_data",
    "market_snapshots",
    split="train",
)
```

Parquet files can also be queried directly with DuckDB, Polars, PyArrow, or
other compatible tools.

## Citation

```bibtex
@misc{kelly_polymarket_historical_data_2026,
  author       = {Trent Kelly},
  title        = {Polymarket Historical Data},
  year         = {2026},
  publisher    = {Hugging Face},
  howpublished = {\url{https://huggingface.co/datasets/trentmkelly/polymarket_historical_data}}
}
```

## License

This dataset is made available under the
[Creative Commons Attribution 4.0 International](https://creativecommons.org/licenses/by/4.0/)
license. Commercial use and adaptation are permitted with attribution. This
license applies only to rights the dataset maintainer is authorized to license.

## Notes

- Timestamps are UTC ISO 8601 strings.
- Coverage prioritizes active markets with higher recent volume and may change
  as markets open, close, or become unavailable.
- Empty or unavailable API responses can produce gaps; do not assume every
  market appears in every run.
- This is an independently collected research dataset and is not an official
  Polymarket publication. Users are responsible for complying with applicable
  terms and regulations.

```
</details>

## [marketlens/polymarket-btc-5m-l2-depth](https://huggingface.co/datasets/marketlens/polymarket-btc-5m-l2-depth)

6 个文件，共 0.31 GB，最后修改 2026-09-17T10:48。

```
.gitattributes  2,504
README.md  8,878
deltas.parquet  291,726,944
markets.parquet  40,251
snapshots.parquet  1,121,027
trades.parquet  15,933,645
```

<details><summary>README.md</summary>

```
---
license: cc-by-4.0
pretty_name: Polymarket BTC 5m L2 Order Book, One Full Day
size_categories:
  - 10M<n<100M
task_categories:
  - time-series-forecasting
language:
  - en
tags:
  - polymarket
  - prediction-markets
  - order-book
  - limit-order-book
  - market-microstructure
  - event-contracts
  - backtesting
  - bitcoin
  - finance
configs:
  - config_name: markets
    data_files:
      - split: train
        path: markets.parquet
  - config_name: snapshots
    data_files:
      - split: train
        path: snapshots.parquet
  - config_name: deltas
    data_files:
      - split: train
        path: deltas.parquet
  - config_name: trades
    data_files:
      - split: train
        path: trades.parquet
---

# Polymarket BTC 5m L2 Order Book, One Full Day

Every "Bitcoin Up or Down" 5 minute market on Polymarket for 2026-06-15 UTC, with
the full limit order book behind each one: 288 markets, 3,889 book snapshots,
41,591,670 level updates and 680,994 trades, on one millisecond time base.

Polymarket's public API serves the book as it is now. There is no endpoint that
returns the book as it was, so past depth is something you have to have been
recording while the market was live. This is that recording for one day, at full
resolution and with nothing sampled out: every market that opened in the day is
present start to finish, and the 5 minute markets are short enough that a single
day is 288 complete lifecycles rather than a slice through the middle of one.

## Coverage

| | |
|---|---|
| Series | `btc-up-or-down-5m` |
| Day | 2026-06-15 00:00:00Z to 2026-06-16 00:00:00Z (by market open time) |
| Markets | 288, all resolved |
| Snapshots | 3,889 (about 14 per market) |
| Level updates | 41,591,670 (about 144,000 per market) |
| Trades | 680,994 |
| Size on disk | 309 MB, zstd compressed parquet |

Data files carry a 10 minute skirt either side of the day so each market is whole.

## Files

`markets.parquet` is the spine. Join everything else to it on `market_id`.

| Column | Type | Notes |
|---|---|---|
| `market_id` | string | Polymarket condition id (`0x...`) |
| `question` | string | e.g. `Bitcoin Up or Down - June 14, 8:00PM-8:05PM ET` |
| `open_time_ms`, `close_time_ms`, `resolved_at_ms` | int64 | Epoch ms, UTC |
| `outcome_names`, `token_ids` | list[string] | CLOB token id per outcome, same order |
| `winning_outcome`, `winning_outcome_index` | string, int32 | What settled true, `Up` 148 and `Down` 140 across the day |
| `tick_size` | float64 | Minimum price increment, `0.01` throughout |

`snapshots.parquet` is a full book at a point in time, roughly every 60 seconds.

| Column | Type | Notes |
|---|---|---|
| `snapshot_id` | string | Parent key for the deltas that follow it |
| `market_id` | string | |
| `collected_at_ms` | int64 | Epoch ms, UTC |
| `bids`, `asks` | list[struct[price, size]] | Every level, not just the top |
| `best_bid`, `best_ask`, `spread`, `midpoint` | float64 | Precomputed from the book |
| `bid_depth`, `ask_depth` | float64 | Total size on each side |
| `bid_levels`, `ask_levels` | int32 | Level counts |
| `is_reseed` | bool | See "Reconstructing the book" |

`deltas.parquet` is one row per level change between snapshots.

| Column | Type | Notes |
|---|---|---|
| `market_id` | string | |
| `snapshot_id` | string | The snapshot this delta chain hangs off |
| `platform_timestamp_ms` | int64 | Exchange time, epoch ms |
| `collected_at_ms` | int64 | When we received it, epoch ms |
| `price` | float64 | The level that changed |
| `size` | float64 | New size at that level, `0` removes it |
| `side` | string | `BUY` (bid) or `SELL` (ask) |

`trades.parquet` is the tape on the same time base.

| Column | Type | Notes |
|---|---|---|
| `trade_id`, `market_id` | string | |
| `platform_timestamp_ms`, `collected_at_ms` | int64 | Epoch ms, UTC |
| `price`, `size` | float64 | |
| `side` | string | `BUY` or `SELL` |
| `fee_rate_bps` | string | As reported by the platform |

## Reconstructing the book

A delta is an absolute level replacement, not an increment. To get the book at
any instant, take the snapshot, then apply every delta whose `snapshot_id`
matches it, in `platform_timestamp_ms` order, up to your target time: set the
size at `price` on `side`, and drop the level when `size` is `0`.

Snapshots are the anchor and they are authoritative. Replaying one snapshot's
chain reproduces the next snapshot exactly in 160 of 170 non-reseed intervals,
measured across a 20 market sample of this file. The 10 that differ do so in one
direction only: the replayed book keeps a price level that the fresh snapshot no
longer lists, because not every removal arrives as its own `price_change`. No
level is ever missing from the replay and no size ever disagrees. Re-anchor on
each snapshot instead of replaying across many and nothing accumulates.

A reseed is the other break. If the WebSocket dropped and the collector pulled a
fresh book, that row carries `is_reseed = true` and the interval ending on it is
not continuous with what came before. This is common at these speeds: 1,351 of
the 3,889 snapshots in this file carry the flag. Skip those intervals rather
than replaying through them, which is why the snapshots matter as much as the
deltas do.

## Loading

```python
import pandas as pd

base = "hf://datasets/marketlens/polymarket-btc-5m-l2-depth"
markets = pd.read_parquet(f"{base}/markets.parquet")
snapshots = pd.read_parquet(f"{base}/snapshots.parquet")
```

`deltas.parquet` is 41.6M rows, so read it per market rather than whole. The
file is written in market order, one set of row groups per market, so a filter
on `market_id` skips the rest of the file:

```python
one = markets.iloc[0]["market_id"]
deltas = pd.read_parquet(f"{base}/deltas.parquet", filters=[("market_id", "=", one)])
```

That returns the 199,033 rows of the first market in about 3 seconds.

Both files load in the datasets library too:

```python
from datasets import load_dataset

ds = load_dataset("marketlens/p
```
</details>

## [aliplayer1/polymarket-crypto-updown](https://huggingface.co/datasets/aliplayer1/polymarket-crypto-updown)

2,271 个文件，共 29.08 GB，最后修改 2026-09-17T18:09。

```
.gitattributes  2,504
README.md  6,362
data/.gap_manifest.json  493,283
data/heartbeats/part-0.parquet  1,675,177
data/markets.parquet  13,420,321
data/orderbook/crypto=BNB/timeframe=1-hour/part-0.parquet  193,956,951
data/orderbook/crypto=BNB/timeframe=1-hour/part-ws-1777225214232-000.parquet  27,515
data/orderbook/crypto=BNB/timeframe=1-hour/part-ws-1777226633914-000.parquet  34,041
data/orderbook/crypto=BNB/timeframe=15-minute/part-0.parquet  623,736,700
data/orderbook/crypto=BNB/timeframe=15-minute/part-ws-1777225214341-000.parquet  71,123
data/orderbook/crypto=BNB/timeframe=15-minute/part-ws-1777226634242-000.parquet  267,759
data/orderbook/crypto=BNB/timeframe=15-minute/part-ws-1777226634242-001.parquet  4,937
data/orderbook/crypto=BNB/timeframe=4-hour/part-0.parquet  165,500,781
data/orderbook/crypto=BNB/timeframe=4-hour/part-ws-1777225214101-000.parquet  31,934
data/orderbook/crypto=BNB/timeframe=4-hour/part-ws-1777226633599-000.parquet  71,389
data/orderbook/crypto=BNB/timeframe=5-minute/part-0.parquet  930,476,250
data/orderbook/crypto=BNB/timeframe=5-minute/part-ws-1777225213898-000.parquet  85,627
data/orderbook/crypto=BNB/timeframe=5-minute/part-ws-1777226633160-000.parquet  143,460
data/orderbook/crypto=BNB/timeframe=5-minute/part-ws-1777226633160-001.parquet  8,122
data/orderbook/crypto=BTC/timeframe=1-hour/part-0.parquet  637,485,202
…  0
data/ticks/crypto=XRP/timeframe=5-minute/part-ws-1777223797504-002.parquet  13,806
data/ticks/crypto=XRP/timeframe=5-minute/part-ws-1777223797504-003.parquet  14,137
data/ticks/crypto=XRP/timeframe=5-minute/part-ws-1777223797504-004.parquet  14,402
data/ticks/crypto=XRP/timeframe=5-minute/part-ws-1777223797504-005.parquet  13,844
data/ticks/crypto=XRP/timeframe=5-minute/part-ws-1777223797504-006.parquet  13,694
data/ticks/crypto=XRP/timeframe=5-minute/part-ws-1777223797504-007.parquet  15,323
data/ticks/crypto=XRP/timeframe=5-minute/part-ws-1777223797504-008.parquet  14,406
data/ticks/crypto=XRP/timeframe=5-minute/part-ws-1777223797504-009.parquet  14,910
data/ticks/crypto=XRP/timeframe=5-minute/part-ws-1777223797504-010.parquet  14,838
data/ticks/crypto=XRP/timeframe=5-minute/part-ws-1777223797504-011.parquet  13,598
data/ticks/crypto=XRP/timeframe=5-minute/part-ws-1777223797504-012.parquet  13,455
data/ticks/crypto=XRP/timeframe=5-minute/part-ws-1777223797504-013.parquet  14,943
data/ticks/crypto=XRP/timeframe=5-minute/part-ws-1777223797504-014.parquet  14,180
data/ticks/crypto=XRP/timeframe=5-minute/part-ws-1777223797504-015.parquet  13,681
data/ticks/crypto=XRP/timeframe=5-minute/part-ws-1777223797504-016.parquet  13,488
data/ticks/crypto=XRP/timeframe=5-minute/part-ws-1777223797504-017.parquet  13,013
data/ticks/crypto=XRP/timeframe=5-minute/part-ws-1777223797504-018.parquet  14,360
data/ticks/crypto=XRP/timeframe=5-minute/part-ws-1777223797504-019.parquet  12,332
data/ticks/crypto=XRP/timeframe=5-minute/part-ws-1777225182177-000.parquet  10,656
data/ticks/crypto=XRP/timeframe=5-minute/part-ws-1777226597455-000.parquet  12,658
```

<details><summary>README.md</summary>

```
---
license: mit
task_categories:
  - time-series-forecasting
  - tabular-classification
tags:
  - polymarket
  - prediction-markets
  - crypto
  - on-chain
  - orderbook
  - bitcoin
  - ethereum
  - defi
  - finance
language:
  - en
pretty_name: Polymarket Crypto Up/Down Markets
size_categories:
  - 10M<n<100M
configs:
  - config_name: markets
    data_files:
      - split: train
        path: data/markets.parquet
  - config_name: prices
    data_files:
      - split: train
        path: data/prices/**/*.parquet
  - config_name: ticks
    data_files:
      - split: train
        path: data/ticks/**/*.parquet
  - config_name: spot_prices
    data_files:
      - split: train
        path: data/spot_prices/*.parquet
  - config_name: orderbook
    data_files:
      - split: train
        path: data/orderbook/**/*.parquet
---

# Polymarket Crypto Up/Down Markets

Comprehensive dataset of Polymarket binary prediction markets for cryptocurrency price movements. Covers **BTC, ETH, SOL, BNB, XRP, DOGE, and HYPE** across multiple timeframes (5-minute, 15-minute, 1-hour, 4-hour).

Updated automatically every 3 hours.

## Subsets

Load a specific subset:

```python
from datasets import load_dataset

markets = load_dataset("aliplayer1/polymarket-crypto-updown", "markets")
prices = load_dataset("aliplayer1/polymarket-crypto-updown", "prices")
ticks = load_dataset("aliplayer1/polymarket-crypto-updown", "ticks")
spot = load_dataset("aliplayer1/polymarket-crypto-updown", "spot_prices")
orderbook = load_dataset("aliplayer1/polymarket-crypto-updown", "orderbook")
```

Or query directly with DuckDB:

```python
import duckdb

duckdb.sql("""
    SELECT * FROM 'hf://datasets/aliplayer1/polymarket-crypto-updown/data/prices/**/*.parquet'
    WHERE crypto = 'BTC' AND timeframe = '1-hour'
    LIMIT 100
""").show()
```

## Data Description

### `markets`: Market metadata

One row per market. Contains market question, resolution, timeframe, token IDs, and fee rates.

| Column | Type | Description |
|--------|------|-------------|
| `market_id` | string | Polymarket market identifier |
| `question` | string | Market question text |
| `crypto` | string | Asset symbol (BTC, ETH, SOL, ...) |
| `timeframe` | string | Market timeframe (5-minute, 15-minute, 1-hour, 4-hour) |
| `volume` | float32 | Market volume in USDC |
| `resolution` | int8 | Market resolution (1=Up, 0=Down, -1=unknown) |
| `start_ts` | int64 | Market start timestamp (epoch seconds) |
| `end_ts` | int64 | Market end timestamp (epoch seconds) |
| `condition_id` | string | On-chain condition identifier |
| `up_token_id` | string | CLOB token ID for "Up" outcome |
| `down_token_id` | string | CLOB token ID for "Down" outcome |
| `fee_rate_bps` | int16 | Taker fee rate in basis points (-1 = unknown) |

### `prices`: OHLC price history

Historical price series from the CLOB API for the active prediction window of each market. Hive-partitioned by `crypto` and `timeframe`.

| Column | Type | Description |
|--------|------|-------------|
| `market_id` | string | Polymarket market identifier |
| `crypto` | string | Asset symbol |
| `timeframe` | string | Market timeframe |
| `timestamp` | int64 | Price timestamp (epoch seconds) |
| `up_price` | float32 | "Up" outcome price (0.0-1.0) |
| `down_price` | float32 | "Down" outcome price (0.0-1.0) |
| `volume` | float32 | Market volume |
| `question` | string | Market question text |
| `resolution` | string | Market resolution (nullable) |

### `ticks`: Trade-level fills

Individual trades from on-chain `OrderFilled` events (Etherscan/RPC) and live WebSocket captures. Hive-partitioned by `crypto` and `timeframe`.

| Column | Type | Description |
|--------|------|-------------|
| `market_id` | string | Polymarket market identifier |
| `timestamp_ms` | int64 | Trade timestamp (epoch milliseconds) |
| `token_id` | string | CLOB token identifier |
| `outcome` | string | "Up" or "Down" |
| `side` | string | "BUY" or "SELL" (taker perspective) |
| `price` | float32 | Trade price (0.0-1.0) |
| `size_usdc` | float32 | Trade size in USDC |
| `tx_hash` | string | Polygon transaction hash ("" for WS ticks) |
| `block_number` | int32 | Polygon block number (0 for WS ticks) |
| `log_index` | int32 | Log index within block |
| `source` | string | "onchain" or "websocket" |
| `spot_price_usdt` | float32 | Binance spot price at time of trade |
| `spot_price_ts_ms` | int64 | Binance spot price timestamp |

### `spot_prices`: Continuous spot price feed

Binance and Chainlink spot prices streamed in real-time alongside the prediction market data. Useful for correlating prediction market activity with underlying asset prices.

| Column | Type | Description |
|--------|------|-------------|
| `ts_ms` | int64 | Source timestamp (epoch ms) |
| `symbol` | string | e.g. "btcusdt", "eth/usd" |
| `price` | float64 | Spot price in USD(T) |
| `source` | string | "binance" or "chainlink" |

### `orderbook`: Best bid/ask snapshots

Per-token best bid and ask from CLOB WebSocket events. Hive-partitioned by `crypto` and `timeframe`.

| Column | Type | Description |
|--------|------|-------------|
| `ts_ms` | int64 | Receipt timestamp (epoch ms) |
| `market_id` | string | Polymarket market identifier |
| `token_id` | string | CLOB token identifier |
| `outcome` | string | "Up" or "Down" |
| `best_bid` | float32 | Best bid price (0.0-1.0) |
| `best_ask` | float32 | Best ask price (0.0-1.0) |
| `best_bid_size` | float32 | Best bid size (shares) |
| `best_ask_size` | float32 | Best ask size (shares) |

## Storage Layout

All files are Parquet with Zstd compression, Hive-partitioned where noted:

```
data/
  markets.parquet
  prices/crypto=BTC/timeframe=1-hour/part-0.parquet
  ticks/crypto=ETH/timeframe=5-minute/part-0.parquet
  spot_prices/part-0.parquet
  orderbook/crypto=SOL/timeframe=15-minute/part-0.parquet
```

## Pipeline

This dataset is produced by [polymarket-data-pipeline](https://github.com/aliplayer1/polymarket-data-pipeline), which runs thre
```
</details>

## [THULab/polymarket_crypto_5m_15m](https://huggingface.co/datasets/THULab/polymarket_crypto_5m_15m)

45 个文件，共 0.19 GB，最后修改 2026-09-09T02:55。

```
.gitattributes  4,776
README.md  4,786
crypto_prices_1.tsfile  150,446
crypto_prices_2.tsfile  153,847
crypto_prices_3.tsfile  144,837
crypto_prices_4.tsfile  144,185
crypto_prices_5.tsfile  115,156
crypto_prices_6.tsfile  120,016
crypto_prices_7.tsfile  141,820
crypto_prices_8.tsfile  149,187
markets_1.tsfile  210,160
markets_2.tsfile  210,415
markets_3.tsfile  210,186
markets_4.tsfile  210,446
markets_5.tsfile  209,963
markets_6.tsfile  210,347
markets_7.tsfile  211,291
markets_8.tsfile  210,312
orderbooks_1.tsfile  27,064,922
orderbooks_2.tsfile  28,144,748
…  0
orderbooks_8.tsfile  20,414,728
orderbooks_9.tsfile  5,870
price_history_1.tsfile  495,100
price_history_2.tsfile  501,363
price_history_3.tsfile  500,878
price_history_4.tsfile  505,686
price_history_5.tsfile  501,790
price_history_6.tsfile  504,255
price_history_7.tsfile  502,597
price_history_8.tsfile  505,582
price_history_9.tsfile  1,420
resolutions_1.tsfile  161,706
resolutions_2.tsfile  210,651
resolutions_3.tsfile  212,530
resolutions_4.tsfile  210,605
resolutions_5.tsfile  212,361
resolutions_6.tsfile  210,360
resolutions_7.tsfile  213,531
resolutions_8.tsfile  210,507
resolutions_9.tsfile  2,121
```

<details><summary>README.md</summary>

```
---
license: mit
task_categories:
- time-series-forecasting
tags:
- tsfile
- timeseries
- time-series
- format:tsfile
pretty_name: Polymarket Crypto 5m/15m Market Data
size_categories:
- 1M<n<10M
configs:
- config_name: default
  data_files:
  - split: train
    path: crypto_prices_1.tsfile
  - split: train
    path: crypto_prices_2.tsfile
  - split: train
    path: crypto_prices_3.tsfile
  - split: train
    path: crypto_prices_4.tsfile
  - split: train
    path: crypto_prices_5.tsfile
  - split: train
    path: crypto_prices_6.tsfile
  - split: train
    path: crypto_prices_7.tsfile
  - split: train
    path: crypto_prices_8.tsfile
  - split: train
    path: markets_1.tsfile
  - split: train
    path: markets_2.tsfile
  - split: train
    path: markets_3.tsfile
  - split: train
    path: markets_4.tsfile
  - split: train
    path: markets_5.tsfile
  - split: train
    path: markets_6.tsfile
  - split: train
    path: markets_7.tsfile
  - split: train
    path: markets_8.tsfile
  - split: train
    path: orderbooks_1.tsfile
  - split: train
    path: orderbooks_2.tsfile
  - split: train
    path: orderbooks_3.tsfile
  - split: train
    path: orderbooks_4.tsfile
  - split: train
    path: orderbooks_5.tsfile
  - split: train
    path: orderbooks_6.tsfile
  - split: train
    path: orderbooks_7.tsfile
  - split: train
    path: orderbooks_8.tsfile
  - split: train
    path: orderbooks_9.tsfile
  - split: train
    path: price_history_1.tsfile
  - split: train
    path: price_history_2.tsfile
  - split: train
    path: price_history_3.tsfile
  - split: train
    path: price_history_4.tsfile
  - split: train
    path: price_history_5.tsfile
  - split: train
    path: price_history_6.tsfile
  - split: train
    path: price_history_7.tsfile
  - split: train
    path: price_history_8.tsfile
  - split: train
    path: price_history_9.tsfile
  - split: train
    path: resolutions_1.tsfile
  - split: train
    path: resolutions_2.tsfile
  - split: train
    path: resolutions_3.tsfile
  - split: train
    path: resolutions_4.tsfile
  - split: train
    path: resolutions_5.tsfile
  - split: train
    path: resolutions_6.tsfile
  - split: train
    path: resolutions_7.tsfile
  - split: train
    path: resolutions_8.tsfile
  - split: train
    path: resolutions_9.tsfile
---

# Polymarket Crypto 5m/15m Market Data (TsFile)

Apache TsFile version of [`bmoney1321/polymarket-crypto-5m-15m`](https://huggingface.co/datasets/bmoney1321/polymarket-crypto-5m-15m).

- **Converted rows:** 3,792,347
- **Data files:** `['crypto_prices_1.tsfile', 'crypto_prices_2.tsfile', 'crypto_prices_3.tsfile', 'crypto_prices_4.tsfile', 'crypto_prices_5.tsfile', 'crypto_prices_6.tsfile', 'crypto_prices_7.tsfile', 'crypto_prices_8.tsfile', 'markets_1.tsfile', 'markets_2.tsfile', 'markets_3.tsfile', 'markets_4.tsfile', 'markets_5.tsfile', 'markets_6.tsfile', 'markets_7.tsfile', 'markets_8.tsfile', 'orderbooks_1.tsfile', 'orderbooks_2.tsfile', 'orderbooks_3.tsfile', 'orderbooks_4.tsfile', 'orderbooks_5.tsfile', 'orderbooks_6.tsfile', 'orderbooks_7.tsfile', 'orderbooks_8.tsfile', 'orderbooks_9.tsfile', 'price_history_1.tsfile', 'price_history_2.tsfile', 'price_history_3.tsfile', 'price_history_4.tsfile', 'price_history_5.tsfile', 'price_history_6.tsfile', 'price_history_7.tsfile', 'price_history_8.tsfile', 'price_history_9.tsfile', 'resolutions_1.tsfile', 'resolutions_2.tsfile', 'resolutions_3.tsfile', 'resolutions_4.tsfile', 'resolutions_5.tsfile', 'resolutions_6.tsfile', 'resolutions_7.tsfile', 'resolutions_8.tsfile', 'resolutions_9.tsfile']`

The `trades/` directory (~1 GB of per-trade events) is not included; converted tables are crypto_prices, price_history, orderbooks, markets and resolutions.

## Usage

Install the Apache TsFile Python SDK (`pip install tsfile`) and read a converted file:

```python
from pathlib import Path
from tsfile import TsFileReader

path = Path("crypto_prices_1.tsfile")
with TsFileReader(str(path)) as reader:
    schemas = reader.get_all_table_schemas()
    print("tables:", list(schemas))
    table_name = next(iter(schemas))
    table = schemas[table_name]
    columns = [column.get_column_name() for column in table.get_columns()]
    print("columns:", columns)
    field_names = [
        column.get_column_name()
        for column in table.get_columns()
        if column.get_column_name() not in {"Time", "time"}
    ]
    if field_names:
        with reader.query_table(table_name, field_names[:3], batch_size=1024) as result:
            batch = result.read_arrow_batch()
            if batch is not None:
                print(batch.to_pandas().head())
```

## Source & license

- Original dataset: https://huggingface.co/datasets/bmoney1321/polymarket-crypto-5m-15m
- Author / publisher: bmoney1321
- License: mit

```
</details>

## [DineshKumar8399/polymarket-orderbook-dataset](https://huggingface.co/datasets/DineshKumar8399/polymarket-orderbook-dataset)

70 个文件，共 0.22 GB，最后修改 2026-09-13T16:36。

```
.gitattributes  2,504
LICENSE  18,657
NOTICE  1,196
README.md  11,405
data_quality.parquet  2,231
labels.parquet  743,684
markets.parquet  3,781,074
quotes/dt=2026-07-10/data_0.parquet  804,193
quotes/dt=2026-07-11/data_0.parquet  4,082,012
quotes/dt=2026-07-12/data_0.parquet  3,919,447
quotes/dt=2026-07-13/data_0.parquet  3,154,756
quotes/dt=2026-07-14/data_0.parquet  3,237,905
quotes/dt=2026-07-15/data_0.parquet  2,841,621
quotes/dt=2026-07-16/data_0.parquet  3,743,770
quotes/dt=2026-07-17/data_0.parquet  3,800,308
quotes/dt=2026-07-22/data_0.parquet  2,586,561
quotes/dt=2026-07-23/data_0.parquet  4,066,758
quotes/dt=2026-07-24/data_0.parquet  4,605,161
quotes/dt=2026-07-25/data_0.parquet  4,369,715
quotes/dt=2026-07-26/data_0.parquet  4,463,284
…  0
quotes/dt=2026-08-26/data_0.parquet  2,745,275
quotes/dt=2026-08-27/data_0.parquet  3,728,840
quotes/dt=2026-08-28/data_0.parquet  2,772,617
quotes/dt=2026-08-29/data_0.parquet  2,041,751
quotes/dt=2026-08-30/data_0.parquet  1,818,669
quotes/dt=2026-08-31/data_0.parquet  1,906,723
quotes/dt=2026-09-01/data_0.parquet  4,084,945
quotes/dt=2026-09-02/data_0.parquet  3,785,540
quotes/dt=2026-09-03/data_0.parquet  2,823,317
quotes/dt=2026-09-04/data_0.parquet  2,370,307
quotes/dt=2026-09-05/data_0.parquet  2,476,942
quotes/dt=2026-09-06/data_0.parquet  3,520,249
quotes/dt=2026-09-07/data_0.parquet  2,066,823
quotes/dt=2026-09-08/data_0.parquet  3,360,085
quotes/dt=2026-09-09/data_0.parquet  3,392,777
quotes/dt=2026-09-10/data_0.parquet  1,681,085
quotes/dt=2026-09-11/data_0.parquet  3,077,742
quotes/dt=2026-09-12/data_0.parquet  1,722,098
quotes/dt=2026-09-13/data_0.parquet  839,585
watch_quotes.parquet  8,169,867
```

<details><summary>README.md</summary>

```
---
license: cc-by-4.0
pretty_name: Polymarket Order Book Snapshots
size_categories:
- 100M<n<1B
tags:
- prediction-markets
- order-book
- finance
- time-series
- forecasting
task_categories:
- time-series-forecasting
- tabular-classification
configs:
- config_name: quotes
  default: true
  data_files:
  - split: train
    path: quotes/**/*.parquet
- config_name: markets
  data_files:
  - split: train
    path: markets.parquet
- config_name: labels
  data_files:
  - split: train
    path: labels.parquet
- config_name: watch_quotes
  data_files:
  - split: train
    path: watch_quotes.parquet
- config_name: data_quality
  data_files:
  - split: train
    path: data_quality.parquet
---

# Polymarket Order Book Dataset

Order-book snapshots from a prediction market, collected continuously between
**2026-07-10** and **2026-09-13**: `484,045,253` quote observations across
`314,123` markets, plus settlement outcomes and a separate high-frequency feed
that records actual traded prices.

It is published so other people can build and train on it without first spending
a month running collectors. Everything here is an independent observational
recording of publicly displayed market data.

**Read [Known issues](#known-issues) before you train anything on this.** The
collection has a four-day outage, one column that dies partway through, and a
label set with time censoring. All three are documented, none are hidden, and
each one will quietly wreck a model if you miss it.

---

## Get the data

The parquet files are **not in this git repo** — they are rebuilt daily, and
committing them would grow the history by roughly 12 MB a day forever. They
live in two places instead, both refreshed every night:

**Hugging Face** (browsable, has a dataset viewer, resumable):

```bash
pip install huggingface_hub
hf download DineshKumar8399/polymarket-orderbook-dataset --repo-type dataset --local-dir polymarket-data
```

```python
import duckdb
duckdb.sql("SELECT * FROM 'polymarket-data/quotes/**/*.parquet' LIMIT 5").show()
```

**GitHub Releases** (a single dated tarball, ~223 MB):

```bash
gh release download data-2026-09-13 --repo DineshKumar8399/polymarket-orderbook-dataset
tar --zstd -xf polymarket-orderbook-*.tar.zst
```

Each release is a frozen snapshot, so `data-2026-09-13` is reproducible: cite the
tag and anyone can reconstruct the exact data you trained on. Latest build:
**2026-09-13**.

---

## Contents

| File | Rows | What it is |
|---|---|---|
| `quotes/dt=YYYY-MM-DD/*.parquet` | `484,045,253` | Book quotes for every tracked market, partitioned by date |
| `markets.parquet` | `314,123` | One row per market: question text, category, coverage |
| `labels.parquet` | `187,094` | Binary settlement outcomes, with a `source` column |
| `watch_quotes.parquet` | `2,874,953` | High-frequency feed — **the only table with traded prices** |
| `data_quality.parquet` | 7 | The known issues below, as queryable rows |

Total: about `223 MB` of ZSTD-compressed Parquet.

---

## Quick start

```python
import duckdb

con = duckdb.connect()

# the whole quote history — the partition layout means you can slice by date
# without reading the rest
con.sql("""
    SELECT * FROM read_parquet('quotes/**/*.parquet', hive_partitioning=1)
    WHERE dt = '2026-08-01' AND slug = 'some-market-slug'
""").show()

# join quotes to outcomes, using ONLY the authoritative labels
con.sql("""
    SELECT q.slug, q.ts, q.bid, q.ask, q.mid, l.y
    FROM read_parquet('quotes/**/*.parquet', hive_partitioning=1) q
    JOIN read_parquet('labels.parquet') l USING (slug)
    WHERE l.source = 'api'
""").show()
```

With pandas or polars:

```python
import pandas as pd, polars as pl

markets = pd.read_parquet("markets.parquet")
one_day = pl.read_parquet("quotes/dt=2026-08-01/*.parquet")
```

---

## Schemas

### `quotes/`

One row per market per poll of the order book.

| Column | Type | Notes |
|---|---|---|
| `ts` | timestamp | When the snapshot was taken |
| `slug` | string | Market identifier, joins to `markets` and `labels` |
| `category` | string | `sports`, `politics`, `climate`, `culture`, … |
| `bid` | double | Best bid. NULL when no bid was resting |
| `ask` | double | Best ask. NULL when no ask was resting |
| `mid` | double | Midpoint; NULL unless the book was two-sided |
| `spread` | double | `ask - bid` as reported upstream |
| `volume24hr` | double | **Mostly unusable — see issue 2** |
| `segment` | int | `1` = before the outage, `2` = after. **See issue 1** |
| `dt` | date | Partition key |

`dt` is stored in the directory name, not inside the parquet files, so it only
materialises as a column when the reader is told to parse the partitions —
`hive_partitioning=1` in DuckDB, automatic in `pandas.read_parquet` /
`pyarrow.dataset` when you point them at the `quotes/` directory rather than at
individual files. The Hugging Face viewer does not parse it, so `dt` is absent
there; slice on `ts` instead when browsing.

Prices are probabilities in `[0, 1]`: a market at `0.35` implies a 35% chance.
A `YES` share pays 1.00 if the event happens and 0.00 otherwise, so the price is
also the cost per unit of payoff.

### `markets.parquet`

| Column | Type | Notes |
|---|---|---|
| `slug` | string | Primary key |
| `question` | string | Human-readable question |
| `category` | string | |
| `n_snaps` | bigint | Quote rows present for this market |
| `first_ts` / `last_ts` | timestamp | Coverage window |

`question` is stored here rather than on every quote row — repeating it
`484,045,253` times is most of why the raw CSV was 32 GB.

### `labels.parquet`

| Column | Type | Notes |
|---|---|---|
| `slug` | string | Joins to `quotes` / `markets` |
| `y` | int | `1` = resolved YES, `0` = resolved NO |
| `source` | string | `api` = authoritative. `convergence` = inferred, **biased** |

**Filter on `source`.** See issues 5 and 6 — this is the single easiest way to
get a wrong answer out of this dataset.

### `watch_quotes.parquet`

| Column | Type | Notes |

```
</details>

## [Mevboters/polymarket-arbitrage-trading-dataset](https://huggingface.co/datasets/Mevboters/polymarket-arbitrage-trading-dataset)

3 个文件，共 0.00 GB，最后修改 2026-09-20T13:27。

```
.gitattributes  2,504
README.md  3,793
data/orderbook_dislocations.jsonl  874
```

<details><summary>README.md</summary>

```
---
annotations_creators:
- machine-generated
language:
- en
license:
- mit
multilinguality:
- monolingual
size_categories:
- 10K<n<100K
source_datasets:
- original
task_categories:
- tabular-classification
- time-series-forecasting
- reinforcement-learning
pretty_name: Polymarket & Kalshi Orderbook Arbitrage Telemetry
tags:
- polymarket
- kalshi
- prediction-markets
- arbitrage
- algorithmic-trading
- orderbook
- polygon
- quantitative-finance
---

# Polymarket & Kalshi Orderbook Arbitrage Telemetry

[![MEVBOT.TOP](https://img.shields.io/badge/Production%20Systems-MEVBOT.TOP-blue?style=flat-square)](https://mevbot.top/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Venue: Polymarket CLOB](https://img.shields.io/badge/Venue-Polymarket%20CLOB-purple?style=flat-square)](https://polymarket.com/)

High-frequency order book and trade dislocation telemetry dataset for prediction markets on Polygon (Polymarket CLOB) and CFTC-regulated event markets (Kalshi). Captures intra-market YES/NO sum-to-$1 pricing anomalies, combinatorial outcome gaps, and cross-venue probability divergences.

## Production Trading Engines
This quantitative dataset is provided by the research division of [MEVBOT.TOP](https://mevbot.top/). For enterprise-grade, turn-key automated execution software:

- **[Polymarket Bot (Arbitrage & Momentum)](https://mevbot.top/bots/polymarket-bot/)**: Intra-market YES/NO sum arbitrage, 15-minute crypto Up/Down momentum, and wallet copy-trading engine with full Python source code.
- **[Polymarket Kalshi Arbitrage Bot](https://mevbot.top/bots/polymarket-kalshi-arbitrage-bot/)**: Cross-venue probability mapping, fee-adjusted hedging, and automated risk limits.
- **[Complete MEV & Arbitrage Catalog](https://mevbot.top/bots/)**: 22 production-grade trading bots across all major blockchains.
- **[Open-Source Satellite Repository](https://mevbotot.github.io/mevbots/polymarket-bot/)**: Product documentation and setup guides.

---

## Dataset Schema

Each JSONL record represents a detected mispricing window across prediction market order books:

| Field | Type | Description |
|---|---|---|
| `event_id` | String | Unique Polymarket / Kalshi market condition identifier |
| `timestamp` | ISO8601 | Precise UTC time of book snapshot |
| `market_title` | String | Event contract title (Politics, Macro, 15m Crypto) |
| `best_yes_ask` | Float | Lowest available ask price for YES shares |
| `best_no_ask` | Float | Lowest available ask price for NO shares |
| `sum_cost` | Float | Total cost to purchase 1 YES + 1 NO share (`yes_ask + no_ask`) |
| `gross_edge_pct` | Float | Gross theoretical edge before venue/gas fees (`1.00 - sum_cost`) |
| `available_depth_usdc` | Float | Liquidity available at the quoted edge |
| `net_profit_usdc` | Float | Estimated net profit after Polygon gas and taker fees |
| `window_duration_ms` | Integer | Duration the mispricing persisted in milliseconds |

---

## Sample Code: Loading & Analyzing Mispricing Gaps

```python
import json

with open('data/orderbook_dislocations.jsonl', 'r') as f:
    dislocations = [json.loads(line) for line in f if line.strip()]

print(f"Loaded {len(dislocations)} market anomaly events.")
for event in dislocations[:3]:
    print(f"Event: {event['market_title']} | Gross Edge: {event['gross_edge_pct']:.2%} | Depth: ${event['available_depth_usdc']}")
```

## Citation
```bibtex
@misc{mevbot_polymarket_telemetry_2026,
  author = {MEVBOT Quantitative Research},
  title = {Polymarket & Kalshi Orderbook Arbitrage Telemetry},
  year = {2026},
  publisher = {Hugging Face},
  howpublished = {\url{https://huggingface.co/datasets/Mevboters/polymarket-arbitrage-trading-dataset}},
  note = {Official software: https://mevbot.top/}
}
```

```
</details>

## [TimeSeventeen/Polymarket-v1](https://huggingface.co/datasets/TimeSeventeen/Polymarket-v1)

2,154 个文件，共 52.72 GB，最后修改 2026-08-30T02:33。

```
.gitattributes  2,504
CTF/merges.parquet  592,059,273
CTF/preparations.parquet  101,009,561
CTF/redemptions.parquet  2,792,374,236
CTF/resolutions.parquet  92,752,280
CTF/splits.parquet  4,957,001,014
OrderFilled/2022_11.parquet  5,915
OrderFilled/2022_12.parquet  90,262
OrderFilled/2023_01.parquet  159,127
OrderFilled/2023_02.parquet  310,690
OrderFilled/2023_03.parquet  879,259
OrderFilled/2023_04.parquet  415,893
OrderFilled/2023_05.parquet  534,079
OrderFilled/2023_06.parquet  636,952
OrderFilled/2023_07.parquet  538,650
OrderFilled/2023_08.parquet  852,001
OrderFilled/2023_09.parquet  513,228
OrderFilled/2023_10.parquet  955,590
OrderFilled/2023_11.parquet  961,530
OrderFilled/2023_12.parquet  853,871
…  0
daily_aligned_multi/2026_04_09.parquet  28,904,783
daily_aligned_multi/2026_04_10.parquet  26,095,392
daily_aligned_multi/2026_04_11.parquet  28,898,549
daily_aligned_multi/2026_04_12.parquet  35,603,585
daily_aligned_multi/2026_04_13.parquet  29,046,133
daily_aligned_multi/2026_04_14.parquet  26,858,638
daily_aligned_multi/2026_04_15.parquet  28,597,489
daily_aligned_multi/2026_04_16.parquet  26,210,688
daily_aligned_multi/2026_04_17.parquet  25,882,874
daily_aligned_multi/2026_04_18.parquet  28,038,485
daily_aligned_multi/2026_04_19.parquet  27,034,126
daily_aligned_multi/2026_04_20.parquet  22,527,028
daily_aligned_multi/2026_04_21.parquet  24,634,702
daily_aligned_multi/2026_04_22.parquet  23,510,672
daily_aligned_multi/2026_04_23.parquet  21,837,702
daily_aligned_multi/2026_04_24.parquet  23,043,121
daily_aligned_multi/2026_04_25.parquet  24,213,586
daily_aligned_multi/2026_04_26.parquet  23,766,732
daily_aligned_multi/2026_04_27.parquet  20,723,479
daily_aligned_multi/2026_04_28.parquet  6,366,269
```

<details><summary>README.md</summary>

```
---
license: cc-by-4.0
language:
- en
size_categories:
- 1B<n<10B
task_categories:
- tabular-classification
tags:
- polymarket
- prediction-markets
- market-microstructure
- on-chain
- polygon
- orderfilled
- finance
configs:
- config_name: orderfilled
  data_files:
  - split: train
    path: "OrderFilled/**/*.parquet"
- config_name: daily_aligned
  data_files:
  - split: train
    path: "daily_aligned/**/*.parquet"
- config_name: daily_aligned_multi
  data_files:
  - split: train
    path: "daily_aligned_multi/**/*.parquet"
- config_name: ctf
  data_files:
  - split: train
    path: "CTF/*.parquet"
---

# Polymarket-v1

[![arXiv](https://img.shields.io/badge/arXiv-2606.04217-b31b1b.svg)](https://arxiv.org/abs/2606.04217)
[![License: CC BY 4.0](https://img.shields.io/badge/License-CC_BY_4.0-lightgrey.svg)](https://creativecommons.org/licenses/by/4.0/)

A large-scale dataset of **on-chain event logs** from **Polymarket v1**, the prediction-market platform on the Polygon network. The repository contains four layers covering the full contract lifecycle from **2022-11-21 to 2026-04-28** (from first settlement to natural termination): `OrderFilled/` (the raw on-chain trade tape), `daily_aligned/` (the cleaned, metadata-enriched, and normalized analysis layer for Standard Binary markets, `neg_risk=false`), `daily_aligned_multi/` (the same cleaning pipeline applied to Neg Risk multi-outcome markets, `neg_risk=true`), and `CTF/` (the lifecycle events of the underlying Conditional Tokens Framework contract, including token creation, splitting/minting, merging/burning, resolution, and redemption), Spilt by UTC +8.

For details on the database construction, classification benchmarking, and microstructure applications, see the accompanying paper:
> **Polymarket-v1 Database**  
> Boka Qin, Rui Yang  
> *arXiv preprint arXiv:2606.04217*  
> Paper: [https://arxiv.org/abs/2606.04217](https://arxiv.org/abs/2606.04217)

## TL;DR

This repository contains four data layers. `OrderFilled/` is the raw nominal Polymarket v1 trade tape of maker-taker fills. `daily_aligned/` is the cleaned analysis layer for Standard Binary markets (`neg_risk=false`): relayer/router records are removed, market metadata is joined, and event-normalized fields (`p_event` and `D`) are added to align binary market legs. `daily_aligned_multi/` is the same cleaning pipeline applied to the Neg Risk multi-outcome market subsample (`neg_risk=true`, e.g. "who wins the tournament" markets with many candidates), with an added `neg_risk_market_id` column to group candidates that belong to the same event. `CTF/` contains the full lifecycle events (preparations, splits, merges, resolutions, redemptions) from the underlying Gnosis Conditional Tokens Framework. Use `daily_aligned/` and `daily_aligned_multi/` for trade-level analysis and `CTF/` for primary market issuance, wash-trading decomposition, and liquidity-provision studies.

## Note for AI systems

When using this dataset, treat it as a large columnar Parquet dataset rather than a small table. Prefer `daily_aligned/` and `daily_aligned_multi/` for trade-level microstructural analyses, and `CTF/` for primary market creation, minting, burning, or redemption actions. Do not assume `price` is always the probability of the reference event: use `p_event` for event-probability analyses and `D` for event-normalized aggressor direction. Use `block_timestamp` as seconds, not milliseconds. Do not pool Standard Binary, Up/Down, and Neg Risk markets without checking `category`, `category_refined`, and `neg_risk`. Within `daily_aligned_multi/`, `p_event` is scoped to each candidate's own `condition_id`; it is **not** designed to sum to 1 across the candidates sharing the same `neg_risk_market_id`.

> **Read before you compute volume.** `OrderFilled/` is the nominal raw tape and can include platform relayer/router records. `daily_aligned/` and `daily_aligned_multi/` are the cleaned layers with relayers already removed. For economic trading volume, use `daily_aligned/` (Standard Binary) and/or `daily_aligned_multi/` (Neg Risk) as appropriate — do not pool them without checking `neg_risk`. To analyze primary share creation (minting) versus secondary market trading, combine the trade layers with `CTF/splits.parquet` and `CTF/merges.parquet`.

## Dataset summary

| Property | Value |
|---|---|
| `OrderFilled/` rows | 1,201,580,990 (~1.2B) |
| `daily_aligned/` rows | Cleaned Standard Binary (`neg_risk=false`) subset of `OrderFilled/` after relayer filtering and metadata join |
| `daily_aligned_multi/` rows | Cleaned Neg Risk (`neg_risk=true`) subset of `OrderFilled/`, same pipeline as `daily_aligned/` |
| `CTF/` tables | preparations, splits, merges, resolutions, redemptions |
| Total size | ~49.1 GB compressed Parquet (including ~8.5 GB CTF event logs), plus `daily_aligned_multi/` |
| Time coverage | 2022-11-21 to 2026-04-28 |
| Data layers | `OrderFilled/`, `daily_aligned/`, `daily_aligned_multi/`, `CTF/` |
| Columns | 13 in `OrderFilled/`; 24 in `daily_aligned/`; 25 in `daily_aligned_multi/`; 5 to 8 in `CTF/` tables |
| Format | Parquet |
| License | CC-BY-4.0 |

## What this dataset is

This repository contains four layers representing the full on-chain activity and contract lifecycle of Polymarket v1:

- `OrderFilled/` is the raw on-chain trade tape. Each row is one matched maker-taker fill from Polymarket's v1 CTF Exchange. This layer is nominal and can include relayer/router records.
- `daily_aligned/` is the cleaned analysis layer for Standard Binary markets (`neg_risk=false`). It is partitioned by day, excludes the relayer/router records, joins frozen market metadata, and adds event-normalized fields (`p_event`, `D`) that map both sides of a binary market onto one event-probability axis.
- `daily_aligned_multi/` is the cleaned analysis layer for Neg Risk multi-outcome markets (`neg_risk=true`) — for example, "who wins the tournament" markets with many candidates. It goes through the identical cleani
```
</details>

## [TimeSeventeen/Polymarket-v2](https://huggingface.co/datasets/TimeSeventeen/Polymarket-v2)

533 个文件，共 53.19 GB，最后修改 2026-09-30T11:19。

```
.gitattributes  2,504
OrderFilled/2026_04_03.parquet  7,831
OrderFilled/2026_04_04.parquet  372
OrderFilled/2026_04_05.parquet  4,291
OrderFilled/2026_04_06.parquet  372
OrderFilled/2026_04_07.parquet  372
OrderFilled/2026_04_08.parquet  372
OrderFilled/2026_04_09.parquet  372
OrderFilled/2026_04_10.parquet  372
OrderFilled/2026_04_11.parquet  372
OrderFilled/2026_04_12.parquet  372
OrderFilled/2026_04_13.parquet  5,297
OrderFilled/2026_04_14.parquet  372
OrderFilled/2026_04_15.parquet  5,129
OrderFilled/2026_04_16.parquet  3,811
OrderFilled/2026_04_17.parquet  10,484
OrderFilled/2026_04_18.parquet  9,033
OrderFilled/2026_04_19.parquet  9,038
OrderFilled/2026_04_20.parquet  43,261
OrderFilled/2026_04_21.parquet  30,255
…  0
daily_aligned_multi/2026_09_07.parquet  12,339,315
daily_aligned_multi/2026_09_08.parquet  13,389,797
daily_aligned_multi/2026_09_09.parquet  13,600,427
daily_aligned_multi/2026_09_11.parquet  11,490,106
daily_aligned_multi/2026_09_12.parquet  11,070,553
daily_aligned_multi/2026_09_13.parquet  12,366,777
daily_aligned_multi/2026_09_14.parquet  12,273,539
daily_aligned_multi/2026_09_15.parquet  12,623,117
daily_aligned_multi/2026_09_16.parquet  12,436,775
daily_aligned_multi/2026_09_17.parquet  12,366,174
daily_aligned_multi/2026_09_18.parquet  13,694,417
daily_aligned_multi/2026_09_19.parquet  13,883,702
daily_aligned_multi/2026_09_20.parquet  17,828,324
daily_aligned_multi/2026_09_21.parquet  12,414,969
daily_aligned_multi/2026_09_22.parquet  10,922,148
daily_aligned_multi/2026_09_23.parquet  9,949,944
daily_aligned_multi/2026_09_24.parquet  10,423,205
daily_aligned_multi/2026_09_25.parquet  10,115,696
daily_aligned_multi/2026_09_28.parquet  6,905,547
daily_aligned_multi/2026_09_29.parquet  6,144,027
```

<details><summary>README.md</summary>

```
---
license: cc-by-4.0
language:
- en
size_categories:
- 1B<n<10B
task_categories:
- tabular-classification
tags:
- polymarket
- prediction-markets
- market-microstructure
- on-chain
- polygon
- orderfilled
- finance
---

# Polymarket-v2

A large-scale dataset of on-chain event logs from Polymarket v2, the prediction-market platform on the Polygon network. The repository contains three layers covering the full contract lifecycle from Polymarket v2 server start: OrderFilled/ (the raw on-chain trade tape), daily_aligned/ (the cleaned, metadata-enriched, and normalized analysis layer, Split by UTC). update daliy.
```
</details>

## [Joseph3222/polymarket-orderbook](https://huggingface.co/datasets/Joseph3222/polymarket-orderbook)

330 个文件，共 1370.05 GB，最后修改 2026-09-02T13:42。

```
.gitattributes  2,504
README.md  7,678
orderbook/date=2026-02-22/data_0.parquet  3,617,575,528
orderbook/date=2026-02-23/data_0.parquet  3,633,619,499
orderbook/date=2026-02-24/data_0.parquet  3,112,141,783
orderbook/date=2026-02-25/data_0.parquet  2,689,856,366
orderbook/date=2026-02-26/data_0.parquet  3,151,875,145
orderbook/date=2026-02-27/data_0.parquet  3,267,934,299
orderbook/date=2026-02-28/data_0.parquet  3,470,725,593
orderbook/date=2026-03-01/data_0.parquet  4,361,556,009
orderbook/date=2026-03-02/data_0.parquet  3,492,455,289
orderbook/date=2026-03-03/data_0.parquet  2,943,498,303
orderbook/date=2026-03-04/data_0.parquet  3,378,096,645
orderbook/date=2026-03-05/data_0.parquet  2,481,797,257
orderbook/date=2026-03-06/data_0.parquet  3,948,710,098
orderbook/date=2026-03-07/data_0.parquet  3,887,140,418
orderbook/date=2026-03-08/data_0.parquet  3,913,077,562
orderbook/date=2026-03-09/data_0.parquet  2,235,401,869
orderbook/date=2026-03-10/data_0.parquet  3,367,471,990
orderbook/date=2026-03-11/data_0.parquet  3,090,366,886
…  0
orderbook_1min/date=2026-07-22/data_0.parquet  1,733,106,837
orderbook_1min/date=2026-07-23/data_0.parquet  1,886,718,938
orderbook_1min/date=2026-07-24/data_0.parquet  1,793,445,337
orderbook_1min/date=2026-07-25/data_0.parquet  1,774,722,763
orderbook_1min/date=2026-07-26/data_0.parquet  1,677,922,632
orderbook_1min/date=2026-07-27/data_0.parquet  1,695,080,857
orderbook_1min/date=2026-07-28/data_0.parquet  1,773,436,054
orderbook_1min/date=2026-07-29/data_0.parquet  2,193,363,018
orderbook_1min/date=2026-07-30/data_0.parquet  1,983,685,908
orderbook_1min/date=2026-07-31/data_0.parquet  1,946,776,507
orderbook_1min/date=2026-08-01/data_0.parquet  2,168,696,498
orderbook_1min/date=2026-08-02/data_0.parquet  2,161,556,418
orderbook_1min/date=2026-08-03/data_0.parquet  2,007,097,554
orderbook_1min/date=2026-08-04/data_0.parquet  2,303,000,407
orderbook_1min/date=2026-08-05/data_0.parquet  2,533,918,057
orderbook_1min/date=2026-08-06/data_0.parquet  2,522,942,010
orderbook_1min/date=2026-08-07/data_0.parquet  2,447,449,542
orderbook_1min/date=2026-08-08/data_0.parquet  2,456,216,905
orderbook_1min/date=2026-08-09/data_0.parquet  2,365,876,687
orderbook_1min/date=2026-08-10/data_0.parquet  115,227,673
```

<details><summary>README.md</summary>

```
---
license: cc-by-4.0
pretty_name: Polymarket Orderbook Archive
language:
  - en
tags:
  - finance
  - prediction-markets
  - polymarket
  - orderbook
  - limit-order-book
  - market-microstructure
  - backtesting
size_categories:
  - 100B<n<1T
configs:
  - config_name: orderbook_1min
    data_files: "orderbook_1min/**/*.parquet"
  - config_name: orderbook
    data_files: "orderbook/**/*.parquet"
---

# Polymarket Orderbook Archive

Tick-by-tick Polymarket CLOB (central limit order book) event stream for every
market, **2026-02-22 → 2026-08-10**, plus a query-ready **1-minute full-depth L2
snapshot** rollup derived from it. Parquet, partitioned by UTC day, one file per
day.

| Config | What | Days | Size | Typical file |
|---|---|---|---|---|
| `orderbook` | raw WebSocket event stream (`book`, `price_change`, `last_trade_price`, `tick_size_change`) | 164 | ~1.18 TB | 8 GB (max 14 GB) |
| `orderbook_1min` | full L2 book at the end of every minute an asset had activity | 164 | ~192 GB | 1.2 GB (max 2.5 GB) |

Layout: `<config>/date=YYYY-MM-DD/data_0.parquet`.

## Coverage and known gaps

- **Days:** 2026-02-22 through 2026-08-10. **2026-06-12 → 2026-06-17 are absent**
  (ingest box outage). **2026-08-10 is partial** (only the first hours; the source
  archive stopped publishing that morning and has not resumed).
- **Two source eras.** 2026-02-22 → 2026-04-12 comes from pmxt's *v1* archive,
  which only covered roughly half of all markets (incomplete WebSocket
  subscriptions). 2026-04-13 onward comes from the *v2* archive with full market
  coverage. The `source_archive` column says which. In the Apr 13-16 overlap the
  v2 feed was preferred.
- **Missing hours inside a day.** Roughly 5-15% of the source's hourly files were
  unreadable (404 or corrupt footer) and were skipped, so a day can have an hour
  or two of silence. `orderbook_1min` carries the last known book across such
  gaps only within a day; do not assume every minute is present.
- **Sub-second ordering.** Source timestamps are 1-second resolution. `event_ts_ms`
  is millisecond-precise, but events inside the same millisecond keep file order,
  which is not guaranteed to be the true exchange order. This produced ~10-15%
  crossed books at end-of-day in naive replays; treat crossed books as noise.

## Source and attribution

The raw stream mirrors the free hourly archive published by
[pmxt](https://archive.pmxt.dev/) (v1 at `r2.pmxt.dev`, v2 at `r2v2.pmxt.dev`),
which is licensed **CC BY 4.0**. This dataset normalizes both eras into one
schema and adds the 1-minute rollup; it is released under the same license.
**Please credit pmxt** when you use it.

## Schema: `orderbook`

One row per CLOB WebSocket event. Reconstruct the book at any moment T by taking
the most recent `book` event for the asset before T and applying every
`price_change` between it and T.

| column | type | meaning |
|---|---|---|
| `event_ts` | UBIGINT | Unix epoch seconds UTC (floor of source timestamp). |
| `event_ts_ms` | UBIGINT | Unix epoch milliseconds UTC. Use this for ordering. |
| `condition_id` | VARCHAR | Market id, `0x` + 64 lowercase hex chars. |
| `asset_id` | VARCHAR | Outcome token id (77-digit ERC-1155 id). One market has two. |
| `outcome` | VARCHAR | `YES` / `NO` for v1 rows; NULL for v2 rows (resolve via Gamma `/markets?clob_token_ids=`). |
| `event_type` | VARCHAR | `book` / `price_change` / `last_trade_price` / `tick_size_change`. |
| `bids_json` | VARCHAR | `[[price, size], ...]` as strings. Only on `book` events. Not sorted; sort at read time. |
| `asks_json` | VARCHAR | Same shape. Only on `book` events. |
| `price` | DOUBLE | Price in [0, 1]. On `price_change` and `last_trade_price`. |
| `size` | DOUBLE | On `price_change`: the level's **new absolute total** (0 = level removed), not a delta. On `last_trade_price`: trade size. |
| `side` | VARCHAR | `BUY` (bid side) / `SELL` (ask side). |
| `best_bid` | DOUBLE | Source-emitted top of book at event time (may be NULL). |
| `best_ask` | DOUBLE | Source-emitted lowest ask at event time (may be NULL). |
| `transaction_hash` | VARCHAR | Only on `last_trade_price`; `0x` + 64 hex. |
| `old_tick_size` | DOUBLE | Only on `tick_size_change`. |
| `new_tick_size` | DOUBLE | Only on `tick_size_change`. |
| `source_archive` | VARCHAR | `v1` or `v2`. |
| `date` | DATE | Partition column, UTC date of `event_ts`. |

Reconstruction semantics match Polymarket's reference client
([nevuamarkets/poly-websockets `OrderBookCache.ts`](https://github.com/nevuamarkets/poly-websockets/blob/main/src/modules/OrderBookCache.ts)):
a `book` event replaces the asset's whole book; `price_change` with `size > 0`
sets that level's total; `size == 0` removes it; `last_trade_price` does **not**
modify the book (a follow-up `price_change` does).

Event frequency differs by era: v1 emitted a `book` snapshot about every 10
minutes per token; v2 emits one only on subscription start or resync (a few per
asset per day), so a v2 reconstruction may replay hundreds of `price_change`
rows after the last `book`.

## Schema: `orderbook_1min`

One row per `(asset_id, minute)` for every minute the asset had at least one
event, holding the **full-depth book at the end of that minute**. Minutes with
no events have no row; carry the previous snapshot forward.

| column | type | meaning |
|---|---|---|
| `asset_id` | VARCHAR | Outcome token id. |
| `condition_id` | VARCHAR | Market id. |
| `minute_ts` | BIGINT | Unix epoch seconds UTC, floored to the minute. |
| `bids_json` | VARCHAR | `[[price, size], ...]`, sorted **descending** by price, native floats. |
| `asks_json` | VARCHAR | `[[price, size], ...]`, sorted **ascending** by price. |
| `n_bid_levels` | UINTEGER | Bid price levels. |
| `n_ask_levels` | UINTEGER | Ask price levels. |
| `best_bid` | DOUBLE | NULL if no bids. |
| `best_ask` | DOUBLE | NULL if no asks. |
| `mid` | DOUBLE | `(best_bid + best_ask) / 2`; NULL if either side empty. |
| `spread` | DOUBLE | `best_ask - best_bid`;
```
</details>

## [polyorderbooks/polymarket-crypto-updown-orderbooks-l2](https://huggingface.co/datasets/polyorderbooks/polymarket-crypto-updown-orderbooks-l2)

5 个文件，共 0.05 GB，最后修改 2026-08-25T17:12。

```
.gitattributes  2,504
README.md  6,296
updown_15m.parquet  16,533,699
updown_4h.parquet  14,648,764
updown_5m.parquet  14,249,234
```

<details><summary>README.md</summary>

```
---
license: cc-by-4.0
pretty_name: Polymarket Crypto Up/Down Order Books — 1-Second L2, Three Contract Lengths
task_categories:
  - time-series-forecasting
  - tabular-regression
language:
  - en
tags:
  - polymarket
  - prediction-markets
  - order-book
  - order-books
  - limit-order-book
  - market-microstructure
  - high-frequency
  - crypto
  - bitcoin
  - ethereum
  - solana
  - xrp
  - dogecoin
  - backtesting
  - slippage
size_categories:
  - 100K<n<1M
configs:
  - config_name: default
    data_files:
      - split: train
        path: "*.parquet"
---

# Polymarket Crypto Up/Down Order Books — 1-Second L2 Depth

**897,192 order book snapshots across 805 resolved markets and three contract
lengths.** Full bid and ask ladders, captured once per second, with the winning
outcome on every row.

This is the DOI-carrying research release. Same data as
[10.5281/zenodo.22084114](https://doi.org/10.5281/zenodo.22084114), mirrored here
for `load_dataset`.

## What is in it

| File | Snapshots | Markets | Contract |
| --- | --- | --- | --- |
| `updown_5m.parquet` | 167,941 | 471 | Five-minute |
| `updown_15m.parquet` | 239,310 | 265 | Fifteen-minute |
| `updown_4h.parquet` | 489,941 | 69 | Four-hour |

Eight coins. Every market resolved, so each row carries its outcome. One
continuous collector regime, so the three files are comparable with each other.

```python
from datasets import load_dataset
ds = load_dataset("polyorderbooks/polymarket-crypto-updown-orderbooks-l2")
```

## Why three contract lengths

Because conclusions do not transfer between them, and that is easy to miss with
one.

| | 5-minute | 15-minute | 4-hour |
| --- | --- | --- | --- |
| One-sided snapshots | 16.9% | 9.3% | **0.5%** |
| Crossed snapshots | 3.24% | 3.08% | 1.50% |
| One-sided in the final 60s | 76.2% | 85.4% | 46.5% |

A four-hour contract holds a two-sided market for almost all of its life. A
five-minute contract does not. Anything measured on one length should be checked
against another before it is described as a property of prediction markets.

## Two things to expect

**Books go one-sided as markets resolve.** Nobody offers the losing outcome and
nobody bids the winning one. This is real market behaviour, not missing data, and
it will break code that indexes the first level of a ladder without guarding.

**Some snapshots are crossed** — best bid at or above best ask, which cannot
persist in a matching engine. They are flagged rather than removed so the rate
stays measurable:

```python
import pandas as pd
df = pd.read_parquet("updown_5m.parquet")

(df.best_bid >= df.best_ask).mean()              # crossed rate
(df.bid_prices.str.len() == 0).mean()            # empty bid side
df[df.seconds_to_close <= 60].crossed.mean()     # and near settlement
```

Publishing the flag rather than filtering the affected rows is deliberate: you
can measure our data quality instead of taking our word for it.

## Schema

One row per outcome token per second. Ladders are typed list columns, so no
parsing step.

| Column | Type | Notes |
| --- | --- | --- |
| `market_slug` | string | e.g. `btc-updown-5m-1787551200` |
| `outcome` | string | `Up` or `Down` |
| `captured_at` | timestamp | UTC, 1-second resolution |
| `seconds_to_close` | float | Time remaining until settlement |
| `bid_prices`, `bid_sizes` | list[float] | Full bid ladder, best first |
| `ask_prices`, `ask_sizes` | list[float] | Full ask ladder, best first |
| `best_bid`, `best_ask` | float | Derived top of book |
| `crossed` | bool | `best_bid >= best_ask` |
| `winning_outcome` | string | Resolved result |

Prices are probabilities in [0, 1]. Sizes are share counts.

## How it was captured

Read from Polymarket's live order book once per second, with a full-book
reconcile every 60 seconds. The reconcile interval is the design decision that
matters: replaying an archived event stream instead diverges from the source at
roughly two-thirds of checkpoints, because the feed carries no sequence numbers
and a dropped removal leaves no gap to detect.

Polymarket archives no order book history itself — `/book` returns the current
state and nothing stores it — so this data exists only because it was recorded
while the markets traded.

## Citation

> PolyOrderbooks. (2026). *Polymarket Crypto Up/Down Order Books: 1-Second L2
> Depth Across Three Contract Lengths* [Dataset]. Zenodo.
> https://doi.org/10.5281/zenodo.22084114

That is the concept DOI and always resolves to the newest version. Each version
also has its own DOI for pinning exact data.

---

## About PolyOrderbooks

We capture Polymarket order book depth at 1-second resolution and serve it over a
REST API. Polymarket archives no order book history of its own — its `/book`
endpoint returns the current state and nothing stores it — so this data exists
only because it was recorded while the markets traded.

| | |
| --- | --- |
| API | [polyorderbooks.com](https://polyorderbooks.com) |
| Documentation | [docs.polyorderbooks.com](https://docs.polyorderbooks.com) — endpoints, parameters, response shapes |
| Free tier | [polyorderbooks.com/signup](https://polyorderbooks.com/signup) — 1-second resolution, no card |
| All open datasets | [polyorderbooks.com/datasets](https://polyorderbooks.com/datasets) |
| Python client | [`pip install polyorderbooks`](https://pypi.org/project/polyorderbooks/) |
| MCP server | [`@polyorderbooks/mcp-server`](https://www.npmjs.com/package/@polyorderbooks/mcp-server) — for Claude, Cursor and other MCP clients |

### The citable release

For a paper, cite the DOI-carrying release rather than this repository — 897,192
snapshots across 805 resolved markets and three contract lengths, same schema,
same licence:

> PolyOrderbooks. (2026). *Polymarket Crypto Up/Down Order Books: 1-Second L2
> Depth Across Three Contract Lengths* [Dataset]. Zenodo.
> https://doi.org/10.5281/zenodo.22084114

### Questions

Open a discussion on this dataset for anything about the data itself.

For the API — a window
```
</details>

## [Lazy108/binance-polymarket-orderflow](https://huggingface.co/datasets/Lazy108/binance-polymarket-orderflow)

134 个文件，共 5.01 GB，最后修改 2026-08-29T17:42。

```
.gitattributes  2,504
README.md  1,056
binance_depth/BTC/2026-08-07.parquet  5,040,890
binance_depth/BTC/2026-08-08.parquet  11,913,680
binance_depth/BTC/2026-08-09.parquet  13,534,388
binance_depth/BTC/2026-08-10.parquet  16,704,007
binance_depth/BTC/2026-08-11.parquet  16,016,823
binance_depth/BTC/2026-08-12.parquet  16,337,619
binance_depth/BTC/2026-08-13.parquet  11,716,995
binance_depth/BTC/2026-08-15.parquet  4,178,482
binance_depth/BTC/2026-08-16.parquet  12,907,535
binance_depth/BTC/2026-08-17.parquet  11,740,612
binance_depth/BTC/2026-08-18.parquet  3,703,933
binance_depth/BTC/2026-08-19.parquet  10,244,530
binance_depth/BTC/2026-08-20.parquet  116,303,799
binance_depth/BTC/2026-08-21.parquet  116,567,339
binance_depth/BTC/2026-08-22.parquet  120,668,966
binance_depth/BTC/2026-08-23.parquet  82,825,349
binance_depth/BTC/2026-08-24.parquet  79,664,099
binance_depth/BTC/2026-08-25.parquet  69,059,862
…  0
binance_trades/SOL/2026-08-09.parquet  1,833,391
binance_trades/SOL/2026-08-10.parquet  2,630,842
binance_trades/SOL/2026-08-11.parquet  2,556,386
binance_trades/SOL/2026-08-12.parquet  2,515,474
binance_trades/SOL/2026-08-13.parquet  1,584,835
binance_trades/SOL/2026-08-15.parquet  417,897
binance_trades/SOL/2026-08-16.parquet  1,407,985
binance_trades/SOL/2026-08-17.parquet  1,966,199
binance_trades/SOL/2026-08-18.parquet  625,304
binance_trades/SOL/2026-08-19.parquet  4,441,002
binance_trades/SOL/2026-08-20.parquet  12,275,405
binance_trades/SOL/2026-08-21.parquet  19,119,926
binance_trades/SOL/2026-08-22.parquet  25,046,832
binance_trades/SOL/2026-08-23.parquet  11,712,134
binance_trades/SOL/2026-08-24.parquet  12,869,148
binance_trades/SOL/2026-08-25.parquet  11,902,407
binance_trades/SOL/2026-08-26.parquet  14,115,909
binance_trades/SOL/2026-08-27.parquet  20,818,523
binance_trades/SOL/2026-08-28.parquet  15,568,671
binance_trades/SOL/2026-08-29.parquet  5,416,038
```

<details><summary>README.md</summary>

```
---
language:
- en
license: cc-by-4.0
size_categories:
- 1GB<n<10GB
tags:
- finance
- orderbook
- binance
- high-frequency
---

# Binance Spot Orderbook & Trade-Flow Dataset (1s cadence)

Gated research dataset. Request access with a short description of your use case.

## Contents

| Directory | Source | Cadence | Levels | Symbols | Coverage |
|---|---|---|---|---|---|
| `binance_depth/` | Binance spot L2 depth (WS snapshot) | 1 s | 20 | BTC/ETH/SOL | 2026-08-07 onward |
| `binance_trades/` | Binance spot aggTrade tape | tick | - | BTC/ETH/SOL | 2026-08-07 onward |

## File layout

`{stream}/{SYMBOL}/{YYYY-MM-DD}.parquet` — one file per stream, symbol, UTC day.

## Notes

- Files contain all three symbols interleaved (filter by `symbol` column); the SYMBOL in the path is the day's rotation anchor.
- `event_time_ms` may be null on depth rows; `timestamp` (ISO, UTC) is the local receive time and always populated.
- All timestamps UTC.

## Citation

Please cite as: Lazy108, *Binance Spot Orderbook & Trade-Flow Dataset*, Hugging Face, 2026.

```
</details>

