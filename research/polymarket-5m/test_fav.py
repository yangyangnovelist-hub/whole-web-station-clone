import io
import math
import zipfile
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

import calib as cb
import fav as F
import resolved as rs

T0 = 1_789_000_000 // 3600 * 3600           # a whole hour
END = T0 + 2 * 3600                         # market end of the synthetic hourly markets


# ------------------------------------------------------------------ rule bands and checkpoints

def test_rule_bands_are_left_closed():
    q = [0.8499, 0.85, 0.9, 0.9499, 0.95, 0.97, 0.9899, 0.99, 1.0, np.nan]
    assert list(F.in_band(q, "R1")) == [False, False, False, False, True, True, True, False, False, False]
    assert list(F.in_band(q, "R2")) == [False, True, True, True, False, False, False, False, False, False]


def test_rule_checks_and_kinds():
    assert F.rule_checks("R1", hourly=False) == (60, 30, 10, 5, 1)
    assert F.rule_checks("R1", hourly=True) == (30, 10, 5, 1)
    assert F.rule_checks("R2", hourly=False) == (60, 30)
    assert F.rule_checks("R2", hourly=True) == (30,)
    assert all(F.rule_applies("R1", k) for k in F.DAILY_KINDS + F.HOURLY)
    assert [k for k in F.DAILY_KINDS + F.HOURLY if F.rule_applies("R2", k)] == ["above", "above_1h"]


def test_bonferroni_count_is_rules_times_groups():
    assert F.N_TESTS == len(F.RULES) * len(F.GROUPS) == 10
    assert F.bonferroni() == pytest.approx(0.005)


# ------------------------------------------------------------------ synthetic klines -> hourly checkpoints

def walk_spot(start, hours=5, seed=1, s0=10_000_000.0, vol=2e-4):
    """rs.Spot of a random walk (1 s klines, integer cents) from `start` for `hours` hours."""
    rng = np.random.default_rng(seed)
    n = hours * 3600
    t = start + np.arange(n)
    px = np.rint(s0 * np.exp(np.cumsum(rng.normal(0, vol, n))))
    return rs.Spot.from_arrays(t, px, px, px), t, px


def hourly_dec(rows):
    base = {"coin": "BTC", "group": "btc_above_1h", "kind": "above_1h", "type": "above", "excluded": "",
            "end": float(END), "tstar": float(END), "ws": np.nan, "we": np.nan, "tsec": np.nan, "lo": np.nan,
            "hi": np.nan, "level": np.nan, "lo_rs": np.nan, "hi_rs": np.nan, "level_rs": np.nan, "ref": np.nan,
            "day": "2026-09-09", "open_ts": float(END - 3 * 3600), "official": 1.0}
    return pd.DataFrame([{**base, **r} for r in rows])


def test_hourly_checkpoints_are_30_10_5_1_and_respect_the_open():
    spot, t, px = walk_spot(T0 - 2 * 3600, hours=5)
    S = float(px[(END - 1800 - 1) - (T0 - 2 * 3600)])
    dec = hourly_dec([
        {"cid": "a", "lo": S / 100, "lo_rs": S / 100, "level_rs": S / 100},                      # opened long ago
        {"cid": "b", "lo": S / 100, "lo_rs": S / 100, "level_rs": S / 100, "open_ts": float(END - 20 * 60)},
        {"cid": "u", "kind": "updown_1h", "type": "updown_day", "group": "btc_updown_1h",
         "ref": float(px[END - 3600 - (T0 - 2 * 3600)]) / 100},
        {"cid": "d", "coin": "ETH", "group": "eth", "kind": "above", "tstar": float(END + 60), "lo_rs": S / 100,
         "level_rs": S / 100},
    ])
    cp = F.points_spot(dec, spot)
    got = cp.groupby("cid")["check"].apply(lambda s: sorted(s)).to_dict()
    assert got["a"] == [1, 5, 10, 30]                     # hourly: no 60-minute checkpoint
    assert got["b"] == [1, 5, 10]                         # opened 20 minutes before the end: 30 is dropped
    assert got["u"] == [1, 5, 10, 30]
    assert got["d"] == [1, 5, 10, 30, 60]                 # daily: all five
    a = cp[cp["cid"] == "a"].set_index("check")
    assert (a["tau"] == END - a["t"]).all()               # decision at the candle close = the end
    assert a.loc[30, "t"] == END - 1800
    # model: driftless, sigma of the past hour of 1 s returns, scaled by sqrt(time left)
    t30 = END - 1800
    sig = spot.sigma(t30)
    want = rs.p_yes("above", spot.price_before(t30), S, np.nan, sig * math.sqrt(1800))
    assert a.loc[30, "p_yes"] == pytest.approx(want)
    u = cp[cp["cid"] == "u"].set_index("check")
    ref = float(px[END - 3600 - (T0 - 2 * 3600)])
    want_u = rs.p_yes("updown_day", spot.price_before(END - 300), np.nan, np.nan,
                      spot.sigma(END - 300) * math.sqrt(300), ref=ref)
    assert u.loc[5, "p_yes"] == pytest.approx(want_u)
    assert set(cp["hourly"][cp["cid"].isin(["a", "b", "u"])]) == {True}


