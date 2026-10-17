# 数据集细查

## TimeSeventeen/Polymarket-v2  `*2026_09_0[1-3]*`

<details><summary>README</summary>

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

匹配 9 个文件，共 857MB：
- `OrderFilled/2026_09_01.parquet` 252MB
- `OrderFilled/2026_09_02.parquet` 239MB
- `OrderFilled/2026_09_03.parquet` 233MB
- `daily_aligned/2026_09_01.parquet` 35MB
- `daily_aligned/2026_09_02.parquet` 34MB
- `daily_aligned/2026_09_03.parquet` 33MB
- `daily_aligned_multi/2026_09_01.parquet` 11MB
- `daily_aligned_multi/2026_09_02.parquet` 11MB
- `daily_aligned_multi/2026_09_03.parquet` 10MB

### `OrderFilled/2026_09_01.parquet`

行数 4,003,888，row groups 33

```
id: string
order_hash: string
maker: string
taker: string
timestamp: int64
maker_direction: string
taker_direction: string
token_asset_id: string
token_amount: double
usdc_amount: double
price: double
fee_usdc: double
builder: string
metadata: string
```

```
                  id                                                          order_hash                                       maker                                       taker   timestamp maker_direction taker_direction                                                                 token_asset_id  token_amount  usdc_amount  price  fee_usdc                                                             builder                                                            metadata
0  137_93013105_1520  0xf2bde9ae1fdb08cdbb75f4b0f8c0825babc2278d622fc06631f7e28f1a526951  0x6401bB22C5C215B2e224431F33B2eE4da36C5377  0xE111180000d2663C0091e4f400237545B87B996B  1788224685             BUY            SELL  64139916136119358526888137155898569114662975772595944250730892773488451482677           1.0         0.01   0.01   0.00069  0x4dfb8091b6f3e88cac7a528d7fb4a1ad02983765cc6bd486393150d998cb4dc9  0x0000000000000000000000000000000000000000000000000000000000000000
1  137_93013105_1940  0x0d39f257c310d2a126c9d1e488fc2eb11467d6343a62ae1ceaf17608191f9f90  0x7F01C99a3a09212F19EEC379250c228B69949e91  0xE111180000d2663C0091e4f400237545B87B996B  1788224685             BUY            SELL  97131355977474714958787847519777868376485243776592162642095248159747435087319          14.0         1.68   0.12   0.07392  0x0000000000000000000000000000000000000000000000000000000000000000  0x0000000000000000000000000000000000000000000000000000000000000000
2  137_93013106_1549  0x51085dbf32f6dfc730161e9db442e10a64bff2835c65daf637fce4eddcb69121  0xDE9d8F33470c3c7C1B6c2614E1A254E22e48430A  0xE111180000d2663C0091e4f400237545B87B996B  1788224686             BUY            SELL  86458727877774440707383384928557588801100134320682624106117369615073123013320           6.0         2.64   0.44   0.10348  0x0000000000000000000000000000000000000000000000000000000000000000  0x0000000000000000000000000000000000000000000000000000000000000000
3  137_93013106_1791  0xa6cd110af1c7389e78826433d2a7e39754a6cf286460d34481c8ee2dedf006df  0xf4d55bC4839C7b22FA44d01c60666227210966e1  0x913f5F691Ce0a947CF60cD573CBdC7C66d8eC676  1788224686             BUY            SELL   5645841125342271838215079374200712736249769405954092348714524098905005302258          10.0         1.20   0.12   0.00000  0x0000000000000000000000000000000000000000000000000000000000000000  0x0000000000000000000000000000000000000000000000000000000000000000
4  137_93013106_1951  0x7fb44d5cacfdefb0f9e4abb45a9fef36236c6778939f0928fb7d61319637a19b  0xdfce7768E8a0C7651B18dcc56E046C0f745D47B8  0x224a89Dbe0DB0d6124B335eDaBd15b3f877da3d5  1788224686             BUY            SELL  97131355977474714958787847519777868376485243776592162642095248159747435087319         100.0        47.00   0.47   0.00000  0x0000000000000000000000000000000000000000000000000000000000000000  0x0000000000000000000000000000000000000000000000000000000000000000
```
- `timestamp`: 1788224685 → 1788307198

