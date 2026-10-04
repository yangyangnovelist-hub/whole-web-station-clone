import gzip
import json

import numpy as np
import pandas as pd
import pytest

import binary as bo
import late_ms as lm
import latency as lt

S0 = 1_789_300_000 // 300 * 300
N = 30
JUMP = 100.85  # seconds into each market: the jump print
PULL = 0.18    # the pre-jump ask is pulled this long after the print


def _rows(s0, change_at, before=(0.49, 0.50, 40.0, 25.0), after=(0.79, 0.80, 40.0, 25.0), step=1.0):
    """An Up book updating every `step` s over the market, (bid, ask, bid size, ask size) `before`
    until change_at and `after` from then on."""
    out = []
    for t in sorted(set(np.arange(s0, s0 + 300, step).tolist()) | {change_at}):
        out.append((t, *(before if t < change_at else after)))
    return out


def _book(by_market):
    parts = []
    for mid, rows in by_market.items():
        df = pd.DataFrame(rows, columns=["ts", "bid", "ask", "bid_size", "ask_size"])
        df["market_id"] = mid
        parts.append(df)
    books = pd.concat(parts, ignore_index=True)
    books["recv"] = books["ts"] + 0.01
    return lt.Book(books.sort_values(["market_id", "ts"], kind="stable").reset_index(drop=True))


def _jumps(mids, side="Up", winner="Up"):
    return pd.DataFrame({"market_id": mids, "start_ts": S0, "t0": S0 + JUMP, "side": side, "winner": winner,
                         "dx": 0.004})


def test_no_lookahead():
    """Every price used is the last book row stamped at or before the moment it is used at; a row
    stamped just after the match never changes a fill."""
    rng = np.random.default_rng(7)
    ts = np.sort(S0 + 40 + rng.uniform(0, 260, 3000))
    rows = list(zip(ts, rng.uniform(0.30, 0.48, len(ts)).round(2), rng.uniform(0.52, 0.70, len(ts)).round(2),
                    rng.uniform(1, 50, len(ts)), rng.uniform(1, 50, len(ts))))
    book = _book({"m": rows})
    df = pd.DataFrame(rows, columns=["ts", "bid", "ask", "bid_size", "ask_size"])
    t = lm.entries(_jumps(["m"], "Down", "Down"), book)
    assert set(t["variant"]) == {"ms", "end", "uniform", "ms_sd"} and set(t["arm"]) == {"with", "against", "random"}

    def last(at, side):
        r = df[df["ts"] <= at].iloc[-1]
        return (r["ask"], r["ask_size"]) if side == "Up" else (1 - r["bid"], r["bid_size"])

    for r in t.itertuples():
        if r.variant in ("ms", "ms_sd"):
            assert r.ask_send == last(r.t_ref, r.side)[0]
            assert (r.ask, r.size) == last(r.t_ref + lm.M, r.side)
        else:
            assert (r.ask, r.size) == last(r.t_ref, r.side)
    # a row stamped 1 ms after the s = 0.11 match, cheap or dear, leaves that fill alone
    t_match = S0 + JUMP + 0.11 + lm.M
    got = []
    for a in (0.30, 0.90):
        base = _rows(S0, S0 + 400)  # 0.50 ask throughout
        t2 = lm.entries(_jumps(["m"]), _book({"m": base + [(t_match + 0.001, a - 0.01, a, 40.0, 25.0)]}))
        got.append(t2[(t2["variant"] == "ms") & (t2["arm"] == "with") & np.isclose(t2["x"], 0.11)])
    assert len(got[0]) == 1 and got[0]["ask"].iloc[0] == 0.50
    assert got[0][["ask", "size", "pnl"]].equals(got[1][["ask", "size", "pnl"]])


