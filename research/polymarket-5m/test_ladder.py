import json
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest
from scipy.stats import norm

import ladder as ld
import ladder_check as lc
import recording as rc

T = int(datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc).timestamp())  # 04:00 ET on 2026-10-05


def _utc(s):
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


# ------------------------------------------------------------------ titles and dates

@pytest.mark.parametrize("title,expected", [
    ("86,000", ("level", 86000.0, None)),
    ("↑ 86,000", ("up", 86000.0, None)),
    ("↓ 80,000", ("down", None, 80000.0)),
    ("84,000-86,000", ("range", 84000.0, 86000.0)),
    ("84,000 – 86,000", ("range", 84000.0, 86000.0)),
    ("<74,000", ("range", None, 74000.0)),
    (">92,000", ("range", 92000.0, None)),
    ("$86k", ("level", 86000.0, None)),
    ("Up", None),
])
def test_title_parsing(title, expected):
    assert ld.parse_title(title) == expected


def test_noon_et_in_utc_follows_daylight_saving():
    assert ld.noon_et(date(2026, 10, 5)) == _utc("2026-10-05T16:00:00Z")   # EDT
    assert ld.noon_et(date(2026, 11, 1)) == _utc("2026-11-01T17:00:00Z")   # EST from 2026-11-01 02:00
    assert ld.et_to_utc(date(2026, 11, 1), 0) == _utc("2026-11-01T04:00:00Z")  # midnight still EDT
    assert ld.noon_et(date(2026, 3, 7)) == _utc("2026-03-07T17:00:00Z")
    assert ld.noon_et(date(2026, 3, 8)) == _utc("2026-03-08T16:00:00Z")
    assert ld.day_slug(date(2026, 10, 5)) == "october-5-2026"
    assert ld.et_date(_utc("2026-10-05T03:59:00Z")) == date(2026, 10, 4)
    assert ld.et_date(_utc("2026-10-05T04:00:00Z")) == date(2026, 10, 5)
    assert ld.et_date(_utc("2026-12-05T04:30:00Z")) == date(2026, 12, 4)    # EST: midnight is 05:00 UTC


def test_dst_fallback_matches_the_tz_database():
    if ld._tz() is None:
        pytest.skip("no tz database")
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("America/New_York")
    d = date(2026, 1, 1)
    while d.year < 2028:
        for hour in (0, 12):
            off = -datetime(d.year, d.month, d.day, hour, tzinfo=tz).utcoffset().total_seconds() / 3600
            assert ld._us_dst(d, hour) == (off == 4), (d, hour)
        d = date.fromordinal(d.toordinal() + 1)


def test_four_hour_starts():
    assert ld.h4_starts(_utc("2026-10-04T08:21:00Z")) == [_utc("2026-10-04T08:00:00Z"), _utc("2026-10-04T12:00:00Z")]
    # in winter the ET-aligned candidates (05:00 UTC = midnight EST) are tried as well
    assert ld.h4_starts(_utc("2026-12-01T10:00:00Z")) == [_utc(f"2026-12-01T{h:02d}:00:00Z") for h in (8, 9, 12, 13)]


# ------------------------------------------------------------------ discovery

def _gm(cid, title, end, question="", outcomes=("Yes", "No"), closed=False, **kw):
    return {"conditionId": cid, "slug": f"m-{cid}", "groupItemTitle": title, "question": question, "endDate": end,
            "outcomes": json.dumps(list(outcomes)), "clobTokenIds": json.dumps([f"{cid}-a", f"{cid}-b"]),
            "closed": closed, "orderPriceMinTickSize": 0.01, **kw}


