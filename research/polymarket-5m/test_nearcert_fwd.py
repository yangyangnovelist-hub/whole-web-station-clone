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
    m = {"slug": cid, "event_slug": kw.pop("event_slug", f"slug-{typ}"), "type": typ, "title": "", "question": "",
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


def rows_of(tmp_path, markets, books, spot=SPOT, name="r1", gamma=None, ref_close=None, closes=(), checks=nf.CHECKS):
    rec = nf.Rec.load(write_built(tmp_path / name / "built", books + filler(END - 3 * 3600, END + 120), markets,
                                  spot, closes))
    rows = pd.DataFrame(nf.rec_rows(rec, {m["condition_id"] for m in markets}, checks, nf.SIGMA_S, ref_close, gamma))
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

    ms = [mkt("A", "above", lo=K), mkt("R", "range", lo=a, hi=b), mkt("U", "updown_day")]
    _, rows = rows_of(tmp_path, ms, [], ref_close=ref_close)
    sd = sigma_np(SPOT, T10) * math.sqrt(TAU10)
    S = s_before(T10)
    r = at10(rows, "A")
    assert r["tau"] == TAU10 and r["S"] == pytest.approx(S)
    assert r["p_yes"] == pytest.approx(norm.cdf(math.log(S / K) / sd), abs=1e-12)
    assert r["p_yes"] == pytest.approx(0.97, abs=1e-9)
    assert at10(rows, "R")["p_yes"] == pytest.approx(norm.cdf(math.log(S / a) / sd) - norm.cdf(math.log(S / b) / sd),
                                                     abs=1e-12)
    assert at10(rows, "U")["p_yes"] == pytest.approx(norm.cdf(math.log(S / ref) / sd), abs=1e-12)
    # the reference is the previous ET day's open12 candle (opens at noon ET), not the recorder's close12
    assert set(calls) == {rs.noon_open(D - timedelta(days=1), "open12")} == {ld.noon_et(D - timedelta(days=1))}


def test_updown_day_reference_pending_and_end_mismatch(tmp_path):
    ms = [mkt("U", "updown_day"), mkt("X", "above", lo=80000.0, end=END + 3600)]
    _, rows = rows_of(tmp_path, ms, [], ref_close=lambda o: None)
    assert at10(rows, "U")["status"] == "ref_pending"
    assert set(rows.loc[rows["cid"] == "X", "status"]) == {"end_mismatch"}


def test_hit_reflection_and_high_so_far(tmp_path):
    tau = END - T10
    sd = sigma_np(SPOT, T10) * math.sqrt(tau)
    S = s_before(T10)
    K = S * math.exp(-norm.ppf(0.015) * sd)   # 2 Phi(ln(S/K)/sd) = 0.03 -> No favoured at 0.97
    first = float(SPOT["trade_ts"].iloc[0])
    start = END - 12 * 3600                   # the window began before the recording
    pre = dict(start_ts=start, pre_at=first - 2, event_slug="what-price-will-bitcoin-hit-on-october-6-2026")
    Kd = S * math.exp(norm.ppf(0.015) * sd)
    ms = [mkt("H", "hit_up", lo=K, pre_high=K - 50, pre_low=80000.0, **pre),
          mkt("H2", "hit_up", lo=K, pre_high=K + 1, pre_low=80000.0, **pre),                 # reached before
          mkt("H3", "hit_up", lo=K, pre_high=K - 50, pre_low=80000.0, **{**pre, "pre_at": T10 + 5}),  # look-ahead
          mkt("H4", "hit_up", lo=K, start_ts=start),                                          # no pre_*
          mkt("H5", "hit_up", lo=S0 * math.exp(DELTA / 2), pre_high=S0, pre_low=S0, **pre),   # a trade reached it
          mkt("H6", "hit_up", lo=K, start_ts=T10 - 1800),                                     # window inside rec
          mkt("H7", "hit_up", lo=K, pre_high=K - 50, pre_low=80000.0, **{**pre, "pre_at": first - 60}),  # hole
          mkt("C1", "hit_up", lo=K, pre_high=K + 1, pre_low=80000.0, **pre),                 # from creation
          mkt("C2", "hit_up", lo=K, pre_high=K - 50, pre_low=80000.0, **pre),
          mkt("L", "hit_down", hi=Kd, pre_high=90000.0, pre_low=Kd + 50, **pre),
          mkt("G", "hit_up", lo=K, pre_high=K - 50, pre_low=80000.0, **pre)]
    for m in ms:
        m["end_ts"], m["day"] = END, None
    plain = {"description": "between 12:00 AM ET and 11:59 PM ET", "createdAt": "2026-10-05T04:00:01Z"}
    creation = {"description": "... from the creation of this market ...", "createdAt": "2026-10-06T06:00:30Z"}
    gamma = {m["condition_id"]: plain for m in ms if m["condition_id"] != "G"}
    gamma["C1"] = gamma["C2"] = creation
    books = [row(c + "N", T10 - 20, 0.96) for c in ("H", "H6", "C2", "L")]
    _, rows = rows_of(tmp_path, ms, books, gamma=gamma)
    r = at10(rows, "H")
    assert r["tau"] == tau
    assert r["p_yes"] == pytest.approx(2 * norm.cdf(math.log(S / K) / sd), abs=1e-12)
    assert (r["status"], r["side"], r["token"], r["ask"]) == ("trade", "No", "HN", 0.96)
    assert r["kind"] == "hit_daily"
    assert at10(rows, "L")["p_yes"] == pytest.approx(2 * norm.cdf(math.log(Kd / S) / sd), abs=1e-12)
    assert at10(rows, "L")["status"] == "trade"
    st = {c: at10(rows, c)["status"] for c in ("H2", "H3", "H4", "H5", "H6", "H7", "C1", "C2", "G")}
    assert st == {"H2": "decided", "H3": "hit_unknown", "H4": "hit_unknown", "H5": "decided", "H6": "trade",
                  "H7": "hit_unknown", "C1": "hit_unknown", "C2": "trade", "G": "gamma_na"}


def test_hit_kinds_from_event_slug():
    assert nf.kind_of("hit_up", "what-price-will-bitcoin-hit-on-october-6-2026") == "hit_daily"
    assert nf.kind_of("hit_down", "what-price-will-bitcoin-hit-in-october-2026") == "hit_monthly"
    assert nf.kind_of("hit_up", "what-price-will-bitcoin-hit-october-5-11-2026") == "hit_weekly"
    assert nf.kind_of("range", None) == "range"


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
    now = covered + 86400
    # too few Binance-settled markets (the 4-hour control does not count)
    assert "不到 4 个" in nf.judge(out, e, cp, covered, n=4, min_days=3) and not pinned.exists()
    # the first 3 are not all resolved yet
    late = e.copy()
    late.loc[late["cid"] == "B", "official"] = np.nan
    assert "还没结算" in nf.judge(out, late, cp, covered, n=3, min_days=3) and not pinned.exists()
    # an earlier checkpoint still undetermined
    cp2 = pd.concat([cp, pd.DataFrame([{"cid": "U", "kind": "updown_day", "t": T10 - 5, "end": END,
                                        "status": "ref_pending"}])])
    assert "待定" in nf.judge(out, e, cp2, covered, n=3, min_days=3, now=END + 86400) and not pinned.exists()
    assert "待定" not in nf.judge(tmp_path / "old.md", e, cp2, covered, n=3, min_days=3,
                                   now=END + 4 * 86400)
    assert (tmp_path / "old.verdict.md").exists()       # an old undetermined checkpoint stays skipped
    # recordings not yet past the markets' end
    assert "录制" in nf.judge(out, e, cp, T10, n=3, min_days=3) and not pinned.exists()
    # development runs never judge
    assert "开发运行" in nf.judge(out, e, cp, covered, n=3, min_days=3, design=False) and not pinned.exists()
    v = nf.judge(out, e, cp, covered, n=3, min_days=3, now=now)
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
    v = nf.judge(tmp_path / "a.md", pd.DataFrame(lose), pd.DataFrame(lose), covered, n=10,
                 now=covered + 86400)
    assert "没通过" in v
    priced_fair = [entry(c, T10 + 86400 * i, ask=0.95) for i, c in enumerate("ABCDEFGHIJ")]
    covered = max(r["end"] for r in priced_fair) + 1
    v = nf.judge(tmp_path / "b.md", pd.DataFrame(priced_fair), pd.DataFrame(priced_fair), covered, n=10,
                 now=covered + 86400)
    assert "没通过" in v and "精确" in v
    win = [entry(c, T10 + 86400 * i, ask=0.10) for i, c in enumerate("ABCDEFGHIJ")]
    covered = max(r["end"] for r in win) + 1
    v = nf.judge(tmp_path / "c.md", pd.DataFrame(win), pd.DataFrame(win), covered, n=10,
                 now=covered + 86400)
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
    """Gamma answers from a table; data.binance.vision has nothing."""

    def __init__(self, resolved, open_=()):
        self.resolved, self.open, self.urls = resolved, set(open_), []

    def get(self, url, binary=False):
        self.urls.append(url)
        if "gamma-api" not in url:
            raise nf.NotFound(url)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        out = []
        for c in q.get("condition_ids", []):
            base = {"conditionId": c, "outcomes": '["Yes", "No"]', "description": "12:00 AM ET to 11:59 PM ET",
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
              day=None, event_slug="what-price-will-bitcoin-hit-on-october-6-2026"),
          mkt("Z", "updown_4h", end=END, day=None, ref_ts=END - 4 * 3600, ref_price=strike(0.97, T10, END - T10, SPOT))]
    books = [row("AY", T10 - 30, 0.96), row("BN", T10 - 30, 0.95), row("ZY", T10 - 30, 0.96)] + filler(END - 3 * 3600,
                                                                                                         END + 120)
    write_built(tmp_path / "rec" / "111" / "built", books, ms, SPOT)
    net = FakeNet({"A": '["1", "0"]', "B": '["1", "0"]', "Z": '["1", "0"]'}, open_=["H"])
    out = tmp_path / "real" / "nearcert-forward.md"
    text = nf.run([tmp_path / "rec"], out, cache=tmp_path / "cache", now=END + 3600, net=net, n=1,
                  min_days=1, log=lambda *a: None)
    assert out.exists() and text == out.read_text(encoding="utf-8")
    assert "检验 2" in text and "| 买入 |" in text and "模拟成交率" in text
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
                   min_days=1, log=lambda *a: None)
    assert "已判定，不再重算" in text2
    assert not any("condition_ids=A" in u for u in net2.urls)


def test_run_without_recordings_writes_nothing(tmp_path):
    out = tmp_path / "x.md"
    assert nf.run([tmp_path], out, cache=tmp_path / "c", net=FakeNet({}), log=lambda *a: None) is None
    assert not out.exists()


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
