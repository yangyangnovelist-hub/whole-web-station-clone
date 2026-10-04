import io
import math
import zipfile
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

import ladder as lad
import nearcert as N
import resolved as R

# ------------------------------------------------------------------ descriptions (Gamma wording)

ABOVE = ('This market will resolve to "Yes" if the Binance 1 minute candle for BTC/USDT 12:00 in the ET timezone (noon) '
         'on the date specified in the title has a final “Close” price higher than the price specified in the title. '
         'Otherwise, this market will resolve to "No".\n\nThe resolution source for this market is Binance, specifically '
         'the BTC/USDT "Close" prices currently available at https://www.binance.com/en/trade/BTC_USDT with “1m” and '
         '“Candles” selected on the top bar.')
RANGE = ('This market will resolve according to the final "Close" price of the Binance 1 minute candle for BTC/USDT 12:00 '
         'in the ET timezone (noon) on the date specified in the title. Otherwise, this market will resolve to "No".\n\n'
         'The resolution source for this market is Binance, specifically the BTC/USDT "Close" prices currently available '
         'at https://www.binance.com/en/trade/BTC_USDT with "1m" and "Candles" selected on the top bar.\n\nIf the reported '
         'value falls exactly between two brackets, then this market will resolve to the higher range bracket.')


def updown(a, b):
    return (f'This market will resolve to "Up" if the "Close" price for the Binance 1 minute candle for BTC/USDT {a} 12:00 '
            f'in the ET timezone (noon) is lower than the final "Close" price for the {b} 12:00 ET candle.\n\nThis market '
            f'will resolve to "Down" if ... If the final "Close" price for both of these candles is exactly equal on Binance, '
            f'this market will resolve 50-50.\n\nThe resolution source for this market is Binance, specifically the BTC/USDT '
            f'"Close" prices currently available at https://www.binance.com/en/trade/BTC_USDT with "1m" and "Candles" '
            f'selected on the top bar.')


def hit(field, period):
    cmp_ = "equal to or greater than" if field == "High" else "equal to or lower than"
    where = {"day": "on the date specified in the title, between 12:00 AM ET and 11:59 PM ET",
             "week": "during the date range specified in the title (from 12:00 AM ET on the first date to 11:59 PM ET on "
                     "the last)",
             "month": "during the month specified in the title (from 00:00 AM ET on the first day to 11:59 PM ET on the "
                      "last),"}[period]
    return (f'This market will immediately resolve to "Yes" if any Binance 1-minute candle for Bitcoin (BTC/USDT) {where} '
            f'has a final "{field}" price {cmp_} the price specified in the title. Otherwise, this market will resolve to '
            f'"No".\n\nThe resolution source for this market is Binance, specifically the BTC/USDT "{field}" prices '
            f'available at https://www.binance.com/en/trade/BTC_USDT, with the chart settings on "1m" candles selected on '
            f'the top bar.')


H4 = ('This market will resolve to "Up" if the Bitcoin price at the end of the time range specified in the title is greater '
      'than or equal to the price at the beginning of that range. Otherwise, it will resolve to "Down".\nThe resolution '
      'source for this market is information from Chainlink, specifically the BTC/USD data stream available at '
      'https://data.chain.link/streams/btc-usd.')


def reason(kind, desc, typ=None, day=None):
    return N.rule_reason(kind, N.rule_sig(kind, desc, typ), typ, day)


# ------------------------------------------------------------------ the kind / rule filter

def test_2026_rules_match():
    assert reason("above", ABOVE) == ""
    assert reason("range", RANGE) == ""
    assert reason("updown_day", updown("Sep 14 '25", "Sep 15 '25"), day="2025-09-15") == ""
    assert reason("updown_day", updown("3 Nov '25", "4 Nov '25"), day="2025-11-04") == ""   # day-month spelling
    assert reason("hit_daily", hit("High", "day"), "hit_up") == ""
    assert reason("hit_weekly", hit("Low", "week"), "hit_down") == ""
    assert reason("hit_monthly", hit("High", "month"), "hit_up") == ""
    assert reason("updown_4h", H4) == ""
    creation = ('This market will immediately resolve to "Yes" if any Binance 1 minute candle for BTC/USDT during the '
                'month specified in the title (from the creation of this market to April 30, 11:59 PM ET), has a final '
                'High price equal to or greater than the price specified in the title. The resolution source ... '
                'BTC/USDT High prices ... with the chart settings on "1m" for one-minute candles')
    assert reason("hit_monthly", creation, "hit_up") == ""


