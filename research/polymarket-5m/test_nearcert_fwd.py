"""Tests for nearcert_fwd.py on synthetic built ladder tables (no network)."""
import gzip
import json
import math
import urllib.parse
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest
from scipy.stats import norm

import ladder as ld
import nearcert_fwd as nf
import resolved as rs

D = date(2026, 10, 6)
END = ld.noon_et(D)                 # 16:00 UTC, a market ending after the forward cut-off
S0, DELTA = 85000.0, 1e-4
T10 = END - 600                     # the 10-minute checkpoint
TE = T10 + nf.ENTRY_LAG
COLS = ["market_id", "ts", "recv", "bid", "ask", "bid_size", "ask_size", "tick", "depth99", "edge99"]
# Gamma descriptions, as written on 2026-10-04 (above / range: the old noon wording; daily up/down since
# 10-02: the candle CLOSING at 12:00)
ABOVE_DESC = ('This market will resolve to "Yes" if the Binance 1 minute candle for BTC/USDT 12:00 in the ET timezone '
              '(noon) on the date specified in the title has a final "Close" price higher than the price specified in '
              'the title. Otherwise, this market will resolve to "No".')
RANGE_DESC = ('This market will resolve according to the final "Close" price of the Binance 1 minute candle for BTC/USDT '
              '12:00 in the ET timezone (noon) on the date specified in the title.')
UPDOWN_NEW = ('This market will resolve to "Up" if the "Close" price for the Binance 1 minute candle for BTC/USDT closing '
              'at 12:00 in the ET timezone (noon) on Oct 5 \'26 is lower than the final "Close" price for the candle '
              'closing at 12:00 ET on Oct 6 \'26. If the final "Close" price for both of these candles is exactly equal '
              'on Binance, this market will resolve 50-50.')
UPDOWN_OLD = ('This market will resolve to "Up" if the "Close" price for the Binance 1 minute candle for BTC/USDT '
              'Oct 5 \'26 12:00 in the ET timezone (noon) is lower than the final "Close" price for the Oct 6 \'26 12:00 '
              'in the ET timezone (noon) candle. this market will resolve 50-50.')
HIT_DAILY = ('This market will immediately resolve to "Yes" if any Binance 1-minute candle for Bitcoin (BTC/USDT) on the '
             'date specified in the title, between 12:00 AM ET and 11:59 PM ET has a final "High" price equal to or '
             'greater than the price specified in the title.')
HIT_CREATION = ('This market will resolve to "Yes" if any Binance 1 minute candle for BTC/USDT on the date specified in '
                'the title, from the creation of this market through 11:59 PM ET, has a final High price equal to or '
                'greater than the price specified in the title.')
DESC = {"above": ABOVE_DESC, "range": RANGE_DESC, "updown_day": UPDOWN_NEW, "hit_up": HIT_DAILY, "hit_down": HIT_DAILY}


# ------------------------------------------------------------------ synthetic built tables

def spot_rows(t0, t1, skip=()):
    """One Binance trade per second at s + 0.5; the log price alternates 0, DELTA."""
    s = np.arange(int(t0), int(t1))
    s = s[~np.isin(s, list(skip))]
    return pd.DataFrame({"trade_ts": s + 0.5, "receive_ts": s + 0.55, "price": S0 * np.exp(DELTA * (s % 2))})


def s_before(t):
    return S0 * math.exp(DELTA * ((int(t) - 1) % 2))


def sigma_np(spot, t, w=3600):
    """The 1 s grid sigma computed independently: last price per second, carried forward."""
    sec = np.floor(spot["trade_ts"].to_numpy()).astype(int)
    last = pd.Series(spot["price"].to_numpy(), index=sec).groupby(level=0).last()
    grid = last.reindex(range(sec.min(), sec.max() + 1)).ffill()
    return float(np.diff(np.log(grid.loc[t - w - 1:t - 1].to_numpy())).std(ddof=1))


def row(tok, ts, ask, size=100.0, bid=None):
    return [tok, ts, ts + 0.05, (ask - 0.01) if bid is None else bid, ask, 50.0, size, 0.01, np.nan, np.nan]


def filler(t0, t1, step=1.0):
    """A busy token keeping the CLOB feed visibly alive."""
    return [row("FILL", float(x), 0.5) for x in np.arange(t0, t1, step)]


def mkt(cid, typ, lo=None, hi=None, end=END, day=D.isoformat(), **kw):
    m = {"slug": cid, "event_slug": kw.pop("event_slug", f"slug-{typ}"), "type": typ, "title": "",
         "question": kw.pop("question", ""),
         "lo": lo, "hi": hi, "end_ts": end, "start_ts": None, "ref_ts": None, "ref_price": None,
         "condition_id": cid, "yes_token": cid + "Y", "no_token": cid + "N", "day": day}
    m.update(kw)
    return m


def write_built(d, books, markets, spot, closes=()):
    d.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(books, columns=COLS).to_csv(d / "books.csv.gz", index=False)
    (d / "markets.json").write_text(json.dumps(markets))
    with gzip.open(d / "binance_trades.jsonl.gz", "wt") as f:
        for r in spot.itertuples(index=False):
            f.write(json.dumps({"event": "BINANCE_WS_TRADE", "trade_ts": r.trade_ts, "receive_ts": r.receive_ts,
                                "price": r.price}) + "\n")
    pd.DataFrame({"at_s": list(closes)}).to_csv(d / "clob_closes.csv.gz", index=False)
    (d / "counts.json").write_text("{}")
    return d


def strike(p, t, tau, spot):
    """Strike K with Phi(ln(S/K) / (sigma sqrt(tau))) = p at checkpoint t."""
    return s_before(t) * math.exp(-norm.ppf(p) * sigma_np(spot, t) * math.sqrt(tau))


SPOT = spot_rows(END - 3 * 3600, END + 120)
TAU10 = END + 60 - T10              # open12: the noon candle closes 60 s after the end


