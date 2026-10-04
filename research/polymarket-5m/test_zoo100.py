import hashlib

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
    assert zoo100.FILL_MS == 500


def test_state_grid_and_model_price():
    feat, mkts, binance, trades = synth()
    S = zoo100.build_state(feat, mkts, binance, trades)
    assert set(S["market_id"]) == {f"m{i}" for i in range(6)} and len(S) == 6 * len(zoo100.GRID_TAUS)
    assert S["ok"].all() and S["fair_m"].between(0, 1).all()
    assert S["decision_ok"].all() and S["fill_ok"].all()
    assert S.loc[S["market_id"] == "m2", "p15"].notna().all() and S.loc[S["market_id"] != "m2", "p15"].isna().all()
    m0 = S[S["market_id"] == "m0"].set_index("tau")
    assert m0.loc[101, "flow10"] == 0.0 and m0.loc[100, "flow10"] == 120.0  # a print is known when it arrives
    assert m0.loc[95, "flow10"] == pytest.approx(120.0 - 30.0) and m0.loc[89, "flow10"] == 0.0
    assert m0.loc[89, "flow30"] == pytest.approx(90.0)
    # the mid follows Binance, so the model price and the mid agree in direction late in the market
    late = S[S["tau"] == 30]
    assert ((late["fair_m"] > 0.5) == (late["mid"] > 0.5)).mean() >= 5 / 6
    assert S["prev_binance_up"].notna().all()


def test_previous_window_rules_do_not_read_the_previous_market_outcome():
    feat, mkts, binance, trades = synth()
    flipped = mkts.copy()
    flipped["up_won"] = 1.0 - flipped["up_won"]

    original = zoo100.build_state(feat, mkts, binance, trades)
    rescored = zoo100.build_state(feat, flipped, binance, trades)
    np.testing.assert_array_equal(
        original["prev_binance_up"].to_numpy(),
        rescored["prev_binance_up"].to_numpy(),
    )
    previous_rules = [rule for rule in zoo100.make_rules() if rule[1] == "上一窗"]
    assert len(previous_rules) == 2
    for _, _, _, rule in previous_rules:
        original_mask, original_side = rule(original)
        rescored_mask, rescored_side = rule(rescored)
        np.testing.assert_array_equal(original_mask, rescored_mask)
        np.testing.assert_array_equal(original_side, rescored_side)


def test_binance_features_ignore_a_trade_not_received_by_the_decision_time():
    feat, mkts, binance, trades = synth(n_mk=1)
    decision_ms = E0 - 60_000
    delayed_index = binance.index[binance["trade_ts_ms"] == decision_ms - 250][0]
    delayed = binance.copy()
    delayed.loc[delayed_index, "recv_ts_ms"] = decision_ms + 10_000
    delayed = delayed.sort_values("recv_ts_ms", kind="stable")
    repriced = delayed.copy()
    repriced.loc[delayed_index, "price"] *= 4

    before = zoo100.build_state(feat, mkts, delayed, trades)
    after = zoo100.build_state(feat, mkts, repriced, trades)
    cols = ["r1", "r5", "r30", "r60", "rv60", "fair_m"]
    before_row = before[(before["market_id"] == "m0") & (before["tau"] == 60)].iloc[0]
    after_row = after[(after["market_id"] == "m0") & (after["tau"] == 60)].iloc[0]
    np.testing.assert_allclose(before_row[cols].to_numpy(float), after_row[cols].to_numpy(float),
                               rtol=0, atol=0, equal_nan=True)


def test_fill_uses_the_latest_book_already_received_at_500ms():
    feat, mkts, binance, trades = synth(n_mk=1)
    decision_ms = E0 - 60_000
    is_m0 = feat["market_id"] == "m0"
    gap = is_m0 & (feat["timestamp_ms"] > decision_ms + 400) & (feat["timestamp_ms"] < decision_ms + 800)
    feat = feat[~gap].copy()
    for timestamp, up_bid, up_ask, down_bid, down_ask in (
        (decision_ms + 400, 0.39, 0.40, 0.59, 0.60),
        (decision_ms + 800, 0.79, 0.80, 0.19, 0.20),
    ):
        row = (feat["market_id"] == "m0") & (feat["timestamp_ms"] == timestamp)
        feat.loc[row, ["up_best_bid", "up_best_ask", "down_best_bid", "down_best_ask"]] = [
            up_bid, up_ask, down_bid, down_ask,
        ]

    state = zoo100.build_state(feat, mkts, binance, trades)
    row = state[(state["market_id"] == "m0") & (state["tau"] == 60)].iloc[0]
    assert row["ua_f"] == pytest.approx(0.40)
    assert bool(row["fill_ok"])


def test_fill_health_never_depends_on_a_book_change_after_500ms():
    feat, mkts, binance, trades = synth(n_mk=1)
    decision_ms = E0 - 60_000
    feat = feat[(feat["market_id"] != "m0") | (feat["timestamp_ms"] <= decision_ms + 500)].copy()

    state = zoo100.build_state(feat, mkts, binance, trades)
    row = state[(state["market_id"] == "m0") & (state["tau"] == 60)].iloc[0]
    assert bool(row["fill_ok"])


