import numpy as np
import pandas as pd

import scalein as si
from test_latency import export  # noqa: F401  (the module-scoped export fixture)

RULE = {r.name: r for r in si.RULES}


def _t():
    """Market 1: a 10c Up jump (not a first trade), the first trade (Up 0.40), a cheaper Up add,
    a 9c Down jump that would lock profit, a 30c Down jump at 0.70 that would not; market 2: one."""
    t = pd.DataFrame({
        "t": [0.0, 2.0, 4.0, 20.0, 40.0, 5.0], "market_id": [1, 1, 1, 1, 1, 2],
        "up": [True, True, True, False, False, False], "price": [0.40, 0.40, 0.35, 0.50, 0.70, 0.30],
        "edge": [0.10, 0.13, 0.15, 0.09, 0.30, 0.20], "pre_ok": True, "tau": [230, 228, 200, 180, 50, 200],
        "z": [2.1, 2.1, 2.2, 2.6, 3.5, 2.0], "mid": [0.45, 0.45, 0.45, 0.48, 0.72, 0.35], "size": 1000.0,
        "won": [0, 0, 0, 1, 1, 1.0], "day": "2026-06-01", "period": "A"})
    t["fee"] = 0.07 * t["price"] * (1 - t["price"])
    t["pnl"] = t["won"] - t["price"] - t["fee"]
    return t.sort_values("t", kind="stable").reset_index(drop=True)


def w(name, t=None):
    t = _t() if t is None else t
    return dict(zip(t["t"], si.weights(t, RULE[name])))


def test_rules():
    assert w("只买第一笔") == {0: 0, 2: 1, 4: 0, 20: 0, 40: 0, 5: 1}
    assert w(si.BASE) == {0: 0, 2: 1, 4: 1, 20: 0, 40: 1, 5: 1}
    assert w("加仓门槛 8¢")[20] == 1
    assert w("只加反向") == {0: 0, 2: 1, 4: 0, 20: 0, 40: 1, 5: 1}
    assert w("只加反向且能锁利")[40] == 0  # 0.40 + 0.70 + fees > 1
    assert w("只加反向 + 反向门槛 8¢")[20] == 1 and w("只加反向 + 反向门槛 8¢")[40] == 1
    assert w("反向只在能锁利时加，同向照常")[4] == 1
    assert w("最多 2 笔")[40] == 0
    assert w("距上一笔 ≥ 10 秒")[4] == 0 and w("距上一笔 ≥ 10 秒")[40] == 1
    assert w("同向只在比上一笔便宜时加")[4] == 1 and w("同向只在比上一笔贵时加")[4] == 0
    assert w("同向只在浮盈时加（顺势加码）")[4] == 1  # mid 0.45 above the 0.40 + fee paid
    assert w("同向只在浮亏时加（摊低成本）")[4] == 0
    assert w("加仓要急动 ≥ 3σ")[4] == 0 and w("加仓要急动 ≥ 3σ")[40] == 1
    assert w("加仓只在剩 < 60 秒")[4] == 0 and w("加仓只在剩 < 60 秒")[40] == 1
    assert w("加仓价 ≤ 0.5")[40] == 0
    k = w("第 k 笔 1/k 倍")
    assert k[4] == 0.5 and np.isclose(k[40], 1 / 3)
    assert w("每个市场最多 2 倍仓位")[40] == 0
    assert w("第一笔半仓，加仓全仓")[2] == 0.5 and w("同向全仓，反向 2 倍")[40] == 2
    assert np.isclose(w("加仓按边际（边际/12¢，最多 3 倍）")[40], 2.5)
    t = _t()
    t.loc[t["t"] == 4, "pre_ok"] = False  # G: the mid had already moved
    assert w(si.BASE, t)[4] == 0 and w("加仓不用 G 的“已被抢先”过滤", t)[4] == 1


def test_daily_and_run():
    t = _t()
    t["size"] = [1000, 1000, 2.0, 1000, 1000, 1000]
    usd, n, adds, c = si.daily(t, si.weights(t, RULE["加仓 2 倍"]))
    assert n == 4 and adds == 2
    cost = t["price"] + t["fee"]
    sh = np.minimum(3 * np.array([0, 1, 2, 1, 0, 2]) / cost, t["size"]) * 0.78  # rows in time order
    assert np.isclose(usd.sum(), (sh * t["pnl"]).sum()) and sh[2] == 2 * 0.78  # at most the shares shown
    res = si.run(t, [RULE[si.BASE], RULE["只买第一笔"]], ["A"])
    assert res[si.BASE]["A"]["diff"] == 0 and res["只买第一笔"]["A"]["n"] == 2


def test_episodes_from_run_local(export, tmp_path):
    """run_local.py --episodes writes the jitter columns; the first trades scalein.py takes from
    them are run_local's G and H trades."""
    import run_local
    from test_run_local import _folder
    d = _folder(export, tmp_path / "export-label")
    out = tmp_path / "r.md"
    run_local.main([str(d), "--out", str(out), "--reps", "200", "--episodes"])
    ep = tmp_path / "r-episodes.csv.gz"
    trades = pd.read_csv(out.with_suffix(".csv.gz"))
    for rule, name in (("G", "G（检验 G）"), ("H", "H（2 秒前起算）")):
        t = si.episodes(ep, rule, period="X")
        first = t[si.weights(t, RULE["只买第一笔"]) > 0].set_index("market_id").sort_index()
        g = trades[trades["strategy"] == name].set_index("market_id").sort_index()
        assert len(first) == len(g) > 0
        assert np.allclose(first["t"], g["t"]) and np.allclose(first["price"], g["price"])
