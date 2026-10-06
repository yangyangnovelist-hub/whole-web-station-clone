"""Strict paper replay for the frozen MIX rules and their receipt-causal jump-z control."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import heapq
import json
import math
import os
import platform
from bisect import bisect_right, insort_right
from collections import Counter, deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

import numpy as np
from scipy.stats import norm

import binary as binary_model
import h_replay_archive as archive
import h_replay_run as hrun
import jump2s as j2
import mix_hf as mix
import pm_outcomes


CONTROL_CUT = 5.834074974060059
PRIMARY_LATENCY_MS = 500.0
MIN_FILLS = 1_000
MIN_DAYS = 7
FAMILY_TESTS = 100
ALPHA = 0.01
FREEZE_SCHEMA = "polymarket-mix-control-forward-v1"
STRATEGY_ID = "mix-r1-jump-z-settle-control"
DIRECT_MODEL_FEATURES = tuple(mix.FEATURES[:13])
TREND_MODEL_FEATURES = ("trend_agree", "trend_side", "vr60")
AUX_MODEL_FEATURES = ("dvol_rv",)
ABSENT_MODEL_FEATURES = ("f_ridge5", "f_hgb5", "f_ridge15", "f_hgb15")


def audit_model_features(manifest: Mapping[str, Any],
                         signal_tape_profile: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """Describe whether each frozen MIX input can be rebuilt without future or external data."""
    if tuple(mix.FEATURES) != (DIRECT_MODEL_FEATURES + TREND_MODEL_FEATURES +
                               ABSENT_MODEL_FEATURES + AUX_MODEL_FEATURES):
        raise ValueError("MIX feature contract no longer covers the frozen feature order")
    counts = manifest.get("counts") or {}
    start, end = manifest.get("started_ms"), manifest.get("ended_ms")
    strict_duration_s = ((float(end) - float(start)) / 1_000.0
                         if start is not None and end is not None and float(end) >= float(start) else None)
    tape_records = int((signal_tape_profile or {}).get("records", 0))
    tape_pre_roll_s = ((signal_tape_profile or {}).get("longest_contiguous_s")
                       if tape_records > 0 else None)
    tape_pre_roll_s = float(tape_pre_roll_s) if tape_pre_roll_s is not None else None
    books = (int(counts.get("clob_snapshot", 0)) > 0 and
             int(manifest.get("markets_with_both_token_snapshots", 0)) > 0)
    markets = int(counts.get("markets", 0)) > 0
    requirements: dict[str, tuple[tuple[str, ...], float, int]] = {
        "jump_bp": (("aggregate_trade_tape",), 5.0, 2),
        "jump_abs_bp": (("aggregate_trade_tape",), 5.0, 2),
        "jump_z": (("aggregate_trade_tape",), 600.0, 300),
        "dir": (("aggregate_trade_tape",), 5.0, 2),
        "is_jump": (("aggregate_trade_tape",), 5.0, 2),
        "h_edge": (("aggregate_trade_tape", "dual_token_l2", "market_mapping"), 600.0, 300),
        "dmid2": (("dual_token_l2",), 3.0, 0),
        "dmid10": (("dual_token_l2",), 11.0, 0),
        "imb": (("dual_token_l2",), 0.0, 0),
        "spread": (("dual_token_l2",), 0.0, 0),
        "ask": (("dual_token_l2",), 0.0, 0),
        "ask_fee": (("dual_token_l2",), 0.0, 0),
        "tau": (("market_mapping",), 0.0, 0),
        "trend_agree": (("aggregate_trade_tape",), float(mix.MIGRATION_KLINE_PRE_ROLL_S), 0),
        "trend_side": (("aggregate_trade_tape",), float(mix.MIGRATION_KLINE_PRE_ROLL_S), 0),
        "vr60": (("aggregate_trade_tape",), float((j2.VR_N + 2) * 60), 0),
        "dvol_rv": (("aggregate_trade_tape", "deribit_dvol"),
                    float(max(mix.RV_N, mix.MIGRATION_DVOL_PRE_ROLL_S)), mix.RV_MIN),
    }
    available = {"aggregate_trade_tape": tape_records > 0,
                 "dual_token_l2": books, "market_mapping": markets,
                 "deribit_dvol": int(counts.get("deribit_dvol", 0)) >= 2}
    features: dict[str, dict[str, Any]] = {}
    for name in mix.FEATURES:
        if name in ABSENT_MODEL_FEATURES:
            source = "walk_forward_factor_prediction"
            features[name] = {
                "status": "absent_source", "inputs": [source], "required_pre_roll_s": None,
                "reason": f"standard strict artifact does not record {source}",
            }
            continue
        inputs, pre_roll, minimum_tape_records = requirements[name]
        missing = [item for item in inputs if not available[item]]
        durations = ([tape_pre_roll_s] if "aggregate_trade_tape" in inputs else []) + \
                    ([strict_duration_s] if "dual_token_l2" in inputs else [])
        usable_pre_roll = (min(float(value) for value in durations if value is not None)
                           if durations and all(value is not None for value in durations) else
                           None if durations else math.inf)
        if missing:
            status, reason = "absent_source", "missing " + ", ".join(missing)
        elif (usable_pre_roll is None or usable_pre_roll + 1e-9 < pre_roll or
              tape_records < minimum_tape_records):
            status = "insufficient_pre_roll"
            if usable_pre_roll is None:
                reason = "required input pre-roll unavailable"
            elif tape_records < minimum_tape_records:
                reason = (f"aggregate tape has {tape_records} records; feature requires "
                          f"at least {minimum_tape_records}")
            else:
                reason = (f"input has {usable_pre_roll:.3f}s contiguous pre-roll; feature requires "
                          f"{pre_roll:.3f}s")
        else:
            status = "implemented_and_causal"
            reason = "built per row from inputs observed at or before the decision"
        features[name] = {"status": status, "inputs": list(inputs),
                          "required_pre_roll_s": pre_roll,
                          "required_tape_records": minimum_tape_records, "reason": reason}
    blocking = [name for name in mix.FEATURES
                if features[name]["status"] != "implemented_and_causal"]
    return {
        "feature_order": list(mix.FEATURES), "features": features,
        "strict_recording_duration_s": strict_duration_s,
        "aggregate_trade_tape": dict(signal_tape_profile or {}),
        "status_counts": dict(Counter(row["status"] for row in features.values())),
        "blocking_features": blocking,
        "full_model_forward_executable": not blocking,
        "missing_policy": "fail_closed_no_imputation_no_posthoc_fetch",
    }


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha(value: Mapping[str, Any]) -> str:
    payload = {key: item for key, item in value.items() if key != "protocol_sha256"}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _atomic_json(path: str | Path, value: Mapping[str, Any]) -> None:
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def load_control_freeze(path: str | Path) -> tuple[dict[str, Any], "ControlConfig"]:
    path = Path(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != FREEZE_SCHEMA or payload.get("strategy_id") != STRATEGY_ID:
        raise ValueError("unknown MIX control freeze")
    if payload.get("protocol_sha256") != _canonical_sha(payload):
        raise ValueError("MIX control freeze fingerprint mismatch")
    expected_protocol = os.environ.get("EXPECTED_PROTOCOL_SHA256")
    if not expected_protocol:
        raise ValueError("EXPECTED_PROTOCOL_SHA256 is required")
    if payload.get("protocol_sha256") != expected_protocol:
        raise ValueError("MIX control freeze does not match EXPECTED_PROTOCOL_SHA256")
    if payload.get("runner_sha256") != sha256_file(__file__):
        raise ValueError("MIX control freeze does not match the audited runner")
    expected = {
        "z_cut": CONTROL_CUT,
        "primary_latency_ms": PRIMARY_LATENCY_MS,
        "min_fills": MIN_FILLS,
        "min_days": MIN_DAYS,
        "family_tests": FAMILY_TESTS,
        "alpha": ALPHA,
    }
    bad = [key for key, value in expected.items() if payload.get(key) != value]
    if bad:
        raise ValueError("MIX control freeze differs from code: " + ", ".join(bad))
    historical = (path.parent / str(payload["historical_frozen_file"])).resolve()
    if payload.get("historical_frozen_sha256") != sha256_file(historical):
        raise ValueError("MIX historical freeze sha256 mismatch")
    start = datetime.fromisoformat(str(payload["holdout_start_utc"]).replace("Z", "+00:00"))
    if start.tzinfo is None:
        raise ValueError("MIX holdout start must include a timezone")
    config = ControlConfig(
        jump_bp=float(payload["jump_bp"]), z_cut=float(payload["z_cut"]),
        reference_min_s=float(payload["reference_min_s"]),
        reference_max_s=float(payload["reference_max_s"]), spacing_s=float(payload["spacing_s"]),
        tau_hi_s=float(payload["tau_hi_s"]), tau_lo_s=float(payload["tau_lo_s"]),
        fixed_taus_s=tuple(int(value) for value in payload["fixed_taus_s"]),
        sigma_window_s=int(payload["sigma_window_s"]),
        sigma_min_observations=int(payload["sigma_min_observations"]),
        forward_fill_s=int(payload["forward_fill_s"]),
        fill_latencies_ms=tuple(float(value) for value in payload["fill_latencies_ms"]),
        shares=float(payload["shares"]), min_price=float(payload["min_price"]),
        limit_price=float(payload["limit_price"]),
        fee_rate=float(payload["fee_rate"]), book_fresh_ms=float(payload["book_fresh_ms"]),
        holdout_start_ms=start.timestamp() * 1_000.0,
    )
    if PRIMARY_LATENCY_MS not in config.fill_latencies_ms:
        raise ValueError("MIX primary latency is absent from the freeze")
    return payload, config


@dataclass(frozen=True)
class FrozenMixScorer:
    features: tuple[str, ...]
    rules: tuple[dict[str, Any], ...]
    models: Mapping[str, Any]

    def score_historical_audit_only(self, values: Mapping[str, float]) -> dict[str, float]:
        """Reproduce frozen historical scores; never an executable forward decision API."""
        missing = [name for name in self.features if name not in values]
        if missing:
            raise ValueError("missing MIX features: " + ", ".join(missing))
        row = np.asarray([[float(values[name]) for name in self.features]], dtype=float)
        return {str(rule["id"]): float(self.models[str(rule["id"])].predict(row)[0]) for rule in self.rules}

    def selected_historical_audit_only(self, values: Mapping[str, float]) -> dict[str, float]:
        scores = self.score_historical_audit_only(values)
        return {str(rule["id"]): scores[str(rule["id"])] for rule in self.rules
                if scores[str(rule["id"])] >= float(rule["cut"])}


def load_scorer(frozen_path: str | Path) -> FrozenMixScorer:
    import joblib
    import sklearn

    frozen_path = Path(frozen_path)
    frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
    model_path, manifest_path = mix.model_bundle_paths(frozen_path)
    anchor_path = mix.model_anchor_path(frozen_path)
    frozen_version = frozen.get("a_fingerprint_version", mix.A_FINGERPRINT_V1_ROUNDED)
    approval_binding, migration_binding, approval = {}, {}, None
    approval_path = mix.model_approval_path(frozen_path)
    if frozen_version == mix.A_FINGERPRINT_V1_ROUNDED:
        if not approval_path.exists():
            raise ValueError("MIX legacy model approval is missing")
        approval = json.loads(approval_path.read_text(encoding="utf-8"))
        provenance = approval.get("provenance")
        provenance_sha = approval.get("provenance_sha256")
        try:
            provenance = mix._validated_migration_provenance(provenance, frozen)
        except ValueError as error:
            raise ValueError("MIX legacy model approval provenance mismatch") from error
        if (approval.get("format") != mix.MODEL_APPROVAL_FORMAT
                or provenance_sha != mix._canonical_json_sha256(provenance)):
            raise ValueError("MIX legacy model approval provenance mismatch")
        approval_binding = {"approval_file": approval_path.name,
                            "approval_sha256": sha256_file(approval_path)}
        migration_binding = {"migration_provenance_sha256": provenance_sha}
    if not anchor_path.exists():
        raise ValueError("MIX model anchor is missing")
    anchor = json.loads(anchor_path.read_text(encoding="utf-8"))
    frozen_sha = sha256_file(frozen_path)
    anchor_files = {
        "format": mix.MODEL_ANCHOR_FORMAT,
        "frozen_file": frozen_path.name,
        "frozen_sha256": frozen_sha,
        "model_file": model_path.name,
        "model_sha256": sha256_file(model_path),
        "manifest_file": manifest_path.name,
        "manifest_sha256": sha256_file(manifest_path),
        **approval_binding,
        **migration_binding,
    }
    if any(anchor.get(key) != value for key, value in anchor_files.items()):
        raise ValueError("MIX model anchor file hashes mismatch")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != mix.MODEL_BUNDLE_FORMAT:
        raise ValueError("unknown MIX model bundle format")
    if manifest.get("frozen_sha256") != frozen_sha:
        raise ValueError("MIX frozen sha256 mismatch")
    if manifest.get("features") != frozen.get("features"):
        raise ValueError("MIX feature schema differs from the frozen study")
    frozen_rules = [{key: rule[key] for key in ("id", "policy", "model", "q", "cut")}
                    for rule in frozen.get("rules", [])]
    if manifest.get("rules") != frozen_rules:
        raise ValueError("MIX model rules differ from the frozen study")
    match_kind = ("legacy_v1_rounded" if frozen_version == mix.A_FINGERPRINT_V1_ROUNDED
                  else "canonical_v2_exact" if frozen_version == mix.A_FINGERPRINT_V2_EXACT else None)
    canonical = manifest.get("canonical_a_fingerprint")
    identity_expected = {
        "a_fingerprint": frozen.get("a_fingerprint"),
        "a_fingerprint_version": frozen_version,
        "frozen_a_fingerprint": frozen.get("a_fingerprint"),
        "frozen_a_fingerprint_version": frozen_version,
        "canonical_a_fingerprint_version": mix.A_FINGERPRINT_V2_EXACT,
        "a_identity_match": match_kind,
    }
    if (match_kind is None or any(manifest.get(key) != value for key, value in identity_expected.items())
            or not isinstance(canonical, str) or len(canonical) != 64
            or any(char not in "0123456789abcdef" for char in canonical)
            or (frozen_version == mix.A_FINGERPRINT_V2_EXACT
                and canonical != frozen.get("a_fingerprint"))):
        raise ValueError("MIX A identity mismatch")
    anchor_identity = {**identity_expected, "canonical_a_fingerprint": canonical,
                       "training_matrix_sha256": manifest.get("training_matrix_sha256")}
    if any(anchor.get(key) != value for key, value in anchor_identity.items()):
        raise ValueError("MIX model anchor A identity mismatch")
    if frozen_version == mix.A_FINGERPRINT_V1_ROUNDED:
        approval_expected = {
            "format": mix.MODEL_APPROVAL_FORMAT,
            "frozen_file": frozen_path.name,
            "frozen_sha256": frozen_sha,
            **identity_expected,
            "canonical_a_fingerprint": canonical,
            "training_rows": manifest.get("training_rows"),
            "training_matrix_sha256": manifest.get("training_matrix_sha256"),
            "provenance_sha256": provenance_sha,
        }
        if any(approval.get(key) != value for key, value in approval_expected.items()):
            raise ValueError("MIX legacy model approval differs from the fitted exact A identity")
    if manifest.get("model_file") != model_path.name or manifest.get("model_sha256") != sha256_file(model_path):
        raise ValueError("MIX model bundle sha256 mismatch")
    payload = joblib.load(model_path)
    runtime = {"python": platform.python_version(), "numpy": np.__version__, "sklearn": sklearn.__version__,
               "joblib": joblib.__version__}
    if manifest.get("versions") != runtime:
        raise ValueError("MIX model runtime differs from the fitted bundle")
    identity_payload = {**identity_expected, "canonical_a_fingerprint": canonical}
    if any(manifest.get(key) != value for key, value in migration_binding.items()):
        raise ValueError("MIX migration provenance binding mismatch")
    audit_fields = ("training_matrix_sha256", "a_prediction_sha256", "selected_row_keys_sha256")
    if (payload.get("format") != mix.MODEL_BUNDLE_FORMAT or
            payload.get("frozen_sha256") != manifest.get("frozen_sha256") or
            payload.get("features") != manifest.get("features") or
            payload.get("rules") != manifest.get("rules") or
            payload.get("versions") != manifest.get("versions") or
            any(payload.get(key) != value for key, value in migration_binding.items()) or
            any(payload.get(key) != value for key, value in identity_payload.items()) or
            any(payload.get(key) != manifest.get(key) for key in audit_fields)):
        raise ValueError("MIX model payload differs from its manifest")
    return FrozenMixScorer(tuple(manifest["features"]), tuple(manifest["rules"]), payload["models"])


class ReceiptTrend:
    """Closed-minute trend inputs built only from trades received so far."""

    def __init__(self) -> None:
        self.closes: dict[int, tuple[float, float]] = {}
        self.latest_source_ms = -math.inf
        self.latest_receive_ms = -math.inf

    def reset(self) -> None:
        self.__init__()

    def update(self, receive_ms: float, source_ms: float, price: float) -> None:
        if receive_ms + 1e-9 < self.latest_receive_ms:
            raise ValueError("MIX trend receipt time regressed")
        self.latest_receive_ms = receive_ms
        self.latest_source_ms = max(self.latest_source_ms, source_ms)
        minute = int(math.floor(source_ms / 60_000.0) * 60)
        previous = self.closes.get(minute)
        if previous is None or source_ms >= previous[0]:
            self.closes[minute] = (source_ms, price)

    def features(self, candidate: "ControlCandidate", side: int) -> dict[str, float]:
        if not math.isfinite(self.latest_source_ms):
            ret4h = vr60 = math.nan
        else:
            closes = {minute: value[1] for minute, value in self.closes.items()}
            ret, vr = j2.trend_features(
                closes, np.asarray([self.latest_source_ms / 1_000.0 - mix.KLINE_GUARD_S]),
            )
            ret4h, vr60 = float(ret[0]), float(vr[0])
        return {
            "trend_agree": (float(np.sign(candidate.move) * np.sign(ret4h))
                            if math.isfinite(ret4h) and math.isfinite(candidate.move) else math.nan),
            "trend_side": (float(np.sign((1 if side > 0 else -1) * ret4h))
                           if math.isfinite(ret4h) else math.nan),
            "vr60": vr60,
        }


@dataclass(frozen=True)
class ControlConfig:
    jump_bp: float = 2.0
    z_cut: float = CONTROL_CUT
    reference_min_s: float = 1.0
    reference_max_s: float = 5.0
    spacing_s: float = 5.0
    tau_hi_s: float = 285.0
    tau_lo_s: float = 20.0
    fixed_taus_s: tuple[int, ...] = (240, 180, 120, 60)
    sigma_window_s: int = 600
    sigma_min_observations: int = 300
    forward_fill_s: int = 20
    fill_latencies_ms: tuple[float, ...] = (300.0, 400.0, 500.0)
    shares: float = 5.0
    min_price: float = 0.02
    limit_price: float = 0.98
    fee_rate: float = 0.07
    book_fresh_ms: float = 1_000.0
    holdout_start_ms: Optional[float] = None


class ReceiptSigma:
    """One-second receipt-clock volatility, excluding the current partial second."""

    def __init__(self, window: int, minimum: int, forward_fill_s: int) -> None:
        self.window = int(window)
        self.minimum = int(minimum)
        self.forward_fill_s = int(forward_fill_s)
        self.current_second: Optional[int] = None
        self.current_value = math.nan
        self.last_trade_second: Optional[int] = None
        self.last_trade_value = math.nan
        self.last_final_value = math.nan
        self.returns: deque[float] = deque()
        self.count = 0
        self.total = 0.0
        self.total_sq = 0.0

    def _push_return(self, value: float) -> None:
        self.returns.append(value)
        if math.isfinite(value):
            self.count += 1
            self.total += value
            self.total_sq += value * value
        if len(self.returns) > self.window:
            old = self.returns.popleft()
            if math.isfinite(old):
                self.count -= 1
                self.total -= old
                self.total_sq -= old * old

    def _finalize(self, value: float) -> None:
        difference = value - self.last_final_value if math.isfinite(value) and math.isfinite(self.last_final_value) else math.nan
        if math.isfinite(self.last_final_value) or self.returns:
            self._push_return(difference)
        self.last_final_value = value

    def advance(self, timestamp_s: float) -> None:
        target = int(math.floor(timestamp_s))
        if self.current_second is None:
            self.current_second = target
            return
        while self.current_second < target:
            self._finalize(self.current_value)
            self.current_second += 1
            if (self.last_trade_second is not None and
                    self.current_second - self.last_trade_second <= self.forward_fill_s):
                self.current_value = self.last_trade_value
            else:
                self.current_value = math.nan

    def update(self, timestamp_s: float, log_price: float) -> None:
        self.advance(timestamp_s)
        second = int(math.floor(timestamp_s))
        if self.current_second != second:
            return
        self.current_value = log_price
        self.last_trade_second = second
        self.last_trade_value = log_price

    def sigma(self, timestamp_s: float) -> float:
        self.advance(timestamp_s)
        if self.count < max(self.minimum, 2):
            return math.nan
        variance = (self.total_sq - self.total * self.total / self.count) / (self.count - 1)
        return math.sqrt(max(variance, 0.0))


class ReceiptDvolRv:
    """DVOL minus receipt-clock rv30, using only a proven-closed Deribit source hour."""

    def __init__(self, window: int = mix.RV_N, minimum: int = mix.RV_MIN,
                 forward_fill_s: int = mix.GAP_FILL_S) -> None:
        self.window = int(window)
        self.minimum = int(minimum)
        self.forward_fill_s = int(forward_fill_s)
        self.grid = ReceiptSigma(self.window, self.minimum, self.forward_fill_s)
        self.current_hour: Optional[int] = None
        self.current_value = math.nan
        self.closed_value = math.nan
        self.closed_valid_until_ms = -math.inf
        self.latest_receive_ms = -math.inf

    def reset_spot(self) -> None:
        self.grid = ReceiptSigma(self.window, self.minimum, self.forward_fill_s)

    def reset_dvol_current(self) -> None:
        self.current_hour = None
        self.current_value = math.nan

    def update_spot(self, receive_ms: float, price: float) -> None:
        if math.isfinite(price) and price > 0:
            self.grid.update(receive_ms / 1_000.0, math.log(price))

    def update_dvol(self, receive_ms: float, source_ms: float, volatility: float) -> None:
        if receive_ms + 1e-9 < self.latest_receive_ms:
            raise ValueError("MIX DVOL receipt time regressed")
        self.latest_receive_ms = receive_ms
        if not (math.isfinite(source_ms) and math.isfinite(volatility) and volatility > 0):
            return
        hour = int(math.floor(source_ms / 3_600_000.0) * 3_600)
        if self.current_hour is None:
            self.current_hour, self.current_value = hour, volatility
        elif hour == self.current_hour:
            self.current_value = volatility
        elif hour > self.current_hour:
            self.closed_value = self.current_value
            self.closed_valid_until_ms = (self.current_hour + 7_200) * 1_000.0
            self.current_hour, self.current_value = hour, volatility

    def value(self, receive_ms: float) -> float:
        rv30 = self.grid.sigma(receive_ms / 1_000.0) * math.sqrt(mix.SEC_YEAR)
        if not (math.isfinite(self.closed_value) and math.isfinite(rv30) and
                receive_ms <= self.closed_valid_until_ms + 1e-6):
            return math.nan
        return self.closed_value / 100.0 - rv30


@dataclass(frozen=True)
class ControlCandidate:
    slot: int
    receive_ms: float
    source_ms: float
    direction: int
    move: float
    sigma: float
    z: float
    kind: str
    receipt_move_2s: float = math.nan
    direct_features: tuple[tuple[str, float], ...] = ()
    trend_features: tuple[tuple[str, float], ...] = ()
    aux_features: tuple[tuple[str, float], ...] = ()


class ReceiptJumpTrigger:
    """MIX receipt-clock trigger, including raw-jump spacing before score filtering."""

    def __init__(self, config: ControlConfig | None = None) -> None:
        self.config = config or ControlConfig()
        self.grid = ReceiptSigma(self.config.sigma_window_s, self.config.sigma_min_observations,
                                 self.config.forward_fill_s)
        self.source_rows: list[tuple[float, int, float]] = []
        self.receipt_rows: list[tuple[float, int, float]] = []
        self.order = 0
        self.last_received: Optional[tuple[float, float, float, float]] = None
        self.last_kept: dict[int, float] = {}

    def reset(self) -> None:
        self.__init__(self.config)

    def _reference(self, source_s: float) -> Optional[float]:
        key = (source_s - self.config.reference_min_s + 1e-6, math.inf, math.inf)
        index = bisect_right(self.source_rows, key) - 1
        if index < 0:
            return None
        reference_s, _, reference_price = self.source_rows[index]
        if source_s - reference_s > self.config.reference_max_s + 1e-6:
            return None
        return reference_price

    def _receipt_price(self, receive_s: float) -> Optional[float]:
        index = bisect_right(self.receipt_rows, (receive_s + 1e-6, math.inf, math.inf)) - 1
        if index < 0:
            return None
        timestamp, _, value = self.receipt_rows[index]
        return value if receive_s - timestamp <= mix.PRICE_MAX_AGE_S + 1e-6 else None

    def receipt_move_2s(self, receive_ms: float) -> float:
        receive_s = receive_ms / 1_000.0
        current = self._receipt_price(receive_s)
        prior = self._receipt_price(receive_s - mix.H_ANCHOR_S)
        return current - prior if current is not None and prior is not None else math.nan

    def _candidate(self, receive_s: float, source_s: float, move: float, sigma: float, kind: str,
                   slot: Optional[int] = None, receipt_move_2s: float = math.nan) -> Optional[ControlCandidate]:
        if not (math.isfinite(move) and math.isfinite(sigma) and sigma > 0 and move != 0):
            return None
        slot = int(receive_s // 300) * 300 if slot is None else int(slot)
        tau = slot + 300 - receive_s
        if kind == "jump" and not (self.config.tau_lo_s - 1e-6 <= tau <= self.config.tau_hi_s + 1e-6):
            return None
        direction = 1 if move > 0 else -1
        return ControlCandidate(slot, receive_s * 1_000.0, source_s * 1_000.0, direction, move, sigma,
                                abs(move) / sigma, kind, receipt_move_2s)

    def update(self, receive_ms: float, source_ms: float, price: float) -> Optional[ControlCandidate]:
        if not (math.isfinite(price) and price > 0):
            return None
        receive_s, source_s, log_price = receive_ms / 1_000.0, source_ms / 1_000.0, math.log(price)
        self.grid.update(receive_s, log_price)
        reference = self._reference(source_s)
        move = log_price - reference if reference is not None else math.nan
        receipt_reference = self._receipt_price(receive_s - mix.H_ANCHOR_S)
        receipt_move_2s = log_price - receipt_reference if receipt_reference is not None else math.nan
        insort_right(self.source_rows, (source_s, self.order, log_price))
        self.receipt_rows.append((receive_s, self.order, log_price))
        self.order += 1
        if self.source_rows:
            latest = self.source_rows[-1][0]
            cut = bisect_right(self.source_rows, (latest - 30.0, -1, -math.inf))
            if cut:
                del self.source_rows[:cut]
        if self.receipt_rows:
            cut = bisect_right(self.receipt_rows, (receive_s - 30.0, -1, -math.inf))
            if cut:
                del self.receipt_rows[:cut]
        self.last_received = (receive_s, source_s, log_price, move)
        if not math.isfinite(move) or abs(move) * 1e4 < self.config.jump_bp - 1e-9:
            return None
        slot = int(receive_s // 300) * 300
        last = self.last_kept.get(slot)
        if last is not None and receive_s < last + self.config.spacing_s - 1e-6:
            return None
        tau = slot + 300 - receive_s
        if not (self.config.tau_lo_s - 1e-6 <= tau <= self.config.tau_hi_s + 1e-6):
            return None
        self.last_kept[slot] = receive_s
        return self._candidate(receive_s, source_s, move, self.grid.sigma(receive_s), "jump", slot,
                               receipt_move_2s)

    def fixed(self, receive_ms: float, slot: int) -> Optional[ControlCandidate]:
        receive_s = receive_ms / 1_000.0
        sigma = self.grid.sigma(receive_s)
        if self.last_received is None or receive_s - self.last_received[0] > mix.PRICE_MAX_AGE_S + 1e-6:
            return None
        _, source_s, log_price, move = self.last_received
        receipt_reference = self._receipt_price(receive_s - mix.H_ANCHOR_S)
        receipt_move_2s = log_price - receipt_reference if receipt_reference is not None else math.nan
        return self._candidate(receive_s, source_s, move, sigma, "fixed", slot,
                               receipt_move_2s)


@dataclass(frozen=True)
class _BookState:
    receive_ms: float
    epoch: int
    up_ready: bool
    down_ready: bool
    up_source_ms: float
    down_source_ms: float
    up_last_change_ms: float
    down_last_change_ms: float
    up_bids: tuple[tuple[float, float], ...]
    down_bids: tuple[tuple[float, float], ...]
    up_asks: tuple[tuple[float, float], ...]
    down_asks: tuple[tuple[float, float], ...]


@dataclass
class _BookHistory:
    receives: list[float] = field(default_factory=list)
    states: list[_BookState] = field(default_factory=list)

    def push(self, state: _BookState) -> None:
        receive_ms = max(state.receive_ms, self.receives[-1]) if self.receives else state.receive_ms
        self.receives.append(receive_ms)
        self.states.append(_BookState(receive_ms, state.epoch, state.up_ready, state.down_ready,
                                      state.up_source_ms, state.down_source_ms,
                                      state.up_last_change_ms, state.down_last_change_ms,
                                      state.up_bids, state.down_bids,
                                      state.up_asks, state.down_asks))
        cutoff = receive_ms - 10_000.0
        index = bisect_right(self.receives, cutoff) - 1
        if index > 0:
            del self.receives[:index]
            del self.states[:index]

    def at(self, receive_ms: float) -> Optional[_BookState]:
        index = bisect_right(self.receives, receive_ms) - 1
        return self.states[index] if index >= 0 else None


def _direct_asks(market: hrun.DirectMarketBook, direction: str) -> tuple[tuple[float, float], ...]:
    token = market.up if direction == "Up" else market.down
    return tuple(sorted((float(price), float(size)) for price, size in token.asks.items() if size > 0))


def _direct_bids(market: hrun.DirectMarketBook, direction: str) -> tuple[tuple[float, float], ...]:
    token = market.up if direction == "Up" else market.down
    return tuple(sorted(((float(price), float(size)) for price, size in token.bids.items() if size > 0),
                        reverse=True))


def _token_uncrossed(token: hrun.TokenBook) -> bool:
    return bool(token.asks and (not token.bids or max(token.bids) < min(token.asks)))


def _feature_quote(history: _BookHistory, at_ms: float, side: int,
                   epoch: int) -> Optional[tuple[float, float, float, float]]:
    state = history.at(at_ms)
    if state is None or state.epoch != epoch or at_ms - state.receive_ms > 1_000.0 + 1e-6:
        return None
    ready = state.up_ready if side > 0 else state.down_ready
    bids = state.up_bids if side > 0 else state.down_bids
    asks = state.up_asks if side > 0 else state.down_asks
    if not ready or not bids or not asks:
        return None
    bid, bid_size = bids[0]
    ask, ask_size = asks[0]
    if not (math.isfinite(bid) and math.isfinite(ask) and bid < ask):
        return None
    return bid, ask, bid_size, ask_size


def direct_model_features(candidate: ControlCandidate, history: _BookHistory, side: int, epoch: int,
                          twap_window_s: Optional[int] = None) -> dict[str, float]:
    """Build the 13 frozen receipt-causal features that need no external factor/DVOL source."""
    side = 1 if side > 0 else -1
    current = _feature_quote(history, candidate.receive_ms, side, epoch)
    lag2 = _feature_quote(history, candidate.receive_ms - 2_000.0, side, epoch)
    lag10 = _feature_quote(history, candidate.receive_ms - 10_000.0, side, epoch)
    prior_up = _feature_quote(history, candidate.receive_ms - 2_000.0, 1, epoch)
    bid, ask, bid_size, ask_size = current or (math.nan, math.nan, math.nan, math.nan)
    mid = (bid + ask) / 2.0
    lag2_mid = ((lag2[0] + lag2[1]) / 2.0) if lag2 is not None else math.nan
    lag10_mid = ((lag10[0] + lag10[1]) / 2.0) if lag10 is not None else math.nan
    prior = (float(np.clip((prior_up[0] + prior_up[1]) / 2.0, 0.005, 0.995))
             if prior_up is not None else math.nan)
    tau = candidate.slot + 300.0 - candidate.receive_ms / 1_000.0
    elapsed = candidate.receive_ms / 1_000.0 - candidate.slot
    if twap_window_s is None:
        twap_window_s = int(pm_outcomes.rule_window("5m", np.asarray([candidate.slot]))[0])
    factor = (float(binary_model.twap_std_factor(elapsed, binary_model.WINDOW_S,
                                                  max(int(twap_window_s), 1)))
              if twap_window_s > 0 else math.sqrt(max(binary_model.WINDOW_S - elapsed, 1.0)))
    scale = factor * candidate.sigma
    fair_up = (float(norm.cdf(norm.ppf(prior) + candidate.receipt_move_2s / scale))
               if math.isfinite(candidate.receipt_move_2s) and scale > 0 else math.nan)
    fair = fair_up if side > 0 else 1.0 - fair_up
    denominator = bid_size + ask_size
    values = {
        "jump_bp": side * candidate.move * 1e4,
        "jump_abs_bp": abs(candidate.move) * 1e4,
        "jump_z": side * candidate.move / candidate.sigma,
        "dir": float(np.sign(side * candidate.move)),
        "is_jump": 1.0 if candidate.kind == "jump" else 0.0,
        "h_edge": fair - ask,
        "dmid2": mid - lag2_mid,
        "dmid10": mid - lag10_mid,
        "imb": (bid_size - ask_size) / denominator if denominator > 0 else math.nan,
        "spread": ask - bid,
        "ask": ask,
        "ask_fee": float(mix.fee(ask)),
        "tau": tau,
    }
    return {name: float(values[name]) for name in DIRECT_MODEL_FEATURES}


def _fill_best(levels: tuple[tuple[float, float], ...], shares: float, limit: float,
               fee_rate: float, minimum: float = 0.02) -> tuple[float, Optional[float], float, str]:
    """Frozen MIX entry semantics: all five shares must exist at the single best ask."""
    if not levels:
        return 0.0, None, 0.0, "ask_missing"
    price, available = levels[0]
    if price < minimum - 1e-9:
        return 0.0, None, 0.0, "below_price_band"
    if price > limit + 1e-9:
        return 0.0, None, 0.0, "above_limit"
    if available < shares - 1e-9:
        return 0.0, None, 0.0, "insufficient_depth"
    fee = shares * fee_rate * price * (1.0 - price)
    return shares, price, fee, "filled"


def replay_normalized(source_events: Iterable[dict[str, Any]], clob_events: Iterable[dict[str, Any]],
                      mappings: Iterable[dict[str, Any]], outcomes: Mapping[str, str],
                      config: ControlConfig | None = None) -> tuple[list[dict[str, Any]], Counter]:
    config = config or ControlConfig()
    mapping_rows = list(mappings)
    markets, tokens = hrun._mapping_dict(mapping_rows)
    by_id = {market.market_id: market for market in markets.values()}
    histories = {market.market_id: _BookHistory() for market in markets.values()}
    book_signatures: dict[tuple[str, str], tuple[Any, ...]] = {}
    last_change_ms: dict[tuple[str, str], float] = {}
    trigger = ReceiptJumpTrigger(config)
    trend = ReceiptTrend()
    dvol_rv = ReceiptDvolRv()
    counters: Counter = Counter()
    rows: list[dict[str, Any]] = []
    pending: list[tuple[float, int, ControlCandidate, float, int]] = []
    sequence = 0
    connected = False
    active_epoch = 0
    watermark = -math.inf
    group_receive_ms: Optional[float] = None
    deferred_candidates: list[Optional[ControlCandidate]] = []

    timers = sorted((int(row["slot"] + 300 - tau) * 1_000.0, int(row["slot"]))
                    for row in mapping_rows for tau in config.fixed_taus_s)
    base = hrun._merged_events(source_events, clob_events)
    decorated_base = ((float(event["recv_ms"]), 0, index, event) for index, event in enumerate(base))
    decorated_timers = ((timestamp, 1, index, {"kind": "fixed_timer", "recv_ms": timestamp, "slot": slot})
                        for index, (timestamp, slot) in enumerate(timers))

    def evaluate(candidate: ControlCandidate, latency: float, epoch: int, reason_override: Optional[str] = None) -> None:
        target = candidate.receive_ms + latency
        market = markets.get(candidate.slot)
        direction = "Up" if candidate.direction > 0 else "Down"
        state = histories.get(market.market_id if market else "", _BookHistory()).at(target) if market else None
        reason = reason_override
        quantity, price, fee = 0.0, None, 0.0
        if reason is None and market is None:
            reason = "market_missing"
        elif reason is None and state is None:
            reason = "book_unavailable"
        elif reason is None and (state.epoch != epoch or
                                 not (state.up_ready if direction == "Up" else state.down_ready)):
            reason = "book_epoch_changed"
        elif reason is None and target - (state.up_last_change_ms if direction == "Up" else
                                          state.down_last_change_ms) > config.book_fresh_ms + 1e-6:
            reason = "book_stale"
        elif reason is None:
            levels = state.up_asks if direction == "Up" else state.down_asks
            quantity, price, fee, reason = _fill_best(
                levels, config.shares, config.limit_price, config.fee_rate, config.min_price
            )
        filled = reason == "filled"
        winner = outcomes.get(market.market_id) if filled and market else None
        all_in = price + fee / quantity if filled and price is not None and quantity else None
        won = bool(winner == direction) if winner is not None else None
        pnl_per_share = ((1.0 if won else 0.0) - all_in) if filled and winner is not None else None
        rows.append({
            "market_id": market.market_id if market else None,
            "slot": candidate.slot,
            "kind": candidate.kind,
            "signal_receive_ms": candidate.receive_ms,
            "signal_source_ms": candidate.source_ms,
            "direction": direction,
            "jump_bp": candidate.move * candidate.direction * 1e4,
            "jump_z": candidate.z,
            "evaluation_ms": latency,
            "target_receive_ms": target,
            "book_source_ms": ((state.up_source_ms if direction == "Up" else state.down_source_ms)
                               if state is not None and math.isfinite(
                                   state.up_source_ms if direction == "Up" else state.down_source_ms)
                               else None),
            "limit_price": config.limit_price,
            "reason": reason,
            "filled": filled,
            "filled_shares": config.shares if filled else quantity,
            "fill_price": price if filled else None,
            "fill_fee": fee if filled else 0.0,
            "all_in_cost": all_in,
            "winner": winner,
            "won": won,
            "pnl_per_share": pnl_per_share,
            "pnl": pnl_per_share * config.shares if pnl_per_share is not None else None,
            "model_features_direct": {
                name: (value if math.isfinite(value) else None)
                for name, value in candidate.direct_features
            },
            "model_features_trend": {
                name: (value if math.isfinite(value) else None)
                for name, value in candidate.trend_features
            },
            "model_features_aux": {
                name: (value if math.isfinite(value) else None)
                for name, value in candidate.aux_features
            },
            "day": datetime.fromtimestamp(candidate.receive_ms / 1_000.0, timezone.utc).date().isoformat(),
        })

    def drain(inclusive: bool = True) -> None:
        threshold = watermark + 1e-6 if inclusive else math.nextafter(watermark, -math.inf)
        while pending and pending[0][0] <= threshold:
            _, _, candidate, latency, epoch = heapq.heappop(pending)
            evaluate(candidate, latency, epoch)

    def select(candidate: Optional[ControlCandidate]) -> None:
        nonlocal sequence
        if candidate is None:
            return
        counters[f"candidate_{candidate.kind}"] += 1
        if config.holdout_start_ms is not None and candidate.receive_ms < config.holdout_start_ms:
            counters["before_holdout"] += 1
            return
        if candidate.z < config.z_cut:
            counters["below_z_cut"] += 1
            return
        counters["selected"] += 1
        market = markets.get(candidate.slot)
        if market is not None:
            values = direct_model_features(candidate, histories[market.market_id], candidate.direction,
                                           active_epoch)
        else:
            values = {name: math.nan for name in DIRECT_MODEL_FEATURES}
        trend_values = trend.features(candidate, candidate.direction)
        aux_values = {"dvol_rv": dvol_rv.value(candidate.receive_ms)}
        candidate = replace(candidate, direct_features=tuple(values.items()),
                            trend_features=tuple(trend_values.items()),
                            aux_features=tuple(aux_values.items()))
        if not connected:
            for latency in config.fill_latencies_ms:
                evaluate(candidate, float(latency), active_epoch, "clob_disconnected")
            return
        for latency in config.fill_latencies_ms:
            heapq.heappush(pending, (candidate.receive_ms + float(latency), sequence, candidate,
                                    float(latency), active_epoch))
            sequence += 1

    def finish_receive_group() -> None:
        for candidate in deferred_candidates:
            if candidate is not None:
                candidate = replace(candidate, receipt_move_2s=trigger.receipt_move_2s(candidate.receive_ms))
            select(candidate)
        deferred_candidates.clear()
        drain()

    for _, _, _, event in heapq.merge(decorated_base, decorated_timers, key=lambda item: item[:3]):
        event_receive_ms = float(event["recv_ms"])
        if group_receive_ms is not None and event_receive_ms > group_receive_ms + 1e-6:
            finish_receive_group()
        group_receive_ms = event_receive_ms
        watermark = max(watermark, event_receive_ms)
        kind = str(event["kind"])
        if kind == "fixed_timer":
            deferred_candidates.append(trigger.fixed(event_receive_ms, int(event["slot"])))
            continue
        if kind == "spot_connection" or kind == "spot_disconnect":
            trigger.reset()
            trend.reset()
            dvol_rv.reset_spot()
            counters[kind] += 1
            continue
        if kind == "spot_trade":
            counters["spot_trades"] += 1
            trend.update(event_receive_ms, float(event["source_ts_ms"]), float(event["price"]))
            dvol_rv.update_spot(event_receive_ms, float(event["price"]))
            deferred_candidates.append(
                trigger.update(event_receive_ms, float(event["source_ts_ms"]), float(event["price"]))
            )
            continue
        if kind == "deribit_connection" or kind == "deribit_disconnect":
            dvol_rv.reset_dvol_current()
            counters[kind] += 1
            continue
        if kind == "deribit_dvol":
            dvol_rv.update_dvol(event_receive_ms, float(event["source_ts_ms"]),
                                float(event["volatility"]))
            counters["deribit_dvol"] += 1
            continue
        if kind != "clob_batch":
            continue
        touched: dict[str, hrun.DirectMarketBook] = {}
        for change in event["events"]:
            change_kind = str(change["kind"])
            if change_kind == "clob_connection":
                connected = True
                active_epoch = int(change["connection_epoch"])
                for market in by_id.values():
                    market.up = hrun.TokenBook()
                    market.down = hrun.TokenBook()
                    for direction in ("Up", "Down"):
                        book_signatures.pop((market.market_id, direction), None)
                        last_change_ms[(market.market_id, direction)] = float(event["recv_ms"])
                    histories[market.market_id].push(_BookState(
                        float(event["recv_ms"]), active_epoch, False, False, -math.inf, -math.inf,
                        float(event["recv_ms"]), float(event["recv_ms"]), (), (), (), ()
                    ))
                continue
            if change_kind == "clob_error":
                connected = False
                for market in by_id.values():
                    market.up = hrun.TokenBook()
                    market.down = hrun.TokenBook()
                    for direction in ("Up", "Down"):
                        book_signatures.pop((market.market_id, direction), None)
                        last_change_ms[(market.market_id, direction)] = float(event["recv_ms"])
                    histories[market.market_id].push(_BookState(
                        float(event["recv_ms"]), active_epoch, False, False, -math.inf, -math.inf,
                        float(event["recv_ms"]), float(event["recv_ms"]), (), (), (), ()
                    ))
                continue
            if not connected:
                continue
            hit = tokens.get(str(change.get("asset_id")))
            if hit is None:
                continue
            market, direction = hit
            token = market.up if direction == "Up" else market.down
            source_ms = float(change["source_ts_ms"])
            if change_kind == "clob_snapshot" and source_ms >= token.source_ms:
                token.replace(change["bids"], change["asks"], source_ms)
            elif change_kind == "clob_price_change":
                token.change(str(change["side"]), float(change["price"]), float(change["size"]), source_ms,
                             change.get("best_bid"), change.get("best_ask"))
            touched[market.market_id] = market
        for market in touched.values():
            up_ready = _token_uncrossed(market.up)
            down_ready = _token_uncrossed(market.down)
            up_bids, down_bids = _direct_bids(market, "Up"), _direct_bids(market, "Down")
            up_asks, down_asks = _direct_asks(market, "Up"), _direct_asks(market, "Down")
            def top(token: hrun.TokenBook) -> tuple[Optional[float], Optional[float], float, float]:
                bid = max(token.bids) if token.bids else None
                ask = min(token.asks) if token.asks else None
                return bid, ask, float(token.bids.get(bid, 0.0)), float(token.asks.get(ask, 0.0))

            for direction, token, ready in (("Up", market.up, up_ready),
                                            ("Down", market.down, down_ready)):
                key = (market.market_id, direction)
                signature = (ready, top(token))
                if signature != book_signatures.get(key):
                    last_change_ms[key] = float(event["recv_ms"])
                    book_signatures[key] = signature
            histories[market.market_id].push(_BookState(
                float(event["recv_ms"]), active_epoch, up_ready, down_ready,
                market.up.source_ms, market.down.source_ms,
                last_change_ms[(market.market_id, "Up")], last_change_ms[(market.market_id, "Down")],
                up_bids, down_bids,
                up_asks, down_asks
            ))
    finish_receive_group()
    while pending:
        _, _, candidate, latency, epoch = heapq.heappop(pending)
        evaluate(candidate, latency, epoch, "book_horizon_incomplete")
    return rows, counters


def _aggregate_spot_path(archive_dir: str | Path) -> Optional[Path]:
    root = Path(archive_dir)
    candidates = (root / "latency" / "binance_trades.jsonl.gz",
                  root.parent / "latency" / "binance_trades.jsonl.gz")
    return next((candidate for candidate in candidates if candidate.exists()), None)


def iter_aggregate_spot_events(archive_dir: str | Path,
                               profile: Optional[dict[str, Any]] = None) -> Iterable[dict[str, Any]]:
    """Yield the aggregate-trade tape used by frozen MIX, preserving its receipt order."""
    path = _aggregate_spot_path(archive_dir)
    if path is None:
        raise FileNotFoundError(f"MIX aggregate trade tape missing under {Path(archive_dir)}")
    previous_receive = -math.inf
    first_receive: Optional[float] = None
    segment_start: Optional[float] = None
    max_gap = 0.0
    longest = 0.0
    records = 0
    sequence = 0
    opened = False
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            row = json.loads(line)
            if row.get("event") != "BINANCE_WS_TRADE":
                continue
            try:
                receive_ms = float(row["receive_ts"]) * 1_000.0
                source_ms = float(row["trade_ts"]) * 1_000.0
                price = float(row["price"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"malformed MIX aggregate trade at {path}:{line_number}") from exc
            if receive_ms < previous_receive:
                raise ValueError(f"MIX aggregate trade receipt time regressed at {path}:{line_number}")
            if not (math.isfinite(source_ms) and math.isfinite(receive_ms) and math.isfinite(price) and price > 0):
                raise ValueError(f"invalid MIX aggregate trade at {path}:{line_number}")
            if not opened:
                yield {"kind": "spot_connection", "seq": sequence, "recv_ms": receive_ms,
                       "source_ts_ms": None, "stream": "mix_aggregate"}
                sequence += 1
                opened = True
            if first_receive is None:
                first_receive = receive_ms
                segment_start = receive_ms
            elif math.isfinite(previous_receive):
                gap = receive_ms - previous_receive
                max_gap = max(max_gap, gap)
                if gap > mix.GAP_FILL_S * 1_000.0 + 1e-6:
                    longest = max(longest, previous_receive - float(segment_start))
                    segment_start = receive_ms
            yield {"kind": "spot_trade", "seq": sequence, "recv_ms": receive_ms,
                   "source_ts_ms": source_ms, "price": price, "size": 0.0,
                   "stream": "mix_aggregate"}
            sequence += 1
            previous_receive = receive_ms
            records += 1
    if not opened:
        raise ValueError(f"MIX aggregate trade tape is empty: {path}")
    if segment_start is not None:
        longest = max(longest, previous_receive - segment_start)
    if profile is not None:
        profile.clear()
        profile.update({
            "records": records,
            "first_receive_ms": first_receive,
            "last_receive_ms": previous_receive,
            "duration_s": (previous_receive - float(first_receive)) / 1_000.0,
            "max_gap_ms": max_gap,
            "longest_contiguous_s": longest / 1_000.0,
        })
    yield {"kind": "spot_disconnect", "seq": sequence, "recv_ms": previous_receive + 1e-3,
           "source_ts_ms": None, "stream": "mix_aggregate"}


def iter_mix_source_events(archive_dir: str | Path, standard_dir: str | Path,
                           profile: Optional[dict[str, Any]] = None) -> Iterable[dict[str, Any]]:
    """Merge frozen aggregate spot with only the causal DVOL lifecycle from the strict source tape."""
    spot = iter_aggregate_spot_events(archive_dir, profile=profile)
    standard_source = archive.iter_normalized_events(
        Path(standard_dir) / "source_events.jsonl.gz", family="source"
    )
    dvol_kinds = {"deribit_connection", "deribit_dvol", "deribit_disconnect"}
    dvol = (event for event in standard_source if str(event["kind"]) in dvol_kinds)
    decorated_spot = ((float(event["recv_ms"]), 0, index, event)
                      for index, event in enumerate(spot))
    decorated_dvol = ((float(event["recv_ms"]), 1, index, event)
                      for index, event in enumerate(dvol))
    for _, _, _, event in heapq.merge(decorated_spot, decorated_dvol, key=lambda item: item[:3]):
        yield event


def exact_pvalue(rows: list[dict[str, Any]]) -> float:
    if not rows:
        return 1.0
    probabilities = [min(max(float(row["all_in_cost"]), 0.0), 1.0) for row in rows]
    observed = sum(bool(row["won"]) for row in rows)
    pmf = [0.0] * (len(probabilities) + 1)
    pmf[0] = 1.0
    for index, probability in enumerate(probabilities):
        for wins in range(index + 1, 0, -1):
            pmf[wins] = pmf[wins] * (1.0 - probability) + pmf[wins - 1] * probability
        pmf[0] *= 1.0 - probability
    return min(max(sum(pmf[observed:]), 0.0), 1.0)


def daily_lower_99(rows: list[dict[str, Any]]) -> Optional[float]:
    from scipy.stats import t as student_t

    by_day: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_day.setdefault(str(row["day"]), []).append(row)
    if len(by_day) < 2:
        return None
    clusters = [
        (sum(float(row["pnl"]) for row in day_rows),
         sum(float(row["filled_shares"]) for row in day_rows))
        for day_rows in by_day.values()
    ]
    total_pnl = sum(pnl for pnl, _ in clusters)
    total_shares = sum(shares for _, shares in clusters)
    if total_shares <= 0:
        return None
    estimate = total_pnl / total_shares
    influence = [pnl - estimate * shares for pnl, shares in clusters]
    count = len(clusters)
    standard_error = math.sqrt(count / (count - 1) * sum(value * value for value in influence)) / total_shares
    return estimate - float(student_t.ppf(0.99, count - 1)) * standard_error


def primary_market_fills(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(
        (row for row in rows if float(row["evaluation_ms"]) == PRIMARY_LATENCY_MS and row.get("filled")),
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


def stopping_sample(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fills = primary_market_fills(rows)
    days = set()
    for index, row in enumerate(fills):
        days.add(str(row["day"]))
        if index + 1 >= MIN_FILLS and len(days) >= MIN_DAYS:
            return fills[:index + 1]
    return []


def verdict(rows: list[dict[str, Any]]) -> dict[str, Any]:
    current = primary_market_fills(rows)
    sample = stopping_sample(rows)
    if not sample or any(row.get("winner") is None for row in sample):
        progress = sample or current
        collecting = {"status": "collecting", "fills": len(progress),
                      "days": len({str(row["day"]) for row in progress}),
                      "required_fills": MIN_FILLS, "required_days": MIN_DAYS}
        if sample:
            collecting["pending_outcomes"] = sum(row.get("winner") is None for row in sample)
        return collecting
    shares = sum(float(row["filled_shares"]) for row in sample)
    net_ev = sum(float(row["pnl"]) for row in sample) / shares
    lower = daily_lower_99(sample)
    raw_p = exact_pvalue(sample) if net_ev > 0 else 1.0
    corrected_p = min(1.0, raw_p * FAMILY_TESTS)
    passed = net_ev > 0 and lower is not None and lower > 0 and corrected_p < ALPHA
    return {"status": "passed" if passed else "rejected", "fills": len(sample),
            "days": len({str(row["day"]) for row in sample}), "net_ev_per_share": net_ev,
            "day_cluster_lower_99": lower, "raw_exact_p": raw_p, "corrected_exact_p": corrected_p,
            "family_tests": FAMILY_TESTS, "last_signal_ms": max(float(row["signal_receive_ms"]) for row in sample)}


def _read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def observation_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (str(row.get("market_id")), str(row.get("kind")), float(row["signal_source_ms"]),
            str(row["direction"]), float(row["evaluation_ms"]))


_OUTCOME_FIELD_NAMES = ("winner", "won", "pnl_per_share", "pnl")
_OUTCOME_FIELDS = frozenset(_OUTCOME_FIELD_NAMES)


def _execution_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key not in _OUTCOME_FIELDS}


def _outcome_values(row: Mapping[str, Any]) -> tuple[Any, ...]:
    values = tuple(row.get(field) for field in _OUTCOME_FIELD_NAMES)
    if any(value is None for value in values) and not all(value is None for value in values):
        raise ValueError("MIX observation has a partial outcome")
    if not row.get("filled") and any(value is not None for value in values):
        raise ValueError("unfilled MIX observation carries an outcome")
    return values


def _merge_observation(previous: dict[str, Any], row: dict[str, Any], key: tuple[Any, ...]) -> dict[str, Any]:
    if _execution_fields(previous) != _execution_fields(row):
        raise ValueError(f"execution-field conflict for MIX observation {key!r}")
    previous_outcome = _outcome_values(previous)
    new_outcome = _outcome_values(row)
    if previous_outcome == new_outcome:
        return previous
    if all(value is None for value in previous_outcome) and all(value is not None for value in new_outcome):
        return {**previous, **{field: row.get(field) for field in _OUTCOME_FIELD_NAMES}}
    raise ValueError(f"outcome conflict for MIX observation {key!r}")


def _ledger_sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (float(row["signal_receive_ms"]), str(row.get("market_id")),
            float(row["evaluation_ms"]), str(row["direction"]),
            str(row.get("kind")), float(row["signal_source_ms"]))


def _jsonl_bytes(rows: Iterable[Mapping[str, Any]]) -> bytes:
    return "".join(json.dumps(row, sort_keys=True, allow_nan=False) + "\n" for row in rows).encode()


def _jsonl_sha256(rows: Iterable[Mapping[str, Any]]) -> str:
    return hashlib.sha256(_jsonl_bytes(rows)).hexdigest()


def append_observations(destination: str | Path, new_rows: str | Path,
                        verdict_path: str | Path | None = None) -> list[dict[str, Any]]:
    """Append once: an already-recorded market/signal/latency can never be replaced by a rerun."""
    destination = Path(destination)
    pooled: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in [*_read_jsonl(destination), *_read_jsonl(new_rows)]:
        key = observation_key(row)
        _outcome_values(row)
        previous = pooled.get(key)
        pooled[key] = row if previous is None else _merge_observation(previous, row, key)
    rows = sorted(pooled.values(), key=_ledger_sort_key)
    if verdict_path is not None and Path(verdict_path).exists():
        pinned = json.loads(Path(verdict_path).read_text(encoding="utf-8"))
        pinned_keys = pinned.get("sample_keys")
        if not isinstance(pinned_keys, list) or not pinned_keys:
            raise ValueError("pinned MIX verdict is missing its stopping prefix")
        current_sample = stopping_sample(rows)
        current_keys = [list(observation_key(row)) for row in current_sample]
        if current_keys != pinned_keys:
            raise ValueError("MIX observation would change the pinned stopping prefix")
        if pinned.get("last_key") != current_keys[-1]:
            raise ValueError("pinned MIX verdict has an invalid last key")
        if pinned.get("sample_sha256") != _jsonl_sha256(current_sample):
            raise ValueError("MIX observation would change the pinned sample SHA")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
    os.replace(temporary, destination)
    return rows


def pin_verdict(rows: list[dict[str, Any]], path: str | Path, freeze: Mapping[str, Any],
                ledger_path: str | Path | None = None) -> dict[str, Any]:
    destination = Path(path)
    sample = stopping_sample(rows)
    sample_keys = [list(observation_key(row)) for row in sample]
    if destination.exists():
        pinned = json.loads(destination.read_text(encoding="utf-8"))
        if pinned.get("strategy_id") != STRATEGY_ID or pinned.get("protocol_sha256") != freeze["protocol_sha256"]:
            raise ValueError("pinned MIX verdict belongs to another protocol")
        if (not sample_keys or pinned.get("sample_keys") != sample_keys or
                pinned.get("last_key") != sample_keys[-1]):
            raise ValueError("current MIX ledger changes the pinned stopping prefix")
        if pinned.get("sample_sha256") != _jsonl_sha256(sample):
            raise ValueError("current MIX ledger changes the pinned sample SHA")
        return pinned
    result = verdict(rows)
    if result["status"] == "collecting":
        return result
    ordered_rows = sorted(rows, key=_ledger_sort_key)
    if ledger_path is not None:
        ledger_path = Path(ledger_path)
        if _read_jsonl(ledger_path) != ordered_rows:
            raise ValueError("MIX verdict rows do not match the persisted ledger")
        ledger_sha256 = sha256_file(ledger_path)
    else:
        ledger_sha256 = _jsonl_sha256(ordered_rows)
    record = {**result, "strategy_id": STRATEGY_ID, "protocol_sha256": freeze["protocol_sha256"],
              "sample_keys": sample_keys,
              "sample_sha256": _jsonl_sha256(sample),
              "ledger_sha256": ledger_sha256, "last_key": sample_keys[-1],
              "pinned_at": datetime.now(timezone.utc).isoformat()}
    destination.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json(destination, record)
    return record


def summarize(rows: list[dict[str, Any]], counters: Counter) -> dict[str, Any]:
    result: dict[str, Any] = {"counters": dict(counters), "latencies": {}}
    for latency in sorted({float(row["evaluation_ms"]) for row in rows}):
        sample = [row for row in rows if float(row["evaluation_ms"]) == latency]
        fills = [row for row in sample if row["filled"] and row["winner"] is not None]
        shares = sum(float(row["filled_shares"]) for row in fills)
        pnl = sum(float(row["pnl"]) for row in fills)
        mean = pnl / shares if shares else None
        result["latencies"][str(int(latency))] = {
            "signals": len(sample),
            "fills": len(fills),
            "fill_rate": len(fills) / len(sample) if sample else 0.0,
            "net_ev_per_share": mean,
            "pnl": pnl,
            "days": len({row["day"] for row in fills}),
            "day_cluster_lower_99": daily_lower_99(fills),
            "exact_p": exact_pvalue(fills) if mean is not None and mean > 0 else 1.0,
            "reasons": dict(Counter(str(row["reason"]) for row in sample)),
        }
    return result


def replay_archive(archive_dir: str | Path, config: ControlConfig | None = None,
                   rows_out: str | Path | None = None,
                   source_protocol_sha256: str | None = None) -> dict[str, Any]:
    archive_dir = Path(archive_dir)
    standard = archive_dir / "strict" if (archive_dir / "strict" / "manifest.json").exists() else archive_dir
    checked = archive.validate_standard_artifact(standard)
    mappings = list(archive.iter_market_mappings(standard / "market_registry.csv.gz"))
    outcomes = {row["market_id"]: row["winner"] for row in archive.iter_outcomes(standard / "market_outcomes.csv.gz")}
    signal_tape_profile: dict[str, Any] = {}
    source = iter_mix_source_events(archive_dir, standard, profile=signal_tape_profile)
    clob = archive.iter_normalized_events(standard / "clob_events.jsonl.gz", family="clob")
    rows, counters = replay_normalized(source, clob, mappings, outcomes, config)
    if source_protocol_sha256 is not None:
        if (len(source_protocol_sha256) != 64
                or any(char not in "0123456789abcdef" for char in source_protocol_sha256)):
            raise ValueError("invalid MIX source protocol fingerprint")
        for row in rows:
            row["source_strategy_id"] = STRATEGY_ID
            row["source_protocol_sha256"] = source_protocol_sha256
    if rows_out is not None:
        destination = Path(rows_out)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("w", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
    result = summarize(rows, counters)
    result["verdict"] = verdict(rows)
    result["model_feature_audit"] = audit_model_features(
        checked["manifest"], signal_tape_profile
    )
    result["dataset"] = {"archive": archive_dir.name, "manifest": checked["manifest"],
                         "paper_gate_eligible": checked["manifest"].get("collector_region") == "eu-west-1",
                         "signal_tape": "aggregate_trade_receipt_clock",
                         "signal_tape_profile": signal_tape_profile}
    result["protocol"] = {"rule": "jump_z_settle_control", "z_cut": (config or ControlConfig()).z_cut,
                          "primary_latency_ms": PRIMARY_LATENCY_MS,
                          "book_clock": "local_receipt", "depth": "single_best_ask_at_least_five",
                          "outcome_use": "after_fill_only", "orders": "paper_only",
                          "limitation": "standard artifact has no explicit lifecycle/halt field"}
    if source_protocol_sha256 is not None:
        result["source_protocol"] = {"strategy_id": STRATEGY_ID,
                                     "protocol_sha256": source_protocol_sha256}
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", required=True)
    parser.add_argument("--rows-out")
    parser.add_argument("--freeze")
    parser.add_argument("--append")
    parser.add_argument("--verdict-out")
    args = parser.parse_args()
    if (args.append or args.verdict_out) and not args.freeze:
        parser.error("--freeze is required with --append or --verdict-out")
    if args.verdict_out and not args.append:
        parser.error("--verdict-out requires --append")
    if args.append and not args.rows_out:
        parser.error("--append requires --rows-out")
    freeze, config = load_control_freeze(args.freeze) if args.freeze else (None, ControlConfig())
    rows_out = args.rows_out
    result = replay_archive(args.archive, config=config, rows_out=rows_out,
                            source_protocol_sha256=freeze["protocol_sha256"] if freeze else None)
    if args.append:
        if result["dataset"]["paper_gate_eligible"]:
            pooled = append_observations(args.append, rows_out, args.verdict_out)
            result["pooled_verdict"] = (pin_verdict(pooled, args.verdict_out, freeze, args.append)
                                         if args.verdict_out else verdict(pooled))
        else:
            result["pooled_verdict"] = {"status": "audit_only", "reason": "collector is not eu-west-1"}
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
