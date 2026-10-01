import numpy as np
import pandas as pd
import pytest

import zoo100

E0 = 1_787_000_100_000 // 900_000 * 900_000 + 900_000


def synth(n_mk=6, seed=0):
    """n_mk consecutive 5m markets ending E0 + 300 s * i whose Up mid follows Binance, and the 15m
    market ending with the third; one Polymarket print of 120 Up shares 100 s before the first end."""
    rng = np.random.default_rng(seed)
    tt = np.arange(E0 - 1_500_000, E0 + 300_000 * n_mk, 250)
    price = 80_000 * np.exp(np.cumsum(rng.normal(0, 2e-5, len(tt))))
    binance = pd.DataFrame({"trade_ts_ms": tt, "recv_ts_ms": tt + 150, "price": price})
    feats, rows = [], []
    for i, (mid_id, end, span, h) in enumerate([(f"m{i}", E0 + 300_000 * i, 300_000, 5) for i in range(n_mk)]
                                               + [("q15", E0 + 600_000, 900_000, 15)]):
        start = end - span
        ts = np.arange(start, end, 100)
        k = float(np.mean(np.interp(np.arange(start - 59_000, start + 1, 1000), tt, price)))
        mid = np.round(np.clip(0.5 + (np.interp(ts, tt, price) / k - 1) * 300, 0.02, 0.98), 2)
        feats.append(pd.DataFrame({"timestamp_ms": ts, "market_id": mid_id, "lifecycle_state": "active",
                                   "up_best_bid": mid - 0.01, "up_best_ask": mid + 0.01,
                                   "down_best_bid": 1 - mid - 0.01, "down_best_ask": 1 - mid + 0.01,
                                   "up_bid_size": 50.0 + (ts // 700) % 7, "up_ask_size": 40.0 + (ts // 300) % 5,
                                   "down_bid_size": 40.0, "down_ask_size": 50.0 + (ts // 500) % 3}))
        settle = float(np.mean(np.interp(np.arange(end - 59_000, end + 1, 1000), tt, price)))
        rows.append({"market_id": mid_id, "start": start, "end": end, "k": k, "up_won": float(settle >= k),
                     "horizon": h, "up_token": f"u{i}", "down_token": f"d{i}"})
    trades = pd.DataFrame({"recv_ts_ms": [E0 - 100_000, E0 - 99_000], "instrument": ["u0", "d0"],
                           "price": [0.5, 0.5], "size": [120.0, 30.0], "taker_side": ["buy", "buy"]})
    return pd.concat(feats, ignore_index=True), pd.DataFrame(rows), binance, trades


def test_exactly_100_rules():
    rules = zoo100.make_rules()
    assert len(rules) == 100 and [r[0] for r in rules] == list(range(1, 101))
    assert len({r[2] for r in rules}) == 100  # every rule is described differently


def test_state_grid_and_model_price():
    feat, mkts, binance, trades = synth()
    S = zoo100.build_state(feat, mkts, binance, trades)
    assert set(S["market_id"]) == {f"m{i}" for i in range(6)} and len(S) == 6 * len(zoo100.GRID_TAUS)
    assert S["ok"].all() and S["fair_m"].between(0, 1).all()
    assert S.loc[S["market_id"] == "m2", "p15"].notna().all() and S.loc[S["market_id"] != "m2", "p15"].isna().all()
    m0 = S[S["market_id"] == "m0"].set_index("tau")
    assert m0.loc[101, "flow10"] == 0.0 and m0.loc[100, "flow10"] == 120.0  # a print is known when it arrives
    assert m0.loc[95, "flow10"] == pytest.approx(120.0 - 30.0) and m0.loc[89, "flow10"] == 0.0
    assert m0.loc[89, "flow30"] == pytest.approx(90.0)
    # the mid follows Binance, so the model price and the mid agree in direction late in the market
    late = S[S["tau"] == 30]
    assert ((late["fair_m"] > 0.5) == (late["mid"] > 0.5)).mean() >= 5 / 6
    assert S["prev_up"].notna().sum() == 5 * len(zoo100.GRID_TAUS)


def test_evaluate_takes_the_first_healthy_second_at_the_later_ask():
    S = pd.DataFrame({"market_id": ["a"] * 3 + ["b"] * 2, "end": [1] * 3 + [2] * 2, "tau": [60, 59, 58, 60, 59],
                      "ok": [False, True, True, True, True], "mid": 0.8, "ua_f": [0.81, 0.82, 0.83, 0.70, 0.71],
                      "da_f": 0.2, "uas_f": 10.0, "das_f": 10.0, "up_won": [1.0] * 3 + [0.0] * 2})
    rule = [(7, "x", "buy Up when mid >= 0.8", lambda S: (S["mid"] >= 0.8, S["mid"] > 0.5))]
    t = zoo100.evaluate(S, rule).set_index("market_id")
    assert t.loc["a", "tau"] == 59 and t.loc["a", "price"] == 0.82 and t.loc["b", "price"] == 0.70
    assert t.loc["a", "pnl"] == pytest.approx(1 - 0.82 - 0.07 * 0.82 * 0.18)
    assert t.loc["b", "pnl"] == pytest.approx(-0.70 - 0.07 * 0.70 * 0.30)


def test_report_selects_on_a_and_checks_b_and_c():
    rng = np.random.default_rng(1)
    rows = []
    for per, day in (("A", "2026-06-01"), ("B", "2026-07-20"), ("C", "2026-08-20")):
        for rid, win in ((1, 0.9), (2, 0.5)):
            for i in range(300):
                rows.append({"rule": rid, "day": day, "price": 0.5, "fee": 0.0175,
                             "pnl": (1.0 if rng.random() < win else 0.0) - 0.5175})
    rules = [(1, "f", "good", None), (2, "f", "coin", None)] + [(i, "f", f"r{i}", None) for i in range(3, 101)]
    text = "\n".join(zoo100.report(pd.DataFrame(rows), rules, reps=500))
    assert "候选 1 条" in text and "都赚钱、且 B+C 合起来 p < 0.05/1 的：1 条" in text
