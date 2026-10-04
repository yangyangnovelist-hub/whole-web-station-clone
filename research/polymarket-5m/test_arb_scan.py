import gzip
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import arb_scan as ar

S = 1_790_000_100 // 300 * 300  # a 5m boundary after 2026-07-01 (fee rate 0.07)
S_OLD = 1_775_000_100 // 300 * 300  # April 2026 (fee rate 0.072)


def fee(p, rate=0.07):
    return rate * p * (1 - p)


def kacho_rows(start=S, n=300, market="m1"):
    """Mirrored 1 s book: Up 0.49/0.50, Down 0.50/0.51, 100 shares everywhere."""
    t = np.arange(start, start + n)
    return pd.DataFrame({"market": market, "ts_ms": t * 1000, "start_ms": start * 1000, "end_ms": (start + 300) * 1000,
                         "horizon": 5, "bu": 0.49, "au": 0.50, "bd": 0.50, "ad": 0.51,
                         "bu_sz": 100.0, "au_sz": 100.0, "bd_sz": 100.0, "ad_sz": 100.0})


def test_edges_formula_and_fee_rate():
    f = kacho_rows(n=1).assign(au=0.45, ad=0.50, bu=0.55, bd=0.52)
    e = ar.edges(f)
    assert e["merge"][0][0] == pytest.approx(1 - 0.95 - fee(0.45) - fee(0.5))
    assert e["split"][0][0] == pytest.approx(1.07 - fee(0.55) - fee(0.52) - 1)
    assert e["merge"][2][0] and e["merge"][3][0] and e["split"][3][0]
    old = kacho_rows(start=S_OLD, n=1).assign(au=0.45, ad=0.50)
    assert ar.edges(old)["merge"][0][0] == pytest.approx(1 - 0.95 - fee(0.45, 0.072) - fee(0.5, 0.072))


def test_mirrored_book_has_no_arb():
    f = kacho_rows()
    st = ar.row_stats(f)
    assert st["mirror"] == len(f) and st["merge_raw"] == st["split_raw"] == st["merge_nofee"] == 0
    assert ar.scan(f, ar.KACHO_LAG_MS, 1000, 1000).empty


def test_kacho_episodes_persistence_and_execution():
    f = kacho_rows()
    i = lambda s: f.index[f["ts_ms"] == (S + s) * 1000]  # noqa: E731
    for s in (10, 11, 12):  # 3-row merge episode: seen at row 10, filled at row 12, still there
        f.loc[i(s), ["bd", "ad", "ad_sz"]] = (0.39, 0.40, 50.0)
    f.loc[i(12), "ad_sz"] = 40.0
    f.loc[i(20), ["bd", "ad"]] = (0.39, 0.40)  # one row: gone by row 22
    f.loc[i(30), ["bd", "ad", "ad_sz"]] = (0.39, 0.40, 3.0)  # too small to trade
    f.loc[i(31), ["bd", "ad", "ad_sz"]] = (0.39, 0.40, 3.0)
    f.loc[i(299), ["bd", "ad"]] = (0.39, 0.40)  # last row: the fill would come after the end
    ep = ar.scan(f.sample(frac=1, random_state=1), ar.KACHO_LAG_MS, 1000, 1000)
    raw = ep[(ep["kind"] == "merge") & (ep["level"] == "raw")].sort_values("ts_ms")
    tr = ep[(ep["kind"] == "merge") & (ep["level"] == "tradable")].sort_values("ts_ms")
    assert list(raw["rows"]) == [3, 1, 2, 1] and list(raw["dur_s"]) == [3.0, 1.0, 2.0, 1.0]
    assert list(tr["rows"]) == [3, 1, 1] and list(tr["exec"]) == [True, False, False]
    first = tr.iloc[0]
    assert first["exec_ts_ms"] == (S + 12) * 1000 and first["exec_size"] == 40.0
    assert first["exec_edge"] == pytest.approx(0.10 - fee(0.5) - fee(0.4))
    assert first["usd"] == pytest.approx(first["exec_edge"] * 40.0)
    assert first["tau"] == 290 and not first["crossed0"]
    assert not (ep["kind"] == "split").any()