### `OrderFilled/2026_09_02.parquet`

行数 3,858,729，row groups 32

```
id: string
order_hash: string
maker: string
taker: string
timestamp: int64
maker_direction: string
taker_direction: string
token_asset_id: string
token_amount: double
usdc_amount: double
price: double
fee_usdc: double
builder: string
metadata: string
```

```
                  id                                                          order_hash                                       maker                                       taker   timestamp maker_direction taker_direction                                                                 token_asset_id  token_amount  usdc_amount  price  fee_usdc                                                             builder                                                            metadata
0   137_93068111_559  0x9bc30c10cf87b3f1c1a0d7abf7fe7d43ce64963e994258abcdd51013c4339093  0xCb478CbA19De920f9F7e359c9A8609D7F41C4737  0x29b52d98ac9ef9414b04164246c95BC63d74CC6c  1788307200             BUY            SELL  59874801672059246339694067529327525873222065668334212227254998702288175956053         15.00       7.6500   0.51       0.0  0x0000000000000000000000000000000000000000000000000000000000000000  0x0000000000000000000000000000000000000000000000000000000000000000
1   137_93068111_561  0x1fe7cbeec09472707906cde18141bd3b1b2aff45e751c19d5f8aa341b2a2fc4f  0xA951006f1Ce68498C1aeF9b013880459E6E08A2F  0x29b52d98ac9ef9414b04164246c95BC63d74CC6c  1788307200             BUY            SELL  59874801672059246339694067529327525873222065668334212227254998702288175956053         48.00      24.4800   0.51       0.0  0x0000000000000000000000000000000000000000000000000000000000000000  0x0000000000000000000000000000000000000000000000000000000000000000
2  137_93068113_1070  0xac2837c2fa45b310962193c3f68d9aabdc0aac3b537cf56470821e3418875de3  0xbc5887953750f812bE60E57Ba6E178c847d65E72  0x0484e64092bA4108C2786b61E6fc052d3bf41B1a  1788307203             BUY            SELL  21260910510688009832688130209032890213767525727212452054681289470326476954271         12.63       7.0728   0.56       0.0  0x0000000000000000000000000000000000000000000000000000000000000000  0x0000000000000000000000000000000000000000000000000000000000000000
3  137_93068113_1289  0xea26718a0f3cb982c12b17f06869f10eef5c787f4a88f56b7f8810364badc6fd  0xb1c916c1dAB3da9DF8C4F138B45EbEcf31aB8A13  0xDf6539D1fadb951a02A999d31a72E0Cd7fD9c36d  1788307203             BUY            SELL  21260910510688009832688130209032890213767525727212452054681289470326476954271          5.00       2.5000   0.50       0.0  0x0000000000000000000000000000000000000000000000000000000000000000  0x0000000000000000000000000000000000000000000000000000000000000000
4  137_93068113_1291  0x2426db2c52c113247b4b71394daf40a3c193598332b875f4dc7d80edefd0046b  0xdd715675030b272a9ffea2C5E2FD66Bd506dF6E8  0xDf6539D1fadb951a02A999d31a72E0Cd7fD9c36d  1788307203             BUY            SELL  21260910510688009832688130209032890213767525727212452054681289470326476954271          2.71       1.3550   0.50       0.0  0x0000000000000000000000000000000000000000000000000000000000000000  0x0000000000000000000000000000000000000000000000000000000000000000
```
- `timestamp`: 1788307200 → 1788393598

### `OrderFilled/2026_09_03.parquet`

行数 3,722,917，row groups 31

```
id: string
order_hash: string
maker: string
taker: string
timestamp: int64
maker_direction: string
taker_direction: string
token_asset_id: string
token_amount: double
usdc_amount: double
price: double
fee_usdc: double
builder: string
metadata: string
```

