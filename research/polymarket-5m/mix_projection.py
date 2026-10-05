"""Executable 17-feature projection of the frozen historical MIX R1 rule.

This is a new forward candidate, not the original 21-feature R1.  The four
walk-forward factor predictions unavailable in the strict artifact are fixed
at their fitted Ridge raw-feature training means by passing NaN.  Ridge maps
those values to pre-centering z=0; its fitted ``zm`` centering remains intact.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np

import mix_forward as forward
import mix_hf as mix


STRATEGY_ID = "mix-r1-17f-mean-projection-v1"
RULE_ID = "R1"
RULE_CUT = 0.052917894565120874
PRIMARY_LATENCY_MS = 500.0
MIN_FILLS = 1_000
MIN_DAYS = 7
FAMILY_TESTS = 200
ALPHA = 0.01
PROTOCOL_SCHEMA = "polymarket-mix-r1-17f-projection-v1"
PROJECTION_MISSING_POLICY = "frozen_ridge_raw_training_mean"
FAMILY_TESTS_RATIONALE = "100 prior searched strategies plus one fixed projection; rounded up to 200"
STATISTICAL_LIMITATION = "Poisson-binomial assumes independent markets; passage also requires a 99% UTC-day cluster lower bound"
SOURCE_PROTOCOL_MISMATCH_POLICY = "fail_closed_for_rows_at_or_after_projection_holdout"
CAUSAL_FEATURES = (forward.DIRECT_MODEL_FEATURES + forward.TREND_MODEL_FEATURES +
                   forward.AUX_MODEL_FEATURES)


class IncompleteCausalFeatures(ValueError):
    pass


def _sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha(payload: Mapping[str, Any]) -> str:
    body = {key: value for key, value in payload.items() if key != "protocol_sha256"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _relative(path: Path, parent: Path) -> str:
    return os.path.relpath(path.resolve(), parent.resolve())


def _dependency_hashes() -> dict[str, str]:
    """Bind every local module that can change replay or model semantics."""
    modules = {
        "mix_forward.py": forward,
        "mix_hf.py": mix,
        "binary.py": forward.binary_model,
        "h_replay_archive.py": forward.archive,
        "h_replay_run.py": forward.hrun,
        "jump2s.py": forward.j2,
        "pm_outcomes.py": forward.pm_outcomes,
    }
    return {name: _sha256(module.__file__) for name, module in modules.items()}


def _atomic_write(path: str | Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def create_protocol(destination: str | Path, historical_frozen: str | Path,
                    source_control_freeze: str | Path, holdout_start_utc: str) -> dict[str, Any]:
    destination = Path(destination)
    if destination.exists():
        raise ValueError(f"R1 projection protocol already exists: {destination}")
    historical_frozen = Path(historical_frozen)
    source_control_freeze = Path(source_control_freeze)
    model_path, manifest_path = mix.model_bundle_paths(historical_frozen)
    anchor_path = mix.model_anchor_path(historical_frozen)
    required = (historical_frozen, source_control_freeze, model_path, manifest_path, anchor_path)
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise ValueError("R1 projection protocol lacks model artifacts: " + ", ".join(missing))
    control = json.loads(source_control_freeze.read_text(encoding="utf-8"))
    control_protocol = str(control.get("protocol_sha256", ""))
    if (control.get("schema") != forward.FREEZE_SCHEMA
            or control.get("strategy_id") != forward.STRATEGY_ID
            or control_protocol != _canonical_sha(control)
            or control.get("runner_sha256") != _sha256(forward.__file__)):
        raise ValueError("R1 projection source control protocol is invalid")
    start = datetime.fromisoformat(holdout_start_utc.replace("Z", "+00:00"))
    if start.tzinfo is None:
        raise ValueError("R1 projection holdout start must include a timezone")
    current = datetime.now(timezone.utc)
    if start <= current:
        raise ValueError("R1 projection protocol must be frozen before its holdout")
    control_start = datetime.fromisoformat(str(control.get("holdout_start_utc", "")).replace("Z", "+00:00"))
    control_made = datetime.fromisoformat(str(control.get("made", "")).replace("Z", "+00:00"))
    if control_start.tzinfo is None or control_made.tzinfo is None:
        raise ValueError("R1 projection source control timestamps are invalid")
    if start < control_start or start < control_made:
        raise ValueError("R1 projection holdout predates its source control")
    ProjectedR1Scorer(forward.load_scorer(historical_frozen))
    parent = destination.parent
    dependency_sha256 = _dependency_hashes()
    payload: dict[str, Any] = {
        "schema": PROTOCOL_SCHEMA,
        "strategy_id": STRATEGY_ID,
        "made": current.astimezone(timezone.utc).isoformat(),
        "holdout_start_utc": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "runner_sha256": _sha256(__file__),
        "dependency_sha256": dependency_sha256,
        "historical_frozen_file": _relative(historical_frozen, parent),
        "historical_frozen_sha256": _sha256(historical_frozen),
        "source_control_freeze_file": _relative(source_control_freeze, parent),
        "source_control_freeze_sha256": _sha256(source_control_freeze),
        "source_control_protocol_sha256": control_protocol,
        "model_file": _relative(model_path, parent),
        "model_sha256": _sha256(model_path),
        "model_manifest_file": _relative(manifest_path, parent),
        "model_manifest_sha256": _sha256(manifest_path),
        "model_anchor_file": _relative(anchor_path, parent),
        "model_anchor_sha256": _sha256(anchor_path),
        "source_strategy_id": forward.STRATEGY_ID,
        "source_protocol_mismatch_policy": SOURCE_PROTOCOL_MISMATCH_POLICY,
        "source_jump_z_cut": forward.CONTROL_CUT,
        "rule": {"id": RULE_ID, "policy": "settle", "model": "ridge", "q": 0.02,
                 "cut": RULE_CUT},
        "causal_features": list(CAUSAL_FEATURES),
        "projected_features": list(forward.ABSENT_MODEL_FEATURES),
        "projection_missing_policy": PROJECTION_MISSING_POLICY,
        "primary_latency_ms": PRIMARY_LATENCY_MS,
        "min_fills": MIN_FILLS,
        "min_days": MIN_DAYS,
        "family_tests": FAMILY_TESTS,
        "family_tests_rationale": FAMILY_TESTS_RATIONALE,
        "alpha": ALPHA,
        "statistical_limitation": STATISTICAL_LIMITATION,
    }
    payload["protocol_sha256"] = _canonical_sha(payload)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as error:
        raise ValueError(f"R1 projection protocol already exists: {destination}") from error
    return payload


def load_protocol(path: str | Path) -> tuple[dict[str, Any], float]:
    path = Path(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != PROTOCOL_SCHEMA or payload.get("strategy_id") != STRATEGY_ID:
        raise ValueError("unknown R1 projection protocol")
    if payload.get("protocol_sha256") != _canonical_sha(payload):
        raise ValueError("R1 projection protocol fingerprint mismatch")
    expected = os.environ.get("EXPECTED_PROJECTION_PROTOCOL_SHA256")
    if not expected:
        raise ValueError("EXPECTED_PROJECTION_PROTOCOL_SHA256 is required")
    if payload["protocol_sha256"] != expected:
        raise ValueError("R1 projection protocol differs from the expected fingerprint")
    if payload.get("runner_sha256") != _sha256(__file__):
        raise ValueError("R1 projection protocol does not match the audited runner")
    expected_dependencies = _dependency_hashes()
    if payload.get("dependency_sha256") != expected_dependencies:
        raise ValueError("R1 projection protocol dependency hash mismatch")
    constants = {
        "source_strategy_id": forward.STRATEGY_ID,
        "source_protocol_mismatch_policy": SOURCE_PROTOCOL_MISMATCH_POLICY,
        "source_jump_z_cut": forward.CONTROL_CUT,
        "rule": {"id": RULE_ID, "policy": "settle", "model": "ridge", "q": 0.02,
                 "cut": RULE_CUT},
        "causal_features": list(CAUSAL_FEATURES),
        "projected_features": list(forward.ABSENT_MODEL_FEATURES),
        "projection_missing_policy": PROJECTION_MISSING_POLICY,
        "primary_latency_ms": PRIMARY_LATENCY_MS,
        "min_fills": MIN_FILLS,
        "min_days": MIN_DAYS,
        "family_tests": FAMILY_TESTS,
        "family_tests_rationale": FAMILY_TESTS_RATIONALE,
        "alpha": ALPHA,
        "statistical_limitation": STATISTICAL_LIMITATION,
    }
    changed = [key for key, value in constants.items() if payload.get(key) != value]
    if changed:
        raise ValueError("R1 projection protocol differs from code: " + ", ".join(changed))
    bindings = (
        ("historical_frozen_file", "historical_frozen_sha256"),
        ("source_control_freeze_file", "source_control_freeze_sha256"),
        ("model_file", "model_sha256"),
        ("model_manifest_file", "model_manifest_sha256"),
        ("model_anchor_file", "model_anchor_sha256"),
    )
    for file_key, sha_key in bindings:
        artifact = (path.parent / str(payload[file_key])).resolve()
        if not artifact.is_file() or payload.get(sha_key) != _sha256(artifact):
            raise ValueError(f"R1 projection model artifact mismatch: {file_key}")
    control_path = (path.parent / str(payload["source_control_freeze_file"])).resolve()
    control = json.loads(control_path.read_text(encoding="utf-8"))
    if control.get("protocol_sha256") != payload.get("source_control_protocol_sha256"):
        raise ValueError("R1 projection source control protocol mismatch")
    start = datetime.fromisoformat(str(payload["holdout_start_utc"]).replace("Z", "+00:00"))
    if start.tzinfo is None:
        raise ValueError("R1 projection holdout start must include a timezone")
    return payload, start.timestamp() * 1_000.0


@dataclass(frozen=True)
class ProjectedR1Scorer:
    frozen: forward.FrozenMixScorer

    def __post_init__(self) -> None:
        if self.frozen.features != tuple(mix.FEATURES):
            raise ValueError("R1 projection feature order differs from the frozen study")
        rules = [rule for rule in self.frozen.rules if str(rule.get("id")) == RULE_ID]
        expected = {"id": RULE_ID, "policy": "settle", "model": "ridge", "q": 0.02,
                    "cut": RULE_CUT}
        if len(rules) != 1 or any(rules[0].get(key) != value for key, value in expected.items()):
            raise ValueError("R1 projection rule differs from the frozen study")
        if not isinstance(self.frozen.models.get(RULE_ID), mix.Ridge):
            raise ValueError("R1 projection requires the frozen mean-projecting Ridge model")

    def values(self, row: Mapping[str, Any]) -> dict[str, float]:
        groups = (row.get("model_features_direct"), row.get("model_features_trend"),
                  row.get("model_features_aux"))
        if any(not isinstance(group, Mapping) for group in groups):
            raise IncompleteCausalFeatures("R1 projection row lacks causal feature groups")
        supplied: dict[str, Any] = {}
        for group in groups:
            for name, value in group.items():
                if name in supplied and supplied[name] != value:
                    raise ValueError(f"conflicting R1 projection feature: {name}")
                supplied[str(name)] = value
        leaked = [name for name in forward.ABSENT_MODEL_FEATURES if name in supplied]
        if leaked:
            raise ValueError("R1 projection received forbidden factor predictions: " + ", ".join(leaked))
        missing = [name for name in CAUSAL_FEATURES if name not in supplied]
        if missing:
            raise IncompleteCausalFeatures("R1 projection lacks causal features: " + ", ".join(missing))
        values: dict[str, float] = {}
        for name in CAUSAL_FEATURES:
            try:
                value = float(supplied[name])
            except (TypeError, ValueError) as error:
                raise IncompleteCausalFeatures(f"R1 projection feature is unavailable: {name}") from error
            if not math.isfinite(value):
                raise IncompleteCausalFeatures(f"R1 projection feature is not finite: {name}")
            values[name] = value
        values.update({name: math.nan for name in forward.ABSENT_MODEL_FEATURES})
        if set(values) != set(mix.FEATURES):
            raise ValueError("R1 projection feature contract is incomplete")
        return values

    def score(self, row: Mapping[str, Any]) -> float:
        values = self.values(row)
        matrix = np.asarray([[values[name] for name in self.frozen.features]], dtype=float)
        return float(self.frozen.models[RULE_ID].predict(matrix)[0])


def project_rows(rows: Iterable[Mapping[str, Any]], scorer: ProjectedR1Scorer,
                 *, holdout_start_ms: float,
                 source_protocol_sha256: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
    selected: list[dict[str, Any]] = []
    counters: Counter = Counter()
    for source in rows:
        receive_ms = float(source["signal_receive_ms"])
        if receive_ms < holdout_start_ms:
            counters["before_holdout"] += 1
            continue
        if (source.get("source_strategy_id") != forward.STRATEGY_ID
                or source.get("source_protocol_sha256") != source_protocol_sha256):
            raise ValueError("R1 projection row source protocol mismatch")
        jump_z = float(source["jump_z"])
        if not math.isfinite(jump_z) or jump_z < forward.CONTROL_CUT:
            identity = (source.get("market_id"), source.get("signal_source_ms"),
                        source.get("evaluation_ms"))
            raise ValueError(f"R1 projection input is outside the frozen jump-z control: {identity!r}")
        try:
            score = float(round(scorer.score(source), 15))
        except IncompleteCausalFeatures:
            counters["incomplete_features"] += 1
            continue
        if not math.isfinite(score):
            raise ValueError("R1 projection score is not finite")
        counters["scored"] += 1
        if score < RULE_CUT:
            counters["below_score_cut"] += 1
            continue
        counters["selected"] += 1
        selected.append({
            **dict(source),
            "projection_strategy_id": STRATEGY_ID,
            "projection_rule_id": RULE_ID,
            "projection_score": score,
            "projection_cut": RULE_CUT,
            "projected_features": list(forward.ABSENT_MODEL_FEATURES),
            "projection_missing_policy": PROJECTION_MISSING_POLICY,
        })
    return selected, dict(counters)


def primary_market_fills(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(
        (dict(row) for row in rows
         if float(row["evaluation_ms"]) == PRIMARY_LATENCY_MS and row.get("filled")),
        key=lambda row: (float(row["signal_receive_ms"]), str(row.get("market_id")),
                         float(row.get("signal_source_ms", row["signal_receive_ms"])),
                         str(row.get("kind", "")), str(row.get("direction", ""))),
    )
    fills: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in ordered:
        market_id = str(row.get("market_id"))
        if market_id in seen:
            continue
        seen.add(market_id)
        fills.append(row)
    return fills


def stopping_sample(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    fills = primary_market_fills(rows)
    days: set[str] = set()
    for index, row in enumerate(fills):
        days.add(str(row["day"]))
        if index + 1 >= MIN_FILLS and len(days) >= MIN_DAYS:
            return fills[:index + 1]
    return []


def verdict(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    materialized = [dict(row) for row in rows]
    current = primary_market_fills(materialized)
    sample = stopping_sample(materialized)
    if not sample or any(row.get("winner") is None for row in sample):
        progress = sample or current
        result: dict[str, Any] = {
            "status": "collecting", "fills": len(progress),
            "days": len({str(row["day"]) for row in progress}),
            "required_fills": MIN_FILLS, "required_days": MIN_DAYS,
            "selected_rows": len(materialized), "primary_fills": len(current),
            "resolved_fills": sum(row.get("winner") is not None for row in current),
            "pending_outcomes": sum(row.get("winner") is None for row in current),
        }
        return result
    shares = sum(float(row["filled_shares"]) for row in sample)
    net_ev = sum(float(row["pnl"]) for row in sample) / shares
    lower = forward.daily_lower_99(sample)
    raw_p = forward.exact_pvalue(sample) if net_ev > 0 else 1.0
    corrected_p = min(1.0, raw_p * FAMILY_TESTS)
    passed = net_ev > 0 and lower is not None and lower > 0 and corrected_p < ALPHA
    return {
        "status": "passed" if passed else "rejected", "fills": len(sample),
        "days": len({str(row["day"]) for row in sample}), "net_ev_per_share": net_ev,
        "day_cluster_lower_99": lower, "raw_exact_p": raw_p,
        "corrected_exact_p": corrected_p, "family_tests": FAMILY_TESTS,
        "last_signal_ms": max(float(row["signal_receive_ms"]) for row in sample),
    }


_OUTCOME_FIELDS = ("winner", "won", "pnl_per_share", "pnl")


def _observation_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (str(row.get("market_id")), str(row.get("kind")), float(row["signal_source_ms"]),
            str(row["direction"]), float(row["evaluation_ms"]))


def _validate_observation(row: Mapping[str, Any]) -> None:
    receive_ms = float(row["signal_receive_ms"])
    if not math.isfinite(receive_ms):
        raise ValueError("R1 projection observation has a non-finite receive timestamp")
    expected_day = datetime.fromtimestamp(receive_ms / 1_000.0, timezone.utc).date().isoformat()
    if row.get("day") != expected_day:
        raise ValueError("R1 projection observation UTC day does not match its receive timestamp")
    if row.get("projection_strategy_id") != STRATEGY_ID:
        raise ValueError("R1 projection observation has the wrong strategy id")


def _outcome(row: Mapping[str, Any]) -> tuple[Any, ...]:
    values = tuple(row.get(name) for name in _OUTCOME_FIELDS)
    if any(value is None for value in values) and not all(value is None for value in values):
        raise ValueError("R1 projection observation has a partial outcome")
    if not row.get("filled") and any(value is not None for value in values):
        raise ValueError("unfilled R1 projection observation carries an outcome")
    if all(value is not None for value in values):
        winner, won, pnl_per_share, pnl = values
        if bool(won) != (str(winner) == str(row.get("direction"))):
            raise ValueError("R1 projection outcome direction is inconsistent")
        all_in = float(row["all_in_cost"])
        shares = float(row["filled_shares"])
        expected_per_share = (1.0 if bool(won) else 0.0) - all_in
        if (not math.isclose(float(pnl_per_share), expected_per_share, rel_tol=0.0, abs_tol=1e-9)
                or not math.isclose(float(pnl), expected_per_share * shares,
                                    rel_tol=0.0, abs_tol=1e-9)):
            raise ValueError("R1 projection PnL is inconsistent with fill cost")
    return values


def _execution(row: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key not in _OUTCOME_FIELDS}


def _sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (float(row["signal_receive_ms"]), str(row.get("market_id")),
            float(row["evaluation_ms"]), str(row.get("direction")),
            str(row.get("kind")), float(row["signal_source_ms"]))


def _jsonl_bytes(rows: Iterable[Mapping[str, Any]]) -> bytes:
    return "".join(json.dumps(row, sort_keys=True, allow_nan=False) + "\n" for row in rows).encode()


def _sample_sha(rows: Iterable[Mapping[str, Any]]) -> str:
    return hashlib.sha256(_jsonl_bytes(rows)).hexdigest()


def _read_jsonl(path: str | Path, *, required: bool = False) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        if required:
            raise ValueError(f"required R1 projection JSONL is missing: {path}")
        return []
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def append_rows(destination: str | Path, incoming: Iterable[Mapping[str, Any]],
                verdict_path: str | Path | None = None) -> list[dict[str, Any]]:
    destination = Path(destination)
    pooled: dict[tuple[Any, ...], dict[str, Any]] = {}
    for raw in [*_read_jsonl(destination), *(dict(row) for row in incoming)]:
        _validate_observation(raw)
        key = _observation_key(raw)
        current_outcome = _outcome(raw)
        previous = pooled.get(key)
        if previous is None:
            pooled[key] = raw
            continue
        if _execution(previous) != _execution(raw):
            raise ValueError(f"execution-field conflict for R1 projection observation {key!r}")
        previous_outcome = _outcome(previous)
        if previous_outcome == current_outcome:
            continue
        if all(value is None for value in previous_outcome) and all(value is not None for value in current_outcome):
            pooled[key] = {**previous, **{name: raw.get(name) for name in _OUTCOME_FIELDS}}
            continue
        raise ValueError(f"outcome conflict for R1 projection observation {key!r}")
    rows = sorted(pooled.values(), key=_sort_key)
    if verdict_path is not None and Path(verdict_path).exists():
        pinned = json.loads(Path(verdict_path).read_text(encoding="utf-8"))
        sample = stopping_sample(rows)
        keys = [list(_observation_key(row)) for row in sample]
        if (not keys or pinned.get("sample_keys") != keys or pinned.get("last_key") != keys[-1]
                or pinned.get("sample_sha256") != _sample_sha(sample)):
            raise ValueError("R1 projection observation would change the pinned stopping prefix")
    _atomic_write(destination, _jsonl_bytes(rows))
    return rows


def pin_verdict(rows: Iterable[Mapping[str, Any]], destination: str | Path,
                protocol: Mapping[str, Any], ledger_path: str | Path,
                source_ledger_sha256: str, source_row_count: int) -> dict[str, Any]:
    rows = [dict(row) for row in rows]
    destination = Path(destination)
    sample = stopping_sample(rows)
    keys = [list(_observation_key(row)) for row in sample]
    if destination.exists():
        pinned = json.loads(destination.read_text(encoding="utf-8"))
        if (pinned.get("strategy_id") != STRATEGY_ID
                or pinned.get("protocol_sha256") != protocol.get("protocol_sha256")
                or not keys or pinned.get("sample_keys") != keys
                or pinned.get("last_key") != keys[-1]
                or pinned.get("sample_sha256") != _sample_sha(sample)):
            raise ValueError("current R1 projection ledger changes the pinned verdict")
        return pinned
    result = verdict(rows)
    if result["status"] == "collecting":
        return result
    ledger_path = Path(ledger_path)
    if _read_jsonl(ledger_path) != sorted(rows, key=_sort_key):
        raise ValueError("R1 projection verdict rows do not match the persisted ledger")
    record = {
        **result,
        "strategy_id": STRATEGY_ID,
        "protocol_sha256": protocol["protocol_sha256"],
        "sample_keys": keys,
        "sample_sha256": _sample_sha(sample),
        "ledger_sha256": _sha256(ledger_path),
        "source_ledger_sha256": source_ledger_sha256,
        "source_row_count": source_row_count,
        "last_key": keys[-1],
        "pinned_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write(destination, (json.dumps(record, indent=2, sort_keys=True) + "\n").encode())
    return record


def load_projected_scorer(protocol_path: str | Path,
                          protocol: Mapping[str, Any] | None = None) -> ProjectedR1Scorer:
    protocol_path = Path(protocol_path)
    payload = dict(protocol) if protocol is not None else load_protocol(protocol_path)[0]
    historical = (protocol_path.parent / str(payload["historical_frozen_file"])).resolve()
    return ProjectedR1Scorer(forward.load_scorer(historical))


def run_protocol(protocol_path: str | Path, source_rows: str | Path, ledger_path: str | Path,
                 report_path: str | Path, verdict_path: str | Path) -> dict[str, Any]:
    protocol, holdout_start_ms = load_protocol(protocol_path)
    scorer = load_projected_scorer(protocol_path, protocol)
    source_path = Path(source_rows)
    source_bytes = source_path.read_bytes() if source_path.is_file() else None
    if source_bytes is None:
        raise ValueError(f"required R1 projection JSONL is missing: {source_path}")
    source = [json.loads(line) for line in source_bytes.decode().splitlines() if line.strip()]
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    report_path = Path(report_path)
    ledger_path = Path(ledger_path)
    selected, counters = project_rows(
        source, scorer, holdout_start_ms=holdout_start_ms,
        source_protocol_sha256=str(protocol["source_control_protocol_sha256"]),
    )
    ledger = append_rows(ledger_path, selected, verdict_path)
    terminal = pin_verdict(ledger, verdict_path, protocol, ledger_path,
                           source_sha256, len(source))
    result = {
        "strategy_id": STRATEGY_ID,
        "protocol_sha256": protocol["protocol_sha256"],
        "source_rows": len(source),
        "source_ledger_sha256": source_sha256,
        "ledger_rows": len(ledger),
        "ledger_sha256": _sha256(ledger_path),
        "projection": counters,
        "verdict": terminal,
    }
    _atomic_write(report_path, (json.dumps(result, indent=2, sort_keys=True) + "\n").encode())
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", required=True)
    parser.add_argument("--source", required=True, help="append-only MIX jump-z control JSONL")
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--verdict", required=True)
    args = parser.parse_args(argv)
    result = run_protocol(args.protocol, args.source, args.ledger, args.report, args.verdict)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
