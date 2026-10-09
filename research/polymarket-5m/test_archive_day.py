import gzip
import json
from pathlib import Path
import zlib

import archive_day as archive
from botocore.exceptions import ClientError


class ManifestS3:
    def __init__(self):
        self.body = None
        self.objects = {}
        self.uploads = 0

    def head_object(self, *, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        value, metadata = self.objects[Key]
        return {"ContentLength": len(value), "Metadata": metadata}

    def get_object(self, *, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

        class Body:
            def __init__(self, value):
                self.value = value

            def read(self):
                return self.value

        return {"Body": Body(self.objects[Key][0])}

    def upload_file(self, path, bucket, key, ExtraArgs):
        self.uploads += 1
        self.objects[key] = (Path(path).read_bytes(), ExtraArgs["Metadata"])

    def put_object(self, *, Bucket, Key, Body):
        self.body = json.loads(Body)
        self.objects[Key] = (Body, {})


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


def test_upload_day_fails_when_any_gzip_is_invalid(tmp_path, monkeypatch):
    group = archive.Group("poly", tmp_path, "jsonl.gz")
    path = tmp_path / "poly_clob.20261009T07.jsonl.gz"
    path.write_bytes(b"truncated")
    s3 = ManifestS3()
    monkeypatch.setattr(archive, "log", lambda _message: None)

    assert archive.upload_day(s3, group, "20261009", "2026100908") is False
    assert s3.body is None
    assert s3.uploads == 0


def test_upload_day_never_overwrites_a_different_remote_object(tmp_path, monkeypatch):
    group = archive.Group("poly", tmp_path, "jsonl.gz")
    path = tmp_path / "poly_clob.20261009T07.jsonl.gz"
    with gzip.open(path, "wt") as stream:
        stream.write("valid\n")
    key = f"poly/20261009/{path.name}"
    s3 = ManifestS3()
    s3.objects[key] = (b"different", {"sha256": "0" * 64, "gzip_ok": "True"})
    monkeypatch.setattr(archive, "log", lambda _message: None)

    try:
        archive.upload_day(s3, group, "20261009", "2026100908")
    except ValueError as exc:
        assert "immutable S3 collision" in str(exc)
    else:
        raise AssertionError("different remote bytes were accepted")

    assert s3.objects[key][0] == b"different"
    assert s3.body is None
    assert s3.uploads == 0


def test_formal_manifest_requires_every_frozen_route_hour(tmp_path, monkeypatch):
    group = archive.Group(
        "mix", tmp_path, "txt.gz", required_routes=("a", "b"), complete_from="20261010",
    )
    for route in group.required_routes:
        for hour in range(23 if route == "b" else 24):
            path = tmp_path / f"{route}.20261010T{hour:02d}.txt.gz"
            with gzip.open(path, "wt") as stream:
                stream.write("valid\n")
    s3 = ManifestS3()
    monkeypatch.setattr(archive, "log", lambda _message: None)

    assert archive.upload_day(s3, group, "20261010", "2026101100") is False
    assert s3.body is None
    assert s3.uploads == 0