def _events():
    noon, midnight = "2026-10-05T16:00:00Z", "2026-10-06T04:00:00Z"
    return {
        "bitcoin-above-on-october-5-2026": [{"slug": "bitcoin-above-on-october-5-2026", "markets": [
            _gm("a84", "84,000", noon), _gm("a86", "86,000", noon), _gm("a88", "88,000", noon, closed=True)]}],
        "bitcoin-price-on-october-5-2026": [{"slug": "bitcoin-price-on-october-5-2026", "markets": [
            _gm("r1", "<84,000", noon), _gm("r2", "84,000-86,000", noon), _gm("r3", ">86,000", noon)]}],
        "what-price-will-bitcoin-hit-on-october-5-2026": [{"slug": "what-price-will-bitcoin-hit-on-october-5-2026",
                                                           "startDate": "2026-10-05T04:00:00Z",
                                                           "endDate": midnight, "closed": False,
                                                           "markets": [
            _gm("h1", "↑ 86,000", midnight, "Will Bitcoin reach $86,000 on October 5?"),
            _gm("h2", "↓ 84,000", midnight, "Will Bitcoin dip to $84,000 on October 5?")]}],
        "bitcoin-up-or-down-on-october-5-2026": [{"slug": "bitcoin-up-or-down-on-october-5-2026", "markets": [
            _gm("u1", "", noon, outcomes=("Up", "Down"), eventStartTime="2026-10-04T16:00:00Z")]}],
        f"btc-updown-4h-{T}": [{"slug": f"btc-updown-4h-{T}", "markets": [
            _gm("f1", "", "2026-10-05T12:00:00Z", outcomes=("Up", "Down"), eventStartTime="2026-10-05T08:00:00Z")]}],
        f"btc-updown-4h-{T + 14400}": [{"slug": f"btc-updown-4h-{T + 14400}", "markets": [
            _gm("f2", "", "2026-10-05T16:00:00Z", outcomes=("Up", "Down"), eventStartTime="2026-10-05T12:00:00Z")]}],
    }


def _fetch(url):
    ev = _events()
    if url == ld.GAMMA_SEARCH:
        return {"events": ev["what-price-will-bitcoin-hit-on-october-5-2026"]}
    if "events?slug=" in url:
        return ev.get(url.split("slug=")[1], [])
    if "/clob-markets/" in url:
        cid = url.rsplit("/", 1)[1]
        return {"itode": cid == "f2", "mts": 0.01, "mos": 5, "fd": {"r": 0.07},
                "t": [{"t": f"{cid}-a"}, {"t": f"{cid}-b"}]}
    if "klines" in url:
        ms = int(url.split("startTime=")[1].split("&")[0])
        return [[ms, "85000.5", "85010", "84990", "84999.5", "1", ms + 59999]]
    raise AssertionError(url)


def test_discover_types_bounds_times_and_the_delay_check():
    markets, notes = ld.discover(_fetch, now=T + 30)
    by = {m["condition_id"]: m for m in markets}
    assert set(by) == {"a84", "a86", "r1", "r2", "r3", "h1", "h2", "u1", "f1"}   # a88 closed, f2 delayed
    assert any("itode" in n and "m-f2" in n for n in notes)
    assert [by[c]["type"] for c in ("a84", "r2", "h1", "h2", "u1", "f1")] == \
        ["above", "range", "hit_up", "hit_down", "updown_day", "updown_4h"]
    assert (by["a86"]["lo"], by["r1"]["lo"], by["r1"]["hi"], by["r3"]["lo"], by["h2"]["hi"]) == \
        (86000, None, 84000, 86000, 84000)
    assert all(m["end_ok"] for m in markets) and not any(m["itode"] for m in markets)
    assert by["h1"]["start_ts"] == _utc("2026-10-05T04:00:00Z")
    assert (by["a84"]["yes_token"], by["a84"]["no_token"], by["u1"]["yes_outcome"]) == ("a84-a", "a84-b", "Up")
    # the day market's reference: the close of the candle closing at 16:00 UTC the day before
    assert by["u1"]["ref_ts"] == _utc("2026-10-04T16:00:00Z") and by["u1"]["ref_price"] == 84999.5
    assert by["f1"]["ref_price"] == 85000.5 and by["f1"]["settle"] == "chainlink-twap"
    assert "9 undelayed markets" in ld.summary(markets, notes)


def test_failed_clob_check_is_not_cached_as_undelayed():
    cache, calls = {}, 0

    def flaky(url):
        nonlocal calls
        if url == ld.GAMMA_SEARCH:
            event = dict(_events()["what-price-will-bitcoin-hit-on-october-5-2026"][0])
            event["markets"] = event["markets"][:1]
            return {"events": [event]}
        if "events?slug=" in url:
            return []
        calls += 1
        if calls <= 2:
            raise TimeoutError("temporary")
        cid = url.rsplit("/", 1)[1]
        return {"itode": True, "mts": 0.01, "t": [{"t": f"{cid}-a"}, {"t": f"{cid}-b"}]}

    first, notes = ld.discover(flaky, now=T + 30, days=1, cache=cache)
    assert not first and not cache and any("temporary" in n for n in notes)
    second, notes = ld.discover(flaky, now=T + 30, days=1, cache=cache)
    assert not second and cache and all(v["itode"] for v in cache.values())
    assert any("itode" in n for n in notes)


