import json
import math
import urllib.error
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

import ladder as lad
import resolved as R


# ------------------------------------------------------------------ synthetic spot

def make_spot(t0, n, base=7_000_000.0):
    """1 s klines from t0 (a minute boundary) for n seconds, flat at `base` cents."""
    t = np.arange(t0, t0 + n)
    p = np.full(n, base)
    return t, p.copy(), p.copy(), p.copy()


def spot_with(t0, n, bumps=(), base=7_000_000.0):
    t, h, l, c = make_spot(t0, n, base)
    for sec, hi, lo in bumps:
        k = sec - t0
        h[k], l[k] = hi, lo
    return R.Spot.from_arrays(t, h, l, c)


T0 = lad.et_to_utc(date(2026, 6, 3), 0)  # ET midnight (04:00 UTC)


def test_touch_at_minute_close_and_second():
    s = spot_with(T0 - 600, 86400 + 1200, bumps=[(T0 + 3600 + 17, 7_100_000.0, 7_000_000.0)])
    tstar, tsec = R.first_touch(s, T0, T0 + 86400, 7_100_000.0, up=True)
    assert tstar == T0 + 3600 + 60          # close of the minute opening at T0 + 3600
    assert tsec == T0 + 3600 + 17           # the second that crossed
    # level not reached (needs >=; 1 cent short)
    tstar, tsec = R.first_touch(s, T0, T0 + 86400, 7_100_001.0, up=True)
    assert tstar == T0 + 86400 and math.isnan(tsec)


def test_touch_outside_window_ignored_and_down():
    s = spot_with(T0 - 600, 86400 + 1200, bumps=[(T0 - 30, 7_200_000.0, 6_800_000.0),
                                                   (T0 + 7200 + 59, 7_000_000.0, 6_900_000.0)])
    t, _ = R.first_touch(s, T0, T0 + 86400, 7_200_000.0, up=True)
    assert t == T0 + 86400                  # the bump before the window does not count
    t, ts = R.first_touch(s, T0, T0 + 86400, 6_900_000.0, up=False)
    assert t == T0 + 7200 + 60 and ts == T0 + 7200 + 59
    # touch in the last minute of the window: T* = window end, and it is a Yes
    s2 = spot_with(T0 - 600, 86400 + 1200, bumps=[(T0 + 86400 - 5, 7_300_000.0, 7_000_000.0)])
    t, ts = R.first_touch(s2, T0, T0 + 86400, 7_300_000.0, up=True)
    assert t == T0 + 86400 and ts == T0 + 86400 - 5


def test_touch_uses_binance_1m_when_attached():
    s = spot_with(T0 - 600, 86400 + 1200, bumps=[(T0 + 100, 7_100_001.0, 7_000_000.0)])
    # Binance's own 1m kline for that minute says the high was 1 cent lower than the 1 s data
    m = (T0 + 60)
    s.set_minutes([m], [7_100_000.0], [7_000_000.0], [7_000_000.0])
    t, ts = R.first_touch(s, T0, T0 + 86400, 7_100_001.0, up=True)
    assert t == T0 + 86400                   # not a touch on the settlement candles
    t, ts = R.first_touch(s, T0, T0 + 86400, 7_100_000.0, up=True)
    assert t == m + 60


def test_window_not_covered():
    s = spot_with(T0, 3600)
    assert all(math.isnan(x) for x in R.first_touch(s, T0, T0 + 86400, 1.0, up=True))


def test_noon_candle_conventions_and_rules():
    d = date(2026, 7, 1)
    noon = lad.noon_et(d)
    assert noon == 1782921600                # 16:00 UTC in EDT
    assert R.noon_open(d, "open12") == noon and R.noon_open(d, "close12") == noon - 60
    t, h, l, c = make_spot(noon - 3600, 7200)
    c[(noon - 1) - (noon - 3600)] = 6_100_000.0      # last second of the 11:59 candle
    c[(noon + 59) - (noon - 3600)] = 6_200_000.0     # last second of the 12:00 candle
    s = R.Spot.from_arrays(t, h, l, c)
    assert s.close_of(R.noon_open(d, "open12")) == 6_200_000.0
    assert s.close_of(R.noon_open(d, "close12")) == 6_100_000.0
    assert R.rule_outcome("above", 62000.0, lo=62000.0) == 0.0       # strictly higher
    assert R.rule_outcome("above", 62000.01, lo=62000.0) == 1.0
    assert R.rule_outcome("range", 62000.0, lo=62000.0, hi=64000.0) == 1.0   # boundary goes up
    assert R.rule_outcome("range", 64000.0, lo=62000.0, hi=64000.0) == 0.0
    assert R.rule_outcome("range", 10.0, lo=np.nan, hi=52000.0) == 1.0
    assert R.rule_outcome("range", 99000.0, lo=92000.0, hi=np.nan) == 1.0
    assert R.rule_outcome("updown_day", 5.0, ref=5.0) == 0.5
    assert R.rule_outcome("updown_day", 6.0, ref=5.0) == 1.0


