"""Compare current-H baseline and experimental trigger races with independent full replays."""
from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

import h_replay as replay
import h_replay_run as run


REQUIRED_STRATEGY_ID = "CURRENT-H-TIMESTAMPED-FIRST-TWAP-V3"
BASELINE_SOURCES = frozenset({"spot_trade", "futures_book_ticker"})
COMBINED_SOURCES = frozenset({*BASELINE_SOURCES, "futures_trade", "deribit_quote"})
SCHEMA = "current-h-source-race-v1"

_SIGNATURE_FIELDS = (
    "signal_receive_ms", "signal_source_ms", "signal_source", "signal_ms", "direction",
    "fair", "decision_ask", "fixed_limit", "sent", "decision_reason", "send_ms",
    "evaluation_time_ms", "target_shares", "filled_shares", "fill_price", "all_in_cost",
    "filled", "reason", "winner", "won", "pnl_per_share", "pnl",
)
_ARTIFACT_FILES = (
    "manifest.json",
    "source_events.jsonl.gz",
    "clob_events.jsonl.gz",
    "market_registry.csv.gz",
    "market_outcomes.csv.gz",
)
_CHANGED_MARKET_SAMPLE_LIMIT = 20


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _artifact_stats(root: Path) -> dict[str, tuple[int, int, int]]:
    stats = {}
    for name in _ARTIFACT_FILES:
        path = root / name
        stat = path.stat()
        stats[name] = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
    return stats


def _capture_artifact_identity(
    archive_dir: Path,
) -> tuple[dict[str, Any], dict[str, tuple[int, int, int]], Path]:
    root = archive_dir / "strict" if (archive_dir / "strict" / "manifest.json").exists() else archive_dir
    missing = [name for name in _ARTIFACT_FILES if not (root / name).is_file()]
    if missing:
        raise ValueError(f"source-race replay requires a standard strict artifact; missing {missing}")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != "polymarket-5m-strict-replay-v2":
        raise ValueError("source-race replay requires polymarket-5m-strict-replay-v2")
    if manifest.get("receipt_race_ready") is not True:
        raise ValueError("source-race replay requires a receipt-race-ready artifact")
    file_hashes = {name: _file_sha256(root / name) for name in _ARTIFACT_FILES}
    identity = {
        "run_id": manifest.get("run_id"),
        "collector_region": manifest.get("collector_region"),
        "files": file_hashes,
    }
    return identity, _artifact_stats(root), root


def _latency_key(value: object) -> str:
    return f"{float(value):g}"


def _dataset_identity(dataset: dict[str, Any]) -> dict[str, Any]:
    return {
        "archive": dataset.get("archive"),
        "sample_scope": dataset.get("sample_scope"),
        "collector_region": dataset.get("collector_region"),
        "mapped_markets": dataset.get("mapped_markets"),
        "raw_clob_files": dataset.get("raw_clob_files"),
        "manifest": dataset.get("manifest"),
    }


def _protocol_identity(protocol: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in protocol.items() if key != "active_trigger_sources"}


def _read_row_audit(path: Path) -> tuple[dict[str, dict[str, Any]], dict[tuple[str, str], bytes]]:
    counts: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "rows": 0,
        "resolved_fills": 0,
        "wins": 0,
        "losses": 0,
        "filled_shares": 0.0,
        "pnl": 0.0,
        "worst_pnl_per_share": None,
    })
    sequences: dict[tuple[str, str], bytes] = {}
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid replay row") from exc
            if not isinstance(row, dict) or "market_id" not in row or "evaluation_ms" not in row:
                raise ValueError(f"{path}:{line_number}: replay row lacks market/evaluation identity")
            latency = _latency_key(row["evaluation_ms"])
            market_id = str(row["market_id"])
            signature = {key: row.get(key) for key in _SIGNATURE_FIELDS}
            prior = sequences.get((latency, market_id), b"")
            sequences[(latency, market_id)] = hashlib.sha256(
                prior + json.dumps(signature, sort_keys=True, separators=(",", ":"),
                                   allow_nan=False).encode()
            ).digest()
            audit = counts[latency]
            audit["rows"] += 1
            if not (row.get("filled") and row.get("winner") is not None):
                continue
            audit["resolved_fills"] += 1
            audit["wins"] += int(bool(row.get("won")))
            audit["losses"] += int(not bool(row.get("won")))
            audit["filled_shares"] += float(row.get("filled_shares") or 0.0)
            audit["pnl"] += float(row.get("pnl") or 0.0)
            pnl_per_share = float(row["pnl_per_share"])
            previous_worst = audit["worst_pnl_per_share"]
            audit["worst_pnl_per_share"] = (
                pnl_per_share if previous_worst is None else min(float(previous_worst), pnl_per_share)
            )
    for audit in counts.values():
        resolved = int(audit["resolved_fills"])
        audit["loss_rate"] = int(audit["losses"]) / resolved if resolved else 0.0
    return dict(counts), sequences


