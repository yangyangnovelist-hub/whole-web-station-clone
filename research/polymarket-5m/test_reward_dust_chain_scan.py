from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import reward_dust_chain as chain
import reward_dust_chain_scan as scan


START = int(datetime(2026, 7, 2, tzinfo=UTC).timestamp())
TOKEN = "123"


def _token() -> scan.TokenMeta:
    return scan.TokenMeta(
        market_day=date(2026, 7, 2),
        market_start_s=START,
        market_id="condition",
        token_id=TOKEN,
    )


def _row(*, side=0, taker=scan.CTF_EXCHANGE_V2_ADDRESS, token=TOKEN):
    return {
        "timestamp": START + 10,
        "datetime": datetime.fromtimestamp(START + 10, UTC),
        "block_number": 90_000_001,
        "transaction_hash": "0x" + "ab" * 32,
        "log_index": 7,
        "contract": scan.CTF_EXCHANGE_V2_LABEL,
        "order_hash": "0x" + "cd" * 32,
        "maker": "0xmaker",
        "taker": taker,
        "side": side,
        "token_id": token,
        "maker_amount_filled": "100000",
        "taker_amount_filled": "200000",
        "fee": "7",
        "builder": "0x" + "00" * 32,
        "metadata": "0x" + "00" * 32,
    }


def test_v2_buy_and_sell_amounts_are_normalised_from_raw_integers_without_float_rounding():
    buy = scan.normalise_v2_row(_row(side=0), {TOKEN: _token()})
    sell = scan.normalise_v2_row(_row(side=1), {TOKEN: _token()})

    assert buy.role == chain.AGGRESSOR_PARENT
    assert buy.gross_collateral == Decimal("0.1")
    assert buy.gross_shares == Decimal("0.2")
    assert buy.fee_usdc == Decimal("0.000007")
    assert sell.gross_shares == Decimal("0.1")
    assert sell.gross_collateral == Decimal("0.2")


def test_uint256_scaling_preserves_units_beyond_decimal_context_precision():
    row = _row()
    row["maker_amount_filled"] = "10000000000000000000000000001"

    event = scan.normalise_v2_row(row, {TOKEN: _token()})

    assert event.gross_collateral.as_tuple().digits[-1] == 1
    assert event.gross_collateral == Decimal("10000000000000000000000.000001")


def test_v2_role_uses_the_pinned_exchange_address_not_modal_taker_frequency():
    parent = scan.normalise_v2_row(_row(taker=scan.CTF_EXCHANGE_V2_ADDRESS), {TOKEN: _token()})
    child = scan.normalise_v2_row(_row(taker="0xpopular-wallet"), {TOKEN: _token()})

    assert parent.role == chain.AGGRESSOR_PARENT
    assert child.role == chain.MAKER_CHILD


def test_source_file_selection_is_commit_pinned_and_retains_lfs_sha256():
    metadata = {
        "sha": "commit-sha",
        "siblings": [
            {
                "rfilename": "event_month=2026-07/data_0.parquet",
                "size": 123,
                "lfs": {"sha256": "a" * 64, "size": 123},
            },
            {
                "rfilename": "event_month=2026-08/data_0.parquet",
                "size": 456,
                "lfs": {"sha256": "b" * 64, "size": 456},
            },
            {"rfilename": "README.md", "size": 10},
        ],
    }

    files = scan.source_files(metadata, {"2026-07", "2026-08"})

    assert [item.path for item in files] == [
        "event_month=2026-07/data_0.parquet",
        "event_month=2026-08/data_0.parquet",
    ]
    assert files[0].sha256 == "a" * 64
    assert "/resolve/commit-sha/" in files[0].url

    with pytest.raises(ValueError, match="differs from pinned"):
        scan.source_files(metadata, {"2026-07"}, expected_revision=scan.PINNED_REVISION)


def test_window_months_include_the_following_partition_for_final_grace():
    assert scan._months_for_window(date(2026, 7, 2), 30) == {"2026-07", "2026-08"}


def _inventory_files():
    return [
        scan.SourceFile(
            path="event_month=2026-07/data.parquet",
            url="https://example.test/july",
            size=100,
            sha256="a" * 64,
        ),
        scan.SourceFile(
            path="event_month=2026-08/data.parquet",
            url="https://example.test/august",
            size=20,
            sha256="b" * 64,
        ),
    ]


