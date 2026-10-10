from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Callable

import common_mode_forward as forward
import pytest


ROOT = Path(__file__).parent
PROTOCOL_SHA = "p" * 64
EVALUATOR_SHA = "e" * 64


def _ms(day: str, seconds: float = 60.0) -> float:
    start = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    return start.timestamp() * 1_000 + seconds * 1_000


def _frozen() -> dict[str, Any]:
    frozen = json.loads(
        (ROOT / "forward" / "common-mode-residual-freeze.json").read_text(
            encoding="utf-8"
        )
    )
    frozen["holdout_start"] = "2026-10-10T00:00:00Z"
    frozen["holdout_start_ms"] = _ms("2026-10-10", 0)
    frozen["statistics"].update(
        {
            "minimum_full_fills_per_latency": 2,
            "minimum_utc_days": 2,
            "family_tests": 1,
            "one_sided_alpha": 0.5,
        }
    )
    return frozen


def _row(
    day: str,
    variant: str,
    latency: float,
    *,
    signal: int = 1,
    full: bool = True,
    won: bool = True,
) -> dict[str, Any]:
    base_decision_ms = _ms(day, 60.0 + signal)
    decision_ms = (
        base_decision_ms + 5_000.0 if variant == "time_shift" else base_decision_ms
    )
    shares = Decimal("5") if full else Decimal("2")
    price = Decimal("0.4")
    fee = (shares * Decimal("0.07") * price * (Decimal(1) - price)).quantize(
        Decimal("0.00001"), rounding=ROUND_HALF_UP
    )
    total_cost = shares * price + fee
    all_in_cost = total_cost / shares
    common_direction = "up"
    buy_side = "up" if variant == "direction_reversal" else "down"
    winner = buy_side.title() if won else ("Down" if buy_side == "up" else "Up")
    pnl = (shares if won else Decimal(0)) - total_cost
    parent_signal_id = (
        f"base:{day}:{signal}"
        if variant in {"direction_reversal", "time_shift"}
        else None
    )
    comparison_design = (
        "matched_base_arm"
        if variant in {"direction_reversal", "time_shift"}
        else "disjoint_negative_control_cohort"
        if variant in {"benchmark_confirmation", "no_pm_follow"}
        else "base"
    )
    return {
        "signal_id": f"{variant}:{day}:{signal}",
        "parent_signal_id": parent_signal_id,
        "paper_only": True,
        "ordering_clock": "local_receipt_ms",
        "match_book_clock": "local_receipt_ms",
        "market_id": f"m-{day}-{signal}",
        "variant": variant,
        "comparison_design": comparison_design,
        "signal_recv_ms": base_decision_ms,
        "decision_recv_ms": decision_ms,
        "scheduled_decision_recv_ms": decision_ms,
        "decision_book_recv_ms": decision_ms,
        "evaluation_ms": latency,
        "evaluation_recv_ms": decision_ms + latency,
        "book_recv_ms": decision_ms + latency,
        "common_direction": common_direction,
        "buy_side": buy_side,
        "fair": 0.5,
        "decision_vwap": 0.39,
        "fixed_limit": float(price),
        "signal_fee_per_share": 0.0,
        "signal_net_edge": 0.11,
        "target_shares": 5.0,
        "filled_shares": float(shares),
        "fill_vwap": float(price),
        "fill_levels": [{"price": float(price), "shares": float(shares)}],
        "fee_rate": 0.07,
        "fee": float(fee),
        "total_cost": float(total_cost),
        "all_in_cost": float(all_in_cost),
        "winner": winner,
        "won": won,
        "pnl_per_share": float(pnl / shares),
        "pnl": float(pnl),
        "filled": True,
        "full_fill": full,
        "sent": True,
        "reason": "filled" if full else "partial_fill",
        "signal": {
            "market_id": f"m-{day}-{signal}",
            "recv_ms": base_decision_ms,
            "common_direction": common_direction,
            "buy_side": buy_side,
            "fair": 0.5,
            "ask": 0.39,
            "fixed_limit": float(price),
            "fee": 0.0,
            "net_edge": 0.11,
            "direct_depth": 5.0,
        },
    }


def _rows(
    day: str,
    *,
    full_500: bool = True,
    include_negative_cohorts: bool = False,
    include_time_shift: bool = True,
) -> list[dict[str, Any]]:
    variants = ["base", "direction_reversal"]
    if include_time_shift:
        variants.append("time_shift")
    if include_negative_cohorts:
        variants.extend(["benchmark_confirmation", "no_pm_follow"])
    return [
        _row(
            day,
            variant,
            latency,
            full=full_500 or variant != "base" or latency == 400.0,
        )
        for variant in variants
        for latency in (400.0, 500.0)
    ]


