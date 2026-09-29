import pandas as pd

import forward as fw
import strategy_zoo as sz
from test_real_day import S0, make_bundle


def trades(rows):
    return pd.DataFrame([{"id": i, "slug": f"btc-updown-5m-{s}", "start": s, "side": "Up", "price": 0.9,
                          "fee": 0.0063, "won": w, "pnl": float(w) - 0.9063, "t": 270, "kind": "taker"}
                         for i, s, w in rows])


def test_add_pools_runs_without_double_counting(tmp_path):
    bundle = make_bundle(tmp_path / "bundle")
    store = tmp_path / "forward"
    trades([(10, S0, True), (61, S0, False)]).to_csv(tmp_path / "a.csv", index=False)
    trades([(10, S0, True), (10, S0 + 300, True)]).to_csv(tmp_path / "b.csv", index=False)
    assert fw.add(tmp_path / "a.csv", bundle, store) == (2, 2)
    assert fw.add(tmp_path / "b.csv", bundle, store) == (3, 2)  # (10, S0) seen twice, kept once
    (tmp_path / "empty.csv").write_text("")
    assert fw.add(tmp_path / "empty.csv", bundle, store) == (3, 2)


def test_report_uses_preregistered_ids(tmp_path):
    bundle = make_bundle(tmp_path / "bundle")
    store = tmp_path / "forward"
    assert "还没有录到数据" in fw.report(store)
    trades([(10, S0, True), (99, S0, True)]).to_csv(tmp_path / "a.csv", index=False)
    fw.add(tmp_path / "a.csv", bundle, store)
    text = fw.report(store, reps=100)
    assert "预注册候选的前向检验" in text and f"只检验 {len(sz.PREREGISTERED)} 个" in text
    assert "| 99 |" not in text  # not a candidate, ignored
    assert "#101 的前向检验" in text


def test_add_ledger_pools_paper_trades(tmp_path):
    row = {"slug": f"btc-updown-5m-{S0}", "end": S0 + 300, "tau": 30, "side": "Up", "ask": 0.9, "shares": 10.0,
           "fee": 0.063, "cost": 9.063, "won": True, "pnl": 0.937, "mid_up": 0.9, "decided_ms": 1}
    pd.DataFrame([row]).to_csv(tmp_path / "l1.csv", index=False)
    pd.DataFrame([row, {**row, "tau": 60}]).to_csv(tmp_path / "l2.csv", index=False)
    store = tmp_path / "forward"
    assert fw.add_ledger(tmp_path / "missing.csv", store) == 0
    assert fw.add_ledger(tmp_path / "l1.csv", store) == 1
    assert fw.add_ledger(tmp_path / "l2.csv", store) == 2
    assert "| 30s | 1 |" in (store / "paper_report.md").read_text(encoding="utf-8")


def test_report_tests_the_onchain_rules_on_later_markets_only(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path / "bundle")
    store = tmp_path / "forward"
    trades([(103, S0, True), (104, S0 + 300, False)]).to_csv(tmp_path / "a.csv", index=False)
    fw.add(tmp_path / "a.csv", bundle, store)
    assert "九月链上规律的前向检验" not in fw.report(store, reps=100)  # recorded before the rules existed
    monkeypatch.setattr(fw, "ONCHAIN_ADDED", 0)
    text = fw.report(store, reps=100)
    assert "九月链上规律的前向检验" in text and "| 103 |" in text and "| 104 |" in text
