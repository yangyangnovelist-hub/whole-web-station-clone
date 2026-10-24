import json

import numpy as np
import pandas as pd

import pm_outcomes as PMO


def test_rule_windows_switch_on_2026_08_07():
    T = np.array([PMO.ts("2026-08-06 23:55"), PMO.ts("2026-08-07"), PMO.ts("2026-08-13 23:55"), PMO.ts("2026-08-14")])
    assert PMO.rule_window("5m", T).tolist() == [0, 30, 30, 60]
    assert PMO.rule_window("15m", T).tolist() == [0, 60, 60, 60]
    assert PMO.rule_window("4h", T).tolist() == [0, 60, 60, 60]
    assert PMO.rule_window("1h", T).tolist() == [0, 0, 0, 0]
    assert PMO.source_window("https://data.chain.link/streams/btc-usd-twap-30s-streams") == 30
    assert PMO.source_window("https://data.chain.link/streams/btc-usd-twap-60s-streams") == 60
    assert PMO.source_window("https://data.chain.link/streams/btc-usd") == 0


def test_slugs_and_window_opens():
    T = PMO.ts("2026-08-20 12:00")
    assert PMO.slug("5m", T) == f"btc-updown-5m-{T}"
    assert PMO.slug("1h", PMO.ts("2026-03-24")) == "bitcoin-up-or-down-march-23-2026-8pm-et"
    assert PMO.slug("1h", PMO.ts("2026-08-20 16:00")) == "bitcoin-up-or-down-august-20-2026-12pm-et"
    w = PMO.window_opens("4h", "2026-03-24", "2026-03-25")
    assert w.tolist() == [PMO.ts("2026-03-24") + 4 * 3600 * k for k in range(6)]


def test_proxy_price_point_and_twap_are_known_at_t():
    sec0 = 1000
    close = np.arange(200, dtype=float)  # close of the kline opening at sec0 + i is i
    t = np.array([1100])
    assert PMO.proxy_price(sec0, close, t, 0)[0] == 99  # the kline opening at t - 1
    assert PMO.proxy_price(sec0, close, t, 30)[0] == np.mean(np.arange(70, 100))
    assert np.isnan(PMO.proxy_price(sec0, close, np.array([1010]), 30)[0])
    r = PMO.proxy_return("5m", np.array([sec0 + 60]), sec0, 100 + np.arange(1000, dtype=float))
    assert np.isclose(r[0], np.log(100 + 359) - np.log(100 + 59))  # before the switch: point to point


def test_twap_label_can_differ_from_point_to_point():
    """After the switch the 5m market compares the 60 s TWAP at the end with the one at the start:
    a last-second jump that makes the point-to-point label Up leaves the TWAP label Down."""
    sec0 = PMO.ts("2026-08-20")
    close = np.full(1200, 100.0)
    close[59] = 99.0           # start: point 99, 60 s TWAP 99.98
    close[300:359] = 99.5
    close[359] = 100.5         # end: point 100.5, 60 s TWAP 99.52
    T = np.array([sec0 + 60])
    up, src, r = PMO.settlement("5m", T, sec0, close)
    assert src[0] == "proxy" and up[0] == 0.0 and r[0] < 0
    assert PMO.proxy_return("5m", T, sec0, close, np.array([0]))[0] > 0  # point to point: Up
    before = np.array([PMO.ts("2026-08-01") + 60])
    up_b, _, _ = PMO.settlement("5m", before, PMO.ts("2026-08-01"), close)
    assert up_b[0] == 1.0  # the same path before the switch settles Up


def test_settlement_prefers_the_official_outcome_and_its_window():
    sec0 = PMO.ts("2026-08-20")
    close = 100 + np.arange(2000, dtype=float)
    T = np.array([sec0 + 300, sec0 + 600])
    o = pd.DataFrame({"h": ["5m"], "T": [sec0 + 300], "up": [0.0], "w": [60]})
    up, src, r = PMO.settlement("5m", T, sec0, close, o)
    assert up.tolist() == [0.0, 1.0] and src.tolist() == ["official", "proxy"]


def event(T, up_price, src="https://data.chain.link/streams/btc-usd-twap-60s-streams", outs=("Up", "Down")):
    prices = [up_price, 1 - up_price] if outs[0] == "Up" else [1 - up_price, up_price]
    return {"slug": f"btc-updown-5m-{T}", "markets": [{
        "outcomes": json.dumps(list(outs)), "outcomePrices": json.dumps([str(p) for p in prices]),
        "clobTokenIds": json.dumps(["tok_a", "tok_b"]), "conditionId": f"0x{T}",
        "eventStartTime": pd.Timestamp(T, unit="s", tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ"),
        "resolutionSource": src}]}


def test_parse_event_reads_up_by_outcome_name():
    e = PMO.parse_event(event(PMO.ts("2026-08-20"), 0.0, outs=("Down", "Up")))
    assert e["up"] == 0.0 and e["w"] == 60 and e["token_up"] == "tok_b" and e["T"] == PMO.ts("2026-08-20")
    e = PMO.parse_event(event(PMO.ts("2026-08-20"), 0.5))
    assert np.isnan(e["up"])  # not resolved


def test_fetch_outcomes_batches_caches_and_resumes(tmp_path):
    calls = []

    def get(url):
        calls.append(url)
        slugs = [x.split("=", 1)[1] for x in url.split("?", 1)[1].split("&") if x.startswith("slug=")]
        return [event(int(s.rsplit("-", 1)[1]), 1.0) for s in slugs if s.startswith("btc-updown-5m-")]

    o = PMO.fetch_outcomes(tmp_path, ("5m",), "2026-08-20", "2026-08-20 01:00", batch=5, workers=2, get=get, log=lambda *a: None)
    assert len(o) == 12 and (o["up"] == 1.0).all() and len(calls) == 3
    PMO.fetch_outcomes(tmp_path, ("5m",), "2026-08-20", "2026-08-20 01:00", batch=5, get=get, log=lambda *a: None)
    assert len(calls) == 3  # all resolved in the cache: nothing asked again


def test_first_taker_buy_and_entry_prices(tmp_path):
    T = 1_000_000
    tr = [{"side": "BUY", "outcome": "Up", "timestamp": T + 2, "price": 0.55},
          {"side": "BUY", "outcome": "Up", "timestamp": T + 1, "price": 0.53},
          {"side": "BUY", "outcome": "Up", "timestamp": T + 1, "price": 0.52},
          {"side": "SELL", "outcome": "Up", "timestamp": T, "price": 0.40},
          {"side": "BUY", "outcome": "Down", "timestamp": T, "price": 0.47},
          {"side": "BUY", "outcome": "Up", "timestamp": T - 1, "price": 0.30}]
    assert PMO.first_taker_buy(tr, "Up", T, 5) == (0.52, 1.0)
    assert PMO.first_taker_buy(tr, "Down", T, 5) == (0.47, 0.0)
    assert np.isnan(PMO.first_taker_buy(tr, "Down", T + 1, 5)[0])
    asked = []

    def get(url):
        asked.append(url)
        return tr

    rows = pd.DataFrame({"h": ["5m", "5m"], "T": [T, T], "side_up": [True, False], "condition_id": ["0xabc", "0xabc"]})
    out = PMO.entry_prices(rows, tmp_path, workers=1, get=get, log=lambda *a: None)
    assert out["px"].tolist() == [0.52, 0.47]
    assert f"start={T}&end={T + 5}" in asked[0]
    PMO.entry_prices(rows, tmp_path, workers=1, get=get, log=lambda *a: None)
    assert len(asked) == 2  # cached
