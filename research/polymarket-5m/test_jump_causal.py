from __future__ import annotations

import gzip
import hashlib
import json
import math

import numpy as np
import pytest

import jump_causal as jc


MS = 1_000_000
SECOND = 1_000_000_000


def trade(
    recv_ns: int,
    exchange_ns: int,
    price: float,
    *,
    source: str = "bn_spot",
    sign: int = 1,
    sequence_id: int | None = None,
):
    return jc.MarketEvent(
        recv_ns=recv_ns,
        source=source,
        channel=source,
        kind="trade",
        exchange_ns=exchange_ns,
        price=price,
        quantity=1.0,
        trade_sign=sign,
        sequence_id=None if sequence_id is None else str(sequence_id),
    )


def book(
    recv_ns: int,
    mid: float,
    bid_depth: float,
    ask_depth: float,
    *,
    source: str,
):
    return jc.MarketEvent(
        recv_ns=recv_ns,
        source=source,
        channel=source,
        kind="book",
        exchange_ns=recv_ns - 10 * MS,
        bid=mid - 0.5,
        ask=mid + 0.5,
        bid_depth=bid_depth,
        ask_depth=ask_depth,
        book_kind="depth_snapshot",
    )


def slow_detector(prices: list[tuple[int, float]]) -> list[tuple[int, int, int]]:
    """Literal reference for the production 1 s / 1 bp / 5 s detector."""
    fired: list[tuple[int, int, int]] = []
    last_fire = -10**30
    for i, (stamp, price) in enumerate(prices):
        if stamp - last_fire < 5 * SECOND:
            continue
        window = [(t, p) for t, p in prices[: i + 1] if t > stamp - SECOND]
        # min/max ties resolve to the latest print.
        min_price = min(p for _, p in window)
        max_price = max(p for _, p in window)
        min_time = max(t for t, p in window if p == min_price)
        max_time = max(t for t, p in window if p == max_price)
        lp = math.log(price) * 10_000
        up = lp - math.log(min_price) * 10_000
        down = math.log(max_price) * 10_000 - lp
        if up >= 1.0 and up >= down:
            fired.append((stamp, 1, min_time))
            last_fire = stamp
        elif down >= 1.0:
            fired.append((stamp, -1, max_time))
            last_fire = stamp
    return fired


def test_detector_matches_literal_reference_including_ties_window_and_refractory():
    base = 1_790_000_000 * SECOND
    prices = [
        (base, 100.0),
        (base + 100 * MS, 100.0),  # latest equal minimum wins
        (base + 220 * MS, 100.011),
        (base + 999 * MS, 99.999),
        (base + 1_100 * MS, 100.0105),  # first print is exactly 1 s old and excluded
        (base + 5_220 * MS, 100.022),  # exact 5 s boundary may fire again
    ]
    detector = jc.ExactJumpDetector()
    got = []
    for stamp, price in prices:
        jump = detector.update(stamp, price)
        if jump is not None:
            got.append((jump.exchange_ns, jump.direction, jump.extremum_ns))

    assert got == slow_detector(prices)
    assert got[0] == (base + 220 * MS, 1, base + 100 * MS)


def test_detector_rejects_exchange_timestamp_regression():
    detector = jc.ExactJumpDetector()
    detector.update(1_790_000_100 * SECOND, 100.0)
    with pytest.raises(ValueError, match="timestamp regressed"):
        detector.update(1_790_000_100 * SECOND - 1, 100.0)


def test_feature_rows_use_receipt_order_not_exchange_timestamp():
    base = 1_790_000_000 * SECOND
    events = [
        trade(base, base - 20 * MS, 100.0),
        # Exchange timestamp is old, but this frame arrives after the first decision.
        trade(base + 80 * MS, base + 10 * MS, 100.02),
    ]
    rows = jc.build_feature_rows(events, np.array([base + 50 * MS, base + 100 * MS]))
    names = {name: i for i, name in enumerate(rows.feature_names)}

    assert rows.X[0, names["detector_last_price"]] == pytest.approx(100.0)
    assert rows.X[0, names["detector_up_move_bp"]] == pytest.approx(0.0)
    assert rows.X[1, names["detector_last_price"]] == pytest.approx(100.02)
    assert rows.X[1, names["detector_up_move_bp"]] > 1.0