def _dataset_manifest():
    return {
        "schema_version": "orderfilled_v2",
        "partition_timezone": "UTC",
        "selection": [scan.CTF_EXCHANGE_V2_LABEL],
        "validation": {"null_rows": 0, "max_block": 120},
        "partitions": {
            "2026-07": {
                "file_count": 1,
                "size_bytes": 100,
                "min_block": 100,
                "max_block": 110,
                "min_datetime": "2026-07-01T00:00:00+00:00",
                "max_datetime": "2026-07-31T23:59:59+00:00",
            },
            "2026-08": {
                "file_count": 1,
                "size_bytes": 20,
                "min_block": 111,
                "max_block": 120,
                "min_datetime": "2026-08-01T00:00:00+00:00",
                "max_datetime": "2026-08-03T23:59:59+00:00",
            },
        },
    }


def _collector_state():
    final_grace = datetime(2026, 8, 1, 1, tzinfo=UTC)
    return {
        "repo_id": scan.DATASET,
        "chain_id": scan.CHAIN_ID,
        "dataset_schema": "orderfilled_v2",
        "confirmation_depth": 512,
        "reorg_conflicts": 0,
        "last_scanned_block_hash": "0x" + "c" * 64,
        "last_scanned_block": 120,
        "target_block": 120,
        "next_block": 121,
        "last_event_block": 120,
        "last_event_timestamp": int((final_grace + timedelta(days=2)).timestamp()),
        "scanned_ranges": [{"from_block": 100, "to_block": 120}],
    }


def test_source_inventory_requires_exact_shards_and_finalized_window_coverage():
    inventory = scan.validate_source_inventory(
        _inventory_files(),
        _dataset_manifest(),
        _collector_state(),
        start_day=date(2026, 7, 2),
        days=30,
    )

    assert inventory.complete
    assert inventory.finalized
    assert inventory.confirmation_depth == 512
    assert inventory.reasons == ()


def test_source_inventory_fails_closed_on_missing_shard_or_short_final_grace():
    files = [replace(_inventory_files()[0], size=99)]
    state = {**_collector_state(), "last_event_timestamp": int(datetime(2026, 8, 1, tzinfo=UTC).timestamp())}

    inventory = scan.validate_source_inventory(
        files,
        _dataset_manifest(),
        state,
        start_day=date(2026, 7, 2),
        days=30,
    )

    assert not inventory.complete
    assert not inventory.finalized
    assert any("Parquet count" in reason or "Parquet bytes" in reason for reason in inventory.reasons)
    assert any("final market grace" in reason for reason in inventory.reasons)


def test_source_inventory_rejects_partition_gap_and_marks_missing_range_ledger_unfinalized():
    manifest = _dataset_manifest()
    manifest["partitions"]["2026-08"]["min_block"] = 999
    gap = scan.validate_source_inventory(
        _inventory_files(), manifest, _collector_state(), start_day=date(2026, 7, 2), days=30,
    )
    assert not gap.complete
    assert not gap.finalized
    assert any("block boundaries" in reason for reason in gap.reasons)

    state = {key: value for key, value in _collector_state().items() if key != "scanned_ranges"}
    unproven = scan.validate_source_inventory(
        _inventory_files(), _dataset_manifest(), state, start_day=date(2026, 7, 2), days=30,
    )
    assert unproven.complete
    assert not unproven.finalized
    assert any("scanned-range ledger" in reason for reason in unproven.reasons)


def test_source_inventory_requires_range_ledger_through_declared_safe_target():
    state = {
        **_collector_state(),
        "last_scanned_block": 1_000,
        "target_block": 1_000,
        "next_block": 1_001,
    }

    inventory = scan.validate_source_inventory(
        _inventory_files(), _dataset_manifest(), state, start_day=date(2026, 7, 2), days=30,
    )

    assert inventory.complete
    assert not inventory.finalized
    assert any("declared safe target block" in reason for reason in inventory.reasons)


def test_source_inventory_rejects_nonhex_hash_and_missing_or_positive_reorg_audit():
    invalid_hash = {
        **_collector_state(),
        "last_scanned_block_hash": "0x" + "z" * 64,
    }
    hash_inventory = scan.validate_source_inventory(
        _inventory_files(), _dataset_manifest(), invalid_hash, start_day=date(2026, 7, 2), days=30,
    )
    assert hash_inventory.complete
    assert not hash_inventory.finalized
    assert any("safe block hash" in reason for reason in hash_inventory.reasons)

    for state in (
        {key: value for key, value in _collector_state().items() if key != "reorg_conflicts"},
        {**_collector_state(), "reorg_conflicts": 1},
    ):
        inventory = scan.validate_source_inventory(
            _inventory_files(), _dataset_manifest(), state, start_day=date(2026, 7, 2), days=30,
        )
        assert inventory.complete
        assert not inventory.finalized
        assert inventory.reorg_conflicts != 0
        assert any("reorg" in reason for reason in inventory.reasons)