def rows_of(tmp_path, markets, books, spot=SPOT, name="r1", gamma=None, ref_close=None, closes=(), checks=nf.CHECKS,
            around=END, extremes=None):
    """Rows of one synthetic recording; gamma=None gives every market its type's description (DESC)."""
    if gamma is None:
        gamma = {m["condition_id"]: {"description": DESC.get(m["type"], ""), "createdAt": "2026-09-01T00:00:00Z"}
                 for m in markets}
    rec = nf.Rec.load(write_built(tmp_path / name / "built", books + filler(around - 3 * 3600, around + 120), markets,
                                  spot, closes))
    rows = pd.DataFrame(nf.rec_rows(rec, {m["condition_id"] for m in markets}, checks, nf.SIGMA_S, ref_close, gamma,
                                    extremes))
    return rec, rows


def at10(rows, cid):
    r = rows[(rows["cid"] == cid) & (rows["check"] == 10)]
    assert len(r) == 1
    return r.iloc[0]


# ------------------------------------------------------------------ model

def test_sigma_is_the_1s_grid_std_with_gaps_carried_forward(tmp_path):
    spot = spot_rows(END - 3 * 3600, END + 120, skip=range(T10 - 900, T10 - 880))  # a 20 s Binance outage
    rec = nf.Rec("x", pd.DataFrame(columns=COLS), pd.DataFrame(), spot)
    assert rec.sigma(T10) == pytest.approx(sigma_np(spot, T10), rel=1e-9)
    assert rec.sigma(T10) == pytest.approx(DELTA * math.sqrt(3580 / 3599), rel=1e-6)  # 20 zero returns
    assert np.isnan(rec.sigma(int(spot["trade_ts"].iloc[0]) + 3600))   # second t-3601 not recorded
    assert rec.history_ok(int(spot["trade_ts"].iloc[0]) + 3601, 3600)
    thin = spot.iloc[::3]                                                 # a trade every third second
    assert np.isnan(nf.Rec("y", pd.DataFrame(columns=COLS), pd.DataFrame(), thin).sigma(T10))


def test_model_never_uses_a_trade_received_after_the_checkpoint():
    base = spot_rows(END - 3 * 3600, END + 120)
    late = pd.DataFrame({"trade_ts": [T10 - 0.01], "receive_ts": [T10 + 0.20], "price": [S0 * 1.20]})
    clean = nf.Rec("clean", pd.DataFrame(columns=COLS), pd.DataFrame(), base)
    delayed = nf.Rec("delayed", pd.DataFrame(columns=COLS), pd.DataFrame(), pd.concat([base, late]))
    assert delayed.price_before(T10) == pytest.approx(clean.price_before(T10))
    assert delayed.sigma(T10) == pytest.approx(clean.sigma(T10))


def test_model_probability_above_range_and_updown_day(tmp_path):
    K = strike(0.97, T10, TAU10, SPOT)
    a, b = strike(0.99, T10, TAU10, SPOT), strike(0.02, T10, TAU10, SPOT)
    ref = s_before(T10) * 0.9999
    calls = []

    def ref_close(o):
        calls.append(o)
        return ref

    ms = [mkt("A", "above", lo=K), mkt("R", "range", lo=a, hi=b), mkt("U", "updown_day"), mkt("V", "updown_day")]
    gamma = {"A": {"description": ABOVE_DESC}, "R": {"description": RANGE_DESC}, "U": {"description": UPDOWN_NEW},
             "V": {"description": UPDOWN_OLD}}
    _, rows = rows_of(tmp_path, ms, [], ref_close=ref_close, gamma=gamma)
    sd = sigma_np(SPOT, T10) * math.sqrt(TAU10)
    S = s_before(T10)
    r = at10(rows, "A")
    assert r["tau"] == TAU10 and r["S"] == pytest.approx(S) and r["conv"] == "open12"
    assert r["p_yes"] == pytest.approx(norm.cdf(math.log(S / K) / sd), abs=1e-12)
    assert r["p_yes"] == pytest.approx(0.97, abs=1e-9)
    assert at10(rows, "R")["p_yes"] == pytest.approx(norm.cdf(math.log(S / a) / sd) - norm.cdf(math.log(S / b) / sd),
                                                     abs=1e-12)
    # the 10-05 wording settles on the candle CLOSING at noon (opens 11:59 ET): tau = noon - t, and the
    # reference is the previous day's candle closing at noon (the recorder's ref_price)
    u = at10(rows, "U")
    assert (u["conv"], u["tau"]) == ("close12", END - T10)
    assert u["p_yes"] == pytest.approx(norm.cdf(math.log(S / ref) / (sigma_np(SPOT, T10) * math.sqrt(END - T10))),
                                       abs=1e-12)
    # the old wording ('Oct 5 '26 12:00 in the ET timezone (noon)') stays resolved.py's open12
    v = at10(rows, "V")
    assert (v["conv"], v["tau"]) == ("open12", TAU10)
    assert v["p_yes"] == pytest.approx(norm.cdf(math.log(S / ref) / sd), abs=1e-12)
    assert set(calls) == {ld.noon_et(D - timedelta(days=1)) - 60, ld.noon_et(D - timedelta(days=1))}


def test_noon_rule_from_the_description():
    assert nf.noon_rule("above", ABOVE_DESC, D) == ("open12", "")
    assert nf.noon_rule("range", RANGE_DESC, D) == ("open12", "")
    assert nf.noon_rule("updown_day", UPDOWN_NEW, D) == ("close12", "")
    assert nf.noon_rule("updown_day", UPDOWN_OLD, D) == ("open12", "")
    # the live 10-05 market, verbatim (curly quotes, "Oct 4 '26")
    live = ("This market will resolve to \u201cUp\u201d if the \u201cClose\u201d price for the Binance 1 minute candle "
            "for BTC/USDT closing at 12:00 in the ET timezone (noon) on Oct 4 \u201926 is lower than the final "
            "\u201cClose\u201d price for the candle closing at 12:00 ET on Oct 5 \u201926.")
    assert nf.noon_rule("updown_day", live, date(2026, 10, 5)) == ("close12", "")
    assert nf.noon_rule("updown_day", UPDOWN_NEW, D + timedelta(days=1))[0] is None          # other dates
    mixed = UPDOWN_NEW.replace("for the candle closing at 12:00 ET", "for the Oct 6 '26 12:00 in the ET timezone (noon)")
    assert nf.noon_rule("updown_day", mixed, D) == (None, "candle")
    assert nf.noon_rule("above", ABOVE_DESC.replace("Binance", "Coinbase"), D) == (None, "source")
    assert nf.noon_rule("above", ABOVE_DESC.replace("1 minute candle", "1 hour candle"), D) == (None, "source")
    assert nf.noon_rule("above", "", D) == (None, "no description")
    assert nf.noon_rule("above", ABOVE_DESC.replace("12:00 in the ET timezone (noon)", "4:00 PM ET"), D)[0] is None


