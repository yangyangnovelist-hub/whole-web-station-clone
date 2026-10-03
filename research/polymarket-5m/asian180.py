"""Causal fixed-time research for Polymarket BTC five-minute Asian binaries.

The strategy deliberately avoids the stale-quote race.  It schedules one
decision two minutes after the market opens, estimates the probability that
the closing 60-second average beats the opening 60-second average, and sends a
fixed-limit FAK only when the all-in model edge is large.  Historical fills are
counted only when executable depth survives a conservative venue-time window.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy.stats import t as student_t

import binary as bo


@dataclass(frozen=True)
class Asian180Spec:
    """The single frozen candidate nominated by the exploratory search."""

    strategy_id: str = "ASIAN180-V1"
    elapsed_s: int = 120
    strike_window_s: int = 60
    settlement_window_s: int = 60
    sigma_window_s: int = 600
    sigma_min_samples: int = 300
    sigma_multiplier: float = 1.5
    min_edge: float = 0.075
    shares: float = 5.0
    spot_receive_delay_ms: int = 110
    max_book_age_ms: int = 250
    match_start_ms: int = 150
    persistence_ms: int = 200
    min_price: float = 0.02
    max_price: float = 0.98
    fee_rate: float = bo.CRYPTO_FEE_RATE
    insurance: str = "profitable_lock_after_confirmed_fill_only"

    def fingerprint(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class SpotSnapshot:
    current_log: float
    strike_log: float
    sigma_s: float
    cutoff_ms: int
    feature_max_ms: int


class SpotHistory:
    """Binance trade history indexed by exchange time.

    A configurable receive-delay haircut converts exchange time into the data
    that could have reached the strategy.  Volatility uses completed one-second
    bars only, preventing a partial future second from entering the estimate.
    """

    def __init__(self, trades: pd.DataFrame):
        required = {"T_us", "price"}
        missing = required - set(trades.columns)
        if missing:
            raise ValueError(f"missing spot columns: {sorted(missing)}")
        ordered = trades.sort_values("T_us", kind="stable")
        self.t_ms = ordered["T_us"].to_numpy(np.float64) / 1_000.0
        prices = ordered["price"].to_numpy(float)
        if len(prices) == 0 or np.any(prices <= 0):
            raise ValueError("spot prices must be positive and non-empty")
        self.log_price = np.log(prices)
        seconds = np.floor(self.t_ms / 1_000.0).astype(np.int64)
        self.second_log = pd.Series(self.log_price, index=seconds).groupby(level=0).last()

    def snapshot(
        self,
        *,
        start_ts: int,
        decision_ms: int,
        receive_delay_ms: int,
        strike_window_s: int = 60,
        sigma_window_s: int = 600,
        sigma_min_samples: int = 300,
        official_strike_log: float | None = None,
    ) -> SpotSnapshot:
        cutoff_ms = int(decision_ms - receive_delay_ms)
        current_index = int(np.searchsorted(self.t_ms, cutoff_ms, side="right") - 1)
        if current_index < 0:
            raise ValueError("no spot trade is observable by the decision cutoff")

        if official_strike_log is None:
            strike_seconds = np.arange(start_ts - strike_window_s, start_ts, dtype=np.int64)
            strike_values = self.second_log.reindex(strike_seconds)
            if strike_values.isna().any():
                raise ValueError("opening strike window has missing one-second samples")
            strike_log = float(strike_values.mean())
        else:
            strike_log = float(official_strike_log)
            if not np.isfinite(strike_log):
                raise ValueError("official strike is invalid")

        last_complete_second = int(math.floor(cutoff_ms / 1_000.0) - 1)
        sigma_seconds = np.arange(
            last_complete_second - sigma_window_s,
            last_complete_second + 1,
            dtype=np.int64,
        )
        sigma_prices = self.second_log.reindex(sigma_seconds).ffill(limit=2)
        returns = sigma_prices.diff().dropna()
        if len(returns) < sigma_min_samples or not np.isfinite(returns).all():
            raise ValueError("insufficient causal spot history for volatility")
        sigma_s = float(returns.iloc[-sigma_window_s:].std(ddof=1))
        if not np.isfinite(sigma_s) or sigma_s <= 0:
            raise ValueError("invalid realized volatility")

        feature_max_ms = int(self.t_ms[current_index])
        return SpotSnapshot(
            current_log=float(self.log_price[current_index]),
            strike_log=strike_log,
            sigma_s=sigma_s,
            cutoff_ms=cutoff_ms,
            feature_max_ms=feature_max_ms,
        )

    def average_log(self, start_s: int, end_s: int) -> float:
        values = self.second_log.reindex(np.arange(start_s, end_s, dtype=np.int64))
        if values.isna().any() or values.empty:
            raise ValueError("averaging window has missing one-second samples")
        return float(values.mean())


@dataclass(frozen=True)
class BookView:
    src_ms: int
    rcv_ms: int
    ask_up: float
    ask_down: float
    ask_up_size: float
    ask_down_size: float


def _valid_top(row: pd.Series) -> bool:
    values = [row.get("b1p"), row.get("a1p"), row.get("b1s"), row.get("a1s")]
    if not np.isfinite(values).all():
        return False
    return 0 < row["b1p"] < row["a1p"] < 1 and row["b1s"] >= 0 and row["a1s"] >= 0


def received_book_at(rows: pd.DataFrame, *, decision_ms: int, max_age_ms: int) -> BookView | None:
    """Last non-regressive source book actually received by ``decision_ms``."""
    if rows.empty:
        return None
    source = rows["src_ms"].to_numpy(np.int64)
    received = rows["rcv_ms"].to_numpy(np.int64)
    bid = rows["b1p"].to_numpy(float)
    ask = rows["a1p"].to_numpy(float)
    bid_size = rows["b1s"].to_numpy(float)
    ask_size = rows["a1s"].to_numpy(float)
    valid = (
        (received <= decision_ms)
        & np.isfinite(bid) & np.isfinite(ask)
        & np.isfinite(bid_size) & np.isfinite(ask_size)
        & (bid > 0) & (bid < ask) & (ask < 1)
        & (bid_size >= 0) & (ask_size >= 0)
    )
    positions = np.flatnonzero(valid)
    if not len(positions):
        return None
    newest_source = source[positions].max()
    positions = positions[source[positions] == newest_source]
    position = int(positions[np.argmax(received[positions])])
    accepted = rows.iloc[position]
    if decision_ms - int(accepted["src_ms"]) > max_age_ms:
        return None
    return BookView(
        src_ms=int(accepted["src_ms"]),
        rcv_ms=int(accepted["rcv_ms"]),
        ask_up=float(accepted["a1p"]),
        ask_down=float(1.0 - accepted["b1p"]),
        ask_up_size=float(accepted["a1s"]),
        ask_down_size=float(accepted["b1s"]),
    )


def max_profitable_limit(
    fair: float,
    min_edge: float,
    *,
    fee_rate: float = bo.CRYPTO_FEE_RATE,
    min_price: float = 0.02,
    max_price: float = 0.98,
) -> float | None:
    """Highest cent limit whose all-in model edge still clears the threshold."""
    ticks = np.arange(math.ceil(min_price * 100), math.floor(max_price * 100) + 1) / 100.0
    valid = ticks[fair - ticks - bo.taker_fee(ticks, fee_rate) >= min_edge - 1e-12]
    return None if not len(valid) else float(valid[-1])


def _levels(row: pd.Series, side_up: bool) -> list[tuple[float, float]]:
    result = []
    for level in range(1, 6):
        if side_up:
            price, size = row.get(f"a{level}p"), row.get(f"a{level}s")
        else:
            up_bid, size = row.get(f"b{level}p"), row.get(f"b{level}s")
            price = np.nan if not np.isfinite(up_bid) else 1.0 - float(up_bid)
        if np.isfinite(price) and np.isfinite(size) and 0 < price < 1 and size > 0:
            result.append((float(price), float(size)))
    return sorted(result)


@dataclass(frozen=True)
class Fill:
    price: float
    fee: float
    all_in_cost: float
    shares: float
    start_ms: int
    end_ms: int


def _executable_cost(row: pd.Series, side_up: bool, fixed_limit: float, shares: float, fee_rate: float):
    remaining = shares
    notional = 0.0
    fees = 0.0
    for price, available in _levels(row, side_up):
        if price > fixed_limit + 1e-9:
            break
        take = min(remaining, available)
        notional += take * price
        fees += take * float(bo.taker_fee(price, fee_rate))
        remaining -= take
        if remaining <= 1e-9:
            return notional / shares, fees / shares
    return None


def persistent_taker_fill(
    rows: pd.DataFrame,
    *,
    side_up: bool,
    fixed_limit: float,
    shares: float,
    start_ms: int,
    end_ms: int,
    fee_rate: float = bo.CRYPTO_FEE_RATE,
) -> Fill | None:
    """Conservative FAK proxy requiring executable depth throughout a window.

    The limit is fixed before replay.  The source-time book at the start and
    every subsequent update through the end must support the full order.  The
    worst executable VWAP/fee state is charged.
    """
    if rows.empty or end_ms < start_ms or shares <= 0:
        return None
    source = rows["src_ms"].to_numpy(np.int64)
    if np.any(np.diff(source) < 0):
        ordered = rows.sort_values("src_ms", kind="stable")
        source = ordered["src_ms"].to_numpy(np.int64)
    else:
        ordered = rows
    first = int(np.searchsorted(source, start_ms, side="right") - 1)
    if first < 0:
        return None
    stop = int(np.searchsorted(source, end_ms, side="right"))
    states = (row for _, row in ordered.iloc[first:stop].iterrows())
    worst_price = -np.inf
    worst_fee = -np.inf
    worst_cost = -np.inf
    for row in states:
        executable = _executable_cost(row, side_up, fixed_limit, shares, fee_rate)
        if executable is None:
            return None
        price, fee = executable
        cost = price + fee
        if cost > worst_cost:
            worst_price, worst_fee, worst_cost = price, fee, cost
    return Fill(
        price=float(worst_price),
        fee=float(worst_fee),
        all_in_cost=float(worst_cost),
        shares=float(shares),
        start_ms=int(start_ms),
        end_ms=int(end_ms),
    )


def profitable_lock_price(
    first_price: float,
    hedge_price: float,
    *,
    first_fill_confirmed: bool,
    fee_rate: float = bo.CRYPTO_FEE_RATE,
) -> float | None:
    if not first_fill_confirmed:
        return None
    if bool(bo.can_lock_profit(first_price, hedge_price, rate=fee_rate)):
        return float(hedge_price)
    return None


def _pick_order(q_up: float, book: BookView, spec: Asian180Spec):
    choices = []
    for side_up, fair, ask, size in (
        (True, q_up, book.ask_up, book.ask_up_size),
        (False, 1.0 - q_up, book.ask_down, book.ask_down_size),
    ):
        limit = max_profitable_limit(
            fair,
            spec.min_edge,
            fee_rate=spec.fee_rate,
            min_price=spec.min_price,
            max_price=spec.max_price,
        )
        edge = fair - ask - float(bo.taker_fee(ask, spec.fee_rate))
        if limit is not None and ask <= limit + 1e-9 and size >= spec.shares and edge >= spec.min_edge - 1e-12:
            choices.append((edge, side_up, fair, ask, limit))
    return None if not choices else max(choices, key=lambda item: item[0])


def replay_market(
    market: pd.Series,
    book_rows: pd.DataFrame,
    spot: SpotHistory,
    spec: Asian180Spec,
    *,
    execution_shift_ms: int = 0,
) -> dict[str, Any]:
    start_ts = int(market["start_ts"])
    decision_ms = (start_ts + spec.elapsed_s) * 1_000
    base = {
        "market_id": market["market_id"],
        "day": datetime.fromtimestamp(start_ts, timezone.utc).date().isoformat(),
        "decision_ms": decision_ms,
        "sent": False,
        "filled": False,
        "reason": "",
    }
    try:
        official_strike_log = None
        if "strike_price" in market and pd.notna(market["strike_price"]):
            strike_price = float(market["strike_price"])
            if strike_price <= 0:
                return {**base, "reason": "invalid_official_strike"}
            available_ms = market.get("strike_receive_ms", np.nan)
            if pd.notna(available_ms) and float(available_ms) > decision_ms:
                return {**base, "reason": "official_strike_not_yet_received"}
            official_strike_log = math.log(strike_price)
        snapshot = spot.snapshot(
            start_ts=start_ts,
            decision_ms=decision_ms,
            receive_delay_ms=spec.spot_receive_delay_ms,
            strike_window_s=spec.strike_window_s,
            sigma_window_s=spec.sigma_window_s,
            sigma_min_samples=spec.sigma_min_samples,
            official_strike_log=official_strike_log,
        )
    except ValueError as exc:
        return {**base, "reason": f"spot:{exc}"}
    book = received_book_at(book_rows, decision_ms=decision_ms, max_age_ms=spec.max_book_age_ms)
    if book is None:
        return {**base, "reason": "no_causal_book"}
    q_up = float(bo.prob_up(
        snapshot.current_log,
        snapshot.strike_log,
        snapshot.sigma_s * spec.sigma_multiplier,
        spec.elapsed_s,
        window=bo.WINDOW_S,
        twap=spec.settlement_window_s,
    ))
    order = _pick_order(q_up, book, spec)
    if order is None:
        return {**base, "q_up": q_up, "reason": "edge_or_capacity"}
    decision_edge, side_up, fair, decision_ask, fixed_limit = order
    fill_start = decision_ms + spec.match_start_ms + execution_shift_ms
    fill_end = fill_start + spec.persistence_ms
    fill = persistent_taker_fill(
        book_rows,
        side_up=side_up,
        fixed_limit=fixed_limit,
        shares=spec.shares,
        start_ms=fill_start,
        end_ms=fill_end,
        fee_rate=spec.fee_rate,
    )
    row = {
        **base,
        "sent": True,
        "q_up": q_up,
        "side_up": bool(side_up),
        "fair": fair,
        "decision_ask": decision_ask,
        "decision_edge": decision_edge,
        "fixed_limit": fixed_limit,
        "feature_max_ms": snapshot.feature_max_ms,
        "sigma_s": snapshot.sigma_s,
        "strike_source": "official" if official_strike_log is not None else "cross_venue_proxy",
    }
    if fill is None:
        return {**row, "reason": "no_persistent_executable_depth"}
    won_up = str(market["winner"]).lower() == "up"
    won = won_up if side_up else not won_up
    pnl_per_share = float(won) - fill.all_in_cost
    return {
        **row,
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
    spot_trades: pd.DataFrame,
    spec: Asian180Spec | None = None,
    *,
    execution_shift_ms: int = 0,
) -> pd.DataFrame:
    spec = Asian180Spec() if spec is None else spec
    spot = SpotHistory(spot_trades)
    grouped = {key: value for key, value in books.groupby("market_id", sort=False)}
    rows = []
    for market in markets.sort_values("start_ts", kind="stable").itertuples(index=False):
        series = pd.Series(market._asdict())
        book = grouped.get(series["market_id"], books.iloc[:0])
        rows.append(replay_market(series, book, spot, spec, execution_shift_ms=execution_shift_ms))
    return pd.DataFrame(rows)


@dataclass(frozen=True)
class FreezeManifest:
    strategy: Asian180Spec
    frozen_at: str
    discovery_through: str
    code_commit: str
    explored_variants: int
    alpha: float = 0.01
    min_forward_fills: int = 30
    min_forward_days: int = 5
    fingerprint: str = field(init=False)

    def __post_init__(self):
        datetime.fromisoformat(self.frozen_at.replace("Z", "+00:00"))
        payload = {
            "strategy": asdict(self.strategy),
            "frozen_at": self.frozen_at,
            "discovery_through": self.discovery_through,
            "code_commit": self.code_commit,
            "explored_variants": self.explored_variants,
            "alpha": self.alpha,
            "min_forward_fills": self.min_forward_fills,
            "min_forward_days": self.min_forward_days,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        object.__setattr__(self, "fingerprint", hashlib.sha256(encoded).hexdigest())


def write_freeze_manifest(path: str | Path, manifest: FreezeManifest) -> None:
    path = Path(path)
    payload = asdict(manifest)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if path.exists() and path.read_text() != text:
        raise FileExistsError("freeze manifest is immutable; refusing to overwrite it")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def load_freeze_manifest(path: str | Path) -> FreezeManifest:
    payload = json.loads(Path(path).read_text())
    recorded_fingerprint = payload.pop("fingerprint")
    payload["strategy"] = Asian180Spec(**payload["strategy"])
    manifest = FreezeManifest(**payload)
    if manifest.fingerprint != recorded_fingerprint:
        raise ValueError("freeze manifest fingerprint mismatch")
    return manifest


def _utc_ms(value: str) -> int:
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int(timestamp.timestamp() * 1_000)


def validate_forward_observations(observations: pd.DataFrame, manifest: FreezeManifest) -> None:
    required = {"market_id", "decision_ms"}
    missing = required - set(observations.columns)
    if missing:
        raise ValueError(f"missing forward columns: {sorted(missing)}")
    if (observations["decision_ms"] < _utc_ms(manifest.frozen_at)).any():
        raise ValueError("pre-freeze observation is not forward evidence")
    if observations["market_id"].duplicated().any():
        raise ValueError("duplicate forward market")


@dataclass(frozen=True)
class ForwardGate:
    passed: bool
    reason: str
    fills: int
    days: int
    mean_pnl: float
    lower_bound: float


def forward_gate(
    fills: pd.DataFrame,
    *,
    alpha: float = 0.01,
    min_fills: int = 30,
    min_days: int = 5,
) -> ForwardGate:
    n = len(fills)
    days = fills["day"].nunique() if n else 0
    mean = float(fills["pnl_per_share"].mean()) if n else float("nan")
    if n < min_fills:
        return ForwardGate(False, "insufficient_fills", n, days, mean, float("nan"))
    if days < min_days:
        return ForwardGate(False, "insufficient_days", n, days, mean, float("nan"))
    daily = fills.groupby("day", sort=True)["pnl_per_share"].mean().to_numpy(float)
    se = float(np.std(daily, ddof=1) / np.sqrt(len(daily))) if len(daily) > 1 else float("inf")
    lower = mean if se == 0 else mean - float(student_t.ppf(1.0 - alpha, len(daily) - 1)) * se
    passed = bool(np.isfinite(lower) and mean > 0 and lower > 0)
    return ForwardGate(passed, "passed" if passed else "lower_bound_not_positive", n, days, mean, lower)


def replay_summary(rows: pd.DataFrame) -> dict[str, Any]:
    sent = rows[rows["sent"]] if "sent" in rows else rows.iloc[:0]
    fills = rows[rows["filled"]] if "filled" in rows else rows.iloc[:0]
    return {
        "markets": int(len(rows)),
        "sent": int(len(sent)),
        "fills": int(len(fills)),
        "fill_rate": float(len(fills) / len(sent)) if len(sent) else 0.0,
        "mean_pnl_per_share": float(fills["pnl_per_share"].mean()) if len(fills) else float("nan"),
        "total_pnl": float(fills["pnl"].sum()) if len(fills) else 0.0,
        "wins": int(fills["won"].sum()) if len(fills) else 0,
        "days": int(fills["day"].nunique()) if len(fills) else 0,
        "fair_price_p": float(bo.fair_price_pvalue(
            fills["pnl_per_share"], fills["all_in_cost"], sims=100_000, seed=180
        )) if len(fills) else 1.0,
    }


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--markets", required=True)
    parser.add_argument("--books", required=True)
    parser.add_argument("--spot", required=True)
    parser.add_argument("--strikes", help="optional CSV with start_ts and official strike_price")
    parser.add_argument("--out", required=True)
    parser.add_argument("--execution-shift-ms", type=int, default=0)
    args = parser.parse_args(argv)
    markets = pd.read_parquet(args.markets)
    if args.strikes:
        strikes = pd.read_csv(args.strikes)
        markets = markets.merge(strikes[["start_ts", "strike_price"]], on="start_ts", how="left", validate="one_to_one")
    books = pd.read_parquet(args.books)
    spot = pd.read_parquet(args.spot)
    rows = replay_dataset(markets, books, spot, execution_shift_ms=args.execution_shift_ms)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    rows.to_csv(args.out, index=False)
    print(json.dumps(replay_summary(rows), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