```
                  id                                                          order_hash                                       maker                                       taker   timestamp maker_direction taker_direction                                                                  token_asset_id  token_amount  usdc_amount     price  fee_usdc                                                             builder                                                            metadata
0  137_93125714_1332  0xe6e24001f25795f4800f03fae7732d8afab21fcfaae1d70e76f18cf107188e6f  0x84413583462b68f0A379F533bCbdCD089ACfBC73  0xE111180000d2663C0091e4f400237545B87B996B  1788393604             BUY            SELL   75084186531815017173084991539797135524999518008110763766140658670929815133777        120.32      90.0000  0.748005   1.58756  0x771600518f706ba80a5202446d9c7202efa6c09a75eadda9f3a72a3deac7b3b1  0x0000000000000000000000000000000000000000000000000000000000000000
1  137_93125714_1436  0x216f490bfa069e1651279cd9226a39b47ff73621ebe9f613cf674ea7f3de8142  0x48Fee9F552871192f0417637d3F388A7a26EFD3f  0xE111180000d2663C0091e4f400237545B87B996B  1788393604             BUY            SELL  100673689481064306447042281696915623371134126487224566655903821834895204750731          7.93       4.9654  0.626154   0.12994  0x7ebded22daa608af839ea1fd0a1eeedddb23850c5a374dc63f105a2e0f7e5aea  0x0000000000000000000000000000000000000000000000000000000000000000
2  137_93125714_1637  0x819b3136f3b838e769bd53c508a54ec8322ca69c808bc8b9f1b02dade146029e  0x82ae434f983771A5Bc94092A6850Da80368e2890  0x48Fee9F552871192f0417637d3F388A7a26EFD3f  1788393604             BUY            SELL   43948672634980567926901105782248698991878868409529382429270464514343165668134          3.48       1.3572  0.390000   0.00000  0x1eb72e03ab449a2d2083b7bca2287513436a3599288d19f7a827e630be59a3d9  0x0000000000000000000000000000000000000000000000000000000000000000
3  137_93125714_1682  0xc417d289bbbe79b186249e361fb5b2686a9b69f203a98bd4c9921548e6c1d315  0xf6ab262D360c85F98782B84a04dB35c6e18d3579  0xf40eD828C44291eaFe624B2948F2Dd6560c79973  1788393604             BUY            SELL   75084186531815017173084991539797135524999518008110763766140658670929815133777          5.00       3.5000  0.700000   0.00000  0x0000000000000000000000000000000000000000000000000000000000000000  0x0000000000000000000000000000000000000000000000000000000000000000
4  137_93125715_1174  0xe5141b1422aef04474cd5db4bbd0b70e6eeeb94c5d91ebbe3379f6b7dd3a7400  0xa8877D0E8909fF016a5b560513c6C9f806f8Ef5F  0xBf69D183025786eC230565A6d1c4fE0765E476D8  1788393606             BUY            SELL   18619576573477706841867802427792480706693589350127893189825350798874870214136         11.95       3.3460  0.280000   0.00000  0x0000000000000000000000000000000000000000000000000000000000000000  0x0000000000000000000000000000000000000000000000000000000000000000
```
- `timestamp`: 1788393600 → 1788479998

## TimeSeventeen/Polymarket-v2  `*2026?09?1[0-1]*`

<details><summary>README</summary>

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

匹配 3 个文件，共 251MB：
- `OrderFilled/2026_09_11.parquet` 227MB
- `daily_aligned/2026_09_11.parquet` 13MB
- `daily_aligned_multi/2026_09_11.parquet` 11MB

### `OrderFilled/2026_09_11.parquet`

行数 3,619,999，row groups 30

```
id: string
order_hash: string
maker: string
taker: string
timestamp: int64
maker_direction: string
taker_direction: string
token_asset_id: string
token_amount: double
usdc_amount: double
price: double
fee_usdc: double
builder: string
metadata: string
```

