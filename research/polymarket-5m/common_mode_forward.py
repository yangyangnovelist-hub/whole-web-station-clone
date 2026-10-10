"""Immutable UTC-day forward evaluator for the frozen US-017 paper strategy."""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import time
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import basis_wedge_forward as basis
import common_mode_run as replay


DAY_SCHEMA = "common-mode-residual-day-v1"
INDEX_SCHEMA = "common-mode-residual-forward-index-v1"
STATUS_SCHEMA = "common-mode-residual-forward-status-v1"
VERDICT_SCHEMA = "common-mode-residual-forward-verdict-v1"
INDEX = "day-index.json"
STATUS = "status.json"
VERDICT = "verdict.json"
DAYS_DIR = "days"
JOURNAL = "admission-journal.json"
STAGING = "pending-day.json"

VARIANTS = (
    replay.BASE_VARIANT,
    replay.DIRECTION_CONTROL,
    replay.TIME_SHIFT_CONTROL,
    replay.BENCHMARK_CONTROL,
    replay.NO_FOLLOW_CONTROL,
)
MATCHED_VARIANTS = (replay.DIRECTION_CONTROL, replay.TIME_SHIFT_CONTROL)
NEGATIVE_CONTROL_COHORTS = (replay.BENCHMARK_CONTROL, replay.NO_FOLLOW_CONTROL)

ROOT = Path(__file__).resolve().parent
PROTOCOL_FREEZE = ROOT / "forward" / "common-mode-residual-freeze.json"
EVALUATOR_FREEZE = ROOT / "forward" / "common-mode-evaluator-freeze.json"
_JOURNAL_SCHEMA = "common-mode-residual-admission-journal-v1"
_EPS = 1e-9
_SHA256 = re.compile(r"[0-9a-fA-F]{64}")
_TIME_SHIFT_MS = float(replay.execution.TIME_SHIFT_MS)

_REQUIRED_ROW_FIELDS = frozenset(
    {
        "signal_id",
        "parent_signal_id",
        "paper_only",
        "ordering_clock",
        "match_book_clock",
        "market_id",
        "variant",
        "comparison_design",
        "signal_recv_ms",
        "decision_recv_ms",
        "scheduled_decision_recv_ms",
        "decision_book_recv_ms",
        "evaluation_ms",
        "evaluation_recv_ms",
        "book_recv_ms",
        "common_direction",
        "buy_side",
        "fair",
        "decision_vwap",
        "fixed_limit",
        "signal_fee_per_share",
        "signal_net_edge",
        "target_shares",
        "filled_shares",
        "fill_vwap",
        "fill_levels",
        "fee_rate",
        "fee",
        "total_cost",
        "all_in_cost",
        "winner",
        "won",
        "pnl_per_share",
        "pnl",
        "filled",
        "full_fill",
        "sent",
        "reason",
        "signal",
    }
)

_PAIR_FIELDS = (
    "market_id",
    "parent_signal_id",
    "comparison_design",
    "signal_recv_ms",
    "decision_recv_ms",
    "scheduled_decision_recv_ms",
    "decision_book_recv_ms",
    "common_direction",
    "buy_side",
    "fair",
    "decision_vwap",
    "fixed_limit",
    "signal_fee_per_share",
    "signal_net_edge",
    "target_shares",
    "fee_rate",
    "sent",
    "signal",
)


def _sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode()


def _json_sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_json_bytes(payload)).hexdigest()


def _atomic_json(path: str | Path, payload: Mapping[str, Any]) -> None:
    destination = Path(path)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_bytes(_json_bytes(payload))
    temporary.replace(destination)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON value: {value}")


