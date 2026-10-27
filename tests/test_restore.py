import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modding_harness import durable
from modding_harness.preservation import PreservationFailed, SaveStore


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.a, self.b = self.root / "A.ess", self.root / "A.skse"
        self.a.write_bytes(b"ESS-A")
        self.b.write_bytes(b"SKSE-A")
        self.store = SaveStore(self.root, reserve=0)

    def test_member_selection_expands_complete_pair(self):
        tx = self.store.backup([[self.a, self.b]])
        self.a.write_bytes(b"ESS-B")
        self.b.write_bytes(b"SKSE-B")
        result = self.store.restore(tx["id"], selected=[self.a])
        self.assertEqual(2, len(result["published"]))
        self.assertEqual([b"ESS-A", b"SKSE-A"], [Path(p).read_bytes() for p in result["published"]])
        self.assertEqual(b"ESS-B", self.a.read_bytes())

    def test_healthy_selected_save_can_be_recovered_when_another_group_is_corrupt(self):
        other = self.root / "Other.ess"
        other.write_bytes(b"other save")
        tx = self.store.backup([[self.a, self.b], [other]])
        bad = tx["groups"][1]["members"][0]["sha256"]
        (self.store.root / "blobs" / bad).write_bytes(b"corrupt")
        with self.assertRaises(PreservationFailed):
            self.store.load(tx["id"])
        result = self.store.restore(tx["id"], selected=[self.b])
        self.assertEqual([b"ESS-A", b"SKSE-A"], [Path(p).read_bytes() for p in result["published"]])

    def test_unexpected_counterpart_refuses_before_any_write(self):
        self.b.unlink()
        tx = self.store.backup([[self.a, self.b]])
        self.a.write_bytes(b"ESS-B")
        self.b.write_bytes(b"SKSE-B")
        with self.assertRaisesRegex(PreservationFailed, "unexpected counterpart"):
            self.store.restore(tx["id"], in_place=True)
        self.assertEqual(b"ESS-B", self.a.read_bytes())

    def test_in_place_preserves_complete_current_pair(self):
        tx = self.store.backup([[self.a, self.b]])
        self.a.write_bytes(b"ESS-B")
        self.b.write_bytes(b"SKSE-B")
        result = self.store.restore(tx["id"], selected=[self.b], in_place=True)
        self.assertEqual(b"ESS-A", self.a.read_bytes())
        self.assertEqual(b"SKSE-A", self.b.read_bytes())
        import json
        journal = json.loads((Path(result["journal"]) / "0000-plan.json").read_text())
        backup = self.store.load(journal["prebackup"])
        self.assertEqual(2, len(backup["groups"][0]["members"]))

    def test_in_place_can_restore_an_entirely_absent_pair(self):
        tx = self.store.backup([[self.a, self.b]])
        self.a.unlink()
        self.b.unlink()
        result = self.store.restore(tx["id"], in_place=True)
        self.assertEqual([b"ESS-A", b"SKSE-A"], [self.a.read_bytes(), self.b.read_bytes()])
        self.assertEqual(2, len(result["published"]))

    def test_entirely_absent_group_rechecks_both_members_after_staging(self):
        tx = self.store.backup([[self.a, self.b]])
        original = durable.copy_verified
        for appeared in (self.a, self.b):
            with self.subTest(member=appeared.name):
                self.a.unlink(missing_ok=True)
                self.b.unlink(missing_ok=True)
                fired = []
                def race(source, target, deadline=None):
                    result = original(source, target, deadline)
                    if Path(target).name.startswith(".restore-") and not fired:
                        appeared.write_bytes(b"FOREIGN")
                        fired.append(True)
                    return result
                with patch.object(durable, "copy_verified", race):
                    with self.assertRaisesRegex(PreservationFailed, "published=\\[\\]"):
                        self.store.restore(tx["id"], in_place=True)
                self.assertEqual([True], fired)
                self.assertEqual(b"FOREIGN", appeared.read_bytes())
                self.assertFalse((self.b if appeared == self.a else self.a).exists())

    def test_absent_group_is_rechecked_even_when_another_group_has_a_prebackup(self):
        other = self.root / "Other.ess"
        other.write_bytes(b"OTHER-BACKUP")
        tx = self.store.backup([[self.a, self.b], [other]])
        other.unlink()
        self.a.write_bytes(b"CURRENT-A")
        self.b.write_bytes(b"CURRENT-B")
        original = durable.copy_verified
        fired = []
        def race(source, target, deadline=None):
            result = original(source, target, deadline)
            if Path(target).name.startswith(".restore-") and not fired:
                other.write_bytes(b"FOREIGN")
                fired.append(True)
            return result
        with patch.object(durable, "copy_verified", race):
            with self.assertRaisesRegex(PreservationFailed, "published=\\[\\]"):
                self.store.restore(tx["id"], in_place=True)
        self.assertEqual([True], fired)
        self.assertEqual([b"CURRENT-A", b"CURRENT-B", b"FOREIGN"],
                         [self.a.read_bytes(), self.b.read_bytes(), other.read_bytes()])

    def test_previously_absent_member_uses_atomic_no_clobber_at_publication(self):
        self.b.unlink()
        tx = self.store.backup([[self.a, self.b]])
        self.a.unlink()
        original_replace, original_publish = os.replace, durable.publish_new
        fired = []
        def race(original, source, target):
            if Path(target) == self.a:
                self.a.write_bytes(b"FOREIGN")
                fired.append(True)
            return original(source, target)
        with patch.object(os, "replace", lambda a, b: race(original_replace, a, b)):
            with patch.object(durable, "publish_new", lambda a, b: race(original_publish, a, b)):
                with self.assertRaisesRegex(PreservationFailed, "published=\\[\\]"):
                    self.store.restore(tx["id"], in_place=True)
        self.assertEqual([True], fired)
        self.assertEqual(b"FOREIGN", self.a.read_bytes())

    def test_recorded_missing_counterpart_appearing_at_publication_reports_partial_failure(self):
        self.b.unlink()
        tx = self.store.backup([[self.a, self.b]])
        self.a.unlink()
        original_replace, original_publish = os.replace, durable.publish_new
        def race(original, source, target):
            if Path(target) == self.a:
                self.b.write_bytes(b"FOREIGN-COSAVE")
            return original(source, target)
        with patch.object(os, "replace", lambda a, b: race(original_replace, a, b)):
            with patch.object(durable, "publish_new", lambda a, b: race(original_publish, a, b)):
                with self.assertRaisesRegex(PreservationFailed, "published=.*A.ess"):
                    self.store.restore(tx["id"], in_place=True)
        self.assertEqual(b"FOREIGN-COSAVE", self.b.read_bytes())
        self.assertEqual(b"ESS-A", self.a.read_bytes())

    def test_concurrent_destination_preserved_and_partial_reported(self):
        tx = self.store.backup([[self.a, self.b]])
        original = durable.publish_new
        dest = self.root / "restored"

        def race(part, target):
            if target.name == "A.skse":
                target.write_bytes(b"OTHER")
            return original(part, target)

        with patch.object(durable, "publish_new", race):
            with self.assertRaisesRegex(PreservationFailed, "published="):
                self.store.restore(tx["id"], destination=dest)
        self.assertEqual(b"OTHER", (dest / "0/A.skse").read_bytes())
        self.assertEqual([], list(dest.rglob(".restore-*")))


if __name__ == "__main__":
    unittest.main()
