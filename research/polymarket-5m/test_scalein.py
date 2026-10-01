import numpy as np
import pandas as pd

import scalein as si


def _t():
    # market 1: Up, Up 3 s later, Down 20 s later; market 2: one Down
    return pd.DataFrame({"t": [0.0, 3.0, 23.0, 5.0], "market_id": [1, 1, 1, 2], "up": [True, True, False, False],
                         "edge": [0.13, 0.15, 0.25, 0.30]}).sort_values("t").reset_index(drop=True)


def test_rules():
    t = _t()
    w = lambda name: dict(zip(zip(t["market_id"], t["t"]), si.apply(t, si.RULES[name])))
    assert list(w("只买第一笔").values()).count(1.0) == 2 and w("只买第一笔")[(1, 3.0)] == 0
    assert all(v == 1.0 for v in w("每次都加（现在的）").values())
    assert w("只加反向")[(1, 3.0)] == 0 and w("只加反向")[(1, 23.0)] == 1
    assert w("只加同向")[(1, 3.0)] == 1 and w("只加同向")[(1, 23.0)] == 0
    assert w("最多 2 笔")[(1, 23.0)] == 0
    assert w("加仓要边际 ≥ 20¢")[(1, 3.0)] == 0 and w("加仓要边际 ≥ 20¢")[(1, 23.0)] == 1
    assert w("加仓距上一笔 ≥ 10 秒")[(1, 3.0)] == 0 and w("加仓距上一笔 ≥ 10 秒")[(1, 23.0)] == 1  # 23 s after the first
    assert w("加仓半仓")[(1, 23.0)] == 0.5 and w("加仓半仓")[(1, 0.0)] == 1.0


def test_extra(tmp_path):
    e = pd.DataFrame({"strategy": ["G 加仓", "G 加仓", "G（检验 G）"], "t": [1e9, 1e9 + 3, 1e9], "market_id": 7,
                      "side": ["Up", "Down", "Up"], "price": 0.4, "fee": 0.0168, "fair": 0.6, "size": 50.0, "won": [1.0, 0.0, 1.0]})
    f = tmp_path / "x.csv.gz"
    e.to_csv(f, index=False)
    x = si.extra_episodes(f, "G")
    assert len(x) == 2 and x["up"].tolist() == [True, False] and np.allclose(x["edge"], 0.6 - 0.4 - 0.0168)
    assert (x["period"] == "X").all() and si.extra_episodes(f, "H") is None
    w = si.apply(x, si.RULES["只加反向"])
    assert w.tolist() == [1.0, 1.0]