def test_fixed_limit_and_live_minimum_order_shape():
    import fill_rate as fr
    assert fr.pilot_order_shares(0.49) == 5.0
    assert fr.pilot_order_shares(0.10) >= 10.0
    p = fr.limit_price(0.62, 0.03, 0.01)
    assert 0.56 < p < 0.59
    assert 0.62 - p - 0.07 * p * (1 - p) >= 0.03 - 1e-12


def test_recorder_refresh_subscribes_new_tokens_and_drops_ended_markets(tmp_path):
    r = ld.LadderRecorder(tmp_path, fetch=_fetch)
    markets, _ = ld.discover(_fetch, now=T + 30)
    new = r.refresh(markets, now=T + 30)
    assert len(new) == 18 and r.token_coin["a84-a"] == "btc"
    assert r.refresh(markets, now=T + 60) == []
    r.refresh([], now=_utc("2026-10-05T12:20:00Z"))   # the 4-hour market ended at 12:00
    assert "f1-a" not in r.subscribed and "a84-a" in r.subscribed
    r.rec.close()


# ------------------------------------------------------------------ build

def _frame(t_ms):
    return [{"event_type": "book", "asset_id": "y", "timestamp": str(t_ms),
             "bids": [{"price": "0.40", "size": "10"}], "asks": [{"price": "0.60", "size": "20"},
                                                                  {"price": "0.70", "size": "5"}]},
            {"event_type": "price_change", "timestamp": str(t_ms + 100), "price_changes": [
                {"asset_id": "y", "price": "0.55", "size": "8", "side": "SELL"},
                {"asset_id": "n", "price": "0.45", "size": "8", "side": "BUY"}]},
            {"event_type": "tick_size_change", "asset_id": "y", "old_tick_size": "0.01", "new_tick_size": "0.001",
             "timestamp": str(t_ms + 150)},
            {"event_type": "price_change", "timestamp": str(t_ms + 200), "price_changes": [
                {"asset_id": "y", "price": "0.70", "size": "0", "side": "SELL"}]},
            {"event_type": "last_trade_price", "asset_id": "y", "price": "0.55", "size": "3", "side": "BUY",
             "timestamp": str(t_ms + 250)}]


def test_book_rows_are_book_events_xtop_rows_plus_tick_and_depth():
    recs = [{"recv_ms": 1_000_400, "msg": _frame(1_000_000)}]
    ours = list(ld.book_rows(recs, depth_tokens={"y"}, ticks={"y": 0.01}))
    xtop = [r for k, r in rc.book_events(recs) if k == "xtop"]
    tops = [r for k, r in ours if k == "top"]
    # the depth change at +200 ms (0.70 removed, top unchanged) is one more row for a depth token
    assert [(r["asset_id"], r["ts"], r["top"]) for r in tops if r["ts"] != 1000.2] == \
        [(r["asset_id"], r["ts"], r["top"]) for r in xtop]
    assert [r["ts"] for r in tops] == [1000.0, 1000.1, 1000.1, 1000.2]
    assert [r["tick"] for r in tops if r["asset_id"] == "y"] == [0.01, 0.01, 0.001]
    fee = lambda p: 0.07 * p * (1 - p)  # noqa: E731
    assert tops[0]["depth"] == (25.0, pytest.approx(round((0.4 - fee(0.6)) * 20 + (0.3 - fee(0.7)) * 5, 6)))
    assert tops[-1]["depth"] == (28.0, pytest.approx(round((0.45 - fee(0.55)) * 8 + (0.4 - fee(0.6)) * 20, 6)))
    assert np.isnan(tops[2]["depth"][0])  # "n" is not a depth token
    trades = [r for k, r in ours if k == "trade"]
    assert trades == [{"asset_id": "y", "ts": 1000.25, "recv_ms": 1_000_400, "price": 0.55, "size": 3.0, "side": "BUY"}]