def test_detector_state_and_microstructure_features_are_mechanistic():
    base = 1_790_000_000 * SECOND
    events = [
        trade(base, base - 20 * MS, 100.0),
        book(base, 100.0, 10.0, 10.0, source="bn_spot"),
        book(base, 100.0, 12.0, 8.0, source="bn_perp"),
        trade(base + 100 * MS, base + 80 * MS, 100.005, source="bn_spot", sign=1),
        book(base + 100 * MS, 100.005, 5.0, 9.0, source="bn_spot"),
        book(base + 100 * MS, 100.015, 6.0, 4.0, source="bn_perp"),
    ]
    rows = jc.build_feature_rows(events, np.array([base + 100 * MS]))
    row = dict(zip(rows.feature_names, rows.X[0], strict=True))

    assert row["detector_up_distance_bp"] == pytest.approx(0.5, abs=0.001)
    assert row["detector_min_age_ms"] == pytest.approx(100.0)
    assert row["bn_spot_trade_count_200ms"] == 2
    assert row["bn_spot_bid_depletion_200ms"] == pytest.approx(0.5)
    assert row["bn_spot_return_50ms_bp"] == pytest.approx(math.log(100.005 / 100.0) * 10_000)
    assert row["bn_perp_return_50ms_bp"] == pytest.approx(math.log(100.015 / 100.0) * 10_000)
    assert row["bn_perp_residual_bp"] == pytest.approx(1.0, abs=0.01)


def test_bbo_update_cannot_masquerade_as_top20_depth_depletion():
    base = 1_790_000_000 * SECOND
    depth = book(base, 100.0, 10.0, 10.0, source="bn_spot")
    bbo = jc.MarketEvent(
        recv_ns=base + 100 * MS,
        source="bn_spot",
        channel="bn_spot",
        kind="book",
        bid=99.5,
        ask=100.5,
        bid_depth=1.0,
        ask_depth=1.0,
        book_kind="bbo",
    )
    rows = jc.build_feature_rows((depth, bbo), np.array([base + 100 * MS]))
    row = dict(zip(rows.feature_names, rows.X[0], strict=True))
    assert row["bn_spot_bid_depletion_200ms"] == 0.0
    assert row["bn_spot_ask_depletion_200ms"] == 0.0


def test_bbo_cannot_make_stale_depth_look_fresh():
    base = 1_790_000_000 * SECOND
    depth = book(base, 100.0, 9.0, 1.0, source="bn_spot")
    bbo = jc.MarketEvent(
        recv_ns=base + 2 * SECOND,
        source="bn_spot",
        channel="bn_spot",
        kind="book",
        bid=100.0,
        ask=101.0,
        book_kind="bbo",
    )
    rows = jc.build_feature_rows((depth, bbo), np.array([base + 2 * SECOND]))
    row = dict(zip(rows.feature_names, rows.X[0], strict=True))

    assert row["bn_spot_age_ms"] == 0.0
    assert row["bn_spot_depth_age_ms"] == 2_000.0
    assert math.isnan(row["bn_spot_depth_imbalance"])


def test_labels_use_exchange_time_120_to_220_ms_window():
    base = 1_790_000_000 * SECOND
    times = np.array([base, base + MS, base + 2 * MS], dtype=np.int64)
    jumps = (
        jc.TargetJump(base + 119 * MS, base + 400 * MS, 1, 1.1),
        jc.TargetJump(base + 121 * MS, base + 450 * MS, -1, 1.2),
        jc.TargetJump(base + 223 * MS, base + 500 * MS, 1, 1.3),
    )

    labels, matched = jc.label_decisions(times, jumps)

    assert labels.tolist() == [-1, -1, 0]
    assert matched.tolist() == [1, 1, -1]


def test_labels_do_not_credit_a_crossing_already_received_by_the_decision():
    base = 1_790_000_000 * SECOND
    labels, matched = jc.label_decisions(
        np.array([base], dtype=np.int64),
        (
            jc.TargetJump(base + 150 * MS, base - 1, 1, 1.1),
            jc.TargetJump(base + 180 * MS, base + 200 * MS, -1, 1.2),
        ),
    )
    assert labels.tolist() == [-1]
    assert matched.tolist() == [1]


def test_label_nanosecond_boundaries_are_exact_and_inclusive():
    base = 1_790_000_000 * SECOND
    offsets = (120 * MS - 1, 120 * MS, 220 * MS, 220 * MS + 1)
    labels = []
    for offset in offsets:
        value, _ = jc.label_decisions(
            np.array([base], dtype=np.int64),
            (jc.TargetJump(base + offset, base + 500 * MS, 1, 1.0),),
        )
        labels.append(int(value[0]))
    assert labels == [0, 1, 1, 0]


