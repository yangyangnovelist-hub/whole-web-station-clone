"""Audit whether fast Deribit DVOL changes can directly reprice a 5m binary.

This is deliberately a narrow structural test, not a direction model.  Holding
the expected settlement distance fixed, a digital probability ``p`` observed
at volatility ``old`` becomes::

    Phi(Phi^-1(p) * old / new)

after a volatility-only shock.  If even the largest receipt-causal DVOL shock
cannot move any binary probability by one Polymarket tick, there is no direct
taker strategy to replay against the CLOB.  A claim that DVOL predicts later
spot direction or realised volatility would be a different economic family.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
from bisect import bisect_right
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import NormalDist
from typing import Iterable, Iterator, Sequence


_NORMAL = NormalDist()
ONE_CENT = 0.01
DEFAULT_HORIZONS_MS = (1_000.0, 5_000.0, 10_000.0, 30_000.0, 60_000.0, 120_000.0, 300_000.0)


@dataclass(frozen=True)
class DvolPoint:
    receive_ms: float
    volatility: float
    source_ms: float | None = None


@dataclass(frozen=True)
class HorizonAudit:
    horizon_ms: float
    observations: int
    max_absolute_points: float
    max_relative_move: float
    max_probability_shift: float
    crosses_one_cent_tick: bool


def parse_dvol_line(line: str) -> DvolPoint | None:
    """Parse one recorder row, using receipt rather than venue time."""
    try:
        receive_ns, raw = line.split("\t", 1)
        payload = json.loads(raw)
        params = payload.get("params") or {}
        if params.get("channel") != "deribit_volatility_index.btc_usd":
            return None
        data = params.get("data") or {}
        value = float(data.get("volatility"))
        source_ms = float(data["timestamp"]) if data.get("timestamp") is not None else None
        receive_ms = int(receive_ns) / 1_000_000.0
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if not (math.isfinite(receive_ms) and math.isfinite(value) and value > 0):
        return None
    if source_ms is not None and not math.isfinite(source_ms):
        source_ms = None
    return DvolPoint(receive_ms=receive_ms, volatility=value, source_ms=source_ms)


def iter_dvol(paths: Iterable[str | Path]) -> Iterator[DvolPoint]:
    for path_value in paths:
        path = Path(path_value)
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                point = parse_dvol_line(line)
                if point is not None:
                    yield point


def reprice_probability(prior: float, old_volatility: float, new_volatility: float) -> float:
    if not (0.0 < prior < 1.0):
        raise ValueError("prior probability must be strictly between zero and one")
    if not (math.isfinite(old_volatility) and old_volatility > 0):
        raise ValueError("old volatility must be positive and finite")
    if not (math.isfinite(new_volatility) and new_volatility > 0):
        raise ValueError("new volatility must be positive and finite")
    z_score = _NORMAL.inv_cdf(prior)
    return _NORMAL.cdf(z_score * old_volatility / new_volatility)


def maximum_probability_shift(new_over_old: float) -> float:
    """Exact supremum over all starting probabilities for one vol ratio."""
    if not (math.isfinite(new_over_old) and new_over_old > 0):
        raise ValueError("volatility ratio must be positive and finite")
    if math.isclose(new_over_old, 1.0, rel_tol=0.0, abs_tol=1e-15):
        return 0.0
    denominator = 1.0 - 1.0 / (new_over_old * new_over_old)
    z_squared = 2.0 * math.log(new_over_old) / denominator
    z_score = math.sqrt(max(z_squared, 0.0))
    return abs(_NORMAL.cdf(z_score / new_over_old) - _NORMAL.cdf(z_score))


def audit_horizon(points: Sequence[DvolPoint], horizon_ms: float) -> HorizonAudit:
    if not (math.isfinite(horizon_ms) and horizon_ms > 0):
        raise ValueError("horizon must be positive and finite")
    ordered = sorted(points, key=lambda point: point.receive_ms)
    receives = [point.receive_ms for point in ordered]
    max_absolute = 0.0
    max_relative = 0.0
    max_shift = 0.0
    observations = 0
    for current in ordered:
        prior_index = bisect_right(receives, current.receive_ms - horizon_ms) - 1
        if prior_index < 0:
            continue
        prior = ordered[prior_index]
        ratio = current.volatility / prior.volatility
        max_absolute = max(max_absolute, abs(current.volatility - prior.volatility))
        max_relative = max(max_relative, abs(ratio - 1.0))
        max_shift = max(max_shift, maximum_probability_shift(ratio))
        observations += 1
    return HorizonAudit(
        horizon_ms=horizon_ms,
        observations=observations,
        max_absolute_points=max_absolute,
        max_relative_move=max_relative,
        max_probability_shift=max_shift,
        crosses_one_cent_tick=max_shift >= ONE_CENT,
    )


def audit(points: Sequence[DvolPoint], horizons_ms: Sequence[float] = DEFAULT_HORIZONS_MS) -> list[HorizonAudit]:
    return [audit_horizon(points, horizon) for horizon in horizons_ms]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="Deribit recorder .txt or .txt.gz files")
    parser.add_argument("--horizon-ms", type=float, action="append", dest="horizons")
    args = parser.parse_args(argv)
    points = list(iter_dvol(args.paths))
    if not points:
        raise SystemExit("no valid DVOL observations")
    rows = audit(points, args.horizons or DEFAULT_HORIZONS_MS)
    result = {
        "points": len(points),
        "first_receive_ms": min(point.receive_ms for point in points),
        "last_receive_ms": max(point.receive_ms for point in points),
        "horizons": [asdict(row) for row in rows],
        "verdict": "replay_required" if any(row.crosses_one_cent_tick for row in rows) else "reject_below_tick",
    }
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