# ------------------------------------------------------------------ the fetch prefilter

def random_cp(n=4000, seed=7):
    rng = np.random.default_rng(seed)
    kinds = np.array(F.DAILY_KINDS + F.HOURLY)
    kind = kinds[rng.integers(0, len(kinds), n)]
    hourly = np.isin(kind, F.HOURLY)
    check = np.where(hourly, rng.choice([30, 10, 5, 1], n), rng.choice([60, 30, 10, 5, 1], n))
    p = rng.uniform(0, 1, n)
    edges = np.array([0.01, 0.05, 0.15, 0.85, 0.95, 0.99, 0.5, 0.0, 1.0])
    pick = rng.uniform(0, 1, n) < 0.15
    p[pick] = edges[rng.integers(0, len(edges), pick.sum())]
    grp = np.where(kind == "above_1h", "btc_above_1h", np.where(kind == "updown_1h", "btc_updown_1h",
                                                               rng.choice(["eth", "sol", "xrp"], n)))
    cp = pd.DataFrame({"cid": [f"m{i // 3}" for i in range(n)], "kind": kind, "check": check, "t": 1000 * np.arange(n),
                       "p_yes": p, "group": grp, "day": "2026-01-01", "hourly": hourly})
    cp = cp.drop_duplicates(["cid", "check"]).reset_index(drop=True)
    fav_yes = cp["p_yes"] >= 0.5
    cp["fav_yes"] = fav_yes
    cp["p_fav"] = np.where(fav_yes, cp["p_yes"], 1 - cp["p_yes"])
    # one market keeps a single kind / group
    first = cp.groupby("cid")[["kind", "group", "hourly"]].transform("first")
    cp[["kind", "group", "hourly"]] = first
    ok_check = np.where(cp["hourly"], cp["check"].isin([30, 10, 5, 1]), True)
    return cp[ok_check].reset_index(drop=True)


def test_prefilter_never_drops_a_market_with_an_in_band_side():
    cp = random_cp()
    w = F.windows(cp)
    have = set(zip(w["cid"], w["check"]))
    need = set()
    for r in cp.itertuples(index=False):
        for q in (r.p_yes, 1.0 - r.p_yes):
            for rule, R in F.RULES.items():
                checks = R["hourly"] if r.hourly else R["daily"]
                if R["lo"] <= q < R["hi"] and r.check in checks and F.rule_applies(rule, r.kind):
                    need.add((r.cid, r.check))
    assert need and need <= have                          # nothing in band is dropped
    assert have <= need                                   # and nothing else is fetched
    # every row the cells later take (cb.side_rows' q, both sides) has its window fetched
    mk = pd.DataFrame({"cid": cp["cid"].unique(), "official": 1.0})
    rows = F.side_rows(cp, pd.DataFrame(columns=["cid", "check", "is_yes", "price", "size"]), mk)
    for rule in F.RULES:
        for g in F.GROUPS:
            r = F.cell_rows(rows, rule, g)
            assert set(zip(r["cid"], r["check"])) <= have
    # markets with an in-band side are all among the fetched markets
    assert {c for c, _ in need} == set(w["cid"])


