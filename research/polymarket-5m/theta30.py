"""Near-expiry favourite strategy for Polymarket BTC five-minute markets.

This candidate was specified in the 2026-09-08 independent-data report before
the later Dublin datasets were inspected: with 30 seconds remaining, buy the
current favourite only when its ask is 0.80--0.97.  A real order uses a fixed
limit no more than one cent above the decision ask and pays the crypto taker
fee.  It is a behavioural/theta hypothesis, not a cross-venue latency race.
"""
from __future__ import annotations

import argparse
import gzip
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

import binary as bo
from asian180 import BookView, persistent_taker_fill, received_book_at


@dataclass(frozen=True)
class Theta30Spec:
    strategy_id: str = "THETA30-FAVOURITE-V1"
    remaining_s: int = 30
    min_ask: float = 0.80
    max_ask: float = 0.97
    limit_slack: float = 0.01
    tick: float = 0.001
    max_limit: float = 0.99
    shares: float = 5.0
    max_book_age_ms: int = 250
    match_start_ms: int = 150
    persistence_ms: int = 200
    fee_rate: float = bo.CRYPTO_FEE_RATE
    insurance: str = "profitable_lock_after_confirmed_fill_only"

    def fingerprint(self) -> str:
        body = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(body.encode()).hexdigest()


@dataclass(frozen=True)
class ThetaOrder:
    side_up: bool
    decision_ask: float
    fixed_limit: float
    decision_mid_up: float


def select_order(book: BookView, spec: Theta30Spec) -> ThetaOrder | None:
    up_bid = 1.0 - book.ask_down
    mid_up = (up_bid + book.ask_up) / 2.0
    side_up = bool(mid_up >= .5)
    ask = book.ask_up if side_up else book.ask_down
    size = book.ask_up_size if side_up else book.ask_down_size
    if not (spec.min_ask - 1e-12 <= ask <= spec.max_ask + 1e-12) or size < spec.shares:
        return None
    limit = math.ceil((ask + spec.limit_slack - 1e-12) / spec.tick) * spec.tick
    limit = min(spec.max_limit, limit)
    return ThetaOrder(side_up, float(ask), float(round(limit, 6)), float(mid_up))


def replay_market(
    market: pd.Series,
    book_rows: pd.DataFrame,
    spec: Theta30Spec,
    *,
    execution_shift_ms: int = 0,
) -> dict[str, Any]:
    start_ts = int(market["start_ts"])
    decision_ms = (start_ts + 300 - spec.remaining_s) * 1_000
    base = {
        "market_id": market["market_id"],
        "day": datetime.fromtimestamp(start_ts, timezone.utc).date().isoformat(),
        "decision_ms": decision_ms,
        "sent": False,
        "filled": False,
    }
    book = received_book_at(book_rows, decision_ms=decision_ms, max_age_ms=spec.max_book_age_ms)
    if book is None:
        return {**base, "reason": "no_causal_book"}
    order = select_order(book, spec)
    if order is None:
        return {**base, "reason": "outside_frozen_band_or_capacity"}
    start = decision_ms + spec.match_start_ms + execution_shift_ms
    fill = persistent_taker_fill(
        book_rows,
        side_up=order.side_up,
        fixed_limit=order.fixed_limit,
        shares=spec.shares,
        start_ms=start,
        end_ms=start + spec.persistence_ms,
        fee_rate=spec.fee_rate,
    )
    sent = {
        **base,
        "sent": True,
        "side_up": order.side_up,
        "decision_ask": order.decision_ask,
        "decision_mid_up": order.decision_mid_up,
        "fixed_limit": order.fixed_limit,
    }
    if fill is None:
        return {**sent, "reason": "no_persistent_executable_depth"}
    won_up = str(market["winner"]).lower() == "up"
    won = won_up if order.side_up else not won_up
    pnl_per_share = float(won) - fill.all_in_cost
    return {
        **sent,
        "filled": True,
        "reason": "filled_proxy",
        "fill_price": fill.price,
        "fill_fee": fill.fee,
        "all_in_cost": fill.all_in_cost,
        "won": bool(won),
        "pnl_per_share": pnl_per_share,
        "pnl": pnl_per_share * spec.shares,
    }


