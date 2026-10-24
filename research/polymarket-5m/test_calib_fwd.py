"""Tests for calib_fwd.py on synthetic built ladder tables (no network). The table helpers are those of
test_nearcert_fwd.py, with the bid side and the CLOB prints added."""
import gzip
import json
import math
import urllib.parse
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest
from scipy.stats import norm

import calib_fwd as cf
import ladder as ld
import nearcert_fwd as nf

D = date(2026, 10, 6)
END = ld.noon_et(D)                 # 16:00 UTC, after the forward cut-off
S0, DELTA = 85000.0, 1e-4
T60, T30 = END - 3600, END - 1800
TE60, TE30 = T60 + nf.ENTRY_LAG, T30 + nf.ENTRY_LAG
TAU60, TAU30 = END + 60 - T60, END + 60 - T30      # open12: the noon candle closes 60 s after the end
COLS = ["market_id", "ts", "recv", "bid", "ask", "bid_size", "ask_size", "tick", "depth99", "edge99"]
PCOLS = ["asset_id", "ts", "recv", "price", "size", "side"]
ABOVE_DESC = ('This market will resolve to "Yes" if the Binance 1 minute candle for BTC/USDT 12:00 in the ET timezone '
              '(noon) on the date specified in the title has a final "Close" price higher than the price specified in '
              'the title. Otherwise, this market will resolve to "No".')
RANGE_DESC = ('This market will resolve according to the final "Close" price of the Binance 1 minute candle for BTC/USDT '
              '12:00 in the ET timezone (noon) on the date specified in the title.')


# ------------------------------------------------------------------ synthetic built tables

def spot_rows(t0, t1):
    """One Binance trade per second at s + 0.5; the log price alternates 0, DELTA."""
    s = np.arange(int(t0), int(t1))
    return pd.DataFrame({"trade_ts": s + 0.5, "receive_ts": s + 0.55, "price": S0 * np.exp(DELTA * (s % 2))})


def s_before(t):
    return S0 * math.exp(DELTA * ((int(t) - 1) % 2))


def sigma_np(spot, t, w=3600):
    sec = np.floor(spot["trade_ts"].to_numpy()).astype(int)
    last = pd.Series(spot["price"].to_numpy(), index=sec).groupby(level=0).last()
    grid = last.reindex(range(sec.min(), sec.max() + 1)).ffill()
    return float(np.diff(np.log(grid.loc[t - w - 1:t - 1].to_numpy())).std(ddof=1))


SPOT = spot_rows(END - 3 * 3600, END + 120)


def strike(p, t=T60, tau=TAU60, spot=SPOT):
    """Strike K with Phi(ln(S/K) / (sigma sqrt(tau))) = p at checkpoint t."""
    return s_before(t) * math.exp(-norm.ppf(p) * sigma_np(spot, t) * math.sqrt(tau))


def p_at(K, t, tau):
    return norm.cdf(math.log(s_before(t) / K) / (sigma_np(SPOT, t) * math.sqrt(tau)))


def row(tok, ts, ask, size=100.0, bid=None, bid_size=50.0):
    return [tok, ts, ts + 0.05, (ask - 0.02) if bid is None else bid, ask, bid_size, size, 0.01, np.nan, np.nan]


def sell(tok, ts, price, size=100.0, side="SELL"):
    return [tok, ts, ts + 0.02, price, size, side]


def filler(t0, t1, step=1.0):
    """A busy token keeping the CLOB feed visibly alive."""
    return [row("FILL", float(x), 0.5) for x in np.arange(t0, t1, step)]


def mkt(cid, typ="above", lo=None, hi=None, end=END, day=D.isoformat(), **kw):
    m = {"slug": cid, "event_slug": f"slug-{typ}", "type": typ, "title": "", "question": kw.pop("question", ""),
         "lo": lo, "hi": hi, "end_ts": end, "start_ts": None, "ref_ts": None, "ref_price": None,
         "condition_id": cid, "yes_token": cid + "Y", "no_token": cid + "N", "day": day}
    m.update(kw)
    return m