def test_stale_pre_jump_quote_is_bought_only_by_the_kacho_emulation():
    """The jump print comes 0.85 s into its second and the 0.50 ask is pulled 0.18 s later, in the
    next second: kacho's d = 1 row (stamped the jump second) shows it at its end and at any moment
    in it, a real order sent 0.11 s or later after the print is matched at 0.80."""
    book = _book({"m": _rows(S0, S0 + JUMP + PULL)})
    for seed in range(5):
        t = lm.entries(_jumps(["m"]), book, seed=seed)
        ok = t[t["ok"]]
        k1 = ok[ok["variant"].isin(["end", "uniform"]) & (ok["x"] == 1)]
        assert len(k1) == 2 and (k1["ask"] == 0.50).all() and k1["stale"].all()
        assert k1["pnl"].iloc[0] == pytest.approx(1 - 0.50 - bo.taker_fee(0.50))
        real = ok[(ok["variant"] == "ms") & (ok["arm"] == "with")]
        assert len(real) == len(lm.SENDS) and (real["ask"] == 0.80).all()
        assert (ok.loc[ok["variant"] == "ms_sd", "ask"] == 0.80).all()
        k2 = ok[(ok["variant"] == "end") & (ok["x"] == 2)]
        assert (k2["ask"] == 0.80).all() and not k2["stale"].any()
    lo = lm.limit(t, 0.05)  # sent at +0.11 s seeing 0.50: the 0.80 ask is above it; later sends see 0.80
    first = np.isclose(lo["x"], 0.11)
    assert not lo.loc[first, "fill"].any() and (lo.loc[first, "pnl"] == 0).all() and lo.loc[~first, "fill"].all()


def test_limit_fill_rule():
    """Sent at +0.11 s seeing 0.50; at the 0.485 s match the ask is 0.52 (m0), 0.56 (m1), 0.45 with
    3 shares (m2) or 0.45 with 25 (m3); m4's ask at the send is 0.99 (not sent, though a market
    order fills)."""
    ch = S0 + JUMP + 0.3
    book = _book({"m0": _rows(S0, ch, after=(0.49, 0.52, 40.0, 25.0)),
                  "m1": _rows(S0, ch, after=(0.49, 0.56, 40.0, 25.0)),
                  "m2": _rows(S0, ch, after=(0.44, 0.45, 40.0, 3.0)),
                  "m3": _rows(S0, ch, after=(0.44, 0.45, 40.0, 25.0)),
                  "m4": _rows(S0, ch, before=(0.98, 0.99, 40.0, 25.0), after=(0.49, 0.50, 40.0, 25.0))})
    t = lm.entries(_jumps(["m0", "m1", "m2", "m3", "m4"]), book)
    expect = {0.0: {"m3"}, 0.02: {"m0", "m3"}, 0.05: {"m0", "m3"}}
    for k, filled in expect.items():
        lo = lm.limit(t, k)
        lo = lo[np.isclose(lo["x"], 0.11)]
        assert set(lo["market_id"]) == {"m0", "m1", "m2", "m3"}  # m4 is not sent
        assert set(lo.loc[lo["fill"], "market_id"]) == filled
        for r in lo.itertuples():
            assert r.pnl == pytest.approx(1 - r.ask - bo.taker_fee(r.ask) if r.fill else 0.0)
        m, _, n = lm.cl(lo)
        assert n == 4 and m == pytest.approx(lo["pnl"].sum() / 4)  # unfilled orders count as 0
    mk = t[t["ok"] & (t["variant"] == "ms") & (t["arm"] == "with") & np.isclose(t["x"], 0.11)]
    assert set(mk["market_id"]) == {"m0", "m1", "m3", "m4"}  # the market order needs 5 shares, not a limit


def test_jumps_first_of_each_ten_seconds():
    """Spot prints every second at .85; 10-sigma moves at 55 (241 s left: outside), 100, 104 (4 s
    after a kept jump: dropped), 111 (down), 115 (dropped), 122 and 290 (10 s left: outside)."""
    secs = np.arange(S0 - 20, S0 + 320)
    r = 1e-5 * (-1.0) ** np.arange(len(secs))
    for k, sgn in ((55, 1), (100, 1), (104, 1), (111, -1), (115, 1), (122, 1), (290, 1)):
        r[secs == S0 + k] += sgn * 1e-3
    spot = pd.DataFrame({"trade_ts": secs + 0.85, "receive_ts": secs + 0.9, "price": 80_000 * np.exp(np.cumsum(r))})
    sigma = pd.Series(1e-4, index=range(S0 - 100, S0 + 400))
    markets = pd.DataFrame({"market_id": ["m", "e"], "start_ts": [S0, S0 + 600], "winner": ["Up", "Down"]})
    j = lm.jumps(markets, spot, sigma)
    assert np.allclose(j["t0"] - S0, [100.85, 111.85, 122.85])
    assert j["side"].tolist() == ["Up", "Down", "Up"] and (j["market_id"] == "m").all()
    assert lm.jumps(markets, spot, sigma, spacing=0.0)["t0"].size == 5


