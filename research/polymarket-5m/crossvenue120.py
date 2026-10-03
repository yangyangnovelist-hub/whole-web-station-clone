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


AUG_17_2026_1400_UTC_MS = int(pd.Timestamp("2026-08-17T14:00:00Z").timestamp() * 1000)
SEP_04_2026_1400_UTC_MS = int(pd.Timestamp("2026-09-04T14:00:00Z").timestamp() * 1000)

RETURN_WINDOWS_MS = (20, 50, 100, 250, 500, 1000, 2000)
FLOW_WINDOWS_MS = (50, 100, 250, 500, 1000)
VENUE_WINDOWS_MS = (50, 100, 250, 500)


def taker_hold_ms(timestamp_ms: int) -> int:
    """Return the fixed crypto-market taker hold in force at ``timestamp_ms``."""
    if timestamp_ms < AUG_17_2026_1400_UTC_MS:
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
    horizon_ms: int = 120,
) -> pd.DataFrame:
    """Build event-driven, strictly causal primary/secondary venue features.

    With no explicit decisions, the first primary print in each ``step_ms`` bucket is considered
    and a fixed, causal recent-move gate is applied.  Labels are the first ±``jump_bps`` crossing
    after the decision and no later than ``horizon_ms``.  The function records both venues' newest
    consumed timestamps so extraction jobs can assert causality before writing a shard.
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
    cutoff = jump_bps / 10_000.0
    for n, (decision, anchor_i) in enumerate(zip(decisions, pi)):
        lo = np.searchsorted(pt, decision, side="right")
        hi = np.searchsorted(pt, decision + horizon_ms, side="right")
        if lo >= hi:
            continue
        r = np.log(pp[lo:hi] / pp[anchor_i])
        hit = np.flatnonzero(np.abs(r) >= cutoff)
        if len(hit):
            j = lo + int(hit[0])
            direction[n] = 1 if r[hit[0]] > 0 else -1
            jump_ts[n] = pt[j]
    rows["jump_direction"] = direction
    rows["jump_ts_ms"] = jump_ts
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
    primary = pd.DataFrame({
        "recv_ts_ms": (spot["ts_us"].to_numpy(np.int64) // 1000) + receive_delay_ms,
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
    args = parser.parse_args(argv)
    if args.command == "extract-public":
        extract_public(out=args.out, workdir=args.workdir, shard=args.shard, start=args.start, end=args.end)
    else:
        extract_local_segment(
            spot_path=args.spot,
            book_path=args.book,
            market_path=args.markets,
            out=args.out,
            secondary_zips=args.secondary_zip,
            receive_delay_ms=args.receive_delay_ms,
        )


if __name__ == "__main__":
    main()
