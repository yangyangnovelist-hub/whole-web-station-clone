"""Frozen forward test for zoo100 rule #27 on independently recorded BTC books.

The rule was selected without September/October books in commit 86e2767 at
2026-10-01T06:20:50Z: once per market, between 240 and 30 seconds remaining,
buy in the direction of the first five-second Binance move greater than 2.5
trailing per-second sigma.  This module uses Binance receipt time for the
signal and the executable Polymarket ask after the configured local-to-match
lag.  It never submits orders.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

import binary as bo
import latency as lt
from asian180 import forward_gate

SIGNAL_FREEZE = "2026-10-01T06:20:50Z"
PROTOCOL_FREEZE = "2026-10-03T15:00:57Z"
FREEZE_TS = pd.Timestamp(SIGNAL_FREEZE).timestamp()
PROTOCOL_FREEZE_TS = pd.Timestamp(PROTOCOL_FREEZE).timestamp()
Z = 2.5
LOOKBACK_S = 5
TAU_HI, TAU_LO = 240, 30
LAGS = (0.3, 0.35, 0.4, 0.5)
PRIMARY_LAG = 0.4
MIN_SIZE = 5.0
MIN_PRICE, MAX_PRICE = 0.02, 0.98
STOP_N = 1_000
MIN_DAYS = 7
ALPHA = 0.01
SIGNAL_COLS = ["market_id", "start_ts", "t", "side", "winner", "z5"]
RESULT_COLS = ["market_id", "start_ts", "t", "day", "side", "z5", "lag", "won", "price", "fee",
               "size", "filled", "pnl", "reason"]
FROZEN_SPEC = {
    "origin_commit": "86e27671f2312ecd1c2064dd15cbe77c8d111fc2",
    "signal_frozen_at": SIGNAL_FREEZE,
    "protocol_frozen_at": PROTOCOL_FREEZE,
    "coin": "btc",
    "lookback_s": LOOKBACK_S,
    "z": Z,
    "tau_hi": TAU_HI,
    "tau_lo": TAU_LO,
    "signal_clock": "ceil(receive_ts) once per completed second",
    "lags_s": LAGS,
    "primary_lag_s": PRIMARY_LAG,
    "minimum_top_level_shares": MIN_SIZE,
    "price_band": [MIN_PRICE, MAX_PRICE],
    "fee": "Polymarket crypto taker fee",
    "first_fills": STOP_N,
    "minimum_days": MIN_DAYS,
    "alpha": ALPHA,
}


def strategy_fingerprint() -> str:
    raw = json.dumps(FROZEN_SPEC, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def load_manifest(path: str | Path) -> dict:
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    if manifest.get("strategy_fingerprint") != strategy_fingerprint():
        raise ValueError("momentum5s freeze manifest does not match the executable strategy")
    return manifest


def received_grid(spot: pd.DataFrame, *, max_gap: int = 10, min_periods: int = 300):
    """Log-price and trailing volatility grids using only trades already received."""
    ordered = spot.sort_values("receive_ts", kind="stable")
    # The strategy evaluates only on whole-second boundaries.  A trade received at
    # S+0.9 is first usable at S+1, never retroactively at S.
    sec = np.ceil(ordered["receive_ts"].to_numpy(float)).astype("int64")
    last = pd.Series(np.log(ordered["price"].to_numpy(float)), index=sec).groupby(level=0).last()
    grid = last.reindex(range(int(last.index[0]), int(last.index[-1]) + 1)).ffill(limit=max_gap)
    sigma = grid.diff().rolling(600, min_periods=min_periods).std()
    return grid, sigma


def signals_from_grid(
    markets: pd.DataFrame,
    grid: pd.Series,
    sigma: pd.Series,
    *,
    z: float = Z,
    lookback_s: int = LOOKBACK_S,
) -> pd.DataFrame:
    """Return the first qualifying once-per-second signal in each market."""
    rows = []
    for market in markets.itertuples():
        if str(market.winner) not in ("Up", "Down"):
            continue
        first = int(np.ceil(float(market.start_ts) + 300 - TAU_HI))
        last = int(np.floor(float(market.start_ts) + 300 - TAU_LO))
        seconds = np.arange(first, last + 1, dtype=np.int64)
        now = grid.reindex(seconds).to_numpy(float)
        before = grid.reindex(seconds - lookback_s).to_numpy(float)
        scale = sigma.reindex(seconds - 1).to_numpy(float) * np.sqrt(lookback_s)
        with np.errstate(invalid="ignore", divide="ignore"):
            score = (now - before) / scale
        hit = np.flatnonzero(np.isfinite(score) & (np.abs(score) > z))
        if not len(hit):
            continue
        i = int(hit[0])
        rows.append({
            "market_id": str(market.market_id),
            "start_ts": float(market.start_ts),
            "t": float(seconds[i]),
            "side": "Up" if score[i] > 0 else "Down",
            "winner": str(market.winner),
            "z5": float(abs(score[i])),
        })
    return pd.DataFrame(rows, columns=SIGNAL_COLS)


def signals(markets: pd.DataFrame, spot: pd.DataFrame) -> pd.DataFrame:
    if markets.empty or spot.empty:
        return pd.DataFrame(columns=SIGNAL_COLS)
    markets = markets[(markets["start_ts"] >= FREEZE_TS) & markets["winner"].isin(["Up", "Down"])]
    if markets.empty:
        return pd.DataFrame(columns=SIGNAL_COLS)
    grid, sigma = received_grid(spot)
    return signals_from_grid(markets, grid, sigma)


def execute(
    signal_rows: pd.DataFrame,
    book,
    *,
    lags=LAGS,
    min_size: float = MIN_SIZE,
) -> pd.DataFrame:
    """Price each signal at the exchange book after lag; insufficient depth is not a fill."""
    rows = []
    for signal in signal_rows.itertuples():
        for lag in lags:
            at = float(signal.t) + float(lag)
            # Only require a recent exchange-book update already present at the
            # simulated match time.  A later update must not decide whether this fills.
            healthy = book.at(signal.market_id, at, max_age=2.0) is not None
            disconnected = bool(book.across_close(signal.market_id, float(signal.t), at))
            price, size = book.side_ask(signal.market_id, at, signal.side) if healthy and not disconnected else (np.nan, np.nan)
            quoted = bool(np.isfinite(price) and MIN_PRICE <= price <= MAX_PRICE and np.isfinite(size))
            filled = bool(quoted and size >= min_size)
            won = float(signal.winner == signal.side)
            fee = float(bo.taker_fee(price)) if quoted else np.nan
            pnl = won - price - fee if filled else np.nan
            rows.append({
                "market_id": signal.market_id,
                "start_ts": float(signal.start_ts),
                "t": float(signal.t),
                "day": pd.Timestamp(signal.t, unit="s", tz="UTC").strftime("%Y-%m-%d"),
                "side": signal.side,
                "z5": float(signal.z5),
                "lag": float(lag),
                "won": won,
                "price": price,
                "fee": fee,
                "size": size,
                "filled": filled,
                "pnl": pnl,
                "reason": "filled" if filled else "depth" if quoted else (
                    "outside_frozen_price_band" if np.isfinite(price) else "book_unavailable"
                ),
            })
    return pd.DataFrame(rows, columns=RESULT_COLS)


def replay(dirs_by_coin: dict[str, list[Path]], notes: list[str] | None = None) -> pd.DataFrame:
    notes = [] if notes is None else notes

    def make(markets, spot, _sigma, book):
        return execute(signals(markets, spot), book)

    rows = lt._per_recording(
        dirs_by_coin,
        SIGNAL_FREEZE,
        "binancews",
        make,
        ["coin", "market_id", "lag"],
        notes,
    )
    if rows.empty:
        return pd.DataFrame(columns=RESULT_COLS + ["coin", "run"])
    return rows


def combine(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    keys = ["market_id", "lag"]

    def valid(frame):
        if frame is None or not len(frame):
            return pd.DataFrame(columns=RESULT_COLS + ["coin", "run"])
        frame = frame.copy()
        if "start_ts" not in frame:
            raise ValueError("momentum5s rows require start_ts")
        frame = frame[pd.to_numeric(frame["start_ts"], errors="coerce") >= FREEZE_TS]
        return frame.drop_duplicates(keys, keep="first")

    old, new = valid(old), valid(new)
    if len(old) and len(new):
        seen = pd.MultiIndex.from_frame(old[keys])
        incoming = pd.MultiIndex.from_frame(new[keys])
        new = new[~incoming.isin(seen)]
    rows = pd.concat([old, new], ignore_index=True)
    return rows.sort_values(["t", "market_id", "lag"], kind="stable").reset_index(drop=True)


def append(rows: pd.DataFrame, path: str | Path) -> pd.DataFrame:
    path = Path(path)
    old = pd.read_csv(path) if path.exists() and path.stat().st_size else pd.DataFrame()
    merged = combine(old, rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(path, index=False)
    return merged


def _filled(rows: pd.DataFrame) -> pd.DataFrame:
    if rows.empty or "filled" not in rows:
        return pd.DataFrame(columns=rows.columns)
    return rows[rows["filled"].astype(str).str.lower().isin(["true", "1"])].copy()


def exact_fair_price_pvalue(fills: pd.DataFrame) -> float:
    """Exact Poisson-binomial P(PnL >= observed PnL) under fair all-in prices."""
    if fills.empty:
        return 1.0
    probabilities = np.clip((fills["price"] + fills["fee"]).to_numpy(float), 0.0, 1.0)
    observed_wins = int(round(fills["won"].sum()))
    pmf = np.zeros(len(probabilities) + 1)
    pmf[0] = 1.0
    for i, probability in enumerate(probabilities):
        pmf[1:i + 2] = pmf[1:i + 2] * (1 - probability) + pmf[:i + 1] * probability
        pmf[0] *= 1 - probability
    return float(np.clip(pmf[observed_wins:].sum(), 0.0, 1.0))


def _stats(rows: pd.DataFrame) -> dict:
    fills = _filled(rows)
    ev = float(fills["pnl"].mean()) if len(fills) else float("nan")
    p = exact_fair_price_pvalue(fills) if len(fills) and ev > 0 else 1.0
    return {"signals": rows["market_id"].nunique(), "fills": len(fills), "days": fills["day"].nunique(), "ev": ev, "p": p}


def verdict(rows: pd.DataFrame) -> dict:
    if rows.empty:
        primary = rows
    else:
        primary = rows[
            np.isclose(pd.to_numeric(rows["lag"]), PRIMARY_LAG)
            & (pd.to_numeric(rows["start_ts"]) >= PROTOCOL_FREEZE_TS)
        ]
    fills = _filled(primary).sort_values(["t", "market_id"], kind="stable")
    judged = fills.head(STOP_N).copy()
    gate_input = judged.rename(columns={"pnl": "pnl_per_share"})
    gate = forward_gate(gate_input, alpha=ALPHA, min_fills=STOP_N, min_days=MIN_DAYS)
    exact = exact_fair_price_pvalue(judged) if len(judged) and judged["pnl"].mean() > 0 else 1.0
    passed = bool(gate.passed and exact < ALPHA)
    return {
        "passed": passed,
        "reason": "passed_paper_gate" if passed else gate.reason if not gate.passed else "exact_p_not_significant",
        "fills": len(judged),
        "days": judged["day"].nunique() if len(judged) else 0,
        "ev": float(judged["pnl"].mean()) if len(judged) else float("nan"),
        "lower": gate.lower_bound,
        "p": exact,
    }


def pin_verdict(rows: pd.DataFrame, path: str | Path) -> tuple[dict, dict | None]:
    """Persist the first stopping sample once; later runs can only read it."""
    path = Path(path)
    if path.exists():
        pinned = json.loads(path.read_text(encoding="utf-8"))
        if pinned.get("strategy_fingerprint") != strategy_fingerprint():
            raise ValueError("pinned momentum5s verdict belongs to a different strategy")
        return pinned["verdict"], pinned
    current = verdict(rows)
    if current["fills"] < STOP_N:
        return current, None
    primary = rows[
        np.isclose(pd.to_numeric(rows["lag"]), PRIMARY_LAG)
        & (pd.to_numeric(rows["start_ts"]) >= PROTOCOL_FREEZE_TS)
    ]
    judged = _filled(primary).sort_values(["t", "market_id"], kind="stable").head(STOP_N)
    payload = {
        "strategy_fingerprint": strategy_fingerprint(),
        "pinned_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "sample": judged[["market_id", "start_ts", "t"]].to_dict("records"),
        "verdict": current,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return current, payload


def report(rows: pd.DataFrame, notes: list[str] | None = None, frozen_verdict: dict | None = None) -> str:
    notes = [] if notes is None else notes
    v = frozen_verdict or verdict(rows)
    lines = [
        "# Frozen five-second momentum forward test",
        "",
        f"Signal frozen at `{SIGNAL_FREEZE}` from zoo100 rule #27; executable protocol frozen at `{PROTOCOL_FREEZE}`. Paper only; no orders are submitted.",
        "",
        "| Local signal-to-match lag | Signals | Five-share fills | Days | Net EV/share | Exact fair-price p |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for lag in LAGS:
        subset = rows[np.isclose(pd.to_numeric(rows.get("lag", pd.Series(dtype=float))), lag)] if len(rows) else rows
        s = _stats(subset)
        lines.append(f"| {lag * 1000:.0f} ms | {s['signals']:,} | {s['fills']:,} | {s['days']:,} | {100 * s['ev']:+.2f}¢ | {s['p']:.4g} |")
    lines += [
        "",
        f"Primary 400 ms fresh stopping sample (markets after the executable-protocol freeze): {v['fills']:,}/{STOP_N:,} fills across {v['days']} days; "
        f"EV {100 * v['ev']:+.2f}¢, one-sided 99% day lower bound {100 * v['lower']:+.2f}¢, exact p {v['p']:.4g}.",
        f"Status: `{v['reason']}`.",
    ]
    if notes:
        lines += ["", "## Recording notes", ""] + [f"- {note}" for note in notes]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", action="append", default=[], help="root containing unpacked recording artifacts")
    parser.add_argument("--latency-dir", action="append", default=[], help="a bundle-btc/latency directory")
    parser.add_argument("--seed", action="append", default=[], help="immutable earlier replay CSV to include in the report")
    parser.add_argument("--append", dest="append_path")
    parser.add_argument("--csv-out")
    parser.add_argument("--verdict-out")
    parser.add_argument("--manifest", default=str(Path(__file__).parent / "forward/momentum5s-freeze.json"))
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    load_manifest(args.manifest)
    dirs = lt._latency_dirs(args.root, ("btc",)) if args.root else {}
    if args.latency_dir:
        dirs.setdefault("btc", []).extend(Path(path) for path in args.latency_dir)
    notes: list[str] = []
    new_rows = replay(dirs, notes)
    if args.csv_out:
        Path(args.csv_out).parent.mkdir(parents=True, exist_ok=True)
        new_rows.to_csv(args.csv_out, index=False)
    rows = append(new_rows, args.append_path) if args.append_path else new_rows
    for seed in args.seed:
        path = Path(seed)
        if path.exists() and path.stat().st_size:
            rows = combine(pd.read_csv(path), rows)
    pinned = None
    if args.verdict_out:
        current, pinned = pin_verdict(rows, args.verdict_out)
    else:
        current = verdict(rows)
    text = report(rows, notes, pinned["verdict"] if pinned else current)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
