import gzip
import json
import math

import numpy as np
import pandas as pd
import pytest

import binary as bo
import latency as lt
import rt_fwd as rf

P = 80_000.0
IND = int(pd.Timestamp("2026-10-02 00:00", tz="UTC").timestamp())  # an independent-check market start
FWD = int(pd.Timestamp("2026-10-05 00:00", tz="UTC").timestamp())  # a forward market start
BASE = (0.49, 0.50, 40.0, 25.0)


def _ts(s):
    return int(pd.Timestamp(s, tz="UTC").timestamp())


def _px(bp):
    return P * math.exp(bp * 1e-4)


def _base(s0, lo=-30, hi=300, level=lambda x: 0.0, gaps=(), extra=()):
    """Binance prints (trade_ts, receive_ts, price): one a second, printed at x.00 and received at
    x.05, at log level `level(x)` bp (seconds in `gaps` have none), plus `extra` prints given as
    (trade, receipt, bp) relative to s0. Sorted by trade time, as latency.load returns them."""
    rows = [(s0 + x, s0 + x + 0.05, _px(level(x))) for x in range(lo, hi) if x not in gaps]
    rows += [(s0 + a, s0 + b, _px(bp)) for a, b, bp in extra]
    return pd.DataFrame(sorted(rows), columns=["trade_ts", "receive_ts", "price"])


def _markets(starts, winners=None):
    winners = winners or ["Up"] * len(starts)
    return pd.DataFrame({"market_id": [f"m{s}" for s in starts], "start_ts": starts, "winner": winners})


def _flicker(s0, changes=(), lo=-5, hi=305, extra=()):
    """An Up book (ts, bid, ask, bid size, ask size) whose bid dips 1c at every whole second and is
    back at x.2, so a fill at x.5 sees a top of book changed 0.3 s before; `changes` = [(from s,
    state)] relative to s0; `extra` rows (relative time, state) added as they are."""
    def state(x):
        st = BASE
        for at, s in changes:
            if x >= at:
                st = s
        return st
    out = []
    for x in range(lo, hi):
        b, a, bs, az = state(x)
        out += [(s0 + x, b - 0.01, a, bs, az), (s0 + x + 0.2, *state(x + 0.2))]
    out += [(s0 + x, *s) for x, s in extra]
    return out


def _book(by_market, closes=None):
    parts = []
    for mid, rows in by_market.items():
        df = pd.DataFrame(rows, columns=["ts", "bid", "ask", "bid_size", "ask_size"])
        df["market_id"] = mid
        parts.append(df)
    books = pd.concat(parts, ignore_index=True)
    books["recv"] = books["ts"] + 0.01
    return lt.Book(books.sort_values(["market_id", "ts"], kind="stable").reset_index(drop=True), closes)


def _trig(mid, s0, ts, side="Up", winner="Up"):
    return pd.DataFrame({"market_id": mid, "start_ts": s0, "winner": winner, "t": [s0 + x for x in ts],
                         "side": side, "dx": 5e-4, "t_ref": np.nan})


def _fires(spot, markets, rule):
    _, w, bp = rf.RULES[rule]
    rr, lp = rf.receipt_order(spot)
    return rf.triggers(markets, rr, lp, w, bp)


def test_triggers_use_the_receipt_time():
    """A +5 bp print made at 100.30 but received at 101.70 triggers at its receipt (decision at
    102); a print made before the next market's start but received after it belongs to that
    market; one made with 16.4 s left but received with 15.8 s left opens nothing."""
    m1, m2 = IND, IND + 300
    spot = _base(m1, hi=600, extra=[(100.30, 101.70, 5.0), (283.60, 284.20, 5.0), (299.90, 300.40, 5.0)])
    j = _fires(spot, _markets([m1, m2]), "R1")
    assert j["market_id"].tolist() == [f"m{m1}", f"m{m2}"]
    assert np.allclose(j["t"] - m1, [101.70, 300.40]) and (j["side"] == "Up").all()
    assert np.isclose(j["t_ref"].iloc[0] - m1, 100.05)  # the last print received at or before t - 1
    assert _fires(spot, _markets([m1, m2]), "R2").empty  # 5 bp is below R2's 8 bp
    tr = rf.trades(j.iloc[:1], _book({f"m{m1}": _flicker(m1)}), "R1")
    r = tr[tr["arm"] == "rule"].iloc[0]
    assert r["decision"] == m1 + 102 and r["fill_t"] == m1 + 102.5
    # by exchange time the same prints would trigger at 100.30: receipt order is what is used
    by_trade = spot.assign(receive_ts=spot["trade_ts"])
    assert np.allclose(_fires(by_trade, _markets([m1]), "R1")["t"] - m1, [100.30, 283.60])


