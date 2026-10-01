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


def test_extra_selection(tmp_path, capsys):
    e = pd.DataFrame({"strategy": ["G（检验 G）", "G 加仓", "G 加仓", "H（2 秒前起算）", "F（检验 F）", "M27（币安 5 秒动量）"],
                      "t": [1e12, 1e9, 1e9 + 2, 1e9, 1e9, 1e9], "price": 0.4, "size": 50.0, "won": 1.0})
    f = tmp_path / "x.csv.gz"
    e.to_csv(f, index=False)
    g1 = ec.extra_trades(f, "G", False)
    assert len(g1) == 1 and g1["t"].iloc[0] == 1e9  # ms read as seconds
    assert len(ec.extra_trades(f, "G", True)) == 2
    assert len(ec.extra_trades(f, "H", False)) == 1
    assert len(ec.extra_trades(f, "H", True)) == 1  # no "H 加仓": falls back to the first trades
    assert "no 'H 加仓' rows" in capsys.readouterr().out