def _validate_arm(name: str, result: dict[str, Any], expected_sources: frozenset[str]) -> None:
    protocol = result.get("protocol", {})
    strategy_id = protocol.get("strategy_id")
    if strategy_id != REQUIRED_STRATEGY_ID:
        raise ValueError(f"source-race replay requires {REQUIRED_STRATEGY_ID}, got {strategy_id!r}")
    active = frozenset(protocol.get("active_trigger_sources", ()))
    if active != expected_sources:
        raise ValueError(f"{name} active source drift: {sorted(active)} != {sorted(expected_sources)}")
    manifest = result.get("dataset", {}).get("manifest") or {}
    if manifest.get("receipt_race_ready") is not True:
        raise ValueError(f"{name} dataset is not receipt-race-ready")


def _causal_changes(
    baseline: dict[tuple[str, str], bytes],
    combined: dict[tuple[str, str], bytes],
) -> dict[str, dict[str, int]]:
    latencies = sorted({key[0] for key in baseline} | {key[0] for key in combined}, key=float)
    output: dict[str, dict[str, int]] = {}
    for latency in latencies:
        base = {market: digest for (delay, market), digest in baseline.items() if delay == latency}
        combo = {market: digest for (delay, market), digest in combined.items() if delay == latency}
        common = set(base) & set(combo)
        changed_common = {market for market in common if base[market] != combo[market]}
        baseline_only = set(base) - set(combo)
        combined_only = set(combo) - set(base)
        changed = sorted(changed_common | baseline_only | combined_only)
        output[latency] = {
            "baseline_markets": len(base),
            "combined_markets": len(combo),
            "identical_markets": len(common - changed_common),
            "changed_common_markets": len(changed_common),
            "baseline_only_markets": len(baseline_only),
            "combined_only_markets": len(combined_only),
            "changed_markets": len(changed),
            "changed_market_ids_sha256": _canonical_digest(changed),
            "changed_market_id_samples": changed[:_CHANGED_MARKET_SAMPLE_LIMIT],
        }
    return output


def _number_delta(combined: object, baseline: object) -> float | int | None:
    if not isinstance(combined, (int, float)) or not isinstance(baseline, (int, float)):
        return None
    value = combined - baseline
    return int(value) if isinstance(combined, int) and isinstance(baseline, int) else float(value)