def test_updown_day_reference_pending_end_mismatch_and_rule(tmp_path):
    ms = [mkt("U", "updown_day"), mkt("X", "above", lo=80000.0, end=END + 3600), mkt("G", "above", lo=80000.0),
          mkt("O", "range", lo=80000.0, hi=90000.0)]
    gamma = {"U": {"description": UPDOWN_NEW}, "X": {"description": ABOVE_DESC},
             "O": {"description": RANGE_DESC.replace("12:00 in the ET timezone (noon)", "closing at 12:00 ET")
                   .replace("1 minute candle", "1 hour candle")}}
    _, rows = rows_of(tmp_path, ms, [], ref_close=lambda o: None, gamma=gamma)
    assert at10(rows, "U")["status"] == "ref_pending"
    assert set(rows.loc[rows["cid"] == "X", "status"]) == {"end_mismatch"}
    assert at10(rows, "G")["status"] == "gamma_na"        # no description yet: undetermined, retried
    assert at10(rows, "O")["status"] == "rule_other"


HSTART = ld.et_to_utc(D, 0)                          # the daily hit window of D: 04:00 UTC ..
HEND = ld.et_to_utc(D + timedelta(days=1), 0)       # .. 04:00 UTC the next day (its endDate)
HSPOT = spot_rows(HEND - 3 * 3600, HEND + 120)
HT10 = HEND - 600


def test_hit_reflection_window_and_high_so_far(tmp_path):
    tau = HEND - HT10
    sd = sigma_np(HSPOT, HT10) * math.sqrt(tau)
    S = s_before(HT10)
    K = S * math.exp(-norm.ppf(0.015) * sd)   # 2 Phi(ln(S/K)/sd) = 0.03 -> No favoured at 0.97
    first = float(HSPOT["trade_ts"].iloc[0])
    q = "Will Bitcoin reach $X on October 6?"
    pre = dict(start_ts=HSTART, pre_at=first - 2, question=q)
    Kd = S * math.exp(norm.ppf(0.015) * sd)
    late = lambda k: dict(pre, start_ts=HSTART + 600 * k)   # Gamma's startDate k x 10 min after the window start
    ms = [mkt("H", "hit_up", lo=K, pre_high=K - 50, pre_low=80000.0, **pre),
          mkt("H2", "hit_up", lo=K, pre_high=K + 1, pre_low=80000.0, **pre),                 # reached before
          mkt("H3", "hit_up", lo=K, pre_high=K - 50, pre_low=80000.0, **{**pre, "pre_at": HT10 + 5}),  # look-ahead
          mkt("H4", "hit_up", lo=K, start_ts=HSTART, question=q),                             # no pre_*
          mkt("H5", "hit_up", lo=S0 * math.exp(DELTA / 2), pre_high=S0, pre_low=S0, **pre),   # a trade reached it
          mkt("H6", "hit_up", lo=K, start_ts=HSTART, question=q),                             # created inside rec
          mkt("H7", "hit_up", lo=K, pre_high=K - 50, pre_low=80000.0, **{**pre, "pre_at": first - 60}),  # hole
          mkt("H8", "hit_up", lo=K, pre_high=K - 50, pre_low=80000.0, **late(1)),   # the gap candles reached it
          mkt("H9", "hit_up", lo=K, pre_high=K - 50, pre_low=80000.0, **late(2)),   # ... did not
          mkt("H10", "hit_up", lo=K, pre_high=K - 50, pre_low=80000.0, **late(3)),  # ... not published yet
          mkt("C1", "hit_up", lo=K, pre_high=K + 1, pre_low=80000.0, **pre),                 # from creation
          mkt("C2", "hit_up", lo=K, pre_high=K - 50, pre_low=80000.0, **pre),
          mkt("L", "hit_down", hi=Kd, pre_high=90000.0, pre_low=Kd + 50, **pre),
          mkt("G", "hit_up", lo=K, pre_high=K - 50, pre_low=80000.0, **pre),                 # no Gamma record
          mkt("W", "hit_up", lo=K, pre_high=K - 50, pre_low=80000.0, **pre),                 # weekly wording
          mkt("E", "hit_up", lo=K, pre_high=K - 50, pre_low=80000.0, **{**pre, "question": "Will Bitcoin reach $X on "
                                                                         "October 7?"}),     # window ends later
          mkt("Y", "hit_up", lo=K, pre_high=K - 50, pre_low=80000.0, **{**pre, "question": "Will Bitcoin reach $X in "
                                                                         "2026?"})]          # a yearly window
    for m in ms:
        m["end_ts"], m["day"] = HEND, None
    plain = {"description": HIT_DAILY, "createdAt": "2026-10-05T04:00:01Z"}
    gamma = {m["condition_id"]: plain for m in ms if m["condition_id"] != "G"}
    gamma["C1"] = gamma["C2"] = {"description": HIT_CREATION, "createdAt": "2026-10-06T06:00:30Z"}
    gamma["H6"] = {"description": HIT_CREATION, "createdAt": nf._iso(HT10 - 1800)}
    gamma["W"] = {"description": HIT_DAILY.replace("on the date specified in the title, between 12:00 AM ET and 11:59 "
                                                   "PM ET", "during the date range specified in the title")}
    asked = []

    def extremes(a, b):
        asked.append((a, b))
        return {HSTART + 600: (K + 1, 80000.0), HSTART + 1200: (K - 10, 80000.0)}.get(b)

    books = [row(c + "N", HT10 - 20, 0.96) for c in ("H", "H6", "H9", "C2", "L")]
    _, rows = rows_of(tmp_path, ms, books, spot=HSPOT, gamma=gamma, around=HEND, extremes=extremes)
    at = lambda c: rows[(rows["cid"] == c) & (rows["check"] == 10)].iloc[0]
    r = at("H")
    assert r["tau"] == tau
    assert r["p_yes"] == pytest.approx(2 * norm.cdf(math.log(S / K) / sd), abs=1e-12)
    assert (r["status"], r["side"], r["token"], r["ask"]) == ("trade", "No", "HN", 0.96)
    assert r["kind"] == "hit_daily"
    assert at("L")["p_yes"] == pytest.approx(2 * norm.cdf(math.log(Kd / S) / sd), abs=1e-12)
    assert at("L")["status"] == "trade"
    st = {c: at(c)["status"] for c in ("H2", "H3", "H4", "H5", "H6", "H7", "H8", "H9", "H10", "C1", "C2", "G", "W",
                                       "E", "Y")}
    assert st == {"H2": "decided", "H3": "hit_unknown", "H4": "hit_unknown", "H5": "decided", "H6": "trade",
                  "H7": "hit_unknown", "H8": "decided", "H9": "trade", "H10": "kline_pending", "C1": "hit_unknown",
                  "C2": "trade", "G": "gamma_na", "W": "rule_other", "E": "end_mismatch", "Y": "rule_other"}
    assert at("Y")["kind"] == "hit_other"
    # the gap between the window start (00:00 ET) and pre_*'s start is read from the official candles, once
    # per checkpoint, from the window start to the candle holding startDate
    assert {b - a for a, b in asked} == {600, 1200, 1800} and {a for a, _ in asked} == {HSTART}


