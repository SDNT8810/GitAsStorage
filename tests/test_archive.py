"""Offline integration and failure tests; all remotes are disposable file:// URLs."""
from contextlib import redirect_stdout, redirect_stderr
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from git_as_storage import cli, transfer
from git_as_storage.restore import restore, validate_manifest


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="gitasstorage-test-")).resolve()
        self.source = self.root / "original media"
        self.source.mkdir()
        self.folder = self.source / ".gitasstorage"
        self.environment = patch.dict(transfer.ENV, {
            "GIT_AUTHOR_NAME": "Offline Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Offline Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"})
        self.environment.start()
        self.intervals = patch.multiple(transfer, PUSH_INTERVAL=0, POLL_INTERVAL=0.01)
        self.intervals.start()

    def tearDown(self):
        self.intervals.stop()
        self.environment.stop()
        root = self.root
        if root.parent != Path(tempfile.gettempdir()).resolve() or not root.name.startswith("gitasstorage-test-"):
            raise RuntimeError("Unsafe test cleanup target")
        def remove_readonly(function, path, error):
            if not Path(path).resolve().is_relative_to(root):
                raise RuntimeError("Unsafe test cleanup child")
            os.chmod(path, stat.S_IWRITE)
            function(path)
        shutil.rmtree(root, onexc=remove_readonly)

    def remote(self, name):
        location = self.root / (name + ".git")
        transfer.git(["init", "--bare", "--initial-branch=main", str(location)])
        transfer.git(["-C", str(location), "config", "uploadpack.allowFilter", "true"])
        return location.as_uri()

    def command(self, *arguments, expected=0):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = cli.main(list(arguments))
        self.assertEqual(result, expected)

    def state(self):
        return json.loads((self.folder / "state.json").read_text(encoding="utf-8"))

    def initialize(self, remote, **options):
        arguments = ["init", str(self.source), "--remote", remote, "--name", "archive-01",
                     "--chunk-mib", "1", "--batch-mib", "2"]
        for name, value in options.items():
            arguments += ["--" + name.replace("_", "-"), str(value)]
        self.command(*arguments)

    def test_multi_repository_restore_resume_and_integrity(self):
        originals = {
            "empty.bin": b"",
            "movie.bin": bytes(range(256)) * (5 * 1024**2 // 256),
            "camera.xml": b"<camera>Keep LF exactly</camera>\n",
            "subtitles.srt": b"1\r\n00:00:00,000 --> 00:00:01,000\r\nKeep CRLF\r\n",
            "nested/photo-❤.txt": "Wedding archive\n".encode("utf-8"),
        }
        for name, data in originals.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        first = self.remote("first")
        self.initialize(first)
        self.command("upload", "--state-dir", str(self.folder), "--max-batches", "1")
        checkpoint = self.state()
        self.assertFalse(checkpoint["complete"])
        self.assertEqual(checkpoint["_source"], str(self.source))
        first_tip = transfer.remote_tip(first)
        second = self.remote("second")
        self.command("continue", "--state-dir", str(self.folder), "--remote", second, "--name", "archive-02")
        migration = self.state()
        self.command("upload", "--state-dir", str(self.folder))
        state = self.state()
        self.assertTrue(state["complete"])
        self.assertEqual(state["uploaded_bytes"], sum(map(len, originals.values())))
        self.assertEqual(transfer.remote_tip(first), first_tip)
        movie = next(e for e in state["entries"] if e["path"] == "movie.bin")
        self.assertEqual({c["repository"] for c in movie["chunks"]}, {"archive-01", "archive-02"})
        clones = {}
        for name, remote in [("archive-01", first), ("archive-02", second)]:
            clone = self.root / name
            # The archive's * -text rule must protect LF and CRLF even with autocrlf enabled.
            transfer.git(["clone", "-c", "core.autocrlf=true", remote, str(clone)])
            clones[name] = clone
        latest = clones["archive-02"]
        published = json.loads((latest / "backup-manifest.json").read_text(encoding="utf-8"))
        self.assertNotIn("_source", published)
        self.assertFalse(any(k.startswith("_") for k in published))
        self.assertFalse((latest / movie["chunks"][0]["path"]).exists())
        output = self.root / "restored"
        result = subprocess.run([sys.executable, str(latest / "restore.py"), str(output)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        for name, data in originals.items():
            self.assertEqual((output / name).read_bytes(), data)
            self.assertEqual((self.source / name).read_bytes(), data)
        # Simulate a crash after remote push but before the local checkpoint write.
        final_tip = transfer.remote_tip(second)
        transfer.save(self.folder / "state.json", migration)
        self.command("upload", "--state-dir", str(self.folder))
        self.assertTrue(self.state()["complete"])
        self.assertEqual(self.state()["_source"], str(self.source))
        self.assertEqual(transfer.remote_tip(second), final_tip)
        with self.assertRaises(FileExistsError):
            restore(latest, output)
        missing_output = self.root / "missing-output"
        with self.assertRaises(FileNotFoundError):
            restore(latest, missing_output, ["archive-01=" + str(self.root / "does-not-exist")])
        self.assertFalse(missing_output.exists())
        damaged = clones["archive-01"] / movie["chunks"][0]["path"]
        content = damaged.read_bytes()
        damaged.write_bytes(bytes([content[0] ^ 1]) + content[1:])
        bad_output = self.root / "bad-output"
        with self.assertRaisesRegex(ValueError, "Checksum mismatch"):
            restore(latest, bad_output)
        self.assertFalse((bad_output / "movie.bin").exists())
        self.assertFalse(list(bad_output.rglob("*.gitasstorage-restoring")))

    def test_source_changes_stop_before_upload(self):
        media = self.source / "file.bin"
        media.write_bytes(b"original")
        remote = self.remote("changed")
        self.initialize(remote)
        media.write_bytes(b"changed and longer")
        self.command("upload", "--state-dir", str(self.folder), expected=1)
        self.assertIsNone(transfer.remote_tip(remote))
        self.assertEqual(self.state()["uploaded_bytes"], 0)

    def test_local_repository_budget_pauses(self):
        (self.source / "movie.bin").write_bytes(b"x" * (3 * 1024**2))
        remote = self.remote("budget")
        self.initialize(remote, repo_budget_gib=2.1 / 1024)
        self.command("upload", "--state-dir", str(self.folder))
        self.assertEqual(self.state()["uploaded_bytes"], 2 * 1024**2)
        self.assertFalse(self.state()["complete"])

    def test_lock_blocks_concurrent_uploader(self):
        (self.source / "file.bin").write_bytes(b"test")
        self.initialize(self.remote("lock"))
        with cli.state_lock(self.folder):
            self.command("upload", "--state-dir", str(self.folder), expected=1)

    def test_nonempty_repository_is_not_reused(self):
        (self.source / "file.bin").write_bytes(b"test")
        remote = self.remote("nonempty")
        self.initialize(remote)
        self.command("upload", "--state-dir", str(self.folder))
        other = self.root / "other-state"
        self.command("init", str(self.source), "--remote", remote, "--state-dir", str(other), expected=1)


class InputSafetyTests(unittest.TestCase):
    def manifest(self):
        return {"format": 2, "complete": True, "chunk_bytes": 1024,
                "repositories": {"archive-01": {"url": "file:///unused"}}, "active_repository": "archive-01",
                "entries": [{"path": "safe.bin", "size": 0, "chunks": [
                    {"path": "files/safe.bin", "size": 0, "sha256": hashlib.sha256(b"").hexdigest(),
                     "repository": "archive-01"}]}]}

    def test_manifest_paths_and_missing_data(self):
        validate_manifest(self.manifest())
        for unsafe in ["../outside", "/absolute", "C:/absolute", "nested/../outside", "back\\slash"]:
            manifest = self.manifest()
            manifest["entries"][0]["path"] = unsafe
            with self.assertRaises(ValueError):
                validate_manifest(manifest)
        manifest = self.manifest()
        manifest["entries"][0]["chunks"] = []
        with self.assertRaises(ValueError):
            validate_manifest(manifest)

    def test_url_credentials_and_unsupported_hosts_rejected(self):
        for url in ["https://user:secret@github.com/person/repo.git", "https://example.com/p/r", "https://github.com/p/r?token=secret"]:
            with self.assertRaises(ValueError):
                cli.remote_details(url)

    def test_public_repository_rejected(self):
        response = io.BytesIO(json.dumps({"private": False, "permissions": {"push": True}}).encode())
        with patch.dict(os.environ, {"GH_TOKEN": "fake-unit-test-token"}), patch("urllib.request.urlopen", return_value=response):
            with self.assertRaisesRegex(ValueError, "public repository"):
                cli.check_private("https://github.com/example/archive-01.git")

    def test_continuation_keeps_original_checkpoint(self):
        state = self.manifest()
        state.update(complete=False, _last_commit="a" * 40)
        before = copy.deepcopy(state)
        next_state = transfer.prepare_continuation(state, "archive-01", "file:///one", "archive-02", "file:///two")
        self.assertEqual(state, before)
        self.assertEqual(next_state["repositories"]["archive-01"]["checkpoint"], "a" * 40)
        self.assertEqual(next_state["entries"][0]["chunks"], state["entries"][0]["chunks"])


if __name__ == "__main__":
    unittest.main()