def test_build_and_load_a_recording(tmp_path):
    """Raw files written by LadderRecorder (paper_trader's recorder and handlers) convert into books
    stamped with the exchange time, Binance trades in latency.py's format and the markets table."""
    r = ld.LadderRecorder(tmp_path, fetch=_fetch)
    markets, notes = ld.discover(_fetch, now=T + 30)
    r.refresh(markets, now=T + 30)
    r.rec.write("markets", {"at": 0, "markets": markets, "notes": notes})
    tok = markets[0]["yes_token"]
    r.on_clob(json.dumps([{**f, "asset_id": tok} if "asset_id" in f else
                          {**f, "price_changes": [{**pc, "asset_id": tok} for pc in f["price_changes"]]}
                          for f in _frame(T * 1000)]), T * 1000 + 300)
    r.on_clob(json.dumps({"event_type": "price_change", "timestamp": str(T * 1000 + 300), "price_changes": [
        {"asset_id": tok, "price": "0.54", "size": "2", "side": "SELL"}]}), T * 1000 + 350)
    r.on_clob(json.dumps({"event_type": "book", "asset_id": "not-ours", "bids": [], "asks": []}), T * 1000 + 400)
    r.on_clob("PONG", T * 1000 + 500)
    for k in range(5):
        r.on_binance(json.dumps({"stream": "btcusdt@aggTrade", "data": {
            "e": "aggTrade", "s": "BTCUSDT", "T": T * 1000 + 100 * k, "p": f"{85000 + k}", "q": "0.1", "m": True}}),
            T * 1000 + 100 * k + 40)
    r.rec.write("errors", {"at": T * 1000 + 700, "where": "clob-open", "tokens": 18})
    r.rec.write("errors", {"at": T * 1000 + 900, "where": "clob", "err": "closed 1006"})
    r.rec.close()
    counts = ld.build(tmp_path, tmp_path / "built")
    assert counts == {"markets": 9, "books": 3, "clob_trades": 1, "binance": 5, "clob_closes": 1}
    books, mk, spot, trades, closes = ld.load(tmp_path / "built")
    # one row per top-of-book change, at the exchange's time; the removal of a second level is none
    assert list(books["ts"]) == [T, T + 0.1, T + 0.3] and list(books["recv"]) == [T + 0.3, T + 0.3, T + 0.35]
    assert list(books["ask"]) == [0.6, 0.55, 0.54] and list(books["bid"]) == [0.4, 0.45, 0.45]
    assert list(books["tick"]) == [0.01, 0.01, 0.001] and list(books["ask_size"]) == [20, 8, 2]
    assert books["market_id"].iloc[0] == tok and np.isnan(books["depth99"]).all()  # an above market
    assert list(spot["trade_ts"]) == pytest.approx([T + 0.1 * k for k in range(5)])
    assert spot["receive_ts"].iloc[0] == pytest.approx(T + 0.04)
    assert list(trades["price"]) == [0.55] and list(closes) == [pytest.approx(T + 0.9)]
    assert set(mk["type"]) == set(ld.TYPES) and mk.loc[mk["condition_id"] == "u1", "ref_price"].iloc[0] == 84999.5


# ------------------------------------------------------------------ model

def test_model_fair_values():
    sd = 0.01
    assert lc.fair_yes("above", 100.0, sd, lo=100.0) == pytest.approx(0.5)
    assert lc.fair_yes("above", 100 * np.exp(sd), sd, lo=100.0) == pytest.approx(0.8413447, abs=1e-6)
    r = lc.fair_yes("range", 100.0, sd, lo=100 * np.exp(-sd), hi=100 * np.exp(sd))
    assert r == pytest.approx(0.6826895, abs=1e-6)
    assert lc.fair_yes("range", 100.0, sd, lo=np.nan, hi=100.0) == pytest.approx(0.5)      # "<100"
    assert lc.fair_yes("range", 100.0, sd, lo=100 * np.exp(-sd), hi=np.nan) == pytest.approx(0.8413447, abs=1e-6)
    assert lc.fair_yes("hit_up", 100 * np.exp(-sd), sd, lo=100.0) == pytest.approx(2 * norm.cdf(-1))
    assert lc.fair_yes("hit_up", 100.0, sd, lo=100.0) == 1.0
    assert lc.fair_yes("hit_down", 100 * np.exp(2 * sd), sd, hi=100.0) == pytest.approx(2 * norm.cdf(-2))
    assert lc.fair_yes("hit_down", 99.0, sd, hi=100.0) == 1.0
    assert lc.fair_yes("updown_day", 101.0, sd, ref=101.0) == pytest.approx(0.5)
    # sigma per second times sqrt(seconds left): 1e-4 over 2,500 s is sd 0.005
    assert lc.fair_yes("above", 100 * np.exp(0.005), 1e-4 * np.sqrt(2500), lo=100.0) == pytest.approx(norm.cdf(1))


