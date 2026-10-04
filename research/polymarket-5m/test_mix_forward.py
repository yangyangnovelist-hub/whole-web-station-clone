import copy
import gzip
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest

import mix_forward as F
import mix_hf as M


SLOT = 1_800_000_000


def source_event(kind, seq, recv_ms, **extra):
    return {"kind": kind, "seq": seq, "recv_ms": recv_ms, "source_ts_ms": extra.pop("source_ts_ms", None),
            **extra}


def clob_event(kind, seq, recv_ms, epoch=1, **extra):
    return {"kind": kind, "seq": seq, "recv_ms": recv_ms, "source_ts_ms": extra.pop("source_ts_ms", None),
            "connection_epoch": epoch, **extra}


def snapshot(seq, recv_ms, source_ms, token, asks, bids=((0.39, 20.0),)):
    return clob_event(
        "clob_snapshot", seq, recv_ms, source_ts_ms=source_ms, market_id="m1", asset_id=token,
        asks=[{"price": str(price), "size": str(size)} for price, size in asks],
        bids=[{"price": str(price), "size": str(size)} for price, size in bids],
    )


def quiet_then_jump_events(receive_lag_ms=100.0):
    rows = [source_event("spot_connection", 0, (SLOT - 20) * 1000, stream="spot", stream_sequence=0,
                         connection_epoch=1)]
    price = 60_000.0
    seq = 1
    stream_seq = 1
    for second in range(-10, 100):
        price *= math.exp((1 if second % 2 else -1) * 1e-6)
        source_ms = (SLOT + second) * 1000.0
        rows.append(source_event("spot_trade", seq, source_ms + receive_lag_ms, source_ts_ms=source_ms,
                                 stream="spot", stream_sequence=stream_seq, connection_epoch=1,
                                 price=price, size=1.0))
        seq += 1
        stream_seq += 1
    price *= math.exp(0.001)
    source_ms = (SLOT + 100) * 1000.0
    rows.append(source_event("spot_trade", seq, source_ms + receive_lag_ms, source_ts_ms=source_ms,
                             stream="spot", stream_sequence=stream_seq, connection_epoch=1,
                             price=price, size=1.0))
    rows.append(source_event("spot_disconnect", seq + 1, source_ms + 2_000, stream="spot",
                             stream_sequence=stream_seq + 1, connection_epoch=1))
    return rows, source_ms + receive_lag_ms


def mapping():
    return {"kind": "market_mapping", "market_id": "m1", "slot": SLOT,
            "up_token_id": "up", "down_token_id": "down"}


def test_receipt_trigger_applies_raw_spacing_before_the_frozen_z_filter():
    cfg = F.ControlConfig(sigma_window_s=4, sigma_min_observations=3, z_cut=5.0, tau_hi_s=300.0)
    trigger = F.ReceiptJumpTrigger(cfg)
    price = 100.0
    for second in range(11):
        ret = 0.0 if second == 0 else (0.0001 if second % 2 else -0.0001)
        price *= math.exp(ret)
        trigger.update((SLOT + second) * 1000.0, (SLOT + second) * 1000.0, price)
    price *= math.exp(0.00021)
    first = trigger.update((SLOT + 11) * 1000.0, (SLOT + 11) * 1000.0, price)
    assert first is not None and first.kind == "jump" and first.z < cfg.z_cut
    price *= math.exp(0.0008)
    assert trigger.update((SLOT + 13) * 1000.0, (SLOT + 13) * 1000.0, price) is None


def test_aggregate_spot_adapter_preserves_the_frozen_mix_tape_and_rejects_regression(tmp_path):
    path = tmp_path / "latency" / "binance_trades.jsonl.gz"
    path.parent.mkdir()
    payload = [
        {"event": "BINANCE_WS_TRADE", "trade_ts": 100.0, "receive_ts": 100.1, "price": 60_000},
        {"event": "BINANCE_WS_TRADE", "trade_ts": 100.2, "receive_ts": 100.3, "price": 60_001},
    ]
    with gzip.open(path, "wt") as stream:
        stream.writelines(json.dumps(row) + "\n" for row in payload)
    rows = list(F.iter_aggregate_spot_events(tmp_path))
    assert [row["kind"] for row in rows] == ["spot_connection", "spot_trade", "spot_trade", "spot_disconnect"]
    assert rows[1]["source_ts_ms"] == 100_000 and rows[1]["recv_ms"] == 100_100

    payload[1]["receive_ts"] = 99.0
    with gzip.open(path, "wt") as stream:
        stream.writelines(json.dumps(row) + "\n" for row in payload)
    with pytest.raises(ValueError, match="receipt time regressed"):
        list(F.iter_aggregate_spot_events(tmp_path))