```
                  id                                                          order_hash                                       maker                                       taker   timestamp maker_direction taker_direction                                                                 token_asset_id  token_amount  usdc_amount  price  fee_usdc                                                             builder                                                            metadata
0   137_93586513_187  0x37709096c51ea07c967eafa6391795a2e0f89f2da0d4ce0d929cdfd276d6d96d  0xFfE0092B55e91ce5f8faD7Ad533Daa0858709e69  0xE111180000d2663C0091e4f400237545B87B996B  1789084803             BUY            SELL  35084812423344790035848895176269912494850794700916874778837770364105895514270      2.040817     1.000000  0.490   0.03570  0x0000000000000000000000000000000000000000000000000000000000000000  0x0000000000000000000000000000000000000000000000000000000000000000
1   137_93586513_383  0xaff248ba8f9be6e85cbd289db96d1393df17297fa11bb9725a320cd6e0ba139e  0x32ed2e546b187CA15e2841edC82b22C713cf8eC3  0x0e4c53375f127294F2AB59E0F42542dEd5Be8ec3  1789084803             BUY            SELL  35084812423344790035848895176269912494850794700916874778837770364105895514270      0.581539     0.279139  0.480   0.00000  0x0000000000000000000000000000000000000000000000000000000000000000  0x0000000000000000000000000000000000000000000000000000000000000000
2  137_93586514_1112  0xf812b7f80372463827fd411e3b697703d79cc0199ca1a34b47a09598be683f55  0xeab7b0a830A1755F1296feE950b3080E3529b389  0x76C56d6652D962A65A7FBEadAC4B6E856526ca79  1789084804             BUY            SELL  45127814810347931745731516471880063932041243452569969979053269165408485568382      5.000000     0.005000  0.001   0.00000  0x0000000000000000000000000000000000000000000000000000000000000000  0x0000000000000000000000000000000000000000000000000000000000000000
3  137_93586514_1118  0x1b1dcd3c97882214c00d666c2efc067773f0447149e6f5285fe9dedfa187f782  0x76C56d6652D962A65A7FBEadAC4B6E856526ca79  0xE111180000d2663C0091e4f400237545B87B996B  1789084804             BUY            SELL  14902363915048275478079296645380121248484566017233203901627378740912125259531     15.000000    14.985000  0.999   0.00104  0x0000000000000000000000000000000000000000000000000000000000000000  0x0000000000000000000000000000000000000000000000000000000000000000
4  137_93586514_1550  0x10eb257a34dd929f7e790725129e8fae0195f1cf3719c642919c7e481c3be7f0  0x13e0D447520ebE7f8EEAF7817211201b2C585204  0x0719E5f6aD7bB4659cff00320FaC0654559826EE  1789084804             BUY            SELL  35084812423344790035848895176269912494850794700916874778837770364105895514270      3.885455     1.748455  0.450   0.00000  0x0000000000000000000000000000000000000000000000000000000000000000  0x0000000000000000000000000000000000000000000000000000000000000000
```
- `timestamp`: 1789084800 → 1789171198

### `daily_aligned/2026_09_11.parquet`

行数 693,555，row groups 6

```
asset_id: large_string
block_timestamp: int64
price: double
maker: large_string
taker: large_string
taker_direction: large_string
usdc_amount: double
fee_usdc: double
condition_id: large_string
outcome_seq: int64
neg_risk: large_string
category: large_string
category_refined: large_string
outcome_label: large_string
winning_outcome_label: large_string
resolution_status: large_string
taker_base_fee: double
maker_base_fee: double
opens_at: timestamp[us, tz=UTC]
close_at: timestamp[us, tz=UTC]
resolved_at: timestamp[us, tz=UTC]
market_slug: large_string
p_event: double
D: int8
```