def write_built(d, books, markets, spot=SPOT, prints=(), closes=()):
    d.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(books, columns=COLS).to_csv(d / "books.csv.gz", index=False)
    pd.DataFrame(list(prints), columns=PCOLS).to_csv(d / "clob_trades.csv.gz", index=False)
    (d / "markets.json").write_text(json.dumps(markets))
    with gzip.open(d / "binance_trades.jsonl.gz", "wt") as f:
        for r in spot.itertuples(index=False):
            f.write(json.dumps({"event": "BINANCE_WS_TRADE", "trade_ts": r.trade_ts, "receive_ts": r.receive_ts,
                                "price": r.price}) + "\n")
    pd.DataFrame({"at_s": list(closes)}).to_csv(d / "clob_closes.csv.gz", index=False)
    (d / "counts.json").write_text("{}")
    return d


def gamma_of(markets):
    return {m["condition_id"]: {"description": ABOVE_DESC if m["type"] == "above" else RANGE_DESC,
                                "createdAt": "2026-09-01T00:00:00Z"} for m in markets}


def rows_of(tmp_path, markets, books, prints=(), name="r1", fill_to=END + 120, closes=()):
    """One synthetic recording: (Rec, rows of every (market, checkpoint) as calib_fwd computes them)."""
    rec = cf.Rec.load(write_built(tmp_path / name / "built", books + filler(END - 3 * 3600, fill_to), markets,
                                  prints=prints, closes=closes))
    pts = nf.rec_points(rec, {m["condition_id"] for m in markets}, cf.CHECKS)
    return rec, pd.DataFrame([cf.checkpoint(rec, m, c, t, nf.SIGMA_S, gamma_of(markets)) for m, c, t in pts])


def at(rows, cid, check=60):
    r = rows[(rows["cid"] == cid) & (rows["check"] == check)]
    assert len(r) == 1
    return r.iloc[0]


def combined(rows, markets):
    return cf.one_per_side(cf.complete(nf.combine(rows.to_dict("records"), pd.DataFrame(markets), cf.CHECKS,
                                                  data_end=END + 120)))


# ------------------------------------------------------------------ band, type, one buy per side

def test_band_selection_is_85_to_95_on_nearcert_fwds_model(tmp_path):
    ms = [mkt("A", lo=strike(0.90)), mkt("B", lo=strike(0.10)), mkt("C", lo=strike(0.97)), mkt("E", lo=strike(0.80)),
          mkt("F", lo=strike(0.85) * (1 - 1e-9)),     # just above 0.85: inside (left-closed)
          mkt("G", lo=strike(0.95) * (1 - 1e-9)),     # just above 0.95: outside (right-open)
          mkt("H", lo=strike(0.95) * (1 + 1e-9))]     # just below 0.95: inside
    books = [row(c + s, T60 - 30, 0.90) for c in "ABCEFGH" for s in "YN"]
    rec, rows = rows_of(tmp_path, ms, books)
    a, b = at(rows, "A"), at(rows, "B")
    assert (a["status"], a["side"], a["token"], a["ask"]) == ("trade", "Yes", "AY", 0.90)
    assert a["p_fav"] == pytest.approx(0.90, abs=1e-9) and a["conv"] == "open12" and a["tau"] == TAU60
    assert (b["status"], b["side"], b["token"]) == ("trade", "No", "BN")
    assert {c: at(rows, c)["status"] for c in "CEFGH"} == {"C": "above", "E": "below", "F": "trade", "G": "above",
                                                           "H": "trade"}
    assert np.isnan(at(rows, "C")["ask"])                 # nothing is priced outside the band
    # the model, sides and hygiene are nearcert_fwd's own row; only the band differs (there A is 'below',
    # C a buy at 0.95-0.99)
    g = gamma_of(ms)
    m = {x.condition_id: x for x in rec.markets.itertuples(index=False)}
    na, nc = nf.checkpoint(rec, m["A"], 60, T60, gamma=g), nf.checkpoint(rec, m["C"], 60, T60, gamma=g)
    assert (na["status"], nc["status"]) == ("below", "trade")
    for k in ("S", "sigma", "tau", "p_yes", "p_fav", "side", "token", "conv", "fee_rate"):
        assert a[k] == na[k]


