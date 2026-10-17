from datetime import date

import numpy as np
import pandas as pd
import pytest

import makers as mk
import onchain as oc

COINS = ("btc", "eth", "sol", "xrp", "doge")


def write(root, edge=True, n_per_day=60, days=range(1, 26)):
    """Markets where resting buyers of the favourite at 0.85 with 30-10 s left win 97% of the time
    (if `edge`), and aggressors buying at 0.50 with 150 s left are fair."""
    rng = np.random.default_rng(1)
    pth = oc.paths(root)
    pth["fills"].mkdir(parents=True)
    rows, fills = [], []
    for d in days:
        for i in range(n_per_day):
            start = oc.epoch(date(2026, 9, d)) + 300 * i
            coin = COINS[i % 5]
            up_won = bool(rng.random() < 0.5)
            rows.append({"slug": f"{coin}-updown-5m-{start}", "coin": coin, "start": start,
                         "up_token": f"u{start}{coin}", "down_token": f"d{start}{coin}", "up_won": up_won})
            fav_wins = rng.random() < (0.97 if edge else 0.85)
            fav = f"u{start}{coin}" if up_won == fav_wins else f"d{start}{coin}"
            end = start + 300
            fills.append({"timestamp": end - 20 + mk.DELAY_S, "token_asset_id": fav, "price": 0.85,
                          "token_amount": 10.0, "maker_direction": "BUY", "taker_direction": "SELL",
                          "fee_usdc": 0.0, "agg": False})
            # the same trade seen from the aggressor's order: sold the favourite at 0.85
            fills.append({"timestamp": end - 20 + mk.DELAY_S, "token_asset_id": fav, "price": 0.85,
                          "token_amount": 10.0, "maker_direction": "SELL", "taker_direction": "BUY",
                          "fee_usdc": 0.089, "agg": True})
            coin_flip = f"u{start}{coin}" if rng.random() < 0.5 else f"d{start}{coin}"
            fills.append({"timestamp": start + 150, "token_asset_id": coin_flip, "price": 0.50,
                          "token_amount": 5.0, "maker_direction": "BUY", "taker_direction": "SELL",
                          "fee_usdc": 0.0875, "agg": True})
    pd.DataFrame(rows).to_csv(pth["markets"], index=False)
    pd.DataFrame(fills).to_parquet(pth["fills"] / "2026_09_01.parquet", index=False)


def test_positions_turn_sells_into_the_other_side(tmp_path):
    write(tmp_path, n_per_day=5, days=(1,))
    markets, fills = oc.load(tmp_path, extra=["maker_direction", "fee_usdc", "agg"])
    pos = mk.positions(fills, markets)
    lp = pos[~pos["agg"]]
    ag = pos[pos["agg"] & (pos["tb"] == 2)]  # 30-10 s left
    assert lp["q"].tolist() == pytest.approx([0.85] * len(lp)) and (lp["tb"] == 2).all()
    assert ag["q"].tolist() == pytest.approx([0.15] * len(ag))  # sold the favourite = long the other side
    assert (lp["won"].to_numpy() + ag["won"].to_numpy() == 1).all()


def test_a_resting_edge_is_selected_and_confirmed(tmp_path):
    write(tmp_path)
    passed = mk.run(tmp_path, tmp_path / "r.md", reps=2000)
    text = (tmp_path / "r.md").read_text()
    assert passed == ["挂单方 q∈[0.8,0.9) 剩 30–10 秒"], text
    assert "吃单方付费、挂单方不付费" in text and "结论：1 个格子通过" in text


def test_no_edge_nothing_passes(tmp_path):
    write(tmp_path, edge=False)
    assert mk.run(tmp_path, tmp_path / "r.md", reps=2000) == []
