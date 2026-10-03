# 跨周期一致性：数据探查（whodisidk/polymarket-btc-updown-exchange-data）

<details><summary>README.md</summary>

```
---
viewer: false
tags:
- polymarket
- btc
- up
- down
- prediction-markets
- historical-market-data
- binance
- hyperliquid
- timeseries
- 5m
- 15m
- 1h
---

# Polymarket BTC Up/Down historical market and exchange data

Selected inclusive UTC date range: **2026-05-25 through 2026-08-29**. Market horizons: **5m / 15m / 1h**.

**67 daily archives**. Not every date in this range is included. See `MANIFEST.txt` for the included dates and file sizes.

Historical data for Polymarket BTC Up/Down markets, alongside Binance BTCUSDT and Hyperliquid BTC/USDC observations: order-book snapshots, market features, captured trades, lifecycle information and available resolution records. Market accuracy has not been independently verified.

The daily UTC archives contain Parquet tables and, where present, compressed JSONL lifecycle transitions under `dataset=<table>/date=YYYY-MM-DD/`. Missing observations are not filled during preparation.

## Interpretation and limitations

- Snapshots follow a nominal **100 ms grid**, with missing ticks, interruptions and some missing market windows. See `COVERAGE.md` for coverage by market, horizon and stream.
- Recorded observations are retained, including incomplete and crossed quotes. `COVERAGE.jsonl` provides window-level quality flags; `REPLAY-SEGMENTS.jsonl` lists adjacent windows meeting optional coverage screens. Apply filters during replay.
- Snapshots may repeat the last observed state; quotes may be stale and fields may be null. Timestamp presence and stored quality flags do not establish fresh quotes or usable prices.
- Binance provides trade and reference-price context; its bid/ask fields are missing. Captured trades are in `trades`. Binance order-book history is not included; `books_100ms` contains Polymarket and Hyperliquid snapshots.
- Captured trades and resolution records may be incomplete. Resolution records can contain successive updates for the same market; prices and outcomes have not been independently validated.
- 4h and 1d markets are excluded. Daily archive partitions describe storage dates, not market horizons.
- Features and lifecycle tables include derived values. Missing records do not establish that no market activity occurred.

## Tables

| Table | Interpretation |
| --- | --- |
| `books_100ms` | Polymarket token-level and Hyperliquid order-book snapshots. |
| `polymarket_features_100ms` | Paired UP/DOWN market features and derived calculations. |
| `external_100ms` | Binance and Hyperliquid price, trade and market context. |
| `trades` | Captured exchange trades and reference-price updates, distinguished by source; completeness is not guaranteed. |
| `polymarket_market_100ms` | Periodic market state, timing and quality annotations. |
| `lifecycle` | Market identity, horizon, token pairing and lifecycle updates. |
| `resolution` | Available outcome records and subsequent updates. |
| `lifecycle_transitions` | Lifecycle transition records, where present. |

See `SCHEMA.md` and `SCHEMAS.json` for field descriptions and observed schema variants.

## Integrity

`MANIFEST.txt` records SHA-256 and byte size for each archive; `SHA256SUMS` is a conventional checksum list. Integrity checks verify file consistency, not market accuracy or complete coverage.

```

</details>

<details><summary>SCHEMA.md</summary>

