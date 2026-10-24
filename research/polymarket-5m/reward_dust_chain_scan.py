"""Stream the public V2 OrderFilled archive into Family 31's chain bound.

The scanner reuses the repository's seekable HTTP Parquet reader, reads one
UTC market day at a time, and discards raw rows after that day's parent-order
upper bound is computed.  It never accesses a wallet or submits an order.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote

import cross
import reward_dust
import reward_dust_chain as chain
import resolved


DATASET = "wzsg/polymarket-orderfilled-v2"
PINNED_REVISION = "f8782fc0c18a4e32b7bdc348ada114e5007a4367"
HF_API = "https://huggingface.co/api/datasets"
HF_BASE = "https://huggingface.co/datasets"
CHAIN_ID = 137
CTF_EXCHANGE_V2_LABEL = "CTF_EXCHANGE_V2"
CTF_EXCHANGE_V2_ADDRESS = "0xE111180000d2663C0091e4f400237545B87B996B"
ABI_VERSION = "ctf-exchange-v2"
RAW_SCALE = 1_000_000
FRAGMENT_INCREMENT = Decimal("0.000001")
ZERO_FEE_THRESHOLD = Decimal("0.00001")
SCAN_COLUMNS = tuple(sorted(chain.REQUIRED_RAW_COLUMNS - {"datetime", "builder", "metadata"}))


@dataclass(frozen=True)
class TokenMeta:
    market_day: date
    market_start_s: int
    market_id: str
    token_id: str


@dataclass(frozen=True)
class SourceFile:
    path: str
    url: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ScanStats:
    raw_rows: int
    selected_rows: int
    row_groups: int
    row_groups_read: int


@dataclass(frozen=True)
class SourceInventory:
    complete: bool
    finalized: bool
    safe_block_hash: str
    confirmation_depth: int
    reorg_conflicts: int
    reasons: tuple[str, ...]


def _decimal_integer(value: Any, field: str) -> int:
    text = str(value)
    if re.fullmatch(r"[0-9]+", text) is None:
        raise ValueError(f"{field} must be a lossless unsigned integer string")
    return int(text)


def _human_units(value: int) -> Decimal:
    whole, fraction = divmod(value, RAW_SCALE)
    return Decimal(f"{whole}.{fraction:06d}")


def normalise_v2_row(
    row: dict[str, Any],
    tokens: dict[str, TokenMeta],
) -> chain.ChainFill:
    """Normalise one raw V2 ABI event using exact six-decimal integer units."""
    token_id = str(row["token_id"])
    token = tokens.get(token_id)
    if token is None:
        raise ValueError(f"unknown BTC5m token {token_id}")
    if str(row["contract"]) != CTF_EXCHANGE_V2_LABEL:
        raise ValueError(f"unsupported contract {row['contract']!r}")
    side = int(row["side"])
    making = _decimal_integer(row["maker_amount_filled"], "maker_amount_filled")
    taking = _decimal_integer(row["taker_amount_filled"], "taker_amount_filled")
    fee = _decimal_integer(row["fee"], "fee")
    if side == 0:
        collateral_raw, shares_raw = making, taking
    elif side == 1:
        shares_raw, collateral_raw = making, taking
    else:
        raise ValueError(f"unsupported side {side}")
    maker = str(row["maker"])
    taker = str(row["taker"])
    return chain.ChainFill(
        market_day=token.market_day,
        chain_id=CHAIN_ID,
        exchange_contract=CTF_EXCHANGE_V2_ADDRESS,
        abi_version=ABI_VERSION,
        transaction_hash=str(row["transaction_hash"]).lower(),
        log_index=int(row["log_index"]),
        block_number=int(row["block_number"]),
        timestamp_s=int(row["timestamp"]),
        market_start_s=token.market_start_s,
        market_id=token.market_id,
        token_asset_id=token_id,
        order_hash=str(row["order_hash"]).lower(),
        maker=maker.lower(),
        taker=taker.lower(),
        role=(
            chain.AGGRESSOR_PARENT
            if taker.lower() == CTF_EXCHANGE_V2_ADDRESS.lower()
            else chain.MAKER_CHILD
        ),
        gross_shares=_human_units(shares_raw),
        gross_collateral=_human_units(collateral_raw),
        fee_usdc=_human_units(fee),
    )


def source_files(
    metadata: dict[str, Any],
    months: set[str],
    *,
    expected_revision: str | None = None,
) -> list[SourceFile]:
    revision = str(metadata.get("sha") or "")
    if not revision:
        raise ValueError("dataset metadata has no immutable revision")
    if expected_revision is not None and revision != expected_revision:
        raise ValueError(f"dataset revision {revision!r} differs from pinned {expected_revision!r}")
    selected: list[SourceFile] = []
    for item in metadata.get("siblings") or []:
        path = str(item.get("rfilename") or "")
        if not path.endswith(".parquet"):
            continue
        month = next((value for value in months if path.startswith(f"event_month={value}/")), None)
        if month is None:
            continue
        lfs = item.get("lfs") or {}
        sha256 = str(lfs.get("sha256") or "")
        size = int(lfs.get("size") or item.get("size") or 0)
        if len(sha256) != 64 or size <= 0:
            raise ValueError(f"missing LFS identity for {path}")
        encoded = quote(path, safe="/=")
        selected.append(
            SourceFile(
                path=path,
                url=f"{HF_BASE}/{DATASET}/resolve/{revision}/{encoded}",
                size=size,
                sha256=sha256,
            )
        )
    if not selected:
        raise ValueError(f"no source Parquets for {sorted(months)}")
    return sorted(selected, key=lambda item: item.path)


def _iso_timestamp(value: Any, field: str) -> int:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} is not an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} has no timezone")
    return int(parsed.timestamp())


def validate_source_inventory(
    files: list[SourceFile],
    dataset_manifest: dict[str, Any],
    collector_state: dict[str, Any],
    *,
    start_day: date,
    days: int,
) -> SourceInventory:
    """Prove the pinned shards cover the full market window plus final grace."""
    reasons: list[str] = []
    finalization_reasons: list[str] = []
    months = _months_for_window(start_day, days)
    partitions = dataset_manifest.get("partitions") or {}
    if dataset_manifest.get("schema_version") != "orderfilled_v2":
        reasons.append("dataset manifest schema is not orderfilled_v2")
    if dataset_manifest.get("partition_timezone") != "UTC":
        reasons.append("dataset partitions are not UTC")
    if CTF_EXCHANGE_V2_LABEL not in (dataset_manifest.get("selection") or []):
        reasons.append("standard CTF Exchange V2 is absent from the dataset selection")
    validation = dataset_manifest.get("validation") or {}
    if int(validation.get("null_rows", -1)) != 0:
        reasons.append("dataset validation reports null rows")

    for month in sorted(months):
        partition = partitions.get(month)
        if not isinstance(partition, dict):
            reasons.append(f"{month}: partition manifest is missing")
            continue
        month_files = [item for item in files if item.path.startswith(f"event_month={month}/")]
        if len(month_files) != int(partition.get("file_count", -1)):
            reasons.append(f"{month}: pinned Parquet count differs from the partition manifest")
        if sum(item.size for item in month_files) != int(partition.get("size_bytes", -1)):
            reasons.append(f"{month}: pinned Parquet bytes differ from the partition manifest")

    ordered_partitions = [partitions.get(month) for month in sorted(months)]
    if all(isinstance(partition, dict) for partition in ordered_partitions):
        for left, right in zip(ordered_partitions, ordered_partitions[1:]):
            left_max = int(left.get("max_block", -1))
            right_min = int(right.get("min_block", -1))
            if left_max < 0 or right_min != left_max + 1:
                reasons.append("partition block boundaries are not contiguous")

    capture_start = _day_start(start_day)
    capture_end = _day_start(start_day + timedelta(days=days)) + chain.CAPTURE_GRACE_SECONDS
    selected_partitions = ordered_partitions
    if all(isinstance(partition, dict) for partition in selected_partitions):
        earliest = min(
            _iso_timestamp(partition["min_datetime"], f"{month}.min_datetime")
            for month, partition in zip(sorted(months), selected_partitions, strict=True)
        )
        latest = max(
            _iso_timestamp(partition["max_datetime"], f"{month}.max_datetime")
            for month, partition in zip(sorted(months), selected_partitions, strict=True)
        )
        if earliest > capture_start:
            reasons.append("partition coverage starts after the frozen market window")
        if latest < capture_end:
            reasons.append("partition coverage ends before the final market grace")

    if collector_state.get("repo_id") != DATASET:
        reasons.append("collector state belongs to another dataset")
    if int(collector_state.get("chain_id", -1)) != CHAIN_ID:
        reasons.append("collector state chain_id is not Polygon 137")
    if collector_state.get("dataset_schema") != "orderfilled_v2":
        reasons.append("collector state schema is not orderfilled_v2")
    confirmation_depth = int(collector_state.get("confirmation_depth", 0))
    if confirmation_depth < 512:
        finalization_reasons.append("collector confirmation depth is below 512 blocks")
    safe_block_hash = str(collector_state.get("last_scanned_block_hash") or "")
    if re.fullmatch(r"0x[0-9a-fA-F]{64}", safe_block_hash) is None:
        finalization_reasons.append("collector safe block hash is missing or invalid")
    raw_reorg_conflicts = collector_state.get("reorg_conflicts")
    if isinstance(raw_reorg_conflicts, bool) or not isinstance(raw_reorg_conflicts, int):
        reorg_conflicts = -1
        finalization_reasons.append("collector has no explicit reorg-conflict audit")
    else:
        reorg_conflicts = raw_reorg_conflicts
        if reorg_conflicts < 0:
            finalization_reasons.append("collector reorg-conflict count is invalid")
        elif reorg_conflicts > 0:
            finalization_reasons.append("collector reports reorg conflicts")
    last_block = int(collector_state.get("last_scanned_block", -1))
    if last_block != int(collector_state.get("target_block", -2)):
        finalization_reasons.append("collector did not reach its declared safe target block")
    if int(collector_state.get("next_block", -1)) != last_block + 1:
        finalization_reasons.append("collector next-block boundary is not contiguous")
    if int(collector_state.get("last_event_timestamp", -1)) < capture_end:
        reasons.append("collector state ends before the final market grace")
    if int(validation.get("max_block", -1)) != int(collector_state.get("last_event_block", -2)):
        reasons.append("manifest and collector disagree on the final event block")

    ranges = collector_state.get("scanned_ranges")
    if not isinstance(ranges, list) or not ranges:
        finalization_reasons.append("collector has no immutable contiguous scanned-range ledger")
    else:
        normalised_ranges: list[tuple[int, int]] = []
        for item in ranges:
            if not isinstance(item, dict):
                finalization_reasons.append("collector scanned-range ledger is malformed")
                break
            lo, hi = int(item.get("from_block", -1)), int(item.get("to_block", -1))
            if lo < 0 or hi < lo:
                finalization_reasons.append("collector scanned-range ledger is malformed")
                break
            normalised_ranges.append((lo, hi))
        if normalised_ranges:
            normalised_ranges.sort()
            if any(right[0] != left[1] + 1 for left, right in zip(normalised_ranges, normalised_ranges[1:])):
                finalization_reasons.append("collector scanned-range ledger has an internal block gap")
            if normalised_ranges[-1][1] != last_block:
                finalization_reasons.append(
                    "collector scanned-range ledger does not end at the declared safe target block"
                )
            if all(isinstance(partition, dict) for partition in ordered_partitions):
                manifest_min = min(int(partition.get("min_block", -1)) for partition in ordered_partitions)
                manifest_max = max(int(partition.get("max_block", -1)) for partition in ordered_partitions)
                if normalised_ranges[0][0] > manifest_min or normalised_ranges[-1][1] < manifest_max:
                    finalization_reasons.append("collector scanned-range ledger does not cover selected partitions")
            else:
                finalization_reasons.append("collector ranges cannot be tied to missing partition manifests")

    complete = not reasons
    deduplicated = tuple(dict.fromkeys((*reasons, *finalization_reasons)))
    return SourceInventory(
        complete=complete,
        finalized=complete and not finalization_reasons,
        safe_block_hash=safe_block_hash,
        confirmation_depth=confirmation_depth,
        reorg_conflicts=reorg_conflicts,
        reasons=deduplicated,
    )


def _parquet_file(source: Any) -> Any:
    if hasattr(source, "read_row_group") and hasattr(source, "metadata"):
        return source
    import pyarrow.parquet as pq

    return pq.ParquetFile(source)


def timestamp_row_group_bounds(source: Any) -> tuple[tuple[int, int], ...]:
    """Validate and return every row group's timestamp bounds without reading row data."""
    pf = _parquet_file(source)
    timestamp_index = pf.schema_arrow.get_field_index("timestamp")
    bounds: list[tuple[int, int]] = []
    for group_index in range(pf.metadata.num_row_groups):
        stats = pf.metadata.row_group(group_index).column(timestamp_index).statistics
        if stats is None or not stats.has_min_max:
            raise ValueError("timestamp row-group statistics are required for bounded remote scanning")
        bounds.append((int(stats.min), int(stats.max)))
    return tuple(bounds)