def test_target_tape_deduplicates_trade_ids_and_rejects_regressions():
    slot = (1_790_000_000 - (1_790_000_000 % 300)) * SECOND + 60 * SECOND
    first = trade(slot, slot, 100.0)
    duplicate = jc.MarketEvent(**{**first.__dict__, "recv_ns": slot + MS, "sequence_id": "10"})
    first = jc.MarketEvent(**{**first.__dict__, "sequence_id": "10"})
    crossing = jc.MarketEvent(**{
        **trade(slot + 2 * MS, slot + 2 * MS, 100.011).__dict__,
        "sequence_id": "11",
    })
    assert len(jc.extract_target_jumps((first, duplicate, crossing))) == 1
    regression = jc.MarketEvent(**{**crossing.__dict__, "recv_ns": slot + 3 * MS, "sequence_id": "9"})
    with pytest.raises(ValueError, match="trade id regressed"):
        jc.extract_target_jumps((first, crossing, regression))


def test_quiet_trade_interval_does_not_reset_detector_when_transport_is_live():
    base = (1_790_000_000 - (1_790_000_000 % 300)) * SECOND + 60 * SECOND
    events = [
        trade(base, base, 100.0, sequence_id=10),
        trade(base + 100 * MS, base + 100 * MS, 100.011, sequence_id=11),
    ]
    for offset in (200, 600, 1_000, 1_400):
        events.append(book(base + offset * MS, 100.011, 5.0, 5.0, source="bn_spot"))
    events.extend((
        trade(base + 1_500 * MS, base + 1_500 * MS, 100.011, sequence_id=12),
        trade(base + 1_600 * MS, base + 1_600 * MS, 100.022, sequence_id=13),
    ))
    events.sort(key=lambda item: item.recv_ns)

    engine = jc.CausalFeatureEngine()
    engine_fires = [jump for event in events if (jump := engine.apply(event)) is not None]
    target_fires = jc.extract_target_jumps(events, max_gap_ms=1_000)

    assert len(engine_fires) == 1
    assert len(target_fires) == 1


def test_active_window_requires_the_whole_label_horizon_inside_detector_hours():
    slot = (1_790_000_000 - (1_790_000_000 % 300)) * SECOND
    times = np.array([
        slot + 59_880 * MS - 1,
        slot + 59_880 * MS,
        slot + 284_780 * MS - 1,
        slot + 284_780 * MS,
    ])
    assert jc.active_decision_mask(times).tolist() == [False, True, True, False]


def test_raw_parser_preserves_top20_depth_and_exchange_clocks():
    recv = 1_790_000_000_500_000_000
    depth = jc.parse_frame("bn_spot", recv, {
        "stream": "btcusdt@depth20@100ms",
        "data": {
            "bids": [["100", "3"], ["99", "1"]],
            "asks": [["101", "1"], ["102", "2"]],
        },
    })[0]
    future = jc.parse_frame("bn_fut_trade", recv, {
        "e": "trade", "T": 1_790_000_000_123, "p": "101", "q": "0.3", "m": False,
    })[0]

    assert (depth.bid, depth.ask, depth.bid_depth, depth.ask_depth) == (100.0, 101.0, 4.0, 3.0)
    assert future.exchange_ns == 1_790_000_000_123_000_000
    assert (future.channel, future.trade_sign) == ("bn_perp", 1)


def test_deribit_quote_is_a_live_bbo_but_batched_trades_are_not_timing_features():
    recv = 1_790_000_000_500_000_000
    quote = jc.parse_frame("deribit", recv, {
        "jsonrpc": "2.0",
        "method": "subscription",
        "params": {
            "channel": "quote.BTC-PERPETUAL",
            "data": {
                "timestamp": 1_790_000_000_123,
                "instrument_name": "BTC-PERPETUAL",
                "best_ask_price": 100.5,
                "best_bid_price": 100.0,
                "best_ask_amount": 12.0,
                "best_bid_amount": 8.0,
            },
        },
    })
    batched_trades = jc.parse_frame("deribit", recv, {
        "jsonrpc": "2.0",
        "method": "subscription",
        "params": {
            "channel": "trades.BTC-PERPETUAL.100ms",
            "data": [{"timestamp": 1_790_000_000_100, "price": 100.25, "amount": 1.0}],
        },
    })

    assert len(quote) == 1
    assert quote[0].channel == "deribit"
    assert quote[0].book_kind == "bbo"
    assert quote[0].exchange_ns == 1_790_000_000_123_000_000
    assert (quote[0].bid, quote[0].ask) == (100.0, 100.5)
    assert batched_trades == []


