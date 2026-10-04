"""Idempotently pool strict current-H observations and pin its one-time paper verdict."""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from scipy.stats import t as student_t

import h_replay as replay
import h_replay_run as run


STORE = Path(__file__).with_name("forward") / "current-h-observations.jsonl"
REPORT = Path(__file__).with_name("forward") / "current-h.md"
PIN = Path(__file__).with_name("forward") / "current-h-verdict.json"
_FREEZE = replay.load_freeze()
STRATEGY_ID = str(_FREEZE["strategy_id"])
PRIMARY_DELAY_MS = float(_FREEZE["timing"].get("primary_evaluation_ms", 500.0))
FAMILY_TESTS = int(_FREEZE.get("statistics", {}).get("family_tests", 100))
MIN_FILLS = 1_000
MIN_DAYS = 7
ALPHA = 0.01


def _read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON") from exc
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_number}: expected object")
            rows.append(row)
    return rows


def observation_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (str(row["observation_run_id"]), str(row["market_id"]), float(row["evaluation_ms"]),
            float(row["signal_ms"]), str(row["direction"]),
            str(row.get("signal_source") or "unknown"))


def market_observation_key(row: dict[str, Any]) -> tuple[str, float]:
    return str(row["market_id"]), float(row["evaluation_ms"])


def add(new_rows: str | Path, store: str | Path = STORE) -> list[dict[str, Any]]:
    """Append unseen observations; the first recording of each market/latency always wins."""
    destination = Path(store)
    destination.parent.mkdir(parents=True, exist_ok=True)
    chosen_run: dict[tuple[str, float], str] = {}
    pooled: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in [*_read_jsonl(destination), *_read_jsonl(new_rows)]:
        if row.get("strategy_id") != STRATEGY_ID:
            raise ValueError(
                f"observation strategy_id {row.get('strategy_id')!r} does not match {STRATEGY_ID!r}"
            )
        run_id = str(row["observation_run_id"])
        market_key = market_observation_key(row)
        if chosen_run.setdefault(market_key, run_id) != run_id:
            continue
        pooled.setdefault(observation_key(row), row)
    rows = sorted(pooled.values(), key=lambda row: (float(row["signal_ms"]), str(row["market_id"]),
                                                     float(row["evaluation_ms"]), str(row["direction"])))
    with destination.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
    return rows


def _stopping_sample(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    fills = sorted((row for row in rows if float(row["evaluation_ms"]) == PRIMARY_DELAY_MS and
                    row.get("filled") and row.get("winner") is not None),
                   key=lambda row: (float(row["signal_ms"]), str(row["market_id"])))
    days = set()
    for index, row in enumerate(fills):
        days.add(str(row["day"]))
        if index + 1 >= MIN_FILLS and len(days) >= MIN_DAYS:
            return fills[:index + 1]
    return []


def _daily_lower_99(sample: list[dict[str, Any]]) -> float | None:
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in sample:
        by_day[str(row["day"])].append(row)
    clusters = [(sum(float(row["pnl"]) for row in rows),
                 sum(float(row["filled_shares"]) for row in rows)) for rows in by_day.values()]
    if len(clusters) < 2:
        return None
    total_pnl = sum(pnl for pnl, _ in clusters)
    total_shares = sum(shares for _, shares in clusters)
    estimate = total_pnl / total_shares
    influence = [pnl - estimate * shares for pnl, shares in clusters]
    cluster_count = len(clusters)
    standard_error = (math.sqrt(cluster_count / (cluster_count - 1) *
                                sum(value * value for value in influence)) / total_shares)
    return estimate - float(student_t.ppf(1.0 - ALPHA, cluster_count - 1)) * standard_error


def verdict(rows: list[dict[str, Any]]) -> dict[str, Any]:
    primary_fills = [row for row in rows if float(row["evaluation_ms"]) == PRIMARY_DELAY_MS and
                     row.get("filled") and row.get("winner") is not None]
    all_days = len({str(row["day"]) for row in primary_fills})
    sample = _stopping_sample(rows)
    if not sample:
        return {"status": "collecting", "fills": len(primary_fills), "days": all_days,
                "required_fills": MIN_FILLS, "required_days": MIN_DAYS}
    shares = sum(float(row["filled_shares"]) for row in sample)
    net_ev = sum(float(row["pnl"]) for row in sample) / shares
    lower = _daily_lower_99(sample)
    raw_exact_p = run._exact_pvalue(sample) if net_ev > 0 else 1.0
    corrected_exact_p = min(1.0, raw_exact_p * FAMILY_TESTS)
    passed = net_ev > 0 and lower is not None and lower > 0 and corrected_exact_p < ALPHA
    return {"status": "passed" if passed else "rejected", "fills": len(sample),
            "days": len({str(row["day"]) for row in sample}), "net_ev_per_share": net_ev,
            "day_cluster_lower_99": lower, "raw_exact_p": raw_exact_p,
            "corrected_exact_p": corrected_exact_p, "exact_p": corrected_exact_p,
            "family_tests": FAMILY_TESTS,
            "last_signal_ms": max(float(row["signal_ms"]) for row in sample)}


def pin_verdict(rows: list[dict[str, Any]], path: str | Path = PIN) -> dict[str, Any]:
    destination = Path(path)
    if destination.exists():
        payload = json.loads(destination.read_text(encoding="utf-8"))
        if payload.get("strategy_id") != STRATEGY_ID:
            raise ValueError("pinned current-H verdict belongs to another strategy")
        return payload
    result = verdict(rows)
    if result["status"] == "collecting":
        return result
    payload = {"strategy_id": STRATEGY_ID, "verdict": result}
    destination.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def report(rows: list[dict[str, Any]], pinned: dict[str, Any]) -> str:
    result = pinned.get("verdict", pinned)
    lines = ["# Current H strict forward test", "",
             "Timestamped spot-trade/futures-BBO race; effective-book FAK replay at measured latency.",
             "Public-book fills are a surviving-depth upper bound and require live FAK calibration.", "",
             f"Status: **{result['status']}**; {PRIMARY_DELAY_MS:g} ms fills/days: "
             f"{result['fills']}/{result['days']}."]
    if result["status"] != "collecting":
        lines += [f"Net EV/share: {100 * result['net_ev_per_share']:+.2f}¢.",
                  f"One-sided 99% day-cluster lower bound: {100 * result['day_cluster_lower_99']:+.2f}¢.",
                  f"Family-corrected exact fair-price p: {result['corrected_exact_p']:.4g} "
                  f"(raw {result['raw_exact_p']:.4g}, {result['family_tests']} tests)."]
    summary = run.summarize(rows, {})["latencies"] if rows else {}
    if summary:
        lines += ["", "| delay | signals | sent | fills | net EV/share |", "|---:|---:|---:|---:|---:|"]
        for delay, row in summary.items():
            ev = "—" if row["net_ev_per_share"] is None else f"{100 * row['net_ev_per_share']:+.2f}¢"
            lines.append(f"| {delay} ms | {row['signals']} | {row['sent']} | {row['fills']} | {ev} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("rows")
    parser.add_argument("--store", default=str(STORE))
    parser.add_argument("--report", default=str(REPORT))
    parser.add_argument("--verdict", default=str(PIN))
    args = parser.parse_args()
    rows = add(args.rows, args.store)
    pinned = pin_verdict(rows, args.verdict)
    Path(args.report).write_text(report(rows, pinned), encoding="utf-8")
    print(json.dumps(pinned, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
