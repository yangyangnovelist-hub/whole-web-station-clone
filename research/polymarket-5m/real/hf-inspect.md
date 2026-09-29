# 数据集细查

## aliplayer1/polymarket-crypto-updown  `data/orderbook/crypto=BTC/timeframe=5-minute/*`

<details><summary>README</summary>

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

'''python
from datasets import load_dataset

markets = load_dataset("aliplayer1/polymarket-crypto-updown", "markets")
prices = load_dataset("aliplayer1/polymarket-crypto-updown", "prices")
ticks = load_dataset("aliplayer1/polymarket-crypto-updown", "ticks")
spot = load_dataset("aliplayer1/polymarket-crypto-updown", "spot_prices")
orderbook = load_dataset("aliplayer1/polymarket-crypto-updown", "orderbook")
'''

Or query directly with DuckDB:

'''python
import duckdb

duckdb.sql("""
    SELECT * FROM 'hf://datasets/aliplayer1/polymarket-crypto-updown/data/prices/**/*.parquet'
    WHERE crypto = 'BTC' AND timeframe = '1-hour'
    LIMIT 100
""").show()
'''

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

'''
data/
  markets.parquet
  prices/crypto=BTC/timeframe=1-hour/part-0.parquet
  ticks/crypto=ETH/timeframe=5-minute/part-0.parquet
  spot_prices/part-0.parquet
  orderbook/crypto=SOL/timeframe=15-minute/part-0.parquet
'''

## Pipeline