def test_strict_control_replay_fills_five_direct_shares_and_scores_afterward():
    source, signal_ms = quiet_then_jump_events()
    due = signal_ms + 500.0
    clob = [
        clob_event("clob_connection", 0, signal_ms - 1_000, source_ts_ms=None, token_count=2),
        snapshot(1, signal_ms - 900, signal_ms - 920, "up", ((0.32, 20.0),)),
        snapshot(2, signal_ms - 899, signal_ms - 919, "down", ((0.69, 20.0),), bids=((0.68, 20.0),)),
        snapshot(3, due - 5, due - 20, "up", ((0.40, 20.0),)),
        snapshot(4, due - 4, due - 19, "down", ((0.60, 20.0),), bids=((0.59, 20.0),)),
        clob_event("clob_error", 5, due + 2_000, source_ts_ms=None, error="closed"),
    ]
    cfg = F.ControlConfig(fill_latencies_ms=(500.0,), sigma_window_s=20, sigma_min_observations=10,
                          z_cut=5.834074974060059)
    rows, counters = F.replay_normalized(source, clob, [mapping()], {"m1": "Up"}, cfg)
    assert counters["selected"] == 1 and len(rows) == 1
    row = rows[0]
    assert row["kind"] == "jump" and row["direction"] == "Up"
    assert row["filled"] and row["filled_shares"] == pytest.approx(5.0)
    assert row["fill_price"] == pytest.approx(0.40)
    fee = M.fee(0.40)
    assert row["all_in_cost"] == pytest.approx(row["fill_price"] + fee)
    assert row["pnl_per_share"] == pytest.approx(1.0 - row["all_in_cost"])
    assert row["winner"] == "Up" and row["won"] is True


def test_control_replay_requires_only_the_bought_token_ask():
    source, signal_ms = quiet_then_jump_events()
    due = signal_ms + 500.0
    clob = [
        clob_event("clob_connection", 0, signal_ms - 1_000, source_ts_ms=None, token_count=2),
        snapshot(1, due - 5, due - 20, "up", ((0.40, 20.0),), bids=()),
        clob_event("clob_error", 2, due + 2_000, source_ts_ms=None, error="closed"),
    ]
    cfg = F.ControlConfig(fill_latencies_ms=(500.0,), sigma_window_s=20, sigma_min_observations=10)
    rows, _ = F.replay_normalized(source, clob, [mapping()], {"m1": "Up"}, cfg)
    assert len(rows) == 1 and rows[0]["filled"]
    assert rows[0]["fill_price"] == pytest.approx(0.40)


def test_valid_bought_token_ask_does_not_require_the_internal_ready_flag():
    source, signal_ms = quiet_then_jump_events()
    due = signal_ms + 500.0
    clob = [
        clob_event("clob_connection", 0, signal_ms - 1_000, source_ts_ms=None, token_count=2),
        snapshot(1, due - 100, due - 120, "up", ((0.40, 20.0),)),
        clob_event("clob_price_change", 2, due - 5, source_ts_ms=due - 20,
                   market_id="m1", asset_id="up", price="0.50", size="1", side="SELL",
                   best_bid="0.37", best_ask="0.40"),
        clob_event("clob_error", 3, due + 2_000, source_ts_ms=None, error="closed"),
    ]
    cfg = F.ControlConfig(fill_latencies_ms=(500.0,), sigma_window_s=20, sigma_min_observations=10)
    rows, _ = F.replay_normalized(source, clob, [mapping()], {"m1": "Up"}, cfg)
    assert len(rows) == 1 and rows[0]["filled"]
    assert rows[0]["fill_price"] == pytest.approx(0.40)


