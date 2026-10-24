import gzip
import json
import math

import numpy as np
import pandas as pd
import pytest

import alts
import binary as bo
import forward_variants as fv
import latency as lt

P = 2_500.0
BASE, UP, DOWN = (0.49, 0.50, 40.0, 25.0), (0.59, 0.60, 40.0, 25.0), (0.29, 0.30, 40.0, 30.0)


def _ts(s):
    return int(pd.Timestamp(s, tz="UTC").timestamp())


IND = _ts("2026-10-02 00:00")
FWD = _ts(alts.FWD_FROM)


def _write(root, run, coin, starts, winners, at=100.0, down=(), book_from=0.0, seed=0):
    """A recording directory as recording.LatencyFiles writes it for `coin`: consecutive markets,
    the coin's Binance prints one a second (x.05, received at x.15, 1e-5 noise) with a +0.4% jump at
    start + `at` and, in the markets of `down`, a -0.8% drop at start + 200; the Up book (a row a
    second from `book_from`) is 0.49 / 0.50, 0.59 / 0.60 from 0.4 s after the jump and 0.29 / 0.30
    from 0.4 s after the drop."""
    d = root / str(run) / "x" / f"bundle-{coin}" / "latency"
    d.mkdir(parents=True)
    mids = [f"{coin}-updown-5m-{s}" for s in starts]
    pd.DataFrame({"market_id": mids, "start_ts": starts}).to_csv(
        d / "runtime_1000.market_registry.csv.gz", index=False, compression="gzip")
    pd.DataFrame({"market_id": mids, "winner": winners}).to_csv(
        d / "runtime_1000.market_outcomes.csv.gz", index=False, compression="gzip")
    rows = []
    for i, (mid, s) in enumerate(zip(mids, starts)):
        def state(x):
            if i in down and x >= 200.4:
                return DOWN
            return UP if x >= at + 0.4 else BASE
        for x in sorted(set(np.arange(book_from, 300.0, 1.0)) | {at + 0.4} | ({200.4} if i in down else set())):
            b, a, bs, az = state(x)
            rows.append({"market_id": mid, "source_ts": s + x, "receive_ts": s + x + 0.01, "best_bid": b,
                         "best_ask": a, "bids_json": f"[[{b:.2f}, {bs}]]", "asks_json": f"[[{a:.2f}, {az}]]"})
    pd.DataFrame(rows).sort_values("source_ts").to_csv(d / "runtime_1000.poly_probability_observations_v1.csv.gz",
                                                        index=False, compression="gzip")
    secs = np.arange(starts[0] - 1200, starts[-1] + 300)
    lp = np.cumsum(np.random.default_rng(seed).normal(0, 1e-5, len(secs)))
    for i, s in enumerate(starts):
        lp[secs >= s + at] += 0.004
        if i in down:
            lp[secs >= s + 200] -= 0.008
    lines = [json.dumps({"event": "BINANCE_WS_TRADE", "trade_ts": float(x) + 0.05, "receive_ts": float(x) + 0.15,
                         "price": float(P * math.exp(v))}) for x, v in zip(secs, lp)]
    (d / "binance_trades.jsonl.gz").write_bytes(gzip.compress(("\n".join(lines) + "\n").encode()))
    pd.DataFrame({"at_s": []}).to_csv(d / "clob_closes.csv.gz", index=False, compression="gzip")
    return mids


def _starts(t0, n):
    return [t0 + 300 * i for i in range(n)]


