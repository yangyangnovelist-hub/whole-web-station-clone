import gzip
import json

import numpy as np
import pandas as pd
import pytest

import binary as bo
import real_day as rd

S0 = 1_788_825_600  # first market opens here


def write_gz(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt") as f:
        f.write(text)


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    """A two-market bundle laid out like the outcometick sample."""
    root = tmp_path_factory.mktemp("bundle") / "polymarket-data-samples"
    rng = np.random.default_rng(3)
    secs = np.arange(S0 - 1200, S0 + 700)
    price = 80_000 * np.exp(np.cumsum(rng.normal(0, 1e-4, len(secs))))
    twap = pd.Series(price).rolling(60, min_periods=1).mean().to_numpy()

    def csv(values):
        fa = [str(int(round(v * 1e6)) * 10**12) for v in values]
        return pd.DataFrame({"feed_ts_ms": secs * 1000, "value": values, "full_accuracy_value": fa,
                             "server_ts_ms": secs * 1000 + 300, "recv_ms": secs * 1000 + 1500}).to_csv(index=False)

    write_gz(root / "data/chainlink/daily/prices/BTCUSD/BTCUSD-prices-2026-09-08.csv.gz", csv(price))
    write_gz(root / "data/chainlink-twap-60s/daily/prices/BTCUSD/BTCUSD-twap60s-prices-2026-09-08.csv.gz", csv(twap))
    tw_int = {s: int(round(v * 1e6)) * 10**12 for s, v in zip(secs, twap)}

    markets, bba, book = [], [], []
    for i, start in enumerate((S0, S0 + 300)):
        end = start + 300
        up_tok, dn_tok = f"up{i}", f"dn{i}"
        up_won = tw_int[end] >= tw_int[start]
        markets.append(json.dumps({
            "slug": f"btc-updown-5m-{start}", "start_sec": start, "end_sec": end, "resolved": True,
            "token_ids": [up_tok, dn_tok], "outcome_prices": ["1", "0"] if up_won else ["0", "1"],
            "strike_value": str(tw_int[start]),
            "raw": {"outcomes": "[\"Up\", \"Down\"]", "feeSchedule": {"rate": 0.07},
                    "cryptoMarketConfig": {"twapLookbackSeconds": 60}},
        }))
        for t in range(0, 300):
            p = 0.97 if up_won and t > 200 else (0.03 if t > 200 else 0.5)
            bid, ask = bo.quote_book(p)
            ms = (start + t) * 1000 - 50
            for tok, b, a in ((up_tok, bid, ask), (dn_tok, 1 - ask, 1 - bid)):
                bba.append(json.dumps({"asset_id": tok, "recv_ms": ms, "event_ts_ms": ms - 10,
                                       "payload": {"best_bid": f"{b:.2f}", "best_ask": f"{a:.2f}"}}))
                book.append(json.dumps({"asset_id": tok, "recv_ms": ms, "payload": {
                    "asks": [{"price": f"{a:.2f}", "size": "150"}, {"price": f"{min(a + 0.01, 1):.2f}", "size": "50"}]}}))
    write_gz(root / "data/polymarket/daily/markets/BTC-5m/BTC-5m-markets-2026-09-08.jsonl.gz", "\n".join(markets))
    write_gz(root / "data/polymarket/daily/best_bid_ask/BTC-5m/BTC-5m-best_bid_ask-2026-09-08.jsonl.gz", "\n".join(bba))
    write_gz(root / "data/polymarket/daily/book/BTC-5m/BTC-5m-book-2026-09-08.jsonl.gz", "\n".join(book))
    return root


def test_settlement_checks_use_exact_boundary_reports(bundle):
    markets = rd.load_markets(bundle)
    spot = rd.load_chainlink(rd._one(bundle, "BTCUSD-prices-*.csv.gz"))
    twap = rd.load_chainlink(rd._one(bundle, "BTCUSD-twap60s-prices-*.csv.gz"))
    checks = rd.settlement_checks(markets, spot, twap)
    assert rd.count(checks["strike_is_open"]) == "2/2"
    assert rd.count(checks["twap_rule"]) == "2/2"


def test_panel_has_no_lookahead(bundle):
    markets = rd.load_markets(bundle)
    spot = rd.load_chainlink(rd._one(bundle, "BTCUSD-prices-*.csv.gz"))
    quotes = rd.load_quotes(bundle, set(markets["up_token"]) | set(markets["down_token"]))
    panel = rd.build_panel(markets, spot, quotes, rd.vol_forecasts(spot))
    assert len(panel) == 2 * bo.WINDOW_S
    # Chainlink ticks arrive 1.5s late, so at second t the model only knows t-2.
    assert (panel["t_info"] <= panel["t"] - 1).loc[panel["t"] >= 2].all()
    assert panel["p[" + rd.MAIN_VOL + "]"].between(0, 1).all()


def test_vol_models_share_units(bundle):
    spot = rd.load_chainlink(rd._one(bundle, "BTCUSD-prices-*.csv.gz"))
    vols = rd.vol_forecasts(spot).iloc[-1]
    # An unsmoothed random walk has the same vol at every return horizon.
    assert vols["30分钟·15秒收益"] == pytest.approx(vols["30分钟·1秒收益"], rel=0.25)
    assert vols["30分钟·1秒收益"] == pytest.approx(1e-4, rel=0.25)


def test_report_end_to_end(bundle, tmp_path):
    out = tmp_path / "day.md"
    rd.main([str(bundle), "--out", str(out)])
    text = out.read_text(encoding="utf-8")
    for heading in ("## 1. 结算与数据核对", "## 2.", "## 3.", "## 4.", "## 5."):
        assert heading in text
    assert "253" not in text and "2/2" in text
