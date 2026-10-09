import gzip
import json

import poly_clob_rec as poly
import raw_rec as raw


def test_raw_recorder_lifecycle_marker_keeps_frame_format(tmp_path, monkeypatch):
    monkeypatch.setattr(raw, "OUT", str(tmp_path))
    sink = raw.Sink("deribit")
    sink.lifecycle(123, "connection", 123, source="deribit")
    sink.lifecycle(456, "disconnect", 123, source="deribit")
    sink.close()

    with gzip.open(tmp_path / "deribit.19700101T00.txt.gz", "rt") as stream:
        rows = [line.rstrip().split("\t", 1) for line in stream]
    assert [int(row[0]) for row in rows] == [123, 456]
    markers = [json.loads(row[1])["_recorder"] for row in rows]
    assert [marker["kind"] for marker in markers] == ["connection", "disconnect"]
    assert {marker["epoch"] for marker in markers} == {123}


def test_formal_source_race_uses_lean_trade_and_bbo_routes():
    assert raw.SOURCES["bn_spot_4"][0].endswith("btcusdt@trade?timeUnit=MICROSECOND")
    assert raw.SOURCES["bn_fut_pub_3"][0].endswith("streams=btcusdt@bookTicker")


def test_poly_recorder_envelope_binds_epoch_and_sequence():
    row = poly.envelope(999, 1, 888, 7, "message", m={"event_type": "book"})

    assert row == {
        "rn": 999,
        "c": 1,
        "e": 888,
        "s": 7,
        "k": "message",
        "m": {"event_type": "book"},
    }


def test_poly_sink_close_finishes_a_valid_gzip_member(tmp_path, monkeypatch):
    monkeypatch.setattr(poly, "OUT", str(tmp_path))
    sink = poly.Sink()
    sink.write('{"rn":1,"c":0,"k":"message","m":[]}')
    sink.close()

    path = next(tmp_path.glob("poly_clob.*.jsonl.gz"))
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        assert json.loads(stream.readline()) == {"rn": 1, "c": 0, "k": "message", "m": []}


def test_poly_recorder_keeps_only_the_fastest_duplicate_within_window():
    recent = poly.RecentFrames(keep_ns=100)

    assert recent.first("same-frame", 1_000) is True
    assert recent.first("same-frame", 1_050) is False
    assert recent.first("same-frame", 1_151) is True


def test_poly_recorder_sanitizes_transport_newlines_before_jsonl_embedding():
    assert poly.single_line_frame('[{"event_type":"book"}]\r\n') == \
        '[{"event_type":"book"}]  '
    assert poly.single_line_frame(b'{"event_type":"price_change"}\n') == \
        '{"event_type":"price_change"} '


def test_poly_recorder_subscribes_only_to_current_and_next_five_minute_market():
    assert poly.market_slugs(1_791_590_499) == [
        "btc-updown-5m-1791590400",
        "btc-updown-5m-1791590700",
    ]