def test_local_parquet_scan_checks_schema_filters_token_and_preserves_chain_identity(tmp_path):
    rows = [
        _row(),
        {**_row(token="not-target"), "log_index": 8},
    ]
    table = pa.Table.from_pylist(rows)
    path = tmp_path / "raw.parquet"
    pq.write_table(table, path, row_group_size=1)

    events, stats = scan.scan_parquet(
        path,
        {TOKEN: _token()},
        capture_start_s=START,
        capture_end_s=START + 3_600,
    )

    assert stats.raw_rows == 2
    assert stats.selected_rows == 1
    assert len(events) == 1
    assert events[0].event_key == (
        137,
        scan.CTF_EXCHANGE_V2_ADDRESS.lower(),
        "0x" + "ab" * 32,
        7,
    )


def test_scan_requires_timestamp_statistics_instead_of_rereading_every_row_group(tmp_path):
    table = pa.Table.from_pylist([_row()])
    path = tmp_path / "no-stats.parquet"
    pq.write_table(table, path, write_statistics=False)

    with pytest.raises(ValueError, match="row-group statistics"):
        scan.scan_parquet(
            path,
            {TOKEN: _token()},
            capture_start_s=START,
            capture_end_s=START + 3_600,
        )


def test_scan_preflights_all_statistics_before_any_remote_row_group_read():
    class Stats:
        has_min_max = True
        min = START
        max = START + 10

    class Column:
        def __init__(self, statistics):
            self.statistics = statistics

    class RowGroup:
        def __init__(self, statistics):
            self._statistics = statistics

        def column(self, _index):
            return Column(self._statistics)

    class Metadata:
        num_row_groups = 2
        num_rows = 2

        def row_group(self, index):
            return RowGroup(Stats() if index == 0 else None)

    class FakeParquet:
        metadata = Metadata()
        schema_arrow = pa.schema([(name, pa.string()) for name in chain.REQUIRED_RAW_COLUMNS])

        def __init__(self):
            self.reads = 0

        def read_row_group(self, *_args, **_kwargs):
            self.reads += 1
            raise AssertionError("row groups must not be read before metadata validation")

    parquet = FakeParquet()
    with pytest.raises(ValueError, match="row-group statistics"):
        scan.scan_parquet(
            parquet,
            {TOKEN: _token()},
            capture_start_s=START,
            capture_end_s=START + 3_600,
        )
    assert parquet.reads == 0


def test_dataset_preflight_checks_later_shards_before_reading_earlier_ones():
    class Stats:
        has_min_max = True
        min = START
        max = START + 10

    class Column:
        def __init__(self, statistics):
            self.statistics = statistics

    class RowGroup:
        def __init__(self, statistics):
            self._statistics = statistics

        def column(self, _index):
            return Column(self._statistics)

    class Metadata:
        num_row_groups = 1

        def __init__(self, statistics):
            self._statistics = statistics

        def row_group(self, _index):
            return RowGroup(self._statistics)

    class FakeParquet:
        schema_arrow = pa.schema([("timestamp", pa.int64())])

        def __init__(self, statistics):
            self.metadata = Metadata(statistics)
            self.reads = 0

        def read_row_group(self, *_args, **_kwargs):
            self.reads += 1

    first = FakeParquet(Stats())
    later_broken = FakeParquet(None)

    with pytest.raises(ValueError, match="row-group statistics"):
        scan.preflight_timestamp_statistics([first, later_broken])
    assert first.reads == 0
    assert later_broken.reads == 0


def test_report_never_promotes_an_upper_bound_without_public_parity():
    result = chain.ChainCapacityResult(
        verdict=chain.INCOMPLETE_CHAIN_MANIFEST,
        evidence_class="parent_order_chain_upper_bound",
        inflation_mode=chain.PARENT_FRAGMENT_UPPER_BOUND,
        complete_days=30,
        complete_markets=8_640,
        unique_events=10,
        exact_duplicates=0,
        aggressor_events=4,
        maker_child_events=6,
        distinct_parent_orders=4,
        fee_paying_parent_orders=3,
        inflated_weighted_volume=Decimal("12.5"),
        bronze_weighted_volume=Decimal(2000),
        parent_bounds=(),
        incomplete_reasons=("2026-07-02: Data API aggregate parity was not checked",),
    )

    report = scan.render_report(
        result,
        source_commit="abc",
        source_files=47,
        source_bytes=10_000,
        fetched_bytes=2_000,
        start_day="2026-07-02",
        days=30,
    )

    assert "INCOMPLETE_CHAIN_MANIFEST" in report
    assert "12.5" in report
    assert "Data API aggregate parity was not checked" in report
    assert "PASS" not in report
    assert "target UTC market days" in report
    assert "Complete markets" not in report