def test_gaps_are_unavailable_not_negative_and_require_recovery_warmup():
    base = 1_790_000_000 * SECOND
    recv = {
        "bn_spot": np.array([base + i * 100 * MS for i in range(15)] +
                            [base + 3 * SECOND + i * 100 * MS for i in range(15)], dtype=np.int64),
        "okx": np.array([base + i * 100 * MS for i in range(50)], dtype=np.int64),
        "bybit": np.array([base + i * 100 * MS for i in range(50)], dtype=np.int64),
    }
    sessions = {source: jc.continuous_sessions(values, max_gap_ms=250) for source, values in recv.items()}
    times = np.array([
        base + 1_100 * MS,  # complete first session
        base + 1_400 * MS,  # label horizon crosses the gap
        base + 3_500 * MS,  # recovery has not rebuilt 1 s of history
        base + 4_100 * MS,  # complete recovered session
    ])

    mask = jc.availability_mask(
        times,
        sessions,
        primary="bn_spot",
        leaders=("okx", "bybit"),
        min_leaders=2,
        feature_lookback_ms=1_000,
        label_lookahead_ms=220,
    )

    assert mask.tolist() == [True, False, False, True]


def test_target_session_uses_multiplexed_transport_not_trade_cadence(tmp_path):
    base = 1_790_000_000 * SECOND
    path = tmp_path / "bn_spot.20261005T00.txt.gz"
    rows = []
    for index in range(23):
        recv = base + index * 100 * MS
        if index in (0, 20):
            payload = {
                "stream": "btcusdt@trade",
                "data": {
                    "e": "trade", "T": recv // 1_000, "t": 10 + index // 20,
                    "p": "100", "q": "1", "m": False,
                },
            }
        else:
            payload = {
                "stream": "btcusdt@bookTicker",
                "data": {"b": "99", "B": "1", "a": "101", "A": "1"},
            }
        rows.append((recv, payload))
    with gzip.open(path, "wt") as handle:
        for stamp, payload in rows:
            handle.write(f"{stamp}\t{json.dumps(payload, separators=(',', ':'))}\n")

    sessions = jc.certified_target_sessions_from_files([path], max_gap_ms=250)

    assert len(sessions) == 1
    assert sessions[0].start_recv_ns == rows[0][0]
    assert sessions[0].end_recv_ns == rows[-1][0]
    assert sessions[0].first_sequence == 10
    assert sessions[0].last_sequence == 11


def test_target_trade_id_gap_splits_a_live_multiplexed_transport(tmp_path):
    base = 1_790_000_000 * SECOND
    path = tmp_path / "bn_spot.20261005T00.txt.gz"
    with gzip.open(path, "wt") as handle:
        for index, trade_id in enumerate((10, 12)):
            recv = base + index * 100 * MS
            payload = {
                "stream": "btcusdt@trade",
                "data": {
                    "e": "trade", "T": recv // 1_000, "t": trade_id,
                    "p": "100", "q": "1", "m": False,
                },
            }
            handle.write(f"{recv}\t{json.dumps(payload, separators=(',', ':'))}\n")

    sessions = jc.certified_target_sessions_from_files([path], max_gap_ms=250)

    assert len(sessions) == 2
    assert [(item.first_sequence, item.last_sequence) for item in sessions] == [(10, 10), (12, 12)]


def test_detector_gap_state_is_quarantined_until_the_next_slot():
    slot = (1_790_000_000 - (1_790_000_000 % 300)) * SECOND
    sessions = ((slot + 10 * SECOND, slot + 350 * SECOND),
                (slot + 410 * SECOND, slot + 650 * SECOND))
    times = np.array([slot + 299 * SECOND, slot + 300 * SECOND, slot + 599 * SECOND, slot + 600 * SECOND])
    assert jc.detector_gap_safe_mask(times, sessions).tolist() == [False, True, False, True]


def test_purged_splits_remove_every_window_crossing_a_boundary():
    times = np.arange(0, 30_001, 100, dtype=np.int64) * MS
    split = jc.purged_three_way_split(
        times,
        train_end_ns=10_000 * MS,
        validation_end_ns=20_000 * MS,
        feature_lookback_ms=1_000,
        label_lookahead_ms=220,
    )

    assert times[split.train].max() + 220 * MS < 10_000 * MS
    assert times[split.validation].min() - 5 * SECOND >= 10_000 * MS
    assert times[split.validation].max() + 220 * MS < 20_000 * MS
    assert times[split.test].min() - 5 * SECOND >= 20_000 * MS
    assert not (split.train & split.validation).any()
    assert not (split.validation & split.test).any()