def test_prefilter_boundaries():
    cp = pd.DataFrame({"cid": list("abcdef"), "kind": ["above", "above", "range", "above_1h", "above_1h", "updown_1h"],
                       "check": [60, 60, 60, 60, 30, 30], "t": 0, "group": ["eth"] * 3 + ["btc_above_1h"] * 2 + ["btc_updown_1h"],
                       "day": "d", "hourly": [False, False, False, True, True, True],
                       "p_yes": [0.05, 0.9, 0.9, 0.97, 0.9, 0.9]})
    cp["p_fav"] = np.where(cp["p_yes"] >= 0.5, cp["p_yes"], 1 - cp["p_yes"])
    w = F.windows(cp).set_index("cid")["rules"].to_dict()
    assert w == {"a": "R1", "b": "R2", "e": "R2"}         # c: R2 is above-only; d: 60 is not hourly; f: R2 not up/down


# ------------------------------------------------------------------ sampling

def test_sample_plan_fits_and_is_reproducible():
    rng = np.random.default_rng(3)
    days = [f"2026-{m:02d}-{d:02d}" for m in range(1, 7) for d in range(1, 29)]
    win = pd.DataFrame({"group": np.r_[["eth"] * 300, ["btc_above_1h"] * 5000, ["sol"] * 200],
                        "day": np.r_[rng.choice(days, 300), rng.choice(days, 5000), rng.choice(days, 200)]})
    active = {g: days for g in F.GROUPS}
    p_all = F.sample_plan(win, active, budget=10_000)
    assert all(p["fraction"] == 1.0 for g, p in p_all.items())
    p1 = F.sample_plan(win, active, budget=2000)
    p2 = F.sample_plan(win, active, budget=2000)
    assert p1 == p2
    assert p1["eth"]["fraction"] == 1.0 and p1["sol"]["fraction"] == 1.0
    assert 0 < p1["btc_above_1h"]["fraction"] < 1
    assert sum(p["kept_windows"] for p in p1.values()) <= 2000 + 5000 / len(days) * 3


# ------------------------------------------------------------------ cell statistics and verdict

def cell_rows_synth(n_mk, pnl_fn, rule="R1", group="eth", kind="above", day_fn=None, seed=0):
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n_mk):
        p = 0.96
        res = float(pnl_fn(i, rng))
        rows.append({"cid": f"m{i}", "kind": kind, "check": 30, "t": i, "p_yes": 0.97, "is_yes": True, "q": 0.97,
                     "result": res, "trades": 1, "shares": 10.0 + i % 7, "cost": p * (10.0 + i % 7), "p": p,
                     "group": group, "hourly": False, "mday": day_fn(i) if day_fn else f"2026-01-{1 + i % 28:02d}"})
    r = pd.DataFrame(rows)
    r["pnl"] = r["result"] - r["p"]
    r["pnl_taker"] = r["pnl"] - 0.07 * r["p"] * (1 - r["p"])
    r["gap"] = r["q"] - r["p"]
    r["day"] = r["mday"]
    return r