```
# Schema

Parquet files preserve their observed field types and nullability. Nested fields and schema variants are listed in `SCHEMAS.json`; readers should use the schema of each file.

Fields ending in `_ms` denote milliseconds. Other timestamp and price fields retain source semantics; units and freshness should be checked before replay. Market token identifiers are public data identifiers, not authentication tokens.

Window-level quality flags are in `COVERAGE.jsonl`; they do not replace the source annotations or modify quote values. Resolution revisions and quality flags should be interpreted together.

## books_100ms

Token-level Polymarket and Hyperliquid order-book snapshots.

| Field | Observed type(s) |
| --- | --- |
| timestamp_ms | int64 |
| venue | large_string, string |
| instrument | large_string, string |
| market_address | large_string, string |
| market_index | large_string, string |
| side_bids | list<double> |
| bid_sizes | list<double> |
| side_asks | list<double> |
| ask_sizes | list<double> |
| snapshot_kind | large_string, string |
| _run_id | large_string, string |
| dataset | string |
| date | date32[day] |

## external_100ms

Exchange and reference-price context. Binance bid/ask fields are absent.

| Field | Observed type(s) |
| --- | --- |
| timestamp_ms | int64 |
| exchange | large_string |
| instrument | large_string |
| spot_bid | double |
| spot_ask | double |
| midquote_price | double |
| last_trade_price | double |
| last_trade_ts_ms | int64 |
| spread_bps | double |
| spot_volume_1m | double, null |
| funding_rate | null |
| microprice | double |
| imbalance | double |
| short_returns | double, null |
| realized_vol | double, null |
| quote_staleness_ms | double |
| trade_staleness_ms | int64 |
| stale_flags | struct |
| mark_price | double |
| context_mid_price | double |
| context_fields | struct |
| _run_id | large_string |

## lifecycle

Market identity, horizons, token pairing and lifecycle updates.

| Field | Observed type(s) |
| --- | --- |
| record_type | large_string |
| market_id | large_string |
| condition_id | large_string |
| horizon | large_string |
| window_key | large_string |
| slug | large_string |
| lifecycle_state | large_string |
| first_seen_ts | int64 |
| first_tradeable_ts | double, int64 |
| observed_begin_ts | double |
| observed_halt_ts | null |
| resolution_ts | double |
| session_start_ts | double |
| session_end_ts | double |
| betting_deadline_ts | double |
| up_token_id | large_string |
| down_token_id | large_string |
| active | bool |
| closed | bool |
| accepting_orders | bool |
| enable_orderbook | bool |
| resolved | null |
| winner | null |
| chainlink_open_price | double |
| chainlink_open_price_source | large_string |
| open_boundary_captured | bool |
| open_boundary_fallback_used | bool |
| chainlink_resolution_price | double, null |
| close_boundary_captured | bool |
| close_boundary_source | large_string, null |
| close_boundary_fallback_used | bool |
| usable_for_backtest | bool |
| market_quality_flag | large_string |
| run_started_after_market_open | bool |
| last_poll_ts | int64 |
| _run_id | large_string |
| token_ids | list<string> |
| endpoint_end_time | double |
| metadata_version | double |
| betting_deadline | double, null |
| extra | struct |
| series_slug | large_string |
| endpoint_start_time | double |
| event_slug | large_string |

## lifecycle_transitions

Recorded lifecycle transition events, where present.

| Field | Observed type(s) |
| --- | --- |
| record_type | large_string |
| transition_ts_ms | int64 |
| transition_type | large_string |
| market_id | large_string |
| condition_id | large_string |
| horizon | large_string |
| window_key | large_string |
| lifecycle_state | large_string |
| metadata_version | int64 |
| details | struct |
| _run_id | large_string |

## polymarket_features_100ms

Paired UP/DOWN quotes and derived market features.

| Field | Observed type(s) |
| --- | --- |
| timestamp_ms | int64 |
| window_key | large_string, string |
| market_id | large_string, string |
| condition_id | large_string, string |
| lifecycle_state | large_string, string |
| slug | large_string, string |
| up_token_id | large_string, string |
| down_token_id | large_string, string |
| time_to_start_ms | int64 |
| time_to_deadline_ms | double, int64 |
| time_to_resolution_ms | int64 |
| up_best_bid | double |
| up_best_ask | double |
| up_midquote | double |
| up_spread_bps | double |
| up_side_bids | list<double> |
| up_bid_sizes | list<double> |
| up_side_asks | list<double> |
| up_ask_sizes | list<double> |
| down_best_bid | double |
| down_best_ask | double |
| down_midquote | double |
| down_spread_bps | double |
| down_side_bids | list<double> |
| down_bid_sizes | list<double> |
| down_side_asks | list<double> |
| down_ask_sizes | list<double> |
| up_last_trade_price | double |
| up_last_trade_ts_ms | double, int64 |
| down_last_trade_price | double |
| down_last_trade_ts_ms | double, int64 |
| sum_best_bid | double |
| sum_best_ask | double |
| sum_midquote | double |
| yes_no_dislocation_bps | double |
| full_depth_bid_notional | double |
| full_depth_ask_notional | double |
| est_slippage_bps_500 | double |
| dislocation_vs_spot_bps | double |
| observed_halt_flag | bool |
| _run_id | large_string, string |
| dataset | string |
| date | date32[day] |

## polymarket_market_100ms

Market timing, state and recorded quality annotations.

| Field | Observed type(s) |
| --- | --- |
| timestamp_ms | int64 |
| window_key | large_string, string |
| market_id | large_string, string |
| condition_id | large_string, string |
| slug | large_string, string |
| lifecycle_state | large_string, string |
| session_start_ts | int64 |
| session_end_ts | int64 |
| betting_deadline_ts | double, int64 |
| observed_begin_ts | int64 |
| observed_halt_ts | int32, null |
| resolution_ts | int32, null |
| oracle_source | int32, null |
| up_token_id | large_string, string |
| down_token_id | large_string, string |
| resolved_flag | bool |
| outcome_direction | int32, null |
| winner_token_id | int32, null |
| up_won | int32, null |
| down_won | int32, null |
| chainlink_open_price | double, null |
| chainlink_open_price_source | large_string, null, string |
| chainlink_resolution_price | int32, null |
| resolution_price_source | int32, null |
| close_boundary_source | int32, null |
| resolution_consistency | int32, null |
| resolution_from_lifecycle_final_price | bool |
| run_started_after_market_open | bool |
| open_boundary_captured | bool |
| close_boundary_captured | bool |
| open_boundary_fallback_used | bool |
| close_boundary_fallback_used | bool |
| usable_for_backtest | bool |
| market_quality_flag | large_string, string |
| _run_id | large_string, string |
| dataset | string |
| date | date32[day] |

## resolution

Recorded resolution observations and revisions; not independently validated final labels.

| Field | Observed type(s) |
| --- | --- |
| market_id | large_string |
| window_key | large_string |
| condition_id | large_string |
| horizon | large_string |
| slug | large_string |
| resolution_ts | double, int64 |
| resolved_flag | bool |
| outcome_direction | large_string |
| winner_token_id | large_string |
| up_token_id | large_string |
| down_token_id | large_string |
| chainlink_open_price | double, null |
| chainlink_resolution_price | double, null |
| oracle_source | large_string |
| resolution_price_source | large_string, null |
| final_outcome_fields | struct |
| linked_lifecycle_snapshot | struct |
| linked_external_reference_snapshots | list<null>, list<struct> |
| consistency_check | large_string |
| resolution_from_lifecycle_final_price | bool |
| run_started_after_market_open | bool |
| open_boundary_source | large_string, null |
| close_boundary_source | large_string, null |
| open_boundary_captured | bool |
| close_boundary_captured | bool |
| open_boundary_fallback_used | bool |
| close_boundary_fallback_used | bool |
| usable_for_backtest | bool |
| market_quality_flag | large_string |
| emitted_at_ts | int64 |
| revision | int64 |
| _run_id | large_string |

## trades

Captured trades and reference-price updates, distinguished by exchange/source.

| Field | Observed type(s) |
| --- | --- |
| trade_ts_ms | double, int64 |
| recv_ts_ms | int64 |
| exchange | large_string |
| instrument | large_string |
| price | double |
| size | double |
| taker_side | large_string |
| trade_id | large_string |
| market_id | large_string, null |
| condition_id | large_string, null |
| _run_id | large_string |

## lifecycle_transitions

Compressed JSONL lifecycle transitions. Retained record bytes are preserved; nested JSON details retain source structure.

Top-level fields: _run_id, condition_id, details, horizon, lifecycle_state, market_id, metadata_version, record_type, transition_ts_ms, transition_type, window_key.

```

