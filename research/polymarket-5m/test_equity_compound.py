import numpy as np
import pandas as pd

import equity_compound as ec


def test_simulate_sizing():
    t = pd.DataFrame({"t": [1.0, 2.0, 3.0], "price": [0.5, 0.5, 0.5], "size": [1000.0, 3.0, np.nan], "won": [1, 0, 1]})
    cost = 0.5 + 0.07 * 0.25
    p = ec.simulate(t, 300, 0.01, 1.0, cap=200, min_shares=5)
    sh1 = max(0.01 * 300 / cost, 5)
    eq1 = 300 + sh1 * (1 - cost)
    assert np.isclose(p["eq"].iloc[0], eq1)
    assert np.isclose(p["eq"].iloc[1], eq1 - 3 * cost)  # only 3 shares shown at the ask
    assert np.isclose(p["eq"].iloc[2], p["eq"].iloc[1])  # no size known: no trade
    big = ec.simulate(t.iloc[:1], 1e6, 0.01, 0.78, cap=200)
    assert np.isclose(big["eq"].iloc[0] - 1e6, 200 * 0.78 * (1 - cost))  # the cap, then the fill haircut


def test_rule_trades_and_weights(tmp_path):
    import scalein as si
    rows = pd.DataFrame({"market_id": [1, 1, 1], "t0": [0, 3000, 30000], "tau": [200, 197, 170], "up": [1, 1, 0],
                         "z": 2.2, "p0": 0.5, "fair_up": [0.7, 0.75, 0.2], "fair_a2_up": [0.7, 0.75, 0.2],
                         "m_m2": 0.5, "m_0": 0.5, "ua_l": 0.5, "da_l": 0.5, "uas_l": 100.0, "das_l": 100.0,
                         "ok": True, "up_won": 1.0, "day": "2026-06-01"})
    f = tmp_path / "c.csv.gz"
    rows.to_csv(f, index=False)
    first = ec.rule_trades([f], "G", "只买第一笔")
    every = ec.rule_trades([f], "G", si.BASE)
    double = ec.rule_trades([f], "G", "同向全仓，反向 2 倍")
    assert len(first) == 1 and len(every) == 3 and double["w"].tolist() == [1, 1, 2]
    p1 = ec.simulate(every, 300, 0.01, 1.0, min_shares=0)
    p2 = ec.simulate(double, 300, 0.01, 1.0, min_shares=0)
    step1, step2 = (p["eq"].iloc[-1] - p["eq"].iloc[-2] for p in (p1, p2))
    assert step1 < 0 and np.isclose(step2, 2 * step1, rtol=0.01)  # the Down add lost (Up won), twice as big