def test_reference_w1_against_w3():
    """R1 compares with the last print received >= 1 s earlier, R2 with >= 3 s earlier, both at
    most 5 s old: a lone +5 bp print fires R1 only; a 3 bp-a-second ramp fires R2 only (9 bp over
    3 s); a print exactly 1 s after a dip is measured from the dip (4.5 bp, not 3.5 bp from the
    print before it); after a 6 s gap nothing fires, after a 4.5 s gap both do."""
    s0 = IND

    def level(x):
        return 0.0 if x <= 150 else {151: 3.0, 152: 6.0}.get(x, 9.0 if x <= 250 else 19.0 if x <= 260 else 29.0)
    spot = _base(s0, level=level, gaps=set(range(251, 256)) | set(range(261, 265)),
                 extra=[(100.50, 100.55, 5.0), (200.20, 200.25, 8.0), (201.20, 201.25, 12.5), (264.50, 264.55, 29.0)])
    mk = _markets([s0])
    r1, r2 = _fires(spot, mk, "R1"), _fires(spot, mk, "R2")
    assert np.allclose(r1["t"] - s0, [100.55, 201.25, 264.55])
    assert np.allclose(r1["t_ref"] - s0, [99.05, 200.25, 260.05])
    assert np.allclose(r1["dx"], [5e-4, 4.5e-4, 1e-3])
    assert np.allclose(r2["t"] - s0, [153.05, 264.55]) and np.allclose(r2["t_ref"] - s0, [150.05, 260.05])
    assert (r1["side"] == "Up").all() and (r2["side"] == "Up").all()
    # a move down buys Down
    down = _base(s0, extra=[(120.50, 120.55, -4.5)])
    assert _fires(down, mk, "R1")["side"].tolist() == ["Down"]


def test_ten_second_spacing_and_one_position_per_market():
    """Candidates at 100.25, 105.25, 108.75, 110.25, 115.25 and 121.25: kept 100.25 (Up ask 0.99,
    no fill), 110.25 (exactly 10 s later) and 121.25. 105.25 would fill (the ask is 0.50 from 104)
    but is dropped by the spacing, which counts from the last KEPT trigger whether it filled or not.
    The rule buys once per market, at 110.25; every kept fill is listed as a description."""
    s0 = IND
    ts = [100.25, 105.25, 108.75, 110.25, 115.25, 121.25]
    spot = _base(s0, extra=[(x - 0.05, x, 5.0) for x in ts])
    j = _fires(spot, _markets([s0]), "R1")
    assert np.allclose(j["t"] - s0, [100.25, 110.25, 121.25])
    book = _book({f"m{s0}": _flicker(s0, [(-5, (0.97, 0.99, 40.0, 25.0)), (104, BASE)])})
    tr = rf.trades(j, book, "R1")
    rule = tr[tr["arm"] == "rule"]
    assert len(rule) == 1 and rule["t"].iloc[0] == s0 + 110.25 and rule["ask"].iloc[0] == 0.50
    assert np.allclose(tr.loc[tr["arm"] == "every", "t"] - s0, [110.25, 121.25])
    opp = tr[tr["arm"] == "opposite"]  # Down's ask at 101.5 is 1 - 0.97 = 0.03: it fills at the first
    assert len(opp) == 1 and opp["t"].iloc[0] == s0 + 100.25 and np.isclose(opp["ask"].iloc[0], 0.03)
    rnd = tr[tr["arm"] == "random"]
    assert len(rnd) == 1 and 1 <= rnd["decision"].iloc[0] - s0 <= 284
    assert float(rnd["decision"].iloc[0]).is_integer() and rnd["fill_t"].iloc[0] == rnd["decision"].iloc[0] + 0.5
    # the spacing is per rule: R2 has its own chain (8 bp spikes fire both rules here)
    spot8 = _base(s0, extra=[(x - 0.05, x, 9.0) for x in (100.25, 105.25, 112.25)])
    assert np.allclose(_fires(spot8, _markets([s0]), "R2")["t"] - s0, [100.25, 112.25])


