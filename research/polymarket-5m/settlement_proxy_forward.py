"""Pool immutable daily settlement-proxy observations and pin one verdict."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import settlement_proxy as proxy


DAY_SCHEMA = "settlement-proxy-day-v1"
INDEX_SCHEMA = "settlement-proxy-forward-index-v1"
STATUS_SCHEMA = "settlement-proxy-forward-status-v1"
VERDICT_SCHEMA = "settlement-proxy-forward-verdict-v1"
INDEX = "day-index.json"
STATUS = "status.json"
VERDICT = "verdict.json"
DAYS_DIR = "days"


def _sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: str | Path, payload: Mapping[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)


def expected_variants() -> tuple[str, ...]:
    return (
        proxy.BASE_VARIANT,
        proxy.BASELINE_VARIANT,
        proxy.TIMING_VARIANT,
        *(f"omit_{source}" for source in (*proxy.SPOT_SOURCES, proxy.DERIBIT_SOURCE)),
    )


def _day_for_slot(slot: int) -> str:
    return dt.datetime.fromtimestamp(slot, tz=dt.timezone.utc).strftime("%Y%m%d")


def _next_day(day: str) -> str:
    value = dt.date.fromisoformat(f"{day[:4]}-{day[4:6]}-{day[6:]}") + dt.timedelta(days=1)
    return value.strftime("%Y%m%d")


def validate_day_result(
    payload: Mapping[str, Any],
    *,
    fingerprint: str,
    calibration_sha256: str,
    provenance_sha256: str,
    evaluator_sha256: str,
) -> str:
    day = str(payload.get("day") or "")
    if (
        payload.get("schema") != DAY_SCHEMA
        or not re.fullmatch(r"\d{8}", day)
        or payload.get("strategy_fingerprint") != fingerprint
        or payload.get("calibration_sha256") != calibration_sha256
        or payload.get("provenance_sha256") != provenance_sha256
        or payload.get("evaluator_sha256") != evaluator_sha256
    ):
        raise ValueError("settlement-proxy day identity drift")
    observations = payload.get("observations")
    if not isinstance(observations, Mapping):
        raise TypeError("settlement-proxy day observations are missing")
    signal_rows = observations.get("signals")
    execution_rows = observations.get("execution_rows")
    outcome_rows = observations.get("outcome_rows")
    variants = set(expected_variants())
    if not isinstance(signal_rows, Mapping) or set(signal_rows) != variants:
        raise ValueError("settlement-proxy day signal variants drift")
    if not isinstance(execution_rows, Mapping) or set(execution_rows) != variants:
        raise ValueError("settlement-proxy day execution variants drift")
    if not isinstance(outcome_rows, list):
        raise TypeError("settlement-proxy day outcome rows are missing")

    seen_signals: set[tuple[str, str]] = set()
    seen_rows: set[tuple[str, str, float]] = set()
    all_signal_markets: set[str] = set()
    for variant in expected_variants():
        if not isinstance(signal_rows[variant], list) or not isinstance(execution_rows[variant], list):
            raise TypeError("settlement-proxy day observations must be lists")
        variant_signals: dict[str, Mapping[str, Any]] = {}
        for row in signal_rows[variant]:
            if not isinstance(row, Mapping) or row.get("variant") != variant:
                raise ValueError("settlement-proxy signal variant mismatch")
            slot = int(row["slot"])
            market_id = str(row["market_id"])
            if _day_for_slot(slot) != day:
                raise ValueError("settlement-proxy signal is outside its UTC day")
            identity = (variant, market_id)
            if identity in seen_signals:
                raise ValueError("duplicate settlement-proxy signal")
            seen_signals.add(identity)
            variant_signals[market_id] = row
            all_signal_markets.add(market_id)
            proxy.ProxySignal(**row)
        actual_rows: set[tuple[str, float]] = set()
        for row in execution_rows[variant]:
            if not isinstance(row, Mapping):
                raise TypeError("settlement-proxy execution row is not an object")
            market_id = str(row["market_id"])
            latency = float(row["evaluation_ms"])
            if row.get("variant") != variant or latency not in (300.0, 500.0):
                raise ValueError("settlement-proxy execution identity mismatch")
            signal = variant_signals.get(market_id)
            if (
                signal is None
                or int(row.get("protocol_slot")) != int(signal["slot"])
                or row.get("winner") != signal.get("actual_winner")
            ):
                raise ValueError("settlement-proxy execution has no matching signal")
            expected_date = f"{day[:4]}-{day[4:6]}-{day[6:]}"
            if str(row.get("day")) != expected_date:
                raise ValueError("settlement-proxy execution row is outside its UTC day")
            identity = (variant, market_id, latency)
            if identity in seen_rows:
                raise ValueError("duplicate settlement-proxy execution row")
            seen_rows.add(identity)
            actual_rows.add((market_id, latency))
        expected_rows = {
            (market_id, latency)
            for market_id in variant_signals
            for latency in (300.0, 500.0)
        }
        if actual_rows != expected_rows:
            raise ValueError("settlement-proxy execution rows are incomplete")

    seen_outcomes: set[str] = set()
    for row in outcome_rows:
        if not isinstance(row, Mapping):
            raise TypeError("settlement-proxy outcome row is not an object")
        market_id = str(row["market_id"])
        if _day_for_slot(int(row["slot"])) != day or market_id in seen_outcomes:
            raise ValueError("settlement-proxy outcome identity mismatch")
        if row.get("status") not in ("compared", "missing_exact", "unresolved"):
            raise ValueError("settlement-proxy outcome status drift")
        seen_outcomes.add(market_id)
    if not all_signal_markets <= seen_outcomes:
        raise ValueError("settlement-proxy signal is missing its outcome row")
    return day


def evaluate_day_payloads(payloads: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not payloads:
        raise ValueError("no settlement-proxy days to evaluate")
    days = [str(payload["day"]) for payload in payloads]
    if days != sorted(days) or len(days) != len(set(days)):
        raise ValueError("settlement-proxy days are not unique chronological rows")
    variants = expected_variants()
    signals: dict[str, list[proxy.ProxySignal]] = {variant: [] for variant in variants}
    execution_rows: dict[str, list[dict[str, Any]]] = {variant: [] for variant in variants}
    outcome_rows: list[dict[str, Any]] = []
    for payload in payloads:
        observations = payload["observations"]
        for variant in variants:
            signals[variant].extend(
                proxy.ProxySignal(**row) for row in observations["signals"][variant]
            )
            execution_rows[variant].extend(dict(row) for row in observations["execution_rows"][variant])
        outcome_rows.extend(dict(row) for row in observations["outcome_rows"])
    evaluation = proxy.evaluate_observations(execution_rows, signals, outcome_rows)
    result = {
        "days": days,
        "day_count": len(days),
        **evaluation,
    }
    result["verdict"] = proxy.paper_verdict(result)
    return result


def _load_index(
    state: Path,
    *,
    fingerprint: str,
    calibration_sha256: str,
    provenance_sha256: str,
    evaluator_sha256: str,
) -> dict[str, Any]:
    path = state / INDEX
    if not path.is_file():
        if (
            (state / DAYS_DIR).exists()
            or (state / STATUS).exists()
            or (state / VERDICT).exists()
        ):
            raise ValueError("settlement-proxy forward state exists without an index")
        return {
            "schema": INDEX_SCHEMA,
            "strategy_fingerprint": fingerprint,
            "calibration_sha256": calibration_sha256,
            "provenance_sha256": provenance_sha256,
            "evaluator_sha256": evaluator_sha256,
            "days": [],
        }
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        payload.get("schema") != INDEX_SCHEMA
        or payload.get("strategy_fingerprint") != fingerprint
        or payload.get("calibration_sha256") != calibration_sha256
        or payload.get("provenance_sha256") != provenance_sha256
        or payload.get("evaluator_sha256") != evaluator_sha256
        or not isinstance(payload.get("days"), list)
    ):
        raise ValueError("settlement-proxy forward index drift")
    previous = None
    for item in payload["days"]:
        day = str(item.get("day") or "") if isinstance(item, Mapping) else ""
        path = state / DAYS_DIR / f"{day}.json"
        if (
            not re.fullmatch(r"\d{8}", day)
            or (previous is not None and day != _next_day(previous))
            or not path.is_file()
            or _sha256(path) != item.get("sha256")
        ):
            raise ValueError("settlement-proxy forward day ledger drift")
        previous = day
    return payload


def update(
    day_result: str | Path,
    state_dir: str | Path,
    *,
    fingerprint: str,
    calibration_sha256: str,
    provenance_sha256: str,
    evaluator_sha256: str,
    holdout_day: str,
) -> dict[str, Any]:
    source = Path(day_result)
    incoming = json.loads(source.read_text(encoding="utf-8"))
    day = validate_day_result(
        incoming,
        fingerprint=fingerprint,
        calibration_sha256=calibration_sha256,
        provenance_sha256=provenance_sha256,
        evaluator_sha256=evaluator_sha256,
    )
    state = Path(state_dir)
    state.mkdir(parents=True, exist_ok=True)
    index = _load_index(
        state,
        fingerprint=fingerprint,
        calibration_sha256=calibration_sha256,
        provenance_sha256=provenance_sha256,
        evaluator_sha256=evaluator_sha256,
    )
    existing_days = [str(item["day"]) for item in index["days"]]
    expected = holdout_day if not existing_days else _next_day(existing_days[-1])
    destination = state / DAYS_DIR / f"{day}.json"
    incoming_sha256 = _sha256(source)
    if day in existing_days:
        recorded = index["days"][existing_days.index(day)]
        if recorded["sha256"] != incoming_sha256 or not destination.is_file():
            raise ValueError("settlement-proxy admitted day changed")
    else:
        if day != expected:
            raise ValueError(f"day is not the next chronological admission: expected {expected}")
        if (state / VERDICT).exists():
            raise ValueError("terminal settlement-proxy sample cannot accept another day")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".tmp")
        shutil.copyfile(source, temporary)
        if _sha256(temporary) != incoming_sha256:
            raise ValueError("settlement-proxy day copy changed")
        temporary.replace(destination)
        index["days"].append({"day": day, "sha256": incoming_sha256})
        _atomic_json(state / INDEX, index)

    payloads = [
        json.loads((state / DAYS_DIR / f"{item['day']}.json").read_text(encoding="utf-8"))
        for item in index["days"]
    ]
    for payload in payloads:
        validate_day_result(
            payload,
            fingerprint=fingerprint,
            calibration_sha256=calibration_sha256,
            provenance_sha256=provenance_sha256,
            evaluator_sha256=evaluator_sha256,
        )
    result = evaluate_day_payloads(payloads)
    status = {
        "schema": STATUS_SCHEMA,
        "strategy_fingerprint": fingerprint,
        "calibration_sha256": calibration_sha256,
        "provenance_sha256": provenance_sha256,
        "evaluator_sha256": evaluator_sha256,
        "index_sha256": _sha256(state / INDEX),
        "result": result,
    }
    _atomic_json(state / STATUS, status)
    if result["verdict"]["status"] != "collecting":
        verdict = {
            "schema": VERDICT_SCHEMA,
            "strategy_fingerprint": fingerprint,
            "calibration_sha256": calibration_sha256,
            "provenance_sha256": provenance_sha256,
            "evaluator_sha256": evaluator_sha256,
            "index_sha256": status["index_sha256"],
            "verdict": result["verdict"],
        }
        if (state / VERDICT).is_file():
            existing = json.loads((state / VERDICT).read_text(encoding="utf-8"))
            if existing != verdict:
                raise ValueError("pinned settlement-proxy verdict changed")
        else:
            _atomic_json(state / VERDICT, verdict)
    return result
