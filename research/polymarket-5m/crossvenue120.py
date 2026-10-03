"""Causal primitives for the CrossVenue-120 research backtest.

The strategy makes a decision from information received by ``decision_ms``, predicts a
directional Binance move in the next 120 ms, and submits a marketable limit order to the
corresponding Polymarket outcome.  These small functions are deliberately independent of the
dataset loader so their timing and execution semantics can be tested exactly.
"""

from __future__ import annotations

import argparse
import io
import math
import tarfile
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd


AUG_17_2026_1100_UTC_MS = int(pd.Timestamp("2026-08-17T11:00:00Z").timestamp() * 1000)
SEP_04_2026_1400_UTC_MS = int(pd.Timestamp("2026-09-04T14:00:00Z").timestamp() * 1000)

RETURN_WINDOWS_MS = (20, 50, 100, 250, 500, 1000, 2000)
FLOW_WINDOWS_MS = (50, 100, 250, 500, 1000)
VENUE_WINDOWS_MS = (50, 100, 250, 500)


def taker_hold_ms(timestamp_ms: int) -> int:
    """Return the fixed crypto-market taker hold in force at ``timestamp_ms``."""
    if timestamp_ms < AUG_17_2026_1100_UTC_MS:
        return 250
    if timestamp_ms < SEP_04_2026_1400_UTC_MS:
        return 50
    return 150


def forward_jump_labels(
    trades: pd.DataFrame,
    decisions_ms: np.ndarray,
    *,
    horizon_ms: int = 120,
    threshold_bps: float = 1.0,
) -> pd.DataFrame:
    """Label the first threshold crossing strictly after each decision.

    The anchor is the last price received no later than the decision.  Only prints in
    ``(decision, decision + horizon]`` may determine the label.
    """
    t = trades.sort_values("recv_ts_ms", kind="stable")
    ts = t["recv_ts_ms"].to_numpy(np.int64)
    px = t["price"].to_numpy(float)
    rows = []
    threshold = threshold_bps / 10_000.0
    for decision in np.asarray(decisions_ms, dtype=np.int64):
        anchor_i = np.searchsorted(ts, decision, side="right") - 1
        lo = np.searchsorted(ts, decision, side="right")
        hi = np.searchsorted(ts, decision + horizon_ms, side="right")
        direction, label_ts = 0, np.nan
        if anchor_i >= 0 and lo < hi and px[anchor_i] > 0:
            returns = np.log(px[lo:hi] / px[anchor_i])
            crossings = np.flatnonzero(np.abs(returns) >= threshold)
            if len(crossings):
                j = lo + int(crossings[0])
                direction = 1 if returns[crossings[0]] > 0 else -1
                label_ts = int(ts[j])
        rows.append((int(decision), direction, label_ts))
    return pd.DataFrame(rows, columns=["decision_ms", "direction", "label_ts_ms"])


def causal_feature_rows(
    trades: pd.DataFrame,
    books: pd.DataFrame,
    decisions_ms: np.ndarray,
    *,
    market_id: str,
) -> pd.DataFrame:
    """Build a compact causal feature row at each decision timestamp.

    This public primitive also records the newest input timestamp, making the no-lookahead
    invariant auditable in every extracted shard.
    """
    spot = trades.sort_values("recv_ts_ms", kind="stable")
    book = books.loc[books["market_id"] == market_id].sort_values("timestamp_ms", kind="stable")
    st = spot["recv_ts_ms"].to_numpy(np.int64)
    bt = book["timestamp_ms"].to_numpy(np.int64)
    rows = []
    for decision in np.asarray(decisions_ms, dtype=np.int64):
        si = np.searchsorted(st, decision, side="right") - 1
        bi = np.searchsorted(bt, decision, side="right") - 1
        if si < 0 or bi < 0:
            continue
        window = spot[(spot["recv_ts_ms"] >= decision - 100) & (spot["recv_ts_ms"] <= decision)]
        sizes = window["size"].astype(float) if "size" in window else pd.Series(1.0, index=window.index)
        if "taker_side" in window:
            signs = np.where(window["taker_side"].astype(str).str.lower() == "buy", 1.0, -1.0)
            flow = float(np.sum(sizes.to_numpy() * signs))
        else:
            flow = 0.0
        b = book.iloc[bi]
        rows.append(
            {
                "market_id": market_id,
                "decision_ms": int(decision),
                "feature_max_ms": int(max(st[si], bt[bi])),
                "book_ts_ms": int(bt[bi]),
                "spot_price": float(spot.iloc[si]["price"]),
                "flow_100ms": flow,
                "up_bid": float(b["up_best_bid"]),
                "up_ask": float(b["up_best_ask"]),
                "down_bid": float(b["down_best_bid"]),
                "down_ask": float(b["down_best_ask"]),
                "up_bid_size": float(b.get("up_bid_size", np.nan)),
                "up_ask_size": float(b.get("up_ask_size", np.nan)),
                "down_bid_size": float(b.get("down_bid_size", np.nan)),
                "down_ask_size": float(b.get("down_ask_size", np.nan)),
            }
        )
    return pd.DataFrame(rows)


def _side_sign(values: pd.Series) -> np.ndarray:
    value = values.astype(str).str.lower()
    return np.where(value.isin(["buy", "b", "1", "false"]), 1.0, -1.0)


def _window_values(ts: np.ndarray, values: np.ndarray, decisions: np.ndarray, windows: tuple[int, ...]):
    """Sum ``values`` over inclusive ``[decision-window, decision]`` windows."""
    cs = np.r_[0.0, np.cumsum(values, dtype=float)]
    right = np.searchsorted(ts, decisions, side="right")
    return {
        window: cs[right] - cs[np.searchsorted(ts, decisions - window, side="left")]
        for window in windows
    }


