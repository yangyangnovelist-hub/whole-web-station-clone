from pathlib import Path
import zlib

import archive_day as archive


def test_stamp_and_day_selection_exclude_the_open_hour(tmp_path):
    group = archive.Group("poly", tmp_path, "jsonl.gz")
    closed = tmp_path / "poly_clob.20261009T07.jsonl.gz"
    open_hour = tmp_path / "poly_clob.20261009T08.jsonl.gz"
    unrelated = tmp_path / "notes.jsonl.gz"
    for path in (closed, open_hour, unrelated):
        path.touch()

    assert archive.stamp(closed) == ("20261009", "07")
    assert archive.files_for_day(group, "20261009", "2026100908") == [closed]


def test_mix_group_archives_the_isolated_aggregate_tape():
    group = archive.GROUPS["mix"]

    assert group.root == Path("/home/ubuntu/rec/data_mix")
    assert group.prefix == "mix"
    assert group.suffix == "txt.gz"


def test_prune_deletes_only_verified_old_files(tmp_path, monkeypatch):
    group = archive.Group("poly", tmp_path, "jsonl.gz")
    verified = tmp_path / "poly_clob.20261006T01.jsonl.gz"
    unverified = tmp_path / "poly_clob.20261006T02.jsonl.gz"
    recent = tmp_path / "poly_clob.20261008T01.jsonl.gz"
    for path in (verified, unverified, recent):
        path.write_bytes(path.name.encode())
    calls = []

    def fake_remote_ok(_s3, key, _size, _digest, *, require_valid_gzip=False):
        calls.append(require_valid_gzip)
        return "T01" in key

    monkeypatch.setattr(archive, "remote_ok", fake_remote_ok)
    monkeypatch.setattr(archive, "log", lambda _message: None)

    deleted, freed = archive.prune(object(), group, 2, archive.dt.date(2026, 10, 9))

    assert deleted == 1
    assert freed > 0
    assert not verified.exists()
    assert unverified.exists()
    assert recent.exists()
    assert calls == [True, True]


def test_gzip_check_marks_corrupt_stream_without_crashing(tmp_path, monkeypatch):
    path = tmp_path / "poly_clob.20261006T01.jsonl.gz"

    class BrokenGzip:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size):
            raise zlib.error("corrupt member")

    monkeypatch.setattr(archive.gzip, "open", lambda *_args, **_kwargs: BrokenGzip())

    assert archive.gzip_ok(path) is False