def test_only_above_markets(tmp_path):
    K = strike(0.90)
    ms = [mkt("A", lo=K), mkt("R", "range", lo=K, hi=K * 10), mkt("U", "updown_day"),
          mkt("H", "hit_up", lo=K, day=None, question="Will Bitcoin reach $X on October 6?"),
          mkt("Z", "updown_4h", day=None, ref_ts=END - 4 * 3600, ref_price=K)]
    d = write_built(tmp_path / "x" / "built", [row(t, T60 - 30, 0.90) for t in ("AY", "RY", "UY", "HN", "ZY")], ms)
    assert cf.scope_markets([d], cf.SINCE)["condition_id"].tolist() == ["A"]
    assert nf.scope_markets([d], cf.SINCE)["condition_id"].nunique() == 5      # nearcert_fwd's scope is wider
    out = tmp_path / "real" / "calib-forward.md"
    cf.run([tmp_path / "x"], out, cache=tmp_path / "cache", now=END + 3600, net=FakeNet({}), log=lambda *a: None,
           expect=["x"])
    led = cf.Ledger(tmp_path / "real" / "calib-forward.ledger")
    assert {r["cid"] for r in led.rows("x")} == {"A"} and set(led.markets["condition_id"]) == {"A"}
    assert {(r["cid"], r["check"]) for r in led.rows("x")} == {("A", 60), ("A", 30)}


def test_one_buy_per_market_and_side_at_the_earliest_checkpoint(tmp_path):
    K = strike(0.87)
    assert cf.BAND[0] <= p_at(K, T30, TAU30) < cf.BAND[1]          # in band at both checkpoints
    ms = [mkt("A", lo=K), mkt("B", lo=K)]
    books = [row("AY", T60 - 30, 0.90),
             row("BY", T60 - 30, 0.90, size=4.0), row("BY", T30 - 30, 0.91)]   # B: too thin at 60, buyable at 30
    _, rows = rows_of(tmp_path, ms, books)
    assert [at(rows, "A", c)["status"] for c in (60, 30)] == ["trade", "trade"]   # per checkpoint, before the rule
    cp = combined(rows, ms)
    st = cp.set_index(["cid", "check"])["status"].to_dict()
    assert st == {("A", 60): "trade", ("A", 30): "repeat", ("B", 60): "ask_size", ("B", 30): "trade"}
    e = nf.settle(cp, lambda c: {}, now=END + 3600)
    assert sorted(zip(e["cid"], e["check"])) == [("A", 60), ("B", 30)]
    assert e.set_index("cid").loc["B", "ask"] == 0.91


def test_one_per_side_rule_on_rows():
    r = lambda cid, check, side, status, bid_status="": {"cid": cid, "check": check, "t": float(END - 60 * check),
                                                          "side": side, "status": status, "bid_status": bid_status}
    cp = pd.DataFrame([r("X", 30, "No", "trade", "bid_unfilled"), r("X", 60, "Yes", "trade", "bid_filled"),
                       r("Y", 60, "Yes", "stale", "bid_open"), r("Y", 30, "Yes", "trade", "bid_filled"),
                       r("W", 60, "Yes", "trade", "bid_filled"), r("W", 30, "Yes", "trade", "bid_unfilled"),
                       r("V", 60, "Yes", "below"), r("V", 30, "Yes", "below")])
    got = cf.one_per_side(cp).set_index(["cid", "check"])[["status", "bid_status"]].apply(tuple, axis=1).to_dict()
    assert got == {("X", 60): ("trade", "bid_filled"), ("X", 30): ("trade", "bid_unfilled"),   # the other side
                   ("Y", 60): ("stale", "bid_open"), ("Y", 30): ("trade", "bid_filled"),
                   ("W", 60): ("trade", "bid_filled"), ("W", 30): ("repeat", "bid_repeat"),
                   ("V", 60): ("below", ""), ("V", 30): ("below", "")}


# ------------------------------------------------------------------ entry timing and the resting bid