def preflight_timestamp_statistics(sources: list[Any]) -> None:
    """Fail before the first data read if any shard lacks timestamp pruning metadata."""
    for source in sources:
        timestamp_row_group_bounds(source)


def scan_parquet(
    source: Any,
    tokens: dict[str, TokenMeta],
    *,
    capture_start_s: int,
    capture_end_s: int,
) -> tuple[list[chain.ChainFill], ScanStats]:
    """Read only intersecting row groups and retain one day's target tokens."""
    import pyarrow as pa
    import pyarrow.compute as pc

    pf = _parquet_file(source)
    columns = frozenset(pf.schema_arrow.names)
    missing = chain.REQUIRED_RAW_COLUMNS - columns
    if missing:
        raise ValueError(f"raw V2 Parquet misses {','.join(sorted(missing))}")
    token_values = pa.array(sorted(tokens), type=pa.string())
    bounds = timestamp_row_group_bounds(pf)
    intersecting_groups = [
        group_index
        for group_index, (minimum, maximum) in enumerate(bounds)
        if maximum >= capture_start_s and minimum < capture_end_s
    ]

    # Validate every row group's pruning metadata before the first potentially remote read.  A
    # missing statistic late in a multi-gigabyte shard must fail fast rather than after earlier
    # groups have already been downloaded.
    events: list[chain.ChainFill] = []
    read_groups = 0
    for group_index in intersecting_groups:
        table = pf.read_row_group(group_index, columns=list(SCAN_COLUMNS))
        mask = pc.and_(
            pc.and_(
                pc.greater_equal(table["timestamp"], pa.scalar(capture_start_s)),
                pc.less(table["timestamp"], pa.scalar(capture_end_s)),
            ),
            pc.and_(
                pc.equal(table["contract"], pa.scalar(CTF_EXCHANGE_V2_LABEL)),
                pc.is_in(table["token_id"], value_set=token_values),
            ),
        )
        selected = table.filter(mask)
        read_groups += 1
        events.extend(normalise_v2_row(row, tokens) for row in selected.to_pylist())
    return events, ScanStats(
        raw_rows=pf.metadata.num_rows,
        selected_rows=len(events),
        row_groups=pf.metadata.num_row_groups,
        row_groups_read=read_groups,
    )