</details>

<details><summary>COVERAGE.md</summary>

```
# BTC coverage

Recorded observations are retained. Coverage and quality flags support filtering during replay; they do not remove market windows from the archives.

`COVERAGE.jsonl` contains window timestamps, per-stream coverage, observed feature-quality counts and optional coverage screens. `REPLAY-SEGMENTS.jsonl` lists adjacent qualifying windows for each horizon and screen.

A run is not gap-free: screen limits apply within each window, and an outage across a window boundary may be longer. Check actual timestamps during replay. Timestamp coverage does not prove fresh quotes, complete exchange data or correct resolution labels.

Flags summarize feature observations per window. Row-level quote checks can be derived from stored UP/DOWN bid/ask fields: missing values, non-finite or out-of-range prices, and bid above ask. Incomplete quotes can be legitimate.

Missing dates and unidentified market windows break runs. Horizons are evaluated independently. No missing observations are filled.

```

</details>

67 个日档：market_parquet_2026-05-25.tar.gz … market_parquet_2026-08-29.tar.gz

## market_parquet_2026-08-19.tar.gz

### books_100ms：90 个文件，73.2 MB

- `dataset=books_100ms/date=2026-08-19/part-market-000001.parquet` 0.8 MB
- `dataset=books_100ms/date=2026-08-19/part-market-000002.parquet` 0.8 MB
- `dataset=books_100ms/date=2026-08-19/part-market-000003.parquet` 0.7 MB
- `dataset=books_100ms/date=2026-08-19/part-market-000004.parquet` 0.9 MB
- `dataset=books_100ms/date=2026-08-19/part-market-000005.parquet` 0.9 MB
- `dataset=books_100ms/date=2026-08-19/part-market-000006.parquet` 0.7 MB
(unreadable: TypeError("unhashable type: 'numpy.ndarray'"))