def test_evaluate_freezes_first_signal_even_when_it_does_not_fill():
    S = pd.DataFrame({"market_id": ["a"] * 3 + ["b"] * 2 + ["c"], "end": [1] * 3 + [2] * 2 + [3],
                      "tau": [60, 59, 58, 60, 59, 60],
                      "ok": [True, True, True, True, True, True], "mid": 0.8,
                      "ua_0": [0.40, 0.42, 0.43, 0.70, 0.71, 0.50],
                      "da_0": 0.2,
                      # a@60 has a cheap surviving quote but only four shares; b@60 moved above
                      # the limit.  Later rows would fill, but a one-order protocol cannot use that
                      # future fact to replace the first signal.  c is a normal fill.
                      "ua_f": [0.39, 0.41, 0.42, 0.72, 0.70, 0.49],
                      "da_f": 0.2, "uas_f": [4.0, 10.0, 10.0, 10.0, 10.0, 6.0], "das_f": 10.0,
                      "fee_rate": [0.07, 0.07, 0.07, 0.07, 0.07, 0.072],
                      "up_won": [1.0] * 3 + [0.0] * 2 + [1.0]})
    rule = [(7, "x", "buy Up when mid >= 0.8", lambda S: (S["mid"] >= 0.8, S["mid"] > 0.5))]
    t = zoo100.evaluate(S, rule).set_index("market_id")
    assert t["sent"].all()
    assert t.loc["a", "tau"] == 60 and not t.loc["a", "filled"]
    assert t.loc["a", "no_fill_reason"] == "insufficient_depth"
    assert t.loc["b", "tau"] == 60 and not t.loc["b", "filled"]
    assert t.loc["b", "no_fill_reason"] == "above_limit"
    assert t.loc["c", "filled"] and t.loc["c", "limit"] == 0.50 and t.loc["c", "price"] == 0.49
    assert t.loc["c", "pnl"] == pytest.approx(1 - 0.49 - 0.072 * 0.49 * 0.51)


def test_evaluate_freezes_raw_first_signal_before_decision_checks():
    S = pd.DataFrame({
        "market_id": ["bad_book", "bad_book", "bad_limit", "bad_limit", "good"],
        "end": [1, 1, 2, 2, 3],
        "tau": [60, 59, 60, 59, 60],
        "ok": True,
        "decision_ok": [False, True, True, True, True],
        "fill_ok": True,
        "mid": 0.8,
        "ua_0": [0.40, 0.41, 0.99, 0.50, 0.50],
        "da_0": 0.20,
        "ua_f": [0.39, 0.40, 0.50, 0.49, 0.49],
        "da_f": 0.20,
        "uas_f": 10.0,
        "das_f": 10.0,
        "fee_rate": 0.07,
        "up_won": 1.0,
    })
    rule = [(1, "x", "raw signal", lambda frame: (frame["mid"] >= 0.8, frame["mid"] > 0.5))]

    result = zoo100.evaluate(S, rule).set_index("market_id")

    assert result.loc["bad_book", "tau"] == 60
    assert not result.loc["bad_book", "sent"]
    assert result.loc["bad_book", "no_send_reason"] == "book_unavailable"
    assert result.loc["bad_limit", "tau"] == 60
    assert not result.loc["bad_limit", "sent"]
    assert result.loc["bad_limit", "no_send_reason"] == "invalid_limit"
    assert result.loc["good", "sent"] and result.loc["good", "filled"]


def test_model_edge_rule_uses_decision_ask_not_future_ask():
    S = pd.DataFrame({
        "tau": [180], "fair_m": [0.70],
        "ua_0": [0.69], "da_0": [0.31],
        "ua_f": [0.40], "da_f": [0.60],
        "fee_rate": [0.07],
    })
    rule46 = next(rule for rule in zoo100.make_rules() if rule[0] == 46)
    mask, _ = rule46[3](S)
    assert not bool(mask.iloc[0])


def test_report_selects_on_a_and_checks_b_and_c():
    rng = np.random.default_rng(1)
    rows = []
    for per, day in (("A", "2026-06-01"), ("B", "2026-07-20"), ("C", "2026-08-20")):
        for rid, win in ((1, 0.9), (2, 0.5)):
            for i in range(300):
                filled = i < 200
                rows.append({"rule": rid, "day": day, "price": 0.5, "fee": 0.0175,
                             "filled": filled,
                             "won": (won := float(rng.random() < win)) if filled else 0.0,
                             "pnl": (won - 0.5175) if filled else 0.0})
    rules = [(1, "f", "good", None), (2, "f", "coin", None)] + [(i, "f", f"r{i}", None) for i in range(3, 101)]
    text = "\n".join(zoo100.report(pd.DataFrame(rows), rules, reps=500))
    assert "候选 1 条" in text and "都赚钱、且 B+C 合起来 p < 0.05/1 的：1 条" in text
    assert "200/300" in text  # fills/sends, not a leaked assumption that every send filled


