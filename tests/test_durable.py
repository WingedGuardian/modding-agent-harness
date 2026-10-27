"""Metadata cannot become authoritative after incomplete or corrupt staging writes."""
import builtins
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from modding_harness import durable, installation, upgrade


class MetadataDurabilityTests(unittest.TestCase):
    def qualify(self, defect):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "transaction.json"
            value = {"v": 1, "members": [{"path": "fixture.ess", "sha256": "a" * 64}]}
            real_open, real_write, faults = builtins.open, os.write, []
            class Writer:
                def __init__(self, stream, name):
                    self.stream, self.name = stream, name
                def __enter__(self):
                    self.stream.__enter__()
                    return self
                def __exit__(self, *args):
                    result = self.stream.__exit__(*args)
                    if defect == "corrupt_before_publication":
                        with real_open(self.name, "r+b") as damaged:
                            damaged.write(b"?")
                        faults.append("corrupted closed staging file")
                    return result
                def __getattr__(self, key):
                    return getattr(self.stream, key)
                def write(self, data):
                    if defect == "short_write":
                        faults.append("short file write")
                        data = data[:max(1, len(data) // 2)]
                    return self.stream.write(data)
            def opening(name, mode="r", *args, **kwargs):
                stream = real_open(name, mode, *args, **kwargs)
                return Writer(stream, name) if mode == "xb" else stream
            def writing(fd, data):
                if defect == "short_write":
                    faults.append("short descriptor write")
                    data = data[:max(1, len(data) // 2)]
                return real_write(fd, data)
            with mock.patch.object(builtins, "open", side_effect=opening), mock.patch.object(os, "write", side_effect=writing):
                try:
                    durable.atomic_json(target, value)
                except OSError:
                    self.assertFalse(target.exists(), "damaged metadata must not be published")
                else:
                    self.assertEqual(json.loads(target.read_bytes()), value)
            self.assertTrue(faults, "the I/O fault must reach the actual writer")
            self.assertFalse(list(Path(folder).glob(".part-*")))

    def test_metadata_short_writes_are_completed_or_refused_before_publication(self):
        self.qualify("short_write")

    def test_corrupt_closed_metadata_is_refused_before_publication(self):
        self.qualify("corrupt_before_publication")

    def test_upgrade_cannot_publish_runtime_with_a_corrupted_rollback_plan(self):
        import hashlib
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            root, source, bundle = base / "installed", base / "source", base / "review"
            root.mkdir()
            source.mkdir()
            (root / "runtime.py").write_bytes(b"old runtime")
            (source / "runtime.py").write_bytes(b"new runtime")
            inventory = lambda raw: {"v": 1, "files": {"runtime.py": hashlib.sha256(raw).hexdigest()}}
            upgrade.stage_review_bundle(root, source, bundle, inventory(b"new runtime"), inventory(b"old runtime"), reserve=0)
            review_sha = hashlib.sha256((bundle / "complete.json").read_bytes()).hexdigest()
            real_open, damaged = builtins.open, []
            class CorruptAfterClose:
                def __init__(self, stream, name):
                    self.stream, self.name = stream, name
                def __enter__(self):
                    return self.stream.__enter__()
                def __exit__(self, *args):
                    result = self.stream.__exit__(*args)
                    with real_open(self.name, "r+b") as stream:
                        stream.write(b"?")
                    damaged.append(self.name)
                    return result
            def opening(name, mode="r", *args, **kwargs):
                stream = real_open(name, mode, *args, **kwargs)
                path = Path(name)
                if mode == "xb" and "upgrades" in path.parts and path.name.startswith(".part-"):
                    return CorruptAfterClose(stream, name)
                return stream
            with mock.patch.object(builtins, "open", side_effect=opening):
                with self.assertRaises(OSError):
                    installation.apply(root, bundle, review_sha, quiescent=True, reserve=0)
            self.assertTrue(damaged)
            self.assertEqual((root / "runtime.py").read_bytes(), b"old runtime")
