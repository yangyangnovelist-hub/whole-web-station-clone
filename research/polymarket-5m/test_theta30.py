import gzip
import json

import pandas as pd
import pytest

import theta30
from asian180 import BookView


def _view(up_bid=.08, up_ask=.10, size=20):
    return BookView(
        src_ms=1_000,
        rcv_ms=1_010,
        ask_up=up_ask,
        ask_down=1 - up_bid,
        ask_up_size=size,
        ask_down_size=size,
    )


def test_theta30_buys_only_the_displayed_favourite_inside_the_frozen_band():
    spec = theta30.Theta30Spec()
    down_favourite = theta30.select_order(_view(), spec)
    assert down_favourite is not None
    assert down_favourite.side_up is False
    assert down_favourite.decision_ask == pytest.approx(.92)
    assert down_favourite.fixed_limit == pytest.approx(.93)

    assert theta30.select_order(_view(up_bid=.02, up_ask=.03), spec) is None  # favourite ask .98
    assert theta30.select_order(_view(up_bid=.21, up_ask=.23), spec) is None  # favourite ask .79


def test_theta30_requires_capacity_and_never_adds_routine_insurance():
    spec = theta30.Theta30Spec()
    assert theta30.select_order(_view(size=4.99), spec) is None
    assert spec.insurance == "profitable_lock_after_confirmed_fill_only"


def test_theta30_summary_charges_fees_and_reports_fill_rate():
    rows = pd.DataFrame({
        "sent": [True, True, False],
        "filled": [True, False, False],
        "won": [True, False, False],
        "pnl_per_share": [.05, 0, 0],
        "all_in_cost": [.95, 0, 0],
        "pnl": [.25, 0, 0],
        "day": ["d1", "d1", "d1"],
    })
    got = theta30.summarize(rows, sims=1_000)
    assert got["sent"] == 2
    assert got["fills"] == 1
    assert got["fill_rate"] == pytest.approx(.5)
    assert got["mean_pnl_per_share"] == pytest.approx(.05)


def test_latency_export_loader_builds_the_common_up_book(tmp_path):
    def write(name, rows):
        with gzip.open(tmp_path / name, "wt") as f:
            f.write("\n".join(rows) + "\n")

    write("runtime_1000.market_registry.csv.gz", ["market_id,start_ts", "m1,1000"])
    write("runtime_1000.market_outcomes.csv.gz", ["market_id,winner", "m1,Down"])
    write("runtime_1000.poly_probability_observations_v1.csv.gz", [
        "market_id,source_ts,receive_ts,best_bid,best_ask,bids_json,asks_json",
        'm1,1270.000,1270.010,0.08,0.10,"[[0.08, 10]]","[[0.10, 12]]"',
    ])
    markets, books = theta30.load_latency_export(tmp_path)
    assert markets.to_dict("records") == [{"market_id": "m1", "start_ts": 1000, "winner": "Down"}]
    assert books.loc[0, "src_ms"] == 1_270_000
    assert books.loc[0, "b1s"] == pytest.approx(10)
    assert books.loc[0, "a1s"] == pytest.approx(12)


def test_forward_pool_excludes_prefreeze_rows_and_keeps_first_recording_per_market():
    prior = pd.DataFrame({"market_id": ["same"], "decision_ms": [2_000], "sent": [True]})
    new = pd.DataFrame({
        "market_id": ["old", "same", "new"],
        "decision_ms": [999, 2_000, 3_000],
        "sent": [True, False, True],
    })
    got = theta30.combine_forward(prior, new, freeze_ms=1_000)
    assert list(got.market_id) == ["same", "new"]
    assert bool(got.loc[got.market_id == "same", "sent"].item()) is True
