import gzip
import io
import json
import sqlite3

import pandas as pd
import pyarrow as pa

import run_local
from test_latency import N, S0, _latency_dir, export  # noqa: F401  (the module-scoped export fixture)


def _folder(export, d):
    """An export-style folder: the fixture's markets, a book updating every second (ask 0.50 until
    0.4 s after the +0.4% Binance jump at start+100, then 0.70) and the engine log's Binance trades."""
    starts = [S0 + 300 * i for i in range(N)]
    _latency_dir(d, export, [], starts)
    (d / "coinbase_trades.jsonl.gz").unlink()
    raw = b"".join(q.read_bytes() for q in sorted(export.glob("shadow_current.jsonl.gz.part-*")))
    (d / "shadow_current.jsonl.gz").write_bytes(raw)
    return d


def _zst_csv(path):
    raw = b"".join(q.read_bytes() for q in sorted(path.parent.glob(path.name + ".part-*")))
    return pd.read_csv(io.BytesIO(pa.CompressedInputStream(pa.BufferReader(raw), "zstd").read()))


def test_export_folder(export, tmp_path):
    d = _folder(export, tmp_path / "export-label")
    out = tmp_path / "r.md"
    run_local.main([str(d), "--out", str(out), "--reps", "200", "--split", "2026-09-01"])
    text = out.read_text()
    for name in ("G（检验 G）", "F（检验 F）", "H（2 秒前起算）", "M27（币安 5 秒动量）"):
        assert f"## {name}" in text
    t = pd.read_csv(out.with_suffix(".csv.gz"))
    f = t[t["strategy"] == "F（检验 F）"]
    assert len(f) == N and (f["price"] == 0.50).all()  # the stale ask 0.3 s after the jump
    m = t[t["strategy"] == "M27（币安 5 秒动量）"]
    # a second later on the grid the book had moved (the first market has no jump yet in its sigma
    # window, so noise can trigger it earlier)
    assert len(m) == N and (m["price"] == 0.70).sum() >= N - 1
    g = t[t["strategy"] == "G（检验 G）"]
    assert len(g) == N and g["range30"].eq(0).all() and g["mid_move2"].eq(0).all()


def test_sqlite_and_log(export, tmp_path):
    d = _folder(export, tmp_path / "x")
    db = tmp_path / "runtime_1000.sqlite"
    con = sqlite3.connect(db)
    pd.read_csv(d / "runtime_1000.poly_probability_observations_v1.csv.gz").to_sql(
        "poly_probability_observations_v1", con, index=False)
    _zst_csv(export / "runtime_1000.market_registry.csv.zst").to_sql("market_registry", con, index=False)
    _zst_csv(export / "runtime_1000.market_outcomes.csv.zst").to_sql("market_outcomes", con, index=False)
    con.execute("create table orders (id integer, secret text)")  # never read
    con.commit()
    con.close()
    raw = (d / "shadow_current.jsonl.gz").read_bytes()
    (tmp_path / "engine.jsonl.gz").write_bytes(raw)
    books, markets, spot = run_local.load([db, tmp_path / "engine.jsonl.gz"])
    assert markets["market_id"].nunique() == N and books["market_id"].nunique() == N
    assert len(spot) == len(gzip.decompress(raw).decode().splitlines())
    out = tmp_path / "r.md"
    run_local.main([str(db), str(tmp_path / "engine.jsonl.gz"), "--out", str(out), "--reps", "200"])
    t = pd.read_csv(out.with_suffix(".csv.gz"))
    assert len(t[t["strategy"] == "F（检验 F）"]) == N