def test_cell_verdict_needs_all_four_conditions():
    days = [f"2026-01-{d:02d}" for d in range(1, 29)]
    win = lambda i, rng: 1.0 if rng.uniform() > 0.01 else 0.0
    good = F.cell(cell_rows_synth(150, win), "R1", "eth", days)
    assert good["pass"] and good["markets"] == 150 and good["thr"] == pytest.approx(0.005)
    few = F.cell(cell_rows_synth(80, win), "R1", "eth", days)
    assert not few["pass"] and not few["ok"]["n"]
    # second half loses: markets on later days settle 0
    later = F.cell(cell_rows_synth(150, lambda i, rng: 1.0 if i % 28 < 14 else 0.0), "R1", "eth", days)
    assert not later["ok"]["halves"]
    # the threshold is alpha / n_tests: the same rows pass or fail depending on the count
    mixed = cell_rows_synth(150, lambda i, rng: 1.0 if rng.uniform() > 0.02 else 0.0, seed=3)
    c = F.cell(mixed, "R1", "eth", days)
    p1 = c["p1"]
    assert np.isfinite(p1) and 0 < p1 < 0.05
    assert F.cell(mixed, "R1", "eth", days, n_tests=max(1, int(0.05 / p1) - 1))["ok"]["p"]
    assert not F.cell(mixed, "R1", "eth", days, n_tests=int(0.05 / p1) + 1)["ok"]["p"]
    # rule 2 does not apply to a group without above markets
    assert not F.cell(cell_rows_synth(150, win, group="btc_updown_1h", kind="updown_1h"), "R2", "btc_updown_1h", days)["applies"]


def test_halves_split_at_the_median_day():
    r = cell_rows_synth(10, lambda i, rng: 1.0, day_fn=lambda i: f"2026-01-{1 + i:02d}")
    h1, h2, med = F.halves(r, 10)
    assert med == "2026-01-05" and h1["markets"] == 5 and h2["markets"] == 5
    r2 = cell_rows_synth(9, lambda i, rng: 1.0, day_fn=lambda i: "2026-01-01" if i < 6 else "2026-01-09")
    h1, h2, med = F.halves(r2, 10)
    assert med == "2026-01-01" and h1["markets"] == 6 and h2["markets"] == 3


def test_side_rows_price_and_pnl():
    cp = pd.DataFrame({"cid": ["a"], "kind": ["above_1h"], "check": [30], "t": [100], "p_yes": [0.96],
                       "group": ["btc_above_1h"], "day": ["2026-01-01"], "hourly": [True]})
    wt = pd.DataFrame({"cid": ["a", "a", "a"], "ts": [100, 130, 159], "taker_buy": [True, False, True],
                       "is_yes": [True, True, False], "price": [0.95, 0.97, 0.05], "size": [10.0, 30.0, 5.0], "check": 30})
    mk = pd.DataFrame({"cid": ["a"], "official": [1.0]})
    rows = F.side_rows(cp, wt, mk)
    y = rows[rows["is_yes"]].iloc[0]
    assert y["p"] == pytest.approx((0.95 * 10 + 0.97 * 30) / 40)
    assert y["pnl"] == pytest.approx(1 - y["p"])
    assert y["pnl_taker"] == pytest.approx(y["pnl"] - 0.07 * y["p"] * (1 - y["p"]))
    r = F.cell_rows(rows, "R1", "btc_above_1h")
    assert len(r) == 1 and bool(r["is_yes"].iloc[0])


def test_window_trades_keeps_only_the_window():
    kept = pd.DataFrame({"cid": ["a"], "check": [5], "t": [1000]})
    tr = pd.DataFrame({"cid": ["a"] * 4, "ts": [999, 1000, 1059, 1060], "taker_buy": True, "is_yes": True,
                       "price": 0.9, "size": 1.0, "check": 5})
    assert list(F.window_trades(tr, kept)["ts"]) == [1000, 1059]


# ------------------------------------------------------------------ hourly titles, DST, outcomes

def utc(*a):
    return int(datetime(*a, tzinfo=timezone.utc).timestamp())