def test_control_replay_rejects_a_crossed_bid_on_the_bought_token():
    source, signal_ms = quiet_then_jump_events()
    due = signal_ms + 500.0
    clob = [
        clob_event("clob_connection", 0, signal_ms - 1_000, source_ts_ms=None, token_count=2),
        snapshot(1, due - 5, due - 20, "up", ((0.40, 20.0),), bids=((0.41, 20.0),)),
        clob_event("clob_error", 2, due + 2_000, source_ts_ms=None, error="closed"),
    ]
    cfg = F.ControlConfig(fill_latencies_ms=(500.0,), sigma_window_s=20, sigma_min_observations=10)
    rows, _ = F.replay_normalized(source, clob, [mapping()], {"m1": "Up"}, cfg)
    assert len(rows) == 1 and not rows[0]["filled"]


def test_other_token_activity_does_not_refresh_the_bought_token():
    source, signal_ms = quiet_then_jump_events()
    due = signal_ms + 500.0
    clob = [
        clob_event("clob_connection", 0, due - 2_100, source_ts_ms=None, token_count=2),
        snapshot(1, due - 2_000, due - 2_020, "up", ((0.40, 20.0),)),
        snapshot(2, due - 5, due - 20, "down", ((0.60, 20.0),)),
        clob_event("clob_error", 3, due + 2_000, source_ts_ms=None, error="closed"),
    ]
    cfg = F.ControlConfig(fill_latencies_ms=(500.0,), sigma_window_s=20, sigma_min_observations=10)
    rows, _ = F.replay_normalized(source, clob, [mapping()], {"m1": "Up"}, cfg)
    assert len(rows) == 1 and not rows[0]["filled"]
    assert rows[0]["reason"] == "book_stale"


def test_control_replay_uses_frozen_limit_and_fails_closed_on_thin_depth():
    source, signal_ms = quiet_then_jump_events()
    due = signal_ms + 500.0
    clob = [
        clob_event("clob_connection", 0, signal_ms - 1_000, source_ts_ms=None, token_count=2),
        snapshot(1, signal_ms - 900, signal_ms - 920, "up", ((0.40, 4.8),)),
        snapshot(2, signal_ms - 899, signal_ms - 919, "down", ((0.60, 20.0),), bids=((0.59, 20.0),)),
        snapshot(3, due - 5, due - 20, "up", ((0.40, 4.9), (0.41, 100.0))),
        clob_event("clob_error", 4, due + 2_000, source_ts_ms=None, error="closed"),
    ]
    cfg = F.ControlConfig(fill_latencies_ms=(500.0,), sigma_window_s=20, sigma_min_observations=10)
    rows, _ = F.replay_normalized(source, clob, [mapping()], {"m1": "Up"}, cfg)
    assert len(rows) == 1 and not rows[0]["filled"] and rows[0]["reason"] == "insufficient_depth"
    assert rows[0]["winner"] is None
    assert rows[0]["won"] is None
    assert rows[0]["pnl_per_share"] is None
    assert rows[0]["pnl"] is None
    assert F._fill_best(((0.01, 100.0),), 5.0, 0.98, 0.07)[3] == "below_price_band"


def test_control_replay_never_uses_a_book_received_after_the_match_clock():
    source, signal_ms = quiet_then_jump_events()
    due = signal_ms + 500.0
    clob = [
        clob_event("clob_connection", 0, signal_ms - 1_000, source_ts_ms=None, token_count=2),
        snapshot(1, due - 100, due - 120, "up", ((0.40, 20.0),)),
        snapshot(2, due - 99, due - 119, "down", ((0.60, 20.0),), bids=((0.59, 20.0),)),
        # Its exchange timestamp is before the match, but the recorder had not received it by then.
        snapshot(3, due + 20, due - 1, "up", ((0.20, 20.0),)),
        clob_event("clob_error", 4, due + 2_000, source_ts_ms=None, error="closed"),
    ]
    cfg = F.ControlConfig(fill_latencies_ms=(500.0,), sigma_window_s=20, sigma_min_observations=10)
    rows, _ = F.replay_normalized(source, clob, [mapping()], {"m1": "Up"}, cfg)
    assert len(rows) == 1 and rows[0]["filled"]
    assert rows[0]["fill_price"] == pytest.approx(0.40)