def test_entry_and_bid_use_only_the_book_as_of_entry_time(tmp_path):
    K = strike(0.90)
    ms = [mkt(c, lo=K) for c in ("A1", "A2", "A3", "A4", "A5")]
    books = [row("A1Y", T60 - 30, 0.90, bid=0.88), row("A1Y", TE60 + 0.001, 0.50, bid=0.49),   # later row not read
             row("A2Y", T60 - 30, 0.90), row("A2Y", TE60, 0.91, bid=0.89),                       # stamped at te: read
             row("A3Y", TE60 + 0.2, 0.90),                                                        # nothing before te
             row("A4Y", T60 - 30, 0.985, bid=0.97),                                               # ask above 0.98
             row("A5Y", T60 - 30, 0.90, size=4.0, bid=0.01)]                                      # thin ask, bid < 0.02
    rec, rows = rows_of(tmp_path, ms, books)
    got = {c: tuple(at(rows, c)[["status", "ask", "bid_status", "bid"]]) for c in ("A1", "A2", "A4", "A5")}
    assert got == {"A1": ("trade", 0.90, "bid_unfilled", 0.88), "A2": ("trade", 0.91, "bid_unfilled", 0.89),
                   "A4": ("ask_band", 0.985, "bid_unfilled", 0.97), "A5": ("ask_size", 0.90, "bid_band", 0.01)}
    assert (at(rows, "A3")["status"], at(rows, "A3")["bid_status"]) == ("no_book", "bid_no_book")
    assert at(rows, "A1")["age"] == pytest.approx(30.5)
    # the same entries with every book row after te removed
    cut = pd.read_csv(tmp_path / "r1" / "built" / "books.csv.gz", dtype={"market_id": str})
    rec2 = cf.Rec("cut", cut[cut["ts"] <= TE60], rec.markets, SPOT)
    for c in ("A1", "A2", "A3", "A4", "A5"):
        assert rec2.entry(c + "Y", TE60)[:3] == pytest.approx(rec.entry(c + "Y", TE60)[:3], nan_ok=True)
        assert rec2.bid_entry(c + "Y", TE60) == pytest.approx(rec.bid_entry(c + "Y", TE60), nan_ok=True)
    # a CLOB disconnect just before the entry: neither order is placed
    _, dc = rows_of(tmp_path, ms[:1], books[:2], name="dc", closes=[TE60 - 3])
    assert (at(dc, "A1")["status"], at(dc, "A1")["bid_status"]) == ("stale", "bid_stale")


def test_resting_bid_fill_rules(tmp_path):
    K = strike(0.87)
    ms = [mkt("A", lo=K), mkt("B", lo=K)]
    books = [row("AY", T60 - 30, 0.90, bid=0.88), row("BY", T60 - 30, 0.90, bid=0.88)]
    prints = [sell("AY", TE60 - 0.1, 0.87),                  # before the order is live
              sell("AY", TE60 + 10, 0.88),                   # at the bid, not below it
              sell("AY", TE60 + 20, 0.87, size=4.0),         # under 5 shares
              sell("AY", TE60 + 30, 0.85, side="BUY"),       # a taker buy
              sell("AY", TE60 + 600, 0.87, size=5.0),        # the fill
              sell("AY", TE60 + 700, 0.80),
              sell("BY", TE60 + 1800 + 1, 0.80)]             # 1 s after B's 60-minute bid expired
    _, rows = rows_of(tmp_path, ms, books, prints)
    a = at(rows, "A")
    assert (a["bid_status"], a["bid"], a["fill_t"], a["fill_px"], a["fill_size"]) == \
        ("bid_filled", 0.88, TE60 + 600, 0.87, 5.0)
    assert at(rows, "B")["bid_status"] == "bid_unfilled"
    assert at(rows, "B", 30)["bid_status"] == "bid_filled" and at(rows, "B", 30)["fill_t"] == TE60 + 1801
    cp = combined(rows, ms)
    st = cp.set_index(["cid", "check"])["bid_status"].to_dict()
    assert st == {("A", 60): "bid_filled", ("A", 30): "bid_repeat", ("B", 60): "bid_unfilled", ("B", 30): "bid_filled"}
    # valued at the bid, no fee, held to the official result (A: Yes, B: No)
    res = {"A": {"closed": True, "outcomes": '["Yes", "No"]', "outcomePrices": '["1", "0"]'},
           "B": {"closed": True, "outcomes": '["Yes", "No"]', "outcomePrices": '["0", "1"]'}}
    f = cf.settle_bids(cp, lambda c: res, now=END + 3600).set_index("cid")
    assert f.loc["A", "pnl"] == pytest.approx(1 - 0.88) and f.loc["B", "pnl"] == pytest.approx(-0.88)
    assert (f["fee"] == 0).all() and len(f) == 2
    # the taker buys are unaffected by the bid: A bought once at the ask, with the fee
    e = nf.settle(cp, lambda c: res, now=END + 3600).set_index("cid")
    assert e.loc["A", "pnl"] == pytest.approx(1 - 0.90 - nf.FEE * 0.90 * 0.10)


