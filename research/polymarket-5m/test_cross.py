import io
import tarfile

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import cross

E = 1_787_145_300_000  # ms; a 5m and a 15m market end here


def parquet_bytes(df):
    buf = io.BytesIO()
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), buf)
    return buf.getvalue()


@pytest.fixture
def archive(tmp_path):
    """5m (reference 100) and 15m (reference 101) ending at E. For one second the 5m Up ask is
    0.30 and the 15m Down ask 0.60 (box 0.90 + fees < 1); otherwise the box costs 1.02."""
    ts = np.arange(E - 300_000, E, 100)
    cheap = (ts >= E - 60_000) & (ts < E - 59_000)
    f5 = pd.DataFrame({"timestamp_ms": ts, "market_id": "m5", "lifecycle_state": "active",
                       "up_best_bid": 0.40, "up_best_ask": np.where(cheap, 0.30, 0.42),
                       "down_best_bid": 0.58, "down_best_ask": 0.60,
                       "up_ask_sizes": [[25.0, 5.0]] * len(ts), "down_ask_sizes": [[30.0]] * len(ts)})
    f15 = pd.DataFrame({"timestamp_ms": ts, "market_id": "m15", "lifecycle_state": "active",
                        "up_best_bid": 0.38, "up_best_ask": 0.40, "down_best_bid": 0.58, "down_best_ask": 0.60,
                        "up_ask_sizes": [[10.0]] * len(ts), "down_ask_sizes": [[40.0]] * len(ts)})
    mk = pd.DataFrame({"timestamp_ms": [E, E], "market_id": ["m5", "m15"], "slug": ["btc-updown-5m-x", "btc-updown-15m-x"],
                       "session_start_ts": [E - 300_000, E - 900_000], "session_end_ts": [E, E],
                       "chainlink_open_price": [100.0, 101.0], "up_won": [1.0, 0.0], "lifecycle_state": ["closed"] * 2})
    path = tmp_path / "market_parquet_2026-08-19.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        for name, df in (("dataset=polymarket_features_100ms/date=2026-08-19/part-1.parquet", f5),
                         ("dataset=polymarket_features_100ms/date=2026-08-19/part-2.parquet", f15),
                         ("dataset=polymarket_market_100ms/date=2026-08-19/part-3.parquet", mk)):
            raw = parquet_bytes(df)
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))
    return path


def test_box_on_the_lower_reference(archive):
    feat, mk, rs = cross.read_day(archive)
    assert set(feat["market_id"]) == {"m5", "m15"} and feat["up_ask_size"].iloc[0] == 25.0
    mkts = cross.market_table(mk, rs)
    assert dict(zip(mkts["market_id"], mkts["horizon"])) == {"m5": 5, "m15": 15}
    b = cross.boxes(feat, mkts)
    assert len(b) == 1
    r = b.iloc[0]
    fee = 0.07 * 0.3 * 0.7 + 0.07 * 0.6 * 0.4
    assert r["pair"] == "5m-15m" and r["min_cost"] == pytest.approx(0.90 + fee)
    assert r["n_persist"] == 9 and r["persist_size"] == 25.0 and r["tau_best"] == pytest.approx(60.0)
    assert r["payoff"] == 2.0  # Up won on the 5m (final >= 100) and Down on the 15m (final < 101)