def test_deep_book_change_does_not_refresh_the_frozen_top_of_book_gate():
    source, signal_ms = quiet_then_jump_events()
    due = signal_ms + 500.0
    clob = [
        clob_event("clob_connection", 0, due - 2_100, source_ts_ms=None, token_count=2),
        snapshot(1, due - 2_000, due - 2_020, "up", ((0.40, 20.0), (0.50, 10.0))),
        snapshot(2, due - 1_999, due - 2_019, "down", ((0.60, 20.0),), bids=((0.59, 20.0),)),
        # Only a deep level changes. Historical MIX freshness follows top price/size, not any L2 update.
        snapshot(3, due - 100, due - 120, "up", ((0.40, 20.0), (0.51, 10.0))),
        clob_event("clob_error", 4, due + 2_000, source_ts_ms=None, error="closed"),
    ]
    cfg = F.ControlConfig(fill_latencies_ms=(500.0,), sigma_window_s=20, sigma_min_observations=10)
    rows, _ = F.replay_normalized(source, clob, [mapping()], {"m1": "Up"}, cfg)
    assert len(rows) == 1 and not rows[0]["filled"] and rows[0]["reason"] == "book_stale"


def test_clob_update_at_exact_match_time_is_seen_even_if_spot_arrives_first():
    source, signal_ms = quiet_then_jump_events()
    due = signal_ms + 500.0
    disconnect = source.pop()
    source.append(source_event("spot_trade", disconnect["seq"], due, source_ts_ms=due - 100,
                               stream="spot", stream_sequence=disconnect["stream_sequence"],
                               connection_epoch=1, price=60_060.0, size=1.0))
    disconnect["seq"] += 1
    disconnect["stream_sequence"] += 1
    source.append(disconnect)
    clob = [
        clob_event("clob_connection", 0, signal_ms - 1_000, source_ts_ms=None, token_count=2),
        snapshot(1, due - 100, due - 120, "up", ((0.40, 20.0),)),
        snapshot(2, due - 99, due - 119, "down", ((0.60, 20.0),), bids=((0.59, 20.0),)),
        snapshot(3, due, due - 1, "up", ((0.30, 20.0),), bids=((0.29, 20.0),)),
        clob_event("clob_error", 4, due + 2_000, source_ts_ms=None, error="closed"),
    ]
    cfg = F.ControlConfig(fill_latencies_ms=(500.0,), sigma_window_s=20, sigma_min_observations=10)
    rows, _ = F.replay_normalized(source, clob, [mapping()], {"m1": "Up"}, cfg)
    assert rows[0]["filled"] and rows[0]["fill_price"] == pytest.approx(0.30)


def test_frozen_scorer_rejects_missing_features_and_tampering(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "MODELS", ("ridge",))
    monkeypatch.setattr(M, "POLICIES", ["settle"])
    monkeypatch.setattr(M, "QS", (0.2,))
    rng = np.random.default_rng(4)
    rows = []
    for day in pd.date_range("2026-05-25", "2026-07-15", freq="D", tz="UTC"):
        n = 30
        start = int(day.timestamp()) + 3600
        values = {name: rng.normal(size=n) for name in M.FEATURES}
        pnl = 0.1 * values["jump_z"] + rng.normal(0, 0.02, n)
        base = {"day": day.strftime("%Y-%m-%d"), "market": f"m{start}", "start": start,
                "t": start + np.arange(n), "kind": "jump", "side": 1, "jdir": 1,
                "price": 0.4, "ask_size": 10.0, "won": 1, **values,
                "pnl_settle": pnl, "hold_settle": 10.0, "how_settle": 0, "sh20_settle": 10.0}
        rows.append(pd.DataFrame(base))
    A = M.compact(pd.concat(rows, ignore_index=True)[M.META + M.FEATURES + M.label_cols()])
    frozen = tmp_path / "cross-mix-frozen.json"
    spec, _, _ = M.freeze(A, frozen, log=lambda _: None)
    M.export_model_bundle(A, spec, frozen)
    scorer = F.load_scorer(frozen)
    feature_row = {name: float(A.iloc[0][name]) for name in M.FEATURES}
    scores = scorer.score(feature_row)
    assert set(scores) == {rule["id"] for rule in spec["rules"]}
    with pytest.raises(ValueError, match="missing MIX features"):
        scorer.score({"jump_z": 1.0})

    _, manifest_path = M.model_bundle_paths(frozen)
    original_manifest = json.loads(manifest_path.read_text())
    wrong_runtime = copy.deepcopy(original_manifest)
    wrong_runtime["versions"]["sklearn"] = "0.0"
    manifest_path.write_text(json.dumps(wrong_runtime))
    with pytest.raises(ValueError, match="runtime differs"):
        F.load_scorer(frozen)
    manifest_path.write_text(json.dumps(original_manifest))

    model_path, _ = M.model_bundle_paths(frozen)
    model_path.write_bytes(model_path.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="model bundle sha256"):
        F.load_scorer(frozen)


