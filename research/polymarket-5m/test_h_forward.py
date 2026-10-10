import json

import pytest

import h_forward as hf


def row(market, signal, day, *, pnl=0.5, cost=0.4, evaluation=None, run="run-1"):
    evaluation = hf.PRIMARY_DELAY_MS if evaluation is None else evaluation
    return {"market_id": market, "evaluation_ms": evaluation, "signal_ms": signal,
            "direction": "Up", "signal_source": "futures_book_ticker", "filled": True,
            "winner": "Up", "won": True, "day": day, "pnl": pnl, "pnl_per_share": pnl / 5,
            "filled_shares": 5.0, "all_in_cost": cost, "sent": True, "reason": "filled",
            "observation_run_id": run, "strategy_id": hf.STRATEGY_ID}


def write(path, rows):
    path.write_text("".join(json.dumps(value) + "\n" for value in rows))


def test_add_is_idempotent_and_preserves_first_observation(tmp_path):
    store, fresh = tmp_path / "store.jsonl", tmp_path / "fresh.jsonl"
    first = row("m", 1, "2026-10-01", pnl=1.0)
    changed = row("m", 1, "2026-10-01", pnl=-1.0)
    write(fresh, [first])
    hf.add(fresh, store)
    write(fresh, [changed, row("n", 2, "2026-10-02")])

    pooled = hf.add(fresh, store)

    assert len(pooled) == 2
    assert pooled[0]["pnl"] == 1.0


def test_add_keeps_all_attempts_from_first_market_recording_only(tmp_path):
    store, fresh = tmp_path / "store.jsonl", tmp_path / "fresh.jsonl"
    write(fresh, [row("m", 1, "2026-10-01"), row("m", 2, "2026-10-01")])
    hf.add(fresh, store)
    write(fresh, [row("m", 3, "2026-10-01", run="run-2"),
                  row("n", 4, "2026-10-02", run="run-2")])

    pooled = hf.add(fresh, store)

    assert [(value["market_id"], value["signal_ms"], value["observation_run_id"])
            for value in pooled] == [("m", 1, "run-1"), ("m", 2, "run-1"),
                                      ("n", 4, "run-2")]


def test_add_rejects_observations_from_an_invalidated_strategy(tmp_path):
    store, fresh = tmp_path / "store.jsonl", tmp_path / "fresh.jsonl"
    stale = row("m", 1, "2026-10-01")
    stale["strategy_id"] = "CURRENT-H-TIMESTAMPED-FIRST-V2"
    write(fresh, [stale])

    with pytest.raises(ValueError, match="strategy_id"):
        hf.add(fresh, store)


def test_verdict_waits_for_both_fill_and_day_thresholds(monkeypatch):
    monkeypatch.setattr(hf, "MIN_FILLS", 5)
    rows = [row(str(index), index, "2026-10-01") for index in range(5)]
    assert hf.verdict(rows)["status"] == "collecting"


def test_positive_stopping_sample_passes_and_pin_is_immutable(tmp_path, monkeypatch):
    monkeypatch.setattr(hf, "MIN_FILLS", 7)
    monkeypatch.setattr(hf, "FAMILY_TESTS", 1)
    rows = [row(str(index), index, f"2026-10-{index + 1:02d}") for index in range(7)]
    result = hf.verdict(rows)
    assert result["status"] == "passed"
    assert result["net_ev_per_share"] > 0 and result["day_cluster_lower_99"] > 0
    assert result["exact_p"] < 0.01

    path = tmp_path / "verdict.json"
    first = hf.pin_verdict(rows, path)
    second = hf.pin_verdict([row("loss", 99, "2026-10-09", pnl=-5.0)], path)
    assert second == first


def test_family_correction_can_reject_an_uncorrected_signal(monkeypatch):
    monkeypatch.setattr(hf, "MIN_FILLS", 7)
    monkeypatch.setattr(hf, "FAMILY_TESTS", 100)
    rows = [row(str(index), index, f"2026-10-{index + 1:02d}") for index in range(7)]

    result = hf.verdict(rows)

    assert result["raw_exact_p"] < 0.01
    assert result["corrected_exact_p"] >= 0.01
    assert result["status"] == "rejected"