### external_100ms：343 个文件，24.9 MB

- `dataset=external_100ms/date=2026-08-19/part-market-000091.parquet` 0.1 MB
- `dataset=external_100ms/date=2026-08-19/part-market-000092.parquet` 0.1 MB
- `dataset=external_100ms/date=2026-08-19/part-market-000093.parquet` 0.1 MB
- `dataset=external_100ms/date=2026-08-19/part-market-000094.parquet` 0.1 MB
- `dataset=external_100ms/date=2026-08-19/part-market-000095.parquet` 0.1 MB
- `dataset=external_100ms/date=2026-08-19/part-market-000096.parquet` 0.1 MB
(unreadable: TypeError("unhashable type: 'dict'"))

### polymarket_features_100ms：506 个文件，123.9 MB

- `dataset=polymarket_features_100ms/date=2026-08-19/part-market-000434.parquet` 0.1 MB
- `dataset=polymarket_features_100ms/date=2026-08-19/part-market-000435.parquet` 0.5 MB
- `dataset=polymarket_features_100ms/date=2026-08-19/part-market-000436.parquet` 0.1 MB
- `dataset=polymarket_features_100ms/date=2026-08-19/part-market-000437.parquet` 0.1 MB
- `dataset=polymarket_features_100ms/date=2026-08-19/part-market-000438.parquet` 0.1 MB
- `dataset=polymarket_features_100ms/date=2026-08-19/part-market-000439.parquet` 0.4 MB
(unreadable: TypeError("unhashable type: 'numpy.ndarray'"))

### polymarket_market_100ms：142 个文件，10.8 MB

- `dataset=polymarket_market_100ms/date=2026-08-19/part-market-000940.parquet` 0.2 MB
- `dataset=polymarket_market_100ms/date=2026-08-19/part-market-000941.parquet` 0.0 MB
- `dataset=polymarket_market_100ms/date=2026-08-19/part-market-000942.parquet` 0.2 MB
- `dataset=polymarket_market_100ms/date=2026-08-19/part-market-000943.parquet` 0.0 MB
- `dataset=polymarket_market_100ms/date=2026-08-19/part-market-000944.parquet` 0.2 MB
- `dataset=polymarket_market_100ms/date=2026-08-19/part-market-000945.parquet` 0.0 MB

行数 3,500