def test_manifest_is_deterministic_hashed_and_pins_consumed_baseline(tmp_path):
    path = tmp_path / "bn_spot.20261005T00.txt.gz"
    rows = [
        (1_790_000_000_000_000_000, {"stream": "btcusdt@trade", "data": {"e": "trade", "T": 1, "p": "1", "q": "1", "m": False}}),
        (1_790_000_000_100_000_000, {"stream": "btcusdt@trade", "data": {"e": "trade", "T": 2, "p": "2", "q": "1", "m": False}}),
    ]
    with gzip.open(path, "wt") as handle:
        for stamp, payload in rows:
            handle.write(f"{stamp}\t{json.dumps(payload, sort_keys=True)}\n")

    first = jc.build_split_manifest(
        [path],
        train_end_ns=1_790_000_000_030_000_000,
        validation_end_ns=1_790_000_000_060_000_000,
    )
    second = jc.build_split_manifest(
        [path],
        train_end_ns=1_790_000_000_030_000_000,
        validation_end_ns=1_790_000_000_060_000_000,
    )

    assert first == second
    assert first["consumed_baseline"] == jc.CONSUMED_BASELINE
    assert first["sources"][0]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert first["sources"][0]["first_recv_ns"] == rows[0][0]
    assert first["sources"][0]["last_recv_ns"] == rows[-1][0]
    assert first["protocol"]["target_lead_ms"] == [120, 220]
    assert first["manifest_sha256"] == jc.manifest_digest(first)


def test_manifest_fails_closed_on_malformed_or_truncated_raw_file(tmp_path):
    path = tmp_path / "bn_spot.20261005T00.txt.gz"
    with gzip.open(path, "wb") as handle:
        handle.write(b"1790000000000000000 no-tab\n")
    with pytest.raises(ValueError, match="missing tab"):
        jc.build_split_manifest([path], train_end_ns=1, validation_end_ns=2)

    with gzip.open(path, "wb") as handle:
        handle.write(b"1790000000000000000\t{not-json}\n")
    with pytest.raises(ValueError, match="malformed JSON"):
        jc.build_split_manifest([path], train_end_ns=1, validation_end_ns=2)

    heartbeat = tmp_path / "okx.20261005T00.txt.gz"
    with gzip.open(heartbeat, "wb") as handle:
        handle.write(b"1790000000000000000\tpong\n")
    manifest = jc.build_split_manifest([heartbeat], train_end_ns=1, validation_end_ns=2)
    assert manifest["sources"][0]["ignored_control_or_unmodelled_frames"] == 1


def test_closed_hour_selection_requires_every_source_in_every_hour(tmp_path):
    hours = ("20261004T14", "20261004T15")
    for hour in hours:
        for source in jc.RAW_SOURCES:
            (tmp_path / f"{source}.{hour}.txt.gz").touch()

    selected = jc.select_closed_hour_files(tmp_path, hours)

    assert len(selected) == len(hours) * len(jc.RAW_SOURCES)
    (tmp_path / f"deribit.{hours[1]}.txt.gz").unlink()
    with pytest.raises(ValueError, match=r"20261004T15.*deribit"):
        jc.select_closed_hour_files(tmp_path, hours)


def test_freeze_rejects_any_overlap_with_consumed_baseline(tmp_path):
    start = jc.CONSUMED_START_NS + SECOND
    with pytest.raises(ValueError, match="consumed immutable baseline"):
        jc.freeze_dataset(
            [],
            output_dir=tmp_path / "dataset",
            manifest_path=tmp_path / "manifest.json",
            sample_start_ns=start,
            train_end_ns=start + SECOND,
            validation_end_ns=start + 2 * SECOND,
            sample_end_ns=start + 3 * SECOND,
        )
    for sample_start, sample_end in (
        (jc.CONSUMED_START_NS - 3 * SECOND, jc.CONSUMED_START_NS),
        (jc.CONSUMED_END_NS, jc.CONSUMED_END_NS + 3 * SECOND),
    ):
        with pytest.raises(ValueError, match="consumed immutable baseline"):
            jc.freeze_dataset(
                [],
                output_dir=tmp_path / "dataset",
                manifest_path=tmp_path / "manifest.json",
                sample_start_ns=sample_start,
                train_end_ns=sample_start + SECOND,
                validation_end_ns=sample_start + 2 * SECOND,
                sample_end_ns=sample_end,
            )