def test_hit_kinds_from_the_question():
    end = lambda d: ld.et_to_utc(d + timedelta(days=1), 0)
    assert nf.kind_of("hit_up", "Will Bitcoin reach $92,000 on October 6?", end(D)) == "hit_daily"
    assert nf.kind_of("hit_down", "Will Bitcoin dip to $80,000 in October?", end(date(2026, 10, 31))) == "hit_monthly"
    assert nf.kind_of("hit_up", "Will Bitcoin reach $98,000 September 28-October 4?", end(date(2026, 10, 4))) == \
        "hit_weekly"
    # the yearly event (slug what-price-will-bitcoin-hit-before-2027) and anything unreadable: excluded
    assert nf.kind_of("hit_up", "Will Bitcoin reach $150,000 in 2026?", ld.et_to_utc(date(2027, 1, 1), 0)) == "hit_other"
    assert nf.kind_of("hit_down", "Will Bitcoin dip to $50,000 by December 31, 2026?", end(D)) == "hit_other"
    assert nf.kind_of("hit_up", None, end(D)) == "hit_other"
    assert nf.kind_of("range", None) == "range"
    assert "hit_other" not in nf.BINANCE_KINDS


# ------------------------------------------------------------------ side and entry

def test_side_selection(tmp_path):
    ms = [mkt("A", "above", lo=strike(0.97, T10, TAU10, SPOT), fee_rate=0.072),
          mkt("B", "above", lo=strike(0.03, T10, TAU10, SPOT)),
          mkt("C", "above", lo=strike(0.995, T10, TAU10, SPOT)), mkt("E", "above", lo=strike(0.90, T10, TAU10, SPOT)),
          mkt("F", "above", lo=strike(0.95, T10, TAU10, SPOT) * (1 - 1e-9))]
    books = [row(t, T10 - 30, 0.96) for t in ("AY", "AN", "BY", "BN", "CY", "EY", "FY")]
    _, rows = rows_of(tmp_path, ms, books)
    a, b = at10(rows, "A"), at10(rows, "B")
    assert (a["status"], a["side"], a["token"]) == ("trade", "Yes", "AY")
    assert a["fee_rate"] == pytest.approx(0.072)
    assert (b["status"], b["side"], b["token"]) == ("trade", "No", "BN")
    assert b["p_fav"] == pytest.approx(0.97, abs=1e-9)
    assert at10(rows, "C")["status"] == "above"
    assert at10(rows, "E")["status"] == "below"
    assert at10(rows, "F")["status"] == "trade"          # 0.95 is inside the band


def test_entry_uses_only_the_book_as_of_entry_time(tmp_path):
    K = strike(0.97, T10, TAU10, SPOT)
    ms = [mkt(c, "above", lo=K) for c in ("A1", "A2", "A3", "A4", "A5")]
    books = [row("A1Y", T10 - 30, 0.96), row("A1Y", TE + 0.001, 0.50),     # a later row is not read
             row("A2Y", T10 - 30, 0.96), row("A2Y", TE, 0.97),             # a row stamped at te is
             row("A3Y", TE + 0.2, 0.96),                                   # nothing before te
             row("A4Y", T10 - 30, 0.985),                                   # above 0.98
             row("A5Y", T10 - 30, 0.96, size=4.0)]                          # under 5 shares
    rec, rows = rows_of(tmp_path, ms, books)
    got = {c: (at10(rows, c)["status"], at10(rows, c)["ask"]) for c in ("A1", "A2", "A4", "A5")}
    assert got == {"A1": ("trade", 0.96), "A2": ("trade", 0.97), "A4": ("ask_band", 0.985), "A5": ("ask_size", 0.96)}
    assert at10(rows, "A3")["status"] == "no_book"
    assert at10(rows, "A1")["age"] == pytest.approx(30.5)
    # the same entries with every book row after te removed
    cut = pd.read_csv(tmp_path / "r1" / "built" / "books.csv.gz", dtype={"market_id": str})
    rec2 = nf.Rec("cut", cut[cut["ts"] <= TE], rec.markets, SPOT)
    for c in ("A1", "A2", "A3", "A4", "A5"):
        assert rec2.entry(c + "Y", TE)[:3] == pytest.approx(rec.entry(c + "Y", TE)[:3], nan_ok=True)