def test_scorer_rejects_feature_order_drift(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "MODELS", ("ridge",))
    monkeypatch.setattr(M, "POLICIES", ["settle"])
    monkeypatch.setattr(M, "QS", (0.2,))
    # This test exercises the manifest gate without fitting another full synthetic study.
    frozen = tmp_path / "cross-mix-frozen.json"
    frozen.write_text(json.dumps({"features": M.FEATURES, "rules": [], "a_fingerprint": "x"}))
    model_path, manifest_path = M.model_bundle_paths(frozen)
    model_path.write_bytes(b"not-loaded-before-hash-and-schema-pass")
    manifest = {"format": "polymarket-mix-models-v1", "model_file": model_path.name,
                "model_sha256": F.sha256_file(model_path), "frozen_sha256": F.sha256_file(frozen),
                "a_fingerprint": "x", "features": list(reversed(M.FEATURES)), "rules": []}
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="feature schema"):
        F.load_scorer(frozen)


def test_formal_gate_uses_first_500ms_fill_per_market_for_every_statistic(monkeypatch):
    monkeypatch.setattr(F, "MIN_FILLS", 3)
    monkeypatch.setattr(F, "MIN_DAYS", 2)
    monkeypatch.setattr(F, "FAMILY_TESTS", 1)
    rows = []
    for index, (market, day) in enumerate((("m1", "2026-10-05"), ("m1", "2026-10-05"),
                                           ("m2", "2026-10-06"), ("m3", "2026-10-07"))):
        rows.append({"market_id": market, "signal_receive_ms": float(index), "evaluation_ms": 500.0,
                     "filled": True, "winner": "Up", "won": True, "all_in_cost": 0.4,
                     "filled_shares": 5.0, "pnl_per_share": 0.6, "pnl": 3.0, "day": day})
    rows[1].update(winner="Down", won=False, all_in_cost=0.9, pnl_per_share=-0.9, pnl=-4.5)
    sample = F.stopping_sample(rows)
    assert [row["market_id"] for row in sample] == ["m1", "m2", "m3"]
    result = F.verdict(rows)
    assert result["fills"] == 3 and result["days"] == 3 and result["net_ev_per_share"] == pytest.approx(0.6)
    assert result["day_cluster_lower_99"] == pytest.approx(0.6)
    assert result["raw_exact_p"] == pytest.approx(0.4 ** 3)


def test_formal_gate_waits_for_outcomes_without_changing_its_stop_prefix(monkeypatch):
    monkeypatch.setattr(F, "MIN_FILLS", 2)
    monkeypatch.setattr(F, "MIN_DAYS", 2)
    monkeypatch.setattr(F, "FAMILY_TESTS", 1)
    rows = [
        {"market_id": "m1", "signal_receive_ms": 1.0, "evaluation_ms": 500.0,
         "filled": True, "winner": None, "won": None, "all_in_cost": 0.4,
         "filled_shares": 5.0, "pnl_per_share": None, "pnl": None, "day": "2026-10-05"},
        {"market_id": "m2", "signal_receive_ms": 2.0, "evaluation_ms": 500.0,
         "filled": True, "winner": "Up", "won": True, "all_in_cost": 0.4,
         "filled_shares": 5.0, "pnl_per_share": 0.6, "pnl": 3.0, "day": "2026-10-06"},
        {"market_id": "m3", "signal_receive_ms": 3.0, "evaluation_ms": 500.0,
         "filled": True, "winner": "Up", "won": True, "all_in_cost": 0.4,
         "filled_shares": 5.0, "pnl_per_share": 0.6, "pnl": 3.0, "day": "2026-10-07"},
    ]
    assert [row["market_id"] for row in F.stopping_sample(rows)] == ["m1", "m2"]
    collecting = F.verdict(rows)
    assert collecting["status"] == "collecting"
    assert collecting["fills"] == 2 and collecting["pending_outcomes"] == 1

    rows[0].update(winner="Up", won=True, pnl_per_share=0.6, pnl=3.0)
    terminal = F.verdict(rows)
    assert terminal["status"] in {"passed", "rejected"}
    assert terminal["fills"] == 2