def test_no_trigger_with_fifteen_seconds_or_less_left():
    """Decision at the first whole second after the receipt must leave more than 15 s: a print
    received at 283.95 (decision 284, 16 s left) opens, one at 284.00 (decision 285) does not."""
    s0 = IND
    for x, n in ((283.95, 1), (284.00, 0), (284.5, 0)):
        spot = _base(s0, extra=[(x - 0.01, x, 5.0)])
        assert len(_fires(spot, _markets([s0]), "R1")) == n


def test_decision_and_fill_timing_use_no_later_row():
    """Received at 100.30: decided at 101, filled at 101.5 at the ask of the row stamped exactly
    101.5 (0.60), never at the row stamped 1 ms later; received exactly at 150.00: decided at 151
    (not 150), so the 0.40 ask shown at 150.5 is not the one bought. Rows stamped after the fill,
    cheap or dear, change nothing."""
    s0 = IND
    got = []
    for late in (0.10, 0.95):
        rows = _flicker(s0, [(150.1, (0.39, 0.40, 40.0, 25.0)), (151.1, BASE)],
                        extra=[(101.5, (0.59, 0.60, 40.0, 25.0)), (101.501, (late - 0.01, late, 40.0, 25.0)),
                               (151.501, (late - 0.01, late, 40.0, 25.0))])
        tr = rf.trades(_trig(f"m{s0}", s0, [100.30, 150.0]), _book({f"m{s0}": rows}), "R1")
        ev = tr[tr["arm"] == "every"].reset_index(drop=True)
        assert (ev["decision"] - s0).tolist() == [101.0, 151.0] and (ev["fill_t"] - s0).tolist() == [101.5, 151.5]
        assert ev["ask"].tolist() == [0.60, 0.50]
        assert np.isclose(ev["pnl"].iloc[0], 1 - 0.60 - bo.taker_fee(0.60))
        got.append(tr)
    assert got[0].equals(got[1])
    # Down: ask = 1 - Up bid at the fill, with the bid's size
    tr = rf.trades(_trig(f"m{s0}", s0, [100.30], side="Down", winner="Up"),
                   _book({f"m{s0}": _flicker(s0, [(101.1, (0.30, 0.32, 7.0, 25.0))])}), "R1")
    r = tr[tr["arm"] == "rule"].iloc[0]
    assert np.isclose(r["ask"], 0.70) and r["size"] == 7.0 and np.isclose(r["pnl"], -0.70 - bo.taker_fee(0.70))


def _sized(s0, change_at, after_f=True):
    """Rows every 1/8 s with sizes changing on every row; the bid moves 0.48 -> 0.49 at change_at
    (and to 0.47 1/8 s after the 101.5 fill when after_f)."""
    rows = []
    for k in range(-40, 8 * 300):
        x = k / 8
        bid = 0.49 if x >= change_at else 0.48
        if after_f and x >= 101.625:
            bid = 0.47
        rows.append((s0 + x, bid, 0.50, 40.0 + k % 3, 25.0 + k % 2))
    return rows


def test_freshness_filter():
    """The fill snapshot's bid or ask must have changed within 1 s of the fill, from rows stamped
    at or before it: a change exactly 1 s before passes, 1.25 s before fails although size-only
    updates keep arriving every 1/8 s and a price changes 1/8 s after the fill."""
    s0, mid = IND, f"m{IND}"
    ok = _book({mid: _sized(s0, 100.5)})
    assert rf.changed_within(ok, mid, s0 + 101.5) and rf.fill(ok, mid, s0 + 101, "Up") == (0.50, 25.0 + 812 % 2)
    stale = _book({mid: _sized(s0, 100.25)})
    assert not rf.changed_within(stale, mid, s0 + 101.5) and rf.fill(stale, mid, s0 + 101, "Up") is None
    assert rf.changed_within(stale, mid, s0 + 101.625)  # the later change counts only from its own stamp
    # repeated identical rows are no change either
    rep = _book({mid: [(s0 + x / 8, 0.49 if x >= 804 else 0.48, 0.50, 40.0, 25.0) for x in range(-40, 2400)]})
    assert rf.changed_within(rep, mid, s0 + 101.5) and not rf.changed_within(rep, mid, s0 + 101.51 + 1.0)
    # the feed must be alive around the fill and the CLOB socket must not close across the order
    dead = _book({mid: [r for r in _sized(s0, 100.5, after_f=False) if r[0] <= s0 + 101.5]})
    assert rf.fill(dead, mid, s0 + 101, "Up") is None
    cut = _book({mid: _sized(s0, 100.5)}, closes=[s0 + 101.2])
    assert rf.fill(cut, mid, s0 + 101, "Up") is None and cut.cut == 1
    # price band and size
    for st in ((0.98, 0.99, 40.0, 25.0), (0.49, 0.50, 40.0, 4.0), (0.48, 0.01, 40.0, 25.0)):
        b = _book({mid: _flicker(s0, [(-5, st)])})
        assert rf.fill(b, mid, s0 + 101, "Up") is None