def test_resting_bid_window_not_recorded_is_open(tmp_path):
    ms = [mkt("A", lo=strike(0.87))]
    _, rows = rows_of(tmp_path, ms, [row("AY", T60 - 30, 0.90, bid=0.88)], fill_to=T60 + 900)
    assert at(rows, "A")["bid_status"] == "bid_open"        # the book feed ends 15 min into the 30
    assert (at(rows, "A", 30)["status"], at(rows, "A", 30)["bid_status"]) == ("stale", "bid_stale")
    # a fill seen before the recording ends still counts
    _, rows = rows_of(tmp_path, ms, [row("AY", T60 - 30, 0.90, bid=0.88)], [sell("AY", TE60 + 60, 0.85)],
                      name="r2", fill_to=T60 + 900)
    assert at(rows, "A")["bid_status"] == "bid_filled"


# ------------------------------------------------------------------ verdict at the 40-market gate

def entry(cid, t, ask=0.10, official=1.0, side="Yes", check=60, run="r1"):
    won = official if side == "Yes" else 1 - official
    fee = nf.FEE * ask * (1 - ask)
    return {"run": run, "cid": cid, "kind": "above", "type": "above", "check": check, "t": float(t),
            "end": float(t + 60 * check), "status": "trade", "side": side, "ask": ask, "size": 100.0,
            "p_fav": 0.9, "age": 1.0, "fee_rate": nf.FEE, "official": official, "won": won, "fee": fee,
            "pnl": won - ask - fee}


def test_pinned_verdict_at_the_40_market_gate(tmp_path):
    assert cf.N_VERDICT == 40
    out = tmp_path / "real" / "calib-forward.md"
    pinned = tmp_path / "real" / "calib-forward.verdict.md"
    # 41 markets over only 2 UTC days (no day minimum in CALIB.md); market 41 comes after the gate
    rows = [entry(f"M{i:02d}", T60 + 1800 * i) for i in range(41)]            # 10-06 15:00 .. 10-07 11:00 UTC
    rows.append(entry("M00", T30, check=30, side="No", official=0.0))       # a second buy of M00 (the other side)
    e = pd.DataFrame(rows)
    covered = float(e["end"].max() + 1)
    # 39 markets: not yet
    first39 = e[e["cid"].isin({f"M{i:02d}" for i in range(39)})]
    assert "不到 40 个" in cf.judge(out, first39, first39, covered) and not pinned.exists()
    # 40 buys in 20 markets: the gate counts markets, not buys
    twice = pd.DataFrame([entry(f"M{i:02d}", T60 + 1800 * i) for i in range(20)] +
                         [entry(f"M{i:02d}", T30 + 1800 * i, check=30) for i in range(20)])
    assert "不到 40 个" in cf.judge(out, twice, twice, covered) and not pinned.exists()
    # what holds it: a chosen market not resolved, an undetermined checkpoint, a blocker, recordings too short
    late = e.copy()
    late.loc[late["cid"] == "M05", "official"] = np.nan
    assert "还没结算" in cf.judge(out, late, e, covered) and not pinned.exists()
    cp = pd.concat([e, pd.DataFrame([{"cid": "Q", "kind": "above", "t": T60 - 5, "end": END, "status": "gamma_na"}])])
    assert "待定" in cf.judge(out, e, cp, covered) and not pinned.exists()
    assert "清单" in cf.judge(out, e, e, covered, blockers=["没有可下载录制的清单（--expect）"]) and not pinned.exists()
    assert "录制" in cf.judge(out, e, e, T60) and not pinned.exists()
    assert "开发运行" in cf.judge(out, e, e, covered, design=False) and not pinned.exists()
    # a pending market after the 40th does not hold it
    after = e.copy()
    after.loc[after["cid"] == "M40", "official"] = np.nan
    v = cf.judge(out, after, after, covered)
    assert pinned.exists() and "CALIB.md" in v and "前 40 个有买入市场，41 笔" in v and "2 个 UTC 日" in v
    assert "→ 通过" in v and "99%" in v and "精确" in v
    judged = pd.read_csv(tmp_path / "real" / "calib-forward.trades.csv")
    assert judged["cid"].nunique() == 40 and "M40" not in set(judged["cid"]) and (judged["cid"] == "M00").sum() == 2
    text = pinned.read_text(encoding="utf-8")
    # later data never changes it
    worse = e.assign(official=0.0, won=0.0, pnl=-1.0)
    assert cf.judge(out, worse, worse, covered) == text.strip() + "（已判定，不再重算）"
    assert pinned.read_text(encoding="utf-8") == text