def test_btc_rows_are_forward_variants_rows(tmp_path):
    """BTC through alts.py gives exactly the trades forward_variants.py computes for the BTC curve
    (G first and H buying every fill), at each lag; H's weights are latency.opp_weights."""
    starts = _starts(IND, 8)
    _write(tmp_path / "rec", 1, "btc", starts, ["Up", "Down"] * 4, down={2, 5})
    t, markets, _ = alts.run(tmp_path / "rec", ("btc",), lags=(0.3, 0.4))
    cols = ["market_id", "t", "side", "price", "fee", "pnl", "size", "won"]
    for lag in (0.3, 0.4):
        ref = fv.run(tmp_path / "rec", lag)
        for rule, name in (("G", "G 首笔（检验 G）"), ("H", "H 每次都加")):
            a = alts.sel(t, "btc", rule, lag).reset_index(drop=True)
            b = ref[name].reset_index(drop=True)
            assert len(a) == len(b) > 0
            pd.testing.assert_frame_equal(a[cols], b[cols], check_dtype=False)
        h = alts.sel(t, "btc", "H", lag)
        assert np.allclose(h["w"], lt.opp_weights(ref["H 每次都加"]).to_numpy())
        c, se, _, _ = lt.clustered(ref["H 每次都加"], lt.opp_weights(ref["H 每次都加"]))
        assert alts.cell(h) == f"{c:+.1f}¢ ±{se:.1f}（{len(h)}）" == fv.curve_cell(ref["H 每次都加"],
                                                                                lt.opp_weights(ref["H 每次都加"]))
    assert len(markets) == 8 and (markets["segment"] == "ind").all()


def test_each_coin_on_its_own_feed_and_book(tmp_path):
    """ETH jumps at 150 s, BTC at 100 s, in the same recording: each coin trades its own jump on its
    own book; at 0.3 s the 0.50 ask is still there, at 0.4 s the 0.60 one; the opposite side's ask
    is 1 - the Up bid at the same moment."""
    starts = _starts(IND, 6)
    _write(tmp_path / "rec", 1, "btc", starts, ["Up"] * 6)
    _write(tmp_path / "rec", 1, "eth", starts, ["Up"] * 6, at=150.0, seed=1)
    t, markets, _ = alts.run(tmp_path / "rec", ("btc", "eth"), lags=(0.3, 0.4))
    for coin, at in (("btc", 100.05), ("eth", 150.05)):
        for rule in alts.RULES:
            for lag, px, opp in ((0.3, 0.50, 0.51), (0.4, 0.60, 0.41)):
                r = alts.sel(t, coin, rule, lag)
                assert len(r) == 6 and np.allclose(r["t"] - r["start_ts"], at) and (r["price"] == px).all()
                assert r["market_id"].str.startswith(f"{coin}-").all() and (r["side"] == "Up").all()
                o = alts.sel(t, coin, rule, lag, "opposite")
                assert len(o) == 6 and np.allclose(o["price"], opp) and (o["side"] == "Down").all()
                assert np.allclose(o["t_buy"], r["t"].to_numpy() + lag, rtol=0, atol=1e-6) and (o["won"] == 0).all()
    assert markets.groupby("coin").size().to_dict() == {"btc": 6, "eth": 6}