def test_other_rules_are_excluded():
    # another source / pair / candle / time / comparison
    assert "source=binance+chainlink" in reason("above", ABOVE.replace("Binance", "Chainlink"))   # URL still binance
    assert "source=chainlink" in reason("above", ABOVE.replace("Binance", "Chainlink").replace("binance.com", "chain.link"))
    assert "candle=1h" in reason("above", ABOVE.replace("1 minute candle", "1 hour candle").replace("“1m”", "“1h”"))
    assert "time=" in reason("above", ABOVE.replace("12:00 in the ET timezone (noon)", "4:00 PM ET"))
    assert "cmp=>=" in reason("above", ABOVE.replace("higher than the price", "equal to or higher than the price"))
    assert "pair=" in reason("above", ABOVE.replace("BTC/USDT", "BTC/USD").replace("BTC_USDT", "BTC-USD"))
    assert "cmp=tie_down" in reason("range", RANGE.replace("higher range bracket", "lower range bracket"))
    # up/down: the reference must be the previous day's noon, of the market's own date
    assert "ref=gap2d" in reason("updown_day", updown("Sep 13 '25", "Sep 15 '25"), day="2025-09-15")
    assert "date=2025-09-15" in reason("updown_day", updown("Sep 14 '25", "Sep 15 '25"), day="2025-09-16")
    # hit: the field must match the market's direction, the window the kind's period
    assert "field=low" in reason("hit_weekly", hit("Low", "week"), "hit_up")
    assert "window=week" in reason("hit_monthly", hit("High", "week"), "hit_up")
    feb6 = ('This market will immediately resolve to "Yes" if any Binance 1 minute candle for BTC/USDT between February 6 '
            '11AM ET and February 28 11:59PM ET, has a final High price equal to or greater than the price specified in the '
            'title. The resolution source for this market is Binance, specifically the BTC/USDT High prices ... "1m"')
    assert reason("hit_monthly", feb6, "hit_up").startswith("window=february 6 11am et..february 28 11:59pm et")
    yearly = ('This market will immediately resolve to "Yes" if any Binance 1 minute candle for Bitcoin (BTCUSDT) between '
              'December 30, 2024, 20:00 and December 31, 2025, 23:59 in the ET timezone has a final "High" price of '
              '$1,000,000 or higher. The resolution source ... BTCUSDT "High" prices ... "1m"')
    r = reason("hit_other", yearly, "hit_up")
    assert "december 30, 2024" in r and "no such kind" in r
    assert reason("above", "") == "no description"
    assert "cmp=twap>=start" in reason("updown_4h", H4.replace("the Bitcoin price at the end of", "the time-weighted "
                                                                  "average price (TWAP) of Bitcoin, generated by Chainlink, of"))


def test_rule_check_and_table():
    mk = pd.DataFrame({"kind": ["above", "above", "range", "hit_other"], "type": ["above", "above", "range", "hit_up"],
                       "description": [ABOVE, ABOVE.replace("Binance", "Coinbase"), RANGE, "between May 1, 2025 and "
                                       "December 31, 2025 has a final \"High\" price ... binance btc/usdt 1m"],
                       "day": ["2025-10-01", "2025-10-01", "2025-11-02", "2025-12-31"],
                       "event_slug": ["e1", "e1", "e2", "e3"], "slug": ["a", "b", "c", "d"]})
    out = N.rule_check(mk)
    assert list(out["rule"] == "") == [True, False, True, False]
    out["month"] = out["day"].str[:7]
    tab = N.rule_table(out).set_index(["kind", "month"])
    assert tab.loc[("above", "2025-10"), "ok"] == 1 and tab.loc[("above", "2025-10"), "markets"] == 2
    assert "e.g. b" in tab.loc[("above", "2025-10"), "other"]


# ------------------------------------------------------------------ klines: us vs ms

def _zip(path, rows):
    buf = io.StringIO()
    for r in rows:
        buf.write(",".join(str(x) for x in r) + "\n")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(path.stem + ".csv", buf.getvalue())