```
timestamp_ms: int64
window_key: large_string
market_id: large_string
condition_id: large_string
slug: large_string
lifecycle_state: large_string
session_start_ts: int64
session_end_ts: int64
betting_deadline_ts: double
observed_begin_ts: int64
observed_halt_ts: null
resolution_ts: null
oracle_source: null
up_token_id: large_string
down_token_id: large_string
resolved_flag: bool
outcome_direction: null
winner_token_id: null
up_won: null
down_won: null
chainlink_open_price: double
chainlink_open_price_source: large_string
chainlink_resolution_price: null
resolution_price_source: null
close_boundary_source: null
resolution_consistency: null
resolution_from_lifecycle_final_price: bool
run_started_after_market_open: bool
open_boundary_captured: bool
close_boundary_captured: bool
open_boundary_fallback_used: bool
close_boundary_fallback_used: bool
usable_for_backtest: bool
market_quality_flag: large_string
_run_id: large_string
-- schema metadata --
pandas: '{"index_columns": [], "column_indexes": [], "columns": [{"name":' + 4687
```

```
    timestamp_ms  window_key market_id                                                 condition_id                                       slug lifecycle_state  session_start_ts  session_end_ts  betting_deadline_ts  observed_begin_ts observed_halt_ts resolution_ts oracle_source                                                  up_token_id                                                down_token_id  resolved_flag outcome_direction winner_token_id up_won down_won  chainlink_open_price chainlink_open_price_source chainlink_resolution_price resolution_price_source close_boundary_source resolution_consistency  resolution_from_lifecycle_final_price  run_started_after_market_open  open_boundary_captured  close_boundary_captured  open_boundary_fallback_used  close_boundary_fallback_used  usable_for_backtest    market_quality_flag       _run_id
0  1787144917700  1787144400   3667706  0xe3a65aa5daffa5164bed18388c3468ff8f6bdd2013b814ce444a6e...   bitcoin-up-or-down-august-19-2026-9am-et        pre_open     1787144400000   1787148000000         1.787148e+12      1787141315971             None          None          None  17914165496571000853312510997896655594044449571238717858...  90725149016002540541272542713836356366689504264239793261...          False              None            None   None     None                   NaN                         NaN                       None                    None                  None                   None                                  False                          False                   False                    False                        False                         False                False  missing_open_boundary  8f43992ff912
1  1787144917700  1787144400   3696558  0x9ff588a3fa78aa021331b5921151b1ec8a81dcb43c357661f06917...                  btc-updown-15m-1787144400        pre_open     1787144400000   1787145300000         1.787145e+12      1787143879852             None          None          None  61089817759724233697549891742548199845056959314302386517...  96707504258503597331826076231869290098652971396657667565...          False              None            None   None     None                   NaN                         NaN                       None                    None                  None                   None                                  False                          False                   False                    False                        False                         False                False  missing_open_boundary  8f43992ff912
2  1787144917700  1787144700   3696572  0x4e9e054226b19c1d4b7f3a12aa9cc42ea094d1c8239b04fa1d5057...                   btc-updown-5m-1787144700          active     1787144700000   1787145000000                  NaN      1787144789449             None          None          None  97721937346726548248837036877390090107046418359138311430...  84383293958956574722482174192334170340561454951993488272...          False              None            None   None     None                   NaN                         NaN                       None                    None                  None                   None                                  False                          False                   False                    False                        False                         False                False  missing_open_boundary  8f43992ff912
3  1787144917700  1787145300   3696718  0xe60cecf1ce0b7b72bd00cd06aa7105db471354c3c67a17dc4b0a29...                  btc-updown-15m-1787145300        pre_open     1787145300000   1787146200000                  NaN      1787144789449             None          None          None  57605719487382894062090004372289499479311806988440720985...  37131553147517892072404704538115958901773015692114270984...          False              None            None   None     None                   NaN                         NaN                       None                    None                  None                   None                                  False                          False                   False                    False                        False                         False                False  missing_open_boundary  8f43992ff912
4  1787144917700  1787144400   3696560  0xbb719a4b8d3cc34b4cc0931c1051571f8944cd442bb2805cc10072...                   btc-updown-5m-1787144400          active     1787144400000   1787144700000                  NaN      1787144789449             None          None          None  65867529509075027560998159222542407074417489308084430327...  39744135189863997382646584332214466370519112414852672449...          False              None            None   None     None                   NaN                         NaN                       None                    None                  None                   None                                  False                          False                   False                    False                        False                         False                False  missing_open_boundary  8f43992ff912
5  1787144917700  1787145000   3696698  0xdbf10ba1d01b3ea0c81341ee5671df451dcfdae0dab2c57f7f28da...                   btc-updown-5m-1787145000        pre_open     1787145000000   1787145300000                  NaN      1787144789449             None          None          None  11577739160593484383484898706166654299552375323560783469...  83754853006768503411612174767274211601532874756575343757...          False              None            None   None     None                   NaN                         NaN                       None                    None                  None                   None                                  False                          False                   False                    False                        False                         False                False  missing_open_boundary  8f43992ff912
6  1787144917700  1787148000   3669184  0xe87ac6921914499af71bfe42ba198
```
- `observed_halt_ts`: []
- `resolution_ts`: []
- `oracle_source`: []
- `outcome_direction`: []
- `winner_token_id`: []
- `up_won`: []
- `down_won`: []
- `chainlink_resolution_price`: []
- `resolution_price_source`: []
- `close_boundary_source`: []
- `resolution_consistency`: []

