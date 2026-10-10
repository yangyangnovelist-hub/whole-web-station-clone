"""Receipt-causal audit of non-atomic 5m/15m same-expiry BTC boxes."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import re
from typing import Any

import numpy as np
import pandas as pd


EPS = 1e-9
EVIDENCE_TIER = "aligned_100ms_snapshot_upper_bound"


@dataclass(frozen=True)
class BoxBatchConfig:
    latencies_ms: tuple[int, ...] = (200, 300, 400, 500)
    primary_latency_ms: int = 400
    max_cost: float = 0.98
    shares: float = 5.0
    min_buy_notional: float = 1.0
    persistence_ms: int = 100
    orphan_unwind_ms: int = 100
    freshness_ms: int = 2_000


def _fee_rate(stamp_ms: int) -> float:
    return 0.072 if stamp_ms < 1_782_864_000_000 else 0.07


def _fee(price: float, stamp_ms: int) -> float:
    return _fee_rate(stamp_ms) * price * (1.0 - price)


def _fee_total(price: float, shares: float, stamp_ms: int) -> float:
    decimal_price = Decimal(str(price))
    raw = (
        Decimal(str(shares))
        * Decimal(str(_fee_rate(stamp_ms)))
        * decimal_price
        * (Decimal(1) - decimal_price)
    )
    return float(raw.quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP))


def _fee_totals(prices: pd.Series, shares: float, stamps: np.ndarray) -> np.ndarray:
    """Vectorised positive half-up rounding matching five-decimal venue accounting."""
    values = pd.to_numeric(prices, errors="coerce").to_numpy(dtype=float)
    rates = np.where(stamps < 1_782_864_000_000, 0.072, 0.07)
    raw = float(shares) * rates * values * (1.0 - values)
    return np.floor(raw * 100_000.0 + 0.5 + 1e-12) / 100_000.0


def _normalize_exact_source(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip().lower().rstrip("/")
    if not text or "btc" not in text or "usd" not in text:
        return None
    if "twap-30" in text:
        return "chainlink_btc_usd_twap_30s"
    if "twap-60" in text:
        return "chainlink_btc_usd_twap_60s"
    if "chainlink" in text or "data.chain.link" in text:
        return "chainlink_btc_usd_point"
    return None


def _documented_source(market: pd.Series) -> str | None:
    try:
        horizon = int(market["horizon"])
        start_s = int(float(market["start"]) // 1_000)
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if horizon not in (5, 15):
        return None
    slug = str(market.get("slug") or "")
    match = re.fullmatch(rf"btc-updown-{horizon}m-(\d+)", slug)
    if match is None or int(match.group(1)) != start_s:
        return None
    import pm_outcomes

    window = int(pm_outcomes.rule_window(f"{horizon}m", np.asarray([start_s]))[0])
    return {
        0: "chainlink_btc_usd_point",
        30: "chainlink_btc_usd_twap_30s",
        60: "chainlink_btc_usd_twap_60s",
    }.get(window)


def _source(market: pd.Series) -> tuple[str | None, str | None, bool]:
    exact = None
    for column in (
        "resolution_source",
        "oracle_source",
    ):
        exact = _normalize_exact_source(market.get(column))
        if exact is not None:
            break
    documented = _documented_source(market)
    if exact is not None and documented is not None and exact != documented:
        return None, "source_annotation_conflict", True
    if exact is not None:
        return exact, "archive_exact", False
    if documented is not None:
        return documented, "documented_gamma_regime", False
    return None, None, False


def _base_row(
    lower: pd.Series,
    upper: pd.Series,
    *,
    source: str | None,
    source_basis: str | None,
    latency_ms: int | None,
    primary_latency_ms: int,
    shares: float,
) -> dict[str, Any]:
    return {
        "pair_id": f"{lower['market_id']}|{upper['market_id']}",
        "lower_market_id": str(lower["market_id"]),
        "upper_market_id": str(upper["market_id"]),
        "lower_horizon": int(lower["horizon"]),
        "upper_horizon": int(upper["horizon"]),
        "lower_slug": lower.get("slug"),
        "upper_slug": upper.get("slug"),
        "lower_side": "Up",
        "upper_side": "Down",
        "lower_token_id": lower.get("up_token"),
        "upper_token_id": upper.get("down_token"),
        "lower_up_won": lower.get("up_won"),
        "upper_up_won": upper.get("up_won"),
        "lower_raw_resolution_source": lower.get("resolution_price_source"),
        "upper_raw_resolution_source": upper.get("resolution_price_source"),
        "lower_raw_oracle_source": lower.get("oracle_source"),
        "upper_raw_oracle_source": upper.get("oracle_source"),
        "lower_strike": float(lower["k"]),
        "upper_strike": float(upper["k"]),
        "end_ms": int(lower["end"]),
        "resolution_source": source,
        "source_basis": source_basis,
        "source_verified": source is not None,
        "latency_ms": latency_ms,
        "primary": latency_ms == primary_latency_ms,
        "evidence_tier": EVIDENCE_TIER,
        "candidate_count": 0,
        "signal_count": 0,
        "shares": shares,
        "decision_ms": np.nan,
        "match_ms": np.nan,
        "unwind_ms": np.nan,
        "lower_limit": np.nan,
        "upper_limit": np.nan,
        "decision_cost_per_set": np.nan,
        "gross_floor_edge_per_set": np.nan,
        "lower_filled": False,
        "upper_filled": False,
        "paired_sets": 0.0,
        "entry_cost": 0.0,
        "exit_proceeds": 0.0,
        "fees": 0.0,
        "payout": 0.0,
        "bonus_payout": 0.0,
        "pnl": np.nan,
        "execution_valid": False,
        "failure": None,
        "status": None,
    }


def _snapshot(index: np.ndarray, target: int) -> int | None:
    position = int(np.searchsorted(index, target, side="left"))
    if position >= len(index) or int(index[position]) != target:
        return None
    return position


def _paired_books(
    books_by_market: dict[str, pd.DataFrame],
    lower_id: str,
    upper_id: str,
) -> pd.DataFrame:
    lower = books_by_market.get(lower_id)
    upper = books_by_market.get(upper_id)
    if lower is None or upper is None:
        return pd.DataFrame()
    return lower.join(upper, how="inner", lsuffix="_lower", rsuffix="_upper")


def _books_by_market(feat: pd.DataFrame, wanted: set[str]) -> dict[str, pd.DataFrame]:
    books: dict[str, pd.DataFrame] = {}
    for market_id, group in feat.groupby("market_id", sort=False):
        key = str(market_id)
        if key not in wanted:
            continue
        book = (
            group.sort_values("timestamp_ms", kind="stable")
            .drop_duplicates("timestamp_ms", keep="last")
        )
        stamps = book["timestamp_ms"].to_numpy(dtype=np.int64)
        state_columns = [
            column
            for column in (
                "up_best_bid", "up_best_ask", "down_best_bid", "down_best_ask",
                "up_ask_size", "down_ask_size", "up_bid_size", "down_bid_size",
            )
            if column in book
        ]
        state = book[state_columns].apply(pd.to_numeric, errors="coerce").to_numpy()
        if len(book) <= 1 or not state_columns:
            changed = np.ones(len(book), dtype=bool)
        else:
            same = (state[1:] == state[:-1]) | (
                np.isnan(state[1:]) & np.isnan(state[:-1])
            )
            changed = np.r_[True, ~same.all(axis=1)]
        change_stamps = stamps[changed]
        previous = np.searchsorted(change_stamps, stamps, side="right") - 1
        following = np.searchsorted(change_stamps, stamps, side="right")
        book["_ms_since_change"] = np.where(
            previous >= 0,
            stamps - change_stamps[np.maximum(previous, 0)],
            np.inf,
        )
        book["_ms_to_next_change"] = np.where(
            following < len(change_stamps),
            change_stamps[np.minimum(following, max(len(change_stamps) - 1, 0))] - stamps,
            np.inf,
        )
        books[key] = book.set_index("timestamp_ms")
    return books


def _active(frame: pd.DataFrame, suffix: str) -> pd.Series:
    column = f"lifecycle_state_{suffix}"
    if column not in frame:
        return pd.Series(False, index=frame.index)
    active = frame[column].isin(("active", "open", "trading"))
    halt = f"observed_halt_flag_{suffix}"
    if halt in frame:
        active &= ~frame[halt].fillna(False).astype(bool)
    return active


def _sane(frame: pd.DataFrame, suffix: str) -> pd.Series:
    sane = _active(frame, suffix)
    values = {}
    for side in ("up", "down"):
        for quote in ("bid", "ask"):
            column = f"{side}_best_{quote}_{suffix}"
            if column not in frame:
                return pd.Series(False, index=frame.index)
            values[f"{side}_{quote}"] = pd.to_numeric(frame[column], errors="coerce")
    for side in ("up", "down"):
        bid, ask = values[f"{side}_bid"], values[f"{side}_ask"]
        sane &= bid.between(EPS, 1.0 - EPS) & ask.between(EPS, 1.0 - EPS) & (bid < ask)
    sane &= values["up_ask"] + values["down_ask"] >= 0.99 - EPS
    return sane


def _fresh(frame: pd.DataFrame, suffix: str, limit_ms: int, *, require_after: bool) -> pd.Series:
    before = pd.to_numeric(frame[f"_ms_since_change_{suffix}"], errors="coerce") <= limit_ms
    if not require_after:
        return before
    after = pd.to_numeric(frame[f"_ms_to_next_change_{suffix}"], errors="coerce") <= limit_ms
    return before & after


def _diagnostic(
    lower: pd.Series,
    upper: pd.Series,
    status: str,
    config: BoxBatchConfig,
    *,
    source: str | None = None,
    source_basis: str | None = None,
) -> dict[str, Any]:
    row = _base_row(
        lower,
        upper,
        source=source,
        source_basis=source_basis,
        latency_ms=config.primary_latency_ms,
        primary_latency_ms=config.primary_latency_ms,
        shares=config.shares,
    )
    row["status"] = status
    row["failure"] = status
    return row


def replay_same_expiry_boxes(
    feat: pd.DataFrame,
    markets: pd.DataFrame,
    *,
    config: BoxBatchConfig = BoxBatchConfig(),
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    eligible = markets[markets["horizon"].isin((5, 15))].copy()
    eligible["market_id"] = eligible["market_id"].astype(str)
    books_by_market = _books_by_market(feat, set(eligible["market_id"]))
    for _end, group in eligible.groupby("end", sort=True):
        five = group[group["horizon"] == 5]
        fifteen = group[group["horizon"] == 15]
        for _, first in five.iterrows():
            for _, second in fifteen.iterrows():
                if not np.isfinite(first["k"]) or not np.isfinite(second["k"]):
                    continue
                if abs(float(first["k"]) - float(second["k"])) <= EPS:
                    continue
                lower, upper = (
                    (first, second)
                    if float(first["k"]) < float(second["k"])
                    else (second, first)
                )
                authority_conflict = any(
                    bool(market.get(column, False))
                    for market in (lower, upper)
                    for column in ("settlement_conflict", "resolution_source_conflict")
                    if not pd.isna(market.get(column, False))
                )
                fallback_used = any(
                    bool(market.get(column, False))
                    for market in (lower, upper)
                    for column in (
                        "open_boundary_fallback_used", "close_boundary_fallback_used",
                        "resolution_open_boundary_fallback_used",
                        "resolution_close_boundary_fallback_used",
                    )
                    if not pd.isna(market.get(column, False))
                )
                if authority_conflict or fallback_used:
                    rows.append(_diagnostic(
                        lower, upper, "settlement_authority_conflict", config
                    ))
                    continue
                lower_source, lower_basis, lower_conflict = _source(lower)
                upper_source, upper_basis, upper_conflict = _source(upper)
                if lower_conflict or upper_conflict:
                    rows.append(_diagnostic(lower, upper, "source_annotation_conflict", config))
                    continue
                if lower_source is None or upper_source is None:
                    rows.append(_diagnostic(lower, upper, "source_unverified", config))
                    continue
                if lower_source != upper_source:
                    rows.append(_diagnostic(lower, upper, "source_mismatch", config))
                    continue
                source_basis = (
                    lower_basis
                    if lower_basis == upper_basis
                    else "mixed_exact_and_documented"
                )
                books = _paired_books(
                    books_by_market,
                    str(lower["market_id"]),
                    str(upper["market_id"]),
                )
                if books.empty:
                    rows.append(_diagnostic(
                        lower, upper, "book_missing", config,
                        source=lower_source, source_basis=source_basis,
                    ))
                    continue
                common_start = max(int(lower["start"]), int(upper["start"]))
                common_end = min(int(lower["end"]), int(upper["end"]))
                books = books[(books.index >= common_start) & (books.index < common_end)]
                if books.empty:
                    rows.append(_diagnostic(
                        lower, upper, "no_common_trading_snapshots", config,
                        source=lower_source, source_basis=source_basis,
                    ))
                    continue
                lower_ask = pd.to_numeric(books["up_best_ask_lower"], errors="coerce")
                upper_ask = pd.to_numeric(books["down_best_ask_upper"], errors="coerce")
                lower_size = pd.to_numeric(books["up_ask_size_lower"], errors="coerce")
                upper_size = pd.to_numeric(books["down_ask_size_upper"], errors="coerce")
                stamps = books.index.to_numpy(dtype=np.int64)
                costs = (
                    lower_ask
                    + upper_ask
                    + _fee_totals(lower_ask, config.shares, stamps) / config.shares
                    + _fee_totals(upper_ask, config.shares, stamps) / config.shares
                )
                available = (
                    _sane(books, "lower")
                    & _sane(books, "upper")
                    & _fresh(books, "lower", config.freshness_ms, require_after=False)
                    & _fresh(books, "upper", config.freshness_ms, require_after=False)
                    & lower_ask.between(EPS, 1.0 - EPS)
                    & upper_ask.between(EPS, 1.0 - EPS)
                    & (lower_size >= config.shares)
                    & (upper_size >= config.shares)
                    & (costs <= config.max_cost + EPS)
                )
                contiguous = pd.Series(False, index=books.index)
                contiguous.iloc[1:] = np.diff(stamps) == config.persistence_ms
                decisions = available & available.shift(1, fill_value=False) & contiguous
                hits = np.flatnonzero(decisions.to_numpy())
                if not len(hits):
                    rows.append(_diagnostic(
                        lower, upper, "no_signal", config,
                        source=lower_source, source_basis=source_basis,
                    ))
                    continue
                decision_position = int(hits[0])
                decision_ms = int(stamps[decision_position])
                lower_limit = float(lower_ask.iloc[decision_position])
                upper_limit = float(upper_ask.iloc[decision_position])
                if (
                    lower_limit * config.shares < config.min_buy_notional - EPS
                    or upper_limit * config.shares < config.min_buy_notional - EPS
                ):
                    row = _diagnostic(
                        lower, upper, "order_below_minimum", config,
                        source=lower_source, source_basis=source_basis,
                    )
                    row.update(
                        candidate_count=1,
                        decision_ms=decision_ms,
                        lower_limit=lower_limit,
                        upper_limit=upper_limit,
                        decision_cost_per_set=float(costs.iloc[decision_position]),
                    )
                    rows.append(row)
                    continue
                for latency_ms in config.latencies_ms:
                    row = _base_row(
                        lower,
                        upper,
                        source=lower_source,
                        source_basis=source_basis,
                        latency_ms=int(latency_ms),
                        primary_latency_ms=config.primary_latency_ms,
                        shares=config.shares,
                    )
                    row.update({
                        "source_verified": True,
                        "candidate_count": 1,
                        "signal_count": 1,
                        "shares": config.shares,
                        "decision_ms": decision_ms,
                        "lower_limit": lower_limit,
                        "upper_limit": upper_limit,
                        "decision_cost_per_set": float(costs.iloc[decision_position]),
                    })
                    match_position = _snapshot(
                        stamps,
                        decision_ms + int(latency_ms),
                    )
                    if decision_ms + int(latency_ms) >= common_end:
                        row.update(status="match_outside_market", failure="match_outside_market")
                        rows.append(row)
                        continue
                    if match_position is None:
                        row.update(status="match_snapshot_missing", failure="match_snapshot_missing")
                        rows.append(row)
                        continue
                    match_ms = int(stamps[match_position])
                    row["match_ms"] = match_ms
                    match_frame = books.iloc[[match_position]]
                    healthy_match = bool(
                        _sane(match_frame, "lower").iloc[0]
                        and _sane(match_frame, "upper").iloc[0]
                        and _fresh(
                            match_frame, "lower", config.freshness_ms, require_after=True
                        ).iloc[0]
                        and _fresh(
                            match_frame, "upper", config.freshness_ms, require_after=True
                        ).iloc[0]
                    )
                    if not healthy_match:
                        row.update(status="match_book_unhealthy", failure="match_book_unhealthy")
                        rows.append(row)
                        continue
                    lower_price = float(lower_ask.iloc[match_position])
                    upper_price = float(upper_ask.iloc[match_position])
                    lower_filled = bool(
                        np.isfinite(lower_price)
                        and np.isfinite(lower_size.iloc[match_position])
                        and EPS < lower_price < 1.0 - EPS
                        and lower_price <= lower_limit + EPS
                        and lower_size.iloc[match_position] >= config.shares
                    )
                    upper_filled = bool(
                        np.isfinite(upper_price)
                        and np.isfinite(upper_size.iloc[match_position])
                        and EPS < upper_price < 1.0 - EPS
                        and upper_price <= upper_limit + EPS
                        and upper_size.iloc[match_position] >= config.shares
                    )
                    row["lower_filled"] = lower_filled
                    row["upper_filled"] = upper_filled
                    if lower_filled and upper_filled:
                        entry_fees = (
                            _fee_total(lower_price, config.shares, match_ms)
                            + _fee_total(upper_price, config.shares, match_ms)
                        )
                        entry_cost = config.shares * (lower_price + upper_price)
                        lower_won = lower.get("up_won")
                        upper_won = upper.get("up_won")
                        if pd.isna(lower_won) or pd.isna(upper_won):
                            row.update(
                                status="paired_unresolved",
                                failure="outcome_unresolved",
                                entry_cost=entry_cost,
                                fees=entry_fees,
                            )
                        else:
                            lower_result = float(lower_won)
                            upper_result = float(upper_won)
                            payout = config.shares * (
                                lower_result + 1.0 - upper_result
                            )
                            if (
                                lower_result not in (0.0, 1.0)
                                or upper_result not in (0.0, 1.0)
                                or payout < config.shares - EPS
                            ):
                                row.update(
                                    status="settlement_inconsistent",
                                    failure="nested_payoff_below_one",
                                    entry_cost=entry_cost,
                                    fees=entry_fees,
                                    payout=payout,
                                )
                            else:
                                row.update(
                                    status="paired",
                                    execution_valid=True,
                                    paired_sets=config.shares,
                                    entry_cost=entry_cost,
                                    fees=entry_fees,
                                    payout=payout,
                                    bonus_payout=max(0.0, payout - config.shares),
                                    gross_floor_edge_per_set=(
                                        1.0 - lower_price - upper_price
                                    ),
                                    pnl=payout - entry_cost - entry_fees,
                                )
                        rows.append(row)
                        continue
                    if not lower_filled and not upper_filled:
                        row.update(status="neither", execution_valid=True, pnl=0.0)
                        rows.append(row)
                        continue
                    orphan = "lower" if lower_filled else "upper"
                    entry_price = lower_price if lower_filled else upper_price
                    entry_fee = _fee_total(entry_price, config.shares, match_ms)
                    row.update(
                        entry_cost=config.shares * entry_price,
                        fees=entry_fee,
                    )
                    unwind_position = _snapshot(
                        stamps,
                        match_ms + config.orphan_unwind_ms,
                    )
                    if match_ms + config.orphan_unwind_ms >= common_end:
                        row.update(
                            status=f"orphan_{orphan}_invalid",
                            failure="unwind_outside_market",
                        )
                        rows.append(row)
                        continue
                    if unwind_position is None:
                        row.update(
                            status=f"orphan_{orphan}_invalid",
                            failure="unwind_snapshot_missing",
                        )
                        rows.append(row)
                        continue
                    if orphan == "lower":
                        bid = float(books["up_best_bid_lower"].iloc[unwind_position])
                        depth = float(books["up_bid_size_lower"].iloc[unwind_position])
                    else:
                        bid = float(books["down_best_bid_upper"].iloc[unwind_position])
                        depth = float(books["down_bid_size_upper"].iloc[unwind_position])
                    unwind_frame = books.iloc[[unwind_position]]
                    if not (
                        _sane(unwind_frame, orphan).iloc[0]
                        and _fresh(
                            unwind_frame, orphan, config.freshness_ms, require_after=True
                        ).iloc[0]
                    ):
                        row.update(
                            status=f"orphan_{orphan}_invalid",
                            failure="unwind_market_inactive",
                        )
                        rows.append(row)
                        continue
                    if (
                        not np.isfinite(bid)
                        or not np.isfinite(depth)
                        or not EPS < bid < 1.0 - EPS
                        or depth < config.shares
                    ):
                        row.update(
                            status=f"orphan_{orphan}_invalid",
                            failure="insufficient_unwind_depth",
                        )
                        rows.append(row)
                        continue
                    unwind_ms = int(stamps[unwind_position])
                    exit_fee = _fee_total(bid, config.shares, unwind_ms)
                    row.update(
                        status=f"orphan_{orphan}_unwound",
                        execution_valid=True,
                        unwind_ms=unwind_ms,
                        entry_cost=config.shares * entry_price,
                        exit_proceeds=config.shares * bid,
                        fees=entry_fee + exit_fee,
                        pnl=config.shares * (bid - entry_price) - entry_fee - exit_fee,
                    )
                    rows.append(row)
    return pd.DataFrame(rows)


def _period(day: str) -> str:
    if day <= "2026-07-15":
        return "A"
    if day <= "2026-08-16":
        return "B"
    return "C"


def finalize_rows(rows: pd.DataFrame) -> pd.DataFrame:
    if rows.empty:
        return rows.copy()
    result = rows.copy()
    result = result.sort_values(
        ["pair_id", "latency_ms", "signal_count", "decision_ms"],
        ascending=[True, True, False, True],
        kind="stable",
        na_position="last",
    ).drop_duplicates(["pair_id", "latency_ms"], keep="first")
    stamp = pd.to_numeric(result["decision_ms"], errors="coerce").fillna(
        pd.to_numeric(result["end_ms"], errors="coerce")
    )
    result["day"] = pd.to_datetime(stamp, unit="ms", utc=True).dt.strftime("%Y-%m-%d")
    return result.sort_values(
        ["day", "decision_ms", "pair_id", "latency_ms"],
        kind="stable",
        na_position="last",
    ).reset_index(drop=True)


def _day_cluster_lower_99(group: pd.DataFrame, shares: pd.Series) -> float:
    pnl_values = pd.to_numeric(group["pnl"], errors="coerce")
    if (
        (~group["execution_valid"].astype(bool)).any()
        or pnl_values.isna().any()
        or shares.isna().any()
    ):
        return np.nan
    daily = pd.DataFrame({
        "day": group["day"].astype(str),
        "pnl": pnl_values,
        "shares": shares,
    }).groupby("day", sort=True)[["pnl", "shares"]].sum()
    if len(daily) < 2 or daily["pnl"].isna().any():
        return np.nan
    total_shares = float(daily["shares"].sum())
    if total_shares <= 0:
        return np.nan
    estimate = float(daily["pnl"].sum()) / total_shares
    influence = daily["pnl"] - estimate * daily["shares"]
    count = len(daily)
    standard_error = float(
        np.sqrt(count / (count - 1) * np.square(influence).sum()) / total_shares
    )
    if standard_error <= EPS:
        return estimate
    from scipy.stats import t as student_t

    return estimate - float(student_t.ppf(0.99, count - 1)) * standard_error


def _peak_required_capital(group: pd.DataFrame, attempted: pd.Series) -> float:
    """Peak reserved plus filled cost, respecting overlapping box lifetimes."""
    if "decision_ms" not in group or pd.to_numeric(
        group["decision_ms"], errors="coerce"
    ).isna().all():
        return float(attempted.max()) if attempted.notna().any() else np.nan
    events: dict[int, float] = {}

    def change(stamp: float, amount: float) -> None:
        if np.isfinite(stamp) and np.isfinite(amount) and abs(amount) > EPS:
            key = int(stamp)
            events[key] = events.get(key, 0.0) + float(amount)

    for index, row in group.iterrows():
        start = pd.to_numeric(pd.Series([row.get("decision_ms")]), errors="coerce").iloc[0]
        reserve = float(attempted.loc[index]) if np.isfinite(attempted.loc[index]) else 0.0
        if not np.isfinite(start) or reserve <= 0:
            continue
        match = pd.to_numeric(pd.Series([row.get("match_ms")]), errors="coerce").iloc[0]
        if not np.isfinite(match):
            latency = float(row.get("latency_ms", 0.0) or 0.0)
            match = start + max(0.0, latency)
        change(start, reserve)
        change(match, -reserve)
        entry = float(row.get("entry_cost", 0.0) or 0.0)
        fees = float(row.get("fees", 0.0) or 0.0)
        held = entry + fees
        if held <= 0:
            continue
        status = str(row.get("status", ""))
        release = row.get("end_ms")
        if status.startswith("orphan_") and status.endswith("_unwound"):
            release = row.get("unwind_ms")
        change(match, held)
        release_value = pd.to_numeric(pd.Series([release]), errors="coerce").iloc[0]
        change(release_value, -held)
    if not events:
        return float(attempted.max()) if attempted.notna().any() else np.nan
    capital = 0.0
    peak = 0.0
    for stamp in sorted(events):
        capital += events[stamp]
        peak = max(peak, capital)
    return peak


def summarize_rows(rows: pd.DataFrame) -> pd.DataFrame:
    if rows.empty:
        return pd.DataFrame()
    sent = rows[rows["signal_count"] > 0].copy()
    if sent.empty:
        return pd.DataFrame()
    sent["period"] = sent["day"].astype(str).map(_period)
    output = []
    for (period, latency), group in sent.groupby(["period", "latency_ms"], sort=True):
        paired = group["status"] == "paired"
        statuses = group["status"].astype(str)
        orphan = statuses.str.startswith("orphan_")
        lower_filled = (
            group["lower_filled"].fillna(False).astype(bool)
            if "lower_filled" in group
            else statuses.str.startswith("orphan_lower") | paired
        )
        upper_filled = (
            group["upper_filled"].fillna(False).astype(bool)
            if "upper_filled" in group
            else statuses.str.startswith("orphan_upper") | paired
        )
        lower_orphan = lower_filled & ~upper_filled
        upper_orphan = upper_filled & ~lower_filled
        pnl_all = pd.to_numeric(group["pnl"], errors="coerce")
        valid = group["execution_valid"].astype(bool) & pnl_all.notna()
        pnl = pnl_all.loc[valid]
        signals = len(group)
        valid_pnl = float(pnl.sum()) if pnl.notna().any() else np.nan
        shares = (
            pd.to_numeric(group["shares"], errors="coerce")
            if "shares" in group
            else pd.Series(5.0, index=group.index)
        )
        attempted_shares = float(shares.sum())
        all_valid = bool(valid.all())
        net_pnl = valid_pnl if all_valid else np.nan
        attempted = pd.to_numeric(
            group["decision_cost_per_set"], errors="coerce"
        ) * shares
        attempted_notional = float(attempted.sum())
        peak_capital = _peak_required_capital(group, attempted)
        paired_sets = float(pd.to_numeric(group["paired_sets"], errors="coerce").sum())
        gross_floor = pd.to_numeric(
            group.get(
                "gross_floor_edge_per_set",
                pd.Series(np.nan, index=group.index),
            ),
            errors="coerce",
        )
        fee_values = pd.to_numeric(
            group.get("fees", pd.Series(0.0, index=group.index)), errors="coerce"
        )
        bonus_values = pd.to_numeric(
            group.get("bonus_payout", pd.Series(0.0, index=group.index)),
            errors="coerce",
        )
        if all_valid:
            if "decision_ms" in group:
                missing = pd.Series(np.nan, index=group.index)
                realization = pd.to_numeric(
                    group.get("end_ms", missing), errors="coerce"
                )
                orphan_realization = pd.to_numeric(
                    group.get("unwind_ms", missing), errors="coerce"
                )
                match_realization = pd.to_numeric(
                    group.get("match_ms", missing), errors="coerce"
                )
                realization = realization.where(~orphan, orphan_realization)
                realization = realization.fillna(match_realization).fillna(
                    pd.to_numeric(group["decision_ms"], errors="coerce")
                )
                ordered = group.assign(
                    _pnl=pnl_all, _realization=realization
                ).sort_values(["_realization", "decision_ms"], kind="stable")
            else:
                ordered = group.assign(_pnl=pnl_all).sort_values("day", kind="stable")
            cumulative = ordered["_pnl"].cumsum().to_numpy(dtype=float)
            running_high = np.maximum.accumulate(np.r_[0.0, cumulative])
            drawdowns = running_high[1:] - cumulative
            max_drawdown = float(drawdowns.max(initial=0.0))
            nonzero = pnl[np.abs(pnl) > EPS]
            if len(nonzero):
                from scipy.stats import binomtest

                exact_sign_p = float(
                    binomtest(
                        int((nonzero > 0).sum()),
                        len(nonzero),
                        0.5,
                        alternative="greater",
                    ).pvalue
                )
            else:
                exact_sign_p = 1.0
        else:
            max_drawdown = np.nan
            exact_sign_p = np.nan
        orphan_pnl = pnl_all.loc[orphan & valid]
        output.append({
            "period": period,
            "latency_ms": int(latency),
            "signals": signals,
            "paired": int(paired.sum()),
            "orphans": int(orphan.sum()),
            "lower_orphans": int(lower_orphan.sum()),
            "upper_orphans": int(upper_orphan.sum()),
            "neither": int((group["status"] == "neither").sum()),
            "invalid": int((~valid).sum()),
            "paired_rate": float(paired.mean()),
            "orphan_rate": float(orphan.mean()),
            "gross_box_edge_per_set": float(
                (1.0 - pd.to_numeric(group["decision_cost_per_set"], errors="coerce")).mean()
            ),
            "gross_floor_edge_per_set": float(
                gross_floor.loc[paired].mean()
            ),
            "fees": float(fee_values.sum()),
            "bonus_payout": float(bonus_values.sum()),
            "net_pnl": net_pnl,
            "net_ev_per_share": (
                net_pnl / attempted_shares
                if all_valid and np.isfinite(net_pnl) and attempted_shares > 0
                else np.nan
            ),
            "day_cluster_lower_99": _day_cluster_lower_99(group, shares),
            "exact_sign_p": exact_sign_p,
            "family100_corrected_p": (
                min(1.0, 100.0 * exact_sign_p)
                if np.isfinite(exact_sign_p)
                else np.nan
            ),
            "max_drawdown": max_drawdown,
            "worst_orphan_loss": (
                float(orphan_pnl.min()) if orphan_pnl.notna().any() else np.nan
            ),
            "pnl_per_signal": net_pnl / signals if np.isfinite(net_pnl) and signals else np.nan,
            "pnl_per_paired_set": (
                net_pnl / paired_sets
                if np.isfinite(net_pnl) and paired_sets > 0
                else np.nan
            ),
            "attempted_notional": attempted_notional,
            "peak_capital": peak_capital,
        })
    return pd.DataFrame(output)


def _historical_screen_verdict(summary: pd.DataFrame) -> str:
    """Return an audit verdict, never a production or live-trading approval."""
    required = {(period, latency) for period in ("B", "C") for latency in (400, 500)}
    present = {
        (str(row.period), int(row.latency_ms))
        for row in summary.itertuples(index=False)
    }
    if not required.issubset(present):
        return "INCOMPLETE: B/C evidence at both 400 ms and 500 ms is unavailable."
    selected = summary[
        summary.apply(
            lambda row: (str(row["period"]), int(row["latency_ms"])) in required,
            axis=1,
        )
    ].set_index(["period", "latency_ms"])
    if (selected["invalid"] > 0).any():
        return "REJECTED: at least one required execution path is unresolved or invalid."
    primary = selected.xs(400, level="latency_ms")
    robust = selected.xs(500, level="latency_ms")
    primary_pass = bool(
        (primary["net_ev_per_share"] > 0).all()
        and (primary["day_cluster_lower_99"] > 0).all()
        and (primary["family100_corrected_p"] < 0.01).all()
    )
    robust_pass = bool((robust["net_ev_per_share"] >= 0).all())
    if primary_pass and robust_pass:
        return (
            "QUALIFIES FOR FRESH DIRECT-L2 FORWARD VALIDATION ONLY; "
            "the inspected historical tape is not an untouched holdout."
        )
    return "REJECTED: the frozen historical execution gates are not all positive."


def _fmt_number(value: float, template: str, *, scale: float = 1.0) -> str:
    return template.format(value * scale) if np.isfinite(value) else "–"


def _report(rows: pd.DataFrame, archives_seen: int) -> str:
    summary = summarize_rows(rows)
    diagnostics = rows[rows["signal_count"] == 0] if not rows.empty else rows
    eligible_pairs = (
        rows.loc[rows["source_verified"].fillna(False), "pair_id"].nunique()
        if not rows.empty and {"source_verified", "pair_id"}.issubset(rows)
        else 0
    )
    candidate_pairs = (
        rows.loc[pd.to_numeric(rows["candidate_count"], errors="coerce") > 0, "pair_id"].nunique()
        if not rows.empty and {"candidate_count", "pair_id"}.issubset(rows)
        else 0
    )
    minimum_blocks = (
        int((rows["status"] == "order_below_minimum").sum())
        if not rows.empty and "status" in rows
        else 0
    )
    diagnostic_counts = (
        diagnostics["status"].astype(str).value_counts().sort_index().to_dict()
        if not diagnostics.empty and "status" in diagnostics
        else {}
    )
    verdict = _historical_screen_verdict(summary)
    lines = [
        "# Family 29: same-expiry 5m-15m box with independent batch legs",
        "",
        f"Archives: {archives_seen}. Evidence tier: `{EVIDENCE_TIER}`.",
        "",
        "The lower opening strike's Up and the higher opening strike's Down pay at least $1 only when expiry and resolution source match. Two FOK entries in one Polymarket batch are processed independently, so paired fills and orphan legs are reported separately.",
        "",
        f"Eligible source-verified pairs: {eligible_pairs:,}. Economic candidate pairs: {candidate_pairs:,}. Minimum-order blocks: {minimum_blocks:,}.",
        f"Diagnostic rows: {len(diagnostics):,}. Status counts: `{diagnostic_counts}`.",
        "",
        f"**Historical screen verdict:** {verdict}",
        "",
        "| Period | Latency | Signals | Paired | Lower/upper orphans | Invalid | Paired rate | Gross floor/set | Fees | Bonus payout | Net EV/share | 99% day lower | Corrected p | Net PnL | Max DD | Worst orphan | Peak capital | Attempted notional |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summary.itertuples(index=False):
        lines.append(
            f"| {row.period} | {row.latency_ms} ms | {row.signals:,} | {row.paired:,} | "
            f"{row.lower_orphans:,}/{row.upper_orphans:,} | {row.invalid:,} | "
            f"{row.paired_rate:.1%} | "
            f"{_fmt_number(row.gross_floor_edge_per_set, '{:+.2f}¢', scale=100)} | "
            f"${row.fees:.2f} | ${row.bonus_payout:.2f} | "
            f"{_fmt_number(row.net_ev_per_share, '{:+.2f}¢', scale=100)} | "
            f"{_fmt_number(row.day_cluster_lower_99, '{:+.2f}¢', scale=100)} | "
            f"{_fmt_number(row.family100_corrected_p, '{:.4g}')} | "
            f"{_fmt_number(row.net_pnl, '${:+.2f}')} | "
            f"{_fmt_number(row.max_drawdown, '${:.2f}')} | "
            f"{_fmt_number(row.worst_orphan_loss, '${:+.2f}')} | "
            f"${row.peak_capital:,.2f} | ${row.attempted_notional:,.2f} |"
        )
    if summary.empty:
        lines.append("| – | – | 0 | 0 | 0/0 | 0 | – | – | – | – | – | – | – | – | – | – | – | – |")
    lines += [
        "",
        "This historical screen cannot prove a live fill. A positive result only qualifies the frozen rule for direct receipt-ordered L2 forward validation; it does not authorise production.",
    ]
    return "\n".join(lines) + "\n"


def run(
    workdir: str,
    out: str,
    days: int | None = None,
    *,
    dataset: str | None = None,
) -> None:
    import cross

    if dataset is not None:
        cross.set_dataset(dataset)
    root = Path(workdir)
    root.mkdir(parents=True, exist_ok=True)
    archives = cross.archives(cross.fetch("MANIFEST.txt").decode())
    if days:
        archives = archives[-int(days):]
    parts = []
    for name, _digest in archives:
        local = Path(cross.fetch(name, root / name))
        try:
            feat, raw_markets, resolutions = cross.read_day(local)
            markets = cross.market_table(raw_markets, resolutions)
            rows = replay_same_expiry_boxes(feat, markets)
            parts.append(rows)
            print(f"{name}: {len(rows):,} box execution rows", flush=True)
        finally:
            local.unlink(missing_ok=True)
    result = finalize_rows(pd.concat(parts, ignore_index=True)) if parts else pd.DataFrame()
    output = Path(out)
    output.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(output.with_suffix(".csv.gz"), index=False)
    output.write_text(_report(result, len(archives)), encoding="utf-8")