def _day(path: Path, day: str, rows: list[dict[str, Any]]) -> Path:
    compact_day = day.replace("-", "")
    sidecar_files = [
        {
            "file": f"{route}.{compact_day}T{hour:02d}.txt.gz",
            "size": hour + 1,
            "sha256": hashlib.sha256(f"{route}-{day}-{hour}".encode()).hexdigest(),
        }
        for route in ("bn_spot", "deribit")
        for hour in range(24)
    ]
    rec_files = [
        {**item, "uploaded_ok": True, "gzip_ok": True}
        for item in sidecar_files
    ]
    archive_manifests = {
        "rec": {"day": compact_day, "group": "rec", "files": rec_files},
        "poly": {"day": compact_day, "group": "poly", "files": []},
    }
    archive_sha = hashlib.sha256(
        (
            json.dumps(archive_manifests, indent=2, sort_keys=True, allow_nan=False)
            + "\n"
        ).encode()
    ).hexdigest()
    raw_sha = hashlib.sha256(
        json.dumps(
            {
                "schema": "common-mode-raw-sidecars-v1",
                "day": compact_day,
                "files": sidecar_files,
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
    strict_sha = hashlib.sha256(f"strict-{day}".encode()).hexdigest()
    artifact_binding = {
        "schema": "eu-strict-input-binding-v1",
        "day": compact_day,
        "raw_archive_manifests_sha256": hashlib.sha256(
            json.dumps(
                archive_manifests,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest(),
        "strict_manifest_sha256": strict_sha,
    }
    artifact_sha = hashlib.sha256(
        (
            json.dumps(artifact_binding, indent=2, sort_keys=True, allow_nan=False)
            + "\n"
        ).encode()
    ).hexdigest()
    raw_binding = {
        "schema": "common-mode-raw-sidecars-v1",
        "day": compact_day,
        "protocol_sha256": PROTOCOL_SHA,
        "evaluator_sha256": EVALUATOR_SHA,
        "archive_manifests_sha256": archive_sha,
        "raw_sidecar_identity_sha256": raw_sha,
        "strict_manifest_sha256": strict_sha,
        "files": sidecar_files,
    }
    payload = {
        "schema": "common-mode-residual-day-v1",
        "complete": True,
        "day": day,
        "collector_region": "eu-west-1",
        "protocol_sha256": PROTOCOL_SHA,
        "evaluator_sha256": EVALUATOR_SHA,
        "raw_sidecar_identity_sha256": raw_sha,
        "archive_manifests_sha256": archive_sha,
        "strict_manifest_sha256": strict_sha,
        "artifact_binding_sha256": artifact_sha,
        "input_bindings": {
            "archive_manifests": archive_manifests,
            "artifact_binding": artifact_binding,
            "raw_sidecar_binding": raw_binding,
        },
        "rows": rows,
    }
    path.write_text(
        json.dumps(payload, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return path


def _update(day_file: Path, state: Path) -> dict[str, Any]:
    return forward.update(
        day_file,
        state,
        frozen=_frozen(),
        protocol_sha256=PROTOCOL_SHA,
        evaluator_sha256=EVALUATOR_SHA,
        now_ms=_ms("2026-10-15", 0),
        _allow_test_freeze=True,
    )


def _find(rows: list[dict[str, Any]], variant: str, latency: float) -> dict[str, Any]:
    return next(
        row
        for row in rows
        if row["variant"] == variant and row["evaluation_ms"] == latency
    )


def _write_hashed_json(path: Path, payload: dict[str, Any]) -> str:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_suffix(".sha256").write_text(digest + "\n", encoding="ascii")
    return digest


def test_load_freezes_binds_evaluator_to_protocol_and_dependencies(
    tmp_path: Path,
) -> None:
    protocol = json.loads(
        (ROOT / "forward" / "common-mode-residual-freeze.json").read_text(
            encoding="utf-8"
        )
    )
    protocol_path = tmp_path / "protocol.json"
    protocol_sha = _write_hashed_json(protocol_path, protocol)
    evaluator = json.loads(
        (ROOT / "forward" / "common-mode-evaluator-freeze.json").read_text(
            encoding="utf-8"
        )
    )
    evaluator["protocol_sha256"] = protocol_sha
    evaluator["dependencies_sha256"] = {
        "common_mode_run.py": hashlib.sha256(
            (ROOT / "common_mode_run.py").read_bytes()
        ).hexdigest()
    }
    evaluator_path = tmp_path / "evaluator.json"
    evaluator_sha = _write_hashed_json(evaluator_path, evaluator)

    loaded_protocol, loaded_protocol_sha, loaded_evaluator, loaded_evaluator_sha = (
        forward.load_freezes(protocol_path, evaluator_path)
    )

    assert loaded_protocol == protocol
    assert loaded_protocol_sha == protocol_sha
    assert loaded_evaluator == evaluator
    assert loaded_evaluator_sha == evaluator_sha

    for field, value in (
        ("protocol_sha256", "f" * 64),
        ("strategy_id", "different-strategy"),
        ("holdout_start", "2026-10-12T00:00:00Z"),
    ):
        changed = {**evaluator, field: value}
        _write_hashed_json(evaluator_path, changed)
        with pytest.raises(ValueError, match="identity drift"):
            forward.load_freezes(protocol_path, evaluator_path)

    evaluator["dependencies_sha256"]["common_mode_run.py"] = "0" * 64
    _write_hashed_json(evaluator_path, evaluator)
    with pytest.raises(ValueError, match="dependency drift"):
        forward.load_freezes(protocol_path, evaluator_path)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        (None, "variants", ["base"]),
        ("execution", "evaluation_ms", [300.0, 500.0]),
        ("execution", "paper_fill_is_upper_bound", False),
        ("statistics", "family_tests", 1),
        ("statistics", "matched_arms_must_be_complete", False),
    ],
)
def test_evaluator_freeze_semantics_cannot_silently_drift(
    tmp_path: Path,
    section: str | None,
    field: str,
    value: Any,
) -> None:
    protocol = json.loads(
        (ROOT / "forward" / "common-mode-residual-freeze.json").read_text(
            encoding="utf-8"
        )
    )
    protocol_path = tmp_path / "protocol.json"
    protocol_sha = _write_hashed_json(protocol_path, protocol)
    evaluator = json.loads(
        (ROOT / "forward" / "common-mode-evaluator-freeze.json").read_text(
            encoding="utf-8"
        )
    )
    evaluator["protocol_sha256"] = protocol_sha
    evaluator["dependencies_sha256"] = {
        "common_mode_run.py": hashlib.sha256(
            (ROOT / "common_mode_run.py").read_bytes()
        ).hexdigest()
    }
    if section is None:
        evaluator[field] = value
    else:
        evaluator[section][field] = value
    evaluator_path = tmp_path / "evaluator.json"
    _write_hashed_json(evaluator_path, evaluator)

    with pytest.raises(ValueError, match="semantic drift|execution/statistics drift"):
        forward.load_freezes(protocol_path, evaluator_path)


def test_production_update_uses_evaluator_freeze_sha_not_source_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frozen = _frozen()
    evaluator = {
        "schema": "common-mode-evaluator-freeze-v1",
        "strategy_id": frozen["strategy_id"],
        "holdout_start": frozen["holdout_start"],
        "protocol_sha256": PROTOCOL_SHA,
        "dependencies_sha256": {},
    }
    monkeypatch.setattr(
        forward,
        "load_freezes",
        lambda: (frozen, PROTOCOL_SHA, evaluator, EVALUATOR_SHA),
    )
    day = _day(tmp_path / "day.json", "2026-10-10", [])

    result = forward.update(
        day,
        tmp_path / "state",
        frozen=frozen,
        protocol_sha256=PROTOCOL_SHA,
        evaluator_sha256=EVALUATOR_SHA,
        now_ms=_ms("2026-10-15", 0),
    )

    assert result["status"] == "collecting"


def test_empty_holdout_day_is_admitted_and_same_bytes_are_idempotent(
    tmp_path: Path,
) -> None:
    day = _day(tmp_path / "empty.json", "2026-10-10", [])
    state = tmp_path / "state"

    first = _update(day, state)
    repeated = _update(day, state)

    assert first == repeated
    assert first["status"] == "collecting"
    index = json.loads((state / forward.INDEX).read_text(encoding="utf-8"))
    assert [item["day"] for item in index["days"]] == ["2026-10-10"]
    assert (state / forward.STATUS).is_file()
    assert not (state / forward.VERDICT).exists()


def test_first_day_gap_and_changed_admitted_bytes_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="expected day 2026-10-10"):
        _update(
            _day(tmp_path / "first-gap.json", "2026-10-11", []),
            tmp_path / "first-gap-state",
        )

    state = tmp_path / "state"
    original = _day(tmp_path / "original.json", "2026-10-10", [])
    _update(original, state)
    changed_payload = json.loads(original.read_text(encoding="utf-8"))
    changed_payload["unexpected_extra_field"] = "different bytes"
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(changed_payload, sort_keys=True) + "\n")
    with pytest.raises(ValueError, match="admitted day changed"):
        _update(changed, state)

    with pytest.raises(ValueError, match="expected day 2026-10-11"):
        _update(_day(tmp_path / "gap.json", "2026-10-12", []), state)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("collector_region", "us-east-1", "collector region"),
        ("protocol_sha256", "x" * 64, "protocol hash"),
        ("evaluator_sha256", "x" * 64, "evaluator hash"),
        ("raw_sidecar_identity_sha256", "not-a-hash", "raw sidecar"),
        ("strict_manifest_sha256", "", "strict manifest"),
    ],
)
def test_empty_day_still_requires_complete_identity(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    path = _day(tmp_path / f"{field}.json", "2026-10-10", [])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[field] = value
    path.write_text(json.dumps(payload, sort_keys=True) + "\n")

    with pytest.raises(ValueError, match=message):
        _update(path, tmp_path / f"state-{field}")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda payload: payload.update(complete=False), "day schema"),
        (
            lambda payload: payload["input_bindings"]["archive_manifests"]["rec"].update(
                day="20261009"
            ),
            "archive manifest binding",
        ),
        (
            lambda payload: payload["input_bindings"]["raw_sidecar_binding"][
                "files"
            ][0].update(size=999),
            "raw sidecar",
        ),
        (
            lambda payload: payload["input_bindings"]["artifact_binding"].update(
                strict_manifest_sha256="f" * 64
            ),
            "artifact binding",
        ),
    ],
)
def test_incomplete_or_unbound_day_evidence_is_rejected(
    tmp_path: Path,
    mutate: Callable[[dict[str, Any]], None],
    message: str,
) -> None:
    path = _day(tmp_path / "day.json", "2026-10-10", [])
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        _update(path, tmp_path / "state")


