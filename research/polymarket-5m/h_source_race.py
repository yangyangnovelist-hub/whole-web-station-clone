"""Compare current-H baseline and experimental trigger races with independent full replays."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

import h_replay as replay
import h_replay_run as run

try:
    import orjson
except ImportError:  # pragma: no cover - compatibility fallback outside the research environment
    orjson = None


REQUIRED_STRATEGY_ID = "CURRENT-H-TIMESTAMPED-FIRST-TWAP-V3"
BASELINE_SOURCES = frozenset({"spot_trade", "futures_book_ticker"})
CANDIDATE_SOURCES = frozenset({*BASELINE_SOURCES, "deribit_quote"})
SCHEMA = "current-h-source-race-v2"
SOURCE_RACE_FREEZE_PATH = Path(__file__).with_name("forward") / "source-race-freeze.json"
SOURCE_RACE_FREEZE_SHA256 = "c37941027d6002cd6a2a9344881d863442f66669d29151b26fd2365bc3b42809"

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


def _load_source_race_freeze(path: str | Path = SOURCE_RACE_FREEZE_PATH) -> tuple[dict[str, Any], str]:
    path = Path(path)
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != SOURCE_RACE_FREEZE_SHA256:
        raise ValueError("source-race freeze fingerprint drift")
    frozen = json.loads(raw)
    if frozen.get("schema") != "current-h-source-race-freeze-v1":
        raise ValueError("unknown source-race freeze schema")
    if frozen.get("strategy_id") != REQUIRED_STRATEGY_ID:
        raise ValueError("source-race freeze strategy drift")
    arms = frozen.get("arms") or {}
    if frozenset(arms.get("baseline") or ()) != BASELINE_SOURCES:
        raise ValueError("source-race baseline drift")
    if frozenset(arms.get("candidate") or ()) != CANDIDATE_SOURCES:
        raise ValueError("source-race candidate drift")
    root = Path(__file__).resolve().parent
    for relative, expected in (frozen.get("dependencies_sha256") or {}).items():
        dependency = root / relative
        if not dependency.is_file() or _file_sha256(dependency) != expected:
            raise ValueError(f"source-race dependency drift: {relative}")
    return frozen, digest


def _formal_eligibility(identity: dict[str, Any], frozen: dict[str, Any]) -> bool:
    cutoff = float(frozen["cutoff_ms"])
    maximum_leading = float(frozen["execution"]["maximum_leading_carryover_ms"])
    started = identity.get("started_ms")
    ended = identity.get("ended_ms")
    day_start = (float(started) // 86_400_000.0) * 86_400_000.0 if isinstance(started, (int, float)) else None
    return bool(
        identity.get("collector_region") == frozen.get("collector_region")
        and identity.get("receipt_race_ready") is True
        and day_start is not None
        and day_start >= cutoff
        and day_start <= float(started) <= day_start + maximum_leading
        and isinstance(ended, (int, float))
        and float(ended) >= day_start + 86_400_000.0 - maximum_leading
    )


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
        "receipt_race_ready": manifest.get("receipt_race_ready"),
        "started_ms": manifest.get("started_ms"),
        "ended_ms": manifest.get("ended_ms"),
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
    loads = orjson.loads if orjson is not None else json.loads
    mode = "rb" if orjson is not None else "r"
    options = {} if orjson is not None else {"encoding": "utf-8"}
    with path.open(mode, **options) as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                row = loads(line)
            except (json.JSONDecodeError, ValueError) as exc:
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
    candidate: dict[tuple[str, str], bytes],
) -> dict[str, dict[str, int]]:
    latencies = sorted({key[0] for key in baseline} | {key[0] for key in candidate}, key=float)
    output: dict[str, dict[str, int]] = {}
    for latency in latencies:
        base = {market: digest for (delay, market), digest in baseline.items() if delay == latency}
        combo = {market: digest for (delay, market), digest in candidate.items() if delay == latency}
        common = set(base) & set(combo)
        changed_common = {market for market in common if base[market] != combo[market]}
        baseline_only = set(base) - set(combo)
        candidate_only = set(combo) - set(base)
        changed = sorted(changed_common | baseline_only | candidate_only)
        output[latency] = {
            "baseline_markets": len(base),
            "candidate_markets": len(combo),
            "identical_markets": len(common - changed_common),
            "changed_common_markets": len(changed_common),
            "baseline_only_markets": len(baseline_only),
            "candidate_only_markets": len(candidate_only),
            "changed_markets": len(changed),
            "changed_market_ids_sha256": _canonical_digest(changed),
            "changed_market_id_samples": changed[:_CHANGED_MARKET_SAMPLE_LIMIT],
        }
    return output


def _number_delta(candidate: object, baseline: object) -> float | int | None:
    if not isinstance(candidate, (int, float)) or not isinstance(baseline, (int, float)):
        return None
    value = candidate - baseline
    return int(value) if isinstance(candidate, int) and isinstance(baseline, int) else float(value)


def _latency_deltas(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    baseline_audit: dict[str, dict[str, Any]],
    candidate_audit: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    base_rows = baseline.get("latencies", {})
    combo_rows = candidate.get("latencies", {})
    latencies = sorted(set(base_rows) | set(combo_rows) | set(baseline_audit) | set(candidate_audit), key=float)
    output: dict[str, dict[str, Any]] = {}
    for latency in latencies:
        base = base_rows.get(latency, {})
        combo = combo_rows.get(latency, {})
        base_loss = baseline_audit.get(latency, {})
        combo_loss = candidate_audit.get(latency, {})
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
    candidate: dict[str, Any],
    baseline_audit: dict[str, dict[str, Any]],
    candidate_audit: dict[str, dict[str, Any]],
    frozen: dict[str, Any],
) -> dict[str, Any]:
    primary = _latency_key(frozen["timing"]["primary_evaluation_ms"])
    required_fills = int(frozen["statistics"]["paper_gate_min_fills"])
    required_days = int(frozen["statistics"]["paper_gate_min_utc_days"])
    base = baseline.get("latencies", {}).get(primary, {})
    combo = candidate.get("latencies", {}).get(primary, {})
    base_loss = baseline_audit.get(primary, {})
    combo_loss = candidate_audit.get(primary, {})
    base_fills = int(base.get("fills", 0))
    combo_fills = int(combo.get("fills", 0))
    base_ev = base.get("net_ev_per_share")
    combo_ev = combo.get("net_ev_per_share")
    loss_control_comparable = base_fills > 0 and combo_fills > 0
    checks = {
        "both_arms_paper_gate_eligible": bool(
            baseline.get("dataset", {}).get("paper_gate_eligible")
            and candidate.get("dataset", {}).get("paper_gate_eligible")
        ),
        "candidate_min_fills": combo_fills >= required_fills,
        "candidate_min_days": int(combo.get("days", 0)) >= required_days,
        "candidate_positive_net_ev": isinstance(combo_ev, (int, float)) and combo_ev > 0,
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
    rows_dir: str | Path | None = None,
    source_race_freeze_path: str | Path = SOURCE_RACE_FREEZE_PATH,
    require_formal: bool = False,
) -> dict[str, Any]:
    archive_dir = Path(archive_dir)
    freeze_path = Path(freeze_path)
    freeze_bytes = freeze_path.read_bytes()
    frozen = json.loads(freeze_bytes)
    source_race_frozen, source_race_freeze_sha256 = _load_source_race_freeze(source_race_freeze_path)
    if float(frozen["timing"]["primary_evaluation_ms"]) != float(
        source_race_frozen["execution"]["primary_evaluation_ms"]
    ):
        raise ValueError("source-race primary latency drift")
    if int(frozen["statistics"]["family_tests"]) != int(
        source_race_frozen["statistics"]["family_tests"]
    ):
        raise ValueError("source-race multiplicity drift")
    artifact_identity, artifact_stats, artifact_root = _capture_artifact_identity(archive_dir)
    formal_eligible = _formal_eligibility(artifact_identity, source_race_frozen)
    if require_formal and not formal_eligible:
        raise ValueError("artifact is outside the frozen eu-west formal source-race sample")
    with tempfile.TemporaryDirectory(prefix="h-source-race-") as temporary:
        temporary_dir = Path(temporary)
        frozen_snapshot = temporary_dir / "freeze.json"
        frozen_snapshot.write_bytes(freeze_bytes)
        baseline_rows = temporary_dir / "baseline.jsonl"
        candidate_rows = temporary_dir / "candidate.jsonl"
        baseline = run.replay_archive(
            archive_dir, frozen_snapshot, baseline_rows, trigger_sources=BASELINE_SOURCES,
        )
        candidate = run.replay_archive(
            archive_dir, frozen_snapshot, candidate_rows, trigger_sources=CANDIDATE_SOURCES,
        )
        if _artifact_stats(artifact_root) != artifact_stats:
            raise ValueError("strict artifact changed during source-race replay")
        _validate_arm("baseline", baseline, BASELINE_SOURCES)
        _validate_arm("candidate", candidate, CANDIDATE_SOURCES)
        if _protocol_identity(baseline["protocol"]) != _protocol_identity(candidate["protocol"]):
            raise ValueError("protocol identity drift between source-race arms")
        baseline_dataset = _dataset_identity(baseline["dataset"])
        candidate_dataset = _dataset_identity(candidate["dataset"])
        if baseline_dataset != candidate_dataset:
            raise ValueError("dataset identity drift between source-race arms")
        baseline_audit, baseline_sequences = _read_row_audit(baseline_rows)
        candidate_audit, candidate_sequences = _read_row_audit(candidate_rows)
        observation_files = None
        if rows_dir is not None:
            destination = Path(rows_dir)
            destination.mkdir(parents=True, exist_ok=True)
            observation_files = {}
            for name, source in (("baseline", baseline_rows), ("candidate", candidate_rows)):
                target = destination / f"{name}.jsonl"
                temporary_target = target.with_suffix(".jsonl.tmp")
                shutil.copyfile(source, temporary_target)
                temporary_target.replace(target)
                observation_files[name] = {
                    "path": target.name,
                    "sha256": _file_sha256(target),
                    "bytes": target.stat().st_size,
                }
    baseline["loss_controls"] = baseline_audit
    candidate["loss_controls"] = candidate_audit
    result = {
        "schema": SCHEMA,
        "comparison_method": "independent_full_state_replays_no_signal_pairing",
        "strategy_id": REQUIRED_STRATEGY_ID,
        "freeze_sha256": hashlib.sha256(freeze_bytes).hexdigest(),
        "source_race_freeze_sha256": source_race_freeze_sha256,
        "formal_sample_eligible": formal_eligible,
        "artifact_identity": artifact_identity,
        "artifact_files_sha256": _canonical_digest(artifact_identity),
        "dataset_identity_sha256": _canonical_digest(baseline_dataset),
        "arms": {"baseline": baseline, "candidate": candidate},
        "latency_deltas": _latency_deltas(baseline, candidate, baseline_audit, candidate_audit),
        "causal_sequence_changes": _causal_changes(baseline_sequences, candidate_sequences),
        "activation_gate": _activation_gate(
            baseline, candidate, baseline_audit, candidate_audit, frozen,
        ),
    }
    if observation_files is not None:
        result["observation_files"] = observation_files
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True)
    parser.add_argument("--freeze", default=str(replay.FREEZE_PATH))
    parser.add_argument("--out")
    parser.add_argument("--rows-dir", help="persist validated baseline/candidate observation JSONL")
    parser.add_argument("--source-race-freeze", default=str(SOURCE_RACE_FREEZE_PATH))
    parser.add_argument("--require-formal", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    result = compare_archive(
        args.archive, args.freeze, args.rows_dir, args.source_race_freeze, args.require_formal,
    )
    encoded = json.dumps(result, indent=2 if args.pretty else None, sort_keys=True, allow_nan=False) + "\n"
    if args.out:
        destination = Path(args.out)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