```
                                                                        asset_id  block_timestamp  price                                       maker                                       taker taker_direction  usdc_amount  fee_usdc                                                        condition_id  outcome_seq neg_risk       category category_refined outcome_label winning_outcome_label resolution_status  taker_base_fee  maker_base_fee                  opens_at                         close_at resolved_at                                                                          market_slug  p_event  D
0  94804945936254062473501953881538186399542649981023180786137918450794351699975       1789115058  0.290  0x4c9aFfa3a4F5Ba6b9ab3DD272C20E47ea47E0Cd4  0x982a77e75498FA134607F598c861b8dE9d0c6851             BUY    11.600000       0.0  0x27235b69b909633483255f7edee8c71bb42674627b08599ee0fab5c18481bde2            1    false  uncategorized    uncategorized           Yes                   NaN               NaN          1000.0          1000.0 2026-09-01 16:43:41+00:00 2026-10-01 03:59:59.999000+00:00         NaT                               will-pltr-reach-186-in-september-2026-from-september-1    0.290  1
1  93545829196097645502478845468538500378884659545591615251600784696822943389252       1789161577  0.011  0x510F4963b66B1B18505faaB74b0bB943D1dDa43C  0xEd1696f82De0E4155887c0d5A489dAacb690CC75            SELL     0.323228       0.0  0x1cb0376cda32a95ede7beaf7af651815de0febc4ea961e87bdd8df695f065a1e            1    false  uncategorized    uncategorized           Yes                   NaN               NaN          1000.0          1000.0 2026-08-13 21:50:30+00:00                              NaT         NaT  will-openais-astra-model-debut-on-the-arena-leaderboard-at-a-score-of-at-least-1520    0.011 -1
2  36689083175211575193992678806838180096523091005812327846343899501556924715766       1789163226  0.002  0x6cfeE94B2A4CB41DB4A36C17597a3766BCA886A0  0x2a20Cfc5814B416b28F4d729d2ADCEaBf8A689D2            SELL     0.093340       0.0  0x18784dd7447846b09178fc0a2fd340f08999272631e10443953d9c8cf15692a7            1    false  uncategorized    uncategorized           Yes                   NaN               NaN          1000.0          1000.0 2026-08-13 21:50:27+00:00                              NaT         NaT  will-openais-astra-model-debut-on-the-arena-leaderboard-at-a-score-of-at-least-1510    0.002 -1
3  58792260917145593845014436314007416197591021818803130272466698447004982368912       1789161489  0.900  0xBa8Dd483Ab52bf7Fd1877Ac323a5c2AE7747009d  0xA7d2ec6680f4e5f6Ab0Da244D8006eEc3E20f954             BUY    63.000000       0.0  0xb8b3f47f189260847c94ea61c7ee4291a199bf4ff2d25b0ed3ff26417c4bee18            2    false  uncategorized    uncategorized            No                   NaN               NaN          1000.0          1000.0 2026-08-13 21:50:29+00:00                              NaT         NaT  will-openais-astra-model-debut-on-the-arena-leaderboard-at-a-score-of-at-least-1500    0.100 -1
4  29769222853119390542580144516663262553792446213756525572975144783180650105022       1789161490  0.590  0x4c4Ed674fDE7Dc998036b3ae92445D00fcdb297B  0xc004B035B67bE284E0d1DB56C39e865df6CaC095             BUY   148.367300       0.0  0x5840df9420fd17bc2513782e3b7372a4ae09f1e3260c71e450767d9c2a81238e            2    false  uncategorized    uncategorized            No                   NaN               NaN          1000.0          1000.0 2026-08-13 21:50:29+00:00                              NaT         NaT  will-openais-astra-model-debut-on-the-arena-leaderboard-at-a-score-of-at-least-1480    0.410 -1
```
- `block_timestamp`: 1789084800 → 1789171198
- `market_slug`: 0-ships-transit-hormuz-on-any-date-by-october-31 → zuffa-vanho-akpej-2026-09-12

### `daily_aligned_multi/2026_09_11.parquet`

行数 397,394，row groups 4

```
asset_id: large_string
block_timestamp: int64
price: double
maker: large_string
taker: large_string
taker_direction: large_string
usdc_amount: double
fee_usdc: double
condition_id: large_string
outcome_seq: int64
neg_risk: large_string
category: large_string
category_refined: large_string
outcome_label: large_string
winning_outcome_label: large_string
resolution_status: large_string
taker_base_fee: double
maker_base_fee: double
opens_at: timestamp[us, tz=UTC]
close_at: timestamp[us, tz=UTC]
resolved_at: timestamp[us, tz=UTC]
market_slug: large_string
p_event: double
D: int8
neg_risk_market_id: large_string
```