def test_segments():
    assert rf.segment(_ts("2026-10-01 02:25")) is None
    assert rf.segment(_ts("2026-10-01 02:30")) == "ind"
    assert rf.segment(_ts("2026-10-04 17:20")) == "ind"
    assert rf.segment(_ts("2026-10-04 17:25")) == "fwd"
    assert rf.segment(_ts("2026-10-04 17:20") + 1) == "fwd"


def _synthetic(n, rule="R1", seg="fwd", seed=0, mean=0.08):
    rng = np.random.default_rng(seed)
    t = FWD + rng.permutation(n) * 30.0
    pnl = mean + rng.normal(0, 0.3, n)
    return pd.DataFrame({"market_id": [f"m{i}" for i in range(n)], "start_ts": FWD, "rule": rule, "arm": "rule",
                         "t": t, "decision": t, "fill_t": t, "side": "Up", "winner": "Up", "ask": 0.5, "size": 25.0,
                         "won": 1.0, "fee": 0.0, "pnl": pnl, "coin": "btc", "run": "1", "segment": seg})


def test_pinned_verdict_at_300_trades(tmp_path):
    out = tmp_path / "real" / "rt-fwd.md"
    vfile, tfile = tmp_path / "real" / "rt-fwd.verdict.md", tmp_path / "real" / "rt-fwd.trades.csv"
    noise = pd.concat([_synthetic(400, seg="ind", seed=1), _synthetic(400, seed=2).assign(arm="random"),
                       _synthetic(400, seed=3).assign(arm="every")])
    t = pd.concat([_synthetic(299), noise])
    lines = rf.judge(t, out)
    assert not vfile.exists() and not tfile.exists()
    assert "前向段目前 299 笔，不到 300 笔，不判定" in lines[0] and "R2" in lines[1]
    t = pd.concat([_synthetic(340), noise])  # the first 300 by trigger time are judged
    lines = rf.judge(t, out)
    first = t[(t["arm"] == "rule") & (t["segment"] == "fwd")].sort_values("t").head(300)
    mu, se, z, p, n, k = rf.cluster(first)
    assert n == 300 and mu > 0 and p < rf.ALPHA
    assert lines[0].startswith("- R1 `w1-bp4-all-all10-settle`（前向段前 300 笔，300 个市场）：**通过**")
    assert f"{100 * mu:+.2f}¢ ±{100 * se:.2f}" in lines[0]
    saved = pd.read_csv(tfile)
    assert len(saved) == 300 and set(saved["rule"]) == {"R1"} and sorted(saved["t"]) == sorted(first["t"])
    fw = t.loc[(t["arm"] == "rule") & (t["segment"] == "fwd"), "t"]
    assert saved["t"].max() == fw.nsmallest(300).max() < fw.max()
    pinned = vfile.read_text(encoding="utf-8")
    # never recomputed: worse later data leaves the line and the trades as they are
    worse = t.assign(pnl=-1.0)
    lines = rf.judge(worse, out)
    assert lines[0] == pinned.splitlines()[0] + "（已判定，不再重算）"
    assert vfile.read_text(encoding="utf-8") == pinned and len(pd.read_csv(tfile)) == 300
    # R2 is judged when it reaches 300, appended without touching R1's line
    lines = rf.judge(pd.concat([worse, _synthetic(300, rule="R2", seed=4, mean=-0.05)]), out)
    text = vfile.read_text(encoding="utf-8")
    assert text.startswith(pinned) and text.splitlines()[1].startswith("- R2 ") and "**不通过**" in lines[1]
    saved = pd.read_csv(tfile)
    assert (saved["rule"] == "R1").sum() == 300 and (saved["rule"] == "R2").sum() == 300