def test_stale_feed_and_disconnect(tmp_path):
    K = strike(0.97, T10, TAU10, SPOT)
    ms = [mkt("A", "above", lo=K)]
    books = [row("AY", T10 - 30, 0.96)]
    # the feed went quiet for 8 s after the token's row: stale
    quiet = [r for r in filler(END - 3 * 3600, END + 120) if not (T10 - 12 < r[1] < T10 - 4)]
    rec = nf.Rec.load(write_built(tmp_path / "q" / "built", books + quiet, ms, SPOT))
    assert rec.entry("AY", TE)[0] == "stale"
    # a quiet stretch before the token's row does not matter
    quiet = [r for r in filler(END - 3 * 3600, END + 120) if not (T10 - 60 < r[1] < T10 - 40)]
    rec = nf.Rec.load(write_built(tmp_path / "q2" / "built", books + quiet, ms, SPOT))
    assert rec.entry("AY", TE)[0] == "trade"
    # a CLOB disconnect just before the entry
    _, rows = rows_of(tmp_path, ms, books, name="dc", closes=[TE - 3])
    assert at10(rows, "A")["status"] == "stale"


def test_snapshot_rows_and_resubscription_confirm_a_quiet_book(tmp_path):
    """A quiet token's only row is the subscription snapshot, stamped with the book's last change long
    before the connection; after a CLOB disconnect an unchanged book writes no new row. Both are the
    exchange's state once the feed runs: not stale (they were before, which favoured busy books)."""
    con = T10 - 100                                         # the recorder connects
    snap = lambda tok, ts, recv=con + 0.3: [tok, ts, recv, 0.95, 0.96, 50.0, 100.0, 0.01, np.nan, np.nan]
    books = [snap("AY", T10 - 2000), snap("OLD1", T10 - 1900), snap("OLD2", T10 - 1500),
             row("CY", T10 - 300, 0.96),                     # an ordinary row, then a disconnect at T10 - 199
             snap("DY", T10 - 40, recv=TE + 3.0)]            # stamped before te, received after it
    feed = [r for r in filler(con, END + 120) if not (T10 - 200 <= r[1] < T10 - 190)]
    rec = nf.Rec.load(write_built(tmp_path / "s" / "built", books + feed + filler(T10 - 400, T10 - 200),
                                  [], SPOT, closes=[T10 - 199]))
    assert rec.entry("AY", TE)[:2] == ("trade", 0.96)
    assert rec.entry("CY", TE)[0] == "trade"                 # reconfirmed by the resubscription
    assert rec.entry("DY", TE)[0] == "stale"                 # its state reached the recorder only after te
    # the same quiet stretch without a disconnect: nothing reconfirms the book -> stale, as before
    rec2 = nf.Rec.load(write_built(tmp_path / "s2" / "built", books + feed + filler(T10 - 400, T10 - 200), [], SPOT))
    assert rec2.entry("CY", TE)[0] == "stale"
    # a disconnect inside [te - 10, te + 1] still makes every entry stale
    rec3 = nf.Rec.load(write_built(tmp_path / "s3" / "built", books + feed, [], SPOT, closes=[TE - 3]))
    assert rec3.entry("AY", TE)[0] == "stale"


def test_spot_gap_and_short_history(tmp_path):
    K = strike(0.97, T10, TAU10, SPOT)
    ms = [mkt("A", "above", lo=K)]
    gap = spot_rows(END - 3 * 3600, END + 120, skip=range(T10 - 8, T10))
    _, rows = rows_of(tmp_path, ms, [row("AY", T10 - 30, 0.96)], spot=gap, name="g")
    assert at10(rows, "A")["status"] == "spot_gap"
    short = spot_rows(T10 - 3000, END + 120)
    _, rs_short = rows_of(tmp_path, ms, [row("AY", T10 - 30, 0.96)], spot=short, name="s")
    assert at10(rs_short, "A")["status"] == "short_history"
    _, rs_full = rows_of(tmp_path, ms, [row("AY", T10 - 30, 0.96)], name="f")
    scope = pd.DataFrame(ms)
    cp = nf.combine(rs_short.to_dict("records") + rs_full.to_dict("records"), scope, data_end=END + 120)
    assert len(cp) == len(nf.CHECKS)                       # one row per (market, checkpoint)
    assert cp.loc[cp["check"] == 10, ["run", "status"]].values.tolist() == [["f", "trade"]]
    only_short = nf.combine(rs_short.to_dict("records"), scope, data_end=END + 120)
    assert only_short.loc[only_short["check"] == 10, "status"].item() == "short_history"


def test_combine_counts_unrecorded_and_future_checkpoints():
    scope = pd.DataFrame([mkt("A", "above", lo=80000.0), mkt("Z", "updown_4h", end=END + 7200, day=None)])
    cp = nf.combine([], scope, data_end=END - 2000)
    a = cp[cp["cid"] == "A"].set_index("check")["status"].to_dict()
    assert a == {60: "not_recorded", 30: "not_yet", 10: "not_yet", 5: "not_yet", 1: "not_yet"}
    assert set(cp.loc[cp["cid"] == "Z", "kind"]) == {"updown_4h"}


def test_scope_since_filter(tmp_path):
    ts_since = int(nf.ts_of(nf.SINCE))
    ms = [mkt("A", "above", lo=80000.0, end=ld.noon_et(date(2026, 10, 4))),
          mkt("B", "above", lo=80000.0, end=ts_since),                   # ends exactly at the cut-off
          mkt("C", "above", lo=80000.0), mkt("Q", "weird")]
    d = write_built(tmp_path / "x" / "built", [], ms, SPOT)
    assert ts_since == 1791158400
    assert nf.scope_markets([d], nf.SINCE)["condition_id"].tolist() == ["C"]
    assert set(nf.scope_markets([d], "2026-10-04T00:00:00Z")["condition_id"]) == {"A", "B", "C"}


# ------------------------------------------------------------------ results, pending, verdict

def entry(cid, t, kind="above", side="Yes", ask=0.96, official=1.0, end=None, check=10, run="r1",
          fee_rate=nf.FEE):
    won = official if side == "Yes" else 1 - official
    fee = fee_rate * ask * (1 - ask)
    return {"run": run, "cid": cid, "kind": kind, "type": kind, "check": check, "t": float(t),
            "end": float(end if end is not None else t + 600), "status": "trade", "side": side, "ask": ask,
            "p_fav": 0.97, "age": 1.0, "fee_rate": fee_rate,
            "official": official, "won": won, "fee": fee, "pnl": won - ask - fee}