def test_common_cutoff_passes_with_complete_matched_arms_and_empty_cohorts(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    first = _update(
        _day(tmp_path / "d1.json", "2026-10-10", _rows("2026-10-10")),
        state,
    )
    terminal = _update(
        _day(tmp_path / "d2.json", "2026-10-11", _rows("2026-10-11")),
        state,
    )

    assert first["status"] == "collecting"
    assert terminal["status"] == "passed"
    assert terminal["common_cutoff_ms"] == _ms("2026-10-11", 61.0)
    assert set(terminal["base"]) == {"400", "500"}
    assert all(item["fills"] == 2 for item in terminal["base"].values())
    assert terminal["checks"]["matched_arms_complete"] is True
    assert all(
        cohort["400"]["signals"] == cohort["500"]["signals"] == 0
        for cohort in terminal["negative_control_cohorts"].values()
    )
    assert (state / forward.VERDICT).is_file()

    repeated = _update(tmp_path / "d2.json", state)
    assert repeated == terminal
    with pytest.raises(ValueError, match="terminal"):
        _update(_day(tmp_path / "d3.json", "2026-10-12", []), state)


def test_partial_fills_add_pnl_but_never_advance_stopping(tmp_path: Path) -> None:
    state = tmp_path / "state"
    for day in ("2026-10-10", "2026-10-11"):
        result = _update(
            _day(
                tmp_path / f"{day}.json",
                day,
                _rows(day, full_500=False),
            ),
            state,
        )

    assert result["status"] == "collecting"
    assert result["progress"]["400"]["full_fills"] == 2
    assert result["progress"]["500"]["full_fills"] == 0
    assert result["variants"]["base"]["500"]["partial_fills"] == 2
    assert result["variants"]["base"]["500"]["pnl"] > 0


Mutation = Callable[[list[dict[str, Any]]], None]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda rows: _find(rows, "base", 400.0).update(variant="unknown"),
            "variant",
        ),
        (
            lambda rows: _find(rows, "base", 500.0).update(evaluation_ms=300.0),
            "evaluation",
        ),
        (
            lambda rows: rows.remove(_find(rows, "base", 500.0)),
            "missing.*latency",
        ),
        (
            lambda rows: _find(rows, "direction_reversal", 400.0).update(
                parent_signal_id="missing"
            ),
            "paired decision|orphan parent",
        ),
        (
            lambda rows: _find(rows, "direction_reversal", 400.0).update(
                comparison_design="disjoint_negative_control_cohort"
            ),
            "comparison design|paired decision",
        ),
        (
            lambda rows: _find(rows, "benchmark_confirmation", 400.0).update(
                parent_signal_id="base:2026-10-10:1"
            ),
            "parent|paired decision",
        ),
    ],
)
def test_variant_latency_pair_and_parent_invariants_fail_closed(
    tmp_path: Path, mutate: Mutation, message: str
) -> None:
    rows = _rows("2026-10-10", include_negative_cohorts=True)
    mutate(rows)

    with pytest.raises((TypeError, ValueError), match=message):
        _update(
            _day(tmp_path / "bad.json", "2026-10-10", rows),
            tmp_path / "state",
        )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda row: row.update(fee=0.0), "fee"),
        (lambda row: row.update(total_cost=0.0), "total cost|PnL"),
        (lambda row: row.update(pnl=0.0), "PnL"),
        (lambda row: row.update(full_fill=False), "full/partial"),
        (lambda row: row.update(winner=None, won=None), "unresolved fill"),
    ],
)
def test_fill_economics_and_settlement_are_recomputed(
    tmp_path: Path,
    mutate: Callable[[dict[str, Any]], None],
    message: str,
) -> None:
    rows = _rows("2026-10-10")
    mutate(_find(rows, "base", 400.0))

    with pytest.raises(ValueError, match=message):
        _update(
            _day(tmp_path / "bad.json", "2026-10-10", rows),
            tmp_path / "state",
        )


