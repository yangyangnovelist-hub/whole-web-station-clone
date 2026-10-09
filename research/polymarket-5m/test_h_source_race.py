from __future__ import annotations

import json

import pytest

import h_source_race as race


def _row(
    market_id: str,
    source: str,
    *,
    signal_ms: float,
    filled: bool,
    won: bool | None,
    pnl_per_share: float | None,
    filled_shares: float = 0.0,
) -> dict[str, object]:
    return {
        "market_id": market_id,
        "evaluation_ms": 500.0,
        "signal_receive_ms": signal_ms + 80.0,
        "signal_source_ms": signal_ms,
        "signal_source": source,
        "signal_ms": signal_ms,
        "direction": "Up",
        "fair": 0.70,
        "decision_ask": 0.40,
        "fixed_limit": 0.50,
        "sent": True,
        "reason": "filled" if filled else "insufficient_effective_depth",
        "filled": filled,
        "filled_shares": filled_shares,
        "fill_price": 0.40 if filled else None,
        "all_in_cost": 0.41 if filled else None,
        "winner": "Up" if won else ("Down" if won is False else None),
        "won": won,
        "pnl_per_share": pnl_per_share,
        "pnl": pnl_per_share * filled_shares if pnl_per_share is not None else None,
    }


def _summary(sources: frozenset[str], *, manifest_id: str = "same-run") -> dict[str, object]:
    return {
        "dataset": {
            "archive": "strict-sample",
            "sample_scope": "standard_forward_artifact",
            "collector_region": "eu-west-1",
            "paper_gate_eligible": True,
            "manifest": {
                "run_id": manifest_id,
                "schema": "polymarket-5m-strict-replay-v2",
                "receipt_race_ready": True,
            },
        },
        "protocol": {
            "strategy_id": race.REQUIRED_STRATEGY_ID,
            "scope": "current",
            "fill_qualification": "positive_fak",
            "endpoint_move_sensitivity": "unknown_closing_twap_samples/60",
            "candidate_sources": ["binance_spot_trade", "binance_futures_bookTicker"],
            "active_trigger_sources": sorted(sources),
        },
        "latencies": {
            "500": {
                "signals": 1,
                "sent": 1,
                "fills": 0,
                "filled_shares": 0.0,
                "fill_rate_per_signal": 0.0,
                "fill_rate_per_send": 0.0,
                "net_ev_per_share": None,
                "pnl": 0.0,
                "days": 0,
                "exact_p": 1.0,
                "reasons": {},
                "sent_by_source": {},
                "fills_by_source": {},
            },
        },
    }


def test_compare_archive_runs_independent_source_arms_and_reports_execution_delta(tmp_path, monkeypatch):
    calls: list[frozenset[str]] = []

    def fake_replay(archive, freeze, rows_out, trigger_sources):
        sources = frozenset(trigger_sources)
        calls.append(sources)
        if sources == race.BASELINE_SOURCES:
            rows = [_row("m1", "futures_book_ticker", signal_ms=1_000.0,
                         filled=False, won=None, pnl_per_share=None)]
            summary = _summary(sources)
        else:
            rows = [
                _row("m1", "deribit_quote", signal_ms=850.0, filled=True, won=True,
                     pnl_per_share=0.59, filled_shares=5.0),
            ]
            summary = _summary(sources)
            summary["latencies"]["500"].update({
                "signals": 1,
                "sent": 1,
                "fills": 1,
                "filled_shares": 5.0,
                "fill_rate_per_signal": 1.0,
                "fill_rate_per_send": 1.0,
                "net_ev_per_share": 0.59,
                "pnl": 2.95,
                "fills_by_source": {"deribit_quote": 1},
            })
        with open(rows_out, "w", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row) + "\n")
        return summary

    monkeypatch.setattr(race.run, "replay_archive", fake_replay)
    monkeypatch.setattr(race, "_capture_artifact_identity",
                        lambda _: ({"files": {"manifest.json": "sha"}}, {}, tmp_path))
    monkeypatch.setattr(race, "_artifact_stats", lambda _: {})

    rows_dir = tmp_path / "rows"
    result = race.compare_archive(tmp_path, rows_dir=rows_dir)

    assert calls == [race.BASELINE_SOURCES, race.CANDIDATE_SOURCES]
    assert result["comparison_method"] == "independent_full_state_replays_no_signal_pairing"
    assert result["arms"]["baseline"]["protocol"]["active_trigger_sources"] == sorted(race.BASELINE_SOURCES)
    assert result["arms"]["candidate"]["latencies"]["500"]["fills_by_source"] == {
        "deribit_quote": 1,
    }
    delta = result["latency_deltas"]["500"]
    assert delta["fills"] == 1
    assert delta["filled_shares"] == 5.0
    assert delta["pnl"] == pytest.approx(2.95)
    assert delta["loss_rate"] == pytest.approx(0.0)
    changes = result["causal_sequence_changes"]["500"]
    assert changes["changed_markets"] == 1
    assert changes["baseline_only_markets"] == 0
    assert changes["candidate_only_markets"] == 0
    assert changes["changed_market_id_samples"] == ["m1"]
    assert len(changes["changed_market_ids_sha256"]) == 64
    assert result["activation_gate"]["passes"] is False
    assert (rows_dir / "baseline.jsonl").is_file()
    assert (rows_dir / "candidate.jsonl").is_file()
    assert result["observation_files"]["baseline"]["bytes"] > 0
    assert len(result["observation_files"]["candidate"]["sha256"]) == 64