def test_settle_marks_pending_and_values_resolved_markets():
    cp = pd.DataFrame([{**entry("A", T10, fee_rate=0.072), "end": END},
                       {**entry("B", T10, side="No"), "end": END},
                       {**entry("C", T10), "end": END + 86400}, {**entry("N", T10), "status": "below", "end": END}])
    cp = cp.drop(columns=["official", "won", "fee", "pnl"])
    asked = []

    def fetch(cids):
        asked.append(sorted(cids))
        return {"A": {"closed": True, "outcomes": '["Yes", "No"]', "outcomePrices": '["1", "0"]',
                      "umaResolutionStatus": "resolved"},
                "B": {"closed": False, "outcomes": '["Yes", "No"]', "outcomePrices": '["0.999", "0.001"]'}}

    e = nf.settle(cp, fetch, now=END + 3600).set_index("cid")
    assert asked == [["A", "B"]]                      # C has not ended: not asked, pending
    assert e.loc["A", "pnl"] == pytest.approx(1 - 0.96 - 0.072 * 0.96 * 0.04)
    assert np.isnan(e.loc["B", "official"]) and np.isnan(e.loc["C", "official"])   # open: pending
    assert "N" not in e.index


def test_official_needs_a_closed_resolved_market():
    rec = {"closed": True, "outcomes": '["Up", "Down"]', "outcomePrices": '["0", "1"]', "umaResolutionStatus": "resolved"}
    assert nf.official(rec) == 0.0
    assert np.isnan(nf.official({**rec, "closed": False}))
    assert np.isnan(nf.official({**rec, "umaResolutionStatus": "proposed"}))
    assert nf.official({**rec, "outcomePrices": '["0.5", "0.5"]'}) == 0.5
    assert np.isnan(nf.official(None))


def test_clustered_standard_error():
    mu, se, t, G = nf.clustered([1.0, 3.0, -1.0, 1.0], ["a", "a", "b", "b"])
    # deviations summed per market: a = 2, b = -2 -> se = sqrt(8 * 2 / 1) / 4
    assert (mu, G) == (1.0, 2) and se == pytest.approx(1.0) and t == pytest.approx(1.0)


def test_pinned_verdict_once(tmp_path):
    out = tmp_path / "real" / "nf.md"
    pinned = tmp_path / "real" / "nf.verdict.md"
    base = [entry(c, T10 + 86400 * i, ask=0.10, official=1.0) for i, c in enumerate("ABC")] + \
        [entry("A", T10 + 300, ask=0.10, check=5), entry("Z", T10 + 1, kind="updown_4h", official=0.0)]
    cp = pd.DataFrame(base)
    e = pd.DataFrame(base)
    covered = float(e["end"].max() + 1)
    # too few Binance-settled markets (the 4-hour control does not count)
    assert "不到 4 个" in nf.judge(out, e, cp, covered, n=4, min_days=3) and not pinned.exists()
    # the first 3 are not all resolved yet
    late = e.copy()
    late.loc[late["cid"] == "B", "official"] = np.nan
    assert "还没结算" in nf.judge(out, late, cp, covered, n=3, min_days=3) and not pinned.exists()
    # an earlier checkpoint still undetermined holds the verdict whatever its age (no silent escape)
    cp2 = pd.concat([cp, pd.DataFrame([{"cid": "U", "kind": "updown_day", "t": T10 - 5, "end": END - 30 * 86400,
                                        "status": "ref_pending"}])])
    assert "待定" in nf.judge(out, e, cp2, covered, n=3, min_days=3) and not pinned.exists()
    # ... and so does a later undetermined checkpoint of a chosen market (it could add an entry)
    cp3 = pd.concat([cp, pd.DataFrame([{"cid": "C", "kind": "above", "t": T10 + 86400 * 5, "end": END + 86400 * 5,
                                        "status": "gamma_na"}])])
    assert "待定" in nf.judge(out, e, cp3, covered, n=3, min_days=3) and not pinned.exists()
    # an explicit, counted exclusion (expired_pending) does not hold it
    cp4 = cp2.assign(status=cp2["status"].replace("ref_pending", "expired_pending"))
    assert "待定" not in nf.judge(tmp_path / "x.md", e, cp4, covered, n=3, min_days=3)
    # this run's blockers (a recording not read, a fetch error) hold it
    assert "清单" in nf.judge(out, e, cp, covered, n=3, min_days=3, blockers=["没有可下载录制的清单（--expect）"])
    assert not pinned.exists()
    # recordings not yet past the markets' end
    assert "录制" in nf.judge(out, e, cp, T10, n=3, min_days=3) and not pinned.exists()
    # development runs never judge
    assert "开发运行" in nf.judge(out, e, cp, covered, n=3, min_days=3, design=False) and not pinned.exists()
    v = nf.judge(out, e, cp, covered, n=3, min_days=3)
    assert pinned.exists() and "前 3 个市场，4 笔" in v and "通过" in v
    assert "99%" in v and "精确" in v
    judged = pd.read_csv(tmp_path / "real" / "nf.trades.csv")
    assert sorted(judged["cid"]) == ["A", "A", "B", "C"]
    text = pinned.read_text(encoding="utf-8")
    # later data never changes it
    worse = pd.DataFrame([entry(c, T10 + i, official=0.0) for i, c in enumerate("ABCDE")])
    again = nf.judge(out, worse, worse, covered, n=3, min_days=3)
    assert again == text.strip() + "（已判定，不再重算）"
    assert pinned.read_text(encoding="utf-8") == text


