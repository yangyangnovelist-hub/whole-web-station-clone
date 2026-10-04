"""jump2s_hf.py (the Hugging Face lane of JUMP2S.md) on synthetic archives; no network."""
import gzip
import io
import tarfile
import zipfile

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import cross
import jump2s as j2
import jump2s_hf as hf

S = 1_786_000_200       # a 5m window open (s), 2026-08-05: point-price settlement
MS = S * 1000
JUMP = 100.0            # the +2 bp Binance print, exchange time, s after the open
LAG_MS = 100            # recorder receipt lag of every Binance print
T0 = S + JUMP + LAG_MS / 1000  # = S + 100.1 (receipt): the trigger
UP, DN = "tok-up", "tok-dn"


def binance(steps=((JUMP, 2.0), (250.0, 1.1)), noise=5e-7, seed=0, t_from=-900.0, t_to=330.0):
    """Prints every 250 ms (exchange time), received LAG_MS later; log steps of `bp` at `at` s."""
    t = np.arange(t_from, t_to, 0.25)
    lp = np.log(60000.0) + np.cumsum(np.random.default_rng(seed).normal(0, noise, len(t)))
    for at, bp in steps:
        lp[t >= at] += bp * 1e-4
    ms = MS + np.round(t * 1000).astype(np.int64)
    return pd.DataFrame({"trade_ts_ms": ms, "recv_ts_ms": ms + LAG_MS, "price": np.exp(lp)})


def book(market="m1", start=S, up_ask=None, size=20.0, drop=None, state="active"):
    """100 ms snapshots from 60 s before the open to the end (recorder clock). Up ask 0.50, 0.55 from
    +101 s up to and including the entry snapshot +102.1 s (= T0 + 2), 0.90 after it; bids 2c
    below, the Down book the mirror image."""
    k = np.arange(-600, 3001)
    ts = start * 1000 + 100 * k
    rel = k / 10
    ua = np.where(rel < 101, 0.50, np.where(rel <= 102.1 + 1e-9, 0.55, 0.90)) if up_ask is None else up_ask(rel)
    ub = ua - 0.02
    f = pd.DataFrame({"timestamp_ms": ts, "market_id": market, "lifecycle_state": state, "observed_halt_flag": False,
                      "up_best_bid": ub, "up_best_ask": ua, "down_best_bid": 1 - ua, "down_best_ask": 1 - ub,
                      "up_ask_size": size, "down_ask_size": 20.0})
    if drop is not None:
        f = f[~drop(rel)]
    return f.reset_index(drop=True)


def markets(*specs):
    """(market, start, up_won) -> a market_table-like frame."""
    return pd.DataFrame([{"market_id": m, "start": s * 1000, "end": (s + 300) * 1000, "k": 60000.0, "up_won": w,
                          "horizon": 5, "up_token": f"{UP}-{m}", "down_token": f"{DN}-{m}"} for m, s, w in specs])


def prints(rows, market="m1"):
    """[(s after the open, 'up'|'dn', price, taker side)] -> Polymarket prints (receipt time)."""
    return pd.DataFrame({"recv_ts_ms": [MS + round(1000 * t) for t, *_ in rows],
                         "instrument": [f"{UP if tk == 'up' else DN}-{market}" for _, tk, *_ in rows],
                         "price": [p for *_, p, _ in rows], "size": 10.0,
                         "taker_side": [s for *_, s in rows]})


def one(rows, kind, pt):
    r = rows[(rows["kind"] == kind) & (rows["price_type"] == pt)]
    assert len(r) == 1, (kind, pt, len(r))
    return r.iloc[0]


