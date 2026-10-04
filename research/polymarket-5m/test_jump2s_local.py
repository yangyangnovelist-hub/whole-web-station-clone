import gzip
import io
import zipfile

import numpy as np
import pandas as pd
import pytest

import jump2s as j2
import jump2s_local as jl

DAY = "2026-04-01"
D0 = int(pd.Timestamp(DAY, tz="UTC").timestamp())
S0 = D0 + 3600          # a 5m window open
X = np.log(70000.0)


def kline_zip(path, t_open, close, unit="ms", header=False):
    t = np.asarray(t_open, np.int64) * (1000 if unit == "ms" else 1_000_000)
    c = np.asarray(close, float)
    lines = ["open_time,open,high,low,close,volume,close_time,qv,n,tb,tq,ig"] if header else []
    lines += [f"{a},{b},{b},{b},{b},1.0,{a + 999},1,1,0.5,1,0" for a, b in zip(t, c)]
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(path.stem + ".csv", "\n".join(lines) + "\n")


def walk(n, seed=0, sd=0.3e-4):
    rng = np.random.default_rng(seed)
    return X + np.cumsum(rng.normal(0, sd, n))


# ------------------------------------------------------------------ loading and jump timing
def test_load_seconds_units_header_duplicates_and_no_fill(tmp_path):
    kline_zip(tmp_path / "BTCUSDT-1s-2026-04-01.zip", [D0 + 2, D0, D0 + 1, D0 + 1], [3.0, 1.0, 2.0, 2.5],
              unit="ms", header=True)
    kline_zip(tmp_path / "BTCUSDT-1s-2026-04-02.zip", [D0 + 86400, D0 + 86403], [4.0, 5.0], unit="us")
    kline_zip(tmp_path / "BTCUSDT-1s-2026-05-01.zip", [D0 + 30 * 86400], [6.0])
    ts, c = jl.load_seconds(tmp_path, "2026-04-01", "2026-04-03")
    assert list(ts - D0) == [0, 1, 2, 86400, 86403]          # the gap 86401..86402 stays a gap
    assert list(c) == [1.0, 2.5, 3.0, 4.0, 5.0]               # duplicate second: the last row kept
    assert len(jl.load_seconds(tmp_path)[0]) == 6


def test_kline_jump_time_is_the_close_of_the_jump_second():
    starts = [S0 + 300 * k for k in range(4)]
    w = pd.DataFrame({"market": starts, "start": starts, "end": [s + 300 for s in starts]})
    sec = np.arange(S0 - 10, S0 + 1210)
    inc = {starts[0] + 13: 2.0,     # known at +14: out
           starts[1] + 14: 2.0,     # known at +15: in
           starts[2] + 287: -2.0,   # known at +288 (12 s before the end): in
           starts[3] + 288: 2.0}    # known at +289: out
    lp = X + 1e-4 * np.cumsum([inc.get(int(s), 0.0) for s in sec])
    j = jl.kline_jumps(sec, lp, w)
    assert list(j["market"]) == [starts[1], starts[2]]
    assert list(j["t_ex"]) == [starts[1] + 14, starts[2] + 287] and list(j["t0"] - j["t_ex"]) == [1.0, 1.0]
    assert list(j["sign"]) == [1, -1] and set(j["bucket"]) == {"big"}


def test_window_grid():
    w = jl.window_grid("2026-04-01", "2026-04-03")
    assert len(w) == 576 and w["start"].iloc[0] == D0 and (w["start"] % 300 == 0).all()
    assert (w["end"] - w["start"] == 300).all() and (w["market"] == w["start"]).all()


# ------------------------------------------------------------------ kacho books
def ticks_for(starts, up_won, gaps=()):
    """Two-sided books for each market; au at stamp s is 0.30 + 0.001 * (s - start) (so the row used
    can be read off the price), ad = 1.01 - au, bids 0.01 below."""
    rows = []
    for st, uw in zip(starts, up_won):
        for off in range(300):
            if (st, off) in gaps:
                continue
            au = 0.30 + 0.001 * off
            rows.append(dict(condition_id=f"c{st}", t=st + off, au=au, ad=round(1.01 - au, 6), bu=au - 0.01,
                             bd=round(1.0 - au, 6), sau=100.0, sad=100.0, su=50.0, sd=50.0, start=st, up_won=uw))
    return pd.DataFrame(rows)