def test_kline_units_and_build_spot(tmp_path):
    d = date(2025, 10, 1)
    t0 = int(datetime(2025, 10, 1, tzinfo=timezone.utc).timestamp())
    (tmp_path / "klines").mkdir()
    f = tmp_path / "klines" / f"BTCUSDT-1s-{d}.zip"
    _zip(f, [(t0 * 1_000_000 + k * 1_000_000, 1, 114000.5 + k, 113999.0, 114000.25, 1) for k in range(120)])
    assert N.kline_unit(f) == "us"
    g = tmp_path / "ms.zip"
    _zip(g, [(t0 * 1000, 1, 2, 3, 4, 5)])
    assert N.kline_unit(g) == "ms"
    t, h, l, c = R.read_kline_zip(g)
    assert t[0] == t0
    spot, missing, units = N.build_spot(tmp_path, None, first=d, last=d, log=lambda *a: None)
    assert units == {"us": 1} and missing == []
    assert spot.sec0 == t0 and spot.hi[5] == 11400050 + 500 and spot.cl[0] == 11400025
    assert math.isnan(spot.cl[200])
    # cached arrays give the same Spot
    spot2, _, units2 = N.build_spot(tmp_path, None, first=d, last=d)
    assert units2 == units and np.array_equal(np.nan_to_num(spot2.hi), np.nan_to_num(spot.hi))


# ------------------------------------------------------------------ fixture: computation equals resolved.py's

D = date(2026, 6, 3)
NOON = lad.noon_et(D)
MID = lad.et_to_utc(D, 0)


