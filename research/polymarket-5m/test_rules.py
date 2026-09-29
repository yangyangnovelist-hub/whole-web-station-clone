import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

import onchain as oc
import rules as ru

COINS = ("btc", "eth", "sol", "xrp", "doge")


def fill(t, tok, price, buy, agg=True, amount=5.0):
    return {"timestamp": t, "token_asset_id": tok, "price": price, "token_amount": amount,
            "maker_direction": "BUY" if buy else "SELL", "taker_direction": "SELL" if buy else "BUY",
            "fee_usdc": 0.01 if agg else 0.0, "agg": agg}


def write(root, edge=True, n_per_day=40, days=range(1, 26)):
    """Each market: the Up ask shows 0.64 and the Up bid 0.62 from t=0 (so Up is the favourite at 0.64);
    Up wins 90% of the time if `edge`, else 64%. Binance is a flat random walk."""
    rng = np.random.default_rng(2)
    pth = oc.paths(root)
    pth["fills"].mkdir(parents=True)
    rows, fills, spot = [], [], []
    for d in days:
        day0 = oc.epoch(date(2026, 9, d))
        for i in range(n_per_day):
            start = day0 + 3600 + 300 * i
            coin = COINS[i % 5]
            u, dn = f"u{start}{coin}", f"d{start}{coin}"
            rows.append({"slug": f"{coin}-updown-5m-{start}", "coin": coin, "start": start, "up_token": u,
                         "down_token": dn, "up_won": bool(rng.random() < (0.90 if edge else 0.64))})
            for s in range(-20, 300, 4):
                t = start + s + ru.KNOWN_AFTER_BLOCK
                fills.append(fill(t, u, 0.64, buy=True))          # someone paid the Up ask
                fills.append(fill(t, dn, 0.38, buy=True))         # buying Down at 0.38 hits the Up bid 0.62
                fills.append(fill(t, u, 0.64, buy=False, agg=False))
        secs = np.arange(day0, day0 + 3600 + 300 * n_per_day + 600)
        spot.append(pd.DataFrame({"sec": secs, "value": 100 * np.exp(np.cumsum(rng.normal(0, 1e-5, len(secs)))),
                                  "coin": "x"}))
    pd.DataFrame(rows).to_csv(pth["markets"], index=False)
    pd.DataFrame(fills).to_parquet(pth["fills"] / "2026_09_01.parquet", index=False)
    sp = pd.concat(spot).drop(columns="coin")
    for c in COINS:
        sp.to_parquet(str(pth["binance"]).format(coin=c), index=False)


def test_quotes_come_from_aggressors_and_are_causal():
    start = 1_000_000
    t = np.array([start + 10, start + 10, start + 20]) + ru.KNOWN_AFTER_BLOCK
    st = ru.build_state(start, "btc", True, np.nan, t, np.array([True, False, True]), np.array([0.64, 0.38, 0.70]),
                        np.array([True, True, True]), np.array([True, True, True]), None, None)
    assert st.ask_up[7] == pytest.approx(0.64)  # nothing known yet, but the ask seen within 5 s is paid
    assert st.ask_up[10] == pytest.approx(0.64) and st.ask_down[10] == pytest.approx(0.38)
    assert st.ask_up[16] == pytest.approx(0.70)  # the 0.70 ask 4 s later is worse, so it is paid
    assert st.ask_up[25] == pytest.approx(0.70) and np.isnan(st.ask_up[35])  # 15 s old: stale
    assert st.fav(12) == "Up" and st.mid[12] == pytest.approx(0.63)


def test_registry_is_fixed():
    r = ru.registry()
    assert len({x.name for x in r}) == len(r) and [x.id for x in r] == list(range(1, len(r) + 1))


def test_a_planted_favourite_edge_is_found_and_judged(tmp_path):
    write(tmp_path)
    _, passed = ru.run(tmp_path, tmp_path / "r.md", reps=2000, cand_out=tmp_path / "c.json")
    names = {r.id: r.name for r in ru.registry()}
    assert passed and all("强势方" in names[i] and "[0.60,0.70)" in names[i] for i in passed)
    assert json.loads((tmp_path / "c.json").read_text())["ids"] == sorted(passed)
    ru.final(tmp_path, tmp_path / "c.json", tmp_path / "f.md", date(2026, 9, 20), date(2026, 9, 25), reps=2000)
    assert "✓" in (tmp_path / "f.md").read_text()


def test_no_edge_passes_nothing(tmp_path):
    write(tmp_path, edge=False, days=range(1, 21))
    assert ru.run(tmp_path, tmp_path / "r.md", reps=2000)[1] == set()


def test_maker_fills_only_when_a_later_trade_goes_through():
    start = 1_000_000
    t = np.array([start + 100, start + 100, start + 200, start + 250]) + ru.KNOWN_AFTER_BLOCK
    is_up = np.array([True, False, True, True])
    price = np.array([0.66, 0.36, 0.63, 0.60])   # ask 0.66, bid 0.64 (1 - 0.36); later trades 0.63 and 0.60
    agg = np.array([True, True, False, False])
    buy = np.array([True, True, True, True])
    st = ru.build_state(start, "btc", True, np.nan, t, is_up, price, agg, buy, None, None)
    got = ru.maker(195, "favourite", 0.60, 0.70)(st)  # rests at s=105 on the 0.64 bid
    assert got == ("Up", pytest.approx(0.64), 105, "maker")
    assert ru.maker(195, "favourite", 0.70, 0.80)(st) is None  # bid outside the band
    st2 = ru.build_state(start, "btc", True, np.nan, t[:2], is_up[:2], price[:2], agg[:2], buy[:2], None, None)
    assert ru.maker(195, "favourite", 0.60, 0.70)(st2) is None  # nothing traded below the bid afterwards


def test_registry_keeps_ids_when_rules_are_added():
    names = [r.name for r in ru.registry()]
    assert len(names) == 172 and names[0].startswith("强势方 τ=240") and names[116].startswith("挂买单")