def test_verdict_needs_all_three_conditions(tmp_path):
    days = lambda i: T60 + (i % 5) * 86400 + i
    lose = pd.DataFrame([entry(f"L{i:02d}", days(i), official=0.0) for i in range(40)])
    cov = float(lose["end"].max() + 1)
    assert "没通过" in cf.judge(tmp_path / "a.md", lose, lose, cov)
    # all 40 won at 0.90: P&L and the day bound are > 0, but 0.906^40 = 0.02 is no evidence at 1%
    fair = pd.DataFrame([entry(f"F{i:02d}", days(i), ask=0.90) for i in range(40)])
    v = cf.judge(tmp_path / "b.md", fair, fair, cov)
    assert "没通过" in v and "下界 +" in v and "p = 0.0" in v
    one_day = pd.DataFrame([entry(f"O{i:02d}", T60 + i) for i in range(40)])   # the day bound needs 2 days
    assert "没通过" in cf.judge(tmp_path / "c.md", one_day, one_day, cov)
    win = pd.DataFrame([entry(f"W{i:02d}", days(i)) for i in range(40)])
    assert "→ 通过" in cf.judge(tmp_path / "d.md", win, win, cov)


# ------------------------------------------------------------------ end to end and the ledger

class FakeNet:
    """Gamma answers from a table (descriptions: the above wording)."""

    def __init__(self, resolved, open_=()):
        self.resolved, self.open, self.urls = resolved, set(open_), []

    def get(self, url, binary=False):
        self.urls.append(url)
        if "gamma-api" not in url:
            raise nf.NotFound(url)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        out = []
        for c in q.get("condition_ids", []):
            base = {"conditionId": c, "outcomes": '["Yes", "No"]', "description": ABOVE_DESC,
                    "createdAt": "2026-10-05T04:00:00Z"}
            if q.get("closed") == ["true"] and c in self.resolved:
                out.append({**base, "closed": True, "outcomePrices": self.resolved[c], "umaResolutionStatus": "resolved"})
            elif "closed" not in q and c in self.open:
                out.append({**base, "closed": False, "outcomePrices": '["0.5", "0.5"]'})
        return out


def test_run_end_to_end_and_ledger_reuse(tmp_path, monkeypatch):
    tok = "71321045679252212594626385532706912750332728571942532289631379312455583992563"  # a real-sized token id
    K = strike(0.87)
    ms = [mkt("A", lo=K, yes_token=tok, fee_rate=0.072), mkt("B", lo=strike(0.10)),
          mkt("P", lo=K, end=END + 86400, day=(D + timedelta(days=1)).isoformat()),   # after the recording
          mkt("R", "range", lo=K, hi=K * 10)]
    books = [row(tok, T60 - 30, 0.90, bid=0.88), row("BN", T60 - 30, 0.92, bid=0.89), row("RY", T60 - 30, 0.90)]
    prints = [sell(tok, TE60 + 60, 0.85), sell("BN", TE60 + 120, 0.80)]
    root = tmp_path / "rec"
    write_built(root / "111" / "built", books + filler(END - 3 * 3600, END + 120), ms, prints=prints)
    out = tmp_path / "real" / "calib-forward.md"
    net = FakeNet({"A": '["1", "0"]', "B": '["1", "0"]'})
    text = cf.run([root], out, cache=tmp_path / "cache", now=END + 3600, net=net, n=1, log=lambda *a: None,
                  expect=["111"])
    assert out.exists() and text == out.read_text(encoding="utf-8")
    assert "“高于”市场 3 个" in text and "结束前 60/30 分钟" in text
    for unit in ("有买入市场", "已结算", "待结算", "份数", "花费 $", "已结算盈亏 $", "每份 ¢"):
        assert unit in text
    taker = text.split("## 吃单买入")[1].split("##")[0]
    # A bought Yes at 60 (0.90 + 0.072 fee), its 30 is a repeat; B bought No at 60 and Yes won
    cost = 0.90 + 0.072 * 0.09 + 0.92 + nf.FEE * 0.92 * 0.08
    pnl = (1 - 0.90 - 0.072 * 0.09) + (0 - 0.92 - nf.FEE * 0.92 * 0.08)
    assert f"| 全部 | 2 | 2 | 0 | 2 | {cost:.2f} | {cost:.2f} | {pnl:+.2f} | {100 * pnl / 2:+.2f} |" in taker
    assert "同一市场同一边已买过 1 个" in taker              # A at 30 (B at 30 is above 0.95)
    bids = text.split("## 挂单买入")[1].split("##")[0]
    assert "| 全部 | 2 | 2 | 0 | 2 | 1.77 | 1.77 | " in bids and "成交 2" in bids and "同一市场同一边已在更早的时点成交" in bids
    verdict = (tmp_path / "real" / "calib-forward.verdict.md").read_text(encoding="utf-8")
    assert "CALIB.md" in verdict and "前 1 个有买入市场" in verdict
    # the ledger: rows of the 'above' markets with the resting-bid fields, frozen
    led = cf.Ledger(tmp_path / "real" / "calib-forward.ledger")
    assert led.meta["111"]["frozen"] and set(led.markets["condition_id"]) == {"A", "B", "P"}
    lr = {(r["cid"], r["check"]): r for r in led.rows("111")}
    assert set(lr) == {("A", 60), ("A", 30), ("B", 60), ("B", 30)}
    assert lr[("A", 60)]["token"] == tok and lr[("A", 60)]["bid_status"] == "bid_filled"
    assert lr[("A", 60)]["fill_t"] == pytest.approx(TE60 + 60)
    assert lr[("A", 30)]["status"] == "trade"                 # the ledger keeps raw rows; 'repeat' is applied later
    # second run: frozen recordings are read back from the ledger, never loaded or recomputed
    loads = []
    monkeypatch.setattr(cf.Rec, "load", classmethod(lambda cls, d: loads.append(d)))
    again = cf.run([root], out, cache=tmp_path / "cache", now=END + 7200, net=FakeNet({}), n=1, log=lambda *a: None,
                   expect=["111"])
    assert loads == [] and "已判定，不再重算" in again
    assert again.split("## 吃单买入")[1].split("##")[0] == taker
    assert again.split("## 挂单买入")[1].split("##")[0] == bids


