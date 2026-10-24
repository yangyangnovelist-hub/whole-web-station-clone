"""Family 32: causal informed-wallet following on public chain events and direct books.

The signal is the first qualifying CTF Exchange V2 aggressor-parent BUY from a
wallet selected only from fourteen settlement-safe UTC days ending two days
before the signal day.  Integer
block timestamps become observable only at the next whole second.  Execution
uses a fixed limit and the first direct-token 100 ms book at 400 or 500 ms.

This historical July/August run is an audit, not forward evidence.  It never
submits an order or accesses a trading wallet.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import ssl
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import certifi
import numpy as np
import pandas as pd

import cross
import momentum5s
import resolved
import reward_dust
import wallet_lead as wl


START_DAY = date(2026, 7, 2)
FINAL_DAY = date(2026, 8, 29)
DAYS = (FINAL_DAY - START_DAY).days + 1
HISTORY_SEED_DAY = START_DAY - timedelta(days=1)
DISCOVERY_END = date(2026, 7, 15)
CONFIRMATION_END = date(2026, 8, 16)
LOCKED_START = CONFIRMATION_END + timedelta(days=1)
HORIZONS = (400, 500)
TARGET_SHARES = Decimal(5)
MAX_SNAPSHOT_LAG_MS = 100
BOOK_CHANGE_WINDOW_MS = 2_000
MULTIPLE_TESTS = 100
ALPHA = 0.01
CONDITIONAL_TOKENS_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
PAYOUT_DENOMINATOR_SELECTOR = "0xdd34de67"
PAYOUT_NUMERATORS_SELECTOR = "0x0504c814"
POLYGON_RPC_URLS = (
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.drpc.org",
)
BOOK_DATASET = cross.DS
BOOK_START = "2026-07-02"
BOOK_END = "2026-08-29"
CHAIN_DATASET = "TimeSeventeen/Polymarket-v2"
CHAIN_REVISION = "101908446bbff1afa2513fa7f8f3dd1d168c4345"
CHAIN_API = "https://huggingface.co/api/datasets"
CHAIN_BASE = "https://huggingface.co/datasets"
DAILY_SCAN_COLUMNS = (
    "id",
    "order_hash",
    "maker",
    "taker",
    "timestamp",
    "maker_direction",
    "taker_direction",
    "token_asset_id",
    "token_amount",
    "usdc_amount",
    "price",
    "fee_usdc",
)
RESULT_COLUMNS = (
    "day",
    "period",
    "variant",
    "horizon_ms",
    "market_id",
    "market_start_s",
    "wallet",
    "wallet_score",
    "wallet_markets",
    "event_key",
    "signal_timestamp_s",
    "observable_ms",
    "target_ms",
    "side",
    "token_id",
    "limit_price",
    "snapshot_ms",
    "filled",
    "reason",
    "price",
    "displayed_size",
    "filled_shares",
    "fee_per_share",
    "won",
    "pnl",
)


@dataclass(frozen=True)
class ScanStats:
    raw_rows: int
    selected_rows: int
    row_groups: int
    row_groups_read: int


@dataclass(frozen=True)
class DailyChainFile:
    day: date
    name: str
    sha256: str
    size: int

    @property
    def url(self) -> str:
        return (
            f"{CHAIN_BASE}/{CHAIN_DATASET}/resolve/{CHAIN_REVISION}/{self.name}"
        )


@dataclass(frozen=True)
class BookFile:
    day: date
    name: str
    sha256: str
    size: int


def _day_start(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())


def period_for_day(day: date) -> str:
    if type(day) is not date:
        raise ValueError("day must be a UTC calendar date")
    if day < START_DAY or day > FINAL_DAY:
        raise ValueError("day is outside the frozen audit interval")
    if day <= DISCOVERY_END:
        return "discovery"
    if day <= CONFIRMATION_END:
        return "confirmation"
    return "locked"


def daily_source_files(
    metadata: Mapping[str, Any],
    *,
    start_day: date,
    days: int,
) -> list[DailyChainFile]:
    if metadata.get("sha") != CHAIN_REVISION:
        raise ValueError("daily chain metadata does not match the pinned revision")
    siblings = metadata.get("siblings")
    if not isinstance(siblings, list):
        raise ValueError("daily chain metadata has no sibling inventory")
    by_name: dict[str, Mapping[str, Any]] = {}
    for item in siblings:
        if not isinstance(item, Mapping):
            continue
        name = item.get("rfilename")
        if isinstance(name, str):
            if name in by_name:
                raise ValueError(f"duplicate daily chain file {name}")
            by_name[name] = item
    files: list[DailyChainFile] = []
    for offset in range(days):
        day = start_day + timedelta(days=offset)
        name = f"OrderFilled/{day:%Y_%m_%d}.parquet"
        raw = by_name.get(name)
        if raw is None:
            raise ValueError(f"missing daily chain file {name}")
        size = raw.get("size")
        lfs = raw.get("lfs")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size <= 0
            or not isinstance(lfs, Mapping)
            or lfs.get("size") != size
        ):
            raise ValueError(f"invalid LFS size metadata for {name}")
        sha256 = lfs.get("sha256")
        if (
            not isinstance(sha256, str)
            or len(sha256) != 64
            or any(char not in "0123456789abcdef" for char in sha256)
        ):
            raise ValueError(f"invalid LFS SHA-256 metadata for {name}")
        files.append(DailyChainFile(day, name, sha256, size))
    return files


def _timestamp_bounds(source: Any) -> list[tuple[int, int]]:
    timestamp_index = source.schema_arrow.get_field_index("timestamp")
    bounds: list[tuple[int, int]] = []
    for group_index in range(source.metadata.num_row_groups):
        statistics = source.metadata.row_group(group_index).column(
            timestamp_index
        ).statistics
        if statistics is None or not statistics.has_min_max:
            raise ValueError("daily timestamp row-group statistics are required")
        bounds.append((int(statistics.min), int(statistics.max)))
    return bounds


def validate_daily_partition(source: Any, expected_day: date) -> None:
    if source.metadata.num_rows <= 0:
        raise ValueError(f"{expected_day}: daily chain file is empty")
    start_s = _day_start(expected_day)
    end_s = start_s + 86_400
    for minimum, maximum in _timestamp_bounds(source):
        if minimum < start_s or maximum >= end_s or minimum > maximum:
            raise ValueError(f"{expected_day}: daily chain timestamp escaped partition")


def write_verified_stream(
    source: Any,
    destination: Path,
    *,
    expected_size: int,
    expected_sha256: str,
) -> int:
    destination.unlink(missing_ok=True)
    digest = hashlib.sha256()
    written = 0
    try:
        with destination.open("wb") as handle:
            while block := source.read(1 << 20):
                if not isinstance(block, bytes):
                    raise ValueError("chain response must yield bytes")
                handle.write(block)
                digest.update(block)
                written += len(block)
        if written != expected_size:
            raise ValueError(
                f"chain file has {written} bytes, expected {expected_size}"
            )
        if digest.hexdigest() != expected_sha256:
            raise ValueError("chain file sha256 mismatch")
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return written


def _download_chain(item: DailyChainFile, destination: Path) -> int:
    request = urllib.request.Request(item.url, headers={"User-Agent": "research"})
    error: Exception | None = None
    for _attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return write_verified_stream(
                    response,
                    destination,
                    expected_size=item.size,
                    expected_sha256=item.sha256,
                )
        except Exception as exc:
            error = exc
    raise RuntimeError(f"failed to download verified chain file {item.name}") from error


def scan_parquet(
    source: Any,
    tokens: Mapping[str, wl.TokenMeta],
    *,
    capture_start_s: int,
    capture_end_s: int,
) -> tuple[list[wl.ParentBuy], ScanStats]:
    """Read one pinned daily source and retain target-token parent BUYs."""
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    pf = source if hasattr(source, "read_row_group") else pq.ParquetFile(source)
    columns = frozenset(pf.schema_arrow.names)
    missing = frozenset(DAILY_SCAN_COLUMNS) - columns
    if missing:
        raise ValueError(f"daily V2 Parquet misses {','.join(sorted(missing))}")
    bounds = _timestamp_bounds(pf)
    groups = [
        index
        for index, (minimum, maximum) in enumerate(bounds)
        if maximum >= capture_start_s and minimum < capture_end_s
    ]
    token_values = pa.array(sorted(tokens), type=pa.string())
    events: list[wl.ParentBuy] = []
    for index in groups:
        table = pf.read_row_group(index, columns=list(DAILY_SCAN_COLUMNS))
        mask = pc.and_(
            pc.and_(
                pc.greater_equal(table["timestamp"], pa.scalar(capture_start_s)),
                pc.less(table["timestamp"], pa.scalar(capture_end_s)),
            ),
            pc.and_(
                pc.and_(
                    pc.equal(
                        pc.utf8_lower(table["taker"]),
                        pa.scalar(wl.CTF_EXCHANGE_V2_ADDRESS.lower()),
                    ),
                    pc.equal(table["maker_direction"], pa.scalar("BUY")),
                ),
                pc.and_(
                    pc.equal(table["taker_direction"], pa.scalar("SELL")),
                    pc.is_in(table["token_asset_id"], value_set=token_values),
                ),
            ),
        )
        for row in table.filter(mask).to_pylist():
            events.append(wl.normalise_daily_parent_buy(row, tokens))
    keys = [event.event_key for event in events]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate daily chain event id")
    return events, ScanStats(
        raw_rows=pf.metadata.num_rows,
        selected_rows=len(events),
        row_groups=pf.metadata.num_row_groups,
        row_groups_read=len(groups),
    )


def dedupe_parent_buys(events: Iterable[wl.ParentBuy]) -> list[wl.ParentBuy]:
    unique: dict[wl.EventKey, wl.ParentBuy] = {}
    for event in events:
        if not isinstance(event, wl.ParentBuy):
            raise ValueError("events must contain ParentBuy values")
        previous = unique.get(event.event_key)
        if previous is None:
            unique[event.event_key] = event
        elif previous != event:
            raise ValueError(f"conflicting duplicate event {event.event_key}")
    return sorted(
        unique.values(),
        key=lambda event: (
            event.timestamp_s,
            event.block_number,
            event.log_index,
            event.event_key,
        ),
    )


def certified_json_post(url: str, payload: Mapping[str, Any]) -> Any:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, separators=(",", ":")).encode(),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "polymarket-wallet-lead/1.0",
        },
    )
    context = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(request, timeout=30, context=context) as response:
        return json.loads(response.read())


def _eth_call(
    post: Callable[[str, Mapping[str, Any]], Any],
    rpc_url: str,
    data: str,
) -> int:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [
            {"to": CONDITIONAL_TOKENS_ADDRESS, "data": data},
            "latest",
        ],
    }
    response = post(rpc_url, payload)
    if not isinstance(response, Mapping) or response.get("error") is not None:
        raise ValueError("Polygon RPC returned an eth_call error")
    result = response.get("result")
    if not isinstance(result, str) or not result.startswith("0x"):
        raise ValueError("Polygon RPC returned an invalid eth_call result")
    return int(result, 16)


def resolve_condition_up(
    condition_id: str,
    up_index: int,
    *,
    post: Callable[[str, Mapping[str, Any]], Any] = certified_json_post,
    rpc_urls: tuple[str, ...] = POLYGON_RPC_URLS,
) -> bool | None:
    """Resolve a stale Gamma binary outcome from official CTF payouts."""
    condition_hex = condition_id.removeprefix("0x")
    if len(condition_hex) != 64 or any(
        character not in "0123456789abcdefABCDEF" for character in condition_hex
    ):
        raise ValueError("condition id must be bytes32 hex")
    if up_index not in (0, 1):
        raise ValueError("Up outcome index must be 0 or 1")
    condition_word = condition_hex.lower()
    last_error: Exception | None = None
    for rpc_url in rpc_urls:
        try:
            denominator = _eth_call(
                post,
                rpc_url,
                PAYOUT_DENOMINATOR_SELECTOR + condition_word,
            )
            if denominator == 0:
                return None
            payouts = tuple(
                _eth_call(
                    post,
                    rpc_url,
                    PAYOUT_NUMERATORS_SELECTOR
                    + condition_word
                    + f"{index:064x}",
                )
                for index in (0, 1)
            )
            if sum(payouts) != denominator:
                raise ValueError("binary CTF payouts do not sum to denominator")
            if payouts[up_index] == denominator:
                return True
            if payouts[up_index] == 0:
                return False
            return None
        except Exception as exc:
            last_error = exc
    raise RuntimeError("all Polygon payout RPC endpoints failed") from last_error


def fetch_token_maps(
    http: Any,
    *,
    outcome_resolver: Callable[[str, int], bool | None] | None = None,
) -> dict[date, dict[str, wl.TokenMeta]]:
    result: dict[date, dict[str, wl.TokenMeta]] = {}
    for offset in range(DAYS + 1):
        day = HISTORY_SEED_DAY + timedelta(days=offset)
        markets = reward_dust.fetch_btc5m_markets(
            http,
            _day_start(day),
            resolve_unsettled=outcome_resolver,
        )
        tokens: dict[str, wl.TokenMeta] = {}
        for market in markets:
            token_sides: tuple[tuple[str, wl.Side], ...] = (
                (market.token_up, "Up"),
                (market.token_down, "Down"),
            )
            for token_id, side in token_sides:
                if token_id in tokens:
                    raise ValueError(f"{day}: duplicate token {token_id}")
                tokens[token_id] = wl.TokenMeta(
                    day=day,
                    start_s=market.start_s,
                    market=market.condition_id,
                    token=token_id,
                    side=side,
                    up_won=market.up_won,
                )
        if len(tokens) != 576:
            raise ValueError(f"{day}: expected 576 direct tokens, got {len(tokens)}")
        result[day] = tokens
    return result


def parse_book_manifest(text: str) -> dict[date, BookFile]:
    import re

    files: dict[date, BookFile] = {}
    pattern = re.compile(
        r"^(market_parquet_(\d{4}-\d{2}-\d{2})\.tar\.gz)\s+"
        r"([0-9a-f]{64})\s+(\d+)$"
    )
    for raw in text.splitlines():
        match = pattern.match(raw.strip())
        if not match:
            continue
        day = date.fromisoformat(match.group(2))
        item = BookFile(day, match.group(1), match.group(3), int(match.group(4)))
        if day in files and files[day] != item:
            raise ValueError(f"conflicting book manifest row for {day}")
        files[day] = item
    audit_window = {START_DAY + timedelta(days=offset) for offset in range(DAYS)}
    retained = audit_window & set(files)
    locked_days = {
        LOCKED_START + timedelta(days=offset)
        for offset in range((FINAL_DAY - LOCKED_START).days + 1)
    }
    missing_locked = locked_days - retained
    if missing_locked:
        raise ValueError(
            f"book manifest misses locked days {min(missing_locked)} through "
            f"{max(missing_locked)}"
        )
    if not retained:
        raise ValueError("book manifest has no retained audit days")
    return {day: files[day] for day in sorted(retained)}


def _download_book(item: BookFile, destination: Path) -> None:
    destination.unlink(missing_ok=True)
    cross.fetch(item.name, destination)
    if destination.stat().st_size != item.size:
        raise ValueError(
            f"{item.name}: {destination.stat().st_size} bytes, expected {item.size}"
        )
    digest = hashlib.sha256()
    with destination.open("rb") as handle:
        while block := handle.read(1 << 20):
            digest.update(block)
    if digest.hexdigest() != item.sha256:
        raise ValueError(f"{item.name}: sha256 mismatch")


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or pd.isna(value):
        return None
    return Decimal(str(value))


def _snapshot_at(
    market_frame: pd.DataFrame,
    target_ms: int,
) -> list[wl.BookSnapshot]:
    """Build only the first causal snapshot; freshness is measured around target."""
    if market_frame.empty:
        return []
    ordered = market_frame.sort_values("timestamp_ms", kind="stable").reset_index(
        drop=True
    )
    timestamps = pd.to_numeric(ordered["timestamp_ms"], errors="coerce").to_numpy()
    if np.isnan(timestamps).any():
        raise ValueError("book contains a nonnumeric timestamp")
    sane, changes = cross.book_health(ordered)
    index = int(np.searchsorted(timestamps, target_ms, side="left"))
    if index >= len(ordered):
        return []
    row = ordered.iloc[index]
    timestamp_ms = int(timestamps[index])
    lifecycle = str(row.get("lifecycle_state", "") or "").lower()
    active = bool(sane[index]) and lifecycle in (
        "",
        "nan",
        "none",
        "active",
        "open",
        "trading",
    )
    halt_value = row.get("observed_halt_flag", False)
    halted = False if pd.isna(halt_value) else bool(halt_value)
    fresh = cross.changed_near(
        changes,
        target_ms,
        BOOK_CHANGE_WINDOW_MS,
        BOOK_CHANGE_WINDOW_MS,
    )
    return [
        wl.BookSnapshot(
            timestamp_ms=timestamp_ms,
            up_ask=_decimal_or_none(row.get("up_best_ask")),
            up_ask_size=_decimal_or_none(row.get("up_ask_size")),
            down_ask=_decimal_or_none(row.get("down_best_ask")),
            down_ask_size=_decimal_or_none(row.get("down_ask_size")),
            active=active,
            halted=halted,
            fresh=bool(fresh),
            market=str(row["market_id"]),
        )
    ]


def _result_row(
    day: date,
    signal: wl.ParentBuy,
    score: wl.WalletScore,
    result: wl.ReplayResult,
) -> dict[str, Any]:
    return {
        "day": day.isoformat(),
        "period": period_for_day(day),
        "variant": result.variant,
        "horizon_ms": result.horizon_ms,
        "market_id": signal.market,
        "market_start_s": signal.start_s,
        "wallet": signal.wallet,
        "wallet_score": float(score.score),
        "wallet_markets": score.n,
        "event_key": "|".join(map(str, signal.event_key)),
        "signal_timestamp_s": signal.timestamp_s,
        "observable_ms": wl.observable_ms(signal.timestamp_s),
        "target_ms": result.target_ms,
        "side": result.side,
        "token_id": result.token,
        "limit_price": float(result.limit_price)
        if result.limit_price is not None
        else None,
        "snapshot_ms": result.snapshot_ms,
        "filled": result.filled,
        "reason": result.reason,
        "price": float(result.fill_price) if result.fill_price is not None else None,
        "displayed_size": float(result.ask_size)
        if result.ask_size is not None
        else None,
        "filled_shares": float(result.shares),
        "fee_per_share": float(result.fee / result.shares) if result.shares else 0.0,
        "won": float(result.settlement) if result.filled else 0.0,
        "pnl": float(result.pnl),
    }


def replay_day(
    day: date,
    events: list[wl.ParentBuy],
    history: list[wl.WalletObservation],
    features: pd.DataFrame,
) -> tuple[list[dict[str, Any]], tuple[wl.WalletScore, ...]]:
    if day <= DISCOVERY_END:
        return [], ()
    selected = wl.select_wallets(history, day)
    score_by_wallet = {score.wallet: score for score in selected}
    if not selected:
        return [], selected
    if features.empty:
        books: dict[str, pd.DataFrame] = {}
    else:
        features = features.copy()
        features["market_id"] = features["market_id"].astype(str)
        books = {
            market: frame.reset_index(drop=True)
            for market, frame in features.groupby("market_id", sort=False)
        }
    by_market: dict[tuple[int, str], list[wl.ParentBuy]] = {}
    for event in events:
        by_market.setdefault((event.start_s, event.market), []).append(event)
    rows: list[dict[str, Any]] = []
    for (start_s, market), market_events in sorted(by_market.items()):
        signal = wl.first_market_signal(market_events, selected, start_s)
        if signal is None:
            continue
        score = score_by_wallet[signal.wallet]
        frame = books.get(market, pd.DataFrame())
        for variant in ("base", "delay_5s", "opposite"):
            for horizon_ms in HORIZONS:
                target_ms = wl.replay_target_ms(signal, horizon_ms, variant)
                snapshots = _snapshot_at(frame, target_ms)
                result = wl.replay(
                    signal,
                    score,
                    snapshots,
                    horizon_ms,
                    variant=variant,
                )
                rows.append(_result_row(day, signal, score, result))
    return rows, selected


def _daily_lower(group: pd.DataFrame) -> float:
    daily = (
        group.assign(attempted_shares=5.0)
        .groupby("day", sort=True)[["pnl", "attempted_shares"]]
        .sum()
    )
    if len(daily) < 2:
        return float("nan")
    total_shares = float(daily["attempted_shares"].sum())
    estimate = float(daily["pnl"].sum()) / total_shares
    influence = daily["pnl"] - estimate * daily["attempted_shares"]
    count = len(daily)
    se = float(np.sqrt(count / (count - 1) * np.square(influence).sum()) / total_shares)
    if se == 0:
        return estimate
    from scipy.stats import t as student_t

    return estimate - float(student_t.ppf(1.0 - ALPHA, count - 1)) * se


def summarize(rows: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "period",
        "variant",
        "horizon_ms",
        "signals",
        "fills",
        "days",
        "fill_rate",
        "net_pnl",
        "net_ev_per_attempted_share",
        "net_ev_per_filled_share",
        "day_cluster_lower_99",
        "exact_p",
        "corrected_p",
    ]
    if rows.empty:
        return pd.DataFrame(columns=columns)
    summaries: list[dict[str, Any]] = []
    for (period, variant, horizon), group in rows.groupby(
        ["period", "variant", "horizon_ms"], sort=True
    ):
        fills = group[group["filled"].astype(bool)].copy()
        signals = len(group)
        filled_shares = float(fills["filled_shares"].sum()) if len(fills) else 0.0
        pnl = float(group["pnl"].sum())
        fair_rows = fills.rename(columns={"fee_per_share": "fee"})
        exact = (
            momentum5s.exact_fair_price_pvalue(fair_rows)
            if len(fair_rows) and pnl > 0
            else 1.0
        )
        summaries.append(
            {
                "period": period,
                "variant": variant,
                "horizon_ms": int(horizon),
                "signals": signals,
                "fills": len(fills),
                "days": int(group["day"].nunique()),
                "fill_rate": len(fills) / signals if signals else 0.0,
                "net_pnl": pnl,
                "net_ev_per_attempted_share": pnl / (signals * 5.0),
                "net_ev_per_filled_share": pnl / filled_shares
                if filled_shares
                else np.nan,
                "day_cluster_lower_99": _daily_lower(group),
                "exact_p": exact,
                "corrected_p": min(1.0, exact * MULTIPLE_TESTS),
            }
        )
    return pd.DataFrame(summaries, columns=columns)


def promotion_verdict(
    summary: pd.DataFrame,
    *,
    chain_finalized: bool,
) -> tuple[bool, str]:
    if not chain_finalized:
        return False, "chain finality is not independently proven"
    required = summary[
        summary["period"].isin(["confirmation", "locked"])
        & summary["variant"].eq("base")
        & summary["horizon_ms"].isin(HORIZONS)
    ]
    if len(required) != 4:
        return False, "required confirmation/locked cells are missing"
    if (required["fills"] <= 0).any():
        return False, "at least one required cell has no executable fill"
    checks = (
        required["net_ev_per_attempted_share"].gt(0)
        & required["day_cluster_lower_99"].gt(0)
        & required["corrected_p"].lt(ALPHA)
    )
    if not bool(checks.all()):
        return False, "the preregistered historical promotion gate did not pass"
    return True, "eligible only for a fresh, untouched forward paper test"


def _fmt(value: Any, scale: float = 1.0, suffix: str = "") -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(number):
        return "—"
    return f"{number * scale:.3f}{suffix}"


def render_report(
    rows: pd.DataFrame,
    summary: pd.DataFrame,
    *,
    chain_revision: str,
    chain_files: int,
    chain_fetched_bytes: int,
    chain_finalized: bool,
    chain_manifest_sha256: str,
    book_manifest_sha256: str,
    book_files: int,
    onchain_outcome_fallbacks: int = 0,
) -> str:
    passed, reason = promotion_verdict(summary, chain_finalized=chain_finalized)
    lines = [
        "# Family 32：知情钱包领先回放",
        "",
        f"结论：{'历史门槛通过，可进入全新前向纸测' if passed else '历史审计未通过'}。{reason}。",
        "",
        "这是一轮已看过日期上的机制审计，不是前向证据，也不授权实盘。",
        "",
        "## 冻结规则",
        "",
        "- 每天只用截至两天前的 14 个结算安全完整 UTC 日；每钱包每市场保留任意正数量的首笔 BUY；至少 20 个市场。",
        "- 按加 20 个零收益伪观察后的净结算收益评分，只取分数高于 2¢ 的前 5 个钱包。",
        "- 链上整数秒事件只在下一整秒可见；每市场只取首个可见信号。",
        "- 固定限价保留预测净值至少 1¢；仅直接 token 卖盘、至少 5 份、真实 taker 费。",
        "- 400/500 ms；盘口需在目标前后 2 秒内都真实变化，100 ms 后仍无快照即拒绝。",
        "- +5 秒延迟和反向 token 是冻结安慰剂；所有 p 值按 100 次检验校正。",
        "",
        "## 数值",
        "",
        "| 分段 | 变体 | 延迟 | 信号 | 成交 | 成交率 | 净 EV/尝试份 | 净 EV/成交份 | PnL | 99% 日聚类下界 | 校正 p |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summary.itertuples(index=False):
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row.period),
                    str(row.variant),
                    f"{int(row.horizon_ms)} ms",
                    str(int(row.signals)),
                    str(int(row.fills)),
                    _fmt(row.fill_rate, 100, "%"),
                    _fmt(row.net_ev_per_attempted_share, 100, "¢"),
                    _fmt(row.net_ev_per_filled_share, 100, "¢"),
                    _fmt(row.net_pnl, 1, "$"),
                    _fmt(row.day_cluster_lower_99, 100, "¢"),
                    _fmt(row.corrected_p),
                ]
            )
            + " |"
        )
    lines += [
        "",
        "## 来源与审计边界",
        "",
        f"- 链事件：{CHAIN_DATASET} 原始逐日 OrderFilled，固定 revision {chain_revision}，{chain_files} 个 Parquet；LFS 清单 SHA-256 {chain_manifest_sha256}；远程实际读取 {chain_fetched_bytes / 1e6:.1f} MB。",
        f"- Gamma 未终结但 CTF 已终结的市场：{onchain_outcome_fallbacks} 个；结果由官方 Conditional Tokens payout numerator/denominator 恢复。",
        "- 吃单身份只认 CTF Exchange V2 parent 行：taker 为交易所合约、maker_direction=BUY；逐 maker 成交行不作钱包信号。",
        f"- 链源最终性账本：{'通过' if chain_finalized else '未证明；结果因此只能作历史诊断'}。",
        f"- 盘口：{BOOK_DATASET}，冻结区间内公开保留 {book_files} 个逐日档；未列入固定 MANIFEST 的日期不插值、不回填；锁定段 {LOCKED_START} 至 {FINAL_DAY} 完整。MANIFEST SHA-256 {book_manifest_sha256}。",
        f"- 日期：{BOOK_START} 至 {BOOK_END}；共 {len(rows):,} 条变体/延迟尝试。",
        "- 这批日期已用于机制开发，只能否决或决定是否进入新的前向样本，不能直接宣称可赚钱。",
        "",
    ]
    return "\n".join(lines)


def _write_rows(path: Path, rows: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
        rows.to_csv(handle, index=False)


def run_audit(workdir: Path, out: Path, csv_out: Path) -> pd.DataFrame:
    workdir.mkdir(parents=True, exist_ok=True)
    http = resolved.Http(
        workdir / "gamma",
        rate=2.0,
        opener=reward_dust.certified_json_get,
        tries=7,
    )
    payout_outcomes: dict[tuple[str, int], bool] = {}

    def resolve_outcome(condition_id: str, up_index: int) -> bool | None:
        key = (condition_id, up_index)
        if key not in payout_outcomes:
            outcome = resolve_condition_up(condition_id, up_index)
            if outcome is None:
                return None
            payout_outcomes[key] = outcome
        return payout_outcomes[key]

    token_maps = fetch_token_maps(http, outcome_resolver=resolve_outcome)

    metadata = reward_dust.certified_json_get(
        f"{CHAIN_API}/{CHAIN_DATASET}/revision/{CHAIN_REVISION}?blobs=true"
    )
    chain_files = daily_source_files(
        metadata,
        start_day=HISTORY_SEED_DAY,
        days=DAYS + 1,
    )
    chain_manifest = "\n".join(
        f"{item.day.isoformat()} {item.name} {item.size} {item.sha256}"
        for item in chain_files
    ).encode()
    chain_manifest_sha256 = hashlib.sha256(chain_manifest).hexdigest()
    chain_by_day = {item.day: item for item in chain_files}

    raw_book_manifest = cross.fetch("MANIFEST.txt")
    book_manifest_sha256 = hashlib.sha256(raw_book_manifest).hexdigest()
    book_files = parse_book_manifest(raw_book_manifest.decode("utf-8"))
    rows: list[dict[str, Any]] = []
    chain_fetched_bytes = 0
    local_chain = workdir / "chain-day.parquet"

    def read_chain_day(day: date) -> list[wl.ParentBuy]:
        nonlocal chain_fetched_bytes
        import pyarrow.parquet as pq

        item = chain_by_day[day]
        try:
            chain_fetched_bytes += _download_chain(item, local_chain)
            parquet = pq.ParquetFile(local_chain)
            validate_daily_partition(parquet, day)
            start_s = _day_start(day)
            events, _stats = scan_parquet(
                parquet,
                token_maps[day],
                capture_start_s=start_s,
                capture_end_s=start_s + 86_400,
            )
            return dedupe_parent_buys(events)
        finally:
            local_chain.unlink(missing_ok=True)

    seed_events = read_chain_day(HISTORY_SEED_DAY)
    history = [
        wl.WalletObservation.from_parent_buy(event)
        for event in seed_events
    ]
    local_book = workdir / "book-day.tar.gz"
    for offset in range(DAYS):
        day = START_DAY + timedelta(days=offset)
        events = read_chain_day(day)
        if day <= DISCOVERY_END or day not in book_files:
            day_rows: list[dict[str, Any]] = []
            selected: tuple[wl.WalletScore, ...] = ()
        else:
            item = book_files[day]
            try:
                _download_book(item, local_book)
                features, _markets, _resolution = cross.read_day(local_book, depth=0)
            finally:
                local_book.unlink(missing_ok=True)
            day_rows, selected = replay_day(day, events, history, features)
            del features
        rows.extend(day_rows)
        history.extend(wl.WalletObservation.from_parent_buy(event) for event in events)
        cutoff = day - timedelta(days=14)
        history = [observation for observation in history if observation.day >= cutoff]
        print(
            f"{day}: {len(events):,} parent BUYs, {len(selected)} selected wallets, "
            f"{len(day_rows):,} attempts",
            flush=True,
        )
        del events

    frame = pd.DataFrame(rows, columns=RESULT_COLUMNS)
    summary = summarize(frame)
    _write_rows(csv_out, frame)
    report = render_report(
        frame,
        summary,
        chain_revision=CHAIN_REVISION,
        chain_files=len(chain_files),
        chain_fetched_bytes=chain_fetched_bytes,
        chain_finalized=False,
        chain_manifest_sha256=chain_manifest_sha256,
        book_manifest_sha256=book_manifest_sha256,
        book_files=len(book_files),
        onchain_outcome_fallbacks=len(payout_outcomes),
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    return frame


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default=START_DAY.isoformat())
    parser.add_argument("--days", type=int, default=DAYS)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("real/wallet-lead.md"))
    parser.add_argument(
        "--csv-out",
        type=Path,
        default=Path("real/wallet-lead.csv.gz"),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.start != START_DAY.isoformat() or args.days != DAYS:
        raise ValueError(
            "Family 32 audit interval is frozen at 2026-07-02 through 2026-08-29"
        )
    run_audit(args.workdir, args.out, args.csv_out)
    print(f"wrote {args.out} and {args.csv_out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
