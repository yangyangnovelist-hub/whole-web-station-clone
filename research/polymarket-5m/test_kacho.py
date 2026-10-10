import numpy as np
import pandas as pd
import pytest

import kacho as kc
import strategy_zoo as sz

S0 = 1_774_310_400  # 2026-03-24, inside the kacho period
N = 40


def write_coin(root, coin, seed=7):
    """A kacho-shaped dataset: N markets, the winner quoted at 0.90/0.91 after t=200."""
    rng = np.random.default_rng(seed)
    won = rng.random(N) < 0.5
    markets, ticks = [], []
    for i in range(N):
        start = S0 + 300 * i
        slug, cid = f"{coin}-updown-5m-{start}", f"0x{coin}{i:04x}"
        markets.append({"slug": slug, "condition_id": cid, "outcome": "Up" if won[i] else "Down"})
        for t in range(-5, 300):
            up = (0.90 if won[i] else 0.09) if t > 200 else 0.49
            bu, au = up, round(up + 0.01, 2)
            if i == 0 and t == 100:  # one odd second, to check when it becomes visible
                bu, au = 0.30, 0.31
            ticks.append({"t": start + t, "condition_id": cid, "bu": bu, "au": au, "bd": round(1 - au, 2),
                          "ad": round(1 - bu, 2), "su": 10.0, "sau": 10.0, "sd": 10.0, "sad": 10.0,
                          "du": 100.0, "dd": 50.0})
    pd.DataFrame(markets).to_parquet(root / f"{coin}_markets.parquet", index=False)
    pd.DataFrame(ticks).to_parquet(root / f"{coin}_ticks.parquet", index=False)
    secs = np.arange(S0 - 3600, S0 + 300 * N + 300)
    price = 70_000 * np.exp(np.cumsum(rng.normal(0, 1e-4, len(secs))))
    pd.DataFrame({"sec": secs, "value": price}).to_parquet(root / f"{coin}_binance_1s.parquet", index=False)
    return dict(zip((m["slug"] for m in markets), won))


@pytest.fixture(scope="module")
def root(tmp_path_factory):
    root = tmp_path_factory.mktemp("kacho")
    return root, write_coin(root, "btc")


def test_quotes_are_used_one_second_after_their_stamp(root):
    path, _ = root
    m = kc.build_markets(path)[0]
    assert m.at(100)["bid_up"] == 0.49 and m.at(101)["bid_up"] == 0.30 and m.at(102)["bid_up"] == 0.49
    assert m.at(0)["bid_up"] == 0.49  # stamped -1, usable at 0


def test_outcomes_prefer_gamma_over_dataset(root):
    path, won = root
    first = next(iter(won))
    gamma = pd.DataFrame({"slug": [first], "up_won": [not won[first]]})
    markets = kc.build_markets(path, outcomes=gamma)
    assert len(markets) == 1 and markets[0].up_won == (not won[first])  # only Gamma-resolved markets
    assert len(kc.build_markets(path)) == N


def test_candidates_trade_on_kacho(root):
    path, won = root
    spot = pd.read_parquet(path / "btc_binance_1s.parquet")
    markets = kc.build_markets(path, spot=spot)
    fav = [sz.late_favourite(30, 0.85, 0.99)(m) for m in markets]
    assert all(f is not None and (f["side"] == "Up") == won[m.slug] for m, f in zip(markets, fav))
    assert fav[0]["price"] == 0.91
    # Momentum needs a spot series and a vol forecast; both come from the Binance closes.
    assert markets[5].at(60)["spot_ok"] and np.isfinite(markets[5].at(60)[sz.rd.MAIN_VOL])
    assert sum(sz.chainlink_momentum(240, 10, 1.0)(m) is not None for m in markets) > 0
    # Imbalance runs on 5c depth: 100 bid vs 50 ask on Up.
    assert sz.book_imbalance(120, 0.3)(markets[0])["side"] == "Up"


def test_run_writes_report(root, tmp_path):
    path, _ = root
    text = kc.run(path, tmp_path / "kacho.md", reps=200)
    assert "预注册候选在 kacho.io 数据上的检验" in text and "附：100 个策略" in text
    assert "60 秒 TWAP 结算" not in text.split("---")[0]
    res = pd.read_csv(tmp_path / "kacho.csv")
    assert res["id"].tolist() == list(sz.PREREGISTERED)
    assert res.loc[res["id"] == 11, "n"].item() == N  # [0.85, 0.99] at 30s catches every winner at 0.91


def test_signal_lag_withholds_the_price(root):
    path, _ = root
    spot = pd.read_parquet(path / "btc_binance_1s.parquet")
    m1 = kc.build_markets(path, spot=spot, limit=1)[0]
    m2 = kc.build_markets(path, spot=spot, limit=1, signal_lag=2)[0]
    assert m1.at(60)["t_info"] == 59 and m2.at(60)["t_info"] == 58
    assert m2.at(60)["log_spot"] == m1.at(59)["log_spot"]


def test_stage2_pools_other_coins(tmp_path):
    write_coin(tmp_path, "eth", seed=11)
    text = kc.stage2(tmp_path, tmp_path / "s2.md", coins=("eth", "sol"), reps=200)
    assert "主检验：信号延后 2 秒" in text and "缺数据、未参与：sol" in text
    assert "| 62 |" in text and "| 54 |" in text and "结论：" in text
    per_coin = pd.read_csv(tmp_path / "s2.csv")
    assert set(per_coin["coin"]) == {"eth"} and set(per_coin["lag"]) == {1, 2}


def test_stale_rules_fire_only_on_a_still_book(tmp_path):
    coin_won = write_coin(tmp_path, "sol", seed=3)
    spot = pd.read_parquet(tmp_path / "sol_binance_1s.parquet")
    markets = kc.build_markets(tmp_path, spot=spot, coin="sol", signal_lag=2)
    m = markets[3]
    still = sz._unchanged_for(m)
    # The synthetic book sits at 0.49/0.50 until t=200, then jumps and sits again.
    assert still[150] >= 100 and still[202] <= 2
    # #102 buys the favourite once both book and price have been still for 20s inside tau 120..20.
    trade = sz.STALE[102].fn(m)
    if trade is not None:
        assert 180 <= trade["t"] <= 280 and 0.60 <= trade["price"] <= 0.95
    text = kc.stage3(tmp_path, tmp_path / "s3.md", coins=("sol",), reps=200)
    assert "第三阶段" in text and "| 101 |" in text and "| 102 |" in text


def test_diag101_separates_live_from_frozen_recordings(tmp_path):
    write_coin(tmp_path, "btc", seed=5)
    m = kc.build_markets(tmp_path, spot=pd.read_parquet(tmp_path / "btc_binance_1s.parquet"), detail=True)[0]
    assert {"su", "sau", "sd", "sad", "present"} <= set(m.rows.columns) and m.rows["present"].all()
    text = kc.diagnose_101(tmp_path, tmp_path / "d.md", coins=("btc",), reps=200)
    assert "判定" in text
    # The synthetic book never changes size, so any #101 trade here counts as a frozen recording.
    d = pd.read_csv(tmp_path / "d.csv") if (tmp_path / "d.csv").exists() else pd.DataFrame()
    if len(d):
        assert not d["live"].any() and d["all_present"].all()


def test_stage4_runs_the_onchain_taker_rules(tmp_path):
    write_coin(tmp_path, "xrp", seed=5)
    text = kc.stage4(tmp_path, tmp_path / "s4.md", coins=("xrp",), reps=200)
    assert "第四阶段" in text and "| 103 |" in text and "| 104 |" in text and "结论：" in text