def spot_candidate_features(
    primary: pd.DataFrame,
    secondary: pd.DataFrame | None = None,
    *,
    decision_times: np.ndarray | None = None,
    step_ms: int = 20,
    gate_bps: float = 0.05,
    jump_bps: float = 1.0,
    min_lead_ms: int = 80,
    horizon_ms: int = 160,
) -> pd.DataFrame:
    """Build event-driven, strictly causal primary/secondary venue features.

    With no explicit decisions, the first primary print in each ``step_ms`` bucket is considered
    and a fixed, causal recent-move gate is applied.  Labels are the first ±``jump_bps`` crossing
    in ``[decision + min_lead, decision + horizon]`` (centred on 120 ms by default).  The function
    records both venues' newest consumed timestamps so extraction jobs can assert causality.
    """
    if primary.empty:
        return pd.DataFrame()
    p = primary.sort_values("recv_ts_ms", kind="stable").dropna(subset=["recv_ts_ms", "price"])
    pt = p["recv_ts_ms"].to_numpy(np.int64)
    pp = p["price"].to_numpy(float)
    psz = p["size"].to_numpy(float) if "size" in p else np.ones(len(p))
    psgn = _side_sign(p["taker_side"]) if "taker_side" in p else np.zeros(len(p))
    if decision_times is None:
        bucket = pt // step_ms
        first = np.r_[True, bucket[1:] != bucket[:-1]]
        decisions = pt[first]
    else:
        decisions = np.asarray(decision_times, dtype=np.int64)
    pi = np.searchsorted(pt, decisions, side="right") - 1
    valid = pi >= 0
    decisions, pi = decisions[valid], pi[valid]
    if not len(decisions):
        return pd.DataFrame()

    rows: dict[str, np.ndarray] = {
        "decision_ms": decisions,
        "feature_max_ms": pt[pi],
        "spot_price": pp[pi],
        "last_size": psz[pi],
        "last_sign": psgn[pi],
        "interarrival_ms": np.where(pi > 0, pt[pi] - pt[np.maximum(pi - 1, 0)], np.nan),
    }
    max_gate = np.zeros(len(decisions), dtype=float)
    for window in RETURN_WINDOWS_MS:
        before = np.searchsorted(pt, decisions - window, side="right") - 1
        value = np.where(before >= 0, 10_000 * np.log(pp[pi] / pp[np.maximum(before, 0)]), np.nan)
        rows[f"ret_{window}ms_bps"] = value
        max_gate = np.fmax(max_gate, np.abs(value))
    signed = psz * psgn
    flow = _window_values(pt, signed, decisions, FLOW_WINDOWS_MS)
    volume = _window_values(pt, psz, decisions, FLOW_WINDOWS_MS)
    count = _window_values(pt, np.ones(len(p)), decisions, FLOW_WINDOWS_MS)
    for window in FLOW_WINDOWS_MS:
        rows[f"flow_{window}ms"] = flow[window]
        rows[f"volume_{window}ms"] = volume[window]
        rows[f"prints_{window}ms"] = count[window]

    direction = np.zeros(len(decisions), dtype=np.int8)
    jump_ts = np.full(len(decisions), np.nan)
    jump_recv_ts = np.full(len(decisions), np.nan)
    if "trade_ts_ms" in p:
        source_raw = pd.to_numeric(p["trade_ts_ms"], errors="coerce").to_numpy(float)
        source_raw = np.where(np.isfinite(source_raw), source_raw, pt)
    else:
        source_raw = pt.astype(float)
    source_order = np.argsort(source_raw, kind="stable")
    source_ts = source_raw[source_order]
    source_px = pp[source_order]
    source_recv = pt[source_order]
    cutoff = jump_bps / 10_000.0
    for n, decision in enumerate(decisions):
        # Labels describe a move that happens after the local decision, not a stale packet that
        # was already reflected at the exchange but had not reached us yet.  Ground-truth labels
        # may therefore anchor on the final exchange-time print at/before ``decision``; model
        # features above remain restricted to packets actually received by ``decision``.
        source_anchor_i = np.searchsorted(source_ts, decision, side="right") - 1
        if source_anchor_i < 0 or source_px[source_anchor_i] <= 0:
            continue
        lo = np.searchsorted(source_ts, decision + min_lead_ms, side="left")
        hi = np.searchsorted(source_ts, decision + horizon_ms, side="right")
        if lo >= hi:
            continue
        r = np.log(source_px[lo:hi] / source_px[source_anchor_i])
        hit = np.flatnonzero(np.abs(r) >= cutoff)
        if len(hit):
            j = lo + int(hit[0])
            direction[n] = 1 if r[hit[0]] > 0 else -1
            jump_ts[n] = source_ts[j]
            jump_recv_ts[n] = source_recv[j]
    rows["jump_direction"] = direction
    rows["jump_ts_ms"] = jump_ts
    rows["jump_recv_ts_ms"] = jump_recv_ts
    rows["jump_lead_ms"] = jump_ts - decisions

    if secondary is not None and not secondary.empty:
        s = secondary.sort_values("recv_ts_ms", kind="stable").dropna(subset=["recv_ts_ms", "price"])
        st = s["recv_ts_ms"].to_numpy(np.int64)
        sp = s["price"].to_numpy(float)
        ssz = s["size"].to_numpy(float) if "size" in s else np.ones(len(s))
        ssgn = _side_sign(s["taker_side"]) if "taker_side" in s else np.zeros(len(s))
        si = np.searchsorted(st, decisions, side="right") - 1
        present = si >= 0
        rows["venue_feature_max_ms"] = np.where(present, st[np.maximum(si, 0)], np.nan)
        rows["venue_last_price"] = np.where(present, sp[np.maximum(si, 0)], np.nan)
        rows["venue_age_ms"] = np.where(present, decisions - st[np.maximum(si, 0)], np.nan)
        for window in VENUE_WINDOWS_MS:
            before = np.searchsorted(st, decisions - window, side="right") - 1
            value = np.where(
                present & (before >= 0),
                10_000 * np.log(sp[np.maximum(si, 0)] / sp[np.maximum(before, 0)]),
                np.nan,
            )
            rows[f"venue_ret_{window}ms_bps"] = value
        sflow = _window_values(st, ssz * ssgn, decisions, (50, 100, 250))
        for window in (50, 100, 250):
            rows[f"venue_flow_{window}ms"] = sflow[window]
    else:
        rows["venue_feature_max_ms"] = np.nan
        rows["venue_last_price"] = np.nan
        rows["venue_age_ms"] = np.nan
        for window in VENUE_WINDOWS_MS:
            rows[f"venue_ret_{window}ms_bps"] = np.nan
        for window in (50, 100, 250):
            rows[f"venue_flow_{window}ms"] = np.nan

    out = pd.DataFrame(rows)
    if decision_times is None:
        out = out[max_gate >= gate_bps].reset_index(drop=True)
    return out


def taker_fee(price: float) -> float:
    return 0.07 * price * (1.0 - price)


def max_limit_for_edge(probability: float, min_edge: float, *, tick: float = 0.01) -> float:
    """Highest tick whose fee-adjusted expected edge is at least ``min_edge``."""
    prices = np.arange(tick, 1.0, tick)
    good = probability - prices - 0.07 * prices * (1.0 - prices) >= min_edge - 1e-12
    return float(prices[np.flatnonzero(good)[-1]]) if good.any() else np.nan