### resolution：206 个文件，7.9 MB

- `dataset=resolution/date=2026-08-19/part-market-001082.parquet` 0.0 MB
- `dataset=resolution/date=2026-08-19/part-market-001083.parquet` 0.0 MB
- `dataset=resolution/date=2026-08-19/part-market-001084.parquet` 0.0 MB
- `dataset=resolution/date=2026-08-19/part-market-001085.parquet` 0.0 MB
- `dataset=resolution/date=2026-08-19/part-market-001086.parquet` 0.0 MB
- `dataset=resolution/date=2026-08-19/part-market-001087.parquet` 0.0 MB
(unreadable: TypeError("unhashable type: 'dict'"))

### trades：198 个文件，31.7 MB

- `dataset=trades/date=2026-08-19/part-market-001288.parquet` 0.3 MB
- `dataset=trades/date=2026-08-19/part-market-001289.parquet` 0.0 MB
- `dataset=trades/date=2026-08-19/part-market-001290.parquet` 0.1 MB
- `dataset=trades/date=2026-08-19/part-market-001291.parquet` 0.2 MB
- `dataset=trades/date=2026-08-19/part-market-001292.parquet` 0.1 MB
- `dataset=trades/date=2026-08-19/part-market-001293.parquet` 0.1 MB

行数 5,000

```
trade_ts_ms: int64
recv_ts_ms: int64
exchange: large_string
instrument: large_string
price: double
size: double
taker_side: large_string
trade_id: large_string
market_id: null
condition_id: null
_run_id: large_string
-- schema metadata --
pandas: '{"index_columns": [], "column_indexes": [], "columns": [{"name":' + 1353
```

```
     trade_ts_ms     recv_ts_ms exchange instrument     price     size taker_side    trade_id market_id condition_id       _run_id
0  1787102575567  1787102575815  binance    BTCUSDT  64529.92  0.00008       sell  6580252385      None         None  8f43992ff912
1  1787102575567  1787102575816  binance    BTCUSDT  64529.92  0.00008       sell  6580252386      None         None  8f43992ff912
2  1787102575567  1787102575816  binance    BTCUSDT  64529.92  0.00008       sell  6580252387      None         None  8f43992ff912
3  1787102575567  1787102575816  binance    BTCUSDT  64529.92  0.00009       sell  6580252388      None         None  8f43992ff912
4  1787102575567  1787102575817  binance    BTCUSDT  64529.92  0.00009       sell  6580252389      None         None  8f43992ff912
5  1787102575567  1787102575817  binance    BTCUSDT  64529.91  0.00008       sell  6580252390      None         None  8f43992ff912
6  1787102575567  1787102575817  binance    BTCUSDT  64528.97  0.00023       sell  6580252391      None         None  8f43992ff912
7  1787102575567  1787102575818  binance    BTCUSDT  64528.96  0.00008       sell  6580252392      None         None  8f43992ff912
```
- `market_id`: []
- `condition_id`: []