def _day_start(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())


def fetch_token_maps(http: Any, start_day: date, days: int) -> dict[date, dict[str, TokenMeta]]:
    maps: dict[date, dict[str, TokenMeta]] = {}
    for offset in range(days):
        day = start_day + timedelta(days=offset)
        markets = reward_dust.fetch_btc5m_markets(http, _day_start(day))
        tokens: dict[str, TokenMeta] = {}
        for market in markets:
            for token_id in (market.token_up, market.token_down):
                if token_id in tokens:
                    raise ValueError(f"duplicate token {token_id} on {day}")
                tokens[token_id] = TokenMeta(
                    market_day=day,
                    market_start_s=market.start_s,
                    market_id=market.condition_id,
                    token_id=token_id,
                )
        if len(tokens) != 576:
            raise ValueError(f"{day}: expected 576 unique token ids, got {len(tokens)}")
        maps[day] = tokens
    return maps


def _unique_summary(events: list[chain.ChainFill]) -> tuple[int, Decimal]:
    unique: dict[tuple[int, str, str, int], chain.ChainFill] = {}
    for event in events:
        if event.event_key is None:
            continue
        previous = unique.get(event.event_key)
        if previous is None:
            unique[event.event_key] = event
        elif previous != event:
            raise ValueError(f"conflicting duplicate chain event {event.event_key}")
    parent_shares = sum(
        (
            event.gross_shares
            for event in unique.values()
            if event.role == chain.AGGRESSOR_PARENT
        ),
        Decimal(0),
    )
    return len(unique), parent_shares