def test_halves_must_both_be_positive(tmp_path):
    """A strongly positive mean whose second half is negative does not pass."""
    t = _synthetic(300, seed=5).sort_values("t").reset_index(drop=True)
    t["pnl"] = np.r_[np.full(150, 0.40), np.full(150, -0.01)] + np.random.default_rng(6).normal(0, 0.01, 300)
    mu, se, z, p, _, _ = rf.cluster(t)
    assert mu > 0 and p < 1e-6
    lines = rf.judge(t.sample(frac=1, random_state=1), tmp_path / "rt-fwd.md")
    assert "**不通过**" in lines[0] and "后 150 笔 -" in lines[0]


def _write(root, run, starts, spot, books, winners):
    """A recording directory like recording.LatencyFiles writes (Binance prints with their receipt)."""
    d = root / str(run) / "x" / "bundle-btc" / "latency"
    d.mkdir(parents=True)
    mids = [f"m{s}" for s in starts]
    pd.DataFrame({"market_id": mids, "start_ts": starts}).to_csv(
        d / "runtime_1000.market_registry.csv.gz", index=False, compression="gzip")
    pd.DataFrame({"market_id": mids, "winner": winners}).to_csv(
        d / "runtime_1000.market_outcomes.csv.gz", index=False, compression="gzip")
    rows = []
    for mid, rs in books.items():
        for t, bid, ask, bs, az in rs:
            rows.append({"market_id": mid, "source_ts": t, "receive_ts": t + 0.01, "best_bid": bid, "best_ask": ask,
                         "bids_json": f"[[{bid:.2f}, {bs}]]", "asks_json": f"[[{ask:.2f}, {az}]]"})
    pd.DataFrame(rows).sort_values("source_ts").to_csv(d / "runtime_1000.poly_probability_observations_v1.csv.gz",
                                                        index=False, compression="gzip")
    lines = [json.dumps({"event": "BINANCE_WS_TRADE", "trade_ts": float(a), "receive_ts": float(b), "price": float(p)})
             for a, b, p in spot.itertuples(index=False)]
    (d / "binance_trades.jsonl.gz").write_bytes(gzip.compress(("\n".join(lines) + "\n").encode()))


def _rec(root, run, starts, winners, book_from=-5):
    """Consecutive markets: in each, Binance steps up 10 bp at prints received at 100.30 and 150.30
    (both rules fire twice, the level carries over to the next market); the Up book (rows from
    `book_from` s) is 0.49 / 0.50 until 100.9, then 0.69 / 0.70."""
    def level(x):
        k, r = divmod(x, 300)
        return 20.0 * k + (0.0 if r < 101 else 10.0 if r < 151 else 20.0)
    extra = [e for i in range(len(starts)) for e in ((300 * i + 100.20, 300 * i + 100.30, 20.0 * i + 10.0),
                                                      (300 * i + 150.20, 300 * i + 150.30, 20.0 * i + 20.0))]
    spot = _base(starts[0], lo=-30, hi=300 * len(starts), level=level, extra=extra)
    books = {f"m{s}": _flicker(s, [(100.9, (0.69, 0.70, 40.0, 25.0))], lo=book_from) for s in starts}
    _write(root, run, starts, spot, books, winners)