def test_opposite_adds_weigh_twice_and_controls_follow(tmp_path):
    """Markets 1 and 4 drop 0.8% at 200 s: H adds Down there (weight 2), G keeps its first (Up)
    trade; the controls carry the weight of the trade they mirror. Random controls: a moment in the
    market's 240..15 s window, the same for a trigger at every lag, different for G and H."""
    starts = _starts(IND, 6)
    w = ["Up", "Down", "Up", "Up", "Down", "Up"]
    mids = _write(tmp_path / "rec", 1, "eth", starts, w, down={1, 4})
    t, markets, _ = alts.run(tmp_path / "rec", ("eth",), lags=(0.3, 0.4))
    g, h = alts.sel(t, "eth", "G", 0.4), alts.sel(t, "eth", "H", 0.4)
    assert g["market_id"].tolist() == mids and len(alts.sel(t, "eth", "G", 0.3)) == 6
    assert (g["side"] == "Up").all() and (g["w"] == 1).all() and np.allclose(g["t"] - g["start_ts"], 100.05)
    down = h[h["side"] == "Down"]
    assert down["market_id"].tolist() == [mids[1], mids[4]] and (down["w"] == 2.0).all()
    assert np.allclose(down["t"] - down["start_ts"], 200.05) and np.allclose(down["price"], 0.71)
    assert (h.loc[h["side"] == "Up", "w"] == 1.0).all() and h.loc[h["side"] == "Up", "market_id"].is_unique
    pnl = (h["won"] - h["price"] - bo.taker_fee(h["price"])).to_numpy()
    assert np.allclose(h["pnl"], pnl)
    c = alts.stats(h)[0]
    assert np.isclose(c, 100 * (h["w"] * h["pnl"]).sum() / h["w"].sum())
    for arm in ("opposite", "random"):
        x = alts.sel(t, "eth", "H", 0.4, arm)
        assert len(x) == len(h) and x.merge(h[["t", "w"]], on="t", suffixes=("", "_r")).eval("w == w_r").all()
    r3, r4 = alts.sel(t, "eth", "G", 0.3, "random"), alts.sel(t, "eth", "G", 0.4, "random")
    same = r3.merge(r4, on="t", suffixes=("3", "4"))
    assert len(same) == 6 and np.allclose(same["t_buy3"] - 0.3, same["t_buy4"] - 0.4, rtol=0, atol=1e-6)
    assert (same["side3"] == same["side4"]).all()
    end = r3["start_ts"] + bo.WINDOW_S
    assert ((r3["t_buy"] - 0.3 >= end - 240) & (r3["t_buy"] - 0.3 <= end - lt.G_TAU_LO)).all()
    assert r3["t_buy"].nunique() == 6 and set(r3["side"]) <= {"Up", "Down"}
    both = r4.merge(alts.sel(t, "eth", "H", 0.4, "random"), on="t", suffixes=("_g", "_h"))
    assert len(both) == 6 and (np.abs(both["t_buy_g"] - both["t_buy_h"]) > 1e-3).all()  # the rule is in the seed
    again, _, _ = alts.run(tmp_path / "rec", ("eth",), lags=(0.3, 0.4))
    pd.testing.assert_frame_equal(t, again)


def test_money_and_frequency(tmp_path):
    """$/day at 5 shares (x the weight) and at min(20 w, displayed size); days = markets x 300 s."""
    starts = _starts(IND, 6)
    _write(tmp_path / "rec", 1, "eth", starts, ["Up", "Down", "Up", "Up", "Down", "Up"], down={1, 4})
    t, markets, _ = alts.run(tmp_path / "rec", ("eth",), lags=(0.4,))
    d = alts.days(markets, "eth", "ind")
    assert np.isclose(d, 6 * 300 / 86400)
    h = alts.sel(t, "eth", "H", 0.4)
    d5, dc = alts.money(h, d)
    assert np.isclose(d5, (5 * h["w"] * h["pnl"]).sum() / d)
    shares = np.where(h["side"] == "Up", np.minimum(20 * h["w"], 25.0), np.minimum(20 * h["w"], 40.0))
    assert np.allclose(np.minimum(20 * h["w"], h["size"]), shares)
    assert np.isclose(dc, (shares * h["pnl"]).sum() / d)
    row = alts.freq_row(h, d).split(" | ")
    assert row[0] == "8" and row[1] == "6" and row[2] == f"{6 / d:,.1f}" and row[3] == f"{8 / d:,.1f}"


def test_market_in_two_recordings_keeps_its_earliest_g_trade(tmp_path):
    """Recording 1 holds the whole market (G buys Up at 100.05); recording 2 its book only from 150 s
    (G can only buy the 200 s drop): pooled, G keeps recording 1's trade; H keeps 100.05 and one
    200.05 trade; the controls follow the kept rows."""
    s = _starts(IND, 3)
    _write(tmp_path / "rec", 1, "sol", s, ["Up"] * 3, down={1})
    _write(tmp_path / "rec", 2, "sol", s, ["Up"] * 3, down={1}, book_from=150.0)
    t, markets, _ = alts.run(tmp_path / "rec", ("sol",), lags=(0.4,))
    g = alts.sel(t, "sol", "G", 0.4)
    assert len(g) == 3 and (g["run"] == "1").all() and np.allclose(g["t"] - g["start_ts"], 100.05)
    h = alts.sel(t, "sol", "H", 0.4)
    assert len(h) == 4 and h["t"].is_unique and (h["run"] == "1").all()
    for arm in ("opposite", "random"):
        x = alts.sel(t, "sol", "G", 0.4, arm)
        assert len(x) == 3 and (x["run"] == "1").all() and np.allclose(x["t"], g["t"], rtol=0, atol=1e-6)
    assert len(markets) == 3


