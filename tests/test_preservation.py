import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from modding_harness import durable
from modding_harness.preservation import PreservationFailed, SaveStore, protects
from modding_harness.profiles import skyrim, toy, x4


class PreservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.saves = self.root / "Saves"
        self.saves.mkdir()
        self.ess, self.skse = self.saves / "A.ess", self.saves / "A.skse"
        self.ess.write_bytes(b"ESS-A")
        self.skse.write_bytes(b"SKSE-A")
        self.store = SaveStore(self.root, save_roots=[self.saves], reserve=0)

    def backup(self):
        return self.store.backup([skyrim().members(self.ess)])

    def test_complete_pair_survives_without_any_index(self):
        first = self.backup()
        second = self.backup()
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first, self.store.load(first["id"]))
        self.assertEqual(11, first["stored_bytes"])
        self.assertEqual(0, second["stored_bytes"])
        self.assertEqual(11, second["deduplicated_bytes"])
        self.assertEqual(0, second["required_bytes"])
        self.assertEqual(11, second["logical_bytes"])

    def test_pair_selection_from_cosave(self):
        self.assertEqual(skyrim().members(self.ess), skyrim().members(self.skse))

    def test_copy_race_changes_earlier_member_without_metadata_change(self):
        original = durable.copy_verified
        st = self.ess.stat()

        def race(source, target, deadline):
            result = original(source, target, deadline)
            if source == str(self.skse):
                self.ess.write_bytes(b"ESS-B")
                os.utime(self.ess, ns=(st.st_atime_ns, st.st_mtime_ns))
            return result

        with patch.object(durable, "copy_verified", race):
            with self.assertRaises(PreservationFailed):
                self.backup()
        self.assertEqual([], list((self.store.root / "transactions").glob("*.json")))

    def test_missing_counterpart_appearing_invalidates_transaction(self):
        self.skse.unlink()
        original = durable.copy_verified

        def race(*args):
            result = original(*args)
            self.skse.write_bytes(b"NEW")
            return result

        with patch.object(durable, "copy_verified", race):
            with self.assertRaisesRegex(PreservationFailed, "counterpart appeared"):
                self.backup()

    def test_missing_counterpart_recorded(self):
        self.skse.unlink()
        record = self.backup()
        self.assertTrue(record["groups"][0]["members"][1]["missing"])

    def test_corrupt_reused_blob_refuses(self):
        first = self.backup()
        sha = first["groups"][0]["members"][0]["sha256"]
        (self.store.root / "blobs" / sha).write_bytes(b"WRONG")
        with self.assertRaisesRegex(PreservationFailed, "reused backup blob is corrupt"):
            self.backup()

    def test_destination_volume_reserve(self):
        self.store.reserve = 1 << 63
        with self.assertRaisesRegex(PreservationFailed, "destination space"):
            self.backup()

    def test_dedup_capacity_uses_required_new_bytes(self):
        self.backup()
        import shutil
        original = shutil.disk_usage
        self.store.reserve = 100
        with patch.object(shutil, "disk_usage", lambda p: original(p)._replace(free=100)):
            tx = self.backup()
        self.assertEqual(0, tx["required_bytes"])

    def test_capacity_lost_during_copy_refuses(self):
        import shutil
        original_usage = shutil.disk_usage
        original_copy = durable.copy_verified
        self.store.reserve = 100
        available = [1000]

        def copy(*args):
            result = original_copy(*args)
            available[0] = 0
            return result

        with patch.object(shutil, "disk_usage", lambda p: original_usage(p)._replace(free=available[0])):
            with patch.object(durable, "copy_verified", copy):
                with self.assertRaisesRegex(PreservationFailed, "free-space reserve changed"):
                    self.backup()

    def test_busy_lock_refuses_without_transaction(self):
        with patch("modding_harness.preservation.exclusive", side_effect=TimeoutError("store busy")):
            with self.assertRaisesRegex(PreservationFailed, "store busy"):
                self.backup()

    def test_process_lock_blocks_and_is_released_when_holder_exits(self):
        ready = self.root / "lock-ready"
        self.store.root.mkdir(parents=True)
        code = ("import sys,time; from pathlib import Path; from modding_harness.locking import exclusive; "
                "lock=exclusive(sys.argv[1]); lock.__enter__(); Path(sys.argv[2]).write_text('ready'); time.sleep(10)")
        process = subprocess.Popen([sys.executable, "-B", "-c", code, str(self.store.root / ".lock"), str(ready)])
        try:
            until = time.monotonic() + 3
            while not ready.exists() and time.monotonic() < until:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), "owned child did not acquire its lock")
            with self.assertRaisesRegex(PreservationFailed, "lock is busy"):
                self.store.backup([[self.ess, self.skse]], deadline=time.monotonic() + 0.05)
        finally:
            process.terminate()
            process.wait(timeout=3)
        self.assertTrue(self.backup()["complete"])

    @unittest.skipIf(os.name == "nt", "POSIX distinct physical case variants")
    def test_distinct_case_variants_are_refused_as_ambiguous_pair(self):
        (self.saves / "A.ESS").write_bytes(b"different physical file")
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            skyrim().members(self.ess)

    def test_expired_deadline_and_active_native_writer_refuse(self):
        with self.assertRaises(PreservationFailed):
            self.store.backup([[self.ess, self.skse]], deadline=time.monotonic() - 1)
        with self.assertRaisesRegex(PreservationFailed, "quiescent"):
            self.store.backup([[self.ess, self.skse]], known_writer_active=True)

    def test_failed_manifest_publication_never_acknowledged(self):
        with patch.object(durable, "atomic_json", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(PreservationFailed, "disk full"):
                self.backup()

    def test_short_writes_are_completed(self):
        path = self.root / "short"
        real = os.write
        with open(path, "wb") as stream:
            with patch.object(os, "write", lambda fd, data: real(fd, data[:3])):
                durable.write_all(stream.fileno(), b"abcdefghijk")
        self.assertEqual(b"abcdefghijk", path.read_bytes())

    def test_zero_write_fails(self):
        with patch.object(os, "write", return_value=0):
            with self.assertRaises(OSError):
                durable.write_all(99, b"a")

    def test_no_clobber_concurrent_destination(self):
        candidate, target = self.root / "candidate", self.root / "target"
        candidate.write_bytes(b"ours")
        target.write_bytes(b"other writer")
        with self.assertRaises(FileExistsError):
            durable.publish_new(candidate, target)
        self.assertEqual(b"other writer", target.read_bytes())

    def test_store_overlap_and_protection(self):
        with self.assertRaises(ValueError):
            SaveStore(self.root, self.saves / "backups", [self.saves])
        with self.assertRaises(ValueError):
            SaveStore(self.root, self.root, [self.saves])
        self.assertTrue(protects(self.store.root / "blobs/a", [self.store.root]))
        self.assertTrue(protects(self.root, [self.store.root], ancestor=True))
        self.assertFalse(protects(self.root / "elsewhere", [self.store.root], ancestor=True))

    def test_profiles_are_independent(self):
        self.assertEqual((self.saves / "a.xml.gz",), x4().members(self.saves / "a.xml.gz"))
        path = self.saves / "third.save"
        path.write_bytes(b"third game")
        record = self.store.backup([toy().members(path)])
        self.assertEqual(1, len(record["groups"][0]["members"]))
        with self.assertRaises(ValueError):
            x4().members(self.ess)

    def test_incomplete_manifest_is_not_a_backup(self):
        record = self.backup()
        file = self.store.root / "transactions" / (record["id"] + ".json")
        record["complete"] = False
        file.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.store.load(record["id"])


if __name__ == "__main__":
    unittest.main()