# ------------------------------------------------------------------ entries
def test_book_entry_is_the_last_snapshot_at_or_before_t0_plus_2():
    rows, info = hf.day_rows(book(), markets(("m1", S, 1.0)), binance(), prints([]))
    assert info["big"] == 1 and info["small"] == 1 and info["markets"] == 1
    b = one(rows, "big", "book")
    assert b["t0"] == pytest.approx(T0) and b["t_ex"] == pytest.approx(S + JUMP) and b["sign"] == 1
    assert b["price"] == pytest.approx(0.55)  # the 0.90 snapshot at +102.2 s comes after the entry
    assert b["pnl"] == pytest.approx(1 - 0.55 - 0.07 * 0.55 * 0.45) and b["won"] == 1
    o = one(rows, "opposite", "book")
    assert o["sign"] == -1 and o["price"] == pytest.approx(1 - 0.53) and o["won"] == 0
    assert o["pnl"] == pytest.approx(-0.47 - 0.07 * 0.47 * 0.53)
    # decomposition: the bought side's mids at t0 - 0.5 s and t0 + 2 s
    assert b["mid0"] == pytest.approx(0.49) and b["mid2"] == pytest.approx(0.54)
    assert 0 < b["fair0"] < b["fair2"] and b["fair2"] > 0.9  # sigma is tiny: +2 bp is decisive
    assert np.isnan(o["fair0"]) and np.isnan(o["cont_bp"])  # only jump rows carry them


def test_snapshot_one_tick_after_the_entry_is_never_used():
    # the jump is received 50 ms later, so the entry (+102.15 s) is between the +102.1 and +102.2 snapshots
    b = binance()
    b["recv_ts_ms"] += 50
    rows, _ = hf.day_rows(book(), markets(("m1", S, 1.0)), b, prints([]))
    assert one(rows, "big", "book")["price"] == pytest.approx(0.55)
    # moving the 0.90 quote to the +102.1 s snapshot (at the entry) is seen
    f = book(up_ask=lambda r: np.where(r < 102.1 - 1e-9, 0.55, 0.90))
    rows, _ = hf.day_rows(f, markets(("m1", S, 1.0)), binance(), prints([]))
    assert one(rows, "big", "book")["price"] == pytest.approx(0.90)


def test_book_entry_needs_a_fresh_live_snapshot_and_five_shares():
    m = markets(("m1", S, 1.0))
    stale = book(drop=lambda r: (r > 101.0 + 1e-9) & (r < 103))  # last snapshot 1.1 s before the entry
    rows, _ = hf.day_rows(stale, m, binance(), prints([]))
    assert not ((rows["kind"].isin(["big", "opposite"])) & (rows["price_type"] == "book")).any()
    thin = book(size=4.9)  # the Up ask shows 4.9 shares: no Up buy; the Down ask has 20
    rows, _ = hf.day_rows(thin, m, binance(), prints([]))
    assert not ((rows["kind"] == "big") & (rows["price_type"] == "book")).any()
    assert one(rows, "opposite", "book")["price"] == pytest.approx(0.47)
    halted = book(state="pre_open")
    rows, _ = hf.day_rows(halted, m, binance(), prints([]))
    assert not (rows["price_type"] == "book").any()
    band = book(up_ask=lambda r: np.full(len(r), 0.99))  # ask outside 0.02..0.98
    rows, _ = hf.day_rows(band, m, binance(), prints([]))
    assert not ((rows["kind"] == "big") & (rows["price_type"] == "book")).any()