def build_day_manifest(
    day: date,
    tokens: dict[str, TokenMeta],
    *,
    source_hashes: tuple[str, ...],
    source_complete: bool,
    finalized_ranges_complete: bool,
    reorg_conflicts: int,
    raw_rows: int,
    selected_rows: int,
    parent_shares: Decimal,
) -> chain.DayManifest:
    start = _day_start(day)
    market_starts = tuple(sorted({token.market_start_s for token in tokens.values()}))
    return chain.DayManifest(
        market_day=day,
        capture_start_s=start,
        capture_end_s=start + 90_000,
        source_hashes=source_hashes,
        source_complete=source_complete,
        truncated=False,
        finalized_ranges_complete=finalized_ranges_complete,
        reorg_conflicts=reorg_conflicts,
        identity_semantics_proven=True,
        chain_id=CHAIN_ID,
        allowed_exchange_contracts=frozenset({CTF_EXCHANGE_V2_ADDRESS}),
        supported_abi_versions=frozenset({ABI_VERSION}),
        amount_semantics_proven=True,
        fee_semantics_proven=True,
        fragment_increment=FRAGMENT_INCREMENT,
        fragment_precision_proven=True,
        zero_fee_threshold_usdc=ZERO_FEE_THRESHOLD,
        zero_fee_threshold_proven=True,
        raw_columns=chain.REQUIRED_RAW_COLUMNS,
        raw_rows=raw_rows,
        selected_rows=selected_rows,
        market_starts=market_starts,
        token_ids=tuple(sorted(tokens)),
        parity_checked=False,
        parity_metric=chain.PARITY_METRIC,
        parity_api_token_amount=parent_shares,
        parity_chain_token_amount=parent_shares,
        parity_abs_tolerance=Decimal("0.000001"),
    )