def replay_joint_predictions(
    candidates: pd.DataFrame,
    *,
    min_jump: float,
    min_fill: float,
    min_expected_edge: float,
    min_realizable_edge: float,
    chase: float,
    shares: float = 5.0,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Turn out-of-sample model predictions into signals and executable fills.

    Selection uses predicted fields only.  The edge-capped limit is fixed at decision time; the
    first eligible ask then either fills it or does not.  Actual outcomes enter only after fills.
    """
    if candidates.empty:
        return candidates.copy(), candidates.copy()
    frame = candidates.copy()
    frame["predicted_edge"] = (
        frame["p_side_win"] - frame["pred_match_ask"]
        - 0.07 * frame["pred_match_ask"] * (1.0 - frame["pred_match_ask"])
    )
    eligible = frame[
        frame["book_ok"].astype(bool)
        & (frame["p_jump"] >= min_jump)
        & (frame["p_fill"] >= min_fill)
        & (frame["predicted_edge"] >= min_expected_edge)
    ].copy()
    if eligible.empty:
        return eligible, eligible.copy()
    edge_limits = np.array([
        max_limit_for_edge(q, min_realizable_edge) for q in eligible["p_side_win"].to_numpy(float)
    ])
    ask0 = np.where(eligible["pred_side"].astype(str).str.lower() == "up",
                    eligible["up_ask0"], eligible["down_ask0"]).astype(float)
    eligible["limit_price"] = np.minimum(np.round((ask0 + chase) * 100) / 100, edge_limits)
    eligible = eligible[np.isfinite(eligible["limit_price"])]
    signals = (
        eligible.sort_values(["market_id", "decision_ms"], kind="stable")
        .drop_duplicates("market_id", keep="first")
        .reset_index(drop=True)
    )
    up = signals["pred_side"].astype(str).str.lower() == "up"
    match_ask = np.where(up, signals["match_up_ask"], signals["match_down_ask"]).astype(float)
    match_size = np.where(up, signals["match_up_size"], signals["match_down_size"]).astype(float)
    fill_mask = (
        signals["match_book_ok"].astype(bool).to_numpy()
        & np.isfinite(match_ask)
        & (match_ask <= signals["limit_price"].to_numpy(float) + 1e-12)
    )
    fills = signals.loc[fill_mask].copy()
    if fills.empty:
        return signals, fills
    up_filled = fills["pred_side"].astype(str).str.lower() == "up"
    price = np.where(up_filled, fills["match_up_ask"], fills["match_down_ask"]).astype(float)
    available = np.where(up_filled, fills["match_up_size"], fills["match_down_size"]).astype(float)
    quantity = np.minimum(shares, np.where(np.isfinite(available), available, 0.0))
    won = np.where(up_filled, fills["up_won"], 1.0 - fills["up_won"]).astype(float)
    fee = 0.07 * price * (1.0 - price)
    fills["price"] = price
    fills["shares"] = quantity
    fills["fee"] = fee
    fills["won"] = won
    fills["pnl_per_share"] = won - price - fee
    fills["pnl_usd"] = quantity * fills["pnl_per_share"]
    fills = fills[fills["shares"] > 0].reset_index(drop=True)
    return signals, fills


def _sane_books(frame: pd.DataFrame) -> np.ndarray:
    ub = frame["up_best_bid"].to_numpy(float)
    ua = frame["up_best_ask"].to_numpy(float)
    db = frame["down_best_bid"].to_numpy(float)
    da = frame["down_best_ask"].to_numpy(float)
    with np.errstate(invalid="ignore"):
        sane = (
            np.isfinite(ub) & np.isfinite(ua) & np.isfinite(db) & np.isfinite(da)
            & (ub < ua) & (db < da) & (ua + da >= 0.99 - 1e-9)
            & (ub >= 0.0) & (da <= 1.0) & (db >= 0.0) & (ua <= 1.0)
        )
    if "lifecycle_state" in frame:
        state = frame["lifecycle_state"]
        sane &= (state.isna() | state.isin(["active", "open", "trading"])).to_numpy()
    if "observed_halt_flag" in frame:
        sane &= ~frame["observed_halt_flag"].fillna(False).to_numpy(bool)
    return sane


def attach_market_replay(
    candidates: pd.DataFrame,
    books: pd.DataFrame,
    markets: pd.DataFrame,
    *,
    transport_ms: int = 5,
    tau_high_s: int = 240,
    tau_low_s: int = 15,
    max_book_age_ms: int = 1000,
    alive_ms: int = 2000,
) -> pd.DataFrame:
    """Attach causal Polymarket state and the first quote eligible for matching.

    Candidate selection is independent of future books.  The current row is the final snapshot no
    later than the decision; the match row is the first snapshot at/after historical hold plus
    transport.  Stale repeated snapshots are explicitly tagged through nearby state changes.
    """
    if candidates.empty or books.empty or markets.empty:
        return pd.DataFrame()
    rows = []
    c = candidates.sort_values("decision_ms", kind="stable").copy()
    five = markets[(markets["horizon"] == 5) & markets["up_won"].notna()].copy()
    if five.empty:
        return pd.DataFrame()
    five["market_id"] = five["market_id"].astype(str)
    five = five.sort_values("start", kind="stable").reset_index(drop=True)
    decisions = c["decision_ms"].to_numpy(np.int64)
    start_values = five["start"].to_numpy(np.int64)
    market_i = np.searchsorted(start_values, decisions, side="right") - 1
    valid_i = market_i >= 0
    safe_i = np.maximum(market_i, 0)
    ends = five["end"].to_numpy(np.int64)[safe_i]
    valid_i &= (decisions >= ends - tau_high_s * 1000) & (decisions <= ends - tau_low_s * 1000)
    c = c.loc[valid_i].copy()
    c["_market_id"] = five["market_id"].to_numpy()[safe_i[valid_i]]
    candidate_groups = {mid: g for mid, g in c.groupby("_market_id", sort=False)}
    book_groups = {
        str(mid): g for mid, g in books.assign(market_id=books["market_id"].astype(str)).groupby("market_id", sort=False)
    }
    state_cols = [
        col for col in (
            "up_best_bid", "up_best_ask", "down_best_bid", "down_best_ask",
            "up_bid_size", "up_ask_size", "down_bid_size", "down_ask_size",
        ) if col in books
    ]
    for market in five.itertuples(index=False):
        mid = str(market.market_id)
        if mid not in candidate_groups or mid not in book_groups:
            continue
        b = book_groups[mid]
        b = b.drop_duplicates("timestamp_ms", keep="last").sort_values("timestamp_ms", kind="stable").reset_index(drop=True)
        bt = b["timestamp_ms"].to_numpy(np.int64)
        sane = _sane_books(b)
        state = b[state_cols].to_numpy(float)
        same = (state[1:] == state[:-1]) | (np.isnan(state[1:]) & np.isnan(state[:-1]))
        changed = np.r_[True, ~same.all(axis=1)]
        change_ts = bt[changed]
        part = candidate_groups[mid]
        for candidate in part.to_dict("records"):
            candidate.pop("_market_id", None)
            decision = int(candidate["decision_ms"])
            current_i = np.searchsorted(bt, decision, side="right") - 1
            if current_i < 0:
                continue
            hold = taker_hold_ms(decision)
            eligible = decision + hold + transport_ms
            match_i = np.searchsorted(bt, eligible, side="left")
            if match_i >= len(b):
                continue
            before_i = np.searchsorted(change_ts, decision, side="right") - 1
            after_i = np.searchsorted(change_ts, bt[match_i], side="right")
            alive_before = before_i >= 0 and decision - change_ts[before_i] <= alive_ms
            alive_after = after_i < len(change_ts) and change_ts[after_i] - bt[match_i] <= alive_ms
            current = b.iloc[current_i]
            match = b.iloc[match_i]
            ub_sz = float(current.get("up_bid_size", np.nan))
            ua_sz = float(current.get("up_ask_size", np.nan))
            db_sz = float(current.get("down_bid_size", np.nan))
            da_sz = float(current.get("down_ask_size", np.nan))
            up_den, down_den = ub_sz + ua_sz, db_sz + da_sz
            out = dict(candidate)
            out.update(
                market_id=str(market.market_id),
                market_start_ms=int(market.start),
                market_end_ms=int(market.end),
                tau_s=(int(market.end) - decision) / 1000.0,
                up_won=float(market.up_won),
                book_ts_ms=int(bt[current_i]),
                feature_max_ms=int(max(float(candidate.get("feature_max_ms", -np.inf)), bt[current_i])),
                book_age_ms=decision - int(bt[current_i]),
                up_bid0=float(current["up_best_bid"]),
                up_ask0=float(current["up_best_ask"]),
                down_bid0=float(current["down_best_bid"]),
                down_ask0=float(current["down_best_ask"]),
                up_bid_size0=ub_sz,
                up_ask_size0=ua_sz,
                down_bid_size0=db_sz,
                down_ask_size0=da_sz,
                up_mid0=(float(current["up_best_bid"]) + float(current["up_best_ask"])) / 2.0,
                up_spread0=float(current["up_best_ask"]) - float(current["up_best_bid"]),
                up_imbalance0=(ub_sz - ua_sz) / up_den if np.isfinite(up_den) and up_den > 0 else np.nan,
                down_imbalance0=(db_sz - da_sz) / down_den if np.isfinite(down_den) and down_den > 0 else np.nan,
                book_ok=bool(sane[current_i] and decision - bt[current_i] <= max_book_age_ms and alive_before),
                eligible_ms=int(eligible),
                match_ms=int(bt[match_i]),
                match_delay_ms=int(bt[match_i] - decision),
                match_up_ask=float(match["up_best_ask"]),
                match_down_ask=float(match["down_best_ask"]),
                match_up_size=float(match.get("up_ask_size", np.nan)),
                match_down_size=float(match.get("down_ask_size", np.nan)),
                match_book_ok=bool(sane[match_i] and bt[match_i] - eligible <= max_book_age_ms and alive_after),
            )
            rows.append(out)
    return pd.DataFrame(rows)


def simulate_taker(
    books: pd.DataFrame,
    *,
    decision_ms: int,
    side: str,
    limit_price: float,
    won: float,
    transport_ms: int = 5,
    shares: float = 5.0,
) -> dict:
    """Simulate an IOC-like marketable limit at the first observable match instant.

    The order gets exactly one execution opportunity after the historical fixed hold.  A quote
    returning below the limit later is not counted as a fill.
    """
    eligible = int(decision_ms + taker_hold_ms(decision_ms) + transport_ms)
    ordered = books.sort_values("timestamp_ms", kind="stable")
    pos = np.searchsorted(ordered["timestamp_ms"].to_numpy(np.int64), eligible, side="left")
    base = {
        "decision_ms": int(decision_ms),
        "eligible_ms": eligible,
        "match_ms": np.nan,
        "filled": False,
        "price": np.nan,
        "shares": 0.0,
        "fee_per_share": 0.0,
        "pnl_per_share": 0.0,
        "pnl_usd": 0.0,
    }
    if pos >= len(ordered):
        return base
    row = ordered.iloc[pos]
    ask_col = "up_best_ask" if side.lower() == "up" else "down_best_ask"
    size_col = "up_ask_size" if side.lower() == "up" else "down_ask_size"
    ask = float(row[ask_col])
    base["match_ms"] = int(row["timestamp_ms"])
    if not math.isfinite(ask) or ask > limit_price:
        return base
    available = float(row.get(size_col, shares))
    quantity = min(float(shares), available) if math.isfinite(available) else float(shares)
    if quantity <= 0:
        return base
    fee = taker_fee(ask)
    pnl = float(won) - ask - fee
    base.update(
        filled=True,
        price=ask,
        shares=quantity,
        fee_per_share=fee,
        pnl_per_share=pnl,
        pnl_usd=quantity * pnl,
    )
    return base


def select_joint_signals(
    candidates: pd.DataFrame,
    *,
    min_jump: float,
    min_direction: float,
    min_fill: float,
    min_edge: float,
) -> pd.DataFrame:
    """Apply locked joint thresholds and retain the earliest signal per market."""
    ok = candidates[
        (candidates["p_jump"] >= min_jump)
        & (candidates["p_up_given_jump"] >= min_direction)
        & (candidates["p_fill"] >= min_fill)
        & (candidates["expected_edge"] >= min_edge)
    ]
    return (
        ok.sort_values(["market_id", "decision_ms"], kind="stable")
        .drop_duplicates("market_id", keep="first")
        .reset_index(drop=True)
    )


def model_matrix(frame: pd.DataFrame, *, include_venue: bool = True) -> pd.DataFrame:
    """Causal model inputs; labels and all post-decision execution fields are excluded by design."""
    index = frame.index

    def col(name: str, default=np.nan) -> pd.Series:
        if name in frame:
            return pd.to_numeric(frame[name], errors="coerce").astype(float)
        return pd.Series(default, index=index, dtype=float)

    values: dict[str, pd.Series | np.ndarray] = {
        "last_size_log": np.log1p(col("last_size").clip(lower=0)),
        "last_sign": col("last_sign"),
        "interarrival_log": np.log1p(col("interarrival_ms").clip(lower=0)),
    }
    for window in RETURN_WINDOWS_MS:
        values[f"ret_{window}"] = col(f"ret_{window}ms_bps")
    for window in FLOW_WINDOWS_MS:
        flow = col(f"flow_{window}ms")
        volume = col(f"volume_{window}ms")
        values[f"flow_ratio_{window}"] = flow / volume.replace(0, np.nan)
        values[f"flow_log_{window}"] = np.sign(flow) * np.log1p(np.abs(flow))
        values[f"volume_log_{window}"] = np.log1p(volume.clip(lower=0))
        values[f"prints_log_{window}"] = np.log1p(col(f"prints_{window}ms").clip(lower=0))
    if include_venue:
        values["venue_age_log"] = np.log1p(col("venue_age_ms").clip(lower=0))
        for window in VENUE_WINDOWS_MS:
            values[f"venue_ret_{window}"] = col(f"venue_ret_{window}ms_bps")
        for window in (50, 100, 250):
            flow = col(f"venue_flow_{window}ms")
            values[f"venue_flow_log_{window}"] = np.sign(flow) * np.log1p(np.abs(flow))
    values.update({
        "tau_fraction": col("tau_s") / 300.0,
        "book_age_log": np.log1p(col("book_age_ms").clip(lower=0)),
        "up_mid": col("up_mid0"),
        "up_spread": col("up_spread0"),
        "up_ask": col("up_ask0"),
        "down_ask": col("down_ask0"),
        "up_imbalance": col("up_imbalance0"),
        "down_imbalance": col("down_imbalance0"),
        "up_bid_size_log": np.log1p(col("up_bid_size0").clip(lower=0)),
        "up_ask_size_log": np.log1p(col("up_ask_size0").clip(lower=0)),
        "down_bid_size_log": np.log1p(col("down_bid_size0").clip(lower=0)),
        "down_ask_size_log": np.log1p(col("down_ask_size0").clip(lower=0)),
    })
    return pd.DataFrame(values, index=index).replace([np.inf, -np.inf], np.nan)


def _bounded_sample(indices: np.ndarray, maximum: int, rng: np.random.Generator) -> np.ndarray:
    if len(indices) <= maximum:
        return indices
    return np.sort(rng.choice(indices, maximum, replace=False))


def fit_joint_models(
    frame: pd.DataFrame,
    *,
    include_venue: bool = True,
    random_state: int = 120,
) -> dict:
    """Fit jump direction, terminal outcome, quote survival and eligible-ask models."""
    from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor

    clean = frame[
        frame["book_ok"].astype(bool)
        & (frame["feature_max_ms"] <= frame["decision_ms"])
    ].reset_index(drop=True)
    if clean.empty:
        raise ValueError("no healthy causal training rows")
    X = model_matrix(clean, include_venue=include_venue)
    rng = np.random.default_rng(random_state)
    y_jump = clean["jump_direction"].to_numpy(np.int8)
    positive = np.flatnonzero(y_jump != 0)
    negative = np.flatnonzero(y_jump == 0)
    negative = _bounded_sample(negative, min(700_000, max(100_000, 20 * len(positive))), rng)
    jump_i = np.sort(np.r_[positive, negative])
    common_classifier = dict(
        learning_rate=0.06,
        max_iter=140,
        max_leaf_nodes=15,
        min_samples_leaf=200,
        l2_regularization=2.0,
        early_stopping=False,
        random_state=random_state,
    )
    jump = HistGradientBoostingClassifier(class_weight="balanced", **common_classifier)
    jump.fit(X.iloc[jump_i], y_jump[jump_i])

    # Repeated candidates from one market must not let an active market dominate the terminal model.
    terminal_i = np.arange(len(clean))
    if len(terminal_i) > 350_000:
        weight = 1.0 / clean.groupby("market_id")["market_id"].transform("size").to_numpy(float)
        weight /= weight.sum()
        terminal_i = np.sort(rng.choice(terminal_i, 350_000, replace=False, p=weight))
    terminal = HistGradientBoostingClassifier(**common_classifier)
    terminal.fit(X.iloc[terminal_i], clean["up_won"].to_numpy(float)[terminal_i])

    match_wait = clean["match_ms"].to_numpy(float) - clean["eligible_ms"].to_numpy(float)
    quote_ok = clean["match_book_ok"].astype(bool).to_numpy() & (match_wait >= 0) & (match_wait <= 250)
    quote_i = np.flatnonzero(quote_ok)
    quote_i = _bounded_sample(quote_i, 500_000, rng)
    if len(quote_i) < 1000:
        raise ValueError(f"only {len(quote_i)} usable execution rows")
    regressors, fill_models = {}, {}
    reg_params = dict(
        loss="absolute_error",
        learning_rate=0.06,
        max_iter=120,
        max_leaf_nodes=15,
        min_samples_leaf=200,
        l2_regularization=2.0,
        early_stopping=False,
        random_state=random_state,
    )
    for side in ("up", "down"):
        target = clean[f"match_{side}_ask"].to_numpy(float)
        usable = quote_i[np.isfinite(target[quote_i])]
        regressor = HistGradientBoostingRegressor(**reg_params)
        regressor.fit(X.iloc[usable], target[usable])
        regressors[side] = regressor
        survived = target[usable] <= clean[f"{side}_ask0"].to_numpy(float)[usable] + 0.03 + 1e-12
        fill_model = HistGradientBoostingClassifier(**common_classifier)
        fill_model.fit(X.iloc[usable], survived.astype(np.int8))
        fill_models[side] = fill_model
    return {
        "include_venue": include_venue,
        "jump": jump,
        "terminal": terminal,
        "ask": regressors,
        "fill": fill_models,
        "train_rows": int(len(clean)),
        "jump_rows": int(len(jump_i)),
        "quote_rows": int(len(quote_i)),
    }


def predict_joint_models(models: dict, frame: pd.DataFrame) -> pd.DataFrame:
    """Append out-of-sample predictions used by the policy; no realized field is consulted."""
    X = model_matrix(frame, include_venue=bool(models["include_venue"]))
    probability = models["jump"].predict_proba(X)
    classes = models["jump"].classes_
    p_up_jump = probability[:, int(np.flatnonzero(classes == 1)[0])] if (classes == 1).any() else np.zeros(len(frame))
    p_down_jump = probability[:, int(np.flatnonzero(classes == -1)[0])] if (classes == -1).any() else np.zeros(len(frame))
    p_up_win = models["terminal"].predict_proba(X)[:, 1]
    pred_up_ask = np.clip(models["ask"]["up"].predict(X), 0.01, 0.99)
    pred_down_ask = np.clip(models["ask"]["down"].predict(X), 0.01, 0.99)
    p_up_fill = models["fill"]["up"].predict_proba(X)[:, 1]
    p_down_fill = models["fill"]["down"].predict_proba(X)[:, 1]
    up = p_up_jump >= p_down_jump
    out = frame.copy()
    out["p_up_jump"] = p_up_jump
    out["p_down_jump"] = p_down_jump
    out["p_jump"] = np.where(up, p_up_jump, p_down_jump)
    out["pred_side"] = np.where(up, "Up", "Down")
    out["p_side_win"] = np.where(up, p_up_win, 1.0 - p_up_win)
    out["p_fill"] = np.where(up, p_up_fill, p_down_fill)
    out["pred_match_ask"] = np.where(up, pred_up_ask, pred_down_ask)
    return out


def choose_policy(validation: pd.DataFrame, *, min_fills: int = 30) -> dict:
    """Choose one locked policy on validation by conservative five-share profit."""
    jump_levels = np.unique(np.quantile(validation["p_jump"].dropna(), [0.90, 0.95, 0.975, 0.99]))
    best = None
    for jump in jump_levels:
        for fill in (0.40, 0.60, 0.75):
            for edge in (0.02, 0.04, 0.06, 0.08):
                for chase in (0.03,):  # matches the fill model and the existing live order rule
                    signals, fills = replay_joint_predictions(
                        validation,
                        min_jump=float(jump),
                        min_fill=fill,
                        min_expected_edge=edge,
                        min_realizable_edge=edge,
                        chase=chase,
                    )
                    if len(fills) < min_fills:
                        continue
                    pnl = fills["pnl_usd"].to_numpy(float)
                    penalty = 1.645 * pnl.std(ddof=1) * math.sqrt(len(pnl)) if len(pnl) > 1 else np.inf
                    score = float(pnl.sum() - penalty)
                    item = {
                        "min_jump": float(jump), "min_fill": fill,
                        "min_expected_edge": edge, "min_realizable_edge": edge,
                        "chase": chase, "validation_signals": int(len(signals)),
                        "validation_fills": int(len(fills)),
                        "validation_edge": float(fills["pnl_per_share"].mean()),
                        "validation_profit_usd": float(pnl.sum()), "selection_score": score,
                    }
                    if best is None or item["selection_score"] > best["selection_score"]:
                        best = item
    if best is None:
        raise ValueError("no validation policy reached the minimum fill count")
    return best


def evaluate_policy(predictions: pd.DataFrame, policy: dict) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    keys = ("min_jump", "min_fill", "min_expected_edge", "min_realizable_edge", "chase")
    signals, fills = replay_joint_predictions(predictions, **{key: policy[key] for key in keys})
    summary = cluster_summary(fills)
    summary["candidates"] = int(len(predictions))
    summary["signals"] = int(len(signals))
    summary["fill_rate"] = float(len(fills) / len(signals)) if len(signals) else np.nan
    summary["price_miss"] = int((~signals["market_id"].isin(set(fills["market_id"]))).sum()) if len(signals) else 0
    if len(fills):
        ordered = fills.sort_values("decision_ms", kind="stable")
        equity = ordered["pnl_usd"].cumsum().to_numpy(float)
        drawdown = equity - np.maximum.accumulate(np.r_[0.0, equity])[-len(equity):]
        summary["max_drawdown_usd"] = float(-drawdown.min())
        days = max(1, ordered["day"].nunique()) if "day" in ordered else 1
        summary["profit_per_covered_day"] = float(summary["profit_usd"] / days)
        delay = fills["match_ms"].to_numpy(float) - fills["decision_ms"].to_numpy(float)
        summary["match_delay_p50_ms"] = float(np.quantile(delay, 0.50))
        summary["match_delay_p90_ms"] = float(np.quantile(delay, 0.90))
        event = fills["jump_direction"].to_numpy(int) != 0
        if event.any():
            relative = fills.loc[event, "match_ms"].to_numpy(float) - fills.loc[event, "jump_ts_ms"].to_numpy(float)
            summary["jump_to_match_p50_ms"] = float(np.quantile(relative, 0.50))
            summary["jump_to_match_p90_ms"] = float(np.quantile(relative, 0.90))
        else:
            summary["jump_to_match_p50_ms"] = np.nan
            summary["jump_to_match_p90_ms"] = np.nan
    else:
        summary["max_drawdown_usd"] = 0.0
        summary["profit_per_covered_day"] = 0.0
        summary["match_delay_p50_ms"] = np.nan
        summary["match_delay_p90_ms"] = np.nan
        summary["jump_to_match_p50_ms"] = np.nan
        summary["jump_to_match_p90_ms"] = np.nan
    return summary, signals, fills


def prediction_metrics(predictions: pd.DataFrame, min_jump: float) -> dict:
    """Out-of-sample quality of the actual 80–160 ms source-time target."""
    from sklearn.metrics import average_precision_score, roc_auc_score

    actual = predictions["jump_direction"].to_numpy(int)
    event = actual != 0
    p_event = predictions["p_up_jump"].to_numpy(float) + predictions["p_down_jump"].to_numpy(float)
    pred_direction = np.where(
        predictions["p_up_jump"].to_numpy(float) >= predictions["p_down_jump"].to_numpy(float), 1, -1
    )
    selected = predictions["p_jump"].to_numpy(float) >= min_jump
    return {
        "rows": int(len(predictions)),
        "prevalence": float(event.mean()),
        "average_precision": float(average_precision_score(event, p_event)),
        "roc_auc": float(roc_auc_score(event, p_event)) if event.any() and (~event).any() else np.nan,
        "direction_accuracy_on_events": float((pred_direction[event] == actual[event]).mean()) if event.any() else np.nan,
        "selected_rows": int(selected.sum()),
        "selected_event_precision": float(event[selected].mean()) if selected.any() else np.nan,
        "selected_exact_precision": float((pred_direction[selected] == actual[selected]).mean()) if selected.any() else np.nan,
    }


def direction_only_control(predictions: pd.DataFrame, policy: dict) -> tuple[dict, pd.DataFrame]:
    """Same advance predictor, but no terminal-value or fill model; chase the current ask blindly."""
    control = predictions.copy()
    control["p_side_win"] = 0.99
    control["p_fill"] = 1.0
    up = control["pred_side"].astype(str).str.lower() == "up"
    control["pred_match_ask"] = np.where(up, control["up_ask0"], control["down_ask0"])
    signals, fills = replay_joint_predictions(
        control,
        min_jump=policy["min_jump"], min_fill=0.0, min_expected_edge=-1.0,
        min_realizable_edge=0.0, chase=policy["chase"],
    )
    summary = cluster_summary(fills)
    summary.update(signals=int(len(signals)), fill_rate=float(len(fills) / len(signals)) if len(signals) else np.nan)
    return summary, fills


def placebo_jump_models(models: dict, train: pd.DataFrame, *, random_state: int = 121) -> dict:
    """Replace only the ahead model with one trained on deterministically shuffled labels."""
    from sklearn.base import clone

    clean = train[
        train["book_ok"].astype(bool) & (train["feature_max_ms"] <= train["decision_ms"])
    ].reset_index(drop=True)
    X = model_matrix(clean, include_venue=bool(models["include_venue"]))
    rng = np.random.default_rng(random_state)
    label = clean["jump_direction"].to_numpy(np.int8).copy()
    rng.shuffle(label)
    positive = np.flatnonzero(label != 0)
    negative = np.flatnonzero(label == 0)
    negative = _bounded_sample(negative, min(700_000, max(100_000, 20 * len(positive))), rng)
    use = np.sort(np.r_[positive, negative])
    jump = clone(models["jump"])
    jump.fit(X.iloc[use], label[use])
    out = dict(models)
    out["jump"] = jump
    return out


def _format_summary(name: str, summary: dict) -> str:
    edge = 100 * summary.get("edge", np.nan)
    lo, hi = 100 * summary.get("ci_low", np.nan), 100 * summary.get("ci_high", np.nan)
    return (
        f"| {name} | {summary.get('candidates', 0):,} | {summary.get('signals', 0):,} | "
        f"{summary.get('fills', 0):,} | {summary.get('fill_rate', np.nan):.1%} | "
        f"{edge:+.2f}¢ [{lo:+.2f}, {hi:+.2f}] | ${summary.get('profit_usd', 0):+.2f} | "
        f"${summary.get('profit_per_covered_day', 0):+.2f} | ${summary.get('max_drawdown_usd', 0):.2f} |"
    )


def save_public_bundle(bundle: dict, path: str | Path) -> None:
    """Persist fitted public models and locked-C evidence for local September scoring."""
    import joblib

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, target, compress=3)


def load_public_bundle(path: str | Path) -> dict:
    """Load a bundle written by :func:`save_public_bundle`."""
    import joblib

    bundle = joblib.load(path)
    if not isinstance(bundle, dict) or bundle.get("schema_version") != 1:
        raise ValueError("unsupported CrossVenue-120 public bundle")
    return bundle


def _read_unique_frames(paths: list[str]) -> pd.DataFrame:
    if not paths:
        raise ValueError("at least one parquet path is required")
    frame = pd.concat([pd.read_parquet(path) for path in paths], ignore_index=True)
    return frame.drop_duplicates(["market_id", "decision_ms"], keep="last").reset_index(drop=True)


def fit_public_bundle(public_paths: list[str]) -> dict:
    """Fit on A, select once on B, and lock all public-C evidence into a portable bundle."""
    public = _read_unique_frames(public_paths)
    train = public[public["day"] <= "2026-07-15"].reset_index(drop=True)
    validation = public[(public["day"] >= "2026-07-16") & (public["day"] <= "2026-08-16")].reset_index(drop=True)
    public_locked = public[public["day"] >= "2026-08-17"].reset_index(drop=True)
    del public
    if min(map(len, (train, validation, public_locked))) == 0:
        raise ValueError("one of train/validation/public-lockbox is empty")

    full = fit_joint_models(train, include_venue=True)
    validation_full = predict_joint_models(full, validation)
    policy = choose_policy(validation_full)
    del validation_full
    public_prediction = predict_joint_models(full, public_locked)
    public_summary, _, public_fills = evaluate_policy(public_prediction, policy)
    public_direction, _ = direction_only_control(public_prediction, policy)
    public_metrics = prediction_metrics(public_prediction, policy["min_jump"])
    del public_prediction

    spot = fit_joint_models(train, include_venue=False)
    validation_spot = predict_joint_models(spot, validation)
    spot_policy = choose_policy(validation_spot)
    del validation_spot
    public_spot_summary, _, _ = evaluate_policy(predict_joint_models(spot, public_locked), spot_policy)

    placebo = placebo_jump_models(full, train)
    validation_placebo = predict_joint_models(placebo, validation)
    placebo_policy = choose_policy(validation_placebo)
    del validation_placebo
    public_placebo, _, _ = evaluate_policy(predict_joint_models(placebo, public_locked), placebo_policy)

    public_summary["candidates"] = len(public_locked)
    public_spot_summary["candidates"] = len(public_locked)
    public_placebo["candidates"] = len(public_locked)
    return {
        "schema_version": 1,
        "full": full,
        "spot": spot,
        "placebo": placebo,
        "policy": policy,
        "spot_policy": spot_policy,
        "placebo_policy": placebo_policy,
        "public_summary": public_summary,
        "public_spot_summary": public_spot_summary,
        "public_placebo": public_placebo,
        "public_direction": public_direction,
        "public_metrics": public_metrics,
        "public_fills": public_fills,
        "split": {
            "train_min": str(train["day"].min()), "train_max": str(train["day"].max()),
            "train_rows": int(len(train)),
            "validation_min": str(validation["day"].min()), "validation_max": str(validation["day"].max()),
            "validation_rows": int(len(validation)),
            "public_min": str(public_locked["day"].min()), "public_max": str(public_locked["day"].max()),
            "public_rows": int(len(public_locked)),
        },
    }


def lockbox_verdict(
    public_summary: dict,
    september_summary: dict,
    public_spot_summary: dict,
    september_spot_summary: dict,
    public_placebo: dict,
    september_placebo: dict,
) -> tuple[str, bool, bool]:
    """Require both profitable lockboxes and evidence that the ahead mechanism adds value."""
    pnl_pass = (
        public_summary["fills"] >= 30
        and public_summary["ci_low"] > 0
        and september_summary["fills"] >= 30
        and september_summary["ci_low"] > 0
    )
    mechanism_pass = (
        public_summary["edge"] > public_spot_summary["edge"]
        and september_summary["edge"] > september_spot_summary["edge"]
        and public_summary["edge"] > public_placebo["edge"]
        and september_summary["edge"] > september_placebo["edge"]
    )
    if pnl_pass and mechanism_pass:
        return "通过双锁箱与提前预测机制检验", True, True
    if pnl_pass:
        return "收益门槛通过，但提前预测机制未通过", True, False
    return "未通过双锁箱收益门槛", False, mechanism_pass


def jump_attribution(fills: pd.DataFrame) -> dict:
    """Split realized profit by whether the promised 80–160 ms source-time jump occurred."""
    event = fills["jump_direction"].to_numpy(float) != 0 if len(fills) else np.array([], dtype=bool)

    def group(mask: np.ndarray) -> tuple[int, float, float]:
        part = fills.loc[mask]
        return (
            int(len(part)),
            float(part["pnl_per_share"].mean()) if len(part) else np.nan,
            float(part["pnl_usd"].sum()),
        )

    event_fills, event_edge, event_profit = group(event)
    no_event_fills, no_event_edge, no_event_profit = group(~event)
    return {
        "event_fills": event_fills,
        "event_rate": float(event.mean()) if len(event) else np.nan,
        "event_edge": event_edge,
        "event_profit_usd": event_profit,
        "no_event_fills": no_event_fills,
        "no_event_edge": no_event_edge,
        "no_event_profit_usd": no_event_profit,
    }


def _write_combined_report(
    bundle: dict,
    *,
    september: pd.DataFrame,
    september_summary: dict,
    september_spot_summary: dict,
    september_placebo: dict,
    september_direction: dict,
    september_metrics: dict,
    september_perp_count: int,
    september_perp_metrics: dict,
    public_attribution: dict,
    september_attribution: dict,
    pnl_pass: bool,
    mechanism_pass: bool,
    verdict: str,
    report_out: str | Path,
) -> None:
    policy = bundle["policy"]
    public_summary = bundle["public_summary"]
    public_spot_summary = bundle["public_spot_summary"]
    public_placebo = bundle["public_placebo"]
    public_direction = bundle["public_direction"]
    public_metrics = bundle["public_metrics"]
    split = bundle["split"]
    lines = [
        "# CrossVenue-120：提前预测 + 可成交性联合模型",
        "",
        f"**结论：{verdict}。** 收益门槛要求 8/17–29 与九月实录两段都至少 30 笔成交且按市场聚类的 95% CI 下界大于 0；"
        "机制门槛还要求完整模型在两段都优于去跨所特征和打乱跳动标签的对照。",
        "",
        "目标是在本机决策之后、交易所源时钟 80–160 ms（中心 120 ms）发生的 Binance ≥1 bp 跳动。"
        "特征只使用决策时已经收到的 Binance、永续和 Polymarket 数据；订单按当时历史固定压单 250/50/150 ms，"
        "再加 5 ms 传输，在第一份可用盘口上按事先固定的限价判断成交。未穿透限价就是未成交。每笔最多 5 份。",
        "",
        "## 锁定结果",
        "",
        "| 样本 | 候选 | 信号 | 成交 | 成交率 | 净 edge/份（市场聚类 95% CI） | 最多5份总收益 | 每覆盖日 | 最大回撤 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        _format_summary("C：8/17–8/29", public_summary),
        _format_summary("九月实录", september_summary),
        "",
        "这里的收益已扣 taker 费，但没有假定不可见的排队优势；成交只来自压单结束时仍低于限价的卖一。",
        "公开 C 全部处于 50 ms 固定压单时期；九月处于当前 150 ms 时期，但其实录盘口已知偏旧。因此两段日收益都不能直接外推为当前实盘收益。",
        "",
        "## 训练、选参和固定策略",
        "",
        f"- 训练 A：{split['train_min']}–{split['train_max']}，{split['train_rows']:,} 个候选。",
        f"- 选参 B：{split['validation_min']}–{split['validation_max']}，{split['validation_rows']:,} 个候选；只在这里选择一次门槛。",
        f"- 锁定 C：{split['public_min']}–{split['public_max']}，{split['public_rows']:,} 个候选；九月另有 {len(september):,} 个候选。",
        f"- 固定门槛：跳动概率 ≥ {policy['min_jump']:.4f}，成交概率 ≥ {policy['min_fill']:.2f}，"
        f"预测净 edge ≥ {100 * policy['min_expected_edge']:.0f}¢，最多追 {100 * policy['chase']:.0f}¢。",
        f"- B 段选参结果：{policy['validation_fills']:,} 成交，净 edge {100 * policy['validation_edge']:+.2f}¢/份，最多 5 份总收益 ${policy['validation_profit_usd']:+.2f}。",
        "",
        "## 实际重放延迟",
        "",
        "| 样本 | 决策→匹配 p50 / p90 | 源跳动→匹配 p50 / p90 |",
        "|---|---:|---:|",
        f"| C | {public_summary['match_delay_p50_ms']:.0f} / {public_summary['match_delay_p90_ms']:.0f} ms | "
        f"{public_summary['jump_to_match_p50_ms']:+.0f} / {public_summary['jump_to_match_p90_ms']:+.0f} ms |",
        f"| 九月 | {september_summary['match_delay_p50_ms']:.0f} / {september_summary['match_delay_p90_ms']:.0f} ms | "
        f"{september_summary['jump_to_match_p50_ms']:+.0f} / {september_summary['jump_to_match_p90_ms']:+.0f} ms |",
        "",
        "负的“源跳动→匹配”表示订单在该次跳动发生前已完成撮合；正数表示跳动后才撮合。",
        "",
        "## 提前预测本身",
        "",
        "| 样本 | 跳动基准率 | AUPRC | ROC-AUC | 有跳动时方向准确率 | 高分样本跳动率 | 高分样本方向完全命中率 |",
        "|---|---:|---:|---:|---:|---:|---:|",
        f"| C | {public_metrics['prevalence']:.2%} | {public_metrics['average_precision']:.3f} | {public_metrics['roc_auc']:.3f} | "
        f"{public_metrics['direction_accuracy_on_events']:.1%} | {public_metrics['selected_event_precision']:.1%} | {public_metrics['selected_exact_precision']:.1%} |",
        f"| 九月 | {september_metrics['prevalence']:.2%} | {september_metrics['average_precision']:.3f} | {september_metrics['roc_auc']:.3f} | "
        f"{september_metrics['direction_accuracy_on_events']:.1%} | {september_metrics['selected_event_precision']:.1%} | {september_metrics['selected_exact_precision']:.1%} |",
        "",
        "## 收益归因",
        "",
        "| 样本 | 目标跳动成交 | 占全部成交 | 有跳动 edge/份 | 有跳动收益 | 无跳动 edge/份 | 无跳动收益 |",
        "|---|---:|---:|---:|---:|---:|---:|",
        f"| C | {public_attribution['event_fills']:,} | {public_attribution['event_rate']:.2%} | "
        f"{100 * public_attribution['event_edge']:+.2f}¢ | ${public_attribution['event_profit_usd']:+.2f} | "
        f"{100 * public_attribution['no_event_edge']:+.2f}¢ | ${public_attribution['no_event_profit_usd']:+.2f} |",
        f"| 九月 | {september_attribution['event_fills']:,} | {september_attribution['event_rate']:.2%} | "
        f"{100 * september_attribution['event_edge']:+.2f}¢ | ${september_attribution['event_profit_usd']:+.2f} | "
        f"{100 * september_attribution['no_event_edge']:+.2f}¢ | ${september_attribution['no_event_profit_usd']:+.2f} |",
        "",
        "公开 C 的目标跳动成交为负，且绝大多数收益来自没有发生目标跳动的成交；因此利润不能归因于提前 120 ms 的跳动预测。",
        "",
        "## 消融与安慰剂",
        "",
        "| 版本 | C edge/份 | C 成交 | 九月 edge/份 | 九月成交 |",
        "|---|---:|---:|---:|---:|",
        f"| 完整联合模型 | {100 * public_summary['edge']:+.2f}¢ | {public_summary['fills']:,} | {100 * september_summary['edge']:+.2f}¢ | {september_summary['fills']:,} |",
        f"| 去掉永续/第二交易所特征 | {100 * public_spot_summary['edge']:+.2f}¢ | {public_spot_summary['fills']:,} | {100 * september_spot_summary['edge']:+.2f}¢ | {september_spot_summary['fills']:,} |",
        f"| 只看提前方向、盲追当前卖一 | {100 * public_direction['edge']:+.2f}¢ | {public_direction['fills']:,} | {100 * september_direction['edge']:+.2f}¢ | {september_direction['fills']:,} |",
        f"| 打乱提前标签后重新选参 | {100 * public_placebo['edge']:+.2f}¢ | {public_placebo['fills']:,} | {100 * september_placebo['edge']:+.2f}¢ | {september_placebo['fills']:,} |",
        "",
        f"- 双锁箱收益门槛：{'通过' if pnl_pass else '未通过'}。",
        f"- 提前预测机制门槛：{'通过' if mechanism_pass else '未通过'}；去掉跨所特征和打乱标签的对照没有变差，不能把收益归因于 CrossVenue-120。",
        "",
        (f"九月带 Binance 永续的子集有 {september_perp_count:,} 个候选；其提前预测 AUPRC "
         f"{september_perp_metrics.get('average_precision', np.nan):.3f}，基准率 {september_perp_metrics.get('prevalence', np.nan):.2%}。"),
        "",
        "## 仍然不能从回测中假定的事情",
        "",
        "- 公开 C 是 50 ms 压单制度且盘口为 100 ms 对齐快照；真实撮合时刻落在两帧之间时仍有离散误差。",
        "- 九月是当前 150 ms 压单制度，但记录器盘口已知偏旧；同时给 Binance 源时间统一加 100 ms 到机延迟，这不是每笔实测延迟。",
        "- 公开段的第二交易所是 Hyperliquid，九月带第二交易所的子集是 Binance 永续；跨域改善必须单列看，不能混称稳定收益。",
        "- 回测知道卖一数量，但不知道同一 150 ms 窗口里其他 taker 抢走多少；5 份仍可能被竞争者吃掉。",
        "",
        ("因此，可以进入受限小额实盘。" if pnl_pass and mechanism_pass else
         "因此，不部署 CrossVenue-120。双锁箱中的正收益只能作为独立 ValueFill 策略的研究线索，必须另行预注册并验证。"),
    ]
    Path(report_out).parent.mkdir(parents=True, exist_ok=True)
    Path(report_out).write_text("\n".join(lines) + "\n", encoding="utf-8")


def finish_backtest_bundle(
    bundle: dict,
    september_paths: list[str],
    *,
    report_out: str | Path,
    fills_out: str | Path,
) -> dict:
    """Score the untouched September recorder with an already locked public-data bundle."""
    if bundle.get("schema_version") != 1:
        raise ValueError("unsupported CrossVenue-120 public bundle")
    september = _read_unique_frames(september_paths)
    if september.empty:
        raise ValueError("September lockbox is empty")

    september_prediction = predict_joint_models(bundle["full"], september)
    september_summary, _, september_fills = evaluate_policy(september_prediction, bundle["policy"])
    september_direction, _ = direction_only_control(september_prediction, bundle["policy"])
    september_metrics = prediction_metrics(september_prediction, bundle["policy"]["min_jump"])
    september_perp = september_prediction[september_prediction["venue_feature_max_ms"].notna()]
    september_perp_count = len(september_perp)
    september_perp_metrics = (
        prediction_metrics(september_perp, bundle["policy"]["min_jump"]) if len(september_perp) else {}
    )
    del september_prediction, september_perp
    september_spot_summary, _, _ = evaluate_policy(
        predict_joint_models(bundle["spot"], september), bundle["spot_policy"]
    )
    september_placebo, _, _ = evaluate_policy(
        predict_joint_models(bundle["placebo"], september), bundle["placebo_policy"]
    )
    september_summary["candidates"] = len(september)
    september_spot_summary["candidates"] = len(september)
    september_placebo["candidates"] = len(september)

    locked_fills = pd.concat([
        bundle["public_fills"].assign(period="C 2026-08-17..29"),
        september_fills.assign(period="September recorder lockbox"),
    ], ignore_index=True)
    Path(fills_out).parent.mkdir(parents=True, exist_ok=True)
    locked_fills.to_csv(fills_out, index=False, compression="gzip")

    public_summary = bundle["public_summary"]
    public_attribution = jump_attribution(bundle["public_fills"])
    september_attribution = jump_attribution(september_fills)
    verdict, pnl_pass, mechanism_pass = lockbox_verdict(
        public_summary,
        september_summary,
        bundle["public_spot_summary"],
        september_spot_summary,
        bundle["public_placebo"],
        september_placebo,
    )
    _write_combined_report(
        bundle,
        september=september,
        september_summary=september_summary,
        september_spot_summary=september_spot_summary,
        september_placebo=september_placebo,
        september_direction=september_direction,
        september_metrics=september_metrics,
        september_perp_count=september_perp_count,
        september_perp_metrics=september_perp_metrics,
        public_attribution=public_attribution,
        september_attribution=september_attribution,
        pnl_pass=pnl_pass,
        mechanism_pass=mechanism_pass,
        verdict=verdict,
        report_out=report_out,
    )
    return {
        "verdict": verdict,
        "policy": bundle["policy"],
        "public": public_summary,
        "september": september_summary,
        "public_metrics": bundle["public_metrics"],
        "september_metrics": september_metrics,
        "pnl_pass": pnl_pass,
        "mechanism_pass": mechanism_pass,
        "public_attribution": public_attribution,
        "september_attribution": september_attribution,
    }


def run_backtest(
    public_paths: list[str],
    september_paths: list[str],
    *,
    report_out: str | Path,
    fills_out: str | Path,
) -> dict:
    """Fit public A/B/C and score September in one process."""
    return finish_backtest_bundle(
        fit_public_bundle(public_paths),
        september_paths,
        report_out=report_out,
        fills_out=fills_out,
    )


def cluster_summary(fills: pd.DataFrame, *, bootstrap_reps: int = 10_000, seed: int = 120) -> dict:
    """Summarize fills with a market-cluster bootstrap confidence interval."""
    if fills.empty:
        return {"fills": 0, "markets": 0, "edge": np.nan, "profit_usd": 0.0,
                "ci_low": np.nan, "ci_high": np.nan}
    clusters = [g["pnl_per_share"].to_numpy(float) for _, g in fills.groupby("market_id", sort=False)]
    rng = np.random.default_rng(seed)
    samples = np.empty(bootstrap_reps, dtype=float)
    for i in range(bootstrap_reps):
        chosen = rng.integers(0, len(clusters), len(clusters))
        samples[i] = np.concatenate([clusters[j] for j in chosen]).mean()
    return {
        "fills": int(len(fills)),
        "markets": int(fills["market_id"].nunique()),
        "edge": float(fills["pnl_per_share"].mean()),
        "profit_usd": float(fills["pnl_usd"].sum()),
        "ci_low": float(np.quantile(samples, 0.025)),
        "ci_high": float(np.quantile(samples, 0.975)),
    }


def read_archive_venues(path: str | Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Read Binance spot and Hyperliquid perpetual prints from one public daily archive."""
    import pyarrow.parquet as pq

    wanted = {"binance": [], "hyperliquid": []}
    with tarfile.open(path, "r|gz") as archive:
        for member in archive:
            if not (member.isfile() and "dataset=trades/" in member.name and member.name.endswith(".parquet")):
                continue
            raw = archive.extractfile(member).read()
            table = pq.read_table(io.BytesIO(raw))
            need = [c for c in ("trade_ts_ms", "recv_ts_ms", "exchange", "instrument", "price", "size", "taker_side")
                    if c in table.column_names]
            frame = table.select(need).to_pandas()
            exchange = frame["exchange"].astype(str).str.lower()
            for venue in wanted:
                part = frame[exchange == venue]
                if len(part):
                    wanted[venue].append(part.drop(columns=["exchange"], errors="ignore"))

    def finish(parts: list[pd.DataFrame]) -> pd.DataFrame:
        cols = ["trade_ts_ms", "recv_ts_ms", "price", "size", "taker_side"]
        if not parts:
            return pd.DataFrame(columns=cols)
        frame = pd.concat(parts, ignore_index=True)
        for col in ("trade_ts_ms", "recv_ts_ms", "price", "size"):
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
        frame = frame.dropna(subset=["recv_ts_ms", "price"])
        frame["recv_ts_ms"] = frame["recv_ts_ms"].astype(np.int64)
        frame["taker_side"] = frame["taker_side"].astype(str).str.lower()
        return frame.sort_values("recv_ts_ms", kind="stable").drop_duplicates().reset_index(drop=True)

    return finish(wanted["binance"]), finish(wanted["hyperliquid"])


def _write_frame(frame: pd.DataFrame, out: str | Path) -> None:
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".parquet":
        frame.to_parquet(path, index=False, compression="zstd")
    else:
        frame.to_csv(path, index=False, compression="gzip" if str(path).endswith(".gz") else None)


def extract_public(
    *,
    out: str | Path,
    workdir: str | Path,
    shard: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame:
    """Extract causal CrossVenue-120 candidates from the public daily archives."""
    import cross

    work = Path(workdir)
    work.mkdir(parents=True, exist_ok=True)
    archives = cross.archives(cross.fetch("MANIFEST.txt").decode())
    if start:
        archives = [a for a in archives if a[0][15:25] >= start]
    if end:
        archives = [a for a in archives if a[0][15:25] <= end]
    if shard:
        index, total = map(int, shard.split("/"))
        archives = archives[index::total]
    parts = []
    for name, _ in archives:
        local = work / name
        cross.fetch(name, local)
        try:
            features, market_rows, resolutions = cross.read_day(local)
            primary, secondary = read_archive_venues(local)
            markets = cross.market_table(market_rows, resolutions)
            candidates = spot_candidate_features(primary, secondary)
            replay = attach_market_replay(candidates, features, markets)
            replay["day"] = name[15:25]
            replay["source"] = "public-binance-hyperliquid"
            if len(replay):
                if not (replay["feature_max_ms"] <= replay["decision_ms"]).all():
                    raise AssertionError(f"future feature leaked on {name}")
                known_venue = replay["venue_feature_max_ms"].notna()
                if not (replay.loc[known_venue, "venue_feature_max_ms"] <= replay.loc[known_venue, "decision_ms"]).all():
                    raise AssertionError(f"future secondary-venue feature leaked on {name}")
                parts.append(replay)
            positives = int((replay.get("jump_direction", pd.Series(dtype=int)) != 0).sum())
            print(f"{name}: {len(primary):,} Binance, {len(secondary):,} Hyperliquid, "
                  f"{len(replay):,} candidates, {positives:,} forward jumps", flush=True)
        finally:
            local.unlink(missing_ok=True)
    result = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    _write_frame(result, out)
    return result


def _read_binance_vision(zips: list[str], *, receive_delay_ms: int) -> pd.DataFrame:
    parts = []
    for item in zips:
        with zipfile.ZipFile(item) as archive:
            raw = archive.read(archive.namelist()[0])
        frame = pd.read_csv(io.BytesIO(raw))
        lower = {c.lower(): c for c in frame.columns}
        time_col = lower.get("transact_time") or lower.get("timestamp")
        price_col = lower.get("price")
        size_col = lower.get("quantity") or lower.get("qty")
        maker_col = lower.get("is_buyer_maker")
        if not all((time_col, price_col, size_col, maker_col)):
            raise ValueError(f"unrecognized Binance Vision schema in {item}: {list(frame.columns)}")
        timestamp = pd.to_numeric(frame[time_col], errors="coerce").to_numpy(float)
        timestamp = np.where(timestamp > 1e15, timestamp / 1000.0, timestamp)
        maker = frame[maker_col].astype(str).str.lower().isin(["true", "1"]).to_numpy()
        parts.append(pd.DataFrame({
            "recv_ts_ms": np.rint(timestamp).astype(np.int64) + receive_delay_ms,
            "price": pd.to_numeric(frame[price_col], errors="coerce"),
            "size": pd.to_numeric(frame[size_col], errors="coerce"),
            "taker_side": np.where(maker, "sell", "buy"),
        }))
    return pd.concat(parts, ignore_index=True).sort_values("recv_ts_ms", kind="stable").reset_index(drop=True) \
        if parts else pd.DataFrame(columns=["recv_ts_ms", "price", "size", "taker_side"])


def extract_local_segment(
    *,
    spot_path: str | Path,
    book_path: str | Path,
    market_path: str | Path,
    out: str | Path,
    secondary_zips: list[str] | None = None,
    receive_delay_ms: int = 100,
) -> pd.DataFrame:
    """Extract one September recorder segment into the public-shard schema."""
    spot = pd.read_parquet(spot_path, columns=["ts_us", "p", "q", "m"])
    spot_source_ms = spot["ts_us"].to_numpy(np.int64) // 1000
    primary = pd.DataFrame({
        "trade_ts_ms": spot_source_ms,
        "recv_ts_ms": spot_source_ms + receive_delay_ms,
        "price": spot["p"].to_numpy(float),
        "size": spot["q"].to_numpy(float),
        "taker_side": np.where(spot["m"].to_numpy(bool), "sell", "buy"),
    }).sort_values("recv_ts_ms", kind="stable").reset_index(drop=True)
    secondary = _read_binance_vision(secondary_zips or [], receive_delay_ms=receive_delay_ms)
    raw_book = pd.read_parquet(
        book_path,
        columns=["market_id", "src_ms", "rcv", "bb", "ba", "b1s", "a1s"],
    )
    receive_ms = np.rint(raw_book["rcv"].to_numpy(float) * 1000).astype(np.int64)
    missing_receive = ~np.isfinite(raw_book["rcv"].to_numpy(float))
    receive_ms[missing_receive] = raw_book.loc[missing_receive, "src_ms"].to_numpy(np.int64)
    up_bid = raw_book["bb"].to_numpy(float)
    up_ask = raw_book["ba"].to_numpy(float)
    up_bid_size = raw_book["b1s"].to_numpy(float)
    up_ask_size = raw_book["a1s"].to_numpy(float)
    books = pd.DataFrame({
        "timestamp_ms": receive_ms,
        "market_id": raw_book["market_id"].astype(str),
        "lifecycle_state": "active",
        "up_best_bid": up_bid,
        "up_best_ask": up_ask,
        "down_best_bid": 1.0 - up_ask,
        "down_best_ask": 1.0 - up_bid,
        "up_bid_size": up_bid_size,
        "up_ask_size": up_ask_size,
        "down_bid_size": up_ask_size,
        "down_ask_size": up_bid_size,
    })
    raw_market = pd.read_parquet(market_path)
    raw_market = raw_market[raw_market["winner"].astype(str).str.lower().isin(["up", "down"])]
    markets = pd.DataFrame({
        "market_id": raw_market["market_id"].astype(str),
        "start": np.rint(raw_market["start_ts"].to_numpy(float) * 1000).astype(np.int64),
        "up_won": (raw_market["winner"].astype(str).str.lower() == "up").astype(float),
    })
    markets["end"] = markets["start"] + 300_000
    markets["horizon"] = 5
    candidates = spot_candidate_features(primary, secondary)
    replay = attach_market_replay(candidates, books, markets)
    if len(replay):
        replay["day"] = pd.to_datetime(replay["decision_ms"], unit="ms", utc=True).dt.strftime("%Y-%m-%d")
        replay["source"] = "september-binance-perp" if len(secondary) else "september-binance-only"
        if not (replay["feature_max_ms"] <= replay["decision_ms"]).all():
            raise AssertionError("future feature leaked in September adapter")
    _write_frame(replay, out)
    print(f"{Path(spot_path).name}: {len(primary):,} spot, {len(secondary):,} perp, "
          f"{len(replay):,} candidates", flush=True)
    return replay


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    public = sub.add_parser("extract-public")
    public.add_argument("--out", required=True)
    public.add_argument("--workdir", required=True)
    public.add_argument("--shard")
    public.add_argument("--start")
    public.add_argument("--end")
    local = sub.add_parser("extract-local")
    local.add_argument("--spot", required=True)
    local.add_argument("--book", required=True)
    local.add_argument("--markets", required=True)
    local.add_argument("--out", required=True)
    local.add_argument("--secondary-zip", action="append", default=[])
    local.add_argument("--receive-delay-ms", type=int, default=100)
    fit_report = sub.add_parser("fit-report")
    fit_report.add_argument("--public", action="append", required=True)
    fit_report.add_argument("--september", action="append", required=True)
    fit_report.add_argument("--out", required=True)
    fit_report.add_argument("--fills-out", required=True)
    fit_public = sub.add_parser("fit-public")
    fit_public.add_argument("--public", action="append", required=True)
    fit_public.add_argument("--bundle-out", required=True)
    finish_report = sub.add_parser("finish-report")
    finish_report.add_argument("--bundle", required=True)
    finish_report.add_argument("--september", action="append", required=True)
    finish_report.add_argument("--out", required=True)
    finish_report.add_argument("--fills-out", required=True)
    args = parser.parse_args(argv)
    if args.command == "extract-public":
        extract_public(out=args.out, workdir=args.workdir, shard=args.shard, start=args.start, end=args.end)
    elif args.command == "extract-local":
        extract_local_segment(
            spot_path=args.spot,
            book_path=args.book,
            market_path=args.markets,
            out=args.out,
            secondary_zips=args.secondary_zip,
            receive_delay_ms=args.receive_delay_ms,
        )
    elif args.command == "fit-report":
        result = run_backtest(
            args.public, args.september, report_out=args.out, fills_out=args.fills_out,
        )
        print(result)
    elif args.command == "fit-public":
        bundle = fit_public_bundle(args.public)
        save_public_bundle(bundle, args.bundle_out)
        print({"policy": bundle["policy"], "public": bundle["public_summary"], "split": bundle["split"]})
    else:
        result = finish_backtest_bundle(
            load_public_bundle(args.bundle),
            args.september,
            report_out=args.out,
            fills_out=args.fills_out,
        )
        print(result)


if __name__ == "__main__":
    main()