# ------------------------------------------------------------------ window parsing

DAILY = ('This market will immediately resolve to "Yes" if any Binance 1-minute candle for Bitcoin (BTC/USDT) on '
         'the date specified in the title, between 12:00 AM ET and 11:59 PM ET has a final "High" price ...')
WEEKLY = ('... during the date range specified in the title (from 12:00 AM ET on the first date to 11:59 PM ET on '
          'the last) has a final "Low" price ...')
MONTHLY = '... during the month specified in the title (from 00:00 AM ET on the first day to 11:59 PM ET on the last) ...'
CREATION = ('... from the creation of this market through 11:59 PM ET on the last day of the month specified in the '
            'title ... Price action before this market\'s creation will not be considered.')


def test_hit_window_daily_weekly_monthly_creation():
    ev = {"title": "What price will Bitcoin hit on June 3?", "endDate": "2026-06-04T04:00:00Z"}
    p, cr, a, b, note = R.hit_window(ev, {"description": DAILY})
    assert (p, cr, note) == ("daily", False, "")
    assert a == lad.et_to_utc(date(2026, 6, 3), 0) and b == lad.et_to_utc(date(2026, 6, 4), 0) == R.ts_of(ev["endDate"])
    ev = {"title": "What price will Bitcoin hit August 31-September 6?", "endDate": "2026-09-07T04:00:00Z"}
    p, cr, a, b, note = R.hit_window(ev, {"description": WEEKLY})
    assert p == "weekly" and a == lad.et_to_utc(date(2026, 8, 31), 0) and b == R.ts_of(ev["endDate"]) and note == ""
    ev = {"title": "What price will Bitcoin hit in March?", "endDate": "2026-04-01T04:00:00Z"}
    p, cr, a, b, note = R.hit_window(ev, {"description": MONTHLY})
    assert p == "monthly" and a == lad.et_to_utc(date(2026, 3, 1), 0) == 1772341200   # EST on March 1
    assert b == R.ts_of(ev["endDate"])
    ev = {"title": "What price will Bitcoin hit in June?", "endDate": "2026-07-01T04:00:00Z"}
    p, cr, a, b, note = R.hit_window(ev, {"description": CREATION, "createdAt": "2026-06-25T15:29:13.880124Z"})
    assert p == "monthly" and cr is True
    assert a == R.ts_of("2026-06-25T15:30:00Z")    # rounded UP to the next whole minute
    # a description of another period is flagged
    _, _, _, _, note = R.hit_window({"title": "What price will Bitcoin hit on June 3?",
                                     "endDate": "2026-06-04T04:00:00Z"}, {"description": MONTHLY})
    assert note


def test_slugs_and_times():
    assert R.daily_slugs(date(2026, 6, 3))["hit_daily"] == ["what-price-will-bitcoin-hit-on-june-3-2026",
                                                            "what-price-will-bitcoin-hit-on-june-3"]
    assert R.week_slugs(date(2026, 6, 29)) == ["what-price-will-bitcoin-hit-june-29-july-5-2026",
                                               "what-price-will-bitcoin-hit-june-29-july-5"]
    s = R.h4_slugs(date(2026, 3, 14), date(2026, 3, 14))
    assert len(s) == 6 and s[0] == f"btc-updown-4h-{lad.et_to_utc(date(2026, 3, 14), 0)}"
    assert R.ts_of("2026-07-01 16:13:41+00") == R.ts_of("2026-07-01T16:13:41Z") == 1782922421
    assert math.isnan(R.ts_of(None))


def test_official_outcome():
    assert R.official_yes({"outcomePrices": '["1", "0"]'}, 0) == 1.0
    assert R.official_yes({"outcomePrices": '["0", "1"]'}, 0) == 0.0
    assert R.official_yes({"outcomePrices": '["0.5", "0.5"]'}, 1) == 0.5
    assert math.isnan(R.official_yes({"outcomePrices": '["0.97", "0.03"]'}, 0))


# ------------------------------------------------------------------ outcome check