def test_report_uses_the_deterministic_exact_poisson_binomial_tail():
    rows = pd.DataFrame({
        "rule": 1,
        "day": "2026-06-01",
        "price": 0.5,
        "fee": 0.0,
        "won": [1.0] * 8 + [0.0] * 2,
        "filled": True,
        "pnl": [0.5] * 8 + [-0.5] * 2,
    })
    rules = [(1, "f", "exact", None)] + [(i, "f", f"r{i}", None) for i in range(2, 101)]

    one_rep = zoo100.report(rows, rules, reps=1)
    many_reps = zoo100.report(rows, rules, reps=99_999)

    assert one_rep == many_reps
    assert "0.0547" in "\n".join(one_rep)


def test_report_rejects_rows_outside_the_frozen_date_range():
    rows = pd.DataFrame({"rule": [1], "day": ["2026-08-30"], "price": [0.5], "fee": [0.0],
                         "won": [1.0], "filled": [True], "pnl": [0.5]})
    rules = [(1, "f", "r1", None)]
    with pytest.raises(ValueError, match="2026-05-25.*2026-08-29"):
        zoo100.report(rows, rules)


def archive_names():
    return list(zoo100.EXPECTED_ARCHIVES)


def stub_archive_run(monkeypatch, *, missing=(), failing=(), include_outside=False):
    import cross

    names = [name for name in archive_names() if name[15:25] not in set(missing)]
    if include_outside:
        names = ["market_parquet_2026-05-24.tar.gz", *names, "market_parquet_2026-08-30.tar.gz"]
    monkeypatch.setattr(zoo100, "ZOO_MANIFEST_SHA256", hashlib.sha256(b"manifest").hexdigest())
    monkeypatch.setattr(cross, "archives", lambda manifest: [(name, 7) for name in names])
    fetched = []

    def fetch(name, dest=None):
        if dest is None:
            return b"manifest"
        day = name[15:25]
        fetched.append(name)
        if day in set(failing):
            raise OSError(f"cannot read {day}")
        dest.write_bytes(b"archive")
        return dest

    monkeypatch.setattr(cross, "fetch", fetch)
    monkeypatch.setattr(cross, "read_day", lambda path: (pd.DataFrame(), pd.DataFrame(), pd.DataFrame()))
    monkeypatch.setattr(cross, "read_binance", lambda path: pd.DataFrame())
    monkeypatch.setattr(cross, "read_poly_trades", lambda path: pd.DataFrame())
    monkeypatch.setattr(cross, "market_table", lambda markets, resolutions: pd.DataFrame())
    monkeypatch.setattr(zoo100, "build_state", lambda *args: pd.DataFrame())
    monkeypatch.setattr(zoo100, "evaluate", lambda *args: pd.DataFrame())
    return fetched


def test_run_uses_only_the_frozen_dates_and_reports_segment_coverage(tmp_path, monkeypatch):
    fetched = stub_archive_run(monkeypatch, include_outside=True)
    out = tmp_path / "zoo.md"

    zoo100.run(tmp_path / "work", out)

    assert fetched == archive_names()
    text = out.read_text(encoding="utf-8")
    assert "A 46/46" in text and "B 8/8" in text and "C 13/13" in text


def test_run_fails_closed_when_a_required_day_is_missing(tmp_path, monkeypatch):
    stub_archive_run(monkeypatch, missing={"2026-07-17"})
    out = tmp_path / "zoo.md"

    with pytest.raises(RuntimeError, match="2026-07-17") as error:
        zoo100.run(tmp_path / "work", out)

    assert "B 7/8" in str(error.value)
    assert not out.exists()


def test_run_fails_closed_when_a_required_day_cannot_be_read(tmp_path, monkeypatch):
    stub_archive_run(monkeypatch, failing={"2026-08-20"})
    out = tmp_path / "zoo.md"

    with pytest.raises(RuntimeError, match="2026-08-20") as error:
        zoo100.run(tmp_path / "work", out)

    assert "C 12/13" in str(error.value)
    assert not out.exists()


def test_zoo_uses_the_dataset_given_on_the_command_line(monkeypatch):
    """Run as `python cross.py zoo --dataset X`, cross.py is __main__ and zoo100 imports another copy
    of it, so the dataset has to be passed on explicitly."""
    import importlib.util
    import cross
    seen = {}
    monkeypatch.setattr(zoo100, "run", lambda workdir, out, days=None, dataset=None: seen.update(ds=dataset))
    spec = importlib.util.spec_from_file_location("cross_as_script", cross.__file__)  # a second copy, as __main__ is
    main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main)
    main.main(["zoo", "--dataset", "whodisidk/polymarket-eth-updown-exchange-data", "--out", "x.md"])
    assert seen["ds"] == "whodisidk/polymarket-eth-updown-exchange-data"
    assert cross.DS == "whodisidk/polymarket-btc-updown-exchange-data"  # the imported copy is untouched
