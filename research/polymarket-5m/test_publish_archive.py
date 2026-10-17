"""Offline publication gating tests; synthetic bytes, never a real backtest."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import publish_archive as pub


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "sealed.tar"
        self.source.write_bytes(b"only synthetic transport bytes" * 11)
        self.parts = self.root / "parts"
        self.env = patch.dict(os.environ, {"GITHUB_RUN_ID": "12345"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def prepared(self):
        return pub.prepare(self.source, "source-recording-12345", self.parts, 1, 128)

    def steps(self, count):
        return {key: {"outcome": "success" if i < count else "skipped",
                      "outputs": {"artifact-id": str(100+i)} if i < count else {}}
                for i, key in enumerate(pub.PART_IDS)}

    def completed(self, steps=None):
        result = self.prepared()
        pub.complete(self.parts, "source-recording-12345", result["manifest-sha256"],
                     self.steps(result["part-count"]) if steps is None else steps)
        return result

    def test_success_index_exact_parts_and_no_extra_environment(self):
        result = self.completed()
        index = json.loads((self.parts / "publish-index.json").read_text())
        self.assertEqual(index["schema"], "research-archive-publication-v1")
        self.assertEqual(index["status"], "all_parts_uploaded")
        self.assertEqual(index["run_id"], "12345")
        self.assertEqual(index["manifest_sha256"], result["manifest-sha256"])
        self.assertEqual(len(index["parts"]), result["part-count"])
        self.assertEqual(index["parts"][0]["artifact_name"], "source-recording-12345-part-00000")
        self.assertNotIn("environment", index)

    def test_failed_part_prevents_index(self):
        result = self.prepared()
        steps = self.steps(result["part-count"])
        steps["part_01"]["outcome"] = "failure"
        with self.assertRaisesRegex(ValueError, "failed or skipped"):
            pub.complete(self.parts, "x", result["manifest-sha256"], steps)
        self.assertFalse((self.parts / "publish-index.json").exists())

    def test_skipped_required_part_prevents_index(self):
        result = self.prepared()
        steps = self.steps(result["part-count"])
        steps["part_00"]["outcome"] = "skipped"
        with self.assertRaises(ValueError):
            pub.complete(self.parts, "x", result["manifest-sha256"], steps)

    def test_extra_uploaded_part_prevents_index(self):
        result = self.prepared()
        steps = self.steps(result["part-count"])
        steps["part_15"] = {"outcome": "success", "outputs": {"artifact-id": "999"}}
        with self.assertRaisesRegex(ValueError, "extra part"):
            pub.complete(self.parts, "x", result["manifest-sha256"], steps)

    def test_missing_step_prevents_index(self):
        result = self.prepared()
        steps = self.steps(result["part-count"])
        del steps["part_15"]
        with self.assertRaisesRegex(ValueError, "missing upload step"):
            pub.complete(self.parts, "x", result["manifest-sha256"], steps)

    def test_missing_artifact_id_prevents_index(self):
        result = self.prepared()
        steps = self.steps(result["part-count"])
        steps["part_00"]["outputs"] = {}
        with self.assertRaisesRegex(ValueError, "artifact ID"):
            pub.complete(self.parts, "x", result["manifest-sha256"], steps)

    def test_duplicate_artifact_id_prevents_index(self):
        result = self.prepared()
        steps = self.steps(result["part-count"])
        steps["part_01"]["outputs"]["artifact-id"] = "100"
        with self.assertRaisesRegex(ValueError, "duplicate"):
            pub.complete(self.parts, "x", result["manifest-sha256"], steps)

    def test_wrong_trusted_manifest_hash(self):
        result = self.prepared()
        with self.assertRaisesRegex(ValueError, "manifest hash"):
            pub.complete(self.parts, "x", "0"*64, self.steps(result["part-count"]))

    def test_part_mutation_prevents_index(self):
        result = self.prepared()
        with (self.parts / "part-00000.zip").open("ab") as stream:
            stream.write(b"mutation")
        with self.assertRaisesRegex(ValueError, "changed after prepare"):
            pub.complete(self.parts, "x", result["manifest-sha256"], self.steps(result["part-count"]))

    def test_count_cap_checked_before_split(self):
        with self.source.open("r+b") as stream:
            stream.truncate(pub.DEFAULT_PART * 16 + 1)
        with patch.object(pub, "split_archive") as split:
            with self.assertRaisesRegex(ValueError, "16 separate"):
                pub.prepare(self.source, "x", self.parts)
            split.assert_not_called()
        self.assertFalse(self.parts.exists())

    def test_no_source_symlink(self):
        link = self.root / "link.tar"
        link.symlink_to(self.source)
        with self.assertRaises(ValueError):
            pub.prepare(link, "x", self.parts)

    def test_no_staging_overwrite(self):
        self.parts.mkdir()
        sentinel = self.parts / "untouched"
        sentinel.write_text("retain")
        with self.assertRaises(ValueError):
            self.prepared()
        self.assertEqual(sentinel.read_text(), "retain")

    def test_unsafe_prefix_and_newline_path(self):
        for prefix in ("$(touch PWN)", "a\nb", "../escape", ""):
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                pub.prepare(self.source, prefix, self.parts)
        with self.assertRaises(ValueError):
            pub.prepare(self.source, "okay", str(self.parts)+"\n")

    def test_retention_and_payload_caps(self):
        for retention in (0, 91, "1;echo", "01", 1.5):
            with self.subTest(retention=retention), self.assertRaises(ValueError):
                pub.prepare(self.source, "x", self.parts, retention)
        with self.assertRaises(ValueError):
            pub.prepare(self.source, "x", self.parts, part_bytes=pub.DEFAULT_PART+1)

    def test_invalid_run_id_prevents_index(self):
        result = self.prepared()
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "untrusted"}), self.assertRaises(ValueError):
            pub.complete(self.parts, "x", result["manifest-sha256"], self.steps(result["part-count"]))

    def test_github_output_scalar_fields(self):
        output = self.root / "gha-output"
        pub.github_outputs({"part-count": 3, "manifest-sha256": "a"*64}, output)
        self.assertEqual(output.read_text(), "part-count=3\nmanifest-sha256="+"a"*64+"\n")
        with self.assertRaises(ValueError):
            pub.github_outputs({"prefix": "a\nb"}, output)

    def test_cli_uses_small_parts_and_never_dumps_step_json(self):
        self.source.write_bytes(b"x"*(2*pub.BLOCK+10))
        env = {"ARCHIVE_PATH": str(self.source), "ARTIFACT_PREFIX": "ci-12345",
               "PARTS_DIR": str(self.parts), "RETENTION_DAYS": "1", "PART_MIB": "1",
               "GITHUB_OUTPUT": str(self.root / "out")}
        output = io.StringIO()
        with patch.dict(os.environ, env), contextlib.redirect_stdout(output):
            self.assertEqual(pub.main(["prepare"]), 0)
        self.assertEqual(json.loads(output.getvalue())["part-count"], 3)
        self.assertNotIn(str(self.source), output.getvalue())

    def test_composite_uploads_each_part_independently(self):
        action = Path(__file__).resolve().parents[2] / ".github/actions/publish-research-archive/action.yml"
        content = action.read_text()
        self.assertEqual(content.count("uses: actions/upload-artifact@v4"), 17)
        for i in range(16):
            self.assertIn(f"id: part_{i:02d}", content)
            self.assertIn(f"path: ${{{{ inputs.parts-dir }}}}/part-{i:05d}.zip", content)
        self.assertIn("UPLOAD_STEP_RESULTS: ${{ toJSON(steps) }}", content)
        self.assertNotIn("workflow run", content)


if __name__ == "__main__":
    unittest.main()