def test_ledger_rejects_execution_conflicts(tmp_path):
    row = {"market_id": "m1", "kind": "jump", "signal_source_ms": 1.0,
           "signal_receive_ms": 2.0, "direction": "Up", "evaluation_ms": 500.0,
           "filled": True, "fill_price": 0.4, "winner": "Up", "won": True,
           "all_in_cost": 0.4, "filled_shares": 5.0,
           "pnl_per_share": 0.6, "pnl": 3.0, "day": "2026-10-05"}
    ledger = tmp_path / "ledger.jsonl"
    first = tmp_path / "first.jsonl"
    first.write_text(json.dumps(row) + "\n")
    F.append_observations(ledger, first)

    conflict = tmp_path / "conflict.jsonl"
    conflict.write_text(json.dumps({**row, "fill_price": 0.41}) + "\n")
    before = ledger.read_bytes()
    with pytest.raises(ValueError, match="execution-field conflict"):
        F.append_observations(ledger, conflict)
    assert ledger.read_bytes() == before


def test_ledger_enriches_an_unresolved_fill_with_its_outcome(tmp_path):
    unresolved = {"market_id": "m1", "kind": "jump", "signal_source_ms": 1.0,
                  "signal_receive_ms": 2.0, "direction": "Up", "evaluation_ms": 500.0,
                  "filled": True, "fill_price": 0.4, "winner": None, "won": None,
                  "all_in_cost": 0.4, "filled_shares": 5.0,
                  "pnl_per_share": None, "pnl": None, "day": "2026-10-05"}
    resolved = {**unresolved, "winner": "Up", "won": True, "pnl_per_share": 0.6, "pnl": 3.0}
    ledger = tmp_path / "ledger.jsonl"
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    first.write_text(json.dumps(unresolved) + "\n")
    second.write_text(json.dumps(resolved) + "\n")

    F.append_observations(ledger, first)
    pooled = F.append_observations(ledger, second)
    assert len(pooled) == 1
    assert pooled[0]["winner"] == "Up" and pooled[0]["won"] is True
    assert pooled[0]["pnl_per_share"] == pytest.approx(0.6)
    assert pooled[0]["pnl"] == pytest.approx(3.0)


def test_ledger_rejects_outcome_downgrades_and_partial_outcomes(tmp_path):
    resolved = {"market_id": "m1", "kind": "jump", "signal_source_ms": 1.0,
                "signal_receive_ms": 2.0, "direction": "Up", "evaluation_ms": 500.0,
                "filled": True, "fill_price": 0.4, "winner": "Up", "won": True,
                "all_in_cost": 0.4, "filled_shares": 5.0,
                "pnl_per_share": 0.6, "pnl": 3.0, "day": "2026-10-05"}
    ledger = tmp_path / "ledger.jsonl"
    first = tmp_path / "first.jsonl"
    first.write_text(json.dumps(resolved) + "\n")
    F.append_observations(ledger, first)
    before = ledger.read_bytes()

    downgrade = tmp_path / "downgrade.jsonl"
    downgrade.write_text(json.dumps({**resolved, "winner": None, "won": None,
                                     "pnl_per_share": None, "pnl": None}) + "\n")
    with pytest.raises(ValueError, match="outcome conflict"):
        F.append_observations(ledger, downgrade)

    partial = tmp_path / "partial.jsonl"
    partial.write_text(json.dumps({**resolved, "market_id": "m2", "signal_source_ms": 3.0,
                                   "winner": "Up", "won": None,
                                   "pnl_per_share": None, "pnl": None}) + "\n")
    with pytest.raises(ValueError, match="partial outcome"):
        F.append_observations(ledger, partial)
    assert ledger.read_bytes() == before