```
                                                                         asset_id  block_timestamp  price                                       maker                                       taker taker_direction  usdc_amount  fee_usdc                                                        condition_id  outcome_seq neg_risk       category category_refined outcome_label winning_outcome_label resolution_status  taker_base_fee  maker_base_fee                         opens_at                  close_at resolved_at                                                                                 market_slug  p_event  D                                                  neg_risk_market_id
0   85429080116383235366871649469257579401536169213876975155894969355859039578490       1789084807  0.984  0xc29198ad764BD6adaf7bb971A3757a689eCE5d74  0x19277bf626A454d28939a4F2E2e7B142c3abC627            SELL      64.9440       0.0  0xe72a730c50e9f9dd4bf3e646d34a22a8be07d87bd42b7d1173fe17258ca8153b            2     true  uncategorized    uncategorized            No                   NaN               NaN          1000.0          1000.0        2026-08-28 17:15:57+00:00 2026-09-11 00:30:00+00:00         NaT                                                      sud-cie-tor-2026-09-10-exact-score-0-2    0.016  1  0xa0ebcf10d7634bd812b0494301b45ad1deb57d2aae4cc290a0ec29968ea7e300
1    2940595711000683931027984497245775523924729338980367977675368340260725930264       1789084815  0.820  0x496f76BD5Cf4c2819C710aE90Ed1C84B2dc6FA5D  0x6C0dea2107D32ae5E016d43ea3BA52872ecb0885            SELL       4.1000       0.0  0x323a5bae25345a7ac2e22698b13974fc7798d247f67702cf9ed5a3c2924459a2            2     true  uncategorized    uncategorized            No                   NaN               NaN          1000.0          1000.0        2026-09-09 04:58:49+00:00 2026-09-11 12:00:00+00:00         NaT                                highest-temperature-in-kuala-lumpur-on-september-11-2026-29c    0.180  1  0xacf521d2655f2b3d5d8f63a6ba2ad0d92f8235b427bae2863e893debf7fc0d00
2   33339798372916037220786136133406478491116150628220441951753426683441228279665       1789084843  0.002  0x3E9fF2dbF2A6356EE47049E9F3C43a70CB55d57F  0xC69Bd5567b40ef4d11922Eaa57E1F9BE1c642076             BUY       0.0100       0.0  0xefa17dee3af09f69f9ddf245b969aa4efbe7c71cdf06ee49d694408bc33e2ed2            1     true            UCL              UCL           Yes                   NaN               NaN          1000.0          1000.0 2026-07-02 19:03:02.322624+00:00 2027-05-30 23:59:00+00:00         NaT  will-shakhtar-donetsk-win-the-2026-27-uefa-champions-league-championship-20260701202025578    0.002  1  0xfb5399f0e1c4ea4d78a40957d48838ab5769fcec80592b8ce2ffd6e27a3cad00
3  109365771021818243966564428036615214093247289474171950890617939845298355194180       1789084864  0.020  0xa15b7b435214FaDAE0E13A8c089EC242DfaAC4De  0xe436Be3b754eEf6A1Ba4aaD716fB53290e8B13F2            SELL       0.0252       0.0  0xa7b5ecfbfd836f1064faff5cfc0c6417b3ad49ce9b774d28c303c856e4c5f2da            1     true  uncategorized    uncategorized           Yes                   NaN               NaN          1000.0          1000.0        2026-09-10 04:57:51+00:00 2026-09-12 12:00:00+00:00         NaT                                   highest-temperature-in-chongqing-on-september-12-2026-31c    0.020 -1  0x3aa5bbaf7859f98a57644c2a6c24ec8d5d0c60e4281ccf8f3de8aca156ffc300
4   33339798372916037220786136133406478491116150628220441951753426683441228279665       1789084870  0.002  0x3E9fF2dbF2A6356EE47049E9F3C43a70CB55d57F  0xC69Bd5567b40ef4d11922Eaa57E1F9BE1c642076             BUY       0.0100       0.0  0xefa17dee3af09f69f9ddf245b969aa4efbe7c71cdf06ee49d694408bc33e2ed2            1     true            UCL              UCL           Yes                   NaN               NaN          1000.0          1000.0 2026-07-02 19:03:02.322624+00:00 2027-05-30 23:59:00+00:00         NaT  will-shakhtar-donetsk-win-the-2026-27-uefa-champions-league-champio
```
- `block_timestamp`: 1789084800 → 1789171198
- `market_slug`: 2026-balance-of-power-d-senate-d-house-949 → zelenskyy-of-tweets-september-8-september-15-2026-80-99