def _mk(rows):
    base = {"type": "", "lo": np.nan, "hi": np.nan, "level": np.nan, "ws": np.nan, "we": np.nan, "official": np.nan,
            "end": np.nan, "closed_ts": np.nan, "cid": "", "day": None}
    return pd.DataFrame([{**base, **r} for r in rows])


def test_decide_checks_official_and_picks_convention():
    d = date(2026, 7, 1)
    noon = lad.noon_et(d)
    t, h, l, c = make_spot(lad.et_to_utc(d, 0) - 86400, 2 * 86400 + 600)
    t0 = t[0]
    c[(noon - 1) - t0] = 6_100_000.0       # close12 candle close
    c[(noon + 59) - t0] = 6_200_000.0      # open12 candle close
    h[(lad.et_to_utc(d, 5)) - t0] = 7_100_000.0
    h[(lad.et_to_utc(d + timedelta(days=1), 0) - 2) - t0] = 7_500_000.0   # touch in the window's last minute
    s = R.Spot.from_arrays(t, h, l, c)
    ws, we = lad.et_to_utc(d, 0), lad.et_to_utc(d + timedelta(days=1), 0)
    mk = _mk([
        {"cid": "a", "kind": "above", "type": "above", "lo": 61500.0, "official": 1.0, "day": d.isoformat(), "end": noon},
        {"cid": "b", "kind": "above", "type": "above", "lo": 60000.0, "official": 1.0, "day": d.isoformat(), "end": noon},
        {"cid": "c", "kind": "hit_daily", "type": "hit_up", "level": 71000.0, "ws": ws, "we": we, "official": 1.0, "end": we},
        {"cid": "d", "kind": "hit_daily", "type": "hit_up", "level": 80000.0, "ws": ws, "we": we, "official": 1.0, "end": we},
        {"cid": "e", "kind": "above", "type": "above", "lo": 1.0, "official": np.nan, "day": d.isoformat(), "end": noon},
        {"cid": "f", "kind": "hit_daily", "type": "hit_up", "level": 75000.0, "ws": ws, "we": we, "official": 1.0, "end": we},
        {"cid": "g", "kind": "hit_daily", "type": "hit_up", "level": 75000.01, "ws": ws, "we": we, "official": 0.0, "end": we},
    ])
    out, conv, mism = R.decide(mk, s)
    assert conv == "open12" and mism == {"open12": 0, "close12": 1}
    o = out.set_index("cid")
    assert o.loc["a", "tstar"] == noon + 60 and o.loc["a", "excluded"] == ""
    assert o.loc["c", "tstar"] == lad.et_to_utc(d, 5) + 60 and o.loc["c", "excluded"] == ""
    assert o.loc["d", "excluded"] == "mismatch"          # computed No, official Yes -> excluded and listed
    assert o.loc["e", "excluded"] == "unresolved"
    assert o.loc["f", "tstar"] == we and o.loc["f", "comp"] == 1.0 and o.loc["f", "excluded"] == ""
    assert o.loc["g", "tstar"] == we and o.loc["g", "comp"] == 0.0 and o.loc["g", "excluded"] == ""


# ------------------------------------------------------------------ model

def test_model_probability_monotone():
    S = np.linspace(6_800_000, 7_200_000, 41)
    pa = [R.p_yes("above", x, 7_000_000.0, np.nan, 0.01) for x in S]
    assert np.all(np.diff(pa) > 0) and abs(R.p_yes("above", 7e6, 7e6, np.nan, 0.01) - 0.5) < 1e-12
    pk = [R.p_yes("above", 7e6, K, np.nan, 0.01) for K in S]
    assert np.all(np.diff(pk) < 0)
    sds = np.linspace(0.001, 0.05, 30)
    ph = [R.p_yes("hit_up", 7e6, 7.2e6, np.nan, sd) for sd in sds]
    assert np.all(np.diff(ph) > 0) and all(0 <= x <= 1 for x in ph)
    assert R.p_yes("hit_up", 7.3e6, 7.2e6, np.nan, 0.01) == 1.0
    pd_ = [R.p_yes("hit_down", x, 6.8e6, np.nan, 0.01) for x in S[S > 6.8e6]]
    assert np.all(np.diff(pd_) < 0)
    # touch prob is twice the terminal prob (reflection) when far
    assert abs(R.p_yes("hit_up", 7e6, 7.2e6, np.nan, 0.01) - 2 * R.p_yes("above", 7e6, 7.2e6, np.nan, 0.01)) < 1e-12
    r = R.p_yes("range", 7e6, 6.9e6, 7.1e6, 0.01)
    assert 0 < r < 1 and r > R.p_yes("range", 7e6, 7.1e6, 7.3e6, 0.01)
    assert R.p_yes("updown_day", 7e6, np.nan, np.nan, 0.01, ref=6.9e6) > 0.5