def replay_dataset(
    markets: pd.DataFrame,
    books: pd.DataFrame,
    spec: Theta30Spec | None = None,
    *,
    execution_shift_ms: int = 0,
) -> pd.DataFrame:
    spec = Theta30Spec() if spec is None else spec
    grouped = {key: value for key, value in books.groupby("market_id", sort=False)}
    rows = []
    for market in markets[markets["winner"].notna()].sort_values("start_ts", kind="stable").itertuples(index=False):
        series = pd.Series(market._asdict())
        book = grouped.get(series["market_id"], books.iloc[:0])
        rows.append(replay_market(series, book, spec, execution_shift_ms=execution_shift_ms))
    return pd.DataFrame(rows)


def _first_level(raw: str) -> tuple[float, float]:
    try:
        levels = json.loads(raw)
        if levels:
            return float(levels[0][0]), float(levels[0][1])
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    return float("nan"), float("nan")


def load_latency_export(root: str | Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load ``recording.py``'s compact exchange/receive-stamped export."""
    root = Path(root)
    registry = pd.read_csv(root / "runtime_1000.market_registry.csv.gz")
    outcomes = pd.read_csv(root / "runtime_1000.market_outcomes.csv.gz")
    markets = registry[["market_id", "start_ts"]].merge(
        outcomes[["market_id", "winner"]], on="market_id", how="left", validate="one_to_one"
    )
    raw = pd.read_csv(root / "runtime_1000.poly_probability_observations_v1.csv.gz")
    bids = [_first_level(value) for value in raw["bids_json"]]
    asks = [_first_level(value) for value in raw["asks_json"]]
    books = pd.DataFrame({
        "market_id": raw["market_id"],
        "src_ms": np.rint(raw["source_ts"].to_numpy(float) * 1_000).astype(np.int64),
        "rcv_ms": np.rint(raw["receive_ts"].to_numpy(float) * 1_000).astype(np.int64),
        "b1p": [value[0] for value in bids],
        "b1s": [value[1] for value in bids],
        "a1p": [value[0] for value in asks],
        "a1s": [value[1] for value in asks],
    })
    for level in range(2, 6):
        books[f"b{level}p"] = np.nan
        books[f"b{level}s"] = np.nan
        books[f"a{level}p"] = np.nan
        books[f"a{level}s"] = np.nan
    books = books.sort_values(["market_id", "src_ms"], kind="stable").reset_index(drop=True)
    return markets, books


def combine_forward(prior: pd.DataFrame, new: pd.DataFrame, *, freeze_ms: int) -> pd.DataFrame:
    """Append without backfill or overlap selection; the first recording owns a market."""
    parts = [frame for frame in (prior, new) if not frame.empty]
    if not parts:
        return pd.DataFrame()
    combined = pd.concat(parts, ignore_index=True)
    combined = combined[combined["decision_ms"] >= freeze_ms]
    combined = combined.drop_duplicates("market_id", keep="first")
    return combined.sort_values(["decision_ms", "market_id"], kind="stable").reset_index(drop=True)


def _iso_ms(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1_000)


def append_forward(
    rows: pd.DataFrame,
    *,
    manifest_path: str | Path,
    store_path: str | Path,
) -> pd.DataFrame:
    manifest = json.loads(Path(manifest_path).read_text())
    freeze_ms = _iso_ms(manifest["frozen_at"])
    store_path = Path(store_path)
    prior = pd.read_csv(store_path) if store_path.exists() else pd.DataFrame()
    combined = combine_forward(prior, rows, freeze_ms=freeze_ms)
    store_path.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(store_path, index=False)
    return combined


def forward_verdict(rows: pd.DataFrame, manifest: dict[str, Any]) -> dict[str, Any]:
    from asian180 import forward_gate

    fills = rows[rows["filled"]].sort_values(["decision_ms", "market_id"], kind="stable")
    stop_n = int(manifest["stopping_rule"]["first_fills"])
    judged = fills.head(stop_n)
    gate = forward_gate(
        judged,
        alpha=float(manifest["alpha"]),
        min_fills=stop_n,
        min_days=int(manifest["minimum_days"]),
    )
    exact_p = float(bo.fair_price_pvalue(
        judged["pnl_per_share"].to_numpy(float),
        judged["all_in_cost"].to_numpy(float),
        sims=200_000,
        seed=300_001,
    )) if len(judged) else 1.0
    passed = bool(gate.passed and exact_p < float(manifest["alpha"]))
    reason = "passed_paper_gate" if passed else gate.reason
    if gate.passed and not passed:
        reason = "exact_p_not_significant"
    return {
        "passed": passed,
        "reason": reason,
        "fills": int(len(judged)),
        "days": int(judged["day"].nunique()) if len(judged) else 0,
        "mean_pnl_per_share": float(judged["pnl_per_share"].mean()) if len(judged) else float("nan"),
        "day_lower_bound": gate.lower_bound,
        "exact_p": exact_p,
    }


def render_forward_report(rows: pd.DataFrame, manifest: dict[str, Any]) -> str:
    overall = summarize(rows)
    verdict = forward_verdict(rows, manifest)
    return "\n".join([
        "# Theta30 fresh-forward monitor",
        "",
        f"**Status:** `{verdict['reason']}`. This is paper execution evidence, not yet a claim of live profit.",
        "",
        f"Frozen at: `{manifest['frozen_at']}`",
        f"Strategy fingerprint: `{manifest['strategy_fingerprint']}`",
        "",
        f"- Post-freeze markets: {overall['markets']}",
        f"- Orders / persistent-depth fills: {overall['sent']} / {overall['fills']} ({overall['fill_rate']:.1%})",
        f"- Judged fills / days: {verdict['fills']} / {verdict['days']}",
        f"- Net EV: {verdict['mean_pnl_per_share'] * 100:+.2f} cents/share",
        f"- One-sided day-cluster lower bound: {verdict['day_lower_bound'] * 100:+.2f} cents/share",
        f"- Fair-price exact p: {verdict['exact_p']:.4g}",
        "",
        "Passing the paper gate requires the first frozen fill sample to clear both the 1% day-cluster lower-bound and exact fair-price tests. Genuine exchange fills are a separate required gate.",
        "",
    ])


def summarize(rows: pd.DataFrame, *, sims: int = 100_000) -> dict[str, Any]:
    sent = rows[rows["sent"]]
    fills = rows[rows["filled"]]
    return {
        "markets": int(len(rows)),
        "sent": int(len(sent)),
        "fills": int(len(fills)),
        "fill_rate": float(len(fills) / len(sent)) if len(sent) else 0.0,
        "wins": int(fills["won"].sum()) if len(fills) else 0,
        "days": int(fills["day"].nunique()) if len(fills) else 0,
        "mean_pnl_per_share": float(fills["pnl_per_share"].mean()) if len(fills) else float("nan"),
        "total_pnl": float(fills["pnl"].sum()) if len(fills) else 0.0,
        "fair_price_p": float(bo.fair_price_pvalue(
            fills["pnl_per_share"].to_numpy(float),
            fills["all_in_cost"].to_numpy(float),
            sims=sims,
            seed=30,
        )) if len(fills) else 1.0,
    }


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--markets")
    parser.add_argument("--books")
    parser.add_argument("--latency-dir")
    parser.add_argument("--out", required=True)
    parser.add_argument("--execution-shift-ms", type=int, default=0)
    parser.add_argument("--manifest")
    parser.add_argument("--append")
    parser.add_argument("--report")
    args = parser.parse_args(argv)
    if args.latency_dir:
        markets, books = load_latency_export(args.latency_dir)
    elif args.markets and args.books:
        markets, books = pd.read_parquet(args.markets), pd.read_parquet(args.books)
    else:
        parser.error("provide --latency-dir or both --markets and --books")
    rows = replay_dataset(
        markets,
        books,
        execution_shift_ms=args.execution_shift_ms,
    )
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    rows.to_csv(args.out, index=False)
    if args.append:
        if not args.manifest:
            parser.error("--append requires --manifest")
        rows = append_forward(rows, manifest_path=args.manifest, store_path=args.append)
    if args.report:
        if not args.manifest:
            parser.error("--report requires --manifest")
        manifest = json.loads(Path(args.manifest).read_text())
        Path(args.report).write_text(render_forward_report(rows, manifest))
    print(json.dumps(summarize(rows), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