def _recording(root, n=N):
    """A recording directory like recording.LatencyFiles writes: n markets, Binance trades every
    second at .85 with alternating 1e-5 moves and a +0.4% jump at start + 100 (print at + 100.85);
    the Up ask is 0.50 until 0.18 s after that print, then 0.80. Up wins 80%."""
    d = root / "1" / "x" / "bundle-btc" / "latency"
    d.mkdir(parents=True)
    starts = [S0 + 300 * i for i in range(n)]
    pd.DataFrame({"market_id": [f"m{i}" for i in range(n)], "start_ts": starts}).to_csv(
        d / "runtime_1000.market_registry.csv.gz", index=False, compression="gzip")
    pd.DataFrame({"market_id": [f"m{i}" for i in range(n)], "winner": ["Up" if i % 5 else "Down" for i in range(n)]}
                 ).to_csv(d / "runtime_1000.market_outcomes.csv.gz", index=False, compression="gzip")
    rows = []
    for i, s0 in enumerate(starts):
        for t, bid, ask, bs, az in _rows(s0, s0 + JUMP + PULL):
            rows.append({"market_id": f"m{i}", "source_ts": t, "receive_ts": t + 0.01, "best_bid": bid,
                         "best_ask": ask, "bids_json": f"[[{bid:.2f}, {bs}]]", "asks_json": f"[[{ask:.2f}, {az}]]"})
    pd.DataFrame(rows).sort_values("source_ts").to_csv(d / "runtime_1000.poly_probability_observations_v1.csv.gz",
                                                        index=False, compression="gzip")
    secs = np.arange(S0 - 1200, S0 + 300 * n + 300)
    lp = np.cumsum(1e-5 * (-1.0) ** np.arange(len(secs)))
    for s0 in starts:
        lp[secs >= s0 + 100] += np.log(1.004)
    lines = [json.dumps({"event": "BINANCE_WS_TRADE", "trade_ts": float(s) + 0.85, "receive_ts": float(s) + 0.9,
                         "price": float(80_000 * np.exp(x))}) for s, x in zip(secs, lp)]
    (d / "binance_trades.jsonl.gz").write_bytes(gzip.compress(("\n".join(lines) + "\n").encode()))
    return starts


def test_cli_end_to_end(tmp_path, monkeypatch):
    _recording(tmp_path / "rec")
    monkeypatch.setattr(lt, "G_SINCE", "2026-09-01")
    out = tmp_path / "real" / "late-ms.md"
    lm.main([str(tmp_path / "rec"), "--out", str(out)])
    text = out.read_text(encoding="utf-8")
    assert f"{N:,} 个有盘口的 BTC 5m 市场" in text and f"急动 {N:,} 次" in text
    # real orders are matched at the 0.80 ask (Up wins 80%: -1.12c); kacho's d = 1 row shows 0.50 (+28.25c)
    assert f"| 0.11 | -1.12¢ ±" in text and f"（{N}） |" in text
    assert "| 1 | +5.57¢ ±0.11 | +28.25¢ ±" in text
    d1 = next(ln for ln in text.splitlines() if ln.startswith("| 1 | +5.57"))
    assert d1.count("+28.25¢") == 2 and d1.rstrip(" |").split(" | ")[-1].startswith("-1.12¢")
    assert f"| 行末状态 | {N} | 100% |" in text and f"| 行内随机时刻 | {N} | 100% |" in text
    assert "## 表 2" in text and "## 表 4" in text and "| 真实市价单 s = 0.11 |" in text
    assert any("no CLOB disconnect log" in ln for ln in text.splitlines())
    t, markets, n_jumps, _ = lm.run(tmp_path / "rec")
    assert n_jumps == N and len(markets) == N and t["market_id"].nunique() == N