def _load_json(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        payload = json.loads(
            source.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON artifact: {source}") from exc
    if not isinstance(payload, dict):
        raise TypeError(f"expected JSON object: {source}")
    return payload


def load_freeze(
    protocol_path: str | Path = PROTOCOL_FREEZE,
) -> tuple[dict[str, Any], str]:
    """Load the checksum-bound US-017 protocol and its frozen dependencies."""
    return basis._load_hashed_json(
        Path(protocol_path),
        "common-mode-residual-protocol-v1",
    )


def validate_evaluator_freeze(
    evaluator: Mapping[str, Any],
    protocol: Mapping[str, Any],
) -> None:
    """Require the descriptive evaluator freeze to equal executable semantics."""
    execution = evaluator.get("execution")
    statistics = evaluator.get("statistics")
    if (
        evaluator.get("collector_region") != protocol.get("collector_region")
        or evaluator.get("paper_only") is not True
        or evaluator.get("variants") != list(VARIANTS)
        or not isinstance(execution, Mapping)
        or not isinstance(statistics, Mapping)
    ):
        raise ValueError("common-mode evaluator semantic drift")
    protocol_execution = protocol["execution"]
    protocol_statistics = protocol["statistics"]
    if (
        tuple(map(float, execution.get("evaluation_ms", ()))) != _latencies(protocol)
        or not basis._same_number(
            execution.get("minimum_full_fill_shares"),
            protocol_execution["minimum_full_fill_shares"],
        )
        or not basis._same_number(
            execution.get("taker_fee_rate"),
            protocol_execution["taker_fee_rate"],
        )
        or execution.get("direct_dual_token_l2") is not True
        or execution.get("fixed_limit") is not True
        or execution.get("paper_fill_is_upper_bound") is not True
        or int(statistics.get("minimum_full_fills_per_latency", -1))
        != int(protocol_statistics["minimum_full_fills_per_latency"])
        or int(statistics.get("minimum_utc_days", -1))
        != int(protocol_statistics["minimum_utc_days"])
        or int(statistics.get("family_tests", -1))
        != int(protocol_statistics["family_tests"])
        or not basis._same_number(
            statistics.get("one_sided_alpha"),
            protocol_statistics["one_sided_alpha"],
        )
        or statistics.get("common_cutoff") is not True
        or statistics.get("positive_ev_at_both_latencies") is not True
        or statistics.get("positive_one_sided_day_cluster_lower_bound") is not True
        or statistics.get("bonferroni_exact_p_below_alpha") is not True
        or statistics.get("matched_arms_must_be_complete") is not True
    ):
        raise ValueError("common-mode evaluator execution/statistics drift")


def load_freezes(
    protocol_path: str | Path = PROTOCOL_FREEZE,
    evaluator_path: str | Path = EVALUATOR_FREEZE,
) -> tuple[dict[str, Any], str, dict[str, Any], str]:
    """Load and cross-bind the protocol and executable evaluator freezes."""
    protocol, protocol_sha = load_freeze(protocol_path)
    evaluator, evaluator_sha = basis._load_hashed_json(
        Path(evaluator_path),
        "common-mode-evaluator-freeze-v1",
    )
    if (
        evaluator.get("protocol_sha256") != protocol_sha
        or evaluator.get("strategy_id") != protocol.get("strategy_id")
        or evaluator.get("holdout_start") != protocol.get("holdout_start")
    ):
        raise ValueError("common-mode evaluator/protocol identity drift")
    validate_evaluator_freeze(evaluator, protocol)
    return protocol, protocol_sha, evaluator, evaluator_sha


def _date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid UTC day: {value!r}") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"invalid UTC day: {value!r}")
    return parsed


def _holdout_day(frozen: Mapping[str, Any]) -> date:
    try:
        timestamp_day = datetime.fromtimestamp(
            basis._finite_number(
                frozen["holdout_start_ms"],
                "holdout start",
            )
            / 1_000,
            tz=timezone.utc,
        ).date()
        declared = datetime.fromisoformat(
            str(frozen["holdout_start"]).replace("Z", "+00:00")
        )
    except (KeyError, OverflowError, OSError, TypeError, ValueError) as exc:
        raise ValueError("protocol holdout start is invalid") from exc
    if (
        declared.tzinfo is None
        or declared.astimezone(timezone.utc).date() != timestamp_day
    ):
        raise ValueError("protocol holdout identity drift")
    return timestamp_day


def _latencies(frozen: Mapping[str, Any]) -> tuple[float, float]:
    try:
        values = tuple(
            basis._finite_number(value, "evaluation latency")
            for value in frozen["execution"]["evaluation_ms"]
        )
    except (KeyError, TypeError) as exc:
        raise ValueError("protocol evaluation latencies are invalid") from exc
    if len(values) != 2 or set(values) != {400.0, 500.0}:
        raise ValueError("protocol evaluation latencies drift")
    return 400.0, 500.0


def _validate_protocol(frozen: Mapping[str, Any]) -> None:
    if (
        frozen.get("schema") != "common-mode-residual-protocol-v1"
        or frozen.get("paper_only") is not True
        or not str(frozen.get("strategy_id") or "")
        or not str(frozen.get("collector_region") or "")
    ):
        raise ValueError("common-mode protocol identity drift")
    _holdout_day(frozen)
    _latencies(frozen)
    try:
        target = basis._finite_number(
            frozen["execution"]["minimum_full_fill_shares"],
            "minimum full fill",
        )
        fee_rate = basis._finite_number(
            frozen["execution"]["taker_fee_rate"],
            "taker fee rate",
        )
        statistics = frozen["statistics"]
        minimum_fills = int(statistics["minimum_full_fills_per_latency"])
        minimum_days = int(statistics["minimum_utc_days"])
        family_tests = int(statistics["family_tests"])
        alpha = basis._finite_number(statistics["one_sided_alpha"], "alpha")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("common-mode protocol statistics are invalid") from exc
    if (
        not math.isclose(target, 5.0, rel_tol=0, abs_tol=_EPS)
        or not math.isclose(fee_rate, 0.07, rel_tol=0, abs_tol=_EPS)
        or minimum_fills <= 0
        or minimum_days <= 0
        or family_tests <= 0
        or not 0 < alpha < 1
    ):
        raise ValueError("common-mode protocol execution or statistics drift")


def _resolve_freeze(
    frozen: Mapping[str, Any],
    *,
    protocol_sha256: str,
    evaluator_sha256: str,
    allow_test_freeze: bool,
) -> dict[str, Any]:
    if not isinstance(frozen, Mapping):
        raise TypeError("protocol freeze must be an object")
    supplied = dict(frozen)
    if not allow_test_freeze:
        loaded, loaded_sha, _evaluator, loaded_evaluator_sha = load_freezes()
        if supplied != loaded:
            raise ValueError("caller protocol freeze differs from the hashed freeze")
        if protocol_sha256 != loaded_sha:
            raise ValueError("caller protocol hash differs from the hashed freeze")
        if evaluator_sha256 != loaded_evaluator_sha:
            raise ValueError("caller evaluator hash differs from the hashed freeze")
        supplied = loaded
    _validate_protocol(supplied)
    return supplied


def _utc_day(timestamp_ms: float) -> str:
    return basis._utc_day(timestamp_ms)


def _canonical_value(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_value(value).encode()).hexdigest()


def _validate_input_bindings(payload: Mapping[str, Any], day: str) -> None:
    bindings = payload.get("input_bindings")
    if not isinstance(bindings, Mapping):
        raise ValueError("day input bindings are missing")
    archive_manifests = bindings.get("archive_manifests")
    artifact_binding = bindings.get("artifact_binding")
    raw_binding = bindings.get("raw_sidecar_binding")
    if not all(
        isinstance(value, Mapping)
        for value in (archive_manifests, artifact_binding, raw_binding)
    ):
        raise ValueError("day input binding payload is invalid")
    assert isinstance(archive_manifests, Mapping)
    assert isinstance(artifact_binding, Mapping)
    assert isinstance(raw_binding, Mapping)

    archive_sha = str(payload.get("archive_manifests_sha256") or "")
    artifact_sha = str(payload.get("artifact_binding_sha256") or "")
    raw_sha = str(payload.get("raw_sidecar_identity_sha256") or "")
    strict_sha = str(payload.get("strict_manifest_sha256") or "")
    for label, value in (
        ("archive manifest", archive_sha),
        ("artifact binding", artifact_sha),
        ("raw sidecar", raw_sha),
        ("strict manifest", strict_sha),
    ):
        if _SHA256.fullmatch(value) is None:
            raise ValueError(f"day {label} identity is invalid")
    if _json_sha256(archive_manifests) != archive_sha:
        raise ValueError("day archive manifest binding drift")
    if _json_sha256(artifact_binding) != artifact_sha:
        raise ValueError("day strict artifact binding drift")

    compact_day = day.replace("-", "")
    rec = archive_manifests.get("rec")
    poly = archive_manifests.get("poly")
    if not (
        isinstance(rec, Mapping)
        and isinstance(poly, Mapping)
        and rec.get("day") == compact_day
        and rec.get("group") == "rec"
        and poly.get("day") == compact_day
        and poly.get("group") == "poly"
        and isinstance(rec.get("files"), list)
        and isinstance(poly.get("files"), list)
    ):
        raise ValueError("day archive manifest identity drift")
    rec_files = {
        str(item.get("file")): item
        for item in rec["files"]
        if isinstance(item, Mapping)
    }
    if len(rec_files) != len(rec["files"]):
        raise ValueError("day rec archive contains duplicate or invalid files")

    raw_files = raw_binding.get("files")
    if (
        raw_binding.get("schema") != "common-mode-raw-sidecars-v1"
        or raw_binding.get("day") != compact_day
        or raw_binding.get("protocol_sha256") != payload.get("protocol_sha256")
        or raw_binding.get("evaluator_sha256") != payload.get("evaluator_sha256")
        or raw_binding.get("archive_manifests_sha256") != archive_sha
        or raw_binding.get("raw_sidecar_identity_sha256") != raw_sha
        or raw_binding.get("strict_manifest_sha256") != strict_sha
        or not isinstance(raw_files, list)
    ):
        raise ValueError("day raw sidecar binding drift")
    expected_names = [
        f"{route}.{compact_day}T{hour:02d}.txt.gz"
        for route in ("bn_spot", "deribit")
        for hour in range(24)
    ]
    names = [str(item.get("file")) for item in raw_files if isinstance(item, Mapping)]
    if names != expected_names:
        raise ValueError("day raw sidecar hour coverage drift")
    identity_files: list[dict[str, Any]] = []
    for item in raw_files:
        assert isinstance(item, Mapping)
        name = str(item["file"])
        size = int(item.get("size") or 0)
        digest = str(item.get("sha256") or "")
        archived = rec_files.get(name)
        if (
            size <= 0
            or _SHA256.fullmatch(digest) is None
            or not isinstance(archived, Mapping)
            or int(archived.get("size") or 0) != size
            or archived.get("sha256") != digest
            or archived.get("uploaded_ok") is not True
            or archived.get("gzip_ok") is not True
        ):
            raise ValueError("day raw sidecar differs from its rec manifest")
        identity_files.append({"file": name, "size": size, "sha256": digest})
    expected_raw_sha = _canonical_sha256({
        "schema": "common-mode-raw-sidecars-v1",
        "day": compact_day,
        "files": identity_files,
    })
    if expected_raw_sha != raw_sha:
        raise ValueError("day raw sidecar identity drift")

    raw_archive_sha = _canonical_sha256({"rec": rec, "poly": poly})
    binding_schema = artifact_binding.get("schema")
    if (
        binding_schema not in {
            "eu-strict-input-binding-v1",
            "current-h-source-race-input-v1",
        }
        or artifact_binding.get("day") != compact_day
        or artifact_binding.get("strict_manifest_sha256") != strict_sha
        or (
            binding_schema == "eu-strict-input-binding-v1"
            and artifact_binding.get("raw_archive_manifests_sha256")
            != raw_archive_sha
        )
        or (
            binding_schema != "eu-strict-input-binding-v1"
            and (
                artifact_binding.get("archive_manifests_sha256") != archive_sha
                or artifact_binding.get("raw_archive_manifests_sha256")
                != raw_archive_sha
            )
        )
    ):
        raise ValueError("day strict artifact input binding drift")


def _validate_row(
    row: Mapping[str, Any],
    *,
    day: str,
    frozen: Mapping[str, Any],
    index: int,
) -> None:
    prefix = f"row {index}"
    missing = _REQUIRED_ROW_FIELDS - row.keys()
    if missing:
        raise ValueError(f"{prefix} is missing fields: {', '.join(sorted(missing))}")
    variant = str(row["variant"])
    if variant not in VARIANTS:
        raise ValueError(f"{prefix} has an unfrozen variant")
    if row["paper_only"] is not True:
        raise ValueError(f"{prefix} is not paper-only")
    if (
        row["ordering_clock"] != "local_receipt_ms"
        or row["match_book_clock"] != "local_receipt_ms"
    ):
        raise ValueError(f"{prefix} clock drift")

    expected_design = (
        "matched_base_arm"
        if variant in MATCHED_VARIANTS
        else "disjoint_negative_control_cohort"
        if variant in NEGATIVE_CONTROL_COHORTS
        else "base"
    )
    if row["comparison_design"] != expected_design:
        raise ValueError(f"{prefix} comparison design drift")
    parent = row["parent_signal_id"]
    if variant in MATCHED_VARIANTS:
        if not isinstance(parent, str) or not parent:
            raise ValueError(f"{prefix} matched arm has no parent signal")
    elif parent is not None:
        raise ValueError(f"{prefix} independent cohort unexpectedly has a parent")

    signal_id = row["signal_id"]
    market_id = row["market_id"]
    if not isinstance(signal_id, str) or not signal_id:
        raise ValueError(f"{prefix} signal identity is missing")
    if not isinstance(market_id, str) or not market_id:
        raise ValueError(f"{prefix} market identity is missing")
    for name in ("sent", "filled", "full_fill"):
        if not isinstance(row[name], bool):
            raise TypeError(f"{prefix} {name} flag is invalid")
    if not isinstance(row["reason"], str) or not row["reason"]:
        raise ValueError(f"{prefix} execution reason is missing")

    signal_ms = basis._finite_number(row["signal_recv_ms"], f"{prefix} signal time")
    decision_ms = basis._finite_number(
        row["decision_recv_ms"],
        f"{prefix} decision time",
    )
    evaluation_ms = basis._finite_number(
        row["evaluation_ms"],
        f"{prefix} evaluation",
    )
    evaluation_recv_ms = basis._finite_number(
        row["evaluation_recv_ms"],
        f"{prefix} evaluation time",
    )
    if signal_ms > decision_ms + _EPS:
        raise ValueError(f"{prefix} decision predates its signal")
    if decision_ms < float(frozen["holdout_start_ms"]):
        raise ValueError(f"{prefix} is before the frozen cutoff")
    if _utc_day(decision_ms) != day:
        raise ValueError(f"{prefix} receipt day mismatch")
    if evaluation_ms not in set(_latencies(frozen)) or not math.isclose(
        evaluation_recv_ms,
        decision_ms + evaluation_ms,
        rel_tol=0,
        abs_tol=1e-6,
    ):
        raise ValueError(f"{prefix} evaluation timing drift")

    scheduled_ms = basis._finite_number(
        row["scheduled_decision_recv_ms"],
        f"{prefix} scheduled decision time",
    )
    if variant == replay.TIME_SHIFT_CONTROL:
        expected_shift = signal_ms + _TIME_SHIFT_MS
        if not math.isclose(scheduled_ms, expected_shift, rel_tol=0, abs_tol=1e-6):
            raise ValueError(f"{prefix} shifted decision timing drift")
        censored = (
            row["sent"] is False and row["reason"] == "censored_before_shifted_decision"
        )
        expected_decision = signal_ms if censored else scheduled_ms
        if not math.isclose(decision_ms, expected_decision, rel_tol=0, abs_tol=1e-6):
            raise ValueError(f"{prefix} shifted decision timing drift")
    elif not math.isclose(scheduled_ms, decision_ms, rel_tol=0, abs_tol=1e-6):
        raise ValueError(f"{prefix} scheduled decision timing drift")

    decision_book_ms = row["decision_book_recv_ms"]
    if (
        decision_book_ms is not None
        and basis._finite_number(
            decision_book_ms,
            f"{prefix} decision book time",
        )
        > decision_ms + _EPS
    ):
        raise ValueError(f"{prefix} uses a future decision book")
    book_recv_ms = row["book_recv_ms"]
    if (
        book_recv_ms is not None
        and basis._finite_number(
            book_recv_ms,
            f"{prefix} book time",
        )
        > evaluation_recv_ms + _EPS
    ):
        raise ValueError(f"{prefix} uses a future book")

    common_direction = row["common_direction"]
    buy_side = row["buy_side"]
    if common_direction not in {"up", "down"} or buy_side not in {"up", "down"}:
        raise ValueError(f"{prefix} direction is invalid")
    if variant == replay.DIRECTION_CONTROL:
        if buy_side != common_direction:
            raise ValueError(f"{prefix} direction-reversal side drift")
    elif buy_side == common_direction:
        raise ValueError(f"{prefix} residual-fade side drift")

    fair = basis._finite_number(row["fair"], f"{prefix} fair")
    decision_vwap = basis._finite_number(
        row["decision_vwap"],
        f"{prefix} decision VWAP",
    )
    fixed_limit = basis._finite_number(row["fixed_limit"], f"{prefix} fixed limit")
    basis._finite_number(row["signal_fee_per_share"], f"{prefix} signal fee")
    basis._finite_number(row["signal_net_edge"], f"{prefix} signal edge")
    if not 0 <= fair <= 1 or not 0 <= decision_vwap <= 1 or not 0 <= fixed_limit < 1:
        raise ValueError(f"{prefix} frozen prices are impossible")

    signal = row["signal"]
    if not isinstance(signal, Mapping):
        raise TypeError(f"{prefix} immutable signal payload is missing")
    signal_fields = {
        "market_id": market_id,
        "recv_ms": signal_ms,
        "common_direction": common_direction,
        "buy_side": buy_side,
        "fair": fair,
        "ask": decision_vwap,
        "fixed_limit": fixed_limit,
        "fee": row["signal_fee_per_share"],
        "net_edge": row["signal_net_edge"],
    }
    for name, expected_value in signal_fields.items():
        actual = signal.get(name)
        if isinstance(expected_value, (int, float)):
            matches = basis._same_number(actual, expected_value)
        else:
            matches = actual == expected_value
        if not matches:
            raise ValueError(f"{prefix} {name} differs from the immutable signal")
    signal_fee = basis._finite_number(
        row["signal_fee_per_share"],
        f"{prefix} signal fee",
    )
    signal_edge = basis._finite_number(
        row["signal_net_edge"],
        f"{prefix} signal edge",
    )
    signal_depth = basis._finite_number(
        signal.get("direct_depth"),
        f"{prefix} signal direct depth",
    )
    if (
        signal_fee < 0
        or fixed_limit + _EPS < decision_vwap
        or not math.isclose(
            signal_edge,
            fair - decision_vwap - signal_fee,
            rel_tol=0,
            abs_tol=1e-9,
        )
        or (
            variant != replay.DIRECTION_CONTROL
            and (
                signal_edge + _EPS
                < float(frozen["execution"]["minimum_net_edge_per_share"])
                or signal_depth + _EPS
                < float(frozen["execution"]["minimum_full_fill_shares"])
            )
        )
    ):
        raise ValueError(f"{prefix} frozen signal economics drift")

    shares = basis._finite_number(row["filled_shares"], f"{prefix} filled shares")
    target = basis._finite_number(row["target_shares"], f"{prefix} target shares")
    fee_rate = basis._finite_number(row["fee_rate"], f"{prefix} fee rate")
    frozen_target = float(frozen["execution"]["minimum_full_fill_shares"])
    frozen_fee_rate = float(frozen["execution"]["taker_fee_rate"])
    if not math.isclose(target, frozen_target, rel_tol=0, abs_tol=_EPS):
        raise ValueError(f"{prefix} target shares drift")
    if not math.isclose(fee_rate, frozen_fee_rate, rel_tol=0, abs_tol=_EPS):
        raise ValueError(f"{prefix} fee rate drift")
    if shares < 0:
        raise ValueError(f"{prefix} filled shares are impossible")
    levels_payload = row["fill_levels"]
    if not isinstance(levels_payload, list):
        raise TypeError(f"{prefix} fill levels are missing")

    if row["filled"] is False:
        economics = (
            "fill_vwap",
            "fee",
            "total_cost",
            "all_in_cost",
            "pnl_per_share",
            "pnl",
            "winner",
            "won",
        )
        if (
            shares > _EPS
            or levels_payload
            or row["full_fill"]
            or any(row[name] is not None for name in economics)
        ):
            raise ValueError(f"{prefix} unfilled economics are inconsistent")
        return

    if row["sent"] is not True or not _EPS < shares <= target + _EPS:
        raise ValueError(f"{prefix} filled quantity is impossible")
    expected_full = shares + _EPS >= target
    if row["full_fill"] is not expected_full or row["reason"] != (
        "filled" if expected_full else "partial_fill"
    ):
        raise ValueError(f"{prefix} full/partial status is inconsistent")
    if row["winner"] not in {"Up", "Down"} or not isinstance(row["won"], bool):
        raise ValueError(f"{prefix} has an unresolved fill")
    won = str(row["winner"]).lower() == buy_side
    if row["won"] is not won:
        raise ValueError(f"{prefix} winner identity is inconsistent")

    fill_vwap = basis._finite_number(row["fill_vwap"], f"{prefix} fill VWAP")
    fee = basis._finite_number(row["fee"], f"{prefix} fee")
    total_cost = basis._finite_number(row["total_cost"], f"{prefix} total cost")
    all_in_cost = basis._finite_number(row["all_in_cost"], f"{prefix} all-in cost")
    pnl_per_share = basis._finite_number(
        row["pnl_per_share"],
        f"{prefix} PnL per share",
    )
    pnl = basis._finite_number(row["pnl"], f"{prefix} PnL")
    if not 0 < fill_vwap < 1 or not 0 < all_in_cost < 1 or fee < 0:
        raise ValueError(f"{prefix} fill economics are impossible")

    levels: list[tuple[float, float]] = []
    for level in levels_payload:
        if not isinstance(level, Mapping):
            raise TypeError(f"{prefix} fill levels are invalid")
        price = basis._finite_number(level.get("price"), f"{prefix} fill level price")
        level_shares = basis._finite_number(
            level.get("shares"),
            f"{prefix} fill level shares",
        )
        if not 0 < price <= fixed_limit + _EPS or level_shares <= 0:
            raise ValueError(f"{prefix} fill levels violate the frozen limit")
        levels.append((price, level_shares))
    level_shares = sum(size for _, size in levels)
    notional = sum(price * size for price, size in levels)
    expected_fee = basis._rounded_fee(levels, fee_rate)
    if (
        not levels
        or not math.isclose(level_shares, shares, rel_tol=0, abs_tol=1e-8)
        or not math.isclose(fill_vwap, notional / shares, rel_tol=0, abs_tol=1e-9)
    ):
        raise ValueError(f"{prefix} fill levels do not reproduce the fill VWAP")
    if not math.isclose(fee, expected_fee, rel_tol=0, abs_tol=5e-9):
        raise ValueError(f"{prefix} fee does not match the frozen formula")
    expected_total_cost = notional + expected_fee
    expected_pnl = (shares if won else 0.0) - expected_total_cost
    if not math.isclose(total_cost, expected_total_cost, rel_tol=0, abs_tol=1e-8):
        raise ValueError(f"{prefix} total cost is inconsistent")
    if (
        not math.isclose(
            all_in_cost,
            expected_total_cost / shares,
            rel_tol=0,
            abs_tol=1e-9,
        )
        or not math.isclose(
            pnl_per_share,
            expected_pnl / shares,
            rel_tol=0,
            abs_tol=1e-9,
        )
        or not math.isclose(pnl, expected_pnl, rel_tol=0, abs_tol=1e-8)
    ):
        raise ValueError(f"{prefix} fill PnL is inconsistent")


def _group_signals(
    rows: Iterable[dict[str, Any]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (str(row["variant"]), str(row["signal_id"]))
        grouped.setdefault(key, []).append(row)
    return grouped


def _validate_signal_pairs(
    rows: Iterable[dict[str, Any]],
    frozen: Mapping[str, Any],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    grouped = _group_signals(rows)
    expected = set(_latencies(frozen))
    for signal_rows in grouped.values():
        evaluations = [float(row["evaluation_ms"]) for row in signal_rows]
        if len(evaluations) != len(set(evaluations)):
            raise ValueError("duplicate signal evaluation")
        if set(evaluations) != expected:
            raise ValueError("signal is missing a frozen evaluation latency")
        ordered = sorted(signal_rows, key=lambda row: float(row["evaluation_ms"]))
        canonical = ordered[0]
        for row in ordered[1:]:
            if any(
                _canonical_value(row[field]) != _canonical_value(canonical[field])
                for field in _PAIR_FIELDS
            ):
                raise ValueError("paired decision fields differ across latencies")
    return grouped


def _validate_relationships(
    rows: Iterable[dict[str, Any]],
) -> tuple[
    dict[str, list[dict[str, Any]]],
    dict[tuple[str, str], list[list[dict[str, Any]]]],
]:
    retained = list(rows)
    grouped = _group_signals(retained)
    base = {
        signal_id: signal_rows
        for (variant, signal_id), signal_rows in grouped.items()
        if variant == replay.BASE_VARIANT
    }
    children: dict[tuple[str, str], list[list[dict[str, Any]]]] = {}
    for (variant, _signal_id), signal_rows in grouped.items():
        if variant not in MATCHED_VARIANTS:
            continue
        representative = signal_rows[0]
        parent_id = str(representative["parent_signal_id"])
        parent_rows = base.get(parent_id)
        if parent_rows is None:
            raise ValueError("matched arm has an orphan parent signal")
        children.setdefault((variant, parent_id), []).append(signal_rows)
        parent = parent_rows[0]
        if (
            representative["market_id"] != parent["market_id"]
            or not basis._same_number(
                representative["signal_recv_ms"],
                parent["signal_recv_ms"],
            )
            or representative["common_direction"] != parent["common_direction"]
        ):
            raise ValueError("matched arm identity differs from its base signal")
        if variant == replay.DIRECTION_CONTROL:
            if (
                representative["buy_side"] == parent["buy_side"]
                or representative["buy_side"] != parent["common_direction"]
                or not basis._same_number(
                    representative["fair"],
                    1.0 - float(parent["fair"]),
                )
                or not basis._same_number(
                    representative["decision_recv_ms"],
                    parent["decision_recv_ms"],
                )
            ):
                raise ValueError("direction-reversal matched arm semantics drift")
        elif (
            representative["buy_side"] != parent["buy_side"]
            or not basis._same_number(representative["fair"], parent["fair"])
            or not basis._same_number(
                representative["fixed_limit"],
                parent["fixed_limit"],
            )
            or not basis._same_number(
                representative["scheduled_decision_recv_ms"],
                float(parent["decision_recv_ms"]) + _TIME_SHIFT_MS,
                tolerance=1e-6,
            )
        ):
            raise ValueError("time-shift matched arm semantics drift")
    for child_groups in children.values():
        if len(child_groups) != 1:
            raise ValueError("base signal has duplicate matched arms")
    return base, children


def _matched_coverage(
    rows: Iterable[dict[str, Any]],
    *,
    cutoff_ms: float | None = None,
) -> tuple[dict[str, Any], set[str]]:
    base, children = _validate_relationships(rows)
    selected = {
        signal_id
        for signal_id, signal_rows in base.items()
        if cutoff_ms is None
        or float(signal_rows[0]["decision_recv_ms"]) <= cutoff_ms + _EPS
    }
    missing = {variant: 0 for variant in MATCHED_VARIANTS}
    censored = {variant: 0 for variant in MATCHED_VARIANTS}
    complete_parents = 0
    for parent_id in selected:
        parent_complete = True
        for variant in MATCHED_VARIANTS:
            matches = children.get((variant, parent_id), [])
            if len(matches) != 1:
                missing[variant] += 1
                parent_complete = False
                continue
            if any(str(row["reason"]).startswith("censored_") for row in matches[0]):
                censored[variant] += 1
                parent_complete = False
        complete_parents += int(parent_complete)
    coverage = {
        "complete": complete_parents == len(selected),
        "base_parents": len(selected),
        "complete_parents": complete_parents,
        "missing_by_variant": missing,
        "censored_by_variant": censored,
    }
    return coverage, selected


def validate_day_result(
    payload: Mapping[str, Any],
    *,
    frozen: Mapping[str, Any],
    protocol_sha256: str,
    evaluator_sha256: str,
    now_ms: float,
) -> str:
    """Validate one complete, closed UTC artifact without trusting its economics."""
    if payload.get("schema") != DAY_SCHEMA or payload.get("complete") is not True:
        raise ValueError("day schema drift")
    day = str(payload.get("day") or "")
    day_value = _date(day)
    if day_value < _holdout_day(frozen):
        raise ValueError("day is before the frozen cutoff")
    if payload.get("collector_region") != frozen["collector_region"]:
        raise ValueError("day collector region drift")
    if payload.get("protocol_sha256") != protocol_sha256:
        raise ValueError("day protocol hash drift")
    if payload.get("evaluator_sha256") != evaluator_sha256:
        raise ValueError("day evaluator hash drift")
    _validate_input_bindings(payload, day)
    next_midnight_ms = (
        datetime.combine(
            day_value + timedelta(days=1),
            datetime.min.time(),
            tzinfo=timezone.utc,
        ).timestamp()
        * 1_000
    )
    if now_ms + _EPS < next_midnight_ms:
        raise ValueError("UTC day is not closed")
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise TypeError("day rows are missing")
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise TypeError(f"row {index} is not an object")
        _validate_row(row, day=day, frozen=frozen, index=index)
    _validate_signal_pairs(rows, frozen)
    return day


def _primary(
    rows: Iterable[dict[str, Any]],
    latency: float,
    target_shares: float,
) -> list[dict[str, Any]]:
    return sorted(
        (
            row
            for row in rows
            if row["variant"] == replay.BASE_VARIANT
            and float(row["evaluation_ms"]) == latency
            and row["filled"]
            and row["full_fill"]
            and float(row["filled_shares"]) + _EPS >= target_shares
        ),
        key=lambda row: (float(row["decision_recv_ms"]), str(row["signal_id"])),
    )


def _variant_statistics(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return basis._variant_statistics(rows)


def evaluate(rows: list[dict[str, Any]], frozen: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate the first frozen dual-latency stopping sample."""
    _validate_protocol(frozen)
    retained = list(rows)
    _validate_relationships(retained)
    statistics = frozen["statistics"]
    minimum_fills = int(statistics["minimum_full_fills_per_latency"])
    minimum_days = int(statistics["minimum_utc_days"])
    target = float(frozen["execution"]["minimum_full_fill_shares"])
    samples = {
        str(int(latency)): _primary(retained, latency, target)
        for latency in _latencies(frozen)
    }
    cutoff = basis._common_cutoff(
        samples,
        minimum_fills=minimum_fills,
        minimum_days=minimum_days,
    )
    progress = {}
    for latency, sample in samples.items():
        frozen_sample = (
            sample
            if cutoff is None
            else [
                row for row in sample if float(row["decision_recv_ms"]) <= cutoff + _EPS
            ]
        )
        progress[latency] = {
            "full_fills": len(frozen_sample),
            "days": len(
                {_utc_day(float(row["decision_recv_ms"])) for row in frozen_sample}
            ),
        }
    coverage, selected_parents = _matched_coverage(retained, cutoff_ms=cutoff)

    selected_rows: dict[str, list[dict[str, Any]]] = {}
    for variant in VARIANTS:
        variant_rows = [row for row in retained if row["variant"] == variant]
        if cutoff is not None:
            if variant in MATCHED_VARIANTS:
                variant_rows = [
                    row
                    for row in variant_rows
                    if row["parent_signal_id"] in selected_parents
                ]
            else:
                variant_rows = [
                    row
                    for row in variant_rows
                    if float(row["decision_recv_ms"]) <= cutoff + _EPS
                ]
        selected_rows[variant] = variant_rows
    variants = {
        variant: _variant_statistics(selected_rows[variant]) for variant in VARIANTS
    }
    matched_arms = {variant: variants[variant] for variant in MATCHED_VARIANTS}
    negative_cohorts = {
        variant: variants[variant] for variant in NEGATIVE_CONTROL_COHORTS
    }
    common = {
        "common_cutoff_ms": cutoff,
        "required_fills_per_latency": minimum_fills,
        "required_days": minimum_days,
        "progress": progress,
        "matched_arm_coverage": coverage,
        "matched_arms": matched_arms,
        "negative_control_cohorts": negative_cohorts,
        "variants": variants,
    }
    if cutoff is None:
        return {"status": "collecting", **common}

    alpha = float(statistics["one_sided_alpha"])
    family_tests = int(statistics["family_tests"])
    base = {
        latency: basis._base_statistics(
            [row for row in sample if float(row["decision_recv_ms"]) <= cutoff + _EPS],
            alpha=alpha,
            family_tests=family_tests,
        )
        for latency, sample in samples.items()
    }
    checks: dict[str, bool] = {}
    for latency, stats in base.items():
        checks[f"{latency}_positive_net_ev"] = stats["net_ev_per_share"] > 0
        checks[f"{latency}_positive_day_cluster_lower"] = (
            stats["day_cluster_lower"] is not None and stats["day_cluster_lower"] > 0
        )
        checks[f"{latency}_corrected_exact_p"] = stats["corrected_exact_p"] < alpha
    checks["matched_arms_complete"] = bool(coverage["complete"])
    return {
        "status": "passed" if all(checks.values()) else "rejected",
        **common,
        "alpha": alpha,
        "family_tests": family_tests,
        "base": base,
        "checks": checks,
    }


def _new_index(
    frozen: Mapping[str, Any],
    protocol_sha256: str,
    evaluator_sha256: str,
) -> dict[str, Any]:
    return {
        "schema": INDEX_SCHEMA,
        "strategy_id": frozen["strategy_id"],
        "collector_region": frozen["collector_region"],
        "paper_only": True,
        "protocol_sha256": protocol_sha256,
        "evaluator_sha256": evaluator_sha256,
        "days": [],
    }


def _status_payload(
    frozen: Mapping[str, Any],
    protocol_sha256: str,
    evaluator_sha256: str,
    index_sha256: str,
    result: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema": STATUS_SCHEMA,
        "strategy_id": frozen["strategy_id"],
        "paper_only": True,
        "protocol_sha256": protocol_sha256,
        "evaluator_sha256": evaluator_sha256,
        "index_sha256": index_sha256,
        "result": dict(result),
    }


def _verdict_payload(
    frozen: Mapping[str, Any],
    protocol_sha256: str,
    evaluator_sha256: str,
    index_sha256: str,
    result: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema": VERDICT_SCHEMA,
        "strategy_id": frozen["strategy_id"],
        "paper_only": True,
        "protocol_sha256": protocol_sha256,
        "evaluator_sha256": evaluator_sha256,
        "index_sha256": index_sha256,
        "verdict": dict(result),
    }


def _state_identity_matches(
    payload: Mapping[str, Any],
    *,
    schema: str,
    frozen: Mapping[str, Any],
    protocol_sha256: str,
    evaluator_sha256: str,
) -> bool:
    return (
        payload.get("schema") == schema
        and payload.get("strategy_id") == frozen["strategy_id"]
        and payload.get("paper_only") is True
        and payload.get("protocol_sha256") == protocol_sha256
        and payload.get("evaluator_sha256") == evaluator_sha256
    )


def _load_state(
    state: Path,
    *,
    frozen: Mapping[str, Any],
    protocol_sha256: str,
    evaluator_sha256: str,
    now_ms: float,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any] | None]:
    index_path = state / INDEX
    if not index_path.is_file():
        if any((state / name).exists() for name in (DAYS_DIR, STATUS, VERDICT)):
            raise ValueError("common-mode forward state exists without an index")
        return _new_index(frozen, protocol_sha256, evaluator_sha256), [], None

    index = _load_json(index_path)
    if (
        not _state_identity_matches(
            index,
            schema=INDEX_SCHEMA,
            frozen=frozen,
            protocol_sha256=protocol_sha256,
            evaluator_sha256=evaluator_sha256,
        )
        or index.get("collector_region") != frozen["collector_region"]
    ):
        raise ValueError("common-mode forward index identity drift")
    entries = index.get("days")
    if not isinstance(entries, list):
        raise TypeError("common-mode forward index days are missing")

    expected_day = _holdout_day(frozen)
    indexed_names: set[str] = set()
    rows: list[dict[str, Any]] = []
    for item in entries:
        if not isinstance(item, Mapping):
            raise TypeError("common-mode forward day index entry is invalid")
        day = str(item.get("day") or "")
        if _date(day) != expected_day:
            raise ValueError(
                f"common-mode forward day index is not contiguous at {day!r}"
            )
        expected_day += timedelta(days=1)
        path = state / DAYS_DIR / f"{day}.json"
        if not path.is_file() or _sha256(path) != item.get("sha256"):
            raise ValueError("common-mode immutable day hash mismatch")
        payload = _load_json(path)
        validate_day_result(
            payload,
            frozen=frozen,
            protocol_sha256=protocol_sha256,
            evaluator_sha256=evaluator_sha256,
            now_ms=now_ms,
        )
        if any(
            payload[field] != item.get(field)
            for field in (
                "raw_sidecar_identity_sha256",
                "strict_manifest_sha256",
                "archive_manifests_sha256",
                "artifact_binding_sha256",
            )
        ):
            raise ValueError("common-mode immutable day identity mismatch")
        indexed_names.add(day)
        rows.extend(dict(row) for row in payload["rows"])

    days_path = state / DAYS_DIR
    files = (
        {path.stem for path in days_path.glob("*.json")}
        if days_path.is_dir()
        else set()
    )
    if files != indexed_names:
        raise ValueError("common-mode forward day index mismatch")
    rows.sort(
        key=lambda row: (
            float(row["decision_recv_ms"]),
            str(row["variant"]),
            str(row["signal_id"]),
            float(row["evaluation_ms"]),
        )
    )
    _validate_relationships(rows)

    status_path = state / STATUS
    if not entries:
        if status_path.exists() or (state / VERDICT).exists():
            raise ValueError("empty index has unexpected forward state")
        return index, rows, None
    if not status_path.is_file():
        raise ValueError("indexed common-mode days exist without status")
    status = _load_json(status_path)
    if not _state_identity_matches(
        status,
        schema=STATUS_SCHEMA,
        frozen=frozen,
        protocol_sha256=protocol_sha256,
        evaluator_sha256=evaluator_sha256,
    ):
        raise ValueError("common-mode forward status identity drift")
    if status.get("index_sha256") != _sha256(index_path):
        raise ValueError("common-mode forward status index hash mismatch")
    result = status.get("result")
    if not isinstance(result, dict) or result != evaluate(rows, frozen):
        raise ValueError("common-mode forward status result drift")

    verdict_path = state / VERDICT
    terminal = result.get("status") != "collecting"
    if terminal and not verdict_path.is_file():
        raise ValueError("terminal common-mode verdict is missing")
    if not terminal and verdict_path.exists():
        raise ValueError("collecting common-mode state has a terminal verdict")
    if terminal:
        verdict = _load_json(verdict_path)
        expected_verdict = _verdict_payload(
            frozen,
            protocol_sha256,
            evaluator_sha256,
            str(status["index_sha256"]),
            result,
        )
        if verdict != expected_verdict:
            raise ValueError("pinned common-mode verdict changed")
    return index, rows, result


def _read_recovery_rows(
    state: Path,
    entries: list[dict[str, Any]],
    *,
    frozen: Mapping[str, Any],
    protocol_sha256: str,
    evaluator_sha256: str,
    now_ms: float,
) -> list[dict[str, Any]]:
    expected_day = _holdout_day(frozen)
    expected_files: set[str] = set()
    rows: list[dict[str, Any]] = []
    for item in entries:
        day = str(item.get("day") or "")
        if _date(day) != expected_day:
            raise ValueError("journaled day sequence is not contiguous")
        expected_day += timedelta(days=1)
        path = state / DAYS_DIR / f"{day}.json"
        if not path.is_file() or _sha256(path) != item.get("sha256"):
            raise ValueError("journaled immutable day hash mismatch")
        payload = _load_json(path)
        if (
            validate_day_result(
                payload,
                frozen=frozen,
                protocol_sha256=protocol_sha256,
                evaluator_sha256=evaluator_sha256,
                now_ms=now_ms,
            )
            != day
            or any(
                payload[field] != item.get(field)
                for field in (
                    "raw_sidecar_identity_sha256",
                    "strict_manifest_sha256",
                    "archive_manifests_sha256",
                    "artifact_binding_sha256",
                )
            )
        ):
            raise ValueError("journaled immutable day identity mismatch")
        expected_files.add(day)
        rows.extend(dict(row) for row in payload["rows"])
    days_path = state / DAYS_DIR
    actual_files = (
        {path.stem for path in days_path.glob("*.json")}
        if days_path.is_dir()
        else set()
    )
    if actual_files != expected_files:
        raise ValueError("journaled day file set drift")
    rows.sort(
        key=lambda row: (
            float(row["decision_recv_ms"]),
            str(row["variant"]),
            str(row["signal_id"]),
            float(row["evaluation_ms"]),
        )
    )
    _validate_relationships(rows)
    return rows


def _recover_journal(
    state: Path,
    *,
    frozen: Mapping[str, Any],
    protocol_sha256: str,
    evaluator_sha256: str,
    now_ms: float,
) -> dict[str, Any]:
    journal_path = state / JOURNAL
    journal = _load_json(journal_path)
    if not _state_identity_matches(
        journal,
        schema=_JOURNAL_SCHEMA,
        frozen=frozen,
        protocol_sha256=protocol_sha256,
        evaluator_sha256=evaluator_sha256,
    ):
        raise ValueError("common-mode admission journal identity drift")
    prior_payload = journal.get("prior_days")
    if not isinstance(prior_payload, list) or not all(
        isinstance(item, dict) for item in prior_payload
    ):
        raise ValueError("common-mode admission journal has invalid prior days")
    prior_days = [dict(item) for item in prior_payload]
    prior_status_sha = journal.get("prior_status_sha256")
    if prior_status_sha is not None and (
        not isinstance(prior_status_sha, str)
        or _SHA256.fullmatch(prior_status_sha) is None
    ):
        raise ValueError("common-mode admission journal status hash is invalid")
    if bool(prior_days) != (prior_status_sha is not None):
        raise ValueError("common-mode admission journal prior state is inconsistent")

    day = str(journal.get("day") or "")
    day_sha = str(journal.get("day_sha256") or "")
    raw_identity = str(journal.get("raw_sidecar_identity_sha256") or "")
    strict_identity = str(journal.get("strict_manifest_sha256") or "")
    archive_identity = str(journal.get("archive_manifests_sha256") or "")
    artifact_identity = str(journal.get("artifact_binding_sha256") or "")
    if (
        _SHA256.fullmatch(day_sha) is None
        or _SHA256.fullmatch(raw_identity) is None
        or _SHA256.fullmatch(strict_identity) is None
        or _SHA256.fullmatch(archive_identity) is None
        or _SHA256.fullmatch(artifact_identity) is None
    ):
        raise ValueError("common-mode admission journal day identity is invalid")
    expected_day = (
        _holdout_day(frozen)
        if not prior_days
        else _date(str(prior_days[-1]["day"])) + timedelta(days=1)
    )
    if _date(day) != expected_day:
        raise ValueError("common-mode admission journal day is not next")

    destination = state / DAYS_DIR / f"{day}.json"
    staging = state / STAGING
    if destination.exists():
        if _sha256(destination) != day_sha:
            raise ValueError("journaled immutable day hash mismatch")
        if staging.exists() and _sha256(staging) != day_sha:
            raise ValueError("journaled staging bytes changed")
    elif staging.is_file() and _sha256(staging) == day_sha:
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging.replace(destination)
    else:
        raise ValueError("journaled day bytes are missing")

    entry = {
        "day": day,
        "sha256": day_sha,
        "raw_sidecar_identity_sha256": raw_identity,
        "strict_manifest_sha256": strict_identity,
        "archive_manifests_sha256": archive_identity,
        "artifact_binding_sha256": artifact_identity,
    }
    prior_index = _new_index(frozen, protocol_sha256, evaluator_sha256)
    prior_index["days"] = prior_days
    next_index = _new_index(frozen, protocol_sha256, evaluator_sha256)
    next_index["days"] = [*prior_days, entry]
    index_path = state / INDEX
    if index_path.exists():
        current_index = _load_json(index_path)
        if current_index not in (prior_index, next_index):
            raise ValueError("journaled forward index drift")
    elif prior_days:
        raise ValueError("journaled prior index is missing")

    rows = _read_recovery_rows(
        state,
        next_index["days"],
        frozen=frozen,
        protocol_sha256=protocol_sha256,
        evaluator_sha256=evaluator_sha256,
        now_ms=now_ms,
    )
    result = evaluate(rows, frozen)
    next_index_sha = _json_sha256(next_index)
    expected_status = _status_payload(
        frozen,
        protocol_sha256,
        evaluator_sha256,
        next_index_sha,
        result,
    )
    status_path = state / STATUS
    if status_path.exists():
        current_status = _load_json(status_path)
        prior_status_matches = (
            prior_status_sha is not None and _sha256(status_path) == prior_status_sha
        )
        if current_status != expected_status and not prior_status_matches:
            raise ValueError("journaled forward status drift")
    elif prior_status_sha is not None:
        raise ValueError("journaled prior status is missing")

    expected_verdict = _verdict_payload(
        frozen,
        protocol_sha256,
        evaluator_sha256,
        next_index_sha,
        result,
    )
    verdict_path = state / VERDICT
    if verdict_path.exists() and (
        result["status"] == "collecting" or _load_json(verdict_path) != expected_verdict
    ):
        raise ValueError("journaled terminal verdict drift")

    _atomic_json(index_path, next_index)
    if _sha256(index_path) != next_index_sha:
        raise ValueError("recovered forward index bytes drift")
    _atomic_json(status_path, expected_status)
    if result["status"] != "collecting":
        _atomic_json(verdict_path, expected_verdict)
    journal_path.unlink()
    staging.unlink(missing_ok=True)
    return result


def update(
    day_file: str | Path,
    state_dir: str | Path,
    *,
    frozen: Mapping[str, Any],
    protocol_sha256: str,
    evaluator_sha256: str,
    now_ms: float | None = None,
    _allow_test_freeze: bool = False,
    _crash_after_day_commit: bool = False,
    _crash_after_index_commit: bool = False,
    _crash_after_status_commit: bool = False,
) -> dict[str, Any]:
    """Admit one immutable closed UTC day and return the frozen paper result."""
    resolved = _resolve_freeze(
        frozen,
        protocol_sha256=protocol_sha256,
        evaluator_sha256=evaluator_sha256,
        allow_test_freeze=_allow_test_freeze,
    )
    now = time.time() * 1_000 if now_ms is None else basis._finite_number(now_ms, "now")
    state = Path(state_dir)
    state.mkdir(parents=True, exist_ok=True)
    if (state / JOURNAL).exists():
        _recover_journal(
            state,
            frozen=resolved,
            protocol_sha256=protocol_sha256,
            evaluator_sha256=evaluator_sha256,
            now_ms=now,
        )
    elif (state / STAGING).exists():
        raise ValueError("common-mode staging exists without an admission journal")

    source = Path(day_file)
    incoming = _load_json(source)
    day = validate_day_result(
        incoming,
        frozen=resolved,
        protocol_sha256=protocol_sha256,
        evaluator_sha256=evaluator_sha256,
        now_ms=now,
    )
    incoming_sha = _sha256(source)

    index, existing_rows, current_result = _load_state(
        state,
        frozen=resolved,
        protocol_sha256=protocol_sha256,
        evaluator_sha256=evaluator_sha256,
        now_ms=now,
    )
    existing_days = [str(item["day"]) for item in index["days"]]
    destination = state / DAYS_DIR / f"{day}.json"
    if day in existing_days:
        recorded = index["days"][existing_days.index(day)]
        if recorded["sha256"] != incoming_sha:
            raise ValueError("common-mode admitted day changed")
        if current_result is None:
            raise ValueError("common-mode admitted day has no status")
        return current_result

    if current_result is not None and current_result["status"] != "collecting":
        raise ValueError("terminal common-mode verdict cannot accept another day")
    expected = (
        _holdout_day(resolved)
        if not existing_days
        else _date(existing_days[-1]) + timedelta(days=1)
    )
    if _date(day) != expected:
        raise ValueError(f"expected day {expected.isoformat()}, got {day}")

    rows = [*existing_rows, *(dict(row) for row in incoming["rows"])]
    rows.sort(
        key=lambda row: (
            float(row["decision_recv_ms"]),
            str(row["variant"]),
            str(row["signal_id"]),
            float(row["evaluation_ms"]),
        )
    )
    result = evaluate(rows, resolved)

    entry = {
        "day": day,
        "sha256": incoming_sha,
        "raw_sidecar_identity_sha256": incoming["raw_sidecar_identity_sha256"],
        "strict_manifest_sha256": incoming["strict_manifest_sha256"],
        "archive_manifests_sha256": incoming["archive_manifests_sha256"],
        "artifact_binding_sha256": incoming["artifact_binding_sha256"],
    }
    next_index = {**index, "days": [*index["days"], entry]}
    staging = state / STAGING
    staging_temporary = staging.with_name(staging.name + ".tmp")
    shutil.copyfile(source, staging_temporary)
    staging_temporary.replace(staging)
    if _sha256(staging) != incoming_sha:
        raise ValueError("common-mode staged day changed")
    prior_status_sha = _sha256(state / STATUS) if (state / STATUS).is_file() else None
    _atomic_json(
        state / JOURNAL,
        {
            "schema": _JOURNAL_SCHEMA,
            "strategy_id": resolved["strategy_id"],
            "paper_only": True,
            "protocol_sha256": protocol_sha256,
            "evaluator_sha256": evaluator_sha256,
            "day": day,
            "day_sha256": incoming_sha,
            "raw_sidecar_identity_sha256": incoming["raw_sidecar_identity_sha256"],
            "strict_manifest_sha256": incoming["strict_manifest_sha256"],
            "archive_manifests_sha256": incoming["archive_manifests_sha256"],
            "artifact_binding_sha256": incoming["artifact_binding_sha256"],
            "prior_days": index["days"],
            "prior_status_sha256": prior_status_sha,
        },
    )

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging.replace(destination)
    if _crash_after_day_commit:
        raise RuntimeError("simulated crash after day commit")

    _atomic_json(state / INDEX, next_index)
    if _crash_after_index_commit:
        raise RuntimeError("simulated crash after index commit")

    index_sha = _sha256(state / INDEX)
    status = _status_payload(
        resolved,
        protocol_sha256,
        evaluator_sha256,
        index_sha,
        result,
    )
    _atomic_json(state / STATUS, status)
    if _crash_after_status_commit:
        raise RuntimeError("simulated crash after status commit")
    if result["status"] != "collecting":
        _atomic_json(
            state / VERDICT,
            _verdict_payload(
                resolved,
                protocol_sha256,
                evaluator_sha256,
                index_sha,
                result,
            ),
        )
    (state / JOURNAL).unlink()
    staging.unlink(missing_ok=True)
    return result