def test_age_limit_and_crossed_rows():
    f = kacho_rows(n=3)  # rows at S, S+1, S+2, then nothing until S+20
    f = pd.concat([f, kacho_rows(start=S + 20, n=1).assign(start_ms=S * 1000, end_ms=(S + 300) * 1000)])
    f.loc[f["ts_ms"] == (S + 2) * 1000, ["bu", "au", "bd"]] = (0.60, 0.45, 0.50)  # Up crossed: ask < bid
    f.loc[f["ts_ms"] == (S + 2) * 1000, "ad"] = 0.51
    ep = ar.scan(f, 5000, 1000, 1000)
    e = ep[(ep["kind"] == "merge") & (ep["level"] == "tradable")]
    assert len(e) == 1 and e["crossed0"].iloc[0]
    assert e["exec"].iloc[0]  # the row at S+2 is the latest at S+7 and only 5 s old
    assert not ar.scan(f, 5000, 1000, 1000, max_age_ms=4000).query("kind == 'merge'")["exec"].any()


def test_kacho_frame_column_meanings():
    ticks = pd.DataFrame({"condition_id": ["c"], "t": [S + 5], "bu": [0.4], "au": [0.41], "bd": [0.59], "ad": [0.6],
                          "su": [11.0], "sd": [22.0], "sau": [33.0], "sad": [44.0]})
    mk = pd.DataFrame({"condition_id": ["c"], "market_start": [pd.Timestamp(S, unit="s", tz="UTC")],
                       "market_end": [pd.Timestamp(S + 300, unit="s", tz="UTC")]})
    f = ar.kacho_frame(ticks, mk)
    r = f.iloc[0]
    assert (r["bu_sz"], r["bd_sz"], r["au_sz"], r["ad_sz"]) == (11.0, 22.0, 33.0, 44.0)
    assert r["ts_ms"] == (S + 5) * 1000 and r["end_ms"] == (S + 300) * 1000 and r["start_ms"] == S * 1000


def test_kacho_report_writes(tmp_path):
    f = kacho_rows()
    f.loc[f["ts_ms"].between((S + 50) * 1000, (S + 53) * 1000), ["bd", "ad"]] = (0.56, 0.57)  # split: 0.49 + 0.56
    out = tmp_path / "k.md"
    ep, st = ar.kacho_report(f, out)
    text = out.read_text(encoding="utf-8")
    assert "拆分" in text and st["split_raw"] == 4
    s = ep[(ep["kind"] == "split") & (ep["level"] == "tradable")]
    assert len(s) == 1 and s["exec"].iloc[0] and s["rows"].iloc[0] == 4


def hf_feat():
    """5m market: a split arb for 3 snapshots (0.3 s); 15m market: a merge arb for 8 (0.8 s)."""
    rows = []
    for mid, start, mins in (("m5", S * 1000, 5), ("m15", S * 1000, 15)):
        for k in range(100):
            ts = start + 10_000 + 100 * k
            r = dict(timestamp_ms=ts, market_id=mid, lifecycle_state="active", observed_halt_flag=False,
                     up_best_bid=0.49, up_best_ask=0.50, down_best_bid=0.50, down_best_ask=0.51,
                     up_bid_size=100.0, up_ask_size=100.0, down_bid_size=100.0, down_ask_size=100.0)
            if mid == "m5" and 20 <= k < 23:
                r["down_best_bid"], r["down_best_ask"] = 0.56, 0.57
            if mid == "m15" and 40 <= k < 48:
                r["down_best_bid"], r["down_best_ask"], r["down_ask_size"] = 0.41, 0.42, 30.0
            rows.append(r)
    f = pd.DataFrame(rows)
    junk = f.iloc[:3].assign(lifecycle_state="pre_open", down_best_ask=0.1)  # not trading
    halted = f.iloc[3:4].assign(observed_halt_flag=True, down_best_ask=0.1)
    late = f.iloc[4:5].assign(timestamp_ms=S * 1000 + 300_000, down_best_ask=0.1)  # after the 5m end
    feat = pd.concat([f, junk, halted, late, f.iloc[:5]], ignore_index=True)  # plus duplicates
    mkts = pd.DataFrame({"market_id": ["m5", "m15"], "start": [S * 1000, S * 1000],
                         "end": [S * 1000 + 300_000, S * 1000 + 900_000], "horizon": [5, 15]})
    return feat, mkts


def test_hf_frame_and_half_second_persistence():
    feat, mkts = hf_feat()
    fr = ar.hf_frame(feat, mkts)
    assert len(fr) == 200 and set(fr["horizon"]) == {5, 15}
    ep = ar.scan(fr, ar.HF_LAG_MS, gap_ms=150, step_ms=100)
    tr = ep[ep["level"] == "tradable"]
    s = tr[tr["kind"] == "split"]
    m = tr[tr["kind"] == "merge"]
    assert len(s) == 1 and s["horizon"].iloc[0] == 5 and s["rows"].iloc[0] == 3 and not s["exec"].iloc[0]
    assert s["dur_s"].iloc[0] == pytest.approx(0.3)
    assert len(m) == 1 and m["horizon"].iloc[0] == 15 and m["rows"].iloc[0] == 8 and m["exec"].iloc[0]
    assert m["exec_ts_ms"].iloc[0] == m["ts_ms"].iloc[0] + 500 and m["exec_size"].iloc[0] == 30.0
    assert m["usd"].iloc[0] == pytest.approx((0.08 - fee(0.5) - fee(0.42)) * 30)