def test_hourly_titles_and_expected_ends():
    end = utc(2026, 9, 15, 20)
    assert F.hourly_title("Bitcoin above ___ on September 15, 4PM ET?", end, "above_1h") == (date(2026, 9, 15), 16)
    assert F.hourly_expected_end("above_1h", date(2026, 9, 15), 16) == [end]
    end_u = utc(2026, 9, 15, 17)
    assert F.hourly_title("Bitcoin Up or Down - September 15, 12PM ET", end_u, "updown_1h") == (date(2026, 9, 15), 12)
    assert F.hourly_expected_end("updown_1h", date(2026, 9, 15), 12) == [end_u]
    # winter (EST) and the year boundary
    assert F.hourly_title("Bitcoin above ___ on January 1, 12AM ET?", utc(2026, 1, 1, 5), "above_1h") == (date(2026, 1, 1), 0)
    assert F.hourly_expected_end("above_1h", date(2026, 1, 1), 0) == [utc(2026, 1, 1, 5)]
    assert F.hourly_title("Bitcoin Up or Down - December 31, 11PM ET", utc(2026, 1, 1, 5), "updown_1h") == (date(2025, 12, 31), 23)
    # DST fall-back: 1 AM happens twice
    assert F.hourly_expected_end("above_1h", date(2025, 11, 2), 1) == [utc(2025, 11, 2, 5), utc(2025, 11, 2, 6)]
    # DST spring-forward: 2 AM does not exist -> no valid end (the market is excluded as end_mismatch)
    assert F.hourly_expected_end("updown_1h", date(2026, 3, 8), 2) == []
    assert F.hourly_expected_end("updown_1h", date(2026, 3, 8), 3) == [utc(2026, 3, 8, 8)]
    assert F.hour_slugs("updown_1h", end_u) == ["bitcoin-up-or-down-september-15-2026-12pm-et",
                                                  "bitcoin-up-or-down-september-15-12pm-et"]
    assert F.hour_slugs("above_1h", end) == ["bitcoin-above-on-september-15-2026-4pm-et", "bitcoin-above-on-september-15-4pm-et"]


def minutes_frame(rows):
    return F.Minutes(pd.DataFrame(rows, columns=["t", "o", "h", "l", "c"]))


def test_decide_hourly_rules():
    e = utc(2026, 9, 15, 20)
    mins = minutes_frame([(e - 3600, 10_000, 10_000, 10_000, 10_000), (e - 60, 10_000, 10_000, 10_000, 10_000),
                          (e + 3540, 10_100, 10_100, 10_100, 10_100), (e - 3660, 9_000, 9_000, 9_000, 9_000)])
    mk = pd.DataFrame({"cid": ["tie", "a99", "a100", "bad"], "kind": ["updown_1h", "above_1h", "above_1h", "above_1h"],
                       "lo": [np.nan, 99.0, 100.0, 99.0], "hi": np.nan, "level": [np.nan, 99.0, 100.0, 99.0],
                       "end": float(e), "official": [1.0, 1.0, 0.0, 0.0]})
    dec, conv, mism = F.decide_hourly(mk, mins)
    d = dec.set_index("cid")
    assert conv == "ends" and mism["above_ends"] == 1 and mism["above_begins"] == 2
    assert d.loc["tie", "comp"] == 1.0                 # close == open -> Up
    assert d.loc["a100", "comp"] == 0.0                # close == strike -> No (strictly higher)
    assert d.loc["bad", "excluded"] == "mismatch" and d.loc["a99", "excluded"] == ""
    assert d.loc["tie", "ref"] * 100 == 10_000 and d.loc["a99", "tstar"] == e


# ------------------------------------------------------------------ klines

def zip_bytes(lines):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("k.csv", "\n".join(lines) + "\n")
    b.seek(0)
    return b


def test_read_zip_units_and_scale(tmp_path):
    t_us, t_ms = 1757894400000000, 1757894400000
    p1 = tmp_path / "XRPUSDT-1s.zip"
    p1.write_bytes(zip_bytes([f"{t_us},3.0279,3.0281,3.0276,3.0277,1,0,0,0,0,0,0",
                              f"{t_us + 1_000_000},3.0277,3.0277,3.0277,3.0277,0,0,0,0,0,0,0"]).getvalue())
    t, o, h, l, c, dev = F.read_zip(p1, 10_000)
    assert list(t) == [1757894400, 1757894401]
    assert list(o) == [30279, 30277] and list(h) == [30281, 30277] and list(l) == [30276, 30277] and dev < 1e-6
    p2 = tmp_path / "BTCUSDT-1m.zip"
    p2.write_bytes(zip_bytes([f"{t_ms},115000.01,115010.50,114990.00,115005.99,1,0,0,0,0,0,0"]).getvalue())
    t, o, h, l, c, _ = F.read_zip(p2, 100)
    rt, rh, rl, rc = rs.read_kline_zip(p2)
    assert list(t) == list(rt) == [1757894400] and list(h) == list(rh) and list(l) == list(rl) and list(c) == list(rc)
    assert o[0] == 11500001


