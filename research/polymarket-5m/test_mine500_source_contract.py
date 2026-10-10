"""Input-contract tests are engineering checks, never PnL evidence."""
import pytest

from mine500_source_contract import (
    BUY, SELL, ReceiptTape, explicit_aggressor_side,
    normalize_external_trade, normalize_twap_sample, signed_volume_fraction,
)


def trade(recv, *, side="BUY", event=None, price=100.0, quantity=2.0):
    row = {"source":"binance", "symbol":"BTCUSDT", "source_event_ns":recv-1,
           "local_recv_ns":recv, "sequence":str(recv), "price":price,
           "quantity":quantity}
    if side is not None:
        row["aggressor_side"] = side
    if event:
        row.update(event)
    return row


def twap(recv=70_000_000_000, **updates):
    row = {"provider":"chainlink_data_streams", "symbol":"BTC/USD",
           "window_seconds":60, "window_start_ns":0,
           "window_end_ns":60_000_000_000,
           "source_event_ns":60_100_000_000, "local_recv_ns":recv,
           "sequence":"twap-1", "value":100_000.0,
           "provenance":"raw_provider_frame"}
    row.update(updates)
    return row


def test_documented_maker_flag_mapping_and_explicit_side():
    assert explicit_aggressor_side({"is_buyer_maker":False}) == (BUY, "is_buyer_maker")
    assert explicit_aggressor_side({"buyer_is_maker":True}) == (SELL, "buyer_is_maker")
    assert explicit_aggressor_side({"side":"sell"}) == (SELL, "side")


def test_conflict_fails_closed_and_missing_stays_missing():
    with pytest.raises(ValueError, match="conflicting"):
        explicit_aggressor_side({"aggressor_side":"BUY", "is_buyer_maker":True})
    row = normalize_external_trade(trade(20, side=None))
    assert row.aggressor_side is None and row.side_provenance == "missing"


def test_price_changes_never_impute_side_and_missing_blocks_flow():
    rows = [normalize_external_trade(trade(1_000_000_000, side="BUY", price=100)),
            normalize_external_trade(trade(2_000_000_000, side=None, price=110))]
    assert signed_volume_fraction(rows, decision_recv_ns=2_000_000_000,
                                  window_seconds=15) is None


def test_receipt_cutoff_excludes_later_trade_and_source_clock_does_not_reorder():
    rows = [normalize_external_trade(trade(10_000_000_000, side="BUY",
                                           event={"source_event_ns":9_000_000_000})),
            normalize_external_trade(trade(20_000_000_000, side="SELL",
                                           event={"source_event_ns":1_000_000_000}))]
    assert signed_volume_fraction(rows, decision_recv_ns=15_000_000_000,
                                  window_seconds=15) == 1.0
    assert signed_volume_fraction(rows, decision_recv_ns=20_000_000_000,
                                  window_seconds=15) == 0.0


def test_future_source_guard_and_receipt_monotonicity():
    with pytest.raises(ValueError, match="ahead"):
        normalize_external_trade(trade(1, event={"source_event_ns":20_000_000_000}))
    tape = ReceiptTape()
    tape.add_trade(trade(20, event={"source_event_ns":19}))
    with pytest.raises(ValueError, match="backwards"):
        tape.add_trade(trade(19, event={"source_event_ns":18}))


@pytest.mark.parametrize("updates", [
    {"provider":"binance"}, {"symbol":"ETH/USD"}, {"window_seconds":30},
    {"window_end_ns":59_000_000_000},
])
def test_unverified_or_wrong_twap_is_rejected(updates):
    with pytest.raises(ValueError):
        normalize_twap_sample(twap(**updates))


def test_twap_preserves_two_clocks_and_is_receipt_causal():
    tape = ReceiptTape()
    sample = tape.add_twap(twap())
    assert sample.source_event_ns == 60_100_000_000
    assert sample.local_recv_ns == 70_000_000_000
    assert tape.known_twap(69_999_999_999) is None
    assert tape.known_twap(70_000_000_000) == sample