@pytest.mark.parametrize("mutation_flag", ("--append", "--verdict-out"))
def test_mutating_cli_requires_a_freeze(tmp_path, mutation_flag):
    command = [sys.executable, str(Path(F.__file__)), "--archive", str(tmp_path),
               mutation_flag, str(tmp_path / "mutation.json")]
    if mutation_flag == "--append":
        command += ["--rows-out", str(tmp_path / "rows.jsonl")]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert completed.returncode == 2
    assert "--freeze is required" in completed.stderr


def test_cli_keeps_non_eu_west_1_data_out_of_the_formal_ledger(tmp_path, monkeypatch, capsys):
    ledger = tmp_path / "ledger.jsonl"
    verdict_path = tmp_path / "verdict.json"
    monkeypatch.setattr(sys, "argv", [str(Path(F.__file__)), "--archive", str(tmp_path),
                                     "--freeze", str(tmp_path / "freeze.json"),
                                     "--rows-out", str(tmp_path / "rows.jsonl"),
                                     "--append", str(ledger), "--verdict-out", str(verdict_path)])
    monkeypatch.setattr(F, "load_control_freeze",
                        lambda _: ({"protocol_sha256": "anchor"}, F.ControlConfig()))
    monkeypatch.setattr(F, "replay_archive",
                        lambda *args, **kwargs: {"dataset": {"paper_gate_eligible": False}})
    F.main()
    output = json.loads(capsys.readouterr().out)
    assert output["pooled_verdict"]["status"] == "audit_only"
    assert not ledger.exists() and not verdict_path.exists()


