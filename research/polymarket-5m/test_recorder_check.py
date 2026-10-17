import gzip

import numpy as np

import latency as lt
import recorder_check as rc
from test_latency import N, S0, _coinbase_lines, _latency_dir, export  # noqa: F401  (module fixture)


def _recording(export, root, delay=0.0):
    """The fixture's markets as a GitHub recording: Binance jumps at start + 100.05, the book updates
    every second and its ask moves 0.50 -> 0.70 at start + 100.4 (+ delay)."""
    starts = [S0 + 300 * i for i in range(N)]
    d = root / "1" / "x" / "bundle-btc" / "latency"
    _latency_dir(d, export, _coinbase_lines(export), starts)
    lines = [l.replace('"COINBASE_TRADE"', '"BINANCE_WS_TRADE"') for l in _coinbase_lines(export)]
    (d / "binance_trades.jsonl.gz").write_bytes(gzip.compress(("\n".join(lines) + "\n").encode()))
    if delay:
        import pandas as pd
        f = d / "runtime_1000.poly_probability_observations_v1.csv.gz"
        b = pd.read_csv(f)
        k = b["best_ask"] == 0.70
        jump = b.loc[k].groupby("market_id")["source_ts"].transform("min") == b.loc[k, "source_ts"]
        b.loc[b.index[k][jump.to_numpy()], "source_ts"] += delay
        b.sort_values("source_ts").to_csv(f, index=False, compression="gzip")
    return d


def test_response_and_first_move(export, tmp_path):
    d = _recording(export, tmp_path / "rec")
    books, markets, sp = lt.load(d, spot="binancews")
    r = rc.jump_rows(books, sp, markets)
    main = r[np.isclose((r["t0"] - S0) % 300, 100.05)]  # the designed jumps (noise can add a 2-sigma print)
    assert len(main) == N and (main["sign"] == 1).all() and np.allclose(main["first"], 0.35)
    assert (r.drop(main.index)[["d0.5", "d_end"]] == 0).all().all()
    s = rc.fingerprint(books, sp, markets)
    assert s["r0.3"] == 0 and s["r0.4"] == 1 and np.isclose(s["first50"], 0.35) and np.isclose(s["lag50"], 10)


def test_a_late_recorder_shows_the_reaction_later(export, tmp_path):
    fast = rc.fingerprint(*_load(_recording(export, tmp_path / "a")))
    slow = rc.fingerprint(*_load(_recording(export, tmp_path / "b", delay=0.3)))
    assert fast["r0.4"] == 1 and slow["r0.4"] == 0 and slow["r1"] == 1
    assert np.isclose(slow["first50"] - fast["first50"], 0.3)


def _load(d):
    books, markets, sp = lt.load(d, spot="binancews")
    return books, sp, markets


def test_github_report(export, tmp_path):
    _recording(export, tmp_path / "rec")
    out = tmp_path / "check.md"
    rc.main([str(tmp_path / "rec"), "--github", "--since", "2026-09-01", "--out", str(out)])
    text = out.read_text()
    assert "| GitHub 1 |" in text and "| 0% | 0% | 0% | 100% | 100% |" in text and "| 350 / 350 | 10 / 10 / 10 |" in text
