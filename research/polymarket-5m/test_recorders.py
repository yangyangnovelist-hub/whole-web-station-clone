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


def test_poly_recorder_keeps_only_the_fastest_duplicate_within_window():
    recent = poly.RecentFrames(keep_ns=100)

    assert recent.first("same-frame", 1_000) is True
    assert recent.first("same-frame", 1_050) is False
    assert recent.first("same-frame", 1_151) is True