def test_cli_end_to_end(tmp_path, monkeypatch):
    """Two recordings: 10-01 02:25 (before the test: left out), 02:30 .. 02:50 and 10-04 17:20
    (independent check), 17:25 .. 17:45 (forward). Every market buys Up at 0.70 at 101.5 once;
    with FWD_N = 3 the forward verdict is pinned on the first three and read back afterwards."""
    ind = [_ts("2026-10-01 02:25") + 300 * i for i in range(6)]
    fwd = [_ts("2026-10-04 17:20") + 300 * i for i in range(6)]
    w_ind = ["Up", "Up", "Down", "Up", "Up", "Down"]
    w_fwd = ["Up", "Up", "Up", "Down", "Up", "Up"]
    _rec(tmp_path / "rec", 1, ind, w_ind)
    _rec(tmp_path / "rec", 2, fwd, w_fwd)
    t, markets, trig, notes = rf.run(tmp_path / "rec")
    assert markets.set_index("market_id")["segment"].to_dict() == {
        **{f"m{s}": "ind" for s in ind[1:] + fwd[:1]}, **{f"m{s}": "fwd" for s in fwd[1:]}}
    for rule in rf.RULES:
        s = t[t["rule"] == rule]
        for arm in ("rule", "opposite", "random"):
            assert s[s["arm"] == arm].groupby("market_id").size().eq(1).all() and (s["arm"] == arm).sum() == 11
        assert (s["arm"] == "every").sum() == 22
        r = s[s["arm"] == "rule"]
        assert (r["ask"] == 0.70).all() and (r["side"] == "Up").all() and np.allclose(r["t"] - r["start_ts"], 100.30)
        o = s[s["arm"] == "opposite"]
        assert np.allclose(o["ask"], 0.31) and (o["side"] == "Down").all()
    assert trig.groupby(["rule", "segment"]).size().to_dict() == {("R1", "fwd"): 10, ("R1", "ind"): 12,
                                                                   ("R2", "fwd"): 10, ("R2", "ind"): 12}
    monkeypatch.setattr(rf, "FWD_N", 3)
    out = tmp_path / "real" / "rt-fwd.md"
    rf.main([str(tmp_path / "rec"), "--out", str(out)])
    text = out.read_text(encoding="utf-8")
    won = np.array([w == "Up" for w in w_ind[1:] + w_fwd[:1]], float)
    pnl = won - 0.70 - bo.taker_fee(0.70)
    mu = pnl.mean()
    se = math.sqrt(((pnl - mu) ** 2).sum() * 6 / 5) / 6
    assert f"| {rf.ARMS['rule']} | 6 | 6 | 288.0 | {100 * mu:+.2f}¢ ±{100 * se:.2f}（t {mu / se:+.1f}） | 66.7% | 0.700 |" in text
    assert f"{5 * pnl.sum() / (6 * 300 / 86400):+,.2f} | {20 * pnl.sum() / (6 * 300 / 86400):+,.2f} |" in text
    assert "有盘口的市场 6 个（开始于 10-01 02:30 – 10-04 17:20 UTC，约 0.02 天）" in text
    assert "有盘口的市场 5 个（开始于 10-04 17:25 – 10-04 17:45 UTC" in text
    assert "保留的触发 12 次，其中顺变动方向能成交的 12 次" in text
    v = (tmp_path / "real" / "rt-fwd.verdict.md").read_text(encoding="utf-8")
    assert v.count("\n") == 2 and "- R1 `w1-bp4-all-all10-settle`（前向段前 3 笔，3 个市场）" in v and "- R2 " in v
    assert "录制段 1 个：2" in v and len(pd.read_csv(tmp_path / "real" / "rt-fwd.trades.csv")) == 6
    assert any("no CLOB disconnect log" in ln for ln in text.splitlines())
    rf.main([str(tmp_path / "rec"), "--out", str(out)])
    assert (tmp_path / "real" / "rt-fwd.verdict.md").read_text(encoding="utf-8") == v
    assert out.read_text(encoding="utf-8").count("（已判定，不再重算）") == 2


def test_market_in_two_recordings_keeps_its_earliest_trade(tmp_path):
    """Recording 1 has the market's book only from 120 s (its first trigger cannot fill there, so it
    buys at 150.30), recording 2 all of it (buys at 100.30): pooled, one trade per rule and arm, the
    earliest."""
    s = [_ts("2026-10-02 00:00")]
    _rec(tmp_path / "rec", 1, s, ["Up"], book_from=120)
    _rec(tmp_path / "rec", 2, s, ["Up"])
    t, markets, trig, _ = rf.run(tmp_path / "rec")
    assert len(markets) == 1 and t.groupby(["rule", "arm"]).size().to_dict() == {
        (r, a): (2 if a == "every" else 1) for r in rf.RULES for a in rf.ARMS}
    r = t[t["arm"] == "rule"]
    assert np.allclose(r["t"] - s[0], 100.30) and (r["run"] == "2").all()