def _latency_deltas(
    baseline: dict[str, Any],
    combined: dict[str, Any],
    baseline_audit: dict[str, dict[str, Any]],
    combined_audit: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    base_rows = baseline.get("latencies", {})
    combo_rows = combined.get("latencies", {})
    latencies = sorted(set(base_rows) | set(combo_rows) | set(baseline_audit) | set(combined_audit), key=float)
    output: dict[str, dict[str, Any]] = {}
    for latency in latencies:
        base = base_rows.get(latency, {})
        combo = combo_rows.get(latency, {})
        base_loss = baseline_audit.get(latency, {})
        combo_loss = combined_audit.get(latency, {})
        output[latency] = {
            metric: _number_delta(combo.get(metric), base.get(metric))
            for metric in (
                "signals", "sent", "fills", "filled_shares", "fill_rate_per_signal",
                "fill_rate_per_send", "net_ev_per_share", "pnl", "days",
            )
        }
        output[latency].update({
            "wins": _number_delta(combo_loss.get("wins", 0), base_loss.get("wins", 0)),
            "losses": _number_delta(combo_loss.get("losses", 0), base_loss.get("losses", 0)),
            "loss_rate": _number_delta(combo_loss.get("loss_rate", 0.0),
                                       base_loss.get("loss_rate", 0.0)),
        })
    return output


def _activation_gate(
    baseline: dict[str, Any],
    combined: dict[str, Any],
    baseline_audit: dict[str, dict[str, Any]],
    combined_audit: dict[str, dict[str, Any]],
    frozen: dict[str, Any],
) -> dict[str, Any]:
    primary = _latency_key(frozen["timing"]["primary_evaluation_ms"])
    required_fills = int(frozen["statistics"]["paper_gate_min_fills"])
    required_days = int(frozen["statistics"]["paper_gate_min_utc_days"])
    base = baseline.get("latencies", {}).get(primary, {})
    combo = combined.get("latencies", {}).get(primary, {})
    base_loss = baseline_audit.get(primary, {})
    combo_loss = combined_audit.get(primary, {})
    base_fills = int(base.get("fills", 0))
    combo_fills = int(combo.get("fills", 0))
    base_ev = base.get("net_ev_per_share")
    combo_ev = combo.get("net_ev_per_share")
    loss_control_comparable = base_fills > 0 and combo_fills > 0
    checks = {
        "both_arms_paper_gate_eligible": bool(
            baseline.get("dataset", {}).get("paper_gate_eligible")
            and combined.get("dataset", {}).get("paper_gate_eligible")
        ),
        "combined_min_fills": combo_fills >= required_fills,
        "combined_min_days": int(combo.get("days", 0)) >= required_days,
        "combined_positive_net_ev": isinstance(combo_ev, (int, float)) and combo_ev > 0,
        "incremental_fill_or_ev_improvement": bool(
            combo_fills > base_fills
            or (isinstance(combo_ev, (int, float)) and isinstance(base_ev, (int, float)) and combo_ev > base_ev)
        ),
        "loss_control_comparable": loss_control_comparable,
        "loss_rate_not_worse": bool(
            loss_control_comparable
            and float(combo_loss.get("loss_rate", 1.0)) <= float(base_loss.get("loss_rate", 1.0))
        ),
    }
    return {
        "passes": False,
        "primary_evaluation_ms": float(primary),
        "checks": checks,
        "reason": "shadow_sources_require_locked_out_of_sample_review_before_activation",
    }


def compare_archive(
    archive_dir: str | Path,
    freeze_path: str | Path = replay.FREEZE_PATH,
) -> dict[str, Any]:
    archive_dir = Path(archive_dir)
    freeze_path = Path(freeze_path)
    freeze_bytes = freeze_path.read_bytes()
    frozen = json.loads(freeze_bytes)
    artifact_identity, artifact_stats, artifact_root = _capture_artifact_identity(archive_dir)
    with tempfile.TemporaryDirectory(prefix="h-source-race-") as temporary:
        temporary_dir = Path(temporary)
        frozen_snapshot = temporary_dir / "freeze.json"
        frozen_snapshot.write_bytes(freeze_bytes)
        baseline_rows = temporary_dir / "baseline.jsonl"
        combined_rows = temporary_dir / "combined.jsonl"
        baseline = run.replay_archive(
            archive_dir, frozen_snapshot, baseline_rows, trigger_sources=BASELINE_SOURCES,
        )
        combined = run.replay_archive(
            archive_dir, frozen_snapshot, combined_rows, trigger_sources=COMBINED_SOURCES,
        )
        if _artifact_stats(artifact_root) != artifact_stats:
            raise ValueError("strict artifact changed during source-race replay")
        _validate_arm("baseline", baseline, BASELINE_SOURCES)
        _validate_arm("combined", combined, COMBINED_SOURCES)
        if _protocol_identity(baseline["protocol"]) != _protocol_identity(combined["protocol"]):
            raise ValueError("protocol identity drift between source-race arms")
        baseline_dataset = _dataset_identity(baseline["dataset"])
        combined_dataset = _dataset_identity(combined["dataset"])
        if baseline_dataset != combined_dataset:
            raise ValueError("dataset identity drift between source-race arms")
        baseline_audit, baseline_sequences = _read_row_audit(baseline_rows)
        combined_audit, combined_sequences = _read_row_audit(combined_rows)
    baseline["loss_controls"] = baseline_audit
    combined["loss_controls"] = combined_audit
    return {
        "schema": SCHEMA,
        "comparison_method": "independent_full_state_replays_no_signal_pairing",
        "strategy_id": REQUIRED_STRATEGY_ID,
        "freeze_sha256": hashlib.sha256(freeze_bytes).hexdigest(),
        "artifact_files_sha256": _canonical_digest(artifact_identity),
        "dataset_identity_sha256": _canonical_digest(baseline_dataset),
        "arms": {"baseline": baseline, "combined": combined},
        "latency_deltas": _latency_deltas(baseline, combined, baseline_audit, combined_audit),
        "causal_sequence_changes": _causal_changes(baseline_sequences, combined_sequences),
        "activation_gate": _activation_gate(
            baseline, combined, baseline_audit, combined_audit, frozen,
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True)
    parser.add_argument("--freeze", default=str(replay.FREEZE_PATH))
    parser.add_argument("--out")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    result = compare_archive(args.archive, args.freeze)
    encoded = json.dumps(result, indent=2 if args.pretty else None, sort_keys=True, allow_nan=False) + "\n"
    if args.out:
        destination = Path(args.out)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