def test_sigma_and_price_before():
    t = np.arange(0, 7200)
    rng = np.random.default_rng(1)
    c = 7e6 * np.exp(np.cumsum(rng.normal(0, 1e-4, len(t))))
    s = R.Spot.from_arrays(t, c, c, c)
    sig = s.sigma(7200)
    lr = np.diff(np.log(c))[-3600:]
    assert abs(sig - lr.std(ddof=1)) < 1e-12
    assert s.price_before(100) == c[99]


# ------------------------------------------------------------------ pagination with a fake API

class FakeTrades:
    """data-api /trades: newest first, limit / offset / start / end (inclusive), offset cap 10000."""

    def __init__(self, ts, cap=10000):
        self.rows = sorted(([int(x), "BUY", "tok", 0.5, 1.0, 0, f"0x{i:06d}", "w"] for i, x in enumerate(ts)),
                           key=lambda r: (-r[0], r[6]))
        self.cap, self.calls = cap, 0

    def __call__(self, url):
        self.calls += 1
        q = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))
        lim, off = int(q["limit"]), int(q.get("offset", 0))
        if off > self.cap:
            raise urllib.error.HTTPError(url, 400, "max historical trades offset of 10000 exceeded", None, None)
        rs = [r for r in self.rows if r[0] >= int(q.get("start", -1)) and r[0] <= int(q.get("end", 10 ** 12))]
        return [list(r) for r in rs[off:off + lim]]


def test_pagination_beyond_offset_cap():
    ts = np.repeat(np.arange(1000, 1000 + 2500), 10)          # 25,000 trades, 10 per second
    api = FakeTrades(ts)
    recs, info = R.fetch_trades(api, "0xabc", start=0)
    assert len(recs) == 25_000 and len({r[6] for r in recs}) == 25_000
    assert info["windows"] >= 3 and not info["truncated"]
    recs, info = R.fetch_trades(api, "0xabc", start=3000)
    assert sorted({r[0] for r in recs}) == list(range(3000, 3500)) and len(recs) == 5000 and info["windows"] == 1


def test_pagination_small_pages_and_truncation():
    api = FakeTrades([6] * 5 + [5] * 12 + [4] * 3)
    recs, info = R.fetch_trades(api, "x", start=0, page=10, max_offset=10)
    assert len(recs) == 20 and not info["truncated"] and info["windows"] == 2
    api = FakeTrades([5] * 40 + [4] * 3)                      # 40 in one second, at most 30 reachable
    recs, info = R.fetch_trades(api, "x", start=0, page=10, max_offset=20)
    assert info["truncated"] and len(recs) == 33 and sum(r[0] == 4 for r in recs) == 3


def test_identical_records_kept_by_multiplicity():
    class Dup(FakeTrades):
        def __call__(self, url):
            return [[7, "SELL", "tok", 0.999, 5.0, 0, "0xa", "w"]] * 3
    recs, _ = R.fetch_trades(Dup([]), "x", start=0, page=10)
    assert len(recs) == 3


def test_http_cache_retry_and_rate(tmp_path):
    calls, sleeps = [], []

    def opener(url):
        calls.append(url)
        if len(calls) == 1:
            raise urllib.error.HTTPError(url, 503, "busy", None, None)
        return [{"timestamp": 1, "side": "BUY", "asset": "t", "price": 0.5, "size": 2, "extra": "x"}]

    clock = iter(np.arange(0, 100, 0.01))
    h = R.Http(tmp_path, rate=4, opener=opener, sleep=sleeps.append, clock=lambda: next(clock))
    a = h.get("https://x/trades?market=1", slim=R.slim_trades)
    b = h.get("https://x/trades?market=1", slim=R.slim_trades)
    assert a == b == [[1, "BUY", "t", 0.5, 2, None, None, None]]
    assert len(calls) == 2 and h.hits == 1 and any(s >= 1.5 for s in sleeps)
    with pytest.raises(urllib.error.HTTPError):
        R.Http(tmp_path, opener=lambda u: (_ for _ in ()).throw(urllib.error.HTTPError(u, 404, "nf", None, None)),
               sleep=lambda s: None).get("https://x/none")


# ------------------------------------------------------------------ measurements