def test_latency_books_are_degenerate():
    books = pd.DataFrame({"market_id": ["a", "a", "b"], "ts": [1.0, 2.0, 3.0], "recv": [1.1, 2.1, 3.1],
                          "bid": [0.49, 0.60, 0.30], "ask": [0.50, 0.55, 0.32], "bid_size": 10.0, "ask_size": 10.0})
    d = ar.latency_degenerate(books)
    assert d["one_token"] and d["crossed"] == 1 and d["markets"] == 2
    assert d["min_spread"] == pytest.approx(-0.05)
    assert not ar.latency_degenerate(books.assign(down_bid=0.5))["one_token"]


def _gz(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt") as fh:
        fh.writelines(json.dumps(x) + "\n" for x in lines)


@pytest.fixture
def forward_root(tmp_path):
    b = tmp_path / "1" / "x" / "bundle-btc"
    lat = b / "latency"
    lat.mkdir(parents=True)
    slug = f"btc-updown-5m-{S}"
    with gzip.open(lat / "runtime_1000.poly_probability_observations_v1.csv.gz", "wt") as fh:
        fh.write("market_id,source_ts,receive_ts,best_bid,best_ask,bids_json,asks_json\n")
        fh.write(f'{slug},{S + 1}.000,{S + 1}.100,0.49,0.5,"[[0.49, 100]]","[[0.5, 100]]"\n')
        fh.write(f'{slug},{S + 2}.000,{S + 2}.100,0.52,0.5,"[[0.52, 100]]","[[0.5, 100]]"\n')
    with gzip.open(lat / "runtime_1000.market_registry.csv.gz", "wt") as fh:
        fh.write(f"market_id,start_ts\n{slug},{S}\n")
    with gzip.open(lat / "runtime_1000.market_outcomes.csv.gz", "wt") as fh:
        fh.write(f"market_id,winner\n{slug},Up\n")
    d = b / "data" / "polymarket" / "daily"
    _gz(d / "markets" / "BTC-5m" / "BTC-5m-markets-x.jsonl.gz",
        [{"slug": slug, "start_sec": S, "end_sec": S + 300, "token_ids": ["dn", "up"],
          "raw": {"outcomes": json.dumps(["Down", "Up"])}}])
    t0 = S * 1000

    def q(tok, ms, bid, ask):
        return {"asset_id": tok, "recv_ms": t0 + ms, "event_ts_ms": t0 + ms,
                "payload": {"best_bid": str(bid), "best_ask": str(ask)}}

    _gz(d / "best_bid_ask" / "BTC-5m" / "BTC-5m-best_bid_ask-x.jsonl.gz", [
        q("up", 1000, 0.49, 0.50), q("dn", 1000, 0.50, 0.51),
        q("dn", 5000, 0.39, 0.40), q("dn", 7000, 0.50, 0.51),  # 2 s merge arb: still there 0.5 s later
        q("dn", 9000, 0.39, 0.40), q("dn", 9200, 0.50, 0.51)])  # 0.2 s: gone before the fill

    def bk(tok, ms, bid, bs, ask, as_):
        return {"asset_id": tok, "recv_ms": t0 + ms,
                "payload": {"bids": [{"price": str(bid), "size": str(bs)}, {"price": "0.01", "size": "9"}],
                            "asks": [{"price": "0.99", "size": "9"}, {"price": str(ask), "size": str(as_)}]}}

    _gz(d / "book" / "BTC-5m" / "BTC-5m-book-x.jsonl.gz", [
        bk("up", 1000, 0.49, 100, 0.50, 100), bk("dn", 1000, 0.50, 100, 0.51, 100),
        bk("dn", 5000, 0.39, 100, 0.40, 60), bk("dn", 9000, 0.39, 100, 0.40, 60)])
    return tmp_path


def test_forward_detects_one_token_and_scans_bundle(forward_root, tmp_path):
    out = tmp_path / "f.md"
    deg, ep, st = ar.run_forward(forward_root, out)
    assert len(deg) == 1 and deg[0][1]["one_token"] and deg[0][1]["crossed"] == 1
    tr = ep[(ep["kind"] == "merge") & (ep["level"] == "tradable")].sort_values("ts_ms")
    assert list(tr["exec"]) == [True, False] and not tr["crossed0"].any()
    assert list(tr["dur_s"]) == [pytest.approx(2.0), pytest.approx(0.2)]
    assert tr["exec_size"].iloc[0] == 60.0 and tr["exec_edge"].iloc[0] == pytest.approx(0.10 - fee(0.5) - fee(0.4))
    text = out.read_text(encoding="utf-8")
    assert "只有 Up 一个 token" in text and "按构造不存在" in text


def test_forward_without_bundle_files_reports_only_degeneracy(forward_root, tmp_path):
    import shutil
    shutil.rmtree(forward_root / "1" / "x" / "bundle-btc" / "data")
    out = tmp_path / "f.md"
    deg, ep, st = ar.run_forward(forward_root, out)
    assert ep.empty and st is None and "两 token 的检验没跑" in out.read_text(encoding="utf-8")


def _hf_archive(path):
    """A daily archive in the whodisidk layout holding hf_feat()'s rows (sizes as best-first lists)."""
    import io
    import tarfile

    import pyarrow as pa
    import pyarrow.parquet as pq
    feat, mkts = hf_feat()
    f = feat.copy()
    for c in ("up_ask", "down_ask", "up_bid", "down_bid"):
        f[f"{c}_sizes"] = [[float(v), 1.0] for v in f.pop(f"{c}_size")]
    f.at[7, "up_bid_sizes"] = []  # an empty side: no size
    mk = pd.DataFrame({"market_id": mkts["market_id"], "slug": ["btc-updown-5m-x", "btc-updown-15m-x"],
                       "session_start_ts": mkts["start"], "session_end_ts": mkts["end"],
                       "chainlink_open_price": 1.0, "up_won": np.nan, "outcome_direction": None,
                       "lifecycle_state": "active", "up_token_id": "u", "down_token_id": "d"})
    with tarfile.open(path, "w:gz") as tar:
        for name, df in (("polymarket_features_100ms", f), ("polymarket_market_100ms", mk)):
            buf = io.BytesIO()
            pq.write_table(pa.Table.from_pandas(df, preserve_index=False), buf)
            info = tarfile.TarInfo(f"dataset={name}/date=2026-08-29/part-0.parquet")
            info.size = buf.tell()
            buf.seek(0)
            tar.addfile(info, buf)


def test_run_hf_end_to_end(tmp_path, monkeypatch):
    import shutil

    import cross
    src = tmp_path / "src.tar.gz"
    _hf_archive(src)
    name = "market_parquet_2026-08-29.tar.gz"

    calls = []

    def fake_fetch(n, dest=None):
        if n == "MANIFEST.txt":
            return f"{name} {src.stat().st_size}\n".encode()
        calls.append(n)
        if len(calls) == 1:  # the first download is cut off and must be retried
            Path(dest).write_bytes(src.read_bytes()[:1000])
        else:
            shutil.copy(src, dest)
        return dest

    monkeypatch.setattr(cross, "fetch", fake_fetch)
    out = tmp_path / "hf.md"
    ep, stats = ar.run_hf(tmp_path / "work", out)
    tr = ep[ep["level"] == "tradable"]
    assert sorted(stats) == [5.0, 15.0] and stats[5.0]["rows"] == 100
    assert list(tr.sort_values("kind")["exec"]) == [True, False]  # merge (15m) persists, split (5m) does not
    assert (tr["day"] == "2026-08-29").all() and (tmp_path / "hf.csv.gz").exists()
    text = out.read_text(encoding="utf-8")
    assert "15 分钟市场" in text and "5 分钟市场" in text
    assert not (tmp_path / "work" / name).exists()  # the archive is deleted after use
    assert calls == [name, name]


def test_lean_reader_matches_cross_read_day(tmp_path):
    import cross
    path = tmp_path / "a.tar.gz"
    _hf_archive(path)
    feat, mk, rs = cross.read_day(path)
    ref = ar.hf_frame(feat, cross.market_table(mk, rs))
    lf, lm = ar.read_day_lean(path)
    got = ar.hf_frame(lf, cross.market_table(lm, pd.DataFrame()))
    key = ["market", "ts_ms"]
    pd.testing.assert_frame_equal(ref.sort_values(key).reset_index(drop=True),
                                  got.sort_values(key).reset_index(drop=True), check_dtype=False)
    assert np.isnan(got.loc[(got["market"] == "m5") & (got["ts_ms"] == S * 1000 + 10_700), "bu_sz"]).all()