def test_book_row_is_exact_and_market_local():
    t = ticks_for([S0, S0 + 300], [True, False], gaps={(S0, 10)})
    b = jl.Book(t)
    i = b.row([S0, S0, S0, S0 + 300, S0], [S0 + 9, S0 + 10, S0 + 299, S0 + 300, S0 + 300])
    assert i[0] >= 0 and i[1] == -1 and i[2] >= 0 and i[3] >= 0 and i[4] == -1
    assert b.cols["au"][i[3]] == pytest.approx(0.30) and b.cols["au"][i[2]] == pytest.approx(0.30 + 0.299)
    ask, size = b.ask(np.array([i[0], -1]), np.array([-1, 1]))
    assert ask[0] == pytest.approx(1.01 - 0.309) and np.isnan(ask[1]) and np.isnan(size[1])
    assert b.mid(np.array([i[0]]), np.array([1]))[0] == pytest.approx(0.309 - 0.005)
    assert b.up_won.to_dict() == {S0: 1.0, S0 + 300: 0.0}


def _sp_and_jump(S, d_bp=2.0, n_before=1500, seed=0):
    sec = np.arange(S0 - n_before, S0 + 900)
    lp = walk(len(sec), seed)
    k = int(np.flatnonzero(sec == S)[0])
    lp[k:] += d_bp * 1e-4 - (lp[k] - lp[k - 1])     # exactly d_bp at second S
    return sec, lp


def test_kacho_rows_use_the_rows_stamped_s_plus_2_and_s_plus_1():
    S = S0 + 100
    sec, lp = _sp_and_jump(S)
    sp = j2.Seconds(sec, np.exp(lp))
    w = pd.DataFrame({"market": [S0], "start": [S0], "end": [S0 + 300]})
    sel = jl.kline_jumps(sec, lp, w)
    sel = sel[(sel["t_ex"] == S) & (sel["bucket"] == "big")]
    p = j2.plan(sel, w, seed=1)
    t = ticks_for([S0], [False])
    rows, counts = jl.kacho_rows(p, jl.Book(t), sp)
    big = rows[(rows.kind == "big") & (rows.price_type == "book")].iloc[0]
    s1 = rows[(rows.kind == "big") & (rows.price_type == "book_s1")].iloc[0]
    opp = rows[(rows.kind == "opposite") & (rows.price_type == "book")].iloc[0]
    assert big.sign == 1 and big.price == pytest.approx(0.30 + 0.001 * 102)    # row S + 2
    assert s1.price == pytest.approx(0.30 + 0.001 * 101)                       # row S + 1
    assert opp.sign == -1 and opp.price == pytest.approx(1.01 - (0.30 + 0.001 * 102))
    assert big.won == 0.0 and opp.won == 1.0
    assert big.pnl == pytest.approx(-big.price - 0.07 * big.price * (1 - big.price))
    # decomposition: mids of rows S - 1 and S + 2, model fair at S and S + 3, continuation from close(S + 2)
    assert big.mid0 == pytest.approx(0.30 + 0.001 * 99 - 0.005) and big.mid2 == pytest.approx(0.30 + 0.001 * 102 - 0.005)
    assert big.fair0 == pytest.approx(float(j2.model_fair(sp, 1, S0, S)[0]))
    assert big.fair2 == pytest.approx(float(j2.model_fair(sp, 1, S0, S + 3)[0]))
    k = {int(s): v for s, v in zip(sec, lp)}
    assert big.cont_bp == pytest.approx((k[S0 + 299] - k[S + 2]) * 1e4)
    assert np.isnan(s1.mid0) and np.isnan(s1.cont_bp)
    assert counts[("book", "big")] == (1, 0, 0)


def test_kacho_rows_drop_missing_rows_thin_asks_and_band():
    S = S0 + 100
    sec, lp = _sp_and_jump(S)
    sp = j2.Seconds(sec, np.exp(lp))
    w = pd.DataFrame({"market": [S0], "start": [S0], "end": [S0 + 300]})
    sel = jl.kline_jumps(sec, lp, w)
    p = j2.plan(sel[(sel["t_ex"] == S) & (sel["bucket"] == "big")], w, seed=1)
    p = p[p.kind == "big"]
    t = ticks_for([S0], [True], gaps={(S0, 102)})                  # row S + 2 missing: no fallback
    rows, counts = jl.kacho_rows(p, jl.Book(t), sp)
    assert list(rows["price_type"]) == ["book_s1"] and counts[("book", "big")] == (1, 1, 0)
    t = ticks_for([S0], [True])
    t.loc[t.t == S + 1, "sau"] = 4.99                               # fewer than 5 shares
    t.loc[t.t == S + 2, "au"] = 0.985                               # outside the band
    rows, counts = jl.kacho_rows(p, jl.Book(t), sp)
    assert rows.empty and counts[("book", "big")] == (1, 0, 1) and counts[("book_s1", "big")] == (1, 0, 1)