def fixture(seed=7):
    rng = np.random.default_rng(seed)
    t0 = lad.et_to_utc(date(2026, 6, 2), 8)           # covers yesterday's noon and the hour before it
    n = int(lad.et_to_utc(date(2026, 6, 4), 2) - t0)
    lr = rng.normal(0, 1.2e-4, n)
    p = 7_000_000.0 * np.exp(np.cumsum(lr))
    c = np.round(p)
    h = c + np.round(rng.uniform(0, 300, n))
    lo = c - np.round(rng.uniform(0, 300, n))
    spot = R.Spot.from_arrays(np.arange(t0, t0 + n), h, lo, c)
    rows = []

    def add(kind, typ, desc, **kw):
        k = len(rows)
        base = {"kind": kind, "type": typ, "description": desc, "cid": f"c{k}", "slug": f"m{k}", "event_slug": f"e-{kind}",
                "yes_token": f"y{k}", "no_token": f"n{k}", "level": np.nan, "lo": np.nan, "hi": np.nan, "ws": np.nan,
                "we": np.nan, "end": float(NOON), "closed_ts": float(NOON + 3600), "created": float(MID - 86400 * 3),
                "day": D.isoformat(), "official": np.nan}
        base.update(kw)
        rows.append(base)

    S = spot.close_of(NOON) / 100        # dollars, near where the strikes must be to land in the buckets
    S60 = spot.price_before(NOON - 3600) / 100
    for k in range(-12, 13):
        add("above", "above", ABOVE, lo=round(S60 * (1 + 0.0012 * k), 2))
    for k in range(-6, 6):
        add("range", "range", RANGE, lo=round(S60 * (1 + 0.003 * k), 2), hi=round(S60 * (1 + 0.003 * (k + 1)), 2))
    add("range", "range", RANGE, lo=np.nan, hi=round(S60 * 0.982, 2))
    add("updown_day", "updown_day", updown("Jun 2 '26", "Jun 3 '26"))
    dmax = spot.Hs[(MID // 60 - spot.m0):((MID + 86400) // 60 - spot.m0)].max() / 100
    dmin = spot.Ls[(MID // 60 - spot.m0):((MID + 86400) // 60 - spot.m0)].min() / 100
    for k in range(-4, 5):
        add("hit_daily", "hit_up", hit("High", "day"), level=round(dmax * (1 + 0.001 * k), 0), ws=float(MID),
            we=float(MID + 86400), end=float(MID + 86400), closed_ts=float(MID + 86400 + 1800))
        add("hit_daily", "hit_down", hit("Low", "day"), level=round(dmin * (1 - 0.001 * k), 0), ws=float(MID),
            we=float(MID + 86400), end=float(MID + 86400), closed_ts=float(MID + 86400 + 1800))
    for a in (NOON - 4 * 3600, NOON):
        add("updown_4h", "updown_4h", H4, ws=float(a), we=float(a + 4 * 3600), end=float(a + 4 * 3600))
    # a market with another rule: the near table must not see it
    add("above", "above", ABOVE.replace("Binance", "Coinbase"), lo=round(S60 * 0.999, 2))
    mk = pd.DataFrame(rows)
    # official outcomes = what the 2026 rule gives on this path (open12 convention)
    dec, conv, _ = R.decide(mk, spot)
    assert conv == "open12"
    off = np.where(mk["kind"].isin(R.NOON_KINDS), dec["comp_open12"], dec["comp_hit"])
    mk["official"] = off
    mk.loc[mk["kind"] == "updown_4h", "official"] = dec.loc[mk["kind"] == "updown_4h", "comp_hit"].to_numpy()
    # trades: inside and outside each checkpoint's 60 s, both tokens, both taker sides
    tr = []
    for r in mk.itertuples():
        anchor = r.we if r.kind in R.HIT_KINDS or r.kind == "updown_4h" else r.end
        for c in (60, 30, 10, 5, 1, 45, 2):
            t = int(anchor - 60 * c)
            for _ in range(3):
                ts = t + int(rng.integers(-20, 80))
                tr.append({"cid": r.cid, "ts": ts, "taker_buy": bool(rng.random() < 0.5), "is_yes": bool(rng.random() < 0.5),
                           "price": float(np.round(rng.uniform(0.85, 0.999), 3)), "size": float(rng.integers(5, 500))})
    trades = R.typed_trades(pd.DataFrame(tr))
    return mk, spot, trades


def test_fixture_reaches_both_buckets():
    mk, spot, trades = fixture()
    m2, conv, mism = N.decide_checked(mk, spot)
    cp, near, near_kind, j, mon = N.measure(m2, spot, trades)
    assert {">=0.99", "0.95-0.99"} <= set(near["bucket"])
    a = near[(near["group"] == "all")].set_index("bucket")
    assert a.loc["0.95-0.99", "mk_tr"] >= 3 and a.loc[">=0.99", "mk_tr"] >= 3


def test_computation_equals_resolved():
    mk, spot, trades = fixture()
    ours, conv, mism = N.decide_checked(mk, spot)
    cp, near, near_kind, j, mon = N.measure(ours, spot, trades)
    # resolved.py's own path (as resolved.run) on the markets the rule check keeps
    keep = mk[~mk["description"].str.contains("Coinbase")].reset_index(drop=True)
    dec, conv2, mism2 = R.decide(keep, spot)
    dec["ref"] = np.where(dec["kind"] == "updown_day", dec[f"ref_{conv2}"], np.nan)
    cp2 = R.checkpoints(dec, spot)
    near2, kind2, j2 = R.near_table(cp2, trades, dec)
    assert (conv, mism) == (conv2, mism2)
    key = ["cid", "check"]
    pd.testing.assert_frame_equal(cp.sort_values(key).reset_index(drop=True), cp2.sort_values(key).reset_index(drop=True),
                                  check_like=True)
    pd.testing.assert_frame_equal(near.reset_index(drop=True), near2.reset_index(drop=True))
    pd.testing.assert_frame_equal(near_kind.reset_index(drop=True), kind2.reset_index(drop=True))
    # the excluded market is in our frame, marked, and in no checkpoint
    x = ours[ours["description"].str.contains("Coinbase")]
    assert len(x) == 1 and x["excluded"].iloc[0].startswith("rule: source=binance+coinbase")
    assert x["cid"].iloc[0] not in set(cp["cid"])


def test_month_table_matches_pooled_rows():
    mk, spot, trades = fixture()
    m2, _, _ = N.decide_checked(mk, spot)
    cp, near, near_kind, j, mon = N.measure(m2, spot, trades)
    cols = ["points", "markets", "model", "win_all", "pts_tr", "mk_tr", "trades", "shares", "win_tr", "vwap", "pnl",
            "se", "t", "pnl_taker", "t_taker", "pnl_sell", "t_sell", "losses", "loss_pts", "exp_fail", "mk_sell",
            "losses_sell"]
    for b in (N.TEST, N.REF):
        a = near[(near["bucket"] == b) & (near["group"] == "all")][cols].reset_index(drop=True)
        m = mon[(mon["bucket"] == b) & (mon["month"] == "all")][cols].reset_index(drop=True)
        pd.testing.assert_frame_equal(a, m, check_dtype=False)
        one = mon[(mon["bucket"] == b) & (mon["month"] == "2026-06")][cols].reset_index(drop=True)
        pd.testing.assert_frame_equal(a, one, check_dtype=False)    # a single month = the pooled row


def test_month_split_and_stat_row_cluster():
    # two months: shares add up; the clustered t is resolved.clustered_mean's
    cp = pd.DataFrame({"cid": ["a", "b", "c"], "kind": "above", "check": 5, "t": [1760000000, 1763000000, 1763000100],
                       "anchor": 0, "p_yes": [0.97, 0.96, 0.03], "fav_yes": [True, True, False],
                       "p_fav": [0.97, 0.96, 0.97], "sigma": 1e-4, "tau": 300.0})
    mk = pd.DataFrame({"cid": ["a", "b", "c"], "official": [1.0, 0.0, 0.0]})
    cpb = N.bucket_points(cp, mk)
    assert list(cpb["won"]) == [1.0, 0.0, 1.0] and list(cpb["month"]) == ["2025-10", "2025-11", "2025-11"]
    j = cpb.merge(pd.DataFrame({"cid": ["a", "a", "b", "c"], "check": 5, "ts": 0, "taker_buy": [True, False, False, True],
                                "is_yes": True, "price": [0.97, 0.96, 0.95, 0.9], "size": [10.0, 30.0, 20.0, 5.0]}),
                  on=["cid", "check"])
    j["pnl"] = j["won"] - j["price"]
    j["pnl_taker"] = j["pnl"] - R.FEE * j["price"] * (1 - j["price"])
    mon = N.month_table(cp, j, mk)
    t = mon[mon["bucket"] == N.TEST].set_index("month")
    assert t.loc["2025-10", "shares"] + t.loc["2025-11", "shares"] == t.loc["all", "shares"] == 65.0
    mu, se, tt, G = R.clustered_mean(j["pnl"], j["size"], j["cid"])
    assert t.loc["all", "pnl"] == pytest.approx(mu) and t.loc["all", "mk_tr"] == G == 3
    assert t.loc["all", "losses"] == 1 and t.loc["2025-11", "loss_pts"] == 1
    s = j[~j["taker_buy"]]
    assert t.loc["all", "pnl_sell"] == pytest.approx(R.clustered_mean(s["pnl"], s["size"], s["cid"])[0])


# ------------------------------------------------------------------ verdict

def _near(pnl, t, mk_tr, taker=None):
    return pd.DataFrame([{"bucket": N.TEST, "group": "all", "pnl": pnl, "t": t, "mk_tr": mk_tr,
                          "pnl_taker": pnl if taker is None else taker, "t_taker": t}])


def test_verdict_rules():
    assert N.verdict(_near(0.01, 2.5, 250))[0] is True
    assert N.verdict(_near(0.01, 2.5, 199))[0] is False        # fewer than 200 markets
    assert N.verdict(_near(0.01, 1.99, 250))[0] is False       # t < 2
    assert N.verdict(_near(-0.001, 3.0, 250))[0] is False      # not > 0
    assert N.verdict(_near(float("nan"), float("nan"), 0))[0] is False
    ok, _, text = N.verdict(_near(0.002, 2.1, 300, taker=-0.001))
    assert ok and "吃单扣费后" in text
    assert N.verdict(pd.DataFrame())[0] is False


def test_h4_et_slugs_cover_est_windows():
    s = N.h4_slugs_et(date(2025, 12, 10), date(2025, 12, 10))
    assert s[0] == "btc-updown-4h-1765342800"      # 00:00 EST = 05:00 UTC (the series' winter boundary)
    assert len(s) == 6 and all(int(x.rsplit("-", 1)[1]) % 3600 == 0 for x in s)
    s = N.h4_slugs_et(date(2025, 10, 10), date(2025, 10, 10))
    assert s[0] in R.h4_slugs(date(2025, 10, 10), date(2025, 10, 10))   # EDT: same as resolved.py's


def test_report_smoke(tmp_path):
    mk, spot, trades = fixture()
    mk = N.rule_check(mk)
    mk["month"] = mk["day"].str[:7]
    rule_tab = N.rule_table(mk)
    m2, conv, mism = N.decide_checked(mk, spot)
    tabs = N.tables(m2, spot, trades, first=D, last=D)
    cov, missing = R.coverage(m2, first=D, last=D)
    ctx = dict(tabs, mk=m2, cov=cov, missing=missing, notes={"search_pages": 1, "search_hit_slugs": 0},
               ref_rules=pd.DataFrame({"kind": ["above"], "month": ["2026-06"], "event_slug": ["e"], "slug": ["s"],
                                       "sig": ["x"], "rule": [""]}),
               rule_tab=rule_tab, excluded_kinds={}, quotes={"above": ("m0", ABOVE[:80])}, conv=conv, mism=mism,
               mall=None, spot_info={"days": 2, "units": {"us": 2}, "missing": []},
               trade_info=pd.DataFrame({"cid": ["c0"], "pages": [1], "windows": [1], "truncated": [False]}),
               timing=None, timing_spot=None, runtime=1.0)
    text = N.report(ctx, tmp_path / "r.md")
    assert "判定" in text and "0.95-0.99" in text and "source=binance+coinbase" in text
    assert (tmp_path / "r.md").read_text() == text + "\n"