def test_entry_verdict_pinned_and_read_back(tmp_path, monkeypatch):
    """ETH (Up wins, bought at 0.60 at 0.4 s) enters, SOL (Down wins) does not, XRP has no recording
    yet (entry pending). With FWD_N = 4 ETH's forward verdict is pinned on its first four trades and
    read back; SOL's forward segment is only described. A later run with XRP data pins XRP's entry
    and leaves the rest."""
    monkeypatch.setattr(alts, "FWD_N", 4)
    ind, fwd = _starts(IND, 4), _starts(FWD - 300, 7)  # the first forward-recording market is before FWD_FROM
    _write(tmp_path / "rec", 1, "eth", ind, ["Up"] * 4)
    _write(tmp_path / "rec", 1, "sol", ind, ["Down"] * 4)
    _write(tmp_path / "rec", 2, "eth", fwd, ["Up", "Up", "Down", "Up", "Up", "Up", "Up"])
    _write(tmp_path / "rec", 2, "sol", fwd, ["Up"] * 7)
    out = tmp_path / "real" / "alts.md"
    alts.main([str(tmp_path / "rec"), "--out", str(out)])
    text = out.read_text(encoding="utf-8")
    v = out.with_suffix(".verdict.md").read_text(encoding="utf-8")
    lines = v.splitlines()
    assert [ln.split("：")[0] for ln in lines] == ["- 入选 eth", "- 判定 eth", "- 入选 sol"]
    assert "→ **入选**" in lines[0] and "→ **不入选**" in lines[2] and "5 笔" in lines[0]
    assert "前向段前 4 笔，4 个市场" in lines[1] and f"p < {0.025 / 4:g}" in lines[1]
    tr = pd.read_csv(out.with_suffix(".trades.csv"))
    assert len(tr) == 4 and (tr["start_ts"] >= FWD).all() and (tr["coin"] == "eth").all()
    exp = np.array([1, 0, 1, 1], float) - 0.60 - bo.taker_fee(0.60)  # forward markets fwd[1:5]
    se = math.sqrt(((exp - exp.mean()) ** 2).sum() * 4 / 3) / 4
    assert f"每份 {100 * exp.mean():+.2f}¢ ±{100 * se:.2f}" in lines[1]
    assert ("**通过**" in lines[1]) == bool(exp.mean() > 0 and 0.5 * math.erfc(exp.mean() / se / math.sqrt(2)) < 0.025 / 4
                                         and exp[:2].mean() > 0 and exp[2:].mean() > 0)
    assert "- 入选 xrp：独立段还没有 G 首笔 0.4 秒的成交，入选待定。" in text and "- 判定 sol：没入选" in text
    assert "| BTC（对照） | 独立段 | 0 |" in text
    assert "| ETH | 独立段 | 5 |" in text and "| ETH | 前向段 | 6 |" in text
    _write(tmp_path / "rec", 3, "xrp", ind, ["Up"] * 4)
    alts.main([str(tmp_path / "rec"), "--out", str(out)])
    v2 = out.with_suffix(".verdict.md").read_text(encoding="utf-8")
    assert v2.startswith(v) and v2[len(v):].startswith("- 入选 xrp：") and v2.count("\n") == 4
    text = out.read_text(encoding="utf-8")
    assert text.count("（已定，不再改）") == 2 and text.count("（已判定，不再重算）") == 1
    assert len(pd.read_csv(out.with_suffix(".trades.csv"))) == 4


def _rows(coin, seg, pnl, start=None):
    n = len(pnl)
    start = (FWD if seg == "fwd" else IND) if start is None else start
    st = start + 300 * np.arange(n)
    return pd.DataFrame({"coin": coin, "run": "9", "market_id": [f"{coin}-{s}" for s in st], "start_ts": st,
                         "segment": seg, "rule": "G", "lag": 0.4, "arm": "rule", "t": st + 100.05, "t_buy": st + 100.45,
                         "side": "Up", "won": 1.0, "price": 0.5, "fee": 0.0, "pnl": pnl, "size": 25.0, "w": 1.0})