def test_verdict_requires_tail_robust_daily_lower_bound_and_exact_p(tmp_path):
    lose = [entry(c, T10 + 86400 * i, ask=0.10, official=0.0) for i, c in enumerate("ABCDEFGHIJ")]
    covered = max(r["end"] for r in lose) + 1
    v = nf.judge(tmp_path / "a.md", pd.DataFrame(lose), pd.DataFrame(lose), covered, n=10)
    assert "没通过" in v
    priced_fair = [entry(c, T10 + 86400 * i, ask=0.95) for i, c in enumerate("ABCDEFGHIJ")]
    covered = max(r["end"] for r in priced_fair) + 1
    v = nf.judge(tmp_path / "b.md", pd.DataFrame(priced_fair), pd.DataFrame(priced_fair), covered, n=10)
    assert "没通过" in v and "精确" in v
    win = [entry(c, T10 + 86400 * i, ask=0.10) for i, c in enumerate("ABCDEFGHIJ")]
    covered = max(r["end"] for r in win) + 1
    v = nf.judge(tmp_path / "c.md", pd.DataFrame(win), pd.DataFrame(win), covered, n=10)
    assert "→ 通过" in v


def test_exact_p_counts_a_market_once_even_with_multiple_checkpoints():
    rows = pd.DataFrame([
        entry("A", T10, ask=0.10), entry("A", T10 + 1, ask=0.30, check=5),
        entry("B", T10 + 86400, ask=0.20),
    ])
    qa = rows.loc[rows["cid"] == "A", "ask"].iloc[0] + rows.loc[rows["cid"] == "A", "fee"].iloc[0]
    qb = rows.loc[rows["cid"] == "B", "ask"].mean() + rows.loc[rows["cid"] == "B", "fee"].mean()
    assert nf.market_exact_pvalue(rows) == pytest.approx(qa * qb)


# ------------------------------------------------------------------ end to end

class FakeNet:
    """Gamma answers from a table (descriptions: desc, else the above wording); data.binance.vision has nothing."""

    def __init__(self, resolved, open_=(), desc=None):
        self.resolved, self.open, self.urls, self.desc = resolved, set(open_), [], desc or {}

    def get(self, url, binary=False):
        self.urls.append(url)
        if "gamma-api" not in url:
            raise nf.NotFound(url)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        out = []
        for c in q.get("condition_ids", []):
            base = {"conditionId": c, "outcomes": '["Yes", "No"]', "description": self.desc.get(c, ABOVE_DESC),
                    "createdAt": "2026-10-05T04:00:00Z"}
            if q.get("closed") == ["true"] and c in self.resolved:
                out.append({**base, "closed": True, "outcomePrices": self.resolved[c], "umaResolutionStatus": "resolved"})
            elif "closed" not in q and c in self.open:
                out.append({**base, "closed": False, "outcomePrices": '["0.5", "0.5"]'})
        return out


def test_run_end_to_end(tmp_path):
    K = strike(0.97, T10, TAU10, SPOT)
    first = float(SPOT["trade_ts"].iloc[0])
    ms = [mkt("A", "above", lo=K), mkt("B", "above", lo=strike(0.03, T10, TAU10, SPOT)),
          mkt("P", "above", lo=K, end=END + 86400, day=(D + timedelta(days=1)).isoformat()),
          mkt("H", "hit_up", lo=S0 * 1.5, start_ts=END - 12 * 3600, pre_at=first - 1, pre_high=S0, pre_low=S0,
              day=None, question="Will Bitcoin reach $X on October 6?"),
          mkt("Z", "updown_4h", end=END, day=None, ref_ts=END - 4 * 3600, ref_price=strike(0.97, T10, END - T10, SPOT))]
    books = [row("AY", T10 - 30, 0.96), row("BN", T10 - 30, 0.95), row("ZY", T10 - 30, 0.96)] + filler(END - 3 * 3600,
                                                                                                         END + 120)
    write_built(tmp_path / "rec" / "111" / "built", books, ms, SPOT)
    net = FakeNet({"A": '["1", "0"]', "B": '["1", "0"]', "Z": '["1", "0"]'}, open_=["H"], desc={"H": HIT_DAILY})
    out = tmp_path / "real" / "nearcert-forward.md"
    text = nf.run([tmp_path / "rec"], out, cache=tmp_path / "cache", now=END + 3600, net=net, n=1,
                  min_days=1, log=lambda *a: None, expect=["111"])
    assert out.exists() and text == out.read_text(encoding="utf-8")
    assert "检验 2" in text and "| 买入 |" in text and "模拟成交率" in text and "按目前速度" in text
    assert "不含队列竞争" in text
    # A won on Yes; B bought No and Yes won: a loss; the verdict pinned on the first market (n=1)
    verdict = (tmp_path / "real" / "nearcert-forward.verdict.md").read_text(encoding="utf-8")
    assert "前 1 个市场" in verdict
    judged = pd.read_csv(tmp_path / "real" / "nearcert-forward.trades.csv")
    assert judged["cid"].nunique() == 1
    ctrl = text.split("## 4 小时涨跌")[1].split("##")[0]
    assert "| 全部 | 1 | 1 | 1 |" in ctrl                # the 4-hour control: its own table only
    main = text.split("## 每份盈亏")[1].split("##")[0]
    assert "| 全部 | 2 | 2 | 2 |" in main
    # resolved Gamma records are cached; the open hit market is not
    cached = json.loads((tmp_path / "cache" / "gamma.json").read_text())
    assert set(cached) == {"A", "B", "Z"}
    # a market ending outside the recordings is counted, not bought
    assert "时点在最后一段录制之后" in text
    # second run: the verdict is read back, and resolved markets are not asked again
    net2 = FakeNet({})
    text2 = nf.run([tmp_path / "rec"], out, cache=tmp_path / "cache", now=END + 7200, net=net2, n=1,
                   min_days=1, log=lambda *a: None, expect=["111"])
    assert "已判定，不再重算" in text2
    assert not any("condition_ids=A" in u for u in net2.urls)
    # the recording's rows are in the ledger, all final: frozen
    led = nf.Ledger(tmp_path / "real" / "nearcert-forward.ledger")
    assert led.meta["111"]["frozen"] and {r["cid"] for r in led.rows("111") if r["status"] == "trade"} == {"A", "B", "Z"}


def _one_recording(root, name, ms, books):
    write_built(root / name / "built", books + filler(END - 3 * 3600, END + 120), ms, SPOT)


