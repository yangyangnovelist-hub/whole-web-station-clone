"""Tests of roundtrip_run.py's bookkeeping (variant lists, N, settle-control map, selection with the
pooled N, frozen JSON, report) on synthetic stats; the engine itself is tested in test_roundtrip.py."""
import json

import numpy as np
import pandas as pd

import roundtrip as rt
import roundtrip_run as rr


def test_all_families_and_n():
    fams, names = rr.all_variants(None)
    sizes = {f: len(vs) for f, vs in fams.items()}
    assert list(sizes) == ["f1_binance_momentum", "f2_fair_gap", "f3_pm_reversion", "f4_pm_momentum",
                           "f5_book_imbalance", "f6_factor_model", "f7_time_price_bucket"]
    assert sizes == {"f1_binance_momentum": 4760, "f2_fair_gap": 918, "f3_pm_reversion": 1680, "f4_pm_momentum": 440,
                     "f5_book_imbalance": 45, "f6_factor_model": 960, "f7_time_price_bucket": 174}
    assert sum(sizes.values()) == 8977 and [names[f][0] for f in fams] == [f"f{i}" for i in range(1, 8)]


def test_control_map_is_the_same_signal_held_to_settlement():
    fams, _ = rr.all_variants(None)
    flat = [v for vs in fams.values() for v in vs]
    ctrl = rr.control_map(flat)
    by_id = {v.id: v for v in flat}
    assert all(c is not None for c in ctrl.values())                  # every family has a settle control
    for v in flat[::97]:
        c = by_id[ctrl[v.id]]
        assert c.exit.kind == "settle" and c.key == v.key and c.family == v.family
    assert ctrl["f1_binance_momentum:w1-bp4-all-all10-h10"] == "f1_binance_momentum:w1-bp4-all-all10-settle"
    assert ctrl["f7_time_price_bucket:t240-fav80-95-x15"] == "f7_time_price_bucket:t240-fav80-95-settle"


def fake_stats(variants, rng):
    rows = []
    for v in variants:
        r = {"id": v.id, "family": v.family, "name": v.name, "params": json.dumps(v.all_params(), sort_keys=True),
             "control": v.control, "n_signals": 100}
        for sp in "AB":
            mean = rng.normal(-0.02, 0.01)
            se = 0.005
            r.update({f"n_{sp}": 200, f"mk_{sp}": 150, f"mean_{sp}": mean, f"se_{sp}": se, f"sed_{sp}": se,
                      f"t_{sp}": mean / se, f"p_{sp}": rt._p_one_sided(mean / se), f"hold_{sp}": 30.0,
                      f"fb_{sp}": 0.1, f"forced_{sp}": 0.0, f"stopx_{sp}": 0.0, f"conv_{sp}": 0.0})
        rows.append(r)
    return pd.DataFrame(rows)


def test_selection_report_and_frozen_json(tmp_path):
    fams, names = rr.all_variants(None)
    small = {f: vs[:12] + [v for v in vs if v.exit.kind == "settle"][:1] for f, vs in fams.items()}
    small = {f: list({v.id: v for v in vs}.values()) for f, vs in small.items()}
    flat = [v for vs in small.values() for v in vs]
    st = fake_stats(flat, np.random.default_rng(0))
    win = small["f1_binance_momentum"][0].id                          # one clear winner in A and B
    st.loc[st["id"] == win, ["mean_A", "t_A", "p_A", "mean_B", "t_B", "p_B"]] = [0.05, 10.0, 1e-20, 0.04, 8.0, 1e-15]
    N = len(flat)
    sel, info = rt.select(st, n_total=N)
    assert info["N"] == N and info["k"] == 1 and info["k_B"] == 1
    ctrl = rr.control_map(flat)
    by_id = {r["id"]: r for r in sel.to_dict("records")}
    doc = rr.frozen_doc(sel, info, small, names, ctrl, by_id)
    assert [s["id"] for s in doc["survivors"]] == [win] and doc["N"] == N and doc["frozen_at"].endswith("+00:00")
    s = doc["survivors"][0]
    assert s["params"] == small["f1_binance_momentum"][0].all_params() and s["settle_control"]["id"] == ctrl[win]
    assert set(doc["describe"]) == set(small) and all(len(v) <= 10 for v in doc["describe"].values())
    assert doc["families"]["f1_binance_momentum"]["pass_B"] == 1
    json.dumps(doc, default=rr._num)
    arb = ("合并 / 拆分扫描", "合计每天 $0.00。")
    md = rr.report_md(sel, info, small, names, ctrl, by_id, None, "ok", "1 秒", doc["frozen_at"], arb=arb)
    for k in ("## 各族汇总", "**变体总数 N = ", "## 冻结名单（1 个）", "arb-scan-kacho.md", "同信号持有到结算",
              "（本身即持有到结算）", small["f1_binance_momentum"][0].name):
        assert k in md, k


def test_arb_summary_reads_the_scan_report(tmp_path):
    p = tmp_path / "arb.md"
    p.write_text("# 标题 X\n\n说明。\n\n## 结果\n\n第一段，结论。\n\n| a |\n", encoding="utf-8")
    assert rr.arb_summary(p) == ("标题 X", "第一段，结论。")
    assert rr.arb_summary(tmp_path / "missing.md") is None