def test_trade_entry_window_down_print_as_one_minus_p_and_taker_flag():
    pr = prints([(JUMP + 0.1 + 1.9, "up", 0.40, "buy"),     # before t0 + 2: ignored
                 (JUMP + 0.1 + 2.5, "dn", 0.30, "buy"),     # first in [t0 + 2, t0 + 4]
                 (JUMP + 0.1 + 3.0, "up", 0.75, "buy")])
    rows, _ = hf.day_rows(book(), markets(("m1", S, 1.0)), binance(), pr)
    b, o = one(rows, "big", "trade"), one(rows, "opposite", "trade")
    assert b["price"] == pytest.approx(0.70) and b["taker_buy"] == 0  # a Down print, not a buy of Up
    assert o["price"] == pytest.approx(0.30) and o["taker_buy"] == 1
    assert b["pnl"] == pytest.approx(1 - 0.70 - 0.07 * 0.70 * 0.30)
    assert np.isnan(one(rows, "big", "book")["taker_buy"])
    # the window is inclusive at t0 + 4.0 and closed after it
    rows, _ = hf.day_rows(book(), markets(("m1", S, 1.0)), binance(), prints([(JUMP + 0.1 + 4.0, "up", 0.6, "sell")]))
    b = one(rows, "big", "trade")
    assert b["price"] == pytest.approx(0.60) and b["taker_buy"] == 0
    rows, _ = hf.day_rows(book(), markets(("m1", S, 1.0)), binance(), prints([(JUMP + 0.1 + 4.01, "up", 0.6, "buy")]))
    assert not ((rows["kind"] == "big") & (rows["price_type"] == "trade")).any()


def test_controls_random_rows_are_seeded_per_market_and_small_jumps_are_separate():
    m = markets(("m1", S, 1.0), ("m2", S + 300, 0.0))
    b = binance(steps=((JUMP, 2.0), (250.0, 1.1), (300 + 50.0, -1.5)), t_to=630.0)
    f = pd.concat([book(), book("m2", S + 300)], ignore_index=True)
    rows, info = hf.day_rows(f, m, b, prints([]))
    assert info["plan"] == {"big": 2, "opposite": 2, "random": 2, "small": 1}
    sm = one(rows, "small", "book")
    assert sm["size_bp"] == pytest.approx(1.1, abs=0.05) and sm["t0"] == pytest.approx(S + 250.1)
    rnd = rows[(rows["kind"] == "random") & (rows["price_type"] == "book")].set_index("market")
    for mk, st in (("m1", S), ("m2", S + 300)):
        assert st + 15 <= rnd.loc[mk, "t0"] <= st + 288 and rnd.loc[mk, "sign"] in (-1, 1)
        assert np.isnan(rnd.loc[mk, "jump_sign"]) and np.isnan(rnd.loc[mk, "size_bp"])
    # m1's random row does not change when m2 is left out
    alone, _ = hf.day_rows(book(), markets(("m1", S, 1.0)), b, prints([]))
    r1 = one(alone, "random", "book")
    assert r1["t0"] == rnd.loc["m1", "t0"] and r1["sign"] == rnd.loc["m1", "sign"]
    big2 = rows[(rows["market"] == "m2") & (rows["kind"] == "big")].iloc[0]
    assert big2["sign"] == -1 and big2["won"] == 1  # m2: Down won


def test_continuation_is_the_binance_move_after_the_entry_to_the_end():
    b = binance(steps=((JUMP, 2.0), (200.0, 0.5), (260.0, 0.5)), noise=1e-7)
    rows, _ = hf.day_rows(book(), markets(("m1", S, 1.0)), b, prints([]))
    r = one(rows, "big", "book")
    lp = np.log(b["price"].to_numpy())
    x2 = lp[b["recv_ts_ms"].to_numpy() <= MS + round(1000 * (JUMP + 0.1 + 2.0))][-1]
    x_end = lp[b["trade_ts_ms"].to_numpy() <= MS + 300_000][-1]
    assert r["cont_bp"] == pytest.approx((x_end - x2) * 1e4) and r["cont_bp"] == pytest.approx(1.0, abs=0.1)
    # without a print in the last GAP_FILL_S seconds the outcome side is unknown
    gap = b[(b["trade_ts_ms"] < MS + (300 - hf.GAP_FILL_S - 1) * 1000) | (b["trade_ts_ms"] > MS + 300_000)]
    rows, _ = hf.day_rows(book(), markets(("m1", S, 1.0)), gap, prints([]))
    assert np.isnan(one(rows, "big", "book")["cont_bp"])