This dataset is produced by [polymarket-data-pipeline](https://github.com/aliplayer1/polymarket-data-pipeline), which runs three services:

- **Historical scan** (every 6h): pages through Polymarket's Gamma API for closed markets, fetches CLOB price history, and backfills on-chain tick data from Polygon.
- **Live WebSocket** (24/7): captures real-time trades, orderbook BBO, and spot prices.
- **Upload** (every 3h): consolidates shard files and pushes to this dataset.

## License

MIT

```

</details>

匹配 68 个文件，共 9GB：
- `data/orderbook/crypto=BTC/timeframe=5-minute/part-0.parquet` 9GB
- `data/orderbook/crypto=BTC/timeframe=5-minute/part-ws-1777225328480-000.parquet` 4MB
- `data/orderbook/crypto=BTC/timeframe=5-minute/part-ws-1777225328480-001.parquet` 4MB
- `data/orderbook/crypto=BTC/timeframe=5-minute/part-ws-1777225328480-002.parquet` 5MB
- `data/orderbook/crypto=BTC/timeframe=5-minute/part-ws-1777225328480-003.parquet` 5MB
- `data/orderbook/crypto=BTC/timeframe=5-minute/part-ws-1777225328480-004.parquet` 5MB
- `data/orderbook/crypto=BTC/timeframe=5-minute/part-ws-1777225328480-005.parquet` 5MB
- `data/orderbook/crypto=BTC/timeframe=5-minute/part-ws-1777225328480-006.parquet` 5MB
- `data/orderbook/crypto=BTC/timeframe=5-minute/part-ws-1777225328480-007.parquet` 5MB
- `data/orderbook/crypto=BTC/timeframe=5-minute/part-ws-1777225328480-008.parquet` 6MB
- `data/orderbook/crypto=BTC/timeframe=5-minute/part-ws-1777225328480-009.parquet` 5MB
- `data/orderbook/crypto=BTC/timeframe=5-minute/part-ws-1777225328480-010.parquet` 5MB
- `data/orderbook/crypto=BTC/timeframe=5-minute/part-ws-1777225328480-011.parquet` 5MB
- `data/orderbook/crypto=BTC/timeframe=5-minute/part-ws-1777225328480-012.parquet` 5MB
- `data/orderbook/crypto=BTC/timeframe=5-minute/part-ws-1777225328480-013.parquet` 5MB
- `data/orderbook/crypto=BTC/timeframe=5-minute/part-ws-1777225328480-014.parquet` 5MB
- `data/orderbook/crypto=BTC/timeframe=5-minute/part-ws-1777225328480-015.parquet` 5MB
- `data/orderbook/crypto=BTC/timeframe=5-minute/part-ws-1777225328480-016.parquet` 4MB
- `data/orderbook/crypto=BTC/timeframe=5-minute/part-ws-1777225328480-017.parquet` 5MB
- `data/orderbook/crypto=BTC/timeframe=5-minute/part-ws-1777225328480-018.parquet` 5MB
- `data/orderbook/crypto=BTC/timeframe=5-minute/part-ws-1777225328480-019.parquet` 5MB
- `data/orderbook/crypto=BTC/timeframe=5-minute/part-ws-1777225328480-020.parquet` 5MB
- `data/orderbook/crypto=BTC/timeframe=5-minute/part-ws-1777225328480-021.parquet` 5MB
- `data/orderbook/crypto=BTC/timeframe=5-minute/part-ws-1777225328480-022.parquet` 4MB
- `data/orderbook/crypto=BTC/timeframe=5-minute/part-ws-1777225328480-023.parquet` 4MB
- `data/orderbook/crypto=BTC/timeframe=5-minute/part-ws-1777225328480-024.parquet` 4MB
- `data/orderbook/crypto=BTC/timeframe=5-minute/part-ws-1777225328480-025.parquet` 4MB
- `data/orderbook/crypto=BTC/timeframe=5-minute/part-ws-1777225328480-026.parquet` 4MB
- `data/orderbook/crypto=BTC/timeframe=5-minute/part-ws-1777225328480-027.parquet` 4MB
- `data/orderbook/crypto=BTC/timeframe=5-minute/part-ws-1777225328480-028.parquet` 4MB
- `data/orderbook/crypto=BTC/timeframe=5-minute/part-ws-1777225328480-029.parquet` 4MB
- `data/orderbook/crypto=BTC/timeframe=5-minute/part-ws-1777225328480-030.parquet` 4MB
- `data/orderbook/crypto=BTC/timeframe=5-minute/part-ws-1777225328480-031.parquet` 4MB
- `data/orderbook/crypto=BTC/timeframe=5-minute/part-ws-1777225328480-032.parquet` 4MB
- `data/orderbook/crypto=BTC/timeframe=5-minute/part-ws-1777225328480-033.parquet` 4MB
- `data/orderbook/crypto=BTC/timeframe=5-minute/part-ws-1777225328480-034.parquet` 4MB
- `data/orderbook/crypto=BTC/timeframe=5-minute/part-ws-1777225328480-035.parquet` 3MB
- `data/orderbook/crypto=BTC/timeframe=5-minute/part-ws-1777225328480-036.parquet` 4MB
- `data/orderbook/crypto=BTC/timeframe=5-minute/part-ws-1777225328480-037.parquet` 4MB
- `data/orderbook/crypto=BTC/timeframe=5-minute/part-ws-1777225328480-038.parquet` 4MB

### `data/orderbook/crypto=BTC/timeframe=5-minute/part-ws-1777225328480-008.parquet`

行数 944,577，row groups 8

```
ts_ms: int64
market_id: string
token_id: string
outcome: string
best_bid: double
best_ask: double
best_bid_size: double
best_ask_size: double
local_recv_ts_ns: int64
```

```
           ts_ms market_id                                                                        token_id outcome  best_bid  best_ask  best_bid_size  best_ask_size     local_recv_ts_ns
0  1777140841640   2070311   30610633059465877178210899870781155838500951203823255292362987343232271485591    Down      0.49      0.50       10826.07       10840.12  1777140841640077815
1  1777140841640   2072691  111416031264392453274244091175767779683618172332085793513684306996935435028231      Up      0.50      0.51        7668.75        7668.11  1777140841640466381
2  1777140841641   2070240   37749015915338028998227505889029567023258647967160995250659917394164636363233      Up      0.17      0.18        5958.84        5751.24  1777140841641943204
3  1777140841641   2070311   53356882044726707360179076033106429029589239429657386941935334408925527118057      Up      0.50      0.51       10840.12       10826.07  1777140841641515998
4  1777140841642   2070240   37749015915338028998227505889029567023258647967160995250659917394164636363233      Up      0.17      0.18        5958.84        5751.24  1777140841642937899
```
- `ts_ms`: 1777140841640 → 1777142231953
- `local_recv_ts_ns`: 1777140841640077815 → 1777142231953134008

### `data/orderbook/crypto=BTC/timeframe=5-minute/part-ws-1777225328480-009.parquet`

行数 926,160，row groups 8

```
ts_ms: int64
market_id: string
token_id: string
outcome: string
best_bid: double
best_ask: double
best_bid_size: double
best_ask_size: double
local_recv_ts_ns: int64
```

```
           ts_ms market_id                                                                       token_id outcome  best_bid  best_ask  best_bid_size  best_ask_size     local_recv_ts_ns
0  1777142232027   2070459  90300281616800853496941993183701443931280147355960170850462220634107193372414    Down      0.34      0.35        8235.32       10842.25  1777142232027898200
1  1777142232028   2070459  90300281616800853496941993183701443931280147355960170850462220634107193372414    Down      0.34      0.35        8235.32       10842.25  1777142232028246605
2  1777142232028   2070459  54383078742171980032486179951597389067630007150934264133905226538864218961397      Up      0.65      0.66       10842.25        8235.32  1777142232028973736
3  1777142232029   2070459  54383078742171980032486179951597389067630007150934264133905226538864218961397      Up      0.65      0.66       10842.25        8235.32  1777142232029212580
4  1777142232031   2070459  54383078742171980032486179951597389067630007150934264133905226538864218961397      Up      0.65      0.66       10842.25        8235.32  1777142232031976983
```
- `ts_ms`: 1777142232027 → 1777143864968
- `local_recv_ts_ns`: 1777142232027898200 → 1777143864968740484

## aliplayer1/polymarket-crypto-updown  `data/markets.parquet`

<details><summary>README</summary>

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

'''python
from datasets import load_dataset

markets = load_dataset("aliplayer1/polymarket-crypto-updown", "markets")
prices = load_dataset("aliplayer1/polymarket-crypto-updown", "prices")
ticks = load_dataset("aliplayer1/polymarket-crypto-updown", "ticks")
spot = load_dataset("aliplayer1/polymarket-crypto-updown", "spot_prices")
orderbook = load_dataset("aliplayer1/polymarket-crypto-updown", "orderbook")
'''

Or query directly with DuckDB:

'''python
import duckdb

duckdb.sql("""
    SELECT * FROM 'hf://datasets/aliplayer1/polymarket-crypto-updown/data/prices/**/*.parquet'
    WHERE crypto = 'BTC' AND timeframe = '1-hour'
    LIMIT 100
""").show()
'''

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

'''
data/
  markets.parquet
  prices/crypto=BTC/timeframe=1-hour/part-0.parquet
  ticks/crypto=ETH/timeframe=5-minute/part-0.parquet
  spot_prices/part-0.parquet
  orderbook/crypto=SOL/timeframe=15-minute/part-0.parquet
'''

## Pipeline

This dataset is produced by [polymarket-data-pipeline](https://github.com/aliplayer1/polymarket-data-pipeline), which runs three services:

- **Historical scan** (every 6h): pages through Polymarket's Gamma API for closed markets, fetches CLOB price history, and backfills on-chain tick data from Polygon.
- **Live WebSocket** (24/7): captures real-time trades, orderbook BBO, and spot prices.
- **Upload** (every 3h): consolidates shard files and pushes to this dataset.

## License

MIT

```

</details>

匹配 1 个文件，共 13MB：
- `data/markets.parquet` 13MB

### `data/markets.parquet`

行数 104,668，row groups 1

```
market_id: string
question: string
crypto: dictionary<values=string, indices=int8, ordered=0>
timeframe: dictionary<values=string, indices=int8, ordered=0>
volume: float
resolution: int8
start_ts: int64
end_ts: int64
closed_ts: int64
condition_id: string
up_token_id: string
down_token_id: string
slug: string
fee_rate_bps: int16
-- schema metadata --
pandas: '{"index_columns": [], "column_indexes": [], "columns": [{"name":' + 1791
schema_version: '4'
```

```
  market_id                                          question crypto timeframe         volume  resolution    start_ts      end_ts  closed_ts                                                        condition_id                                                                     up_token_id                                                                  down_token_id slug  fee_rate_bps
0   1573928  Ethereum Up or Down - March 14, 1:20AM-1:25AM ET    ETH  5-minute   12972.414062          -1  1773379690  1773465900          0  0xccbf2b6ec2462602705b8906dd511d8a5fa9fcd7b8f9c9d7f7557df79721775c   59198213117443604249479334064430557785580986641965734019675987678938213157385  18771421854516769603977654374516808123769619428439778779970431677944944932689                 -1
1   1573927   Bitcoin Up or Down - March 14, 1:20AM-1:25AM ET    BTC  5-minute  126833.515625          -1  1773379688  1773465900          0  0xbdc119fb3f5f48dfb5b3e95eed128fed434f4304282718c0715d6bb5708e4d23   83790357452450695800540189972924364052031693233398289262253580578245793333168  92472023763775608320193369904885598977138319762455744829639181208774943086349                 -1
2   1573925    Solana Up or Down - March 14, 1:20AM-1:25AM ET    SOL  5-minute    4399.829590          -1  1773379686  1773465900          0  0x27e638a4b584009add6b59adc687afb583734ae4a475cd6175d6fa5622c97d9c  115339471089935594417185842548927854270517701247563074242343157931681061017465  64629010210091730573069223481053639789458892795898310529303653922991694559937                 -1
3   1573915  Ethereum Up or Down - March 14, 1:15AM-1:20AM ET    ETH  5-minute    8961.315430          -1  1773379389  1773465600          0  0xa3a29f22e1147bfe09731c05d4b7fbd1de3703df42163d9f4a14916140808595   11709716851419578358579039444953628497112976559018747947585805533308332134425  21705622051291744702990090078963814303460894179678405802504854931947574333465                 -1
4   1573918   Bitcoin Up or Down - March 14, 1:15AM-1:20AM ET    BTC  5-minute   98861.179688          -1  1773379391  1773465600          0  0x254a422ac145c0a18660f2898bc749b3e5e6748c954dd4eb3f4eaa308d374d0c   54136719050565548772909806599999069445021786747199077306981423425785800420669  62024665658881996491685016743532279121272763445829119126650181347676853074098                 -1
```
- `timeframe`: 1-hour → 5-minute
- `start_ts`: 1766617269 → 1776644928
- `end_ts`: 1766793600 → 1776731100
- `closed_ts`: 0 → 1776731120
- `slug`:  → xrp-updown-5m-1776730800

## aliplayer1/polymarket-crypto-updown  `data/heartbeats/*`

<details><summary>README</summary>

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

'''python
from datasets import load_dataset

markets = load_dataset("aliplayer1/polymarket-crypto-updown", "markets")
prices = load_dataset("aliplayer1/polymarket-crypto-updown", "prices")
ticks = load_dataset("aliplayer1/polymarket-crypto-updown", "ticks")
spot = load_dataset("aliplayer1/polymarket-crypto-updown", "spot_prices")
orderbook = load_dataset("aliplayer1/polymarket-crypto-updown", "orderbook")
'''

Or query directly with DuckDB:

'''python
import duckdb

duckdb.sql("""
    SELECT * FROM 'hf://datasets/aliplayer1/polymarket-crypto-updown/data/prices/**/*.parquet'
    WHERE crypto = 'BTC' AND timeframe = '1-hour'
    LIMIT 100
""").show()
'''

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

'''
data/
  markets.parquet
  prices/crypto=BTC/timeframe=1-hour/part-0.parquet
  ticks/crypto=ETH/timeframe=5-minute/part-0.parquet
  spot_prices/part-0.parquet
  orderbook/crypto=SOL/timeframe=15-minute/part-0.parquet
'''

## Pipeline

This dataset is produced by [polymarket-data-pipeline](https://github.com/aliplayer1/polymarket-data-pipeline), which runs three services:

- **Historical scan** (every 6h): pages through Polymarket's Gamma API for closed markets, fetches CLOB price history, and backfills on-chain tick data from Polygon.
- **Live WebSocket** (24/7): captures real-time trades, orderbook BBO, and spot prices.
- **Upload** (every 3h): consolidates shard files and pushes to this dataset.

## License

MIT

```

</details>

匹配 1 个文件，共 2MB：
- `data/heartbeats/part-0.parquet` 2MB

### `data/heartbeats/part-0.parquet`

行数 680,532，row groups 1

```
ts_ms: int64
source: string
shard_key: string
event_type: string
last_event_age_ms: int64
-- schema metadata --
pandas: '{"index_columns": [], "column_indexes": [], "columns": [{"name":' + 672
schema_version: '5'
```

```
           ts_ms   source      shard_key    event_type  last_event_age_ms
0  1776728095826  clob_ws   clob_shard_7  price_change                 21
1  1776728095826  clob_ws  clob_shard_10  price_change                 16
2  1776728095826  clob_ws   clob_shard_4  price_change                 35
3  1776728095826  clob_ws   clob_shard_8  price_change                 22
4  1776728095826  clob_ws   clob_shard_5  price_change                 17
```
- `ts_ms`: 1776728095826 → 1777226631997

## DineshKumar8399/polymarket-orderbook-dataset  `*`

<details><summary>README</summary>

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

'''bash
pip install huggingface_hub
hf download DineshKumar8399/polymarket-orderbook-dataset --repo-type dataset --local-dir polymarket-data
'''

'''python
import duckdb
duckdb.sql("SELECT * FROM 'polymarket-data/quotes/**/*.parquet' LIMIT 5").show()
'''

**GitHub Releases** (a single dated tarball, ~223 MB):

'''bash
gh release download data-2026-09-13 --repo DineshKumar8399/polymarket-orderbook-dataset
tar --zstd -xf polymarket-orderbook-*.tar.zst
'''

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

'''python
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
'''

With pandas or polars:

'''python
import pandas as pd, polars as pl

markets = pd.read_parquet("markets.parquet")
one_day = pl.read_parquet("quotes/dt=2026-08-01/*.parquet")
'''

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
|---|---|---|
| `ts` | timestamp | |
| `slug` | string | |
| `bid` / `ask` / `mid` | double | |
| `last_traded` | double | **Last traded price — the only print data here** |

---

## How it was collected

Two independent collectors ran continuously on a dedicated machine:

**Broad sweep** — polled the full market list roughly every two minutes and
recorded the top of book for every market it could see. This produced `quotes/`.
It is wide (every market) but shallow: it records what was *quoted*, never what
*traded*.

**Watchlist** — polled about twelve actively-traded markets every twenty
seconds, rotating the selection every thirty minutes, and recorded the last
traded price alongside the book. This produced `watch_quotes.parquet`. It is
narrow but deep, and it is the only place in this dataset where you can ask
whether a trade actually happened.

That split matters more than it sounds. Displayed quotes are not the same thing
as executable prices, and nothing in `quotes/` can tell you whether a given
quote could have been filled.

---

## Known issues

Also shipped as `data_quality.parquet` so you can assert on them in a pipeline.

### 1. A four-day collector outage

No rows exist between **2026-07-17 17:26:09** and **2026-07-22 10:32:48**. The
machine lost its storage enclosure and stopped writing.

The dataset is therefore **two disjoint series**, not one 35-day window. Any
per-market price path that spans the gap is broken, and any price *change*
computed across it is meaningless — you would be measuring a 4-day-17-hour jump
as if it were a normal interval.

The `segment` column marks which side each row falls on. Restrict to a single
segment, or handle the discontinuity explicitly.

### 2. `volume24hr` dies partway through

94.4% NULL overall, and **0.0% populated after 2026-07-22** — the upstream API
stopped returning the field. It is 26–40% populated before the outage.

Any feature built on volume or liquidity silently becomes all-NULL for the
larger part of the dataset. Use `spread`, or per-market snapshot frequency
(`markets.n_snaps`) as a rough activity proxy, and label them as proxies.

### 3. No traded prices in `quotes/`

The broad sweep records book quotes only. Its `last` column was 100% NULL, so it
has been **dropped rather than shipped as an empty column named `last`**.

If your question is "did this actually transact" — fill realism, execution
modelling, print-versus-quote — it is only answerable on `watch_quotes`, which
covers 6,867 markets rather than 314,123. Note `last_traded` is itself 83.0%
populated, not 100%.

### 4. Crossed books

A small number of rows have `ask < bid`, which is not physically meaningful and
reflects the two sides being read a moment apart. Filter with `ask >= bid` if
your method is sensitive to it.

### 5. Labels are time-censored

Most `source = 'api'` labels come from a one-off backfill run on 2026-07-23/24.
So "has a label" correlates strongly with "settled before Jul 24" — 169,458
markets (54%) carry an authoritative label, and they are **not a random 54%**.

This bites hardest on walk-forward validation: naively splitting train/test on a
late date can leave you with an empty test set and a script that reports success
anyway. Check your split sizes.

### 6. Convergence labels are biased — prefer `source = 'api'`

Rows with `source = 'convergence'` were inferred by watching the price settle
toward 0 or 1. That method systematically mislabels markets whose books died
before converging, and it selects for markets that converged at all.

On this data that bias was large enough to manufacture an edge that did not
exist — a backtest showed a substantial per-share profit that vanished entirely
when the same cell was recomputed on authoritative labels. They are included
because throwing away data is worse than labelling it, but treat them as a
weak-supervision signal, never as ground truth, and never blend the two sources
without checking how much the choice moves your result.

### 7. `watch_quotes` is not a random sample

The watchlist deliberately tracked the *most active* markets, rotating every
thirty minutes. Anything you measure there describes liquid, high-attention
markets — typically live in-play sports — and will not generalise to the long
tail in `quotes/`.

---

## Coverage

Markets by category:

'''
   category  slugs
     sports 304488
   politics   6530
    culture   1415
    climate   1216
      macro    174
 technology    116
    finance     99
     crypto     57
    science     14
geopolitics     14
'''

Sports dominates by design: it is the bulk of what the venue lists and the bulk
of what trades.

---

## License

**CC BY 4.0** — use it, remix it, build commercial things on it; just give
credit. Full legal code in [LICENSE](LICENSE); attribution and disclaimer in
[NOTICE](NOTICE).

'''
Polymarket Order Book Dataset (2026), Dinesh Gopalakrishnan.
Licensed under CC BY 4.0.
https://github.com/DineshKumar8399/polymarket-orderbook-dataset
'''

## Disclaimer

Research and educational use. This is an independent observational recording of
publicly displayed data and is not affiliated with, endorsed by, or supplied
under agreement with any exchange or venue. Nothing here is financial advice.
Past market behaviour does not predict future market behaviour, and a backtest
on this data is not a trading strategy.

```

</details>

匹配 70 个文件，共 213MB：
- `.gitattributes` 2KB
- `LICENSE` 18KB
- `NOTICE` 1KB
- `README.md` 11KB
- `data_quality.parquet` 2KB
- `labels.parquet` 726KB
- `markets.parquet` 4MB
- `quotes/dt=2026-07-10/data_0.parquet` 785KB
- `quotes/dt=2026-07-11/data_0.parquet` 4MB
- `quotes/dt=2026-07-12/data_0.parquet` 4MB
- `quotes/dt=2026-07-13/data_0.parquet` 3MB
- `quotes/dt=2026-07-14/data_0.parquet` 3MB
- `quotes/dt=2026-07-15/data_0.parquet` 3MB
- `quotes/dt=2026-07-16/data_0.parquet` 4MB
- `quotes/dt=2026-07-17/data_0.parquet` 4MB
- `quotes/dt=2026-07-22/data_0.parquet` 2MB
- `quotes/dt=2026-07-23/data_0.parquet` 4MB
- `quotes/dt=2026-07-24/data_0.parquet` 4MB
- `quotes/dt=2026-07-25/data_0.parquet` 4MB
- `quotes/dt=2026-07-26/data_0.parquet` 4MB
- `quotes/dt=2026-07-27/data_0.parquet` 4MB
- `quotes/dt=2026-07-28/data_0.parquet` 4MB
- `quotes/dt=2026-07-29/data_0.parquet` 4MB
- `quotes/dt=2026-07-30/data_0.parquet` 5MB
- `quotes/dt=2026-07-31/data_0.parquet` 4MB
- `quotes/dt=2026-08-01/data_0.parquet` 2MB
- `quotes/dt=2026-08-02/data_0.parquet` 4MB
- `quotes/dt=2026-08-03/data_0.parquet` 4MB
- `quotes/dt=2026-08-04/data_0.parquet` 4MB
- `quotes/dt=2026-08-05/data_0.parquet` 4MB
- `quotes/dt=2026-08-06/data_0.parquet` 4MB
- `quotes/dt=2026-08-07/data_0.parquet` 4MB
- `quotes/dt=2026-08-08/data_0.parquet` 4MB
- `quotes/dt=2026-08-09/data_0.parquet` 4MB
- `quotes/dt=2026-08-10/data_0.parquet` 4MB
- `quotes/dt=2026-08-11/data_0.parquet` 4MB
- `quotes/dt=2026-08-12/data_0.parquet` 4MB
- `quotes/dt=2026-08-13/data_0.parquet` 4MB
- `quotes/dt=2026-08-14/data_0.parquet` 4MB
- `quotes/dt=2026-08-15/data_0.parquet` 4MB

### `watch_quotes.parquet`

行数 2,874,953，row groups 24

```
ts: timestamp[us]
slug: string
bid: double
ask: double
mid: double
last_traded: double
```

```
                   ts                                 slug   bid   ask    mid  last_traded
0 2026-08-28 14:51:20  aachc-bun-2027-05-22-relegation-bmg  0.34  0.35  0.345         0.43
1 2026-08-28 14:51:41  aachc-bun-2027-05-22-relegation-bmg  0.34  0.35  0.345         0.43
2 2026-08-28 14:52:02  aachc-bun-2027-05-22-relegation-bmg  0.34  0.35  0.345         0.43
3 2026-08-28 14:52:22  aachc-bun-2027-05-22-relegation-bmg  0.34  0.35  0.345         0.43
4 2026-08-28 14:52:43  aachc-bun-2027-05-22-relegation-bmg  0.34  0.35  0.345         0.43
```
- `ts`: 2026-07-10 21:29:30 → 2026-09-13 11:54:18
- `slug`: aachc-bun-2027-05-22-relegation-bmg → urc-usunemp-sa-july-2026-08-07-gt4pt1pct

### `quotes/dt=2026-07-30/data_0.parquet`

行数 8,442,752，row groups 69

```
ts: timestamp[us]
slug: string
category: string
bid: double
ask: double
mid: double
spread: double
volume24hr: double
segment: int32
```

```
                   ts                              slug category   bid   ask   mid  spread  volume24hr  segment
0 2026-07-30 00:00:20  aachc-cfb-txs-2026-01-25-w-texas   sports  0.32  0.34  0.33    0.02         NaN        2
1 2026-07-30 00:02:22  aachc-cfb-txs-2026-01-25-w-texas   sports  0.32  0.34  0.33    0.02         NaN        2
2 2026-07-30 00:04:25  aachc-cfb-txs-2026-01-25-w-texas   sports  0.32  0.34  0.33    0.02         NaN        2
3 2026-07-30 00:06:29  aachc-cfb-txs-2026-01-25-w-texas   sports  0.32  0.34  0.33    0.02         NaN        2
4 2026-07-30 00:08:33  aachc-cfb-txs-2026-01-25-w-texas   sports  0.32  0.34  0.33    0.02         NaN        2
```
- `ts`: 2026-07-30 00:00:20 → 2026-07-30 23:59:46
- `slug`: aachc-cfb-txs-2026-01-25-w-texas → vtc-hrep-to-2026-11-03-lt90m

### `quotes/dt=2026-07-27/data_0.parquet`

行数 8,608,587，row groups 71

```
ts: timestamp[us]
slug: string
category: string
bid: double
ask: double
mid: double
spread: double
volume24hr: double
segment: int32
```

```
                   ts                              slug category  bid  ask  mid  spread  volume24hr  segment
0 2026-07-27 00:00:47  aachc-cfb-txs-2026-01-25-w-texas   sports  NaN  NaN  NaN     NaN         NaN        2
1 2026-07-27 00:02:52  aachc-cfb-txs-2026-01-25-w-texas   sports  NaN  NaN  NaN     NaN         NaN        2
2 2026-07-27 00:04:59  aachc-cfb-txs-2026-01-25-w-texas   sports  NaN  NaN  NaN     NaN         NaN        2
3 2026-07-27 00:07:07  aachc-cfb-txs-2026-01-25-w-texas   sports  NaN  NaN  NaN     NaN         NaN        2
4 2026-07-27 00:09:09  aachc-cfb-txs-2026-01-25-w-texas   sports  NaN  NaN  NaN     NaN         NaN        2
```
- `ts`: 2026-07-27 00:00:47 → 2026-07-27 23:59:14
- `slug`: aachc-cfb-txs-2026-01-25-w-texas → vtc-hrep-to-2026-11-03-lt90m