def test_freeze_fingerprint_append_once_and_terminal_verdict_are_immutable(tmp_path, monkeypatch):
    historical = tmp_path / "historical-frozen.json"
    historical.write_text("{}")
    freeze = {"schema": F.FREEZE_SCHEMA, "strategy_id": F.STRATEGY_ID,
              "holdout_start_utc": "2026-10-05T00:00:00Z", "z_cut": F.CONTROL_CUT,
              "historical_frozen_file": historical.name,
              "historical_frozen_sha256": F.sha256_file(historical),
              "jump_bp": 2.0, "reference_min_s": 1.0, "reference_max_s": 5.0,
              "spacing_s": 5.0, "tau_hi_s": 285.0, "tau_lo_s": 20.0,
              "fixed_taus_s": [240, 180, 120, 60], "sigma_window_s": 600,
              "sigma_min_observations": 300, "forward_fill_s": 20,
              "fill_latencies_ms": [300.0, 400.0, 500.0], "primary_latency_ms": F.PRIMARY_LATENCY_MS,
              "shares": 5.0, "min_price": 0.02, "limit_price": 0.98,
              "fee_rate": 0.07, "book_fresh_ms": 1000.0,
              "min_fills": F.MIN_FILLS, "min_days": F.MIN_DAYS, "family_tests": F.FAMILY_TESTS,
              "alpha": F.ALPHA}
    freeze["runner_sha256"] = F.sha256_file(F.__file__)
    freeze["protocol_sha256"] = F._canonical_sha(freeze)
    freeze_path = tmp_path / "freeze.json"
    freeze_path.write_text(json.dumps(freeze))
    monkeypatch.delenv("EXPECTED_PROTOCOL_SHA256", raising=False)
    with pytest.raises(ValueError, match="EXPECTED_PROTOCOL_SHA256 is required"):
        F.load_control_freeze(freeze_path)
    monkeypatch.setenv("EXPECTED_PROTOCOL_SHA256", freeze["protocol_sha256"])
    loaded, config = F.load_control_freeze(freeze_path)
    assert loaded == freeze and config.holdout_start_ms == pytest.approx(1_791_158_400_000)
    monkeypatch.setenv("EXPECTED_PROTOCOL_SHA256", "0" * 64)
    with pytest.raises(ValueError, match="EXPECTED_PROTOCOL_SHA256"):
        F.load_control_freeze(freeze_path)
    monkeypatch.setenv("EXPECTED_PROTOCOL_SHA256", freeze["protocol_sha256"])
    wrong_runner = dict(freeze, runner_sha256="0" * 64)
    wrong_runner["protocol_sha256"] = F._canonical_sha(wrong_runner)
    freeze_path.write_text(json.dumps(wrong_runner))
    monkeypatch.setenv("EXPECTED_PROTOCOL_SHA256", wrong_runner["protocol_sha256"])
    with pytest.raises(ValueError, match="audited runner"):
        F.load_control_freeze(freeze_path)
    freeze_path.write_text(json.dumps(freeze))
    monkeypatch.setenv("EXPECTED_PROTOCOL_SHA256", freeze["protocol_sha256"])
    tampered = dict(freeze, z_cut=1.0)
    freeze_path.write_text(json.dumps(tampered))
    with pytest.raises(ValueError, match="fingerprint mismatch"):
        F.load_control_freeze(freeze_path)

    def row(market, signal, won=True):
        return {"market_id": market, "kind": "jump", "signal_source_ms": signal,
                "signal_receive_ms": signal + 10, "direction": "Up", "evaluation_ms": 500.0,
                "filled": True, "winner": "Up" if won else "Down", "won": won,
                "all_in_cost": 0.4, "filled_shares": 5.0,
                "pnl_per_share": 0.6 if won else -0.4, "pnl": 3.0 if won else -2.0,
                "day": "2026-10-05" if signal < 2 else "2026-10-06"}

    first = tmp_path / "first.jsonl"
    conflict = tmp_path / "conflict.jsonl"
    second = tmp_path / "second.jsonl"
    ledger = tmp_path / "ledger.jsonl"
    first.write_text(json.dumps(row("m1", 1)) + "\n")
    changed = row("m1", 1, won=False)
    conflict.write_text(json.dumps(changed) + "\n")
    second.write_text(json.dumps(row("m2", 2)) + "\n")
    pooled = F.append_observations(ledger, first)
    with pytest.raises(ValueError, match="outcome conflict"):
        F.append_observations(ledger, conflict)
    pooled = F.append_observations(ledger, second)
    assert len(pooled) == 2 and pooled[0]["won"] is True

    monkeypatch.setattr(F, "MIN_FILLS", 2)
    monkeypatch.setattr(F, "MIN_DAYS", 2)
    monkeypatch.setattr(F, "FAMILY_TESTS", 1)
    pinned_path = tmp_path / "verdict.json"
    pinned = F.pin_verdict(pooled, pinned_path, freeze, ledger)
    assert pinned_path.exists() and pinned["fills"] == 2
    sample = F.stopping_sample(pooled)
    sample_keys = [list(F.observation_key(item)) for item in sample]
    sample_bytes = "".join(json.dumps(item, sort_keys=True, allow_nan=False) + "\n" for item in sample).encode()
    assert pinned["sample_keys"] == sample_keys
    assert pinned["sample_sha256"] == hashlib.sha256(sample_bytes).hexdigest()
    assert pinned["ledger_sha256"] == F.sha256_file(ledger)
    assert pinned["last_key"] == sample_keys[-1]
    altered_sample = [{**pooled[0], "pnl": 2.5}, pooled[1]]
    with pytest.raises(ValueError, match="sample SHA"):
        F.pin_verdict(altered_sample, pinned_path, freeze)

    earlier_rows = tmp_path / "earlier.jsonl"
    earlier_rows.write_text(json.dumps(row("m0", 0)) + "\n")
    ledger_before = ledger.read_bytes()
    with pytest.raises(ValueError, match="stopping prefix"):
        F.append_observations(ledger, earlier_rows, pinned_path)
    assert ledger.read_bytes() == ledger_before

    later_rows = tmp_path / "later.jsonl"
    later_rows.write_text(json.dumps(row("m3", 3, won=False)) + "\n")
    later = F.append_observations(ledger, later_rows, pinned_path)
    assert len(later) == 3
    assert F.pin_verdict(later, pinned_path, freeze, ledger) == pinned