def render_report(
    result: chain.ChainCapacityResult,
    *,
    source_commit: str,
    source_files: int,
    source_bytes: int,
    fetched_bytes: int,
    start_day: str,
    days: int,
) -> str:
    fraction = result.inflated_weighted_volume / result.bronze_weighted_volume
    reasons = list(result.incomplete_reasons)
    lines = [
        "# Family 31: chain-event dust-capacity upper bound",
        "",
        f"**Window:** {start_day}, {days} target UTC market days",
        f"**Verdict:** **{result.verdict}**",
        f"**Evidence:** {result.evidence_class}",
        "",
        "## Result",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Target markets represented | {result.complete_markets:,} |",
        f"| Unique target chain events | {result.unique_events:,} |",
        f"| Aggressor aggregate events | {result.aggressor_events:,} |",
        f"| Maker child events (not counted as new parents) | {result.maker_child_events:,} |",
        f"| Distinct aggressor parent orders | {result.distinct_parent_orders:,} |",
        f"| Fee-paying parents still included | {result.fee_paying_parent_orders:,} |",
        f"| Conservative hidden zero-fee capacity bound | {result.inflated_weighted_volume} wV |",
        f"| Fraction of 2,000-wV Bronze | {fraction:.6%} |",
        "",
        "The bound includes possible hidden zero-fee fragments inside fee-paying parent orders. "
        "It groups by aggressor parent and therefore does not multiply one match by its maker-child events. "
        "Per parent it uses the full nominal concavity ceiling. The smaller fragment-count estimate is retained "
        "only as a conditional diagnostic: neither it nor observed positive fees may shrink the formal bound, "
        "because V2 events do not prove one fixed operator fee curve. An upper bound at or above Bronze is not "
        "evidence of an executable strategy.",
        "",
        "## Evidence gate",
        "",
    ]
    if reasons:
        lines.extend(f"- {reason}" for reason in reasons[:40])
        if len(reasons) > 40:
            lines.append(f"- … {len(reasons) - 40} additional repeated day-level failures")
    else:
        lines.append("- All frozen chain-manifest gates passed.")
    lines.extend(
        [
            "",
            "## Source",
            "",
            f"- Dataset commit: `{source_commit}`",
            f"- Parquet files: {source_files:,}; repository bytes: {source_bytes:,}; HTTP bytes fetched: {fetched_bytes:,}",
            "- https://huggingface.co/datasets/wzsg/polymarket-orderfilled-v2",
            "- https://github.com/Polymarket/ctf-exchange-v2",
            "",
        ]
    )
    return "\n".join(lines)


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, frozenset):
        return sorted(value)
    raise TypeError(type(value).__name__)


