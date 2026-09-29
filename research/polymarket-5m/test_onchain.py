import json
from datetime import date

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import onchain as oc

DAYS = (date(2026, 9, 1), date(2026, 9, 20))  # one in each half of the month
N = 20  # markets per day
T_JUMP = 118  # the Binance jump, in market seconds; known (2s lag) from t = 120


def market_starts():
    return [oc.epoch(d) + 3600 + 300 * i for d in DAYS for i in range(N)]


def fill(tok, ts, price, size=10.0, k=0):
    return {"id": f"137_{ts}_{k}", "timestamp": ts, "token_asset_id": tok, "price": price,
            "token_amount": size, "taker_direction": "BUY"}


@pytest.fixture(scope="module")
def root(tmp_path_factory):
    """Each market: a Binance jump at T_JUMP, the side of the move quoted 0.50 before it and
    still filled at 0.50 for a few seconds after it (stale), 0.75 later; it wins 80% of the time.
    Fills near the close die out 3 seconds after it, so D = 3."""
    root = tmp_path_factory.mktemp("onchain")
    pth = oc.paths(root)
    rng = np.random.default_rng(3)
    rows, fills, spot = [], [], []
    for i, start in enumerate(market_starts()):
        up = i % 2 == 0
        side_won = i % 5 != 0
        rows.append({"slug": f"btc-updown-5m-{start}", "coin": "btc", "start": start, "up_token": f"u{i}",
                     "down_token": f"d{i}", "up_won": up == side_won})
        tok = f"u{i}" if up else f"d{i}"
        other = f"d{i}" if up else f"u{i}"
        fills += [fill(tok, start + s, 0.50) for s in range(100, 110)]
        fills += [fill(tok, start + s, 0.50) for s in range(123, 128)]  # stale, after the move
        fills += [fill(tok, start + s, 0.75) for s in range(150, 180)]
        fills += [fill(other, start + s, 0.30) for s in range(123, 128)]
        end = start + 300
        fills += [fill(f"u{i}", end + s, 0.9, k=k) for s in range(-40, 0) for k in range(10)]
        fills += [fill(f"u{i}", end + s, 0.9, k=k) for s, n in ((0, 5), (1, 2), (2, 1)) for k in range(n)]
    for d in DAYS:
        secs = np.arange(oc.epoch(d), oc.epoch(d) + 3600 + 300 * N + 600)
        steps = rng.normal(0, 1e-5, len(secs))
        for i, start in enumerate(s for s in market_starts() if oc.epoch(d) <= s < oc.epoch(d) + 86400):
            steps[start + T_JUMP - secs[0]] += 0.005 if (i + (N if d == DAYS[1] else 0)) % 2 == 0 else -0.005
        spot.append(pd.DataFrame({"sec": secs, "value": 50_000 * np.exp(np.cumsum(steps))}))
    pd.DataFrame(rows).to_csv(pth["markets"], index=False)
    pth["fills"].mkdir()
    pd.DataFrame(fills).to_parquet(pth["fills"] / "2026_09_01.parquet", index=False)
    pd.concat(spot).to_parquet(str(pth["binance"]).format(coin="btc"), index=False)
    return root


def test_settlement_delay_reads_how_fills_die_out(root):
    markets, fills = oc.load(root)
    counts, rate, tail, d95 = oc.settlement_delay(fills, markets)
    assert rate == pytest.approx(10 * len(markets))
    assert tail.loc[0] == pytest.approx(0.5) and tail.loc[2] == pytest.approx(0.1)
    assert d95 == 3
    still_trading = pd.concat([fills, fills.assign(timestamp=fills["timestamp"] + 40)])
    assert oc.settlement_delay(still_trading, markets)[3] is None


def test_triggers_fire_at_the_jump_with_the_signal_lag(root):
    markets, _ = oc.load(root)
    spot = pd.read_parquet(str(oc.paths(root)["binance"]).format(coin="btc"))
    trig = oc.triggers(spot, markets)
    assert len(trig) == len(markets) and (trig["t"] == T_JUMP + 2).all()
    sides = dict(zip(trig["slug"], trig["side"]))
    assert [sides[s] for s in markets["slug"][:4]] == ["Up", "Down", "Up", "Down"]