def test_post_tstar_lags_and_near_table():
    mk = _mk([{"cid": "m1", "kind": "above", "type": "above", "official": 1.0, "tstar": 1000.0, "tsec": np.nan,
               "closed_ts": 4600.0, "end": 940.0, "slug": "s1", "excluded": ""},
              {"cid": "m2", "kind": "hit_daily", "type": "hit_up", "official": 0.0, "tstar": 2000.0, "tsec": np.nan,
               "closed_ts": 2600.0, "end": 2000.0, "slug": "s2", "excluded": ""}])
    tr = pd.DataFrame({"cid": ["m1"] * 4 + ["m2"] * 2, "ts": [999, 1000, 1004, 1060, 2001, 2100],
                       "taker_buy": [False, False, True, False, False, True],
                       "is_yes": [True, True, True, False, False, True],
                       "price": [0.99, 0.999, 0.995, 0.002, 0.99, 0.01], "size": [10, 100, 10, 50, 20, 5]})
    pt = R.post_tstar(tr, mk)
    w = pt[pt["win"] & (pt["price"] < 1)]
    assert set(w["ts"]) == {1000, 1004, 2001}                 # 999 is before T*; losers out
    lt = R.lag_table(w, mk)
    r = lt[(lt["kind"] == "all") & (lt["L"] == 1)].iloc[0]
    assert r["trades"] == 2 and abs(r["profit"] - (0.005 * 10 + 0.01 * 20)) < 1e-12
    assert abs(r["sell_profit"] - 0.01 * 20) < 1e-12
    assert abs(w.loc[w["ts"] == 1000, "hours"].iloc[0] - 1.0) < 1e-12
    # near-certain: checkpoint 1 minute before end of m1, favoured Yes
    cp = pd.DataFrame([{"cid": "m1", "kind": "above", "check": 1, "t": 880, "anchor": 940, "p_yes": 0.995,
                        "fav_yes": True, "p_fav": 0.995, "sigma": 1e-4, "tau": 120}])
    tr2 = pd.DataFrame({"cid": ["m1"] * 3, "ts": [879, 880, 939], "taker_buy": [True, False, True],
                        "is_yes": [True, True, False], "price": [0.9, 0.99, 0.01], "size": [1, 10, 5]})
    tab, by_kind, j = R.near_table(cp, tr2, mk)
    assert list(j["ts"]) == [880]                              # 879 before t, 939 is the other token
    row = tab[(tab["group"] == "all") & (tab["bucket"] == ">=0.99")].iloc[0]
    assert abs(row["pnl"] - 0.01) < 1e-12 and row["trades"] == 1


def test_clustered_mean():
    x = np.array([0.01, 0.01, -0.99, 0.01])
    w = np.ones(4)
    mu, se, t, G = R.clustered_mean(x, w, ["a", "a", "b", "c"])
    assert G == 3 and abs(mu - x.mean()) < 1e-12 and se > 0
    mu, se, t, G = R.clustered_mean([0.1], [1], ["a"])
    assert G == 1 and math.isnan(se)


# ------------------------------------------------------------------ verdict frame (review fixes)

def _w(rows):
    """Winning-token trades as post_tstar returns them (only the columns daily_dollars reads)."""
    base = {"dt": 10.0, "price": 0.99, "size": 100.0, "taker_buy": False, "after_close": False}
    w = pd.DataFrame([{**base, **r} for r in rows])
    w["notional"] = w["price"] * w["size"]
    w["profit"] = (1 - w["price"]) * w["size"]
    w["fee"] = R.FEE * w["price"] * (1 - w["price"]) * w["size"]
    w["hours"] = 1.0
    return w


def test_daily_dollars_uses_binance_kinds_period_days_and_unsettled_trades_only():
    first, last = date(2026, 3, 14), date(2026, 3, 16)
    w = _w([{"kind": "above", "day": "2026-03-14"},                              # counted
            {"kind": "above", "day": "2026-03-14", "taker_buy": True},          # counted (buy part)
            {"kind": "updown_4h", "day": "2026-03-15"},                         # Chainlink control: never
            {"kind": "hit_weekly", "day": "2026-03-10"},                        # before the period: apart
            {"kind": "range", "day": "2026-03-16", "after_close": True},        # already settled: never
            {"kind": "range", "day": "2026-03-16", "price": 0.999},             # above the 0.995 cap
            {"kind": "range", "day": "2026-03-16", "dt": 2.0}])                 # before T* + 5 s
    dd = R.daily_dollars(w, first=first, last=last)
    assert list(dd.index) == ["2026-03-14", "2026-03-15", "2026-03-16"]          # every period day, no other
    assert dd["notional"].tolist() == pytest.approx([198.0, 0.0, 0.0])
    assert dd["sell_notional"].tolist() == pytest.approx([99.0, 0.0, 0.0])
    assert dd["buy_profit"].tolist() == pytest.approx([1.0, 0.0, 0.0])
    st = R.day_stats(dd, "notional")
    assert st["mean"] == pytest.approx(66.0) and st["median"] == 0.0 and st["ge"] == 1 and st["n"] == 3
    c = R.daily_dollars(w, first=first, last=last, kinds=("updown_4h",))
    assert c["notional"].tolist() == pytest.approx([0.0, 99.0, 0.0])