def test_carry_gives_the_model_its_lookback_but_no_jumps():
    b = binance()
    day = b[b["trade_ts_ms"] >= MS - 30_000]  # the archive alone: 30 s before the open
    carry = b[b["trade_ts_ms"] < MS - 30_000]
    alone, _ = hf.day_rows(book(), markets(("m1", S, 1.0)), day, prints([]))
    assert np.isnan(one(alone, "big", "book")["fair2"])  # no 300 s of returns for sigma
    both, info = hf.day_rows(book(), markets(("m1", S, 1.0)), day, prints([]), carry=carry)
    full, _ = hf.day_rows(book(), markets(("m1", S, 1.0)), b, prints([]))
    assert one(both, "big", "book")["fair2"] == pytest.approx(one(full, "big", "book")["fair2"])
    # a jump inside the carried prints is no candidate
    jumpy = binance(steps=((-100.0, 3.0), (JUMP, 2.0)))
    r, info = hf.day_rows(book(), markets(("m1", S - 300, 1.0)), jumpy[jumpy["trade_ts_ms"] >= MS - 30_000],
                          prints([]), carry=jumpy[jumpy["trade_ts_ms"] < MS - 30_000])
    assert info["big"] == 0


# ------------------------------------------------------------------ the subcommand, end to end
def parquet_bytes(df):
    buf = io.BytesIO()
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), buf)
    return buf.getvalue()


def archive(path, day):
    """One 5m market (Up won) in the archive layout cross.read_day / read_binance / read_poly_trades read."""
    f = book().drop(columns=["up_ask_size", "down_ask_size"])
    f["up_ask_sizes"] = [[20.0, 7.0]] * len(f)  # read_day keeps the size at the best (first) ask
    f["down_ask_sizes"] = [[20.0]] * len(f)
    mk = pd.DataFrame({"timestamp_ms": [MS + 300_000], "market_id": ["m1"], "slug": [f"btc-updown-5m-{S}"],
                       "session_start_ts": [MS], "session_end_ts": [MS + 300_000], "chainlink_open_price": [60000.0],
                       "up_won": [1.0], "lifecycle_state": ["resolved"], "up_token_id": [f"{UP}-m1"],
                       "down_token_id": [f"{DN}-m1"]})
    b = binance().assign(exchange="binance", instrument="BTCUSDT", size=0.01, taker_side="buy")
    p = prints([(JUMP + 0.1 + 2.5, "dn", 0.30, "buy")]).assign(exchange="polymarket", trade_ts_ms=lambda d: d["recv_ts_ms"])
    tr = pd.concat([b, p], ignore_index=True)[["trade_ts_ms", "recv_ts_ms", "exchange", "instrument", "price", "size",
                                               "taker_side"]]
    with tarfile.open(path, "w:gz") as tar:
        for name, df in ((f"dataset=polymarket_features_100ms/date={day}/part-1.parquet", f),
                         (f"dataset=polymarket_market_100ms/date={day}/part-2.parquet", mk),
                         (f"dataset=trades/date={day}/part-3.parquet", tr)):
            raw = parquet_bytes(df)
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))
    return path


def kline_zip(path, start, n):
    """A data.binance.vision-style 1m kline zip (open time in microseconds, no header)."""
    rng = np.random.default_rng(5)
    m = (start + 60 * np.arange(n)) * 1_000_000
    c = 60000 * np.exp(np.cumsum(rng.normal(0, 3e-4, n)))
    d = pd.DataFrame({0: m, 1: c, 2: c, 3: c, 4: c, 5: 1.0, 6: m + 59_999_999, 7: 1.0, 8: 10, 9: 0.5, 10: 0.5, 11: 0})
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(path.name.replace(".zip", ".csv"), d.to_csv(header=False, index=False))
    return path