def test_scaled_strikes_round_trip_through_resolved_units():
    mk = pd.DataFrame({"lo": [0.9, 2.875], "hi": [1.0, np.nan], "level": [0.9, np.nan]})
    x = F.scaled(mk, "XRP")
    assert list(x["lo"] * 100) == [9000.0, 28750.0] and x["hi"].iloc[0] * 100 == 10000.0
    assert list(F.scaled(mk, "ETH")["lo"]) == [0.9, 2.875]


def test_minutes_lookup():
    m = minutes_frame([(60, 1, 2, 0, 1), (120, 1, 3, 1, 2)])
    assert list(m.at("c", [60, 120, 180, np.nan])[:2]) == [1.0, 2.0]
    assert np.isnan(m.at("c", [180])[0]) and np.isnan(m.at("c", [np.nan])[0])


# ------------------------------------------------------------------ resolution rule texts

ETH_ABOVE = ('This market will resolve to "Yes" if the Binance 1 minute candle for ETH/USDT 12:00 in the ET timezone (noon) '
             'on the date specified in the title has a final "Close" price higher than the price specified in the title. '
             'Otherwise, this market will resolve to "No". The resolution source for this market is Binance, specifically '
             'the ETH/USDT "Close" prices currently available at https://www.binance.com/en/trade/ETH_USDT with "1m" and '
             '"Candles" selected on the top bar.')
H_ABOVE = ('This market will resolve to "Yes" if the "Close" price for the BTC/USDT 1 hour candle that ends on the time and '
           'date specified in the title is higher than the price specified in the title. Otherwise, this market will resolve '
           'to "No". The resolution source for this market is Binance, specifically the BTC/USDT "Close" prices currently '
           'available at https://www.binance.com/en/trade/BTC_USDT with "1h" and "Candles" selected on the top bar.')
H_UP = ('This market will resolve to "Up" if the close price is greater than or equal to the open price for the BTC/USDT 1 '
        'hour candle that begins on the time and date specified in the title. Otherwise, this market will resolve to "Down". '
        'The resolution source for this market is information from Binance, specifically the BTC/USDT pair.')


def test_rule_texts():
    assert F.rule_one("ETH", "above", ETH_ABOVE, "above", "2026-09-15")[1] == ""
    assert "pair" in F.rule_one("SOL", "above", ETH_ABOVE, "above", "2026-09-15")[1]
    assert "other_pair=BTC" in F.rule_one("ETH", "above", ETH_ABOVE + " Compare BTC/USDT.", "above", "2026-09-15")[1]
    assert "candle=1h" in F.rule_one("ETH", "above", ETH_ABOVE.replace("1 minute candle", "1 hour candle")
                                     .replace('"1m"', '"1h"'), "above", "2026-09-15")[1]
    assert F.rule_one("BTC", "above_1h", H_ABOVE)[1] == ""
    assert F.rule_one("BTC", "above_1h", H_ABOVE.replace(" in the title is higher", " is higher"))[1] == ""
    assert "window" in F.rule_one("BTC", "above_1h", H_ABOVE.replace("ends on", "begins on"))[1]
    assert "candle" in F.rule_one("BTC", "above_1h", H_ABOVE.replace("1 hour candle", "1 minute candle").replace('"1h"', '"1m"'))[1]
    assert F.rule_one("BTC", "updown_1h", H_UP)[1] == ""
    assert "cmp" in F.rule_one("BTC", "updown_1h", H_UP.replace("greater than or equal to", "greater than"))[1]
    assert F.rule_one("BTC", "updown_1h", "")[1] == "no description"


def test_group_of():
    assert F.group_of("BTC", "above_1h") == "btc_above_1h" and F.group_of("BTC", "updown_1h") == "btc_updown_1h"
    assert F.group_of("XRP", "hit_daily") == "xrp"