def test_halves_must_both_be_positive(tmp_path):
    """A strongly positive forward mean with a negative first half does not pass; a coin whose
    independent mean is exactly 0 does not enter."""
    pnl = np.r_[np.full(300, -0.05), np.full(300, 0.40)] + np.tile([0.01, -0.01], 300)
    t = pd.concat([_rows("eth", "ind", np.full(10, 0.1)), _rows("eth", "fwd", pnl),
                   _rows("sol", "ind", np.zeros(10))], ignore_index=True)
    lines = alts.judge(t, tmp_path / "alts.md")
    assert "→ **入选**" in lines[0] and "**不通过**" in lines[1] and "前 300 笔 -5.00¢" in lines[1]
    assert "→ **不入选**" in lines[2] and lines[3].startswith("- 判定 sol：没入选")
    ok = pd.concat([_rows("doge", "ind", np.full(10, 0.1)), _rows("doge", "fwd", 0.05 + np.tile([0.3, -0.3], 300))])
    lines = alts.judge(ok, tmp_path / "ok.md", coins=("doge",))
    assert "**通过**" in lines[1] and "p = " in lines[1]


def test_itode_lookup():
    seen = []

    def fetch(url):
        seen.append(url)
        if "gamma" in url:
            return [] if "doge" in url else [{"conditionId": "0x" + url.split("slug=")[1][:3]}]
        return {"itode": "0xeth" not in url, "mts": 0.01, "fd": {"r": 0.07}}

    now = _ts("2026-10-04 20:36")
    rows = alts.itode(("btc", "eth", "doge"), fetch, now=now)
    start = _ts("2026-10-04 20:35")
    assert rows["btc"] == (f"btc-updown-5m-{start}", True, 0.01, 0.07, None)
    assert rows["eth"][1] is False  # the fake CLOB record of condition 0xeth has itode false
    assert rows["doge"][1] is None and "not listed" in rows["doge"][4]
    assert seen[0] == alts.GAMMA.format(slug=f"btc-updown-5m-{start}")
    assert seen[1] == alts.CLOB_MARKET.format(cid="0xbtc")


def test_report_lists_itode_and_every_table(tmp_path, monkeypatch):
    starts = _starts(IND, 4)
    _write(tmp_path / "rec", 1, "btc", starts, ["Up"] * 4)
    _write(tmp_path / "rec", 1, "eth", starts, ["Up"] * 4)
    monkeypatch.setattr(alts, "itode", lambda coins: {c: (f"{c}-updown-5m-1", c != "doge", 0.01, 0.07, None)
                                                      for c in coins})
    out = tmp_path / "real" / "alts.md"
    alts.main([str(tmp_path / "rec"), "--out", str(out), "--itode"])
    text = out.read_text(encoding="utf-8")
    for head in ("## 表 1a G 首笔", "## 表 1b H 反向 2 倍", "## 表 2 ", "## 表 3 ", "## 表 4 ", "## 表 5 ", "## 备注"):
        assert head in text
    assert "| eth | `eth-updown-5m-1` | true（有 150 ms 冻结） |" in text and "| doge | `doge-updown-5m-1` | 没有 |" in text
    assert "| 0.4 秒 | +" in text and "（4） | +" in text  # BTC and ETH cells at 0.4 s
    assert "| ETH | G 首笔 | 0.4 秒 | 4 | 4 | " in text
    assert "| SOL | 独立段 | 0 |" in text


def test_an_error_is_left_in_the_report(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("damaged recording")
    monkeypatch.setattr(alts, "run", boom)
    out = tmp_path / "real" / "alts.md"
    with pytest.raises(RuntimeError):
        alts.main([str(tmp_path / "rec"), "--out", str(out)])
    text = out.read_text(encoding="utf-8")
    assert text.startswith("# alts.py 出错") and "RuntimeError: damaged recording" in text
    assert not out.with_suffix(".verdict.md").exists()