def test_freeze_rejects_source_mutation_after_initial_hash(tmp_path, monkeypatch):
    base = 1_800_000_000 * SECOND
    path = tmp_path / "bn_spot.20270115T08.txt.gz"
    rows = []
    for index in range(401):
        recv = base + index * 100 * MS
        rows.append((recv, {
            "stream": "btcusdt@trade",
            "data": {
                "e": "trade", "T": recv // 1_000, "t": index,
                "p": "100", "q": "1", "m": False,
            },
        }))

    def write_rows(quantity: str) -> None:
        with gzip.open(path, "wt") as handle:
            for stamp, payload in rows:
                payload["data"]["q"] = quantity
                handle.write(f"{stamp}\t{json.dumps(payload, separators=(',', ':'))}\n")

    write_rows("1")
    original = jc._source_file_metadata
    changed = False

    def hash_then_mutate(raw_path):
        nonlocal changed
        metadata = original(raw_path)
        if not changed:
            write_rows("2")
            changed = True
        return metadata

    monkeypatch.setattr(jc, "_source_file_metadata", hash_then_mutate)
    with pytest.raises(ValueError, match="changed after initial hash"):
        jc.freeze_dataset(
            [path],
            output_dir=tmp_path / "dataset",
            manifest_path=tmp_path / "manifest.json",
            sample_start_ns=base + 5 * SECOND,
            train_end_ns=base + 15 * SECOND,
            validation_end_ns=base + 25 * SECOND,
            sample_end_ns=base + 35 * SECOND,
        )


def test_freeze_dataset_writes_only_causal_eligible_labels(tmp_path):
    slot_s = 1_790_000_000 - (1_790_000_000 % 300)
    base = slot_s * SECOND
    paths = {}
    for source in ("bn_spot", "bn_fut_pub", "okx", "bybit_spot"):
        path = tmp_path / f"{source}.20261005T00.txt.gz"
        paths[source] = path
        with gzip.open(path, "wt") as handle:
            for i in range(3_711):
                recv = base + i * 100 * MS
                if source == "bn_spot":
                    price = 100.0 if i < 3_620 else 100.011
                    payload = {
                        "stream": "btcusdt@trade",
                        "data": {
                            "e": "trade", "T": recv // 1_000, "t": i,
                            "p": str(price), "q": "1", "m": False,
                        },
                    }
                elif source == "bn_fut_pub":
                    payload = {
                        "stream": "btcusdt@bookTicker",
                        "data": {"e": "bookTicker", "T": recv // MS, "b": "99.9", "B": "2", "a": "100.1", "A": "2"},
                    }
                elif source == "okx":
                    payload = {
                        "arg": {"channel": "bbo-tbt", "instId": "BTC-USDT"},
                        "data": [{"ts": str(recv // MS), "bids": [["99.9", "2"]], "asks": [["100.1", "2"]]}],
                    }
                else:
                    payload = {
                        "topic": "orderbook.1.BTCUSDT",
                        "cts": recv // MS,
                        "data": {"b": [["99.9", "2"]], "a": [["100.1", "2"]]},
                    }
                handle.write(f"{recv}\t{json.dumps(payload, separators=(',', ':'))}\n")

    output = tmp_path / "dataset"
    manifest_path = tmp_path / "manifest.json"
    manifest = jc.freeze_dataset(
        list(paths.values()),
        output_dir=output,
        manifest_path=manifest_path,
        sample_start_ns=base + 361_200 * MS,
        train_end_ns=base + 364_000 * MS,
        validation_end_ns=base + 367_000 * MS,
        sample_end_ns=base + 370_500 * MS,
    )
    labels = np.load(output / "labels.npy")
    eligible = np.load(output / "eligible.npy")
    split = np.load(output / "split.npy")
    features = np.load(output / "features.npy", mmap_mode="r")

    assert features.shape == (len(labels), manifest["dataset"]["features"])
    assert (labels[~eligible] == -128).all()
    assert set(split[eligible]) <= {1, 2, 3}
    assert np.count_nonzero(labels[eligible]) > 0
    assert manifest_path.exists()
    assert manifest["manifest_sha256"] == jc.manifest_digest(manifest)
    assert all(item["sha256"] for item in manifest["dataset"]["artifacts"])