# ------------------------------------------------------------------ analysis on a fixture

S0 = 1_791_000_000
J = S0 + 650.05                 # the designed Binance jump
END = J + 600                   # the above market's expiry (10 minutes after the jump)


def at_j(rows):
    return (rows["jump"] - J).abs() < 1e-6


@pytest.fixture(scope="module")
def fixture():
    """Binance prints every 0.5 s with noise, flat for 8 s, then +0.3% at J; an above market whose
    strike the jump crosses, its Yes book 0.48 / 0.50 until the exchange stamps 0.90 / 0.92 at
    J + 0.35; a hit_up market whose level the jump reaches, Yes asks 0.60 (100) and 0.70 (50)
    until J + 0.25, then 0.995."""
    rng = np.random.default_rng(3)
    ts = np.round(np.arange(S0, S0 + 800, 0.5), 3)
    step = rng.normal(0, 2e-5, len(ts))
    step[(ts >= J - 8) & (ts < J)] = 0.0
    lp = np.log(85_000) + np.cumsum(step)
    pre = lp[ts < J][-1]
    lp[ts > J] += 0.003
    ts = np.append(ts, J)
    lp = np.append(lp, pre + 0.003)
    o = np.argsort(ts, kind="stable")
    spot = pd.DataFrame({"trade_ts": ts[o], "receive_ts": ts[o] + 0.05, "price": np.exp(lp[o])})
    K, H = float(np.exp(pre + 0.0015)), float(np.exp(pre + 0.002))
    markets = pd.DataFrame([
        {"condition_id": "c1", "type": "above", "lo": K, "hi": np.nan, "end_ts": END, "start_ts": np.nan,
         "ref_ts": np.nan, "ref_price": np.nan, "yes_token": "y1", "no_token": "n1", "slug": "above"},
        {"condition_id": "c2", "type": "hit_up", "lo": H, "hi": np.nan, "end_ts": END + 3600, "start_ts": J - 100,
         "ref_ts": np.nan, "ref_price": np.nan, "yes_token": "y2", "no_token": "n2", "slug": "hit"}])
    fee = lambda p: 0.07 * p * (1 - p)  # noqa: E731
    edge = (0.4 - fee(0.6)) * 100 + (0.3 - fee(0.7)) * 50
    rows = [("y1", S0, 0.48, 0.50, 100, 40, 0.01, np.nan, np.nan),
            ("n1", S0, 0.50, 0.52, 40, 100, 0.01, np.nan, np.nan),
            ("y1", J + 0.35, 0.90, 0.92, 30, 60, 0.01, np.nan, np.nan),
            ("n1", J + 0.35, 0.08, 0.10, 60, 30, 0.01, np.nan, np.nan),
            ("y2", J - 100, 0.55, 0.60, 10, 100, 0.01, 150, edge),
            ("y2", J + 0.25, 0.98, 0.995, 500, 20, 0.001, 0, 0)]
    rows += [("hb", float(t), 0.1 + 0.01 * (k % 2), 0.2, 1, 1, 0.01, np.nan, np.nan)
             for k, t in enumerate(np.arange(S0, S0 + 800, 5.0))]  # another token, so the feed shows alive
    books = pd.DataFrame(rows, columns=["market_id", "ts", "bid", "ask", "bid_size", "ask_size", "tick", "depth99",
                                        "edge99"])
    books["recv"] = books["ts"] + 0.1
    books = books.sort_values(["market_id", "ts"], kind="stable").reset_index(drop=True)
    trades = pd.DataFrame({"asset_id": ["y2"], "ts": [J + 0.1], "recv": [J + 0.2], "price": [0.70], "size": [50.0],
                           "side": ["BUY"]})
    return books, markets, spot, trades