def test_post_tstar_flags_after_close_period_and_risk():
    t0 = lad.et_to_utc(date(2026, 3, 14), 13)
    mk = _mk([{"cid": "m1", "kind": "above", "type": "above", "official": 1.0, "tstar": t0, "tsec": np.nan,
               "closed_ts": t0 + 600.5, "end": t0 - 60, "slug": "s1", "excluded": "", "risk": "noon_conv"}])
    tr = pd.DataFrame({"cid": ["m1"] * 3, "ts": [t0 + 10, t0 + 600, t0 + 601], "taker_buy": [False] * 3,
                       "is_yes": [True] * 3, "price": [0.99] * 3, "size": [1.0] * 3})
    pt = R.post_tstar(tr, mk, first=date(2026, 3, 14), last=date(2026, 3, 14))
    assert pt["after_close"].tolist() == [False, False, True]                  # stamped after closedTime
    assert pt["in_period"].all() and (pt["risk"] == "noon_conv").all()


def test_risk_flags_and_lag_table_all_row_is_binance_only():
    mk = _mk([{"cid": "a", "kind": "above", "comp_open12": 1.0, "comp_close12": 0.0, "excluded": "", "official": 1.0},
              {"cid": "b", "kind": "above", "comp_open12": 1.0, "comp_close12": 1.0, "excluded": "", "official": 1.0},
              {"cid": "c", "kind": "hit_daily", "comp": 1.0, "tstar": 1000.0, "created": 2000.0, "excluded": "",
               "official": 1.0},
              {"cid": "d", "kind": "hit_daily", "comp": 1.0, "tstar": 1000.0, "created": 2000.0, "excluded": "mismatch",
               "official": 0.0},
              {"cid": "e", "kind": "updown_4h", "excluded": "", "official": 1.0}])
    assert R.risk_flags(mk).tolist() == ["noon_conv", "", "pre_created", "", ""]
    w = _w([{"kind": "above", "cid": "a"}, {"kind": "updown_4h", "cid": "e"}])
    lt = R.lag_table(w, mk)
    r = lt[(lt["kind"] == "all") & (lt["L"] == 5)].iloc[0]
    assert r["trades"] == 1 and r["mk"] == 3                                    # the 4h control is its own row


def test_t_cell_needs_clusters_and_marks_no_losses():
    assert R.t_cell(325.07, 2, 0) == "–"                    # two clusters: no t
    assert R.t_cell(4.0, 25, 0) == "4.00†"                  # no losing cluster: price dispersion only
    assert R.t_cell(-0.48, 2409, 24) == "-0.48"


def test_calibration_counts_distinct_markets_and_days():
    d1, d2 = lad.noon_et(date(2026, 4, 9)), lad.noon_et(date(2026, 4, 10))
    mk = _mk([{"cid": c, "kind": "above", "official": o, "event_slug": e} for c, o, e in
              (("a", 0.0, "e1"), ("b", 0.0, "e1"), ("c", 1.0, "e2"), ("h", 0.0, "e3"))])
    cp = pd.DataFrame([{"cid": c, "kind": k, "anchor": a, "fav_yes": True, "p_fav": p} for c, k, a, p in
                       (("a", "above", d1, 0.999), ("a", "above", d1, 0.995), ("b", "above", d1, 0.999),
                        ("c", "above", d2, 0.999), ("h", "updown_4h", d2, 0.999))])
    cal = R.calibration(cp, mk)
    assert cal["points"] == 4 and cal["fail_pts"] == 3 and cal["fail_mk"] == 2 and cal["fail_ev"] == 1
    assert cal["fail_days"] == 1 and cal["days"] == 2 and cal["expected"] == pytest.approx(0.001 * 3 + 0.005)