def test_immutable_signal_payload_tampering_is_rejected(tmp_path: Path) -> None:
    rows = _rows("2026-10-10")
    row = _find(rows, "base", 400.0)
    row["signal"]["fair"] = 0.75

    with pytest.raises(ValueError, match="differs from the immutable signal"):
        _update(
            _day(tmp_path / "bad-signal.json", "2026-10-10", rows),
            tmp_path / "state",
        )


def test_missing_matched_arm_rejects_when_stopping_cutoff_is_reached(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    for day in ("2026-10-10", "2026-10-11"):
        result = _update(
            _day(
                tmp_path / f"{day}.json",
                day,
                _rows(day, include_time_shift=False),
            ),
            state,
        )

    assert result["status"] == "rejected"
    assert result["checks"]["matched_arms_complete"] is False


def test_admitted_day_tampering_is_detected_before_reuse(tmp_path: Path) -> None:
    source = _day(tmp_path / "d1.json", "2026-10-10", [])
    state = tmp_path / "state"
    _update(source, state)
    admitted = state / forward.DAYS_DIR / "2026-10-10.json"
    admitted.write_text(admitted.read_text(encoding="utf-8") + " ", encoding="utf-8")

    with pytest.raises(ValueError, match="immutable day hash"):
        _update(source, state)


@pytest.mark.parametrize(
    "crash_flag",
    [
        "_crash_after_day_commit",
        "_crash_after_index_commit",
        "_crash_after_status_commit",
    ],
)
def test_every_admission_commit_boundary_recovers(
    tmp_path: Path, crash_flag: str
) -> None:
    state = tmp_path / crash_flag
    day = _day(tmp_path / f"{crash_flag}.json", "2026-10-10", [])

    with pytest.raises(RuntimeError, match="simulated crash"):
        forward.update(
            day,
            state,
            frozen=_frozen(),
            protocol_sha256=PROTOCOL_SHA,
            evaluator_sha256=EVALUATOR_SHA,
            now_ms=_ms("2026-10-15", 0),
            _allow_test_freeze=True,
            **{crash_flag: True},
        )

    assert (state / forward.JOURNAL).is_file()
    result = _update(day, state)
    assert result["status"] == "collecting"
    assert not (state / forward.JOURNAL).exists()
    assert not (state / forward.STAGING).exists()


def test_status_commit_recovery_materializes_terminal_verdict(tmp_path: Path) -> None:
    frozen = _frozen()
    frozen["statistics"].update(
        {
            "minimum_full_fills_per_latency": 1,
            "minimum_utc_days": 1,
        }
    )
    state = tmp_path / "terminal-status"
    day = _day(tmp_path / "terminal.json", "2026-10-10", _rows("2026-10-10"))

    with pytest.raises(RuntimeError, match="simulated crash after status commit"):
        forward.update(
            day,
            state,
            frozen=frozen,
            protocol_sha256=PROTOCOL_SHA,
            evaluator_sha256=EVALUATOR_SHA,
            now_ms=_ms("2026-10-15", 0),
            _allow_test_freeze=True,
            _crash_after_status_commit=True,
        )

    assert not (state / forward.VERDICT).exists()
    recovered = forward.update(
        day,
        state,
        frozen=frozen,
        protocol_sha256=PROTOCOL_SHA,
        evaluator_sha256=EVALUATOR_SHA,
        now_ms=_ms("2026-10-15", 0),
        _allow_test_freeze=True,
    )

    assert recovered["status"] in {"passed", "rejected"}
    assert (state / forward.VERDICT).is_file()
    assert not (state / forward.JOURNAL).exists()