def test_stale_limit_takes_only_fills_at_the_old_price(root):
    markets, fills = oc.load(root)
    tape = oc.Tape(fills)
    tr = oc.prepare(pd.DataFrame({"slug": markets["slug"][:1], "t": [T_JUMP + 2], "side": ["Up"]}), markets)
    got = oc.price(tr, tape, 3, 8).iloc[0]
    assert got["ref"] == pytest.approx(0.50) and got["price"] == pytest.approx(0.50) and got["n_fills"] == 5
    late = oc.price(tr, tape, 30, 60).iloc[0]  # only 0.75 fills there: above the limit
    assert np.isnan(late["price"]) and late["n_fills"] == 0
    assert oc.price(tr, tape, 30, 60, stale=False).iloc[0]["price"] == pytest.approx(0.75)


def test_run_passes_a_stale_edge_and_reports_d(root, tmp_path):
    out = tmp_path / "r.md"
    assert oc.run(root, out, reps=500, csv_out=tmp_path / "t.csv")
    text = out.read_text()
    assert "D = 3 秒" in text and "结论：通过" in text
    trades = pd.read_csv(tmp_path / "t.csv")
    assert len(trades) == 2 * N and trades["price"].eq(0.5).all()
    assert trades["won"].mean() == pytest.approx(0.8)


def test_run_fails_when_the_book_had_already_moved(root, tmp_path):
    markets, fills = oc.load(root)
    moved = tmp_path / "moved"
    pth = oc.paths(moved)
    pth["fills"].mkdir(parents=True)
    markets.to_csv(pth["markets"], index=False)
    t = fills["timestamp"] % 300
    fills.loc[(t >= 123) & (t < 128) & (fills["price"] == 0.5), "price"] = 0.80  # repriced at once
    fills.to_parquet(pth["fills"] / "2026_09_01.parquet", index=False)
    pd.read_parquet(str(oc.paths(root)["binance"]).format(coin="btc")).to_parquet(
        str(pth["binance"]).format(coin="btc"), index=False)
    assert not oc.run(moved, tmp_path / "r.md", reps=500)
    assert "结论：没通过" in (tmp_path / "r.md").read_text()


def test_filter_fills_keeps_only_listed_tokens(tmp_path):
    t = pa.table({c: pa.array(v, type=pa.large_string()) if isinstance(v[0], str) else v for c, v in {
        "id": ["a", "b", "c"], "order_hash": ["x", "y", "z"], "timestamp": [1, 2, 3],
        "token_asset_id": ["u1", "zz", "d1"], "price": [0.4, 0.5, 0.6], "token_amount": [1.0, 2.0, 3.0],
        "taker_direction": ["BUY", "SELL", "BUY"]}.items()})
    pq.write_table(t, tmp_path / "day.parquet", row_group_size=2)
    assert oc.filter_fills(tmp_path / "day.parquet", {"u1", "d1"}, tmp_path / "kept.parquet") == (3, 2)
    kept = pd.read_parquet(tmp_path / "kept.parquet")
    assert kept["token_asset_id"].tolist() == ["u1", "d1"] and list(kept.columns) == oc.FILL_COLS


def test_fetch_markets_reads_gamma_events(tmp_path):
    start = oc.epoch(date(2026, 9, 1))
    asked = []

    def get(url):
        asked.append(url)
        slugs = [p.split("=", 1)[1] for p in url.split("?", 1)[1].split("&") if p.startswith("slug=")]
        return json.dumps([{"slug": s, "markets": [{
            "slug": s, "outcomes": "[\"Up\", \"Down\"]", "clobTokenIds": f"[\"{s}-u\", \"{s}-d\"]",
            "outcomePrices": "[\"0\", \"1\"]", "closed": True}]} for s in slugs if s.endswith(str(start))]).encode()

    n, asked_slugs = oc.fetch_markets(tmp_path, ("btc", "eth"), date(2026, 9, 1), date(2026, 9, 1), get=get)
    assert (n, asked_slugs) == (2, 2 * 288)
    mk = pd.read_csv(oc.paths(tmp_path)["markets"], dtype={"up_token": str})
    assert set(mk["coin"]) == {"btc", "eth"} and not mk["up_won"].any()
    assert mk["up_token"].iloc[0].endswith("-u")