def test_ledger_keeps_rows_after_the_artifact_expires(tmp_path, monkeypatch):
    K = strike(0.97, T10, TAU10, SPOT)
    tok = "71321045679252212594626385532706912750332728571942532289631379312455583992563"  # a real-sized token id
    ms = [mkt("A", "above", lo=K, yes_token=tok), mkt("U", "updown_day")]
    root = tmp_path / "rec"
    _one_recording(root, "111", ms, [row(tok, T10 - 30, 0.96)])
    _one_recording(root, "222", [], [])                      # a later recording, nothing in scope
    out = tmp_path / "real" / "nearcert-forward.md"
    net = FakeNet({"A": '["1", "0"]'}, open_=["U"], desc={"U": UPDOWN_NEW})
    kw = dict(cache=tmp_path / "cache", net=net, n=1, min_days=1, log=lambda *a: None)
    text = nf.run([root], out, now=END + 3600, expect=["111", "222"], **kw)
    led = nf.Ledger(tmp_path / "real" / "nearcert-forward.ledger")
    # U's reference file is not published (data.binance.vision has nothing): 111 stays open, and holds the verdict
    assert not led.meta["111"]["frozen"] and led.meta["222"]["frozen"]
    assert "待定" in text and not (tmp_path / "real" / "nearcert-forward.verdict.md").exists()
    rows = {(r["cid"], r["check"]): r for r in led.rows("111")}
    assert rows[("A", 10)]["token"] == tok and rows[("A", 10)]["status"] == "trade"   # ids survive the CSV
    assert rows[("U", 10)]["status"] == "ref_pending"
    # a recording in the --expect list that was not read holds it too
    text = nf.run([root], out, now=END + 3600, expect=["111", "222", "333"], **kw)
    assert "333" in text and not (tmp_path / "real" / "nearcert-forward.verdict.md").exists()
    # a list that misses a recording read now is not trusted: nothing is marked expired, no verdict
    text = nf.run([root], out, now=END + 3600, expect=["222"], **kw)
    assert not nf.Ledger(tmp_path / "real" / "nearcert-forward.ledger").meta["111"]["frozen"]
    assert "清单" in text and not (tmp_path / "real" / "nearcert-forward.verdict.md").exists()
    # 111's artifact expires (no longer downloadable, not in --expect): its rows stay, the undetermined ones
    # become an explicit exclusion, and the verdict can be judged from the ledger
    import shutil
    shutil.rmtree(root / "111")
    text = nf.run([root], out, now=END + 7200, expect=["222"], **kw)
    led = nf.Ledger(tmp_path / "real" / "nearcert-forward.ledger")
    st = {(r["cid"], r["check"]): r["status"] for r in led.rows("111")}
    assert led.meta["111"]["frozen"] and st[("U", 10)] == "expired_pending" and st[("A", 10)] == "trade"
    assert "录制过期时仍待定" in text
    judged = pd.read_csv(tmp_path / "real" / "nearcert-forward.trades.csv", dtype={"token": str})
    assert set(judged["cid"]) == {"A"} and set(judged["token"]) == {tok}
    # frozen recordings are read back from the ledger, never loaded or recomputed again
    loads = []
    monkeypatch.setattr(nf.Rec, "load", classmethod(lambda cls, d: loads.append(d)))
    again = nf.run([root], out, now=END + 9000, expect=["222"], **kw)
    assert loads == [] and "已判定，不再重算" in again
    assert "| 全部 | 1 | 1 | 1 |" in again.split("## 每份盈亏")[1]


def test_dev_runs_use_no_ledger(tmp_path):
    _one_recording(tmp_path / "rec", "111", [mkt("A", "above", lo=80000.0)], [])
    out = tmp_path / "real" / "dev.md"
    nf.run([tmp_path / "rec"], out, cache=tmp_path / "cache", net=FakeNet({}), since="2026-10-04T00:00:00Z",
           log=lambda *a: None)
    assert out.exists() and not nf.ledger_dir(out).exists()


def test_run_without_recordings_writes_nothing(tmp_path):
    out = tmp_path / "x.md"
    assert nf.run([tmp_path], out, cache=tmp_path / "c", net=FakeNet({}), log=lambda *a: None) is None
    assert not out.exists()


def test_klines_close_extremes_pending_and_errors(tmp_path):
    import io
    import zipfile
    day = date(2026, 10, 1)
    t0 = ld.et_to_utc(day, 0)                                   # 04:00 UTC
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:                       # Binance's 1m layout, microsecond open times
        z.writestr("BTCUSDT-1m-2026-10-01.csv", "\n".join(
            f"{(t0 + 60 * k) * 1_000_000},84000.0,{84100.0 + k},{83900.0 - k},{84050.0 + k},1,0,0,0,0,0,0"
            for k in range(3)) + "\n")

    class Net:
        def __init__(self):
            self.urls = []

        def get(self, url, binary=False):
            self.urls.append(url)
            if "2026-10-01" in url:
                return buf.getvalue()
            if "2026-10-02" in url:
                raise nf.NotFound(url)
            raise RuntimeError("proxy reset")

    notes, errors = [], []
    k = nf.Klines(Net(), tmp_path, notes, errors)
    assert k.close(t0 + 60) == pytest.approx(84051.0)
    assert k.extremes(t0, t0 + 120) == pytest.approx((84102.0, 83898.0))
    assert all(np.isnan(k.extremes(t0, t0 + 180)))             # a candle missing: coverage incomplete
    assert np.isnan(k.close(t0 + 600))
    assert k.close(t0 + 86400) is None and errors == []         # not published yet: pending, not an error
    assert k.close(t0 + 2 * 86400) is None and len(errors) == 1  # a failed fetch blocks the verdict
    assert any("还没发布" in x for x in notes)
    assert (tmp_path / "klines1m" / "BTCUSDT-1m-2026-10-01.zip").exists()


def test_net_rate_limit():
    t = [0.0]
    waits = []

    def sleep(s):
        waits.append(s)
        t[0] += s

    net = nf.Net(rate=4.0, opener=lambda url, binary: url, sleep=sleep, clock=lambda: t[0])
    for i in range(5):
        net.get(str(i))
    assert waits == pytest.approx([0.25] * 4)
