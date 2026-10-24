import numpy as np
import pandas as pd

import binary as bo
import latency as lt
import rt_ms as rt
from test_late_ms import JUMP, N, S0, _recording


def _book(rows):
    df = pd.DataFrame(rows, columns=["ts", "bid", "ask", "bid_size", "ask_size"])
    df["market_id"] = "m"
    df["recv"] = df["ts"] + 0.01
    return lt.Book(df.sort_values("ts", kind="stable").reset_index(drop=True))


def _rows(changes):
    """An Up book updating every 0.5 s over the market; `changes` = [(from_t, (bid, ask, bs, as)), ...]."""
    out = []
    for t in sorted(set(np.arange(S0, S0 + 300, 0.5).tolist()) | {c[0] for c in changes}):
        state = (0.49, 0.50, 40.0, 25.0)
        for at, st in changes:
            if t >= at:
                state = st
        out.append((t, *state))
    return out


def test_entry_and_exit_use_rows_at_or_before_their_match():
    t0 = S0 + 100.0
    t_in = t0 + rt.SEND + rt.M
    t_out = t_in + 10 + rt.M
    book = _book(_rows([(t_in + 0.001, (0.59, 0.60, 40.0, 25.0)),      # after the entry match: not used for it
                        (t_out - 0.2, (0.69, 0.70, 40.0, 25.0)),       # before the exit match: used
                        (t_out + 0.001, (0.10, 0.11, 40.0, 25.0))]))   # after the exit match: not used
    pnl, ask, bid, settled = rt.trip(book, "m", S0, "Up", t0, "Up", 10)
    assert ask == 0.50 and bid == 0.69 and not settled
    assert np.isclose(pnl, 0.69 - 0.50 - bo.taker_fee(0.50) - bo.taker_fee(0.69))
    # Down: ask = 1 - Up bid at the entry match, bid = 1 - Up ask at the exit match
    pnl, ask, bid, _ = rt.trip(book, "m", S0, "Up", t0, "Down", 10)
    assert np.isclose(ask, 0.51) and np.isclose(bid, 0.30)


def test_exit_capped_at_fifteen_seconds_left_and_settles_without_a_bid():
    t0 = S0 + 280.0  # entry near the end: exit decided at 285 s at the latest
    book = _book(_rows([]))
    pnl, ask, bid, settled = rt.trip(book, "m", S0, "Up", t0, "Up", 60)
    assert not settled and bid == 0.49
    empty = _book([(t, 0.0, 0.50, 0.0, 25.0) for t in np.arange(S0, S0 + 300, 0.5)])  # no bid size
    pnl, ask, bid, settled = rt.trip(empty, "m", S0, "Up", S0 + 100.0, "Up", 10)
    assert settled and np.isclose(pnl, 1 - 0.50 - bo.taker_fee(0.50))


def test_cli_end_to_end(tmp_path, monkeypatch):
    """late_ms's synthetic recording: a 40 bp jump at start + 100.85, the Up ask 0.50 -> 0.80 0.18 s
    later. Bought at 0.80 and sold at the 0.79 bid: -1c - both fees, whatever the outcome."""
    _recording(tmp_path / "rec")
    monkeypatch.setattr(lt, "G_SINCE", "2026-09-01")
    out = tmp_path / "rt.md"
    rt.main([str(tmp_path / "rec"), "--out", str(out)])
    text = out.read_text(encoding="utf-8")
    exp = 100 * (0.79 - 0.80 - bo.taker_fee(0.80) - bo.taker_fee(0.79))
    assert f"| ≥ 4 bp | 10 | {exp:+.2f}¢ ±0.00（{N:,} / {N:,}）" in text
    assert f"{N:,} 个 BTC 5m 市场" in text