def test_compare_archive_rejects_dataset_identity_drift(tmp_path, monkeypatch):
    calls = 0

    def fake_replay(archive, freeze, rows_out, trigger_sources):
        nonlocal calls
        calls += 1
        sources = frozenset(trigger_sources)
        with open(rows_out, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(_row("m", "spot_trade", signal_ms=1.0,
                                         filled=False, won=None, pnl_per_share=None)) + "\n")
        return _summary(sources, manifest_id=f"run-{calls}")

    monkeypatch.setattr(race.run, "replay_archive", fake_replay)
    monkeypatch.setattr(race, "_capture_artifact_identity",
                        lambda _: ({"files": {"manifest.json": "sha"}}, {}, tmp_path))
    monkeypatch.setattr(race, "_artifact_stats", lambda _: {})

    with pytest.raises(ValueError, match="dataset identity drift"):
        race.compare_archive(tmp_path)


def test_compare_archive_rejects_non_v3_protocol(tmp_path, monkeypatch):
    def fake_replay(archive, freeze, rows_out, trigger_sources):
        with open(rows_out, "w", encoding="utf-8") as stream:
            pass
        result = _summary(frozenset(trigger_sources))
        result["protocol"]["strategy_id"] = "CURRENT-H-TIMESTAMPED-FIRST-V2"
        return result

    monkeypatch.setattr(race.run, "replay_archive", fake_replay)
    monkeypatch.setattr(race, "_capture_artifact_identity",
                        lambda _: ({"files": {"manifest.json": "sha"}}, {}, tmp_path))
    monkeypatch.setattr(race, "_artifact_stats", lambda _: {})

    with pytest.raises(ValueError, match="requires CURRENT-H-TIMESTAMPED-FIRST-TWAP-V3"):
        race.compare_archive(tmp_path)


def test_compare_archive_rejects_manifest_without_complete_receipt_race(tmp_path, monkeypatch):
    def fake_replay(archive, freeze, rows_out, trigger_sources):
        with open(rows_out, "w", encoding="utf-8"):
            pass
        result = _summary(frozenset(trigger_sources))
        result["dataset"]["manifest"]["receipt_race_ready"] = False
        return result

    monkeypatch.setattr(race.run, "replay_archive", fake_replay)
    monkeypatch.setattr(race, "_capture_artifact_identity",
                        lambda _: ({"files": {"manifest.json": "sha"}}, {}, tmp_path))
    monkeypatch.setattr(race, "_artifact_stats", lambda _: {})

    with pytest.raises(ValueError, match="receipt-race-ready"):
        race.compare_archive(tmp_path)


def test_formal_eligibility_requires_frozen_region_and_full_post_cutoff_day():
    frozen, _digest = race._load_source_race_freeze()
    cutoff = frozen["cutoff_ms"]
    identity = {
        "collector_region": "eu-west-1",
        "receipt_race_ready": True,
        "started_ms": cutoff + 5_000,
        "ended_ms": cutoff + 86_400_000 - 1,
    }

    assert race._formal_eligibility(identity, frozen) is True
    assert race._formal_eligibility({
        **identity,
        "started_ms": cutoff + 86_400_000 + 5_000,
        "ended_ms": cutoff + 2 * 86_400_000 - 1,
    }, frozen) is True
    assert race._formal_eligibility({**identity, "collector_region": "unknown"}, frozen) is False
    assert race._formal_eligibility({**identity, "started_ms": cutoff - 1}, frozen) is False