def test_reaction_and_markouts(fixture):
    books, markets, spot, trades = fixture
    res = lc.analyze(books, markets, spot, closes=np.array([]), trades=trades)
    js = res["jumps"]
    assert (abs(js["t0"] - J) < 1e-6).sum() == 1
    p = res["pairs"]
    row = p[at_j(p) & (p["type"] == "above")].iloc[0]
    assert row["side"] == "Yes" and row["change"] > 0.9 and row["fair1"] > 0.95
    assert row["first"] == pytest.approx(0.35) and row["mid0"] == pytest.approx(0.49)
    assert (row["d0.3"], row["d0.5"], row["d_end"]) == (0, pytest.approx(0.42), pytest.approx(0.42))
    s = lc.reaction_stats(p[at_j(p) & (p["type"] == "above")])
    assert (s["r0.3"], s["r0.5"], s["moved"]) == (0, 1, 1)
    t = res["takers"]
    t = t[at_j(t) & (t["type"] == "above") & (t["variant"] == "model") & (t["theta"] == 0.05)]
    assert sorted(t["lag"]) == [0.1, 0.15, 0.2, 0.3, 0.5, 1.0]  # fair ~0.99 is 5c above even the new ask
    early = t[t["lag"] <= 0.3]
    fee = 0.07 * 0.5 * 0.5
    assert (early["ask"] == 0.50).all() and (early["size"] == 40).all()
    assert early["mark30"].to_numpy() == pytest.approx(0.91 - 0.50 - fee)
    assert early["mark120"].to_numpy() == pytest.approx(0.91 - 0.50 - fee)
    late = t[t["lag"] >= 0.5]
    assert (late["ask"] == 0.92).all() and late["mark30"].to_numpy() == pytest.approx(0.91 - 0.92 - 0.07 * 0.92 * 0.08)
    # the anchored fair value: the mid 2 s before (0.49) plus the model's change since
    a = res["takers"]
    a = a[at_j(a) & (a["type"] == "above") & (a["variant"] == "anchored") & (a["lag"] == 0.1)]
    assert len(a) == 2 and a["fair"].iloc[0] == pytest.approx(min(0.999, 0.49 + row["fair1"] - row["fair_m2"]))


def test_a_disconnect_around_the_jump_drops_it(fixture):
    books, markets, spot, trades = fixture
    res = lc.analyze(books, markets, spot, closes=np.array([J + 60.0]), trades=trades)
    p = res["pairs"]
    assert not (len(p) and at_j(p).any())


def test_barrier_event(fixture):
    books, markets, spot, trades = fixture
    b = lc.analyze(books, markets, spot, closes=None, trades=trades)["barriers"]
    assert len(b) == 1
    r = b.iloc[0]
    assert r["t"] == pytest.approx(J) and r["below"] == pytest.approx(0.25)
    assert (r["ask-1"], r["ask+0"], r["ask+0.2"], r["ask+0.3"], r["ask+5"]) == (0.60, 0.60, 0.60, 0.995, 0.995)
    assert r["depth"] == 150 and r["trades"] == 1 and r["traded"] == 50
    # the hit market is settled from the jump on: no pair after it
    p = lc.analyze(books, markets, spot)["pairs"]
    assert not ((p["type"] == "hit_up") & (p["jump"] > J + 1)).any()


def test_report_from_built_directories(fixture, tmp_path):
    books, markets, spot, trades = fixture
    d = tmp_path / "rec" / "123" / "built"
    d.mkdir(parents=True)
    books.to_csv(d / "books.csv.gz", index=False)
    trades.to_csv(d / "clob_trades.csv.gz", index=False)
    (d / "markets.json").write_text(markets.replace({np.nan: None}).to_json(orient="records"))
    import gzip
    with gzip.open(d / "binance_trades.jsonl.gz", "wt") as f:
        for r in spot.itertuples():
            f.write(json.dumps({"event": "BINANCE_WS_TRADE", "trade_ts": r.trade_ts, "receive_ts": r.receive_ts,
                                "price": r.price}) + "\n")
    pd.DataFrame({"at_s": []}).to_csv(d / "clob_closes.csv.gz", index=False)
    out = tmp_path / "ladder-check.md"
    lc.main([str(tmp_path / "rec"), "--out", str(out)])
    text = out.read_text()
    assert "录制段 1 个" in text and "标记不是结算盈亏" in text and "| ↑ " in text
    assert "| above（高于） | 1 | 100% | 350 / 350 | 0% | 0% | 0% | 100% | 100% | 100% |" in text