def test_undetermined_rows_hold_the_verdict_until_the_artifact_expires(tmp_path):
    ms = [mkt("A", lo=strike(0.87))]
    root = tmp_path / "rec"
    write_built(root / "111" / "built", [row("AY", T60 - 30, 0.90)] + filler(END - 3 * 3600, END + 120), ms)
    out = tmp_path / "real" / "calib-forward.md"

    class NoGamma(FakeNet):
        def get(self, url, binary=False):
            return []

    kw = dict(cache=tmp_path / "cache", n=1, log=lambda *a: None)
    text = cf.run([root], out, now=END + 3600, net=NoGamma({}), expect=["111"], **kw)
    led = cf.Ledger(tmp_path / "real" / "calib-forward.ledger")
    assert not led.meta["111"]["frozen"] and {r["status"] for r in led.rows("111")} == {"gamma_na"}
    assert "不到 1 个" in text and not (tmp_path / "real" / "calib-forward.verdict.md").exists()
    # without the --expect list it never judges
    assert "清单" in cf.run([root], out, now=END + 3600, net=NoGamma({}), **kw)
    import shutil
    shutil.rmtree(root / "111")
    write_built(root / "222" / "built", filler(END, END + 60), [])
    text = cf.run([root], out, now=END + 7200, net=NoGamma({}), expect=["222"], **kw)
    led = cf.Ledger(tmp_path / "real" / "calib-forward.ledger")
    assert led.meta["111"]["frozen"] and {r["status"] for r in led.rows("111")} == {"expired_pending"}
    assert "录制已过期" in text


def test_dev_runs_use_no_ledger_and_never_judge(tmp_path):
    write_built(tmp_path / "rec" / "111" / "built", [row("AY", T60 - 30, 0.90)] + filler(END - 3 * 3600, END + 120),
                [mkt("A", lo=strike(0.87))])
    out = tmp_path / "real" / "dev.md"
    text = cf.run([tmp_path / "rec"], out, cache=tmp_path / "cache", net=FakeNet({}), since="2026-10-04T00:00:00Z",
                  n=1, now=END + 3600, log=lambda *a: None, expect=["111"])
    assert out.exists() and not nf.ledger_dir(out).exists() and "开发运行" in text
    assert not (tmp_path / "real" / "dev.verdict.md").exists()


def test_run_without_recordings_writes_nothing(tmp_path):
    out = tmp_path / "x.md"
    assert cf.run([tmp_path], out, cache=tmp_path / "c", net=FakeNet({}), log=lambda *a: None) is None
    assert not out.exists()