def write_evidence(
    path: Path,
    *,
    source_commit: str,
    files: list[SourceFile],
    inventory: SourceInventory,
    manifests: list[chain.DayManifest],
    audits: list[chain.DayCapacityAudit],
    result: chain.ChainCapacityResult,
) -> None:
    payload = {
        "source_commit": source_commit,
        "source_files": [asdict(item) for item in files],
        "source_inventory": asdict(inventory),
        "manifests": [asdict(item) for item in manifests],
        "audits": [asdict(item) for item in audits],
        "result": asdict(result),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(temporary, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle, default=_json_default, sort_keys=True, separators=(",", ":"))
    temporary.replace(path)


def _months_for_window(start_day: date, days: int) -> set[str]:
    final_capture_day = start_day + timedelta(days=days)
    current = start_day.replace(day=1)
    months: set[str] = set()
    while current <= final_capture_day:
        months.add(current.strftime("%Y-%m"))
        current = (current + timedelta(days=32)).replace(day=1)
    return months


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2026-07-02")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("real/reward-dust-chain.md"))
    parser.add_argument(
        "--evidence-out",
        type=Path,
        default=Path("real/reward-dust-chain.json.gz"),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.days != chain.REQUIRED_DAYS:
        raise ValueError("the frozen chain capacity window is exactly 30 days")
    start_day = date.fromisoformat(args.start)
    args.workdir.mkdir(parents=True, exist_ok=True)
    http = resolved.Http(
        args.workdir / "gamma",
        rate=2.0,
        opener=reward_dust.certified_json_get,
        tries=7,
    )
    token_maps = fetch_token_maps(http, start_day, args.days)
    metadata = reward_dust.certified_json_get(
        f"{HF_API}/{DATASET}/revision/{PINNED_REVISION}?blobs=true"
    )
    revision = PINNED_REVISION
    files = source_files(
        metadata,
        _months_for_window(start_day, args.days),
        expected_revision=PINNED_REVISION,
    )
    dataset_manifest = reward_dust.certified_json_get(
        f"{HF_BASE}/{DATASET}/resolve/{revision}/manifest.json"
    )
    collector_state = reward_dust.certified_json_get(
        f"{HF_BASE}/{DATASET}/resolve/{revision}/collector_state.json"
    )
    inventory = validate_source_inventory(
        files,
        dataset_manifest,
        collector_state,
        start_day=start_day,
        days=args.days,
    )
    if not inventory.complete:
        raise ValueError("source inventory failed: " + "; ".join(inventory.reasons))

    opened: list[tuple[SourceFile, Any, Any]] = []
    for index, item in enumerate(files, 1):
        pf, raw = cross.remote_parquet(item.url)
        missing = chain.REQUIRED_RAW_COLUMNS - frozenset(pf.schema_arrow.names)
        if missing:
            raise ValueError(f"{item.path}: missing {','.join(sorted(missing))}")
        opened.append((item, pf, raw))
        print(f"opened {index}/{len(files)} {item.path}", flush=True)

    raw_rows = sum(pf.metadata.num_rows for _, pf, _ in opened)
    preflight_timestamp_statistics([pf for _, pf, _ in opened])
    hashes = tuple(item.sha256 for item in files)
    manifests: list[chain.DayManifest] = []
    audits: list[chain.DayCapacityAudit] = []
    for offset in range(args.days):
        day = start_day + timedelta(days=offset)
        start_s = _day_start(day)
        events: list[chain.ChainFill] = []
        read_groups = 0
        for _item, pf, _raw in opened:
            part, stats = scan_parquet(
                pf,
                token_maps[day],
                capture_start_s=start_s,
                capture_end_s=start_s + 90_000,
            )
            events.extend(part)
            read_groups += stats.row_groups_read
        selected_rows, parent_shares = _unique_summary(events)
        manifest = build_day_manifest(
            day,
            token_maps[day],
            source_hashes=hashes,
            source_complete=inventory.complete,
            finalized_ranges_complete=inventory.finalized,
            reorg_conflicts=inventory.reorg_conflicts,
            raw_rows=raw_rows,
            selected_rows=selected_rows,
            parent_shares=parent_shares,
        )
        audit = chain.audit_day_events(events, manifest, retain_parent_bounds=False)
        manifests.append(manifest)
        audits.append(audit)
        print(
            f"{day}: {selected_rows:,} events, {audit.distinct_parent_orders:,} parents, "
            f"{audit.inflated_weighted_volume} wV, {read_groups:,} row groups",
            flush=True,
        )
        del events

    result = chain.combine_day_audits(audits, manifests)
    fetched_bytes = sum(int(raw.fetched) for _, _, raw in opened)
    report = render_report(
        result,
        source_commit=revision,
        source_files=len(files),
        source_bytes=sum(item.size for item in files),
        fetched_bytes=fetched_bytes,
        start_day=args.start,
        days=args.days,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report, encoding="utf-8")
    write_evidence(
        args.evidence_out,
        source_commit=revision,
        files=files,
        inventory=inventory,
        manifests=manifests,
        audits=audits,
        result=result,
    )
    print(f"wrote {args.out} and {args.evidence_out}: {result.verdict}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