# ------------------------------------------------------------------ Binance only
def test_binance_rows_edge_and_continuation_on_point_proxy():
    S = S0 + 100
    sec, lp = _sp_and_jump(S, d_bp=-2.5, seed=3)
    sp = j2.Seconds(sec, np.exp(lp))
    w = jl.window_grid(DAY, "2026-04-02")
    w = w[w.start == S0]
    sel = jl.kline_jumps(sec, lp, w)
    p = j2.plan(sel, w, seed=2)
    r = jl.binance_rows(p, sp)
    assert set(r["kind"]) <= set(jl.BIN_KINDS) and "opposite" not in set(r["kind"])
    assert set(r["price_type"]) == {"model"}
    k = {int(s): v for s, v in zip(sec, lp)}
    up = float(k[S0 + 299] >= k[S0 - 1])
    b = r[(r.kind == "big") & (r.t_ex == S)].iloc[0]
    assert b.sign == -1 and b.won == 1.0 - up
    assert b.price == pytest.approx(float(j2.model_fair(sp, -1, S0, S + 3, twap=0)[0]))
    assert b.pnl == pytest.approx(b.won - b.price)
    assert b.cont_bp == pytest.approx(-(k[S0 + 299] - k[S + 2]) * 1e4)
    # the proxy uses the point price even after 08-14 (real markets: 60 s TWAP)
    late = int(pd.Timestamp("2026-08-20", tz="UTC").timestamp())
    assert j2.proxy_up(sp, [late], twap=0).shape == (1,)


# ------------------------------------------------------------------ end to end
def _fixture(tmp_path):
    kd, kl = tmp_path / "kacho", tmp_path / "klines"
    kd.mkdir()
    kl.mkdir()
    sec = np.arange(D0 - 86400, D0 + 86400)
    lp = walk(len(sec), seed=5, sd=0.9e-4)
    for day, m in ((pd.Timestamp(D0 - 86400, unit="s").strftime("%Y-%m-%d"), sec < D0),
                   (DAY, sec >= D0)):
        kline_zip(kl / f"BTCUSDT-1s-{day}.zip", sec[m], np.exp(lp[m]).round(2))
    starts = [S0 + 300 * k for k in range(12)]
    lpd = dict(zip(sec.tolist(), lp))
    won = [lpd[s + 299] >= lpd[s - 1] for s in starts]
    t = ticks_for(starts, won)
    m = pd.DataFrame({"condition_id": [f"c{s}" for s in starts], "slug": [f"btc-updown-5m-{s}" for s in starts],
                      "market_start": pd.to_datetime(starts, unit="s", utc=True).astype("datetime64[ns, UTC]")})
    # (kacho's parquet stores ns; kacho_late.load_books divides by 1e9)
    m.to_parquet(kd / "btc_markets.parquet")
    pd.DataFrame({"slug": m["slug"], "up_won": won}).to_csv(kd / "btc_outcomes.csv", index=False)
    t.drop(columns=["start", "up_won"]).to_parquet(kd / "btc_ticks.parquet")
    return kd, kl


def test_main_end_to_end(tmp_path):
    kd, kl = _fixture(tmp_path)
    out, csv = tmp_path / "r.md", tmp_path / "r.csv.gz"
    jl.main(["--kacho", str(kd), "--klines", str(kl), "--out", str(out), "--csv", str(csv)])
    txt = out.read_text(encoding="utf-8")
    for h in ("## 数据", "## 一、kacho.io", "### 分解", "### H1 顺势", "## 二、币安 1 秒 K 线", "### 逐月",
              "按判定门槛算出的数"):
        assert h in txt
    assert "**成立**" not in txt and "**不成立**" not in txt       # side evidence: no verdict
    assert "| 4月 |" in txt and "| 9月 |" in txt
    d = pd.read_csv(io.BytesIO(gzip.decompress(csv.read_bytes())))
    assert set(d["lane"]) == {"kacho", "binance_daily"}
    k = d[d.lane == "kacho"]
    assert set(k["price_type"]) == {"book", "book_s1"} and k["market"].isin([S0 + 300 * i for i in range(12)]).all()
    g = d[d.lane == "binance_daily"]
    assert g["n"].sum() > 0 and set(g["kind"]) <= set(jl.BIN_KINDS)


def test_compact_csv_respects_the_size_limit(tmp_path):
    kr = pd.DataFrame({"market": [S0], "t0": [S0 + 20.0], "kind": ["big"], "price_type": ["book"], "pnl": [0.1]})
    br = pd.DataFrame({"t0": [S0 + 20.0], "kind": ["big"], "h1": [1.0], "h2": [np.nan], "pnl": [0.1],
                       "cont_bp": [1.0], "won": [1.0]})
    assert jl.compact_csv(kr, br, tmp_path / "a.csv.gz", limit=10) is None and not (tmp_path / "a.csv.gz").exists()
    assert jl.compact_csv(kr, br, tmp_path / "b.csv.gz") > 0