def test_subcommand_end_to_end(tmp_path, monkeypatch):
    day = "2026-08-05"
    name = f"market_parquet_{day}.tar.gz"
    src = archive(tmp_path / "src.tar.gz", day)
    wd = tmp_path / "wd"
    wd.mkdir()
    kline_zip(wd / "BTCUSDT-1m-2026-08.zip", S - 6 * 3600, 8 * 60)  # cached, so not downloaded
    fetched, downloads = [], []

    def fetch(n, dest=None):
        fetched.append(n)
        if n == "MANIFEST.txt":
            return f"# file  sha256  bytes\n{name}  abc  {src.stat().st_size}\n".encode()
        dest.write_bytes(src.read_bytes())
        return dest

    def download(url, dest):
        downloads.append(url)
        raise OSError("offline")

    monkeypatch.setattr(cross, "fetch", fetch)
    monkeypatch.setattr(hf, "download", download)
    out = tmp_path / "real" / "cross-jump2s.md"
    cross.main(["jump2s", "--workdir", str(wd), "--out", str(out)])
    assert fetched == ["MANIFEST.txt", name] and not (wd / name).exists()
    assert len(downloads) == 4 and not any("2026-08" in u for u in downloads)  # 08 came from the cache
    rows = pd.read_csv(out.with_suffix(".csv.gz"))
    assert set(hf.OUT_COLS + ["ret4h", "vr60", "h1", "h2"]) <= set(rows.columns)
    big = rows[(rows["kind"] == "big") & (rows["price_type"] == "book")]
    assert len(big) == 1 and big["price"].iloc[0] == pytest.approx(0.55) and big["h1"].notna().all()
    tr = rows[(rows["kind"] == "big") & (rows["price_type"] == "trade")]
    assert tr["price"].iloc[0] == pytest.approx(0.70)
    text = out.read_text(encoding="utf-8")
    for s in ("## 判定", "主规则（≥ 1.2 bp、盘口价）", "H1 顺势", "H2 趋势型", "| 买法 | 盘口价 | 成交价 |",
              "连续 3 天合计", "## 分解", "K 线缺", "| 2026-08-05 | 1 | 1 | 1 |"):
        assert s in text, s
    with gzip.open(out.with_suffix(".csv.gz"), "rt") as g:
        assert g.readline().strip().split(",")[:5] == ["market", "t0", "t_ex", "kind", "price_type"]


def test_klines_cache_download_and_fallback(tmp_path, monkeypatch):
    calls = []

    def download(url, dest):
        calls.append(url)
        if "2026-05" in url:
            return kline_zip(dest, S - 3600, 30)
        raise OSError("offline")

    monkeypatch.setattr(hf, "download", download)
    kl, missing = hf.klines(tmp_path, months=("2026-05", "2026-06"))
    assert missing == ["2026-06"] and len(kl) == 30 and (tmp_path / "BTCUSDT-1m-2026-05.zip").exists()
    assert kl.index[0] == S - 3600 and not (tmp_path / "BTCUSDT-1m-2026-06.zip").exists()
    kl2, _ = hf.klines(tmp_path, months=("2026-05",))
    assert calls.count(hf.KLINE_URL.format("2026-05")) == 1 and kl2.equals(kl)  # second time from the cache
    own = pd.Series([1.0, 2.0], index=[S - 3600, S + 10 * 86400])
    c = hf.closes_for(kl, own, missing)
    assert c.loc[S - 3600] == kl.loc[S - 3600] and c.loc[S + 10 * 86400] == 2.0
    assert hf.closes_for(kl, own, []).equals(kl)


def test_report_without_rows_and_market_seed():
    L = hf.report(pd.DataFrame(columns=hf.OUT_COLS), [], ["2026-06-01"], 1)
    assert any("没有可用的数据" in x for x in L) and any("读失败 1 个" in x for x in L)
    assert hf.market_seed("123") == hf.market_seed("123") != hf.market_seed("124")
